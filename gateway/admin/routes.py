"""
Admin API (Step 9)
===================
REST endpoints to manage the gateway at runtime without redeploying:

- Register/deregister backend services
- View live metrics and circuit breaker states
- Toggle rate limit configurations
- View/invalidate cache
- Manage API keys

All admin endpoints live under ``/gateway/admin/`` which is exempt from
the auth middleware (but could be locked down with a separate admin token
in production).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("gateway.admin")

router = APIRouter(prefix="/gateway/admin", tags=["Admin"])


# ---------------------------------------------------------------------------
# Backend service management
# ---------------------------------------------------------------------------
@router.get("/services")
async def list_services(request: Request) -> dict:
    """List all registered backend services and their instances."""
    # Import here to avoid circular imports
    from gateway.main import route_table
    routes = route_table.all_routes()
    return {
        "services": [
            {
                "prefix": r.prefix,
                "backends": [{"url": b.url, "weight": b.weight} for b in r.backends],
                "strip_prefix": r.strip_prefix,
                "description": r.description,
            }
            for r in routes
        ]
    }


@router.get("/health-detail")
async def detailed_health(request: Request) -> dict:
    """Detailed health including circuit breaker states."""
    try:
        from gateway.circuit_breaker.cb import CircuitBreakerRegistry
        registry = CircuitBreakerRegistry()
        breakers = registry.get_all()
        cb_states = {name: {
            "state": cb.state,
            "failure_count": cb._failure_count,
            "success_count": cb._success_count,
        } for name, cb in breakers.items()}
    except ImportError:
        cb_states = {}

    return {
        "status": "ok",
        "circuit_breakers": cb_states,
    }


# ---------------------------------------------------------------------------
# Rate limit configuration
# ---------------------------------------------------------------------------
@router.get("/rate-limits")
async def get_rate_limits() -> dict:
    """View current rate limit tier configuration."""
    try:
        from gateway.middleware.rate_limit import TIER_LIMITS
        return {
            "tiers": {
                tier: {"requests_per_minute": cfg["rate"], "burst": cfg["burst"]}
                for tier, cfg in TIER_LIMITS.items()
            }
        }
    except (ImportError, NameError):
        return {"error": "Rate limiter not loaded"}


@router.put("/rate-limits/{tier}")
async def update_rate_limit(tier: str, request: Request) -> dict:
    """Update rate limit for a specific tier."""
    try:
        from gateway.middleware.rate_limit import TIER_LIMITS
        body = await request.json()
        if tier not in TIER_LIMITS:
            return JSONResponse(status_code=404, content={"error": f"Unknown tier: {tier}"})
        if "rate" in body:
            TIER_LIMITS[tier]["rate"] = int(body["rate"])
        if "burst" in body:
            TIER_LIMITS[tier]["burst"] = int(body["burst"])
        logger.info("Rate limit updated for tier %s: %s", tier, TIER_LIMITS[tier])
        return {"tier": tier, "config": TIER_LIMITS[tier]}
    except (ImportError, NameError):
        return JSONResponse(status_code=503, content={"error": "Rate limiter not loaded"})


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------
@router.get("/cache")
async def cache_info() -> dict:
    """View cache configuration."""
    try:
        from gateway.middleware.cache import ROUTE_CACHE_TTL
        return {"route_ttls": ROUTE_CACHE_TTL}
    except ImportError:
        return {"error": "Cache not loaded"}


@router.delete("/cache")
async def invalidate_cache(prefix: Optional[str] = None) -> dict:
    """Invalidate cached responses. Optionally filter by path prefix."""
    try:
        from gateway.middleware.cache import get_cache
        cache = get_cache()
        if prefix:
            count = await cache.clear_prefix(f"cache:GET:{prefix}")
            return {"invalidated": count, "prefix": prefix}
        else:
            count = await cache.clear_prefix("cache:")
            return {"invalidated": count, "prefix": "all"}
    except ImportError:
        return JSONResponse(status_code=503, content={"error": "Cache not loaded"})


# ---------------------------------------------------------------------------
# API Key management
# ---------------------------------------------------------------------------
@router.get("/api-keys")
async def list_api_keys() -> dict:
    """List registered API keys (masked for security)."""
    try:
        from gateway.middleware.auth import API_KEY_REGISTRY
        return {
            "keys": [
                {
                    "key_preview": f"{k[:8]}...{k[-4:]}" if len(k) > 12 else k[:4] + "...",
                    "client_id": v[0],
                    "tier": v[1],
                }
                for k, v in API_KEY_REGISTRY.items()
            ]
        }
    except ImportError:
        return {"error": "Auth not loaded"}


@router.post("/api-keys")
async def create_api_key(request: Request) -> dict:
    """Register a new API key."""
    try:
        import secrets
        from gateway.middleware.auth import API_KEY_REGISTRY
        body = await request.json()
        client_id = body.get("client_id", f"client-{secrets.token_hex(4)}")
        tier = body.get("tier", "free")
        new_key = body.get("key") or f"gw-{secrets.token_hex(16)}"
        API_KEY_REGISTRY[new_key] = (client_id, tier)
        logger.info("API key created for client %s (tier: %s)", client_id, tier)
        return {"key": new_key, "client_id": client_id, "tier": tier}
    except ImportError:
        return JSONResponse(status_code=503, content={"error": "Auth not loaded"})


@router.delete("/api-keys/{key}")
async def delete_api_key(key: str) -> dict:
    """Delete an API key."""
    try:
        from gateway.middleware.auth import API_KEY_REGISTRY
        if key in API_KEY_REGISTRY:
            del API_KEY_REGISTRY[key]
            return {"deleted": True, "key": key}
        return JSONResponse(status_code=404, content={"error": "Key not found"})
    except ImportError:
        return JSONResponse(status_code=503, content={"error": "Auth not loaded"})
