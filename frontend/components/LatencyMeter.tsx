"use client";

import { useEffect, useRef, useState } from "react";

export interface LatencyMeterProps {
  stt: number | null;
  firstToken: number | null;
  total: number | null;
  rtt: number | null;
}

/** How long a value takes to travel to its new reading. Linear, never a spring. */
const COUNT_MS = 280;

/** Budgets, in milliseconds. The bar is the ratio of the value to its budget. */
const BUDGET_STT = 900;
const BUDGET_FIRST = 1200;
const BUDGET_TOTAL = 2500;

/**
 * Reads the reduced motion preference once and subscribes to changes. Server
 * render and first client render agree on `false`, so there is no hydration
 * mismatch, and the effect corrects it before the first value ever arrives.
 */
function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    // matchMedia does not exist during the prerender, so the first read has to
    // happen after mount. Subscribing to an external system is exactly what an
    // effect is for, the rule cannot see that.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setReduced(mq.matches);
    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  return reduced;
}

/**
 * Counts from whatever is currently on screen to the new reading over `ms`,
 * linearly, in integer steps, on requestAnimationFrame. Cancelled on unmount and
 * on every new target. Skipped entirely when reduced motion is set: a
 * measurement that crawls is worse than one that lands.
 */
function useCountUp(target: number | null, ms: number, instant: boolean): number | null {
  const [shown, setShown] = useState<number | null>(target);
  const displayRef = useRef<number>(target ?? 0);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }

    if (target === null) {
      displayRef.current = 0;
      // Resetting the animated readout when the measurement disappears. There
      // is no render path that can express this, the value is driven by rAF.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setShown(null);
      return;
    }

    if (instant || ms <= 0 || displayRef.current === target) {
      displayRef.current = target;
      setShown(target);
      return;
    }

    const from = displayRef.current;
    const distance = target - from;
    let start: number | null = null;

    const step = (now: number) => {
      if (start === null) start = now;
      const progress = Math.min(1, (now - start) / ms);
      const next = Math.round(from + distance * progress);
      displayRef.current = next;
      setShown(next);
      if (progress < 1) {
        rafRef.current = requestAnimationFrame(step);
      } else {
        rafRef.current = null;
      }
    };

    rafRef.current = requestAnimationFrame(step);

    return () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [target, ms, instant]);

  return shown;
}

/** Under 0.6 of budget is fine, up to 1.0 is a warning, past it is a problem. */
function fillClass(ratio: number): string {
  if (ratio > 1) return "bg-danger";
  if (ratio >= 0.6) return "bg-warn";
  return "bg-ok";
}

function rttDotClass(rtt: number | null): string {
  if (rtt === null) return "bg-dim";
  if (rtt >= 800) return "bg-danger";
  if (rtt >= 300) return "bg-warn";
  return "bg-ok";
}

interface ReadoutProps {
  label: string;
  spoken: string;
  value: number | null;
  budget: number;
  reduced: boolean;
  /**
   * The display utility for this column, passed in rather than baked in so a
   * single element never carries two conflicting display classes.
   */
  display: string;
}

/**
 * One column: label, value, unit, budget bar. The numeral is always `--text`,
 * never a threshold colour. Only the bar carries hue, because colouring the
 * normal case destroys the meaning of colour everywhere else in the product.
 */
function Readout({ label, spoken, value, budget, reduced, display }: ReadoutProps) {
  const shown = useCountUp(value, COUNT_MS, reduced);
  const ratio = value === null ? 0 : value / budget;
  const width = value === null ? 0 : Math.min(100, Math.round(ratio * 100));

  return (
    <div
      role="group"
      aria-label={
        value === null
          ? `${spoken}, no reading yet`
          : `${spoken}, ${value} milliseconds`
      }
      className={`w-[56px] shrink-0 flex-col gap-1 ${display}`}
    >
      <span aria-hidden="true" className="font-mono text-micro uppercase text-muted">
        {label}
      </span>

      <div aria-hidden="true" className="flex items-baseline gap-1">
        {shown === null ? (
          <span className="mb-1 flex min-w-[4ch] shrink-0 justify-end">
            <span className="h-px w-2.5 bg-dim" />
          </span>
        ) : (
          <span className="min-w-[4ch] shrink-0 text-right font-mono text-value tabnum text-text">
            {shown}
          </span>
        )}
        <span className="shrink-0 font-mono text-micro font-normal tracking-[0.06em] text-dim">
          ms
        </span>
      </div>

      <div aria-hidden="true" className="h-[3px] w-10 bg-line-strong">
        <div
          className={`h-full transition-[width] duration-[220ms] ease-out ${fillClass(ratio)}`}
          style={{ width: `${width}%` }}
        />
      </div>
    </div>
  );
}

/**
 * The one question this answers in 200 ms: is the machine keeping up, or am I
 * about to sit in silence.
 *
 * Three fixed width columns so digits never reflow, plus the socket round trip as
 * a small round dot. Round, because the status mark two inches to its left is a
 * square and the two must never be confused at a glance.
 *
 * Below 1024 only the round trip dot and the total render. The other two are
 * diagnostics, and the phone user is standing up.
 */
export function LatencyMeter({ stt, firstToken, total, rtt }: LatencyMeterProps) {
  const reduced = usePrefersReducedMotion();

  return (
    <div role="group" aria-label="Latency" className="flex items-end gap-3">
      <span
        title={rtt === null ? "Round trip: no reading" : `Round trip ${rtt} ms`}
        aria-label={
          rtt === null
            ? "Round trip, no reading yet"
            : `Round trip, ${rtt} milliseconds`
        }
        role="img"
        className={`mb-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${rttDotClass(rtt)}`}
      />

      <Readout
        label="STT"
        spoken="Speech to text"
        value={stt}
        budget={BUDGET_STT}
        reduced={reduced}
        display="hidden lg:flex"
      />
      <Readout
        label="1ST"
        spoken="First token"
        value={firstToken}
        budget={BUDGET_FIRST}
        reduced={reduced}
        display="hidden lg:flex"
      />
      <Readout
        label="TOT"
        spoken="Total"
        value={total}
        budget={BUDGET_TOTAL}
        reduced={reduced}
        display="flex"
      />
    </div>
  );
}
