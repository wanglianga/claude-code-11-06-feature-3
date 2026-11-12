"""API + export behaviour without a full crawl (seeded rows only)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from carscout.db.pool import execute, query
from carscout.utils import hash_to_bit_literal


@pytest.fixture()
def seeded(clean_db):
    now = datetime.now(timezone.utc)
    with __import__("carscout.db.pool", fromlist=["tx"]).tx() as conn:
        cur = conn.execute(
            """INSERT INTO vehicles (brand, series, reg_year, reg_month, mileage_km, city,
                    platform_count, first_listed_at, last_seen_at)
               VALUES ('丰田','凯美瑞',2019,11,38000,'苏州',3,%s,%s) RETURNING id""", (now, now))
        vid = cur.fetchone()["id"]; cur.close()
        listings = []
        for i, (plat, price, days) in enumerate([("A", 138000, 40), ("B", 140000, 36), ("D", 139000, 33)]):
            cur = conn.execute(
                """INSERT INTO listings (platform, platform_listing_id, source_url, url_norm, title,
                        brand, series, price_yuan, listed_at, first_seen_at, last_seen_at,
                        vehicle_id, match_status, detail_status, description)
                   VALUES (%s, %s, %s, %s, %s, '丰田','凯美瑞', %s, %s, %s, %s, %s,'auto','ok','车况良好')
                   RETURNING id""",
                (plat, f"{plat}-x{i}", f"http://x/{plat}/{i}", f"http://x/{plat}/{i}",
                 "丰田凯美瑞 2019", price, now - timedelta(days=days),
                 now - timedelta(days=days), now, vid))
            listings.append(cur.fetchone()["id"]); cur.close()
        conn.execute("""INSERT INTO listing_price_events (listing_id, price_yuan, previous_price,
                        delta_yuan, observed_at) VALUES (%s, 145000, 150000, -5000, %s)""",
                     (listings[0], now - timedelta(days=20)))
        conn.execute("""INSERT INTO listing_price_events (listing_id, price_yuan, previous_price,
                        delta_yuan, observed_at) VALUES (%s, 138000, 145000, -7000, %s)""",
                     (listings[0], now - timedelta(days=5)))
    yield vid


def test_series_export_reports_per_platform_distribution(seeded):
    from carscout.api.app import app

    client = TestClient(app)
    res = client.get("/api/export/series", params={"series": "凯美瑞", "days": 30}).json()
    assert res["window_days"] == 30
    rows = res["by_platform"]
    assert {r["platform"] for r in rows} == {"A", "B", "D"}
    pricing = {r["platform"]: (r["price_min"], r["price_max"]) for r in rows}
    assert pricing["A"] == (138000, 138000)
    assert pricing["B"] == (140000, 140000)
    # one merged vehicle shows up once in the cross-platform view
    assert len(res["by_vehicle"]) == 1
    assert res["by_vehicle"][0]["platforms"] == 3


def test_series_export_honours_window(seeded):
    from carscout.api.app import app

    client = TestClient(app)
    res = client.get("/api/export/series", params={"series": "凯美瑞", "days": 1}).json()
    # last_seen_at is now, so all three listings remain in the 1-day window
    assert res["by_platform"]


def test_vehicle_dossier_counts_cuts_and_age(seeded):
    from carscout.api.app import app

    client = TestClient(app)
    d = client.get(f"/api/vehicles/{seeded}").json()
    assert d["platform_count"] == 3
    assert d["total_price_cuts"] == 2            # two price drops recorded
    assert d["price_min"] == 138000 and d["price_max"] == 140000
    # "首次挂牌" tracks the platform's own publish date, and the first observation
    # is reported separately so an estimator can tell dataset age from crawl age
    assert d["days_since_first_listing"] >= 30
    assert d["days_since_first_observed"] >= 30
    assert d["first_listed_at"] and d["first_observed_at"]
    a = next(l for l in d["listings"] if l["platform"] == "A")
    assert a["price_cuts"] == 2
    assert a["price_history"]
    assert a["source_url"]


def test_out_of_window_listing_is_excluded(seeded):
    from carscout.api.app import app

    execute("UPDATE listings SET first_seen_at = now() - interval '90 days', "
            "last_seen_at = now() - interval '60 days' WHERE platform='D'")
    client = TestClient(app)
    res = client.get("/api/export/series", params={"series": "凯美瑞", "days": 30}).json()
    assert {r["platform"] for r in res["by_platform"]} == {"A", "B"}


def test_unknown_vehicle_is_404(seeded):
    from carscout.api.app import app

    client = TestClient(app)
    assert client.get("/api/vehicles/999999").status_code == 404
    assert client.get("/api/health/Z").status_code == 404


def test_review_import_rejects_blank_and_unknown_decisions(clean_db):
    from carscout.api import export

    csv_text = "candidate_id,decision,note\n999999,merge,ghost\n7,banana,oops\n8,,blank\n"
    res = export.import_review_csv(csv_text)
    assert res["applied"] == 0
    assert res["skipped"] == 1
    assert res["errors"] == 2
