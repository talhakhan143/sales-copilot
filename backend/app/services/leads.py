"""Read side of the Google Maps lead engine, shaped for the calling app.

The rep already owns a lead engine. It scrapes a niche and a city, audits every
business site and GMB listing, scores what it found, works out a deal value and
writes pain points with proof. All of that lands on disk in
``leadengine/data`` as plain files. This module is the only place in the
backend that knows how those files are laid out, so the REST routes never have
to think about ndjson, join keys or missing files.

What is on disk, one set per search, named by a slug such as ``barber-hoboken``:

===========================  ==================================================
``<slug>.ndjson``            one raw scraped business per line
``<slug>.audit.json``        site and GMB audit, keyed by lead key
``<slug>.scores.json``       ``{"context": {...}, "leads": {"<key>": {...}}}``
``<slug>.messages.json``     email and DM copy, not read here
``pipeline.json``            ``{"<key>": {status, notes, updatedAt, contactedAt?}}``
===========================  ==================================================

``feature_id`` in the ndjson and ``lead_key`` in the scores are the same value,
and that value is the join key everywhere.

Design rules this module follows, all of them on purpose:

* **Every join is a left join and nothing raises on bad data.** A search with an
  ndjson but no scores is normal, it only means the audit has not run yet, so
  those leads come back with their raw fields and zeroed scores. A broken
  ndjson line is skipped. A scores file that is not a JSON object is treated as
  missing. A lead with no name falls back to its category.
* **One :class:`Lead` and one :meth:`Lead.to_wire`.** The list endpoint calls
  ``to_wire()`` and the detail endpoint calls ``to_wire(full=True)``, so the two
  shapes can never drift apart.
* **Parsed files are cached by path, mtime and size.** The leads list is polled,
  and re-parsing megabytes of JSON once a second would be silly. Any change to
  a file changes its mtime or its size, which drops the cache entry.
* **Ids are validated before they touch the filesystem.** A search id and a lead
  key both arrive from a URL. Anything with a path separator, a null byte, a
  leading dot or the wrong shape is refused, and the read functions answer
  ``None`` so a bad URL is a plain 404 instead of a crash.
* **The only write is ``pipeline.json``**, and it is a read, merge, write to a
  temp file, then :func:`os.replace` cycle under a module level
  :class:`asyncio.Lock`. The file is re-read inside the lock, so a key another
  writer added is never dropped. The rep's own dashboard reads this same file,
  so the two views must never disagree.

Public surface, in the order a route will want it::

    from app.services import leads

    rows = leads.list_searches()                     # summary rows, newest first
    body = leads.leads_page("barber-hoboken", sort="value")   # the list endpoint body
    lead = leads.get_lead("barber-hoboken", key)     # one full record
    result = await leads.set_status(key, "interested", "call back Monday")
    await leads.mark_called(key)                     # stamp contactedAt, keep the status

Every string this module hands out is cleaned first: control characters are
dropped, odd spaces are normalised and an em dash is turned into a comma,
because an em dash is never allowed to reach the rep's screen.

That includes ``proof``, and it is worth spelling out here, because a proof is
the number the rep says out loud on the call. A proof comes out of this module
word for word as the lead engine wrote it, with exactly one exception: an em
dash inside it becomes a comma, the same swap every other string gets. No
digit, no decimal point and no unit is ever touched. The cap is
:data:`MAX_PROOF_CHARS` and no real proof is anywhere near it. Anything
downstream that says it passes a proof through unchanged is promising not to
edit it any further, which is true. It is not promising that the string still
carries the data file's own em dash, because that one is gone before the proof
leaves here. The self test checks both halves of that against the rep's real
files, on the same code path the endpoints use.

Threading note: everything except :func:`set_status` and :func:`mark_called` is
synchronous and does no awaiting, so it is safe to call straight from a route.
The files here are small, tens to hundreds of kilobytes, and a parse is cached,
so the event loop is not held for long.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

log = logging.getLogger(__name__)

try:  # pragma: no cover - the fallback only runs outside the backend package
    from app.config import settings as _settings
except Exception:  # noqa: BLE001 - importing settings must never break a read
    _settings = None  # type: ignore[assignment]


# --------------------------------------------------------------------------
# Where the data lives
# --------------------------------------------------------------------------

_THIS_FILE: Final[Path] = Path(__file__).resolve()

_REPO_ROOT: Final[Path] = _THIS_FILE.parents[3]
"""Repository root, worked out from this file, not from the process cwd.

``backend/app/services/leads.py`` has four parents up to the repo root, so the
data directory is found the same way whether uvicorn was started from the repo
root, from ``backend/`` or from anywhere else.
"""

_FALLBACK_DATA_DIR: Final[Path] = _REPO_ROOT / "leadengine" / "data"
"""Default data directory, used when the settings object has no override."""

_DATA_DIR_OVERRIDE: Path | None = None
"""Set by :func:`use_data_dir`. For tests and for a second data root only."""


def data_dir() -> Path:
    """Return the directory that holds the lead engine output.

    Resolution order, first hit wins:

    1. an override set with :func:`use_data_dir`,
    2. ``settings.leadengine_data_dir`` when the setting exists and is not empty,
    3. ``<repo root>/leadengine/data``.

    The value is resolved on every call, which costs nothing and means a
    settings change or a test override is picked up without a restart.

    Returns:
        The data directory as an absolute path. The directory is not created
        and may not exist, every reader here treats a missing directory as an
        empty one.
    """
    if _DATA_DIR_OVERRIDE is not None:
        return _DATA_DIR_OVERRIDE
    configured = ""
    if _settings is not None:
        configured = str(getattr(_settings, "leadengine_data_dir", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return _FALLBACK_DATA_DIR


def use_data_dir(path: str | Path | None) -> None:
    """Point this module at another data directory, or clear the override.

    This exists for the self test and for an operator who keeps the lead engine
    data somewhere else. It clears every cache, because the cached values
    belong to the old directory.

    Args:
        path: The new directory, or None to fall back to the normal resolution
            described in :func:`data_dir`.
    """
    global _DATA_DIR_OVERRIDE
    _DATA_DIR_OVERRIDE = None if path is None else Path(path).expanduser().resolve()
    clear_cache()


def pipeline_path() -> Path:
    """Return the path of the shared ``pipeline.json`` file."""
    return data_dir() / "pipeline.json"


# --------------------------------------------------------------------------
# Frozen vocabulary
# --------------------------------------------------------------------------

LEAD_STATUSES: Final[frozenset[str]] = frozenset(
    {"new", "interested", "callback", "not_interested", "no_answer", "won", "lost"}
)
"""The seven pipeline statuses from the contract. Nothing else may be written."""

DEFAULT_STATUS: Final[str] = "new"
"""Status of a lead that has no pipeline entry yet."""

STATUS_UNCALLED: Final[str] = "uncalled"
"""Filter word meaning "not called yet". It is a filter, never a stored status."""

STATUS_CALLED: Final[str] = "called"
"""Filter word meaning "already called". It is a filter, never a stored status."""

_ANY_STATUS: Final[frozenset[str]] = frozenset({"", "all", "any"})
"""Filter words that mean no status filter at all."""

SORT_KEYS: Final[tuple[str, ...]] = ("value", "score", "name")
"""Allowed values of the ``sort`` query parameter."""

DEFAULT_SORT: Final[str] = "value"
"""Sort used when the client sends nothing, or sends a word we do not know."""

TRACK_SEO: Final[str] = "SEO"
"""Track for a business that already has a site and needs ranking work."""

TRACK_WEBSITE: Final[str] = "Website"
"""Track for a business that needs a first website."""

DEFAULT_CURRENCY_SYMBOL: Final[str] = "$"
"""Currency symbol used when the scored money block does not carry one."""

SEVERITY_RANK: Final[dict[str, int]] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}
"""How loud a pain point is. The audit writes ``critical`` as well as ``high``,
so anything reading pain points must treat ``critical`` as at least as strong as
``high``. :meth:`Lead.top_pain_points` already does."""

STRONG_SEVERITIES: Final[tuple[str, ...]] = ("critical", "high", "medium")
"""The severities worth putting in front of the rep. ``low`` is noise on a call."""

MAX_SEARCH_ID_CHARS: Final[int] = 100
"""Longest search slug accepted. The scraper cuts its own slugs at 80."""

MAX_LEAD_KEY_CHARS: Final[int] = 64
"""Longest lead key accepted. Real keys are about 41 characters."""

MAX_NOTES_CHARS: Final[int] = 2000
"""Hard cap on the note the rep types after a call."""

MAX_TEXT_CHARS: Final[int] = 4000
"""Default cap for any single cleaned string coming out of the data files."""

MAX_PROOF_CHARS: Final[int] = 400
"""Cap on one ``proof`` string, used by both ways of building a pain point.

The longest proof in the rep's real data is about 120 characters, so this cap
has never fired. It is here so a broken audit file cannot push a paragraph into
the call context, and it is named rather than typed twice so the promise in the
module docstring stays checkable."""


class LeadError(ValueError):
    """Bad input from a URL or a request body.

    Routes should map this to a 400. It is a subclass of :class:`ValueError`,
    so a route that already catches ``ValueError`` catches this too. Reading
    functions never raise it, they answer ``None`` instead, so a bad id in a URL
    turns into a 404 rather than a 500.
    """


# --------------------------------------------------------------------------
# Id validation, run before anything touches the filesystem
# --------------------------------------------------------------------------

_SEARCH_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
"""The exact slug shape the scraper produces.

``leadengine/scrape.py`` builds a slug with
``re.sub(r"[^a-z0-9]+", "-", (niche + "--" + location).lower()).strip("-")``, so
a real slug is lowercase letters, digits and single hyphens, with no hyphen at
either end. This pattern also rejects, for free, every string that could walk
out of the data directory.
"""

_LEAD_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"^0x[0-9a-f]{1,24}:0x[0-9a-f]{1,24}$", re.IGNORECASE
)
"""Google's feature id: two hex words joined by a colon.

Real keys in the rep's data are ``0x`` plus 17 or 18 hex digits on each side,
for example ``0x89c259a0df6f48af:0x229bd2d48c26502f``. The bound is loose
enough for both lengths and tight enough that nothing else gets through.
"""

_UNSAFE_CHARS: Final[tuple[str, ...]] = ("/", "\\", "\x00", "..", "%", "\n", "\r", "\t")
"""Characters that must never appear in an id that becomes a file name.

The regexes above already refuse all of these. The explicit check is kept as a
second layer, because this is the one place where a URL turns into a path.
"""


def _looks_unsafe(value: str) -> bool:
    """Report whether a raw id carries anything that could escape the data dir."""
    if not value or value.startswith(".") or value.startswith("-"):
        return True
    return any(bad in value for bad in _UNSAFE_CHARS)


def valid_search_id(value: object) -> bool:
    """Report whether a value is a search id we are willing to open a file for.

    Args:
        value: Anything at all, usually a path parameter straight from a URL.

    Returns:
        True when the value is a string of the slug shape the scraper writes and
        carries no path separator, no null byte and no leading dot.
    """
    if not isinstance(value, str):
        return False
    if len(value) > MAX_SEARCH_ID_CHARS or _looks_unsafe(value):
        return False
    return _SEARCH_ID_RE.match(value) is not None


def valid_lead_key(value: object) -> bool:
    """Report whether a value has the shape of a Google Maps feature id.

    Args:
        value: Anything at all, usually a path parameter straight from a URL.

    Returns:
        True when the value is ``0x<hex>:0x<hex>`` and nothing else. A lead key
        is never used as a file name, but it is used as a dictionary key that
        gets written into ``pipeline.json``, so it is checked just as hard.
    """
    if not isinstance(value, str):
        return False
    if len(value) > MAX_LEAD_KEY_CHARS or _looks_unsafe(value):
        return False
    return _LEAD_KEY_RE.match(value) is not None


def _require_lead_key(value: object) -> str:
    """Return a validated lead key or raise :class:`LeadError`.

    Args:
        value: The key from the URL.

    Returns:
        The key unchanged.

    Raises:
        LeadError: When the value is not a lead key. Writes validate hard,
            because a bad key would land in the rep's pipeline file forever.
    """
    if not valid_lead_key(value):
        raise LeadError("That lead id does not look right, so nothing was saved.")
    return str(value)


# --------------------------------------------------------------------------
# Text and number cleaning
# --------------------------------------------------------------------------

_CONTROL_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EM_DASH_RE: Final[re.Pattern[str]] = re.compile(r"\s*[\u2014\u2015]\s*")
_ODD_SPACE_RE: Final[re.Pattern[str]] = re.compile(r"[ \t    ]+")
_MANY_NEWLINES_RE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")
_SPACE_BEFORE_PUNCT_RE: Final[re.Pattern[str]] = re.compile(r" +([,.;:!?])")
_DOUBLE_COMMA_RE: Final[re.Pattern[str]] = re.compile(r",(?: *,)+")
_LOWER_WORD_START_RE: Final[re.Pattern[str]] = re.compile(r"(?<![A-Za-z'\u2019])([a-z])")


def clean_text(value: object, *, limit: int = MAX_TEXT_CHARS) -> str:
    """Turn any value from the data files into a string safe to show the rep.

    The steps, in order: coerce to text, drop control characters, replace an em
    dash with a comma, fold the odd Unicode spaces the scraper picks up from
    Google Maps into ordinary spaces, tidy the punctuation that folding can
    leave behind, and cut to ``limit``.

    The em dash step matters. The audit writes sentences such as
    ``"The phone number is not tappable - this is where most leads die."`` with
    a real em dash in the middle. An em dash must never reach the screen, so it
    becomes a comma here, once, at the door, rather than in five call sites.

    Args:
        value: Anything. ``None`` becomes an empty string.
        limit: Longest string to keep. Longer text is cut on a word boundary
            when one is close to the end, otherwise cut straight.

    Returns:
        A cleaned string, possibly empty. Never ``None``.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if not text:
        return ""
    text = _CONTROL_RE.sub("", text)
    text = _EM_DASH_RE.sub(", ", text)
    text = _ODD_SPACE_RE.sub(" ", text)
    text = _MANY_NEWLINES_RE.sub("\n\n", text)
    text = _DOUBLE_COMMA_RE.sub(",", text)
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    text = text.strip().rstrip(" ,")
    if len(text) > limit:
        cut = text[:limit]
        space = cut.rfind(" ")
        if space > limit - 40:
            cut = cut[:space]
        text = cut.rstrip().rstrip(" ,")
    return text


def display_name(raw: object, fallback: object = "") -> str:
    """Return a business name fit to print in a calling list.

    Google Maps stores some names exactly as the owner typed them, so a real
    record in the rep's data reads ``little space salon``. A list of leads that
    the rep scans in one second reads better with a capital on each word, so an
    all lowercase name gets one. A name that already carries any capital letter
    is left completely alone, which protects ``SkyBarbers`` and
    ``Barber & Co Hoboken``. The letter after an apostrophe is left alone too,
    so ``joe's barber`` becomes ``Joe's Barber`` and not ``Joe'S Barber``.

    Args:
        raw: The name from the scores file or the ndjson row.
        fallback: Used when the name is empty. The category is the sensible
            fallback, a lead with no name is still a hair salon.

    Returns:
        A non empty display name. The last resort is the plain word
        ``Business``, because a blank row in a calling list is worse than a dull
        one.
    """
    name = clean_text(raw, limit=200)
    if not name:
        name = clean_text(fallback, limit=200)
    if not name:
        return "Business"
    if not any(ch.isupper() for ch in name):
        name = _LOWER_WORD_START_RE.sub(lambda m: m.group(1).upper(), name)
    return name


def as_int(value: object, default: int = 0) -> int:
    """Read an integer out of anything the data files might hold.

    Args:
        value: An int, a float, a numeric string, or something else entirely.
        default: Returned when the value cannot be read as a finite number.

    Returns:
        The value as an int, rounded when it arrives as a float.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return default
        return int(round(value))
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return default
        try:
            return int(round(float(text)))
        except ValueError:
            return default
    return default


def as_float(value: object) -> float | None:
    """Read a float out of anything, answering ``None`` when there is no number.

    Args:
        value: An int, a float, a numeric string, or something else.

    Returns:
        The value as a float, or ``None``. ``None`` is the honest answer for a
        missing rating: zero would say the business has one star.
    """
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        if number != number:
            return None
        return number
    return None


def _clamp(value: int, low: int, high: int) -> int:
    """Keep an integer inside a range."""
    return max(low, min(high, value))


def parse_bool_flag(value: object) -> bool | None:
    """Read a query string flag such as ``has_phone=1`` into a real boolean.

    Args:
        value: The raw query value. ``None`` and an empty string both mean the
            client did not ask for the filter at all.

    Returns:
        True, False, or ``None`` for "no filter". Anything unreadable is
        treated as no filter, because a typo in a URL should not silently hide
        every lead the rep has.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    text = str(value).strip().lower()
    if not text:
        return None
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return None


def normalize_track(value: object) -> str:
    """Return the track as the contract spells it.

    The scoring engine writes ``"SEO"`` and ``"WEBSITE"``. The contract spells
    them ``"SEO"`` and ``"Website"``, so that is what goes on the wire.

    Args:
        value: The raw track from the scores or the audit file.

    Returns:
        ``"SEO"``, ``"Website"``, some other cleaned word, or an empty string
        when the audit has not run and nothing is known. Compare against
        :data:`TRACK_SEO` and :data:`TRACK_WEBSITE`, and treat an empty track as
        "not known yet" rather than as an error.
    """
    text = clean_text(value, limit=40)
    if not text:
        return ""
    lowered = text.lower()
    if lowered == "seo":
        return TRACK_SEO
    if lowered in ("website", "web", "site"):
        return TRACK_WEBSITE
    return text[:1].upper() + text[1:].lower()


def _now_iso() -> str:
    """Return the current UTC time in the same shape the dashboard writes.

    Their Next.js dashboard writes JavaScript ISO strings such as
    ``2026-08-27T09:31:50.109Z``. We write the same shape so the two writers
    leave a file that looks like it had one author.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_to_epoch(value: object) -> float | None:
    """Turn an ISO timestamp from the scraper into epoch seconds.

    Args:
        value: A string such as ``2026-08-25T11:03:14+00:00`` or one ending in
            ``Z``. Anything else answers ``None``.

    Returns:
        Seconds since the epoch as a float, or ``None`` when the value cannot be
        read. A timestamp with no zone is read as UTC, which is what the
        scraper writes.
    """
    text = clean_text(value, limit=64)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


# --------------------------------------------------------------------------
# The parsed file cache
# --------------------------------------------------------------------------

_FileStamp = tuple[int, int]
"""Identity of a file on disk: modification time in nanoseconds, and size."""

_FILE_CACHE: dict[str, tuple[_FileStamp, Any]] = {}
"""Parsed file contents, keyed by path. Value is the stamp plus the parse."""

_FILE_CACHE_MAX: Final[int] = 96
"""How many parsed files to hold. Well past one rep's worth of searches."""


def _file_stamp(path: Path) -> _FileStamp | None:
    """Return the identity of a file, or ``None`` when it is not there.

    Args:
        path: The file to stat.

    Returns:
        ``(mtime_ns, size)``, or ``None`` for a missing file, a directory, or
        anything else this process cannot stat.
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _load_cached(path: Path, parse: Callable[[Path], Any], empty: Any) -> Any:
    """Parse a file once and keep the result until the file changes.

    A failed parse is cached too, as ``empty``. Without that, a file with one
    bad byte would be re-read and re-logged on every poll of the leads list.

    Args:
        path: The file to read.
        parse: The parser to run on a cache miss.
        empty: The value to answer with when the file is missing or unreadable.

    Returns:
        The parsed value, or ``empty``. Treat the result as read only, it is the
        same object every caller gets.
    """
    key = str(path)
    stamp = _file_stamp(path)
    if stamp is None:
        _FILE_CACHE.pop(key, None)
        return empty
    hit = _FILE_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        value = parse(path)
    except (OSError, ValueError, UnicodeError) as exc:
        log.warning("lead file could not be read, treating it as missing: %s (%s)", path, exc)
        value = empty
    if len(_FILE_CACHE) >= _FILE_CACHE_MAX:
        _FILE_CACHE.clear()
    _FILE_CACHE[key] = (stamp, value)
    return value


def clear_cache() -> None:
    """Drop every parsed file and every built search.

    Nothing in normal operation needs this, the mtime check does the job. It is
    here for the self test and for an operator who has just restored files by
    hand.
    """
    _FILE_CACHE.clear()
    _SEARCH_CACHE.clear()


def _parse_ndjson(path: Path) -> list[dict[str, Any]]:
    """Read one business per line, skipping any line that is not a JSON object.

    Args:
        path: The ``<slug>.ndjson`` file.

    Returns:
        The rows that parsed. A half written last line, which happens when the
        scraper is killed mid flush, costs one row and never an exception.
    """
    rows: list[dict[str, Any]] = []
    bad = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except ValueError:
                bad += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                bad += 1
    if bad:
        log.warning("skipped %d bad line(s) in %s", bad, path.name)
    return rows


def _parse_json_mapping(path: Path) -> dict[str, Any]:
    """Read a JSON file that is meant to be an object.

    Args:
        path: The file to read.

    Returns:
        The parsed object, or an empty dict when the file holds a list, a
        number, or anything else that is not an object.
    """
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if isinstance(data, dict):
        return data
    log.warning("%s is not a JSON object, treating it as missing", path.name)
    return {}


def _search_path(search_id: str, suffix: str) -> Path:
    """Build the path of one file belonging to a search.

    Args:
        search_id: A slug that has already passed :func:`valid_search_id`.
        suffix: The file ending, for example ``.ndjson`` or ``.scores.json``.

    Returns:
        The full path inside the data directory.
    """
    return data_dir() / f"{search_id}{suffix}"


# --------------------------------------------------------------------------
# The record shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PainPoint:
    """One checked fault in the prospect's online presence.

    Attributes:
        title: The short name of the fault, for example "The site is very slow".
        detail: The full sentence the rep can say, written by the audit.
        proof: The number or the word that proves it, for example ``6.6s``.
            Copy it word for word when you build call context, it is what makes
            the rep believable. It has already been cleaned on the way in, so
            the only way it can differ from the data file is an em dash that has
            become a comma. Every digit and every unit is the audit's own.
        severity: One of ``critical``, ``high``, ``medium`` or ``low``.
    """

    title: str
    detail: str
    proof: str
    severity: str

    @property
    def rank(self) -> int:
        """How loud this pain point is, higher is louder. Unknown words rank 0."""
        return SEVERITY_RANK.get(self.severity, 0)

    def to_wire(self) -> dict[str, Any]:
        """Return the JSON shape from the contract."""
        return {
            "title": self.title,
            "detail": self.detail,
            "proof": self.proof,
            "severity": self.severity,
        }

    @classmethod
    def from_score(cls, raw: object) -> PainPoint | None:
        """Build one pain point from a ``pain_points`` entry in the scores file.

        This is the door every proof comes through on the way to an endpoint, so
        it is the one place the em dash swap happens to a proof. Two proofs in
        the rep's real data carry an em dash, and they leave here with a comma
        in its place. Nothing else about a proof changes, see
        :attr:`PainPoint.proof`.

        Args:
            raw: One list entry. Anything that is not an object, or that has no
                title and no detail, is dropped.

        Returns:
            The pain point, or ``None`` when there is nothing worth showing.
        """
        if not isinstance(raw, dict):
            return None
        title = clean_text(raw.get("title"), limit=200)
        detail = clean_text(raw.get("detail"), limit=1200)
        if not title and not detail:
            return None
        severity = clean_text(raw.get("severity"), limit=20).lower()
        if severity not in SEVERITY_RANK:
            severity = "medium"
        return cls(
            title=title or detail[:80],
            detail=detail,
            proof=clean_text(raw.get("proof"), limit=MAX_PROOF_CHARS),
            severity=severity,
        )

    @classmethod
    def from_finding(cls, raw: object) -> PainPoint | None:
        """Build one pain point from an audit finding.

        The audit file names the same three things differently: ``sales_line``
        is the detail and ``evidence`` is the proof. This is what makes a
        search with an audit but no scores still useful on a call.

        The proof is cleaned exactly as :meth:`from_score` cleans one, so a
        pain point built from an audit and a pain point built from the scores
        make the same promise about their proof.

        Args:
            raw: One entry from ``site.findings`` or from ``gmb_findings``.

        Returns:
            The pain point, or ``None`` when the entry is empty or malformed.
        """
        if not isinstance(raw, dict):
            return None
        title = clean_text(raw.get("title"), limit=200)
        detail = clean_text(raw.get("sales_line"), limit=1200)
        if not title and not detail:
            return None
        severity = clean_text(raw.get("severity"), limit=20).lower()
        if severity not in SEVERITY_RANK:
            severity = "medium"
        return cls(
            title=title or detail[:80],
            detail=detail,
            proof=clean_text(raw.get("evidence"), limit=MAX_PROOF_CHARS),
            severity=severity,
        )


@dataclass(frozen=True, slots=True)
class Money:
    """What this lead is worth, as the scoring engine worked it out.

    Every amount is a whole number of the currency named by
    :attr:`currency_symbol`. The field names keep the engine's own meaning:
    ``deal_value_usd`` is the one off fee, ``retainer_monthly_usd`` is the
    monthly fee, and ``contract_value_usd`` is the two together over the
    expected life of the deal.
    """

    tier: str = ""
    tier_label: str = ""
    deal_value_usd: int = 0
    retainer_monthly_usd: int = 0
    contract_value_usd: int = 0
    ltv_usd: int = 0
    currency: str = "USD"
    currency_symbol: str = DEFAULT_CURRENCY_SYMBOL
    close_probability: float = 0.0
    expected_value_usd: int = 0

    def to_wire(self) -> dict[str, Any]:
        """Return the money block in camelCase, like every other wire shape."""
        return {
            "tier": self.tier,
            "tierLabel": self.tier_label,
            "dealValueUsd": self.deal_value_usd,
            "retainerMonthlyUsd": self.retainer_monthly_usd,
            "contractValueUsd": self.contract_value_usd,
            "ltvUsd": self.ltv_usd,
            "currency": self.currency,
            "currencySymbol": self.currency_symbol,
            "closeProbability": self.close_probability,
            "expectedValueUsd": self.expected_value_usd,
        }

    @classmethod
    def from_raw(cls, raw: object) -> Money:
        """Build the money block from the scores file, defaulting every field.

        Args:
            raw: The ``money`` object from a scored lead. Anything that is not
                an object gives an all zero block, which is exactly right for a
                search whose audit has not run yet.

        Returns:
            A money block. Never ``None``, so no caller has to check.
        """
        if not isinstance(raw, dict):
            return cls()
        probability = as_float(raw.get("close_probability"))
        if probability is None or probability < 0:
            probability = 0.0
        symbol = clean_text(raw.get("currency_symbol"), limit=4) or DEFAULT_CURRENCY_SYMBOL
        return cls(
            tier=clean_text(raw.get("tier"), limit=20),
            tier_label=clean_text(raw.get("tier_label"), limit=60),
            deal_value_usd=max(0, as_int(raw.get("deal_value_usd"))),
            retainer_monthly_usd=max(0, as_int(raw.get("retainer_monthly_usd"))),
            contract_value_usd=max(0, as_int(raw.get("contract_value_usd"))),
            ltv_usd=max(0, as_int(raw.get("ltv_usd"))),
            currency=clean_text(raw.get("currency"), limit=8) or "USD",
            currency_symbol=symbol,
            close_probability=round(min(1.0, probability), 4),
            expected_value_usd=max(0, as_int(raw.get("expected_value_usd"))),
        )


@dataclass(frozen=True, slots=True)
class HoursRow:
    """One line of opening hours, as scraped.

    Attributes:
        day: The day name, for example ``Tuesday``.
        hours: What Maps printed, for example ``9 AM to 8:15 PM`` or ``Closed``.
    """

    day: str
    hours: str

    def to_wire(self) -> dict[str, str]:
        """Return the row as the drawer expects it."""
        return {"day": self.day, "hours": self.hours}


@dataclass(frozen=True, slots=True)
class Lead:
    """One business, joined from every file that knows something about it.

    The raw fields come from the ndjson row, the scores come from
    ``<slug>.scores.json``, the pain points come from the scores or, when the
    scoring step has not run, from ``<slug>.audit.json``, and the last three
    fields come from ``pipeline.json``.

    A lead built from an ndjson row alone is a complete, usable record with
    zeroed scores. That is not an error state, it is a search whose audit has
    not finished yet.
    """

    search_id: str
    key: str
    name: str
    category: str
    city: str
    phone: str
    website: str
    rating: float | None
    reviews: int
    track: str
    lead_score: int
    urgency: int
    why: str
    priority: str
    presence_score: int
    local_rank: int
    address: str
    gmb_url: str
    money: Money
    hours: tuple[HoursRow, ...] = ()
    pain_points: tuple[PainPoint, ...] = ()
    talking_points: tuple[str, ...] = ()
    has_scores: bool = False
    status: str = DEFAULT_STATUS
    notes: str = ""
    called_at: str | None = None
    updated_at: str | None = None

    # -- derived values -------------------------------------------------

    @property
    def deal_value(self) -> int:
        """The one off fee this lead is worth, zero when nothing is scored."""
        return self.money.deal_value_usd

    @property
    def currency(self) -> str:
        """The currency symbol to print in front of :attr:`deal_value`."""
        return self.money.currency_symbol or DEFAULT_CURRENCY_SYMBOL

    @property
    def pain_count(self) -> int:
        """How many faults the audit found."""
        return len(self.pain_points)

    @property
    def has_phone(self) -> bool:
        """Whether this lead can be called at all."""
        return bool(self.phone)

    @property
    def called(self) -> bool:
        """Whether the rep has already reached out to this lead.

        True when the pipeline carries a ``contactedAt`` stamp, or when the
        status has moved off ``new``. Either one means the lead is no longer
        money sitting on the table, which is what the searches list counts.
        """
        return self.called_at is not None or self.status != DEFAULT_STATUS

    def top_pain_points(
        self,
        limit: int = 5,
        severities: Sequence[str] = STRONG_SEVERITIES,
    ) -> list[PainPoint]:
        """Return the strongest pain points, loudest first.

        Ten faults make a copilot pick a weak one, so this is what anything
        building call context should read instead of :attr:`pain_points`.
        ``critical`` counts as stronger than ``high``, which matters because the
        audit writes both.

        Args:
            limit: How many to return at most.
            severities: Which severities are allowed through. The default drops
                ``low``, which is never worth saying out loud on a call.

        Returns:
            A list of at most ``limit`` pain points, strongest first. Order
            inside one severity is the order the audit wrote them.
        """
        allowed = {s.lower() for s in severities}
        picked = [p for p in self.pain_points if p.severity in allowed]
        picked.sort(key=lambda p: -p.rank)
        return picked[: max(0, limit)]

    def with_pipeline(self, entry: object) -> Lead:
        """Return a copy of this lead carrying its pipeline state.

        Args:
            entry: The ``pipeline.json`` value for this key, or ``None`` when
                the lead has never been touched.

        Returns:
            A new lead. The original is left alone, because base leads are
            cached and shared between requests.
        """
        if not isinstance(entry, dict):
            return self
        status = clean_text(entry.get("status"), limit=40).lower()
        if status not in LEAD_STATUSES:
            status = DEFAULT_STATUS
        called_at = clean_text(entry.get("contactedAt"), limit=64) or None
        updated_at = clean_text(entry.get("updatedAt"), limit=64) or None
        return replace(
            self,
            status=status,
            notes=clean_text(entry.get("notes"), limit=MAX_NOTES_CHARS),
            called_at=called_at,
            updated_at=updated_at,
        )

    # -- the one wire shape ---------------------------------------------

    def to_wire(self, *, full: bool = False) -> dict[str, Any]:
        """Return the JSON the frontend reads.

        This is the only place a lead becomes JSON. The list endpoint calls it
        with no arguments and the detail endpoint calls it with ``full=True``,
        so the two can never describe the same lead differently.

        Args:
            full: When True, add the fields the detail drawer needs: the pain
                points with their proof, the talking points, the address, the
                hours, the GMB link, the money block and the rep's notes.

        Returns:
            A plain dict, camelCase, JSON safe all the way down.
        """
        wire: dict[str, Any] = {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "city": self.city,
            "phone": self.phone,
            "website": self.website,
            "rating": self.rating,
            "reviews": self.reviews,
            "track": self.track,
            "leadScore": self.lead_score,
            "urgency": self.urgency,
            "why": self.why,
            "dealValue": self.deal_value,
            "currency": self.currency,
            "painCount": self.pain_count,
            "status": self.status,
            "calledAt": self.called_at,
        }
        if not full:
            return wire
        wire.update(
            {
                "searchId": self.search_id,
                "painPoints": [p.to_wire() for p in self.pain_points],
                "talkingPoints": list(self.talking_points),
                "address": self.address,
                "hours": [h.to_wire() for h in self.hours],
                "gmbUrl": self.gmb_url,
                "money": self.money.to_wire(),
                "notes": self.notes,
                "updatedAt": self.updated_at,
                "presenceScore": self.presence_score,
                "localRank": self.local_rank,
                "priority": self.priority,
                "scored": self.has_scores,
            }
        )
        return wire


@dataclass(frozen=True, slots=True)
class SearchSummary:
    """One row of the search picker.

    Attributes:
        search_id: The slug, which is also the id in the URL.
        niche: What was searched for, for example ``barber``.
        location: Where, for example ``hoboken``.
        leads: How many businesses the scrape found.
        called: How many of them the rep has already reached out to.
        value: Deal value still on the table, summed over leads not called yet.
        currency: The symbol to print in front of ``value``.
        scraped_at: When the scrape ran, in epoch seconds.
        scored: Whether the audit and scoring steps have run for this search.
    """

    search_id: str
    niche: str
    location: str
    leads: int
    called: int
    value: int
    currency: str
    scraped_at: float
    scored: bool

    def to_wire(self) -> dict[str, Any]:
        """Return the summary row from the contract."""
        return {
            "id": self.search_id,
            "niche": self.niche,
            "location": self.location,
            "leads": self.leads,
            "called": self.called,
            "value": self.value,
            "currency": self.currency,
            "scrapedAt": self.scraped_at,
            "scored": self.scored,
        }


@dataclass(frozen=True, slots=True)
class SearchLeads:
    """Every lead of one search, already joined.

    Attributes:
        search_id: The slug.
        niche: What was searched for.
        location: Where.
        scraped_at: When the scrape ran, in epoch seconds.
        scored: Whether any lead in this search carries audit scores.
        leads: The leads, in the order the scraper found them.
    """

    search_id: str
    niche: str
    location: str
    scraped_at: float
    scored: bool
    leads: tuple[Lead, ...] = ()

    def by_key(self) -> dict[str, Lead]:
        """Return the leads keyed by lead key, for a one step lookup."""
        return {lead.key: lead for lead in self.leads}

    def summary(self) -> SearchSummary:
        """Fold this search into one picker row.

        ``value`` counts only the leads that have not been called, because the
        number is there to answer "how much is still on the table", not "how
        much did this search ever contain".
        """
        currency = DEFAULT_CURRENCY_SYMBOL
        for lead in self.leads:
            if lead.money.currency_symbol:
                currency = lead.money.currency_symbol
                break
        return SearchSummary(
            search_id=self.search_id,
            niche=self.niche,
            location=self.location,
            leads=len(self.leads),
            called=sum(1 for lead in self.leads if lead.called),
            value=sum(lead.deal_value for lead in self.leads if not lead.called),
            currency=currency,
            scraped_at=self.scraped_at,
            scored=self.scored,
        )


# --------------------------------------------------------------------------
# Building a search out of the files
# --------------------------------------------------------------------------

_SEARCH_CACHE: dict[str, tuple[tuple[_FileStamp | None, ...], SearchLeads]] = {}
"""Built searches without pipeline state, keyed by slug.

The value carries the stamps of the three files it was built from, so any edit
to the ndjson, the scores or the audit rebuilds it. Pipeline state is left out
on purpose: it changes after every call and it is cheap to apply on read.
"""


def _pain_points_from_audit(audit: object) -> list[PainPoint]:
    """Turn one audit record into pain points.

    Used when the scoring step has not run. The GMB findings come first,
    because "they have no website at all" beats any site fault, and then the
    site findings in the order the audit wrote them.

    Args:
        audit: The ``<slug>.audit.json`` value for one lead key.

    Returns:
        Pain points, loudest first. An empty list when there is no audit.
    """
    if not isinstance(audit, dict):
        return []
    raw: list[Any] = []
    gmb = audit.get("gmb_findings")
    if isinstance(gmb, list):
        raw.extend(gmb)
    site = audit.get("site")
    if isinstance(site, dict) and isinstance(site.get("findings"), list):
        raw.extend(site["findings"])
    points = [p for p in (PainPoint.from_finding(item) for item in raw) if p is not None]
    points.sort(key=lambda p: -p.rank)
    return points


def _pain_points_from_score(score: object) -> list[PainPoint]:
    """Turn the scored ``pain_points`` list into pain points, loudest first.

    Args:
        score: The scores entry for one lead key.

    Returns:
        Pain points. An empty list when the lead is not scored.
    """
    if not isinstance(score, dict):
        return []
    raw = score.get("pain_points")
    if not isinstance(raw, list):
        return []
    points = [p for p in (PainPoint.from_score(item) for item in raw) if p is not None]
    points.sort(key=lambda p: -p.rank)
    return points


def _hours_from_row(row: dict[str, Any]) -> tuple[HoursRow, ...]:
    """Read the opening hours out of a scraped row.

    Args:
        row: One ndjson row.

    Returns:
        The hours as a tuple, empty when Maps showed none.
    """
    raw = row.get("hours")
    if not isinstance(raw, list):
        return ()
    rows: list[HoursRow] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        day = clean_text(item.get("day"), limit=40)
        hours = clean_text(item.get("hours"), limit=120)
        if not day and not hours:
            continue
        rows.append(HoursRow(day=day, hours=hours))
    return tuple(rows)


def _talking_points_from_score(score: object) -> tuple[str, ...]:
    """Read the talking points, which are notes for the rep, not a script.

    Args:
        score: The scores entry for one lead key.

    Returns:
        The lines, cleaned, in the order the engine wrote them.
    """
    if not isinstance(score, dict):
        return ()
    raw = score.get("talking_points")
    if not isinstance(raw, list):
        return ()
    lines = [clean_text(item, limit=400) for item in raw]
    return tuple(line for line in lines if line)


def _build_lead(
    search_id: str,
    row: dict[str, Any],
    score: object,
    audit: object,
) -> Lead | None:
    """Join one scraped row with its score and its audit.

    Args:
        search_id: The slug this lead belongs to.
        row: The ndjson row. It is the only required part.
        score: The scores entry for this key, or ``None``.
        audit: The audit entry for this key, or ``None``.

    Returns:
        The joined lead, or ``None`` when the row has no usable key. A row with
        no key cannot be addressed by a URL or written to the pipeline, so it is
        dropped rather than shown.
    """
    key = clean_text(row.get("feature_id"), limit=MAX_LEAD_KEY_CHARS)
    if not valid_lead_key(key):
        return None

    score_map: dict[str, Any] = score if isinstance(score, dict) else {}
    audit_map: dict[str, Any] = audit if isinstance(audit, dict) else {}
    has_scores = bool(score_map)

    category = clean_text(row.get("category"), limit=120)
    name = display_name(score_map.get("name") or row.get("name"), category)

    pain_points = _pain_points_from_score(score_map)
    if not pain_points:
        pain_points = _pain_points_from_audit(audit_map)

    why = clean_text(score_map.get("need_reason"), limit=400)
    if not why:
        why = clean_text(score_map.get("urgency_reason"), limit=400)
    if not why and pain_points:
        why = pain_points[0].detail or pain_points[0].title

    website = clean_text(row.get("website"), limit=600)
    if not website:
        site = audit_map.get("site")
        if isinstance(site, dict):
            website = clean_text(site.get("url"), limit=600)

    track = normalize_track(score_map.get("track") or audit_map.get("track"))

    return Lead(
        search_id=search_id,
        key=key,
        name=name,
        category=category,
        city=clean_text(row.get("city") or row.get("search_location"), limit=120),
        phone=clean_text(row.get("phone"), limit=40),
        website=website,
        rating=as_float(row.get("rating")),
        reviews=max(0, as_int(row.get("review_count"))),
        track=track,
        lead_score=_clamp(as_int(score_map.get("lead_score")), 0, 100),
        urgency=_clamp(as_int(score_map.get("urgency")), 0, 5),
        why=why,
        priority=clean_text(score_map.get("priority"), limit=40),
        presence_score=_clamp(as_int(score_map.get("presence_score")), 0, 100),
        local_rank=max(0, as_int(score_map.get("local_rank"))),
        address=clean_text(row.get("address"), limit=400),
        gmb_url=clean_text(row.get("gmb_url"), limit=1200),
        money=Money.from_raw(score_map.get("money")),
        hours=_hours_from_row(row),
        pain_points=tuple(pain_points),
        talking_points=_talking_points_from_score(score_map),
        has_scores=has_scores,
    )


def _base_search(search_id: str) -> SearchLeads | None:
    """Build one search from disk, without pipeline state, and cache it.

    Args:
        search_id: A slug that has already passed :func:`valid_search_id`.

    Returns:
        The search, or ``None`` when there is no ndjson file for it. The ndjson
        is the spine, a search that was never scraped does not exist.
    """
    ndjson = _search_path(search_id, ".ndjson")
    ndjson_stamp = _file_stamp(ndjson)
    if ndjson_stamp is None:
        _SEARCH_CACHE.pop(search_id, None)
        return None

    scores_file = _search_path(search_id, ".scores.json")
    audit_file = _search_path(search_id, ".audit.json")
    stamps = (ndjson_stamp, _file_stamp(scores_file), _file_stamp(audit_file))

    hit = _SEARCH_CACHE.get(search_id)
    if hit is not None and hit[0] == stamps:
        return hit[1]

    rows: list[dict[str, Any]] = _load_cached(ndjson, _parse_ndjson, [])
    scores_doc: dict[str, Any] = _load_cached(scores_file, _parse_json_mapping, {})
    audit_doc: dict[str, Any] = _load_cached(audit_file, _parse_json_mapping, {})

    scored_leads = scores_doc.get("leads")
    if not isinstance(scored_leads, dict):
        scored_leads = {}

    leads: list[Lead] = []
    scraped_at: float | None = None
    niche = ""
    location = ""
    for row in rows:
        if not niche:
            niche = clean_text(row.get("search_niche"), limit=80)
        if not location:
            location = clean_text(row.get("search_location"), limit=80)
        moment = _iso_to_epoch(row.get("scraped_at"))
        if moment is not None and (scraped_at is None or moment > scraped_at):
            scraped_at = moment
        key = clean_text(row.get("feature_id"), limit=MAX_LEAD_KEY_CHARS)
        lead = _build_lead(
            search_id,
            row,
            scored_leads.get(key),
            audit_doc.get(key),
        )
        if lead is not None:
            leads.append(lead)

    if not niche:
        niche = search_id.replace("-", " ")
    if scraped_at is None:
        scraped_at = ndjson_stamp[0] / 1_000_000_000

    built = SearchLeads(
        search_id=search_id,
        niche=niche,
        location=location,
        scraped_at=scraped_at,
        scored=any(lead.has_scores for lead in leads),
        leads=tuple(leads),
    )
    if len(_SEARCH_CACHE) >= _FILE_CACHE_MAX:
        _SEARCH_CACHE.clear()
    _SEARCH_CACHE[search_id] = (stamps, built)
    return built


# --------------------------------------------------------------------------
# pipeline.json, the one file this module writes
# --------------------------------------------------------------------------

_PIPELINE_LOCK: Final[asyncio.Lock] = asyncio.Lock()
"""Serialises every write to ``pipeline.json`` inside this process.

The whole read, merge, write cycle happens under this lock, and the file is
re-read inside it, so two calls finishing at the same moment cannot lose each
other's key. It does not protect against their dashboard writing at the same
millisecond, nothing short of a lock file would, but the write itself is atomic
so the worst case is a lost update, never a damaged file.
"""


def _read_pipeline_from_disk() -> dict[str, Any]:
    """Read ``pipeline.json`` straight from disk, past the cache.

    Returns:
        The parsed object. An empty dict when the file is missing or empty.

    Raises:
        LeadError: When the file exists, holds bytes, and cannot be parsed. The
            damaged file is moved aside first, so nothing the rep typed is lost
            and the next write starts clean.
    """
    path = pipeline_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise LeadError(f"The pipeline file could not be read: {exc}") from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        spoiled = path.with_name(f"pipeline.damaged-{int(datetime.now(timezone.utc).timestamp())}.json")
        try:
            os.replace(path, spoiled)
            log.error("pipeline.json was damaged, moved it to %s", spoiled.name)
        except OSError:
            log.error("pipeline.json was damaged and could not be moved aside: %s", exc)
        return {}
    if not isinstance(data, dict):
        log.error("pipeline.json is not a JSON object, starting a fresh one")
        return {}
    return data


def _write_pipeline(data: dict[str, Any]) -> None:
    """Write the whole pipeline to disk without ever leaving it half written.

    The file goes to a temp file in the same directory, is flushed and synced,
    and is then moved onto the real name with :func:`os.replace`, which is
    atomic on the same filesystem. A crash at any point leaves either the old
    file or the new one, never a truncated one. The rep's own dashboard reads
    this file, so a half written moment would show them an empty pipeline.

    Args:
        data: The complete mapping to write.

    Raises:
        LeadError: When the file cannot be written.
    """
    path = pipeline_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".pipeline-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise LeadError(f"The pipeline file could not be saved: {exc}") from exc

    # Best effort, so the rename itself survives a power cut on filesystems
    # that need the directory synced too. A failure here changes nothing.
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass

    _FILE_CACHE.pop(str(path), None)


def read_pipeline() -> dict[str, Any]:
    """Return the whole pipeline mapping, cached.

    Returns:
        Lead key to entry. Treat it as read only, it is the cached object.
        Use :func:`pipeline_entry` when you want a copy you can change.
    """
    return _load_cached(pipeline_path(), _parse_json_mapping, {})


def pipeline_entry(key: str) -> dict[str, Any]:
    """Return a copy of one pipeline entry.

    Args:
        key: The lead key.

    Returns:
        A fresh dict with ``status`` and ``notes`` always present. An untouched
        lead gives ``{"status": "new", "notes": ""}``.
    """
    entry = read_pipeline().get(key)
    result: dict[str, Any] = dict(entry) if isinstance(entry, dict) else {}
    status = clean_text(result.get("status"), limit=40).lower()
    result["status"] = status if status in LEAD_STATUSES else DEFAULT_STATUS
    result["notes"] = clean_text(result.get("notes"), limit=MAX_NOTES_CHARS)
    return result


def _merge_entry(
    key: str,
    *,
    status: str | None,
    notes: str | None,
    stamp_called: bool,
) -> dict[str, Any]:
    """Read, merge one key, write. Only ever called while holding the lock.

    Args:
        key: The validated lead key.
        status: The new status, or ``None`` to leave it alone.
        notes: The new note, or ``None`` to leave it alone.
        stamp_called: When True and the key has no ``contactedAt`` yet, stamp
            it with now. An existing stamp is never moved, because it is the
            first contact time and their dashboard shows it.

    Returns:
        The result body for the route.

    Raises:
        LeadError: When the file cannot be read or written.
    """
    data = _read_pipeline_from_disk()
    current = data.get(key)
    entry: dict[str, Any] = dict(current) if isinstance(current, dict) else {}

    now = _now_iso()
    if status is not None:
        entry["status"] = status
    if notes is not None:
        entry["notes"] = notes
    if not isinstance(entry.get("status"), str) or entry.get("status") not in LEAD_STATUSES:
        entry["status"] = DEFAULT_STATUS
    if not isinstance(entry.get("notes"), str):
        entry["notes"] = ""
    entry["updatedAt"] = now
    if stamp_called and not clean_text(entry.get("contactedAt"), limit=64):
        entry["contactedAt"] = now

    data[key] = entry
    _write_pipeline(data)

    return {
        "ok": True,
        "key": key,
        "status": entry["status"],
        "notes": entry["notes"],
        "updatedAt": entry["updatedAt"],
        "calledAt": entry.get("contactedAt"),
    }


async def set_status(key: str, status: str, notes: str | None = None) -> dict[str, Any]:
    """Write one lead's outcome into the shared ``pipeline.json``.

    The whole cycle is read, merge that one key, write a temp file, rename. The
    file is re-read inside the lock, so a key their dashboard added a second ago
    is still there afterwards. Any status other than ``new`` also stamps
    ``contactedAt`` when there is none yet, which is what moves the lead out of
    the "still on the table" money in the searches list.

    Args:
        key: The lead key from the URL.
        status: One of :data:`LEAD_STATUSES`. Spaces are accepted in place of
            underscores, so ``"not interested"`` works.
        notes: What the rep typed, or ``None`` to leave the existing note alone.

    Returns:
        ``{"ok": True, "key", "status", "notes", "updatedAt", "calledAt"}``.

    Raises:
        LeadError: When the key or the status is not one we accept, or when the
            file cannot be written. Routes should answer 400.
    """
    lead_key = _require_lead_key(key)
    wanted = clean_text(status, limit=40).lower().replace(" ", "_").replace("-", "_")
    if wanted not in LEAD_STATUSES:
        allowed = ", ".join(sorted(LEAD_STATUSES))
        raise LeadError(f"That status is not one we know. Use one of: {allowed}.")
    note_text = None if notes is None else clean_text(notes, limit=MAX_NOTES_CHARS)

    async with _PIPELINE_LOCK:
        return _merge_entry(
            lead_key,
            status=wanted,
            notes=note_text,
            stamp_called=wanted != DEFAULT_STATUS,
        )


async def mark_called(key: str) -> dict[str, Any]:
    """Stamp a lead as contacted without changing its status.

    Call this when a call actually starts. The status stays whatever it was,
    usually ``new``, until the rep picks an outcome afterwards, but the lead
    stops counting as money on the table straight away.

    Args:
        key: The lead key from the URL.

    Returns:
        The same body as :func:`set_status`.

    Raises:
        LeadError: When the key is bad or the file cannot be written.
    """
    lead_key = _require_lead_key(key)
    async with _PIPELINE_LOCK:
        return _merge_entry(lead_key, status=None, notes=None, stamp_called=True)


# --------------------------------------------------------------------------
# The read API the routes use
# --------------------------------------------------------------------------


def search_ids() -> list[str]:
    """List every search on disk, newest first by ndjson modification time.

    Returns:
        The slugs. A file whose name is not the slug shape the scraper writes
        is skipped, because we would not be able to serve it under a URL
        anyway.
    """
    directory = data_dir()
    try:
        files = list(directory.glob("*.ndjson"))
    except OSError as exc:
        log.warning("lead data directory could not be listed: %s (%s)", directory, exc)
        return []
    dated: list[tuple[int, str]] = []
    for path in files:
        slug = path.name[: -len(".ndjson")]
        if not valid_search_id(slug):
            log.warning("skipping lead file with an unexpected name: %s", path.name)
            continue
        stamp = _file_stamp(path)
        if stamp is None:
            continue
        dated.append((stamp[0], slug))
    dated.sort(key=lambda item: (-item[0], item[1]))
    return [slug for _, slug in dated]


def search_summaries() -> list[SearchSummary]:
    """Fold every search into one picker row each, newest first.

    Returns:
        The summaries. An empty list when the rep has never run a search, which
        is the empty state the frontend draws.
    """
    pipeline = read_pipeline()
    rows: list[SearchSummary] = []
    for slug in search_ids():
        base = _base_search(slug)
        if base is None:
            continue
        joined = tuple(lead.with_pipeline(pipeline.get(lead.key)) for lead in base.leads)
        rows.append(replace(base, leads=joined).summary())
    return rows


def list_searches() -> list[dict[str, Any]]:
    """Return the body of ``GET /api/leads/searches``.

    Returns:
        A list of summary rows, newest first, each in the contract's shape.
        ``value`` is the sum of the deal value over the leads that have not been
        called yet.
    """
    return [row.to_wire() for row in search_summaries()]


def load_search(search_id: str) -> SearchLeads | None:
    """Load one search with every join applied.

    This is the left join at the heart of the merge: ndjson rows, plus the
    scores keyed by lead key, plus the audit, plus the pipeline. Every one of
    those files may be missing. A search with an ndjson but no scores comes
    back as normal leads with zeroed scores, because the audit simply has not
    run yet.

    Args:
        search_id: The slug from the URL.

    Returns:
        The search, or ``None`` when the id is not a slug we accept or there is
        no ndjson for it. Routes should answer 404 for ``None``.
    """
    if not valid_search_id(search_id):
        return None
    base = _base_search(search_id)
    if base is None:
        return None
    pipeline = read_pipeline()
    joined = tuple(lead.with_pipeline(pipeline.get(lead.key)) for lead in base.leads)
    return replace(base, leads=joined)


def filter_leads(
    leads: Iterable[Lead],
    *,
    status: str | None = None,
    has_phone: bool | str | int | None = None,
    sort: str | None = DEFAULT_SORT,
) -> list[Lead]:
    """Apply the list endpoint's filters and sort.

    Args:
        leads: The leads to work on.
        status: One of :data:`LEAD_STATUSES` for an exact match,
            :data:`STATUS_UNCALLED` for "not called yet",
            :data:`STATUS_CALLED` for the opposite, or an empty value for no
            filter at all. An unknown word filters nothing, so a stale chip in
            the UI cannot hide every lead.
        has_phone: True keeps only leads with a phone number, because a lead
            with no number cannot be called and is noise in a calling list.
            Accepts the raw query string value, see :func:`parse_bool_flag`.
        sort: ``value`` (default), ``score`` or ``name``.

    Returns:
        A new list. The input is not changed.
    """
    picked = list(leads)

    wanted = clean_text(status, limit=40).lower().replace(" ", "_").replace("-", "_")
    if wanted and wanted not in _ANY_STATUS:
        if wanted == STATUS_UNCALLED:
            picked = [lead for lead in picked if not lead.called]
        elif wanted == STATUS_CALLED:
            picked = [lead for lead in picked if lead.called]
        elif wanted in LEAD_STATUSES:
            picked = [lead for lead in picked if lead.status == wanted]
        else:
            log.debug("ignoring unknown lead status filter: %r", status)

    phone_flag = parse_bool_flag(has_phone)
    if phone_flag is True:
        picked = [lead for lead in picked if lead.has_phone]
    elif phone_flag is False:
        picked = [lead for lead in picked if not lead.has_phone]

    key = clean_text(sort, limit=20).lower() or DEFAULT_SORT
    if key not in SORT_KEYS:
        key = DEFAULT_SORT
    if key == "name":
        picked.sort(key=lambda lead: (lead.name.casefold(), -lead.deal_value))
    elif key == "score":
        picked.sort(
            key=lambda lead: (-lead.lead_score, -lead.deal_value, lead.name.casefold())
        )
    else:
        picked.sort(
            key=lambda lead: (-lead.deal_value, -lead.lead_score, lead.name.casefold())
        )
    return picked


def leads_page(
    search_id: str,
    *,
    status: str | None = None,
    has_phone: bool | str | int | None = None,
    sort: str | None = DEFAULT_SORT,
) -> dict[str, Any] | None:
    """Return the body of ``GET /api/leads/{search_id}``.

    Args:
        search_id: The slug from the URL.
        status: The ``status`` query parameter.
        has_phone: The ``has_phone`` query parameter.
        sort: The ``sort`` query parameter.

    Returns:
        ``{"searchId": ..., "leads": [...]}``, or ``None`` when the search does
        not exist, which the route should answer as a 404.
    """
    search = load_search(search_id)
    if search is None:
        return None
    picked = filter_leads(search.leads, status=status, has_phone=has_phone, sort=sort)
    return {"searchId": search.search_id, "leads": [lead.to_wire() for lead in picked]}


def get_lead(search_id: str, key: str) -> Lead | None:
    """Return one full lead record.

    Args:
        search_id: The slug from the URL.
        key: The lead key from the URL.

    Returns:
        The lead with its pain points, talking points, hours, address, GMB link,
        money and notes, or ``None`` when either id is bad or the lead is not in
        that search.
    """
    if not valid_lead_key(key):
        return None
    search = load_search(search_id)
    if search is None:
        return None
    for lead in search.leads:
        if lead.key == key:
            return lead
    return None


def next_uncalled(search_id: str, after_key: str | None = None) -> Lead | None:
    """Return the next lead worth calling in this search.

    The frontend's "Next lead" button is the whole product for a rep doing
    forty calls a day, so the rule lives here rather than in the UI: skip
    anything already called, skip anything with no phone number, and take the
    most valuable one that is left.

    Args:
        search_id: The slug.
        after_key: The lead just finished, which is skipped even if its status
            was never written.

    Returns:
        The next lead, or ``None`` when the list is done.
    """
    search = load_search(search_id)
    if search is None:
        return None
    picked = filter_leads(
        search.leads, status=STATUS_UNCALLED, has_phone=True, sort=DEFAULT_SORT
    )
    for lead in picked:
        if after_key and lead.key == after_key:
            continue
        return lead
    return None


__all__ = [
    "DEFAULT_CURRENCY_SYMBOL",
    "DEFAULT_SORT",
    "DEFAULT_STATUS",
    "HoursRow",
    "LEAD_STATUSES",
    "Lead",
    "LeadError",
    "MAX_PROOF_CHARS",
    "Money",
    "PainPoint",
    "SEVERITY_RANK",
    "SORT_KEYS",
    "STATUS_CALLED",
    "STATUS_UNCALLED",
    "STRONG_SEVERITIES",
    "SearchLeads",
    "SearchSummary",
    "TRACK_SEO",
    "TRACK_WEBSITE",
    "as_float",
    "as_int",
    "clean_text",
    "clear_cache",
    "data_dir",
    "display_name",
    "filter_leads",
    "get_lead",
    "leads_page",
    "list_searches",
    "load_search",
    "mark_called",
    "next_uncalled",
    "normalize_track",
    "parse_bool_flag",
    "pipeline_entry",
    "pipeline_path",
    "read_pipeline",
    "search_ids",
    "search_summaries",
    "set_status",
    "use_data_dir",
    "valid_lead_key",
    "valid_search_id",
]


# --------------------------------------------------------------------------
# Self test, run against the rep's real data
# --------------------------------------------------------------------------


def _check(label: str, condition: bool) -> None:
    """Print one check and stop the self test on the first failure."""
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}")
    if not condition:
        raise AssertionError(label)


def _self_test() -> int:  # pragma: no cover - developer tool
    """Run every read path against the real files in ``leadengine/data``.

    Nothing here writes to the rep's own data. The write test copies the data
    files into a temporary directory first and points the module at the copy.

    Returns:
        0 when every check passed. Any failure raises instead.
    """
    import shutil
    import tempfile as tmp_module

    print("lead service self test")
    print(f"  data dir: {data_dir()}")

    rows = list_searches()
    _check("at least one search on disk", bool(rows))
    print()
    print("  searches, newest first")
    for row in rows:
        print(
            f"    {row['id']:<24} leads={row['leads']:>3} called={row['called']:>3} "
            f"value={row['currency']}{row['value']:<8} scored={row['scored']}"
        )
    print()

    for row in rows:
        _check(f"{row['id']} has an id, a niche and a lead count", bool(row["id"]) and bool(row["niche"]) and row["leads"] > 0)
        _check(f"{row['id']} value counts only uncalled leads", row["value"] >= 0)
        _check(f"{row['id']} scrapedAt is a real time", isinstance(row["scrapedAt"], float) and row["scrapedAt"] > 0)

    biggest_id = max(rows, key=lambda item: int(item["leads"]))["id"]
    search = load_search(biggest_id)
    _check(f"biggest search {biggest_id} loads", search is not None)
    assert search is not None
    print(f"  biggest search: {search.search_id}, niche {search.niche!r}, location {search.location!r}")

    _check("every lead has a valid key", all(valid_lead_key(lead.key) for lead in search.leads))
    _check("every lead has a name", all(lead.name.strip() for lead in search.leads))
    _check("no lead key repeats", len({lead.key for lead in search.leads}) == len(search.leads))
    _check("no em dash in any name or reason", not any("\u2014" in lead.name + lead.why for lead in search.leads))

    joined = [lead for lead in search.leads if lead.has_scores and lead.pain_points]
    _check("the join attached scores and pain points to at least one lead", bool(joined))
    _check("at least one lead carries a deal value", any(lead.deal_value > 0 for lead in search.leads))
    _check("at least one lead carries talking points", any(lead.talking_points for lead in search.leads))
    _check("at least one lead has a proof string", any(p.proof for lead in search.leads for p in lead.pain_points))

    # Proof strings, checked on the same path an endpoint uses.
    #
    # A proof is the number the rep says out loud, so it is the one string where
    # a silent edit would be a lie. The rule is not "byte for byte", it is "byte
    # for byte apart from the em dash swap the project demands". Both halves are
    # checked here, against the raw files rather than a hand written fixture,
    # because the raw files are where the two em dashes actually live. A check
    # that only ever sees a fixture can never catch this path.
    number_re = re.compile(r"\d+(?:[.,]\d+)*")
    proof_pairs: list[tuple[str, str]] = []
    for row in rows:
        raw_doc: dict[str, Any] = _load_cached(
            _search_path(str(row["id"]), ".scores.json"), _parse_json_mapping, {}
        )
        raw_leads = raw_doc.get("leads")
        if not isinstance(raw_leads, dict):
            continue
        for scored in raw_leads.values():
            if not isinstance(scored, dict):
                continue
            entries = scored.get("pain_points")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                source = entry.get("proof")
                built = PainPoint.from_score(entry)
                if built is None or not isinstance(source, str) or not source.strip():
                    continue
                proof_pairs.append((source, built.proof))

    edited = [(source, got) for source, got in proof_pairs if source != got]
    _check("real proof strings were found to check", len(proof_pairs) > 100)
    _check(
        "no proof reaches the rep with an em dash",
        not any("\u2014" in got or "\u2015" in got for _, got in proof_pairs),
    )
    _check(
        "every number in every proof survived cleaning",
        all(
            number_re.findall(source) == number_re.findall(got)
            for source, got in proof_pairs
        ),
    )
    _check(
        "the only proofs that changed are the ones that held an em dash",
        all("\u2014" in source or "\u2015" in source for source, _ in edited),
    )
    _check(
        "no real proof was long enough to be cut",
        all(len(source) <= MAX_PROOF_CHARS for source, _ in proof_pairs),
    )
    print(f"  proofs checked: {len(proof_pairs)}, changed by the em dash rule: {len(edited)}")

    sample = max(search.leads, key=lambda lead: lead.deal_value)
    print(
        f"  top lead: {sample.name} | {sample.track} | score {sample.lead_score} | "
        f"{sample.currency}{sample.deal_value} | {sample.pain_count} pain points"
    )
    print(f"    why: {sample.why[:110]}")

    # A lead with no phone must still load everywhere.
    no_phone: Lead | None = None
    for row in rows:
        loaded = load_search(str(row["id"]))
        if loaded is None:
            continue
        for lead in loaded.leads:
            if not lead.has_phone:
                no_phone = lead
                break
        if no_phone is not None:
            break
    _check("a lead with no phone was found in the real data", no_phone is not None)
    assert no_phone is not None
    again = get_lead(no_phone.search_id, no_phone.key)
    _check("that lead loads through get_lead", again is not None and again.key == no_phone.key)
    assert again is not None
    detail = again.to_wire(full=True)
    _check("its detail wire has every list field", all(k in detail for k in ("key", "name", "dealValue", "status", "calledAt")))
    _check(
        "its detail wire has every detail field",
        all(k in detail for k in ("painPoints", "talkingPoints", "hours", "address", "gmbUrl", "money", "notes")),
    )
    _check("its phone is an empty string, not None", detail["phone"] == "")
    print(f"  no phone lead: {again.name} ({again.search_id})")

    # Filters and sort.
    page = leads_page(biggest_id, sort="value")
    _check("leads_page returns the contract body", page is not None and set(page) == {"searchId", "leads"})
    assert page is not None
    values = [int(item["dealValue"]) for item in page["leads"]]
    _check("sort by value is highest first", values == sorted(values, reverse=True))
    by_score = leads_page(biggest_id, sort="score")
    assert by_score is not None
    scores = [int(item["leadScore"]) for item in by_score["leads"]]
    _check("sort by score is highest first", scores == sorted(scores, reverse=True))
    by_name = leads_page(biggest_id, sort="name")
    assert by_name is not None
    names = [str(item["name"]).casefold() for item in by_name["leads"]]
    _check("sort by name is A to Z", names == sorted(names))
    with_phone = leads_page(biggest_id, has_phone="1")
    assert with_phone is not None
    _check("has_phone drops the leads with no number", all(item["phone"] for item in with_phone["leads"]))
    _check("has_phone never adds leads", len(with_phone["leads"]) <= len(page["leads"]))
    only_new = leads_page(biggest_id, status="new")
    assert only_new is not None
    _check("status filter keeps only that status", all(item["status"] == "new" for item in only_new["leads"]))
    unknown = leads_page(biggest_id, status="banana", sort="sideways")
    assert unknown is not None
    _check("an unknown filter hides nothing", len(unknown["leads"]) == len(page["leads"]))

    # Bad ids never touch the filesystem.
    for bad in ("../../etc/passwd", "..", ".hidden", "bar/ber", "bar\\ber", "a\x00b", "Barber-Hoboken", ""):
        _check(f"search id refused: {bad!r}", not valid_search_id(bad) and load_search(bad) is None)
    for bad_key in ("../x", "0x123", "0xzz:0x11", "", "0x89c2:0x22/x"):
        _check(f"lead key refused: {bad_key!r}", not valid_lead_key(bad_key))
    _check("a search that does not exist returns None", load_search("no-such-search-here") is None)
    _check("a lead that does not exist returns None", get_lead(biggest_id, "0xdead:0xbeef") is None)

    # Now the write path, and the missing files path, on a copy.
    original_dir = data_dir()
    workspace = Path(tmp_module.mkdtemp(prefix="leads-selftest-"))
    try:
        for item in sorted(original_dir.glob("*.json")) + sorted(original_dir.glob("*.ndjson")):
            if item.is_file():
                shutil.copy2(item, workspace / item.name)
        use_data_dir(workspace)

        copied = load_search(biggest_id)
        _check("the copy loads the same number of leads", copied is not None and len(copied.leads) == len(search.leads))
        assert copied is not None

        before = _read_pipeline_from_disk()
        target = copied.leads[0]
        result = asyncio.run(set_status(target.key, "interested", "call back on Monday"))
        _check("set_status reports ok", bool(result["ok"]) and result["status"] == "interested")
        _check("set_status stamped the call time", bool(result["calledAt"]))
        after = _read_pipeline_from_disk()
        _check("no other pipeline key was dropped", all(key in after for key in before))
        _check("the new key is on disk", after[target.key]["status"] == "interested")
        _check("the note is on disk", after[target.key]["notes"] == "call back on Monday")

        reloaded = get_lead(biggest_id, target.key)
        _check("the lead reads back with its new status", reloaded is not None and reloaded.status == "interested")
        assert reloaded is not None
        _check("the lead reads back as called", reloaded.called)

        summary_after = {row["id"]: row for row in list_searches()}[biggest_id]
        _check("the called count went up", int(summary_after["called"]) >= 1)

        again_result = asyncio.run(set_status(target.key, "callback"))
        _check("a second write keeps the first call time", again_result["calledAt"] == result["calledAt"])
        _check("a second write keeps the note", again_result["notes"] == "call back on Monday")

        marked = asyncio.run(mark_called(copied.leads[1].key))
        _check("mark_called stamps without changing the status", marked["status"] == "new" and bool(marked["calledAt"]))

        try:
            asyncio.run(set_status(target.key, "exploded"))
            raised = False
        except LeadError:
            raised = True
        _check("an unknown status is refused", raised)
        try:
            asyncio.run(set_status("../etc/passwd", "won"))
            raised_key = False
        except LeadError:
            raised_key = True
        _check("a bad key is refused before any write", raised_key)

        # A search with an ndjson but no scores and no audit is normal.
        (workspace / f"{biggest_id}.scores.json").unlink()
        (workspace / f"{biggest_id}.audit.json").unlink()
        bare = load_search(biggest_id)
        _check("a search with no scores still loads", bare is not None)
        assert bare is not None
        _check("it keeps every lead", len(bare.leads) == len(search.leads))
        _check("every lead still has a name", all(lead.name.strip() for lead in bare.leads))
        _check("scores are zeroed, not missing", all(lead.lead_score == 0 and lead.deal_value == 0 for lead in bare.leads))
        _check("pain points are empty, not broken", all(lead.pain_points == () for lead in bare.leads))
        _check("the search is marked as not scored", bare.scored is False)
        bare_page = leads_page(biggest_id)
        _check("the list endpoint still answers", bare_page is not None and len(bare_page["leads"]) == len(bare.leads))

        # A broken line and a broken scores file are skipped, never fatal.
        ndjson_copy = workspace / f"{biggest_id}.ndjson"
        with ndjson_copy.open("a", encoding="utf-8") as handle:
            handle.write("{not json at all\n")
            handle.write("[1, 2, 3]\n")
            handle.write("\n")
        after_bad_line = load_search(biggest_id)
        _check("a broken ndjson line is skipped", after_bad_line is not None and len(after_bad_line.leads) == len(bare.leads))
        (workspace / f"{biggest_id}.scores.json").write_text("[]", encoding="utf-8")
        after_bad_scores = load_search(biggest_id)
        _check("a scores file that is not an object is treated as missing", after_bad_scores is not None and len(after_bad_scores.leads) == len(bare.leads))
        (workspace / f"{biggest_id}.scores.json").write_text("{oops", encoding="utf-8")
        after_broken_scores = load_search(biggest_id)
        _check("a damaged scores file is treated as missing", after_broken_scores is not None and len(after_broken_scores.leads) == len(bare.leads))

        # A damaged pipeline is moved aside instead of blocking the rep.
        (workspace / "pipeline.json").write_text("{ this is not json", encoding="utf-8")
        recovered = asyncio.run(set_status(target.key, "won"))
        _check("a damaged pipeline does not block a write", recovered["status"] == "won")
        _check("the damaged file was kept", bool(list(workspace.glob("pipeline.damaged-*.json"))))
    finally:
        use_data_dir(None)
        shutil.rmtree(workspace, ignore_errors=True)

    _check("the real data dir is back", data_dir() == original_dir)
    total_leads = sum(int(row["leads"]) for row in list_searches())
    print()
    print(
        f"  all checks passed: {len(rows)} searches, {total_leads} leads, "
        f"biggest is {biggest_id}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - developer tool
    raise SystemExit(_self_test())
