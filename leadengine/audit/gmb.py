"""GMB-side audit — website ke baghair wale leads ka track.

Ye pure Python hai, koi browser nahi chahiye. Scraped lead dict lo aur
findings + track wapas do.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from leadengine.audit.findings import Finding, gmb_score_from, make, sort_findings

# "website" field jo asal me social page hai
SOCIAL_HOSTS = {
    "facebook.com": "Facebook",
    "fb.com": "Facebook",
    "instagram.com": "Instagram",
    "linktr.ee": "Linktree",
    "twitter.com": "X (Twitter)",
    "x.com": "X (Twitter)",
    "tiktok.com": "TikTok",
    "youtube.com": "YouTube",
    "linkedin.com": "LinkedIn",
    "wa.me": "WhatsApp",
    "yelp.com": "Yelp",
    "business.site": "Google Business Site",
    "sites.google.com": "Google Sites",
}

TRACK_WEBSITE = "WEBSITE"   # website bechni hai
TRACK_SEO = "SEO"           # SEO bechni hai
SUBTRACK_SOCIAL = "SOCIAL_ONLY"


def social_host(url: str) -> str | None:
    """Agar URL kisi social platform ka hai to us platform ka naam do."""
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    for needle, label in SOCIAL_HOSTS.items():
        if host == needle or host.endswith("." + needle):
            return label
    return None


def classify(lead: dict) -> tuple[str, str | None]:
    """(track, subtrack) return karo."""
    website = (lead.get("website") or "").strip()
    if not website:
        return TRACK_WEBSITE, None
    platform = social_host(website)
    if platform:
        return TRACK_WEBSITE, SUBTRACK_SOCIAL
    return TRACK_SEO, None


def _review_phrase(lead: dict) -> str:
    reviews = lead.get("review_count")
    rating = lead.get("rating")
    bits = []
    if reviews:
        bits.append(str(reviews) + " reviews")
    if rating:
        bits.append(str(rating) + " stars")
    return ", ".join(bits) if bits else "Their Google listing is active"


def audit_gmb(lead: dict) -> dict:
    """GMB completeness + track detection. Har lead pe chalta hai (site ho ya na ho)."""
    findings: list[Finding] = []
    track, subtrack = classify(lead)
    website = (lead.get("website") or "").strip()

    if track == TRACK_WEBSITE:
        if subtrack == SUBTRACK_SOCIAL:
            findings.append(
                make("SOCIAL_ONLY", evidence=social_host(website) or "social media")
            )
        else:
            findings.append(
                make(
                    "NO_WEBSITE",
                    evidence=_review_phrase(lead),
                    niche=lead.get("search_niche") or lead.get("category") or "service",
                )
            )

    if not lead.get("is_claimed", True):
        findings.append(make("GMB_UNCLAIMED"))

    if not lead.get("hours"):
        findings.append(make("GMB_NO_HOURS"))

    reviews = lead.get("review_count") or 0
    if reviews and reviews < 15:
        findings.append(
            make("GMB_LOW_REVIEWS", evidence="Only " + str(reviews) + " reviews")
        )

    rating = lead.get("rating")
    if rating is not None and rating < 3.8:
        findings.append(
            make("GMB_LOW_RATING", evidence="Google rating " + str(rating) + "/5")
        )

    findings = sort_findings(findings)
    return {
        "track": track,
        "subtrack": subtrack,
        "gmb_findings": [f.to_dict() for f in findings],
        "gmb_score": gmb_score_from(findings),
    }
