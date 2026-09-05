import type { Metadata } from "next";

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
        <h1 className="text-h1 text-text">Set up your call, then open the teleprompter.</h1>
        <p className="mt-2 max-w-[62ch] text-lede text-muted">
          Write what you sell, point it at the client, and the copilot writes your next line
          while they are still talking.
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
