"""
Unit tests for the Circuit Breaker module.

Test strategy
-------------
* Each state transition in the FSM is exercised individually.
* Time-dependent transitions (Open -> Half-Open) are tested by directly
  manipulating the internal ``_opened_at`` timestamp rather than sleeping.
  This keeps the suite deterministic and sub-second.
* Where we need to verify that ``time.time()`` is called at the right
  moment (e.g. when recording a failure re-trips Open), we patch
  ``time.time`` with ``unittest.mock.patch``.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from gateway.circuit_breaker.cb import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitState,
)


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture()
def cb() -> CircuitBreaker:
    """A breaker with low threshold (3) and short timeout (10 s)
    for easy testing."""
    return CircuitBreaker(
        name="test-service",
        failure_threshold=3,
        recovery_timeout=10.0,
        half_open_max_calls=1,
    )


# ======================================================================
# 1. Initial state
# ======================================================================

class TestInitialState:
    """The breaker must start in the Closed state with zero failures."""

    def test_starts_closed(self, cb: CircuitBreaker) -> None:
        assert cb.state == "closed"

    def test_starts_with_zero_failures(self, cb: CircuitBreaker) -> None:
        assert cb.failure_count == 0

    def test_can_execute_when_closed(self, cb: CircuitBreaker) -> None:
        assert cb.can_execute() is True


# ======================================================================
# 2. Closed state behaviour
# ======================================================================

class TestClosedState:
    """While closed, requests are allowed and the failure counter
    increments on errors but resets on success."""

    def test_stays_closed_below_threshold(self, cb: CircuitBreaker) -> None:
        """Fewer failures than the threshold must NOT trip the breaker."""
        for _ in range(cb.failure_threshold - 1):
            cb.record_failure()
        assert cb.state == "closed"
        assert cb.can_execute() is True

    def test_success_resets_failure_counter(self, cb: CircuitBreaker) -> None:
        """A single success in between failures resets the consecutive
        counter, so the breaker stays closed even if total errors exceed
        the threshold."""
        # Record (threshold - 1) failures, then one success, repeat.
        for _ in range(3):
            for _ in range(cb.failure_threshold - 1):
                cb.record_failure()
            cb.record_success()
        # Total failures = 3 * (threshold-1) = 6, but never consecutive.
        assert cb.state == "closed"
        assert cb.failure_count == 0


# ======================================================================
# 3. Closed -> Open transition
# ======================================================================

class TestTripping:
    """Reaching the consecutive-failure threshold trips the breaker."""

    def test_opens_at_threshold(self, cb: CircuitBreaker) -> None:
        for _ in range(cb.failure_threshold):
            cb.record_failure()
        assert cb.state == "open"

    def test_rejects_when_open(self, cb: CircuitBreaker) -> None:
        for _ in range(cb.failure_threshold):
            cb.record_failure()
        assert cb.can_execute() is False


# ======================================================================
# 4. Open -> Half-Open transition
# ======================================================================

class TestRecoveryTimeout:
    """After spending ``recovery_timeout`` seconds in Open, the breaker
    must transition to Half-Open on the next ``state`` or
    ``can_execute()`` check."""

    def test_transitions_to_half_open_via_state(
        self, cb: CircuitBreaker
    ) -> None:
        # Trip the breaker.
        for _ in range(cb.failure_threshold):
            cb.record_failure()
        assert cb.state == "open"

        # Simulate time passing by backdating ``_opened_at``.
        cb._opened_at = time.time() - cb.recovery_timeout - 1
        assert cb.state == "half_open"

    def test_transitions_to_half_open_via_can_execute(
        self, cb: CircuitBreaker
    ) -> None:
        for _ in range(cb.failure_threshold):
            cb.record_failure()
        cb._opened_at = time.time() - cb.recovery_timeout - 1
        # ``can_execute`` should lazily promote to Half-Open and allow
        # exactly one probe.
        assert cb.can_execute() is True
        assert cb.state == "half_open"

    def test_stays_open_before_timeout(self, cb: CircuitBreaker) -> None:
        for _ in range(cb.failure_threshold):
            cb.record_failure()
        # Do NOT backdate -- timeout has not elapsed.
        assert cb.state == "open"
        assert cb.can_execute() is False


# ======================================================================
# 5. Half-Open behaviour
# ======================================================================

class TestHalfOpen:
    """In Half-Open only ``half_open_max_calls`` (1) probe is allowed.
    Success closes the breaker; failure re-opens it."""

    def _make_half_open(self, cb: CircuitBreaker) -> None:
        """Helper: trip then fast-forward past recovery timeout."""
        for _ in range(cb.failure_threshold):
            cb.record_failure()
        cb._opened_at = time.time() - cb.recovery_timeout - 1

    def test_allows_one_probe(self, cb: CircuitBreaker) -> None:
        self._make_half_open(cb)
        assert cb.can_execute() is True  # first probe
        assert cb.can_execute() is False  # second must be rejected

    def test_success_closes(self, cb: CircuitBreaker) -> None:
        self._make_half_open(cb)
        cb.can_execute()  # admit the probe
        cb.record_success()
        assert cb.state == "closed"
        assert cb.failure_count == 0
        assert cb.can_execute() is True

    def test_failure_reopens(self, cb: CircuitBreaker) -> None:
        self._make_half_open(cb)
        cb.can_execute()  # admit the probe
        cb.record_failure()
        assert cb.state == "open"

    def test_failure_resets_recovery_timer(
        self, cb: CircuitBreaker
    ) -> None:
        """When a half-open probe fails, the recovery clock must restart
        from *now*, not from the original trip time."""
        self._make_half_open(cb)
        cb.can_execute()

        # Patch time.time so we know the exact timestamp recorded.
        fake_now = 9999999.0
        with patch("gateway.circuit_breaker.cb.time") as mock_time:
            mock_time.time.return_value = fake_now
            cb.record_failure()
            
            assert cb.state == "open"
            assert cb._opened_at == fake_now


# ======================================================================
# 6. Manual reset
# ======================================================================

class TestManualReset:
    """``reset()`` forces the breaker back to Closed regardless of
    current state."""

    def test_reset_from_open(self, cb: CircuitBreaker) -> None:
        for _ in range(cb.failure_threshold):
            cb.record_failure()
        assert cb.state == "open"
        cb.reset()
        assert cb.state == "closed"
        assert cb.failure_count == 0
        assert cb.can_execute() is True

    def test_reset_from_half_open(self, cb: CircuitBreaker) -> None:
        for _ in range(cb.failure_threshold):
            cb.record_failure()
        cb._opened_at = time.time() - cb.recovery_timeout - 1
        assert cb.state == "half_open"
        cb.reset()
        assert cb.state == "closed"


# ======================================================================
# 7. repr
# ======================================================================

class TestRepr:
    def test_repr_includes_name_and_state(self, cb: CircuitBreaker) -> None:
        r = repr(cb)
        assert "test-service" in r
        assert "closed" in r


# ======================================================================
# 8. CircuitBreakerRegistry
# ======================================================================

class TestRegistry:
    """The registry lazily creates per-service breakers and returns
    the same instance on subsequent calls."""

    def test_creates_new_breaker(self) -> None:
        reg = CircuitBreakerRegistry()
        cb = reg.get_or_create("svc-a")
        assert isinstance(cb, CircuitBreaker)
        assert cb.name == "svc-a"

    def test_returns_same_instance(self) -> None:
        reg = CircuitBreakerRegistry()
        cb1 = reg.get_or_create("svc-b")
        cb2 = reg.get_or_create("svc-b")
        assert cb1 is cb2

    def test_separate_services_get_separate_breakers(self) -> None:
        reg = CircuitBreakerRegistry()
        cb_a = reg.get_or_create("svc-a")
        cb_b = reg.get_or_create("svc-b")
        assert cb_a is not cb_b
        assert cb_a.name == "svc-a"
        assert cb_b.name == "svc-b"

    def test_get_all_returns_snapshot(self) -> None:
        reg = CircuitBreakerRegistry()
        reg.get_or_create("svc-x")
        reg.get_or_create("svc-y")
        all_cbs = reg.get_all()
        assert set(all_cbs.keys()) == {"svc-x", "svc-y"}
        # Mutating the snapshot must not affect the registry.
        all_cbs.pop("svc-x")
        assert "svc-x" in reg.get_all()

    def test_custom_defaults(self) -> None:
        reg = CircuitBreakerRegistry(
            default_failure_threshold=10,
            default_recovery_timeout=60.0,
            default_half_open_max_calls=2,
        )
        cb = reg.get_or_create("svc-custom")
        assert cb.failure_threshold == 10
        assert cb.recovery_timeout == 60.0
        assert cb.half_open_max_calls == 2

    def test_service_state_is_independent(self) -> None:
        """Tripping one service's breaker must not affect another."""
        reg = CircuitBreakerRegistry(default_failure_threshold=2)
        cb_a = reg.get_or_create("svc-a")
        cb_b = reg.get_or_create("svc-b")

        # Trip svc-a.
        cb_a.record_failure()
        cb_a.record_failure()
        assert cb_a.state == "open"

        # svc-b must still be closed.
        assert cb_b.state == "closed"
        assert cb_b.can_execute() is True
