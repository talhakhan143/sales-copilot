"use client";

/**
 * The frame every lead screen sits in, and the words they all share.
 *
 * There are five of these screens now, and they were one page until the rest of
 * the rep's old dashboard moved in. A rep working a list moves between them all
 * day: pick who to call, see what is left, check the money, change a price. So
 * the way out of each one has to be in the same place on all of them, which is
 * what this file is.
 *
 * The style constants live here rather than being copied into five files. They
 * were already duplicated across two before this, and a button that is 22px on
 * one screen and 26px on the next is the kind of thing nobody reports and
 * everybody notices.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { BarChart3, Phone, Settings, Target } from "lucide-react";

/** A small bordered chip. The product's one shape for "this is an action". */
export const MICRO_BUTTON =
  "relative flex h-[22px] shrink-0 items-center gap-1.5 rounded-hair border border-line-strong px-2 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] ease-out before:absolute before:-inset-y-[11px] before:-inset-x-2 before:content-[''] hover:bg-surface-2 hover:text-text";

/** The same chip one size up, for anywhere it is the main thing on the row. */
export const SMALL_BUTTON =
  "relative flex h-[26px] shrink-0 items-center gap-1.5 rounded-hair border border-line-strong px-2.5 font-mono text-micro uppercase text-muted transition-colors duration-[120ms] ease-out before:absolute before:-inset-[9px] before:content-[''] hover:bg-surface-2 hover:text-text";

/** The one filled button. There is at most one of these on a screen. */
export const PRIMARY_BUTTON =
  "relative flex h-[34px] shrink-0 items-center gap-2 rounded-hair bg-accent px-4 font-mono text-micro font-semibold uppercase text-bg transition-opacity duration-[120ms] ease-out hover:opacity-90 disabled:opacity-40";

export const FIELD_LABEL = "font-mono text-micro uppercase text-muted";
export const FIELD_INPUT =
  "h-11 w-full rounded-hair border border-line-strong bg-surface-2 px-3 font-sans text-body text-text placeholder:text-dim";

/** The five screens, in the order a day actually goes. */
const SCREENS: { href: string; label: string; hint: string; Icon: typeof Phone }[] = [
  { href: "/leads", label: "Leads", hint: "who to call", Icon: Phone },
  { href: "/leads/queue", label: "Today", hint: "in what order", Icon: Target },
  { href: "/leads/analytics", label: "Money", hint: "what it adds up to", Icon: BarChart3 },
  { href: "/leads/settings", label: "Settings", hint: "you and your prices", Icon: Settings },
];

export interface LeadChromeProps {
  /** The screen's own heading. */
  title: string;
  /** One or two plain lines under it saying what the screen is for. */
  lede?: string;
  /** Anything the screen wants in the header, beside the nav. */
  action?: ReactNode;
  children: ReactNode;
}

/**
 * The row of screens.
 *
 * Its own component because the calling list draws its own header, it has an
 * export button and a way back to the one off call form that nothing else has,
 * and a nav that lived only inside `LeadChrome` would have to be copied there.
 */
export function LeadNav() {
  const pathname = usePathname();
  return (
    <nav aria-label="Lead screens" className="flex min-w-0 items-center gap-1.5">
      {SCREENS.map((screen) => {
        /* "/leads" is the only one that has to match exactly. Everything else
           lives under it, so a prefix test would light Leads up on every
           screen. */
        const on =
          screen.href === "/leads" ? pathname === "/leads" : pathname.startsWith(screen.href);
        return (
          <Link
            key={screen.href}
            href={screen.href}
            aria-current={on ? "page" : undefined}
            title={screen.hint}
            className={`relative flex h-[26px] shrink-0 items-center gap-1.5 rounded-hair px-2.5 font-mono text-micro uppercase transition-colors duration-[120ms] ease-out ${
              on ? "bg-surface-2 text-text" : "text-muted hover:bg-surface-2 hover:text-text"
            }`}
          >
            {on ? (
              <span
                aria-hidden="true"
                className="pointer-events-none absolute inset-x-0 bottom-0 h-0.5 bg-accent"
              />
            ) : null}
            <screen.Icon aria-hidden="true" className="h-3 w-3" />
            {screen.label}
          </Link>
        );
      })}
    </nav>
  );
}

/**
 * The header, the nav and the page width, in one place.
 *
 * The nav is a row of chips and not a sidebar. A sidebar costs 200px of width
 * on every screen, and the widest thing in this product is a table of leads
 * that a rep reads across, so the width is worth more than the column.
 */
export function LeadChrome({ title, lede, action, children }: LeadChromeProps) {
  return (
    <div className="min-h-dvh bg-bg">
      <header className="sticky top-0 z-20 flex h-14 items-center justify-between gap-4 border-b border-line bg-bg/95 px-6">
        <div className="flex min-w-0 items-center gap-4">
          <Link href="/leads" className="flex shrink-0 items-center gap-3">
            <span className="h-2 w-2 shrink-0 bg-accent animate-mark" aria-hidden="true" />
            <span className="font-mono text-micro uppercase text-muted">Sales copilot</span>
          </Link>

          <span aria-hidden="true" className="h-3.5 w-px shrink-0 bg-line-strong" />

          <LeadNav />
        </div>

        <div className="flex shrink-0 items-center gap-2">{action}</div>
      </header>

      <main className="mx-auto w-full max-w-[1280px] px-6 pb-24 pt-10">
        <h1 className="text-h1 text-text">{title}</h1>
        {lede ? <p className="mt-2 max-w-[62ch] text-lede text-muted">{lede}</p> : null}
        <div className="mt-8">{children}</div>
      </main>
    </div>
  );
}

/**
 * One big number with a word over it and a line under it.
 *
 * The line under is not decoration. Every number on the money screen is a
 * guess built on another guess, and a rep who does not know which is which will
 * either trust all of them or none.
 */
export function Stat({
  label,
  value,
  sub,
  tone = "text",
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "text" | "accent" | "ok" | "warn";
}) {
  const colour =
    tone === "accent"
      ? "text-accent"
      : tone === "ok"
        ? "text-ok"
        : tone === "warn"
          ? "text-warn"
          : "text-text";
  return (
    <div className="flex flex-col gap-1.5 bg-surface p-4">
      <span className={FIELD_LABEL}>{label}</span>
      <span className={`tabnum font-sans text-value ${colour}`}>{value}</span>
      {sub ? <span className="text-body text-dim">{sub}</span> : null}
    </div>
  );
}

/** Money, short. 358400 reads as $358.4k, which is the number a rep repeats. */
export function shortMoney(amount: number, symbol = "$"): string {
  if (!Number.isFinite(amount) || amount === 0) return `${symbol}0`;
  const n = Math.round(amount);
  if (Math.abs(n) >= 1_000_000) return `${symbol}${(n / 1_000_000).toFixed(1)}m`;
  if (Math.abs(n) >= 1_000) return `${symbol}${(n / 1_000).toFixed(1)}k`;
  return `${symbol}${n.toLocaleString("en-US")}`;
}

/** Money, in full, for the places a rounded number would be wrong. */
export function fullMoney(amount: number, symbol = "$"): string {
  if (!Number.isFinite(amount)) return `${symbol}0`;
  return `${symbol}${Math.round(amount).toLocaleString("en-US")}`;
}
