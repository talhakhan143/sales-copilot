"use client";

/**
 * The leads screen.
 *
 * This page is the shell and the state. It owns which search is open, the
 * filter, the sort, every fetch and every error, and it hands finished data
 * down to three children that fetch nothing: the picker at the top, the list
 * under it, and the drawer over both.
 *
 * The day this page exists for:
 *
 *     pick a search  ->  read the top row  ->  press Call
 *                    ->  the notes are already built from the audit
 *                    ->  the teleprompter opens
 *
 * So the two things that must never be slow are pressing a filter chip and
 * pressing Call. A chip press is filtered in memory by LeadList over rows that
 * are already here. Call posts once and routes straight onto the glass, with no
 * result card in between.
 *
 * Starting a new search opens a real Chromium window that scrolls Google Maps
 * for minutes. That is far too long to show nothing, so the job is polled and
 * its own last line is printed in the picker until it ends.
 *
 * Nothing in lib/leads.ts throws. Every call comes back as a result with a plain
 * sentence in it, so every failure on this page is a line of text on the screen
 * and never a blank page between two calls. A failure that belongs to one lead
 * is handed to LeadList with that lead's key, and LeadList draws it under that
 * row. A notice at the top of the page is off screen at row 40.
 *
 * Keyboard: "n" starts a new search. "/" focuses the filter, and that one lives
 * in LeadList, because the component that owns a control owns its shortcut.
 * Both are dead while a field has focus and dead while a modifier is held.
 */

import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, GraduationCap, Plus, TriangleAlert, X } from "lucide-react";

import { LeadDetail } from "@/components/LeadDetail";
import { LeadList } from "@/components/LeadList";
import { SearchPicker } from "@/components/SearchPicker";
import {
  canCall,
  fetchJob,
  fetchLead,
  fetchLeads,
  fetchSearches,
  startLeadCall,
  startSearch,
} from "@/lib/leads";
import type {
  Lead,
  LeadDetail as LeadFull,
  LeadBucket,
  LeadSearch,
  LeadSort,
  LeadsFailure,
  ScrapeJob,
} from "@/lib/leads";
import { loadProfile } from "@/lib/profile";
import { saveSession } from "@/lib/session";
import type { LeadCallSession } from "@/lib/types";

/** How often the scrape job is asked what it is doing. */
const POLL_MS = 2000;

/** How many polls in a row may fail before the job is called dead. */
const POLL_FAILS = 5;

/** How long a finished job stays on screen before it clears itself. */
const DONE_MS = 8000;

/** Which search was open last. A per browser convenience, never a source of truth. */
const LAST_SEARCH_KEY = "salescopilot:leadSearch";

const HOW_MANY = [20, 40, 60, 100];

const COUNTRIES: { code: string; label: string }[] = [
  { code: "us", label: "United States" },
  { code: "gb", label: "United Kingdom" },
  { code: "ca", label: "Canada" },
  { code: "au", label: "Australia" },
  { code: "ae", label: "United Arab Emirates" },
  { code: "pk", label: "Pakistan" },
  { code: "in", label: "India" },
];

const FIELD_LABEL = "font-mono text-micro uppercase text-muted";
const FIELD_INPUT =
  "h-11 w-full rounded-hair border border-line-strong bg-surface-2 px-3 font-sans text-body text-text placeholder:text-dim";
const FIELD_SELECT =
  "h-11 w-full rounded-hair border border-line-strong bg-surface-2 px-3 font-sans text-body text-text";

/**
 * The small text buttons, 22px of ink and 44px of hit area.
 *
 * The invisible pseudo element is the same one the drawer's close button uses.
 * It paints nothing and takes no space in the layout, so the control still
 * reads as one small line of type while a thumb still lands on it.
 */
const MICRO_BUTTON =
  "relative flex h-[22px] shrink-0 items-center gap-1.5 rounded-hair border border-line-strong px-2 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] ease-out before:absolute before:-inset-y-[11px] before:-inset-x-2 before:content-[''] hover:bg-surface-2 hover:text-text";

/**
 * The one button on this screen that is not Call, so it is not filled.
 *
 * Call is the only filled control on the leads screen. Anything else with a
 * solid fill competes with it, and the whole point of the row is that the eye
 * finds the cyan block without looking for it.
 */
const START_BUTTON =
  "flex h-11 w-full items-center justify-center gap-2 rounded-hair border border-line-strong bg-surface-2 font-mono text-[12px] font-semibold uppercase tracking-[0.12em] text-text transition-colors duration-[120ms] ease-out hover:border-accent hover:text-accent disabled:pointer-events-none disabled:opacity-[.38] sm:w-[240px]";

/**
 * What actually goes into localStorage when a lead call is built.
 *
 * The two extra field names are not free choices. The call page reads exactly
 * `leadKey`, `leadName`, `searchId` and `clientPhone` back out of this record,
 * and it reads `leadKey` and `searchId` off the address bar under those same
 * two names. Rename one here and the whole outcome loop on the other side goes
 * quiet, because every part of it is behind "this call came from a lead".
 */
type StoredLeadSession = LeadCallSession & {
  /** The lead's own number, so the call page does not ask for it again. */
  clientPhone: string | null;
  /** Which search this call came out of, so the outcome can be written back. */
  searchId: string;
};

function isEditable(node: unknown): boolean {
  if (!node || typeof node !== "object") return false;
  const el = node as HTMLElement;
  const tag = typeof el.tagName === "string" ? el.tagName.toLowerCase() : "";
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  return el.isContentEditable === true;
}

/** A failure's two lines joined into the one string a child prop takes. */
function sentence(failure: LeadsFailure): string {
  return failure.hint ? `${failure.message} ${failure.hint}` : failure.message;
}

function readLastSearch(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const value = window.localStorage.getItem(LAST_SEARCH_KEY);
    return value && value.trim().length > 0 ? value : null;
  } catch {
    return null;
  }
}

function writeLastSearch(id: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LAST_SEARCH_KEY, id);
  } catch {
    // Storage is off. Losing which search was open costs one click.
  }
}

/** Keep the wanted search when it still exists, otherwise fall to the newest. */
function pickSearch(list: LeadSearch[], wanted: string | null): string | null {
  if (wanted && list.some((search) => search.id === wanted)) return wanted;
  return list.length > 0 ? list[0].id : null;
}

interface NoticeProps {
  tone: "danger" | "warn";
  text: string;
  children?: ReactNode;
}

/** One notice, in the house shape: a rule down the left, an icon, one sentence. */
function Notice({ tone, text, children }: NoticeProps) {
  const border = tone === "danger" ? "border-danger" : "border-warn";
  const icon = tone === "danger" ? "text-danger" : "text-warn";
  return (
    <div className={`flex gap-3 border-l-2 ${border} bg-surface-2 p-4`}>
      <TriangleAlert aria-hidden="true" className={`mt-0.5 h-4 w-4 shrink-0 ${icon}`} />
      <div className="flex min-w-0 flex-col gap-2">
        <p className="text-body text-muted">{text}</p>
        {children}
      </div>
    </div>
  );
}

export default function LeadsPage() {
  const router = useRouter();
  const nicheId = useId();
  const locationId = useId();
  const limitId = useId();
  const countryId = useId();
  const nicheRef = useRef<HTMLInputElement | null>(null);

  /* null means the searches have not been read yet, which is not the same as
     "there are none". The empty state must not flash on every load. */
  const [searches, setSearches] = useState<LeadSearch[] | null>(null);
  const [searchesError, setSearchesError] = useState<string | null>(null);
  const [searchId, setSearchId] = useState<string | null>(null);

  const [leads, setLeads] = useState<Lead[]>([]);
  const [loading, setLoading] = useState(false);
  const [leadsError, setLeadsError] = useState<string | null>(null);
  const [dropped, setDropped] = useState(0);
  const shownRef = useRef<string | null>(null);

  /* Three tabs, and a separate toggle for the rows that cannot be dialled.
     Both live here and not in LeadList so that pressing Call back on the
     drawer can send the rep to the tab that lead just moved to. */
  const [bucket, setBucket] = useState<LeadBucket>("to_call");
  const [phoneOnly, setPhoneOnly] = useState(false);
  const [sort, setSort] = useState<LeadSort>("value");

  const [detailKey, setDetailKey] = useState<string | null>(null);
  const [detailLead, setDetailLead] = useState<LeadFull | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [callingKey, setCallingKey] = useState<string | null>(null);
  const [callError, setCallError] = useState<string | null>(null);
  const [noProfile, setNoProfile] = useState(false);
  /* Which row the press that failed came from. The sentence goes under that
     row, so the rep reads it where their finger already is. */
  const [failKey, setFailKey] = useState<string | null>(null);

  const [newOpen, setNewOpen] = useState(false);
  const [niche, setNiche] = useState("");
  const [location, setLocation] = useState("");
  const [limit, setLimit] = useState(60);
  const [country, setCountry] = useState("us");
  const [starting, setStarting] = useState(false);
  const [newError, setNewError] = useState<string | null>(null);

  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<ScrapeJob | null>(null);

  const busy = jobId !== null;

  /* ============================================================
     SEARCHES
     ============================================================ */

  const reloadSearches = useCallback(async (prefer?: string, signal?: AbortSignal) => {
    const result = await fetchSearches(signal);
    if (signal && signal.aborted) return;

    if (result.kind === "error") {
      setSearches((current) => current ?? []);
      setSearchesError(sentence(result));
      return;
    }

    setSearches(result.searches);
    setSearchesError(null);
    setSearchId((current) => pickSearch(result.searches, prefer ?? current));
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const wanted = readLastSearch();
    /* Every state write inside reloadSearches happens after an await, so this is
       not a synchronous cascade. The rule cannot see through the async call. */
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reloadSearches(wanted ?? undefined, controller.signal);
    return () => controller.abort();
  }, [reloadSearches]);

  /* ============================================================
     LEADS
     ============================================================ */

  useEffect(() => {
    /* Nothing to read when no search is open. The list is not rendered in that
       case either, so whatever is still in state can never reach the screen. */
    if (!searchId) return;

    /* A new search means the old rows are wrong. A new sort means the same rows
       in another order, so they stay on screen while the server answers. */
    if (shownRef.current !== searchId) {
      setLeads([]);
      shownRef.current = searchId;
    }

    let live = true;
    const controller = new AbortController();
    /* The request is the external system this effect drives, and the rep has to
       be told it started. One render on a search or sort change is the cost. */
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setLeadsError(null);

    void fetchLeads(searchId, sort, controller.signal).then((result) => {
      if (!live) return;
      if (result.kind === "error") {
        setLeads([]);
        setDropped(0);
        setLeadsError(sentence(result));
      } else {
        setLeads(result.leads);
        setDropped(result.dropped);
        setLeadsError(null);
      }
      setLoading(false);
    });

    return () => {
      live = false;
      controller.abort();
    };
  }, [searchId, sort]);

  const changeSearch = useCallback((id: string) => {
    setSearchId(id);
    setDetailKey(null);
    setCallError(null);
    setNoProfile(false);
    setFailKey(null);
    writeLastSearch(id);
  }, []);

  /* ============================================================
     THE DRAWER
     ============================================================ */

  useEffect(() => {
    if (!searchId || !detailKey) return;

    let live = true;
    const controller = new AbortController();
    /* The drawer is mounted already, showing its waiting state. This tells it
       the read is out, which is the state it draws until the answer lands. */
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setDetailLoading(true);
    setDetailLead(null);
    setDetailError(null);

    void fetchLead(searchId, detailKey, controller.signal).then((result) => {
      if (!live) return;
      if (result.kind === "error") setDetailError(sentence(result));
      else setDetailLead(result.lead);
      setDetailLoading(false);
    });

    return () => {
      live = false;
      controller.abort();
    };
  }, [searchId, detailKey]);

  const openDetail = useCallback((lead: Lead) => setDetailKey(lead.key), []);
  const closeDetail = useCallback(() => setDetailKey(null), []);

  /* ============================================================
     THE SCRAPE JOB
     ============================================================ */

  useEffect(() => {
    if (!jobId) return;

    let live = true;
    let timer = 0;
    let fails = 0;

    const tick = async () => {
      const result = await fetchJob(jobId);
      if (!live) return;

      if (result.kind === "error") {
        /* One failed poll is not a failed search. The scrape runs in its own
           process, so a hiccup between here and the server means nothing. */
        fails += 1;
        if (fails >= POLL_FAILS) {
          setJobId(null);
          setJob({ state: "failed", step: "", line: sentence(result), searchId: "", done: true });
          return;
        }
        timer = window.setTimeout(() => void tick(), POLL_MS);
        return;
      }

      fails = 0;
      const next = result.job;
      setJob(next);

      if (next.state === "running") {
        timer = window.setTimeout(() => void tick(), POLL_MS);
        return;
      }

      setJobId(null);
      if (next.state === "finished") {
        setNewOpen(false);
        if (next.searchId) writeLastSearch(next.searchId);
        void reloadSearches(next.searchId || undefined);
      }
    };

    timer = window.setTimeout(() => void tick(), 0);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [jobId, reloadSearches]);

  /* A finished job clears itself. A failed one stays, because the rep has to
     read why before they try again. */
  useEffect(() => {
    if (!job || job.state !== "finished") return;
    const timer = window.setTimeout(() => setJob(null), DONE_MS);
    return () => window.clearTimeout(timer);
  }, [job]);

  /* ============================================================
     NEW SEARCH
     ============================================================ */

  const openNew = useCallback(() => {
    if (busy) return;
    setNewError(null);
    setNewOpen(true);
  }, [busy]);

  useEffect(() => {
    if (newOpen) nicheRef.current?.focus();
  }, [newOpen]);

  useEffect(() => {
    function onKey(event: globalThis.KeyboardEvent) {
      if (event.key !== "n" && event.key !== "N") return;
      if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;
      if (isEditable(event.target) || isEditable(document.activeElement)) return;
      event.preventDefault();
      openNew();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openNew]);

  const submitNew = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (busy || starting) return;

      setStarting(true);
      setNewError(null);

      /* startSearch checks the two boxes itself and answers with a sentence, so
         that rule has one home and it sits beside the request it guards. */
      const result = await startSearch({ niche, location, limit, country });
      setStarting(false);

      if (result.kind === "error") {
        setNewError(sentence(result));
        return;
      }
      setJobId(result.jobId);
    },
    [busy, country, limit, location, niche, starting],
  );

  const onFormKey = useCallback((event: ReactKeyboardEvent<HTMLFormElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      setNewOpen(false);
    }
  }, []);

  /* ============================================================
     THE CALL
     ============================================================ */

  const callLead = useCallback(
    async (lead: Lead) => {
      if (!searchId || callingKey || !canCall(lead)) return;

      /* Checked here as well as inside lib/leads.ts, because this is the only
         place that can also offer the way to fix it. The drawer is put away
         either way: neither failure can be fixed from inside it, and the
         sentence that says so is drawn under the row it belongs to. */
      const profile = loadProfile();
      const knowledgeBase = profile ? profile.knowledgeBase.trim() : "";
      if (!knowledgeBase) {
        setNoProfile(true);
        setCallError(null);
        setFailKey(lead.key);
        setDetailKey(null);
        return;
      }

      const language = profile && profile.language ? profile.language : "en";
      setNoProfile(false);
      setCallError(null);
      setFailKey(null);
      setCallingKey(lead.key);

      const result = await startLeadCall(searchId, lead.key, { knowledgeBase, language });
      if (result.kind === "error") {
        setCallingKey(null);
        setCallError(sentence(result));
        setFailKey(lead.key);
        setDetailKey(null);
        return;
      }

      /* Two fields more than the session itself: the lead's number, so the call
         page can dial without asking for it again, and the search id, so the
         outcome the rep picks afterwards lands on the right lead. Both names
         are the ones the call page reads. */
      const stored: StoredLeadSession = {
        ...result.session,
        clientPhone: lead.phone ? lead.phone.trim() : null,
        searchId,
      };
      saveSession(stored);
      writeLastSearch(searchId);

      /* Straight onto the glass. The search and the lead ride in the address bar
         as well as in storage, so the call page can ask what happened and offer
         the next lead even after a hard refresh. These two names match the ones
         the call page reads off the query string, and the ones it writes itself
         when it moves the rep on to the next lead. */
      const query = [
        `session=${encodeURIComponent(result.session.sessionId)}`,
        `leadKey=${encodeURIComponent(lead.key)}`,
        `searchId=${encodeURIComponent(searchId)}`,
      ].join("&");
      router.push(`/call?${query}`);
    },
    [callingKey, router, searchId],
  );

  const callFromDrawer = useCallback(() => {
    if (detailLead) void callLead(detailLead);
  }, [callLead, detailLead]);

  /* The one sentence a failed press produces, ready to be dropped under the row
     that produced it. Only one of the two can be on at a time. */
  const rowNotice: ReactNode = noProfile ? (
    <Notice
      tone="warn"
      text="The copilot does not know what you sell yet, so it cannot write your lines. Fill that in once and come back here."
    >
      <Link href="/" className={`${MICRO_BUTTON} w-fit`}>
        Tell it what you sell
      </Link>
    </Notice>
  ) : callError ? (
    <Notice tone="danger" text={callError} />
  ) : null;

  return (
    <div className="min-h-dvh bg-bg">
      <header className="sticky top-0 z-20 flex h-14 items-center justify-between gap-4 border-b border-line bg-bg/95 px-6">
        <div className="flex items-center gap-3">
          <span className="h-2 w-2 shrink-0 bg-accent animate-mark" aria-hidden="true" />
          <span className="font-mono text-micro uppercase text-muted">Sales copilot</span>
        </div>
        <div className="flex items-center gap-2">
          {/* Practice lives on the setup page, and before this the only way to
              reach it was to leave the list, scroll past the lead panel and find
              a segmented control. A rep who spends the whole day here could not
              find it at all, so the door is on this screen and it opens straight
              onto practice. */}
          <Link
            href="/?mode=practice"
            title="Train against a robot client before you ring a real one"
            className={MICRO_BUTTON}
          >
            <GraduationCap aria-hidden="true" className="h-3 w-3" />
            Practice call
          </Link>
          <Link href="/" className={MICRO_BUTTON}>
            <ArrowLeft aria-hidden="true" className="h-3 w-3" />
            Set up one call
          </Link>
        </div>
      </header>

      <main className="mx-auto w-full max-w-[1280px] px-6 pb-24 pt-10">
        <h1 className="text-h1 text-text">Pick who to call next.</h1>
        <p className="mt-2 max-w-[62ch] text-lede text-muted">
          These are the businesses from your search, best first. Press Call and your notes are
          already written from what we found on their website.
        </p>

        {/* One seam grid around the whole screen, so every hairline between the
            picker, the form, a notice and the list is the same 1 px of
            --line-strong showing through the gap. A margin between separate
            blocks would show the page ground instead and break the rhythm. */}
        <div className="seam-grid mt-8 grid-cols-1">
          {searches === null ? (
            <div className="bg-surface px-4 py-10">
              <span className="font-mono text-micro uppercase text-muted">
                Reading your searches
              </span>
            </div>
          ) : (
            <SearchPicker
              searches={searches}
              value={searchId}
              onChange={changeSearch}
              onNewSearch={openNew}
              busy={busy}
              job={job}
            />
          )}

          {searchesError ? <Notice tone="danger" text={searchesError} /> : null}

          {newOpen ? (
            <form onSubmit={submitNew} onKeyDown={onFormKey} className="seam-grid grid-cols-1">
              <div className="flex flex-col gap-4 bg-surface px-4 py-4">
                <div className="flex items-center justify-between gap-3">
                  <span className={FIELD_LABEL}>New search</span>
                  <button type="button" onClick={() => setNewOpen(false)} className={MICRO_BUTTON}>
                    <X aria-hidden="true" className="h-3 w-3" />
                    Close
                  </button>
                </div>

                <p className="max-w-[62ch] font-sans text-[12px] leading-[18px] text-muted">
                  A browser window opens and scrolls Google Maps for you. It takes a few minutes.
                  Leave that window alone until it closes on its own.
                </p>

                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
                  <div className="flex flex-col gap-1.5">
                    <label htmlFor={nicheId} className={FIELD_LABEL}>
                      Kind of business
                    </label>
                    <input
                      id={nicheId}
                      ref={nicheRef}
                      value={niche}
                      onChange={(event) => setNiche(event.target.value)}
                      placeholder="dentist"
                      autoComplete="off"
                      spellCheck={false}
                      className={FIELD_INPUT}
                    />
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <label htmlFor={locationId} className={FIELD_LABEL}>
                      Town or city
                    </label>
                    <input
                      id={locationId}
                      value={location}
                      onChange={(event) => setLocation(event.target.value)}
                      placeholder="hoboken"
                      autoComplete="off"
                      spellCheck={false}
                      className={FIELD_INPUT}
                    />
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <label htmlFor={limitId} className={FIELD_LABEL}>
                      How many
                    </label>
                    <select
                      id={limitId}
                      value={String(limit)}
                      onChange={(event) => setLimit(Number(event.target.value))}
                      className={FIELD_SELECT}
                    >
                      {HOW_MANY.map((count) => (
                        <option key={count} value={String(count)}>
                          {count} businesses
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <label htmlFor={countryId} className={FIELD_LABEL}>
                      Country
                    </label>
                    <select
                      id={countryId}
                      value={country}
                      onChange={(event) => setCountry(event.target.value)}
                      className={FIELD_SELECT}
                    >
                      {COUNTRIES.map((item) => (
                        <option key={item.code} value={item.code}>
                          {item.label}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>

                {newError ? (
                  <div className="flex gap-3 border-l-2 border-danger bg-surface-2 p-3">
                    <TriangleAlert
                      aria-hidden="true"
                      className="mt-0.5 h-4 w-4 shrink-0 text-danger"
                    />
                    <p className="text-body text-muted">{newError}</p>
                  </div>
                ) : null}

                <button type="submit" disabled={busy || starting} className={START_BUTTON}>
                  <Plus aria-hidden="true" className="h-3.5 w-3.5" />
                  {busy ? "A search is running" : starting ? "Starting" : "Start the search"}
                </button>
              </div>
            </form>
          ) : null}

          {dropped > 0 ? (
            <Notice
              tone="warn"
              text={
                dropped === 1
                  ? "One business in this search could not be read, so it is not in the list below. Every other lead is here."
                  : `${dropped} businesses in this search could not be read, so they are not in the list below. Every other lead is here.`
              }
            />
          ) : null}

          {searches !== null && searches.length > 0 && searchId ? (
            <LeadList
              leads={leads}
              loading={loading}
              error={leadsError}
              bucket={bucket}
              phoneOnly={phoneOnly}
              sort={sort}
              onBucket={setBucket}
              onPhoneOnly={setPhoneOnly}
              onSort={setSort}
              onCall={callLead}
              onOpen={openDetail}
              callingKey={callingKey}
              noticeKey={failKey}
              notice={rowNotice}
            />
          ) : null}
        </div>
      </main>

      {detailKey ? (
        <LeadDetail
          lead={detailLead}
          loading={detailLoading}
          error={detailError}
          calling={callingKey === detailKey}
          onClose={closeDetail}
          onCall={callFromDrawer}
        />
      ) : null}
    </div>
  );
}
