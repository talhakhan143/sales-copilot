"use client";

/**
 * The strip at the top of the leads screen.
 *
 * It answers three questions before the rep touches anything: which search am I
 * looking at, how much of it have I already worked through, and how much money
 * is still sitting in the part I have not called. Then it gives them the two
 * ways out: switch to another search, or start a new one.
 *
 * When a scrape is running it also prints the job's own last line. A Chromium
 * window opening and scrolling Google Maps for four minutes with nothing on
 * screen looks broken, and a rep who thinks the app is broken starts it again
 * and ends up with two browsers fighting over one search. One line of real
 * output costs nothing and removes the whole problem.
 *
 * There are no searches at all on the first run, so this is also the empty
 * state: one line saying what to do, with the button that does it right there.
 */

import { useId } from "react";
import { Plus, Search } from "lucide-react";

import { displayName, formatMoney } from "@/lib/leads";
import type { LeadSearch, ScrapeJob } from "@/lib/leads";

export interface SearchPickerProps {
  /** Every search on disk, newest first. */
  searches: LeadSearch[];
  /** The search that is open, or null when there is none. */
  value: string | null;
  onChange(id: string): void;
  /** Ask the page to open the new search form. */
  onNewSearch(): void;
  /** True while a scrape is running, so a second one cannot be started. */
  busy: boolean;
  /** The running or just finished scrape job, or null. */
  job: ScrapeJob | null;
}

/**
 * 36px of ink, 40px of hit area.
 *
 * The invisible pseudo element is the same one the drawer's close button uses.
 * It paints nothing and takes no space, so the button still lines up with the
 * select beside it while a thumb still lands on it every time.
 */
const ACTION =
  "relative flex h-9 shrink-0 items-center gap-2 rounded-hair border border-line-strong px-3 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] ease-out before:absolute before:inset-x-0 before:-inset-y-[2px] before:content-[''] hover:bg-surface-2 hover:text-text disabled:pointer-events-none disabled:opacity-[.38]";

/** Built once. A new formatter per render in a polled component is waste. */
const DAY = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
});

/**
 * The day the search was scraped.
 *
 * The backend sends seconds since the epoch, which is what Python hands out. A
 * value already big enough to be milliseconds is accepted too, so a later change
 * on that side cannot turn every date into 1970.
 */
function scrapedOn(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "";
  const ms = value > 1e12 ? value : value * 1000;
  const date = new Date(ms);
  if (!Number.isFinite(date.getTime())) return "";
  return DAY.format(date);
}

/** "barber" in "hoboken" becomes "Barber in Hoboken". */
function labelFor(search: LeadSearch): string {
  const niche = displayName(search.niche || search.id);
  const place = displayName(search.location || "");
  return place ? `${niche} in ${place}` : niche;
}

interface StatProps {
  label: string;
  value: string;
  tone: string;
  /** Plain sentence shown on hover, for a number whose name is not enough. */
  hint?: string;
}

/** One number with its label, in the house instrument shape. */
function Stat({ label, value, tone, hint }: StatProps) {
  return (
    <div className="flex shrink-0 flex-col gap-1" title={hint}>
      <span className="font-mono text-micro uppercase text-muted">{label}</span>
      <span className={`tabnum font-mono text-value ${tone}`}>{value}</span>
    </div>
  );
}

/** The live output of a running scrape, or the word it ended on. */
function JobStrip({ job }: { job: ScrapeJob }) {
  const running = job.state === "running";
  const failed = job.state === "failed";
  const word = running ? "Working" : failed ? "Failed" : "Done";
  /* Violet is the machine working, the same as the thinking boresight on the
     call page. Green and rose are the two ends, and both are one small word. */
  const tone = running ? "text-accent-2" : failed ? "text-danger" : "text-ok";
  const line = job.line.trim().length > 0 ? job.line : "Starting the browser";

  return (
    <div className="relative flex min-w-0 items-center gap-3 bg-surface px-4 py-2.5">
      <span className={`shrink-0 font-mono text-micro uppercase ${tone}`} aria-live="polite">
        {word}
      </span>
      {job.step.trim().length > 0 ? (
        <span className="shrink-0 font-mono text-micro uppercase text-dim">{job.step}</span>
      ) : null}
      <span
        className="min-w-0 flex-1 truncate font-mono text-[12px] leading-[18px] text-muted"
        title={line}
      >
        {line}
      </span>
      {running ? (
        <span
          aria-hidden="true"
          className="absolute inset-x-0 bottom-0 h-0.5 overflow-hidden bg-line-strong"
        >
          <span className="block h-full w-2/5 bg-accent-2 animate-sweep" />
        </span>
      ) : null}
    </div>
  );
}

export function SearchPicker({
  searches,
  value,
  onChange,
  onNewSearch,
  busy,
  job,
}: SearchPickerProps) {
  const selectId = useId();
  const current = searches.find((search) => search.id === value) ?? null;

  if (searches.length === 0) {
    return (
      <section aria-label="Your searches" className="seam-grid grid-cols-1">
        <div className="flex flex-col items-start gap-4 bg-surface px-4 py-10">
          <span className="font-mono text-micro uppercase text-muted">No searches yet</span>
          <p className="max-w-[62ch] text-lede text-muted">
            Start a search and you get a list of businesses to call, best first, with the reason to
            call each one already written.
          </p>
          <button type="button" onClick={onNewSearch} disabled={busy} className={ACTION}>
            <Plus aria-hidden="true" className="h-3.5 w-3.5" />
            New search
          </button>
        </div>
        {job ? <JobStrip job={job} /> : null}
      </section>
    );
  }

  const day = current ? scrapedOn(current.scrapedAt) : "";

  return (
    <section aria-label="Your searches" className="seam-grid grid-cols-1">
      <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-5 bg-surface px-4 py-4">
        <div className="flex min-w-0 flex-wrap items-end gap-x-8 gap-y-5">
          <div className="flex min-w-0 flex-col gap-1">
            <span className="font-mono text-micro uppercase text-muted">Search</span>
            <h2 className="truncate text-lede text-text">
              {current ? labelFor(current) : "Pick a search"}
            </h2>
            {day ? (
              <span className="tabnum font-mono text-micro uppercase text-dim">Found {day}</span>
            ) : null}
          </div>

          {current ? (
            <div className="flex flex-wrap items-end gap-x-8 gap-y-4">
              <Stat label="Leads" value={String(current.leads)} tone="text-text" />
              <Stat label="Called" value={String(current.called)} tone="text-text" />
              <Stat
                label="Money left"
                value={formatMoney(current.currency, current.value)}
                tone="text-accent"
                hint="What the leads you have not called yet are worth"
              />
            </div>
          ) : null}
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-3">
          <label htmlFor={selectId} className="sr-only">
            Pick a search
          </label>
          <div className="relative">
            <Search
              aria-hidden="true"
              className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-dim"
            />
            <select
              id={selectId}
              value={current ? current.id : ""}
              onChange={(event) => onChange(event.target.value)}
              className="h-9 max-w-[280px] rounded-hair border border-line-strong bg-surface-2 pl-9 pr-3 font-sans text-chip text-text"
            >
              {current ? null : (
                <option value="" disabled>
                  Pick a search
                </option>
              )}
              {searches.map((search) => (
                <option key={search.id} value={search.id}>
                  {labelFor(search)} ({search.leads})
                </option>
              ))}
            </select>
          </div>

          <button
            type="button"
            onClick={onNewSearch}
            disabled={busy}
            aria-keyshortcuts="n"
            title={busy ? "A search is already running" : "New search (key n)"}
            className={ACTION}
          >
            <Plus aria-hidden="true" className="h-3.5 w-3.5" />
            New search
            <span aria-hidden="true" className="tabnum hidden text-dim lg:inline">
              N
            </span>
          </button>
        </div>
      </div>

      {job ? <JobStrip job={job} /> : null}
    </section>
  );
}
