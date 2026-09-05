import { BACKEND_HTTP_URL } from "@/lib/config";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Server side proxy for GET /api/call/{sessionId}/status.
 *
 * Same job and same rules as the other call proxies: the browser never talks to
 * FastAPI directly, the upstream status and JSON are mirrored back, and a
 * network failure is reported as a 502 with a message the call page can render
 * as a calm notice.
 *
 * WHY THIS ROUTE EXISTS. The teleprompter socket pushes a `call_state` frame
 * only when the call moves, so a browser that opened in the middle of a call
 * that is already running is told nothing at all until the call ends. Without a
 * way to ask, a rep who reloaded the page during a live Twilio call would see an
 * empty bar and a Call button, and pressing it would place a second call that
 * costs real money. This is that way to ask. It is read once when the call page
 * opens, never on a timer, because the socket covers every change after that.
 *
 * The timeout is short. The backend answers this one from memory, so it is fast
 * or it is broken.
 *
 * This route must never be cached. It reports where a call is right now, and a
 * cached answer would be worse than no answer. `force-dynamic` plus
 * `cache: "no-store"` is what makes that true.
 *
 * Nothing from the incoming request headers is forwarded and nothing is logged.
 */

const UPSTREAM_TIMEOUT_MS = 8_000;
const START_COMMAND = "cd backend && ./run.sh";

interface ProxyFailure {
  error: string;
  detail: string;
  hint: string;
}

/** Next 16 hands the dynamic segment over as a promise, so it must be awaited. */
interface StatusContext {
  params: Promise<{ sessionId: string }>;
}

function isTimeout(err: unknown): boolean {
  if (!(err instanceof Error)) return false;
  return err.name === "TimeoutError" || err.name === "AbortError";
}

function describe(err: unknown): string {
  if (isTimeout(err)) {
    return "The server did not answer in 8 seconds.";
  }
  if (err instanceof Error) {
    const cause = err.cause;
    if (cause instanceof Error && cause.message) return cause.message;
    return err.message || "Unknown network error.";
  }
  return "Unknown network error.";
}

export async function GET(request: Request, context: StatusContext): Promise<Response> {
  const { sessionId } = await context.params;
  const id = typeof sessionId === "string" ? sessionId.trim() : "";

  if (id.length === 0) {
    const payload: ProxyFailure = {
      error: "Missing session",
      detail: "This web address has no call id, so there is nothing to look up.",
      hint: "Go back to setup and start a new call.",
    };
    return Response.json(payload, { status: 400 });
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND_HTTP_URL}/api/call/${encodeURIComponent(id)}/status`, {
      method: "GET",
      headers: { accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
  } catch (err) {
    const payload: ProxyFailure = {
      error: "Backend unreachable",
      detail: describe(err),
      hint: `Start the FastAPI server first: ${START_COMMAND}`,
    };
    return Response.json(payload, { status: 502 });
  }

  const text = await upstream.text();
  if (!text) {
    return Response.json(
      {
        error: "Empty response",
        detail: `The backend answered ${upstream.status} with no body.`,
        hint: `Check the FastAPI log in the terminal running ${START_COMMAND}`,
      },
      { status: upstream.status === 200 ? 502 : upstream.status },
    );
  }

  let payload: unknown;
  try {
    payload = JSON.parse(text) as unknown;
  } catch {
    return Response.json(
      {
        error: "Unexpected response",
        detail: `The backend answered ${upstream.status} with something that is not JSON.`,
        hint: `Check the FastAPI log in the terminal running ${START_COMMAND}`,
      },
      { status: 502 },
    );
  }

  return Response.json(payload, { status: upstream.status });
}
