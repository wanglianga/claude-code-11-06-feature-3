"""Crash recovery: a hard-killed crawl resumes at the committed page, not page 1."""
import os
import subprocess
import sys
import time

import pytest

from carscout.db import pool

pytestmark = pytest.mark.slow


def test_sigkill_resumes_from_committed_page(fixtures, clean_db):
    from carscout.fixture.server import RUNTIME
    RUNTIME.response_delay_ms = 250          # slow enough to interrupt deterministically
    RUNTIME.c_max_per_window = 1000
    env = {**os.environ, "CARSCOUT_DATABASE_URL": os.environ["CARSCOUT_DATABASE_URL"]}
    proc = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), "_crash_worker.py"),
                             "2026-11-12"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    run_id = None
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if "RUN_ID" in (line or ""):
            run_id = line.split()[-1]
            break
    assert run_id, "worker did not start"
    # wait until page 1 is fully committed (all its details drained), then hard-kill
    # the worker while it is working on page 2
    deadline = time.time() + 90
    committed = 0
    while time.time() < deadline:
        cur = pool.query_one("SELECT cursor FROM crawl_cursors WHERE platform='A'")
        if cur:
            committed = int((cur["cursor"] or {}).get("last_completed_page") or 0)
        if committed >= 1:
            break
        time.sleep(0.4)
    proc.kill()
    proc.wait(timeout=10)
    RUNTIME.response_delay_ms = 0

    cur = pool.query_one("SELECT * FROM crawl_cursors WHERE platform='A'")
    assert cur is not None, "cursor row must exist even after a kill"
    committed = int((cur["cursor"] or {}).get("last_completed_page") or 0)
    assert committed >= 1, cur
    assert cur["completed"] is False
    # the abandoned run is not left "running" forever
    row = pool.query_one("SELECT status FROM crawl_runs WHERE id=%s", (run_id,))
    run_status = row["status"] if row else None

    # resume: must continue after the committed page, not re-scan from page 1
    from carscout.crawl.runner import start_platform
    started = start_platform("A", day="2026-11-12", mode="resume", resume=True, auto_merge=False)
    deadline = time.time() + 120
    row = {}
    while time.time() < deadline:
        row = pool.query_one("SELECT status, stats FROM crawl_runs WHERE id=%s",
                             (started["run_id"],)) or {}
        if row.get("status") != "running":
            break
        time.sleep(0.5)
    assert row.get("status") == "finished", row
    assert run_status in ("running", "failed", "aborted")   # never silently "finished"
    assert (row.get("stats") or {}).get("start_page") == committed + 1
    cur = pool.query_one("SELECT * FROM crawl_cursors WHERE platform='A'")
    assert cur["completed"] is True
    # no duplicate listings were created by the resume
    dupes = pool.query("""SELECT platform_listing_id, count(*) n FROM listings
                          WHERE platform='A' GROUP BY 1 HAVING count(*) > 1""")
    assert not dupes
