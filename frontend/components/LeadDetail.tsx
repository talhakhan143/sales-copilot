"use client";

/**
 * The lead drawer, over the calling list.
 *
 * One question, answered in about ten seconds: what do I know about this
 * business that they do not know I know. The rep opens this, skims it, and
 * presses Call. So the panel is one column that reads straight down in the order
 * a cold call actually goes:
 *
 *   who they are  ->  what the job is worth  ->  what is wrong with them
 *   ->  what to say first  ->  their links  ->  their hours
 *
 * The proof beside each fault is the point of the whole screen. It is the number
 * the rep says out loud, and it is the only reason the prospect stays on the
 * line, so it is printed as its own chip on the darkest fill in the product
 * instead of being buried inside the sentence.
 *
 * Drawer rules, all of them real:
 *   - role dialog with aria-modal, named by the business.
 *   - Escape closes it. A pointer down anywhere outside the panel closes it.
 *   - Tab cycles inside the panel and cannot walk back into the list behind it.
 *   - Focus moves into the panel when it opens and goes back to the row that
 *     opened it when it closes.
 *   - The panel scrolls inside itself. The list behind it is pinned where the
 *     rep left it and does not move, at every width down to 390px.
 *
 * There is no motion here at all. Nothing on this panel is live.
 */

import { useEffect, useRef } from "react";
import { ExternalLink, MapPin, Phone, PhoneOff, Star, TriangleAlert, X } from "lucide-react";

import { STATUS_LABELS, TRACK_LABELS, canCall, formatDealValue } from "@/lib/leads";
import type { LeadDetail as Lead, PainPoint, PainSeverity } from "@/lib/leads";

export interface LeadDetailProps {
  /** The lead to show, or null while it is being read or after it failed. */
  lead: Lead | null;
  loading: boolean;
  /** One plain sentence from the fetch, or null. */
  error: string | null;
  /** Put the drawer away and go back to the list. */
  onClose(): void;
  /** Build the call context for this lead and open the teleprompter. */
  onCall(): void;
  /** True while this lead's call is being built. */
  calling: boolean;
}

/* ============================================================
   WORDS AND CLASSES
   ============================================================ */

const SECTION_HEAD = "font-mono text-micro uppercase text-muted";
const NOTE = "font-sans text-[12px] leading-[18px] text-muted";

/**
 * How bad one finding is, in words a person says.
 *
 * The colour law lets one small uppercase word carry an exception hue when it
 * marks a crossed threshold, and this is that case. Only the two worst levels
 * get a hue, so the eye is pulled to the lines worth opening with. Nothing here
 * is ever `--dim`, because these are words the rep has to read.
 */
const SEVERITY_WORD: Record<PainSeverity, string> = {
  critical: "Very bad",
  high: "Bad",
  medium: "Medium",
  low: "Small",
};

const SEVERITY_TONE: Record<PainSeverity, string> = {
  critical: "text-danger",
  high: "text-warn",
  medium: "text-muted",
  low: "text-muted",
};

/** Strongest first, whatever order the file on disk was in. */
const SEVERITY_RANK: Record<PainSeverity, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
};

const LINK_BUTTON = [
  "flex h-9 items-center gap-2 rounded-hair border border-line-strong px-3",
  "font-mono text-[12px] font-semibold uppercase tracking-[0.12em] text-muted",
  "transition-colors duration-[120ms] ease-out hover:bg-surface-2 hover:text-text",
].join(" ");

/**
 * The Call button, the same shape and the same fill as the one on the row.
 *
 * This is the only filled control on the leads screen, exactly as the leads
 * contract asks, and the dark ink on the cyan fill is the pair DESIGN.md
 * already fixed for the one saturated button in the product.
 */
const CALL_BUTTON = [
  "relative flex h-10 min-w-[136px] shrink-0 items-center justify-center gap-2",
  "overflow-hidden rounded-hair px-4",
  "font-mono text-[12px] font-semibold uppercase tracking-[0.12em]",
  "transition-opacity duration-[120ms] ease-out",
].join(" ");

/** Everything a person can tab to inside the panel. */
const FOCUSABLE =
  'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/* ============================================================
   SMALL HELPERS
   ============================================================ */

/** "littlespacesalon.com" out of "http://www.littlespacesalon.com/". */
function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/** Strongest finding first. The list is copied, so the prop is never touched. */
function strongestFirst(points: PainPoint[]): PainPoint[] {
  return [...points].sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]);
}

/**
 * One plain line about the money under the big number.
 *
 * Every part is left out when its number is missing, so the rep never reads a
 * zero that was never measured.
 */
function moneyLine(lead: Lead): string {
  const symbol = lead.currency;
  const parts: string[] = [];

  if (lead.money.tierLabel.trim().length > 0) parts.push(`${lead.money.tierLabel} job.`);
  if (lead.money.retainerMonthlyUsd > 0) {
    parts.push(`Then ${formatDealValue(lead.money.retainerMonthlyUsd, symbol)} every month.`);
  }
  if (lead.money.contractValueUsd > 0) {
    parts.push(`The whole contract is ${formatDealValue(lead.money.contractValueUsd, symbol)}.`);
  }

  return parts.join(" ");
}

/* ============================================================
   SMALL PARTS
   ============================================================ */

/** One thing the audit found wrong, with the number the rep says out loud. */
function PainRow({ point }: { point: PainPoint }) {
  const proof = point.proof.trim();

  return (
    <li className="flex flex-col gap-2 border-b border-line py-3.5 last:border-b-0">
      <div className="flex items-baseline justify-between gap-3">
        <p className="min-w-0 font-sans text-body text-text">{point.title}</p>
        <span className={`shrink-0 font-mono text-micro uppercase ${SEVERITY_TONE[point.severity]}`}>
          {SEVERITY_WORD[point.severity]}
        </span>
      </div>

      {proof.length > 0 ? (
        <span className="flex items-center gap-2">
          <span className="shrink-0 font-mono text-micro uppercase text-muted">Proof</span>
          {/* The darkest fill in the product, so the number the rep has to say
              sits in its own well and is found without reading the sentence. */}
          <span className="tabnum inline-flex h-[22px] items-center rounded-hair border border-line-strong bg-bg px-2 font-mono text-micro uppercase text-text">
            {proof}
          </span>
        </span>
      ) : null}

      <p className={NOTE}>{point.detail}</p>
    </li>
  );
}

/**
 * The waiting state. Blocks in the shape of the real thing, with no pulse and no
 * spinner, because this product has neither.
 */
function Skeleton() {
  return (
    <div role="status" aria-live="polite" className="flex flex-col gap-6">
      <p className="font-sans text-lede text-text">Opening this lead</p>
      <div className="flex flex-col gap-2.5">
        <span aria-hidden="true" className="h-6 w-2/3 bg-surface-2" />
        <span aria-hidden="true" className="h-2.5 w-1/2 bg-surface-2" />
      </div>
      <div className="flex flex-col gap-2.5">
        <span aria-hidden="true" className="h-2.5 w-1/3 bg-surface-2" />
        <span aria-hidden="true" className="h-[3px] w-full bg-line-strong" />
        <span aria-hidden="true" className="h-2.5 w-3/4 bg-surface-2" />
      </div>
      <div className="flex flex-col gap-2.5">
        <span aria-hidden="true" className="h-2.5 w-1/3 bg-surface-2" />
        <span aria-hidden="true" className="h-[3px] w-full bg-line-strong" />
        <span aria-hidden="true" className="h-2.5 w-2/3 bg-surface-2" />
      </div>
    </div>
  );
}

/* ============================================================
   THE DRAWER
   ============================================================ */

export function LeadDetail({ lead, loading, error, onClose, onCall, calling }: LeadDetailProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef(onClose);

  useEffect(() => {
    closeRef.current = onClose;
  });

  /* Focus in on open, back to the row that opened it on close. The opener is
     read once, at mount, because by the time this unmounts the list underneath
     may have been re-sorted and the row may be gone. */
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panelRef.current?.focus();

    return () => {
      if (opener && document.contains(opener)) opener.focus();
    };
  }, []);

  /* The list behind must not move while the drawer is open, and it must still
     be exactly where the rep left it when the drawer closes.

     The call page's own lock cannot be borrowed for this. It puts overflow
     hidden and height 100 percent on the body, which turns the body into a box
     one screen tall that clips everything under it. The call page never scrolls
     so that costs it nothing, but the leads list is a long page: the browser is
     left with nothing to scroll, drops the offset to zero, and the rep who opens
     the fortieth lead comes back to the top of the list. DESIGN.md says the same
     thing in its own words, a page that scrolls never sets that flag.

     So the offset is read first, the body is pinned at minus that offset, and
     the offset is put back on the way out. Pinning the body is also the one
     lock a phone browser actually obeys. The bar left by the scrollbar is filled
     in, so the list does not jump sideways as it goes still. Every value is put
     back exactly as it was found. */
  useEffect(() => {
    const body = document.body;
    const offset = window.scrollY;
    const gap = window.innerWidth - document.documentElement.clientWidth;

    const before = {
      position: body.style.position,
      top: body.style.top,
      left: body.style.left,
      right: body.style.right,
      width: body.style.width,
      paddingRight: body.style.paddingRight,
    };

    body.style.position = "fixed";
    body.style.top = `-${offset}px`;
    body.style.left = "0";
    body.style.right = "0";
    body.style.width = "100%";
    if (gap > 0) body.style.paddingRight = `${gap}px`;

    return () => {
      body.style.position = before.position;
      body.style.top = before.top;
      body.style.left = before.left;
      body.style.right = before.right;
      body.style.width = before.width;
      body.style.paddingRight = before.paddingRight;
      window.scrollTo(0, offset);
    };
  }, []);

  /* Escape closes. Tab cycles inside the panel and never walks out into the list
     behind it, which is still rendered and still full of buttons. */
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeRef.current();
        return;
      }

      if (event.key !== "Tab") return;

      const panel = panelRef.current;
      if (!panel) return;

      const nodes = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (nodes.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }

      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      const active = document.activeElement;

      if (event.shiftKey && (active === first || active === panel)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      } else if (active instanceof HTMLElement && !panel.contains(active)) {
        event.preventDefault();
        first.focus();
      }
    }

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  /* A pointer down outside the panel closes it. It is bound in the capture
     phase, so a row underneath cannot open a second drawer with the same press.
     The press that opened this drawer happened before the listener existed, so
     it can never close itself on the way in. */
  useEffect(() => {
    function onPointerDown(event: PointerEvent) {
      const panel = panelRef.current;
      const target = event.target;
      if (panel && target instanceof Node && !panel.contains(target)) closeRef.current();
    }

    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, []);

  /* No lead and no error means the read is still out. Treating that as the
     waiting state is the honest default: an empty drawer reads as a bug. */
  const waiting = loading || (lead === null && error === null);
  const failed = !loading && lead === null && error !== null;

  const rated = lead !== null && typeof lead.rating === "number" && lead.rating > 0;
  const reviews = lead !== null && typeof lead.reviews === "number" ? lead.reviews : 0;
  const callable = lead !== null && canCall(lead);
  const points = lead === null ? [] : strongestFirst(lead.painPoints);
  const website = lead !== null && typeof lead.website === "string" ? lead.website.trim() : "";
  const gmbUrl = lead !== null && typeof lead.gmbUrl === "string" ? lead.gmbUrl.trim() : "";
  const phone = lead !== null && typeof lead.phone === "string" ? lead.phone.trim() : "";
  const money = lead === null ? "" : moneyLine(lead);

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-bg/80">
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={lead ? lead.name : "Lead details"}
        tabIndex={-1}
        className="flex h-full w-full max-w-[560px] flex-col border-l border-line-strong bg-surface shadow-pop"
      >
        {/* HEAD */}
        <div className="flex h-11 shrink-0 items-center justify-between gap-3 border-b border-line px-4">
          <span className={SECTION_HEAD}>Lead</span>
          <div className="flex shrink-0 items-center gap-2">
            {lead ? (
              <span className="flex h-[22px] items-center rounded-hair border border-line-strong px-2 font-mono text-micro uppercase text-muted">
                {STATUS_LABELS[lead.status]}
              </span>
            ) : null}
            <button
              type="button"
              onClick={onClose}
              aria-label="Close this lead"
              title="Close (Escape)"
              /* The 26px icon button the teleprompter head uses, with the hit
                 area grown to 44px by a pseudo element that paints nothing and
                 shifts nothing, because a finger is not 26px wide. On a screen
                 with no hover it sits at full ink. */
              className="relative flex h-[26px] w-[26px] items-center justify-center rounded-hair text-muted opacity-45 transition-opacity duration-[140ms] ease-out before:absolute before:-inset-[9px] before:content-[''] hover:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
        </div>

        {/* BODY. The only scrolling element in the drawer. */}
        <div className="rail-scroll flex-1 px-4 py-5">
          {waiting ? <Skeleton /> : null}

          {failed ? (
            <div className="flex flex-col gap-4">
              <div className="flex items-start gap-2.5 border-l-2 border-danger bg-surface-2 p-3">
                <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-danger" aria-hidden="true" />
                <div className="flex min-w-0 flex-col gap-1.5">
                  <p className="font-sans text-body text-text">We could not open this lead.</p>
                  <p className={NOTE}>{error}</p>
                </div>
              </div>
              <p className={NOTE}>Close this and pick the lead again.</p>
            </div>
          ) : null}

          {lead && !waiting ? (
            <div className="flex flex-col gap-7">
              {/* 1. WHO THEY ARE */}
              <section className="flex flex-col gap-2.5">
                <h2 className="font-sans text-h1 text-text">{lead.name}</h2>

                <p className="font-sans text-lede text-muted">
                  {[lead.category, lead.city].filter((part) => part.trim().length > 0).join(", ") ||
                    "No type listed"}
                </p>

                <span className="flex flex-wrap items-center gap-1.5">
                  {rated && lead.rating !== null ? (
                    <>
                      <Star aria-hidden="true" className="h-3 w-3 shrink-0 text-dim" />
                      <span className="tabnum font-mono text-micro text-text">
                        {lead.rating.toFixed(1)}
                      </span>
                      <span className="font-mono text-micro uppercase text-muted">
                        from {reviews} reviews
                      </span>
                    </>
                  ) : (
                    <span className="font-mono text-micro uppercase text-muted">No stars yet</span>
                  )}
                </span>

                {lead.address.trim().length > 0 ? (
                  <p className="flex items-start gap-2">
                    <MapPin aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0 text-dim" />
                    <span className={NOTE}>{lead.address}</span>
                  </p>
                ) : null}
              </section>

              {/* 2. WHAT THE JOB IS WORTH */}
              <section className="flex flex-col gap-2 bg-surface-2 p-3">
                <span className={SECTION_HEAD}>What this job is worth</span>
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span className="tabnum font-mono text-[28px] font-semibold leading-[30px] text-accent">
                    {formatDealValue(lead.dealValue, lead.currency)}
                  </span>
                  <span className="font-sans text-body text-accent">{TRACK_LABELS[lead.track]}</span>
                </div>
                {money.length > 0 ? <p className={NOTE}>{money}</p> : null}
              </section>

              {/* 3. WHAT IS WRONG */}
              <section className="flex flex-col gap-2">
                <h3 className={SECTION_HEAD}>What is wrong</h3>
                {points.length === 0 ? (
                  <p className={NOTE}>We have not checked this business yet.</p>
                ) : (
                  <>
                    <p className={NOTE}>
                      These are checked facts, not guesses. Say the proof out loud.
                    </p>
                    <ul className="flex flex-col">
                      {points.map((point, index) => (
                        <PainRow key={`${index}-${point.title.slice(0, 24)}`} point={point} />
                      ))}
                    </ul>
                  </>
                )}
              </section>

              {/* 4. WHAT TO SAY FIRST */}
              <section className="flex flex-col gap-2">
                <h3 className={SECTION_HEAD}>What to say first</h3>
                {lead.talkingPoints.length === 0 ? (
                  <p className={NOTE}>Nothing yet.</p>
                ) : (
                  <>
                    <ul className="flex flex-col gap-1">
                      {lead.talkingPoints.map((line, index) => (
                        <li
                          key={`${index}-${line.slice(0, 24)}`}
                          className="flex items-start gap-2.5 py-1"
                        >
                          <span
                            aria-hidden="true"
                            className="mt-[7px] h-1.5 w-1.5 shrink-0 bg-line-strong"
                          />
                          <span className="font-sans text-body text-text">{line}</span>
                        </li>
                      ))}
                    </ul>
                    <p className={NOTE}>These are for you to read, not to say out loud.</p>
                  </>
                )}
              </section>

              {/* 5. THEIR LINKS */}
              <section className="flex flex-col gap-2">
                <h3 className={SECTION_HEAD}>Their pages</h3>

                {website.length > 0 || gmbUrl.length > 0 ? (
                  <div className="flex flex-wrap items-center gap-2">
                    {website.length > 0 ? (
                      <a
                        href={website}
                        target="_blank"
                        rel="noopener noreferrer"
                        title={website}
                        className={LINK_BUTTON}
                      >
                        <ExternalLink aria-hidden="true" className="h-3.5 w-3.5" />
                        <span>Their site</span>
                      </a>
                    ) : null}
                    {gmbUrl.length > 0 ? (
                      <a
                        href={gmbUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        title="Their Google Maps listing"
                        className={LINK_BUTTON}
                      >
                        <ExternalLink aria-hidden="true" className="h-3.5 w-3.5" />
                        <span>Google listing</span>
                      </a>
                    ) : null}
                  </div>
                ) : null}

                {website.length > 0 ? (
                  <p className={NOTE}>{hostOf(website)}. Links open in a new tab.</p>
                ) : (
                  <p className={NOTE}>They have no website. That is your strongest line.</p>
                )}
              </section>

              {/* 6. YOUR OWN NOTE FROM LAST TIME */}
              {lead.notes.trim().length > 0 ? (
                <section className="flex flex-col gap-1.5 border-l-2 border-line-strong bg-surface-2 p-3">
                  <span className={SECTION_HEAD}>Your note from last time</span>
                  <p className="font-sans text-body text-text">{lead.notes}</p>
                </section>
              ) : null}

              {/* 7. HOURS, QUIETLY */}
              <section className="flex flex-col gap-2">
                <h3 className={SECTION_HEAD}>Hours</h3>
                {lead.hours.length === 0 ? (
                  <p className={NOTE}>No hours listed.</p>
                ) : (
                  <dl className="grid grid-cols-[minmax(84px,auto)_minmax(0,1fr)] gap-x-4 gap-y-1">
                    {lead.hours.map((row, index) => (
                      <div key={`${index}-${row.day}`} className="contents">
                        <dt className={NOTE}>{row.day}</dt>
                        <dd className={NOTE}>{row.hours}</dd>
                      </div>
                    ))}
                  </dl>
                )}
              </section>
            </div>
          ) : null}
        </div>

        {/* FOOT. Outside the scroll box, so Call is always on screen. */}
        <div className="flex shrink-0 items-center justify-between gap-3 border-t border-line px-4 py-3">
          <span className="flex min-w-0 flex-col gap-0.5">
            <span className={SECTION_HEAD}>Phone</span>
            {phone.length > 0 ? (
              <span className="tabnum truncate font-mono text-chip text-text">{phone}</span>
            ) : (
              <span className="truncate font-sans text-[12px] leading-[18px] text-muted">
                Google has no number for this one
              </span>
            )}
          </span>

          {callable ? (
            <button
              type="button"
              onClick={onCall}
              disabled={calling}
              aria-disabled={calling || undefined}
              title={lead ? `Call ${lead.name}` : "Call this lead"}
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
                  <span className="block h-full w-2/5 animate-sweep bg-[#04121A]" />
                </span>
              ) : null}
            </button>
          ) : (
            <button
              type="button"
              disabled
              aria-disabled="true"
              title="Google Maps has no phone number for this one"
              className={`${CALL_BUTTON} cursor-not-allowed border border-line-strong text-muted`}
            >
              <PhoneOff aria-hidden="true" className="h-3.5 w-3.5" />
              <span>No number</span>
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
