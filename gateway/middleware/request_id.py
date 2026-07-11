"""
Request ID Middleware (Step 7)
===============================
Injects a unique ``X-Request-ID`` header into every request so that log
entries, upstream calls, and responses can be correlated end-to-end.

Design decisions
-----------------
- **UUID4**: globally unique without coordination; no collision risk even
  across multiple gateway instances behind a load balancer.
- **Preserve client-provided ID**: if the caller already sends
  ``X-Request-ID``, we keep it.  This lets upstream services trace through
  multiple gateways.
"""

from __future__ import annotations

import uuid
import logging

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

logger = logging.getLogger("gateway.request_id")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Ensure every request has an ``X-Request-ID`` header.

    - If the caller already provided one, keep it.
    - Otherwise generate a UUID4 and inject it.
    - Echo the ID back in the response for correlation.
    - Store it in ``request.state.request_id`` for downstream middleware/logging.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Use caller-provided ID or generate a new one
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())

        # Stash on request.state for structured logging
        request.state.request_id = request_id

        response = await call_next(request)

        # Echo in response so the caller can correlate
        response.headers["X-Request-ID"] = request_id
        return response
