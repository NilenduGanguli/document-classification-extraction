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
"""
from __future__ import annotations

import pytest

from dce.adapters import from_plain_text
from dce.classify.cascade import classify
from dce.models import Zone
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
