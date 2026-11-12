"""Cross-platform merge engine.

Order of operations for every active listing without a vehicle:
  1. Platform-local dedup is enforced by the DB unique key (platform, listing_id).
  2. VIN rule: identical VIN tail-6 (or full VIN) => merge immediately.
  3. Otherwise recall candidates by brand/series + registration month window +
     mileage band + city, score them (image pHash, description text, numeric
     agreement), apply learned rules, and then:
        score >= AUTO_MERGE_SCORE  -> auto merge
        GRAY_ZONE <= score < auto  -> manual review queue
        score < GRAY_ZONE          -> keep separate (candidate row for audit)

Every threshold, weight, gate and necessary-evidence condition comes from a
versioned :class:`carscout.rules.RuleSpec` (table merge_rule_versions). Each
scoring run also produces an evidence list explaining, at evidence level, why
two listings were judged the same car, which evidence fired and what each
weighted contribution was.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from carscout.config import settings
from carscout.db import pool
from carscout.rules import RuleSpec, default_spec
from carscout.utils import (hamming_hex, phash_similarity, price_median,
                            series_similarity, text_similarity)

M = 10000.0


def _fallback_spec() -> RuleSpec:
    """Spec for callers that do not pin a version.

    Built from the v1 baseline and patched with the process-level env
    overrides (AUTO_MERGE_SCORE / ...) so legacy behaviour is preserved.
    """
    raw = default_spec()
    raw["thresholds"]["auto_merge"] = float(settings.auto_merge_score)
    raw["thresholds"]["gray_zone"] = float(settings.gray_zone_score)
    raw["recall"]["reg_months"] = int(settings.recall_reg_months)
    raw["recall"]["mileage_km"] = int(settings.recall_mileage_km)
    raw["recall"]["candidate_limit"] = int(settings.candidate_limit)
    return RuleSpec(raw)


@dataclass
class ScoreBreakdown:
    total: float
    components: dict[str, Any]
    hard_conflict: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    required_block: str | None = None

    def to_json(self) -> str:
        return json.dumps({"components": self.components, "hard_conflict": self.hard_conflict,
                           "required_block": self.required_block},
                          ensure_ascii=False, sort_keys=True)

    def evidence_json(self) -> str:
        return json.dumps(self.evidence, ensure_ascii=False, sort_keys=True)


def _num_similarity(a: float | None, b: float | None, *, tolerance: float, scale: float,
                    max_points: float) -> float:
    """0..max_points based on |a-b|; 0 when one side is missing."""
    if a is None or b is None:
        return 0.0
    diff = abs(float(a) - float(b))
    if diff <= tolerance:
        return max_points
    excess = diff - tolerance
    return max(0.0, max_points * math.exp(-excess / scale))


def _reg_month_index(y: int | None, m: int | None) -> int | None:
    if y is None:
        return None
    return y * 12 + ((m or 6) - 1)


def _ev(code: str, label: str, *, matched: bool, weight: float | None,
        contribution: float, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """One evidence-level record for the explanation trail."""
    return {
        "code": code, "label": label, "matched": bool(matched),
        "weight": round(weight, 4) if weight is not None else None,
        "contribution": round(float(contribution), 4),
        "detail": detail or {},
    }


def score_pair(a: dict[str, Any], b: dict[str, Any], phash_a: str | None, phash_b: str | None,
               spec: RuleSpec | None = None) -> ScoreBreakdown:
    spec = spec or _fallback_spec()
    w = spec.weights
    comps: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []
    hard_conflict = None

    # hard identifiers -----------------------------------------------------
    vy_a = _reg_month_index(a.get("reg_year"), a.get("reg_month"))
    vy_b = _reg_month_index(b.get("reg_year"), b.get("reg_month"))
    city_a, city_b = (a.get("city") or "").strip(), (b.get("city") or "").strip()

    # brand/series: hard reject on series mismatch
    ser = series_similarity(
        f"{a.get('brand') or ''}{a.get('series') or ''}", f"{b.get('brand') or ''}{b.get('series') or ''}")
    comps["series"] = round(ser * w["series"], 4)
    evidence.append(_ev("series", "品牌车系相似度", matched=ser >= spec.series_min_similarity,
                        weight=w["series"], contribution=comps["series"],
                        detail={"raw_similarity": round(ser, 4)}))
    if ser < spec.series_min_similarity:
        return ScoreBreakdown(0.0, {"series": comps["series"]},
                              hard_conflict="series_mismatch", evidence=evidence)
    comps["series_raw"] = ser

    # VIN tail ---------------------------------------------------------------
    ta, tb = a.get("vin_tail6"), b.get("vin_tail6")
    if ta and tb and ta == tb:
        comps["vin_tail6"] = 1.0
        evidence.append(_ev("vin_match", "VIN 后六位一致（必要证据候选）", matched=True,
                            weight=None, contribution=1.0, detail={"vin_tail6": ta}))
        return ScoreBreakdown(1.0, {"vin_tail6": 1.0}, evidence=evidence)
    if ta and tb and ta != tb:
        # both platforms published a (different) VIN tail: same car is very unlikely
        comps["vin_tail6"] = 0.0
        comps["vin_conflict"] = spec.vin_conflict_penalty
        evidence.append(_ev("vin_conflict", "双方 VIN 后六位不一致", matched=False,
                            weight=None, contribution=spec.vin_conflict_penalty,
                            detail={"vin_a": ta, "vin_b": tb}))
    elif ta or tb:
        evidence.append(_ev("vin_match", "VIN 后六位（仅一方公开，无法比对）", matched=False,
                            weight=None, contribution=0.0,
                            detail={"vin_a": ta, "vin_b": tb}))
    else:
        evidence.append(_ev("vin_match", "VIN 后六位（双方均未公开，无法满足一致性要求）",
                            matched=False, weight=None, contribution=0.0, detail={}))

    # registration month -----------------------------------------------------
    if vy_a is not None and vy_b is not None:
        delta = abs(vy_a - vy_b)
        comps["reg_month"] = round(min(w["reg_month"], max(0.0, w["reg_month"] - delta / spec.reg_decay_months)), 4)
        comps["reg_delta_months"] = delta
        evidence.append(_ev("reg_month", "上牌年月接近度", matched=delta <= spec.recall_reg_months,
                            weight=w["reg_month"], contribution=comps["reg_month"],
                            detail={"delta_months": delta, "window_months": spec.recall_reg_months}))
        if delta > spec.recall_reg_months:
            hard_conflict = "reg_date_conflict"
    else:
        comps["reg_month"] = spec.unknown_reg_credit
        evidence.append(_ev("reg_month", "上牌年月（一方缺失，给中性分）", matched=False,
                            weight=w["reg_month"], contribution=comps["reg_month"], detail={}))

    # mileage ---------------------------------------------------------------
    ma, mb = a.get("mileage_km"), b.get("mileage_km")
    comps["mileage"] = round(_num_similarity(
        ma, mb, tolerance=spec.mileage_tolerance_km, scale=spec.mileage_scale_km,
        max_points=w["mileage"]), 4)
    if ma and mb:
        mileage_delta = abs(int(ma) - int(mb))
        comps["mileage_delta"] = mileage_delta
        evidence.append(_ev("mileage", "表显里程接近度", matched=mileage_delta <= spec.mileage_tolerance_km,
                            weight=w["mileage"], contribution=comps["mileage"],
                            detail={"delta_km": mileage_delta,
                                    "tolerance_km": spec.mileage_tolerance_km}))
    else:
        evidence.append(_ev("mileage", "表显里程（一方缺失）", matched=False,
                            weight=w["mileage"], contribution=0.0, detail={}))

    # city -----------------------------------------------------------------
    city_match = bool(city_a and city_b and city_a == city_b)
    comps["city"] = w["city"] if city_match else 0.0
    evidence.append(_ev("city", "所在城市一致", matched=city_match, weight=w["city"],
                        contribution=comps["city"], detail={"city_a": city_a, "city_b": city_b}))

    # transmission / displacement / transfers ------------------------------
    if a.get("transmission") and b.get("transmission"):
        tx_match = str(a["transmission"]) == str(b["transmission"])
        comps["transmission"] = w["transmission"] if tx_match else 0.0
        evidence.append(_ev("transmission", "变速箱一致", matched=tx_match, weight=w["transmission"],
                            contribution=comps["transmission"],
                            detail={"a": a["transmission"], "b": b["transmission"]}))
    else:
        comps["transmission"] = spec.unknown_transmission_credit
        evidence.append(_ev("transmission", "变速箱（一方缺失，给中性分）", matched=False,
                            weight=w["transmission"], contribution=comps["transmission"], detail={}))
    da, db = a.get("displacement_ml"), b.get("displacement_ml")
    if da is not None and db is not None and da and db:
        disp = _num_similarity(da, db, tolerance=spec.displacement_tolerance_ml,
                               scale=spec.displacement_scale_ml, max_points=w["displacement"])
        comps["displacement"] = round(disp, 4)
        evidence.append(_ev("displacement", "排量接近度",
                            matched=abs(int(da) - int(db)) <= spec.displacement_tolerance_ml,
                            weight=w["displacement"], contribution=comps["displacement"],
                            detail={"a_ml": da, "b_ml": db}))
    else:
        comps["displacement"] = 0.0
        evidence.append(_ev("displacement", "排量（一方缺失）", matched=False,
                            weight=w["displacement"], contribution=0.0, detail={}))
    ta2, tb2 = a.get("transfer_count"), b.get("transfer_count")
    if ta2 is not None and tb2 is not None:
        tr_match = int(ta2) == int(tb2)
        comps["transfer"] = w["transfer"] if tr_match else 0.0
        evidence.append(_ev("transfer", "过户次数一致", matched=tr_match, weight=w["transfer"],
                            contribution=comps["transfer"], detail={"a": ta2, "b": tb2}))
    else:
        evidence.append(_ev("transfer", "过户次数（一方缺失）", matched=False,
                            weight=w["transfer"], contribution=0.0, detail={}))

    # image pHash ------------------------------------------------------------
    image = None
    phash_dist = None
    if phash_a and phash_b:
        dist = hamming_hex(phash_a, phash_b)
        phash_dist = dist
        image = phash_similarity(dist)
        comps["phash_hamming"] = float(dist)
        comps["image"] = round(image * w["image"], 4)
        evidence.append(_ev("image_agreement", "图片感知哈希(pHash)一致性",
                            matched=dist < spec.image_gate_hamming, weight=w["image"],
                            contribution=comps["image"],
                            detail={"hamming_distance": dist,
                                    "gate_distance": spec.image_gate_hamming,
                                    "raw_similarity": round(image, 4)}))
    else:
        comps["image"] = 0.0
        evidence.append(_ev("image_agreement", "图片 pHash（一方缺失，无法比对）", matched=False,
                            weight=w["image"], contribution=0.0, detail={}))

    # description text ------------------------------------------------------
    text = None
    if a.get("description") and b.get("description"):
        text = text_similarity(a.get("description"), b.get("description"))
        comps["text_raw"] = text
        comps["text"] = round(text * w["text"], 4)
        evidence.append(_ev("text_agreement", "车况描述文本相似度",
                            matched=text >= spec.text_gate_similarity, weight=w["text"],
                            contribution=comps["text"],
                            detail={"raw_similarity": round(text, 4),
                                    "gate_similarity": spec.text_gate_similarity}))
    else:
        comps["text"] = 0.0
        evidence.append(_ev("text_agreement", "车况描述（一方缺失，无法比对）", matched=False,
                            weight=w["text"], contribution=0.0, detail={}))

    # Evidence gates. Two independent sources (a photograph of a *different*
    # car, and a description that reads like a different car) must not both be
    # ignored just because the structured fields happen to line up. Any such
    # disagreement caps the pair inside the grey zone so a human decides.
    gate_cap: float | None = None
    if phash_dist is not None and phash_dist >= spec.image_gate_hamming:
        gate_cap = spec.gray_zone_score + spec.gate_cap_offset
        comps["gate_image_conflict"] = 1.0
    if text is not None and text < spec.text_gate_similarity:
        cap = spec.gray_zone_score + spec.gate_cap_offset
        gate_cap = min(gate_cap, cap) if gate_cap is not None else cap
        comps["gate_text_disagreement"] = 1.0

    # Necessary-evidence conditions. A configured condition (e.g. "VIN must
    # match") that cannot be satisfied forbids auto-merging: the pair is
    # capped inside the grey zone for a human instead of being silently split.
    required_block = _check_required_evidence(spec, evidence, ta, tb, phash_dist, text)
    if required_block:
        comps["required_evidence_missing"] = required_block
        gate_cap = min(gate_cap, spec.gray_zone_score) if gate_cap is not None \
            else spec.gray_zone_score

    # price sanity (informational; never a hard conflict) --------------------
    pa, pb = a.get("price_yuan"), b.get("price_yuan")
    if pa and pb:
        ratio = min(pa, pb) / max(pa, pb)
        comps["price_ratio"] = round(ratio, 4)

    total = sum(v for k, v in comps.items() if k in {
        "series", "reg_month", "mileage", "city", "transmission", "displacement",
        "transfer", "image", "text", "vin_tail6", "vin_conflict"})
    total = max(0.0, min(1.0, round(total, 4)))
    if gate_cap is not None and total > gate_cap:
        comps["capped_by_gate"] = round(total, 4)
        total = round(gate_cap, 4)
    return ScoreBreakdown(total, comps, hard_conflict, evidence, required_block)


def _check_required_evidence(spec: RuleSpec, evidence: list[dict[str, Any]],
                             vin_a: str | None, vin_b: str | None,
                             phash_dist: int | None, text: float | None) -> str | None:
    """Return the first unsatisfied necessary-evidence code, else None."""
    for code in spec.required_evidence:
        if code == "vin_match":
            if not (vin_a and vin_b and vin_a == vin_b):
                return code
        elif code == "image_agreement":
            # a missing photo cannot satisfy a "photos must agree" requirement
            if phash_dist is None or phash_dist >= spec.image_gate_hamming:
                return code
        elif code == "text_agreement":
            if text is None or text < spec.text_gate_similarity:
                return code
    return None


def _phash_of(row: dict[str, Any]) -> str | None:
    """Return the 16-char hex pHash of a row.

    psycopg hands bit(64) columns back as a 64-character '0'/'1' string, so this
    normalises whatever we get into hex. SQL converts back with ('x'||%s)::bit(64).
    """
    return _bitstr_to_hex(row.get("image_phash"))


def phash_literal(hex_value: str | None) -> str | None:
    """SQL fragment helper value: hex digits, to be wrapped as ('x'||%s)::bit(64)."""
    if not hex_value:
        return None
    return format(int(hex_value, 16), "016x")


def _bitstr_to_hex(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if all(c in "01" for c in s) and len(s) in (64, 128):
        return format(int(s, 2), "016x")
    if len(s) == 16 and all(c in "0123456789abcdef" for c in s.lower()):
        return s.lower()
    return None


# ---------------------------------------------------------------------------
# vehicle creation & refresh
# ---------------------------------------------------------------------------
def _vehicle_fields_from(listing: dict[str, Any]) -> dict[str, Any]:
    return {
        "brand": listing.get("brand"), "series": listing.get("series"),
        "reg_year": listing.get("reg_year"), "reg_month": listing.get("reg_month"),
        "mileage_km": listing.get("mileage_km"), "displacement_ml": listing.get("displacement_ml"),
        "transmission": listing.get("transmission"), "transfer_count": listing.get("transfer_count"),
        "city": listing.get("city"), "seller_type": listing.get("seller_type"),
        "win_vin": listing.get("vin"), "vin_tail6": listing.get("vin_tail6"),
        "title_rep": listing.get("title"), "description_rep": listing.get("description"),
        "image_phash": phash_literal(_phash_of(listing)),
        "first_listed_at": listing.get("listed_at"),
        "last_seen_at": listing.get("last_seen_at"),
    }


def create_vehicle(listing: dict[str, Any], confidence: float = 1.0,
                   status: str = "auto", rule_version_id: int | None = None) -> int:
    f = _vehicle_fields_from(listing)
    with pool.tx() as conn:
        cur = conn.execute(
            """INSERT INTO vehicles
               (brand, series, reg_year, reg_month, mileage_km, displacement_ml, transmission,
                transfer_count, city, seller_type, win_vin, vin_tail6, title_rep, description_rep,
                image_phash, first_listed_at, last_seen_at, platform_count, merged_confidence,
                rule_version_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,('x'||%s::text)::bit(64),%s,%s,0,%s,%s)
               RETURNING id""",
            (f["brand"], f["series"], f["reg_year"], f["reg_month"], f["mileage_km"],
             f["displacement_ml"], f["transmission"], f["transfer_count"], f["city"],
             f["seller_type"], f["win_vin"], f["vin_tail6"], f["title_rep"], f["description_rep"],
             f["image_phash"], f["first_listed_at"], f["last_seen_at"], confidence,
             rule_version_id),
        )
        vid = int(cur.fetchone()["id"])
        cur.close()
        conn.execute(
            """UPDATE listings SET vehicle_id = %s, match_status = %s, rule_version_id = %s,
                      pending_rereview_reason = NULL, updated_at = now() WHERE id = %s""",
            (vid, status, rule_version_id, listing["id"]))
        _refresh_vehicle_stats(conn, vid)
        return vid


def attach_to_vehicle(listing_id: int, vehicle_id: int, *, confidence: float, status: str = "auto",
                      rule_version_id: int | None = None) -> None:
    with pool.tx() as conn:
        conn.execute(
            """UPDATE listings SET vehicle_id = %s, match_status = %s, match_score = %s,
                      rule_version_id = COALESCE(%s, rule_version_id),
                      pending_rereview_reason = NULL, updated_at = now()
               WHERE id = %s""", (vehicle_id, status, confidence, rule_version_id, listing_id))
        if rule_version_id is not None:
            conn.execute("UPDATE vehicles SET rule_version_id = %s, updated_at = now() WHERE id = %s",
                         (rule_version_id, vehicle_id))
        _refresh_vehicle_stats(conn, vehicle_id)


def detach_from_vehicle(listing_id: int) -> int | None:
    with pool.tx() as conn:
        cur = conn.execute("SELECT vehicle_id FROM listings WHERE id = %s", (listing_id,))
        row = cur.fetchone()
        cur.close()
        vid = row.get("vehicle_id") if row else None
        conn.execute("""UPDATE listings SET vehicle_id = NULL, match_status = 'unmatched',
                        pending_rereview_reason = NULL, updated_at = now() WHERE id = %s""",
                     (listing_id,))
        if vid:
            _refresh_vehicle_stats(conn, vid)
        return vid


def _refresh_vehicle_stats(conn, vehicle_id: int) -> None:
    cur = conn.execute(
        """SELECT count(DISTINCT platform) AS platforms, count(*) AS n,
                  bool_and(is_active) AS all_active,
                  min(first_seen_at) AS first_seen, max(last_seen_at) AS last_seen
           FROM listings WHERE vehicle_id = %s""", (vehicle_id,))
    stats = cur.fetchone()
    cur.close()
    if not stats or not stats["n"]:
        return
    # representative fields: most recently seen listing wins for display text
    cur = conn.execute(
        """SELECT title, description, city, mileage_km, brand, series, reg_year, reg_month,
                  vin, vin_tail6, image_phash, listed_at, seller_type, transmission,
                  transfer_count, displacement_ml, price_yuan
           FROM listings WHERE vehicle_id = %s
           ORDER BY is_active DESC, last_seen_at DESC, id DESC LIMIT 1""", (vehicle_id,))
    rep = cur.fetchone()
    cur.close()
    ph = phash_literal(_phash_of(rep)) if rep else None
    # price range across platforms (informative for exports)
    cur = conn.execute(
        """SELECT min(price_yuan) AS lo, max(price_yuan) AS hi, count(DISTINCT price_yuan) AS distinct_prices
           FROM listings WHERE vehicle_id = %s AND is_active""", (vehicle_id,))
    pr = cur.fetchone()
    cur.close()
    conn.execute(
        """UPDATE vehicles SET
             brand = COALESCE(%s, brand), series = COALESCE(%s, series),
             reg_year = COALESCE(%s, reg_year), reg_month = COALESCE(%s, reg_month),
             mileage_km = COALESCE(%s, mileage_km), city = COALESCE(%s, city),
             title_rep = COALESCE(%s, title_rep), description_rep = COALESCE(%s, description_rep),
             win_vin = COALESCE(%s, win_vin), vin_tail6 = COALESCE(%s, vin_tail6),
             seller_type = COALESCE(%s, seller_type), transmission = COALESCE(%s, transmission),
             transfer_count = COALESCE(%s, transfer_count),
             displacement_ml = COALESCE(%s, displacement_ml),
             image_phash = COALESCE(('x'||%s::text)::bit(64), image_phash),
             first_listed_at = LEAST(COALESCE(first_listed_at, %s), %s),
             last_seen_at = GREATEST(COALESCE(last_seen_at, %s), %s),
             platform_count = %s, updated_at = now()
           WHERE id = %s""",
        (rep["brand"], rep["series"], rep["reg_year"], rep["reg_month"], rep["mileage_km"],
         rep["city"], rep["title"], rep["description"], rep["vin"], rep["vin_tail6"],
         rep["seller_type"], rep["transmission"], rep["transfer_count"], rep["displacement_ml"],
         ph, rep["listed_at"], rep["listed_at"], stats["last_seen"], stats["last_seen"],
         stats["platforms"], vehicle_id),
    )
    # orphaned single-listing vehicles are still valid (one platform, one record)
    cur = conn.execute("SELECT count(*) AS n FROM listings WHERE vehicle_id = %s", (vehicle_id,))
    n = cur.fetchone()["n"]
    cur.close()
    if n == 0:
        conn.execute("DELETE FROM vehicles WHERE id = %s", (vehicle_id,))


# ---------------------------------------------------------------------------
# candidate recall
# ---------------------------------------------------------------------------
def recall_candidates(listing: dict[str, Any], spec: RuleSpec | None = None) -> list[dict[str, Any]]:
    """Recall vehicles (as pseudo-listing bundles) from other platforms."""
    spec = spec or _fallback_spec()
    reg_idx = _reg_month_index(listing.get("reg_year"), listing.get("reg_month"))
    rows = pool.query(
        """
        SELECT v.id AS vehicle_id, v.brand, v.series, v.reg_year, v.reg_month, v.mileage_km,
               v.city, v.transmission, v.displacement_ml, v.transfer_count, v.vin_tail6,
               v.description_rep, v.title_rep, v.image_phash,
               (SELECT l.price_yuan FROM listings l WHERE l.vehicle_id = v.id AND l.is_active
                ORDER BY l.last_seen_at DESC LIMIT 1) AS price_yuan,
               (SELECT array_agg(DISTINCT l.platform) FROM listings l WHERE l.vehicle_id = v.id) AS platforms
        FROM vehicles v
        WHERE v.id <> COALESCE(%s, 0)
          AND (%s::int IS NULL OR v.reg_year IS NULL
               OR abs((v.reg_year * 12 + COALESCE(v.reg_month, 6)) - %s::int) <= %s::int)
          AND (%s::text IS NULL OR v.series IS NULL OR v.series = %s::text OR v.series LIKE %s::text)
        ORDER BY v.updated_at DESC
        LIMIT %s::int
        """,
        (listing.get("vehicle_id"),
         reg_idx, reg_idx, spec.recall_reg_months,
         listing.get("series"), listing.get("series"), f"%{listing.get('series') or ''}%",
         spec.candidate_limit),
    )
    out = []
    for r in rows:
        r = dict(r)
        r["description"] = r.pop("description_rep")
        r["title"] = r.pop("title_rep")
        r["image_phash"] = _phash_of(r)
        out.append(r)
    return out


def _recall_is_viable(listing: dict[str, Any], cand: dict[str, Any],
                      spec: RuleSpec | None = None) -> bool:
    spec = spec or _fallback_spec()
    if listing.get("mileage_km") and cand.get("mileage_km"):
        if abs(int(listing["mileage_km"]) - int(cand["mileage_km"])) > spec.recall_mileage_km:
            return False
    if listing.get("city") and cand.get("city") and listing["city"] != cand["city"]:
        # cross-city listings are possible but only keep them as candidates
        pass
    return True


# ---------------------------------------------------------------------------
# learned rules
# ---------------------------------------------------------------------------
def rule_key_for(a: dict[str, Any], b: dict[str, Any]) -> tuple[str, str]:
    """Symmetric rule key from the pair's discriminative fields."""
    keys = []
    for k in ("brand", "series", "city"):
        va, vb = a.get(k), b.get(k)
        parts = sorted([str(va or ""), str(vb or "")])
        keys.append("=".join(parts) if parts[0] != parts[1] else str(va or ""))
    return "|".join(keys), f"{a.get('series')}|{b.get('series')}"


def lookup_rule(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any] | None:
    key1, key2 = rule_key_for(a, b)
    row = pool.query_one(
        """SELECT * FROM match_rules WHERE active AND rule_key IN (%s,%s) ORDER BY confidence DESC LIMIT 1""",
        (key1, key2))
    return row


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------
def run_merge_for_listing(listing: dict[str, Any], *, run_id: str | None = None,
                          rule_version_id: int | None = None) -> dict[str, Any]:
    """Match a single listing against the vehicle pool. Returns a summary dict."""
    # rollback-flagged rows wait for a human; the automated pass must not touch them
    if listing.get("match_status") == "pending_rereview":
        return {"action": "pending_rereview", "listing_id": listing["id"]}
    if listing.get("vehicle_id"):
        return {"action": "already_matched", "vehicle_id": listing["vehicle_id"]}

    from carscout import rules as rules_mod
    version_id = rule_version_id or rules_mod.select_version_id(
        listing["platform"], int(listing["id"]))
    spec = rules_mod.load_spec(version_id)

    cands = recall_candidates(listing, spec)
    if not cands:
        vid = create_vehicle(listing, confidence=1.0, rule_version_id=version_id)
        return {"action": "created_vehicle", "vehicle_id": vid, "rule_version_id": version_id}

    best: tuple[float, dict[str, Any], ScoreBreakdown] | None = None
    for cand in cands:
        if not _recall_is_viable(listing, cand, spec):
            continue
        sb = score_pair(listing, cand, _phash_of(listing), cand.get("image_phash"), spec)
        if sb.hard_conflict == "series_mismatch":
            continue
        if best is None or sb.total > best[0]:
            best = (sb.total, cand, sb)

    if best is None:
        vid = create_vehicle(listing, confidence=1.0, rule_version_id=version_id)
        return {"action": "created_vehicle", "vehicle_id": vid, "rule_version_id": version_id}

    score, cand, sb = best
    vid = int(cand["vehicle_id"])
    rule = lookup_rule(listing, cand)
    if rule and rule.get("decision") == "merge" and sb.hard_conflict is None:
        _record_candidate(listing["id"], _candidate_listing_id(vid), score, sb, "rule_applied",
                          run_id=run_id, rule_id=rule["id"], note=f"rule {rule['rule_key']}",
                          rule_version_id=version_id)
        attach_to_vehicle(listing["id"], vid, confidence=score, status="ruled",
                          rule_version_id=version_id)
        pool.execute("UPDATE match_rules SET applied_count = applied_count + 1, updated_at = now() WHERE id = %s",
                     (rule["id"],))
        return {"action": "merged_by_rule", "vehicle_id": vid, "score": score,
                "rule_version_id": version_id}
    if rule and rule.get("decision") == "split":
        _record_candidate(listing["id"], _candidate_listing_id(vid), score, sb, "rule_applied",
                          run_id=run_id, rule_id=rule["id"], note=f"rule {rule['rule_key']}",
                          rule_version_id=version_id)
        new_vid = create_vehicle(listing, confidence=1.0, rule_version_id=version_id)
        return {"action": "split_by_rule", "vehicle_id": new_vid, "score": score,
                "rule_version_id": version_id}

    if score >= spec.auto_merge_score and sb.hard_conflict is None and sb.required_block is None:
        _record_candidate(listing["id"], _candidate_listing_id(vid), score, sb, "auto_merged",
                          run_id=run_id, rule_version_id=version_id)
        attach_to_vehicle(listing["id"], vid, confidence=score, rule_version_id=version_id)
        return {"action": "merged", "vehicle_id": vid, "score": score,
                "rule_version_id": version_id}

    if score >= spec.gray_zone_score:
        # Grey zone: the pair must not be joined automatically, but leaving the
        # listing fully unattached would make it invisible as a candidate for
        # later listings (two identical unmatched rows could never meet).
        # So: give it its OWN provisional vehicle (marked manual_pending, low
        # confidence) and queue the candidate. A human decision either folds
        # the two vehicles together or confirms they stay apart.
        other_id = _candidate_listing_id(vid)
        cid = _record_candidate(listing["id"], other_id, score, sb, "manual_pending",
                                run_id=run_id, rule_version_id=version_id)
        # Only the newly arriving listing is parked: it gets its own provisional
        # vehicle. The counterpart keeps whatever cluster it already belongs to;
        # the review row names both listing ids, so a human sees the whole pair.
        own_vid = create_vehicle(listing, confidence=score, status="manual_pending",
                                 rule_version_id=version_id)
        pool.execute("""UPDATE listings SET match_status = 'manual_pending', match_score = %s,
                        rule_version_id = %s, updated_at = now() WHERE id = %s""",
                     (score, version_id, listing["id"]))
        return {"action": "queued_for_review", "vehicle_id": own_vid,
                "candidate_vehicle_id": vid, "score": score, "candidate_id": cid,
                "rule_version_id": version_id}

    _record_candidate(listing["id"], _candidate_listing_id(vid), score, sb, "rejected",
                      run_id=run_id, rule_version_id=version_id)
    new_vid = create_vehicle(listing, confidence=1.0, rule_version_id=version_id)
    return {"action": "created_vehicle", "vehicle_id": new_vid, "score": score,
            "rule_version_id": version_id}


def _candidate_listing_id(vehicle_id: int) -> int:
    row = pool.query_one(
        """SELECT id FROM listings WHERE vehicle_id = %s ORDER BY is_active DESC, last_seen_at DESC LIMIT 1""",
        (vehicle_id,))
    return int(row["id"]) if row else 0


def _record_candidate(a: int, b: int, score: float, sb: ScoreBreakdown, state: str, *,
                      run_id: str | None = None, rule_id: int | None = None,
                      note: str | None = None,
                      rule_version_id: int | None = None) -> int:
    lo, hi = min(a, b), max(a, b)
    with pool.tx() as conn:
        cur = conn.execute(
            """INSERT INTO match_candidates
               (run_id, listing_a, listing_b, score, components, state, rule_id, note, created_at,
                rule_version_id, evidence)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, now(), %s, %s::jsonb)
               ON CONFLICT (listing_a, listing_b) DO UPDATE
                 SET score = EXCLUDED.score, components = EXCLUDED.components,
                     state = EXCLUDED.state, note = EXCLUDED.note,
                     rule_version_id = EXCLUDED.rule_version_id, evidence = EXCLUDED.evidence
               RETURNING id""",
            (run_id, lo, hi, score, sb.to_json(), state, rule_id, note,
             rule_version_id, sb.evidence_json()),
        )
        cid = int(cur.fetchone()["id"])
        cur.close()
        if state == "manual_pending":
            batch = run_id[:8] if run_id else "manual"
            conn.execute(
                """INSERT INTO manual_review_queue (candidate_id, batch_id)
                   VALUES (%s, %s) ON CONFLICT (candidate_id, batch_id) DO NOTHING""",
                (cid, batch),
            )
        return cid


def merge_pending_listings(*, run_id: str | None = None, platforms: Iterable[str] | None = None,
                           limit: int = 500) -> dict[str, Any]:
    """Batch pass: match every unmatched active listing."""
    plat = list(platforms) if platforms else None
    rows = pool.query(
        """SELECT * FROM listings
           WHERE is_active AND (vehicle_id IS NULL OR match_status = 'unmatched')
             AND match_status <> 'pending_rereview'
             AND detail_status IN ('ok','skipped_unchanged')
             AND (%s::text[] IS NULL OR platform = ANY(%s::text[]))
           ORDER BY first_seen_at ASC, id ASC LIMIT %s""",
        (plat, plat, limit))
    summary: dict[str, int] = {}
    for row in rows:
        res = run_merge_for_listing(row, run_id=run_id)
        summary[res["action"]] = summary.get(res["action"], 0) + 1
    return summary


def merge_vehicles(target_id: int, source_id: int, *, confidence: float,
                   status: str = "auto") -> None:
    """Fold `source` vehicle's listings into `target` and delete the empty shell."""
    if target_id == source_id:
        return
    with pool.tx() as conn:
        conn.execute(
            """UPDATE listings SET vehicle_id = %s, match_status = %s,
                      match_score = COALESCE(match_score, %s),
                      pending_rereview_reason = NULL, updated_at = now()
               WHERE vehicle_id = %s""", (target_id, status, confidence, source_id))
        conn.execute("DELETE FROM vehicles WHERE id = %s AND NOT EXISTS "
                     "(SELECT 1 FROM listings WHERE vehicle_id = %s)", (source_id, source_id))
        _refresh_vehicle_stats(conn, target_id)


def confirm_review(candidate_id: int, decision: str, *, reviewer: str = "operator",
                   note: str | None = None, learn_rule: bool = True) -> dict[str, Any]:
    """Apply a human decision to a grey-zone pair and persist it as a rule."""
    cand = pool.query_one("SELECT * FROM match_candidates WHERE id = %s", (candidate_id,))
    if not cand:
        return {"ok": False, "error": "candidate not found"}
    a = pool.query_one("SELECT * FROM listings WHERE id = %s", (cand["listing_a"],)) or {}
    b = pool.query_one("SELECT * FROM listings WHERE id = %s", (cand["listing_b"],)) or {}
    if not a or not b:
        return {"ok": False, "error": "listing row missing"}

    va, vb = a.get("vehicle_id"), b.get("vehicle_id")
    if decision == "merge":
        if not va and not vb:
            target = create_vehicle(a)
            vb = None
        elif va and not vb:
            target = va
        elif vb and not va:
            target = vb
        else:
            target = va
        for lid in (a["id"], b["id"]):
            attach_to_vehicle(int(lid), int(target), confidence=float(cand["score"]),
                              status="manual_confirmed")
        if vb and int(vb) != int(target):
            merge_vehicles(int(target), int(vb), confidence=float(cand["score"]),
                           status="manual_confirmed")
        state = "manual_confirmed"
    else:
        state = "rejected"
        if va and vb and int(va) == int(vb):
            # same vehicle: detach b and give it its own shell
            detach_from_vehicle(int(b["id"]))
            nb = pool.query_one("SELECT * FROM listings WHERE id = %s", (b["id"],)) or b
            create_vehicle(nb, confidence=0.0, status="rejected")
        # different vehicles already: confirm they stay apart
        pool.execute(
            """UPDATE listings SET pending_rereview_reason = NULL, updated_at = now()
               WHERE id IN (%s, %s) AND match_status = 'pending_rereview'""",
            (a["id"], b["id"]))

    rule_id = None
    if learn_rule:
        key = rule_key_for(a, b)[1]
        with pool.tx() as conn:
            cur = conn.execute(
                """INSERT INTO match_rules (rule_key, decision, source, confidence, note)
                   VALUES (%s, %s, 'manual', 0.95, %s)
                   ON CONFLICT (rule_key) DO UPDATE
                     SET decision = EXCLUDED.decision, note = EXCLUDED.note,
                         active = TRUE, updated_at = now()
                   RETURNING id""",
                (key, decision, note or f"learned from candidate {candidate_id}"),
            )
            rule_id = int(cur.fetchone()["id"])
            cur.close()
    pool.execute(
        """UPDATE match_candidates SET state = %s, decided_at = now(), decided_by = %s,
           note = %s, rule_id = %s WHERE id = %s""",
        (state, reviewer, note, rule_id, candidate_id))
    pool.execute(
        """UPDATE manual_review_queue SET status = %s, reviewed_at = now(), reviewer = %s,
           note = %s WHERE candidate_id = %s AND status = 'pending'""",
        ("confirmed_merge" if decision == "merge" else "confirmed_split", reviewer, note, candidate_id))
    return {"ok": True, "state": state, "rule_key": rule_key_for(a, b)[1], "rule_id": rule_id}


def rebuild_all_vehicles(*, keep_rules: bool = True) -> dict[str, Any]:
    """Re-cluster every active listing from scratch.

    Learned rules are kept (and therefore re-applied), so a rebuild reproduces
    earlier human decisions without re-asking. Candidate/queue audit rows are
    rebuilt as well; use this after threshold changes, not routinely.
    """
    rebuild_scope = "all"
    with pool.tx() as conn:
        conn.execute("""UPDATE listings SET vehicle_id = NULL, match_status = 'unmatched',
                        rule_version_id = NULL, pending_rereview_reason = NULL WHERE is_active""")
        conn.execute("DELETE FROM match_candidates")
        conn.execute("DELETE FROM manual_review_queue")
        conn.execute("DELETE FROM vehicles")
        if not keep_rules:
            conn.execute("DELETE FROM match_rules")
    summary = merge_pending_listings(limit=100000)
    summary["rebuilt_from"] = rebuild_scope
    return summary


# ---------------------------------------------------------------------------
# evidence-level explanation
# ---------------------------------------------------------------------------
def explain_pair(listing_a_id: int, listing_b_id: int,
                 rule_version_id: int | None = None) -> dict[str, Any]:
    """Explain why two listings are (or are not) the same car, evidence by evidence."""
    from carscout import rules as rules_mod

    a = pool.query_one("SELECT * FROM listings WHERE id = %s", (listing_a_id,))
    b = pool.query_one("SELECT * FROM listings WHERE id = %s", (listing_b_id,))
    if not a or not b:
        raise ValueError("listing not found")
    version_id = rule_version_id or rules_mod.active_version_id()
    version = rules_mod.get_version(version_id)
    spec = rules_mod.load_spec(version_id)
    sb = score_pair(dict(a), dict(b), _phash_of(a), _phash_of(b), spec)

    if sb.hard_conflict:
        decision = "reject"
    elif sb.required_block is not None:
        decision = "gray"  # necessary evidence missing: human review required
    elif sb.total >= spec.auto_merge_score:
        decision = "auto_merge"
    elif sb.total >= spec.gray_zone_score:
        decision = "manual_review"
    else:
        decision = "separate"

    existing = pool.query_one(
        """SELECT id, state, score FROM match_candidates
           WHERE listing_a = %s AND listing_b = %s""",
        (min(listing_a_id, listing_b_id), max(listing_a_id, listing_b_id)))
    return {
        "pair": {
            "listing_a": {"id": a["id"], "platform": a["platform"],
                          "platform_listing_id": a["platform_listing_id"], "title": a["title"],
                          "vin_tail6": a["vin_tail6"], "city": a["city"],
                          "mileage_km": a["mileage_km"]},
            "listing_b": {"id": b["id"], "platform": b["platform"],
                          "platform_listing_id": b["platform_listing_id"], "title": b["title"],
                          "vin_tail6": b["vin_tail6"], "city": b["city"],
                          "mileage_km": b["mileage_km"]},
        },
        "rule_version": {"id": version_id, "version": version["version"] if version else None,
                         "name": version["name"] if version else None,
                         "thresholds": {"auto_merge": spec.auto_merge_score,
                                        "gray_zone": spec.gray_zone_score},
                         "required_evidence": list(spec.required_evidence)},
        "score": sb.total,
        "decision": decision,
        "hard_conflict": sb.hard_conflict,
        "required_block": sb.required_block,
        "gates": {"capped": "capped_by_gate" in sb.components,
                  "image_conflict": sb.components.get("gate_image_conflict") == 1.0,
                  "text_disagreement": sb.components.get("gate_text_disagreement") == 1.0},
        "evidence": sb.evidence,
        "components": sb.components,
        "existing_candidate": existing,
    }


# ---------------------------------------------------------------------------
# offline replay support: in-memory re-clustering under an arbitrary RuleSpec
# ---------------------------------------------------------------------------
def simulate_clustering(spec: RuleSpec) -> dict[str, Any]:
    """Re-cluster every active listing in memory under `spec`.

    Read-only against listings/match_rules; writes nothing. Returns the
    partition (listing_id -> cluster_id), clusters with their members, and
    every evaluated pair with its decision/score/evidence, which the replay
    report diffs against the baseline.
    """
    rows = pool.query(
        """SELECT * FROM listings
           WHERE is_active AND detail_status IN ('ok','skipped_unchanged')
           ORDER BY first_seen_at ASC, id ASC""")
    clusters: dict[int, dict[str, Any]] = {}
    partition: dict[int, int] = {}
    pair_decisions: dict[tuple[int, int], dict[str, Any]] = {}
    next_cid = 0

    for raw in rows:
        listing = dict(raw)
        listing["image_phash"] = _phash_of(listing)
        # recall: same-series vehicles inside the registration window
        cands: list[dict[str, Any]] = []
        reg_idx = _reg_month_index(listing.get("reg_year"), listing.get("reg_month"))
        for cid, cl in clusters.items():
            rep = cl["rep"]
            if listing.get("series") and rep.get("series") and \
                    series_similarity(f"{listing.get('brand') or ''}{listing.get('series') or ''}",
                                      f"{rep.get('brand') or ''}{rep.get('series') or ''}") \
                    < spec.series_min_similarity:
                continue
            rep_idx = _reg_month_index(rep.get("reg_year"), rep.get("reg_month"))
            if reg_idx is not None and rep_idx is not None \
                    and abs(reg_idx - rep_idx) > spec.recall_reg_months:
                continue
            cands.append((cid, rep))
            if len(cands) >= spec.candidate_limit:
                break

        best: tuple[float, int, dict[str, Any], ScoreBreakdown] | None = None
        for cid, rep in cands:
            if not _recall_is_viable(listing, rep, spec):
                continue
            sb = score_pair(listing, rep, listing.get("image_phash"),
                            rep.get("image_phash"), spec)
            if sb.hard_conflict == "series_mismatch":
                continue
            if best is None or sb.total > best[0]:
                best = (sb.total, cid, rep, sb)

        if best is None:
            next_cid += 1
            clusters[next_cid] = {"rep": listing, "members": {listing["id"]}}
            partition[listing["id"]] = next_cid
            continue

        score, cid, rep, sb = best
        key = tuple(sorted((listing["id"], rep["id"])))
        rule = lookup_rule(listing, rep)
        if rule and rule.get("decision") == "merge" and sb.hard_conflict is None:
            decision = "merge"
        elif rule and rule.get("decision") == "split":
            decision = "reject"
        elif score >= spec.auto_merge_score and sb.hard_conflict is None \
                and sb.required_block is None:
            decision = "merge"
        elif score >= spec.gray_zone_score:
            decision = "gray"
        else:
            decision = "reject"
        pair_decisions[key] = {"decision": decision, "score": score,
                               "evidence": sb.evidence, "required_block": sb.required_block}

        if decision == "merge":
            clusters[cid]["members"].add(listing["id"])
            partition[listing["id"]] = cid
            # representative follows the newest listing, mirroring _refresh_vehicle_stats
            clusters[cid]["rep"] = listing
        else:
            # gray or reject: the listing gets its own cluster (the pair itself
            # is recorded above so the replay diff can see the disagreement)
            next_cid += 1
            clusters[next_cid] = {"rep": listing, "members": {listing["id"]}}
            partition[listing["id"]] = next_cid

    return {
        "clusters": [{"id": cid, "members": sorted(cl["members"])}
                     for cid, cl in sorted(clusters.items())],
        "partition": partition,
        "pair_decisions": pair_decisions,
        "listings_evaluated": len(rows),
    }
