"""Scheduler due-logic and CLI smoke tests."""
import pytest
from datetime import datetime, timedelta, timezone

from carscout.db.pool import execute, query


@pytest.mark.usefixtures("clean_db")
def test_platform_due_uses_interval_and_last_finished_run():
    from carscout.scheduler import platform_due

    now = datetime.now(timezone.utc)
    assert platform_due("A", now) is True          # no completed sweep yet

    execute("""INSERT INTO crawl_runs (platform, run_date, mode, status, started_at, finished_at)
               VALUES ('A', current_date, 'incremental', 'finished', %s, %s)""", (now, now))
    assert platform_due("A", now) is False         # just finished

    execute("DELETE FROM crawl_runs")
    old = now - timedelta(minutes=35)              # A runs every 30 minutes
    execute("""INSERT INTO crawl_runs (platform, run_date, mode, status, started_at, finished_at)
               VALUES ('A', current_date, 'incremental', 'finished', %s, %s)""", (old, old))
    assert platform_due("A", old + timedelta(minutes=31)) is True
    # unfinished runs are never counted as a completed sweep
    execute("""INSERT INTO crawl_runs (platform, run_date, mode, status, started_at)
               VALUES ('A', current_date, 'incremental', 'aborted', %s)""", (now,))
    assert platform_due("A", now) is True


@pytest.mark.usefixtures("clean_db")
def test_due_platforms_excludes_open_breaker():
    from carscout.scheduler import due_platforms

    execute("""INSERT INTO platform_health (platform, consecutive_failures, circuit_state,
                    cooldown_until) VALUES ('C', 5, 'open', now() + interval '5 minutes')""")
    due, skipped = due_platforms()
    assert "C" not in due
    assert "C" in skipped
    assert "A" in due


def test_cli_help_and_status_commands(fixtures, clean_db):
    from click.testing import CliRunner
    from carscout.cli import cli

    runner = CliRunner()
    res = runner.invoke(cli, ["init-db"])
    assert res.exit_code == 0, res.output
    res = runner.invoke(cli, ["status"])
    assert res.exit_code == 0
    assert "platforms" in res.output


def test_platform_rate_reads_db_row(fixtures, clean_db):
    from carscout.db.pool import execute
    from carscout.crawl.handlers import platform_rate

    base = platform_rate("C")
    execute("""UPDATE platforms SET config = config || '{"rate": {"concurrent": 7, "delay": 0.9}}'::jsonb
               WHERE code = 'C'""")
    assert platform_rate("C") == (7, 0.9)
    execute("""UPDATE platforms SET config = config || %s::jsonb WHERE code = 'C'""",
            ('{"rate": {"concurrent": 1, "delay": 1.2}}',))
    assert platform_rate("C") == (1, 1.2)
