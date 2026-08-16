"""One file, several documents: where each one starts, and what each one is.

A KYC upload is routinely a bundle — a passport, two utility bills and a bank statement
concatenated into one PDF. Classifying that as a whole answers the wrong question.

**The approach here is boundaries first, classification second, and the order is the whole
design.** The obvious alternative — classify every page, then group runs of equal doctype —
was built, measured, and rejected. Against the real 129-doctype registry on 78 corpus
documents that are each a *single* document (so the correct output is 78 segments) it emitted
**791**: 86% of documents split, 58% given a segment naming a doctype that was not theirs,
and precision when answered fell from **100% to 71.3%**. A real "How to read your statement"
back page classifies as ``ca_drivers_license`` at 0.625 confidence.

The cause is not a bug to fix. The cascade's accept gates — the per-class floor, the pairwise
separation margin, coverage — were reasoned about against *whole-document* evidence, and one
page of a bank statement carries a fraction of it. Page-scope classification is a different
instrument that was never calibrated.

So this module never classifies a page. It proposes boundaries from **structural** evidence
that costs no classification at all, and then hands each candidate span to
:func:`~dce.classify.cascade.classify` **whole** — the scope where the cascade's precision was
measured and where it holds.

Two consequences worth stating plainly:

* **A file with no boundary evidence yields one span**, which is exactly today's behaviour and
  today's numbers. Segmentation can only ever *add* structure where evidence supports it.
* **Splitting happens only on positive evidence.** An abstaining page is not a boundary. A
  blank page is not a boundary. Silence continues the current span. This is deliberate and
  asymmetric: a missed split leaves one answer that is merely incomplete, while a false split
  turns one correct classification into two thinner-evidence ones and then runs extraction
  against each. That asymmetry is how 100% precision became 71.3%, and it is why the default
  everywhere here is *do not split*.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise

from dce.classify.cascade import Segment, classify, load_registry, page_range_view
from dce.classify.profiles import ProfileSet, build_profiles
from dce.config import Settings, get_settings
from dce.models import Controls, DocTypeSpec, LayoutView

#: Controls whose anchors mark the FIRST page of a document and essentially never a later one.
#:
#: A form number, a control number and an MRZ prefix are printed once, at the head of the
#: instrument. Meeting one part-way through a bundle is strong evidence a new document has
#: started. The weaker tiers are deliberately excluded: ``ISSUER_NAME`` repeats in a running
#: header on every page, ``STATUTE_TITLE`` and ``ISSUER_TEMPLATE`` recur in body prose, and
#: ``CLASS_NAME_UNCONTESTED`` is documented in the registry as the tier of *no evidence yet*.
#: Using any of them here would manufacture boundaries out of page furniture.
FIRST_PAGE_CONTROLS = frozenset(
    {Controls.FORM_NUMBER, Controls.CONTROL_NUMBER, Controls.MRZ_PREFIX}
)

#: Relative difference in page width or height above which two pages are different stock.
#: A passport scan against an A4 utility bill clears this comfortably; the ordinary jitter
#: between pages of one scanned document does not.
GEOMETRY_TOLERANCE = 0.02

#: Image coverage at or above which a page is a picture rather than a page carrying one.
#: Matches :data:`dce.ingest.pdf.MAX_IMAGE_FRACTION` — the same question, asked of the same
#: measurement, so the two cannot drift into disagreeing about what a scanned page is.
IMAGE_DOMINANCE = 0.6


@dataclass(frozen=True)
class Boundary:
    """A proposed split between ``page`` - 1 and ``page``, and what proposed it."""

    #: First page of the NEW document, 1-based.
    page: int
    #: Machine-readable signal name: ``adequacy`` | ``geometry`` | ``first_page_anchor``.
    signal: str
    #: One sentence a reviewer can read.
    detail: str


def _norm(text: str) -> str:
    return " ".join(text.upper().split())


@lru_cache(maxsize=1)
def _first_page_markers(spec_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Normalised anchor strings that mark a document's first page.

    Cached on the registry's id tuple rather than rebuilt per call: this walks every anchor of
    every doctype, and a bundle asks for it once per boundary check.
    """
    markers: set[str] = set()
    for spec in load_registry():
        if spec.doctype_id not in spec_ids:
            continue
        for anchor in spec.anchors:
            if getattr(anchor, "controls", None) in FIRST_PAGE_CONTROLS:
                markers.add(_norm(anchor.text))
    return tuple(sorted(markers))


def _page_text(view: LayoutView, page: int) -> str:
    parts = [b.text for b in view.blocks if b.page == page]
    parts.extend(kv.value for kv in view.key_values if kv.page == page)
    return _norm(" ".join(parts))


def _geometry_differs(view: LayoutView, left: int, right: int) -> tuple[bool, str]:
    pages = {p.page: p for p in view.pages}
    a, b = pages.get(left), pages.get(right)
    if a is None or b is None or not (a.width and a.height and b.width and b.height):
        return False, ""
    for name, x, y in (("width", a.width, b.width), ("height", a.height, b.height)):
        if max(x, y) <= 0:
            continue
        if abs(x - y) / max(x, y) > GEOMETRY_TOLERANCE:
            return True, f"page {right} changes {name} from {x:g} to {y:g}"
    return False, ""


def _adequacy_differs(view: LayoutView, left: int, right: int) -> tuple[bool, str]:
    """A page read one way followed by a page read another way.

    Returns False when either page was never measured. ``text_adequate is None`` means nothing
    looked, which is not a finding and must not be read as one — a payload that came from a
    caller-supplied layout has no verdicts at all, and inventing boundaries from their absence
    would split every such document at every page.
    """
    pages = {p.page: p for p in view.pages}
    a, b = pages.get(left), pages.get(right)
    if a is None or b is None:
        return False, ""
    both_measured = a.text_adequate is not None and b.text_adequate is not None
    if both_measured and a.text_adequate != b.text_adequate:
        was = "carried usable text" if a.text_adequate else "was a picture"
        now = "carries usable text" if b.text_adequate else "is a picture"
        return True, f"page {left} {was} and page {right} {now}"
    dominant_before = a.image_fraction >= IMAGE_DOMINANCE
    dominant_after = b.image_fraction >= IMAGE_DOMINANCE
    if dominant_before != dominant_after:
        return True, (
            f"image coverage crosses {IMAGE_DOMINANCE:.0%} between pages {left} and {right} "
            f"({a.image_fraction:.0%} then {b.image_fraction:.0%})"
        )
    return False, ""


def _first_page_anchor(
    view: LayoutView, specs: list[DocTypeSpec], left: int, right: int
) -> tuple[bool, str]:
    """A first-page-only marker appearing on ``right`` and not on ``left``.

    The ``and not on left`` half matters: a marker present on both pages is a running header,
    not the head of a new instrument, and treating it as a boundary would split a document at
    every page of itself.
    """
    markers = _first_page_markers(tuple(s.doctype_id for s in specs))
    if not markers:
        return False, ""
    before, after = _page_text(view, left), _page_text(view, right)
    for marker in markers:
        if marker and marker in after and marker not in before:
            return True, f"page {right} carries {marker!r}, which marks a document's first page"
    return False, ""


def candidate_boundaries(
    view: LayoutView, specs: Iterable[DocTypeSpec] | None = None
) -> list[Boundary]:
    """Where a new document may start, on structural evidence alone.

    No page is classified here. Every signal is either a measurement ingestion already took
    or a substring test, so proposing boundaries across a 200-page bundle costs no
    classifications at all.
    """
    spec_list = list(specs) if specs is not None else load_registry()
    pages = sorted({p.page for p in view.pages} | {b.page for b in view.blocks})
    found: list[Boundary] = []
    for left, right in pairwise(pages):
        for signal, test in (
            ("adequacy", _adequacy_differs(view, left, right)),
            ("geometry", _geometry_differs(view, left, right)),
            ("first_page_anchor", _first_page_anchor(view, spec_list, left, right)),
        ):
            hit, detail = test
            if hit:
                found.append(Boundary(page=right, signal=signal, detail=detail))
                break
    return found


def segment_document(
    view: LayoutView,
    specs: Iterable[DocTypeSpec] | None = None,
    *,
    settings: Settings | None = None,
    profiles: ProfileSet | None = None,
) -> tuple[list[Segment], list[Boundary]]:
    """Split ``view`` into documents and classify each one whole.

    Returns:
        ``(segments, boundaries)``. A payload with no boundary evidence returns exactly one
        segment covering everything, classified identically to a plain :func:`classify` call —
        so a caller needs no special case for the single-document file, and a single-document
        file loses nothing by being sent here.
    """
    resolved = settings if settings is not None else get_settings()
    spec_list = list(specs) if specs is not None else load_registry()
    shared = profiles or (build_profiles(spec_list) if spec_list else None)

    pages = sorted({p.page for p in view.pages} | {b.page for b in view.blocks})
    if not pages:
        return [], []

    boundaries = candidate_boundaries(view, spec_list)
    starts = {b.page for b in boundaries}

    spans: list[tuple[int, int]] = []
    span_start = pages[0]
    for previous, page in pairwise(pages):
        # A gap in the numbering ends a span for the same reason it ends a run: the missing
        # page is not evidence either way, and a span that covered it would claim a page it
        # was never shown.
        if page in starts or page != previous + 1:
            spans.append((span_start, previous))
            span_start = page
    spans.append((span_start, pages[-1]))

    classified = [
        (start, end, classify(page_range_view(view, start, end), spec_list,
                              settings=resolved, profiles=shared))
        for start, end in spans
    ]

    # Merge neighbours that classified the same. A boundary signal is a *proposal*; whole-span
    # classification is the check on it, and two adjacent spans agreeing on the doctype means
    # the proposal was a false positive — a change of page stock inside one document, an
    # attachment photographed at a different size. Merging re-classifies the union so the
    # surviving segment's evidence describes the pages it actually covers, rather than being
    # inherited from whichever half happened to come first.
    segments: list[Segment] = []
    index = 0
    while index < len(classified):
        start, end, result = classified[index]
        step = index + 1
        while (
            step < len(classified)
            and classified[step][2].doctype_id == result.doctype_id
            and classified[step][0] == end + 1
        ):
            end = classified[step][1]
            step += 1
        if step > index + 1:
            result = classify(
                page_range_view(view, start, end), spec_list,
                settings=resolved, profiles=shared,
            )
        result.page_types = [result.doctype_id] * (end - start + 1)
        segments.append(
            Segment(
                doctype_id=result.doctype_id,
                start_page=start,
                end_page=end,
                confidence=result.confidence,
                classification=result,
            )
        )
        index = step

    surviving = {s.start_page for s in segments}
    return segments, [b for b in boundaries if b.page in surviving]


__all__ = [
    "FIRST_PAGE_CONTROLS",
    "GEOMETRY_TOLERANCE",
    "IMAGE_DOMINANCE",
    "Boundary",
    "candidate_boundaries",
    "segment_document",
]
