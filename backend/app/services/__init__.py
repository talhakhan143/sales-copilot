"""Service layer for the Sales Copilot backend.

Modules in this package hold everything that is not HTTP or WebSocket plumbing:
audio segmentation (`audio`), the Groq REST client (`groq_client`), the Jina
Reader scraper (`jina`), the prompt fusion step (`context_builder`) and the in
memory session store (`session_store`).

Nothing in this package imports from `app.api`, so the services stay unit
testable without spinning up a FastAPI application.
"""

from __future__ import annotations
