import type { Metadata } from "next";
import Link from "next/link";
import { ArrowRight, MapPin } from "lucide-react";

import { HealthPill, ModelStrip, SetupForm } from "@/components/SetupForm";

export const metadata: Metadata = {
  title: "Sales Copilot, set up your call",
  description:
    "Put what you sell and the client web page into one set of notes, then open the live call teleprompter.",
};

const STEPS: { n: string; title: string; body: string }[] = [
  {
    n: "01",
    title: "Set up the call",
    body: "What you sell, plus the client's web page or just a few lines about them, are joined into one set of notes. The notes stay in memory for the call and are never saved to disk.",
  },
  {
    n: "02",
    title: "Send it the sound",
    body: "Share the call tab, or pick a virtual cable, so the app can hear the client. Your own mic is the second lane.",
  },
  {
    n: "03",
    title: "Read it out loud",
    body: "Each time the client stops talking, your next line shows up in about a second. Read it out loud, word for word.",
  },
];

export default function Home() {
  return (
    <div className="min-h-dvh bg-bg">
      <header className="sticky top-0 z-20 flex h-14 items-center justify-between gap-4 border-b border-line bg-bg/95 px-6">
        <div className="flex items-center gap-3">
          <span className="h-2 w-2 shrink-0 bg-accent animate-mark" aria-hidden="true" />
          <span className="font-mono text-micro uppercase text-muted">Sales copilot</span>
        </div>
        <HealthPill />
      </header>

      <main className="mx-auto w-full max-w-[980px] px-6 pb-24 pt-12">
        <h1 className="text-h1 text-text">Who are you calling?</h1>
        <p className="mt-2 max-w-[62ch] text-lede text-muted">
          Tell the copilot about this one client. What you sell is saved already, so you only
          do this bit. Then the teleprompter opens and writes your next line while they talk.
        </p>

        {/*
          The way into the calling list.

          It sits above the form because for a rep with a lead list it is the
          faster door: the audit already knows the business, so the notes are
          built for them and they type nothing at all. The form below stays
          exactly as it is, word for word, for anyone with no list to work from.

          Drawn as one hairline panel with the one luminance step on hover, and
          no filled colour, because the only saturated fill on this page is the
          BUILD CALL CONTEXT button under the form and it has to stay the only
          one.
        */}
        <Link
          href="/leads"
          className="group mt-8 flex items-center justify-between gap-4 border border-line-strong bg-surface p-4 transition-colors duration-[120ms] ease-out hover:bg-surface-2"
        >
          <span className="flex min-w-0 items-start gap-3">
            <MapPin className="mt-1 h-4 w-4 shrink-0 text-muted" aria-hidden="true" />
            <span className="flex min-w-0 flex-col gap-1.5">
              <span className="font-mono text-micro uppercase text-muted">Your lead list</span>
              <span className="text-lede text-text">Call from your Google Maps leads</span>
              <span className="max-w-[58ch] text-body text-muted">
                Pick one of your searches, press Call on a business, and the notes are
                ready before it rings. You do not type a thing.
              </span>
            </span>
          </span>
          <ArrowRight
            className="h-4 w-4 shrink-0 text-dim transition-colors duration-[120ms] group-hover:text-muted"
            aria-hidden="true"
          />
        </Link>

        {/*
          A sentence, so it is drawn as a sentence.

          It used to be a 10px uppercase mono label in --dim, which is 2.7:1 on
          the page ground and breaks invariant 8 of the design spec: --dim is
          never used for a word the user must read. Micro dim is for chip digits
          and units, not for a line that tells the rep what to do next. It is
          body copy in --muted now, which is 6.4:1 and matches every other
          sentence on this page.
        */}
        <p className="mt-4 max-w-[58ch] text-body text-muted">
          No lead list? Set up one call by hand below.
        </p>

        <SetupForm />

        <section className="mt-16" aria-labelledby="how-it-works">
          <h2 id="how-it-works" className="font-mono text-micro uppercase text-muted">
            How it works
          </h2>
          <ol className="seam-grid mt-4 grid-cols-1 sm:grid-cols-3">
            {STEPS.map((step) => (
              <li key={step.n} className="flex flex-col gap-2 bg-surface p-5">
                <span className="font-mono text-micro tabnum uppercase text-dim">{step.n}</span>
                <span className="text-lede text-text">{step.title}</span>
                <span className="text-body text-muted">{step.body}</span>
              </li>
            ))}
          </ol>
          <div className="mt-8 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4">
            <span className="font-mono text-micro uppercase text-muted">
              Sessions live in server memory and expire after six hours
            </span>
            <ModelStrip />
          </div>
        </section>
      </main>
    </div>
  );
}
