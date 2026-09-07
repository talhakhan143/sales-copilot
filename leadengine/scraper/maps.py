"""Google Maps results scraper (Playwright, no API).

Do pass chalta hai:
  1. Feed scroll  -> saare result cards ke place URLs + card-level data
  2. Detail visit -> har place URL kholo, data-item-id selectors se poora record

Har record turant NDJSON me likha jata hai, is liye crash ke baad resume
dobara scrape nahi karta.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote_plus

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from leadengine.settings import PROFILE_DIR, SCRAPE, USER_AGENT, load_config
from leadengine.util import text as T

SELECTORS = load_config("selectors.json")

PHONE_RE = re.compile(r"(\+?\(?\d[\d\s().\-]{7,}\d)")
SEARCH_URL = "https://www.google.com/maps/search/{query}/?hl=en&gl={gl}"
MIDDOT = "·"


@dataclass
class Lead:
    """One Google Maps listing. Ye humara raw record hai."""

    feature_id: str = ""
    name: str = ""
    category: str = ""
    address: str = ""
    city: str = ""
    lat: float | None = None
    lng: float | None = None
    phone: str = ""
    website: str = ""
    rating: float | None = None
    review_count: int | None = None
    price_level: str | None = None
    hours: list[dict] = field(default_factory=list)
    plus_code: str = ""
    is_claimed: bool = True
    is_sponsored: bool = False
    has_booking: bool = False
    gmb_url: str = ""
    search_niche: str = ""
    search_location: str = ""
    scraped_at: str = ""

    def key(self) -> str:
        return self.feature_id or self.gmb_url


# --------------------------------------------------------------------------
# Browser-side extraction (ek hi evaluate call - tez aur kam flaky)
# --------------------------------------------------------------------------

JS_COLLECT_CARDS = r"""
(sel) => {
  const feed = document.querySelector(sel.feed);
  if (!feed) return [];
  const seen = new Set();
  const out = [];
  for (const a of feed.querySelectorAll(sel.card_link)) {
    const href = a.href;
    if (!href || seen.has(href)) continue;
    seen.add(href);
    const card = a.closest('div[jsaction]') || a.parentElement;
    const text = card ? card.innerText : '';
    const site = card ? card.querySelector("a[data-value='Website'], a[aria-label*='Visit'][href^='http']") : null;
    out.push({
      url: href,
      name: a.getAttribute('aria-label') || '',
      text: text,
      website: site ? site.href : '',
      sponsored: /(^|\n)\s*(Sponsored|Ad)\s*(\n|$)/i.test(text)
    });
  }
  return out;
}
"""

JS_FEED_STATE = r"""
(sel) => {
  const feed = document.querySelector(sel.feed);
  const body = document.body ? document.body.innerText : '';
  const ended = sel.feed_end_text.some(t => body.includes(t));
  return { height: feed ? feed.scrollHeight : 0, ended: ended };
}
"""

JS_SCROLL_FEED = r"""
(sel) => {
  const feed = document.querySelector(sel.feed);
  if (feed) feed.scrollTop = feed.scrollHeight;
}
"""

JS_DETAIL = r"""
(sel) => {
  const first = (list) => {
    for (const s of list) { const el = document.querySelector(s); if (el) return el; }
    return null;
  };
  const txt = (el) => (el && el.innerText ? el.innerText.trim() : '');
  const root = document.querySelector(sel.detail_root);
  const bodyText = root ? root.innerText : '';

  const addrBtn = document.querySelector(sel.detail_address);
  const phoneBtn = document.querySelector(sel.detail_phone);
  const siteLink = document.querySelector(sel.detail_website);
  const plusBtn = document.querySelector(sel.detail_pluscode);
  const ratingImg = first(sel.detail_rating_aria);
  const reviewsBtn = first(sel.detail_reviews_button);

  const hours = [];
  const table = first(sel.detail_hours_table);
  if (table) {
    for (const row of table.querySelectorAll('tr')) {
      const cells = row.querySelectorAll('td, th');
      if (cells.length >= 2) {
        hours.push({ day: cells[0].innerText.trim(), hours: cells[1].innerText.trim() });
      }
    }
  }

  const actions = [];
  for (const el of document.querySelectorAll(sel.detail_root + ' [data-item-id]')) {
    const id = el.getAttribute('data-item-id');
    if (id) actions.push(id);
  }

  return {
    name: txt(first(sel.detail_name)),
    category: txt(first(sel.detail_category)),
    ratingBlock: txt(first(sel.detail_rating_block)),
    ratingAria: ratingImg ? ratingImg.getAttribute('aria-label') : '',
    reviewsAria: reviewsBtn ? reviewsBtn.getAttribute('aria-label') : '',
    address: addrBtn ? (addrBtn.getAttribute('aria-label') || addrBtn.innerText) : '',
    phoneItemId: phoneBtn ? phoneBtn.getAttribute('data-item-id') : '',
    phoneText: phoneBtn ? phoneBtn.innerText : '',
    website: siteLink ? siteLink.href : '',
    plusCode: plusBtn ? (plusBtn.getAttribute('aria-label') || plusBtn.innerText) : '',
    priceText: txt(first(sel.detail_price)),
    hours: hours,
    actions: actions,
    unclaimed: /Claim this business|Own this business\?/i.test(bodyText),
    hasBooking: /Book online|Order online|Make an appointment|Reserve a table/i.test(bodyText)
  };
}
"""


# --------------------------------------------------------------------------
# Card text parsing
# --------------------------------------------------------------------------

def _parse_card_text(raw: str) -> dict:
    """Card ke innerText se rating / reviews / category / address / phone nikalo."""
    lines = [T.clean(line) for line in (raw or "").splitlines()]
    lines = [line for line in lines if line]
    out: dict = {}

    for line in lines:
        if "rating" not in out:
            match = re.match(r"^(\d[.,]\d)\s*\(?([\d., \s]+)?\)?$", line)
            if match:
                out["rating"] = T.to_float(match.group(1))
                out["review_count"] = T.to_int(match.group(2) or "")
                continue
        if "category" not in out and MIDDOT in line:
            parts = [T.clean(p) for p in line.split(MIDDOT)]
            if parts:
                out["category"] = parts[0]
                if len(parts) > 1:
                    out["address"] = parts[-1]
        if "phone" not in out:
            match = PHONE_RE.search(line)
            if match and sum(c.isdigit() for c in match.group(1)) >= 9:
                out["phone"] = T.clean(match.group(1))

    return out


def _merge_detail(lead: Lead, d: dict) -> None:
    """Detail-panel payload ko Lead pe apply karo (blank value kabhi overwrite na kare)."""
    if d.get("name"):
        lead.name = T.clean(d["name"])
    if d.get("category"):
        lead.category = T.clean(d["category"])

    rating = T.parse_rating(d.get("ratingAria")) or T.parse_rating(d.get("ratingBlock"))
    if rating is not None:
        lead.rating = rating

    reviews = T.parse_review_count(d.get("reviewsAria")) or T.parse_review_count(
        d.get("ratingBlock")
    )
    if reviews is not None:
        lead.review_count = reviews

    address = T.clean(d.get("address", ""))
    if address.lower().startswith("address:"):
        address = T.clean(address[len("address:"):])
    if address:
        lead.address = address

    item_id = d.get("phoneItemId") or ""
    if item_id.startswith("phone:tel:"):
        lead.phone = item_id[len("phone:tel:"):]
    elif d.get("phoneText"):
        match = PHONE_RE.search(d["phoneText"])
        if match:
            lead.phone = T.clean(match.group(1))

    if d.get("website"):
        lead.website = d["website"]

    plus_code = T.clean(d.get("plusCode", ""))
    if plus_code.lower().startswith("plus code:"):
        plus_code = T.clean(plus_code[len("plus code:"):])
    if plus_code:
        lead.plus_code = plus_code

    price = T.strip_price_level(d.get("priceText"))
    if price:
        lead.price_level = price

    if d.get("hours"):
        lead.hours = d["hours"]

    lead.is_claimed = not d.get("unclaimed", False)
    lead.has_booking = bool(d.get("hasBooking"))


# --------------------------------------------------------------------------
# Checkpoint file
# --------------------------------------------------------------------------

class Checkpoint:
    """NDJSON append-log. Resume ke liye already-scraped keys yaad rakhta hai."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seen: set[str] = set()
        for row in self.rows():
            key = row.get("feature_id") or row.get("gmb_url")
            if key:
                self.seen.add(key)

    def append(self, lead: Lead) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(lead), ensure_ascii=False) + "\n")
        self.seen.add(lead.key())

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows


# --------------------------------------------------------------------------
# Scraper
# --------------------------------------------------------------------------

Progress = Callable[[str, dict], None]


def _noop(stage: str, info: dict) -> None:  # pragma: no cover
    pass


class MapsScraper:
    def __init__(self, progress: Progress = _noop, settings=SCRAPE):
        self.progress = progress
        self.s = settings

    # -- browser lifecycle -------------------------------------------------

    def _pause(self) -> None:
        time.sleep(random.uniform(self.s.min_delay, self.s.max_delay))

    def _dismiss_consent(self, page: Page) -> None:
        for sel in SELECTORS["consent_buttons"]:
            try:
                btn = page.locator(sel).first
                if btn.count() and btn.is_visible(timeout=1500):
                    btn.click(timeout=3000)
                    page.wait_for_timeout(1500)
                    return
            except Exception:
                continue

    # -- pass 1: feed ------------------------------------------------------

    def _collect_cards(self, page: Page, limit: int) -> list[dict]:
        try:
            page.wait_for_selector(SELECTORS["feed"], timeout=self.s.nav_timeout_ms)
        except PWTimeout:
            self.progress(
                "warn", {"message": "results feed nahi mila - shayad single place result hai"}
            )
            return []

        deadline = time.time() + self.s.max_scroll_minutes * 60
        stable = 0
        last_height = 0
        cards: list[dict] = []

        while True:
            cards = page.evaluate(JS_COLLECT_CARDS, SELECTORS)
            self.progress("scroll", {"found": len(cards)})

            if len(cards) >= limit:
                break

            state = page.evaluate(JS_FEED_STATE, SELECTORS)
            if state["ended"]:
                self.progress("scroll_end", {"reason": "end-of-list sentinel"})
                break
            if time.time() > deadline:
                self.progress("scroll_end", {"reason": "time cap"})
                break

            stable = stable + 1 if state["height"] == last_height else 0
            last_height = state["height"]
            if stable >= self.s.stable_rounds_to_stop:
                self.progress("scroll_end", {"reason": "feed height stable"})
                break

            page.evaluate(JS_SCROLL_FEED, SELECTORS)
            self._pause()

        return cards[:limit]

    # -- pass 2: detail ----------------------------------------------------

    def _fetch_detail(self, page: Page, lead: Lead) -> Lead:
        page.goto(lead.gmb_url, wait_until="domcontentloaded", timeout=self.s.nav_timeout_ms)
        try:
            page.wait_for_selector(
                SELECTORS["detail_name"][-1], timeout=self.s.detail_timeout_ms
            )
        except PWTimeout:
            self.progress("warn", {"message": "detail panel timeout: " + lead.name})
            return lead

        page.wait_for_timeout(400)
        _merge_detail(lead, page.evaluate(JS_DETAIL, SELECTORS))

        # URL settles to the canonical place URL with coordinates in it.
        final_url = page.url
        lat, lng = T.parse_coords(final_url)
        if lat is not None:
            lead.lat, lead.lng = lat, lng
        if not lead.feature_id:
            lead.feature_id = T.parse_feature_id(final_url) or ""
        return lead

    # -- public ------------------------------------------------------------

    def scrape(
        self,
        niche: str,
        location: str,
        limit: int | None = None,
        deep: bool = True,
        checkpoint: Checkpoint | None = None,
        country: str = "us",
    ) -> Iterator[Lead]:
        limit = limit or self.s.max_listings
        query = quote_plus(niche + " in " + location)
        url = SEARCH_URL.format(query=query, gl=country)

        def now() -> str:
            return datetime.now(timezone.utc).isoformat(timespec="seconds")

        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=self.s.headless,
                viewport={
                    "width": self.s.viewport_width,
                    "height": self.s.viewport_height,
                },
                locale=self.s.locale,
                user_agent=USER_AGENT,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(self.s.nav_timeout_ms)

            try:
                self.progress("open", {"url": url})
                page.goto(url, wait_until="domcontentloaded", timeout=self.s.nav_timeout_ms)
                self._dismiss_consent(page)

                cards = self._collect_cards(page, limit)
                self.progress("collected", {"count": len(cards)})

                for index, card in enumerate(cards, start=1):
                    parsed = _parse_card_text(card.get("text", ""))
                    lead = Lead(
                        feature_id=T.parse_feature_id(card["url"]) or "",
                        name=T.clean(card.get("name")) or T.place_slug(card["url"]),
                        category=parsed.get("category", ""),
                        address=parsed.get("address", ""),
                        city=location,
                        phone=parsed.get("phone", ""),
                        website=card.get("website", ""),
                        rating=parsed.get("rating"),
                        review_count=parsed.get("review_count"),
                        is_sponsored=bool(card.get("sponsored")),
                        gmb_url=card["url"],
                        search_niche=niche,
                        search_location=location,
                        scraped_at=now(),
                    )
                    lead.lat, lead.lng = T.parse_coords(card["url"])

                    if checkpoint and lead.key() in checkpoint.seen:
                        self.progress("skip", {"index": index, "name": lead.name})
                        continue

                    if deep:
                        try:
                            lead = self._fetch_detail(page, lead)
                        except Exception as exc:  # ek lead fail ho to poori run na ruke
                            self.progress("warn", {"message": lead.name + ": " + str(exc)})
                        self._pause()

                    lead.scraped_at = now()
                    if checkpoint:
                        checkpoint.append(lead)
                    self.progress(
                        "lead", {"index": index, "total": len(cards), "lead": lead}
                    )
                    yield lead
            finally:
                ctx.close()
