"""Per-platform request construction + parse glue.

Each handler translates the platform's transport quirks into a uniform
`ListPage` / `DetailOutcome`. Transport specifics:

  A  plain Scrapy HTTP GET, static HTML, page param
  B  Playwright page; scroll loop drives XHR list calls inside the browser;
     detail also fetched via in-page fetch (same cookie/session as the page)
  C  plain HTTP GET but the site silently throttles with 200 + empty body
  D  Playwright page that boots the App shell; signed XHR calls are executed
     inside the page context so they reuse the app session and signature secret
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import scrapy
from scrapy_playwright.page import PageMethod

from carscout import parsers
from carscout.config import settings
from carscout.parsers import Anomaly, ParsedListing
from carscout.parsers import parse_mileage_km, parse_price_yuan, parse_reg_ym
from carscout.utils import normalize_url

BASE = None  # resolved lazily from settings (tests override fixture_base_url)
PAGE_SIZE = {"A": 6, "B": 8, "C": 10, "D": 10}


def base_url() -> str:
    return (settings.fixture_base_url or BASE or "").rstrip("/")


def platform_path(code: str) -> str:
    """Path prefix for a platform; the DB row is authoritative when reachable."""
    from carscout.db import pool

    try:
        row = pool.query_one("SELECT base_url FROM platforms WHERE code = %s", (code.upper(),))
    except Exception:  # database not reachable (pure parser tests)
        row = None
    suffix = {"A": "/pa", "B": "/pb", "C": "/pc", "D": "/pd"}[code.upper()]
    db_base = (row or {}).get("base_url") or ""
    if db_base.startswith("http"):
        # an operator may pin a full URL for a real platform
        return db_base.rstrip("/")
    if db_base.startswith("/"):
        suffix = db_base.rstrip("/")
    return base_url() + suffix


@dataclass
class ListPage:
    items: list[dict[str, Any]] = field(default_factory=list)
    anomaly: Anomaly | None = None
    has_more: bool = False
    total: int | None = None
    raw: Any = None
    kind: str = "html"
    content_type: str = "text/html"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def category(self) -> str:
        if self.anomaly is None:
            return "ok"
        if self.anomaly.category == "empty" or self.anomaly.category == "empty_confirmed":
            return "empty_confirmed"
        return self.anomaly.category


@dataclass
class DetailOutcome:
    parsed: ParsedListing | None = None
    anomaly: Anomaly | None = None
    kind: str = "html"
    content_type: str = "text/html"
    raw: Any = None

    @property
    def missing(self) -> bool:
        return bool(self.anomaly and self.anomaly.category == "missing")

    @property
    def category(self) -> str:
        if self.anomaly is None:
            return "ok"
        return self.anomaly.category


def _norm_item(platform: str, *, pid: str, url: str, title: str | None, price_yuan: int | None,
               mileage_km: int | None, city: str | None, image: str | None,
               vin_tail6: str | None, raw: dict[str, Any],
               reg: tuple[int | None, int | None] = (None, None)) -> dict[str, Any]:
    from carscout.parsers import parse_brand_series_from_title

    brand, series = parse_brand_series_from_title(title)
    ry, rm = reg
    if ry is None:
        ry, rm = parse_reg_ym(title)
    return {
        "platform": platform,
        "platform_listing_id": pid,
        "url": url,
        "title": title,
        "price_yuan": price_yuan,
        "mileage_km": mileage_km,
        "reg_year": ry,
        "reg_month": rm,
        "city": city,
        "vin_tail6": vin_tail6,
        "image": image,
        "brand": brand,
        "series": series,
        "raw": raw,
        "url_norm": normalize_url(url),
    }


def list_fingerprint(item: dict[str, Any]) -> str:
    """Fingerprint of list-visible fields; unchanged means the detail page is
    almost certainly unchanged, so an incremental run can skip the detail GET."""
    from carscout.utils import content_fingerprint

    return content_fingerprint("list", item.get("url_norm") or normalize_url(item["url"]), {
        "title": item.get("title"), "price": item.get("price_yuan"),
        "mileage": item.get("mileage_km"), "city": item.get("city"),
        "vin_tail6": item.get("vin_tail6"), "reg": [item.get("reg_year"), item.get("reg_month")],
    })


# ---------------------------------------------------------------------------
# base
# ---------------------------------------------------------------------------
class PlatformHandler:
    code: str = "?"
    needs_playwright = False

    def list_request(self, page: int, day: str, *, attempt: int = 0, delay: float = 0.0) -> scrapy.Request:
        raise NotImplementedError

    def parse_list(self, response, *, page: int, day: str) -> ListPage:
        raise NotImplementedError

    def detail_request(self, item: dict[str, Any], day: str, *, attempt: int = 0,
                       delay: float = 0.0) -> scrapy.Request:
        raise NotImplementedError

    def parse_detail(self, response) -> DetailOutcome:
        raise NotImplementedError


def _pw_meta(context: str, methods: list[PageMethod], *, timeout: int | None = None,
             include_page: bool = False) -> dict[str, Any]:
    return {
        "playwright": True,
        "playwright_context": context,
        "playwright_page_methods": methods,
        "playwright_timeout": timeout or settings.playwright_timeout_ms,
        "playwright_include_page": include_page,
        "playwright_abort_request": _abort_heavy_assets,
    }


async def _abort_heavy_assets(request) -> bool:
    return request.resource_type in {"font", "media", "stylesheet"}


# ---------------------------------------------------------------------------
# A: static HTML
# ---------------------------------------------------------------------------
class PlatformA(PlatformHandler):
    code = "A"

    def list_request(self, page, day, *, attempt=0, delay=0.0):
        url = f"{platform_path('A')}/list?page={page}" + (f"&day={day}" if day else "")
        return scrapy.Request(url, callback=None,
                              meta={"attempt": attempt, "delay_before": delay,
                                    "page": page, "day": day,
                                    "handle_httpstatus_list": [403, 429, 500, 502, 503]})

    def parse_list(self, response, *, page, day):
        body = response.text
        items, an = parsers.parse_a_list(body, base_url=base_url())
        try:
            total = int(response.css(".listing-list::attr(data-total)").get() or "")
        except (TypeError, ValueError):
            total = None
        has_more = total is not None and page * PAGE_SIZE["A"] < total
        norm = [_norm_item("A", pid=it["platform_listing_id"], url=it["url"], title=it["title"],
                           price_yuan=parse_price_yuan(it["price_text"]),
                           mileage_km=parse_mileage_km(it["mileage_text"]),
                           city=it["city"], image=it["image"], vin_tail6=it.get("vin_tail6"),
                           raw=it.get("raw") or it) for it in items]
        return ListPage(items=norm, anomaly=an, has_more=has_more, total=total,
                        raw=body, kind="html", extra={"page": page, "day": day})

    def detail_request(self, item, day, *, attempt=0, delay=0.0):
        return scrapy.Request(item["url"], callback=None,
                              meta={"attempt": attempt, "delay_before": delay, "day": day,
                                    "pid": item["platform_listing_id"],
                                    "handle_httpstatus_list": [404, 410, 403, 429, 500, 502, 503]})

    def parse_detail(self, response):
        if response.status == 410:
            return DetailOutcome(anomaly=Anomaly("missing", {"status": 410}), raw=response.text)
        if response.status != 200:
            return DetailOutcome(anomaly=Anomaly("http_error", {"status": response.status}),
                                 raw=response.text)
        p, an = parsers.parse_a_detail(response.text, source_url=response.url)
        if p is not None:
            p.image_urls[:] = [_abs(u) for u in p.image_urls]
        return DetailOutcome(parsed=p, anomaly=an, raw=response.text,
                             content_type=response.headers.get(b"Content-Type", b"").decode() or "text/html")


# ---------------------------------------------------------------------------
# B: Playwright scroll + XHR
# ---------------------------------------------------------------------------
class PlatformB(PlatformHandler):
    code = "B"
    needs_playwright = True

    def list_request(self, page, day, *, attempt=0, delay=0.0):
        collector: dict[str, Any] = {"payload": None, "seen": []}

        async def scroll_and_collect(pg):
            async def on_resp(resp):
                if "/pb/api/list" in resp.url:
                    try:
                        payload = await resp.json()
                    except Exception:
                        return
                    collector["seen"].append(payload)
                    if payload.get("page") == page:
                        collector["payload"] = payload
            pg.on("response", on_resp)
            # load pages 1..N in order; we only keep the payload for this page
            # drive the page's own scroll handler until the requested page arrives
            for _ in range(page):
                if collector.get("payload") is not None:
                    break
                await pg.evaluate("async () => window.__pbScroll()")
                await pg.wait_for_timeout(80)
            pg.remove_listener("response", on_resp)

        # unique URL per page: Scrapy's dupefilter is URL based and the shell is shared
        req = scrapy.Request(
            f"{platform_path('B')}/list?page={page}&day={day}",
            meta=_pw_meta(f"pb", [PageMethod(scroll_and_collect)]) | {
                "attempt": attempt, "delay_before": delay, "page": page, "day": day,
                "collector": collector,
            },
        )
        return req

    def parse_list(self, response, *, page, day):
        collector = response.meta.get("collector") or {}
        payload = collector.get("payload")
        if payload is None:
            return ListPage(anomaly=Anomaly("empty_unverified", {"reason": "xhr payload not captured",
                                                                 "seen": len(collector.get("seen") or [])}),
                            raw=response.text, kind="html")
        items, an = parsers.parse_b_list(payload, base_url=base_url())
        norm = [_norm_item("B", pid=it["platform_listing_id"], url=it["url"], title=it["title"],
                           price_yuan=parse_price_yuan(it["price_text"]),
                           mileage_km=parse_mileage_km(it["mileage_text"]),
                           city=it["city"], image=it["image"], vin_tail6=it.get("vin_tail6"),
                           raw=it.get("raw") or it) for it in items]
        has_more = bool(payload.get("hasMore"))
        return ListPage(items=norm, anomaly=an, has_more=has_more,
                        total=payload.get("total"), raw=payload, kind="xhr",
                        content_type="application/json", extra={"page": page, "payload": payload})

    def detail_request(self, item, day, *, attempt=0, delay=0.0):
        collector: dict[str, Any] = {"payload": None}

        async def fetch_detail(pg):
            collector["payload"] = await pg.evaluate(
                """async ({url}) => {
                    const r = await fetch(url, {credentials: 'include'});
                    let body = null;
                    try { body = await r.json(); } catch (e) { body = {code: -1, msg: 'non-json'}; }
                    return {status: r.status, body: body};
                }""",
                {"url": f"/pb/api/detail?id={item['platform_listing_id']}"},
            )

        return scrapy.Request(
            f"{platform_path('B')}/blank?pid={item['platform_listing_id']}",
            meta=_pw_meta("pb", [PageMethod(fetch_detail)]) | {
                "attempt": attempt, "delay_before": delay, "day": day,
                "pid": item["platform_listing_id"], "collector": collector,
                "list_title": item.get("title"),
            },
        )

    def parse_detail(self, response):
        collector = response.meta.get("collector") or {}
        payload = collector.get("payload") or {}
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            return DetailOutcome(anomaly=Anomaly("blocked_signal", {"reason": "detail xhr failed"}),
                                 raw=payload, kind="xhr", content_type="application/json")
        if body.get("code") == 404:
            return DetailOutcome(anomaly=Anomaly("missing", {"reason": body.get("msg")}),
                                 raw=body, kind="xhr", content_type="application/json")
        p, an = parsers.parse_b_detail(body, source_url=f"/pb/detail/{response.meta.get('pid')}")
        if p is not None:
            p.source_url = f"{platform_path('B')}/detail/{p.platform_listing_id}"
            for i, u in enumerate(p.image_urls):
                p.image_urls[i] = _abs(u)
        return DetailOutcome(parsed=p, anomaly=an, raw=body, kind="xhr",
                             content_type="application/json")


# ---------------------------------------------------------------------------
# C: anti-bot static HTML
# ---------------------------------------------------------------------------
class PlatformC(PlatformA):
    code = "C"

    def list_request(self, page, day, *, attempt=0, delay=0.0):
        url = f"{platform_path('C')}/list?page={page}" + (f"&day={day}" if day else "")
        return scrapy.Request(url, meta={"attempt": attempt, "delay_before": delay,
                                         "page": page, "day": day,
                                         "handle_httpstatus_list": [403, 429, 500, 502, 503]})

    def parse_list(self, response, *, page, day):
        body = response.text
        items, an = parsers.parse_c_list(body, base_url=base_url())
        total = None
        try:
            total = int(response.css(".listing-list::attr(data-total)").get() or "")
        except ValueError:
            total = None
        has_more = total is not None and page * PAGE_SIZE["C"] < total
        norm = [_norm_item("C", pid=it["platform_listing_id"], url=it["url"], title=it["title"],
                           price_yuan=parse_price_yuan(it["price_text"]),
                           mileage_km=parse_mileage_km(it["mileage_text"]),
                           city=it["city"], image=it["image"], vin_tail6=it.get("vin_tail6"),
                           raw=it.get("raw") or it) for it in items]
        return ListPage(items=norm, anomaly=an, has_more=has_more, total=total,
                        raw=body, kind="html", extra={"page": page, "day": day})

    def detail_request(self, item, day, *, attempt=0, delay=0.0):
        return scrapy.Request(item["url"], meta={"attempt": attempt, "delay_before": delay,
                                                 "day": day, "pid": item["platform_listing_id"],
                                                 "handle_httpstatus_list": [404, 410, 403, 429, 500, 502, 503]})

    def parse_detail(self, response):
        if response.status == 404:
            return DetailOutcome(anomaly=Anomaly("missing", {"status": 404}), raw=response.text)
        if response.status != 200:
            return DetailOutcome(anomaly=Anomaly("http_error", {"status": response.status}),
                                 raw=response.text)
        p, an = parsers.parse_c_detail(response.text, source_url=response.url)
        if p is not None:
            p.image_urls[:] = [_abs(u) for u in p.image_urls]
        return DetailOutcome(parsed=p, anomaly=an, raw=response.text)


# ---------------------------------------------------------------------------
# D: Playwright + signed app API
# ---------------------------------------------------------------------------
class PlatformD(PlatformHandler):
    code = "D"
    needs_playwright = True

    def _app_request(self, methods, *, attempt, delay, page=None, pid=None, collector=None):
        marker = pid or f"page-{page}"
        req = scrapy.Request(
            f"{platform_path('D')}/app?req={marker}&kind={'detail' if pid else 'list'}",
            meta=_pw_meta("pd", methods) | {
                "attempt": attempt, "delay_before": delay,
                "page": page, "pid": pid, "collector": collector or {},
            },
        )
        return req

    def list_request(self, page, day, *, attempt=0, delay=0.0):
        collector: dict[str, Any] = {"payload": None}

        async def fetch_list(pg):
            await pg.wait_for_function("() => window.__pdReady === true", timeout=15000)
            collector["payload"] = await pg.evaluate(
                """async ({page, day}) => {
                    const r = await pdAttempt('/pd/api/list', {page: page, day: day});
                    return r;
                }""",
                {"page": page, "day": day},
            )

        return self._app_request([PageMethod(fetch_list)], attempt=attempt, delay=delay,
                                 page=page, collector=collector)

    def parse_list(self, response, *, page, day):
        collector = response.meta.get("collector") or {}
        payload = collector.get("payload") or {}
        status = payload.get("status")
        body = payload.get("body")
        if status == 403:
            return ListPage(anomaly=Anomaly("signature_expired", {"status": 403, "body": body}),
                            raw=payload, kind="xhr")
        if status == 401:
            return ListPage(anomaly=Anomaly("blocked_signal", {"status": 401, "body": body}),
                            raw=payload, kind="xhr")
        if status != 200 or not isinstance(body, dict):
            return ListPage(anomaly=Anomaly("http_error", {"status": status}), raw=payload, kind="xhr")
        items, an = parsers.decode_d_list(body, base_url=base_url())
        norm = [_norm_item("D", pid=str(it["platform_listing_id"]), url=str(it["url"]),
                           title=it["title"],
                           price_yuan=(int(it["price_fen"]) // 100) if isinstance(it.get("price_fen"), (int, float)) else None,
                           mileage_km=it.get("mileage_km"),
                           city=it.get("city"),
                           image=it.get("image"),
                           vin_tail6=None,
                           reg=parse_reg_ym(it.get("reg_text")),
                           raw=it.get("raw") or it) for it in items]
        return ListPage(items=norm, anomaly=an, has_more=bool((body.get("data") or {}).get("hasMore")),
                        total=(body.get("data") or {}).get("total"), raw=body, kind="xhr",
                        content_type="application/json", extra={"page": page})

    def detail_request(self, item, day, *, attempt=0, delay=0.0):
        collector: dict[str, Any] = {"payload": None}

        async def fetch_detail(pg):
            await pg.wait_for_function("() => window.__pdReady === true", timeout=15000)
            collector["payload"] = await pg.evaluate(
                """async ({id, day}) => await pdAttempt('/pd/api/detail', {id: id, day: day})""",
                {"id": item["platform_listing_id"], "day": day},
            )

        return self._app_request([PageMethod(fetch_detail)], attempt=attempt, delay=delay,
                                 pid=item["platform_listing_id"], collector=collector)

    def parse_detail(self, response):
        collector = response.meta.get("collector") or {}
        payload = collector.get("payload") or {}
        status = payload.get("status")
        body = payload.get("body")
        if status == 403:
            return DetailOutcome(anomaly=Anomaly("signature_expired", {"status": 403, "body": body}),
                                 raw=payload, kind="xhr")
        if status == 401:
            return DetailOutcome(anomaly=Anomaly("blocked_signal", {"status": 401, "body": body}),
                                 raw=payload, kind="xhr")
        if status != 200 or not isinstance(body, dict):
            return DetailOutcome(anomaly=Anomaly("http_error", {"status": status}), raw=payload, kind="xhr")
        if body.get("code") in (404, "404"):
            return DetailOutcome(anomaly=Anomaly("missing", {"reason": body.get("msg")}),
                                 raw=body, kind="xhr")
        p, an = parsers.decode_d_detail(body, source_url=f"/pd/detail/{response.meta.get('pid')}")
        if p is not None:
            p.source_url = f"{platform_path('D')}/detail/{p.platform_listing_id}"
            p.image_urls[:] = [_abs(u) for u in p.image_urls]
        return DetailOutcome(parsed=p, anomaly=an, raw=body, kind="xhr",
                             content_type="application/json")


def _abs(url: str | None) -> str:
    if not url:
        return ""
    if url.startswith("http"):
        return url
    return base_url().rstrip("/") + "/" + url.lstrip("/")


HANDLERS: dict[str, PlatformHandler] = {"A": PlatformA(), "B": PlatformB(), "C": PlatformC(), "D": PlatformD()}


def platform_rate(code: str) -> tuple[int, float]:
    """(concurrency, delay) from the platform row, falling back to config."""
    from carscout.db import pool

    try:
        row = pool.query_one("SELECT config FROM platforms WHERE code = %s", (code.upper(),))
    except Exception:
        row = None
    rate = ((row or {}).get("config") or {}).get("rate") or {}
    default = settings.platform_limits.get(code.upper(), {"concurrent": 2, "delay": 0.4})
    return (int(rate.get("concurrent", default.get("concurrent", 2))),
            float(rate.get("delay", default.get("delay", 0.4))))


# ---------------------------------------------------------------------------
# forced detail refresh (rerun / specific-date modes, and changed list rows)
# ---------------------------------------------------------------------------
def force_detail_refresh(item: dict[str, Any], *, run_mode: str) -> bool:
    """Whether this run must re-fetch the detail page even if the fingerprint matches.

    incremental  : only when the list fingerprint changed (handled by the engine)
    rerun        : always (the operator explicitly asked for a fresh extraction)
    resume       : only when the row is not already in a terminal detail state
    """
    return run_mode in {"rerun"}
