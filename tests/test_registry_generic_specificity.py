"""The cross-country generics must stay weaker than the country packs they back up.

``dce.registry.crosscountry`` promises that "a country-specific doctype always outranks the
generic one when both fire". Forbidding the decisive flag on ``XX`` anchors does not deliver
that on its own, because a spec can win on the *number* of anchors it matches rather than on
their weight — ``xx_bank_statement`` used to declare five of ``us_bank_statement``'s seven
anchors plus fifteen more, so on a US statement the generic matched more of its own declared
vocabulary than the specific doctype matched of its own, and "issuer not modelled" won.

These tests pin the repaired invariant and the loader check that enforces it. They are pure
data assertions: no classifier is involved, because the property is supposed to hold of the
registry regardless of how the cascade scores it.
"""

from __future__ import annotations

import pytest

from dce.models import Anchor, Category, DocTypeSpec
from dce.registry import all_specs, crosscountry, loader

GENERICS = tuple(spec for spec in crosscountry.SPECS)
PACK_SPECS = tuple(spec for spec in all_specs() if spec.country != "XX")


def _folded(anchor: Anchor) -> str:
    return anchor.text.strip().casefold()


def test_the_packs_are_actually_present() -> None:
    """Guard the guard: these tests are vacuous if the country packs did not import."""
    assert len(GENERICS) >= 5
    assert len(PACK_SPECS) > 100


def test_no_generic_declares_a_string_a_country_pack_claims() -> None:
    """The rule itself, stated over the live registry rather than over the loader.

    The only permitted overlap is the noun that names the document class the generic is a
    fallback for — ``xx_passport_generic`` has to be able to say "passport". That carve-out
    is written out per doctype in :data:`dce.registry.loader._GENERIC_NAMING_ANCHORS` so
    that widening it is a visible decision.
    """
    claimed: dict[str, set[str]] = {}
    for spec in PACK_SPECS:
        for anchor in spec.anchors:
            claimed.setdefault(_folded(anchor), set()).add(spec.doctype_id)

    offenders: list[str] = []
    for spec in GENERICS:
        allowed = loader._GENERIC_NAMING_ANCHORS.get(spec.doctype_id, frozenset())
        for anchor in spec.anchors:
            text = _folded(anchor)
            if text in allowed:
                continue
            if text in claimed:
                offenders.append(f"{spec.doctype_id}:{anchor.text!r} <- {sorted(claimed[text])}")

    assert not offenders, (
        "a cross-country generic is repeating a country pack's vocabulary, which lets it "
        "compete with the pack on documents the pack should win:\n  " + "\n  ".join(offenders)
    )


def test_two_generics_do_not_claim_the_same_string() -> None:
    """``Statement`` and ``Account Number`` were on both the bank and the utility generic.

    A string shared between two generics cannot help choose between them, and both of them
    are already the bottom of the ranking — so it only adds floor score to whichever the
    softmax happens to favour.
    """
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for spec in GENERICS:
        for anchor in spec.anchors:
            text = _folded(anchor)
            if text in seen:
                clashes.append(f"{anchor.text!r}: {seen[text]} and {spec.doctype_id}")
            else:
                seen[text] = spec.doctype_id
    assert not clashes, "cross-country generics share anchors:\n  " + "\n  ".join(clashes)


def test_the_loader_rejects_a_greedy_generic() -> None:
    """The check has to actually bite, or it is documentation.

    Registers a country doctype that claims a string an existing generic also declares, and
    asserts ``validate_registry`` refuses the combination. The spec is removed again in the
    finally block so the module-level registry is left exactly as it was found.
    """
    stolen = "Debit"  # xx_bank_statement declares it; no pack does.
    assert any(
        _folded(a) == stolen.casefold()
        for spec in GENERICS
        if spec.doctype_id == "xx_bank_statement"
        for a in spec.anchors
    ), "fixture is stale: xx_bank_statement no longer declares 'Debit'"

    intruder = DocTypeSpec(
        doctype_id="zz_greedy_probe",
        label="Greedy probe",
        country="ZZ",
        category=Category.financial,
        issuing_authority="",
        applies_to="both",
        officially_valid=False,
        anchors=[Anchor(text=stolen)],
        fields=[],
    )
    loader.register(intruder)
    try:
        with pytest.raises(loader.RegistryError, match="cross-country doctype"):
            loader.validate_registry()
    finally:
        loader._REGISTRY.pop("zz_greedy_probe", None)

    loader.validate_registry()  # and the real registry is still clean


def test_the_naming_carve_out_stays_narrow() -> None:
    """The exception list may only contain document-naming nouns, not field labels.

    Its whole safety argument is that it is small and readable. A entry that is not
    actually declared by the generic it names is dead weight that hides a hole.
    """
    by_id = {spec.doctype_id: spec for spec in GENERICS}
    for doctype_id, names in loader._GENERIC_NAMING_ANCHORS.items():
        assert doctype_id in by_id, f"carve-out names unknown generic {doctype_id!r}"
        declared = {_folded(a) for a in by_id[doctype_id].anchors}
        assert names <= declared, (
            f"{doctype_id!r} carve-out lists {sorted(names - declared)}, which it does not "
            "declare as an anchor — remove the stale entry rather than leaving the hole open"
        )
