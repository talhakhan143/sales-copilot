"""Twilio's own routes: the two webhooks and the Media Streams socket.

Nothing here is called by our frontend. Twilio calls all three of them, from the
public internet, while a real person is on the line. That single fact drives
every decision in this module:

* **Never answer 500.** A 500 makes Twilio play "an application error has
  occurred" into the client's ear and hang up. So an unknown session gets TwiML
  that says one plain line and hangs up politely, and a malformed media frame is
  counted and dropped rather than raised.
* **All three URLs are public, so all three carry a token.** ``?t=`` holds
  ``hmac_sha256(CALL_WEBHOOK_SECRET, session_id)``, checked with
  ``twilio_client.verify_token``. The two webhooks check it before they answer.
  The media socket checks it on the ``start`` frame, because that frame is where
  the session id it belongs to finally arrives. This is a light guard against a
  stranger who knows a session id, not proof the request came from Twilio.
  Validating the ``X-Twilio-Signature`` header against the full URL and body is
  the production upgrade.
* **The audio pipeline is not duplicated.** Frames are decoded from mu-law to
  the same 16 kHz Int16 PCM the browser worklet produces and handed to
  ``routes_ws.feed_call_audio``, which pushes them into the very same VAD, the
  same utterance queue, the same Whisper worker and the same copilot task the
  browser drives. This module owns the transcode and the lane mapping, and
  nothing else.

The session id is not a secret. It rides in the call page address bar and in the
teleprompter socket query string, and reps share their screen. So the token is
what stands between a stranger who read that id off a screen share and a socket
that would push their voice into the rep's live teleprompter as if it were the
prospect.

The lane mapping is the one thing that is easy to get backwards, and it is
written out in full at :data:`TRACK_TO_STREAM`.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any
from urllib.parse import quote

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)

from app.api.routes_ws import (
    StreamName,
    feed_call_audio,
    notify_call_state,
    teleprompter_attached,
)
from app.config import public_wss_base
from app.services import twilio_client
from app.services.session_store import CallState, Session, store
from app.services.telephony_audio import Upsampler, twilio_frame_to_pcm16k

log = logging.getLogger("salescopilot.api.twilio")

router = APIRouter(tags=["twilio"])


# ====================================================================== #
# constants
# ====================================================================== #

MEDIA_PATH: str = "/ws/twilio"
"""Path Twilio opens the media socket on. Appended to the public wss base."""

XML_MEDIA_TYPE: str = "application/xml"
"""Content type every TwiML answer carries."""

BAD_TOKEN_MESSAGE: str = "That link is not valid."
"""Answer for a webhook call with a missing or wrong token."""

POLICY_CLOSE_CODE: int = 1008
"""WebSocket close code for a socket we refuse on purpose.

1008 is "policy violation". A refused socket is not a network problem and not
Twilio's fault, and the code says so, which is what a log on the other side
needs to read.
"""

FAILED_TWIML: str = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<Response>"
    '<Say voice="alice">Sorry. This call could not be set up. Goodbye.</Say>'
    "<Hangup/>"
    "</Response>"
)
"""TwiML for a call we cannot bridge.

A real person is holding a ringing phone when this is served, so it says one
short line and hangs up. Answering an error instead would make Twilio play its
own robot apology, which tells the rep nothing and sounds broken to the client.
"""

TRACK_TO_STREAM: dict[str, StreamName] = {"outbound": "client", "inbound": "rep"}
"""Twilio track names mapped onto our two lanes. Read this before changing it.

Twilio names a track from the point of view of the call leg it forked, and that
leg is the REP's phone, because ``place_call`` rings the rep first and only then
dials the client. So on that leg:

* ``inbound`` is audio coming FROM the rep's phone, which is the REP talking,
  our lane 1.
* ``outbound`` is audio going TO the rep's phone, which is everything the rep
  hears, and that is the CLIENT talking, our lane 0.

The words therefore point the opposite way to the first guess. Getting it
backwards does not crash anything, it quietly makes the copilot answer the rep's
own voice and ignore the prospect, which is the single worst failure this
feature can have. That is why the mapping is one table with this comment on it.
"""

CLIENT_WARMUP_FRAMES: int = 100
"""Client lane frames dropped at the start of a call. 20 ms each, so 2 seconds.

"Everything the rep hears" includes the things this app plays at them, and none
of that is the client. Two of them land on this lane:

* Twilio's own voice reading our ``<Say>`` line. ``build_twiml`` already speaks
  that line before it starts the fork, so it is out of the audio for good. This
  window is the second layer under that, for the day somebody moves the element.
* The dial click and the ringing tone, while the client's phone is ringing.
  There is no way to say "start the fork when the other side answers" in TwiML,
  so the tone reaches this lane, the VAD cuts it into an utterance and Whisper
  writes something into the client column that nobody said.

Nothing real is lost. The window starts when the fork starts, which is when the
dial starts, and a phone cannot be reached, rung and picked up inside two
seconds, so no human can be speaking on this lane while it runs.

A longer window would be the wrong trade. The tone keeps playing for as long as
the phone rings, which can be thirty seconds, and no fixed window can cover that
without eating the client's first word. The rest of the tone is caught
downstream by the transcript blocklist, which is the layer that can judge a
transcript rather than a clock.
"""

TWILIO_CALL_STATES: dict[str, CallState] = {
    "queued": "dialing",
    "initiated": "dialing",
    "ringing": "ringing",
    "answered": "live",
    "in-progress": "live",
    "completed": "ended",
    "canceled": "ended",
    "busy": "failed",
    "failed": "failed",
    "no-answer": "failed",
}
"""Twilio's own call status words mapped onto our six states.

Twilio sends more words than the four events we subscribe to, so all of them are
here. A word that is not in this table leaves the state alone, because guessing
would move the chip on the rep's screen for no reason.
"""

TWILIO_CALL_DETAILS: dict[str, str] = {
    "queued": "The call is waiting to go out.",
    "initiated": "Calling your phone.",
    "ringing": "Your phone is ringing.",
    "answered": "The call is on.",
    "in-progress": "The call is on.",
    "completed": "The call is over.",
    "canceled": "The call was stopped before it started.",
    "busy": "The line was busy.",
    "failed": "The call did not go through.",
    "no-answer": "Nobody picked up.",
}
"""One plain line per Twilio status, shown under the state chip."""

LIVE_ON_MEDIA: frozenset[str] = frozenset({"idle", "dialing", "ringing"})
"""States the media stream is allowed to move on to ``live``.

``idle`` is in the set because audio can arrive for a call this process never
started, which is what happens when the backend restarts while a call is up.
``ended`` and ``failed`` are not, so a late frame can never bring a dead call
back to life on the rep's screen.
"""

MEDIA_ON_DETAIL: str = "The app can hear the call."
"""Detail written when audio actually starts arriving."""

MEDIA_OFF_DETAIL: str = "The call is over."
"""Detail written when Twilio stops the media stream."""

NO_LISTENER_NOTICE: str = (
    "Phone audio is arriving but no teleprompter is open for session %s. "
    "Open the call page for that session to see the words."
)
"""Logged once per socket when the audio has nowhere to go."""

MAX_TEXT_FRAME_CHARS: int = 32768
"""Ceiling on one Twilio frame.

A media frame is about 400 characters. Anything near this size is not something
Twilio sent, so it is dropped before it is decoded rather than parsed.
"""


# ====================================================================== #
# webhooks
# ====================================================================== #


def _guard(session_id: str, token: str) -> None:
    """Refuse a webhook that does not carry this session's token.

    Args:
        session_id: The session id taken from the URL path.
        token: The ``t`` query parameter, possibly empty.

    Raises:
        HTTPException: 403 when the token is missing or wrong.
    """
    if not twilio_client.verify_token(session_id, token):
        log.warning("twilio webhook refused for session %s, bad token", session_id)
        raise HTTPException(status_code=403, detail=BAD_TOKEN_MESSAGE)


def _media_url(request: Request, session_id: str) -> str:
    """Work out the WebSocket URL to put in the TwiML.

    ``PUBLIC_BASE_URL`` wins, because that is the address the rep configured and
    the address the call was placed against. When it is empty the address Twilio
    actually used to reach this request is the next best answer, and it is often
    the more correct one: behind a tunnel the Host header carries the tunnel's
    own name. That fallback is also what lets this webhook be exercised on a
    laptop, where a real Twilio call could never arrive in the first place.

    The URL carries the same ``?t=`` token as the two webhooks. Twilio hands it
    straight back to us when it opens the socket, and the socket checks it on
    the start frame. Without it, anyone who knows a session id could open the
    socket and feed their own voice into the rep's live teleprompter.

    Args:
        request: The incoming webhook request.
        session_id: The session this stream will belong to, which is what the
            token is computed over.

    Returns:
        A full ``wss://`` or ``ws://`` URL ending in the media path and the
        token, or ``""`` when there is no host to build one from.
    """
    query = f"?t={quote(twilio_client.webhook_token(session_id), safe='')}"
    base = public_wss_base()
    if base:
        return f"{base}{MEDIA_PATH}{query}"
    host = request.url.netloc
    if not host:
        return ""
    scheme = "wss" if request.url.scheme in ("https", "wss") else "ws"
    return f"{scheme}://{host}{MEDIA_PATH}{query}"


@router.post("/twilio/voice/{session_id}")
async def twilio_voice(
    session_id: str,
    request: Request,
    t: str = Query(default=""),
) -> Response:
    """Hand Twilio the TwiML that forks the audio and dials the client.

    Twilio fetches this the moment the rep picks up their phone. The document it
    gets starts the media fork, says one short line to cover the dial, and then
    rings the client.

    A session we do not know, or one with no client number stored, cannot be
    bridged. That answers 200 with the polite hangup document rather than an
    error, because the rep has the phone against their ear right now and a 500
    would only play a robot apology at them.

    Args:
        session_id: The session the call belongs to, from the URL path.
        request: The incoming request, read for the address Twilio reached us on.
        t: The webhook token this URL was built with.

    Returns:
        A TwiML document with the ``application/xml`` content type.

    Raises:
        HTTPException: 403 when the token is missing or wrong.
    """
    _guard(session_id, t)

    session = store.get(session_id)
    if session is None:
        log.warning("twilio voice webhook for unknown session %s", session_id)
        return Response(content=FAILED_TWIML, media_type=XML_MEDIA_TYPE)

    to_number = (session.to_number or "").strip()
    if not to_number:
        log.warning("twilio voice webhook for session %s has no client number", session_id)
        return Response(content=FAILED_TWIML, media_type=XML_MEDIA_TYPE)

    media_url = _media_url(request, session_id)
    if not media_url:
        log.warning("twilio voice webhook for session %s has no media address", session_id)
        return Response(content=FAILED_TWIML, media_type=XML_MEDIA_TYPE)

    twiml = twilio_client.build_twiml(
        session_id=session_id,
        to_number=to_number,
        wss_url=media_url,
    )
    log.info("twilio voice webhook served for session %s", session_id)
    return Response(content=twiml, media_type=XML_MEDIA_TYPE)


@router.post("/twilio/status/{session_id}", status_code=204)
async def twilio_status(
    session_id: str,
    request: Request,
    t: str = Query(default=""),
) -> Response:
    """Record a Twilio call status callback and push it to the browser.

    The body is read as a form rather than through typed parameters on purpose.
    Twilio sends more than a dozen fields, they change between API versions, and
    a missing one must never turn into a 422 on a live call.

    Args:
        session_id: The session the call belongs to, from the URL path.
        request: The incoming request, read for its form body.
        t: The webhook token this URL was built with.

    Returns:
        An empty 204. Twilio ignores the body of a status callback.

    Raises:
        HTTPException: 403 when the token is missing or wrong.
    """
    _guard(session_id, t)
    empty = Response(status_code=204)

    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 - a torn body must not fail the callback.
        log.exception("twilio status callback body could not be read")
        return empty

    raw_status = str(form.get("CallStatus", "") or "").strip().lower()
    call_sid = str(form.get("CallSid", "") or "").strip()

    session = store.get(session_id)
    if session is None:
        log.info("twilio status %r for unknown session %s", raw_status, session_id)
        return empty

    state = TWILIO_CALL_STATES.get(raw_status)
    if state is None:
        log.info("twilio status %r is not one we map, session %s", raw_status, session_id)
        return empty

    session.set_call_state(
        state,
        call_id=call_sid or None,
        detail=TWILIO_CALL_DETAILS.get(raw_status, ""),
    )
    await notify_call_state(session.id, session.call_snapshot())
    log.info("session %s call is now %s (twilio said %r)", session_id, state, raw_status)
    return empty


# ====================================================================== #
# media stream
# ====================================================================== #


class TwilioMediaStream:
    """One Twilio Media Streams socket, carrying both sides of one phone call.

    The socket outlives nothing: it opens when the TwiML runs and closes when
    the call ends. All it does is decode frames and hand them to the session's
    live teleprompter pipeline, so it holds no VAD, no queue and no model call
    of its own.

    Every handler is wrapped. A malformed frame is counted and dropped, because
    a socket that dies mid call takes the rep's live transcript with it.
    """

    def __init__(self, ws: WebSocket, token: str = "") -> None:
        """Wire the stream up without touching the socket yet.

        Args:
            ws: The incoming, not yet accepted, WebSocket.
            token: The ``t`` query parameter the socket was opened with. It is
                checked against the session named in the start frame, which is
                the first moment we know which session to check it against.
        """
        self._ws = ws
        self._token = token
        self._session: Session | None = None
        self._stream_sid = ""
        self._call_sid = ""
        # One resampler per Twilio TRACK, not per lane. Each one carries the
        # last sample of the previous frame across the 20 ms boundary, and the
        # two tracks are two different voices, so sharing one instance would
        # interpolate each voice against the other one.
        self._upsamplers: dict[str, Upsampler] = {}
        self._frames = 0
        self._dropped = 0
        self._bad = 0
        self._skipped = 0
        self._client_warmup = CLIENT_WARMUP_FRAMES
        self._closed = False
        self._warned_no_listener = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Accept the socket, serve it, and always log what it carried."""
        await self._ws.accept()
        try:
            await self._recv_loop()
        except WebSocketDisconnect:
            log.info("twilio media socket disconnected, stream=%s", self._stream_sid or "none")
        except Exception:  # noqa: BLE001 - never let one call take the app down.
            log.exception("twilio media socket failed, stream=%s", self._stream_sid or "none")
        finally:
            await self._close()
            log.info(
                "twilio media socket closed, session=%s frames=%d skipped=%d dropped=%d bad=%d",
                self._session.id if self._session else "none",
                self._frames,
                self._skipped,
                self._dropped,
                self._bad,
            )

    async def _recv_loop(self) -> None:
        """Read Twilio frames until the peer goes away or we close.

        ``WebSocket.receive`` hands back the raw ASGI message, which is how the
        disconnect is seen without a second read path. Twilio only ever sends
        text, so a binary frame is ignored rather than decoded.
        """
        while not self._closed:
            message = await self._ws.receive()
            kind = message.get("type")
            if kind == "websocket.disconnect":
                break
            if kind != "websocket.receive":
                continue
            raw = message.get("text")
            if raw is None:
                continue
            await self._handle_text(raw)

    async def _close(self, code: int = 1000) -> None:
        """Close the socket once, quietly.

        Args:
            code: The WebSocket close code. 1000 is a normal close, which is
                what an unknown session gets: it is not Twilio's fault, and an
                error code would only make it retry. A socket we refuse on
                purpose gets :data:`POLICY_CLOSE_CODE` instead.
        """
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._ws.close(code=code)

    # ------------------------------------------------------------------ #
    # frames
    # ------------------------------------------------------------------ #

    async def _handle_text(self, raw: str) -> None:
        """Decode and dispatch one Twilio frame.

        The decode is guarded broadly on purpose. A deeply nested payload makes
        the JSON scanner raise ``RecursionError``, which is not a ``ValueError``,
        so a narrow guard would let one bad frame end a live call.

        Args:
            raw: The text payload exactly as received.
        """
        if len(raw) > MAX_TEXT_FRAME_CHARS:
            self._bad += 1
            return

        try:
            message = json.loads(raw)
        except Exception:  # noqa: BLE001 - RecursionError is not a ValueError.
            self._bad += 1
            return
        if not isinstance(message, dict):
            self._bad += 1
            return

        event = str(message.get("event", ""))
        try:
            if event == "media":
                await self._on_media(message)
            elif event == "start":
                await self._on_start(message)
            elif event == "stop":
                await self._on_stop()
            elif event == "connected":
                log.info("twilio media socket connected")
            # mark and dtmf are real Twilio events we have no use for. They are
            # dropped without a word, because logging every one of them on a
            # long call is noise.
        except Exception:  # noqa: BLE001 - one frame, not the call.
            self._bad += 1
            log.exception("twilio %r frame failed, stream=%s", event, self._stream_sid or "none")

    async def _on_start(self, message: dict[str, Any]) -> None:
        """Check the token, then attach this socket to the session it names.

        The token is checked here and not at the handshake because the session
        id it is computed over arrives in this frame. Our own TwiML is what put
        that token in the URL, so a real Twilio stream always carries it.

        Args:
            message: The decoded ``start`` frame.
        """
        start = message.get("start")
        start_data: dict[str, Any] = start if isinstance(start, dict) else {}

        self._stream_sid = str(message.get("streamSid") or start_data.get("streamSid") or "")
        self._call_sid = str(start_data.get("callSid") or "")

        session_id = _session_id_from(start_data)
        if not twilio_client.verify_token(session_id, self._token):
            # The session id is not a secret, the token is. Without this check
            # anyone who read a session id off a shared screen could open this
            # socket and push their own voice in as the prospect.
            log.warning(
                "twilio media stream %s refused for session %r, bad token",
                self._stream_sid or "none",
                session_id,
            )
            await self._close(POLICY_CLOSE_CODE)
            return

        session = store.get(session_id) if session_id else None
        if session is None:
            log.warning(
                "twilio media stream %s named session %r, which is unknown",
                self._stream_sid or "none",
                session_id,
            )
            await self._close()
            return

        self._session = session
        log.info(
            "twilio media stream %s attached to session %s, call %s",
            self._stream_sid or "none",
            session.id,
            self._call_sid or "none",
        )
        if not teleprompter_attached(session.id):
            self._warned_no_listener = True
            log.warning(NO_LISTENER_NOTICE, session.id)

        if session.call_state in LIVE_ON_MEDIA:
            # Audio is flowing, which means the rep picked up. The status
            # callback says the same thing a moment later, and whichever one
            # arrives first is right.
            session.set_call_state(
                "live",
                call_id=self._call_sid or None,
                detail=MEDIA_ON_DETAIL,
            )
            await notify_call_state(session.id, session.call_snapshot())

    async def _on_media(self, message: dict[str, Any]) -> None:
        """Decode one 20 ms frame and push it into the session's pipeline.

        Args:
            message: The decoded ``media`` frame.
        """
        session = self._session
        if session is None:
            # Twilio always sends start first. A media frame before it means the
            # start frame was lost, malformed or refused, and there is no
            # session to attribute this audio to.
            self._bad += 1
            return

        media = message.get("media")
        if not isinstance(media, dict):
            self._bad += 1
            return

        track = str(media.get("track", "")).strip().lower()
        stream = TRACK_TO_STREAM.get(track)
        if stream is None:
            self._bad += 1
            return

        if stream == "client" and self._client_warmup > 0:
            # The client's phone is still ringing, so this lane is carrying the
            # ringing tone and nothing else, see CLIENT_WARMUP_FRAMES. Dropped,
            # not transcribed, so the copilot never answers a noise.
            self._client_warmup -= 1
            self._skipped += 1
            return

        payload = media.get("payload")
        if not isinstance(payload, str) or not payload:
            self._bad += 1
            return

        upsampler = self._upsamplers.get(track)
        if upsampler is None:
            upsampler = Upsampler()
            self._upsamplers[track] = upsampler

        # base64 to mu-law to 8 kHz PCM to 16 kHz PCM, in one call. A torn
        # payload comes back empty rather than raising, so it is counted here.
        pcm = twilio_frame_to_pcm16k(payload, upsampler)
        if not pcm:
            self._bad += 1
            return

        self._frames += 1
        if await feed_call_audio(session.id, stream, pcm):
            return

        self._dropped += 1
        if not self._warned_no_listener:
            self._warned_no_listener = True
            log.warning(NO_LISTENER_NOTICE, session.id)

    async def _on_stop(self) -> None:
        """Mark the call finished when Twilio stops the media stream."""
        session = self._session
        if session is not None and session.call_state == "live":
            session.set_call_state("ended", call_id=None, detail=MEDIA_OFF_DETAIL)
            await notify_call_state(session.id, session.call_snapshot())
        await self._close()


def _session_id_from(start_data: dict[str, Any]) -> str:
    """Read ``sessionId`` out of a start frame's custom parameters.

    The name is matched without case, because the parameter travels from our
    TwiML through Twilio and back, and a carrier that lower cases the key would
    otherwise silently detach every call from its session.

    Args:
        start_data: The ``start`` object from the frame.

    Returns:
        The session id, or ``""`` when the frame carries none.
    """
    params = start_data.get("customParameters")
    if not isinstance(params, dict):
        return ""
    for key, value in params.items():
        if str(key).strip().lower() == "sessionid":
            return str(value or "").strip()
    return ""


@router.websocket(MEDIA_PATH)
async def twilio_media(websocket: WebSocket, t: str = Query(default="")) -> None:
    """Serve the Twilio Media Streams socket for one phone call.

    Args:
        websocket: The incoming socket. Twilio opens it from the URL that our
            own TwiML put in the ``<Stream>`` element.
        t: The token that URL carried. It is checked against the session named
            in the start frame, and the socket is closed when it does not match.
    """
    await TwilioMediaStream(websocket, token=t).run()


__all__ = ["router", "TRACK_TO_STREAM", "TWILIO_CALL_STATES"]
