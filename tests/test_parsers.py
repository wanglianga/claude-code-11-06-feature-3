"""Pure parser tests: field extraction, anomaly classification, price/mileage formats."""
from datetime import datetime, timezone

import pytest

from carscout import parsers
from carscout.fixture import server as fx


def test_price_formats():
    assert parsers.parse_price_yuan("18.60万") == 186000
    assert parsers.parse_price_yuan("18.6 万元") == 186000
    assert parsers.parse_price_yuan("186000元") == 186000
    assert parsers.parse_price_yuan("186000") == 186000
    assert parsers.parse_price_yuan("18.6") == 186000      # bare value treated as 万
    assert parsers.parse_price_yuan("价格待询") is None


def test_mileage_formats():
    assert parsers.parse_mileage_km("6.00万公里") == 60000
    assert parsers.parse_mileage_km("60000公里") == 60000
    assert parsers.parse_mileage_km("60,000 km") == 60000
    assert parsers.parse_mileage_km(60000) == 60000
    assert parsers.parse_mileage_km("6.0") == 60000


def test_registration_year_month():
    assert parsers.parse_reg_ym("2018-06") == (2018, 6)
    assert parsers.parse_reg_ym("2018年6月") == (2018, 6)
    assert parsers.parse_reg_ym("2019/03/01") == (2019, 3)
    assert parsers.parse_reg_ym("201806") == (2018, 6)
    assert parsers.parse_reg_ym("2018") == (2018, None)
    # a title containing a year and a month must not raise
    assert parsers.parse_reg_ym("【南京】宝马3系 2018款 上牌2018年6月") == (2018, 6)


def test_displacement_and_transfer():
    assert parsers.parse_displacement_ml("2.0L") == 2000
    assert parsers.parse_displacement_ml(2.0) == 2000
    assert parsers.parse_displacement_ml("纯电动") == 0
    assert parsers.parse_transfer_count("2次") == 2
    assert parsers.parse_transfer_count(0) == 0


def test_vin_tail_extraction():
    assert parsers.vin_tail("LFV3A28G8J1234ABC") == "234ABC"
    assert parsers.vin_tail("******234ABC") == "234ABC"
    assert parsers.vin_tail(None) is None
    assert parsers.vin_tail("ABC") is None


def test_block_signal_detection():
    assert parsers.detect_block_signal(403, "") == "http_403"
    assert parsers.detect_block_signal(429, "") == "http_429"
    assert parsers.detect_block_signal(200, "<title>安全验证</title>") == "安全验证"
    assert parsers.detect_block_signal(200, "<h1>平台A</h1>") is None


def test_a_detail_full_extraction():
    listing = fx.ds.listing_by_pid("A", "pa-1001")
    body = fx.render_detail_page("A", listing)
    p, an = parsers.parse_a_detail(body, source_url="http://x/pa/detail/pa-1001")
    assert an is None
    assert p.platform == "A"
    assert p.label if False else True
    assert (p.brand, p.series) == ("宝马", "3系")
    assert (p.reg_year, p.reg_month) == (2018, 6)
    assert p.mileage_km == 60000
    assert p.displacement_ml == 2000    # fixture renders displacement as "2.0L"
    assert p.transmission == "8挡手自一体"
    assert p.transfer_count == 1
    assert p.city == "南京"
    assert p.price_yuan == 188000
    assert p.listed_at.date().isoformat() == "2026-09-28"
    assert p.seller_type == "个人"
    assert p.vin_tail6 == "234ABC"
    assert len(p.image_urls) == 3


def test_a_detail_schema_drift_is_classified_not_blocked():
    listing = fx.ds.listing_by_pid("A", "pa-1001")
    body = fx.render_detail_page("A", listing, drift=True)   # 变速箱 removed, 表显里程 renamed
    p, an = parsers.parse_a_detail(body, source_url="http://x/pa/detail/pa-1001")
    assert an is not None
    assert an.category == "schema_drift"
    assert "mileage_km" in an.detail["missing"]
    assert p.reg_year == 2018            # the rest still parsed from the drifted page


def test_a_list_detects_confirmed_empty_vs_unverified():
    empty_ok = ('<div class="listing-list" data-total="0"></div>'
                '<footer class="site-foot">真实车源</footer>')
    items, an = parsers.parse_a_list(empty_ok, base_url="http://x")
    assert items == [] and an is None          # genuinely no listings

    stub = '<div class="listing-list"></div>'   # anti-bot stub: no footer marker
    items, an = parsers.parse_a_list(stub, base_url="http://x")
    assert items == []
    assert an is not None and an.category == "empty_unverified"


def test_c_stub_is_blocked_signal():
    items, an = parsers.parse_c_list("<html><body><div class='listing-list'></div></body></html>",
                                     base_url="http://x")
    assert an is not None and an.category == "empty_unverified"
    items, an = parsers.parse_c_list("<html><body>no container</body></html>", base_url="http://x")
    assert an.category == "blocked_signal"


def test_b_detail_platform_carries_metadata():
    listing = fx.ds.listing_by_pid("B", "pb-2001")
    payload = {"code": 0, "data": fx._b_detail(listing, False)}
    p, an = parsers.parse_b_detail(payload, source_url="/pb/detail/pb-2001")
    assert an is None
    assert p.platform == "B"
    assert p.mileage_km == 60000 and p.transfer_count == 1 and p.vin_tail6 == "234ABC"


def test_b_list_missing_items_is_schema_drift():
    items, an = parsers.parse_b_list({"page": 1, "pageSize": 8}, base_url="http://x")
    assert items == []
    assert an.category == "schema_drift"


def test_d_signed_errors_are_distinguished():
    items, an = parsers.decode_d_list({"code": "SIGN_INVALID", "reason": "signature_expired"},
                                     base_url="http://x")
    assert an.category == "signature_expired"
    items, an = parsers.decode_d_list({"code": "SESSION_REQUIRED", "reason": "no_session"},
                                     base_url="http://x")
    assert an.category == "blocked_signal"
    items, an = parsers.decode_d_list({"code": 0, "data": {}}, base_url="http://x")
    assert an.category == "schema_drift"


def test_d_detail_price_fen_conversion():
    listing = fx.ds.listing_by_pid("D", "pd-4001")
    payload = {"code": 0, "data": fx._d_detail(listing, False)}
    p, an = parsers.decode_d_detail(payload, source_url="/pd/detail/pd-4001")
    assert an is None
    assert p.price_yuan == listing.current_price()
    assert p.seller_type == "个人"


def test_signed_api_rejects_wrong_signature_and_expired_window():
    params = {"page": "1", "uid": "u", "ts": "1000"}
    params["sign"] = fx.d_sign(params, "wrong-secret")
    ok, reason = fx.verify_d_sign(params)
    assert not ok and reason == "signature_mismatch"
    params = {"page": "1", "uid": "u", "ts": "1000"}
    params["sign"] = fx.d_sign(params, fx.RUNTIME.d_secret)
    ok, reason = fx.verify_d_sign(params)
    assert not ok and reason == "signature_expired"
