"""The invariant, made executable — and it has **two** directions.

**Before classification: nothing leaves.** Other business units hand this service documents
that have **not** been classified. Sending their bytes, their text, or an *embedding of*
their text to any external service before we know what the document is, is the exact failure
this service exists to prevent. A comment in a design document does not prevent that; a guard
that raises does.

**After a doctype is accepted: egress is allowed, on purpose.** That is the entire point of
the tiering. Once the cascade has said "this is a US W-9", calling an Azure prebuilt
specialist (T2/T3) or a constrained LLM (T4) is a considered decision about a *known* document
type, made by a deployment that switched that tier on. What must never happen is a tier
reaching the network on behalf of a document the cascade **abstained** on: ``unknown`` routes
to a human, never to a model.

Five pieces:

* :func:`classification_scope` — marks the current task/thread as *pre-classification*. The
  cascade wraps its whole run in it. It is a :class:`contextvars.ContextVar`, so it follows
  ``asyncio`` tasks (which copy the context) and does not leak between threads (which each
  start from their own context).
* :func:`post_classification_scope` — the other direction. Entered with the accepted
  ``doctype_id``, it *permits* egress and refuses to open at all for ``unknown``/empty. Every
  remote tier opens one before it touches a client, so "we only call out about documents we
  have identified" is enforced at the one place that can enforce it rather than re-checked in
  each tier.
* :func:`assert_no_egress` — called by any code that is about to leave the process. Inside a
  classification scope it raises :class:`EgressViolation` unless an operator has deliberately
  set ``allow_preclassification_egress``. Outside a classification scope it is a no-op:
  post-classification egress (fetching a layout payload from DES, calling a model) is normal.
* :func:`no_egress` — the decorator form, for whole functions.
* :func:`socket_tripwire` — an audit/test utility that makes *any* socket creation raise, so
  a test can prove the classification path opened zero connections rather than asserting a
  code path was not taken.

``assert_no_egress`` is about the **process boundary**, not about I/O in general: reading a
locally mounted BERT checkpoint off disk is not egress and does not call it. Downloading that
checkpoint from a model hub is, and does.
"""
from __future__ import annotations

import contextvars
import functools
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from dce.config import Settings, get_settings
from dce.models import UNKNOWN

__all__ = [
    "EgressViolation",
    "assert_no_egress",
    "classification_scope",
    "in_classification_scope",
    "no_egress",
    "post_classification_doctype",
    "post_classification_scope",
    "socket_tripwire",
]


class EgressViolation(RuntimeError):
    """Raised when pre-classification code tries to leave the process.

    This is a hard error on purpose. There is no degraded mode in which the service quietly
    ships an unclassified customer document to a third party.
    """


#: True while the current task/thread is inside the classification cascade.
_IN_CLASSIFICATION: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "dce_in_classification", default=False
)

#: The accepted doctype the current task/thread is permitted to talk to the outside world
#: about, or ``None``. Set only by :func:`post_classification_scope`.
_POST_CLASSIFICATION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "dce_post_classification_doctype", default=None
)

_F = TypeVar("_F", bound=Callable[..., Any])


def _record_block(component: str) -> None:
    """Count a refused egress attempt, if metrics are available.

    Imported lazily and swallowed defensively: this runs only on the path that is about to
    raise :class:`EgressViolation`, and a metrics problem must never replace the exception a
    caller needs to see.
    """
    try:
        from dce import observability

        observability.observe_egress_block(component)
    except Exception:  # noqa: BLE001 - the guard's job is to raise, not to report
        return


@contextmanager
def classification_scope() -> Iterator[None]:
    """Mark the current task/thread as pre-classification for the duration of the block.

    Re-entrant: nesting is harmless because the flag is boolean and restored via token.

    Yields:
        ``None``. On exit the previous value is restored, so a caller that was already inside
        a scope stays inside it.
    """
    token = _IN_CLASSIFICATION.set(True)
    try:
        yield
    finally:
        _IN_CLASSIFICATION.reset(token)


def in_classification_scope() -> bool:
    """Return whether the caller is running inside :func:`classification_scope`."""
    return _IN_CLASSIFICATION.get()


@contextmanager
def post_classification_scope(doctype_id: str) -> Iterator[str]:
    """Permit egress for a document whose type is **known**, and refuse otherwise.

    This is the second half of the invariant and the gate every remote tier (T2 Azure
    specialists, T3 query fields, T4 the constrained LLM) opens before it constructs a client.
    Two things make it worth being a context manager rather than an ``if``:

    * the refusal happens **once, here**, so a new tier cannot forget it — asking for a scope
      is the only way to get one, and asking with ``unknown`` raises;
    * it is a :class:`contextvars.ContextVar` like :func:`classification_scope`, so it follows
      ``asyncio`` tasks (an async tier that fans out over ``asyncio.gather`` keeps the scope in
      every child task) and does not leak into threads.

    Entering while a classification is still running is itself a violation: a tier called from
    *inside* the cascade is pre-classification egress no matter what doctype id it was handed,
    and silently clearing the flag would turn this helper into the bypass the service exists to
    prevent.

    Args:
        doctype_id: The doctype the cascade **accepted**. ``unknown``, empty or whitespace is
            an abstention, and an abstention routes to a human, never to a model.

    Yields:
        The normalised doctype id, so a caller can use it in a prompt or a request path
        without re-deriving it.

    Raises:
        EgressViolation: When ``doctype_id`` is empty or ``unknown``, or when the caller is
            still inside :func:`classification_scope`.
    """
    resolved = (doctype_id or "").strip()
    if not resolved or resolved.casefold() == UNKNOWN:
        _record_block("post_classification_scope")
        raise EgressViolation(
            f"post_classification_scope({doctype_id!r}) refused: the cascade abstained, so "
            "this document has no accepted type. An abstention routes to the human review "
            "queue — it must never be forwarded to Azure, to an LLM, or to any other remote "
            "tier, because 'ask a model what this is' is pre-classification egress wearing a "
            "different hat."
        )
    if _IN_CLASSIFICATION.get():
        _record_block("post_classification_scope")
        raise EgressViolation(
            f"post_classification_scope({resolved!r}) was opened inside classification_scope. "
            "A remote tier is being called from within the cascade, which is pre-classification "
            "egress whatever doctype id it was handed. Run the tier after classify() returns."
        )
    scope_token = _IN_CLASSIFICATION.set(False)
    doctype_token = _POST_CLASSIFICATION.set(resolved)
    try:
        yield resolved
    finally:
        _POST_CLASSIFICATION.reset(doctype_token)
        _IN_CLASSIFICATION.reset(scope_token)


def post_classification_doctype() -> str | None:
    """Return the doctype the caller is inside a :func:`post_classification_scope` for.

    ``None`` outside such a scope. Tiers use it for audit strings and to assert they were not
    invoked bare — the scope is the authority on which document a remote call is about.
    """
    return _POST_CLASSIFICATION.get()


def assert_no_egress(stage: str, *, settings: Settings | None = None) -> None:
    """Assert that leaving the process is permitted right now.

    Call this immediately before any operation that crosses the process boundary — an HTTP
    request, a vendor SDK call, an embedding API, a model-hub download.

    Args:
        stage: Human-readable name of the call site, e.g. ``"bert_knn.hub_download"``. It is
            quoted in the exception so an operator can find the offending line.
        settings: Settings override; defaults to :func:`dce.config.get_settings`.

    Raises:
        EgressViolation: If the caller is inside a classification scope and
            ``allow_preclassification_egress`` is False.
    """
    if not _IN_CLASSIFICATION.get():
        return
    resolved = settings if settings is not None else get_settings()
    if resolved.allow_preclassification_egress:
        return
    _record_block(stage)
    raise EgressViolation(
        f"{stage!r} attempted network egress during classification. The document type is not "
        "known yet, so its content must not leave this process. Classification is anchors, "
        "checksums, lexical scoring and an optional LOCAL BERT — nothing else. If this is "
        "genuinely intended, set allow_preclassification_egress=true, which is a deliberate, "
        "auditable act and not a tuning knob."
    )


def no_egress(stage: str) -> Callable[[_F], _F]:
    """Decorator form of :func:`assert_no_egress` for whole functions.

    Args:
        stage: Name reported in the exception.

    Returns:
        A decorator that guards the wrapped callable.
    """

    def decorate(fn: _F) -> _F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            assert_no_egress(stage)
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


@contextmanager
def socket_tripwire() -> Iterator[list[str]]:
    """Make every socket creation inside the block raise :class:`EgressViolation`.

    Defence in depth for audits and tests: instead of asserting that a code path was not
    taken, assert that no connection was even attempted. Deliberately **not** enabled in the
    request path — it patches the :mod:`socket` module globally and would therefore affect
    unrelated threads in the same process.

    Yields:
        A list that collects a description of each blocked attempt (empty when clean).
    """
    attempts: list[str] = []
    original_socket = socket.socket
    original_connect = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo

    def _blocked(name: str) -> Callable[..., Any]:
        def raiser(*args: Any, **kwargs: Any) -> Any:
            attempts.append(f"{name}{args!r}")
            raise EgressViolation(f"socket_tripwire blocked {name}{args!r}")

        return raiser

    socket.socket = _blocked("socket.socket")  # type: ignore[assignment]
    socket.create_connection = _blocked("socket.create_connection")  # type: ignore[assignment]
    socket.getaddrinfo = _blocked("socket.getaddrinfo")  # type: ignore[assignment]
    try:
        yield attempts
    finally:
        socket.socket = original_socket  # type: ignore[assignment]
        socket.create_connection = original_connect  # type: ignore[assignment]
        socket.getaddrinfo = original_getaddrinfo  # type: ignore[assignment]
