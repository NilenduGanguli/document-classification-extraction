"""PDF — the format that is two formats.

A PDF with a text layer is native text: the publisher's own characters, exactly as
authoritative as a DOCX's. A PDF whose pages are each one scanned picture is an image in a
different container, and it belongs on the ``needs_ocr`` path with the JPEGs. The same file
extension covers both, so the distinction is measured rather than assumed: if the extracted
text has fewer than :data:`MIN_ALNUM_CHARS` alphanumeric characters across the pages read,
there is no text layer worth classifying on.

**Zones: everything is body.** A PDF's text layer carries fonts and positions, not roles.
Deriving "this line is large and near the top of page 1, so it is the title" is inference,
and the reference corpus harness does exactly that inference *and labels it as an
approximation* for precisely that reason. Doing it silently inside the service, where it
would feed a 3.0x title weight, is the kind of manufactured evidence
``dce.config.zone_weight_title`` was written to warn about. So this parser reports what the
file states, which is nothing, and a caller who wants production-faithful zones sends an
Azure Document Intelligence payload — which is what ``azure_analyze_result`` is for.

PyMuPDF is an optional extra (``.[pdf]``), not a base dependency: a deployment whose callers
all send ``layout`` payloads should not carry a PDF engine in its image.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dce.ingest.builder import LayoutBuilder
from dce.ingest.errors import EngineUnavailable, MalformedDocument
from dce.ingest.limits import Deadline, IngestLimits
from dce.ingest.ocr import LocalOcrProvider, OcrPage
from dce.models import Zone

#: Below this many alphanumeric characters, the "text layer" is a scanning artefact — a
#: page number, a stray watermark — not text. Matches the corpus harness's own floor so the
#: two agree about which documents are scans.
MIN_ALNUM_CHARS = 40


@dataclass
class PdfOutcome:
    """What the PDF turned out to be."""

    page_count: int = 0
    pages_read: int = 0
    alnum_chars: int = 0
    needs_ocr: bool = False
    truncated: bool = False
    #: Populated only when a local engine ran.
    ocr_pages: list[OcrPage] = field(default_factory=list)


def _import_fitz():
    try:
        import fitz

        return fitz
    except ImportError as exc:
        raise EngineUnavailable(
            "PDF ingestion needs PyMuPDF, which is not installed. Install the optional "
            "extra: pip install '.[pdf]'. A caller that already has a layout payload does "
            "not need it — send 'layout' or 'azure_analyze_result' instead."
        ) from exc


def _count_alnum(text: str) -> int:
    return sum(1 for char in text if char.isalnum())


def parse_pdf(
    data: bytes,
    builder: LayoutBuilder,
    limits: IngestLimits,
    deadline: Deadline,
    *,
    provider: LocalOcrProvider | None = None,
) -> PdfOutcome:
    """Read a PDF's text layer, or report that it has none.

    Args:
        data: The file.
        builder: Where blocks go.
        limits: Caps; ``max_pages`` truncates, it does not refuse.
        deadline: Wall clock, checked per page.
        provider: A local OCR engine. When supplied and the text layer is empty, the pages
            are rasterised and recognised in-process rather than returning ``needs_ocr``.

    Returns:
        A :class:`PdfOutcome`. ``needs_ocr`` is True for a scan when no provider was given.

    Raises:
        EngineUnavailable: PyMuPDF is not installed.
        MalformedDocument: The bytes are not a readable PDF, or it is encrypted.
    """
    fitz = _import_fitz()
    deadline.check("pdf.open")
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise MalformedDocument(f"PDF cannot be opened: {exc}") from exc

    outcome = PdfOutcome()
    with document:
        if document.needs_pass:
            raise MalformedDocument(
                "PDF is password-protected; nothing in this process can read it"
            )
        outcome.page_count = document.page_count
        limit = min(limits.max_pages, document.page_count)
        outcome.truncated = limit < document.page_count

        page_texts: list[str] = []
        for index in range(limit):
            deadline.check(f"pdf.page{index + 1}")
            page = document.load_page(index)
            rect = page.rect
            builder.page(
                index + 1, width=float(rect.width), height=float(rect.height), unit="point"
            )
            try:
                text = page.get_text("text") or ""
            except Exception:  # noqa: BLE001 - a broken page costs that page, not the file
                text = ""
            page_texts.append(text)
        outcome.pages_read = limit
        outcome.alnum_chars = sum(_count_alnum(t) for t in page_texts)

        if outcome.alnum_chars >= MIN_ALNUM_CHARS:
            for index, text in enumerate(page_texts):
                builder.lines(text, zone=Zone.body, page=index + 1)
            return outcome

        # No text layer. Either recognise it here, or say so.
        if provider is None:
            outcome.needs_ocr = True
            return outcome

        pages = min(limits.max_ocr_pages, limit)
        outcome.truncated = outcome.truncated or pages < limit
        for index in range(pages):
            deadline.check(f"pdf.ocr.page{index + 1}")
            page = document.load_page(index)
            try:
                pixmap = page.get_pixmap(dpi=limits.ocr_dpi)
                png = pixmap.tobytes("png")
            except Exception as exc:
                raise MalformedDocument(f"page {index + 1} cannot be rasterised: {exc}") from exc
            outcome.ocr_pages.append(provider.recognize(png, page=index + 1, deadline=deadline))
    return outcome


def scanned_reason(outcome: PdfOutcome) -> str:
    """The sentence a caller sees for a scanned PDF."""
    return (
        f"PDF has no usable text layer ({outcome.alnum_chars} alphanumeric characters across "
        f"{outcome.pages_read} page(s) of {outcome.page_count}) — its pages are images, so "
        "classifying it would require optical recognition"
    )


__all__ = ["MIN_ALNUM_CHARS", "PdfOutcome", "parse_pdf", "scanned_reason"]
