import { BACKEND_HTTP_URL } from "@/lib/config";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Server side proxy for POST /api/call/start.
 *
 * Same job and same rules as the prepare context proxy: the browser never talks
 * to FastAPI directly, so there is no CORS surface and no backend URL in the
 * client bundle. The request body is forwarded verbatim, the upstream status and
 * JSON are mirrored back, and a network failure is reported as a 502 with a
 * message the call launcher can render as a calm notice.
 *
 * Mirroring the status matters more here than anywhere else in the app. The
 * backend answers a refusal with the very same body shape and `ok: false`, and
 * the message inside it is written for the rep, for example "Add
 * TWILIO_ACCOUNT_SID to backend/.env" or "Write the number with the country
 * code, like +923001234567." That body has to reach the browser untouched, so a
 * 400 is passed on as a 400 with its own JSON and is never rewritten here.
 *
 * The timeout is 30 seconds because the Twilio path makes a real REST call to a
 * carrier from inside this request. It does not need the 40 seconds the setup
 * routes get, because nothing here fetches a client web page.
 *
 * This route can spend money. A Twilio call is billed per minute from the moment
 * it connects. So the body is forwarded exactly once and never retried: a retry
 * on a slow answer could place a second call the rep never asked for.
 *
 * Nothing from the incoming request headers is forwarded and nothing is logged,
 * so no cookie, token or API key can leak through this route in either
 * direction.
 */

const UPSTREAM_TIMEOUT_MS = 30_000;
const START_COMMAND = "cd backend && ./run.sh";

interface ProxyFailure {
  error: string;
  detail: string;
  hint: string;
}

function isTimeout(err: unknown): boolean {
  if (!(err instanceof Error)) return false;
  return err.name === "TimeoutError" || err.name === "AbortError";
}

function describe(err: unknown): string {
  if (isTimeout(err)) {
    return "The server did not answer in 30 seconds. Check your phone, the call may still be ringing.";
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

export async function POST(request: Request): Promise<Response> {
  let body: string;
  try {
    body = await request.text();
  } catch {
    return Response.json(
      { error: "Unreadable request body", detail: "The app could not read what you sent.", hint: "" },
      { status: 400 },
    );
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND_HTTP_URL}/api/call/start`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
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
