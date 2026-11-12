"""Central configuration (environment driven, with dev defaults)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "CARSCOUT_DATABASE_URL",
        os.getenv("DATABASE_URL", "postgresql://carscout:carscout@127.0.0.1:5432/carscout"),
    )
    redis_url: str = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    data_dir: str = os.getenv("DATA_DIR", "./data")
    fixture_base_url: str = os.getenv("FIXTURE_BASE_URL", "http://127.0.0.1:8088")
    fixture_secret: str = os.getenv("FIXTURE_SECRET", "carscout-demo-secret-v1")


    # request-dedup fingerprint retention (incremental crawl horizon)
    fingerprint_ttl_seconds: int = _int("FINGERPRINT_TTL_SECONDS", 45 * 24 * 3600)

    # health / circuit breaking
    failure_threshold: int = _int("FAILURE_THRESHOLD", 5)
    breaker_cooldown_seconds: int = _int("BREAKER_COOLDOWN_SECONDS", 180)
    max_retries: int = _int("MAX_RETRIES", 3)
    retry_base_delay: float = _float("RETRY_BASE_DELAY", 1.0)
    retry_max_delay: float = _float("RETRY_MAX_DELAY", 30.0)

    # arming / scheduling
    scheduler_tick_seconds: int = _int("SCHEDULER_TICK_SECONDS", 20)
    max_parallel_platforms: int = _int("MAX_PARALLEL_PLATFORMS", 4)
    crawler_spawn_mode: str = os.getenv("CRAWLER_SPAWN_MODE", "subprocess")
    # a hung platform (e.g. a browser that never launches) must not block the others:
    # no output for this long => treat the worker as dead and kill it
    platform_stall_timeout_seconds: int = _int("PLATFORM_STALL_TIMEOUT_SECONDS", 300)

    # matching thresholds (tuned against fixture corpus; documented in README)
    auto_merge_score: Decimal = Decimal(os.getenv("AUTO_MERGE_SCORE", "0.82"))
    gray_zone_score: Decimal = Decimal(os.getenv("GRAY_ZONE_SCORE", "0.60"))
    recall_reg_months: int = _int("RECALL_REG_MONTHS", 9)
    recall_mileage_km: int = _int("RECALL_MILEAGE_KM", 9000)
    candidate_limit: int = _int("CANDIDATE_LIMIT", 40)

    # rule-version replay: exceeding either bound forces manual approval before activation
    replay_max_extra_merges: int = _int("REPLAY_MAX_EXTRA_MERGES", 10)
    replay_max_split_confirmed: int = _int("REPLAY_MAX_SPLIT_CONFIRMED", 0)

    fingerprint_skip_unchanged: bool = _bool("FINGERPRINT_SKIP_UNCHANGED", True)
    max_pages_per_run: int = _int("MAX_PAGES_PER_RUN", 200)

    api_host: str = os.getenv("API_HOST", "0.0.0.0")
    api_port: int = _int("API_PORT", 8000)

    playwright_headless: bool = _bool("PLAYWRIGHT_HEADLESS", True)
    playwright_timeout_ms: int = _int("PLAYWRIGHT_TIMEOUT_MS", 45000)
    playwright_extra_library_path: str = os.getenv(
        "PLAYWRIGHT_EXTRA_LIBRARY_PATH", ""
    )
    archive_dir_name: str = os.getenv("ARCHIVE_DIR_NAME", "archive")

    # per-platform crawl knobs
    platform_limits: dict = field(
        default_factory=lambda: {
            "A": {"concurrent": 4, "delay": 0.25, "interval_minutes": 30},
            "B": {"concurrent": 2, "delay": 0.5, "interval_minutes": 45},
            "C": {"concurrent": 1, "delay": 1.2, "interval_minutes": 60},
            "D": {"concurrent": 3, "delay": 0.2, "interval_minutes": 15},
        }
    )


settings = Settings()
