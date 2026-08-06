"""Classification tests. Offline, pure, no registry required — every spec is built here.

The first test is the one that matters. This service exists because a substring matcher
classified ordinary English prose as a driving licence, an EIN letter and a SIN card at the
same time, and a real passport at 0.5335 — below its own 0.55 floor. Both halves of that
failure are pinned below.
"""
from __future__ import annotations

import sys
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
    """The old implementation scored a real passport at 0.5335, below its own 0.55 floor."""
    result = classify(passport_view(), registry(), settings=SETTINGS)

    assert result.doctype_id == "passport"
    assert result.abstained is False
    assert result.confidence >= SETTINGS.classify_accept_probability
    assert result.confidence > 0.9, "a checksum-verified MRZ is near-proof"
    assert result.margin >= SETTINGS.classify_min_margin
    tiers = {e.tier for e in result.evidence}
    assert "checksum" in tiers and "anchor" in tiers


def test_passport_short_circuits_before_the_lexical_tier():
    """A checksum-verified decisive identifier must not pay for BM25."""
    result = classify(passport_view(), registry(), settings=SETTINGS)

    assert result.doctype_id == "passport"
    assert not any(e.tier == "lexical" for e in result.evidence)
    assert any("was not run" in e.detail for e in result.evidence)


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
