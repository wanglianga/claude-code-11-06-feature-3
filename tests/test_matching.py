"""Merge scoring: VIN rule, feature recall, grey-zone caps."""
from carscout import matching
from carscout.config import settings


def _row(**kw):
    base = dict(brand="宝马", series="3系", reg_year=2018, reg_month=6, mileage_km=60000,
                city="南京", transmission="8挡手自一体", displacement_ml=1998,
                transfer_count=1, description="个人一手车，保养记录齐全，无事故",
                price_yuan=190000, image_phash=None, vin_tail6=None)
    base.update(kw)
    return base


def test_vin_tail_match_is_decisive_even_with_different_photos():
    a = _row(vin_tail6="234ABC", image_phash="ffffffffffffffff")
    b = _row(vin_tail6="234ABC", image_phash="0000000000000000", mileage_km=95000)
    sb = matching.score_pair(a, b, a["image_phash"], b["image_phash"])
    assert sb.total == 1.0
    assert sb.components.get("vin_tail6") == 1.0


def test_vin_conflict_lowers_score():
    sb = matching.score_pair(_row(vin_tail6="111111"), _row(vin_tail6="222222"), None, None)
    assert "vin_conflict" in sb.components
    assert sb.total < 1.0


def test_series_mismatch_is_hard_reject():
    sb = matching.score_pair(_row(), _row(series="A4L"), None, None)
    assert sb.hard_conflict == "series_mismatch"
    assert sb.total == 0.0


def test_registration_date_window_conflict():
    sb = matching.score_pair(_row(reg_year=2018, reg_month=6),
                             _row(reg_year=2011, reg_month=6), None, None)
    assert sb.hard_conflict == "reg_date_conflict"


def test_close_numbers_but_different_photo_stays_in_grey_zone():
    a = _row(image_phash="ffffffffffffffff")
    b = _row(image_phash="0000000000000000", mileage_km=76000,
             description="外地转入，公里数以实际为准")
    sb = matching.score_pair(a, b, a["image_phash"], b["image_phash"])
    assert settings.gray_zone_score <= sb.total < settings.auto_merge_score
    assert sb.components.get("gate_image_conflict") == 1.0


def test_identical_features_score_above_auto_merge():
    a = _row(image_phash="1010101010101010")
    sb = matching.score_pair(a, _row(), a["image_phash"], a["image_phash"])
    assert sb.total >= float(settings.auto_merge_score)
    assert "capped_by_gate" not in sb.components


def test_reg_month_index():
    assert matching._reg_month_index(2018, 6) == 2018 * 12 + 5
    assert matching._reg_month_index(None, None) is None
    assert matching._reg_month_index(2019, None) == 2019 * 12 + 5
