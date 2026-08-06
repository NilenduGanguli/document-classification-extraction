"""Checkbox locator — on a KYC form, which box is ticked often *is* the answer.

Plain OCR text drops selection marks entirely, so a form that says "Marital status: ☐
Single ☒ Married" reads as "Marital status: Single Married" — worse than useless. This
locator binds every mark to its nearest option label and then answers a field two ways:

* **Boolean field** — the field's own label matches an option ("Is a US person?"), so the
  answer is that mark's state.
* **Group field** — the field's label matches a heading that a cluster of marks belongs to
  ("Marital status"), so the answer is the option text of the mark that is selected.

An unselected mark never produces a value in group mode; that is the whole point.
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
)
from dce.models import FieldSpec, LayoutView, Mark, TextBlock

__all__ = ["bind_marks", "locate"]

_CONF_BOOLEAN = 0.86
_CONF_GROUP = 0.82
#: Option labels sit immediately beside their box; a wide search would swallow the whole row.
_OPTION_WINDOW_X = 0.22
_OPTION_WINDOW_Y = 0.03
#: How far below/right of a group heading its options may sit.
_GROUP_WINDOW_Y = 0.14
_GROUP_WINDOW_X = 0.60


def locate(field: FieldSpec, view: LayoutView, ctx: LocatorContext) -> list[Candidate]:
    """Resolve a field from the document's selection marks.

    Args:
        field: The field being resolved. ``type="bool"`` yields ``"true"``/``"false"``;
            any other type yields the selected option's label text.
        view: The layout view to search.
        ctx: Locator context.

    Returns:
        Candidates ordered best-first.
    """
    labels = field_labels(field, ctx)
    if not labels or not view.marks:
        return []

    pairs = bind_marks(view)
    out: list[Candidate] = []

    for mark, option_block, option_text in pairs:
        matched, score = match_label(option_text, labels, ctx.min_label_score)
        if not matched:
            continue
        value = "true" if mark.selected else "false"
        if field.type != "bool":
            # A non-boolean field whose label names one option still answers with the
            # option's own text, but only when that option is the one that is ticked.
            if not mark.selected:
                continue
            value = clean_value(option_text)
        if not passes_pattern(field, value):
            continue
        out.append(
            Candidate(
                value=value,
                locator="mark",
                confidence=round(_CONF_BOOLEAN * (score / 100.0), 4),
                page=mark.page,
                bbox=mark.bbox or (option_block.bbox if option_block else None),
                raw=f"{'☒' if mark.selected else '☐'} {option_text}".strip(),
                detail=f"mark bound to option {option_text!r} ~ label {matched!r} ({score:.0f})",
            )
        )

    out.extend(_group_candidates(field, view, ctx, labels, pairs))
    out.sort(key=lambda c: -c.confidence)
    return out


def bind_marks(view: LayoutView) -> list[tuple[Mark, TextBlock | None, str]]:
    """Bind every selection mark to its nearest option label.

    A checkbox's caption is printed immediately to its right in almost every form, and to
    its left in a minority of them; either way it shares the mark's row. Only if neither
    exists do we look at the line beneath.

    Args:
        view: The layout view whose marks to bind.

    Returns:
        ``(mark, block, option_text)`` triples; ``block`` is ``None`` when nothing bound.
    """
    blocks = [b for b in view.blocks if b.text.strip()]
    rects = [geo.rect_from_quad(b.bbox) for b in blocks]
    out: list[tuple[Mark, TextBlock | None, str]] = []

    for mark in view.marks:
        mark_rect = geo.rect_from_quad(mark.bbox)
        if mark_rect is None:
            out.append((mark, None, ""))
            continue
        width, height = geo.page_size(view, mark.page)
        max_dx, max_dy = _OPTION_WINDOW_X * width, _OPTION_WINDOW_Y * height

        best: tuple[TextBlock, float] | None = None
        for block, rect in zip(blocks, rects, strict=True):
            if block.page != mark.page or rect is None:
                continue
            if geo.is_right_of(mark_rect, rect, max_dx=max_dx, min_v_overlap=0.25):
                gap = max(0.0, geo.right_gap(mark_rect, rect))
            elif geo.is_right_of(rect, mark_rect, max_dx=max_dx, min_v_overlap=0.25):
                # Caption printed to the LEFT of its box: penalised so a right-hand
                # caption always wins when both exist.
                gap = max(0.0, geo.right_gap(rect, mark_rect)) + 0.5 * max_dx
            elif geo.is_below(mark_rect, rect, max_dy=max_dy, min_h_overlap=0.3):
                gap = max(0.0, geo.below_gap(mark_rect, rect)) + max_dx
            else:
                continue
            if best is None or gap < best[1]:
                best = (block, gap)

        if best is None:
            out.append((mark, None, ""))
        else:
            out.append((mark, best[0], clean_value(best[0].text)))
    return out


def _group_candidates(
    field: FieldSpec,
    view: LayoutView,
    ctx: LocatorContext,
    labels: list[tuple[str, float]],
    pairs: list[tuple[Mark, TextBlock | None, str]],
) -> list[Candidate]:
    """Answer a field whose label is the *heading* of a cluster of options."""
    out: list[Candidate] = []
    for block in view.blocks:
        if not block.text.strip():
            continue
        matched, score = match_label(block.text, labels, ctx.min_label_score)
        if not matched:
            continue
        heading_rect = geo.rect_from_quad(block.bbox)
        if heading_rect is None:
            continue
        width, height = geo.page_size(view, block.page)
        max_dx, max_dy = _GROUP_WINDOW_X * width, _GROUP_WINDOW_Y * height

        selected = [
            (mark, text)
            for mark, _option_block, text in pairs
            if mark.selected and text and mark.page == block.page
            and _within_group(heading_rect, mark, max_dx, max_dy)
        ]
        if not selected:
            continue
        # Two ticked boxes under one heading is a real form state, not an error; report
        # both and let the reviewer see the ambiguity in the confidence.
        penalty = 1.0 if len(selected) == 1 else 0.8
        for mark, text in selected:
            value = "true" if field.type == "bool" else clean_value(text)
            if not passes_pattern(field, value):
                continue
            out.append(
                Candidate(
                    value=value,
                    locator="mark",
                    confidence=round(_CONF_GROUP * (score / 100.0) * penalty, 4),
                    page=mark.page,
                    bbox=mark.bbox,
                    raw=f"☒ {text}",
                    detail=(
                        f"group {matched!r} ({score:.0f}) -> selected option {text!r}"
                        + ("; multiple options selected" if len(selected) > 1 else "")
                    ),
                )
            )
    return out


def _within_group(heading: geo.Rect, mark: Mark, max_dx: float, max_dy: float) -> bool:
    """``True`` when a mark plausibly belongs to the cluster under/right of a heading."""
    rect = geo.rect_from_quad(mark.bbox)
    if rect is None:
        return False
    if geo.is_below(heading, rect, max_dy=max_dy, min_h_overlap=0.0):
        return True
    return geo.is_right_of(heading, rect, max_dx=max_dx, min_v_overlap=0.2)
