"""What one ingestion produced — including the honest "we cannot read this without OCR".

:class:`IngestResult` has exactly two statuses and they are not a success/failure pair:

``ok``
    The bytes carried text, the text was extracted in-process, and ``view`` is a
    :class:`~dce.models.LayoutView` ready for the cascade.

``needs_ocr``
    The bytes carry **no text at all** — a JPEG, a TIFF, a scanned PDF whose pages are one
    image each. This is not an error and not an abstention. Nothing was misread and no
    classifier ran; there was simply nothing to read, and saying so is the only honest answer
    a service with no recogniser configured can give. ``reason`` says what the file is,
    ``remedy`` says what the caller can do, and ``ocr_available`` says whether this deployment
    could have done it locally, so an operator can tell "we chose not to" from "we cannot".

A failure is neither of these: it is an exception from :mod:`dce.ingest.errors`.
"""
from __future__ import annotations

import enum

from pydantic import BaseModel, Field

from dce.ingest.detect import MediaType
from dce.models import LayoutView


class IngestStatus(enum.StrEnum):
    ok = "ok"
    needs_ocr = "needs_ocr"


class TextSource(enum.StrEnum):
    """Where the text in ``view`` came from. Every downstream rate should be split on it."""

    #: The publisher's own text — a PDF text layer, a DOCX's XML, an email body.
    native = "native_text"
    #: A local, in-process OCR engine. Lower accuracy; no call to another host.
    local_ocr = "local_ocr"
    #: An OCR SERVICE: the document was read by the endpoint this deployment configures,
    #: before its type was known. Kept distinct from ``local_ocr`` rather than folded into one
    #: "ocr" bucket because which of the two read a document is a fact about the deployment
    #: that a reviewer must be able to see without inferring it from a provider's name.
    ocr_service = "ocr_service"
    #: PART of the document carried its own text and part of it was recognised — a typed
    #: cover page in front of photographed attachments, an e-filed wrapper around a scanned
    #: ID. Distinct from both neighbours because it is neither: a rate split on ``native``
    #: would be flattered by these documents and one split on ``local_ocr`` dragged down by
    #: them, and this is precisely the shape most likely to be misread.
    mixed = "mixed"
    #: Nothing was extracted.
    none = "none"


class IngestResult(BaseModel):
    """The outcome of one ingestion."""

    status: IngestStatus = IngestStatus.ok
    media_type: MediaType
    #: How the media type was decided: ``magic`` | ``container`` | ``text-sniff``…
    detected_by: str = ""
    text_source: TextSource = TextSource.native
    #: Present exactly when ``status`` is ``ok``.
    view: LayoutView | None = None

    byte_size: int = 0
    page_count: int = 0
    pages_read: int = 0
    block_count: int = 0
    char_count: int = 0

    #: True when a truncating cap bit. The view is still usable, and shorter than the file.
    truncated: bool = False
    #: Names of the caps that bit, e.g. ``["max_table_rows"]``.
    limits_hit: list[str] = Field(default_factory=list)

    #: Why OCR is needed, in one sentence. Empty when ``status`` is ``ok``.
    reason: str = ""
    #: What the caller can do about it.
    remedy: str = ""
    #: Whether a local OCR engine was available **for this request**. False covers three
    #: distinguishable situations, and ``reason`` says which: the deployment has local OCR
    #: switched off (the default), the deployment switched it on but the engine's optional
    #: extra is not installed, or the caller declined it with ``local_ocr=False``.
    ocr_available: bool = False
    #: Which engine produced the text, when ``text_source`` is ``local_ocr`` or
    #: ``ocr_service`` — e.g. ``rapidocr``, ``azure_layout``.
    ocr_engine: str = ""
    #: True when ``ocr_engine`` recognised this document by **calling an OCR service** rather
    #: than reading it in this process. Set from the provider record's ``service`` flag, never
    #: from the engine's name.
    ocr_via_service: bool = False
    #: Host that read the document, when ``ocr_via_service``. The host, not the URL: it answers
    #: "which service read this document" and cannot carry a key in a query string.
    ocr_endpoint_host: str = ""

    ms: int = 0

    @property
    def needs_ocr(self) -> bool:
        return self.status is IngestStatus.needs_ocr

    def as_detail(self) -> dict[str, object]:
        """A compact, machine-readable form for an API error body.

        Deliberately not the whole model: a 422 body should tell a caller what happened and
        what to do, not carry a document's text back to them.
        """
        return {
            "status": str(self.status),
            "media_type": str(self.media_type),
            "detected_by": self.detected_by,
            "page_count": self.page_count,
            "reason": self.reason,
            "remedy": self.remedy,
            "ocr_available": self.ocr_available,
        }

    def provenance(self) -> dict[str, object]:
        """Where this document's text came from — carried onto every API response.

        Small and boring on purpose: ``native_text`` on most requests, and on the requests
        where it is not, the one line that tells a reviewer whether the document was read in
        this process or by a service, and which one.
        """
        return {
            "text_source": str(self.text_source),
            "ocr_engine": self.ocr_engine,
            "ocr_via_service": self.ocr_via_service,
            "ocr_endpoint_host": self.ocr_endpoint_host,
        }


__all__ = ["IngestResult", "IngestStatus", "TextSource"]
