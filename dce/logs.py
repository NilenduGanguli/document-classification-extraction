"""Structured, correlated, PII-aware logging for the whole service.

Three problems this solves, in order of how much they hurt at 2am.

**Correlation.** Without it, a detailed log is a wall of lines from a dozen modules with no
way to tell which upload each belongs to. Every line emitted through :func:`event` carries a
short ``req`` id, and a ``doc`` id once one is known, so one request can be pulled out with a
single grep.

**Shape.** ``key=value`` throughout, one event name per line, so the log is greppable by a
human (``grep 'ocr.submit'``) and parseable by a shipper without a regex per message. The
alternative — prose interpolated into sentences — is readable once and unqueryable forever.

**PII.** This service classifies passports and bank statements. A page's text, an extracted
field's *value*, an anchor's matched string and a filename are all potentially personal data,
and the default position is that **none of them are logged**. :func:`event` will not accept
them by accident: values are coerced to short scalars, containers are counted rather than
dumped, and anything long is truncated with its length reported instead.

That is a deliberate asymmetry. Counting is almost always enough to debug — "did Azure return
anything, and how much" — and the one time it is not, ``DCE_INGEST_OCR_LOG_BODIES`` is an
explicit, separately-documented switch rather than a side effect of raising a level.

Usage::

    from dce import logs

    logs.event(logger, "ingest.detect", media_type=media_type, basis=detection.basis)
    with logs.stage(logger, "classify", doctype=spec.doctype_id):
        ...

``DCE_LOG_LEVEL`` sets the level for the ``dce`` package; see
:func:`dce.api.app._configure_logging`.
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Iterator, Sized
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

#: The current request's short id, and the document's id once ingestion has one. Contextvars
#: rather than arguments so a module deep in the cascade does not need a parameter threaded
#: through six call sites to say which request it is serving.
_request_id: ContextVar[str] = ContextVar("dce_request_id", default="")
_doc_id: ContextVar[str] = ContextVar("dce_doc_id", default="")

#: Longer than this and a value is reported by length rather than content. Deliberately short:
#: the fields worth logging here are ids, doctypes, statuses and numbers, none of which are
#: long, so anything that trips this is probably document text that should not be in a log.
_MAX_VALUE_CHARS = 120


def new_request_id() -> str:
    """A short id for one request. Short because it is prefixed onto every line."""
    return uuid.uuid4().hex[:8]


def bind_request(request_id: str = "") -> str:
    """Start a correlation scope. Returns the id in use.

    Prefer :func:`request_scope`, which restores the previous values on exit. This bare form
    exists for callers that cannot use a context manager; it leaves the ids set, so anything
    logged afterwards on the same thread inherits them.
    """
    value = request_id or new_request_id()
    _request_id.set(value)
    _doc_id.set("")
    return value


@contextmanager
def request_scope(request_id: str = "") -> Iterator[str]:
    """Bind a correlation id for the duration of the block, then restore what was there.

    Restoring matters because worker threads are reused. Setting a contextvar without keeping
    its token leaves the id in place after the request ends, and the next thing logged on that
    thread — a background task, a probe, the next request before it binds — is stamped with a
    request id it has nothing to do with. A log that attributes lines to the wrong request is
    worse than one that attributes them to none.
    """
    value = request_id or new_request_id()
    request_token = _request_id.set(value)
    doc_token = _doc_id.set("")
    try:
        yield value
    finally:
        _request_id.reset(request_token)
        _doc_id.reset(doc_token)


def bind_doc(doc_id: str) -> None:
    """Attach a document id to the current request, once ingestion has determined one.

    **Hashed, not carried.** ``doc_id`` is caller-supplied and is routinely a filename —
    ``john-smith-passport.pdf`` — so it is personal data, and a correlation key is repeated on
    every single line of a request. An 8-character digest still groups a request's lines and
    still matches across runs of the same upload, without putting a customer's name in a log
    aggregator thousands of times.

    The raw value is not lost: it is emitted once, at DEBUG, by whoever binds it, so somebody
    debugging with an explicit level can still tie a digest back to a file.
    """
    if doc_id:
        _doc_id.set(hashlib.sha256(doc_id.encode("utf-8", "replace")).hexdigest()[:8])


def current_request_id() -> str:
    return _request_id.get()


def _render(value: Any) -> str:
    """One field value, rendered safely.

    Containers are COUNTED, never dumped: a list of blocks is document text, and a log line
    that grew one by accident would be a disclosure nobody reviewed. Long strings are reported
    by length for the same reason.
    """
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{value:g}" if isinstance(value, float) else str(value)
    if isinstance(value, (list, tuple, set, dict)):
        return f"<{type(value).__name__}:{len(value)}>"
    text = str(value)
    if len(text) > _MAX_VALUE_CHARS:
        return f"<{len(text)}chars>"
    if not text:
        return "-"
    return text.replace(" ", "_").replace("\n", " ") if " " in text or "\n" in text else text


def fields(**values: Any) -> str:
    """Render ``key=value`` pairs, dropping the ones that carry nothing."""
    return " ".join(f"{k}={_render(v)}" for k, v in values.items() if v is not None)


def event(logger: logging.Logger, name: str, /, level: int = logging.INFO, **values: Any) -> None:
    """Emit one structured event.

    Args:
        logger: The calling module's logger, so the log names the module.
        name: A dotted event name — ``ingest.detect``, ``ocr.submit``, ``classify.accept``.
            Greppable, stable, and the first thing on the line after the correlation.
        level: ``logging.INFO`` for a stage a reader should always see; ``DEBUG`` for detail.
        **values: Scalar fields. Containers are counted, long strings reported by length.
    """
    if not logger.isEnabledFor(level):
        return
    prefix = fields(req=_request_id.get() or None, doc=_doc_id.get() or None)
    body = fields(**values)
    logger.log(level, "%s", " ".join(part for part in (prefix, name, body) if part))


@contextmanager
def stage(
    logger: logging.Logger, name: str, /, level: int = logging.INFO, **values: Any
) -> Iterator[dict[str, Any]]:
    """Time a stage and log its start and end, including on failure.

    Yields a dict the body can add fields to; those appear on the closing line, so a stage can
    report what it *found* rather than only that it ran::

        with logs.stage(logger, "ingest.pdf") as out:
            out["pages"] = len(pages)

    A failure logs ``<name>.failed`` at WARNING with the exception type — the type, not the
    message, because an exception message can quote document content.
    """
    extra: dict[str, Any] = {}
    event(logger, f"{name}.start", level=level, **values)
    started = time.perf_counter()
    try:
        yield extra
    except BaseException as exc:
        event(
            logger,
            f"{name}.failed",
            level=logging.WARNING,
            error=type(exc).__name__,
            ms=int((time.perf_counter() - started) * 1000),
            **extra,
        )
        raise
    event(
        logger,
        f"{name}.done",
        level=level,
        ms=int((time.perf_counter() - started) * 1000),
        **extra,
    )


def count(value: Sized | None) -> int:
    """Length of a container, or 0. For logging how much came back, never what."""
    return len(value) if value is not None else 0


__all__ = [
    "bind_doc",
    "bind_request",
    "count",
    "current_request_id",
    "event",
    "fields",
    "new_request_id",
    "request_scope",
    "stage",
]
