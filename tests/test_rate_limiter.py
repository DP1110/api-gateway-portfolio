"""
Tests for the Token Bucket Rate Limiter (Step 4)
==================================================
All tests use the ``InMemoryTokenBucket`` backend -- no Redis required.

We inject explicit ``now`` timestamps into every ``consume()`` call so the
tests are fully deterministic and never depend on wall-clock time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import pytest

from gateway.middleware.rate_limit import (
    ConsumeResult,
    InMemoryTokenBucket,
    RateLimitMiddleware,
    TierConfig,
    TIER_CONFIGS,
    DEFAULT_TIER,
    _resolve_client,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
FREE_TIER  = TIER_CONFIGS["free"]   # 10 req/min, bucket=15
PRO_TIER   = TIER_CONFIGS["pro"]    # 100 req/min, bucket=150
ADMIN_TIER = TIER_CONFIGS["admin"]  # 1000 req/min, bucket=1500

# Small custom tier for focused arithmetic tests.
# 1 token/sec, bucket size 3 -- easy to reason about.
TINY_TIER = TierConfig(name="tiny", rate=1.0, capacity=3.0)


# ---------------------------------------------------------------------------
# 1. Token bucket initialisation
# ---------------------------------------------------------------------------
class TestBucketInitialisation:
    """A new bucket should start full (tokens == capacity)."""

    def test_new_bucket_starts_full(self) -> None:
        bucket = InMemoryTokenBucket()
        result = bucket.consume("user:1", TINY_TIER, now=1000.0)
        # Capacity is 3; after consuming 1, remaining should be 2.
        assert result.allowed is True
        assert result.remaining == pytest.approx(2.0)

    def test_new_bucket_limit_matches_capacity(self) -> None:
        bucket = InMemoryTokenBucket()
        result = bucket.consume("user:1", FREE_TIER, now=1000.0)
        assert result.limit == FREE_TIER.capacity


# ---------------------------------------------------------------------------
# 2. Token consumption -- requests allowed when tokens available
# ---------------------------------------------------------------------------
class TestTokenConsumption:
    """Requests should be allowed as long as there are tokens."""

    def test_consume_decrements_tokens(self) -> None:
        bucket = InMemoryTokenBucket()
        # Bucket starts with capacity=3 tokens.
        r1 = bucket.consume("k", TINY_TIER, now=100.0)
        assert r1.allowed is True
        assert r1.remaining == pytest.approx(2.0)

        r2 = bucket.consume("k", TINY_TIER, now=100.0)
        assert r2.allowed is True
        assert r2.remaining == pytest.approx(1.0)

        r3 = bucket.consume("k", TINY_TIER, now=100.0)
        assert r3.allowed is True
        assert r3.remaining == pytest.approx(0.0)

    def test_multiple_clients_independent(self) -> None:
        """Buckets for different keys must not interfere."""
        bucket = InMemoryTokenBucket()
        r_a = bucket.consume("client:A", TINY_TIER, now=100.0)
        r_b = bucket.consume("client:B", TINY_TIER, now=100.0)

        # Both should still have capacity - 1 tokens.
        assert r_a.remaining == pytest.approx(2.0)
        assert r_b.remaining == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 3. Token exhaustion -- requests denied when bucket is empty
# ---------------------------------------------------------------------------
class TestTokenExhaustion:
    """When the bucket is empty, requests must be denied."""

    def test_denied_when_empty(self) -> None:
        bucket = InMemoryTokenBucket()
        # Drain all 3 tokens at the same instant.
        for _ in range(3):
            bucket.consume("k", TINY_TIER, now=100.0)

        result = bucket.consume("k", TINY_TIER, now=100.0)
        assert result.allowed is False
        assert result.remaining == pytest.approx(0.0)

    def test_denied_repeatedly_when_empty(self) -> None:
        """Multiple requests at the same timestamp after exhaustion."""
        bucket = InMemoryTokenBucket()
        # Drain the bucket.
        for _ in range(3):
            bucket.consume("k", TINY_TIER, now=100.0)

        for _ in range(5):
            result = bucket.consume("k", TINY_TIER, now=100.0)
            assert result.allowed is False

    def test_free_tier_exhaustion(self) -> None:
        """Free tier (capacity=15) should deny the 16th request."""
        bucket = InMemoryTokenBucket()
        for i in range(15):
            r = bucket.consume("free-user", FREE_TIER, now=100.0)
            assert r.allowed is True, f"Request {i+1} should be allowed"

        denied = bucket.consume("free-user", FREE_TIER, now=100.0)
        assert denied.allowed is False


# ---------------------------------------------------------------------------
# 4. Refill logic
# ---------------------------------------------------------------------------
class TestRefillLogic:
    """Tokens should be added proportional to elapsed time * rate."""

    def test_partial_refill(self) -> None:
        bucket = InMemoryTokenBucket()
        # Drain all 3 tokens at t=100.
        for _ in range(3):
            bucket.consume("k", TINY_TIER, now=100.0)

        # 1.5 seconds later at rate=1 tok/sec, we should have 1.5 tokens.
        # Consuming one leaves 0.5.
        result = bucket.consume("k", TINY_TIER, now=101.5)
        assert result.allowed is True
        assert result.remaining == pytest.approx(0.5)

    def test_full_refill_capped_at_capacity(self) -> None:
        """Tokens should never exceed the bucket capacity."""
        bucket = InMemoryTokenBucket()
        # Drain all tokens at t=100.
        for _ in range(3):
            bucket.consume("k", TINY_TIER, now=100.0)

        # Wait 1000 seconds -- way more than enough to refill.
        # Should be capped at capacity (3), then one consumed -> 2.
        result = bucket.consume("k", TINY_TIER, now=1100.0)
        assert result.allowed is True
        assert result.remaining == pytest.approx(2.0)

    def test_exact_one_token_refill(self) -> None:
        """After draining, waiting exactly 1/rate seconds should yield
        exactly one token."""
        bucket = InMemoryTokenBucket()
        # Drain at t=0.
        for _ in range(3):
            bucket.consume("k", TINY_TIER, now=0.0)

        # rate = 1 tok/sec => wait 1 second for 1 token.
        result = bucket.consume("k", TINY_TIER, now=1.0)
        assert result.allowed is True
        assert result.remaining == pytest.approx(0.0)

    def test_refill_does_not_exceed_capacity_with_partial_drain(self) -> None:
        """If the bucket is partially drained and enough time passes,
        tokens should cap at capacity."""
        bucket = InMemoryTokenBucket()
        # Consume 1 token at t=0 => 2 remaining.
        bucket.consume("k", TINY_TIER, now=0.0)

        # Wait 100 seconds => would add 100 tokens, but cap at 3.
        # Then consume 1 => 2.
        result = bucket.consume("k", TINY_TIER, now=100.0)
        assert result.allowed is True
        assert result.remaining == pytest.approx(2.0)

    def test_no_refill_at_same_timestamp(self) -> None:
        """Multiple consumes at the same instant should not refill."""
        bucket = InMemoryTokenBucket()
        results = [bucket.consume("k", TINY_TIER, now=100.0) for _ in range(3)]
        assert results[0].remaining == pytest.approx(2.0)
        assert results[1].remaining == pytest.approx(1.0)
        assert results[2].remaining == pytest.approx(0.0)

    def test_free_tier_refill_rate(self) -> None:
        """Free tier: 10 req/min = 1/6 tok/sec.
        After draining 15 tokens, 60 seconds should add 10 tokens."""
        bucket = InMemoryTokenBucket()
        # Drain all 15 tokens at t=0.
        for _ in range(15):
            bucket.consume("free-user", FREE_TIER, now=0.0)

        # At t=60, refill = 60 * (10/60) = 10 tokens. Consume 1 => 9.
        result = bucket.consume("free-user", FREE_TIER, now=60.0)
        assert result.allowed is True
        assert result.remaining == pytest.approx(9.0)


# ---------------------------------------------------------------------------
# 5. Different tier limits
# ---------------------------------------------------------------------------
class TestTierLimits:
    """Each tier must enforce its own rate and capacity."""

    def test_free_tier_capacity(self) -> None:
        assert FREE_TIER.capacity == 15
        assert FREE_TIER.rate == pytest.approx(10 / 60)

    def test_pro_tier_capacity(self) -> None:
        assert PRO_TIER.capacity == 150
        assert PRO_TIER.rate == pytest.approx(100 / 60)

    def test_admin_tier_capacity(self) -> None:
        assert ADMIN_TIER.capacity == 1500
        assert ADMIN_TIER.rate == pytest.approx(1000 / 60)

    def test_pro_allows_more_burst_than_free(self) -> None:
        bucket = InMemoryTokenBucket()
        # Free tier: 16th request denied at same timestamp.
        for _ in range(15):
            bucket.consume("free-user", FREE_TIER, now=0.0)
        assert bucket.consume("free-user", FREE_TIER, now=0.0).allowed is False

        # Pro tier: 16th request still allowed (capacity=150).
        for _ in range(15):
            bucket.consume("pro-user", PRO_TIER, now=0.0)
        assert bucket.consume("pro-user", PRO_TIER, now=0.0).allowed is True

    def test_admin_allows_massive_burst(self) -> None:
        bucket = InMemoryTokenBucket()
        for _ in range(1500):
            r = bucket.consume("admin-user", ADMIN_TIER, now=0.0)
            assert r.allowed is True
        # 1501st request should be denied.
        assert bucket.consume("admin-user", ADMIN_TIER, now=0.0).allowed is False

    def test_default_tier_is_free(self) -> None:
        """Unknown tiers should fall back to free-tier limits."""
        assert DEFAULT_TIER.name == "free"
        assert DEFAULT_TIER.capacity == FREE_TIER.capacity


# ---------------------------------------------------------------------------
# 6. Retry-After calculation
# ---------------------------------------------------------------------------
class TestRetryAfter:
    """When denied, the middleware returns a Retry-After header.

    The middleware computes Retry-After as ceil(1 / rate), i.e. the time
    until at least one token becomes available.
    """

    def test_retry_after_tiny_tier(self) -> None:
        """Tiny tier: rate=1 tok/sec => Retry-After = ceil(1/1) = 1 second."""
        tier = TINY_TIER
        retry_after = math.ceil(1.0 / tier.rate)
        assert retry_after == 1

    def test_retry_after_free_tier(self) -> None:
        """Free tier: rate = 10/60 tok/sec => 1/rate = 6 => Retry-After = 6."""
        retry_after = math.ceil(1.0 / FREE_TIER.rate)
        assert retry_after == 6

    def test_retry_after_pro_tier(self) -> None:
        """Pro tier: rate = 100/60 tok/sec => 1/rate = 0.6 => ceil = 1."""
        retry_after = math.ceil(1.0 / PRO_TIER.rate)
        assert retry_after == 1

    def test_retry_after_admin_tier(self) -> None:
        """Admin tier: rate = 1000/60 tok/sec => 1/rate ~ 0.06 => ceil = 1."""
        retry_after = math.ceil(1.0 / ADMIN_TIER.rate)
        assert retry_after == 1


# ---------------------------------------------------------------------------
# 7. Reset-at (X-RateLimit-Reset) calculation
# ---------------------------------------------------------------------------
class TestResetAt:
    """The reset_at timestamp should reflect when the bucket is full."""

    def test_reset_at_full_bucket(self) -> None:
        """A full bucket has reset_at == now (already full)."""
        bucket = InMemoryTokenBucket()
        # First consume: bucket goes from 3 -> 2.
        # tokens_needed = 3 - 2 = 1, time = 1/rate = 1s.
        result = bucket.consume("k", TINY_TIER, now=100.0)
        assert result.reset_at == pytest.approx(101.0)

    def test_reset_at_empty_bucket(self) -> None:
        """An empty bucket needs capacity/rate seconds to refill."""
        bucket = InMemoryTokenBucket()
        for _ in range(3):
            bucket.consume("k", TINY_TIER, now=100.0)

        result = bucket.consume("k", TINY_TIER, now=100.0)
        # tokens = 0, tokens_needed = 3, time = 3/1 = 3s.
        assert result.reset_at == pytest.approx(103.0)


# ---------------------------------------------------------------------------
# 8. ConsumeResult data integrity
# ---------------------------------------------------------------------------
class TestConsumeResult:
    """Ensure ConsumeResult fields are always consistent."""

    def test_remaining_never_negative(self) -> None:
        bucket = InMemoryTokenBucket()
        # Drain bucket then try more requests.
        for _ in range(5):
            result = bucket.consume("k", TINY_TIER, now=0.0)
        assert result.remaining >= 0.0

    def test_limit_always_matches_capacity(self) -> None:
        bucket = InMemoryTokenBucket()
        for tier in [FREE_TIER, PRO_TIER, ADMIN_TIER]:
            result = bucket.consume(f"k:{tier.name}", tier, now=0.0)
            assert result.limit == tier.capacity


# ---------------------------------------------------------------------------
# 9. Integration-style test: simulate a traffic burst + cooldown
# ---------------------------------------------------------------------------
class TestBurstAndCooldown:
    """Simulate a realistic scenario: burst of requests, then wait."""

    def test_burst_then_cooldown_then_allowed(self) -> None:
        """Drain the bucket, wait for refill, verify access restored."""
        bucket = InMemoryTokenBucket()

        # Phase 1: burst at t=0 -- drain all 3 tokens.
        for _ in range(3):
            assert bucket.consume("k", TINY_TIER, now=0.0).allowed is True

        # Phase 2: denied at t=0.
        assert bucket.consume("k", TINY_TIER, now=0.0).allowed is False

        # Phase 3: wait 2 seconds => refill 2 tokens => first request allowed.
        result = bucket.consume("k", TINY_TIER, now=2.0)
        assert result.allowed is True
        assert result.remaining == pytest.approx(1.0)

    def test_steady_state_at_exact_rate(self) -> None:
        """If we send exactly one request per 1/rate seconds, we should
        never be denied (steady state)."""
        bucket = InMemoryTokenBucket()
        interval = 1.0 / TINY_TIER.rate  # 1 second for TINY_TIER

        # Send 20 requests spaced exactly at the refill interval.
        for i in range(20):
            t = float(i) * interval
            result = bucket.consume("k", TINY_TIER, now=t)
            assert result.allowed is True, f"Request at t={t} should be allowed"


# ---------------------------------------------------------------------------
# 10. Middleware-level exempt paths
# ---------------------------------------------------------------------------
class TestExemptPaths:
    """Gateway-internal paths must bypass rate limiting."""

    def test_gateway_health_is_exempt(self) -> None:
        from gateway.middleware.rate_limit import _is_exempt
        assert _is_exempt("/gateway/health") is True

    def test_gateway_docs_is_exempt(self) -> None:
        from gateway.middleware.rate_limit import _is_exempt
        assert _is_exempt("/gateway/docs") is True

    def test_api_path_is_not_exempt(self) -> None:
        from gateway.middleware.rate_limit import _is_exempt
        assert _is_exempt("/api/users") is False

    def test_root_is_not_exempt(self) -> None:
        from gateway.middleware.rate_limit import _is_exempt
        assert _is_exempt("/") is False


# ---------------------------------------------------------------------------
# 11. Client resolution logic
# ---------------------------------------------------------------------------
class TestClientResolution:
    """The middleware should resolve client identity from request.state."""

    def test_resolve_with_client_identity(self) -> None:
        """When request.state.client exists, use its client_id and tier."""

        @dataclass
        class FakeClient:
            host: str = "10.0.0.1"

        @dataclass
        class FakeIdentity:
            client_id: str = "user-42"
            tier: str = "pro"

        class FakeState:
            client: Optional[FakeIdentity] = FakeIdentity()

        class FakeRequest:
            state = FakeState()
            client = FakeClient()

        key, tier = _resolve_client(FakeRequest())  # type: ignore[arg-type]
        assert key == "rl:user-42"
        assert tier.name == "pro"
        assert tier.capacity == PRO_TIER.capacity

    def test_resolve_fallback_to_ip(self) -> None:
        """When request.state has no 'client', fall back to IP + free tier."""

        @dataclass
        class FakeClient:
            host: str = "192.168.1.100"

        class FakeState:
            pass  # no 'client' attribute

        class FakeRequest:
            state = FakeState()
            client = FakeClient()

        key, tier = _resolve_client(FakeRequest())  # type: ignore[arg-type]
        assert key == "rl:ip:192.168.1.100"
        assert tier.name == "free"

    def test_resolve_unknown_tier_defaults_to_free(self) -> None:
        """An unrecognised tier string should fall back to free limits."""

        @dataclass
        class FakeIdentity:
            client_id: str = "user-99"
            tier: str = "enterprise"  # not in TIER_CONFIGS

        class FakeState:
            client: Optional[FakeIdentity] = FakeIdentity()

        @dataclass
        class FakeClient:
            host: str = "10.0.0.1"

        class FakeRequest:
            state = FakeState()
            client = FakeClient()

        key, tier = _resolve_client(FakeRequest())  # type: ignore[arg-type]
        assert key == "rl:user-99"
        assert tier.name == "free"
