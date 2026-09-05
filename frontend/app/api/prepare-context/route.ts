import { BACKEND_HTTP_URL } from "@/lib/config";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Server side proxy for POST /api/prepare-context.
 *
 * The browser never talks to FastAPI directly, so there is no CORS surface and no
 * backend URL in the client bundle. The request body is forwarded verbatim, the
 * upstream status and JSON are mirrored back, and a network failure is reported as
 * a 502 with a message the setup form can render as a calm notice.
 *
 * Nothing from the incoming request headers is forwarded and nothing is logged, so
 * no cookie, token or API key can leak through this route in either direction.
 */

const UPSTREAM_TIMEOUT_MS = 40_000;
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
    return "The server did not answer in 40 seconds. A slow client web page can cause this, so try again or clear the web address.";
  }
  if (err instanceof Error) {
    const cause = err.cause;
    if (cause instanceof Error && cause.message) return cause.message;
    return err.message || "Unknown network error.";
  }
  return "Unknown network error.";
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
    upstream = await fetch(`${BACKEND_HTTP_URL}/api/prepare-context`, {
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
