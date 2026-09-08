"use client";

/**
 * Today's queue: the same leads as the list, in the order to actually work them.
 *
 * The calling list answers "who is in this search". This answers "what do I do
 * first", which is a different question and the reason the rep's old dashboard
 * had both. A list sorted by money says ring the biggest deal first. That is
 * wrong on a cold call: the biggest deal whose website is fine will not take the
 * meeting, and the smaller one whose site has been down for a week will.
 *
 * So the sort here is the engine's own `priority`, and inside each lane the
 * money. Two columns, because a call selling a first website and a call selling
 * SEO to someone who already has one are different conversations, and a rep who
 * has warmed up on one does better staying on it than alternating.
 *
 * Done leads are not here at all. This screen is what is left.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowRight, Flame, Phone, PhoneOff, Clock, Minus } from "lucide-react";

import { LeadChrome, MICRO_BUTTON, SMALL_BUTTON, shortMoney } from "@/components/LeadChrome";
import { Notice } from "@/components/Notice";
import { canCall, fetchLeads, fetchSearches, readLastSearch, writeLastSearch } from "@/lib/leads";
import type { Lead, LeadSearch, LeadTrack } from "@/lib/leads";
import type { LeadPriority } from "@/lib/types";
import { PRIORITY_BLURBS, PRIORITY_LABELS, PRIORITY_ORDER } from "@/lib/types";

/** The two conversations. Anything the engine did not label lands in Website. */
const LANES: { track: LeadTrack; label: string; blurb: string }[] = [
  {
    track: "WEBSITE",
    label: "Needs a website",
    blurb: "They have nothing, or nothing that works. You are selling them a first site.",
  },
  {
    track: "SEO",
    label: "Has a website",
    blurb: "They already have one. You are selling them the work to make it earn.",
  },
];

const PRIORITY_ICON: Record<LeadPriority, typeof Flame> = {
  now: Flame,
  week: Clock,
  later: Minus,
};

const PRIORITY_TONE: Record<LeadPriority, string> = {
  now: "text-danger",
  week: "text-warn",
  later: "text-dim",
};

/** A lead is off this screen once the rep has said what happened to it. */
function stillToDo(lead: Lead): boolean {
  return lead.status === "new" || lead.status === "callback";
}

export default function QueuePage() {
  const router = useRouter();

  const [searches, setSearches] = useState<LeadSearch[] | null>(null);
  const [searchId, setSearchId] = useState<string | null>(null);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /* "later" starts folded away. It is the biggest lane and the one nothing in
     it needs doing today, so open it costs the rep a scroll past forty rows to
     reach the two that matter. */
  const [openLater, setOpenLater] = useState(false);

  useEffect(() => {
    const stop = new AbortController();
    void (async () => {
      const answer = await fetchSearches(stop.signal);
      if (stop.signal.aborted) return;
      if (answer.kind === "error") {
        setError(answer.message);
        setSearches([]);
        return;
      }
      setSearches(answer.searches);
      const saved = readLastSearch();
      const pick =
        answer.searches.find((s) => s.id === saved)?.id ??
        answer.searches[0]?.id ??
        null;
      setSearchId(pick);
    })();
    return () => stop.abort();
  }, []);

  useEffect(() => {
    if (!searchId) return;
    const stop = new AbortController();
    /* The request is the external system this effect drives, and the rep has to
       be told it started. One render on a search change is the cost. */
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setError(null);
    void (async () => {
      const answer = await fetchLeads(searchId, { sort: "value" }, stop.signal);
      if (stop.signal.aborted) return;
      setLoading(false);
      if (answer.kind === "error") {
        setError(answer.message);
        setLeads([]);
        return;
      }
      setLeads(answer.leads);
    })();
    return () => stop.abort();
  }, [searchId]);

  const changeSearch = useCallback((id: string) => {
    setSearchId(id);
    writeLastSearch(id);
  }, []);

  /* Grouped once, not per lane per priority. Six passes over sixty five rows is
     nothing, but the shape is what matters: one place decides which lane and
     which band a lead is in, so the counts in the headings and the rows under
     them can never disagree. */
  const grouped = useMemo(() => {
    const table = new Map<string, Lead[]>();
    let waiting = 0;
    for (const lead of leads) {
      if (!stillToDo(lead)) continue;
      waiting += 1;
      const track: LeadTrack = lead.track === "SEO" ? "SEO" : "WEBSITE";
      const slot = `${track}|${lead.priority}`;
      const bucket = table.get(slot);
      if (bucket) bucket.push(lead);
      else table.set(slot, [lead]);
    }
    return { table, waiting };
  }, [leads]);

  const openLead = useCallback(
    (lead: Lead) => {
      /* Straight to the calling list with this lead's drawer open. The queue
         says who is next, the list is where a call is actually started, and
         duplicating the whole call flow here would be a second place for it to
         go wrong. */
      router.push(`/leads?search=${encodeURIComponent(searchId ?? "")}&open=${encodeURIComponent(lead.key)}`);
    },
    [router, searchId],
  );

  const totalLeft = grouped.waiting;

  return (
    <LeadChrome
      title="What to do today."
      lede="The same businesses, in the order worth working them. Top of each column first, then down. Anything you have already dealt with is gone from here."
      action={
        searches && searches.length > 1 ? (
          <select
            aria-label="Pick a search"
            value={searchId ?? ""}
            onChange={(event) => changeSearch(event.target.value)}
            className="h-[26px] rounded-hair border border-line-strong bg-surface-2 px-2 font-mono text-micro uppercase text-text"
          >
            {searches.map((s) => (
              <option key={s.id} value={s.id}>
                {s.niche} in {s.location} ({s.leads})
              </option>
            ))}
          </select>
        ) : null
      }
    >
      {error ? <Notice tone="danger" text={error} /> : null}

      {!error && searches !== null && searches.length === 0 ? (
        <Notice
          tone="warn"
          text="There are no searches yet, so there is nothing to work through."
        >
          <Link href="/leads" className={`${MICRO_BUTTON} w-fit`}>
            Start a search
          </Link>
        </Notice>
      ) : null}

      {loading && leads.length === 0 ? (
        <p className="font-mono text-micro uppercase text-muted">Working out the order</p>
      ) : null}

      {leads.length > 0 && totalLeft === 0 ? (
        <div className="flex flex-col items-center gap-3 bg-surface px-4 py-16 text-center">
          <span className="font-mono text-micro uppercase text-muted">Nothing left</span>
          <p className="max-w-[46ch] text-body text-muted">
            Every business in this search has been dealt with. Start a new search when you want
            more.
          </p>
        </div>
      ) : null}

      {totalLeft > 0 ? (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          {LANES.map((lane) => {
            const laneTotal = PRIORITY_ORDER.reduce(
              (n, p) => n + (grouped.table.get(`${lane.track}|${p}`)?.length ?? 0),
              0,
            );
            const laneMoney = PRIORITY_ORDER.reduce(
              (n, p) =>
                n +
                (grouped.table.get(`${lane.track}|${p}`) ?? []).reduce(
                  (m, l) => m + l.dealValue,
                  0,
                ),
              0,
            );

            return (
              <section key={lane.track} aria-label={lane.label} className="seam-grid grid-cols-1">
                <div className="flex flex-col gap-1 bg-surface px-4 py-3">
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="font-sans text-idle text-text">{lane.label}</span>
                    <span className="tabnum font-mono text-micro uppercase text-muted">
                      {laneTotal} left · {shortMoney(laneMoney)}
                    </span>
                  </div>
                  <span className="text-body text-dim">{lane.blurb}</span>
                </div>

                {laneTotal === 0 ? (
                  <div className="bg-surface px-4 py-8 text-center">
                    <span className="font-mono text-micro uppercase text-muted">
                      Nothing in this column
                    </span>
                  </div>
                ) : null}

                {PRIORITY_ORDER.map((priority) => {
                  const rows = grouped.table.get(`${lane.track}|${priority}`) ?? [];
                  if (rows.length === 0) return null;
                  const folded = priority === "later" && !openLater;
                  const Icon = PRIORITY_ICON[priority];

                  return (
                    <div key={priority} className="seam-grid grid-cols-1">
                      <button
                        type="button"
                        onClick={
                          priority === "later" ? () => setOpenLater((v) => !v) : undefined
                        }
                        aria-expanded={priority === "later" ? openLater : undefined}
                        className={`flex items-center gap-2 bg-surface-2 px-4 py-2 text-left ${
                          priority === "later" ? "cursor-pointer hover:bg-surface" : "cursor-default"
                        }`}
                      >
                        <Icon
                          aria-hidden="true"
                          className={`h-3.5 w-3.5 shrink-0 ${PRIORITY_TONE[priority]}`}
                        />
                        <span className="font-mono text-micro uppercase text-text">
                          {PRIORITY_LABELS[priority]}
                        </span>
                        <span className="tabnum font-mono text-micro text-dim">{rows.length}</span>
                        <span className="ml-auto truncate text-body text-dim">
                          {folded ? "Show them" : PRIORITY_BLURBS[priority]}
                        </span>
                      </button>

                      {folded
                        ? null
                        : rows.map((lead, index) => (
                            <QueueRow
                              key={lead.key}
                              lead={lead}
                              number={index + 1}
                              onOpen={() => openLead(lead)}
                            />
                          ))}
                    </div>
                  );
                })}
              </section>
            );
          })}
        </div>
      ) : null}
    </LeadChrome>
  );
}

/**
 * One business in the queue.
 *
 * Numbered, because the whole promise of this screen is "start at one and go
 * down". A list of equals makes the rep choose again on every row, which is the
 * work this screen exists to take off them.
 */
function QueueRow({
  lead,
  number,
  onOpen,
}: {
  lead: Lead;
  number: number;
  onOpen(): void;
}) {
  const callable = canCall(lead);
  return (
    <div className="flex items-center gap-3 bg-surface px-4 py-3">
      <span className="tabnum w-5 shrink-0 font-mono text-micro text-dim">{number}</span>

      <button
        type="button"
        onClick={onOpen}
        className="flex min-w-0 flex-1 flex-col items-start gap-0.5 text-left"
      >
        <span className="truncate font-sans text-body text-text">{lead.name}</span>
        <span className="truncate text-body text-dim">{lead.why}</span>
      </button>

      <span className="tabnum shrink-0 font-sans text-body text-accent">
        {shortMoney(lead.dealValue, lead.currency)}
      </span>

      {callable ? (
        <button type="button" onClick={onOpen} className={`${SMALL_BUTTON} shrink-0`}>
          <Phone aria-hidden="true" className="h-3 w-3" />
          Open
          <ArrowRight aria-hidden="true" className="h-3 w-3" />
        </button>
      ) : (
        <span
          title="Google has no phone number for this one."
          className={`${SMALL_BUTTON} shrink-0 cursor-not-allowed`}
        >
          <PhoneOff aria-hidden="true" className="h-3 w-3" />
          No number
        </span>
      )}
    </div>
  );
}
