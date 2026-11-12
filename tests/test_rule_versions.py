"""Versioned merge rules: spec management, replay gating, gray rollout,
rollback semantics and evidence-level explanation.

These tests stay at the DB/engine layer (hand-crafted listings, no crawl), so
they belong to the fast suite.
"""
import copy

import pytest
from fastapi.testclient import TestClient

from carscout import matching, rules
from carscout.config import settings
from carscout.db import pool


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _insert_listing(platform: str, pid: str, *, series: str = "3系", brand: str = "宝马",
                    reg_year: int = 2018, reg_month: int = 6, mileage_km: int = 60000,
                    city: str = "南京", price_yuan: int = 190000,
                    transmission: str = "8挡手自一体", displacement_ml: int = 1998,
                    transfer_count: int = 1, vin_tail6: str | None = None,
                    description: str = "个人一手车，保养记录齐全，无事故",
                    image_phash: str | None = "1010101010101010") -> dict:
    sql = """
        INSERT INTO listings
          (platform, platform_listing_id, source_url, url_norm, title, brand, series,
           reg_year, reg_month, mileage_km, displacement_ml, transmission, transfer_count,
           city, price_yuan, seller_type, description, vin_tail6, image_phash,
           detail_status, first_seen_at, last_seen_at, price_observed_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'dealer',%s,%s,
                ('x'||%s::text)::bit(64),'ok',now(),now(),now())
        RETURNING *"""
    return dict(pool.query_one(
        sql, (platform, pid, f"http://x/{platform}/{pid}", f"http://x/{platform}/{pid}",
              f"{brand}{series}", brand, series, reg_year, reg_month, mileage_km,
              displacement_ml, transmission, transfer_count, city, price_yuan,
              description, vin_tail6, image_phash)))


def _candidate_spec(**overrides) -> dict:
    spec = copy.deepcopy(rules.default_spec())
    spec.update(overrides)
    return spec


# ---------------------------------------------------------------------------
# baseline / spec validation
# ---------------------------------------------------------------------------
def test_baseline_version_seeded_with_legacy_parameters(clean_db):
    base = rules.get_version_by_number(rules.BASELINE_VERSION)
    assert base and base["status"] == "active"
    assert base["spec"] == rules.default_spec()
    assert rules.active_version_id() == base["id"]
    # fallback scoring and baseline-RuleSpec scoring are numerically identical
    a = _insert_listing("A", "pa-t1")
    b = _insert_listing("B", "pb-t1", mileage_km=62000)
    legacy = matching.score_pair(dict(a), dict(b), matching._phash_of(a), matching._phash_of(b))
    versioned = matching.score_pair(dict(a), dict(b), matching._phash_of(a),
                                    matching._phash_of(b), rules.load_spec(base["id"]))
    assert legacy.total == versioned.total
    assert legacy.components == versioned.components


def test_create_version_validates_thresholds_and_required_evidence(clean_db):
    bad = _candidate_spec()
    bad["thresholds"] = {"auto_merge": 0.5, "gray_zone": 0.7}  # gray above auto
    with pytest.raises(ValueError, match="gray_zone"):
        rules.create_version(bad, name="bad-threshold")

    bad2 = _candidate_spec()
    bad2["required_evidence"] = ["dna_match"]
    with pytest.raises(ValueError, match="required_evidence"):
        rules.create_version(bad2, name="bad-evidence")

    bad3 = _candidate_spec()
    del bad3["weights"]["image"]
    with pytest.raises(ValueError, match="weights"):
        rules.create_version(bad3, name="bad-weights")

    ok = rules.create_version(_candidate_spec(), name="v2 draft", changelog="just a draft")
    assert ok["status"] == "draft" and ok["version"] == 2


# ---------------------------------------------------------------------------
# necessary evidence
# ---------------------------------------------------------------------------
def test_required_vin_evidence_downgrades_auto_merge_to_grey_zone(clean_db):
    spec = _candidate_spec()
    spec["required_evidence"] = ["vin_match"]
    v2 = rules.create_version(spec, name="vin-required")
    # no replay yet: activation must be refused
    with pytest.raises(ValueError, match="回放"):
        rules.activate_version(v2["id"])

    a = _insert_listing("A", "pa-vin1")  # no VIN published
    vid = matching.create_vehicle(a, rule_version_id=v2["id"])
    b = _insert_listing("B", "pb-vin1")  # identical features, same photo, still no VIN
    res = matching.run_merge_for_listing(b, rule_version_id=v2["id"])
    assert res["action"] == "queued_for_review"          # would have auto-merged on v1
    cand = pool.query_one("SELECT * FROM match_candidates WHERE id = %s", (res["candidate_id"],))
    assert cand["rule_version_id"] == v2["id"]
    # components column stores {"components": {...}, "hard_conflict": ...}
    assert cand["components"]["components"]["required_evidence_missing"] == "vin_match"
    assert cand["components"]["required_block"] == "vin_match"
    assert any(e["code"] == "vin_match" for e in cand["evidence"])

    # once both sides publish the same VIN tail, the requirement is satisfied
    c = _insert_listing("C", "pc-vin1", vin_tail6="AB12CD")
    d = _insert_listing("D", "pd-vin1", vin_tail6="AB12CD")
    vid2 = matching.create_vehicle(c, rule_version_id=v2["id"])
    res2 = matching.run_merge_for_listing(d, rule_version_id=v2["id"])
    assert res2["action"] == "merged" and res2["vehicle_id"] == vid2


def test_required_image_evidence_blocks_when_photo_disagrees(clean_db):
    spec = _candidate_spec()
    spec["required_evidence"] = ["image_agreement"]
    v2 = rules.create_version(spec, name="image-required")
    a = _insert_listing("A", "pa-img1", image_phash="ffffffffffffffff")
    matching.create_vehicle(a, rule_version_id=v2["id"])
    b = _insert_listing("B", "pb-img1", image_phash="0000000000000000")  # hamming 64
    sb = matching.score_pair(dict(a), dict(b), matching._phash_of(a),
                             matching._phash_of(b), rules.RuleSpec(v2["spec"]))
    assert sb.required_block == "image_agreement"
    res = matching.run_merge_for_listing(b, rule_version_id=v2["id"])
    assert res["action"] == "queued_for_review"


# ---------------------------------------------------------------------------
# replay / approval gate
# ---------------------------------------------------------------------------
def _activate(version_id: int):
    rules.replay_version(version_id)
    rules.activate_version(version_id)


def test_replay_estimates_impact_and_blocks_activation_until_approved(clean_db):
    # Settings is a frozen dataclass: bypass the freeze like conftest does.
    object.__setattr__(settings, "replay_max_extra_merges", 1)
    try:
        _replay_impact_flow()
    finally:
        object.__setattr__(settings, "replay_max_extra_merges", 10)


def _replay_impact_flow():
    # two pairs: same structured features, totally different photos -> v1 grey
    # zone (gate cap), but a v2 with a 0.50 auto threshold merges them.
    _insert_listing("A", "pa-g1", image_phash="ffffffffffffffff")
    _insert_listing("B", "pb-g1", image_phash="0000000000000000")
    _insert_listing("A", "pa-g2", series="A4L", brand="奥迪", image_phash="ffffffffffffffff")
    _insert_listing("B", "pb-g2", series="A4L", brand="奥迪", image_phash="0000000000000000")
    # a human-confirmed pair across two clearly different cars (different series)
    x = _insert_listing("A", "pa-h1", series="汉", brand="比亚迪")
    y = _insert_listing("B", "pb-h1", series="雅阁", brand="本田")
    pool.execute(
        "INSERT INTO match_candidates (listing_a, listing_b, score, state) VALUES (%s,%s,0.9,'manual_confirmed')",
        (min(x["id"], y["id"]), max(x["id"], y["id"])))

    low = _candidate_spec()
    low["thresholds"] = {"auto_merge": 0.50, "gray_zone": 0.40}
    v2 = rules.create_version(low, name="looser-threshold")
    replay = rules.replay_version(v2["id"])
    assert replay["extra_merges"] >= 2
    assert replay["split_confirmed"] >= 1
    assert replay["exceeds_threshold"] is True
    with pytest.raises(ValueError, match="审批"):
        rules.activate_version(v2["id"])

    # rejecting keeps the block; approving lifts it
    rules.decide_replay(replay["id"], False, reviewer="qa")
    with pytest.raises(ValueError, match="审批"):
        rules.activate_version(v2["id"])
    rules.decide_replay(replay["id"], True, reviewer="qa", note="接受放量风险")
    activated = rules.activate_version(v2["id"])
    assert activated["activated"] == v2["id"]
    assert rules.active_version_id() == v2["id"]


def test_replay_within_threshold_activates_without_approval(clean_db):
    v2 = rules.create_version(_candidate_spec(), name="twin-spec")  # identical to baseline
    replay = rules.replay_version(v2["id"])
    assert replay["exceeds_threshold"] is False
    rules.activate_version(v2["id"])
    assert rules.active_version_id() == v2["id"]


def test_simulate_clustering_matches_live_partition(clean_db):
    a = _insert_listing("A", "pa-s1")
    matching.create_vehicle(a)
    b = _insert_listing("B", "pb-s1", mileage_km=61000)
    matching.run_merge_for_listing(b)
    _insert_listing("C", "pc-s1", series="汉", brand="比亚迪")  # different car
    sim = matching.simulate_clustering(rules.load_spec(rules.active_version_id()))
    # the two 3系 listings share a cluster both live and in simulation
    assert sim["partition"][a["id"]] == sim["partition"][b["id"]]
    assert sim["partition"][a["id"]] != sim["partition"][
        pool.query_one("SELECT id FROM listings WHERE platform_listing_id='pc-s1'")["id"]]
    pair = tuple(sorted((a["id"], b["id"])))
    assert pair in sim["pair_decisions"]


# ---------------------------------------------------------------------------
# percentage rollout (stable hash buckets)
# ---------------------------------------------------------------------------
def test_rollout_percentage_uses_stable_hash_buckets(clean_db):
    v2 = rules.create_version(_candidate_spec(), name="rollout-spec")
    _activate(v2["id"])
    base_id = rules.baseline_id()

    rules.set_rollout(v2["id"], {"A": 0})
    assert rules.select_version_id("A", 123) == base_id
    rules.set_rollout(v2["id"], {"A": 100})
    assert rules.select_version_id("A", 123) == v2["id"]

    rules.set_rollout(v2["id"], {"A": 50})
    first = [rules.select_version_id("A", i) for i in range(1, 201)]
    second = [rules.select_version_id("A", i) for i in range(1, 201)]
    assert first == second                          # stable across calls
    assert 0.3 < sum(1 for v in first if v == v2["id"]) / len(first) < 0.7
    # explicit platform beats the wildcard
    rules.set_rollout(v2["id"], {"*": 100, "A": 0})
    assert rules.select_version_id("A", 123) == base_id
    assert rules.select_version_id("B", 123) == v2["id"]


# ---------------------------------------------------------------------------
# rollback: flag for re-review, never auto-split
# ---------------------------------------------------------------------------
def test_rollback_flags_listings_pending_rereview_without_splitting(clean_db):
    v2 = rules.create_version(_candidate_spec(), name="rollback-spec")
    _activate(v2["id"])

    a = _insert_listing("A", "pa-rb1")
    matching.create_vehicle(a, rule_version_id=v2["id"])
    b = _insert_listing("B", "pb-rb1", mileage_km=60500)
    res = matching.run_merge_for_listing(b, rule_version_id=v2["id"])
    assert res["action"] == "merged"
    vehicle_before = pool.query_one("SELECT vehicle_id FROM listings WHERE id = %s", (b["id"],))["vehicle_id"]
    vehicles_before = pool.query_one("SELECT count(*) AS n FROM vehicles")["n"]

    out = rules.rollback_active(reason="线上发现误合并")
    assert out["listings_pending_rereview"] >= 2

    row = pool.query_one("SELECT * FROM listings WHERE id = %s", (b["id"],))
    assert row["match_status"] == "pending_rereview"
    assert row["vehicle_id"] == vehicle_before          # NOT auto-split
    assert "误合并" in row["pending_rereview_reason"]
    # historical statistics do not jump: vehicles are untouched
    assert pool.query_one("SELECT count(*) AS n FROM vehicles")["n"] == vehicles_before

    # the automated merge pass leaves flagged rows alone (they keep their
    # vehicle_id and stay parked outside the unmatched working set)
    matching.merge_pending_listings()
    assert pool.query_one("SELECT match_status FROM listings WHERE id=%s", (b["id"],))["match_status"] \
        == "pending_rereview"
    # run_merge_for_listing itself refuses to re-decide a flagged row
    flagged = dict(pool.query_one("SELECT * FROM listings WHERE id = %s", (b["id"],)))
    assert matching.run_merge_for_listing(flagged)["action"] == "pending_rereview"

    # the affected pair re-enters the human review queue under a rollback batch
    q = pool.query_one("SELECT * FROM manual_review_queue WHERE batch_id LIKE %s",
                       ("rollback-%",))
    assert q and q["status"] == "pending"
    # previous version is active again
    assert rules.active_version_id() == rules.baseline_id()

    # a human can still resolve the pair through the normal review loop
    cid = q["candidate_id"]
    decision = matching.confirm_review(cid, "split", reviewer="qa")
    assert decision["ok"]


def test_rereview_listings_visible_via_api(clean_db):
    v2 = rules.create_version(_candidate_spec(), name="rb-api-spec")
    _activate(v2["id"])
    a = _insert_listing("A", "pa-api1")
    matching.create_vehicle(a, rule_version_id=v2["id"])
    rules.rollback_active()
    client = TestClient(__import__("carscout.api.app", fromlist=["app"]).app)
    resp = client.get("/api/pending-rereview")
    assert resp.status_code == 200 and resp.json()["count"] >= 1


# ---------------------------------------------------------------------------
# rebuild under an active rolled-out version
# ---------------------------------------------------------------------------
def test_rebuild_restamps_listings_using_active_version_rollout(clean_db):
    v2 = rules.create_version(_candidate_spec(), name="rebuild-spec")
    _activate(v2["id"])
    rules.set_rollout(v2["id"], {"*": 100})
    _insert_listing("A", "pa-rb2")
    _insert_listing("B", "pb-rb2", mileage_km=61000)

    summary = matching.rebuild_all_vehicles()
    assert summary.get("created_vehicle")
    stamped = {r["rule_version_id"] for r in pool.query(
        "SELECT DISTINCT rule_version_id FROM listings WHERE rule_version_id IS NOT NULL")}
    assert stamped == {v2["id"]}
    # the merge pair keeps full evidence on its candidate row
    cand = pool.query_one("SELECT * FROM match_candidates")
    assert cand and cand["evidence"] and cand["rule_version_id"] == v2["id"]


# ---------------------------------------------------------------------------
# evidence-level explanation
# ---------------------------------------------------------------------------
def test_explain_pair_reports_every_evidence_with_weight(clean_db):
    a = _insert_listing("A", "pa-ex1")
    b = _insert_listing("B", "pb-ex1", mileage_km=72000, city="苏州")
    explanation = matching.explain_pair(a["id"], b["id"])
    codes = {e["code"] for e in explanation["evidence"]}
    assert {"series", "reg_month", "mileage", "city", "image_agreement",
            "text_agreement", "transmission", "displacement"}.issubset(codes)
    for e in explanation["evidence"]:
        assert set(e) >= {"code", "label", "matched", "weight", "contribution", "detail"}
    assert explanation["rule_version"]["version"] == rules.BASELINE_VERSION
    assert explanation["score"] >= 0
    # explain under a VIN-requiring version surfaces the missing necessity
    spec = _candidate_spec()
    spec["required_evidence"] = ["vin_match"]
    v2 = rules.create_version(spec, name="explain-spec")
    explanation2 = matching.explain_pair(a["id"], b["id"], v2["id"])
    assert explanation2["required_block"] == "vin_match"
    assert explanation2["decision"] == "gray"


def test_full_rule_lifecycle_via_http(clean_db):
    client = TestClient(__import__("carscout.api.app", fromlist=["app"]).app)
    spec = client.get("/api/rule-versions/default-spec").json()
    spec["thresholds"] = {"auto_merge": 0.8, "gray_zone": 0.55}
    created = client.post("/api/rule-versions", json={
        "name": "http-lifecycle", "spec": spec, "changelog": "api 端到端"}).json()
    vid = created["id"]
    # activation without replay is rejected
    bad = client.post(f"/api/rule-versions/{vid}/activate", json={})
    assert bad.status_code == 400
    replay = client.post(f"/api/rule-versions/{vid}/replay").json()
    if replay["exceeds_threshold"]:
        client.post(f"/api/replays/{replay['id']}/decision", json={"approve": True})
    client.post(f"/api/rule-versions/{vid}/activate",
                json={"initial_rollout": {"A": 10}}).raise_for_status()
    rollout = client.post(f"/api/rule-versions/{vid}/rollout",
                          json={"rollouts": {"A": 100, "B": 25}}).json()
    assert rollout["rollouts"] == {"A": 100, "B": 25}

    a = _insert_listing("A", "pa-http1")
    b = _insert_listing("B", "pb-http1")
    explained = client.get("/api/explain", params={"listing_a": a["id"], "listing_b": b["id"]}).json()
    assert explained["rule_version"]["id"] == vid
    assert any(e["code"] == "series" for e in explained["evidence"])

    rolled = client.post(f"/api/rule-versions/{vid}/rollback",
                         json={"reason": "http 回滚验证"}).json()
    assert rolled["review_batch"].startswith("rollback-")
    versions = client.get("/api/rule-versions").json()
    statuses = {v["version"]: v["status"] for v in versions}
    assert statuses[2] == "rollback" and statuses[1] == "active"
