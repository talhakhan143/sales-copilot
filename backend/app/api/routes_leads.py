"""REST routes for the lead list, the lead detail, the lead call and the scraper.

Everything here is mounted under ``/api/leads`` and every JSON shape is frozen by
the leads contract, section 3.1. Three rules drive the whole module.

First, a lead call is an ordinary call. ``POST /{search_id}/{key}/call`` builds
the context out of the audit and then hands it to the very same
``context_builder.prepare_context`` the setup page uses, so the session it opens
is a normal session. The teleprompter, the objection buttons, the reading style
switch and all four calling providers work on it with no special casing at all.
The only thing this route adds to the answer is the lead key and the lead name,
so the call page can write the outcome back when the call ends.

Second, nothing about the rep's data may crash the screen. The lead engine
writes five plain files per search and any of them can be missing, half written
or newer than this code. Every value is therefore read through the tolerant
readers below, exactly the way ``routes_practice`` reads the scoring module, and
a field this code has never seen is simply not shown.

Third, this layer owns no lead logic. ``app.services.leads`` reads the files,
builds the ``Lead`` shape and writes the pipeline, ``app.services.lead_context``
builds the block the copilot reads, and ``app.services.lead_jobs`` runs the
scraper. This module checks the ids, shapes the wire and nothing else.

ROUTE ORDER, THE TRAP IN THIS FILE
----------------------------------
Starlette matches routes in the order they are registered, and ``/{search_id}``
matches the literal word ``searches`` just as happily as it matches
``barber-hoboken``. The same goes for ``/{search_id}/{key}`` against
``/jobs/{job_id}``. So the fixed paths, ``/searches``, ``/search`` and
``/jobs/{job_id}``, are declared FIRST, in one block, above every parameterised
path. Moving one of them below the ``/{search_id}`` routes would not raise
anything, it would quietly answer the searches list with a 404 for a search
called "searches". A prefix such as ``/by-id/{search_id}`` would also have
worked, but it would have changed the URLs the contract froze, and the contract
wins.
"""

from __future__ import annotations

import asyncio
import csv
import dataclasses
import io
import inspect
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app.api.routes_context import get_groq
from app.api.routes_practice import turn_into_practice
from app.models import (
    DEFAULT_LEAD_SORT,
    DEFAULT_LEAD_STATUS,
    MAX_LEAD_KEY_CHARS,
    MAX_SEARCH_ID_CHARS,
    JobStatusResponse,
    LeadCallRequest,
    LeadCallResponse,
    LeadConfigRequest,
    LeadConfigResponse,
    LeadDetail,
    LeadRow,
    MessageModel,
    LeadsResponse,
    LeadStatusRequest,
    LeadStatusResponse,
    MoneyModel,
    PainPointModel,
    PrepareContextRequest,
    ScrapeRequest,
    ScrapeResponse,
    SearchesResponse,
    SearchSummary,
)
from app.services.context_builder import prepare_context
from app.services.session_store import store

log = logging.getLogger("salescopilot.api.leads")

router = APIRouter(prefix="/api/leads", tags=["leads"])


# ====================================================================== #
# the three services, imported softly
#
# These modules are the lead half of the app. The other half, the teleprompter
# and the calling, does not need them at all. So a missing or broken lead module
# must not stop the process from booting, because that would take the phone
# calls down with it. The import failure is logged loudly once, the leads
# endpoints answer a plain 503, and everything else keeps working.
# ====================================================================== #

try:
    from app.services import leads as leads_service
except Exception:  # noqa: BLE001 - a broken lead module must not kill the app.
    leads_service = None  # type: ignore[assignment]
    log.exception("app.services.leads could not be imported, the leads screen is off")

try:
    from app.services import lead_context as context_service
except Exception:  # noqa: BLE001 - same reason as above.
    context_service = None  # type: ignore[assignment]
    log.exception("app.services.lead_context could not be imported, lead calls are off")

try:
    from app.services import lead_jobs as jobs_service
except Exception:  # noqa: BLE001 - same reason as above.
    jobs_service = None  # type: ignore[assignment]
    log.exception("app.services.lead_jobs could not be imported, new searches are off")


# ====================================================================== #
# copy
#
# Read by a rep who is about to talk to a stranger, and English is not their
# first language. Short words, short sentences, and never a word they cannot
# act on.
# ====================================================================== #

LEADS_OFF: str = "The lead list is not set up on this server yet."
"""Answer when the lead service is missing. The log line says which part."""

JOBS_OFF: str = "New searches are not set up on this server yet."
"""Answer when the scrape job service is missing."""

BAD_SEARCH_ID: str = "That search name is not valid. Pick a search from the list."
"""Answer for a search id that could never be a real one."""

UNKNOWN_SEARCH: str = "That search is not on this computer. Pick another one."
"""Answer for a search id that is valid but has no files."""

UNKNOWN_LEAD: str = "That lead is not in this search."
"""Answer for a lead key that is not in the search."""

UNKNOWN_JOB: str = "That search job is unknown. It may have finished a while ago."
"""Answer for a job id the job service does not hold any more."""

NEED_WORDS: str = "Write what you sell and the city. Like barber and hoboken."
"""Answer when a new search is missing the niche or the location.

Only for the empty case. When the job service refuses a search for its own
reason, that reason is shown instead. See :func:`_plain_reason`.
"""

MAX_REFUSAL_CHARS: int = 200
"""Longest refusal line taken from a service. Longer text is cut and ended with a dot.

A refusal is one line under a form, so it has to stay one line. Every message
the job service writes today is far shorter than this, and the cut is only here
so a future message can never push the button off the screen.
"""

STATUS_FAILED: str = "The lead was not saved. Try again."
"""Answer when the pipeline file could not be written."""

SCRAPE_FAILED: str = "The search did not start. Try again."
"""Answer when the job service refused to start a scrape."""

ALREADY_RUNNING: str = "A search is already running. This is the one you are watching."
"""Answer when a second scrape is asked for while one is still open."""

SEARCH_STARTED: str = "The search is running. A browser window will open and read Google Maps."
"""Answer when a fresh scrape really started."""

CALL_GOAL_SEO: str = "Get them to agree to a paid SEO retainer."
"""Goal for a business that already has a site, so the job is ranking it."""

CALL_GOAL_WEBSITE: str = "Get them to agree to a first website."
"""Goal for a business with no site at all."""

CALL_GOAL_OTHER: str = "Get them to agree to a first paid job."
"""Goal when the audit did not pick a track, so neither promise can be made."""


# ====================================================================== #
# service lookup
#
# Each service function is found by a small list of the names it could carry,
# best guess first. The names that are really there today are the first entry in
# every list. The rest are there because these three modules are written and
# rewritten on their own, and a renamed function should cost the rep a plain 503
# on one screen, never an ImportError that takes the phone calls down with it.
# ====================================================================== #

_LIST_SEARCHES: tuple[str, ...] = ("list_searches", "search_summaries", "searches")
_LEADS_PAGE: tuple[str, ...] = ("leads_page", "list_leads", "leads_for_search")
_GET_LEAD: tuple[str, ...] = ("get_lead", "load_lead", "find_lead")
_SET_STATUS: tuple[str, ...] = ("set_status", "update_status", "write_status")
_MARK_CALLED: tuple[str, ...] = ("mark_called", "stamp_called", "set_called")
_VALID_SEARCH_ID: tuple[str, ...] = ("valid_search_id",)
_VALID_LEAD_KEY: tuple[str, ...] = ("valid_lead_key",)
_BUILD_CONTEXT: tuple[str, ...] = ("build_lead_context", "lead_context", "build_context")
_BUILD_GOAL: tuple[str, ...] = ("build_call_goal", "call_goal", "lead_call_goal")
_START_JOB: tuple[str, ...] = ("start_scrape", "start_search", "start_job")
_JOB_STATUS: tuple[str, ...] = ("get_job", "job_status", "job")
_JOB_SLUG: tuple[str, ...] = ("search_id", "search_slug", "slug_for")


def _lookup(module: object, names: Sequence[str]) -> Callable[..., Any] | None:
    """Find the first callable on a module out of a list of possible names.

    Args:
        module: The service module, or ``None`` when it failed to import.
        names: Names to try in order, best guess first.

    Returns:
        The callable, or ``None`` when the module is missing or carries none of
        the names.
    """
    if module is None:
        return None
    for name in names:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return candidate
    return None


def _need(module: object, names: Sequence[str], message: str, label: str) -> Callable[..., Any]:
    """Return a service function, or refuse the request with a plain message.

    Args:
        module: The service module.
        names: Names to try in order.
        message: What the rep reads when it is not there.
        label: The module name, for the log line only.

    Returns:
        The callable.

    Raises:
        HTTPException: 503 when the module is missing or carries none of the
            names.
    """
    found = _lookup(module, names)
    if found is None:
        log.error("%s has none of %s", label, ", ".join(names))
        raise HTTPException(status_code=503, detail=message)
    return found


def _need_lead_fn(names: Sequence[str]) -> Callable[..., Any]:
    """Return a function from the lead service, or refuse with a 503.

    Args:
        names: Names to try in order.

    Returns:
        The callable.
    """
    return _need(leads_service, names, LEADS_OFF, "app.services.leads")


def _need_job_fn(names: Sequence[str]) -> Callable[..., Any]:
    """Return a function from the job service, or refuse with a 503.

    Args:
        names: Names to try in order.

    Returns:
        The callable.
    """
    return _need(jobs_service, names, JOBS_OFF, "app.services.lead_jobs")


async def _call_io(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a service function that reads or writes files.

    A search can hold megabytes of JSON, and a rep may be on a live call while
    the leads list is polled, so a plain function is pushed onto a worker thread
    rather than parsed on the event loop. A coroutine function is awaited where
    it is, because it already yields on its own and, in the case of the pipeline
    write, holds an asyncio lock that only means anything on this loop.

    Args:
        fn: The service function.
        *args: Positional arguments for it.
        **kwargs: Keyword arguments for it.

    Returns:
        Whatever the function produced, awaited when it was awaitable.
    """
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    result = await asyncio.to_thread(fn, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _call_loop(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a service function that has to stay on the event loop.

    The job service starts an asyncio subprocess and keeps tasks alive, and none
    of that works from a worker thread, so these calls are made where the loop
    is. They do no file parsing, so there is nothing to move off it anyway.

    Args:
        fn: The service function.
        *args: Positional arguments for it.
        **kwargs: Keyword arguments for it.

    Returns:
        Whatever the function produced, awaited when it was awaitable.
    """
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


# ====================================================================== #
# tolerant field readers
#
# The same pattern ``routes_practice`` uses on the scoring module, and for the
# same reason: another module owns these objects and may hand back a dataclass,
# a pydantic model or a plain dict, spelled snake_case or camelCase. The leads
# screen must survive all of it, so every field is read through these.
# ====================================================================== #

_MISSING: Any = object()
"""Sentinel telling "this object has no such field" apart from a real ``None``."""


def _field(obj: object, *names: str, default: Any = None) -> Any:
    """Read the first field that exists, from a mapping or from an object.

    Args:
        obj: A dataclass, a pydantic model, a mapping, or ``None``.
        *names: Field names to try in order.
        default: Returned when none of the names is present.

    Returns:
        The first value found, otherwise ``default``.
    """
    if obj is None:
        return default
    for name in names:
        if isinstance(obj, Mapping):
            if name in obj:
                return obj[name]
            continue
        value = getattr(obj, name, _MISSING)
        if value is not _MISSING:
            return value
    return default


def _str_field(obj: object, *names: str, default: str = "") -> str:
    """Read one field and coerce it to a stripped string.

    Args:
        obj: The object or mapping to read from.
        *names: Field names to try in order.
        default: Value used when the field is missing or empty.

    Returns:
        The stripped text, or ``default``.
    """
    value = _field(obj, *names, default=None)
    if value is None or isinstance(value, (Mapping, list, tuple)):
        return default
    text = str(value).strip()
    return text or default


def _opt_str_field(obj: object, *names: str) -> str | None:
    """Read one field that is allowed to be absent.

    Args:
        obj: The object or mapping to read from.
        *names: Field names to try in order.

    Returns:
        The stripped text, or ``None`` when there is nothing there. An empty
        website or phone is ``None`` on the wire, never an empty string, so the
        screen can test one thing.
    """
    text = _str_field(obj, *names)
    return text or None


def _int_field(obj: object, *names: str, default: int = 0) -> int:
    """Read one field and coerce it to a whole number.

    Args:
        obj: The object or mapping to read from.
        *names: Field names to try in order.
        default: Value used when the field is missing or not a number.

    Returns:
        The value as an ``int``.
    """
    value = _field(obj, *names, default=None)
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _float_field(obj: object, *names: str, default: float = 0.0) -> float:
    """Read one field and coerce it to a float.

    Args:
        obj: The object or mapping to read from.
        *names: Field names to try in order.
        default: Value used when the field is missing or not a number.

    Returns:
        The value as a ``float``.
    """
    value = _field(obj, *names, default=None)
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _opt_float_field(obj: object, *names: str) -> float | None:
    """Read a number that is allowed to be absent, such as a star rating.

    Args:
        obj: The object or mapping to read from.
        *names: Field names to try in order.

    Returns:
        The number, or ``None`` when the field is missing or unreadable. A
        business with no reviews has no rating, and zero stars would be a lie.
    """
    value = _field(obj, *names, default=None)
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rows(obj: object, *names: str) -> list[Any]:
    """Read one field that should be a list.

    Args:
        obj: The object or mapping to read from.
        *names: Field names to try in order.

    Returns:
        The list, or an empty list when the field is missing or not a sequence.
    """
    value = _field(obj, *names, default=None)
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _epoch(value: object) -> float | None:
    """Turn a timestamp of any shape into unix seconds.

    The raw files carry ISO strings such as ``2026-08-25T11:03:14+00:00`` while
    the wire wants a number, and the service may hand over either, so both are
    accepted here rather than guessed at in two places.

    Args:
        value: A number, a numeric string, an ISO 8601 string, or ``None``.

    Returns:
        Seconds since the epoch, or ``None`` when there is no readable date.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _now_iso() -> str:
    """Return the time now in the shape the pipeline file already uses.

    Returns:
        A UTC ISO 8601 string with milliseconds and a ``Z``, for example
        ``2026-08-25T11:31:30.602Z``.
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _readable(lead: object) -> object:
    """Return one mapping holding everything known about a lead.

    ``Lead.to_wire()`` is the contract's own shape, so it wins wherever it has
    a field. It is the LIST row though, so the drawer fields, the pain points,
    the hours, the address, the money and the notes, are not in it. Those live
    on the dataclass, so the two are merged: the dataclass first, the wire on
    top. The keys do not clash, one side spells them camelCase and the other
    snake_case, and the readers above try both.

    Args:
        lead: Whatever the lead service returned.

    Returns:
        The merged mapping, or the object itself when there was nothing to
        merge, which the readers cope with as well.
    """
    merged: dict[str, Any] = {}
    if dataclasses.is_dataclass(lead) and not isinstance(lead, type):
        try:
            merged.update(dataclasses.asdict(lead))
        except Exception:  # noqa: BLE001 - fall through to the wire dict.
            log.exception("reading the lead dataclass failed")
    elif isinstance(lead, Mapping):
        merged.update(lead)

    to_wire = getattr(lead, "to_wire", None)
    if callable(to_wire):
        try:
            wire = to_wire()
        except Exception:  # noqa: BLE001 - fall back to what we already have.
            log.exception("Lead.to_wire failed, reading the object directly")
        else:
            if isinstance(wire, Mapping):
                merged.update(wire)

    return merged or lead


# ====================================================================== #
# id checks
#
# Both ids arrive in the URL and both end up naming files or dictionary keys
# inside the rep's own data folder, so they are checked here before they are
# passed on. The lead service checks its own inputs too, and where it exposes
# that check it is run here as well. Two locks on one door is the point: this
# one refuses the request early with a message the rep can read, that one
# protects the files whoever calls it.
# ====================================================================== #

_SEARCH_ID_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]*$")
"""A search slug is lower case letters, digits and dashes, and nothing else.

That is exactly what ``leadengine.scrape.search_id`` produces, so anything else
was never a search on this computer. It also means a dot, a slash or a
backslash can never reach the file readers.
"""

_LEAD_KEY_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9:@+._-]+$")
"""A lead key is the Google feature id, two hex ids joined by a colon."""

_JOB_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9:._-]+$")
"""A job id is whatever the job service made, kept to safe characters."""


def _clean_search_id(raw: str) -> str:
    """Check a search id and return it in its canonical shape.

    Args:
        raw: The id straight out of the URL.

    Returns:
        The lower case id.

    Raises:
        HTTPException: 400 with a plain message when it could never be a real
            search id.
    """
    value = (raw or "").strip().lower()
    if not value or len(value) > MAX_SEARCH_ID_CHARS or not _SEARCH_ID_RE.match(value):
        raise HTTPException(status_code=400, detail=BAD_SEARCH_ID)
    checker = _lookup(leads_service, _VALID_SEARCH_ID)
    if checker is not None and not checker(value):
        raise HTTPException(status_code=400, detail=BAD_SEARCH_ID)
    return value


def _clean_lead_key(raw: str) -> str:
    """Check a lead key and return it.

    A key that could never exist is answered as an unknown lead rather than as a
    bad request, because that is what it is: there is no such lead in this
    search, and the contract answers 404 for that.

    Args:
        raw: The key straight out of the URL.

    Returns:
        The stripped key.

    Raises:
        HTTPException: 404 with a plain message when the key is malformed.
    """
    value = (raw or "").strip()
    if (
        not value
        or len(value) > MAX_LEAD_KEY_CHARS
        or ".." in value
        or not _LEAD_KEY_RE.match(value)
    ):
        raise HTTPException(status_code=404, detail=UNKNOWN_LEAD)
    checker = _lookup(leads_service, _VALID_LEAD_KEY)
    if checker is not None and not checker(value):
        raise HTTPException(status_code=404, detail=UNKNOWN_LEAD)
    return value


def _clean_job_id(raw: str) -> str:
    """Check a job id and return it.

    Args:
        raw: The id straight out of the URL.

    Returns:
        The stripped id.

    Raises:
        HTTPException: 404 with a plain message when the id is malformed.
    """
    value = (raw or "").strip()
    if not value or len(value) > 120 or ".." in value or not _JOB_ID_RE.match(value):
        raise HTTPException(status_code=404, detail=UNKNOWN_JOB)
    return value


# ====================================================================== #
# wire builders
# ====================================================================== #


def _search_summary(row: object) -> SearchSummary:
    """Shape one search into the picker row.

    Args:
        row: Whatever the lead service returned for this search.

    Returns:
        The wire model. A search with no id is impossible to open, so callers
        drop those rather than draw a button that goes nowhere.
    """
    return SearchSummary(
        id=_str_field(row, "id", "search_id", "searchId", "slug"),
        niche=_str_field(row, "niche", "search_niche", "searchNiche"),
        location=_str_field(row, "location", "search_location", "searchLocation", "city"),
        leads=_int_field(row, "leads", "lead_count", "leadCount", "total"),
        called=_int_field(row, "called", "called_count", "calledCount"),
        value=_int_field(row, "value", "value_left", "valueLeft", "money"),
        currency=_str_field(row, "currency", "currency_symbol", "currencySymbol", default="$"),
        scraped_at=_epoch(_field(row, "scrapedAt", "scraped_at", "created_at", "createdAt")),
    )


def _row_fields(source: object) -> dict[str, Any]:
    """Read the fields every lead row carries.

    Kept apart from :func:`_lead_row` so the drawer and the row are built from
    one reader and cannot show a different name or a different price for the
    same business.

    Args:
        source: The lead's merged mapping, or the lead object itself.

    Returns:
        The keyword arguments shared by ``LeadRow`` and ``LeadDetail``.
    """
    return {
        "key": _str_field(source, "key", "lead_key", "leadKey", "feature_id", "featureId"),
        "name": _str_field(source, "name"),
        "category": _str_field(source, "category"),
        "city": _str_field(source, "city"),
        "phone": _opt_str_field(source, "phone"),
        "website": _opt_str_field(source, "website", "site", "url"),
        "rating": _opt_float_field(source, "rating"),
        "reviews": _int_field(source, "reviews", "review_count", "reviewCount"),
        "track": _str_field(source, "track"),
        "lead_score": _int_field(source, "leadScore", "lead_score"),
        "urgency": _int_field(source, "urgency"),
        "why": _str_field(source, "why", "need_reason", "needReason", "urgency_reason"),
        "deal_value": _int_field(source, "dealValue", "deal_value", "deal_value_usd"),
        "currency": _str_field(
            source, "currency", "currency_symbol", "currencySymbol", default="$"
        ),
        "pain_count": _int_field(source, "painCount", "pain_count"),
        "status": _str_field(source, "status", default=DEFAULT_LEAD_STATUS),
        "called_at": _opt_str_field(source, "calledAt", "called_at", "contactedAt", "contacted_at"),
        "priority": _priority_field(source),
        # The money the queue and the analytics screens add up. The list shape
        # carries these flat and the drawer carries them inside the money block,
        # so both are tried, flat first. Reading only the block gave every list
        # row a zero contract, which quietly zeroed the whole analytics screen.
        "contract_value": _money_number(source, "contractValue", "contract_value_usd"),
        "expected_value": _money_number(source, "expectedValue", "expected_value_usd"),
    }


def _money_number(source: object, flat: str, nested: str) -> int:
    """Read one money figure from wherever this shape happens to keep it.

    Args:
        source: The lead's merged mapping.
        flat: The camelCase name on a list row.
        nested: The snake_case name inside the money block.

    Returns:
        The number, or 0 when neither shape has it.
    """
    direct = _int_field(source, flat)
    if direct:
        return direct
    return _int_field(_readable(_field(source, "money")), nested)


#: What the scoring step calls the three answers to "when do I get to them".
_PRIORITIES: Final[frozenset[str]] = frozenset({"now", "week", "later"})


def _priority_field(source: object) -> str:
    """Read when to contact this lead, falling back to the urgency behind it.

    The scoring step writes ``priority`` and every lead it scored has one. A
    lead from a search whose scoring never ran has none, and putting all of
    those in "now" would drown the queue on the one screen that exists to say
    what to do first. So an unscored lead is derived from urgency and lands in
    "later" when even that is missing.

    Args:
        source: The lead's merged mapping, or the lead object itself.

    Returns:
        One of ``now``, ``week`` or ``later``.
    """
    raw = str(_str_field(source, "priority") or "").strip().lower()
    if raw in _PRIORITIES:
        return raw
    urgency = _int_field(source, "urgency")
    if urgency >= 4:
        return "now"
    if urgency >= 2:
        return "week"
    return "later"


def _lead_row(lead: object) -> LeadRow:
    """Shape one lead into a list row.

    Args:
        lead: Whatever the lead service returned.

    Returns:
        The wire model.
    """
    return LeadRow(**_row_fields(_readable(lead)))


def _pain_points(source: object) -> list[PainPointModel]:
    """Shape the audit findings for the drawer.

    Every finding is kept here. Cutting the list to the strongest five is the
    call context's job, because a long list makes the copilot pick a weak one,
    while a rep reading the drawer wants all of it.

    Args:
        source: The lead's merged mapping, or the lead object itself.

    Returns:
        One model per finding, in the order they came.
    """
    points: list[PainPointModel] = []
    for raw in _rows(source, "painPoints", "pain_points"):
        points.append(
            PainPointModel(
                title=_str_field(raw, "title"),
                detail=_str_field(raw, "detail", "sales_line", "salesLine"),
                proof=_str_field(raw, "proof", "evidence"),
                severity=_str_field(raw, "severity").lower(),
            )
        )
    return points


def _talking_points(source: object) -> list[str]:
    """Shape the rep's own notes for the drawer.

    Args:
        source: The lead's merged mapping, or the lead object itself.

    Returns:
        The lines, empty ones dropped.
    """
    lines: list[str] = []
    for raw in _rows(source, "talkingPoints", "talking_points"):
        text = str(raw).strip()
        if text:
            lines.append(text)
    return lines


def _hours(source: object) -> list[dict[str, str]]:
    """Shape the opening hours into rows of two plain strings.

    The raw listing carries a day and an hours string per row, but a listing
    with no hours, or one that stored a bare line of text, both happen. Every
    shape ends up as the same two keys here so the drawer never has to branch.

    Args:
        source: The lead's merged mapping, or the lead object itself.

    Returns:
        One row per day, each with a ``day`` key and an ``hours`` key.
    """
    rows: list[dict[str, str]] = []
    for raw in _rows(source, "hours"):
        if isinstance(raw, Mapping):
            day = _str_field(raw, "day", "name", "weekday")
            hours = _str_field(raw, "hours", "time", "value")
        elif hasattr(raw, "day") or hasattr(raw, "hours"):
            day = _str_field(raw, "day")
            hours = _str_field(raw, "hours")
        else:
            day = ""
            hours = str(raw).strip()
        if day or hours:
            rows.append({"day": day, "hours": hours})
    return rows


def _money(source: object) -> MoneyModel:
    """Shape the deal maths for the drawer.

    Always an object, never ``None``. A search that has only been scraped has no
    scores file yet, which the leads contract calls a normal state, and the lead
    service already answers that with an all zero money block. Sending ``null``
    instead would make every row in that search fail the browser's check and the
    drawer would refuse to open at all, which is far worse than a zero price.
    The screen decides on its own not to draw a price that is zero.

    Args:
        source: The lead's merged mapping, or the lead object itself.

    Returns:
        The model. Zeros throughout when nothing has been worked out yet.
    """
    raw = _field(source, "money")
    if raw is None:
        return MoneyModel()
    return MoneyModel(
        tier_label=_str_field(raw, "tierLabel", "tier_label"),
        deal_value_usd=_int_field(raw, "dealValueUsd", "deal_value_usd"),
        retainer_monthly_usd=_int_field(raw, "retainerMonthlyUsd", "retainer_monthly_usd"),
        contract_value_usd=_int_field(raw, "contractValueUsd", "contract_value_usd"),
        currency_symbol=_str_field(raw, "currencySymbol", "currency_symbol", default="$"),
        close_probability=_float_field(raw, "closeProbability", "close_probability"),
        expected_value_usd=_int_field(raw, "expectedValueUsd", "expected_value_usd"),
    )


def _lead_detail(lead: object, search_id: str = "") -> LeadDetail:
    """Shape one lead into the full drawer object.

    Args:
        lead: Whatever the lead service returned.
        search_id: The search the lead came from. Needed for the two things that
            live in their own files rather than on the lead: the copywriter's
            drafts and the screenshots. Left empty by callers that only want the
            lead itself, such as the call context builder, and both then come
            back empty rather than being looked up.

    Returns:
        The wire model, which is the list row plus everything the drawer shows.
    """
    source = _readable(lead)
    key = _str_field(source, "key", "lead_key", "leadKey")
    drafts = _messages(search_id, key)
    return LeadDetail(
        **_row_fields(source),
        pain_points=_pain_points(source),
        talking_points=_talking_points(source),
        address=_str_field(source, "address"),
        hours=_hours(source),
        gmb_url=_opt_str_field(source, "gmbUrl", "gmb_url"),
        money=_money(source),
        notes=_str_field(source, "notes"),
        urgency_reason=_str_field(source, "urgencyReason", "urgency_reason", "why"),
        messages=drafts,
        screenshots=_screenshots(search_id, key),
        message_count=len(drafts),
    )


def _messages(search_id: str, key: str) -> list[MessageModel]:
    """Read the copywriter's drafts for one lead.

    Args:
        search_id: The search slug, or empty to skip the lookup entirely.
        key: The lead key.

    Returns:
        Every draft, in channel order. Empty when the copywriter never ran,
        which is a normal state and never an error.
    """
    if not search_id or not key:
        return []
    reader = _lookup(leads_service, ("messages_for",))
    if reader is None:
        return []
    try:
        drafts = reader(search_id, key)
    except Exception:  # noqa: BLE001 - missing copy never blocks the drawer.
        log.exception("reading messages for %s failed", key)
        return []
    out: list[MessageModel] = []
    for draft in drafts or ():
        item = _readable(draft)
        body = _str_field(item, "body")
        if body:
            out.append(
                MessageModel(
                    channel=_str_field(item, "channel"),
                    subject=_str_field(item, "subject"),
                    body=body,
                )
            )
    return out


def _screenshots(search_id: str, key: str) -> dict[str, bool]:
    """Say which pictures of this lead's site are on disk.

    Booleans and not URLs, because the URL is the same shape every time and the
    only thing the drawer cannot work out for itself is whether the image is
    really there. Asking the browser to load one that is not gives the rep a
    broken frame instead of an honest "no picture".

    Args:
        search_id: The search slug, or empty to skip the lookup.
        key: The lead key.

    Returns:
        View name to whether it exists.
    """
    if not search_id or not key:
        return {}
    reader = _lookup(leads_service, ("has_screenshots",))
    if reader is None:
        return {}
    try:
        found = reader(search_id, key)
    except Exception:  # noqa: BLE001 - a missing picture is not a failure.
        log.exception("looking for screenshots of %s failed", key)
        return {}
    return found if isinstance(found, dict) else {}


# ====================================================================== #
# the call goal
#
# ``app.services.lead_context`` builds this, next to the block it belongs
# above, and that is what runs. The version below is the fallback for a build
# where that module did not import, so a rep can still call a lead with a goal
# that names the track and the money instead of no goal at all.
# ====================================================================== #


def _money_words(detail: LeadDetail) -> str:
    """Say what the deal is worth, in plain words, or say nothing.

    Nothing is invented here. A search that has not been scored yet has no
    price, and the goal then simply carries no money line at all.

    Args:
        detail: The lead being called.

    Returns:
        One short sentence, or an empty string.
    """
    money = detail.money
    symbol = money.currency_symbol or detail.currency or "$"
    deal = money.deal_value_usd if money.deal_value_usd > 0 else detail.deal_value
    if deal <= 0:
        return ""
    monthly = money.retainer_monthly_usd
    whole = money.contract_value_usd
    if monthly > 0:
        line = f"It is worth about {symbol}{deal:,} to start and {symbol}{monthly:,} every month."
        if whole > 0:
            line = f"{line} The whole deal is about {symbol}{whole:,}."
        return line
    if whole > 0 and whole != deal:
        return f"It is worth about {symbol}{deal:,}, and about {symbol}{whole:,} in all."
    return f"It is worth about {symbol}{deal:,}."


def _fallback_goal(detail: LeadDetail) -> str:
    """Build the one line goal for this call without the context service.

    Args:
        detail: The lead being called.

    Returns:
        The goal, always with a first sentence and usually with the money.
    """
    track = detail.track.strip().lower()
    if track == "seo":
        goal = CALL_GOAL_SEO
    elif track == "website":
        goal = CALL_GOAL_WEBSITE
    else:
        goal = CALL_GOAL_OTHER
    money = _money_words(detail)
    return f"{goal} {money}".strip() if money else goal


async def _call_goal(lead: object, detail: LeadDetail) -> str:
    """Ask the context service for the goal, or build a plain one here.

    Args:
        lead: The lead object the service handed over.
        detail: The same lead in wire shape, used by the fallback.

    Returns:
        One or two short sentences. Never empty.
    """
    builder = _lookup(context_service, _BUILD_GOAL)
    if builder is not None:
        try:
            goal = str(await _call_io(builder, lead) or "").strip()
        except Exception:  # noqa: BLE001 - a goal must never fail a call.
            log.exception("building the call goal failed, using the plain one")
        else:
            if goal:
                return goal
    return _fallback_goal(detail)


# ====================================================================== #
# routes, fixed paths first
#
# See the note at the top of this file. /searches, /search and /jobs/{job_id}
# must stay above every /{search_id} route or they will never be reached.
# ====================================================================== #


@router.get("/searches", response_model=SearchesResponse)
async def get_searches() -> SearchesResponse:
    """List every search on this computer, newest first.

    An empty list is a normal answer, not an error. A fresh install has no data
    folder at all, and the screen shows the empty state with the new search
    button in it.

    Returns:
        The searches, each with how many leads it holds, how many were called
        and how much money is still on the table.

    Raises:
        HTTPException: 503 when the lead service is not installed.
    """
    lister = _need_lead_fn(_LIST_SEARCHES)
    try:
        raw_rows = await _call_io(lister)
    except FileNotFoundError:
        return SearchesResponse(searches=[])
    except Exception:  # noqa: BLE001 - one bad file must not empty the screen.
        log.exception("listing searches failed")
        raise HTTPException(status_code=503, detail=LEADS_OFF) from None

    summaries = [_search_summary(row) for row in (raw_rows or [])]
    summaries = [row for row in summaries if row.id]
    # Sorted here as well as in the service. It costs nothing on a list of a few
    # rows, and it means the picker is in the same order whoever built the list.
    summaries.sort(key=lambda row: (-(row.scraped_at or 0.0), row.id))
    return SearchesResponse(searches=summaries)


@router.post("/search", response_model=ScrapeResponse)
async def post_search(payload: ScrapeRequest) -> Any:
    """Start a new Google Maps search in the background.

    This opens a real browser window and reads Maps for minutes, so it answers
    at once with a job id and the screen watches that job. Only one scrape runs
    at a time, so a second ask while one is open hands back the running job
    instead of opening a competing browser.

    Args:
        payload: The niche, the location, how many listings and the country.

    Returns:
        The search id the job will write under, and the job id to poll.

    Raises:
        HTTPException: 503 when the job service is not installed.
    """
    if not payload.niche or not payload.location:
        return _refuse_search(400, NEED_WORDS)

    starter = _need_job_fn(_START_JOB)
    try:
        job = await _call_loop(
            starter,
            payload.niche,
            payload.location,
            limit=payload.limit,
            country=payload.country,
        )
    except Exception as exc:  # noqa: BLE001 - the rep gets a plain refusal.
        bad_words = getattr(jobs_service, "InvalidSearchError", None)
        if bad_words is not None and isinstance(exc, bad_words):
            log.info("refused a search for %r %r, %s", payload.niche, payload.location, exc)
            # The job service writes these messages for the rep to read, and
            # they say the actual problem, for example that the niche cannot
            # start with a dash. Repeating "write what you sell and the city"
            # at someone whose boxes are both full gives them nothing to do.
            return _refuse_search(400, _plain_reason(exc) or NEED_WORDS)
        log.exception("starting a scrape for %r %r failed", payload.niche, payload.location)
        return _refuse_search(503, SCRAPE_FAILED)

    job_id = _str_field(job, "jobId", "job_id", "id")
    search_id = _str_field(job, "searchId", "search_id", "slug")
    if not job_id:
        log.error("the job service started a scrape but gave back no job id")
        return _refuse_search(503, SCRAPE_FAILED)

    wanted = _search_slug(payload.niche, payload.location)
    if not search_id:
        search_id = wanted
    # A different slug than the one just asked for can only mean one thing: an
    # older scrape is still running and this is the job that came back instead.
    message = ALREADY_RUNNING if wanted and search_id != wanted else SEARCH_STARTED
    log.info("Scrape job %s for %s, %s", job_id, search_id, message)
    return ScrapeResponse(ok=True, search_id=search_id, job_id=job_id, message=message)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: str) -> JobStatusResponse:
    """Report what a running search is doing right now.

    The last line the job printed is on the wire on purpose. A Chromium window
    scrolling Maps for four minutes behind a spinner looks broken, and one line
    of real output is the difference between waiting and giving up.

    Args:
        job_id: The id handed back by ``POST /api/leads/search``.

    Returns:
        The state, the stage, the last line, the search id and whether it is
        over.

    Raises:
        HTTPException: 404 when the job is unknown, 503 when the job service is
            not installed.
    """
    clean_id = _clean_job_id(job_id)
    reader = _need_job_fn(_JOB_STATUS)
    try:
        job = await _call_loop(reader, clean_id)
    except KeyError:
        job = None
    except Exception:  # noqa: BLE001 - a poll must never answer a 500.
        log.exception("reading job %s failed", clean_id)
        raise HTTPException(status_code=503, detail=JOBS_OFF) from None

    if job is None:
        raise HTTPException(status_code=404, detail=UNKNOWN_JOB)

    state = _str_field(job, "state", default="running").lower()
    done_value = _field(job, "done")
    done = bool(done_value) if done_value is not None else state in {"finished", "failed"}
    return JobStatusResponse(
        state=state,
        step=_str_field(job, "step", "stage"),
        line=_str_field(job, "line", "last_line", "lastLine"),
        search_id=_str_field(job, "searchId", "search_id", "slug"),
        done=done,
    )


# ====================================================================== #
# routes, parameterised paths
# ====================================================================== #


@router.get("/config", response_model=LeadConfigResponse)
async def get_lead_config() -> LeadConfigResponse:
    """Read the two files the settings screen owns.

    Declared above ``/{search_id}`` on purpose. Route order decides which
    pattern wins, and a search id matches literally anything, so a settings
    request registered after it would be answered as "no search called config".

    Returns:
        The rep's profile and the whole pricing file. Missing files come back as
        empty objects, which is a first run rather than a failure.

    Raises:
        HTTPException: 503 when the lead service is not installed.
    """
    reader = _need(leads_service, ("read_config",), LEADS_OFF, "app.services.leads")
    try:
        profile = await _call_io(reader, "profile")
        pricing = await _call_io(reader, "pricing")
    except Exception:  # noqa: BLE001 - an unreadable settings file is not a crash.
        log.exception("reading the lead engine config failed")
        raise HTTPException(status_code=503, detail=LEADS_OFF) from None
    return LeadConfigResponse(
        profile=profile if isinstance(profile, dict) else {},
        pricing=pricing if isinstance(pricing, dict) else {},
    )


@router.put("/config", response_model=LeadConfigResponse)
async def put_lead_config(payload: LeadConfigRequest) -> LeadConfigResponse:
    """Write the settings back.

    Each half is written only when it was sent, so a screen that edits the
    profile cannot blank the pricing file by leaving it out of the body.

    Args:
        payload: Either half of the settings, or both.

    Returns:
        Both files as they now stand on disk, so the screen redraws from what
        was really saved rather than from what it hoped it saved.

    Raises:
        HTTPException: 400 when a file cannot be written, 503 when the lead
            service is not installed.
    """
    writer = _need(leads_service, ("write_config",), LEADS_OFF, "app.services.leads")
    try:
        if payload.profile is not None:
            await _call_io(writer, "profile", payload.profile)
        if payload.pricing is not None:
            await _call_io(writer, "pricing", payload.pricing)
    except Exception as exc:  # noqa: BLE001 - the message says what went wrong.
        log.exception("saving the lead engine config failed")
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return await get_lead_config()


@router.get("/{search_id}", response_model=LeadsResponse)
async def get_leads(
    search_id: str,
    status: str = Query(default="", description="Keep only leads in this pipeline status."),
    has_phone: str = Query(default="", description="Send 1 to keep only leads with a phone."),
    sort: str = Query(default=DEFAULT_LEAD_SORT, description="value, score or name."),
) -> LeadsResponse:
    """List the leads in one search, filtered and ordered for calling.

    The filtering and the ordering are the lead service's job, not this layer's,
    so the three query values are handed straight over. It knows what a called
    lead is and what a lead is worth, and doing it twice would be two answers to
    one question.

    Args:
        search_id: The search slug, for example ``barber-hoboken``.
        status: One of the seven pipeline statuses, or empty for all of them.
        has_phone: ``1`` to hide the leads with no number.
        sort: ``value``, ``score`` or ``name``. Money first is the default.

    Returns:
        The rows, ready to draw in the order they are given.

    Raises:
        HTTPException: 400 for a search id that could never be real, 404 for one
            that has no files, 503 when the lead service is not installed.
    """
    clean_id = _clean_search_id(search_id)
    lister = _need_lead_fn(_LEADS_PAGE)

    try:
        page = await _call_io(
            lister,
            clean_id,
            status=status.strip() or None,
            has_phone=has_phone.strip() or None,
            sort=sort.strip() or None,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=UNKNOWN_SEARCH) from None
    except Exception:  # noqa: BLE001 - a broken file is not a crash.
        log.exception("listing leads for %s failed", clean_id)
        raise HTTPException(status_code=503, detail=LEADS_OFF) from None

    if page is None:
        raise HTTPException(status_code=404, detail=UNKNOWN_SEARCH)

    # A page object is the whole body. A plain list of leads is accepted too, so
    # the route does not care which of the two the service hands over.
    raw_leads = page if isinstance(page, (list, tuple)) else _rows(page, "leads")
    rows = [_lead_row(lead) for lead in raw_leads]

    # How many drafts the copywriter wrote, stamped on the rows in one pass. The
    # file is read once for the whole search and cached, so this is a dictionary
    # lookup per row rather than sixty five file reads.
    counts = _message_counts(clean_id)
    if counts:
        for row in rows:
            row.message_count = counts.get(row.key, 0)

    return LeadsResponse(search_id=clean_id, leads=[row for row in rows if row.key])


def _message_counts(search_id: str) -> dict[str, int]:
    """How many ready to send drafts each lead in a search has.

    Args:
        search_id: The search slug, already cleaned.

    Returns:
        Lead key to draft count. Empty when the copywriter never ran, which is
        normal and is why nothing here raises.
    """
    counter = _lookup(leads_service, ("message_counts",))
    if counter is None:
        return {}
    try:
        result = counter(search_id)
    except Exception:  # noqa: BLE001 - missing copy is never worth a 500.
        log.exception("counting messages for %s failed", search_id)
        return {}
    return result if isinstance(result, dict) else {}


@router.get("/{search_id}/export.csv")
async def get_lead_csv(search_id: str) -> Response:
    """Hand the whole calling list back as a spreadsheet.

    Declared above ``/{search_id}/{key}`` so ``export.csv`` is not read as a
    lead key.

    Every row the search holds, not the filtered view: the rep exporting a list
    is taking it somewhere else, and a file that quietly dropped the rows behind
    whatever tab happened to be open would be worse than useless.

    Args:
        search_id: The search slug.

    Returns:
        A CSV download named after the search.

    Raises:
        HTTPException: 404 when the search is unknown, 503 when the lead
            service is not installed.
    """
    clean_id = _clean_search_id(search_id)
    lister = _need(leads_service, ("leads_page",), LEADS_OFF, "app.services.leads")
    try:
        page = await _call_io(lister, clean_id)
    except Exception:  # noqa: BLE001 - a broken file is not a crash.
        log.exception("exporting %s failed", clean_id)
        raise HTTPException(status_code=503, detail=LEADS_OFF) from None
    if page is None:
        raise HTTPException(status_code=404, detail=UNKNOWN_SEARCH)

    raw_leads = page if isinstance(page, (list, tuple)) else _rows(page, "leads")
    rows = [_lead_row(lead) for lead in raw_leads]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Name", "Category", "City", "Phone", "Website", "Rating", "Reviews",
            "Track", "Priority", "Urgency", "Score", "Deal value", "Contract value",
            "Status", "Called at", "Why",
        ]
    )
    for row in rows:
        if not row.key:
            continue
        writer.writerow(
            [
                row.name, row.category, row.city, row.phone or "", row.website or "",
                row.rating if row.rating is not None else "", row.reviews,
                row.track, row.priority, row.urgency, row.lead_score,
                row.deal_value, row.contract_value, row.status, row.called_at or "",
                row.why,
            ]
        )

    # The BOM is for Excel. Without it Excel reads UTF-8 as its own local code
    # page and a business called "Café" arrives mangled, which is exactly the
    # kind of thing that makes a rep stop trusting the export.
    body = "\ufeff" + buffer.getvalue()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{clean_id}.csv"'},
    )


@router.get("/{search_id}/{key}/shot/{view}")
async def get_lead_screenshot(search_id: str, key: str, view: str) -> Response:
    """Serve one of the pictures the audit took of a lead's site.

    The file is found by rebuilding its name from the lead key, never by the
    path stored in the audit file. That path was written on whichever machine
    ran the scrape and points at a drive that does not exist here, and trusting
    a path out of a data file to name a file to serve is how a reader of leads
    becomes a reader of anything on the disk.

    Args:
        search_id: The search slug.
        key: The lead key.
        view: ``desktop`` or ``mobile``.

    Returns:
        The JPEG.

    Raises:
        HTTPException: 400 for a bad view, 404 when there is no such picture,
            503 when the lead service is not installed.
    """
    clean_id = _clean_search_id(search_id)
    clean_key = _clean_lead_key(key)
    finder = _need(leads_service, ("screenshot_path",), LEADS_OFF, "app.services.leads")
    try:
        path = await _call_io(finder, clean_id, clean_key, view)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception:  # noqa: BLE001 - a missing picture is not a crash.
        log.exception("reading the screenshot for %s failed", clean_key)
        raise HTTPException(status_code=503, detail=LEADS_OFF) from None

    if path is None:
        raise HTTPException(
            status_code=404,
            detail="There is no picture of this one's site.",
        )
    try:
        blob = await _call_io(Path(path).read_bytes)
    except OSError:
        raise HTTPException(
            status_code=404,
            detail="There is no picture of this one's site.",
        ) from None

    # Cached hard: the audit writes these once and never touches them again, and
    # the drawer opens the same two images every time the rep reopens a lead.
    return Response(
        content=blob,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/{search_id}/{key}", response_model=LeadDetail)
async def get_lead(search_id: str, key: str) -> LeadDetail:
    """Return everything known about one lead.

    Args:
        search_id: The search slug.
        key: The lead key, which is the Google feature id.

    Returns:
        The list row plus the pain points, the talking points, the hours, the
        address, the links, the deal maths and the rep's own notes.

    Raises:
        HTTPException: 400 for a bad search id, 404 when the lead is not there,
            503 when the lead service is not installed.
    """
    clean_id = _clean_search_id(search_id)
    clean_key = _clean_lead_key(key)
    return _lead_detail(await _load_lead(clean_id, clean_key), clean_id)


@router.post("/{search_id}/{key}/call", response_model=LeadCallResponse)
async def post_lead_call(
    search_id: str,
    key: str,
    payload: LeadCallRequest,
    request: Request,
) -> LeadCallResponse:
    """Build the call context from the audit and open a normal session.

    This is the heart of the merge. The rep types nothing. The audit already
    found what is wrong with this business, worked out what the job is worth and
    wrote the proof for every claim, so all of that becomes the client context,
    the prospect site is scraped the way it is for any other call, and the goal
    is built from the track.

    A lead with no phone is still called from here. The rep may have the number
    somewhere else, or may dial through WhatsApp, and refusing to prepare the
    context would only send them back to typing it by hand.

    Args:
        search_id: The search slug.
        key: The lead key.
        payload: The rep's knowledge base and the language to answer in.
        request: The incoming request, used to reach the shared Groq client.

    Returns:
        A plain prepare context response with the lead key and the lead name
        added, so the call page treats this like any other session.

    Raises:
        HTTPException: 400 for a bad search id or an unusable prospect URL, 404
            when the lead is not there, 503 when the lead service is not
            installed.
    """
    clean_id = _clean_search_id(search_id)
    clean_key = _clean_lead_key(key)
    groq = get_groq(request)

    lead = await _load_lead(clean_id, clean_key)
    detail = _lead_detail(lead)

    builder = _need(context_service, _BUILD_CONTEXT, LEADS_OFF, "app.services.lead_context")
    try:
        block = await _call_io(builder, lead)
    except Exception:  # noqa: BLE001 - see the note below.
        log.exception("building the call context for %s failed", clean_key)
        block = ""
    client_context = str(block or "").strip()
    if not client_context:
        # The call still goes ahead. A rep waiting to dial would rather have the
        # knowledge base alone than a red box, and the copilot asks discovery
        # questions when it is given no client block, which is the safe answer.
        log.warning("lead %s produced an empty call context", clean_key)

    context_request = PrepareContextRequest(
        knowledge_base=payload.knowledge_base,
        client_url=detail.website,
        client_context=client_context or None,
        call_goal=await _call_goal(lead, detail),
        language=payload.language,
    )

    try:
        context = await prepare_context(context_request, groq_configured=bool(groq.configured))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"That prospect URL is not usable: {exc}",
        ) from exc

    # A rehearsal against this business is still this business: the same audit,
    # the same notes, the same goal, and only the person on the other end is
    # made up. So the whole build above is shared and practice is one step at
    # the end, taken from the practice route itself rather than copied, because
    # a persona that behaves differently depending on which door it came through
    # would make the rehearsal worthless.
    practice_fields: dict[str, object] = {}
    if payload.mode == "practice":
        session = store.get(context.session_id)
        if session is None:
            raise HTTPException(
                status_code=500,
                detail="The practice call was lost right after it was made. Try again.",
            )
        practice_fields = turn_into_practice(
            session,
            context,
            knowledge_base=payload.knowledge_base,
            client_context=client_context or None,
            call_goal=context_request.call_goal,
            difficulty=payload.difficulty,
        )
    else:
        # The lead stops counting as money on the table the moment the call
        # starts. The status stays whatever it was until the rep picks an
        # outcome. A failure here is logged and swallowed, because the session is
        # already built and sending the rep back for a bookkeeping problem would
        # be absurd.
        #
        # A practice run never reaches this. Nobody was rung, so counting it as
        # a call would quietly eat the lead out of the money still on the table
        # and out of the To call tab, which is the opposite of what rehearsing
        # is for.
        stamper = _lookup(leads_service, _MARK_CALLED)
        if stamper is not None:
            try:
                await _call_io(stamper, clean_key)
            except Exception:  # noqa: BLE001 - the call matters more than the stamp.
                log.exception("could not stamp lead %s as called", clean_key)

    log.info(
        "Lead %s ready, session %s, lead %s (%s), site=%s",
        payload.mode,
        context.session_id,
        clean_key,
        detail.name or "unknown",
        detail.website or "none",
    )

    return LeadCallResponse.model_validate(
        {
            **context.model_dump(by_alias=True),
            "leadKey": detail.key or clean_key,
            "leadName": detail.name,
            **practice_fields,
        }
    )


@router.post("/{search_id}/{key}/status", response_model=LeadStatusResponse)
async def post_lead_status(
    search_id: str,
    key: str,
    payload: LeadStatusRequest,
) -> LeadStatusResponse:
    """Write what happened on the call back into the pipeline file.

    That file is the same one the rep's own lead dashboard reads, so the two
    views never disagree about where a lead stands. The pipeline is one file for
    every search, keyed by the lead, which is why the write takes the key alone.

    Args:
        search_id: The search slug, used to check the lead really is in it.
        key: The lead key.
        payload: The new status and an optional note.

    Returns:
        The status that was stored and when it was stored.

    Raises:
        HTTPException: 400 for a bad search id, 404 when the lead is not there,
            500 when the file could not be written, 503 when the lead service is
            not installed.
    """
    clean_id = _clean_search_id(search_id)
    clean_key = _clean_lead_key(key)

    # Checked before the write, so a typed key can never add a row the rep's own
    # dashboard would then show as a lead that does not exist.
    await _load_lead(clean_id, clean_key)

    writer = _need_lead_fn(_SET_STATUS)
    try:
        result = await _call_io(writer, clean_key, payload.status, notes=payload.notes)
    except Exception:  # noqa: BLE001 - the rep is told, and the log has the why.
        log.exception("writing status %s for lead %s failed", payload.status, clean_key)
        raise HTTPException(status_code=500, detail=STATUS_FAILED) from None

    if isinstance(result, str):
        updated_at = result.strip()
        stored = payload.status
    else:
        updated_at = _str_field(result, "updatedAt", "updated_at")
        stored = _str_field(result, "status", default=payload.status)

    log.info("Lead %s in %s is now %s", clean_key, clean_id, stored)
    return LeadStatusResponse(ok=True, status=stored, updated_at=updated_at or _now_iso())


# ====================================================================== #
# shared route helpers
# ====================================================================== #


def _plain_reason(exc: BaseException) -> str:
    """Pull one clean line out of a refusal the job service raised.

    ``lead_jobs.InvalidSearchError`` carries a message written for the rep, in
    the same plain English as the copy above, so it is shown as it is rather
    than replaced by a general line that may not fit the problem. Only the
    shape is fixed here: newlines are folded into spaces so the answer stays one
    line, and the length is capped.

    Args:
        exc: The exception the job service raised.

    Returns:
        One short line, or an empty string when the exception carried no text,
        which lets the caller fall back to its own copy.
    """
    text = " ".join(str(exc).split())
    if not text:
        return ""
    if len(text) > MAX_REFUSAL_CHARS:
        text = text[: MAX_REFUSAL_CHARS - 1].rstrip() + "."
    return text


def _refuse_search(status: int, message: str) -> JSONResponse:
    """Build a refusal that has the same shape as a started search.

    The screen reads ``message`` whether the search started or not, so it never
    has to guess from a status code. Same idea as the calling routes.

    Args:
        status: The HTTP status to answer with.
        message: One plain line saying what went wrong.

    Returns:
        The JSON response.
    """
    body = ScrapeResponse(ok=False, search_id="", job_id="", message=message)
    return JSONResponse(status_code=status, content=body.model_dump(by_alias=True))


async def _load_lead(search_id: str, key: str) -> object:
    """Fetch one lead or answer 404.

    Args:
        search_id: The already checked search slug.
        key: The already checked lead key.

    Returns:
        Whatever the lead service holds for this lead.

    Raises:
        HTTPException: 404 when the search or the lead is not there, 503 when
            the lead service is not installed.
    """
    getter = _need_lead_fn(_GET_LEAD)
    try:
        lead = await _call_io(getter, search_id, key)
    except (FileNotFoundError, KeyError):
        lead = None
    except Exception:  # noqa: BLE001 - a bad file is not a crash.
        log.exception("reading lead %s in %s failed", key, search_id)
        raise HTTPException(status_code=503, detail=LEADS_OFF) from None
    if lead is None:
        raise HTTPException(status_code=404, detail=UNKNOWN_LEAD)
    return lead


def _search_slug(niche: str, location: str) -> str:
    """Work out the file name prefix a search will write under.

    The job service owns this name, so its own function is used when it has one.
    The copy below is the fallback, and it mirrors ``leadengine.scrape.search_id``
    character for character. That module is not imported here because importing
    it pulls in the Playwright browser stack, and the API process has no reason
    to load a browser driver just to name a file.

    Args:
        niche: What is being searched for.
        location: Where it is being searched.

    Returns:
        The slug, for example ``barber-hoboken``, or an empty string when even
        the fallback could make nothing of the words.
    """
    slugger = _lookup(jobs_service, _JOB_SLUG)
    if slugger is not None:
        try:
            found = str(slugger(niche, location) or "").strip()
        except Exception:  # noqa: BLE001 - fall through to the copy below.
            log.exception("the job service could not name the search")
        else:
            if found:
                return found
    slug = re.sub(r"[^a-z0-9]+", "-", f"{niche}--{location}".lower()).strip("-")
    return slug[:80]
