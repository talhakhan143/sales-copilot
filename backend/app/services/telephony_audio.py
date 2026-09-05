"""G.711 mu-law decoding and 8 kHz to 16 kHz resampling for phone call audio.

This module is pure, stdlib only and unit testable. No numpy, no network, no
FastAPI, no clock. It runs inside the Twilio Media Streams receive loop, so it
has to be cheap and it has to be exactly right.

WHY THIS EXISTS
---------------
The browser path already hands the VAD what it wants: 16 kHz mono Int16 little
endian PCM. A real phone call does not. Twilio Media Streams sends base64 text
frames carrying 160 bytes of G.711 mu-law at 8 kHz, one frame every 20 ms. That
is a different sample rate, a different sample width and a different companding
law, all three at once.

Everything downstream, the VAD thresholds in ``app.services.audio``, the WAV
header, the Whisper upload, assumes 16 kHz Int16. So this file is the single
adapter that turns a phone frame into exactly that. Get it wrong and nothing
crashes, which is the dangerous part: the call simply transcribes as noise, or
transcribes at half speed, or the VAD never opens because the level is wrong.
There is no error message for "your audio is chipmunks". Hence the self test at
the bottom, which checks the decode table against the stdlib byte for byte.

MU-LAW, IN ONE PARAGRAPH
------------------------
Mu-law is a logarithmic 8 bit code. Quiet sounds get fine steps and loud sounds
get coarse ones, which fits about 14 bits of dynamic range into 8 bits. A byte
on the wire is stored inverted, so a decode always starts with a complement.
What is left is a sign bit, a 3 bit exponent (the segment) and a 4 bit mantissa.
The magnitude is rebuilt as ``((mantissa << 3) + BIAS) << exponent`` and the
bias is then subtracted back out. The shift by 3 puts the mantissa back where
the encoder's quantiser left it, the bias of 132 is the constant the encoder
added before compressing so that very small values still have a code, and the
exponent shift undoes the compression by doubling the step size once per
segment. The result spans -32124 to +32124.

There are only 256 possible inputs, so the whole thing is computed once at
import into a tuple and every sample after that is one index lookup. Computing
it per sample would be pure waste: a two way call is 100 frames a second, which
is 16000 samples a second through this code.

RESAMPLING, AND WHY THE STATE MATTERS
-------------------------------------
Going from 8 kHz to 16 kHz means one new sample between every pair of real ones.
Linear interpolation is the right tool here: the audio was already band limited
to 3.4 kHz by the phone network, so there is nothing above the old Nyquist limit
to alias, and a proper filter would buy nothing that Whisper can hear.

The trap is the frame boundary. A pure function that only sees one 20 ms frame
has no previous sample to interpolate from, so it has to invent one, and every
frame then starts with a small step discontinuity. Fifty of those a second, per
lane, is a 50 Hz buzz laid on top of the speech. It is quiet enough that a
person listening would call the line "a bit rough" and loud enough to cost real
transcription accuracy. So :class:`Upsampler` keeps the last sample of the
previous frame and the stream stays continuous across every call. One instance
per audio lane, because the two lanes of a call are two independent streams.

The output is exactly the same as ``audioop.ratecv`` linear interpolation,
delayed by half an input sample. The self test asserts that equality sample for
sample while ``audioop`` still exists, which is the strongest check available.
"""

from __future__ import annotations

import array
import base64
import binascii
import sys

#: Sample rate of the phone network leg, in Hz. Twilio Media Streams is always
#: 8 kHz mono mu-law, whatever codec the carrier used further up the line.
TELEPHONY_RATE = 8000

#: Sample rate everything downstream expects, in Hz. Must stay equal to
#: ``app.services.audio.SAMPLE_RATE``, which is what the VAD timing is built on.
TARGET_RATE = 16000

BYTES_PER_SAMPLE = 2

#: Duration of one Twilio media frame, in milliseconds.
TWILIO_FRAME_MS = 20

#: Mu-law bytes in one Twilio media frame, 160. That decodes to 320 bytes of
#: 8 kHz PCM and 640 bytes of 16 kHz PCM.
TWILIO_FRAME_BYTES = TELEPHONY_RATE * TWILIO_FRAME_MS // 1000

#: Constant the mu-law encoder adds to the magnitude before compressing, 132.
#: It is what gives near silence a usable code instead of collapsing it to zero.
MULAW_BIAS = 0x84

#: Sign bit of a complemented mu-law byte. Set means the sample is negative.
MULAW_SIGN_BIT = 0x80

#: The 3 bit exponent, called the segment, of a complemented mu-law byte.
MULAW_SEG_MASK = 0x70

#: Bit position of that exponent.
MULAW_SEG_SHIFT = 4

#: The 4 bit mantissa of a complemented mu-law byte.
MULAW_QUANT_MASK = 0x0F

#: Widest value the decode can produce, positive or negative. Well inside the
#: Int16 range, so the clamp below never actually fires on valid input.
MULAW_PEAK = 32124

INT16_MIN = -32768
INT16_MAX = 32767

_HOST_IS_BIG_ENDIAN = sys.byteorder == "big"

__all__ = [
    "BYTES_PER_SAMPLE",
    "MULAW_BIAS",
    "MULAW_DECODE_TABLE",
    "MULAW_PEAK",
    "TARGET_RATE",
    "TELEPHONY_RATE",
    "TWILIO_FRAME_BYTES",
    "TWILIO_FRAME_MS",
    "Upsampler",
    "mulaw_to_pcm16",
    "pcm16_upsample_8k_to_16k",
    "twilio_frame_to_pcm16k",
]


def _build_mulaw_table() -> tuple[int, ...]:
    """Compute the 256 entry G.711 mu-law to linear decode table.

    This is the standard decode from the ITU G.711 recommendation, written out
    step by step rather than pasted in as a magic list of 256 numbers, so the
    next person can check it instead of trusting it.

    For each possible byte:

    1. Complement it. Mu-law is transmitted inverted, which is what makes
       digital silence 0xFF rather than 0x00 and keeps the bit density on the
       line high.
    2. Split the complement into a sign bit, a 3 bit exponent and a 4 bit
       mantissa.
    3. Rebuild the magnitude as ``((mantissa << 3) + BIAS) << exponent``. The
       shift by 3 restores the low bits the encoder quantised away, the bias is
       the encoder's own offset, and the exponent shift undoes the logarithmic
       companding one segment at a time.
    4. Subtract the bias back out and apply the sign.
    5. Clamp into the Int16 range. Valid input can only reach 32124, so this is
       a guard against a future edit, not against the data.

    Two entries are worth remembering: 0xFF and 0x7F both decode to exactly 0,
    because mu-law has a positive zero and a negative zero. A silent phone line
    is a long run of 0xFF bytes.

    Returns:
        A 256 entry tuple of signed 16 bit sample values, indexed by the raw
        mu-law byte.
    """
    table: list[int] = []
    for raw in range(256):
        code = ~raw & 0xFF
        magnitude = ((code & MULAW_QUANT_MASK) << 3) + MULAW_BIAS
        magnitude <<= (code & MULAW_SEG_MASK) >> MULAW_SEG_SHIFT
        value = MULAW_BIAS - magnitude if code & MULAW_SIGN_BIT else magnitude - MULAW_BIAS
        table.append(min(INT16_MAX, max(INT16_MIN, value)))
    return tuple(table)


#: The decode table, built exactly once when this module is imported. Every
#: sample on every call is a single index into this tuple.
MULAW_DECODE_TABLE: tuple[int, ...] = _build_mulaw_table()


def mulaw_to_pcm16(payload: bytes) -> bytes:
    """Decode G.711 mu-law bytes into signed 16 bit little endian PCM.

    One input byte becomes exactly two output bytes, so a 160 byte Twilio frame
    becomes 320 bytes. The sample rate does not change, the output is still
    8 kHz. The byte order of the result is little endian on every host, big
    endian machines included.

    Args:
        payload: Raw mu-law bytes as they arrived from the carrier.

    Returns:
        Int16 little endian PCM, two bytes per input byte. An empty input
        returns ``b""``.
    """
    if not payload:
        return b""

    table = MULAW_DECODE_TABLE
    samples = array.array("h", [table[byte] for byte in payload])
    if _HOST_IS_BIG_ENDIAN:
        # array("h") writes in host order, so undo it to get little endian.
        samples.byteswap()
    return samples.tobytes()


class Upsampler:
    """Stateful 8 kHz to 16 kHz linear resampler for one audio lane.

    Feed it 8 kHz Int16 little endian PCM with :meth:`upsample` and it returns
    twice as many bytes at 16 kHz. It remembers the last sample it saw, so the
    interpolated sample that opens each call is computed against the real
    previous sample rather than against an invented one. That is the whole
    point of the class. See the module docstring for what a pure function costs
    at a 20 ms frame boundary.

    Use one instance per lane. A call has two lanes, the client and the rep, and
    sharing one instance between them would interpolate each lane against the
    other lane's audio, which is not a subtle bug but is an easy one to write.

    The instance is not thread safe and not task safe. Drive it from a single
    coroutine, which is what the WebSocket receive loop does anyway.
    """

    def __init__(self) -> None:
        """Start cold, assuming silence before the first sample."""
        self._last = 0

    @property
    def last(self) -> int:
        """The most recent input sample, carried into the next call."""
        return self._last

    def reset(self) -> None:
        """Forget the carried sample, for example when a new call starts."""
        self._last = 0

    def upsample(self, pcm: bytes) -> bytes:
        """Double the sample rate of one chunk of 8 kHz PCM.

        Each input sample produces two output samples: first the midpoint
        between the previous input sample and this one, then this one
        unchanged. That is ordinary linear interpolation, written so that the
        output stream lags the input by half an input sample, which is 62.5
        microseconds and is inaudible. Writing it the other way round would need
        the next sample, which has not arrived yet, and would cost a whole frame
        of latency.

        The midpoint can never overflow, since the average of two Int16 values
        is an Int16, so no clamp is needed here.

        Args:
            pcm: Raw 8 kHz mono Int16 little endian PCM of any length. A
                trailing odd byte, which means a torn frame, is dropped rather
                than raising.

        Returns:
            16 kHz mono Int16 little endian PCM, exactly twice the input length
            in bytes. An empty input returns ``b""`` and leaves the carried
            sample untouched.
        """
        if not pcm:
            return b""
        remainder = len(pcm) % BYTES_PER_SAMPLE
        if remainder:
            pcm = pcm[: len(pcm) - remainder]
            if not pcm:
                return b""

        source = array.array("h")
        source.frombytes(pcm)
        if _HOST_IS_BIG_ENDIAN:
            # The wire is little endian, array("h") reads in host order.
            source.byteswap()

        # Preallocated so the loop only assigns, never grows the buffer.
        out = array.array("h", bytes(len(pcm) * 2))
        previous = self._last
        index = 0
        for sample in source:
            out[index] = (previous + sample) // 2
            out[index + 1] = sample
            previous = sample
            index += 2
        self._last = previous

        if _HOST_IS_BIG_ENDIAN:
            out.byteswap()
        return out.tobytes()


def pcm16_upsample_8k_to_16k(pcm: bytes) -> bytes:
    """Upsample one self contained buffer of 8 kHz PCM to 16 kHz.

    This is the stateless convenience wrapper. It builds a fresh
    :class:`Upsampler` per call, so it starts every buffer from an assumed
    silence and clicks at the seam if you feed it a live stream frame by frame.
    It exists for the self test below and for one shot whole recordings. The
    live call path must use a long lived :class:`Upsampler` per lane instead.

    Args:
        pcm: Raw 8 kHz mono Int16 little endian PCM.

    Returns:
        16 kHz mono Int16 little endian PCM, twice the input length in bytes.
        An empty input returns ``b""``.
    """
    return Upsampler().upsample(pcm)


def twilio_frame_to_pcm16k(b64_payload: str, upsampler: Upsampler | None = None) -> bytes:
    """Turn one Twilio ``media`` frame payload into VAD ready 16 kHz PCM.

    This is the whole adapter in one call: base64 decode, mu-law decode, then
    upsample. A standard 160 byte frame arrives as about 216 characters of
    base64 and leaves as 640 bytes of PCM, which is 20 ms at 16 kHz.

    A payload that is not valid base64 returns ``b""`` instead of raising. One
    torn frame must never kill the WebSocket receive loop and end a live call,
    and 20 ms of dropped audio is not noticeable. A caller that wants to count
    bad frames can: a non empty payload that returns ``b""`` was malformed.

    Args:
        b64_payload: The ``media.payload`` string from Twilio, base64 encoded
            8 kHz mu-law.
        upsampler: The lane's resampler, so its carried sample survives across
            frames. Defaults to a fresh one, which is correct only for a single
            isolated frame such as a test.

    Returns:
        16 kHz mono Int16 little endian PCM ready for
        :meth:`app.services.audio.VadSegmenter.push`. An empty or malformed
        payload returns ``b""``.
    """
    if not b64_payload:
        return b""
    try:
        mulaw = base64.b64decode(b64_payload)
    except (binascii.Error, ValueError):
        return b""
    if not mulaw:
        return b""

    pcm8 = mulaw_to_pcm16(mulaw)
    if upsampler is None:
        upsampler = Upsampler()
    return upsampler.upsample(pcm8)


if __name__ == "__main__":
    import math
    import warnings

    def _mulaw_sine(count: int, freq: float = 300.0, amp: int = 9000) -> bytes:
        """Encode a sine as mu-law by picking the nearest decode table entry."""
        wants = [int(amp * math.sin(2.0 * math.pi * freq * i / TELEPHONY_RATE)) for i in range(count)]
        return bytes(min(range(256), key=lambda b: abs(MULAW_DECODE_TABLE[b] - want)) for want in wants)

    def _samples(pcm: bytes) -> array.array[int]:
        """Read little endian Int16 bytes back into an array on any host."""
        buf = array.array("h")
        buf.frombytes(pcm)
        if _HOST_IS_BIG_ENDIAN:
            buf.byteswap()
        return buf

    assert len(MULAW_DECODE_TABLE) == 256, "decode table must cover every byte"
    assert all(INT16_MIN <= v <= INT16_MAX for v in MULAW_DECODE_TABLE), "value out of Int16 range"
    assert MULAW_DECODE_TABLE[0xFF] == 0 and MULAW_DECODE_TABLE[0x7F] == 0, "both zeros decode to 0"
    assert MULAW_DECODE_TABLE[0x00] == -MULAW_PEAK and MULAW_DECODE_TABLE[0x80] == MULAW_PEAK

    frame = _mulaw_sine(TWILIO_FRAME_BYTES)
    pcm8 = mulaw_to_pcm16(frame)
    pcm16 = pcm16_upsample_8k_to_16k(pcm8)
    assert len(frame) == 160 and len(pcm8) == 320 and len(pcm16) == 640, "frame size chain broke"
    assert mulaw_to_pcm16(b"") == b"" and pcm16_upsample_8k_to_16k(b"") == b"", "empty must be empty"
    assert twilio_frame_to_pcm16k("") == b"" and twilio_frame_to_pcm16k("~ not base64 ~") == b""
    assert twilio_frame_to_pcm16k(base64.b64encode(frame).decode()) == pcm16

    tone = _mulaw_sine(800)
    tone8 = mulaw_to_pcm16(tone)
    src, up = _samples(tone8), _samples(pcm16_upsample_8k_to_16k(tone8))
    assert abs(max(up) - max(src)) <= 64 and abs(min(up) - min(src)) <= 64, "the peaks moved"
    rms8, rms16 = (math.sqrt(sum(s * s for s in buf) / len(buf)) for buf in (src, up))
    drift_pct = abs(rms16 - rms8) / rms8 * 100.0
    assert drift_pct < 2.0, f"upsampling changed the level by {drift_pct:.2f} percent"

    try:  # audioop was removed in Python 3.13, so the strongest check is optional.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import audioop
    except ImportError:
        audioop_note = "audioop not available, cross check skipped"
    else:
        codes = bytes(range(256))
        assert mulaw_to_pcm16(codes) == audioop.ulaw2lin(codes, 2), "table differs from audioop"
        ref, _state = audioop.ratecv(tone8, 2, 1, TELEPHONY_RATE, TARGET_RATE, None)
        assert list(up[1:]) == list(_samples(ref)), "upsampler drifted from audioop"
        audioop_note = "audioop cross check exact on all 256 codes and on the tone"

    # The carry test. Frame by frame through ONE Upsampler must not make a
    # bigger sample to sample step at a seam than the worst step inside a frame.
    lane = Upsampler()
    whole = array.array("h")
    inside = 0
    for start in range(0, len(tone), TWILIO_FRAME_BYTES):
        block = _samples(lane.upsample(mulaw_to_pcm16(tone[start : start + TWILIO_FRAME_BYTES])))
        inside = max(inside, max(abs(block[i + 1] - block[i]) for i in range(len(block) - 1)))
        whole.extend(block)
    across = max(abs(whole[i + 1] - whole[i]) for i in range(len(whole) - 1))
    assert across <= inside, f"seam jump {across} beats the worst in frame step {inside}"
    assert list(whole) == list(up), "frame by frame must equal the whole buffer in one go"

    print(
        f"telephony_audio self test ok: 256 entries in {-MULAW_PEAK} to {MULAW_PEAK}, {len(frame)} "
        f"mu-law bytes to {len(pcm8)} pcm to {len(pcm16)} at 16 kHz, level drift {drift_pct:.2f} "
        f"percent, seam step {across} against in frame worst {inside}, {audioop_note}"
    )
