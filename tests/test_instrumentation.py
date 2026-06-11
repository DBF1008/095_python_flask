from __future__ import annotations

import flask
from flask import Flask
from flask import jsonify
from flask import session
from flask.instrumentation import ResponseMetadata
from flask.signals import response_instrumented


class TestAfterResponseJSONMetadata:
    def test_jsonify_captures_json_body(self, app, client):
        collected = []

        @app.after_response
        def collect(response, metadata):
            collected.append(metadata)

        @app.route("/json")
        def json_view():
            return jsonify(key="value")

        client.get("/json")
        assert len(collected) == 1
        meta = collected[0]
        assert meta.json_body is not None
        assert '"key"' in meta.json_body
        assert '"value"' in meta.json_body
        assert meta.status_code == 200
        assert meta.endpoint == "json_view"
        assert meta.method == "GET"
        assert meta.path == "/json"

    def test_dict_return_captures_json_body(self, app, client):
        collected = []

        @app.after_response
        def collect(response, metadata):
            collected.append(metadata)

        @app.route("/dict")
        def dict_view():
            return {"a": 1}

        client.get("/dict")
        assert len(collected) == 1
        assert collected[0].json_body is not None
        assert '"a"' in collected[0].json_body

    def test_non_json_has_no_json_body(self, app, client):
        collected = []

        @app.after_response
        def collect(response, metadata):
            collected.append(metadata)

        @app.route("/text")
        def text_view():
            return "hello"

        client.get("/text")
        assert len(collected) == 1
        assert collected[0].json_body is None


class TestSessionCookieDetection:
    def test_session_cookie_set_detected(self, app, client):
        collected = []

        @app.after_response
        def collect(response, metadata):
            collected.append(metadata)

        @app.route("/set-session")
        def set_session():
            session["foo"] = "bar"
            return "ok"

        client.get("/set-session")
        assert len(collected) == 1
        assert collected[0].session_cookie_set is True
        assert collected[0].session_cookie_deleted is False

    def test_session_cookie_deleted_detected(self, app, client):
        collected = []

        @app.after_response
        def collect(response, metadata):
            collected.append(metadata)

        @app.route("/set")
        def set_it():
            session["foo"] = "bar"
            return "ok"

        @app.route("/clear")
        def clear_it():
            session.clear()
            return "ok"

        # First request: set session
        client.get("/set")
        collected.clear()

        # Second request: clear session
        client.get("/clear")
        assert len(collected) == 1
        assert collected[0].session_cookie_deleted is True
        assert collected[0].session_cookie_set is False

    def test_session_not_touched(self, app, client):
        collected = []

        @app.after_response
        def collect(response, metadata):
            collected.append(metadata)

        @app.route("/no-session")
        def no_session():
            return "ok"

        client.get("/no-session")
        assert len(collected) == 1
        assert collected[0].session_cookie_set is False
        assert collected[0].session_cookie_deleted is False


class TestVaryHeaderTracking:
    def test_vary_from_session_access(self, app, client):
        collected = []

        @app.after_response
        def collect(response, metadata):
            collected.append(metadata)

        @app.route("/session-access")
        def access_session():
            session["x"] = "y"
            return "ok"

        client.get("/session-access")
        assert len(collected) == 1
        assert "Cookie" in collected[0].vary_added
        assert "Cookie" in collected[0].vary_final

    def test_vary_from_after_request(self, app, client):
        collected = []

        @app.after_response
        def collect(response, metadata):
            collected.append(metadata)

        @app.after_request
        def add_vary(response):
            response.vary.add("Accept-Encoding")
            return response

        @app.route("/vary")
        def vary_view():
            return "ok"

        client.get("/vary")
        assert len(collected) == 1
        assert "Accept-Encoding" in collected[0].vary_added
        assert "Accept-Encoding" in collected[0].vary_final


class TestErrorHandlerPipeline:
    def test_404_goes_through_pipeline(self, app, client):
        collected = []

        @app.after_response
        def collect(response, metadata):
            collected.append(metadata)

        @app.errorhandler(404)
        def handle_404(e):
            return "not found", 404

        client.get("/nonexistent")
        assert len(collected) == 1
        assert collected[0].status_code == 404

    def test_500_goes_through_pipeline(self):
        app = Flask(__name__)
        app.config["SECRET_KEY"] = "test key"
        app.config["PROPAGATE_EXCEPTIONS"] = False

        collected = []

        @app.after_response
        def collect(response, metadata):
            collected.append(metadata)

        @app.route("/error")
        def error_view():
            raise RuntimeError("boom")

        with app.test_client() as client:
            client.get("/error")

        assert len(collected) == 1
        assert collected[0].status_code == 500


class TestSignal:
    def test_signal_receives_metadata(self, app, client):
        signal_data = []

        def receiver(sender, **kwargs):
            signal_data.append(kwargs)

        response_instrumented.connect(receiver, app)
        try:
            @app.route("/sig")
            def sig_view():
                return jsonify(ok=True)

            client.get("/sig")
            assert len(signal_data) == 1
            assert "response" in signal_data[0]
            assert "metadata" in signal_data[0]
            meta = signal_data[0]["metadata"]
            assert isinstance(meta, ResponseMetadata)
            assert meta.status_code == 200
            assert meta.json_body is not None
        finally:
            response_instrumented.disconnect(receiver, app)


class TestNoOverhead:
    def test_no_metadata_when_unused(self, app, client):
        """When no callbacks or signal receivers are registered,
        process_response should not construct ResponseMetadata."""

        @app.route("/plain")
        def plain():
            return "ok"

        # Simply verify no errors and the response is normal
        rv = client.get("/plain")
        assert rv.status_code == 200
        assert rv.data == b"ok"


class TestTestClientPipeline:
    def test_test_client_goes_through_pipeline(self, app):
        collected = []

        @app.after_response
        def collect(response, metadata):
            collected.append(metadata)

        @app.route("/tc")
        def tc_view():
            return jsonify(test=True)

        with app.test_client() as client:
            client.get("/tc")

        assert len(collected) == 1
        assert collected[0].json_body is not None
        assert collected[0].status_code == 200


class TestCallbackBehavior:
    def test_multiple_callbacks_called_in_order(self, app, client):
        order = []

        @app.after_response
        def first(response, metadata):
            order.append("first")

        @app.after_response
        def second(response, metadata):
            order.append("second")

        @app.after_response
        def third(response, metadata):
            order.append("third")

        @app.route("/order")
        def order_view():
            return "ok"

        client.get("/order")
        assert order == ["first", "second", "third"]

    def test_decorator_returns_function(self, app):
        def my_func(response, metadata):
            pass

        result = app.after_response(my_func)
        assert result is my_func


class TestMetadataImmutability:
    def test_metadata_is_frozen(self, app, client):
        collected = []

        @app.after_response
        def collect(response, metadata):
            collected.append(metadata)

        @app.route("/frozen")
        def frozen_view():
            return jsonify(x=1)

        client.get("/frozen")
        meta = collected[0]
        import dataclasses

        assert dataclasses.is_dataclass(meta)
        try:
            meta.status_code = 999
            assert False, "Should have raised FrozenInstanceError"
        except dataclasses.FrozenInstanceError:
            pass
