"""Application settings loaded from the environment and from a local .env file.

Field names are snake_case on the Python side and map to the UPPER_SNAKE
environment variable of the same name, because ``case_sensitive`` is off. So
``groq_api_key`` reads ``GROQ_API_KEY``, ``vad_silence_ms`` reads
``VAD_SILENCE_MS``, and so on.

Nothing in this module performs IO beyond reading the process environment and
the .env file at import time, so it is safe to import from anywhere including
the event loop.

Typical use:
    from app.config import settings, cors_origin_list

    origins = cors_origin_list()
    if not settings.groq_api_key:
        log.warning("no GROQ key")
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the Sales Copilot backend.

    Every field has a safe default so the application still boots with an empty
    environment. The only value that really matters for a working install is
    ``groq_api_key``. When it is empty the API still serves, prepares context
    and accepts WebSocket connections, but transcription and suggestions fail
    with a clear error instead of silently doing nothing.

    Attributes:
        groq_api_key: Groq API key. Empty means the Groq client is not
            configured and STT plus LLM calls will be rejected early.
        groq_base_url: OpenAI compatible base URL for the Groq API.
        stt_model: Whisper model id used for speech to text.
        llm_model: Chat completion model id used for suggestions.
        jina_base_url: Jina Reader prefix. The target URL is appended to it.
        jina_api_key: Optional Jina key. It only raises the rate limit, the
            reader works without it.
        cors_origins: Comma separated list of allowed browser origins.
        session_ttl_seconds: Age after which an idle session is swept.
        max_scrape_chars: Hard cap on characters kept from a scraped page.
        max_kb_chars: Hard cap on characters kept from the pasted knowledge base.
        transcript_window_turns: How many recent turns are replayed to the LLM.
        llm_max_tokens: Token ceiling for a single suggestion.
        llm_temperature: Sampling temperature for suggestions.
        vad_silence_ms: Trailing silence that closes an utterance.
        vad_min_speech_ms: Utterances shorter than this are discarded.
        vad_max_utterance_ms: Forced cut length for a long monologue.
        vad_preroll_ms: Audio kept before speech onset so word starts survive.
        log_level: Root logging level name, for example INFO or DEBUG.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    stt_model: str = "whisper-large-v3-turbo"
    llm_model: str = "qwen/qwen3.6-27b"

    jina_base_url: str = "https://r.jina.ai/"
    jina_api_key: str = ""

    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    session_ttl_seconds: int = 21600
    max_scrape_chars: int = 12000
    max_kb_chars: int = 24000
    transcript_window_turns: int = 14

    llm_max_tokens: int = 160
    llm_temperature: float = 0.4

    vad_silence_ms: int = 620
    vad_min_speech_ms: int = 260
    vad_max_utterance_ms: int = 12000
    vad_preroll_ms: int = 320

    log_level: str = "INFO"


settings = Settings()
"""Process wide settings singleton. Import this, do not build a second one."""


def cors_origin_list() -> list[str]:
    """Split the configured CORS origins into a clean list.

    The ``CORS_ORIGINS`` value is a single comma separated string so it stays
    easy to set in a .env file or a container environment. This helper turns it
    into the list shape that Starlette's CORSMiddleware expects, dropping empty
    entries and surrounding whitespace.

    Returns:
        A list of origin strings. An empty list when nothing is configured,
        which means the middleware will allow no cross origin browser calls.
    """
    return [item.strip() for item in settings.cors_origins.split(",") if item.strip()]


def _mask(secret: str) -> str:
    """Describe a secret without ever revealing it.

    Args:
        secret: The raw secret value, possibly empty.

    Returns:
        The literal string ``"set"`` when the secret has content, otherwise
        ``"not set"``. The value itself is never included, not even a prefix,
        because this output ends up in logs and in the health endpoint.
    """
    return "set" if secret.strip() else "not set"


def redacted() -> dict[str, object]:
    """Return the current settings as a plain dict with secrets masked.

    Useful for the startup banner and for a debug view on the health endpoint.
    API keys are replaced by ``"set"`` or ``"not set"``, and two extra boolean
    keys (``groq_configured`` and ``jina_configured``) are added so callers can
    branch without parsing strings.

    Returns:
        A JSON safe dict of every setting. No value in it is a secret.
    """
    data: dict[str, object] = settings.model_dump()
    data["groq_api_key"] = _mask(settings.groq_api_key)
    data["jina_api_key"] = _mask(settings.jina_api_key)
    data["groq_configured"] = bool(settings.groq_api_key.strip())
    data["jina_configured"] = bool(settings.jina_api_key.strip())
    data["cors_origins_parsed"] = cors_origin_list()
    return data


__all__ = ["Settings", "settings", "cors_origin_list", "redacted"]
