"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Sparkles, ArrowDown } from "lucide-react";
import type { TranscriptItem } from "@/lib/types";

export interface TranscriptRailProps {
  items: TranscriptItem[];
  className?: string;
}

/*
 * The one question this rail answers: what did they just say, and did the machine
 * hear it right. It is explicitly not for glancing at during a read, so nothing in
 * here slides and only the prospect's own words sit at full ink. Everything else is
 * --muted, because the only line worth re reading mid call is theirs.
 */

/** Auto scroll sticks to the bottom while the user is within this many pixels of it. */
const STICK_PX = 24;

/**
 * STT latency is printed only when it is bad. A number that is always on screen is
 * read never, a number that appears only when something is wrong is read instantly.
 */
const SLOW_STT_MS = 900;

/**
 * Built once, at module scope. Building an Intl.DateTimeFormat per row per render is
 * one of the more expensive things you can do in a list that updates on every
 * utterance. Fixed locale and h23 so the format is stable everywhere.
 */
const CLOCK = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

const TAG: Record<TranscriptItem["role"], string> = {
  client: "Client",
  rep: "You",
  copilot: "Copilot",
};

const TAG_COLOUR: Record<TranscriptItem["role"], string> = {
  client: "text-accent",
  rep: "text-muted",
  copilot: "text-accent-2",
};

const SPINE_COLOUR: Record<TranscriptItem["role"], string> = {
  client: "bg-accent",
  rep: "bg-accent-2/55",
  copilot: "bg-accent-2",
};

const BODY_COLOUR: Record<TranscriptItem["role"], string> = {
  client: "text-text",
  rep: "text-muted",
  copilot: "text-muted",
};

function clockOf(ts: number): string {
  const d = new Date(ts);
  return Number.isFinite(d.getTime()) ? CLOCK.format(d) : "00:00:00";
}

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function TranscriptRail({ items, className }: TranscriptRailProps) {
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const stuckRef = useRef(true);
  const prevCountRef = useRef(0);
  const prevKeyRef = useRef<string | null>(null);
  const [stuck, setStuck] = useState(true);
  const [unseen, setUnseen] = useState(0);

  const count = items.length;
  const last = count > 0 ? items[count - 1] : null;
  const lastKey = last ? `${last.role}:${last.id}` : null;
  const lastText = last ? last.text : "";

  const scrollToLatest = useCallback((smooth: boolean) => {
    const el = bodyRef.current;
    if (!el) return;
    if (smooth) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    else el.scrollTop = el.scrollHeight;
    stuckRef.current = true;
    setStuck(true);
    setUnseen(0);
  }, []);

  const onScroll = useCallback(() => {
    const el = bodyRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const nowStuck = distance <= STICK_PX;
    if (nowStuck === stuckRef.current) return;
    stuckRef.current = nowStuck;
    setStuck(nowStuck);
    if (nowStuck) setUnseen(0);
  }, []);

  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;

    // The list is capped upstream at 60, so a trim can hold the length steady while
    // the content moves. Fall back to the last row's identity in that case.
    let arrived = count - prevCountRef.current;
    if (arrived <= 0 && lastKey !== null && lastKey !== prevKeyRef.current) arrived = 1;
    prevCountRef.current = count;
    prevKeyRef.current = lastKey;

    if (stuckRef.current) {
      // Pin on growth of any kind, including the last row getting longer.
      el.scrollTop = el.scrollHeight;
      setUnseen(0);
      return;
    }
    if (arrived > 0) setUnseen((n) => n + arrived);
  }, [count, lastKey, lastText]);

  return (
    <section
      aria-label="Call transcript"
      className={`relative flex min-h-0 min-w-0 flex-col bg-surface ${className ?? ""}`}
    >
      <div className="flex h-[30px] shrink-0 items-center justify-between border-b border-line px-3">
        <span className="font-mono text-micro uppercase text-muted">Call log</span>
        <span className="font-mono text-micro tabnum text-dim">{count}</span>
      </div>

      <div ref={bodyRef} onScroll={onScroll} className="rail-scroll min-h-0 flex-1 px-3 py-2">
        {count === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 px-4 text-center">
            <span className="font-mono text-micro uppercase text-dim">Nothing heard yet</span>
            <p className="max-w-[32ch] font-sans text-body text-muted">
              Every line the client says, every line you say, and every line the copilot writes
              lands here as it happens.
            </p>
          </div>
        ) : (
          items.map((item, i) => {
            const first = i === 0 || items[i - 1].role !== item.role;
            const newest = i === count - 1;
            const ms = typeof item.ms === "number" ? Math.round(item.ms) : null;
            const slow = ms !== null && ms > SLOW_STT_MS;
            return (
              <div
                key={`${item.role}:${item.id}`}
                className="animate-row-in grid grid-cols-[3px_1fr] gap-2.5 border-b border-line py-2 last:border-b-0"
              >
                <div
                  aria-hidden="true"
                  className={`w-[3px] rounded-hair ${SPINE_COLOUR[item.role]} ${newest ? "spine-flash" : ""}`}
                />
                <div className="min-w-0">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="flex min-w-0 items-center gap-1.5">
                      {first && item.role === "copilot" ? (
                        <Sparkles aria-hidden="true" className="h-3 w-3 shrink-0 text-dim" />
                      ) : null}
                      {first ? (
                        <span className={`font-mono text-micro uppercase ${TAG_COLOUR[item.role]}`}>
                          {TAG[item.role]}
                        </span>
                      ) : null}
                    </span>
                    <span className="flex shrink-0 items-baseline gap-2">
                      {slow ? (
                        <span
                          className="font-mono text-micro tabnum text-warn"
                          title={`Speech to text took ${ms} milliseconds`}
                        >
                          {ms}ms
                        </span>
                      ) : null}
                      <span className="font-mono text-micro tabnum font-normal tracking-[0.04em] text-dim">
                        {clockOf(item.ts)}
                      </span>
                    </span>
                  </div>
                  <p className={`mt-0.5 break-words font-sans text-body ${BODY_COLOUR[item.role]}`}>
                    {item.text}
                  </p>
                </div>
              </div>
            );
          })
        )}
      </div>

      {!stuck && unseen > 0 ? (
        <button
          type="button"
          onClick={() => scrollToLatest(!prefersReducedMotion())}
          aria-label={`Jump to the latest line, ${unseen} new`}
          className="animate-latest-in absolute bottom-2 left-1/2 flex h-[22px] -translate-x-1/2 items-center gap-1.5 rounded-hair border border-line-strong bg-surface-2 px-2.5 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] ease-out hover:text-text"
        >
          <ArrowDown aria-hidden="true" className="h-3 w-3" />
          <span>
            Latest <span className="tabnum">+{unseen}</span>
          </span>
        </button>
      ) : null}
    </section>
  );
}
