"""Energy based voice activity detection and WAV framing for the copilot.

This module is pure, dependency free and unit testable. It never touches the
network, never touches FastAPI and never blocks, so the WebSocket layer can call
it straight from the receive loop.

WHY THIS EXISTS
---------------
Groq has no streaming websocket STT. It only exposes a REST transcription
endpoint that wants a complete, self contained audio file. So "streaming STT" is
really "cut the microphone stream into utterances fast and accurately, then POST
each one". Everything about the perceived latency of the product depends on how
quickly and how cleanly this file decides that a person stopped talking.

THE ALGORITHM
-------------
The input is raw 16 kHz mono Int16 little endian PCM, pushed in arbitrarily
sized chunks (the browser worklet sends about 20 ms, a slow network can coalesce
several frames into one 200 ms delivery). Nothing here assumes a fixed frame
size: every timing decision converts bytes to milliseconds with
`bytes / (SAMPLE_RATE * 2) * 1000`, so 20 ms chunks and 200 ms chunks behave the
same.

1. For each chunk compute a normalised RMS in 0.0 to 1.0.
2. While NOT speaking, an EMA tracks the room noise floor,
   `floor = 0.95 * floor + 0.05 * rms`, clamped to [0.0008, 0.08]. Updating the
   floor only during silence is what makes this adaptive without the speaker's
   own voice dragging the threshold up until the detector goes deaf. The weight
   drops to 0.005 for a chunk that is already above the open threshold, since
   those chunks are the first moments of a word rather than room noise. See
   NOISE_FLOOR_ALPHA_LOUD, that one detail is worth a whole utterance.
3. `open_threshold = max(floor * 3.2 / sensitivity, 0.008 / sensitivity)`. The
   absolute term is the guard for a pathologically quiet room, where a purely
   relative threshold would trigger on the sound of a fan. `close_threshold` is
   0.55 of the open threshold, and that hysteresis gap is what stops a detector
   from chattering open and shut on every syllable boundary.
4. Speech opens once enough consecutive above threshold audio has arrived, and
   closes after `silence_ms` of continuous below threshold audio.
5. The emitted utterance is `pre roll + speech + 120 ms tail`. A segment that
   is force split at `max_utterance_ms` is the one exception: the speaker has
   not stopped, so it is emitted whole and the silence timer keeps running
   across the boundary instead of restarting.

LATENCY BUDGET
--------------
The endpoint silence is 620 ms and that number is the single biggest fixed cost
in the whole pipeline, so it is worth stating why.

    620 ms  endpoint silence (this file)
    +  80 ms WAV framing, queue hop and TLS to Groq
    + 250 to 500 ms  whisper-large-v3-turbo on a short utterance
    + 300 to 600 ms  llama-3.3-70b first token
    =========================================
    about 1.3 to 1.8 s from "prospect stops talking" to "first word on screen"

Below roughly 450 ms the segmenter starts cutting people off mid sentence,
because a human thinking pause inside one sentence ("we already have, uh, a
vendor for that") is routinely 300 to 400 ms. Every one of those false endpoints
costs a whole extra STT round trip and, worse, hands the LLM half a sentence.
Above roughly 800 ms the rep is left staring at a dead screen and starts talking
over the copilot. 620 ms sits just past the natural pause distribution while
still feeling immediate.

The pre roll ring buffer is the other half of that trade. Energy VAD is always
late: by the time two frames have cleared the threshold, the attack of the first
word is already gone, and Whisper transcribing "ready have a vendor" instead of
"we already have a vendor" is a real accuracy loss. So the last
`preroll_ms` (320 ms by default) of pre speech audio is kept in a ring buffer and
prepended to the segment. The 120 ms tail at the end does the same job for the
final consonant, which the close threshold clips off.

Everything here is O(bytes pushed) with no per chunk concatenation. Chunks are
held by reference in a deque and joined exactly once, when a segment closes.
"""

from __future__ import annotations

import array
import math
import struct
import sys
from collections import deque
from dataclasses import dataclass
from typing import Literal

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2

#: Bytes of 16 kHz mono Int16 audio per millisecond, 32.0. Kept as a float so
#: the timing maths never silently truncates on a short chunk.
BYTES_PER_MS = SAMPLE_RATE * BYTES_PER_SAMPLE / 1000.0

#: Audio kept after the close threshold is crossed, so trailing consonants and
#: the end of a question are not clipped.
TAIL_MS = 120

#: The contract says "speech opens after 2 consecutive frames over
#: open_threshold". Frame size is not fixed here (a caller may push 20 ms or
#: 200 ms), so the rule is implemented as its time equivalent instead: speech
#: opens once 40 ms of consecutive above threshold audio has accumulated, which
#: is exactly 2 frames at the 20 ms frame size the browser worklet emits. One
#: below threshold chunk resets the accumulator to zero, keeping the
#: "consecutive" part of the rule intact.
#:
#: The audio that does the confirming is real speech and is credited as voiced,
#: not as pre roll. See :meth:`VadSegmenter._open`.
OPEN_CONFIRM_MS = 40.0

NOISE_FLOOR_INITIAL = 0.004
NOISE_FLOOR_MIN = 0.0008
NOISE_FLOOR_MAX = 0.08

#: EMA weight for a chunk that sits below the open threshold, which is the
#: contract formula `floor = 0.95 * floor + 0.05 * rms`.
NOISE_FLOOR_ALPHA = 0.05

#: EMA weight for a chunk that is already above the open threshold while the
#: detector is still closed. Those chunks are the attack of a word, not room
#: noise. Folding them in at the full weight poisons the floor: two 20 ms chunks
#: of ordinary speech push the floor from 0.0014 to 0.019, which lifts the open
#: threshold to about 0.06 and leaves the detector deaf to the next sentence for
#: roughly a second, clipping its opening words. Measured, not theoretical. They
#: are still folded in at a tenth of the weight so a genuinely loud room (a fan
#: that starts mid call) is still tracked, just slowly enough that speech cannot
#: yank the estimate.
NOISE_FLOOR_ALPHA_LOUD = 0.005

OPEN_FLOOR_MULTIPLIER = 3.2
OPEN_ABSOLUTE_MIN = 0.008
CLOSE_RATIO = 0.55

SENSITIVITY_MIN = 0.5
SENSITIVITY_MAX = 3.0

_INT16_FULL_SCALE = 32768.0
_HOST_IS_BIG_ENDIAN = sys.byteorder == "big"

#: Canonical RIFF/WAVE header size for 16 bit PCM with no extra chunks.
WAV_HEADER_BYTES = 44

__all__ = [
    "SAMPLE_RATE",
    "BYTES_PER_SAMPLE",
    "BYTES_PER_MS",
    "TAIL_MS",
    "WAV_HEADER_BYTES",
    "VadConfig",
    "VadEvent",
    "VadSegmenter",
    "pcm16_rms",
    "wav_bytes",
]


def pcm16_rms(chunk: bytes) -> float:
    """Compute the normalised RMS level of a 16 bit little endian PCM chunk.

    The chunk is interpreted as signed 16 bit little endian samples regardless
    of the host byte order, so the result is identical on x86 and on a big
    endian machine.

    Args:
        chunk: Raw PCM bytes. A trailing odd byte (a torn frame from the wire)
            is dropped rather than raising.

    Returns:
        The root mean square amplitude normalised to 0.0 through 1.0, where 1.0
        is full scale. An empty chunk, or a chunk shorter than one sample,
        returns 0.0.
    """
    length = len(chunk)
    remainder = length % BYTES_PER_SAMPLE
    if remainder:
        length -= remainder
        chunk = chunk[:length]
    if length < BYTES_PER_SAMPLE:
        return 0.0

    samples = array.array("h")
    samples.frombytes(chunk)
    if _HOST_IS_BIG_ENDIAN:
        # array("h") decodes in host order, so undo it to get little endian.
        samples.byteswap()

    total = 0
    for sample in samples:
        total += sample * sample
    return math.sqrt(total / len(samples)) / _INT16_FULL_SCALE


def wav_bytes(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap raw mono 16 bit PCM in a canonical 44 byte RIFF/WAVE container.

    The header is the minimal canonical form that Whisper, ffmpeg and the
    stdlib `wave` module all accept: a 12 byte RIFF descriptor, a 24 byte
    `fmt ` subchunk of size 16 describing uncompressed PCM, then an 8 byte
    `data` subchunk header. Every field is packed explicitly little endian.

    Invariant: ``len(wav_bytes(pcm)) == 44 + len(pcm)`` for any input.

    Args:
        pcm: Raw signed 16 bit little endian mono samples.
        sample_rate: Sample rate in Hz written into the header.

    Returns:
        A complete in memory WAV file.

    Raises:
        RuntimeError: If the packed header is not exactly 44 bytes, which would
            mean the struct format has been corrupted.
        ValueError: If the sample rate is not positive.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")

    data_size = len(pcm)
    channels = 1
    bits_per_sample = BYTES_PER_SAMPLE * 8
    block_align = channels * BYTES_PER_SAMPLE
    byte_rate = sample_rate * block_align

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # audio format 1 = uncompressed PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    if len(header) != WAV_HEADER_BYTES:
        raise RuntimeError(f"WAV header must be {WAV_HEADER_BYTES} bytes, got {len(header)}")
    return header + pcm


@dataclass
class VadConfig:
    """Tuning for one :class:`VadSegmenter`.

    Attributes:
        silence_ms: Continuous below threshold audio required to close an
            utterance. See the module docstring for why the default is 620.
        min_speech_ms: Voiced duration below which a closed segment is thrown
            away instead of being sent to STT. Filters door slams and coughs.
        max_utterance_ms: Hard ceiling on a single segment. A monologue is force
            split at this point so the rep is not left waiting.
        preroll_ms: Pre speech audio kept in the ring buffer and prepended to
            each segment so word attacks are not clipped.
        sensitivity: Threshold divisor in 0.5 through 3.0. Higher means a lower
            threshold, so the detector triggers more easily.
    """

    silence_ms: int = 620
    min_speech_ms: int = 260
    max_utterance_ms: int = 12000
    preroll_ms: int = 320
    sensitivity: float = 1.0


@dataclass
class VadEvent:
    """One thing the segmenter decided while consuming audio.

    Attributes:
        kind: `speech_start` when the detector opens, `speech_end` when it
            closes, `utterance` when a segment is ready for transcription.
        pcm: The segment audio, set only on `utterance`. Always None on
            `speech_start` and `speech_end`.
        duration_ms: For `utterance`, the duration of `pcm` including pre roll
            and tail. For `speech_end`, the voiced duration only, excluding pre
            roll and trailing silence. Zero for `speech_start`.
        rms: For `utterance`, the peak RMS seen inside the segment, which is a
            useful "was this actually loud enough" signal. Otherwise the RMS of
            the chunk that caused the event.
    """

    kind: Literal["speech_start", "speech_end", "utterance"]
    pcm: bytes | None = None
    duration_ms: int = 0
    rms: float = 0.0


class VadSegmenter:
    """Energy VAD with adaptive noise floor, pre roll ring buffer and hangover.

    Feed 16 kHz Int16 little endian mono bytes with :meth:`push` and act on the
    returned events. One instance handles one audio stream, so a dual stream
    call uses two of them (one for the prospect, one for the rep).

    The instance is not thread safe and not task safe. Drive it from a single
    coroutine, which is what the WebSocket receive loop does anyway.
    """

    def __init__(self, cfg: VadConfig) -> None:
        """Initialise the segmenter.

        Args:
            cfg: Tuning parameters. The sensitivity is clamped on the way in.
        """
        self._cfg = cfg
        self._cfg.sensitivity = _clamp(cfg.sensitivity, SENSITIVITY_MIN, SENSITIVITY_MAX)

        self._preroll_cap = _even_bytes_for_ms(cfg.preroll_ms)
        self._tail_cap = _even_bytes_for_ms(TAIL_MS)

        self._preroll: deque[bytes] = deque()
        self._preroll_bytes = 0

        self._segment: list[bytes] = []
        self._segment_bytes = 0
        self._head_bytes = 0
        self._silence_bytes = 0

        self._speaking = False
        self._above_ms = 0.0
        self._above_bytes = 0
        self._last_rms = 0.0
        self._peak_rms = 0.0
        self._noise_floor = NOISE_FLOOR_INITIAL

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def speaking(self) -> bool:
        """Whether the detector currently considers the stream to be speech."""
        return self._speaking

    @property
    def last_rms(self) -> float:
        """Normalised RMS of the most recently pushed chunk, 0.0 through 1.0."""
        return self._last_rms

    @property
    def noise_floor(self) -> float:
        """Current adaptive noise floor estimate."""
        return self._noise_floor

    @property
    def sensitivity(self) -> float:
        """Current threshold divisor, always inside 0.5 through 3.0."""
        return self._cfg.sensitivity

    @property
    def open_threshold(self) -> float:
        """RMS a chunk must reach to count toward opening a segment."""
        floor_term = self._noise_floor * OPEN_FLOOR_MULTIPLIER / self._cfg.sensitivity
        absolute_term = OPEN_ABSOLUTE_MIN / self._cfg.sensitivity
        return max(floor_term, absolute_term)

    @property
    def close_threshold(self) -> float:
        """RMS a chunk must drop below to count as silence, with hysteresis."""
        return self.open_threshold * CLOSE_RATIO

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def set_sensitivity(self, value: float) -> None:
        """Retune the detector live, clamping to the supported range.

        Args:
            value: Requested sensitivity. Values outside 0.5 through 3.0 are
                clamped, and a non finite value is ignored.
        """
        if not math.isfinite(value):
            return
        self._cfg.sensitivity = _clamp(float(value), SENSITIVITY_MIN, SENSITIVITY_MAX)

    def reset(self) -> None:
        """Drop all buffered audio and return to the cold start state.

        The noise floor goes back to its initial estimate, so the detector
        re learns the room. The configured sensitivity is deliberately kept,
        because it is a user setting sent over the control channel rather than
        stream state.
        """
        self._preroll.clear()
        self._preroll_bytes = 0
        self._segment = []
        self._segment_bytes = 0
        self._head_bytes = 0
        self._silence_bytes = 0
        self._speaking = False
        self._above_ms = 0.0
        self._above_bytes = 0
        self._last_rms = 0.0
        self._peak_rms = 0.0
        self._noise_floor = NOISE_FLOOR_INITIAL

    # ------------------------------------------------------------------
    # Audio path
    # ------------------------------------------------------------------

    def push(self, chunk: bytes) -> list[VadEvent]:
        """Consume one chunk of audio and report anything that changed.

        The chunk may be any size. All timing is derived from its byte length,
        so callers can send 20 ms frames or coalesced 200 ms bursts without
        changing the behaviour. The call is O(len(chunk)) and allocates only the
        temporary sample array used for the RMS, except on the rare push that
        closes a segment.

        Args:
            chunk: Raw 16 kHz mono Int16 little endian PCM. An empty chunk is a
                no op, and a trailing odd byte is dropped to keep the buffered
                stream sample aligned.

        Returns:
            The events produced by this chunk, usually an empty list.
        """
        if not chunk:
            return []
        remainder = len(chunk) % BYTES_PER_SAMPLE
        if remainder:
            chunk = chunk[: len(chunk) - remainder]
            if not chunk:
                return []

        rms = pcm16_rms(chunk)
        self._last_rms = rms
        chunk_ms = len(chunk) / BYTES_PER_MS

        if not self._speaking:
            return self._push_idle(chunk, rms, chunk_ms)
        return self._push_speaking(chunk, rms)

    def flush(self) -> list[VadEvent]:
        """Force close the current utterance, for example on a `flush` control.

        Returns:
            The closing events, or an empty list when nothing is open. The
            minimum speech duration rule still applies, so flushing a stream
            that only holds a click produces `speech_end` and no utterance.
        """
        if not self._speaking:
            return []
        return self._close(seed=None)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _push_idle(self, chunk: bytes, rms: float, chunk_ms: float) -> list[VadEvent]:
        """Handle a chunk that arrived while the detector was closed."""
        # The threshold is sampled once, before the floor moves, so a chunk
        # cannot change the verdict on itself.
        threshold = self.open_threshold

        # The noise floor only tracks while nothing is being said, otherwise the
        # speaker's own voice would drag the threshold up above their voice. The
        # weight is asymmetric, see NOISE_FLOOR_ALPHA_LOUD for why.
        alpha = NOISE_FLOOR_ALPHA if rms < threshold else NOISE_FLOOR_ALPHA_LOUD
        floor = (1.0 - alpha) * self._noise_floor + alpha * rms
        self._noise_floor = _clamp(floor, NOISE_FLOOR_MIN, NOISE_FLOOR_MAX)

        # Ring buffer push. Chunks are stored by reference and only the running
        # byte total is maintained, so nothing is concatenated per chunk. One
        # chunk is always kept even if it is on its own larger than the cap.
        self._preroll.append(chunk)
        self._preroll_bytes += len(chunk)
        while self._preroll_bytes > self._preroll_cap and len(self._preroll) > 1:
            self._preroll_bytes -= len(self._preroll.popleft())

        # The byte count is tracked next to the millisecond count because _open
        # has to know exactly how much of the ring buffer is confirmed speech
        # rather than pre roll, and the caller's chunk size is not fixed.
        if rms >= threshold:
            self._above_ms += chunk_ms
            self._above_bytes += len(chunk)
        else:
            self._above_ms = 0.0
            self._above_bytes = 0

        if self._above_ms < OPEN_CONFIRM_MS:
            return []

        self._open(rms)
        return [VadEvent(kind="speech_start", pcm=None, duration_ms=0, rms=rms)]

    def _push_speaking(self, chunk: bytes, rms: float) -> list[VadEvent]:
        """Handle a chunk that arrived while a segment was open."""
        self._segment.append(chunk)
        self._segment_bytes += len(chunk)
        if rms > self._peak_rms:
            self._peak_rms = rms

        if rms < self.close_threshold:
            self._silence_bytes += len(chunk)
        else:
            self._silence_bytes = 0

        if self._segment_bytes / BYTES_PER_MS >= self._cfg.max_utterance_ms:
            return self._force_split(chunk)
        if self._silence_bytes / BYTES_PER_MS >= self._cfg.silence_ms:
            return self._close(seed=chunk)
        return []

    def _open(self, rms: float) -> None:
        """Promote the pre roll ring buffer into a new open segment."""
        self._speaking = True
        self._segment = list(self._preroll)
        self._segment_bytes = self._preroll_bytes
        # The pre roll is duplicated context, not voiced audio, so it is
        # excluded from the minimum speech duration test. The chunks that
        # confirmed the open are sitting in that same ring buffer, because
        # _push_idle buffers a chunk before it decides on it, and those chunks
        # ARE voiced, so they are subtracted back out. Counting them as pre roll
        # inflated min_speech_ms by one whole confirm window on every utterance:
        # at the 32 ms frame the worklet actually sends, a configured 260 ms
        # behaved like roughly 324 ms and clipped short answers ("no, thanks")
        # out of the transcript entirely. The clamp covers a pre roll shorter
        # than the confirm window, where the whole ring buffer is speech.
        self._head_bytes = max(0, self._preroll_bytes - self._above_bytes)
        self._preroll.clear()
        self._preroll_bytes = 0
        self._silence_bytes = 0
        self._above_ms = 0.0
        self._above_bytes = 0
        self._peak_rms = rms

    def _finish(self, *, trim: bool = True) -> tuple[bytes, float, float]:
        """Join the open segment, optionally trimming its trailing silence.

        Args:
            trim: True for a genuine close, where the endpoint decision has
                already been taken and everything past the 120 ms tail is dead
                air that only slows STT down. False for a force split, where
                the speaker has not stopped: the silence run is still being
                counted, it is carried into the next segment, and cutting the
                audio it refers to here would desynchronise the counter from
                the buffer it describes.

        Returns:
            A tuple of the segment PCM, its voiced duration in milliseconds
            (pre roll and trailing silence excluded) and the peak RMS seen.
        """
        body = b"".join(self._segment)
        trailing = self._silence_bytes
        if trim and trailing > self._tail_cap:
            cut = trailing - self._tail_cap
            body = body[: len(body) - cut] if cut < len(body) else b""
        kept_silence = min(trailing, self._tail_cap if trim else len(body))
        voiced_bytes = max(0, len(body) - self._head_bytes - kept_silence)
        return body, voiced_bytes / BYTES_PER_MS, self._peak_rms

    def _close(self, *, seed: bytes | None) -> list[VadEvent]:
        """Close the open segment and reset to the idle state.

        Args:
            seed: The chunk that triggered the close, used to prime the pre roll
                ring buffer so the next segment has continuity. None when the
                close came from :meth:`flush`.

        Returns:
            A `speech_end` event, followed by an `utterance` event when the
            segment carried enough voiced audio to be worth transcribing.
        """
        pcm, voiced_ms, peak = self._finish()
        events: list[VadEvent] = [
            VadEvent(
                kind="speech_end",
                pcm=None,
                duration_ms=int(round(voiced_ms)),
                rms=self._last_rms,
            )
        ]
        # Segments shorter than the minimum are discarded outright. speech_end
        # is still reported because speech_start already was, and the UI needs
        # the state machine to close.
        if pcm and voiced_ms >= self._cfg.min_speech_ms:
            events.append(
                VadEvent(
                    kind="utterance",
                    pcm=pcm,
                    duration_ms=int(round(len(pcm) / BYTES_PER_MS)),
                    rms=peak,
                )
            )

        self._speaking = False
        self._segment = []
        self._segment_bytes = 0
        self._head_bytes = 0
        self._silence_bytes = 0
        self._above_ms = 0.0
        self._above_bytes = 0
        self._peak_rms = 0.0
        self._preroll.clear()
        self._preroll_bytes = 0
        if seed:
            self._preroll.append(seed)
            self._preroll_bytes = len(seed)
        return events

    def _force_split(self, chunk: bytes) -> list[VadEvent]:
        """Emit an over long segment and continue speaking without a gap.

        The speaker has not stopped, so no `speech_end` is reported and the
        detector stays open. The next segment is seeded with the tail of the one
        just emitted, which guarantees that the audio delivered in this very
        push call survives into the next segment and that a word straddling the
        boundary is present in full on at least one side of it. The seed is
        counted as head, not voiced, audio.

        A silence run that is already in progress is carried across the split
        intact. It was earned against the same continuous stream and no endpoint
        decision consumed it, so restarting it would make the speaker pay the
        `silence_ms` timer twice, up to `silence_ms - TAIL_MS` (500 ms at the
        defaults) of pure added latency on the close that follows a split, which
        is exactly the case every long monologue walks into. The segment is
        therefore emitted untrimmed and the seed is stretched to cover the whole
        run, so `_silence_bytes` keeps describing real trailing silence inside
        the new segment and the trim in :meth:`_finish` can never reach past it
        into voiced audio.
        """
        pcm, _voiced_ms, peak = self._finish(trim=False)
        events: list[VadEvent] = []
        if pcm:
            events.append(
                VadEvent(
                    kind="utterance",
                    pcm=pcm,
                    duration_ms=int(round(len(pcm) / BYTES_PER_MS)),
                    rms=peak,
                )
            )

        seed_target = max(self._preroll_cap, len(chunk), self._silence_bytes)
        seed = pcm if seed_target >= len(pcm) else pcm[len(pcm) - seed_target :]
        self._segment = [seed] if seed else []
        self._segment_bytes = len(seed)
        self._head_bytes = len(seed)
        # The silence run survives the split whole. The len(seed) bound is only
        # a safety net for a segment shorter than the run, which the seed target
        # above already makes impossible.
        self._silence_bytes = min(self._silence_bytes, len(seed))
        self._peak_rms = 0.0
        return events


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp a float into an inclusive range.

    Args:
        value: The value to clamp.
        low: Lower bound.
        high: Upper bound.

    Returns:
        The value constrained to the range.
    """
    if value < low:
        return low
    if value > high:
        return high
    return value


def _even_bytes_for_ms(ms: int) -> int:
    """Convert a duration to a sample aligned, non negative byte count.

    Args:
        ms: Duration in milliseconds.

    Returns:
        The byte count rounded down to a whole 16 bit sample, never negative.
    """
    total = int(max(0, ms) * BYTES_PER_MS)
    return total - (total % BYTES_PER_SAMPLE)


if __name__ == "__main__":
    import io
    import wave

    def _silence(ms: int) -> bytes:
        return b"\x00" * _even_bytes_for_ms(ms)

    def _tone(ms: int, freq: float = 440.0, amp: float = 0.25) -> bytes:
        count = int(SAMPLE_RATE * ms / 1000)
        buf = array.array(
            "h",
            (int(amp * 32767 * math.sin(2.0 * math.pi * freq * i / SAMPLE_RATE)) for i in range(count)),
        )
        if _HOST_IS_BIG_ENDIAN:
            buf.byteswap()
        return buf.tobytes()

    assert pcm16_rms(b"") == 0.0
    assert pcm16_rms(b"\x00") == 0.0
    assert pcm16_rms(_tone(20) + b"\x7f") > 0.1  # odd trailing byte is dropped

    LEAD_MS, TONE_MS, TRAIL_MS, FRAME_MS = 400, 500, 900, 20
    signal = _silence(LEAD_MS) + _tone(TONE_MS) + _silence(TRAIL_MS)
    frame = _even_bytes_for_ms(FRAME_MS)
    seg = VadSegmenter(VadConfig())
    seen: list[VadEvent] = []
    for offset in range(0, len(signal) - frame + 1, frame):
        seen.extend(seg.push(signal[offset : offset + frame]))

    starts = [e for e in seen if e.kind == "speech_start"]
    ends = [e for e in seen if e.kind == "speech_end"]
    utterances = [e for e in seen if e.kind == "utterance"]
    assert len(starts) == 1, f"expected 1 speech_start, got {len(starts)}"
    assert len(ends) == 1, f"expected 1 speech_end, got {len(ends)}"
    assert len(utterances) == 1, f"expected 1 utterance, got {len(utterances)}"
    assert not seg.speaking, "segmenter should be closed after the trailing silence"

    utt = utterances[0]
    assert utt.pcm is not None
    assert starts[0].pcm is None and ends[0].pcm is None, "only utterance carries pcm"
    expected_ms = VadConfig().preroll_ms + TONE_MS + TAIL_MS
    drift = abs(utt.duration_ms - expected_ms)
    assert drift <= 200, f"duration {utt.duration_ms} ms drifted {drift} ms from {expected_ms} ms"

    wav = wav_bytes(utt.pcm)
    assert len(wav) == WAV_HEADER_BYTES + len(utt.pcm)
    with wave.open(io.BytesIO(wav), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == BYTES_PER_SAMPLE
        assert reader.getframerate() == SAMPLE_RATE
        assert reader.readframes(reader.getnframes()) == utt.pcm

    print(
        f"audio self test ok: 1 utterance, {utt.duration_ms} ms "
        f"(expected about {expected_ms} ms), peak rms {utt.rms:.4f}, "
        f"wav {len(wav)} bytes, noise floor {seg.noise_floor:.5f}"
    )
