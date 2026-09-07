"use client";

/**
 * What happened on the call, asked the second the call ends.
 *
 * This is the loop a rep runs forty times a day, so it is two presses and
 * nothing more: pick what happened, press Next lead. Everything else on the row
 * is optional and stays out of the way.
 *
 * The four answers are the four the pipeline file understands, in the order a
 * call actually lands in: the good one first, then the two that are still alive,
 * then the one where nobody picked up. Their positions never change, so after
 * one call the rep hits the right one without reading it, exactly like the
 * objection matrix. Digits 1 to 4 do the same thing with the same guards: dead
 * while a field has focus, dead while a modifier is held, dead on key repeat.
 *
 * The lead's name is printed on the row on purpose. The rep is coming off a
 * call, the list behind them may have moved, and marking the wrong business is
 * a mistake that is never found again.
 *
 * The note is written to the same place as the answer. If it is typed after the
 * answer was picked, it is flushed on the way out through Next lead, so a note
 * can never be lost by a rep who typed it and moved on.
 *
 * SIZE. This row stands in the chips cell of the call grid, and that cell is a
 * fixed track: 113px wide screens, 105px from 1024 to 1439, and 56px below
 * 1024. So the row is built to fit those numbers instead of being centred and
 * spilling out of both ends of a box it cannot grow:
 *
 *   - Below 1024 it is one line that scrolls sideways, the same strip the
 *     objection matrix becomes at that width. 36px of controls inside 8px of
 *     padding is 52px, and the track is 56px.
 *   - From 1024 up the same items wrap onto two lines, 40 plus 12 plus 36 inside
 *     8px of padding, which is 104px, and the smaller of the two tracks is 105.
 *   - Nothing is centred with justify-content or align-content set to center,
 *     because a centred flex box pushes anything too tall out of BOTH ends and
 *     the part above the top can never be scrolled back to.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowRight, Ban, Check, Clock, PhoneOff, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { STATUS_LABELS } from "@/lib/leads";
import type { LeadStatus } from "@/lib/leads";

export interface CallOutcomeProps {
  /** The business that was just called. Printed, so the wrong one cannot be marked. */
  leadName: string;
  /** True while the answer is being written to the pipeline file. */
  saving: boolean;
  /** One plain sentence from the write, or null. */
  error: string | null;
  /** Save what happened, plus whatever note has been typed so far. */
  onPick(status: LeadStatus, notes: string): void;
  /** Open the next lead in this search that has not been called. */
  onNext(): void;
  /** Move on without saving anything at all. */
  onSkip(): void;
  /** False when this was the last uncalled lead in the search. */
  hasNext: boolean;
}

/** How long the keyboard press styling is held, so a key hit looks like a finger hit. */
const PRESS_MS = 120;

interface Choice {
  status: LeadStatus;
  digit: string;
  Icon: LucideIcon;
}

/**
 * The four answers, frozen in this order.
 *
 * The words come from STATUS_LABELS, so the row, the list and the drawer can
 * never call the same outcome by two different names.
 */
const CHOICES: readonly Choice[] = [
  { status: "interested", digit: "1", Icon: Check },
  { status: "callback", digit: "2", Icon: Clock },
  { status: "not_interested", digit: "3", Icon: Ban },
  { status: "no_answer", digit: "4", Icon: PhoneOff },
];

/**
 * One answer button.
 *
 * Below 1024 it holds its own width inside the sideways strip. From 1024 up the
 * four of them share the middle of the line and shrink together, and they never
 * wrap onto a second line, because a second line would make the row taller than
 * the track it lives in.
 *
 * The focus ring goes inside the button below 1024. The strip clips on Y at that
 * width, so a ring drawn outside would be cut off on the top and bottom edges.
 * That is the same trick the objection matrix uses.
 */
const CHOICE_BUTTON = [
  "chip-press relative flex h-9 min-w-[128px] shrink-0 grow-0 items-center justify-center gap-2",
  "rounded-hair border px-3",
  "font-mono text-[12px] font-semibold uppercase tracking-[0.12em]",
  "transition-colors duration-[120ms] ease-out",
  "focus-visible:[outline-offset:-2px] lg:focus-visible:[outline-offset:2px]",
  "disabled:cursor-not-allowed disabled:pointer-events-none disabled:opacity-[.38]",
  "lg:h-10 lg:min-w-0 lg:shrink lg:grow lg:basis-[140px]",
].join(" ");

const ACTION_BUTTON = [
  "flex h-9 shrink-0 items-center gap-2 rounded-hair border px-3 lg:h-10",
  "font-mono text-[12px] font-semibold uppercase tracking-[0.12em]",
  "transition-colors duration-[120ms] ease-out",
  "focus-visible:[outline-offset:-2px] lg:focus-visible:[outline-offset:2px]",
  "disabled:cursor-not-allowed disabled:pointer-events-none disabled:opacity-[.38]",
].join(" ");

const LABEL = "font-mono text-micro uppercase text-muted";
const NOTE = "font-sans text-[12px] leading-[18px] text-muted";

function isEditable(node: unknown): boolean {
  if (!node || typeof node !== "object") return false;
  const el = node as HTMLElement;
  const tag = typeof el.tagName === "string" ? el.tagName.toLowerCase() : "";
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  return el.isContentEditable === true;
}

/**
 * The row itself, mounted fresh for each business.
 *
 * Every piece of state in here belongs to one call and one call only. The
 * wrapper below hands it a key, so a new business tears this down and builds it
 * again with empty state. That is React's own way of resetting a component when
 * a prop changes, and it is why there is no effect in here that wipes four
 * pieces of state after the fact.
 */
function OutcomeRow({
  leadName,
  saving,
  error,
  onPick,
  onNext,
  onSkip,
  hasNext,
}: CallOutcomeProps) {
  const [picked, setPicked] = useState<LeadStatus | null>(null);
  const [notes, setNotes] = useState("");
  /** The note text that was last handed to onPick, or null when none was. */
  const [sentNotes, setSentNotes] = useState<string | null>(null);
  const [pressed, setPressed] = useState<LeadStatus | null>(null);
  const [reduced, setReduced] = useState(false);

  const pickRef = useRef(onPick);
  const savingRef = useRef(saving);
  const notesRef = useRef(notes);
  const pressTimerRef = useRef(0);

  /* Written after the commit, never during render, and with no dependency array
     on purpose: the window key listener below is mounted once and must always
     see the newest props, and a render React throws away must not be able to
     leave a value behind in a ref. Same pattern as ObjectionBar. */
  useEffect(() => {
    pickRef.current = onPick;
    savingRef.current = saving;
    notesRef.current = notes;
  });

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const apply = () => setReduced(query.matches);
    apply();
    query.addEventListener("change", apply);
    return () => query.removeEventListener("change", apply);
  }, []);

  useEffect(() => () => window.clearTimeout(pressTimerRef.current), []);

  const flashPress = useCallback((status: LeadStatus) => {
    setPressed(status);
    window.clearTimeout(pressTimerRef.current);
    pressTimerRef.current = window.setTimeout(() => setPressed(null), PRESS_MS);
  }, []);

  /* One path for the pointer and for the digit key, so a keyboard hit looks
     exactly like a finger hit. Picking a second time is a correction and is
     allowed: it writes the new answer over the old one, and it carries whatever
     note has been typed since, which is the only way to save a late note on the
     last lead in a search. */
  const pick = useCallback(
    (status: LeadStatus) => {
      if (savingRef.current) return;
      const text = notesRef.current.trim();
      flashPress(status);
      setPicked(status);
      setSentNotes(text);
      pickRef.current(status, text);
    },
    [flashPress],
  );

  /* Digits 1 to 4, one listener on window. Dead while a field has focus, dead
     while a modifier is held, dead on key repeat, dead while a write is out. */
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;
      if (isEditable(event.target) || isEditable(document.activeElement)) return;

      const choice = CHOICES.find((c) => c.digit === event.key);
      if (!choice) return;
      if (savingRef.current) return;

      event.preventDefault();
      pick(choice.status);
    }

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pick]);

  /* A note typed after the answer was picked is still the rep's note, so it is
     written on the way out rather than dropped. */
  const goNext = useCallback(() => {
    const text = notes.trim();
    if (picked !== null && text !== (sentNotes ?? "")) {
      setSentNotes(text);
      onPick(picked, text);
    }
    onNext();
  }, [notes, onNext, onPick, picked, sentNotes]);

  const noteUnsaved = picked !== null && notes.trim() !== (sentNotes ?? "");

  /* One paragraph, never two. The old second line about a note that is not
     written yet is folded in here, because a second paragraph made the row
     taller than the track the grid gives it.

     On the last lead in a search there is no Next lead to press, so the note is
     saved by pressing the same answer again, and that is what it says. */
  let message = "Pick what happened. Keys 1 to 4 work too.";
  if (saving) {
    message = "Saving your answer.";
  } else if (picked !== null) {
    const head = `Saved. ${STATUS_LABELS[picked]}.`;
    if (noteUnsaved) {
      message = hasNext
        ? `${head} Your note goes in when you press Next lead.`
        : `${head} Press the same answer again to save your note.`;
    } else {
      message = hasNext ? `${head} Press Next lead.` : `${head} That was the last lead.`;
    }
  }

  return (
    <section
      aria-label={`What happened on the call with ${leadName}`}
      className={[
        "flex h-full min-h-0 w-full items-center gap-2 bg-surface px-3 py-2",
        // Below 1024 this is one sideways strip. From 1024 up it wraps onto two
        // lines and grows downward only, so anything too tall stays reachable.
        "overflow-x-auto overflow-y-hidden",
        "lg:flex-wrap lg:gap-x-4 lg:gap-y-1.5 lg:overflow-x-hidden lg:overflow-y-auto lg:px-4",
      ].join(" ")}
    >
      {/* WHO IT WAS. The label is dropped below 1024, where the strip is one
          line tall and the name alone is enough to stop a wrong mark. */}
      <span className="flex shrink-0 flex-col justify-center gap-0.5 lg:basis-[160px]">
        <span className={`hidden lg:block ${LABEL}`}>Call ended</span>
        <span
          className="max-w-[136px] truncate font-sans text-body text-text lg:max-w-none"
          title={leadName}
        >
          {leadName}
        </span>
      </span>

      {/* THE FOUR ANSWERS. Frozen order, one line at every width. */}
      <div
        role="group"
        aria-label="What happened"
        className="flex shrink-0 items-center gap-2 lg:min-w-0 lg:shrink lg:grow lg:basis-[320px]"
      >
        {CHOICES.map(({ status, digit, Icon }) => {
          const isPicked = picked === status;
          const isPressed = pressed === status;

          return (
            <button
              key={status}
              type="button"
              onClick={() => pick(status)}
              disabled={saving}
              aria-pressed={isPicked}
              aria-keyshortcuts={digit}
              title={`${STATUS_LABELS[status]} (key ${digit})`}
              className={[
                CHOICE_BUTTON,
                isPicked
                  ? "border-accent text-text"
                  : "border-line-strong text-muted hover:bg-surface-2 hover:text-text",
                isPicked || (isPressed && reduced) ? "bg-surface-2" : "bg-surface",
              ].join(" ")}
              style={
                isPressed && !reduced
                  ? { transform: "scale(.97)", boxShadow: "inset 0 0 0 1px var(--accent)" }
                  : undefined
              }
            >
              <Icon
                aria-hidden="true"
                className={`h-3.5 w-3.5 shrink-0 ${isPicked ? "text-accent" : ""}`}
              />
              <span className="truncate">{STATUS_LABELS[status]}</span>
              <span
                aria-hidden="true"
                className="tabnum absolute right-1.5 top-1 hidden font-mono text-micro text-dim lg:block"
              >
                {digit}
              </span>
            </button>
          );
        })}
      </div>

      {/* THE WAY OUT. */}
      <div className="flex shrink-0 items-center gap-2">
        <button
          type="button"
          onClick={goNext}
          disabled={picked === null || !hasNext}
          title={
            picked === null
              ? "Pick what happened first"
              : hasNext
                ? "Open the next lead in this search"
                : "There are no more leads to call in this search"
          }
          className={`${ACTION_BUTTON} ${
            picked !== null && hasNext
              ? "border-accent text-accent hover:bg-surface-2"
              : "border-line-strong text-muted"
          }`}
        >
          <ArrowRight aria-hidden="true" className="h-3.5 w-3.5" />
          <span>Next lead</span>
        </button>

        <button
          type="button"
          onClick={onSkip}
          title="Move on without saving an answer"
          className={`${ACTION_BUTTON} border-line-strong text-muted hover:bg-surface-2 hover:text-text`}
        >
          <span>Skip</span>
        </button>
      </div>

      {/* The line break from 1024 up. It paints nothing and is zero tall. Below
          1024 it is not there at all, so the strip stays a single line. */}
      <span aria-hidden="true" className="hidden shrink-0 lg:block lg:h-0 lg:w-full lg:basis-full" />

      {/* THE NOTE. There is no room for a label above it in a 105px track, so
          the field carries its own name for a screen reader and its own hint
          for everybody else. It cannot be dragged taller either, because the
          track it sits in cannot grow with it. */}
      <textarea
        rows={1}
        value={notes}
        onChange={(event) => setNotes(event.target.value)}
        aria-label="Note about this call"
        placeholder="Write a note if you want."
        className="h-9 w-[200px] shrink-0 resize-none rounded-hair border border-line-strong bg-surface-2 px-2 py-1.5 font-sans text-chip text-text placeholder:text-dim lg:w-auto lg:min-w-0 lg:shrink lg:grow lg:basis-[240px]"
      />

      {/* WHERE THINGS STAND.

          Two lines is all the strip is tall below 1024, so the words are cut at
          two lines there and run in full from 1024 up. Nothing is lost by the
          cut: both of these are live regions, so a screen reader is read the
          whole sentence either way, and the title carries it on a mouse. */}
      <div className="flex w-[300px] shrink-0 flex-col justify-center lg:w-auto lg:min-w-0 lg:shrink lg:grow lg:basis-[240px]">
        {error ? (
          /* The write failed, and the rep is about to move on to the next
             business. A live region is the only way that reaches somebody who
             is not looking at this corner of the screen. */
          <p role="alert" className="flex items-start gap-2">
            <TriangleAlert aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger" />
            <span
              title={error}
              className="line-clamp-2 font-sans text-[12px] leading-[18px] text-danger lg:line-clamp-none"
            >
              {error} Press the same button again to try one more time.
            </span>
          </p>
        ) : (
          <p
            role="status"
            aria-live="polite"
            title={message}
            className={`${NOTE} line-clamp-2 lg:line-clamp-none`}
          >
            {message}
          </p>
        )}
      </div>
    </section>
  );
}

/**
 * The outcome row for one business.
 *
 * All this does is hand the row a key. A new business is a new key, so React
 * throws the old row away and builds a fresh one, and the answer, the note and
 * the press styling from the last call cannot survive into the next one. That
 * matters: an answer left sitting under a new business's name is the exact
 * mistake this row exists to prevent.
 */
export function CallOutcome(props: CallOutcomeProps) {
  return <OutcomeRow key={props.leadName} {...props} />;
}
