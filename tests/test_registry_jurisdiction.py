"""Cross-jurisdiction confusion: the defect, the class, and the two controls that close it.

The defect, reproduced verbatim by the lead::

    PERMANENT RESIDENT CARD
    CANADA
    ID  SMITH  JANE
    Sex F  Nationality CAN
    P<CANSMITH<<JANE<<<<

    -> us_green_card, country="US", confidence 0.900, abstained=False

Three separate signals in that document say Canada — the printed country name, the nationality
field, and the MRZ issuing State — and the cascade read none of them. It read one thing: that
``PERMANENT RESIDENT CARD`` was declared **decisive** for ``us_green_card`` and **non-decisive**
for ``ca_pr_card``, whose own decisive anchors are French. OCR loses a French line on a
bilingual card routinely; when it does, exactly one doctype holds a decisive anchor and the
identification route hands it the document.

This file pins the fix as a *class*, in two layers, because fixing the one pair would leave
the other ten instances the sweep found:

1. **Registry consistency, at import.** :func:`dce.registry.loader._check_decisive_asymmetry`
   refuses a decisive claim on a string another doctype also declares, unless both sides
   declare the overlap. The predecessor check compared decisive anchors only against *other
   decisive* anchors, which is exactly why this class slipped through — two decisive claims
   cancel each other, a decisive/non-decisive pair leaves one claimant standing.
2. **Classification-time, registry-derived.** A decisive anchor the registry says two doctypes
   print cannot carry the conclusive-L1 route, even when the overlap is legitimately declared.

**A third layer existed and was removed. This file is where that is recorded, because the
removal is the kind of thing somebody re-adds in good faith.**

``dce/registry/jurisdiction.py`` read the ICAO 9303 issuing-State field out of the payload's
machine-readable zone and deleted every doctype belonging to a different jurisdiction. Its
reasoning about *what to read* was sound and is not what failed — it correctly refused to read
the holder's nationality, a country name in running prose, or an MRZ name line. What failed is
the scope it read over. It read ``view.text()``: the whole payload.

A payload on the production API path is a KYC *packet* — ``dce/api/routes.py`` posts one
:class:`~dce.models.LayoutView` per upload to ``classify()``, and ``classify_pages()`` is not
reachable from any route. So one page carrying a foreign MRZ deleted 91 of 121 doctypes for
every page in the packet. A customer uploading a US licence and a Canadian passport together —
an ordinary submission — got::

    us_drivers_license.pdf text alone                 -> us_drivers_license  US  0.729  correct
    the same text + "P<CANSMITH<<JANE<<<<<<<<"        -> ca_drivers_license  CA  0.625  WRONG

Measured over the whole corpus: appending one foreign MRZ line to each document took
**CORRECT 36 -> 0 and WRONG 1 -> 14**, with 10 documents converting directly from correct to a
confident wrong doctype. One of the ten was ``ca_copr`` -> ``us_green_card`` at 0.53 — the veto
manufacturing the very cross-jurisdiction identity determination it was written to prevent.

**Why it was removed rather than scoped.** Scoping requires a unit of "this document" smaller
than the payload. ``classify()`` has none: it fuses one text over one view and returns one
doctype, and the page is the finest segmentation that exists anywhere in the cascade
(``classify_pages()``, which no route calls). Page-scoping does not even close the repro above,
where the licence and the MRZ line share a page. Anything finer means giving ``classify()``
per-document segmentation, which is a restructuring, not a scoping.

**And it was not load-bearing.** The registry demotion in layer 1 already removed the dangerous
outcome on its own — the ``ca_pr_card`` reproduction abstains without the veto instead of
claiming ``us_green_card``. The veto's only remaining effect was to upgrade that safe abstention
to a correct answer, while converting correct answers elsewhere into confident wrong ones. Under
the standing constraint — abstention is safe, a wrong doctype is a compliance incident — that
trade is net-negative, so the abstention is the honest outcome and is what this file now pins.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:  # keeps the suite runnable without an installed package
    sys.path.insert(0, str(_REPO_ROOT))

from dce.classify.cascade import classify, load_registry  # noqa: E402
from dce.config import Settings  # noqa: E402
from dce.models import (  # noqa: E402
    Anchor,
    Category,
    DocTypeSpec,
    LayoutView,
    PageInfo,
    TextBlock,
    Zone,
)
from dce.registry import loader  # noqa: E402

SETTINGS = Settings(_env_file=None)

#: The lead's reproduction, character for character.
PR_CARD_REPRO = (
    "PERMANENT RESIDENT CARD\n"
    "CANADA\n"
    "ID  SMITH  JANE\n"
    "Sex F  Nationality CAN\n"
    "P<CANSMITH<<JANE<<<<"
)

#: One ICAO document-line opener naming a jurisdiction that is not the document's own. This is
#: the whole of the contamination: a second document in the same upload.
FOREIGN_MRZ_LINE = "P<CANSMITH<<JANE<<<<<<<<"


def view_of(text: str) -> LayoutView:
    """One block per line, every block ``body`` — what a text-layer PDF actually produces."""
    return LayoutView(
        doc_id="jurisdiction-test",
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")],
        blocks=[TextBlock(text=line, zone=Zone.body) for line in text.split("\n")],
    )


# ---------------------------------------------------------------------------
# (a) The reproduction
# ---------------------------------------------------------------------------
def test_the_canadian_pr_card_is_never_a_us_green_card() -> None:
    """THE regression. A Canadian PR card must not become a US immigration determination.

    The acceptance condition is deliberately weak in one direction and absolute in the other:
    classifying as ``ca_pr_card`` is right and abstaining is safe, but a confident US answer is
    a compliance incident — it puts a Canadian permanent resident into US immigration-status
    logic. Asserting only "not us_green_card" rather than "== ca_pr_card" keeps this test
    honest about what the system owes: it owes precision, not coverage.

    This now passes on layer 1 alone. The MRZ veto that used to answer it was removed (see the
    module docstring), and the registry demotion it was added alongside carries the case by
    itself — the reproduction abstains rather than claiming ``us_green_card``. An abstention is
    the outcome this assertion was written to permit, and it is the one being pinned.
    """
    result = classify(view_of(PR_CARD_REPRO), load_registry(), settings=SETTINGS)

    assert result.doctype_id != "us_green_card", (
        f"the cross-jurisdiction defect is back: {result.doctype_id} at {result.confidence}"
    )
    assert result.country != "US", (
        f"a document whose MRZ issuing State is CAN was given country={result.country!r}"
    )
    assert result.abstained or result.doctype_id == "ca_pr_card", (
        f"the only acceptable non-abstention here is ca_pr_card, got {result.doctype_id!r}"
    )


def test_a_refusal_on_the_reproduction_still_says_why() -> None:
    """A KYC answer — accepted or refused — must carry its reasoning.

    The MRZ line in the audit trail went with the veto. What must not go with it is the
    obligation to explain: whatever this document gets, the evidence list names the candidate
    that came closest and the gate it failed, so a reviewer is never handed a bare refusal.
    """
    result = classify(view_of(PR_CARD_REPRO), load_registry(), settings=SETTINGS)

    assert result.evidence, "an answer with no evidence is not reviewable"
    if result.abstained:
        assert result.reason, "a refusal with no reason cannot be actioned by a human"


# ---------------------------------------------------------------------------
# (b) The class — no undeclared decisive/non-decisive asymmetry anywhere
# ---------------------------------------------------------------------------
def test_no_decisive_anchor_is_claimed_by_another_doctype_undeclared() -> None:
    """The whole-registry sweep, as an assertion rather than a one-off script.

    Eleven cross-doctype instances existed when this was written, six of them undeclared and
    five of those cross-country: ``ACCOUNT STATEMENT`` (IN decisive vs CA and MX),
    ``CERTIFICATE OF INCORPORATION`` (two IN decisive vs CA and US), ``PASAPORTE`` (MX decisive
    vs XX), ``ARTICLES OF INCORPORATION`` (US decisive vs CA) and ``आयकर विभाग`` (in_pan
    decisive vs in_form16). ``PERMANENT RESIDENT CARD`` and ``IDENTIFICATION CARD`` were
    *declared* and still misbehaved, which is why declaration alone is not the fix.
    """
    specs = load_registry()
    by_id = {spec.doctype_id: spec for spec in specs}
    claims = loader.anchor_claims(specs)

    offences: list[str] = []
    for spec in specs:
        for anchor in spec.anchors:
            if not anchor.decisive:
                continue
            others = claims.get(loader.anchor_claim_key(anchor.text), frozenset()) - {
                spec.doctype_id
            }
            for other in sorted(others):
                if other not in spec.confusable_with or spec.doctype_id not in (
                    by_id[other].confusable_with or {}
                ):
                    offences.append(f"{spec.doctype_id}:{anchor.text!r} vs {other}")
    assert not offences, "undeclared decisive/non-decisive asymmetry: " + "; ".join(offences)


def test_no_decisive_anchor_crosses_a_country_boundary_at_all() -> None:
    """Stronger than the loader check, and the one that matters for KYC.

    A *declared* overlap between two doctypes of the same issuer is legitimate — a masked and
    an unmasked Aadhaar share the UIDAI header. A decisive claim on a string a *different
    jurisdiction* also prints is not legitimate under any declaration, because the error it
    produces is a cross-border identity determination rather than a within-family ambiguity.
    Declaring ``us_green_card`` confusable with ``ca_pr_card`` did not stop the misclassification
    and could not have.
    """
    specs = load_registry()
    by_id = {spec.doctype_id: spec for spec in specs}
    claims = loader.anchor_claims(specs)

    offences: list[str] = []
    for spec in specs:
        for anchor in spec.anchors:
            if not anchor.decisive:
                continue
            others = claims.get(loader.anchor_claim_key(anchor.text), frozenset()) - {
                spec.doctype_id
            }
            for other in sorted(others):
                if by_id[other].country != spec.country:
                    offences.append(
                        f"{spec.doctype_id} ({spec.country}) declares {anchor.text!r} decisive; "
                        f"{other} ({by_id[other].country}) also prints it"
                    )
    assert not offences, "cross-jurisdiction decisive claim: " + "; ".join(offences)


def check_asymmetry(*specs: DocTypeSpec) -> list[str]:
    """Run the loader's asymmetry check over a synthetic registry, then restore the real one."""
    errors: list[str] = []
    registry = loader._REGISTRY
    saved = dict(registry)
    try:
        registry.clear()
        registry.update({spec.doctype_id: spec for spec in specs})
        loader._check_decisive_asymmetry(errors)
    finally:
        registry.clear()
        registry.update(saved)
    return errors


def zz_pair(*, declare: bool, right_country: str) -> tuple[DocTypeSpec, DocTypeSpec]:
    """Two doctypes printing one string: decisive on the left, non-decisive on the right."""
    left = DocTypeSpec(
        doctype_id="zz_alpha",
        label="Alpha",
        country="US",
        category=Category.identity,
        anchors=[Anchor(text="SHARED CARD TITLE", decisive=True)],
        confusable_with={"zz_beta": "separated by X"} if declare else {},
    )
    right = DocTypeSpec(
        doctype_id="zz_beta",
        label="Beta",
        country=right_country,
        category=Category.identity,
        # Non-decisive, differently cased, and gated to a different zone. None of those three
        # makes it a different claim, which is the whole point of ``anchor_claim_key``.
        anchors=[Anchor(text="Shared Card Title", zone=Zone.title)],
        confusable_with={"zz_alpha": "separated by X"} if declare else {},
    )
    return left, right


def test_the_loader_refuses_a_new_undeclared_asymmetry_at_import() -> None:
    """A future pack must fail loudly at startup, not pick a silent winner in production.

    This is the check the round-1 auditor asked for: the predecessor
    ``_check_decisive_collisions`` "only compares decisive anchors against OTHER decisive
    anchors", which is exactly why this class slipped through — two decisive claims cancel,
    but a decisive/non-decisive pair leaves one claimant standing and is silently accepted.
    """
    errors = check_asymmetry(*zz_pair(declare=False, right_country="US"))

    assert errors, "an undeclared decisive/non-decisive asymmetry passed validation"
    assert "zz_alpha" in errors[0] and "zz_beta" in errors[0]
    assert "SHARED CARD TITLE" in errors[0]


def test_declaring_a_same_country_overlap_is_accepted() -> None:
    """One issuer's document family may share a string, provided it says so both ways."""
    assert check_asymmetry(*zz_pair(declare=True, right_country="US")) == []


def test_declaring_a_cross_country_overlap_is_still_refused() -> None:
    """Declaration is not a remedy across jurisdictions, and the loader must not accept it.

    Measured, and the reason this case is separated from the one above: ``us_green_card`` and
    ``ca_pr_card`` declared each other in both directions with the separating term spelled out,
    and ``corpus/ca/ca_pr_card.pdf`` still classified as ``us_green_card`` at 0.545. Suppressing
    the conclusive-L1 route (which the classification-time rule does) leaves the *concurrence*
    route, which reads the anchor score — and ``decisive=True`` multiplies that score by 2.0.
    A cross-jurisdiction claim has to leave the registry; nothing downstream can neutralise it.
    """
    errors = check_asymmetry(*zz_pair(declare=True, right_country="CA"))

    assert errors, "a declared CROSS-COUNTRY decisive claim must still fail validation"
    assert "DEMOTED, not" in errors[0], (
        "the error must tell the author that declaring is not the remedy here"
    )


# ---------------------------------------------------------------------------
# (c) Classification-time: a contested decisive claim is not conclusive
# ---------------------------------------------------------------------------
def contested_pair() -> list[DocTypeSpec]:
    """The abstract shape of us_green_card / ca_pr_card, with the asymmetry declared."""
    us = DocTypeSpec(
        doctype_id="zz_us_card",
        label="US Residence Card",
        country="US",
        category=Category.identity,
        anchors=[
            Anchor(text="RESIDENCE CARD", decisive=True),
            Anchor(text="Department of Homeland Security"),
        ],
        confusable_with={"zz_ca_card": "the Canadian card is bilingual"},
    )
    ca = DocTypeSpec(
        doctype_id="zz_ca_card",
        label="Canadian Residence Card",
        country="CA",
        category=Category.identity,
        anchors=[
            Anchor(text="CARTE DE RESIDENCE", lang="fr", decisive=True),
            Anchor(text="RESIDENCE CARD"),
        ],
        confusable_with={"zz_us_card": "the US card names Homeland Security"},
    )
    return [us, ca]


def test_a_decisive_anchor_two_doctypes_print_is_not_conclusive() -> None:
    """The general rule, on a synthetic pair, with the French line lost exactly as in the wild.

    Only ``zz_us_card`` holds a decisive *hit* here — the Canadian doctype's French anchor is
    genuinely absent, not merely unmeasurable, so the muted-claim guard does not fire. The
    registry nevertheless says both doctypes print ``RESIDENCE CARD``, and that is what
    disqualifies the claim.
    """
    from dce.classify.anchors import anchor_scores
    from dce.classify.cascade import _conclusive_l1

    specs = contested_pair()
    view = view_of("RESIDENCE CARD\nCANADA\nSMITH JANE")
    anchor = anchor_scores(view, specs, settings=SETTINGS)

    assert anchor.decisive_doctypes() == ("zz_us_card",), (
        "precondition: exactly one doctype is HEARD making a decisive claim"
    )
    assert not anchor.muted_decisive.get("zz_ca_card"), (
        "precondition: the French claim is absent, not muted — the existing guard cannot help"
    )

    conclusive, _muted, shared = _conclusive_l1(anchor, specs)
    assert conclusive is None, "a string both doctypes print must not identify either"
    assert shared, "the audit trail must name the contested claim"
    assert shared[0][0] == "RESIDENCE CARD"
    assert shared[0][1] == ("zz_ca_card",)


def test_one_exclusive_decisive_anchor_still_earns_the_route() -> None:
    """The rule must not disarm decisive anchors generally — only the contested ones.

    A doctype holding one exclusive decisive anchor keeps its identification route even when
    its *other* decisive anchors are shared. Losing that would delete the path that produces
    most of this cascade's correct answers, which is the failure mode the cascade docstring
    warns about at length.
    """
    from dce.classify.anchors import anchor_scores
    from dce.classify.cascade import _conclusive_l1

    specs = contested_pair()
    specs[0].anchors.append(Anchor(text="FORM I-551 UNIQUE HEADER", decisive=True))
    view = view_of("RESIDENCE CARD\nFORM I-551 UNIQUE HEADER\nSMITH JANE")
    anchor = anchor_scores(view, specs, settings=SETTINGS)

    conclusive, _muted, shared = _conclusive_l1(anchor, specs)
    assert conclusive == "zz_us_card"
    assert not shared




# ---------------------------------------------------------------------------
# (d) Payload contamination — the regression that removed the veto
# ---------------------------------------------------------------------------
#: A minimal US Form W-9, written from the form's public title block. Short enough to read,
#: confident enough to be a real accept, and — the point — a *US* document.
W9_PAGE = (
    "Form W-9\n"
    "(Rev. March 2024)\n"
    "Request for Taxpayer Identification Number and Certification\n"
    "Department of the Treasury\n"
    "Internal Revenue Service\n"
    "Go to www.irs.gov/FormW9 for instructions and the latest information.\n"
    "1 Name of entity/individual. An entry is required.\n"
    "2 Business name/disregarded entity name, if different from above\n"
    "Part I Taxpayer Identification Number (TIN)\n"
    "Part II Certification\n"
    "Under penalties of perjury, I certify that:\n"
)

#: A Canadian passport bio page. The *second document* in the upload — not a second reading of
#: the first one. Nothing here is evidence about the W-9 above it.
CA_PASSPORT_PAGE = (
    "PASSPORT PASSEPORT\n"
    "CANADA\n"
    "Type/Type P  Code/Code CAN\n"
    "P<CANSMITH<<JANE<<<<<<<<<<<<<<<<<<<<<<<<<<<<\n"
)

#: The lead's reproduction document, when the corpus is checked out beside the suite.
_DRIVERS_LICENCE_PDF = _REPO_ROOT / "corpus" / "us" / "us_drivers_license.pdf"


def answer(text: str) -> tuple[str, bool]:
    """``(doctype_id, abstained)`` for one payload — the pair a caller acts on."""
    result = classify(view_of(text), load_registry(), settings=SETTINGS)
    return result.doctype_id, result.abstained


def test_a_second_document_in_the_packet_does_not_reclassify_the_first() -> None:
    """THE round-3 regression, self-contained: a KYC packet is not one jurisdiction.

    A customer uploads a W-9 and a Canadian passport in one submission. That is an ordinary
    day, not an attack. The passport's MRZ is evidence about the passport and about nothing
    else in the file, so appending it must leave the W-9's answer exactly where it was.

    With the payload-wide MRZ veto in place this abstained: the Canadian issuing State deleted
    all 91 non-Canadian doctypes, ``us_w9`` among them, and the packet went to a human. On the
    lead's real ``us_drivers_license.pdf`` the same edit did worse than abstain — it returned
    ``ca_drivers_license`` at 0.625, a confident foreign-jurisdiction identity determination.
    """
    alone = answer(W9_PAGE)
    assert alone == ("us_w9", False), f"precondition: the W-9 must classify on its own, got {alone}"

    with_passport = answer(W9_PAGE + CA_PASSPORT_PAGE)
    assert with_passport == alone, (
        "a second document's MRZ changed the first document's answer: "
        f"{alone} became {with_passport}"
    )


def test_a_bare_foreign_mrz_line_changes_nothing() -> None:
    """The same property reduced to its smallest form: one line, no other new evidence.

    ``P<CAN...`` is one more line of body text. It matches no anchor of any doctype, so a rule
    that reads only evidence *for* doctypes cannot see it at all — and after the veto's removal,
    nothing else looks at it either. Any future control that reads jurisdiction off the whole
    payload will fail here first.
    """
    assert answer(W9_PAGE + FOREIGN_MRZ_LINE + "\n") == answer(W9_PAGE)


@pytest.mark.skipif(
    not _DRIVERS_LICENCE_PDF.exists(), reason="corpus/us/us_drivers_license.pdf is not present"
)
def test_the_leads_drivers_licence_reproduction_verbatim() -> None:
    """The lead's reproduction on the real document, when the corpus is available.

    The synthetic packet above is the portable version of this and is the one that always runs;
    this is the instance that was actually measured, and it is the one that produced a confident
    *wrong* answer rather than an abstention. Never a foreign jurisdiction, whatever else
    changes.
    """
    fitz = pytest.importorskip("fitz", reason="PyMuPDF is not installed")
    with fitz.open(_DRIVERS_LICENCE_PDF) as pdf:
        text = "\n".join(page.get_text("text") or "" for page in pdf)

    clean_id, clean_abstained = answer(text)
    assert (clean_id, clean_abstained) == ("us_drivers_license", False), (
        f"precondition: the licence must classify on its own, got {clean_id}"
    )

    dirty_id, dirty_abstained = answer(text + "\n" + FOREIGN_MRZ_LINE + "\n")
    assert dirty_id != "ca_drivers_license", (
        "payload contamination is back: one appended MRZ line turned a US licence into "
        f"{dirty_id}, a confident foreign-jurisdiction identity determination"
    )
    assert dirty_abstained or dirty_id == "us_drivers_license", (
        f"the only acceptable non-abstention here is us_drivers_license, got {dirty_id!r}"
    )
