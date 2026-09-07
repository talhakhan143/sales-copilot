"use client";

/**
 * The live call dashboard.
 *
 * One seam grid, four fixed rows, and exactly one scrolling element (the
 * transcript rail body). The page itself never scrolls at any width, because the
 * suggestion must always be on screen. That is the entire product.
 *
 * The default export wraps the real screen in a Suspense boundary. CallScreen
 * calls useSearchParams(), and Next refuses to build a page that does that
 * without a boundary above it.
 *
 * PRACTICE MODE (CONTRACT_PRACTICE.md section 6.2)
 *
 * The same screen runs the rehearsal. `?mode=practice` in the address bar, or
 * the practice flag on the saved session, turns on four extra pieces: the
 * practice strip above the objection bar, a state word over the teleprompter,
 * a synthetic client lane in the wave, and the score card overlay at the end.
 * Every one of them is behind a guard, so a real call renders exactly the DOM it
 * rendered before practice mode existed.
 *
 * CALLING (CONTRACT_CALLING.md section 5)
 *
 * The top bar also places the call. A CALL popover sits next to SOURCES, with a
 * state chip beside it that appears only once the call is doing something. This
 * screen owns the provider list, the two phone numbers, the busy flag and the
 * last message, and the launcher only draws them. The state itself is not owned
 * here: it arrives on the socket as a call_state frame and comes out of the
 * hook, so the chip and the popover can never disagree with the server.
 *
 * That frame is pushed only when the call MOVES, so this screen also reads
 * GET /api/call/{id}/status once when it opens and hands the answer to the hook,
 * into the very same place. That one read is what brings the chip and the
 * launcher back for a rep who reloaded the page in the middle of a live call,
 * and it is what stops them being offered a second call they would pay for.
 *
 * In practice mode none of it is mounted. There is nobody on the other end of a
 * rehearsal, so a Call button there would be a button that cannot call.
 *
 * LEADS (CONTRACT_LEADS.md section 6)
 *
 * A call can also come from the calling list. When it does, the address bar
 * carries the lead key and the search it belongs to, and this screen adds two
 * things: a thin strip over the glass holding the business name, and the
 * outcome row that asks what happened once the call is over. The outcome row
 * takes the objection bar's own row in the grid, because the eight chips answer
 * a client who is still on the line and there is nobody on the line any more.
 *
 * Picking an answer writes it straight into the lead engine's own pipeline
 * file, and Next lead builds the context for the next uncalled lead in the same
 * search and opens it, so a rep doing forty calls a day never walks back to the
 * list between two of them.
 *
 * All of it sits behind one flag. A call the rep set up by hand has no lead key,
 * so it renders exactly the screen it rendered before any of this existed.
 */

import { Suspense, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ArrowLeft, Check, Copy, ExternalLink, Plus, ScrollText, Send, X } from "lucide-react";

import { AudioSourcePicker } from "@/components/AudioSourcePicker";
import { CallLauncher, CallStateChip } from "@/components/CallLauncher";
import { CallOutcome } from "@/components/CallOutcome";
import { Debrief } from "@/components/Debrief";
import { LatencyMeter } from "@/components/LatencyMeter";
import { ObjectionBar } from "@/components/ObjectionBar";
import { PracticeBar } from "@/components/PracticeBar";
import { StatusPill } from "@/components/StatusPill";
import { Teleprompter } from "@/components/Teleprompter";
import { TranscriptRail } from "@/components/TranscriptRail";
import { WaveVisualizer } from "@/components/WaveVisualizer";
import { fetchProviders, hangUp, startCall } from "@/lib/calling";
import { useTeleprompter } from "@/lib/hooks/useTeleprompter";
import type { CallSnapshot, StreamFlags } from "@/lib/hooks/useTeleprompter";
import { fetchLeads, setLeadStatus, startLeadCall } from "@/lib/leads";
import { loadProfile } from "@/lib/profile";
import { SESSION_STORAGE_KEY, loadSession, saveSession } from "@/lib/session";
import { isCallProvider, isCallState, isDebrief, isDifficulty } from "@/lib/types";
import type {
  CallProvider,
  CallProviderInfo,
  Debrief as DebriefData,
  Difficulty,
  LeadRow,
  LeadStatus,
  PreparedSession,
  QuickAction,
  StreamKind,
} from "@/lib/types";

/**
 * The matrix the bar falls back to before the `ready` frame lands, or when the
 * socket never opens. Same eight keys, same frozen order, so the chip under the
 * rep's finger is in the same place whether or not the server has answered yet.
 */
const FALLBACK_ACTIONS: QuickAction[] = [
  { key: "too_expensive", label: "Too Expensive", icon: "BadgeDollarSign", hint: "Price objection" },
  { key: "not_interested", label: "Not Interested", icon: "Ban", hint: "Brush off" },
  { key: "send_email", label: "Send Me An Email", icon: "Mail", hint: "Deflection" },
  { key: "have_vendor", label: "Already Have Vendor", icon: "Building2", hint: "Incumbent" },
  { key: "no_time", label: "No Time Right Now", icon: "Clock", hint: "Time objection" },
  { key: "who_are_you", label: "Who Are You?", icon: "HelpCircle", hint: "Cold open" },
  { key: "send_proposal", label: "Send A Proposal", icon: "FileText", hint: "Positive signal" },
  { key: "book_meeting", label: "Book The Meeting", icon: "CalendarCheck", hint: "Close" },
];

/** How long the fired chip keeps its cooldown receipt. */
const ACTIVE_MS = 2000;
const COPIED_MS = 1200;

/** The level used when the address bar says practice but nothing says how hard. */
const DEFAULT_DIFFICULTY: Difficulty = "normal";

/**
 * How often the made up client level is redrawn while the client talks.
 * 80 ms is the same rate the server sends real VAD frames at, so the two lanes
 * move at the same speed and neither one looks faster than the other.
 */
const SYNTHETIC_LEVEL_MS = 80;

/**
 * The client lane height under prefers-reduced-motion, held steady.
 *
 * The wave itself keeps scrolling under reduced motion because it is a
 * measurement (design spec 2.5), but this one number is not measured, it is
 * decoration, so the wobble is dropped and one flat value is fed instead. The
 * lane still stands up while the client talks, which is the only thing it has
 * to say, and it stops flickering at the moment the rep is meant to be
 * listening rather than watching.
 */
const STEADY_CLIENT_LEVEL = 0.5;

/**
 * Both lanes plugged in, for a call the server is carrying by itself.
 *
 * The wave dims a lane and throws its history away when nothing is plugged into
 * it. On a phone call the browser has plugged nothing in and never will, yet the
 * bars are moving, because the server is pushing the phone's own levels down the
 * same socket. Without this the instrument would draw two dead lanes over live
 * measured audio, which is the one lie it is built never to tell.
 */
const PHONE_LANES: StreamFlags = { client: true, rep: true };

/**
 * The keys the call screen answers from anywhere on the page: the eight
 * objection chips, c to copy the line, r to replay it. They are held back while
 * the score card is open, see the shield below.
 */
const SHORTCUT_KEYS: ReadonlySet<string> = new Set([
  "1",
  "2",
  "3",
  "4",
  "5",
  "6",
  "7",
  "8",
  "c",
  "r",
]);

/**
 * The two keys the outcome row takes off the rest of the page.
 *
 * The digits are not in here on purpose. The outcome row wants 1 to 4 for
 * itself, and it gets them, because it stands in the objection bar's row while
 * it is open and the bar is not mounted at all. That leaves c and r, which would
 * copy a line the rep has stopped reading and ask for a rewrite of a call that
 * is already finished.
 */
const OUTCOME_SHIELD_KEYS: ReadonlySet<string> = new Set(["c", "r"]);

/**
 * Where the calling list lives.
 *
 * It is the way back when a search has no uncalled lead left in it, and it is
 * the only link out of the outcome row.
 */
const LEADS_HREF = "/leads";

/**
 * The name printed on the outcome row when the business name is not known.
 *
 * The name is saved with the session, so this only shows up in a browser that
 * has storage switched off. The row still has to say something, because "Call
 * ended" with a blank beside it reads as a bug.
 */
const UNNAMED_LEAD = "This lead";

/**
 * Same rule the shortcut owners use: a field has the keyboard, so nobody else
 * does. Kept here as well because the shield below runs before their listeners
 * and must not take a key out of a box someone is typing in.
 */
function isEditable(node: unknown): boolean {
  if (!node || typeof node !== "object") return false;
  const el = node as HTMLElement;
  const tag = typeof el.tagName === "string" ? el.tagName.toLowerCase() : "";
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  return el.isContentEditable === true;
}

/**
 * A session the server will not accept any more.
 *
 * The frozen hook API reports this as `status: "error"` plus a human message and
 * never as a code, so the two strings this app can produce for it are matched
 * here: the backend's `unknown_session` error frame, and the socket client's own
 * text for a 4404 close. A miss is safe, the dashboard simply stays up with the
 * OFFLINE pill and the rose boresight.
 */
const SESSION_GONE = /does not know this session|unknown or has expired/i;

/* ------------------------------------------------------------------ */
/* The saved practice flag                                             */
/* ------------------------------------------------------------------ */

interface StoredPractice {
  sessionId: string;
  difficulty: Difficulty;
}

/**
 * Read the practice flag straight out of the stored session.
 *
 * The setup page saves the whole practice session, mode and difficulty
 * included, but `loadSession()` normalises a stored record down to the live call
 * shape and does not carry those two fields back out. They are still in the
 * stored JSON, so this reads them from it, which is what lets a rep open the
 * rehearsal from the resume link or a bookmark, with no `mode` in the address
 * bar, and still get the practice screen.
 *
 * The stored id is returned with them, because a flag from an older call must
 * never be applied to a different session. Every access is wrapped: reading
 * localStorage itself throws in a private window, and a corrupt value must
 * degrade to "not a practice call" rather than to an exception in a render path.
 */
function readStoredPractice(): StoredPractice | null {
  if (typeof window === "undefined") return null;

  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(SESSION_STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;

  const record = parsed as Record<string, unknown>;
  if (record.mode !== "practice") return null;
  if (typeof record.sessionId !== "string" || record.sessionId.trim().length === 0) return null;

  return {
    sessionId: record.sessionId.trim(),
    difficulty: isDifficulty(record.difficulty) ? record.difficulty : DEFAULT_DIFFICULTY,
  };
}

/* ------------------------------------------------------------------ */
/* The two phone numbers                                               */
/* ------------------------------------------------------------------ */

/**
 * Where the rep's own phone number is remembered.
 *
 * It is theirs, it is the same every day, and typing a phone number on a laptop
 * one minute before a cold call is exactly the friction worth removing.
 *
 * The CLIENT's number is deliberately never written here. It belongs to somebody
 * else, this browser is often shared, and it changes with every call. It comes
 * from the call the rep set up instead, which they can clear from the setup page
 * along with the rest of the session.
 */
const REP_NUMBER_KEY = "salescopilot:repNumber";

/** The saved rep number, or an empty string when there is none to read. */
function readRepNumber(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(REP_NUMBER_KEY)?.trim() ?? "";
  } catch {
    // Reading storage itself throws in a private window. No saved number then.
    return "";
  }
}

/** Remember the rep number, or forget it when the box is emptied. */
function writeRepNumber(value: string): void {
  if (typeof window === "undefined") return;
  try {
    const trimmed = value.trim();
    if (trimmed.length === 0) window.localStorage.removeItem(REP_NUMBER_KEY);
    else window.localStorage.setItem(REP_NUMBER_KEY, trimmed);
  } catch {
    // Storage is off in this browser. The field still works for this call, it
    // just will not be filled in for the next one.
  }
}

/**
 * The client number the rep typed on the setup page, for THIS call.
 *
 * loadSession() normalises a stored record down to the fields lib/types.ts
 * declares and drops everything else, so this reads the stored JSON itself, the
 * same way the practice flag above is read. The id is checked because a number
 * saved with last week's call must never end up in the dialler for this one.
 */
function readStoredClientNumber(sessionId: string): string {
  if (typeof window === "undefined") return "";

  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(SESSION_STORAGE_KEY);
  } catch {
    return "";
  }
  if (!raw) return "";

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return "";
  }
  if (typeof parsed !== "object" || parsed === null) return "";

  const record = parsed as Record<string, unknown>;
  if (record.sessionId !== sessionId) return "";
  return typeof record.clientPhone === "string" ? record.clientPhone.trim() : "";
}

/* ------------------------------------------------------------------ */
/* The lead this call came from                                        */
/* ------------------------------------------------------------------ */

/** Which lead this call was built from, as the leads screen saved it. */
interface StoredLead {
  /** The lead engine's own feature id, the join key for everything. */
  leadKey: string;
  /** The business name, already in plain words. May be empty. */
  leadName: string;
  /** The search slug the lead belongs to, for example "barber-hoboken". */
  searchId: string;
}

/** One trimmed string field out of a stored record, or an empty string. */
function storedField(record: Record<string, unknown>, name: string): string {
  const value = record[name];
  return typeof value === "string" ? value.trim() : "";
}

/**
 * Read the lead this call came from out of the stored session.
 *
 * loadSession() normalises a stored record down to the fields lib/types.ts
 * declares and drops the rest, so these three are read from the stored JSON
 * itself, the same way the practice flag and the client number above are read.
 * The id is checked, because writing this call's answer onto last week's lead
 * would put a wrong word in a file the rep's own dashboard reads.
 *
 * This is the fallback, not the source. The leads screen puts the lead key and
 * the search id in the address bar when it starts the call, and the address bar
 * wins, so the whole outcome flow still works in a browser with storage turned
 * off. Every access is wrapped for the same reason as the two readers above:
 * reading localStorage itself throws in a private window.
 *
 * TWO NAMES FOR THE SEARCH. The leads screen saves the slug as `leadSearchId`,
 * next lead below saves it as `searchId`, and both are read here. One name would
 * be nicer, but this screen is the only reader and a record written by the other
 * screen must not come back with an empty search, because the whole outcome flow
 * is behind "there is a lead key AND a search id". A missed field there is not a
 * small bug, it is the outcome row never opening at all.
 */
function readStoredLead(sessionId: string): StoredLead | null {
  if (typeof window === "undefined") return null;

  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(SESSION_STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;

  const record = parsed as Record<string, unknown>;
  if (record.sessionId !== sessionId) return null;

  const leadKey = storedField(record, "leadKey");
  if (leadKey.length === 0) return null;

  const searchId = storedField(record, "searchId") || storedField(record, "leadSearchId");

  return {
    leadKey,
    leadName: storedField(record, "leadName"),
    searchId,
  };
}

/**
 * Has this business been rung already.
 *
 * This is the same test the backend calls `uncalled` and the same one the list
 * chip "Not called yet" uses, written out here so the three cannot drift.
 *
 * WHY IT IS NOT `status === "new"`. Building a call stamps the call time and
 * deliberately leaves the status alone until the rep picks an outcome, so a lead
 * that was dialled and never graded stays on "new" for ever. Asking the server
 * for status "new" therefore hands back businesses that were already called, and
 * on a search where nothing has been graded yet it would hand back the same
 * highest value business again and again. Checked against the rep's own data:
 * car-wash-new-york has 65 leads, all of them still on "new", and 3 of them
 * carry a call time.
 *
 * Both halves are tested because that is what the backend tests. A call time is
 * what a plain dial leaves behind, and a status that has moved off "new" is what
 * a graded call leaves behind.
 */
function hasBeenCalled(lead: LeadRow): boolean {
  if (typeof lead.calledAt === "string" && lead.calledAt.trim().length > 0) return true;
  return lead.status !== "new";
}

/**
 * The receipt for the last call action, which has nowhere else to live.
 *
 * The launcher draws failures, because a failure is fixed inside the popover: a
 * missing variable, a number with no country code. It does not draw the success
 * line, and the success line is the one that has to survive the popover closing.
 * In WhatsApp link mode it is read in a different tab, after the rep has already
 * left this one, and it is the line that tells them to come back and share that
 * tab's sound. So the screen keeps it, under the bar.
 */
interface CallNote {
  /** One or two plain sentences. */
  text: string;
  /** The WhatsApp link, kept so a blocked pop up is still reachable. */
  openUrl: string | null;
}

/** A failure and its fix, as one line. Both come from lib/calling.ts already. */
function withHint(message: string, hint: string | null): string {
  return hint ? `${message} ${hint}` : message;
}

/**
 * Where the phone call is right now, read once when this screen opens.
 *
 * GET /api/call/{id}/status, through the proxy route, the same way every other
 * backend read in this app goes.
 *
 * WHY IT IS HERE. The server pushes a call_state frame only when the call
 * MOVES. A rep who reloads this page in the middle of a live call, or opens it
 * in a second tab, would therefore be told nothing at all until the call ends:
 * no chip in the bar, and a launcher offering to start a call that is already
 * running. On Twilio that is a second call the rep pays for. One read on open
 * closes that hole, and the socket covers every change after it.
 *
 * Nothing here throws and nothing here is shown as an error. When the answer
 * does not arrive the page simply keeps the idle snapshot it started with,
 * which is exactly what it had before this read existed, and the launcher
 * already says its own piece when the backend is down. Every field is checked,
 * because an unknown state would put a word in the top bar that means nothing.
 */
async function readCallStatus(
  sessionId: string,
  signal: AbortSignal,
): Promise<CallSnapshot | null> {
  let res: Response;
  try {
    res = await fetch(`/api/call/${encodeURIComponent(sessionId)}/status`, {
      headers: { accept: "application/json" },
      cache: "no-store",
      signal,
    });
  } catch {
    return null;
  }
  if (!res.ok) return null;

  let payload: unknown;
  try {
    payload = (await res.json()) as unknown;
  } catch {
    return null;
  }
  if (typeof payload !== "object" || payload === null) return null;

  const record = payload as Record<string, unknown>;
  if (!isCallProvider(record.provider) || !isCallState(record.state)) return null;

  return {
    provider: record.provider,
    state: record.state,
    callId: typeof record.callId === "string" ? record.callId : null,
    detail: typeof record.detail === "string" ? record.detail : null,
  };
}

/** Turns a failed score card request into one sentence the rep can act on. */
function describeDebriefFailure(status: number, payload: unknown): string {
  if (typeof payload === "object" && payload !== null) {
    const v = payload as Record<string, unknown>;
    const error = typeof v.error === "string" ? v.error.trim() : "";
    const detail = typeof v.detail === "string" ? v.detail.trim() : "";
    if (error && detail) return `${error}. ${detail}`;
    if (error) return `${error}.`;
    if (detail) return detail;
  }

  if (status === 404) {
    return "The server does not know this call any more, so it cannot score it. A call is kept for six hours.";
  }
  if (status === 409) {
    return "That was a real call, not a practice call, so there is no score for it.";
  }
  return `The server answered ${status} and did not say why.`;
}

/* ------------------------------------------------------------------ */
/* Page                                                                */
/* ------------------------------------------------------------------ */

export default function CallPage() {
  return (
    <Suspense fallback={<CallSkeleton />}>
      <CallScreen />
    </Suspense>
  );
}

function CallSkeleton() {
  return (
    <div className="call-grid" aria-busy="true">
      <div style={{ gridArea: "bar" }} className="flex items-center gap-3 bg-surface px-3 lg:px-4">
        <span className="h-2 w-2 shrink-0 bg-accent animate-mark" aria-hidden="true" />
        <span className="font-mono text-micro uppercase text-muted">SALES COPILOT</span>
        <span className="h-3.5 w-px bg-line-strong" aria-hidden="true" />
        <span className="font-mono text-status uppercase text-muted">CONNECTING</span>
      </div>
      <div style={{ gridArea: "glass" }} className="bg-bg" />
      <div style={{ gridArea: "wave" }} className="bg-surface" />
      <div style={{ gridArea: "rail" }} className="hidden bg-surface lg:block" />
      <div style={{ gridArea: "chips" }} className="bg-surface" />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* The screen                                                          */
/* ------------------------------------------------------------------ */

function CallScreen() {
  const searchParams = useSearchParams();
  const fromUrl = searchParams?.get("session")?.trim() ?? "";
  const modeFromUrl = searchParams?.get("mode")?.trim().toLowerCase() ?? "";

  const [stored, setStored] = useState<PreparedSession | null>(null);
  const [storedPractice, setStoredPractice] = useState<StoredPractice | null>(null);
  const [storedChecked, setStoredChecked] = useState(false);

  const [repNumber, setRepNumber] = useState("");

  // localStorage does not exist during the prerender, so the restore cannot be a
  // lazy initial state without the server and the client disagreeing about which
  // screen to paint. Reading it once after mount is the pattern, and the
  // setState calls are the whole point of the effect.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setStored(loadSession());
    setStoredPractice(readStoredPractice());
    setStoredChecked(true);
    setRepNumber(readRepNumber());
  }, []);

  const sessionId = fromUrl.length > 0 ? fromUrl : (stored?.sessionId ?? null);

  /* Which kind of call this is. The address bar wins, because it is what the
     setup page just wrote. The saved flag is the fallback for the resume link
     and for a bookmark, and it only counts when it was saved for THIS call. */
  const savedPractice =
    storedPractice !== null && sessionId !== null && storedPractice.sessionId === sessionId
      ? storedPractice
      : null;
  const practiceOn = sessionId !== null && (modeFromUrl === "practice" || savedPractice !== null);
  const difficulty: Difficulty = savedPractice?.difficulty ?? DEFAULT_DIFFICULTY;

  /* ---------------- the lead this call came from ---------------- */

  /* Storage does not exist during the prerender, so the saved half is read
     after mount, exactly like the client number further down. */
  const [storedLead, setStoredLead] = useState<StoredLead | null>(null);

  useEffect(() => {
    if (!sessionId) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setStoredLead(readStoredLead(sessionId));
  }, [sessionId]);

  /* Two spellings for each, for the same reason readStoredLead takes two names
     for the search: the leads screen writes `lead` and `search`, next lead below
     writes `leadKey` and `searchId`, and a link the rep pasted may be either.
     Reading both costs nothing and keeps the outcome row from quietly never
     opening on a call that really did come from the list. */
  const urlLeadKey =
    (searchParams?.get("leadKey")?.trim() || searchParams?.get("lead")?.trim()) ?? "";
  const urlSearchId =
    (searchParams?.get("searchId")?.trim() || searchParams?.get("search")?.trim()) ?? "";
  const leadKey = urlLeadKey.length > 0 ? urlLeadKey : (storedLead?.leadKey ?? "");
  const searchId = urlSearchId.length > 0 ? urlSearchId : (storedLead?.searchId ?? "");
  const leadName = storedLead?.leadName ?? "";

  /* A call that came out of the calling list. Every piece of the outcome flow
     is behind this one flag, so a call the rep set up by hand renders the same
     screen it always did. A practice call is never a lead call: there is nobody
     on the other end to write an answer about. */
  const isLead = !practiceOn && leadKey.length > 0 && searchId.length > 0;

  /* ---------------- what happened on the call ---------------- */

  /** True once the outcome row has taken the objection bar's row. */
  const [outcomeOpen, setOutcomeOpen] = useState(false);
  /** True once the rep has pressed End, which is what arms the strip button. */
  const [endPressed, setEndPressed] = useState(false);
  /** True while an answer is being written, or the next lead is being built. */
  const [outcomeBusy, setOutcomeBusy] = useState(false);
  /** One plain sentence about the last thing that failed, or null. */
  const [outcomeError, setOutcomeError] = useState<string | null>(null);
  /**
   * The next uncalled lead in this search, or null when there is not one.
   *
   * The whole row is held, not just its key, because the next call needs the
   * business phone number as well. It is the only place that number can come
   * from: the call context the backend builds does not carry it, so a key on its
   * own would open the next call with an empty dialler and send the rep back to
   * the list to read the number, which is the walk this loop exists to remove.
   */
  const [nextRow, setNextRow] = useState<LeadRow | null>(null);
  /** Bumped to ask for that lookup again, after an answer is written. */
  const [lookupTick, setLookupTick] = useState(0);

  /* Re entry guard for Next lead. It is a ref and not the busy flag above,
     because the outcome row flushes a late note through onPick and then calls
     onNext in the very same tick, and a state flag set by the first of those is
     still false inside the second. */
  const movingRef = useRef(false);

  const {
    status,
    connState,
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
    devices,
    quickActions,
    sensitivity,
    promptStyle,
    setPromptStyle,
    rephrase,
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
  } = useTeleprompter(sessionId, {
    practice: practiceOn,
    difficulty,
    language: stored?.language ?? "en",
  });

  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const [nudgeOff, setNudgeOff] = useState(false);
  const [copiedId, setCopiedId] = useState(false);

  const activeTimerRef = useRef(0);
  const copyTimerRef = useRef(0);
  const logButtonRef = useRef<HTMLButtonElement | null>(null);

  /* ---------------- the call page never scrolls ---------------- */

  useEffect(() => {
    if (!sessionId) return;
    const root = document.documentElement;
    root.dataset.lock = "1";
    return () => {
      delete root.dataset.lock;
    };
  }, [sessionId]);

  /* ---------------- objection chips ---------------- */

  const fireAction = useCallback(
    (key: string) => {
      quickAction(key);
      setActiveKey(key);
      window.clearTimeout(activeTimerRef.current);
      activeTimerRef.current = window.setTimeout(() => setActiveKey(null), ACTIVE_MS);
    },
    [quickAction],
  );

  useEffect(() => () => window.clearTimeout(activeTimerRef.current), []);

  /* ---------------- manual injection ---------------- */

  const handleManual = useCallback(
    (text: string, stream: StreamKind) => {
      const value = text.trim();
      if (value.length === 0) return;
      sendManual(value, stream);
    },
    [sendManual],
  );

  const handleReset = useCallback(() => {
    setActiveKey(null);
    reset();
  }, [reset]);

  /* ---------------- session chip ---------------- */

  const copySessionId = useCallback(() => {
    if (!sessionId) return;
    const done = () => {
      setCopiedId(true);
      window.clearTimeout(copyTimerRef.current);
      copyTimerRef.current = window.setTimeout(() => setCopiedId(false), COPIED_MS);
    };
    try {
      if (navigator.clipboard && window.isSecureContext) {
        void navigator.clipboard.writeText(sessionId).then(done, () => undefined);
        return;
      }
    } catch {
      // Nothing to recover. The id is also in the address bar.
    }
    done();
  }, [sessionId]);

  useEffect(() => () => window.clearTimeout(copyTimerRef.current), []);

  /* ---------------- the phone transcript sheet ---------------- */

  const closeLog = useCallback(() => {
    setLogOpen(false);
    logButtonRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!logOpen) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeLog();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [logOpen, closeLog]);

  /* ---------------- practice: the made up client level ---------------- */

  const [clientLevel, setClientLevel] = useState(0);
  const [reducedMotion, setReducedMotion] = useState(false);
  const clientSpeaking = practiceOn && practice.clientSpeaking;

  /* Read once and then subscribed to, the same way the wave and the objection
     bar read it, so the setting can be changed mid call and the page follows. */
  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const apply = () => setReducedMotion(query.matches);
    apply();
    query.addEventListener("change", apply);
    return () => query.removeEventListener("change", apply);
  }, []);

  /*
   * COSMETIC ONLY. This is not measured audio and it never can be.
   *
   * The client's voice is made by the browser speech engine and played straight
   * out of the speakers. It never passes through an AudioContext, so there is no
   * level to read. Without a made up number the client lane would be a dead flat
   * line for the whole time the client is talking, which this instrument is
   * built to mean "nothing is arriving", the one lie it must not tell. So while
   * the client speaks the lane is fed a wobble, and the moment it stops the lane
   * goes back to the quiet floor.
   *
   * Under reduced motion the timer never starts. The wave is exempt from the
   * reduced motion rules only because it shows a measurement, and this number is
   * not one, so the lane is held at STEADY_CLIENT_LEVEL instead of shaking
   * twelve times a second for the whole client turn.
   */
  useEffect(() => {
    if (!clientSpeaking || reducedMotion) return;
    const timer = window.setInterval(() => {
      setClientLevel(0.3 + Math.random() * 0.4);
    }, SYNTHETIC_LEVEL_MS);
    return () => window.clearInterval(timer);
  }, [clientSpeaking, reducedMotion]);

  /* ---------------- practice: the score card ---------------- */

  const [debriefOpen, setDebriefOpen] = useState(false);
  const [debriefLoading, setDebriefLoading] = useState(false);
  const [debriefError, setDebriefError] = useState<string | null>(null);
  const [debriefData, setDebriefData] = useState<DebriefData | null>(null);

  /** True once the score for this ending has been asked for, so it is asked once. */
  const scoreAskedRef = useRef(false);

  const loadDebrief = useCallback(async () => {
    if (!sessionId) return;

    setDebriefOpen(true);
    setDebriefLoading(true);
    setDebriefError(null);

    let res: Response;
    try {
      res = await fetch(`/api/practice/debrief/${encodeURIComponent(sessionId)}`, {
        cache: "no-store",
      });
    } catch (err) {
      setDebriefLoading(false);
      setDebriefError(
        err instanceof Error && err.message
          ? `The app could not reach the server for the score. ${err.message}`
          : "The app could not reach the server for the score.",
      );
      return;
    }

    let payload: unknown = null;
    try {
      payload = (await res.json()) as unknown;
    } catch {
      payload = null;
    }

    setDebriefLoading(false);

    if (!res.ok) {
      setDebriefError(describeDebriefFailure(res.status, payload));
      return;
    }
    if (!isDebrief(payload)) {
      setDebriefError("The score came back with parts missing, so it cannot be shown.");
      return;
    }

    setDebriefData(payload);
  }, [sessionId]);

  /* The call is over, so ask for the score once. The flag is cleared when a new
     practice call starts, which is what arms the next ending. */
  useEffect(() => {
    if (!practiceOn) return;
    if (!practice.over) {
      scoreAskedRef.current = false;
      return;
    }
    if (scoreAskedRef.current) return;
    scoreAskedRef.current = true;
    void loadDebrief();
  }, [practiceOn, practice.over, loadDebrief]);

  const closeDebrief = useCallback(() => {
    setDebriefOpen(false);
  }, []);

  /*
   * The keyboard shield for the score card.
   *
   * The call screen is still mounted behind the panel, so the objection bar and
   * the teleprompter still have their window listeners up. Nothing inside the
   * panel is a text box, so their own "a field has the keyboard" guard does not
   * catch it, and a rep reading the score who pressed 3 would fire a chip on a
   * call that is finished, or press c and lose whatever was in their clipboard.
   *
   * One listener in the capture phase on window runs before all of them and
   * swallows only those ten keys, only while the panel is open. Escape and Tab
   * are deliberately not in the set, because the panel itself is listening for
   * them. Propagation is stopped, never the default action, so a key still does
   * whatever the browser would do with it.
   */
  useEffect(() => {
    if (!debriefOpen) return;

    function shield(event: KeyboardEvent) {
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (!SHORTCUT_KEYS.has(event.key.toLowerCase())) return;
      if (isEditable(event.target) || isEditable(document.activeElement)) return;
      event.stopPropagation();
    }

    window.addEventListener("keydown", shield, true);
    return () => window.removeEventListener("keydown", shield, true);
  }, [debriefOpen]);

  /*
   * The keyboard shield for the outcome row.
   *
   * The dashboard is still mounted behind it, so the teleprompter still has its
   * window listener up, and c would copy a line the rep has stopped reading
   * while r would ask for a rewrite of a call that is over.
   *
   * The digits are handled the other way round, and on purpose: the outcome row
   * wants 1 to 4 for itself, so they are NOT swallowed here. The objection bar
   * is not mounted at all while the row is open, which leaves the digits free to
   * reach the row however it listens for them.
   *
   * Propagation is stopped, never the default action, and never while a field
   * has the keyboard, so typing a c in the note box still types a c.
   */
  useEffect(() => {
    if (!outcomeOpen) return;

    function shield(event: KeyboardEvent) {
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (!OUTCOME_SHIELD_KEYS.has(event.key.toLowerCase())) return;
      if (isEditable(event.target) || isEditable(document.activeElement)) return;
      event.stopPropagation();
    }

    window.addEventListener("keydown", shield, true);
    return () => window.removeEventListener("keydown", shield, true);
  }, [outcomeOpen]);

  /* Reopen a scorecard the rep closed. A card that already arrived is just shown
     again, and one that never arrived is asked for again, so this doubles as the
     retry after a failed fetch. */
  const showScore = useCallback(() => {
    if (debriefData !== null) {
      setDebriefOpen(true);
      return;
    }
    void loadDebrief();
  }, [debriefData, loadDebrief]);

  /* Same setup, fresh call, no page load. reset() clears the log and what the
     copilot remembers, then the client is asked to speak first again. The score
     flag is left alone: it is cleared by the effect above when the hook reports
     the call is running again, so a start that fails to send cannot reopen the
     score card behind the rep. */
  const practiseAgain = useCallback(() => {
    setDebriefOpen(false);
    setDebriefData(null);
    setDebriefError(null);
    setDebriefLoading(false);
    setActiveKey(null);
    reset();
    startPractice();
  }, [reset, startPractice]);

  /* ---------------- the phone call ---------------- */

  /* Empty, not the fallback list. lib/calling.ts hands the fallback back inside
     its error result together with the sentence that says why every option is
     off, and the fallback drawn on its own would say "this app cannot call"
     when the truth is "the list has not arrived yet". The launcher has an
     honest empty state for exactly this, and it asks for the list itself the
     moment it is opened. */
  const [providers, setProviders] = useState<CallProviderInfo[]>([]);
  const [callProvider, setCallProvider] = useState<CallProvider>("manual");
  const [toNumber, setToNumber] = useState("");
  const [callBusy, setCallBusy] = useState(false);
  const [callError, setCallError] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [callNote, setCallNote] = useState<CallNote | null>(null);

  /**
   * Read the provider list. Nothing here throws, the result carries the failure.
   *
   * The list is never cached, because `ready` is computed from the server
   * environment: a rep can add TWILIO_ACCOUNT_SID to backend/.env, restart the
   * server, reopen the popover and see the option come alive.
   */
  const loadProviders = useCallback(async (signal?: AbortSignal) => {
    const result = await fetchProviders(signal);
    // An aborted request is a screen that has gone away, so it writes nothing.
    if (signal?.aborted) return;
    setProviders([...result.providers]);
    setListError(result.kind === "ok" ? null : withHint(result.message, result.hint));
  }, []);

  /**
   * Catch up with a call that was already running before this screen opened.
   *
   * The answer is handed to the hook, so it lands on the very same `call` the
   * socket writes and the chip and the launcher can never disagree. The hook
   * drops it if a call_state frame has already arrived, so a slow answer cannot
   * put a stale word back in the bar.
   */
  const syncCallState = useCallback(
    async (signal: AbortSignal) => {
      if (!sessionId) return;
      const snapshot = await readCallStatus(sessionId, signal);
      // A cancelled read belongs to a screen that has gone away.
      if (snapshot === null || signal.aborted) return;
      applyCallState(snapshot);
    },
    [applyCallState, sessionId],
  );

  /* One read when the screen opens: which ways there are to call, and whether
     one is already running. Practice calls skip both: there is nobody to call,
     so the launcher is not mounted and neither answer would be read.

     The status read is what makes a reload safe. Without it a rep who refreshed
     during a live call would be shown an empty bar and a Call button, and
     pressing it would place a second call that Twilio bills for. */
  useEffect(() => {
    if (!sessionId || practiceOn) return;
    const controller = new AbortController();
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadProviders(controller.signal);
    void syncCallState(controller.signal);
    return () => controller.abort();
  }, [sessionId, practiceOn, loadProviders, syncCallState]);

  /* The client number the rep already typed on the setup page. Storage does not
     exist during the prerender, so it is read here rather than as an initial
     state. It is assigned, never merged: opening a different call must empty the
     box, because the surest way to dial the wrong person is to leave the last
     client's number sitting in it. */
  useEffect(() => {
    if (!sessionId || practiceOn) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setToNumber(readStoredClientNumber(sessionId));
  }, [sessionId, practiceOn]);

  const rememberRepNumber = useCallback((value: string) => {
    setRepNumber(value);
    writeRepNumber(value);
  }, []);

  /* A failure belongs to the option that caused it, so it goes when the rep
     picks another one. Leaving a Twilio message on screen under a WhatsApp
     choice would read as WhatsApp being broken too. */
  const pickProvider = useCallback((next: CallProvider) => {
    setCallProvider(next);
    setCallError(null);
  }, []);

  const refreshProviders = useCallback(() => {
    void loadProviders();
  }, [loadProviders]);

  const dismissNote = useCallback(() => setCallNote(null), []);

  /**
   * Place the call.
   *
   * Three answers, three different words. A refusal is the backend saying no in
   * a sentence written for the rep, so it is shown word for word. An error means
   * the request never landed. A start means the phone is about to ring, or, for
   * link mode, that WhatsApp is opening in another tab.
   */
  const startTheCall = useCallback(() => {
    if (!sessionId || callBusy) return;

    setCallBusy(true);
    setCallError(null);
    setCallNote(null);

    void (async () => {
      const result = await startCall({
        sessionId,
        provider: callProvider,
        toNumber: toNumber.trim() || null,
        repNumber: repNumber.trim() || null,
      });

      if (result.kind === "error") {
        setCallError(withHint(result.message, result.hint));
        setCallBusy(false);
        return;
      }

      if (result.kind === "refused") {
        setCallError(result.call.message);
        setCallBusy(false);
        return;
      }

      const url = result.call.openUrl;
      if (url === null) {
        /* Twilio is the one provider whose audio the server carries, so it is
           the one where the rep must NOT go and share a tab as well. Saying so
           right here is the difference between one clean lane and the same
           voice arriving twice on it. Every other provider keeps the server's
           own sentence, which already asks for the shared sound. */
        const bridged = callProvider === "twilio";
        setCallNote({
          text: bridged
            ? `${result.call.message} The app hears this call from the phone line, so there is no tab to share.`
            : result.call.message,
          openUrl: null,
        });
        setCallBusy(false);
        return;
      }

      /* WhatsApp link mode, the only provider that answers with a link. The tab
         is opened here so the rep does not have to hunt for it.

         The return value is deliberately not read. window.open answers null
         whenever noopener is asked for, success or not, so a check on it would
         report every single open as blocked. noopener is worth keeping: without
         it the page we just opened gets a handle on this one. So the note covers
         both outcomes in one line and the link is kept below it, which is the
         way through when a browser did block the tab. */
      window.open(url, "_blank", "noopener,noreferrer");
      setCallNote({
        /* The server sentence already says to share the tab sound, so this adds
           only what it cannot know: which button does that, and where the link
           is if the tab never appeared. */
        text: `${result.call.message} If the new tab did not open, use the link below. SOURCES here is the button that shares the sound.`,
        openUrl: url,
      });
      setCallBusy(false);
    })();
  }, [callBusy, callProvider, repNumber, sessionId, toNumber]);

  /**
   * End the call.
   *
   * Twilio is the only provider the server can really hang up, because it is the
   * only one holding the call. Every other one lives somewhere the app cannot
   * reach: the rep's own phone, their own WhatsApp, or Meta's side of a Cloud
   * API call. For those the server clears the state and this app stops following
   * the call, and the note says exactly that rather than claiming a phone was
   * put down when it was not.
   */
  const hangUpCall = useCallback(() => {
    if (!sessionId || callBusy) return;

    const ours = call.provider === "twilio";

    setCallBusy(true);
    setCallError(null);
    setCallNote(null);

    /* The rep pressed End, so as far as they are concerned this call is over.
       That is what arms the strip button, whatever the server then says about
       the line itself. On a call that did not come from a lead this flag is read
       by nothing at all. */
    if (isLead) setEndPressed(true);

    void (async () => {
      const result = await hangUp(sessionId);
      if (result.kind === "error") {
        setCallError(withHint(result.message, result.hint));
      } else {
        setCallNote({
          text: ours
            ? "The call was ended."
            : "The app stopped following this call. If it is still going, end it where you started it.",
          openUrl: null,
        });
        /* The line really is down, so this is the moment to ask what happened.
           The row opens by itself here and only here, because pressing End is
           the one action that means the call is finished. */
        if (isLead) setOutcomeOpen(true);
      }
      setCallBusy(false);
    })();
  }, [call.provider, callBusy, isLead, sessionId]);

  /* ---------------- what happened on the call ---------------- */

  const openOutcome = useCallback(() => {
    setOutcomeOpen(true);
    setOutcomeError(null);
  }, []);

  /* Skip means not now, so the row stands down and the eight chips come back.
     Nothing is written and nothing is lost: the strip button is still there and
     brings the row straight back. */
  const skipOutcome = useCallback(() => {
    setOutcomeOpen(false);
    setOutcomeError(null);
  }, []);

  /**
   * Look up the next lead in this search that has not been called.
   *
   * It runs when the row opens rather than when Next lead is pressed, for two
   * reasons. The row has to know whether there IS a next lead before the rep
   * picks anything, because that is the difference between "Press Next lead" and
   * "That was the last lead". And doing the read while the rep is reading the
   * four buttons takes it off the path between two calls.
   *
   * The lead now on the glass is skipped by key, not by its status, because a
   * status write that failed would otherwise hand the rep the same business
   * twice in a row.
   *
   * WHY THE SERVER IS NOT ASKED FOR A STATUS. It used to ask for status "new",
   * which is not the same question as "has not been called". Building a call
   * stamps the call time and leaves the status alone until the rep grades it, so
   * status "new" still holds every business that was dialled and never graded,
   * and the row would offer one of them again. The read now asks only for leads
   * with a phone, best money first, and hasBeenCalled does the rest here, which
   * is the same test the list chip and the backend both use.
   */
  useEffect(() => {
    if (!isLead || !outcomeOpen) return;

    const controller = new AbortController();

    void (async () => {
      const list = await fetchLeads(
        searchId,
        { hasPhone: true, sort: "value" },
        controller.signal,
      );
      // An aborted read belongs to a row that has gone away.
      if (controller.signal.aborted) return;

      if (list.kind === "error") {
        setNextRow(null);
        setOutcomeError(withHint(list.message, list.hint));
        return;
      }

      const row = list.leads.find((lead) => lead.key !== leadKey && !hasBeenCalled(lead));
      setNextRow(row === undefined ? null : row);
    })();

    return () => controller.abort();
  }, [isLead, outcomeOpen, leadKey, searchId, lookupTick]);

  /**
   * Write what happened into this lead's row.
   *
   * It goes to POST /api/leads/{search}/{key}/status, which lands in the same
   * pipeline.json the rep's own lead dashboard reads, so the two can never
   * disagree about who has been called. Nothing here throws: lib/leads.ts
   * answers with a result either way, and a failure comes back as one plain
   * sentence with the four buttons still live, so pressing again is the retry.
   *
   * A written answer also asks for the next lead lookup again. The list has just
   * changed, and if the first lookup failed this is the moment it matters.
   */
  const pickOutcome = useCallback(
    (next: LeadStatus, notes: string) => {
      if (!isLead) return;

      setOutcomeBusy(true);
      setOutcomeError(null);

      void (async () => {
        const result = await setLeadStatus(searchId, leadKey, next, notes);
        if (result.kind === "error") {
          setOutcomeError(withHint(result.message, result.hint));
          setOutcomeBusy(false);
          return;
        }
        setOutcomeBusy(false);
        setLookupTick((tick) => tick + 1);
      })();
    },
    [isLead, leadKey, searchId],
  );

  /**
   * Build the next lead's call and open it.
   *
   * WHY A WHOLE PAGE LOAD. Changing the address bar in place would keep this
   * component mounted, and the hook only tears the socket and the microphones
   * down on a new session id, not the transcript and not the line on the glass.
   * The rep would start the next call reading the last one's answer. A real
   * navigation gives them an empty screen with the new context behind it, which
   * is what "the next lead" has to mean. The busy flag is deliberately left on:
   * the page is leaving, and a second press in that gap would build a context
   * for a call nobody is going to make.
   */
  const nextLead = useCallback(() => {
    if (!isLead || movingRef.current) return;

    const row = nextRow;
    if (row === null) {
      setOutcomeError("There is no other lead to call in this search right now.");
      return;
    }

    movingRef.current = true;
    setOutcomeBusy(true);
    setOutcomeError(null);

    void (async () => {
      /* The same knowledge base the setup page saved, which is what the leads
         screen sent for this call too. lib/leads.ts refuses an empty one with a
         sentence saying where to fill it in, so there is nothing to check
         here. */
      const profile = loadProfile();
      const built = await startLeadCall(searchId, row.key, {
        knowledgeBase: profile?.knowledgeBase ?? "",
        language: stored?.language ?? profile?.language ?? "en",
      });

      if (built.kind === "error") {
        setOutcomeError(withHint(built.message, built.hint));
        setOutcomeBusy(false);
        movingRef.current = false;
        return;
      }

      /* Saved with three things beside the session itself.

         The search id, so a rep who reloads the next call still gets the outcome
         row with nothing in the address bar. The same slug goes in under the
         leads screen's own name as well, so one record reads the same whichever
         screen wrote it.

         And the business phone number, which is the field this used to drop. The
         call context the backend builds does not carry a number, so if it is not
         put here the dialler on the next screen opens empty and the rep has to
         walk back to the list to read it. That walk is the one thing the loop
         exists to remove, and without this line it happened on every call from
         the second one on. It is written exactly the way the leads screen writes
         it, trimmed, or null when the row has no number. */
      const phone = typeof row.phone === "string" ? row.phone.trim() : "";
      const record: PreparedSession &
        StoredLead & { clientPhone: string | null; leadSearchId: string } = {
        ...built.session,
        searchId,
        leadSearchId: searchId,
        clientPhone: phone.length > 0 ? phone : null,
      };
      saveSession(record);

      const url =
        `/call?session=${encodeURIComponent(built.session.sessionId)}` +
        `&leadKey=${encodeURIComponent(built.session.leadKey)}` +
        `&searchId=${encodeURIComponent(searchId)}`;

      /* A real navigation, not router.push, and the rule below is turned off
         on purpose. A soft push keeps this component mounted, and everything it
         is holding, the log, the line on the glass, the call receipt, the
         number in the dialler, would carry over onto a different business. The
         page load is the cheap and total way to be sure none of it does. */
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.assign(url);
    })();
  }, [isLead, nextRow, searchId, stored?.language]);

  /* ---------------- derived ---------------- */

  const socketOpen = connState === "open";
  const browserAudio = captures.client || captures.rep;

  /* A call that is on its way or up, whoever placed it and however. */
  const callActive =
    call.state === "dialing" || call.state === "ringing" || call.state === "live";

  /**
   * The option the launcher draws.
   *
   * While a call is up this is the provider that is really on the wire, not the
   * one this browser happens to have picked. A rep who reloaded the page, or who
   * opened the call in a second tab, never picked anything here, so the live
   * panel would otherwise print the wrong sentence about a call that is running:
   * "Nothing is dialled for you" over a Twilio call that is billing by the
   * minute. When no call is up it is the rep's own pick, untouched, and that is
   * the one the start button uses.
   */
  const shownProvider = callActive ? call.provider : callProvider;

  /*
   * A phone call the app placed, on the one provider that forks its sound to us.
   *
   * Twilio is that provider, and only Twilio. The other three put the call on
   * the rep's own phone or their own WhatsApp, where this app cannot hear a
   * thing, so for those the shared tab stays the only way in and everything
   * below reads exactly as it did before any of this existed.
   *
   * Two values, not one, because the server draws the same line twice and in two
   * different places, see PHONE_AUDIO_PROVIDERS and PHONE_AUDIO_STATES in
   * backend/app/api/routes_ws.py:
   *
   *   phoneCall  a Twilio call is on its way or up. The browser is never going
   *              to be the source of this call, so it is never worth sending
   *              the rep off to share a tab for it.
   *   phoneAudio the fork is actually running. The server is filling BOTH lanes
   *              itself and refuses browser frames outright, because a lane is
   *              shared and not split, and a second source would put the same
   *              voice into one segmenter twice.
   *
   * Ringing sits between them on purpose: nothing is flowing yet, but nothing
   * ever will flow from this browser either.
   */
  const phoneCall = call.provider === "twilio" && callActive;
  const phoneAudio = call.provider === "twilio" && call.state === "live";

  const noAudio = !browserAudio && !phoneAudio;
  /* No nudge in practice: there is no client audio to plug in, only the mic.
     No nudge under a call note either: they hang in the same place, and the note
     is the more specific of the two, since it already says to open SOURCES.
     No nudge during a phone call: the server is carrying that sound, or is about
     to, and "share the call tab" would send the rep to set up a second source
     the server then throws away frame by frame. */
  const showNudge =
    Boolean(sessionId) &&
    socketOpen &&
    noAudio &&
    !phoneCall &&
    !nudgeOff &&
    !practiceOn &&
    callNote === null;
  const sessionGone = status === "error" && SESSION_GONE.test(error ?? "");

  /* When the outcome row is worth offering.
     The rep pressed End, or the copilot has written at least one line, which is
     the proof that a call really happened. Before either of those the lead strip
     holds only the business name and carries no button at all. */
  const outcomeArmed = isLead && (endPressed || suggestion.trim().length > 0);

  /* The row stands in the objection bar's row in the grid, so it is only ever
     drawn on a lead call that is over. */
  const outcomeShown = isLead && outcomeOpen;
  const actions = quickActions.length > 0 ? quickActions : FALLBACK_ACTIONS;
  const shortId = useMemo(
    () => (sessionId ? sessionId.replace(/-/g, "").slice(0, 4).toUpperCase() : ""),
    [sessionId],
  );
  const idleHint = practiceOn
    ? captures.rep
      ? "PRESS START, THE CLIENT TALKS FIRST"
      : "PRESS START. THE BROWSER WILL ASK TO USE YOUR MIC, SAY ALLOW"
    : phoneCall
      ? phoneAudio
        ? "THE PHONE CALL FEEDS THE APP, NOTHING TO SHARE"
        : "PICK UP YOUR PHONE, THEN WE DIAL THE CLIENT"
      : noAudio
        ? "OPEN SOURCES ABOVE, OR TYPE A LINE IN THE LOG"
        : "PRESS 1 TO 8 FOR AN INSTANT LINE";

  /* The wave in practice mode.
     client lane: alive for the whole call (there IS a client on the line), with
     the made up level above while it talks.
     rep lane: shown as muted while the client talks, because that is the truth,
     the hook holds the rep's frames back until the client stops. */
  const waveLevels = practiceOn
    ? {
        client: clientSpeaking ? (reducedMotion ? STEADY_CLIENT_LEVEL : clientLevel) : 0,
        rep: levels.rep,
      }
    : levels;
  const waveSpeaking = practiceOn ? { client: clientSpeaking, rep: speaking.rep } : speaking;
  const waveActive = practiceOn
    ? { client: practice.started && !practice.over, rep: captures.rep }
    : captures;
  const waveMuted = practiceOn ? { client: false, rep: muted.rep || clientSpeaking } : muted;

  /* The wave on a real call. It asks "is anything arriving on this lane", not
     "did this browser plug it in", so a phone call the server is carrying counts
     as plugged in on both lanes. Every other case is the browser's captures,
     unchanged. */
  const liveActive = phoneAudio ? PHONE_LANES : captures;

  /* ---------------- no session, or one the server has forgotten ---------------- */

  if (!storedChecked && fromUrl.length === 0) return <CallSkeleton />;

  if (!sessionId) {
    return (
      <SessionGate
        kicker="NO SESSION"
        line="No call yet. Go back and set one up."
        body="The teleprompter needs a call set up first. That takes about twenty seconds."
      />
    );
  }

  // A 4404 never reconnects, so without this the glass would sit there inviting
  // the rep to start talking into a socket that is permanently dead. The guard on
  // the suggestion keeps the promise that an error never takes a line off the
  // glass while the rep may still be reading it.
  if (sessionGone && suggestion.length === 0) {
    return (
      <SessionGate
        kicker="SESSION EXPIRED"
        line="This call has expired. Set up a new one."
        body="A call is kept for six hours. After that, set it up again before the copilot can answer."
      />
    );
  }

  /* ---------------- the dashboard ---------------- */

  return (
    /* overflow is visible so the sources popover can hang below the top bar
       instead of being clipped by the grid. The document itself is locked by
       html[data-lock="1"], so nothing can scroll either way. */
    <div className="call-grid" style={{ overflow: "visible" }}>
      {/* The bar is the one row whose content cannot be made to fit a 360 px
          phone: the pill has a 116 px floor, the sources trigger and the latency
          column are fixed. Every gap and label that could shed width below sm
          does, and the left group clips on X rather than letting the pill ride
          out over the latency meter. overflow-x:clip leaves the Y axis visible,
          so focus rings are not cut. */}
      <header
        style={{ gridArea: "bar" }}
        className="relative z-30 flex min-w-0 items-center justify-between gap-2 bg-surface px-2 sm:gap-3 sm:px-3 lg:gap-4 lg:px-4"
      >
        <div className="flex min-w-0 items-center gap-2 overflow-x-clip sm:gap-2.5 lg:gap-3">
          <span className="h-2 w-2 shrink-0 bg-accent animate-mark" aria-hidden="true" />
          <span className="hidden font-mono text-micro uppercase text-muted sm:inline">
            SALES COPILOT
          </span>
          <span className="hidden h-3.5 w-px shrink-0 bg-line-strong sm:block" aria-hidden="true" />
          <StatusPill state={status} detail={null} streaming={streaming} />
          {practiceOn ? (
            <span
              className="hidden shrink-0 items-center gap-1.5 rounded-hair border border-line-strong px-2 font-mono text-micro uppercase text-accent-2 sm:inline-flex"
              title="This is a practice call. The client is a robot."
            >
              PRACTICE
            </span>
          ) : null}
          <button
            type="button"
            onClick={copySessionId}
            title={
              stored?.clientTitle ? `${stored.clientTitle} (copy session id)` : "Copy session id"
            }
            aria-label="Copy the full session id"
            className="hidden shrink-0 items-center gap-1.5 rounded-hair px-1 font-mono text-micro uppercase text-dim transition-colors duration-[120ms] hover:text-muted sm:inline-flex"
          >
            {copiedId ? (
              <>
                <Check className="h-3 w-3 text-ok" aria-hidden="true" />
                <span className="text-ok">COPIED</span>
              </>
            ) : (
              <>
                <Copy className="h-3 w-3" aria-hidden="true" />
                <span className="tabnum">SES {shortId}</span>
              </>
            )}
          </button>

          {/* The next call is a different client, so it needs its own notes.

              Where it goes depends on where this call came from, because "the
              next call" means two different things. On a lead call the next
              business is already sitting in the list, so this goes back to the
              list. On a call the rep typed out by hand there is no list, so it
              goes back to the form with what you sell still filled in and the
              client half empty.

              It is drawn as a bordered chip, the same as CALL and SOURCES on the
              right, because it is an action. The first version wore the dim micro
              type of the session id beside it, which reads as decoration, and a
              rep looking for it could not find it. */}
          <Link
            href={isLead ? LEADS_HREF : "/"}
            title={
              isLead
                ? "Back to your lead list to pick the next business"
                : "Start a call with a different client"
            }
            className="flex h-[26px] shrink-0 items-center gap-1.5 rounded-hair border border-line-strong px-2.5 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] ease-out hover:bg-surface-2 hover:text-text"
          >
            <Plus className="h-3 w-3" aria-hidden="true" />
            <span>New call</span>
          </Link>
        </div>

        <div className="flex shrink-0 items-center gap-2 sm:gap-2.5 lg:gap-4">
          <LatencyMeter
            stt={latency.stt}
            firstToken={latency.firstToken}
            total={latency.total}
            rtt={latency.rtt}
          />
          <span className="hidden h-3.5 w-px shrink-0 bg-line-strong lg:block" aria-hidden="true" />
          <button
            ref={logButtonRef}
            type="button"
            onClick={() => setLogOpen((open) => !open)}
            aria-expanded={logOpen}
            aria-controls="transcript-sheet"
            aria-label={`Transcript, ${transcript.length} rows`}
            className="flex h-[26px] shrink-0 items-center gap-1.5 rounded-hair border border-line-strong px-2 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] hover:bg-surface-2 hover:text-text sm:px-2.5 lg:hidden"
          >
            <ScrollText className="h-3 w-3" aria-hidden="true" />
            <span aria-hidden="true" className="tabnum">
              <span className="hidden sm:inline">LOG </span>
              {transcript.length}
            </span>
          </button>
          {/* The call, next to the audio sources, because they are the same job
              done in two halves: how the voices get on the line, and how they
              get into this app. The chip draws nothing at all while the call is
              idle, so a rep who dials with their own phone sees the bar exactly
              as it was before any of this existed.

              Both are held back below sm, and the reason is width, not taste.
              Design spec 4.4 gives the 360 px bar exactly four things: the mark,
              the status pill, LOG and SOURCES. They already fill it. A fifth
              control there does not wrap, it eats the left group, and the first
              thing to go under the clip is the status pill, which is the one
              instrument that has to be readable without a saccade. So the call
              is placed from a laptop. That is a real cost for exactly one case,
              a Twilio call started on a phone, where the server hears the line
              and the browser does not have to, and it is still the better trade
              than a status pill nobody can read. Every other provider needs a
              shared tab or a virtual cable anyway, and a phone browser has
              neither. */}
          {practiceOn ? null : (
            <div className="hidden shrink-0 items-center gap-2 sm:flex sm:gap-2.5">
              <CallStateChip state={call.state} />
              <CallLauncher
                providers={providers}
                value={shownProvider}
                state={call.state}
                busy={callBusy}
                error={callError ?? listError}
                toNumber={toNumber}
                repNumber={repNumber}
                onProvider={pickProvider}
                onToNumber={setToNumber}
                onRepNumber={rememberRepNumber}
                onStart={startTheCall}
                onHangUp={hangUpCall}
                onRefresh={refreshProviders}
              />
            </div>
          )}
          <AudioSourcePicker
            devices={devices}
            captures={captures}
            muted={muted}
            sensitivity={sensitivity}
            busy={busy}
            onStartClient={startClient}
            onStartRep={startRep}
            onStop={stopStream}
            onToggleMute={toggleMute}
            onRefreshDevices={refreshDevices}
            onSensitivity={setSensitivity}
          />
        </div>

        {showNudge ? (
          <div className="absolute right-3 top-[calc(100%+6px)] z-40 hidden w-[268px] border border-line-strong bg-surface-2 p-3 lg:block">
            <div className="flex items-start justify-between gap-2">
              <p className="font-mono text-micro uppercase text-muted">NO AUDIO YET</p>
              <button
                type="button"
                onClick={() => setNudgeOff(true)}
                aria-label="Dismiss the audio hint"
                className="-mr-1 -mt-1 flex h-5 w-5 items-center justify-center rounded-hair text-dim transition-colors duration-[120ms] hover:text-muted"
              >
                <X className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            </div>
            <p className="mt-2 font-sans text-[12px] leading-[18px] text-muted">
              Open SOURCES above and share the call tab, or pick a virtual cable. With no hardware at
              all, type what they said in the log and the copilot answers it.
            </p>
          </div>
        ) : null}

        {/* The call receipt, hung under the bar in the same place as the hint
            above. It is not an error, so it is allowed to sit over the top of
            the glass for the few seconds it takes to read: it only ever appears
            in the moment after the rep pressed a call button, when the glass is
            still holding the idle line and not an answer they are reading out
            loud. On a phone it is moved off the glass entirely, see below. */}
        {callNote ? (
          <div className="absolute right-3 top-[calc(100%+6px)] z-40 hidden w-[300px] lg:block">
            <CallNotePanel note={callNote} onDismiss={dismissNote} />
          </div>
        ) : null}
      </header>

      <div style={{ gridArea: "glass" }} className="relative flex min-h-0 min-w-0 flex-col bg-bg">
        {/* The practice state word, in the one place the rep is already looking.
            It is mounted for the whole practice call and not only while the
            client talks, so the glass below it never moves under a reading eye
            when the client starts or stops. */}
        {practiceOn ? (
          <div className="flex h-8 shrink-0 items-center justify-between gap-3 border-b border-line px-[var(--tp-pad-x)]">
            {/* Not a live region. The practice strip below already announces
                every turn change, and announcing it twice is worse than not
                announcing it here at all. */}
            <span className="flex min-w-0 items-center gap-2">
              <span
                className={`h-1.5 w-1.5 shrink-0 ${
                  clientSpeaking ? "bg-accent animate-breathe" : "bg-dim"
                }`}
                aria-hidden="true"
              />
              <span
                className={`font-mono text-micro uppercase ${
                  clientSpeaking ? "text-accent" : "text-muted"
                }`}
              >
                {practice.over
                  ? "PRACTICE CALL OVER"
                  : !practice.started
                    ? "PRACTICE CALL READY"
                    : clientSpeaking
                      ? "CLIENT IS TALKING"
                      : "YOUR TURN"}
              </span>
            </span>
            {/* The way back to a scorecard that was closed. Without it, closing
                the panel would leave the rep on a call that is over with no
                button left anywhere on the screen. */}
            {practice.over && !debriefOpen ? (
              <button
                type="button"
                onClick={showScore}
                className="flex h-[22px] shrink-0 items-center rounded-hair border border-line-strong px-2 font-mono text-micro uppercase text-accent transition-colors duration-[120ms] hover:bg-surface-2"
              >
                SEE MY SCORE
              </button>
            ) : null}
          </div>
        ) : null}

        {/* The lead strip, the same one row shape the practice strip above uses,
            in the same place. It says which business is on the line, which a rep
            forty calls into a day genuinely needs, and it is where the outcome
            row is asked for once there is something to say. It settles before
            the first line ever lands, and its one button slot never changes
            height, so the glass under it cannot move while somebody is
            reading. */}
        {isLead ? (
          <div className="flex h-8 shrink-0 items-center justify-between gap-3 border-b border-line px-[var(--tp-pad-x)]">
            <span className="flex min-w-0 items-center gap-2">
              <span className="h-1.5 w-1.5 shrink-0 bg-dim" aria-hidden="true" />
              <span className="truncate font-mono text-micro uppercase text-muted">
                {leadName.length > 0 ? `LEAD, ${leadName}` : "FROM YOUR LEAD LIST"}
              </span>
            </span>
            {/* One slot, and only ever one thing in it. Before the row is open
                it asks for the row. While the row is open it is the door out,
                which is what a rep needs when this search has no uncalled lead
                left and Next lead is dead. It is muted, not accent, so it never
                competes with Next lead in the row below. */}
            {outcomeOpen ? (
              <Link
                href={LEADS_HREF}
                className="flex h-[22px] shrink-0 items-center gap-1.5 rounded-hair border border-line-strong px-2 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] hover:bg-surface-2 hover:text-text"
              >
                <ArrowLeft className="h-3 w-3" aria-hidden="true" />
                BACK TO THE LIST
              </Link>
            ) : outcomeArmed ? (
              <button
                type="button"
                onClick={openOutcome}
                title="The call is over, say how it went"
                className="flex h-[22px] shrink-0 items-center rounded-hair border border-line-strong px-2 font-mono text-micro uppercase text-accent transition-colors duration-[120ms] hover:bg-surface-2"
              >
                SAY WHAT HAPPENED
              </button>
            ) : null}
          </div>
        ) : null}

        <Teleprompter
          text={suggestion}
          streaming={streaming}
          trigger={trigger}
          sourceText={sourceText}
          idleHint={idleHint}
          status={status}
          promptStyle={promptStyle}
          onPromptStyle={setPromptStyle}
          onRephrase={rephrase}
        />

        {/* In practice mode the wave moves up into this column, because its own
            row is now the practice strip and this column is the only one in the
            grid with any slack in it. Same instrument, one row higher. */}
        {practiceOn ? (
          <div className="h-12 shrink-0 border-t border-line lg:h-14">
            <WaveVisualizer
              levels={waveLevels}
              speaking={waveSpeaking}
              active={waveActive}
              muted={waveMuted}
            />
          </div>
        ) : null}
      </div>

      <div style={{ gridArea: "wave" }} className="relative min-h-0 min-w-0 bg-surface">
        {practiceOn ? (
          <PracticeBar
            difficulty={practice.difficulty}
            turns={practice.turns}
            elapsedMs={practice.elapsedMs}
            started={practice.started}
            over={practice.over}
            clientSpeaking={practice.clientSpeaking}
            micOn={captures.rep && !muted.rep}
            busy={debriefLoading}
            onStart={() => void startPractice()}
            onCutIn={cutIn}
            onEnd={endPractice}
          />
        ) : (
          <WaveVisualizer levels={levels} speaking={speaking} active={liveActive} muted={muted} />
        )}
      </div>

      <div
        style={{ gridArea: "rail" }}
        className="hidden min-h-0 min-w-0 flex-col bg-surface lg:flex"
      >
        {error ? <FaultLine message={error} /> : null}
        <TranscriptRail items={transcript} className="min-h-0 flex-1" />
        <Composer
          disabled={!socketOpen}
          onSend={handleManual}
          onReset={handleReset}
          practice={practiceOn}
        />
      </div>

      {/* One row, two jobs, never both at once.

          While the call is live it is the objection matrix. Once a lead call is
          over the outcome row stands in its place, because the eight chips
          answer a client who is still on the line and there is nobody on the
          line any more. Swapping them rather than stacking them is also what
          keeps the grid at four rows and the page unable to scroll.

          The second half of the score card shield lives here too, and it is the
          honest one: while the card is up the chips are not usable, so they are
          drawn as not usable and their own digit guard turns the key down as
          well. */}
      <div style={{ gridArea: "chips" }} className="min-h-0 min-w-0">
        {outcomeShown ? (
          <CallOutcome
            leadName={leadName.length > 0 ? leadName : UNNAMED_LEAD}
            saving={outcomeBusy}
            error={outcomeError}
            hasNext={nextRow !== null}
            onPick={pickOutcome}
            onNext={nextLead}
            onSkip={skipOutcome}
          />
        ) : (
          <ObjectionBar
            actions={actions}
            disabled={!socketOpen || debriefOpen}
            activeKey={activeKey}
            onFire={fireAction}
          />
        )}
      </div>

      {logOpen ? (
        <>
          <div
            aria-hidden="true"
            onPointerDown={closeLog}
            className="fixed inset-0 z-30 bg-bg/40 lg:hidden"
          />
          <div
            id="transcript-sheet"
            role="dialog"
            aria-modal="false"
            aria-label="Transcript"
            className="fixed inset-x-0 bottom-0 z-40 flex h-[72dvh] flex-col border-t border-line-strong bg-surface lg:hidden"
          >
            <div className="flex h-10 shrink-0 items-center justify-between border-b border-line px-3">
              <span className="font-mono text-micro uppercase text-muted">CALL LOG</span>
              <button
                type="button"
                onClick={closeLog}
                aria-label="Close the transcript"
                className="flex h-7 w-7 items-center justify-center rounded-hair text-muted transition-colors duration-[120ms] hover:text-text"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            {error ? <FaultLine message={error} /> : null}
            <TranscriptRail items={transcript} className="min-h-0 flex-1" />
            <Composer
              disabled={!socketOpen}
              onSend={handleManual}
              onReset={handleReset}
              practice={practiceOn}
              autoFocus
            />
          </div>
        </>
      ) : null}

      {/* The same receipt on a small tablet, parked above the objection bar
          instead of under the top bar, because the narrow grid puts the glass
          directly under the bar and words being read out loud are never covered.
          It starts at sm, the same width the launcher itself starts at. */}
      {callNote ? (
        <div
          className="fixed inset-x-2 z-40 hidden sm:block lg:hidden"
          style={{ bottom: "calc(56px + env(safe-area-inset-bottom) + 8px)" }}
        >
          <CallNotePanel note={callNote} onDismiss={dismissNote} />
        </div>
      ) : null}

      {practiceOn && debriefOpen ? (
        <Debrief
          data={debriefData}
          loading={debriefLoading}
          error={debriefError}
          onAgain={practiseAgain}
          onClose={closeDebrief}
        />
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Gates and faults                                                    */
/* ------------------------------------------------------------------ */

interface SessionGateProps {
  kicker: string;
  line: string;
  body: string;
}

/** The screen for a call that cannot start: no session, or one the server dropped. */
function SessionGate({ kicker, line, body }: SessionGateProps) {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center bg-bg px-6 py-12">
      <div className="w-full max-w-[520px] border-l-2 border-warn bg-surface p-6">
        <p className="font-mono text-micro uppercase text-muted">{kicker}</p>
        <p className="mt-3 font-sans text-idle text-text">{line}</p>
        <p className="mt-2 font-sans text-body text-muted">{body}</p>
        <Link
          href="/"
          className="mt-5 inline-flex h-11 items-center gap-2 rounded-hair border border-line-strong px-4 font-mono text-[12px] font-semibold uppercase tracking-[0.12em] text-accent transition-colors duration-[120ms] hover:bg-surface-2"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
          START A NEW CALL
        </Link>
      </div>
    </main>
  );
}

/**
 * The fault report, in the rail rather than over the wave.
 *
 * There are no toasts in this product. A dead link is announced by the status
 * pill going OFFLINE and by the boresight going rose, both of which sit in the
 * rep's fixation cone, and the sentence explaining it is parked in the rail with
 * the rest of the record, where reading it costs a deliberate look away.
 *
 * FAULT rather than LINK PROBLEM because this one string carries socket errors,
 * server error frames and capture failures, and naming the wrong cause is worse
 * than naming none.
 */
function FaultLine({ message }: { message: string }) {
  return (
    <div role="status" className="shrink-0 border-b border-line bg-surface px-3 py-2.5">
      <p className="font-mono text-micro uppercase text-danger">PROBLEM</p>
      <p className="mt-1.5 font-sans text-body text-muted">{message}</p>
    </div>
  );
}

interface CallNotePanelProps {
  note: CallNote;
  onDismiss(): void;
}

/**
 * What just happened to the call, in one or two plain sentences.
 *
 * This is the only place the successful side of a call action is written down.
 * The launcher owns failures, because a failure is fixed inside the popover,
 * and this owns the receipt, because the receipt has to outlive the popover: in
 * WhatsApp link mode the rep is in another tab by the time it matters, and the
 * sentence they need on the way back is the one that says to share that tab's
 * sound.
 *
 * It is dismissed by hand rather than on a timer. A line that removes itself
 * while somebody is halfway through reading it is worse than one more click.
 */
function CallNotePanel({ note, onDismiss }: CallNotePanelProps) {
  return (
    <div role="status" className="border border-line-strong bg-surface-2 p-3 shadow-pop">
      <div className="flex items-start justify-between gap-2">
        <p className="font-mono text-micro uppercase text-muted">THE CALL</p>
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Hide this call message"
          className="-mr-1 -mt-1 flex h-5 w-5 items-center justify-center rounded-hair text-dim transition-colors duration-[120ms] hover:text-muted"
        >
          <X className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </div>
      <p className="mt-2 font-sans text-[12px] leading-[18px] text-muted">{note.text}</p>
      {note.openUrl ? (
        <a
          href={note.openUrl}
          target="_blank"
          rel="noreferrer"
          className="mt-2 inline-flex h-[26px] items-center gap-1.5 rounded-hair border border-line-strong px-2 font-mono text-micro uppercase text-accent transition-colors duration-[120ms] hover:bg-surface"
        >
          <ExternalLink className="h-3 w-3" aria-hidden="true" />
          Open WhatsApp
        </a>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Manual injection                                                    */
/* ------------------------------------------------------------------ */

interface ComposerProps {
  /** True on a practice call, where a line typed as YOU is answered by the robot client. */
  practice?: boolean;
  disabled: boolean;
  onSend(text: string, stream: StreamKind): void;
  onReset(): void;
  autoFocus?: boolean;
}

/**
 * Type a line and the copilot answers it. This is the whole app working with
 * zero audio hardware, so it is a first class control and not a debug hatch.
 */
function Composer({ disabled, onSend, onReset, autoFocus, practice = false }: ComposerProps) {
  const [value, setValue] = useState("");
  const [stream, setStream] = useState<StreamKind>("client");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const inputId = useId();

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus();
  }, [autoFocus]);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const text = value.trim();
    if (text.length === 0 || disabled) return;
    onSend(text, stream);
    setValue("");
  };

  const tab = (kind: StreamKind, label: string) => (
    <button
      key={kind}
      type="button"
      onClick={() => setStream(kind)}
      aria-pressed={stream === kind}
      className={[
        "h-[22px] px-2 font-mono text-micro uppercase transition-colors duration-[120ms]",
        stream === kind ? "bg-surface-2 text-text" : "bg-surface text-muted hover:text-text",
      ].join(" ")}
    >
      {label}
    </button>
  );

  return (
    <form onSubmit={submit} className="shrink-0 border-t border-line px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="font-mono text-micro uppercase text-muted">TYPE IT</span>
          <span className="flex gap-px bg-line-strong">
            {tab("client", "CLIENT")}
            {tab("rep", "YOU")}
          </span>
        </div>
        <button
          type="button"
          onClick={onReset}
          disabled={disabled}
          title="Clear the log and what the copilot remembers"
          className="font-mono text-micro uppercase text-muted transition-colors duration-[120ms] hover:text-text disabled:opacity-40"
        >
          RESET
        </button>
      </div>

      <div className="mt-2 flex gap-2">
        <label htmlFor={inputId} className="sr-only">
          Type what was said
        </label>
        <input
          id={inputId}
          ref={inputRef}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          disabled={disabled}
          placeholder={stream === "client" ? "Type what they said" : "Type what you said"}
          autoComplete="off"
          className="h-9 min-w-0 flex-1 rounded-hair border border-line-strong bg-surface-2 px-2.5 font-sans text-body text-text placeholder:text-dim disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={disabled || value.trim().length === 0}
          className="flex h-9 shrink-0 items-center gap-1.5 rounded-hair border border-line-strong px-3 font-mono text-[12px] font-semibold uppercase tracking-[0.12em] text-accent transition-colors duration-[120ms] hover:bg-surface-2 disabled:opacity-40"
        >
          <Send className="h-3.5 w-3.5" aria-hidden="true" />
          SEND
        </button>
      </div>

      <p className="mt-2 font-mono text-micro uppercase text-muted">
        {stream === "client"
          ? "ANSWERED LIKE THE CLIENT SAID IT"
          : practice
            ? "THE ROBOT CLIENT WILL ANSWER YOU"
            : "ADDS CONTEXT ONLY, NO SUGGESTION"}
      </p>
    </form>
  );
}
