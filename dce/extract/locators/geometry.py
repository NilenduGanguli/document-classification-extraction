"""Page geometry: rectangles, overlap tests, search windows and reading order.

Bounding boxes arrive as Azure-style quads — eight floats, four ``(x, y)`` points clockwise
from top-left. Every locator works on axis-aligned rectangles derived from them, because a
KYC form's label/value relationships are horizontal and vertical; a rotated page is
straightened by the OCR provider before we ever see it.

Search windows are expressed as a **fraction of the page**, never in absolute units. The
same document arrives measured in pixels from a scan and in inches from a native PDF, and
``label_window_x = 0.55`` means "a bit over half the page" in both.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import NamedTuple, TypeVar

from dce.models import LayoutView, Quad

__all__ = [
    "Rect",
    "below_gap",
    "h_overlap",
    "is_below",
    "is_right_of",
    "page_size",
    "quad_from_rect",
    "reading_order",
    "rect_from_quad",
    "right_gap",
    "union",
    "v_overlap",
]

T = TypeVar("T")


class Rect(NamedTuple):
    """Axis-aligned rectangle in page units."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


def rect_from_quad(quad: Quad | None) -> Rect | None:
    """Axis-aligned bounds of a quad, or ``None`` when there is no geometry."""
    if not quad or len(quad) < 8:
        return None
    xs = [float(quad[i]) for i in range(0, 8, 2)]
    ys = [float(quad[i]) for i in range(1, 8, 2)]
    return Rect(min(xs), min(ys), max(xs), max(ys))


def quad_from_rect(rect: Rect) -> Quad:
    """Render a rectangle back as a clockwise-from-top-left quad."""
    return [rect.x0, rect.y0, rect.x1, rect.y0, rect.x1, rect.y1, rect.x0, rect.y1]


def union(rects: Iterable[Rect]) -> Rect | None:
    """Bounding rectangle of everything given, or ``None`` when nothing was."""
    items = [r for r in rects if r is not None]
    if not items:
        return None
    return Rect(
        min(r.x0 for r in items),
        min(r.y0 for r in items),
        max(r.x1 for r in items),
        max(r.y1 for r in items),
    )


def h_overlap(a: Rect, b: Rect) -> float:
    """Width of the horizontal intersection (0.0 when disjoint)."""
    return max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))


def v_overlap(a: Rect, b: Rect) -> float:
    """Height of the vertical intersection (0.0 when disjoint)."""
    return max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))


def right_gap(label: Rect, candidate: Rect) -> float:
    """Horizontal distance from the label's right edge to the candidate's left edge."""
    return candidate.x0 - label.x1


def below_gap(label: Rect, candidate: Rect) -> float:
    """Vertical distance from the label's bottom edge to the candidate's top edge."""
    return candidate.y0 - label.y1


def is_right_of(
    label: Rect, candidate: Rect, *, max_dx: float, min_v_overlap: float = 0.3
) -> bool:
    """``True`` when ``candidate`` sits on the same row, to the right, inside the window.

    ``min_v_overlap`` is a fraction of the *shorter* box's height: two boxes on the same
    printed line always share most of their vertical extent, and requiring that is what
    keeps a value from the row below out of a same-row lookup.
    """
    gap = right_gap(label, candidate)
    if gap < -0.25 * label.width or gap > max_dx:
        return False
    shorter = min(label.height, candidate.height) or 1e-9
    return v_overlap(label, candidate) / shorter >= min_v_overlap


def is_below(
    label: Rect, candidate: Rect, *, max_dy: float, min_h_overlap: float = 0.15
) -> bool:
    """``True`` when ``candidate`` sits under the label, inside the window, and aligned.

    ``min_h_overlap`` is a fraction of the *label's* width, so a narrow label ("DOB") still
    binds the wide value printed beneath it, while a value in the next column does not.
    """
    gap = below_gap(label, candidate)
    if gap < -0.25 * label.height or gap > max_dy:
        return False
    width = label.width or 1e-9
    return h_overlap(label, candidate) / width >= min_h_overlap


def page_size(view: LayoutView, page: int) -> tuple[float, float]:
    """Return ``(width, height)`` for a page, inferring it when the provider omitted it.

    Falls back to the extent of the geometry actually present on that page, and finally to
    ``(1.0, 1.0)`` so that fractional windows still behave sensibly on normalised coords.
    """
    for info in view.pages:
        if info.page == page and info.width > 0 and info.height > 0:
            return float(info.width), float(info.height)

    rects: list[Rect] = []
    for block in view.blocks:
        if block.page == page:
            rect = rect_from_quad(block.bbox)
            if rect is not None:
                rects.append(rect)
    for table in view.tables:
        if table.page == page:
            for cell in table.cells:
                rect = rect_from_quad(cell.bbox)
                if rect is not None:
                    rects.append(rect)
    bounds = union(rects)
    if bounds is None or bounds.x1 <= 0 or bounds.y1 <= 0:
        return 1.0, 1.0
    return bounds.x1, bounds.y1


def reading_order(
    items: Sequence[tuple[int, Rect | None, T]], *, line_tolerance: float = 0.6
) -> list[T]:
    """Sort ``(page, rect, payload)`` triples into human reading order.

    Items whose vertical centres fall within ``line_tolerance`` of a box height are treated
    as being on the same printed line and are ordered left-to-right; everything else orders
    top-to-bottom. Items with no geometry keep their original relative position at the end
    of their page, which is the best available guess.
    """
    heights = [r.height for _, r, _ in items if r is not None and r.height > 0]
    band = (sum(heights) / len(heights) * line_tolerance) if heights else 1.0

    def key(indexed: tuple[int, tuple[int, Rect | None, T]]) -> tuple[int, int, float, float]:
        index, (page, rect, _payload) = indexed
        if rect is None:
            return (page, 1, float(index), 0.0)
        return (page, 0, round(rect.cy / band) if band else rect.cy, rect.x0)

    return [payload for _, (_, _, payload) in sorted(enumerate(items), key=key)]
