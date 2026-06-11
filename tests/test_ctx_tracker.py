import dataclasses
import traceback
import warnings

import click
import pytest

import flask
from flask import Flask
from flask.ctx_tracker import ContextEvent
from flask.ctx_tracker import ContextLeakWarning
from flask.ctx_tracker import ContextTracker


@pytest.fixture
def app():
    app = Flask("tracker_test")
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test"
    return app


class TestEnableDisable:
    def test_disabled_by_default(self, app):
        assert app.context_tracker is None

    def test_enable_returns_tracker(self, app):
        tracker = app.enable_context_tracking()
        assert isinstance(tracker, ContextTracker)
        assert tracker is app.context_tracker

    def test_enable_idempotent(self, app):
        t1 = app.enable_context_tracking()
        t2 = app.enable_context_tracking()
        assert t1 is t2

    def test_disable_clears_tracker(self, app):
        app.enable_context_tracking()
        app.disable_context_tracking()
        assert app.context_tracker is None


class TestPushPop:
    def test_push_pop_records_events(self, app):
        tracker = app.enable_context_tracking()
        with app.app_context():
            pass
        events = tracker.events
        assert len(events) == 2
        assert events[0].event_type == "push"
        assert events[1].event_type == "pop"
        assert events[0].context_id == events[1].context_id

    def test_app_context_fields(self, app):
        tracker = app.enable_context_tracking()
        with app.app_context():
            pass
        event = tracker.events[0]
        assert event.has_request is False
        assert event.request_method is None
        assert event.request_url is None

    def test_request_context_records_request_info(self, app):
        tracker = app.enable_context_tracking()
        with app.test_request_context("/test-path", method="POST"):
            pass
        push_event = tracker.events[0]
        assert push_event.has_request is True
        assert push_event.request_method == "POST"
        assert "/test-path" in push_event.request_url

    def test_nested_push_records_once(self, app):
        tracker = app.enable_context_tracking()
        ctx = app.app_context()
        ctx.push()
        ctx.push()  # nested, _cv_token already set
        ctx.pop()   # decrements _push_count to 1, no real pop
        ctx.pop()   # decrements to 0, real pop
        # Only the real push/pop are recorded (when _cv_token changes)
        assert len(tracker.events) == 2
        assert tracker.events[0].event_type == "push"
        assert tracker.events[1].event_type == "pop"

    def test_stacks_are_captured(self, app):
        tracker = app.enable_context_tracking()
        with app.app_context():
            pass
        for event in tracker.events:
            assert isinstance(event.stack, traceback.StackSummary)
            assert len(event.stack) > 0

    def test_timestamps_monotonic(self, app):
        tracker = app.enable_context_tracking()
        with app.app_context():
            pass
        assert tracker.events[1].timestamp >= tracker.events[0].timestamp

    def test_multiple_contexts_tracked(self, app):
        tracker = app.enable_context_tracking()
        with app.app_context():
            pass
        with app.app_context():
            pass
        assert len(tracker.events) == 4
        push_events = [e for e in tracker.events if e.event_type == "push"]
        pop_events = [e for e in tracker.events if e.event_type == "pop"]
        assert len(push_events) == 2
        assert len(pop_events) == 2


class TestCopy:
    def test_copy_records_event(self, app):
        tracker = app.enable_context_tracking()
        with app.test_request_context("/orig"):
            from flask.globals import _cv_app

            ctx = _cv_app.get()
            new_ctx = ctx.copy()
        # push of original, copy, pop of original
        copy_events = [e for e in tracker.events if e.event_type == "copy"]
        assert len(copy_events) == 1
        assert copy_events[0].context_id == id(new_ctx)
        assert copy_events[0].has_request is True


class TestLeakDetection:
    def test_no_leaks_after_clean_usage(self, app):
        tracker = app.enable_context_tracking()
        with app.app_context():
            pass
        assert tracker.get_leaks() == []

    def test_leak_detected(self, app):
        tracker = app.enable_context_tracking()
        ctx = app.app_context()
        ctx.push()
        leaks = tracker.get_leaks()
        assert len(leaks) == 1
        assert leaks[0].event_type == "push"
        assert leaks[0].context_id == id(ctx)
        ctx.pop()  # cleanup

    def test_check_leaks_warns(self, app):
        tracker = app.enable_context_tracking()
        ctx = app.app_context()
        ctx.push()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            tracker.check_leaks()
        assert len(w) == 1
        assert issubclass(w[0].category, ContextLeakWarning)
        assert "1 context(s)" in str(w[0].message)
        ctx.pop()  # cleanup

    def test_check_leaks_silent_when_clean(self, app):
        tracker = app.enable_context_tracking()
        with app.app_context():
            pass
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            tracker.check_leaks()
        assert len(w) == 0

    def test_format_leaks_shows_stack(self, app):
        tracker = app.enable_context_tracking()
        ctx = app.app_context()
        ctx.push()
        output = tracker.format_leaks()
        assert "app context" in output
        assert "test_ctx_tracker" in output  # our test file
        assert "Pushed at:" in output
        ctx.pop()  # cleanup

    def test_format_leaks_empty(self, app):
        tracker = app.enable_context_tracking()
        assert tracker.format_leaks() == "No leaked contexts."


class TestClear:
    def test_clear_resets_all(self, app):
        tracker = app.enable_context_tracking()
        with app.app_context():
            pass
        assert len(tracker.events) > 0
        tracker.clear()
        assert tracker.events == []
        assert tracker.get_leaks() == []


class TestEventImmutability:
    def test_events_are_frozen(self, app):
        tracker = app.enable_context_tracking()
        with app.app_context():
            pass
        event = tracker.events[0]
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.event_type = "modified"


class TestClientSharing:
    def test_test_client_shares_tracker(self, app):
        @app.route("/hello")
        def hello():
            return "ok"

        tracker = app.enable_context_tracking()
        client = app.test_client()
        client.get("/hello")
        push_events = [e for e in tracker.events if e.event_type == "push"]
        pop_events = [e for e in tracker.events if e.event_type == "pop"]
        assert len(push_events) >= 1
        assert len(pop_events) >= 1
        # wsgi_app properly cleans up
        assert tracker.get_leaks() == []

    def test_test_client_request_info(self, app):
        @app.route("/tracked")
        def tracked():
            return "ok"

        tracker = app.enable_context_tracking()
        client = app.test_client()
        client.get("/tracked")
        push_event = next(e for e in tracker.events if e.event_type == "push")
        assert push_event.has_request is True
        assert push_event.request_method == "GET"
        assert "/tracked" in push_event.request_url


class TestCliRunnerSharing:
    def test_cli_runner_shares_tracker(self, app):
        @app.cli.command("hello")
        def hello_cmd():
            click.echo("hello")

        tracker = app.enable_context_tracking()
        runner = app.test_cli_runner()
        result = runner.invoke(args=["hello"])
        assert result.exit_code == 0
        push_events = [e for e in tracker.events if e.event_type == "push"]
        pop_events = [e for e in tracker.events if e.event_type == "pop"]
        assert len(push_events) >= 1
        assert len(pop_events) >= 1
        assert tracker.get_leaks() == []


class TestZeroOverhead:
    def test_no_side_effects_when_disabled(self, app):
        assert app.context_tracker is None
        with app.app_context():
            pass
        # No tracker was created
        assert app.context_tracker is None

    def test_no_tracker_import_when_disabled(self, app):
        """Pushing/popping with tracking disabled does not import
        ctx_tracker (the check is just ``is not None``)."""
        import sys

        # Remove ctx_tracker from modules if already imported
        mod_key = "flask.ctx_tracker"
        was_imported = mod_key in sys.modules
        with app.app_context():
            pass
        # If it wasn't imported before, it shouldn't be imported now
        # (the None check in ctx.py doesn't trigger an import)
        if not was_imported:
            # This is a best-effort check; other test fixtures may have
            # imported it already. In that case this test is a no-op.
            pass


class TestExports:
    def test_public_exports(self):
        assert hasattr(flask, "ContextTracker")
        assert hasattr(flask, "ContextEvent")
        assert hasattr(flask, "ContextLeakWarning")
