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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any, Literal

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.config import Settings, settings
from app.prompts import QUICK_ACTIONS, quick_action_prompt
from app.services.audio import SAMPLE_RATE, VadConfig, VadSegmenter, wav_bytes
from app.services.groq_client import GroqClient, GroqError
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

#: Message ids must be monotonic per session, and a session outlives its
#: socket (a reconnect resumes the same call), so the counter lives here
#: keyed by session id instead of on the connection object.
_ID_COUNTERS: dict[str, int] = {}

#: Hard cap so a long lived server cannot accumulate counters forever.
_ID_COUNTER_CAP = 1024

_QUICK_ACTION_BY_KEY: dict[str, dict[str, Any]] = {
    str(action["key"]): dict(action) for action in QUICK_ACTIONS
}

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

    # ================================================================== #
    # lifecycle
    # ================================================================== #

    async def run(self) -> None:
        """Accept the socket, serve it, and always clean up afterwards.

        The receive loop runs on the calling task while a second task drains
        the utterance queue. Whatever happens, the finally block cancels both
        the worker and the LLM task and empties the queue.
        """
        await self._ws.accept()
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
        """
        self._closed = True
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
            "session %s socket closed, dropped %d pending utterances, %d overruns",
            self._session.id,
            drained,
            self._overruns,
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

            pcm = data[1:]
            if len(pcm) % 2:
                # An odd byte count would shift every following sample by one
                # byte and turn the audio into noise, so drop the stray byte.
                pcm = pcm[:-1]
            if not pcm:
                return

            segmenter = self._segmenters[stream]
            events = segmenter.push(pcm)
            await self._emit_vad(stream, segmenter.speaking, segmenter.last_rms)

            for event in events:
                if event.kind == "utterance" and event.pcm:
                    await self._enqueue(stream, event.pcm)
                elif event.kind == "speech_start" and stream == "client":
                    await self._status("listening")
        except Exception as exc:  # noqa: BLE001 - one bad frame must not close the call.
            log.exception("binary frame failed on session %s", self._session.id)
            await self._send({"type": "error", "code": "internal", "message": str(exc)})

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
        the autoSuggest flag.

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

        if stream != "client" or not self._auto_suggest:
            await self._status("idle")
            return
        if not self._groq.configured:
            await self._notify_missing_key()
            return
        await self._start_llm(self._build_messages(), "manual", text, 0)

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
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._session.system_prompt}
        ]
        messages.extend(self._session.recent_messages(self._settings.transcript_window_turns))
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
