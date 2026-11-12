"""Batch archiving of raw responses + anomaly recording + health bookkeeping.

Every list page, detail payload, XHR body and anomaly stub is written to disk
under DATA_DIR/archive/YYYY-MM-DD/<platform>/<run-short>/ so a later
re-layout can be replayed offline (see `carscout.cli archive-grep`).
"""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from carscout.config import settings
from carscout.db import pool
from carscout.utils import sha256_text

_write_counter = 0


def archive_root() -> Path:
    root = Path(settings.data_dir) / settings.archive_dir_name
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:120]


def save_raw(run_id: str, platform: str, url: str, kind: str, body: str | bytes,
             *, http_status: int | None = None, category: str | None = None,
             content_type: str | None = None, extra: dict[str, Any] | None = None,
             force: bool = False) -> int:
    """Persist one raw response; returns ingest_archive row id.

    Small text bodies are gzip-compressed. Identical (url, sha) pairs for the same
    run are still written once (batch archive, not per-request spam).
    """
    global _write_counter
    if isinstance(body, str):
        raw_bytes = body.encode("utf-8", errors="replace")
    elif isinstance(body, (bytes, bytearray)):
        raw_bytes = bytes(body)
    else:
        # dict / list payloads (JSON XHR bodies) are archived as canonical JSON
        import json as _json
        raw_bytes = _json.dumps(body, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(raw_bytes).hexdigest()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_short = (run_id or "noid").split("-")[0][:12]
    d = archive_root() / day / platform.upper() / run_short
    d.mkdir(parents=True, exist_ok=True)
    stem = _sanitize(f"{kind}_{hashlib.md5(url.encode()).hexdigest()[:12]}_{digest[:10]}")
    ext = _ext_for(content_type, kind)
    path = d / f"{stem}{ext}.gz"
    if not path.exists() or force:
        with gzip.open(path, "wb", compresslevel=6) as fh:
            fh.write(raw_bytes)
        _write_counter += 1
    meta_path = d / f"{stem}{ext}.meta.json"
    if not meta_path.exists():
        meta_path.write_text(json.dumps({
            "url": url, "platform": platform, "kind": kind, "http_status": http_status,
            "category": category, "content_type": content_type,
            "sha256": digest, "bytes": len(raw_bytes), "extra": extra or {},
            "archived_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        with pool.tx() as conn:
            cur = conn.execute(
                """INSERT INTO ingest_archive
                   (run_id, platform, url, url_norm, http_status, kind, category,
                    content_type, path, size_bytes, sha256)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (run_id if run_id and run_id != "noid" else None, platform.upper(), url,
                 url, http_status, kind, category, content_type,
                 str(path.relative_to(Path(settings.data_dir))), len(raw_bytes), digest),
            )
            row = cur.fetchone()
            cur.close()
            return int(row["id"]) if row else 0
    except Exception:
        return 0


def _ext_for(content_type: str | None, kind: str) -> str:
    ct = (content_type or "").lower()
    if "json" in ct:
        return ".json"
    if "image" in ct:
        return ".bin"
    if kind in {"xhr", "api"}:
        return ".json"
    return ".html"


def record_anomaly(run_id: str | None, platform: str, category: str, *, url: str | None = None,
                   http_status: int | None = None, detail: dict[str, Any] | None = None,
                   archive_id: int | None = None) -> None:
    if detail is not None and not isinstance(detail, dict):
        detail = {"value": str(detail)}
    try:
        with pool.tx() as conn:
            conn.execute(
                """INSERT INTO ingest_anomalies
                   (run_id, platform, url, http_status, category, detail, archive_id)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)""",
                (run_id, platform.upper(), url, http_status, category,
                 json.dumps(detail or {}, ensure_ascii=False, default=str), archive_id),
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# platform health / circuit breaker
# ---------------------------------------------------------------------------
def get_health(platform: str) -> dict[str, Any]:
    row = pool.query_one("SELECT * FROM platform_health WHERE platform = %s", (platform.upper(),))
    if row:
        return row
    pool.execute(
        """INSERT INTO platform_health (platform) VALUES (%s)
           ON CONFLICT (platform) DO NOTHING""", (platform.upper(),))
    return pool.query_one("SELECT * FROM platform_health WHERE platform = %s", (platform.upper(),)) or {}


def register_success(platform: str) -> None:
    pool.execute(
        """UPDATE platform_health
           SET consecutive_failures = 0,
               circuit_state = CASE WHEN circuit_state = 'open' THEN 'half_open' ELSE 'closed' END,
               opened_at = NULL, cooldown_until = NULL,
               last_success_at = now(), updated_at = now(),
               last_error_category = NULL, last_error_detail = NULL
           WHERE platform = %s""",
        (platform.upper(),),
    )


def register_failure(platform: str, category: str, detail: str | None = None) -> dict[str, Any]:
    """Increment the failure counter; open the circuit at the configured threshold."""
    threshold = settings.failure_threshold
    with pool.tx() as conn:
        conn.execute(
            """INSERT INTO platform_health (platform) VALUES (%s)
               ON CONFLICT (platform) DO NOTHING""", (platform.upper(),))
        cur = conn.execute(
            """UPDATE platform_health
               SET consecutive_failures = consecutive_failures + 1,
                   last_error_category = %s,
                   last_error_detail = %s,
                   circuit_state = CASE
                       WHEN consecutive_failures + 1 >= %s THEN 'open'
                       ELSE circuit_state END,
                   opened_at = CASE WHEN consecutive_failures + 1 >= %s
                                    THEN COALESCE(opened_at, now()) ELSE opened_at END,
                   cooldown_until = CASE WHEN consecutive_failures + 1 >= %s
                                    THEN now() + make_interval(secs => %s) ELSE cooldown_until END,
                   updated_at = now()
               WHERE platform = %s
               RETURNING *""",
            (category, (detail or "")[:2000], threshold, threshold, threshold,
             settings.breaker_cooldown_seconds, platform.upper()),
        )
        row = cur.fetchone()
        cur.close()
    return row or {}


def circuit_open(platform: str) -> tuple[bool, dict[str, Any]]:
    """True when this platform must not be crawled right now."""
    h = get_health(platform)
    if not h:
        return False, {}
    if h.get("circuit_state") == "open":
        until = h.get("cooldown_until")
        if until is not None:
            now = datetime.now(timezone.utc)
            if hasattr(until, "tzinfo") and until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if until <= now:
                # cooldown elapsed: allow a single probe (half-open)
                pool.execute(
                    """UPDATE platform_health SET circuit_state = 'half_open', updated_at = now()
                       WHERE platform = %s""", (platform.upper(),))
                return False, h
        return True, h
    return False, h


def pause_platforms(extra_seconds: int) -> None:  # pragma: no cover - helper for demo
    pool.execute(
        """UPDATE platform_health
           SET circuit_state = 'open', cooldown_until = now() + make_interval(secs => %s)
           WHERE circuit_state <> 'open'""", (extra_seconds,))
