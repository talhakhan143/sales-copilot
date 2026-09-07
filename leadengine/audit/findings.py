"""Finding catalog.

Har check ka ek code hota hai. Yahan uska severity, points (score se kitne
minus honge), aur sabse ahem cheez: `sales_line` - wo jumla jo seedha
prospect ko bheja ja sakta hai.

Ek hi jagah rakha hai taake copy tweak karni ho to poore codebase me
dhoondna na pare.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# severity -> UI colour bucket
CRITICAL, HIGH, MEDIUM, LOW = "critical", "high", "medium", "low"


@dataclass
class Finding:
    code: str
    severity: str
    title: str
    evidence: str = ""
    sales_line: str = ""
    points: int = 0
    category: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "evidence": self.evidence,
            "sales_line": self.sales_line,
            "points": self.points,
            "category": self.category,
        }


# code: (category, severity, points, title, sales_line)
# {evidence} placeholders sales_line me bhi use ho sakte hain.
CATALOG: dict[str, tuple] = {
    # -- reachability / security ------------------------------------------
    "SITE_DOWN": ("infra", CRITICAL, 45, "The website does not load",
                  "Their website is not loading at all right now. Every customer who "
                  "reaches them from Google lands on a blank page."),
    "NO_HTTPS": ("infra", CRITICAL, 20, "No HTTPS",
                 "Chrome puts a “Not secure” warning on their site. People leave "
                 "before filling in a form, and Google ranks HTTP sites lower."),
    "BAD_REDIRECT": ("infra", MEDIUM, 5, "www and non-www run as separate sites",
                     "Google reads them as two different websites, so the ranking power "
                     "gets split in half."),
    "NO_ROBOTS": ("infra", LOW, 3, "No robots.txt",
                  "Google gets no crawl instructions at all."),
    "NO_SITEMAP": ("infra", MEDIUM, 6, "No sitemap.xml",
                   "Google never gets a list of their pages, so several of them never "
                   "get indexed."),
    "NO_CANONICAL": ("infra", LOW, 3, "No canonical tag",
                     "Duplicate URLs can split the ranking between them."),

    # -- speed -------------------------------------------------------------
    "SLOW_LOAD": ("speed", HIGH, 15, "The site is very slow",
                  "Their site takes {evidence} to load. Google's own research says 53% of "
                  "mobile users will not wait more than three seconds, so over half the "
                  "traffic leaves before seeing the page."),
    "SLOW_TTFB": ("speed", MEDIUM, 7, "Slow server response",
                  "The server takes {evidence} to answer at all. That is a hosting or "
                  "caching problem."),
    "HEAVY_PAGE": ("speed", MEDIUM, 6, "The page is very heavy",
                   "The homepage downloads {evidence}. On mobile data that is expensive "
                   "and slow."),
    "TOO_MANY_REQUESTS": ("speed", LOW, 4, "Too many requests",
                          "The page loads {evidence} separate files, and each one adds "
                          "delay."),

    # -- mobile ------------------------------------------------------------
    "NO_VIEWPORT": ("mobile", CRITICAL, 20, "Not mobile friendly (viewport meta missing)",
                    "On a phone the site serves the desktop version, so visitors have to "
                    "pinch and zoom to read it. Around 60-70% of local searches happen on "
                    "mobile."),
    "H_SCROLL": ("mobile", HIGH, 10, "Sideways scrolling on mobile",
                 "On a phone the content runs off the edge of the screen, which makes it "
                 "hard to read."),
    "TINY_FONTS": ("mobile", MEDIUM, 5, "Text too small on mobile",
                   "{evidence} — visitors have to zoom in to read it on a phone."),
    "SMALL_TAP_TARGETS": ("mobile", MEDIUM, 5, "Buttons too small to tap",
                          "{evidence} — people hit the wrong thing on mobile."),

    # -- on-page SEO -------------------------------------------------------
    "NO_TITLE": ("seo", CRITICAL, 15, "No page title",
                 "Their name does not appear in Google's search results at all."),
    "WEAK_TITLE": ("seo", MEDIUM, 7, "Weak page title",
                   "The title names neither the service nor the city — “{evidence}”. "
                   "People searching “service near me” never find them."),
    "NO_META_DESC": ("seo", MEDIUM, 7, "No meta description",
                     "The two lines under their link in Google were never written, so "
                     "Google grabs whatever text it likes."),
    "NO_H1": ("seo", MEDIUM, 6, "No H1 heading",
              "The main heading is missing, so Google cannot tell what the page is about."),
    "MULTI_H1": ("seo", LOW, 3, "More than one H1",
                 "{evidence} — Google cannot tell which one is the real topic."),
    "THIN_CONTENT": ("seo", HIGH, 12, "Barely any content",
                     "The homepage has only {evidence}. Google needs something to read "
                     "before it will rank a page; competitors run 800+ words."),
    "LOW_ALT_COVERAGE": ("seo", LOW, 4, "Images have no alt text",
                         "{evidence} — they get nothing from Google Images."),

    # -- structured data / local -------------------------------------------
    "NO_SCHEMA": ("local", HIGH, 12, "No LocalBusiness structured data",
                  "Google has no machine-readable copy of their hours, address, phone or "
                  "services, which is why they never get a rich result in search."),
    "NAP_MISMATCH": ("local", HIGH, 12, "Site and Google listing details disagree",
                     "{evidence} Google may read these as two different businesses, and "
                     "local ranking drops as a direct result."),
    "NO_MAP_EMBED": ("local", LOW, 3, "No map on the site",
                     "Customers have to run a separate search to find the location."),

    # -- conversion --------------------------------------------------------
    "NO_PHONE_LINK": ("conversion", HIGH, 10, "Phone number is not tappable",
                      "A mobile visitor cannot tap to call. They have to memorise the "
                      "number and open the dialler themselves, and more leads die here "
                      "than anywhere else."),
    "NO_CONTACT_FORM": ("conversion", MEDIUM, 8, "No contact form",
                        "A customer who does not want to phone has no way to reach them "
                        "at all."),
    "NO_WHATSAPP": ("conversion", LOW, 3, "No WhatsApp link",
                    "Most people would rather message than call."),
    "NO_CTA": ("conversion", MEDIUM, 6, "No clear call to action",
               "Nowhere on the page does it say what the visitor should do next."),

    # -- freshness ---------------------------------------------------------
    "STALE_COPYRIGHT": ("freshness", MEDIUM, 5, "Old year in the footer",
                        "The footer still reads “{evidence}”, which makes visitors "
                        "assume the business has closed."),
    "NO_BLOG": ("freshness", LOW, 4, "No blog or news section",
                "With no new content, Google treats the site as abandoned."),
    "OLD_DESIGN": ("freshness", MEDIUM, 6, "The design looks dated",
                   "{evidence}"),

    # -- tracking ----------------------------------------------------------
    "NO_ANALYTICS": ("tracking", MEDIUM, 6, "No analytics installed",
                     "They have no idea how many people visit, where they come from or "
                     "where they drop off. The marketing is running blind."),
    "NO_PIXEL": ("tracking", LOW, 3, "No Facebook/Meta pixel",
                 "Anyone who visits and leaves can never be reached with an ad again."),

    # -- no-website track (GMB only) ---------------------------------------
    "NO_WEBSITE": ("gmb", CRITICAL, 50, "No website at all",
                   "{evidence}, and no website. When people search “{niche} near me” "
                   "they only appear on the map; anyone who decides by looking at a "
                   "website goes to a competitor instead."),
    "SOCIAL_ONLY": ("gmb", CRITICAL, 40, "A social page instead of a website",
                    "Their “website” link goes to {evidence}. Google does not rank a "
                    "social page the way it ranks a website, and they do not control that "
                    "page either."),
    "GMB_UNCLAIMED": ("gmb", HIGH, 15, "Google listing is unclaimed",
                      "Anyone can edit their listing — hours, phone, address — and they "
                      "cannot reply to reviews."),
    "GMB_NO_HOURS": ("gmb", MEDIUM, 8, "No opening hours on Google",
                     "Customers cannot tell whether they are open, so they check the next "
                     "option instead."),
    "GMB_FEW_PHOTOS": ("gmb", LOW, 4, "Very few photos on the listing",
                       "Listings with photos get 42% more direction requests."),
    "GMB_LOW_REVIEWS": ("gmb", MEDIUM, 6, "Very few reviews",
                        "{evidence} — competitors have more, which is part of why they "
                        "sit higher."),
    "GMB_LOW_RATING": ("gmb", HIGH, 10, "Low rating",
                       "{evidence} — a low rating feeds straight through to sales."),
}


def make(code: str, evidence: str = "", **fmt) -> Finding:
    """Catalog se ek Finding banao, evidence inject kar ke."""
    category, severity, points, title, sales = CATALOG[code]
    context = {"evidence": evidence, **fmt}
    try:
        sales_line = sales.format(**context)
    except (KeyError, IndexError):
        sales_line = sales
    return Finding(
        code=code,
        severity=severity,
        title=title,
        evidence=evidence,
        sales_line=sales_line,
        points=points,
        category=category,
    )


SEVERITY_ORDER = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}


def sort_findings(items: list[Finding]) -> list[Finding]:
    return sorted(items, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), -f.points))


# Category ka wazan. Jama = 1.0
CATEGORY_WEIGHTS = {
    "seo": 0.20,
    "conversion": 0.18,
    "mobile": 0.15,
    "speed": 0.15,
    "local": 0.14,
    "infra": 0.10,
    "freshness": 0.04,
    "tracking": 0.04,
}

# Har category me ziyada se ziyada kitne points kat sakte hain.
# SITE_DOWN alag handle hota hai (score seedha 0), is liye nikal diya.
CATEGORY_MAX: dict[str, int] = {}
for _code, (_cat, _sev, _pts, *_rest) in CATALOG.items():
    if _code == "SITE_DOWN":
        continue
    CATEGORY_MAX[_cat] = CATEGORY_MAX.get(_cat, 0) + _pts


def category_scores(items: list[Finding]) -> dict[str, int]:
    """Har category ka 0-100 score. UI me bar chart isi se banega."""
    deducted: dict[str, int] = {}
    for f in items:
        deducted[f.category] = deducted.get(f.category, 0) + f.points
    out = {}
    for cat in CATEGORY_WEIGHTS:
        ceiling = CATEGORY_MAX.get(cat, 0)
        lost = deducted.get(cat, 0)
        out[cat] = 100 if not ceiling else max(0, round(100 * (1 - min(1.0, lost / ceiling))))
    return out


def score_from(items: list[Finding]) -> int:
    """Weighted site-quality score 0-100. Kam score = zyada pain = better lead.

    Seedha 100 - sum(points) is liye nahi karte ke har chhoti finding jama ho
    kar har site ko 0 pe le aati thi. Har category apne max ke hisaab se
    normalise hoti hai, phir wazan lagta hai.
    """
    if any(f.code == "SITE_DOWN" for f in items):
        return 0
    cats = category_scores(items)
    total = sum(CATEGORY_WEIGHTS[c] * cats[c] for c in CATEGORY_WEIGHTS)
    return max(0, min(100, round(total)))


def gmb_score_from(items: list[Finding]) -> int:
    """GMB track ka apna simple score (categories nahi hain)."""
    gmb_items = [f for f in items if f.category == "gmb"]
    ceiling = CATEGORY_MAX.get("gmb", 0) or 1
    lost = sum(f.points for f in gmb_items)
    return max(0, round(100 * (1 - min(1.0, lost / ceiling))))
