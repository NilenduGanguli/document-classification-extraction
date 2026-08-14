"""Validator tests — pure, offline, no network, no DB, no model.

Every specimen here is either a published documentation example (RENAPO's CURP sample, the
SAT's RFC sample, the ICAO 9303 "Anna Maria Eriksson / Utopia" MRZ) or an obviously
synthetic number built to satisfy a checksum. No real person's identifier appears in this
file, and none should ever be added to it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # keeps the suite runnable without an installed package
    sys.path.insert(0, str(_ROOT))

from dce.extract import validate as V  # noqa: E402

# ICAO 9303 specimen passport MRZ (Utopia, a state that does not exist).
TD3_LINE1 = "P<UTOERIKSSON<<ANNA<MARIA".ljust(44, "<")
TD3_LINE2 = "L898902C36UTO7408122F1204159ZE184226B<<<<<10"
TD3_BLOCK = f"{TD3_LINE1}\n{TD3_LINE2}"

TD1_BLOCK = "\n".join(
    [
        "I<UTOD231458907<<<<<<<<<<<<<<<",
        "7408122F1204159UTO<<<<<<<<<<<6",
        "ERIKSSON<<ANNA<MARIA<<<<<<<<<<",
    ]
)
TD2_BLOCK = "\n".join(
    [
        "I<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<",
        "D231458907UTO7408122F1204159<<<<<<<6",
    ]
)


# ---------------------------------------------------------------------------
# Registry contract
# ---------------------------------------------------------------------------
def test_every_named_validator_is_registered():
    """The names doctype declarations are allowed to use, all present."""
    expected = {
        "ssn", "ein", "itin", "sin_luhn", "curp", "rfc",
        "mrz_td1", "mrz_td2", "mrz_td3",
        "iso_date", "generic_date", "name", "address", "amount",
    }
    assert expected <= set(V.list_validators())


def test_the_india_validators_are_gone_not_merely_unreferenced():
    """The India pack was removed, so its six identifier validators were too.

    Two of them — ``pan`` and ``in_dl`` — are plain English words in another jurisdiction's
    vocabulary, so a later pack could re-register the name meaning something else. Asserting
    absence rather than trusting it is what makes that a visible decision.
    """
    for gone in ("verhoeff_aadhaar", "pan", "epic_voter", "gstin", "in_passport", "in_dl"):
        assert gone not in V.list_validators()
        assert gone not in V.ID_PATTERNS
        assert gone not in V.CHECKSUM_VALIDATORS
    for helper in ("verhoeff_ok", "verhoeff_check_digit", "gstin_check_char"):
        assert not hasattr(V, helper), f"{helper} outlived its only caller"


def test_unknown_validator_is_soft_not_destructive():
    """A typo in a doctype declaration must be visible, not data-destroying."""
    result = V.validate("sinn_luhn", "some value")
    assert result.ok is True
    assert result.normalized == "some value"
    assert "unknown_validator" in result.error


def test_empty_value_is_rejected_by_every_validator():
    for name in V.list_validators():
        assert V.validate(name, "   ").ok is False


def test_validators_never_raise_on_hostile_input():
    """OCR emits garbage; a validator that throws would take the extraction down."""
    for name in V.list_validators():
        for junk in ("<<<<", "☒☐", "-" * 500, "٣٤٥", "\x00\x01"):
            assert isinstance(V.validate(name, junk), V.ValidationResult)


def test_verification_level_separates_checksums_from_format_checks():
    assert V.verification_level("sin_luhn") == "checksum"
    assert V.verification_level("curp") == "checksum"
    assert V.verification_level("mrz_td3") == "checksum"
    # The SSA publishes area/group/serial rules and no check digit; an EIN can only have
    # its campus prefix checked. Both are shapes, not proof.
    assert V.verification_level("ssn") == "format"
    assert V.verification_level("ein") == "format"
    assert V.verification_level("nope") == "none"


# ---------------------------------------------------------------------------
# United States
# ---------------------------------------------------------------------------
def test_ssn_allocation_rules():
    assert V.validate("ssn", "123-45-6789").normalized == "123-45-6789"
    assert V.validate("ssn", "123456789").normalized == "123-45-6789"
    for bad, reason in (
        ("000-45-6789", "invalid_area:000"),
        ("666-45-6789", "invalid_area:666"),
        ("900-45-6789", "invalid_area:900"),
        ("123-00-6789", "invalid_group:00"),
        ("123-45-0000", "invalid_serial:0000"),
    ):
        assert V.validate("ssn", bad).error == reason


def test_ssn_advertising_block_is_rejected_with_a_useful_reason():
    """987-65-4320..4329 sit inside the never-issued 9xx range; say so specifically."""
    result = V.validate("ssn", "987-65-4320")
    assert result.ok is False
    assert result.error == "reserved_for_advertising:never_issued"


def test_ein_prefix_table():
    assert V.validate("ein", "12-3456789").normalized == "12-3456789"
    assert V.validate("ein", "31-1234567").ok          # SBA prefix
    for bad in ("07-1234567", "00-1234567", "97-1234567", "29-1234567"):
        assert "invalid_irs_prefix" in V.validate("ein", bad).error


def test_itin_accepts_the_50_to_65_group_range():
    """stdnum.us.itin omits 50-65 and rejects legitimate ITINs issued in that band.

    That omission is exactly the bug this service cannot ship: a wrongly rejected ITIN
    sends a real person to a human queue for no reason. Our range table is complete.
    """
    for group in range(50, 66):
        number = f"912-{group:02d}-1234"
        assert V.validate("itin", number).ok, f"{number} must be accepted"

    stdnum_itin = pytest.importorskip(
        "stdnum.us.itin", reason="python-stdnum is optional; the divergence is documented"
    )
    assert stdnum_itin.is_valid("912-50-1234") is False   # the omission, pinned
    assert V.validate("itin", "912-50-1234").ok is True   # ...and corrected here


def test_itin_other_ranges_and_rejections():
    for group in (70, 88, 90, 92, 94, 99):
        assert V.validate("itin", f"900-{group:02d}-1234").ok
    for bad in ("912-93-1234", "912-89-1234", "912-66-1234", "812-70-1234", "912-70-0000"):
        assert V.validate("itin", bad).ok is False


# ---------------------------------------------------------------------------
# Canada
# ---------------------------------------------------------------------------
def test_sin_luhn():
    assert V.validate("sin_luhn", "130 692 544").normalized == "130-692-544"
    assert V.validate("sin_luhn", "130692545").error == "luhn_check_failed"


def test_sin_unassigned_prefix_is_soft():
    """046-454-286 is Luhn-valid and published as a test SIN; keep fixtures usable."""
    result = V.validate("sin_luhn", "046454286")
    assert result.ok is True
    assert "unassigned_prefix" in result.error


# ---------------------------------------------------------------------------
# Mexico
# ---------------------------------------------------------------------------
def test_curp_check_digit_and_decoding():
    """RENAPO's own documentation example (a fictitious Gloria Hernández García)."""
    specimen = "HEGG560427MVZRRL04"
    ok, normalized, error = V.validate("curp", specimen)
    assert (ok, normalized, error) == (True, specimen, "")
    assert V.curp_check_digit(specimen[:17]) == "4"
    assert V.curp_birth_date(specimen).isoformat() == "1956-04-27"
    assert V.curp_sex(specimen) == "F"


def test_curp_rejects_a_wrong_check_digit():
    assert "check_digit_failed" in V.validate("curp", "HEGG560427MVZRRL05").error


def test_curp_accepts_the_non_binary_sex_marker():
    """Modern CURPs carry 'X'; the official alphabet only listed H/M."""
    base = "HEGG560427XVZRRL0"
    candidate = base + V.curp_check_digit(base)
    result = V.validate("curp", candidate)
    assert result.ok is True
    assert V.curp_sex(candidate) == "X"


def test_rfc_structure_is_strict_and_check_digit_is_soft():
    """SAT's documentation example; the homoclave is the most OCR-hostile glyph there is."""
    assert V.validate("rfc", "GODE561231GR8") == (True, "GODE561231GR8", "")
    soft = V.validate("rfc", "GODE561231GR7")
    assert soft.ok is True
    assert "check_digit_soft_fail" in soft.error
    assert V.rfc_check_digit("GODE561231GR8") == "8"
    for bad in ("GODE56123", "1234561231GR8", "GODE569931GR8"):
        assert V.validate("rfc", bad).ok is False


def test_rfc_without_a_homoclave_is_soft():
    result = V.validate("rfc", "GODE561231")
    assert result.ok is True
    assert "no_homoclave" in result.error


# ---------------------------------------------------------------------------
# MRZ
# ---------------------------------------------------------------------------
def test_mrz_check_digit_matches_the_icao_specimen():
    assert V.mrz_check_digit("L898902C3") == "6"
    assert V.mrz_check_digit("740812") == "2"
    assert V.mrz_check_digit("120415") == "9"
    assert V.mrz_check_digit("ZE184226B<<<<<") == "1"


def test_mrz_td3_specimen_validates():
    assert V.validate("mrz_td3", TD3_BLOCK) == (True, TD3_BLOCK, "")


def test_mrz_td3_detects_a_single_corrupted_glyph():
    """One wrong character breaks a check digit — that is why the MRZ outranks everything."""
    corrupted = TD3_BLOCK.replace("L898902C36", "L898902C35")
    result = V.validate("mrz_td3", corrupted)
    assert result.ok is False
    assert "document_number" in result.error


def test_mrz_td3_rejects_the_wrong_shape():
    assert "bad_shape" in V.validate("mrz_td3", TD3_LINE2).error
    assert "bad_shape" in V.validate("mrz_td3", TD2_BLOCK).error


def test_mrz_td1_and_td2_specimens_validate():
    assert V.validate("mrz_td1", TD1_BLOCK).ok
    assert V.validate("mrz_td2", TD2_BLOCK).ok


def test_mrz_tolerates_ocr_filler_confusables_and_spacing():
    noisy = TD3_BLOCK.replace("<<<", "\u00ab\u2039<").replace("L898902", "L898 902")
    assert V.validate("mrz_td3", noisy).ok


# ---------------------------------------------------------------------------
# Dates, names, addresses, amounts
# ---------------------------------------------------------------------------
def test_iso_date():
    assert V.validate("iso_date", "2026-08-05").normalized == "2026-08-05"
    assert "not_a_calendar_date" in V.validate("iso_date", "2026-02-30").error
    assert "bad_format" in V.validate("iso_date", "05/08/2026").error


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("31/12/1999", "1999-12-31"),
        ("1999-12-31", "1999-12-31"),
        ("31-12-1999", "1999-12-31"),
        ("Dec 31, 1999", "1999-12-31"),
        ("31 December 1999", "1999-12-31"),
        ("27 abril 1956", "1956-04-27"),
        ("1 de enero de 2020", "2020-01-01"),
    ],
)
def test_generic_date_parses_offline(raw, expected):
    assert V.validate("generic_date", raw).normalized == expected


def test_generic_date_reports_the_assumption_it_had_to_make():
    """01/02/2020 is a coin flip; the result says which way it was called."""
    result = V.validate("generic_date", "01/02/2020")
    assert result.normalized == "2020-02-01"
    assert "ambiguous_day_month:assumed_DMY" in result.error
    us = V.validate("generic_date", "01/02/2020", {"date_order": "MDY"})
    assert us.normalized == "2020-01-02"
    # Unambiguous when a component cannot be a month — no note.
    assert V.validate("generic_date", "31/12/1999").error == ""


def test_generic_date_rejects_impossible_and_unparseable_values():
    assert V.validate("generic_date", "31/13/1999").ok is False
    assert V.validate("generic_date", "not a date").ok is False


def test_name_rejects_dates_and_digit_runs():
    assert V.validate("name", "  Anna Maria  Eriksson ").normalized == "Anna Maria Eriksson"
    assert V.validate("name", "गीता शर्मा").ok           # script-agnostic
    assert V.validate("name", "12/03/1999").error == "looks_like_a_date"
    assert V.validate("name", "9999 9999 0011").ok is False
    assert V.validate("name", "X").ok is False


def test_address_requires_a_word():
    result = V.validate("address", "12 Long Road\nBengaluru 560001")
    assert result.normalized == "12 Long Road, Bengaluru 560001"
    assert V.validate("address", "12/03/1999").ok is False
    assert V.validate("address", "1234567890").error == "no_word_of_three_letters"
    assert V.validate("address", "12 A").ok is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1,234.56", "1234.56"),
        ("₹ 1,23,456.78", "123456.78"),        # Indian lakh grouping
        ("1.234,56", "1234.56"),               # European convention
        ("INR 5000", "5000"),
        ("(1,234.56)", "-1234.56"),            # accountancy parentheses
        ("1234.56-", "-1234.56"),
    ],
)
def test_amount_normalisation(raw, expected):
    assert V.validate("amount", raw).normalized == expected


def test_amount_rejects_non_numbers():
    assert V.validate("amount", "twelve").ok is False
    assert V.validate("amount", "12.34.56.78").ok is False


# ---------------------------------------------------------------------------
# The classifier's checksum sweep
# ---------------------------------------------------------------------------
def test_sweep_finds_only_valid_identifiers():
    text = (
        "CURP BEML920529HDFRRS03 on file. "
        "SIN 046 454 286 is Luhn-valid but 046 454 287 is not. "
        "EIN 12-3456789 valid; EIN 07-1234567 has no such IRS prefix."
    )
    hits = {(h.validator, h.normalized) for h in V.sweep(text)}
    assert ("curp", "BEML920529HDFRRS03") in hits
    assert ("sin_luhn", "046-454-286") in hits
    assert ("ein", "12-3456789") in hits
    assert not any(n.replace("-", "") == "046454287" for _v, n in hits)
    assert not any(n == "07-1234567" for _v, n in hits)


def test_sweep_marks_soft_hits_as_unverified():
    """RFC is the surviving soft-failure identifier: strict shape, advisory check digit."""
    hits = V.sweep("RFC AAA010101AA9", ["rfc"])
    assert len(hits) == 1
    assert hits[0].checksum_verified is False
    assert "check_digit" in hits[0].soft_error


def test_sweep_can_be_restricted_and_is_order_preserving():
    text = "first 12-3456789 then BEML920529HDFRRS03"
    assert [h.validator for h in V.sweep(text, ["ein", "curp"])] == ["ein", "curp"]
    assert V.sweep(text, ["sin_luhn"]) == []
    assert V.sweep("") == []
