"""Key/value locator — matches field labels against provider-detected key/value pairs.

When the OCR provider ran with key/value detection (Azure ``features=keyValuePairs``), it
has already solved the hard half of the problem: which text on the page is a key and which
is its value. This locator only has to decide whether a detected key *is* one of our
field's labels, which is a fuzzy string match per language and nothing more.

That is why ``kv`` carries the second-highest prior after the MRZ: the geometry was done by
something with more signal than we have.
"""
from __future__ import annotations

from dce.extract.locators.base import (
    Candidate,
    LocatorContext,
    clean_value,
    field_labels,
    match_label,
    passes_pattern,
    refine_to_pattern,
)
from dce.models import FieldSpec, LayoutView

__all__ = ["locate"]

_BASE_CONFIDENCE = 0.90


def locate(field: FieldSpec, view: LayoutView, ctx: LocatorContext) -> list[Candidate]:
    """Find values whose provider-detected key matches one of the field's labels.

    Args:
        field: The field being resolved.
        view: The layout view to search.
        ctx: Locator context (languages, fuzzy threshold).

    Returns:
        Candidates ordered best-first. A value that fails the field's pattern is first
        narrowed to the pattern's match inside it, and only dropped if that fails too.
    """
    labels = field_labels(field, ctx)
    if not labels or not view.key_values:
        return []

    out: list[Candidate] = []
    for pair in view.key_values:
        matched, score = match_label(pair.key, labels, ctx.min_label_score)
        if not matched:
            continue
        value = clean_value(pair.value)
        if not value:
            continue

        detail = f"kv key {pair.key!r} ~ label {matched!r} ({score:.0f})"
        confidence = _BASE_CONFIDENCE * (score / 100.0)
        if pair.confidence is not None:
            # The provider's own belief in the pairing is real evidence; respect it.
            confidence *= max(0.5, min(1.0, float(pair.confidence)))

        if not passes_pattern(field, value):
            continue
        narrowed = refine_to_pattern(field, value)
        if narrowed != value:
            confidence *= 0.95
            detail += "; narrowed to pattern"

        out.append(
            Candidate(
                value=narrowed,
                locator="kv",
                confidence=round(min(confidence, 0.97), 4),
                page=pair.page,
                bbox=pair.value_bbox or pair.key_bbox,
                raw=pair.value,
                detail=detail,
            )
        )

    out.sort(key=lambda c: -c.confidence)
    return out
