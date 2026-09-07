"use client";

/**
 * The list of leads, with the two controls that sit above it.
 *
 * The filters are chips and not a form on purpose. A rep changes them between
 * calls, sometimes twice in a minute, and a form means open it, change it, press
 * apply, close it. A chip is one press. The same reasoning makes the sort a
 * joined trio of cells rather than a dropdown: three choices, one press each,
 * and the one that is on is visible without opening anything.
 *
 * Filtering happens here, in memory, over the rows the page already fetched.
 * Sorting does not: it is a query parameter, so the page refetches. That split
 * is deliberate. A chip press must feel instant because it happens constantly,
 * and the order of a calling list is the one thing the server is authoritative
 * about.
 *
 * When a press on Call fails, the page hands the sentence down here and this
 * component puts it under the row that was pressed, then scrolls it into view.
 * A notice at the top of the page is off screen at row 40, and a rep who
 * presses Call and sees nothing move presses it again.
 *
 * The "/" key focuses the chip that is on. The component that owns a control
 * owns its shortcut, which is the rule ObjectionBar already follows for the
 * digits, so there is no global key table anywhere in this product.
 */

import { Fragment, useCallback, useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { TriangleAlert } from "lucide-react";

import { LeadRow } from "@/components/LeadRow";
import { BUCKET_EMPTY, BUCKET_LABELS, SORT_LABELS, bucketOf, canCall } from "@/lib/leads";
import type { Lead, LeadBucket, LeadSort } from "@/lib/leads";

export interface LeadListProps {
  /** Every lead in the selected search, in the order the server sent them. */
  leads: Lead[];
  /** True while the list is being fetched. */
  loading: boolean;
  /** One plain sentence about why the fetch failed, or null. */
  error: string | null;
  /** The chip that is on. */
  bucket: LeadBucket;
  /** Hide the leads with no number, because those cannot be dialled. */
  phoneOnly: boolean;
  /** The sort cell that is on. */
  sort: LeadSort;
  onBucket(bucket: LeadBucket): void;
  onPhoneOnly(next: boolean): void;
  onSort(sort: LeadSort): void;
  onCall(lead: Lead): void;
  onOpen(lead: Lead): void;
  /** The key of the lead whose call is being built, or null. */
  callingKey: string | null;
  /** The lead the notice below belongs to, or null when there is no notice. */
  noticeKey?: string | null;
  /** What to draw under that lead's row. The page owns the words and the link. */
  notice?: ReactNode;
}

/**
 * Three tabs, not nine chips.
 *
 * Nine chips is the data model on screen. A rep planning their morning holds
 * three questions: who is left, who did I promise to ring again, who am I done
 * with. So the seven statuses fold into three tabs, each carrying its own count,
 * and the exact outcome still shows as a small tag on the row.
 *
 * "Has phone" is not one of these. It is not a stage of the day, it is a way to
 * hide rows that cannot be dialled, so it sits beside the tabs as its own toggle.
 */
const BUCKETS: { key: LeadBucket; label: string }[] = [
  { key: "to_call", label: BUCKET_LABELS.to_call },
  { key: "callback", label: BUCKET_LABELS.callback },
  { key: "done", label: BUCKET_LABELS.done },
];

/**
 * The sort cells.
 *
 * One word each on screen, because three full sentences beside nine chips is a
 * toolbar nobody can scan. The full sentence from SORT_LABELS is the title and
 * the accessible name, so hover and a screen reader both get the whole thing.
 */
const SORTS: { key: LeadSort; label: string }[] = [
  { key: "value", label: "Money" },
  { key: "score", label: "Score" },
  { key: "name", label: "Name" },
];

/**
 * The invisible hit area, the same trick the drawer's close button uses.
 *
 * These controls are 26px tall because the toolbar has to hold nine chips and
 * three sort cells on one line. A thumb needs 44. The pseudo element paints
 * nothing and takes no space in the layout, so the control looks 26px and
 * catches a press over 44px. The vertical grow is the full 9px. The sideways
 * grow is 4px, which is exactly half the 8px gap between two chips, so no chip
 * ever steals a press meant for the one beside it.
 */
const HIT_TALL = "before:absolute before:-inset-y-[9px] before:content-['']";
const HIT_WIDE = `${HIT_TALL} before:-inset-x-1`;

/* Padding is the same in both states on purpose. The bar that marks the chip
   that is on is absolutely positioned, so turning a chip on cannot change its
   width and cannot reflow the row under the rep's finger. */
const CHIP =
  `relative flex h-[26px] shrink-0 items-center rounded-hair border border-line-strong pl-3 pr-2.5 font-mono text-micro uppercase transition-colors duration-[120ms] ease-out ${HIT_WIDE}`;

/* Sideways grow would reach into the cell beside it, because the sort cells are
   joined with a 1px seam and nothing else. Vertical only. */
const SORT_CELL =
  `relative flex h-[26px] items-center px-2.5 font-mono text-micro uppercase transition-colors duration-[120ms] ease-out ${HIT_TALL} before:inset-x-0`;

const SMALL_BUTTON =
  "relative flex h-[26px] items-center rounded-hair border border-line-strong px-2.5 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] ease-out before:absolute before:-inset-[9px] before:content-[''] hover:bg-surface-2 hover:text-text";

/** True when a key press landed in something the rep is typing into. */
function isEditable(node: unknown): boolean {
  if (!node || typeof node !== "object") return false;
  const el = node as HTMLElement;
  const tag = typeof el.tagName === "string" ? el.tagName.toLowerCase() : "";
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  return el.isContentEditable === true;
}

/** Does this lead belong in the list under the chip that is on. */
function matches(lead: Lead, bucket: LeadBucket, phoneOnly: boolean): boolean {
  if (phoneOnly && !canCall(lead)) return false;
  return bucketOf(lead.status) === bucket;
}

export function LeadList({
  leads,
  loading,
  error,
  bucket,
  phoneOnly,
  sort,
  onBucket,
  onPhoneOnly,
  onSort,
  onCall,
  onOpen,
  callingKey,
  noticeKey = null,
  notice = null,
}: LeadListProps) {
  const chipsRef = useRef<HTMLDivElement | null>(null);
  const noticeRef = useRef<HTMLDivElement | null>(null);

  /* Focus lands on the tab that is on, and the tab that is on is the one whose
     aria-selected is true. That is read off the same attribute the tab already
     has to carry to be a tab at all, so there is no second copy of "which one
     is on" to fall out of step with the first. */
  const focusFilter = useCallback(() => {
    const group = chipsRef.current;
    if (!group) return;
    const on = group.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]');
    if (on) on.focus();
  }, []);

  /* One listener for the one key this component owns. Dead while a field has
     focus, dead while a modifier is held, dead on key repeat, exactly like the
     objection digits on the call page. */
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key !== "/") return;
      if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;
      if (isEditable(event.target) || isEditable(document.activeElement)) return;
      event.preventDefault();
      focusFilter();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focusFilter]);

  const shown = leads.filter((lead) => matches(lead, bucket, phoneOnly));

  /* Every tab carries its own count, so the rep can see there are three left to
     ring back without having to click onto that tab to find out. */
  const counts: Record<LeadBucket, number> = { to_call: 0, callback: 0, done: 0 };
  for (const lead of leads) {
    if (phoneOnly && !canCall(lead)) continue;
    counts[bucketOf(lead.status)] += 1;
  }
  const total = leads.length;
  const count = `${shown.length} of ${total} leads`;

  const hasNotice = typeof noticeKey === "string" && noticeKey.length > 0 && notice !== null;
  const noticeAt = hasNotice ? shown.findIndex((lead) => lead.key === noticeKey) : -1;

  /* Scroll on the key, never on the node. The node is fresh JSX on every render,
     so depending on it would drag the page every time the parent re-renders.
     "nearest" moves the page the smallest amount that makes it visible, so a
     notice that is already on screen does not move anything at all. */
  useEffect(() => {
    if (!hasNotice) return;
    noticeRef.current?.scrollIntoView({ block: "nearest" });
  }, [hasNotice, noticeKey]);

  const noticeBlock = hasNotice ? <div ref={noticeRef}>{notice}</div> : null;

  return (
    <section aria-label="Leads" className="seam-grid grid-cols-1">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 bg-surface px-4 py-3">
        <div
          ref={chipsRef}
          role="tablist"
          aria-label="Which leads to show"
          aria-keyshortcuts="/"
          className="flex flex-wrap items-center gap-2"
        >
          {BUCKETS.map((item) => {
            const on = item.key === bucket;
            return (
              <button
                key={item.key}
                type="button"
                role="tab"
                aria-selected={on}
                onClick={() => onBucket(item.key)}
                className={`${CHIP} gap-2 ${
                  on ? "bg-surface-2 text-text" : "text-muted hover:bg-surface-2 hover:text-text"
                }`}
              >
                {on ? (
                  <span
                    aria-hidden="true"
                    className="pointer-events-none absolute inset-y-0 left-0 w-0.5 bg-accent"
                  />
                ) : null}
                {item.label}
                <span className={`tabnum ${on ? "text-accent" : "text-dim"}`}>
                  {counts[item.key]}
                </span>
              </button>
            );
          })}

          <span aria-hidden="true" className="mx-1 h-3.5 w-px shrink-0 bg-line-strong" />

          {/* Not a stage of the day, so not a tab. A lead with no number cannot
              be rung, and hiding those is a different question from where the
              lead is up to. */}
          <button
            type="button"
            aria-pressed={phoneOnly}
            onClick={() => onPhoneOnly(!phoneOnly)}
            title="Hide the leads that have no phone number"
            className={`${CHIP} ${
              phoneOnly ? "bg-surface-2 text-text" : "text-muted hover:bg-surface-2 hover:text-text"
            }`}
          >
            {phoneOnly ? (
              <span
                aria-hidden="true"
                className="pointer-events-none absolute inset-y-0 left-0 w-0.5 bg-accent"
              />
            ) : null}
            Has phone
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <span className="tabnum font-mono text-micro uppercase text-muted" aria-live="polite">
            {loading && total === 0 ? "Loading" : count}
          </span>
          <span aria-hidden="true" className="h-3.5 w-px bg-line-strong" />
          <span className="font-mono text-micro uppercase text-muted">Sort</span>
          <div role="group" aria-label="Sort the leads" className="seam-grid grid-flow-col">
            {SORTS.map((item) => {
              const on = item.key === sort;
              return (
                <button
                  key={item.key}
                  type="button"
                  aria-pressed={on}
                  aria-label={SORT_LABELS[item.key]}
                  title={SORT_LABELS[item.key]}
                  onClick={() => onSort(item.key)}
                  className={`${SORT_CELL} ${
                    on
                      ? "bg-surface-2 text-text"
                      : "bg-surface text-muted hover:bg-surface-2 hover:text-text"
                  }`}
                >
                  {on ? (
                    <span
                      aria-hidden="true"
                      className="pointer-events-none absolute inset-x-0 bottom-0 h-0.5 bg-accent"
                    />
                  ) : null}
                  {item.label}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {error ? (
        <div className="flex gap-3 border-l-2 border-danger bg-surface-2 p-4">
          <TriangleAlert aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
          <p className="text-body text-muted">{error}</p>
        </div>
      ) : null}

      {/* The pressed row is not in the list right now, because the rep changed
          the chip while the call was being built. The sentence still has to be
          read, so it goes to the top of the list instead of nowhere. */}
      {hasNotice && noticeAt < 0 ? noticeBlock : null}

      {shown.map((lead, index) => (
        <Fragment key={lead.key}>
          <LeadRow
            lead={lead}
            calling={callingKey === lead.key}
            onCall={() => onCall(lead)}
            onOpen={() => onOpen(lead)}
          />
          {index === noticeAt ? noticeBlock : null}
        </Fragment>
      ))}

      {shown.length === 0 && !error ? (
        <div className="flex flex-col items-center gap-3 bg-surface px-4 py-12 text-center">
          {loading ? (
            <span className="font-mono text-micro uppercase text-muted">Getting the leads</span>
          ) : total === 0 ? (
            <>
              <span className="font-mono text-micro uppercase text-muted">No leads yet</span>
              <p className="max-w-[46ch] text-body text-muted">
                This search has no leads ready yet. The check on their websites is still running,
                or it has not started. Wait for it to finish, or start a new search.
              </p>
            </>
          ) : (
            <>
              <span className="font-mono text-micro uppercase text-muted">Nothing here</span>
              <p className="max-w-[46ch] text-body text-muted">{BUCKET_EMPTY[bucket]}</p>
              {/* One reason this tab can look empty is the rep's own doing. If
                  "Has phone" is on, say so and give them the press that undoes
                  it, because the tab count beside it already reads zero and
                  that looks like the search found nothing. */}
              {phoneOnly ? (
                <button type="button" onClick={() => onPhoneOnly(false)} className={SMALL_BUTTON}>
                  Show the ones with no number
                </button>
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </section>
  );
}
