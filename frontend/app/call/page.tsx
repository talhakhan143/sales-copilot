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
 */

import { Suspense, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ArrowLeft, Check, Copy, ScrollText, Send, X } from "lucide-react";

import { AudioSourcePicker } from "@/components/AudioSourcePicker";
import { Debrief } from "@/components/Debrief";
import { LatencyMeter } from "@/components/LatencyMeter";
import { ObjectionBar } from "@/components/ObjectionBar";
import { PracticeBar } from "@/components/PracticeBar";
import { StatusPill } from "@/components/StatusPill";
import { Teleprompter } from "@/components/Teleprompter";
import { TranscriptRail } from "@/components/TranscriptRail";
import { WaveVisualizer } from "@/components/WaveVisualizer";
import { useTeleprompter } from "@/lib/hooks/useTeleprompter";
import { SESSION_STORAGE_KEY, loadSession } from "@/lib/session";
import { isDebrief, isDifficulty } from "@/lib/types";
import type {
  Debrief as DebriefData,
  Difficulty,
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

  // localStorage does not exist during the prerender, so the restore cannot be a
  // lazy initial state without the server and the client disagreeing about which
  // screen to paint. Reading it once after mount is the pattern, and the two
  // setState calls are the whole point of the effect.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setStored(loadSession());
    setStoredPractice(readStoredPractice());
    setStoredChecked(true);
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
    busy,
    error,
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

  /* ---------------- derived ---------------- */

  const socketOpen = connState === "open";
  const noAudio = !captures.client && !captures.rep;
  /* No nudge in practice: there is no client audio to plug in, only the mic. */
  const showNudge = Boolean(sessionId) && socketOpen && noAudio && !nudgeOff && !practiceOn;
  const sessionGone = status === "error" && SESSION_GONE.test(error ?? "");
  const actions = quickActions.length > 0 ? quickActions : FALLBACK_ACTIONS;
  const shortId = useMemo(
    () => (sessionId ? sessionId.replace(/-/g, "").slice(0, 4).toUpperCase() : ""),
    [sessionId],
  );
  const idleHint = practiceOn
    ? captures.rep
      ? "PRESS START, THE CLIENT TALKS FIRST"
      : "PRESS START. THE BROWSER WILL ASK TO USE YOUR MIC, SAY ALLOW"
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

        <Teleprompter
          text={suggestion}
          streaming={streaming}
          trigger={trigger}
          sourceText={sourceText}
          idleHint={idleHint}
          status={status}
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
          <WaveVisualizer levels={levels} speaking={speaking} active={captures} muted={muted} />
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

      {/* The second half of the shield above, and the honest one: while the
          score card is up the chips are not usable, so they are drawn as not
          usable and their own digit guard turns the key down as well. */}
      <div style={{ gridArea: "chips" }} className="min-h-0 min-w-0">
        <ObjectionBar
          actions={actions}
          disabled={!socketOpen || debriefOpen}
          activeKey={activeKey}
          onFire={fireAction}
        />
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
          BUILD CONTEXT
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
