from __future__ import annotations

import collections.abc as cabc
import warnings
from concurrent import futures

import pytest

import flask
from flask.sessions import SecureCookieSessionInterface
from flask.sessions import SessionInterface
from flask.testing import FlaskClient


def test_teardown_on_pop(app):
    buffer = []

    @app.teardown_request
    def end_of_request(exception):
        buffer.append(exception)

    ctx = app.test_request_context()
    ctx.push()
    assert buffer == []
    ctx.pop()
    assert buffer == [None]


def test_teardown_with_previous_exception(app):
    buffer = []

    @app.teardown_request
    def end_of_request(exception):
        buffer.append(exception)

    try:
        raise Exception("dummy")
    except Exception:
        pass

    with app.test_request_context():
        assert buffer == []
    assert buffer == [None]


def test_teardown_with_handled_exception(app):
    buffer = []

    @app.teardown_request
    def end_of_request(exception):
        buffer.append(exception)

    with app.test_request_context():
        assert buffer == []
        try:
            raise Exception("dummy")
        except Exception:
            pass
    assert buffer == [None]


def test_proper_test_request_context(app):
    app.config.update(SERVER_NAME="localhost.localdomain:5000")

    @app.route("/")
    def index():
        return None

    @app.route("/", subdomain="foo")
    def sub():
        return None

    with app.test_request_context("/"):
        assert (
            flask.url_for("index", _external=True)
            == "http://localhost.localdomain:5000/"
        )

    with app.test_request_context("/"):
        assert (
            flask.url_for("sub", _external=True)
            == "http://foo.localhost.localdomain:5000/"
        )

    # suppress Werkzeug 0.15 warning about name mismatch
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", "Current server name", UserWarning, "flask.app"
        )
        with app.test_request_context(
            "/", environ_overrides={"HTTP_HOST": "localhost"}
        ):
            pass

    app.config.update(SERVER_NAME="localhost")
    with app.test_request_context("/", environ_overrides={"SERVER_NAME": "localhost"}):
        pass

    app.config.update(SERVER_NAME="localhost:80")
    with app.test_request_context(
        "/", environ_overrides={"SERVER_NAME": "localhost:80"}
    ):
        pass


def test_context_binding(app):
    @app.route("/")
    def index():
        return f"Hello {flask.request.args['name']}!"

    @app.route("/meh")
    def meh():
        return flask.request.url

    with app.test_request_context("/?name=World"):
        assert index() == "Hello World!"
    with app.test_request_context("/meh"):
        assert meh() == "http://localhost/meh"
    assert not flask.request


def test_context_test(app):
    assert not flask.request
    assert not flask.has_request_context()
    ctx = app.test_request_context()
    ctx.push()
    try:
        assert flask.request
        assert flask.has_request_context()
    finally:
        ctx.pop()


def test_manual_context_binding(app):
    @app.route("/")
    def index():
        return f"Hello {flask.request.args['name']}!"

    ctx = app.test_request_context("/?name=World")
    ctx.push()
    assert index() == "Hello World!"
    ctx.pop()
    with pytest.raises(RuntimeError):
        index()


def test_copy_context_thread(
    request: pytest.FixtureRequest, app: flask.Flask, client: FlaskClient
) -> None:
    executor = futures.ThreadPoolExecutor(max_workers=2)
    request.addfinalizer(lambda: executor.shutdown(cancel_futures=True))
    result: cabc.Iterator[int] | None = None

    @app.route("/")
    def index():
        flask.session["fizz"] = "buzz"

        @flask.copy_current_request_context
        def work(n: int) -> int:
            assert flask.current_app == app
            assert flask.request.path == "/"
            assert flask.request.args["foo"] == "bar"
            assert flask.session["fizz"] == "buzz"
            return n

        nonlocal result
        result = executor.map(work, range(10))
        return "Hello World!"

    rv = client.get(query_string={"foo": "bar"})
    assert rv.text == "Hello World!"

    assert result is not None
    assert set(result) == set(range(10))


def test_session_error_pops_context():
    class SessionError(Exception):
        pass

    class FailingSessionInterface(SessionInterface):
        def open_session(self, app, request):
            raise SessionError()

    class CustomFlask(flask.Flask):
        session_interface = FailingSessionInterface()

    app = CustomFlask(__name__)

    @app.route("/")
    def index():
        # shouldn't get here
        AssertionError()

    response = app.test_client().get("/")
    assert response.status_code == 500
    assert not flask.request
    assert not flask.current_app


def test_session_dynamic_cookie_name():
    # This session interface will use a cookie with a different name if the
    # requested url ends with the string "dynamic_cookie"
    class PathAwareSessionInterface(SecureCookieSessionInterface):
        def get_cookie_name(self, app):
            if flask.request.url.endswith("dynamic_cookie"):
                return "dynamic_cookie_name"
            else:
                return super().get_cookie_name(app)

    class CustomFlask(flask.Flask):
        session_interface = PathAwareSessionInterface()

    app = CustomFlask(__name__)
    app.secret_key = "secret_key"

    @app.route("/set", methods=["POST"])
    def set():
        flask.session["value"] = flask.request.form["value"]
        return "value set"

    @app.route("/get")
    def get():
        v = flask.session.get("value", "None")
        return v

    @app.route("/set_dynamic_cookie", methods=["POST"])
    def set_dynamic_cookie():
        flask.session["value"] = flask.request.form["value"]
        return "value set"

    @app.route("/get_dynamic_cookie")
    def get_dynamic_cookie():
        v = flask.session.get("value", "None")
        return v

    test_client = app.test_client()

    # first set the cookie in both /set urls but each with a different value
    assert test_client.post("/set", data={"value": "42"}).data == b"value set"
    assert (
        test_client.post("/set_dynamic_cookie", data={"value": "616"}).data
        == b"value set"
    )

    # now check that the relevant values come back - meaning that different
    # cookies are being used for the urls that end with "dynamic cookie"
    assert test_client.get("/get").data == b"42"
    assert test_client.get("/get_dynamic_cookie").data == b"616"


class TestCopyContextIsolation:
    """Tests for copy_current_request_context ensuring proper isolation
    between parent and child contexts across threads."""

    def test_session_snapshot_isolation(self, app, client):
        """Modifying session in a child task must not affect the parent."""
        parent_session_after = {}

        @app.route("/")
        def index():
            flask.session["key"] = "parent_value"

            @flask.copy_current_request_context
            def child():
                # Child sees the snapshot of the parent's session data.
                assert flask.session["key"] == "parent_value"
                # Mutating in the child must not leak back.
                flask.session["key"] = "child_value"
                flask.session["child_only"] = True

            executor = futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(child)
            future.result()
            executor.shutdown()

            # Parent session is unaffected.
            parent_session_after["key"] = flask.session["key"]
            parent_session_after["has_child_only"] = "child_only" in flask.session
            return "ok"

        client.get("/")
        assert parent_session_after["key"] == "parent_value"
        assert parent_session_after["has_child_only"] is False

    def test_vary_cookie_always_set(self, app, client):
        """Vary: Cookie must appear on the response even if only the child
        accesses session, because copy_current_request_context eagerly
        accesses the parent session."""

        @app.route("/")
        def index():
            # Deliberately do NOT access session here in the view body.
            @flask.copy_current_request_context
            def child():
                _ = flask.session.get("anything")

            executor = futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(child)
            future.result()
            executor.shutdown()
            return "ok"

        app.secret_key = "test-secret"
        rv = client.get("/")
        # The decorator eagerly touched the parent session, so Vary is set.
        assert "Cookie" in rv.headers.get("Vary", "")

    def test_g_isolation(self, app, client):
        """Each copied context must have its own independent g object."""
        results = {}

        @app.route("/")
        def index():
            flask.g.parent_val = "parent"

            @flask.copy_current_request_context
            def child():
                # Child gets a fresh g — parent_val should not exist.
                assert not hasattr(flask.g, "parent_val")
                flask.g.child_val = "child"

            executor = futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(child)
            future.result()
            executor.shutdown()

            results["parent_val"] = flask.g.parent_val
            results["has_child_val"] = hasattr(flask.g, "child_val")
            return "ok"

        client.get("/")
        assert results["parent_val"] == "parent"
        assert results["has_child_val"] is False

    def test_teardown_runs_in_child(self, app, client):
        """Teardown functions must fire in both parent and child contexts."""
        teardown_log = []

        @app.teardown_request
        def log_teardown(exc):
            import threading

            teardown_log.append(threading.current_thread().name)

        @app.route("/")
        def index():
            @flask.copy_current_request_context
            def child():
                pass  # Teardown fires when the copied context pops.

            executor = futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(child)
            future.result()
            executor.shutdown()
            return "ok"

        client.get("/")
        # One teardown from the child thread, one from the main thread.
        assert len(teardown_log) == 2

    def test_request_close_skipped_in_child(self, app, client):
        """request.close() must only be called by the original context,
        not by the copied child context."""
        close_count = 0

        class TrackingRequest(flask.Request):
            def close(self):
                nonlocal close_count
                close_count += 1
                super().close()

        app.request_class = TrackingRequest

        @app.route("/")
        def index():
            @flask.copy_current_request_context
            def child():
                # Just access request to prove the context is alive.
                _ = flask.request.path

            executor = futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(child)
            future.result()
            executor.shutdown()
            return "ok"

        client.get("/")
        # close() should have been called exactly once (by the parent).
        assert close_count == 1

    def test_no_cross_request_leaking_threaded(self, app, client):
        """Concurrent requests using copy_current_request_context must not
        leak state across request boundaries."""
        import threading

        barrier = threading.Barrier(2, timeout=5)

        @app.route("/<label>")
        def index(label):
            flask.session["label"] = label
            flask.g.label = label

            @flask.copy_current_request_context
            def child():
                barrier.wait()  # Force both children to run concurrently.
                return {
                    "session_label": flask.session["label"],
                    "request_path": flask.request.path,
                }

            executor = futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(child)
            result = future.result()
            executor.shutdown()
            return flask.jsonify(result)

        app.secret_key = "test-secret"

        results = {}

        def do_request(label):
            with app.test_client() as c:
                rv = c.get(f"/{label}")
                results[label] = rv.get_json()

        t1 = threading.Thread(target=do_request, args=("alpha",))
        t2 = threading.Thread(target=do_request, args=("beta",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert results["alpha"]["session_label"] == "alpha"
        assert results["alpha"]["request_path"] == "/alpha"
        assert results["beta"]["session_label"] == "beta"
        assert results["beta"]["request_path"] == "/beta"

    def test_body_cached_before_copy(self, app, client):
        """The request body must be cached before the context is copied,
        so child tasks can read form/json/data without stream races."""

        @app.route("/", methods=["POST"])
        def index():
            @flask.copy_current_request_context
            def child():
                # Should be able to read the body even though the parent
                # already consumed the stream (it was cached).
                return flask.request.get_json()

            executor = futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(child)
            result = future.result()
            executor.shutdown()
            return flask.jsonify(result)

        rv = client.post(
            "/",
            json={"msg": "hello"},
        )
        assert rv.get_json() == {"msg": "hello"}

    def test_copy_is_copy_flag(self, app):
        """The _is_copy flag must be set on copied contexts only."""
        with app.test_request_context("/"):
            from flask.globals import _cv_app

            original = _cv_app.get()
            assert original._is_copy is False

            copy = original.copy()
            assert copy._is_copy is True

    def test_multiple_children_independent_sessions(self, app, client):
        """Multiple children from the same parent must each get their own
        independent session snapshot."""

        @app.route("/")
        def index():
            flask.session["counter"] = 0

            results = []

            for i in range(3):

                @flask.copy_current_request_context
                def child(n=i):
                    flask.session["counter"] += 1
                    return flask.session["counter"]

                executor = futures.ThreadPoolExecutor(max_workers=1)
                future = executor.submit(child)
                results.append(future.result())
                executor.shutdown()

            # Each child starts from the same snapshot (counter=0),
            # so each independently increments to 1.
            return flask.jsonify(
                {"results": results, "parent": flask.session["counter"]}
            )

        app.secret_key = "test-secret"
        rv = client.get("/")
        data = rv.get_json()
        assert data["results"] == [1, 1, 1]
        assert data["parent"] == 0
