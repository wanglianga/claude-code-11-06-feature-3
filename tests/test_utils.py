import hashlib

import numpy as np
import pytest
from PIL import Image, ImageDraw

from carscout.utils import (canonical_json, content_fingerprint, hamming_hex,
                            hash_to_bit_literal, normalize_url, phash_hex,
                            phash_similarity, series_similarity, text_similarity)


def test_url_normalisation_strips_tracking_and_sorts():
    raw = "HTTP://Example.COM:80//a//b/?utm_source=x&b=2&a=1&sign=abc&ts=123#frag"
    norm = normalize_url(raw)
    assert norm == "http://example.com/a/b?a=1&b=2"


def test_url_normalisation_keeps_meaningful_pagination():
    norm = normalize_url("http://h/p/list?page=3&pagesize=20&_t=99")
    assert "page=3" in norm and "pagesize=20" in norm and "_t" not in norm


def test_fingerprint_is_stable_and_order_independent():
    a = content_fingerprint("detail", "/x", {"b": 2, "a": 1})
    b = content_fingerprint("detail", "/x", {"a": 1, "b": 2})
    assert a == b
    c = content_fingerprint("detail", "/x", {"a": 1, "b": 3})
    assert a != c


def test_text_similarity_rewards_rephrasing():
    same_car = text_similarity("个人一手车，实表3万公里，车况精品，随时看车",
                               "一手车源，实跑3万公里，车况很好，看车方便")
    other = text_similarity("个人一手车，实表3万公里，车况精品，随时看车",
                            "七座SUV，柴油版，拉货专用，手续不全")
    assert same_car > other


def test_series_similarity_handles_suffix_cut():
    assert series_similarity("宝马3系", "宝马3系") == 1.0
    assert series_similarity("大众迈腾", "迈腾") >= 0.9
    assert series_similarity("宝马3系", "奥迪A4L") < 0.6


def test_phash_is_deterministic_and_scale_invariant():
    img = Image.new("RGB", (200, 200), (10, 10, 10))
    d = ImageDraw.Draw(img)
    d.rectangle([20, 60, 180, 140], fill=(240, 200, 30))
    h1 = phash_hex(img)
    h2 = phash_hex(img.resize((160, 160), Image.LANCZOS))
    assert h1 == h2            # scale invariant by construction (32x32 DCT)
    assert len(h1) == 16


def test_phash_distinguishes_unrelated_images():
    a = Image.new("RGB", (64, 64), (0, 0, 0))
    b = Image.new("RGB", (64, 64), (255, 255, 255))
    da = ImageDraw.Draw(a)
    da.rectangle([0, 0, 32, 64], fill=(255, 255, 255))
    db = ImageDraw.Draw(b)
    db.ellipse([8, 8, 56, 56], fill=(0, 0, 0))
    dist = hamming_hex(phash_hex(a), phash_hex(b))
    assert dist > 8


def test_phash_similarity_monotonic():
    assert phash_similarity(0) > phash_similarity(8) > phash_similarity(30)


def test_bit_literal_roundtrip():
    h = "aa959654e8cb8b59"
    lit = hash_to_bit_literal(h)
    assert len(lit) == 64                  # bit(64) literal
    assert set(lit) <= {"0", "1"}
    assert format(int(lit, 2), "016x") == h
