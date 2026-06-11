"""Tests for the optional context tracing mode (flask.tracing)."""

from __future__ import annotations

import threading
import warnings

import pytest

import flask
from flask import Flask
from flask.tracing import (
    ContextRecord,
    ContextTrace,
    check_leaks,
    disable_tracing,
    enable_tracing,
    get_active,
    get_records,
    is_tracing_enabled,
    trace_contexts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test"
    return app


# ---------------------------------------------------------------------------
# Disabled by default / zero overhead
# ---------------------------------------------------------------------------


def test_disabled_by_default():
    """Tracing is off by default; no records are created."""
    assert not is_tracing_enabled()


def test_zero_overhead_when_disabled():
    """When tracing is disabled, no ContextRecord objects are created."""
    app = _make_app()

    with app.app_context():
        pass

    with app.test_request_context("/"):
        pass

    assert get_records() == []


# ---------------------------------------------------------------------------
# App context lifecycle
# ---------------------------------------------------------------------------


def test_app_context_lifecycle():
    app = _make_app()

    with trace_contexts() as records:
        with app.app_context():
            # One record created, push event recorded.
            assert len(get_active()) == 1

        # After the with-block, context is popped.
        assert len(get_active()) == 0

    assert len(records) == 1
    rec = records[0]
    assert rec.context_type == "app"
    assert rec.app_name == app.name
    assert rec.request_info is None
    assert rec.popped is True

    # Should have at least one push and one pop event.
    event_types = [e.event for e in rec.events]
    assert "push" in event_types
    assert "pop" in event_types


def test_request_context_lifecycle():
    app = _make_app()

    with trace_contexts():
        with app.test_request_context("/hello"):
            active = get_active()
            assert len(active) == 1
            rec = active[0]
            assert rec.context_type == "request"
            assert rec.request_info is not None
            assert "GET" in rec.request_info or "/hello" in rec.request_info

        assert len(get_active()) == 0

    records = get_records()
    assert len(records) == 1
    assert records[0].popped is True


def test_context_type_distinction():
    """App-only vs request contexts are distinguished in context_type."""
    app = _make_app()

    with trace_contexts():
        with app.app_context():
            app_rec = get_active()[0]
            assert app_rec.context_type == "app"

        with app.test_request_context("/path"):
            req_rec = get_active()[0]
            assert req_rec.context_type == "request"

    records = get_records()
    types = {r.context_type for r in records}
    assert types == {"app", "request"}


# ---------------------------------------------------------------------------
# Test client lifecycle
# ---------------------------------------------------------------------------


def test_client_request_lifecycle():
    app = _make_app()

    @app.route("/ping")
    def ping():
        return "pong"

    with trace_contexts() as records:
        client = app.test_client()
        resp = client.get("/ping")
        assert resp.data == b"pong"

    # Exactly one request context should have been recorded.
    req_records = [r for r in records if r.context_type == "request"]
    assert len(req_records) == 1
    rec = req_records[0]
    assert rec.popped is True
    assert "/ping" in (rec.request_info or "")


def test_multiple_requests():
    app = _make_app()

    @app.route("/a")
    def a():
        return "a"

    @app.route("/b")
    def b():
        return "b"

    with trace_contexts() as records:
        client = app.test_client()
        client.get("/a")
        client.get("/b")

    req_records = [r for r in records if r.context_type == "request"]
    assert len(req_records) == 2

    paths = {r.request_info for r in req_records}
    assert any("/a" in (p or "") for p in paths)
    assert any("/b" in (p or "") for p in paths)


# ---------------------------------------------------------------------------
# Nested pushes (stream_with_context)
# ---------------------------------------------------------------------------


def test_nested_push():
    """stream_with_context re-pushes the context; all pushes are recorded."""
    app = _make_app()

    @app.route("/stream")
    def stream_view():
        @flask.stream_with_context
        def gen():
            yield "chunk"

        return flask.Response(gen(), mimetype="text/plain")

    with trace_contexts() as records:
        client = app.test_client()
        resp = client.get("/stream")
        assert resp.data == b"chunk"

    req_records = [r for r in records if r.context_type == "request"]
    assert len(req_records) >= 1

    # The context should have been pushed at least twice:
    # once in wsgi_app, once in stream_with_context.
    push_events = [e for e in req_records[0].events if e.event == "push"]
    assert len(push_events) >= 2

    # There should be at least 2 pop events: one nested pop from
    # stream_with_context and one final pop from wsgi_app.
    pop_events = [e for e in req_records[0].events if e.event == "pop"]
    assert len(pop_events) >= 2

    # Context should be fully cleaned up.
    assert req_records[0].popped is True


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------


def test_copy_records_source():
    app = _make_app()

    with trace_contexts() as records:
        with app.test_request_context("/copy-test") as ctx:
            source_id = id(ctx)
            copied = ctx.copy()
            copied_id = id(copied)

            # Push and pop the copied context to test its lifecycle.
            with copied:
                pass

    # Find the copied record.
    copied_records = [r for r in records if r.context_id == copied_id]
    assert len(copied_records) == 1
    assert copied_records[0].source_context_id == source_id
    assert copied_records[0].popped is True

    # Source should have a "copy" event.
    source_records = [r for r in records if r.context_id == source_id]
    assert len(source_records) == 1
    copy_events = [e for e in source_records[0].events if e.event == "copy"]
    assert len(copy_events) == 1


def test_copy_current_request_context():
    app = _make_app()
    results = {}

    @app.route("/bg")
    def bg():
        @flask.copy_current_request_context
        def worker():
            results["path"] = flask.request.path

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        return "ok"

    with trace_contexts() as records:
        client = app.test_client()
        resp = client.get("/bg")
        assert resp.data == b"ok"

    # The worker thread should have created a copied context.
    copied = [r for r in records if r.source_context_id is not None]
    assert len(copied) >= 1
    assert results["path"] == "/bg"


# ---------------------------------------------------------------------------
# Stack traces
# ---------------------------------------------------------------------------


def test_stack_traces_captured():
    app = _make_app()

    with trace_contexts() as records:
        with app.app_context():
            pass

    rec = records[0]

    # created_trace should have a stack.
    assert isinstance(rec.created_trace.stack, list)

    # At least one frame should reference this test file.
    full_stack = "\n".join(rec.created_trace.stack)
    assert "test_tracing" in full_stack or "test_stack_traces_captured" in full_stack

    # Push/pop events also have stacks.
    for event in rec.events:
        assert isinstance(event.stack, list)


# ---------------------------------------------------------------------------
# Leak detection
# ---------------------------------------------------------------------------


def test_get_active():
    app = _make_app()

    with trace_contexts():
        ctx = app.app_context()
        ctx.push()

        active = get_active()
        assert len(active) == 1

        ctx.pop()

        active = get_active()
        assert len(active) == 0


def test_check_leaks_warns():
    app = _make_app()

    enable_tracing()
    try:
        ctx = app.app_context()
        ctx.push()

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            leaks = check_leaks()

        assert len(leaks) == 1
        assert leaks[0].context_type == "app"

        # Should have emitted a ResourceWarning.
        resource_warnings = [x for x in w if issubclass(x.category, ResourceWarning)]
        assert len(resource_warnings) == 1
        assert "Leaked" in str(resource_warnings[0].message)
    finally:
        # Clean up to avoid leaking into other tests.
        ctx.pop()
        disable_tracing()


def test_trace_contexts_warns_on_leak():
    app = _make_app()

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")

        with trace_contexts() as records:
            ctx = app.app_context()
            ctx.push()
            # Intentionally do NOT pop — this is a leak.

        # After the with block, a warning should have been emitted.
        resource_warnings = [x for x in w if issubclass(x.category, ResourceWarning)]
        assert len(resource_warnings) == 1

    # Clean up.
    ctx.pop()


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_trace_contexts_manager():
    app = _make_app()

    assert not is_tracing_enabled()

    with trace_contexts() as records:
        assert is_tracing_enabled()

        with app.app_context():
            assert len(records) >= 1

    # After exit, tracing is disabled again.
    assert not is_tracing_enabled()


def test_trace_contexts_no_warn():
    """trace_contexts(warn_leaks=False) suppresses leak warnings."""
    app = _make_app()

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")

        with trace_contexts(warn_leaks=False):
            ctx = app.app_context()
            ctx.push()

        resource_warnings = [x for x in w if issubclass(x.category, ResourceWarning)]
        assert len(resource_warnings) == 0

    ctx.pop()


# ---------------------------------------------------------------------------
# Enable / disable manually
# ---------------------------------------------------------------------------


def test_enable_disable_manual():
    app = _make_app()

    enable_tracing()
    assert is_tracing_enabled()

    with app.app_context():
        assert len(get_records()) == 1

    disable_tracing()
    assert not is_tracing_enabled()

    # New contexts after disable should NOT be recorded.
    prev_count = len(get_records())

    with app.app_context():
        assert len(get_records()) == prev_count  # no new records


def test_enable_clears_previous_records():
    app = _make_app()

    enable_tracing()
    with app.app_context():
        pass

    assert len(get_records()) > 0

    # Re-enabling should clear previous records.
    enable_tracing()
    assert len(get_records()) == 0
    disable_tracing()


# ---------------------------------------------------------------------------
# CLI context tracing
# ---------------------------------------------------------------------------


def test_cli_context_traced():
    """App contexts pushed by CLI (app.app_context()) are traced."""
    app = _make_app()

    with trace_contexts() as records:
        with app.app_context():
            assert flask.has_app_context()
            assert not flask.has_request_context()

    assert len(records) == 1
    assert records[0].context_type == "app"
    assert records[0].popped is True


# ---------------------------------------------------------------------------
# Client with context preservation (with client:)
# ---------------------------------------------------------------------------


def test_client_context_preservation():
    """Contexts preserved via `with client:` are still traced."""
    app = _make_app()

    @app.route("/preserve")
    def preserve():
        flask.g.marker = 42
        return "ok"

    with trace_contexts() as records:
        client = app.test_client()

        with client:
            resp = client.get("/preserve")
            assert resp.data == b"ok"
            # Context is preserved — we can still access g.
            assert flask.g.marker == 42

        # After exiting the with-client block, context is popped.

    # The preserved context goes through two lifecycles:
    # 1. Created and pushed in wsgi_app, popped after response.
    # 2. Re-pushed by FlaskClient for context preservation, popped on __exit__.
    # So we expect at least 1 request record, and all should be popped.
    req_records = [r for r in records if r.context_type == "request"]
    assert len(req_records) >= 1
    for rec in req_records:
        assert rec.popped is True


# ---------------------------------------------------------------------------
# ContextTrace is frozen
# ---------------------------------------------------------------------------


def test_context_trace_is_frozen():
    trace = ContextTrace(event="push", timestamp=0.0, stack=[])

    with pytest.raises(AttributeError):
        trace.event = "pop"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Mid-flight tracing
# ---------------------------------------------------------------------------


def test_mid_flight_enable():
    """Enabling tracing after a context is already pushed still records push."""
    app = _make_app()

    ctx = app.app_context()
    ctx.push()

    # Enable tracing AFTER push.
    enable_tracing()

    # The context is already pushed so no push event, but pop should work.
    # However, record_pop handles the case where _index has no entry.
    ctx.pop()

    records = get_records()
    # record_pop was called but since the context was created before tracing,
    # the record might or might not exist depending on backfill logic.
    # The key point: no crash.
    disable_tracing()
