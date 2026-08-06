"""Table locator — address a value by its header cell rather than by geometry.

Two table shapes cover almost every KYC form:

* **Column-header tables** — the label is a header in the top row and the value is in the
  cell(s) beneath it.
* **Row-header tables** — the label is in the first column and the value is to its right.
  A two-column "table" is how most providers report a form's field grid.

Merged spans are handled by addressing through :meth:`dce.models.Table.cell_at`, which
resolves a coordinate to whichever cell spans it. A header merged across two columns is
searched across both of its columns, because the value may be printed under either one.
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
from dce.models import Cell, FieldSpec, LayoutView, Table

__all__ = ["locate"]

_CONF_PREFERRED = 0.78
_CONF_ALTERNATE = 0.60
#: How many rows under a column header we will look at for a ``multi`` field.
_MULTI_ROW_LIMIT = 25


def locate(field: FieldSpec, view: LayoutView, ctx: LocatorContext) -> list[Candidate]:
    """Locate a value by finding its header cell and stepping to the value cell.

    Args:
        field: The field being resolved.
        view: The layout view to search.
        ctx: Locator context.

    Returns:
        Candidates ordered best-first. Orientation is inferred per header cell: a flagged
        header prefers the cell below, a first-column label prefers the cell to its right,
        and the other direction is still offered at a lower confidence.
    """
    labels = field_labels(field, ctx)
    if not labels or not view.tables:
        return []

    out: list[Candidate] = []
    for table in view.tables:
        for cell in table.cells:
            if not cell.text.strip():
                continue
            matched, score = match_label(cell.text, labels, ctx.min_label_score)
            if not matched:
                continue
            weight = score / 100.0
            prefer_below = cell.is_header and cell.row + cell.row_span < table.row_count
            prefer_right = cell.col == 0 and table.col_count > 1

            below = _cells_below(table, cell, limit=_MULTI_ROW_LIMIT if field.multi else 1)
            right = _cell_right(table, cell)

            right_first = (_as_list(right), "right"), (below, "below")
            below_first = (below, "below"), (_as_list(right), "right")
            if prefer_below and not prefer_right:
                order, second = below_first, _CONF_ALTERNATE
            elif prefer_right and not prefer_below:
                order, second = right_first, _CONF_ALTERNATE
            else:
                # No orientation signal: a two-column form grid reads left-to-right, so
                # the cell to the right leads, but the cell below stays a close contender.
                order, second = right_first, _CONF_PREFERRED * 0.95
            ranked = [
                (order[0][0], _CONF_PREFERRED, order[0][1]),
                (order[1][0], second, order[1][1]),
            ]

            for targets, base, direction in ranked:
                for rank, target in enumerate(targets):
                    candidate = _make(
                        field, table, cell, target,
                        base * weight * (1.0 - 0.05 * rank),
                        f"table {table.table_id} header {cell.text.strip()!r} "
                        f"-> {direction} r{target.row}c{target.col} ({score:.0f})",
                    )
                    if candidate is not None:
                        out.append(candidate)

    out.sort(key=lambda c: -c.confidence)
    return out


def _as_list(cell: Cell | None) -> list[Cell]:
    return [cell] if cell is not None else []


def _cells_below(table: Table, header: Cell, *, limit: int) -> list[Cell]:
    """Non-empty content cells under ``header``, scanning every column it spans.

    A header merged across columns 1-2 is searched in column 1 then column 2, so a value
    printed under either half of the merge is still found.
    """
    found: list[Cell] = []
    seen: set[tuple[int, int]] = set()
    for row in range(header.row + header.row_span, table.row_count):
        for col in range(header.col, header.col + header.col_span):
            target = table.cell_at(row, col)
            if target is None or target is header or target.is_header:
                continue
            key = (target.row, target.col)
            if key in seen or not target.text.strip():
                continue
            seen.add(key)
            found.append(target)
            if len(found) >= limit:
                return found
    return found


def _cell_right(table: Table, header: Cell) -> Cell | None:
    """First non-empty cell to the right of ``header`` on the same row."""
    for col in range(header.col + header.col_span, table.col_count):
        target = table.cell_at(header.row, col)
        if target is None or target is header:
            continue
        if target.text.strip():
            return target
    return None


def _make(
    field: FieldSpec, table: Table, header: Cell, target: Cell, confidence: float, detail: str
) -> Candidate | None:
    """Build a candidate from a value cell, gated on the field's pattern."""
    value = clean_value(target.text)
    if not value:
        return None
    if not passes_pattern(field, value):
        return None
    narrowed = refine_to_pattern(field, value)
    if narrowed != value:
        confidence *= 0.95
        detail += "; narrowed to pattern"
    return Candidate(
        value=narrowed,
        locator="table",
        confidence=round(min(confidence, 0.94), 4),
        page=table.page,
        bbox=target.bbox or table.bbox,
        raw=target.text,
        detail=detail,
        extra={"header": header.text.strip(), "cell": f"r{target.row}c{target.col}"},
    )
