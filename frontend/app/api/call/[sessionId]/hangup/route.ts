import { BACKEND_HTTP_URL } from "@/lib/config";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Server side proxy for POST /api/call/{sessionId}/hangup.
 *
 * Same job and same rules as the other call proxies: the browser never talks to
 * FastAPI directly, the upstream status and JSON are mirrored back, and a
 * network failure is reported as a 502 with a message the call launcher can
 * render as a calm notice.
 *
 * The timeout is 15 seconds. Hanging up is one small REST call to the carrier,
 * and the rep is standing on the button waiting for it, so a long wait here
 * would be worse than an honest failure.
 *
 * No body is forwarded. The session id in the path is the whole request, so
 * whatever the browser sends is ignored on purpose, which keeps this route from
 * being a way to push anything at all into the backend.
 *
 * Nothing from the incoming request headers is forwarded and nothing is logged.
 */

const UPSTREAM_TIMEOUT_MS = 15_000;
const START_COMMAND = "cd backend && ./run.sh";

interface ProxyFailure {
  error: string;
  detail: string;
  hint: string;
}

/** Next 16 hands the dynamic segment over as a promise, so it must be awaited. */
interface HangupContext {
  params: Promise<{ sessionId: string }>;
}

function isTimeout(err: unknown): boolean {
  if (!(err instanceof Error)) return false;
  return err.name === "TimeoutError" || err.name === "AbortError";
}

function describe(err: unknown): string {
  if (isTimeout(err)) {
    return "The server did not answer in 15 seconds. The call may still be running, so end it on your phone.";
  }
  if (err instanceof Error) {
    const cause = err.cause;
    if (cause instanceof Error && cause.message) return cause.message;
    return err.message || "Unknown network error.";
  }
  return "Unknown network error.";
}

/**
 * The line under the message, which has to agree with the message.
 *
 * A timeout means the backend took the connection and then went quiet, so it IS
 * running, and telling the rep to start it would send them to fix the one thing
 * that is not broken. Only a real connection failure earns the start command.
 */
function hintFor(err: unknown): string {
  if (isTimeout(err)) {
    return `Check the FastAPI log in the terminal running ${START_COMMAND}`;
  }
  return `Start the FastAPI server first: ${START_COMMAND}`;
}

export async function POST(request: Request, context: HangupContext): Promise<Response> {
  const { sessionId } = await context.params;
  const id = typeof sessionId === "string" ? sessionId.trim() : "";

  if (id.length === 0) {
    const payload: ProxyFailure = {
      error: "Missing session",
      detail: "This web address has no call id, so there is nothing to end.",
      hint: "Go back to setup and start a new call.",
    };
    return Response.json(payload, { status: 400 });
  }

  let upstream: Response;
  try {
    upstream = await fetch(
      `${BACKEND_HTTP_URL}/api/call/${encodeURIComponent(id)}/hangup`,
      {
        method: "POST",
        headers: { accept: "application/json" },
        cache: "no-store",
        signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      },
    );
  } catch (err) {
    const payload: ProxyFailure = {
      error: "Backend unreachable",
      detail: describe(err),
      hint: hintFor(err),
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
