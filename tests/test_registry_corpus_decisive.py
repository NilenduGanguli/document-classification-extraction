"""The decisive-anchor invariant, enforced against documents instead of against the registry.

WHY THIS TEST EXISTS, AND WHY IT CANNOT BE A LOADER CHECK
--------------------------------------------------------
:mod:`dce.registry.loader` carries four checks on decisive anchors and every one of them
compares the registry against itself. That makes a *lone* false claim invisible by
construction: one pack declaring ``BIRTH CERTIFICATE`` decisive has no second claimant to
collide with, so nothing fires, however many foreign birth certificates the string goes on to
match. Four such claims survived review that way — ``in_aoa`` on
``ARTICLES OF ASSOCIATION``, ``in_birth_certificate`` on ``BIRTH CERTIFICATE``,
``us_lease_agreement`` on ``LEASE AGREEMENT`` and ``in_aoa`` on ``Table F`` — and when this
test was first run against the corpus it found **60**, across 47 doctypes, 56 of which had no
registry co-claimant at all.

The evidence that contradicts a lone false claim is not in the registry. It is documents. So
the property is stated here, directly, in the form the cascade actually cares about:

    **A decisive anchor must not match a document of a different doctype.**

Three consequences of stating it this way, each of which is why this shape was chosen over
tightening the loader further:

* It needs no judgement about what "issuer-controlled" means. That was the old rule, and
  measurement showed it is a bad proxy in both directions — it keeps ``Form W-9`` (printed on
  the corpus's 1099 and its 20-F) and demotes ``RATION CARD`` (printed on nothing else).
* It is deterministic and registry-size-independent: one anchor against one document, with no
  threshold and no normalisation over the doctype count.
* It gets **stronger** as the corpus grows, which is the opposite of the loader's checks —
  those get weaker as the registry grows relative to the evidence.

And it stays a test rather than a runtime check for two hard reasons: the service must not
depend on ``corpus/`` existing, and the loader must stay pure. A registry that can only
validate itself where a corpus is mounted is a registry that fails to load in production.

WHAT "MATCH" MEANS HERE
-----------------------
The classifier's own matcher, :func:`dce.classify.anchors._match_in` with ``decisive=True`` —
the same exact/skeleton comparison, with fuzzy matching refused, that L1 performs. Nothing is
re-implemented; a test that approximated the matcher would drift away from it and start
passing on documents the service still gets wrong.

**Zone gating is deliberately ignored.** ``us_lease_agreement`` gates ``LEASE AGREEMENT`` to
``zone=title``, and that mitigates the collision without preventing it: whether the anchor is
audible is a fact about the payload's zone labelling, not about the document, and an Azure
Document Intelligence ``title`` role landing on a section heading satisfies the gate. Eight of
the 60 offenders were zone-gated. A gate is a reason the failure is rarer, not a reason the
claim is true.

Scanned documents with no text layer are skipped, exactly as ``tools/corpus_test.py`` skips
them: absence of a text layer is absence of evidence, and OCR error would make a failure here
un-diagnosable.

.. note::

   Doctype ids beginning ``in_`` in the prose below cite the India pack, which was removed
   from the registry on 2026-08-14 and is preserved on the ``archive/india-doctypes``
   branch. The measurements they belong to were taken while it was present (181
   doctypes, 158 corpus documents) and are kept as taken rather than restated. The
   assertions in this file are all against doctypes that exist; only the narration is
   historical.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dce.classify.anchors import _match_in
from dce.models import Controls
from dce.normalize import NormalizedText, normalize
from dce.registry import loader

CORPUS = Path(__file__).resolve().parent.parent / "corpus"

pytest.importorskip("fitz", reason="PyMuPDF is needed to read the corpus PDFs")
pytestmark = pytest.mark.skipif(
    not CORPUS.is_dir(), reason="corpus/ is not present in this checkout"
)


def _manifest_rows() -> list[dict[str, str]]:
    """Every ``corpus/<cc>/manifest.jsonl`` row, sorted for deterministic iteration."""
    rows: list[dict[str, str]] = []
    for manifest in sorted(CORPUS.glob("*/manifest.jsonl")):
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return sorted(rows, key=lambda r: r["file"])


def _text_of(path: Path) -> str:
    """The document's text layer, or ``""`` when it has none.

    PDFs are read with PyMuPDF, as the corpus harness reads them. Everything else goes
    through the service's own :mod:`dce.ingest`, which is in-process and opens no socket —
    the same parser that produced the ``service_ingest`` rows in the corpus report.
    """
    data = path.read_bytes()
    if data[:5] == b"%PDF-":
        import fitz

        try:
            doc = fitz.open(str(path))
        except Exception:  # noqa: BLE001 - an unreadable file is not evidence about an anchor
            return ""
        with doc:
            if doc.needs_pass:
                return ""
            return "\n".join(
                doc.load_page(i).get_text("text") or "" for i in range(doc.page_count)
            )

    from dce.ingest import ingest
    from dce.ingest.result import IngestStatus

    try:
        result = ingest(data, filename=path.name)
    except Exception:  # noqa: BLE001 - unparseable is "no text layer", not a registry defect
        return ""
    if result.status is not IngestStatus.ok or result.view is None:
        return ""
    return "\n".join(block.text for block in result.view.blocks)


@pytest.fixture(scope="module")
def corpus_text() -> list[tuple[str, str, NormalizedText]]:
    """``(path, expected_doctype, normalised text)`` for every corpus document that has text.

    Module-scoped because reading 150 PDFs is the expensive part and the anchor sweep on top
    of it is cheap.
    """
    loaded: list[tuple[str, str, NormalizedText]] = []
    for row in _manifest_rows():
        path = CORPUS.parent / row["file"]
        if not path.exists():
            continue
        text = _text_of(path)
        if text.strip():
            loaded.append((row["file"], row["expected_doctype"], normalize(text)))
    if not loaded:
        pytest.skip("no corpus document yielded a text layer")
    return loaded


def test_no_decisive_anchor_matches_another_doctypes_document(corpus_text) -> None:
    """The invariant itself. Every violation is reported, not just the first.

    A failure here is **not** "add the doctype to ``confusable_with``". The string is printed
    on a document this doctype does not describe, so the decisive claim about it is false, and
    ``confusable_with`` does not make a false claim true — that was measured directly:
    ``us_green_card`` and ``ca_pr_card`` declared each other in both directions and a Canadian
    PR card was still classified ``us_green_card``. The fix is to demote the anchor, or to
    replace it with a string the issuer actually controls *and* that nothing cites.

    Note the two mechanisms that produce failures here, because neither is exotic:

    * **Citation.** ``Form W-9`` is printed on the corpus's 1099 ("Give Form W-9 to the
      requester") and on a 20-F. Perfectly issuer-controlled; defeated by being referenced.
    * **Acceptable-document lists.** ``corpus/ca/ca_sin_confirmation.pdf`` enumerates the ID
      it accepts and thereby prints six other doctypes' decisive anchors;
      ``corpus/in/in_form60.pdf`` prints four. This is a systematic hazard for a KYC
      classifier: onboarding paperwork names the document classes it collects.
    """
    violations: list[str] = []
    for spec in loader.all_specs():
        for anchor in spec.anchors:
            if not anchor.decisive:
                continue
            needle = normalize(anchor.text)
            hits = sorted(
                {
                    f"{expected} ({path})"
                    for path, expected, text in corpus_text
                    if expected != spec.doctype_id and _match_in(text, needle, decisive=True)
                }
            )
            if hits:
                violations.append(
                    f"{spec.doctype_id} declares {anchor.text!r} decisive "
                    f"(controls={anchor.controls.value if anchor.controls else None}, "
                    f"zone={anchor.zone.value if anchor.zone else None}) but it is printed on: "
                    + "; ".join(hits)
                )
    assert not violations, (
        f"{len(violations)} decisive anchor(s) match a document of another doctype:\n  - "
        + "\n  - ".join(violations)
    )


def test_class_name_uncontested_is_the_only_weak_tier_and_is_countable(corpus_text) -> None:
    """The weak tier is *declared*, so it can be counted — and it is bounded by the same rule.

    Two things are asserted, and the second is the one that matters:

    1. Every decisive anchor names its grounds. The loader already refuses otherwise; this
       repeats it against the loaded registry so a future bypass of ``register()`` cannot
       reintroduce an unjustified claim silently.
    2. The known-weak claims are a *minority* of the decisive surface. This is a tripwire on
       drift, not a quality bar: the tier is honest but it is the tier of "no evidence yet",
       and a registry where most decisive anchors are document-class names has stopped
       identifying documents by anything an issuer controls. The bound is deliberately loose
       (half) so it fires on a change of character rather than on ordinary growth.
    """
    decisive = [a for spec in loader.all_specs() for a in spec.anchors if a.decisive]
    unjustified = [a.text for a in decisive if a.controls is None]
    assert not unjustified, f"decisive anchors with no controls=: {unjustified}"

    weak = [a for a in decisive if a.controls is Controls.CLASS_NAME_UNCONTESTED]
    assert len(weak) * 2 < len(decisive), (
        f"{len(weak)} of {len(decisive)} decisive anchors are "
        f"{Controls.CLASS_NAME_UNCONTESTED.value} — the registry is now identifying documents "
        "mostly by their class names, which is what this field exists to make visible"
    )
