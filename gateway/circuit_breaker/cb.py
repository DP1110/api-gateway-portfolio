"""
Circuit Breaker module for the API Gateway.

==========================================================================
WHY CIRCUIT BREAKERS PREVENT CASCADE FAILURES
==========================================================================

In a microservices architecture an upstream service can become slow or
unresponsive.  Without a circuit breaker, the API gateway would keep
dispatching requests to the sick backend, tying up its own connection-pool
threads / async tasks while waiting for timeouts.  This creates a domino
effect:

  1. Backend B becomes slow (disk full, GC pause, DB lock, ...).
  2. Gateway threads pile up waiting on B's responses.
  3. Thread-pool exhaustion starves *healthy* backends A and C.
  4. Callers of A and C now time out --> cascade failure.

The circuit breaker acts as an *automatic fuse*: once it detects a backend
is unhealthy it *immediately* short-circuits requests with a fast 503,
freeing resources for the rest of the system.  After a cooldown it sends a
single probe request to check whether the backend has recovered before
re-opening the floodgates.

==========================================================================
ALGORITHM DESIGN NOTES
==========================================================================

Why CONSECUTIVE failures (not a rolling error-rate)?
----------------------------------------------------
A rolling error-rate (e.g. "50 % of the last 100 requests") is more
sophisticated but requires a sliding window data structure (ring buffer or
time-bucketed counters).  It also reacts slowly when traffic is low --
10 requests/min means a 5-minute window to even *detect* 50 % errors.

Consecutive-failure counting is:
  * O(1) memory (single integer),
  * deterministic (exactly N failures in a row),
  * fast to trip under bursty errors (which is the common failure mode for
    network partitions, pod evictions, and connection-refused errors).
  * easy to reason about in incident reviews ("it tripped after 5 straight
    failures").

The trade-off is that a single success in between two bursts of errors
resets the counter, keeping the breaker closed.  In practice this is
acceptable because a backend that occasionally succeeds is still at least
partially healthy, and the retry/load-balancer layer will spread requests
across replicas.

Why half-open allows only ONE probe request?
--------------------------------------------
When the recovery timer expires, we do NOT want to let all queued callers
slam the recovering backend at once -- that is the "thundering herd"
problem and can immediately knock the backend back down.

By gating the half-open state to a single probe (`half_open_max_calls`),
we:
  * give the backend minimal load to prove health,
  * keep serving 503 to the rest of the callers (no thundering herd),
  * transition atomically: one success -> closed, one failure -> open.

The `half_open_max_calls` parameter is exposed so operators can bump it to
2-3 in high-confidence environments, but defaults to the safest value (1).
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Final


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class CircuitState(Enum):
    """
    Three canonical states of a circuit breaker finite state machine.

    Transitions:
        CLOSED  --[failures >= threshold]--> OPEN
        OPEN    --[recovery_timeout elapsed]--> HALF_OPEN
        HALF_OPEN --[success]--> CLOSED
        HALF_OPEN --[failure]--> OPEN
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Per-service circuit breaker implementing the Closed/Open/Half-Open FSM.

    Thread-safe: every mutation is protected by a re-entrant lock so the
    breaker can be shared safely across ASGI worker tasks.

    Parameters
    ----------
    name:
        Human-readable identifier (typically the backend service name).
    failure_threshold:
        Number of *consecutive* failures required to trip the breaker.
    recovery_timeout:
        Seconds to wait in the Open state before transitioning to Half-Open.
    half_open_max_calls:
        Maximum concurrent probe requests allowed in Half-Open.  Defaults to
        1 to prevent thundering-herd on a recovering backend.

    Usage
    -----
    ::

        cb = CircuitBreaker("payments-service")

        if not cb.can_execute():
            return Response(status_code=503)

        try:
            resp = await backend.call(...)
            cb.record_success()
            return resp
        except Exception:
            cb.record_failure()
            return Response(status_code=502)
    """

    # -- Class-level defaults (documented here for discoverability) ---------

    DEFAULT_FAILURE_THRESHOLD: Final[int] = 5
    DEFAULT_RECOVERY_TIMEOUT: Final[float] = 30.0
    DEFAULT_HALF_OPEN_MAX_CALLS: Final[int] = 1

    def __init__(
        self,
        name: str,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        recovery_timeout: float = DEFAULT_RECOVERY_TIMEOUT,
        half_open_max_calls: int = DEFAULT_HALF_OPEN_MAX_CALLS,
    ) -> None:
        # ---- public config (immutable after init) -------------------------
        self.name: Final[str] = name
        self.failure_threshold: Final[int] = failure_threshold
        self.recovery_timeout: Final[float] = recovery_timeout
        self.half_open_max_calls: Final[int] = half_open_max_calls

        # ---- mutable internal state (guarded by _lock) --------------------

        # We use a *reentrant* lock (RLock) rather than a plain Lock so that
        # ``can_execute`` -> ``record_success/failure`` sequences that happen
        # to be on the same thread cannot deadlock.
        self._lock: Final[threading.RLock] = threading.RLock()

        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0

        # Timestamp (epoch seconds) when the breaker entered the Open state.
        # Used to decide when to allow the Half-Open probe.
        self._opened_at: float = 0.0

        # Counter of in-flight probe calls during Half-Open.  Prevents
        # more than ``half_open_max_calls`` concurrent probes (thundering-
        # herd guard).
        self._half_open_calls: int = 0

    # -- Read-only properties -----------------------------------------------

    @property
    def state(self) -> str:
        """Return the current state as a lowercase string.

        We expose a plain string rather than the enum so JSON serialization
        and logging are trivial and callers do not need to import the enum.
        """
        with self._lock:
            # If we are nominally OPEN but the recovery timeout has elapsed,
            # lazily promote to HALF_OPEN on read.  This avoids needing a
            # background timer / scheduler just to flip state.
            if self._state is CircuitState.OPEN:
                if time.time() - self._opened_at >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
            return self._state.value

    @property
    def failure_count(self) -> int:
        """Current consecutive-failure counter (useful for monitoring)."""
        with self._lock:
            return self._consecutive_failures

    # -- Core API -----------------------------------------------------------

    def can_execute(self) -> bool:
        """Determine whether a new outbound request should be allowed.

        Returns ``True`` if the request may proceed, ``False`` if the
        circuit is open and the caller should fail-fast with 503.

        State-transition side-effects:
        * OPEN -> HALF_OPEN when recovery_timeout has elapsed.
        * Increments ``_half_open_calls`` so only a bounded number of
          probes are admitted.
        """
        with self._lock:
            # --- Closed: everything passes ---
            if self._state is CircuitState.CLOSED:
                return True

            # --- Open: check if recovery timeout has elapsed ---
            if self._state is CircuitState.OPEN:
                elapsed = time.time() - self._opened_at
                if elapsed < self.recovery_timeout:
                    # Still cooling down -- reject immediately.
                    return False
                # Recovery window reached -- transition to Half-Open.
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0
                # Fall through to the Half-Open logic below.

            # --- Half-Open: allow up to ``half_open_max_calls`` probes ---
            #
            # Why a counter instead of a boolean flag?
            # A counter generalises to N probe requests, which is useful in
            # high-traffic gateways where a single probe is too conservative.
            # The default (1) is the classic "allow one" strategy.
            if self._half_open_calls < self.half_open_max_calls:
                self._half_open_calls += 1
                return True

            # All probe slots occupied -- reject surplus requests.
            return False

    def record_success(self) -> None:
        """Record a successful backend response.

        * In **Closed** state: resets the consecutive-failure counter.
          This is important -- a single success proves the backend is at
          least partially alive, so we give it a clean slate.

        * In **Half-Open** state: the probe succeeded, meaning the backend
          has recovered.  Transition back to Closed and reset counters.
        """
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                # Probe succeeded -- backend is healthy again.
                self._state = CircuitState.CLOSED
            # Always reset the failure counter on success, regardless of
            # current state.  This ensures that a single success in between
            # bursts of errors buys the backend another ``failure_threshold``
            # chances.
            self._consecutive_failures = 0
            self._half_open_calls = 0

    def record_failure(self) -> None:
        """Record a failed backend response.

        * In **Closed** state: increment consecutive failures.  If the
          threshold is reached, trip the breaker to Open.

        * In **Half-Open** state: the probe request failed, so the backend
          is still unhealthy.  Go back to Open and restart the recovery
          timer.
        """
        with self._lock:
            self._consecutive_failures += 1

            if self._state is CircuitState.HALF_OPEN:
                # Probe failed -- backend is not ready yet.
                # Transition back to Open and restart the cooldown clock.
                self._trip_open()
                return

            if self._state is CircuitState.CLOSED:
                if self._consecutive_failures >= self.failure_threshold:
                    self._trip_open()

    def reset(self) -> None:
        """Manually reset the breaker to the Closed state.

        Useful for admin / ops endpoints that want to force-close a breaker
        after a manual intervention (e.g. the backend was restarted).
        """
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._half_open_calls = 0
            self._opened_at = 0.0

    # -- Internals ----------------------------------------------------------

    def _trip_open(self) -> None:
        """Transition to the Open state and record the timestamp.

        Must be called while holding ``_lock``.
        """
        self._state = CircuitState.OPEN
        self._opened_at = time.time()
        self._half_open_calls = 0

    # -- Dunder helpers -----------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(name={self.name!r}, state={self.state!r}, "
            f"failures={self.failure_count}/{self.failure_threshold})"
        )


# ---------------------------------------------------------------------------
# CircuitBreakerRegistry
# ---------------------------------------------------------------------------

class CircuitBreakerRegistry:
    """Thread-safe registry that manages one :class:`CircuitBreaker` per
    backend service.

    The gateway typically has one global registry instance.  When a request
    arrives for ``/api/payments/...``, the proxy layer calls
    ``registry.get_or_create("payments-service")`` to obtain (or lazily
    create) the breaker for that backend.

    Parameters
    ----------
    default_failure_threshold, default_recovery_timeout, default_half_open_max_calls:
        Defaults applied to every breaker created through
        :meth:`get_or_create`.  Individual services can override these by
        constructing their own :class:`CircuitBreaker` and registering it
        directly via ``registry._breakers[name] = custom_cb``.
    """

    def __init__(
        self,
        default_failure_threshold: int = CircuitBreaker.DEFAULT_FAILURE_THRESHOLD,
        default_recovery_timeout: float = CircuitBreaker.DEFAULT_RECOVERY_TIMEOUT,
        default_half_open_max_calls: int = CircuitBreaker.DEFAULT_HALF_OPEN_MAX_CALLS,
    ) -> None:
        self._lock: Final[threading.Lock] = threading.Lock()
        self._breakers: dict[str, CircuitBreaker] = {}
        self._default_failure_threshold = default_failure_threshold
        self._default_recovery_timeout = default_recovery_timeout
        self._default_half_open_max_calls = default_half_open_max_calls

    def get_or_create(self, service_name: str) -> CircuitBreaker:
        """Return the existing breaker for *service_name*, or create one.

        Uses double-checked locking to avoid holding the lock on the fast
        path (breaker already exists).
        """
        # Fast path -- no lock needed for a simple dict lookup in CPython
        # thanks to the GIL, but we still do the formal double-check inside
        # the lock for correctness on alternative runtimes (e.g. nogil).
        cb = self._breakers.get(service_name)
        if cb is not None:
            return cb

        with self._lock:
            # Double-check: another thread may have created it between the
            # first lookup and acquiring the lock.
            cb = self._breakers.get(service_name)
            if cb is not None:
                return cb

            cb = CircuitBreaker(
                name=service_name,
                failure_threshold=self._default_failure_threshold,
                recovery_timeout=self._default_recovery_timeout,
                half_open_max_calls=self._default_half_open_max_calls,
            )
            self._breakers[service_name] = cb
            return cb

    def get_all(self) -> dict[str, CircuitBreaker]:
        """Return a snapshot dict of all registered breakers.

        Returns a *shallow copy* so the caller cannot accidentally mutate
        the registry's internal mapping.
        """
        with self._lock:
            return dict(self._breakers)

    def __repr__(self) -> str:
        return f"CircuitBreakerRegistry(services={list(self._breakers.keys())})"
