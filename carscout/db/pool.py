"""PostgreSQL connection pool + Redis client, shared by CLI, API and spiders."""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
import redis

from carscout.config import settings

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None
_redis: redis.Redis | None = None
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=12,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


def init_schema() -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_pool().connection() as conn:
        conn.execute(sql)
        conn.commit()
    seed_platforms()
    # versioned merge rules: seed the v1 baseline (idempotent)
    from carscout.rules import ensure_baseline_version

    ensure_baseline_version()


def seed_platforms() -> None:
    """Per-platform crawl scope, frequency, pagination mode and rate limits.

    These live in the database (not only in code) so an operator can tune one
    platform's cadence without redeploying. The scheduler reads interval_minutes
    from here; the spider reads concurrency/delay from the same row.
    """
    rows = [
        ("A", "平台A · 静态列表", "/pa/", {
            "kind": "static_html", "pagination": "page_param", "page_size": 6,
            "scope": "全部在售车源（按页翻到最后）", "interval_minutes": 30,
            "rate": {"concurrent": 4, "delay": 0.25},
            "detail_strategy": "separate_html_get",
        }),
        ("B", "平台B · 滚动加载", "/pb/", {
            "kind": "playwright_scroll", "pagination": "scroll_xhr_has_more", "page_size": 8,
            "scope": "全部在售车源（滚动到 hasMore=false）", "interval_minutes": 45,
            "rate": {"concurrent": 2, "delay": 0.5},
            "detail_strategy": "xhr_in_browser_context",
        }),
        ("C", "平台C · 风控平台", "/pc/", {
            "kind": "anti_bot_html", "pagination": "page_param", "page_size": 10,
            "scope": "全部在售车源（低并发、长间隔）", "interval_minutes": 60,
            "rate": {"concurrent": 1, "delay": 1.2},
            "detail_strategy": "separate_html_get",
        }),
        ("D", "平台D · App签名接口", "/pd/", {
            "kind": "signed_api", "pagination": "has_more_cursor", "page_size": 10,
            "scope": "全部在售车源（签名会话内翻页）", "interval_minutes": 15,
            "rate": {"concurrent": 3, "delay": 0.2},
            "detail_strategy": "signed_xhr_in_page",
        }),
    ]
    with get_pool().connection() as conn:
        for code, name, path, config in rows:
            base = path        # path only: the host comes from FIXTURE_BASE_URL at request time
            conn.execute(
                """INSERT INTO platforms (code, name, base_url, config)
                   VALUES (%s, %s, %s, %s::jsonb)
                   ON CONFLICT (code) DO UPDATE
                   SET name = EXCLUDED.name, base_url = EXCLUDED.base_url,
                       config = platforms.config || EXCLUDED.config""",
                (code, name, base, json.dumps(config, ensure_ascii=False)),
            )
        conn.commit()


@contextmanager
def tx() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        cur = conn.execute(sql, params)
        try:
            return cur.fetchall()
        finally:
            cur.close()


def query_one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | list = ()) -> int:
    with get_pool().connection() as conn:
        cur = conn.execute(sql, params)
        n = cur.rowcount
        cur.close()
        conn.commit()
        return n


def healthcheck() -> dict[str, bool]:
    ok = {"postgres": False, "redis": False}
    try:
        query_one("SELECT 1 AS ok")
        ok["postgres"] = True
    except Exception as exc:  # pragma: no cover
        log.warning("postgres healthcheck failed: %s", exc)
    try:
        ok["redis"] = bool(get_redis().ping())
    except Exception as exc:  # pragma: no cover
        log.warning("redis healthcheck failed: %s", exc)
    return ok
