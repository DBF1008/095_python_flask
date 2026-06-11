"""Public API for Flask context tracing.

Provides tools to debug context leaks by recording where each app and request
context is created, pushed, popped, and copied — with full stack traces.

Usage::

    from flask.tracing import trace_contexts

    with trace_contexts() as records:
        client.get("/api/users")
        # ... any app code ...

    for r in records:
        print(r.context_type, r.request_info, r.popped)
        for event in r.events:
            print(f"  {event.event} at {event.timestamp}")
            print("".join(event.stack[-3:]))

Or enable/disable manually::

    from flask.tracing import enable_tracing, disable_tracing, get_records

    enable_tracing()
    # ... run requests ...
    for r in get_records():
        ...
    disable_tracing()

Tracing is **off by default** and has negligible overhead when disabled.

.. versionadded:: 3.2
"""

from ._tracing import check_leaks
from ._tracing import ContextRecord
from ._tracing import ContextTrace
from ._tracing import disable_tracing
from ._tracing import enable_tracing
from ._tracing import get_active
from ._tracing import get_records
from ._tracing import is_tracing_enabled
from ._tracing import trace_contexts

__all__ = [
    "ContextRecord",
    "ContextTrace",
    "check_leaks",
    "disable_tracing",
    "enable_tracing",
    "get_active",
    "get_records",
    "is_tracing_enabled",
    "trace_contexts",
]
