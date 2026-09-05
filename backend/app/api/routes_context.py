"""REST routes for call preparation, session lookup, health and quick actions.

Everything here is mounted under the ``/api`` prefix. The shapes returned by
these handlers are frozen by the wire contract, section 2.1, and the frontend
proxies (``/api/prepare-context`` and ``/api/health`` in Next.js) forward to
them verbatim, so do not rename any JSON key. The response models carry
camelCase aliases, FastAPI serializes by alias, so Python stays snake_case and
the wire stays camelCase.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.models import (
    HealthResponse,
    ModelsModel,
    PrepareContextRequest,
    PrepareContextResponse,
    QuickActionsResponse,
    SessionInfoResponse,
)
from app.prompts import QUICK_ACTIONS
from app.services.context_builder import prepare_context
from app.services.session_store import store

log = logging.getLogger("salescopilot.api.context")

router = APIRouter(prefix="/api", tags=["context"])


def get_groq(request: Request) -> Any:
    """Return the shared Groq client stored on the application state.

    The client is created once by the lifespan context manager in
    ``app.main`` and shared by every request, because it owns a pooled
    ``httpx.AsyncClient``.

    Args:
        request: The incoming request, used only to reach ``app.state``.

    Returns:
        The ``GroqClient`` instance held on ``app.state.groq``.

    Raises:
        HTTPException: 503 when the lifespan has not populated the state yet,
            which in practice only happens if startup failed.
    """
    groq = getattr(request.app.state, "groq", None)
    if groq is None:
        raise HTTPException(status_code=503, detail="Groq client is not ready yet.")
    return groq


@router.post("/prepare-context", response_model=PrepareContextResponse)
async def post_prepare_context(
    payload: PrepareContextRequest,
    request: Request,
) -> PrepareContextResponse:
    """Fuse the knowledge base and the scraped prospect page into a session.

    Nothing about the prospect URL is fatal here. A typo, a host that does not
    resolve, a private address, a dead page or a Jina timeout all come back as
    200 with ``scrapeOk`` false plus a readable ``scrapeError``, and the system
    prompt is built from the knowledge base alone, because the rep must still be
    able to start the call. That is the contract, section 2.1, and the setup
    page renders it as an amber notice rather than a blocking error.

    The SSRF guard still holds: ``scrape_url`` normalizes the URL and checks
    ``is_public_host`` itself before any network call, so a local or non public
    target is refused, it is just reported instead of raised. A malformed body
    is rejected by pydantic with a 422 before this function runs.

    Args:
        payload: The validated request body.
        request: The incoming request, used to reach the shared Groq client.

    Returns:
        The prepared context, including the new session id and system prompt.

    Raises:
        HTTPException: 400 only if the context builder itself rejects the URL,
            which the current builder never does.
    """
    groq = get_groq(request)
    try:
        return await prepare_context(payload, groq_configured=bool(groq.configured))
    except ValueError as exc:
        # normalize_url can also fire from inside the builder, keep it a 400.
        raise HTTPException(
            status_code=400,
            detail=f"That prospect URL is not usable: {exc}",
        ) from exc


@router.get("/session/{session_id}", response_model=SessionInfoResponse)
async def get_session(session_id: str) -> SessionInfoResponse:
    """Report whether a session is still alive and how much transcript it holds.

    An unknown or expired id is not an error, it answers 200 with ``exists``
    false so the frontend can send the user back to the setup page without
    treating it as a failure. ``turns`` is the number of transcript turns
    held, not the turns themselves.

    Args:
        session_id: The uuid4 string handed out by prepare context.

    Returns:
        The session info model.
    """
    session = store.get(session_id)
    if session is None:
        return SessionInfoResponse(
            session_id=session_id,
            exists=False,
            turns=0,
            created_at=0.0,
        )
    return SessionInfoResponse(
        session_id=session.id,
        exists=True,
        turns=len(session.turns),
        created_at=session.created_at,
    )


@router.get("/health", response_model=HealthResponse)
async def get_health(request: Request) -> HealthResponse:
    """Liveness probe used by the setup page health pill.

    Args:
        request: The incoming request, used to reach the shared Groq client.

    Returns:
        The health model with the live session count and key status.
    """
    groq = get_groq(request)
    return HealthResponse(
        status="ok",
        groq_configured=bool(groq.configured),
        sessions=len(store),
        version=str(request.app.version),
        models=ModelsModel(stt=settings.stt_model, llm=settings.llm_model),
    )


@router.get("/quick-actions", response_model=QuickActionsResponse)
async def get_quick_actions() -> QuickActionsResponse:
    """Return the frozen objection buttons shown in the call dashboard.

    Returns:
        The actions model, each item carrying ``key``, ``label``, ``icon``
        and ``hint``.
    """
    return QuickActionsResponse.from_entries(QUICK_ACTIONS)
