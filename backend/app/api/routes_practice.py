"""REST routes for practice mode: the levels, the start call and the scorecard.

Everything here is mounted under ``/api/practice`` and every JSON shape is
frozen by the practice contract, sections 3.1 and 4.

Two rules drive the code below.

First, a practice call is prepared exactly like a real one, so ``POST /start``
runs the same ``prepare_context`` the live path runs and only marks the session
afterwards. The rep rehearses against the real prospect they are about to call.

Second, every number on the scorecard is counted in Python by
``app.services.scoring``. The coach model is asked only for the judgement calls
(what went well, what to fix), and when it cannot answer the scorecard is still
returned with empty lists instead of an error, because a rep who just finished a
call must always see their score.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app import practice
from app.api.routes_context import get_groq
from app.models import (
    DebriefResponse,
    DifficultiesResponse,
    PracticeStartRequest,
    PracticeStartResponse,
    PrepareContextRequest,
    PrepareContextResponse,
)
from app.services import practice_engine, scoring
from app.services.context_builder import (
    FALLBACK_CALL_GOAL,
    clamp_on_word_boundary,
    prepare_context,
)
from app.services.session_store import Session, store

log = logging.getLogger("salescopilot.api.practice")

router = APIRouter(prefix="/api/practice", tags=["practice"])

DEFAULT_DIFFICULTY: str = "normal"
"""Level used when the client sends nothing, or sends a level we do not know."""

KNOWLEDGE_HINT_CHARS: int = 1200
"""How much of the offer sheet the persona is allowed to see.

The persona plays the prospect, not the rep, so it only needs enough of the
offer to push back on it. Feeding it the whole knowledge base would make it
argue with details a real prospect has never read, and it would also pay for
those tokens on every single client turn of the call.
"""

COACH_BUDGET_SECONDS: float = 25.0
"""Hard timeout around the coach call so the debrief can never hang."""

MAX_WINS: int = 3
"""How many "what went well" lines the scorecard shows."""

MAX_FIXES: int = 3
"""How many "what to fix" rows the scorecard shows."""

OUTCOMES: tuple[str, ...] = ("booked", "soft_yes", "no_answer", "hung_up")
"""The four call results the rubric knows how to score."""

FALLBACK_OUTCOME: str = "no_answer"
"""Outcome used when the coach did not run or returned something unknown."""

DEFAULT_DRILL: str = "Say your opening line out loud three times. Keep it short."
"""Practice tip used when the coach could not write one."""

EMPTY_DRILL: str = "Start a practice call and say your first line out loud."
"""Practice tip for a call where the rep never spoke."""

_CLIENT_BLOCK_HEADING: str = "# TARGET CLIENT INFO"
"""Heading that opens the prospect block inside the fused system prompt."""

_CALL_GOAL_HEADING: str = "# CALL GOAL"
"""Heading that closes the prospect block inside the fused system prompt."""

_MISSING: Any = object()
"""Sentinel telling "this object has no such field" apart from a real ``None``."""


# ====================================================================== #
# tolerant field readers
#
# ``scoring`` and ``practice_engine`` are owned by other modules and may hand
# back a dataclass, a pydantic model or a plain dict. The debrief must survive
# all three, and must never answer 500 because one field was spelled the other
# way, so every value is read through these helpers.
# ====================================================================== #


def _field(obj: object, *names: str, default: Any = None) -> Any:
    """Read the first field that exists, from a mapping or from an object.

    Args:
        obj: A dataclass, a pydantic model, a mapping, or ``None``.
        *names: Field names to try in order, usually the snake_case spelling
            followed by the camelCase one.
        default: Returned when none of the names is present.

    Returns:
        The first value found, otherwise ``default``.
    """
    if obj is None:
        return default
    for name in names:
        if isinstance(obj, Mapping):
            if name in obj:
                return obj[name]
            continue
        value = getattr(obj, name, _MISSING)
        if value is not _MISSING:
            return value
    return default


def _int_field(obj: object, *names: str, default: int = 0) -> int:
    """Read one field and coerce it to a whole number.

    Args:
        obj: The object or mapping to read from.
        *names: Field names to try in order.
        default: Value used when the field is missing or not a number.

    Returns:
        The value as an ``int``, never a float and never ``None``.
    """
    value = _field(obj, *names, default=None)
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _str_field(obj: object, *names: str, default: str = "") -> str:
    """Read one field and coerce it to a stripped string.

    Args:
        obj: The object or mapping to read from.
        *names: Field names to try in order.
        default: Value used when the field is missing or empty.

    Returns:
        The stripped text, or ``default`` when there is nothing usable.
    """
    value = _field(obj, *names, default=None)
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _rows(obj: object, *names: str) -> list[Any]:
    """Read one field that should be a list.

    Args:
        obj: The object or mapping to read from.
        *names: Field names to try in order.

    Returns:
        The list, or an empty list when the field is missing or not a sequence.
    """
    value = _field(obj, *names, default=None)
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


# ====================================================================== #
# small builders
# ====================================================================== #


def _difficulty_key(raw: object) -> str:
    """Coerce whatever the client sent into one of the known levels.

    A wrong level is not worth a 422. The rep asked for a practice call, so an
    unknown value quietly becomes the middle level instead of a dead end.

    Args:
        raw: The ``difficulty`` value from the request.

    Returns:
        One of the keys in ``practice.DIFFICULTIES``, or ``"normal"``.
    """
    key = str(raw or "").strip().lower()
    for entry in practice.DIFFICULTIES:
        if str(entry.get("key", "")) == key:
            return key
    return DEFAULT_DIFFICULTY


def _persona_name(client_context: str | None) -> str | None:
    """Pull a person name out of the rep's own notes about the client.

    Args:
        client_context: Free text the rep typed about this prospect.

    Returns:
        The name to put in the opening line, or ``None`` when the notes carry
        no name and the neutral placeholder should be used instead.
    """
    text = (client_context or "").strip()
    if not text:
        return None
    try:
        found = practice.extract_person_name(text)
    except Exception:  # noqa: BLE001 - a name guess must never fail a start.
        log.exception("name extraction failed, using the neutral placeholder")
        return None
    if not isinstance(found, str):
        return None
    return found.strip() or None


def _client_block(context: PrepareContextResponse, client_context: str | None) -> str:
    """Recover the prospect block that ``prepare_context`` built.

    The builder fuses that block straight into the system prompt and does not
    hand it back on its own, so it is sliced back out between the two frozen
    headings. If the headings ever move, the fallback rebuilds a smaller block
    from the response fields, which is enough for the persona to sound real.

    Args:
        context: The response ``prepare_context`` returned.
        client_context: The rep's own notes about the client.

    Returns:
        The prospect text the persona will be given. May be empty.
    """
    prompt = context.system_prompt or ""
    start = prompt.find(_CLIENT_BLOCK_HEADING)
    if start >= 0:
        start += len(_CLIENT_BLOCK_HEADING)
        end = prompt.find(_CALL_GOAL_HEADING, start)
        block = (prompt[start:end] if end >= 0 else prompt[start:]).strip()
        if block:
            return block

    pieces: list[str] = []
    if context.client_title:
        pieces.append(f"TITLE: {context.client_title}")
    if context.client_url:
        pieces.append(f"SOURCE: {context.client_url}")
    if context.client_excerpt:
        pieces.append(context.client_excerpt)
    notes = (client_context or "").strip()
    if notes:
        pieces.append(notes)
    return "\n\n".join(pieces).strip()


def _moment(value: object) -> dict[str, str]:
    """Shape one coach moment into the frozen wire object.

    Args:
        value: Whatever the coach returned for this moment, possibly ``None``.

    Returns:
        An object with all four keys. Every field is empty when there was no
        moment, so the frontend can hide the card without a null check.
    """
    return {
        "clientSaid": _str_field(value, "client_said", "clientSaid"),
        "copilotSaid": _str_field(value, "copilot_said", "copilotSaid"),
        "youSaid": _str_field(value, "you_said", "youSaid"),
        "why": _str_field(value, "why"),
    }


def _is_empty_moment(moment: Mapping[str, str]) -> bool:
    """Report whether a moment object carries nothing at all.

    Args:
        moment: A moment object built by :func:`_moment`.

    Returns:
        True when every field is empty.
    """
    return not any(str(value).strip() for value in moment.values())


def _fix_rows(coach: object) -> list[dict[str, str]]:
    """Shape the coach's "what to fix" list into the frozen wire objects.

    Rows that quote nothing are dropped, because a fix the rep cannot tie to
    their own words is not a fix, it is filler.

    Args:
        coach: The coach result object, possibly ``None``.

    Returns:
        At most :data:`MAX_FIXES` rows, each with all three keys.
    """
    rows: list[dict[str, str]] = []
    for raw in _rows(coach, "fixes"):
        row = {
            "youSaid": _str_field(raw, "you_said", "youSaid"),
            "problem": _str_field(raw, "problem"),
            "sayInstead": _str_field(raw, "say_instead", "sayInstead"),
        }
        if not row["problem"] and not row["sayInstead"]:
            continue
        rows.append(row)
        if len(rows) >= MAX_FIXES:
            break
    return rows


def _win_rows(coach: object) -> list[str]:
    """Shape the coach's "what went well" list into plain strings.

    Args:
        coach: The coach result object, possibly ``None``.

    Returns:
        At most :data:`MAX_WINS` non empty lines.
    """
    wins: list[str] = []
    for raw in _rows(coach, "wins"):
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text:
            continue
        wins.append(text)
        if len(wins) >= MAX_WINS:
            break
    return wins


def _fallback_outcome(turns: list[Any]) -> str:
    """Work out how the call ended without asking the model.

    Args:
        turns: The recorded practice turns, oldest first.

    Returns:
        ``"hung_up"`` when the last client line was a hangup, ``"soft_yes"``
        when the client ever agreed, otherwise ``"no_answer"``.
    """
    last_intent = ""
    agreed = False
    for turn in turns:
        if _str_field(turn, "role") != "client":
            continue
        intent = _str_field(turn, "intent")
        if intent:
            last_intent = intent
        if intent == "agree":
            agreed = True
    if last_intent == "hangup":
        return "hung_up"
    if agreed:
        return "soft_yes"
    return FALLBACK_OUTCOME


def _outcome(raw: str, turns: list[Any]) -> str:
    """Accept the coach's outcome only when it is one of the four we score.

    Args:
        raw: The outcome string the coach returned.
        turns: The recorded practice turns, used for the fallback.

    Returns:
        One of :data:`OUTCOMES`.
    """
    value = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if value in OUTCOMES:
        return value
    return _fallback_outcome(turns)


def _transcript_text(turns: list[Any]) -> str:
    """Render the practice turns the way the coach prompt says it reads them.

    ``app.practice.coach_prompt`` tells the model that every line starts with
    ``CLIENT:`` for the fake client, ``YOU:`` for the words the rep really said,
    or ``PROMPTER:`` for the teleprompter line that was on screen at that
    moment. The store renders ``REP:`` instead, and it cannot show the prompter
    line at all, so the transcript the coach reads is built here.

    The prompter line is written just above the rep line it belongs to, because
    that is the order the rep lived it: the line appeared, then they spoke.
    Without it the coach has no copilot quote and can never fill in the best
    moment or the missed moment.

    Args:
        turns: The recorded practice turns, oldest first.

    Returns:
        The whole call as plain labelled lines. Empty when nothing was said.
    """
    lines: list[str] = []
    for turn in turns:
        text = _str_field(turn, "text")
        if not text:
            continue
        if _str_field(turn, "role") == "client":
            lines.append(f"CLIENT: {text}")
            continue
        shown = _str_field(turn, "suggestion_shown", "suggestionShown")
        if shown:
            lines.append(f"PROMPTER: {shown}")
        lines.append(f"YOU: {text}")
    return "\n".join(lines)


def _duration_ms(session: Session) -> int:
    """Measure how long the practice call ran, in milliseconds.

    Args:
        session: The practice session.

    Returns:
        The elapsed time, ``0`` when the call was never started. A call that is
        still open is measured up to now.
    """
    started = float(getattr(session, "started_at", 0.0) or 0.0)
    if started <= 0.0:
        return 0
    ended = float(getattr(session, "ended_at", 0.0) or 0.0)
    if ended <= 0.0:
        ended = time.time()
    return max(0, int((ended - started) * 1000))


async def _run_coach(request: Request, session: Session, turns: list[Any]) -> object:
    """Ask the coach model to read the whole practice call, once.

    Nothing here is allowed to fail the debrief. No turns, no words, no API key,
    a Groq error or a slow answer all end the same way: the scorecard is
    returned with the counted numbers and empty coaching lists.

    Args:
        request: The incoming request, used to reach the shared Groq client.
        session: The practice session.
        turns: The recorded practice turns.

    Returns:
        The coach result object, or ``None`` when the coach did not run.
    """
    if not turns:
        return None
    groq = getattr(request.app.state, "groq", None)
    if groq is None or not groq.configured:
        log.info("coach skipped for session %s, Groq is not configured", session.id)
        return None
    transcript_text = _transcript_text(turns)
    if not transcript_text:
        log.info("coach skipped for session %s, the call has no words in it", session.id)
        return None
    try:
        return await asyncio.wait_for(
            practice_engine.run_coach(
                groq,
                session=session,
                transcript_text=transcript_text,
            ),
            timeout=COACH_BUDGET_SECONDS,
        )
    except Exception:  # noqa: BLE001 - the rep still gets their numbers.
        log.exception("coach failed for session %s", session.id)
        return None


# ====================================================================== #
# routes
# ====================================================================== #


@router.get("/difficulties", response_model=DifficultiesResponse)
async def get_difficulties() -> DifficultiesResponse:
    """List the three practice levels for the setup page.

    Returns:
        The levels in display order, each with its key, label and one line
        blurb. The turn limit and the patience value stay on the server, the
        UI has no use for them.
    """
    levels = [
        {
            "key": str(entry.get("key", "")),
            "label": str(entry.get("label", "")),
            "blurb": str(entry.get("blurb", "")),
        }
        for entry in practice.DIFFICULTIES
        if str(entry.get("key", ""))
    ]
    return DifficultiesResponse.model_validate({"levels": levels})


@router.post("/start", response_model=PracticeStartResponse)
async def post_practice_start(
    payload: PracticeStartRequest,
    request: Request,
) -> PracticeStartResponse:
    """Build a practice call against the same prospect as a real call.

    The context build is not duplicated here. ``prepare_context`` does the
    scrape, the clamping, the prompt fusion and the session creation exactly as
    it does for a live call, so a practice call can never drift from a real one.

    That function creates the session internally and hands back only the
    response model, so the practice fields are set by fetching the session back
    out of the store by its id. The alternative, teaching ``prepare_context``
    about practice mode, would put a practice branch in the middle of the live
    call path for no gain. One extra dictionary lookup is the whole cost of
    keeping that path untouched.

    Args:
        payload: The validated practice start body, which is a prepare context
            body plus the chosen difficulty.
        request: The incoming request, used to reach the shared Groq client.

    Returns:
        Everything the prepare context response carries, plus the mode, the
        level, the client name and the line the client says first.

    Raises:
        HTTPException: 400 when the prospect URL cannot be used at all, 500 in
            the impossible case where the fresh session is already gone.
    """
    groq = get_groq(request)

    # Built by hand rather than passed straight through, so this route works
    # whether or not the practice request model inherits from the context one.
    context_request = PrepareContextRequest(
        knowledge_base=payload.knowledge_base,
        client_url=payload.client_url,
        client_context=payload.client_context,
        call_goal=payload.call_goal,
        language=payload.language,
    )
    try:
        context = await prepare_context(context_request, groq_configured=bool(groq.configured))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"That prospect URL is not usable: {exc}",
        ) from exc

    session = store.get(context.session_id)
    if session is None:
        raise HTTPException(
            status_code=500,
            detail="The practice call was lost right after it was made. Try again.",
        )

    difficulty = _difficulty_key(getattr(payload, "difficulty", DEFAULT_DIFFICULTY))
    persona_name = _persona_name(payload.client_context)

    session.mode = "practice"
    session.difficulty = difficulty
    session.persona_name = persona_name
    session.client_block = _client_block(context, payload.client_context)
    session.knowledge_hint = clamp_on_word_boundary(
        (payload.knowledge_base or "").strip(),
        KNOWLEDGE_HINT_CHARS,
    )
    session.call_goal = (payload.call_goal or "").strip() or FALLBACK_CALL_GOAL

    opening_line = practice.opening_line(difficulty, persona_name)

    log.info(
        "Practice session %s ready, level=%s, name=%s",
        session.id,
        difficulty,
        persona_name or "none",
    )

    return PracticeStartResponse.model_validate(
        {
            **context.model_dump(by_alias=True),
            "mode": "practice",
            "difficulty": difficulty,
            "personaName": persona_name,
            "openingLine": opening_line,
        }
    )


@router.get("/{session_id}/debrief", response_model=DebriefResponse)
async def get_debrief(session_id: str, request: Request) -> DebriefResponse:
    """Score a finished practice call.

    The numbers come from ``scoring.compute_metrics``, the prompter use from
    ``scoring.compute_copilot_use`` and the seven rubric rows from
    ``scoring.compute_breakdown``, all three plain arithmetic over the stored
    turns. Only the outcome, the wins, the fixes, the two moments and the next
    drill come from the coach model, and all of them degrade to empty rather
    than to an error.

    A call the rep ended before saying anything is a normal answer, not a
    failure: it comes back with ``turns`` zero, empty lists and a real score.

    Args:
        session_id: The session the practice call ran in.
        request: The incoming request, used to reach the shared Groq client.

    Returns:
        The full scorecard.

    Raises:
        HTTPException: 404 when the session is unknown or has expired, 409 when
            it is a real call rather than a practice call.
    """
    session = store.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail="That practice call is unknown or has expired.",
        )
    if str(getattr(session, "mode", "live")) != "practice":
        raise HTTPException(
            status_code=409,
            detail="That session is a real call, so there is nothing to score.",
        )

    turns: list[Any] = list(getattr(session, "practice_turns", None) or [])

    metrics: Any = {}
    try:
        metrics = scoring.compute_metrics(turns)
    except Exception:  # noqa: BLE001 - a zeroed card beats a 500.
        log.exception("metrics failed for session %s", session.id)

    # Prompter use is its own count over the same turns. It is not part of the
    # metrics block, and reading it off that object would silently answer zero
    # on every call.
    shown = 0
    used = 0
    used_pct = 0
    try:
        shown, used, used_pct = scoring.compute_copilot_use(turns)
    except Exception:  # noqa: BLE001 - a zeroed panel beats a 500.
        log.exception("prompter use failed for session %s", session.id)

    coach = await _run_coach(request, session, turns)
    outcome = _outcome(_str_field(coach, "outcome"), turns)

    score = 0
    grade = "Rough"
    breakdown: list[dict[str, Any]] = []
    try:
        raw_score, raw_grade, raw_rows = scoring.compute_breakdown(metrics, outcome, used_pct)
        score = int(raw_score)
        grade = str(raw_grade)
        breakdown = [
            {
                "label": _str_field(row, "label"),
                "got": _int_field(row, "got"),
                "outOf": _int_field(row, "out_of", "outOf"),
                "note": _str_field(row, "note"),
            }
            for row in raw_rows
        ]
    except Exception:  # noqa: BLE001 - the rep still sees their numbers.
        log.exception("breakdown failed for session %s", session.id)

    best_moment = _moment(_field(coach, "best_moment", "bestMoment"))
    missed_moment = _moment(_field(coach, "missed_moment", "missedMoment"))
    next_drill = _str_field(
        coach,
        "next_drill",
        "nextDrill",
        default=EMPTY_DRILL if not turns else DEFAULT_DRILL,
    )

    payload: dict[str, Any] = {
        "sessionId": session.id,
        "difficulty": str(getattr(session, "difficulty", "") or DEFAULT_DIFFICULTY),
        "durationMs": _duration_ms(session),
        "turns": len(turns),
        "outcome": outcome,
        "score": score,
        "grade": grade,
        "metrics": {
            "repWords": _int_field(metrics, "rep_words", "repWords"),
            "clientWords": _int_field(metrics, "client_words", "clientWords"),
            "talkingTimePct": _int_field(metrics, "talking_time_pct", "talkingTimePct"),
            "questionsAsked": _int_field(metrics, "questions_asked", "questionsAsked"),
            "objectionsFaced": _int_field(metrics, "objections_faced", "objectionsFaced"),
            "objectionsHandled": _int_field(metrics, "objections_handled", "objectionsHandled"),
            "avgReplyMs": _int_field(metrics, "avg_reply_ms", "avgReplyMs"),
            "fillerWords": _int_field(metrics, "filler_words", "fillerWords"),
            "longestSentenceWords": _int_field(
                metrics, "longest_sentence_words", "longestSentenceWords"
            ),
        },
        "copilot": {
            "suggestionsShown": shown,
            "suggestionsUsed": used,
            "usedPct": used_pct,
            # Both are nullable by contract, and the overlay hides the card on
            # null. An object of four empty strings is not null, so it would
            # draw an empty card with three blank quotes in it.
            "bestMoment": None if _is_empty_moment(best_moment) else best_moment,
            "missedMoment": None if _is_empty_moment(missed_moment) else missed_moment,
        },
        "wins": _win_rows(coach),
        "fixes": _fix_rows(coach),
        "nextDrill": next_drill,
        "breakdown": breakdown,
    }

    log.info(
        "Debrief for session %s, turns=%d, outcome=%s, score=%d",
        session.id,
        len(turns),
        outcome,
        score,
    )
    return DebriefResponse.model_validate(payload)
