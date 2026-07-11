"""
Load Balancer Module for the API Gateway
=========================================

Provides two pluggable load-balancing strategies (round-robin and
least-connections) and a background health-checker that removes unhealthy
backends from rotation and reinstates them once they recover.

Design Decisions
----------------
* **Thread-safety first** -- every mutable data path is guarded by a
  ``threading.Lock``.  We deliberately chose a single coarse lock per
  ``LoadBalancer`` instance rather than per-service fine-grained locks.
  The critical sections are tiny (counter increment / min-scan) so
  contention is negligible, and a single lock eliminates the risk of
  lock-ordering deadlocks when health-check results span services.

* **Dataclass backend model** -- ``BackendInstance`` is a plain
  ``@dataclass`` so it remains serialisable and easy to inspect in
  admin endpoints.  Fields like ``consecutive_failures`` live here
  rather than in a side table so that a single object carries full
  backend state.

* **Strategy as a string enum** -- keeps the public API simple
  (``select(..., strategy='round-robin')``) and avoids importing an
  ``Enum`` in every call-site.  Unknown strategies raise ``ValueError``
  immediately.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import urllib.request
import urllib.error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class BackendInstance:
    """Represents a single upstream backend server.

    Attributes
    ----------
    url:
        Base URL of the backend (e.g. ``http://10.0.1.5:8080``).
    healthy:
        Current health status.  Set to ``False`` when consecutive health
        check failures exceed the threshold; set back to ``True`` on
        recovery.
    active_connections:
        Number of in-flight requests currently routed to this backend.
        Managed externally via ``mark_request_start`` / ``mark_request_end``.
    last_health_check:
        Unix timestamp of the most recent health probe.
    consecutive_failures:
        Running count of back-to-back health check failures.  Resets to
        zero on the first successful probe.
    """

    url: str
    healthy: bool = True
    active_connections: int = 0
    last_health_check: float = 0.0
    consecutive_failures: int = 0


# ---------------------------------------------------------------------------
# Load Balancer
# ---------------------------------------------------------------------------

# How many consecutive health-check failures before we mark a backend
# as unhealthy.  Three strikes balances false-positive tolerance against
# detection speed.
_DEFAULT_FAILURE_THRESHOLD: int = 3

# Timeout in seconds for a single health-check HTTP GET.
_HEALTH_CHECK_TIMEOUT: float = 5.0


class LoadBalancer:
    """Service-aware load balancer with health checking.

    Each *service* (identified by a plain string name) owns an
    independent set of ``BackendInstance`` objects.  The balancer
    supports two selection strategies:

    ``round-robin``
        O(1) amortised.  An atomic counter is incremented on every
        call and the modulo index into the *healthy* backend list is
        returned.  This is ideal for **homogeneous** backends -- when
        every server has roughly the same processing power and
        latency profile, round-robin distributes load evenly without
        any per-request bookkeeping overhead.

    ``least-connections``
        O(n) where *n* is the number of healthy backends.  Scans all
        healthy backends and returns the one with the fewest
        ``active_connections``.  This is the better choice when
        backends are **heterogeneous** -- for instance, when some
        servers are faster than others, or when request durations
        vary widely.  Faster servers will complete requests sooner,
        freeing their connection slots, so subsequent requests
        naturally gravitate toward them.  The O(n) scan is
        acceptable because *n* (backend count per service) is
        typically small (single digits to low dozens).

    Parameters
    ----------
    failure_threshold:
        Number of consecutive probe failures before a backend is
        marked unhealthy.
    """

    def __init__(self, failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD) -> None:
        # ---- internal state ----
        # service_name -> list[BackendInstance]
        self._backends: dict[str, list[BackendInstance]] = {}

        # service_name -> monotonically increasing counter for round-robin.
        # We use a plain ``int`` behind a lock instead of
        # ``itertools.count`` so that the counter can be inspected and
        # tested deterministically.
        self._rr_counters: dict[str, int] = {}

        self._failure_threshold = failure_threshold

        # A single re-entrant lock protects all mutable state.
        # -----------------------------------------------------------
        # Why *one* lock instead of per-service locks?
        # The critical sections are very short (integer arithmetic or a
        # list scan over a handful of items).  A single lock eliminates
        # the possibility of ABBA deadlocks when operations span
        # services (e.g. health-check iterating all services).
        # -----------------------------------------------------------
        self._lock = threading.Lock()

        # Background health-check thread (created lazily).
        self._health_thread: Optional[threading.Thread] = None
        self._health_check_stop = threading.Event()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, service_name: str, url: str) -> None:
        """Add a backend URL to *service_name*.

        If the URL is already registered the call is a no-op so that
        callers can safely retry without producing duplicates.
        """
        with self._lock:
            backends = self._backends.setdefault(service_name, [])
            # Guard against duplicate registrations.
            if any(b.url == url for b in backends):
                logger.debug(
                    "Backend %s already registered for service '%s' -- skipping",
                    url,
                    service_name,
                )
                return
            backends.append(BackendInstance(url=url))
            self._rr_counters.setdefault(service_name, 0)
            logger.info(
                "Registered backend %s for service '%s'", url, service_name
            )

    def deregister(self, service_name: str, url: str) -> None:
        """Remove a backend URL from *service_name*.

        Silently returns if the service or URL is not found.
        """
        with self._lock:
            backends = self._backends.get(service_name)
            if backends is None:
                return
            self._backends[service_name] = [
                b for b in backends if b.url != url
            ]
            logger.info(
                "Deregistered backend %s from service '%s'", url, service_name
            )

    # ------------------------------------------------------------------
    # Selection strategies
    # ------------------------------------------------------------------

    def select(
        self,
        service_name: str,
        strategy: str = "round-robin",
    ) -> Optional[BackendInstance]:
        """Pick the next backend for *service_name*.

        Returns ``None`` when no healthy backend is available.

        Raises
        ------
        ValueError
            If *strategy* is not one of ``'round-robin'`` or
            ``'least-connections'``.
        """
        if strategy == "round-robin":
            return self._select_round_robin(service_name)
        if strategy == "least-connections":
            return self._select_least_connections(service_name)
        raise ValueError(
            f"Unknown strategy '{strategy}'. "
            f"Supported: 'round-robin', 'least-connections'"
        )

    def _select_round_robin(
        self, service_name: str
    ) -> Optional[BackendInstance]:
        """Round-robin selection -- O(1) amortised.

        Algorithm
        ---------
        We maintain a monotonically increasing counter per service.
        On each call we:

        1. Filter the backend list to only healthy instances.
        2. Compute ``counter % len(healthy)`` to get the index.
        3. Increment the counter.

        Why this works well for **homogeneous** backends:
        Every backend receives exactly the same share of traffic over
        time because the counter visits indices 0, 1, ..., n-1 in a
        repeating cycle.  There is no per-request state to track
        (unlike least-connections) so the overhead is a single modulo
        operation -- effectively O(1).

        The trade-off is that round-robin is *blind* to actual load:
        if one backend is slower than the rest, it will still receive
        its full share of requests, causing queue build-up on that
        server.  Use ``least-connections`` when backends are
        heterogeneous.
        """
        with self._lock:
            healthy = self._healthy_backends(service_name)
            if not healthy:
                return None
            counter = self._rr_counters.get(service_name, 0)
            idx = counter % len(healthy)
            self._rr_counters[service_name] = counter + 1
            return healthy[idx]

    def _select_least_connections(
        self, service_name: str
    ) -> Optional[BackendInstance]:
        """Least-connections selection -- O(n).

        Algorithm
        ---------
        Scan every healthy backend and return the one with the lowest
        ``active_connections`` value.  Ties are broken by list order
        (i.e. the first registered backend wins), which provides
        deterministic behaviour in tests.

        Why this is better for **heterogeneous** latencies:
        When backends have different processing speeds, faster servers
        complete requests sooner, decrementing their
        ``active_connections`` counter.  The next ``select`` call will
        naturally prefer the now-less-loaded server.  Over time this
        produces a *weighted* distribution that matches each server's
        capacity, without requiring explicit weight configuration.

        Cost: We do a full scan of the healthy list on every call.
        This is acceptable because the list is typically small (a
        handful of backends per service).  If the backend list ever
        grew large (hundreds) we could switch to a min-heap, but the
        heap's O(log n) updates on every ``mark_request_start`` /
        ``mark_request_end`` call would add overhead that is not
        worthwhile at small *n*.
        """
        with self._lock:
            healthy = self._healthy_backends(service_name)
            if not healthy:
                return None
            # ``min()`` with ``key`` is cleaner than a manual loop and
            # just as fast for small *n*.  Ties go to the first element
            # encountered, which matches insertion order.
            return min(healthy, key=lambda b: b.active_connections)

    # ------------------------------------------------------------------
    # Connection tracking
    # ------------------------------------------------------------------

    def mark_request_start(self, service_name: str, url: str) -> None:
        """Increment ``active_connections`` for the backend identified
        by (*service_name*, *url*).

        Called by the proxy layer just before forwarding a request to
        the backend.
        """
        with self._lock:
            backend = self._find_backend(service_name, url)
            if backend is not None:
                backend.active_connections += 1

    def mark_request_end(self, service_name: str, url: str) -> None:
        """Decrement ``active_connections`` for the backend identified
        by (*service_name*, *url*).

        Called by the proxy layer when the backend response has been
        fully relayed (or the request errored out).  The counter is
        clamped to zero to guard against mismatched start/end calls.
        """
        with self._lock:
            backend = self._find_backend(service_name, url)
            if backend is not None:
                backend.active_connections = max(
                    0, backend.active_connections - 1
                )

    # ------------------------------------------------------------------
    # Health checking
    # ------------------------------------------------------------------

    def start_health_checks(self, interval: float = 10.0) -> None:
        """Start a background daemon thread that probes every
        registered backend at ``/health`` every *interval* seconds.

        The thread is a daemon so it will not prevent interpreter
        shutdown.  Call ``stop_health_checks`` for a clean stop.
        """
        if self._health_thread is not None and self._health_thread.is_alive():
            logger.warning("Health-check thread is already running")
            return

        self._health_check_stop.clear()

        def _health_loop() -> None:
            while not self._health_check_stop.is_set():
                self._run_health_checks()
                # ``Event.wait`` is interruptible, unlike ``time.sleep``,
                # so ``stop_health_checks`` can wake us immediately.
                self._health_check_stop.wait(timeout=interval)

        self._health_thread = threading.Thread(
            target=_health_loop, daemon=True, name="lb-health-check"
        )
        self._health_thread.start()
        logger.info(
            "Health-check thread started (interval=%.1fs)", interval
        )

    def stop_health_checks(self) -> None:
        """Signal the background health-check thread to stop and wait
        for it to finish (up to 5 s).
        """
        self._health_check_stop.set()
        if self._health_thread is not None:
            self._health_thread.join(timeout=5.0)
            self._health_thread = None
            logger.info("Health-check thread stopped")

    def _run_health_checks(self) -> None:
        """Probe every registered backend once.

        We snapshot the service -> backends mapping under the lock,
        then release the lock while performing (potentially slow) HTTP
        requests.  Results are applied under the lock afterwards.  This
        keeps the critical section short and avoids blocking ``select``
        during I/O.
        """
        # --- snapshot ---
        with self._lock:
            snapshot: list[tuple[str, BackendInstance]] = []
            for svc, backends in self._backends.items():
                for b in backends:
                    snapshot.append((svc, b))

        # --- probe (no lock held) ---
        results: list[tuple[str, BackendInstance, bool]] = []
        for svc, backend in snapshot:
            healthy = self._probe(backend.url)
            results.append((svc, backend, healthy))

        # --- apply ---
        with self._lock:
            for _svc, backend, healthy in results:
                backend.last_health_check = time.time()
                if healthy:
                    if not backend.healthy:
                        logger.info(
                            "Backend %s recovered -- marking healthy",
                            backend.url,
                        )
                    backend.healthy = True
                    backend.consecutive_failures = 0
                else:
                    backend.consecutive_failures += 1
                    if (
                        backend.consecutive_failures
                        >= self._failure_threshold
                    ):
                        if backend.healthy:
                            logger.warning(
                                "Backend %s exceeded failure threshold "
                                "(%d) -- marking unhealthy",
                                backend.url,
                                self._failure_threshold,
                            )
                        backend.healthy = False

    @staticmethod
    def _probe(url: str) -> bool:
        """HTTP GET ``<url>/health``.  Returns ``True`` on 2xx."""
        health_url = url.rstrip("/") + "/health"
        try:
            req = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(
                req, timeout=_HEALTH_CHECK_TIMEOUT
            ) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Helpers (must be called under ``self._lock``)
    # ------------------------------------------------------------------

    def _healthy_backends(
        self, service_name: str
    ) -> list[BackendInstance]:
        """Return only healthy backends for *service_name*.

        Preserves insertion order so that round-robin cycling is
        deterministic.
        """
        return [
            b
            for b in self._backends.get(service_name, [])
            if b.healthy
        ]

    def _find_backend(
        self, service_name: str, url: str
    ) -> Optional[BackendInstance]:
        """Locate a single backend by URL inside *service_name*."""
        for b in self._backends.get(service_name, []):
            if b.url == url:
                return b
        return None
