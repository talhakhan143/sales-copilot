"""Application entry point for the Sales Copilot backend.

Run it with::

    ./run.sh
    # or
    .venv/bin/python -m uvicorn app.main:app --reload

The lifespan owns four process wide resources: the shared Groq client, which
holds the pooled httpx connection, the Twilio and WhatsApp clients, which hold
one each, and a background task that sweeps expired sessions out of the in
memory store.

The two calling clients are modules rather than instances, so they are not
stored on ``app.state`` the way the Groq client is. Their transports are still
opened and closed here, in the same place and for the same reason: a connection
pool that outlives the app leaks sockets, and one that is built per request
throws away every keep alive. Both open even when the provider has no
credentials, because an idle pool costs nothing and it means adding keys to
backend/.env is the only step to a working call.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes_call import router as call_router
from app.api.routes_context import router as context_router
from app.api.routes_leads import router as leads_router
from app.api.routes_practice import router as practice_router
from app.api.routes_twilio import router as twilio_router
from app.api.routes_ws import router as ws_router
from app.config import (
    cors_origin_list,
    leadengine_data_path,
    leadengine_dirs_agree,
    public_wss_base,
    settings,
    twilio_ready,
    whatsapp_cloud_ready,
)
from app.services import twilio_client, whatsapp_client
from app.services.groq_client import GroqClient
from app.services.session_store import store

API_TITLE = "Sales Copilot API"
API_VERSION = "1.0.0"

#: How often expired sessions are swept out of the store, in seconds.
SWEEP_INTERVAL_S = 300.0

logging.basicConfig(
    level=getattr(logging, str(settings.log_level).upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("salescopilot.main")


async def _sweeper() -> None:
    """Drop sessions that have not been touched inside the TTL.

    Runs forever until the lifespan cancels it. A failure in one pass is
    logged and the loop keeps going, because losing the sweeper would leak
    memory for the whole process life.
    """
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            removed = store.sweep(settings.session_ttl_seconds)
            if removed:
                log.info("swept %d expired session(s), %d still live", removed, len(store))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the sweeper must survive a bad pass.
            log.exception("session sweep failed")


def _calling_lines() -> list[str]:
    """Describe the calling providers for the startup banner.

    The banner is where an operator finds out that phone calls are off, so it
    says so plainly instead of listing values they would have to interpret. No
    secret is printed, only whether it is present.

    Returns:
        The banner lines for the calling section.
    """
    address = settings.public_base_url.strip()
    if not public_wss_base():
        reachable = f"{address} is not reachable from outside" if address else "not set"
    else:
        reachable = address

    twilio_line = "ready" if twilio_ready() else "off, needs keys in backend/.env"
    if whatsapp_cloud_ready():
        # The keys are there and it still cannot call. A Cloud API call carries
        # a WebRTC offer this server cannot make, so the button stays off and
        # the banner says why, rather than promising a call that always fails.
        cloud_line = "off, the app cannot place a WhatsApp call yet"
    else:
        cloud_line = "off, link mode still works"

    # The webhook token is derived from this secret. Leave it empty and a new
    # one is made at every start, so a call placed before a restart can no
    # longer reach its own webhooks.
    if settings.call_webhook_secret.strip():
        token_line = "pinned by CALL_WEBHOOK_SECRET"
    else:
        token_line = "new on every start, set CALL_WEBHOOK_SECRET in backend/.env"

    return [
        f"  Public address: {reachable}",
        f"  Twilio call   : {twilio_line}",
        f"  WhatsApp app  : {cloud_line}",
        "  WhatsApp link : ready, free",
        f"  Webhook token : {token_line}",
    ]


def _scraper_ready() -> bool:
    """Report whether a new search could actually open a browser.

    The scraper drives a real Chromium through playwright. On an install that
    was set up before the leads screen existed, the virtualenv is already there
    and playwright was never added to it, so a new search dies one second after
    it starts with an import error nobody sees.

    ``find_spec`` is used rather than an import, so nothing heavy is loaded at
    startup just to answer a banner line.

    Returns:
        True when the playwright package is installed in this virtualenv.
    """
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


def _leads_lines() -> list[str]:
    """Describe the leads screen for the startup banner.

    Two things can be quietly wrong here and both cost the rep a wasted scrape,
    so both are said out loud at startup rather than found later.

    Returns:
        The banner lines for the leads section.
    """
    folder = str(leadengine_data_path())
    if _scraper_ready():
        search_line = "ready"
    else:
        search_line = "off, playwright is missing, run backend/run.sh again"

    lines = [
        f"  Lead data     : {folder}",
        f"  New search    : {search_line}",
    ]
    if not leadengine_dirs_agree():
        lines.append("  WARNING       : a new search writes into leadengine/data in this repo,")
        lines.append("                  not the folder above, so it will not show on the screen")
    return lines


def _banner(origins: list[str], groq_configured: bool) -> str:
    """Build the startup banner printed to the console.

    Args:
        origins: The CORS origins that will be accepted.
        groq_configured: Whether a Groq API key was found.

    Returns:
        The full multi line banner, ready to print.
    """
    key_line = "present" if groq_configured else "MISSING, set GROQ_API_KEY in backend/.env"
    width = 74
    rule = "=" * width
    lines = [
        "",
        rule,
        f"  {API_TITLE} v{API_VERSION}",
        rule,
        f"  STT model     : {settings.stt_model}",
        f"  LLM model     : {settings.llm_model}",
        f"  Groq base url : {settings.groq_base_url}",
        f"  Groq api key  : {key_line}",
        f"  Jina reader   : {settings.jina_base_url}",
        f"  CORS origins  : {', '.join(origins) if origins else 'none'}",
        f"  Session TTL   : {settings.session_ttl_seconds}s",
        f"  Log level     : {settings.log_level}",
        rule,
        *_calling_lines(),
        rule,
        *_leads_lines(),
        rule,
        "  REST      http://127.0.0.1:8000/api/health",
        "  Docs      http://127.0.0.1:8000/docs",
        "  Providers http://127.0.0.1:8000/api/call/providers",
        "  WebSocket ws://127.0.0.1:8000/ws/teleprompter?session_id=<uuid>",
        "  Twilio    POST /twilio/voice/<session_id>, media on /ws/twilio",
        rule,
        "",
    ]
    return "\n".join(lines)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the shared clients and the session sweeper.

    Args:
        app: The FastAPI application being started.

    Yields:
        None, once the process is ready to serve traffic.
    """
    groq = GroqClient(
        api_key=settings.groq_api_key,
        base_url=settings.groq_base_url,
        stt_model=settings.stt_model,
        llm_model=settings.llm_model,
    )
    await groq.startup()
    app.state.groq = groq

    await twilio_client.startup()
    await whatsapp_client.startup()

    sweeper_task = asyncio.create_task(_sweeper(), name="session-sweeper")
    app.state.sweeper = sweeper_task

    origins = cors_origin_list()
    print(_banner(origins, groq.configured), flush=True)
    if not groq.configured:
        log.warning(
            "GROQ_API_KEY is empty. Transcription and suggestions are disabled. "
            "Get a free key at https://console.groq.com/keys and put it in backend/.env"
        )
    if not _scraper_ready():
        log.warning(
            "playwright is not installed in this virtualenv, so a new lead search "
            "will fail as soon as it starts. Run backend/run.sh again, or install "
            "it by hand with: uv pip install --python backend/.venv/bin/python "
            "-r backend/requirements.txt"
        )
    if not leadengine_dirs_agree():
        log.warning(
            "LEADENGINE_DATA_DIR points at %s, but a new search always writes into "
            "the leadengine/data folder inside this repo. The leads screen will not "
            "list a search started from this app while these two differ.",
            leadengine_data_path(),
        )

    try:
        yield
    finally:
        sweeper_task.cancel()
        try:
            # asyncio.wait, not "suppress(CancelledError): await sweeper_task".
            # Awaiting a cancelled task directly raises CancelledError here, and
            # suppressing it would also swallow a cancellation aimed at the
            # lifespan itself. The nested finally blocks keep every client
            # teardown unconditional either way, so one that fails cannot leave
            # the other one open.
            await asyncio.wait({sweeper_task})
        finally:
            try:
                await twilio_client.shutdown()
            finally:
                try:
                    await whatsapp_client.shutdown()
                finally:
                    await groq.shutdown()
                    log.info("shutdown complete")


app = FastAPI(
    title=API_TITLE,
    version=API_VERSION,
    description=(
        "Realtime cold call copilot. Prepare a call context over REST, place the "
        "call through one of four providers, then stream raw PCM into the "
        "teleprompter WebSocket and read the suggestions aloud."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origin_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(context_router)
app.include_router(practice_router)
app.include_router(leads_router)
app.include_router(call_router)
app.include_router(twilio_router)
app.include_router(ws_router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Turn any unhandled error into a JSON 500 and log the full traceback.

    Args:
        request: The request that blew up.
        exc: The exception raised.

    Returns:
        A JSON 500 response carrying a short, safe message.
    """
    log.error(
        "unhandled error on %s %s\n%s",
        request.method,
        request.url.path,
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error.",
            "error": type(exc).__name__,
            "path": request.url.path,
        },
    )


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    """Tiny banner so hitting the root in a browser is not a 404.

    Returns:
        The service name, version and the useful entry points.
    """
    return {
        "name": API_TITLE,
        "version": API_VERSION,
        "docs": "/docs",
        "health": "/api/health",
        "providers": "/api/call/providers",
        "websocket": "/ws/teleprompter?session_id=<uuid>",
    }
