"""Fuse the knowledge base, the scraped prospect page and the call goal.

This is the one place that turns a ``POST /api/prepare-context`` request into a
live session: it optionally scrapes the prospect URL through Jina Reader, clamps
the knowledge base, builds the system prompt and registers the session in the
shared in memory store.

Import rule: nothing from ``app.api`` may be imported here, the API layer imports
this module and the reverse would be a cycle.
"""

from __future__ import annotations

import asyncio
import logging

from app import prompts
from app.config import settings
from app.models import PrepareContextRequest, PrepareContextResponse
from app.services.jina import scrape_url
from app.services.session_store import store

logger = logging.getLogger(__name__)

TRUNCATION_MARKER = "\n\n[...truncated]"
"""Appended to any text we had to cut, so the model knows it is looking at a slice."""

EXCERPT_CHARS = 600
"""How much of the scraped page the frontend preview card gets."""

SCRAPE_BUDGET_SECONDS = 30.0
"""Outer timeout around the scrape so the REST call can never hang forever."""

FALLBACK_CALL_GOAL = "Book a short discovery call with the prospect."
"""Used when the caller did not supply a call goal."""

_NO_CLIENT_INFO_FALLBACK = (
    "No public information was fetched for this prospect. Ask discovery questions "
    "before making claims."
)
"""Literal fallback used only if ``app.prompts`` does not export ``NO_CLIENT_INFO``."""

LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ur": "Urdu",
    "hi": "Hindi",
    "es": "Spanish",
    "ar": "Arabic",
    "fr": "French",
    "de": "German",
}
"""ISO 639-1 code to human readable name, matching the frontend language select."""


def _no_client_info() -> str:
    """Return the "nothing was scraped" block for the system prompt.

    Returns:
        ``prompts.NO_CLIENT_INFO`` when that constant exists, otherwise the
        literal fallback frozen in the contract.
    """
    value = getattr(prompts, "NO_CLIENT_INFO", _NO_CLIENT_INFO_FALLBACK)
    return value if isinstance(value, str) and value.strip() else _NO_CLIENT_INFO_FALLBACK


def normalize_language(raw: str | None) -> tuple[str, str]:
    """Normalize a language input into a code and a display name.

    Accepts a bare code (``en``), a locale (``en-US``) or an English name
    (``Urdu``). Unknown values are passed through so an exotic language still
    reaches Whisper instead of being silently forced to English.

    Args:
        raw: Whatever the client sent, possibly ``None``.

    Returns:
        A tuple of ``(code, display_name)``. The code is the only value that
        travels onward: it goes on the session, is later sent to Whisper, and is
        handed to ``prompts.build_system_prompt``, which expands it to a human
        name itself. The display name is for logging and for callers that want a
        label, never feed it back into ``build_system_prompt`` because that
        function maps codes, not names.
    """
    value = (raw or "").strip()
    if not value:
        return "en", LANGUAGE_NAMES["en"]
    lowered = value.lower()
    if lowered in LANGUAGE_NAMES:
        return lowered, LANGUAGE_NAMES[lowered]
    base = lowered.replace("_", "-").split("-", 1)[0]
    if base in LANGUAGE_NAMES:
        return base, LANGUAGE_NAMES[base]
    for code, name in LANGUAGE_NAMES.items():
        if name.lower() == lowered:
            return code, name
    return (base or "en")[:8], value


def clamp_on_word_boundary(text: str, limit: int, marker: str = TRUNCATION_MARKER) -> str:
    """Cut ``text`` to ``limit`` characters without splitting a word.

    Args:
        text: The source text.
        limit: Maximum characters to keep before the marker. Values at or below
            zero return the text untouched.
        marker: Suffix appended when the text was actually cut.

    Returns:
        The original text when it already fits, otherwise the clamped text with
        ``marker`` appended.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(" "), cut.rfind("\n"))
    if boundary > limit // 2:
        cut = cut[:boundary]
    return cut.rstrip() + marker


_WHAT_THE_REP_KNOWS: str = "WHAT THE REP ALREADY KNOWS ABOUT THIS CLIENT:\n"
"""Heading placed above the rep's own notes inside the client block."""


def _build_client_block(url: str, title: str | None, text: str) -> str:
    """Format the scraped prospect page for the system prompt.

    Args:
        url: The normalized prospect URL.
        title: Page title, may be missing.
        text: The cleaned page text.

    Returns:
        A block headed with the source and the title, exactly as the contract
        specifies.
    """
    heading = title.strip() if title and title.strip() else "Unknown"
    return f"SOURCE: {url}\nTITLE: {heading}\n\n{text}"


async def prepare_context(
    req: PrepareContextRequest,
    *,
    groq_configured: bool,
) -> PrepareContextResponse:
    """Build the call context and open a session.

    Scrapes the prospect URL when one was supplied (a failure there is never
    fatal), clamps the knowledge base to ``settings.max_kb_chars``, fuses the
    system prompt and registers the session in the shared store.

    Args:
        req: The validated prepare context request.
        groq_configured: Whether the Groq API key is present. Only used for a
            loud log line, the endpoint still returns a usable session so the
            frontend can be exercised without a key.

    Returns:
        The response model carrying the new session id, the fused system prompt
        and the scrape outcome.
    """
    knowledge_base = clamp_on_word_boundary(
        (req.knowledge_base or "").strip(),
        settings.max_kb_chars,
    )
    call_goal = (req.call_goal or "").strip() or FALLBACK_CALL_GOAL
    language_code, language_name = normalize_language(req.language)

    client_url: str | None = None
    client_title: str | None = None
    client_excerpt: str | None = None
    client_block: str = _no_client_info()
    scrape_chars = 0
    scrape_ok = False
    scrape_error: str | None = None

    raw_url = (req.client_url or "").strip()
    if raw_url:
        result = await _scrape(raw_url)
        client_url = result_url(result, raw_url)
        if result_ok(result):
            text = str(getattr(result, "text", "") or "").strip()
            title = getattr(result, "title", None)
            scrape_chars = int(getattr(result, "chars", 0) or len(text))
            scrape_ok = True
            client_title = title.strip() if isinstance(title, str) and title.strip() else None
            client_excerpt = text[:EXCERPT_CHARS] or None
            client_block = _build_client_block(client_url, client_title, text)
            logger.info("Scraped %s, %d chars", client_url, scrape_chars)
        else:
            default_error = (
                "The page loaded but returned no readable text."
                if getattr(result, "ok", False)
                else "Scrape failed."
            )
            scrape_error = str(getattr(result, "error", None) or default_error)
            logger.warning("Scrape failed for %s, %s", client_url, scrape_error)

    # The client may have no website at all. That is not an edge case here, it
    # is the core customer for anyone selling websites, so whatever the rep
    # typed about them carries the same weight as a scraped page and is stacked
    # on top of one when both exist.
    client_context = (req.client_context or "").strip()
    if client_context:
        notes = _WHAT_THE_REP_KNOWS + client_context
        client_block = f"{client_block}\n\n{notes}" if scrape_ok else notes

    system_prompt = prompts.build_system_prompt(
        knowledge_base=knowledge_base,
        client_block=client_block,
        call_goal=call_goal,
        language=language_code,
    )

    session = store.create(
        system_prompt=system_prompt,
        language=language_code,
        client_title=client_title,
        client_url=client_url,
    )

    if not groq_configured:
        logger.warning(
            "Session %s created without a GROQ_API_KEY, transcription and suggestions "
            "will fail until one is set.",
            session.id,
        )

    logger.info(
        "Prepared session %s, kb=%d chars, scrape_ok=%s, language=%s (%s)",
        session.id,
        len(knowledge_base),
        scrape_ok,
        language_code,
        language_name,
    )

    return PrepareContextResponse(
        session_id=session.id,
        system_prompt=system_prompt,
        client_url=client_url,
        client_title=client_title,
        client_excerpt=client_excerpt,
        scrape_chars=scrape_chars,
        scrape_ok=scrape_ok,
        scrape_error=scrape_error,
        created_at=session.created_at,
    )


async def _scrape(raw_url: str) -> object:
    """Run the Jina scrape behind a hard timeout and never raise.

    Args:
        raw_url: The URL exactly as the user typed it.

    Returns:
        A ``ScrapeResult`` from ``app.services.jina`` on success or on a handled
        failure, or a small stand in object carrying ``ok=False`` and an error
        when the scrape blew up or ran out of time.
    """
    try:
        return await asyncio.wait_for(
            scrape_url(
                raw_url,
                max_chars=settings.max_scrape_chars,
                api_key=settings.jina_api_key,
                base_url=settings.jina_base_url,
            ),
            timeout=SCRAPE_BUDGET_SECONDS,
        )
    except (asyncio.TimeoutError, TimeoutError):
        return _FailedScrape(raw_url, f"Scrape timed out after {int(SCRAPE_BUDGET_SECONDS)} seconds.")
    except ValueError as exc:
        return _FailedScrape(raw_url, f"Invalid URL, {exc}")
    except Exception as exc:  # noqa: BLE001 a scrape must never break the endpoint
        logger.exception("Unexpected scrape failure for %s", raw_url)
        return _FailedScrape(raw_url, f"{type(exc).__name__}: {exc}")


class _FailedScrape:
    """Minimal stand in for ``ScrapeResult`` when the scrape never produced one."""

    def __init__(self, url: str, error: str) -> None:
        """Create a failed result.

        Args:
            url: The URL we tried to fetch.
            error: Human readable failure reason.
        """
        self.ok = False
        self.url = url
        self.title: str | None = None
        self.text = ""
        self.chars = 0
        self.error = error


def result_ok(result: object) -> bool:
    """Return whether a scrape result carries usable text.

    Args:
        result: A ``ScrapeResult`` or the internal stand in.

    Returns:
        ``True`` only when the scrape reported success and returned non empty text.
    """
    return bool(getattr(result, "ok", False)) and bool(str(getattr(result, "text", "") or "").strip())


def result_url(result: object, fallback: str) -> str:
    """Return the normalized URL from a scrape result.

    Args:
        result: A ``ScrapeResult`` or the internal stand in.
        fallback: The raw URL to use when the result carries none.

    Returns:
        The best available URL string.
    """
    url = getattr(result, "url", None)
    if isinstance(url, str) and url.strip():
        return url.strip()
    return fallback
