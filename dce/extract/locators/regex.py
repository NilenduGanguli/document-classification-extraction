"""Regex locator — the last resort, swept in zone order so the page's shape still counts.

A pattern match anywhere on the page is weak evidence on its own, which is why this locator
carries the lowest prior. Two things make it worth having:

* **Zone ordering.** The same identifier found in a title is worth more than one found in
  page furniture, and the layout payload already tells us which is which. Sweeping in zone
  order (and reporting the zone in the provenance) is free signal that a plain text grep
  throws away.
* **Validators.** A checksummed identifier that a regex found and a checksum confirmed is
  as good as anything a label could have told us — :mod:`dce.extract.resolve` promotes it
  on that basis, not on this locator's confidence.

Pattern sources, in order: the field's own ``pattern``; the default shape for the field's
``validator``; and — only for ``type="id"`` fields with neither — the doctype's declared
``id_patterns``. That last fallback is narrow on purpose: sweeping every doctype identifier
into every field is how an Aadhaar number ends up in a name field.
"""
from __future__ import annotations

import re

from dce.extract import validate as V
from dce.extract.locators import geometry as geo
from dce.extract.locators.base import Candidate, LocatorContext, clean_value
from dce.models import FieldSpec, LayoutView, Zone

__all__ = ["locate"]

_BASE_CONFIDENCE = 0.50
#: Where a value was printed changes how much a bare pattern match is worth.
_ZONE_BONUS: dict[Zone, float] = {
    Zone.title: 0.12,
    Zone.heading: 0.08,
    Zone.body: 0.04,
    Zone.table: 0.03,
    Zone.furniture: -0.10,
}
_MAX_CONFIDENCE = 0.68


def locate(field: FieldSpec, view: LayoutView, ctx: LocatorContext) -> list[Candidate]:
    """Sweep the document for text matching the field's shape.

    Args:
        field: The field being resolved.
        view: The layout view to search.
        ctx: Locator context; supplies the doctype's id patterns.

    Returns:
        Candidates ordered best-first, de-duplicated by matched value.
    """
    patterns = _patterns_for(field, ctx)
    if not patterns:
        return []
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue  # a malformed declaration must not take the sweep down
    if not compiled:
        return []

    best: dict[str, Candidate] = {}
    for text, zone, page, bbox, source in _sources(view):
        bonus = _ZONE_BONUS.get(zone, 0.0)
        for regex in compiled:
            for match in regex.finditer(text):
                value = clean_value(match.group(0))
                if not value:
                    continue
                confidence = round(min(_BASE_CONFIDENCE + bonus, _MAX_CONFIDENCE), 4)
                existing = best.get(value)
                if existing is not None and existing.confidence >= confidence:
                    continue
                best[value] = Candidate(
                    value=value,
                    locator="regex",
                    confidence=confidence,
                    page=page,
                    bbox=bbox,
                    raw=text,
                    detail=f"regex {regex.pattern} in {zone.value} ({source})",
                )
    out = list(best.values())
    out.sort(key=lambda c: (-c.confidence, c.page or 0))
    return out


def _patterns_for(field: FieldSpec, ctx: LocatorContext) -> list[str]:
    """Resolve the pattern list for a field, most specific first."""
    patterns: list[str] = []
    if field.pattern:
        patterns.append(field.pattern)
    if field.validator:
        default = V.ID_PATTERNS.get(field.validator)
        if default and default not in patterns:
            patterns.append(default)
    if not patterns and field.type == "id":
        patterns.extend(_doctype_fallback(field, ctx))
    return patterns


def _doctype_fallback(field: FieldSpec, ctx: LocatorContext) -> list[str]:
    """Doctype-level ``id_patterns`` this field may borrow — usually none.

    A doctype's ``id_patterns`` exist to make it *recognisable*; they say nothing about
    which field a match belongs to. Borrowing one for extraction is only safe when the
    answer is unambiguous, which needs both of:

    * the pattern is **unclaimed** — no other field on the doctype declares it. An Aadhaar
      card lists the UID shape at doctype level *and* on ``aadhaar_number``; letting
      ``enrolment_number`` borrow it too makes the UID come back twice, once under a field
      it does not belong to.
    * this is the **only** id field with nothing of its own, so there is exactly one field
      the leftover patterns could describe.

    Failing either, the field gets no regex sweep. A missing value goes to the review queue;
    a confidently wrong identifier propagates.
    """
    if not ctx.id_patterns:
        return []
    if ctx.spec is None:
        # No spec to cross-check against — a caller handed us patterns directly, so they
        # are this field's by construction.
        return list(ctx.id_patterns)

    claimed = {f.pattern for f in ctx.spec.fields if f.pattern}
    claimed |= {
        V.ID_PATTERNS[f.validator]
        for f in ctx.spec.fields
        if f.validator and f.validator in V.ID_PATTERNS
    }
    unclaimed_id_fields = [
        f for f in ctx.spec.fields if f.type == "id" and not f.pattern and not f.validator
    ]
    if len(unclaimed_id_fields) > 1:
        return []
    return [p for p in ctx.id_patterns if p not in claimed]


def _sources(view: LayoutView) -> list[tuple[str, Zone, int, list[float] | None, str]]:
    """Every searchable text run, ordered by zone weight then reading order."""
    items: list[tuple[int, geo.Rect | None, tuple[str, Zone, int, list[float] | None, str]]] = []
    for block in view.blocks:
        items.append(
            (
                block.page,
                geo.rect_from_quad(block.bbox),
                (block.text, block.zone, block.page, block.bbox, "block"),
            )
        )
    for table in view.tables:
        for cell in table.cells:
            if cell.text.strip():
                items.append(
                    (
                        table.page,
                        geo.rect_from_quad(cell.bbox),
                        (cell.text, Zone.table, table.page, cell.bbox,
                         f"{table.table_id} r{cell.row}c{cell.col}"),
                    )
                )
    for pair in view.key_values:
        if pair.value.strip():
            items.append(
                (
                    pair.page,
                    geo.rect_from_quad(pair.value_bbox),
                    (pair.value, Zone.body, pair.page, pair.value_bbox, f"kv {pair.key!r}"),
                )
            )

    ordered = geo.reading_order(items)
    zone_rank = {
        Zone.title: 0, Zone.heading: 1, Zone.body: 2, Zone.table: 3, Zone.furniture: 4,
    }
    return sorted(ordered, key=lambda entry: zone_rank.get(entry[1], 2))
