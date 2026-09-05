"""REST routes for placing the call itself.

Everything here is mounted under ``/api/call`` and every JSON shape is frozen by
the calling contract, section 2.1. One rule shapes the whole module: be honest.
A provider that cannot place a call says so, names the exact environment
variables it still needs, and shows what it costs, all of that before the rep
presses anything. Nobody should learn that Twilio charges money by being charged.

So there are two kinds of answer here and they have the same shape on the wire.
A refusal (no credentials, a bad number, no public address, a call that is
already running) comes back as the very same body as a success, only with ``ok``
false and a ``message`` written in plain words. The frontend renders ``message``
either way and never has to guess from a status code. That is why the 400 and
the 404 below are built by hand instead of raised as an ``HTTPException``, which
would answer ``{"detail": ...}`` and lose the one field the UI actually shows.

Nothing in this module talks to a provider's network API itself.
``app.telephony`` owns the provider table and the number rules,
``app.services.twilio_client`` owns Twilio, and ``app.services.whatsapp_client``
owns both WhatsApp paths. This layer picks one, records the result on the
session, and pushes the new call state down the teleprompter socket so the call
page updates without polling.

MONEY IS THE THING TO GET RIGHT
-------------------------------
Two of the four providers bill per call, so this layer holds three guards that
exist only because of that:

* One call at a time per session. Starting a second call would overwrite the
  first call's id on the session, and that id is the only handle the hangup
  route has, so the first call would ring on and bill on with no way to stop it
  from the app.
* A cap on how many calls one caller may start in a minute. This endpoint has no
  login, and Twilio needs this whole app published on a public tunnel, so the
  cap is what stands between a stranger who found that address and the rep's
  Twilio bill.
* WhatsApp from the app is switched off at the door. The Cloud API needs a
  WebRTC offer this Python server cannot make, so that button is never armed.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, cast

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app import telephony
from app.api.routes_ws import notify_call_state
from app.config import public_wss_base
from app.models import (
    CallProvidersResponse,
    CallStartRequest,
    CallStartResponse,
    CallStatusResponse,
)
from app.services import twilio_client, whatsapp_client
from app.services.session_store import CallProvider, CallState, Session, store
from app.services.twilio_client import TwilioError
from app.services.whatsapp_client import WhatsAppError

log = logging.getLogger("salescopilot.api.call")

router = APIRouter(prefix="/api/call", tags=["call"])


# ====================================================================== #
# copy
#
# Every string below is read by a rep who is seconds away from talking to a
# stranger, and English is not their first language. Short words, short
# sentences, no jargon, and never a promise the app cannot keep.
# ====================================================================== #

MANUAL_MESSAGE: str = (
    "Call the client from your own phone now. Then press SOURCES up here and "
    "pick what the app should listen to, so it can hear them."
)
"""What the rep reads after picking the manual provider.

It names the SOURCES button on purpose. Sharing the sound is the one step this
whole feature needs from the rep, and a sentence that only says "share the
audio" sends them looking for a menu that is not there. SOURCES is the label
printed on the button, so that is the word used here.
"""

WHATSAPP_LINK_MESSAGE: str = (
    "WhatsApp is open with the number ready. Press call there. Then come back "
    "here, press SOURCES, and share the sound of that tab so the app can hear "
    "the client."
)
"""What the rep reads after picking WhatsApp link mode.

Same reason as :data:`MANUAL_MESSAGE` for naming SOURCES, and the steps are in
the order the rep does them, because this one message is the whole instruction
for the path that always works.
"""

WHATSAPP_CLOUD_MESSAGE: str = (
    "The app is calling the client on WhatsApp now. Wait for them to pick up."
)
"""What the rep would read after the Cloud API accepted the call.

Kept because the Cloud path is scaffolded and this is the sentence it will use
once a WebRTC leg exists. Today :data:`WHATSAPP_CLOUD_UNFINISHED` is what the
rep actually sees.
"""

WHATSAPP_CLOUD_UNFINISHED: str = (
    "This one is not finished yet. The app cannot make a WhatsApp call on its "
    "own. Use WhatsApp from my phone instead."
)
"""Why WhatsApp from the app is switched off, in words a rep can act on.

A real Cloud API call carries a WebRTC offer, and this server has no WebRTC
stack, so Meta refuses every call this app could send. Two environment
variables are therefore not enough to switch this on, and letting them arm the
button would sell the rep a Meta business number that still cannot place a
call. See the honest note in ``app.services.whatsapp_client``.
"""

TWILIO_MESSAGE: str = "Your phone is ringing now. Pick it up and we will dial the client."
"""What the rep reads after Twilio accepted the call. The rep's phone rings first."""

TWILIO_DIALING_DETAIL: str = "Calling your phone."
"""Detail held while the Twilio request is still in the air."""

HANGUP_MESSAGE: str = "The call is ended."
"""Detail stored on the session after a hangup."""

UNKNOWN_SESSION_MESSAGE: str = (
    "That call is unknown or has expired. Build the call context again."
)
"""Answer for a session id that is not in the store any more."""

ALREADY_RUNNING_MESSAGE: str = (
    "A call is already running. End it first, then start a new one."
)
"""Answer when this session still has a call in flight.

The picker in the browser hides the start button while a call is up, but that
guard lives in one page. A reload, a second tab or a client that retries a slow
request all get past it, and each of those would place a second billed call.
"""

TOO_MANY_STARTS_MESSAGE: str = (
    "Too many calls were started just now. Wait one minute, then try again."
)
"""Answer when the start cap is hit. Plain, and it says what to do."""

NO_NUMBER_MESSAGE: str = (
    "Write the client number with the country code, like +923001234567."
)
"""Answer when the client number is missing for a provider that needs one."""

COUNTRY_CODE_HINT: str = "Always write the country code first."
"""Added to any number refusal that does not already say it.

``telephony.normalise_e164`` writes a more exact reason for each way a number can
be wrong, and some of those, for example "Use only numbers", do not mention the
country code at all. Missing the country code is the mistake reps actually make,
so every refusal from this route ends up saying the one rule that fixes it.
"""

NO_REP_NUMBER_MESSAGE: str = (
    "Write your own phone number with the country code, like +923009876543. "
    "We ring your phone first, then we dial the client."
)
"""Answer when the rep's own number is missing for a Twilio call."""

NO_PUBLIC_ADDRESS_MESSAGE: str = (
    "Twilio cannot reach this app yet. It needs a public address. Run ngrok, "
    "then put that https address in PUBLIC_BASE_URL in backend/.env and start "
    "the backend again."
)
"""Answer when the app is only reachable from this computer."""

TWILIO_GENERIC: str = "The call did not start. Try again in a moment."
"""Used only when a Twilio failure carries no message of its own, which is rare."""

NOT_READY_PLAIN: str = "That way of calling is not set up yet."
"""Opening words when a provider has no credentials."""

START_STATE: CallState = "dialing"
"""Where every provider lands the moment a call starts.

Twilio moves on from here by itself through its status callbacks. Manual and
WhatsApp never do, because the app cannot see that phone ring, and showing
``live`` for a call we cannot hear would be a lie on the screen.
"""

BUSY_STATES: frozenset[str] = frozenset({"dialing", "ringing", "live"})
"""The states that mean a call is still up, so a new one must not start.

Exactly the three states the picker in the browser calls "active", so the route
and the screen agree on when the start button is dead. ``ended`` and ``failed``
are both over, and ``idle`` never started, so all three of those are free to
start a fresh call.
"""

UNFINISHED_PROVIDERS: dict[str, str] = {"whatsapp_cloud": WHATSAPP_CLOUD_UNFINISHED}
"""Providers that cannot work at all yet, mapped to the reason the rep reads.

This is a gate, not a setting. A provider listed here is reported as not ready
no matter what is in ``backend/.env``, because the thing it is missing is code,
not credentials. It belongs in ``app.telephony`` the day that code exists, and
it lives here meanwhile because this module answers both the picker and the
start, which are the only two places the gate has to hold.
"""

NUMBERS_USED: dict[str, tuple[bool, bool]] = {
    "manual": (False, False),
    "whatsapp_link": (True, False),
    "whatsapp_cloud": (True, False),
    "twilio": (True, True),
}
"""Which numbers each provider really dials, as (the client, the rep).

Only a number in this table is read, checked or stored, and that matters more
than it looks. The call page sends both fields on every start: the client number
is filled in from the setup page and the rep's own number is remembered in the
browser from the last time Twilio was picked. Checking a number the provider
never dials would let a half typed number, left behind in a field that is not
even on screen, block the two providers that are free and always work.
"""


# ====================================================================== #
# start rate cap
#
# This endpoint has no login. Sessions are handed out by /api/prepare-context,
# which has no login either, so "the session exists" proves nothing about who is
# asking. Twilio also needs this whole app on a public tunnel, so while a call
# is possible the start endpoint is reachable from the internet by anyone who
# knows that address. Two of the four providers bill per call.
#
# The cap below is a small in process brake, not authentication. It cannot tell
# a stranger from the rep, it only stops either of them from starting calls in a
# loop. A shared secret header between the Next proxy and this API is the real
# fix, and it needs both sides changed at once.
# ====================================================================== #

RATE_WINDOW_S: float = 60.0
"""How far back the cap looks, in seconds."""

MAX_STARTS_PER_SESSION: int = 5
"""How many calls one session may start inside the window.

A rep starts one call and talks for minutes. Five in a minute is already far
past normal use, so the cap is generous to a human and still tight on a loop.
"""

MAX_PAID_STARTS: int = 10
"""How many billed calls this whole process may start inside the window.

Per session is not enough on its own, because a new session costs an attacker
one unauthenticated POST. This second cap is counted across every session, so
starting a fresh session for each call does not get around it.
"""

PAID_PROVIDERS: frozenset[str] = frozenset({"twilio", "whatsapp_cloud"})
"""The providers that cost money, and so the ones the process wide cap counts."""

PAID_BUCKET: str = "*paid*"
"""Key for the process wide bucket. Not a session id, those are uuids."""

_STARTS: dict[str, deque[float]] = {}
"""When each bucket last started calls, as monotonic seconds, oldest first.

Process wide and not locked. The event loop is single threaded, and nothing
between the read and the write below awaits, so two requests cannot interleave
inside :func:`_too_many_starts`.
"""


def _too_many_starts(session_id: str, provider: str) -> bool:
    """Count this start against the caps, and say whether it is over them.

    Args:
        session_id: The session asking for a call.
        provider: The provider key, already known to be one of the four.

    Returns:
        True when a cap is already full, in which case nothing is counted and
        the caller must refuse. False when the start is allowed, and then it has
        been recorded.
    """
    now = time.monotonic()
    cutoff = now - RATE_WINDOW_S

    # Drop everything older than the window, from every bucket, so the table
    # stays the size of one minute of traffic rather than growing per session.
    for key in list(_STARTS):
        hits = _STARTS[key]
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            del _STARTS[key]

    buckets: list[tuple[str, int]] = [(session_id, MAX_STARTS_PER_SESSION)]
    if provider in PAID_PROVIDERS:
        buckets.append((PAID_BUCKET, MAX_PAID_STARTS))

    for key, cap in buckets:
        if len(_STARTS.get(key, ())) >= cap:
            return True

    for key, _cap in buckets:
        _STARTS.setdefault(key, deque()).append(now)
    return False


# ====================================================================== #
# small helpers
# ====================================================================== #


def _join_words(names: list[str]) -> str:
    """Join names into a readable list, with "and" before the last one.

    Args:
        names: The words to join, already in display order.

    Returns:
        ``"A"``, ``"A and B"`` or ``"A, B and C"``. Empty input gives ``""``.
    """
    cleaned = [str(name).strip() for name in names if str(name).strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    return f"{', '.join(cleaned[:-1])} and {cleaned[-1]}"


def _unfinished_reason(provider: str) -> str:
    """Say why a provider cannot work at all yet, or return an empty string.

    Args:
        provider: The provider key the rep picked.

    Returns:
        One plain sentence for a provider in :data:`UNFINISHED_PROVIDERS`, and
        ``""`` for every other one, which means nothing is blocking it here.
    """
    return UNFINISHED_PROVIDERS.get((provider or "").strip().lower(), "")


def _not_ready_message(provider: str) -> str:
    """Say what a provider still needs, in words the rep can act on.

    Args:
        provider: The provider key the rep picked.

    Returns:
        One plain sentence naming the exact environment variables to add, or the
        reason this provider cannot work at all when adding keys would not help.
    """
    unfinished = _unfinished_reason(provider)
    if unfinished:
        return unfinished
    joined = _join_words(telephony.missing_settings(provider))
    if not joined:
        return NOT_READY_PLAIN
    return (
        f"{NOT_READY_PLAIN} Add {joined} to backend/.env, then start the backend again."
    )


def _plain_twilio_error(exc: TwilioError) -> str:
    """Return the sentence the rep should see for a Twilio failure.

    The translation table lives in ``twilio_client.FRIENDLY_ERRORS``, which is
    where the error code is actually known, so this route does not keep a second
    copy of it. The four codes that really happen (20003 bad login, 21210 a from
    number the account does not own, 21219 and 21608 an unverified number on a
    trial account) are already plain English by the time they arrive here.

    Args:
        exc: The error the Twilio client raised.

    Returns:
        A short message in plain English, never empty.
    """
    message = str(getattr(exc, "message", "") or "").strip()
    return message or TWILIO_GENERIC


def _number_problem(exc: ValueError) -> str:
    """Turn a refused number into one line that always says how to fix it.

    Args:
        exc: The error raised by ``normalise_e164`` or by :func:`_clean_number`.

    Returns:
        The reason, with :data:`COUNTRY_CODE_HINT` added when the reason does not
        already talk about the country code.
    """
    reason = str(exc).strip() or NO_NUMBER_MESSAGE
    if "country code" in reason.lower():
        return reason
    return f"{reason} {COUNTRY_CODE_HINT}"


def _refuse(status: int, provider: str, message: str) -> JSONResponse:
    """Build a failure that has the very same shape as a success.

    Args:
        status: The HTTP status to answer with, 400 or 404.
        provider: The provider the rep asked for, echoed back.
        message: One plain line saying what went wrong and what to do.

    Returns:
        A JSON response carrying ``ok`` false plus the four other fields, so the
        frontend reads ``message`` without branching on the status code.
    """
    body = CallStartResponse(
        ok=False,
        provider=provider,
        call_id=None,
        open_url=None,
        message=message,
    )
    return JSONResponse(status_code=status, content=body.model_dump(by_alias=True))


def _accept(
    provider: str,
    message: str,
    *,
    call_id: str | None = None,
    open_url: str | None = None,
) -> CallStartResponse:
    """Build the success body for a started call.

    Args:
        provider: The provider that was used.
        message: One plain line telling the rep what happens next.
        call_id: The provider's own call id, when it gave us one.
        open_url: The link the browser must open, link mode only.

    Returns:
        The response model, serialized by FastAPI with camelCase aliases.
    """
    return CallStartResponse(
        ok=True,
        provider=provider,
        call_id=call_id,
        open_url=open_url,
        message=message,
    )


async def _announce(session: Session) -> None:
    """Push this session's call state down to the browser, if one is watching.

    ``notify_call_state`` is already a quiet no op when no teleprompter socket
    is open, and it never raises, so no call site here has to guard it.

    Args:
        session: The session whose call state just changed.
    """
    await notify_call_state(session.id, session.call_snapshot())


def _clean_number(raw: str | None, *, missing_message: str) -> str:
    """Validate one phone number this provider is really going to dial.

    Only called for a number the chosen provider uses, see :data:`NUMBERS_USED`.
    A number the provider ignores is never passed through here, because refusing
    a call over a field the rep cannot even see is the worst kind of dead end.

    Args:
        raw: The number as the rep typed it, already stripped by the request
            model, or ``None``.
        missing_message: What to say when it is absent.

    Returns:
        The number in E.164.

    Raises:
        ValueError: With a plain message, either because the number is missing,
            or because ``normalise_e164`` refused it. The caller turns that
            message straight into the body of the 400.
    """
    if not raw:
        raise ValueError(missing_message)
    return telephony.normalise_e164(raw)


# ====================================================================== #
# routes
# ====================================================================== #


@router.get("/providers", response_model=CallProvidersResponse)
async def get_providers() -> CallProvidersResponse:
    """List the four ways of placing a call, with their cost and their gaps.

    Readiness is computed from the environment on every request and never
    cached, so adding a key to ``backend/.env`` and restarting is enough to make
    a provider live without touching any code. The one exception is a provider
    in :data:`UNFINISHED_PROVIDERS`, which is forced to not ready here whatever
    the environment says, because what it is missing is code and no key can fill
    that gap. The browser draws a not ready provider as a disabled row, so this
    is what keeps the contract's hardest rule true: never show a call button
    that cannot place a call.

    Returns:
        The four providers in display order. A provider that cannot place a call
        is still listed, with ``ready`` false and the exact environment variable
        names it needs, because the rep has to see that the option exists and
        what it would take to switch it on.
    """
    entries = telephony.provider_status()
    for entry in entries:
        if _unfinished_reason(str(entry.get("key", ""))):
            entry["ready"] = False
    return CallProvidersResponse.from_entries(entries)


@router.post("/start", response_model=CallStartResponse)
async def post_call_start(payload: CallStartRequest) -> Any:
    """Start the call the rep asked for, or say plainly why it cannot start.

    The checks run in the order things go wrong: an expired session, a call that
    is already running, a provider that cannot work, a missing public address, a
    number that is not a number, and finally the start cap. Nothing reaches a
    provider until every one of them passes, so the rep never waits on a network
    call that was never going to work, and no second call is ever billed while
    the first one is still up.

    Args:
        payload: The validated request body. The provider word is already known
            to be one of the four, pydantic refuses anything else.

    Returns:
        ``CallStartResponse`` with ``ok`` true on a started call, or the very
        same shape with ``ok`` false and a 400 or 404 status when it could not
        start. Both carry a ``message`` written for the rep.
    """
    provider: str = payload.provider
    session = store.get(payload.session_id)
    if session is None:
        return _refuse(404, provider, UNKNOWN_SESSION_MESSAGE)

    # One call at a time, and this is the guard that keeps money safe. Every
    # start writes the new call id over the old one, and the hangup route can
    # only end the id it finds on the session, so a second start would leave the
    # first call ringing with no way to stop it from the app. The picker hides
    # the start button while a call is up, but that lives in one page, and a
    # reload, a second tab or a retried request all walk straight past it.
    if session.call_state in BUSY_STATES:
        return _refuse(400, provider, ALREADY_RUNNING_MESSAGE)

    unfinished = _unfinished_reason(provider)
    if unfinished:
        return _refuse(400, provider, unfinished)

    if not telephony.provider_ready(provider):
        return _refuse(400, provider, _not_ready_message(provider))

    # Checked here, with the other pre flight checks, rather than inside the
    # Twilio branch. Everything from the assignment below onwards counts as an
    # attempt and writes call state, and a rep who is already on a manual call
    # must not have that call marked failed because they looked at Twilio.
    # ``public_wss_base`` answers empty both for a missing address and for one
    # pointing at localhost, so this one check covers both.
    if provider == "twilio" and not public_wss_base():
        return _refuse(400, provider, NO_PUBLIC_ADDRESS_MESSAGE)

    # Only a number this provider actually dials is read at all. Manual dials
    # nothing, WhatsApp needs the client and never the rep, Twilio needs both.
    wants_client, wants_rep = NUMBERS_USED.get(provider, (True, True))
    try:
        to_number = (
            _clean_number(payload.to_number, missing_message=NO_NUMBER_MESSAGE)
            if wants_client
            else None
        )
        rep_number = (
            _clean_number(payload.rep_number, missing_message=NO_REP_NUMBER_MESSAGE)
            if wants_rep
            else None
        )
    except ValueError as exc:
        return _refuse(400, provider, _number_problem(exc))

    # Counted last, so a typed number that gets refused never uses up a start.
    if _too_many_starts(session.id, provider):
        log.warning("session %s hit the start cap for provider %s", session.id, provider)
        return _refuse(400, provider, TOO_MANY_STARTS_MESSAGE)

    # The request model already refused anything outside the four known words,
    # so the cast only tells the type checker what pydantic has proved.
    session.call_provider = cast(CallProvider, provider)
    session.to_number = to_number
    session.rep_number = rep_number

    if provider == "manual":
        return await _start_manual(session)
    if provider == "whatsapp_link":
        return await _start_whatsapp_link(session, to_number)
    if provider == "whatsapp_cloud":
        return await _start_whatsapp_cloud(session, to_number)
    return await _start_twilio(session, to_number, rep_number)


async def _start_manual(session: Session) -> CallStartResponse:
    """Record a call the rep places on their own phone.

    Nothing is dialled here and nothing can be. The app cannot see that phone
    ring, so the state stays at ``dialing`` until the rep ends the call in the
    app, and the message says out loud that the audio still has to be shared.

    Args:
        session: The session this call belongs to.

    Returns:
        The success body for the manual provider.
    """
    session.set_call_state(START_STATE, call_id="", detail=MANUAL_MESSAGE)
    await _announce(session)
    log.info("session %s starts a manual call", session.id)
    return _accept("manual", MANUAL_MESSAGE)


async def _start_whatsapp_link(session: Session, to_number: str | None) -> Any:
    """Build the WhatsApp link the browser opens in a new tab.

    This is the path that always works. It costs nothing, needs no approval, and
    the call is placed by the rep's own WhatsApp account, so the audio arrives
    the way it already does, from the shared tab.

    Args:
        session: The session this call belongs to.
        to_number: The client number in E.164.

    Returns:
        The success body carrying ``openUrl``, or a 400 with a plain message
        when the number carries no digits at all.
    """
    try:
        open_url = whatsapp_client.call_link(to_number or "")
    except ValueError as exc:
        return _refuse(400, "whatsapp_link", str(exc))

    session.set_call_state(START_STATE, call_id="", detail=WHATSAPP_LINK_MESSAGE)
    await _announce(session)
    log.info("session %s opens a WhatsApp link", session.id)
    return _accept("whatsapp_link", WHATSAPP_LINK_MESSAGE, open_url=open_url)


async def _start_whatsapp_cloud(session: Session, to_number: str | None) -> Any:
    """Ask the WhatsApp Cloud API to place the call from the business number.

    This path is written and it is not finished. A real Cloud API call carries a
    WebRTC offer, this server has no WebRTC stack, and Meta refuses a connect
    without one. So the refusal happens here, before any request is built, and
    it says that in plain words. Sending the request instead would spend the
    rep's Meta number on a call that answers 400 every single time.

    The rest of the function is the shape the call takes the day that offer
    exists, and ``whatsapp_client.cloud_call`` already accepts it.

    Args:
        session: The session this call belongs to.
        to_number: The client number in E.164.

    Returns:
        The success body carrying the Cloud API call id, or a 400 with a plain
        message. Today it is always the 400.
    """
    unfinished = _unfinished_reason("whatsapp_cloud")
    if unfinished:
        # A pre flight refusal, so no call state is written. Marking this
        # session failed would wipe the line the rep is reading about the call
        # they are actually on.
        log.info("session %s asked for whatsapp cloud, which is not finished", session.id)
        return _refuse(400, "whatsapp_cloud", unfinished)

    try:
        call_id = await whatsapp_client.cloud_call(
            to_number=to_number or "",
            session_id=session.id,
        )
    except WhatsAppError as exc:
        message = str(getattr(exc, "message", "") or exc).strip()
        log.warning("whatsapp cloud refused the call for session %s: %s", session.id, exc)
        session.set_call_state("failed", call_id="", detail=message)
        await _announce(session)
        return _refuse(400, "whatsapp_cloud", message)

    session.set_call_state(START_STATE, call_id=call_id or "", detail=WHATSAPP_CLOUD_MESSAGE)
    await _announce(session)
    log.info("session %s placed a WhatsApp cloud call, id=%s", session.id, call_id or "none")
    return _accept("whatsapp_cloud", WHATSAPP_CLOUD_MESSAGE, call_id=call_id or None)


async def _start_twilio(
    session: Session,
    to_number: str | None,
    rep_number: str | None,
) -> Any:
    """Ring the rep's phone, then let Twilio dial the client and fork the audio.

    Twilio calls our webhook back from the public internet a second or two after
    it accepts the call, and that webhook builds its TwiML from the numbers
    stored on the session. So the session is written BEFORE the call is placed,
    not after. Written after, the webhook could arrive first and find no number
    to dial.

    The caller has already checked that this app has a public address, and that
    no other call is running on this session, because both of those must happen
    before any call state is written.

    Args:
        session: The session this call belongs to.
        to_number: The client number in E.164.
        rep_number: The rep's own phone in E.164, which rings first.

    Returns:
        The success body carrying the Twilio call sid, or a 400 with a plain
        message when Twilio refused.
    """
    token = twilio_client.webhook_token(session.id)
    session.set_call_state(START_STATE, call_id="", detail=TWILIO_DIALING_DETAIL)
    await _announce(session)

    try:
        call_sid = await twilio_client.place_call(
            session_id=session.id,
            to_number=to_number or "",
            rep_number=rep_number or "",
            token=token,
        )
    except TwilioError as exc:
        message = _plain_twilio_error(exc)
        log.warning("twilio refused the call for session %s: %s", session.id, exc)
        session.set_call_state("failed", call_id="", detail=message)
        await _announce(session)
        return _refuse(400, "twilio", message)

    if session.call_state == START_STATE:
        session.set_call_state(START_STATE, call_id=call_sid or "", detail=TWILIO_MESSAGE)
    else:
        # Twilio's status callbacks are asynchronous and one of them can land
        # while the request above is still in the air, so by now the call may
        # already be ringing, or over. That callback knows more than this line
        # does, so its state and its detail are left alone and only the sid is
        # written, which is the one thing this side of the call owns and the
        # only handle the hangup route has.
        session.set_call_state(session.call_state, call_id=call_sid or "")
    await _announce(session)
    log.info("session %s placed a twilio call, sid=%s", session.id, call_sid or "none")
    return _accept("twilio", TWILIO_MESSAGE, call_id=call_sid or None)


@router.get("/{session_id}/status", response_model=CallStatusResponse)
async def get_call_status(session_id: str) -> CallStatusResponse:
    """Report where the phone call is right now.

    The same four fields are pushed down the teleprompter socket as a
    ``call_state`` frame, so this route is the fallback for a browser that just
    reconnected, not the normal way the screen stays up to date.

    Args:
        session_id: The session the call belongs to.

    Returns:
        The provider, the state, the provider's call id and one plain detail
        line.

    Raises:
        HTTPException: 404 when the session is unknown or has expired.
    """
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=UNKNOWN_SESSION_MESSAGE)
    return CallStatusResponse.model_validate(session.call_snapshot())


@router.post("/{session_id}/hangup")
async def post_call_hangup(session_id: str) -> dict[str, bool]:
    """End the call, and never complain about ending one that is already over.

    Hang up is pressed by someone who wants the call to stop, sometimes twice,
    sometimes after the other side already hung up, sometimes on a session this
    server has already forgotten. All of those answer ``ok`` true, because in
    every one of them the call is not running, which is what was asked for.

    Only Twilio is actually told anything. A manual or WhatsApp call lives on
    the rep's own phone and the app cannot reach it, so ending it here only
    clears the state on the screen.

    Args:
        session_id: The session the call belongs to.

    Returns:
        ``{"ok": true}``, always.
    """
    session = store.get(session_id)
    if session is None:
        return {"ok": True}

    call_id = session.call_id
    if session.call_provider == "twilio" and call_id:
        try:
            await twilio_client.hangup(call_id)
        except TwilioError as exc:
            # Very often the call is already over by the time this runs. The rep
            # asked for it to stop and it is stopped, so this is logged and the
            # state moves on regardless.
            log.info("twilio hangup for session %s was refused: %s", session.id, exc)
        except Exception:  # noqa: BLE001 - a hangup must never fail on the rep.
            log.exception("twilio hangup failed for session %s", session.id)

    session.set_call_state("ended", call_id=None, detail=HANGUP_MESSAGE)
    await _announce(session)
    log.info("session %s ended its call", session.id)
    return {"ok": True}


__all__ = ["router"]
