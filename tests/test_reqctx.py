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


def test_copy_session_isolation(
    request: pytest.FixtureRequest, app: flask.Flask, client: FlaskClient
) -> None:
    """Child session modifications must not leak to the parent's session
    or affect the parent response's Set-Cookie header.
    """
    executor = futures.ThreadPoolExecutor(max_workers=1)
    request.addfinalizer(lambda: executor.shutdown(cancel_futures=True))

    @app.route("/")
    def index():
        flask.session["parent_key"] = "parent_value"

        @flask.copy_current_request_context
        def work():
            # Child reads parent's data.
            assert flask.session["parent_key"] == "parent_value"
            # Child modifies its own session snapshot.
            flask.session["child_key"] = "child_value"
            return "done"

        future = executor.submit(work)
        future.result(timeout=5)
        return "OK"

    rv = client.get("/")
    assert rv.text == "OK"

    # Parent response should NOT contain the child's session modification.
    with client.session_transaction() as sess:
        assert sess.get("parent_key") == "parent_value"
        assert "child_key" not in sess


def test_copy_session_vary_cookie_stable(
    request: pytest.FixtureRequest, app: flask.Flask, client: FlaskClient
) -> None:
    """Vary: Cookie must be set when the parent accesses the session,
    regardless of whether the child also accesses it.
    """
    executor = futures.ThreadPoolExecutor(max_workers=1)
    request.addfinalizer(lambda: executor.shutdown(cancel_futures=True))

    @app.route("/")
    def index():
        # Parent accesses session (sets accessed=True on parent's session).
        _ = flask.session.get("anything")

        @flask.copy_current_request_context
        def work():
            # Child also accesses session (sets accessed=True on child's snapshot).
            flask.session.get("something")
            return "done"

        future = executor.submit(work)
        future.result(timeout=5)
        return "OK"

    rv = client.get("/")
    assert "Cookie" in rv.headers.get("Vary", "")


def test_copy_g_isolation(
    request: pytest.FixtureRequest, app: flask.Flask, client: FlaskClient
) -> None:
    """Child g starts with a snapshot of parent's g values. Child modifications
    do not affect the parent's g.
    """
    executor = futures.ThreadPoolExecutor(max_workers=1)
    request.addfinalizer(lambda: executor.shutdown(cancel_futures=True))
    parent_g_after = {}

    @app.route("/")
    def index():
        flask.g.parent_data = "from_parent"

        @flask.copy_current_request_context
        def work():
            # Child can read parent's g.
            assert flask.g.parent_data == "from_parent"
            # Child modifies its own g.
            flask.g.child_data = "from_child"
            # Child's override does not propagate to parent.
            flask.g.parent_data = "overridden_by_child"
            return "done"

        future = executor.submit(work)
        future.result(timeout=5)

        # Parent g is unaffected by child's writes.
        parent_g_after["parent_data"] = flask.g.parent_data
        parent_g_after["has_child_data"] = hasattr(flask.g, "child_data")
        return "OK"

    rv = client.get("/")
    assert rv.text == "OK"
    assert parent_g_after["parent_data"] == "from_parent"
    assert parent_g_after["has_child_data"] is False


def test_copy_teardown_runs_once(
    request: pytest.FixtureRequest, app: flask.Flask, client: FlaskClient
) -> None:
    """Teardown callbacks must only fire once — when the parent context is
    popped — not when the child's copied context is popped.
    """
    executor = futures.ThreadPoolExecutor(max_workers=2)
    request.addfinalizer(lambda: executor.shutdown(cancel_futures=True))
    teardown_calls: list = []
    app_teardown_calls: list = []

    @app.teardown_request
    def on_teardown(exc):
        teardown_calls.append(exc)

    @app.teardown_appcontext
    def on_app_teardown(exc):
        app_teardown_calls.append(exc)

    @app.route("/")
    def index():
        @flask.copy_current_request_context
        def work(n):
            # The child context is pushed and will be popped here.
            assert flask.request.path == "/"
            return n

        results = list(executor.map(work, range(5)))
        assert results == list(range(5))
        return "OK"

    rv = client.get("/")
    assert rv.text == "OK"

    # teardown_request fires exactly once, not 1 + 5 times.
    assert len(teardown_calls) == 1
    assert len(app_teardown_calls) == 1


def test_copy_request_body_cached(
    request: pytest.FixtureRequest, app: flask.Flask, client: FlaskClient
) -> None:
    """The request body must be pre-cached so the child thread can safely
    read it without racing on the WSGI input stream.
    """
    executor = futures.ThreadPoolExecutor(max_workers=1)
    request.addfinalizer(lambda: executor.shutdown(cancel_futures=True))

    @app.route("/", methods=["POST"])
    def index():
        # Parent does NOT read the body first.

        @flask.copy_current_request_context
        def work():
            # Child reads the body — should work because the wrapper
            # pre-cached it before the child thread started.
            return flask.request.get_data(as_text=True)

        future = executor.submit(work)
        return future.result(timeout=5)

    rv = client.post("/", data="hello world")
    assert rv.text == "hello world"


def test_copy_multiple_threads_no_cross_contamination(
    request: pytest.FixtureRequest, app: flask.Flask, client: FlaskClient
) -> None:
    """Multiple concurrent copied contexts must each see the correct parent
    data without any cross-contamination between threads.
    """
    executor = futures.ThreadPoolExecutor(max_workers=5)
    request.addfinalizer(lambda: executor.shutdown(cancel_futures=True))

    @app.route("/")
    def index():
        flask.session["shared"] = "original"
        flask.g.shared = "original"

        @flask.copy_current_request_context
        def work(n):
            # Each task sees the parent's snapshot.
            assert flask.session["shared"] == "original"
            assert flask.g.shared == "original"

            # Each task modifies its own copy.
            flask.session["shared"] = f"child_{n}"
            flask.g.shared = f"child_{n}"

            # Modifications are visible within this task.
            assert flask.session["shared"] == f"child_{n}"
            assert flask.g.shared == f"child_{n}"
            return n

        results = list(executor.map(work, range(20)))
        assert sorted(results) == list(range(20))

        # Parent session and g are unaffected by any child modifications.
        assert flask.session["shared"] == "original"
        assert flask.g.shared == "original"
        return "OK"

    rv = client.get("/")
    assert rv.text == "OK"


def test_copy_parent_session_dirty_state_isolated(
    request: pytest.FixtureRequest, app: flask.Flask, client: FlaskClient
) -> None:
    """Multiple child modifications must not accumulate on the parent session,
    even when the child reads, writes, and deletes session keys.
    """
    executor = futures.ThreadPoolExecutor(max_workers=1)
    request.addfinalizer(lambda: executor.shutdown(cancel_futures=True))

    @app.route("/")
    def index():
        flask.session["keep"] = "keep_value"
        flask.session["to_delete"] = "will_be_deleted"

        @flask.copy_current_request_context
        def work():
            # Child reads parent data.
            assert flask.session["keep"] == "keep_value"
            assert flask.session["to_delete"] == "will_be_deleted"

            # Child modifies extensively.
            flask.session["keep"] = "changed_by_child"
            del flask.session["to_delete"]
            flask.session["new_from_child"] = "child_data"
            flask.session["another_child_key"] = "another_value"

            return "done"

        future = executor.submit(work)
        future.result(timeout=5)
        return "OK"

    rv = client.get("/")
    assert rv.text == "OK"

    # Parent session is completely unaffected by child's modifications.
    with client.session_transaction() as sess:
        assert sess.get("keep") == "keep_value"
        assert sess.get("to_delete") == "will_be_deleted"
        assert "new_from_child" not in sess
        assert "another_child_key" not in sess
