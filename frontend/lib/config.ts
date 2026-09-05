/**
 * Runtime configuration for the frontend.
 *
 * Two separate URLs on purpose:
 *   - BACKEND_HTTP_URL is used only by the Next.js route handlers in app/api/*,
 *     which run on the server. The browser talks to those proxies, never to
 *     FastAPI directly, so there is no CORS surface.
 *   - wsBaseUrl() is used only by the browser, because a WebSocket cannot be
 *     proxied through a route handler.
 */

/** Remove one or more trailing slashes so callers can always append a path. */
function stripTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

/**
 * Base URL of the FastAPI backend for server side fetches.
 *
 * SERVER SIDE ONLY. There is no NEXT_PUBLIC_ prefix, so Next does not inline it
 * into the client bundle. In the browser this expression resolves to the
 * fallback below, which is harmless (it is a loopback address, not a secret),
 * but you must never rely on this constant inside a "use client" file. Change it
 * when FastAPI runs on another host or port, for example inside Docker where it
 * would be http://backend:8000.
 */
export const BACKEND_HTTP_URL: string = stripTrailingSlash(
  process.env.BACKEND_HTTP_URL || "http://127.0.0.1:8000",
);

/**
 * Base URL for the teleprompter WebSocket, safe to call in the browser.
 *
 * Resolution order:
 *   1. NEXT_PUBLIC_BACKEND_WS_URL when it is set (inlined at build time).
 *   2. In the browser, derive from window.location: same hostname, port 8000,
 *      wss when the page is https, ws otherwise. This makes a LAN or ngrok
 *      style setup work with zero configuration.
 *   3. On the server (SSR pass, where window does not exist), a loopback default.
 *
 * Always returned without a trailing slash.
 */
export function wsBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_BACKEND_WS_URL;
  if (configured && configured.trim().length > 0) {
    return stripTrailingSlash(configured.trim());
  }

  if (typeof window !== "undefined" && window.location) {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.hostname || "127.0.0.1";
    return `${scheme}//${host}:8000`;
  }

  return "ws://127.0.0.1:8000";
}

/** Full WebSocket URL for one prepared session. */
export function teleprompterWsUrl(sessionId: string): string {
  return `${wsBaseUrl()}/ws/teleprompter?session_id=${encodeURIComponent(sessionId)}`;
}

/** Capture rate the backend expects. Int16 LE mono at this rate, nothing else. */
export const SAMPLE_RATE = 16000;

/** Samples per frame posted from the AudioWorklet. 512 samples is 32 ms at 16 kHz. */
export const FRAME_SAMPLES = 512;

/** Languages the backend accepts for STT and for the copilot reply. */
export const LANGUAGES: { code: string; label: string }[] = [
  { code: "en", label: "English" },
  { code: "ur", label: "Urdu" },
  { code: "hi", label: "Hindi" },
  { code: "es", label: "Spanish" },
  { code: "ar", label: "Arabic" },
  { code: "fr", label: "French" },
  { code: "de", label: "German" },
];

/** How often the setup page re polls /api/health, in milliseconds. */
export const HEALTH_POLL_MS = 15000;
