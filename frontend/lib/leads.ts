/**
 * The leads layer: pick a search, read the leads, call one, say what happened.
 *
 * Seven browser side calls, all going through the one catch all proxy at
 * app/api/leads/[...path]/route.ts, so the browser never talks to FastAPI
 * directly and there is no CORS surface. The paths below are relative, so
 * nothing in this file may be called during server rendering.
 *
 * NOTHING IN THIS FILE THROWS. Every function answers with a discriminated
 * result. The rep is holding a phone, not a debugger, so every failure has to
 * come back as one plain sentence a component can print: the server is not
 * running, this search has no leads yet, the scrape is still going. A thrown
 * error would be a blank screen between two calls, and a blank screen in the
 * middle of a calling session is the one thing this file exists to prevent.
 *
 * Two more jobs live here, and both are on purpose:
 *
 *   1. Plain words. The lead engine wrote its findings with em dashes and with
 *      odd invisible spaces, because it was written for an email dashboard.
 *      `plainText` turns those into a comma and a normal space before anything
 *      reaches the screen. The backend does the same thing on its side. Two
 *      layers, because the rep reads these lines out loud and a dash is not a
 *      sound.
 *   2. Names. The scraper stores names the way Google Maps prints them, which is
 *      sometimes all lower case ("little space salon"). `displayName` fixes only
 *      the fully lower case ones, so a name the backend already fixed, or a name
 *      like "NYC Barber", is never damaged.
 *
 * See CONTRACT_LEADS.md sections 3.1 and 6.
 */

import type {
  LeadCallSession,
  LeadDetail,
  LeadMoney,
  LeadRow,
  LeadStatus,
  PainPoint,
  PreparedSession,
  ScrapeJob,
  SearchSummary,
} from "@/lib/types";
import {
  asLeadTrack,
  isLeadDetail,
  isLeadRow,
  isLeadStatus,
  isScrapeJob,
  isScrapeStarted,
  isSearchSummary,
} from "@/lib/types";

/**
 * The lead types, forwarded from lib/types.ts.
 *
 * They are declared there with the rest of the wire protocol, because that file
 * is the one trust boundary for everything the backend sends. They are forwarded
 * from here as well so a component can take a type and a fetcher from the same
 * import, which is how every leads component is written.
 *
 * `Lead` is `LeadRow` under a shorter name. The list component is itself called
 * LeadRow, and a component and a type with one name in one file reads badly, so
 * the row type answers to both.
 */
export type {
  LeadCallSession,
  LeadDetail,
  LeadMoney,
  LeadRow,
  LeadRow as Lead,
  LeadStatus,
  LeadTrack,
  OpeningHours,
  PainPoint,
  PainSeverity,
  ScrapeJob,
  ScrapeStarted,
  ScrapeState,
  SearchSummary,
  SearchSummary as LeadSearch,
} from "@/lib/types";

/* ------------------------------------------------------------------ */
/* Copy the rep reads                                                  */
/* ------------------------------------------------------------------ */

/**
 * The seven pipeline words, in words a person reads.
 *
 * "not_interested" is how the file on disk stores it. Nobody should ever see
 * that. Every screen that shows a status goes through this map.
 */
/**
 * The three lists a rep actually thinks in.
 *
 * Seven statuses is the truth on disk, but nobody planning their morning holds
 * seven buckets in their head. They hold: who is left to call, who I promised to
 * ring again, and who I am finished with. So the seven fold into three, and the
 * exact outcome stays visible as a small tag on the row for anyone who wants it.
 */
export type LeadBucket = "to_call" | "callback" | "done";

/** Which list a status belongs in. */
export function bucketOf(status: LeadStatus): LeadBucket {
  if (status === "new") return "to_call";
  if (status === "callback") return "callback";
  return "done";
}

/** The tab words, in the order the day goes. */
export const BUCKET_LABELS: Record<LeadBucket, string> = {
  to_call: "To call",
  callback: "Call back",
  done: "Done",
};

/** One line under each tab, so an empty list is never a mystery. */
export const BUCKET_EMPTY: Record<LeadBucket, string> = {
  to_call: "Every lead here has been dealt with. Start a new search for more.",
  callback: "Nobody to ring again. Leads land here when you pick Call back.",
  done: "Nothing finished yet. Leads land here once you say what happened.",
};

export const STATUS_LABELS: Record<LeadStatus, string> = {
  new: "Not called yet",
  interested: "Interested",
  callback: "Call back",
  not_interested: "Not interested",
  no_answer: "No answer",
  won: "Won",
  lost: "Lost",
};

/**
 * The order the status chips are drawn in.
 *
 * It follows the day, not the alphabet: what is left to do, then what came out
 * of a call, then how it finished.
 */
export const LEAD_STATUSES: readonly LeadStatus[] = [
  "new",
  "interested",
  "callback",
  "no_answer",
  "not_interested",
  "won",
  "lost",
];

/** How the list can be ordered. Frozen by the contract. */
export type LeadSort = "value" | "score" | "name";

/** The sort words, in plain English. */
export const SORT_LABELS: Record<LeadSort, string> = {
  value: "Biggest money first",
  score: "Best lead first",
  name: "By name",
};

/** What the rep would sell this business, in plain English. */
export const TRACK_LABELS: Record<"SEO" | "WEBSITE" | "OTHER", string> = {
  SEO: "Get found on Google",
  WEBSITE: "Build a website",
  OTHER: "Other work",
};

/* ------------------------------------------------------------------ */
/* Timeouts and the one command that fixes most failures               */
/* ------------------------------------------------------------------ */

/**
 * How long to wait for a read.
 *
 * Every one of these endpoints answers from files on the same machine, so it is
 * fast or it is broken. The number is 14 seconds and not 12 for one reason: the
 * proxy route gives up at 12 seconds and answers with its own sentence, and the
 * browser has to still be listening when that sentence lands. Two equal timers
 * would be a coin flip over which message the rep gets to read.
 */
const READ_TIMEOUT_MS = 14000;

/**
 * How long to wait for a call context to be built.
 *
 * This one is long because the backend fetches the lead's own website through
 * Jina inside the request, exactly as the setup page does. The proxy gives that
 * 40 seconds, so the browser waits 45, same ordering as above.
 */
const CALL_TIMEOUT_MS = 45000;

/** What to tell the rep to run when the server is not answering. */
const START_COMMAND = "cd backend && ./run.sh";

/** The hint shown under a "cannot reach the server" message. */
const START_HINT = `Start the server first: ${START_COMMAND}`;

/** Where every request in this file goes. */
const BASE = "/api/leads";

/* ------------------------------------------------------------------ */
/* Results                                                             */
/* ------------------------------------------------------------------ */

/** The failure half of every result in this file. */
export interface LeadsFailure {
  kind: "error";
  /** One full sentence, ready to print. Never empty. */
  message: string;
  /** A second line with something to do about it, or null. */
  hint: string | null;
}

/** What `fetchSearches` gives back. An empty list is a real answer. */
export type SearchesResult = { kind: "ok"; searches: SearchSummary[] } | LeadsFailure;

/**
 * What `fetchLeads` gives back.
 *
 * `dropped` counts rows the app could not read. It is almost always 0. When it
 * is not, the list still renders every good row, and the screen can say that a
 * few were skipped, which is honest and is better than a short list that looks
 * complete.
 */
export type LeadListResult =
  | { kind: "ok"; searchId: string; leads: LeadRow[]; dropped: number }
  | LeadsFailure;

/** What `fetchLead` gives back. */
export type LeadResult = { kind: "ok"; lead: LeadDetail } | LeadsFailure;

/** What `startLeadCall` gives back. The session opens the teleprompter. */
export type LeadCallResult = { kind: "ok"; session: LeadCallSession } | LeadsFailure;

/** What `setLeadStatus` gives back. */
export type LeadStatusResult =
  | { kind: "ok"; status: LeadStatus; updatedAt: string }
  | LeadsFailure;

/** What `startScrape` gives back. Both ids are needed: one to watch, one to open. */
export type ScrapeStartResult =
  | { kind: "ok"; searchId: string; jobId: string }
  | LeadsFailure;

/** What `fetchJob` gives back. */
export type JobResult = { kind: "ok"; job: ScrapeJob } | LeadsFailure;

/* ------------------------------------------------------------------ */
/* Request bodies                                                      */
/* ------------------------------------------------------------------ */

/** The filters the calling list can ask for. All of them are optional. */
export interface LeadFilters {
  /** One status, or "all" for no status filter at all. */
  status?: LeadStatus | "all";
  /** True keeps only leads with a phone number. A lead with no number is noise. */
  hasPhone?: boolean;
  /** Defaults to "value" on the server, so leave it out to get that. */
  sort?: LeadSort;
}

/** The body of POST /api/leads/{search_id}/{key}/call. */
export interface LeadCallBody {
  /** The rep's own services and offers. Required, the backend refuses an empty one. */
  knowledgeBase: string;
  /** Two letter language code. Defaults to "en". */
  language?: string;
}

/** The body of POST /api/leads/search, which starts a new scrape. */
export interface ScrapeBody {
  niche: string;
  location: string;
  /** How many businesses to look for. */
  limit: number;
  /** Two letter country code, for example "us" or "pk". */
  country: string;
}

/* ------------------------------------------------------------------ */
/* Plain words                                                         */
/* ------------------------------------------------------------------ */

/**
 * A dash used as punctuation, which becomes a comma.
 *
 * The lead engine writes lines like "The phone number is not tappable, this is
 * where most leads die." with a long dash where that comma is. The rep reads
 * these lines out loud, and a dash has no sound, so it becomes the comma it was
 * standing in for. A spaced short dash is the same thing and is treated the
 * same way.
 */
const DASH_AS_PUNCTUATION = /\s*[\u2014\u2015]\s*|\s+\u2013\s+/g;

/** A short dash inside a range, like an opening hours span. That becomes a hyphen. */
const DASH_AS_RANGE = /\u2013/g;

/** Invisible spaces Google Maps uses inside hours. They become normal spaces. */
const ODD_SPACES = /[\u00a0\u202f\u2009\u2007]/g;

/**
 * One string, in words the rep can read out loud.
 *
 * Safe to run twice, and safe on a string that has none of these characters, so
 * it can sit in the parse path without a check.
 */
export function plainText(value: string): string {
  return value
    .replace(ODD_SPACES, " ")
    .replace(DASH_AS_PUNCTUATION, ", ")
    .replace(DASH_AS_RANGE, "-")
    .replace(/^[\s,]+/, "")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * A business name with capital letters, when it clearly needs them.
 *
 * The scraper stores what Google Maps printed, and Maps prints some names all in
 * lower case. Only a name with no capital letter anywhere is touched, so a name
 * the backend already fixed, and a name like "NYC Barber" or "L'Oreal", is left
 * exactly as it is.
 */
export function displayName(name: string): string {
  const clean = plainText(name);
  if (clean.length === 0) return clean;
  if (clean !== clean.toLowerCase()) return clean;
  return clean.replace(/(^|[\s(&/-])([a-z])/g, (_match, before: string, letter: string) => {
    return before + letter.toUpperCase();
  });
}

/* ------------------------------------------------------------------ */
/* Money and phones                                                    */
/* ------------------------------------------------------------------ */

/**
 * An amount of money with its currency symbol in front of it.
 *
 * `formatMoney("$", 850)` gives "$850", and `formatMoney("$", 12400)` gives
 * "$12,400". A symbol made of letters, like "Rs", gets a space after it, because
 * "Rs3,000" is hard to read. Cents are dropped: these are estimates, and a price
 * with cents on it looks like a bill.
 *
 * A missing or broken number becomes zero rather than "NaN". A price the rep may
 * read out loud must never come out as a word.
 */
export function formatMoney(currency: string, value: number): string {
  const amount = Number.isFinite(value) ? Math.round(value) : 0;
  const digits = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(amount);
  const symbol = currency.trim();
  if (symbol.length === 0) return digits;
  return /[A-Za-z]$/.test(symbol) ? `${symbol} ${digits}` : `${symbol}${digits}`;
}

/**
 * The same thing, with the deal value first.
 *
 * Both orders exist because both read naturally at the call site: a row shows a
 * currency and a number, a drawer shows a deal value in a currency. TypeScript
 * catches the wrong order at build time, since one argument is a string and the
 * other is a number, so there is no way to mix them up quietly.
 */
export function formatDealValue(value: number, currency: string): string {
  return formatMoney(currency, value);
}

/**
 * True when this lead can actually be called.
 *
 * The scraper stores an empty string for a business with no listed number, and 7
 * of the 91 leads on disk are like that. Those rows are drawn dimmed with "No
 * number" where the Call button would be, so this is the one test for it.
 */
export function canCall(lead: LeadRow): boolean {
  return typeof lead.phone === "string" && lead.phone.trim().length > 0;
}

/* ------------------------------------------------------------------ */
/* Small helpers                                                       */
/* ------------------------------------------------------------------ */

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isStr(v: unknown): v is string {
  return typeof v === "string";
}

function asString(v: unknown, fallback: string): string {
  return typeof v === "string" ? v : fallback;
}

function asNullableString(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

function asNumber(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

/**
 * A deadline for one fetch, plus the caller's own cancel signal.
 *
 * `AbortSignal.timeout` alone would cover the deadline, and the caller's signal
 * alone would cover a component unmounting, but a drawer that is closed mid
 * request needs both. Merging them by hand also remembers which of the two
 * fired, so the failure can be described honestly instead of always saying it
 * timed out.
 *
 * This is the same shape lib/calling.ts uses. It is written out again rather
 * than shared, because that file is finished and working and is not mine to
 * reopen.
 */
interface Deadline {
  readonly signal: AbortSignal;
  /** True when our own timer fired, false when the caller cancelled. */
  expired(): boolean;
  /** Drop the timer and the listener. Always call this, success or failure. */
  done(): void;
}

function deadline(ms: number, outer?: AbortSignal): Deadline {
  const controller = new AbortController();
  let expired = false;

  const timer: ReturnType<typeof setTimeout> = setTimeout(() => {
    expired = true;
    controller.abort();
  }, ms);

  const forward = () => controller.abort();
  if (outer) {
    if (outer.aborted) controller.abort();
    else outer.addEventListener("abort", forward, { once: true });
  }

  return {
    signal: controller.signal,
    expired: () => expired,
    done: () => {
      clearTimeout(timer);
      if (outer) outer.removeEventListener("abort", forward);
    },
  };
}

/**
 * One plain sentence for a fetch that never landed.
 *
 * The browser's own words are thrown away on purpose. A failed fetch to our own
 * origin says "Failed to fetch" in Chrome and something else in Firefox, and
 * neither tells a rep anything they can do about it.
 */
function describeNetwork(err: unknown, mark: Deadline, seconds: number): string {
  if (mark.expired()) return `The server did not answer in ${seconds} seconds.`;
  if (err instanceof Error && err.name === "AbortError") return "The request was stopped.";
  return "The app could not reach the server.";
}

/** Read a JSON body, or null when the body is empty or is not JSON. */
async function readJson(res: Response): Promise<unknown> {
  let text: string;
  try {
    text = await res.text();
  } catch {
    return null;
  }
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

/**
 * Turn a failed response into a message and a hint.
 *
 * The proxy answers with `{ error, detail, hint }` and FastAPI answers with its
 * own `{ detail }`, so both shapes are read, and anything else falls back to the
 * status code. The message is always a full sentence, so callers print it with
 * no work.
 */
function describeFailure(res: Response, payload: unknown): { message: string; hint: string | null } {
  if (isRecord(payload)) {
    const hint = isStr(payload.hint) && payload.hint ? payload.hint : null;
    if (isStr(payload.detail) && payload.detail) return { message: plainText(payload.detail), hint };
    if (isStr(payload.message) && payload.message) return { message: plainText(payload.message), hint };
    if (isStr(payload.error) && payload.error) return { message: `${plainText(payload.error)}.`, hint };
  }

  if (res.status === 404) {
    return {
      message: "The server does not have this one. The search may have been run again since.",
      hint: null,
    };
  }
  return { message: `The server answered ${res.status}.`, hint: null };
}

/** The failure result for a fetch that never landed. */
function networkFailure(err: unknown, mark: Deadline, ms: number): LeadsFailure {
  return {
    kind: "error",
    message: describeNetwork(err, mark, Math.round(ms / 1000)),
    hint: START_HINT,
  };
}

/** The failure result for an answer the app could not read. */
function unreadable(what: string): LeadsFailure {
  return {
    kind: "error",
    message: `The server sent ${what} the app could not read.`,
    hint: `Check the server log in the terminal running ${START_COMMAND}`,
  };
}

/**
 * One GET or POST through the proxy, with every failure already a sentence.
 *
 * Returns the parsed body on a 2xx, or a ready made failure. Nothing here
 * throws.
 */
async function request(
  path: string,
  init: { method: "GET" | "POST"; body?: string },
  ms: number,
  signal?: AbortSignal,
): Promise<{ kind: "ok"; payload: unknown } | LeadsFailure> {
  const mark = deadline(ms, signal);

  let res: Response;
  try {
    res = await fetch(path, {
      method: init.method,
      headers:
        init.body === undefined
          ? { accept: "application/json" }
          : { "content-type": "application/json", accept: "application/json" },
      body: init.body,
      cache: "no-store",
      signal: mark.signal,
    });
  } catch (err) {
    return networkFailure(err, mark, ms);
  } finally {
    mark.done();
  }

  const payload = await readJson(res);

  if (!res.ok) {
    const failure = describeFailure(res, payload);
    return {
      kind: "error",
      message: failure.message,
      hint: failure.hint ?? (res.status >= 500 ? START_HINT : null),
    };
  }

  return { kind: "ok", payload };
}

/** Build the path for one lead, with the key safe to put in a URL. */
function leadPath(searchId: string, key: string, tail: string = ""): string {
  const id = encodeURIComponent(searchId.trim());
  const k = encodeURIComponent(key.trim());
  return `${BASE}/${id}/${k}${tail}`;
}

/* ------------------------------------------------------------------ */
/* Parsing                                                             */
/* ------------------------------------------------------------------ */

/**
 * Fix the three fields whose shape is not settled, then hand the record on.
 *
 * Only three, and each for a stated reason:
 *
 *   - `track`. The lead engine writes it in capitals and the contract writes it
 *     in title case, so it is folded to one of the three words this build knows.
 *   - `rating` and `reviews`. The scraper stores null for a business Google has
 *     no stars for yet, which is 2 of the 91 leads on disk. Null becomes 0, so
 *     the lead survives the guard and no component has to hold a null. A real
 *     rating is never 0, so nothing is lost by saying it this way.
 *
 * Everything else is left exactly as it arrived, so the guard is still judging
 * the server's real answer.
 */
function normalizeWire(v: unknown): unknown {
  if (!isRecord(v)) return v;
  return {
    ...v,
    track: asLeadTrack(v.track),
    rating: asNumber(v.rating, 0),
    reviews: asNumber(v.reviews, 0),
  };
}

/**
 * A price block that says nothing was worked out.
 *
 * A search that has been scraped but not scored yet has no prices at all. That
 * is a normal state, and it is the state the rep is in for the minutes a scrape
 * is still running. The server answers it with exactly this block, a zero in
 * every number and an empty string in every word, and the drawer leaves out
 * every money line whose number is zero, so the rep sees no price rather than a
 * price nobody worked out.
 *
 * It is written out here as well for the case below.
 */
const NO_PRICE_YET: LeadMoney = {
  tierLabel: "",
  dealValueUsd: 0,
  retainerMonthlyUsd: 0,
  contractValueUsd: 0,
  currencySymbol: "$",
  closeProbability: 0,
  expectedValueUsd: 0,
};

/**
 * The same fold as `normalizeWire`, plus the one field only a whole lead has.
 *
 * `isLeadDetail` checks `money` field by field, so a lead that arrives with no
 * money block at all fails the check, and `fetchLead` then tells the rep the
 * lead could not be read. For a search with no scores that would mean the list
 * draws every row fine and no row ever opens, forever, which is the worst way to
 * fail: it looks like the app is broken rather than like the scoring has not run.
 *
 * So a missing block, or a null one, becomes the empty block above. This is the
 * second layer, not the first. The server already sends that same block, so this
 * only fires against an older server or a shape nobody planned for.
 *
 * A money block that is present but broken is left exactly as it arrived, so the
 * guard still catches it. A wrong price is worth refusing over, because the rep
 * says it out loud. A missing one is not.
 */
function normalizeDetailWire(v: unknown): unknown {
  const folded = normalizeWire(v);
  if (!isRecord(folded)) return folded;
  if (folded.money === null || folded.money === undefined) {
    return { ...folded, money: { ...NO_PRICE_YET } };
  }
  return folded;
}

/** A row with every string the rep will read turned into plain words. */
function plainRow<T extends LeadRow>(row: T): T {
  return {
    ...row,
    name: displayName(row.name),
    category: plainText(row.category),
    city: plainText(row.city),
    why: plainText(row.why),
  };
}

function plainPain(p: PainPoint): PainPoint {
  return {
    title: plainText(p.title),
    detail: plainText(p.detail),
    proof: plainText(p.proof),
    severity: p.severity,
  };
}

function plainMoney(m: LeadMoney): LeadMoney {
  return { ...m, tierLabel: plainText(m.tierLabel) };
}

/** A whole lead with every string turned into plain words. */
function plainDetail(lead: LeadDetail): LeadDetail {
  return {
    ...plainRow(lead),
    address: plainText(lead.address),
    notes: plainText(lead.notes),
    hours: lead.hours.map((h) => ({ day: plainText(h.day), hours: plainText(h.hours) })),
    painPoints: lead.painPoints.map(plainPain),
    talkingPoints: lead.talkingPoints.map(plainText),
    money: plainMoney(lead.money),
  };
}

/**
 * Read the prepared session out of the call answer.
 *
 * The session id is the only field worth refusing over: without it there is no
 * teleprompter to open. Everything else is filled in the way lib/session.ts
 * fills it when reading storage back, so the call page gets a real session and
 * never an object full of undefined.
 */
function readSession(payload: Record<string, unknown>, language: string): PreparedSession | null {
  const sessionId = isStr(payload.sessionId) ? payload.sessionId.trim() : "";
  if (sessionId.length === 0) return null;

  return {
    sessionId,
    systemPrompt: asString(payload.systemPrompt, ""),
    clientUrl: asNullableString(payload.clientUrl),
    clientTitle: asNullableString(payload.clientTitle),
    clientExcerpt: asNullableString(payload.clientExcerpt),
    scrapeChars: asNumber(payload.scrapeChars, 0),
    scrapeOk: payload.scrapeOk === true,
    scrapeError: asNullableString(payload.scrapeError),
    createdAt: asNumber(payload.createdAt, Date.now() / 1000),
    language,
    /* The audit is written into the context on the server, so the copilot knows
       this business even when the site scrape failed or there was no site at
       all. That is exactly what this flag means. */
    hasNotes: true,
  };
}

/* ------------------------------------------------------------------ */
/* The seven calls                                                     */
/* ------------------------------------------------------------------ */

/**
 * Read the list of searches, newest first.
 *
 * GET /api/leads/searches. An empty list is not an error, it means the rep has
 * not run a search yet, and the screen shows the empty state with the New search
 * button in it.
 *
 * @param signal Optional cancel signal, for an effect cleanup.
 * @returns Always a result, never a thrown error.
 */
export async function fetchSearches(signal?: AbortSignal): Promise<SearchesResult> {
  const answer = await request(`${BASE}/searches`, { method: "GET" }, READ_TIMEOUT_MS, signal);
  if (answer.kind === "error") return answer;

  const raw: unknown = isRecord(answer.payload) ? answer.payload.searches : null;
  if (!Array.isArray(raw)) return unreadable("a search list");

  const searches = (raw as unknown[]).filter(isSearchSummary).map((s) => ({
    ...s,
    niche: plainText(s.niche),
    location: plainText(s.location),
  }));

  return { kind: "ok", searches };
}

/**
 * Read one search's calling list.
 *
 * GET /api/leads/{search_id}. The filters go on the query string exactly as the
 * contract names them, and a filter that is not set is left off entirely, so the
 * server keeps its own defaults.
 *
 * A row the app cannot read is dropped, not thrown, and counted in `dropped`. A
 * single bad record must never empty a calling list.
 *
 * The second argument takes either the whole filter object or just a sort word,
 * because the leads page only ever asks the server to reorder: the chips are
 * applied in the browser over rows it already has, so a chip press is instant.
 *
 * @param searchId The search slug, for example "barber-hoboken".
 * @param filters Status, has phone, and sort, or just the sort word. Optional.
 * @param signal Optional cancel signal.
 * @returns Always a result, never a thrown error.
 */
export async function fetchLeads(
  searchId: string,
  filters: LeadFilters | LeadSort = {},
  signal?: AbortSignal,
): Promise<LeadListResult> {
  const id = searchId.trim();
  if (id.length === 0) {
    return { kind: "error", message: "No search is picked yet.", hint: null };
  }

  const asked: LeadFilters = typeof filters === "string" ? { sort: filters } : filters;

  const query = new URLSearchParams();
  if (asked.status && asked.status !== "all") query.set("status", asked.status);
  if (asked.hasPhone) query.set("has_phone", "1");
  if (asked.sort) query.set("sort", asked.sort);

  const tail = query.toString();
  const path = `${BASE}/${encodeURIComponent(id)}${tail ? `?${tail}` : ""}`;

  const answer = await request(path, { method: "GET" }, READ_TIMEOUT_MS, signal);
  if (answer.kind === "error") return answer;

  const body = answer.payload;
  const raw: unknown = isRecord(body) ? body.leads : null;
  if (!Array.isArray(raw)) return unreadable("a lead list");

  const rows = raw as unknown[];
  const leads: LeadRow[] = [];
  for (const row of rows) {
    const candidate = normalizeWire(row);
    if (isLeadRow(candidate)) leads.push(plainRow(candidate));
  }

  return {
    kind: "ok",
    searchId: isRecord(body) && isStr(body.searchId) ? body.searchId : id,
    leads,
    dropped: rows.length - leads.length,
  };
}

/**
 * Read one whole lead, for the drawer.
 *
 * GET /api/leads/{search_id}/{key}. The key holds a colon, so it is escaped
 * before it goes in the path and the proxy puts it back together on the way to
 * FastAPI.
 *
 * @param searchId The search slug.
 * @param key The lead key, the lead engine's own feature id.
 * @param signal Optional cancel signal.
 * @returns Always a result, never a thrown error.
 */
export async function fetchLead(
  searchId: string,
  key: string,
  signal?: AbortSignal,
): Promise<LeadResult> {
  if (searchId.trim().length === 0 || key.trim().length === 0) {
    return { kind: "error", message: "No lead is picked yet.", hint: null };
  }

  const answer = await request(leadPath(searchId, key), { method: "GET" }, READ_TIMEOUT_MS, signal);
  if (answer.kind === "error") return answer;

  const candidate = normalizeDetailWire(answer.payload);
  if (!isLeadDetail(candidate)) return unreadable("a lead");

  return { kind: "ok", lead: plainDetail(candidate) };
}

/**
 * Build the call context for one lead and open a session for it.
 *
 * POST /api/leads/{search_id}/{key}/call. This is the point of the whole merge:
 * the audit already knows this business, so the rep types nothing. The answer is
 * an ordinary prepared session, so the teleprompter, the objection buttons and
 * the calling providers all work on it with no special case.
 *
 * The knowledge base is checked here before the request goes out, because the
 * backend refuses an empty one with a 422 whose wording is written for a
 * programmer, and the rep would have to read it.
 *
 * @param searchId The search slug.
 * @param key The lead key.
 * @param body The rep's own services, and the language for the call.
 * @param signal Optional cancel signal.
 * @returns Always a result, never a thrown error.
 */
export async function startLeadCall(
  searchId: string,
  key: string,
  body: LeadCallBody,
  signal?: AbortSignal,
): Promise<LeadCallResult> {
  if (searchId.trim().length === 0 || key.trim().length === 0) {
    return { kind: "error", message: "No lead is picked yet.", hint: null };
  }

  const knowledgeBase = body.knowledgeBase.trim();
  if (knowledgeBase.length === 0) {
    return {
      kind: "error",
      message: "Your services and offers are empty, so there is nothing to sell with.",
      hint: "Fill in your knowledge base on the setup page first.",
    };
  }

  const language = (body.language ?? "en").trim() || "en";

  const answer = await request(
    leadPath(searchId, key, "/call"),
    { method: "POST", body: JSON.stringify({ knowledgeBase, language }) },
    CALL_TIMEOUT_MS,
    signal,
  );
  if (answer.kind === "error") return answer;

  if (!isRecord(answer.payload)) return unreadable("a call context");

  const session = readSession(answer.payload, language);
  if (session === null) {
    return {
      kind: "error",
      message: "The server answered, but it did not send a call id.",
      hint: `Check the server log in the terminal running ${START_COMMAND}`,
    };
  }

  const leadName = isStr(answer.payload.leadName) ? displayName(answer.payload.leadName) : "";
  const leadKey = isStr(answer.payload.leadKey) && answer.payload.leadKey.trim().length > 0
    ? answer.payload.leadKey
    : key.trim();

  return { kind: "ok", session: { ...session, leadKey, leadName } };
}

/**
 * Write what happened on the call.
 *
 * POST /api/leads/{search_id}/{key}/status. This lands in the same
 * `pipeline.json` the rep's own lead engine dashboard reads, so the two views
 * never disagree about who has been called.
 *
 * @param searchId The search slug.
 * @param key The lead key.
 * @param status One of the seven words.
 * @param notes What the rep typed, or nothing.
 * @param signal Optional cancel signal.
 * @returns Always a result, never a thrown error.
 */
export async function setLeadStatus(
  searchId: string,
  key: string,
  status: LeadStatus,
  notes: string = "",
  signal?: AbortSignal,
): Promise<LeadStatusResult> {
  if (searchId.trim().length === 0 || key.trim().length === 0) {
    return { kind: "error", message: "No lead is picked yet.", hint: null };
  }

  const answer = await request(
    leadPath(searchId, key, "/status"),
    { method: "POST", body: JSON.stringify({ status, notes: notes.trim() }) },
    READ_TIMEOUT_MS,
    signal,
  );
  if (answer.kind === "error") return answer;

  const body = answer.payload;
  if (!isRecord(body)) return unreadable("an answer");

  /* The server echoes the word it saved. If it sent something else back, the
     word we asked for is still the truth of what was written, so it is kept
     rather than failing a save that worked. */
  const saved = isLeadStatus(body.status) ? body.status : status;
  const updatedAt = isStr(body.updatedAt) ? body.updatedAt : new Date().toISOString();

  return { kind: "ok", status: saved, updatedAt };
}

/**
 * Start a new scrape.
 *
 * POST /api/leads/search. This opens a real Chromium window on the server
 * machine and drives Google Maps for minutes, so it answers at once with two
 * ids: the search it will save, and the job to watch with `fetchJob`.
 *
 * @param body Niche, location, how many, and the country code.
 * @param signal Optional cancel signal.
 * @returns Always a result, never a thrown error.
 */
export async function startScrape(
  body: ScrapeBody,
  signal?: AbortSignal,
): Promise<ScrapeStartResult> {
  const niche = body.niche.trim();
  const location = body.location.trim();
  if (niche.length === 0 || location.length === 0) {
    return {
      kind: "error",
      message: "Write what you are looking for and where.",
      hint: null,
    };
  }

  const limit = Number.isFinite(body.limit) ? Math.max(1, Math.round(body.limit)) : 40;
  const country = body.country.trim().toLowerCase() || "us";

  const answer = await request(
    `${BASE}/search`,
    { method: "POST", body: JSON.stringify({ niche, location, limit, country }) },
    READ_TIMEOUT_MS,
    signal,
  );
  if (answer.kind === "error") return answer;

  if (!isScrapeStarted(answer.payload)) return unreadable("an answer");
  if (!answer.payload.ok) {
    return {
      kind: "error",
      message: "The server did not start the search.",
      hint: `Check the server log in the terminal running ${START_COMMAND}`,
    };
  }

  return { kind: "ok", searchId: answer.payload.searchId, jobId: answer.payload.jobId };
}

/**
 * Ask how a scrape is going.
 *
 * GET /api/leads/jobs/{job_id}. Poll this while a search runs and show
 * `job.line`, which is the last line the job printed. A Chromium window
 * scrolling Maps for four minutes behind a spinner looks broken. The real line
 * looks like work.
 *
 * @param jobId The id `startScrape` handed back.
 * @param signal Optional cancel signal.
 * @returns Always a result, never a thrown error.
 */
export async function fetchJob(jobId: string, signal?: AbortSignal): Promise<JobResult> {
  const id = jobId.trim();
  if (id.length === 0) {
    return { kind: "error", message: "There is no search running.", hint: null };
  }

  const answer = await request(
    `${BASE}/jobs/${encodeURIComponent(id)}`,
    { method: "GET" },
    READ_TIMEOUT_MS,
    signal,
  );
  if (answer.kind === "error") return answer;

  if (!isScrapeJob(answer.payload)) return unreadable("a job report");

  const job = answer.payload;
  return {
    kind: "ok",
    job: { ...job, step: plainText(job.step), line: plainText(job.line) },
  };
}

/**
 * The same call under the name the leads page uses.
 *
 * "Start a search" is what the button says, and "scrape" is what the lead engine
 * calls the job it runs, so both names are true and both are in use. One
 * function, two names, so neither side has to be renamed for the other.
 */
export const startSearch = startScrape;

/**
 * Take the good half of a result, or throw the sentence.
 *
 * Every fetcher above answers rather than throwing, which is what the leads
 * screen wants, because a failure there is a line of text on the page and not a
 * crash. This is for the caller that would rather write a try and a catch: it
 * hands back the ok result and throws an Error whose message is the same plain
 * sentence, so nothing is lost either way.
 *
 * Use it like this:
 *
 *   const list = orThrow(await fetchSearches(signal)).searches;
 *
 * Prefer checking `result.kind` where you can. This exists so a component built
 * around try and catch does not have to be turned inside out to use this file.
 */
export function orThrow<T extends { kind: "ok" }>(result: T | LeadsFailure): T {
  if (result.kind === "error") {
    throw new Error(result.hint ? `${result.message} ${result.hint}` : result.message);
  }
  return result;
}
