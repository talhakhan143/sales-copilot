"""WhatsApp calling, the free link path and the paid Cloud API path.

There are two very different ways to put a WhatsApp call on the screen, and only
one of them works today.

**Link mode, and it always works.** :func:`call_link` and :func:`deep_link` build
a plain URL out of the number. The rep taps it, WhatsApp opens on their own
phone or desktop with the contact ready, and they press the green button
themselves. Nothing is configured, nothing is charged, no approval is needed.
The app still hears the call the way it hears one now, from the shared tab or
from a virtual audio cable.

**Cloud API mode, and read this before trusting it.** :func:`cloud_call` posts to
the WhatsApp Business Calling API so the app places the call itself. That path
is real, but it is not free and it is not open to everybody. It needs, all at
the same time:

* a Meta Business account with a verified business,
* a WhatsApp Business phone number registered on the Cloud API, with **calling
  switched on** for that number in the WhatsApp Manager,
* a permanent access token with the messaging permissions on that number,
* and consent from the person being called. Meta does not allow a cold call to a
  stranger. The person must have messaged your business number recently, inside
  the customer service window, or must have granted call permission. A call to
  somebody who never wrote to you is refused by Meta, not by us.

**This path could not be tested here.** Nobody on this machine has a Meta
business number or a token, so no request in this module has ever reached
Meta's servers. The link building, the number cleaning and the error translation
are unit tested and correct. The Cloud API call itself is written from the
documented request shape and is unverified. Treat the first real call as a test,
and do not tell a rep this feature is proven, because it is not.

One more honest limit. A real Cloud API call carries an SDP offer, which is a
WebRTC handshake this Python server does not have a stack for. :func:`cloud_call`
accepts an ``sdp_offer`` argument for whoever wires that up later. Without one,
Meta answers with an error, and this module turns that error into a plain
sentence instead of hiding it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Final

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

__all__ = [
    "CALLING_OFF_MESSAGE",
    "DEFAULT_API_VERSION",
    "EMPTY_NUMBER_MESSAGE",
    "NETWORK_MESSAGE",
    "NOT_CONFIGURED_MESSAGE",
    "NO_CONSENT_MESSAGE",
    "TOKEN_MESSAGE",
    "WhatsAppError",
    "call_link",
    "cloud_call",
    "cloud_configured",
    "deep_link",
    "shutdown",
    "startup",
    "wa_digits",
]


# ===========================================================================
# Constants
# ===========================================================================

GRAPH_BASE_URL: Final[str] = "https://graph.facebook.com"
"""Root of the Meta Graph API. Every Cloud API call hangs off this."""

DEFAULT_API_VERSION: Final[str] = "v21.0"
"""Graph version used when ``WHATSAPP_API_VERSION`` is not set."""

WA_LINK_BASE: Final[str] = "https://wa.me/"
"""Public WhatsApp link. Opens in a browser, then hands over to the app."""

WA_DEEP_LINK_BASE: Final[str] = "whatsapp://send?phone="
"""Native deep link. Opens the installed app straight away, no browser hop."""

REQUEST_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(
    connect=5.0, read=20.0, write=15.0, pool=5.0
)
"""Timeouts for one Cloud API call. Short, because a rep is waiting on it."""

CONNECTION_LIMITS: Final[httpx.Limits] = httpx.Limits(
    max_connections=8, max_keepalive_connections=4
)
"""Pool size. Calls are rare compared to speech, so this stays small."""


# ===========================================================================
# Plain messages, every one of them is read by the rep
# ===========================================================================

NOT_CONFIGURED_MESSAGE: Final[str] = (
    "WhatsApp calling from the app is not set up. Use the link option, or add "
    "WHATSAPP_TOKEN and WHATSAPP_PHONE_ID to backend/.env."
)
"""Shown when the token or the phone id is missing. Frozen by the contract."""

EMPTY_NUMBER_MESSAGE: Final[str] = (
    "There is no phone number to call. Write it with the country code, like +923001234567."
)
"""Shown when the number has no digits left after cleaning."""

TOKEN_MESSAGE: Final[str] = (
    "Your WhatsApp token has expired. Make a new one in the Meta dashboard and "
    "put it in backend/.env."
)
"""Shown when Meta rejects the access token."""

NO_CONSENT_MESSAGE: Final[str] = (
    "This person has not messaged your WhatsApp business number, so you are not "
    "allowed to call them yet. Ask them to send you a message first."
)
"""Shown when Meta blocks the call because there is no consent."""

CALLING_OFF_MESSAGE: Final[str] = (
    "Calling is not switched on for your WhatsApp business number. Turn it on in "
    "the Meta dashboard, then try again."
)
"""Shown when the business number itself is not enabled for calls."""

NOT_ALLOWED_NUMBER_MESSAGE: Final[str] = (
    "Meta does not allow calls to this number yet. Add it to the allowed list in "
    "the Meta dashboard, or use the link option."
)
"""Shown when the number is outside the allowed test list on a new account."""

PERMISSION_MESSAGE: Final[str] = (
    "Your WhatsApp token is not allowed to place calls. Check the app permissions "
    "in the Meta dashboard."
)
"""Shown when the token is valid but does not carry the calling permission."""

NETWORK_MESSAGE: Final[str] = (
    "WhatsApp did not answer. Check your internet and try again."
)
"""Shown when the request never reached Meta at all."""

BAD_REPLY_MESSAGE: Final[str] = (
    "WhatsApp answered in a way the app did not understand. Try the link option."
)
"""Shown when the response is a 200 with a shape we cannot read."""

#: Meta error codes we are confident about, mapped to the sentence the rep sees.
#: Meta's calling specific codes are not fully public and none of this could be
#: tested here, so the table stays short and :func:`_plain_reason` falls back to
#: a keyword pass over Meta's own message text before it gives up.
_REASON_BY_CODE: Final[dict[int, str]] = {
    0: TOKEN_MESSAGE,  # cannot parse the access token
    102: TOKEN_MESSAGE,  # session expired
    190: TOKEN_MESSAGE,  # access token expired or revoked
    3: PERMISSION_MESSAGE,  # capability or permission problem
    10: PERMISSION_MESSAGE,  # permission denied
    200: PERMISSION_MESSAGE,  # permission error
    131005: PERMISSION_MESSAGE,  # access denied
    131026: NO_CONSENT_MESSAGE,  # cannot be delivered to this person
    131047: NO_CONSENT_MESSAGE,  # more than 24 hours since their last message
    131030: NOT_ALLOWED_NUMBER_MESSAGE,  # number is not in the allowed list
    133010: CALLING_OFF_MESSAGE,  # this business number is not registered
}

#: Fallback keyword pass, tried in this order when the code is unknown. Meta
#: writes its messages in English prose, so the words are a better signal than a
#: code number we cannot look up.
_REASON_BY_KEYWORD: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("access token", "token has expired", "session has expired", "oauth"), TOKEN_MESSAGE),
    (
        ("calling is not enabled", "calling not enabled", "call permission", "voice calling"),
        CALLING_OFF_MESSAGE,
    ),
    (
        ("re-engagement", "24 hour", "24-hour", "customer service window", "consent", "opt-in"),
        NO_CONSENT_MESSAGE,
    ),
    (("not in the allowed list", "allowed list", "test number"), NOT_ALLOWED_NUMBER_MESSAGE),
    (("permission", "not authorized", "unauthorized"), PERMISSION_MESSAGE),
)


# ===========================================================================
# Errors
# ===========================================================================


class WhatsAppError(RuntimeError):
    """Raised when a WhatsApp Cloud call cannot be placed.

    Attributes:
        status: HTTP status Meta returned, or ``0`` when the request never
            produced a response, including the not configured case.
        message: One short plain sentence for the rep. A route can put this on
            the screen as it is, it never carries Meta's jargon.
        detail: Meta's own words, kept for the logs. Empty when there are none.
        code: Meta's numeric error code when there was one, else ``None``.
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        detail: str = "",
        code: int | None = None,
    ) -> None:
        """Store the parts and build the display string.

        Args:
            status: HTTP status code, or ``0`` for a failure with no response.
            message: The plain sentence shown to the rep.
            detail: Meta's raw message, for the logs only.
            code: Meta's numeric error code, when the envelope carried one.
        """
        self.status = status
        self.message = message
        self.detail = detail
        self.code = code
        label = f"HTTP {status}" if status else "not sent"
        tail = f" (Meta code {code}: {detail})" if detail else ""
        super().__init__(f"WhatsApp {label}: {message}{tail}")


# ===========================================================================
# Links, pure and always available
# ===========================================================================


def wa_digits(number: str) -> str:
    """Reduce a phone number to the digits a WhatsApp link needs.

    WhatsApp links carry the full international number with no plus, no spaces
    and no leading zeros, so ``+92 300 123 4567`` and ``0092-300-1234567`` both
    become ``923001234567``.

    Pass a number that has already been through
    :func:`app.telephony.normalise_e164`. This helper only reshapes digits, it
    cannot add a country code that was never typed, so a local number stays
    local and the link will point at the wrong person.

    Args:
        number: The number in any shape.

    Returns:
        Bare digits, ready to paste into a link.

    Raises:
        ValueError: When there are no digits left, with a plain message.
    """
    digits = re.sub(r"\D", "", number or "").lstrip("0")
    if not digits:
        raise ValueError(EMPTY_NUMBER_MESSAGE)
    return digits


def call_link(number: str) -> str:
    """Build the public ``wa.me`` link for a number.

    This is the link that always works. Opening it hands the number to WhatsApp
    on whatever device the rep is on, and the rep presses call.

    Args:
        number: The client number, ideally already in E.164.

    Returns:
        A URL such as ``https://wa.me/923001234567``.

    Raises:
        ValueError: When the number has no digits.
    """
    return WA_LINK_BASE + wa_digits(number)


def deep_link(number: str) -> str:
    """Build the native ``whatsapp://`` deep link for a number.

    The deep link opens the installed app directly and skips the browser hop, so
    it is the better one on a phone. It does nothing on a machine with no
    WhatsApp installed, which is why :func:`call_link` stays the default.

    Args:
        number: The client number, ideally already in E.164.

    Returns:
        A URL such as ``whatsapp://send?phone=923001234567``.

    Raises:
        ValueError: When the number has no digits.
    """
    return WA_DEEP_LINK_BASE + wa_digits(number)


# ===========================================================================
# Cloud API, gated and unverified
# ===========================================================================


def _read(field: str, fallback: str = "") -> str:
    """Read one WhatsApp setting, with the fallback for an empty value.

    The WhatsApp settings are optional and all default to an empty string, so
    they are read through ``getattr`` with the process environment behind them.
    That keeps this module importable and self testable on a checkout where the
    settings object has not grown the field yet, and it lets a value exported
    straight into the shell work too.

    This is deliberately the same read order as ``app.telephony._setting``. The
    provider picker and the call itself must agree on what is configured, or the
    rep gets a button that says ready and a call that says not set up.

    Args:
        field: The snake_case settings field name, for example
            ``"whatsapp_token"``. The environment fallback looks up the same
            name upper cased.
        fallback: Value to use when the setting is empty everywhere.

    Returns:
        The trimmed setting value, or the fallback.
    """
    value = str(getattr(settings, field, "") or "").strip()
    if not value:
        value = os.environ.get(field.upper(), "").strip()
    return value or fallback


def cloud_configured() -> bool:
    """Report whether the Cloud API path has both of the values it needs.

    Returns:
        True when a token and a phone id are both set. It says nothing about
        whether Meta will accept them, only that there is something to send.
    """
    return bool(_read("whatsapp_token") and _read("whatsapp_phone_id"))


_client: httpx.AsyncClient | None = None
_client_lock: asyncio.Lock = asyncio.Lock()


async def startup() -> None:
    """Open the shared HTTP transport. Safe to call more than once.

    The app lifespan can call this, and it does not have to. The transport is
    opened lazily on the first call either way, because WhatsApp calling is an
    optional path that most installs never touch.
    """
    global _client
    async with _client_lock:
        if _client is not None and not _client.is_closed:
            return
        _client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            limits=CONNECTION_LIMITS,
            headers={"User-Agent": "sales-copilot/1.0"},
        )
        logger.info("WhatsApp client ready (configured=%s)", cloud_configured())


async def shutdown() -> None:
    """Close the shared HTTP transport and release pooled connections."""
    global _client
    async with _client_lock:
        client = _client
        _client = None
    if client is not None and not client.is_closed:
        await client.aclose()
        logger.info("WhatsApp client closed")


async def _ensure_client() -> httpx.AsyncClient:
    """Return the shared client, opening it lazily when startup was skipped.

    Returns:
        A live :class:`httpx.AsyncClient`.

    Raises:
        WhatsAppError: When the transport could not be created at all.
    """
    client = _client
    if client is not None and not client.is_closed:
        return client
    await startup()
    if _client is None:
        raise WhatsAppError(0, NETWORK_MESSAGE, detail="HTTP transport could not be created")
    return _client


def _plain_reason(code: int | None, detail: str) -> str:
    """Translate a Meta error into one sentence the rep can act on.

    The code table is tried first, then a keyword pass over Meta's own message,
    because Meta's calling error codes are not fully documented in public and
    none of them could be verified from this machine. When neither matches, the
    rep is shown Meta's own words rather than an invented reason.

    Args:
        code: Meta's numeric error code, or ``None``.
        detail: Meta's message text, possibly empty.

    Returns:
        A plain English sentence.
    """
    if code is not None and code in _REASON_BY_CODE:
        return _REASON_BY_CODE[code]
    lowered = (detail or "").lower()
    for keywords, reason in _REASON_BY_KEYWORD:
        if any(word in lowered for word in keywords):
            return reason
    if detail:
        return f"WhatsApp could not start the call. Meta said: {detail}"
    return "WhatsApp could not start the call. Try the link option."


def _envelope(body: object) -> dict[str, Any]:
    """Pull the ``error`` object out of a Graph API response body.

    Args:
        body: The decoded JSON body, whatever shape it came back in.

    Returns:
        The error dict, or an empty dict when there is no error in it.
    """
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return error
    return {}


def _error_from_body(status: int, body: object) -> WhatsAppError:
    """Build a :class:`WhatsAppError` from a Graph API error response.

    Args:
        status: The HTTP status code Meta returned.
        body: The decoded JSON body.

    Returns:
        The error, with a plain message on the outside and Meta's own words
        kept in ``detail`` for the logs.
    """
    error = _envelope(body)
    detail = str(error.get("message") or "").strip()
    raw_code = error.get("code")
    code = raw_code if isinstance(raw_code, int) else None
    return WhatsAppError(status, _plain_reason(code, detail), detail=detail, code=code)


def _call_id(body: object) -> str:
    """Read the call id out of a successful Cloud API response.

    The documented success shape is
    ``{"messaging_product": "whatsapp", "calls": [{"id": "wacid.XXXX"}]}``.

    Args:
        body: The decoded JSON body.

    Returns:
        The call id string.

    Raises:
        WhatsAppError: When the body does not carry one.
    """
    if isinstance(body, dict):
        calls = body.get("calls")
        if isinstance(calls, list) and calls:
            first = calls[0]
            if isinstance(first, dict):
                call_id = str(first.get("id") or "").strip()
                if call_id:
                    return call_id
    raise WhatsAppError(200, BAD_REPLY_MESSAGE, detail=str(body)[:300])


async def cloud_call(
    *,
    to_number: str,
    session_id: str,
    sdp_offer: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Ask Meta to place a WhatsApp call from the business number.

    This is the gated path. Read the module docstring first: it needs a Meta
    Business account, a business number with calling switched on, and consent
    from the person being called, and it has never been run against the real API
    from this machine.

    Args:
        to_number: The client number. Pass it already normalised by
            :func:`app.telephony.normalise_e164`.
        session_id: Our own call session id. It is not sent to Meta, it only
            tags the log line so one call can be traced later.
        sdp_offer: The WebRTC SDP offer for the audio leg. A real connect needs
            one. Leave it out and Meta will refuse, which is surfaced as a plain
            message rather than swallowed.
        client: An HTTP client to reuse. Leave it out to use the shared one.

    Returns:
        Meta's call id, for example ``"wacid.HBgMOTIzMDAxMjM0NTY3"``.

    Raises:
        WhatsAppError: When the feature is not configured, when the number is
            empty, when the network fails, or when Meta refuses. ``message`` is
            always a plain sentence that can go straight on the screen.
    """
    token = _read("whatsapp_token")
    phone_id = _read("whatsapp_phone_id")
    if not token or not phone_id:
        raise WhatsAppError(0, NOT_CONFIGURED_MESSAGE)

    try:
        digits = wa_digits(to_number)
    except ValueError as exc:
        raise WhatsAppError(0, str(exc)) from exc

    version = _read("whatsapp_api_version", DEFAULT_API_VERSION)
    url = f"{GRAPH_BASE_URL}/{version}/{phone_id}/calls"
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": digits,
        "action": "connect",
    }
    if sdp_offer:
        payload["session"] = {"sdp_type": "offer", "sdp": sdp_offer}

    transport = client or await _ensure_client()
    logger.info("whatsapp cloud call session=%s to=%s version=%s", session_id, digits, version)

    try:
        response = await transport.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        raise WhatsAppError(0, NETWORK_MESSAGE, detail=f"{type(exc).__name__}: {exc}") from exc

    try:
        body: object = response.json()
    except ValueError:
        body = {"error": {"message": response.text[:300]}}

    if response.status_code >= 400 or _envelope(body):
        error = _error_from_body(response.status_code, body)
        logger.warning(
            "whatsapp cloud call refused session=%s status=%s code=%s detail=%s",
            session_id,
            error.status,
            error.code,
            error.detail,
        )
        raise error

    call_id = _call_id(body)
    logger.info("whatsapp cloud call accepted session=%s call_id=%s", session_id, call_id)
    return call_id


if __name__ == "__main__":
    # Self test. It covers the two link builders, the number cleaning, the error
    # translation table and the not configured path, all with no network. The
    # Cloud API request itself cannot be tested here, see the module docstring.
    # Run it with the venv python: .venv/bin/python -m app.services.whatsapp_client
    _BANNED_CHARS = ("\u2014", "\u2013")

    assert wa_digits("+923001234567") == "923001234567"
    assert wa_digits("+92 300 123 4567") == "923001234567"
    assert wa_digits("0092-300-1234567") == "923001234567"
    assert wa_digits("(+92) 300 1234567") == "923001234567"
    assert wa_digits("+1 (415) 555-2671") == "14155552671"

    assert call_link("+923001234567") == "https://wa.me/923001234567"
    assert call_link("0092 300 123 4567") == "https://wa.me/923001234567"
    assert deep_link("+923001234567") == "whatsapp://send?phone=923001234567"

    for _bad in ("", "   ", "no number", "()-"):
        try:
            call_link(_bad)
        except ValueError as _exc:
            assert str(_exc) == EMPTY_NUMBER_MESSAGE
        else:
            raise AssertionError(f"{_bad!r} should not build a link")

    assert _plain_reason(190, "Error validating access token") == TOKEN_MESSAGE
    assert _plain_reason(131047, "Re-engagement message") == NO_CONSENT_MESSAGE
    assert _plain_reason(131030, "Recipient not in allowed list") == NOT_ALLOWED_NUMBER_MESSAGE
    assert _plain_reason(133010, "number not registered") == CALLING_OFF_MESSAGE
    assert _plain_reason(None, "The access token has expired") == TOKEN_MESSAGE
    assert _plain_reason(None, "Calling is not enabled for this number") == CALLING_OFF_MESSAGE
    assert _plain_reason(999999, "Calling not enabled on this phone") == CALLING_OFF_MESSAGE
    assert _plain_reason(None, "24 hour window has closed") == NO_CONSENT_MESSAGE
    assert _plain_reason(None, "Something new broke").startswith("WhatsApp could not start")
    assert "Something new broke" in _plain_reason(None, "Something new broke")
    assert _plain_reason(None, "") == "WhatsApp could not start the call. Try the link option."

    _built = _error_from_body(401, {"error": {"message": "Session has expired", "code": 190}})
    assert _built.status == 401 and _built.code == 190
    assert _built.message == TOKEN_MESSAGE
    assert _built.detail == "Session has expired"
    assert "Meta code 190" in str(_built)

    assert _call_id({"messaging_product": "whatsapp", "calls": [{"id": "wacid.TEST"}]}) == "wacid.TEST"
    for _shape in ({}, {"calls": []}, {"calls": [{}]}, "not json at all"):
        try:
            _call_id(_shape)
        except WhatsAppError as _exc:
            assert _exc.message == BAD_REPLY_MESSAGE
        else:
            raise AssertionError(f"{_shape!r} should not read as a call id")

    for _label, _text in (
        ("not configured", NOT_CONFIGURED_MESSAGE),
        ("empty number", EMPTY_NUMBER_MESSAGE),
        ("token", TOKEN_MESSAGE),
        ("consent", NO_CONSENT_MESSAGE),
        ("calling off", CALLING_OFF_MESSAGE),
        ("allowed list", NOT_ALLOWED_NUMBER_MESSAGE),
        ("permission", PERMISSION_MESSAGE),
        ("network", NETWORK_MESSAGE),
        ("bad reply", BAD_REPLY_MESSAGE),
    ):
        for _char in _BANNED_CHARS:
            assert _char not in _text, f"the {_label} message has a banned dash"
        assert "--" not in _text, f"the {_label} message has a double hyphen"
        assert _text.endswith("."), f"the {_label} message does not end in a period"

    if cloud_configured():
        print("  note: WhatsApp Cloud is configured here, the not configured path was skipped")
    else:
        async def _check_not_configured() -> None:
            """The gate must fire before anything touches the network."""
            try:
                await cloud_call(to_number="+923001234567", session_id="test-session")
            except WhatsAppError as exc:
                assert exc.status == 0
                assert exc.message == NOT_CONFIGURED_MESSAGE
            else:
                raise AssertionError("cloud_call ran with no token, it must refuse")

        asyncio.run(_check_not_configured())

    print("app/services/whatsapp_client.py self test passed")
