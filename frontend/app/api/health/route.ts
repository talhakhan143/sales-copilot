import { BACKEND_HTTP_URL } from "@/lib/config";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Server side proxy for GET /api/health.
 *
 * The health pill polls this every 15 seconds, so an unreachable backend must not
 * be an exception. Every failure path answers HTTP 200 with { status: "down" } plus
 * a short detail, which lets the UI render a calm pill instead of throwing.
 *
 * Nothing is logged and no request header is forwarded.
 */

const UPSTREAM_TIMEOUT_MS = 5_000;

interface HealthPayload {
  status: string;
  groqConfigured?: boolean;
  sessions?: number;
  version?: string;
  detail?: string;
}

function describe(err: unknown): string {
  if (err instanceof Error) {
    if (err.name === "TimeoutError" || err.name === "AbortError") {
      return "The server did not answer in 5 seconds.";
    }
    const cause = err.cause;
    if (cause instanceof Error && cause.message) return cause.message;
    return err.message || "Unknown network error.";
  }
  return "Unknown network error.";
}

function down(detail: string): Response {
  const payload: HealthPayload = { status: "down", detail };
  return Response.json(payload, { status: 200 });
}

export async function GET(): Promise<Response> {
  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND_HTTP_URL}/api/health`, {
      method: "GET",
      headers: { accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
  } catch (err) {
    return down(describe(err));
  }

  if (!upstream.ok) {
    return down(`The backend answered ${upstream.status}.`);
  }

  let payload: unknown;
  try {
    payload = (await upstream.json()) as unknown;
  } catch {
    return down("The server sent back something the app could not read.");
  }

  if (typeof payload !== "object" || payload === null) {
    return down("The server health reply did not look right.");
  }

  return Response.json(payload, { status: 200 });
}
