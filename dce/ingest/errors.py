"""Ingestion failures, as a small closed set.

Every one of these is a *clean* failure: a malformed upload must produce one of these
exceptions promptly, never a hang, never a traceback from deep inside a parser, and never a
silent empty document (an empty ``LayoutView`` would be classified, would abstain, and would
present to a caller as "the classifier could not tell" when the truth is "we could not read
your file"). Each carries a stable ``code`` so an API layer can map it without string
matching.

Note what is *not* here: "this is an image and we have no text". That is not a failure — it
is a legitimate, expected outcome with its own structured shape
(:class:`dce.ingest.result.IngestResult` with ``status=needs_ocr``). See the module docstring
of :mod:`dce.ingest` for why that distinction is the central design decision of this package.
"""
from __future__ import annotations


class IngestError(Exception):
    """Base class: this upload could not be turned into a ``LayoutView``."""

    code = "ingest_error"
    #: HTTP status an API layer should use. 400 = the caller sent something wrong.
    http_status = 400


class UnsupportedFormat(IngestError):
    """The bytes are a format this package does not parse.

    Includes formats we deliberately refuse: a legacy ``.doc``/``.xls`` OLE2 file, a bare
    ZIP archive, an executable renamed to ``.pdf``.
    """

    code = "unsupported_format"
    http_status = 415


class MalformedDocument(IngestError):
    """The format was recognised but the bytes do not conform to it.

    Also covers documents we can identify but not read, such as an encrypted PDF: the file is
    not corrupt, but nothing in this process can turn it into text.
    """

    code = "malformed_document"


class LimitExceeded(IngestError):
    """A resource cap in :class:`dce.ingest.limits.IngestLimits` was hit.

    A cap that *truncates* (too many blocks, too much text) does not raise — it sets
    ``truncated`` on the result and names itself in ``limits_hit``, because half a document
    still classifies. A cap that raises is one where continuing would mean spending unbounded
    time or memory on behalf of one request.
    """

    code = "limit_exceeded"
    http_status = 413


class PayloadTooLarge(LimitExceeded):
    """The upload is larger than ``max_bytes`` before a parser is even chosen."""

    code = "payload_too_large"
    http_status = 413


class IngestTimeout(LimitExceeded):
    """Parsing ran past ``max_seconds``.

    The wall-clock cap is the backstop for every quadratic or pathological input we did not
    think of. It is checked in the loops, not enforced by a signal or a thread, so it cannot
    interrupt a single blocking C call — which is why the per-format structural caps (pages,
    rows, archive entries) exist as well rather than instead.
    """

    code = "timeout"
    http_status = 408


class ArchiveBomb(LimitExceeded):
    """A container format expanded far beyond its compressed size, or has absurd structure.

    DOCX, XLSX, PPTX and ODT are ZIP archives, so accepting them means accepting the zip-bomb
    class of attack. Guarded in :mod:`dce.ingest.zipsafe`.
    """

    code = "archive_bomb"
    http_status = 413


class EngineUnavailable(IngestError):
    """A local OCR engine was asked for but is not installed or not usable.

    Not a 400: the caller did nothing wrong, the deployment is missing an optional extra.
    """

    code = "engine_unavailable"
    http_status = 503


__all__ = [
    "ArchiveBomb",
    "EngineUnavailable",
    "IngestError",
    "IngestTimeout",
    "LimitExceeded",
    "MalformedDocument",
    "PayloadTooLarge",
    "UnsupportedFormat",
]
