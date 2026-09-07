"use client";

/**
 * The two ways to call one business, asked right under the row that was pressed.
 *
 * Pressing Call used to dial straight away. Now it asks, because the same lead
 * is worth two different things: ringing it, and rehearsing against it first.
 * Both are built from the same audit, so the robot knows exactly what the real
 * business looks like, what is wrong with their website and what the job is
 * worth. Practising against a made up client and then ringing a real one is
 * practice for the wrong call.
 *
 * It opens under the pressed row and not in a panel at the top of the page, for
 * the same reason the call failure sentence does: at row forty, the top of the
 * page is off screen, and a rep who presses Call and sees nothing move presses
 * it again. The question has to appear under their finger.
 *
 * The keys are the whole point of the layout. One press opens it, one key
 * chooses, Enter starts. A rep ringing sixty businesses in a morning must not
 * pay two clicks for the answer they give fifty nine times, so Enter alone runs
 * a real call and the shortcut row says so out loud.
 */

import { useCallback, useEffect, useRef } from "react";
import { GraduationCap, Phone, X } from "lucide-react";

import { DifficultyPicker } from "@/components/DifficultyPicker";
import { DIFFICULTIES } from "@/lib/config";
import type { CallMode, Difficulty } from "@/lib/types";

export interface CallChoiceProps {
  /** The business being called, shown so the rep is sure which row this is. */
  leadName: string;
  /** True when this lead has no number. Practice still works, ringing does not. */
  canRing: boolean;
  mode: CallMode;
  difficulty: Difficulty;
  onMode(mode: CallMode): void;
  onDifficulty(level: Difficulty): void;
  /** Go. The page builds the context and moves to the teleprompter. */
  onStart(): void;
  onCancel(): void;
  /** True while the context is being built. Everything goes quiet, nothing moves. */
  busy: boolean;
}

interface ModeChoice {
  mode: CallMode;
  label: string;
  hint: string;
  key: string;
  Icon: typeof Phone;
}

/* The same two words, the same order and the same icons as the setup page. A
   rep who has seen this question once has seen it everywhere it is asked. */
const MODES: ModeChoice[] = [
  {
    mode: "live",
    label: "Real call",
    hint: "You ring them. The copilot writes your next line.",
    key: "1",
    Icon: Phone,
  },
  {
    mode: "practice",
    label: "Practice call",
    hint: "A robot plays this business. Nobody is rung.",
    key: "2",
    Icon: GraduationCap,
  },
];

export function CallChoice({
  leadName,
  canRing,
  mode,
  difficulty,
  onMode,
  onDifficulty,
  onStart,
  onCancel,
  busy,
}: CallChoiceProps) {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const startRef = useRef<HTMLButtonElement | null>(null);

  /* Focus lands on the button that runs the real call, because that is what the
     rep wants almost every time. Enter is then already aimed at it and the two
     digits are still live, so no choice costs more than one key. */
  useEffect(() => {
    startRef.current?.focus();
  }, []);

  const practice = mode === "practice";

  const onKey = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (busy) return;
      if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;

      if (event.key === "Escape") {
        event.preventDefault();
        onCancel();
        return;
      }
      /* The digits are dead while a field has focus. There is no text field in
         here today, but the difficulty picker is a real group of controls and
         this panel is the kind of thing that grows a notes box later. */
      const el = event.target as HTMLElement | null;
      const tag = typeof el?.tagName === "string" ? el.tagName.toLowerCase() : "";
      if (tag === "input" || tag === "textarea" || el?.isContentEditable) return;

      if (event.key === "1") {
        event.preventDefault();
        onMode("live");
      } else if (event.key === "2") {
        event.preventDefault();
        onMode("practice");
      }
    },
    [busy, onCancel, onMode],
  );

  return (
    <div
      ref={boxRef}
      role="group"
      aria-label={`How to call ${leadName}`}
      onKeyDown={onKey}
      className="seam-grid grid-cols-1 border-l-2 border-accent"
    >
      <div className="flex flex-col gap-4 bg-surface-2 px-4 py-4">
        <div className="flex items-center justify-between gap-3">
          <span className="font-mono text-micro uppercase text-muted">
            How do you want to call {leadName}
          </span>
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="relative flex h-[26px] shrink-0 items-center gap-1.5 rounded-hair border border-line-strong px-2.5 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] ease-out before:absolute before:-inset-[9px] before:content-[''] hover:bg-surface hover:text-text disabled:opacity-40"
          >
            <X aria-hidden="true" className="h-3 w-3" />
            Close
          </button>
        </div>

        <div className="seam-grid grid-cols-1 sm:grid-cols-2">
          {MODES.map((choice) => {
            const on = choice.mode === mode;
            /* A lead with no number can still be rehearsed against. The real
               call cell is the one that goes quiet, and it says why, rather
               than the whole question refusing to open. */
            const dead = choice.mode === "live" && !canRing;
            return (
              <button
                key={choice.mode}
                type="button"
                aria-pressed={on}
                disabled={busy || dead}
                onClick={() => onMode(choice.mode)}
                className={`relative flex items-center gap-3 px-4 py-3 text-left transition-colors duration-[120ms] ease-out ${
                  dead
                    ? "cursor-not-allowed bg-surface opacity-40"
                    : on
                      ? "bg-surface-2"
                      : "bg-surface hover:bg-surface-2"
                }`}
              >
                {on && !dead ? (
                  <span
                    aria-hidden="true"
                    className="pointer-events-none absolute inset-y-0 left-0 w-0.5 bg-accent"
                  />
                ) : null}
                <choice.Icon
                  aria-hidden="true"
                  className={`h-4 w-4 shrink-0 ${on && !dead ? "text-accent" : "text-dim"}`}
                />
                <span className="flex min-w-0 flex-col gap-0.5">
                  <span className={`text-body ${on && !dead ? "text-text" : "text-muted"}`}>
                    {choice.label}
                  </span>
                  <span className="font-mono text-micro uppercase text-dim">
                    {dead ? "No number for this one" : choice.hint}
                  </span>
                </span>
                <span className="ml-auto shrink-0 font-mono text-micro text-dim">{choice.key}</span>
              </button>
            );
          })}
        </div>

        {practice ? (
          <div className="flex flex-col gap-2">
            <span className="font-mono text-micro uppercase text-muted">How hard is the robot</span>
            <DifficultyPicker
              levels={DIFFICULTIES}
              value={difficulty}
              onChange={onDifficulty}
              disabled={busy}
            />
          </div>
        ) : null}

        <div className="flex flex-wrap items-center gap-3">
          <button
            ref={startRef}
            type="button"
            onClick={onStart}
            disabled={busy || (!practice && !canRing)}
            className="relative flex h-[34px] items-center gap-2 rounded-hair bg-accent px-4 font-mono text-micro font-semibold uppercase text-bg transition-opacity duration-[120ms] ease-out hover:opacity-90 disabled:opacity-40"
          >
            {practice ? (
              <GraduationCap aria-hidden="true" className="h-3.5 w-3.5" />
            ) : (
              <Phone aria-hidden="true" className="h-3.5 w-3.5" />
            )}
            {busy
              ? practice
                ? "Building the robot"
                : "Getting your notes"
              : practice
                ? "Start practice"
                : "Call them"}
          </button>
          <span className="font-mono text-micro uppercase text-dim">
            1 real, 2 practice, Enter starts, Esc closes
          </span>
        </div>
      </div>
    </div>
  );
}
