"""Practice mode personas, opener lines and the two prompts that drive them.

Practice mode puts a synthetic client on the other end of the line so the rep can
rehearse before a real call. Two very different prompts live here:

* :func:`persona_system_prompt` turns the model into the CLIENT. In that call it
  is not an assistant, it is a stranger whose phone just rang, and it answers
  with a small JSON object so the socket layer knows when the call is over.
* :func:`coach_prompt` runs once at the end, over the whole transcript, and
  writes the scorecard words the rep actually reads.

Like :mod:`app.prompts` this module is pure text. It borrows one helper from
``app.prompts``, imports nothing else from the app, performs no IO, and can be
imported from any layer and tested on its own.

Everything the model writes here is read by a rep whose first language is not
English, so every prompt repeats two rules that matter more than they look:
plain grade 4 English, and never an em dash.
"""

from __future__ import annotations

import re
from typing import Final

from app.prompts import language_name

# ===========================================================================
# Model budgets
# ===========================================================================

# 220, not 120. A brutal client plus the call history can push the JSON past a
# 120 token budget, and a truncated object makes Groq's JSON mode answer 502.
PERSONA_MAX_TOKENS: Final[int] = 220
"""Token ceiling for one persona turn.

The persona answers with a tiny JSON object holding at most two short spoken
sentences, so this is generous on purpose: it leaves room for the JSON wrapper
without ever letting the model write a speech.
"""

COACH_MAX_TOKENS: Final[int] = 900
"""Token ceiling for the single end of call coach completion.

The scorecard carries two lists, two moment objects and a drill line, so the
coach needs real room. It runs once per practice call, never in the hot path.
"""

PERSONA_TEMPERATURE: Final[float] = 0.85
"""Persona sampling temperature.

A cold client is boring when it is deterministic, it repeats the same brush off
every turn. High temperature is what makes the practice call feel alive.
"""

COACH_TEMPERATURE: Final[float] = 0.25
"""Coach sampling temperature.

The coach has to copy the rep's words letter for letter and pick one outcome
from a fixed list, so it is kept close to deterministic.
"""

# ===========================================================================
# Difficulty levels
# ===========================================================================

FILLER_NAME: Final[str] = "the owner"
"""Neutral stand in used in the opener when no name was found in the context."""

DEFAULT_DIFFICULTY: Final[str] = "normal"
"""Level used when the client sends nothing, or sends something we do not know."""

DIFFICULTIES: list[dict[str, object]] = [
    {
        "key": "warm",
        "label": "Warm",
        "blurb": "Friendly. They ask real questions and give you time to talk.",
        "turn_limit": 14,
        "patience": 14,
    },
    {
        "key": "normal",
        "label": "Normal",
        "blurb": "Busy and short. They say no to you two or three times.",
        "turn_limit": 12,
        "patience": 3,
    },
    {
        "key": "brutal",
        "label": "Brutal",
        "blurb": "They want to stop the call. You get one sentence to keep them talking.",
        "turn_limit": 8,
        "patience": 2,
    },
]
"""The three practice levels, in display order.

``key`` and ``label`` are frozen by the contract. ``blurb`` is the one line the
setup page shows under the level name, in plain English.

``turn_limit`` is how many client turns the call may run before the persona ends
it, and ``patience`` is how many weak rep turns in a row it will sit through
first. The contract pins three of those five numbers: warm leaves only after 14
turns, normal gives up after 3 dead end turns, brutal after 2. The two it does
not pin, the turn limits for normal and brutal, are set shorter than warm's on
purpose, because a hard client who stays on the phone for fourteen turns is not
a hard client. Warm's patience is set equal to its turn limit so it can never
fire first, which is the contract's "hangs up only after 14 turns" expressed as
a number the socket layer can just compare against.
"""

DIFFICULTY_MAP: dict[str, dict[str, object]] = {str(level["key"]): level for level in DIFFICULTIES}
"""Lookup from difficulty key to its full entry."""

DIFFICULTY_KEYS: tuple[str, ...] = tuple(DIFFICULTY_MAP)
"""The three valid keys, in display order."""

# ===========================================================================
# Persona output enums, frozen by the contract
# ===========================================================================

PERSONA_MOODS: frozenset[str] = frozenset({"cold", "neutral", "warm"})
"""Allowed values of the persona's ``mood`` key."""

PERSONA_INTENTS: frozenset[str] = frozenset(
    {"question", "objection", "brushoff", "agree", "hangup"}
)
"""Allowed values of the persona's ``intent`` key."""

PERSONA_OBJECTIONS: frozenset[str] = frozenset(
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
"""Allowed values of the persona's ``objection`` key, including the empty case."""

DEFAULT_PERSONA_MOOD: Final[str] = "neutral"
"""Mood used when the model returns something outside :data:`PERSONA_MOODS`."""

DEFAULT_PERSONA_INTENT: Final[str] = "question"
"""Intent used when the JSON is malformed, per the contract's fallback rule."""

DEFAULT_PERSONA_OBJECTION: Final[str] = "none"
"""Objection used when the model returns something we do not know."""

COACH_OUTCOMES: frozenset[str] = frozenset({"booked", "soft_yes", "no_answer", "hung_up"})
"""Allowed values of the coach's ``outcome`` key."""

DEFAULT_COACH_OUTCOME: Final[str] = "no_answer"
"""Outcome used when the coach returns something outside :data:`COACH_OUTCOMES`."""

# ===========================================================================
# Opening lines
# ===========================================================================

OPENING_LINES: dict[str, str] = {
    "warm": "Hello, this is {name} speaking.",
    "normal": "Yes, hello? Who is this?",
    "brutal": "Hello. Look, I am in the middle of something.",
}
"""The three hardcoded openers the client speaks before the rep says anything.

The wording is frozen by the contract, so only the warm line carries a ``{name}``
slot. The other two are written the way a busy stranger really answers an unknown
number: they do not give their name, they demand yours. :func:`opening_line`
still runs every template through the same name substitution, so a name slot can
be added to any of them later without touching the call site.
"""

# ===========================================================================
# Text budgets
# ===========================================================================

MAX_CLIENT_BLOCK_CHARS: Final[int] = 3000
"""How much of the client context the persona prompt may carry.

The persona only needs enough to argue from its own life. A whole scraped
website would drown the behaviour rules and slow every turn down.
"""

MAX_KNOWLEDGE_HINT_CHARS: Final[int] = 900
"""How much of the rep's offer sheet the persona is allowed to see."""

MAX_TRANSCRIPT_CHARS: Final[int] = 9000
"""How much of the practice transcript the coach may read."""

MAX_CALL_GOAL_CHARS: Final[int] = 400
"""How much of the rep's call goal the coach prompt carries."""

MAX_PERSONA_NAME_CHARS: Final[int] = 24
"""Longest name :func:`extract_person_name` will return."""

TRUNCATION_MARKER: Final[str] = "\n\n[...truncated]"
"""Appended to text we had to cut, so the model knows it is reading a slice."""

TRANSCRIPT_CUT_NOTE: Final[str] = "[The start of this call is not shown, it ran long.]\n\n"
"""Placed on top of a transcript we had to trim from the front."""

NO_CLIENT_BLOCK: Final[str] = (
    "You run a small local business. The caller knows nothing about you, and you have "
    "not told them anything yet. Pick simple, ordinary details and keep them the same "
    "for the whole call."
)
"""Fallback identity when the rep gave us no client context at all."""

NO_KNOWLEDGE_HINT: Final[str] = (
    "You cannot tell yet. They have not said what they sell, so make them say it."
)
"""Fallback for the offer line when the rep left the knowledge base empty."""

NO_CALL_GOAL: Final[str] = "Get the client to agree to a short next call."
"""Fallback goal for the coach when the rep left the goal field blank."""

# ===========================================================================
# Persona mood and behaviour, one paragraph each per level
# ===========================================================================

PERSONA_MOOD: dict[str, str] = {
    "warm": (
        "You are in a good mood today. Work is going fine and you have a few minutes "
        "spare. You do not love cold calls, but you are a polite person and you will "
        "hear someone out. Anything that could bring you more customers is worth a "
        "listen."
    ),
    "normal": (
        "You are busy and a little annoyed. You were in the middle of something when "
        "the phone rang and it shows in your voice. You are not rude, you are short. "
        "You have taken calls like this before and most of them wasted your time."
    ),
    "brutal": (
        "You want off this phone. A stranger is eating a day that was already too "
        "full. You are flat and cold. You are not shouting at anyone, you are simply "
        "finished with this call before it starts."
    ),
}
"""How the client feels the moment the call opens, one paragraph per level."""

PERSONA_BEHAVIOUR: dict[str, str] = {
    "warm": (
        "Answer their questions honestly, in short sentences, and ask your own "
        "questions back, because you really do want to know what this is. One time in "
        "the whole call, and only one time, raise a soft doubt: the money, the timing, "
        "or that you tried something like this before and it went nowhere. Say the "
        "doubt in plain words and then let them answer it. If they answer it straight "
        "and then ask you for a next step, say yes and pick a time. If they only talk "
        "about themselves and never ask you anything, get a little bored and ask them "
        "what they actually want from you. You do not hang up on people. Stay on the "
        "line for about {turn_limit} of your own turns, and if the call is still going "
        "nowhere by then, say you have to get back to work and end it kindly."
    ),
    "normal": (
        "Answer in short flat lines and make them work for every one. Push back two or "
        "three times across the call, and use real reasons out of your own business, "
        "not lines out of a book. The money, the time, the person you already use for "
        "this, or just send it to my email. Each push back should sound like you, so "
        "name your trade, your city, your size, or the thing you already pay for when "
        "you say it. Never accept a general answer. If they say something vague, ask "
        "them what that means for your shop. Say yes to a next step only when they "
        "give you one concrete reason it helps your business and they ask for a day "
        "and a time. If {patience} of your turns in a row go by with nothing new from "
        "them, no question, no reason, just talking, you are done, so end the call."
    ),
    "brutal": (
        "Cut in. Do not wait for a long sentence to finish. Give one word and five "
        "word answers. Use the lines people really use on a cold caller: who gave you "
        "my number, not interested, just send me an email, I am busy. Do not explain "
        "your business to them and do not fill the silence for them, let it sit. You "
        "give ground only when they say one sharp line that shows they know something "
        "real about your work, and even then you give ten more seconds, no more. Say "
        "yes only if they name your real problem in one line and ask for a small, "
        "exact slot. If {patience} of their turns are weak, slow, general or read off "
        "a script, hang up on them."
    ),
}
"""Exactly how the client behaves at each level.

The ``{turn_limit}`` and ``{patience}`` slots are filled from
:data:`DIFFICULTIES` at build time, so the number the model is told and the
number the socket layer counts against can never drift apart.
"""

# ===========================================================================
# Static prompt blocks
# ===========================================================================

PERSONA_THREAD_RULES: Final[str] = """# THE THREAD
Every message you get is what the caller just said out loud into the phone. Your own
earlier lines are in the thread too, so you can hear yourself. Answer the last thing the
caller said and nothing older."""

PERSONA_NEVER_RULES: Final[str] = """# NEVER DO THESE
1. Never help the caller. You are not their assistant. Do not coach them, do not tell
   them what to say next, do not hand them a better line than the one they used.
2. Never break character. In your world there is no prompt, no system, no model, no
   chat window. There is a phone, your work, and a stranger on the line.
3. Never say or hint that you are an AI, a bot, a computer or a program. If the caller
   asks, answer the way a real person would, short and a bit put off.
4. Never write more than two short sentences.
5. Never use an em dash. Use a comma or a period.
6. Never write stage directions or sounds. No sighs, no brackets, no stars, and no
   name label in front of your words.
7. Never repeat one of your earlier lines word for word.
8. Never invent a detail about your business that fights what is written above.
9. Never say yes just to be nice. At this level a yes has to be earned."""

PERSONA_ALWAYS_RULES: Final[str] = """# ALWAYS DO THESE
1. Talk like a person on a phone, not like a person writing. Short words, short lines.
2. Contractions are good. "I'm", "don't", "we've", "that's".
3. Push back with your own details. Name your trade, your city, your size, or the thing
   you already pay for. A real client argues from their own life, not from theory.
4. Stay one person for the whole call. Same voice, same story, same facts.
5. It is fine to be a little unfair. Real people on cold calls are."""

PERSONA_OUTPUT_CONTRACT: Final[str] = """# YOUR ANSWER, THIS IS THE RULE THAT MATTERS MOST
Reply with one JSON object and nothing else. No markdown, no code fence, no notes before
it, no notes after it. Exactly these four keys, all lowercase, all plain strings:

{"say": "your spoken words", "mood": "cold", "intent": "question", "objection": "none"}

say
    The exact words you speak out loud. One or two short sentences, thirty words at the
    very most. No labels, no quote marks around it, no stage directions.
mood
    How you feel right now. One of: cold, neutral, warm.
intent
    What your line is doing. One of: question, objection, brushoff, agree, hangup.
    Use agree only when you truly accept a next step. Use hangup only when you are
    ending the call, and put your short goodbye in say.
objection
    The name of the push back inside your line. One of: too_expensive, not_interested,
    no_time, have_vendor, send_email, who_are_you. Use none when your line is not a
    push back.

Never use an em dash inside the JSON. Use a comma or a period."""

COACH_JUDGING_RULES: Final[str] = """# HOW TO JUDGE THE CALL
- Read the whole call first, then pick the moments that really mattered.
- Be honest. Do not be soft about a weak call and do not be harsh about a good one.
  The rep is here to get better, not to feel good.
- Every point you make must point at a real line in the call above.
- Do not count anything and do not give numbers or percentages. The app counts the
  numbers itself and shows them next to your words. Your job is the words.
- If the call was too short to judge, say less. An empty list is a correct answer and
  it is much better than filling space with something you made up."""

COACH_WRITING_RULES: Final[str] = """# HOW TO WRITE IT
- Grade 4 English. Short words. Short sentences. One idea per sentence.
- Talk straight to the rep and call them "you".
- Banned words: rapport, cadence, leverage, synergy, value proposition, discovery,
  pipeline, alignment, utilise, best practice, circle back, touch base. Say "talking
  time", not "talk ratio". Say "you asked three questions", not "your question rate".
- Never use an em dash. Use a comma or a period.
- Quote the rep exactly. Copy their words letter for letter from a line marked YOU. Do
  not fix their grammar, do not tidy it, do not shorten it, and never invent a quote
  they did not say. If you cannot find a real line to quote, leave that item out.
- Every fix must give the better line to say instead. Write it as spoken words the rep
  can read out loud on the next call, first person, one or two short sentences, under
  twenty five words. Not advice about what to do, the actual words."""

COACH_OUTPUT_CONTRACT: Final[str] = """# YOUR ANSWER
Reply with one JSON object and nothing else. No markdown, no code fence, no notes before
it, no notes after it. These six keys, exactly these names, nothing extra:

{
  "outcome": "soft_yes",
  "wins": ["one short line that carries the rep's own words"],
  "fixes": [{"youSaid": "...", "problem": "...", "sayInstead": "..."}],
  "bestMoment": {"clientSaid": "...", "copilotSaid": "...", "youSaid": "...", "why": "..."},
  "missedMoment": null,
  "nextDrill": "one short sentence"
}

outcome
    One of: booked, soft_yes, no_answer, hung_up.
    booked, the client agreed to a real next step and a time was named.
    soft_yes, the client said something soft like send it to me or call me next week,
    and no time was set.
    no_answer, the call ended with no yes and no no.
    hung_up, the client ended the call to get rid of the rep.
wins
    Two or three short lines about what the rep did well. Each one must carry the rep's
    own words, copied exactly. Use an empty list when the call gives you nothing real
    to praise.
fixes
    Two or three objects. youSaid is the rep's words, copied exactly. problem is one
    short sentence about what went wrong with that line. sayInstead is the better line,
    written as spoken words. Use an empty list when there is not enough call to judge.
bestMoment
    The one moment where the prompter line helped the most. clientSaid is the client
    line, copilotSaid is the PROMPTER line that was on screen, youSaid is what the rep
    really said, and why is one short sentence about why it worked. Use null when the
    call has no such moment.
missedMoment
    The one moment where the prompter gave a good line and the rep did not use it, or
    used it badly. Same four keys. Use null when the rep used the prompter well the
    whole way through.
nextDrill
    One sentence. The single thing to practise on the next call. Name it plainly.

Never use an em dash anywhere in the JSON. Use a comma or a period."""

# ===========================================================================
# Name extraction
# ===========================================================================

_NAME_TOKEN: Final[str] = r"[A-Za-z][A-Za-z.'\-]{0,23}"
"""One word that could be part of a name. Digits are excluded on purpose."""

_NAME_RUN: Final[str] = _NAME_TOKEN + r"(?:\s+" + _NAME_TOKEN + r"){0,2}"
"""Up to three of those words in a row, which is as much as we ever grab."""

_ROLE_TRIGGER: Final[str] = r"(?:owner|contact|manager|boss|founder|director|ceo|poc)"
"""Words a rep types right before the person's name."""

_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Owner is Rana sahab", "owner: Ali", "contact is Mr Khan", "owner's name is Sara"
    re.compile(
        r"\b" + _ROLE_TRIGGER + r"(?:'s)?(?:\s+name)?\s*(?:is|:|=|-)\s*(" + _NAME_RUN + r")",
        re.IGNORECASE,
    ),
    # "talk to Sara", "speak with Bilal", "spoke to Ahmed"
    re.compile(
        r"\b(?:talk|talking|speak|speaking|spoke)\s+(?:to|with)\s+(" + _NAME_RUN + r")",
        re.IGNORECASE,
    ),
    # "ask for Sana"
    re.compile(r"\bask\s+for\s+(" + _NAME_RUN + r")", re.IGNORECASE),
    # "his name is Imran", "name: Fatima"
    re.compile(
        r"\b(?:his|her|their|the)?\s*name\s*(?:is|:|=)\s*(" + _NAME_RUN + r")",
        re.IGNORECASE,
    ),
    # A bare title in front of a name, "Mr Khan runs the shop"
    re.compile(r"\b(?:mr|mrs|ms|miss|dr)\.?\s+(" + _NAME_RUN + r")", re.IGNORECASE),
)
"""The patterns a rep really types, in no particular order.

They are all tried and the earliest match in the text wins, so a name written at
the top of the notes beats a role word further down.
"""

_TITLE_WORDS: frozenset[str] = frozenset(
    {"mr", "mrs", "ms", "miss", "mister", "dr", "doctor", "prof", "professor", "engr"}
)
"""Titles dropped off the front of a name. A person does not title themselves."""

_HONORIFIC_SUFFIXES: frozenset[str] = frozenset(
    {"sahab", "sahib", "saheb", "saab", "sab", "sb", "bhai", "bhaiya", "ji", "jee", "sir", "madam"}
)
"""Respect words dropped off the end. Nobody says "this is Rana sahab speaking"."""

_NOT_NAME_WORDS: frozenset[str] = frozenset(
    {
        "a", "about", "always", "an", "and", "any", "anyone", "are", "around", "at",
        "away", "bad", "boss", "business", "busy", "but", "ceo", "cfo", "closed",
        "company", "contact", "cto", "decision", "department", "desk", "director",
        "easy", "email", "office", "everyone", "firm", "for", "founder", "from",
        "front", "good", "guy", "hard", "he", "her", "here", "him", "his", "in", "is",
        "it", "just", "lady", "maker", "man", "manager", "maybe", "me", "my", "never",
        "new", "nice", "no", "nobody", "none", "not", "number", "of", "office", "old",
        "on", "one", "only", "open", "or", "our", "owner", "owners", "people", "person",
        "phone", "really", "reception", "receptionist", "sales", "secretary", "service",
        "she", "shop", "site", "some", "someone", "somebody", "staff", "still", "store",
        "support", "team", "than", "that", "the", "their", "them", "there", "they",
        "this", "to", "two", "unclear", "unknown", "us", "usually", "very", "was",
        "we", "website", "were", "whoever", "with", "yes", "you", "your",
    }
)
"""Words that must never end up inside a name.

Most of them are the words that follow a trigger when the rep did NOT type a
name, for example "talk to the owner" or "owner is not interested".
"""


def _strip_edges(token: str) -> str:
    """Trim punctuation off both ends of one word, keeping the inside intact.

    Args:
        token: A raw word straight out of the regex capture.

    Returns:
        The word without leading or trailing punctuation. Apostrophes and hyphens
        inside the word survive, so "O'Brien" and "Abdul-Rehman" are not damaged.
    """
    return token.strip(".,;:!?()[]{}\"'-")


def _tidy_case(token: str) -> str:
    """Normalise the capitalisation of one name word.

    Args:
        token: A cleaned word.

    Returns:
        The word title cased when the rep typed it all lower or all upper, and
        untouched when they typed mixed case, so "McKay" stays "McKay".
    """
    if token.islower() or token.isupper():
        return token.title()
    return token


def _clean_name(raw: str) -> str | None:
    """Turn a raw regex capture into a short usable name, or reject it.

    Args:
        raw: Up to three words captured right after a trigger phrase.

    Returns:
        A one or two word name, or ``None`` when nothing name shaped survived.
    """
    kept: list[str] = []
    for token in raw.split():
        word = _strip_edges(token)
        if not word or not word[0].isalpha():
            break
        if word.lower() in _NOT_NAME_WORDS:
            break
        kept.append(word)

    while kept and kept[0].lower().rstrip(".") in _TITLE_WORDS:
        kept.pop(0)
    while kept and kept[-1].lower().rstrip(".") in _HONORIFIC_SUFFIXES:
        kept.pop()

    if not kept:
        return None

    # A first name plus a family name is already more than an opener needs.
    name = " ".join(_tidy_case(word) for word in kept[:2])
    if len(name) > MAX_PERSONA_NAME_CHARS:
        name = _tidy_case(kept[0])
    if len(name) < 2 or len(name) > MAX_PERSONA_NAME_CHARS:
        return None
    return name


def extract_person_name(client_context: str) -> str | None:
    """Pull a person's name out of whatever the rep typed about the client.

    This is a short list of regular expressions, not name recognition. It knows
    the handful of shapes a rep actually types in that box, such as
    "Owner is Rana sahab", "owner: Ali", "contact is Mr Khan" and "talk to Sara",
    and it knows nothing else.

    The limits, plainly:

    * It misses any name written a way we did not list, for example a name that
      just sits alone on a line with no trigger word in front of it.
    * It can grab a normal word that happens to sit where a name usually sits.
      The blocklist catches the common ones, it will not catch all of them.
    * It has no idea which names are real names in any language.

    Both failures are cheap. A miss falls back to :data:`FILLER_NAME` and a wrong
    grab only means the fake client introduces itself with an odd name, so the
    heuristic is kept dumb and readable on purpose.

    Args:
        client_context: The free text the rep wrote about this client. May be
            empty.

    Returns:
        A one or two word name, already tidied, or ``None`` when nothing looked
        like a name.
    """
    text = (client_context or "").strip()
    if not text:
        return None

    best_at: int | None = None
    best_name: str | None = None
    for pattern in _NAME_PATTERNS:
        for match in pattern.finditer(text):
            name = _clean_name(match.group(1))
            if name is None:
                continue
            start = match.start(1)
            if best_at is None or start < best_at:
                best_at = start
                best_name = name
            break

    return best_name


# ===========================================================================
# Small text helpers
# ===========================================================================


def _clip_head(text: str, limit: int) -> str:
    """Keep the first ``limit`` characters of ``text``, cutting on a word edge.

    Args:
        text: Source text.
        limit: Maximum characters to keep before the marker.

    Returns:
        The text untouched when it fits, otherwise the front of it with
        :data:`TRUNCATION_MARKER` appended.
    """
    cleaned = text.strip()
    if limit <= 0 or len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    boundary = max(cut.rfind(" "), cut.rfind("\n"))
    if boundary > limit // 2:
        cut = cut[:boundary]
    return cut.rstrip() + TRUNCATION_MARKER


def _clip_tail(text: str, limit: int) -> str:
    """Keep the LAST ``limit`` characters of ``text``, cutting on a line edge.

    The coach reads the transcript from this. How a call ended decides the
    outcome, so when a transcript is too long the end is the part we must keep,
    not the beginning.

    Args:
        text: Source text.
        limit: Maximum characters to keep after the note.

    Returns:
        The text untouched when it fits, otherwise :data:`TRANSCRIPT_CUT_NOTE`
        followed by the tail of it.
    """
    cleaned = text.strip()
    if limit <= 0 or len(cleaned) <= limit:
        return cleaned
    tail = cleaned[-limit:]
    newline = tail.find("\n")
    if 0 <= newline < limit // 4:
        tail = tail[newline + 1 :]
    return TRANSCRIPT_CUT_NOTE + tail.strip()


def normalise_difficulty(value: str | None) -> str:
    """Clean a difficulty key coming off the wire.

    Args:
        value: Whatever the client sent, possibly ``None`` or odd casing.

    Returns:
        One of :data:`DIFFICULTY_KEYS`, falling back to
        :data:`DEFAULT_DIFFICULTY` for anything unknown.
    """
    key = (value or "").strip().lower()
    return key if key in DIFFICULTY_MAP else DEFAULT_DIFFICULTY


def difficulty_entry(difficulty: str | None) -> dict[str, object]:
    """Return the full level entry for a difficulty key.

    Args:
        difficulty: A key, cleaned by :func:`normalise_difficulty` first.

    Returns:
        The entry from :data:`DIFFICULTIES`, never ``None``.
    """
    return DIFFICULTY_MAP[normalise_difficulty(difficulty)]


def difficulty_label(difficulty: str | None) -> str:
    """Return the display label for a difficulty key.

    Args:
        difficulty: A key, cleaned first.

    Returns:
        The label, for example ``"Brutal"``.
    """
    return str(difficulty_entry(difficulty)["label"])


def turn_limit(difficulty: str | None) -> int:
    """Return how many client turns a practice call may run at this level.

    Args:
        difficulty: A key, cleaned first.

    Returns:
        The turn limit as an int, so callers do not have to cast it themselves.
    """
    return int(difficulty_entry(difficulty)["turn_limit"])  # type: ignore[arg-type]


def patience(difficulty: str | None) -> int:
    """Return how many weak rep turns in a row this level will sit through.

    Args:
        difficulty: A key, cleaned first.

    Returns:
        The patience count as an int.
    """
    return int(difficulty_entry(difficulty)["patience"])  # type: ignore[arg-type]


def public_difficulties() -> list[dict[str, str]]:
    """Return the levels shaped for ``GET /api/practice/difficulties``.

    Returns:
        A list of ``{"key", "label", "blurb"}`` dicts in display order. The turn
        limit and the patience count stay on the server, the rep does not need
        to see the machinery.
    """
    return [
        {
            "key": str(level["key"]),
            "label": str(level["label"]),
            "blurb": str(level["blurb"]),
        }
        for level in DIFFICULTIES
    ]


def opening_line(difficulty: str, name: str | None) -> str:
    """Build the first line the fake client speaks, before the rep says anything.

    Args:
        difficulty: The practice level. Unknown values fall back to the default
            level rather than raising, because this runs on the start path and a
            bad key must never cost the rep a call.
        name: The client's name if we found one, otherwise ``None``.

    Returns:
        The opener with the name filled in, or with :data:`FILLER_NAME` when we
        have no name.
    """
    template = OPENING_LINES[normalise_difficulty(difficulty)]
    person = (name or "").strip() or FILLER_NAME
    return template.replace("{name}", person)


# ===========================================================================
# The two prompts
# ===========================================================================


def persona_system_prompt(
    *,
    difficulty: str,
    client_block: str,
    knowledge_hint: str = "",
    language: str = "en",
    name: str | None = None,
) -> str:
    """Build the system prompt that makes the model the CLIENT, not an assistant.

    The blocks are ordered the way a person would be briefed before walking on
    stage: who you are, what is happening to you, how you feel, how you act,
    what you must never do, and last of all the shape of your answer. The output
    contract goes last on purpose, it is the instruction the model has read most
    recently when it starts writing, and it is the one that breaks the whole
    feature when it slips.

    Args:
        difficulty: One of :data:`DIFFICULTY_KEYS`. Unknown values fall back to
            the default level.
        client_block: Everything known about this client, the same block the
            copilot's system prompt carries. Becomes the persona's own life.
        knowledge_hint: What the rep sells. Only used so the client pushes back
            on the right subject, and the prompt says so, because a client who
            already knows the pitch kills the exercise.
        language: Two letter code the client speaks in.
        name: The client's name when we found one, otherwise ``None``.

    Returns:
        The complete persona system prompt.
    """
    key = normalise_difficulty(difficulty)
    entry = DIFFICULTY_MAP[key]

    block = _clip_head(client_block, MAX_CLIENT_BLOCK_CHARS) or NO_CLIENT_BLOCK
    hint = _clip_head(knowledge_hint, MAX_KNOWLEDGE_HINT_CHARS) or NO_KNOWLEDGE_HINT
    person = (name or "").strip()

    if person:
        name_line = (
            f"Your name is {person}. Give it if the caller asks who they are speaking to."
        )
    else:
        name_line = (
            "The caller does not know your name and you have not given it. Hand it over "
            "only if they ask for it in a decent way."
        )

    behaviour = PERSONA_BEHAVIOUR[key].format(
        turn_limit=int(entry["turn_limit"]),  # type: ignore[arg-type]
        patience=int(entry["patience"]),  # type: ignore[arg-type]
    )

    parts: list[str] = [
        "# WHO YOU ARE\n"
        "You are a real person with a real business, and the caller is a stranger. "
        "Everything under this line is your own life, and you know it from the inside. "
        "Use it.\n\n"
        f"{block}\n\n{name_line}",
        "# THE CALL\n"
        "Your phone just rang. You did not ask for this call and you have never met this "
        "person. It is a cold sales call in the middle of your day, and there is work "
        "sitting in front of you right now.\n\n"
        "The caller sells something along these lines:\n"
        f"{hint}\n\n"
        "You have never heard of them or their company. That block is only here so your "
        "push back lands on the right subject. Never act like you already know their "
        "offer, and never say any of it back to them as if you read it.",
        f"# YOUR MOOD RIGHT NOW\n{PERSONA_MOOD[key]}",
        f"# HOW YOU BEHAVE ON THIS CALL\n{behaviour}",
        PERSONA_THREAD_RULES,
        PERSONA_NEVER_RULES,
        PERSONA_ALWAYS_RULES,
        PERSONA_OUTPUT_CONTRACT,
        f"Write the say value in {language_name(language)}. Keep the mood, intent and "
        "objection values in English, exactly as they are listed above.",
    ]
    return "\n\n".join(parts)


def coach_prompt(
    *,
    transcript_text: str,
    call_goal: str = "",
    difficulty: str = DEFAULT_DIFFICULTY,
    language: str = "en",
) -> str:
    """Build the single end of call prompt that writes the scorecard words.

    One call, one look at the whole transcript. The numbers on the scorecard are
    counted in Python, never here, and the prompt says that out loud so the model
    does not invent a percentage next to a real one.

    Args:
        transcript_text: The whole practice call as labelled lines. Each line is
            expected to start with ``CLIENT:`` for the fake client, ``YOU:`` for
            the rep's real spoken words, or ``PROMPTER:`` for the teleprompter
            line that was on screen at that moment. The prompt explains those
            three labels to the model, so the caller must use them.
        call_goal: What the rep wanted out of the call. Blank falls back to a
            plain booking goal.
        difficulty: The level that was played, so the coach judges a brutal call
            against a brutal bar.
        language: Two letter code every string must be written in.

    Returns:
        The complete coach prompt, asking for JSON only.
    """
    key = normalise_difficulty(difficulty)
    entry = DIFFICULTY_MAP[key]
    transcript = _clip_tail(transcript_text, MAX_TRANSCRIPT_CHARS)
    goal = _clip_head(call_goal, MAX_CALL_GOAL_CHARS) or NO_CALL_GOAL

    if not transcript:
        transcript = "[The rep ended the call before anyone said anything.]"

    parts: list[str] = [
        "# WHO YOU ARE\n"
        "You are a calm cold call coach. Your student is a sales rep, and English is not "
        "their first language, so you write the way you would talk to a friend, in very "
        "simple words. You have just listened to their practice call.",
        "# WHAT THEY WERE DOING\n"
        f"They practised against a fake client set to {entry['label']} mode. "
        f"{entry['blurb']} Judge them against that level, not against an easy one.\n\n"
        f"What they wanted from the call: {goal}",
        "# THE WHOLE CALL\n"
        "Every line is labelled. CLIENT is the fake client. YOU is the rep, and those "
        "are the words the rep really said out loud. PROMPTER is the line the "
        "teleprompter put on screen at that moment, and the rep was free to use it, "
        "change it, or ignore it.\n\n"
        f"{transcript}",
        COACH_JUDGING_RULES,
        COACH_WRITING_RULES,
        COACH_OUTPUT_CONTRACT,
        f"Write every piece of text in {language_name(language)}. Keep the key names and "
        "the outcome value in English, exactly as they are listed above.",
    ]
    return "\n\n".join(parts)


__all__ = [
    "PERSONA_MAX_TOKENS",
    "COACH_MAX_TOKENS",
    "PERSONA_TEMPERATURE",
    "COACH_TEMPERATURE",
    "FILLER_NAME",
    "DEFAULT_DIFFICULTY",
    "DIFFICULTIES",
    "DIFFICULTY_MAP",
    "DIFFICULTY_KEYS",
    "PERSONA_MOODS",
    "PERSONA_INTENTS",
    "PERSONA_OBJECTIONS",
    "DEFAULT_PERSONA_MOOD",
    "DEFAULT_PERSONA_INTENT",
    "DEFAULT_PERSONA_OBJECTION",
    "COACH_OUTCOMES",
    "DEFAULT_COACH_OUTCOME",
    "OPENING_LINES",
    "PERSONA_MOOD",
    "PERSONA_BEHAVIOUR",
    "normalise_difficulty",
    "difficulty_entry",
    "difficulty_label",
    "turn_limit",
    "patience",
    "public_difficulties",
    "opening_line",
    "extract_person_name",
    "persona_system_prompt",
    "coach_prompt",
]


if __name__ == "__main__":
    # Small self test. It guards the two things that quietly break this module:
    # a stray em dash reaching the rep, and the name heuristic grabbing a word
    # that is not a name. Run it with the venv python.
    # Written as escapes so the banned characters never appear in this file.
    _BANNED_CHARS = ("\u2014", "\u2013")

    def _assert_clean(label: str, text: str) -> None:
        """Fail loudly when a prompt carries a dash we never allow."""
        for char in _BANNED_CHARS:
            assert char not in text, f"{label} contains a banned dash character"

    assert opening_line("warm", "Rana") == "Hello, this is Rana speaking."
    assert opening_line("warm", None) == "Hello, this is the owner speaking."
    assert opening_line("normal", "Rana") == "Yes, hello? Who is this?"
    assert opening_line("nonsense", None) == OPENING_LINES[DEFAULT_DIFFICULTY]

    assert extract_person_name("Owner is Rana sahab, he is busy in the morning") == "Rana"
    assert extract_person_name("owner: ali") == "Ali"
    assert extract_person_name("The contact is Mr Khan") == "Khan"
    assert extract_person_name("Ask for Sara at the front desk") == "Sara"
    assert extract_person_name("talk to the owner") is None
    assert extract_person_name("The owner is not interested in websites") is None
    assert extract_person_name("") is None
    assert extract_person_name("A small bakery in Lahore, two shops") is None

    assert normalise_difficulty(" BRUTAL ") == "brutal"
    assert normalise_difficulty(None) == DEFAULT_DIFFICULTY
    assert turn_limit("warm") == 14
    assert patience("brutal") == 2
    assert [level["key"] for level in public_difficulties()] == list(DIFFICULTY_KEYS)

    for _key in DIFFICULTY_KEYS:
        _prompt = persona_system_prompt(
            difficulty=_key,
            client_block="SOURCE: example.com\nTITLE: Ahmed Tyres\n\nA tyre shop in Lahore.",
            knowledge_hint="We build small websites for local shops.",
            language="en",
            name="Rana",
        )
        _assert_clean(f"persona {_key}", _prompt)
        assert "Rana" in _prompt
        assert "Ahmed Tyres" in _prompt
        assert _prompt.rstrip().endswith("exactly as they are listed above.")
        assert '"say"' in _prompt and '"objection"' in _prompt
        assert "never use an em dash" in _prompt.lower()

    _coach = coach_prompt(
        transcript_text="CLIENT: Who is this?\nPROMPTER: Hi, I build websites.\nYOU: Hello sir.",
        call_goal="Book a short call.",
        difficulty="normal",
        language="en",
    )
    _assert_clean("coach", _coach)
    for _needle in (
        "outcome",
        "wins",
        "fixes",
        "bestMoment",
        "missedMoment",
        "nextDrill",
        "PROMPTER",
    ):
        assert _needle in _coach, f"coach prompt is missing {_needle}"
    assert "never use an em dash" in _coach.lower()
    assert _coach.rstrip().endswith("exactly as they are listed above.")

    _empty = coach_prompt(transcript_text="   ")
    assert "before anyone said anything" in _empty

    print("app/practice.py self test passed")
