"""Runner: spawn one crawler process per platform, independently, with isolation."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
import traceback
from datetime import date
from pathlib import Path
from typing import Any

from carscout.config import settings
from carscout.crawl import common
from carscout.db import pool
from carscout.ingest import circuit_open

log = logging.getLogger("carscout.runner")

PLATFORMS = ("A", "B", "C", "D")
_proc_registry: dict[str, Any] = {}


def _crawler_settings() -> dict[str, Any]:
    from carscout.crawl.settings import (AUTOTHROTTLE_ENABLED, AUTOTHROTTLE_MAX_DELAY,
                                         AUTOTHROTTLE_START_DELAY, AUTOTHROTTLE_TARGET_CONCURRENCY,
                                         CONCURRENT_REQUESTS_PER_DOMAIN, COOKIES_ENABLED,
                                         DOWNLOAD_DELAY, DOWNLOAD_HANDLERS, DOWNLOAD_TIMEOUT,
                                         PLAYWRIGHT_BROWSERS_PATH, PLAYWRIGHT_LAUNCH_OPTIONS,
                                         PLAYWRIGHT_MAX_CONTEXTS, PLAYWRIGHT_MAX_PAGES_PER_CONTEXT,
                                         RETRY_ENABLED, TWISTED_REACTOR)
    return {
        "TWISTED_REACTOR": TWISTED_REACTOR,
        "DOWNLOAD_HANDLERS": DOWNLOAD_HANDLERS,
        "PLAYWRIGHT_BROWSERS_PATH": PLAYWRIGHT_BROWSERS_PATH,
        "PLAYWRIGHT_LAUNCH_OPTIONS": PLAYWRIGHT_LAUNCH_OPTIONS,
        "PLAYWRIGHT_MAX_CONTEXTS": PLAYWRIGHT_MAX_CONTEXTS,
        "PLAYWRIGHT_MAX_PAGES_PER_CONTEXT": PLAYWRIGHT_MAX_PAGES_PER_CONTEXT,
        "CONCURRENT_REQUESTS": 8,
        "CONCURRENT_REQUESTS_PER_DOMAIN": CONCURRENT_REQUESTS_PER_DOMAIN,
        "DOWNLOAD_DELAY": DOWNLOAD_DELAY,
        "DOWNLOAD_TIMEOUT": DOWNLOAD_TIMEOUT,
        "RETRY_ENABLED": RETRY_ENABLED,
        "AUTOTHROTTLE_ENABLED": AUTOTHROTTLE_ENABLED,
        "AUTOTHROTTLE_START_DELAY": AUTOTHROTTLE_START_DELAY,
        "AUTOTHROTTLE_MAX_DELAY": AUTOTHROTTLE_MAX_DELAY,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": AUTOTHROTTLE_TARGET_CONCURRENCY,
        "COOKIES_ENABLED": COOKIES_ENABLED,
        "LOG_LEVEL": "WARNING",
        "TELNETCONSOLE_ENABLED": False,
        "REQUEST_FINGERPRINTER_IMPLEMENTATION": "2.7",
    }


def crawl_platform_sync(platform: str, run_id: str, day: str, mode: str = "incremental",
                        resume: bool = False, start_page: int = 1, scope: str = "all") -> str:
    """Run a single platform spider in-process (used by tests and the subprocess runner)."""
    from scrapy.crawler import CrawlerProcess
    from carscout.crawl.engine import PlatformSpider

    process = CrawlerProcess(_crawler_settings(), install_root_handler=False)
    process.crawl(PlatformSpider, platform=platform, run_id=run_id, day=day, mode=mode,
                  resume=resume, start_page=start_page, scope=scope)
    try:
        process.start()
    except Exception:
        traceback.print_exc()
        common.finish_run(run_id, "failed", error=traceback.format_exc()[-500:])
        return "failed"
    run = pool.query_one("SELECT status FROM crawl_runs WHERE id = %s", (run_id,)) or {}
    return run.get("status", "failed")


def start_platform(platform: str, *, day: str, mode: str = "incremental", resume: bool = False,
                   parent_run_id: str | None = None, auto_merge: bool = True,
                   scope: str = "all", start_page: int = 1) -> dict[str, Any]:
    """Create a run row and spawn one platform worker process (isolated)."""
    """Create a run row and spawn the platform worker as a separate OS process."""
    open_now, health = circuit_open(platform)
    if open_now:
        return {"platform": platform, "skipped": True, "reason": "circuit_open",
                "cooldown_until": str((health or {}).get("cooldown_until"))}
    run = common.create_run(platform, date.fromisoformat(day), mode=mode, parent_run_id=parent_run_id,
                            scope=scope)
    run_id = str(run["id"])
    if settings.crawler_spawn_mode == "inline":
        status = crawl_platform_sync(platform, run_id, day, mode=mode, resume=resume,
                                     start_page=start_page, scope=scope)
        if auto_merge:
            from carscout.matching import merge_pending_listings
            merge_pending_listings(run_id=run_id, platforms=[platform])
        return {"platform": platform, "run_id": run_id, "status": status}

    import os
    cmd = [sys.executable, "-m", "carscout.crawl.worker",
           "--platform", platform, "--run-id", run_id, "--day", day, "--mode", mode,
           "--scope", scope, "--start-page", str(start_page)]
    if resume:
        cmd.append("--resume")
    # merging is done once, by run_platforms, after every platform finishes.
    # Letting each worker merge concurrently would double-process candidates.
    cmd.append("--no-merge")
    log_dir = Path(settings.data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out = open(log_dir / f"worker-{platform}-{run_id[:6]}.out", "ab")
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    extra_libs = settings.playwright_extra_library_path
    if extra_libs:
        env["LD_LIBRARY_PATH"] = extra_libs + ":" + env.get("LD_LIBRARY_PATH", "")
    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=str(Path(__file__).resolve().parents[2]),
                            env=env, start_new_session=True)
    _proc_registry[f"{platform}-{run_id[:6]}"] = proc
    return {"platform": platform, "run_id": run_id, "pid": proc.pid}


def run_platforms(platforms: list[str] | None = None, *, day: str | None = None,
                  mode: str = "incremental", resume: bool = False,
                  wait: bool = True, auto_merge: bool = True, max_parallel: int | None = None) -> dict[str, Any]:
    """Concurrent, isolated crawl of several platforms, then a merge pass."""
    platforms = platforms or list(PLATFORMS)
    day = day or date.today().isoformat()
    cap = max_parallel or settings.max_parallel_platforms
    queue = list(platforms)
    running: list[dict[str, Any]] = []
    started: list[dict[str, Any]] = []
    # watchdog: (start_time, last_progress_marker) per platform
    watch: dict[str, tuple[float, int]] = {}
    stall_at = settings.platform_stall_timeout_seconds
    while queue or running:
        while queue and len(running) < cap:
            p = queue.pop(0)
            s = start_platform(p, day=day, mode=mode, resume=resume, auto_merge=auto_merge)
            started.append(s)
            if s.get("skipped"):
                continue
            running.append(s)
            if s.get("run_id"):
                watch[s["run_id"]] = (time.time(), 0)
        for s in list(running):
            run_id = s["run_id"]
            proc = _proc_registry.get(f"{s['platform']}-{run_id[:6]}")
            # progress = text already written by the worker (its log keeps growing
            # as pages finish); a browser launch alone writes enough to reset it)
            out_path = Path(settings.data_dir) / "logs" / f"worker-{s['platform']}-{run_id[:6]}.out"
            try:
                marker = out_path.stat().st_size if out_path.exists() else 0
            except OSError:
                marker = 0
            first_seen, last_marker = watch.get(run_id, (time.time(), 0))
            if marker > last_marker:
                # real progress: reset the stall clock
                first_seen = time.time()
                watch[run_id] = (first_seen, marker)
            else:
                watch[run_id] = (first_seen, last_marker)
            stalled = (time.time() - first_seen) > stall_at
            if proc is not None and proc.poll() is None and stalled:
                # hard timeout: kill the platform and keep everything else going
                try:
                    proc.kill()
                    proc.wait(timeout=10)
                except Exception:
                    pass
                common.finish_run(run_id, "aborted",
                                  error=f"no worker progress for {stall_at}s; killed")
                s["status"] = "aborted"
                running.remove(s)
                log.warning("platform %s produced no progress for %ss — worker killed",
                            s["platform"], stall_at)
                continue
            if proc is not None and proc.poll() is not None:
                rc = proc.returncode
                s["exit_code"] = rc
                if rc != 0:
                    # the worker died (kill / segfault / unhandled exception): make
                    # the run row reflect that instead of leaving it "running"
                    row = pool.query_one("SELECT status FROM crawl_runs WHERE id=%s",
                                         (s["run_id"],)) or {}
                    if row.get("status") == "running":
                        common.finish_run(s["run_id"], "failed",
                                          error=f"worker exited with code {rc}")
                running.remove(s)
        if not wait:
            break
        if running:
            time.sleep(0.4)
    if not wait:
        return {"day": day, "started": started}
    statuses = []
    for s in started:
        if s.get("skipped"):
            statuses.append(s)
            continue
        row = pool.query_one("SELECT status, error, stats FROM crawl_runs WHERE id = %s", (s["run_id"],))
        statuses.append({**s, **(row or {})})
    summary = _summarize(day, platforms, statuses)
    if auto_merge:
        from carscout.matching import merge_pending_listings
        try:
            summary["merge"] = merge_pending_listings(run_id=None, platforms=platforms)
        except Exception:
            traceback.print_exc()
            summary["merge"] = {"error": "merge failed"}
    return summary


def _summarize(day: str, platforms: list[str], runs: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"day": day, "platforms": {}}
    for p in platforms:
        runs_p = [r for r in runs if r.get("platform") == p]
        last = runs_p[-1] if runs_p else {}
        stats = last.get("stats") or {}
        health = pool.query_one("SELECT * FROM platform_health WHERE platform = %s", (p,)) or {}
        out["platforms"][p] = {
            "run_id": last.get("run_id"),
            "status": last.get("status"),
            "items_seen": stats.get("items_seen"),
            "list_items": stats.get("list_items"),
            "detail_ok": stats.get("detail_ok"),
            "detail_skipped_unchanged": stats.get("details_skipped_unchanged"),
            "circuit_state": health.get("circuit_state"),
            "consecutive_failures": health.get("consecutive_failures"),
            "last_error_category": health.get("last_error_category"),
        }
    return out


def gc_stale_runs() -> int:
    """Mark runs left in `running` by a hard kill as aborted."""
    return pool.execute(
        """UPDATE crawl_runs SET status = 'aborted', finished_at = now(),
                  error = COALESCE(error, 'stale: process died')
           WHERE status = 'running' AND started_at < now() - interval '2 hours'""")
