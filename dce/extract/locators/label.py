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

**Where the span ends is as important as where it begins.** A form line carries several
fields — ``Signature of U.S. person: J. Smith  Date: 2026-03-14`` — so taking everything to
the right of a matched label swallows the next field's label *and its value*. Every span
this locator captures therefore goes through :mod:`dce.extract.locators.trim`, which
terminates it at the next known caption and narrows it to the field's own shape. A span that
needed either is reported with a lower confidence than one that did not, so a clean binding
elsewhere on the page wins on tightness alone.

That line is also why a block is matched against **every** label the field declares rather
than only its best-scoring one: ``signature_date`` declares both captions on it, and only
one of them is followed by the date.
"""
from __future__ import annotations

import re

from dce.extract.locators import geometry as geo
from dce.extract.locators import trim
from dce.extract.locators.base import (
    Candidate,
    LocatorContext,
    clean_value,
    field_labels,
    label_similarity,
    match_label,
    passes_pattern,
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
#: A bracketed clarifier printed as part of a caption, not as part of the value.
_CLARIFIER_RE = re.compile(r"^[\s:.\-\u2013\u2014]*[(\[][^)\]]*[)\]]")


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

    captions = trim.known_labels(field, ctx)
    blocks = list(view.blocks)
    rects = [geo.rect_from_quad(b.bbox) for b in blocks]
    out: list[Candidate] = []

    for index, block in enumerate(blocks):
        if not block.text.strip():
            continue
        matches = _matching_labels(block.text, labels, ctx.min_label_score)
        if not matches:
            continue

        same_line = _same_line_candidates(
            field, block, matches, captions, ctx.min_label_score
        )
        if same_line:
            out.extend(same_line)
            continue

        matched, score = matches[0]
        weight = score / 100.0
        label_rect = rects[index]
        if label_rect is None:
            continue
        width, height = geo.page_size(view, block.page)
        max_dx = ctx.settings.label_window_x * width
        max_dy = ctx.settings.label_window_y * height

        right = _nearest(
            blocks, rects, index, block.page, label_rect, labels, captions, ctx,
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
                    captions=captions, matched=matched,
                    min_label_score=ctx.min_label_score,
                )
            )

        below = _nearest(
            blocks, rects, index, block.page, label_rect, labels, captions, ctx,
            horizontal=False, limit=max_dy,
        )
        if below is not None:
            candidate_block, gap = below
            decay = 1.0 - _DISTANCE_PENALTY * min(1.0, gap / max_dy if max_dy else 0.0)
            text, last_block = _extend_multiline(
                field, blocks, rects, candidate_block, labels, captions, ctx, max_dy
            )
            out.append(
                _make(
                    field, text, block, candidate_block, _CONF_BELOW * weight * decay,
                    f"label {matched!r} -> below, gap {gap:.1f} ({score:.0f})",
                    span_to=last_block, captions=captions, matched=matched,
                    min_label_score=ctx.min_label_score,
                )
            )

    accepted = [c for c in out if c is not None]
    accepted.sort(key=lambda c: -c.confidence)
    return accepted


def _matching_labels(
    text: str, labels: list[tuple[str, float]], min_score: float
) -> list[tuple[str, float]]:
    """Every declared label this block clears the fuzzy floor for, best-scoring first.

    :func:`~dce.extract.locators.base.match_label` returns only the winner, which is the
    right answer when asking "is this block my label?" and the wrong one when asking "where
    does my value start?": a single printed line can carry two of a field's own captions,
    and the higher-scoring one is not necessarily the one the value follows.
    """
    scored = [
        (label, label_similarity(label, text) * weight) for label, weight in labels
    ]
    clearing = [(label, score) for label, score in scored if score >= min_score]
    clearing.sort(key=lambda pair: -pair[1])
    return clearing


def _same_line_candidates(
    field: FieldSpec,
    block: TextBlock,
    matches: list[tuple[str, float]],
    captions: tuple[str, ...],
    min_label_score: float,
) -> list[Candidate]:
    """Candidates for every one of the field's captions that this line carries."""
    out: list[Candidate] = []
    seen: set[str] = set()
    for matched, score in matches:
        tail = _strip_clarifier(split_on_label(block.text, matched))
        if not tail:
            continue
        candidate = _make(
            field, tail, block, block, _CONF_SAME_LINE * (score / 100.0),
            f"label {matched!r} same line ({score:.0f})",
            captions=captions, matched=matched, min_label_score=min_label_score,
        )
        if candidate is None or candidate.value in seen:
            continue
        seen.add(candidate.value)
        out.append(candidate)
    return out


def _strip_clarifier(tail: str) -> str:
    """Drop the parenthetical a caption trails behind it.

    ``1 Name (as shown on your income tax return)`` is one caption, not a caption and a
    value: splitting on ``Name`` leaves the clarifier, and reporting it fills ``full_name``
    with the form's own instructions. Stripping it leaves nothing, which correctly sends the
    lookup on to the geometry bindings and finds the name printed below.

    It also does the useful half of the same job for ``Date (MM-DD-YYYY): 2026-03-14``.
    """
    previous = None
    while tail != previous:
        previous = tail
        tail = clean_value(_CLARIFIER_RE.sub("", tail, count=1))
    return tail


def _make(
    field: FieldSpec,
    value: str,
    label_block: TextBlock,
    value_block: TextBlock,
    confidence: float,
    detail: str,
    *,
    span_to: TextBlock | None = None,
    captions: tuple[str, ...] = (),
    matched: str = "",
    min_label_score: float = 0.0,
) -> Candidate | None:
    """Build a candidate, or ``None`` when what is left is not a value at all.

    The last guard is the one that catches a bilingual card: ``Name / नाम`` splits on
    ``Name`` and leaves the *other half of its own caption*, which would be reported as the
    holder's name. Whatever survives trimming still has to not be a caption.
    """
    trimmed = trim.trim_span(field, value, labels=captions, matched=matched)
    if not trimmed.value:
        return None
    if trim.reads_as_caption(trimmed.value, captions, min_label_score):
        return None
    if not passes_pattern(field, trimmed.value):
        return None
    confidence *= trimmed.penalty
    if trimmed.note:
        detail += f"; {trimmed.note}"

    bbox = value_block.bbox
    if span_to is not None and span_to is not value_block:
        merged = geo.union(
            r for r in (geo.rect_from_quad(value_block.bbox), geo.rect_from_quad(span_to.bbox))
            if r is not None
        )
        if merged is not None:
            bbox = geo.quad_from_rect(merged)
    return Candidate(
        value=trimmed.value,
        locator="label",
        confidence=round(min(confidence, 0.95), 4),
        page=value_block.page,
        bbox=bbox,
        raw=value_block.text,
        detail=detail,
        extra={"label_page": str(label_block.page)},
    )


def _nearest(
    blocks: list[TextBlock],
    rects: list[geo.Rect | None],
    label_index: int,
    page: int,
    label_rect: geo.Rect,
    labels: list[tuple[str, float]],
    captions: tuple[str, ...],
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
        # A block that restates the label (bilingual caption) is furniture, not a value —
        # and so is a block that states some *other* field's label.
        if match_label(block.text, labels, ctx.min_label_score)[0]:
            continue
        if trim.reads_as_caption(block.text, captions, ctx.min_label_score):
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
    captions: tuple[str, ...],
    ctx: LocatorContext,
    max_dy: float,
) -> tuple[str, TextBlock]:
    """Absorb the continuation lines of a multi-line value (addresses, mostly).

    Only runs for field types that genuinely wrap. Stops at the first block that reads like
    a caption — its own label, any other field's label, or anything ending in a colon — or
    that sits too far below or drifts out of the column: the conditions under which the next
    printed line belongs to a different field.
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
        if trim.reads_as_caption(block.text, captions, ctx.min_label_score):
            break
        if match_label(block.text, labels, ctx.min_label_score)[0]:
            break
        if block.zone is Zone.furniture:
            break
        parts.append(block.text)
        current, current_rect, last = block, rect, block
    return "\n".join(parts), last
