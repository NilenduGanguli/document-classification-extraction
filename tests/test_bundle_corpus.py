"""The bundle corpus: real multi-document files, scored against declared page ranges.

`corpus/<cc>/manifest.jsonl` maps one file to one doctype, so it cannot express a bundle —
and putting one there would score it as a wrong answer and corrupt the precision figure the
project is judged on. These live under `corpus/bundles/bundles.jsonl`, a filename
`corpus_test.py`'s `*/manifest.jsonl` glob deliberately does not match.

**What is asserted, and what deliberately is not.** Precision is asserted absolutely: a false
split is a compliance-grade failure, so zero is the only acceptable number and the test says
so. Recall is asserted only as a floor, because it is a property of what signals exist rather
than of correctness — the no-anchor bundle here CANNOT be split by any current signal, and a
test demanding it would be demanding a feature rather than guarding a behaviour.

Regenerate the files with `tools/make_bundles.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dce.classify.segments import candidate_boundaries, segment_document
from dce.ingest.pipeline import ingest

pytest.importorskip("fitz")

MANIFEST = Path(__file__).resolve().parent.parent / "corpus" / "bundles" / "bundles.jsonl"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _entries() -> list[dict]:
    if not MANIFEST.exists():
        return []
    return [json.loads(line) for line in MANIFEST.read_text().splitlines() if line.strip()]


ENTRIES = _entries()
if not ENTRIES:
    pytest.skip("bundle corpus not present", allow_module_level=True)


def _segments(entry: dict):
    data = (REPO_ROOT / entry["file"]).read_bytes()
    result = ingest(data)
    assert result.view is not None, f"{entry['file']} did not ingest"
    segments, boundaries = segment_document(result.view)
    return result, segments, boundaries


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: Path(e["file"]).stem)
def test_no_bundle_is_split_where_no_document_begins(entry: dict):
    """Precision, asserted absolutely. Every proposed boundary must be a real one.

    A false split turns one correct classification into two thinner-evidence ones and then
    runs extraction against each. There is no acceptable non-zero number here.
    """
    _, _, boundaries = _segments(entry)

    truth = set(entry["true_boundaries"])
    proposed = {b.page for b in boundaries}

    assert not (proposed - truth), (
        f"{Path(entry['file']).name}: split at {sorted(proposed - truth)} where no document "
        f"begins (real boundaries: {sorted(truth) or 'none'})"
    )


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: Path(e["file"]).stem)
def test_every_segment_covers_a_contiguous_range_of_the_file(entry: dict):
    """Whatever it decides, the segments must tile the document exactly once."""
    result, segments, _ = _segments(entry)

    assert segments, "at least one segment is always returned"
    pages = [p.page for p in result.view.pages]
    covered: list[int] = []
    for segment in segments:
        assert segment.start_page <= segment.end_page
        covered.extend(range(segment.start_page, segment.end_page + 1))

    assert covered == sorted(covered), "segments must be in page order"
    assert len(covered) == len(set(covered)), "no page may appear in two segments"
    assert set(covered) == set(pages), "every page belongs to exactly one segment"


def test_the_control_document_is_not_split():
    """A single document sent through segmentation must cost it nothing."""
    control = next((e for e in ENTRIES if e["documents"] == 1), None)
    if control is None:
        pytest.skip("no single-document control in the bundle corpus")

    _, segments, boundaries = _segments(control)

    assert len(segments) == 1
    assert boundaries == []


def test_a_bundle_with_no_structural_difference_is_honestly_missed():
    """The blind spot, pinned as a known limit rather than left to be rediscovered.

    Two same-size, text-bearing documents with no form or control number are structurally
    indistinguishable. All three signals are silent. This asserts the LIMIT — if a future
    signal makes this bundle split, this test should fail and be deleted, which is the point.
    """
    entry = next(
        (e for e in ENTRIES if "noanchor" in Path(e["file"]).stem),
        None,
    )
    if entry is None:
        pytest.skip("no no-anchor bundle in the corpus")

    result, segments, _ = _segments(entry)

    assert candidate_boundaries(result.view) == [], (
        "no signal should fire on this shape; if one now does, the blind spot has closed "
        "and this test should be replaced with a recall assertion"
    )
    assert len(segments) == 1


def test_bundles_are_not_visible_to_the_precision_corpus():
    """The reason for the filename. A bundle in `manifest.jsonl` corrupts the headline number.

    `corpus_test.py` globs `corpus/*/manifest.jsonl` and expects exactly one
    `expected_doctype` per file. A bundle holds several, so it would be scored as a wrong
    answer per bundle — silently lowering the precision figure the project is judged on.
    """
    found = sorted(p.as_posix() for p in (REPO_ROOT / "corpus").glob("*/manifest.jsonl"))

    assert found, "the country manifests should still be discoverable"
    assert not any("bundles" in path for path in found)
