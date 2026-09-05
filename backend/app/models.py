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

PRACTICE_DIFFICULTIES: frozenset[str] = frozenset({"warm", "normal", "brutal"})
"""The three practice clients a rep can rehearse against."""

DEFAULT_DIFFICULTY: str = "normal"
"""Difficulty used when the client sends nothing, or sends something unknown."""

PRACTICE_OUTCOMES: frozenset[str] = frozenset({"booked", "soft_yes", "no_answer", "hung_up"})
"""How a practice call is allowed to end, as judged by the coach model."""

DEFAULT_OUTCOME: str = "no_answer"
"""Outcome used when the coach model answers with a word we do not know."""

CALL_PROVIDERS: frozenset[str] = frozenset(
    {"manual", "whatsapp_link", "whatsapp_cloud", "twilio"}
)
"""The four ways a call can be placed.

Keep this in step with ``app.telephony.PROVIDERS``, which owns the labels, the
blurbs and the cost lines, and with ``session_store.CallProvider``, which is the
same four words as a typing Literal.
"""

DEFAULT_CALL_PROVIDER: str = "manual"
"""Provider used before the rep picks one. The rep dials, the app only listens."""

CALL_STATES: frozenset[str] = frozenset(
    {"idle", "dialing", "ringing", "live", "ended", "failed"}
)
"""Every state a phone call is allowed to be in, mirrored by ``session_store.CallState``."""

DEFAULT_CALL_STATE: str = "idle"
"""State of a session that has never started a call."""

MAX_PHONE_CHARS: int = 32
"""Longest phone number string we keep. A real E.164 number is at most 16 characters.

The extra room is for the spaces, dashes and brackets people type. Cleaning the
number into real E.164 is ``app.telephony.normalise_e164``, not this module, so
there is exactly one place that decides what a good number looks like.
"""


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


class PracticeStartRequest(PrepareContextRequest):
    """Body of ``POST /api/practice/start``.

    The rep practises against the very client they are about to call, so this is
    the prepare context body with one extra field. Every validator on the parent
    still runs, so the knowledge base, the URL, the notes, the goal and the
    language are cleaned exactly the same way here as on a real call.

    Attributes:
        difficulty: ``warm``, ``normal`` or ``brutal``. Anything else becomes
            ``normal`` instead of a 422, because a bad difficulty must never
            stop the rep from practising.
    """

    difficulty: str = DEFAULT_DIFFICULTY

    @field_validator("difficulty", mode="before")
    @classmethod
    def _normalize_difficulty(cls, value: object) -> str:
        """Lowercase the difficulty and fall back to ``normal`` when unknown.

        Args:
            value: Raw incoming difficulty value of any type.

        Returns:
            One of ``warm``, ``normal`` or ``brutal``. Never raises, for the
            same reason the language validator never raises.
        """
        if not isinstance(value, str):
            return DEFAULT_DIFFICULTY
        key = value.strip().lower()
        return key if key in PRACTICE_DIFFICULTIES else DEFAULT_DIFFICULTY


class PracticeStartResponse(PrepareContextResponse):
    """Result of starting a practice call.

    Everything the real prepare context route returns, plus the four things the
    practice UI needs to open the call page and speak the first line.

    Attributes:
        mode: Always ``"practice"``. The frontend switches its whole call page
            on this one value.
        difficulty: The level that was actually used, after the fallback above.
        persona_name: Name of the person the AI client is playing, pulled out of
            the client notes when one could be found, otherwise ``None``.
        opening_line: The line the browser speaks before the rep says anything.
    """

    mode: str = "practice"
    difficulty: str = DEFAULT_DIFFICULTY
    persona_name: str | None = None
    opening_line: str = ""


class DifficultyModel(CamelModel):
    """One practice level, as shown on the setup page.

    Attributes:
        key: Stable identifier sent back in ``PracticeStartRequest.difficulty``.
        label: Short name shown on the card.
        blurb: One plain line saying how this client behaves.
    """

    key: str
    label: str
    blurb: str


class DifficultiesResponse(CamelModel):
    """Result of ``GET /api/practice/difficulties``.

    Attributes:
        levels: The three practice levels, in display order, easiest first.
    """

    levels: list[DifficultyModel] = Field(default_factory=list)

    @classmethod
    def from_entries(cls, entries: list[dict[str, object]]) -> DifficultiesResponse:
        """Build the response straight from the frozen list in ``app.practice``.

        Only the three UI fields are copied. ``app.practice.DIFFICULTIES`` also
        carries server side tuning like the turn limit, and none of that belongs
        on the wire.

        Args:
            entries: Dicts carrying at least ``key``, ``label`` and ``blurb``.

        Returns:
            A populated ``DifficultiesResponse``.
        """
        return cls(
            levels=[
                DifficultyModel(
                    key=str(entry.get("key", "")),
                    label=str(entry.get("label", "")),
                    blurb=str(entry.get("blurb", "")),
                )
                for entry in entries
            ]
        )


class MetricsModel(CamelModel):
    """The counted part of the scorecard.

    Every number here is plain arithmetic over the stored practice turns. None
    of it comes from a model, because a model cannot count.

    Attributes:
        rep_words: How many words the rep said in the whole call.
        client_words: How many words the AI client said.
        talking_time_pct: Rep words over total words, as a whole number.
        questions_asked: How many rep turns contained a question mark.
        objections_faced: How many client turns carried a real objection.
        objections_handled: How many of those the rep got past.
        avg_reply_ms: Average time the rep took to start talking after the
            client stopped.
        filler_words: How many filler words the rep used, like um and uh.
        longest_sentence_words: Word count of the rep's longest sentence.
    """

    rep_words: int = 0
    client_words: int = 0
    talking_time_pct: int = 0
    questions_asked: int = 0
    objections_faced: int = 0
    objections_handled: int = 0
    avg_reply_ms: int = 0
    filler_words: int = 0
    longest_sentence_words: int = 0


class CopilotMomentModel(CamelModel):
    """One moment in the call, quoted back with all three lines.

    Attributes:
        client_said: What the AI client said.
        copilot_said: The line the teleprompter was showing at that moment.
        you_said: What the rep actually said next, quoted word for word.
        why: One plain line saying why this moment matters.
    """

    client_said: str = ""
    copilot_said: str = ""
    you_said: str = ""
    why: str = ""


class CopilotReportModel(CamelModel):
    """How much the teleprompter helped the rep.

    Attributes:
        suggestions_shown: How many lines the teleprompter put on screen.
        suggestions_used: How many of those the rep actually used.
        used_pct: Used over shown, as a whole number.
        best_moment: The moment the rep used the line well, or ``None`` when
            there was not enough of a call to pick one.
        missed_moment: A good line the rep skipped, or ``None``.
    """

    suggestions_shown: int = 0
    suggestions_used: int = 0
    used_pct: int = 0
    best_moment: CopilotMomentModel | None = None
    missed_moment: CopilotMomentModel | None = None


class BreakdownModel(CamelModel):
    """One row of the score, so the rep can see where the points came from.

    Attributes:
        label: Plain name of the row, for example ``"Talking time"``.
        got: Points won on this row.
        out_of: Points that were on offer.
        note: One plain line saying why, and what to do next time.
    """

    label: str
    got: int = 0
    out_of: int = 0
    note: str = ""


class FixModel(CamelModel):
    """One thing the rep should say differently next time.

    Attributes:
        you_said: The rep's own words, quoted, never invented.
        problem: One plain line saying what went wrong with it.
        say_instead: The better line, ready to read out loud.
    """

    you_said: str = ""
    problem: str = ""
    say_instead: str = ""


class DebriefResponse(CamelModel):
    """Result of ``GET /api/practice/{session_id}/debrief``, the scorecard.

    A rep who ended the call before saying anything still gets a valid body:
    ``turns`` is 0, the lists are empty and both moments are ``None``, so the
    overlay can render a thin debrief without any special casing.

    Attributes:
        session_id: The practice session this scorecard belongs to.
        difficulty: The level that was played.
        duration_ms: How long the call ran, in milliseconds.
        turns: How many turns the call had, both sides counted.
        outcome: ``booked``, ``soft_yes``, ``no_answer`` or ``hung_up``.
        score: 0 to 100, computed in Python from the fixed rubric.
        grade: The word next to the score, for example ``"Getting there"``.
        summary: One plain line about the whole call.
        metrics: The counted numbers.
        copilot: How much the teleprompter helped.
        breakdown: The seven rubric rows, in rubric order.
        wins: Two or three things the rep did well, each quoting the rep.
        fixes: Two or three things to say differently next time.
        next_drill: One line saying what to practise on the next call.
    """

    session_id: str
    difficulty: str = DEFAULT_DIFFICULTY
    duration_ms: int = 0
    turns: int = 0
    outcome: str = DEFAULT_OUTCOME
    score: int = 0
    grade: str = ""
    summary: str = ""
    metrics: MetricsModel = Field(default_factory=lambda: MetricsModel())
    copilot: CopilotReportModel = Field(default_factory=lambda: CopilotReportModel())
    breakdown: list[BreakdownModel] = Field(default_factory=list)
    wins: list[str] = Field(default_factory=list)
    fixes: list[FixModel] = Field(default_factory=list)
    next_drill: str = ""

    @field_validator("outcome", mode="before")
    @classmethod
    def _normalize_outcome(cls, value: object) -> str:
        """Lowercase the outcome and fall back to ``no_answer`` when unknown.

        The coach model picks this word, so it is the one field on the scorecard
        that a model can get wrong. A wrong word must not turn the whole debrief
        into a 500, it just becomes the neutral outcome.

        Args:
            value: Raw outcome value of any type.

        Returns:
            One of the four allowed outcome words.
        """
        if not isinstance(value, str):
            return DEFAULT_OUTCOME
        key = value.strip().lower()
        return key if key in PRACTICE_OUTCOMES else DEFAULT_OUTCOME


class CallProviderModel(CamelModel):
    """One way of placing the call, as shown in the provider picker.

    The picker is the one place the rep learns that a call costs money, so
    ``cost`` is never empty and ``missing`` is never hidden. A provider with
    ``ready=False`` must be drawn disabled and must show both of them.

    Attributes:
        key: Stable identifier, one of :data:`CALL_PROVIDERS`.
        label: Short name of the option, for example ``"Ring my phone"``.
        blurb: One plain line saying what happens when the rep picks this.
        cost: What it costs in plain words, for example ``"Free"``. Never empty.
        ready: Whether this provider can actually place a call right now, which
            is computed from the environment by ``app.telephony``.
        missing: Names of the environment variables still needed, empty when the
            provider is ready. Shown to the rep word for word, so they know
            exactly what to add to ``backend/.env``.
    """

    key: str
    label: str
    blurb: str
    cost: str
    ready: bool = False
    missing: list[str] = Field(default_factory=list)


class CallProvidersResponse(CamelModel):
    """Result of ``GET /api/call/providers``.

    Attributes:
        providers: The four providers, in display order, cheapest and simplest
            first. Providers that are not configured are still listed, because
            the rep needs to see that the option exists and what it needs.
    """

    providers: list[CallProviderModel] = Field(default_factory=list)

    @classmethod
    def from_entries(cls, entries: list[dict[str, object]]) -> CallProvidersResponse:
        """Build the response straight from ``app.telephony.provider_status()``.

        Values are read defensively so a new key in the telephony table can
        never break this endpoint, which is the one endpoint the call page needs
        before it can render anything at all.

        Args:
            entries: Dicts carrying ``key``, ``label``, ``blurb``, ``cost``,
                ``ready`` and ``missing``.

        Returns:
            A populated ``CallProvidersResponse``.
        """
        providers: list[CallProviderModel] = []
        for entry in entries:
            raw_missing = entry.get("missing") or []
            missing = [str(name) for name in raw_missing] if isinstance(raw_missing, list) else []
            providers.append(
                CallProviderModel(
                    key=str(entry.get("key", "")),
                    label=str(entry.get("label", "")),
                    blurb=str(entry.get("blurb", "")),
                    cost=str(entry.get("cost", "")),
                    ready=bool(entry.get("ready", False)),
                    missing=missing,
                )
            )
        return cls(providers=providers)


class CallStartRequest(CamelModel):
    """Body of ``POST /api/call/start``.

    Numbers are only stripped and capped here. Turning them into real E.164 is
    ``app.telephony.normalise_e164``, so the route can answer a bad number with
    the plain 400 body the contract asks for instead of a pydantic error blob.

    Attributes:
        session_id: The session this call belongs to. The call state is stored
            on that session, so an unknown id is a 404 at the route.
        provider: One of :data:`CALL_PROVIDERS`. Required, and never guessed.
        to_number: The client's number. Required for every provider except
            ``manual``, which the route checks because the message it has to
            send back is provider specific.
        rep_number: The rep's own phone, which Twilio rings first. Only used by
            the ``twilio`` provider.
    """

    session_id: str = Field(min_length=1, max_length=64)
    provider: str
    to_number: str | None = None
    rep_number: str | None = None

    @field_validator("provider", mode="before")
    @classmethod
    def _check_provider(cls, value: object) -> str:
        """Lowercase the provider and refuse a word we do not know.

        This is the one place in the file that raises instead of falling back to
        a default. Language, difficulty and outcome all fall back because a
        wrong value there costs nothing. Here it would cost the rep a call: a
        silent fall back to ``manual`` would answer "you dial it yourself" to
        someone who asked us to ring their phone, and they would sit and wait
        for a ring that never comes.

        Args:
            value: Raw incoming provider value of any type.

        Returns:
            One of :data:`CALL_PROVIDERS`.

        Raises:
            ValueError: When the provider is missing or unknown.
        """
        key = value.strip().lower() if isinstance(value, str) else ""
        if key not in CALL_PROVIDERS:
            raise ValueError(
                "Pick how to call: manual, whatsapp_link, whatsapp_cloud or twilio."
            )
        return key

    @field_validator("to_number", "rep_number", mode="before")
    @classmethod
    def _clean_number(cls, value: object) -> object:
        """Strip a phone number, cap it, and turn an empty result into ``None``.

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
        return cleaned[:MAX_PHONE_CHARS]


class CallStartResponse(CamelModel):
    """Result of ``POST /api/call/start``, on both the 200 and the 400.

    The same shape is returned when the call could not start, with ``ok=False``
    and a ``message`` that says what is missing in plain words. The frontend
    shows ``message`` either way, so it never has to guess from a status code.

    Attributes:
        ok: Whether the call was actually placed or the link was actually built.
        provider: The provider that was used, echoed back.
        call_id: The provider's own id for this call, for example a Twilio call
            sid. ``None`` for link mode and for manual.
        open_url: A link the browser must open, used by WhatsApp link mode only.
            ``None`` for every other provider.
        message: One plain line for the rep, for example "Your phone is ringing
            now. Pick it up and we will dial the client." Never empty.
    """

    ok: bool = False
    provider: str = DEFAULT_CALL_PROVIDER
    call_id: str | None = None
    open_url: str | None = None
    message: str = ""


class CallStatusResponse(CamelModel):
    """Result of ``GET /api/call/{session_id}/status``.

    The same four fields are pushed down the teleprompter socket as a
    ``call_state`` frame, so the call page can show the state without polling.
    ``Session.call_snapshot()`` builds that frame from the very same fields.

    Attributes:
        provider: The provider this session last used.
        state: One of :data:`CALL_STATES`.
        call_id: The provider's own id for the call, or ``None``.
        detail: One plain line with more about the state, for example why it
            failed. ``None`` when there is nothing to add.
    """

    provider: str = DEFAULT_CALL_PROVIDER
    state: str = DEFAULT_CALL_STATE
    call_id: str | None = None
    detail: str | None = None

    @field_validator("state", mode="before")
    @classmethod
    def _normalize_state(cls, value: object) -> str:
        """Lowercase the state and fall back to ``idle`` when unknown.

        Providers speak their own words for this. Twilio alone sends queued,
        initiated, ringing, in-progress, completed, busy, no-answer, canceled
        and failed. Mapping those onto our six is the job of the Twilio routes,
        and this validator is only the last guard so a word nobody mapped can
        never turn a status poll into a 500 in the middle of a live call.

        Args:
            value: Raw state value of any type.

        Returns:
            One of :data:`CALL_STATES`.
        """
        if not isinstance(value, str):
            return DEFAULT_CALL_STATE
        key = value.strip().lower()
        return key if key in CALL_STATES else DEFAULT_CALL_STATE


__all__ = [
    "SUPPORTED_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "MAX_CLIENT_URL_CHARS",
    "MAX_CALL_GOAL_CHARS",
    "MAX_CLIENT_CONTEXT_CHARS",
    "PRACTICE_DIFFICULTIES",
    "DEFAULT_DIFFICULTY",
    "PRACTICE_OUTCOMES",
    "DEFAULT_OUTCOME",
    "CALL_PROVIDERS",
    "DEFAULT_CALL_PROVIDER",
    "CALL_STATES",
    "DEFAULT_CALL_STATE",
    "MAX_PHONE_CHARS",
    "CamelModel",
    "PrepareContextRequest",
    "PrepareContextResponse",
    "SessionInfoResponse",
    "HealthResponse",
    "QuickActionModel",
    "QuickActionsResponse",
    "PracticeStartRequest",
    "PracticeStartResponse",
    "DifficultyModel",
    "DifficultiesResponse",
    "MetricsModel",
    "CopilotMomentModel",
    "CopilotReportModel",
    "BreakdownModel",
    "FixModel",
    "DebriefResponse",
    "CallProviderModel",
    "CallProvidersResponse",
    "CallStartRequest",
    "CallStartResponse",
    "CallStatusResponse",
]
