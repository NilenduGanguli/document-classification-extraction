"""Page enumeration and slicing — the machinery every per-page answer is built on.

Four defects lived here, all silent. A page could vanish from a bundle with nothing in the
result to indicate it had ever existed; a gap in the numbering was bridged into a segment
that claimed a page it was never shown; a slice lost the provenance saying which recogniser
read the document; and a slice shared its blocks with its parent, so mutating one mutated
the other.

None of them could be caught by the existing test, which runs a synthetic seven-doctype
fixture registry whose ids are not registry ids at all.
"""
from __future__ import annotations

from dce.classify.cascade import _page_numbers, _page_view, page_range_view
from dce.models import KeyValue, LayoutView, Mark, PageInfo, TextBlock, Zone


def _bundle() -> LayoutView:
    """Three pages: prose, a form page with only marks and key-values, prose again."""
    return LayoutView(
        doc_id="bundle",
        pages=[PageInfo(page=p, width=8.5, height=11.0, unit="inch") for p in (1, 2, 3)],
        blocks=[
            TextBlock(text="FORM W-9", zone=Zone.title, page=1),
            TextBlock(text="Request for Taxpayer Identification Number", zone=Zone.body, page=1),
            TextBlock(text="CONTINUATION", zone=Zone.body, page=3),
        ],
        marks=[Mark(label="Individual", state="selected", page=2)],
        key_values=[KeyValue(key="Name", value="A. Example", page=2)],
        languages=["en"],
        raw={"provider": "azure_layout", "media_type": "pdf"},
    )


# ---------------------------------------------------------------------------
# D1 — a page must not be able to disappear
# ---------------------------------------------------------------------------
def test_a_page_of_only_marks_and_key_values_is_still_a_page():
    """A scanned form page: one checkbox, one key/value, no prose. Ordinary Azure output."""
    assert _page_numbers(_bundle()) == [1, 2, 3]


def test_a_page_of_only_marks_is_a_page():
    view = LayoutView(
        doc_id="d",
        pages=[],
        blocks=[TextBlock(text="COVER", zone=Zone.title, page=1)],
        marks=[Mark(label="Yes", state="selected", page=7)],
    )

    assert _page_numbers(view) == [1, 7]


def test_a_page_of_only_key_values_is_a_page():
    view = LayoutView(
        doc_id="d",
        pages=[],
        blocks=[TextBlock(text="COVER", zone=Zone.title, page=1)],
        key_values=[KeyValue(key="Date", value="2026-01-01", page=4)],
    )

    assert _page_numbers(view) == [1, 4]


# ---------------------------------------------------------------------------
# D3 — provenance survives a slice
# ---------------------------------------------------------------------------
def test_a_slice_keeps_the_provenance_of_the_document_it_came_from():
    """`raw` is where "which recogniser read this" lives. A segment without it is unauditable."""
    sliced = page_range_view(_bundle(), 1, 1)

    assert sliced.raw == {"provider": "azure_layout", "media_type": "pdf"}


def test_a_slice_does_not_share_the_parents_raw_dict():
    view = _bundle()
    sliced = page_range_view(view, 1, 1)
    sliced.raw["provider"] = "tampered"

    assert view.raw["provider"] == "azure_layout"


# ---------------------------------------------------------------------------
# D4 — a slice is a copy, and page numbers are absolute
# ---------------------------------------------------------------------------
def test_mutating_a_slice_does_not_mutate_the_bundle():
    view = _bundle()
    sliced = page_range_view(view, 1, 1)
    sliced.blocks[0].text = "TAMPERED"

    assert view.blocks[0].text == "FORM W-9"


def test_page_numbers_stay_absolute_across_a_slice():
    """A segment saying "page 3" must mean page 3 of the file the caller uploaded."""
    sliced = page_range_view(_bundle(), 3, 3)

    assert [p.page for p in sliced.pages] == [3]
    assert [b.page for b in sliced.blocks] == [3]


def test_a_range_slice_carries_every_collection():
    sliced = page_range_view(_bundle(), 2, 3)

    assert [p.page for p in sliced.pages] == [2, 3]
    assert [b.page for b in sliced.blocks] == [3]
    assert [m.page for m in sliced.marks] == [2]
    assert [kv.page for kv in sliced.key_values] == [2]
    assert sliced.languages == ["en"]


def test_a_range_slice_labels_itself_with_its_range():
    assert page_range_view(_bundle(), 2, 3).doc_id == "bundle#p2-3"
    assert page_range_view(_bundle(), 2, 2).doc_id == "bundle#p2"


def test_the_single_page_slicer_still_behaves():
    """_page_view now delegates; its contract must not have moved."""
    sliced = _page_view(_bundle(), 1)

    assert [p.page for p in sliced.pages] == [1]
    assert [b.page for b in sliced.blocks] == [1, 1]
    assert sliced.doc_id == "bundle#p1"
