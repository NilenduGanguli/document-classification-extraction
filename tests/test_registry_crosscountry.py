"""Offline tests for the cross-country doctype pack, and for the loader's own refusals.

Pure and offline: importing :mod:`dce.registry` runs the whole registry validation, so most
of these tests are assertions about data that has already been structurally checked at
import. What is tested here is the *knowledge* — that the ``XX`` pack stays a fallback and
never a competitor — plus the loader-mechanism refusals, which have no country of their own
and have to live somewhere.

This module is the surviving half of ``tests/test_registry_india.py``. That file owned two
packs, ``INDIA_SPECS + XX_SPECS``; the India pack was removed from the registry (it lives on
the ``archive/india-doctypes`` branch) and its assertions went with it. Everything here is
either ``XX``-scoped or registry-wide, and nothing in it asserts anything about a doctype
that no longer exists.
"""

from __future__ import annotations

import unicodedata

import pytest

import dce.extract.validate as validate_module
from dce.models import (
    Anchor,
    Category,
    Controls,
    DocTypeSpec,
    Zone,
)
from dce.registry import (
    ATTRIBUTE_KEYS,
    KNOWN_FIELD_TYPES,
    KNOWN_LOCATORS,
    VALIDATOR_CONTRACT,
    RegistryError,
    all_specs,
    by_country,
    get,
    required_validators,
    validate_registry,
)
from dce.registry.crosscountry import SPECS as XX_SPECS

#: The pack this module owns. Other country packs register into the same table, and a
#: hygiene rule that is right for the cross-country pack is not automatically right for
#: theirs — an assertion swept over ``all_specs()`` would fail *their* build for *our*
#: opinion. Rules that are genuinely registry-wide (attribute keys resolve, decisive anchors
#: do not collide, confusable_with targets exist) stay global on purpose.
OWNED_SPECS: tuple[DocTypeSpec, ...] = tuple(XX_SPECS)

# ---------------------------------------------------------------------------
# Registry-wide invariants
# ---------------------------------------------------------------------------


def test_validate_registry_passes() -> None:
    """The whole registry validates. Re-run explicitly so a failure names this test."""
    validate_registry()


def test_pack_sizes() -> None:
    """129 doctypes across four packs, 15 of them cross-country, with no id collisions."""
    assert len(XX_SPECS) == 15
    grouped = by_country()
    assert {k: len(v) for k, v in sorted(grouped.items())} == {
        "CA": 37,
        "MX": 27,
        "US": 50,
        "XX": 15,
    }
    assert len(all_specs()) == 129
    ids = [s.doctype_id for s in all_specs()]
    assert len(ids) == len(set(ids))


def test_no_doctype_is_registered_for_a_removed_pack() -> None:
    """The India pack was removed, not disabled. Nothing may re-register an ``in_`` id.

    A feature-flagged pack would satisfy every other test in this file and still ship the
    52 doctypes the owner asked to have gone, so the absence is asserted directly.
    """
    assert "IN" not in by_country()
    stragglers = sorted(s.doctype_id for s in all_specs() if s.doctype_id.startswith("in_"))
    assert stragglers == []


def test_every_spec_is_reachable_by_id() -> None:
    for spec in all_specs():
        assert get(spec.doctype_id) is spec


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


def test_crosscountry_specs_are_all_country_xx() -> None:
    for spec in XX_SPECS:
        assert spec.country == "XX"
        assert spec.doctype_id.startswith("xx_")


# ---------------------------------------------------------------------------
# Decisive anchors, and the generic's promise never to compete
# ---------------------------------------------------------------------------


def _decisive(spec: DocTypeSpec) -> set[str]:
    return {a.text.casefold() for a in spec.anchors if a.decisive}


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


def test_no_decisive_anchor_is_shared_by_two_doctypes_at_all() -> None:
    """No decisive anchor is claimed twice. The set is empty, and it used to have members.

    ``_check_decisive_collisions`` permits a *declared* family — two documents of one
    issuer that genuinely share its header, each declaring the other in ``confusable_with``
    and each keeping a decisive anchor of its own. This asserts that no pack is currently
    relying on that permission, which is the state that makes ``_check_issuer_name_not_shared``
    and ``_check_class_name_uncontested`` cheap to keep.
    """
    owners: dict[str, list[str]] = {}
    for spec in all_specs():
        for text in _decisive(spec):
            owners.setdefault(text, []).append(spec.doctype_id)
    collisions = {t: sorted(ids) for t, ids in owners.items() if len(ids) > 1}
    assert collisions == {}


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


def test_the_validator_contract_describes_nothing_that_is_gone() -> None:
    """A contract entry for a validator no pack references and no module implements is a lie.

    The India pack took six identifier validators with it (``verhoeff_aadhaar``, ``pan``,
    ``gstin``, ``epic_voter``, ``in_dl``, ``in_passport``). The contract is the registry's
    statement of what a named validator must enforce; an entry naming a function that no
    longer exists would have a FieldSpec pass ``_check_field`` and then fail
    ``_check_validators`` at import — or worse, never be noticed.
    """
    implemented = set(validate_module.VALIDATORS)
    orphans = sorted(set(VALIDATOR_CONTRACT) - implemented)
    assert orphans == [], f"contract describes validators that do not exist: {orphans}"


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
    for name in ("ssn", "ein"):
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
        "id.passport_number",
        "id.sin",
        "id.ssn",
        "id.itin",
        "id.curp",
        "id.driver_license",
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


# ---------------------------------------------------------------------------
# The loader rejects malformed packs
# ---------------------------------------------------------------------------


def _minimal(**overrides: object) -> DocTypeSpec:
    base: dict[str, object] = {
        "doctype_id": "us_test_only",
        "label": "Test",
        "country": "US",
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
    """A bare word in the *title* really can be an issuing-authority header."""
    from dce.registry.loader import _validate_spec

    _validate_spec(
        _minimal(
            anchors=[
                Anchor(
                    text="NEXUS",
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


def test_loader_rejects_a_validator_the_removed_india_pack_used_to_declare() -> None:
    """The India validators are gone from the contract, so naming one now fails at import.

    This is the removal's own tripwire: if a later change restores ``verhoeff_aadhaar`` or
    ``pan`` to :data:`VALIDATOR_CONTRACT` without a doctype that needs it, this test says so.
    """
    from dce.models import FieldSpec
    from dce.registry.loader import _validate_spec

    for gone in ("verhoeff_aadhaar", "pan", "gstin", "epic_voter", "in_dl", "in_passport"):
        assert gone not in VALIDATOR_CONTRACT
        assert gone not in validate_module.VALIDATORS
        with pytest.raises(RegistryError, match="VALIDATOR_CONTRACT"):
            _validate_spec(_minimal(fields=[FieldSpec(name="x", validator=gone)]))


def test_loader_rejects_an_unknown_locator() -> None:
    from dce.models import FieldSpec
    from dce.registry.loader import _validate_spec

    spec = _minimal(fields=[FieldSpec(name="x", locators=["telepathy"])])
    with pytest.raises(RegistryError, match="not implemented"):
        _validate_spec(spec)


def test_loader_rejects_a_country_id_mismatch() -> None:
    from dce.registry.loader import _validate_spec

    with pytest.raises(RegistryError, match="prefix does not match country"):
        _validate_spec(_minimal(doctype_id="ca_test_only"))


def test_loader_rejects_a_confusable_target_that_is_not_registered() -> None:
    """The check that caught the India removal's own leftovers.

    Deleting the pack left ``us_articles_incorporation`` and ``us_secretary_certificate``
    pointing at ``in_certificate_incorporation`` and ``in_board_resolution``. The import
    failed and named both, which is the behaviour this asserts.
    """
    from dce.registry import loader

    errors: list[str] = []
    original = dict(loader._REGISTRY)
    spec = original["us_w9"]
    try:
        loader._REGISTRY["us_w9"] = spec.model_copy(
            update={"confusable_with": {"in_pan": "a doctype that is not registered"}}
        )
        loader._check_confusable_targets(errors)
    finally:
        loader._REGISTRY.clear()
        loader._REGISTRY.update(original)
    assert len(errors) == 1
    assert "in_pan" in errors[0]
    assert "no such doctype is registered" in errors[0]


def test_loader_rejects_a_duplicate_registration() -> None:
    from dce.registry.loader import register

    spec = get("us_w9")
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
