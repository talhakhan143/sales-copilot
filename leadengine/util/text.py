"""Small parsing helpers shared across scraper, audit and scoring."""

from __future__ import annotations

import re
import unicodedata

_RATING_RE = re.compile(r"(\d[.,]\d)\s*(?:stars?)?", re.I)
_REVIEWS_RE = re.compile(r"([\d.,\u00a0\s]+)\s*(?:reviews?|ratings?)", re.I)
_PARENS_NUM_RE = re.compile(r"\(([\d.,\u00a0\s]+)\)")
_COORDS_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
_FEATURE_ID_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.I)
_PLACE_NAME_RE = re.compile(r"/maps/place/([^/@]+)")


def clean(value: str | None) -> str:
    """Collapse whitespace and normalise unicode spaces."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


def to_int(value: str | None) -> int | None:
    """'1,234' / '1 234' / '1.234' -> 1234."""
    if not value:
        return None
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None


def to_float(value: str | None) -> float | None:
    """'4,6' or '4.6' -> 4.6."""
    if not value:
        return None
    match = re.search(r"\d[.,]?\d*", value)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def parse_rating(text: str | None) -> float | None:
    if not text:
        return None
    match = _RATING_RE.search(text)
    return to_float(match.group(1)) if match else None


def parse_review_count(text: str | None) -> int | None:
    """Handles '(1,234)', '1,234 reviews', '1.234 Rezensionen'."""
    if not text:
        return None
    match = _REVIEWS_RE.search(text) or _PARENS_NUM_RE.search(text)
    return to_int(match.group(1)) if match else None


def parse_coords(url: str | None) -> tuple[float | None, float | None]:
    """Pull lat/lng out of a Google Maps place URL."""
    if not url:
        return None, None
    match = _COORDS_RE.search(url)
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


def parse_feature_id(url: str | None) -> str | None:
    """Google's stable feature id (0x…:0x…). Used as our unique place key."""
    if not url:
        return None
    match = _FEATURE_ID_RE.search(url)
    return match.group(1).lower() if match else None


def place_slug(url: str | None) -> str:
    """Readable fallback key from the URL path."""
    if not url:
        return ""
    match = _PLACE_NAME_RE.search(url)
    return match.group(1).replace("+", " ") if match else ""


def strip_price_level(text: str | None) -> str | None:
    """'$$' / '$10-20' -> normalised price marker."""
    if not text:
        return None
    match = re.search(r"[$€£¥₹]{1,4}", text)
    return match.group(0) if match else clean(text) or None
