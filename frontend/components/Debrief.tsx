"use client";

/**
 * The scorecard, shown over the call page when a practice call ends.
 *
 * It answers the one question the rep actually has: did I get better, and how
 * much did the prompter help me. So it is a focused panel that reads top to
 * bottom in one pass, not a dashboard: the score, where the points came from,
 * how much of the prompter they used, what went well, what to fix, and one
 * button to go again.
 *
 * Every number here was counted in Python and every sentence was written by the
 * coach model in plain English. This file adds no arithmetic of its own beyond
 * turning a millisecond count into minutes and a got over outOf pair into a bar
 * width, and it never rewrites the copy it is handed.
 *
 * The foot carries the only two ways forward, and they are different things.
 * Practice again runs a fresh call on this same page. Back to setup is a real
 * link off the page, because a finished call page has nothing left on it. The X
 * and the Escape key only put the panel away, so a rep who closed it by mistake
 * can open it again from the call page and lose nothing.
 *
 * Modal rules, all four of them real:
 *   - role dialog with aria-modal, named by its title.
 *   - Escape closes it, and Tab cycles inside it and cannot walk out into the
 *     call page behind it.
 *   - Focus moves into the panel when it opens and returns to whatever opened
 *     it when it closes.
 *   - The panel scrolls inside itself. The page behind never moves, which is
 *     invariant 6 in DESIGN.md and holds at every width.
 *
 * There is no motion in this component at all. The call is over, the rep is
 * reading, and nothing on this panel is live.
 */

import { useEffect, useRef } from "react";
import Link from "next/link";
import { TriangleAlert, X } from "lucide-react";

import type {
  Debrief as DebriefData,
  DebriefFix,
  DebriefMoment,
  Difficulty,
  PracticeOutcome,
} from "@/lib/types";

export interface DebriefProps {
  /** The scorecard, or null while it is being fetched or after it failed. */
  data: DebriefData | null;
  loading: boolean;
  /** One plain sentence from the fetch, or null. */
  error: string | null;
  /** Start a fresh practice call with the same setup. */
  onAgain(): void;
  /**
   * Put the scorecard away and stay on the call page. This does NOT leave the
   * page, so the way back to setup is the link in the foot, not this.
   */
  onClose(): void;
}

const DIFFICULTY_WORD: Record<Difficulty, string> = {
  warm: "Warm",
  normal: "Normal",
  brutal: "Brutal",
};

/** How the call finished, in words a person says out loud. */
const OUTCOME_LINE: Record<PracticeOutcome, string> = {
  booked: "They said yes to a meeting.",
  soft_yes: "They almost said yes.",
  no_answer: "They did not say yes and they did not say no.",
  hung_up: "They put the phone down.",
};

const SECTION_HEAD = "font-mono text-micro uppercase text-muted";
const NOTE = "font-sans text-[12px] leading-[18px] text-muted";

const BUTTON = [
  "flex h-10 shrink-0 items-center justify-center gap-2 rounded-hair border px-4",
  "font-mono text-[12px] font-semibold uppercase tracking-[0.12em]",
  "transition-colors duration-[120ms] ease-out",
  "disabled:cursor-not-allowed disabled:pointer-events-none disabled:opacity-[.38]",
].join(" ");

/** Everything a person can tab to inside the panel. */
const FOCUSABLE =
  'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** "3 min 34 sec", or "34 sec" for a short call. */
function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  if (mins <= 0) return `${secs} sec`;
  return `${mins} min ${secs} sec`;
}

function plural(n: number, one: string, many: string): string {
  return n === 1 ? `${n} ${one}` : `${n} ${many}`;
}

/** A got out of outOf pair as a bar width, clamped and safe when outOf is 0. */
function barPct(got: number, outOf: number): number {
  if (outOf <= 0) return 0;
  const pct = Math.round((got / outOf) * 100);
  return Math.max(0, Math.min(100, pct));
}

/* ============================================================
   SMALL PARTS
   ============================================================ */

type QuoteTone = "client" | "copilot" | "rep";

/**
 * One quoted line with its speaker label.
 *
 * The colours are the transcript rail's, exactly: cyan for the person on the
 * other end, violet for the machine, muted for the rep. Only the client line is
 * at full ink, because that is the line the rep has to learn to hear.
 */
function Quote({ tone, label, text }: { tone: QuoteTone; label: string; text: string }) {
  const labelClass =
    tone === "client" ? "text-accent" : tone === "copilot" ? "text-accent-2" : "text-muted";
  const bodyClass = tone === "client" ? "text-text" : "text-muted";

  return (
    <div className="flex flex-col gap-1">
      <span className={`font-mono text-micro uppercase ${labelClass}`}>{label}</span>
      <p className={`font-sans text-body ${bodyClass}`}>{text}</p>
    </div>
  );
}

/** The best moment or the missed moment, quoted three ways. */
function MomentBlock({
  title,
  tone,
  moment,
}: {
  title: string;
  tone: "best" | "missed";
  moment: DebriefMoment;
}) {
  return (
    <div className="flex flex-col gap-3 bg-surface-2 p-3">
      {/* One MICRO word carries the exception colour here. Nothing else on this
          panel is allowed to, see the colour law in DESIGN.md section 6. */}
      <span
        className={`font-mono text-micro uppercase ${
          tone === "best" ? "text-ok" : "text-warn"
        }`}
      >
        {title}
      </span>
      <Quote tone="client" label="They said" text={moment.clientSaid} />
      <Quote tone="copilot" label="The prompter said" text={moment.copilotSaid} />
      <Quote tone="rep" label="You said" text={moment.youSaid} />
      <p className={NOTE}>{moment.why}</p>
    </div>
  );
}

/** One thing to say better next time. The better line is the loudest thing here. */
function FixRow({ fix }: { fix: DebriefFix }) {
  return (
    <li className="flex flex-col gap-2.5 border-b border-line py-3.5 last:border-b-0">
      <Quote tone="rep" label="You said" text={fix.youSaid} />
      <p className={NOTE}>{fix.problem}</p>
      <div className="border-l-2 border-accent-2 bg-surface-2 p-3">
        <span className="font-mono text-micro uppercase text-accent-2">Say this instead</span>
        <p className="mt-1.5 font-sans text-lede text-text">{fix.sayInstead}</p>
      </div>
    </li>
  );
}

/**
 * The waiting state. Blocks that hold the shape of the real thing, and no pulse
 * on any of them: this product has no spinners and no shimmer, and the rep has
 * just stopped talking and does not need one more moving thing.
 */
function Skeleton() {
  return (
    <div role="status" aria-live="polite" className="flex flex-col gap-6">
      <p className="font-sans text-lede text-text">Scoring your call</p>
      <div className="flex flex-col gap-3">
        <span aria-hidden="true" className="h-[56px] w-[112px] bg-surface-2" />
        <span aria-hidden="true" className="h-3 w-3/4 bg-surface-2" />
      </div>
      <div className="flex flex-col gap-2.5">
        <span aria-hidden="true" className="h-2.5 w-1/3 bg-surface-2" />
        <span aria-hidden="true" className="h-[3px] w-full bg-line-strong" />
        <span aria-hidden="true" className="h-2.5 w-2/3 bg-surface-2" />
      </div>
      <div className="flex flex-col gap-2.5">
        <span aria-hidden="true" className="h-2.5 w-1/3 bg-surface-2" />
        <span aria-hidden="true" className="h-[3px] w-full bg-line-strong" />
        <span aria-hidden="true" className="h-2.5 w-1/2 bg-surface-2" />
      </div>
      <p className={NOTE}>This takes a few seconds.</p>
    </div>
  );
}

/* ============================================================
   THE PANEL
   ============================================================ */

export function Debrief({ data, loading, error, onAgain, onClose }: DebriefProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef(onClose);

  useEffect(() => {
    closeRef.current = onClose;
  });

  /* Focus in on open, back to the opener on close. The opener is read once, at
     mount, because by the time this unmounts the button that opened it may
     already be gone from the page. */
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panelRef.current?.focus();

    return () => {
      if (opener && document.contains(opener)) opener.focus();
    };
  }, []);

  /* Escape closes. Tab cycles inside the panel and never walks out into the call
     page behind it, which is still fully rendered and still full of buttons. */
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
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

  /* No data and no error means the fetch is still out. Treating that as the
     waiting state is the honest default: an empty panel would read as a bug. */
  const waiting = loading || (data === null && error === null);
  const failed = !loading && data === null && error !== null;

  const summary = data
    ? `${OUTCOME_LINE[data.outcome]} The call took ${formatDuration(
        data.durationMs,
      )} and you took ${plural(data.turns, "turn", "turns")}.`
    : "";

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-bg/90 p-0 sm:items-center sm:p-6"
      /* The scrim does not close the panel on a click. Losing a scorecard to a
         stray tap is worse than one extra key press, and Escape and two buttons
         all close it. */
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label="Practice scorecard"
        tabIndex={-1}
        className="flex max-h-[94dvh] w-full max-w-[720px] flex-col rounded-hair border border-line-strong bg-surface shadow-pop sm:max-h-[88dvh]"
      >
        {/* HEAD */}
        <div className="flex h-11 shrink-0 items-center justify-between gap-3 border-b border-line px-4 sm:px-5">
          <span className="font-mono text-micro uppercase text-muted">Practice scorecard</span>
          <div className="flex shrink-0 items-center gap-2">
            {data ? (
              <span className="flex h-[22px] items-center rounded-hair border border-line-strong px-2 font-mono text-micro uppercase text-muted">
                {DIFFICULTY_WORD[data.difficulty]}
              </span>
            ) : null}
            <button
              type="button"
              onClick={onClose}
              aria-label="Close the scorecard"
              title="Close (Escape)"
              /* The box stays the 26px icon button DESIGN.md pins, so it looks
                 the same as the two in the teleprompter head. The hit area is
                 grown to 44px by an absolute pseudo element that paints nothing
                 and shifts nothing, because a finger is not 26px wide. On a
                 screen with no hover it sits at full ink, since there is no
                 hover there to bring it up. */
              className="relative flex h-[26px] w-[26px] items-center justify-center rounded-hair text-muted opacity-45 transition-opacity duration-[140ms] ease-out before:absolute before:-inset-[9px] before:content-[''] hover:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
        </div>

        {/* BODY. The only scrolling element in the whole overlay. */}
        <div className="rail-scroll flex-1 px-4 py-5 sm:px-5">
          {waiting ? <Skeleton /> : null}

          {failed ? (
            <div className="flex flex-col gap-4">
              <div className="flex items-start gap-2.5 border-l-2 border-danger bg-surface-2 p-3">
                <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-danger" aria-hidden="true" />
                <div className="flex min-w-0 flex-col gap-1.5">
                  <p className="font-sans text-body text-text">
                    We could not get your score.
                  </p>
                  <p className={NOTE}>{error}</p>
                </div>
              </div>
              {/* The retry lives in the foot of the panel, which is outside the
                  scroll box and always on screen, so this points at it by name
                  rather than shipping a second button with the same label. */}
              <p className={NOTE}>
                Your practice call still happened. Press Practice again to run one more call,
                or go back to setup.
              </p>
            </div>
          ) : null}

          {data && !waiting ? (
            <div className="flex flex-col gap-8">
              {/* 1. THE SCORE */}
              <section className="flex flex-col gap-3">
                <div className="flex items-end gap-4">
                  <span
                    aria-hidden="true"
                    className="tabnum font-mono text-[56px] font-semibold leading-[52px] text-text sm:text-[72px] sm:leading-[66px]"
                  >
                    {data.score}
                  </span>
                  <span aria-hidden="true" className="flex flex-col gap-1.5 pb-1">
                    <span className="font-mono text-micro uppercase text-muted">Out of 100</span>
                    <span className="font-mono text-status uppercase text-text">{data.grade}</span>
                  </span>
                  <span className="sr-only">
                    You scored {data.score} out of 100. {data.grade}.
                  </span>
                </div>
                <p className="font-sans text-lede text-muted">{summary}</p>
              </section>

              {/* 2. WHERE THE POINTS CAME FROM */}
              <section className="flex flex-col gap-2">
                <h3 className={SECTION_HEAD}>Where the points came from</h3>
                {data.breakdown.length === 0 ? (
                  <p className={NOTE}>There are no points to show for this call.</p>
                ) : (
                  <ul className="flex flex-col">
                    {data.breakdown.map((row) => (
                      <li
                        key={row.label}
                        className="flex flex-col gap-1.5 border-b border-line py-3 last:border-b-0"
                      >
                        <div className="flex items-baseline justify-between gap-3">
                          <span className="font-sans text-body text-text">{row.label}</span>
                          <span className="tabnum shrink-0 font-mono text-micro text-muted">
                            {row.got} / {row.outOf}
                          </span>
                        </div>
                        {/* Monochrome on purpose. A score bar is not live audio,
                            not machine authorship and not a crossed threshold,
                            so it gets no hue. */}
                        <span aria-hidden="true" className="block h-[3px] w-full bg-line-strong">
                          <span
                            className="block h-full bg-text"
                            style={{ width: `${barPct(row.got, row.outOf)}%` }}
                          />
                        </span>
                        <p className={NOTE}>{row.note}</p>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              {/* 3. THE PROMPTER */}
              <section className="flex flex-col gap-3">
                <h3 className={SECTION_HEAD}>How much the prompter helped you</h3>

                {data.copilot.suggestionsShown === 0 ? (
                  <p className="font-sans text-body text-muted">
                    The prompter did not show you any lines in this call.
                  </p>
                ) : (
                  <>
                    <div className="flex items-end gap-3">
                      <span
                        aria-hidden="true"
                        className="tabnum font-mono text-[36px] font-semibold leading-[34px] text-text"
                      >
                        {data.copilot.suggestionsUsed}
                      </span>
                      <span
                        aria-hidden="true"
                        className="tabnum pb-0.5 font-mono text-value text-muted"
                      >
                        / {data.copilot.suggestionsShown}
                      </span>
                      <span
                        aria-hidden="true"
                        className="tabnum pb-1 font-mono text-micro uppercase text-muted"
                      >
                        {data.copilot.usedPct} percent
                      </span>
                    </div>
                    <span aria-hidden="true" className="block h-[3px] w-full bg-line-strong">
                      <span
                        className="block h-full bg-accent-2"
                        style={{ width: `${Math.max(0, Math.min(100, data.copilot.usedPct))}%` }}
                      />
                    </span>
                    <p className="font-sans text-body text-muted">
                      You used {data.copilot.suggestionsUsed} of the{" "}
                      {data.copilot.suggestionsShown} lines it gave you.
                    </p>
                  </>
                )}

                {data.copilot.bestMoment ? (
                  <MomentBlock
                    title="Best moment"
                    tone="best"
                    moment={data.copilot.bestMoment}
                  />
                ) : null}

                {data.copilot.missedMoment ? (
                  <MomentBlock
                    title="Missed moment"
                    tone="missed"
                    moment={data.copilot.missedMoment}
                  />
                ) : null}
              </section>

              {/* 4. WINS AND FIXES */}
              <section className="flex flex-col gap-2">
                <h3 className={SECTION_HEAD}>What went well</h3>
                {data.wins.length === 0 ? (
                  <p className={NOTE}>Nothing to show yet. The call was too short.</p>
                ) : (
                  <ul className="flex flex-col gap-1">
                    {data.wins.map((win, index) => (
                      <li key={`${index}-${win.slice(0, 24)}`} className="flex items-start gap-2.5 py-1">
                        <span
                          aria-hidden="true"
                          className="mt-[7px] h-1.5 w-1.5 shrink-0 bg-ok"
                        />
                        <span className="font-sans text-body text-text">{win}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section className="flex flex-col gap-2">
                <h3 className={SECTION_HEAD}>What to fix</h3>
                {data.fixes.length === 0 ? (
                  <p className={NOTE}>Nothing to fix yet. The call was too short.</p>
                ) : (
                  <ul className="flex flex-col">
                    {data.fixes.map((fix, index) => (
                      <FixRow key={`${index}-${fix.youSaid.slice(0, 24)}`} fix={fix} />
                    ))}
                  </ul>
                )}
              </section>

              {data.nextDrill ? (
                <section className="flex flex-col gap-1.5 border-l-2 border-line-strong bg-surface-2 p-3">
                  <span className={SECTION_HEAD}>Next time</span>
                  <p className="font-sans text-body text-text">{data.nextDrill}</p>
                </section>
              ) : null}
            </div>
          ) : null}
        </div>

        {/* FOOT. Outside the scroll box, so the way out is always on screen. */}
        <div className="flex shrink-0 items-center justify-between gap-3 border-t border-line px-4 py-3 sm:px-5">
          <button
            type="button"
            onClick={onAgain}
            disabled={waiting}
            className={`${BUTTON} border-accent text-accent hover:bg-surface-2`}
          >
            Practice again
          </button>
          {/* A real link, not onClose again. onClose only puts this panel away
              and leaves the rep on a call page that is finished, with no other
              way off it. The negative margin cancels the padding, so the words
              still sit flush with the foot's right edge while the box a finger
              has to hit is the same 40px tall as the button beside it. */}
          <Link
            href="/"
            className="-mr-3 flex h-10 shrink-0 items-center rounded-hair px-3 font-mono text-micro uppercase text-muted underline-offset-4 transition-colors duration-[120ms] ease-out hover:text-text hover:underline"
          >
            Back to setup
          </Link>
        </div>
      </div>
    </div>
  );
}
