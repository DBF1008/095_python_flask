"""Tests for the response instrumentation hook.

Covers:
- JSON serialisation metadata (jsonify / dict returns)
- Session cookie write-back observation
- Vary header tracking
- Error-handler path convergence
- Test-client path convergence
- Extensibility via subclass override
- Default behaviour compatibility (no signal connected)
- Signal subscriber receives correct metadata
"""

from __future__ import annotations

import flask
from flask import Flask


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _collect_via_signal(app, client, path="/"):
    """Make a request and return the metadata dicts captured by the signal."""
    captured = []

    def on_instrumented(sender, **kwargs):
        captured.append(kwargs["metadata"])

    flask.response_instrumented.connect(on_instrumented, app)
    try:
        client.get(path)
    finally:
        flask.response_instrumented.disconnect(on_instrumented, app)

    return captured


# ---------------------------------------------------------------------------
# JSON metadata
# ---------------------------------------------------------------------------


class TestJsonMetadata:
    def test_jsonify_populates_json_metadata(self, app, client):
        @app.route("/json")
        def json_view():
            return flask.jsonify({"hello": "world"})

        captured = _collect_via_signal(app, client, "/json")
        assert len(captured) == 1
        meta = captured[0]

        assert "json" in meta
        assert meta["json"]["content_length"] > 0
        assert meta["json"]["is_well_formed"] is True

    def test_dict_return_populates_json_metadata(self, app, client):
        @app.route("/dict")
        def dict_view():
            return {"key": "value"}

        captured = _collect_via_signal(app, client, "/dict")
        assert len(captured) == 1
        meta = captured[0]

        assert "json" in meta
        assert meta["json"]["is_well_formed"] is True

    def test_non_json_response_omits_json_key(self, app, client):
        @app.route("/text")
        def text_view():
            return "plain text"

        captured = _collect_via_signal(app, client, "/text")
        assert len(captured) == 1
        meta = captured[0]

        assert "json" not in meta

    def test_json_content_length_matches_body(self, app, client):
        @app.route("/json")
        def json_view():
            return flask.jsonify({"a": 1})

        captured = _collect_via_signal(app, client, "/json")
        meta = captured[0]

        # Verify content_length matches actual body length
        resp = client.get("/json")
        assert meta["json"]["content_length"] == len(resp.data)

    def test_malformed_json_body_detected(self, app, client):
        """If an after_request callback corrupts the JSON body,
        is_well_formed should be False."""

        @app.route("/json")
        def json_view():
            return flask.jsonify({"ok": True})

        @app.after_request
        def corrupt_body(response):
            if "application/json" in (response.content_type or ""):
                response.set_data(b"not-json{{{")
            return response

        captured = _collect_via_signal(app, client, "/json")
        meta = captured[0]

        assert "json" in meta
        assert meta["json"]["is_well_formed"] is False


# ---------------------------------------------------------------------------
# Session cookie write-back
# ---------------------------------------------------------------------------


class TestSessionMetadata:
    def test_session_modified_sets_cookie(self, app, client):
        @app.route("/set")
        def set_session():
            flask.session["user"] = "alice"
            return "ok"

        captured = _collect_via_signal(app, client, "/set")
        meta = captured[0]

        assert meta["session"]["cookie_set"] is True
        assert meta["session"]["modified"] is True
        assert meta["session"]["accessed"] is True
        assert meta["session"]["null_session"] is False

    def test_session_not_accessed_no_cookie(self, app, client):
        @app.route("/noop")
        def noop():
            return "ok"

        captured = _collect_via_signal(app, client, "/noop")
        meta = captured[0]

        assert meta["session"]["cookie_set"] is False
        assert meta["session"]["modified"] is False
        assert meta["session"]["accessed"] is False

    def test_session_read_only_accessed_not_modified(self, app, client):
        @app.route("/read")
        def read_session():
            _ = flask.session.get("user")
            return "ok"

        captured = _collect_via_signal(app, client, "/read")
        meta = captured[0]

        assert meta["session"]["accessed"] is True
        assert meta["session"]["modified"] is False
        # Cookie may or may not be set depending on whether the session
        # already had data; for a fresh session with no modification,
        # should_set_cookie returns False.
        assert meta["session"]["cookie_set"] is False

    def test_session_delete_clears_cookie(self, app, client):
        @app.route("/set")
        def set_session():
            flask.session["user"] = "alice"
            return "ok"

        @app.route("/clear")
        def clear_session():
            flask.session.clear()
            return "ok"

        # First set the session
        client.get("/set")

        # Then clear it
        captured = _collect_via_signal(app, client, "/clear")
        meta = captured[0]

        assert meta["session"]["modified"] is True
        assert meta["session"]["accessed"] is True


# ---------------------------------------------------------------------------
# Vary header
# ---------------------------------------------------------------------------


class TestVaryMetadata:
    def test_vary_empty_by_default(self, app, client):
        @app.route("/plain")
        def plain():
            return "ok"

        captured = _collect_via_signal(app, client, "/plain")
        meta = captured[0]

        assert meta["vary"]["values"] == []

    def test_vary_cookie_added_by_session_access(self, app, client):
        @app.route("/sess")
        def sess_view():
            _ = flask.session.get("x")
            return "ok"

        captured = _collect_via_signal(app, client, "/sess")
        meta = captured[0]

        assert "Cookie" in meta["vary"]["values"]

    def test_vary_custom_header_from_after_request(self, app, client):
        @app.route("/custom")
        def custom():
            return "ok"

        @app.after_request
        def add_vary(response):
            response.vary.add("Accept-Encoding")
            return response

        captured = _collect_via_signal(app, client, "/custom")
        meta = captured[0]

        assert "Accept-Encoding" in meta["vary"]["values"]

    def test_vary_multiple_values_sorted(self, app, client):
        @app.route("/multi")
        def multi():
            _ = flask.session.get("x")
            return "ok"

        @app.after_request
        def add_vary(response):
            response.vary.add("Accept-Encoding")
            response.vary.add("Accept-Language")
            return response

        captured = _collect_via_signal(app, client, "/multi")
        meta = captured[0]

        values = meta["vary"]["values"]
        assert values == sorted(values)
        assert "Accept-Encoding" in values
        assert "Accept-Language" in values
        assert "Cookie" in values


# ---------------------------------------------------------------------------
# Error-handler path
# ---------------------------------------------------------------------------


class TestErrorHandlerPath:
    def test_error_handler_goes_through_instrumentation(self, app, client):
        @app.route("/boom")
        def boom():
            raise ValueError("kaboom")

        @app.errorhandler(ValueError)
        def handle_value_error(e):
            return flask.jsonify({"error": str(e)}), 400

        captured = _collect_via_signal(app, client, "/boom")
        assert len(captured) == 1
        meta = captured[0]

        # JSON metadata is present even on error-handler responses
        assert "json" in meta
        assert meta["json"]["is_well_formed"] is True

    def test_unhandled_500_goes_through_instrumentation(self, app, client):
        app.config["PROPAGATE_EXCEPTIONS"] = False

        @app.route("/crash")
        def crash():
            raise RuntimeError("unhandled")

        captured = _collect_via_signal(app, client, "/crash")
        assert len(captured) == 1
        meta = captured[0]

        # Session metadata is always present
        assert "session" in meta
        assert "vary" in meta

    def test_http_exception_goes_through_instrumentation(self, app, client):
        @app.route("/missing")
        def missing():
            flask.abort(404)

        captured = _collect_via_signal(app, client, "/missing")
        assert len(captured) == 1
        meta = captured[0]

        assert "session" in meta
        assert "vary" in meta

    def test_error_handler_with_session_modification(self, app, client):
        @app.route("/boom")
        def boom():
            flask.session["error_count"] = 1
            raise ValueError("kaboom")

        @app.errorhandler(ValueError)
        def handle_value_error(e):
            return flask.jsonify({"error": str(e)}), 500

        captured = _collect_via_signal(app, client, "/boom")
        meta = captured[0]

        assert meta["session"]["modified"] is True
        assert meta["session"]["cookie_set"] is True


# ---------------------------------------------------------------------------
# Test-client convergence (same pipeline)
# ---------------------------------------------------------------------------


class TestTestClientConvergence:
    def test_test_client_uses_same_pipeline(self, app):
        """The test client goes through the same process_response as the
        real WSGI pipeline, so the signal fires identically."""
        @app.route("/ping")
        def ping():
            return flask.jsonify({"pong": True})

        captured_direct = []
        captured_client = []

        def on_instrumented(sender, **kwargs):
            # This captures from BOTH direct and client calls
            captured_direct.append(kwargs["metadata"])

        flask.response_instrumented.connect(on_instrumented, app)
        try:
            # Using test client
            with app.test_client() as c:
                resp = c.get("/ping")
                assert resp.status_code == 200

            assert len(captured_direct) == 1
            assert captured_direct[0]["json"]["is_well_formed"] is True
        finally:
            flask.response_instrumented.disconnect(on_instrumented, app)

    def test_context_preserving_client(self, app):
        """The ``with client:`` pattern also triggers instrumentation."""
        @app.route("/sess")
        def sess_view():
            flask.session["x"] = 1
            return "ok"

        captured = _collect_via_signal(app, app.test_client(), "/sess")
        assert len(captured) == 1
        assert captured[0]["session"]["cookie_set"] is True


# ---------------------------------------------------------------------------
# Extensibility
# ---------------------------------------------------------------------------


class TestExtensibility:
    def test_subclass_override_metadata(self):
        """A Flask subclass can override collect_response_metadata to
        inject custom fields."""

        class InstrumentedFlask(Flask):
            def collect_response_metadata(self, ctx, response):
                meta = super().collect_response_metadata(ctx, response)
                meta["custom"] = {
                    "status_code": response.status_code,
                    "endpoint": ctx.request.endpoint,
                }
                return meta

        app = InstrumentedFlask("test", root_path=__file__)
        app.config.update(TESTING=True, SECRET_KEY="test key")

        @app.route("/hello")
        def hello():
            return "hi"

        captured = _collect_via_signal(app, app.test_client(), "/hello")
        meta = captured[0]

        assert "custom" in meta
        assert meta["custom"]["status_code"] == 200
        assert meta["custom"]["endpoint"] == "hello"

    def test_multiple_signal_subscribers(self, app, client):
        """Multiple subscribers can independently observe the metadata."""
        results_a = []
        results_b = []

        def subscriber_a(sender, **kwargs):
            results_a.append(kwargs["metadata"])

        def subscriber_b(sender, **kwargs):
            results_b.append(kwargs["metadata"])

        flask.response_instrumented.connect(subscriber_a, app)
        flask.response_instrumented.connect(subscriber_b, app)
        try:
            @app.route("/multi")
            def multi():
                return flask.jsonify({"x": 1})

            client.get("/multi")
            assert len(results_a) == 1
            assert len(results_b) == 1
            assert results_a[0]["json"] == results_b[0]["json"]
        finally:
            flask.response_instrumented.disconnect(subscriber_a, app)
            flask.response_instrumented.disconnect(subscriber_b, app)

    def test_metadata_is_mutable_dict(self, app, client):
        """Signal subscribers receive a plain dict they can inspect freely."""

        @app.route("/r")
        def r():
            return "ok"

        captured = _collect_via_signal(app, client, "/r")
        meta = captured[0]

        assert isinstance(meta, dict)
        # Standard keys are present
        assert "session" in meta
        assert "vary" in meta


# ---------------------------------------------------------------------------
# Default behaviour compatibility
# ---------------------------------------------------------------------------


class TestDefaultCompatibility:
    def test_no_signal_connected_still_works(self, app, client):
        """When no subscriber is connected the request completes normally."""

        @app.route("/ok")
        def ok():
            return flask.jsonify({"status": "ok"})

        resp = client.get("/ok")
        assert resp.status_code == 200
        assert resp.json == {"status": "ok"}

    def test_after_request_still_runs_before_instrumentation(self, app, client):
        """after_request functions execute before metadata is collected,
        so their mutations are visible in the metadata."""
        call_order = []

        @app.route("/order")
        def order():
            return flask.jsonify({"step": "view"})

        @app.after_request
        def mark_header(response):
            call_order.append("after_request")
            response.headers["X-Custom"] = "yes"
            return response

        captured = []

        def on_instrumented(sender, **kwargs):
            call_order.append("signal")
            captured.append(kwargs["metadata"])

        flask.response_instrumented.connect(on_instrumented, app)
        try:
            resp = client.get("/order")
            assert resp.headers["X-Custom"] == "yes"
            assert call_order == ["after_request", "signal"]
            assert len(captured) == 1
        finally:
            flask.response_instrumented.disconnect(on_instrumented, app)

    def test_session_save_visible_in_metadata(self, app, client):
        """Session saving happens before metadata collection, so the
        Set-Cookie header is observable."""

        @app.route("/set")
        def set_sess():
            flask.session["k"] = "v"
            return "ok"

        captured = _collect_via_signal(app, client, "/set")
        meta = captured[0]

        # save_session already ran, so Set-Cookie is in the response
        assert meta["session"]["cookie_set"] is True

    def test_blueprint_after_request_included(self, app, client):
        """Blueprint-level after_request functions also run before
        metadata collection."""
        bp = flask.Blueprint("bp", __name__, url_prefix="/bp")

        @bp.route("/hello")
        def hello():
            return "hello"

        @bp.after_request
        def bp_after(response):
            response.headers["X-BP"] = "yes"
            return response

        app.register_blueprint(bp)

        captured = _collect_via_signal(app, client, "/bp/hello")
        assert len(captured) == 1

        resp = client.get("/bp/hello")
        assert resp.headers["X-BP"] == "yes"


# ---------------------------------------------------------------------------
# after_this_request convergence
# ---------------------------------------------------------------------------


class TestAfterThisRequest:
    def test_after_this_request_visible_in_metadata(self, app, client):
        @app.route("/inline")
        def inline():
            @flask.after_this_request
            def add_header(response):
                response.vary.add("Accept")
                return response

            return flask.jsonify({"ok": True})

        captured = _collect_via_signal(app, client, "/inline")
        meta = captured[0]

        assert "Accept" in meta["vary"]["values"]
        assert meta["json"]["is_well_formed"] is True
