"""Deep website audit — SEO track.

Har site pe 8 categories ke ~35 checks chalte hain:
  infra · speed · mobile · seo · local · conversion · freshness · tracking

Do pass: desktop (1440x900) + mobile (Pixel 7 emulation).
Homepage ke ilawa 5 internal pages bhi dekhe jate hain.
Har site ka desktop + mobile screenshot save hota hai (pitch me lagane ke liye).
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, Error as PWError, TimeoutError as PWTimeout

from leadengine.audit.findings import Finding, category_scores, make, score_from, sort_findings
from leadengine.settings import SCREENSHOT_DIR, USER_AGENT

NAV_TIMEOUT = 25_000
PAGE_TIMEOUT = 20_000
MAX_INNER_PAGES = 5
PREFERRED_PATHS = ("contact", "about", "service", "pricing", "book", "quote")

DESKTOP_VIEWPORT = {"width": 1440, "height": 900}
MOBILE_VIEWPORT = {"width": 412, "height": 915}
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)

# ---------------------------------------------------------------------------
# Browser-side fact collection
# ---------------------------------------------------------------------------

JS_FACTS = r"""
() => {
  const text = document.body ? document.body.innerText : '';
  const html = document.documentElement ? document.documentElement.outerHTML : '';
  const metaOf = (name) => {
    const el = document.querySelector('meta[name="' + name + '"], meta[property="' + name + '"]');
    return el ? (el.getAttribute('content') || '') : '';
  };

  const jsonld = [];
  for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { jsonld.push(JSON.parse(node.textContent)); } catch (e) { /* ignore bad blocks */ }
  }

  const images = Array.from(document.images || []);
  const links = Array.from(document.querySelectorAll('a[href]'));
  const host = location.hostname.replace(/^www\./, '');
  const internal = [];
  for (const a of links) {
    try {
      const u = new URL(a.href, location.href);
      if (u.protocol.startsWith('http') && u.hostname.replace(/^www\./, '') === host) {
        internal.push(u.origin + u.pathname);
      }
    } catch (e) { /* skip */ }
  }

  const nav = performance.getEntriesByType('navigation')[0] || {};
  const resources = performance.getEntriesByType('resource') || [];
  let bytes = nav.transferSize || 0;
  for (const r of resources) bytes += (r.transferSize || 0);

  const yearMatches = (text.match(/(?:©|&copy;|Copyright)\s*(\d{4})/gi) || [])
    .map(s => parseInt((s.match(/\d{4}/) || [0])[0], 10)).filter(Boolean);

  return {
    title: (document.title || '').trim(),
    metaDescription: metaOf('description'),
    ogTitle: metaOf('og:title'),
    viewport: metaOf('viewport'),
    generator: metaOf('generator'),
    canonical: (document.querySelector('link[rel="canonical"]') || {}).href || '',
    favicon: !!document.querySelector('link[rel~="icon"]'),
    h1: Array.from(document.querySelectorAll('h1')).map(h => h.innerText.trim()).filter(Boolean),
    h2Count: document.querySelectorAll('h2').length,
    wordCount: text.split(/\s+/).filter(Boolean).length,
    imageCount: images.length,
    imagesWithAlt: images.filter(i => (i.getAttribute('alt') || '').trim()).length,
    internalLinks: Array.from(new Set(internal)),
    linkCount: links.length,
    jsonldTypes: jsonld.flat().map(o => (o && (o['@type'] || '')) ).flat().filter(Boolean),
    hasForm: !!document.querySelector('form input[type="email"], form input[type="text"], form textarea'),
    telLinks: links.filter(a => a.href.startsWith('tel:')).map(a => a.href.slice(4)),
    mailLinks: links.filter(a => a.href.startsWith('mailto:')).map(a => a.href.slice(7)),
    whatsapp: links.some(a => /wa\.me|api\.whatsapp\.com|whatsapp:/i.test(a.href)),
    mapEmbed: !!document.querySelector('iframe[src*="google.com/maps"], iframe[src*="maps.google"]'),
    hasBlog: links.some(a => /\/(blog|news|articles|insights)(\/|$)/i.test(a.href)),
    ctaText: links.concat(Array.from(document.querySelectorAll('button')))
      .map(e => (e.innerText || '').trim().toLowerCase())
      .some(t => /call|contact|book|quote|get started|schedule|appointment|order/.test(t)),
    copyrightYears: yearMatches,
    bodyText: text.slice(0, 6000),
    analytics: {
      ga4: /gtag\(|googletagmanager\.com\/gtag|G-[A-Z0-9]{8,}/.test(html),
      gtm: /googletagmanager\.com\/gtm|GTM-[A-Z0-9]{5,}/.test(html),
      metaPixel: /connect\.facebook\.net|fbq\(/.test(html)
    },
    platform: {
      wordpress: /wp-content|wp-includes|wp-json/.test(html),
      wix: /static\.wixstatic|wix\.com/.test(html),
      squarespace: /squarespace/i.test(html),
      shopify: /cdn\.shopify|shopify\.com/.test(html),
      webflow: /webflow/i.test(html),
      godaddy: /godaddysites|websitebuilder/i.test(html)
    },
    ttfb: Math.round(nav.responseStart || 0),
    domContentLoaded: Math.round(nav.domContentLoadedEventEnd || 0),
    loadEvent: Math.round(nav.loadEventEnd || nav.domComplete || 0),
    requestCount: resources.length + 1,
    transferBytes: Math.round(bytes)
  };
}
"""

JS_MOBILE = r"""
() => {
  const doc = document.documentElement;
  const overflow = Math.max(0, doc.scrollWidth - window.innerWidth);
  let tinyFonts = 0;
  let smallTargets = 0;
  const nodes = Array.from(document.querySelectorAll('p, span, li, a, button, td'));
  for (const el of nodes.slice(0, 600)) {
    const cs = window.getComputedStyle(el);
    const size = parseFloat(cs.fontSize || '16');
    if (size && size < 12 && (el.innerText || '').trim().length > 12) tinyFonts++;
    if (el.tagName === 'A' || el.tagName === 'BUTTON') {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0 && (r.width < 40 || r.height < 32)) smallTargets++;
    }
  }
  return { overflowPx: Math.round(overflow), tinyFonts, smallTargets };
}
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def normalise_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _phone_match(gmb_phone: str, page_text: str, tel_links: list[str]) -> bool:
    target = _digits(gmb_phone)[-9:]
    if len(target) < 7:
        return True  # phone hi nahi hai to mismatch claim nahi kar sakte
    haystack = _digits(page_text) + " " + " ".join(_digits(t) for t in tel_links)
    return target in haystack


def _address_overlap(gmb_address: str, page_text: str) -> float:
    """Street number + street name page pe mojood hai ya nahi."""
    if not gmb_address:
        return 1.0
    tokens = [
        t.lower()
        for t in re.findall(r"[A-Za-z0-9]+", gmb_address)
        if len(t) > 2 and not t.isdigit() or t.isdigit()
    ]
    tokens = tokens[:6]
    if not tokens:
        return 1.0
    lowered = page_text.lower()
    hits = sum(1 for t in tokens if t in lowered)
    return hits / len(tokens)


def _pick_inner_links(links: list[str], base: str) -> list[str]:
    """Contact/about/services ko priority do, phir baaki."""
    base_path = urlparse(base).path.rstrip("/") or "/"
    seen: list[str] = []
    preferred: list[str] = []
    others: list[str] = []
    for link in links:
        path = urlparse(link).path.rstrip("/") or "/"
        if path == base_path or link in seen:
            continue
        seen.append(link)
        if any(p in path.lower() for p in PREFERRED_PATHS):
            preferred.append(link)
        else:
            others.append(link)
    return (preferred + others)[:MAX_INNER_PAGES]


def _human_bytes(n: int) -> str:
    return str(round(n / 1_048_576, 1)) + " MB" if n >= 1_048_576 else str(round(n / 1024)) + " KB"


def _human_ms(ms: int) -> str:
    return str(round(ms / 1000, 1)) + "s"


def detect_platform(platform: dict, generator: str) -> str:
    for key, label in (
        ("wordpress", "WordPress"),
        ("wix", "Wix"),
        ("squarespace", "Squarespace"),
        ("shopify", "Shopify"),
        ("webflow", "Webflow"),
        ("godaddy", "GoDaddy Website Builder"),
    ):
        if platform.get(key):
            return label
    return (generator or "").strip() or "Custom / unknown"


# ---------------------------------------------------------------------------
# main audit
# ---------------------------------------------------------------------------

async def _probe(request, url: str) -> tuple[int, str]:
    try:
        resp = await request.get(url, timeout=10_000, max_redirects=5)
        body = ""
        try:
            body = (await resp.text())[:200_000]
        except PWError:
            pass
        return resp.status, body
    except Exception:
        return 0, ""


async def audit_site(browser: Browser, lead: dict) -> dict:
    """Ek website ka poora audit. Hamesha dict return karta hai, kabhi raise nahi."""
    url = normalise_url(lead.get("website", ""))
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result: dict = {
        "url": url,
        "final_url": "",
        "reachable": False,
        "audited_at": started,
        "findings": [],
        "score": 0,
        "metrics": {},
        "screenshots": {},
        "platform": "",
        "inner_pages": [],
    }
    if not url:
        return result

    findings: list[Finding] = []
    slug = (lead.get("feature_id") or lead.get("name", "site")).replace(":", "_")[:60]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", slug)

    context = await browser.new_context(
        viewport=DESKTOP_VIEWPORT,
        user_agent=USER_AGENT,
        ignore_https_errors=True,
    )
    context.set_default_timeout(PAGE_TIMEOUT)
    page = await context.new_page()

    try:
        # ---- load homepage ------------------------------------------------
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            await page.wait_for_timeout(1200)
        except (PWTimeout, PWError) as exc:
            findings.append(make("SITE_DOWN", evidence=str(exc)[:120]))
            result["findings"] = [f.to_dict() for f in sort_findings(findings)]
            result["score"] = score_from(findings)
            return result

        status = response.status if response else 0
        if status >= 400:
            findings.append(make("SITE_DOWN", evidence="HTTP " + str(status)))
            result["findings"] = [f.to_dict() for f in sort_findings(findings)]
            result["score"] = score_from(findings)
            return result

        result["reachable"] = True
        final_url = page.url
        result["final_url"] = final_url
        parsed = urlparse(final_url)

        facts = await page.evaluate(JS_FACTS)
        result["platform"] = detect_platform(facts["platform"], facts["generator"])

        shot_desktop = SCREENSHOT_DIR / (slug + "-desktop.jpg")
        try:
            await page.screenshot(path=str(shot_desktop), type="jpeg", quality=65)
            result["screenshots"]["desktop"] = str(shot_desktop)
        except PWError:
            pass

        # ---- security / infra --------------------------------------------
        if parsed.scheme != "https":
            findings.append(make("NO_HTTPS", evidence=final_url))

        origin = parsed.scheme + "://" + parsed.netloc
        request = context.request

        robots_status, robots_body = await _probe(request, origin + "/robots.txt")
        if robots_status != 200 or not robots_body.strip():
            findings.append(make("NO_ROBOTS"))

        sitemap_urls = 0
        sitemap_status, sitemap_body = await _probe(request, origin + "/sitemap.xml")
        if sitemap_status != 200 or "<" not in sitemap_body:
            match = re.search(r"(?im)^sitemap:\s*(\S+)", robots_body or "")
            if match:
                sitemap_status, sitemap_body = await _probe(request, match.group(1))
        if sitemap_status == 200 and "<" in sitemap_body:
            sitemap_urls = len(re.findall(r"<loc>", sitemap_body))
        else:
            findings.append(make("NO_SITEMAP"))

        if not facts["canonical"]:
            findings.append(make("NO_CANONICAL"))

        # www vs non-www should end up at the same place.
        # Sirf apex domains pe check karo - subdomain (shop.x.com) pe
        # www.shop.x.com ka na hona bilkul normal hai.
        host = parsed.netloc.split(":")[0]
        labels = host.split(".")
        is_apex_or_www = len(labels) == 2 or (len(labels) == 3 and labels[0] == "www")
        if is_apex_or_www:
            alt_host = host[4:] if host.startswith("www.") else "www." + host
            alt_status, _ = await _probe(request, parsed.scheme + "://" + alt_host + "/")
            if alt_status == 0:
                findings.append(make("BAD_REDIRECT", evidence=alt_host + " is not reachable"))

        # ---- speed --------------------------------------------------------
        load_ms = facts["loadEvent"] or facts["domContentLoaded"]
        if load_ms > 5000:
            findings.append(make("SLOW_LOAD", evidence=_human_ms(load_ms)))
        if facts["ttfb"] > 1200:
            findings.append(make("SLOW_TTFB", evidence=_human_ms(facts["ttfb"])))
        if facts["transferBytes"] > 3_500_000:
            findings.append(make("HEAVY_PAGE", evidence=_human_bytes(facts["transferBytes"])))
        if facts["requestCount"] > 110:
            findings.append(
                make("TOO_MANY_REQUESTS", evidence=str(facts["requestCount"]) + " requests")
            )

        # ---- on-page SEO ---------------------------------------------------
        title = facts["title"]
        if not title:
            findings.append(make("NO_TITLE"))
        else:
            city_hint = (lead.get("city") or "").split(",")[0].strip().lower()
            niche_hint = (lead.get("search_niche") or lead.get("category") or "").lower()
            lowered = title.lower()
            weak = len(title) < 25 or (
                city_hint and city_hint not in lowered and niche_hint.split(" ")[0] not in lowered
            )
            if weak:
                findings.append(make("WEAK_TITLE", evidence=title[:70]))

        if not facts["metaDescription"]:
            findings.append(make("NO_META_DESC"))

        h1s = facts["h1"]
        if not h1s:
            findings.append(make("NO_H1"))
        elif len(h1s) > 2:
            findings.append(make("MULTI_H1", evidence=str(len(h1s)) + " H1 tags"))

        if facts["wordCount"] < 300:
            findings.append(
                make("THIN_CONTENT", evidence=str(facts["wordCount"]) + " words")
            )

        if facts["imageCount"] >= 4:
            coverage = facts["imagesWithAlt"] / facts["imageCount"]
            if coverage < 0.5:
                findings.append(
                    make(
                        "LOW_ALT_COVERAGE",
                        evidence=str(facts["imageCount"] - facts["imagesWithAlt"])
                        + " images bina alt text ke",
                    )
                )

        # ---- structured data / local ---------------------------------------
        types = [str(t).lower() for t in facts["jsonldTypes"]]
        has_local = any(
            t in types for t in ("localbusiness", "organization", "professionalservice")
        ) or any("business" in t for t in types)
        if not has_local:
            findings.append(make("NO_SCHEMA", evidence=str(len(types)) + " JSON-LD blocks"))

        page_text = facts["bodyText"]
        nap_problems = []
        if not _phone_match(lead.get("phone", ""), page_text, facts["telLinks"]):
            nap_problems.append("the phone number is not on the site")
        overlap = _address_overlap(lead.get("address", ""), page_text)
        if overlap < 0.5:
            nap_problems.append("the address does not match")
        if nap_problems:
            findings.append(make("NAP_MISMATCH", evidence=" and ".join(nap_problems) + "."))

        if not facts["mapEmbed"]:
            findings.append(make("NO_MAP_EMBED"))

        # ---- conversion -----------------------------------------------------
        if not facts["telLinks"]:
            findings.append(make("NO_PHONE_LINK"))
        if not facts["hasForm"]:
            findings.append(make("NO_CONTACT_FORM"))
        if not facts["whatsapp"]:
            findings.append(make("NO_WHATSAPP"))
        if not facts["ctaText"]:
            findings.append(make("NO_CTA"))

        # ---- freshness ------------------------------------------------------
        current_year = datetime.now(timezone.utc).year
        years = facts["copyrightYears"]
        if years and max(years) < current_year - 1:
            findings.append(make("STALE_COPYRIGHT", evidence="© " + str(max(years))))
        if not facts["hasBlog"]:
            findings.append(make("NO_BLOG"))
        if result["platform"] in ("Wix", "GoDaddy Website Builder") and facts["wordCount"] < 500:
            findings.append(
                make(
                    "OLD_DESIGN",
                    evidence=result["platform"]
                    + " template with thin content on top of it — a custom site "
                    "converts a good deal better.",
                )
            )

        # ---- tracking --------------------------------------------------------
        analytics = facts["analytics"]
        if not (analytics["ga4"] or analytics["gtm"]):
            findings.append(make("NO_ANALYTICS"))
        if not analytics["metaPixel"]:
            findings.append(make("NO_PIXEL"))

        # ---- mobile pass ------------------------------------------------------
        if not facts["viewport"]:
            findings.append(make("NO_VIEWPORT"))

        mobile_ctx = await browser.new_context(
            viewport=MOBILE_VIEWPORT,
            user_agent=MOBILE_UA,
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            ignore_https_errors=True,
        )
        try:
            mpage = await mobile_ctx.new_page()
            await mpage.goto(final_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            await mpage.wait_for_timeout(900)
            mobile = await mpage.evaluate(JS_MOBILE)
            shot_mobile = SCREENSHOT_DIR / (slug + "-mobile.jpg")
            try:
                await mpage.screenshot(path=str(shot_mobile), type="jpeg", quality=65)
                result["screenshots"]["mobile"] = str(shot_mobile)
            except PWError:
                pass
            if mobile["overflowPx"] > 20:
                findings.append(
                    make("H_SCROLL", evidence=str(mobile["overflowPx"]) + "px extra width")
                )
            if mobile["tinyFonts"] > 8:
                findings.append(
                    make("TINY_FONTS", evidence=str(mobile["tinyFonts"]) + " text blocks under 12px")
                )
            if mobile["smallTargets"] > 10:
                findings.append(
                    make(
                        "SMALL_TAP_TARGETS",
                        evidence=str(mobile["smallTargets"]) + " buttons/links too small to tap",
                    )
                )
            result["metrics"]["mobile"] = mobile
        except (PWTimeout, PWError):
            pass
        finally:
            await mobile_ctx.close()

        # ---- inner pages -------------------------------------------------------
        inner = _pick_inner_links(facts["internalLinks"], final_url)
        for link in inner:
            try:
                await page.goto(link, wait_until="domcontentloaded", timeout=15_000)
                await page.wait_for_timeout(400)
                sub = await page.evaluate(JS_FACTS)
                result["inner_pages"].append(
                    {
                        "url": link,
                        "title": sub["title"],
                        "has_meta": bool(sub["metaDescription"]),
                        "h1": len(sub["h1"]),
                        "words": sub["wordCount"],
                    }
                )
            except (PWTimeout, PWError):
                result["inner_pages"].append({"url": link, "error": "load fail"})

        # ---- metrics ------------------------------------------------------------
        result["metrics"].update(
            {
                "https": parsed.scheme == "https",
                "status": status,
                "ttfb_ms": facts["ttfb"],
                "load_ms": load_ms,
                "transfer_bytes": facts["transferBytes"],
                "requests": facts["requestCount"],
                "title": title,
                "title_length": len(title),
                "meta_description": facts["metaDescription"],
                "h1_count": len(h1s),
                "h2_count": facts["h2Count"],
                "word_count": facts["wordCount"],
                "images": facts["imageCount"],
                "images_with_alt": facts["imagesWithAlt"],
                "internal_links": len(facts["internalLinks"]),
                "jsonld_types": facts["jsonldTypes"],
                "sitemap_urls": sitemap_urls,
                "has_robots": robots_status == 200,
                "has_form": facts["hasForm"],
                "tel_links": facts["telLinks"][:3],
                "emails_found": facts["mailLinks"][:5],
                "whatsapp": facts["whatsapp"],
                "map_embed": facts["mapEmbed"],
                "has_blog": facts["hasBlog"],
                "analytics": analytics,
                "viewport_meta": facts["viewport"],
                "copyright_years": years,
                "nap_address_overlap": round(overlap, 2),
            }
        )

    finally:
        await context.close()

    findings = sort_findings(findings)
    result["findings"] = [f.to_dict() for f in findings]
    result["score"] = score_from(findings)
    result["category_scores"] = category_scores(findings)
    return result


async def audit_many(browser: Browser, leads: list[dict], concurrency: int = 3, on_done=None):
    """Kai sites ek saath audit karo (default 3 parallel)."""
    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict] = [None] * len(leads)  # type: ignore[list-item]

    async def one(index: int, lead: dict) -> None:
        async with semaphore:
            try:
                audit = await audit_site(browser, lead)
            except Exception as exc:  # noqa: BLE001 - ek site poori run na rok de
                audit = {
                    "url": lead.get("website", ""),
                    "reachable": False,
                    "error": str(exc)[:200],
                    "findings": [],
                    "score": 0,
                }
            results[index] = audit
            if on_done:
                on_done(index, lead, audit)

    await asyncio.gather(*(one(i, l) for i, l in enumerate(leads)))
    return results
