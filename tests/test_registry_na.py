"""Tests for the North-American doctype packs (US / Canada / Mexico).

Pure and offline: the packs are data, so every assertion here is about the data. There are
three families of test.

*Structural* — the registry accepts the packs (``validate_registry``), every declared
validator and attribute key is known, every regex compiles, sensitive fields are flagged.

*Separability* — the pairs that a KYC operator actually confuses (W-9 vs W-8BEN, a state ID
vs a driver licence, a Canadian PR card vs a citizenship certificate, an INE vs a matrícula
consular) are separated by their **decisive** anchors on realistic specimen text. This file
carries its own small anchor matcher rather than importing the classifier: the point is to
prove the *data* separates, independently of how the cascade happens to score it. The
matcher is deliberately harsher than the real one — it ignores ``Anchor.zone``, so an anchor
that only wins because it is pinned to the title zone still has to survive here.

*Accent robustness* — Spanish and French anchors are stored accented, and OCR routinely
drops the diacritics. Folding both sides has to leave them matching, and folding must not
destroy the token structure.
"""

from __future__ import annotations

import importlib
import re
import unicodedata

import pytest

from dce.models import DocTypeSpec
from dce.registry import canada, loader, mexico, usa

NA_MODULES = (usa, canada, mexico)
ALL_SPECS: tuple[DocTypeSpec, ...] = tuple(spec for module in NA_MODULES for spec in module.SPECS)
BY_ID: dict[str, DocTypeSpec] = {spec.doctype_id: spec for spec in ALL_SPECS}

#: Validators the North-American packs are required to use. ``mrz_td2`` is deliberately
#: absent: no document in these packs uses the TD2 format.
REQUIRED_VALIDATORS = frozenset(
    {"ssn", "ein", "itin", "sin_luhn", "curp", "rfc", "mrz_td1", "mrz_td3"}
)

#: Identifiers that are restricted by law wherever they are used.
SENSITIVE_KEYS = frozenset({"id.ssn", "id.itin", "id.spouse_ssn", "id.sin", "id.curp"})


# ---------------------------------------------------------------------------
# A minimal, zone-agnostic anchor matcher
# ---------------------------------------------------------------------------
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def fold(text: str) -> str:
    """Accent-fold and case-fold, the way the classifier's skeleton form does.

    Args:
        text: Any anchor or document string.

    Returns:
        The NFKD decomposition with combining marks removed, case-folded. "MATRÍCULA" and
        "matricula" fold to the same string; Devanagari and other non-Latin scripts survive
        because stripping combining marks is all that happens.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def tokens(text: str) -> list[str]:
    """Fold, then tokenise on word boundaries — never a raw substring match."""
    return _WORD_RE.findall(fold(text))


def phrase_in(needle: str, haystack_tokens: list[str]) -> bool:
    """True when ``needle``'s tokens appear contiguously in ``haystack_tokens``."""
    needle_tokens = tokens(needle)
    if not needle_tokens:
        return False
    span = len(needle_tokens)
    return any(
        haystack_tokens[i : i + span] == needle_tokens
        for i in range(len(haystack_tokens) - span + 1)
    )


def anchor_hits(spec: DocTypeSpec, text: str, *, decisive_only: bool = False) -> list[str]:
    """Return the anchors of ``spec`` that fire on ``text``.

    Zone constraints are ignored on purpose: a doctype that separates from its neighbour
    without the help of a zone restriction separates with it too.
    """
    hay = tokens(text)
    return [
        anchor.text
        for anchor in spec.anchors
        if (anchor.decisive or not decisive_only) and phrase_in(anchor.text, hay)
    ]


def negative_hits(spec: DocTypeSpec, text: str) -> list[str]:
    """Return the negative anchors of ``spec`` that fire on ``text``."""
    hay = tokens(text)
    return [neg for neg in spec.negative_anchors if phrase_in(neg, hay)]


# ---------------------------------------------------------------------------
# Specimen text — the wording these documents actually print
# ---------------------------------------------------------------------------
W9_TEXT = """Form W-9 (Rev. March 2024)
Department of the Treasury Internal Revenue Service
Request for Taxpayer Identification Number and Certification
Go to www.irs.gov/FormW9 for instructions and the latest information.
1 Name (as shown on your income tax return)
3a Federal tax classification"""

W8BEN_TEXT = """Form W-8BEN (Rev. October 2021)
Department of the Treasury Internal Revenue Service
Certificate of Foreign Status of Beneficial Owner for United States Tax Withholding
and Reporting (Individuals)
For use by individuals. Entities must use Form W-8BEN-E.
OMB No. 1545-1621
1 Name of individual who is the beneficial owner
6a Foreign tax identifying number"""

W8BENE_TEXT = """Form W-8BEN-E (Rev. October 2021)
Department of the Treasury Internal Revenue Service
Certificate of Status of Beneficial Owner for United States Tax Withholding
and Reporting (Entities)
For use by entities. Individuals must use Form W-8BEN.
OMB No. 1545-1621
5 Chapter 4 Status (FATCA status)
GIIN"""

DRIVERS_LICENSE_TEXT = """CALIFORNIA
DRIVER LICENSE
DL I1234562
CLASS C
ENDORSEMENTS NONE
RESTRICTIONS NONE
DOB 08/31/1977
EXP 08/31/2028"""

STATE_ID_TEXT = """CALIFORNIA
IDENTIFICATION CARD
NOT FOR DRIVING
Identification No. D1234562
DOB 08/31/1977"""

MILITARY_ID_TEXT = """UNITED STATES UNIFORMED SERVICES IDENTIFICATION CARD
Department of Defense
DoD ID Number 1234567890
Pay Grade E-5"""

PR_CARD_TEXT = """CANADA
PERMANENT RESIDENT CARD
CARTE DE RÉSIDENT PERMANENT
IRCC
Category / Catégorie: PR1
Card No. / No de la carte
Expiry / Date d'expiration 2029-04-30"""

CITIZENSHIP_CERT_TEXT = """CERTIFICATE OF CANADIAN CITIZENSHIP
CERTIFICAT DE CITOYENNETÉ CANADIENNE
Immigration, Refugees and Citizenship Canada
Certificate No. 1234567
Effective date of citizenship"""

INE_TEXT = """INSTITUTO NACIONAL ELECTORAL
CREDENCIAL PARA VOTAR
NOMBRE PEREZ RAMIREZ JUAN
DOMICILIO CALLE MORELOS 12 COL CENTRO
CLAVE DE ELECTOR PRRMJN85010109H100
CURP PERJ850101HDFRMN09
SECCIÓN 1234
VIGENCIA 2031"""

#: The same INE header as an OCR engine that drops diacritics would return it.
INE_TEXT_UNACCENTED = """INSTITUTO NACIONAL ELECTORAL
CREDENCIAL PARA VOTAR
SECCION 1234"""

MATRICULA_CONSULAR_TEXT = """SECRETARÍA DE RELACIONES EXTERIORES
CONSULADO GENERAL DE MÉXICO
MATRÍCULA CONSULAR DE ALTA SEGURIDAD
Lugar de nacimiento: Michoacán
Domicilio en el extranjero"""

MATRICULA_CONSULAR_TEXT_UNACCENTED = """SECRETARIA DE RELACIONES EXTERIORES
CONSULADO GENERAL DE MEXICO
MATRICULA CONSULAR DE ALTA SEGURIDAD"""


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
def test_pack_sizes() -> None:
    """The packs keep at least their agreed baseline coverage, and their ids stay unique.

    The baselines (35 US, 25 Canadian, 20 Mexican) are floors rather than equalities. An
    equality here asserts that the registry never grows, which is not a property anyone
    wants: it turns "we added a doctype" into a test failure that says nothing about
    correctness, and it fails identically whether the addition was good or bad.

    The property that *is* worth asserting is the one the last line makes: no id collides
    across the three packs. That one is real — ``BY_ID`` is built by merging the packs, so a
    duplicate id would silently shadow a doctype rather than raise, and the count identity
    is the only thing that notices.
    """
    assert len(usa.SPECS) >= 35
    assert len(canada.SPECS) >= 25
    assert len(mexico.SPECS) >= 20
    assert len(BY_ID) == len(usa.SPECS) + len(canada.SPECS) + len(mexico.SPECS), (
        "doctype ids must be unique across the three packs"
    )


def test_country_code_matches_the_id_prefix() -> None:
    """``us_*`` is US, ``ca_*`` is CA, ``mx_*`` is MX — the loader relies on this."""
    for module, country in ((usa, "US"), (canada, "CA"), (mexico, "MX")):
        for spec in module.SPECS:
            assert spec.country == country
            assert spec.doctype_id.startswith(f"{country.lower()}_")


def test_registry_validates() -> None:
    """Every cross-spec rule the loader enforces holds with all three packs registered."""
    loader.validate_registry()


def test_every_spec_is_registered() -> None:
    """Importing a pack registers it — the object in the registry is the object we built."""
    for spec in ALL_SPECS:
        assert loader.get(spec.doctype_id) is spec


def test_importing_one_pack_pulls_in_its_siblings() -> None:
    """The NA packs cross-reference each other, so they have to load together.

    ``confusable_with`` entries such as ``us_green_card -> ca_pr_card`` are only checkable
    when both packs are registered; each pack therefore imports the other two after it has
    registered itself.
    """
    for name in ("dce.registry.usa", "dce.registry.canada", "dce.registry.mexico"):
        assert importlib.import_module(name) is not None
    registered = {spec.doctype_id for spec in loader.all_specs()}
    assert set(BY_ID) <= registered


def test_specs_carry_a_label_and_an_issuing_authority() -> None:
    """A doctype nobody can name in a review queue is not finished."""
    for spec in ALL_SPECS:
        assert spec.label.strip(), spec.doctype_id
        assert spec.category is not None
        assert spec.issuing_authority.strip(), spec.doctype_id
        assert spec.applies_to in {"individual", "corporate", "both"}
        assert any(field.required for field in spec.fields), (
            f"{spec.doctype_id} has no required field, so it can never report "
            "missing_required and would look complete no matter what was extracted"
        )


def test_confusable_targets_resolve() -> None:
    """Every ``confusable_with`` key names a registered doctype, and names the term.

    Most references stay inside the NA packs. A few cross into another pack — Delaware
    titles its filing "Certificate of Incorporation", exactly like the Indian MCA's — and
    those resolve because importing any pack imports ``dce.registry``, which loads them all.
    """
    registered = {spec.doctype_id for spec in loader.all_specs()}
    for spec in ALL_SPECS:
        for other, discriminator in spec.confusable_with.items():
            assert other in registered, f"{spec.doctype_id} -> unknown {other}"
            assert discriminator.strip(), f"{spec.doctype_id} -> {other} has no term"


def test_patterns_compile_and_never_match_the_empty_string() -> None:
    """A pattern that matches "" would bind an empty value and call it valid."""
    for spec in ALL_SPECS:
        for pattern in spec.id_patterns:
            assert re.compile(pattern).match("") is None, (spec.doctype_id, pattern)
        for field in spec.fields:
            if field.pattern is None:
                continue
            compiled = re.compile(field.pattern)
            assert compiled.match("") is None, (spec.doctype_id, field.name)


# ---------------------------------------------------------------------------
# Decisive anchors
# ---------------------------------------------------------------------------
def test_decisive_anchors_are_globally_unique() -> None:
    """No two NA doctypes claim the same decisive anchor.

    The loader tolerates a *declared* collision (a masked and an unmasked Aadhaar really do
    share a header). These packs do not need that latitude: where two documents share an
    issuing header — the IRS, the SAT, the CRA, Corporations Canada — the header is
    non-decisive and the form's own title carries the decision.
    """
    owners: dict[str, list[str]] = {}
    for spec in ALL_SPECS:
        for anchor in spec.anchors:
            if anchor.decisive:
                owners.setdefault(fold(anchor.text), []).append(spec.doctype_id)
    collisions = {text: ids for text, ids in owners.items() if len(ids) > 1}
    assert not collisions, f"decisive anchors shared by several doctypes: {collisions}"


def test_shared_issuing_headers_are_not_decisive() -> None:
    """The specific headers that appear on several documents must not decide anything."""
    shared = [
        "Internal Revenue Service",
        "Department of Homeland Security",
        "Secretary of State",
        "Canada Revenue Agency",
        "Corporations Canada",
        "SERVICIO DE ADMINISTRACIÓN TRIBUTARIA",
        "SECRETARÍA DE RELACIONES EXTERIORES",
        "USCIS",
        "IRCC",
    ]
    folded_shared = {fold(text) for text in shared}
    for spec in ALL_SPECS:
        for anchor in spec.anchors:
            if fold(anchor.text) in folded_shared:
                assert not anchor.decisive, (
                    f"{spec.doctype_id} makes the shared header {anchor.text!r} decisive"
                )


def test_every_doctype_owns_at_least_one_anchor() -> None:
    """Each doctype has one anchor string no other doctype declares.

    Without this, two doctypes are lexically identical and only the structural prior could
    ever separate them.
    """
    counts: dict[str, int] = {}
    for spec in ALL_SPECS:
        for anchor in spec.anchors:
            counts[fold(anchor.text)] = counts.get(fold(anchor.text), 0) + 1
    for spec in ALL_SPECS:
        assert any(counts[fold(a.text)] == 1 for a in spec.anchors), spec.doctype_id


def test_mexican_fallback_has_no_decisive_anchor() -> None:
    """``mx_comprobante_generico`` must never outrank a modelled issuer."""
    generic = BY_ID["mx_comprobante_generico"]
    assert not [a for a in generic.anchors if a.decisive]


# ---------------------------------------------------------------------------
# Separability of the pairs that actually get confused
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("left", "left_text", "right", "right_text"),
    [
        ("us_w9", W9_TEXT, "us_w8ben", W8BEN_TEXT),
        ("us_w8ben", W8BEN_TEXT, "us_w8bene", W8BENE_TEXT),
        ("ca_pr_card", PR_CARD_TEXT, "ca_citizenship_certificate", CITIZENSHIP_CERT_TEXT),
        ("mx_ine", INE_TEXT, "mx_matricula_consular", MATRICULA_CONSULAR_TEXT),
    ],
)
def test_confusable_pairs_are_separable_by_decisive_anchors(
    left: str, left_text: str, right: str, right_text: str
) -> None:
    """Each side fires its own decisive anchors and none of its neighbour's.

    ``us_state_id`` / ``us_drivers_license`` used to be in this list and is not any more.
    See :func:`test_a_document_class_name_is_not_a_decisive_anchor` for why: the string that
    put it here was ``IDENTIFICATION CARD``, which Alberta and Manitoba print too. The pair is
    still separable — see :func:`test_state_id_and_drivers_license_separate_without_decisive`,
    which pins the property that actually holds.
    """
    left_spec, right_spec = BY_ID[left], BY_ID[right]

    assert anchor_hits(left_spec, left_text, decisive_only=True), left
    assert anchor_hits(right_spec, right_text, decisive_only=True), right
    assert not anchor_hits(right_spec, left_text, decisive_only=True), (
        f"{right} decisive anchors fire on a {left} document"
    )
    assert not anchor_hits(left_spec, right_text, decisive_only=True), (
        f"{left} decisive anchors fire on a {right} document"
    )


def test_state_id_and_drivers_license_separate_without_decisive_anchors() -> None:
    """A pair can be separable without either side owning an exclusive string.

    ``us_state_id`` has no decisive anchor, on purpose. Its only candidate was
    ``IDENTIFICATION CARD``, and Alberta and Manitoba title their non-driver cards with the
    identical words — ``ca_provincial_photo_id`` declares the string here. There is no string
    on a California ID that no other jurisdiction's ID card prints, so what identifies it is
    the *combination*, which is the lexical tier's job rather than L1's.

    That is not a weakening. A decisive anchor buys one thing — the conclusive-L1
    identification route — and a doctype that reaches that route on a string its neighbour
    also prints reaches it wrongly. What must hold is that each side's anchors fire on its own
    specimen and not on its neighbour's, and that is asserted here directly.
    """
    state_id, licence = BY_ID["us_state_id"], BY_ID["us_drivers_license"]

    assert not [a for a in state_id.anchors if a.decisive], (
        "us_state_id must not reclaim a document-class name as decisive"
    )
    own = set(anchor_hits(state_id, STATE_ID_TEXT))
    assert {"IDENTIFICATION CARD", "NOT FOR DRIVING"} <= own

    # Neither side's anchors are satisfied by the other's document.
    assert "NOT FOR DRIVING" not in anchor_hits(state_id, DRIVERS_LICENSE_TEXT)
    assert not anchor_hits(licence, STATE_ID_TEXT, decisive_only=True), (
        "a driver-licence decisive anchor fires on a non-driver ID"
    )


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("us_w9", "us_w8ben"),
        ("us_w9", "us_w8bene"),
        ("us_state_id", "us_drivers_license"),
        ("ca_pr_card", "ca_citizenship_certificate"),
        ("mx_ine", "mx_matricula_consular"),
    ],
)
def test_confusable_pairs_declare_each_other(left: str, right: str) -> None:
    """The pair is declared in both directions, each naming the separating term."""
    assert right in BY_ID[left].confusable_with, f"{left} does not declare {right}"
    assert left in BY_ID[right].confusable_with, f"{right} does not declare {left}"
    assert BY_ID[left].confusable_with[right].strip()
    assert BY_ID[right].confusable_with[left].strip()


def test_w8bene_form_number_is_not_decisive() -> None:
    """The bare form number "Form W-8BEN-E" appears on the *individual* W-8BEN.

    Its header reads "For use by individuals. Entities must use Form W-8BEN-E." — so making
    that string decisive for the entity form would misclassify every individual W-8BEN.
    This is the exact trap a substring- or phrase-matching classifier walks into, and the
    reason the -E form's decisive anchors are its full title and its own use-line.
    """
    w8bene = BY_ID["us_w8bene"]
    decisive = {fold(a.text) for a in w8bene.anchors if a.decisive}
    assert fold("Form W-8BEN-E") not in decisive
    assert not anchor_hits(w8bene, W8BEN_TEXT, decisive_only=True)


def test_negative_anchors_are_a_secondary_control_not_the_separator() -> None:
    """Negative anchors help, and they are not what makes a shared title safe.

    This test used to assert the opposite, and its own docstring stated the defect as the
    design: "A Canadian PR card is titled PERMANENT RESIDENT CARD in English — exactly the US
    green card's decisive anchor — so ``us_green_card`` must carry negative anchors that fire
    on the Canadian card." It even asserted, as a *precondition*, that the shared English title
    fires as a decisive hit for the US doctype on a Canadian document.

    That mitigation cannot work, and the reason is the same OCR loss on both sides. The US
    doctype's negative anchors are ``CARTE DE RÉSIDENT PERMANENT``, ``IRCC`` and
    ``EMPLOYMENT AUTHORIZATION``; the Canadian doctype's decisive anchors are
    ``CARTE DE RÉSIDENT PERMANENT`` and ``RÉSIDENT PERMANENT``. The French line carries both.
    Drop it — routine on a bilingual card — and the negative anchor is as silent as the
    decisive one, leaving the US doctype the sole decisive claimant of a string Canada prints
    in the same words. Measured: ``PERMANENT RESIDENT CARD / CANADA / P<CANSMITH<<JANE<<<<``
    classified ``us_green_card``, ``country="US"``, confidence 0.900, ``abstained=False``.
    A negative anchor is a score adjustment; it was never a veto, and a control that is
    defeated by the same input damage as the thing it protects is not a control.

    What actually makes the shared title safe is that no doctype claims it as decisive, so
    neither can reach the conclusive-L1 route on it alone. The negative anchors stay — they
    are useful and they cost nothing — but they are pinned here as secondary.
    """
    green_card = BY_ID["us_green_card"]
    assert not anchor_hits(green_card, PR_CARD_TEXT, decisive_only=True), (
        "us_green_card must not hold a decisive claim on a Canadian PR card's text"
    )
    assert "PERMANENT RESIDENT CARD" in anchor_hits(green_card, PR_CARD_TEXT), (
        "the shared title is still evidence — it is just not proof"
    )
    assert negative_hits(green_card, PR_CARD_TEXT), (
        "the secondary control is still wired up"
    )
    assert anchor_hits(BY_ID["ca_pr_card"], PR_CARD_TEXT, decisive_only=True), (
        "the Canadian doctype still owns exclusive French decisive anchors"
    )

    # us_state_id no longer holds a decisive anchor at all (see
    # test_state_id_and_drivers_license_separate_without_decisive_anchors), so the DoD card is
    # separated the same way: the military doctype owns an exclusive string, the state ID does
    # not claim one, and the negative anchor remains as the secondary control.
    state_id = BY_ID["us_state_id"]
    assert not anchor_hits(state_id, MILITARY_ID_TEXT, decisive_only=True)
    assert negative_hits(state_id, MILITARY_ID_TEXT)
    assert anchor_hits(BY_ID["us_military_id"], MILITARY_ID_TEXT, decisive_only=True)


# ---------------------------------------------------------------------------
# Validators and attribute keys
# ---------------------------------------------------------------------------
def test_every_validator_name_is_declared_in_the_contract() -> None:
    """A validator the registry has not been told about is a typo waiting to happen."""
    for spec in ALL_SPECS:
        for field in spec.fields:
            if field.validator:
                assert field.validator in loader.VALIDATOR_CONTRACT, (
                    f"{spec.doctype_id}.{field.name} -> {field.validator}"
                )
                assert loader.VALIDATOR_CONTRACT[field.validator].strip()


def test_the_required_north_american_validators_are_actually_used() -> None:
    """Checksummable NA identifiers are bound to their validator, not left as free text."""
    used = {f.validator for s in ALL_SPECS for f in s.fields if f.validator}
    assert used >= REQUIRED_VALIDATORS, REQUIRED_VALIDATORS - used


def test_validator_names_resolve_in_the_validate_module() -> None:
    """Once ``dce.extract.validate`` exists, every referenced name must resolve there."""
    pytest.importorskip("dce.extract.validate")
    probe = loader._validator_probe()
    assert probe is not None
    unresolved = sorted(
        name
        for spec in ALL_SPECS
        for field in spec.fields
        if field.validator and not probe(field.validator)
        for name in [field.validator]
    )
    assert not unresolved, f"validators referenced but not implemented: {set(unresolved)}"


def test_attribute_keys_are_in_the_catalog() -> None:
    """Every key a field emits is declared, so the merge view knows where it lands."""
    for spec in ALL_SPECS:
        for field in spec.fields:
            if field.attribute_key:
                assert field.attribute_key in loader.ATTRIBUTE_KEYS, (
                    f"{spec.doctype_id}.{field.name} -> {field.attribute_key}"
                )


def test_pack_namespace_extensions_agree_with_each_other() -> None:
    """The shared entries the three packs declare must be declared identically.

    Each pack declares the keys and validators it uses so it can be imported alone. Where
    two packs declare the same name, the text has to match, or the meaning of a key would
    depend on import order.
    """
    for left, right in ((usa, canada), (canada, mexico), (usa, mexico)):
        for attr in ("ATTRIBUTE_KEY_EXTENSIONS", "VALIDATOR_EXTENSIONS"):
            a, b = getattr(left, attr), getattr(right, attr)
            for key in set(a) & set(b):
                assert a[key] == b[key], f"{attr}[{key!r}] differs between packs"


def test_mrz_validator_matches_the_document_format() -> None:
    """Passport books are TD3; wallet-card credentials are TD1."""
    books = {"us_passport", "ca_passport", "mx_passport"}
    cards = {"us_passport_card", "us_green_card", "ca_pr_card"}
    for doctype_id in books | cards:
        spec = BY_ID[doctype_id]
        mrz = [f for f in spec.fields if f.name == "machine_readable_zone"]
        assert mrz, f"{doctype_id} has no MRZ field"
        expected = "mrz_td3" if doctype_id in books else "mrz_td1"
        assert mrz[0].validator == expected, doctype_id
        assert "mrz" in mrz[0].locators


# ---------------------------------------------------------------------------
# Handling of sensitive data
# ---------------------------------------------------------------------------
def test_restricted_identifiers_are_flagged_pii() -> None:
    """SSN, ITIN, SIN and CURP fields are PII, and are either validated or explained.

    The one field that legitimately carries no validator is the 1099 recipient TIN: it may
    be an SSN, an ITIN or an EIN, and the recipient copy usually masks it to the last four
    digits, so validating it would reject the normal case. That is allowed only because the
    field says so in ``notes``.
    """
    for spec in ALL_SPECS:
        for field in spec.fields:
            if field.attribute_key not in SENSITIVE_KEYS:
                continue
            assert field.pii, f"{spec.doctype_id}.{field.name} is not flagged pii"
            assert field.validator or field.notes.strip(), (
                f"{spec.doctype_id}.{field.name} has neither a validator nor a note "
                "explaining why a restricted identifier is left unchecked"
            )


def test_primary_identifier_documents_carry_handling_notes() -> None:
    """The documents whose whole purpose is a restricted number say how to treat it."""
    for doctype_id in ("us_ssn_card", "us_itin_letter", "ca_sin_confirmation", "mx_ine"):
        assert BY_ID[doctype_id].handling.strip(), doctype_id


def test_health_card_is_not_officially_valid_and_says_why() -> None:
    """Several provinces prohibit using a health number for identification.

    Flagging the card as acceptable identity evidence would push a compliance breach into
    the extraction pipeline, so the spec records the exclusion and the reason.
    """
    health = BY_ID["ca_health_card"]
    assert health.officially_valid is False
    assert "PHIPA" in health.handling or "FINTRAC" in health.handling


def test_photo_identity_documents_are_flagged_officially_valid() -> None:
    """The credentials each regime accepts as primary photo ID are marked as such."""
    expected_valid = {
        "us_passport",
        "us_passport_card",
        "us_drivers_license",
        "us_state_id",
        # "us_real_id" is deliberately absent: it was merged into the two doctypes above as
        # the ``real_id_compliant`` field. See test_real_id_is_a_field_not_a_doctype.
        "us_green_card",
        "us_ead",
        "us_military_id",
        "ca_passport",
        "ca_drivers_license",
        "ca_provincial_photo_id",
        "ca_pr_card",
        "ca_citizenship_certificate",
        "ca_secure_status_card",
        "ca_nexus",
        "mx_ine",
        "mx_passport",
        "mx_cedula_profesional",
        "mx_cartilla_militar",
        "mx_matricula_consular",
        "mx_tarjeta_residente",
    }
    actual = {s.doctype_id for s in ALL_SPECS if s.officially_valid}
    assert actual == expected_valid
    for doctype_id in sorted(actual):
        assert BY_ID[doctype_id].handling.strip(), (
            f"{doctype_id} is accepted as identity evidence but says nothing about how it "
            "may be retained or used"
        )


def test_identifiers_without_a_published_format_are_documented_not_invented() -> None:
    """Where no format is published, the field carries a note and no regex.

    A fabricated pattern silently rejects genuine documents, which in a KYC system means
    silently rejecting genuine people. These are the fields where that risk is real.
    """
    undocumented = [
        ("us_drivers_license", "license_number"),
        ("us_state_id", "id_number"),
        ("ca_drivers_license", "license_number"),
        ("ca_pr_card", "pr_card_number"),
        ("mx_matricula_consular", "matricula_number"),
        ("mx_predial", "cuenta_catastral"),
    ]
    for doctype_id, field_name in undocumented:
        field = next(f for f in BY_ID[doctype_id].fields if f.name == field_name)
        assert field.pattern is None, f"{doctype_id}.{field_name} asserts a format"
        assert field.notes.strip(), f"{doctype_id}.{field_name} has no explanation"


# ---------------------------------------------------------------------------
# Language and accent folding
# ---------------------------------------------------------------------------
def test_every_mexican_doctype_has_spanish_anchors() -> None:
    """The Mexican pack is anchored in the language its documents are printed in."""
    for spec in mexico.SPECS:
        assert [a for a in spec.anchors if a.lang == "es"], spec.doctype_id


def test_english_anchors_exist_where_the_mexican_document_prints_english() -> None:
    """English anchors are declared only for genuinely bilingual documents.

    The passport data page's field labels, the consular card and the bilingual statements
    the international banks issue really do print English. Inventing an English anchor for
    a Spanish-only document would be a string that appears on no specimen and can only
    create false positives, so the packs use bilingual field *labels* for translated copies
    instead.
    """
    for doctype_id in ("mx_passport", "mx_matricula_consular", "mx_estado_cuenta"):
        spec = BY_ID[doctype_id]
        assert [a for a in spec.anchors if a.lang == "en"], doctype_id


def test_mexican_fields_carry_both_spanish_and_english_labels() -> None:
    """Certified English translations are routine in KYC packs; labels cover both."""
    for spec in mexico.SPECS:
        labelled = [f for f in spec.fields if f.labels]
        assert labelled, spec.doctype_id
        assert any("es" in f.labels for f in labelled), spec.doctype_id
        assert any("en" in f.labels for f in labelled), spec.doctype_id


def test_bilingual_canadian_doctypes_have_french_anchors() -> None:
    """Canadian federal documents are bilingual, and so are their anchors."""
    federal = {
        "ca_passport",
        "ca_pr_card",
        "ca_copr",
        "ca_citizenship_certificate",
        "ca_secure_status_card",
        "ca_refugee_protection_doc",
        "ca_sin_confirmation",
        "ca_cra_noa",
        "ca_t4",
        "ca_t1_general",
        "ca_bn_letter",
        "ca_articles_incorporation_federal",
        "ca_certificate_status",
        "ca_annual_return",
    }
    for doctype_id in federal:
        spec = BY_ID[doctype_id]
        assert [a for a in spec.anchors if a.lang == "fr"], doctype_id


def test_canadian_drivers_licence_uses_the_canadian_spelling() -> None:
    """LICENCE versus LICENSE is the cheapest US/Canada discriminator there is.

    It is still the discriminator; it is no longer a *decisive* one, and the demotion is
    measured rather than cautious. ``corpus/ca/ca_sin_confirmation.pdf`` lists the ID Service
    Canada accepts and prints ``DRIVER'S LICENSE`` in that list, and the AAMVA specimen in
    ``corpus/us`` prints ``DRIVER'S LICENCE`` — so each spelling appears on a document of the
    other country, and a decisive claim on either was false. This is the acceptable-document
    list hazard in its purest form: KYC onboarding paperwork enumerates the document classes
    it collects, and a classifier anchored on class names reads the list as the thing.

    What survives is what an issuer actually controls: the French title on the Canadian side
    (Canada is the only issuer of a bilingual EN/FR licence) and the un-apostrophised AAMVA
    card title on the US side.
    """
    ca_anchors = {a.text for a in BY_ID["ca_drivers_license"].anchors}
    us_anchors = {a.text for a in BY_ID["us_drivers_license"].anchors}
    ca_decisive = {a.text for a in BY_ID["ca_drivers_license"].anchors if a.decisive}
    us_decisive = {a.text for a in BY_ID["us_drivers_license"].anchors if a.decisive}

    assert any("LICENCE" in text for text in ca_anchors)
    assert all("LICENCE" not in text for text in us_decisive)
    assert all("LICENSE" not in text for text in ca_decisive)
    assert "PERMIS DE CONDUIRE" in ca_decisive
    assert "DRIVER LICENSE" in us_decisive
    # And the reason neither spelling may be decisive, stated as data rather than as prose.
    assert "DRIVER'S LICENCE" in ca_anchors
    assert "DRIVER'S LICENSE" in us_anchors
    assert not any(a.decisive for a in BY_ID["ca_drivers_license"].anchors
                   if a.text == "DRIVER'S LICENCE")
    assert not any(a.decisive for a in BY_ID["us_drivers_license"].anchors
                   if a.text == "DRIVER'S LICENSE")


@pytest.mark.parametrize(
    ("doctype_id", "accented", "unaccented"),
    [
        (
            "mx_matricula_consular",
            "MATRÍCULA CONSULAR DE ALTA SEGURIDAD",
            "MATRICULA CONSULAR DE ALTA SEGURIDAD",
        ),
        ("mx_rfc_csf", "CONSTANCIA DE SITUACIÓN FISCAL", "CONSTANCIA DE SITUACION FISCAL"),
        ("mx_cif", "CÉDULA DE IDENTIFICACIÓN FISCAL", "CEDULA DE IDENTIFICACION FISCAL"),
        ("ca_pr_card", "CARTE DE RÉSIDENT PERMANENT", "CARTE DE RESIDENT PERMANENT"),
        (
            "ca_citizenship_certificate",
            "CERTIFICAT DE CITOYENNETÉ CANADIENNE",
            "CERTIFICAT DE CITOYENNETE CANADIENNE",
        ),
        ("ca_cra_noa", "AVIS DE COTISATION", "AVIS DE COTISATION"),
        ("ca_property_tax_assessment", "AVIS D'ÉVALUATION FONCIÈRE", "AVIS D'EVALUATION FONCIERE"),
    ],
)
def test_accented_anchors_match_unaccented_ocr(
    doctype_id: str, accented: str, unaccented: str
) -> None:
    """The accented anchor is declared, and it still fires when OCR drops the accents.

    The subject here is accent folding, not decisiveness, so the hit is asserted at the
    anchor level and the decisive assertion is made only where the anchor is *still* decisive.
    Two of these no longer are: ``CÉDULA DE IDENTIFICACIÓN FISCAL`` and
    ``CONSTANCIA DE SITUACIÓN FISCAL`` were demoted because the SAT prints the cédula as page
    one of the constancia, so each title is printed on the other's document — see
    ``test_cedula_and_constancia_stay_separate_and_page_one_is_declared_ambiguous``. Folding
    has to keep working for them regardless: a demoted anchor still feeds the lexical channel,
    and it is the lexical channel that carries these two now.
    """
    spec = BY_ID[doctype_id]
    declared = {a.text: a for a in spec.anchors}
    assert accented in declared, f"{doctype_id} does not declare {accented!r}"
    assert phrase_in(accented, tokens(unaccented))
    assert anchor_hits(spec, unaccented)
    if declared[accented].decisive:
        assert anchor_hits(spec, unaccented, decisive_only=True)


def test_folding_preserves_token_structure() -> None:
    """Accent folding must drop marks only — never merge or split words."""
    for spec in ALL_SPECS:
        for anchor in spec.anchors:
            raw_words = len(_WORD_RE.findall(anchor.text.casefold()))
            folded_words = len(tokens(anchor.text))
            assert folded_words == raw_words, (spec.doctype_id, anchor.text)


def test_spanish_and_french_pairs_separate_after_folding() -> None:
    """Folding must not make two doctypes collide that were distinct while accented."""
    assert anchor_hits(BY_ID["mx_ine"], INE_TEXT_UNACCENTED, decisive_only=True)
    assert not anchor_hits(BY_ID["mx_matricula_consular"], INE_TEXT_UNACCENTED, decisive_only=True)
    assert anchor_hits(
        BY_ID["mx_matricula_consular"],
        MATRICULA_CONSULAR_TEXT_UNACCENTED,
        decisive_only=True,
    )
    assert not anchor_hits(BY_ID["mx_ine"], MATRICULA_CONSULAR_TEXT_UNACCENTED, decisive_only=True)


def test_normalize_collapses_accented_and_unaccented_anchors_together() -> None:
    """``dce.normalize``'s skeleton must make the accented anchor and an OCR read equal.

    The skeleton also folds OCR-confusable characters (I/1, S/5, O/0), so it is *not* the
    same string as this file's accent-only folding — comparing the two forms would be
    testing the wrong thing. What has to hold is that the accented anchor and its
    accent-stripped read collapse to the same skeleton, and that the word count survives.
    """
    normalize_module = pytest.importorskip("dce.normalize")
    normalize = normalize_module.normalize
    pairs = [
        ("MATRÍCULA CONSULAR DE ALTA SEGURIDAD", "MATRICULA CONSULAR DE ALTA SEGURIDAD"),
        ("CARTE DE RÉSIDENT PERMANENT", "CARTE DE RESIDENT PERMANENT"),
        ("CONSTANCIA DE SITUACIÓN FISCAL", "CONSTANCIA DE SITUACION FISCAL"),
        ("AVIS D'ÉVALUATION FONCIÈRE", "AVIS D'EVALUATION FONCIERE"),
    ]
    for accented, unaccented in pairs:
        accented_skeleton = getattr(normalize(accented), "skeleton", "")
        unaccented_skeleton = getattr(normalize(unaccented), "skeleton", "")
        assert accented_skeleton
        assert accented_skeleton == unaccented_skeleton, accented
        assert len(_WORD_RE.findall(accented_skeleton)) == len(tokens(accented))


# ---------------------------------------------------------------------------
# Registry-design decisions that eliminated a wrong answer each
#
# These three tests are not about scoring. Each one pins a *structural* claim about the
# data that a future edit could quietly undo, and each corresponds to a document the
# classifier used to get wrong or to refuse for a reason nobody could explain.
# ---------------------------------------------------------------------------

#: A REAL-ID-compliant licence. The compliance marking is a star, which is a figure and not
#: text, so the only compliance evidence in an OCR dump is the AAMVA payload's DDA element.
REAL_ID_LICENCE_TEXT = """Real ID Driver's License - Over 21
Barcode Reader Calibration Sheet - 2020 AAMVA Standard Version: 10
ANSI 636000100202DL00410422ZV04630096DL
Vehicle class DCA D, M2  Restriction DCB NONE  Endorsement DCD S
Compliance type DDA F  "F" Fully Compliant (including all REAL ID DLs and IDs
and eDLs and eIDs)  "N" Non-Compliant (for all State Issued (non-Real ID) Documents
"""

#: The *same* AAMVA sheet for the standard, NON-compliant licence. The point of carrying it
#: here is that it prints the words "REAL ID" too, in the legend explaining DDA.
STANDARD_LICENCE_TEXT = """Standard Driver's License Card - Under 21
Barcode Reader Calibration Sheet - 2020 AAMVA Standard Version: 10
ANSI 636000100202DL00410422ZV04630096DL
Vehicle class DCA D  Restriction DCB NONE  Endorsement DCD NONE
Compliance type DDA N  "F" Fully Compliant (including all REAL ID DLs and IDs
and eDLs and eIDs)  "N" Non-Compliant (for all State Issued (non-Real ID) Documents
"""


def test_real_id_is_a_field_not_a_doctype() -> None:
    """REAL ID compliance is an attribute of a licence or ID card, never a document type.

    The REAL ID Act of 2005 and 6 CFR Part 37 set minimum standards for a *state-issued
    driver licence or identification card*; 6 CFR 37.17(n) adds the star marking and leaves
    the card's title alone. The set of REAL ID cards is therefore a strict subset of
    ``us_drivers_license`` united with ``us_state_id``, and no anchor can separate a subset
    from its own superset — the superset's issuer prints everything the subset does. A
    doctype that cannot be distinguished from its parent can only ever take answers away
    from it, which is exactly what it did.
    """
    assert "us_real_id" not in BY_ID, (
        "us_real_id was merged into the real_id_compliant field; re-adding it re-adds a "
        "doctype that is a strict subset of us_drivers_license / us_state_id"
    )
    for doctype_id in ("us_drivers_license", "us_state_id"):
        field = next(
            (f for f in BY_ID[doctype_id].fields if f.name == "real_id_compliant"), None
        )
        assert field is not None, f"{doctype_id} lost the real_id_compliant field"
        assert field.type == "bool"
        assert field.attribute_key == "doc.real_id_compliant"
        assert not field.required, (
            f"{doctype_id}.real_id_compliant must not be required: the marking is a star, "
            "a figure rather than text, so a compliant card routinely yields no value"
        )
        assert field.notes.strip(), f"{doctype_id}.real_id_compliant has no caveat recorded"


def test_real_id_wording_is_not_an_anchor_anywhere() -> None:
    """"REAL ID" is not a discriminator, in any zone, for any doctype.

    The words appear on the *non*-compliant licence too — AAMVA prints them in the legend
    that explains what element DDA means — so a doctype anchored on them would fire on the
    documents it is supposed to exclude. This is the concrete instance of the general rule
    that a decisive anchor must be a string one issuer controls AND that picks out exactly
    one document type.
    """
    for spec in ALL_SPECS:
        for anchor in spec.anchors:
            assert "real id" not in anchor.text.casefold(), (
                f"{spec.doctype_id} anchors on {anchor.text!r}; the standard "
                "non-compliant AAMVA licence prints those words too"
            )
    # And the corroborating fact, asserted rather than asserted-about: the two AAMVA sheets
    # are separated by DDA, not by the programme name, which both of them print.
    assert "REAL ID" in REAL_ID_LICENCE_TEXT
    assert "REAL ID" in STANDARD_LICENCE_TEXT
    licence = BY_ID["us_drivers_license"]
    # Both sheets are still identified, and no longer by a decisive anchor: they print
    # "Driver's License", and that spelling was demoted because corpus/ca/ca_sin_confirmation
    # prints it too, in its list of acceptable ID. The doctype's remaining decisive anchor is
    # the AAMVA card title "DRIVER LICENSE", which these AAMVA *calibration sheets* do not
    # carry. That is the honest state: on a document that only says "Driver's License", this
    # doctype has lexical evidence and no proof, which is what an abstention is for.
    assert anchor_hits(licence, REAL_ID_LICENCE_TEXT)
    assert anchor_hits(licence, STANDARD_LICENCE_TEXT)


#: An oil-and-gas issuer's AIF. NI 51-101 s.2.1 obliges the issuer to file Forms 51-101F1,
#: F2 and F3, and they are routinely bound into the AIF as schedules — so an AIF legitimately
#: carries all three form numbers. What it also carries, and a reserves statement never does,
#: is Form 51-102F2's own item headings.
AIF_WITH_RESERVES_TEXT = """ANNUAL INFORMATION FORM
For the year ended December 31, 2025
Corporate Structure - Intercorporate Relationships
General Development of the Business
Description of Capital Structure
Market for Securities - Trading Price and Volume
Legal Proceedings and Regulatory Actions
Interest of Management and Others in Material Transactions
Transfer Agents and Registrars
Interests of Experts
Audit Committee Information
Schedule "A" Form 51-101F2 Report on Reserves Data by Independent Qualified
Reserves Evaluator or Auditor
Schedule "B" Form 51-101F3 Report of Management and Directors on Oil and Gas Disclosure
Form 51-101F1 Statement of Reserves Data and Other Oil and Gas Information
disclosed in accordance with National Instrument 51-101 Standards of Disclosure for
Oil and Gas Activities
"""

#: A standalone reserves statement — the document ``ca_ni_51_101_oil_gas`` is actually for.
STANDALONE_51_101_TEXT = """FORM 51-101F1
STATEMENT OF RESERVES DATA AND OTHER OIL AND GAS INFORMATION
The information contained in this Form 51-101F1 is required to be included pursuant to
National Instrument 51-101 Standards of Disclosure for Oil and Gas Activities.
Readers should also refer to the Form 51-101F2 Report on Reserves Data by Independent
Qualified Reserves Evaluator and the Form 51-101F3 Report of Management and Directors
on Oil and Gas Disclosure. Future net revenue, COGE Handbook.
"""


def test_aif_and_reserves_statement_declare_the_containment_both_ways() -> None:
    """The relationship is containment, and both specs have to say so.

    ``confusable_with`` is read in both directions by the cascade's audibility guard, so a
    one-sided declaration leaves half the guard inoperative.
    """
    assert "ca_ni_51_101_oil_gas" in BY_ID["ca_aif"].confusable_with
    assert "ca_aif" in BY_ID["ca_ni_51_101_oil_gas"].confusable_with


def test_reserves_form_numbers_do_not_identify_a_document_that_encloses_them() -> None:
    """An AIF carrying bound-in 51-101 schedules must not read as a reserves statement.

    This test's own premise is what retired the form numbers' decisive status. It used to say
    "both doctypes' decisive anchors fire on this text and that is *correct* — the form
    numbers really are printed", and treat the separation as the negative anchors' job alone.
    The corpus then produced the document rather than the fixture:
    ``corpus/ca/ca_aif__oilgas_issuer.pdf`` prints ``FORM 51-101F1``, ``F2``, ``F3`` and
    ``NATIONAL INSTRUMENT 51-101``, and it is an AIF. A decisive anchor is a claim that the
    string appears on this doctype's documents and no others, so on the corpus's own evidence
    those four claims were false — perfectly issuer-controlled strings, defeated by citation.

    The form numbers are still declared, still fire, and still feed the lexical channel. What
    they no longer do is short-circuit L1 on a document that merely *encloses* them, which is
    a strictly better outcome than relying on the negative anchors to undo a decisive hit.
    """
    reserves = BY_ID["ca_ni_51_101_oil_gas"]
    aif = BY_ID["ca_aif"]

    assert anchor_hits(reserves, AIF_WITH_RESERVES_TEXT), (
        "the form numbers are genuinely present; a test that pretended otherwise would be "
        "testing a document nobody files"
    )
    assert not anchor_hits(reserves, AIF_WITH_RESERVES_TEXT, decisive_only=True), (
        "a form number an AIF is obliged to print cannot be near-proof of a reserves "
        "statement; that is the citation failure mode, measured on a real filing"
    )
    # The separation is now carried entirely by POSITIVE evidence on the AIF's side. The ten
    # Form 51-102F2 item headings used to sit here as negative anchors on the reserves
    # statement; ca_ni_51_101_oil_gas explains at its own definition why they are gone
    # (ca_aif declares the whole item list and wins the anchor channel outright, so the
    # penalty could not change any outcome). Both halves of the containment are asserted:
    # the AIF must fire, and the reserves statement must not fire decisively.
    assert negative_hits(reserves, AIF_WITH_RESERVES_TEXT) == []
    aif_fired = anchor_hits(aif, AIF_WITH_RESERVES_TEXT)
    assert len(aif_fired) >= 5, (
        f"only {aif_fired} of ca_aif's Form 51-102F2 item headings fired on an AIF; the "
        "containment discriminator has moved to positive evidence and has been weakened"
    )


def test_reserves_statement_negatives_never_fire_on_a_real_reserves_statement() -> None:
    """The guard against fixing one document by breaking the other.

    A negative anchor can only lower this doctype's score. Every string in the set must
    therefore be one that a genuine standalone Form 51-101F1/F2/F3 filing cannot contain —
    otherwise the containment fix would be paid for by the documents the doctype exists for.
    """
    reserves = BY_ID["ca_ni_51_101_oil_gas"]
    assert negative_hits(reserves, STANDALONE_51_101_TEXT) == []
    # The cost of demoting the form numbers, stated rather than hidden: on a genuine
    # standalone filing this doctype now has lexical evidence and no decisive anchor at all —
    # nothing in the 51-101 family distinguishes a reserves statement from the AIF that is
    # obliged to bind it in, so there is no string left to be near-proof. That is a real
    # coverage loss and it was
    # accepted, because the alternative is a claim the corpus shows to be false.
    assert anchor_hits(reserves, STANDALONE_51_101_TEXT)
    assert not anchor_hits(reserves, STANDALONE_51_101_TEXT, decisive_only=True)


#: Page 1 of a Constancia de Situación Fiscal. The SAT reproduces the cédula, QR code and
#: all, as the constancia's first sheet — so this text carries both titles and cannot be
#: told apart from a standalone cédula by anything printed on it.
CSF_PAGE_ONE_TEXT = """Página [1] de [3]
CÉDULA DE IDENTIFICACIÓN FISCAL
Registro Federal de Contribuyentes
idCIF: 14091470816
CONSTANCIA DE SITUACIÓN FISCAL
Lugar y Fecha de Emisión
Datos de Identificación del Contribuyente
Régimen Capital: SIN TIPO DE SOCIEDAD
Datos del domicilio registrado
"""


def test_cedula_and_constancia_stay_separate_and_page_one_is_declared_ambiguous() -> None:
    """Two documents, not one — and a page-1 extract is undecidable *by construction*.

    Merging them would lose a real distinction: a cédula identifies the taxpayer, a
    constancia additionally publishes the regimes and standing obligations, which is a
    larger disclosure with a different retention profile. Keeping them costs an abstention
    on any page-1-only extract, and that abstention is the right answer rather than a gap:
    the information needed to decide is on pages 2-3, which the caller did not send.

    The registry states this itself. The cédula's negative anchor fires on the constancia's
    title, so the data declares "this is not a standalone cédula" without any anchor having
    to be invented to make a truncated file classify.
    """
    cedula, constancia = BY_ID["mx_cif"], BY_ID["mx_rfc_csf"]
    # Both titles fire, and NEITHER fires decisively any more. That is the same fact this
    # test has always asserted, finally expressed in the anchors instead of only in prose:
    # each doctype's title is printed on the other's document, so neither title can be
    # near-proof of either. The previous state — both decisive, both correct, separated
    # afterwards by a negative anchor — meant the L1 short-circuit had to be walked back
    # rather than never taken. Measured on the corpus: mx_cif's title matches
    # corpus/mx/mx_rfc_csf.pdf and mx_rfc_csf's matches corpus/mx/mx_cif.pdf.
    assert anchor_hits(cedula, CSF_PAGE_ONE_TEXT)
    assert anchor_hits(constancia, CSF_PAGE_ONE_TEXT)
    assert not anchor_hits(cedula, CSF_PAGE_ONE_TEXT, decisive_only=True)
    assert not anchor_hits(constancia, CSF_PAGE_ONE_TEXT, decisive_only=True)
    assert "CONSTANCIA DE SITUACIÓN FISCAL" in negative_hits(cedula, CSF_PAGE_ONE_TEXT)
    assert "mx_rfc_csf" in cedula.confusable_with
    assert "mx_cif" in constancia.confusable_with
    # The discriminating sections live past page 1 and are declared on the constancia.
    for section in ("Regímenes", "Obligaciones"):
        assert any(a.text == section for a in constancia.anchors), section
        assert section not in CSF_PAGE_ONE_TEXT


#: The Virginia DMV's AAMVA specimen for a Standard Identification Card. A US non-driver ID:
#: its barcode encodes the AAMVA country element ``DCG USA`` and jurisdiction ``DAJ VA``.
US_STATE_ID_AAMVA_TEXT = """Standard Identification Card - Over 21
Barcode Reader Calibration Sheet - 2020 AAMVA Standard Version: 10
ANSI 636000100202ID00410422ZV04630096ID
Jurisdiction DAJ VA   Country DCG USA
Compliance type DDA N
"""


def test_a_shared_document_class_name_is_equally_audible_to_both_claimants() -> None:
    """Two doctypes claiming one string must gate it to the same zone, or to none.

    ``IDENTIFICATION CARD`` is a document-class name — every jurisdiction's ID card prints
    it — so neither ``us_state_id`` nor ``ca_provincial_photo_id`` may treat it as decisive,
    and neither does. The subtler failure is *audibility*: while the US claim was pinned to
    ``zone=title`` and the Canadian claim was not, any payload without a title zone (which
    is every plain-text extraction) could hear only one of them. The zone gate then settled
    a cross-border question that the evidence never got to answer, and a Virginia DMV
    specimen scored the Canadian card above the American one.

    This is the audibility analogue of the asymmetry
    ``dce.registry.loader._check_decisive_asymmetry`` rejects — there the two packs disagreed
    about exclusivity, here about where the string counts — and the remedy is the same: make
    the claim symmetric and let the rest of the evidence decide.
    """
    us_anchor = next(
        a for a in BY_ID["us_state_id"].anchors if fold(a.text) == fold("IDENTIFICATION CARD")
    )
    ca_anchor = next(
        a
        for a in BY_ID["ca_provincial_photo_id"].anchors
        if fold(a.text) == fold("Identification Card")
    )
    assert not us_anchor.decisive and not ca_anchor.decisive
    assert us_anchor.zone == ca_anchor.zone, (
        "us_state_id and ca_provincial_photo_id claim the same document-class name with "
        f"different zone gates ({us_anchor.zone} vs {ca_anchor.zone}); whichever claim the "
        "payload can hear then decides the jurisdiction"
    )
    # And the consequence the symmetry buys: on a US specimen, the US-specific evidence is
    # now what separates them rather than a zone label.
    assert anchor_hits(BY_ID["us_state_id"], US_STATE_ID_AAMVA_TEXT)
    assert "DMV" not in US_STATE_ID_AAMVA_TEXT, (
        "this specimen deliberately lacks the DMV token, so the separation cannot come from "
        "ca_provincial_photo_id's negative anchor"
    )
