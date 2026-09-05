"""Call provider registry, readiness checks and phone number cleaning.

Until now the app only listened. The rep dialled on their own phone and pushed
the audio in by hand. There are four ways to start a call now, and only two of
them work out of the box, so something has to hold the honest truth about each:

* ``manual``, the rep dials and the app only listens. Always available, free.
* ``whatsapp_link``, the app opens WhatsApp with the number already filled in
  and the rep presses the green button. Always available, free.
* ``whatsapp_cloud``, the app places the WhatsApp call itself. Gated three ways by
  Meta, and the first gate rules out cold calling entirely: you have to ask the
  person for call permission and be told yes before you may dial them, and a
  production number gets one ask per person per day. On top of that the number
  needs a 2000 a day messaging limit, and the call is WebRTC, so Meta wants an SDP
  offer rather than a plain request. Use ``whatsapp_link`` to cold call.
* ``twilio``, the app rings the rep's phone and then bridges the client. This is
  real money per minute, plus a rented number, plus a public URL that Twilio can
  reach from the internet.

:func:`provider_status` is what the picker in the browser reads. It reports
``ready`` and, when a provider is not ready, the exact environment variable
names that are still missing. "Not configured" with no detail is the kind of
message that costs somebody an afternoon.

:func:`normalise_e164` is the other half. A number without a country code cannot
be dialled, and guessing one is how a rep ends up calling a stranger in another
country. So this module either returns a number it is sure about, or it raises
with one short sentence the rep can act on.

Nothing here touches the network. It reads the settings object and the process
environment only, so it imports from any layer and self tests on its own::

    .venv/bin/python -m app.telephony
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from typing import Final
from urllib.parse import urlsplit

from app.config import settings

logger = logging.getLogger(__name__)

__all__ = [
    "CALL_STATES",
    "DEFAULT_CALL_STATE",
    "COUNTRY_CODE_MESSAGE",
    "DIGITS_ONLY_MESSAGE",
    "EMPTY_NUMBER_MESSAGE",
    "LENGTH_MESSAGE",
    "MAX_E164_DIGITS",
    "MIN_E164_DIGITS",
    "PROVIDERS",
    "PROVIDER_KEYS",
    "PROVIDER_MAP",
    "default_country_code",
    "missing_settings",
    "normalise_call_state",
    "normalise_e164",
    "provider_entry",
    "provider_ready",
    "provider_status",
    "public_base_url",
    "public_base_usable",
]


# ===========================================================================
# Settings access
# ===========================================================================


def _setting(field: str) -> str:
    """Read one optional telephony setting as a clean string.

    The telephony settings are all optional and all default to an empty string,
    so the app boots with none of them. They are read through ``getattr`` with a
    fallback to the process environment for two reasons. First, this module then
    imports and self tests on its own, even in a checkout where the settings
    object has not grown the field yet. Second, a value exported straight into
    the environment (a CI job, a one off shell) still works.

    Args:
        field: The snake_case settings field name, for example
            ``"twilio_account_sid"``. The environment fallback looks up the same
            name upper cased, which is the variable name the contract uses.

    Returns:
        The trimmed value, or an empty string when it is not set anywhere.
    """
    value = str(getattr(settings, field, "") or "").strip()
    if value:
        return value
    return os.environ.get(field.upper(), "").strip()


def default_country_code() -> str:
    """Return the configured default country code as bare digits.

    ``DEFAULT_COUNTRY_CODE`` may be written any of the ways a person naturally
    writes it: ``92``, ``+92`` or ``0092``. All three mean the same thing, so
    they are all reduced to ``92`` here.

    Returns:
        The country code digits, for example ``"92"``. An empty string when it
        is not set, or when it is set to something that cannot be a country
        code, because a bad value must not turn into a wrong phone number.
    """
    raw = _setting("default_country_code")
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw).lstrip("0")
    if not 1 <= len(digits) <= 4:
        logger.debug("DEFAULT_COUNTRY_CODE is set to %r, which is not usable", raw)
        return ""
    return digits


def public_base_url() -> str:
    """Return the public base URL Twilio calls back on, without a trailing slash.

    Returns:
        The configured URL, for example ``"https://abc123.ngrok-free.app"``, or
        an empty string when it is not set.
    """
    return _setting("public_base_url").rstrip("/")


def public_base_usable(raw: str = "") -> bool:
    """Report whether a public base URL is one Twilio could actually reach.

    Twilio fetches our TwiML and opens our media socket from its own servers, so
    the URL has to resolve on the public internet. A laptop address does not.
    This is why a filled in but local ``PUBLIC_BASE_URL`` still counts as
    missing in :func:`provider_status`: the value is there, but a call made with
    it would ring the rep's phone and then die in silence, which is worse than
    refusing up front.

    The scheme is required for the same reason. The webhook URL Twilio fetches
    is built by joining this value with a path, so a bare host with no
    ``https://`` in front of it makes a URL Twilio cannot fetch. Note that
    ``config.public_wss_base`` is kinder and fills in ``wss://`` for a bare
    host. This check stays the stricter of the two on purpose, because it is the
    one that decides whether the rep is shown a button that spends money.

    A plain IP address gets the same treatment through
    :func:`_is_local_ip_literal`, and this is the trap worth naming. A rep who
    reads "needs a public address" often writes down the address their laptop
    shows them, which is a home network address like ``192.168.1.10``. That is
    not a public address. Twilio would still create the call and still charge
    for it, ring the rep's phone, fail to fetch the webhook, and read its own
    apology into their ear. So the name rules alone are not enough here, the
    number ranges have to be checked too.

    Args:
        raw: A URL to test. Empty means test the configured one.

    Returns:
        True when the URL has an http or https scheme and a host that is not a
        loopback, a machine local name, or an IP address from a range that never
        travels across the public internet.
    """
    value = (raw or public_base_url()).strip()
    if not value:
        return False
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host or host in _LOCAL_HOSTS:
        return False
    if _is_local_ip_literal(host):
        return False
    return not host.endswith(_LOCAL_SUFFIXES)


#: Hosts that can never be reached from Twilio's side of the internet.
_LOCAL_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "ip6-localhost",
        "host.docker.internal",
    }
)

#: Host suffixes that only mean something on the local machine or network.
_LOCAL_SUFFIXES: Final[tuple[str, ...]] = (".local", ".localhost", ".internal", ".lan")


def _is_local_ip_literal(host: str) -> bool:
    """Report whether a host is an IP address that stays inside one network.

    A name like ``abc123.ngrok-free.app`` is judged by the name rules above.
    This is for the other case, where the rep wrote a number instead of a name.
    Home and office addresses such as ``192.168.1.10``, ``10.0.0.5`` and
    ``172.16.4.4`` look like real addresses and read like real addresses, but
    routers on the internet drop them, so Twilio can never reach one.

    The same test that ``services/jina.py`` uses for its own fetches is used
    here, for the same reason: the address ranges are the honest answer, and a
    hand written list of them would go stale.

    Args:
        host: The host part of a URL, already lower cased. ``urlsplit`` has
            taken the square brackets off an IPv6 address by this point.

    Returns:
        True when the host is an IP address from a private, loopback, link
        local, reserved, multicast or unspecified range. False for a public IP
        address, which is a fine thing to point at, and False for anything that
        is not an IP address at all.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False

    # An IPv6 address can carry an IPv4 one inside it, and "::ffff:192.168.1.10"
    # is just as unreachable as the plain form. Judge the address it wraps.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped

    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


# ===========================================================================
# Provider registry
# ===========================================================================

PROVIDERS: list[dict[str, object]] = [
    {
        "key": "manual",
        "label": "I will dial myself",
        "blurb": "You call from your own phone or WhatsApp. The app just listens.",
        "cost": "Free",
        "requires": (),
    },
    {
        "key": "whatsapp_link",
        "label": "WhatsApp, from my phone",
        "blurb": "We open WhatsApp with the number ready. You press call.",
        "cost": "Free",
        "requires": (),
    },
    {
        "key": "whatsapp_cloud",
        "label": "WhatsApp, from the app",
        # Checked against Meta's own docs, not guessed. Three separate walls, and
        # the first one is the reason this can never do a cold call:
        #   1. You must ask the person for call permission first, and they must
        #      say yes. Production accounts get 1 ask per person per day, 2 a week.
        #   2. Your number needs a 2000 a day messaging limit. A new number starts
        #      far below that and has to earn its way up.
        #   3. The call itself is WebRTC. Meta wants an SDP offer, so this needs a
        #      whole media stack, not just an HTTP request.
        # Business initiated calling is also off in the US, Canada, Egypt, Vietnam
        # and Nigeria. Pakistan is fine.
        "blurb": (
            "Cannot cold call. WhatsApp makes you ask the person for permission "
            "first, and they have to say yes."
        ),
        "cost": "Meta business rates, and a number that already sends 2000 a day",
        "requires": ("whatsapp_token", "whatsapp_phone_id"),
    },
    {
        "key": "twilio",
        "label": "Phone call, from the app",
        "blurb": "We ring your phone first, then join the client. The app hears both sides.",
        "cost": "About 1 to 3 US cents a minute, plus the number",
        "requires": (
            "twilio_account_sid",
            "twilio_auth_token",
            "twilio_from_number",
            "public_base_url",
        ),
    },
]
"""The four ways to start a call, in the order the picker shows them.

``key``, ``label``, ``blurb`` and ``cost`` are frozen by the contract and every
one of them is read by the rep, so they stay short and plain. The two free
options come first on purpose: a rep who never reads past the first line still
lands on something that works and costs nothing.

``cost`` is the only place the money is stated, so it states it in numbers, not
in a word like "paid". A rep should never find out that Twilio charges by being
charged.

``requires`` is internal. It lists the settings fields a provider needs before
it can work, in the order they should be filled in. :func:`provider_status`
turns those field names into the upper case environment variable names the rep
puts in ``backend/.env``, and never leaks the ``requires`` key onto the wire.
"""

PROVIDER_MAP: dict[str, dict[str, object]] = {str(item["key"]): item for item in PROVIDERS}
"""Lookup from provider key to its full entry."""

PROVIDER_KEYS: tuple[str, ...] = tuple(PROVIDER_MAP)
"""The four valid provider keys, in display order."""

CALL_STATES: Final[tuple[str, ...]] = (
    "idle",
    "dialing",
    "ringing",
    "live",
    "ended",
    "failed",
)
"""Every state a call can be in, in the order a call normally walks through them.

The last two are both end states. ``ended`` means the call finished, ``failed``
means it never really started, and the rep needs to see the difference.
"""

DEFAULT_CALL_STATE: Final[str] = "idle"
"""State of a session that has no call on it, and the fallback for junk input."""


def provider_entry(key: str) -> dict[str, object] | None:
    """Look up one provider entry by key.

    Args:
        key: A provider key off the wire, possibly with odd casing or spaces.

    Returns:
        The entry from :data:`PROVIDERS`, or ``None`` when the key is not one of
        ours. This returns ``None`` instead of falling back to a default on
        purpose: a wrong default here would dial through a provider the rep did
        not pick, and one of them costs money.
    """
    return PROVIDER_MAP.get((key or "").strip().lower())


def missing_settings(key: str) -> list[str]:
    """List the environment variables a provider still needs.

    Args:
        key: A provider key.

    Returns:
        The upper case variable names that are empty or unusable, in the order
        they should be filled in. An empty list when the provider is ready, and
        also when the key is unknown, because there is nothing to name.
    """
    entry = provider_entry(key)
    if entry is None:
        return []
    raw = entry.get("requires")
    required: tuple[str, ...] = raw if isinstance(raw, tuple) else ()
    return [field.upper() for field in required if not _field_present(field)]


def provider_ready(key: str) -> bool:
    """Report whether a provider can place a call right now.

    Args:
        key: A provider key.

    Returns:
        True when every setting the provider needs is present and usable. False
        for an unknown key.
    """
    if provider_entry(key) is None:
        return False
    return not missing_settings(key)


def provider_status() -> list[dict[str, object]]:
    """Return the providers shaped for ``GET /api/call/providers``.

    This is the whole honesty layer of the feature. The browser draws the picker
    straight from this list: an entry with ``ready`` false is drawn disabled, and
    the ``missing`` names are printed as the reason, so the rep learns what to
    put in ``backend/.env`` instead of pressing a dead button.

    Returns:
        One dict per provider carrying exactly ``key``, ``label``, ``ready``,
        ``blurb``, ``cost`` and ``missing``. The internal ``requires`` field is
        not included.
    """
    out: list[dict[str, object]] = []
    for entry in PROVIDERS:
        key = str(entry["key"])
        missing = missing_settings(key)
        out.append(
            {
                "key": key,
                "label": str(entry["label"]),
                "ready": not missing,
                "blurb": str(entry["blurb"]),
                "cost": str(entry["cost"]),
                "missing": missing,
            }
        )
    return out


def normalise_call_state(value: str | None) -> str:
    """Clean a call state string.

    Args:
        value: Whatever a caller or a provider callback produced.

    Returns:
        One of :data:`CALL_STATES`, falling back to :data:`DEFAULT_CALL_STATE`.
    """
    state = (value or "").strip().lower()
    return state if state in CALL_STATES else DEFAULT_CALL_STATE


def _field_present(field: str) -> bool:
    """Report whether one required setting is filled in and usable.

    Args:
        field: A settings field name from a provider's ``requires`` tuple.

    Returns:
        True when the setting has a value we can work with. ``public_base_url``
        is special: a value that points at this laptop is treated as missing,
        see :func:`public_base_usable`.
    """
    if field == "public_base_url":
        return public_base_usable()
    return bool(_setting(field))


# ===========================================================================
# Phone numbers
# ===========================================================================

MIN_E164_DIGITS: Final[int] = 8
"""Shortest number we will accept, country code included."""

MAX_E164_DIGITS: Final[int] = 15
"""Longest number E.164 allows, country code included. This is a hard ceiling."""

EMPTY_NUMBER_MESSAGE: Final[str] = "Write the phone number, like +923001234567."
"""Shown when the number field is blank."""

DIGITS_ONLY_MESSAGE: Final[str] = "Use only numbers, like +923001234567."
"""Shown when the text carries letters or signs we cannot read as a number."""

COUNTRY_CODE_MESSAGE: Final[str] = "Write the number with the country code, like +923001234567."
"""Shown when the country code is missing, and we refuse to guess one.

This exact sentence is frozen by the contract. It is the answer to the one
mistake that actually happens: a rep types their local number the way they say
it out loud, and no software on earth can tell which country that belongs to.
"""

LENGTH_MESSAGE: Final[str] = (
    f"That number looks wrong. A phone number has {MIN_E164_DIGITS} to "
    f"{MAX_E164_DIGITS} digits, like +923001234567."
)
"""Shown when the digit count cannot be a real phone number."""

#: Characters people put inside a phone number that carry no meaning. Every dash
#: shape is written as an escape, the two long ones included, so this file never
#: holds a character the project bans.
_TRIM_CHARS: Final[frozenset[str]] = frozenset(
    " \t\r\n\u00a0"  # spaces, including the one a browser pastes
    "()[]{}<>"  # brackets
    ".,;:/\\'\"*"  # punctuation people sprinkle in
    "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212"  # every dash shape there is
)

#: A cleaned number: an optional single plus, then digits, and nothing else.
_E164_SHAPE_RE: Final[re.Pattern[str]] = re.compile(r"^\+?\d+$")


def normalise_e164(raw: str, *, country_code: str | None = None) -> str:
    """Turn what a rep typed into one dialable E.164 number, or refuse.

    Four shapes are accepted, and they cover how the number is written on a
    business card, in a browser, in a contact list and out loud:

    ============================  ===================================================
    ``+923001234567``             already E.164, kept as it is
    ``00923001234567``            ``00`` is the international prefix, it is dropped
    ``923001234567``             bare digits, read as already carrying the country code
    ``03001234567``              local, only when a default country code is configured
    ============================  ===================================================

    Spaces, dashes of every shape, brackets and dots are removed first, so
    ``+92 300 123 4567`` and ``(+92)-300-1234567`` both work.

    The bare form has one limit worth stating plainly. ``923001234567`` and a
    local number that happens to be twelve digits long look identical, and there
    is no honest way to tell them apart, so a number with no plus and no leading
    zero is taken as already carrying its country code. That is the contract's
    rule. Everything that is genuinely ambiguous, which is the local ``0`` form
    with no default country code, is refused instead of guessed.

    Args:
        raw: The number as typed.
        country_code: Country code to use for a local number, written any way
            (``"92"``, ``"+92"``, ``"0092"``). Leave it out to read
            ``DEFAULT_COUNTRY_CODE`` from the settings, which is the normal call.
            Pass ``""`` to say there is no default, which makes a local number an
            error.

    Returns:
        The number in E.164, always starting with a plus, for example
        ``"+923001234567"``.

    Raises:
        ValueError: With one short plain sentence the rep can act on. It is one
            of :data:`EMPTY_NUMBER_MESSAGE`, :data:`DIGITS_ONLY_MESSAGE`,
            :data:`COUNTRY_CODE_MESSAGE` or :data:`LENGTH_MESSAGE`. The message
            is written for the screen, so a route can pass it straight through.
    """
    cleaned = "".join(char for char in (raw or "") if char not in _TRIM_CHARS)
    if not cleaned or cleaned == "+":
        raise ValueError(EMPTY_NUMBER_MESSAGE)
    if not _E164_SHAPE_RE.match(cleaned):
        raise ValueError(DIGITS_ONLY_MESSAGE)

    if cleaned.startswith("+"):
        digits = cleaned[1:]
        # A country code never starts with a zero, so "+0300..." is a local
        # number wearing a plus. We cannot know which country, so we refuse.
        if digits.startswith("0"):
            raise ValueError(COUNTRY_CODE_MESSAGE)
    elif cleaned.startswith("00"):
        digits = cleaned[2:]
        if digits.startswith("0"):
            raise ValueError(COUNTRY_CODE_MESSAGE)
    elif cleaned.startswith("0"):
        prefix = default_country_code() if country_code is None else _clean_country_code(country_code)
        if not prefix:
            raise ValueError(COUNTRY_CODE_MESSAGE)
        digits = prefix + cleaned[1:]
    else:
        digits = cleaned

    if not MIN_E164_DIGITS <= len(digits) <= MAX_E164_DIGITS:
        raise ValueError(LENGTH_MESSAGE)
    return "+" + digits


def _clean_country_code(raw: str) -> str:
    """Reduce a country code written any way to bare digits.

    Args:
        raw: The code as configured or passed in, for example ``"+92"``.

    Returns:
        The digits, for example ``"92"``. An empty string when the value cannot
        be a country code, which the caller treats as "no default is set".
    """
    digits = re.sub(r"\D", "", raw or "").lstrip("0")
    return digits if 1 <= len(digits) <= 4 else ""


if __name__ == "__main__":
    # Self test. It covers the number shapes a rep really types, the shapes we
    # refuse, the local number path with and without a default country code, and
    # the readiness maths behind the provider picker. No network, no server.
    # Run it with the venv python: .venv/bin/python -m app.telephony
    _BANNED_CHARS = ("\u2014", "\u2013")

    def _accepts(raw: str, expected: str, **kwargs: str) -> None:
        """Assert one input normalises to the expected E.164 number."""
        got = normalise_e164(raw, **kwargs)  # type: ignore[arg-type]
        assert got == expected, f"{raw!r} gave {got!r}, expected {expected!r}"

    def _refuses(raw: str, message: str, **kwargs: str) -> None:
        """Assert one input is refused with the exact plain message."""
        try:
            got = normalise_e164(raw, **kwargs)  # type: ignore[arg-type]
        except ValueError as exc:
            assert str(exc) == message, f"{raw!r} said {str(exc)!r}, expected {message!r}"
            return
        raise AssertionError(f"{raw!r} was accepted as {got!r}, it should have been refused")

    # == accepted shapes
    _accepts("+923001234567", "+923001234567")
    _accepts("+92 300 123 4567", "+923001234567")
    _accepts("+92-300-1234567", "+923001234567")
    _accepts("(+92) 300 1234567", "+923001234567")
    _accepts("  +923001234567  ", "+923001234567")
    _accepts("00923001234567", "+923001234567")
    _accepts("0092 300 1234567", "+923001234567")
    _accepts("923001234567", "+923001234567")
    _accepts("92 300 1234567", "+923001234567")
    _accepts("+1 (415) 555-2671", "+14155552671")
    _accepts("+44 20 7946 0958", "+442079460958")
    # Every dash shape, including the two this project never types by hand.
    _accepts("+92\u2013300\u20111234567", "+923001234567")
    _accepts("+92\u2014300\u20121234567", "+923001234567")

    # == the local shape, which only works with a default country code
    _accepts("03001234567", "+923001234567", country_code="92")
    _accepts("0300 123 4567", "+923001234567", country_code="+92")
    _accepts("0300-1234567", "+923001234567", country_code="0092")
    _refuses("03001234567", COUNTRY_CODE_MESSAGE, country_code="")
    _refuses("03001234567", COUNTRY_CODE_MESSAGE, country_code="abc")
    _refuses("0300 123 4567", COUNTRY_CODE_MESSAGE, country_code="")

    # == the same local path, this time reading the setting instead of an argument
    _saved_cc = os.environ.get("DEFAULT_COUNTRY_CODE")
    try:
        os.environ["DEFAULT_COUNTRY_CODE"] = "92"
        if default_country_code() == "92":
            _accepts("03001234567", "+923001234567")
            _accepts("0300 123 4567", "+923001234567")
            os.environ["DEFAULT_COUNTRY_CODE"] = "+92"
            _accepts("03001234567", "+923001234567")
            os.environ.pop("DEFAULT_COUNTRY_CODE")
            if not default_country_code():
                _refuses("03001234567", COUNTRY_CODE_MESSAGE)
        else:
            print("  note: DEFAULT_COUNTRY_CODE is pinned elsewhere, env path not exercised")
    finally:
        os.environ.pop("DEFAULT_COUNTRY_CODE", None)
        if _saved_cc is not None:
            os.environ["DEFAULT_COUNTRY_CODE"] = _saved_cc

    # == refused shapes
    _refuses("", EMPTY_NUMBER_MESSAGE)
    _refuses("   ", EMPTY_NUMBER_MESSAGE)
    _refuses("+", EMPTY_NUMBER_MESSAGE)
    _refuses("()- ", EMPTY_NUMBER_MESSAGE)
    _refuses("call me", DIGITS_ONLY_MESSAGE)
    _refuses("+92300abc4567", DIGITS_ONLY_MESSAGE)
    _refuses("++923001234567", DIGITS_ONLY_MESSAGE)
    _refuses("92300+1234567", DIGITS_ONLY_MESSAGE)
    _refuses("+92 300 12", LENGTH_MESSAGE)
    _refuses("1234567", LENGTH_MESSAGE)
    _refuses("+9230012345678901", LENGTH_MESSAGE)
    _refuses("00923001234567890123", LENGTH_MESSAGE)
    _refuses("+0923001234567", COUNTRY_CODE_MESSAGE)
    _refuses("000923001234567", COUNTRY_CODE_MESSAGE)

    # == the provider registry
    assert PROVIDER_KEYS == ("manual", "whatsapp_link", "whatsapp_cloud", "twilio")
    assert provider_entry(" TWILIO ") is PROVIDER_MAP["twilio"]
    assert provider_entry("skype") is None
    assert provider_ready("skype") is False
    assert missing_settings("skype") == []

    _status = provider_status()
    assert [row["key"] for row in _status] == list(PROVIDER_KEYS)
    for _row in _status:
        assert set(_row) == {"key", "label", "ready", "blurb", "cost", "missing"}
        for _field in ("label", "blurb", "cost"):
            _text = str(_row[_field])
            assert _text and _text[0].isupper(), f"{_row['key']} {_field} reads badly"
            for _char in _BANNED_CHARS:
                assert _char not in _text, f"{_row['key']} {_field} has a banned dash"
            assert "--" not in _text, f"{_row['key']} {_field} has a double hyphen"

    _by_key = {str(row["key"]): row for row in _status}
    assert _by_key["manual"]["ready"] is True and _by_key["manual"]["missing"] == []
    assert _by_key["whatsapp_link"]["ready"] is True
    assert _by_key["manual"]["cost"] == "Free"
    assert _by_key["whatsapp_link"]["cost"] == "Free"
    assert "cents a minute" in str(_by_key["twilio"]["cost"])
    # The cloud option has to say WHY it cannot be used, not just what is
    # missing, because the reason is a policy wall and no amount of setting env
    # vars gets past it.
    assert "Cannot cold call" in str(_by_key["whatsapp_cloud"]["blurb"])
    assert "permission" in str(_by_key["whatsapp_cloud"]["blurb"]).lower()

    # == readiness, simulated through the environment. Skipped when this machine
    #    already has real telephony settings, because then the "off" state is
    #    not reachable and a failure here would be a lie.
    _TELEPHONY_FIELDS = (
        "twilio_account_sid",
        "twilio_auth_token",
        "twilio_from_number",
        "public_base_url",
        "whatsapp_token",
        "whatsapp_phone_id",
    )
    if any(_setting(_field) for _field in _TELEPHONY_FIELDS):
        print("  note: telephony settings are configured here, readiness checks skipped")
    else:
        assert _by_key["whatsapp_cloud"]["ready"] is False
        assert _by_key["whatsapp_cloud"]["missing"] == ["WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID"]
        assert _by_key["twilio"]["ready"] is False
        assert _by_key["twilio"]["missing"] == [
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_FROM_NUMBER",
            "PUBLIC_BASE_URL",
        ]

        try:
            os.environ["WHATSAPP_TOKEN"] = "test-token"
            assert missing_settings("whatsapp_cloud") == ["WHATSAPP_PHONE_ID"]
            os.environ["WHATSAPP_PHONE_ID"] = "1234567890"
            assert provider_ready("whatsapp_cloud") is True

            os.environ["TWILIO_ACCOUNT_SID"] = "ACtest"
            os.environ["TWILIO_AUTH_TOKEN"] = "secret"
            os.environ["TWILIO_FROM_NUMBER"] = "+14155552671"
            # A tunnel URL that only exists on this laptop is not a tunnel.
            os.environ["PUBLIC_BASE_URL"] = "http://localhost:8000"
            assert missing_settings("twilio") == ["PUBLIC_BASE_URL"]
            os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:8000/"
            assert missing_settings("twilio") == ["PUBLIC_BASE_URL"]
            # The laptop's own address on the office network is not public
            # either, and this is the one a rep is most likely to paste in.
            os.environ["PUBLIC_BASE_URL"] = "http://192.168.1.10:8000"
            assert missing_settings("twilio") == ["PUBLIC_BASE_URL"]
            assert provider_ready("twilio") is False
            os.environ["PUBLIC_BASE_URL"] = "http://10.0.0.5:8000"
            assert missing_settings("twilio") == ["PUBLIC_BASE_URL"]
            os.environ["PUBLIC_BASE_URL"] = "abc123.ngrok-free.app"
            assert missing_settings("twilio") == ["PUBLIC_BASE_URL"]
            os.environ["PUBLIC_BASE_URL"] = "https://abc123.ngrok-free.app/"
            assert public_base_url() == "https://abc123.ngrok-free.app"
            assert provider_ready("twilio") is True
            assert provider_status()[3]["missing"] == []
        finally:
            for _name in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID", "TWILIO_ACCOUNT_SID",
                          "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "PUBLIC_BASE_URL"):
                os.environ.pop(_name, None)

    # == the public URL check, on its own, with no environment in the way. The
    #    home network addresses matter most: that is the address a laptop shows
    #    a rep who was told the URL has to be public, and a call started on one
    #    is billed by Twilio and then dies.
    # An empty argument means "test the configured one", so the configured value
    # has to be out of the way or this loop tests the developer's own tunnel
    # instead of the strings written below it.
    _saved_public = settings.public_base_url
    settings.public_base_url = ""
    for _bad_url in (
        "",
        "   ",
        "abc123.ngrok-free.app",
        "ftp://abc123.ngrok-free.app",
        "http://localhost:8000",
        "http://LOCALHOST:8000",
        "http://127.0.0.1:8000",
        "http://127.0.0.53:8000",
        "http://0.0.0.0:8000",
        "http://[::1]:8000",
        "http://host.docker.internal:8000",
        "http://mymac.local:8000",
        "http://api.lan",
        "http://192.168.1.10:8000",
        "http://192.168.0.44",
        "http://10.0.0.5:8000",
        "http://172.16.4.4:8000",
        "http://172.31.255.1",
        "http://169.254.1.1:8000",
        "https://[fd00::1]:8000",
        "https://[fe80::1]:8000",
        "https://[::ffff:192.168.1.10]:8000",
        "http://255.255.255.255",
    ):
        assert public_base_usable(_bad_url) is False, f"{_bad_url!r} must not count as public"
    settings.public_base_url = _saved_public

    for _good_url in (
        "https://abc123.ngrok-free.app",
        "https://abc123.ngrok-free.app/",
        "http://calls.example.com:8000",
        "https://8.8.8.8:8000",
        "https://[2001:4860:4860::8888]:8000",
    ):
        assert public_base_usable(_good_url) is True, f"{_good_url!r} must count as public"

    # Only an IP literal is judged by range. A name is left to the name rules.
    assert _is_local_ip_literal("192.168.1.10") is True
    assert _is_local_ip_literal("8.8.8.8") is False
    assert _is_local_ip_literal("abc123.ngrok-free.app") is False

    # == call states
    assert normalise_call_state("RINGING") == "ringing"
    assert normalise_call_state(None) == DEFAULT_CALL_STATE
    assert normalise_call_state("on fire") == DEFAULT_CALL_STATE

    print("app/telephony.py self test passed")
