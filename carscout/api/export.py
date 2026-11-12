"""Export & reporting queries: 30-day price distribution, vehicle dossiers, review CSV."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from carscout.db import pool
from carscout.utils import price_median


def _day_window(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    return end - timedelta(days=days), end


# ---------------------------------------------------------------------------
# 近 30 天按车系的挂牌 / 报价分布
# ---------------------------------------------------------------------------
def series_price_distribution(*, series: str | None = None, brand: str | None = None,
                              days: int = 30, platforms: Iterable[str] | None = None) -> dict[str, Any]:
    start, end = _day_window(days)
    plat = list(platforms) if platforms else None
    rows = pool.query(
        """
        SELECT l.platform, v.brand, v.series,
               count(*) FILTER (WHERE l.is_active)                    AS active_listings,
               count(*)                                              AS listings_seen,
               count(DISTINCT v.id)                                   AS vehicles,
               min(l.price_yuan)                                      AS price_min,
               max(l.price_yuan)                                      AS price_max,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY l.price_yuan) AS price_median,
               min(l.listed_at)                                       AS earliest_listed,
               max(l.listed_at)                                       AS latest_listed
        FROM listings l
        JOIN vehicles v ON v.id = l.vehicle_id
        WHERE l.price_yuan IS NOT NULL
          AND (l.listed_at IS NULL OR l.listed_at <= %s)
          AND l.last_seen_at >= %s
          AND (%s::text IS NULL OR v.series = %s::text)
          AND (%s::text IS NULL OR v.brand = %s::text)
          AND (%s::text[] IS NULL OR l.platform = ANY(%s::text[]))
        GROUP BY l.platform, v.brand, v.series
        ORDER BY v.brand, v.series, l.platform
        """,
        (end, start, series, series, brand, brand, plat, plat),
    )
    rows = [dict(r) for r in rows]
    for r in rows:
        if r.get("price_median") is not None:
            r["price_median"] = float(r["price_median"])
    # cross-platform view
    cross = pool.query(
        """
        SELECT v.id AS vehicle_id, v.brand, v.series, v.title_rep, v.platform_count,
               min(l.price_yuan) AS price_min, max(l.price_yuan) AS price_max,
               count(DISTINCT l.platform) AS platforms
        FROM vehicles v JOIN listings l ON l.vehicle_id = v.id AND l.is_active
        WHERE (l.listed_at IS NULL OR l.listed_at <= %s) AND l.last_seen_at >= %s
          AND (%s::text IS NULL OR v.series = %s::text)
          AND (%s::text IS NULL OR v.brand = %s::text)
        GROUP BY v.id, v.brand, v.series, v.title_rep, v.platform_count
        ORDER BY v.brand, v.series, v.id
        """,
        (end, start, series, series, brand, brand),
    )
    return {
        "window_days": days,
        "since": start.isoformat(),
        "filters": {"series": series, "brand": brand, "platforms": plat},
        "by_platform": rows,
        "by_vehicle": [dict(r) for r in cross],
    }


def series_summary(*, series: str | None = None, days: int = 30) -> list[dict[str, Any]]:
    """Coarser roll-up: one row per (brand, series, platform)."""
    dist = series_price_distribution(series=series, days=days)
    out = []
    for r in dist["by_platform"]:
        med = r["price_median"]
        out.append({
            "brand": r["brand"], "series": r["series"], "platform": r["platform"],
            "active_listings": r["active_listings"], "vehicles": r["vehicles"],
            "price_min": r["price_min"], "price_max": r["price_max"],
            "price_median": int(med) if med is not None else None,
            "price_spread": (r["price_max"] - r["price_min"]) if r["price_min"] and r["price_max"] else None,
            "earliest_listed": r["earliest_listed"], "latest_listed": r["latest_listed"],
        })
    return out


# ---------------------------------------------------------------------------
# 单车档案: 几个平台挂过 / 各挂多少 / 降过几次价 / 首次挂牌距今
# ---------------------------------------------------------------------------
def vehicle_dossier(vehicle_id: int) -> dict[str, Any] | None:
    v = pool.query_one("SELECT * FROM vehicles WHERE id = %s", (vehicle_id,))
    if not v:
        return None
    listings = pool.query(
        """SELECT id, platform, platform_listing_id, source_url, title, price_yuan, listed_at,
                  first_seen_at, last_seen_at, is_active, removed_at, detail_status, match_status,
                  match_score, vin, city, mileage_km, description
           FROM listings WHERE vehicle_id = %s ORDER BY platform, id""", (vehicle_id,))
    rows = []
    for l in listings:
        events = pool.query(
            """SELECT price_yuan, previous_price, delta_yuan, observed_at
               FROM listing_price_events WHERE listing_id = %s ORDER BY observed_at""", (l["id"],))
        price_changes = [e for e in events if e["previous_price"] is not None
                         and e["delta_yuan"] is not None and e["delta_yuan"] < 0]
        rows.append({
            **dict(l),
            "price_history": [dict(e) for e in events],
            "price_cuts": len(price_changes),
            "current_price": l["price_yuan"],
            "listed_at": l["listed_at"],
            "days_on_market": (datetime.now(timezone.utc) - (l["listed_at"] or l["first_seen_at"])).days
            if (l["listed_at"] or l["first_seen_at"]) else None,
        })
    priced = [r["price_yuan"] for r in rows if r["price_yuan"]]
    # "首次挂牌" is the platform's own listing date; first_seen_at is only the
    # fallback for platforms that do not expose a publish time.
    listed_dates = [r["listed_at"] for r in rows if r["listed_at"]]
    first_listed = min(listed_dates) if listed_dates else None
    if first_listed is None:
        first_listed = min((r["first_seen_at"] for r in rows if r["first_seen_at"]), default=None)
    first_observed = min((r["first_seen_at"] for r in rows if r["first_seen_at"]), default=None)
    dossier = {
        "vehicle": dict(v),
        "platforms": sorted({r["platform"] for r in rows}),
        "platform_count": len({r["platform"] for r in rows}),
        "listing_count": len(rows),
        "price_min": min(priced) if priced else None,
        "price_max": max(priced) if priced else None,
        "price_median": price_median(priced),
        "total_price_cuts": sum(r["price_cuts"] for r in rows),
        "first_listed_at": first_listed,
        "days_since_first_listing": (datetime.now(timezone.utc) - first_listed).days if first_listed else None,
        "first_observed_at": first_observed,
        "days_since_first_observed": (datetime.now(timezone.utc) - first_observed).days
        if first_observed else None,
        "listings": rows,
    }
    if v.get("image_phash") is not None:
        dossier["vehicle"]["image_phash"] = str(v["image_phash"])
    return dossier


# ---------------------------------------------------------------------------
# 人工确认队列: CSV 导出 / 回传
# ---------------------------------------------------------------------------
REVIEW_CSV_FIELDS = [
    "candidate_id", "listing_a", "listing_b", "score", "city_a", "city_b",
    "mileage_a", "mileage_b", "reg_a", "reg_b", "price_a", "price_b",
    "phash_distance", "text_similarity", "decision", "note",
]


def export_review_csv(*, batch_id: str | None = None, status: str = "pending",
                      min_score: float | None = None) -> str:
    rows = pool.query(
        """
        SELECT c.id AS candidate_id, c.score, c.components,
               a.id AS listing_a_id, a.platform AS platform_a, a.platform_listing_id AS pid_a,
               a.city AS city_a, a.mileage_km AS mileage_a, a.reg_year AS ry_a, a.reg_month AS rm_a,
               a.price_yuan AS price_a,
               b.id AS listing_b_id, b.platform AS platform_b, b.platform_listing_id AS pid_b,
               b.city AS city_b, b.mileage_km AS mileage_b, b.reg_year AS ry_b, b.reg_month AS rm_b,
               b.price_yuan AS price_b,
               q.batch_id, q.status
        FROM match_candidates c
        JOIN listings a ON a.id = c.listing_a
        JOIN listings b ON b.id = c.listing_b
        LEFT JOIN LATERAL (
            SELECT batch_id, status FROM manual_review_queue mq
            WHERE mq.candidate_id = c.id ORDER BY mq.id DESC LIMIT 1
        ) q ON TRUE
        WHERE c.state IN ('manual_pending', 'auto_merged', 'rule_applied')
          AND q.batch_id IS NOT NULL
          AND (%s::text IS NULL OR q.batch_id = %s::text)
          AND (%s::text IS NULL OR q.status = %s::text)
          AND (%s::float IS NULL OR c.score >= %s::float)
        ORDER BY c.score DESC, c.id
        """,
        (batch_id, batch_id, status, status, min_score, min_score),
    )
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=REVIEW_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        comps = r.get("components") or {}
        c = comps.get("components") if isinstance(comps, dict) else {}
        writer.writerow({
            "candidate_id": r["candidate_id"],
            "listing_a": f"{r['platform_a']}:{r['pid_a']}",
            "listing_b": f"{r['platform_b']}:{r['pid_b']}",
            "score": r["score"],
            "city_a": r["city_a"], "city_b": r["city_b"],
            "mileage_a": r["mileage_a"], "mileage_b": r["mileage_b"],
            "reg_a": f"{r['ry_a']}-{r['rm_a']:02d}" if r["ry_a"] else "",
            "reg_b": f"{r['ry_b']}-{r['rm_b']:02d}" if r["ry_b"] else "",
            "price_a": r["price_a"], "price_b": r["price_b"],
            "phash_distance": (c or {}).get("phash_hamming", ""),
            "text_similarity": (c or {}).get("text_raw", ""),
            "decision": "", "note": "",
        })
    return buf.getvalue()


def import_review_csv(csv_text: str, *, reviewer: str = "operator",
                      learn_rules: bool = True) -> dict[str, Any]:
    """Apply decisions from a returned CSV.

    Each row must carry `candidate_id` and a `decision` of merge|split.
    Unknown ids and blank decisions are skipped and reported.
    """
    from carscout.matching import confirm_review

    reader = csv.DictReader(io.StringIO(csv_text))
    applied, skipped, errors = [], [], []
    for idx, row in enumerate(reader, start=2):
        cid = (row.get("candidate_id") or "").strip()
        decision = (row.get("decision") or "").strip().lower()
        note = (row.get("note") or "").strip()
        if not cid or not decision:
            skipped.append({"line": idx, "reason": "missing candidate_id or decision"})
            continue
        if decision not in ("merge", "split"):
            errors.append({"line": idx, "candidate_id": cid, "reason": f"unknown decision {decision!r}"})
            continue
        try:
            res = confirm_review(int(cid), decision, reviewer=reviewer, note=note or None,
                                 learn_rule=learn_rules)
        except Exception as exc:  # noqa: BLE001
            errors.append({"line": idx, "candidate_id": cid, "reason": repr(exc)})
            continue
        (applied if res.get("ok") else errors).append({"line": idx, "candidate_id": cid, "result": res})
    return {"applied": len(applied), "skipped": len(skipped), "errors": len(errors),
            "details": {"applied": applied, "skipped": skipped, "errors": errors}}


# ---------------------------------------------------------------------------
# ops overview
# ---------------------------------------------------------------------------
def overview() -> dict[str, Any]:
    platforms = pool.query("SELECT code, name, enabled FROM platforms ORDER BY code")
    health = pool.query("SELECT * FROM platform_health ORDER BY platform")
    counts = pool.query(
        """SELECT platform, count(*) FILTER (WHERE is_active) AS active,
                  count(*) FILTER (WHERE detail_status = 'ok') AS ok,
                  count(*) FILTER (WHERE detail_status = 'pending') AS pending,
                  count(*) FILTER (WHERE detail_status = 'blocked') AS blocked,
                  count(*) FILTER (WHERE detail_status = 'schema_drift') AS schema_drift
           FROM listings GROUP BY platform ORDER BY platform""")
    vehicles = pool.query_one(
        """SELECT count(*) AS n,
                  count(*) FILTER (WHERE EXISTS (
                      SELECT 1 FROM listings l WHERE l.vehicle_id = v.id
                        AND l.match_status IN ('manual_pending', 'rejected', 'pending_rereview'))) AS provisional,
                  count(*) FILTER (WHERE NOT EXISTS (
                      SELECT 1 FROM listings l WHERE l.vehicle_id = v.id
                        AND l.match_status IN ('manual_pending', 'rejected', 'pending_rereview'))) AS confirmed
           FROM vehicles v""") or {}
    review = pool.query_one(
        "SELECT count(*) AS pending FROM manual_review_queue WHERE status = 'pending'") or {}
    rereview = pool.query_one(
        "SELECT count(*) AS n FROM listings WHERE match_status = 'pending_rereview'") or {}
    from carscout import rules as rules_mod

    active_rule = rules_mod.active_version_row()
    anomalies = pool.query(
        """SELECT platform, category, count(*) AS n FROM ingest_anomalies
           WHERE created_at > now() - interval '7 days' GROUP BY platform, category
           ORDER BY platform, n DESC""")
    runs = pool.query(
        """SELECT platform, status, count(*) AS n FROM crawl_runs
           WHERE started_at > now() - interval '7 days' GROUP BY platform, status
           ORDER BY platform""")
    cursors = pool.query("SELECT platform, scope, completed, items_seen, cursor, updated_at FROM crawl_cursors")
    for r in cursors:
        if isinstance(r.get("updated_at"), datetime):
            r["updated_at"] = r["updated_at"].isoformat()
    for r in health:
        for k in ("opened_at", "cooldown_until", "last_success_at", "updated_at"):
            if isinstance(r.get(k), datetime):
                r[k] = r[k].isoformat()
    return {
        "platforms": platforms, "health": health, "listing_counts": counts,
        "vehicles": vehicles.get("n"),
        "confirmed_vehicles": vehicles.get("confirmed"),
        "provisional_vehicles": vehicles.get("provisional"),
        "pending_reviews": review.get("pending"),
        "pending_rereview": rereview.get("n", 0),
        "active_rule_version": (
            {"id": active_rule["id"], "version": active_rule["version"],
             "name": active_rule["name"], "status": active_rule["status"],
             "rollouts": rules_mod.get_rollout(int(active_rule["id"]))}
            if active_rule else None),
        "recent_anomalies": anomalies, "recent_runs": runs, "cursors": cursors,
    }
