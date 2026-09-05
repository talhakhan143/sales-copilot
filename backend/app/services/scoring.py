"""Scorecard arithmetic for a practice call.

This module is pure. No network, no FastAPI, no clock, no randomness. Give it a
list of turns and it gives back numbers. That makes it the one part of the
debrief that is fully testable, and it runs its own self test at the bottom.

WHY THIS EXISTS
---------------
The debrief has two halves. The LLM coach decides the soft things: the outcome,
what went well, what to fix, which moment was the best one. This file decides
every hard number, because a language model cannot count and must never be
asked to. If the model is asked "how many questions did the rep ask" it will
guess a plausible number, and a scorecard that quietly invents its own facts is
worse than no scorecard at all. So the rule is simple: anything that can be
counted is counted here, in Python, from the stored turns.

The score itself is also computed here, from the fixed rubric in the practice
contract section 4.2, for the same reason. A score that changes when the model
is in a different mood teaches the rep nothing. This one is stable: the same
transcript always scores the same, and every point can be traced back to the row
that gave it.

THE RUBRIC (contract 4.2, 100 points)
-------------------------------------
    30  call result      booked 30, soft yes 20, no answer 5, hung up 0
    20  objections       round(20 * handled / max(1, faced))
    15  talking time     15 when 35 to 60 percent, 8 when 25 to 70, else 0
    10  questions        10 when 3 or more, 5 when 1 or more, else 0
    10  reply speed      10 when 2500 ms or less, 5 when 4500 or less, else 0
    10  prompter use     round(10 * used_pct / 100)
     5  filler words     5 when 2 or fewer, 2 when 5 or fewer, else 0

Grades: 80 and up "Good", 60 to 79 "Getting there", 40 to 59 "Needs work",
under 40 "Rough".

THE FOUR PLACES THIS BENDS THE RUBRIC, AND WHY
----------------------------------------------
All four point the same way: the rep never earns points for something they did
not do. Read them before "fixing" what looks like a bug.

1. A call with zero turns scores 0 on every row. Played straight, the rubric
   would hand a rep who said nothing at all 5 points for the call result and 5
   points for having no filler words, so quitting instantly would beat a real
   attempt at a hard client. An empty call is not a 10 out of 100, it is a 0.
2. Reply speed needs at least one measured reply. ``avg_reply_ms`` is 0 when the
   rep never answered, and 0 is "under 2500 ms", so silence would score a
   perfect 10 for being fast.
3. Filler words need at least one spoken rep word, for the same reason: saying
   nothing is not the same as speaking cleanly.
4. A client who hangs up was not talked round. Contract 4.1 counts an objection
   as handled when the next client turn came back in a better mood OR was not
   another objection, and a goodbye is not another objection, so played straight
   the rubric would pay the rep for the very objection that ended the call. A
   hangup is the clearest proof there is that the objection was not handled, so
   a client turn whose intent is ``hangup`` never closes an objection as won.

Everything else about the objection rule follows contract 4.1 exactly, including
the OR. A rep whose good answer left the client one mood step cooler, but with
nothing new to push back with, still handled that objection.

ROUNDING
--------
Every rounding step goes through :func:`round_half_up`, not the builtin
``round``. The builtin rounds half to even, so 2.5 becomes 2 while 3.5 becomes
4, and a rep who asks why 1 objection out of 8 gave them 2 points when their
friend got 3 for the same thing has found a real bug in the explanation, not in
the maths. Half up is the rounding people are taught at school, so it is the
rounding a scorecard should use.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "FILLER_PHRASES",
    "FILLER_WORDS",
    "GRADE_BANDS",
    "MOOD_RANK",
    "MAX_SCORE",
    "NO_OBJECTION",
    "OUTCOME_POINTS",
    "SIMILARITY_USED_THRESHOLD",
    "STOPWORDS",
    "Breakdown",
    "Metrics",
    "PracticeTurn",
    "compute_breakdown",
    "compute_copilot_use",
    "compute_metrics",
    "count_fillers",
    "grade_for",
    "is_objection_turn",
    "longest_sentence_words",
    "mood_rank",
    "normalise_words",
    "round_half_up",
    "similarity",
    "talking_time_pct",
]

PracticeRole = Literal["rep", "client"]
"""Who spoke a practice turn. The copilot never gets a turn of its own here."""

SIMILARITY_USED_THRESHOLD: float = 0.35
"""Jaccard score at or above which a rep turn counts as "used the prompter line".

Tuned by hand against real paraphrases, it is not an arbitrary round number.
A rep reading a line aloud almost never reads it word for word: they drop the
greeting, swap "we build" for "we make", add an "um", and stop halfway to let
the client cut in. Measured on pairs like that, an honest read scores 0.55 to
0.85 and an unrelated sentence scores 0.00 to 0.15, with the genuinely partial
reads (rep used the first half of the line, then improvised) landing near 0.40.
0.35 sits under that partial read cluster and well over the noise. Below 0.25
unrelated sentences start counting as used because two English sentences about
the same product share words by accident. Above 0.50 an honest read that was
cut short stops counting, which makes the "how much did the prompter help me"
number lie in the direction that flatters the rep. Do not "clean this up" to
0.5, and do not lower it to make the number look better.
"""

MOOD_RANK: dict[str, int] = {"cold": 0, "neutral": 1, "warm": 2}
"""The client mood ladder. Higher is warmer, so ranks can be compared directly."""

NEUTRAL_RANK: int = 1
"""Rank used for a mood the persona model spelled in some way we do not know."""

NO_OBJECTION: frozenset[str] = frozenset({"", "none", "null", "no_objection"})
"""Values of ``PracticeTurn.objection`` that mean "the client raised nothing"."""

FILLER_WORDS: frozenset[str] = frozenset(
    {
        "um",
        "umm",
        "uh",
        "uhh",
        "erm",
        "er",
        "ah",
        "like",
        "actually",
        "basically",
        "literally",
        "honestly",
    }
)
"""Single word fillers counted across the rep's turns.

This is a blunt count on purpose. "I would like to show you" does contain a
legitimate "like" and will be counted, and that is an acceptable trade for a
rule the rep can check themselves by reading their own transcript. A clever
filler detector that is right 90 percent of the time is worse here than a dumb
one that is right 100 percent of the time about what it actually counts.
"""

FILLER_PHRASES: tuple[tuple[str, ...], ...] = (
    ("you", "know"),
    ("i", "mean"),
    ("sort", "of"),
    ("kind", "of"),
)
"""Multi word fillers. None of their parts sit in ``FILLER_WORDS``, so a phrase
is counted exactly once and never twice.
"""

STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "about", "all", "also", "am", "an", "and", "any", "are", "as",
        "at", "be", "been", "being", "but", "by", "can", "could", "did", "do",
        "does", "doing", "for", "from", "had", "has", "have", "he", "her",
        "here", "him", "his", "how", "i", "if", "in", "into", "is", "it",
        "its", "just", "like", "me", "my", "no", "not", "of", "off", "ok",
        "okay", "on", "or", "our", "out", "over", "really", "she", "should",
        "so", "some", "than", "that", "the", "their", "them", "then", "there",
        "these", "they", "this", "those", "to", "too", "uh", "um", "up", "us",
        "very", "was", "we", "well", "were", "what", "when", "where", "which",
        "who", "why", "will", "with", "would", "yeah", "yes", "you", "your",
    }
)
"""Small English stopword set removed before the Jaccard compare.

Kept small on purpose. These are words two unrelated English sentences share by
accident, so leaving them in would push every pair of sentences up toward the
threshold and make the prompter look more useful than it was.
"""

OUTCOME_POINTS: dict[str, int] = {
    "booked": 30,
    "soft_yes": 20,
    "no_answer": 5,
    "hung_up": 0,
}
"""Points for how the call ended. An outcome we do not know scores 0."""

POINTS_OUTCOME: int = 30
POINTS_OBJECTIONS: int = 20
POINTS_TALKING_TIME: int = 15
POINTS_QUESTIONS: int = 10
POINTS_REPLY_SPEED: int = 10
POINTS_COPILOT: int = 10
POINTS_FILLER: int = 5

MAX_SCORE: int = 100
"""The seven rows add up to exactly this. Asserted by the self test."""

TALK_BEST_LOW: int = 35
TALK_BEST_HIGH: int = 60
TALK_OK_LOW: int = 25
TALK_OK_HIGH: int = 70
TALK_OK_POINTS: int = 8

QUESTIONS_GOOD: int = 3
QUESTIONS_SOME: int = 1
QUESTIONS_SOME_POINTS: int = 5

REPLY_FAST_MS: int = 2500
REPLY_OK_MS: int = 4500
REPLY_OK_POINTS: int = 5

FILLER_CLEAN: int = 2
FILLER_OK: int = 5
FILLER_OK_POINTS: int = 2

GRADE_BANDS: tuple[tuple[int, str], ...] = (
    (80, "Good"),
    (60, "Getting there"),
    (40, "Needs work"),
    (0, "Rough"),
)
"""Lowest score for each grade word, highest band first."""

_WORD_RE = re.compile(r"[a-z0-9]+")
"""Word matcher used after the text has been lowercased and de apostrophed."""

_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n\r]+")
"""Sentence splitter. Spoken transcripts have no abbreviations worth protecting."""


@dataclass
class PracticeTurn:
    """One thing that was said during a practice call.

    This dataclass is the single source of truth for the shape of a practice
    turn. The WebSocket layer builds these as the call runs and hands the list
    to :func:`compute_metrics` when the rep asks to be scored.

    The extra fields all carry safe defaults, so a rep turn is built with just
    the role, the text, the timestamp and the line that was on screen, and a
    client turn adds the three persona fields.

    Attributes:
        role: ``"rep"`` for the person practising, ``"client"`` for the AI
            prospect. Any other value is skipped by every function here.
        text: What was said, already stripped.
        ts: Unix timestamp in seconds when the turn was captured. Used only for
            the reply speed gaps, so it must be taken from the same clock for
            every turn in one call.
        mood: Client turns only. One of ``"cold"``, ``"neutral"`` or ``"warm"``.
            Any other string is treated as neutral, because the persona model
            can and does spell things its own way and a scorecard must never
            crash on that.
        intent: Client turns only. One of ``"question"``, ``"objection"``,
            ``"brushoff"``, ``"agree"`` or ``"hangup"``. Free text is accepted
            for the same defensive reason.
        objection: Client turns only. The objection code the persona raised, or
            ``"none"``. See ``NO_OBJECTION`` for the values that mean nothing
            was raised.
        suggestion_shown: Rep turns only. The copilot line that was on the
            teleprompter at the moment the rep started speaking, or ``None``
            when the screen was empty. This is what the prompter use number is
            measured against, so it must be the line the rep could actually
            see, not the one that arrived afterwards.
    """

    role: PracticeRole
    text: str
    ts: float
    mood: str = "neutral"
    intent: str = "question"
    objection: str = "none"
    suggestion_shown: str | None = None


@dataclass
class Metrics:
    """Every countable fact about one practice call.

    The first nine fields are exactly the ``metrics`` block of the debrief
    payload. ``turns`` and ``reply_samples`` are working numbers that the score
    needs and the wire does not, so :meth:`to_dict` leaves them out. Build the
    pydantic response model from :meth:`to_dict`, not from ``dataclasses.asdict``,
    or those two extras will show up in the JSON.

    Attributes:
        rep_words: Total words the rep said, across every rep turn.
        client_words: Total words the AI client said.
        talking_time_pct: Rep words as a whole percent of all words.
        questions_asked: Rep turns whose text contains a question mark.
        objections_faced: Client turns that carried a real objection code.
        objections_handled: How many of those the rep got past. See
            :func:`compute_metrics` for the exact rule.
        avg_reply_ms: Mean time from the client finishing to the rep starting.
        filler_words: Filler words and phrases across the rep's turns.
        longest_sentence_words: The rep's longest single sentence, in words.
        turns: Rep and client turns counted. Also the ``turns`` field at the top
            level of the debrief payload.
        reply_samples: How many reply gaps went into ``avg_reply_ms``. Zero
            means the rep never answered the client at all.
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
    turns: int = 0
    reply_samples: int = 0

    def to_dict(self) -> dict[str, int]:
        """Return the nine wire fields of the ``metrics`` block, camelCase keyed.

        Returns:
            A JSON safe dict matching the practice contract section 4. The
            working fields ``turns`` and ``reply_samples`` are deliberately not
            included.
        """
        return {
            "repWords": self.rep_words,
            "clientWords": self.client_words,
            "talkingTimePct": self.talking_time_pct,
            "questionsAsked": self.questions_asked,
            "objectionsFaced": self.objections_faced,
            "objectionsHandled": self.objections_handled,
            "avgReplyMs": self.avg_reply_ms,
            "fillerWords": self.filler_words,
            "longestSentenceWords": self.longest_sentence_words,
        }


@dataclass
class Breakdown:
    """One row of the seven line score breakdown.

    Attributes:
        label: Plain English name of the row, for example ``"Talking time"``.
        got: Points won on this row.
        out_of: Points that were on offer.
        note: One short plain English sentence that states the real number the
            points came from, so the rep can see why and not just what.
    """

    label: str
    got: int
    out_of: int
    note: str

    def to_dict(self) -> dict[str, object]:
        """Return the row as the frontend expects it.

        Returns:
            A dict with ``label``, ``got``, ``outOf`` and ``note``.
        """
        return {"label": self.label, "got": self.got, "outOf": self.out_of, "note": self.note}


def round_half_up(value: float) -> int:
    """Round a non negative number half up, the way it is taught at school.

    Args:
        value: The number to round. Negative input returns 0, since nothing in
            this module can legitimately produce a negative score or count.

    Returns:
        The rounded whole number. 2.5 becomes 3, unlike the builtin ``round``
        which would give 2.
    """
    if value <= 0.0:
        return 0
    return int(math.floor(value + 0.5))


def normalise_words(text: str) -> list[str]:
    """Split text into lowercase words with the punctuation taken off.

    Apostrophes are deleted rather than treated as separators, so ``"don't"``
    becomes ``dont`` and matches a transcript that spelled it ``dont``. Hyphens
    do separate, so ``"twenty-four"`` becomes two words on both sides of any
    comparison. Digits are kept, because ``"9am"`` and ``"500"`` are real words
    in a sales call.

    Stopwords are NOT removed here. :func:`similarity` removes them itself, and
    :func:`count_fillers` needs the small words this returns.

    Args:
        text: Any text, may be empty.

    Returns:
        The words in order. An empty or punctuation only string returns ``[]``.
    """
    if not text:
        return []
    lowered = text.lower().replace("’", "'").replace("'", "")
    return _WORD_RE.findall(lowered)


def _content_words(text: str) -> set[str]:
    """Return the set of meaningful words in ``text``, stopwords dropped.

    Args:
        text: Any text.

    Returns:
        A set of words with everything in ``STOPWORDS`` removed. May be empty,
        which is exactly what happens for a line like "yes, ok, sure".
    """
    return {word for word in normalise_words(text) if word not in STOPWORDS}


def similarity(a: str, b: str) -> float:
    """Measure how much two lines overlap, as a Jaccard score over word bags.

    Both sides are lowercased, stripped of punctuation and stripped of
    stopwords, then turned into sets. The score is the size of the overlap over
    the size of the union, so word order and repetition do not matter. That is
    the right shape for this job: a rep who reads the prompter line aloud but
    reorders it, or drops the last third of it, still gets credit.

    Args:
        a: One line, usually what the rep actually said.
        b: The other line, usually the copilot line that was on screen.

    Returns:
        A float from 0.0 through 1.0. Two identical lines score 1.0. Two lines
        with no meaningful word in common score 0.0. A line that is made only
        of stopwords, like "yes ok", leaves an empty side and scores 0.0 rather
        than dividing by zero, and that is the honest answer: saying "yes" is
        not reading the prompter.
    """
    set_a = _content_words(a)
    set_b = _content_words(b)
    if not set_a or not set_b:
        return 0.0
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return len(set_a & set_b) / union


def count_fillers(texts: Iterable[str]) -> int:
    """Count filler words and filler phrases across many pieces of text.

    Args:
        texts: The texts to scan, normally every rep turn of the call.

    Returns:
        The total count. Single words come from ``FILLER_WORDS`` and two word
        phrases from ``FILLER_PHRASES``. No word is counted twice, because no
        part of a phrase is also a single word filler.
    """
    total = 0
    for text in texts:
        words = normalise_words(text)
        total += sum(1 for word in words if word in FILLER_WORDS)
        for index in range(len(words) - 1):
            pair = (words[index], words[index + 1])
            if pair in FILLER_PHRASES:
                total += 1
    return total


def talking_time_pct(rep_words: int, client_words: int) -> int:
    """Work out how much of the call the rep filled with their own words.

    Args:
        rep_words: Words the rep said. Negative input is treated as 0.
        client_words: Words the client said. Negative input is treated as 0.

    Returns:
        The rep share as a whole percent from 0 through 100. A call where
        nobody said anything returns 0 instead of dividing by zero.
    """
    rep = max(0, rep_words)
    client = max(0, client_words)
    total = rep + client
    if total <= 0:
        return 0
    return min(100, round_half_up(rep / total * 100.0))


def mood_rank(mood: str) -> int:
    """Place a client mood on the cold, neutral, warm ladder.

    Args:
        mood: The mood string from a client turn.

    Returns:
        0 for cold, 1 for neutral, 2 for warm. Anything we do not recognise,
        including an empty string, ranks as neutral so a strange value from the
        persona model can never make a comparison blow up.
    """
    return MOOD_RANK.get(mood.strip().lower(), NEUTRAL_RANK)


def is_objection_turn(turn: PracticeTurn) -> bool:
    """Say whether a client turn is pushing back on the rep.

    This is deliberately wider than the ``objections_faced`` count. Faced counts
    only turns that carry a real objection code, which is the contract rule. But
    when we ask "did the client come back with another objection", a turn whose
    intent is ``objection`` counts too, even if the persona failed to give it a
    code. Being wide here can only make ``objections_handled`` stricter, and a
    score that is a little hard on the rep is safer than one that hands out
    points the rep did not earn.

    Args:
        turn: The turn to test.

    Returns:
        True when this is a client turn that carries an objection code or an
        objection intent.
    """
    if turn.role != "client":
        return False
    if turn.objection.strip().lower() not in NO_OBJECTION:
        return True
    return turn.intent.strip().lower() == "objection"


def longest_sentence_words(texts: Iterable[str]) -> int:
    """Find the longest single sentence across several texts, in words.

    Long sentences are the thing that makes a non native speaker run out of
    breath on a call, so this is a coaching number, not a style number.

    Args:
        texts: The texts to scan, normally every rep turn.

    Returns:
        The word count of the longest sentence found, or 0 when there is none.
    """
    longest = 0
    for text in texts:
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            longest = max(longest, len(normalise_words(sentence)))
    return longest


def compute_metrics(turns: Sequence[PracticeTurn]) -> Metrics:
    """Count everything countable about a practice call in one walk.

    The turns must arrive in the order they happened. Nothing here sorts them,
    because a wrong order is a bug in the caller and quietly sorting it away
    would hide it.

    The objection rule, spelled out because it is the only clever part, and it
    is contract 4.1 word for word: a client objection counts as handled when the
    rep answered it AND the next thing the client said either came back in a
    better mood OR was not another objection. The one thing laid on top is that
    a client turn whose intent is ``hangup`` never counts, see point 4 of the
    module docstring. Only one objection is open at a time. If the client
    objects again before the first one is resolved, that new objection is itself
    the proof the first one was not handled.

    Args:
        turns: The whole practice transcript, oldest first. Turns whose role is
            neither ``"rep"`` nor ``"client"`` are skipped.

    Returns:
        A fully filled :class:`Metrics`. An empty transcript returns a Metrics
        of zeroes. This function never raises.
    """
    metrics = Metrics()

    rep_texts: list[str] = []
    reply_gaps_ms: list[float] = []
    pending_client_ts: float | None = None

    # The one open objection, as (mood rank when it was raised, rep has replied).
    open_objection: tuple[int, bool] | None = None

    for turn in turns:
        if turn.role == "client":
            metrics.turns += 1
            metrics.client_words += len(normalise_words(turn.text))

            if open_objection is not None and open_objection[1]:
                mood_improved = mood_rank(turn.mood) > open_objection[0]
                hung_up = turn.intent.strip().lower() == "hangup"
                if not hung_up and (mood_improved or not is_objection_turn(turn)):
                    metrics.objections_handled += 1
                open_objection = None

            if turn.objection.strip().lower() not in NO_OBJECTION:
                metrics.objections_faced += 1
                open_objection = (mood_rank(turn.mood), False)

            pending_client_ts = turn.ts

        elif turn.role == "rep":
            metrics.turns += 1
            rep_texts.append(turn.text)
            metrics.rep_words += len(normalise_words(turn.text))
            if "?" in turn.text:
                metrics.questions_asked += 1

            if open_objection is not None and not open_objection[1]:
                open_objection = (open_objection[0], True)

            if pending_client_ts is not None:
                # Only the first rep turn after the client counts, because the
                # question is how long the rep took to START talking.
                reply_gaps_ms.append(max(0.0, turn.ts - pending_client_ts) * 1000.0)
                pending_client_ts = None

    metrics.talking_time_pct = talking_time_pct(metrics.rep_words, metrics.client_words)
    metrics.filler_words = count_fillers(rep_texts)
    metrics.longest_sentence_words = longest_sentence_words(rep_texts)
    metrics.reply_samples = len(reply_gaps_ms)
    if reply_gaps_ms:
        metrics.avg_reply_ms = round_half_up(sum(reply_gaps_ms) / len(reply_gaps_ms))
    return metrics


def compute_copilot_use(turns: Sequence[PracticeTurn]) -> tuple[int, int, int]:
    """Measure how much the rep leaned on the teleprompter.

    A rep turn counts as shown when there was a copilot line on the screen at
    the moment they started speaking, and counts as used when what they said
    overlaps that line at or above ``SIMILARITY_USED_THRESHOLD``.

    Args:
        turns: The whole practice transcript.

    Returns:
        A tuple of (shown, used, used_pct). ``used_pct`` is a whole number from
        0 through 100, and is 0 when nothing was ever shown.
    """
    shown = 0
    used = 0
    for turn in turns:
        if turn.role != "rep":
            continue
        line = (turn.suggestion_shown or "").strip()
        if not line:
            continue
        shown += 1
        if similarity(turn.text, line) >= SIMILARITY_USED_THRESHOLD:
            used += 1
    if shown <= 0:
        return 0, 0, 0
    return shown, used, min(100, round_half_up(used / shown * 100.0))


def grade_for(score: int) -> str:
    """Turn a score into the one word grade shown next to it.

    Args:
        score: The total score, 0 through 100.

    Returns:
        ``"Good"``, ``"Getting there"``, ``"Needs work"`` or ``"Rough"``.
    """
    for floor, word in GRADE_BANDS:
        if score >= floor:
            return word
    return "Rough"


def _plural(count: int, one: str, many: str) -> str:
    """Join a number to the right word form.

    Args:
        count: The number.
        one: The word to use for exactly 1.
        many: The word to use for every other count.

    Returns:
        For example ``"1 question"`` or ``"4 questions"``.
    """
    return f"{count} {one if count == 1 else many}"


def _empty_rows() -> list[Breakdown]:
    """Build the seven zero rows for a call that never really started.

    Returns:
        Seven :class:`Breakdown` rows, all with ``got`` of 0, each with its own
        short note so the panel does not repeat one sentence seven times.
    """
    return [
        Breakdown("Call result", 0, POINTS_OUTCOME, "The call did not really start."),
        Breakdown("Times they said no", 0, POINTS_OBJECTIONS, "The client never said no to you."),
        Breakdown("Talking time", 0, POINTS_TALKING_TIME, "You did not say anything."),
        Breakdown("Questions asked", 0, POINTS_QUESTIONS, "You did not ask anything."),
        Breakdown("Reply speed", 0, POINTS_REPLY_SPEED, "You never replied, so there is no speed to show."),
        Breakdown("Prompter use", 0, POINTS_COPILOT, "We did not show you any lines."),
        Breakdown("Filler words", 0, POINTS_FILLER, "You said nothing, so there are no filler words."),
    ]


def compute_breakdown(
    metrics: Metrics,
    outcome: str,
    used_pct: int,
) -> tuple[int, str, list[Breakdown]]:
    """Score a practice call from the fixed rubric and explain every point.

    See the module docstring for the rubric itself and for the three places this
    refuses to hand out points for something the rep did not do.

    Args:
        metrics: The counted facts from :func:`compute_metrics`.
        outcome: How the call ended, decided by the coach. One of ``"booked"``,
            ``"soft_yes"``, ``"no_answer"`` or ``"hung_up"``. Anything else
            scores 0 on that row instead of raising.
        used_pct: Prompter use as a whole percent, from
            :func:`compute_copilot_use`. Clamped into 0 through 100.

    Returns:
        A tuple of (score, grade, rows). The score is clamped into 0 through
        100 and always equals the sum of the rows. The rows are in rubric
        order and always number seven, even for an empty call.
    """
    if metrics.turns <= 0:
        return 0, grade_for(0), _empty_rows()

    rows: list[Breakdown] = []
    used = max(0, min(100, int(used_pct)))

    # 1. How the call ended, 30 points.
    key = outcome.strip().lower()
    outcome_got = OUTCOME_POINTS.get(key, 0)
    if key == "booked":
        outcome_note = "You got the meeting. That is the best ending."
    elif key == "soft_yes":
        outcome_note = "The client almost said yes. Next time ask for a day and a time."
    elif key == "no_answer":
        outcome_note = "The call ended with no clear answer."
    elif key == "hung_up":
        outcome_note = "The client hung up on you."
    else:
        outcome_note = "We could not tell how the call ended."
    rows.append(Breakdown("Call result", outcome_got, POINTS_OUTCOME, outcome_note))

    # 2. Objections, 20 points. The row is named for what the rep heard, not for
    # what a sales book calls it, so "objection" never reaches their eyes.
    faced = max(0, metrics.objections_faced)
    handled = max(0, min(faced, metrics.objections_handled))
    objection_got = round_half_up(POINTS_OBJECTIONS * handled / max(1, faced))
    if faced == 0:
        objection_note = "The client never said no to you, so there were no points to win here."
    elif handled == 0:
        objection_note = (
            f"The client said no {_plural(faced, 'time', 'times')}. "
            "You did not have a good answer."
        )
    else:
        objection_note = (
            f"The client said no {_plural(faced, 'time', 'times')}. "
            f"You had a good answer {_plural(handled, 'time', 'times')}."
        )
    rows.append(Breakdown("Times they said no", objection_got, POINTS_OBJECTIONS, objection_note))

    # 3. Talking time, 15 points.
    pct = metrics.talking_time_pct
    if TALK_BEST_LOW <= pct <= TALK_BEST_HIGH:
        talk_got = POINTS_TALKING_TIME
        talk_note = f"You talked {pct} percent of the time. That is a good balance."
    elif TALK_OK_LOW <= pct <= TALK_OK_HIGH:
        talk_got = TALK_OK_POINTS
        if pct > TALK_BEST_HIGH:
            talk_note = f"You talked {pct} percent of the time. Try to get closer to half."
        else:
            talk_note = f"You talked {pct} percent of the time. You can say a bit more."
    else:
        talk_got = 0
        if pct > TALK_OK_HIGH:
            talk_note = f"You talked {pct} percent of the time. Let the client talk more."
        else:
            talk_note = f"You talked {pct} percent of the time. Say more than that."
    rows.append(Breakdown("Talking time", talk_got, POINTS_TALKING_TIME, talk_note))

    # 4. Questions, 10 points.
    asked = max(0, metrics.questions_asked)
    if asked >= QUESTIONS_GOOD:
        question_got = POINTS_QUESTIONS
        question_note = f"You asked {_plural(asked, 'question', 'questions')}. Good work."
    elif asked >= QUESTIONS_SOME:
        question_got = QUESTIONS_SOME_POINTS
        question_note = f"You asked {_plural(asked, 'question', 'questions')}. Try to ask three."
    else:
        question_got = 0
        question_note = "You asked no questions. Try to ask three."
    rows.append(Breakdown("Questions asked", question_got, POINTS_QUESTIONS, question_note))

    # 5. Reply speed, 10 points. No measured reply means no points, see the
    # module docstring: 0 ms of silence is not a fast answer.
    if metrics.reply_samples <= 0:
        speed_got = 0
        speed_note = "You never answered the client, so we could not time you."
    else:
        seconds = metrics.avg_reply_ms / 1000.0
        if metrics.avg_reply_ms <= REPLY_FAST_MS:
            speed_got = POINTS_REPLY_SPEED
            speed_note = f"You started to talk {seconds:.1f} seconds after the client stopped. That is fast."
        elif metrics.avg_reply_ms <= REPLY_OK_MS:
            speed_got = REPLY_OK_POINTS
            speed_note = f"You waited {seconds:.1f} seconds before you talked. Try to be a bit faster."
        else:
            speed_got = 0
            speed_note = f"You waited {seconds:.1f} seconds before you talked. That is too slow."
    rows.append(Breakdown("Reply speed", speed_got, POINTS_REPLY_SPEED, speed_note))

    # 6. Prompter use, 10 points.
    copilot_got = round_half_up(POINTS_COPILOT * used / 100.0)
    if used <= 0:
        copilot_note = "You did not use the lines on the screen. Try to read them."
    elif used >= 60:
        copilot_note = f"You used the lines on the screen {used} percent of the time. Good."
    else:
        copilot_note = f"You used the lines on the screen {used} percent of the time. Read them more."
    rows.append(Breakdown("Prompter use", copilot_got, POINTS_COPILOT, copilot_note))

    # 7. Filler words, 5 points. A rep who said nothing gets nothing here.
    fillers = max(0, metrics.filler_words)
    if metrics.rep_words <= 0:
        filler_got = 0
        filler_note = "You did not say anything."
    elif fillers <= FILLER_CLEAN:
        filler_got = POINTS_FILLER
        filler_note = (
            "You said no filler words. Very clean."
            if fillers == 0
            else f"You said {_plural(fillers, 'filler word', 'filler words')}, like um and uh. That is fine."
        )
    elif fillers <= FILLER_OK:
        filler_got = FILLER_OK_POINTS
        filler_note = f"You said {_plural(fillers, 'filler word', 'filler words')}, like um and uh. Try to use fewer."
    else:
        filler_got = 0
        filler_note = f"You said {_plural(fillers, 'filler word', 'filler words')}, like um and uh. That is too many."
    rows.append(Breakdown("Filler words", filler_got, POINTS_FILLER, filler_note))

    score = max(0, min(MAX_SCORE, sum(row.got for row in rows)))
    return score, grade_for(score), rows


if __name__ == "__main__":
    def _client(text: str, ts: float, mood: str, intent: str, objection: str) -> PracticeTurn:
        return PracticeTurn(role="client", text=text, ts=ts, mood=mood, intent=intent, objection=objection)

    def _rep(text: str, ts: float, shown: str) -> PracticeTurn:
        return PracticeTurn(role="rep", text=text, ts=ts, suggestion_shown=shown)

    assert similarity("we fix your website in two weeks", "we fix your website in two weeks") == 1.0
    assert similarity("our website brings phone calls", "purple elephants dance quietly") == 0.0
    paraphrase = similarity(
        "We build websites for plumbers that bring in more calls.",
        "So we make websites for plumbers, and they bring you more calls.",
    )
    assert SIMILARITY_USED_THRESHOLD < paraphrase < 1.0, paraphrase  # 5 of 7 words shared, 0.714

    S1 = "That is fine. Many shops keep their own guy and still miss calls at night. Can I show you one page?"
    fixture = [
        _client("Yes, hello? Who is this?", 0.0, "neutral", "question", "none"),
        _rep("Hi, I am Talha from Bright Web. We build websites for plumbers that bring in more calls. Do you have one minute?", 2.0,
             "Hi, I am Talha from Bright Web. We build websites for plumbers that bring in more calls. Do you have one minute?"),
        _client("We already have a website guy.", 6.0, "cold", "objection", "have_vendor"),
        _rep("That is fine. Most shops keep their guy and still miss calls at night. Can I show you one page?", 8.0, S1),
        _client("Okay. And what exactly would that new page do for my shop?", 12.0, "neutral", "question", "none"),
        _rep("It shows your work, um, and it books a job in two clicks. Basically it answers at night.", 15.0,
             "It shows your work and books a job in two clicks."),
        _client("Look, it is too expensive for us.", 19.0, "cold", "objection", "too_expensive"),
        _rep("I hear you.", 22.0, "It is less than one job a month. Can we talk Tuesday at ten?"),
    ]

    m = compute_metrics(fixture)
    assert (m.turns, m.rep_words, m.client_words) == (8, 63, 30), m
    assert m.talking_time_pct == 68, m.talking_time_pct  # 63 of 93 words is 67.7, rounds to 68
    assert (m.questions_asked, m.objections_faced, m.objections_handled) == (2, 2, 1), m
    assert m.avg_reply_ms == 2500, m.avg_reply_ms  # gaps 2.0, 2.0, 3.0, 3.0 seconds
    assert (m.filler_words, m.longest_sentence_words, m.reply_samples) == (2, 13, 4), m

    shown, used, used_pct = compute_copilot_use(fixture)
    assert (shown, used, used_pct) == (4, 3, 75), (shown, used, used_pct)

    # By hand from the rubric: no_answer 5, objections round(20 * 1/2) = 10,
    # talking time 68 percent sits in 25 to 70 so 8, 2 questions so 5, reply
    # 2500 ms is exactly the fast band so 10, prompter round(10 * 75/100) = 8,
    # 2 filler words so 5. Total 5 + 10 + 8 + 5 + 10 + 8 + 5 = 51, "Needs work".
    score, grade, rows = compute_breakdown(m, "no_answer", used_pct)
    assert (score, grade) == (51, "Needs work"), (score, grade)
    assert [r.got for r in rows] == [5, 10, 8, 5, 10, 8, 5], [r.got for r in rows]
    assert sum(r.out_of for r in rows) == MAX_SCORE
    assert rows[2].note == "You talked 68 percent of the time. Try to get closer to half."
    assert rows[1].label == "Times they said no", rows[1].label
    assert rows[1].note == "The client said no 2 times. You had a good answer 1 time.", rows[1].note
    booked_score, booked_grade, _ = compute_breakdown(m, "booked", used_pct)
    assert (booked_score, booked_grade) == (76, "Getting there"), (booked_score, booked_grade)

    # Contract 4.1 is an OR, not an AND. The rep answered well and the client
    # came back with nothing new to push with, so that objection was handled,
    # even though the persona model dropped the mood one step on the way.
    mood_dip = compute_metrics(
        [
            _client("Honestly that sounds too expensive for us.", 0.0, "warm", "objection", "too_expensive"),
            _rep("It is less than one job a month, and the site is yours to keep.", 2.0, ""),
            _client("Okay, send me the details and I will look.", 5.0, "neutral", "brushoff", "none"),
        ]
    )
    assert (mood_dip.objections_faced, mood_dip.objections_handled) == (1, 1), mood_dip

    # A goodbye is not another objection, but it is not a win either.
    hung_up = compute_metrics(
        [
            _client("We already have someone for that.", 0.0, "cold", "objection", "have_vendor"),
            _rep("Most shops keep their guy and still miss calls at night.", 2.0, ""),
            _client("Not interested. Goodbye.", 5.0, "cold", "hangup", "none"),
        ]
    )
    assert (hung_up.objections_faced, hung_up.objections_handled) == (1, 0), hung_up

    empty = compute_metrics([])
    assert empty == Metrics(), empty
    empty_score, empty_grade, empty_rows = compute_breakdown(empty, "no_answer", 0)
    assert (empty_score, empty_grade, len(empty_rows)) == (0, "Rough", 7)
    assert compute_copilot_use([]) == (0, 0, 0)
    assert talking_time_pct(0, 0) == 0 and similarity("", "") == 0.0

    print(
        f"scoring self test ok: 8 turns, {m.rep_words} rep words, "
        f"{m.talking_time_pct} percent talking time, {m.objections_handled} of "
        f"{m.objections_faced} objections handled, prompter {used}/{shown} ({used_pct}%), "
        f"score {score} \"{grade}\", paraphrase {paraphrase:.3f}"
    )
