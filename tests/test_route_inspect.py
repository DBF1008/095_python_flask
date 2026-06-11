"""Tests for the route inspection / diagnostic API (``app.list_routes``)."""

from __future__ import annotations

from functools import partial

import pytest
from click.testing import CliRunner
from flask import Blueprint
from flask import Flask
from flask.cli import FlaskGroup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(static_folder=None):
    """Create a minimal Flask app for testing."""
    return Flask(__name__, static_folder=static_folder)


def _find_route(routes, endpoint):
    """Find a single route entry by endpoint name."""
    for r in routes:
        if r["endpoint"] == endpoint:
            return r
    return None


# ---------------------------------------------------------------------------
# Basic route listing
# ---------------------------------------------------------------------------

class TestListRoutesBasic:
    """Basic list_routes functionality."""

    def test_empty_app(self):
        app = _make_app(static_folder=None)
        with app.app_context():
            assert app.list_routes() == []

    def test_single_route(self):
        app = _make_app(static_folder=None)

        @app.route("/")
        def index():
            return "ok"

        with app.app_context():
            routes = app.list_routes()

        assert len(routes) == 1
        r = routes[0]
        assert r["endpoint"] == "index"
        assert r["rule"] == "/"
        assert "GET" in r["methods"]
        assert r["host"] is None
        assert r["subdomain"] is None
        assert r["defaults"] is None
        assert r["websocket"] is False
        assert r["blueprint"] is None
        assert r["arguments"] == set()

    def test_multiple_routes(self):
        app = _make_app(static_folder=None)
        app.add_url_rule("/a", endpoint="a")
        app.add_url_rule("/b", endpoint="b")
        app.add_url_rule("/c", endpoint="c")

        with app.app_context():
            routes = app.list_routes()

        assert len(routes) == 3
        endpoints = {r["endpoint"] for r in routes}
        assert endpoints == {"a", "b", "c"}

    def test_methods_include_head_and_options(self):
        """list_routes returns all methods including auto-added HEAD/OPTIONS."""
        app = _make_app(static_folder=None)
        app.add_url_rule("/test", methods=["GET", "POST"], endpoint="test")

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "test")
        assert r is not None
        assert "GET" in r["methods"]
        assert "POST" in r["methods"]
        assert "HEAD" in r["methods"]
        assert "OPTIONS" in r["methods"]

    def test_arguments_from_url_converters(self):
        app = _make_app(static_folder=None)
        app.add_url_rule("/users/<int:id>/posts/<string:slug>", endpoint="post_detail")

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "post_detail")
        assert r is not None
        assert r["arguments"] == {"id", "slug"}

    def test_url_defaults(self):
        app = _make_app(static_folder=None)
        app.add_url_rule(
            "/page",
            endpoint="page",
            defaults={"format": "html", "version": 1},
        )

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "page")
        assert r is not None
        assert r["defaults"] == {"format": "html", "version": 1}

    def test_websocket_route(self):
        app = _make_app(static_folder=None)
        app.add_url_rule("/ws", endpoint="ws", websocket=True)

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "ws")
        assert r is not None
        assert r["websocket"] is True

    def test_static_route_included(self):
        """Static file route should appear in list_routes output."""
        import tempfile
        import os

        tmpdir = tempfile.mkdtemp()
        static_dir = os.path.join(tmpdir, "static")
        os.makedirs(static_dir)
        app = Flask(__name__, static_folder=static_dir)

        with app.app_context():
            routes = app.list_routes()

        static = _find_route(routes, "static")
        assert static is not None
        assert "/static" in static["rule"]


# ---------------------------------------------------------------------------
# Blueprint routes
# ---------------------------------------------------------------------------

class TestListRoutesBlueprints:
    """Blueprint-related route inspection."""

    def test_simple_blueprint(self):
        app = _make_app(static_folder=None)
        bp = Blueprint("api", __name__, url_prefix="/api")

        @bp.route("/users")
        def users():
            return "ok"

        @bp.route("/items")
        def items():
            return "ok"

        app.register_blueprint(bp)

        with app.app_context():
            routes = app.list_routes()

        users_r = _find_route(routes, "api.users")
        items_r = _find_route(routes, "api.items")

        assert users_r is not None
        assert users_r["rule"] == "/api/users"
        assert users_r["blueprint"] == "api"

        assert items_r is not None
        assert items_r["rule"] == "/api/items"
        assert items_r["blueprint"] == "api"

    def test_blueprint_without_prefix(self):
        app = _make_app(static_folder=None)
        bp = Blueprint("bp", __name__)

        @bp.route("/hello")
        def hello():
            return "ok"

        app.register_blueprint(bp)

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "bp.hello")
        assert r is not None
        assert r["rule"] == "/hello"
        assert r["blueprint"] == "bp"

    def test_blueprint_with_subdomain(self):
        app = _make_app(static_folder=None)
        app.config["SERVER_NAME"] = "example.com"
        bp = Blueprint("api", __name__, subdomain="api")

        @bp.route("/data")
        def data():
            return "ok"

        app.register_blueprint(bp)

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "api.data")
        assert r is not None
        assert r["subdomain"] == "api"

    def test_blueprint_with_url_defaults(self):
        app = _make_app(static_folder=None)
        bp = Blueprint("v1", __name__, url_prefix="/v1", url_defaults={"version": 1})

        @bp.route("/items")
        def items():
            return "ok"

        app.register_blueprint(bp)

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "v1.items")
        assert r is not None
        assert r["defaults"] == {"version": 1}


# ---------------------------------------------------------------------------
# Nested blueprints
# ---------------------------------------------------------------------------

class TestListRoutesNestedBlueprints:
    """Nested blueprint route inspection."""

    def test_two_level_nesting(self):
        app = _make_app(static_folder=None)

        parent = Blueprint("parent", __name__, url_prefix="/parent")
        child = Blueprint("child", __name__, url_prefix="/child")

        @child.route("/page")
        def page():
            return "ok"

        parent.register_blueprint(child)
        app.register_blueprint(parent)

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "parent.child.page")
        assert r is not None
        assert r["rule"] == "/parent/child/page"
        assert r["blueprint"] == "parent.child"

    def test_three_level_nesting(self):
        app = _make_app(static_folder=None)

        top = Blueprint("top", __name__, url_prefix="/top")
        mid = Blueprint("mid", __name__, url_prefix="/mid")
        leaf = Blueprint("leaf", __name__, url_prefix="/leaf")

        @leaf.route("/item")
        def item():
            return "ok"

        mid.register_blueprint(leaf)
        top.register_blueprint(mid)
        app.register_blueprint(top)

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "top.mid.leaf.item")
        assert r is not None
        assert r["rule"] == "/top/mid/leaf/item"
        assert r["blueprint"] == "top.mid.leaf"

    def test_nested_subdomain_merging(self):
        app = _make_app(static_folder=None)
        app.config["SERVER_NAME"] = "example.com"

        parent = Blueprint("parent", __name__, subdomain="parent")
        child = Blueprint("child", __name__, subdomain="child")

        @child.route("/data")
        def data():
            return "ok"

        parent.register_blueprint(child)
        app.register_blueprint(parent)

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "parent.child.data")
        assert r is not None
        assert r["subdomain"] == "child.parent"

    def test_nested_with_mixed_prefix_sources(self):
        """URL prefix from register_blueprint overrides blueprint default."""
        app = _make_app(static_folder=None)

        parent = Blueprint("parent", __name__, url_prefix="/p")
        child = Blueprint("child", __name__)

        @child.route("/info")
        def info():
            return "ok"

        parent.register_blueprint(child)
        # Override parent prefix at registration time.
        app.register_blueprint(parent, url_prefix="/override")

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "parent.child.info")
        assert r is not None
        assert r["rule"] == "/override/info"


# ---------------------------------------------------------------------------
# Duplicate registration (same blueprint, different names)
# ---------------------------------------------------------------------------

class TestListRoutesDuplicateRegistration:
    """Same blueprint registered multiple times with different names."""

    def test_same_blueprint_different_names(self):
        app = _make_app(static_folder=None)
        bp = Blueprint("shared", __name__, url_prefix="/shared")

        @bp.route("/hello")
        def hello():
            return "ok"

        app.register_blueprint(bp, url_prefix="/v1", name="v1")
        app.register_blueprint(bp, url_prefix="/v2", name="v2")

        with app.app_context():
            routes = app.list_routes()

        v1 = _find_route(routes, "v1.hello")
        v2 = _find_route(routes, "v2.hello")

        assert v1 is not None
        assert v1["rule"] == "/v1/hello"
        assert v1["blueprint"] == "v1"

        assert v2 is not None
        assert v2["rule"] == "/v2/hello"
        assert v2["blueprint"] == "v2"

    def test_same_blueprint_duplicate_name_raises(self):
        """Registering the same name twice raises ValueError."""
        app = _make_app(static_folder=None)
        bp = Blueprint("dup", __name__)

        app.register_blueprint(bp)
        with pytest.raises(ValueError, match="already registered"):
            app.register_blueprint(bp)


# ---------------------------------------------------------------------------
# Host matching
# ---------------------------------------------------------------------------

class TestListRoutesHostMatching:
    """Host matching mode route inspection."""

    def test_host_constraints(self):
        app = Flask(__name__, static_folder=None, host_matching=True)
        app.add_url_rule("/a", host="alpha.example.com", endpoint="a")
        app.add_url_rule("/b", host="beta.example.com", endpoint="b")

        with app.app_context():
            routes = app.list_routes()

        a = _find_route(routes, "a")
        b = _find_route(routes, "b")

        assert a is not None
        assert a["host"] == "alpha.example.com"

        assert b is not None
        assert b["host"] == "beta.example.com"


# ---------------------------------------------------------------------------
# WebSocket routes
# ---------------------------------------------------------------------------

class TestListRoutesWebSocket:
    """WebSocket route detection."""

    def test_websocket_flag(self):
        app = _make_app(static_folder=None)
        app.add_url_rule("/ws", endpoint="ws", websocket=True)
        app.add_url_rule("/http", endpoint="http")

        with app.app_context():
            routes = app.list_routes()

        ws = _find_route(routes, "ws")
        http = _find_route(routes, "http")

        assert ws is not None
        assert ws["websocket"] is True

        assert http is not None
        assert http["websocket"] is False


# ---------------------------------------------------------------------------
# Consistency between CLI and list_routes
# ---------------------------------------------------------------------------

class TestCLIConsistency:
    """The ``flask routes`` CLI output is consistent with list_routes."""

    def _run(self, app, args):
        runner = CliRunner()
        cli = FlaskGroup(create_app=lambda: app)
        return runner.invoke(cli, args)

    def test_cli_matches_list_routes(self):
        """CLI text output should contain all endpoints from list_routes."""
        app = _make_app(static_folder=None)
        app.add_url_rule("/", endpoint="index")
        app.add_url_rule("/about", methods=["GET", "POST"], endpoint="about")

        bp = Blueprint("api", __name__, url_prefix="/api")

        @bp.route("/users")
        def users():
            return "ok"

        app.register_blueprint(bp)

        result = self._run(app, ["routes"])
        assert result.exit_code == 0

        # All endpoints from list_routes should appear in CLI output.
        with app.app_context():
            routes = app.list_routes()

        for r in routes:
            assert r["endpoint"] in result.output
            assert r["rule"] in result.output

    def test_cli_no_routes_message(self):
        app = _make_app(static_folder=None)
        result = self._run(app, ["routes"])
        assert result.exit_code == 0
        assert "No routes were registered." in result.output

    def test_cli_sort_by_endpoint(self):
        app = _make_app(static_folder=None)
        app.add_url_rule("/z", endpoint="z_endpoint")
        app.add_url_rule("/a", endpoint="a_endpoint")

        result = self._run(app, ["routes", "-s", "endpoint"])
        assert result.exit_code == 0

        lines = result.output.strip().splitlines()[2:]  # skip header
        endpoints = [line.split()[0] for line in lines]
        assert endpoints == sorted(endpoints)

    def test_cli_all_methods_flag(self):
        app = _make_app(static_folder=None)
        app.add_url_rule("/test", methods=["GET", "POST"], endpoint="test")

        # Without --all-methods: HEAD and OPTIONS should be stripped.
        result = self._run(app, ["routes"])
        assert "HEAD" not in result.output
        assert "OPTIONS" not in result.output

        # With --all-methods: HEAD and OPTIONS should appear.
        result = self._run(app, ["routes", "--all-methods"])
        assert "HEAD" in result.output
        assert "OPTIONS" in result.output

    def test_cli_host_matching_display(self):
        app = Flask(__name__, static_folder=None, host_matching=True)
        app.add_url_rule("/a", host="a.test", endpoint="a")
        app.add_url_rule("/b", host="b.test", endpoint="b")

        result = self._run(app, ["routes"])
        assert result.exit_code == 0
        assert "Host" in result.output

    def test_cli_subdomain_display(self):
        app = _make_app(static_folder=None)
        app.config["SERVER_NAME"] = "example.com"
        app.add_url_rule("/a", subdomain="a", endpoint="a")
        app.add_url_rule("/b", subdomain="b", endpoint="b")

        result = self._run(app, ["routes"])
        assert result.exit_code == 0
        assert "Subdomain" in result.output


# ---------------------------------------------------------------------------
# Test client usage (access list_routes from within a request)
# ---------------------------------------------------------------------------

class TestTestClientUsage:
    """list_routes is usable from within a request context (test client)."""

    def test_list_routes_in_request_handler(self):
        app = _make_app(static_folder=None)

        @app.route("/debug/routes")
        def debug_routes():
            routes = app.list_routes()
            return {"count": len(routes)}

        client = app.test_client()
        resp = client.get("/debug/routes")
        data = resp.get_json()
        assert data["count"] >= 1  # at least the debug_routes endpoint itself

    def test_list_routes_outside_request(self):
        """list_routes works with just an app context, no request needed."""
        app = _make_app(static_folder=None)
        app.add_url_rule("/test", endpoint="test")

        with app.app_context():
            routes = app.list_routes()

        assert _find_route(routes, "test") is not None

    def test_list_routes_from_test_client_snapshot(self):
        """Full route snapshot accessible via test client for diagnostics."""
        app = _make_app(static_folder=None)

        bp = Blueprint("api", __name__, url_prefix="/api")

        @bp.route("/items", methods=["GET", "POST"])
        def items():
            return "ok"

        app.register_blueprint(bp)

        @app.route("/snapshot")
        def snapshot():
            return {"routes": app.list_routes()}

        client = app.test_client()
        resp = client.get("/snapshot")
        data = resp.get_json()

        routes = data["routes"]
        assert len(routes) >= 2  # snapshot + api.items at minimum

        api_items = _find_route(routes, "api.items")
        assert api_items is not None
        assert api_items["blueprint"] == "api"
        assert api_items["rule"] == "/api/items"
        assert "GET" in api_items["methods"]
        assert "POST" in api_items["methods"]


# ---------------------------------------------------------------------------
# Performance: no regression
# ---------------------------------------------------------------------------

class TestPerformance:
    """Ensure list_routes and the routes CLI don't regress in performance."""

    def test_list_routes_many_routes(self):
        """list_routes handles a large number of routes efficiently."""
        app = _make_app(static_folder=None)

        # Register 200 routes across 10 blueprints.
        for i in range(10):
            bp = Blueprint(f"bp{i}", __name__, url_prefix=f"/bp{i}")
            for j in range(20):
                bp.add_url_rule(f"/route{j}", endpoint=f"route{j}")
            app.register_blueprint(bp)

        with app.app_context():
            routes = app.list_routes()

        assert len(routes) == 200

        # Verify blueprint attribution for a sample.
        r = _find_route(routes, "bp5.route10")
        assert r is not None
        assert r["blueprint"] == "bp5"
        assert r["rule"] == "/bp5/route10"

    def test_routes_cli_many_routes(self):
        """CLI routes command handles many routes without issue."""
        app = _make_app(static_folder=None)

        for i in range(50):
            app.add_url_rule(f"/route{i}", endpoint=f"route{i}")

        runner = CliRunner()
        cli = FlaskGroup(create_app=lambda: app)
        result = runner.invoke(cli, ["routes"])
        assert result.exit_code == 0

        lines = result.output.strip().splitlines()
        # Header line + separator + 50 routes = 52 lines.
        assert len(lines) == 52


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases and special scenarios."""

    def test_class_based_view(self):
        from flask.views import MethodView

        app = _make_app(static_folder=None)

        class UserAPI(MethodView):
            def get(self):
                return "ok"

            def post(self):
                return "ok"

        app.add_url_rule("/users", view_func=UserAPI.as_view("users"))

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "users")
        assert r is not None
        assert "GET" in r["methods"]
        assert "POST" in r["methods"]

    def test_endpoint_with_dots_not_blueprint(self):
        """An endpoint with dots that is NOT a blueprint route."""
        app = _make_app(static_folder=None)
        # Manually register with a dotted endpoint but no blueprint.
        app.add_url_rule("/custom", endpoint="custom.dotted.name")

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "custom.dotted.name")
        assert r is not None
        # Since "custom.dotted" and "custom" are not registered blueprints,
        # blueprint should be None.
        assert r["blueprint"] is None

    def test_register_blueprint_overrides_url_prefix(self):
        """url_prefix at registration overrides blueprint default."""
        app = _make_app(static_folder=None)
        bp = Blueprint("bp", __name__, url_prefix="/default")

        @bp.route("/test")
        def test_view():
            return "ok"

        app.register_blueprint(bp, url_prefix="/override")

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "bp.test_view")
        assert r is not None
        assert r["rule"] == "/override/test"

    def test_multiple_methods_single_rule(self):
        app = _make_app(static_folder=None)
        app.add_url_rule(
            "/multi",
            methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
            endpoint="multi",
        )

        with app.app_context():
            routes = app.list_routes()

        r = _find_route(routes, "multi")
        assert r is not None
        for method in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
            assert method in r["methods"]

    def test_list_routes_returns_dicts(self):
        """Each route entry is a plain dict (JSON-serializable friendly)."""
        app = _make_app(static_folder=None)
        app.add_url_rule("/test", endpoint="test")

        with app.app_context():
            routes = app.list_routes()

        for r in routes:
            assert isinstance(r, dict)
            assert isinstance(r["endpoint"], str)
            assert isinstance(r["methods"], list)
            assert isinstance(r["rule"], str)
            assert isinstance(r["websocket"], bool)
            assert isinstance(r["arguments"], set)

    def test_blueprint_static_route(self):
        """Blueprint with static folder produces a static route entry."""
        import tempfile
        import os

        tmpdir = tempfile.mkdtemp()
        static_dir = os.path.join(tmpdir, "static")
        os.makedirs(static_dir)

        bp = Blueprint(
            "mybp",
            __name__,
            url_prefix="/mybp",
            static_folder=static_dir,
            static_url_path="/static",
        )
        app = _make_app(static_folder=None)
        app.register_blueprint(bp)

        with app.app_context():
            routes = app.list_routes()

        static = _find_route(routes, "mybp.static")
        assert static is not None
        assert static["blueprint"] == "mybp"
        assert "/mybp/static" in static["rule"]
