"""The realtime teleprompter WebSocket.

One socket carries two audio streams plus JSON control traffic:

* binary frames are ``byte[0] = stream id`` (0 client, 1 rep) followed by
  Int16 LE mono PCM at 16 kHz,
* text frames are JSON control messages, see the wire contract section 2.2.

The pipeline per stream is: PCM in, energy VAD segments an utterance, the
closed utterance is wrapped as a WAV and sent to Groq Whisper, the transcript
is appended to the session, and for the client stream a Groq chat completion
is streamed back token by token as the teleprompter suggestion.

Everything an operator would call a "stall" is designed against here: audio
ingest never waits on the network (a bounded queue plus one worker does the
slow work), a new suggestion cancels the in flight one (barge in), and every
per message handler is wrapped so a single bad frame can never kill the socket.

The same socket also serves practice mode, chosen by ``session.mode``. There the
prospect is not on the line at all: the server writes the client's lines with a
persona model, the browser speaks them out loud, and the REP stream is what
drives the call forward. The live path above is untouched by that branch.

Two doors are open to the rest of the app, both keyed by session id through a
small registry of live sockets. :func:`feed_call_audio` lets the phone routes
push already decoded audio into the SAME segmenters, the same utterance queue
and the same Whisper worker the browser feeds, so the phone lane duplicates
nothing. :func:`notify_call_state` lets them push the phone call's own state
down to the browser. Both are quiet no ops when no browser is connected.

Because both doors lead to one segmenter per lane, the phone wins while it is
carrying the call. A live Twilio call forks both sides to us, so browser audio
is refused at the door for the whole time, and the browser is told once why. See
:data:`PHONE_AUDIO_PROVIDERS`. Without that the rep's own voice would reach one
segmenter twice, once from the microphone and once, a moment later, over the
phone line, and the transcript would come back as chopped nonsense.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app import practice, prompts
from app.config import Settings, settings
from app.prompts import QUICK_ACTIONS, quick_action_prompt
from app.services import practice_engine
from app.services.audio import SAMPLE_RATE, VadConfig, VadSegmenter, wav_bytes
from app.services.groq_client import GroqClient, GroqError
from app.services.scoring import PracticeTurn
from app.services.session_store import Session, store

log = logging.getLogger("salescopilot.api.ws")

router = APIRouter(tags=["ws"])

StreamName = Literal["client", "rep"]

#: The browser worklet ships 512 sample frames, which is 32 ms at 16 kHz.
#: Advertised in the ``ready`` message so the client can size its buffers.
FRAME_MS = 32

#: Depth of the pending utterance queue. Eight closed utterances is already
#: several seconds of backlog, past that the call has moved on.
UTTERANCE_QUEUE_SIZE = 8

#: Minimum gap between two ``vad`` messages for the same stream, in seconds.
#: A speaking flip always sends immediately, this only throttles the level.
VAD_MIN_INTERVAL_S = 0.080

#: Hard ceiling on one binary audio frame, stream byte included. The contract
#: asks for 20 to 60 ms per frame, which is 641 to 1921 bytes, so 8 KiB is
#: roughly four times the largest legal frame and still small enough that the
#: pure Python RMS loop stays under a millisecond. It also keeps a single chunk
#: far below one ``max_utterance_ms`` worth of audio, which is what stops the
#: VAD force split from being fed a chunk longer than its own segment cap.
MAX_AUDIO_FRAME_BYTES = 8192

#: Hard ceiling on one JSON control frame. Every legal control frame is a few
#: hundred bytes, the biggest legitimate one carries a typed transcript line,
#: so 32 KiB is generous. Anything above this is rejected before it is decoded.
MAX_TEXT_FRAME_CHARS = 32768

#: Cap on a single injected transcript line. It is retained in the session and
#: replayed into every later LLM request, so it has to be bounded.
MAX_MANUAL_TEXT_CHARS = 4000

#: Cap on the free text note attached to a quick action.
MAX_NOTE_CHARS = 1000

#: How often the same "no API key" complaint may be repeated, in seconds.
NO_KEY_NOTICE_INTERVAL_S = 15.0

#: Call providers that fork the live call audio into our own pipeline.
#:
#: Twilio is the only one. Its TwiML asks for ``track="both_tracks"``, so once
#: the media socket is up BOTH of our lanes are being filled by the phone. The
#: other three providers never send us a byte: ``manual`` and ``whatsapp_link``
#: are the rep dialling on their own phone, and the WhatsApp Cloud API places
#: the call but does not stream it here. For those the browser stays the only
#: source of audio, exactly as it was before calling existed.
PHONE_AUDIO_PROVIDERS: frozenset[str] = frozenset({"twilio"})

#: Call states in which that fork is actually running.
#:
#: The media socket sets ``live`` on its ``start`` frame, before the first byte
#: of audio is decoded, so this flips at the right moment. ``dialing`` and
#: ``ringing`` are deliberately out: nothing is flowing yet, so the rep can
#: still use the browser lanes while the phone rings.
PHONE_AUDIO_STATES: frozenset[str] = frozenset({"live"})

#: Said once when browser audio is refused because the phone is carrying it.
_PHONE_AUDIO_NOTICE = (
    "The phone call is sending the sound now. Your microphone and the shared "
    "tab are not used while the call is on. You can stop them."
)

#: Message ids must be monotonic per session, and a session outlives its
#: socket (a reconnect resumes the same call), so the counter lives here
#: keyed by session id instead of on the connection object.
_ID_COUNTERS: dict[str, int] = {}

#: Hard cap so a long lived server cannot accumulate counters forever.
_ID_COUNTER_CAP = 1024

#: Every teleprompter socket that is open right now, by session id. It is what
#: lets the phone routes reach a browser they hold no reference to. A session
#: has at most one socket, so a reconnect simply replaces the entry, and a
#: socket only ever removes its own entry on the way out. Filled in
#: :meth:`TeleprompterConnection.run` and cleared in its finally block, so an
#: id in here always means a socket that was accepted and is not torn down yet.
_CONNECTIONS: dict[str, TeleprompterConnection] = {}

#: Fallback practice level when the session carries none.
DEFAULT_DIFFICULTY = "normal"

#: Fallback turn limit when the difficulty entry carries none. It counts client
#: lines only, the same way ``app.practice`` writes it and the same way the
#: persona prompt states it, so this is fourteen lines from the client.
DEFAULT_TURN_LIMIT = 14

_QUICK_ACTION_BY_KEY: dict[str, dict[str, Any]] = {
    str(action["key"]): dict(action) for action in QUICK_ACTIONS
}

_DIFFICULTY_BY_KEY: dict[str, dict[str, Any]] = {
    str(entry.get("key", "")): dict(entry) for entry in practice.DIFFICULTIES
}

#: Mood of the hardcoded opening line, so the very first client turn already
#: sounds like the level the rep picked. No model call is made for the opener.
_OPENING_MOOD: dict[str, str] = {"warm": "warm", "normal": "neutral", "brutal": "cold"}

#: What the rep reads when the practice call ends. Plain, short, no jargon.
_PRACTICE_OVER_MESSAGES: dict[str, str] = {
    "hangup": "The client hung up on you.",
    "rep_ended": "You ended the practice call.",
    "goal_reached": "The client said yes. Good work.",
    "turn_limit": "That is the end of this practice call.",
}

_PRACTICE_OVER_DEFAULT = "The practice call is over."

_PRACTICE_CLIENT_AUDIO_NOTICE = (
    "This is a practice call, so the client voice is made here. Shared tab audio "
    "is not used. Only your microphone is needed."
)

_NOT_PRACTICE_MESSAGE = "This is a real call, so practice mode is off for it."

_NO_KEY_MESSAGE = (
    "GROQ_API_KEY is not set. Put a free key from https://console.groq.com/keys "
    "into backend/.env and restart the backend."
)


def _stream_name(stream_id: int) -> StreamName | None:
    """Map a binary frame stream byte to its name.

    Args:
        stream_id: The first byte of a binary frame.

    Returns:
        ``"client"`` for 0, ``"rep"`` for 1, None for anything else.
    """
    if stream_id == 0:
        return "client"
    if stream_id == 1:
        return "rep"
    return None


def _as_stream(value: str) -> StreamName:
    """Coerce a client supplied stream name, defaulting to the client stream.

    Args:
        value: The raw ``stream`` field from a control frame.

    Returns:
        ``"rep"`` only for an exact match, ``"client"`` otherwise.
    """
    return "rep" if value == "rep" else "client"


def _new_segmenter(cfg_settings: Settings) -> VadSegmenter:
    """Build a VAD segmenter from the process settings.

    Args:
        cfg_settings: The loaded settings object.

    Returns:
        A fresh segmenter at the default sensitivity of 1.0.
    """
    return VadSegmenter(
        VadConfig(
            silence_ms=cfg_settings.vad_silence_ms,
            min_speech_ms=cfg_settings.vad_min_speech_ms,
            max_utterance_ms=cfg_settings.vad_max_utterance_ms,
            preroll_ms=cfg_settings.vad_preroll_ms,
            sensitivity=1.0,
        )
    )


def _client_reply_fields(reply: object) -> tuple[str, str, str, str]:
    """Normalise a persona reply into the four fields the wire carries.

    ``practice_engine`` owns the persona call and may hand back a dataclass or
    a plain dict, and the model behind it can always drop a key, so every field
    is read defensively and falls back to a neutral value. A reply with no
    ``say`` is treated as no reply at all by the caller.

    Args:
        reply: Whatever ``practice_engine.next_client_turn`` returned.

    Returns:
        A tuple of ``(say, mood, intent, objection)``, each already stripped.
    """

    def pick(*names: str, default: str) -> str:
        for name in names:
            if isinstance(reply, Mapping):
                if name not in reply:
                    continue
                value: object = reply[name]
            else:
                value = getattr(reply, name, None)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return default

    return (
        pick("say", "text", default=""),
        pick("mood", default="neutral"),
        pick("intent", default="question"),
        pick("objection", default="none"),
    )


def _plain_groq_error(exc: GroqError, prefix: str) -> str:
    """Turn a raw Groq failure into one plain sentence the rep can act on.

    The rep is mid call. A stack of provider jargon helps nobody, and the two
    failures that actually happen on a free key have a clear, short answer.

    Args:
        exc: The error the client raised.
        prefix: What was being attempted, as a full sentence.

    Returns:
        A short message in plain English.
    """
    status = getattr(exc, "status", 0)
    if status == 429:
        return f"{prefix} You have hit the free Groq limit. Wait a minute, then try again."
    if status in (401, 403):
        return f"{prefix} The Groq key was refused. Check GROQ_API_KEY in backend/.env."
    if status == 404:
        return f"{prefix} That model is gone. Check LLM_MODEL in backend/.env."
    if status >= 500 or status == 0:
        return f"{prefix} Groq did not answer. Try again in a moment."
    return f"{prefix} {exc}"


class TeleprompterConnection:
    """One live socket: two VAD streams, one STT worker, one LLM task.

    Attributes are private by convention. The public surface is :meth:`run`,
    which owns the whole lifecycle including cleanup.
    """

    def __init__(
        self,
        ws: WebSocket,
        session: Session,
        groq: GroqClient,
        conn_settings: Settings,
    ) -> None:
        """Wire the connection up without touching the socket yet.

        Args:
            ws: The accepted or about to be accepted WebSocket.
            session: The call session this socket is attached to.
            groq: The shared Groq client.
            conn_settings: The process settings.
        """
        self._ws = ws
        self._session = session
        self._groq = groq
        self._settings = conn_settings

        self._segmenters: dict[StreamName, VadSegmenter] = {
            "client": _new_segmenter(conn_settings),
            "rep": _new_segmenter(conn_settings),
        }
        self._queue: asyncio.Queue[tuple[StreamName, bytes]] = asyncio.Queue(
            maxsize=UTTERANCE_QUEUE_SIZE
        )
        self._send_lock = asyncio.Lock()
        # Separate from _send_lock on purpose: a cancelled suggestion needs
        # _send_lock to emit its final message, so the two must never be the
        # same lock. This one makes "cancel the old task, then install the new
        # one" atomic against the other task that can also start suggestions.
        self._llm_lock = asyncio.Lock()
        self._worker_task: asyncio.Task[None] | None = None
        self._llm_task: asyncio.Task[None] | None = None

        self._auto_suggest = True
        self._closed = False
        self._last_vad_at: dict[StreamName, float] = {"client": 0.0, "rep": 0.0}
        self._last_speaking: dict[StreamName, bool] = {"client": False, "rep": False}
        self._last_no_key_notice = 0.0
        self._overruns = 0
        self._id_counter = _ID_COUNTERS.get(session.id, 0)

        # Phone lane state. All of it is inert unless a provider that forks
        # audio to us, which today means Twilio, is actually live.
        # True while the phone is the one filling the lanes. It tracks the
        # EDGE, the per frame gate reads the session itself, so a state change
        # that arrives while frames are in flight can never be missed.
        self._phone_feeding = False
        self._phone_audio_noticed = False
        self._phone_muted_frames = 0

        # Practice mode state. All of it is inert on a live call.
        self._practice_started = False
        self._practice_over = False
        # True while the browser is speaking the client line through the
        # speakers. See _practice_accepts_audio, this is the half duplex flag.
        self._client_voice_playing = False
        self._client_audio_noticed = False
        # The suggestion the rep can actually read right now. Recorded with the
        # next rep turn so the debrief can measure how much the prompter helped.
        self._suggestion_on_screen = ""

    # ================================================================== #
    # lifecycle
    # ================================================================== #

    async def run(self) -> None:
        """Accept the socket, serve it, and always clean up afterwards.

        The receive loop runs on the calling task while a second task drains
        the utterance queue. Whatever happens, the finally block cancels both
        the worker and the LLM task and empties the queue.

        The connection is published in :data:`_CONNECTIONS` as soon as the
        socket is accepted, so a phone call that rings before the first frame
        arrives can still report its state, and it is withdrawn in the finally
        block. Registration happens after ``accept`` on purpose: a socket that
        was never accepted cannot be sent anything.

        A session outlives its socket and a phone call outlives it too, so the
        current call state is replayed right after ``ready``. Without that a
        refresh in the middle of a paid Twilio call shows an idle chip, hides
        the Hang up button, and re arms Start, so the rep's next press bills a
        second call while the first one is still up.
        """
        await self._ws.accept()
        _CONNECTIONS[self._session.id] = self
        self._worker_task = asyncio.create_task(self._worker(), name="utterance-worker")
        try:
            await self._send(
                {
                    "type": "ready",
                    "sessionId": self._session.id,
                    "sampleRate": SAMPLE_RATE,
                    "frameMs": FRAME_MS,
                    "model": {
                        "stt": self._settings.stt_model,
                        "llm": self._settings.llm_model,
                    },
                    "quickActions": [dict(action) for action in QUICK_ACTIONS],
                }
            )
            await self._status("idle")
            # Only when there is something to say. A call_state frame for an
            # idle session would tell the browser nothing it does not already
            # assume, and this replay must not look like a new call starting.
            if self._call_state != "idle":
                await self.send_call_state(self._session.call_snapshot())
            if not self._groq.configured:
                await self._notify_missing_key(force=True)
            await self._recv_loop()
        except WebSocketDisconnect:
            log.info("session %s disconnected", self._session.id)
        except Exception as exc:  # noqa: BLE001 - never let one socket take the app down.
            log.exception("teleprompter socket failed for session %s", self._session.id)
            await self._send({"type": "error", "code": "internal", "message": str(exc)})
        finally:
            await self._teardown()

    async def _teardown(self) -> None:
        """Cancel background work and drop pending audio.

        The session itself is deliberately left in the store so a reconnect
        resumes the same call with its transcript intact.

        The registry entry is only dropped when it still points at this
        connection. A reconnect on the same session id registers the new socket
        before the old one finishes tearing down, and a blind pop there would
        delete the live socket and leave the browser deaf to call state.
        """
        self._closed = True
        if _CONNECTIONS.get(self._session.id) is self:
            del _CONNECTIONS[self._session.id]
        await self._cancel_llm()

        worker = self._worker_task
        self._worker_task = None
        if worker is not None and not worker.done():
            worker.cancel()
            # asyncio.wait, not "suppress(CancelledError): await worker".
            # Awaiting a cancelled task directly raises CancelledError in THIS
            # task, and suppressing it also swallows a cancellation aimed at us
            # (uvicorn shutting the endpoint task down), which would leave the
            # teardown half done and the handler hanging. asyncio.wait absorbs
            # the awaited task's cancellation and still propagates our own.
            await asyncio.wait({worker})

        drained = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            drained += 1

        log.info(
            "session %s socket closed, dropped %d pending utterances, %d overruns, "
            "%d browser frames refused while the phone carried the sound",
            self._session.id,
            drained,
            self._overruns,
            self._phone_muted_frames,
        )

    async def _recv_loop(self) -> None:
        """Read frames until the peer goes away.

        ``WebSocket.receive`` hands back the raw ASGI message so one loop can
        serve both binary audio and JSON control frames without a second
        socket read path.
        """
        while True:
            message = await self._ws.receive()
            kind = message.get("type")
            if kind == "websocket.disconnect":
                break
            if kind != "websocket.receive":
                continue

            data = message.get("bytes")
            if data is not None:
                await self._handle_binary(data)
                continue

            raw = message.get("text")
            if raw is not None:
                await self._handle_text(raw)

    # ================================================================== #
    # inbound frames
    # ================================================================== #

    async def _handle_binary(self, data: bytes) -> None:
        """Feed one audio frame into the right VAD segmenter.

        Frames larger than :data:`MAX_AUDIO_FRAME_BYTES` are rejected outright.
        Without that ceiling the only limit is the websocket server's 16 MiB
        frame size, and a frame that big is two separate problems: the pure
        Python RMS loop blocks the event loop for a third of a second, and one
        chunk holding more audio than ``max_utterance_ms`` drives the VAD force
        split into re emitting a multi megabyte utterance on every push.

        A frame can also be dropped on purpose twice over. While the phone is
        carrying the call the browser is not a source at all, see
        :meth:`_phone_owns_audio`, and in practice mode the microphone is gated
        while the browser speaks the client line, see
        :meth:`_practice_accepts_audio`.

        Args:
            data: The raw binary frame, stream byte followed by Int16 LE PCM.
        """
        try:
            if len(data) < 3:
                return
            if len(data) > MAX_AUDIO_FRAME_BYTES:
                await self._send(
                    {
                        "type": "error",
                        "code": "frame_too_large",
                        "message": (
                            f"Audio frame of {len(data)} bytes was dropped, the limit is "
                            f"{MAX_AUDIO_FRAME_BYTES} bytes. Send 20 to 60 ms per frame."
                        ),
                    }
                )
                return
            stream = _stream_name(data[0])
            if stream is None:
                await self._send(
                    {
                        "type": "error",
                        "code": "bad_stream",
                        "message": f"Unknown stream id {data[0]}, expected 0 or 1.",
                    }
                )
                return

            if self._phone_owns_audio:
                # The phone is filling both lanes already. Letting this frame
                # in would put the same voice into one segmenter twice, once
                # from the room and once, a fraction of a second later, off the
                # line. Checked per frame against the session, so the moment a
                # call goes live the browser stops being a source, even if it
                # is mid capture and has not been told yet.
                self._phone_muted_frames += 1
                await self._notify_phone_audio()
                return

            if self._practice and not await self._practice_accepts_audio(stream):
                return

            await self._ingest_pcm(stream, data[1:])
        except Exception as exc:  # noqa: BLE001 - one bad frame must not close the call.
            log.exception("binary frame failed on session %s", self._session.id)
            await self._send({"type": "error", "code": "internal", "message": str(exc)})

    async def _ingest_pcm(self, stream: StreamName, pcm: bytes) -> bool:
        """Push one lane's PCM through the VAD and on to the utterance queue.

        This is the only door audio walks through. The browser reaches it from
        :meth:`_handle_binary` and the phone from :meth:`ingest_phone_audio`,
        which is what keeps the VAD, the queue, the Whisper worker and the
        copilot single copies instead of one set per source.

        Args:
            stream: Which lane this audio belongs to.
            pcm: Int16 LE mono PCM at 16 kHz, no stream byte.

        Returns:
            True when the audio was segmented, False when the frame held
            nothing usable.
        """
        if len(pcm) % 2:
            # An odd byte count would shift every following sample by one
            # byte and turn the audio into noise, so drop the stray byte.
            pcm = pcm[:-1]
        if not pcm:
            return False

        segmenter = self._segmenters[stream]
        events = segmenter.push(pcm)
        await self._emit_vad(stream, segmenter.speaking, segmenter.last_rms)

        for event in events:
            if event.kind == "utterance" and event.pcm:
                await self._enqueue(stream, event.pcm)
            elif event.kind == "speech_start" and stream == "client":
                await self._status("listening")
        return True

    async def ingest_phone_audio(self, stream: StreamName, pcm: bytes) -> bool:
        """Feed one frame of phone audio into this socket's own pipeline.

        This is the door the phone routes come in through. A carrier hands them
        8 kHz mu-law, they decode it to the same Int16 LE 16 kHz PCM the browser
        worklet sends, then call this. From here nothing is duplicated: the same
        segmenter, the same utterance queue, the same Whisper worker and the
        same copilot task the browser drives.

        Be careful about the lane, it is the easiest thing to invert. ``stream``
        is OUR lane name, ``"client"`` or ``"rep"``, not the carrier's track
        name. On a Twilio call the leg is the rep's phone, so Twilio's
        ``outbound`` track carries the client and its ``inbound`` track carries
        the rep. The caller owns that mapping, because the caller is the only
        place that knows which leg was dialled.

        A lane is shared with the browser, it is not split, so the two sources
        must never run at once or one segmenter hears the same person twice.
        That is not left to good manners on either side. While the call is live
        :meth:`_phone_owns_audio` is true, so :meth:`_handle_binary` refuses
        every browser frame for this session and tells the browser once why.

        Args:
            stream: ``"client"`` or ``"rep"``, our lane, already mapped.
            pcm: Int16 LE mono PCM at 16 kHz. A stray odd byte is dropped.

        Returns:
            True when the audio reached the VAD. False when it was dropped,
            which happens when the socket is closing, when the frame is over
            :data:`MAX_AUDIO_FRAME_BYTES`, when there is nothing usable in it,
            or when this is a practice session, which has no phone call at all.
        """
        if self._closed:
            return False
        if len(pcm) > MAX_AUDIO_FRAME_BYTES:
            # Same ceiling as a browser frame, and for the same reason: the RMS
            # loop is pure Python and the VAD force split must never be handed
            # more audio than one whole utterance. Logged, not sent, because a
            # bad phone frame is not the browser's fault to read about.
            log.warning(
                "phone frame of %d bytes dropped on session %s, the limit is %d",
                len(pcm),
                self._session.id,
                MAX_AUDIO_FRAME_BYTES,
            )
            return False
        if self._practice:
            # A practice call has no line and no prospect. Letting real audio in
            # would have the written client answering a real voice.
            return False
        try:
            return await self._ingest_pcm(stream, pcm)
        except Exception:  # noqa: BLE001 - one bad frame must not close the call.
            log.exception("phone frame failed on session %s", self._session.id)
            return False

    async def _handle_text(self, raw: str) -> None:
        """Decode and dispatch one JSON control frame.

        Oversized frames are refused before they are parsed, and the decode is
        guarded broadly on purpose. A deeply nested payload makes the CPython
        JSON scanner raise ``RecursionError``, which derives from
        ``RuntimeError`` and not from ``ValueError``, so a narrow guard would
        let one hostile 40 KB frame escape and tear down a live call.

        Args:
            raw: The text payload exactly as received.
        """
        if len(raw) > MAX_TEXT_FRAME_CHARS:
            await self._send(
                {
                    "type": "error",
                    "code": "frame_too_large",
                    "message": (
                        f"Control frame of {len(raw)} characters was dropped, the limit is "
                        f"{MAX_TEXT_FRAME_CHARS}."
                    ),
                }
            )
            return

        try:
            message = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - RecursionError is not a ValueError.
            log.debug("undecodable control frame on session %s: %s", self._session.id, exc)
            await self._send(
                {"type": "error", "code": "bad_json", "message": "Control frame was not valid JSON."}
            )
            return

        if not isinstance(message, dict):
            await self._send(
                {
                    "type": "error",
                    "code": "bad_message",
                    "message": "Control frame must be a JSON object with a type field.",
                }
            )
            return

        kind = message.get("type")
        try:
            if kind == "ping":
                await self._send({"type": "pong", "t": message.get("t", 0)})
            elif kind == "control":
                await self._handle_control(message)
            elif kind == "quick_action":
                await self._handle_quick_action(message)
            elif kind == "manual_text":
                await self._handle_manual_text(message)
            elif kind == "config":
                await self._handle_config(message)
            elif kind == "redo":
                await self._handle_redo()
            elif kind == "practice_start":
                await self._handle_practice_start()
            elif kind == "practice_end":
                await self._handle_practice_end()
            elif kind == "speech_state":
                await self._handle_speech_state(message)
            else:
                await self._send(
                    {
                        "type": "error",
                        "code": "unknown_type",
                        "message": f"Unsupported control type {kind!r}.",
                    }
                )
        except Exception as exc:  # noqa: BLE001 - report, do not die.
            log.exception("control frame %r failed on session %s", kind, self._session.id)
            await self._send({"type": "error", "code": "internal", "message": str(exc)})

    async def _handle_control(self, message: dict[str, Any]) -> None:
        """Handle mic lifecycle messages: start, stop, reset and flush.

        Args:
            message: The decoded control frame.
        """
        action = str(message.get("action", "")).lower()
        target = str(message.get("stream", "all")).lower()
        if target not in ("client", "rep", "all"):
            target = "all"
        all_names: list[StreamName] = ["client", "rep"]
        names: list[StreamName] = all_names if target == "all" else [_as_stream(target)]

        if action == "start":
            if self._phone_owns_audio:
                # Say it at the moment the rep opens the lane, not one frame
                # later. The frames are refused either way.
                await self._notify_phone_audio()
            for name in names:
                self._segmenters[name].reset()
                self._last_speaking[name] = False
            await self._status("listening", target)
            return

        if action in ("stop", "flush"):
            for name in names:
                segmenter = self._segmenters[name]
                for event in segmenter.flush():
                    if event.kind == "utterance" and event.pcm:
                        await self._enqueue(name, event.pcm)
                self._last_speaking[name] = False
                await self._emit_vad(name, False, 0.0, force=True)
            if action == "stop":
                await self._status("idle", target)
            return

        if action == "reset":
            await self._cancel_llm()
            self._session.turns.clear()
            for name in all_names:
                self._segmenters[name].reset()
                self._last_speaking[name] = False
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._queue.task_done()
            await self._status("idle", "reset")
            return

        await self._send(
            {
                "type": "error",
                "code": "bad_action",
                "message": f"Unsupported control action {action!r}.",
            }
        )

    async def _handle_quick_action(self, message: dict[str, Any]) -> None:
        """Fire an instant rebuttal for one of the frozen objection buttons.

        Args:
            message: The decoded quick action frame.
        """
        key = str(message.get("key", ""))
        action = _QUICK_ACTION_BY_KEY.get(key)
        if action is None:
            await self._send(
                {
                    "type": "error",
                    "code": "unknown_action",
                    "message": f"Unknown quick action {key!r}.",
                }
            )
            return

        # Capped here because it goes straight into the prompt sent to Groq.
        note = str(message.get("note", "") or "").strip()[:MAX_NOTE_CHARS]
        if not self._groq.configured:
            await self._notify_missing_key(force=True)
            return

        messages = self._build_messages()
        messages.append({"role": "user", "content": quick_action_prompt(key, note)})
        source_text = str(action.get("label", key))
        if note:
            source_text = f"{source_text}: {note}"
        await self._start_llm(messages, "quick_action", source_text, 0)

    async def _handle_manual_text(self, message: dict[str, Any]) -> None:
        """Inject a typed transcript line, the fallback when audio is not usable.

        A client line behaves exactly like a spoken one, so it also respects
        the autoSuggest flag. In practice mode a typed line behaves like a
        spoken one too, which is what lets the whole practice call be driven
        from the keyboard when there is no working microphone.

        The line is clamped to :data:`MAX_MANUAL_TEXT_CHARS`. It is retained in
        the session for the whole TTL and replayed into every later suggestion
        request, so an uncapped line would be paid for once in memory and again
        on every call to Groq. The transcript deque bounds the number of turns,
        not their size.

        Args:
            message: The decoded manual text frame.
        """
        text = str(message.get("text", "") or "").strip()[:MAX_MANUAL_TEXT_CHARS]
        if not text:
            return
        stream = _as_stream(str(message.get("stream", "client")).lower())

        mid = self._next_id()
        self._session.append(stream, text)
        await self._send(
            {
                "type": "transcript",
                "stream": stream,
                "text": text,
                "id": mid,
                "ms": 0,
                "final": True,
            }
        )

        if self._practice:
            if stream == "rep":
                await self._practice_rep_turn(text, 0)
            else:
                await self._status("idle")
            return

        if stream != "client" or not self._auto_suggest:
            await self._status("idle")
            return
        if not self._groq.configured:
            await self._notify_missing_key()
            return
        await self._start_llm(self._build_messages(), "manual", text, 0)

    async def _handle_redo(self) -> None:
        """Ask the copilot for the same moment again.

        The reading style switch uses this. Without it, flipping to points only
        changes the NEXT line, so the rep presses the switch, looks at the glass,
        sees the same sentence sitting there and reasonably concludes it is
        broken. Now the line in front of them changes.

        The copilot's own last answer is dropped first, because it is the thing
        being replaced. Leaving it in would have the model write a variation on
        it, or treat it as already said and move the call forward instead.
        """
        if not self._groq.configured:
            await self._notify_missing_key()
            return

        turns = self._session.turns
        if turns and turns[-1].role == "copilot":
            turns.pop()

        # Answer the last thing the client actually said. With nothing to answer
        # there is nothing to redo, and saying so is better than a blank screen.
        source = next((t.text for t in reversed(turns) if t.role == "client"), "")
        if not source:
            await self._send(
                {
                    "type": "error",
                    "code": "nothing_to_redo",
                    "message": "There is no line to write again yet.",
                }
            )
            return

        await self._start_llm(self._build_messages(), "speech", source, 0)

    async def _handle_config(self, message: dict[str, Any]) -> None:
        """Apply live tuning: VAD sensitivity and the auto suggest switch.

        With ``autoSuggest`` off, client speech is still transcribed and still
        lands in the transcript, it just does not call the LLM.

        Args:
            message: The decoded config frame.
        """
        raw_sensitivity = message.get("sensitivity")
        if isinstance(raw_sensitivity, (int, float)):
            value = max(0.5, min(3.0, float(raw_sensitivity)))
            for segmenter in self._segmenters.values():
                segmenter.set_sensitivity(value)

        raw_auto = message.get("autoSuggest")
        if isinstance(raw_auto, bool):
            self._auto_suggest = raw_auto

        # The reading style can flip in the middle of a live call, so it is not
        # baked into the session's system prompt. It only picks which extra
        # system message gets appended on the next suggestion.
        raw_style = message.get("style")
        if isinstance(raw_style, str) and raw_style.strip().lower() in prompts.STYLES:
            self._session.style = raw_style.strip().lower()

    # ================================================================== #
    # utterance pipeline
    # ================================================================== #

    async def _enqueue(self, stream: StreamName, pcm: bytes) -> None:
        """Queue a closed utterance for transcription, newest wins.

        When the queue is full the OLDEST pending utterance is dropped rather
        than the new one. In a live call stale audio is worthless: whatever
        the prospect said five seconds ago has already scrolled off the
        teleprompter, while the utterance that just closed is the one the rep
        has to answer right now. The drop is reported as an ``overrun`` error
        so the UI can show that the backend fell behind.

        Args:
            stream: Which stream the audio came from.
            pcm: Int16 LE mono PCM at 16 kHz for the whole utterance.
        """
        item = (stream, pcm)
        try:
            self._queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass

        dropped = False
        with contextlib.suppress(asyncio.QueueEmpty):
            self._queue.get_nowait()
            self._queue.task_done()
            dropped = True

        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            log.warning("utterance queue still full on session %s", self._session.id)
            return

        if dropped:
            self._overruns += 1
            await self._send(
                {
                    "type": "error",
                    "code": "overrun",
                    "message": "Speech is arriving faster than it can be transcribed, the oldest clip was dropped.",
                }
            )

    async def _worker(self) -> None:
        """Drain the utterance queue strictly in order.

        One worker means transcripts land in the order they were spoken, and
        it also means a slow Groq call can never block audio ingest.
        """
        while True:
            stream, pcm = await self._queue.get()
            try:
                await self._process_utterance(stream, pcm)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one utterance, not the call.
                log.exception("utterance failed on session %s", self._session.id)
                await self._send({"type": "error", "code": "internal", "message": str(exc)})
            finally:
                self._queue.task_done()

    async def _process_utterance(self, stream: str, pcm: bytes) -> None:
        """Transcribe one utterance and, for the client stream, answer it.

        Args:
            stream: ``"client"`` for the prospect, ``"rep"`` for the user.
            pcm: Int16 LE mono PCM at 16 kHz for the whole utterance.
        """
        name: StreamName = "rep" if stream == "rep" else "client"
        mid = self._next_id()

        # The row appears immediately so the UI can show a "listening" line
        # while Whisper is still working. The text arrives with the final
        # transcript under the same id.
        await self._send({"type": "partial_transcript", "stream": name, "text": "", "id": mid})
        await self._status("transcribing")

        if not self._groq.configured:
            await self._send(
                {
                    "type": "transcript",
                    "stream": name,
                    "text": "",
                    "id": mid,
                    "ms": 0,
                    "final": True,
                }
            )
            await self._notify_missing_key()
            await self._status("idle")
            return

        started = time.perf_counter()
        try:
            text = await self._groq.transcribe(
                wav_bytes(pcm),
                language=self._session.language,
                prompt=self._transcript_hint(),
            )
        except GroqError as exc:
            await self._send(
                {"type": "error", "code": "stt_failed", "message": f"Transcription failed: {exc}"}
            )
            await self._status("idle")
            return
        stt_ms = int((time.perf_counter() - started) * 1000)

        text = (text or "").strip()
        await self._send(
            {
                "type": "transcript",
                "stream": name,
                "text": text,
                "id": mid,
                "ms": stt_ms,
                "final": True,
            }
        )
        if not text:
            await self._status("idle")
            return

        self._session.append(name, text)

        # Practice mode inverts the live rule below: there is no prospect on
        # the line, so it is the REP utterance that moves the call forward.
        if self._practice:
            if name == "rep":
                await self._practice_rep_turn(text, stt_ms)
            else:
                await self._status("idle")
            return

        # The rep stream is context only. It never triggers a suggestion,
        # otherwise the copilot would answer the rep's own voice.
        if name == "rep" or not self._auto_suggest:
            await self._status("idle")
            return

        await self._start_llm(self._build_messages(), "speech", text, stt_ms)

    def _build_messages(self) -> list[dict[str, str]]:
        """Assemble the chat payload: system prompt plus the recent window.

        Returns:
            OpenAI style messages ready for ``stream_chat``.
        """
        directive = prompts.style_directive(self._session.style)

        # The directive joins the rules block rather than trailing the history.
        # Measured on the real model: with a few turns of history behind it, a
        # trailing system message loses to the conversation's own momentum and
        # the answer comes back as sentences anyway.
        system = self._session.system_prompt
        if directive:
            system = f"{system}\n\n{directive}"

        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        messages.extend(
            self._session.recent_messages(
                self._settings.transcript_window_turns,
                # The copilot's own past lines are the strongest pull back to
                # full sentences, and in points mode they are not what the rep
                # said either. See Session.recent_messages.
                include_copilot=directive is None,
            )
        )
        return messages

    def _transcript_hint(self) -> str | None:
        """Build the Whisper biasing prompt from the live transcript.

        Feeding the last few turns to Whisper is what makes product names and
        the rep's own vocabulary come back spelled correctly.

        Returns:
            The hint string, or None when there is nothing useful yet.
        """
        hint = ""
        maker = getattr(self._session, "transcript_hint", None)
        if callable(maker):
            with contextlib.suppress(Exception):
                hint = str(maker() or "")
        if not hint:
            # Fallback so biasing still works if the session model has no
            # helper: the tail of the last few turns. ``turns`` is a deque,
            # which does not slice, so materialise it first.
            recent = list(self._session.turns)[-4:]
            hint = " ".join(turn.text for turn in recent if turn.text)
        hint = hint.strip()
        if not hint:
            return None
        return hint[-800:]

    # ================================================================== #
    # LLM
    # ================================================================== #

    async def _start_llm(
        self,
        messages: list[dict[str, str]],
        trigger: str,
        source_text: str,
        stt_ms: int,
    ) -> None:
        """Barge in: cancel the in flight suggestion, then start a new one.

        The cancelled task emits its own ``suggestion_done`` from its finally
        block before this returns, so the UI is never left with a dangling id.

        Two independent tasks reach this method, the utterance worker (speech)
        and the receive loop (quick action, manual text), so the cancel plus
        replace pair is done under ``_llm_lock``. Without it the second caller
        can slip in while the first is still awaiting the old task, see an empty
        slot, install its own task and then have that task overwritten and
        orphaned, which leaves two live streams writing suggestions into one
        socket. The contract allows exactly one in flight suggestion.

        Args:
            messages: The chat payload for this suggestion.
            trigger: ``"speech"``, ``"quick_action"`` or ``"manual"``.
            source_text: What the suggestion is answering, shown in the UI.
            stt_ms: Measured STT latency, 0 when there was no audio step.
        """
        async with self._llm_lock:
            await self._cancel_llm_locked()
            sid = self._next_id()
            self._llm_task = asyncio.create_task(
                self._run_llm(messages, trigger, source_text, stt_ms, sid),
                name=f"llm-{sid}",
            )

    async def _cancel_llm(self) -> None:
        """Stop the current suggestion and wait for it to finish tidying up.

        Must never be called while holding ``_send_lock``, the cancelled task
        needs that lock to emit its final message. Callers that already hold
        ``_llm_lock`` must use :meth:`_cancel_llm_locked` instead, the lock is
        not reentrant.
        """
        async with self._llm_lock:
            await self._cancel_llm_locked()

    async def _cancel_llm_locked(self) -> None:
        """Cancel the in flight suggestion, assuming ``_llm_lock`` is held."""
        task = self._llm_task
        self._llm_task = None
        if task is not None and not task.done():
            task.cancel()
            # See _teardown: awaiting the cancelled task directly would let a
            # suppress() eat a cancellation delivered to this task, so the
            # utterance worker would survive its own cancel and loop forever.
            await asyncio.wait({task})

    async def _run_llm(
        self,
        messages: list[dict[str, str]],
        trigger: str,
        source_text: str,
        stt_ms: int,
        sid: str,
    ) -> None:
        """Stream one suggestion to the client, token by token.

        The finally block emits ``suggestion_done`` with whatever text was
        produced, including on a barge in, but only for an id the client has
        already seen announced by ``suggestion_start``. ``suggestion_start``
        goes out before the ``thinking`` status precisely so a cancellation in
        that window cannot produce a done event for an unknown id.

        The partial text of a barged in suggestion is NOT appended to the
        transcript: the UI replaced it the moment the new suggestion started,
        so the rep never read it aloud, and feeding a sentence fragment back as
        an assistant turn makes the next suggestion continue or restate it.

        Args:
            messages: The chat payload.
            trigger: What caused this suggestion.
            source_text: The text being answered.
            stt_ms: Measured STT latency in milliseconds.
            sid: The message id shared by start, delta and done.
        """
        parts: list[str] = []
        first_token_ms: int | None = None
        announced = False
        cancelled = False
        started = time.perf_counter()
        try:
            await self._send(
                {
                    "type": "suggestion_start",
                    "id": sid,
                    "trigger": trigger,
                    "sourceText": source_text,
                    "sttMs": stt_ms,
                }
            )
            announced = True
            await self._send({"type": "status", "state": "thinking"})
            stream = self._groq.stream_chat(
                messages,
                max_tokens=self._settings.llm_max_tokens,
                temperature=self._settings.llm_temperature,
            )
            try:
                async for delta in stream:
                    if not delta:
                        continue
                    if first_token_ms is None:
                        first_token_ms = int((time.perf_counter() - started) * 1000)
                    parts.append(delta)
                    await self._send({"type": "suggestion_delta", "id": sid, "delta": delta})
            finally:
                closer = getattr(stream, "aclose", None)
                if closer is not None:
                    with contextlib.suppress(Exception):
                        await closer()
        except asyncio.CancelledError:
            # Barge in. Let the finally below close the id out, then let the
            # cancellation continue so the canceller can move on.
            cancelled = True
            raise
        except GroqError as exc:
            await self._send(
                {"type": "error", "code": "llm_failed", "message": f"Suggestion failed: {exc}"}
            )
        except Exception as exc:  # noqa: BLE001 - report, do not die.
            log.exception("llm stream failed on session %s", self._session.id)
            await self._send({"type": "error", "code": "internal", "message": str(exc)})
        finally:
            text = "".join(parts).strip()
            total_ms = int((time.perf_counter() - started) * 1000)
            with contextlib.suppress(Exception):
                if announced:
                    await self._send(
                        {
                            "type": "suggestion_done",
                            "id": sid,
                            "text": text,
                            # Never null. When the stream failed or was
                            # cancelled before a single token arrived, report
                            # the elapsed time instead, so the frontend's
                            # latency meter always has an integer to render.
                            "firstTokenMs": (
                                total_ms if first_token_ms is None else first_token_ms
                            ),
                            "totalMs": total_ms,
                            "sttMs": stt_ms,
                        }
                    )
                # Sent unguarded: this task is still the in flight one as far
                # as _status is concerned, and on a barge in it lands before
                # the replacement task announces itself.
                await self._send({"type": "status", "state": "idle"})
            if text and not cancelled:
                self._session.append("copilot", text)
                # This line is now the one on the teleprompter, so it is what
                # the rep could read next. The next practice rep turn is stored
                # with it, which is how the debrief measures the prompter.
                self._suggestion_on_screen = text

    # ================================================================== #
    # practice mode
    # ================================================================== #

    @property
    def _practice(self) -> bool:
        """Report whether this session is a practice call.

        Returns:
            True only when the session was marked by ``POST /api/practice/start``.
            The attribute is read with a default so a live session created
            before practice mode existed still works.
        """
        return str(getattr(self._session, "mode", "live")) == "practice"

    def _difficulty(self) -> str:
        """Return the level this practice call was started at.

        Returns:
            The difficulty key, falling back to the middle level.
        """
        return str(getattr(self._session, "difficulty", "") or DEFAULT_DIFFICULTY)

    def _turn_limit(self) -> int:
        """Return how many client turns this level allows before it ends.

        ``app.practice`` writes this number in client turns only, and the
        persona prompt is built from that same number, so the socket has to
        count the same way. Counting both sides would cut every practice call
        at half the length the client was briefed for.

        Returns:
            The turn limit from the difficulty table, never below two.
        """
        entry = _DIFFICULTY_BY_KEY.get(self._difficulty(), {})
        try:
            limit = int(entry.get("turn_limit", DEFAULT_TURN_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_TURN_LIMIT
        return max(2, limit)

    def _practice_turns(self) -> list[PracticeTurn]:
        """Return the practice transcript, creating it when it is missing.

        Returns:
            The live list held on the session, so appending to it is enough.
        """
        turns = getattr(self._session, "practice_turns", None)
        if turns is None:
            turns = []
            self._session.practice_turns = turns
        return turns

    def _client_turn_count(self) -> int:
        """Count how many lines the fake client has said in this call.

        Returns:
            The number of recorded client turns. This is what the turn limit
            is measured against, never the rep turns.
        """
        return sum(1 for turn in self._practice_turns() if turn.role == "client")

    async def _practice_accepts_audio(self, stream: StreamName) -> bool:
        """Decide whether one practice audio frame may reach the VAD.

        This is deliberate half duplex. While the browser is speaking the
        client line through the speakers, the microphone hears those speakers,
        so the frames are dropped here at the door and never touch the
        segmenter. If they were fed in, the VAD would close an utterance made
        of the fake client's own voice, Whisper would transcribe it, and the
        persona would end up answering itself. The rep can cut in at any time,
        the browser stops speaking, clears the flag, and the mic is live again.

        Args:
            stream: Which stream the frame arrived on.

        Returns:
            True when the frame should be segmented as normal.
        """
        if stream == "client":
            # There is no prospect on the line in practice mode, the client
            # voice is written here and spoken by the browser, so shared tab
            # audio has nothing to carry. Say so once, then stay quiet.
            if not self._client_audio_noticed:
                self._client_audio_noticed = True
                await self._send(
                    {
                        "type": "error",
                        "code": "practice_client_audio",
                        "message": _PRACTICE_CLIENT_AUDIO_NOTICE,
                    }
                )
            return False
        if self._client_voice_playing:
            return False
        # Before the first practice_start, and after practice_over, the mic is
        # simply not part of anything.
        return self._practice_started and not self._practice_over

    async def _handle_practice_start(self) -> None:
        """Start a practice call. The fake client speaks first.

        The opening line is hardcoded per level in ``app.practice``, so the
        call opens with zero latency and zero tokens. It is recorded as a real
        practice turn and it also feeds the copilot, so by the time the rep has
        heard it the teleprompter already holds their answer.
        """
        if not self._practice:
            await self._send(
                {"type": "error", "code": "not_practice", "message": _NOT_PRACTICE_MESSAGE}
            )
            return

        session = self._session
        self._practice_started = True
        self._practice_over = False
        self._client_voice_playing = False
        self._suggestion_on_screen = ""

        # A fresh run, so the previous one is cleared out of both transcripts.
        # The copilot must not see the last call, and the scorecard must not
        # count it.
        self._practice_turns().clear()
        session.turns.clear()
        session.started_at = time.time()
        session.ended_at = 0.0
        session.ended_reason = None

        names: tuple[StreamName, StreamName] = ("client", "rep")
        for name in names:
            self._segmenters[name].reset()
            self._last_speaking[name] = False

        await self._send_practice_state()

        difficulty = self._difficulty()
        text = str(
            practice.opening_line(difficulty, getattr(session, "persona_name", None))
        ).strip()
        if not text:
            await self._send(
                {
                    "type": "error",
                    "code": "internal",
                    "message": "The practice client had no opening line.",
                }
            )
            return

        await self._client_turn(
            text,
            mood=_OPENING_MOOD.get(difficulty, "neutral"),
            intent="question",
            objection="none",
            ms=0,
        )
        log.info("practice call started on session %s at level %s", session.id, difficulty)

        if self._groq.configured:
            await self._start_llm(self._build_messages(), "speech", text, 0)
        else:
            await self._notify_missing_key()

    async def _handle_practice_end(self) -> None:
        """Stop the practice call because the rep asked for their score."""
        if not self._practice:
            await self._send(
                {"type": "error", "code": "not_practice", "message": _NOT_PRACTICE_MESSAGE}
            )
            return
        await self._end_practice("rep_ended")

    async def _handle_speech_state(self, message: dict[str, Any]) -> None:
        """Track whether the browser is speaking the client line out loud.

        The segmenter is reset on both edges. Going into speech it may hold a
        half open utterance that would otherwise be glued to whatever the rep
        says afterwards, and coming out of speech its noise floor was measured
        against the speakers, so both sides start clean.

        Args:
            message: The decoded ``speech_state`` frame, carrying ``speaking``.
        """
        speaking = bool(message.get("speaking", False))
        if speaking == self._client_voice_playing:
            return
        self._client_voice_playing = speaking
        self._segmenters["rep"].reset()
        self._last_speaking["rep"] = False
        await self._emit_vad("rep", False, 0.0, force=True)

    async def _practice_rep_turn(self, text: str, stt_ms: int) -> None:
        """Answer one rep utterance as the client, then prompt the rep again.

        The order matters. The rep turn is recorded first, with the suggestion
        that was on screen while they spoke, because that pair is what the
        scorecard uses to say whether the prompter helped. Then the persona
        writes the client's reply, it is emitted and recorded, and only if the
        call is still alive does the copilot answer that reply, exactly as it
        answers a real prospect on a live call.

        Args:
            text: What the rep just said.
            stt_ms: Measured STT latency, ``0`` for a typed line.
        """
        if not self._practice_started or self._practice_over:
            await self._status("idle")
            return

        self._record_practice_turn("rep", text, suggestion=self._suggestion_on_screen)

        if not self._groq.configured:
            await self._notify_missing_key()
            await self._status("idle")
            return

        await self._send({"type": "status", "state": "thinking"})
        started = time.perf_counter()
        try:
            reply = await practice_engine.next_client_turn(
                self._groq,
                session=self._session,
                rep_said=text,
                # The caller already appended this rep line to the transcript,
                # so the persona sees the whole thread, oldest first. Copilot
                # turns are dropped inside the engine, the client never gets to
                # read what the rep is being fed.
                history=list(self._session.turns),
            )
        except GroqError as exc:
            await self._send(
                {
                    "type": "error",
                    "code": "persona_failed",
                    "message": _plain_groq_error(exc, "The practice client could not answer."),
                }
            )
            await self._status("idle")
            return
        reply_ms = int((time.perf_counter() - started) * 1000)

        # The persona is awaited on the utterance worker while the receive loop
        # keeps reading frames, so "End and score me" can land in the middle of
        # this await. If it did, the call is already scored and this reply must
        # not be spoken, recorded, or answered by the copilot.
        if self._practice_over or not self._practice_started:
            await self._status("idle")
            return

        say, mood, intent, objection = _client_reply_fields(reply)
        if not say:
            await self._send(
                {
                    "type": "error",
                    "code": "persona_empty",
                    "message": "The practice client said nothing. Say your line again.",
                }
            )
            await self._status("idle")
            return

        await self._client_turn(say, mood=mood, intent=intent, objection=objection, ms=reply_ms)
        await self._send_practice_state()

        if intent == "hangup":
            await self._end_practice("hangup")
            return
        if intent == "agree":
            # The client agreed to the next step, which is the whole point of
            # the call, so it ends on the win instead of drifting on.
            await self._end_practice("goal_reached")
            return
        if self._client_turn_count() >= self._turn_limit():
            await self._end_practice("turn_limit")
            return

        await self._start_llm(self._build_messages(), "speech", say, stt_ms)

    async def _client_turn(
        self,
        text: str,
        *,
        mood: str,
        intent: str,
        objection: str,
        ms: int,
    ) -> None:
        """Emit one client line, record it, and put it in the copilot window.

        Args:
            text: What the client says. The browser speaks this out loud.
            mood: ``cold``, ``neutral`` or ``warm``.
            intent: ``question``, ``objection``, ``brushoff``, ``agree`` or
                ``hangup``.
            objection: The objection key, or ``none``.
            ms: How long the persona took, ``0`` for the hardcoded opener.
        """
        mid = self._next_id()
        await self._send(
            {
                "type": "client_turn",
                "id": mid,
                "text": text,
                "mood": mood,
                "intent": intent,
                "objection": objection,
                "ms": ms,
            }
        )
        self._record_practice_turn(
            "client",
            text,
            mood=mood,
            intent=intent,
            objection=objection,
        )
        # The copilot sees a practice client line exactly as it sees a real
        # prospect line, which is what makes the teleprompter behave the same.
        self._session.append("client", text)

    def _record_practice_turn(
        self,
        role: str,
        text: str,
        *,
        mood: str = "neutral",
        intent: str = "question",
        objection: str = "none",
        suggestion: str = "",
    ) -> None:
        """Store one practice turn for the scorecard.

        Args:
            role: ``"rep"`` or ``"client"``.
            text: What was said.
            mood: Client mood, ignored for a rep turn.
            intent: Client intent, ignored for a rep turn.
            objection: Client objection key, ignored for a rep turn.
            suggestion: The teleprompter line that was on screen, only ever set
                for a rep turn.
        """
        # Touch the list first so a session made before practice mode existed
        # still gets one, then let the store append so the practice turn cap is
        # enforced in the one place that owns it.
        self._practice_turns()
        self._session.record_practice_turn(
            PracticeTurn(
                role=role,
                text=text,
                ts=time.time(),
                mood=mood,
                intent=intent,
                objection=objection,
                suggestion_shown=suggestion,
            )
        )

    async def _send_practice_state(self) -> None:
        """Tell the client how many turns are in and whether the call is live."""
        await self._send(
            {
                "type": "practice_state",
                "turns": len(self._practice_turns()),
                "started": self._practice_started and not self._practice_over,
            }
        )

    async def _end_practice(self, reason: str) -> None:
        """Close the practice call once, and tell the client why.

        After this the microphone is ignored until a new ``practice_start``.
        The session keeps its turns so the debrief can score them.

        Args:
            reason: ``hangup``, ``rep_ended``, ``goal_reached`` or ``turn_limit``.
        """
        if self._practice_over:
            return
        self._practice_over = True
        self._practice_started = False
        self._client_voice_playing = False
        self._session.ended_at = time.time()
        self._session.ended_reason = reason

        turns = len(self._practice_turns())
        await self._send(
            {
                "type": "practice_over",
                "reason": reason,
                "turns": turns,
                "message": _PRACTICE_OVER_MESSAGES.get(reason, _PRACTICE_OVER_DEFAULT),
            }
        )
        log.info(
            "practice call ended on session %s, reason=%s, turns=%d",
            self._session.id,
            reason,
            turns,
        )

    # ================================================================== #
    # phone lane
    # ================================================================== #

    @property
    def _call_provider(self) -> str:
        """Return how this call is being placed.

        Returns:
            The provider key, ``"manual"`` for a session made before calling
            existed, which is what every session did before this feature.
        """
        return str(getattr(self._session, "call_provider", "manual") or "manual")

    @property
    def _call_state(self) -> str:
        """Return where the phone call is right now.

        Returns:
            One of the six states in ``session_store.CallState``, ``"idle"``
            when the session carries none.
        """
        return str(getattr(self._session, "call_state", "idle") or "idle")

    @property
    def _phone_owns_audio(self) -> bool:
        """Report whether the phone is the only source of audio right now.

        This is the whole rule in one place. When a provider that forks the
        call to us is live, both lanes are already being filled through
        :meth:`ingest_phone_audio`, so the browser must stop being a source.
        The rep will not think of it themselves: the app has always taught them
        to open the microphone first, and their own voice arrives on the line a
        few hundred milliseconds after it arrives in the room, so the rep lane
        would hold one sentence twice, shuffled. Whisper turns that into mush,
        the mush is kept as rep context, and it then biases every later
        transcription on both lanes.

        Practice is excluded for the same reason :meth:`ingest_phone_audio`
        excludes it. A practice call has no line, nothing can be feeding us, and
        blocking the microphone there would break the rehearsal.

        Returns:
            True while the phone is carrying the sound for this session.
        """
        if self._practice:
            return False
        return (
            self._call_provider in PHONE_AUDIO_PROVIDERS
            and self._call_state in PHONE_AUDIO_STATES
        )

    async def _notify_phone_audio(self) -> None:
        """Tell the browser once that the phone is carrying the sound.

        A capture sends about thirty frames a second, so this can never be sent
        per refused frame. The latch is cleared in
        :meth:`_sync_phone_audio_gate` when the call stops feeding us, so the
        next call says it again.
        """
        if self._phone_audio_noticed:
            return
        self._phone_audio_noticed = True
        await self._send(
            {
                "type": "error",
                "code": "phone_audio_active",
                "message": _PHONE_AUDIO_NOTICE,
            }
        )

    async def _sync_phone_audio_gate(self) -> None:
        """Close both lanes cleanly across the edge where the source changes.

        Called on every call state change. On the way in the segmenters may
        hold half an utterance of microphone audio, and on the way out half an
        utterance of phone audio, and a segmenter simply glues whatever it is
        pushed next onto that tail. Left alone, the rep's last words before the
        call connected would come back stuck to the client's first words on the
        line. So both lanes are flushed on both edges, which sends the half
        utterance to Whisper on its own and starts the new source clean.

        Nothing happens on a provider that does not fork audio to us, and
        nothing happens in practice mode, because the flag never flips there.
        """
        feeding = self._phone_owns_audio
        if feeding == self._phone_feeding:
            return
        self._phone_feeding = feeding
        if not feeding:
            # The line is free again, so the browser is a source once more and
            # the notice is due again on the next call.
            self._phone_audio_noticed = False

        names: tuple[StreamName, StreamName] = ("client", "rep")
        for name in names:
            for event in self._segmenters[name].flush():
                if event.kind == "utterance" and event.pcm:
                    await self._enqueue(name, event.pcm)
            self._last_speaking[name] = False
            await self._emit_vad(name, False, 0.0, force=True)

    # ================================================================== #
    # outbound helpers
    # ================================================================== #

    async def _emit_vad(
        self,
        stream: StreamName,
        speaking: bool,
        rms: float,
        *,
        force: bool = False,
    ) -> None:
        """Send a level update, throttled so it cannot flood the socket.

        A change in the speaking flag always goes out immediately because the
        UI drives its glow off it, plain level updates are capped at one per
        80 ms per stream.

        Args:
            stream: Which stream the level belongs to.
            speaking: Whether the segmenter currently considers this speech.
            rms: Normalized level, 0.0 to 1.0.
            force: Send regardless of the throttle.
        """
        now = time.monotonic()
        flipped = self._last_speaking[stream] != speaking
        if not force and not flipped and (now - self._last_vad_at[stream]) < VAD_MIN_INTERVAL_S:
            return
        self._last_speaking[stream] = speaking
        self._last_vad_at[stream] = now
        await self._send(
            {
                "type": "vad",
                "stream": stream,
                "speaking": speaking,
                "rms": round(float(rms), 4),
            }
        )

    def _llm_in_flight(self) -> bool:
        """Report whether a suggestion is currently streaming.

        Returns:
            True while an LLM task exists and has not finished.
        """
        task = self._llm_task
        return task is not None and not task.done()

    async def _status(self, state: str, detail: str | None = None) -> None:
        """Emit a status update unless it would contradict a live suggestion.

        The utterance worker and the receive loop both report status on the
        same socket while the LLM task streams on a third. Without this guard a
        rep utterance closing mid stream sends ``idle`` while tokens are still
        painting, so the status pill drops to idle and jumps back to thinking a
        moment later, which happens on essentially every suggestion because the
        rep reads it aloud while it streams. ``thinking`` always goes out, it is
        the only state the LLM task itself owns.

        Args:
            state: One of the wire contract's status states.
            detail: Optional free text detail carried alongside.
        """
        if state != "thinking" and self._llm_in_flight():
            return
        payload: dict[str, Any] = {"type": "status", "state": state}
        if detail is not None:
            payload["detail"] = detail
        await self._send(payload)

    async def _notify_missing_key(self, *, force: bool = False) -> None:
        """Tell the client that Groq is not configured, without spamming.

        Args:
            force: Send even if the notice was just sent.
        """
        now = time.monotonic()
        if not force and (now - self._last_no_key_notice) < NO_KEY_NOTICE_INTERVAL_S:
            return
        self._last_no_key_notice = now
        await self._send({"type": "error", "code": "no_api_key", "message": _NO_KEY_MESSAGE})

    async def send_call_state(self, payload: Mapping[str, Any]) -> None:
        """Push one phone call state change down to the browser.

        Public because the phone routes reach it through
        :func:`notify_call_state`. It writes through the same send lock as every
        other message, so it can never interleave with a streaming suggestion,
        and it is not gated by :meth:`_status`: call state is not copilot state,
        so a suggestion in flight must not hide the fact that the line dropped.

        This is also the moment the audio source can change hands, so the lane
        gate is resynced right after the frame goes out. The gate itself reads
        the session, not this payload, so a caller that changed the session and
        then sent a different payload cannot desync the two.

        Args:
            payload: The call fields, normally ``provider``, ``state``,
                ``callId`` and ``detail``. A ``type`` key inside it is ignored,
                this message is always ``call_state``.
        """
        message: dict[str, Any] = {"type": "call_state"}
        for key, value in payload.items():
            if key == "type":
                continue
            message[str(key)] = value
        await self._send(message)
        await self._sync_phone_audio_gate()

    async def _send(self, payload: dict[str, Any]) -> None:
        """Serialize and send one JSON message, serialized by a lock.

        The receive loop, the utterance worker and the LLM task all write to
        the same socket, so every write goes through one lock. A send after
        the peer went away is logged and swallowed, it is not an error worth
        tearing anything down for.

        Args:
            payload: The message body, must carry a ``type`` key.
        """
        if self._closed:
            return
        async with self._send_lock:
            try:
                await self._ws.send_json(payload)
            except (WebSocketDisconnect, RuntimeError) as exc:
                self._closed = True
                log.debug("send after close on session %s: %s", self._session.id, exc)
            except Exception as exc:  # noqa: BLE001 - a dead socket is not a crash.
                self._closed = True
                log.debug("send failed on session %s: %s", self._session.id, exc)

    def _next_id(self) -> str:
        """Return the next monotonically increasing message id for this session.

        Returns:
            The id as a string, as the wire contract requires.
        """
        self._id_counter += 1
        if len(_ID_COUNTERS) > _ID_COUNTER_CAP and self._session.id not in _ID_COUNTERS:
            # Long lived process housekeeping. Ids only have to be unique
            # inside one live UI, so starting over is harmless.
            _ID_COUNTERS.clear()
        _ID_COUNTERS[self._session.id] = self._id_counter
        return str(self._id_counter)


# ====================================================================== #
# the doors other modules use
# ====================================================================== #


def teleprompter_attached(session_id: str) -> bool:
    """Report whether a browser is watching this session right now.

    The phone routes use this to stay honest. If nobody is connected there is
    no point telling the rep that the teleprompter is following the call.

    Args:
        session_id: The session to look up.

    Returns:
        True when a teleprompter socket is open for that session.
    """
    return session_id in _CONNECTIONS


async def notify_call_state(session_id: str, payload: Mapping[str, Any]) -> None:
    """Tell the browser that the phone call changed state.

    Sent as ``{"type": "call_state", ...}`` with the caller's fields merged in,
    normally ``provider``, ``state``, ``callId`` and ``detail``. This is a
    fire and forget helper for the phone routes, so it does two things very
    deliberately: it is a plain no op when no browser is connected, and it never
    lets an error out. A webhook from a carrier must not fail because the rep
    closed their tab.

    Args:
        session_id: Which session the phone call belongs to.
        payload: The call fields to send.
    """
    connection = _CONNECTIONS.get(session_id)
    if connection is None:
        return
    try:
        await connection.send_call_state(payload)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a dead socket must not break a webhook.
        log.debug("call_state push failed on session %s", session_id, exc_info=True)


async def feed_call_audio(session_id: str, stream: StreamName, pcm: bytes) -> bool:
    """Push one frame of phone audio into a session's live pipeline.

    The phone routes hold no reference to the socket, they only know the session
    id, so this looks the connection up and hands the frame to
    :meth:`TeleprompterConnection.ingest_phone_audio`. Everything downstream is
    the browser's pipeline, unchanged.

    ``stream`` is our lane name and the caller has already mapped the carrier's
    track name onto it. See :meth:`TeleprompterConnection.ingest_phone_audio`
    for why that mapping is not obvious.

    Args:
        session_id: Which session this audio belongs to.
        stream: ``"client"`` or ``"rep"``, our lane.
        pcm: Int16 LE mono PCM at 16 kHz, already decoded from mu-law.

    Returns:
        True when the audio reached the VAD, False when it was dropped. The
        common false is simply that no browser is connected yet, which is not an
        error, so the caller should count it rather than raise on it.
    """
    connection = _CONNECTIONS.get(session_id)
    if connection is None:
        return False
    try:
        return await connection.ingest_phone_audio(stream, pcm)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a dead socket must not break the media stream.
        log.debug("phone audio push failed on session %s", session_id, exc_info=True)
        return False


@router.websocket("/ws/teleprompter")
async def teleprompter(websocket: WebSocket, session_id: str = Query(default="")) -> None:
    """Serve the realtime teleprompter socket for one call session.

    An unknown or expired ``session_id`` is accepted first (a socket has to
    be accepted before anything can be sent on it), told why, and closed with
    application code 4404.

    Args:
        websocket: The incoming socket.
        session_id: The uuid4 handed out by ``POST /api/prepare-context``.
    """
    session = store.get(session_id) if session_id else None
    if session is None:
        await websocket.accept()
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "unknown_session",
                    "message": "That session is unknown or has expired. Build the call context again.",
                }
            )
        with contextlib.suppress(Exception):
            await websocket.close(code=4404)
        return

    groq: GroqClient = websocket.app.state.groq
    connection = TeleprompterConnection(websocket, session, groq, settings)
    await connection.run()
