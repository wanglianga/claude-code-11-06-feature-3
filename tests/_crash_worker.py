"""Helper process for the crash-recovery test: crawl platform A, then die."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from carscout.crawl.runner import crawl_platform_sync
from carscout.crawl import common
from datetime import date

run_id = common.create_run("A", date.fromisoformat(sys.argv[1]))["id"]
print("RUN_ID", run_id, flush=True)
print("crawl_platform_sync", crawl_platform_sync("A", str(run_id), sys.argv[1]), flush=True)
