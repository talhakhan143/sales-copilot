import { BACKEND_HTTP_URL } from "@/lib/config";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Server side proxy for GET /api/call/providers.
 *
 * Same job and same rules as the prepare context proxy: the browser never talks
 * to FastAPI directly, so there is no CORS surface and no backend URL in the
 * client bundle. The upstream status and JSON are mirrored back, and a network
 * failure is reported as a 502 with a message the picker can render as a calm
 * notice next to its offline provider list.
 *
 * The timeout is short because this endpoint does no network work of its own. It
 * reads the environment and answers, so it is fast or it is broken.
 *
 * This route must never be cached. The whole point of the list is that it says
 * whether Twilio and WhatsApp are configured right now, so an operator can add a
 * key to backend/.env, restart, and see the option turn on. `force-dynamic` plus
 * `cache: "no-store"` is what makes that true.
 *
 * Nothing from the incoming request headers is forwarded and nothing is logged,
 * so no cookie, token or API key can leak through this route in either
 * direction.
 */

const UPSTREAM_TIMEOUT_MS = 8_000;
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
    return "The server did not answer in 8 seconds.";
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

export async function GET(): Promise<Response> {
  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND_HTTP_URL}/api/call/providers`, {
      method: "GET",
      headers: { accept: "application/json" },
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
