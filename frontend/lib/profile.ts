/**
 * What you sell, remembered between calls.
 *
 * This is deliberately NOT the session. A session is one call to one client, and
 * reusing it for the next client is how a rep ends up reading Acme's website to
 * a man who runs a spare parts shop. So the two are stored apart:
 *
 *   profile  what you sell, your prices, your proof. Yours, and it barely
 *            changes, so it is filled in once and prefilled forever after.
 *   session  who you are calling right now. Different every call, so the boxes
 *            for it always start empty.
 *
 * Everything here is safe during a prerender and safe in a browser with storage
 * switched off, because a thrown error while reading a convenience would take
 * the whole setup page down with it.
 */

const PROFILE_KEY = "salescopilot:profile";

/** The part of the setup that survives a call. */
export interface SellerProfile {
  /** Everything the rep sells, pasted raw. */
  knowledgeBase: string;
  /** Two letter language code the copilot answers in. */
  language: string;
  /** Epoch milliseconds, so a later version could show when it was last edited. */
  savedAt: number;
}

function storage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    // Private windows and blocked site data both throw on access, not on use.
    return null;
  }
}

/**
 * Save what the rep sells.
 *
 * Called on a successful build rather than on every keystroke, so a half typed
 * knowledge base never becomes the remembered one.
 *
 * @param profile - The knowledge base and language to keep.
 */
export function saveProfile(profile: Omit<SellerProfile, "savedAt">): void {
  const store = storage();
  if (!store) return;
  const text = profile.knowledgeBase.trim();
  if (!text) return;
  try {
    store.setItem(
      PROFILE_KEY,
      JSON.stringify({ knowledgeBase: text, language: profile.language, savedAt: Date.now() }),
    );
  } catch {
    // Out of quota, or storage is off. Losing this is a small inconvenience.
  }
}

/**
 * Read back what the rep sells.
 *
 * @returns The saved profile, or null when there is none or it is unusable.
 */
export function loadProfile(): SellerProfile | null {
  const store = storage();
  if (!store) return null;
  let raw: string | null = null;
  try {
    raw = store.getItem(PROFILE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    const rec = parsed as Record<string, unknown>;
    const knowledgeBase = typeof rec.knowledgeBase === "string" ? rec.knowledgeBase : "";
    if (!knowledgeBase.trim()) return null;
    return {
      knowledgeBase,
      language: typeof rec.language === "string" && rec.language ? rec.language : "en",
      savedAt: typeof rec.savedAt === "number" ? rec.savedAt : 0,
    };
  } catch {
    // Something else wrote to this key, or it was truncated. Drop it.
    try {
      store.removeItem(PROFILE_KEY);
    } catch {
      // Nothing more to do.
    }
    return null;
  }
}

/** Forget what the rep sells, for the "start again" affordance. */
export function clearProfile(): void {
  const store = storage();
  if (!store) return;
  try {
    store.removeItem(PROFILE_KEY);
  } catch {
    // Nothing more to do.
  }
}

const STYLE_KEY = "salescopilot:promptStyle";

/**
 * Remember whether the rep wants a whole line or a few points.
 *
 * It sits with the profile rather than the session because it is a preference
 * about how this person likes to work, not a fact about the client they are
 * calling. A rep who speaks in their own words wants that on every call.
 *
 * @param style - The choice to keep.
 */
export function savePromptStyle(style: "full" | "points"): void {
  const store = storage();
  if (!store) return;
  try {
    store.setItem(STYLE_KEY, style === "points" ? "points" : "full");
  } catch {
    // Storage is off. The default is the safe one, so this is survivable.
  }
}

/**
 * Read back the reading style.
 *
 * @returns The saved choice, or "full", which is the one a nervous rep needs.
 */
export function loadPromptStyle(): "full" | "points" {
  const store = storage();
  if (!store) return "full";
  try {
    return store.getItem(STYLE_KEY) === "points" ? "points" : "full";
  } catch {
    return "full";
  }
}
