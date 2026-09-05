"use client";

/**
 * The three practice levels, on the setup page.
 *
 * One question, answered before the rep commits: how hard do I want this client
 * to be. Three cards, one row on a desktop, stacked on a phone, in the frozen
 * backend order (warm, normal, brutal). Nothing here reorders and nothing hides.
 *
 * These are real radios, not buttons that look chosen. The wrapper is a
 * radiogroup, each card is a radio with aria-checked, exactly one card is in the
 * tab order at a time, and the arrow keys move the choice between them. A rep
 * who never touches the mouse gets to the level they want with two keys.
 *
 * The cards sit in a `.seam-grid`, so the 1px gap between them IS the hairline
 * and no card owns a border. Same construction as every other card grid on the
 * setup page, see DESIGN.md section 5.9.
 */

import { useId, useRef } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import type { LucideIcon } from "lucide-react";
import { Flame, Meh, Smile } from "lucide-react";

import type { Difficulty, DifficultyInfo } from "@/lib/types";

export interface DifficultyPickerProps {
  /** The three levels, from GET /api/practice/difficulties, in backend order. */
  levels: DifficultyInfo[];
  /** The key that is chosen right now. */
  value: Difficulty;
  onChange(v: Difficulty): void;
  /** True while the form is busy. The whole group goes quiet, not just the click. */
  disabled?: boolean;
}

/**
 * One face per level, so the level is readable before a single word is.
 * A switch rather than a lookup object, so an unknown key from the server can
 * never render a card with no icon.
 */
function iconForLevel(key: Difficulty): LucideIcon {
  switch (key) {
    case "warm":
      return Smile;
    case "brutal":
      return Flame;
    default:
      return Meh;
  }
}

export function DifficultyPicker({
  levels,
  value,
  onChange,
  disabled = false,
}: DifficultyPickerProps) {
  const groupId = useId();
  const buttonsRef = useRef<Array<HTMLButtonElement | null>>([]);

  /* Which card holds the group's one tab stop. A value that matches no level is
     bad data, not a reason to make the whole group unreachable by keyboard, so
     the stop falls back to the first card. */
  const selectedIndex = levels.findIndex((level) => level.key === value);
  const tabStop = selectedIndex >= 0 ? selectedIndex : 0;

  /* An empty group is not an accessibility problem worth shipping: a radiogroup
     with no radios in it reads as a broken control. The setup page owns the
     fallback copy for a levels fetch that failed. */
  if (levels.length === 0) return null;

  function move(index: number) {
    const level = levels[index];
    if (!level) return;
    onChange(level.key);
    /* Selection follows focus, which is the standard radio group behaviour, so
       the two have to move together on the same key. */
    buttonsRef.current[index]?.focus();
  }

  function onKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.ctrlKey || event.metaKey || event.altKey) return;

    let next = -1;
    switch (event.key) {
      case "ArrowRight":
      case "ArrowDown":
        next = (index + 1) % levels.length;
        break;
      case "ArrowLeft":
      case "ArrowUp":
        next = (index - 1 + levels.length) % levels.length;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = levels.length - 1;
        break;
      default:
        return;
    }

    event.preventDefault();
    move(next);
  }

  return (
    <div
      role="radiogroup"
      aria-label="How hard the practice client is"
      className="seam-grid grid-cols-1 sm:grid-cols-3"
    >
      {levels.map((level, index) => {
        const Icon = iconForLevel(level.key);
        const selected = level.key === value;
        const blurbId = `${groupId}-blurb-${level.key}`;

        return (
          <button
            key={level.key}
            type="button"
            role="radio"
            aria-checked={selected}
            aria-label={level.label}
            aria-describedby={blurbId}
            /* Roving tab index. One stop for the whole group, then arrows. */
            tabIndex={index === tabStop ? 0 : -1}
            disabled={disabled}
            aria-disabled={disabled || undefined}
            ref={(node) => {
              buttonsRef.current[index] = node;
            }}
            onClick={() => onChange(level.key)}
            onKeyDown={(event) => onKeyDown(event, index)}
            className={[
              "group relative flex flex-col gap-2 p-4 text-left",
              "transition-colors duration-[120ms] ease-out focus-visible:z-10",
              // pointer-events-none is what actually kills the hover treatment on
              // a disabled card. :hover still matches a disabled button.
              "disabled:cursor-not-allowed disabled:pointer-events-none disabled:opacity-[.38]",
              selected ? "bg-surface-2" : "bg-surface hover:bg-surface-2",
            ].join(" ")}
          >
            {/* The chosen mark, in the card's own left gutter. Same 2px accent
                bar the teleprompter uses for the boresight, so a mark in a
                gutter means the same thing everywhere in the product. */}
            {selected ? (
              <span aria-hidden="true" className="absolute inset-y-0 left-0 w-0.5 bg-accent" />
            ) : null}

            <span className="flex items-center gap-2">
              <Icon
                className={[
                  "h-4 w-4 shrink-0 transition-colors duration-[120ms]",
                  selected ? "text-accent" : "text-muted",
                  "group-hover:text-accent",
                ].join(" ")}
                aria-hidden="true"
              />
              <span className="truncate font-sans text-chip text-text">{level.label}</span>
              {/* A 6px square, hollow or filled. The same mark the status pill
                  uses, so "this one is on" has one shape in this product. */}
              <span
                aria-hidden="true"
                className={`ml-auto h-1.5 w-1.5 shrink-0 ${
                  selected ? "bg-accent" : "border border-line-strong"
                }`}
              />
            </span>

            <span id={blurbId} className="font-sans text-[12px] leading-[18px] text-muted">
              {level.blurb}
            </span>
          </button>
        );
      })}
    </div>
  );
}
