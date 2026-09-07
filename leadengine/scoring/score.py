"""Lead scoring.

Teen alag numbers nikaltay hain, kyunke ek hi number sab kuch nahi bata sakta:

  need_score   0-100  "in ko kitni zarurat hai"      -> ORDER isi se banta hai
  money_score  0-100  "deal kitna bara hai"
  lead_score   0-100  dono ka mix (headline number)
  priority     now / week / later  -> UI me teen bucket

AHEM: WEBSITE aur SEO leads ke need_score aapas me compare nahi karne chahiye.
Website na hona hamesha kharab site se zyada "need" hai, is liye ek hi list me
website wale hamesha upar aa jate the. Isi wajah se UI me dono track alag
lanes me dikhte hain, aur ye scores sirf apni lane ke andar compare hote hain.
"""

from __future__ import annotations

import math
import statistics

from leadengine.audit.gmb import SUBTRACK_SOCIAL, TRACK_SEO, TRACK_WEBSITE

# Kitne reviews pe "revenue signal" poora ho jata hai.
REVIEWS_FULL_AT = 400

# Deal ki qeemat -> 0-100 (absolute bands, dataset pe depend nahi karta).
MONEY_BANDS = [
    (2_000, 20),
    (5_000, 40),
    (10_000, 60),
    (20_000, 80),
    (10**9, 100),
]

# Ye findings site ko practically bekaar kar dete hain.
BLOCKER_CODES = {"SITE_DOWN", "NO_HTTPS", "NO_VIEWPORT"}
# Ye customers ko rokte hain lekin site chalti rehti hai.
SERIOUS_CODES = {"SLOW_LOAD", "H_SCROLL", "NO_PHONE_LINK", "NAP_MISMATCH", "THIN_CONTENT"}

PRIORITY_NOW = "now"
PRIORITY_WEEK = "week"
PRIORITY_LATER = "later"

# Dono track ke need scores alag paimane pe hain (website na hona hamesha
# kharab site se zyada need hai), is liye bucket ki hadd bhi alag hai.
# Ye numbers asli data ki distribution dekh kar rakhe gaye hain.
PRIORITY_CUTOFFS = {
    TRACK_WEBSITE: (80, 68),
    TRACK_SEO: (55, 38),
}

# Ye masle site ko practically bekaar kar dete hain - inka apna farsh hai,
# chahe baqi site kitni bhi theek ho.
NEED_FLOOR = {
    "SITE_DOWN": 100,
    "NO_HTTPS": 82,
    "NO_VIEWPORT": 78,
}

PRIORITY_LABEL = {
    PRIORITY_NOW: "Do today",
    PRIORITY_WEEK: "This week",
    PRIORITY_LATER: "Later",
}


# ------------------------------------------------------------------ context

def market_context(leads: list[dict], audits: dict) -> dict:
    """Poore search ka manzar. Google ka apna result order hi local rank hai."""
    reviews = [l.get("review_count") or 0 for l in leads]
    with_reviews = [r for r in reviews if r > 0]
    ratings = [l.get("rating") for l in leads if l.get("rating")]

    ranked = []
    for position, lead in enumerate(leads, start=1):
        key = lead.get("feature_id") or lead.get("gmb_url")
        audit = audits.get(key, {})
        ranked.append(
            {
                "key": key,
                "rank": position,
                "name": lead.get("name", ""),
                "reviews": lead.get("review_count") or 0,
                "rating": lead.get("rating"),
                "has_website": audit.get("track") == TRACK_SEO,
                "site_score": audit.get("score", 0),
            }
        )

    leaders = sorted(
        [r for r in ranked if r["has_website"]],
        key=lambda r: (-r["reviews"], r["rank"]),
    )[:3]

    return {
        "total_leads": len(leads),
        "median_reviews": int(statistics.median(with_reviews)) if with_reviews else 0,
        "max_reviews": max(reviews) if reviews else 0,
        "median_rating": round(statistics.median(ratings), 1) if ratings else None,
        "with_website": sum(1 for r in ranked if r["has_website"]),
        "without_website": sum(1 for r in ranked if not r["has_website"]),
        "leaders": leaders,
        "rank_by_key": {r["key"]: r["rank"] for r in ranked},
        "ranked": ranked,
    }


# --------------------------------------------------------------- components

def _reviews_signal(review_count: int | None) -> float:
    """0-1. Reviews = customers = paisa mojood hai. Log scale."""
    reviews = review_count or 0
    if reviews <= 0:
        return 0.0
    return min(1.0, math.log10(reviews + 1) / math.log10(REVIEWS_FULL_AT))


def _codes(audit: dict) -> set[str]:
    codes = {f["code"] for f in (audit.get("site") or {}).get("findings", [])}
    codes |= {f["code"] for f in audit.get("gmb_findings", [])}
    return codes


def money_score(contract_value: int) -> int:
    for ceiling, points in MONEY_BANDS:
        if contract_value < ceiling:
            return points
    return 100


# ------------------------------------------------------------------- need

def need_score(lead: dict, audit: dict, context: dict) -> tuple[int, str]:
    """Kitni zarurat hai (0-100) + ek line me wajah.

    Har track ka apna paimana hai — dono ko aapas me compare mat karna.
    """
    codes = _codes(audit)
    reviews = lead.get("review_count") or 0
    signal = _reviews_signal(reviews)
    rank = context.get("rank_by_key", {}).get(lead.get("feature_id") or lead.get("gmb_url"))
    median = context.get("median_reviews") or 0

    if audit.get("track") == TRACK_WEBSITE:
        social = audit.get("subtrack") == SUBTRACK_SOCIAL
        # Website hi nahi = buniyadi kami. Baaki farq ye hai ke kitna paisa
        # zaya ho raha hai (reviews) aur unhe khud kitna ehsaas hai (social page).
        score = 45 + round(35 * signal)
        if social:
            score += 12          # social page bana chuke hain = zarurat maan chuke hain
        if not lead.get("is_claimed", True):
            score += 5
        if not lead.get("hours"):
            score += 3

        # Har lead ki sabse alag baat uthao - warna 30 leads ek jaisi line
        # dikhati hain aur nazar phisal jati hai.
        rating = lead.get("rating")
        if social:
            why = (
                "A social page stands in for a website — Google does not treat it "
                "as one."
            )
        elif rank and rank <= 3:
            why = (
                f"Ranks #{rank} in this search — right at the top — and still has no "
                "website. Anyone who clicks through finds nothing."
            )
        elif reviews >= 500:
            why = (
                f"A {reviews}-review business running with no website at all. "
                "That much demand and nowhere to send it."
            )
        elif rating and rating >= 4.5 and reviews >= 40:
            why = (
                f"{rating} stars across {reviews} reviews — the reputation is already "
                "built, only the website is missing."
            )
        elif not lead.get("is_claimed", True):
            why = (
                "No website and an unclaimed Google listing — no control over their "
                "own presence."
            )
        elif reviews >= 150:
            why = (
                f"{reviews} reviews and still no website — losing customers every day."
            )
        elif reviews >= 30:
            why = f"A working business with {reviews} reviews and zero website."
        elif not lead.get("hours"):
            why = (
                "No website and no opening hours on Google — customers cannot even "
                "tell when they are open."
            )
        else:
            why = "No website — online they exist only as a map pin."
        return min(100, score), why

    # ---- SEO track: site kitni buri hai, wohi asli need hai -----------------
    site = audit.get("site") or {}
    site_score = audit.get("score", 0)

    if not site.get("reachable", True):
        return 100, (
            "The website does not load at all — they are losing customers right now."
        )

    score = round((100 - site_score) * 0.75)          # 0-75

    blockers = codes & BLOCKER_CODES
    serious = codes & SERIOUS_CODES
    if blockers:
        score += 20
    elif serious:
        score += 10

    # Blocker ho to score kisi soorat farsh se neeche nahi ja sakta - HTTPS na
    # hona ya mobile pe toot jana emergency hai, chahe baqi site theek ho.
    for code in blockers:
        score = max(score, NEED_FLOOR.get(code, 0))

    # Achhi reputation lekin kharab site = sabse behtareen SEO lead.
    wasted = reviews >= max(20, median) and site_score < 70
    if wasted:
        score += 8

    if blockers:
        label = {
            "NO_HTTPS": "No HTTPS — Chrome shows visitors a “Not secure” warning.",
            "NO_VIEWPORT": "The site is broken on mobile — unreadable without pinch-zooming.",
            "SITE_DOWN": "The site does not load.",
        }
        why = label.get(sorted(blockers)[0], "Something major is broken on the site.")
    elif wasted and rank:
        why = (
            f"{reviews} reviews behind a site scoring {site_score}/100 — the hard work "
            "is being wasted."
        )
    elif serious:
        label = {
            "SLOW_LOAD": "The site is slow enough that people leave before it opens.",
            "H_SCROLL": "On mobile the content runs off the side of the screen.",
            "NO_PHONE_LINK": "The phone number is not tappable — this is where most leads die.",
            "NAP_MISMATCH": "Site and Google listing details disagree — local ranking suffers.",
            "THIN_CONTENT": "There is nothing to read on the homepage — Google has no reason to rank it.",
        }
        why = label.get(sorted(serious)[0], "The site is not converting visitors.")
    elif site_score < 60:
        why = f"Site scores {site_score}/100 — plenty here worth fixing."
    else:
        why = f"Site scores {site_score}/100 — fine as it is; upsell only."
    return max(0, min(100, score)), why


def priority_for(need: int, track: str) -> str:
    now_at, week_at = PRIORITY_CUTOFFS.get(track, (75, 55))
    if need >= now_at:
        return PRIORITY_NOW
    if need >= week_at:
        return PRIORITY_WEEK
    return PRIORITY_LATER


# ---------------------------------------------------------------- urgency

def urgency_for(lead: dict, audit: dict) -> int:
    """1-5. Rules tight rakhi hain — warna sab 4-5 pe aa jate hain aur
    'urgent' lafz apna matlab kho deta hai."""
    codes = _codes(audit)
    reviews = lead.get("review_count") or 0
    site = audit.get("site") or {}
    website_track = audit.get("track") == TRACK_WEBSITE

    if "SITE_DOWN" in codes or not site.get("reachable", True) and not website_track:
        return 5
    if "NO_HTTPS" in codes or "NO_VIEWPORT" in codes:
        return 5
    if website_track and reviews >= 150:
        return 5

    if website_track and reviews >= 40:
        return 4
    if audit.get("subtrack") == SUBTRACK_SOCIAL:
        return 4
    if "SLOW_LOAD" in codes or "H_SCROLL" in codes:
        return 4

    if website_track:
        return 3
    if "NAP_MISMATCH" in codes or not lead.get("is_claimed", True):
        return 3
    if len(codes & SERIOUS_CODES) >= 2:
        return 3

    if codes:
        return 2
    return 1


# ------------------------------------------------------------------ public

def score_lead(lead: dict, audit: dict, money: dict, context: dict) -> dict:
    need, need_reason = need_score(lead, audit, context)
    value = money_score(money["contract_value_usd"])
    urgency = urgency_for(lead, audit)

    # Order need se banta hai; paisa usay thora sa adjust karta hai.
    combined = round(need * 0.65 + value * 0.35)

    key = lead.get("feature_id") or lead.get("gmb_url")
    reviews = lead.get("review_count") or 0
    median = context.get("median_reviews") or 0

    return {
        "lead_score": max(0, min(100, combined)),
        "need_score": need,
        "money_score": value,
        "need_reason": need_reason,
        "priority": priority_for(need, audit.get("track", TRACK_WEBSITE)),
        "urgency": urgency,
        "urgency_reason": need_reason,
        "score_parts": {
            "need": round(need * 0.65, 1),
            "deal_size": round(value * 0.35, 1),
        },
        "local_rank": context.get("rank_by_key", {}).get(key),
        "reviews_vs_median": None if not median else round(reviews / median, 2),
    }
