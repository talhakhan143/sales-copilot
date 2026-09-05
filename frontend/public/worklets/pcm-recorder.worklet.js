/**
 * pcm-recorder.worklet.js
 *
 * Why an AudioWorklet and not MediaRecorder.
 * MediaRecorder gives back WebM/Opus blobs where only the very first chunk carries the
 * container header, so every later chunk is undecodable on its own. Our server slices the
 * conversation into utterances and POSTs each one to a REST speech endpoint, which means it
 * needs self contained audio at any cut point. Opus in a container cannot give us that
 * cheaply, and we do not want Opus at all: whisper class models want raw 16 kHz mono PCM.
 * So this processor takes the Float32 render quanta straight off the audio graph, resamples
 * them to the target rate with linear interpolation, and posts fixed size Int16 little
 * endian frames as transferable ArrayBuffers (zero copy, no garbage on the main thread).
 *
 * This file runs inside AudioWorkletGlobalScope: plain ES2020, no imports, no bundler.
 * `sampleRate`, `AudioWorkletProcessor` and `registerProcessor` are globals of that scope.
 */

const DEFAULT_TARGET_RATE = 16000;
const DEFAULT_FRAME_SAMPLES = 512;

class PcmRecorder extends AudioWorkletProcessor {
  constructor(options) {
    super(options);

    const processorOptions = (options && options.processorOptions) || {};
    const wantedRate = Number(processorOptions.targetRate);
    const wantedFrame = Number(processorOptions.frameSamples);

    this.targetRate =
      Number.isFinite(wantedRate) && wantedRate > 0 ? wantedRate : DEFAULT_TARGET_RATE;
    this.frameSamples =
      Number.isFinite(wantedFrame) && wantedFrame > 0
        ? Math.floor(wantedFrame)
        : DEFAULT_FRAME_SAMPLES;

    // `sampleRate` is the AudioContext hardware rate, typically 48000. We never force a
    // rate on the context, we resample here instead, so the graph keeps its native rate.
    this.ratio = sampleRate / this.targetRate;

    // Fractional read cursor into the CURRENT render quantum. It survives across process()
    // calls, which is the whole trick: restarting it at 0 every quantum would round the
    // phase 375 times a second and put an audible click every 128 input samples in the
    // stream. It lives in [-1, ratio) and a value below 0 means "interpolate between the
    // last sample of the previous quantum and the first sample of this one".
    this.readIndex = 0;

    // Last sample of the previous quantum, virtual index -1, so interpolation is continuous
    // across the quantum boundary.
    this.tail = 0;

    this.frame = new Int16Array(this.frameSamples);
    this.filled = 0;

    // Scratch buffer reused for downmixing, allocated on first multi channel input.
    this.mono = null;
  }

  /**
   * Returns a mono Float32Array view of the input. One channel is passed through as is,
   * anything wider is averaged into the scratch buffer.
   */
  downmix(channels, length) {
    if (channels.length === 1) {
      return channels[0];
    }
    if (this.mono === null || this.mono.length < length) {
      this.mono = new Float32Array(length);
    }
    const out = this.mono;
    const count = channels.length;
    for (let i = 0; i < length; i += 1) {
      let sum = 0;
      for (let c = 0; c < count; c += 1) {
        const channel = channels[c];
        sum += channel.length > i ? channel[i] : 0;
      }
      out[i] = sum / count;
    }
    return out;
  }

  emit(sample) {
    // Clamp before scaling so a hot source cannot wrap around into the opposite polarity.
    let value = sample;
    if (value > 1) {
      value = 1;
    } else if (value < -1) {
      value = -1;
    }
    // `| 0` truncates toward zero, which is what we want here, and it also turns a NaN
    // into a harmless 0 instead of poisoning the frame.
    this.frame[this.filled] = (value * 0x7fff) | 0;
    this.filled += 1;

    if (this.filled === this.frameSamples) {
      const buffer = this.frame.buffer;
      this.port.postMessage(buffer, [buffer]);
      // The buffer is detached by the transfer, so a fresh one is mandatory.
      this.frame = new Int16Array(this.frameSamples);
      this.filled = 0;
    }
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) {
      return true;
    }
    const firstChannel = input[0];
    if (!firstChannel || firstChannel.length === 0) {
      return true;
    }

    const length = firstChannel.length;
    const source = this.downmix(input, length);
    const ratio = this.ratio;

    // We only produce a sample when both neighbours exist, so the loop stops at length - 1
    // and whatever phase is left over is carried into the next quantum below.
    const limit = length - 1;
    let position = this.readIndex;

    while (position < limit) {
      const index = Math.floor(position);
      const t = position - index;
      const a = index < 0 ? this.tail : source[index];
      const b = source[index + 1];
      this.emit(a + (b - a) * t);
      position += ratio;
    }

    // Rebase the cursor onto the next quantum and remember the boundary sample.
    this.readIndex = position - length;
    this.tail = source[length - 1];

    return true;
  }
}

registerProcessor("pcm-recorder", PcmRecorder);
