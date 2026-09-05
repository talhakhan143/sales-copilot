"""Sales Copilot backend package.

A realtime cold call copilot API built on FastAPI. The package exposes a small
REST surface for building call context and a single WebSocket endpoint that
carries raw PCM audio in and streamed suggestion tokens out.

Attributes:
    __version__: Semantic version of the backend, reported by the health route
        and by the FastAPI application metadata.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
