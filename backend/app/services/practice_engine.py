"""Model plumbing for practice mode: the synthetic client and the coach.

Practice mode makes two model calls that have nothing to do with the live
teleprompter stream, so they live here and the socket route stays readable.

* :func:`next_client_turn` plays the prospect. It answers whatever the rep just
  said, in character, and returns a small structured record so the caller knows
  the mood, the objection and whether the client just hung up.
* :func:`run_coach` reads the finished transcript once and returns the parts of
  the scorecard a model is actually good at: the outcome, what went well, what
  to fix, the two copilot moments and the drill for next time. Every number in
  the scorecard is counted in Python somewhere else, never asked for here.

Both calls ask Groq for one JSON object and both parse the answer defensively.
Every Groq chat model is a reasoning model, and a reasoning model that decides
to answer with prose, with a markdown fence, with a trailing note or with a near
miss enum value must never kill a live practice call. So every field falls back
to a safe default, near miss values are mapped when the meaning is obvious, and
a completely unparseable persona answer is still spoken as the client's line.

Nothing here touches the session store or the WebSocket. These functions read a
few fields off the session object, build messages, call the model, and hand back
plain data.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.practice import FILLER_NAME, coach_prompt, persona_system_prompt
from app.practice import PERSONA_MAX_TOKENS as _PERSONA_MAX_TOKENS
from app.services.groq_client import GroqClient, GroqError
from app.services.session_store import Session

logger = logging.getLogger(__name__)

__all__ = [
    "COACH_MAX_TOKENS",
    "PERSONA_MAX_TOKENS",
    "ClientTurn",
    "default_coach_result",
    "next_client_turn",
    "run_coach",
]

# ===========================================================================
# Model call settings
# ===========================================================================

#: Response format that forces the model to answer with a single JSON object.
JSON_OBJECT_FORMAT: Final[dict[str, str]] = {"type": "json_object"}

#: Token ceiling for one client line. The persona answers with a tiny JSON
#: object holding one or two spoken sentences, so this is already generous.
# Imported from app.practice so the number lives in exactly one place.
# It was duplicated here once and the two copies drifted, which is how a
# truncated reply turned into invalid JSON in production.
PERSONA_MAX_TOKENS: Final[int] = _PERSONA_MAX_TOKENS

#: The client should sound like a person, not like a template, so the persona
#: runs hotter than the teleprompter does.
PERSONA_TEMPERATURE: Final[float] = 0.8

#: Token ceiling for the coach. It writes two lists, two quoted moments and one
#: drill line, which lands near 600 tokens on a long call.
COACH_MAX_TOKENS: Final[int] = 1200

#: The coach quotes the rep back, so it stays cold and literal.
COACH_TEMPERATURE: Final[float] = 0.25

#: How many past turns the persona sees. A cold call is short and the persona
#: only needs the recent thread to stay in character.
PERSONA_HISTORY_TURNS: Final[int] = 12

#: Sent as the last message of the coach call so the model has a user turn to
#: answer. It also carries the word JSON, see :func:`_ensure_json_word`.
COACH_USER_NUDGE: Final[str] = "Score this call now. Return only the JSON object."

#: Appended to a prompt that never says the word JSON. OpenAI compatible APIs,
#: Groq included, reject a JSON object request whose messages never mention JSON,
#: and that 400 would end a live practice call over a wording detail.
JSON_REMINDER: Final[str] = (
    "Answer with one JSON object and nothing else. No markdown, no notes."
)

# ===========================================================================
# Field limits
# ===========================================================================

#: Hard ceiling on one spoken client line. The prompt asks for one or two short
#: sentences, this is the second lock on the same door.
MAX_SAY_CHARS: Final[int] = 240

#: Length of the fallback line when the model wrote prose instead of JSON.
FALLBACK_SAY_CHARS: Final[int] = 200

#: A cut is only made at a sentence end when enough of the line survives it.
MIN_SENTENCE_CHARS: Final[int] = 40

#: How much of the rep's own line is forwarded to the persona.
MAX_REP_LINE_CHARS: Final[int] = 600

#: How much of the fused system prompt is reused as the client description.
MAX_CLIENT_BLOCK_CHARS: Final[int] = 1600

#: How much of the offer sheet the client is allowed to know about.
MAX_KNOWLEDGE_HINT_CHARS: Final[int] = 700

#: How much transcript the coach reads. The tail is kept, because the end of a
#: call decides the outcome.
MAX_TRANSCRIPT_CHARS: Final[int] = 12000

#: Marker put on top of a transcript that had to be cut.
TRANSCRIPT_CUT_MARKER: Final[str] = "[the earlier part of this call was cut]\n\n"

#: How many wins and fixes are kept. The contract asks for two or three.
MAX_LIST_ITEMS: Final[int] = 3

#: Ceilings on the coach's own text, so one runaway sentence cannot break the UI.
MAX_WIN_CHARS: Final[int] = 220
MAX_QUOTE_CHARS: Final[int] = 260
MAX_DRILL_CHARS: Final[int] = 200

# ===========================================================================
# Enums and safe defaults
# ===========================================================================

MOODS: Final[frozenset[str]] = frozenset({"cold", "neutral", "warm"})
"""Allowed values of the persona ``mood`` field."""

INTENTS: Final[frozenset[str]] = frozenset(
    {"question", "objection", "brushoff", "agree", "hangup"}
)
"""Allowed values of the persona ``intent`` field."""

OBJECTIONS: Final[frozenset[str]] = frozenset(
    {
        "too_expensive",
        "not_interested",
        "no_time",
        "have_vendor",
        "send_email",
        "who_are_you",
        "none",
    }
)
"""Allowed values of the persona ``objection`` field, matching the quick actions."""

OUTCOMES: Final[frozenset[str]] = frozenset(
    {"booked", "soft_yes", "no_answer", "hung_up"}
)
"""Allowed values of the coach ``outcome`` field."""

DIFFICULTIES: Final[frozenset[str]] = frozenset({"warm", "normal", "brutal"})
"""The three frozen practice levels. An unknown value falls back to ``normal``."""

DEFAULT_MOOD: Final[str] = "neutral"
DEFAULT_INTENT: Final[str] = "question"
DEFAULT_OBJECTION: Final[str] = "none"
DEFAULT_OUTCOME: Final[str] = "no_answer"
DEFAULT_DIFFICULTY: Final[str] = "normal"
DEFAULT_LANGUAGE: Final[str] = "en"

#: Spoken when the JSON parsed but carried no line. Silence would look like a
#: dropped call, and a real person does say this when they miss a sentence.
EMPTY_SAY_FALLBACK: Final[str] = "Sorry, say that again."

#: Stands in for the rep turn when the microphone caught nothing usable.
NO_REP_LINE: Final[str] = "The caller said nothing clear."

#: Used when the coach returns no drill of its own.
DEFAULT_NEXT_DRILL: Final[str] = "Ask one short question early, then stop and listen."

#: Near miss values the model writes now and then. Mapping the obvious ones is
#: better than throwing a good turn away and calling it neutral.
MOOD_ALIASES: Final[dict[str, str]] = {
    "angry": "cold",
    "annoyed": "cold",
    "cold_and_short": "cold",
    "curious": "warm",
    "flat": "neutral",
    "friendly": "warm",
    "happy": "warm",
    "interested": "warm",
    "irritated": "cold",
    "normal": "neutral",
    "open": "warm",
    "rude": "cold",
    "tired": "neutral",
}

INTENT_ALIASES: Final[dict[str, str]] = {
    "accept": "agree",
    "agreed": "agree",
    "agreement": "agree",
    "answer": "question",
    "asking": "question",
    "brush_off": "brushoff",
    "deflect": "brushoff",
    "deflection": "brushoff",
    "end_call": "hangup",
    "ending_call": "hangup",
    "hang_up": "hangup",
    "hangs_up": "hangup",
    "hung_up": "hangup",
    "objecting": "objection",
    "push_back": "objection",
    "yes": "agree",
}

OBJECTION_ALIASES: Final[dict[str, str]] = {
    "busy": "no_time",
    "cost": "too_expensive",
    "email": "send_email",
    "existing_vendor": "have_vendor",
    "expensive": "too_expensive",
    "identity": "who_are_you",
    "no_interest": "not_interested",
    "no_objection": "none",
    "null": "none",
    "price": "too_expensive",
    "send_me_an_email": "send_email",
    "time": "no_time",
    "vendor": "have_vendor",
    "who_is_this": "who_are_you",
}

OUTCOME_ALIASES: Final[dict[str, str]] = {
    "booked_meeting": "booked",
    "call_booked": "booked",
    "hang_up": "hung_up",
    "hangup": "hung_up",
    "hungup": "hung_up",
    "maybe": "soft_yes",
    "meeting_booked": "booked",
    "no": "no_answer",
    "no_result": "no_answer",
    "nothing": "no_answer",
    "soft_agree": "soft_yes",
    "soft_no": "no_answer",
    "yes": "soft_yes",
}

# ===========================================================================
# Section headings of the fused system prompt
# ===========================================================================

#: Headings frozen in ``app.prompts.SYSTEM_TEMPLATE``. The session already
#: carries the fused system prompt, so the client description, the offer sheet
#: and the call goal can be read back out of it. That keeps this module working
#: whatever extra fields the practice session ends up storing.
SECTION_KNOWLEDGE: Final[str] = "# MY SERVICES & OFFERS"
SECTION_CLIENT: Final[str] = "# TARGET CLIENT INFO"
SECTION_GOAL: Final[str] = "# CALL GOAL"

_HEADING_RE = re.compile(r"^#\s", re.MULTILINE)

_FENCE_HEAD = re.compile(r"^```[A-Za-z0-9_+-]*")
_FENCE_ONLY = re.compile(r"^```[A-Za-z0-9_+-]*\s*$")

#: One ``"key": "value"`` pair inside a JSON object. Used only to salvage fields
#: from an answer that ``json.loads`` already refused.
_STRING_FIELD = re.compile(r'"([A-Za-z_][A-Za-z0-9_ ]*)"\s*:\s*"((?:[^"\\]|\\.)*)"')

#: Typographic characters folded to plain ASCII before anything is shown to the
#: rep. The streaming client already does this, doing it again here costs
#: nothing and protects any future caller.
#: The characters are written as escapes on purpose. The source of this project
#: never carries a long dash character, not even inside a lookup table.
_PUNCTUATION_FIXES: Final[tuple[tuple[str, str], ...]] = (
    ("\u2014", ", "),   # long dash
    ("\u2013", "-"),    # short dash
    ("\u2018", "'"),
    ("\u2019", "'"),
    ("\u201c", '"'),
    ("\u201d", '"'),
    ("\u2026", "..."),  # three dots in one character
    ("\u00a0", " "),    # non breaking space
)


@dataclass
class ClientTurn:
    """One spoken line from the synthetic client, plus its control signals.

    Attributes:
        say: The words the browser speaks out loud. Always plain text, always
            trimmed to :data:`MAX_SAY_CHARS`, never empty.
        mood: One of :data:`MOODS`. The scorecard uses it to see whether the
            rep warmed the client up.
        intent: One of :data:`INTENTS`. ``hangup`` means the call is over.
        objection: One of :data:`OBJECTIONS`, or ``none`` when this line was not
            an objection.
    """

    say: str
    mood: str
    intent: str
    objection: str


#: Canned lines, one per difficulty, used only when Groq itself is unreachable
#: or rate limited. A practice call must never stall on a dead API: the rep is
#: mid sentence and a silent screen teaches them nothing.
_LAST_RESORT_LINES: Final[dict[str, ClientTurn]] = {
    "warm": ClientTurn(
        say="Sorry, could you say that again?",
        mood="neutral",
        intent="question",
        objection="none",
    ),
    "normal": ClientTurn(
        say="Look, I did not catch that. What do you want?",
        mood="neutral",
        intent="question",
        objection="who_are_you",
    ),
    "brutal": ClientTurn(
        say="I have no time for this. Just send an email.",
        mood="cold",
        intent="objection",
        objection="send_email",
    ),
}


async def _persona_with_fallback(
    groq: GroqClient,
    messages: list[dict[str, str]],
    session: Session,
) -> ClientTurn:
    """Get the client's next line, and never come back empty handed.

    Three layers, because the practice call has to keep moving:

    1. Ask with the JSON response format on. This is the normal path and gives
       the cleanest structure.
    2. If that call fails, ask again with the format off. Groq answers a JSON
       mode request with a 502 when the model truncates mid object, and the
       plain call usually still returns parseable JSON that
       :func:`parse_client_turn` handles.
    3. If the API is genuinely down or rate limited, return the canned line for
       this difficulty so the rep can carry on practising.

    Args:
        groq: The shared Groq client.
        messages: The fully built message list.
        session: The practice session, read for the difficulty.

    Returns:
        A usable ClientTurn, always.
    """
    difficulty = _difficulty_of(session)

    try:
        raw = await groq.complete(
            messages,
            max_tokens=PERSONA_MAX_TOKENS,
            temperature=PERSONA_TEMPERATURE,
            response_format=JSON_OBJECT_FORMAT,
        )
        return parse_client_turn(raw)
    except GroqError as first:
        logger.warning("persona json call failed (%s), retrying without the format", first)

    try:
        raw = await groq.complete(
            messages,
            max_tokens=PERSONA_MAX_TOKENS,
            temperature=PERSONA_TEMPERATURE,
        )
        return parse_client_turn(raw)
    except GroqError as second:
        logger.warning("persona retry failed too (%s), using the canned line", second)

    return _LAST_RESORT_LINES.get(difficulty, _LAST_RESORT_LINES["normal"])


async def next_client_turn(
    groq: GroqClient,
    *,
    session: Session,
    rep_said: str,
    history: Sequence[Any],
) -> ClientTurn:
    """Ask the persona what the client says next.

    The persona sees its own system prompt, the recent thread of the call and
    the line the rep just said. Copilot turns are never forwarded, because the
    client must not know what the rep is being fed.

    Args:
        groq: The shared Groq client.
        session: The practice session. Only a few plain fields are read from it,
            nothing is written and the session store is never touched.
        rep_said: The rep's latest line. May be empty when the microphone caught
            nothing usable.
        history: The recent turns of the call, oldest first. Each item may be a
            :class:`~app.services.session_store.Turn` or a plain mapping with a
            ``role`` and a ``text`` (or ``content``) key.

    Returns:
        A :class:`ClientTurn` that is always safe to speak and always carries a
        valid mood, intent and objection.

    Raises:
        GroqError: When the network call itself fails. A bad answer never
            raises, it falls back. Only a dead API does.
    """
    started = time.perf_counter()

    prompt = persona_system_prompt(
        difficulty=_difficulty_of(session),
        client_block=_client_block_of(session),
        knowledge_hint=_knowledge_hint_of(session),
        language=_language_of(session),
        name=_persona_name_of(session),
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _ensure_json_word(prompt)}
    ]
    messages.extend(_history_messages(history))

    rep_line = _clean_line(rep_said, MAX_REP_LINE_CHARS) or NO_REP_LINE
    last = messages[-1]
    if last["role"] != "user" or last["content"] != rep_line:
        messages.append({"role": "user", "content": rep_line})

    turn = await _persona_with_fallback(groq, messages, session)

    logger.debug(
        "persona turn in %.0f ms, intent=%s mood=%s objection=%s chars=%d",
        (time.perf_counter() - started) * 1000.0,
        turn.intent,
        turn.mood,
        turn.objection,
        len(turn.say),
    )
    return turn


async def run_coach(
    groq: GroqClient,
    *,
    session: Session,
    transcript_text: str,
) -> dict[str, Any]:
    """Ask the coach to grade the finished call.

    Only the judgement calls are asked for. Every count and every percentage in
    the scorecard is arithmetic done elsewhere, because a model cannot count.

    Args:
        groq: The shared Groq client.
        session: The practice session, read only.
        transcript_text: The whole call as plain text, already formatted by the
            caller. Long transcripts are cut from the front.

    Returns:
        A dict with the keys ``outcome``, ``wins``, ``fixes``, ``bestMoment``,
        ``missedMoment`` and ``nextDrill``. Every key is always present, so the
        caller can build the debrief response without a KeyError. ``bestMoment``
        and ``missedMoment`` are either None or a dict with the four keys
        ``clientSaid``, ``copilotSaid``, ``youSaid`` and ``why``.

    Raises:
        GroqError: When the network call itself fails.
    """
    started = time.perf_counter()

    prompt = coach_prompt(
        transcript_text=_clamp_transcript(transcript_text),
        call_goal=_call_goal_of(session),
        difficulty=_difficulty_of(session),
        language=_language_of(session),
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _ensure_json_word(prompt)},
        {"role": "user", "content": COACH_USER_NUDGE},
    ]

    raw = await groq.complete(
        messages,
        max_tokens=COACH_MAX_TOKENS,
        temperature=COACH_TEMPERATURE,
        response_format=JSON_OBJECT_FORMAT,
    )
    result = parse_coach_result(raw)

    logger.debug(
        "coach ran in %.0f ms, outcome=%s wins=%d fixes=%d",
        (time.perf_counter() - started) * 1000.0,
        result["outcome"],
        len(result["wins"]),
        len(result["fixes"]),
    )
    return result


def default_coach_result() -> dict[str, Any]:
    """Build the empty scorecard the coach falls back to.

    The caller also uses this for a call that ended before anyone said anything,
    which the contract answers with 200 and empty lists.

    Returns:
        A fresh dict, safe to mutate, with a valid value under every key.
    """
    return {
        "outcome": DEFAULT_OUTCOME,
        "wins": [],
        "fixes": [],
        "bestMoment": None,
        "missedMoment": None,
        "nextDrill": DEFAULT_NEXT_DRILL,
    }


# ===========================================================================
# Parsing
# ===========================================================================


def parse_client_turn(raw: str) -> ClientTurn:
    """Turn one persona answer into a :class:`ClientTurn`, whatever it looks like.

    Four levels of falling back, in order:

    1. Plain JSON, which is what the response format asks for.
    2. JSON wrapped in a markdown fence, or with a note around it.
    3. Field by field salvage with a regex, for JSON broken by a stray quote.
    4. The raw text itself, spoken as the client's line.

    Args:
        raw: The model answer, exactly as it arrived.

    Returns:
        A turn whose four fields are always valid.
    """
    text = _strip_fence(raw)
    data = _load_object(text)
    if data is None:
        data = _salvage_strings(text, frozenset({"say", "mood", "intent", "objection"}))
        parsed_json = False
    else:
        parsed_json = True

    say = _clean_line(_field(data, "say", "text", "line"), MAX_SAY_CHARS)
    if not say:
        if parsed_json or not text:
            # The shape was right but the line was missing, or there is nothing
            # to speak at all. Inventing a short human filler beats silence.
            say = EMPTY_SAY_FALLBACK
        else:
            # The model wrote prose. Speak the prose, that is still a client
            # line, and keep the safe defaults for everything else.
            say = _clean_line(text, FALLBACK_SAY_CHARS) or EMPTY_SAY_FALLBACK

    return ClientTurn(
        say=say,
        mood=_pick(_field(data, "mood"), MOODS, DEFAULT_MOOD, MOOD_ALIASES),
        intent=_pick(_field(data, "intent"), INTENTS, DEFAULT_INTENT, INTENT_ALIASES),
        objection=_pick(
            _field(data, "objection"),
            OBJECTIONS,
            DEFAULT_OBJECTION,
            OBJECTION_ALIASES,
        ),
    )


def parse_coach_result(raw: str) -> dict[str, Any]:
    """Turn one coach answer into the scorecard dict.

    Args:
        raw: The model answer, exactly as it arrived.

    Returns:
        The same shape :func:`default_coach_result` returns, with whatever the
        model gave us filled in. Nothing here can raise on bad input.
    """
    result = default_coach_result()
    text = _strip_fence(raw)
    data = _load_object(text)

    if data is None:
        # Salvage the two flat strings and give up on the lists. Half a
        # scorecard beats an error page after a call the rep just finished.
        data = _salvage_strings(text, frozenset({"outcome", "nextdrill", "drill"}))

    result["outcome"] = _pick(
        _field(data, "outcome", "result"), OUTCOMES, DEFAULT_OUTCOME, OUTCOME_ALIASES
    )
    result["wins"] = _string_list(_field(data, "wins", "whatWentWell", "good"))
    result["fixes"] = _fix_list(_field(data, "fixes", "whatToFix", "improvements"))

    # Some answers nest the two moments under a copilot object, the way the
    # scorecard itself is shaped. Each moment is looked up at the top level
    # first and inside that object second, so either shape works.
    nested = _field(data, "copilot", "prompter")
    inner: Mapping[str, Any] | None = nested if isinstance(nested, Mapping) else None
    result["bestMoment"] = _moment(
        _field(data, "bestMoment", "best") or _field(inner, "bestMoment", "best")
    )
    result["missedMoment"] = _moment(
        _field(data, "missedMoment", "missed") or _field(inner, "missedMoment", "missed")
    )

    drill = _clean_line(_field(data, "nextDrill", "drill", "practiseNext"), MAX_DRILL_CHARS)
    result["nextDrill"] = drill or DEFAULT_NEXT_DRILL
    return result


def _strip_fence(raw: object) -> str:
    """Remove a markdown code fence the model added anyway.

    Args:
        raw: The model answer. Anything that is not a string becomes "".

    Returns:
        The text with a leading ```` ```json ```` line and any trailing fence
        removed, stripped at both ends.
    """
    if not isinstance(raw, str):
        return ""
    cleaned = raw.strip()
    if "```" not in cleaned:
        return cleaned

    lines = cleaned.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].lstrip().startswith("```"):
        head = _FENCE_HEAD.sub("", lines[0].lstrip(), count=1).strip()
        if head:
            lines[0] = head
        else:
            lines.pop(0)
    while lines and _FENCE_ONLY.match(lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip()


def _load_object(text: str) -> dict[str, Any] | None:
    """Parse a JSON object out of a model answer, tolerating extra text.

    Args:
        text: The fence stripped answer.

    Returns:
        The decoded object, or None when nothing object shaped could be read.
    """
    if not text:
        return None

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        data = None

    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                return item
        return None

    # A note before or after the object, which is the most common miss.
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _salvage_strings(text: str, keys: frozenset[str]) -> dict[str, Any]:
    """Pull flat string fields out of JSON that will not decode.

    One unescaped quote inside a sentence is enough to make a whole answer
    unreadable to :func:`json.loads`, and the rest of the fields are still
    sitting there in plain sight.

    Args:
        text: The fence stripped answer.
        keys: Normalised key names worth keeping, lower case and without
            underscores, for example ``{"say", "mood"}``.

    Returns:
        A dict of the fields that were found, possibly empty.
    """
    found: dict[str, Any] = {}
    for match in _STRING_FIELD.finditer(text):
        key = _normalise_key(match.group(1))
        if key not in keys or key in found:
            continue
        value = match.group(2)
        try:
            found[key] = json.loads(f'"{value}"')
        except (json.JSONDecodeError, ValueError):
            found[key] = value.replace('\\"', '"').replace("\\n", " ")
    return found


def _normalise_key(key: object) -> str:
    """Fold a key so ``nextDrill``, ``next_drill`` and ``Next Drill`` all match.

    Args:
        key: The raw key from the model.

    Returns:
        The key in lower case with underscores and spaces removed.
    """
    return str(key).replace("_", "").replace(" ", "").lower()


def _field(data: Mapping[str, Any] | None, *names: str) -> Any:
    """Read the first present key out of a model object, spelling insensitive.

    Args:
        data: The decoded object, or None.
        *names: Candidate key names, best first. Case and underscores do not
            matter, so passing ``"bestMoment"`` also finds ``"best_moment"``.

    Returns:
        The first value that is present and not None, otherwise None.
    """
    if not isinstance(data, Mapping):
        return None
    lookup = {_normalise_key(key): value for key, value in data.items()}
    for name in names:
        value = lookup.get(_normalise_key(name))
        if value is not None:
            return value
    return None


def _pick(
    value: object,
    allowed: frozenset[str],
    default: str,
    aliases: Mapping[str, str],
) -> str:
    """Coerce a model value onto one of the allowed enum values.

    Args:
        value: Whatever the model put in the field.
        allowed: The valid values.
        default: What to return when nothing matches.
        aliases: Near miss values mapped onto valid ones.

    Returns:
        Always a member of ``allowed``.
    """
    if not isinstance(value, str):
        return default
    token = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    if token in allowed:
        return token
    mapped = aliases.get(token)
    if mapped is not None and mapped in allowed:
        return mapped
    return default


def _clean_line(value: object, limit: int) -> str:
    """Flatten one model string into a plain single line.

    Args:
        value: Whatever the model put in the field.
        limit: Maximum characters to keep. Zero or less means no cut.

    Returns:
        A single line of plain ASCII punctuation, cut on a sentence end when it
        is too long. Empty when there was nothing usable.
    """
    if not isinstance(value, str):
        return ""
    text = value
    for bad, good in _PUNCTUATION_FIXES:
        if bad in text:
            text = text.replace(bad, good)
    text = " ".join(text.split()).strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    if limit > 0:
        text = _cut(text, limit)
    return text


def _cut(text: str, limit: int) -> str:
    """Shorten text without leaving half a word or half a thought behind.

    Args:
        text: The single line text.
        limit: Maximum characters to keep.

    Returns:
        The text cut at the last sentence end before the limit, or at the last
        space when there is no sentence end far enough in.
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    end = max(head.rfind("."), head.rfind("?"), head.rfind("!"))
    if end >= MIN_SENTENCE_CHARS:
        return head[: end + 1].strip()
    space = head.rfind(" ")
    if space > 0:
        return head[:space].strip()
    return head.strip()


def _string_list(value: object) -> list[str]:
    """Clean a list of plain sentences from the coach.

    Args:
        value: The raw field. A single string is accepted as a one item list.

    Returns:
        At most :data:`MAX_LIST_ITEMS` non empty lines.
    """
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = list(value)
    else:
        return []

    out: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            item = _field(item, "text", "win", "say")
        line = _clean_line(item, MAX_WIN_CHARS)
        if line:
            out.append(line)
        if len(out) >= MAX_LIST_ITEMS:
            break
    return out


def _fix_list(value: object) -> list[dict[str, str]]:
    """Clean the list of fixes, dropping anything the rep cannot act on.

    Args:
        value: The raw field.

    Returns:
        At most :data:`MAX_LIST_ITEMS` dicts, each with ``youSaid``, ``problem``
        and ``sayInstead`` as strings.
    """
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []

    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        fix = {
            "youSaid": _clean_line(_field(item, "youSaid", "said"), MAX_QUOTE_CHARS),
            "problem": _clean_line(_field(item, "problem", "why"), MAX_WIN_CHARS),
            "sayInstead": _clean_line(
                _field(item, "sayInstead", "better", "instead"), MAX_QUOTE_CHARS
            ),
        }
        # A fix with no better line is just a complaint, so it is dropped.
        if not fix["sayInstead"]:
            continue
        out.append(fix)
        if len(out) >= MAX_LIST_ITEMS:
            break
    return out


def _moment(value: object) -> dict[str, str] | None:
    """Clean one copilot moment.

    Args:
        value: The raw field.

    Returns:
        A dict with ``clientSaid``, ``copilotSaid``, ``youSaid`` and ``why``, or
        None when the model had nothing real to show. The caller must handle
        None for both moments, not only for the missed one.
    """
    if not isinstance(value, Mapping):
        return None
    moment = {
        "clientSaid": _clean_line(_field(value, "clientSaid", "client"), MAX_QUOTE_CHARS),
        "copilotSaid": _clean_line(
            _field(value, "copilotSaid", "copilot", "prompter"), MAX_QUOTE_CHARS
        ),
        "youSaid": _clean_line(_field(value, "youSaid", "rep", "said"), MAX_QUOTE_CHARS),
        "why": _clean_line(_field(value, "why", "reason"), MAX_WIN_CHARS),
    }
    if not any(moment.values()):
        return None
    return moment


# ===========================================================================
# Message building
# ===========================================================================


def _history_messages(history: Sequence[Any]) -> list[dict[str, str]]:
    """Turn recent call turns into messages the persona can answer.

    The persona is the client, so a client turn is the assistant and a rep turn
    is the user. Copilot turns are dropped on purpose: the client never sees
    what the rep is being fed, and feeding it back would break character.

    Args:
        history: Recent turns, oldest first. Items may be dataclass turns or
            plain mappings.

    Returns:
        At most :data:`PERSONA_HISTORY_TURNS` messages, oldest first.
    """
    messages: list[dict[str, str]] = []
    for item in list(history)[-PERSONA_HISTORY_TURNS:]:
        role, text = _role_and_text(item)
        if not text:
            continue
        if role in ("client", "assistant", "prospect"):
            messages.append({"role": "assistant", "content": text})
        elif role in ("rep", "user", "me"):
            messages.append({"role": "user", "content": text})
    return messages


def _role_and_text(item: object) -> tuple[str, str]:
    """Read the role and the text off one history item.

    Every shape is accepted so the caller can pass ``session.turns`` straight
    in, a list of plain dicts, or the stored practice records. An item with a
    ``say`` and no role at all is read as a client line.

    Args:
        item: A turn object or a mapping.

    Returns:
        The lower case role and the stripped text, either of which may be empty.
    """
    if isinstance(item, Mapping):
        role = item.get("role") or ""
        said = item.get("say") or ""
        text = item.get("text") or item.get("content") or said
    else:
        role = getattr(item, "role", "") or ""
        said = getattr(item, "say", "") or ""
        text = getattr(item, "text", "") or getattr(item, "content", "") or said
    if not role and said:
        # A stored practice client record carries a spoken line and no role.
        role = "client"
    return str(role).strip().lower(), _clean_line(str(text), MAX_REP_LINE_CHARS)


def _ensure_json_word(prompt: str) -> str:
    """Guarantee the word JSON appears somewhere in the prompt.

    OpenAI compatible APIs refuse a JSON object request whose messages never
    mention JSON, and Groq is one of them. Losing a live practice call to that
    400 would be a silly way to fail, so the reminder is appended when the
    prompt author did not write the word themselves.

    Args:
        prompt: The prompt text.

    Returns:
        The prompt, with one short reminder line appended when it was needed.
    """
    text = prompt if isinstance(prompt, str) else ""
    if "json" in text.lower():
        return text
    return f"{text}\n\n{JSON_REMINDER}" if text else JSON_REMINDER


# ===========================================================================
# Reading the session
# ===========================================================================


def _attr_text(session: Session, *names: str) -> str:
    """Return the first non empty string attribute found on the session.

    Practice adds its own fields to the session, and this module must keep
    working whichever of them exist, so nothing here indexes a field blindly.

    Args:
        session: The session object.
        *names: Attribute names to try, best first.

    Returns:
        The stripped value, or "" when none of the attributes carry text.
    """
    for name in names:
        value = getattr(session, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _difficulty_of(session: Session) -> str:
    """Return the practice level of this session.

    Args:
        session: The session object.

    Returns:
        One of the three frozen keys. Anything unknown becomes ``normal``.
    """
    value = _attr_text(session, "difficulty", "practice_difficulty").lower()
    return value if value in DIFFICULTIES else DEFAULT_DIFFICULTY


def _language_of(session: Session) -> str:
    """Return the answer language code of this session.

    Args:
        session: The session object.

    Returns:
        The language code, or ``en``.
    """
    return _attr_text(session, "language") or DEFAULT_LANGUAGE


def _persona_name_of(session: Session) -> str:
    """Return the name the client answers to.

    Args:
        session: The session object.

    Returns:
        The stored persona name, or the neutral filler from ``app.practice`` so
        the prompt never has to handle a missing name.
    """
    return _attr_text(session, "persona_name", "client_name") or FILLER_NAME


def _client_block_of(session: Session) -> str:
    """Return the description of the client the persona has to play.

    A practice session may store the block directly. When it does not, the block
    is read back out of the fused system prompt, which was built from the same
    text under the frozen ``# TARGET CLIENT INFO`` heading.

    Args:
        session: The session object.

    Returns:
        The client description, cut to :data:`MAX_CLIENT_BLOCK_CHARS`. Never
        empty, so the prompt always has something to work with.
    """
    block = _attr_text(session, "client_block", "client_context")
    if not block:
        block = _section(_attr_text(session, "system_prompt"), SECTION_CLIENT)
    if not block:
        title = _attr_text(session, "client_title")
        url = _attr_text(session, "client_url")
        block = " ".join(part for part in (title, url) if part)
    if not block:
        block = "No public details were found about this client."
    return _cut(block, MAX_CLIENT_BLOCK_CHARS)


def _knowledge_hint_of(session: Session) -> str:
    """Return a short hint about what the rep is selling.

    The client only needs the gist, enough to push back with a real price or a
    real service name, so this is deliberately much shorter than the offer sheet
    the copilot reads.

    Args:
        session: The session object.

    Returns:
        The hint text, cut to :data:`MAX_KNOWLEDGE_HINT_CHARS`. May be empty.
    """
    hint = _attr_text(session, "knowledge_hint", "knowledge_base")
    if not hint:
        hint = _section(_attr_text(session, "system_prompt"), SECTION_KNOWLEDGE)
    return _cut(hint, MAX_KNOWLEDGE_HINT_CHARS)


def _call_goal_of(session: Session) -> str:
    """Return what the rep was trying to achieve on this call.

    Args:
        session: The session object.

    Returns:
        The call goal, or a plain default when the session has none.
    """
    goal = _attr_text(session, "call_goal", "goal")
    if not goal:
        goal = _section(_attr_text(session, "system_prompt"), SECTION_GOAL)
    return goal or "Book a short call with this client."


def _section(system_prompt: str, heading: str) -> str:
    """Read one block out of the fused system prompt.

    Args:
        system_prompt: The fused prompt built by ``app.prompts``.
        heading: The exact heading line, for example ``# CALL GOAL``.

    Returns:
        The text under that heading up to the next heading, stripped. Empty when
        the heading is not there.
    """
    if not system_prompt or heading not in system_prompt:
        return ""
    body = system_prompt.split(heading, 1)[1].lstrip("\n")
    match = _HEADING_RE.search(body)
    if match is not None:
        body = body[: match.start()]
    return body.strip()


def _clamp_transcript(text: str) -> str:
    """Cut a long transcript down to what the coach can read.

    The tail is kept because the end of a cold call decides the outcome.

    Args:
        text: The whole call as plain text.

    Returns:
        The transcript, marked at the top when something was cut.
    """
    cleaned = (text or "").strip()
    if len(cleaned) <= MAX_TRANSCRIPT_CHARS:
        return cleaned
    tail = cleaned[-MAX_TRANSCRIPT_CHARS:]
    newline = tail.find("\n")
    if 0 <= newline < 400:
        tail = tail[newline + 1 :]
    return TRANSCRIPT_CUT_MARKER + tail.strip()


if __name__ == "__main__":  # pragma: no cover - hand run sanity check
    # Self test for the parsing half of this module. It runs no network calls,
    # it only proves that a badly behaved model cannot end a practice call.
    good = (
        '{"say": "Who is this?", "mood": "cold", '
        '"intent": "question", "objection": "who_are_you"}'
    )
    turn = parse_client_turn(good)
    assert turn.say == "Who is this?", turn.say
    assert turn.mood == "cold"
    assert turn.intent == "question"
    assert turn.objection == "who_are_you"

    fenced = "```json\n{\"say\": \"I am busy.\", \"intent\": \"hang_up\", \"mood\": \"angry\"}\n```"
    turn = parse_client_turn(fenced)
    assert turn.say == "I am busy.", turn.say
    assert turn.intent == "hangup", turn.intent
    assert turn.mood == "cold", turn.mood
    assert turn.objection == "none", turn.objection

    chatty = (
        "Here you go:\n"
        '{"say": "Send me an email.", "objection": "email", "intent": "brush_off"}\n'
        "Hope that helps."
    )
    turn = parse_client_turn(chatty)
    assert turn.say == "Send me an email.", turn.say
    assert turn.objection == "send_email", turn.objection
    assert turn.intent == "brushoff", turn.intent

    broken = '{"say": "He told me "no" last week.", "mood": "cold"}'
    turn = parse_client_turn(broken)
    assert turn.say.startswith("He told me"), turn.say
    assert turn.mood == "cold", turn.mood

    prose = "Look, I really do not have time for this today, call me next month."
    turn = parse_client_turn(prose)
    assert turn.say == prose, turn.say
    assert (turn.mood, turn.intent, turn.objection) == ("neutral", "question", "none")

    turn = parse_client_turn("")
    assert turn.say == EMPTY_SAY_FALLBACK, turn.say

    long_say = (
        "We already have a company doing this for us. "
        "They have been fine for two years now. "
        "I am not going to change that today, and I do not want to talk about price. "
        "Send something over if you must."
    )
    turn = parse_client_turn(json.dumps({"say": long_say, "intent": "objection"}))
    assert len(turn.say) <= MAX_SAY_CHARS, len(turn.say)
    assert turn.say.endswith("."), turn.say
    assert turn.say in long_say.replace("  ", " "), turn.say

    coach_raw = json.dumps(
        {
            "outcome": "meeting_booked",
            "wins": ["You asked for a time.", "You kept it short.", "Good tone.", "Extra"],
            "fixes": [
                {"youSaid": "Umm hi", "problem": "Weak start.", "sayInstead": "Hi, this is Ali."},
                {"youSaid": "ok", "problem": "No ask."},
            ],
            "copilot": {
                "bestMoment": {
                    "clientSaid": "Too costly.",
                    "copilotSaid": "Ask what they pay now.",
                    "youSaid": "What do you pay now?",
                    "why": "You used the line.",
                }
            },
            "nextDrill": "Ask for a day and a time.",
        }
    )
    coach = parse_coach_result(coach_raw)
    assert coach["outcome"] == "booked", coach["outcome"]
    assert len(coach["wins"]) == MAX_LIST_ITEMS, coach["wins"]
    assert len(coach["fixes"]) == 1, coach["fixes"]
    assert coach["fixes"][0]["sayInstead"] == "Hi, this is Ali."
    assert coach["bestMoment"] is not None
    assert coach["bestMoment"]["youSaid"] == "What do you pay now?"
    assert coach["missedMoment"] is None
    assert coach["nextDrill"] == "Ask for a day and a time."

    empty = parse_coach_result("the call was too short to score")
    assert empty == default_coach_result(), empty
    assert set(default_coach_result()) == {
        "outcome",
        "wins",
        "fixes",
        "bestMoment",
        "missedMoment",
        "nextDrill",
    }

    assert _section("# CALL GOAL\nBook a call.\n\n# TASK\nDo it.", SECTION_GOAL) == "Book a call."
    assert _ensure_json_word("say hi").endswith(JSON_REMINDER)
    assert _ensure_json_word("return json") == "return json"

    print("practice_engine self test passed")
