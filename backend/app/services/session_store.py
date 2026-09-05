"""In memory session store for the teleprompter backend.

A session holds the fused system prompt plus the rolling conversation transcript
for one cold call. Everything lives in process memory on purpose: the app is a
single uvicorn worker tool, there is no database, and a call is worthless once
the process dies anyway.

The same session type carries a practice call. ``Session.mode`` is ``"live"`` for
a real call and ``"practice"`` for a rehearsal against the AI client, and every
practice only field is defaulted, so nothing on the live path changes.

Threading note, read before "fixing" anything here: every method on
:class:`SessionStore` and :class:`Session` is synchronous, O(1) or O(200), and
never awaits. FastAPI runs all of this inside one asyncio event loop, which is
single threaded, so no coroutine can be preempted in the middle of one of these
methods. That means NO lock is needed anywhere in this module. Please do not add
an ``asyncio.Lock``, it would only buy contention and deadlock risk. If this ever
grows real threads or multiple workers, move to Redis instead of bolting locks on.
"""

from __future__ import annotations

import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    # ``app.services.scoring`` owns the PracticeTurn shape. The import is kept
    # behind TYPE_CHECKING for two reasons. First, scoring is a practice only
    # leaf module and the live call path must not gain an import time dependency
    # on it. Second, scoring is free to type its own helpers against Session
    # later without creating a cycle. ``from __future__ import annotations`` is
    # already on, so the annotations below resolve for a type checker and cost
    # nothing at runtime.
    from app.services.scoring import PracticeTurn

TurnRole = Literal["client", "rep", "copilot"]
"""Who produced a transcript turn: the prospect, the rep, or the copilot."""

SessionMode = Literal["live", "practice"]
"""Whether this session is a real cold call or a rehearsal against the AI client."""

MAX_TURNS: int = 200
"""Hard cap on retained turns per session.

The contract says "caps at 200". We get that cap for free from
``collections.deque(maxlen=MAX_TURNS)``: the deque drops the oldest turn on
append once it is full, so there is no manual trimming anywhere in this module.
"""

MAX_PRACTICE_TURNS: int = 200
"""Hard cap on retained practice turns per session.

Practice turns are a plain list, not a deque, because the scorer walks them by
index and slices them when it looks for the rep turn that followed a client
turn. A deque would make that awkward, so this one cap is enforced by hand in
``Session.record_practice_turn``.
"""

MAX_HINT_CHARS: int = 400
"""Maximum length of the Whisper biasing prompt produced by ``transcript_hint``."""

MAX_HINT_WORDS: int = 12
"""How many distinctive capitalised words the biasing prompt may carry."""

MAX_HINT_REP_CHARS: int = 200
"""How much of the last rep utterance the biasing prompt may carry."""

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9&+._'/-]*")
"""Loose word matcher, keeps things like ``GPT-4``, ``R&D`` and ``Acme.io`` intact."""

_HINT_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "about", "actually", "after", "again", "all", "also", "although",
        "always", "am", "an", "and", "any", "anyway", "are", "as", "ask", "at",
        "back", "basically", "be", "because", "been", "before", "being", "both",
        "but", "by", "call", "can", "cannot", "cool", "could", "did", "do",
        "does", "doing", "done", "down", "each", "even", "ever", "every",
        "exactly", "fine", "first", "for", "from", "get", "give", "going",
        "good", "great", "had", "has", "have", "he", "hello", "help", "her",
        "here", "hey", "hi", "him", "his", "how", "however", "if", "in",
        "into", "is", "it", "its", "just", "keep", "know", "let", "like",
        "look", "made", "make", "many", "maybe", "me", "mean", "might", "mine",
        "more", "most", "much", "must", "my", "need", "never", "new", "next",
        "nice", "no", "not", "now", "of", "off", "ok", "okay", "on", "once",
        "one", "only", "or", "other", "our", "out", "over", "perfect", "please",
        "probably", "put", "really", "right", "said", "same", "say", "see",
        "send", "she", "should", "since", "so", "some", "sorry", "still",
        "such", "sure", "take", "tell", "than", "thank", "thanks", "that",
        "the", "their", "them", "then", "there", "these", "they", "thing",
        "think", "this", "those", "though", "thought", "through", "time", "to",
        "today", "too", "try", "under", "until", "up", "us", "use", "very",
        "want", "was", "way", "we", "well", "were", "what", "when", "where",
        "which", "while", "who", "why", "will", "with", "would", "yeah", "yes",
        "yet", "you", "your", "yours",
    }
)
"""Common English words that are never worth feeding to Whisper as a bias term."""


@dataclass
class Turn:
    """One line of the call transcript.

    Attributes:
        role: Who spoke, ``client`` (prospect), ``rep`` (the user) or ``copilot``.
        text: The plain text of the turn, already stripped.
        ts: Unix timestamp in seconds when the turn was appended.
    """

    role: TurnRole
    text: str
    ts: float


@dataclass
class Session:
    """One live cold call or one practice call, its prompt and its transcript.

    Attributes:
        id: uuid4 string handed to the frontend and used on the WebSocket.
        system_prompt: The fully fused system prompt for this call.
        language: ISO 639-1 style language code, fed to Whisper as ``language``.
        created_at: Unix timestamp of creation.
        last_seen: Unix timestamp of the last access, used by the sweeper.
        turns: Bounded transcript, oldest first, capped by the deque maxlen.
        client_title: Title of the scraped prospect page, if any.
        client_url: Normalized prospect URL, if any.
        mode: ``live`` for a real call, ``practice`` for a rehearsal. The
            WebSocket picks its branch on this field, so a live call keeps its
            old behaviour byte for byte.
        difficulty: Practice level being played, or ``None`` on a live call.
        persona_name: Name the AI client answers to, or ``None`` when the notes
            did not give us one.
        client_block: The raw client facts, scraped page plus whatever the rep
            typed, exactly as they were fused into the system prompt. The
            persona prompt needs these facts on their own, and the fused system
            prompt is written for the copilot, so it cannot be reused here.
        knowledge_hint: A short summary of what the rep sells, so the AI client
            can push back against the real offer instead of a generic one.
        call_goal: The one line goal for this call, kept so the coach can judge
            whether the rep actually got there.
        started_at: Unix timestamp of the moment the practice call really began,
            which is when the opening line was sent. ``0.0`` until then.
        ended_at: Unix timestamp of the moment the practice call ended, ``0.0``
            while it is still running.
        ended_reason: ``hangup``, ``rep_ended``, ``goal_reached`` or
            ``turn_limit``, or ``None`` while the call is still running.
        practice_turns: Every practice turn, oldest first, capped at
            ``MAX_PRACTICE_TURNS``. This is what the scorer counts over.
        last_suggestion: The teleprompter line that is on screen right now, kept
            so the next rep turn can be compared against it.
    """

    id: str
    system_prompt: str
    language: str
    created_at: float
    last_seen: float
    turns: deque[Turn] = field(default_factory=lambda: deque(maxlen=MAX_TURNS))
    client_title: str | None = None
    client_url: str | None = None
    mode: SessionMode = "live"
    difficulty: str | None = None
    persona_name: str | None = None
    client_block: str = ""
    knowledge_hint: str = ""
    call_goal: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    ended_reason: str | None = None
    practice_turns: list[PracticeTurn] = field(default_factory=list)
    last_suggestion: str = ""

    def touch(self) -> None:
        """Mark the session as active so the TTL sweeper leaves it alone."""
        self.last_seen = time.time()

    def append(self, role: TurnRole, text: str) -> None:
        """Append one turn to the transcript.

        Blank text is ignored. The transcript is bounded by the deque maxlen
        (``MAX_TURNS``), so the oldest turn is evicted automatically once the
        cap is reached, no manual trimming needed.

        Args:
            role: Who produced this text.
            text: The transcript text, whitespace is stripped.
        """
        cleaned = text.strip()
        if not cleaned:
            return
        now = time.time()
        self.turns.append(Turn(role=role, text=cleaned, ts=now))
        self.last_seen = now

    def record_practice_turn(self, turn: PracticeTurn) -> None:
        """Store one practice turn for the scorer.

        This is on top of :meth:`append`, not instead of it. The copilot still
        needs the normal transcript to write the next teleprompter line, while
        the scorer needs the richer practice record with its mood, its intent
        and the suggestion that was showing at the time.

        Args:
            turn: A ``scoring.PracticeTurn``. Nothing is validated here, the
                scorer owns that shape.
        """
        self.practice_turns.append(turn)
        if len(self.practice_turns) > MAX_PRACTICE_TURNS:
            # Keep the newest turns, drop from the front, same idea as the
            # deque maxlen on the live transcript.
            del self.practice_turns[:-MAX_PRACTICE_TURNS]
        self.last_seen = time.time()

    def practice_transcript_text(self) -> str:
        """Render the practice turns as a flat transcript for the coach prompt.

        The three labels are fixed by ``practice.coach_prompt``, which tells the
        model that ``CLIENT`` is the fake client, ``YOU`` is what the rep really
        said out loud, and ``PROMPTER`` is the line the teleprompter had on
        screen at that moment. The coach is told to copy the rep word for word
        from a line marked ``YOU`` and to fill ``copilotSaid`` from a
        ``PROMPTER`` line, so those exact labels have to come out of here or the
        coach has nothing real to quote. Do not rename them on this side alone.

        A rep turn that had a teleprompter line on screen gets that line
        emitted just above it, so the coach can see what the rep was offered and
        what the rep did with it. Only the role, the text and the suggestion are
        read, defensively, so a new field on ``scoring.PracticeTurn`` can never
        break the debrief.

        The result is not clamped here. A very long call could produce a lot of
        text, so the caller that builds the coach prompt is the one that must
        cap it before it goes to the model.

        Returns:
            The transcript, one line per record, oldest first. Empty when the
            rep ended the call before anyone said anything.
        """
        lines: list[str] = []
        for turn in self.practice_turns:
            text = _one_line(str(getattr(turn, "text", "") or ""))
            if not text:
                continue
            role = str(getattr(turn, "role", "") or "").strip().lower()
            if role == "client":
                lines.append(f"CLIENT: {text}")
                continue
            shown = _one_line(str(getattr(turn, "suggestion_shown", "") or ""))
            if shown:
                lines.append(f"PROMPTER: {shown}")
            lines.append(f"YOU: {text}")
        return "\n".join(lines)

    def recent_messages(self, window: int) -> list[dict[str, str]]:
        """Return the last ``window`` turns as OpenAI style chat messages.

        The system message is never included here, the caller prepends
        ``{"role": "system", "content": session.system_prompt}`` itself.

        Args:
            window: How many trailing turns to include. Values below 1 return
                an empty list.

        Returns:
            Messages oldest first. A ``client`` turn becomes
            ``{"role": "user", "content": "PROSPECT: ..."}``, a ``rep`` turn
            becomes ``{"role": "user", "content": "REP (me): ..."}`` and a
            ``copilot`` turn becomes ``{"role": "assistant", "content": ...}``.
        """
        if window < 1 or not self.turns:
            return []
        recent = list(self.turns)[-window:]
        messages: list[dict[str, str]] = []
        for turn in recent:
            if turn.role == "client":
                messages.append({"role": "user", "content": f"PROSPECT: {turn.text}"})
            elif turn.role == "rep":
                messages.append({"role": "user", "content": f"REP (me): {turn.text}"})
            else:
                messages.append({"role": "assistant", "content": turn.text})
        return messages

    def transcript_hint(self) -> str:
        """Build the Whisper biasing prompt for the next transcription.

        Whisper accepts a short prompt that nudges its decoder toward names it
        would otherwise mangle. We feed it the last thing the rep said plus the
        distinctive capitalised words seen so far in the call (product names,
        company names, people), so those keep transcribing consistently.

        Returns:
            A comma joined string, at most ``MAX_HINT_CHARS`` characters. Empty
            when the session has nothing useful to bias with yet.
        """
        pieces: list[str] = []

        last_rep = ""
        for turn in reversed(self.turns):
            if turn.role == "rep":
                last_rep = turn.text
                break
        if last_rep:
            pieces.append(_tail(last_rep, MAX_HINT_REP_CHARS))

        picked: dict[str, str] = {}
        for turn in reversed(self.turns):
            for raw in _WORD_RE.findall(turn.text):
                word = raw.strip("'._-/")
                if len(word) < 3:
                    continue
                if not word[0].isupper():
                    continue
                key = word.lower()
                if key in _HINT_STOPWORDS or key in picked:
                    continue
                picked[key] = word
                if len(picked) >= MAX_HINT_WORDS:
                    break
            if len(picked) >= MAX_HINT_WORDS:
                break
        pieces.extend(picked.values())

        hint = ", ".join(piece for piece in pieces if piece)
        if len(hint) > MAX_HINT_CHARS:
            hint = hint[:MAX_HINT_CHARS].rstrip().rstrip(",")
        return hint

    def snapshot(self) -> dict[str, object]:
        """Return a small JSON safe view of the session.

        Returns:
            A dict with the session id, transcript length and timestamps, shaped
            for the ``GET /api/session/{id}`` response.
        """
        return {
            "sessionId": self.id,
            "exists": True,
            "turns": len(self.turns),
            "createdAt": self.created_at,
        }


def _one_line(text: str) -> str:
    """Flatten text onto a single line and strip it.

    The coach transcript is read by the model one labelled line at a time, so a
    newline inside a turn would look like a new speaker with no label. Whisper
    and the copilot both hand us free text, so we squeeze every run of
    whitespace down to one space here instead of trusting them.

    Args:
        text: Raw turn text or teleprompter line.

    Returns:
        The same words on one line, with no leading or trailing space.
    """
    return " ".join(text.split())


def _tail(text: str, limit: int) -> str:
    """Return at most ``limit`` trailing characters of ``text``, on a word edge.

    Args:
        text: Source text.
        limit: Maximum characters to keep.

    Returns:
        The tail of the text, cut at a space when one is close enough so the
        result does not start mid word.
    """
    cleaned = text.strip()
    if limit <= 0 or len(cleaned) <= limit:
        return cleaned
    tail = cleaned[-limit:]
    space = tail.find(" ")
    if 0 <= space < limit // 4:
        tail = tail[space + 1 :]
    return tail.strip()


class SessionStore:
    """A plain dict of live sessions, no locks by design (see the module docstring)."""

    def __init__(self) -> None:
        """Create an empty store."""
        self._sessions: dict[str, Session] = {}
        self._created_total: int = 0
        self._dropped_total: int = 0
        self._expired_total: int = 0

    def create(
        self,
        *,
        system_prompt: str,
        language: str = "en",
        client_title: str | None = None,
        client_url: str | None = None,
        mode: SessionMode = "live",
        difficulty: str | None = None,
        persona_name: str | None = None,
        client_block: str = "",
        knowledge_hint: str = "",
        call_goal: str = "",
    ) -> Session:
        """Create and register a new session.

        Every practice argument is optional and defaulted, so the live call path
        calls this exactly as it always did.

        Args:
            system_prompt: The fully fused system prompt for the call.
            language: ISO 639-1 style code used for STT and for the prompt.
            client_title: Title of the scraped prospect page, if any.
            client_url: Normalized prospect URL, if any.
            mode: ``live`` for a real call, ``practice`` for a rehearsal.
            difficulty: Practice level, only meaningful in practice mode.
            persona_name: Name the AI client answers to, if one was found.
            client_block: Raw client facts for the persona prompt.
            knowledge_hint: Short summary of what the rep sells.
            call_goal: The one line goal for this call.

        Returns:
            The newly created session, already stored.
        """
        now = time.time()
        session = Session(
            id=str(uuid.uuid4()),
            system_prompt=system_prompt,
            language=language or "en",
            created_at=now,
            last_seen=now,
            turns=deque(maxlen=MAX_TURNS),
            client_title=client_title,
            client_url=client_url,
            mode=mode,
            difficulty=difficulty,
            persona_name=persona_name,
            client_block=client_block,
            knowledge_hint=knowledge_hint,
            call_goal=call_goal,
        )
        self._sessions[session.id] = session
        self._created_total += 1
        return session

    def get(self, sid: str) -> Session | None:
        """Look a session up and refresh its ``last_seen`` timestamp.

        Args:
            sid: The session id.

        Returns:
            The session, or ``None`` when the id is unknown or expired.
        """
        session = self._sessions.get(sid)
        if session is None:
            return None
        session.last_seen = time.time()
        return session

    def peek(self, sid: str) -> Session | None:
        """Look a session up without refreshing ``last_seen``.

        Args:
            sid: The session id.

        Returns:
            The session, or ``None`` when the id is unknown.
        """
        return self._sessions.get(sid)

    def drop(self, sid: str) -> None:
        """Remove a session if it exists.

        Args:
            sid: The session id.
        """
        if self._sessions.pop(sid, None) is not None:
            self._dropped_total += 1

    def sweep(self, ttl: float) -> int:
        """Delete every session whose ``last_seen`` is older than ``ttl``.

        Args:
            ttl: Maximum idle age in seconds. Values at or below zero are a no op
                so a misconfigured TTL cannot wipe live calls.

        Returns:
            How many sessions were removed.
        """
        if ttl <= 0:
            return 0
        cutoff = time.time() - ttl
        stale = [sid for sid, session in self._sessions.items() if session.last_seen < cutoff]
        for sid in stale:
            self._sessions.pop(sid, None)
        self._expired_total += len(stale)
        return len(stale)

    def stats(self) -> dict[str, int]:
        """Return counters for the health endpoint and for logging.

        Returns:
            A dict with the live session count, the total turns held across all
            sessions, and lifetime created, dropped and expired counters.
        """
        return {
            "sessions": len(self._sessions),
            "turns": sum(len(session.turns) for session in self._sessions.values()),
            "created": self._created_total,
            "dropped": self._dropped_total,
            "expired": self._expired_total,
        }

    def __len__(self) -> int:
        """Return the number of live sessions."""
        return len(self._sessions)


store = SessionStore()
"""Process wide session store shared by the REST routes and the WebSocket."""
