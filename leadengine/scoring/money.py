"""Money model — har lead ki asli qeemat.

Deal value, monthly retainer, LTV aur expected value. Saare numbers
worker/config/pricing.json se aate hain (dashboard se editable).
"""

from __future__ import annotations

from leadengine.audit.gmb import SUBTRACK_SOCIAL, TRACK_SEO, TRACK_WEBSITE
from leadengine.settings import load_config

PRICING = load_config("pricing.json")


def reload_pricing() -> None:
    """Settings screen se file badle to dobara load karne ke liye."""
    global PRICING
    PRICING = load_config("pricing.json")


# ---------------------------------------------------------------- tier

def detect_tier(lead: dict) -> tuple[str, str]:
    """Category/niche text se pricing tier nikalo. -> (tier, matched keyword)"""
    haystack = " ".join(
        [
            str(lead.get("category") or ""),
            str(lead.get("search_niche") or ""),
            str(lead.get("name") or ""),
        ]
    ).lower()

    best: tuple[str, str] | None = None
    for tier in ("A", "B", "C"):
        for keyword in PRICING["tiers"][tier]["keywords"]:
            if keyword in haystack:
                # Pehla (sabse mehnga) tier jeet jata hai.
                if best is None:
                    best = (tier, keyword)
                break
        if best:
            break
    return best or (PRICING["default_tier"], "")


def review_band(review_count: int | None) -> dict:
    reviews = review_count or 0
    for band in PRICING["review_bands"]:
        if band["min"] <= reviews <= band["max"]:
            return band
    return PRICING["review_bands"][-1]


def gap_key(track: str, subtrack: str | None, presence_score: int, reachable: bool) -> str:
    if track == TRACK_WEBSITE:
        return "social_only" if subtrack == SUBTRACK_SOCIAL else "no_website"
    if not reachable:
        return "site_down"
    if presence_score < 50:
        return "site_bad"
    if presence_score < 80:
        return "site_average"
    return "site_good"


def _blend(price_range: list[int], multiplier: float) -> int:
    """Range ke andar multiplier ke hisaab se ek number pe pohancho, 50 pe round."""
    low, high = price_range
    factor = max(0.0, min(1.4, multiplier)) / 1.4
    value = low + (high - low) * factor
    return int(round(value / 50.0) * 50)


def price_lead(lead: dict, audit: dict) -> dict:
    """Ek lead ka poora money model."""
    tier, matched = detect_tier(lead)
    tier_conf = PRICING["tiers"][tier]
    band = review_band(lead.get("review_count"))
    track = audit.get("track", TRACK_WEBSITE)
    subtrack = audit.get("subtrack")
    site = audit.get("site") or {}
    presence = audit.get("score", 0)
    reachable = bool(site.get("reachable", track == TRACK_SEO))

    gap = gap_key(track, subtrack, presence, reachable)
    gap_mult = PRICING["gap_multipliers"][gap]
    combined = band["multiplier"] * gap_mult

    ltv_months = PRICING["ltv_months"]

    if track == TRACK_WEBSITE:
        deal_type = "WEBSITE"
        deal_value = _blend(tier_conf["website_price"], combined)
        retainer = int(
            round(
                _blend(tier_conf["retainer_monthly"], combined)
                * PRICING["website_maintenance_share"]
                / 25.0
            )
            * 25
        )
    else:
        deal_type = "SEO"
        retainer = _blend(tier_conf["retainer_monthly"], combined)
        deal_value = retainer * PRICING["seo_setup_fee_months"]

    ltv = retainer * ltv_months
    return {
        "tier": tier,
        "tier_label": tier_conf["label"],
        "tier_matched_keyword": matched,
        "review_band": band["label"],
        "gap": gap,
        "deal_type": deal_type,
        "deal_value_usd": deal_value,
        "retainer_monthly_usd": retainer,
        "ltv_usd": ltv,
        "contract_value_usd": deal_value + ltv,
        "currency": PRICING["currency"],
        "currency_symbol": PRICING["currency_symbol"],
    }


def close_probability(urgency: int) -> float:
    return PRICING["close_probability_by_urgency"].get(str(urgency), 0.05)


def expected_value(money: dict, urgency: int) -> int:
    return int(round(money["contract_value_usd"] * close_probability(urgency)))
