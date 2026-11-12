"""Fake multi-platform HTTP fixture server (仿真的四个平台).

Run standalone:  python -m carscout.fixture.server --port 8088
The handler is also mountable in tests and in the compose stack.

Anti-bot behaviour on platform C is controlled by a small token bucket so the
"200 but empty payload" failure mode can be demonstrated and tested deterministically.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import random
import threading
import time
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from typing import Any
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageDraw

from carscout.config import settings
from carscout.fixture import dataset as ds


# ---------------------------------------------------------------------------
# runtime knobs (reset between test cases)
# ---------------------------------------------------------------------------
class Runtime:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.snapshot_day: date = ds.TODAY
        # platform C anti-bot
        self.c_requests_in_window = 0
        self.c_window_start = 0.0
        self.c_max_per_window = 1000     # effectively unlimited by default
        self.c_window_seconds = 60.0
        self.c_force_blocked = False
        self.c_force_empty = False       # legitimate empty list response
        # schema drift simulation (rename a field / break a selector)
        self.drift: dict[str, bool] = {"A": False, "B": False, "C": False, "D": False}
        # artificial latency (ms) for crash/recovery tests; 0 = off
        self.response_delay_ms = 0
        # platform D signature settings
        self.d_secret = settings.fixture_secret
        self.d_window_seconds = 240
        self.d_force_expired_once = 0
        # request log for tests / observability (bounded)
        self.request_log: list[dict[str, Any]] = []

    def reset(self) -> None:
        with self.lock:
            self.__init__()

    def record(self, platform: str, path: str, outcome: str, **extra: Any) -> None:
        with self.lock:
            entry = {"platform": platform, "path": path, "outcome": outcome,
                     "ts": time.time(), **extra}
            self.request_log.append(entry)
            if len(self.request_log) > 5000:
                del self.request_log[:1000]

    def c_take_token(self) -> bool:
        """Token bucket; returns False when the request should be silently throttled."""
        with self.lock:
            if self.c_force_blocked:
                return False
            now = time.monotonic()
            if now - self.c_window_start > self.c_window_seconds:
                self.c_window_start = now
                self.c_requests_in_window = 0
            self.c_requests_in_window += 1
            return self.c_requests_in_window <= self.c_max_per_window


RUNTIME = Runtime()


# ---------------------------------------------------------------------------
# deterministic images (encode car + variant so pHash is reproducible & stable)
# ---------------------------------------------------------------------------
def _seed(cid: str, variant: str = "same") -> int:
    return int(hashlib.md5(f"{cid}:{variant}".encode()).hexdigest()[:8], 16)


def car_image_png(cid: str, variant: str = "same", size: int = 320) -> bytes:
    """Generate a unique-per-car "photo". The silhouette and low-frequency
    shading pattern derive from the car id, so distinct cars are far apart in
    pHash space while recompressed copies of the same photo stay within a few
    bits. `variant="alt"` is a different photograph of the same car (mirrored,
    different crop / background) used to exercise the grey zone.

    Deterministic: same (cid, variant) always yields the same pixels.
    """
    seed = _seed(cid, variant)
    rnd = random.Random(seed)
    img = Image.new("RGB", (size, size), (225, 226, 224))
    draw = ImageDraw.Draw(img)
    horizon = int(size * (0.34 + (seed % 17) / 100.0))
    sky = (140 + seed % 60, 175 + (seed >> 8) % 50, 205 + (seed >> 16) % 40)
    draw.rectangle([0, 0, size, horizon], fill=sky)
    draw.rectangle([0, horizon, size, size], fill=(105 + (seed >> 4) % 35, 105, 103))

    # large low-frequency blocks whose layout is unique per car (like a big
    # billboard/backdrop), keeps pHash discriminating between distinct cars
    bits = seed.to_bytes(8, "big")
    gx = gy = 8
    bw, bh = size // gx, size // gy
    for i in range(gx * gy):
        if (bits[i % 8] >> (i // 8)) & 1:
            shade = 40 + ((seed >> (i % 19)) % 150)
            draw.rectangle([ (i % gx) * bw, (i // gx) * bh,
                             (i % gx) * bw + bw - 1, (i // gx) * bh + bh - 1 ],
                           fill=(shade, shade, shade))

    if variant == "alt":
        # second photo: different backdrop in the lower half + a bright band,
        # enough low-frequency difference that the pHash is clearly not the same frame
        alt_seed = _seed(cid, "altbg")
        for i in range(10):
            shade = 20 + ((alt_seed >> (i % 19)) % 170)
            bx = (alt_seed >> (i * 3)) % max(1, size - size // 4)
            by_ = horizon + ((alt_seed >> (i * 5)) % max(1, size - horizon - 4))
            draw.rectangle([bx, by_, bx + size // 4, by_ + size // 5], fill=(shade, shade, shade))
        band_y = size // 2 + (alt_seed % (size // 4))
        draw.rectangle([0, band_y, size, band_y + int(size * 0.08)], fill=(235, 235, 225))

    # car silhouette, proportions unique per car
    body_h = int(size * (0.22 + (seed >> 3) % 13 / 100.0))
    body_w = int(size * (0.62 + (seed >> 11) % 20 / 100.0))
    x0 = (size - body_w) // 2
    y0 = horizon + int(size * 0.10)
    roof_h = int(size * (0.11 + (seed >> 7) % 9 / 100.0))
    roof_w = int(body_w * (0.50 + (seed >> 17) % 20 / 100.0))
    tint = ((seed >> 5) % 200, (seed >> 13) % 200, (seed >> 21) % 200)
    draw.rounded_rectangle([x0, y0, x0 + body_w, y0 + body_h], radius=int(body_h * 0.20),
                           fill=tint)
    rx0 = x0 + (body_w - roof_w) // 2
    draw.rectangle([rx0, y0 - roof_h, rx0 + roof_w, y0 + 8],
                   fill=tuple(max(0, c - 30) for c in tint))
    wh_r = int(size * (0.075 + (seed >> 23) % 5 / 100.0))
    wbase = int(x0 + body_w * (0.18 + (seed >> 27) % 10 / 100.0))
    for cx in (wbase, x0 + body_w - (wbase - x0)):
        draw.ellipse([cx - wh_r, y0 + body_h - wh_r // 2, cx + wh_r, y0 + body_h + wh_r],
                     fill=(28, 28, 28))
        draw.ellipse([cx - wh_r // 2, y0 + body_h + wh_r // 4, cx + wh_r // 2,
                      y0 + body_h + wh_r * 3 // 4], fill=(120, 120, 120))
    # windscreen band (dominant low-frequency feature)
    draw.rectangle([rx0 + 10, y0 - roof_h + 6, rx0 + roof_w - 10, y0 - roof_h // 3],
                   fill=(45, 55, 70))

    if variant == "alt":
        # a *second photograph* of the same car: different location, background
        # and framing. Close enough to be a candidate, far enough that the
        # score alone must not auto-merge.
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
        img = img.crop((int(size * 0.06), int(size * 0.04), size - int(size * 0.02), size - int(size * 0.10)))
        img = img.resize((int(size * 0.9), int(size * 0.9)), resample=Image.BILINEAR)
        img = img.transform(img.size, Image.AFFINE, (1, 0.06, -0.09 * size, 0, 0.98, 0.02 * size),
                            resample=Image.BILINEAR)
        img = Image_enhance_brightness(img, 1.25)

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=30 if variant != "same" else 62)
    return buf.getvalue()


def Image_enhance_brightness(img: Image.Image, factor: float) -> Image.Image:
    from PIL import ImageEnhance
    return ImageEnhance.Brightness(img).enhance(factor)


def car_image_webp(cid: str, variant: str = "same") -> bytes:
    img = Image.open(BytesIO(car_image_png(cid, variant)))
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=40)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
TODAY_STR = ds.TODAY.isoformat()


def _fmt_price(p: int) -> str:
    return f"{p / 10000:.2f}万"


def _detail_fields(l: ds.Listing, drift: bool, day: date = ds.TODAY) -> dict[str, str]:
    car = l.car
    reg_y, reg_m = l.eff_reg
    fields = {
        "品牌": car.brand,
        "车系": car.series,
        "车型": car.model,
        "上牌年月": f"{reg_y}-{reg_m:02d}",
        "表显里程": f"{l.eff_mileage}公里",
        "排量": ("纯电动" if car.displacement == 0 else f"{car.displacement / 1000:.1f}L"),
        "变速箱": car.transmission,
        "过户次数": f"{l.eff_transfer}次",
        "所在城市": l.eff_city,
        "挂牌价": _fmt_price(l.current_price(day) or 0),
        "挂牌时间": l.listed_at.strftime("%Y-%m-%d"),
        "卖家类型": l.eff_seller,
        "车况描述": ds.description_for(l),
        "燃料类型": car.fuel,
        "车身颜色": car.color,
    }
    if l.vin_value:
        fields["车架号"] = l.vin_value
    if drift:
        # a "page redesign": the odometer label is renamed to something a stale
        # parser does not recognise, and transmission moved into a nested card
        fields.pop("变速箱", None)
        fields["行驶里程（以实车为准）"] = fields.pop("表显里程")
    return fields


def render_list_page(platform: str, listings: list[ds.Listing], page: int, total_pages: int,
                     drift: bool = False, total: int | None = None, day: str | None = None,
                     snapshot: date | None = None) -> str:
    total = len(listings) if total is None else total
    day_q = f"?day={day}" if day else "?"
    pfx = "p" + platform.lower()
    day_str = day or TODAY_STR
    snap = snapshot or ds.TODAY
    rows = []
    for l in listings:
        price = l.current_price(snap) or 0
        rows.append(f"""
      <div class="listing-card" data-id="{l.pid}">
        <a class="detail-link" href="/{pfx}/detail/{l.pid}{day_q}">{ds.title_for(l)}</a>
        <span class="price">{"价格待询" if drift else _fmt_price(price)}</span>
        <span class="city">{l.eff_city}</span>
        <span class="mileage">{l.eff_mileage}公里</span>
        <img class="thumb" src="/{pfx}/img/{l.pid}.png" loading="lazy"/>
      </div>""")
    pager = "".join(
        f'<a class="page-link" href="/{pfx}/list?page={p}{("&day=" + day) if day else ""}">{p}</a>'
        for p in range(1, total_pages + 1)
    )
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{ds.PLATFORM_META[platform]['name']} - 第{page}页</title></head>
<body><h1>{ds.PLATFORM_META[platform]['name']}</h1>
<div class="listing-list" data-total="{total}">{"".join(rows) or '<p class="empty">暂无车源</p>'}</div>
<nav class="pager">{pager}</nav>
<footer class="site-foot">真实车源 · 页面生成于 {day_str}</footer></body></html>"""


def render_detail_page(platform: str, l: ds.Listing, drift: bool = False,
                       day: date | None = None) -> str | None:
    snap = day or ds.TODAY
    price = l.current_price(snap)
    if price is None:
        return None
    fields = _detail_fields(l, drift, snap)
    rows = "\n".join(
        f'<div class="spec"><label>{k}</label><span class="v">{v}</span></div>'
        for k, v in fields.items()
    )
    imgs = ""
    if l.image_mode != "none":
        variant = "same" if l.image_mode == "same" else "alt"
        pfx = "p" + platform.lower()
        imgs = "".join(
            f'<img class="gallery-img" src="/{pfx}/img/{l.pid}.png?n={i}" alt="车况图{i}"/>'
            for i in range(1, 4)
        )
    title_class = "detail-title" if not drift else "headline-v2"
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{ds.title_for(l)}</title></head><body>
<h1 class="{title_class}">{ds.title_for(l)}</h1>
<div class="price-box" data-price="{price}"><span class="price-now">{_fmt_price(price)}</span></div>
<div class="specs">\n{rows}\n</div>
<div class="gallery">\n{imgs}\n</div>
<div class="seller">{l.eff_seller}</div>
</body></html>"""


# ---------------------------------------------------------------------------
# signed app API (platform D)
# ---------------------------------------------------------------------------
def d_sign(params: dict[str, str], secret: str) -> str:
    """HMAC-SHA256 over sorted key=value pairs excluding sign."""
    body = "&".join(f"{k}={params[k]}" for k in sorted(params) if k != "sign")
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def verify_d_sign(params: dict[str, str]) -> tuple[bool, str]:
    with RUNTIME.lock:
        if RUNTIME.__dict__.get("d_force_expired_once", 0) > 0:
            RUNTIME.d_force_expired_once -= 1
            return False, "signature_expired"
    sign = params.get("sign", "")
    expected = d_sign(params, RUNTIME.d_secret)
    if not hmac.compare_digest(sign, expected):
        return False, "signature_mismatch"
    try:
        ts = int(params.get("ts", "0"))
    except ValueError:
        return False, "signature_expired"
    now = int(time.time())
    if abs(now - ts) > RUNTIME.d_window_seconds:
        return False, "signature_expired"
    return True, "ok"


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "CarScoutFixture/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # silence per-request noise
        pass

    # -- helpers ----------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8",
              headers: dict[str, str] | None = None) -> None:
        if RUNTIME.response_delay_ms:
            time.sleep(RUNTIME.response_delay_ms / 1000.0)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: Any, headers: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", headers)

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route()
        except BrokenPipeError:
            pass

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path.split("?")[0] in {"/_fixture/control", "/_fixture/reset"}:
                return self._handle_control(self.path.split("?")[0])
            self._route()
        except BrokenPipeError:
            pass

    def _route(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        qs = {k: v[0] for k, v in parse_qs(parts.query).items()}
        platform = None
        if path.startswith("/pa/"):
            platform = "A"
        elif path.startswith("/pb/"):
            platform = "B"
        elif path.startswith("/pc/"):
            platform = "C"
        elif path.startswith("/pd/"):
            platform = "D"
        if path in {"/healthz", "/_fixture/reset", "/_fixture/control", "/_fixture/state"}:
            return self._handle_control(path)
        if path == "/pb/blank":
            return self._send(200, b"<!doctype html><html><head><meta charset='utf-8'></head><body>pb-blank</body></html>")
        if path == "/pd/app":
            return self._send(200, PD_APP_HTML.replace("__SECRET__", RUNTIME.d_secret).encode(),
                              "text/html; charset=utf-8",
                              {"Set-Cookie": "app_session=app-session-9f3c; Path=/; HttpOnly"})
        if platform is None:
            return self._send(404, b"not found", "text/plain")

        day = RUNTIME.snapshot_day
        if "day" in qs:
            try:
                day = date.fromisoformat(qs["day"])
            except ValueError:
                return self._send(400, b"bad day", "text/plain")

        if platform == "A":
            return self._platform_a(path, qs, day)
        if platform == "B":
            return self._platform_b(path, qs, day)
        if platform == "C":
            return self._platform_c(path, qs, day)
        if platform == "D":
            return self._platform_d(path, qs, day)
        return self._send(404, b"not found", "text/plain")

    def _read_body(self) -> dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode() or "{}")
        except Exception:
            return {}

    def _handle_control(self, path: str) -> None:
        if path == "/_fixture/state":
            return self._json(200, {
                "snapshot_day": RUNTIME.snapshot_day.isoformat(),
                "c_force_blocked": RUNTIME.c_force_blocked,
                "c_force_empty": RUNTIME.c_force_empty,
                "c_max_per_window": RUNTIME.c_max_per_window,
                "drift": dict(RUNTIME.drift),
                "request_log_tail": RUNTIME.request_log[-30:],
            })
        if path == "/_fixture/control":
            body = self._read_body()
            with RUNTIME.lock:
                if "snapshot_day" in body:
                    RUNTIME.snapshot_day = date.fromisoformat(body["snapshot_day"])
                for k in ("c_force_blocked", "c_force_empty", "c_max_per_window",
                          "c_window_seconds", "d_window_seconds", "d_force_expired_once",
                          "response_delay_ms"):
                    if k in body:
                        setattr(RUNTIME, k, body[k])
                if "d_secret" in body:
                    RUNTIME.d_secret = str(body["d_secret"])
                if "drift" in body and isinstance(body["drift"], dict):
                    RUNTIME.drift.update({str(k): bool(v) for k, v in body["drift"].items()})
            return self._json(200, {"ok": True})
        if path == "/healthz":
            return self._json(200, {"ok": True, "day": RUNTIME.snapshot_day.isoformat()})
        RUNTIME.reset()
        return self._json(200, {"ok": True, "reset": True})

    # -- platform A: static HTML + pagination -----------------------------
    def _platform_a(self, path: str, qs: dict[str, str], day: date) -> None:
        drift = RUNTIME.drift["A"]
        if path == "/pa/list" or path == "/pa/":
            page = max(1, int(qs.get("page", "1") or 1))
            page = 1 if path == "/pa/" else page
            items = ds.listings_on("A", day)
            size = ds.PLATFORM_META["A"]["page_size"]
            pages = max(1, (len(items) + size - 1) // size)
            chunk = items[(page - 1) * size: page * size]
            RUNTIME.record("A", path, "list", page=page, n=len(chunk))
            return self._send(200, render_list_page("A", chunk, page, pages, drift, total=len(items),
                                                     day=qs.get("day"), snapshot=day).encode())
        if path.startswith("/pa/detail/"):
            pid = path.split("?", 1)[0].rsplit("/", 1)[-1]
            l = ds.listing_by_pid("A", pid)
            if l is None or l.current_price(day) is None:
                RUNTIME.record("A", path, "missing")
                return self._send(410, "<html><body>该车源已下架</body></html>".encode())
            RUNTIME.record("A", path, "detail", pid=pid)
            return self._send(200, render_detail_page("A", l, drift, day).encode())
        if path.startswith("/pa/img/"):
            pid = path.rsplit("/", 1)[-1].split(".")[0]
            L = ds.listing_by_pid("A", pid)
            variant = "same" if (L and L.image_mode == "same") else "alt"
            cid = L.car.cid if L else pid
            body = car_image_png(cid, variant)
            return self._send(200, body, "image/png",
                              {"ETag": '"' + hashlib.md5(body).hexdigest() + '"',
                               "Cache-Control": "max-age=86400"})
        return self._send(404, b"not found", "text/plain")

    # -- platform B: rendered shell; scroll triggers XHR -------------------
    def _platform_b(self, path: str, qs: dict[str, str], day: date) -> None:
        drift = RUNTIME.drift["B"]
        if path == "/pb/list" or path == "/pb/":
            size = ds.PLATFORM_META["B"]["page_size"]
            total = len(ds.listings_on("B", day))
            pages = max(1, (total + size - 1) // size)
            shell = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{ds.PLATFORM_META['B']['name']}</title>
<script>
window.__PB_PAGES__ = {pages};
window.__PB_LOADED__ = 0;
window.__pbScroll = function(next) {{
  if (window.__PB_LOADED__ >= window.__PB_PAGES__) return Promise.resolve(null);
  window.__PB_LOADED__ += 1;
  return fetch('/pb/api/list?page=' + window.__PB_LOADED__ + '&day={day.isoformat()}&t=' + Date.now(), {{
    credentials: 'include'
  }}).then(r => r.json());
}};
</script></head><body><h1>{ds.PLATFORM_META['B']['name']}</h1>
<div id="grid" class="scroll-grid"></div>
<div id="sentinels" class="sentinel"></div>
</body></html>"""
            RUNTIME.record("B", path, "list_shell")
            return self._send(200, shell.encode())
        if path == "/pb/api/list":
            page = max(1, int(qs.get("page", "1") or 1))
            size = ds.PLATFORM_META["B"]["page_size"]
            items = ds.listings_on("B", day)
            chunk = items[(page - 1) * size: page * size]
            payload = {"page": page, "pageSize": size, "hasMore": page * size < len(items),
                       "items": [_b_list_item(l, drift, day) for l in chunk]}
            RUNTIME.record("B", path, "xhr_list", page=page, n=len(chunk))
            return self._json(200, payload)
        if path == "/pb/api/detail":
            pid = qs.get("id", "")
            l = ds.listing_by_pid("B", pid)
            if l is None or l.current_price(day) is None:
                RUNTIME.record("B", path, "missing", pid=pid)
                return self._json(200, {"code": 404, "msg": "车源不存在", "data": None})
            RUNTIME.record("B", path, "xhr_detail", pid=pid)
            return self._json(200, {"code": 0, "msg": "ok", "data": _b_detail(l, drift, day)})
        if path.startswith("/pb/img/"):
            pid = path.rsplit("/", 1)[-1].split(".")[0]
            L = ds.listing_by_pid("B", pid)
            variant = "same" if (L and L.image_mode == "same") else "alt"
            cid = L.car.cid if L else pid
            return self._send(200, car_image_webp(cid, variant), "image/webp")
        return self._send(404, b"not found", "text/plain")

    # -- platform C: anti-bot wall ---------------------------------------
    def _platform_c(self, path: str, qs: dict[str, str], day: date) -> None:
        allowed = RUNTIME.c_take_token()
        if path in {"/pc/list", "/pc/"}:
            page = max(1, int(qs.get("page", "1") or 1))
            page = 1 if path == "/pc/" else page
            if not allowed:
                # 200 + empty list: blocked, indistinguishable from "no cars"
                RUNTIME.record("C", path, "blocked_empty_200", page=page)
                return self._send(200, f"""<!doctype html><html><head><meta charset="utf-8">
<title>{ds.PLATFORM_META['C']['name']}</title></head><body>
<div class="listing-list"></div><p class="hint">暂无符合条件的车源</p></body></html>""".encode())
            items = [] if RUNTIME.c_force_empty else ds.listings_on("C", day)
            size = ds.PLATFORM_META["C"]["page_size"]
            pages = max(1, (len(items) + size - 1) // size)
            chunk = items[(page - 1) * size: page * size]
            RUNTIME.record("C", path, "list", page=page, n=len(chunk))
            return self._send(200, render_list_page("C", chunk, page, pages, RUNTIME.drift["C"],
                                                     total=len(items), day=qs.get("day"), snapshot=day).encode())
        if path.startswith("/pc/detail/"):
            pid = path.split("?", 1)[0].rsplit("/", 1)[-1]
            if not allowed:
                RUNTIME.record("C", path, "blocked_empty_200", pid=pid)
                return self._send(200, """<!doctype html><html><body><div class="detail-empty"></div></body></html>""".encode())
            l = ds.listing_by_pid("C", pid)
            if l is None or l.current_price(day) is None:
                RUNTIME.record("C", path, "missing", pid=pid)
                return self._send(404, "<html><body>车源不存在</body></html>".encode())
            RUNTIME.record("C", path, "detail", pid=pid)
            return self._send(200, render_detail_page("C", l, RUNTIME.drift["C"], day).encode())
        if path.startswith("/pc/img/"):
            pid = path.rsplit("/", 1)[-1].split(".")[0]
            L = ds.listing_by_pid("C", pid)
            variant = "same" if (L and L.image_mode == "same") else "alt"
            cid = L.car.cid if L else pid
            return self._send(200, car_image_png(cid, variant), "image/png")
        return self._send(404, b"not found", "text/plain")

    # -- platform D: signed app API ---------------------------------------
    def _platform_d(self, path: str, qs: dict[str, str], day: date) -> None:
        if path == "/pd/api/session":
            # delivers a browser-visitable shell that mints the app session
            self._json(200, {
                "ok": True,
                "sessionKey": "app-session-9f3c",
                "signSecretHint": RUNTIME.d_secret[:10] + "...",
                "windowSeconds": RUNTIME.d_window_seconds,
            }, {"Set-Cookie": "app_session=app-session-9f3c; Path=/; HttpOnly"})
            return
        if path.startswith("/pd/img/"):
            pid = path.rsplit("/", 1)[-1].split(".")[0]
            L = ds.listing_by_pid("D", pid)
            variant = "same" if (L and L.image_mode == "same") else "alt"
            cid = L.car.cid if L else pid
            RUNTIME.record("D", path, "img", pid=pid)
            return self._send(200, car_image_png(cid, variant), "image/png")
        if path not in {"/pd/api/list", "/pd/api/detail"}:
            return self._send(404, b"not found", "text/plain")
        ok, reason = verify_d_sign(qs)
        if not ok:
            RUNTIME.record("D", path, f"rejected_{reason}")
            return self._json(403, {"code": "SIGN_INVALID", "reason": reason})
        # a signed request must also carry the browser session cookie
        cookie = self.headers.get("Cookie", "")
        if "app_session=app-session-9f3c" not in cookie:
            RUNTIME.record("D", path, "rejected_no_session")
            return self._json(401, {"code": "SESSION_REQUIRED", "reason": "no_session"})
        if path == "/pd/api/list":
            page = max(1, int(qs.get("page", "1") or 1))
            size = ds.PLATFORM_META["D"]["page_size"]
            items = ds.listings_on("D", day)
            chunk = items[(page - 1) * size: page * size]
            RUNTIME.record("D", path, "api_list", page=page, n=len(chunk))
            return self._json(200, {"code": 0, "data": {"page": page, "total": len(items),
                                                       "hasMore": page * size < len(items),
                                                       "items": [_d_item(l, RUNTIME.drift["D"], day) for l in chunk]}})
        pid = qs.get("id", "")
        l = ds.listing_by_pid("D", pid)
        if l is None or l.current_price(day) is None:
            RUNTIME.record("D", path, "missing", pid=pid)
            return self._json(200, {"code": 404, "msg": "listing not found", "data": None})
        RUNTIME.record("D", path, "api_detail", pid=pid)
        return self._json(200, {"code": 0, "data": _d_detail(l, RUNTIME.drift["D"], day)})
        if path.startswith("/pd/img/"):
            pass

    # image endpoint for D handled here (path prefix differs from listing pids)
    def _platform_d_img(self, path: str) -> None:
        pid = path.rsplit("/", 1)[-1].split(".")[0]
        return self._send(200, car_image_png(pid, "same"), "image/png")


# ---------------------------------------------------------------------------
# payload shapes (deliberately different per platform)
# ---------------------------------------------------------------------------
def _b_list_item(l: ds.Listing, drift: bool, day: date = ds.TODAY) -> dict[str, Any]:
    item = {
        "id": l.pid,
        "title": ds.title_for(l, "B"),
        "priceWan": round((l.current_price(day) or 0) / 10000, 2),
        "cityName": l.eff_city,
        "mileageWan": round(l.eff_mileage / 10000, 1),
        "cover": f"/pb/img/{l.pid}.webp",
        "vinTail6": (l.car.vin or "")[-6:] if l.vin_mode == "tail6" else None,
    }
    if drift:
        item["priceWan"] = None
    return item


def _b_detail(l: ds.Listing, drift: bool, day: date = ds.TODAY) -> dict[str, Any]:
    car = l.car
    reg_y, reg_m = l.eff_reg
    d = {
        "id": l.pid,
        "title": ds.title_for(l, "B"),
        "brandName": None if drift else car.brand,
        "seriesName": car.series,
        "modelName": car.model,
        "regDate": f"{reg_y}-{reg_m:02d}-01",
        "mileageKm": l.eff_mileage,
        "displacement": car.displacement / 1000 if car.displacement else 0,
        "gearbox": car.transmission if not drift else None,
        "ownerCount": l.eff_transfer,
        "cityName": l.eff_city,
        "priceWan": round((l.current_price(day) or 0) / 10000, 2),
        "publishTime": l.listed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "sellerType": l.eff_seller,
        "description": ds.description_for(l),
        "vin": l.vin_value,
        "images": ([f"/pb/img/{l.pid}.webp?n={i}" for i in range(1, 4)] if l.image_mode != "none" else []),
        "transferTimes": l.eff_transfer,
    }
    return d


def _d_item(l: ds.Listing, drift: bool, day: date = ds.TODAY) -> dict[str, Any]:
    car = l.car
    reg_y, reg_m = l.eff_reg
    return {
        "listingId": l.pid,
        "carName": f"{car.brand} {car.series} {car.model}" if not drift else f"{car.series}",
        "price": (l.current_price(day) or 0) * 100,          # fen
        "cityCode": l.eff_city,
        "odometer": l.eff_mileage,
        "registerDate": f"{reg_y}{reg_m:02d}",
        "picCount": 0 if l.image_mode == "none" else 3,
        "dealerType": "DEALER" if l.eff_seller == "商家" else "PRIVATE",
        "coverUrl": None if l.image_mode == "none" else f"/pd/img/{l.pid}.png",
    }


def _d_detail(l: ds.Listing, drift: bool, day: date = ds.TODAY) -> dict[str, Any]:
    car = l.car
    reg_y, reg_m = l.eff_reg
    return {
        "listingId": l.pid,
        "brandName": None if drift else car.brand,
        "seriesName": car.series,
        "modelName": car.model,
        "firstRegisterDate": f"{reg_y}-{reg_m:02d}",
        "odometerKm": l.eff_mileage,
        "engineDisplacementMl": None if drift else car.displacement,
        "transmission": None if drift else car.transmission,
        "transferTimes": l.eff_transfer,
        "cityName": l.eff_city,
        "priceFen": None if drift else (l.current_price(day) or 0) * 100,
        "publishTime": l.listed_at.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "dealerType": "DEALER" if l.eff_seller == "商家" else "PRIVATE",
        "descriptionText": ds.description_for(l),
        "vin": l.vin_value,
        "images": ([] if l.image_mode == "none" else [f"/pd/img/{l.pid}.png?n={i}" for i in range(1, 4)]),
    }


# ---------------------------------------------------------------------------
# platform D in-browser app shell: mints signatures with the embedded app secret
# ---------------------------------------------------------------------------
PD_APP_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>平台D · App</title></head><body>
<div id="pd-root">loading</div>
<script>
const PD_SECRET = "__SECRET__";
const PD_UID = "device-7a2f9c";
async function pdHmac(secret, msg) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
      {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(msg));
  return Array.from(new Uint8Array(sig)).map(b => b.toString(16).padStart(2, "0")).join("");
}
async function pdSigned(params) {
  const keys = Object.keys(params).filter(k => k !== "sign").sort();
  const body = keys.map(k => k + "=" + params[k]).join("&");
  const out = Object.assign({}, params);
  out.sign = await pdHmac(PD_SECRET, body);
  return out;
}
async function pdCall(path, params, tries) {
  tries = tries || 3;
  const base = {uid: PD_UID, ts: Math.floor(Date.now() / 1000)};
  const merged = Object.assign(base, params || {});
  let qs = await pdSigned(merged);
  for (let i = 0; i < tries; i++) {
    const url = path + "?" + Object.keys(qs).sort().map(k => k + "=" + encodeURIComponent(qs[k])).join("&");
    const r = await fetch(url, {credentials: "include"});
    const j = await r.json();
    if (r.status === 403) {
      // signature window expired: re-sign with a fresh timestamp and retry
      merged.ts = Math.floor(Date.now() / 1000);
      qs = await pdSigned(merged);
      continue;
    }
    return j;
  }
  return {code: "SIGN_RETRY_EXHAUSTED"};
}
// single call helper used by the crawler: returns {status, body} without throwing
async function pdAttempt(path, params) {
  const base = {uid: PD_UID, ts: Math.floor(Date.now() / 1000)};
  const merged = Object.assign(base, params || {});
  let qs = await pdSigned(merged);
  for (let i = 0; i < 2; i++) {
    const url = path + "?" + Object.keys(qs).sort()
        .map(k => k + "=" + encodeURIComponent(qs[k])).join("&");
    const r = await fetch(url, {credentials: "include"});
    let body = null;
    try { body = await r.json(); } catch (e) { body = {code: "NON_JSON"}; }
    if (r.status === 403) {
      merged.ts = Math.floor(Date.now() / 1000);
      qs = await pdSigned(merged);
      if (body && body.reason === "signature_mismatch") return {status: r.status, body: body};
      continue;
    }
    return {status: r.status, body: body};
  }
  return {status: 403, body: {code: "SIGN_INVALID", reason: "retry_exhausted"}};
}
async function pdFetchList(day) {
  const items = [];
  for (let page = 1; page <= 20; page++) {
    const j = await pdCall("/pd/api/list", {page: page, day: day});
    if (!j || j.code !== 0 || !j.data) return {error: j, items: items};
    items.push.apply(items, j.data.items || []);
    if (!j.data.hasMore) break;
  }
  return {items: items};
}
async function pdFetchDetail(id, day) {
  const j = await pdCall("/pd/api/detail", {id: id, day: day});
  if (!j || j.code !== 0) return {error: j, data: null};
  return {data: j.data};
}
window.__pdReady = true;
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------
def make_server(port: int = 8088, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), FixtureHandler)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="CarScout multi-platform fixture server")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--day", help="snapshot date, e.g. 2026-11-12")
    args = ap.parse_args()
    if args.day:
        RUNTIME.snapshot_day = date.fromisoformat(args.day)
    server = make_server(args.port, args.host)
    print(f"fixture server listening on http://{args.host}:{args.port} (snapshot {RUNTIME.snapshot_day})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
