"""Test fixtures: private schema per session and a live fixture HTTP server."""
import os
import socket
import threading
import time

import pytest

os.environ.setdefault("CARSCOUT_DATABASE_URL",
                      os.getenv("TEST_DATABASE_URL",
                                os.getenv("CARSCOUT_DATABASE_URL",
                                          "postgresql://carscout:carscout@127.0.0.1:5432/carscout")))
os.environ.setdefault("REDIS_URL", os.getenv("TEST_REDIS_URL", "redis://127.0.0.1:6379/0"))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.getenv("PLAYWRIGHT_BROWSERS_PATH",
                                                             "/home/node/.cache/ms-playwright"))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def fixture_server():
    from carscout.fixture.server import RUNTIME, make_server
    port = _free_port()
    srv = make_server(port, "127.0.0.1")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    RUNTIME.reset()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def fixtures(fixture_server):
    """Reset fixture runtime and point the service at it."""
    from carscout.fixture.server import RUNTIME
    import carscout.config as cfg

    RUNTIME.reset()
    object.__setattr__(cfg.settings, "fixture_base_url", fixture_server)
    # workers are separate OS processes and read the env var at import time
    os.environ["FIXTURE_BASE_URL"] = fixture_server
    return fixture_server


@pytest.fixture()
def clean_db():
    from carscout.db.pool import init_schema, execute, get_redis

    init_schema()
    execute("""TRUNCATE listings, vehicles, crawl_runs, crawl_cursors, platform_health,
                      match_candidates, manual_review_queue, listing_price_events,
                      image_hashes, ingest_archive, ingest_anomalies, match_rules,
                      merge_rule_versions, merge_rule_rollouts, merge_rule_replays
               RESTART IDENTITY CASCADE""")
    get_redis().flushdb()
    from carscout.rules import ensure_baseline_version

    ensure_baseline_version()
    yield
