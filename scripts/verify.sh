#!/usr/bin/env bash
# Full local verification: needs PostgreSQL + Redis reachable and Playwright Chromium.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${CARSCOUT_DATABASE_URL:=postgresql://carscout:carscout@127.0.0.1:5432/carscout}"
: "${REDIS_URL:=redis://127.0.0.1:6379/0}"
: "${PLAYWRIGHT_BROWSERS_PATH:=$HOME/.cache/ms-playwright}"
export CARSCOUT_DATABASE_URL REDIS_URL PLAYWRIGHT_BROWSERS_PATH

echo "== fast suites (parsers, store, matching, api, scheduler, watchdog)"
pytest tests -q --ignore=tests/test_e2e_crawl.py --ignore=tests/test_e2e_review.py --ignore=tests/test_e2e_crash.py

echo "== end-to-end (real Scrapy + Playwright + PG + Redis)"
pytest tests/test_e2e_crawl.py tests/test_e2e_review.py tests/test_e2e_crash.py -q

echo "ALL GREEN"
