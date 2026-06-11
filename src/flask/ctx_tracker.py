from __future__ import annotations

import dataclasses
import time
import traceback
import typing as t
import warnings

if t.TYPE_CHECKING:
    from .ctx import AppContext


class ContextLeakWarning(UserWarning):
    """Warning issued when context tracking detects contexts that were
    pushed but never popped.

    .. versionadded:: 3.2
    """


@dataclasses.dataclass(frozen=True, slots=True)
class ContextEvent:
    """A recorded context lifecycle event.

    .. versionadded:: 3.2
    """

    event_type: str
    """The type of event: ``"push"``, ``"pop"``, or ``"copy"``."""

    context_id: int
    """The :func:`id` of the :class:`~flask.ctx.AppContext` instance."""

    has_request: bool
    """Whether the context carries request data."""

    request_method: str | None
    """The HTTP method if this is a request context, otherwise ``None``."""

    request_url: str | None
    """The request URL if this is a request context, otherwise ``None``."""

    timestamp: float
    """:func:`time.monotonic` timestamp when the event was recorded."""

    stack: traceback.StackSummary
    """The call stack at the time the event was recorded."""


def _make_event(event_type: str, ctx: AppContext) -> ContextEvent:
    has_request = ctx._request is not None
    return ContextEvent(
        event_type=event_type,
        context_id=id(ctx),
        has_request=has_request,
        request_method=ctx._request.method if has_request else None,
        request_url=ctx._request.url if has_request else None,
        timestamp=time.monotonic(),
        stack=traceback.extract_stack(),
    )


class ContextTracker:
    """Records :class:`~flask.ctx.AppContext` lifecycle events for
    debugging context leaks. Enabled via
    :meth:`~flask.Flask.enable_context_tracking`.

    .. versionadded:: 3.2
    """

    __slots__ = ("_events", "_active_contexts")

    def __init__(self) -> None:
        self._events: list[ContextEvent] = []
        self._active_contexts: dict[int, ContextEvent] = {}

    def record_push(self, ctx: AppContext) -> None:
        """Record a context push event."""
        event = _make_event("push", ctx)
        self._events.append(event)
        self._active_contexts[id(ctx)] = event

    def record_pop(self, ctx: AppContext) -> None:
        """Record a context pop event."""
        event = _make_event("pop", ctx)
        self._events.append(event)
        self._active_contexts.pop(id(ctx), None)

    def record_copy(self, original: AppContext, copy: AppContext) -> None:
        """Record that a context was copied from *original* to *copy*."""
        event = _make_event("copy", copy)
        self._events.append(event)

    @property
    def events(self) -> list[ContextEvent]:
        """A list of all recorded events."""
        return list(self._events)

    def get_leaks(self) -> list[ContextEvent]:
        """Return push events for contexts that were pushed but never
        popped."""
        return list(self._active_contexts.values())

    def check_leaks(self) -> None:
        """Issue a :class:`ContextLeakWarning` if there are contexts that
        were pushed but never popped."""
        leaks = self.get_leaks()
        if leaks:
            warnings.warn(
                f"{len(leaks)} context(s) were pushed but not"
                f" popped:\n{self.format_leaks()}",
                ContextLeakWarning,
                stacklevel=2,
            )

    def format_leaks(self) -> str:
        """Format leaked contexts as a human-readable string with stack
        traces."""
        leaks = self.get_leaks()
        if not leaks:
            return "No leaked contexts."

        parts: list[str] = []
        for i, event in enumerate(leaks, 1):
            ctx_type = "request" if event.has_request else "app"
            header = f"Leak {i}: {ctx_type} context (id={event.context_id})"
            if event.has_request:
                header += f" {event.request_method} {event.request_url}"
            stack_str = "".join(event.stack.format())
            parts.append(f"{header}\nPushed at:\n{stack_str}")

        return "\n".join(parts)

    def clear(self) -> None:
        """Clear all recorded events and active context tracking."""
        self._events.clear()
        self._active_contexts.clear()
