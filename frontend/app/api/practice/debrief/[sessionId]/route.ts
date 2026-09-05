import { BACKEND_HTTP_URL } from "@/lib/config";
import { isDebrief } from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Server side proxy for GET /api/practice/debrief/{sessionId}.
 *
 * It forwards to GET {BACKEND}/api/practice/{sessionId}/debrief, which is where
 * FastAPI puts the score card. The path is shaped differently on this side
 * because a Next.js route file cannot end in a fixed segment after a dynamic
 * one and still be a route handler, so the session id sits last here.
 *
 * The timeout is 30 seconds. The backend runs one coach model call inside this
 * request and gives that call its own 25 second budget, so this outer wait has
 * to stay above the inner one. When it did not, a slow coach made this proxy
 * give up first and the rep saw an error at 20 seconds while the server was
 * about to send a full score card. It still does not need the 40 seconds the
 * start route gets, because nothing here fetches a client web page.
 *
 * Upstream status codes are mirrored, because the overlay tells the rep
 * different things for each one: 404 means the server forgot the session, 409
 * means it was a real call and not a practice call, 200 means here is the score.
 * A 200 body is checked against the full score card shape before it is passed
 * on. The overlay reads every field without a fallback, so a half built payload
 * has to be caught here and reported as an error instead of painting a panel
 * full of blanks.
 *
 * Nothing from the incoming request headers is forwarded and nothing is logged.
 */

// The backend caps its coach call at 25 seconds, see COACH_BUDGET_SECONDS in
// backend/app/api/routes_practice.py. This outer wait must stay bigger than
// that inner budget plus the request overhead, so a slow coach ends as a score
// card and never as an error. Keep the two numbers in step if either moves.
const UPSTREAM_TIMEOUT_MS = 30_000;
const START_COMMAND = "cd backend && ./run.sh";

interface ProxyFailure {
  error: string;
  detail: string;
  hint: string;
}

/** Next 16 hands the dynamic segment over as a promise, so it must be awaited. */
interface DebriefContext {
  params: Promise<{ sessionId: string }>;
}

function isTimeout(err: unknown): boolean {
  if (!(err instanceof Error)) return false;
  return err.name === "TimeoutError" || err.name === "AbortError";
}

function describe(err: unknown): string {
  if (isTimeout(err)) {
    return "The server did not answer in 30 seconds. Wait a moment and ask for the score again.";
  }
  if (err instanceof Error) {
    const cause = err.cause;
    if (cause instanceof Error && cause.message) return cause.message;
    return err.message || "Unknown network error.";
  }
  return "Unknown network error.";
}

export async function GET(request: Request, context: DebriefContext): Promise<Response> {
  const { sessionId } = await context.params;
  const id = typeof sessionId === "string" ? sessionId.trim() : "";

  if (id.length === 0) {
    const payload: ProxyFailure = {
      error: "Missing session",
      detail: "This web address has no call id, so there is nothing to score.",
      hint: "Go back to setup and start a new practice call.",
    };
    return Response.json(payload, { status: 400 });
  }

  let upstream: Response;
  try {
    upstream = await fetch(
      `${BACKEND_HTTP_URL}/api/practice/${encodeURIComponent(id)}/debrief`,
      {
        method: "GET",
        headers: { accept: "application/json" },
        cache: "no-store",
        signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      },
    );
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

  // Only a success body is a score card. A 404 or a 409 body is the backend's
  // own error shape and is passed straight through, so the overlay can show
  // what the server said.
  if (upstream.status === 200 && !isDebrief(payload)) {
    const failure: ProxyFailure = {
      error: "Unexpected score card",
      detail: "The server sent a score card with parts missing, so it cannot be shown.",
      hint: `Check the FastAPI log in the terminal running ${START_COMMAND}`,
    };
    return Response.json(failure, { status: 502 });
  }

  return Response.json(payload, { status: upstream.status });
}
