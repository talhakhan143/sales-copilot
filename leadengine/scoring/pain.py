"""Pain points — built on real numbers, never generic filler.

Two things come out of here:
  pain_points    -> lines you can show or send to the prospect
  talking_points -> for you only, to skim before the call
"""

from __future__ import annotations

from leadengine.audit.gmb import SUBTRACK_SOCIAL, TRACK_SEO, TRACK_WEBSITE

CATEGORY_LABELS = {
    "seo": "Being found on Google",
    "conversion": "Turning visitors into customers",
    "mobile": "Mobile experience",
    "speed": "Speed",
    "local": "Local / Google Business",
    "infra": "Technical setup",
    "freshness": "Freshness",
    "tracking": "Measurement",
}


def _niche(lead: dict) -> str:
    return (lead.get("search_niche") or lead.get("category") or "service").lower()


def _city(lead: dict) -> str:
    city = (lead.get("city") or lead.get("search_location") or "").strip()
    return city.title() if city else "their area"


def _stat_line(lead: dict) -> str:
    reviews = lead.get("review_count") or 0
    rating = lead.get("rating")
    if reviews and rating:
        return str(reviews) + " reviews at " + str(rating) + " stars"
    if reviews:
        return str(reviews) + " reviews"
    if rating:
        return str(rating) + " stars"
    return "Listed on Google"


def _pain(title: str, detail: str, proof: str = "", severity: str = "high") -> dict:
    return {"title": title, "detail": detail, "proof": proof, "severity": severity}


def build_pain_points(lead: dict, audit: dict, money: dict, context: dict) -> list[dict]:
    points: list[dict] = []
    track = audit.get("track")
    site = audit.get("site") or {}
    findings = site.get("findings", [])
    gmb_findings = audit.get("gmb_findings", [])
    reviews = lead.get("review_count") or 0
    median = context.get("median_reviews") or 0
    rank = context.get("rank_by_key", {}).get(lead.get("feature_id") or lead.get("gmb_url"))
    leaders = [l for l in context.get("leaders", []) if l["name"] != lead.get("name")]

    # 1 -- the biggest single gap
    if track == TRACK_WEBSITE and audit.get("subtrack") == SUBTRACK_SOCIAL:
        points.append(
            _pain(
                "A social page instead of a website",
                _stat_line(lead)
                + ", but no page of their own on Google. The website link on their "
                "listing goes to social media, which Google does not rank the way it "
                "ranks a website — and they do not control that page either.",
                proof="Google listing website field points at a social page",
                severity="critical",
            )
        )
    elif track == TRACK_WEBSITE:
        detail = (
            _stat_line(lead)
            + " — customers are coming in and they are happy. But when somebody "
            'searches "'
            + _niche(lead)
            + ' near me" this business only shows up on the map. Anyone who clicks '
            "through to read more finds nothing at all."
        )
        if context.get("with_website"):
            detail += (
                " " + str(context["with_website"])
                + " competitors in this same search do have a website."
            )
        points.append(
            _pain("No website at all", detail, proof=_stat_line(lead), severity="critical")
        )
    elif not site.get("reachable", True):
        points.append(
            _pain(
                "The website does not load",
                "Their site is not loading right now. Every customer arriving from "
                "Google hits a blank page and goes straight back to a competitor.",
                proof=site.get("url", ""),
                severity="critical",
            )
        )

    # 2 -- competitor comparison. Only name a rival who is ACTUALLY ahead,
    #      otherwise you get nonsense like "you're #1 but #11 is beating you".
    rival = None
    for candidate in leaders:
        ahead_in_rank = rank and candidate["rank"] < rank
        ahead_in_reviews = candidate["reviews"] > reviews
        if ahead_in_rank or ahead_in_reviews:
            rival = candidate
            break

    if rival and track == TRACK_WEBSITE:
        edge = (
            "sits at #" + str(rival["rank"])
            if (rank and rival["rank"] < rank)
            else "has " + str(rival["reviews"]) + " reviews"
        )
        points.append(
            _pain(
                "The competitor shows up in both places, this business in one",
                '"' + rival["name"] + '" ' + edge
                + " in this same search — and has a working website on top of it. "
                "This business has " + str(reviews)
                + " reviews and no site. The customer who clicks through to compare "
                "ends up over there.",
                proof=rival["name"] + " · rank #" + str(rival["rank"]),
                severity="high",
            )
        )
    elif rival and rival["site_score"] > audit.get("score", 0) + 10:
        points.append(
            _pain(
                "A competitor's site is scoring better",
                '"' + rival["name"] + '" scores ' + str(rival["site_score"])
                + "/100 against their " + str(audit.get("score", 0)) + "/100. "
                "That gap is what shows up in the Google ranking.",
                proof=rival["name"] + " · " + str(rival["site_score"]) + "/100",
                severity="high",
            )
        )
    elif track == TRACK_WEBSITE and rank == 1 and reviews:
        # The strongest case of all: market leader with nothing to send people to.
        points.append(
            _pain(
                "Market leader with no website",
                "They rank #1 in this search and lead on reviews with " + str(reviews)
                + ". But the customer who clicks through wanting services, pricing or "
                "photos finds nothing, and goes to a lower-ranked competitor who has a "
                "site. They have already won the hard part and are losing leads at the "
                "finish line.",
                proof="rank #1 · " + str(reviews) + " reviews · no website",
                severity="critical",
            )
        )

    # 3 -- rank + review positioning
    if rank and rank > 3 and reviews and median:
        if reviews >= median:
            points.append(
                _pain(
                    "Reviews are strong, the ranking is not",
                    "Their " + str(reviews) + " reviews are "
                    + str(round(reviews / median, 1))
                    + "x the area median of " + str(median)
                    + ", yet they sit at #" + str(rank)
                    + " in search. Customers rate them well — Google is just not "
                    "getting the right signals.",
                    proof="rank #" + str(rank) + " · " + str(reviews)
                    + " vs median " + str(median),
                    severity="high",
                )
            )
        else:
            points.append(
                _pain(
                    "Fewer reviews than the market",
                    "The area averages " + str(median) + " reviews; they have "
                    + str(reviews) + ". Google treats reviews as a ranking signal, "
                    "which is a large part of why they sit at #" + str(rank) + ".",
                    proof=str(reviews) + " vs median " + str(median),
                    severity="medium",
                )
            )

    # 4 -- top audit findings, carrying their own sales_line
    for finding in findings[:4]:
        points.append(
            _pain(
                finding["title"],
                finding["sales_line"],
                proof=finding.get("evidence", ""),
                severity=finding["severity"],
            )
        )

    # 5 -- GMB gaps (these apply to website-track leads too)
    for finding in gmb_findings:
        if finding["code"] in ("NO_WEBSITE", "SOCIAL_ONLY"):
            continue  # already covered above
        points.append(
            _pain(
                finding["title"],
                finding["sales_line"],
                proof=finding.get("evidence", ""),
                severity=finding["severity"],
            )
        )

    # 6 -- weakest category, if anything is left worth saying
    cats = site.get("category_scores") or {}
    if cats:
        worst_cat, worst_score = min(cats.items(), key=lambda kv: kv[1])
        if worst_score < 40:
            points.append(
                _pain(
                    CATEGORY_LABELS.get(worst_cat, worst_cat) + " is the weakest area",
                    "Their site scores " + str(worst_score)
                    + "/100 here — this is where the most ground can be made up.",
                    proof=worst_cat + " " + str(worst_score) + "/100",
                    severity="medium",
                )
            )

    # dedupe by title, keep the top 6
    seen: set[str] = set()
    unique: list[dict] = []
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for point in sorted(points, key=lambda p: order.get(p["severity"], 9)):
        if point["title"] in seen:
            continue
        seen.add(point["title"])
        unique.append(point)
    return unique[:6]


def build_talking_points(lead: dict, audit: dict, money: dict, score: dict, context: dict) -> list[str]:
    """For you only — one glance before the call or the DM."""
    symbol = money["currency_symbol"]
    bits = [
        "Deal: " + money["deal_type"] + " · " + symbol + str(money["deal_value_usd"])
        + " + " + symbol + str(money["retainer_monthly_usd"]) + "/mo"
        + " (contract " + symbol + str(money["contract_value_usd"]) + ")",
        "Tier " + money["tier"] + " (" + money["tier_label"] + ") · " + money["review_band"],
        "Lead score " + str(score["lead_score"]) + "/100 · urgency "
        + str(score["urgency"]) + "/5",
        "Why now: " + score["urgency_reason"],
    ]
    if score.get("local_rank"):
        bits.append("Ranks #" + str(score["local_rank"]) + " in this search")
    if lead.get("phone"):
        bits.append("Phone: " + lead["phone"])
    emails = (audit.get("site") or {}).get("metrics", {}).get("emails_found") or []
    if emails:
        bits.append("Email found on their site: " + ", ".join(emails[:2]))
    if audit.get("track") == TRACK_SEO:
        site = audit["site"]
        bits.append(
            "Site: " + (site.get("platform") or "?") + " · "
            + str(audit.get("score", 0)) + "/100"
        )
    return bits
