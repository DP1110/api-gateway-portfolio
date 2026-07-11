"""
Structured Logging & Metrics (Step 7)
======================================
JSON-formatted request/response logging with latency tracking, plus a
Prometheus-compatible ``/metrics`` endpoint.

Design decisions
-----------------
- **Structured JSON logs**: every request produces a single JSON line with
  method, path, status, latency_ms, client_id, request_id.  This is what
  production systems like ELK and Datadog expect.
- **Prometheus metrics**: we expose a /gateway/metrics endpoint using the
  ``prometheus_client`` library.  Metrics include:
  - ``gateway_requests_total`` (counter, by method/path/status)
  - ``gateway_request_duration_seconds`` (histogram)
  - ``gateway_active_requests`` (gauge)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

logger = logging.getLogger("gateway.access")

# Try to import prometheus_client; gracefully degrade if not installed
try:
    from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Prometheus metrics (created once at module load)
# ---------------------------------------------------------------------------
if PROMETHEUS_AVAILABLE:
    REQUEST_COUNT = Counter(
        "gateway_requests_total",
        "Total HTTP requests processed by the gateway",
        ["method", "path_prefix", "status_code"],
    )
    REQUEST_DURATION = Histogram(
        "gateway_request_duration_seconds",
        "Request duration in seconds",
        ["method", "path_prefix"],
        buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    )
    ACTIVE_REQUESTS = Gauge(
        "gateway_active_requests",
        "Number of in-flight requests",
    )


def _path_prefix(path: str) -> str:
    """Collapse paths to their first segment for metric cardinality control.
    E.g. /users/123/orders -> /users
    """
    parts = path.strip("/").split("/")
    return f"/{parts[0]}" if parts and parts[0] else "/"


# ---------------------------------------------------------------------------
# Logging middleware
# ---------------------------------------------------------------------------
class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Structured JSON access log + Prometheus metrics for every request.

    Emits one JSON log line per request with:
      timestamp, request_id, method, path, status, latency_ms,
      client_id, client_tier
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        start = time.perf_counter()

        if PROMETHEUS_AVAILABLE:
            ACTIVE_REQUESTS.inc()

        try:
            response = await call_next(request)
        except Exception:
            if PROMETHEUS_AVAILABLE:
                ACTIVE_REQUESTS.dec()
            raise

        latency = time.perf_counter() - start
        status = response.status_code

        if PROMETHEUS_AVAILABLE:
            ACTIVE_REQUESTS.dec()
            prefix = _path_prefix(request.url.path)
            REQUEST_COUNT.labels(
                method=request.method,
                path_prefix=prefix,
                status_code=str(status),
            ).inc()
            REQUEST_DURATION.labels(
                method=request.method,
                path_prefix=prefix,
            ).observe(latency)

        # Build structured log entry
        request_id = getattr(request.state, "request_id", "-")
        client = getattr(request.state, "client", None)

        log_entry = {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "query": request.url.query or "",
            "status": status,
            "latency_ms": round(latency * 1000, 2),
            "client_id": client.client_id if client else "-",
            "client_tier": client.tier if client else "-",
            "client_ip": request.client.host if request.client else "-",
        }

        # Log at appropriate level based on status code
        if status >= 500:
            logger.error(json.dumps(log_entry, ensure_ascii=True))
        elif status >= 400:
            logger.warning(json.dumps(log_entry, ensure_ascii=True))
        else:
            logger.info(json.dumps(log_entry, ensure_ascii=True))

        return response


# ---------------------------------------------------------------------------
# Prometheus /metrics endpoint handler
# ---------------------------------------------------------------------------
async def metrics_endpoint(request: Request) -> Response:
    """Return Prometheus-compatible metrics in text exposition format."""
    if not PROMETHEUS_AVAILABLE:
        return Response(
            content="# prometheus_client not installed\n",
            media_type="text/plain",
            status_code=503,
        )
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
