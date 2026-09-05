"""HTTP and WebSocket route modules for the Sales Copilot API.

This package holds the transport layer only. Business logic lives in
``app.services``. Two routers are exported by their modules:

* ``app.api.routes_context.router`` mounts the REST surface under ``/api``.
* ``app.api.routes_ws.router`` mounts the realtime teleprompter socket at
  ``/ws/teleprompter``.
"""

from __future__ import annotations
