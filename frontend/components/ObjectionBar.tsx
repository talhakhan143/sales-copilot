"use client";

/**
 * The objection matrix.
 *
 * One question, answered in 200 ms: where is the button for the thing they just
 * said. Position is the interface, so the matrix is a fixed 4 by 2 grid that
 * never reorders, never sorts by usage and never hides a chip. After one call the
 * rep hits the top left chip without looking.
 *
 * Below 1024 there is no room for two rows (the grid gives this bar 56 px), so
 * the same eight cells become one horizontally scroll snapped strip in the same
 * frozen order, with the hint and the digit removed because a phone has neither
 * hover nor a number row.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { iconFor } from "@/lib/icons";
import type { QuickAction } from "@/lib/types";

export interface ObjectionBarProps {
  /** The eight frozen actions, in their frozen order. */
  actions: QuickAction[];
  /** True while the socket is not usable. */
  disabled: boolean;
  /** The key of the chip that was fired most recently, or null. */
  activeKey: string | null;
  onFire(key: string): void;
}

/** How long the keyboard press styling is held, so a key hit looks like a finger hit. */
const PRESS_MS = 120;

const DIGITS = ["1", "2", "3", "4", "5", "6", "7", "8"];

function isEditable(node: unknown): boolean {
  if (!node || typeof node !== "object") return false;
  const el = node as HTMLElement;
  const tag = typeof el.tagName === "string" ? el.tagName.toLowerCase() : "";
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  return el.isContentEditable === true;
}

export function ObjectionBar({ actions, disabled, activeKey, onFire }: ObjectionBarProps) {
  const [pressed, setPressed] = useState<string | null>(null);
  const [drain, setDrain] = useState(0);
  const [reduced, setReduced] = useState(false);

  const actionsRef = useRef(actions);
  const disabledRef = useRef(disabled);
  const fireRef = useRef(onFire);
  const pressTimerRef = useRef(0);

  /* Written after the commit, never during render. No dependency array on
     purpose: the window key listener below is mounted once and must always see
     the newest props, and a render React throws away must not be able to leave
     a value behind in a ref. */
  useEffect(() => {
    actionsRef.current = actions;
    disabledRef.current = disabled;
    fireRef.current = onFire;
  });

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const apply = () => setReduced(query.matches);
    apply();
    query.addEventListener("change", apply);
    return () => query.removeEventListener("change", apply);
  }, []);

  const flashPress = useCallback((key: string) => {
    setPressed(key);
    window.clearTimeout(pressTimerRef.current);
    pressTimerRef.current = window.setTimeout(() => setPressed(null), PRESS_MS);
  }, []);

  /* One fire path for the pointer and the digit key, so a keyboard hit looks
     exactly like a finger hit. The drain counter is bumped here rather than in
     an effect on activeKey: firing the same chip twice inside the cooldown
     leaves activeKey unchanged, so only a counter can remount the bar and
     restart its animation, and a counter belongs in the event that caused it. */
  const fire = useCallback(
    (key: string) => {
      flashPress(key);
      setDrain((n) => n + 1);
      fireRef.current(key);
    },
    [flashPress],
  );

  useEffect(() => () => window.clearTimeout(pressTimerRef.current), []);

  /* One listener on window for the whole matrix. Dead while a field has focus,
     dead while a modifier is held, dead on key repeat. */
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;
      if (isEditable(event.target) || isEditable(document.activeElement)) return;

      const index = DIGITS.indexOf(event.key);
      if (index < 0) return;

      const action = actionsRef.current[index];
      if (!action || disabledRef.current) return;

      event.preventDefault();
      fire(action.key);
    }

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fire]);

  return (
    <div
      role="group"
      aria-label="Instant objection answers"
      className="flex h-full min-h-0 w-full snap-x snap-mandatory gap-px overflow-x-auto overflow-y-hidden scroll-pl-3 bg-line-strong lg:grid lg:grid-cols-4 lg:grid-rows-2 lg:overflow-visible"
    >
      {actions.map((action, index) => {
        const Icon = iconFor(action.icon);
        const isPressed = pressed === action.key;
        const isActive = activeKey === action.key;

        return (
          <button
            key={action.key}
            type="button"
            disabled={disabled}
            aria-disabled={disabled || undefined}
            aria-keyshortcuts={index < DIGITS.length ? DIGITS[index] : undefined}
            title={index < DIGITS.length ? `${action.label} (key ${DIGITS[index]})` : action.label}
            onPointerDown={() => flashPress(action.key)}
            onClick={() => fire(action.key)}
            className={[
              "chip-press group relative flex min-w-[132px] shrink-0 snap-start flex-col justify-center gap-1",
              "px-3 py-2.5 text-left transition-colors duration-[120ms] ease-out",
              "hover:bg-surface-2 focus-visible:z-10",
              // pointer-events-none is what actually kills the hover treatment on
              // a disabled chip. :hover still matches a disabled button, so
              // without this a dead chip lifts and turns its icon cyan on the one
              // screen where nothing can be fired.
              "disabled:cursor-not-allowed disabled:pointer-events-none disabled:opacity-[.38]",
              // Below lg the strip is a horizontal scroll container, so it clips
              // on Y and an outside ring would be invisible on the top and bottom
              // edges. Inside the cell there, outside the cell from lg up.
              "focus-visible:[outline-offset:-2px] lg:focus-visible:[outline-offset:2px]",
              "lg:min-w-0",
              isPressed && reduced ? "bg-surface-2" : "bg-surface",
            ].join(" ")}
            style={
              isPressed && !reduced
                ? { transform: "scale(.97)", boxShadow: "inset 0 0 0 1px var(--accent)" }
                : undefined
            }
          >
            <span className="flex items-center gap-2">
              <Icon
                className={[
                  "h-4 w-4 shrink-0 transition-colors duration-[120ms]",
                  isActive ? "text-accent" : "text-muted",
                  "group-hover:text-accent",
                ].join(" ")}
                aria-hidden="true"
              />
              <span className="truncate font-sans text-chip text-text">{action.label}</span>
            </span>

            <span className="hidden truncate font-mono text-micro font-normal uppercase tracking-[0.10em] text-muted lg:block">
              {action.hint}
            </span>

            {index < DIGITS.length ? (
              <span
                className="tabnum absolute right-1.5 top-1.5 hidden font-mono text-micro text-dim lg:block"
                aria-hidden="true"
              >
                {DIGITS[index]}
              </span>
            ) : null}

            {isActive ? <span key={drain} className="chip-drain" aria-hidden="true" /> : null}
          </button>
        );
      })}
    </div>
  );
}
