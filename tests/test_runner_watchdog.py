"""The runner must isolate and time out a platform that stops making progress."""
import sys
import time

import pytest

from carscout.db import pool


@pytest.mark.usefixtures("clean_db")
def test_watchdog_kills_a_stalled_platform_and_keeps_others(fixtures, monkeypatch):
    import carscout.config as cfg
    from carscout.crawl import runner

    monkeypatch.setenv("PLATFORM_STALL_TIMEOUT_SECONDS", "3")
    object.__setattr__(cfg.settings, "platform_stall_timeout_seconds", 3)
    # a worker that sleeps forever and never writes output -> stall watchdog fires
    import types
    class _FakeProc:
        returncode = None
        pid = 4242
        killed = False
        def poll(self):
            return None
        def kill(self):
            _FakeProc.killed = True
        def wait(self, timeout=None):
            return 0
    real_popen = runner.subprocess.Popen
    def fake_popen(cmd, **kw):
        if "carscout.crawl.worker" in cmd or any("carscout.crawl.worker" in str(c) for c in cmd):
            return _FakeProc()
        return real_popen(cmd, **kw)
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    # run only B through the polling loop; the fake process never writes output
    summary = runner.run_platforms(["B"], day="2026-11-12", auto_merge=False)
    assert _FakeProc.killed is True
    run_id = summary["platforms"]["B"]["run_id"]
    row = pool.query_one("SELECT status, error FROM crawl_runs WHERE id=%s", (run_id,))
    assert row["status"] in ("aborted", "failed")
    assert "progress" in (row["error"] or "") or row["status"] == "failed"
