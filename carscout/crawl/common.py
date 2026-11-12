"""Shared crawl helpers: classification, cursor + fingerprints, backoff scheduling."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from carscout.config import settings
from carscout.db import pool
from carscout.ingest import record_anomaly, register_failure, register_success, circuit_open
from carscout.parsers import Anomaly
from carscout.utils import canonical_json, content_fingerprint, normalize_url, sha256_text

FP_PREFIX = "carscout:fp"


# ---------------------------------------------------------------------------
# fingerprints (Redis incremental dedup)
# ---------------------------------------------------------------------------
def detail_fingerprint(platform: str, pid: str, fields: dict[str, Any]) -> str:
    return content_fingerprint("detail", f"/{platform.lower()}/detail/{pid}", fields)


def seen_unchanged(platform: str, pid: str, fp: str) -> bool:
    from carscout.db.pool import get_redis
    r = get_redis()
    key = f"{FP_PREFIX}:{platform}:{pid}"
    return r.get(key) == fp


def remember_fingerprint(platform: str, pid: str, fp: str) -> None:
    from carscout.db.pool import get_redis
    r = get_redis()
    key = f"{FP_PREFIX}:{platform}:{pid}"
    r.set(key, fp, ex=settings.fingerprint_ttl_seconds)


# ---------------------------------------------------------------------------
# runs / cursors
# ---------------------------------------------------------------------------
def create_run(platform: str, run_date: date, mode: str = "incremental",
               parent_run_id: str | None = None, scope: str = "all") -> dict[str, Any]:
    with pool.tx() as conn:
        cur = conn.execute(
            """INSERT INTO crawl_runs (platform, scope, run_date, mode, status, parent_run_id)
               VALUES (%s, %s, %s, %s, 'running', %s) RETURNING *""",
            (platform.upper(), scope, run_date, mode, parent_run_id),
        )
        row = cur.fetchone()
        cur.close()
        conn.execute(
            """INSERT INTO platform_health (platform) VALUES (%s)
               ON CONFLICT (platform) DO NOTHING""", (platform.upper(),))
        return row or {}


def get_cursor(platform: str, scope: str = "all") -> dict[str, Any] | None:
    return pool.query_one(
        "SELECT * FROM crawl_cursors WHERE platform = %s AND scope = %s", (platform.upper(), scope))


def ensure_cursor(platform: str, scope: str = "all") -> dict[str, Any]:
    row = get_cursor(platform, scope)
    if row:
        return row
    pool.execute(
        """INSERT INTO crawl_cursors (platform, scope, cursor, completed)
           VALUES (%s, %s, '{}'::jsonb, TRUE)
           ON CONFLICT (platform, scope) DO NOTHING""", (platform.upper(), scope))
    return get_cursor(platform, scope) or {}


def save_cursor(platform: str, scope: str, cursor: dict[str, Any], *, completed: bool,
                run_id: str | None, run_date: date, items_seen: int | None = None,
                ok_pages_delta: int = 0, last_error: dict[str, Any] | None = None,
                stopped_reason: str | None = None) -> None:
    with pool.tx() as conn:
        conn.execute(
            """INSERT INTO crawl_cursors
                 (platform, scope, cursor, completed, run_id, run_date, items_seen,
                  consecutive_ok_pages, last_error, stopped_reason, updated_at)
               VALUES (%s, %s, %s::jsonb, %s, %s, %s, COALESCE(%s, 0), %s, %s::jsonb, %s, now())
               ON CONFLICT (platform, scope) DO UPDATE SET
                 cursor = EXCLUDED.cursor,
                 completed = EXCLUDED.completed,
                 run_id = EXCLUDED.run_id,
                 run_date = EXCLUDED.run_date,
                 items_seen = COALESCE(%s, crawl_cursors.items_seen),
                 consecutive_ok_pages = CASE WHEN %s > 0
                     THEN crawl_cursors.consecutive_ok_pages + %s
                     WHEN %s THEN 0 ELSE crawl_cursors.consecutive_ok_pages END,
                 last_error = EXCLUDED.last_error,
                 stopped_reason = EXCLUDED.stopped_reason,
                 updated_at = now()""",
            (platform.upper(), scope, canonical_json(cursor), completed, run_id, run_date,
             items_seen, max(0, ok_pages_delta),
             json.dumps(last_error, ensure_ascii=False, default=str) if last_error else None,
             stopped_reason,
             items_seen, max(0, ok_pages_delta), max(0, ok_pages_delta),
             ok_pages_delta < 0))


def finish_run(run_id: str, status: str, *, stats: dict[str, Any] | None = None,
               cursor_after: dict[str, Any] | None = None, error: str | None = None) -> None:
    pool.execute(
        """UPDATE crawl_runs SET status = %s, finished_at = now(), stats = stats || %s::jsonb,
                  cursor_after = %s::jsonb, error = %s WHERE id = %s""",
        (status, json.dumps(stats or {}, ensure_ascii=False, default=str),
         json.dumps(cursor_after, ensure_ascii=False, default=str) if cursor_after else None,
         error, run_id))


def update_run_stats(run_id: str, **stats: Any) -> None:
    pool.execute("UPDATE crawl_runs SET stats = stats || %s::jsonb WHERE id = %s",
                 (json.dumps(stats, ensure_ascii=False, default=str), run_id))


# ---------------------------------------------------------------------------
# failure handling
# ---------------------------------------------------------------------------
def failure_policy(platform: str, category: str, detail: str = "", *, attempt: int = 0) -> dict[str, Any]:
    """Record a failure and decide whether to retry / trip the breaker."""
    register_failure(platform, category, detail)
    open_now, health = circuit_open(platform)
    return {
        "circuit_open": open_now,
        "failures": (health or {}).get("consecutive_failures", 0),
        "retry": attempt < settings.max_retries,
        "backoff": min(settings.retry_max_delay,
                       settings.retry_base_delay * (2 ** attempt)),
    }


def success_policy(platform: str) -> None:
    register_success(platform)


def anomaly_category(an: Anomaly | None) -> str:
    return an.category if an else "ok"


def archive_and_log(run_id: str, platform: str, url: str, kind: str, body: Any,
                    status: int, category: str | None, content_type: str | None = None,
                    extra: dict[str, Any] | None = None) -> int:
    from carscout.ingest import save_raw
    aid = save_raw(run_id, platform, url, kind, body, http_status=status,
                   category=category, content_type=content_type, extra=extra)
    if category and category not in ("ok", "empty_confirmed"):
        record_anomaly(run_id, platform, category, url=url, http_status=status,
                       detail=extra or {}, archive_id=aid)
    return aid



