"use client";

/**
 * Who you are, and what you charge.
 *
 * Two files the rep owns rather than two tables this product invented. The name
 * and email go into every message the copywriter writes, and the prices are what
 * every deal value on every other screen is worked out from. Both were only
 * editable in a text file before this screen existed, which meant in practice
 * that neither was ever edited and every draft went out signed with whatever
 * name happened to be in the box.
 *
 * The whole file is carried through on save, not just the fields shown. The
 * pricing file has a dozen more knobs than these, keyword lists and score
 * weights and review bands, and a settings screen that dropped them because it
 * did not draw them would quietly break the scoring for every future search.
 */

import { useCallback, useEffect, useState } from "react";
import { Check, RotateCcw, Save } from "lucide-react";

import {
  FIELD_INPUT,
  FIELD_LABEL,
  LeadChrome,
  PRIMARY_BUTTON,
  SMALL_BUTTON,
} from "@/components/LeadChrome";
import { Notice } from "@/components/Notice";
import { fetchConfig, saveConfig } from "@/lib/leads";
import type { LeadPricing, LeadPricingTier, LeadProfile } from "@/lib/types";

/** The profile fields worth a box, in the order a person would fill them in. */
const PROFILE_FIELDS: { key: keyof LeadProfile & string; label: string; hint: string }[] = [
  { key: "your_name", label: "Your name", hint: "This signs every message. Leave it blank and the model invents one." },
  { key: "company", label: "Company", hint: "Optional. Used when a message needs to sound like a firm rather than a person." },
  { key: "email", label: "Email", hint: "The address you want replies to come back to." },
  { key: "phone", label: "Phone", hint: "Optional. Goes into a message when you offer them a call back." },
  { key: "whatsapp", label: "WhatsApp", hint: "Optional. With the country code." },
  { key: "city", label: "Your city", hint: "Optional. Being local is worth saying when you are." },
  { key: "portfolio_url", label: "Work you can show", hint: "Optional. A link to send when they ask what you have built." },
  { key: "calendar_url", label: "Booking link", hint: "Optional. A link to send instead of going back and forth on times." },
];

/** Read a string field off a loose object without letting undefined through. */
function text(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "string" ? value : "";
}

/** Read one end of a price range. */
function priceAt(tier: LeadPricingTier, field: "website_price" | "retainer_monthly", i: number): string {
  const range = tier[field];
  if (!Array.isArray(range)) return "";
  const value = range[i];
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "";
}

export default function SettingsPage() {
  const [profile, setProfile] = useState<LeadProfile | null>(null);
  const [pricing, setPricing] = useState<LeadPricing | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [dirty, setDirty] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    const answer = await fetchConfig(signal);
    if (signal?.aborted) return;
    setLoading(false);
    if (answer.kind === "error") {
      setError(answer.hint ? `${answer.message} ${answer.hint}` : answer.message);
      return;
    }
    setProfile(answer.config.profile);
    setPricing(answer.config.pricing);
    setDirty(false);
  }, []);

  useEffect(() => {
    const stop = new AbortController();
    /* `load` writes state after an await, apart from the loading flag it sets
       first, which is the one the rule can see. The screen says "reading your
       settings" because of it. */
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load(stop.signal);
    return () => stop.abort();
  }, [load]);

  /* The saved tick clears itself. A tick that stays on screen stops meaning
     "that worked just now" and starts meaning nothing at all. */
  useEffect(() => {
    if (!saved) return;
    const timer = window.setTimeout(() => setSaved(false), 2600);
    return () => window.clearTimeout(timer);
  }, [saved]);

  const editProfile = useCallback((key: string, value: string) => {
    setProfile((old) => (old ? { ...old, [key]: value } : old));
    setDirty(true);
    setSaved(false);
  }, []);

  const editPrice = useCallback(
    (tierKey: string, field: "website_price" | "retainer_monthly", index: number, raw: string) => {
      setPricing((old) => {
        if (!old) return old;
        const tiers = { ...(old.tiers ?? {}) };
        const tier: LeadPricingTier = { ...(tiers[tierKey] ?? {}) };
        const range = Array.isArray(tier[field]) ? [...(tier[field] as number[])] : [0, 0];
        /* An empty box means zero, not NaN. A NaN in this file makes every deal
           value on every other screen come back as "not a number". */
        const parsed = Number.parseInt(raw, 10);
        range[index] = Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
        tier[field] = range;
        tiers[tierKey] = tier;
        return { ...old, tiers };
      });
      setDirty(true);
      setSaved(false);
    },
    [],
  );

  const save = useCallback(async () => {
    if (!profile && !pricing) return;
    setSaving(true);
    setError(null);
    const answer = await saveConfig({
      profile: profile ?? undefined,
      pricing: pricing ?? undefined,
    });
    setSaving(false);
    if (answer.kind === "error") {
      setError(answer.hint ? `${answer.message} ${answer.hint}` : answer.message);
      return;
    }
    /* Redrawn from what came back off disk, not from what was in the boxes. If
       the server cleaned something up, the rep should be looking at the cleaned
       version rather than at their own typing. */
    setProfile(answer.config.profile);
    setPricing(answer.config.pricing);
    setDirty(false);
    setSaved(true);
  }, [pricing, profile]);

  const tiers = Object.entries(pricing?.tiers ?? {});
  const symbol = typeof pricing?.currency_symbol === "string" ? pricing.currency_symbol : "$";

  return (
    <LeadChrome
      title="You, and your prices."
      lede="Your name signs every message the copilot writes. Your prices are what every deal value on the other screens is worked out from. Both are yours, not this app's."
      action={
        <>
          {dirty ? (
            <button type="button" onClick={() => void load()} className={SMALL_BUTTON}>
              <RotateCcw aria-hidden="true" className="h-3 w-3" />
              Undo
            </button>
          ) : null}
          {saved ? (
            <span className="flex h-[26px] items-center gap-1.5 px-1 font-mono text-micro uppercase text-ok">
              <Check aria-hidden="true" className="h-3 w-3" />
              Saved
            </span>
          ) : null}
        </>
      }
    >
      {error ? <Notice tone="danger" text={error} /> : null}

      {loading ? (
        <p className="font-mono text-micro uppercase text-muted">Reading your settings</p>
      ) : null}

      {!loading && profile ? (
        <div className="flex flex-col gap-6">
          <section className="flex flex-col gap-5 bg-surface p-5">
            <div className="flex flex-col gap-1">
              <span className={FIELD_LABEL}>About you</span>
              <span className="text-body text-dim">
                Only the first two really matter. The rest get used when a message has a reason
                to use them.
              </span>
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              {PROFILE_FIELDS.map((field) => (
                <label key={field.key} className="flex flex-col gap-1.5">
                  <span className={FIELD_LABEL}>{field.label}</span>
                  <input
                    value={text(profile, field.key)}
                    onChange={(event) => editProfile(field.key, event.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                    className={FIELD_INPUT}
                  />
                  <span className="text-body text-dim">{field.hint}</span>
                </label>
              ))}
            </div>
          </section>

          {tiers.length > 0 ? (
            <section className="flex flex-col gap-5 bg-surface p-5">
              <div className="flex flex-col gap-1">
                <span className={FIELD_LABEL}>What you charge</span>
                <span className="text-body text-dim">
                  A business lands in one of these bands by what Google calls it. The deal value
                  you see on a lead is the middle of that band, so changing a number here changes
                  every price on every screen from the next search on.
                </span>
              </div>

              <div className="flex flex-col gap-4">
                {tiers.map(([key, tier]) => (
                  <div key={key} className="flex flex-col gap-3 border-l-2 border-line-strong pl-4">
                    <div className="flex items-baseline gap-2">
                      <span className="font-sans text-idle text-text">
                        {typeof tier.label === "string" && tier.label ? tier.label : `Band ${key}`}
                      </span>
                      <span className="font-mono text-micro uppercase text-dim">band {key}</span>
                    </div>

                    <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                      {(
                        [
                          ["website_price", 0, "Website, from"],
                          ["website_price", 1, "Website, to"],
                          ["retainer_monthly", 0, "Monthly, from"],
                          ["retainer_monthly", 1, "Monthly, to"],
                        ] as const
                      ).map(([field, index, label]) => (
                        <label key={`${field}${index}`} className="flex flex-col gap-1.5">
                          <span className={FIELD_LABEL}>{label}</span>
                          <div className="flex items-center gap-1.5">
                            <span className="font-mono text-micro text-dim">{symbol}</span>
                            <input
                              inputMode="numeric"
                              value={priceAt(tier, field, index)}
                              onChange={(event) => editPrice(key, field, index, event.target.value)}
                              className={`${FIELD_INPUT} tabnum`}
                            />
                          </div>
                        </label>
                      ))}
                    </div>

                    {Array.isArray(tier.keywords) && tier.keywords.length > 0 ? (
                      <span className="text-body text-dim">
                        Matches {tier.keywords.length} kinds of business, for example{" "}
                        {tier.keywords.slice(0, 4).join(", ")}.
                      </span>
                    ) : null}
                  </div>
                ))}
              </div>
            </section>
          ) : null}

          <div className="flex flex-wrap items-center gap-3">
            <button type="button" onClick={() => void save()} disabled={saving || !dirty} className={PRIMARY_BUTTON}>
              <Save aria-hidden="true" className="h-3.5 w-3.5" />
              {saving ? "Saving" : "Save"}
            </button>
            <span className="font-mono text-micro uppercase text-dim">
              {dirty ? "You have unsaved changes" : "Nothing to save"}
            </span>
          </div>
        </div>
      ) : null}
    </LeadChrome>
  );
}
