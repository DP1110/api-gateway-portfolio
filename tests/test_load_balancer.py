"""
Unit tests for ``gateway.loadbalancer.lb``
==========================================

All tests are synchronous and use no real HTTP calls.  Health-check
probes are mocked with ``unittest.mock.patch`` so we can control
exactly which backends appear healthy or unhealthy.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from gateway.loadbalancer.lb import BackendInstance, LoadBalancer


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture
def lb() -> LoadBalancer:
    """Fresh ``LoadBalancer`` instance with default settings."""
    return LoadBalancer()


@pytest.fixture
def lb_with_backends(lb: LoadBalancer) -> LoadBalancer:
    """LoadBalancer pre-loaded with three backends for 'web'."""
    lb.register("web", "http://backend-1:8080")
    lb.register("web", "http://backend-2:8080")
    lb.register("web", "http://backend-3:8080")
    return lb


# ================================================================== #
# Round-Robin Tests
# ================================================================== #


class TestRoundRobin:
    """Round-robin selection strategy."""

    def test_cyclic_order(self, lb_with_backends: LoadBalancer) -> None:
        """Backends are returned in repeating 1-2-3-1-2-3 order."""
        lb = lb_with_backends
        urls = [
            lb.select("web", "round-robin").url  # type: ignore[union-attr]
            for _ in range(6)
        ]
        assert urls == [
            "http://backend-1:8080",
            "http://backend-2:8080",
            "http://backend-3:8080",
            "http://backend-1:8080",
            "http://backend-2:8080",
            "http://backend-3:8080",
        ]

    def test_single_backend(self, lb: LoadBalancer) -> None:
        """With only one backend, round-robin always returns it."""
        lb.register("solo", "http://only-one:9090")
        for _ in range(5):
            result = lb.select("solo", "round-robin")
            assert result is not None
            assert result.url == "http://only-one:9090"

    def test_skips_unhealthy(self, lb_with_backends: LoadBalancer) -> None:
        """Unhealthy backends are excluded from the rotation.

        After marking backend-2 unhealthy the cycle should be 1-3-1-3.
        """
        lb = lb_with_backends

        # Directly mark backend-2 as unhealthy.
        with lb._lock:
            for b in lb._backends["web"]:
                if b.url == "http://backend-2:8080":
                    b.healthy = False

        urls = [
            lb.select("web", "round-robin").url  # type: ignore[union-attr]
            for _ in range(4)
        ]
        assert urls == [
            "http://backend-1:8080",
            "http://backend-3:8080",
            "http://backend-1:8080",
            "http://backend-3:8080",
        ]

    def test_all_unhealthy_returns_none(
        self, lb_with_backends: LoadBalancer
    ) -> None:
        """When every backend is unhealthy, ``select`` returns ``None``."""
        lb = lb_with_backends
        with lb._lock:
            for b in lb._backends["web"]:
                b.healthy = False

        assert lb.select("web", "round-robin") is None

    def test_unknown_service_returns_none(self, lb: LoadBalancer) -> None:
        """Selecting from a non-existent service returns ``None``."""
        assert lb.select("does-not-exist", "round-robin") is None


# ================================================================== #
# Least-Connections Tests
# ================================================================== #


class TestLeastConnections:
    """Least-connections selection strategy."""

    def test_picks_fewest_connections(
        self, lb_with_backends: LoadBalancer
    ) -> None:
        """The backend with the fewest active connections is chosen."""
        lb = lb_with_backends

        # Simulate varying load.
        lb.mark_request_start("web", "http://backend-1:8080")
        lb.mark_request_start("web", "http://backend-1:8080")  # 2
        lb.mark_request_start("web", "http://backend-2:8080")  # 1
        # backend-3 has 0

        result = lb.select("web", "least-connections")
        assert result is not None
        assert result.url == "http://backend-3:8080"

    def test_tie_breaks_by_order(
        self, lb_with_backends: LoadBalancer
    ) -> None:
        """When connection counts tie, the first-registered backend wins."""
        lb = lb_with_backends
        # All at zero -- should pick backend-1 (first registered).
        result = lb.select("web", "least-connections")
        assert result is not None
        assert result.url == "http://backend-1:8080"

    def test_skips_unhealthy(self, lb_with_backends: LoadBalancer) -> None:
        """Unhealthy backends are not considered even if they have the
        fewest connections.
        """
        lb = lb_with_backends

        # Make backend-3 (0 connections) unhealthy.
        with lb._lock:
            for b in lb._backends["web"]:
                if b.url == "http://backend-3:8080":
                    b.healthy = False

        # backend-1 and backend-2 both at 0 -- should pick backend-1.
        result = lb.select("web", "least-connections")
        assert result is not None
        assert result.url == "http://backend-1:8080"

    def test_all_unhealthy_returns_none(
        self, lb_with_backends: LoadBalancer
    ) -> None:
        lb = lb_with_backends
        with lb._lock:
            for b in lb._backends["web"]:
                b.healthy = False

        assert lb.select("web", "least-connections") is None


# ================================================================== #
# Registration / Deregistration
# ================================================================== #


class TestRegistration:

    def test_register_adds_backend(self, lb: LoadBalancer) -> None:
        lb.register("api", "http://api-1:3000")
        result = lb.select("api")
        assert result is not None
        assert result.url == "http://api-1:3000"

    def test_duplicate_register_is_noop(self, lb: LoadBalancer) -> None:
        """Registering the same URL twice does not produce duplicates."""
        lb.register("api", "http://api-1:3000")
        lb.register("api", "http://api-1:3000")

        with lb._lock:
            assert len(lb._backends["api"]) == 1

    def test_deregister_removes_backend(
        self, lb_with_backends: LoadBalancer
    ) -> None:
        lb = lb_with_backends
        lb.deregister("web", "http://backend-2:8080")

        with lb._lock:
            urls = [b.url for b in lb._backends["web"]]
        assert "http://backend-2:8080" not in urls
        assert len(urls) == 2

    def test_deregister_unknown_url_is_noop(
        self, lb_with_backends: LoadBalancer
    ) -> None:
        """Deregistering a URL that was never registered does not raise."""
        lb = lb_with_backends
        lb.deregister("web", "http://ghost:1234")  # no error

        with lb._lock:
            assert len(lb._backends["web"]) == 3

    def test_deregister_unknown_service_is_noop(
        self, lb: LoadBalancer
    ) -> None:
        lb.deregister("nope", "http://x:1")  # should not raise


# ================================================================== #
# Connection Tracking
# ================================================================== #


class TestConnectionTracking:

    def test_start_increments(self, lb: LoadBalancer) -> None:
        lb.register("svc", "http://s:80")
        lb.mark_request_start("svc", "http://s:80")
        lb.mark_request_start("svc", "http://s:80")

        with lb._lock:
            assert lb._backends["svc"][0].active_connections == 2

    def test_end_decrements(self, lb: LoadBalancer) -> None:
        lb.register("svc", "http://s:80")
        lb.mark_request_start("svc", "http://s:80")
        lb.mark_request_start("svc", "http://s:80")
        lb.mark_request_end("svc", "http://s:80")

        with lb._lock:
            assert lb._backends["svc"][0].active_connections == 1

    def test_end_clamps_to_zero(self, lb: LoadBalancer) -> None:
        """Decrementing past zero does not go negative."""
        lb.register("svc", "http://s:80")
        lb.mark_request_end("svc", "http://s:80")

        with lb._lock:
            assert lb._backends["svc"][0].active_connections == 0

    def test_unknown_backend_is_noop(self, lb: LoadBalancer) -> None:
        """Tracking calls for unknown backends do not raise."""
        lb.mark_request_start("svc", "http://no-such:80")
        lb.mark_request_end("svc", "http://no-such:80")


# ================================================================== #
# Health Checking (mocked)
# ================================================================== #


class TestHealthChecks:
    """Health-check logic with ``_probe`` mocked out."""

    def test_backend_marked_unhealthy_after_threshold(
        self, lb: LoadBalancer
    ) -> None:
        """A backend becomes unhealthy after ``failure_threshold``
        consecutive probe failures.
        """
        lb = LoadBalancer(failure_threshold=2)
        lb.register("svc", "http://sick:80")

        with patch.object(lb, "_probe", return_value=False):
            lb._run_health_checks()  # failure 1
            with lb._lock:
                assert lb._backends["svc"][0].healthy is True  # not yet
                assert lb._backends["svc"][0].consecutive_failures == 1

            lb._run_health_checks()  # failure 2 -- threshold met
            with lb._lock:
                assert lb._backends["svc"][0].healthy is False
                assert lb._backends["svc"][0].consecutive_failures == 2

    def test_backend_recovers(self, lb: LoadBalancer) -> None:
        """A previously-unhealthy backend is reinstated on a successful
        probe.
        """
        lb = LoadBalancer(failure_threshold=1)
        lb.register("svc", "http://flaky:80")

        # First: make it unhealthy.
        with patch.object(lb, "_probe", return_value=False):
            lb._run_health_checks()
        with lb._lock:
            assert lb._backends["svc"][0].healthy is False

        # Then: recovery.
        with patch.object(lb, "_probe", return_value=True):
            lb._run_health_checks()
        with lb._lock:
            b = lb._backends["svc"][0]
            assert b.healthy is True
            assert b.consecutive_failures == 0

    def test_healthy_probe_resets_failure_counter(
        self, lb: LoadBalancer
    ) -> None:
        lb = LoadBalancer(failure_threshold=3)
        lb.register("svc", "http://h:80")

        # Accumulate 2 failures (threshold is 3).
        with patch.object(lb, "_probe", return_value=False):
            lb._run_health_checks()
            lb._run_health_checks()

        # One success resets the counter.
        with patch.object(lb, "_probe", return_value=True):
            lb._run_health_checks()

        with lb._lock:
            assert lb._backends["svc"][0].consecutive_failures == 0
            assert lb._backends["svc"][0].healthy is True

    def test_last_health_check_timestamp_updated(
        self, lb: LoadBalancer
    ) -> None:
        lb.register("svc", "http://h:80")

        before = time.time()
        with patch.object(lb, "_probe", return_value=True):
            lb._run_health_checks()
        after = time.time()

        with lb._lock:
            ts = lb._backends["svc"][0].last_health_check
        assert before <= ts <= after

    def test_start_and_stop_health_checks(self, lb: LoadBalancer) -> None:
        """The health-check thread can be started and stopped cleanly."""
        lb.register("svc", "http://h:80")

        with patch.object(lb, "_probe", return_value=True):
            lb.start_health_checks(interval=0.05)
            assert lb._health_thread is not None
            assert lb._health_thread.is_alive()

            # Let it run at least one cycle.
            time.sleep(0.15)

            lb.stop_health_checks()
            assert lb._health_thread is None


# ================================================================== #
# Strategy Validation
# ================================================================== #


class TestStrategyValidation:

    def test_unknown_strategy_raises(self, lb: LoadBalancer) -> None:
        lb.register("svc", "http://h:80")
        with pytest.raises(ValueError, match="Unknown strategy"):
            lb.select("svc", strategy="random")

    def test_default_strategy_is_round_robin(
        self, lb_with_backends: LoadBalancer
    ) -> None:
        """Calling ``select`` without an explicit strategy uses
        round-robin.
        """
        lb = lb_with_backends
        first = lb.select("web")
        second = lb.select("web")
        assert first is not None and second is not None
        assert first.url != second.url  # different backends
