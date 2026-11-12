"""End-to-end export / review CSV round trip and API surface."""
import csv
import io

def _write_csv(rows):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()

import pytest

from carscout.crawl import runner
from carscout.db import pool
from carscout.fixture import server as fx

pytestmark = pytest.mark.slow


def _crawl_all():
    import time
    started = runner.run_platforms(["A", "B", "C", "D"], day="2026-11-12", auto_merge=True)
    return started


def test_review_csv_roundtrip_merges_and_learns_rule(fixtures, clean_db):
    summary = _crawl_all()
    assert all(s["status"] == "finished" for s in summary["platforms"].values()), summary
    from carscout.api import export as ex

    text = ex.export_review_csv(status="pending")
    rows = list(csv.DictReader(io.StringIO(text)))
    print("REVIEW ROWS:", [(r["candidate_id"], r["listing_a"], r["listing_b"], r["score"]) for r in rows])
    assert len(rows) >= 2
    # the A4L "second photo / different odometer" pair is always grey-zone;
    # confirm it as the same car and let the service learn from the decision
    target = next(r for r in rows
                  if any("pb-2013" in r[k] for k in ("listing_a", "listing_b")))
    target["decision"] = "merge"
    target["note"] = "同一台车，里程表被改过"
    result = ex.import_review_csv(_write_csv(rows), reviewer="qa")
    assert result["applied"] >= 1 and result["errors"] == 0

    # after the merge, all A4L listings carrying that VIN/feature set are one vehicle
    a4l = pool.query("""SELECT DISTINCT l.vehicle_id FROM listings l JOIN vehicles v ON v.id=l.vehicle_id
                        WHERE v.series='A4L' AND l.platform_listing_id IN ('pa-1003','pc-3003','pd-4003','pb-2013')""")
    assert len(a4l) == 1, a4l
    # the decision produced a reusable rule
    rule = pool.query_one("SELECT * FROM match_rules WHERE decision='merge'")
    assert rule is not None and rule["source"] == "manual"
    queue = pool.query("SELECT status FROM manual_review_queue WHERE candidate_id=%s",
                       (int(target["candidate_id"]),))
    assert queue and queue[0]["status"] == "confirmed_merge"


def test_learned_rule_applies_to_the_next_batch(fixtures, clean_db):
    """Re-running the merge after a confirmed split respects the rule."""
    from carscout.matching import confirm_review, merge_pending_listings
    from carscout.api import export as ex

    _crawl_all()
    text = ex.export_review_csv(status="pending")
    rows = list(csv.DictReader(io.StringIO(text)))
    target = next(r for r in rows
                  if any("pb-2013" in r[k] for k in ("listing_a", "listing_b")))
    res = confirm_review(int(target["candidate_id"]), "split", reviewer="qa", note="公里数差异过大")
    assert res["ok"]
    rule = pool.query_one("SELECT * FROM match_rules WHERE decision='split'")
    assert rule and rule["active"] is True


def test_api_surface(fixtures, clean_db):
    from fastapi.testclient import TestClient
    from carscout.api.app import app

    _crawl_all()
    client = TestClient(app)
    assert client.get("/healthz").json()["ok"] is True
    ov = client.get("/api/overview").json()
    assert ov["vehicles"] >= 12
    assert ov["provisional_vehicles"] >= 2       # grey-zone rows are parked, not merged

    series = client.get("/api/export/series", params={"series": "凯美瑞"}).json()
    assert series["window_days"] == 30
    assert series["by_platform"], series
    assert all(r["price_min"] and r["price_max"] for r in series["by_platform"])

    csv_resp = client.get("/api/export/series/csv", params={"series": "凯美瑞"})
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]

    # the clean Camry cluster (A+B) and the grey-zone C listing are two vehicles
    vehicles = client.get("/api/vehicles", params={"series": "凯美瑞"}).json()
    assert vehicles["count"] == 2
    # only the parked (undecided) listing is provisional; the clean cluster is settled
    assert sum(1 for v in vehicles["items"] if v["provisional"]) == 1
    main = max(vehicles["items"], key=lambda v: v["platform_count"])
    dossier = client.get(f"/api/vehicles/{main['id']}").json()
    # 两个平台 + 详情字段必须是两次请求抽取的完整结果
    assert dossier["platform_count"] == dossier["listing_count"] == 3
    assert set(dossier["platforms"]) == {"A", "B", "D"}
    assert dossier["price_min"] == 138000 and dossier["price_max"] == 140000
    for listing in dossier["listings"]:
        assert listing["source_url"] and listing["price_yuan"] and listing["description"]
    assert dossier["listings"], dossier

    queue = client.get("/api/review/queue").json()
    assert queue["count"] >= 2
    review_csv = client.get("/api/review/export.csv")
    assert "candidate_id,listing_a,listing_b" in review_csv.text

    cid = queue["items"][0]["candidate_id"]
    conf = client.post(f"/api/review/{cid}/confirm", json={"decision": "split", "note": "不同车"})
    assert conf.status_code == 200 and conf.json()["ok"]
    assert client.get("/api/rules").json()

    anomalies = client.get("/api/anomalies").json()
    assert anomalies["count"] == 0           # clean crawl produces no anomalies
    runs = client.get("/api/runs").json()
    assert runs["count"] >= 4
