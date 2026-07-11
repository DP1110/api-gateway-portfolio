"""
Authentication Middleware (Step 3)
===================================
Two authentication strategies that can be used independently or together:

1. **API Key** – a static key passed in the ``X-API-Key`` header, looked up
   in a local registry of valid keys.  Simple, stateless, fast.

2. **JWT (Bearer token)** – a signed JSON Web Token in the ``Authorization:
   Bearer <token>`` header, verified using HMAC-SHA256 with a shared secret.
   Provides richer claims (client_id, tier, exp) and is the standard for
   machine-to-machine auth.

Design decisions
-----------------
- **Middleware pattern**: implemented as a Starlette ``BaseHTTPMiddleware``
  so it intercepts ALL requests before route handlers run, including the
  catch-all proxy route.  This is the typical API gateway pattern — auth
  happens at the edge, not in individual backends.
- **Two modes, one pipeline**: the middleware first checks for a Bearer
  token (JWT), then falls back to X-API-Key.  This allows gradual migration
  from API keys to JWT without breaking existing clients.
- **Client identity propagation**: on success the middleware injects
  ``X-Client-ID`` and ``X-Client-Tier`` headers so downstream backends
  know who the caller is without re-validating credentials.

Security notes
--------------
- In production you'd store API keys hashed (bcrypt) in PostgreSQL and
  cache lookups in Redis.  For this portfolio project we use an in-memory
  dict to keep Step 3 self-contained.
- JWT secret should come from a vault; here it's an env var.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from gateway.config import settings

logger = logging.getLogger("gateway.auth")


# ---------------------------------------------------------------------------
# Client identity (attached to request.state after successful auth)
# ---------------------------------------------------------------------------
@dataclass
class ClientIdentity:
    """Authenticated caller metadata, passed to downstream middleware."""
    client_id: str
    tier: str = "free"   # free | pro | admin
    auth_method: str = "api_key"  # api_key | jwt


# ---------------------------------------------------------------------------
# API Key registry (in-memory for Step 3; Step 9 will add CRUD via Admin API)
# ---------------------------------------------------------------------------
# Key → (client_id, tier)
# In production these would live in PostgreSQL with bcrypt hashes.
API_KEY_REGISTRY: dict[str, tuple[str, str]] = {
    "test-key-free": ("client-free-1", "free"),
    "test-key-pro":  ("client-pro-1",  "pro"),
    "test-key-admin": ("admin-1",      "admin"),
}


def lookup_api_key(key: str) -> Optional[ClientIdentity]:
    """Look up an API key and return a ClientIdentity, or None."""
    entry = API_KEY_REGISTRY.get(key)
    if entry is None:
        return None
    client_id, tier = entry
    return ClientIdentity(client_id=client_id, tier=tier, auth_method="api_key")


# ---------------------------------------------------------------------------
# JWT validation
# ---------------------------------------------------------------------------
def validate_jwt(token: str) -> Optional[ClientIdentity]:
    """
    Decode and validate a JWT.

    Expected claims:
      sub   – client ID
      tier  – client tier (free/pro/admin)
      exp   – expiration timestamp (standard JWT claim)

    Returns ClientIdentity on success, None on any validation failure.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            options={"require": ["sub", "exp"]},
        )
        return ClientIdentity(
            client_id=payload["sub"],
            tier=payload.get("tier", "free"),
            auth_method="jwt",
        )
    except jwt.ExpiredSignatureError:
        logger.debug("JWT expired")
        return None
    except jwt.InvalidTokenError as exc:
        logger.debug("JWT invalid: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Paths that bypass auth (gateway-internal endpoints)
# ---------------------------------------------------------------------------
AUTH_EXEMPT_PREFIXES = (
    "/gateway/",  # health, docs, routes, metrics, admin (admin has its own auth)
)


def _is_exempt(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in AUTH_EXEMPT_PREFIXES)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
class AuthMiddleware(BaseHTTPMiddleware):
    """
    Authenticate every proxied request via API key or JWT.

    Resolution order:
      1. ``Authorization: Bearer <jwt>`` header → JWT validation
      2. ``X-API-Key: <key>`` header → API key lookup
      3. Neither present → 401 Unauthorized

    On success, the resolved ``ClientIdentity`` is stored in
    ``request.state.client`` and forwarded as ``X-Client-ID`` /
    ``X-Client-Tier`` headers to the backend.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip auth for gateway-internal endpoints
        if _is_exempt(request.url.path):
            return await call_next(request)

        identity: Optional[ClientIdentity] = None

        # --- Strategy 1: JWT Bearer token ---
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            identity = validate_jwt(token)
            if identity is None:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Unauthorized", "detail": "Invalid or expired JWT"},
                )

        # --- Strategy 2: API Key ---
        if identity is None:
            api_key = request.headers.get("x-api-key", "")
            if api_key:
                identity = lookup_api_key(api_key)
                if identity is None:
                    return JSONResponse(
                        status_code=403,
                        content={"error": "Forbidden", "detail": "Invalid API key"},
                    )

        # --- No credentials at all ---
        if identity is None:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Unauthorized",
                    "detail": "Missing credentials. Provide Authorization: Bearer <jwt> or X-API-Key header.",
                },
            )

        # Store identity on request.state for downstream middleware
        request.state.client = identity

        # Continue to the next handler
        response = await call_next(request)

        # Tag the response so clients can see which identity was used (debugging)
        response.headers["X-Authenticated-Client"] = identity.client_id
        return response
