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
 */

import { Suspense, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ArrowLeft, Check, Copy, ScrollText, Send, X } from "lucide-react";

import { AudioSourcePicker } from "@/components/AudioSourcePicker";
import { LatencyMeter } from "@/components/LatencyMeter";
import { ObjectionBar } from "@/components/ObjectionBar";
import { StatusPill } from "@/components/StatusPill";
import { Teleprompter } from "@/components/Teleprompter";
import { TranscriptRail } from "@/components/TranscriptRail";
import { WaveVisualizer } from "@/components/WaveVisualizer";
import { useTeleprompter } from "@/lib/hooks/useTeleprompter";
import { loadSession } from "@/lib/session";
import type { PreparedSession, QuickAction, StreamKind } from "@/lib/types";

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

  const [stored, setStored] = useState<PreparedSession | null>(null);
  const [storedChecked, setStoredChecked] = useState(false);

  // localStorage does not exist during the prerender, so the restore cannot be a
  // lazy initial state without the server and the client disagreeing about which
  // screen to paint. Reading it once after mount is the pattern, and the two
  // setState calls are the whole point of the effect.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setStored(loadSession());
    setStoredChecked(true);
  }, []);

  const sessionId = fromUrl.length > 0 ? fromUrl : (stored?.sessionId ?? null);

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
    startClient,
    startRep,
    stopStream,
    toggleMute,
    quickAction,
    sendManual,
    setSensitivity,
    refreshDevices,
    reset,
  } = useTeleprompter(sessionId);

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

  /* ---------------- derived ---------------- */

  const socketOpen = connState === "open";
  const noAudio = !captures.client && !captures.rep;
  const showNudge = Boolean(sessionId) && socketOpen && noAudio && !nudgeOff;
  const sessionGone = status === "error" && SESSION_GONE.test(error ?? "");
  const actions = quickActions.length > 0 ? quickActions : FALLBACK_ACTIONS;
  const shortId = useMemo(
    () => (sessionId ? sessionId.replace(/-/g, "").slice(0, 4).toUpperCase() : ""),
    [sessionId],
  );
  const idleHint = noAudio
    ? "OPEN SOURCES ABOVE, OR TYPE A LINE IN THE LOG"
    : "PRESS 1 TO 8 FOR AN INSTANT LINE";

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
        <Teleprompter
          text={suggestion}
          streaming={streaming}
          trigger={trigger}
          sourceText={sourceText}
          idleHint={idleHint}
          status={status}
        />
      </div>

      <div style={{ gridArea: "wave" }} className="relative min-h-0 min-w-0 bg-surface">
        <WaveVisualizer levels={levels} speaking={speaking} active={captures} muted={muted} />
      </div>

      <div
        style={{ gridArea: "rail" }}
        className="hidden min-h-0 min-w-0 flex-col bg-surface lg:flex"
      >
        {error ? <FaultLine message={error} /> : null}
        <TranscriptRail items={transcript} className="min-h-0 flex-1" />
        <Composer disabled={!socketOpen} onSend={handleManual} onReset={handleReset} />
      </div>

      <div style={{ gridArea: "chips" }} className="min-h-0 min-w-0">
        <ObjectionBar
          actions={actions}
          disabled={!socketOpen}
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
              autoFocus
            />
          </div>
        </>
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
  disabled: boolean;
  onSend(text: string, stream: StreamKind): void;
  onReset(): void;
  autoFocus?: boolean;
}

/**
 * Type a line and the copilot answers it. This is the whole app working with
 * zero audio hardware, so it is a first class control and not a debug hatch.
 */
function Composer({ disabled, onSend, onReset, autoFocus }: ComposerProps) {
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
          : "ADDS CONTEXT ONLY, NO SUGGESTION"}
      </p>
    </form>
  );
}
