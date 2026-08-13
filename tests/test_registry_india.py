"""Offline tests for the India and cross-country doctype packs.

Pure and offline: importing :mod:`dce.registry` runs the whole registry validation, so most
of these tests are assertions about data that has already been structurally checked at
import. What is tested here is the *knowledge*, not the mechanism — that the RBI OVD set is
exactly right, that Aadhaar carries its masking obligation, that PAN and Aadhaar cannot be
confused, and that Devanagari anchors survive the normalisation the classifier actually
uses.
"""

from __future__ import annotations

import re
import unicodedata

import pytest

import dce.extract.validate as validate_module
from dce.models import (
    UNKNOWN,
    Anchor,
    Category,
    Controls,
    DocTypeSpec,
    LayoutView,
    PageInfo,
    TextBlock,
    Zone,
)
from dce.normalize import fold, skeletonize, tokenize_unicode
from dce.registry import (
    ATTRIBUTE_KEYS,
    KNOWN_FIELD_TYPES,
    KNOWN_LOCATORS,
    PENDING_VALIDATORS,
    VALIDATOR_CONTRACT,
    RegistryError,
    all_specs,
    by_country,
    get,
    required_validators,
    validate_registry,
)
from dce.registry.crosscountry import SPECS as XX_SPECS
from dce.registry.india import IN_OVD_DOCTYPES
from dce.registry.india import SPECS as INDIA_SPECS

#: The packs this test module owns. Other country packs register into the same table, and a
#: hygiene rule that is right for the India pack is not automatically right for theirs — an
#: assertion swept over ``all_specs()`` would fail *their* build for *our* opinion. Rules
#: that are genuinely registry-wide (attribute keys resolve, decisive anchors do not
#: collide, confusable_with targets exist) stay global on purpose; everything that encodes
#: an India-pack convention is scoped here.
OWNED_SPECS: tuple[DocTypeSpec, ...] = INDIA_SPECS + XX_SPECS

# ---------------------------------------------------------------------------
# Registry-wide invariants
# ---------------------------------------------------------------------------


def test_validate_registry_passes() -> None:
    """The whole registry validates. Re-run explicitly so a failure names this test."""
    validate_registry()


def test_pack_sizes() -> None:
    """52 Indian doctypes and 15 cross-country ones, with no id collisions."""
    assert len(INDIA_SPECS) == 52
    assert len(XX_SPECS) == 15
    grouped = by_country()
    assert len(grouped["IN"]) == 52
    assert len(grouped["XX"]) == 15
    ids = [s.doctype_id for s in all_specs()]
    assert len(ids) == len(set(ids))


def test_every_spec_is_reachable_by_id() -> None:
    for spec in all_specs():
        assert get(spec.doctype_id) is spec


def test_required_india_doctypes_are_all_present() -> None:
    """The 52 ids the pack is contracted to provide.

    The 16 added in the listed-issuer / due-diligence round are grouped at the end: seven MCA
    e-forms, four SEBI listed-entity filings, two professional audit reports, an RBI FEMA
    return and two Government of India business registrations.
    """
    expected = {
        "in_aadhaar",
        "in_aadhaar_masked",
        "in_pan",
        "in_passport",
        "in_voter_epic",
        "in_driving_licence",
        "in_nrega_job_card",
        "in_npr_letter",
        "in_form60",
        "in_ckyc_record",
        "in_bank_passbook",
        "in_bank_statement",
        "in_cancelled_cheque",
        "in_utility_electricity",
        "in_utility_water",
        "in_utility_gas",
        "in_utility_telephone",
        "in_rent_agreement",
        "in_property_tax_receipt",
        "in_gst_certificate",
        "in_certificate_incorporation",
        "in_moa",
        "in_aoa",
        "in_llp_incorporation",
        "in_partnership_deed",
        "in_partnership_reg_cert",
        "in_board_resolution",
        "in_itr_acknowledgement",
        "in_form16",
        "in_salary_slip",
        "in_employer_allotment_letter",
        "in_pension_payment_order",
        "in_ration_card",
        "in_marriage_certificate",
        "in_birth_certificate",
        "in_caste_certificate",
        # MCA e-forms
        "in_mca_mgt7_annual_return",
        "in_mca_aoc4_financial_statements",
        "in_mca_dir12",
        "in_mca_pas3",
        "in_mca_sh7",
        "in_mca_chg1",
        "in_mca_inc20a",
        # SEBI listed-entity filings
        "in_shareholding_pattern",
        "in_corporate_governance_report",
        "in_brsr",
        "in_offer_document",
        # Professional opinions annexed to the annual report
        "in_statutory_auditor_report",
        "in_secretarial_audit_mr3",
        # RBI / FEMA and Government of India business registrations
        "in_fema_fcgpr",
        "in_iec_certificate",
        "in_udyam_certificate",
    }
    assert {s.doctype_id for s in INDIA_SPECS} == expected


def test_required_crosscountry_doctypes_are_all_present() -> None:
    assert {s.doctype_id for s in XX_SPECS} == {
        # Shape fallbacks — "we recognised the shape and not the issuer".
        "xx_utility_bill",
        "xx_bank_statement",
        "xx_passport_generic",
        "xx_photo_id_generic",
        "xx_unknown_form",
        # Globally-issued instruments — one global body issues or publishes each of
        # these, so no country pack can own them. Not fallbacks; see the crosscountry
        # module docstring for why the two kinds share a file.
        "xx_lei_certificate",
        "xx_fatca_crs_self_certification",
        "xx_wolfsberg_questionnaire",
        "xx_isda_master_agreement",
        "xx_sanctions_screening_report",
        "xx_ubo_declaration",
        "xx_audited_financial_statements",
        "xx_certificate_of_insurance",
        "xx_iso_certificate",
        "xx_duns_record",
    }


# ---------------------------------------------------------------------------
# The RBI Officially Valid Document set
# ---------------------------------------------------------------------------


def test_ovd_set_is_exactly_the_rbi_six() -> None:
    """officially_valid is a regulatory fact, not a tag.

    The RBI Master Direction lists six OVDs: passport, driving licence, proof of possession
    of Aadhaar, Voter's ID, NREGA job card and the NPR letter. Both Aadhaar variants count
    as proof of possession, which is why the flagged set has seven ids for six documents.
    """
    flagged = {s.doctype_id for s in INDIA_SPECS if s.officially_valid}
    assert flagged == IN_OVD_DOCTYPES
    assert flagged == {
        "in_passport",
        "in_driving_licence",
        "in_aadhaar",
        "in_aadhaar_masked",
        "in_voter_epic",
        "in_nrega_job_card",
        "in_npr_letter",
    }
    # A generic doctype has no regulatory standing anywhere, by construction.
    assert not any(s.officially_valid for s in XX_SPECS)


@pytest.mark.parametrize(
    "doctype_id",
    [
        "in_pan",
        "in_ration_card",
        "in_bank_passbook",
        "in_bank_statement",
        "in_utility_electricity",
        "in_utility_water",
        "in_utility_gas",
        "in_utility_telephone",
        "in_rent_agreement",
        "in_property_tax_receipt",
        "in_employer_allotment_letter",
        "in_pension_payment_order",
        "in_ckyc_record",
    ],
)
def test_common_non_ovds_are_not_flagged(doctype_id: str) -> None:
    """The documents most often mistaken for OVDs must not be flagged as such.

    PAN and the ration card are the two classic errors: PAN was never an OVD, and the
    ration card was withdrawn from the list. Utility bills, registered leases, employer
    allotment letters and PPOs are 'deemed OVDs' for a narrow address-update purpose only.
    """
    spec = get(doctype_id)
    assert spec is not None
    assert spec.officially_valid is False


def test_deemed_ovd_nuance_is_recorded_not_lost() -> None:
    """A doctype that is only a *deemed* OVD must say so in its handling note."""
    for doctype_id in (
        "in_utility_electricity",
        "in_utility_water",
        "in_utility_gas",
        "in_utility_telephone",
        "in_rent_agreement",
        "in_employer_allotment_letter",
        "in_pension_payment_order",
    ):
        spec = get(doctype_id)
        assert spec is not None
        assert "Officially Valid Document" in spec.handling
        assert "deemed" in spec.handling.lower()


def test_ovds_carry_a_handling_note() -> None:
    for spec in OWNED_SPECS:
        if spec.officially_valid:
            assert spec.handling.strip(), f"{spec.doctype_id} is an OVD with no handling note"


# ---------------------------------------------------------------------------
# Aadhaar: the UIDAI masking obligation
# ---------------------------------------------------------------------------


def test_aadhaar_carries_the_masking_handling_note() -> None:
    for doctype_id in ("in_aadhaar", "in_aadhaar_masked"):
        spec = get(doctype_id)
        assert spec is not None
        handling = spec.handling
        assert "MASKING" in handling.upper()
        assert "last four digits" in handling
        assert "2016" in handling, "the obligation must cite the instrument it comes from"


def test_aadhaar_number_is_pii_and_verhoeff_checked() -> None:
    """The full number must be pii and must carry the real checksum validator."""
    spec = get("in_aadhaar")
    assert spec is not None
    field = next(f for f in spec.fields if f.name == "aadhaar_number")
    assert field.pii is True
    assert field.attribute_key == "id.aadhaar"
    assert field.validator == "verhoeff_aadhaar"
    assert field.required is True


def test_masked_aadhaar_exposes_only_the_last_four() -> None:
    """The masked variant must not claim an id.aadhaar — only id.aadhaar_last4."""
    spec = get("in_aadhaar_masked")
    assert spec is not None
    keys = {f.attribute_key for f in spec.fields}
    assert "id.aadhaar_last4" in keys
    assert "id.aadhaar" not in keys, "a masked download cannot yield a full Aadhaar number"
    last4 = next(f for f in spec.fields if f.name == "aadhaar_last4")
    assert last4.pii is True
    # The masked string itself is kept for display parity but must not merge into the
    # identity view as though it were an identifier.
    masked = next(f for f in spec.fields if f.name == "masked_aadhaar_number")
    assert masked.attribute_key == ""


def test_masked_aadhaar_pattern_matches_a_real_masked_number() -> None:
    spec = get("in_aadhaar_masked")
    assert spec is not None
    field = next(f for f in spec.fields if f.name == "aadhaar_last4")
    assert field.pattern is not None
    for printed in ("XXXX XXXX 9012", "XXXXXXXX 9012", "xxxx xxxx 9012"):
        match = re.search(field.pattern, printed)
        assert match is not None, printed
        assert match.group(1) == "9012"


def test_every_aadhaar_number_field_anywhere_is_pii_and_checksummed() -> None:
    """Form 60 and the CKYC record also quote an Aadhaar number.

    A masking obligation that only holds on the Aadhaar card itself is not an obligation.
    """
    seen = 0
    for spec in all_specs():
        for field in spec.fields:
            if field.attribute_key == "id.aadhaar":
                seen += 1
                assert field.pii is True, f"{spec.doctype_id}.{field.name}"
                assert field.validator == "verhoeff_aadhaar", f"{spec.doctype_id}.{field.name}"
    assert seen >= 3, "expected id.aadhaar on the card, Form 60 and the CKYC record"


# ---------------------------------------------------------------------------
# Devanagari survives normalisation
# ---------------------------------------------------------------------------

#: Decisive Devanagari headers whose tokens must survive intact.
_DEVANAGARI_HEADERS = [
    ("भारतीय विशिष्ट पहचान प्राधिकरण", 4),  # UIDAI
    ("भारत निर्वाचन आयोग", 3),  # Election Commission of India
    ("निर्वाचक फोटो पहचान पत्र", 4),  # Elector Photo Identity Card
    ("आयकर विभाग", 2),  # Income Tax Department
    ("जन्म प्रमाण पत्र", 3),  # Birth certificate
    ("राशन कार्ड", 2),  # Ration card
    ("आधार", 1),  # Aadhaar
]


@pytest.mark.parametrize(("text", "word_count"), _DEVANAGARI_HEADERS)
def test_devanagari_anchors_survive_normalisation(text: str, word_count: int) -> None:
    """Devanagari anchors must come out of ``dce.normalize`` whole.

    This is the load-bearing property for every bilingual anchor in the pack: on a poor
    scan the Devanagari header often survives when the English one does not, so if
    normalisation shreds it the pack loses half its evidence silently.
    """
    tokens = tokenize_unicode(text)
    assert len(tokens) == word_count, f"{text!r} tokenised to {tokens}"
    assert "".join(tokens) == text.replace(" ", "")
    for token in tokens:
        assert token, "empty token"
        # A stray combining mark on its own is the signature of a shredded word.
        assert unicodedata.category(token[0]) not in {"Mn", "Mc"}, token


@pytest.mark.parametrize(("text", "_word_count"), _DEVANAGARI_HEADERS)
def test_devanagari_keeps_its_vowel_marks_through_fold_and_skeleton(
    text: str, _word_count: int
) -> None:
    """Diacritic stripping must not touch Devanagari.

    In Devanagari a combining mark is a vowel, not an accent: stripping ``ा`` from
    ``आधार`` changes the word. Both the conservative fold and the aggressive skeleton must
    leave the string alone.
    """
    assert fold(text) == unicodedata.normalize("NFKC", text).casefold()
    assert skeletonize(text) == fold(text)


def test_all_devanagari_anchors_in_the_pack_round_trip() -> None:
    """Every Hindi anchor actually shipped, not just the sampled headers."""
    checked = 0
    for spec in all_specs():
        for anchor in spec.anchors:
            if anchor.lang != "hi":
                continue
            checked += 1
            tokens = tokenize_unicode(anchor.text)
            assert tokens, f"{spec.doctype_id}: {anchor.text!r} tokenised to nothing"
            joined = "".join(tokens)
            stripped = re.sub(r"[\s,.\-/]", "", anchor.text)
            assert joined == stripped, f"{spec.doctype_id}: {anchor.text!r} -> {tokens}"
    assert checked >= 60, f"expected a substantial Devanagari surface, found {checked}"


def test_bilingual_government_doctypes_declare_hindi_anchors() -> None:
    """The bilingual documents must actually carry their Devanagari headers."""
    for doctype_id in (
        "in_aadhaar",
        "in_aadhaar_masked",
        "in_pan",
        "in_passport",
        "in_voter_epic",
        "in_driving_licence",
        "in_nrega_job_card",
        "in_npr_letter",
        "in_ration_card",
        "in_birth_certificate",
        "in_marriage_certificate",
        "in_caste_certificate",
    ):
        spec = get(doctype_id)
        assert spec is not None
        assert spec.anchor_texts("hi"), f"{doctype_id} has no Devanagari anchors"


def test_contract_tokenizer_keeps_devanagari_intact() -> None:
    """Both tokenizers must keep Indic words whole.

    This replaces an earlier guard that asserted the two DISAGREED, written when
    ``dce.models.tokenize`` was ``[^\\W_]+`` — which splits at every combining mark
    (Unicode categories Mn/Mc), so ``"आधार"`` became ``["आध", "र"]``. With 123 Devanagari
    anchors in this pack that silently broke classification for every bilingual Indian
    document. ``tokenize`` now treats marks as word continuation; this asserts the fixed
    behaviour and that the two implementations agree.
    """
    from dce.models import tokenize

    for word in ("आधार", "आयकर", "निर्वाचन", "पहचान"):
        assert tokenize(word) == [word], f"{word} was split by dce.models.tokenize"
        assert list(tokenize_unicode(word)) == [word]

    assert tokenize("भारत निर्वाचन आयोग") == ["भारत", "निर्वाचन", "आयोग"]
    # ...and the Latin substring false-positive class stays fixed.
    assert "dl" not in tokenize("in the middle of")
    assert "ein" not in tokenize("being a person")
    assert "sin" not in tokenize("using the form")


# ---------------------------------------------------------------------------
# Separability: PAN vs Aadhaar, and decisive anchors generally
# ---------------------------------------------------------------------------


def _decisive(spec: DocTypeSpec) -> set[str]:
    return {a.text.casefold() for a in spec.anchors if a.decisive}


def test_pan_and_aadhaar_are_separable_by_decisive_anchors() -> None:
    """The two most-collected Indian documents must not overlap at L1."""
    pan = get("in_pan")
    aadhaar = get("in_aadhaar")
    assert pan is not None and aadhaar is not None

    pan_decisive = _decisive(pan)
    aadhaar_decisive = _decisive(aadhaar)
    assert pan_decisive and aadhaar_decisive
    assert pan_decisive.isdisjoint(aadhaar_decisive)

    assert "unique identification authority of india" in aadhaar_decisive
    assert "भारतीय विशिष्ट पहचान प्राधिकरण".casefold() in aadhaar_decisive
    # PAN is separated by the name of the *document*, never by the name of its issuer. This
    # used to assert "income tax department" / "आयकर विभाग" were decisive for in_pan, and they
    # were — wrongly. The Income Tax Department issues four doctypes in this registry (in_pan,
    # in_form16, in_form60, in_itr_acknowledgement) and heads all four with its own name, so
    # the string proves the issuer and says nothing about which of its documents this is.
    # in_form16 already declared "आयकर विभाग" as a non-decisive anchor, which is exactly the
    # decisive/non-decisive asymmetry ``loader._check_decisive_asymmetry`` now refuses at
    # import time.
    assert "permanent account number card" in pan_decisive
    assert "स्थायी लेखा संख्या कार्ड".casefold() in pan_decisive
    assert "income tax department" not in pan_decisive
    assert "आयकर विभाग".casefold() not in pan_decisive


def test_shared_government_furniture_is_never_decisive() -> None:
    """``GOVERNMENT OF INDIA`` / ``भारत सरकार`` is on everything and separates nothing."""
    furniture = {"government of india", "भारत सरकार".casefold(), "govt. of india"}
    for spec in all_specs():
        assert _decisive(spec).isdisjoint(furniture), spec.doctype_id


def test_no_decisive_anchor_is_shared_by_two_doctypes_at_all() -> None:
    """No decisive anchor is claimed twice. The set is empty, and it used to have two members.

    This test used to assert that a shared decisive anchor is *allowed* when both doctypes
    declare each other — pinning ``MINISTRY OF CORPORATE AFFAIRS`` and its Hindi twin as
    "the legitimate, declared, same-issuer case". Measurement against the corpus retired that
    idea. The English string is printed on ``in_brsr`` and ``in_statutory_auditor_report``
    documents as well, because the MCA heads every filing it receives rather than one of
    them; declaring a two-doctype family therefore described the wrong relationship, and
    ``confusable_with`` cannot make a false claim true (``us_green_card`` and ``ca_pr_card``
    declared each other in both directions and a Canadian PR card was still classified
    ``us_green_card``).

    So the string is now an ``ISSUER_NAME`` held by nobody decisively, and
    :func:`dce.registry.loader._check_issuer_name_not_shared` enforces exactly the rule
    ``_check_decisive_asymmetry``'s own docstring had always stated and never applied: an
    issuer name that heads several doctypes proves the issuer, not the document. Both MCA
    doctypes keep their form numbers, statutes and identifier schemes; neither keeps the
    letterhead.

    ``_check_decisive_collisions`` is still the general rule and still permits a declared
    family — a masked and an unmasked Aadhaar really do share the UIDAI header. This asserts
    that no pack is currently relying on that permission.
    """
    owners: dict[str, list[str]] = {}
    for spec in all_specs():
        for text in _decisive(spec):
            owners.setdefault(text, []).append(spec.doctype_id)
    collisions = {t: sorted(ids) for t, ids in owners.items() if len(ids) > 1}
    assert collisions == {}


def test_crosscountry_anchors_are_never_decisive() -> None:
    """A generic doctype must never be able to draw with a country-specific one."""
    for spec in XX_SPECS:
        assert not _decisive(spec), f"{spec.doctype_id} has decisive anchors"


def test_crosscountry_specs_name_the_country_doctypes_they_lose_to() -> None:
    for spec in XX_SPECS:
        if spec.doctype_id == "xx_unknown_form":
            continue  # the terminal fallback has nothing more specific to defer to
        assert spec.confusable_with, f"{spec.doctype_id} names no country-specific rival"
        for other in spec.confusable_with:
            assert get(other) is not None


def test_confusable_with_targets_all_exist_and_are_not_self() -> None:
    for spec in all_specs():
        for other, discriminator in spec.confusable_with.items():
            assert other != spec.doctype_id
            assert get(other) is not None, f"{spec.doctype_id} -> {other}"
            assert discriminator.strip(), f"{spec.doctype_id} -> {other} has no discriminator"


def test_negative_anchors_do_not_contradict_own_anchors() -> None:
    """A doctype must not argue against a term it also claims as evidence."""
    for spec in all_specs():
        own = {a.text.casefold() for a in spec.anchors}
        for negative in spec.negative_anchors:
            assert negative.casefold() not in own, f"{spec.doctype_id}: {negative!r}"


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def test_every_field_validator_exists_in_dce_extract_validate() -> None:
    """The contracted check: no FieldSpec may name a validator that does not exist.

    A field naming a missing validator is a field that silently never validates, which in
    a KYC gate means unverified data presented as verified.
    """
    implemented = set(validate_module.VALIDATORS)
    missing = sorted(required_validators() - implemented)
    assert missing == [], f"validators referenced but not implemented: {missing}"


def test_every_referenced_validator_is_documented_in_the_contract() -> None:
    for name in required_validators():
        assert name in VALIDATOR_CONTRACT
        assert VALIDATOR_CONTRACT[name].strip()


def test_pending_validators_are_still_pending() -> None:
    """The gap list must not go stale.

    :data:`PENDING_VALIDATORS` names validators the registry needs and
    ``dce.extract.validate`` does not implement yet; the affected fields fall back to
    their ``pattern``. This test fails the moment one is implemented — at which point it
    should be wired into the FieldSpecs that need it and moved into VALIDATOR_CONTRACT,
    not silenced.
    """
    implemented = set(validate_module.VALIDATORS)
    landed = sorted(set(PENDING_VALIDATORS) & implemented)
    assert landed == [], (
        f"these validators have landed and should now be wired into the FieldSpecs "
        f"that need them: {landed}"
    )
    assert set(PENDING_VALIDATORS).isdisjoint(VALIDATOR_CONTRACT)


def test_fields_without_a_validator_still_constrain_their_value() -> None:
    """An identifier field with neither validator nor pattern accepts anything.

    Free-text fields (names, addresses, prose) are exempt — they are constrained by the
    generic ``name``/``address`` validators or are genuinely unconstrained.
    """
    for spec in OWNED_SPECS:
        for field in spec.fields:
            if field.type != "id":
                continue
            if field.validator or field.pattern:
                continue
            # An unconstrained identifier is only acceptable when the pack explicitly
            # explains that no format exists to enforce.
            assert field.notes.strip(), (
                f"{spec.doctype_id}.{field.name} is an id with no validator, no pattern "
                "and no note explaining why"
            )


def test_no_invented_checksums_on_unchecksummed_identifiers() -> None:
    """Identifiers with no published check digit must say so rather than imply one."""
    for name in ("pan", "in_dl", "in_passport"):
        assert name in VALIDATOR_CONTRACT
        text = VALIDATOR_CONTRACT[name].lower()
        assert "no" in text and ("check" in text or "checksum" in text)


# ---------------------------------------------------------------------------
# Field-level hygiene
# ---------------------------------------------------------------------------


def test_all_attribute_keys_are_in_the_catalog() -> None:
    for spec in all_specs():
        for field in spec.fields:
            if field.attribute_key:
                assert field.attribute_key in ATTRIBUTE_KEYS, (
                    f"{spec.doctype_id}.{field.name} -> {field.attribute_key}"
                )


def test_all_field_types_and_locators_are_known() -> None:
    for spec in all_specs():
        for field in spec.fields:
            assert field.type in KNOWN_FIELD_TYPES
            assert field.locators
            assert set(field.locators) <= KNOWN_LOCATORS


def test_every_spec_has_anchors_a_label_and_an_issuing_authority() -> None:
    for spec in OWNED_SPECS:
        assert spec.anchors, spec.doctype_id
        assert spec.label.strip(), spec.doctype_id
        assert spec.category in set(Category)
        if spec.doctype_id != "xx_unknown_form":
            assert spec.issuing_authority.strip(), spec.doctype_id


def test_india_specs_are_all_country_in() -> None:
    for spec in INDIA_SPECS:
        assert spec.country == "IN"
        assert spec.doctype_id.startswith("in_")


def test_pii_is_set_on_identifier_and_name_fields() -> None:
    """Anything that identifies a natural person must be flagged for masking."""
    person_keys = {
        "identity.full_name",
        "identity.given_names",
        "identity.surname",
        "identity.father_name",
        "identity.mother_name",
        "identity.spouse_name",
        "identity.guardian_name",
        "identity.date_of_birth",
        "identity.year_of_birth",
        "id.aadhaar",
        "id.aadhaar_last4",
        "id.aadhaar_vid",
        "id.pan",
        "id.passport_number",
        "id.voter_epic",
        "id.driving_licence",
        "id.ckyc_kin",
        "id.ration_card",
        "id.nrega_job_card",
        "id.uan",
        "id.ppo_number",
        "account.number",
        "address.residential",
    }
    for spec in all_specs():
        for field in spec.fields:
            if field.attribute_key in person_keys:
                assert field.pii is True, f"{spec.doctype_id}.{field.name} is not pii"


def test_every_spec_declares_at_least_one_required_field() -> None:
    """A doctype with no required field can never report missing_required."""
    for spec in OWNED_SPECS:
        if spec.doctype_id in {"xx_unknown_form", "xx_photo_id_generic"}:
            continue  # triage landing spots; nothing is guaranteed present
        assert any(f.required for f in spec.fields), spec.doctype_id


def test_id_patterns_compile_and_the_headline_ones_match() -> None:
    samples = {
        "in_pan": "ABCPD1234E",
        "in_gst_certificate": "27ABCPD1234E1Z5",
        "in_certificate_incorporation": "U72900MH2015PTC123456",
        "in_llp_incorporation": "AAB-1234",
        "in_voter_epic": "ABC1234567",
        "in_bank_passbook": "HDFC0001234",
    }
    for doctype_id, sample in samples.items():
        spec = get(doctype_id)
        assert spec is not None
        assert spec.id_patterns, doctype_id
        assert any(re.search(p, sample) for p in spec.id_patterns), (doctype_id, sample)


def test_aadhaar_id_pattern_rejects_a_leading_zero_or_one() -> None:
    """Aadhaar numbers never begin 0 or 1 — the pattern must encode that."""
    spec = get("in_aadhaar")
    assert spec is not None
    pattern = spec.id_patterns[0]
    assert re.search(pattern, "2345 6789 0123")
    assert not re.search(pattern, "1234 5678 9012")
    assert not re.search(pattern, "0234 5678 9012")


# ---------------------------------------------------------------------------
# The loader rejects malformed packs
# ---------------------------------------------------------------------------


def _minimal(**overrides: object) -> DocTypeSpec:
    base: dict[str, object] = {
        "doctype_id": "in_test_only",
        "label": "Test",
        "country": "IN",
        "anchors": [Anchor(text="A TEST ANCHOR HEADER")],
    }
    base.update(overrides)
    return DocTypeSpec(**base)  # type: ignore[arg-type]


def test_loader_rejects_a_decisive_anchor_that_does_not_say_what_makes_it_decisive() -> None:
    """The hole the ``controls`` field closes: an unjustified claim now cannot be registered.

    Before this, declaring ``OMB No. 1545-0074`` decisive and declaring ``BIRTH CERTIFICATE``
    decisive were the same keystroke, and neither cross-spec check could tell them apart —
    both compare the registry against itself, so a *lone* false claim has no second claimant
    to collide with and is invisible by construction. Whoever wrote ``BIRTH CERTIFICATE``
    would now have to put something in this field, and there is nothing honest to put but
    :attr:`~dce.models.Controls.CLASS_NAME_UNCONTESTED`, which is counted and reported as
    weak and held to a stricter uniqueness rule.
    """
    from dce.registry.loader import _validate_spec

    spec = _minimal(anchors=[Anchor(text="A DECISIVE HEADER", decisive=True)])
    with pytest.raises(RegistryError, match="does not say what makes it decisive"):
        _validate_spec(spec)


def test_loader_rejects_controls_on_an_anchor_that_makes_no_decisive_claim() -> None:
    """The other half, and it is what keeps the weak tier countable.

    ``controls`` justifies a decisive claim. On a non-decisive anchor it is decoration, and
    decoration is exactly what would stop ``grep -c class_name_uncontested`` from being an
    honest count of the registry's known-weak claims.
    """
    from dce.registry.loader import _validate_spec

    spec = _minimal(
        anchors=[Anchor(text="A LEXICAL HEADER", controls=Controls.CLASS_NAME_UNCONTESTED)]
    )
    with pytest.raises(RegistryError, match="is not decisive but declares controls"):
        _validate_spec(spec)


def test_loader_rejects_a_short_decisive_anchor_with_no_zone() -> None:
    from dce.registry.loader import register

    spec = _minimal(
        anchors=[Anchor(text="DL", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED)]
    )
    with pytest.raises(RegistryError, match="single short word"):
        register(spec)


def test_loader_accepts_a_short_decisive_anchor_pinned_to_a_zone() -> None:
    """``आधार`` in the title zone really is an issuing-authority header."""
    from dce.registry.loader import _validate_spec

    _validate_spec(
        _minimal(
            anchors=[
                Anchor(
                    text="आधार",
                    lang="hi",
                    decisive=True,
                    controls=Controls.ISSUER_NAME,
                    zone=Zone.title,
                )
            ]
        )
    )


def test_loader_rejects_a_decisive_anchor_on_a_crosscountry_spec() -> None:
    from dce.registry.loader import _validate_spec

    spec = _minimal(
        doctype_id="xx_test_only",
        country="XX",
        anchors=[
            Anchor(
                text="A GENERIC HEADER",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            )
        ],
    )
    with pytest.raises(RegistryError, match="must not carry decisive anchors"):
        _validate_spec(spec)


def test_loader_rejects_an_unknown_attribute_key() -> None:
    from dce.models import FieldSpec
    from dce.registry.loader import _validate_spec

    spec = _minimal(fields=[FieldSpec(name="x", attribute_key="id.not_a_real_key")])
    with pytest.raises(RegistryError, match="not in ATTRIBUTE_KEYS"):
        _validate_spec(spec)


def test_loader_rejects_an_undeclared_validator() -> None:
    from dce.models import FieldSpec
    from dce.registry.loader import _validate_spec

    spec = _minimal(fields=[FieldSpec(name="x", validator="not_a_validator")])
    with pytest.raises(RegistryError, match="VALIDATOR_CONTRACT"):
        _validate_spec(spec)


def test_loader_rejects_an_unknown_locator() -> None:
    from dce.models import FieldSpec
    from dce.registry.loader import _validate_spec

    spec = _minimal(fields=[FieldSpec(name="x", locators=["telepathy"])])
    with pytest.raises(RegistryError, match="not implemented"):
        _validate_spec(spec)


def test_loader_rejects_a_country_id_mismatch() -> None:
    from dce.registry.loader import _validate_spec

    with pytest.raises(RegistryError, match="prefix does not match country"):
        _validate_spec(_minimal(doctype_id="us_test_only"))


def test_loader_rejects_a_duplicate_registration() -> None:
    from dce.registry.loader import register

    spec = get("in_pan")
    assert spec is not None
    clone = spec.model_copy(update={"label": "A different label"})
    with pytest.raises(RegistryError, match="duplicate doctype_id"):
        register(clone)


def test_loader_rejects_a_non_nfc_anchor() -> None:
    """Anchors are stored NFC so a decomposed OCR read and the pack agree."""
    from dce.registry.loader import _validate_spec

    decomposed = unicodedata.normalize("NFD", "SITUACIÓN FISCAL HEADER")
    with pytest.raises(RegistryError, match="NFC"):
        _validate_spec(_minimal(anchors=[Anchor(text=decomposed)]))


# ---------------------------------------------------------------------------
# End-to-end: does the pack actually discriminate through the live cascade?
#
# These assert RANKING, not acceptance. The split is the ownership boundary: this pack owns
# "the right doctype scores highest against a realistic OCR dump", and dce.classify owns
# "is that score high enough to accept". Pinning the accept threshold here would make the
# registry's tests fail whenever the cascade is recalibrated, which is not this module's
# business — and would hide the thing that is: an anchor set that stopped discriminating.
# ---------------------------------------------------------------------------
_SYNTHETIC = {
    "in_aadhaar": [
        ("भारत सरकार", Zone.heading),
        ("GOVERNMENT OF INDIA", Zone.heading),
        ("भारतीय विशिष्ट पहचान प्राधिकरण", Zone.heading),
        ("UNIQUE IDENTIFICATION AUTHORITY OF INDIA", Zone.heading),
        ("आधार", Zone.title),
        ("AADHAAR", Zone.title),
        ("Ramesh Kumar", Zone.body),
        ("जन्म तिथि / DOB: 14/07/1986", Zone.body),
        ("पुरुष / MALE", Zone.body),
        ("2345 6789 0124", Zone.body),  # Verhoeff-valid
        ("मेरा आधार, मेरी पहचान", Zone.furniture),
    ],
    "in_pan": [
        ("INCOME TAX DEPARTMENT", Zone.title),
        ("आयकर विभाग", Zone.title),
        ("भारत सरकार  GOVT. OF INDIA", Zone.heading),
        ("PERMANENT ACCOUNT NUMBER CARD", Zone.heading),
        ("ABCPD1234E", Zone.body),
        ("नाम / Name: RAMESH KUMAR", Zone.body),
        ("पिता का नाम / Father's Name: SURESH KUMAR", Zone.body),
        ("जन्म की तारीख / Date of Birth: 14/07/1986", Zone.body),
    ],
    "in_voter_epic": [
        ("ELECTION COMMISSION OF INDIA", Zone.title),
        ("भारत निर्वाचन आयोग", Zone.title),
        ("ELECTOR PHOTO IDENTITY CARD", Zone.heading),
        ("निर्वाचक फोटो पहचान पत्र", Zone.heading),
        ("ABC1234567", Zone.body),
        ("Elector's Name: Ramesh Kumar", Zone.body),
        ("Assembly Constituency: 145", Zone.body),
    ],
    "in_driving_licence": [
        ("DRIVING LICENCE", Zone.title),
        ("THE UNION OF INDIA", Zone.heading),
        ("भारत संघ", Zone.heading),
        ("TRANSPORT DEPARTMENT", Zone.heading),
        ("AUTHORISATION TO DRIVE FOLLOWING CLASS OF VEHICLES", Zone.heading),
        ("DL No: MH12 20110012345", Zone.body),
        ("Valid Till: 13/07/2031", Zone.body),
        ("COV: LMV  MCWG", Zone.table),
    ],
    "in_gst_certificate": [
        ("Government of India", Zone.heading),
        ("FORM GST REG-06", Zone.title),
        ("Registration Certificate", Zone.title),
        ("GOODS AND SERVICES TAX IDENTIFICATION NUMBER", Zone.heading),
        ("27ABCPD1234E1ZE", Zone.body),  # mod-36 valid
        ("Legal Name: ACME TRADING PRIVATE LIMITED", Zone.body),
        ("Constitution of Business: Private Limited Company", Zone.body),
        ("Centre Jurisdiction: MUMBAI SOUTH", Zone.body),
    ],
    "in_utility_electricity": [
        ("MSEDCL", Zone.heading),
        ("ELECTRICITY BILL", Zone.title),
        ("Consumer Number: 180012345678", Zone.body),
        ("Consumer Name: Ramesh Kumar", Zone.body),
        ("Units Consumed: 245", Zone.table),
        ("Energy Charges: 1,842.00", Zone.table),
        ("Sanctioned Load: 3 kW", Zone.body),
        ("Due Date: 20/02/2024", Zone.body),
    ],
    "in_form16": [
        ("FORM NO. 16", Zone.title),
        (
            "Certificate under Section 203 of the Income-tax Act, 1961 for tax deducted at "
            "source on salary",
            Zone.heading,
        ),
        ("PART A", Zone.heading),
        ("TRACES", Zone.furniture),
        ("PAN of the Deductee: ABCPD1234E", Zone.body),
        ("TAN of the Deductor: MUMA12345B", Zone.body),
        ("Assessment Year: 2024-25", Zone.body),
        ("Gross Salary: 12,45,000.00", Zone.table),
    ],
    "in_bank_statement": [
        ("HDFC BANK", Zone.heading),
        ("STATEMENT OF ACCOUNT", Zone.title),
        ("Account Number: 50100123456789", Zone.body),
        ("IFSC: HDFC0001234", Zone.body),
        ("Statement Period: 01/01/2024 to 31/01/2024", Zone.body),
        ("Opening Balance: 45,120.00", Zone.table),
        ("Closing Balance: 1,02,340.00", Zone.table),
        ("This is a computer generated statement", Zone.furniture),
    ],
    "in_certificate_incorporation": [
        ("MINISTRY OF CORPORATE AFFAIRS", Zone.heading),
        ("कॉर्पोरेट कार्य मंत्रालय", Zone.heading),
        ("CERTIFICATE OF INCORPORATION", Zone.title),
        ("Registrar of Companies", Zone.heading),
        ("Corporate Identity Number: U72900MH2015PTC123456", Zone.body),
        ("Name of Company: ACME TRADING PRIVATE LIMITED", Zone.body),
        ("Date of Incorporation: 12/03/2015", Zone.body),
        ("Companies Act, 2013", Zone.body),
    ],
}


def _layout(blocks: list[tuple[str, Zone]]) -> LayoutView:
    return LayoutView(
        doc_id="synthetic",
        pages=[PageInfo(page=1, width=1000, height=650)],
        blocks=[TextBlock(text=t, zone=z, page=1) for t, z in blocks],
    )


def _top_ranked(result) -> str:
    """The highest-scoring doctype, whether or not the cascade accepted it."""
    if not result.abstained:
        return result.doctype_id
    return result.runners_up[0][0] if result.runners_up else UNKNOWN


@pytest.mark.parametrize("doctype_id", sorted(_SYNTHETIC))
def test_synthetic_document_ranks_its_own_doctype_first(doctype_id: str) -> None:
    """A realistic OCR dump must rank its own doctype above all 40 others."""
    from dce.classify import classify

    result = classify(_layout(_SYNTHETIC[doctype_id]))
    assert _top_ranked(result) == doctype_id, (
        f"{doctype_id} did not rank first; got {_top_ranked(result)} "
        f"(runners: {result.runners_up[:3]})"
    )


def test_masked_aadhaar_narrows_to_the_aadhaar_pair_and_never_guesses() -> None:
    """The masked/full Aadhaar pair, now separated — tightened exactly as this test asked.

    It used to read: *"a masked e-Aadhaar cannot currently be separated from a full one … the
    only real discriminator — a Verhoeff-valid 12-digit number being present or absent — is
    neutralised because ``dce.classify.anchors`` saturates both at its confidence ceiling
    before fusion … If the ceiling stops saturating, this test should be tightened to assert
    ``in_aadhaar_masked`` outright."*

    The ceiling has stopped **deciding**, which is the same condition. The accept rule now
    compares candidates in evidence bits (:func:`dce.classify.cascade.evidence_bits`) — the
    unclipped quantity the 0.97 score is a squash of — so two doctypes that both saturate the
    score are no longer equal to the rule. The ceiling itself is unchanged and still caps what
    L1 may claim on its own; what changed is that a *comparison* is no longer taken on it.

    So: the document says "Masked Aadhaar" and prints a redacted number, and the service now
    says so instead of sending a legible document to a human because of a clipping artefact.
    The pair is still asserted to be the top two, which is the part that was always the point.
    """
    from dce.classify import classify

    result = classify(
        _layout(
            [
                ("भारत सरकार / GOVERNMENT OF INDIA", Zone.heading),
                ("भारतीय विशिष्ट पहचान प्राधिकरण", Zone.heading),
                ("UNIQUE IDENTIFICATION AUTHORITY OF INDIA", Zone.heading),
                ("आधार", Zone.title),
                ("AADHAAR", Zone.title),
                ("Masked Aadhaar", Zone.heading),
                ("Ramesh Kumar", Zone.body),
                ("XXXX XXXX 0124", Zone.body),
                ("VID: 9012 3456 7890 1234", Zone.body),
            ]
        )
    )
    assert result.abstained is False
    assert result.doctype_id == "in_aadhaar_masked"
    top_two = {result.doctype_id, *(d for d, _ in result.runners_up[:1])}
    assert top_two == {"in_aadhaar", "in_aadhaar_masked"}


def test_aadhaar_pattern_does_not_fire_inside_a_virtual_id() -> None:
    """A 16-digit VID contains a 12-digit prefix that used to read as an Aadhaar number.

    Left unguarded this hands the extractor a wrong value with a one-in-ten chance of
    passing Verhoeff by coincidence — a wrong Aadhaar number presented as checksum-verified.
    """
    from dce.classify.anchors import checksum_sweep

    spec = get("in_aadhaar")
    assert spec is not None
    hits = checksum_sweep("VID: 9012 3456 7890 1234", [spec])
    assert hits.get("in_aadhaar", ()) == ()

    verified = checksum_sweep("AADHAAR 2345 6789 0124", [spec])["in_aadhaar"]
    assert [h.verified for h in verified] == [True]
