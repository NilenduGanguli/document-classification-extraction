"""A correct zone label must not un-gate zone-restricted anchors elsewhere in the payload.

The zone-free reading exists so that a layout label cannot *make* a decision — only sharpen
one. It earns that by discarding the provider's zone **weights**. It must not also discard the
registry's zone **restrictions**, which are claims about the document rather than opinions
about the page: ``SOCIAL SECURITY ADMINISTRATION`` identifies an SSN card when it is printed
across the top of one, and is a sentence about a US agency when it appears in running prose.

An earlier form of the guard evaluated the flattened payload with ``zone_blind=True``. That was
forced — flattening rewrites promoted blocks to ``body``, so a title-gated anchor has no title
left to match — but it un-gated all 21 title-gated decisive anchors *everywhere*, so a correct
masthead label silently made them audible in body text. Both cases below are the measured
consequence: each classified correctly with no roles, and became a confident cross-jurisdiction
identity determination once an unrelated, correct ``title`` label was added.

.. note::

   Doctype ids beginning ``in_`` in the prose below cite the India pack, which was removed
   from the registry on 2026-08-14 and is preserved on the ``archive/india-doctypes``
   branch. The measurements they belong to were taken while it was present (181
   doctypes, 158 corpus documents) and are kept as taken rather than restated. The
   assertions in this file are all against doctypes that exist; only the narration is
   historical.
"""
from __future__ import annotations

import pytest

from dce.adapters import from_plain_text
from dce.classify.cascade import classify
from dce.models import LayoutView, PageInfo, TextBlock, Zone
from dce.registry import all_specs

#: (case, document text, a body sentence that legitimately names a foreign issuer).
#: The sentence is ordinary prose, not a masthead — a Canadian record of landing is entitled to
#: mention the SSA, and a Mexican CURP constancia is entitled to explain what a CURP is
#: equivalent to.
CASES = [
    pytest.param(
        "PERMANENT RESIDENT / RESIDENT PERMANENT\nCONFIRMATION OF PERMANENT RESIDENCE\n"
        "IMM 5292\nName: SMITH, JANE\nDate of Birth: 1990-01-01",
        "Benefits paid by the SOCIAL SECURITY ADMINISTRATION are not reportable here.",
        id="ca_copr-mentions-ssa",
    ),
    pytest.param(
        "CONSTANCIA DE LA CLAVE ÚNICA DE REGISTRO DE POBLACIÓN\nRENAPO\n"
        "CURP: SMJA900101MDFXXX01\nNombre: JANE SMITH",
        "Equivalente al numero de la SOCIAL SECURITY ADMINISTRATION de Estados Unidos.",
        id="mx_curp-mentions-ssa",
    ),
]


@pytest.mark.parametrize(("body_text", "injected"), CASES)
def test_correct_title_label_does_not_ungate_anchors_in_body(body_text, injected):
    """Adding a correct masthead label must not change the verdict.

    The label is applied to the document's own first block — it is *right*. The failure this
    pins is not a mislabelling; it is a correct label making a gated anchor audible somewhere
    it was never meant to be heard.
    """
    specs = all_specs()
    payload = f"{body_text}\n{injected}"

    unlabelled = classify(from_plain_text(payload), specs=specs)

    labelled_view = from_plain_text(payload)
    labelled_view.blocks[0].zone = Zone.title
    labelled = classify(labelled_view, specs=specs)

    assert labelled.doctype_id == unlabelled.doctype_id, (
        f"a correct title label changed the verdict from {unlabelled.doctype_id!r} to "
        f"{labelled.doctype_id!r} — the zone-free reading un-gated a zone-restricted anchor"
    )
    # The specific harm: never a confident answer for the country whose issuer was merely
    # *named* in prose. An abstention here would be safe; a foreign doctype is not.
    assert not (labelled.country == "US" and not labelled.abstained), (
        f"mentioning a US agency in body text produced a confident US doctype "
        f"({labelled.doctype_id!r} at {labelled.confidence:.3f})"
    )


# --------------------------------------------------------------------------------------
# Zones must never make the answer worse (monotonicity in evidence).
# --------------------------------------------------------------------------------------

#: A CRA form RC1 ("Request for a Business Number and Certain Program Accounts") reduced to
#: the blocks that carry doctype signal, with the zones a layout provider really assigns it.
#: The load-bearing detail is that RC1 divides itself into "Part A", "Part B", "Part C" — and
#: a section divider is exactly what a provider labels ``heading``.
_RC1_BLOCKS = [
    ("Protected B when completed", Zone.furniture),
    ("Request for a Business Number and Certain Program Accounts", Zone.title),
    ("Canada Revenue Agency", Zone.heading),
    ("Part A - Business Number", Zone.heading),
    ("I want to register for a BN - Part A", Zone.body),
    ("Part B - Program account information", Zone.heading),
    ("GST/HST (RT) - Part B", Zone.body),
    ("Certificate number", Zone.body),
    ("Date of incorporation", Zone.body),
]


def _flattened(blocks: list[tuple[str, Zone]]) -> LayoutView:
    """The same document with every zone label discarded — what plain text looks like."""
    return LayoutView(
        doc_id="synthetic",
        pages=[PageInfo(page=1, width=1000, height=650)],
        blocks=[TextBlock(text=t, zone=Zone.body, page=1) for t, _ in blocks],
    )


def _zoned(blocks: list[tuple[str, Zone]]) -> LayoutView:
    return LayoutView(
        doc_id="synthetic",
        pages=[PageInfo(page=1, width=1000, height=650)],
        blocks=[TextBlock(text=t, zone=z, page=1) for t, z in blocks],
    )


def test_zone_labels_never_downgrade_a_correct_answer() -> None:
    """Adding real zone labels must not turn a correct answer into an abstention.

    This is the monotonicity property stated as a test: zones are *more* information, and more
    information must never produce a worse answer. It is pinned on the document that broke it.

    The failure it guards is subtle, because the losing doctype's own score went **up**. On CRA
    form RC1 the anchor channel scored ``ca_bn_letter`` and ``in_form16`` at exactly 2.2000
    with no zones — a dead tie, silently resolved by ``ca_`` sorting before ``in_``. Adding
    zones lifted ``ca_bn_letter`` to 3.16 and ``in_form16`` to 3.40, because Form 16 claimed
    "PART A"/"PART B" and RC1's section dividers are headings. So the better the layout
    information got, the more a generic ordinal divider outweighed a real issuer string, and a
    correct answer became an abstention. Both halves matter: a tie must not decide anything,
    and a string every jurisdiction's form designers use must not be evidence for one doctype.
    """
    specs = all_specs()
    flat = classify(_flattened(_RC1_BLOCKS), specs=specs)
    zoned = classify(_zoned(_RC1_BLOCKS), specs=specs)

    assert flat.doctype_id == "ca_bn_letter" and not flat.abstained, (
        f"the zone-free reading of CRA RC1 no longer identifies ca_bn_letter "
        f"(got {flat.doctype_id!r}, abstained={flat.abstained})"
    )
    assert zoned.doctype_id == "ca_bn_letter" and not zoned.abstained, (
        f"real zone labels downgraded a correct answer: zone-free said {flat.doctype_id!r} "
        f"but zoned said {zoned.doctype_id!r} (abstained={zoned.abstained}). More information "
        f"produced a worse answer — runners-up: {zoned.runners_up[:3]}"
    )


def test_an_ordinal_section_divider_is_not_evidence_for_any_doctype() -> None:
    """No doctype may anchor on a bare "Part A"-shaped string.

    The general property, stated independently of the document above: an anchor raises the
    posterior for ONE doctype, so it has to be a string one issuer controls. An ordinal section
    divider is controlled by nobody — every jurisdiction's form designers reach for it — and it
    is *worse* than inert, because dividers are what layout providers label ``heading``, so it
    collects a zone multiplier that ordinary body prose does not.

    A denylist is used rather than an allowlist on purpose: this names the shapes that are
    known to be uncontrolled, so a new pack inherits the guard without having to register for
    it, and a legitimate longer anchor that merely *begins* with "Part A" stays legal.
    """
    divider_shapes = {
        f"{word} {suffix}"
        for word in ("PART", "SECTION", "SCHEDULE", "ANNEX", "ANNEXURE", "APPENDIX")
        for suffix in ("A", "B", "C", "D", "I", "II", "III", "IV", "1", "2", "3", "4")
    }
    offenders = [
        (spec.doctype_id, anchor.text)
        for spec in all_specs()
        for anchor in spec.anchors
        if anchor.text.strip().upper() in divider_shapes
    ]
    assert not offenders, (
        "these anchors are bare ordinal section dividers, which no issuer controls and every "
        "layout provider labels as a heading: "
        + ", ".join(f"{d}:{t!r}" for d, t in offenders)
    )
