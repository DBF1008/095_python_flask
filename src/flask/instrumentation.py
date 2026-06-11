from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class ResponseMetadata:
    """Immutable summary of response metadata collected during
    :meth:`~flask.Flask.process_response`.  Passed to callbacks
    registered with :meth:`~flask.Flask.after_response` and to
    receivers of the :data:`~flask.response_instrumented` signal.

    .. versionadded:: 3.2
    """

    status_code: int
    """The HTTP status code of the response."""

    method: str
    """The HTTP method of the request (e.g. ``"GET"``)."""

    path: str
    """The request path."""

    endpoint: str | None
    """The matched endpoint name, or ``None``."""

    json_body: str | None
    """The serialized JSON string if the response was produced by
    :func:`~flask.jsonify` or :meth:`~flask.json.provider.JSONProvider.response`,
    otherwise ``None``."""

    session_cookie_set: bool
    """``True`` if ``save_session`` wrote a ``Set-Cookie`` header for
    the session cookie."""

    session_cookie_deleted: bool
    """``True`` if ``save_session`` deleted the session cookie."""

    vary_added: frozenset[str]
    """Vary header values that were added during response processing."""

    vary_final: frozenset[str]
    """The complete set of Vary header values on the final response."""
