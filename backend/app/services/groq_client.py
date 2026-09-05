"""Async Groq API client for speech to text and streaming chat completions.

This module owns every outbound call to the Groq REST API. It exposes a single
shared :class:`httpx.AsyncClient` that is created on FastAPI startup and closed
on shutdown, so connections are pooled across the whole process instead of being
rebuilt per utterance.

Two calls matter for the teleprompter latency budget:

* :meth:`GroqClient.transcribe` posts one closed utterance as a WAV multipart
  upload to ``/audio/transcriptions`` and returns the recognised text.
* :meth:`GroqClient.stream_chat` posts to ``/chat/completions`` with
  ``stream=True`` and yields raw content deltas as they arrive, so the first
  token can be painted on screen long before the sentence is finished.

Whisper models hallucinate confident sounding filler when they are handed near
silence ("thank you", "you", "subtitles by the amara.org community"). Those
results are filtered out here rather than in the socket layer, so every caller
gets the same protection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
import time
from collections.abc import AsyncIterator
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "GroqClient",
    "GroqError",
    "HALLUCINATION_BLOCKLIST",
    "MIN_TRANSCRIBE_MS",
    "MAX_STT_PROMPT_CHARS",
]

# ===========================================================================
# Constants
# ===========================================================================

#: Utterances shorter than this are never sent to the network. Whisper returns
#: pure hallucination for sub 200 ms clips and each call still costs a round trip.
MIN_TRANSCRIBE_MS: Final[int] = 200

#: The Whisper biasing prompt is capped well under the 224 token API limit.
MAX_STT_PROMPT_CHARS: Final[int] = 800

#: Backoff schedule for retryable failures. The first attempt is immediate, so
#: the total number of attempts is ``1 + len(RETRY_BACKOFFS)``.
RETRY_BACKOFFS: Final[tuple[float, ...]] = (0.4, 1.0)

#: Statuses that are worth a second try. Everything else (400, 401, 403, 404,
#: 413, 422) is a permanent client side mistake and retrying only wastes time.
RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({408, 409, 429})

#: Fallback WAV geometry used when a payload carries no parseable fmt chunk.
DEFAULT_SAMPLE_RATE: Final[int] = 16000
DEFAULT_CHANNELS: Final[int] = 1
DEFAULT_BITS_PER_SAMPLE: Final[int] = 16

#: Normalised strings that mean "the model heard nothing". Every entry is stored
#: in the same shape :func:`_normalise_transcript` produces: lower case, stripped
#: of surrounding whitespace and of trailing punctuation.
HALLUCINATION_BLOCKLIST: Final[frozenset[str]] = frozenset(
    {
        "",
        "you",
        "bye",
        "bye bye",
        "goodbye",
        "ok",
        "okay",
        "so",
        "uh",
        "um",
        "hmm",
        "mm",
        "mhm",
        "yeah yeah yeah",
        "thanks",
        "thank you",
        "thank you very much",
        "thanks for watching",
        "thanks for watching the video",
        "thank you for watching",
        "please subscribe",
        "like and subscribe",
        "the end",
        "silence",
        "[silence]",
        "(silence)",
        "music",
        "[music]",
        "(music)",
        "[blank_audio]",
        "blank_audio",
        "(blank_audio)",
        "[inaudible]",
        "inaudible",
        "[applause]",
        "applause",
        "subtitles by the amara.org community",
        "subtitles by the amara org community",
        "transcription by eso translations",
        "www.mooji.org",
    }
)

#: Characters trimmed from the tail of a transcript before the blocklist test.
_TRAILING_PUNCTUATION: Final[str] = " \t\r\n.,!?;:…\"'»«“”’"

#: Symbols stripped for the secondary bracket insensitive blocklist test.
_SYMBOL_STRIP_RE: Final[re.Pattern[str]] = re.compile(r"[\[\]\(\)\{\}\*_`~]")

#: Collapses any run of whitespace so "you   you" and "you you" compare equal.
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


# ===========================================================================
# Errors
# ===========================================================================


class GroqError(RuntimeError):
    """Raised when the Groq API refuses a request or the transport fails.

    Attributes:
        status: HTTP status code returned by Groq, or ``0`` when the request
            never produced a response (DNS failure, connect timeout, reset).
        message: Human readable detail, usually the decoded response body.
    """

    def __init__(self, status: int, message: str) -> None:
        """Store the status and message and build the display string.

        Args:
            status: HTTP status code, or ``0`` for transport level failures.
            message: Detail to surface to the caller and the logs.
        """
        self.status = status
        self.message = message
        label = f"HTTP {status}" if status else "transport error"
        super().__init__(f"Groq {label}: {message}")


# ===========================================================================
# Pure helpers (unit testable without a network)
# ===========================================================================


def _normalise_transcript(text: str) -> str:
    """Normalise a transcript for the hallucination blocklist test.

    Lower cases the text, collapses internal whitespace, strips surrounding
    whitespace and removes trailing punctuation, so "Thank you." and
    "  thank   you  " both become "thank you".

    Args:
        text: Raw transcript text as returned by the STT model.

    Returns:
        The normalised comparison key, possibly an empty string.
    """
    collapsed = _WHITESPACE_RE.sub(" ", text).strip().lower()
    return collapsed.rstrip(_TRAILING_PUNCTUATION).strip()


def _is_repeated_token(normalised: str) -> bool:
    """Report whether the text is one token repeated over and over.

    Whisper loops on near silence and emits things like "you you you" or
    "Thank you. Thank you. Thank you.". A run of three or more identical tokens
    is always treated as a loop. Two identical tokens only count when the token
    itself is already blocklisted, so a genuine short reply such as "no no"
    survives.

    Args:
        normalised: Output of :func:`_normalise_transcript`.

    Returns:
        True when the text looks like a repetition loop.
    """
    tokens = [tok.strip(_TRAILING_PUNCTUATION) for tok in normalised.split(" ")]
    tokens = [tok for tok in tokens if tok]
    if len(tokens) < 2:
        return False
    first = tokens[0]
    if any(tok != first for tok in tokens):
        return False
    if len(tokens) >= 3:
        return True
    return first in HALLUCINATION_BLOCKLIST


def _is_hallucination(text: str) -> bool:
    """Report whether a transcript is Whisper filler rather than real speech.

    Args:
        text: Raw transcript text as returned by the STT model.

    Returns:
        True when the text should be discarded and treated as silence.
    """
    normalised = _normalise_transcript(text)
    if len(normalised) < 2:
        return True
    if normalised in HALLUCINATION_BLOCKLIST:
        return True
    bare = _SYMBOL_STRIP_RE.sub("", normalised).strip()
    if bare and bare in HALLUCINATION_BLOCKLIST:
        return True
    return _is_repeated_token(normalised)


def _wav_geometry(wav: bytes) -> tuple[int, int, int, int]:
    """Extract the payload size and PCM geometry from a RIFF WAVE buffer.

    The parser walks the chunk list instead of assuming a fixed 44 byte header,
    so a WAV carrying a LIST or fact chunk is still measured correctly. Anything
    that is not a RIFF WAVE is treated as raw 16 kHz mono 16 bit PCM.

    Args:
        wav: The full WAV file bytes, or raw PCM bytes.

    Returns:
        A tuple of ``(payload_bytes, sample_rate, channels, bits_per_sample)``.
    """
    sample_rate = DEFAULT_SAMPLE_RATE
    channels = DEFAULT_CHANNELS
    bits = DEFAULT_BITS_PER_SAMPLE
    total = len(wav)

    if total < 12 or wav[0:4] != b"RIFF" or wav[8:12] != b"WAVE":
        return total, sample_rate, channels, bits

    payload = 0
    pos = 12
    while pos + 8 <= total:
        chunk_id = wav[pos : pos + 4]
        (chunk_size,) = struct.unpack_from("<I", wav, pos + 4)
        body = pos + 8
        available = max(0, total - body)
        size = min(chunk_size, available)
        if chunk_id == b"fmt " and size >= 16:
            (
                _fmt_tag,
                fmt_channels,
                fmt_rate,
                _byte_rate,
                _block_align,
                fmt_bits,
            ) = struct.unpack_from("<HHIIHH", wav, body)
            channels = fmt_channels or DEFAULT_CHANNELS
            sample_rate = fmt_rate or DEFAULT_SAMPLE_RATE
            bits = fmt_bits or DEFAULT_BITS_PER_SAMPLE
        elif chunk_id == b"data":
            payload = size
            break
        pos = body + size + (size % 2)

    if payload == 0:
        payload = max(0, total - 44)
    return payload, sample_rate, channels, bits


def _wav_duration_ms(wav: bytes) -> float:
    """Compute the audio duration of a WAV or raw PCM buffer in milliseconds.

    Args:
        wav: The full WAV file bytes, or raw PCM bytes.

    Returns:
        Duration in milliseconds, ``0.0`` when the buffer carries no samples.
    """
    payload, sample_rate, channels, bits = _wav_geometry(wav)
    frame_bytes = max(1, channels * max(1, bits // 8))
    frames = payload // frame_bytes
    if frames <= 0 or sample_rate <= 0:
        return 0.0
    return (frames / float(sample_rate)) * 1000.0


def _decode_body(raw: bytes | str, limit: int = 700) -> str:
    """Decode a response body for an error message, safely and briefly.

    Args:
        raw: Response body as bytes or text.
        limit: Maximum number of characters kept.

    Returns:
        A single line, length capped string suitable for a log or an error.
    """
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > limit:
        return text[:limit] + " [...]"
    return text


def _is_retryable_status(status: int) -> bool:
    """Report whether an HTTP status is worth retrying.

    Permanent client errors (400, 401, 403, 404 and friends) return False so a
    bad API key fails instantly instead of after three round trips.

    Args:
        status: HTTP status code from the response.

    Returns:
        True for 408, 409, 429 and any 5xx status.
    """
    return status in RETRYABLE_STATUSES or 500 <= status <= 599


def _short_language(language: str) -> str:
    """Reduce a locale tag to the ISO 639 1 code Whisper expects.

    Args:
        language: Language code such as ``"en"``, ``"en-US"`` or ``"ur_PK"``.

    Returns:
        The lower cased primary subtag, or an empty string when there is none.
    """
    cleaned = language.strip().lower().replace("_", "-")
    if not cleaned:
        return ""
    return cleaned.split("-", 1)[0]


# ---------------------------------------------------------------------------
# Reasoning suppression and teleprompter output hygiene
# ---------------------------------------------------------------------------

_THINK_OPEN = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think\s*>", re.IGNORECASE)

#: Longest partial tag prefix the sanitiser may need to hold back ("</think>").
_HOLDBACK = 8

#: Characters a non native speaker should never meet on a teleprompter, mapped
#: to the plain ASCII they will actually be able to read at a glance. The em
#: dash is handled separately because it also needs its surrounding spaces.
_TYPOGRAPHY = {
    "\u2013": "-",   # en dash
    "\u2011": "-",   # non breaking hyphen, gpt-oss emits these constantly
    "\u2012": "-",   # figure dash
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u2026": "...",
    "\u00a0": " ",   # non breaking space
    "\u202f": " ",   # narrow no break space
    "\u2009": " ",   # thin space
}

_EM_DASH = re.compile(r"\s*\u2014\s*")

#: Characters held back at the tail of a chunk so the em dash rewrite can see
#: what follows before it commits.
_UNSAFE_TAIL = " \t\n\r\u2014\u2013\u2011\u2012"


def reasoning_params(model: str) -> dict[str, Any]:
    """Per model flags that keep chain of thought out of the token stream.

    Every chat model Groq currently serves is a reasoning model, and each
    family leaks its thinking a different way:

    ``openai/gpt-oss-*``
        Streams thinking in a separate ``delta.reasoning`` field. It never
        reaches the teleprompter, but it delays the first real token by 400 to
        600 ms. ``reasoning_effort: low`` with ``reasoning_format: hidden``
        shortens it and keeps it out of the response.
    ``qwen/qwen3.*``
        Streams ``<think> ... </think>`` inside ``delta.content``, which would
        put the model's private notes straight onto the teleprompter.
        ``reasoning_effort: none`` turns it off at the source.

    Anything else gets no extra keys, because Groq rejects an unknown
    parameter with a 400 rather than ignoring it.

    Args:
        model: The chat model id.

    Returns:
        Extra payload keys to merge into the chat completion request.
    """
    name = model.lower()
    if name.startswith("openai/gpt-oss"):
        return {"reasoning_effort": "low", "reasoning_format": "hidden"}
    if name.startswith("qwen/"):
        return {"reasoning_effort": "none"}
    return {}


def normalise_output(text: str) -> str:
    """Rewrite model punctuation into plain, readable ASCII.

    Args:
        text: A fragment of model output.

    Returns:
        The fragment with em dashes turned into commas and every other
        typographic character folded to its ASCII equivalent.
    """
    if not text:
        return text
    text = _EM_DASH.sub(", ", text)
    for src, dst in _TYPOGRAPHY.items():
        if src in text:
            text = text.replace(src, dst)
    return text


class StreamSanitizer:
    """Incremental filter sitting between the model and the teleprompter.

    Two jobs, both of which have to survive chunk boundaries falling anywhere:

    1. Drop inline chain of thought. Some models stream ``<think> ... </think>``
       inside the content field, and a tag can be split across two deltas.
    2. Normalise typography through :func:`normalise_output`. An em dash needs
       to see the character after it before it can be rewritten, so a trailing
       run of whitespace and dashes is held back until the next chunk arrives.

    Call :meth:`push` for every delta and :meth:`flush` once at the end.
    """

    __slots__ = ("_buf", "_in_think", "_started")

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False
        self._started = False

    def push(self, chunk: str) -> str:
        """Feed one raw delta and get back the part that is safe to display.

        Args:
            chunk: A raw content delta straight off the SSE stream.

        Returns:
            Cleaned text, possibly empty when everything was held back.
        """
        self._buf += chunk
        out: list[str] = []
        while True:
            if self._in_think:
                match = _THINK_CLOSE.search(self._buf)
                if match is None:
                    # Keep only enough tail to recognise a split closing tag.
                    if len(self._buf) > _HOLDBACK:
                        self._buf = self._buf[-_HOLDBACK:]
                    break
                self._buf = self._buf[match.end():]
                self._in_think = False
                continue

            match = _THINK_OPEN.search(self._buf)
            if match is not None:
                out.append(self._buf[: match.start()])
                self._buf = self._buf[match.end():]
                self._in_think = True
                continue

            cut = len(self._buf)
            tail = self._buf[-_HOLDBACK:]
            angle = tail.rfind("<")
            if angle != -1:
                cut = len(self._buf) - len(tail) + angle
            while cut > 0 and self._buf[cut - 1] in _UNSAFE_TAIL:
                cut -= 1
            out.append(self._buf[:cut])
            self._buf = self._buf[cut:]
            break
        return self._emit("".join(out))

    def flush(self) -> str:
        """Release whatever is still held back.

        Returns:
            The remaining cleaned text, or an empty string when the stream
            ended inside an unterminated think block.
        """
        rest = "" if self._in_think else self._buf
        self._buf = ""
        self._in_think = False
        return self._emit(rest).rstrip()

    def _emit(self, text: str) -> str:
        """Normalise a fragment and swallow any leading blank space.

        A stripped think block leaves the newlines that surrounded it behind,
        which would open the teleprompter with an empty line. Whitespace is
        dropped until the first real character has been sent.

        Args:
            text: The raw fragment about to be displayed.

        Returns:
            The cleaned fragment.
        """
        cleaned = normalise_output(text)
        if not self._started:
            cleaned = cleaned.lstrip()
            if not cleaned:
                return ""
            self._started = True
        return cleaned


def _delta_from_chunk(chunk: dict[str, Any]) -> str | None:
    """Dig the content delta out of one SSE chunk, defensively.

    Groq is OpenAI compatible but chunks in the wild are not always shaped the
    way the docs promise. Role only chunks, empty choice lists and finish frames
    all appear, so every level is checked before it is indexed.

    Args:
        chunk: One decoded JSON object from the SSE stream.

    Returns:
        The text delta when present and non empty, otherwise None.
    """
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None

    delta = first.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str) and content:
            return content

    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content:
            return content

    text = first.get("text")
    if isinstance(text, str) and text:
        return text
    return None


def _error_from_chunk(chunk: dict[str, Any]) -> str | None:
    """Extract an inline error message from an SSE chunk when present.

    Groq can start a 200 response and then push an error object mid stream, for
    example when the token budget for the minute runs out.

    Args:
        chunk: One decoded JSON object from the SSE stream.

    Returns:
        The error message when the chunk carries one, otherwise None.
    """
    error = chunk.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
        return _decode_body(json.dumps(error))
    if isinstance(error, str) and error:
        return error
    return None


# ===========================================================================
# Client
# ===========================================================================


class GroqClient:
    """Shared async client for the Groq speech and chat endpoints.

    One instance lives on ``app.state.groq`` for the lifetime of the process.
    :meth:`startup` opens the pooled transport, :meth:`shutdown` closes it.
    """

    def __init__(self, api_key: str, base_url: str, stt_model: str, llm_model: str) -> None:
        """Configure the client without touching the network.

        Args:
            api_key: Groq API key. An empty string leaves the client unconfigured.
            base_url: API root, for example ``https://api.groq.com/openai/v1``.
            stt_model: Whisper model id used by :meth:`transcribe`.
            llm_model: Chat model id used by :meth:`stream_chat`.
        """
        self._api_key = (api_key or "").strip()
        self._base_url = (base_url or "").strip().rstrip("/")
        self._stt_model = stt_model
        self._llm_model = llm_model
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    # == lifecycle

    async def startup(self) -> None:
        """Open the shared HTTP transport. Safe to call more than once."""
        async with self._lock:
            if self._client is not None and not self._client.is_closed:
                return
            self._client = self._build_client()
            logger.info(
                "Groq client ready (base_url=%s, stt=%s, llm=%s, configured=%s)",
                self._base_url,
                self._stt_model,
                self._llm_model,
                self.configured,
            )

    async def shutdown(self) -> None:
        """Close the shared HTTP transport and release pooled connections."""
        async with self._lock:
            client = self._client
            self._client = None
        if client is not None and not client.is_closed:
            await client.aclose()
            logger.info("Groq client closed")

    @property
    def configured(self) -> bool:
        """Whether an API key is present.

        Returns:
            True when a non empty API key was supplied.
        """
        return bool(self._api_key)

    @property
    def stt_model(self) -> str:
        """The configured speech to text model id."""
        return self._stt_model

    @property
    def llm_model(self) -> str:
        """The configured chat completion model id."""
        return self._llm_model

    # == internals

    def _build_client(self) -> httpx.AsyncClient:
        """Create the pooled httpx client with the contract timeouts.

        Returns:
            A configured :class:`httpx.AsyncClient`.
        """
        return httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=15.0, pool=5.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            headers={"User-Agent": "sales-copilot/1.0"},
        )

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Return the shared client, opening it lazily if startup was skipped.

        Returns:
            The live :class:`httpx.AsyncClient`.
        """
        client = self._client
        if client is not None and not client.is_closed:
            return client
        logger.warning("Groq client used before startup, opening transport lazily")
        await self.startup()
        client = self._client
        if client is None:
            raise GroqError(0, "HTTP transport could not be created")
        return client

    def _headers(self) -> dict[str, str]:
        """Build the authorization headers for a Groq call.

        Returns:
            A header mapping carrying the bearer token.
        """
        return {"Authorization": f"Bearer {self._api_key}"}

    def _require_key(self) -> None:
        """Raise a clear error when no API key is configured."""
        if not self.configured:
            raise GroqError(
                401,
                "GROQ_API_KEY is not set. Add it to backend/.env and restart the server.",
            )

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        label: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send a non streaming request, retrying only transient failures.

        Permanent statuses (400, 401, 403, 404 and the rest) raise immediately.
        Transient ones (408, 409, 429, 5xx) and :class:`httpx.TransportError`
        are retried on the :data:`RETRY_BACKOFFS` schedule.

        Args:
            method: HTTP verb.
            url: Absolute request URL.
            label: Short name used in the debug logs.
            **kwargs: Passed straight through to ``httpx.AsyncClient.request``.

        Returns:
            The successful response.

        Raises:
            GroqError: On a permanent status, or after the retries are spent.
        """
        client = await self._ensure_client()
        attempts = 1 + len(RETRY_BACKOFFS)
        last_status = 0
        last_error = ""

        for attempt in range(attempts):
            try:
                response = await client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last_status = 0
                last_error = f"{type(exc).__name__}: {exc}"
                logger.debug(
                    "groq %s attempt %d/%d transport failure: %s",
                    label,
                    attempt + 1,
                    attempts,
                    last_error,
                )
            except httpx.HTTPError as exc:
                raise GroqError(0, f"{type(exc).__name__}: {exc}") from exc
            else:
                if response.status_code == 200:
                    return response
                body = _decode_body(response.text)
                last_status = response.status_code
                last_error = body
                if not _is_retryable_status(response.status_code):
                    raise GroqError(response.status_code, body)
                logger.debug(
                    "groq %s attempt %d/%d retryable status %d: %s",
                    label,
                    attempt + 1,
                    attempts,
                    response.status_code,
                    body,
                )

            if attempt < attempts - 1:
                await asyncio.sleep(RETRY_BACKOFFS[attempt])

        raise GroqError(last_status, last_error or "request failed after retries")

    # == speech to text

    async def transcribe(
        self,
        wav: bytes,
        *,
        language: str = "en",
        prompt: str | None = None,
    ) -> str:
        """Transcribe one closed utterance with the Whisper endpoint.

        Very short buffers are rejected locally: anything under
        :data:`MIN_TRANSCRIBE_MS` of audio returns an empty string without a
        network call, because Whisper answers those with pure filler. The
        returned text is also run through the hallucination filter, so an empty
        string means "nothing was actually said".

        Args:
            wav: A complete RIFF WAVE buffer, 16 bit mono PCM.
            language: ISO 639 1 language hint, for example ``"en"`` or ``"ur"``.
                Locale tags such as ``"en-US"`` are reduced to their primary
                subtag automatically.
            prompt: Optional biasing text (the last rep utterance plus product
                nouns) so brand names transcribe correctly. Truncated to
                :data:`MAX_STT_PROMPT_CHARS` characters and omitted when empty.

        Returns:
            The stripped transcript, or ``""`` when nothing usable was heard.

        Raises:
            GroqError: When the API rejects the request or the transport fails.
        """
        if not wav:
            return ""

        duration_ms = _wav_duration_ms(wav)
        if duration_ms < MIN_TRANSCRIBE_MS:
            logger.debug(
                "skipping STT, utterance is only %.0f ms (minimum %d ms)",
                duration_ms,
                MIN_TRANSCRIBE_MS,
            )
            return ""

        self._require_key()

        data: dict[str, str] = {
            "model": self._stt_model,
            "response_format": "json",
            "temperature": "0",
        }
        short_language = _short_language(language)
        if short_language:
            data["language"] = short_language
        if prompt:
            trimmed = prompt.strip()[:MAX_STT_PROMPT_CHARS]
            if trimmed:
                data["prompt"] = trimmed

        files = {"file": ("utterance.wav", wav, "audio/wav")}
        url = f"{self._base_url}/audio/transcriptions"

        started = time.perf_counter()
        response = await self._request_with_retry(
            "POST",
            url,
            label="transcribe",
            files=files,
            data=data,
            headers=self._headers(),
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise GroqError(200, f"invalid JSON from STT: {_decode_body(response.text)}") from exc

        raw_text = payload.get("text") if isinstance(payload, dict) else None
        text = raw_text.strip() if isinstance(raw_text, str) else ""

        if _is_hallucination(text):
            logger.debug(
                "STT %.0f ms audio, %.0f ms latency, dropped hallucination %r",
                duration_ms,
                elapsed_ms,
                text,
            )
            return ""

        logger.debug(
            "STT %.0f ms audio, %.0f ms latency, %d chars",
            duration_ms,
            elapsed_ms,
            len(text),
        )
        return text

    # == chat completions

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        """Stream chat completion deltas from Groq as they arrive.

        The SSE framing is parsed by hand: comment lines starting with ``":"``
        and blank keep alive lines are skipped, the ``data: `` prefix is
        stripped, ``[DONE]`` ends the stream and malformed JSON lines are
        ignored rather than killing the generator mid sentence.

        The generator is cancellation safe. :class:`asyncio.CancelledError` is
        never swallowed, and the ``async with`` around the response guarantees
        the connection is released when a barge in cancels the task.

        Args:
            messages: OpenAI style message list (system first, then the window).
            max_tokens: Hard ceiling on generated tokens.
            temperature: Sampling temperature.

        Yields:
            Content deltas in order. Deltas may be partial words, append them
            verbatim.

        Raises:
            GroqError: When the API returns a non 200 status, or pushes an
                error object inside the stream.
        """
        self._require_key()
        client = await self._ensure_client()

        payload: dict[str, Any] = {
            "model": self._llm_model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        # Keep the model's private reasoning out of the token stream. Without
        # this the teleprompter either shows the chain of thought or waits
        # several hundred milliseconds for it to finish.
        payload.update(reasoning_params(self._llm_model))
        sanitizer = StreamSanitizer()
        url = f"{self._base_url}/chat/completions"

        started = time.perf_counter()
        first_token_ms: float | None = None
        chars = 0

        try:
            async with client.stream(
                "POST",
                url,
                json=payload,
                headers=self._headers(),
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise GroqError(response.status_code, _decode_body(body))

                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line:
                        continue
                    if line == "[DONE]":
                        break
                    try:
                        chunk = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        logger.debug("skipping malformed SSE line: %s", _decode_body(line, 160))
                        continue
                    if not isinstance(chunk, dict):
                        continue

                    inline_error = _error_from_chunk(chunk)
                    if inline_error:
                        raise GroqError(502, inline_error)

                    delta = _delta_from_chunk(chunk)
                    if delta:
                        clean = sanitizer.push(delta)
                        if clean:
                            if first_token_ms is None:
                                first_token_ms = (time.perf_counter() - started) * 1000.0
                            chars += len(clean)
                            yield clean

                tail = sanitizer.flush()
                if tail:
                    if first_token_ms is None:
                        first_token_ms = (time.perf_counter() - started) * 1000.0
                    chars += len(tail)
                    yield tail
        except httpx.TransportError as exc:
            raise GroqError(0, f"{type(exc).__name__}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise GroqError(0, f"{type(exc).__name__}: {exc}") from exc
        finally:
            total_ms = (time.perf_counter() - started) * 1000.0
            logger.debug(
                "LLM stream first_token=%s total=%.0f ms chars=%d",
                "n/a" if first_token_ms is None else f"{first_token_ms:.0f} ms",
                total_ms,
                chars,
            )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Run a chat completion and return the whole answer as one string.

        This is the non streaming convenience wrapper around
        :meth:`stream_chat`, for callers that have nothing to do with partial
        tokens (summaries, one shot rewrites, tests).

        Args:
            messages: OpenAI style message list.
            max_tokens: Hard ceiling on generated tokens.
            temperature: Sampling temperature.

        Returns:
            The joined and stripped completion text.

        Raises:
            GroqError: When the API returns a non 200 status or fails.
        """
        started = time.perf_counter()
        parts: list[str] = []
        async for delta in self.stream_chat(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            parts.append(delta)
        text = "".join(parts).strip()
        logger.debug(
            "LLM complete %.0f ms, %d chars",
            (time.perf_counter() - started) * 1000.0,
            len(text),
        )
        return text
