"""
API Gateway – Step 1: Basic Reverse Proxy
==========================================
Forwards every incoming HTTP request to a single hardcoded backend service
and streams the response back to the caller unchanged.

Design decision: We use `httpx.AsyncClient` (rather than the stdlib `http.client`
or `requests`) because FastAPI is fully async; sharing a single long-lived
AsyncClient lets us reuse TCP connections (connection pooling) across requests,
which dramatically reduces per-request latency compared to opening a fresh
connection each time.
"""

from __future__ import annotations

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

# ---------------------------------------------------------------------------
# Shared async HTTP client
# A single client is created at startup and reused for all proxied requests.
# `follow_redirects=False` means we let the caller deal with 3xx responses
# exactly as the backend sends them.
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Return the module-level shared httpx client (lazy init)."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(30.0),  # 30 s total timeout
        )
    return _client


async def close_client() -> None:
    """Cleanly shut down the shared client on app shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# Core forwarding logic
# ---------------------------------------------------------------------------

# HOP-BY-HOP headers must NOT be forwarded between the client and backend.
# They are connection-level metadata that only applies to a single TCP link.
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def _strip_hop_by_hop(headers: httpx.Headers) -> dict[str, str]:
    """Return a plain dict with hop-by-hop headers removed."""
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }


async def forward_request(
    request: Request,
    target_base_url: str,
) -> Response:
    """
    Forward *request* to *target_base_url* and return the backend's response.

    Parameters
    ----------
    request:
        The incoming FastAPI / Starlette request object.
    target_base_url:
        The scheme+host[:port] of the backend, e.g. ``http://users-service:8001``.
        The original path, query string, and body are appended automatically.

    Returns
    -------
    Response
        A FastAPI Response (or StreamingResponse for large bodies) that mirrors
        the backend's status code, headers, and body.
    """
    client = get_client()

    # Build the full target URL: base URL + original path + query string
    target_url = httpx.URL(
        target_base_url.rstrip("/") + str(request.url.path),
    )
    if request.url.query:
        target_url = target_url.copy_with(query=request.url.query.encode())

    # Copy request headers, stripping hop-by-hop and overwriting Host
    forwarded_headers = _strip_hop_by_hop(request.headers)
    forwarded_headers["host"] = target_url.host  # correct Host for backend
    # Standard reverse-proxy header so backends can see the original IP
    forwarded_headers["x-forwarded-for"] = request.client.host if request.client else "unknown"
    forwarded_headers["x-forwarded-proto"] = request.url.scheme

    # Read the request body (safe for all methods; returns b"" for GET/HEAD)
    body = await request.body()

    # --- Make the upstream request ---
    backend_response = await client.request(
        method=request.method,
        url=target_url,
        headers=forwarded_headers,
        content=body,
    )

    # Strip hop-by-hop from the backend response before sending to client
    response_headers = _strip_hop_by_hop(backend_response.headers)

    return Response(
        content=backend_response.content,
        status_code=backend_response.status_code,
        headers=response_headers,
        media_type=backend_response.headers.get("content-type"),
    )
