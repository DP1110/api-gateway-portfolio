"""
Rate Limiting Middleware (Step 4)
==================================
Token Bucket rate limiter implemented as Starlette ``BaseHTTPMiddleware``.

Algorithm choice -- why Token Bucket?
--------------------------------------
We evaluated three common rate-limiting algorithms before choosing:

1. **Fixed Window** (e.g. "100 requests per minute, counter resets on the
   minute boundary"):
   - Simple to implement: one counter per window.
   - *Problem*: suffers from the **boundary burst** issue.  A client can
     send 100 requests at 12:00:59 and another 100 at 12:01:00, achieving
     200 requests in two seconds while technically never exceeding the
     per-window limit.  This can overload backends with twice the expected
     peak rate.

2. **Sliding Window Log / Counter** (tracks timestamps of recent requests
   or uses weighted counters across overlapping windows):
   - Solves the boundary-burst problem by smoothing the window edge.
   - *Problem*: higher storage cost -- the log variant stores one timestamp
     per request (memory grows linearly with traffic), and the counter
     variant requires two counters plus a weighted calculation per check.
     For a gateway handling millions of requests across thousands of
     clients, this memory overhead adds up.

3. **Token Bucket** (tokens are added at a steady rate; each request
   consumes one token; requests are rejected when the bucket is empty):
   - Naturally **smooths traffic** while still allowing short bursts up to
     the bucket capacity -- exactly the behavior we want for an API gateway
     where clients may legitimately send small bursts.
   - **Constant memory per client**: only two values are stored (token
     count + last-refill timestamp), regardless of request volume.
   - **Cheap to compute**: one timestamp comparison and a subtraction per
     request -- no scanning of log entries.
   - Easy to reason about for API consumers: "you can burst up to N
     requests, then you're limited to M per second".
   - *Trade-off*: slightly more complex than fixed window, but the code
     below is still ~30 lines of core logic.

We therefore chose Token Bucket as the best balance of correctness,
performance, and developer experience for this API gateway.

Architecture
------------
- ``TokenBucketBackend`` (Protocol / ABC) defines the interface that both
  storage backends implement.
- ``InMemoryTokenBucket`` -- dict-based backend for development, testing,
  and as an automatic fallback when Redis is unavailable.
- ``RedisTokenBucket`` -- uses a Lua script executed atomically in Redis
  so that the refill + consume operation is race-free across multiple
  gateway instances.
- ``RateLimitMiddleware`` -- the Starlette middleware that ties everything
  together, resolves the client identity, picks the tier config, calls the
  backend, and sets the appropriate HTTP headers.

Per-tier configuration
-----------------------
Tier limits are defined as ``TierConfig`` dataclasses:

=====  ==================  ===========
Tier   Requests / minute   Bucket size
=====  ==================  ===========
free   10                  15
pro    100                 150
admin  1000                1500
=====  ==================  ===========

The bucket size is intentionally ~1.5x the per-minute rate to allow small
legitimate bursts without penalising well-behaved clients.

Headers
-------
Every response includes standard rate-limit headers:

- ``X-RateLimit-Limit``     -- bucket capacity for this client's tier
- ``X-RateLimit-Remaining`` -- tokens left right now
- ``X-RateLimit-Reset``     -- Unix epoch when the bucket will be fully
                               refilled (useful for client back-off logic)

When the bucket is empty a **429 Too Many Requests** response is returned
with a ``Retry-After`` header indicating how many seconds the client
should wait before retrying.
"""

from __future__ import annotations

import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

logger = logging.getLogger("gateway.rate_limit")


# ---------------------------------------------------------------------------
# Tier configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TierConfig:
    """Rate-limit parameters for a single client tier.

    Attributes:
        name:         Human-readable tier name.
        rate:         Tokens added per second (= requests_per_minute / 60).
        capacity:     Maximum tokens the bucket can hold (burst size).
    """
    name: str
    rate: float       # tokens / second
    capacity: float   # max tokens (bucket size)


# Pre-built tier configs.
# rate = requests_per_minute / 60  (converted to per-second for the
# token bucket math so we can work with fractional-second precision).
TIER_CONFIGS: dict[str, TierConfig] = {
    "free":  TierConfig(name="free",  rate=10   / 60, capacity=15),
    "pro":   TierConfig(name="pro",   rate=100  / 60, capacity=150),
    "admin": TierConfig(name="admin", rate=1000 / 60, capacity=1500),
}

# Fallback for unknown tiers -- apply the most restrictive limits.
DEFAULT_TIER = TIER_CONFIGS["free"]


# ---------------------------------------------------------------------------
# Token Bucket result
# ---------------------------------------------------------------------------
@dataclass
class ConsumeResult:
    """Outcome of a single consume-one-token operation.

    Attributes:
        allowed:   True if a token was available and consumed.
        remaining: Tokens left in the bucket *after* this operation.
        reset_at:  Unix timestamp when the bucket will be full again.
        limit:     The bucket capacity (for the X-RateLimit-Limit header).
    """
    allowed: bool
    remaining: float
    reset_at: float
    limit: float


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------
class TokenBucketBackend(ABC):
    """Protocol that both the in-memory and Redis token-bucket backends
    must implement.

    The single method ``consume`` is called once per request.  It must
    atomically:
      1. Refill the bucket based on elapsed time since the last refill.
      2. Attempt to remove one token.
      3. Return a ``ConsumeResult``.
    """

    @abstractmethod
    def consume(self, key: str, tier: TierConfig, now: float | None = None) -> ConsumeResult:
        """Try to consume one token from the bucket identified by *key*.

        Parameters:
            key:  Unique identifier for this bucket (e.g. ``"rl:client-pro-1"``).
            tier: The ``TierConfig`` controlling rate and capacity.
            now:  Current Unix timestamp.  Accepting this as a parameter
                  (instead of always calling ``time.time()`` internally)
                  makes the class deterministically testable.

        Returns:
            A ``ConsumeResult`` with the outcome.
        """
        ...


# ---------------------------------------------------------------------------
# In-memory backend (development / fallback / testing)
# ---------------------------------------------------------------------------
class InMemoryTokenBucket(TokenBucketBackend):
    """Pure-Python token bucket backed by a plain ``dict``.

    Thread-safety note: CPython's GIL makes single-operation dict access
    atomic enough for a single-process dev server.  In production we use
    the Redis backend for cross-process consistency.

    This class is also the one used in unit tests -- no Redis required.
    """

    def __init__(self) -> None:
        # _buckets maps key -> [tokens: float, last_refill: float]
        self._buckets: dict[str, list[float]] = {}

    def consume(self, key: str, tier: TierConfig, now: float | None = None) -> ConsumeResult:
        """Refill then consume one token.

        The refill logic works as follows:
          1. Compute elapsed time since the last refill.
          2. Add ``elapsed * tier.rate`` tokens, capped at ``tier.capacity``.
          3. If tokens >= 1, consume one and allow the request.
          4. Otherwise, deny the request and compute Retry-After.
        """
        if now is None:
            now = time.time()

        # First request for this key -- initialise a full bucket.
        if key not in self._buckets:
            # Start with a full bucket so new clients aren't immediately
            # throttled.  This is standard token-bucket behaviour.
            self._buckets[key] = [tier.capacity, now]

        bucket = self._buckets[key]
        tokens, last_refill = bucket[0], bucket[1]

        # -- Refill --
        elapsed = now - last_refill
        if elapsed > 0:
            tokens = min(tier.capacity, tokens + elapsed * tier.rate)
            last_refill = now

        # -- Consume --
        if tokens >= 1.0:
            tokens -= 1.0
            allowed = True
        else:
            allowed = False

        # Persist updated state back to the dict.
        bucket[0] = tokens
        bucket[1] = last_refill

        # -- Compute reset time --
        # "Reset" = when the bucket will be full again.
        # tokens_needed = capacity - current_tokens
        # time_to_full  = tokens_needed / rate
        tokens_needed = tier.capacity - tokens
        if tokens_needed <= 0 or tier.rate <= 0:
            reset_at = now
        else:
            reset_at = now + (tokens_needed / tier.rate)

        return ConsumeResult(
            allowed=allowed,
            remaining=max(0.0, tokens),
            reset_at=reset_at,
            limit=tier.capacity,
        )


# ---------------------------------------------------------------------------
# Redis backend (production)
# ---------------------------------------------------------------------------
class RedisTokenBucket(TokenBucketBackend):
    """Token bucket backed by Redis, using a Lua script for atomicity.

    Why Lua?  The refill-and-consume operation involves a read-modify-write
    cycle on two hash fields (tokens, last_refill).  Without Lua, we'd need
    an WATCH/MULTI/EXEC optimistic-lock loop which is more complex and
    suffers from retries under contention.  A single ``EVALSHA`` call
    executes atomically inside the Redis event loop -- no races, no retries.

    The Lua script is defined as a class constant and loaded once via
    ``SCRIPT LOAD`` on first use (then called via ``EVALSHA`` for speed).
    """

    # Lua script that runs atomically inside Redis.
    # KEYS[1] = bucket hash key
    # ARGV[1] = rate (tokens/sec), ARGV[2] = capacity, ARGV[3] = now
    #
    # Hash fields: "t" = tokens, "lr" = last_refill
    _LUA_SCRIPT = """
    local key      = KEYS[1]
    local rate     = tonumber(ARGV[1])
    local capacity = tonumber(ARGV[2])
    local now      = tonumber(ARGV[3])

    local data = redis.call('HMGET', key, 't', 'lr')
    local tokens      = tonumber(data[1])
    local last_refill = tonumber(data[2])

    -- First request: initialise full bucket
    if tokens == nil then
        tokens      = capacity
        last_refill = now
    end

    -- Refill
    local elapsed = now - last_refill
    if elapsed > 0 then
        tokens      = math.min(capacity, tokens + elapsed * rate)
        last_refill = now
    end

    -- Consume
    local allowed = 0
    if tokens >= 1 then
        tokens  = tokens - 1
        allowed = 1
    end

    -- Persist
    redis.call('HMSET', key, 't', tostring(tokens), 'lr', tostring(last_refill))
    -- Auto-expire bucket after 2x the time to fill from empty, so we
    -- don't leak memory for clients that disappear.  Minimum 120s.
    local ttl = math.max(120, math.ceil(capacity / rate * 2))
    redis.call('EXPIRE', key, ttl)

    -- Return: allowed, remaining tokens, bucket capacity
    return {allowed, tostring(tokens), tostring(capacity)}
    """

    def __init__(self, redis_client: Any) -> None:
        """
        Parameters:
            redis_client: A ``redis.Redis`` (sync) or compatible instance.
        """
        self._redis = redis_client
        self._script_sha: str | None = None

    def _ensure_script(self) -> str:
        """Load the Lua script into Redis if not already loaded."""
        if self._script_sha is None:
            self._script_sha = self._redis.script_load(self._LUA_SCRIPT)
        return self._script_sha

    def consume(self, key: str, tier: TierConfig, now: float | None = None) -> ConsumeResult:
        if now is None:
            now = time.time()

        sha = self._ensure_script()

        # EVALSHA <sha> 1 <key> <rate> <capacity> <now>
        result = self._redis.evalsha(
            sha, 1, key, str(tier.rate), str(tier.capacity), str(now)
        )

        allowed = bool(int(result[0]))
        remaining = float(result[1])
        capacity = float(result[2])

        # Compute reset_at locally (avoids extra Lua complexity).
        tokens_needed = capacity - remaining
        if tokens_needed <= 0 or tier.rate <= 0:
            reset_at = now
        else:
            reset_at = now + (tokens_needed / tier.rate)

        return ConsumeResult(
            allowed=allowed,
            remaining=max(0.0, remaining),
            reset_at=reset_at,
            limit=capacity,
        )


# ---------------------------------------------------------------------------
# Factory: choose the best available backend
# ---------------------------------------------------------------------------
def _create_backend() -> TokenBucketBackend:
    """Attempt to connect to Redis; fall back to in-memory if unavailable.

    This is called once at module import time.  If the Redis connection
    fails (server down, wrong URL, network issue), we log a warning and
    silently fall back.  This means the gateway can always start, even
    without Redis -- at the cost of per-process (non-shared) rate limits.
    """
    try:
        import redis as redis_lib  # noqa: F811 -- optional dependency
        from gateway.config import settings

        client = redis_lib.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,  # fail fast
        )
        client.ping()  # verify connectivity
        logger.info("Rate limiter: using Redis backend (%s)", settings.redis_url)
        return RedisTokenBucket(redis_client=client)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Rate limiter: Redis unavailable (%s) -- falling back to in-memory backend. "
            "Rate limits will NOT be shared across gateway instances.",
            exc,
        )
        return InMemoryTokenBucket()


# Module-level singleton backend.
# Using a module-level instance means the middleware shares one backend
# (and one set of buckets) across all requests in this process.
_backend: TokenBucketBackend = _create_backend()


# ---------------------------------------------------------------------------
# Paths exempt from rate limiting
# ---------------------------------------------------------------------------
_RATE_LIMIT_EXEMPT_PREFIXES = (
    "/gateway/",
)


def _is_exempt(path: str) -> bool:
    """Return True if the path should bypass rate limiting.

    Gateway-internal endpoints (health checks, docs, admin UI) are not
    subject to rate limits because:
      - They are operated by the gateway team, not external clients.
      - Throttling them could mask operational issues during incidents.
    """
    return any(path.startswith(prefix) for prefix in _RATE_LIMIT_EXEMPT_PREFIXES)


# ---------------------------------------------------------------------------
# Helper: extract client key and tier from the request
# ---------------------------------------------------------------------------
def _resolve_client(request: Request) -> tuple[str, TierConfig]:
    """Determine the rate-limit bucket key and tier for this request.

    Resolution order:
      1. If the auth middleware already authenticated the caller and set
         ``request.state.client`` (a ``ClientIdentity``), use the
         ``client_id`` as the bucket key and the ``tier`` to select the
         config.
      2. Otherwise fall back to the client's IP address with ``free``
         tier limits.  This handles unauthenticated traffic (e.g. if auth
         is disabled or the request somehow bypassed auth).
    """
    client_identity = getattr(request.state, "client", None)

    if client_identity is not None:
        # Authenticated client -- use identity from auth middleware.
        client_id: str = client_identity.client_id
        tier_name: str = getattr(client_identity, "tier", "free")
        tier = TIER_CONFIGS.get(tier_name, DEFAULT_TIER)
        key = f"rl:{client_id}"
    else:
        # Unauthenticated -- fall back to IP with most restrictive limits.
        # request.client can be None in test environments; guard against it.
        ip = request.client.host if request.client else "unknown"
        tier = DEFAULT_TIER
        key = f"rl:ip:{ip}"

    return key, tier


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
class RateLimitMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that enforces per-client token-bucket rate limits.

    This middleware should be added **after** ``AuthMiddleware`` in the
    middleware stack so that ``request.state.client`` is already populated
    when we run.  (Starlette processes middleware in LIFO order, so
    ``app.add_middleware(RateLimitMiddleware)`` should appear *before*
    ``app.add_middleware(AuthMiddleware)`` in ``main.py``.)

    Usage in ``main.py``::

        app.add_middleware(RateLimitMiddleware)      # evaluated 2nd
        app.add_middleware(AuthMiddleware)            # evaluated 1st
    """

    def __init__(self, app: Any, backend: TokenBucketBackend | None = None) -> None:
        super().__init__(app)
        # Allow injecting a custom backend (useful for testing).
        self._backend = backend if backend is not None else _backend

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # --- Exempt internal paths ---
        if _is_exempt(request.url.path):
            return await call_next(request)

        key, tier = _resolve_client(request)

        # --- Consume a token ---
        result = self._backend.consume(key, tier)

        if not result.allowed:
            # Calculate Retry-After: time until at least one token is
            # available.  With rate R tokens/sec, the wait for one token
            # is 1/R seconds.  We ceil() to give a conservative integer.
            retry_after = math.ceil(1.0 / tier.rate) if tier.rate > 0 else 60
            logger.info(
                "Rate limit exceeded for %s (tier=%s, remaining=%.1f)",
                key, tier.name, result.remaining,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Too Many Requests",
                    "detail": (
                        f"Rate limit exceeded. "
                        f"Try again in {retry_after} seconds."
                    ),
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(int(result.limit)),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(result.reset_at)),
                },
            )

        # --- Request allowed: forward and tag the response ---
        response = await call_next(request)

        response.headers["X-RateLimit-Limit"] = str(int(result.limit))
        response.headers["X-RateLimit-Remaining"] = str(int(result.remaining))
        response.headers["X-RateLimit-Reset"] = str(int(result.reset_at))

        return response
