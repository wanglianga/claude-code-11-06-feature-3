"""Platform field parsers.

Every parser returns a `ParsedListing` plus a list of `Anomaly` records.
Anomalies are classified into exactly these categories so the runner can
distinguish "platform genuinely has no stock" from "request was blocked":

  blocked_signal    HTTP/body shows an anti-bot page or a signed request was rejected
  empty_unverified  200 OK but the listing container / payload contains no items
  schema_drift      container present but required detail fields missing or relabelled
  http_error        transport-level 4xx/5xx or timeout
  signature_expired dedicated D-platform rejection (retried with a fresh signature)
  parser_error      unexpected exception while extracting
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import Any

from parsel import Selector

from carscout.utils import normalize_url

REQUIRED_DETAIL_FIELDS = ("brand", "series", "reg_year", "mileage_km", "price_yuan")


@dataclass
class Anomaly:
    category: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.category}:{self.detail}"


@dataclass
class ParsedListing:
    platform: str
    platform_listing_id: str
    source_url: str
    title: str | None = None
    brand: str | None = None
    series: str | None = None
    model: str | None = None
    reg_year: int | None = None
    reg_month: int | None = None
    mileage_km: int | None = None
    displacement_ml: int | None = None
    transmission: str | None = None
    transfer_count: int | None = None
    city: str | None = None
    price_yuan: int | None = None
    listed_at: datetime | None = None
    seller_type: str | None = None
    description: str | None = None
    vin: str | None = None
    image_urls: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def vin_tail6(self) -> str | None:
        return vin_tail(self.vin)

    def missing_required(self) -> list[str]:
        out = []
        for f in REQUIRED_DETAIL_FIELDS:
            if getattr(self, f) in (None, ""):
                out.append(f)
        return out


BRANDS = ["宝马", "奥迪", "奔驰", "大众", "丰田", "本田", "特斯拉", "比亚迪", "别克", "日产"]
SERIES_BY_BRAND = {
    "宝马": ["3系", "5系", "X3", "X5"], "奥迪": ["A4L", "A6L", "Q5L", "Q3"],
    "奔驰": ["C级", "E级", "GLC"], "大众": ["迈腾", "帕萨特", "途观L", "朗逸"],
    "丰田": ["凯美瑞", "汉兰达", "卡罗拉", "RAV4荣放"], "本田": ["雅阁", "CR-V", "思域"],
    "特斯拉": ["Model 3", "Model Y"], "比亚迪": ["汉", "唐", "秦PLUS", "宋PLUS"],
    "别克": ["君威", "昂科威"], "日产": ["轩逸", "天籁"],
}
RECOGNISED_SERIES = {s for v in SERIES_BY_BRAND.values() for s in v}


def vin_tail(vin: str | None) -> str | None:
    if not vin:
        return None
    digits = re.sub(r"[^0-9A-Za-z]", "", vin.upper())
    if len(digits) >= 6:
        return digits[-6:]
    return None


def parse_price_yuan(text: str | None) -> int | None:
    """Parse 报价文本: '18.60万' / '18.6万' / '18.6 万元' / '186000元' / '186000'."""
    if text is None:
        return None
    t = str(text).replace(",", "").replace(" ", "").strip()
    if not t:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", t)
    if not m:
        return None
    num = float(m.group(1))
    if "万" in t:
        return int(round(num * 10000))
    if "元" in t:
        return int(round(num))
    if num < 1000:  # bare number likely 万
        return int(round(num * 10000))
    return int(round(num))


def parse_mileage_km(text: str | None) -> int | None:
    """'6.00万公里' / '60000公里' / '60,000 km' / '6万' / 60000."""
    if text is None:
        return None
    t = str(text).replace(",", "").replace(" ", "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)", t)
    if not m:
        return None
    num = float(m.group(1))
    if "万" in t:
        return int(round(num * 10000))
    if "km" in t.lower() or "公里" in t:
        return int(round(num))
    if num < 100:  # bare 6.0 -> 万
        return int(round(num * 10000))
    return int(round(num))


def parse_reg_ym(text: str | None) -> tuple[int | None, int | None]:
    """'2018-06' | '2018年6月' | '2019/03/01' | '201806' | '2018' -> (year, month)."""
    if text is None:
        return None, None
    t = str(text).strip()
    m = re.search(r"((?:19|20)\d{2})\s*[-/.年]\s*(\d{1,2})(?!\d)", t)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return y, (mo if 1 <= mo <= 12 else None)
    m = re.search(r"\b((?:19|20)\d{2})(\d{2})\b", t)  # 201806
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return (y, mo) if 1 <= mo <= 12 else (y, None)
    m = re.search(r"(?:19|20)\d{2}", t)
    if m:
        return int(m.group(0)), None
    return None, None


def parse_listed_date(text: str | None) -> datetime | None:
    if not text:
        return None
    t = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(t.replace("Z", "+0000"), fmt) if fmt.endswith("%z") else datetime.strptime(t, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    return None


def parse_displacement_ml(text: Any) -> int | None:
    if text is None:
        return None
    t = str(text).strip()
    if not t or "纯电" in t or "电动" in t or t in {"0", "0.0"}:
        return 0
    m = re.search(r"(\d+(?:\.\d+)?)", t)
    if not m:
        return None
    num = float(m.group(1))
    if "L" in t.upper() or "l" in t or num < 10:
        return int(round(num * 1000))
    return int(round(num))


def parse_transfer_count(text: Any) -> int | None:
    if text is None:
        return None
    if isinstance(text, int):
        return text
    m = re.search(r"(\d+)", str(text))
    return int(m.group(1)) if m else None


def parse_brand_series_from_title(title: str | None, fallback_brand: str | None = None,
                                   fallback_series: str | None = None) -> tuple[str | None, str | None]:
    if fallback_brand and fallback_series:
        return fallback_brand, fallback_series
    t = title or ""
    brand = fallback_brand or next((b for b in BRANDS if b in t), None)
    series = fallback_series
    if not series and brand:
        candidates = SERIES_BY_BRAND.get(brand, [])
        series = next((s for s in candidates if s.lower() in t.lower()), None)
    if not series:
        for b, cands in SERIES_BY_BRAND.items():
            hit = next((s for s in cands if s.lower() in t.lower()), None)
            if hit:
                brand = brand or b
                series = hit
                break
    return brand, series


def clean_text(text: str | None) -> str | None:
    if text is None:
        return None
    t = re.sub(r"\s+", " ", text).strip()
    return t or None


# ---------------------------------------------------------------------------
# platform A: static HTML
# ---------------------------------------------------------------------------
BLOCK_MARKERS = [
    "访问过于频繁", "验证码", "安全验证", "请完成验证", "异常访问", "Access Denied",
    "captcha", "blocked", "forbidden", "人机验证", "滑动验证",
]


def detect_block_signal(status: int, body_head: str, headers: dict[str, str] | None = None) -> str | None:
    if status in (401, 403, 429):
        return f"http_{status}"
    if status == 200 and headers:
        server = (headers.get("Server") or "").lower()
        if "cloudflare" in server and "challenge" in (headers.get("Cf-Mitigated") or "").lower():
            return "cf_challenge"
    head_lower = body_head.lower()
    for marker in BLOCK_MARKERS:
        if marker.lower() in head_lower:
            return marker
    return None


def parse_a_list(body: str, *, base_url: str = "") -> tuple[list[dict[str, Any]], Anomaly | None]:
    sel = Selector(text=body)
    cards = sel.css(".listing-card")
    if not cards:
        total = sel.css(".listing-list::attr(data-total)").get()
        has_footer = bool(sel.css(".site-foot"))
        if total is not None and total.isdigit() and int(total) == 0 and has_footer:
            return [], None                       # verified empty: no cars today
        return [], Anomaly("empty_unverified", {"container": ".listing-card", "footer": has_footer})
    if not sel.css("a.detail-link"):
        return [], Anomaly("schema_drift", {"reason": "detail link selector missing"})
    items = []
    for card in cards:
        href = card.css("a.detail-link::attr(href)").get()
        items.append({
            "platform_listing_id": card.css("::attr(data-id)").get() or "",
            "title": clean_text(card.css("a.detail-link::text").get()),
            "url": _abs_url(href, base_url),
            "price_text": clean_text(card.css("span.price::text").get()),
            "city": clean_text(card.css("span.city::text").get()),
            "mileage_text": clean_text(card.css("span.mileage::text").get()),
            "image": _abs_url(card.css("img.thumb::attr(src)").get(), base_url),
        })
    items = [i for i in items if i["platform_listing_id"] and i["url"]]
    if not items:
        return [], Anomaly("schema_drift", {"reason": "cards present but no usable ids/links"})
    return items, None


def parse_a_detail(body: str, *, source_url: str, platform: str = "A") -> tuple[ParsedListing | None, Anomaly | None]:
    sel = Selector(text=body)
    if not sel.css(".specs"):
        return None, Anomaly("schema_drift", {"reason": "spec container missing"})
    title = sel.css("h1::text").get() or sel.css("h1.detail-title::text").get()
    fields = _spec_pairs(sel)
    p = _build_from_fields(fields, title=title, source_url=source_url, platform=platform,
                           extra_images=_gallery(sel, ".gallery-img"))
    missing = p.missing_required()
    if missing:
        return p, Anomaly("schema_drift", {"missing": missing, "labels_seen": sorted(fields)})
    return p, None


def _spec_pairs(sel: Selector) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in sel.css(".spec"):
        label = clean_text(node.css("label::text").get())
        value = clean_text(node.xpath(".//span[contains(@class,'v')]/text()").get())
        if label and value is not None:
            out[label] = value
    return out


def _gallery(sel: Selector, css: str) -> list[str]:
    return [u for u in sel.css(f"{css}::attr(src)").getall() if u]


def _abs_url(url: str | None, base: str) -> str | None:
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return base.rstrip("/") + url
    return base.rstrip("/") + "/" + url


def _build_from_fields(fields: dict[str, str], *, title: str | None, source_url: str,
                       platform: str, extra_images: list[str] | None = None,
                       pid: str | None = None, price_yuan: int | None = None,
                       mileage_km: int | None = None, reg: tuple[int | None, int | None] = (None, None),
                       raw: dict | None = None) -> ParsedListing:
    """Map a platform-independent field bag into ParsedListing.

    Recognises both the canonical Chinese labels and observed relabelled variants.
    """
    def pick(*keys: str) -> str | None:
        for k in keys:
            if k in fields and fields[k] not in (None, ""):
                return fields[k]
        return None

    brand = pick("品牌")
    series = pick("车系")
    model = pick("车型")
    brand, series = parse_brand_series_from_title(title, brand, series)
    reg_text = pick("上牌年月", "上牌时间", "首次上牌", "注册日期")
    ry, rm = parse_reg_ym(reg_text)
    if ry is None and reg[0] is not None:
        ry, rm = reg
    mileage = mileage_km if mileage_km is not None else parse_mileage_km(
        pick("表显里程", "行驶里程", "里程", "公里数"))
    price = price_yuan if price_yuan is not None else parse_price_yuan(pick("挂牌价", "价格", "售价", "报价"))
    imgs = list(extra_images or [])
    p = ParsedListing(
        platform=platform,
        platform_listing_id=pid or _pid_from_url(source_url),
        source_url=source_url,
        title=clean_text(title),
        brand=brand, series=series, model=model,
        reg_year=ry, reg_month=rm,
        mileage_km=mileage,
        displacement_ml=parse_displacement_ml(pick("排量", "发动机排量")),
        transmission=pick("变速箱", "变速器"),
        transfer_count=parse_transfer_count(pick("过户次数", "过户情况")),
        city=pick("所在城市", "城市", "上牌地"),
        price_yuan=price,
        listed_at=parse_listed_date(pick("挂牌时间", "发布时间", "上架时间")),
        seller_type=pick("卖家类型", "卖家", "车商类型"),
        description=pick("车况描述", "车辆描述", "车源描述", "描述"),
        vin=pick("车架号", "VIN", "Vin码", "vin"),
        image_urls=imgs,
        raw=raw or fields,
    )
    return p


def _pid_from_url(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# platform B: XHR JSON list + XHR JSON detail
# ---------------------------------------------------------------------------
def parse_b_list(payload: dict[str, Any], *, base_url: str) -> tuple[list[dict[str, Any]], Anomaly | None]:
    items = payload.get("items") if isinstance(payload, dict) else None
    if items is None:
        return [], Anomaly("schema_drift", {"reason": "items key missing", "keys": sorted(payload)})
    out = []
    for it in items:
        lid = it.get("id") or it.get("listingId")
        if not lid:
            continue
        out.append({
            "platform_listing_id": str(lid),
            "title": clean_text(it.get("title")),
            "price_text": it.get("priceWan"),
            "city": clean_text(it.get("cityName")),
            "mileage_text": it.get("mileageWan"),
            "image": _abs_url(it.get("cover"), base_url),
            "vin_tail6": it.get("vinTail6"),
            "url": f"{base_url.rstrip('/')}/pb/detail/{lid}",
            "raw": it,
        })
    if not out and payload.get("pageSize"):
        # page declared a page size but shipped nothing: ambiguous, treated as blocked-ish
        return [], Anomaly("empty_unverified", {"page": payload.get("page"), "pageSize": payload.get("pageSize")})
    return out, None


def parse_b_detail(payload: dict[str, Any], *, source_url: str) -> tuple[ParsedListing | None, Anomaly | None]:
    if not isinstance(payload, dict):
        return None, Anomaly("schema_drift", {"reason": "detail payload not an object"})
    if payload.get("code") not in (0, "0", None):
        if payload.get("code") in (404, "404"):
            return None, Anomaly("missing", {"reason": payload.get("msg")})
        return None, Anomaly("blocked_signal", {"reason": payload.get("msg"), "code": payload.get("code")})
    d = payload.get("data")
    if not isinstance(d, dict):
        return None, Anomaly("schema_drift", {"reason": "data key missing"})
    ry, rm = parse_reg_ym(d.get("regDate"))
    p = ParsedListing(
        platform="B",
        platform_listing_id=str(d.get("id") or _pid_from_url(source_url)),
        source_url=source_url,
        title=clean_text(d.get("title")),
        brand=d.get("brandName"),
        series=d.get("seriesName"),
        model=d.get("modelName"),
        reg_year=ry, reg_month=rm,
        mileage_km=parse_mileage_km(d.get("mileageKm")),
        displacement_ml=parse_displacement_ml(d.get("displacement")),
        transmission=d.get("gearbox"),
        transfer_count=parse_transfer_count(d.get("transferTimes", d.get("ownerCount"))),
        city=d.get("cityName"),
        price_yuan=parse_price_yuan(d.get("priceWan")),
        listed_at=parse_listed_date(d.get("publishTime")),
        seller_type=d.get("sellerType"),
        description=clean_text(d.get("description")),
        vin=d.get("vin"),
        image_urls=[_abs_url(u, "http://127.0.0.1:8088") or "" for u in (d.get("images") or [])],
        raw=d,
    )
    if p.brand is None or p.series is None:
        p.brand, p.series = parse_brand_series_from_title(p.title, p.brand, p.series)
    missing = p.missing_required()
    if missing:
        return p, Anomaly("schema_drift", {"missing": missing, "keys": sorted(d)})
    return p, None


# ---------------------------------------------------------------------------
# platform C: same HTML shape as A, different anti-bot posture
# ---------------------------------------------------------------------------
def parse_c_list(body: str, *, base_url: str) -> tuple[list[dict[str, Any]], Anomaly | None]:
    sel = Selector(text=body)
    if not sel.css(".listing-list"):
        return [], Anomaly("blocked_signal", {"reason": "listing container absent"})
    if not sel.css(".site-foot"):
        return [], Anomaly("empty_unverified", {"reason": "no page footer; anti-bot stub"})
    return parse_a_list(body, base_url=base_url)


def parse_c_detail(body: str, *, source_url: str) -> tuple[ParsedListing | None, Anomaly | None]:
    sel = Selector(text=body)
    if not sel.css(".specs"):
        return None, Anomaly("blocked_signal", {"reason": "detail stub without specs"})
    return parse_a_detail(body, source_url=source_url, platform="C")


# ---------------------------------------------------------------------------
# platform D: signed app JSON
# ---------------------------------------------------------------------------
def decode_d_list(payload: dict[str, Any], *, base_url: str) -> tuple[list[dict[str, Any]], Anomaly | None]:
    code = payload.get("code")
    if code == "SIGN_INVALID":
        return [], Anomaly("signature_expired", {"reason": payload.get("reason")})
    if code == "SESSION_REQUIRED":
        return [], Anomaly("blocked_signal", {"reason": "session cookie missing"})
    if code not in (0, "0"):
        return [], Anomaly("blocked_signal", {"reason": payload.get("reason") or payload.get("msg"), "code": code})
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("items") is None:
        return [], Anomaly("schema_drift", {"reason": "data.items missing"})
    out = []
    for it in data["items"]:
        lid = it.get("listingId")
        if not lid:
            continue
        out.append({
            "platform_listing_id": str(lid),
            "title": clean_text(it.get("carName")),
            "price_fen": it.get("price"),
            "city": it.get("cityCode"),
            "mileage_km": it.get("odometer"),
            "reg_text": it.get("registerDate"),
            "dealer_type": it.get("dealerType"),
            "image": _abs_url(it.get("coverUrl"), base_url),
            "url": f"{base_url.rstrip('/')}/pd/detail/{lid}",
            "raw": it,
        })
    return out, None


def decode_d_detail(payload: dict[str, Any], *, source_url: str) -> tuple[ParsedListing | None, Anomaly | None]:
    code = payload.get("code")
    if code == "SIGN_INVALID":
        return None, Anomaly("signature_expired", {"reason": payload.get("reason")})
    if code == "SESSION_REQUIRED":
        return None, Anomaly("blocked_signal", {"reason": "session cookie missing"})
    if code in (404, "404"):
        return None, Anomaly("missing", {"reason": payload.get("msg")})
    if code not in (0, "0"):
        return None, Anomaly("blocked_signal", {"reason": payload.get("msg"), "code": code})
    d = payload.get("data")
    if not isinstance(d, dict):
        return None, Anomaly("schema_drift", {"reason": "data missing"})
    ry, rm = parse_reg_ym(d.get("firstRegisterDate"))
    fen = d.get("priceFen")
    p = ParsedListing(
        platform="D",
        platform_listing_id=str(d.get("listingId") or _pid_from_url(source_url)),
        source_url=source_url,
        title=f"{d.get('brandName') or ''} {d.get('seriesName') or ''} {d.get('modelName') or ''}".strip() or None,
        brand=d.get("brandName"), series=d.get("seriesName"), model=d.get("modelName"),
        reg_year=ry, reg_month=rm,
        mileage_km=parse_mileage_km(d.get("odometerKm")),
        displacement_ml=parse_displacement_ml(d.get("engineDisplacementMl")),
        transmission=d.get("transmission"),
        transfer_count=parse_transfer_count(d.get("transferTimes")),
        city=d.get("cityName"),
        price_yuan=(int(fen) // 100) if isinstance(fen, (int, float)) else parse_price_yuan(fen),
        listed_at=parse_listed_date(d.get("publishTime")),
        seller_type={"DEALER": "商家", "PRIVATE": "个人"}.get(d.get("dealerType"), d.get("dealerType")),
        description=clean_text(d.get("descriptionText")),
        vin=d.get("vin"),
        image_urls=[_abs_url(u, "http://127.0.0.1:8088") or "" for u in (d.get("images") or [])],
        raw=d,
    )
    if p.brand is None or p.series is None:
        p.brand, p.series = parse_brand_series_from_title(p.title, p.brand, p.series)
    missing = p.missing_required()
    if missing:
        return p, Anomaly("schema_drift", {"missing": missing, "keys": sorted(d)})
    return p, None
