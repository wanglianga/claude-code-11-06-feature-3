"""Listing persistence: upsert, price history, image pHash enrichment, soft-deletes."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from carscout.db import pool
from carscout.parsers import ParsedListing
from carscout.utils import content_fingerprint, hash_to_bit_literal, normalize_url, now_utc


def _hash_listing(p: ParsedListing) -> str:
    return content_fingerprint("detail", normalize_url(p.source_url), {
        "title": p.title, "mileage": p.mileage_km, "reg": [p.reg_year, p.reg_month],
        "price": p.price_yuan, "desc": p.description, "vin": p.vin,
    })


def upsert_listing(p: ParsedListing, *, run_id: str | None = None,
                   list_fields: dict[str, Any] | None = None,
                   detail_status: str = "ok") -> dict[str, Any]:
    """Insert or update one platform listing. Returns the listing row.

    Price moves are appended to listing_price_events only when the value changes.
    """
    price = p.price_yuan
    url_norm = normalize_url(p.source_url)
    content_hash = _hash_listing(p)
    with pool.tx() as conn:
        cur = conn.execute(
            """SELECT * FROM listings WHERE platform = %s AND platform_listing_id = %s""",
            (p.platform, p.platform_listing_id),
        )
        existing = cur.fetchone()
        cur.close()
        if existing is None:
            cur = conn.execute(
                """INSERT INTO listings
                   (platform, platform_listing_id, source_url, url_norm, title, brand, series,
                    reg_year, reg_month, mileage_km, displacement_ml, transmission, transfer_count,
                    city, price_yuan, listed_at, seller_type, description, vin, vin_tail6,
                    image_urls, content_hash, detail_status, list_seen_at, detail_fetched_at,
                    first_seen_at, last_seen_at, price_observed_at, is_active, raw_list, raw_detail,
                    extra)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s::jsonb,%s,%s,now(),now(),now(),now(),now(),TRUE,%s::jsonb,%s::jsonb,%s::jsonb)
                   RETURNING *""",
                (p.platform, p.platform_listing_id, p.source_url, url_norm, p.title, p.brand,
                 p.series, p.reg_year, p.reg_month, p.mileage_km, p.displacement_ml,
                 p.transmission, p.transfer_count, p.city, price, p.listed_at, p.seller_type,
                 p.description, p.vin, p.vin_tail6, json.dumps(p.image_urls, ensure_ascii=False),
                 content_hash, detail_status, json.dumps(list_fields or {}, ensure_ascii=False, default=str),
                 json.dumps(p.raw, ensure_ascii=False, default=str),
                 json.dumps({"model": p.model}, ensure_ascii=False)),
            )
            row = cur.fetchone()
            cur.close()
        else:
            row = existing
            price_changed = price is not None and price != existing.get("price_yuan")
            keep_price = price if price is not None else existing.get("price_yuan")
            cur = conn.execute(
                """UPDATE listings SET
                     source_url = %s, url_norm = %s, title = %s, brand = COALESCE(%s, brand),
                     series = COALESCE(%s, series),
                     reg_year = COALESCE(%s, reg_year), reg_month = COALESCE(%s, reg_month),
                     mileage_km = COALESCE(%s, mileage_km),
                     displacement_ml = COALESCE(%s, displacement_ml),
                     transmission = COALESCE(%s, transmission),
                     transfer_count = COALESCE(%s, transfer_count),
                     city = COALESCE(%s, city), price_yuan = %s, listed_at = COALESCE(%s, listed_at),
                     seller_type = COALESCE(%s, seller_type), description = COALESCE(%s, description),
                     vin = COALESCE(%s, vin), vin_tail6 = COALESCE(%s, vin_tail6),
                     image_urls = CASE WHEN %s::jsonb = '[]'::jsonb THEN image_urls ELSE %s::jsonb END,
                     content_hash = %s, detail_status = %s, detail_fetched_at = now(),
                     last_seen_at = now(), is_active = TRUE, removed_at = NULL,
                     price_observed_at = CASE WHEN %s THEN now() ELSE price_observed_at END,
                     raw_list = COALESCE(NULLIF(%s::jsonb, '{}'::jsonb), raw_list),
                     raw_detail = %s::jsonb,
                     extra = extra || %s::jsonb, updated_at = now()
                   WHERE platform = %s AND platform_listing_id = %s
                   RETURNING *""",
                (p.source_url, url_norm, p.title, p.brand, p.series, p.reg_year, p.reg_month,
                 p.mileage_km, p.displacement_ml, p.transmission, p.transfer_count, p.city,
                 keep_price, p.listed_at, p.seller_type, p.description, p.vin, p.vin_tail6,
                 json.dumps(p.image_urls), json.dumps(p.image_urls), content_hash, detail_status,
                 bool(price_changed), json.dumps(list_fields or {}, ensure_ascii=False, default=str),
                 json.dumps(p.raw, ensure_ascii=False, default=str),
                 json.dumps({"model": p.model}, ensure_ascii=False),
                 p.platform, p.platform_listing_id),
            )
            row = cur.fetchone()
            cur.close()
            if price_changed:
                conn.execute(
                    """INSERT INTO listing_price_events
                       (listing_id, price_yuan, previous_price, delta_yuan, run_id, observed_at)
                       VALUES (%s, %s, %s, %s, %s, now())
                       ON CONFLICT (listing_id, observed_at, price_yuan) DO NOTHING""",
                    (row["id"], price, existing.get("price_yuan"),
                     (price - existing["price_yuan"]) if existing.get("price_yuan") else None, run_id),
                )
            elif existing.get("price_yuan") is None and price is not None:
                # first successful price observation for a previously price-less row
                conn.execute(
                    """INSERT INTO listing_price_events
                       (listing_id, price_yuan, previous_price, delta_yuan, run_id, observed_at)
                       VALUES (%s, %s, NULL, NULL, %s, now())
                       ON CONFLICT (listing_id, observed_at, price_yuan) DO NOTHING""",
                    (row["id"], price, run_id),
                )
        return row or {}


def touch_list_only(item: dict[str, Any], *, run_id: str | None = None) -> dict[str, Any]:
    """Upsert a list-page observation that has no detail extraction yet.

    A listing first appears in the list; detail fetch is a separate request stage.
    """
    platform = item["platform"]
    pid = item["platform_listing_id"]
    with pool.tx() as conn:
        cur = conn.execute(
            """SELECT * FROM listings WHERE platform = %s AND platform_listing_id = %s""",
            (platform, pid),
        )
        existing = cur.fetchone()
        cur.close()
        raw = item.get("raw") or {}
        if existing is None:
            cur = conn.execute(
                """INSERT INTO listings
                   (platform, platform_listing_id, source_url, url_norm, title, city,
                    price_yuan, mileage_km, reg_year, reg_month, vin_tail6, image_urls,
                    detail_status, list_seen_at, last_seen_at, is_active, raw_list, extra)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'pending',now(),now(),TRUE,
                           %s::jsonb, '{}'::jsonb)
                   ON CONFLICT (platform, platform_listing_id) DO UPDATE
                     SET list_seen_at = now(), last_seen_at = now(), is_active = TRUE,
                         removed_at = NULL, updated_at = now()
                   RETURNING *""",
                (platform, pid, item["url"], normalize_url(item["url"]),
                 item.get("title"), item.get("city"), item.get("price_yuan"),
                 item.get("mileage_km"), item.get("reg_year"), item.get("reg_month"),
                 item.get("vin_tail6"),
                 json.dumps([item["image"]] if item.get("image") else []),
                 json.dumps(raw, ensure_ascii=False, default=str)),
            )
            row = cur.fetchone()
            cur.close()
            if row and item.get("price_yuan") is not None:
                conn.execute(
                    """INSERT INTO listing_price_events
                       (listing_id, price_yuan, previous_price, delta_yuan, run_id, observed_at)
                       VALUES (%s, %s, NULL, NULL, %s, now())
                       ON CONFLICT (listing_id, observed_at, price_yuan) DO NOTHING""",
                    (row["id"], item["price_yuan"], run_id),
                )
        else:
            row = existing
            price_changed = item.get("price_yuan") is not None and item.get("price_yuan") != existing.get("price_yuan")
            cur = conn.execute(
                """UPDATE listings SET
                     title = COALESCE(%s, title), city = COALESCE(%s, city),
                     price_yuan = COALESCE(%s, price_yuan),
                     mileage_km = COALESCE(%s, mileage_km),
                     reg_year = COALESCE(%s, reg_year), reg_month = COALESCE(%s, reg_month),
                     vin_tail6 = COALESCE(%s, vin_tail6),
                     image_urls = CASE WHEN image_urls = '[]'::jsonb AND %s::jsonb <> '[]'::jsonb
                                       THEN %s::jsonb ELSE image_urls END,
                     list_seen_at = now(), last_seen_at = now(), is_active = TRUE,
                     removed_at = NULL,
                     price_observed_at = CASE WHEN %s THEN now() ELSE price_observed_at END,
                     raw_list = %s::jsonb, updated_at = now()
                   WHERE platform = %s AND platform_listing_id = %s
                   RETURNING *""",
                (item.get("title"), item.get("city"), item.get("price_yuan"),
                 item.get("mileage_km"), item.get("reg_year"), item.get("reg_month"),
                 item.get("vin_tail6"),
                 json.dumps([item["image"]] if item.get("image") else []),
                 json.dumps([item["image"]] if item.get("image") else []),
                 bool(price_changed),
                 json.dumps(raw, ensure_ascii=False, default=str),
                 platform, pid),
            )
            row = cur.fetchone()
            cur.close()
            if price_changed:
                conn.execute(
                    """INSERT INTO listing_price_events
                       (listing_id, price_yuan, previous_price, delta_yuan, run_id, observed_at)
                       VALUES (%s, %s, %s, %s, %s, now())
                       ON CONFLICT (listing_id, observed_at, price_yuan) DO NOTHING""",
                    (row["id"], item["price_yuan"], existing.get("price_yuan"),
                     (item["price_yuan"] - existing["price_yuan"]) if existing.get("price_yuan") else None,
                     run_id),
                )
        return row or {}


def mark_pending_detail(platform: str, pid: str) -> None:
    pool.execute(
        """UPDATE listings SET detail_status = CASE WHEN detail_status IN ('ok','skipped_unchanged')
                                    THEN detail_status ELSE 'pending' END
           WHERE platform = %s AND platform_listing_id = %s""", (platform, pid))


def mark_detail_problem(platform: str, pid: str, status: str) -> None:
    pool.execute(
        """UPDATE listings SET detail_status = %s, updated_at = now()
           WHERE platform = %s AND platform_listing_id = %s""", (status, platform, pid))


def deactivate_unseen(platform: str, run_started: datetime, *, scope: str = "all") -> int:
    """Soft-delete listings no longer visible on the platform after a full sweep."""
    n = pool.execute(
        """UPDATE listings
           SET is_active = FALSE, removed_at = COALESCE(removed_at, now()), updated_at = now()
           WHERE platform = %s AND is_active = TRUE
             AND (list_seen_at IS NULL OR list_seen_at < %s)
             AND first_seen_at < %s""",
        (platform, run_started, run_started),
    )
    return n


def save_image_hashes(listing_id: int, hashes: list[tuple[str, str, int, int]]) -> None:
    """hashes: [(url, phash_hex, width, height)] — one row per distinct image."""
    with pool.tx() as conn:
        for url, ph_hex, w, h in hashes:
            if not url or not ph_hex:
                continue
            try:
                ph_bit = hash_to_bit_literal(ph_hex)
            except Exception:
                continue
            conn.execute(
                """INSERT INTO image_hashes (listing_id, image_url, phash, width, height)
                   VALUES (%s, %s, %s::bit(64), %s, %s)
                   ON CONFLICT (listing_id, image_url)
                   DO UPDATE SET phash = EXCLUDED.phash, fetched_at = now()""",
                (listing_id, url, ph_bit, w, h),
            )


def set_primary_phash(listing_id: int, phash: str) -> None:
    pool.execute(
        """UPDATE listings SET image_phash = %s::bit(64), updated_at = now() WHERE id = %s""",
        (hash_to_bit_literal(phash), listing_id),
    )


def set_pending_detail_state(platform: str, pid: str, state: str) -> None:
    mark_detail_problem(platform, pid, state)


def listing_stats(platform: str) -> dict[str, Any]:
    row = pool.query_one(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE is_active) AS active,
                  count(*) FILTER (WHERE detail_status = 'ok') AS ok,
                  count(*) FILTER (WHERE detail_status = 'pending') AS pending
           FROM listings WHERE platform = %s""", (platform.upper()))
    return row or {}
