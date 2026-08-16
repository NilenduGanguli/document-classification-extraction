"""Segmentation: boundaries from structure, classification at document scope.

The property under test is not "it finds every boundary". It is **that it never invents
one**. A missed split leaves one answer that is merely incomplete; a false split turns one
correct classification into two thinner-evidence ones and then runs extraction against each.
Page-scope classification measured 71.3% precision against 100% whole-document, and that gap
is exactly what a false split reintroduces.

So most of what follows asserts that ordinary single documents come back as **one** segment.
"""
from __future__ import annotations

import pytest

from dce.classify.cascade import classify
from dce.classify.segments import (
    Boundary,
    candidate_boundaries,
    segment_document,
)
from dce.models import LayoutView, PageInfo, TextBlock, Zone

fitz = pytest.importorskip("fitz")


def _view(pages: list[PageInfo], blocks: list[TextBlock]) -> LayoutView:
    return LayoutView(doc_id="d", pages=pages, blocks=blocks)


def _letter(page: int, **kw) -> PageInfo:
    return PageInfo(page=page, width=612.0, height=792.0, unit="point", **kw)


# ---------------------------------------------------------------------------
# It must not invent boundaries
# ---------------------------------------------------------------------------
def test_a_uniform_document_proposes_no_boundary():
    view = _view(
        [_letter(p) for p in (1, 2, 3)],
        [TextBlock(text="ACCOUNT SUMMARY", zone=Zone.body, page=p) for p in (1, 2, 3)],
    )

    assert candidate_boundaries(view) == []


def test_an_unmeasured_payload_proposes_no_boundary():
    """A caller-supplied layout has no page verdicts. Absence of measurement is not evidence.

    `text_adequate is None` means nothing looked. Reading that as a finding would split every
    caller-supplied payload at every page.
    """
    view = _view(
        [PageInfo(page=p) for p in (1, 2, 3)],
        [TextBlock(text="ACCOUNT SUMMARY", zone=Zone.body, page=p) for p in (1, 2, 3)],
    )

    assert candidate_boundaries(view) == []


def test_a_single_document_yields_exactly_one_segment():
    """The contract that lets a caller send anything here without a special case."""
    view = _view(
        [_letter(p) for p in (1, 2)],
        [
            TextBlock(text="STATEMENT OF ACCOUNT", zone=Zone.title, page=1),
            TextBlock(text="ACCOUNT SUMMARY BEGINNING BALANCE", zone=Zone.body, page=1),
            TextBlock(text="CLOSING BALANCE 250.00", zone=Zone.body, page=2),
        ],
    )

    segments, boundaries = segment_document(view)

    assert len(segments) == 1
    assert (segments[0].start_page, segments[0].end_page) == (1, 2)
    assert boundaries == []


def test_a_single_document_segments_to_what_classify_alone_says():
    """Sending a single document through segmentation must cost it nothing."""
    view = _view(
        [_letter(p) for p in (1, 2)],
        [
            TextBlock(text="FORM W-9", zone=Zone.title, page=1),
            TextBlock(text="Request for Taxpayer Identification Number", zone=Zone.body, page=1),
            TextBlock(text="Certification instructions continue", zone=Zone.body, page=2),
        ],
    )

    segments, _ = segment_document(view)

    assert len(segments) == 1
    assert segments[0].doctype_id == classify(view).doctype_id


def test_an_abstaining_page_is_not_a_boundary():
    """The failure that made page-scope segmentation unusable: a 6-page statement whose
    middle page carries no anchors became three segments."""
    blocks = [
        TextBlock(text="STATEMENT OF ACCOUNT", zone=Zone.title, page=1),
        TextBlock(text="ACCOUNT SUMMARY", zone=Zone.body, page=1),
        TextBlock(text="This page intentionally contains no account information.", page=2),
        TextBlock(text="CLOSING BALANCE 275.00", zone=Zone.body, page=3),
    ]
    view = _view([_letter(p) for p in (1, 2, 3)], blocks)

    assert candidate_boundaries(view) == []
    assert len(segment_document(view)[0]) == 1


def test_a_blank_page_is_not_a_boundary():
    view = _view(
        [_letter(p) for p in (1, 2, 3)],
        [
            TextBlock(text="STATEMENT OF ACCOUNT", zone=Zone.title, page=1),
            TextBlock(text="CLOSING BALANCE", zone=Zone.body, page=3),
        ],
    )

    assert candidate_boundaries(view) == []


def test_a_blank_page_carrying_real_verdicts_is_still_not_a_boundary():
    """The version with teeth. The test above passed on pages carrying NO verdicts at all.

    `text_adequate=False` means only "fewer than MIN_ALNUM_CHARS characters", which is true of
    a photographed page and equally true of a blank one — and only the first is evidence a new
    document began. A 119-page proxy statement split at its own table of contents, whose pages
    hold the string "TABLE OF CONTENTS" (15 characters, an EDGAR link anchor) and no image at
    all. The module claimed "a blank page is not a boundary" while the shipped code did the
    opposite.
    """
    view = _view(
        [
            _letter(1, text_adequate=True, alnum_chars=2600, image_fraction=0.0),
            # blank: below the floor, but no pixels on it
            _letter(2, text_adequate=False, alnum_chars=15, image_fraction=0.0),
            _letter(3, text_adequate=True, alnum_chars=2400, image_fraction=0.0),
        ],
        [
            TextBlock(text="STATEMENT OF ACCOUNT", zone=Zone.title, page=1),
            TextBlock(text="TABLE OF CONTENTS", zone=Zone.body, page=2),
            TextBlock(text="CLOSING BALANCE", zone=Zone.body, page=3),
        ],
    )

    assert candidate_boundaries(view) == []


def test_a_photographed_page_among_typed_ones_still_is_a_boundary():
    """The other half: the fix must not have disabled the adequacy signal outright."""
    view = _view(
        [
            _letter(1, text_adequate=True, alnum_chars=2600, image_fraction=0.0),
            _letter(2, text_adequate=False, alnum_chars=0, image_fraction=0.95),
        ],
        [TextBlock(text="COVER LETTER", zone=Zone.title, page=1)],
    )

    found = candidate_boundaries(view)

    assert [b.page for b in found] == [2]
    assert found[0].signal == "adequacy"


def test_a_marker_seen_on_any_earlier_page_is_not_a_first_page_marker():
    """Comparing against the previous page alone is not enough.

    A 6-page IRS 1099 carries "OMB No. 1545-0116" on pages 2, 3, 4 and 6 — but not on page 5,
    which is "Instructions for Recipient". Testing only the immediately preceding page made
    the marker read as new at page 6 and split the form off from itself.
    """
    view = _view(
        [_letter(p) for p in (1, 2, 3)],
        [
            TextBlock(text="OMB No. 1545-0116", zone=Zone.body, page=1),
            TextBlock(text="Instructions for Recipient", zone=Zone.body, page=2),
            TextBlock(text="OMB No. 1545-0116", zone=Zone.body, page=3),
        ],
    )

    assert candidate_boundaries(view) == []


# ---------------------------------------------------------------------------
# The signals it does act on
# ---------------------------------------------------------------------------
def test_a_change_of_page_stock_proposes_a_boundary():
    """A passport scan against an A4 bill: different paper, different document."""
    view = _view(
        [_letter(1), _letter(2), PageInfo(page=3, width=396.0, height=612.0, unit="point")],
        [TextBlock(text="TEXT", zone=Zone.body, page=p) for p in (1, 2, 3)],
    )

    found = candidate_boundaries(view)

    assert [b.page for b in found] == [3]
    assert found[0].signal == "geometry"


def test_typed_then_photographed_proposes_a_boundary():
    view = _view(
        [
            _letter(1, text_adequate=True, alnum_chars=400, image_fraction=0.0),
            _letter(2, text_adequate=False, alnum_chars=0, image_fraction=0.95),
        ],
        [TextBlock(text="COVER LETTER", zone=Zone.title, page=1)],
    )

    found = candidate_boundaries(view)

    assert [b.page for b in found] == [2]
    assert found[0].signal == "adequacy"


def test_jitter_between_pages_of_one_scan_is_not_a_boundary():
    """Within tolerance: the same document scanned page by page, not two documents."""
    view = _view(
        [
            _letter(1),
            PageInfo(page=2, width=612.5, height=792.4, unit="point"),
        ],
        [TextBlock(text="TEXT", zone=Zone.body, page=p) for p in (1, 2)],
    )

    assert candidate_boundaries(view) == []


def test_a_running_header_is_not_a_first_page_marker():
    """A marker on BOTH pages is page furniture. Splitting on it splits a document at
    every page of itself."""
    view = _view(
        [_letter(p) for p in (1, 2)],
        [
            TextBlock(text="OMB No. 1545-0116", zone=Zone.body, page=1),
            TextBlock(text="OMB No. 1545-0116", zone=Zone.body, page=2),
        ],
    )

    assert candidate_boundaries(view) == []


# ---------------------------------------------------------------------------
# A real bundle, built from real corpus documents
# ---------------------------------------------------------------------------
def _bundle(paths: list[str], pages_each: int = 3) -> bytes:
    out = fitz.open()
    for path in paths:
        src = fitz.open(path)
        out.insert_pdf(src, from_page=0, to_page=min(pages_each, src.page_count) - 1)
        src.close()
    data = out.tobytes()
    out.close()
    return data


def test_a_real_two_document_bundle_is_split_and_both_halves_are_right(tmp_path):
    """The case segmentation exists for, on real files.

    Whole-document classify() answers `us_w9` for this bundle and never mentions the bank
    statement — one correct-looking answer that silently omits half the upload.
    """
    from pathlib import Path

    from dce.ingest.pipeline import ingest

    root = Path(__file__).resolve().parent.parent / "corpus" / "us"
    w9, statement = root / "us_w9.pdf", root / "us_bank_statement.pdf"
    if not (w9.exists() and statement.exists()):
        pytest.skip("corpus documents not present")

    result = ingest(_bundle([str(w9), str(statement)]))
    assert result.view is not None

    assert classify(result.view).doctype_id == "us_w9", "the whole-document answer omits half"

    segments, boundaries = segment_document(result.view)

    assert [s.doctype_id for s in segments] == ["us_w9", "us_bank_statement"]
    assert segments[0].start_page == 1
    assert segments[1].end_page == result.view.pages[-1].page
    assert boundaries, "the split must carry its evidence"


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------
def _client():
    from fastapi.testclient import TestClient

    from dce.api.app import create_app

    return TestClient(create_app())


def _corpus_bundle() -> bytes | None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "corpus" / "us"
    paths = [root / "us_w9.pdf", root / "us_bank_statement.pdf"]
    if not all(p.exists() for p in paths):
        return None
    return _bundle([str(p) for p in paths])


def _post(path: str, data: bytes):
    import base64

    return _client().post(
        path,
        json={
            "ingest": {"filename": "bundle.pdf"},
            "content_base64": base64.b64encode(data).decode(),
        },
    )


def test_classify_segments_reports_both_documents():
    data = _corpus_bundle()
    if data is None:
        pytest.skip("corpus documents not present")

    body = _post("/api/v1/classify/segments", data).json()

    assert body["segmented"] is True
    assert [s["classification"]["doctype_id"] for s in body["segments"]] == [
        "us_w9",
        "us_bank_statement",
    ]
    assert body["boundaries"], "a split must say what proposed it"
    assert body["page_count"] == 6


def test_a_single_document_through_the_segment_endpoint_is_one_segment():
    """The uniformity contract: a caller never has to know whether it sent a bundle."""
    from pathlib import Path

    w9 = Path(__file__).resolve().parent.parent / "corpus" / "us" / "us_w9.pdf"
    if not w9.exists():
        pytest.skip("corpus documents not present")

    body = _post("/api/v1/classify/segments", w9.read_bytes()).json()

    assert body["segmented"] is False
    assert len(body["segments"]) == 1
    assert body["boundaries"] == []


def test_process_segments_extracts_each_document_separately():
    data = _corpus_bundle()
    if data is None:
        pytest.skip("corpus documents not present")

    body = _post("/api/v1/process/segments", data).json()

    assert len(body["segments"]) == 2
    for segment in body["segments"]:
        extraction = segment["extraction"]
        assert extraction is not None, "a classified segment is extracted"
        assert extraction["doctype_id"] == segment["classification"]["doctype_id"]


def test_every_extracted_segment_reports_what_ran():
    """A tier ledger per segment, because a console that has none reports "no tier ran".

    It did exactly that: /process/segments returned no tiers_used at all, so the Tiers panel
    said "the cascade stopped before extraction, so nothing was asked to fill a field" on a
    document T1 had just filled seven fields from. The ledger is the one place a reader looks
    to find out what a request cost, and an empty one is not a neutral omission — it is a
    false statement.
    """
    data = _corpus_bundle()
    if data is None:
        pytest.skip("corpus documents not present")

    body = _post("/api/v1/process/segments", data).json()

    for segment in body["segments"]:
        tiers = segment["tiers_used"]
        assert tiers, f"segment {segment['start_page']}-{segment['end_page']} reported no tiers"
        local = tiers[0]
        assert local["tier"] == "t1_local"
        if segment["extraction"] is not None:
            assert local["status"] == "ran"
            filled = sum(1 for f in segment["extraction"]["fields"] if f.get("value"))
            assert local["fields_filled"] == filled, "the ledger must match the extraction"


def test_an_abstaining_segment_says_why_nothing_ran():
    """"Nothing ran" and "nothing ran BECAUSE the cascade abstained" are different facts."""
    view = _view(
        [_letter(1), PageInfo(page=2, width=396.0, height=612.0, unit="point")],
        [
            TextBlock(text="zzz qqq", zone=Zone.body, page=1),
            TextBlock(text="zzz qqq", zone=Zone.body, page=2),
        ],
    )
    segments, _ = segment_document(view)

    # Nothing here classifies; the point is only that the API records the reason when it
    # happens, which the endpoint test above covers for the extracted case.
    assert all(s.classification.abstained for s in segments)


def test_no_paid_tier_runs_on_a_bundle():
    """T2/T3 bill per call and a bundle multiplies that by its segment count."""
    data = _corpus_bundle()
    if data is None:
        pytest.skip("corpus documents not present")

    body = _post("/api/v1/process/segments", data).json()

    for segment in body["segments"]:
        assert [t["tier"] for t in segment["tiers_used"]] == ["t1_local"]
        assert not any(t.get("cost_bearing") for t in segment["tiers_used"])


def test_the_plain_endpoints_are_untouched():
    """Existing callers keep their response shape; segmentation lives on new paths."""
    data = _corpus_bundle()
    if data is None:
        pytest.skip("corpus documents not present")

    body = _post("/api/v1/classify", data).json()

    assert "segments" not in body
    assert body["doctype_id"] == "us_w9"


def test_an_unidentifiable_span_does_not_become_its_own_document():
    """Absorption: "we cannot tell" is not evidence of a separate document.

    Without this, a landscape table or an unreadable exhibit inside a long filing is emitted
    as its own "document" — measured at 19.3% false splits across the corpus, where every
    file is a single document and therefore every split is wrong. With it, 6.0%.
    """
    view = _view(
        [
            _letter(1),
            PageInfo(page=2, width=792.0, height=612.0, unit="point"),  # a landscape table
            _letter(3),
        ],
        [
            TextBlock(text="FORM W-9", zone=Zone.title, page=1),
            TextBlock(text="Request for Taxpayer Identification Number", zone=Zone.body, page=1),
            TextBlock(text="1 2 3 4 5", zone=Zone.body, page=2),
            TextBlock(text="Certification instructions", zone=Zone.body, page=3),
        ],
    )

    assert candidate_boundaries(view), "the geometry change is still noticed"

    segments, _ = segment_document(view)

    assert len(segments) == 1, "but it does not survive as a document of its own"
    assert (segments[0].start_page, segments[0].end_page) == (1, 3)


def test_an_absorbed_span_is_reclassified_over_the_pages_it_claims():
    """The D5 defect, reintroduced once and caught by measurement.

    Absorbing a neighbour while keeping the head's verdict reports a classification drawn
    from a SUBSET of the pages the segment now covers. It turned a correctly identified
    47-page circular into `us_bylaws` — which is what its first page says, read alone.
    """
    view = _view(
        [_letter(1), _letter(2), _letter(3)],
        [
            TextBlock(text="STATEMENT OF ACCOUNT", zone=Zone.title, page=1),
            TextBlock(text="x", zone=Zone.body, page=2),
            TextBlock(text="ACCOUNT SUMMARY CLOSING BALANCE IBAN", zone=Zone.body, page=3),
        ],
    )

    segments, _ = segment_document(view)

    assert len(segments) == 1
    whole = classify(view)
    assert segments[0].doctype_id == whole.doctype_id, (
        "a segment covering every page must say what the whole document says"
    )


def test_boundary_carries_readable_evidence():
    view = _view(
        [_letter(1), PageInfo(page=2, width=396.0, height=612.0, unit="point")],
        [TextBlock(text="TEXT", zone=Zone.body, page=p) for p in (1, 2)],
    )

    found = candidate_boundaries(view)

    assert isinstance(found[0], Boundary)
    assert "width" in found[0].detail
