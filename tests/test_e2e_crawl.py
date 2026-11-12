"""End-to-end: real HTTP fixture + real Scrapy/Playwright workers + Postgres/Redis.

Run with:  pytest tests/test_e2e_crawl.py -q
Needs Playwright browsers; set PLAYWRIGHT_BROWSERS_PATH if not the default
(see README §本地开发).
"""
import os
import time
from datetime import date

import pytest

from carscout.crawl import runner
from carscout.db import pool

pytestmark = pytest.mark.slow


def _crawl(platform, day="2026-11-12", mode="incremental", wait=300):
    run = runner.start_platform(platform, day=day, mode=mode, auto_merge=False)
    if run.get("skipped"):
        return run
    deadline = time.time() + wait
    while time.time() < deadline:
        row = pool.query_one("SELECT status, stats, error FROM crawl_runs WHERE id=%s",
                            (run["run_id"],)) or {}
        if row.get("status") != "running":
            return {**run, **row}
        time.sleep(0.5)
    return {**run, "status": "timeout"}


@pytest.mark.parametrize("platform,expected", [("A", 9), ("B", 9), ("C", 9), ("D", 8)])
def test_each_platform_crawls_its_own_transport(fixtures, clean_db, platform, expected):
    res = _crawl(platform)
    assert res["status"] == "finished", res
    rows = pool.query("SELECT detail_status FROM listings WHERE platform=%s", (platform,))
    assert len(rows) == expected
    ok = [r for r in rows if r["detail_status"] == "ok"]
    assert len(ok) == expected
    cursor = pool.query_one("SELECT * FROM crawl_cursors WHERE platform=%s", (platform,))
    assert cursor and cursor["completed"] is True

    # list and detail were two separate requests
    arch = pool.query("""SELECT kind, category, count(*) n FROM ingest_archive
                         WHERE platform=%s GROUP BY kind, category""", (platform,))
    kinds = {r["kind"] for r in arch}
    if platform in ("A", "C"):
        assert "html" in kinds
    else:
        assert "xhr" in kinds


def test_idempotent_incremental_second_run_skips_details(fixtures, clean_db):
    first = _crawl("A")
    assert first["status"] == "finished"
    before = pool.query("SELECT count(*) n FROM listings")[0]["n"]
    second = _crawl("A")
    assert second["status"] == "finished"
    stats = second.get("stats") or {}
    assert stats.get("details_skipped_unchanged", 0) >= 9
    assert pool.query("SELECT count(*) n FROM listings")[0]["n"] == before


def test_rerun_mode_forces_detail_refetch(fixtures, clean_db):
    _crawl("A")
    res = _crawl("A", mode="rerun")
    stats = res.get("stats") or {}
    assert stats.get("detail_ok", 0) >= 9
    assert stats.get("details_skipped_unchanged", 0) == 0


def test_platform_c_blocked_uses_backoff_then_circuit_breaker(fixtures, clean_db):
    """200 + empty list on C must NOT be treated as 'no stock'."""
    from carscout.fixture.server import RUNTIME
    RUNTIME.c_force_blocked = True
    old = {k: os.environ.get(k) for k in ("MAX_RETRIES", "FAILURE_THRESHOLD", "RETRY_BASE_DELAY")}
    os.environ["MAX_RETRIES"] = "1"
    os.environ["FAILURE_THRESHOLD"] = "2"
    os.environ["RETRY_BASE_DELAY"] = "0.05"
    import carscout.config as cfg
    object.__setattr__(cfg.settings, "max_retries", 1)
    object.__setattr__(cfg.settings, "failure_threshold", 2)
    object.__setattr__(cfg.settings, "retry_base_delay", 0.05)
    try:
        res = _crawl("C", wait=60)
        assert res["status"] == "aborted"
        health = pool.query_one("SELECT * FROM platform_health WHERE platform='C'")
        assert health["circuit_state"] == "open"
        assert health["consecutive_failures"] >= 2
        assert "empty_unverified" in (health["last_error_category"] or "")
        cats = {r["category"] for r in pool.query(
            "SELECT DISTINCT category FROM ingest_anomalies WHERE platform='C'")}
        assert "empty_unverified" in cats
        cur = pool.query_one("SELECT * FROM crawl_cursors WHERE platform='C'")
        assert cur["completed"] is False
        assert cur["stopped_reason"] == "circuit_open"
        # breaker blocks a fresh run without touching the site
        blocked = runner.start_platform("C", day="2026-11-12")
        assert blocked.get("skipped") and blocked["reason"] == "circuit_open"
        # other platforms are unaffected by C's breaker
        assert _crawl("A")["status"] == "finished"
    finally:
        RUNTIME.c_force_blocked = False
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        object.__setattr__(cfg.settings, "max_retries", 3)
        object.__setattr__(cfg.settings, "failure_threshold", 5)
        object.__setattr__(cfg.settings, "retry_base_delay", 1.0)


def test_platform_c_legit_empty_is_completed_not_blocked(fixtures, clean_db):
    from carscout.fixture.server import RUNTIME
    RUNTIME.c_force_empty = True
    res = _crawl("C", wait=60)
    assert res["status"] == "finished"
    cur = pool.query_one("SELECT * FROM crawl_cursors WHERE platform='C'")
    assert cur["completed"] is True
    health = pool.query_one("SELECT * FROM platform_health WHERE platform='C'")
    assert health["circuit_state"] == "closed"


def test_schema_drift_is_classified_and_does_not_trip_breaker(fixtures, clean_db):
    from carscout.fixture.server import RUNTIME
    RUNTIME.drift["A"] = True
    res = _crawl("A", wait=60)
    assert res["status"] in ("finished", "aborted")
    statuses = {r["detail_status"] for r in pool.query(
        "SELECT detail_status FROM listings WHERE platform='A'")}
    assert "schema_drift" in statuses
    cats = {r["category"] for r in pool.query(
        "SELECT DISTINCT category FROM ingest_anomalies WHERE platform='A'")}
    assert "schema_drift" in cats
    health = pool.query_one("SELECT * FROM platform_health WHERE platform='A'")
    # schema drift is a data problem, not an anti-bot signal: the breaker stays closed
    assert health["circuit_state"] == "closed"


def test_signed_api_expiry_is_retried(fixtures, clean_db):
    from carscout.fixture.server import RUNTIME
    RUNTIME.d_force_expired_once = 1
    res = _crawl("D", wait=90)
    assert res["status"] == "finished", res
    assert (res.get("stats") or {}).get("detail_ok", 0) >= 8


def test_resume_continues_from_last_page_not_from_scratch(fixtures, clean_db):
    """Kill mid-sweep, then resume: the run continues after the committed page."""
    from carscout.fixture.server import RUNTIME
    from carscout.crawl import common
    import carscout.config as cfg

    # sweep only page 1, leaving the cursor at page 1 (worker reads env at import)
    old_env = os.environ.get("MAX_PAGES_PER_RUN")
    os.environ["MAX_PAGES_PER_RUN"] = "1"
    # isolate this test's breaker state: a previous test may have left C tripped
    from carscout.ingest import register_success
    register_success("A")
    object.__setattr__(cfg.settings, "max_pages_per_run", 1)
    try:
        first = _crawl("A")
    finally:
        object.__setattr__(cfg.settings, "max_pages_per_run", 200)
        if old_env is None:
            os.environ.pop("MAX_PAGES_PER_RUN", None)
        else:
            os.environ["MAX_PAGES_PER_RUN"] = old_env
    assert first["status"] == "finished"
    cur = pool.query_one("SELECT * FROM crawl_cursors WHERE platform='A'")
    assert cur["cursor"]["last_completed_page"] == 1
    assert cur["completed"] is False
    run = common.create_run("A", date(2026, 11, 12), mode="resume")
    # a resuming run must be its own process (Twisted reactor cannot restart)
    from carscout.crawl.runner import start_platform
    started = start_platform("A", day="2026-11-12", mode="resume", resume=True,
                             parent_run_id=None, auto_merge=False)
    # start_platform creates its own run row; re-point the pre-made row at it is not
    # needed — verify the *created* run instead
    deadline = time.time() + 60
    row = {}
    while time.time() < deadline:
        row = pool.query_one("SELECT status, stats FROM crawl_runs WHERE id=%s",
                             (started["run_id"],)) or {}
        if row.get("status") != "running":
            break
        time.sleep(0.4)
    assert row["status"] == "finished", row
    assert (row["stats"] or {}).get("start_page") == 2
    cur = pool.query_one("SELECT * FROM crawl_cursors WHERE platform='A'")
    assert cur["completed"] is True


def test_full_run_merges_cross_platform_duplicates(fixtures, clean_db):
    from carscout.fixture.server import RUNTIME
    RUNTIME.c_max_per_window = 1000
    for p in ("A", "B", "C", "D"):
        assert _crawl(p, wait=180)["status"] == "finished"
    from carscout.matching import merge_pending_listings
    summary = merge_pending_listings(limit=1000)
    assert summary.get("created_vehicle", 0) == 12

    # 12 real cars + the grey-zone listings parked as provisional single-listing vehicles
    confirmed = pool.query(
        """SELECT v.id FROM vehicles v
           WHERE NOT EXISTS (SELECT 1 FROM listings l WHERE l.vehicle_id = v.id
                             AND l.match_status IN ('manual_pending', 'rejected'))""")
    assert len(confirmed) == 12
    # the four-platform BMW 3系 2018 and the four-platform BYD 汉
    four = pool.query(
        """SELECT v.id FROM vehicles v
           WHERE (SELECT count(DISTINCT l.platform) FROM listings l WHERE l.vehicle_id = v.id) = 4""")
    assert len(four) == 2
    members = pool.query(
        """SELECT l.platform FROM listings l WHERE l.vehicle_id = (
             SELECT v.id FROM vehicles v WHERE v.brand='宝马' AND v.series='3系'
             AND v.reg_year=2018) ORDER BY l.platform""")
    assert [m["platform"] for m in members] == ["A", "B", "C", "D"]
    # grey-zone pairs must NOT have been auto-merged
    grey = pool.query("""SELECT count(*) n FROM match_candidates WHERE state='manual_pending'""")
    assert grey[0]["n"] >= 2
    assert pool.query("SELECT count(*) n FROM manual_review_queue WHERE status='pending'")[0]["n"] >= 2


def test_price_history_records_cuts(fixtures, clean_db):
    _crawl("A")
    listing = pool.query_one(
        "SELECT id, price_yuan FROM listings WHERE platform='A' AND platform_listing_id='pa-1001'")
    assert listing["price_yuan"] == 188000
    # simulate the seller dropping the price, then re-crawl
    from carscout.fixture.server import RUNTIME
    old_day = RUNTIME.snapshot_day
    RUNTIME.snapshot_day = date(2026, 11, 12)
    pool.execute("UPDATE listings SET price_yuan = 192000 WHERE id = %s", (listing["id"],))
    pool.execute("""INSERT INTO listing_price_events (listing_id, price_yuan, previous_price,
                    delta_yuan, observed_at) VALUES (%s, 192000, NULL, NULL, now() - interval '5 days')""",
                 (listing["id"],))
    _crawl("A", mode="rerun")
    events = pool.query("SELECT price_yuan, previous_price, delta_yuan FROM listing_price_events "
                        "WHERE listing_id=%s ORDER BY observed_at", (listing["id"],))
    new = pool.query_one("SELECT price_yuan FROM listings WHERE id=%s", (listing["id"],))
    assert new["price_yuan"] == 188000
    cuts = [e for e in events if e["delta_yuan"] is not None and e["delta_yuan"] < 0]
    assert cuts, events
    RUNTIME.snapshot_day = old_day
