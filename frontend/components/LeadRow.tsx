"use client";

/**
 * One lead, one line.
 *
 * The rep reads this row left to right and decides in about a second: who they
 * are, where they are, how good they look, what the job is worth, and why this
 * one is worth the breath. Then they press Call. So the row carries exactly
 * those six things in that order and nothing else. Everything deeper (the proof
 * behind each fault, the hours, the links) lives in the drawer, one click away,
 * because putting it here would make the list unreadable at 65 rows.
 *
 * Two clickable things live in one row, so the row itself is not a button. A
 * full bleed transparent button sits under the content and opens the drawer, the
 * content above it does not take pointer events, and the one filled control,
 * Call, takes them back. That keeps the HTML valid (no button inside a button),
 * keeps both actions reachable from the keyboard, and means a press anywhere in
 * the dead space opens the detail, which is what a rep expects from a row.
 *
 * A lead with no phone number cannot be called. 7 of the 91 leads on disk are
 * like that. They are not hidden, because the rep may still want to look at
 * them, but their ink steps back and the reason is printed in words on the row
 * itself. It is NOT left to a tooltip: a browser never fires a tooltip on a
 * disabled button, so that cell is a plain span and the sentence is also the
 * first thing the why line says.
 */

import { Phone, PhoneOff, Star } from "lucide-react";

import { STATUS_LABELS, canCall, formatMoney } from "@/lib/leads";
import type { Lead, LeadStatus } from "@/lib/leads";

export interface LeadRowProps {
  /** The lead to draw. */
  lead: Lead;
  /** Build the call context for this lead and open the teleprompter. */
  onCall(): void;
  /** Open the detail drawer for this lead. */
  onOpen(): void;
  /** True while this row's call is being built. */
  calling: boolean;
}

/**
 * The colour of the status word beside the name.
 *
 * The colour law allows an exception hue as a single small uppercase word, and
 * this is that case. Everything with no verdict yet stays monochrome, so a win
 * and a loss are the only two things in the whole list the eye is pulled to.
 *
 * "new" is in the map for completeness. It is never drawn: a fresh lead is the
 * normal case, and a tag on every row is a tag the eye stops reading.
 */
const STATUS_TONE: Record<LeadStatus, string> = {
  new: "text-muted",
  interested: "text-ok",
  callback: "text-warn",
  no_answer: "text-muted",
  not_interested: "text-muted",
  won: "text-ok",
  lost: "text-danger",
};

/**
 * 40px tall, which is the smallest thing a thumb hits every time.
 *
 * This is the same height as the Call button in the drawer, so the one filled
 * control in this product is one size wherever it appears.
 */
const CALL_BUTTON =
  "relative flex h-10 w-[112px] shrink-0 items-center justify-center gap-1.5 overflow-hidden rounded-hair px-3 font-mono text-[12px] font-semibold uppercase tracking-[0.12em] transition-opacity duration-[120ms] ease-out";

/** Said on the row, in the drawer, and in the pill's own tooltip. */
const NO_PHONE = "Google has no phone number for this one.";

export function LeadRow({ lead, onCall, onOpen, calling }: LeadRowProps) {
  const callable = canCall(lead);
  const rated = lead.rating > 0;
  const called = typeof lead.calledAt === "string" && lead.calledAt.length > 0;
  const place = [lead.category, lead.city].filter((part) => part.trim().length > 0).join(", ");

  /* The reason a lead cannot be called goes first, ahead of the audit's own
     reason to call it. A rep scanning the list has to see "cannot be dialled"
     without hovering anything, and the audit line is still useful after it. */
  const why = lead.why.trim();
  const whyLine = callable ? why : why.length > 0 ? `${NO_PHONE} ${why}` : NO_PHONE;

  /* One class swap instead of an opacity wash. Dropping the whole row to 55
     percent would take the hairlines and the button border down with it and
     lower the contrast of text the rep still has to read. Stepping the two
     loudest cells back one level says "cannot be called" just as clearly. */
  const nameTone = callable ? "text-text" : "text-muted";
  const valueTone = callable ? "text-accent" : "text-dim";

  return (
    <div className="group relative bg-surface transition-colors duration-[120ms] ease-out hover:bg-surface-2">
      <button
        type="button"
        onClick={onOpen}
        title={whyLine.length > 0 ? whyLine : `Open ${lead.name}`}
        aria-label={`Open the details for ${lead.name}`}
        className="absolute inset-0 z-0 h-full w-full cursor-pointer focus-visible:[outline-offset:-2px]"
      />

      <div className="pointer-events-none relative z-10 grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 px-4 py-3 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,0.85fr)_88px_100px_minmax(0,1.6fr)_112px] lg:py-3.5">
        {/* display:contents at lg, so these five become cells of the row grid
            itself and the row is one line. Below lg they stack, and the button
            keeps its own column beside them. One DOM, both layouts. */}
        <div className="flex min-w-0 flex-col gap-1.5 lg:contents">
          <div className="flex min-w-0 items-baseline gap-2">
            <span className={`truncate font-sans text-body ${nameTone}`}>{lead.name}</span>
            {lead.status !== "new" ? (
              <span
                className={`shrink-0 font-mono text-micro uppercase ${STATUS_TONE[lead.status]}`}
              >
                {STATUS_LABELS[lead.status]}
              </span>
            ) : called ? (
              <span className="shrink-0 font-mono text-micro uppercase text-muted">Called</span>
            ) : null}
          </div>

          <div className="min-w-0 truncate font-sans text-[12px] leading-[18px] text-muted">
            {place || "No type listed"}
          </div>

          <div className="flex min-w-0 items-center gap-4 lg:contents">
            <div className="flex shrink-0 items-center gap-1.5">
              {rated ? (
                <>
                  <Star aria-hidden="true" className="h-3 w-3 shrink-0 text-dim" />
                  <span className="tabnum font-mono text-micro text-muted">
                    {lead.rating.toFixed(1)}
                  </span>
                  <span className="tabnum font-mono text-micro text-dim">{lead.reviews}</span>
                </>
              ) : (
                /* A fixed width rule, not a dash and not a zero, the same way the
                   latency meter draws a value it does not have. The column keeps
                   its width and nobody reads a number that was never measured. */
                <span aria-hidden="true" className="mt-0.5 block h-px w-2.5 bg-dim" />
              )}
              <span className="sr-only">
                {rated
                  ? `${lead.rating.toFixed(1)} stars from ${lead.reviews} reviews`
                  : "No stars yet"}
              </span>
            </div>

            <div className={`tabnum shrink-0 font-mono text-value ${valueTone} lg:text-right`}>
              {formatMoney(lead.currency, lead.dealValue)}
            </div>
          </div>

          <div className="min-w-0 truncate font-sans text-[12px] leading-[18px] text-muted">
            {whyLine}
          </div>
        </div>

        <div className="pointer-events-auto flex justify-end lg:justify-self-end">
          {callable ? (
            <button
              type="button"
              onClick={onCall}
              disabled={calling}
              aria-disabled={calling || undefined}
              title={`Call ${lead.name}`}
              className={`${CALL_BUTTON} bg-accent text-[#04121A] ${
                calling ? "pointer-events-none" : "hover:opacity-90"
              }`}
            >
              <Phone aria-hidden="true" className="h-3.5 w-3.5" />
              <span>{calling ? "Opening" : "Call"}</span>
              {calling ? (
                <span
                  aria-hidden="true"
                  className="absolute inset-x-0 bottom-0 h-0.5 overflow-hidden bg-[#04121A]/30"
                >
                  <span className="block h-full w-2/5 bg-[#04121A] animate-sweep" />
                </span>
              ) : null}
            </button>
          ) : (
            /* A span, not a disabled button. A disabled form control takes no
               pointer events in Chrome or Safari, so a title on one is a
               sentence nobody ever sees. This one is a plain element, so the
               tooltip works, and the words on the row say it anyway. */
            <span
              title={`${NO_PHONE} You cannot call this one.`}
              className={`${CALL_BUTTON} cursor-not-allowed border border-line-strong text-muted`}
            >
              <PhoneOff aria-hidden="true" className="h-3.5 w-3.5" />
              <span>No number</span>
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
