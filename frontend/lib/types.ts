/**
 * Shared type surface for the whole frontend.
 *
 * These types mirror the frozen wire protocol in CONTRACT.md section 2.2 and the
 * practice mode additions in CONTRACT_PRACTICE.md sections 3 and 4, exactly.
 * Nothing here may be renamed or widened without changing the backend too.
 *
 * The guards at the bottom are the trust boundary for anything that arrives off
 * the WebSocket or out of a proxy route. Everything crossing that boundary is
 * `unknown` until a guard says otherwise.
 */

/** Which capture lane a chunk of audio or a transcript belongs to. */
export type StreamKind = "client" | "rep";

/** Lifecycle of the teleprompter WebSocket. */
/**
 * How the copilot writes a suggestion.
 *
 * ``full`` is a whole line to read out loud, which is what a nervous rep wants.
 * ``points`` is two to four short triggers so an experienced rep speaks in their
 * own words instead of sounding like somebody reading a script.
 */
export type PromptStyle = "full" | "points";

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

/* ------------------------------------------------------------------ */
/* Practice mode                                                       */
/* ------------------------------------------------------------------ */

/** A real call, or a rehearsal against the synthetic client. */
export type CallMode = "live" | "practice";

/** How hard the synthetic client is to sell to. */
export type Difficulty = "warm" | "normal" | "brutal";

/** One difficulty card, from GET /api/practice/difficulties. */
export interface DifficultyInfo {
  key: Difficulty;
  label: string;
  /** One plain line the rep reads before choosing. */
  blurb: string;
}

/** How the synthetic client sounds right now. */
export type ClientMood = "cold" | "neutral" | "warm";

/** What the synthetic client is doing with this turn. */
export type ClientIntent = "question" | "objection" | "brushoff" | "agree" | "hangup";

/**
 * The push back the synthetic client just used, or "none".
 *
 * These are the frozen persona keys from CONTRACT_PRACTICE.md section 2. They
 * overlap the quick action keys by design but they are a separate, closed list,
 * so keep them separate from `QuickAction.key`, which the backend owns.
 */
export type PersonaObjection =
  | "too_expensive"
  | "not_interested"
  | "no_time"
  | "have_vendor"
  | "send_email"
  | "who_are_you"
  | "none";

/** Why the practice call stopped. */
export type PracticeOverReason = "hangup" | "rep_ended" | "goal_reached" | "turn_limit";

/** How the practice call finished, decided by the coach model. */
export type PracticeOutcome = "booked" | "soft_yes" | "no_answer" | "hung_up";

/** The one word the scorecard puts next to the number. */
export type DebriefGrade = "Good" | "Getting there" | "Needs work" | "Rough";

/**
 * The result of POST /api/practice/start.
 *
 * Everything a prepared session carries, plus the four fields the practice call
 * page needs: which mode this is, how hard the client is, what the client is
 * called, and the first line the browser has to speak out loud.
 */
export interface PracticeSession extends PreparedSession {
  mode: "practice";
  difficulty: Difficulty;
  /** The name the persona answers to, or null when none was found. */
  personaName: string | null;
  /** The line the client says before the rep says anything. */
  openingLine: string;
}

/** The plain numbers, all counted in Python, never guessed by a model. */
export interface DebriefMetrics {
  repWords: number;
  clientWords: number;
  /** Rep words over total words, as a whole number from 0 to 100. */
  talkingTimePct: number;
  questionsAsked: number;
  objectionsFaced: number;
  objectionsHandled: number;
  /** Mean gap between the client stopping and the rep starting, in milliseconds. */
  avgReplyMs: number;
  fillerWords: number;
  longestSentenceWords: number;
}

/** One moment of the call, quoted three ways. */
export interface DebriefMoment {
  clientSaid: string;
  copilotSaid: string;
  youSaid: string;
  why: string;
}

/** How much the teleprompter actually helped. */
export interface DebriefCopilot {
  suggestionsShown: number;
  suggestionsUsed: number;
  /** Used over shown, as a whole number from 0 to 100. */
  usedPct: number;
  /**
   * Both moments are nullable. A call that ended after one turn has nothing to
   * quote, and the contract says to return empty rather than invent filler.
   */
  bestMoment: DebriefMoment | null;
  missedMoment: DebriefMoment | null;
}

/** One row of the score rubric, so the rep can see where the points came from. */
export interface DebriefBreakdown {
  label: string;
  got: number;
  outOf: number;
  note: string;
}

/** One thing to say better next time. */
export interface DebriefFix {
  youSaid: string;
  problem: string;
  sayInstead: string;
}

/** The scorecard, from GET /api/practice/{sessionId}/debrief. */
export interface Debrief {
  sessionId: string;
  difficulty: Difficulty;
  durationMs: number;
  turns: number;
  outcome: PracticeOutcome;
  /** 0 to 100, summed from the seven breakdown rows. */
  score: number;
  grade: DebriefGrade;
  metrics: DebriefMetrics;
  copilot: DebriefCopilot;
  breakdown: DebriefBreakdown[];
  wins: string[];
  fixes: DebriefFix[];
  nextDrill: string;
}

/* ------------------------------------------------------------------ */
/* Calling                                                             */
/* ------------------------------------------------------------------ */

/**
 * The four ways a call can be placed.
 *
 * Frozen list, from CONTRACT_CALLING.md section 2.1. It is the same four words
 * as `CALL_PROVIDERS` in backend/app/models.py and `CallProvider` in
 * backend/app/services/session_store.py, so all three have to move together.
 */
export type CallProvider = "manual" | "whatsapp_link" | "whatsapp_cloud" | "twilio";

/**
 * Every state a phone call is allowed to be in.
 *
 * Carriers speak their own words for this. Twilio alone sends queued, initiated,
 * ringing, in-progress, completed, busy, no-answer, canceled and failed. The
 * backend maps all of them onto these six before anything reaches the browser,
 * so the UI only ever has six cases to draw.
 */
export type CallState = "idle" | "dialing" | "ringing" | "live" | "ended" | "failed";

/**
 * One row in the provider picker, from GET /api/call/providers.
 *
 * `cost` and `missing` are the honest part and are never hidden. A provider with
 * `ready: false` must be drawn disabled, and must still show what it costs and
 * which environment variables are missing, so the rep can see the option exists
 * and what it would take to turn it on. A rep must never find out that a call
 * costs money by being charged for one.
 */
export interface CallProviderInfo {
  key: CallProvider;
  /** Short name of the option, for example "Ring my phone". */
  label: string;
  /** One plain line saying what happens when this option is picked. */
  blurb: string;
  /** What it costs in plain words, for example "Free". Never empty. */
  cost: string;
  /** True only when this provider can place a call right now. */
  ready: boolean;
  /**
   * Names of the environment variables still needed, empty when ready. Shown to
   * the rep word for word, so they know exactly what to add to backend/.env.
   */
  missing: string[];
}

/**
 * The body of POST /api/call/start, on the 200 and on the 400 alike.
 *
 * The backend answers with this same shape either way, with `ok: false` and a
 * `message` that says what went wrong in plain words, so the UI shows `message`
 * without ever having to guess from a status code.
 */
export interface CallStartResult {
  ok: boolean;
  provider: CallProvider;
  /** The provider's own id for this call, for example a Twilio call sid. */
  callId: string | null;
  /**
   * A link the browser must open. WhatsApp link mode only, null for every other
   * provider.
   */
  openUrl: string | null;
  /** One plain line for the rep. Never empty. */
  message: string;
}

/* ------------------------------------------------------------------ */
/* Wire frames                                                         */
/* ------------------------------------------------------------------ */

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
  | { type: "error"; code: string; message: string }
  | {
      type: "client_turn";
      id: string;
      text: string;
      mood: ClientMood;
      intent: ClientIntent;
      objection: PersonaObjection;
      /** How long the persona model took, in milliseconds. */
      ms: number;
    }
  | { type: "practice_over"; reason: PracticeOverReason; turns: number; message: string }
  | { type: "practice_state"; turns: number; started: boolean }
  /**
   * Pushed whenever the phone call changes state, so the call page shows the
   * ringing and connected states without polling. The backend builds it from
   * `Session.call_snapshot()`, which always fills all four fields.
   */
  | {
      type: "call_state";
      provider: CallProvider;
      state: CallState;
      callId: string | null;
      detail: string | null;
    };

/** Client to server TEXT frames. Audio goes over BINARY frames instead. */
export type ClientMessage =
  | { type: "ping"; t: number }
  | { type: "control"; action: "start" | "stop" | "reset" | "flush"; stream: StreamKind | "all" }
  | { type: "quick_action"; key: string; note?: string }
  | { type: "manual_text"; text: string; stream: StreamKind }
  | { type: "config"; sensitivity?: number; autoSuggest?: boolean; style?: PromptStyle }
  | { type: "practice_start" }
  | { type: "practice_end" }
  /**
   * True while the browser is speaking the client line out loud. The rep's
   * frames must not be sent while this is true, and the server ignores any that
   * still arrive.
   */
  | { type: "speech_state"; speaking: boolean };

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

/**
 * A string, or an explicit null.
 *
 * The call frames carry two fields that are genuinely empty a lot of the time,
 * so null is a real value there and not a missing field. `undefined` is refused
 * on purpose: a field the server forgot is a bug, and it should show up as a
 * rejected frame rather than as the word "undefined" on the glass.
 */
function isStrOrNull(v: unknown): v is string | null {
  return v === null || typeof v === "string";
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

function isClientMood(v: unknown): v is ClientMood {
  return v === "cold" || v === "neutral" || v === "warm";
}

function isClientIntent(v: unknown): v is ClientIntent {
  return (
    v === "question" ||
    v === "objection" ||
    v === "brushoff" ||
    v === "agree" ||
    v === "hangup"
  );
}

function isPersonaObjection(v: unknown): v is PersonaObjection {
  return (
    v === "too_expensive" ||
    v === "not_interested" ||
    v === "no_time" ||
    v === "have_vendor" ||
    v === "send_email" ||
    v === "who_are_you" ||
    v === "none"
  );
}

function isPracticeOverReason(v: unknown): v is PracticeOverReason {
  return v === "hangup" || v === "rep_ended" || v === "goal_reached" || v === "turn_limit";
}

function isPracticeOutcome(v: unknown): v is PracticeOutcome {
  return v === "booked" || v === "soft_yes" || v === "no_answer" || v === "hung_up";
}

function isDebriefGrade(v: unknown): v is DebriefGrade {
  return v === "Good" || v === "Getting there" || v === "Needs work" || v === "Rough";
}

function isDebriefMetrics(v: unknown): v is DebriefMetrics {
  return (
    isRecord(v) &&
    isNum(v.repWords) &&
    isNum(v.clientWords) &&
    isNum(v.talkingTimePct) &&
    isNum(v.questionsAsked) &&
    isNum(v.objectionsFaced) &&
    isNum(v.objectionsHandled) &&
    isNum(v.avgReplyMs) &&
    isNum(v.fillerWords) &&
    isNum(v.longestSentenceWords)
  );
}

function isDebriefMoment(v: unknown): v is DebriefMoment {
  return (
    isRecord(v) &&
    isStr(v.clientSaid) &&
    isStr(v.copilotSaid) &&
    isStr(v.youSaid) &&
    isStr(v.why)
  );
}

/** A moment slot: the object, or null when the call had nothing to quote. */
function isMomentOrNull(v: unknown): v is DebriefMoment | null {
  return v === null || isDebriefMoment(v);
}

function isDebriefCopilot(v: unknown): v is DebriefCopilot {
  return (
    isRecord(v) &&
    isNum(v.suggestionsShown) &&
    isNum(v.suggestionsUsed) &&
    isNum(v.usedPct) &&
    isMomentOrNull(v.bestMoment) &&
    isMomentOrNull(v.missedMoment)
  );
}

function isDebriefBreakdown(v: unknown): v is DebriefBreakdown {
  return isRecord(v) && isStr(v.label) && isNum(v.got) && isNum(v.outOf) && isStr(v.note);
}

function isDebriefFix(v: unknown): v is DebriefFix {
  return isRecord(v) && isStr(v.youSaid) && isStr(v.problem) && isStr(v.sayInstead);
}

/**
 * True when `v` is one of the two capture lane names.
 * Anything else (including a stream id number) is rejected.
 */
export function isStreamKind(v: unknown): v is StreamKind {
  return v === "client" || v === "rep";
}

/** True when `v` is one of the three frozen difficulty keys. */
export function isDifficulty(v: unknown): v is Difficulty {
  return v === "warm" || v === "normal" || v === "brutal";
}

/**
 * True when `v` is one of the four frozen provider keys.
 *
 * Exported because the provider picker reads the same list back out of the
 * providers endpoint and out of localStorage, and a key we do not know must be
 * dropped rather than drawn as a button that cannot place a call.
 */
export function isCallProvider(v: unknown): v is CallProvider {
  return (
    v === "manual" || v === "whatsapp_link" || v === "whatsapp_cloud" || v === "twilio"
  );
}

/** True when `v` is one of the six frozen call states. */
export function isCallState(v: unknown): v is CallState {
  return (
    v === "idle" ||
    v === "dialing" ||
    v === "ringing" ||
    v === "live" ||
    v === "ended" ||
    v === "failed"
  );
}

/**
 * True when a stored session was prepared for practice.
 *
 * A live session has no `mode` field at all, so this is the one safe way to tell
 * the two apart after they come back out of localStorage.
 */
export function isPracticeSession(
  s: PreparedSession | PracticeSession | null,
): s is PracticeSession {
  if (s === null) return false;
  const candidate = s as Partial<PracticeSession>;
  return (
    candidate.mode === "practice" &&
    isDifficulty(candidate.difficulty) &&
    typeof candidate.openingLine === "string" &&
    (candidate.personaName === null || typeof candidate.personaName === "string")
  );
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
 *
 * The practice frames are checked just as tightly. `mood`, `intent` and
 * `objection` must already be one of the frozen persona values, so the server is
 * the one that has to clean up a sloppy model reply, not the browser.
 *
 * The `call_state` frame is checked the same way. `provider` and `state` must
 * already be one of the frozen words, because the carrier's own vocabulary is
 * mapped to ours on the server, and `callId` and `detail` must be a string or an
 * explicit null. All four fields are required, which is what
 * `Session.call_snapshot()` sends.
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
    case "client_turn":
      return (
        isStr(v.id) &&
        isStr(v.text) &&
        isClientMood(v.mood) &&
        isClientIntent(v.intent) &&
        isPersonaObjection(v.objection) &&
        isNum(v.ms)
      );
    case "practice_over":
      return isPracticeOverReason(v.reason) && isNum(v.turns) && isStr(v.message);
    case "practice_state":
      return isNum(v.turns) && isBool(v.started);
    case "call_state":
      return (
        isCallProvider(v.provider) &&
        isCallState(v.state) &&
        isStrOrNull(v.callId) &&
        isStrOrNull(v.detail)
      );
    default:
      return false;
  }
}

/**
 * True when `v` is a complete scorecard.
 *
 * Used by the debrief proxy route, which forwards whatever FastAPI returned. The
 * overlay reads every one of these fields without a fallback, so a half built
 * payload has to be caught here and shown as an error instead of rendering a
 * panel full of blanks. The lists may be empty, that is a real answer for a call
 * that ended after one turn, but they must be arrays of the right shape.
 */
export function isDebrief(v: unknown): v is Debrief {
  if (!isRecord(v)) return false;

  if (!isStr(v.sessionId)) return false;
  if (!isDifficulty(v.difficulty)) return false;
  if (!isNum(v.durationMs) || !isNum(v.turns) || !isNum(v.score)) return false;
  if (!isPracticeOutcome(v.outcome) || !isDebriefGrade(v.grade)) return false;
  if (!isStr(v.nextDrill)) return false;

  if (!isDebriefMetrics(v.metrics)) return false;
  if (!isDebriefCopilot(v.copilot)) return false;

  if (!Array.isArray(v.breakdown) || !v.breakdown.every(isDebriefBreakdown)) return false;
  if (!Array.isArray(v.wins) || !v.wins.every(isStr)) return false;
  if (!Array.isArray(v.fixes) || !v.fixes.every(isDebriefFix)) return false;

  return true;
}
