"""Internal context tracing engine.

Records the lifecycle (create, push, pop, copy) of app and request contexts
with stack traces. Enabled via :func:`enable_tracing` or the
:func:`trace_contexts` context manager.

All hook functions (``record_*``) short-circuit immediately when tracing is
disabled, so the overhead is one function call + one bool check (~100 ns)
per context operation — negligible compared to the ~50 µs context lifecycle.
"""

from __future__ import annotations

import time
import traceback
import warnings
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field

if False:  # TYPE_CHECKING without triggering import cycle
    from .ctx import AppContext


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextTrace:
    """A single lifecycle event for a context (create / push / pop / copy)."""

    event: str
    timestamp: float
    stack: list[str]


@dataclass
class ContextRecord:
    """Tracks the full lifecycle of one ``AppContext`` instance."""

    context_id: int
    context_type: str  # "app" or "request"
    app_name: str
    request_info: str | None  # e.g. "GET /index"
    created_trace: ContextTrace
    source_context_id: int | None = None
    events: list[ContextTrace] = field(default_factory=list)
    popped: bool = False
    _ref: weakref.ref[AppContext] | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Module-level state (shared across all threads / async tasks)
# ---------------------------------------------------------------------------

_tracing_enabled: bool = False
_records: list[ContextRecord] = []
_index: dict[int, ContextRecord] = {}  # id(ctx) -> record (active only)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Frames from these modules are stripped from captured stacks so the
# output starts at the user code that triggered the context operation.
_INTERNAL_MODULES = (
    "flask/_tracing.py",
    "flask/ctx.py",
    "flask/app.py",
    "flask/helpers.py",  # stream_with_context
    "flask/testing.py",
)


def _capture_stack() -> list[str]:
    """Capture the current call stack, stripping internal Flask frames."""
    raw = traceback.extract_stack()
    # Drop the last two frames (this function + the record_* caller in ctx.py).
    raw = raw[:-2]

    lines: list[str] = []

    for frame in raw:
        filename = frame.filename.replace("\\", "/")

        # Skip internal Flask frames.
        if any(filename.endswith(mod) for mod in _INTERNAL_MODULES):
            continue

        lines.append(
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
            f"    {frame.line}"
        )

    return lines


def _make_trace(event: str) -> ContextTrace:
    return ContextTrace(event=event, timestamp=time.time(), stack=_capture_stack())


# ---------------------------------------------------------------------------
# Hook functions — called from ``ctx.py``
# ---------------------------------------------------------------------------


def record_create(ctx: AppContext) -> None:
    """Record the creation of a new context (called from ``AppContext.__init__``)."""
    if not _tracing_enabled:
        return

    cid = id(ctx)
    req = ctx._request
    ctx_type = "request" if req is not None else "app"
    request_info: str | None = None

    if req is not None:
        try:
            request_info = f"{req.method} {req.path}"
        except Exception:
            request_info = "<unavailable>"

    record = ContextRecord(
        context_id=cid,
        context_type=ctx_type,
        app_name=ctx.app.name,
        request_info=request_info,
        created_trace=_make_trace("create"),
        _ref=weakref.ref(ctx),
    )
    _records.append(record)
    _index[cid] = record


def record_push(ctx: AppContext) -> None:
    """Record a push event (called from ``AppContext.push``)."""
    if not _tracing_enabled:
        return

    cid = id(ctx)
    record = _index.get(cid)

    if record is None:
        # Context was created before tracing was enabled — backfill.
        record_create(ctx)
        record = _index.get(cid)

        if record is None:
            return

    record.events.append(_make_trace("push"))


def record_pop(ctx: AppContext, *, final: bool = True) -> None:
    """Record a pop event (called from ``AppContext.pop``).

    :param final: ``True`` when the context is fully popped (push_count
        reached 0). ``False`` for a nested pop that decrements the count
        but does not run teardown.
    """
    if not _tracing_enabled:
        return

    cid = id(ctx)

    if final:
        record = _index.pop(cid, None)
    else:
        record = _index.get(cid)

    if record is None:
        return

    record.events.append(_make_trace("pop"))

    if final:
        record.popped = True


def record_copy(source: AppContext, new_ctx: AppContext) -> None:
    """Record a copy event linking *new_ctx* back to *source* (called from ``AppContext.copy``)."""
    if not _tracing_enabled:
        return

    new_cid = id(new_ctx)
    new_record = _index.get(new_cid) or _records[-1] if _records else None

    if new_record is not None:
        new_record.source_context_id = id(source)

    # Also log a "copy" event on the source.
    src_cid = id(source)
    src_record = _index.get(src_cid)

    if src_record is not None:
        src_record.events.append(_make_trace("copy"))


# ---------------------------------------------------------------------------
# Public control API
# ---------------------------------------------------------------------------


def enable_tracing() -> None:
    """Enable context tracing. Clears any previous records."""
    global _tracing_enabled
    _tracing_enabled = True
    _records.clear()
    _index.clear()


def disable_tracing() -> None:
    """Disable context tracing."""
    global _tracing_enabled
    _tracing_enabled = False


def is_tracing_enabled() -> bool:
    """Return whether context tracing is currently enabled."""
    return _tracing_enabled


def get_records() -> list[ContextRecord]:
    """Return a snapshot of all recorded context lifecycles."""
    return list(_records)


def get_active() -> list[ContextRecord]:
    """Return records for contexts that have not been popped yet."""
    return [r for r in _records if not r.popped]


def check_leaks() -> list[ContextRecord]:
    """Return leaked (un-popped) contexts and emit :class:`ResourceWarning` for each."""
    leaks = get_active()

    for record in leaks:
        info = f"{record.context_type} context of {record.app_name!r}"

        if record.request_info:
            info += f" ({record.request_info})"

        where = ""

        if record.created_trace.stack:
            where = "\n" + "\n".join(record.created_trace.stack[-3:])

        warnings.warn(
            f"Leaked {info} (id={record.context_id}).{where}",
            ResourceWarning,
            stacklevel=2,
        )

    return leaks


@contextmanager
def trace_contexts(*, warn_leaks: bool = True):
    """Context manager that enables tracing for a block and disables it on exit.

    Yields the live records list so callers can inspect it::

        from flask.tracing import trace_contexts

        with trace_contexts() as records:
            client.get("/")
            ...

        for r in records:
            print(r.context_type, r.request_info, r.popped)

    :param warn_leaks: If ``True`` (the default), emit a
        :class:`ResourceWarning` for every context that was not properly
        popped when the block exits.
    """
    enable_tracing()
    try:
        yield _records
    finally:
        if warn_leaks:
            check_leaks()

        disable_tracing()
