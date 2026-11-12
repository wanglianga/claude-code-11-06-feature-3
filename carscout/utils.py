"""Pure utilities: URL normalisation, fingerprints, text/pHash similarity helpers."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = re.compile(
    r"^(utm_|spm$|from$|src$|source$|ref$|refid$|share_|shareid$|channel$|"
    r"track|trace|click|_t$|timestamp$|t$|signature$|sign$|ts$|nonce$)"
)
KEEP_QUERY_PARAMS = {"page", "pagesize", "city", "brand", "series", "offset", "limit", "id"}


def normalize_url(url: str) -> str:
    """Canonical form used for request fingerprints and duplicate listing detection.

    - lower-case scheme/host, drop default ports and fragments
    - sort query parameters, drop tracking/signature/time parameters
    - collapse repeated slashes and trailing slashes in path
    """
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "http:" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower() or "http"
    host = parts.hostname or ""
    netloc = host
    if parts.port and not ((scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)):
        netloc = f"{host}:{parts.port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        low = key.lower()
        if TRACKING_PARAMS.search(low) or (low in {"sign", "signature", "nonce", "ts", "timestamp"}):
            continue
        if low not in KEEP_QUERY_PARAMS:
            # keep unknown but meaningful params, just sorted
            pass
        pairs.append((low, value))
    pairs.sort()
    query = urlencode(pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_fingerprint(kind: str, url_norm: str, fields: dict[str, Any]) -> str:
    """Incremental crawl fingerprint: normalised URL + hashed key business fields."""
    payload = {"kind": kind, "url": url_norm, "fields": fields}
    return sha256_text(canonical_json(payload))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).isoformat()


# ---------------------------------------------------------------------------
# text similarity
# ---------------------------------------------------------------------------
_PUNCT = re.compile(r"[\s,，。;；:：!！?？、\-—_'\"“”‘’()（）\[\]【】<>《》/\\|+*#·~]+")
_NOISE = re.compile(r"(一手车|私家车|个人一手|车况精品|无事故|诚意出售|可议价|接受检测|看车方便|支持检测|"
                    r"女士一手|原版原漆|价格可谈|手续齐全|随时过户|欢迎咨询|详情私聊|诚心转让)")


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    text = _PUNCT.sub("", text)
    text = _NOISE.sub("", text)
    return text


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def text_similarity(a: str | None, b: str | None) -> float:
    """Blend of character bigram Jaccard and difflib ratio, robust to rephrasing."""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    import difflib

    bg_a, bg_b = _bigrams(na), _bigrams(nb)
    jac = len(bg_a & bg_b) / max(1, len(bg_a | bg_b))
    ratio = difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()
    return round(max(0.0, 0.55 * jac + 0.45 * ratio), 4)


def series_similarity(a: str | None, b: str | None) -> float:
    """Brand/series string similarity, tolerant to suffix ordering and spacing."""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    import difflib

    ratio = difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()
    if na in nb or nb in na:
        ratio = max(ratio, 0.9)
    return round(ratio, 4)


# ---------------------------------------------------------------------------
# perceptual hash (64-bit DCT pHash), pure numpy implementation
# ---------------------------------------------------------------------------
def phash_hex(image) -> str:
    """Return 16-hex-char (64-bit) DCT perceptual hash of a PIL image."""
    import numpy as np
    from PIL import Image

    if isinstance(image, np.ndarray):  # pre-computed 64-bit vectors
        arr = image.astype(np.uint64)
        val = int(arr[0])
        return format(val, "016x")

    img = image.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
    arr = np.asarray(img, dtype=np.float64)
    n = 32
    k = np.arange(n)
    dct_m = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n))
    dct_m[0, :] *= 1 / np.sqrt(2)
    dct = dct_m.T @ arr @ dct_m
    low = dct[:8, :8].flatten()
    med = np.median(low[1:])
    bits = (low > med).astype(np.uint8)
    val = 0
    for bit in bits:
        val = (val << 1) | int(bit)
    return format(val, "016x")


def hamming_hex(a: str | None, b: str | None) -> int:
    if not a or not b:
        return 64
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 64


def phash_similarity(hamming_distance: int) -> float:
    """Map Hamming distance to a [0,1] similarity. <=3 near-identical, >=18 unrelated."""
    d = max(0, min(64, int(hamming_distance)))
    if d <= 3:
        return 0.97
    if d >= 18:
        return 0.05
    return round(0.97 - (d - 3) * (0.92 / 15), 4)


def hash_to_bit_literal(phash: str) -> str:
    """16-hex-char (64-bit) pHash -> 64-character '0'/'1' string for bit(64)."""
    h = format(int(phash, 16), "016x")     # normalise width first
    return format(int(h, 16), "064b")


def price_median(values: Iterable[int]) -> float | None:
    vals = sorted(int(v) for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0
