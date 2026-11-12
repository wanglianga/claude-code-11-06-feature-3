"""Resident scheduler: one isolated crawl per platform at its own frequency.

State lives in PostgreSQL (`crawl_cursors.run_date`, `platform_health`), so the
loop is stateless: restarting the container resumes exactly where it stopped
and never re-scans a completed sweep.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import date, datetime, timedelta, timezone

from carscout.config import settings
from carscout.crawl import common
from carscout.crawl.runner import PLATFORMS, gc_stale_runs, run_platforms
from carscout.db import pool
from carscout.ingest import circuit_open

log = logging.getLogger("carscout.scheduler")

_stop = False


def _handle_signal(signum, frame):  # pragma: no cover
    global _stop
    log.info("scheduler received signal %s; finishing current tick", signum)
    _stop = True


def platform_interval_minutes(platform: str) -> int:
    """Cadence comes from the platform's DB row, falling back to config defaults."""
    row = pool.query_one("SELECT config FROM platforms WHERE code = %s", (platform.upper(),))
    cfg = (row or {}).get("config") or {}
    if isinstance(cfg, dict) and cfg.get("interval_minutes"):
        try:
            return int(cfg["interval_minutes"])
        except (TypeError, ValueError):
            pass
    limits = settings.platform_limits.get(platform, {})
    return int(limits.get("interval_minutes", 60))


def platform_due(platform: str, now: datetime | None = None) -> bool:
    """A platform is due when its last finished sweep is older than its interval."""
    now = now or datetime.now(timezone.utc)
    interval = platform_interval_minutes(platform)
    row = pool.query_one(
        """SELECT max(finished_at) AS last_fin FROM crawl_runs
           WHERE platform = %s AND status = 'finished'""", (platform,))
    last = (row or {}).get("last_fin")
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= timedelta(minutes=interval)


def due_platforms(now: datetime | None = None) -> tuple[list[str], dict[str, str]]:
    """Split platforms into (due, skipped-because-breaker-open)."""
    open_now: dict[str, str] = {}
    due: list[str] = []
    for p in PLATFORMS:
        is_open, health = circuit_open(p)
        if is_open:
            open_now[p] = str(health.get("cooldown_until"))
            continue
        if platform_due(p, now):
            due.append(p)
    return due, open_now


def tick(now: datetime | None = None) -> dict:
    """One scheduler iteration: crawl the platforms that are due, in parallel."""
    due, open_now = due_platforms(now)
    if not due:
        return {"due": [], "skipped": open_now}
    day = (now or datetime.now(timezone.utc)).date().isoformat()
    res = run_platforms(due, day=day, mode="incremental", auto_merge=True)
    return {"due": due, "skipped": open_now, "summary": res}


def main_loop() -> None:
    from carscout.db.pool import init_schema
    from carscout.matching import merge_pending_listings

    init_schema()
    gc_stale_runs()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    log.info("scheduler started: platforms=%s tick=%ss parallel=%s",
             list(PLATFORMS), settings.scheduler_tick_seconds, settings.max_parallel_platforms)
    while not _stop:
        try:
            res = tick()
            if res.get("due"):
                log.info("crawl tick: %s", {k: v for k, v in res.items() if k != "summary"})
            merge_pending_listings()
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed; continuing")
        for _ in range(max(1, settings.scheduler_tick_seconds)):
            if _stop:
                break
            time.sleep(1)
    log.info("scheduler stopped")
