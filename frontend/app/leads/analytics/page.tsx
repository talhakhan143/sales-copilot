"use client";

/**
 * What the list is worth, and how much of it is real.
 *
 * Every number on this screen is a guess built on another guess. The deal value
 * comes from a price band matched to the business category, the close odds come
 * from a table indexed by urgency, and neither has ever met this prospect. So
 * every figure here says underneath it where it came from, because the failure
 * mode of a money screen is not a wrong number, it is a rep who believes it.
 *
 * It is read only on purpose. Nothing here changes a lead, so a rep can look at
 * it mid call without the risk of pressing something.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";

import {
  FIELD_LABEL,
  LeadChrome,
  MICRO_BUTTON,
  Stat,
  fullMoney,
  shortMoney,
} from "@/components/LeadChrome";
import { Notice } from "@/components/Notice";
import { fetchLeads, fetchSearches, readLastSearch, writeLastSearch } from "@/lib/leads";
import type { Lead, LeadSearch } from "@/lib/leads";
import { PRIORITY_LABELS, PRIORITY_ORDER } from "@/lib/types";

/** The statuses that mean the rep has actually spoken to them. */
const REACHED = new Set(["interested", "callback", "not_interested", "won", "lost"]);

/** The statuses that mean it went somewhere. */
const WARM = new Set(["interested", "callback", "won"]);

export default function AnalyticsPage() {
  const [searches, setSearches] = useState<LeadSearch[] | null>(null);
  const [searchId, setSearchId] = useState<string | null>(null);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
      setSearchId(
        answer.searches.find((s) => s.id === saved)?.id ?? answer.searches[0]?.id ?? null,
      );
    })();
    return () => stop.abort();
  }, []);

  useEffect(() => {
    if (!searchId) return;
    const stop = new AbortController();
    /* Same as the queue: starting the fetch is the effect's whole job and the
       screen has to say so while it runs. */
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

  const sums = useMemo(() => {
    const symbol = leads[0]?.currency || "$";

    let contract = 0;
    let expected = 0;
    let reached = 0;
    let warm = 0;
    let won = 0;
    let wonValue = 0;
    let withPhone = 0;
    let withCopy = 0;

    const byPriority = new Map<string, { count: number; value: number }>();
    const byTrack = new Map<string, { count: number; value: number }>();
    const byCategory = new Map<string, { count: number; value: number }>();

    for (const lead of leads) {
      contract += lead.contractValue;
      expected += lead.expectedValue;
      if (REACHED.has(lead.status)) reached += 1;
      if (WARM.has(lead.status)) warm += 1;
      if (lead.status === "won") {
        won += 1;
        wonValue += lead.contractValue;
      }
      if (lead.phone) withPhone += 1;
      if (lead.messageCount > 0) withCopy += 1;

      for (const [table, slot] of [
        [byPriority, lead.priority] as const,
        [byTrack, lead.track === "SEO" ? "SEO" : "WEBSITE"] as const,
        [byCategory, lead.category || "Other"] as const,
      ]) {
        const entry = table.get(slot) ?? { count: 0, value: 0 };
        entry.count += 1;
        entry.value += lead.contractValue;
        table.set(slot, entry);
      }
    }

    return {
      symbol,
      total: leads.length,
      contract,
      expected,
      reached,
      warm,
      won,
      wonValue,
      withPhone,
      withCopy,
      average: leads.length ? Math.round(contract / leads.length) : 0,
      replyRate: reached ? Math.round((warm / reached) * 100) : 0,
      closeRate: reached ? Math.round((won / reached) * 100) : 0,
      byPriority,
      byTrack,
      topCategories: [...byCategory.entries()].sort((a, b) => b[1].value - a[1].value).slice(0, 8),
    };
  }, [leads]);

  const empty = !loading && leads.length === 0 && !error;

  return (
    <LeadChrome
      title="What this list is worth."
      lede="Nobody has agreed to any of this. These are the prices the audit worked out and the odds it guessed, so treat the big number as the size of the opportunity, not as money."
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

      {searches !== null && searches.length === 0 ? (
        <Notice tone="warn" text="There are no searches yet, so there is nothing to add up.">
          <Link href="/leads" className={`${MICRO_BUTTON} w-fit`}>
            Start a search
          </Link>
        </Notice>
      ) : null}

      {loading && leads.length === 0 ? (
        <p className="font-mono text-micro uppercase text-muted">Adding it up</p>
      ) : null}

      {empty && searches !== null && searches.length > 0 ? (
        <p className="font-mono text-micro uppercase text-muted">
          This search has no leads to add up yet
        </p>
      ) : null}

      {leads.length > 0 ? (
        <div className="flex flex-col gap-6">
          <div className="seam-grid grid-cols-2 lg:grid-cols-4">
            <Stat
              label="Whole list"
              value={shortMoney(sums.contract, sums.symbol)}
              sub={`If every one of the ${sums.total} said yes`}
              tone="accent"
            />
            <Stat
              label="Weighted"
              value={shortMoney(sums.expected, sums.symbol)}
              sub="The same list times the odds the audit guessed"
            />
            <Stat
              label="Average deal"
              value={fullMoney(sums.average, sums.symbol)}
              sub="Per business, over the whole life of the deal"
            />
            <Stat
              label="Won"
              value={shortMoney(sums.wonValue, sums.symbol)}
              sub={
                sums.reached
                  ? `${sums.won} of the ${sums.reached} you have spoken to`
                  : "You have not spoken to anyone yet"
              }
              tone="ok"
            />
          </div>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <Panel title="When to call them" note="From the audit, not from you.">
              {PRIORITY_ORDER.map((priority) => {
                const row = sums.byPriority.get(priority) ?? { count: 0, value: 0 };
                return (
                  <Bar
                    key={priority}
                    label={PRIORITY_LABELS[priority]}
                    count={row.count}
                    of={sums.total}
                    right={shortMoney(row.value, sums.symbol)}
                  />
                );
              })}
            </Panel>

            <Panel
              title="What you are selling them"
              note="A first website, or the work to make an existing one earn."
            >
              {(["WEBSITE", "SEO"] as const).map((track) => {
                const row = sums.byTrack.get(track) ?? { count: 0, value: 0 };
                return (
                  <Bar
                    key={track}
                    label={track === "SEO" ? "Has a website" : "Needs a website"}
                    count={row.count}
                    of={sums.total}
                    right={shortMoney(row.value, sums.symbol)}
                  />
                );
              })}
            </Panel>

            <Panel
              title="How far the calls got"
              note="Counted from the outcomes you picked, so this one is real."
            >
              <Bar label="In the list" count={sums.total} of={sums.total} right="" />
              <Bar
                label="Spoken to"
                count={sums.reached}
                of={sums.total}
                right={sums.total ? `${Math.round((sums.reached / sums.total) * 100)}%` : ""}
              />
              <Bar
                label="Went somewhere"
                count={sums.warm}
                of={sums.total}
                right={sums.reached ? `${sums.replyRate}% of those` : ""}
              />
              <Bar
                label="Won"
                count={sums.won}
                of={sums.total}
                right={sums.reached ? `${sums.closeRate}% of those` : ""}
              />
            </Panel>

            <Panel title="Ready to work" note="What is actually in hand right now.">
              <Bar
                label="Have a phone number"
                count={sums.withPhone}
                of={sums.total}
                right={`${sums.total - sums.withPhone} without`}
              />
              <Bar
                label="Have messages written"
                count={sums.withCopy}
                of={sums.total}
                right={sums.withCopy === 0 ? "Copywriter has not run" : ""}
              />
            </Panel>
          </div>

          {sums.topCategories.length > 1 ? (
            <Panel title="Where the money is" note="By what Google calls them.">
              {sums.topCategories.map(([name, row]) => (
                <Bar
                  key={name}
                  label={name}
                  count={row.count}
                  of={sums.total}
                  right={shortMoney(row.value, sums.symbol)}
                />
              ))}
            </Panel>
          ) : null}
        </div>
      ) : null}
    </LeadChrome>
  );
}

function Panel({
  title,
  note,
  children,
}: {
  title: string;
  note: string;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-4 bg-surface p-5">
      <div className="flex flex-col gap-1">
        <span className={FIELD_LABEL}>{title}</span>
        <span className="text-body text-dim">{note}</span>
      </div>
      <div className="flex flex-col gap-2.5">{children}</div>
    </section>
  );
}

/**
 * One row with a bar behind it.
 *
 * The bar is a background on the label row rather than its own element, so the
 * number and the bar can never end up on different lines when a long category
 * name wraps.
 */
function Bar({
  label,
  count,
  of,
  right,
}: {
  label: string;
  count: number;
  of: number;
  right: string;
}) {
  const pct = of > 0 ? Math.round((count / of) * 100) : 0;
  return (
    <div className="flex items-center gap-3">
      <span className="relative min-w-0 flex-1 overflow-hidden rounded-hair bg-surface-2">
        <span
          aria-hidden="true"
          className="absolute inset-y-0 left-0 bg-accent/20"
          style={{ width: `${pct}%` }}
        />
        <span className="relative flex items-center justify-between gap-3 px-2.5 py-1.5">
          <span className="truncate text-body text-text">{label}</span>
          <span className="tabnum shrink-0 font-mono text-micro text-muted">{count}</span>
        </span>
      </span>
      <span className="tabnum w-[92px] shrink-0 text-right font-mono text-micro text-dim">
        {right}
      </span>
    </div>
  );
}
