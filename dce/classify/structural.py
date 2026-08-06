"""L0 — structural prior.

The cheapest tier, and the one nobody writes because it feels too obvious: before reading a
single word, the *shape* of the document already excludes most of the registry. A 12-page
scanned bundle full of tables is not a driving licence. A single landscape page with two
paragraphs and no tables is not an Acta Constitutiva.

This tier never decides anything on its own. It produces a small, bounded log-prior per
doctype which enters the fusion as ``log P(c|structure)``, so it can break a tie and can
suppress an absurd candidate, but three points of lexical evidence will always outvote it.

Every feature here comes free with the layout payload we were handed — no extra parsing, no
image access, no second pass over the text.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from itertools import pairwise

from dce.models import Category, DocTypeSpec, LayoutView, Zone

__all__ = [
    "StructuralFeatures",
    "structural_features",
    "structural_log_priors",
]

#: An ICAO 9303 TD3 line: 44 characters from the MRZ alphabet. Two of these stacked is a
#: passport data page; one is enough to call the *shape* present.
_TD3_LINE_RE = re.compile(r"[A-Z0-9<]{30,44}")
#: The passport line-1 prefix — ``P`` + subtype/filler + 3-letter issuing state.
_MRZ_PREFIX_RE = re.compile(r"\bP[<K][A-Z]{3}")

#: Bounds on the prior. Structure is a hint, never a verdict.
_PRIOR_FLOOR = -2.0
_PRIOR_CEILING = 1.5


@dataclass(frozen=True)
class StructuralFeatures:
    """Geometry- and count-derived features of a layout payload.

    Attributes:
        page_count: Number of pages.
        aspect_ratio: ``width / height`` of page 1 (0.0 when page 1 has no dimensions).
        landscape: Whether page 1 is wider than tall — ID cards are, forms are not.
        mark_count: Selection marks (checkboxes/radios) across the document.
        selected_mark_count: Marks whose state is ``selected``.
        table_count: Detected tables.
        kv_count: Provider-detected key/value pairs.
        block_count: Text blocks.
        char_count: Total characters of text.
        title_block_count: Blocks in the title/heading zones — a proxy for "is this a form".
        digital: Whether the payload looks born-digital rather than scanned.
        has_mrz_shape: Whether an MRZ-shaped run of characters is present anywhere.
    """

    page_count: int = 0
    aspect_ratio: float = 0.0
    landscape: bool = False
    mark_count: int = 0
    selected_mark_count: int = 0
    table_count: int = 0
    kv_count: int = 0
    block_count: int = 0
    char_count: int = 0
    title_block_count: int = 0
    digital: bool = False
    has_mrz_shape: bool = False

    def as_dict(self) -> dict[str, float | int | bool]:
        """Return the features as a plain dict (for evidence payloads and logs)."""
        return dict(asdict(self))


def structural_features(view: LayoutView) -> StructuralFeatures:
    """Derive :class:`StructuralFeatures` from a layout payload.

    Args:
        view: The provider-neutral layout view.

    Returns:
        The features. Missing page geometry degrades to zeros rather than raising — a
        partially-understood payload is still worth classifying.
    """
    first = view.pages[0] if view.pages else None
    aspect = 0.0
    if first is not None and first.height:
        aspect = round(float(first.width) / float(first.height), 4)

    text = view.text()
    upper = text.upper()
    has_mrz = bool(_MRZ_PREFIX_RE.search(upper)) or _looks_like_td3(upper)

    # Born-digital PDFs come back from the layout provider in inches with a zero skew angle;
    # rasterised scans come back in pixels and usually carry a non-zero angle.
    digital = bool(first is not None and first.unit == "inch" and abs(first.angle) < 0.5)

    return StructuralFeatures(
        page_count=view.page_count,
        aspect_ratio=aspect,
        landscape=aspect > 1.05,
        mark_count=len(view.marks),
        selected_mark_count=sum(1 for m in view.marks if m.selected),
        table_count=len(view.tables),
        kv_count=len(view.key_values),
        block_count=len(view.blocks),
        char_count=len(text),
        title_block_count=sum(
            1 for b in view.blocks if b.zone in (Zone.title, Zone.heading)
        ),
        digital=digital,
        has_mrz_shape=has_mrz,
    )


def _looks_like_td3(upper_text: str) -> bool:
    """Whether two consecutive MRZ-alphabet runs appear on adjacent lines."""
    runs = [
        bool(_TD3_LINE_RE.fullmatch(line.replace(" ", "")))
        for line in upper_text.splitlines()
    ]
    return any(a and b for a, b in pairwise(runs))


def _wants_mrz(spec: DocTypeSpec) -> bool:
    """Whether this doctype declares a machine-readable zone."""
    if any("P<" in p or "MRZ" in p.upper() for p in spec.id_patterns):
        return True
    return any(
        (f.validator or "").startswith("mrz") or "mrz" in f.locators for f in spec.fields
    )


def _wants_marks(spec: DocTypeSpec) -> bool:
    """Whether this doctype declares checkbox-bound fields (i.e. it is a form)."""
    return any(f.type == "bool" or "mark" in f.locators for f in spec.fields)


def _wants_tables(spec: DocTypeSpec) -> bool:
    """Whether this doctype declares table-addressed fields."""
    return any("table" in f.locators for f in spec.fields)


def structural_log_priors(
    features: StructuralFeatures, specs: Iterable[DocTypeSpec]
) -> dict[str, float]:
    """Return a bounded ``log P(c|structure)`` contribution per doctype.

    The rules are deliberately few and deliberately weak. Each is a statement about physical
    reality that holds across issuers, not a tuning parameter:

    * An identity document is one or two pages. Its category never spans a dozen.
    * A single landscape page is card-shaped, which corporate and tax documents are not.
    * A doctype that declares an MRZ is far more likely on a page that has MRZ-shaped text,
      and somewhat less likely on one that has none.
    * A doctype whose fields bind to checkboxes wants a document that has checkboxes.

    Args:
        features: Output of :func:`structural_features`.
        specs: The doctype registry (or a subset).

    Returns:
        Mapping of ``doctype_id`` to a log-prior in ``[-2.0, 1.5]``.
    """
    priors: dict[str, float] = {}
    for spec in specs:
        lp = 0.0

        if spec.category is Category.identity:
            if features.page_count >= 5:
                lp -= 1.2
            elif features.page_count >= 3:
                lp -= 0.4
            elif 0 < features.page_count <= 2:
                lp += 0.15
            if features.landscape and features.page_count == 1:
                lp += 0.25
            if features.table_count >= 3:
                lp -= 0.3

        if spec.category in (Category.corporate, Category.financial):
            if features.page_count >= 3:
                lp += 0.3
            if features.table_count >= 1:
                lp += 0.15
            if features.page_count == 1 and features.landscape:
                lp -= 0.3

        if spec.category is Category.tax:
            if features.table_count >= 1:
                lp += 0.1
            if features.mark_count >= 1:
                lp += 0.1

        if _wants_mrz(spec):
            lp += 0.8 if features.has_mrz_shape else -0.3
        if _wants_marks(spec):
            lp += 0.3 if features.mark_count else -0.2
        if _wants_tables(spec) and features.table_count:
            lp += 0.1

        priors[spec.doctype_id] = round(max(_PRIOR_FLOOR, min(_PRIOR_CEILING, lp)), 4)
    return priors


def describe(features: StructuralFeatures) -> str:
    """One-line human summary of the structure, for :class:`~dce.models.Evidence`."""
    shape = "landscape" if features.landscape else "portrait"
    origin = "digital" if features.digital else "scanned"
    return (
        f"{features.page_count}p {shape} {origin}; tables={features.table_count} "
        f"marks={features.mark_count} kv={features.kv_count} "
        f"mrz_shape={features.has_mrz_shape}"
    )


