"""Scrapy settings for CarScout platform spiders."""
from __future__ import annotations

import os

from carscout.config import settings

BOT_NAME = "carscout"
SPIDER_MODULES = ["carscout.crawl"]
NEWSPIDER_MODULE = "carscout.crawl"

ROBOTSTXT_OBEY = False           # internal/commercial partner feeds in production; fixture in dev
CONCURRENT_REQUESTS = 8
CONCURRENT_REQUESTS_PER_DOMAIN = 4
DOWNLOAD_DELAY = 0.2
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 0.3
AUTOTHROTTLE_MAX_DELAY = 20.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 2.0
DOWNLOAD_TIMEOUT = 30
RETRY_ENABLED = False             # retries are orchestrated by the engine with backoff + classification
COOKIES_ENABLED = True

TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
DOWNLOAD_HANDLERS = {
    "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
}
PLAYWRIGHT_BROWSERS_PATH = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "")
PLAYWRIGHT_LAUNCH_OPTIONS = {
    "headless": settings.playwright_headless,
    "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    "timeout": settings.playwright_timeout_ms,
}
PLAYWRIGHT_MAX_CONTEXTS = 8
PLAYWRIGHT_MAX_PAGES_PER_CONTEXT = 4

ITEM_PIPELINES: dict[str, int] = {}
LOG_LEVEL = os.getenv("SCRAPY_LOG_LEVEL", "INFO")
TELNETCONSOLE_ENABLED = False
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
