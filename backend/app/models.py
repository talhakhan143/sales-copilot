"""Pydantic v2 request and response models for the REST surface.

Wire shape rule for the whole project: JSON is camelCase, Python is snake_case.
Every model here uses ``alias_generator=to_camel`` plus ``populate_by_name=True``
so both spellings are accepted on input while output stays camelCase.

Route authors: keep FastAPI's default ``response_model_by_alias=True``. That is
what makes ``session_id`` serialize as ``sessionId``. If a route ever sets
``response_model_by_alias=False`` the frontend contract breaks, so do not.
When serializing by hand, call ``model_dump(by_alias=True)`` or
``model_dump_json(by_alias=True)``.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"en", "ur", "hi", "es", "ar", "fr", "de"})
"""Language codes the copilot is allowed to answer in. Anything else falls back to "en"."""

DEFAULT_LANGUAGE: str = "en"
"""Language used when the client sends nothing, or sends something unsupported."""

MAX_CLIENT_URL_CHARS: int = 2048
"""Longest prospect URL we keep. Longer input is truncated, not rejected."""

MAX_CALL_GOAL_CHARS: int = 600
"""Hard cap on the one line call goal."""

MAX_CLIENT_CONTEXT_CHARS: int = 6000
"""Longest free text description of the client we keep."""


class CamelModel(BaseModel):
    """Base model that speaks camelCase on the wire and snake_case in Python.

    Attributes:
        model_config: Enables the camelCase alias generator, accepts either the
            field name or the alias on input, and strips nothing implicitly so
            each model stays explicit about its own cleaning.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
    )


class PrepareContextRequest(CamelModel):
    """Body of ``POST /api/prepare-context``.

    Attributes:
        knowledge_base: Everything the rep sells, pasted raw. Required. Leading
            and trailing whitespace is stripped, and a value that is only
            whitespace is rejected.
        client_url: Optional prospect website. Empty or whitespace only becomes
            ``None`` so downstream code can simply test for truthiness.
        call_goal: Optional one line goal for this call. Same empty handling as
            ``client_url``.
        client_context: Optional free text about the client, for the very common
            case where the client has no website at all, which is exactly who a
            web agency is calling. Their industry, size, city, how they get
            customers today, anything the rep already knows. Used on its own or
            alongside a scraped page.
        language: Two letter code the copilot must answer in. Unsupported or
            missing values fall back to ``"en"``.
    """

    knowledge_base: str = Field(min_length=1, max_length=40000)
    client_url: str | None = None
    client_context: str | None = None
    call_goal: str | None = None
    language: str = DEFAULT_LANGUAGE

    @field_validator("knowledge_base", mode="after")
    @classmethod
    def _clean_knowledge_base(cls, value: str) -> str:
        """Strip the knowledge base and refuse a whitespace only body.

        Args:
            value: The raw knowledge base text after length validation.

        Returns:
            The stripped text.

        Raises:
            ValueError: When nothing but whitespace was sent, which FastAPI
                turns into a 422 response.
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("knowledgeBase must contain more than whitespace")
        return cleaned

    @field_validator("client_url", "client_context", "call_goal", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """Strip optional text fields and turn an empty result into ``None``.

        Over long values are truncated rather than rejected, because the browser
        form does not validate length and a 422 there would be a dead end for
        the user.

        Args:
            value: Raw incoming value, usually a string or ``None``.

        Returns:
            ``None`` for empty input, the stripped and capped string otherwise,
            or the untouched value when it is not a string so pydantic can
            report the real type error.
        """
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if not cleaned:
            return None
        # The shared cap here is the URL cap, which is the smallest of the
        # three. client_context gets its own, much larger cap below.
        return cleaned[:MAX_CLIENT_URL_CHARS] if len(cleaned) > MAX_CLIENT_URL_CHARS else cleaned

    @field_validator("client_context", mode="before")
    @classmethod
    def _clean_client_context(cls, value: object) -> object:
        """Strip the free text client notes and cap them.

        This runs before the shared ``_blank_to_none`` validator would clip it to
        the URL length, so the cap that actually applies is this one.

        Args:
            value: Raw incoming value, usually a string or ``None``.

        Returns:
            ``None`` for empty input, otherwise the stripped and capped text.
        """
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if not cleaned:
            return None
        return cleaned[:MAX_CLIENT_CONTEXT_CHARS]

    @field_validator("call_goal", mode="after")
    @classmethod
    def _cap_call_goal(cls, value: str | None) -> str | None:
        """Cap the call goal so a pasted essay cannot bloat the system prompt.

        Args:
            value: The already stripped call goal, or ``None``.

        Returns:
            The value unchanged when short enough, otherwise the first
            ``MAX_CALL_GOAL_CHARS`` characters.
        """
        if value is None:
            return None
        return value[:MAX_CALL_GOAL_CHARS]

    @field_validator("language", mode="before")
    @classmethod
    def _normalize_language(cls, value: object) -> str:
        """Lowercase the language code and fall back to English when unknown.

        Args:
            value: Raw incoming language value of any type.

        Returns:
            One of the supported codes. Never raises, an unknown or wrongly
            typed value simply becomes ``"en"`` because a bad language should
            not block a call from starting.
        """
        if not isinstance(value, str):
            return DEFAULT_LANGUAGE
        code = value.strip().lower()
        return code if code in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


class PrepareContextResponse(CamelModel):
    """Result of building a call context.

    A failed scrape is not an error. The route still returns 200 with
    ``scrape_ok=False`` and a human readable ``scrape_error``, and the system
    prompt is built from the knowledge base alone.

    Attributes:
        session_id: uuid4 string the WebSocket endpoint expects.
        system_prompt: The fully fused system prompt, shown in the setup UI.
        client_url: Normalized prospect URL, or ``None`` when none was usable.
        client_title: Page title pulled from the scrape, or ``None``.
        client_excerpt: First 600 characters of the scraped text, or ``None``.
        scrape_chars: Number of characters kept from the scrape.
        scrape_ok: Whether the scrape actually produced usable text.
        scrape_error: Why the scrape failed, or ``None`` on success or when no
            URL was supplied.
        created_at: Unix timestamp of session creation, in seconds.
    """

    session_id: str
    system_prompt: str
    client_url: str | None = None
    client_title: str | None = None
    client_excerpt: str | None = None
    scrape_chars: int = 0
    scrape_ok: bool = False
    scrape_error: str | None = None
    created_at: float = Field(default_factory=time.time)


class SessionInfoResponse(CamelModel):
    """Result of ``GET /api/session/{session_id}``.

    Attributes:
        session_id: The id that was asked about, echoed back as sent.
        exists: Whether the session is still live in the in memory store.
        turns: How many transcript turns the session holds right now.
        created_at: Creation timestamp, ``0.0`` when the session is gone.
    """

    session_id: str
    exists: bool
    turns: int = 0
    created_at: float = 0.0


class ModelsModel(CamelModel):
    """The Groq model ids this process is actually configured with.

    Attributes:
        stt: Speech to text model id.
        llm: Chat completion model id.
    """

    stt: str = ""
    llm: str = ""


class HealthResponse(CamelModel):
    """Result of ``GET /api/health``.

    Attributes:
        status: Always ``"ok"`` when the process can answer at all.
        groq_configured: Whether a Groq API key is present. False means STT and
            suggestions will fail, so the UI shows a warning pill.
        sessions: Number of live sessions in the store.
        version: Backend version string.
        models: The live model ids, so the UI never hardcodes a name that
            the operator has since swapped through the environment.
    """

    status: str = "ok"
    groq_configured: bool = False
    sessions: int = 0
    version: str = "1.0.0"
    models: ModelsModel = Field(default_factory=lambda: ModelsModel())


class QuickActionModel(CamelModel):
    """One frozen objection button.

    Attributes:
        key: Stable identifier sent back in a ``quick_action`` WebSocket frame.
        label: Button text.
        icon: lucide-react icon name. Verified to exist in the library.
        hint: Small muted line under the label.
    """

    key: str
    label: str
    icon: str
    hint: str


class QuickActionsResponse(CamelModel):
    """Result of ``GET /api/quick-actions``.

    Attributes:
        actions: The eight frozen quick actions, in display order.
    """

    actions: list[QuickActionModel] = Field(default_factory=list)

    @classmethod
    def from_entries(cls, entries: list[dict[str, str]]) -> QuickActionsResponse:
        """Build the response straight from the frozen list in ``app.prompts``.

        Args:
            entries: Dicts carrying the ``key``, ``label``, ``icon`` and
                ``hint`` fields, normally ``app.prompts.QUICK_ACTIONS``.

        Returns:
            A populated ``QuickActionsResponse``.
        """
        return cls(actions=[QuickActionModel(**entry) for entry in entries])


__all__ = [
    "SUPPORTED_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "MAX_CLIENT_URL_CHARS",
    "MAX_CALL_GOAL_CHARS",
    "MAX_CLIENT_CONTEXT_CHARS",
    "CamelModel",
    "PrepareContextRequest",
    "PrepareContextResponse",
    "SessionInfoResponse",
    "HealthResponse",
    "QuickActionModel",
    "QuickActionsResponse",
]
