"""Classification tests. Offline, pure, no registry required — every spec is built here.

The first test is the one that matters. This service exists because a substring matcher
classified ordinary English prose as a driving licence, an EIN letter and a SIN card at the
same time, and a real passport at 0.5335 — below its own 0.55 floor. Both halves of that
failure are pinned below.

.. note::

   Doctype ids beginning ``in_`` in the prose below cite the India pack, which was removed
   from the registry on 2026-08-14 and is preserved on the ``archive/india-doctypes``
   branch. The measurements they belong to were taken while it was present (181
   doctypes, 158 corpus documents) and are kept as taken rather than restated. The
   assertions in this file are all against doctypes that exist; only the narration is
   historical.
"""
from __future__ import annotations

import math
import sys
from itertools import pairwise
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:  # keeps the suite runnable without an installed package
    sys.path.insert(0, str(_REPO_ROOT))

from dce.classify import (  # noqa: E402
    anchor_scores,
    build_profiles,
    classify,
    classify_pages,
    lexical_scores,
)
from dce.classify.structural import structural_features  # noqa: E402
from dce.config import Settings  # noqa: E402
from dce.models import (  # noqa: E402
    UNKNOWN,
    Anchor,
    Category,
    Cell,
    DocTypeSpec,
    FieldSpec,
    KeyValue,
    LayoutView,
    PageInfo,
    Table,
    TextBlock,
    Zone,
)

SETTINGS = Settings(_env_file=None)

# A real, checksum-valid ICAO 9303 TD3 zone: all four check digits agree.
MRZ_LINE_1 = "P<USAERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<"
MRZ_LINE_2 = "X123456785USA7408122F3204153ZE184226B<<<<<18"


# ---------------------------------------------------------------------------
# Registry fixtures — built in code, mirroring the shape of the real registry
# ---------------------------------------------------------------------------
def passport_spec() -> DocTypeSpec:
    return DocTypeSpec(
        doctype_id="passport",
        label="Passport",
        country="XX",
        category=Category.identity,
        issuing_authority="Department of State",
        officially_valid=True,
        anchors=[
            Anchor(text="PASSPORT", decisive=True),
            Anchor(text="PASAPORTE", lang="es", decisive=True),
            Anchor(text="Type/Type"),
            Anchor(text="Authority"),
        ],
        id_patterns=[r"P<[A-Z]{3}"],
        fields=[
            FieldSpec(
                name="passport_number",
                attribute_key="id.passport_number",
                type="id",
                pii=True,
                validator="mrz_td3",  # names match dce.extract.validate's registry

                locators=["mrz", "label"],
                labels={"en": ["Passport No", "Document No"]},
            ),
            FieldSpec(
                name="surname",
                attribute_key="identity.surname",
                labels={"en": ["Surname"], "es": ["Apellidos"]},
                locators=["mrz", "label"],
            ),
            FieldSpec(
                name="date_of_birth",
                attribute_key="identity.date_of_birth",
                type="date",
                labels={"en": ["Date of birth"], "es": ["Fecha de nacimiento"]},
                locators=["mrz", "label"],
            ),
        ],
    )


def driver_license_spec() -> DocTypeSpec:
    return DocTypeSpec(
        doctype_id="us_driver_license",
        label="US Driver License",
        country="US",
        category=Category.identity,
        anchors=[
            Anchor(text="DRIVER LICENSE", decisive=True),
            Anchor(text="DRIVER'S LICENSE", decisive=True),
            Anchor(text="DL"),
            Anchor(text="USA"),
            Anchor(text="CLASS"),
        ],
        fields=[
            FieldSpec(
                name="license_number",
                attribute_key="id.driver_license",
                labels={"en": ["DL No", "License Number"]},
            ),
            FieldSpec(
                name="expiry_date",
                attribute_key="doc.expiry_date",
                type="date",
                labels={"en": ["Exp", "Expires"]},
            ),
        ],
    )


def ein_letter_spec() -> DocTypeSpec:
    return DocTypeSpec(
        doctype_id="us_ein_letter",
        label="US EIN Letter",
        country="US",
        category=Category.tax,
        issuing_authority="Internal Revenue Service",
        applies_to="corporate",
        anchors=[
            Anchor(text="EMPLOYER IDENTIFICATION NUMBER", decisive=True),
            Anchor(text="INTERNAL REVENUE SERVICE", decisive=True),
            Anchor(text="EIN"),
            Anchor(text="CP 575"),
        ],
        id_patterns=[r"\b\d{2}-\d{7}\b"],
        fields=[
            FieldSpec(
                name="ein",
                attribute_key="id.ein",
                type="id",
                validator="ein",
                labels={"en": ["EIN", "Employer Identification Number"]},
            )
        ],
    )


def ca_sin_spec() -> DocTypeSpec:
    return DocTypeSpec(
        doctype_id="ca_sin",
        label="Canadian Social Insurance Number",
        country="CA",
        category=Category.identity,
        anchors=[
            Anchor(text="SOCIAL INSURANCE NUMBER", decisive=True),
            Anchor(text="NUMERO D'ASSURANCE SOCIALE", lang="fr", decisive=True),
            Anchor(text="SIN"),
        ],
        id_patterns=[r"\b\d{3}-\d{3}-\d{3}\b"],
        fields=[
            FieldSpec(
                name="sin",
                attribute_key="id.sin",
                type="id",
                validator="sin_luhn",
                labels={"en": ["SIN"]},
            )
        ],
    )


def rfc_csf_spec() -> DocTypeSpec:
    return DocTypeSpec(
        doctype_id="mx_rfc_csf",
        label="Constancia de Situacion Fiscal",
        country="MX",
        category=Category.tax,
        issuing_authority="Servicio de Administracion Tributaria",
        anchors=[
            Anchor(text="CONSTANCIA DE SITUACIÓN FISCAL", lang="es", decisive=True),
            Anchor(text="REGISTRO FEDERAL DE CONTRIBUYENTES", lang="es", decisive=True),
            Anchor(text="RFC", lang="es"),
            Anchor(text="SAT", lang="es"),
        ],
        id_patterns=[r"\b[A-ZÑ&]{3,4}\d{6}[0-9A-Z]{2}[0-9A]\b"],
        fields=[
            FieldSpec(
                name="rfc",
                attribute_key="id.rfc",
                type="id",
                validator="rfc",
                labels={"es": ["RFC", "Registro Federal de Contribuyentes"]},
            )
        ],
    )


def bank_statement_spec() -> DocTypeSpec:
    return DocTypeSpec(
        doctype_id="bank_statement",
        label="Bank Statement",
        country="XX",
        category=Category.financial,
        anchors=[
            Anchor(text="STATEMENT OF ACCOUNT"),
            Anchor(text="ACCOUNT SUMMARY"),
            Anchor(text="BEGINNING BALANCE"),
            Anchor(text="CLOSING BALANCE"),
            Anchor(text="IBAN"),
        ],
        fields=[
            FieldSpec(
                name="account_number",
                attribute_key="account.number",
                labels={"en": ["Account Number", "Account No"]},
            ),
            FieldSpec(
                name="closing_balance",
                attribute_key="account.balance",
                type="number",
                labels={"en": ["Closing Balance"]},
            ),
            FieldSpec(
                name="statement_date",
                attribute_key="doc.issue_date",
                type="date",
                labels={"en": ["Statement Date"]},
            ),
        ],
    )


def utility_bill_spec() -> DocTypeSpec:
    return DocTypeSpec(
        doctype_id="utility_bill",
        label="Utility Bill",
        country="XX",
        category=Category.address_proof,
        anchors=[
            Anchor(text="ELECTRICITY BILL"),
            Anchor(text="METER READING"),
            Anchor(text="UNITS CONSUMED"),
            Anchor(text="SUPPLY ADDRESS"),
            Anchor(text="BILLING PERIOD"),
        ],
        fields=[
            FieldSpec(
                name="account_number",
                attribute_key="account.number",
                labels={"en": ["Account Number"]},
            ),
            FieldSpec(
                name="service_address",
                attribute_key="address.residential",
                type="address",
                labels={"en": ["Service Address", "Supply Address"]},
            ),
            FieldSpec(
                name="statement_date",
                attribute_key="doc.issue_date",
                type="date",
                labels={"en": ["Statement Date", "Bill Date"]},
            ),
        ],
    )


def registry() -> list[DocTypeSpec]:
    """The fixture registry used by the cascade tests."""
    return [
        passport_spec(),
        driver_license_spec(),
        ein_letter_spec(),
        ca_sin_spec(),
        rfc_csf_spec(),
        bank_statement_spec(),
        utility_bill_spec(),
    ]


def view_of(
    blocks: list[TextBlock],
    *,
    pages: list[PageInfo] | None = None,
    tables: list[Table] | None = None,
    key_values: list[KeyValue] | None = None,
    doc_id: str = "test",
) -> LayoutView:
    """Assemble a LayoutView, defaulting to one portrait A4 page."""
    return LayoutView(
        doc_id=doc_id,
        pages=pages or [PageInfo(page=1, width=8.5, height=11.0, unit="inch")],
        blocks=blocks,
        tables=tables or [],
        key_values=key_values or [],
    )


# ---------------------------------------------------------------------------
# (a) The regression that motivated this service
# ---------------------------------------------------------------------------
PROSE = (
    "In the middle of the afternoon on Saturday, the team was being careful about "
    "using the new process. La causa principal es que el equipo estaba usando el "
    "mismo proceso de siempre, sin causar problemas en el middle del proyecto."
)


def test_ordinary_prose_produces_no_anchor_matches():
    """'middle', 'being', 'using', 'Saturday', 'causa' must not fire DL/EIN/SIN/USA/SAT.

    The previous implementation used ``needle in haystack``: ``DL`` matched inside "mi**dl**e",
    ``EIN`` inside "b**ein**g", ``SIN`` inside "u**sin**g", ``SAT`` inside "**Sat**urday" and
    ``USA`` inside "ca**usa**"/"**usa**ndo". Every one of those is a KYC misclassification.
    """
    view = view_of([TextBlock(text=PROSE, zone=Zone.body)])
    outcome = anchor_scores(view, registry(), settings=SETTINGS)

    assert outcome.hits == {}, f"substring false positives are back: {outcome.hits}"
    assert all(score == 0.0 for score in outcome.scores.values())
    assert outcome.verified_doctypes() == ()


def test_ordinary_prose_abstains_to_unknown():
    """The full cascade must abstain on prose, with a reason a human can act on."""
    view = view_of([TextBlock(text=PROSE, zone=Zone.body)])
    result = classify(view, registry(), settings=SETTINGS)

    assert result.doctype_id == UNKNOWN
    assert result.abstained is True
    assert result.coverage == 0.0
    assert "coverage" in result.reason
    assert "human review" in result.reason
    assert result.ms >= 0


def test_substring_traps_are_absent_from_every_tier():
    """No tier — anchors, lexical or fused — may prefer a class on substring evidence."""
    view = view_of([TextBlock(text=PROSE, zone=Zone.body)])
    specs = registry()
    lexical = lexical_scores(view, build_profiles(specs), settings=SETTINGS)

    assert all(raw == 0.0 for raw in lexical.raw.values()), lexical.raw
    assert all(cov == 0.0 for cov in lexical.coverage.values())


# ---------------------------------------------------------------------------
# (b) A realistic passport must classify confidently
# ---------------------------------------------------------------------------
def passport_view() -> LayoutView:
    """A passport data page as the layout provider would hand it over."""
    return view_of(
        [
            TextBlock(text="PASSPORT", zone=Zone.title, page=1),
            TextBlock(text="UNITED STATES OF AMERICA", zone=Zone.heading, page=1),
            TextBlock(text="Type/Type  Code/Code  Passport No./No. du Passeport", zone=Zone.body),
            TextBlock(text="P  USA  X12345678", zone=Zone.body),
            TextBlock(text="Surname/Nom: ERIKSSON", zone=Zone.body),
            TextBlock(text="Given Names/Prenoms: ANNA MARIA", zone=Zone.body),
            TextBlock(text="Nationality: UNITED STATES OF AMERICA", zone=Zone.body),
            TextBlock(text="Date of birth: 12 AUG 1974", zone=Zone.body),
            TextBlock(text="Authority: UNITED STATES DEPARTMENT OF STATE", zone=Zone.body),
            TextBlock(text=f"{MRZ_LINE_1}\n{MRZ_LINE_2}", zone=Zone.body),
        ],
        pages=[PageInfo(page=1, width=8.5, height=6.0, unit="inch")],
    )


def test_passport_classifies_confidently():
    """The old implementation scored a real passport at 0.5335, below its own 0.55 floor.

    ``confidence`` is no longer a probability and is not asserted as one. It is the distance
    to whichever gate came closest to blocking the accept, on a scale where 0.5 is exactly the
    decision boundary — so the assertion that carries meaning is "comfortably clear of the
    boundary", not "close to 1.0". A number in [0, 1] that looked like a posterior but was not
    one is how the registry-normalised softmax got into the accept path in the first place.
    """
    result = classify(passport_view(), registry(), settings=SETTINGS)

    assert result.doctype_id == "passport"
    assert result.abstained is False
    assert result.confidence >= 0.5, "0.5 is the accept boundary; an accept cannot be below it"
    assert result.confidence > 0.6, "a checksum-verified MRZ should clear every gate widely"
    assert result.margin >= SETTINGS.classify_min_margin
    tiers = {e.tier for e in result.evidence}
    assert "checksum" in tiers and "anchor" in tiers


def test_checksum_evidence_goes_through_the_accept_rule_not_around_it():
    """A checksum-verified decisive identifier is evidence, not a bypass.

    It used to short-circuit: :func:`classify` returned before the lexical tier ran and before
    the support and coverage gates were evaluated, with a hard-coded confidence. That path is
    gone. The passport still classifies — the evidence is overwhelming — but it does so by
    clearing the same four gates as everything else, and the record shows the lexical tier's
    contribution rather than an explanation of why it was skipped.
    """
    result = classify(passport_view(), registry(), settings=SETTINGS)

    assert result.doctype_id == "passport"
    assert any(e.tier == "lexical" for e in result.evidence), (
        "the lexical tier must run: a gate that is not evaluated is not a gate"
    )
    assert not any("was not run" in e.detail for e in result.evidence)
    fusion = [e for e in result.evidence if e.tier == "fusion"]
    assert fusion, "every decision, including this one, carries the accept rule's own record"
    assert "route=" in fusion[0].detail
    assert result.coverage >= SETTINGS.classify_min_coverage
    assert result.margin >= 0.0


def test_mrz_shape_is_visible_to_the_structural_tier():
    features = structural_features(passport_view())

    assert features.has_mrz_shape is True
    assert features.page_count == 1
    assert features.landscape is True


def test_corrupted_mrz_does_not_short_circuit():
    """One flipped digit breaks the check digits, so L1 stops being decisive."""
    broken = MRZ_LINE_2[:5] + ("9" if MRZ_LINE_2[5] != "9" else "4") + MRZ_LINE_2[6:]
    view = view_of(
        [
            TextBlock(text="PASSPORT", zone=Zone.title),
            TextBlock(text=f"{MRZ_LINE_1}\n{broken}", zone=Zone.body),
        ]
    )
    outcome = anchor_scores(view, registry(), settings=SETTINGS)

    assert outcome.verified_doctypes() == ()
    assert outcome.checksums["passport"][0].verified is False


# ---------------------------------------------------------------------------
# (c) Shared generic terms are separated by coverage, not by term count
# ---------------------------------------------------------------------------
def bank_statement_view() -> LayoutView:
    """A bank statement that also carries every term a utility bill shares with it."""
    return view_of(
        [
            TextBlock(text="STATEMENT OF ACCOUNT", zone=Zone.title),
            TextBlock(text="ACCOUNT SUMMARY", zone=Zone.heading),
            TextBlock(text="Account Number: 0021 9948 7712", zone=Zone.body),
            TextBlock(text="Statement Date: 31 March 2026", zone=Zone.body),
            TextBlock(text="BEGINNING BALANCE 4,120.55", zone=Zone.body),
            TextBlock(text="CLOSING BALANCE 5,004.12", zone=Zone.body),
            TextBlock(text="IBAN GB29 NWBK 6016 1331 9268 19", zone=Zone.body),
            TextBlock(text="Page 1 of 3 - account statement", zone=Zone.furniture),
        ]
    )


def shared_vocabulary_view() -> LayoutView:
    """Only the words a bank statement and a utility bill have in common."""
    return view_of(
        [
            TextBlock(text="Account Number: 0021 9948 7712", zone=Zone.body),
            TextBlock(text="Statement Date: 31 March 2026", zone=Zone.body),
            TextBlock(text="Amount due on the account: 45.10", zone=Zone.body),
        ]
    )


def test_shared_generic_terms_alone_classify_as_nothing():
    """Overlap is not identity: matching both classes a little must accept neither."""
    specs = [bank_statement_spec(), utility_bill_spec()]
    lexical = lexical_scores(shared_vocabulary_view(), build_profiles(specs), settings=SETTINGS)

    assert lexical.matched["bank_statement"], "the shared vocabulary does fire"
    assert lexical.matched["utility_bill"]
    assert lexical.coverage["bank_statement"] < SETTINGS.classify_min_coverage
    assert lexical.coverage["utility_bill"] < SETTINGS.classify_min_coverage

    result = classify(shared_vocabulary_view(), specs, settings=SETTINGS)
    assert result.doctype_id == UNKNOWN
    assert result.abstained is True


def test_coverage_separates_doctypes_that_share_generic_terms():
    """The loser's score comes entirely from shared terms; coverage is what separates them.

    Scored against the whole fixture registry on purpose: with only two classes the log-odds
    estimator prunes their shared vocabulary outright (a term equally frequent in both is not
    discriminative for either), and the overlap this test is about would not exist.
    """
    profiles = build_profiles(registry())
    lexical = lexical_scores(bank_statement_view(), profiles, settings=SETTINGS)

    bank_terms = {term for term, _ in lexical.matched["bank_statement"]}
    utility_terms = {term for term, _ in lexical.matched["utility_bill"]}

    assert len(utility_terms) >= 3, "the shared vocabulary really does fire for both"
    assert utility_terms <= bank_terms, "the runner-up contributed nothing of its own"
    assert lexical.coverage["bank_statement"] > 3 * lexical.coverage["utility_bill"]
    assert lexical.coverage["utility_bill"] < SETTINGS.classify_min_coverage


def test_cascade_picks_the_class_with_coverage():
    result = classify(bank_statement_view(), registry(), settings=SETTINGS)

    assert result.doctype_id == "bank_statement"
    assert result.abstained is False
    assert result.coverage >= SETTINGS.classify_min_coverage
    assert dict(result.runners_up).get("utility_bill", 0.0) < result.confidence


# ---------------------------------------------------------------------------
# (d) Zone weighting
# ---------------------------------------------------------------------------
def test_the_same_term_outranks_itself_in_a_heavier_zone():
    """A title is evidence; a repeated footer is furniture. Same text, different worth."""
    specs = [bank_statement_spec(), utility_bill_spec()]
    profiles = build_profiles(specs)
    text = "STATEMENT OF ACCOUNT"

    as_title = lexical_scores(
        view_of([TextBlock(text=text, zone=Zone.title)]), profiles, settings=SETTINGS
    )
    as_furniture = lexical_scores(
        view_of([TextBlock(text=text, zone=Zone.furniture)]), profiles, settings=SETTINGS
    )

    assert as_title.raw["bank_statement"] > as_furniture.raw["bank_statement"]
    assert as_title.coverage["bank_statement"] == as_furniture.coverage["bank_statement"]


def test_anchor_in_the_title_scores_above_the_same_anchor_in_furniture():
    specs = [bank_statement_spec()]
    title_hit = anchor_scores(
        view_of([TextBlock(text="ACCOUNT SUMMARY", zone=Zone.title)]),
        specs,
        settings=SETTINGS,
    )
    furniture_hit = anchor_scores(
        view_of([TextBlock(text="ACCOUNT SUMMARY", zone=Zone.furniture)]),
        specs,
        settings=SETTINGS,
    )

    assert title_hit.scores["bank_statement"] > furniture_hit.scores["bank_statement"]


def test_zone_restricted_anchor_ignores_the_wrong_zone():
    spec = DocTypeSpec(
        doctype_id="form_x",
        label="Form X",
        country="US",
        anchors=[Anchor(text="FORM X-9 APPLICATION", zone=Zone.title, decisive=True)],
    )
    in_title = anchor_scores(
        view_of([TextBlock(text="FORM X-9 APPLICATION", zone=Zone.title)]),
        [spec],
        settings=SETTINGS,
    )
    in_body = anchor_scores(
        view_of([TextBlock(text="FORM X-9 APPLICATION", zone=Zone.body)]),
        [spec],
        settings=SETTINGS,
    )

    assert in_title.scores["form_x"] > 0.0
    assert in_body.scores["form_x"] == 0.0


# ---------------------------------------------------------------------------
# (e) Thin margins abstain, with a reason
# ---------------------------------------------------------------------------
def test_thin_margin_abstains_with_a_reason():
    """Two near-identical doctypes must produce UNKNOWN, not a coin flip."""
    twins = [
        DocTypeSpec(
            doctype_id=f"state_id_{state}",
            label=f"{state.upper()} Identification Card",
            country="US",
            category=Category.identity,
            anchors=[
                Anchor(text="IDENTIFICATION CARD", decisive=True),
                Anchor(text="DATE OF BIRTH"),
            ],
            fields=[
                FieldSpec(name="id_number", labels={"en": ["ID Number"]}),
                FieldSpec(name="date_of_birth", type="date", labels={"en": ["DOB"]}),
            ],
        )
        for state in ("ca", "ny")
    ]
    view = view_of(
        [
            TextBlock(text="IDENTIFICATION CARD", zone=Zone.title),
            TextBlock(text="DATE OF BIRTH 1974-08-12", zone=Zone.body),
        ]
    )
    result = classify(view, twins, settings=SETTINGS)

    assert result.doctype_id == UNKNOWN
    assert result.abstained is True
    assert "margin below floor" in result.reason
    assert result.runners_up and result.runners_up[0][0].startswith("state_id_")
    assert result.margin < SETTINGS.classify_min_margin


# ---------------------------------------------------------------------------
# (e2) Decisive-anchor collisions must never be resolved silently
# ---------------------------------------------------------------------------
def licence_pair(
    *, us_zone: Zone | None, ca_zone: Zone | None, declare_confusable: bool = True
) -> list[DocTypeSpec]:
    """The exact shape of the real US/CA driving-licence pair, with the gating parameterised.

    In the production registry the US doctype gated both of its decisive anchors to
    ``zone=title`` and the Canadian one gated neither. That asymmetry is the bug; these
    fixtures let each side be built either way.
    """
    us = DocTypeSpec(
        doctype_id="us_licence",
        label="US Driver License",
        country="US",
        category=Category.identity,
        anchors=[
            Anchor(text="DRIVER LICENSE", zone=us_zone, decisive=True),
            Anchor(text="DRIVER'S LICENSE", zone=us_zone, decisive=True),
            Anchor(text="Endorsements"),
            Anchor(text="Restrictions"),
            Anchor(text="CLASS"),
        ],
        confusable_with=(
            {"ca_licence": "the Canadian card spells it LICENCE and is bilingual"}
            if declare_confusable
            else {}
        ),
        fields=[FieldSpec(name="licence_number", labels={"en": ["DL No"]})],
    )
    ca = DocTypeSpec(
        doctype_id="ca_licence",
        label="Canadian Driver's Licence",
        country="CA",
        category=Category.identity,
        anchors=[
            Anchor(text="DRIVER'S LICENCE", zone=ca_zone, decisive=True),
            Anchor(text="PERMIS DE CONDUIRE", zone=ca_zone, lang="fr", decisive=True),
            Anchor(text="CLASS"),
        ],
        fields=[FieldSpec(name="licence_number", labels={"en": ["Licence No"]})],
    )
    return [us, ca]


def us_licence_sheet() -> LayoutView:
    """A US barcode-calibration sheet — which really does print both spellings.

    Every block is ``body`` because that is what a text-layer PDF produces: ``from_plain_text``
    labels everything ``body`` on purpose, since a guessed title would be amplified 3x. No
    adapter on that route can emit a ``title`` zone at all, so a title-gated decisive anchor is
    structurally inaudible there.
    """
    return view_of(
        [
            TextBlock(text="Standard Driver's License Card - Under 21", zone=Zone.body),
            TextBlock(text="AAMVA 2020 ANSI 636 Encoding Reference", zone=Zone.body),
            TextBlock(text="Endorsements: NONE", zone=Zone.body),
            TextBlock(text="Restrictions: B", zone=Zone.body),
            TextBlock(text="CLASS C  USA", zone=Zone.body),
            TextBlock(text="Standard Driver's Licence Card Under 21 - Encoding", zone=Zone.body),
        ]
    )


def test_two_doctypes_claiming_one_decisive_anchor_do_not_short_circuit():
    """A shared decisive anchor is a conflict, and a conflict is not a winner.

    The property the cascade has always claimed and must keep: when the count of doctypes
    holding a decisive hit is not exactly one, nobody is accepted at L1 and the lexical tier
    arbitrates. Pinned explicitly rather than left implicit in the thin-margin test, because it
    is the invariant a registry author relies on when they declare a genuinely shared header.
    """
    specs = licence_pair(us_zone=None, ca_zone=None)
    outcome = anchor_scores(us_licence_sheet(), specs, settings=SETTINGS)

    assert set(outcome.decisive_doctypes()) == {"us_licence", "ca_licence"}

    result = classify(us_licence_sheet(), specs, settings=SETTINGS)
    assert result.doctype_id != "ca_licence", "the wrong country was accepted on a tie"
    assert result.confidence != 0.90, "a decisive short-circuit fired on a contested claim"


def test_a_decisive_claim_muted_by_a_missing_zone_blocks_the_short_circuit():
    """The audibility guard, on the exact shape that produced the original wrong answer.

    The US doctype gates its decisive anchors to ``title``; the payload has no title zone; the
    Canadian doctype gates nothing. Before the guard, ``decisive_doctypes()`` came back as
    exactly ``('ca_licence',)`` and the cascade accepted a Canadian licence at 0.90 — never
    reading the anchor scores, which favoured the US doctype. Unmeasurable evidence is not
    evidence of absence.
    """
    specs = licence_pair(us_zone=Zone.title, ca_zone=None)
    view = us_licence_sheet()
    outcome = anchor_scores(view, specs, settings=SETTINGS)

    assert outcome.decisive_doctypes() == ("ca_licence",), (
        "fixture no longer reproduces the asymmetry this test is about"
    )
    assert "DRIVER'S LICENSE" in outcome.muted_decisive["us_licence"]

    result = classify(view, specs, settings=SETTINGS)
    assert result.doctype_id != "ca_licence"
    assert result.confidence != 0.90


def test_a_muted_claim_is_named_in_the_audit_trail():
    """A suppressed identification route must say so; a silent fallthrough is unreviewable."""
    specs = licence_pair(us_zone=Zone.title, ca_zone=None)
    result = classify(us_licence_sheet(), specs, settings=SETTINGS)

    details = " ".join(e.detail for e in result.evidence)
    assert "conclusive-evidence route was suppressed" in details
    assert "us_licence" in details


def test_a_zone_that_exists_without_the_anchor_is_still_evidence_of_absence():
    """The guard must not swallow a genuine negative.

    If the payload *has* the declared zone and the anchor is not in it, the registry's zone
    restriction did its job and the anchor genuinely did not match. Treating that as
    "unmeasurable" would disarm zone gating everywhere and abstain on everything.
    """
    specs = licence_pair(us_zone=Zone.title, ca_zone=None)
    view = view_of(
        [
            TextBlock(text="PERMIS DE CONDUIRE", zone=Zone.title),
            TextBlock(text="Standard Driver's Licence Card", zone=Zone.body),
            TextBlock(text="CLASS 5", zone=Zone.body),
        ]
    )
    outcome = anchor_scores(view, specs, settings=SETTINGS)

    assert "us_licence" not in outcome.muted_decisive, (
        "the title zone exists and does not contain the US anchor — that is a real negative"
    )
    result = classify(view, specs, settings=SETTINGS)
    assert result.doctype_id == "ca_licence"


def test_a_muted_claim_outside_the_confusable_cluster_does_not_block():
    """The guard is scoped to declared confusables, and must stay that way.

    Some doctype somewhere will always have a muted decisive anchor on some payload. If any
    such doctype could contest any other, the conclusive-L1 route would never fire and the
    cascade would lose the path that rescues photo IDs. Only a doctype the registry has
    declared confusable with the candidate is a contender — which is also why an
    asymmetrically gated cluster is a registry defect as well as a cascade one.

    The assertion is on the guard, not on the final doctype. Since the conclusive-L1 route
    stopped being an accept and became one way of satisfying one gate, this payload abstains
    on the *other* gates instead — which is the better answer for a calibration sheet that
    prints both spellings, and is not what this test is about.
    """
    specs = licence_pair(us_zone=Zone.title, ca_zone=None, declare_confusable=False)
    view = us_licence_sheet()

    assert "DRIVER'S LICENSE" in anchor_scores(
        view, specs, settings=SETTINGS
    ).muted_decisive["us_licence"]
    details = " ".join(e.detail for e in classify(view, specs, settings=SETTINGS).evidence)
    assert "conclusive-evidence route was suppressed" not in details, (
        "an undeclared confusable must not be able to contest the route"
    )


def test_a_symmetrically_gated_cluster_needs_no_guard_at_all():
    """The registry-side remedy, pinned so it is not undone later.

    When both sides of a confusable cluster gate their decisive anchors the same way, both
    claims are audible, the count comes to two, and the *existing* fallthrough handles it with
    no guard involved. Symmetric gating within a cluster is the property a registry lint should
    enforce; the runtime guard is the safety net for when it is violated.
    """
    outcome = anchor_scores(
        us_licence_sheet(), licence_pair(us_zone=None, ca_zone=None), settings=SETTINGS
    )

    assert outcome.muted_decisive == {}
    assert len(outcome.decisive_doctypes()) == 2


def test_empty_registry_abstains_rather_than_guessing():
    result = classify(view_of([TextBlock(text="PASSPORT", zone=Zone.title)]), [])

    assert result.doctype_id == UNKNOWN
    assert result.abstained is True
    assert "registry" in result.reason


def test_empty_document_abstains():
    result = classify(view_of([]), registry(), settings=SETTINGS)

    assert result.doctype_id == UNKNOWN
    assert result.abstained is True


# ---------------------------------------------------------------------------
# (f) The optional tier stays unimported
# ---------------------------------------------------------------------------
def test_bert_knn_is_never_imported_when_disabled():
    """Importing transformers costs seconds and a large dependency tree. Not for free."""
    sys.modules.pop("dce.classify.bert_knn", None)
    assert SETTINGS.bert_enabled is False

    classify(passport_view(), registry(), settings=SETTINGS)
    classify(bank_statement_view(), registry(), settings=SETTINGS)

    assert "dce.classify.bert_knn" not in sys.modules
    assert "transformers" not in sys.modules


# ---------------------------------------------------------------------------
# Merged PDFs
# ---------------------------------------------------------------------------
def test_classify_pages_run_length_aggregates_a_merged_bundle():
    """One upload, three documents: the bundle must not classify as one thing."""
    blocks = [
        TextBlock(text="PASSPORT", zone=Zone.title, page=1),
        TextBlock(text=f"{MRZ_LINE_1}\n{MRZ_LINE_2}", zone=Zone.body, page=1),
        TextBlock(text="STATEMENT OF ACCOUNT", zone=Zone.title, page=2),
        TextBlock(text="ACCOUNT SUMMARY", zone=Zone.heading, page=2),
        TextBlock(text="BEGINNING BALANCE 100.00", zone=Zone.body, page=2),
        TextBlock(text="CLOSING BALANCE 250.00", zone=Zone.body, page=2),
        TextBlock(text="IBAN GB29 NWBK 6016 1331 9268 19", zone=Zone.body, page=2),
        TextBlock(text="STATEMENT OF ACCOUNT", zone=Zone.title, page=3),
        TextBlock(text="ACCOUNT SUMMARY", zone=Zone.heading, page=3),
        TextBlock(text="BEGINNING BALANCE 250.00", zone=Zone.body, page=3),
        TextBlock(text="CLOSING BALANCE 275.00", zone=Zone.body, page=3),
        TextBlock(text="IBAN GB29 NWBK 6016 1331 9268 19", zone=Zone.body, page=3),
    ]
    view = LayoutView(
        doc_id="bundle",
        pages=[PageInfo(page=p, width=8.5, height=11.0, unit="inch") for p in (1, 2, 3)],
        blocks=blocks,
    )

    segments = classify_pages(view, registry(), settings=SETTINGS)

    assert [s.doctype_id for s in segments] == ["passport", "bank_statement"]
    assert segments[0].start_page == 1 and segments[0].end_page == 1
    assert segments[1].start_page == 2 and segments[1].end_page == 3
    assert segments[1].page_count == 2
    assert segments[1].classification.page_types == ["bank_statement", "bank_statement"]


# ---------------------------------------------------------------------------
# Structure, tables, key/values, accents
# ---------------------------------------------------------------------------
def test_structural_prior_penalises_an_identity_card_on_a_long_bundle():
    from dce.classify import structural_log_priors

    long_bundle = LayoutView(
        pages=[PageInfo(page=p, width=8.5, height=11.0) for p in range(1, 13)],
        blocks=[TextBlock(text="text", page=p) for p in range(1, 13)],
    )
    priors = structural_log_priors(structural_features(long_bundle), registry())

    assert priors["us_driver_license"] < 0
    assert priors["us_driver_license"] < priors["bank_statement"]


def test_accented_anchor_matches_accent_stripped_ocr():
    """Spanish anchors must survive an OCR engine that dropped every accent."""
    view = view_of(
        [
            TextBlock(text="CONSTANCIA DE SITUACION FISCAL", zone=Zone.title),
            TextBlock(text="REGISTRO FEDERAL DE CONTRIBUYENTES", zone=Zone.heading),
        ]
    )
    outcome = anchor_scores(view, [rfc_csf_spec()], settings=SETTINGS)

    matched = {hit.text for hit in outcome.hits["mx_rfc_csf"]}
    assert "CONSTANCIA DE SITUACIÓN FISCAL" in matched
    assert outcome.scores["mx_rfc_csf"] > 0.8


def test_devanagari_is_not_mangled():
    """Indic scripts keep their vowel marks — stripping them changes the word.

    A matra is a vowel, not an accent. Note this also pins the tokeniser: Python's ``\\w``
    excludes spacing combining marks, so a naive ``[^\\W_]+`` splits ``सरकार`` into fragments.
    """
    from dce.normalize import normalize

    normalized = normalize("भारत सरकार UNIQUE IDENTIFICATION")

    assert "भारत" in normalized.skeleton
    assert normalized.skeleton_tokens[:2] == ("भारत", "सरकार")


def test_deaccented_is_readable_and_skeleton_is_for_matching():
    """The two folded forms are different tools; peers should reach for the right one.

    ``deaccented`` is the human-readable accent fold. ``skeleton`` additionally collapses the
    OCR confusion classes (O/0, I/l/1, S/5, B/8), which is what makes a damaged scan match a
    clean anchor — and which makes it deliberately unreadable.
    """
    from dce.normalize import normalize

    normalized = normalize("MATRÍCULA CONSULAR DE ALTA SEGURIDAD")

    assert normalized.deaccented == "matricula consular de alta seguridad"
    assert normalized.skeleton == "matr1cu1a c0n5u1ar de a1ta 5egur1dad"
    assert normalized.folded == "matrícula consular de alta seguridad"


def test_ocr_damaged_header_still_matches_its_anchor():
    """The skeleton exists so a scan that read O as 0 and S as 5 still finds the doctype."""
    view = view_of([TextBlock(text="C0N5TANCIA DE SITUACION FI5CAL", zone=Zone.title)])
    outcome = anchor_scores(view, [rfc_csf_spec()], settings=SETTINGS)

    assert outcome.scores["mx_rfc_csf"] > 0.0
    assert any(hit.decisive for hit in outcome.hits["mx_rfc_csf"])


def test_table_and_key_value_text_is_scored():
    """Structure the provider gave us must not be invisible to the lexical tier."""
    specs = [bank_statement_spec(), utility_bill_spec()]
    profiles = build_profiles(specs)
    view = view_of(
        [TextBlock(text="Monthly document", zone=Zone.body)],
        tables=[
            Table(
                table_id="t1",
                row_count=2,
                col_count=2,
                cells=[
                    Cell(row=0, col=0, text="CLOSING BALANCE", is_header=True),
                    Cell(row=0, col=1, text="5,004.12"),
                    Cell(row=1, col=0, text="BEGINNING BALANCE", is_header=True),
                    Cell(row=1, col=1, text="4,120.55"),
                ],
            )
        ],
        key_values=[KeyValue(key="Account Number", value="0021 9948 7712")],
    )
    lexical = lexical_scores(view, profiles, settings=SETTINGS)

    assert lexical.raw["bank_statement"] > 0.0
    assert lexical.coverage["bank_statement"] > lexical.coverage["utility_bill"]


def test_unverified_pattern_hit_is_not_decisive():
    """Nine digits are not evidence; nine digits that satisfy Luhn are."""
    view = view_of(
        [
            TextBlock(text="SOCIAL INSURANCE NUMBER", zone=Zone.title),
            TextBlock(text="123-456-789", zone=Zone.body),
        ]
    )
    outcome = anchor_scores(view, [ca_sin_spec()], settings=SETTINGS)

    assert outcome.checksums["ca_sin"][0].verified is False
    assert outcome.verified_doctypes() == ()


def test_valid_sin_is_checksum_verified():
    view = view_of(
        [
            TextBlock(text="SOCIAL INSURANCE NUMBER", zone=Zone.title),
            TextBlock(text="130-692-544", zone=Zone.body),
        ]
    )
    outcome = anchor_scores(view, [ca_sin_spec()], settings=SETTINGS)

    hit = outcome.checksums["ca_sin"][0]
    assert hit.verified is True
    assert hit.level == "checksum"
    assert hit.validator == "sin_luhn"


def test_structurally_valid_but_unchecksummed_id_is_not_proof():
    """An EIN has no check digit. A well-shaped EIN is a shape, not a verdict."""
    view = view_of(
        [
            TextBlock(text="EMPLOYER IDENTIFICATION NUMBER", zone=Zone.title),
            TextBlock(text="12-3456789", zone=Zone.body),
        ]
    )
    outcome = anchor_scores(view, [ein_letter_spec()], settings=SETTINGS)

    hit = outcome.checksums["us_ein_letter"][0]
    assert hit.level == "format"
    assert hit.verified is False
    assert outcome.verified_doctypes() == ()


def test_evidence_masks_identifiers():
    """This is a KYC audit trail, not a data dump."""
    view = view_of(
        [
            TextBlock(text="SOCIAL INSURANCE NUMBER", zone=Zone.title),
            TextBlock(text="130-692-544", zone=Zone.body),
        ]
    )
    outcome = anchor_scores(view, [ca_sin_spec()], settings=SETTINGS)

    checksum_evidence = [e for e in outcome.evidence["ca_sin"] if e.tier == "checksum"]
    assert checksum_evidence
    assert "130-692-544" not in checksum_evidence[0].detail
    assert "-544" in checksum_evidence[0].detail


# ---------------------------------------------------------------------------
# (k) One accept rule: coherence of the numbers it reports
#
# The cascade used to have two-and-a-half accept paths — a checksum short-circuit and a
# decisive-anchor short-circuit, each returning before the accept rule ran and each with its
# own hard-coded confidence. Three incoherences followed, and each one gets a test here so
# that reintroducing a parallel path fails loudly rather than quietly.
# ---------------------------------------------------------------------------
def _every_verdict_in(specs: list[DocTypeSpec], views: list[LayoutView]) -> list:
    return [classify(view, specs, settings=SETTINGS) for view in views]


def sample_views() -> list[LayoutView]:
    """A spread of payloads: strong evidence, thin evidence, contested, and none at all."""
    return [
        passport_view(),
        bank_statement_view(),
        shared_vocabulary_view(),
        view_of([TextBlock(text="PASSPORT", zone=Zone.title)]),
        view_of([TextBlock(text="Chapter four: the harvest was late.", zone=Zone.body)]),
        view_of(
            [
                TextBlock(text="SOCIAL INSURANCE NUMBER", zone=Zone.title),
                TextBlock(text="130-692-544", zone=Zone.body),
            ]
        ),
        view_of([TextBlock(text="EMPLOYER IDENTIFICATION NUMBER", zone=Zone.title)]),
    ]


def test_an_accepted_classification_never_reports_a_negative_margin():
    """The margin is one subtraction on one scale, so this is true by construction.

    It was not true before. ``_accept_short_circuit`` computed ``margin = score - max(other
    anchor scores)`` where ``score`` was the hard-coded constant 0.90 and the runner-up came
    off the squashed anchor channel: two different scales, subtracted. Two corpus documents
    were accepted reporting a negative margin, which says the winner lost.
    """
    for result in _every_verdict_in(registry(), sample_views()):
        if result.abstained:
            continue
        assert result.margin >= 0.0, (
            f"{result.doctype_id} was accepted while reporting margin {result.margin}"
        )
        assert result.margin >= SETTINGS.classify_min_margin


def test_confidence_orders_acceptance_above_abstention():
    """0.5 is the decision boundary, on both sides of it, for every document.

    Accepts and abstentions used to overlap: an abstention at 0.494 outranked an acceptance at
    0.409, because the short-circuit's confidence was a hard-coded constant on a different
    scale from the accept rule's. A headline number that does not order the two outcomes it
    exists to distinguish cannot be read by a reviewer or thresholded by a caller.
    """
    results = _every_verdict_in(registry(), sample_views())
    accepted = [r.confidence for r in results if not r.abstained]
    abstained = [r.confidence for r in results if r.abstained]

    assert accepted and abstained, "the fixture must exercise both outcomes"
    assert min(accepted) >= 0.5
    assert max(abstained) < 0.5
    assert min(accepted) > max(abstained)


def test_every_accepted_answer_clears_every_configured_floor():
    """No accept path may skip a gate. This is the whole of DEFECT S in one assertion.

    Five corpus documents used to be accepted below ``classify_min_coverage`` because the
    short-circuit returned before that floor was read.
    """
    for result in _every_verdict_in(registry(), sample_views()):
        if result.abstained:
            continue
        assert result.margin >= SETTINGS.classify_min_margin
        assert result.coverage >= SETTINGS.classify_min_coverage


def test_support_floor_is_the_only_gate_that_can_refuse_thin_evidence():
    """``classify_min_support`` is redundant on the corpus; here is what it is not redundant to.

    A doctype that declares exactly ONE anchor has coverage 1.0 the moment that anchor matches
    — coverage is a fraction of the doctype's declared anchors, so no coverage floor can ever
    refuse a one-anchor doctype. And if nothing else in the registry scores, its lead over a
    field of zeros clears any margin floor. Both of the other gates are structurally blind to
    this, which is a registry-authoring accident rather than an exotic one.

    The evidence here is a single low-specificity anchor found in page furniture: about a fifth
    of a bit. The support floor is what refuses it, and it refuses it alone.
    """
    thin = DocTypeSpec(
        doctype_id="footer_only",
        label="Footer Only",
        country="US",
        anchors=[Anchor(text="NOTICE", zone=Zone.furniture)],
        fields=[FieldSpec(name="a", labels={"en": ["A"]})],
    )
    unrelated = DocTypeSpec(
        doctype_id="unrelated",
        label="Unrelated",
        country="US",
        anchors=[Anchor(text="COMPLETELY DIFFERENT HEADER")],
        fields=[FieldSpec(name="b", labels={"en": ["B"]})],
    )
    view = view_of(
        [
            TextBlock(text="Dear customer, your appointment has moved.", zone=Zone.body),
            TextBlock(text="NOTICE", zone=Zone.furniture),
        ]
    )

    without = classify(view, [thin, unrelated], settings=Settings(
        _env_file=None, classify_min_support=0.0
    ))
    assert without.doctype_id == "footer_only", "the other two gates cannot see this"
    assert without.coverage == 1.0
    assert without.margin >= SETTINGS.classify_min_margin

    withit = classify(view, [thin, unrelated], settings=SETTINGS)
    assert withit.doctype_id == UNKNOWN
    assert "support below floor" in withit.reason
    assert "margin below floor" not in withit.reason
    assert "coverage below floor" not in withit.reason


def test_an_unlabelled_luhn_valid_number_is_not_strong_evidence():
    """Ten per cent of nine-digit strings pass Luhn. That is a fact about numbers.

    The anchor channel used to top out at 0.875 for a doctype on the strength of a bare
    nine-digit Luhn-valid number with nothing around it — no label, and no anchor of that
    doctype matched anywhere on the page. A page of unrelated prose that happens to carry an
    invoice reference could therefore outrank the document's real class on L1.

    A checksum is evidence about a *number*. It becomes evidence about a *document* when the
    document says what the number is.
    """
    # Purpose-built so the two corroboration mechanisms are separable: the field's declared
    # label ("Social Insurance Number") is NOT one of the doctype's anchors, so a document
    # that labels the number without carrying the issuer's header exercises the label path
    # alone, and vice versa.
    spec = DocTypeSpec(
        doctype_id="ca_sin_letter",
        label="SIN Confirmation Letter",
        country="CA",
        category=Category.identity,
        anchors=[Anchor(text="CONFIRMATION OF SOCIAL INSURANCE NUMBER", decisive=True)],
        id_patterns=[r"\b\d{3}-\d{3}-\d{3}\b"],
        fields=[
            FieldSpec(
                name="sin",
                type="id",
                validator="sin_luhn",
                labels={"en": ["Social Insurance Number"]},
            )
        ],
    )
    bare = view_of(
        [
            TextBlock(text="Reference for your records: 130-692-544", zone=Zone.body),
            TextBlock(text="Please quote it when you call.", zone=Zone.body),
        ]
    )
    labelled = view_of(
        [
            TextBlock(text="Social Insurance Number: 130-692-544", zone=Zone.body),
            TextBlock(text="Please quote it when you call.", zone=Zone.body),
        ]
    )
    specs = [spec]

    bare_out = anchor_scores(bare, specs, settings=SETTINGS)
    labelled_out = anchor_scores(labelled, specs, settings=SETTINGS)

    bare_hit = bare_out.checksums["ca_sin_letter"][0]
    labelled_hit = labelled_out.checksums["ca_sin_letter"][0]
    assert bare_hit.verified is True, "the check digit really does pass in both"
    assert labelled_hit.verified is True
    assert bare_hit.corroborated is False
    assert labelled_hit.corroborated is True

    assert bare_out.scores["ca_sin_letter"] < labelled_out.scores["ca_sin_letter"]
    assert bare_out.scores["ca_sin_letter"] < 0.5, (
        "an unlabelled identifier must not top out the anchor channel"
    )
    assert bare_out.verified_doctypes() == (), (
        "and it must not be able to satisfy the conclusive-L1 identification route"
    )
    assert classify(bare, specs, settings=SETTINGS).doctype_id == UNKNOWN


def test_an_anchor_match_corroborates_an_identifier_the_document_did_not_label():
    """The other half of the rule, so it is not read as "labels or nothing".

    OCR loses labels. A document that carries the issuer's own header has already said what it
    is, and the identifier on it is then corroborated whether or not its label survived.
    """
    view = view_of(
        [
            TextBlock(text="SOCIAL INSURANCE NUMBER", zone=Zone.title),
            TextBlock(text="Ligne francaise perdue par l'OCR", zone=Zone.body),
            TextBlock(text="130-692-544", zone=Zone.body),
        ]
    )
    outcome = anchor_scores(view, [ca_sin_spec()], settings=SETTINGS)

    assert outcome.checksums["ca_sin"][0].corroborated is False, "no label on that line"
    assert outcome.verified_doctypes() == ("ca_sin",), (
        "but the doctype's own anchor matched, so the document identified itself"
    )


# ---------------------------------------------------------------------------
# (i) MONOTONICITY — the accept rule may not punish stronger evidence
# ---------------------------------------------------------------------------
# Round 3, regression 1. Measured, before the fix, on the reference corpus through the
# container: the SAME 59 documents scored 36 correct with every block labelled `body` and 32
# correct with inferred title/heading zones. Zone labels are strictly MORE information and
# production always has them (Azure DI -> ROLE_ZONES in dce.adapters), so the configuration
# production can never run was the better one and the 36 was a number production would never
# see. All five documents it cost were refused by the margin gate with the winner's support at
# 0.98 — the diagnosis in one number: the gate was a DIFFERENCE of two quantities bounded above
# by 1 and clipped at 0.97, so pushing the winner and its rival up the saturating curve
# together squeezed the measured separation toward zero. More evidence, less separation.
#
# The rule now compares in bits (evidence_bits / separation_of) and decides on the zone-free
# reading, so both halves are structural. These tests pin both halves.


def _promote(view: LayoutView, text: str, zone: Zone) -> LayoutView:
    """The same payload with the block whose text is ``text`` relabelled ``zone``."""
    promoted = view.model_copy(deep=True)
    for block in promoted.blocks:
        if block.text == text:
            block.zone = zone
    return promoted


#: The masthead both sibling forms print, as one line — which is how a layout provider sees it
#: and therefore how a single ``title`` role lands on it.
MASTHEAD = "Department of the Treasury  Internal Revenue Service  OMB No. 1545-0074"


def sibling_form_pair() -> list[DocTypeSpec]:
    """Two form types that share their masthead, which is the shape the defect needed.

    This is the ordinary case, not a contrived one: ``us_w2`` and ``us_1040`` share "Department
    of the Treasury - Internal Revenue Service"; ``mx_cif`` and ``mx_rfc_csf`` share "Registro
    Federal de Contribuyentes"; ``in_aoa`` and ``in_moa`` share the Companies Act header. A
    document of type A therefore carries evidence for B, and promoting the shared masthead into
    the title zone strengthens **both**.

    That is what the previous accept rule could not survive. It tested ``S[1] - S[2] >= 0.04``
    where ``S = 1 - (1 - anchor)(1 - explained)`` is bounded above by 1 — so once the
    RUNNER-UP's support passes 0.96 the gate is unsatisfiable *at any level of evidence for the
    winner*, because there is less than 0.04 of headroom left between ``S[2]`` and the bound.
    The anchor tier clips at 0.97, i.e. 5.06 bits, and a title multiplies anchor bits by 2.0,
    so a shared masthead in a title is exactly the thing that pushes a runner-up over that line.

    Verified to be a real regression guard rather than a test that would have passed anyway:
    replayed through the PREVIOUS accept rule (same tiers, same evidence, only the rule
    differing) this fixture gives ``form_wages`` accepted at confidence 0.618 with every block
    read as ``body``, and ``unknown`` — an abstention — with the masthead promoted to
    ``heading`` (0.315) or to ``title`` (0.312). Strictly more information, no answer at all.
    That is the corpus regression, 36 correct down to 32, in one fixture.
    """
    shared = [
        Anchor(text="Department of the Treasury"),
        Anchor(text="Internal Revenue Service"),
        Anchor(text="OMB No. 1545-0074"),
    ]
    return [
        DocTypeSpec(
            doctype_id="form_wages",
            label="Wage and Tax Statement",
            country="US",
            anchors=[
                *shared,
                Anchor(text="Wage and Tax Statement", decisive=True),
                Anchor(text="Wages, tips, other compensation"),
                Anchor(text="Social security wages"),
            ],
            fields=[FieldSpec(name="wages", labels={"en": ["Wages"]})],
        ),
        DocTypeSpec(
            doctype_id="form_return",
            label="Individual Income Tax Return",
            country="US",
            anchors=[
                *shared,
                Anchor(text="U.S. Individual Income Tax Return", decisive=True),
                Anchor(text="Filing Status"),
            ],
            fields=[FieldSpec(name="agi", labels={"en": ["Adjusted gross income"]})],
        ),
    ]


def _monotonicity_cases() -> list[tuple[str, LayoutView, str, str, list[DocTypeSpec]]]:
    """``(name, all-body view, the document's own header line, expected doctype, registry)``.

    Deliberately a spread rather than one specimen: a photo ID carried by a decisive anchor, a
    letter carried by an anchor plus a checksum, a chatty document carried by the lexical tier,
    and — the one that actually reproduces the defect — a form whose masthead it shares with a
    sibling form, so promoting that masthead strengthens the runner-up too.
    """
    return [
        (
            "shared masthead, runner-up strengthened with the winner",
            view_of(
                [
                    TextBlock(text=MASTHEAD, zone=Zone.body),
                    TextBlock(text="Wage and Tax Statement", zone=Zone.body),
                    TextBlock(text="Wages, tips, other compensation", zone=Zone.body),
                    TextBlock(text="Social security wages", zone=Zone.body),
                    TextBlock(text="Filing Status", zone=Zone.body),
                ]
            ),
            MASTHEAD,
            "form_wages",
            sibling_form_pair(),
        ),
        (
            "decisive-anchor identity document",
            view_of(
                [
                    TextBlock(text="PASSPORT", zone=Zone.body),
                    TextBlock(text="Type/Type   Code/Code", zone=Zone.body),
                    TextBlock(text="Authority", zone=Zone.body),
                    TextBlock(text=MRZ_LINE_1, zone=Zone.body),
                    TextBlock(text=MRZ_LINE_2, zone=Zone.body),
                ]
            ),
            "PASSPORT",
            "passport",
            registry(),
        ),
        (
            "anchor plus corroborated checksum",
            view_of(
                [
                    TextBlock(text="SOCIAL INSURANCE NUMBER", zone=Zone.body),
                    TextBlock(text="Service Canada", zone=Zone.body),
                    TextBlock(text="SIN: 130-692-544", zone=Zone.body),
                ]
            ),
            "SOCIAL INSURANCE NUMBER",
            "ca_sin",
            registry(),
        ),
        (
            "lexical-tier document",
            view_of(
                [
                    TextBlock(text="STATEMENT OF ACCOUNT", zone=Zone.body),
                    TextBlock(text="ACCOUNT SUMMARY", zone=Zone.body),
                    TextBlock(text="Account Number: 0021 9948 7712", zone=Zone.body),
                    TextBlock(text="Statement Date: 31 March 2026", zone=Zone.body),
                    TextBlock(text="BEGINNING BALANCE 4,120.55", zone=Zone.body),
                    TextBlock(text="CLOSING BALANCE 5,004.12", zone=Zone.body),
                    TextBlock(text="IBAN GB29 NWBK 6016 1331 9268 19", zone=Zone.body),
                ]
            ),
            "STATEMENT OF ACCOUNT",
            "bank_statement",
            registry(),
        ),
    ]


def test_promoting_the_true_title_line_never_degrades_the_verdict():
    """THE round-3 property. Stronger evidence for the true doctype may not cost an accept.

    For each case the document's own title line is moved from ``body`` into ``title`` and then
    into ``heading`` — strictly more evidence for the doctype that is already right, and
    nothing else changed. Three things must hold, in order of how badly they fail if they do:

    1. an accepted answer may not become an abstention;
    2. an accepted answer may not become a *different* answer;
    3. the reported confidence may not fall.

    (3) is the one that would have caught the defect early. Before the fix, the confidence of
    an accepted answer fell as its evidence grew, because the margin factor of the confidence
    is ``lead / (lead + floor)`` and ``lead`` was a difference of saturating quantities.
    """
    for name, body_view, title_line, expected, specs in _monotonicity_cases():
        base = classify(body_view, specs, settings=SETTINGS)
        assert base.doctype_id == expected and not base.abstained, (
            f"{name}: the fixture must accept before it can be tested for monotonicity"
        )
        for zone in (Zone.heading, Zone.title):
            stronger = classify(
                _promote(body_view, title_line, zone), specs, settings=SETTINGS
            )
            assert not stronger.abstained, (
                f"{name}: promoting {title_line!r} to {zone.value} turned an accept into an "
                f"abstention — {stronger.reason}"
            )
            assert stronger.doctype_id == expected, (
                f"{name}: promoting {title_line!r} to {zone.value} changed the answer to "
                f"{stronger.doctype_id!r}"
            )
            assert stronger.confidence >= base.confidence - 1e-9, (
                f"{name}: promoting {title_line!r} to {zone.value} LOWERED confidence, "
                f"{base.confidence} -> {stronger.confidence}"
            )


def test_the_separation_measure_is_strictly_increasing_in_the_evidence_lead():
    """The algebra the property rests on, pinned directly rather than only through documents.

    ``separation_of`` must be strictly increasing on a positive lead and must not saturate the
    way ``S[1] - S[2]`` did. The second assertion is the specific failure: scale both
    candidates' evidence by a common zone multiplier and the OLD quantity shrinks while the new
    one grows.
    """
    from dce.classify.cascade import evidence_bits, separation_of

    leads = [0.0, 0.05, 0.25, 1.0, 4.0, 12.0]
    seps = [separation_of(x) for x in leads]
    assert seps == sorted(seps)
    assert all(b > a for a, b in pairwise(seps))
    assert seps[0] >= 0.0 and seps[-1] < 1.0

    # Winner at 3 bits, rival at 2. Apply the title zone's 2.0x anchor multiplier to both.
    winner, rival = 3.0, 2.0
    boost = 2.0
    old_before = (1 - 2**-winner) - (1 - 2**-rival)
    old_after = (1 - 2 ** -(winner * boost)) - (1 - 2 ** -(rival * boost))
    assert old_after < old_before, "the OLD measure shrinks when both sides get stronger"
    assert separation_of(winner * boost - rival * boost) > separation_of(winner - rival), (
        "the new one must grow"
    )

    # And the noisy-OR really is additive in bits, which is what makes the above legitimate.
    a_bits, explained = 2.5, 0.4
    noisy_or = 1.0 - (1.0 - (1.0 - 2.0**-a_bits)) * (1.0 - explained)
    assert abs(evidence_bits(a_bits, explained) - -math.log2(1.0 - noisy_or)) < 1e-9


def test_one_mislabelled_line_cannot_change_the_verdict():
    """Azure DI calls captions, watermarks and marketing copy ``title``. Routinely.

    A title is worth 3.0x in the lexical tier, 2.0x in the anchor tier, and it is the only zone
    in which 21 of the registry's decisive anchors are audible at all — so a single bad label
    is worth up to four bits of evidence for a document type the page has nothing to do with.

    Measured through the container on eight documents the service classified correctly, by
    appending ONE line carrying another doctype's decisive anchor, once labelled ``title`` and
    once labelled ``body`` — identical text, identical position, only the label differing:

        previous accept rule    23 confident wrong answers (title) vs  3 (body)   7.67x
        this accept rule         5 confident wrong answers (title) vs  5 (body)   1.00x

    The label now buys nothing, which is the guarantee: the two comparisons the rule makes —
    who leads, and by how much — are evaluated on the zone-free reading of the payload. What a
    zone label can still do is *add* (a zone-restricted anchor can establish the conclusive-L1
    route, and zone weighting still feeds ``support``), so labels can raise a verdict and
    cannot lower one.
    """
    honest = view_of(
        [
            TextBlock(text="MONTHLY STATEMENT", zone=Zone.heading),
            TextBlock(text="Account Number 0012 3456", zone=Zone.body),
            TextBlock(text="Opening balance 1,204.55", zone=Zone.body),
            TextBlock(text="Closing balance 998.10", zone=Zone.body),
            TextBlock(text="Statement period 01 Mar to 31 Mar", zone=Zone.body),
        ]
    )
    for lie in ("PASSPORT", "SOCIAL INSURANCE NUMBER", "EMPLOYER IDENTIFICATION NUMBER"):
        as_body = classify(
            view_of([*honest.blocks, TextBlock(text=lie, zone=Zone.body)]),
            registry(),
            settings=SETTINGS,
        )
        as_title = classify(
            view_of([*honest.blocks, TextBlock(text=lie, zone=Zone.title)]),
            registry(),
            settings=SETTINGS,
        )
        assert as_title.doctype_id == as_body.doctype_id, (
            f"the label alone changed the answer for {lie!r}: "
            f"body -> {as_body.doctype_id}, title -> {as_title.doctype_id}"
        )
        assert as_title.abstained == as_body.abstained


# ---------------------------------------------------------------------------
# (h) Gate 1 asks the two tiers about the doctype the accept is made OVER
# ---------------------------------------------------------------------------
#: A page whose masthead and body vocabulary the three specs below carve up deliberately.
_RECONCILIATION_PAGE = (
    "ANNUAL WITHHOLDING RECONCILIATION STATEMENT",
    "reconciliation period ending December",
    "Employer identification number",
    "Signature of authorised officer",
    "Notarised on the premises today",
    "Wharfage and demurrage schedule",
)


def _reconciliation_registry(*, rival_shares_the_page_vocabulary: bool) -> list[DocTypeSpec]:
    """Three doctypes: the real one, its nearest rival, and a lexically loud bystander.

    ``target`` owns the masthead, so it leads the anchor channel and the combined channel.
    ``rival`` shares one anchor, so it is the runner-up. ``chatter`` matches no anchor at all —
    its declared vocabulary is three short phrases that all happen to appear on this page, and
    ``explained`` is a *fraction of the class's own mass*, so a small profile that is fully
    exhibited scores high. ``chatter`` therefore tops the lexical channel while sitting last on
    the combined one, which is precisely the position from which it cannot be the answer.

    ``rival_shares_the_page_vocabulary`` moves ``rival``'s profile onto the page so that the
    lexical channel prefers *it* instead — the case where a tier really is contradicting the
    verdict.
    """
    target = DocTypeSpec(
        doctype_id="target", label="Target Statement", country="US", category=Category.tax,
        anchors=[Anchor(text="ANNUAL WITHHOLDING RECONCILIATION STATEMENT"),
                 Anchor(text="reconciliation period ending December")],
        fields=[FieldSpec(name="a", labels={"en": ["Employer identification number"]}),
                FieldSpec(name="a2", labels={"en": ["Quarterly deposit schedule"]}),
                FieldSpec(name="a3", labels={"en": ["Third party designee election"]})],
    )
    rival_fields = [FieldSpec(name="b", labels={"en": ["Employer identification number"]})]
    if not rival_shares_the_page_vocabulary:
        rival_fields += [
            FieldSpec(name="b8", labels={"en": ["Successor employer indicator"]}),
            FieldSpec(name="b9", labels={"en": ["Seasonal filer exemption"]}),
        ]
    rival = DocTypeSpec(
        doctype_id="rival", label="Rival Statement", country="US", category=Category.tax,
        anchors=[Anchor(text="reconciliation period ending December")], fields=rival_fields,
    )
    chatter = DocTypeSpec(
        doctype_id="chatter", label="Chatty Doctype", country="US", category=Category.other,
        anchors=[Anchor(text="A STRING THAT IS NOWHERE ON THE PAGE")],
        fields=[FieldSpec(name="c", labels={"en": ["Signature of authorised officer"]}),
                FieldSpec(name="d", labels={"en": ["Notarised on the premises today"]}),
                FieldSpec(name="e", labels={"en": ["Wharfage and demurrage schedule"]})],
    )
    return [target, rival, chatter]


def _reconciliation_view() -> LayoutView:
    return view_of([TextBlock(text=t, zone=Zone.body) for t in _RECONCILIATION_PAGE])


def test_a_tier_topped_by_a_doctype_that_cannot_be_accepted_does_not_veto():
    """Gate 1's opponent is the runner-up, not whatever tops a channel.

    Gate 2 compares the candidate against exactly one doctype — ``B[2]``, the doctype the
    accept is being made *over*. The predecessor of this gate compared argmaxes over the whole
    registry, so the two gates asked about different opponents, and the lexical channel's
    argmax is frequently a doctype that carries no anchor evidence, sits last on the combined
    channel and could not be accepted under any circumstances. It could not be the answer and
    it vetoed the answer anyway.

    Here ``chatter`` is that doctype: zero anchor bits, bottom of the combined channel, top of
    the lexical one. Both tiers prefer ``target`` to ``rival`` — the comparison the accept
    actually makes — so the accept stands.
    """
    specs = _reconciliation_registry(rival_shares_the_page_vocabulary=False)
    view = _reconciliation_view()
    profiles = build_profiles(specs)
    anchor = anchor_scores(view, specs, settings=SETTINGS)
    lexical = lexical_scores(view, profiles, settings=SETTINGS)

    # The fixture is only meaningful if it really is in the position described.
    assert anchor.bits["chatter"] == 0.0
    assert lexical.explained["chatter"] > lexical.explained["target"], "chatter tops lexical"
    assert anchor.bits["target"] > anchor.bits["rival"]
    assert lexical.explained["target"] > lexical.explained["rival"]

    result = classify(view, specs, settings=SETTINGS, profiles=profiles)
    assert result.doctype_id == "target", result.reason
    assert not result.abstained
    assert result.confidence >= 0.5


def test_a_tier_that_prefers_the_runner_up_still_refuses():
    """The half of gate 1 that buys the precision, and it is unchanged.

    Widening gate 1's opponent from "the whole registry" to "the runner-up" must not widen it
    to "nothing". A tier that positively prefers the doctype the accept would be made over is
    contradicting the verdict, and it still refuses — which is what keeps
    ``corpus/us/us_1099.pdf`` (anchors 7.20 bits for ``us_w9`` against 4.90 for the true
    ``us_1099``, lexical channel preferring ``us_1099``) an abstention rather than a compliance
    incident.
    """
    specs = _reconciliation_registry(rival_shares_the_page_vocabulary=True)
    view = _reconciliation_view()
    profiles = build_profiles(specs)
    lexical = lexical_scores(view, profiles, settings=SETTINGS)
    assert lexical.explained["rival"] > lexical.explained["target"]

    result = classify(view, specs, settings=SETTINGS, profiles=profiles)
    assert result.doctype_id == UNKNOWN
    assert "does not support 'target' over 'rival'" in result.reason
    assert result.confidence == 0.0


def _sibling_masthead_registry(real: str, other: str) -> list[DocTypeSpec]:
    """Two doctypes printing one masthead; only ``real``'s body vocabulary is on the page.

    The anchor channel ties exactly. The lexical channel separates them by a wide margin.
    """
    return [
        DocTypeSpec(
            doctype_id=real, label="Real", country="US", category=Category.tax,
            anchors=[Anchor(text="SHARED MASTHEAD OF TWO SIBLING FORMS")],
            fields=[FieldSpec(name="a", labels={"en": ["Employer identification number"]}),
                    FieldSpec(name="b", labels={"en": ["Quarterly deposit schedule"]}),
                    FieldSpec(name="c", labels={"en": ["Third party designee election"]})],
        ),
        DocTypeSpec(
            doctype_id=other, label="Other", country="US", category=Category.tax,
            anchors=[Anchor(text="SHARED MASTHEAD OF TWO SIBLING FORMS")],
            fields=[FieldSpec(name="d", labels={"en": ["Employer identification number"]}),
                    FieldSpec(name="e", labels={"en": ["Vessel tonnage certificate"]}),
                    FieldSpec(name="f", labels={"en": ["Bunker fuel surcharge"]})],
        ),
    ]


def test_identification_does_not_turn_on_how_a_doctype_id_is_spelled():
    """A tie is not a dissent, and it must not be resolved by the alphabet.

    ``_ranked_channel`` breaks ties by doctype id so that the audit record is reproducible
    across runs — a deliberate choice, and the right one for a *record*. Reading that tie-break
    as a channel's *opinion* is not. Under the argmax form of this gate it was read as one:
    when two doctypes printed the same masthead and tied on the anchor channel, the
    alphabetically-first id was named "the anchor winner", and a candidate that lost the
    coin-toss failed identification however decisively the lexical channel preferred it.

    The consequence is this test: two registries identical in every number — same anchors, same
    profiles, same document, same evidence, verified below — and differing only in whether the
    true doctype's id sorts before or after its sibling's. The argmax form accepted the first
    and abstained on the second. A rule that reads a tie as "this tier expressed no preference"
    answers both the same way, which is the only defensible thing a tie can mean.

    This is what recovers ``corpus/ca/ca_bn_letter.pdf`` on plain text: it ties ``in_form16``
    on the anchor channel at 2.200 bits and was accepted purely because ``c`` sorts before
    ``i``.
    """
    verdicts = {}
    for real, other in (("aaa_form", "zzz_form"), ("zzz_form", "aaa_form")):
        specs = _sibling_masthead_registry(real, other)
        view = view_of([
            TextBlock(text="SHARED MASTHEAD OF TWO SIBLING FORMS", zone=Zone.body),
            TextBlock(text="Employer identification number", zone=Zone.body),
            TextBlock(text="Quarterly deposit schedule", zone=Zone.body),
            TextBlock(text="Third party designee election", zone=Zone.body),
        ])
        profiles = build_profiles(specs)
        anchor = anchor_scores(view, specs, settings=SETTINGS)
        lexical = lexical_scores(view, profiles, settings=SETTINGS)
        assert anchor.bits[real] == anchor.bits[other], "the anchor channel must tie exactly"
        assert lexical.explained[real] > lexical.explained[other]
        result = classify(view, specs, settings=SETTINGS, profiles=profiles)
        verdicts[real] = result.doctype_id

    assert verdicts == {"aaa_form": "aaa_form", "zzz_form": "zzz_form"}, (
        "the same evidence produced different verdicts depending on the doctype id's "
        f"spelling: {verdicts}"
    )


def test_a_tier_with_no_evidence_for_the_candidate_cannot_concur():
    """The silent-tier guard, stated per-candidate rather than per-registry.

    A tier that scores the candidate at zero has nothing to say about it, and "does not prefer
    the runner-up" is then vacuously true. Two indifferent tiers must not add up to
    concurrence — that is the "least-wrong of a tiny registry" failure the whole design exists
    to refuse, and the per-registry form of this guard (``a1 <= 0``) misses it whenever the
    tier is loud about some *other* doctype and silent about this one.

    Here no anchor of any doctype appears on the page, so the anchor tier is silent about
    everything including the winner, and the lexical tier is left to decide alone. It may not.
    """
    lexical_only = DocTypeSpec(
        doctype_id="lexical_only", label="Lexical Only", country="US", category=Category.other,
        anchors=[Anchor(text="A MASTHEAD THAT IS NOT ON THIS PAGE")],
        fields=[FieldSpec(name="a", labels={"en": ["Wharfage and demurrage schedule"]}),
                FieldSpec(name="b", labels={"en": ["Bunker fuel surcharge"]}),
                FieldSpec(name="c", labels={"en": ["Laytime commences on arrival"]})],
    )
    bystander = DocTypeSpec(
        doctype_id="bystander", label="Bystander", country="US", category=Category.other,
        anchors=[Anchor(text="SOMETHING ELSE ENTIRELY")],
        fields=[FieldSpec(name="d", labels={"en": ["Vessel tonnage certificate"]}),
                FieldSpec(name="e", labels={"en": ["Pilotage dues payable"]})],
    )
    view = view_of([
        TextBlock(text="Wharfage and demurrage schedule", zone=Zone.body),
        TextBlock(text="Bunker fuel surcharge", zone=Zone.body),
        TextBlock(text="Laytime commences on arrival", zone=Zone.body),
    ])
    specs = [lexical_only, bystander]
    profiles = build_profiles(specs)
    anchor = anchor_scores(view, specs, settings=SETTINGS)
    lexical = lexical_scores(view, profiles, settings=SETTINGS)
    assert anchor.bits["lexical_only"] == 0.0, "the anchor tier is silent about the winner"
    assert lexical.explained["lexical_only"] > lexical.explained["bystander"]

    result = classify(view, specs, settings=SETTINGS, profiles=profiles)
    fusion = next(e for e in result.evidence if e.tier == "fusion")
    assert "route=concurrence" not in fusion.detail, fusion.detail
    assert result.doctype_id == UNKNOWN
