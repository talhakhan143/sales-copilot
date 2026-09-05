"""Prospect website scraping through the keyless Jina Reader endpoint.

`https://r.jina.ai/{url}` renders any public page to clean markdown without an
API key, which is exactly what the pre call context builder needs. The optional
``JINA_API_KEY`` only raises the rate limit, it is never required.

Everything here is defensive on purpose. A scrape failure is not fatal for the
product: the caller still builds a system prompt from the knowledge base alone,
so :func:`scrape_url` never raises. It always returns a :class:`ScrapeResult`,
with ``ok=False`` and a readable ``error`` when something went wrong.

There is also an SSRF guard. Jina fetches the page server side, so a private
address would not actually reach our intranet, but refusing local targets keeps
the app from being turned into a probe and stops obvious user mistakes such as
pasting ``http://localhost:3000`` into the prospect field.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.services.groq_client import normalise_output

logger = logging.getLogger(__name__)

__all__ = [
    "ScrapeResult",
    "clean_markdown",
    "is_public_host",
    "normalize_url",
    "scrape_url",
]

# ===========================================================================
# Constants
# ===========================================================================

#: Total request timeout for one Jina fetch.
REQUEST_TIMEOUT_SECONDS: Final[float] = 25.0

#: Pause before the single retry.
RETRY_DELAY_SECONDS: Final[float] = 0.6

#: Ceiling on the SSRF guard's DNS lookup. The platform resolver can block for
#: tens of seconds against a black holed nameserver, and this check runs on the
#: request path before any scrape timeout applies, so it needs its own bound.
DNS_TIMEOUT_SECONDS: Final[float] = 3.0

#: A 200 response shorter than this is an error page, not an article.
MIN_USEFUL_BODY_CHARS: Final[int] = 80

#: Appended when the cleaned text had to be cut.
TRUNCATION_SUFFIX: Final[str] = "\n\n[...truncated]"

#: Links whose URL is longer than this lose the URL and keep only the text.
MAX_INLINE_LINK_URL: Final[int] = 60

#: Host names that are always refused.
_BLOCKED_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "broadcasthost",
    }
)

#: Host suffixes that are always refused.
_BLOCKED_SUFFIXES: Final[tuple[str, ...]] = (
    ".local",
    ".localhost",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home.arpa",
    ".corp",
    ".private",
    ".test",
    ".invalid",
)

#: Statuses from Jina that deserve the one retry.
_RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({408, 409, 429, 500, 502, 503, 504, 524})

#: Matches an explicit scheme prefix, so "localhost:8000/x" is not mistaken for
#: a URL whose scheme is "localhost".
_SCHEME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")

#: Markdown image syntax, dropped entirely.
_IMAGE_RE: Final[re.Pattern[str]] = re.compile(r"!\[[^\]]*\]\([^)\s]*(?:\s+\"[^\"]*\")?\)")

#: Markdown inline link syntax, kept or unwrapped depending on the URL length.
_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[([^\]]*)\]\(([^)\s]*)(\s+\"[^\"]*\")?\)")

#: Pure navigation chrome that adds nothing to the prompt.
_NAV_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(home|about|about us|contact|contact us|login|log in|sign in|sign up|signup|"
    r"register|menu|skip to content|skip to main content|search|share|subscribe|"
    r"cookie policy|privacy policy|terms of service|back to top)$",
    re.IGNORECASE,
)

#: Header block Jina puts above the article body.
_HEADER_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(title|url source|published time|markdown content|warning|images|links):\s*",
    re.IGNORECASE,
)

_TITLE_RE: Final[re.Pattern[str]] = re.compile(r"^title:\s*(.+)$", re.IGNORECASE)
_H1_RE: Final[re.Pattern[str]] = re.compile(r"^#\s+(.+?)\s*#*$")
_BLANK_RUN_RE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")
_BULLET_RE: Final[re.Pattern[str]] = re.compile(r"^[-*+•]\s+")

_KEY_HINT: Final[str] = (
    "Jina Reader is rate limiting the keyless tier. "
    "Set JINA_API_KEY in backend/.env for a higher limit, or try again in a minute."
)


# ===========================================================================
# Result
# ===========================================================================


@dataclass
class ScrapeResult:
    """Outcome of one prospect page fetch.

    Attributes:
        ok: True when usable text was extracted.
        url: The normalized URL that was requested.
        title: Page title when Jina reported one, otherwise None.
        text: Cleaned markdown text, empty on failure.
        chars: Length of ``text``.
        error: Readable failure reason, or None when ``ok`` is True.
    """

    ok: bool
    url: str
    title: str | None
    text: str
    chars: int
    error: str | None


def _failure(url: str, error: str) -> ScrapeResult:
    """Build a failed :class:`ScrapeResult`.

    Args:
        url: The URL that was attempted, normalized when possible.
        error: Readable failure reason.

    Returns:
        A ScrapeResult with ``ok=False`` and no text.
    """
    logger.info("scrape failed for %s: %s", url or "<empty>", error)
    return ScrapeResult(ok=False, url=url, title=None, text="", chars=0, error=error)


# ===========================================================================
# URL handling
# ===========================================================================


def normalize_url(raw: str) -> str:
    """Normalise user supplied URL text into an absolute http(s) URL.

    Whitespace is stripped, a missing scheme becomes ``https://``, the scheme
    and host are lower cased and the fragment is dropped. Anything that is not
    http or https, or that carries no host, is rejected.

    Args:
        raw: URL text as typed by the user.

    Returns:
        The normalized absolute URL.

    Raises:
        ValueError: When the input is empty, has a non http(s) scheme, or has
            no usable host.
    """
    candidate = (raw or "").strip().strip("<>").strip()
    if not candidate:
        raise ValueError("URL is empty")
    if any(ch.isspace() for ch in candidate):
        raise ValueError("URL contains whitespace")

    if not _SCHEME_RE.match(candidate):
        candidate = f"https://{candidate}"

    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Only http and https URLs are supported, got {parts.scheme!r}")

    try:
        host = parts.hostname
    except ValueError as exc:
        raise ValueError(f"Invalid host in URL: {exc}") from exc
    if not host:
        raise ValueError("URL has no host")

    netloc = host.lower()
    if parts.port is not None:
        if (scheme == "http" and parts.port != 80) or (scheme == "https" and parts.port != 443):
            netloc = f"{netloc}:{parts.port}"
    if ":" in host:
        # IPv6 literal, restore the brackets the netloc form needs.
        netloc = netloc.replace(host.lower(), f"[{host.lower()}]", 1)

    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _classify_ip(address: str) -> bool:
    """Report whether a literal IP address is safe to fetch.

    Args:
        address: An IPv4 or IPv6 address in text form.

    Returns:
        True when the address is a normal public address.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False

    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped

    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def is_public_host(host: str) -> bool:
    """Report whether a host resolves to a public internet address.

    DNS resolution runs in the default executor so the event loop is never
    blocked by a slow resolver, and it is bounded by
    :data:`DNS_TIMEOUT_SECONDS` so a host whose nameserver never answers cannot
    hold the caller open. The check fails closed: a resolver timeout, any
    resolver error, any empty answer and any private, loopback, link local,
    reserved, multicast or unspecified address all return False.

    Args:
        host: Host name or IP literal taken from the URL.

    Returns:
        True only when every resolved address is public.
    """
    cleaned = (host or "").strip().lower().strip("[]").rstrip(".")
    if not cleaned:
        return False
    if cleaned in _BLOCKED_HOSTS:
        return False
    if any(cleaned.endswith(suffix) for suffix in _BLOCKED_SUFFIXES):
        return False

    try:
        ipaddress.ip_address(cleaned)
    except ValueError:
        pass
    else:
        return _classify_ip(cleaned)

    loop = asyncio.get_running_loop()

    def _resolve() -> list[tuple[int, int, int, str, tuple[object, ...]]]:
        return socket.getaddrinfo(cleaned, None, proto=socket.IPPROTO_TCP)

    try:
        infos = await asyncio.wait_for(
            loop.run_in_executor(None, _resolve),
            timeout=DNS_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.info(
            "refusing %s, DNS resolution did not answer within %.1fs",
            cleaned,
            DNS_TIMEOUT_SECONDS,
        )
        return False
    except Exception as exc:  # noqa: BLE001 - fail closed on any resolver problem
        logger.info("refusing %s, DNS resolution failed: %s", cleaned, exc)
        return False

    addresses = [str(info[4][0]) for info in infos if info[4]]
    if not addresses:
        return False

    for address in addresses:
        if not _classify_ip(address):
            logger.info("refusing %s, resolves to non public address %s", cleaned, address)
            return False
    return True


# ===========================================================================
# Markdown cleaning
# ===========================================================================


def _unwrap_link(match: re.Match[str]) -> str:
    """Keep short markdown links intact and unwrap long ones to their text.

    Args:
        match: A match of :data:`_LINK_RE`.

    Returns:
        The original link, or just the link text when the URL is long.
    """
    text = match.group(1)
    url = match.group(2) or ""
    if len(url) > MAX_INLINE_LINK_URL:
        return text
    return match.group(0)


def _is_nav_line(line: str) -> bool:
    """Report whether a line is pure navigation chrome.

    Args:
        line: One already stripped line of markdown.

    Returns:
        True when the line should be dropped.
    """
    bare = _BULLET_RE.sub("", line).strip()
    bare = bare.strip("*_`#").strip()
    bare = bare.rstrip(".:|>").strip()
    if not bare:
        return False
    return bool(_NAV_LINE_RE.match(bare))


def _extract_title(raw: str) -> str | None:
    """Pull the page title out of the raw Jina response.

    Prefers the ``Title:`` line Jina emits, and falls back to the first level
    one markdown heading.

    Args:
        raw: The raw response body.

    Returns:
        The title, or None when neither form is present.
    """
    lines = raw.splitlines()
    for line in lines[:12]:
        match = _TITLE_RE.match(line.strip())
        if match:
            title = match.group(1).strip()
            if title:
                return title[:300]
    for line in lines:
        match = _H1_RE.match(line.strip())
        if match:
            title = match.group(1).strip()
            if title:
                return title[:300]
    return None


def _truncate_on_word_boundary(text: str, max_chars: int) -> str:
    """Cut text to a maximum length without slicing a word in half.

    Args:
        text: The text to cut.
        max_chars: Maximum number of characters to keep before the suffix.

    Returns:
        The text unchanged when it fits, otherwise the cut text plus
        :data:`TRUNCATION_SUFFIX`.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    window = text[:max_chars]
    boundary = max(window.rfind(" "), window.rfind("\n"))
    if boundary < int(max_chars * 0.6):
        boundary = max_chars
    return window[:boundary].rstrip() + TRUNCATION_SUFFIX


def clean_markdown(raw: str, max_chars: int) -> tuple[str, str | None]:
    """Turn a raw Jina Reader response into prompt ready text.

    The cleaning steps, in order: extract the title, drop the Jina header block,
    strip markdown images, unwrap long links to their anchor text, drop pure
    navigation lines, trim trailing spaces, collapse three or more blank lines
    into two, and truncate on a word boundary.

    Args:
        raw: The raw response body from Jina Reader.
        max_chars: Maximum characters to keep. Values of zero or less mean no
            truncation.

    Returns:
        A tuple of ``(cleaned_text, title)`` where title may be None.
    """
    if not raw:
        return "", None

    title = _extract_title(raw)

    body = raw.replace("\r\n", "\n").replace("\r", "\n")
    body = _IMAGE_RE.sub("", body)
    body = _LINK_RE.sub(_unwrap_link, body)

    kept: list[str] = []
    header_zone = True
    for line in body.split("\n"):
        stripped = line.strip()
        if header_zone:
            if _HEADER_LINE_RE.match(stripped):
                continue
            if stripped:
                header_zone = False
        if _is_nav_line(stripped):
            continue
        kept.append(line.rstrip())

    cleaned = "\n".join(kept)
    cleaned = _BLANK_RUN_RE.sub("\n\n", cleaned).strip()
    cleaned = normalise_output(cleaned)
    cleaned = _truncate_on_word_boundary(cleaned, max_chars)
    return cleaned, normalise_output(title) if title else title


# ===========================================================================
# Fetch
# ===========================================================================


def _reader_url(base_url: str, normalized: str) -> str:
    """Build the Jina Reader URL for a target page.

    Args:
        base_url: Reader root, usually ``https://r.jina.ai/``.
        normalized: The normalized absolute target URL.

    Returns:
        The full reader URL.
    """
    base = (base_url or "https://r.jina.ai/").strip()
    if not base.endswith("/"):
        base = f"{base}/"
    return f"{base}{normalized}"


def _looks_like_html(body: str) -> bool:
    """Report whether a body is an HTML error page rather than markdown.

    Args:
        body: The response body text.

    Returns:
        True when the body opens with an HTML document.
    """
    head = body.lstrip()[:200].lower()
    return head.startswith("<!doctype html") or head.startswith("<html") or "<head>" in head


async def scrape_url(
    url: str,
    *,
    max_chars: int,
    api_key: str = "",
    base_url: str = "https://r.jina.ai/",
) -> ScrapeResult:
    """Fetch a prospect page through Jina Reader and clean it for the prompt.

    This function never raises. Bad input, blocked hosts, network failures,
    rate limits and unreadable pages all come back as a ``ScrapeResult`` with
    ``ok=False`` and a readable ``error``, because a scrape failure must not
    break the pre call flow.

    Args:
        url: The prospect URL as typed by the user.
        max_chars: Maximum characters of cleaned text to keep.
        api_key: Optional Jina API key, which only raises the rate limit.
        base_url: Reader root, usually ``https://r.jina.ai/``.

    Returns:
        A :class:`ScrapeResult`. On success ``text`` holds the cleaned markdown.
    """
    try:
        normalized = normalize_url(url)
    except ValueError as exc:
        return _failure((url or "").strip(), f"Invalid URL: {exc}")

    host = urlsplit(normalized).hostname or ""
    try:
        allowed = await is_public_host(host)
    except Exception as exc:  # noqa: BLE001 - fail closed, never break the caller
        return _failure(normalized, f"Could not verify the host {host}: {exc}")
    if not allowed:
        return _failure(
            normalized,
            f"Refusing to fetch {host}, it is a local or non public address.",
        )

    target = _reader_url(base_url, normalized)
    # No X-Return-Format header on purpose. Jina's default response carries a
    # "Title:" / "URL Source:" header block followed by "Markdown Content:",
    # which is the only place the page title is available. Asking for "text"
    # drops that block and returns more nav noise, and asking for "markdown"
    # explicitly returns the raw link soup. Measured on stripe.com:
    # default 7.6k chars with a title, text 12.4k with none, markdown 30.8k.
    headers = {
        "Accept": "text/plain",
        "X-Timeout": "20",
        "User-Agent": "sales-copilot/1.0",
    }
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=10.0),
            follow_redirects=True,
        ) as client:
            body = ""
            status = 0
            last_error = ""

            for attempt in range(2):
                try:
                    response = await client.get(target, headers=headers)
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    logger.info("jina attempt %d failed: %s", attempt + 1, last_error)
                    if attempt == 0:
                        await asyncio.sleep(RETRY_DELAY_SECONDS)
                        continue
                    return _failure(normalized, f"Could not reach Jina Reader. {last_error}")

                status = response.status_code
                body = response.text or ""

                if status == 200:
                    break

                snippet = " ".join(body.split())[:200]
                last_error = f"Jina Reader returned HTTP {status}. {snippet}".strip()
                if status in (401, 402, 403, 429):
                    last_error = f"{last_error} {_KEY_HINT}".strip()
                if status in _RETRYABLE_STATUSES and attempt == 0:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue
                return _failure(normalized, last_error)

            if status != 200:
                return _failure(normalized, last_error or "Jina Reader did not return a page.")

            flat = " ".join(body.split())
            if "rate limit" in flat.lower():
                return _failure(normalized, _KEY_HINT)
            if _looks_like_html(body):
                return _failure(
                    normalized,
                    f"Jina Reader returned an HTML error page instead of text. {_KEY_HINT}",
                )
            if len(flat) < MIN_USEFUL_BODY_CHARS:
                return _failure(
                    normalized,
                    "Jina Reader returned an almost empty page, the site may block readers. "
                    f"{_KEY_HINT}",
                )

            text, title = clean_markdown(body, max_chars)
            if not text.strip():
                return _failure(
                    normalized,
                    "Nothing readable was left after cleaning the page.",
                )

            logger.info(
                "scraped %s, title=%r, %d chars",
                normalized,
                title,
                len(text),
            )
            return ScrapeResult(
                ok=True,
                url=normalized,
                title=title,
                text=text,
                chars=len(text),
                error=None,
            )
    except httpx.HTTPError as exc:
        return _failure(normalized, f"Could not reach Jina Reader. {type(exc).__name__}: {exc}")
    except ValueError as exc:
        return _failure(normalized, f"Bad response from Jina Reader: {exc}")
    except asyncio.TimeoutError:
        return _failure(normalized, "Jina Reader timed out after 25 seconds.")
    except Exception as exc:  # noqa: BLE001 - scraping must never break the caller
        logger.exception("unexpected scrape failure for %s", normalized)
        return _failure(normalized, f"Unexpected scrape failure: {type(exc).__name__}: {exc}")
