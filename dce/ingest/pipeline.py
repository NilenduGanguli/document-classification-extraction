"""The dispatcher: bytes in, a :class:`~dce.ingest.result.IngestResult` out.

One entry point, :func:`ingest`. It detects the format from content, picks the parser, runs
it under the caps, and returns either a :class:`~dce.models.LayoutView` or the structured
``needs_ocr`` outcome. It raises only the errors in :mod:`dce.ingest.errors`; nothing from a
parser's internals escapes.
"""
from __future__ import annotations

import time

from pydantic import BaseModel, Field

from dce.ingest.builder import LayoutBuilder
from dce.ingest.detect import IMAGE_TYPES, MediaType, decode_text, detect
from dce.ingest.errors import IngestError, PayloadTooLarge, UnsupportedFormat
from dce.ingest.images import (
    NEEDS_OCR_REMEDY,
    needs_ocr_reason,
    probe,
    recognize_image,
)
from dce.ingest.limits import Deadline, IngestLimits
from dce.ingest.ocr import LocalOcrProvider, ocr_pages_to_builder, provider_or_none
from dce.ingest.result import IngestResult, IngestStatus, TextSource
from dce.ingest.settings import IngestSettings, get_ingest_settings


class IngestOptions(BaseModel):
    """Per-request ingestion options — the one new field on the API request model.

    Deliberately small, and deliberately unable to *raise* this deployment's capabilities:

    * ``filename`` is a hint for plain-text subtypes only. It can never choose a binary
      parser (see :mod:`dce.ingest.detect`).
    * ``local_ocr`` can turn local OCR **off** for one request, but cannot turn it on where
      the operator has not enabled it. Whether an unclassified document may be run through a
      recognition engine, and whether this deployment stands behind that engine's accuracy,
      are operator decisions; a caller flag that could switch them on would make the default
      meaningless.
    """

    filename: str | None = Field(default=None, max_length=512)
    local_ocr: bool | None = None


def _resolve_provider(
    settings: IngestSettings, requested: bool | None
) -> tuple[LocalOcrProvider | None, bool]:
    """``(provider, enabled_on_this_deployment)``."""
    if not settings.local_ocr_enabled:
        return None, False
    if requested is False:
        return None, True
    return (
        provider_or_none(settings.local_ocr_engine, languages=settings.local_ocr_languages),
        True,
    )


def _why_no_engine(enabled: bool, requested: bool | None) -> str:
    """The clause that distinguishes the three ways ``ocr_available`` can be False.

    "We cannot read this" is only actionable if the caller can tell *why* nothing recognised
    it: an operator who has to go turn a setting on, an operator who has to go install an
    extra, and a caller who turned it off themselves need three different next steps.
    """
    if not enabled:
        return ". Local OCR is switched off on this deployment"
    if requested is False:
        return ". Local OCR is available here but this request declined it (local_ocr=false)"
    return (
        ". Local OCR is switched on here but its engine is not installed — see the "
        "ocr-rapidocr / ocr-tesseract extras"
    )


def _parse_native(
    data: bytes,
    media_type: MediaType,
    builder: LayoutBuilder,
    limits: IngestLimits,
    deadline: Deadline,
) -> int:
    """Run the parser for a text-bearing format. Returns the page count."""
    # Imported here, not at module scope: each of these pulls in a stdlib parser, and a
    # deployment that only ever sees PDFs should not pay for the XML machinery at import.
    if media_type in {MediaType.docx, MediaType.xlsx, MediaType.pptx, MediaType.odt}:
        from dce.ingest import ooxml
        from dce.ingest.zipsafe import open_archive

        parsers = {
            MediaType.docx: ooxml.parse_docx,
            MediaType.xlsx: ooxml.parse_xlsx,
            MediaType.pptx: ooxml.parse_pptx,
            MediaType.odt: ooxml.parse_odt,
        }
        with open_archive(data, limits, deadline) as archive:
            return parsers[media_type](archive, builder, limits, deadline)

    if media_type is MediaType.msg:
        from dce.ingest.cfb import parse_msg

        parse_msg(data, builder, limits, deadline)
        return 1

    if media_type is MediaType.eml:
        from dce.ingest.markup import parse_eml

        parse_eml(data, builder, limits, deadline)
        return 1

    if media_type is MediaType.rtf:
        from dce.ingest.plain import parse_rtf

        parse_rtf(data, builder, limits, deadline)
        return 1

    text, _encoding = decode_text(data)
    if media_type is MediaType.html:
        from dce.ingest.markup import parse_html

        parse_html(text, builder, limits, deadline)
    elif media_type is MediaType.csv:
        from dce.ingest.plain import parse_csv

        parse_csv(text, builder, limits, deadline)
    else:
        from dce.ingest.plain import parse_txt

        parse_txt(text, builder, deadline)
    return 1


def ingest(
    data: bytes,
    *,
    doc_id: str = "",
    filename: str | None = None,
    limits: IngestLimits | None = None,
    settings: IngestSettings | None = None,
    local_ocr: bool | None = None,
) -> IngestResult:
    """Turn an uploaded file into a :class:`~dce.models.LayoutView`, or say why not.

    Args:
        data: The whole file.
        doc_id: Carried onto the resulting view.
        filename: Hint for plain-text subtypes only; never chooses a binary parser.
        limits: Resource caps; the deployment's configured limits by default.
        settings: Ingestion settings; :func:`~dce.ingest.settings.get_ingest_settings` by
            default.
        local_ocr: ``False`` to suppress local OCR for this request. ``True`` requests it but
            cannot enable it where the deployment has not.

    Returns:
        An :class:`~dce.ingest.result.IngestResult`, with ``status`` either ``ok`` (``view``
        is populated) or ``needs_ocr`` (there was no text to read).

    Raises:
        IngestError: Any subclass, for an upload that could not be parsed. Never anything
            else — a parser's internal exception is translated at its own boundary.
    """
    started = time.perf_counter()
    settings = settings or get_ingest_settings()
    limits = limits or settings.limits()

    if len(data) > limits.max_bytes:
        raise PayloadTooLarge(
            f"upload is {len(data)} bytes, over the {limits.max_bytes}-byte cap"
        )
    deadline = Deadline(limits.max_seconds)
    detection = detect(data, filename=filename, limits=limits, deadline=deadline)
    media_type = detection.media_type
    provider, ocr_enabled = _resolve_provider(settings, local_ocr)

    builder = LayoutBuilder(limits, deadline)
    result = IngestResult(
        media_type=media_type,
        detected_by=detection.basis,
        byte_size=len(data),
        ocr_available=ocr_enabled and provider is not None,
    )

    # -- images: the decision point -----------------------------------------
    if media_type in IMAGE_TYPES:
        info = probe(data, media_type)
        result.page_count = info.frames
        if provider is None:
            result.status = IngestStatus.needs_ocr
            result.text_source = TextSource.none
            result.reason = needs_ocr_reason(media_type, info) + _why_no_engine(
                ocr_enabled, local_ocr
            )
            result.remedy = NEEDS_OCR_REMEDY
            result.ms = int((time.perf_counter() - started) * 1000)
            return result
        pages, truncated = recognize_image(data, media_type, provider, limits, deadline)
        ocr_pages_to_builder(pages, builder, limits)
        result.pages_read = len(pages)
        result.text_source = TextSource.local_ocr
        result.ocr_engine = provider.name
        if truncated:
            builder.truncated = True
            builder.limits_hit.append("max_ocr_pages")

    # -- PDF: text layer, or a scan --------------------------------------------
    elif media_type is MediaType.pdf:
        from dce.ingest.pdf import parse_pdf, scanned_reason

        outcome = parse_pdf(data, builder, limits, deadline, provider=provider)
        result.page_count = outcome.page_count
        result.pages_read = outcome.pages_read
        if outcome.truncated:
            builder.truncated = True
            builder.limits_hit.append("max_pages")
        if outcome.needs_ocr:
            result.status = IngestStatus.needs_ocr
            result.text_source = TextSource.none
            result.reason = scanned_reason(outcome) + _why_no_engine(ocr_enabled, local_ocr)
            result.remedy = NEEDS_OCR_REMEDY
            result.ms = int((time.perf_counter() - started) * 1000)
            return result
        if outcome.ocr_pages:
            ocr_pages_to_builder(outcome.ocr_pages, builder, limits)
            result.text_source = TextSource.local_ocr
            result.ocr_engine = provider.name if provider else ""

    # -- everything else: native text ------------------------------------------
    else:
        result.page_count = _parse_native(data, media_type, builder, limits, deadline)
        result.pages_read = result.page_count

    view = builder.build(
        doc_id=doc_id,
        raw={
            "provider": "dce.ingest",
            "media_type": str(media_type),
            "detected_by": detection.basis,
            "detection_detail": detection.detail,
            "text_source": str(result.text_source),
            "ocr_engine": result.ocr_engine,
            "truncated": builder.truncated,
        },
    )
    result.view = view
    result.block_count = builder.block_count
    result.char_count = builder.char_count
    result.truncated = builder.truncated
    result.limits_hit = list(builder.limits_hit)
    result.page_count = result.page_count or len(view.pages)
    result.ms = int((time.perf_counter() - started) * 1000)

    if not view.blocks and not view.tables:
        # A parseable file with no text at all. Not an abstention and not a scan: the file
        # really is empty, and saying so beats classifying nothing and reporting "unknown".
        raise UnsupportedFormat(
            f"{media_type} parsed cleanly but contains no text — an empty document cannot "
            "be classified, and an empty classification would read as a model decision"
        )
    return result


__all__ = ["IngestError", "IngestOptions", "ingest"]
