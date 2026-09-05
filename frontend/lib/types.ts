/**
 * Shared type surface for the whole frontend.
 *
 * These types mirror the frozen wire protocol in CONTRACT.md section 2.2 exactly.
 * Nothing here may be renamed or widened without changing the backend too.
 *
 * The two guards at the bottom are the trust boundary for anything that arrives
 * off the WebSocket. Everything crossing that boundary is `unknown` until a guard
 * says otherwise.
 */

/** Which capture lane a chunk of audio or a transcript belongs to. */
export type StreamKind = "client" | "rep";

/** Lifecycle of the teleprompter WebSocket. */
export type ConnState = "idle" | "connecting" | "open" | "closed" | "reconnecting";

/** What the call dashboard shows in the status pill. */
export type CallStatus =
  | "connecting"
  | "ready"
  | "listening"
  | "transcribing"
  | "thinking"
  | "error"
  | "closed";

/** What caused a suggestion to be generated. */
export type SuggestionTrigger = "speech" | "quick_action" | "manual";

/** One objection chip, defined by the backend and shipped in the `ready` message. */
export interface QuickAction {
  key: string;
  label: string;
  /** A lucide-react icon name. Resolve it through `iconFor` in lib/icons.ts. */
  icon: string;
  hint: string;
}

/** One row in the transcript rail. */
export interface TranscriptItem {
  id: string;
  role: "client" | "rep" | "copilot";
  text: string;
  /** Epoch milliseconds, client side clock. */
  ts: number;
  /** STT latency in milliseconds, present on finalized transcript rows. */
  ms?: number;
}

/** The result of POST /api/prepare-context, persisted to localStorage. */
export interface PreparedSession {
  sessionId: string;
  systemPrompt: string;
  clientUrl: string | null;
  clientTitle: string | null;
  clientExcerpt: string | null;
  scrapeChars: number;
  scrapeOk: boolean;
  scrapeError: string | null;
  createdAt: number;
  language: string;
  /**
   * True when the rep described the client by hand instead of, or as well as,
   * giving a website. Optional because a session saved by an older build will
   * not carry it.
   */
  hasNotes?: boolean;
}

/** Server to client TEXT frames. */
export type ServerMessage =
  | {
      type: "ready";
      sessionId: string;
      sampleRate: number;
      frameMs: number;
      model: { stt: string; llm: string };
      quickActions: QuickAction[];
    }
  | { type: "pong"; t: number }
  | { type: "vad"; stream: StreamKind; speaking: boolean; rms: number }
  | { type: "partial_transcript"; stream: StreamKind; text: string; id: string }
  | { type: "transcript"; stream: StreamKind; text: string; id: string; ms: number; final: true }
  | { type: "suggestion_start"; id: string; trigger: SuggestionTrigger; sourceText: string; sttMs: number }
  | { type: "suggestion_delta"; id: string; delta: string }
  | { type: "suggestion_done"; id: string; text: string; firstTokenMs: number; totalMs: number; sttMs: number }
  | { type: "status"; state: "idle" | "listening" | "transcribing" | "thinking"; detail?: string }
  | { type: "error"; code: string; message: string };

/** Client to server TEXT frames. Audio goes over BINARY frames instead. */
export type ClientMessage =
  | { type: "ping"; t: number }
  | { type: "control"; action: "start" | "stop" | "reset" | "flush"; stream: StreamKind | "all" }
  | { type: "quick_action"; key: string; note?: string }
  | { type: "manual_text"; text: string; stream: StreamKind }
  | { type: "config"; sensitivity?: number; autoSuggest?: boolean };

/* ------------------------------------------------------------------ */
/* Guards                                                              */
/* ------------------------------------------------------------------ */

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isStr(v: unknown): v is string {
  return typeof v === "string";
}

function isNum(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

function isBool(v: unknown): v is boolean {
  return typeof v === "boolean";
}

function isQuickAction(v: unknown): v is QuickAction {
  return (
    isRecord(v) && isStr(v.key) && isStr(v.label) && isStr(v.icon) && isStr(v.hint)
  );
}

function isSuggestionTrigger(v: unknown): v is SuggestionTrigger {
  return v === "speech" || v === "quick_action" || v === "manual";
}

function isStatusState(v: unknown): v is "idle" | "listening" | "transcribing" | "thinking" {
  return v === "idle" || v === "listening" || v === "transcribing" || v === "thinking";
}

/**
 * True when `v` is one of the two capture lane names.
 * Anything else (including a stream id number) is rejected.
 */
export function isStreamKind(v: unknown): v is StreamKind {
  return v === "client" || v === "rep";
}

/**
 * True when `v` is a well formed server frame.
 *
 * This checks the discriminant AND the required payload fields for that
 * discriminant, because the return type promises both. A frame that names a
 * known type but is missing a field the UI reads is rejected, so a malformed
 * server or a hostile socket cannot smuggle `undefined` into the render path.
 * Unknown extra fields are allowed, so the backend can add fields without
 * breaking an older client.
 */
export function isServerMessage(v: unknown): v is ServerMessage {
  if (!isRecord(v) || !isStr(v.type)) return false;

  switch (v.type) {
    case "ready":
      return (
        isStr(v.sessionId) &&
        isNum(v.sampleRate) &&
        isNum(v.frameMs) &&
        isRecord(v.model) &&
        isStr(v.model.stt) &&
        isStr(v.model.llm) &&
        Array.isArray(v.quickActions) &&
        v.quickActions.every(isQuickAction)
      );
    case "pong":
      return isNum(v.t);
    case "vad":
      return isStreamKind(v.stream) && isBool(v.speaking) && isNum(v.rms);
    case "partial_transcript":
      return isStreamKind(v.stream) && isStr(v.text) && isStr(v.id);
    case "transcript":
      return (
        isStreamKind(v.stream) &&
        isStr(v.text) &&
        isStr(v.id) &&
        isNum(v.ms) &&
        v.final === true
      );
    case "suggestion_start":
      return (
        isStr(v.id) && isSuggestionTrigger(v.trigger) && isStr(v.sourceText) && isNum(v.sttMs)
      );
    case "suggestion_delta":
      return isStr(v.id) && isStr(v.delta);
    case "suggestion_done":
      return (
        isStr(v.id) &&
        isStr(v.text) &&
        isNum(v.firstTokenMs) &&
        isNum(v.totalMs) &&
        isNum(v.sttMs)
      );
    case "status":
      return isStatusState(v.state) && (v.detail === undefined || isStr(v.detail));
    case "error":
      return isStr(v.code) && isStr(v.message);
    default:
      return false;
  }
}
