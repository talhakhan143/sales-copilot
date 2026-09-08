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
/* Leads                                                               */
/* ------------------------------------------------------------------ */

/**
 * Where a lead is in the rep's day.
 *
 * The seven frozen words from CONTRACT_LEADS.md section 3.1. They are written
 * straight into `leadengine/data/pipeline.json`, which the rep's own lead engine
 * dashboard reads too, so these strings are shared with a second app and must
 * never be renamed on this side alone. Show them through `STATUS_LABELS` in
 * lib/leads.ts, never raw: "not_interested" is a database word, not a word a
 * person reads.
 */
export type LeadStatus =
  | "new"
  | "interested"
  | "callback"
  | "not_interested"
  | "no_answer"
  | "won"
  | "lost";

/**
 * What the rep would sell this business.
 *
 * The lead engine writes the word in capitals, "SEO" or "WEBSITE", and that is
 * what the 91 real leads on disk carry today. `OTHER` is not a track the engine
 * produces, it is the safe landing spot for a word this build does not know, so
 * one strange row cannot make a lead vanish from a calling list. Map it through
 * `TRACK_LABELS` in lib/leads.ts before it reaches the screen.
 */
export type LeadTrack = "SEO" | "WEBSITE" | "OTHER";

/**
 * How bad one audit finding is.
 *
 * Four words, not two. The contract only sends high and medium into the call
 * context, but the audit files on disk also hold `critical` and `low`, and the
 * lead drawer shows the whole list, so all four have to be nameable here.
 */
export type PainSeverity = "critical" | "high" | "medium" | "low";

/**
 * One finished search, from GET /api/leads/searches.
 *
 * `value` is the money still on the table: the deal value of every lead in this
 * search that has not been called yet. It drops as the rep works the list, which
 * is the point.
 */
export interface SearchSummary {
  /** The slug the files are named after, for example "barber-hoboken". */
  id: string;
  niche: string;
  location: string;
  /** How many leads the search found. */
  leads: number;
  /** How many of them have been called. */
  called: number;
  /** Sum of the deal value over the leads not called yet. */
  value: number;
  /** The symbol to put in front of `value`, for example "$". */
  currency: string;
  /** Epoch seconds, as a float. Newest search first in the list. */
  scrapedAt: number;
}

/**
 * One row in the calling list, from GET /api/leads/{search_id}.
 *
 * `phone` and `website` are a string, an empty string, or null, because a lead
 * with no number and a lead with no site are both normal and both meaningful. An
 * empty phone means the row cannot be called, so use `canCall` in lib/leads.ts
 * rather than testing the field by hand in three components.
 *
 * `rating` and `reviews` are 0 for a business Google has no stars for yet. That
 * is 2 of the 91 leads on disk, so it is a real case, not a theory. The scraper
 * stores null for those, and lib/leads.ts folds a null to 0 before the guard
 * runs, so a business with no stars is never dropped from a calling list and no
 * component has to hold a null. Test `rating > 0`, never `rating != null`.
 */
export interface LeadRow {
  /** The join key, the same value the lead engine calls `feature_id`. */
  key: string;
  name: string;
  category: string;
  city: string;
  phone: string | null;
  website: string | null;
  /** 0 to 5. Exactly 0 means Google has no stars for this business yet. */
  rating: number;
  /** 0 or more. 0 means nobody has left a review yet. */
  reviews: number;
  track: LeadTrack;
  /** 0 to 100. How good this lead is overall. */
  leadScore: number;
  /** 1 to 5. How much of a hurry they are in. */
  urgency: number;
  /** One plain line saying why this lead is worth a call. */
  why: string;
  /** What the work is worth, in the currency below. */
  dealValue: number;
  /** The symbol to put in front of `dealValue`, for example "$". */
  currency: string;
  /** How many things the audit found wrong. */
  painCount: number;
  status: LeadStatus;
  /** ISO timestamp of the first call, or null when never called. */
  calledAt: string | null;
  /**
   * When to get to them: "now", "week" or "later".
   *
   * This is the decision the scoring step made, and `urgency` is the raw number
   * behind it. The two can disagree, and where they do this one wins, because
   * it is the one the engine's own dashboard sorted the day by.
   */
  priority: LeadPriority;
  /** The whole deal over its life, one off fee plus the retainer. */
  contractValue: number;
  /** `contractValue` multiplied by the odds of closing it. */
  expectedValue: number;
  /** How many ready to send drafts the copywriter wrote. 0 when it never ran. */
  messageCount: number;
}

/** When to get to a lead. The three lanes the work queue is built out of. */
export type LeadPriority = "now" | "week" | "later";

/** True when `v` is one of the three priorities. */
export function isLeadPriority(v: unknown): v is LeadPriority {
  return v === "now" || v === "week" || v === "later";
}

/** The words and the order the queue and the analytics screen both use. */
export const PRIORITY_ORDER: LeadPriority[] = ["now", "week", "later"];

export const PRIORITY_LABELS: Record<LeadPriority, string> = {
  now: "Call today",
  week: "This week",
  later: "Later",
};

/** One line saying what each lane means, for the empty state and the tooltip. */
export const PRIORITY_BLURBS: Record<LeadPriority, string> = {
  now: "Something is badly broken and they are losing customers over it.",
  week: "Worth a call, but nothing is on fire.",
  later: "Their site is fine. Only worth a call when the list above is done.",
};

/**
 * One thing the audit found wrong, with the evidence.
 *
 * `proof` is the number the rep says out loud, for example "6.6s". It is often
 * an empty string, and an empty one must be left out of the screen rather than
 * drawn as an empty bracket.
 */
export interface PainPoint {
  title: string;
  detail: string;
  proof: string;
  severity: PainSeverity;
}

/** One line of opening hours, exactly as Google Maps showed it. */
export interface OpeningHours {
  /** For example "Wednesday". */
  day: string;
  /** For example "9 AM to 8:15 PM", or "Closed". */
  hours: string;
}

/**
 * What this lead is worth, from the lead engine's own scoring.
 *
 * All seven fields are always sent, and every one of the 91 scored leads on disk
 * has all of them. So a half filled block means the backend is broken, not that
 * the business is unusual, and the guard below rejects the lead instead of
 * drawing a blank price.
 *
 * A search that has been scraped but not scored yet has no prices worked out. It
 * still gets a whole block, with a zero in every number and an empty string in
 * every word, because that is a shape the screen can read, and the drawer
 * already leaves out any money line whose number is zero. So the rep sees no
 * price at all, which is the honest answer, instead of a price nobody worked
 * out. lib/leads.ts fills the same block in if the block is ever missing, so
 * this shape holds even against an older server.
 */
export interface LeadMoney {
  /** For example "Mid ticket". */
  tierLabel: string;
  /** The one off part of the deal. */
  dealValueUsd: number;
  /** The monthly part. */
  retainerMonthlyUsd: number;
  /** One off plus the whole retainer run. */
  contractValueUsd: number;
  /** For example "$". */
  currencySymbol: string;
  /** 0 to 1. How likely this one is to close. */
  closeProbability: number;
  /** Contract value times the close chance. */
  expectedValueUsd: number;
}

/**
 * The whole lead, from GET /api/leads/{search_id}/{key}.
 *
 * Everything the row has, plus the parts the drawer needs. It extends `LeadRow`
 * on purpose: the drawer opens over a row that is already on screen, so the two
 * shapes must never disagree about a name or a price.
 */
export interface LeadDetail extends LeadRow {
  address: string;
  hours: OpeningHours[];
  /** The Google Maps listing, or null when the scrape did not get one. */
  gmbUrl: string | null;
  /** Highest severity first. May be empty when the audit has not run. */
  painPoints: PainPoint[];
  /** Short lines for the rep to skim before the call, never read out loud. */
  talkingPoints: string[];
  /**
   * The deal maths. Never null, and never missing. A search that has not been
   * scored yet gets a block of zeros instead, which the drawer draws as no
   * price at all, so the drawer holds one shape and needs no null check.
   */
  money: LeadMoney;
  /** Whatever the rep typed after the last call. Often an empty string. */
  notes: string;
  /**
   * The ready to send drafts the copywriter wrote for this business.
   *
   * The rep copies one and pastes it into whatever app that channel lives in.
   * Nothing here is ever sent by this product. A cold email sent by a server is
   * a different legal question from one a person sends, and the value of these
   * is that a human reads them before anyone else does.
   */
  messages: LeadMessage[];
  /**
   * Which pictures of their site exist on this machine.
   *
   * Booleans, not links. The link is the same shape every time and the only
   * thing the drawer cannot work out for itself is whether the file is really
   * there. Asking the browser for one that is not gives the rep a broken frame
   * instead of an honest "no picture".
   */
  screenshots: Partial<Record<LeadShotView, boolean>>;
  /** Why the engine put them in this lane, in one line. */
  urgencyReason: string;
}

/** The two pictures the audit takes of a prospect's website. */
export type LeadShotView = "desktop" | "mobile";

/** The three places the copywriter writes for. */
export type LeadMessageChannel = "email" | "instagram" | "sms";

export const MESSAGE_CHANNEL_LABELS: Record<LeadMessageChannel, string> = {
  email: "Email",
  instagram: "Instagram DM",
  sms: "Text message",
};

/** One ready to send message. */
export interface LeadMessage {
  channel: LeadMessageChannel;
  /** The email subject. An empty string for the two channels that have none. */
  subject: string;
  body: string;
}

/** The rep's own details, which go into every message the copywriter writes. */
export interface LeadProfile {
  [key: string]: unknown;
  your_name?: string;
  company?: string;
  email?: string;
  phone?: string;
  whatsapp?: string;
  city?: string;
  portfolio_url?: string;
  calendar_url?: string;
  signature_note?: string;
}

/**
 * Every number the deal maths runs on.
 *
 * Deliberately loose. This is the engine's own pricing file, the settings screen
 * edits the handful of fields a rep actually changes, and everything else is
 * carried through untouched so saving one field cannot drop the rest of a file
 * this product did not write.
 */
export interface LeadPricing {
  [key: string]: unknown;
  currency_symbol?: string;
  ltv_months?: number;
  tiers?: Record<string, LeadPricingTier>;
}

/** One price band, matched to a business by the keywords in its category. */
export interface LeadPricingTier {
  [key: string]: unknown;
  label?: string;
  /** Low and high one off fee. */
  website_price?: number[];
  /** Low and high monthly retainer. */
  retainer_monthly?: number[];
  keywords?: string[];
}

/** Both settings files, from GET /api/leads/config. */
export interface LeadConfig {
  profile: LeadProfile;
  pricing: LeadPricing;
}

/** How a background scrape is going. */
export type ScrapeState = "running" | "finished" | "failed";

/**
 * One background scrape, from GET /api/leads/jobs/{job_id}.
 *
 * `line` is the last line the job printed. It is shown as it is, because a
 * Chromium window scrolling Google Maps for four minutes behind a spinner looks
 * broken, and the real line ("scraped 18 of 60") looks like work.
 */
export interface ScrapeJob {
  state: ScrapeState;
  /** Which part of the pipeline is running, for example "audit". */
  step: string;
  /** The last line the job printed. May be an empty string at the very start. */
  line: string;
  /** The slug the finished search will be saved under. */
  searchId: string;
  /** True once the job stopped, whether it worked or failed. */
  done: boolean;
}

/** The answer to POST /api/leads/search, which starts a scrape. */
export interface ScrapeStarted {
  ok: boolean;
  searchId: string;
  jobId: string;
}

/**
 * A call context built from a lead, from POST /api/leads/{search_id}/{key}/call.
 *
 * A prepared session and nothing more, plus the two fields that say which lead
 * it came from. It is an ordinary session: the teleprompter, the objection
 * buttons and the calling providers all treat it exactly like one the rep built
 * by hand. The two extra fields exist so the page can write the outcome back to
 * the right lead when the call ends.
 */
export interface LeadCallSession extends PreparedSession {
  leadKey: string;
  leadName: string;
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
  /** Write the same moment again. The reading style switch uses this so the
      line already on the glass changes, not just the next one. */
  | { type: "redo" }
  /** The client did not follow the line. Same point, different words. */
  | { type: "rephrase" }
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

/* ------------------------------------------------------------------ */
/* Lead guards                                                         */
/* ------------------------------------------------------------------ */

/** True when `v` is one of the seven frozen pipeline words. */
export function isLeadStatus(v: unknown): v is LeadStatus {
  return (
    v === "new" ||
    v === "interested" ||
    v === "callback" ||
    v === "not_interested" ||
    v === "no_answer" ||
    v === "won" ||
    v === "lost"
  );
}

/** True when `v` is one of the three track words this build knows. */
export function isLeadTrack(v: unknown): v is LeadTrack {
  return v === "SEO" || v === "WEBSITE" || v === "OTHER";
}

/**
 * Read the track word off the wire, in whatever case it arrives in.
 *
 * The lead engine writes "SEO" and "WEBSITE", the contract prose writes
 * "Website", and both mean the same thing, so the word is upper cased before it
 * is matched. Anything else becomes "OTHER". This never drops the lead: a
 * business worth 850 dollars must not disappear from a calling list over one
 * unexpected word, and the drawer can still show every pain point without
 * knowing which of the two things is being sold.
 */
export function asLeadTrack(v: unknown): LeadTrack {
  if (typeof v !== "string") return "OTHER";
  const word = v.trim().toUpperCase();
  if (word === "SEO") return "SEO";
  if (word === "WEBSITE" || word === "SITE" || word === "WEB") return "WEBSITE";
  return "OTHER";
}

function isPainSeverity(v: unknown): v is PainSeverity {
  return v === "critical" || v === "high" || v === "medium" || v === "low";
}

/** One audit finding. `proof` is often empty, which is allowed. */
export function isPainPoint(v: unknown): v is PainPoint {
  return (
    isRecord(v) &&
    isStr(v.title) &&
    isStr(v.detail) &&
    isStr(v.proof) &&
    isPainSeverity(v.severity)
  );
}

function isOpeningHours(v: unknown): v is OpeningHours {
  return isRecord(v) && isStr(v.day) && isStr(v.hours);
}

/**
 * The money block, checked field by field.
 *
 * Nothing here is optional. A price is the one number the rep says out loud that
 * cannot be softened later, so a half built money block is refused rather than
 * shown with a number that was never worked out.
 *
 * A block of zeros is not a half built one. That is what an unscored search
 * gets, on both sides of the wire, and it passes here on purpose: it is a whole
 * block, it is simply worth nothing yet, and the drawer skips every money line
 * whose number is zero.
 */
export function isLeadMoney(v: unknown): v is LeadMoney {
  return (
    isRecord(v) &&
    isStr(v.tierLabel) &&
    isNum(v.dealValueUsd) &&
    isNum(v.retainerMonthlyUsd) &&
    isNum(v.contractValueUsd) &&
    isStr(v.currencySymbol) &&
    isNum(v.closeProbability) &&
    isNum(v.expectedValueUsd)
  );
}

/**
 * True when `v` is a complete calling list row.
 *
 * `track` is checked strictly and so are `rating` and `reviews`, so run the raw
 * record through the normaliser in lib/leads.ts first. It folds the track word
 * to one of three, and a missing star count to 0, before this ever sees it.
 */
export function isLeadRow(v: unknown): v is LeadRow {
  return (
    isRecord(v) &&
    isStr(v.key) &&
    v.key.length > 0 &&
    isStr(v.name) &&
    isStr(v.category) &&
    isStr(v.city) &&
    isStrOrNull(v.phone) &&
    isStrOrNull(v.website) &&
    isNum(v.rating) &&
    isNum(v.reviews) &&
    isLeadTrack(v.track) &&
    isNum(v.leadScore) &&
    isNum(v.urgency) &&
    isStr(v.why) &&
    isNum(v.dealValue) &&
    isStr(v.currency) &&
    isNum(v.painCount) &&
    isLeadStatus(v.status) &&
    isStrOrNull(v.calledAt)
  );
}

/**
 * True when `v` is a complete lead.
 *
 * Everything the row needs, plus the parts the drawer reads without a fallback.
 * The two lists may be empty, which is the honest answer for a search whose
 * audit has not run yet, but they must be arrays of the right shape, because a
 * drawer that renders "undefined" next to a phone number the rep is about to
 * dial is worse than a drawer that says the lead could not be read.
 *
 * The same normalising rule as `isLeadRow` applies here, for the same reason,
 * and it covers one more field. `money` is checked in full, so a lead that
 * arrived with no money block at all would be refused and the drawer would never
 * open on it. The normaliser in lib/leads.ts puts a block of zeros there first,
 * which is what the server sends for an unscored search anyway, so a search
 * whose scoring has not run still opens.
 */
export function isLeadDetail(v: unknown): v is LeadDetail {
  if (!isLeadRow(v)) return false;
  const d = v as unknown as Record<string, unknown>;

  if (!isStr(d.address)) return false;
  if (!isStrOrNull(d.gmbUrl)) return false;
  if (!isStr(d.notes)) return false;
  if (!isLeadMoney(d.money)) return false;

  if (!Array.isArray(d.hours) || !d.hours.every(isOpeningHours)) return false;
  if (!Array.isArray(d.painPoints) || !d.painPoints.every(isPainPoint)) return false;
  if (!Array.isArray(d.talkingPoints) || !d.talkingPoints.every(isStr)) return false;

  return true;
}

/** True when `v` is a complete search summary row. */
export function isSearchSummary(v: unknown): v is SearchSummary {
  return (
    isRecord(v) &&
    isStr(v.id) &&
    v.id.length > 0 &&
    isStr(v.niche) &&
    isStr(v.location) &&
    isNum(v.leads) &&
    isNum(v.called) &&
    isNum(v.value) &&
    isStr(v.currency) &&
    isNum(v.scrapedAt)
  );
}

function isScrapeState(v: unknown): v is ScrapeState {
  return v === "running" || v === "finished" || v === "failed";
}

/** True when `v` is a complete job report. */
export function isScrapeJob(v: unknown): v is ScrapeJob {
  return (
    isRecord(v) &&
    isScrapeState(v.state) &&
    isStr(v.step) &&
    isStr(v.line) &&
    isStr(v.searchId) &&
    isBool(v.done)
  );
}

/** True when `v` says a scrape started, with both ids the UI has to keep. */
export function isScrapeStarted(v: unknown): v is ScrapeStarted {
  return (
    isRecord(v) &&
    isBool(v.ok) &&
    isStr(v.searchId) &&
    v.searchId.length > 0 &&
    isStr(v.jobId) &&
    v.jobId.length > 0
  );
}
