"""
API Gateway – Main Application Entry Point (Step 10)
====================================================
Wires together the FastAPI app with all portfolio project steps:
  - Step 1: reverse proxy (httpx forwarding)
  - Step 2: dynamic path-prefix routing with hot-reload
  - Step 3: authentication middleware (API key + JWT)
  - Step 4: rate limiting middleware (Token Bucket)
  - Step 5: load balancing & health checking
  - Step 6: response caching
  - Step 7: structured logging & request ID propagation
  - Step 8: circuit breaker
  - Step 9: admin API
  - Step 10: LLM-aware routing & token tracking
"""

from __future__ import annotations

import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from gateway.config import settings
from gateway.proxy.reverse_proxy import close_client, forward_request
from gateway.routing.router import RouteFileWatcher, RouteTable

# Middlewares
from gateway.middleware.request_id import RequestIDMiddleware
from gateway.logging_tracing.logger import LoggingMiddleware, metrics_endpoint
from gateway.middleware.cache import CacheMiddleware
from gateway.middleware.auth import AuthMiddleware
from gateway.middleware.rate_limit import RateLimitMiddleware
from gateway.llm.llm_gateway import LLMGatewayMiddleware

# Modules
from gateway.loadbalancer.lb import LoadBalancer
from gateway.circuit_breaker.cb import CircuitBreakerRegistry
from gateway.admin.routes import router as admin_router


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("gateway")


# ---------------------------------------------------------------------------
# Core Module Instances
# ---------------------------------------------------------------------------
route_table = RouteTable()
_file_watcher: RouteFileWatcher | None = None

load_balancer = LoadBalancer()
circuit_breaker_registry = CircuitBreakerRegistry()


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="API Gateway",
    version="1.0.0",
    description="Portfolio API Gateway (Complete)",
    docs_url="/gateway/docs",
    redoc_url="/gateway/redoc",
)

# ---------------------------------------------------------------------------
# Middlewares (Order is important! Bottom-most is executed FIRST)
# ---------------------------------------------------------------------------
app.add_middleware(LLMGatewayMiddleware)      # Step 10: LLM routing & usage tracking
app.add_middleware(RateLimitMiddleware)       # Step 4: Rate limit (needs auth client)
app.add_middleware(CacheMiddleware)           # Step 6: Caching
app.add_middleware(AuthMiddleware)            # Step 3: Authentication
app.add_middleware(LoggingMiddleware)         # Step 7: Logging (needs Request ID)
app.add_middleware(RequestIDMiddleware)       # Step 7: Request ID injection

# ---------------------------------------------------------------------------
# Routers & Endpoints
# ---------------------------------------------------------------------------
app.include_router(admin_router)
app.add_route("/gateway/metrics", metrics_endpoint, methods=["GET"])


# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup() -> None:
    global _file_watcher

    # Load dynamic routes
    if settings.routes_file:
        routes_path = Path(settings.routes_file)
        if routes_path.exists():
            _file_watcher = RouteFileWatcher(
                filepath=routes_path,
                route_table=route_table,
                poll_interval=settings.routes_poll_interval,
            )
            _file_watcher.start()
            logger.info("Dynamic routing enabled from %s", routes_path)
        else:
            logger.warning("Routes file %s not found – falling back to BACKEND_URL", routes_path)
    else:
        logger.info("No ROUTES_FILE configured – all traffic → %s", settings.backend_url)

    # Start health checks
    load_balancer.start_health_checks()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if _file_watcher:
        _file_watcher.stop()
    load_balancer.stop_health_checks()
    await close_client()
    logger.info("Gateway shut down cleanly.")


# ---------------------------------------------------------------------------
# Health check (never proxied)
# ---------------------------------------------------------------------------
@app.get("/gateway/health", tags=["Gateway"])
async def health() -> dict:
    """Returns gateway liveness status and loaded routes."""
    routes = route_table.all_routes()
    return {
        "status": "ok",
        "step": 10,
        "routes_loaded": len(routes),
        "routes": [
            {"prefix": r.prefix, "backends": [b.url for b in r.backends]}
            for r in routes
        ],
        "fallback_backend": settings.backend_url,
    }


# ---------------------------------------------------------------------------
# Catch-all reverse proxy route
# ---------------------------------------------------------------------------
@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    tags=["Proxy"],
    summary="Reverse proxy – forward to backend",
    include_in_schema=False,
)
async def proxy(request: Request, path: str) -> Response:
    """
    Resolve the target backend via the dynamic route table, then forward.
    """
    target_url: str | None = None
    service_name: str

    # --- Step 10: LLM override ---
    if hasattr(request.state, "llm_backend"):
        target_url = getattr(request.state, "llm_backend")
        service_name = getattr(request.state, "llm_model", "llm-backend")

    # --- Step 2 & 5: dynamic route resolution & load balancing ---
    elif route := route_table.resolve(request.url.path):
        service_name = route.prefix
        
        # Ensure backends are registered in LB
        for b in route.backends:
            load_balancer.register(service_name, b.url)

        # Select backend via Load Balancer
        backend_inst = load_balancer.select(service_name, strategy="least-connections")
        if backend_inst:
            target_url = backend_inst.url

            # Optional prefix stripping
            if route.strip_prefix:
                new_path = request.url.path[len(route.prefix):] or "/"
                request.scope["path"] = new_path
        else:
            return JSONResponse(status_code=503, content={"error": "No healthy backends available"})
            
    else:
        # --- Fallback ---
        target_url = settings.backend_url
        service_name = "fallback"

    # --- Step 8: Circuit Breaker ---
    cb = circuit_breaker_registry.get_or_create(service_name)
    if not cb.can_execute():
        return JSONResponse(status_code=503, content={"error": "Circuit breaker open for this service"})

    # --- Forward Request ---
    try:
        load_balancer.mark_request_start(service_name, target_url)
        response = await forward_request(request, target_url)
        
        # Treat 5xx as backend failures for the circuit breaker
        if response.status_code >= 500:
            cb.record_failure()
        else:
            cb.record_success()
            
        return response
    except Exception as exc:
        cb.record_failure()
        logger.error("Upstream error for %s: %s", target_url, exc)
        return JSONResponse(
            status_code=502,
            content={"error": "Bad Gateway", "detail": str(exc)},
        )
    finally:
        load_balancer.mark_request_end(service_name, target_url)


# ---------------------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        "gateway.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        log_level=settings.log_level.lower(),
    )
