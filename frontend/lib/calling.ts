/**
 * The calling layer: which providers exist, start a call, hang it up.
 *
 * Three browser side calls, one per endpoint, all going through the Next.js
 * proxy routes in app/api/call/* so the browser never talks to FastAPI directly.
 * The relative paths only resolve in the browser, so nothing here may be called
 * during server rendering.
 *
 * NOTHING IN THIS FILE THROWS. Every function answers with a discriminated
 * result, because every one of these calls can fail in a way the rep has to be
 * told about in one plain sentence: the server is not running, the number is
 * wrong, Twilio is not configured, the trial account has not verified that
 * number. A thrown error at the top of a popover would be a blank panel, and a
 * blank panel during a live call is worse than a sentence.
 *
 * The honest part, which the UI must keep:
 *   - A provider that cannot place a call comes back with `ready: false`, its
 *     cost, and the exact environment variable names it still needs.
 *   - Twilio costs real money per minute. That line is on the card before the
 *     button is pressed, never after.
 *   - WhatsApp from the app needs a Meta business number and Meta approval. The
 *     link option is the one that always works, and it is free.
 *
 * See CONTRACT_CALLING.md sections 2.1 and 5.
 */

import type { CallProvider, CallProviderInfo, CallStartResult } from "@/lib/types";
import { isCallProvider } from "@/lib/types";

/* ------------------------------------------------------------------ */
/* Timeouts and copy                                                   */
/* ------------------------------------------------------------------ */

/**
 * How long to wait for the provider list.
 *
 * The read itself is a pure look at the server environment, so it is fast or it
 * is broken. The number is 10 seconds and not 8 for one reason only: the proxy
 * route gives up at 8 seconds and answers with its own sentence, and the browser
 * has to still be listening when that sentence lands. Two equal timers would be
 * a coin flip over which message the rep gets to read.
 */
const PROVIDERS_TIMEOUT_MS = 10000;

/**
 * How long to wait for a call to start.
 *
 * This one is generous on purpose. The Twilio path makes a real REST call to a
 * carrier from inside the request, and the proxy route gives that 30 seconds, so
 * the browser has to wait a little longer than the proxy or it would give up
 * first and tell the rep the call failed while the phone was already ringing.
 */
const START_TIMEOUT_MS = 32000;

/**
 * How long to wait for a hang up.
 *
 * The proxy route gives the backend 15 seconds, so this one waits 17. Same
 * ordering as the start call above, and for a sharper reason. If the browser
 * gave up first, a rep ending a call that is costing money every minute would
 * be told to start a server that is already running, instead of the true
 * sentence the proxy is holding: the call may still be up, so end it on the
 * phone. The rep is waiting on this button, but wrong advice is worse than two
 * more seconds.
 */
const HANGUP_TIMEOUT_MS = 17000;

/** What to tell the rep to run when the server is not answering. */
const START_COMMAND = "cd backend && ./run.sh";

/** The hint shown under a "cannot reach the server" message. */
const START_HINT = `Start the server first: ${START_COMMAND}`;

/* ------------------------------------------------------------------ */
/* The offline fallback                                                */
/* ------------------------------------------------------------------ */

/**
 * The four providers, all switched off, for when the server does not answer.
 *
 * Without this the picker would render an empty box, which reads as "this app
 * has no calling" instead of "the server is down". So the rep still sees the
 * four options and what each one costs, and every one of them is off, because
 * with the server down not one of them can start a call. Even the manual option
 * is off: the rep can of course dial by hand at any time, but the app cannot
 * record that a call started, so offering it here would be a button that does
 * nothing.
 *
 * NEVER render this list on its own. It is handed back inside the `error`
 * result, and the message that comes with it is what says why everything is
 * off. The list alone would be a lie.
 *
 * Every `missing` array below is empty, and it has to stay empty. These rows are
 * off because the server did not answer, not because a key is absent from
 * backend/.env, and a row naming TWILIO_ACCOUNT_SID here would send the rep off
 * to edit a file for twenty minutes when the real fix is the one command printed
 * in the message beside this list.
 */
export const PROVIDER_FALLBACK: readonly CallProviderInfo[] = [
  {
    key: "manual",
    label: "I will dial myself",
    blurb: "You call from your own phone or WhatsApp. The app just listens.",
    cost: "Free",
    ready: false,
    missing: [],
  },
  {
    key: "whatsapp_link",
    label: "WhatsApp, from my phone",
    blurb: "We open WhatsApp with the number ready. You press call.",
    cost: "Free",
    ready: false,
    missing: [],
  },
  {
    key: "whatsapp_cloud",
    label: "WhatsApp, from the app",
    blurb: "The app places the WhatsApp call. Needs a Meta business number.",
    cost: "Meta business rates",
    ready: false,
    missing: [],
  },
  {
    key: "twilio",
    label: "Phone call, from the app",
    blurb: "We ring your phone first, then join the client. The app hears both sides.",
    cost: "About 1 to 3 US cents a minute, plus the number",
    ready: false,
    missing: [],
  },
];

/* ------------------------------------------------------------------ */
/* Results                                                             */
/* ------------------------------------------------------------------ */

/**
 * What `fetchProviders` gives back.
 *
 * The error case still carries a list, `PROVIDER_FALLBACK`, so the picker has
 * something to draw either way and the caller never has to branch before
 * rendering. It has to draw `message` as well.
 */
export type ProvidersResult =
  | { kind: "ok"; providers: readonly CallProviderInfo[] }
  | {
      kind: "error";
      message: string;
      hint: string | null;
      providers: readonly CallProviderInfo[];
    };

/** The body of POST /api/call/start. */
export interface CallStartBody {
  sessionId: string;
  provider: CallProvider;
  /** The client's number, in full, with the country code. Not used by manual. */
  toNumber?: string | null;
  /** The rep's own phone, which Twilio rings first. Twilio only. */
  repNumber?: string | null;
}

/**
 * What `startCall` gives back.
 *
 * Three cases, not two, because "the server said no" and "the server did not
 * answer" need different words. A refusal is a real answer from the backend,
 * with a message written for the rep, for example "Add TWILIO_ACCOUNT_SID to
 * backend/.env" or "Write the number with the country code, like +923001234567."
 * An error means the request never landed, so there is nothing to quote.
 */
export type StartResult =
  | { kind: "started"; call: CallStartResult }
  | { kind: "refused"; call: CallStartResult }
  | { kind: "error"; message: string; hint: string | null };

/** What `hangUp` gives back. There is nothing to read on success. */
export type HangUpResult =
  | { kind: "ok" }
  | { kind: "error"; message: string; hint: string | null };

/* ------------------------------------------------------------------ */
/* Small helpers                                                       */
/* ------------------------------------------------------------------ */

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isStr(v: unknown): v is string {
  return typeof v === "string";
}

function isStrOrNull(v: unknown): v is string | null {
  return v === null || typeof v === "string";
}

/**
 * One provider row, checked field by field.
 *
 * A row with an unknown key is rejected rather than kept, because the picker
 * turns the key straight into a start request. A key we do not know would be a
 * button that the backend then refuses, which is the one thing this whole file
 * is built to avoid.
 */
function isCallProviderInfo(v: unknown): v is CallProviderInfo {
  return (
    isRecord(v) &&
    isCallProvider(v.key) &&
    isStr(v.label) &&
    isStr(v.blurb) &&
    isStr(v.cost) &&
    typeof v.ready === "boolean" &&
    Array.isArray(v.missing) &&
    v.missing.every(isStr)
  );
}

/** The start response, on the 200 and on the 400 alike. */
function isCallStartResult(v: unknown): v is CallStartResult {
  return (
    isRecord(v) &&
    typeof v.ok === "boolean" &&
    isCallProvider(v.provider) &&
    isStrOrNull(v.callId) &&
    isStrOrNull(v.openUrl) &&
    isStr(v.message)
  );
}

/**
 * A deadline for one fetch, plus the caller's own cancel signal.
 *
 * `AbortSignal.timeout` alone would cover the deadline, and the caller's signal
 * alone would cover an unmount, but a popover that is closed mid request needs
 * both. Merging them by hand keeps this working in every browser that can run
 * the app, and it also remembers which of the two fired, so the failure can be
 * described honestly instead of always saying "timed out".
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
 * origin says "Failed to fetch" in Chrome and "NetworkError when attempting to
 * fetch resource" in Firefox, and neither tells a rep anything they can do. The
 * three cases below cover every real reason: our timer ran out, the caller
 * cancelled, or the page cannot reach the app it was served from.
 */
function describeNetwork(err: unknown, mark: Deadline, seconds: number): string {
  if (mark.expired()) {
    return `The server did not answer in ${seconds} seconds.`;
  }
  if (err instanceof Error && err.name === "AbortError") {
    return "The request was stopped.";
  }
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
 * Turn a failed proxy response into a message and a hint.
 *
 * The proxy routes answer with `{ error, detail, hint }`, and the backend
 * answers with its own error bodies, so both shapes are read and anything else
 * falls back to the status code. The message is always a full sentence, so the
 * caller can print it with no work.
 */
function describeFailure(res: Response, payload: unknown): { message: string; hint: string | null } {
  if (isRecord(payload)) {
    // The proxy's own "Backend unreachable" is the one failure with a fix the
    // rep can act on, so its hint is carried through word for word.
    const hint = isStr(payload.hint) && payload.hint ? payload.hint : null;

    // `detail` is both the proxy's sentence and FastAPI's own error field, so it
    // is read first. `message` is the backend's call shape. `error` is the
    // proxy's short label, which is a fragment, so it gets a full stop.
    if (isStr(payload.detail) && payload.detail) return { message: payload.detail, hint };
    if (isStr(payload.message) && payload.message) return { message: payload.message, hint };
    if (isStr(payload.error) && payload.error) return { message: `${payload.error}.`, hint };
  }

  if (res.status === 404) {
    return {
      message: "The server does not know this call. Build the call context again.",
      hint: null,
    };
  }
  return { message: `The server answered ${res.status}.`, hint: null };
}

/* ------------------------------------------------------------------ */
/* The three calls                                                     */
/* ------------------------------------------------------------------ */

/**
 * Read the provider list.
 *
 * GET /api/call/providers. The list is computed from the server environment on
 * every request, so it is never cached: the rep can add TWILIO_ACCOUNT_SID to
 * backend/.env, restart, reopen the picker, and see the option turn on.
 *
 * Rows the browser does not understand are dropped. If that leaves nothing, the
 * answer is an error carrying `PROVIDER_FALLBACK`, because an empty picker
 * would say the wrong thing.
 *
 * @param signal Optional cancel signal, for an effect cleanup.
 * @returns Always a result, never a thrown error.
 */
export async function fetchProviders(signal?: AbortSignal): Promise<ProvidersResult> {
  const mark = deadline(PROVIDERS_TIMEOUT_MS, signal);

  let res: Response;
  try {
    res = await fetch("/api/call/providers", {
      method: "GET",
      headers: { accept: "application/json" },
      cache: "no-store",
      signal: mark.signal,
    });
  } catch (err) {
    return {
      kind: "error",
      message: describeNetwork(err, mark, PROVIDERS_TIMEOUT_MS / 1000),
      hint: START_HINT,
      providers: PROVIDER_FALLBACK,
    };
  } finally {
    mark.done();
  }

  const payload = await readJson(res);

  if (!res.ok) {
    const failure = describeFailure(res, payload);
    return {
      kind: "error",
      message: failure.message,
      hint: failure.hint ?? START_HINT,
      providers: PROVIDER_FALLBACK,
    };
  }

  const raw: unknown = isRecord(payload) ? payload.providers : null;
  const rows: unknown[] = Array.isArray(raw) ? (raw as unknown[]) : [];
  const providers: CallProviderInfo[] = rows.filter(isCallProviderInfo);

  if (providers.length === 0) {
    return {
      kind: "error",
      message: "The server sent a call list the app could not read.",
      hint: `Check the server log in the terminal running ${START_COMMAND}`,
      providers: PROVIDER_FALLBACK,
    };
  }

  return { kind: "ok", providers };
}

/**
 * Start the call.
 *
 * POST /api/call/start. What happens next depends on the provider:
 *   - manual, nothing is dialled, the session just records that the rep is
 *     dialling by hand.
 *   - whatsapp_link, the answer carries `openUrl` and the caller opens it.
 *   - whatsapp_cloud and twilio, the answer carries `callId` and the phone
 *     starts ringing.
 *
 * A refusal is not an error. The backend refuses with a full message when the
 * provider is not configured or the number cannot be read, and that message is
 * written for the rep, so show it as it is.
 *
 * @param body Session id, provider, and the numbers that provider needs.
 * @param signal Optional cancel signal.
 * @returns Always a result, never a thrown error.
 */
export async function startCall(
  body: CallStartBody,
  signal?: AbortSignal,
): Promise<StartResult> {
  const mark = deadline(START_TIMEOUT_MS, signal);

  let res: Response;
  try {
    res = await fetch("/api/call/start", {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      cache: "no-store",
      body: JSON.stringify({
        sessionId: body.sessionId,
        provider: body.provider,
        toNumber: body.toNumber ?? null,
        repNumber: body.repNumber ?? null,
      }),
      signal: mark.signal,
    });
  } catch (err) {
    // A timeout here is the one failure we must not state too plainly. The
    // request may have placed a real call before it went quiet, so the rep is
    // told to look at their phone instead of pressing the button again.
    const tail = mark.expired() ? " Check your phone, the call may still be ringing." : "";
    return {
      kind: "error",
      message: describeNetwork(err, mark, START_TIMEOUT_MS / 1000) + tail,
      hint: START_HINT,
    };
  } finally {
    mark.done();
  }

  const payload = await readJson(res);

  // The contract says a refusal comes back in the very same shape with
  // ok:false, so the status code is read second, not first. A 400 that carries
  // a real message is a refusal the rep can act on, not a broken app.
  if (isCallStartResult(payload)) {
    return payload.ok && res.ok
      ? { kind: "started", call: payload }
      : { kind: "refused", call: payload };
  }

  if (!res.ok) {
    const failure = describeFailure(res, payload);
    return { kind: "error", message: failure.message, hint: failure.hint };
  }

  return {
    kind: "error",
    message: "The server answered, but it did not say what happened to the call.",
    hint: `Check the server log in the terminal running ${START_COMMAND}`,
  };
}

/**
 * End the call.
 *
 * POST /api/call/{sessionId}/hangup. Only the app placed calls can be ended this
 * way. A manual or link call lives on the rep's own phone, so the app cannot end
 * it, and the backend answers ok for those anyway after marking the session
 * ended, which is the honest thing: the app stopped following the call.
 *
 * @param sessionId The session the call belongs to.
 * @param signal Optional cancel signal.
 * @returns Always a result, never a thrown error.
 */
export async function hangUp(sessionId: string, signal?: AbortSignal): Promise<HangUpResult> {
  const id = sessionId.trim();
  if (id.length === 0) {
    return { kind: "error", message: "There is no call id, so nothing can be ended.", hint: null };
  }

  const mark = deadline(HANGUP_TIMEOUT_MS, signal);

  let res: Response;
  try {
    res = await fetch(`/api/call/${encodeURIComponent(id)}/hangup`, {
      method: "POST",
      headers: { accept: "application/json" },
      cache: "no-store",
      signal: mark.signal,
    });
  } catch (err) {
    return {
      kind: "error",
      message: describeNetwork(err, mark, HANGUP_TIMEOUT_MS / 1000),
      hint: START_HINT,
    };
  } finally {
    mark.done();
  }

  if (res.ok) return { kind: "ok" };

  const payload = await readJson(res);
  const failure = describeFailure(res, payload);
  return { kind: "error", message: failure.message, hint: failure.hint };
}
