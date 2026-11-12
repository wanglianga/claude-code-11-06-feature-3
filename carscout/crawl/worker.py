"""Worker entry point: crawl exactly one platform in its own OS process.

Usage (spawned by `carscout.crawl.runner`, never by users directly):
    python -m carscout.crawl.worker --platform A --run-id UUID --day 2026-11-12

Process isolation is deliberate: a platform that hangs, crashes, exhausts
memory or trips its breaker can never affect the crawl progress of the others.
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--day", required=True)
    ap.add_argument("--mode", default="incremental")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--scope", default="all")
    ap.add_argument("--start-page", type=int, default=1)
    ap.add_argument("--no-merge", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    from carscout.config import settings

    log_dir = Path(settings.data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(log_dir / f"crawl-{args.platform}-{args.run_id[:6]}.log", encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )
    from carscout.crawl.runner import crawl_platform_sync
    from carscout.crawl import common
    from carscout.db import pool

    try:
        status = crawl_platform_sync(args.platform, args.run_id, args.day, mode=args.mode,
                                     resume=args.resume, start_page=args.start_page,
                                     scope=args.scope)
        if not args.no_merge and status == "finished":
            from carscout.matching import merge_pending_listings
            try:
                merge_pending_listings(run_id=args.run_id, platforms=[args.platform])
            except Exception:
                traceback.print_exc()
        pool.close_pool()
        return 0 if status == "finished" else 2
    except BaseException:
        traceback.print_exc()
        try:
            common.finish_run(args.run_id, "failed", error=traceback.format_exc()[-800:])
        except Exception:
            pass
        return 3


if __name__ == "__main__":
    sys.exit(main())
