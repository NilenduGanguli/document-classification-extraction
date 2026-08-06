"""Label-anchored locator — find the label, then take what is printed next to it.

Three bindings, in descending order of how certain they are:

1. **Same line** — ``Date of Birth: 01/02/1990``. The label and its value share a text
   block, so there is nothing to guess.
2. **Right of** — the value block starts to the right of the label within
   ``label_window_x`` of the page width and shares the label's row. The dominant form
   layout.
3. **Below** — the value block starts under the label within ``label_window_y`` of the page
   height and overlaps it horizontally. The stacked layout, and the one that carries
   multi-line addresses.

Nearest wins within each binding, and the whole thing is gated on the field's pattern: an
``address`` field whose label happens to sit left of a date must not bind that date. That
rejection is the entire reason ``FieldSpec.pattern`` exists.
"""
from __future__ import annotations

from dce.extract.locators import geometry as geo
from dce.extract.locators.base import (
    Candidate,
    LocatorContext,
    clean_value,
    field_labels,
    match_label,
    passes_pattern,
    refine_to_pattern,
    split_on_label,
)
from dce.models import FieldSpec, LayoutView, TextBlock, Zone

__all__ = ["locate"]

_CONF_SAME_LINE = 0.82
_CONF_RIGHT = 0.76
_CONF_BELOW = 0.70
#: Fraction of the window distance that erodes confidence — a value 10pt right of its label
#: is a far better bet than one at the far edge of the search window.
_DISTANCE_PENALTY = 0.35
#: Field types whose value legitimately runs over several printed lines.
_MULTILINE_TYPES = frozenset({"address"})
#: A block that is itself a label should never be taken as a value.
_LABEL_TAIL = (":", "\uff1a")


def locate(field: FieldSpec, view: LayoutView, ctx: LocatorContext) -> list[Candidate]:
    """Locate a value by anchoring on the field's label.

    Args:
        field: The field being resolved.
        view: The layout view to search.
        ctx: Locator context; supplies the fuzzy floor and the page-relative windows.

    Returns:
        Candidates ordered best-first, every one of which satisfies the field's pattern.
    """
    labels = field_labels(field, ctx)
    if not labels or not view.blocks:
        return []

    blocks = list(view.blocks)
    rects = [geo.rect_from_quad(b.bbox) for b in blocks]
    out: list[Candidate] = []

    for index, block in enumerate(blocks):
        if not block.text.strip():
            continue
        matched, score = match_label(block.text, labels, ctx.min_label_score)
        if not matched:
            continue
        weight = score / 100.0

        tail = split_on_label(block.text, matched)
        if tail:
            out.append(
                _make(
                    field, tail, block, block, _CONF_SAME_LINE * weight,
                    f"label {matched!r} same line ({score:.0f})",
                )
            )
            continue

        label_rect = rects[index]
        if label_rect is None:
            continue
        width, height = geo.page_size(view, block.page)
        max_dx = ctx.settings.label_window_x * width
        max_dy = ctx.settings.label_window_y * height

        right = _nearest(
            blocks, rects, index, block.page, label_rect, labels, ctx,
            horizontal=True, limit=max_dx,
        )
        if right is not None:
            candidate_block, gap = right
            decay = 1.0 - _DISTANCE_PENALTY * min(1.0, gap / max_dx if max_dx else 0.0)
            out.append(
                _make(
                    field, clean_value(candidate_block.text), block, candidate_block,
                    _CONF_RIGHT * weight * decay,
                    f"label {matched!r} -> right, gap {gap:.1f} ({score:.0f})",
                )
            )

        below = _nearest(
            blocks, rects, index, block.page, label_rect, labels, ctx,
            horizontal=False, limit=max_dy,
        )
        if below is not None:
            candidate_block, gap = below
            decay = 1.0 - _DISTANCE_PENALTY * min(1.0, gap / max_dy if max_dy else 0.0)
            text, last_block = _extend_multiline(
                field, blocks, rects, candidate_block, labels, ctx, max_dy
            )
            out.append(
                _make(
                    field, text, block, candidate_block, _CONF_BELOW * weight * decay,
                    f"label {matched!r} -> below, gap {gap:.1f} ({score:.0f})",
                    span_to=last_block,
                )
            )

    accepted = [c for c in out if c is not None]
    accepted.sort(key=lambda c: -c.confidence)
    return accepted


def _make(
    field: FieldSpec,
    value: str,
    label_block: TextBlock,
    value_block: TextBlock,
    confidence: float,
    detail: str,
    *,
    span_to: TextBlock | None = None,
) -> Candidate | None:
    """Build a candidate, or ``None`` when the value cannot satisfy the field's pattern."""
    value = clean_value(value)
    if not value or _looks_like_a_label(value):
        return None
    if not passes_pattern(field, value):
        return None
    narrowed = refine_to_pattern(field, value)
    if narrowed != value:
        confidence *= 0.95
        detail += "; narrowed to pattern"

    bbox = value_block.bbox
    if span_to is not None and span_to is not value_block:
        merged = geo.union(
            r for r in (geo.rect_from_quad(value_block.bbox), geo.rect_from_quad(span_to.bbox))
            if r is not None
        )
        if merged is not None:
            bbox = geo.quad_from_rect(merged)
    return Candidate(
        value=narrowed,
        locator="label",
        confidence=round(min(confidence, 0.95), 4),
        page=value_block.page,
        bbox=bbox,
        raw=value_block.text,
        detail=detail,
        extra={"label_page": str(label_block.page)},
    )


def _looks_like_a_label(text: str) -> bool:
    """``True`` for a block that is plainly another field's caption, not a value."""
    return text.endswith(_LABEL_TAIL)


def _nearest(
    blocks: list[TextBlock],
    rects: list[geo.Rect | None],
    label_index: int,
    page: int,
    label_rect: geo.Rect,
    labels: list[tuple[str, float]],
    ctx: LocatorContext,
    *,
    horizontal: bool,
    limit: float,
) -> tuple[TextBlock, float] | None:
    """Nearest block right of (or below) the label inside ``limit``, skipping the label."""
    best: tuple[TextBlock, float] | None = None
    for index, block in enumerate(blocks):
        if index == label_index or block.page != page or not block.text.strip():
            continue
        rect = rects[index]
        if rect is None:
            continue
        if horizontal:
            if not geo.is_right_of(label_rect, rect, max_dx=limit):
                continue
            gap = max(0.0, geo.right_gap(label_rect, rect))
        else:
            if not geo.is_below(label_rect, rect, max_dy=limit):
                continue
            gap = max(0.0, geo.below_gap(label_rect, rect))
        # A block that restates the label (bilingual caption) is furniture, not a value.
        if match_label(block.text, labels, ctx.min_label_score)[0]:
            continue
        if best is None or gap < best[1]:
            best = (block, gap)
    return best


def _extend_multiline(
    field: FieldSpec,
    blocks: list[TextBlock],
    rects: list[geo.Rect | None],
    start: TextBlock,
    labels: list[tuple[str, float]],
    ctx: LocatorContext,
    max_dy: float,
) -> tuple[str, TextBlock]:
    """Absorb the continuation lines of a multi-line value (addresses, mostly).

    Only runs for field types that genuinely wrap. Stops at the first block that reads like
    a caption, restates the label, sits too far below, or drifts out of the column — the
    conditions under which the next printed line belongs to a different field.
    """
    if field.type not in _MULTILINE_TYPES and not field.multi:
        return start.text, start

    start_rect = geo.rect_from_quad(start.bbox)
    if start_rect is None:
        return start.text, start

    parts = [start.text]
    current, current_rect, last = start, start_rect, start
    line_height = start_rect.height or max_dy
    for index, block in enumerate(blocks):
        if block is current or block.page != current.page or not block.text.strip():
            continue
        rect = rects[index]
        if rect is None or rect.y0 < current_rect.y1 - 0.25 * line_height:
            continue
        if geo.below_gap(current_rect, rect) > 1.6 * line_height:
            continue
        if geo.h_overlap(current_rect, rect) / (current_rect.width or 1e-9) < 0.35:
            continue
        if _looks_like_a_label(block.text.strip()):
            break
        if match_label(block.text, labels, ctx.min_label_score)[0]:
            break
        if block.zone is Zone.furniture:
            break
        parts.append(block.text)
        current, current_rect, last = block, rect, block
    return "\n".join(parts), last
