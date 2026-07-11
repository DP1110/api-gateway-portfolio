"""
Unit tests for the dynamic router (Step 2).
Covers: prefix matching, longest-prefix wins, no-match returns None,
strip_prefix logic, and config reload.
"""

import json
import os
import tempfile
import time

import pytest

from gateway.routing.router import (
    BackendTarget,
    Route,
    RouteFileWatcher,
    RouteTable,
    load_routes_from_file,
)


# ---------------------------------------------------------------------------
# RouteTable.resolve – prefix matching
# ---------------------------------------------------------------------------
class TestRouteTable:
    @pytest.fixture
    def table(self) -> RouteTable:
        """Pre-populated route table with /users and /orders."""
        t = RouteTable()
        t.load_from_list([
            Route(prefix="/users", backends=(BackendTarget(url="http://users:9001"),)),
            Route(prefix="/orders", backends=(BackendTarget(url="http://orders:9002"),)),
        ])
        return t

    def test_exact_match(self, table: RouteTable) -> None:
        route = table.resolve("/users")
        assert route is not None
        assert route.prefix == "/users"

    def test_subpath_match(self, table: RouteTable) -> None:
        route = table.resolve("/users/123")
        assert route is not None
        assert route.prefix == "/users"

    def test_other_prefix(self, table: RouteTable) -> None:
        route = table.resolve("/orders/456")
        assert route is not None
        assert route.prefix == "/orders"

    def test_no_match(self, table: RouteTable) -> None:
        assert table.resolve("/products") is None

    def test_root_no_match(self, table: RouteTable) -> None:
        """Root path should NOT match /users or /orders."""
        assert table.resolve("/") is None

    def test_similar_prefix_no_false_positive(self, table: RouteTable) -> None:
        """/usersettings should NOT match /users (must require / boundary)."""
        assert table.resolve("/usersettings") is None


class TestLongestPrefixWins:
    def test_specific_beats_general(self) -> None:
        t = RouteTable()
        t.load_from_list([
            Route(prefix="/api", backends=(BackendTarget(url="http://fallback:80"),)),
            Route(prefix="/api/v2", backends=(BackendTarget(url="http://v2:80"),)),
            Route(prefix="/api/v2/admin", backends=(BackendTarget(url="http://admin:80"),)),
        ])

        r = t.resolve("/api/v2/admin/settings")
        assert r is not None
        assert r.backends[0].url == "http://admin:80"

        r = t.resolve("/api/v2/users")
        assert r is not None
        assert r.backends[0].url == "http://v2:80"

        r = t.resolve("/api/v1/legacy")
        assert r is not None
        assert r.backends[0].url == "http://fallback:80"


# ---------------------------------------------------------------------------
# JSON loading
# ---------------------------------------------------------------------------
class TestJsonLoader:
    def test_load_routes_from_file(self, tmp_path) -> None:
        config = {
            "routes": [
                {
                    "prefix": "/svc",
                    "backends": [{"url": "http://svc:80", "weight": 2}],
                    "strip_prefix": True,
                    "description": "test",
                }
            ]
        }
        p = tmp_path / "routes.json"
        p.write_text(json.dumps(config))

        routes = load_routes_from_file(p)
        assert len(routes) == 1
        assert routes[0].prefix == "/svc"
        assert routes[0].strip_prefix is True
        assert routes[0].backends[0].weight == 2

    def test_trailing_slash_normalised(self, tmp_path) -> None:
        config = {"routes": [{"prefix": "/trailing/", "backends": [{"url": "http://x:80"}]}]}
        p = tmp_path / "routes.json"
        p.write_text(json.dumps(config))

        routes = load_routes_from_file(p)
        assert routes[0].prefix == "/trailing"  # normalised (no trailing /)


# ---------------------------------------------------------------------------
# Hot-reload file watcher
# ---------------------------------------------------------------------------
class TestFileWatcher:
    def test_initial_load_and_hot_reload(self, tmp_path) -> None:
        """Watcher loads on start, then picks up file changes."""
        config_v1 = {"routes": [
            {"prefix": "/v1", "backends": [{"url": "http://v1:80"}]},
        ]}
        p = tmp_path / "routes.json"
        p.write_text(json.dumps(config_v1))

        table = RouteTable()
        watcher = RouteFileWatcher(p, table, poll_interval=0.2)
        watcher.start()

        try:
            # v1 should be loaded immediately
            assert table.resolve("/v1") is not None
            assert table.resolve("/v2") is None

            # Update file → v2 added
            time.sleep(0.1)  # ensure mtime differs
            config_v2 = {"routes": [
                {"prefix": "/v1", "backends": [{"url": "http://v1:80"}]},
                {"prefix": "/v2", "backends": [{"url": "http://v2:80"}]},
            ]}
            p.write_text(json.dumps(config_v2))

            # Wait for the watcher to notice
            time.sleep(0.5)
            assert table.resolve("/v2") is not None
        finally:
            watcher.stop()
