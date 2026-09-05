/**
 * localStorage persistence for the prepared call session.
 *
 * The setup page writes the session here after POST /api/prepare-context, and
 * the call page reads it back so a hard refresh on /call still knows the system
 * prompt, the language and the prospect title without a round trip.
 *
 * Every function here is safe to call during SSR and safe to call in a browser
 * that has storage disabled. Reading `window.localStorage` itself throws in
 * Safari private mode and under a blocked cookie policy, and a write throws on
 * quota, so every access sits inside a try/catch and every failure degrades to
 * "no saved session" rather than an exception in a render path.
 */

import type { PreparedSession } from "@/lib/types";

export const SESSION_STORAGE_KEY = "salescopilot:session";

/** The Storage object, or null when it does not exist or is not reachable. */
function storage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage ?? null;
  } catch {
    return null;
  }
}

function asString(v: unknown, fallback: string): string {
  return typeof v === "string" ? v : fallback;
}

function asNullableString(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

function asNumber(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

/**
 * Persist a prepared session. No op during SSR, when storage is unavailable, or
 * when the session has no usable id (an id free session could never be reloaded,
 * so writing it would only shadow a good one).
 */
export function saveSession(s: PreparedSession): void {
  const store = storage();
  if (!store) return;
  if (typeof s?.sessionId !== "string" || s.sessionId.trim().length === 0) return;

  try {
    store.setItem(SESSION_STORAGE_KEY, JSON.stringify(s));
  } catch {
    // Quota exceeded, or storage disabled between the probe and the write.
    // Losing the cache is acceptable, the session id also lives in the URL.
  }
}

/**
 * Read the persisted session back.
 *
 * Returns null during SSR, when nothing is stored, when the stored value is not
 * parseable JSON, or when it has no non empty string sessionId. In the last two
 * cases the corrupt key is removed so the app does not keep tripping over it.
 * Remaining fields are normalized to their declared types, so callers get a real
 * PreparedSession and never an object full of undefined.
 */
export function loadSession(): PreparedSession | null {
  const store = storage();
  if (!store) return null;

  let raw: string | null = null;
  try {
    raw = store.getItem(SESSION_STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    clearSession();
    return null;
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    clearSession();
    return null;
  }

  const record = parsed as Record<string, unknown>;
  const sessionId = typeof record.sessionId === "string" ? record.sessionId.trim() : "";
  if (sessionId.length === 0) {
    clearSession();
    return null;
  }

  return {
    sessionId,
    systemPrompt: asString(record.systemPrompt, ""),
    clientUrl: asNullableString(record.clientUrl),
    clientTitle: asNullableString(record.clientTitle),
    clientExcerpt: asNullableString(record.clientExcerpt),
    scrapeChars: asNumber(record.scrapeChars, 0),
    scrapeOk: record.scrapeOk === true,
    scrapeError: asNullableString(record.scrapeError),
    createdAt: asNumber(record.createdAt, Date.now()),
    language: asString(record.language, "en"),
  };
}

/** Drop the persisted session. No op during SSR or when storage is unavailable. */
export function clearSession(): void {
  const store = storage();
  if (!store) return;
  try {
    store.removeItem(SESSION_STORAGE_KEY);
  } catch {
    // Nothing to do. A session we cannot remove is also a session we could not
    // have written, so the app is already in the "no saved session" path.
  }
}
