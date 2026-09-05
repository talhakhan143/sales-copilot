"use client";

/**
 * Browser side capture pipeline.
 *
 * Two independent sources feed the teleprompter:
 *   client (id 0) the prospect, captured from a shared tab or screen, or from a virtual
 *                 cable input device such as BlackHole or VB-Cable
 *   rep    (id 1) the user's own microphone
 *
 * Both go through the same path: MediaStream -> AudioWorklet ("pcm-recorder") -> fixed size
 * Int16 little endian frames at 16 kHz. MediaRecorder is deliberately not used, see the
 * header of public/worklets/pcm-recorder.worklet.js for the reason.
 */

import { FRAME_SAMPLES, SAMPLE_RATE } from "@/lib/config";
import type { StreamKind } from "@/lib/types";
import { rmsFromInt16, smoothLevel } from "./pcm";

export type { StreamKind };

const WORKLET_URL = "/worklets/pcm-recorder.worklet.js";
const WORKLET_NAME = "pcm-recorder";

/**
 * Level callbacks are throttled to roughly 30 per second. Frames arrive every 32 ms at
 * 16 kHz with 512 sample frames, so this lets every frame through at the default settings
 * and only starts dropping if the frame size is lowered.
 */
const LEVEL_MIN_INTERVAL_MS = 30;

export type CaptureErrorCode =
  | "no_audio_track"
  | "permission"
  | "unsupported"
  | "no_device"
  | "worklet";

export class CaptureError extends Error {
  readonly code: CaptureErrorCode;

  constructor(code: CaptureErrorCode, message: string) {
    super(message);
    this.name = "CaptureError";
    this.code = code;
  }
}

export interface CaptureHandle {
  readonly kind: StreamKind;
  readonly label: string;
  readonly muted: boolean;
  setMuted(v: boolean): void;
  stop(): void;
}

export interface CaptureOptions {
  kind: StreamKind;
  deviceId?: string;
  /** Capture the prospect from a shared tab or screen instead of an input device. */
  useDisplayMedia?: boolean;
  /** One framed Int16 LE buffer at SAMPLE_RATE. Not called while muted. */
  onFrame(pcm: ArrayBuffer): void;
  /** Smoothed level, 0..1, about 30 times a second. Keeps running while muted. */
  onLevel(rms: number): void;
  /** The source went away on its own, for example Chrome's "Stop sharing" button. */
  onEnded(): void;
}

/* ===== Audio context ===== */

interface LegacyWindow {
  webkitAudioContext?: typeof AudioContext;
}

let sharedContext: AudioContext | null = null;

/**
 * One AudioContext for the whole app. Browsers cap how many a page may open, and both
 * streams have to share the graph anyway.
 *
 * No sampleRate is requested: forcing 16000 makes Chrome resample the hardware input with
 * its own resampler on some machines and outright fails on others. The hardware rate is
 * kept and the worklet does the conversion.
 */
export function getAudioContext(): AudioContext {
  if (typeof window === "undefined") {
    throw new CaptureError("unsupported", "Audio capture is only available in the browser.");
  }

  if (sharedContext !== null && sharedContext.state !== "closed") {
    return sharedContext;
  }

  const Ctor: typeof AudioContext | undefined =
    window.AudioContext ?? (window as unknown as LegacyWindow).webkitAudioContext;

  if (typeof Ctor !== "function") {
    throw new CaptureError(
      "unsupported",
      "This browser has no Web Audio support, so live capture cannot run. Use a recent Chrome, Edge or Firefox.",
    );
  }

  sharedContext = new Ctor({ latencyHint: "interactive" });
  return sharedContext;
}

/** Kept as an alias because the contract names the shared context factory both ways. */
export function createAudioContext(): AudioContext {
  return getAudioContext();
}

/**
 * addModule() must run exactly once per context. React StrictMode mounts every effect
 * twice in development, and two concurrent addModule() calls would register the same
 * processor name twice, which throws. The promise is cached, keyed by context so a caller
 * that brings its own AudioContext still gets correct behaviour.
 */
const workletReady = new WeakMap<BaseAudioContext, Promise<void>>();

async function ensureWorklet(ctx: AudioContext): Promise<void> {
  const cached = workletReady.get(ctx);
  if (cached) {
    return cached;
  }

  const pending = ctx.audioWorklet
    .addModule(WORKLET_URL)
    .catch((err: unknown) => {
      // Drop the cache so a transient network failure can be retried by the next click.
      workletReady.delete(ctx);
      throw new CaptureError(
        "worklet",
        `Could not load the audio worklet at ${WORKLET_URL}. ${describe(err)}`,
      );
    });

  workletReady.set(ctx, pending);
  return pending;
}

/* ===== Errors ===== */

function describe(err: unknown): string {
  if (err instanceof Error && err.message) {
    return err.message;
  }
  if (typeof err === "string" && err) {
    return err;
  }
  return "Unknown error.";
}

function nameOf(err: unknown): string {
  if (err !== null && typeof err === "object" && "name" in err) {
    const name = (err as { name?: unknown }).name;
    if (typeof name === "string") {
      return name;
    }
  }
  return "";
}

type MediaSourceKind = "display" | "microphone";

function mapMediaError(err: unknown, source: MediaSourceKind): CaptureError {
  if (err instanceof CaptureError) {
    return err;
  }

  const name = nameOf(err);
  const detail = describe(err);

  switch (name) {
    case "NotAllowedError":
    case "PermissionDeniedError":
    case "SecurityError":
      return new CaptureError(
        "permission",
        source === "display"
          ? "Screen or tab sharing was blocked or cancelled. Start it again and pick a tab or the entire screen."
          : "Microphone permission was denied. Allow the microphone for this site, then start capture again.",
      );

    case "NotFoundError":
    case "DevicesNotFoundError":
    case "OverconstrainedError":
    case "ConstraintNotSatisfiedError":
      return new CaptureError(
        "no_device",
        "That audio input is not available any more. Refresh the device list and pick another one.",
      );

    case "NotReadableError":
    case "TrackStartError":
    case "AbortError":
      return new CaptureError(
        "no_device",
        `The audio device could not be opened, another app may be holding it. ${detail}`,
      );

    case "TypeError":
    case "NotSupportedError":
      return new CaptureError("unsupported", `This browser refused the capture request. ${detail}`);

    default:
      return new CaptureError("unsupported", `Audio capture failed. ${detail}`);
  }
}

function mediaDevicesOrThrow(): MediaDevices {
  if (typeof navigator === "undefined" || !navigator.mediaDevices) {
    throw new CaptureError(
      "unsupported",
      "navigator.mediaDevices is missing. getUserMedia needs a secure context, so open the app over https or on localhost.",
    );
  }
  return navigator.mediaDevices;
}

function stopTracks(stream: MediaStream): void {
  for (const track of stream.getTracks()) {
    try {
      track.stop();
    } catch {
      // Already ended, nothing to do.
    }
  }
}

/* ===== Stream acquisition ===== */

async function openDisplayStream(): Promise<MediaStream> {
  const media = mediaDevicesOrThrow();
  if (typeof media.getDisplayMedia !== "function") {
    throw new CaptureError(
      "unsupported",
      "This browser cannot capture tab or system audio. Use Chrome or Edge on the desktop, or route the call through a virtual audio cable.",
    );
  }

  let stream: MediaStream;
  try {
    // Chrome refuses an audio only display capture, so video is requested and thrown away
    // immediately. The three processing flags are off because the prospect's voice must
    // reach the model unmangled.
    stream = await media.getDisplayMedia({
      video: true,
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
    });
  } catch (err) {
    throw mapMediaError(err, "display");
  }

  for (const track of stream.getVideoTracks()) {
    try {
      track.stop();
    } catch {
      // Nothing to clean up if it never started.
    }
    stream.removeTrack(track);
  }

  if (stream.getAudioTracks().length === 0) {
    stopTracks(stream);
    throw new CaptureError(
      "no_audio_track",
      "No audio track. In the Chrome picker choose a Tab or Entire Screen and tick Share tab audio.",
    );
  }

  return stream;
}

async function openDeviceStream(kind: StreamKind, deviceId?: string): Promise<MediaStream> {
  const media = mediaDevicesOrThrow();
  if (typeof media.getUserMedia !== "function") {
    throw new CaptureError(
      "unsupported",
      "getUserMedia is not available. It needs a secure context, so open the app over https or on localhost.",
    );
  }

  const audio: MediaTrackConstraints =
    kind === "rep"
      ? {
          // The rep's own mic wants the browser's cleanup: it removes the prospect's voice
          // leaking back in from the speakers.
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        }
      : {
          // A virtual cable is already a clean digital feed. Any processing here would gate
          // the prospect's speech and confuse the server side VAD.
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        };

  if (deviceId) {
    audio.deviceId = { exact: deviceId };
  }

  try {
    return await media.getUserMedia({ audio, video: false });
  } catch (err) {
    throw mapMediaError(err, "microphone");
  }
}

/* ===== Device list ===== */

/**
 * Audio input devices for the picker.
 *
 * Labels are empty strings until the user has granted microphone permission at least once,
 * so the UI should call this again right after a successful capture to get real names.
 *
 * Chrome also injects two alias entries, "default" and "communications", that point at a
 * real device. They are dropped only when the device they alias is present in the same
 * list (matched by groupId), otherwise they are kept, because on some setups the alias is
 * the only entry there is.
 */
export async function listInputDevices(): Promise<MediaDeviceInfo[]> {
  if (typeof navigator === "undefined" || !navigator.mediaDevices) {
    return [];
  }
  if (typeof navigator.mediaDevices.enumerateDevices !== "function") {
    return [];
  }

  let devices: MediaDeviceInfo[];
  try {
    devices = await navigator.mediaDevices.enumerateDevices();
  } catch {
    return [];
  }

  const inputs = devices.filter((device) => device.kind === "audioinput");
  const aliasIds = new Set(["default", "communications"]);

  const realGroups = new Set(
    inputs
      .filter((device) => !aliasIds.has(device.deviceId) && device.groupId)
      .map((device) => device.groupId),
  );

  return inputs.filter((device) => {
    if (!aliasIds.has(device.deviceId)) {
      return true;
    }
    return !(device.groupId && realGroups.has(device.groupId));
  });
}

/* ===== Capture ===== */

function defaultLabel(kind: StreamKind, useDisplayMedia: boolean): string {
  if (useDisplayMedia) {
    return "Shared tab or screen";
  }
  return kind === "rep" ? "Microphone" : "Audio input";
}

export async function startCapture(opts: CaptureOptions): Promise<CaptureHandle>;
export async function startCapture(
  ctx: AudioContext,
  opts: CaptureOptions,
): Promise<CaptureHandle>;
export async function startCapture(
  first: AudioContext | CaptureOptions,
  second?: CaptureOptions,
): Promise<CaptureHandle> {
  const opts: CaptureOptions = second ?? (first as CaptureOptions);
  const providedContext = second === undefined ? null : (first as AudioContext);

  const useDisplayMedia = opts.useDisplayMedia === true;

  // The media prompt comes first, while the click that triggered it is still fresh. Loading
  // the worklet before it would burn the user activation Chrome needs for getDisplayMedia.
  const stream = useDisplayMedia
    ? await openDisplayStream()
    : await openDeviceStream(opts.kind, opts.deviceId);

  let ctx: AudioContext;
  let node: AudioWorkletNode;
  let source: MediaStreamAudioSourceNode;
  let sink: GainNode;

  try {
    ctx = providedContext ?? getAudioContext();

    // Autoplay policy parks a fresh context in "suspended". resume() only works inside a
    // user gesture, and startCapture is always reached from a click handler.
    if (ctx.state === "suspended") {
      await ctx.resume();
    }

    await ensureWorklet(ctx);

    source = ctx.createMediaStreamSource(stream);
    node = new AudioWorkletNode(ctx, WORKLET_NAME, {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      processorOptions: {
        targetRate: SAMPLE_RATE,
        frameSamples: FRAME_SAMPLES,
      },
    });
    sink = ctx.createGain();
    sink.gain.value = 0;
  } catch (err) {
    stopTracks(stream);
    if (err instanceof CaptureError) {
      throw err;
    }
    throw mapMediaError(err, useDisplayMedia ? "display" : "microphone");
  }

  // source -> worklet -> gain(0) -> destination.
  //
  // The silent gain node is the point of that last hop. Chrome stops scheduling a worklet
  // whose output reaches nothing, so frames would simply stop arriving. Connecting the
  // worklet straight to ctx.destination would fix the scheduling too, but it would play the
  // prospect out of the rep's speakers, straight back into the rep's microphone. Zero gain
  // keeps the node alive and the room quiet.
  source.connect(node);
  node.connect(sink);
  sink.connect(ctx.destination);

  const audioTracks = stream.getAudioTracks();
  const label = audioTracks[0]?.label || defaultLabel(opts.kind, useDisplayMedia);

  let muted = false;
  let disposed = false;
  let level = 0;
  let lastLevelAt = 0;

  const dispose = (): void => {
    if (disposed) {
      return;
    }
    disposed = true;

    node.port.onmessage = null;
    try {
      node.port.close();
    } catch {
      // The port is already gone.
    }

    for (const track of stream.getTracks()) {
      track.removeEventListener("ended", handleEnded);
    }

    try {
      source.disconnect();
    } catch {
      // Already disconnected.
    }
    try {
      node.disconnect();
    } catch {
      // Already disconnected.
    }
    try {
      sink.disconnect();
    } catch {
      // Already disconnected.
    }

    stopTracks(stream);
  };

  function handleEnded(): void {
    if (disposed) {
      return;
    }
    // Fired when the user hits Chrome's "Stop sharing" bar or unplugs the interface. The
    // teardown sits in a finally so a throwing callback cannot leave the graph running.
    try {
      opts.onEnded();
    } finally {
      dispose();
    }
  }

  for (const track of audioTracks) {
    track.addEventListener("ended", handleEnded);
  }

  node.port.onmessage = (event: MessageEvent) => {
    if (disposed) {
      return;
    }

    const data: unknown = event.data;
    if (!(data instanceof ArrayBuffer) || data.byteLength === 0) {
      return;
    }

    // RMS is read before the frame is handed on, because the consumer is free to do
    // anything it likes with the buffer.
    level = smoothLevel(level, rmsFromInt16(data));

    // Timestamp compare, not a timer: a setInterval here would keep firing after the last
    // frame and would fight the audio thread's own cadence.
    const now =
      typeof performance !== "undefined" ? performance.now() : Date.now();
    if (now - lastLevelAt >= LEVEL_MIN_INTERVAL_MS) {
      lastLevelAt = now;
      opts.onLevel(level);
    }

    // Muting stops the upload but keeps the meter alive, so the rep can still see that the
    // line is live while muted.
    if (!muted) {
      opts.onFrame(data);
    }
  };

  const handle: CaptureHandle = {
    kind: opts.kind,
    get label(): string {
      return label;
    },
    get muted(): boolean {
      return muted;
    },
    setMuted(value: boolean): void {
      muted = value;
    },
    stop(): void {
      dispose();
    },
  };

  return handle;
}
