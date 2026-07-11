"""
Dynamic Route Router (Step 2)
==============================
Resolves an incoming request path to a backend service URL by matching
against a table of path-prefix rules.

Design decisions
----------------
- **Longest-prefix-first matching**: routes are sorted by prefix length
  descending, so ``/users/admin`` beats ``/users``.  This is the same
  strategy used by nginx ``location`` blocks and AWS API Gateway.
- **Hot-reload via file polling**: every N seconds (configurable) the
  gateway checks ``routes.json``'s mtime and reloads if changed.  We
  chose polling over inotify/FSEvents because it works identically on
  Linux, macOS, and Windows (Docker mounts included) and adds zero
  dependencies.  The polling interval is 5 s by default — negligible
  overhead for a feature that prevents downtime on config changes.
- **Thread-safe swap**: the route table is replaced atomically (single
  reference assignment) so in-flight requests never see a half-loaded
  table.

Future steps will add a PostgreSQL-backed route source as an alternative,
but the JSON file remains the simplest way to get started.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("gateway.router")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BackendTarget:
    """A single backend instance for a route."""
    url: str
    weight: int = 1  # reserved for Step 5 weighted load-balancing


@dataclass(frozen=True)
class Route:
    """
    Maps a path prefix to one or more backend targets.

    Attributes
    ----------
    prefix :
        The path prefix to match (e.g. ``/users``).  A request to
        ``/users/123`` matches this route.
    backends :
        One or more upstream URLs.  Step 1–2 use only the first;
        Step 5 fans out to all via load balancing.
    strip_prefix :
        If True, strip the prefix before forwarding.  E.g. a request
        to ``/api/users/1`` with prefix ``/api`` becomes ``/users/1``
        on the backend.
    description :
        Human-readable note (ignored at runtime).
    """
    prefix: str
    backends: tuple[BackendTarget, ...] = ()
    strip_prefix: bool = False
    description: str = ""


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------
class RouteTable:
    """
    Thread-safe, hot-reloadable route table.

    The table is a simple sorted list; ``resolve()`` does a linear scan.
    For a production gateway with thousands of routes you'd build a
    prefix-trie (radix tree), but a linear scan is perfectly fine for
    the ≤ 100 routes typical in a portfolio or mid-scale deployment.
    """

    def __init__(self) -> None:
        self._routes: list[Route] = []
        self._lock = threading.Lock()

    # -- mutation ----------------------------------------------------------
    def load_from_list(self, routes: list[Route]) -> None:
        """Replace the entire route table atomically (longest-prefix first)."""
        sorted_routes = sorted(routes, key=lambda r: len(r.prefix), reverse=True)
        with self._lock:
            self._routes = sorted_routes
        logger.info("Route table loaded: %d route(s)", len(sorted_routes))
        for r in sorted_routes:
            urls = [b.url for b in r.backends]
            logger.info("  %-20s -> %s", r.prefix, urls)

    # -- lookup ------------------------------------------------------------
    def resolve(self, path: str) -> Optional[Route]:
        """
        Return the first Route whose prefix matches *path*, or None.

        Matching is longest-prefix-first because ``_routes`` is sorted
        by descending prefix length.
        """
        routes = self._routes  # snapshot (single ref read = atomic in CPython)
        for route in routes:
            if path == route.prefix or path.startswith(route.prefix + "/"):
                return route
        return None

    def all_routes(self) -> list[Route]:
        """Return a snapshot of all routes (for admin API / debugging)."""
        return list(self._routes)


# ---------------------------------------------------------------------------
# JSON loader
# ---------------------------------------------------------------------------
def _parse_routes_json(data: dict) -> list[Route]:
    """Parse the ``routes.json`` schema into Route objects."""
    routes: list[Route] = []
    for entry in data.get("routes", []):
        backends = tuple(
            BackendTarget(url=b["url"], weight=b.get("weight", 1))
            for b in entry.get("backends", [])
        )
        routes.append(Route(
            prefix=entry["prefix"].rstrip("/"),  # normalise: no trailing /
            backends=backends,
            strip_prefix=entry.get("strip_prefix", False),
            description=entry.get("description", ""),
        ))
    return routes


def load_routes_from_file(filepath: str | Path) -> list[Route]:
    """Read and parse a routes JSON file."""
    filepath = Path(filepath)
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return _parse_routes_json(data)


# ---------------------------------------------------------------------------
# File-watcher (hot-reload)
# ---------------------------------------------------------------------------
class RouteFileWatcher:
    """
    Background thread that polls a JSON config file and reloads the
    RouteTable when the file changes.

    Why polling instead of inotify/FSEvents?
    - Cross-platform (works identically on Windows, Linux, macOS)
    - Works with Docker bind-mounts (inotify doesn't fire for host edits
      on some Docker storage drivers)
    - Zero extra dependencies
    - 5-second polling is negligible overhead
    """

    def __init__(
        self,
        filepath: str | Path,
        route_table: RouteTable,
        poll_interval: float = 5.0,
    ) -> None:
        self._filepath = Path(filepath)
        self._route_table = route_table
        self._poll_interval = poll_interval
        self._last_mtime: float = 0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Load once immediately, then start the background poller."""
        self._do_reload()  # initial load
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("Route file watcher started: %s (poll every %.1fs)",
                     self._filepath, self._poll_interval)

    def stop(self) -> None:
        """Signal the poller thread to exit."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self._poll_interval + 1)

    # -- internals ---------------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(self._poll_interval)
            if self._stop_event.is_set():
                break
            try:
                mtime = os.path.getmtime(self._filepath)
                if mtime != self._last_mtime:
                    self._do_reload()
            except Exception:
                logger.exception("Error checking route file")

    def _do_reload(self) -> None:
        try:
            routes = load_routes_from_file(self._filepath)
            self._route_table.load_from_list(routes)
            self._last_mtime = os.path.getmtime(self._filepath)
            logger.info("Routes reloaded from %s", self._filepath)
        except Exception:
            logger.exception("Failed to reload routes from %s", self._filepath)
