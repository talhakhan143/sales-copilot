"use client";

/**
 * useTeleprompter: the single hook the call dashboard hangs off.
 *
 * It owns the WebSocket, both capture handles, and every piece of live call
 * state. Nothing else in the app is allowed to open a socket or a MediaStream.
 *
 * Shape returned: everything in CONTRACT.md section 4.4, plus these ADDITIVE
 * fields, which the frozen component props need and which nothing in 4.4
 * provided a home for:
 *
 *   quickActions   QuickAction[]        from the `ready` frame, feeds ObjectionBar
 *   connState      ConnState            raw socket lifecycle, for the top bar chip
 *   droppedFrames  number               audio frames dropped by backpressure
 *   devices        MediaDeviceInfo[]    input devices for AudioSourcePicker
 *   refreshDevices ()  => void          re enumerate after a permission grant
 *   muted          {client, rep}        mute flags for AudioSourcePicker
 *   busy           StreamKind | null    a start is in flight, disable that row
 *   sensitivity    number               current VAD multiplier, 0.5 to 3.0
 *   trigger        SuggestionTrigger|null   what caused the shown suggestion
 *   sourceText     string | null        the prospect line that caused it
 *   practice       PracticeApi          rehearsal state and its three actions
 *   startPractice  () => void           the same three actions, also at the top
 *   endPractice    () => void           level, so a call site can destructure
 *   cutIn          () => void           either way
 *   call           CallSnapshot         the phone call, from the call_state frame
 *   applyCallState (CallSnapshot) => void   seed that from a status read
 *
 * Nothing in 4.4 was renamed or removed, so any component written against the
 * contract keeps working.
 *
 * CALLING (CONTRACT_CALLING.md section 2.3)
 *
 * The server pushes one extra frame, `call_state`, whenever the phone call
 * changes: the provider, the state (idle, dialing, ringing, live, ended,
 * failed), the provider's own call id and one plain line of detail. It lands on
 * `call` and on nothing else. A ringing phone is not copilot work, so it never
 * moves the status pill, never touches the glass, and never clears an answer the
 * rep may still be reading. A session that never places a call stays on the idle
 * snapshot for its whole life, so the live path is exactly what it was before.
 *
 * That frame is pushed only when the call MOVES, so a browser that opens in the
 * middle of a call that is already running would hear nothing until it ends. The
 * page covers that gap by reading GET /api/call/{id}/status once when it opens
 * and handing the answer to `applyCallState`. A pushed frame always beats that
 * read, so a reload can restore the call state but can never rewrite it.
 *
 * PRACTICE MODE (CONTRACT_PRACTICE.md sections 3 and 6.1)
 *
 * The second argument turns the rehearsal on. It is off unless the caller asks
 * for it, and with it off this hook does exactly what it did before: a live
 * call never receives a client_turn frame, so nothing below can fire.
 *
 * A practice call is half duplex on purpose. When a client_turn arrives the
 * browser says the line out loud, and while it is talking the rep's microphone
 * frames are held back so the synthetic client is not transcribed as the rep.
 * The gate is a ref, not state, because it is read inside the capture callback
 * about thirty times a second and a state value read there would be the one
 * captured when the stream started, which is always false. cutIn() cuts the
 * speech off mid word and hands the microphone straight back, because talking
 * over a prospect is a real cold call skill.
 *
 * Performance rules held here, in order of how badly they bite:
 *   1. Tokens never call setState directly. suggestion_delta appends to a ref
 *      and schedules ONE animation frame, which flushes the ref into state. At
 *      60 tokens a second that is 60 string appends and about 60 paints instead
 *      of 60 React commits with layout in between. The flush carries whole
 *      words only: a delta is cut at the last space or newline and the tail is
 *      held back, so a half arrived token never reaches the glass.
 *   2. vad frames arrive every 80 ms per stream, and the local level callback
 *      fires around 30 times a second. Both write to a ref and share one frame.
 *   3. The socket callbacks read nothing from the render closure. They go
 *      through a ref that is refreshed after every render, so no handler can
 *      ever see stale state, and the socket effect can depend on sessionId only.
 *   4. Everything returned that is a function is a stable useCallback, so a
 *      memoised child such as the objection bar gets identical props on a
 *      token and bails out of its own render.
 *   5. The practice clock is one interval, not one per component. It is cleared
 *      when the call ends and when the hook unmounts.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { CaptureError, listInputDevices, startCapture } from "@/lib/audio/capture";
import type { CaptureHandle } from "@/lib/audio/capture";
import { teleprompterWsUrl } from "@/lib/config";
import { cancelSpeech, primeSpeech, rateForDifficulty, speak } from "@/lib/practice/speech";
import { loadPromptStyle, savePromptStyle } from "@/lib/profile";
import { TeleprompterSocket } from "@/lib/ws/client";
import type {
  CallProvider,
  CallState,
  CallStatus,
  ConnState,
  Difficulty,
  PracticeOverReason,
  PromptStyle,
  QuickAction,
  ServerMessage,
  StreamKind,
  SuggestionTrigger,
  TranscriptItem,
} from "@/lib/types";

/** Where the prospect audio comes from. */
export type ClientSource = { type: "display" } | { type: "device"; deviceId: string };

export interface StreamFlags {
  client: boolean;
  rep: boolean;
}

export interface StreamLevels {
  client: number;
  rep: number;
}

export interface LatencySnapshot {
  stt: number | null;
  firstToken: number | null;
  total: number | null;
  rtt: number | null;
}

/**
 * Where the phone call is right now.
 *
 * These are the four fields of the `call_state` frame, unchanged. The call page
 * shows `state` as a chip in the top bar and `detail` as the reason when the
 * state is failed, so both are kept exactly as the server sent them.
 */
export interface CallSnapshot {
  /** Which way the call was placed, or the default before one is placed. */
  provider: CallProvider;
  /** idle, dialing, ringing, live, ended or failed. */
  state: CallState;
  /** The provider's own id for the call, for example a Twilio call sid. */
  callId: string | null;
  /** One plain line about the state, normally only set on failed. */
  detail: string | null;
}

/**
 * How the caller asks for a rehearsal instead of a live call.
 *
 * Every field is optional and the whole object is optional, so an existing call
 * site that passes only a session id keeps the live behaviour it has today.
 */
export interface TeleprompterOptions {
  /** True when this session is a rehearsal against the synthetic client. */
  practice?: boolean;
  /** How hard the synthetic client is. It sets how fast the voice talks. */
  difficulty?: Difficulty;
  /**
   * Which language the client is spoken in, for example "en" or "ur". Pass the
   * language the session was prepared with. Defaults to English.
   */
  language?: string;
}

/** Everything the practice strip and the score card overlay need. */
export interface PracticeApi {
  /** True when this session is a rehearsal. False on a real call. */
  active: boolean;
  /** True once the rep has pressed start and the client has been asked to talk. */
  started: boolean;
  /** True once the call is finished, whoever finished it. */
  over: boolean;
  /** Why it finished, or null while it is still running. */
  reason: PracticeOverReason | null;
  /** How many turns the server has counted so far. */
  turns: number;
  /** The level this call was set up with. */
  difficulty: Difficulty;
  /** True while the browser is saying the client's line out loud. */
  clientSpeaking: boolean;
  /** The last line the client said, or null before the first one. */
  clientLine: string | null;
  /** Milliseconds since the rep pressed start. Frozen when the call ends. */
  elapsedMs: number;
  /** Prime the voice engine and ask the client to speak first. */
  startPractice(): Promise<void>;
  /** Stop the call and ask the server for a score. */
  endPractice(): void;
  /** Cut the client off mid word and take the microphone back. */
  cutIn(): void;
}

export interface TeleprompterApi {
  status: CallStatus;
  suggestion: string;
  streaming: boolean;
  trigger: SuggestionTrigger | null;
  sourceText: string | null;
  transcript: TranscriptItem[];
  levels: StreamLevels;
  speaking: StreamFlags;
  latency: LatencySnapshot;
  captures: StreamFlags;
  muted: StreamFlags;
  quickActions: QuickAction[];
  devices: MediaDeviceInfo[];
  connState: ConnState;
  droppedFrames: number;
  sensitivity: number;
  /** Whether the copilot writes a whole line or a few points. */
  promptStyle: PromptStyle;
  /** Switch between a line to read and points to speak around. */
  setPromptStyle(next: PromptStyle): void;
  busy: StreamKind | null;
  error: string | null;
  call: CallSnapshot;
  practice: PracticeApi;
  startClient(source: ClientSource): Promise<void>;
  startRep(deviceId?: string): Promise<void>;
  stopStream(kind: StreamKind): void;
  toggleMute(kind: StreamKind): void;
  quickAction(key: string, note?: string): void;
  sendManual(text: string, stream?: StreamKind): void;
  setSensitivity(v: number): void;
  refreshDevices(): void;
  reset(): void;
  /**
   * Write the call state that a one off status read just brought back.
   *
   * The server pushes a `call_state` frame only when the call MOVES, so a
   * browser that opened in the middle of a call that is already running is told
   * nothing at all until it ends. That browser reads the state once from
   * GET /api/call/{id}/status and hands the answer here, which is what brings
   * the chip and the launcher back after a reload.
   *
   * A frame the socket already pushed always wins over this, so an answer that
   * was slow to arrive can never put a stale word back on the bar.
   */
  applyCallState(snapshot: CallSnapshot): void;
  /**
   * The three practice actions are the same function objects that sit on
   * `practice`. They are here as well so a component can take them straight off
   * the hook, next to quickAction and reset, without unpacking practice first.
   */
  startPractice(): Promise<void>;
  endPractice(): void;
  cutIn(): void;
}

/** Newest rows win. Sixty is about ten minutes of a real call. */
const MAX_TRANSCRIPT = 60;

/** How often we copy the socket drop counter into React state. */
const DROP_POLL_MS = 1000;

/**
 * How often the practice clock updates.
 *
 * The strip shows whole seconds, and a one second interval drifts just enough
 * to skip a second now and then, which looks like a broken clock. Twice a
 * second is two renders a second, which is nothing next to a token stream.
 */
const PRACTICE_TICK_MS = 500;

/** The level used when the caller did not name one. */
const DEFAULT_DIFFICULTY: Difficulty = "normal";

/**
 * The call snapshot for a session that has not placed a call.
 *
 * Same values the backend starts a session with, so the chip in the top bar says
 * the same thing whether it is reading this constant or a frame from the server.
 */
const IDLE_CALL: CallSnapshot = {
  provider: "manual",
  state: "idle",
  callId: null,
  detail: null,
};

const MIN_SENSITIVITY = 0.5;
const MAX_SENSITIVITY = 3;

const SERVER_STATUS_TO_CALL_STATUS: Record<
  "idle" | "listening" | "transcribing" | "thinking",
  CallStatus
> = {
  idle: "ready",
  listening: "listening",
  transcribing: "transcribing",
  thinking: "thinking",
};

const VIRTUAL_CABLE_HINT =
  "On a Mac, install BlackHole 2ch, send the call app sound into it, then pick BlackHole as the client input here. On Windows, VB Cable does the same job.";

/**
 * Pull the code off a capture failure.
 *
 * The instanceof check is the normal path. The duck typed fallback covers the
 * case where the error crossed a bundle boundary and lost its prototype, which
 * would otherwise silently downgrade every message to the generic one.
 */
function captureErrorCode(err: unknown): string | null {
  if (err instanceof CaptureError) return err.code;
  if (typeof err === "object" && err !== null && "code" in err) {
    const code = (err as { code?: unknown }).code;
    if (typeof code === "string") return code;
  }
  return null;
}

/** One clear sentence per failure, each one telling the user the next move. */
function describeCaptureError(err: unknown, kind: StreamKind): string {
  const code = captureErrorCode(err);

  switch (code) {
    case "no_audio_track":
      return `That share came through with no audio track. In the Chrome dialog pick a Tab or Entire Screen, then tick the "Share tab audio" box at the bottom left before you hit Share. ${VIRTUAL_CABLE_HINT}`;
    case "permission":
      return kind === "rep"
        ? "The browser blocked the microphone. Click the padlock next to the address bar, set Microphone to Allow, then start the mic again."
        : "The browser blocked the capture. Click the padlock next to the address bar, allow this site to record, then try sharing again. If you clicked Cancel in the picker, just start it again.";
    case "unsupported":
      return "This browser cannot capture that source. Use desktop Chrome or Edge, Safari and Firefox do not hand tab audio to a web page.";
    case "no_device":
      return `That input is gone, so nothing can be captured from it. Plug the device back in or choose another one, then press refresh on the device list. ${VIRTUAL_CABLE_HINT}`;
    case "worklet":
      return "The audio engine failed to load. Do a hard reload of this page (Cmd Shift R, or Ctrl Shift R on Windows) so the recorder worklet is fetched again.";
    default:
      break;
  }

  const detail = err instanceof Error && err.message ? err.message : "Unknown capture failure.";
  return `Could not start the ${kind === "client" ? "client" : "microphone"} stream. ${detail}`;
}

/** Set one lane on a two lane flag object without a computed key. */
function withFlag(prev: StreamFlags, kind: StreamKind, value: boolean): StreamFlags {
  return kind === "client" ? { ...prev, client: value } : { ...prev, rep: value };
}

export function useTeleprompter(
  sessionId: string | null,
  options?: TeleprompterOptions,
): TeleprompterApi {
  // Read as plain values, never as an object, because the caller almost
  // certainly passes a fresh object literal on every render and any dependency
  // array holding it would rerun on every render.
  const practiceEnabled = options?.practice === true;
  const difficulty: Difficulty = options?.difficulty ?? DEFAULT_DIFFICULTY;
  const speechLanguage = options?.language ?? "en";

  const [status, setStatus] = useState<CallStatus>(sessionId ? "connecting" : "closed");
  const [connState, setConnState] = useState<ConnState>("idle");
  const [suggestion, setSuggestion] = useState<string>("");
  const [streaming, setStreaming] = useState<boolean>(false);
  const [trigger, setTrigger] = useState<SuggestionTrigger | null>(null);
  const [sourceText, setSourceText] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptItem[]>([]);
  const [levels, setLevels] = useState<StreamLevels>({ client: 0, rep: 0 });
  const [speaking, setSpeaking] = useState<StreamFlags>({ client: false, rep: false });
  const [latency, setLatency] = useState<LatencySnapshot>({
    stt: null,
    firstToken: null,
    total: null,
    rtt: null,
  });
  const [captures, setCaptures] = useState<StreamFlags>({ client: false, rep: false });
  const [muted, setMuted] = useState<StreamFlags>({ client: false, rep: false });
  const [quickActions, setQuickActions] = useState<QuickAction[]>([]);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [droppedFrames, setDroppedFrames] = useState<number>(0);
  const [sensitivity, setSensitivityValue] = useState<number>(1);
  const [promptStyle, setPromptStyleValue] = useState<PromptStyle>("full");
  const [busy, setBusy] = useState<StreamKind | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [call, setCall] = useState<CallSnapshot>(IDLE_CALL);

  /* ---------------- practice mode state ---------------- */

  const [practiceStarted, setPracticeStarted] = useState<boolean>(false);
  const [practiceOver, setPracticeOver] = useState<boolean>(false);
  const [practiceReason, setPracticeReason] = useState<PracticeOverReason | null>(null);
  const [practiceTurns, setPracticeTurns] = useState<number>(0);
  const [clientSpeaking, setClientSpeaking] = useState<boolean>(false);
  const [clientLine, setClientLine] = useState<string | null>(null);
  const [elapsedMs, setElapsedMs] = useState<number>(0);

  /* ---------------------------------------------------------------- */
  /* refs: everything that must not trigger a render                   */
  /* ---------------------------------------------------------------- */

  const socketRef = useRef<TeleprompterSocket | null>(null);
  const socketKeyRef = useRef<string | null>(null);
  const captureRef = useRef<{ client: CaptureHandle | null; rep: CaptureHandle | null }>({
    client: null,
    rep: null,
  });

  /**
   * Bumped by every teardown.
   *
   * startCapture can sit for many seconds inside the Chrome share picker or the
   * microphone permission prompt. If the hook is torn down while that promise
   * is in flight, the handle it eventually resolves with belongs to nobody: the
   * teardown that would have stopped it already ran. Comparing this counter
   * across the await is what lets the late resolution stop its own stream
   * instead of leaving a live MediaStream and a self feeding rAF loop behind.
   */
  const teardownGenRef = useRef<number>(0);

  /**
   * The streaming answer, split at the last word boundary.
   *
   * `committedRef` holds whole words only and is the only string that ever
   * reaches state. `pendingRef` holds the tail that no space or newline has
   * closed yet, so a partial token like "recomm" is never painted and no glyph
   * under the reading eye ever mutates. DESIGN.md section 5.4 STREAMING and
   * invariant 2, which are binding.
   */
  const committedRef = useRef<string>("");
  const pendingRef = useRef<string>("");
  const currentIdRef = useRef<string | null>(null);
  const textRafRef = useRef<number | null>(null);

  const vadRafRef = useRef<number | null>(null);
  const pendingLevelsRef = useRef<StreamLevels>({ client: 0, rep: 0 });
  const pendingSpeakingRef = useRef<StreamFlags>({ client: false, rep: false });

  const busyRef = useRef<StreamKind | null>(null);
  const sensitivityRef = useRef<number>(1);
  /* Read inside the ready handler, which runs from a socket callback, so it
     has to be a ref as well as state or a reconnect would resend the style the
     hook started with rather than the one the rep picked. */
  const promptStyleRef = useRef<PromptStyle>("full");
  const capturesRef = useRef<StreamFlags>({ client: false, rep: false });

  /**
   * True once the server has pushed a `call_state` frame for this session.
   *
   * The status read the page does on open is a photograph of a moment that has
   * already passed by the time it lands. A frame is live. So the frame wins:
   * once one has arrived, `applyCallState` stops writing. It is a ref because
   * nothing on screen depends on it, and it is cleared by the teardown below,
   * because a new session id is a new call.
   */
  const callPushedRef = useRef<boolean>(false);

  /**
   * The half duplex gate. True means the browser is talking, so nothing the
   * microphone hears may be sent.
   *
   * It has to be a ref. The capture callback below is created once, when the
   * stream starts, and a state value read inside it would be frozen at whatever
   * it was on that render, which is always false. On a live call this never
   * turns true, because only a client_turn frame can close it, so the audio path
   * behaves exactly as it did before practice mode existed.
   */
  const micGateRef = useRef<boolean>(false);

  /**
   * One counter per spoken line, so a callback from a line that was already
   * cancelled cannot open the gate under the line that replaced it.
   *
   * speak() cancels whatever is speaking first, and that cancel fires the old
   * line's onEnd synchronously. Without this check that stale onEnd would send
   * "not speaking" a moment after we sent "speaking" for the new line, and the
   * microphone would stay live while the client talked over it.
   */
  const speechSeqRef = useRef<number>(0);

  /** Voice settings, held in a ref so the speak helper stays a stable callback. */
  const speechSettingsRef = useRef<{ lang: string; rate: number }>({
    lang: "en",
    rate: rateForDifficulty(DEFAULT_DIFFICULTY),
  });

  /** When the rep pressed start, in epoch milliseconds. Null before that. */
  const practiceStartedAtRef = useRef<number | null>(null);

  /* ---------------------------------------------------------------- */
  /* frame batched flushes                                             */
  /* ---------------------------------------------------------------- */

  const cancelTextFrame = useCallback(() => {
    if (textRafRef.current !== null) {
      cancelAnimationFrame(textRafRef.current);
      textRafRef.current = null;
    }
  }, []);

  const scheduleTextFlush = useCallback(() => {
    if (textRafRef.current !== null) return;
    textRafRef.current = requestAnimationFrame(() => {
      textRafRef.current = null;
      setSuggestion(committedRef.current);
    });
  }, []);

  const cancelVadFrame = useCallback(() => {
    if (vadRafRef.current !== null) {
      cancelAnimationFrame(vadRafRef.current);
      vadRafRef.current = null;
    }
  }, []);

  const scheduleVadFlush = useCallback(() => {
    if (vadRafRef.current !== null) return;
    vadRafRef.current = requestAnimationFrame(() => {
      vadRafRef.current = null;
      const nextLevels = pendingLevelsRef.current;
      const nextSpeaking = pendingSpeakingRef.current;
      setLevels((prev) =>
        prev.client === nextLevels.client && prev.rep === nextLevels.rep
          ? prev
          : { client: nextLevels.client, rep: nextLevels.rep },
      );
      setSpeaking((prev) =>
        prev.client === nextSpeaking.client && prev.rep === nextSpeaking.rep
          ? prev
          : { client: nextSpeaking.client, rep: nextSpeaking.rep },
      );
    });
  }, []);

  /* ---------------------------------------------------------------- */
  /* transcript                                                        */
  /* ---------------------------------------------------------------- */

  /**
   * Append one row, capped at MAX_TRANSCRIPT with the oldest dropped.
   *
   * The id is already role prefixed by the caller, because the backend counter
   * is per session and a client transcript can share a number with a copilot
   * turn. Same id twice replaces the row instead of duplicating a React key.
   */
  const pushTranscript = useCallback((item: TranscriptItem) => {
    setTranscript((prev) => {
      const next = prev.length > 0 ? prev.filter((row) => row.id !== item.id) : [];
      next.push(item);
      return next.length > MAX_TRANSCRIPT ? next.slice(next.length - MAX_TRANSCRIPT) : next;
    });
  }, []);

  /* ---------------------------------------------------------------- */
  /* practice: the voice and the half duplex gate                      */
  /* ---------------------------------------------------------------- */

  // The two settings the voice needs are copied into a ref so the speak helper
  // below can stay a stable callback. They change at most once per session.
  useEffect(() => {
    speechSettingsRef.current = {
      lang: speechLanguage,
      rate: rateForDifficulty(difficulty),
    };
  }, [speechLanguage, difficulty]);

  /**
   * Shut the microphone gate and tell the server we are talking.
   *
   * The server ignores rep audio while this is true, and we stop sending it as
   * well, so the client's own voice can never be transcribed as the rep.
   */
  const closeMicGate = useCallback(() => {
    if (micGateRef.current) return;
    micGateRef.current = true;
    setClientSpeaking(true);
    socketRef.current?.send({ type: "speech_state", speaking: true });
  }, []);

  /** Open the gate again. Safe to call twice, the second call does nothing. */
  const openMicGate = useCallback(() => {
    if (!micGateRef.current) {
      // The ref is also cleared straight out by the socket teardown, so the
      // "client is talking" lamp is put out here as well in case the two ever
      // disagree. React drops this when the value is already false.
      setClientSpeaking(false);
      return;
    }
    micGateRef.current = false;
    setClientSpeaking(false);
    socketRef.current?.send({ type: "speech_state", speaking: false });
  }, []);

  /**
   * Say one client line out loud and hold the microphone while it plays.
   *
   * The gate is shut before the line is handed to the engine, not from the
   * engine's own start event, because muting a moment early costs nothing and
   * muting late lets the speakers into the microphone.
   */
  const speakClientLine = useCallback(
    (text: string) => {
      const seq = speechSeqRef.current + 1;
      speechSeqRef.current = seq;

      const settings = speechSettingsRef.current;
      closeMicGate();

      speak({
        text,
        lang: settings.lang,
        rate: settings.rate,
        onStart: () => {
          if (speechSeqRef.current !== seq) return;
          closeMicGate();
        },
        onEnd: () => {
          if (speechSeqRef.current !== seq) return;
          openMicGate();
        },
        onError: (message: string) => {
          if (speechSeqRef.current === seq) openMicGate();
          // The line is still on the glass and in the rail, so the rehearsal
          // carries on without a voice instead of stopping.
          setError(message);
        },
      });
    },
    [closeMicGate, openMicGate],
  );

  /* ---------------------------------------------------------------- */
  /* socket callbacks (ref driven, never stale)                        */
  /* ---------------------------------------------------------------- */

  const handleServerMessage = useCallback(
    (msg: ServerMessage) => {
      switch (msg.type) {
        case "ready": {
          setQuickActions(msg.quickActions);
          setStatus("ready");
          setError(null);
          // A reconnect gives us a fresh server side connection, so tell it
          // again what we are sending and how sensitive the VAD should be.
          const socket = socketRef.current;
          if (socket) {
            if (sensitivityRef.current !== 1) {
              socket.send({ type: "config", sensitivity: sensitivityRef.current });
            }
            if (promptStyleRef.current !== "full") {
              socket.send({ type: "config", style: promptStyleRef.current });
            }
            if (capturesRef.current.client) {
              socket.send({ type: "control", action: "start", stream: "client" });
            }
            if (capturesRef.current.rep) {
              socket.send({ type: "control", action: "start", stream: "rep" });
            }
            // A reconnect that lands while the browser is still saying a client
            // line out loud must not leave the new server side connection
            // thinking the microphone is live.
            if (micGateRef.current) {
              socket.send({ type: "speech_state", speaking: true });
            }
          }
          break;
        }

        case "pong": {
          const rtt = Math.max(0, Date.now() - msg.t);
          setLatency((prev) => ({ ...prev, rtt }));
          break;
        }

        case "vad": {
          pendingLevelsRef.current[msg.stream] = msg.rms;
          pendingSpeakingRef.current[msg.stream] = msg.speaking;
          scheduleVadFlush();
          break;
        }

        case "partial_transcript": {
          // The text of a partial is not shown as its own row, it would fight
          // with the final row that lands a moment later under the same id.
          // It is still a useful signal that STT is in flight.
          setStatus((prev) => (prev === "thinking" ? prev : "transcribing"));
          break;
        }

        case "transcript": {
          if (msg.text.trim().length > 0) {
            pushTranscript({
              id: `${msg.stream}:${msg.id}`,
              role: msg.stream,
              text: msg.text,
              ts: Date.now(),
              ms: msg.ms,
            });
          }
          // Only the prospect lane is on the path that holds up a suggestion.
          // A rep utterance is transcribed for context and answers nothing, so
          // letting its STT time into the meter would show the rep a number
          // measured on their own microphone during the dead air.
          if (msg.stream === "client") {
            setLatency((prev) => ({ ...prev, stt: msg.ms }));
          }
          break;
        }

        case "suggestion_start": {
          // A new answer always wins over whatever frame was still pending.
          cancelTextFrame();
          committedRef.current = "";
          pendingRef.current = "";
          currentIdRef.current = msg.id;
          // The text on the glass is deliberately left alone. During the 600 to
          // 1500 ms it takes the model to produce a first word, the rep may
          // still be reading the previous line out loud, and blanking it here
          // would drop them into the idle copy, which says the machine is
          // waiting for the prospect at the exact moment it is answering them.
          // The first committed word replaces it in one frame.
          // DESIGN.md section 5.4 CUE.
          setStreaming(true);
          setTrigger(msg.trigger);
          setSourceText(msg.sourceText.length > 0 ? msg.sourceText : null);
          setStatus("thinking");
          setLatency((prev) => ({
            ...prev,
            stt: msg.sttMs > 0 ? msg.sttMs : prev.stt,
            firstToken: null,
            total: null,
          }));
          break;
        }

        case "suggestion_delta": {
          // A delta from a cancelled generation must never leak into the text
          // the rep is reading out loud.
          if (msg.id !== currentIdRef.current) break;

          // Commit up to the last word boundary and hold the rest. The model
          // streams sub word pieces, so without this cut the glass would paint
          // "reco", then "recomm", then "recommend", re breaking the line under
          // an eye that is halfway through reading it aloud.
          pendingRef.current += msg.delta;
          const tail = pendingRef.current;
          const cut = Math.max(tail.lastIndexOf(" "), tail.lastIndexOf("\n"));
          if (cut >= 0) {
            committedRef.current += tail.slice(0, cut + 1);
            pendingRef.current = tail.slice(cut + 1);
            scheduleTextFlush();
          }
          break;
        }

        case "suggestion_done": {
          const isCurrent = msg.id === currentIdRef.current;

          if (isCurrent) {
            // The held back tail lands here, inside the server's final text,
            // which is authoritative because it is what got logged as the turn.
            cancelTextFrame();
            committedRef.current = msg.text;
            pendingRef.current = "";
            setSuggestion(msg.text);
            setStreaming(false);
            setStatus("ready");
            setLatency((prev) => ({
              stt: msg.sttMs > 0 ? msg.sttMs : prev.stt,
              firstToken: msg.firstTokenMs,
              total: msg.totalMs,
              rtt: prev.rtt,
            }));
          }

          // Even a barged in answer really was said by the copilot, so it stays
          // in the record. It just never overwrites what is on screen now.
          if (msg.text.trim().length > 0) {
            pushTranscript({
              id: `copilot:${msg.id}`,
              role: "copilot",
              text: msg.text,
              ts: Date.now(),
              ms: msg.totalMs,
            });
          }
          break;
        }

        case "status": {
          setStatus(SERVER_STATUS_TO_CALL_STATUS[msg.state]);
          break;
        }

        case "error": {
          setError(msg.message);
          if (msg.code === "unknown_session" || msg.code === "session_expired") {
            setStatus("error");
          }
          break;
        }

        case "client_turn": {
          // The synthetic client just said something. It goes on the glass, into
          // the rail as a normal prospect row, and out of the speakers. The row
          // id matches the one a real transcript frame would use, so if the
          // server also logs it the two collapse into one row instead of two.
          setClientLine(msg.text);
          if (msg.text.trim().length > 0) {
            pushTranscript({
              id: `client:${msg.id}`,
              role: "client",
              text: msg.text,
              ts: Date.now(),
              ms: msg.ms,
            });
          }
          speakClientLine(msg.text);
          break;
        }

        case "practice_over": {
          setPracticeOver(true);
          setPracticeReason(msg.reason);
          setPracticeTurns(msg.turns);
          // The microphone comes back to the rep, but a line that is still
          // playing is left alone. A hangup arrives as the client's line first
          // and this frame a few milliseconds later, and the rep has to hear
          // the client hang up on them. That line opens the gate itself when it
          // finishes, so nothing stays shut.
          if (!micGateRef.current) openMicGate();
          // Freeze the clock on the last real reading. The interval is about to
          // stop, so without this the strip would keep the value from up to half
          // a second ago.
          const startedAt = practiceStartedAtRef.current;
          if (startedAt !== null) setElapsedMs(Date.now() - startedAt);
          break;
        }

        case "call_state": {
          // The phone call moved. This frame is the only one that says nothing
          // at all about the copilot, so it writes one piece of state and
          // touches nothing else: a phone that starts ringing must not move the
          // status pill, and it must never take a line off the glass while the
          // rep is still reading it out loud.
          //
          // From here on the socket is the only thing allowed to write the call.
          // A status read that is still in flight is already out of date.
          callPushedRef.current = true;
          setCall({
            provider: msg.provider,
            state: msg.state,
            callId: msg.callId ?? null,
            detail: msg.detail ?? null,
          });
          break;
        }

        case "practice_state": {
          setPracticeTurns(msg.turns);
          if (msg.started) {
            if (practiceStartedAtRef.current === null) {
              practiceStartedAtRef.current = Date.now();
            }
            setPracticeStarted(true);
          }
          // A started:false frame is the server saying it is waiting for the rep
          // to press start. It never cancels a call that is already running,
          // that is what practice_over is for.
          break;
        }

        default:
          break;
      }
    },
    [
      cancelTextFrame,
      openMicGate,
      pushTranscript,
      scheduleTextFlush,
      scheduleVadFlush,
      speakClientLine,
    ],
  );

  const handleConnState = useCallback((next: ConnState) => {
    setConnState(next);
    if (next === "connecting" || next === "reconnecting") {
      setStatus("connecting");
      return;
    }
    if (next === "closed") {
      setStatus((prev) => (prev === "error" ? "error" : "closed"));
    }
    // "open" deliberately does not flip the status. The call is only usable
    // once the server has answered with `ready`.
  }, []);

  const handleSocketError = useCallback((message: string) => {
    setError(message);
    // Transport errors arrive before the close, the give up message arrives
    // after it. Only the second case is fatal for the UI.
    setStatus((prev) => (prev === "closed" ? "error" : prev));
  }, []);

  const handlersRef = useRef({
    onMessage: handleServerMessage,
    onState: handleConnState,
    onError: handleSocketError,
  });

  // No dependency array on purpose: this must run after every render so the
  // socket always calls the newest closures.
  useEffect(() => {
    handlersRef.current = {
      onMessage: handleServerMessage,
      onState: handleConnState,
      onError: handleSocketError,
    };
  });

  /* ---------------------------------------------------------------- */
  /* the socket effect                                                 */
  /* ---------------------------------------------------------------- */

  useEffect(() => {
    const teardown = () => {
      // Any startCapture still awaiting a picker or a permission prompt is now
      // orphaned. The bump tells it so, and it stops itself when it resolves.
      teardownGenRef.current += 1;

      const socket = socketRef.current;
      socketRef.current = null;
      socketKeyRef.current = null;
      if (socket) socket.close();

      const handles = captureRef.current;
      if (handles.client) {
        try {
          handles.client.stop();
        } catch {
          // A handle that already tore itself down is fine.
        }
      }
      if (handles.rep) {
        try {
          handles.rep.stop();
        } catch {
          // Same.
        }
      }
      captureRef.current = { client: null, rep: null };
      capturesRef.current = { client: false, rep: false };

      cancelTextFrame();
      cancelVadFrame();
      committedRef.current = "";
      pendingRef.current = "";
      currentIdRef.current = null;
      pendingLevelsRef.current = { client: 0, rep: 0 };
      pendingSpeakingRef.current = { client: false, rep: false };

      // A page that has gone away must not keep talking. The counter bump makes
      // the cancelled line's own callback a no op, so it cannot try to send a
      // speech_state on the socket we just closed.
      speechSeqRef.current += 1;
      cancelSpeech();
      micGateRef.current = false;
      practiceStartedAtRef.current = null;

      setCaptures({ client: false, rep: false });
      setMuted({ client: false, rep: false });
      setLevels({ client: 0, rep: 0 });
      setSpeaking({ client: false, rep: false });
      setStreaming(false);
      setClientSpeaking(false);

      // A new session id is a new phone call as well. Carrying the old one over
      // would leave a LIVE chip in the bar for a call that belongs to a session
      // this hook is no longer connected to. The gate goes with it, so the next
      // session's status read is allowed to write once more.
      setCall(IDLE_CALL);
      callPushedRef.current = false;

      // A new session id is a new rehearsal. Without this, the "Practise again"
      // button would land on a page that still says the last call is over and
      // would open its score card again.
      setPracticeStarted(false);
      setPracticeOver(false);
      setPracticeReason(null);
      setPracticeTurns(0);
      setClientLine(null);
      setElapsedMs(0);
    };

    if (!sessionId) {
      // No session means there is nothing to connect to. This effect owns the
      // socket, so it also owns saying that the socket is not there.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setStatus("closed");
      setConnState("idle");
      return teardown;
    }

    // StrictMode belt and braces. The double mount runs effect, cleanup,
    // effect, so the guard is normally not hit, but if a future React ever
    // skips the cleanup we still refuse to open a second socket.
    if (socketRef.current && socketKeyRef.current === sessionId) {
      return teardown;
    }

    const socket = new TeleprompterSocket({
      url: teleprompterWsUrl(sessionId),
      onMessage: (m) => handlersRef.current.onMessage(m),
      onState: (s) => handlersRef.current.onState(s),
      onError: (e) => handlersRef.current.onError(e),
    });

    socketRef.current = socket;
    socketKeyRef.current = sessionId;
    setStatus("connecting");
    setError(null);
    setDroppedFrames(0);
    socket.connect();

    return teardown;
  }, [sessionId, cancelTextFrame, cancelVadFrame]);

  /* ---------------------------------------------------------------- */
  /* dropped frame counter                                             */
  /* ---------------------------------------------------------------- */

  useEffect(() => {
    const id = setInterval(() => {
      const socket = socketRef.current;
      const next = socket ? socket.droppedFrames : 0;
      setDroppedFrames((prev) => (prev === next ? prev : next));
    }, DROP_POLL_MS);
    return () => clearInterval(id);
  }, []);

  /* ---------------------------------------------------------------- */
  /* the practice clock                                                */
  /* ---------------------------------------------------------------- */

  /**
   * One interval for the whole page, and only while a practice call is running.
   * It is cleared when the call ends, because the effect stops depending on a
   * true condition, and on unmount, because that is the cleanup.
   */
  useEffect(() => {
    if (!practiceStarted || practiceOver) return;

    const id = setInterval(() => {
      const startedAt = practiceStartedAtRef.current;
      if (startedAt === null) return;
      setElapsedMs(Date.now() - startedAt);
    }, PRACTICE_TICK_MS);

    return () => clearInterval(id);
  }, [practiceStarted, practiceOver]);

  /* ---------------------------------------------------------------- */
  /* devices                                                           */
  /* ---------------------------------------------------------------- */

  const loadDevices = useCallback(async () => {
    try {
      const list = await listInputDevices();
      setDevices(list);
    } catch {
      setDevices([]);
    }
  }, []);

  /* The rep's reading style is a preference, not a fact about this call, so it
     comes back from storage on mount and is told to the server on ready. */
  useEffect(() => {
    const saved = loadPromptStyle();
    promptStyleRef.current = saved;
    if (saved !== "full") {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setPromptStyleValue(saved);
    }
  }, []);

  const refreshDevices = useCallback(() => {
    void loadDevices();
  }, [loadDevices]);

  useEffect(() => {
    // enumerateDevices is a browser API that does not exist during the
    // prerender, and the device list changes underneath us, so this is a
    // subscription to an external system rather than derived state.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadDevices();

    const media = typeof navigator !== "undefined" ? navigator.mediaDevices : undefined;
    if (!media || typeof media.addEventListener !== "function") return;

    const onChange = () => {
      void loadDevices();
    };
    media.addEventListener("devicechange", onChange);
    return () => {
      media.removeEventListener("devicechange", onChange);
    };
  }, [loadDevices]);

  /* ---------------------------------------------------------------- */
  /* capture control                                                   */
  /* ---------------------------------------------------------------- */

  const stopStream = useCallback(
    (kind: StreamKind) => {
      const handle = captureRef.current[kind];
      if (handle) {
        try {
          handle.stop();
        } catch {
          // Already stopped, nothing to do.
        }
      }
      captureRef.current[kind] = null;
      capturesRef.current = withFlag(capturesRef.current, kind, false);

      pendingLevelsRef.current[kind] = 0;
      pendingSpeakingRef.current[kind] = false;
      scheduleVadFlush();

      setCaptures((prev) => withFlag(prev, kind, false));
      setMuted((prev) => withFlag(prev, kind, false));

      socketRef.current?.send({ type: "control", action: "stop", stream: kind });
    },
    [scheduleVadFlush],
  );

  /**
   * Shared body of startClient and startRep.
   *
   * Only ever reached from a click handler, which is what keeps getUserMedia
   * and getDisplayMedia inside a user gesture.
   */
  const startStream = useCallback(
    async (kind: StreamKind, source: { useDisplayMedia?: boolean; deviceId?: string }) => {
      if (busyRef.current !== null) return;

      busyRef.current = kind;
      setBusy(kind);

      // Replacing a lane always kills the old capture first, so two streams can
      // never push frames down the same stream id.
      const existing = captureRef.current[kind];
      if (existing) {
        try {
          existing.stop();
        } catch {
          // Ignore, we are throwing it away anyway.
        }
        captureRef.current[kind] = null;
        // The server segmenter closes an utterance on byte count, not on wall
        // clock, so a lane that simply stops sending leaves half a sentence
        // open forever. Flush it, or the next stream's first frame gets glued
        // onto audio from a different source.
        socketRef.current?.send({ type: "control", action: "flush", stream: kind });
      }

      const streamId: 0 | 1 = kind === "client" ? 0 : 1;
      const generation = teardownGenRef.current;

      try {
        const handle = await startCapture({
          kind,
          deviceId: source.deviceId,
          useDisplayMedia: source.useDisplayMedia,
          onFrame: (pcm: ArrayBuffer) => {
            // The practice half duplex gate. It is only ever shut while the
            // browser is saying a client line out loud, which needs a
            // client_turn frame first, so on a live call this reads false for
            // the whole call and every frame goes out as before.
            if (micGateRef.current) return;
            socketRef.current?.sendAudio(streamId, pcm);
          },
          onLevel: (rms: number) => {
            // Local meter, so the wave still moves while the socket is down or
            // while the backend VAD has not spoken yet. A `vad` frame simply
            // overwrites this with the server reading.
            pendingLevelsRef.current[kind] = rms;
            scheduleVadFlush();
          },
          onEnded: () => {
            stopStream(kind);
            setError(
              kind === "client"
                ? "The shared sound stopped, most likely because sharing was ended in the Chrome bar. Start the client lane again when you are ready."
                : "The microphone stopped, probably because the device was unplugged. Start the mic again when you are ready.",
            );
          },
        });

        // The picker or the permission prompt can sit open for a long time. If
        // the hook was torn down while it was, this handle belongs to nothing
        // and nothing will ever stop it, so it stops itself right here. Without
        // this the shared tab keeps recording and its level callback keeps
        // requesting animation frames for the life of the page.
        if (teardownGenRef.current !== generation) {
          try {
            handle.stop();
          } catch {
            // Already gone, which is the outcome we wanted anyway.
          }
          return;
        }

        captureRef.current[kind] = handle;
        capturesRef.current = withFlag(capturesRef.current, kind, true);
        setCaptures((prev) => withFlag(prev, kind, true));
        setMuted((prev) => withFlag(prev, kind, handle.muted));
        setError(null);

        socketRef.current?.send({ type: "control", action: "start", stream: kind });

        // Labels only exist once a permission has been granted, so this second
        // enumeration is what turns "Audio input 2" into a real device name.
        void loadDevices();
      } catch (err) {
        captureRef.current[kind] = null;
        capturesRef.current = withFlag(capturesRef.current, kind, false);
        setCaptures((prev) => withFlag(prev, kind, false));
        setError(describeCaptureError(err, kind));
      } finally {
        busyRef.current = null;
        setBusy(null);
      }
    },
    [loadDevices, scheduleVadFlush, stopStream],
  );

  const startClient = useCallback(
    async (source: ClientSource) => {
      if (source.type === "display") {
        await startStream("client", { useDisplayMedia: true });
        return;
      }
      await startStream("client", { deviceId: source.deviceId });
    },
    [startStream],
  );

  const startRep = useCallback(
    async (deviceId?: string) => {
      await startStream("rep", { deviceId });
    },
    [startStream],
  );

  /**
   * Mute is not just a local gate.
   *
   * Muting stops the frames, and the server segmenter measures its end of
   * utterance in bytes received, so an open segment can never close while a
   * lane is silent. Thirty seconds later the first frame after the unmute would
   * be appended to the half sentence from before it, byte adjacent with no gap,
   * and whisper would transcribe a run on sentence nobody said. So muting sends
   * a flush (which also force emits a vad frame with speaking false, unsticking
   * the wave lane) and unmuting sends a start, which resets the noise floor to
   * whatever the room sounds like now.
   */
  const toggleMute = useCallback((kind: StreamKind) => {
    const handle = captureRef.current[kind];
    if (!handle) return;
    const next = !handle.muted;
    handle.setMuted(next);
    setMuted((prev) => withFlag(prev, kind, next));

    if (next) {
      pendingSpeakingRef.current[kind] = false;
      scheduleVadFlush();
    }
    socketRef.current?.send({
      type: "control",
      action: next ? "flush" : "start",
      stream: kind,
    });
  }, [scheduleVadFlush]);

  /* ---------------------------------------------------------------- */
  /* outbound messages                                                 */
  /* ---------------------------------------------------------------- */

  const quickAction = useCallback((key: string, note?: string) => {
    const socket = socketRef.current;
    if (!socket || socket.state !== "open") {
      setError("Not connected to the server yet, so that objection could not be sent.");
      return;
    }
    socket.send({ type: "quick_action", key, note });
    // Optimistic, so the pill and the teleprompter react on the same frame as
    // the click instead of one network round trip later.
    setStatus("thinking");
  }, []);

  const sendManual = useCallback((text: string, stream: StreamKind = "client") => {
    const trimmed = text.trim();
    if (trimmed.length === 0) return;

    const socket = socketRef.current;
    if (!socket || socket.state !== "open") {
      setError("Not connected to the server yet, so that line could not be sent.");
      return;
    }

    socket.send({ type: "manual_text", text: trimmed, stream });
    if (stream === "client") setStatus("thinking");
  }, []);

  const setSensitivity = useCallback((value: number) => {
    const safe = Number.isFinite(value)
      ? Math.min(MAX_SENSITIVITY, Math.max(MIN_SENSITIVITY, value))
      : 1;
    sensitivityRef.current = safe;
    setSensitivityValue(safe);
    socketRef.current?.send({ type: "config", sensitivity: safe });
  }, []);

  /**
   * Switch between a whole line to read and a few points to speak around.
   *
   * The server decides, not the browser: turning a finished sentence into points
   * on this side would mangle it. So this only records the choice, remembers it
   * for next time, and tells the server, which appends a different instruction
   * to the next suggestion. The line already on the glass is left alone.
   */
  const setPromptStyle = useCallback((next: PromptStyle) => {
    const safe: PromptStyle = next === "points" ? "points" : "full";
    promptStyleRef.current = safe;
    setPromptStyleValue(safe);
    savePromptStyle(safe);
    socketRef.current?.send({ type: "config", style: safe });
  }, []);

  const reset = useCallback(() => {
    cancelTextFrame();
    committedRef.current = "";
    pendingRef.current = "";
    currentIdRef.current = null;

    setSuggestion("");
    setStreaming(false);
    setTrigger(null);
    setSourceText(null);
    setTranscript([]);
    setLatency((prev) => ({ stt: null, firstToken: null, total: null, rtt: prev.rtt }));
    setError(null);
    setStatus((prev) => (prev === "closed" || prev === "error" ? prev : "ready"));

    socketRef.current?.send({ type: "control", action: "reset", stream: "all" });
  }, [cancelTextFrame]);

  /**
   * Take the call state from the page's own status read.
   *
   * This is the only door into `call` that is not the socket, and it is a narrow
   * one on purpose: it writes nothing once a `call_state` frame has landed, and
   * it touches no other piece of state, so a phone call being found in progress
   * cannot move the status pill or the glass any more than a pushed frame can.
   */
  const applyCallState = useCallback((snapshot: CallSnapshot) => {
    if (callPushedRef.current) return;
    setCall(snapshot);
  }, []);

  /* ---------------------------------------------------------------- */
  /* practice actions                                                  */
  /* ---------------------------------------------------------------- */

  /**
   * Start the rehearsal.
   *
   * This must be called from a click. Browsers only let a page talk after a real
   * user gesture, so the speech engine is woken up here, inside that gesture,
   * with a silent zero length line. Waking it later, when the client's first
   * line arrives over the socket, is too late and the line is dropped without
   * an error.
   */
  const startPractice = useCallback(async () => {
    // Priming comes first, before any early return, because this is the one
    // moment we are certain a real click is on the stack. A click that could not
    // reach the server still leaves the engine unlocked for the next try.
    primeSpeech();

    const socket = socketRef.current;
    if (!socket || socket.state !== "open") {
      setError("Not connected to the server yet, so the practice call could not start.");
      return;
    }

    // Turn the microphone on here, and only here. A practice call needs exactly
    // one input, the rep's own voice, and this click is the user gesture that
    // getUserMedia demands. Making the rep find the source picker first was the
    // whole reason a rep could talk into a dead microphone and never be heard.
    // A refused microphone is not fatal: they can still type their line into the
    // log, so the call starts either way and the bar says which it is.
    if (!capturesRef.current.rep) {
      try {
        await startStream("rep", {});
      } catch {
        // startStream already put a readable sentence into error state.
      }
    }

    practiceStartedAtRef.current = Date.now();
    setElapsedMs(0);
    setPracticeStarted(true);
    setPracticeOver(false);
    setPracticeReason(null);
    setPracticeTurns(0);
    setClientLine(null);
    setError(null);

    socket.send({ type: "practice_start" });
  }, [startStream]);

  /**
   * Stop the rehearsal and ask for a score.
   *
   * The server answers with practice_over, which is what actually ends the call.
   * When there is no socket left to answer, the call is ended here instead, so
   * the rep is never stuck on a screen that says the call is still running.
   */
  const endPractice = useCallback(() => {
    cancelSpeech();
    openMicGate();

    const socket = socketRef.current;
    if (socket && socket.state === "open") {
      socket.send({ type: "practice_end" });
      return;
    }

    setPracticeOver(true);
    setPracticeReason("rep_ended");
    const startedAt = practiceStartedAtRef.current;
    if (startedAt !== null) setElapsedMs(Date.now() - startedAt);
  }, [openMicGate]);

  /**
   * Talk over the client.
   *
   * The speech stops on the word it is on and the microphone comes straight
   * back. cancelSpeech fires the line's own end callback, which opens the gate,
   * and the second call here covers the browsers where a cancel never fires
   * anything at all.
   */
  const cutIn = useCallback(() => {
    cancelSpeech();
    openMicGate();
  }, [openMicGate]);

  const practice = useMemo<PracticeApi>(
    () => ({
      active: practiceEnabled,
      started: practiceStarted,
      over: practiceOver,
      reason: practiceReason,
      turns: practiceTurns,
      difficulty,
      clientSpeaking,
      clientLine,
      elapsedMs,
      startPractice,
      endPractice,
      cutIn,
    }),
    [
      practiceEnabled,
      practiceStarted,
      practiceOver,
      practiceReason,
      practiceTurns,
      difficulty,
      clientSpeaking,
      clientLine,
      elapsedMs,
      startPractice,
      endPractice,
      cutIn,
    ],
  );

  return useMemo<TeleprompterApi>(
    () => ({
      status,
      suggestion,
      streaming,
      trigger,
      sourceText,
      transcript,
      levels,
      speaking,
      latency,
      captures,
      muted,
      quickActions,
      devices,
      connState,
      droppedFrames,
      sensitivity,
      promptStyle,
      setPromptStyle,
      busy,
      error,
      call,
      practice,
      startClient,
      startRep,
      stopStream,
      toggleMute,
      quickAction,
      sendManual,
      setSensitivity,
      refreshDevices,
      reset,
      applyCallState,
      startPractice,
      endPractice,
      cutIn,
    }),
    [
      status,
      suggestion,
      streaming,
      trigger,
      sourceText,
      transcript,
      levels,
      speaking,
      latency,
      captures,
      muted,
      quickActions,
      devices,
      connState,
      droppedFrames,
      sensitivity,
      promptStyle,
      setPromptStyle,
      busy,
      error,
      call,
      practice,
      startClient,
      startRep,
      stopStream,
      toggleMute,
      quickAction,
      sendManual,
      setSensitivity,
      refreshDevices,
      reset,
      applyCallState,
      startPractice,
      endPractice,
      cutIn,
    ],
  );
}
