"""Turn one audited Google Maps lead into the call context the copilot reads.

This is the reason the lead engine and the teleprompter were merged. Before this
module the rep typed two lines about the prospect by hand, from memory, seconds
before dialling. The audit already knows far more than that: how slow the site
is, whether the phone number can be tapped, how the business ranks against the
people it competes with, and what the work is worth.

Three functions, all pure and all synchronous:

``build_lead_context(lead)``
    The block that goes into ``PrepareContextRequest.client_context``. Three
    headings, in the order the contract froze, and nothing invented.
``build_call_goal(lead)``
    The one line goal that goes into ``PrepareContextRequest.call_goal``, built
    from the track and the money the audit worked out.
``lead_language_hint(lead)``
    A best effort two letter language code from the city or the country, or
    ``None``. Read its docstring before trusting it.

Two rules in here are load bearing and must not be softened by a later edit.

1. **Proof strings are copied through byte for byte.** A ``proof`` is the number
   the rep says out loud, so it has to be the number the audit measured. It is
   never rounded, never reworded, never clamped, never whitespace collapsed. One
   consequence is worth stating plainly: a small number of proof strings written
   by the lead engine contain an em dash, and this module passes them through
   unchanged. Everywhere else in this file, in the prose it renders and in its
   own source, an em dash is replaced with a comma, which is exactly what the
   contract's own sample output does to ``need_reason``.
2. **Nothing is invented.** A missing field means the line is dropped, not
   filled with a plausible guess. A zero review count and a zero local rank are
   missing fields too, because a business cannot really sit at rank zero and the
   lead engine itself says nothing when either is falsy, so those lines are
   dropped as well. A business with no website gets the explicit "They have NO
   website at all." line, because that is the strongest thing the rep can open
   with and it must never be quietly lost.

Import rule, same as the rest of this package: nothing from ``app.api`` and
nothing from ``leadengine`` is imported here. This module reads whatever shape
of lead it is handed and stays unit testable on its own.

Run the self test with the repo's own python:

    python -m app.services.lead_context

from the ``backend`` directory. It builds the context for real leads out of
``leadengine/data`` and checks every rule above against them.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

logger = logging.getLogger(__name__)

__all__ = [
    "HEADING_LEAD_WITH",
    "HEADING_WHAT_IS_WRONG",
    "HEADING_WHO_THEY_ARE",
    "MAX_CONTEXT_CHARS",
    "MAX_PAIN_POINTS",
    "LeadFacts",
    "PainPoint",
    "build_call_goal",
    "build_lead_context",
    "lead_language_hint",
    "read_lead_facts",
]

LeadLike: TypeAlias = object
"""Anything that carries lead fields.

Deliberately loose. ``services/leads.py`` owns the ``Lead`` dataclass and the
wire dict, and this module is happy with either one, with a raw scored record
straight out of ``<slug>.scores.json``, or with a plain dict the caller stitched
together. See ``read_lead_facts`` for how a field is looked up.
"""

MAX_CONTEXT_CHARS: int = 4000
"""Hard ceiling on the whole block, from the contract.

Comfortably inside ``models.MAX_CLIENT_CONTEXT_CHARS`` (6000), so the block is
never silently clipped by the request validator after this module capped it.
"""

MAX_PAIN_POINTS: int = 5
"""Most findings the copilot is allowed to see.

Handing it ten makes it pick a weak one. Five strong ones keep it on the number
that actually hurts.
"""

SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2}
"""Severities that are kept, and the order they are shown in, strongest first.

The contract names high and medium. The real audit also writes ``critical``, and
35 of those in the rep's own data are "No website at all", which is the single
strongest thing on any call. Dropping it would have thrown away the best finding
on every lead that has no site, so critical is kept and sorted above high.
Anything else the audit writes, ``low`` included, is dropped.
"""

HEADING_WHO_THEY_ARE: str = "WHO THEY ARE"
"""First heading."""

HEADING_WHAT_IS_WRONG: str = "WHAT IS WRONG WITH THEIR ONLINE PRESENCE"
"""Second heading."""

HEADING_LEAD_WITH: str = "WHAT TO LEAD WITH"
"""Third heading."""

HEADINGS: tuple[str, ...] = (
    HEADING_WHO_THEY_ARE,
    HEADING_WHAT_IS_WRONG,
    HEADING_LEAD_WITH,
)
"""The three headings in the frozen order. The self test checks this order."""

PROOF_INTRO: str = (
    "These are checked facts, not guesses. Use the proof, it is what makes the "
    "rep believable:"
)
"""Line printed under the second heading, wording frozen by the contract."""

LEAD_WITH_NOTE: str = "These notes are for the rep only. Do not read the money numbers out loud."
"""Line printed under the third heading.

The lead engine's own comment calls the talking points "for you only, to skim
before the call", and they carry the contract value and the close probability.
The copilot is told the rep reads its output aloud word for word, so without
this one line there is a real path where it says "contract 5950 dollars" to the
prospect. Delete this constant and the line it renders if the contract owner
wants the section bare.
"""

NO_WEBSITE_LINE: str = "They have NO website at all."
"""The strongest opening a rep can have. Never dropped, never softened."""

WEBSITE_LINE_PREFIX: str = "They have a website at "
"""Prefix for the website line. No trailing period, a period glued to a URL gets copied with it."""

MAX_NAME_CHARS: int = 200
"""Clamp on the business name, so one broken record cannot eat the whole budget."""

MAX_SHORT_FIELD_CHARS: int = 120
"""Clamp on the category and the city."""

MAX_URL_CHARS: int = 500
"""Clamp on the website URL."""

MAX_TITLE_CHARS: int = 160
"""Clamp on a pain point title."""

MAX_DETAIL_CHARS: int = 600
"""Clamp on a pain point detail. The longest one in the rep's real data is 272."""

MAX_TALKING_POINT_CHARS: int = 300
"""Clamp on one talking point. The longest one in the rep's real data is 127."""

CLAMP_MARKER: str = "..."
"""Appended when a field was actually cut, so the copilot knows it saw a slice."""

GOAL_SEO: str = "Get them to agree to a paid SEO retainer."
"""Call goal for the SEO track, wording from the contract."""

GOAL_WEBSITE: str = "Get them to agree to a first website."
"""Call goal for the Website track, wording from the contract."""

GOAL_UNKNOWN: str = "Get them to agree to a first paid job."
"""Call goal when the record carries no track. Neutral on purpose, it promises no service."""

GOAL_MONEY_NOTE: str = (
    "This money is only a guess from the lead tool. Do not say it out loud. "
    "Give a price only if it is in MY SERVICES & OFFERS."
)
"""Guard printed straight after the money sentence in the call goal.

The amount is not a price the rep set. The lead engine picks a pricing tier by
matching a keyword against the Google category, then blends a range, so the
salon in the rep's own data is worth "850" only because the word "salon" hit
tier B. The goal lands in the system prompt under "# CALL GOAL", the rep reads
the copilot out loud word for word, and the talking points already carry the
same warning, so the money in the goal has to carry it too. Without this line
the prospect asks "how much?" and the rep reads back a lookup table.
"""

MAX_CALL_GOAL_CHARS: int = 600
"""Same cap as ``models.MAX_CALL_GOAL_CHARS``, copied so this module imports nothing."""

EM_DASH: str = "\u2014"
"""The character this project bans everywhere. Written as an escape on purpose."""

HORIZONTAL_BAR: str = "\u2015"
"""The other long dash, rarer, treated the same way."""

EN_DASH: str = "\u2013"
"""The short dash. A range when it is tight between words, a break otherwise."""

_DASH_RE = re.compile(rf"\s*[{EM_DASH}{HORIZONTAL_BAR}]\s*")
"""Em dash and horizontal bar, with any space around them."""

_EN_DASH_TIGHT_RE = re.compile(rf"(?<=\w){EN_DASH}(?=\w)")
"""En dash sitting tight between two word characters, which is a range, not a break."""

_EN_DASH_RE = re.compile(rf"\s*{EN_DASH}\s*")
"""Any other en dash."""

_DOUBLE_COMMA_RE = re.compile(r",\s*,")
"""Left behind when a dash that already followed a comma is swapped for a comma."""

_COMMA_BEFORE_STOP_RE = re.compile(r",\s*([.!?;:])")
"""Left behind when a dash sat just before a full stop."""

_SPACE_RE = re.compile(r"\s+")
"""Any run of whitespace, including newlines."""

_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
"""Everything that is not a plain lowercase letter or digit, for place matching."""

ENGLISH_SPEAKING_PLACES: frozenset[str] = frozenset(
    {
        "united states",
        "united states of america",
        "usa",
        "us",
        "united kingdom",
        "uk",
        "great britain",
        "england",
        "scotland",
        "wales",
        "northern ireland",
        "gb",
        "ireland",
        "ie",
        "canada",
        "ca",
        "australia",
        "au",
        "new zealand",
        "nz",
        "singapore",
        "sg",
    }
)
"""Places where a language hint is pointless. Matching one of these returns ``None`` at once."""

PLACE_LANGUAGE: dict[str, str] = {
    # Urdu. Pakistan only, and the cities are unambiguous inside it.
    "pakistan": "ur",
    "pk": "ur",
    "lahore": "ur",
    "karachi": "ur",
    "islamabad": "ur",
    "rawalpindi": "ur",
    "faisalabad": "ur",
    "multan": "ur",
    "peshawar": "ur",
    "quetta": "ur",
    "gujranwala": "ur",
    "sialkot": "ur",
    "sargodha": "ur",
    "bahawalpur": "ur",
    "abbottabad": "ur",
    # Arabic. Gulf and North African places where business is not run in English.
    "saudi arabia": "ar",
    "sa": "ar",
    "riyadh": "ar",
    "jeddah": "ar",
    "dammam": "ar",
    "mecca": "ar",
    "makkah": "ar",
    "medina": "ar",
    "egypt": "ar",
    "eg": "ar",
    "cairo": "ar",
    "alexandria": "ar",
    "giza": "ar",
    "kuwait": "ar",
    "kuwait city": "ar",
    "qatar": "ar",
    "doha": "ar",
    "oman": "ar",
    "muscat": "ar",
    "bahrain": "ar",
    "manama": "ar",
    "jordan": "ar",
    "amman": "ar",
    "iraq": "ar",
    "baghdad": "ar",
    "basra": "ar",
    # Spanish. Spain and the Spanish speaking Americas.
    "spain": "es",
    "es": "es",
    "espana": "es",
    "madrid": "es",
    "barcelona": "es",
    "valencia": "es",
    "seville": "es",
    "sevilla": "es",
    "zaragoza": "es",
    "malaga": "es",
    "bilbao": "es",
    "mexico": "es",
    "mx": "es",
    "mexico city": "es",
    "ciudad de mexico": "es",
    "guadalajara": "es",
    "monterrey": "es",
    "puebla": "es",
    "tijuana": "es",
    "cancun": "es",
    "argentina": "es",
    "buenos aires": "es",
    "rosario": "es",
    "colombia": "es",
    "bogota": "es",
    "medellin": "es",
    "cali": "es",
    "chile": "es",
    "santiago": "es",
    "peru": "es",
    "lima": "es",
    "ecuador": "es",
    "quito": "es",
    "guayaquil": "es",
    "uruguay": "es",
    "montevideo": "es",
    "guatemala": "es",
    "guatemala city": "es",
    "dominican republic": "es",
    "santo domingo": "es",
    "panama": "es",
    "panama city": "es",
    "venezuela": "es",
    "caracas": "es",
    "bolivia": "es",
    "la paz": "es",
    # French. France only. Belgium, Switzerland, Canada and North Africa are all
    # split between two or more languages, so they are left out on purpose.
    "france": "fr",
    "fr": "fr",
    "paris": "fr",
    "lyon": "fr",
    "marseille": "fr",
    "toulouse": "fr",
    "nantes": "fr",
    "bordeaux": "fr",
    "lille": "fr",
    "strasbourg": "fr",
    "montpellier": "fr",
    "rennes": "fr",
    "toulon": "fr",
    # German. Germany and Austria. Switzerland is left out, it is split.
    "germany": "de",
    "de": "de",
    "deutschland": "de",
    "berlin": "de",
    "munich": "de",
    "munchen": "de",
    "hamburg": "de",
    "frankfurt": "de",
    "cologne": "de",
    "koln": "de",
    "stuttgart": "de",
    "dusseldorf": "de",
    "leipzig": "de",
    "dortmund": "de",
    "bremen": "de",
    "hannover": "de",
    "nuremberg": "de",
    "austria": "de",
    "vienna": "de",
    "wien": "de",
    "salzburg": "de",
    "graz": "de",
    # Hindi. Only the north Indian Hindi belt. The bare country "india" is NOT
    # here and neither are Mumbai, Bangalore, Chennai, Hyderabad, Kolkata or
    # Pune, where Hindi would often be the wrong guess and English is normal for
    # business.
    "delhi": "hi",
    "new delhi": "hi",
    "noida": "hi",
    "gurgaon": "hi",
    "gurugram": "hi",
    "ghaziabad": "hi",
    "faridabad": "hi",
    "lucknow": "hi",
    "kanpur": "hi",
    "jaipur": "hi",
    "indore": "hi",
    "bhopal": "hi",
    "patna": "hi",
    "varanasi": "hi",
    "agra": "hi",
}
"""Place name to a two letter language code, for ``lead_language_hint`` only.

Every code in here is one of the seven the backend supports (en, ur, hi, es, ar,
fr, de). Places that are split between languages are deliberately absent, see
that function's docstring.
"""


@dataclass(frozen=True)
class PainPoint:
    """One checked finding from the audit, already cleaned for the prompt.

    Attributes:
        title: Short name of the problem, for example "The site is very slow".
        detail: The sentence that explains why it costs the business money.
        proof: The measured value, byte for byte as the audit wrote it. May be
            empty, plenty of findings have nothing to measure.
        severity: One of ``critical``, ``high`` or ``medium``. Everything else
            was already filtered out.
    """

    title: str
    detail: str
    proof: str
    severity: str


@dataclass(frozen=True)
class LeadFacts:
    """Everything ``build_lead_context`` needs, pulled out of any lead shape.

    Attributes:
        name: Business name, exactly as scraped. Never re cased, "bp" stays "bp".
        category: Google category, for example "Hair salon". May be empty.
        city: The city the search ran in. May be empty.
        website: The website URL, or empty when they have none.
        rating: Star rating, or ``None`` when there is no rating.
        reviews: Review count, or ``None``. A zero is read as ``None``, see
            ``_as_count``.
        local_rank: Position inside this one search, or ``None``. A zero is read
            as ``None``, see ``_as_count``.
        track: ``SEO``, ``WEBSITE`` or empty, in whatever case the record used.
        pain_points: Kept findings, already filtered, sorted and capped.
        talking_points: The rep's own skim notes, in the audit's order.
        deal_value: Money up front, or ``None``.
        retainer: Money per month, or ``None``.
        currency_symbol: For example ``$``. May be empty.
        currency_code: For example ``USD``. May be empty.
        address: Full street address. May be empty.
        country: Country, when the record carries one. May be empty.
    """

    name: str
    category: str
    city: str
    website: str
    rating: float | None
    reviews: int | None
    local_rank: int | None
    track: str
    pain_points: tuple[PainPoint, ...]
    talking_points: tuple[str, ...]
    deal_value: float | None
    retainer: float | None
    currency_symbol: str
    currency_code: str
    address: str
    country: str


def plain_text(value: str) -> str:
    """Strip the dashes this project bans and squeeze the whitespace.

    An em dash is replaced with a comma, which is exactly what the contract's own
    sample output does to the audit's ``need_reason``. An en dash between two
    word characters is a range, so it becomes a hyphen. Any other en dash is a
    break, so it becomes a comma too. Every run of whitespace, newlines included,
    collapses to one space, because each rendered line has to stay one line.

    Never call this on a ``proof`` string. Proof is copied through untouched.

    Args:
        value: Raw text out of the lead record.

    Returns:
        The cleaned text, stripped at both ends.
    """
    if not value:
        return ""
    text = _EN_DASH_TIGHT_RE.sub("-", value)
    text = _DASH_RE.sub(", ", text)
    text = _EN_DASH_RE.sub(", ", text)
    text = _DOUBLE_COMMA_RE.sub(",", text)
    text = _COMMA_BEFORE_STOP_RE.sub(r"\1", text)
    text = _SPACE_RE.sub(" ", text)
    return text.strip().lstrip(",").strip()


def _clamp(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters on a word boundary.

    Args:
        text: Already cleaned text.
        limit: Longest result allowed, before the marker.

    Returns:
        The text untouched when it already fits, otherwise the cut text with
        ``CLAMP_MARKER`` on the end.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind(" ")
    if boundary > limit // 2:
        cut = cut[:boundary]
    return cut.rstrip(" ,.;:") + CLAMP_MARKER


def _sources(lead: LeadLike) -> list[object]:
    """Collect every object a field might live on, best first.

    ``services/leads.py`` owns the ``Lead`` dataclass, this module runs before
    and beside it, and callers also hand it raw scored records. So instead of
    binding to one shape, look at the object itself, then at any nested mapping
    it carries, then at ``to_wire()`` if it has one.

    Args:
        lead: Whatever the caller passed.

    Returns:
        The objects to search, in priority order. Always at least one.
    """
    found: list[object] = [lead]
    for attr in ("scored", "raw", "row", "record", "data"):
        nested = lead.get(attr) if isinstance(lead, Mapping) else getattr(lead, attr, None)
        if isinstance(nested, Mapping):
            found.append(nested)
    to_wire = getattr(lead, "to_wire", None)
    if callable(to_wire):
        try:
            wire = to_wire()
        except Exception:  # noqa: BLE001 a helper on someone else's object must never break us
            logger.debug("to_wire() raised while reading a lead, ignoring it", exc_info=True)
        else:
            if isinstance(wire, Mapping):
                found.append(wire)
    return found


def _raw_field(lead: LeadLike, *names: str) -> Any | None:
    """Return the first value found for any of ``names`` on any source.

    Sources are tried in the order ``_sources`` returned them, and inside one
    source the names are tried in the order given, so the caller controls which
    spelling wins.

    Args:
        lead: Whatever the caller passed.
        *names: Field names to try, snake_case and camelCase both welcome.

    Returns:
        The first value that is not ``None``, or ``None`` when nothing matched.
    """
    for source in _sources(lead):
        for name in names:
            if isinstance(source, Mapping):
                if name in source and source[name] is not None:
                    return source[name]
                continue
            value = getattr(source, name, None)
            if value is not None and not callable(value):
                return value
    return None


def _as_text(value: Any | None) -> str:
    """Coerce any value into cleaned plain text.

    Args:
        value: Anything, usually a string or ``None``.

    Returns:
        The cleaned text, or an empty string for ``None``.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return plain_text(value)
    if isinstance(value, bool):
        return ""
    return plain_text(str(value))


def _as_number(value: Any | None) -> float | None:
    """Coerce any value into a float, without ever raising.

    Args:
        value: Anything, usually a number, a numeric string or ``None``.

    Returns:
        The number, or ``None`` when the value is missing or not numeric.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # JSON has no integer ceiling, so a hand edited scores file can carry an
        # int too big for a float. That is the one way this coercion could raise.
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return None
        return number if number == number else None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _as_int(value: Any | None) -> int | None:
    """Coerce any value into an int, without ever raising.

    Args:
        value: Anything, usually a number, a numeric string or ``None``.

    Returns:
        The whole number, or ``None`` when the value is missing or not numeric.
    """
    number = _as_number(value)
    return None if number is None else int(number)


def _as_count(value: Any | None) -> int | None:
    """Coerce a count or a position into an int, reading a zero as unknown.

    A review count answers "how many" and a local rank answers "which place", so
    neither can honestly be zero. Two things make a zero show up anyway:
    ``services/leads.py`` types both fields as a plain ``int`` and collapses a
    missing one to ``0``, and a search whose audit has not run yet has no ranks
    at all, which is a normal state the contract names. The lead engine itself
    says nothing when either field is falsy.

    So a zero here is a missing value wearing a number. Printing it would put
    "0 reviews." or "Ranked 0 locally." in front of the rep as a checked fact,
    and there is no rank zero and no null review count that means zero. Dropping
    the line is the honest answer, and dropping a line is what this module does
    with every missing field.

    Args:
        value: Anything, usually a number, a numeric string or ``None``.

    Returns:
        The count when it is one or more, otherwise ``None``.
    """
    number = _as_int(value)
    if number is None or number <= 0:
        return None
    return number


def _mapping_field(source: Any | None, *names: str) -> Any | None:
    """Read one of ``names`` out of a mapping, or out of an object's attributes.

    Args:
        source: The nested ``money`` block, or anything else, or ``None``.
        *names: Field names to try in order.

    Returns:
        The first value that is not ``None``, or ``None``.
    """
    if source is None:
        return None
    for name in names:
        if isinstance(source, Mapping):
            if name in source and source[name] is not None:
                return source[name]
            continue
        value = getattr(source, name, None)
        if value is not None and not callable(value):
            return value
    return None


def _read_pain_points(lead: LeadLike, limit: int) -> tuple[PainPoint, ...]:
    """Pull the pain points out of a lead, filter them, sort them and cap them.

    Only ``critical``, ``high`` and ``medium`` survive, strongest first. The sort
    is stable, so inside one severity the audit's own order is kept, and the
    audit already orders by how much each finding costs.

    Args:
        lead: Whatever the caller passed.
        limit: Most pain points to return.

    Returns:
        The kept pain points, at most ``limit`` of them, possibly empty.
    """
    raw = _raw_field(lead, "pain_points", "painPoints")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()

    kept: list[PainPoint] = []
    for item in raw:
        severity = _as_text(_mapping_field(item, "severity")).lower()
        if severity not in SEVERITY_ORDER:
            continue
        title = _clamp(_as_text(_mapping_field(item, "title")), MAX_TITLE_CHARS)
        detail = _clamp(
            _as_text(_mapping_field(item, "detail", "sales_line", "salesLine")),
            MAX_DETAIL_CHARS,
        )
        # Proof is the only string in this module that is not cleaned, not
        # clamped and not whitespace collapsed. It is the number the rep says
        # out loud, so it has to match the audit exactly. Only the surrounding
        # whitespace goes, and only so an empty proof can be spotted.
        raw_proof = _mapping_field(item, "proof", "evidence")
        proof = raw_proof.strip() if isinstance(raw_proof, str) else ""
        if not title and not detail:
            continue
        kept.append(PainPoint(title=title, detail=detail, proof=proof, severity=severity))

    kept.sort(key=lambda point: SEVERITY_ORDER[point.severity])
    return tuple(kept[: max(limit, 0)])


def _read_talking_points(lead: LeadLike) -> tuple[str, ...]:
    """Pull the talking points out of a lead, in the order the audit wrote them.

    Args:
        lead: Whatever the caller passed.

    Returns:
        The cleaned talking points, possibly empty.
    """
    raw = _raw_field(lead, "talking_points", "talkingPoints")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    points: list[str] = []
    for item in raw:
        text = _clamp(_as_text(item), MAX_TALKING_POINT_CHARS)
        if text:
            points.append(text)
    return tuple(points)


def read_lead_facts(lead: LeadLike, *, max_pain_points: int = MAX_PAIN_POINTS) -> LeadFacts:
    """Read any lead shape into the one struct the rest of this module uses.

    Accepts the ``Lead`` dataclass from ``services/leads.py``, its wire dict, a
    raw scored record out of ``<slug>.scores.json``, a raw ndjson row, or a dict
    that merged several of those. Nothing here raises: a field that is missing,
    the wrong type or garbage comes back empty or ``None``.

    Args:
        lead: Whatever the caller has.
        max_pain_points: Most pain points to keep.

    Returns:
        A fully populated ``LeadFacts``. Every string field is at worst empty.
    """
    money = _raw_field(lead, "money")

    website = _as_text(_raw_field(lead, "website", "site", "website_url", "websiteUrl", "url"))
    currency_symbol = _as_text(
        _mapping_field(money, "currency_symbol", "currencySymbol")
        or _raw_field(lead, "currency_symbol", "currencySymbol", "currency")
    )
    # The wire shape puts the symbol itself in "currency", the scored record puts
    # the code there. A three letter code is a code, one or two characters is a
    # symbol, so the two never get mixed up.
    currency_code = _as_text(_mapping_field(money, "currency") or _raw_field(lead, "currency"))
    if len(currency_symbol) > 2:
        currency_code = currency_code or currency_symbol
        currency_symbol = ""
    if len(currency_code) <= 2:
        currency_symbol = currency_symbol or currency_code
        currency_code = ""

    return LeadFacts(
        name=_clamp(_as_text(_raw_field(lead, "name", "business_name", "businessName")), MAX_NAME_CHARS),
        category=_clamp(_as_text(_raw_field(lead, "category")), MAX_SHORT_FIELD_CHARS),
        city=_clamp(
            _as_text(_raw_field(lead, "city", "search_location", "searchLocation", "location")),
            MAX_SHORT_FIELD_CHARS,
        ),
        website=_clamp(website, MAX_URL_CHARS),
        rating=_as_number(_raw_field(lead, "rating", "stars")),
        reviews=_as_count(_raw_field(lead, "reviews", "review_count", "reviewCount")),
        local_rank=_as_count(_raw_field(lead, "local_rank", "localRank", "rank")),
        track=_as_text(_raw_field(lead, "track", "deal_type", "dealType")),
        pain_points=_read_pain_points(lead, max_pain_points),
        talking_points=_read_talking_points(lead),
        deal_value=_as_number(
            _mapping_field(money, "deal_value_usd", "dealValueUsd", "deal_value", "dealValue")
            or _raw_field(lead, "deal_value_usd", "dealValue", "deal_value")
        ),
        retainer=_as_number(
            _mapping_field(money, "retainer_monthly_usd", "retainerMonthlyUsd", "retainer_monthly")
            or _raw_field(lead, "retainer_monthly_usd", "retainerMonthly")
        ),
        currency_symbol=currency_symbol,
        currency_code=currency_code,
        address=_clamp(_as_text(_raw_field(lead, "address")), MAX_NAME_CHARS),
        country=_as_text(_raw_field(lead, "country", "search_country", "searchCountry")),
    )


def _format_rating(value: float) -> str:
    """Format a star rating the way the audit itself writes one.

    Args:
        value: The rating.

    Returns:
        One decimal place, for example ``4.8`` or ``5.0``.
    """
    return f"{value:.1f}"


def _format_amount(value: float, symbol: str, code: str) -> str:
    """Format a money amount for a spoken sentence.

    Args:
        value: The amount.
        symbol: Currency symbol, for example ``$``. May be empty.
        code: Currency code, for example ``USD``. May be empty.

    Returns:
        The amount with thousands separators, carrying whichever of the symbol
        or the code the record actually had. Neither is invented.
    """
    number = f"{int(value):,}" if float(value).is_integer() else f"{value:,.2f}"
    if symbol:
        return f"{symbol}{number}"
    if code:
        return f"{number} {code}"
    return number


def _who_they_are_lines(facts: LeadFacts) -> list[str]:
    """Build the WHO THEY ARE body.

    Three lines at most, and a line is dropped rather than half filled. The rep
    reads this section out loud, so a stat is only printed when it is a real
    measured value. ``read_lead_facts`` already turns a zero rating, a zero
    review count and a zero rank into ``None``, and the checks below say the
    same thing again, so a hand built ``LeadFacts`` cannot slip a zero through
    either.

    Args:
        facts: The normalized lead.

    Returns:
        The body lines, possibly empty.
    """
    lines: list[str] = []

    if facts.name or facts.category or facts.city:
        opening = facts.name
        if facts.category:
            opening = f"{opening}, a {facts.category}" if opening else f"A {facts.category}"
        if facts.city:
            opening = f"{opening} in {facts.city}" if opening else f"In {facts.city}"
        lines.append(f"{opening}.")

    stats: list[str] = []
    rating = facts.rating if facts.rating is not None and facts.rating > 0 else None
    reviews = facts.reviews if facts.reviews is not None and facts.reviews > 0 else None
    if rating is not None and reviews is not None:
        word = "review" if reviews == 1 else "reviews"
        stats.append(f"{_format_rating(rating)} stars from {reviews} {word}.")
    elif rating is not None:
        stats.append(f"{_format_rating(rating)} stars.")
    elif reviews is not None:
        word = "review" if reviews == 1 else "reviews"
        stats.append(f"{reviews} {word}.")
    if facts.local_rank is not None and facts.local_rank > 0:
        stats.append(f"Ranked {facts.local_rank} locally.")
    if stats:
        lines.append(" ".join(stats))

    # No website is not a missing field, it is the finding. It is always said.
    if facts.website:
        lines.append(f"{WEBSITE_LINE_PREFIX}{facts.website}")
    else:
        lines.append(NO_WEBSITE_LINE)

    return lines


def _pain_point_line(point: PainPoint) -> str:
    """Render one pain point as one bullet.

    Args:
        point: The finding.

    Returns:
        A single line starting with ``- ``. The proof clause is only there when
        the audit actually measured something.
    """
    parts: list[str] = []
    if point.title:
        title = point.title if point.title[-1] in ".!?" else f"{point.title}."
        parts.append(title)
    if point.detail:
        detail = point.detail if point.detail[-1] in ".!?" else f"{point.detail}."
        parts.append(detail)
    line = f"- {' '.join(parts)}"
    if point.proof:
        line = f"{line} (proof: {point.proof})"
    return line


def _what_is_wrong_lines(facts: LeadFacts) -> list[str]:
    """Build the WHAT IS WRONG WITH THEIR ONLINE PRESENCE body.

    Args:
        facts: The normalized lead.

    Returns:
        The intro line plus one bullet per kept pain point, or an empty list when
        the audit found nothing worth saying, in which case the whole heading is
        dropped.
    """
    if not facts.pain_points:
        return []
    return [PROOF_INTRO, *(_pain_point_line(point) for point in facts.pain_points)]


def _lead_with_lines(facts: LeadFacts) -> list[str]:
    """Build the WHAT TO LEAD WITH body.

    Args:
        facts: The normalized lead.

    Returns:
        The rep only note plus one bullet per talking point, or an empty list
        when there are none.
    """
    if not facts.talking_points:
        return []
    return [LEAD_WITH_NOTE, *(f"- {point}" for point in facts.talking_points)]


def _fit_sections(sections: list[tuple[str, list[str]]], max_chars: int) -> str:
    """Join the sections and keep the result inside ``max_chars``.

    Sections are added in order and a section is only ever added whole, so the
    text can only ever end on a heading boundary. A section that does not fit is
    first retried with fewer body lines, dropping from the end, which is the same
    rule as "at most five pain points" applied harder. If even its first body
    line does not fit, the whole section is dropped along with everything after
    it, and a heading with no body is never printed.

    With the rep's real data the worst lead comes to about 1500 characters, so
    none of this trimming fires. It is here for a broken record, not for a
    normal one.

    Args:
        sections: Heading and body line pairs, in the frozen order.
        max_chars: The hard ceiling.

    Returns:
        The joined block, at most ``max_chars`` long.
    """
    chosen: list[str] = []
    used = 0
    for heading, body in sections:
        if not body:
            continue
        separator = 2 if chosen else 0  # the blank line between two sections
        keep = len(body)
        while keep > 0:
            block = "\n".join([heading, *body[:keep]])
            if used + separator + len(block) <= max_chars:
                break
            keep -= 1
        if keep <= 0:
            # This section cannot fit, so nothing after it may be printed either.
            break
        block = "\n".join([heading, *body[:keep]])
        chosen.append(block)
        used += separator + len(block)
    return "\n\n".join(chosen)


def build_lead_context(
    lead: LeadLike,
    *,
    max_chars: int = MAX_CONTEXT_CHARS,
    max_pain_points: int = MAX_PAIN_POINTS,
) -> str:
    """Build the call context for one lead.

    Three headings, always in this order, and a heading is only printed when it
    has something real under it:

    ``WHO THEY ARE``
        Who the business is, how well it is rated, and whether it has a website
        at all. A business with no site gets the explicit "They have NO website
        at all." line, never silence.
    ``WHAT IS WRONG WITH THEIR ONLINE PRESENCE``
        At most five findings, critical then high then medium, each with the
        proof the audit measured, copied through untouched.
    ``WHAT TO LEAD WITH``
        The rep's own skim notes from the audit.

    Nothing is invented. A missing field drops its line. The whole block is
    capped so it cannot bloat the system prompt, and the cut lands on a heading
    boundary so a section is never half present.

    Args:
        lead: The lead, in any of the shapes ``read_lead_facts`` accepts.
        max_chars: Hard ceiling on the returned block.
        max_pain_points: Most findings the copilot may see.

    Returns:
        The context block, ready for ``PrepareContextRequest.client_context``.
        An empty string when the record carried nothing at all, which the request
        model turns back into ``None``.
    """
    facts = read_lead_facts(lead, max_pain_points=max_pain_points)
    sections: list[tuple[str, list[str]]] = [
        (HEADING_WHO_THEY_ARE, _who_they_are_lines(facts)),
        (HEADING_WHAT_IS_WRONG, _what_is_wrong_lines(facts)),
        (HEADING_LEAD_WITH, _lead_with_lines(facts)),
    ]
    return _fit_sections(sections, max_chars)


def build_call_goal(lead: LeadLike) -> str:
    """Build the one line call goal from the track and the money.

    The track says what is being sold. SEO means a monthly retainer, Website
    means a first site. The money says how big the deal is, so the copilot knows
    how hard to push, and it comes with ``GOAL_MONEY_NOTE`` so the copilot does
    not hand the number to the prospect as a quote. The amount is the lead
    engine's own tier estimate, not a price the rep set, and the rep reads the
    copilot out loud, so the only place a real price may come from is the
    knowledge base.

    Args:
        lead: The lead, in any of the shapes ``read_lead_facts`` accepts.

    Returns:
        One to four short sentences, never longer than ``MAX_CALL_GOAL_CHARS``.
        The money sentence is left out when the record has no money on it, and
        it is also left out whole, guard and all, in the one odd case where a
        broken amount would push the goal past the cap. A price with the guard
        cut off it is worse than no price.
    """
    facts = read_lead_facts(lead, max_pain_points=0)
    track = facts.track.strip().lower()
    if track == "seo":
        goal = GOAL_SEO
    elif track in {"website", "web"}:
        goal = GOAL_WEBSITE
    else:
        goal = GOAL_UNKNOWN

    up_front = (
        _format_amount(facts.deal_value, facts.currency_symbol, facts.currency_code)
        if facts.deal_value
        else ""
    )
    monthly = (
        _format_amount(facts.retainer, facts.currency_symbol, facts.currency_code)
        if facts.retainer
        else ""
    )
    if up_front and monthly:
        money = f"This deal is worth about {up_front} to start and {monthly} each month."
    elif up_front:
        money = f"This deal is worth about {up_front} to start."
    elif monthly:
        money = f"This deal is worth about {monthly} each month."
    else:
        money = ""

    if money:
        with_money = f"{goal} {money} {GOAL_MONEY_NOTE}"
        if len(with_money) <= MAX_CALL_GOAL_CHARS:
            return with_money

    return goal[:MAX_CALL_GOAL_CHARS]


def _normalize_place(value: str) -> str:
    """Fold a place name into something two spellings can both match on.

    Accents are stripped, case is folded, and everything that is not a letter or
    a digit becomes a single space, so ``München`` and ``Munchen`` and
    ``MUNICH,`` all land on the same key.

    Args:
        value: A raw city, country or country code.

    Returns:
        The folded name, or an empty string.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_WORD_RE.sub(" ", stripped.casefold()).strip()


def _country_from_address(address: str) -> str:
    """Take the country off the end of a Google address.

    Google writes an address as street, city, region postcode, country, so the
    last comma separated part is the country. When there are no commas there is
    no country to read, and the empty string is returned rather than a guess at
    which word might be one.

    Args:
        address: The full address string.

    Returns:
        The last comma separated part, or an empty string.
    """
    if "," not in address:
        return ""
    return address.rsplit(",", 1)[-1].strip()


def lead_language_hint(lead: LeadLike) -> str | None:
    """Guess the language for this call from the city or the country.

    Be careful with this. It is a lookup table over place names, nothing more. It
    does not know who actually answers the phone, and plenty of business in every
    country on earth is done in English. Treat what it returns as a default the
    rep can change in one click, never as a fact.

    What it will not do:

    - It returns ``None`` the moment the country is one where English is normal,
      before it ever looks at the city, so a "Lahore Road" in Ontario is safe.
    - It leaves out every country that is split between languages. Switzerland,
      Belgium, Canada, Morocco, Lebanon and the UAE are all absent on purpose,
      because guessing one of their languages is as likely to be wrong as right.
    - It leaves out the bare country "India" and every Indian city outside the
      Hindi belt, because Hindi is the wrong guess in Chennai or Bangalore and
      English is normal for business across the country.
    - It only reads a country out of an address when the address has commas,
      which is how Google writes one. It never sniffs for a country word.

    Args:
        lead: The lead, in any of the shapes ``read_lead_facts`` accepts.

    Returns:
        A two letter code out of the seven the backend supports (``ur``, ``hi``,
        ``es``, ``ar``, ``fr``, ``de``), or ``None`` when English is likely or
        the guess would not be safe. It never returns ``"en"``, because ``"en"``
        is already the default and a caller needs to be able to tell a real hint
        apart from no hint at all.
    """
    facts = read_lead_facts(lead, max_pain_points=0)

    country = _normalize_place(facts.country) or _normalize_place(
        _country_from_address(facts.address)
    )
    if country:
        if country in ENGLISH_SPEAKING_PLACES:
            return None
        hint = PLACE_LANGUAGE.get(country)
        if hint:
            return hint

    city = _normalize_place(facts.city)
    if city:
        if city in ENGLISH_SPEAKING_PLACES:
            return None
        hint = PLACE_LANGUAGE.get(city)
        if hint:
            return hint

    return None


# ---------------------------------------------------------------------------
# Self test. Runs against the rep's real data, never against made up fixtures.
# ---------------------------------------------------------------------------


def _data_dir() -> Path:
    """Locate ``leadengine/data`` from this file.

    Returns:
        The path to the data directory. It may not exist, the caller checks.
    """
    return Path(__file__).resolve().parents[3] / "leadengine" / "data"


def _load_search(slug: str) -> dict[str, dict[str, Any]]:
    """Load one real search and join its scored leads onto its scraped rows.

    This is the same left join ``services/leads.py`` does, kept deliberately
    small here so the self test depends on nothing but the files on disk.

    Args:
        slug: The search slug, for example ``barber-hoboken``.

    Returns:
        A dict of lead key to a merged record. Empty when the files are missing.
    """
    directory = _data_dir()
    rows: dict[str, dict[str, Any]] = {}
    ndjson = directory / f"{slug}.ndjson"
    if ndjson.exists():
        for line in ndjson.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = str(row.get("feature_id") or "")
            if key:
                rows[key] = row

    scores_path = directory / f"{slug}.scores.json"
    scored: dict[str, Any] = {}
    if scores_path.exists():
        payload = json.loads(scores_path.read_text(encoding="utf-8"))
        scored = payload.get("leads") or {}

    merged: dict[str, dict[str, Any]] = {}
    for key, row in rows.items():
        record = dict(row)
        record.update(scored.get(key) or {})
        merged[key] = record
    for key, lead in scored.items():
        if key not in merged:
            merged[key] = dict(lead)
    return merged


def _check_one(record: dict[str, Any], context: str) -> None:
    """Assert every rule the contract froze, for one real lead.

    Args:
        record: The merged lead record.
        context: The block ``build_lead_context`` produced for it.

    Raises:
        AssertionError: When any rule was broken.
    """
    name = record.get("name") or record.get("lead_key")

    assert len(context) <= MAX_CONTEXT_CHARS, f"{name}: {len(context)} chars is over the cap"
    assert HEADING_WHO_THEY_ARE in context, f"{name}: the first heading is missing"

    positions = [context.find(heading) for heading in HEADINGS if heading in context]
    assert positions == sorted(positions), f"{name}: the headings are out of order"

    # Every heading that is printed has a real line under it, never another
    # heading and never a blank.
    for heading in HEADINGS:
        if heading not in context:
            continue
        tail = context.split(heading, 1)[1]
        first_line = tail.split("\n")[1] if "\n" in tail else ""
        assert first_line.strip(), f"{name}: {heading} has an empty body"
        assert first_line not in HEADINGS, f"{name}: {heading} is followed by another heading"

    website = str(record.get("website") or "").strip()
    if website:
        assert WEBSITE_LINE_PREFIX + website in context, f"{name}: the website line is wrong"
        assert NO_WEBSITE_LINE not in context, f"{name}: says no website but has one"
    else:
        assert NO_WEBSITE_LINE in context, f"{name}: has no website and does not say so"
        assert WEBSITE_LINE_PREFIX not in context, f"{name}: claims a website it does not have"

    kept = [
        point
        for point in (record.get("pain_points") or [])
        if str(point.get("severity") or "").lower() in SEVERITY_ORDER
    ][:MAX_PAIN_POINTS]

    if kept:
        body = context.split(HEADING_WHAT_IS_WRONG, 1)[1].split(HEADING_LEAD_WITH, 1)[0]
        bullets = [line for line in body.splitlines() if line.startswith("- ")]
        assert len(bullets) <= MAX_PAIN_POINTS, f"{name}: {len(bullets)} pain points, over five"
        assert len(bullets) == len(kept), f"{name}: {len(bullets)} bullets for {len(kept)} findings"

        severities = [str(point.get("severity")).lower() for point in kept]
        ranks = [SEVERITY_ORDER[severity] for severity in severities]
        assert ranks == sorted(ranks), f"{name}: pain points are not strongest first"

        for point in kept:
            proof = point.get("proof") or ""
            if proof:
                assert proof in context, f"{name}: proof {proof!r} was not copied through"
    else:
        assert HEADING_WHAT_IS_WRONG not in context, f"{name}: empty findings heading was printed"

    dropped = [
        point
        for point in (record.get("pain_points") or [])
        if str(point.get("severity") or "").lower() not in SEVERITY_ORDER
    ]
    for point in dropped:
        title = plain_text(str(point.get("title") or ""))
        if title:
            assert title not in context, f"{name}: weak finding {title!r} leaked in"


def _self_test() -> None:
    """Build the context for real leads and check every rule, then print a few.

    Raises:
        AssertionError: When any rule was broken.
        SystemExit: When ``leadengine/data`` is missing, so the failure reads as
            a setup problem instead of a code problem.
    """
    directory = _data_dir()
    if not directory.exists():
        raise SystemExit(f"No data directory at {directory}, nothing to test against.")

    slugs = sorted({path.name.split(".")[0] for path in directory.glob("*.scores.json")})
    if not slugs:
        raise SystemExit(f"No .scores.json files in {directory}, nothing to test against.")

    searches = {slug: _load_search(slug) for slug in slugs}
    total = 0
    longest = 0
    no_website = 0
    for slug, records in searches.items():
        for record in records.values():
            context = build_lead_context(record)
            _check_one(record, context)
            total += 1
            longest = max(longest, len(context))
            if not str(record.get("website") or "").strip():
                no_website += 1
        print(f"checked {len(records):>3} leads in {slug}")

    print(f"\nAll {total} real leads pass. Longest block {longest} chars, cap {MAX_CONTEXT_CHARS}.")
    print(f"{no_website} of them have no website at all, and every one says so.\n")

    def pick(slug: str, name: str) -> dict[str, Any]:
        """Find one real lead by name inside one search.

        Args:
            slug: The search slug.
            name: The exact business name.

        Returns:
            The merged record.

        Raises:
            AssertionError: When that lead is not in that search any more.
        """
        for record in searches[slug].values():
            if record.get("name") == name:
                return record
        raise AssertionError(f"{name} is not in {slug} any more, pick another lead")

    salon = pick("barber-hoboken", "little space salon")
    queensboro = pick("car-wash-new-york", "Queensboro Car Wash")
    lmc = pick("car-wash-new-york", "LMC Car Wash & Lube")
    spa = pick("dentist-jersey-city", "Jersey City Dental Spa")

    # A real lead with its findings and notes taken away. No lead in the rep's
    # data has zero findings, so this is the only honest way to exercise the
    # path where both of the last two headings have to disappear.
    bare = dict(queensboro)
    bare["pain_points"] = []
    bare["talking_points"] = []

    samples = [
        ("A lead with a website, five findings, SEO track", salon),
        ("A lead with NO website, critical finding kept", queensboro),
        ("A lead whose website is a social page, two findings", lmc),
        ("A lead where three weak findings are dropped", spa),
        ("The same lead with no findings and no notes", bare),
    ]
    for title, record in samples:
        context = build_lead_context(record)
        print("=" * 78)
        print(f"{title}  ({len(context)} chars)")
        print("=" * 78)
        print(context or "(empty)")
        print()
        print(f"  call goal: {build_call_goal(record)}")
        print(f"  language hint: {lead_language_hint(record)}")
        print()

    bare_context = build_lead_context(bare)
    assert HEADING_WHO_THEY_ARE in bare_context, "the no findings lead lost its first heading"
    assert HEADING_WHAT_IS_WRONG not in bare_context, "a heading was printed with no findings"
    assert HEADING_LEAD_WITH not in bare_context, "a heading was printed with no notes"
    assert NO_WEBSITE_LINE in bare_context, "the no findings lead lost its no website line"

    spa_kept = [
        point for point in spa["pain_points"] if point["severity"] in SEVERITY_ORDER
    ]
    assert len(spa_kept) == 2, "Jersey City Dental Spa should keep two of its five findings"

    assert build_call_goal(salon).startswith(GOAL_SEO), "the SEO track goal is wrong"
    assert build_call_goal(queensboro).startswith(GOAL_WEBSITE), "the Website track goal is wrong"
    assert "$850" in build_call_goal(salon), "the SEO goal lost its deal value"
    assert "$2,500" in build_call_goal(queensboro), "the Website goal lost its deal value"
    assert build_call_goal({}) == GOAL_UNKNOWN, "an empty lead should get the neutral goal"
    assert len(build_call_goal(salon)) <= MAX_CALL_GOAL_CHARS, "the call goal is too long"

    # The amount in the goal is the lead engine's tier estimate, not a price the
    # rep set, and the rep reads the copilot out loud, so money never travels in
    # the goal without its guard.
    assert GOAL_MONEY_NOTE in build_call_goal(salon), "the SEO goal money lost its guard"
    assert GOAL_MONEY_NOTE in build_call_goal(queensboro), "the Website goal money lost its guard"
    assert build_call_goal({"track": "SEO"}) == GOAL_SEO, "a goal with no money must carry no guard"

    unguarded: list[Any] = []
    for records in searches.values():
        for record in records.values():
            goal = build_call_goal(record)
            if any(character.isdigit() for character in goal) and GOAL_MONEY_NOTE not in goal:
                unguarded.append(record.get("name"))
    assert not unguarded, f"money with no guard in the goal for {unguarded[:3]}"

    # A broken amount must drop the money sentence whole, never leave a price
    # with the guard sliced off the end of it.
    giant = dict(salon)
    giant["money"] = {**salon["money"], "deal_value_usd": 1e300, "retainer_monthly_usd": 1e300}
    giant_goal = build_call_goal(giant)
    assert len(giant_goal) <= MAX_CALL_GOAL_CHARS, "a broken amount broke the call goal cap"
    assert giant_goal == GOAL_SEO, "a money sentence over the cap must be dropped whole"
    print("Every call goal that names money carries the do not say it guard.")

    # The cap and the heading boundary rule, forced with a small ceiling.
    for ceiling in (0, 40, 120, 400, 900, 1200, MAX_CONTEXT_CHARS):
        clipped = build_lead_context(salon, max_chars=ceiling)
        assert len(clipped) <= ceiling, f"cap {ceiling} was broken, got {len(clipped)}"
        for heading in HEADINGS:
            if heading in clipped:
                after = clipped.split(heading, 1)[1].lstrip("\n")
                assert after, f"cap {ceiling} left {heading} with no body"
    assert build_lead_context(salon, max_chars=0) == "", "a zero cap should give an empty block"
    print("Cap and heading boundary hold at every ceiling tested.")

    # Nothing at all must not raise and must not invent.
    assert build_lead_context({}) == f"{HEADING_WHO_THEY_ARE}\n{NO_WEBSITE_LINE}"
    assert build_lead_context(None) == f"{HEADING_WHO_THEY_ARE}\n{NO_WEBSITE_LINE}"

    # ``services/leads.py`` owns a ``Lead`` dataclass and a camelCase wire dict,
    # and neither exists yet, so both shapes are exercised here against a real
    # record. All three must read the same as the raw dict does.
    class _StandInLead:
        """A lead shaped the way the ``Lead`` dataclass will be shaped.

        Attributes mirror the wire field names from the contract, in snake_case,
        with the money block nested and a ``to_wire`` that speaks camelCase.
        """

        def __init__(self, record: dict[str, Any]) -> None:
            """Copy one merged record onto plain attributes.

            Args:
                record: A merged real lead.
            """
            self.key = record.get("lead_key", "")
            self.name = record.get("name", "")
            self.category = record.get("category", "")
            self.city = record.get("city", "")
            self.website = record.get("website", "")
            self.phone = record.get("phone", "")
            self.address = record.get("address", "")
            self.rating = record.get("rating")
            self.reviews = record.get("review_count")
            self.local_rank = record.get("local_rank")
            self.track = record.get("track", "")
            self.money = record.get("money") or {}
            self.pain_points = record.get("pain_points") or []
            self.talking_points = record.get("talking_points") or []

        def to_wire(self) -> dict[str, Any]:
            """Return the camelCase shape the REST layer sends.

            Returns:
                The wire dict for this lead.
            """
            return {
                "key": self.key,
                "name": self.name,
                "category": self.category,
                "city": self.city,
                "website": self.website,
                "rating": self.rating,
                "reviews": self.reviews,
                "track": self.track,
                "dealValue": self.money.get("deal_value_usd"),
                "currency": self.money.get("currency_symbol"),
                "painPoints": self.pain_points,
                "talkingPoints": self.talking_points,
            }

    for record in (salon, queensboro, lmc, spa):
        stand_in = _StandInLead(record)
        assert build_lead_context(stand_in) == build_lead_context(record), (
            f"{record['name']}: an object reads differently from a dict"
        )
        assert build_call_goal(stand_in) == build_call_goal(record)
        assert lead_language_hint(stand_in) == lead_language_hint(record)

        wire = _StandInLead(record).to_wire()
        wire_context = build_lead_context(wire)
        assert HEADING_WHO_THEY_ARE in wire_context, "the wire shape lost its first heading"
        for point in record["pain_points"][:MAX_PAIN_POINTS]:
            if point["severity"] in SEVERITY_ORDER and point["proof"]:
                assert point["proof"] in wire_context, "the wire shape lost a proof"
        symbol = record["money"]["currency_symbol"]
        assert symbol in build_call_goal(wire), "the wire shape lost its currency symbol"
    print("Dataclass style objects and camelCase wire dicts read the same as raw records.")

    # A zero is a missing value, not a fact, and both zeros below are normal
    # states in the rep's own files. A search whose audit has not run yet has no
    # ranks at all, and two real car wash rows carry a null review count that
    # the Lead dataclass types as an int, so it reads back as 0.
    def who_block(record: dict[str, Any]) -> str:
        """Return only the WHO THEY ARE body of one built context.

        Args:
            record: A merged lead record.

        Returns:
            The first section without its heading and without any later section.
        """
        block = build_lead_context(record)
        return block.split(HEADING_WHO_THEY_ARE, 1)[-1].split("\n\n", 1)[0]

    real_rank = f"Ranked {salon['local_rank']} locally."
    assert real_rank in who_block(salon), "a real rank stopped printing"

    unranked = {**salon, "local_rank": 0}
    assert "Ranked" not in who_block(unranked), "a rank of zero was printed as a fact"
    assert "4.8 stars from 31 reviews." in who_block(unranked), "the stats went with the rank"

    unreviewed = {**salon, "rating": None, "review_count": 0}
    assert "review" not in who_block(unreviewed), "a zero review count was printed as a fact"
    assert real_rank in who_block(unreviewed), "the rank went with the reviews"

    # And the same two zeros through the object path, which is how a lead
    # actually reaches this module from the endpoint.
    zeroed = _StandInLead({**salon, "rating": None, "review_count": 0, "local_rank": 0})
    zeroed_context = build_lead_context(zeroed)
    assert "Ranked" not in zeroed_context, "an object with rank zero printed a rank"
    assert "0 reviews" not in zeroed_context, "an object with zero reviews printed a count"
    assert HEADING_WHO_THEY_ARE in zeroed_context, "the first heading was lost"
    assert build_lead_context(zeroed.to_wire()) == zeroed_context, "the wire shape reads differently"

    # Two real leads carry a null review count in car-wash-new-york. Neither may
    # ever be told they have zero reviews.
    nulls = [
        record
        for record in searches["car-wash-new-york"].values()
        if record.get("review_count") is None
    ]
    assert nulls, "car-wash-new-york no longer has a lead with a null review count"
    for record in nulls:
        block = who_block(record)
        assert "review" not in block, f"{record.get('name')}: a null review count printed as zero"
    print(f"A zero rank and a zero review count are dropped, checked on {len(nulls)} real nulls.")

    # The language hint, on real cities and on made up ones.
    assert lead_language_hint(salon) is None, "hoboken is English speaking"
    assert lead_language_hint(queensboro) is None, "new york is English speaking"
    assert lead_language_hint({"city": "lahore"}) == "ur", "lahore should hint Urdu"
    assert lead_language_hint({"city": "Lahore", "country": "Pakistan"}) == "ur"
    assert lead_language_hint({"city": "lahore", "address": "1 Main St, Toronto, Canada"}) is None
    assert lead_language_hint({"city": "Munchen"}) == "de", "munich should hint German"
    assert lead_language_hint({"city": "München"}) == "de", "accents must fold"
    assert lead_language_hint({"city": "Málaga"}) == "es", "malaga should hint Spanish"
    assert lead_language_hint({"city": "dubai"}) is None, "the UAE is left out on purpose"
    assert lead_language_hint({"city": "chennai"}) is None, "south India is left out on purpose"
    assert lead_language_hint({"city": "lucknow"}) == "hi", "the Hindi belt is in"
    assert lead_language_hint({}) is None, "an empty lead has no hint"
    print("Language hints behave, including the places left out on purpose.")

    # The dash rule, on this project's own text.
    rendered = "\n".join(build_lead_context(record) for record in searches["barber-hoboken"].values())
    assert EM_DASH not in rendered, "an em dash reached a barber-hoboken block"

    # And the one place an em dash is allowed through, on purpose.
    dashed = [
        point.get("proof")
        for records in searches.values()
        for record in records.values()
        for point in (record.get("pain_points") or [])
        if EM_DASH in str(point.get("proof") or "")
    ]
    if dashed:
        print(
            f"\nNote: {len(dashed)} proof string(s) in the rep's data contain an em dash. "
            "Proof is copied byte for byte, so those go through untouched. Everything "
            "this module writes itself uses a comma."
        )

    print("\nSelf test passed.")
    print(json.dumps({"searches": len(searches), "leads": total, "longest": longest}))


if __name__ == "__main__":
    _self_test()
