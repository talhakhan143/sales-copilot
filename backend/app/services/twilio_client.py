"""Twilio Voice client: ring the rep, bridge the client, fork the audio to us.

This module owns every outbound call to the Twilio REST API. It is deliberately
small: originate a call, hang a call up, and build the TwiML that Twilio fetches
once the rep picks up. Everything else (routes, session state, audio) lives
elsewhere.

WHY WE RING THE REP FIRST
-------------------------
The obvious design is to dial the client and then bring the rep in. It is the
wrong one. Setting up a call takes a second or two, and in that gap the client
would be holding a live line with nothing on it. Dead air on the first second of
a cold call is how you get hung up on.

So the order is: Twilio rings the REP's own phone. The rep picks up, hears one
short line, and only then does the TwiML dial the client. The client's phone
starts ringing with a human already on the line. As a side effect the media
stream is open before the client says a word, so we never miss the first hello.

TRACK DIRECTION, the easy thing to invert
-----------------------------------------
The call leg Twilio streams to us is the REP's leg. From Twilio's point of view
``inbound`` is audio coming FROM that leg (the rep talking) and ``outbound`` is
audio being played TO it (the client talking). The socket layer maps ``outbound``
to our client lane and ``inbound`` to our rep lane. The mapping itself lives in
the socket route, this note is here because the two files have to agree.

WHAT COSTS MONEY
----------------
Every call placed through here is billed by Twilio per minute, plus the monthly
rent on the from number. A trial account can only dial numbers that have been
verified in the console. None of this could be tested end to end on this machine,
because that needs a paid account, a rented number and a public tunnel. What is
tested locally is everything that does not need the network: the TwiML building
and escaping, and the webhook token.

HTTP transport
--------------
One shared :class:`httpx.AsyncClient` is created lazily and closed on shutdown,
the same shape :class:`app.services.groq_client.GroqClient` uses, so connections
are pooled for the whole process instead of being rebuilt per call. The lifespan
in ``app.main`` should call :func:`shutdown` on the way out.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from hashlib import sha256
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

import httpx

from app.config import public_http_base, settings

logger = logging.getLogger(__name__)

__all__ = [
    "TwilioError",
    "FRIENDLY_ERRORS",
    "TOKEN_CHARS",
    "startup",
    "shutdown",
    "place_call",
    "hangup",
    "build_twiml",
    "friendly_message",
    "webhook_token",
    "verify_token",
]

# ===========================================================================
# Constants
# ===========================================================================

#: Root of the Twilio REST API. The account path is appended per request.
TWILIO_API_ROOT: Final[str] = "https://api.twilio.com"

#: How long Twilio lets the rep's phone ring before it gives up, in seconds.
#: The same number is used again inside the TwiML for the client's phone.
RING_TIMEOUT_S: Final[int] = 30

#: Length of the webhook token kept from the hex digest. 32 hex characters is
#: 128 bits, which is far more than enough for a URL that lives for one call.
TOKEN_CHARS: Final[int] = 32

#: Call state events we want pushed back to the status webhook. Twilio takes
#: this parameter once per value, not as one space separated string, so it is
#: sent as four repeated form fields.
STATUS_EVENTS: Final[tuple[str, ...]] = ("initiated", "ringing", "answered", "completed")

#: The Twilio error codes a real user actually hits, in plain English. Anything
#: else falls through to :func:`friendly_message` and keeps Twilio's own text.
FRIENDLY_ERRORS: Final[dict[int, str]] = {
    20003: (
        "Twilio did not accept the login. Check TWILIO_ACCOUNT_SID and "
        "TWILIO_AUTH_TOKEN in backend/.env, then start the server again."
    ),
    21210: (
        "Twilio does not own the number you are calling from. Buy a number in "
        "the Twilio console, then put that number in TWILIO_FROM_NUMBER in "
        "backend/.env."
    ),
    21219: (
        "This is a Twilio trial account. It can only call numbers you have "
        "added to the verified list. Add this number in the Twilio console, "
        "or upgrade the account."
    ),
    21608: (
        "This is a Twilio trial account. It can only call numbers you have "
        "added to the verified list. Add this number in the Twilio console, "
        "or upgrade the account."
    ),
}

#: First ingredient of the key used when ``CALL_WEBHOOK_SECRET`` is empty. It
#: only keeps this key apart from any other key built the same way. See
#: :func:`_fallback_secret` for why the fallback is worked out instead of being
#: drawn at random.
_FALLBACK_DOMAIN: Final[str] = "sales-copilot twilio webhook token v1"

#: Timeouts for the REST calls. Twilio answers a call creation in well under a
#: second when it is healthy, so a long read timeout only hides a problem.
_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(connect=5.0, read=20.0, write=15.0, pool=5.0)

_client: httpx.AsyncClient | None = None
_lock: Final[asyncio.Lock] = asyncio.Lock()


# ===========================================================================
# Errors
# ===========================================================================


class TwilioError(RuntimeError):
    """Raised when Twilio refuses a request, or when we refuse to send one.

    Attributes:
        status: HTTP status code returned by Twilio. ``0`` means the request was
            never sent, either because the config is incomplete or because the
            transport failed before a response arrived.
        message: Plain English detail, safe to show to the rep in the UI.
        code: Twilio's own numeric error code, ``0`` when there is none. Kept so
            the logs carry the exact code even though the rep sees plain words.
        raw_message: Twilio's original English text, for the log.
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: int = 0,
        raw_message: str = "",
    ) -> None:
        """Store the parts and build the display string.

        Args:
            status: HTTP status code, or ``0`` when nothing was sent.
            message: Plain English detail for the user.
            code: Twilio's numeric error code when the body carried one.
            raw_message: Twilio's own wording, kept for the log.
        """
        self.status = status
        self.message = message
        self.code = code
        self.raw_message = raw_message or message
        label = f"HTTP {status}" if status else "not sent"
        suffix = f" (twilio code {code})" if code else ""
        super().__init__(f"Twilio {label}{suffix}: {message}")


# ===========================================================================
# Pure helpers (unit testable without a network)
# ===========================================================================


def _short(text: str, limit: int = 300) -> str:
    """Collapse a body to one short line for an error message or a log.

    Args:
        text: Raw text, possibly multi line and possibly long.
        limit: Maximum number of characters kept.

    Returns:
        A single line, length capped string.
    """
    flat = " ".join(text.split())
    if len(flat) > limit:
        return flat[:limit] + " [...]"
    return flat


def friendly_message(code: int, raw: str) -> str:
    """Turn a Twilio error into words a non technical rep can act on.

    Only the codes that really happen are translated. Anything else keeps
    Twilio's own sentence, wrapped in one line of context, because a wrong
    guess is worse than an honest quote.

    Args:
        code: Twilio's numeric error code, ``0`` when the body carried none.
        raw: Twilio's own message text.

    Returns:
        A plain English sentence for the UI.
    """
    known = FRIENDLY_ERRORS.get(code)
    if known:
        return known
    detail = _short(raw)
    if not detail:
        return "Twilio could not start the call, and it did not say why."
    return f"Twilio could not start the call. It said: {detail}"


def build_twiml(*, session_id: str, to_number: str, wss_url: str) -> str:
    """Build the TwiML Twilio fetches once the rep answers.

    The document does three things, and the order of the first two is the whole
    point of this function:

    1. ``<Say>`` gives the rep one short line so they know the app heard them.
    2. ``<Start><Stream>`` forks both sides of the audio to our WebSocket and
       carries the session id along as a custom parameter, so the socket knows
       which session the frames belong to.
    3. ``<Dial><Number>`` rings the client and bridges the two legs.

    WHY THE LINE IS SPOKEN BEFORE THE FORK STARTS
    ---------------------------------------------
    The leg being forked is the rep's phone. On that leg ``outbound`` is
    everything the rep HEARS, and the socket layer reads that track as the
    client lane, which is right once the two legs are joined. Twilio's own
    voice reading ``<Say>`` is played to the rep, so it lands on that same
    track. With the fork started first, that one sentence was cut into an
    utterance, sent to Whisper, printed in the client column, kept in the call
    history for the rest of the call, and answered by the copilot before the
    client had picked the phone up. Starting the fork after the line is spoken
    keeps it out of the audio for good and costs nothing, because the client
    has not been dialled yet and there is nothing to miss.

    THE PART THIS ORDER DOES NOT FIX
    --------------------------------
    While ``<Dial>`` is ringing, Twilio plays the ringing tone to the rep, and
    that tone is on the same track. Only the media socket can drop it, because
    only the media socket can be told when the two legs were joined. Until it
    is, a client column can still pick up a stray line while the client's phone
    is ringing. That is a smaller problem than a whole spoken sentence, and it
    is not one TwiML can solve: there is no way to say "start the fork when the
    other side answers" in this document.

    Every interpolated value is escaped. The phone number and the session id
    both come from the request body, so they are user input going straight into
    XML. ``quoteattr`` is used for attributes because it picks the quoting for
    us, and ``escape`` for the element text.

    Args:
        session_id: The copilot session id. It is passed to the media socket as
            the ``sessionId`` stream parameter.
        to_number: The client's number in E.164, for example ``+923001234567``.
        wss_url: Full WebSocket URL Twilio should connect to, for example
            ``wss://abc123.ngrok-free.app/ws/twilio``.

    Returns:
        The complete TwiML document as one string, ready to return with the
        ``application/xml`` content type.
    """
    caller_id = settings.twilio_from_number.strip()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        # Spoken first, on purpose. Read the note above before moving it.
        '<Say voice="alice">Connecting your call now.</Say>'
        "<Start>"
        f"<Stream url={quoteattr(wss_url)} track=\"both_tracks\">"
        f"<Parameter name=\"sessionId\" value={quoteattr(session_id)}/>"
        "</Stream>"
        "</Start>"
        f"<Dial timeout=\"{RING_TIMEOUT_S}\" callerId={quoteattr(caller_id)}>"
        f"<Number>{escape(to_number)}</Number>"
        "</Dial>"
        "</Response>"
    )


# ---------------------------------------------------------------------------
# Webhook token
# ---------------------------------------------------------------------------


def _fallback_secret() -> str:
    """Build the HMAC key used when ``CALL_WEBHOOK_SECRET`` is empty.

    This used to be one random number drawn at import time. That looked safe
    and broke the one case it had to survive. The dev server runs with
    ``--reload``, so saving any file while the rep's phone is ringing starts a
    new process with a new random key. Every webhook Twilio then sent carried a
    token this process no longer knew, so the voice webhook answered 403,
    Twilio read its own robot apology into the rep's ear instead of our polite
    line, and the state chip sat on DIALING until somebody reloaded the page.

    So the key is worked out instead of drawn. Every ingredient is a value this
    machine still has after a restart, and none of them is public:

    * a fixed word, so this key is not the same as any other key we build,
    * the full path of this file, which carries the user name on this machine
      and the folder the project sits in,
    * the Twilio account id and auth token, which are real secrets whenever
      calling is actually switched on.

    It is still second best. A key built from things a machine knows is only as
    hard to guess as those things, so put a real secret in
    ``CALL_WEBHOOK_SECRET`` before the server is open to anyone else, and
    :func:`startup` says so in the log.

    Returns:
        A 64 character hex key, the same one on every run on this machine with
        this configuration.
    """
    parts = (
        _FALLBACK_DOMAIN,
        str(Path(__file__).resolve()),
        settings.twilio_account_sid.strip(),
        settings.twilio_auth_token.strip(),
    )
    return sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _webhook_secret() -> str:
    """Return the HMAC key for the webhook token.

    Falls back to :func:`_fallback_secret` when ``CALL_WEBHOOK_SECRET`` is
    empty, so the key is never the empty string. The setting is read on every
    call rather than cached, because the startup code may fill it in after this
    module has already been imported.

    Returns:
        The key to sign with, always non empty.
    """
    return settings.call_webhook_secret.strip() or _fallback_secret()


def webhook_token(session_id: str) -> str:
    """Compute the short token that guards a webhook URL.

    The token is ``hmac_sha256(call_webhook_secret, session_id)`` in hex, cut to
    :data:`TOKEN_CHARS` characters, and it rides in the query string of the URLs
    we hand to Twilio.

    This is a light guard, not Twilio request signature validation. It stops a
    stranger who guesses the session id from driving our webhooks, and nothing
    more. Checking the ``X-Twilio-Signature`` header against the request URL and
    body is the production upgrade, and it is the only thing that proves the
    request really came from Twilio.

    Args:
        session_id: The copilot session id the URL belongs to.

    Returns:
        A 32 character lower case hex token.
    """
    digest = hmac.new(
        _webhook_secret().encode("utf-8"),
        session_id.encode("utf-8"),
        sha256,
    ).hexdigest()
    return digest[:TOKEN_CHARS]


def verify_token(session_id: str, token: str) -> bool:
    """Check a webhook token against the one this session should carry.

    The comparison uses :func:`hmac.compare_digest` so the time it takes does
    not leak how much of the token was right. A token carrying anything outside
    ASCII is rejected before the comparison, because ``compare_digest`` raises
    on those rather than returning False.

    Args:
        session_id: The session id taken from the webhook path.
        token: The ``t`` query parameter, possibly empty or missing.

    Returns:
        True when the token matches, False for anything else.
    """
    candidate = (token or "").strip()
    if not candidate or not candidate.isascii():
        return False
    return hmac.compare_digest(webhook_token(session_id), candidate)


# ===========================================================================
# HTTP lifecycle, the same shape GroqClient uses
# ===========================================================================


async def startup() -> None:
    """Open the shared HTTP transport. Safe to call more than once."""
    global _client
    async with _lock:
        if _client is not None and not _client.is_closed:
            return
        _client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={"User-Agent": "sales-copilot/1.0"},
        )
        logger.info("Twilio client ready (from=%s)", settings.twilio_from_number or "not set")
        if not settings.call_webhook_secret.strip():
            logger.warning(
                "CALL_WEBHOOK_SECRET is empty. The webhook token is built from "
                "values this machine can work out again, so it keeps working "
                "after a restart, but put a secret of your own in "
                "backend/.env before this server is open to anyone else."
            )


async def shutdown() -> None:
    """Close the shared HTTP transport and release pooled connections."""
    global _client
    async with _lock:
        client = _client
        _client = None
    if client is not None and not client.is_closed:
        await client.aclose()
        logger.info("Twilio client closed")


async def _ensure_client() -> httpx.AsyncClient:
    """Return the shared client, opening it lazily if startup was skipped.

    Returns:
        The live :class:`httpx.AsyncClient`.

    Raises:
        TwilioError: When the transport could not be created at all.
    """
    client = _client
    if client is not None and not client.is_closed:
        return client
    logger.warning("Twilio client used before startup, opening transport lazily")
    await startup()
    client = _client
    if client is None:
        raise TwilioError(0, "The HTTP connection to Twilio could not be opened.")
    return client


def _account_path(suffix: str) -> str:
    """Build a full REST URL under this account.

    Args:
        suffix: Path piece after the account, for example ``"Calls.json"``.

    Returns:
        The absolute URL.
    """
    sid = quote(settings.twilio_account_sid.strip(), safe="")
    return f"{TWILIO_API_ROOT}/2010-04-01/Accounts/{sid}/{suffix}"


def _auth() -> tuple[str, str]:
    """Return the HTTP basic auth pair.

    Twilio uses plain basic auth: the account id is the user and the auth token
    is the password. Nothing here is ever logged.

    Returns:
        A ``(account_sid, auth_token)`` tuple.
    """
    return settings.twilio_account_sid.strip(), settings.twilio_auth_token.strip()


def _require_credentials() -> None:
    """Refuse to send anything when the account details are incomplete.

    Raises:
        TwilioError: Naming the exact environment variables that are missing.
    """
    missing: list[str] = []
    if not settings.twilio_account_sid.strip():
        missing.append("TWILIO_ACCOUNT_SID")
    if not settings.twilio_auth_token.strip():
        missing.append("TWILIO_AUTH_TOKEN")
    if not settings.twilio_from_number.strip():
        missing.append("TWILIO_FROM_NUMBER")
    if missing:
        names = ", ".join(missing)
        raise TwilioError(
            0,
            f"Phone calling is not set up yet. Add {names} to backend/.env and "
            "start the server again.",
        )


def _error_from_response(response: httpx.Response) -> TwilioError:
    """Turn a failed Twilio response into a :class:`TwilioError`.

    Args:
        response: The non 2xx response.

    Returns:
        The error to raise, carrying both the plain wording and the raw code.
    """
    payload: Any = None
    try:
        payload = response.json()
    except ValueError:
        payload = None

    code = 0
    raw = ""
    if isinstance(payload, dict):
        raw_code = payload.get("code")
        if isinstance(raw_code, int):
            code = raw_code
        elif isinstance(raw_code, str) and raw_code.strip().isdigit():
            code = int(raw_code.strip())
        message = payload.get("message")
        if isinstance(message, str):
            raw = message
    if not raw:
        raw = _short(response.text)

    return TwilioError(
        response.status_code,
        friendly_message(code, raw),
        code=code,
        raw_message=raw,
    )


# ===========================================================================
# Calls
# ===========================================================================


async def place_call(*, session_id: str, to_number: str, rep_number: str, token: str) -> str:
    """Ring the rep's phone so the app can bridge the client into the call.

    The request creates a call to ``rep_number``. When the rep answers, Twilio
    fetches ``/twilio/voice/{session_id}`` from us and follows the TwiML there,
    which starts the media fork and dials ``to_number``. That is why the rep
    rings first: the client should never be sitting on a silent line.

    This request is never retried. A retry on a call creation can ring the rep
    twice and bill twice, and a first attempt that timed out may still have gone
    through. One attempt, then an honest error.

    Args:
        session_id: The copilot session this call belongs to.
        to_number: The client's number in E.164.
        rep_number: The rep's own phone in E.164, the leg we ring first.
        token: The webhook token from :func:`webhook_token`, put in the URLs so
            our own routes can tell a real callback from a stranger.

    Returns:
        The Twilio call sid, the ``CA...`` string.

    Raises:
        TwilioError: When the config is incomplete, when the public address is
            missing, local or written with no scheme, when Twilio refuses the
            request, or when the network fails.
    """
    _require_credentials()

    # The strict address check, the same one the provider picker uses. Twilio
    # is handed this value with a path glued on the end, so it has to carry
    # https:// in front of it. A bare host name is not an address Twilio can
    # fetch, and neither is one that only exists on this laptop.
    public = public_http_base()
    if not public:
        raise TwilioError(
            0,
            "Twilio needs a web address it can reach from the internet. Set "
            "PUBLIC_BASE_URL in backend/.env to your tunnel address, and write "
            "it in full with https:// in front, like "
            "https://abc123.ngrok-free.app. A localhost address does not work, "
            "because the call is placed from Twilio, not from this computer.",
        )

    session_path = quote(session_id, safe="")
    query = f"?t={quote(token, safe='')}"

    # StatusCallbackEvent is sent once per event name, so its value is a list.
    # Twilio reads repeated form fields, it does not split one long string.
    # The whole body must stay a dict: httpx only form encodes a dict, and it
    # would treat a list of pairs as a raw request body instead.
    form: dict[str, str | list[str]] = {
        "To": rep_number,
        "From": settings.twilio_from_number.strip(),
        "Url": f"{public}/twilio/voice/{session_path}{query}",
        "StatusCallback": f"{public}/twilio/status/{session_path}{query}",
        "StatusCallbackEvent": list(STATUS_EVENTS),
        "Timeout": str(RING_TIMEOUT_S),
    }

    client = await _ensure_client()
    url = _account_path("Calls.json")

    try:
        response = await client.post(url, data=form, auth=_auth())
    except httpx.HTTPError as exc:
        logger.warning("twilio call request failed: %s: %s", type(exc).__name__, exc)
        raise TwilioError(
            0,
            "Could not reach Twilio. Check the internet connection and try again.",
            raw_message=f"{type(exc).__name__}: {exc}",
        ) from exc

    if response.status_code not in (200, 201):
        error = _error_from_response(response)
        logger.warning(
            "twilio call rejected session=%s status=%d code=%d: %s",
            session_id,
            error.status,
            error.code,
            error.raw_message,
        )
        raise error

    try:
        payload = response.json()
    except ValueError as exc:
        raise TwilioError(
            response.status_code,
            "Twilio answered with something we could not read.",
            raw_message=_short(response.text),
        ) from exc

    sid = payload.get("sid") if isinstance(payload, dict) else None
    if not isinstance(sid, str) or not sid:
        raise TwilioError(
            response.status_code,
            "Twilio started the call but did not send back a call id.",
            raw_message=_short(response.text),
        )

    logger.info("twilio call created session=%s sid=%s", session_id, sid)
    return sid


async def hangup(call_sid: str) -> None:
    """End a call that is ringing or live.

    Setting the status to ``completed`` is how Twilio hangs up. The call is
    already over in the common case where the rep just put the phone down, and
    that is not an error here: the button has to be safe to press twice, so a
    404 from Twilio is logged and swallowed.

    Args:
        call_sid: The ``CA...`` id returned by :func:`place_call`.

    Raises:
        TwilioError: When the config is incomplete, when Twilio refuses for any
            reason other than an unknown call, or when the network fails.
    """
    _require_credentials()
    sid = call_sid.strip()
    if not sid:
        raise TwilioError(0, "There is no call id to hang up.")

    client = await _ensure_client()
    url = _account_path(f"Calls/{quote(sid, safe='')}.json")

    try:
        response = await client.post(url, data={"Status": "completed"}, auth=_auth())
    except httpx.HTTPError as exc:
        logger.warning("twilio hangup request failed: %s: %s", type(exc).__name__, exc)
        raise TwilioError(
            0,
            "Could not reach Twilio to end the call.",
            raw_message=f"{type(exc).__name__}: {exc}",
        ) from exc

    if response.status_code == 404:
        logger.info("twilio hangup: call %s is already gone", sid)
        return
    if response.status_code not in (200, 201, 204):
        error = _error_from_response(response)
        logger.warning(
            "twilio hangup rejected sid=%s status=%d code=%d: %s",
            sid,
            error.status,
            error.code,
            error.raw_message,
        )
        raise error

    logger.info("twilio call %s ended", sid)


# ===========================================================================
# Self test
# ===========================================================================

if __name__ == "__main__":
    import subprocess
    import sys
    import xml.etree.ElementTree as ElementTree

    from app.config import public_http_base, twilio_ready

    #: Dash shapes this project never types. They are checked, not typed, so the
    #: file itself stays clean.
    BANNED_DASHES = ("\u2014", "\u2013")

    # A number and a session id that would break naive string building. The
    # quote closes the attribute, the ampersand opens an entity.
    NASTY_NUMBER = '+92 300 "1234567" & 0'
    NASTY_SESSION = 'ses"sion & <id>'
    STREAM_URL = "wss://abc123.ngrok-free.app/ws/twilio?a=1&b=2"

    xml = build_twiml(session_id=NASTY_SESSION, to_number=NASTY_NUMBER, wss_url=STREAM_URL)

    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?><Response>'), xml[:60]
    assert xml.endswith("</Response>"), xml[-40:]
    assert "&amp;" in xml, "the ampersand was not escaped"
    assert "&lt;id&gt;" in xml, "the angle brackets were not escaped"
    # quoteattr picks the quoting itself. A value holding a double quote is
    # wrapped in single quotes instead of being escaped, which is just as safe,
    # so the test asserts the shape it really produces.
    assert "value='ses\"sion &amp; &lt;id&gt;'" in xml, "the attribute is not quoted safely"
    # A value holding both kinds of quote leaves it no choice but to escape.
    both = build_twiml(session_id="a\"b'c", to_number="+1", wss_url="wss://x/ws")
    assert "&quot;" in both, "a value with both quote kinds was not escaped"

    # Well formed is not an opinion, so let the parser decide.
    root = ElementTree.fromstring(xml.encode("utf-8"))
    assert root.tag == "Response"

    # The order is the fix, so the order is the test. Twilio's own voice
    # reading <Say> is played to the rep, and the rep's outbound track is our
    # client lane, so a <Say> after <Start> is transcribed as the client's
    # first sentence. Read the note in build_twiml before touching this.
    order = [child.tag for child in root]
    assert order == ["Say", "Start", "Dial"], order

    stream = root.find("./Start/Stream")
    assert stream is not None, "no Start/Stream element"
    assert stream.get("url") == STREAM_URL, stream.get("url")
    assert stream.get("track") == "both_tracks", stream.get("track")

    parameter = stream.find("./Parameter")
    assert parameter is not None, "no Parameter element"
    assert parameter.get("name") == "sessionId", parameter.get("name")
    assert parameter.get("value") == NASTY_SESSION, parameter.get("value")

    say = root.find("./Say")
    assert say is not None and say.text == "Connecting your call now.", "Say line changed"

    dial = root.find("./Dial")
    assert dial is not None, "no Dial element"
    assert dial.get("timeout") == str(RING_TIMEOUT_S), dial.get("timeout")
    number = dial.find("./Number")
    assert number is not None and number.text == NASTY_NUMBER, "the number did not survive"

    # Tokens.
    token = webhook_token("session-one")
    assert len(token) == TOKEN_CHARS, len(token)
    assert token == webhook_token("session-one"), "the token is not stable"
    assert token != webhook_token("session-two"), "two sessions share a token"
    assert verify_token("session-one", token), "a good token was rejected"
    assert verify_token("session-one", f"  {token}  "), "surrounding spaces broke it"

    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert not verify_token("session-one", tampered), "a tampered token was accepted"
    assert not verify_token("session-two", token), "a token worked on another session"
    assert not verify_token("session-one", ""), "an empty token was accepted"
    assert not verify_token("session-one", token[:-1]), "a short token was accepted"
    assert not verify_token("session-one", "not ascii ✓"), "non ascii blew up"

    # A restart must not change the token. Without CALL_WEBHOOK_SECRET the key
    # is worked out from things this machine still knows, so a second process
    # has to land on the same token. This is the case that matters: the dev
    # server reloads while the rep's phone is ringing, and a token the new
    # process does not recognise makes Twilio read a robot apology to the rep.
    BACKEND_DIR = Path(__file__).resolve().parents[2]
    PROBE = (
        "from app.services.twilio_client import webhook_token; "
        "print(webhook_token('session-one'))"
    )
    try:
        probe_run = subprocess.run(
            [sys.executable, "-c", PROBE],
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 - a machine that cannot fork is not a failure here.
        restart = f"skipped, a second process would not start ({type(exc).__name__})"
    else:
        if probe_run.returncode != 0:
            restart = f"skipped, the second process failed: {_short(probe_run.stderr)}"
        else:
            assert probe_run.stdout.strip() == token, (
                "the token changed in a new process. A reload during a live "
                "call would make every webhook fail."
            )
            restart = "ok, the token survived a restart"

    # Friendly errors keep the meaning and never leak a stack of jargon.
    assert "TWILIO_ACCOUNT_SID" in friendly_message(20003, "Authenticate")
    assert "does not own" in friendly_message(21210, "Invalid From")
    assert "TWILIO_FROM_NUMBER" in friendly_message(21210, "Invalid From")
    assert "trial account" in friendly_message(21219, "unverified")
    assert "trial account" in friendly_message(21608, "unverified")
    assert "did not say why" in friendly_message(0, "   ")
    assert "boom" in friendly_message(99999, "boom")

    # Every line the rep can be shown stays plain and carries no banned dash.
    for _code, _text in FRIENDLY_ERRORS.items():
        assert _text.endswith("."), f"error {_code} does not end in a full stop"
        for _dash in BANNED_DASHES:
            assert _dash not in _text, f"error {_code} has a banned dash"
        assert "--" not in _text, f"error {_code} has a double hyphen"

    # Readiness and the address rule agree with each other. A bare host with no
    # scheme is not an address Twilio can fetch, so it must not read as ready,
    # and it must not turn into a webhook URL with no scheme in front of it.
    _saved_base = settings.public_base_url
    try:
        for _bad in ("", "abc123.ngrok-free.app", "http://localhost:8000",
                     "https://127.0.0.1", "https://mac.local", "ftp://abc.com"):
            settings.public_base_url = _bad
            assert public_http_base() == "", f"{_bad!r} was read as a public address"
            assert not twilio_ready(), f"{_bad!r} was read as ready"
        settings.public_base_url = "https://abc123.ngrok-free.app/"
        assert public_http_base() == "https://abc123.ngrok-free.app", public_http_base()
    finally:
        settings.public_base_url = _saved_base

    # With nothing configured, placing a call must fail before the network,
    # and it must name the variables that are missing.
    if twilio_ready():
        guard = "skipped, this machine has Twilio configured"
    else:
        try:
            asyncio.run(
                place_call(
                    session_id="session-one",
                    to_number="+923001234567",
                    rep_number="+923009876543",
                    token=token,
                )
            )
        except TwilioError as exc:
            assert exc.status == 0, exc.status
            assert "backend/.env" in exc.message or "PUBLIC_BASE_URL" in exc.message, exc.message
            guard = "ok, refused before the network"
        else:  # pragma: no cover - only reachable with live credentials
            raise AssertionError("place_call did not refuse an unconfigured account")

    print(
        "twilio_client self test ok: twiml "
        f"{len(xml)} bytes, verbs {'+'.join(order)}, token {TOKEN_CHARS} chars, "
        f"restart {restart}, unconfigured guard {guard}"
    )
