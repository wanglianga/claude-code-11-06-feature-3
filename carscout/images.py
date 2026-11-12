"""Remote image fetching + perceptual hashing (used by the crawl engine)."""
from __future__ import annotations

import logging
from functools import lru_cache
from io import BytesIO

import httpx
from PIL import Image

from carscout.utils import phash_hex

log = logging.getLogger(__name__)
_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=8.0, follow_redirects=True, headers={
            "User-Agent": "CarScout/1.0 (+crawler; contact=ops@carscout.local)"})
    return _client


@lru_cache(maxsize=4096)
def phash_remote(url: str) -> tuple[str, int, int] | None:
    """Fetch an image once and return (phash_hex, width, height)."""
    if not url:
        return None
    try:
        resp = _get_client().get(url)
        if resp.status_code != 200 or not resp.content:
            return None
        img = Image.open(BytesIO(resp.content))
        img.load()
        w, h = img.size
        return phash_hex(img), w, h
    except Exception as exc:  # noqa: BLE001
        log.debug("image hash failed for %s: %s", url, exc)
        return None


def load_image_from_bytes(data: bytes) -> Image.Image:
    img = Image.open(BytesIO(data))
    img.load()
    return img
