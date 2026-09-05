"""Application entry point for the Sales Copilot backend.

Run it with::

    ./run.sh
    # or
    .venv/bin/python -m uvicorn app.main:app --reload

The lifespan owns two process wide resources: the shared Groq client, which
holds the pooled httpx connection, and a background task that sweeps expired
sessions out of the in memory store.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes_context import router as context_router
from app.api.routes_practice import router as practice_router
from app.api.routes_ws import router as ws_router
from app.config import cors_origin_list, settings
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
        "  REST      http://127.0.0.1:8000/api/health",
        "  Docs      http://127.0.0.1:8000/docs",
        "  WebSocket ws://127.0.0.1:8000/ws/teleprompter?session_id=<uuid>",
        rule,
        "",
    ]
    return "\n".join(lines)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the shared Groq client and the session sweeper.

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

    sweeper_task = asyncio.create_task(_sweeper(), name="session-sweeper")
    app.state.sweeper = sweeper_task

    origins = cors_origin_list()
    print(_banner(origins, groq.configured), flush=True)
    if not groq.configured:
        log.warning(
            "GROQ_API_KEY is empty. Transcription and suggestions are disabled. "
            "Get a free key at https://console.groq.com/keys and put it in backend/.env"
        )

    try:
        yield
    finally:
        sweeper_task.cancel()
        try:
            # asyncio.wait, not "suppress(CancelledError): await sweeper_task".
            # Awaiting a cancelled task directly raises CancelledError here, and
            # suppressing it would also swallow a cancellation aimed at the
            # lifespan itself. The inner finally keeps the client teardown
            # unconditional either way.
            await asyncio.wait({sweeper_task})
        finally:
            await groq.shutdown()
            log.info("shutdown complete")


app = FastAPI(
    title=API_TITLE,
    version=API_VERSION,
    description=(
        "Realtime cold call copilot. Prepare a call context over REST, then stream "
        "raw PCM into the teleprompter WebSocket and read the suggestions aloud."
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
        "websocket": "/ws/teleprompter?session_id=<uuid>",
    }
