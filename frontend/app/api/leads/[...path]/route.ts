import { BACKEND_HTTP_URL } from "@/lib/config";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Server side proxy for every leads endpoint.
 *
 * One catch all instead of seven route files, because all seven do the identical
 * thing: forward the path and the query string to FastAPI, mirror the status and
 * the JSON back, and turn a dead server into one plain sentence. The set of
 * endpoints behind this is:
 *
 *   GET  /api/leads/searches
 *   GET  /api/leads/{search_id}?status=&has_phone=&sort=
 *   GET  /api/leads/{search_id}/{key}
 *   POST /api/leads/{search_id}/{key}/call
 *   POST /api/leads/{search_id}/{key}/status
 *   POST /api/leads/search
 *   GET  /api/leads/jobs/{job_id}
 *   GET  /api/leads/config
 *   PUT  /api/leads/config
 *   GET  /api/leads/{search_id}/export.csv          (CSV, not JSON)
 *   GET  /api/leads/{search_id}/{key}/shot/{view}   (JPEG, not JSON)
 *
 * Same rules as app/api/prepare-context/route.ts: the browser never talks to
 * FastAPI directly, so there is no CORS surface and no backend URL in the client
 * bundle. Nothing from the incoming request headers is forwarded and nothing is
 * logged, so no cookie, token or API key can leak through here in either
 * direction.
 *
 * THE KEY IS THE AWKWARD PART. A lead key looks like
 * "0x89c259a0df6f48af:0x229bd2d48c26502f", so it holds a colon. lib/leads.ts
 * escapes it before it goes in the path, Next hands it back decoded in `params`,
 * and it is escaped again on the way out, so the colon survives the whole round
 * trip and still sits inside one path segment.
 *
 * In Next 16 the params of a dynamic route arrive as a promise and must be
 * awaited. Getting that wrong fails the production build, not the dev server, so
 * it is written out as its own type below.
 */

/**
 * How long the backend gets to answer a read.
 *
 * These endpoints answer from files on the same machine, so they are fast or
 * they are broken. lib/leads.ts waits 14 seconds, two more than this, so this
 * route's own sentence is the one that reaches the rep.
 */
const READ_TIMEOUT_MS = 12_000;

/**
 * How long the backend gets to build a call context.
 *
 * This path fetches the lead's own website through Jina inside the request, the
 * same way prepare-context does, so it gets the same generous window. The
 * browser waits 45 seconds, five more than this.
 */
const CALL_TIMEOUT_MS = 40_000;

/** The deepest path any leads endpoint has, plus room to spare. */
const MAX_SEGMENTS = 6;

const START_COMMAND = "cd backend && ./run.sh";

interface ProxyFailure {
  error: string;
  detail: string;
  hint: string;
}

/** Next 16 hands the catch all segment over as a promise, so it must be awaited. */
interface LeadsContext {
  params: Promise<{ path: string[] }>;
}

function isTimeout(err: unknown): boolean {
  if (!(err instanceof Error)) return false;
  return err.name === "TimeoutError" || err.name === "AbortError";
}

/**
 * One plain sentence for a forward that never landed.
 *
 * Node's own words are dropped on purpose. A dead FastAPI server makes this
 * throw with "fetch failed" and a cause of "connect ECONNREFUSED 127.0.0.1:8000",
 * and this sentence is printed straight to the rep, who is holding a phone and
 * has no use for a port number. The hint under it already says the one command
 * that fixes it.
 */
function describe(err: unknown, timeoutMs: number): string {
  if (isTimeout(err)) {
    return `The server did not answer in ${Math.round(timeoutMs / 1000)} seconds.`;
  }
  return "The app could not reach the server.";
}

/**
 * Build the upstream path out of the segments Next decoded.
 *
 * Every segment is escaped again, so a colon inside a lead key stays inside its
 * own segment. A segment that is empty, or that is a dot or two dots, is refused
 * outright: those are the only ones that could walk the path somewhere other
 * than the leads API, and no real endpoint uses them.
 */
function buildPath(segments: string[]): string | null {
  if (segments.length === 0 || segments.length > MAX_SEGMENTS) return null;

  const parts: string[] = [];
  for (const segment of segments) {
    if (typeof segment !== "string") return null;
    const trimmed = segment.trim();
    if (trimmed.length === 0 || trimmed === "." || trimmed === "..") return null;
    parts.push(encodeURIComponent(trimmed));
  }
  return parts.join("/");
}

/** The call path is the only slow one, so it is the only one with a long wait. */
function timeoutFor(segments: string[]): number {
  return segments[segments.length - 1] === "call" ? CALL_TIMEOUT_MS : READ_TIMEOUT_MS;
}

/**
 * Do the forward, and turn everything that can go wrong into a JSON body.
 *
 * The upstream status is mirrored, so a 404 for a lead that is no longer in the
 * data files stays a 404 and the browser can say so in its own words.
 */
async function forward(
  request: Request,
  context: LeadsContext,
  method: "GET" | "POST" | "PUT",
): Promise<Response> {
  const { path } = await context.params;
  const segments = Array.isArray(path) ? path : [];
  const upstreamPath = buildPath(segments);

  if (upstreamPath === null) {
    const payload: ProxyFailure = {
      error: "Unknown address",
      detail: "This web address is not part of the leads screen.",
      hint: "Go back to the leads list and try again.",
    };
    return Response.json(payload, { status: 400 });
  }

  let body: string | undefined;
  if (method !== "GET") {
    try {
      body = await request.text();
    } catch {
      const payload: ProxyFailure = {
        error: "Cannot read what you sent",
        detail: "The app could not read what you sent.",
        hint: "",
      };
      return Response.json(payload, { status: 400 });
    }
  }

  const timeoutMs = timeoutFor(segments);
  const query = new URL(request.url).search;
  const target = `${BACKEND_HTTP_URL}/api/leads/${upstreamPath}${query}`;

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method,
      headers:
        method === "GET"
          ? { accept: "*/*" }
          : { "content-type": "application/json", accept: "application/json" },
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (err) {
    const payload: ProxyFailure = {
      error: "Cannot reach the server",
      detail: describe(err, timeoutMs),
      hint: `Start the server first: ${START_COMMAND}`,
    };
    return Response.json(payload, { status: 502 });
  }

  /* Two of these endpoints do not answer in JSON: the screenshot is a JPEG and
     the export is a CSV. Both are passed straight through, bytes untouched,
     with the content type and the filename the backend chose. Reading either
     one as text and re-encoding it would corrupt the image and strip the
     download name off the export. */
  const upstreamType = upstream.headers.get("content-type") ?? "";
  if (upstream.ok && !upstreamType.includes("json")) {
    const passthrough = new Headers();
    passthrough.set("content-type", upstreamType || "application/octet-stream");
    for (const name of ["content-disposition", "cache-control"]) {
      const value = upstream.headers.get(name);
      if (value) passthrough.set(name, value);
    }
    return new Response(await upstream.arrayBuffer(), {
      status: upstream.status,
      headers: passthrough,
    });
  }

  const text = await upstream.text();
  if (!text) {
    /* A 204 or a 304 cannot legally carry a body, so answering with one would
       throw here instead of reaching the browser. Every leads endpoint sends
       JSON, so an empty body is a broken answer either way, and it is reported
       as a 502 unless the backend already named its own failure. */
    const status = upstream.status >= 400 ? upstream.status : 502;
    return Response.json(
      {
        error: "Empty answer",
        detail: `The server answered ${upstream.status} and sent nothing back.`,
        hint: `Check the server log in the terminal running ${START_COMMAND}`,
      },
      { status },
    );
  }

  let payload: unknown;
  try {
    payload = JSON.parse(text) as unknown;
  } catch {
    return Response.json(
      {
        error: "Answer the app could not read",
        detail: `The server answered ${upstream.status}, and the answer was not in the shape the app reads.`,
        hint: `Check the server log in the terminal running ${START_COMMAND}`,
      },
      { status: 502 },
    );
  }

  return Response.json(payload, { status: upstream.status });
}

/** Reads: the search list, one calling list, one lead, one job. */
export async function GET(request: Request, context: LeadsContext): Promise<Response> {
  return forward(request, context, "GET");
}

/** Writes: build a call context, save an outcome, start a new scrape. */
export async function POST(request: Request, context: LeadsContext): Promise<Response> {
  return forward(request, context, "POST");
}

/** The settings screen, which replaces a whole file rather than appending to it. */
export async function PUT(request: Request, context: LeadsContext): Promise<Response> {
  return forward(request, context, "PUT");
}
