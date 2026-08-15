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

import unicodedata
from dataclasses import dataclass, field

from dce.ingest.builder import LayoutBuilder
from dce.ingest.errors import EngineUnavailable, MalformedDocument
from dce.ingest.limits import Deadline, IngestLimits
from dce.ingest.ocr import LocalOcrProvider, OcrPage
from dce.models import Zone

#: Below this many alphanumeric characters, **one page's** text layer is a scanning artefact
#: — a page number, a stray watermark — not text.
#:
#: **Per page, and that is the whole point.** This floor was once compared against a sum
#: across every page read, which made the effective bar 40/N: two pages of "Scanned with
#: CamScanner" (21 characters each) cleared it, and a 200-page scan cleared it on 0.2
#: characters a page. Worse than mislabelling one document, clearing the sum took the
#: text-layer branch for *every* page, so a file that was one typed cover page in front of
#: nine photographed ones was classified on the cover page alone — with ``truncated`` False,
#: ``limits_hit`` empty and no reason given. A per-page floor cannot be bought by a page
#: other than the one being judged.
MIN_ALNUM_CHARS = 40

#: A page whose largest single image covers at least this fraction of it is a picture of a
#: document, whatever stray characters share the page. Compared against the *largest single*
#: placement rather than a sum or a union: one corpus page carries 3985 placements whose
#: areas sum well past the page while the largest covers 8.8% of it.
MAX_IMAGE_FRACTION = 0.6

#: Above this share of a single repeated character, a text layer is a previous OCR's failure
#: rather than text — ``lllllllll`` clears any length floor and anchors nothing.
MAX_REPEAT_RATIO = 0.9
#: Above this share of control or replacement characters, likewise.
MAX_CONTROL_RATIO = 0.5

#: Image placements examined per page. ``get_image_info()`` is unbounded and costs 50 ms on
#: the 3985-placement page above; the verdict only ever needs the largest, and a page whose
#: biggest image is not in the first 64 is not a scan.
MAX_IMAGE_PLACEMENTS = 64

#: Unicode general categories that mean "not a character a reader sees".
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Cs"})


@dataclass(frozen=True)
class PageVerdict:
    """Whether one page's text layer is worth classifying on, and why not.

    Recorded per page rather than reduced to a document-wide boolean because the answer
    differs per page in exactly the documents that matter — and because a caller who ends up
    with less than the whole file is owed the page numbers.
    """

    #: 1-based, matching every other page number the service reports.
    page: int
    alnum: int
    adequate: bool
    #: Empty when adequate; otherwise one clause naming which signal failed.
    reason: str = ""
    #: Share of the page covered by its largest single image, 0.0 when it carries none.
    image_fraction: float = 0.0

    @property
    def recoverable(self) -> bool:
        """Whether recognising this page could add anything.

        The distinction between "we could not read this page" and "there is nothing on this
        page to read". A sparse page carrying no image — a cover sheet with a two-word title,
        a deliberately near-blank continuation page — is not a scan and OCR would return the
        same nothing more slowly. Only a page with pixels on it is worth escalating for, and
        only such a page represents content actually lost when nothing can read it.
        """
        return self.image_fraction > 0.0


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
    #: True when ``max_ocr_pages`` stopped a local engine short of the pages that needed it.
    #: Tracked apart from :attr:`truncated` so the cap that bit can be named: "we read fewer
    #: pages than the file has" and "we recognised fewer pages than needed recognising" send a
    #: reader to different settings. Never set on the OCR-service path, which hands the file
    #: over whole and is bounded by the block and character caps instead.
    ocr_truncated: bool = False
    #: One entry per page **read** — not per page in the file. ``max_pages`` truncation is
    #: reported separately, so this list must never be read as covering the whole document.
    page_verdicts: list[PageVerdict] = field(default_factory=list)

    @property
    def inadequate_pages(self) -> tuple[int, ...]:
        """Pages read whose text layer is not worth classifying on, in order."""
        return tuple(v.page for v in self.page_verdicts if not v.adequate)

    @property
    def unread_pages(self) -> tuple[int, ...]:
        """Inadequate pages that carry an image — the ones with content actually unread.

        The subset of :attr:`inadequate_pages` worth escalating for. A sparse page with no
        image is inadequate but holds nothing to recover, so counting it as lost content
        would report a loss that did not happen.
        """
        return tuple(v.page for v in self.page_verdicts if not v.adequate and v.recoverable)

    @property
    def all_pages_inadequate(self) -> bool:
        """Every page read is a scan — the document is one, not partly one."""
        return bool(self.page_verdicts) and not any(v.adequate for v in self.page_verdicts)


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


def _largest_image_fraction(fitz, page) -> float:
    """Share of the page covered by its largest single image placement, 0.0 to 1.0.

    Three corrections that a naive version gets wrong, each found on a real corpus page:

    * **Clip to the page.** An image's bbox may extend past it; one corpus page yields 1.165
      uncorrected, which is not a fraction of anything.
    * **Largest, not sum, not union.** 3985 placements on one page sum to nonsense; the union
      bounding box of scattered small images covers the page while none of them is a scan.
    * **``page.rect`` is the CropBox**, which is also what MuPDF clips text to, so both halves
      of the comparison are measured against the same rectangle.

    Returns 0.0 rather than raising: a page whose images cannot be enumerated is judged on
    its characters alone, which is the pre-existing behaviour and never worse than it.
    """
    rect = page.rect
    area = float(rect.width) * float(rect.height)
    if area <= 0:
        return 0.0
    try:
        placements = page.get_image_info()
    except Exception:  # noqa: BLE001 - a page that will not enumerate costs the signal, not the file
        return 0.0
    largest = 0.0
    for info in placements[:MAX_IMAGE_PLACEMENTS]:
        bbox = info.get("bbox")
        if not bbox:
            continue
        clipped = fitz.Rect(bbox) & rect
        if clipped.is_empty:
            continue
        largest = max(largest, (clipped.width * clipped.height) / area)
    return largest


def _glyph_ratios(text: str) -> tuple[float, float]:
    """``(most-repeated-character share, control-character share)`` over non-space glyphs."""
    glyphs = [char for char in text if not char.isspace()]
    if not glyphs:
        return 0.0, 0.0
    total = len(glyphs)
    counts: dict[str, int] = {}
    control = 0
    for char in glyphs:
        counts[char] = counts.get(char, 0) + 1
        if char == "�" or unicodedata.category(char) in _CONTROL_CATEGORIES:
            control += 1
    return max(counts.values()) / total, control / total


def _page_verdict(
    fitz, page, page_number: int, text: str, *, strict: bool = True
) -> PageVerdict:
    """Judge one page's text layer against every signal, cheapest first.

    Ordered so the common cases cost nothing extra: a page with plenty of clean characters
    returns before any image is enumerated, and a page with none returns before any glyph is
    counted. Only a page that is genuinely ambiguous pays for the whole predicate.
    """
    alnum = _count_alnum(text)
    fraction = _largest_image_fraction(fitz, page)

    def verdict(adequate: bool, reason: str = "") -> PageVerdict:
        return PageVerdict(
            page=page_number,
            alnum=alnum,
            adequate=adequate,
            reason=reason,
            image_fraction=fraction,
        )

    if alnum < MIN_ALNUM_CHARS:
        return verdict(
            False,
            f"{alnum} alphanumeric characters, below the {MIN_ALNUM_CHARS}-character floor",
        )
    if not strict:
        # `trust`: the page has characters, so they are the page. The remaining signals ask
        # whether those characters are *worth* anything, and this policy has said not to ask.
        return verdict(True)
    repeat_ratio, control_ratio = _glyph_ratios(text)
    if repeat_ratio > MAX_REPEAT_RATIO:
        return verdict(
            False,
            f"{repeat_ratio:.0%} of its characters are one repeated glyph, which is a "
            "previous recogniser's failure rather than text",
        )
    if control_ratio > MAX_CONTROL_RATIO:
        return verdict(
            False,
            f"{control_ratio:.0%} of its characters are control or replacement glyphs",
        )
    if fraction >= MAX_IMAGE_FRACTION:
        return verdict(
            False,
            f"one image covers {fraction:.0%} of the page, so its {alnum} characters "
            "caption a picture rather than constitute the page",
        )
    return verdict(True)


def parse_pdf(
    data: bytes,
    builder: LayoutBuilder,
    limits: IngestLimits,
    deadline: Deadline,
    *,
    provider: LocalOcrProvider | None = None,
    strict: bool = True,
    force_ocr: bool = False,
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
            outcome.page_verdicts.append(
                _page_verdict(fitz, page, index + 1, text, strict=strict)
            )
        outcome.pages_read = limit
        outcome.alnum_chars = sum(_count_alnum(t) for t in page_texts)

        def emit_text_layer() -> None:
            for index, text in enumerate(page_texts):
                builder.lines(text, zone=Zone.body, page=index + 1)

        # `always_ocr`: the deployment has said its text layers are not evidence. Recognise
        # every page and use none of the characters the file came with — anything less would
        # leave the policy true of some pages and false of others.
        if force_ocr and provider is not None:
            pages = min(limits.max_ocr_pages, limit)
            outcome.ocr_truncated = pages < limit
            _recognize_pages(document, range(pages), provider, limits, deadline, outcome)
            return outcome

        if not outcome.inadequate_pages:
            emit_text_layer()
            return outcome

        # Nothing on any thin page but the thinness itself. A three-page note with fifteen
        # characters a page is not a scan: there are no pixels to recognise, so recognising
        # it would return the same nothing more slowly, and calling it needs_ocr would send a
        # caller looking for an OCR engine that could not have helped. Take the text as it is.
        #
        # This test comes BEFORE the all-pages-inadequate one deliberately. Reversing them
        # routes every sparse text document to OCR, which is how a fix for silent loss turns
        # into an unnecessary bill and a worse answer.
        lost = outcome.unread_pages
        if not lost:
            emit_text_layer()
            return outcome

        # Every page read is a scan: the document IS one, and the whole-document answer —
        # recognise it, or say we cannot — is the right one.
        if outcome.all_pages_inadequate:
            if provider is None:
                outcome.needs_ocr = True
                return outcome
            pages = min(limits.max_ocr_pages, limit)
            outcome.ocr_truncated = pages < limit
            _recognize_pages(document, range(pages), provider, limits, deadline, outcome)
            return outcome

        # A MIXED document: some pages carry text, some are pictures. Keep the text pages'
        # own characters and recognise only the pages that have none.
        #
        # Merging is safe *here* specifically because both halves land in the same zone: a PDF
        # text layer is all Zone.body by deliberate refusal to infer roles (see the module
        # docstring), and ocr_pages_to_builder writes every recognised line as Zone.body too.
        # Neither half can satisfy a zone-gated anchor the other cannot, so no anchor fires
        # because of which pages happened to be scanned. The OCR *service* path is different —
        # azure_layout returns roles — which is why pipeline.ingest sends a mixed document to a
        # service whole rather than reaching this branch at all.
        emit_text_layer()

        if provider is None:
            # Nothing can read the picture pages. Emit what the text pages hold and DECLARE
            # the loss: the alternative is this function's oldest bug, where the file came
            # back status=ok, truncated=False and short by most of its content.
            outcome.truncated = True
            return outcome

        recognisable = [p for p in lost if p <= limits.max_ocr_pages]
        outcome.ocr_truncated = len(recognisable) < len(lost)
        _recognize_pages(
            document, (p - 1 for p in recognisable), provider, limits, deadline, outcome
        )
        return outcome


def _recognize_pages(
    document,
    indices,
    provider: LocalOcrProvider,
    limits: IngestLimits,
    deadline: Deadline,
    outcome: PdfOutcome,
) -> None:
    """Rasterise the given 0-based page indices and hand each to a local engine."""
    for index in indices:
        deadline.check(f"pdf.ocr.page{index + 1}")
        page = document.load_page(index)
        try:
            pixmap = page.get_pixmap(dpi=limits.ocr_dpi)
            png = pixmap.tobytes("png")
        except Exception as exc:
            raise MalformedDocument(f"page {index + 1} cannot be rasterised: {exc}") from exc
        outcome.ocr_pages.append(provider.recognize(png, page=index + 1, deadline=deadline))


def scanned_reason(outcome: PdfOutcome) -> str:
    """The sentence a caller sees for a scanned PDF."""
    return (
        f"PDF has no usable text layer ({outcome.alnum_chars} alphanumeric characters across "
        f"{outcome.pages_read} page(s) of {outcome.page_count}) — its pages are images, so "
        "classifying it would require optical recognition"
    )


def partial_reason(outcome: PdfOutcome) -> str:
    """The sentence for a document whose text pages were kept and picture pages were not.

    Named separately from :func:`scanned_reason` because it describes a different situation
    with a different remedy: the caller has a usable classification in hand that is missing
    part of its evidence, rather than no classification at all.
    """
    pages = outcome.inadequate_pages
    listed = ", ".join(str(p) for p in pages[:10]) + ("…" if len(pages) > 10 else "")
    detail = outcome.page_verdicts[pages[0] - 1].reason if pages else ""
    return (
        f"page(s) {listed} of {outcome.page_count} carry no usable text ({detail}) and no "
        "recogniser is available here, so this document was classified on its remaining "
        f"{outcome.pages_read - len(pages)} page(s). The pages not read may hold the content "
        "that identifies it"
    )


__all__ = [
    "MAX_IMAGE_FRACTION",
    "MIN_ALNUM_CHARS",
    "PageVerdict",
    "PdfOutcome",
    "parse_pdf",
    "partial_reason",
    "scanned_reason",
]
