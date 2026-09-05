"use client";

/**
 * The practice control strip, one row above the objection bar.
 *
 * One question, answered in 200 ms: whose turn is it, and how do I take the turn
 * back. Everything else on the strip is a number the rep glances at between
 * lines: how long the call has run and how many turns it has taken.
 *
 * The strip has three shapes and never more:
 *
 *   before  one Start button and one line saying what is about to happen
 *   during  the level, the clock, the turn count, Cut in, End and score me
 *   after   the same numbers, frozen, and a line saying the score is coming
 *
 * Cut in is the whole reason this bar exists. While the client is talking the
 * rep's microphone is not sent, so the only way back into the call is this
 * button or the space bar. Interrupting is a real cold call skill, so it is a
 * feature, see CONTRACT_PRACTICE.md section 1.
 *
 * Motion here is one breathing 6px mark, which is the product's existing signal
 * for "this is live". Under prefers-reduced-motion the global rule in
 * globals.css holds it at a fixed opacity, so the state is still readable and
 * nothing moves. No other animation is used on this strip.
 */

import { useCallback, useEffect, useRef } from "react";
import { Hand, PhoneOff, Play } from "lucide-react";

import type { Difficulty } from "@/lib/types";

export interface PracticeBarProps {
  /** True once the client has spoken its opening line. */
  started: boolean;
  /** True once the server sent practice_over. The numbers freeze. */
  over: boolean;
  /** How many turns the pair has taken so far. */
  turns: number;
  /** How long the call has run, in milliseconds. The page owns the tick. */
  elapsedMs: number;
  difficulty: Difficulty;
  /** True while the browser is speaking the client's line out loud. */
  clientSpeaking: boolean;
  /** True while a request to the server is in flight. */
  busy: boolean;
  onStart(): void;
  onCutIn(): void;
  onEnd(): void;
}

const DIFFICULTY_WORD: Record<Difficulty, string> = {
  warm: "Warm",
  normal: "Normal",
  brutal: "Brutal",
};

/** The call page button. A hairline box, never a filled one: the one saturated
    fill in this product lives on the setup page, see DESIGN.md section 5.9. */
const BUTTON = [
  "flex h-9 shrink-0 items-center gap-2 rounded-hair border px-3",
  "font-mono text-[12px] font-semibold uppercase tracking-[0.12em]",
  "transition-colors duration-[120ms] ease-out",
  "disabled:cursor-not-allowed disabled:pointer-events-none disabled:opacity-[.38]",
].join(" ");

const COLUMN_LABEL = "font-mono text-micro uppercase text-muted";
const COLUMN_VALUE = "tabnum font-mono text-value text-text";
/** Hints are for a keyboard, so they are kept at 1024 and up and dropped below. */
const HINT = "hidden font-mono text-micro font-normal uppercase tracking-[0.10em] text-muted lg:block";

/** Minutes and seconds, always two digits each, so the strip never reflows. */
function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function isEditable(node: unknown): boolean {
  if (!node || typeof node !== "object") return false;
  const el = node as HTMLElement;
  const tag = typeof el.tagName === "string" ? el.tagName.toLowerCase() : "";
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  return el.isContentEditable === true;
}

export function PracticeBar({
  started,
  over,
  turns,
  elapsedMs,
  difficulty,
  clientSpeaking,
  busy,
  onStart,
  onCutIn,
  onEnd,
}: PracticeBarProps) {
  const live = started && !over;
  const canCutIn = live && clientSpeaking && !busy;

  const canCutInRef = useRef(canCutIn);
  const cutInRef = useRef(onCutIn);

  /* Written after the commit, never during render, and with no dependency array
     on purpose: the window listener below is mounted once and must always see
     the newest props, and a render React throws away must not be able to leave
     a value behind in a ref. Same pattern as ObjectionBar. */
  useEffect(() => {
    canCutInRef.current = canCutIn;
    cutInRef.current = onCutIn;
  });

  /* The space bar is the real affordance. The button is there so a phone and a
     mouse can do it too, and so the key is discoverable.

     Dead while a field has focus, dead while any modifier is held, dead on key
     repeat, and dead whenever the client is not talking. preventDefault only
     runs when we actually take the key, so space still presses a focused button
     the rest of the time. */
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
      if (event.repeat) return;
      if (event.key !== " " && event.code !== "Space") return;
      if (isEditable(event.target) || isEditable(document.activeElement)) return;
      if (!canCutInRef.current) return;

      event.preventDefault();
      cutInRef.current();
    }

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const handleCutIn = useCallback(() => {
    if (!canCutIn) return;
    onCutIn();
  }, [canCutIn, onCutIn]);

  /* One sentence for the middle of the strip, plus the mark that goes with it.
     The mark vocabulary is the status pill's: hollow means nothing is happening,
     solid means it is, and a breathing solid accent mark means live audio. */
  let markClass = "border border-line-strong";
  let line = "The client talks first. Read the line on the screen out loud.";
  let lineClass = "text-muted";
  let hint = "";

  if (over) {
    markClass = "bg-dim";
    line = "This call is over.";
    hint = "Your score is coming";
  } else if (clientSpeaking) {
    markClass = "bg-accent animate-breathe";
    line = "The client is talking.";
    lineClass = "text-text";
    /* The one line that tells a rep how to get the microphone back, so it says
       it in plain words. The button beside it keeps its short label. */
    hint = "Your mic is off while they talk. Press space to talk now";
  } else if (started) {
    markClass = "bg-accent-2";
    line = "Your turn. Say your line now.";
    lineClass = "text-text";
    hint = "Your mic is on";
  }

  return (
    <div
      role="group"
      aria-label="Practice call controls"
      className="flex h-full min-h-[52px] w-full items-center gap-3 overflow-x-auto overflow-y-hidden bg-surface px-3 lg:gap-4 lg:px-4"
    >
      {/* LEVEL */}
      <span className="flex h-[26px] shrink-0 items-center gap-2.5 rounded-hair border border-line-strong px-2.5">
        <span className="font-mono text-micro uppercase text-muted">Practice</span>
        <span aria-hidden="true" className="h-3.5 w-px bg-line-strong" />
        <span className="font-mono text-status uppercase text-text">
          {DIFFICULTY_WORD[difficulty]}
        </span>
      </span>

      {/* NUMBERS. Only once the call is running, so the strip before the call is
          one button and one sentence and nothing else to read. */}
      {started ? (
        <>
          <span aria-hidden="true" className="h-6 w-px shrink-0 bg-line-strong" />

          <span className="flex shrink-0 flex-col gap-1">
            <span className={COLUMN_LABEL}>Time</span>
            <span className={COLUMN_VALUE}>{formatClock(elapsedMs)}</span>
          </span>

          <span className="flex shrink-0 flex-col gap-1">
            <span className={COLUMN_LABEL}>Turns</span>
            <span className={COLUMN_VALUE}>{turns}</span>
          </span>
        </>
      ) : null}

      {/* WHOSE TURN IT IS */}
      <span className="flex min-w-0 flex-1 items-center gap-2.5">
        <span aria-hidden="true" className={`h-1.5 w-1.5 shrink-0 ${markClass}`} />
        <span className="flex min-w-0 flex-col gap-0.5">
          <span className={`truncate font-sans text-body ${lineClass}`}>{line}</span>
          {hint ? <span className={`truncate ${HINT}`}>{hint}</span> : null}
        </span>
      </span>

      {/* The turn change is the one thing a screen reader user cannot see, and
          it decides whether their microphone is being sent. */}
      <span role="status" aria-live="polite" className="sr-only">
        {over
          ? "The practice call is over."
          : clientSpeaking
            ? "The client is talking. Your microphone is off. Press space to talk now."
            : started
              ? "Your turn. Your microphone is on."
              : ""}
      </span>

      {/* ACTIONS */}
      {!started ? (
        <button
          type="button"
          disabled={busy}
          onClick={onStart}
          className={`${BUTTON} border-accent text-accent hover:bg-surface-2`}
        >
          <Play className="h-3.5 w-3.5" aria-hidden="true" />
          <span>Start practice call</span>
        </button>
      ) : null}

      {live ? (
        <>
          <button
            type="button"
            disabled={!canCutIn}
            aria-keyshortcuts="Space"
            title="Cut in and talk now (space bar)"
            onClick={handleCutIn}
            className={`${BUTTON} ${
              canCutIn
                ? "border-accent bg-surface-2 text-accent"
                : "border-line-strong text-muted"
            }`}
          >
            <Hand className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Cut in</span>
          </button>

          <button
            type="button"
            disabled={busy}
            onClick={onEnd}
            className={`${BUTTON} border-line-strong text-danger hover:bg-surface-2`}
          >
            <PhoneOff className="h-3.5 w-3.5" aria-hidden="true" />
            <span className="hidden sm:inline">End and score me</span>
            <span className="sm:hidden">End</span>
          </button>
        </>
      ) : null}
    </div>
  );
}
