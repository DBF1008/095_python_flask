from __future__ import annotations

import contextvars
import typing as t
from functools import update_wrapper
from types import TracebackType

from werkzeug.exceptions import HTTPException
from werkzeug.routing import MapAdapter

from . import typing as ft
from .globals import _cv_app
from .helpers import _CollectErrors
from .signals import appcontext_popped
from .signals import appcontext_pushed

if t.TYPE_CHECKING:
    import typing_extensions as te
    from _typeshed.wsgi import WSGIEnvironment

    from .app import Flask
    from .sessions import SessionMixin
    from .wrappers import Request


# a singleton sentinel value for parameter defaults
_sentinel = object()


class _AppCtxGlobals:
    """A plain object. Used as a namespace for storing data during an
    application context.

    Creating an app context automatically creates this object, which is
    made available as the :data:`.g` proxy.

    .. describe:: 'key' in g

        Check whether an attribute is present.

        .. versionadded:: 0.10

    .. describe:: iter(g)

        Return an iterator over the attribute names.

        .. versionadded:: 0.10
    """

    # Define attr methods to let mypy know this is a namespace object
    # that has arbitrary attributes.

    def __getattr__(self, name: str) -> t.Any:
        try:
            return self.__dict__[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: t.Any) -> None:
        self.__dict__[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self.__dict__[name]
        except KeyError:
            raise AttributeError(name) from None

    def get(self, name: str, default: t.Any | None = None) -> t.Any:
        """Get an attribute by name, or a default value. Like
        :meth:`dict.get`.

        :param name: Name of attribute to get.
        :param default: Value to return if the attribute is not present.

        .. versionadded:: 0.10
        """
        return self.__dict__.get(name, default)

    def pop(self, name: str, default: t.Any = _sentinel) -> t.Any:
        """Get and remove an attribute by name. Like :meth:`dict.pop`.

        :param name: Name of attribute to pop.
        :param default: Value to return if the attribute is not present,
            instead of raising a ``KeyError``.

        .. versionadded:: 0.11
        """
        if default is _sentinel:
            return self.__dict__.pop(name)
        else:
            return self.__dict__.pop(name, default)

    def setdefault(self, name: str, default: t.Any = None) -> t.Any:
        """Get the value of an attribute if it is present, otherwise
        set and return a default value. Like :meth:`dict.setdefault`.

        :param name: Name of attribute to get.
        :param default: Value to set and return if the attribute is not
            present.

        .. versionadded:: 0.11
        """
        return self.__dict__.setdefault(name, default)

    def __contains__(self, item: str) -> bool:
        return item in self.__dict__

    def __iter__(self) -> t.Iterator[str]:
        return iter(self.__dict__)

    def __repr__(self) -> str:
        ctx = _cv_app.get(None)
        if ctx is not None:
            return f"<flask.g of '{ctx.app.name}'>"
        return object.__repr__(self)


def after_this_request(
    f: ft.AfterRequestCallable[t.Any],
) -> ft.AfterRequestCallable[t.Any]:
    """Decorate a function to run after the current request. The behavior is the
    same as :meth:`.Flask.after_request`, except it only applies to the current
    request, rather than every request. Therefore, it must be used within a
    request context, rather than during setup.

    .. code-block:: python

        @app.route("/")
        def index():
            @after_this_request
            def add_header(response):
                response.headers["X-Foo"] = "Parachute"
                return response

            return "Hello, World!"

    .. versionadded:: 0.9
    """
    ctx = _cv_app.get(None)

    if ctx is None or not ctx.has_request:
        raise RuntimeError(
            "'after_this_request' can only be used when a request"
            " context is active, such as in a view function."
        )

    ctx._after_request_functions.append(f)
    return f


F = t.TypeVar("F", bound=t.Callable[..., t.Any])


def copy_current_request_context(f: F) -> F:
    """Decorate a function to run inside the current request context. This can
    be used when starting a background task, otherwise it will not see the app
    and request objects that were active in the parent.

    .. warning::

        Due to the following caveats, it is often safer (and simpler) to pass
        the data you need when starting the task, rather than using this and
        relying on the context objects.

    In order to avoid execution switching partially though reading data, either
    read the request body (access ``form``, ``json``, ``data``, etc) before
    starting the task, or use a lock. This can be an issue when using threading,
    but shouldn't be an issue when using greenlet/gevent or asyncio.

    The ``session`` data is snapshot-copied into the child context so that
    reads in the child do not affect the parent's ``Vary: Cookie`` header.
    Modifying ``session`` in the task should still be avoided, as the response
    cookie is written by the parent context.

    .. code-block:: python

        import gevent
        from flask import copy_current_request_context

        @app.route('/')
        def index():
            @copy_current_request_context
            def do_some_work():
                # do some work here, it can access flask.request or
                # flask.session like you would otherwise in the view function.
                ...
            gevent.spawn(do_some_work)
            return 'Regular response'

    .. versionchanged:: 3.2
        Session data is snapshot-copied.  The parent's session is eagerly
        accessed so ``Vary: Cookie`` is always set on the response.  The
        request body is cached to prevent stream races in threaded tasks.

    .. versionadded:: 0.10
    """
    # Store the context that was active when the decorator was applied.
    original = _cv_app.get(None)

    if original is None:
        raise RuntimeError(
            "'copy_current_request_context' can only be used when a"
            " request context is active, such as in a view function."
        )

    # Eagerly access the session in the parent context so that:
    # 1. Vary: Cookie is reliably set on the parent response.
    # 2. The session data is available for snapshotting in copy().
    if original._request is not None:
        original.session  # sets accessed = True on the parent session

        # Cache the request body so that concurrent reads in child
        # tasks don't race on the underlying WSGI input stream.
        original._request.get_data(cache=True)

    def wrapper(*args: t.Any, **kwargs: t.Any) -> t.Any:
        # Copy the context before pushing, so each worker acts independently.
        with original.copy() as ctx:
            return ctx.app.ensure_sync(f)(*args, **kwargs)

    return update_wrapper(wrapper, f)  # type: ignore[return-value]


def has_request_context() -> bool:
    """Test if an app context is active and if it has request information.

    .. code-block:: python

        from flask import has_request_context, request

        if has_request_context():
            remote_addr = request.remote_addr

    If a request context is active, the :data:`.request` and :data:`.session`
    context proxies will available and ``True``, otherwise ``False``. You can
    use that to test the data you use, rather than using this function.

    .. code-block:: python

        from flask import request

        if request:
            remote_addr = request.remote_addr

    .. versionadded:: 0.7
    """
    return (ctx := _cv_app.get(None)) is not None and ctx.has_request


def has_app_context() -> bool:
    """Test if an app context is active. Unlike :func:`has_request_context`
    this can be true outside a request, such as in a CLI command.

    .. code-block:: python

        from flask import has_app_context, g

        if has_app_context():
            g.cached_data = ...

    If an app context is active, the :data:`.g` and :data:`.current_app` context
    proxies will available and ``True``, otherwise ``False``. You can use that
    to test the data you use, rather than using this function.

        from flask import g

        if g:
            g.cached_data = ...

    .. versionadded:: 0.9
    """
    return _cv_app.get(None) is not None


class AppContext:
    """An app context contains information about an app, and about the request
    when handling a request. A context is pushed at the beginning of each
    request and CLI command, and popped at the end. The context is referred to
    as a "request context" if it has request information, and an "app context"
    if not.

    Do not use this class directly. Use :meth:`.Flask.app_context` to create an
    app context if needed during setup, and :meth:`.Flask.test_request_context`
    to create a request context if needed during tests.

    When the context is popped, it will evaluate all the teardown functions
    registered with :meth:`~flask.Flask.teardown_request` (if handling a
    request) then :meth:`.Flask.teardown_appcontext`.

    When using the interactive debugger, the context will be restored so
    ``request`` is still accessible. Similarly, the test client can preserve the
    context after the request ends. However, teardown functions may already have
    closed some resources such as database connections, and will run again when
    the restored context is popped.

    :param app: The application this context represents.
    :param request: The request data this context represents.
    :param session: The session data this context represents. If not given,
        loaded from the request on first access.

    .. versionchanged:: 3.2
        Merged with ``RequestContext``. The ``RequestContext`` alias will be
        removed in Flask 4.0.

    .. versionchanged:: 3.2
        A combined app and request context is pushed for every request and CLI
        command, rather than trying to detect if an app context is already
        pushed.

    .. versionchanged:: 3.2
        The session is loaded the first time it is accessed, rather than when
        the context is pushed.
    """

    def __init__(
        self,
        app: Flask,
        *,
        request: Request | None = None,
        session: SessionMixin | None = None,
    ) -> None:
        self.app = app
        """The application represented by this context. Accessed through
        :data:`.current_app`.
        """

        self.g: _AppCtxGlobals = app.app_ctx_globals_class()
        """The global data for this context. Accessed through :data:`.g`."""

        self.url_adapter: MapAdapter | None = None
        """The URL adapter bound to the request, or the app if not in a request.
        May be ``None`` if binding failed.
        """

        self._request: Request | None = request
        self._session: SessionMixin | None = session
        self._flashes: list[tuple[str, str]] | None = None
        self._after_request_functions: list[ft.AfterRequestCallable[t.Any]] = []

        try:
            self.url_adapter = app.create_url_adapter(self._request)
        except HTTPException as e:
            if self._request is not None:
                self._request.routing_exception = e

        self._cv_token: contextvars.Token[AppContext] | None = None
        """The previous state to restore when popping."""

        self._push_count: int = 0
        """Track nested pushes of this context. Cleanup will only run once the
        original push has been popped.
        """

        self._is_copy: bool = False
        """True if this context was created by :meth:`copy`. A copied context
        shares the request object with the original but owns an independent
        session snapshot and ``g``.  On teardown the shared request is **not**
        closed, because the original context owns that lifecycle.
        """

    @classmethod
    def from_environ(cls, app: Flask, environ: WSGIEnvironment, /) -> te.Self:
        """Create an app context with request data from the given WSGI environ.

        :param app: The application this context represents.
        :param environ: The request data this context represents.
        """
        request = app.request_class(environ)
        request.json_module = app.json
        return cls(app, request=request)

    @property
    def has_request(self) -> bool:
        """True if this context was created with request data."""
        return self._request is not None

    def copy(self) -> te.Self:
        """Create a new context with the same request but an independent
        session snapshot and a fresh ``g`` object.  See
        :func:`.copy_current_request_context`.

        The session is shallow-copied so that the child context can read
        session data without affecting the parent's ``Vary: Cookie`` or
        ``modified`` flags.  The request object is shared (read-only for
        the child); callers should cache the request body before copying
        to avoid stream races.

        The returned context is marked as a copy so that
        :meth:`pop` will **not** call ``request.close()``—that
        responsibility stays with the original context.

        .. versionchanged:: 3.2
            Session data is now snapshot-copied instead of shared by reference.
            The copied context no longer closes the shared request on teardown.

        .. versionchanged:: 1.1
            The current session data is used instead of reloading the original data.

        .. versionadded:: 0.10
        """
        ctx = self.__class__(
            self.app,
            request=self._request,
            session=self._snapshot_session(),
        )
        ctx._is_copy = True
        return ctx

    def _snapshot_session(self) -> SessionMixin | None:
        """Create an independent shallow copy of the current session.

        The copy carries over the ``accessed`` and ``new`` flags so the
        child starts in the same logical state, but subsequent reads or
        writes in the child will not affect the parent's session object.
        """
        if self._session is None:
            return None

        src = self._session

        # Construct a new instance of the same session class with a copy
        # of the data.  SecureCookieSession (and most subclasses) accept
        # an ``initial`` mapping in __init__.
        try:
            snapshot: SessionMixin = type(src)(dict(src))
        except TypeError:
            import copy as _copy_mod

            snapshot = _copy_mod.copy(src)

        snapshot.accessed = src.accessed
        snapshot.new = src.new
        return snapshot

    @property
    def request(self) -> Request:
        """The request object associated with this context. Accessed through
        :data:`.request`. Only available in request contexts, otherwise raises
        :exc:`RuntimeError`.
        """
        if self._request is None:
            raise RuntimeError("There is no request in this context.")

        return self._request

    def _get_session(self) -> SessionMixin:
        """Open the session if it is not already open for this request context."""
        if self._request is None:
            raise RuntimeError("There is no request in this context.")

        if self._session is None:
            si = self.app.session_interface
            self._session = si.open_session(self.app, self.request)

            if self._session is None:
                self._session = si.make_null_session(self.app)

        return self._session

    @property
    def session(self) -> SessionMixin:
        """The session object associated with this context. Accessed through
        :data:`.session`. Only available in request contexts, otherwise raises
        :exc:`RuntimeError`. Accessing this sets :attr:`.SessionMixin.accessed`.
        """
        session = self._get_session()
        session.accessed = True
        return session

    def match_request(self) -> None:
        """Apply routing to the current request, storing either the matched
        endpoint and args, or a routing exception.
        """
        try:
            result = self.url_adapter.match(return_rule=True)  # type: ignore[union-attr]
        except HTTPException as e:
            self._request.routing_exception = e  # type: ignore[union-attr]
        else:
            self._request.url_rule, self._request.view_args = result  # type: ignore[union-attr]

    def push(self) -> None:
        """Push this context so that it is the active context. If this is a
        request context, calls :meth:`match_request` to perform routing with
        the context active.

        Typically, this is not used directly. Instead, use a ``with`` block
        to manage the context.

        In some situations, such as streaming or testing, the context may be
        pushed multiple times. It will only trigger matching and signals if it
        is not currently pushed.
        """
        self._push_count += 1

        if self._cv_token is not None:
            return

        self._cv_token = _cv_app.set(self)
        appcontext_pushed.send(self.app, _async_wrapper=self.app.ensure_sync)

        if self._request is not None:
            # Open the session at the moment that the request context is available.
            # This allows a custom open_session method to use the request context.
            self._get_session()

            # Match the request URL after loading the session, so that the
            # session is available in custom URL converters.
            if self.url_adapter is not None:
                self.match_request()

    def pop(self, exc: BaseException | None = None) -> None:
        """Pop this context so that it is no longer the active context. Then
        call teardown functions and signals.

        Typically, this is not used directly. Instead, use a ``with`` block
        to manage the context.

        This context must currently be the active context, otherwise a
        :exc:`RuntimeError` is raised. In some situations, such as streaming or
        testing, the context may have been pushed multiple times. It will only
        trigger cleanup once it has been popped as many times as it was pushed.
        Until then, it will remain the active context.

        :param exc: An unhandled exception that was raised while the context was
            active. Passed to teardown functions.

        .. versionchanged:: 0.9
            Added the ``exc`` argument.
        """
        if self._cv_token is None:
            raise RuntimeError(f"Cannot pop this context ({self!r}), it is not pushed.")

        ctx = _cv_app.get(None)

        if ctx is None or self._cv_token is None:
            raise RuntimeError(
                f"Cannot pop this context ({self!r}), there is no active context."
            )

        if ctx is not self:
            raise RuntimeError(
                f"Cannot pop this context ({self!r}), it is not the active"
                f" context ({ctx!r})."
            )

        self._push_count -= 1

        if self._push_count > 0:
            return

        collect_errors = _CollectErrors()

        if self._request is not None:
            with collect_errors:
                self.app.do_teardown_request(self, exc)

            # A copied context shares the request object with the
            # original.  Only the original context should close it.
            if not self._is_copy:
                with collect_errors:
                    self._request.close()

        with collect_errors:
            self.app.do_teardown_appcontext(self, exc)

        _cv_app.reset(self._cv_token)
        self._cv_token = None

        with collect_errors:
            appcontext_popped.send(self.app, _async_wrapper=self.app.ensure_sync)

        collect_errors.raise_any("Errors during context teardown")

    def __enter__(self) -> te.Self:
        self.push()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.pop(exc_value)

    def __repr__(self) -> str:
        if self._request is not None:
            return (
                f"<{type(self).__name__} {id(self)} of {self.app.name},"
                f" {self.request.method} {self.request.url!r}>"
            )

        return f"<{type(self).__name__} {id(self)} of {self.app.name}>"


def __getattr__(name: str) -> t.Any:
    import warnings

    if name == "RequestContext":
        warnings.warn(
            "'RequestContext' has merged with 'AppContext', and will be removed"
            " in Flask 4.0. Use 'AppContext' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return AppContext

    raise AttributeError(name)
