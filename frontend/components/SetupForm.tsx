"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { LucideIcon } from "lucide-react";
import {
  ArrowRight,
  Check,
  ChevronRight,
  Copy,
  Crosshair,
  Globe,
  GraduationCap,
  Phone,
  RotateCcw,
  ServerCrash,
  Sparkles,
  TriangleAlert,
  X,
} from "lucide-react";

import { DifficultyPicker } from "@/components/DifficultyPicker";
import { LANGUAGES } from "@/lib/config";
import { clearProfile, loadProfile, saveProfile } from "@/lib/profile";
import { saveSession } from "@/lib/session";
import { isDifficulty, isPracticeSession } from "@/lib/types";
import type {
  CallMode,
  Difficulty,
  DifficultyInfo,
  PracticeSession,
  PreparedSession,
} from "@/lib/types";

/* ============================================================
   CONSTANTS
   ============================================================ */

/** The server keeps this many characters of the what you sell (backend MAX_KB_LETTERS). */
const KB_KEPT = 24_000;
/** The request validator's hard ceiling. Past this the submit is blocked. */
const KB_LIMIT = 40_000;
/** How long the copy receipt stays on screen. Matches the .copy-ack keyframe. */
const COPY_ACK_MS = 1020;
/** Health poll interval for the header pill. */
const HEALTH_POLL_MS = 15_000;

const MICRO_BUTTON =
  "flex h-[22px] shrink-0 items-center gap-1.5 rounded-hair border border-line-strong px-2 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] ease-out hover:bg-surface-2 hover:text-text";

const PRIMARY_BUTTON =
  "relative flex h-12 w-full items-center justify-center gap-2 overflow-hidden rounded-hair bg-accent font-mono text-[12px] font-semibold uppercase tracking-[0.12em] text-[#04121A] transition-opacity duration-[120ms] ease-out";

const FIELD_LABEL = "font-mono text-micro uppercase text-muted";
const FIELD_DESC = "font-sans text-[12px] leading-[18px] text-muted";
const FIELD_INPUT =
  "h-11 w-full rounded-hair border border-line-strong bg-surface-2 pl-9 pr-3 font-sans text-body text-text placeholder:text-dim";
const FIELD_ICON =
  "pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-dim";

const SAMPLE_KNOWLEDGE_BASE = `Northbeam Studio, a six person web development and automation agency in Lahore. We have shipped for clients in the US, UK and UAE since 2019.

WHAT WE SELL
1. Marketing sites on Next.js. Two to four weeks. 3,500 to 9,000 USD.
2. Custom web apps and client portals. Six to twelve weeks. 12,000 to 40,000 USD.
3. Workflow automation. We wire the CRM, the forms, the invoicing and the email together so nobody copies data by hand. 1,800 to 6,000 USD to build, 400 USD a month to keep running.
4. Care plan. Hosting, updates, small changes, reply inside 24 hours. 350 USD a month.

WHY US
Fixed price and fixed date, written into the contract before we start. One senior developer owns your account, there are no juniors rotating through it. You get the code and the repository on day one, so you own everything we build.

PROOF
Aster Dental, 42 clinics. Their booking portal cut inbound phone calls by 61 percent in four months.
Vellum Legal. Intake automation saved 22 staff hours a week and paid for itself in seven weeks.
38 projects delivered, 3 of them late, and every one of those three shipped inside the same month.

COMMON OBJECTIONS
"You are too expensive." A cheap site that gets rebuilt twice costs more than one built right. The price and the date are fixed in writing, and I can send you the Aster numbers before you pay anything.

"We already have a developer." Good, we work next to them. Most clients start us on the automation their developer never has time for, and nobody has to be replaced.`;

/* ============================================================
   PRACTICE MODE
   ============================================================ */

/**
 * The three levels the picker shows.
 *
 * They ship in the bundle instead of being fetched. There is no browser route
 * for GET /api/practice/difficulties, only the two proxies the practice
 * contract asks for (start and debrief), so a fetch here would be a request
 * that always fails and a list that never changed. The same three keys, in the
 * same order, live in backend/app/practice.py, which is what the call is
 * actually run with, so a blurb edited there has to be copied here.
 */
const DIFFICULTIES: DifficultyInfo[] = [
  {
    key: "warm",
    label: "Warm",
    blurb: "Friendly. They ask real questions and give you time to talk.",
  },
  {
    key: "normal",
    label: "Normal",
    blurb: "Busy and short. They push back two or three times.",
  },
  {
    key: "brutal",
    label: "Brutal",
    blurb: "They want to hang up. You get one line to keep them.",
  },
];

/** The default level. Same default the backend uses when the field is missing. */
const DEFAULT_DIFFICULTY: Difficulty = "normal";

interface ModeChoice {
  mode: CallMode;
  label: string;
  hint: string;
  Icon: LucideIcon;
}

/** The two cells of the segmented control, in a fixed order. Real call is first. */
const MODE_CHOICES: ModeChoice[] = [
  { mode: "live", label: "Real call", hint: "A real person is on the phone", Icon: Phone },
  {
    mode: "practice",
    label: "Practice call",
    hint: "A robot client, for training",
    Icon: GraduationCap,
  },
];

/* ============================================================
   HEALTH PILL (header, client side polling)
   ============================================================ */

type HealthState = "checking" | "ok" | "nokey" | "down";

interface HealthPayload {
  status?: string;
  groqConfigured?: boolean;
  sessions?: number;
  version?: string;
  detail?: string;
  models?: { stt?: string; llm?: string };
}

const HEALTH_VIEW: Record<
  HealthState,
  { word: string; mark: string; wordClass: string; border: string; title: string }
> = {
  checking: {
    word: "Checking",
    mark: "border border-line-strong bg-transparent",
    wordClass: "text-muted",
    border: "border-line-strong",
    title: "Checking if the server is up",
  },
  ok: {
    word: "API ok",
    mark: "bg-ok",
    wordClass: "text-text",
    border: "border-line-strong",
    title: "Server is up and the Groq key is set",
  },
  nokey: {
    word: "No Groq key",
    mark: "bg-warn",
    wordClass: "text-text",
    border: "border-line-strong",
    title: "Server is up but GROQ_API_KEY is empty, so speech and answers will not work",
  },
  down: {
    word: "API down",
    mark: "bg-danger",
    wordClass: "text-danger",
    border: "border-danger",
    title: "The server is not answering",
  },
};

/**
 * The model strip in the page footer.
 *
 * The model ids are read from the live backend rather than hardcoded, because
 * an operator can swap either one through the environment and a stale name in
 * the footer would be a lie. Fetched once, no polling, silent when the backend
 * is not up.
 */
export function ModelStrip() {
  const [models, setModels] = useState<{ stt: string; llm: string } | null>(null);

  useEffect(() => {
    let alive = true;
    const controller = new AbortController();

    void (async () => {
      try {
        const res = await fetch("/api/health", { cache: "no-store", signal: controller.signal });
        if (!alive || !res.ok) return;
        const data = (await res.json()) as HealthPayload;
        if (!alive || !data.models?.stt || !data.models?.llm) return;
        setModels({ stt: data.models.stt, llm: data.models.llm });
      } catch {
        // The health pill already tells the user the backend is down.
      }
    })();

    return () => {
      alive = false;
      controller.abort();
    };
  }, []);

  if (!models) {
    return <span className="font-mono text-micro uppercase text-dim">Groq free tier</span>;
  }

  return (
    <span className="font-mono text-micro uppercase text-muted">
      {models.stt} plus {models.llm}
    </span>
  );
}

export function HealthPill() {
  const [state, setState] = useState<HealthState>("checking");

  useEffect(() => {
    let alive = true;
    const controller = new AbortController();

    const poll = async () => {
      try {
        const res = await fetch("/api/health", { cache: "no-store", signal: controller.signal });
        if (!alive) return;
        if (!res.ok) {
          setState("down");
          return;
        }
        const data = (await res.json()) as HealthPayload;
        if (!alive) return;
        if (data.status !== "ok") {
          setState("down");
          return;
        }
        setState(data.groqConfigured ? "ok" : "nokey");
      } catch {
        if (alive) setState("down");
      }
    };

    void poll();
    const timer = window.setInterval(() => {
      void poll();
    }, HEALTH_POLL_MS);

    return () => {
      alive = false;
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  const view = HEALTH_VIEW[state];

  return (
    <div
      className={`relative flex h-[26px] min-w-[116px] items-center gap-2 rounded-hair border px-2.5 ${view.border}`}
      title={view.title}
    >
      <span className={`h-1.5 w-1.5 shrink-0 ${view.mark}`} aria-hidden="true" />
      <span className={`font-mono text-status uppercase ${view.wordClass}`} aria-live="polite">
        {view.word}
      </span>
    </div>
  );
}

/* ============================================================
   URL NORMALISATION (mirrors the backend normalize_url and its SSRF guard)
   ============================================================ */

type UrlState =
  | { kind: "empty" }
  | { kind: "ok"; url: string }
  | { kind: "bad"; reason: string };

const PRIVATE_HOST =
  /^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|169\.254\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)$/i;

function normalizeUrl(raw: string): UrlState {
  const trimmed = raw.trim();
  if (!trimmed) return { kind: "empty" };
  if (/\s/.test(trimmed)) return { kind: "bad", reason: "A web address cannot contain a space." };

  const hasScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed);
  let parsed: URL;
  try {
    parsed = new URL(hasScheme ? trimmed : `https://${trimmed}`);
  } catch {
    return { kind: "bad", reason: "That does not look like a web address." };
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { kind: "bad", reason: "Only http and https addresses can be fetched." };
  }

  const host = parsed.hostname;
  if (!host) return { kind: "bad", reason: "That address is missing the site name." };
  if (
    PRIVATE_HOST.test(host) ||
    host.endsWith(".local") ||
    host.endsWith(".internal") ||
    host.endsWith(".localhost")
  ) {
    return { kind: "bad", reason: "Private and local addresses cannot be read." };
  }
  if (!host.includes(".")) {
    return { kind: "bad", reason: "Add the ending too, like acme.com." };
  }

  const bare = parsed.pathname === "/" && !parsed.search && !parsed.hash;
  return { kind: "ok", url: bare ? `${parsed.protocol}//${parsed.host}` : parsed.toString() };
}

/* ============================================================
   PREPARE CONTEXT RESPONSE
   ============================================================ */

interface PrepareResponse {
  sessionId: string;
  systemPrompt: string;
  clientUrl: string | null;
  clientTitle: string | null;
  clientExcerpt: string | null;
  scrapeChars: number;
  scrapeOk: boolean;
  scrapeError: string | null;
  createdAt: number;
}

function isPrepareResponse(value: unknown): value is PrepareResponse {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v.sessionId === "string" && typeof v.systemPrompt === "string";
}

/**
 * What actually goes into localStorage.
 *
 * One field more than a prepared session: the client's phone number, so the call
 * page can fill the number in for the rep instead of asking for it a second
 * time. It is never sent to the backend from here, because building the call
 * notes has nothing to do with dialling.
 *
 * lib/session.ts normalises a stored record down to PreparedSession and drops
 * every field it does not know, so the call page reads this one out of the
 * stored JSON itself, exactly the way it already reads the practice flag.
 */
type StoredSession = (PreparedSession | PracticeSession) & {
  /** The client's number as the rep typed it, or null when the box was empty. */
  clientPhone: string | null;
};

/** Turns any non 200 payload into one sentence a human can act on. */
function describeFailure(status: number, payload: unknown): string {
  if (typeof payload === "object" && payload !== null) {
    const v = payload as Record<string, unknown>;
    if (typeof v.error === "string" && v.error) {
      const detail = typeof v.detail === "string" && v.detail ? ` ${v.detail}` : "";
      return `${v.error}.${detail}`;
    }
    if (Array.isArray(v.detail)) {
      const first = v.detail[0];
      if (typeof first === "object" && first !== null) {
        const msg = (first as Record<string, unknown>).msg;
        if (typeof msg === "string") return msg;
      }
    }
    if (typeof v.detail === "string" && v.detail) return v.detail;
  }
  return `The backend answered ${status} and the reason was not readable.`;
}

/* ============================================================
   THE FORM
   ============================================================ */

type Phase = "idle" | "fetching" | "fusing" | "ready";
type Notice =
  | { kind: "backend"; detail: string; hint: string }
  | { kind: "request"; detail: string };
type CopyState = "none" | "done" | "failed";

/**
 * The button label per phase.
 *
 * A practice call runs the same two milestones (the client site is fetched, the
 * notes are fused) and then builds the robot client on top, so it gets its own
 * third label. Nothing about the live column changed.
 */
const PHASE_LABEL: Record<CallMode, Record<Phase, string>> = {
  live: {
    idle: "Start the call",
    fetching: "Fetching site",
    fusing: "Fusing context",
    ready: "Ready",
  },
  practice: {
    idle: "Start practice call",
    fetching: "Fetching site",
    fusing: "Making the client",
    ready: "Ready",
  },
};

function shortId(id: string): string {
  return id.replace(/-/g, "").slice(0, 4).toUpperCase();
}

function countLabel(chars: number): string {
  const n = chars.toLocaleString("en-US");
  if (chars > KB_LIMIT) return `${n} letters, limit ${KB_LIMIT.toLocaleString("en-US")}`;
  if (chars > KB_KEPT) return `${n} letters, first ${KB_KEPT.toLocaleString("en-US")} kept`;
  return `${n} letters`;
}

export function SetupForm() {
  const router = useRouter();
  const formRef = useRef<HTMLFormElement>(null);
  const copyTimer = useRef<number | null>(null);
  const modeName = useId();

  const [knowledgeBase, setKnowledgeBase] = useState("");
  const [urlRaw, setUrlRaw] = useState("");
  const [clientNotes, setClientNotes] = useState("");
  const [clientPhone, setClientPhone] = useState("");
  const [goal, setGoal] = useState("");
  const [language, setLanguage] = useState(LANGUAGES[0]?.code ?? "en");

  /* Practice mode. Off unless the rep picks it, so the live path is untouched. */
  const [mode, setMode] = useState<CallMode>("live");
  const [difficulty, setDifficulty] = useState<Difficulty>(DEFAULT_DIFFICULTY);

  const [phase, setPhase] = useState<Phase>("idle");
  const [notice, setNotice] = useState<Notice | null>(null);
  const [result, setResult] = useState<PreparedSession | PracticeSession | null>(null);
  /* True once the saved profile has been read, so the page does not flash the
     empty box before the remembered knowledge base lands. */
  const [profileLoaded, setProfileLoaded] = useState(false);
  /* True when what you sell came back from storage rather than being typed now.
     It only decides whether that panel starts folded away. */
  const [profileWasSaved, setProfileWasSaved] = useState(false);
  const [sellOpen, setSellOpen] = useState(false);
  const [copy, setCopy] = useState<CopyState>("none");

  /* What you sell comes back. Who you are calling never does.
     The old build remembered the whole session and offered to walk back into it,
     which quietly invited calling the next client with the last one's notes
     loaded. Now only the half that is genuinely yours is remembered. */
  /* eslint-disable react-hooks/set-state-in-effect -- every setState in this
     effect is a one time read of something that does not exist during the
     prerender, the address bar and localStorage. Reading either one after mount
     is the documented pattern, and a lazy initial state instead would make the
     server and the client paint different things. */
  useEffect(() => {
    /* "?mode=practice" opens this page already on practice, which is what the
       Practice call link on the lead list points at. It is read here and not
       with useSearchParams because this page is prerendered as static, and that
       hook would drag the whole route into being rendered per request just to
       learn one word. Read after mount, like the profile below, so the server
       and the client never disagree about the first paint. */
    if (window.location.search.includes("mode=practice")) {
      setMode("practice");
    }

    // localStorage does not exist during the prerender, so this cannot be a lazy
    // initial state without the server and the client disagreeing about what to
    // paint. Reading it once after mount is the pattern.
    const saved = loadProfile();
    if (saved) {
      setKnowledgeBase(saved.knowledgeBase);
      setLanguage(saved.language);
      setProfileWasSaved(true);
      setSellOpen(false);
    } else {
      setSellOpen(true);
    }
    setProfileLoaded(true);
  }, []);
  /* eslint-enable react-hooks/set-state-in-effect */

  useEffect(() => {
    return () => {
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    };
  }, []);

  const urlState = useMemo(() => normalizeUrl(urlRaw), [urlRaw]);

  const letters = knowledgeBase.length;
  const overLimit = letters > KB_LIMIT;
  const emptyKb = knowledgeBase.trim().length === 0;
  const busy = phase === "fetching" || phase === "fusing";
  const blocked = emptyKb || overLimit || urlState.kind === "bad";
  const buttonDisabled = blocked || busy || phase === "ready";
  const practice = mode === "practice";

  const counterTone = overLimit ? "text-danger" : letters > KB_KEPT ? "text-warn" : "text-dim";

  /** Any edit invalidates a built context, so the button becomes live again. */
  const invalidate = useCallback(() => {
    setPhase((p) => (p === "ready" ? "idle" : p));
  }, []);

  const build = useCallback(async () => {
    if (blocked || busy) return;
    setNotice(null);
    setCopy("none");

    const clientUrl = urlState.kind === "ok" ? urlState.url : null;
    /* The phase machine is driven by real milestones, never by a timer.
       1. A URL was given, so the backend is out fetching it.
       2. The response headers arrived, so the fetch is over and the prompt is being fused.
       3. The payload parsed and the session was saved. */
    setPhase(clientUrl ? "fetching" : "fusing");

    /* Same body both ways, because you rehearse against the real client you are
       about to call. Practice adds one field and one endpoint, nothing else. */
    const body: Record<string, unknown> = {
      knowledgeBase: knowledgeBase.trim(),
      clientUrl,
      clientContext: clientNotes.trim() || null,
      callGoal: goal.trim() || null,
      language,
    };
    if (practice) body.difficulty = difficulty;

    let res: Response;
    try {
      res = await fetch(practice ? "/api/practice/start" : "/api/prepare-context", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (err) {
      setPhase("idle");
      setNotice({
        kind: "request",
        detail: err instanceof Error ? err.message : "The app could not send the request.",
      });
      return;
    }

    setPhase("fusing");

    let payload: unknown = null;
    try {
      payload = (await res.json()) as unknown;
    } catch {
      payload = null;
    }

    if (!res.ok) {
      setPhase("idle");
      const v = (payload ?? {}) as Record<string, unknown>;
      /* Only the proxy's own "Backend unreachable" gets the start command notice.
         Every other 502 came from a backend that did answer, just badly. */
      if (res.status === 502 && v.error === "Backend unreachable") {
        setNotice({
          kind: "backend",
          detail:
            typeof v.detail === "string" && v.detail
              ? v.detail
              : "The web app could not reach the server.",
          hint: typeof v.hint === "string" && v.hint ? v.hint : "cd backend && ./run.sh",
        });
      } else {
        setNotice({ kind: "request", detail: describeFailure(res.status, payload) });
      }
      return;
    }

    if (!isPrepareResponse(payload)) {
      setPhase("idle");
      setNotice({
        kind: "request",
        detail: "The server answered, but it did not send a call id.",
      });
      return;
    }

    const base: PreparedSession = {
      sessionId: payload.sessionId,
      systemPrompt: payload.systemPrompt,
      clientUrl: payload.clientUrl,
      clientTitle: payload.clientTitle,
      clientExcerpt: payload.clientExcerpt,
      scrapeChars: payload.scrapeChars,
      scrapeOk: payload.scrapeOk,
      scrapeError: payload.scrapeError,
      hasNotes: clientNotes.trim().length > 0,
      createdAt: payload.createdAt,
      language,
    };

    let session: PreparedSession | PracticeSession = base;

    if (practice) {
      /* The four practice fields are read leniently. We know this is a practice
         call because of the endpoint we just called, so a missing name or a
         missing opening line is not worth throwing the whole session away for:
         the opening line is spoken from the server over the socket anyway, and
         this copy is only for the record. */
      const extra = payload as unknown as Record<string, unknown>;
      session = {
        ...base,
        mode: "practice",
        difficulty: isDifficulty(extra.difficulty) ? extra.difficulty : difficulty,
        personaName: typeof extra.personaName === "string" ? extra.personaName : null,
        openingLine: typeof extra.openingLine === "string" ? extra.openingLine : "",
      };
    }

    /* The whole object is written, practice fields and the client number
       included, so the call page can tell a rehearsal from a real call after a
       refresh and can fill in the number the rep already typed.

       A practice call stores no number. Its box is not on screen, so a number
       still sitting in the state is one the rep typed for a live call and then
       switched away from, and writing somebody's real phone number into storage
       under a rehearsal that will never dial it is not ours to do. */
    const stored: StoredSession = {
      ...session,
      clientPhone: practice ? null : clientPhone.trim() || null,
    };
    saveSession(stored);
    /* Only now, on a build that worked. Saving on every keystroke would remember
       a half typed knowledge base. */
    saveProfile({ knowledgeBase: knowledgeBase.trim(), language });
    setResult(session);
    setPhase("ready");

    /* Straight onto the glass. The old build parked the rep on a result card and
       made them press a second button to open the thing they came for. */
    const query = `session=${encodeURIComponent(session.sessionId)}`;
    router.push(practice ? `/call?${query}&mode=practice` : `/call?${query}`);
  }, [
    blocked,
    busy,
    clientNotes,
    clientPhone,
    difficulty,
    goal,
    knowledgeBase,
    language,
    practice,
    router,
    urlState,
  ]);

  const onSubmit = useCallback(
    (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      void build();
    },
    [build],
  );

  /* Cmd or Ctrl plus Enter submits from any field in the form. */
  const onKeyDown = useCallback((event: React.KeyboardEvent<HTMLFormElement>) => {
    if (event.key !== "Enter") return;
    if (!event.metaKey && !event.ctrlKey) return;
    event.preventDefault();
    formRef.current?.requestSubmit();
  }, []);

  const copyPrompt = useCallback(async () => {
    if (!result) return;
    let ok = true;
    try {
      await navigator.clipboard.writeText(result.systemPrompt);
    } catch {
      ok = false;
    }
    setCopy(ok ? "done" : "failed");
    if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopy("none"), COPY_ACK_MS);
  }, [result]);

  /* Forget what the rep sells, for a rep who sells something else now. Who they
     are calling was never remembered, so there is nothing else to clear. */
  const forgetProfile = useCallback(() => {
    clearProfile();
    setKnowledgeBase("");
    setProfileWasSaved(false);
    setSellOpen(true);
    invalidate();
  }, [invalidate]);

  /* The mode rides in the address bar as well as in storage, so the call page
     knows which kind of call this is even on a machine where storage is off. */
  const openTeleprompter = useCallback(() => {
    if (!result) return;
    const query = `session=${encodeURIComponent(result.sessionId)}`;
    router.push(isPracticeSession(result) ? `/call?${query}&mode=practice` : `/call?${query}`);
  }, [result, router]);

  const pickMode = useCallback(
    (next: CallMode) => {
      setMode(next);
      invalidate();
    },
    [invalidate],
  );

  const pickDifficulty = useCallback(
    (next: Difficulty) => {
      setDifficulty(next);
      invalidate();
    },
    [invalidate],
  );

  const primaryClass = `${PRIMARY_BUTTON} ${
    busy || phase === "ready"
      ? "pointer-events-none"
      : blocked
        ? "pointer-events-none opacity-40"
        : "hover:opacity-90"
  }`;

  const resultIsPractice = isPracticeSession(result);

  return (
    <form ref={formRef} onSubmit={onSubmit} onKeyDown={onKeyDown} className="mt-10" noValidate>

      {/* KIND OF CALL. Two native radios, so the arrow keys, the tab stop and the
          screen reader wording are the browser's job and not ours. */}
      <fieldset className="mb-4">
        <legend className="sr-only">Pick the kind of call</legend>
        <div className="seam-grid grid-cols-2">
          {MODE_CHOICES.map((choice) => {
            const on = mode === choice.mode;
            return (
              /* Same construction as the difficulty cards below: a 2 px accent
                 bar in the cell gutter, an accent icon, and the product's 6 px
                 square for "this one is on". One vocabulary, two controls. */
              <label
                key={choice.mode}
                className={`relative flex cursor-pointer items-center gap-3 px-4 py-3 transition-colors duration-[120ms] ease-out ${
                  on ? "bg-surface-2" : "bg-surface hover:bg-surface-2"
                }`}
              >
                <input
                  type="radio"
                  name={modeName}
                  value={choice.mode}
                  checked={on}
                  onChange={() => pickMode(choice.mode)}
                  className="peer sr-only"
                />
                {/* The focus ring for the hidden radio, drawn on the cell. */}
                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute inset-0 hidden outline outline-2 outline-accent [outline-offset:-2px] peer-focus-visible:block"
                />
                {on ? (
                  <span
                    aria-hidden="true"
                    className="pointer-events-none absolute inset-y-0 left-0 w-0.5 bg-accent"
                  />
                ) : null}
                <choice.Icon
                  className={`h-4 w-4 shrink-0 transition-colors duration-[120ms] ${
                    on ? "text-accent" : "text-muted"
                  }`}
                  aria-hidden="true"
                />
                <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className={`font-sans text-chip ${on ? "text-text" : "text-muted"}`}>
                    {choice.label}
                  </span>
                  {/* --muted, not --dim. This one line is the only thing that
                      tells the rep what the two modes are, so it has to pass
                      the read it contrast bar (design spec 2.1 and 3.3). */}
                  <span className="truncate font-mono text-micro font-normal uppercase tracking-[0.10em] text-muted">
                    {choice.hint}
                  </span>
                </span>
                <span
                  aria-hidden="true"
                  className={`h-1.5 w-1.5 shrink-0 ${
                    on ? "bg-accent" : "border border-line-strong"
                  }`}
                />
              </label>
            );
          })}
        </div>
        <p className="mt-2 font-sans text-[12px] leading-[18px] text-muted">
          {practice
            ? "Practice call: you talk to a robot client. Nobody real is on the phone, so you can say anything. At the end you get a score."
            : "Real call: a real person is on the phone. The copilot writes your next line while they talk."}
        </p>
      </fieldset>

      {practice ? (
        <div className="mb-4">
          <DifficultyPicker
            levels={DIFFICULTIES}
            value={difficulty}
            onChange={pickDifficulty}
            disabled={busy}
          />
        </div>
      ) : null}

      {/* WHAT YOU SELL, FOLDED AWAY ONCE IT IS KNOWN.
          This half belongs to the rep and barely changes, so after the first
          call it collapses to one line and the client half gets the whole page.
          That is the point of the split: the thing that changes every call is
          the thing that should be in front of you. */}
      {profileLoaded && !sellOpen ? (
        <section className="mb-px flex flex-wrap items-center justify-between gap-3 bg-surface px-5 py-3">
          <div className="flex min-w-0 items-center gap-3">
            <span className={FIELD_LABEL}>What you sell</span>
            <span className="truncate font-sans text-body text-muted">
              {knowledgeBase.trim().slice(0, 90)}
              {knowledgeBase.trim().length > 90 ? "..." : ""}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <span className="font-mono text-micro uppercase tabnum text-dim">
              {countLabel(letters)}
            </span>
            <button
              type="button"
              onClick={() => setSellOpen(true)}
              className={MICRO_BUTTON}
              title="Open the box and change what you sell"
            >
              Change
            </button>
          </div>
        </section>
      ) : null}

      <div
        className={`seam-grid grid-cols-1 ${sellOpen ? "lg:grid-cols-[1.15fr_1fr]" : ""}`}
      >
        {/* KNOWLEDGE BASE */}
        <section className={`flex-col gap-3 bg-surface p-5 ${sellOpen ? "flex" : "hidden"}`}>
          <div className="flex items-center justify-between gap-3">
            <label htmlFor="kb" className={FIELD_LABEL}>
              What you sell
            </label>
            <button
              type="button"
              className={MICRO_BUTTON}
              title="Puts a full example in the box"
              onClick={() => {
                setKnowledgeBase(SAMPLE_KNOWLEDGE_BASE);
                invalidate();
              }}
            >
              <Sparkles className="h-3 w-3" aria-hidden="true" />
              Load sample
            </button>
          </div>
          <p id="kb-desc" className={FIELD_DESC}>
            What you sell, your prices, your proof, and the objections you keep hearing. The
            copilot can only say what is written here, so be exact and use real numbers.
          </p>
          <textarea
            id="kb"
            name="knowledgeBase"
            value={knowledgeBase}
            spellCheck={false}
            aria-describedby="kb-desc kb-count"
            aria-invalid={overLimit}
            onChange={(e) => {
              setKnowledgeBase(e.target.value);
              invalidate();
            }}
            placeholder="What you sell, your prices, why you are better, your proof, and the two objections you hear most."
            className="min-h-[260px] w-full resize-y rounded-hair border border-line-strong bg-surface-2 p-3 font-mono text-chip leading-5 text-text placeholder:text-dim"
          />
          <div className="flex items-center justify-between gap-3">
            {profileWasSaved ? (
              <button
                type="button"
                onClick={forgetProfile}
                className={MICRO_BUTTON}
                title="Empty the box and forget what was saved"
              >
                <X className="h-3 w-3" aria-hidden="true" />
                Forget this
              </button>
            ) : (
              <span />
            )}
            <div className="flex items-center gap-3">
              <span
                id="kb-count"
                className={`font-mono text-micro uppercase tabnum ${counterTone}`}
              >
                {countLabel(letters)}
              </span>
              {knowledgeBase.trim() ? (
                <button
                  type="button"
                  onClick={() => setSellOpen(false)}
                  className={MICRO_BUTTON}
                  title="Fold this away and get on with the call"
                >
                  Done
                </button>
              ) : null}
            </div>
          </div>
        </section>

        {/* CLIENT */}
        <section className="flex flex-col gap-3 bg-surface p-5">
          <div className="flex items-center justify-between gap-3">
            <span className={FIELD_LABEL}>Client</span>
            <span className="font-mono text-micro uppercase text-dim">All optional</span>
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor="url" className={FIELD_LABEL}>
              Client website
            </label>
            <p id="url-desc" className={FIELD_DESC}>
              Just the domain is fine. We read the page once and add it to your notes.
            </p>
            <div className="relative">
              <Globe className={FIELD_ICON} aria-hidden="true" />
              <input
                id="url"
                name="clientUrl"
                type="text"
                inputMode="url"
                autoComplete="url"
                spellCheck={false}
                value={urlRaw}
                aria-describedby="url-desc url-hint"
                aria-invalid={urlState.kind === "bad"}
                onChange={(e) => {
                  setUrlRaw(e.target.value);
                  invalidate();
                }}
                placeholder="acme.com"
                className={FIELD_INPUT}
              />
            </div>
            <p
              id="url-hint"
              className={`font-mono text-micro font-normal tracking-[0.04em] ${
                urlState.kind === "bad" ? "text-danger" : "text-muted"
              }`}
            >
              {urlState.kind === "ok"
                ? `Will fetch ${urlState.url}`
                : urlState.kind === "bad"
                  ? urlState.reason
                  : "No website? Leave this empty and use the box below."}
            </p>
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor="client-notes" className={FIELD_LABEL}>
              About the client
            </label>
            <p id="client-notes-desc" className={FIELD_DESC}>
              No website? Write it here instead. Their trade, their city, their size, and how
              they get customers today. This is what you use when you are selling them their
              first website.
            </p>
            <textarea
              id="client-notes"
              name="clientContext"
              rows={4}
              value={clientNotes}
              aria-describedby="client-notes-desc"
              onChange={(e) => {
                setClientNotes(e.target.value);
                invalidate();
              }}
              placeholder="Al Madina Auto Parts, a spare parts shop in Lahore. No website. They sell on WhatsApp and a Facebook page with 4,000 followers. Eight staff, walk in customers plus phone orders."
              className={`${FIELD_INPUT} min-h-[104px] resize-y py-2.5 leading-[1.55]`}
            />
          </div>

          {/* The number is kept on this browser only. It is not sent with the
              notes, because building the notes has nothing to do with dialling.
              The call page picks it up from the saved call and fills it in.

              A practice call has nobody to ring. The robot client lives in this
              browser, the call page mounts no call button at all for it, and it
              never reads this number. So the box is not shown, because a box
              that asks for a real client's phone number and then does nothing
              with it is a promise the app cannot keep. */}
          {practice ? null : (
            <div className="flex flex-col gap-1.5">
              <label htmlFor="client-phone" className={FIELD_LABEL}>
                Client phone number
              </label>
              <p id="client-phone-desc" className={FIELD_DESC}>
                Only fill this in if you want the app to call the client for you. Write the country
                code too, like +92.
              </p>
              <div className="relative">
                <Phone className={FIELD_ICON} aria-hidden="true" />
                <input
                  id="client-phone"
                  name="clientPhone"
                  type="text"
                  inputMode="tel"
                  autoComplete="off"
                  spellCheck={false}
                  value={clientPhone}
                  aria-describedby="client-phone-desc"
                  onChange={(e) => {
                    setClientPhone(e.target.value);
                    invalidate();
                  }}
                  placeholder="+92 300 1234567"
                  className={FIELD_INPUT}
                />
              </div>
            </div>
          )}

          <div className="flex flex-col gap-1.5">
            <label htmlFor="goal" className={FIELD_LABEL}>
              Call goal
            </label>
            <p id="goal-desc" className={FIELD_DESC}>
              One line. What would make this call a win.
            </p>
            <div className="relative">
              <Crosshair className={FIELD_ICON} aria-hidden="true" />
              <input
                id="goal"
                name="callGoal"
                type="text"
                value={goal}
                aria-describedby="goal-desc"
                onChange={(e) => {
                  setGoal(e.target.value);
                  invalidate();
                }}
                placeholder="Book a 20 minute demo with the marketing lead"
                className={FIELD_INPUT}
              />
            </div>
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor="lang" className={FIELD_LABEL}>
              Language
            </label>
            <p id="lang-desc" className={FIELD_DESC}>
              The language the copilot writes in. This is the language you will speak.
            </p>
            <select
              id="lang"
              name="language"
              value={language}
              aria-describedby="lang-desc"
              onChange={(e) => {
                setLanguage(e.target.value);
                invalidate();
              }}
              className="h-11 w-full rounded-hair border border-line-strong bg-surface-2 px-3 font-sans text-body text-text"
            >
              {LANGUAGES.map((item) => (
                <option key={item.code} value={item.code}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
        </section>
      </div>

      {/* PRIMARY ACTION */}
      <button type="submit" disabled={buttonDisabled} className={`${primaryClass} mt-4`}>
        {phase === "ready" ? <Check className="h-3.5 w-3.5" aria-hidden="true" /> : null}
        <span aria-live="polite">{PHASE_LABEL[mode][phase]}</span>
        {busy ? (
          <span
            className="absolute inset-x-0 bottom-0 h-0.5 overflow-hidden bg-[#04121A]/30"
            aria-hidden="true"
          >
            <span className="block h-full w-2/5 bg-[#04121A] animate-sweep" />
          </span>
        ) : null}
      </button>
      <p className="mt-2 text-center font-mono text-micro uppercase text-muted">
        {emptyKb
          ? "Write what you sell to carry on"
          : overLimit
            ? "Make it shorter than 40,000 letters"
            : "Press Cmd or Ctrl and Enter from any box"}
      </p>

      {/* FAILURE NOTICES */}
      {notice?.kind === "backend" ? (
        <div className="mt-4 flex gap-3 border-l-2 border-danger bg-surface-2 p-3">
          <ServerCrash className="mt-0.5 h-4 w-4 shrink-0 text-danger" aria-hidden="true" />
          <div className="flex min-w-0 flex-col gap-2">
            <p className="text-body text-muted">
              The FastAPI backend is not running, so there is nothing to build the context with.
              Start it in a second terminal and press the button again.
            </p>
            <code className="block overflow-x-auto rounded-hair border border-line bg-bg p-2 font-mono text-[12px] leading-[19px] text-text">
              {notice.hint}
            </code>
            <p className="font-mono text-micro font-normal tracking-[0.04em] text-muted">
              {notice.detail}
            </p>
          </div>
        </div>
      ) : null}

      {notice?.kind === "request" ? (
        <div className="mt-4 flex gap-3 border-l-2 border-danger bg-surface-2 p-3">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-danger" aria-hidden="true" />
          <p className="text-body text-muted">{notice.detail}</p>
        </div>
      ) : null}

      {/* RESULT */}
      {result ? (
        <section
          className="seam-grid mt-4 grid-cols-1"
          aria-label={resultIsPractice ? "Prepared practice call" : "Prepared call context"}
        >
          <div className="flex flex-col gap-3 bg-surface p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <span className={FIELD_LABEL}>
                {resultIsPractice ? "Your practice call is ready" : "Your call is ready"}
              </span>
              <div className="flex items-center gap-2">
                <button type="button" onClick={() => void copyPrompt()} className={MICRO_BUTTON}>
                  <Copy className="h-3 w-3" aria-hidden="true" />
                  Copy the notes
                </button>
                <button
                  type="button"
                  onClick={() => setPhase("idle")}
                  className={MICRO_BUTTON}
                  title="Turn the button back on so you can run it again"
                >
                  <RotateCcw className="h-3 w-3" aria-hidden="true" />
                  Rebuild
                </button>
              </div>
            </div>

            {/* The one thing the rep must not get wrong about a practice call. */}
            {isPracticeSession(result) ? (
              <div className="flex gap-3 border-l-2 border-accent-2 bg-surface-2 p-3">
                <GraduationCap
                  className="mt-0.5 h-4 w-4 shrink-0 text-accent-2"
                  aria-hidden="true"
                />
                <div className="flex min-w-0 flex-col gap-1">
                  <p className="font-sans text-body text-muted">
                    This is a practice call. The client is a robot, not a real person. You are
                    not calling anyone. It talks out loud through this browser, so turn your
                    sound on. Level:{" "}
                    {DIFFICULTIES.find((l) => l.key === result.difficulty)?.label ??
                      result.difficulty}
                    .
                  </p>
                  {/* --muted, not --dim. The rep is being asked to read this
                      line, so it cannot sit at the dim contrast. */}
                  {result.openingLine ? (
                    <p className="font-mono text-micro font-normal tracking-[0.04em] text-muted">
                      It starts with: {result.openingLine}
                    </p>
                  ) : null}
                </div>
              </div>
            ) : null}

            <div className="flex flex-col gap-1">
              <p className="text-lede text-text">
                {result.clientTitle ??
                  (result.hasNotes
                    ? "Notes built from what you sell and what you know about the client"
                    : "Notes built from what you sell")}
              </p>
              {result.clientUrl ? (
                <p className="break-all font-mono text-[12px] leading-[18px] text-dim">
                  {result.clientUrl}
                </p>
              ) : null}
            </div>

            <div className="flex flex-wrap items-center gap-4">
              {result.clientUrl ? (
                <span className="font-mono text-micro uppercase tabnum text-dim">
                  {result.scrapeChars.toLocaleString("en-US")} letters read
                </span>
              ) : null}
              <span className="font-mono text-micro uppercase tabnum text-dim">
                Ses {shortId(result.sessionId)}
              </span>
              <span className="font-mono text-micro uppercase text-dim">
                {LANGUAGES.find((l) => l.code === result.language)?.label ?? result.language}
              </span>
              {copy === "done" ? (
                <span className="copy-ack font-mono text-micro uppercase text-ok">Copied</span>
              ) : null}
              {copy === "failed" ? (
                <span className="copy-ack font-mono text-micro uppercase text-danger">
                  Copy blocked by the browser
                </span>
              ) : null}
            </div>

            {!result.scrapeOk && result.clientUrl ? (
              <div className="flex gap-3 border-l-2 border-warn bg-surface-2 p-3">
                <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-warn" aria-hidden="true" />
                <div className="flex min-w-0 flex-col gap-1">
                  <p className="font-sans text-body text-muted">
                    {result.hasNotes
                      ? "Could not read that site. Your notes were built from what you sell and what you wrote about the client."
                      : "Could not read that site. Your notes were built from what you sell only."}
                  </p>
                  {result.scrapeError ? (
                    <p className="break-words font-mono text-micro font-normal tracking-[0.04em] text-dim">
                      {result.scrapeError}
                    </p>
                  ) : null}
                </div>
              </div>
            ) : result.clientExcerpt ? (
              <p className="line-clamp-2 font-sans text-[12px] leading-[18px] text-muted">
                {result.clientExcerpt}
              </p>
            ) : result.hasNotes ? (
              <p className="font-sans text-[12px] leading-[18px] text-muted">
                No website was used. The copilot will work from what you wrote about the client.
              </p>
            ) : (
              <p className="font-sans text-[12px] leading-[18px] text-muted">
                No client details were given, so the copilot will ask questions before it makes any
                claim. Add a website or a few lines about them for sharper answers.
              </p>
            )}

            <details className="group">
              <summary
                className={`flex w-fit cursor-pointer list-none items-center gap-2 ${FIELD_LABEL} [&::-webkit-details-marker]:hidden`}
              >
                <ChevronRight
                  className="h-3 w-3 transition-transform duration-[140ms] ease-out group-open:rotate-90"
                  aria-hidden="true"
                />
                The notes
              </summary>
              <pre className="rail-scroll mt-2 max-h-[320px] whitespace-pre-wrap break-words rounded-hair border border-line bg-bg p-3 font-mono text-[12px] leading-[19px] text-muted">
                {result.systemPrompt}
              </pre>
            </details>
          </div>

          <div className="bg-surface p-5">
            <button
              type="button"
              onClick={openTeleprompter}
              className={`${PRIMARY_BUTTON} hover:opacity-90`}
            >
              {resultIsPractice ? "Open the practice call" : "Open teleprompter"}
              <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
            <p className="mt-2 text-center font-mono text-micro uppercase text-muted">
              {resultIsPractice
                ? "Turn your sound on, the client talks first"
                : "Saved on this browser, so a refresh keeps the call alive"}
            </p>
          </div>
        </section>
      ) : null}
    </form>
  );
}
