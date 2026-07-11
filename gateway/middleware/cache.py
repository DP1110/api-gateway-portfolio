"""
Response Cache Middleware (Step 6)
====================================
Caches GET request responses in Redis with configurable TTL per route,
with cache invalidation support.

Design decisions
-----------------
- **GET-only caching**: mutating methods (POST/PUT/DELETE) are never cached
  — this follows HTTP semantics and avoids stale-write bugs.
- **Cache key**: ``cache:{method}:{path}:{sorted_query_params}`` — this
  ensures different query strings are cached separately while normalising
  parameter order so ``?a=1&b=2`` and ``?b=2&a=1`` hit the same key.
- **Redis backend with in-memory fallback**: if Redis is unavailable, we
  fall back to a simple TTL dict cache so the gateway doesn't crash.
- **Per-route TTL**: configured via the route config (``cache_ttl_seconds``).
  Routes without a TTL are not cached (pass-through).
- **Cache-Control headers**: we respect ``Cache-Control: no-cache`` from
  callers and add ``X-Cache: HIT/MISS`` to responses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Optional
from urllib.parse import parse_qs, urlencode

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response as StarletteResponse

logger = logging.getLogger("gateway.cache")


# ---------------------------------------------------------------------------
# In-memory TTL cache (fallback when Redis is unavailable)
# ---------------------------------------------------------------------------
class InMemoryCache:
    """Simple dict-based cache with per-key TTL expiration."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, dict]] = {}  # key -> (expires_at, data)

    async def get(self, key: str) -> Optional[dict]:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if time.time() > expires_at:
            del self._store[key]
            return None
        return data

    async def set(self, key: str, data: dict, ttl: int) -> None:
        self._store[key] = (time.time() + ttl, data)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def clear_prefix(self, prefix: str) -> int:
        """Delete all keys starting with prefix. Returns count deleted."""
        to_delete = [k for k in self._store if k.startswith(prefix)]
        for k in to_delete:
            del self._store[k]
        return len(to_delete)


# ---------------------------------------------------------------------------
# Redis cache wrapper
# ---------------------------------------------------------------------------
class RedisCache:
    """Redis-backed cache using redis-py async client."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    async def get(self, key: str) -> Optional[dict]:
        raw = await self._redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def set(self, key: str, data: dict, ttl: int) -> None:
        await self._redis.set(key, json.dumps(data), ex=ttl)

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def clear_prefix(self, prefix: str) -> int:
        """Delete all keys matching prefix* using SCAN (non-blocking)."""
        count = 0
        async for key in self._redis.scan_iter(match=f"{prefix}*", count=100):
            await self._redis.delete(key)
            count += 1
        return count


# ---------------------------------------------------------------------------
# Cache key builder
# ---------------------------------------------------------------------------
def build_cache_key(method: str, path: str, query: str) -> str:
    """
    Build a deterministic cache key from method + path + sorted query params.

    Sorting query params ensures ``?a=1&b=2`` and ``?b=2&a=1`` produce
    the same key, which is important for cache hit rate.
    """
    # Normalise query string: parse, sort, re-encode
    params = parse_qs(query or "", keep_blank_values=True)
    sorted_query = urlencode(sorted(params.items()), doseq=True)

    raw_key = f"cache:{method}:{path}:{sorted_query}"
    # Hash long keys to stay within Redis key length best practices
    if len(raw_key) > 200:
        hashed = hashlib.sha256(raw_key.encode()).hexdigest()[:16]
        raw_key = f"cache:{method}:{path}:h:{hashed}"
    return raw_key


# ---------------------------------------------------------------------------
# Global cache instance (initialised at startup)
# ---------------------------------------------------------------------------
_cache: InMemoryCache | RedisCache = InMemoryCache()


def get_cache() -> InMemoryCache | RedisCache:
    return _cache


def set_cache(cache: InMemoryCache | RedisCache) -> None:
    global _cache
    _cache = cache


# ---------------------------------------------------------------------------
# Per-route TTL configuration
# ---------------------------------------------------------------------------
# Map of path prefix -> TTL in seconds.  Zero or missing means no caching.
# Step 9 (Admin API) will allow runtime updates to this map.
ROUTE_CACHE_TTL: dict[str, int] = {
    "/users": 30,    # cache user listings for 30s
    "/orders": 15,   # cache order listings for 15s
}


# ---------------------------------------------------------------------------
# Cache middleware
# ---------------------------------------------------------------------------
class CacheMiddleware(BaseHTTPMiddleware):
    """
    Cache GET responses in Redis (or in-memory fallback).

    - Only caches GET requests
    - Respects ``Cache-Control: no-cache`` from the caller
    - Adds ``X-Cache: HIT`` or ``X-Cache: MISS`` to responses
    - Skips caching for gateway-internal paths (/gateway/*)
    - Per-route TTL from ROUTE_CACHE_TTL
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Only cache GET requests
        if request.method != "GET":
            return await call_next(request)

        path = request.url.path

        # Skip gateway-internal paths
        if path.startswith("/gateway/"):
            return await call_next(request)

        # Respect Cache-Control: no-cache
        if "no-cache" in request.headers.get("cache-control", ""):
            response = await call_next(request)
            response.headers["X-Cache"] = "BYPASS"
            return response

        # Find TTL for this route (longest prefix match)
        ttl = 0
        for prefix, t in sorted(ROUTE_CACHE_TTL.items(), key=lambda x: len(x[0]), reverse=True):
            if path == prefix or path.startswith(prefix + "/"):
                ttl = t
                break

        if ttl <= 0:
            response = await call_next(request)
            response.headers["X-Cache"] = "SKIP"
            return response

        # Build cache key
        cache_key = build_cache_key(request.method, path, request.url.query or "")

        # Try cache lookup
        cache = get_cache()
        try:
            cached = await cache.get(cache_key)
        except Exception:
            logger.exception("Cache read error")
            cached = None

        if cached is not None:
            logger.debug("Cache HIT: %s", cache_key)
            return Response(
                content=cached["body"].encode("utf-8") if isinstance(cached["body"], str) else cached["body"],
                status_code=cached["status_code"],
                headers={**cached.get("headers", {}), "X-Cache": "HIT"},
                media_type=cached.get("media_type"),
            )

        # Cache MISS — call backend
        response = await call_next(request)

        # Only cache successful responses (2xx)
        if 200 <= response.status_code < 300:
            body = b""
            async for chunk in response.body_iterator:
                if isinstance(chunk, str):
                    body += chunk.encode("utf-8")
                else:
                    body += chunk

            cache_data = {
                "body": body.decode("utf-8", errors="replace"),
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "media_type": response.media_type,
            }
            try:
                await cache.set(cache_key, cache_data, ttl)
            except Exception:
                logger.exception("Cache write error")

            response = Response(
                content=body,
                status_code=response.status_code,
                headers={**dict(response.headers), "X-Cache": "MISS"},
                media_type=response.media_type,
            )
        else:
            response.headers["X-Cache"] = "MISS"

        return response
