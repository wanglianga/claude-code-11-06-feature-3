"""Store-level unit tests — the two upsert paths must never cross-update rows."""
import pytest

from carscout.db.pool import execute, query
from carscout.parsers import ParsedListing
from carscout.store import touch_list_only, upsert_listing, save_image_hashes


@pytest.mark.usefixtures("clean_db")
def test_list_upsert_only_touches_its_own_row():
    items = [
        {"platform": "A", "platform_listing_id": "pa-1", "url": "http://x/pa/detail/pa-1",
         "title": "宝马3系", "city": "南京", "price_yuan": 188000, "mileage_km": 60000,
         "reg_year": 2018, "reg_month": 6, "vin_tail6": None, "image": None, "raw": {}},
        {"platform": "A", "platform_listing_id": "pa-2", "url": "http://x/pa/detail/pa-2",
         "title": "奔驰C级", "city": "苏州", "price_yuan": 255000, "mileage_km": 29000,
         "reg_year": 2019, "reg_month": 11, "vin_tail6": None, "image": None, "raw": {}},
    ]
    for it in items:
        touch_list_only(it)
    touch_list_only({**items[0], "price_yuan": 189000})
    a = query("SELECT * FROM listings WHERE platform_listing_id='pa-1'")[0]
    b = query("SELECT * FROM listings WHERE platform_listing_id='pa-2'")[0]
    assert a["price_yuan"] == 189000 and a["title"] == "宝马3系"
    assert b["price_yuan"] == 255000 and b["title"] == "奔驰C级"


@pytest.mark.usefixtures("clean_db")
def test_detail_upsert_only_touches_its_own_row_and_records_cut():
    for pid, brand, series, price in (("pa-1", "宝马", "3系", 188000), ("pa-2", "奔驰", "C级", 255000)):
        touch_list_only({"platform": "A", "platform_listing_id": pid,
                         "url": f"http://x/pa/detail/{pid}", "title": None, "city": None,
                         "price_yuan": price, "mileage_km": None, "reg_year": None,
                         "reg_month": None, "vin_tail6": None, "image": None, "raw": {}})
    p1 = ParsedListing(platform="A", platform_listing_id="pa-1", source_url="http://x/pa/detail/pa-1",
                       title="宝马3系", brand="宝马", series="3系", reg_year=2018, reg_month=6,
                       mileage_km=60000, price_yuan=185000, image_urls=[], raw={})
    p2 = ParsedListing(platform="A", platform_listing_id="pa-2", source_url="http://x/pa/detail/pa-2",
                       title="奔驰C级", brand="奔驰", series="C级", reg_year=2019, reg_month=11,
                       mileage_km=29000, price_yuan=250000, image_urls=[], raw={})
    upsert_listing(p1)
    upsert_listing(p2)
    rows = {r["platform_listing_id"]: r for r in query("SELECT * FROM listings WHERE platform='A'")}
    assert rows["pa-1"]["series"] == "3系" and rows["pa-1"]["price_yuan"] == 185000
    assert rows["pa-2"]["series"] == "C级" and rows["pa-2"]["price_yuan"] == 250000
    cuts = query("""SELECT e.price_yuan, e.delta_yuan FROM listing_price_events e
                    JOIN listings l ON l.id = e.listing_id
                    WHERE l.platform_listing_id='pa-1' AND e.delta_yuan IS NOT NULL""")
    assert cuts and cuts[0]["delta_yuan"] == -3000


@pytest.mark.usefixtures("clean_db")
def test_price_drop_records_previous_price_and_delta():
    touch_list_only({"platform": "A", "platform_listing_id": "pa-1", "url": "http://x/pa/detail/pa-1",
                     "title": None, "city": None, "price_yuan": 199000, "mileage_km": None,
                     "reg_year": None, "reg_month": None, "vin_tail6": None, "image": None, "raw": {}})
    touch_list_only({"platform": "A", "platform_listing_id": "pa-1", "url": "http://x/pa/detail/pa-1",
                     "title": None, "city": None, "price_yuan": 192000, "mileage_km": None,
                     "reg_year": None, "reg_month": None, "vin_tail6": None, "image": None, "raw": {}})
    touch_list_only({"platform": "A", "platform_listing_id": "pa-1", "url": "http://x/pa/detail/pa-1",
                     "title": None, "city": None, "price_yuan": 188000, "mileage_km": None,
                     "reg_year": None, "reg_month": None, "vin_tail6": None, "image": None, "raw": {}})
    listing = query("SELECT * FROM listings WHERE platform_listing_id='pa-1'")[0]
    events = query("""SELECT price_yuan, previous_price, delta_yuan FROM listing_price_events
                      WHERE listing_id=%s ORDER BY id""", (listing["id"],))
    assert [e["price_yuan"] for e in events] == [199000, 192000, 188000]
    cuts = [e for e in events if e["delta_yuan"] is not None]
    assert len(cuts) == 2
    assert cuts[0]["previous_price"] == 199000 and cuts[0]["delta_yuan"] == -7000
    assert cuts[1]["previous_price"] == 192000 and cuts[1]["delta_yuan"] == -4000


@pytest.mark.usefixtures("clean_db")
def test_image_hashes_are_stored_per_listing():
    touch_list_only({"platform": "A", "platform_listing_id": "pa-1", "url": "http://x/pa/detail/pa-1",
                     "title": None, "city": None, "price_yuan": None, "mileage_km": None,
                     "reg_year": None, "reg_month": None, "vin_tail6": None, "image": None, "raw": {}})
    listing = query("SELECT id FROM listings WHERE platform_listing_id='pa-1'")[0]
    save_image_hashes(listing["id"], [("http://img/1.png", "aa55aa55aa55aa55", 320, 320),
                                      ("http://img/2.png", "55aa55aa55aa55aa", 320, 320)])
    rows = query("SELECT image_url, phash::text AS p FROM image_hashes ORDER BY image_url")
    assert len(rows) == 2
    assert format(int(rows[0]["p"].replace(" ", ""), 2), "016x") == "aa55aa55aa55aa55"
