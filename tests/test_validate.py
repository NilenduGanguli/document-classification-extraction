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
        "verhoeff_aadhaar", "pan", "epic_voter", "gstin", "in_passport", "in_dl",
        "ssn", "ein", "itin", "sin_luhn", "curp", "rfc",
        "mrz_td1", "mrz_td2", "mrz_td3",
        "iso_date", "generic_date", "name", "address", "amount",
    }
    assert expected <= set(V.list_validators())


def test_unknown_validator_is_soft_not_destructive():
    """A typo in a doctype declaration must be visible, not data-destroying."""
    result = V.validate("verhoef_adhar", "some value")
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
    assert V.verification_level("verhoeff_aadhaar") == "checksum"
    assert V.verification_level("curp") == "checksum"
    # PAN's 10th character is a check character whose algorithm was never published, and
    # neither Indian passport nor DL numbers carry one at all.
    assert V.verification_level("pan") == "format"
    assert V.verification_level("in_passport") == "format"
    assert V.verification_level("in_dl") == "format"
    assert V.verification_level("nope") == "none"


# ---------------------------------------------------------------------------
# India
# ---------------------------------------------------------------------------
def test_verhoeff_aadhaar_accepts_a_checksum_valid_number():
    ok, normalized, error = V.validate("verhoeff_aadhaar", "9999 9999 0011")
    assert (ok, normalized, error) == (True, "999999990011", "")


def test_verhoeff_aadhaar_rejects_a_wrong_check_digit():
    assert V.validate("verhoeff_aadhaar", "999999990019").error == "verhoeff_check_failed"


def test_verhoeff_aadhaar_rejects_leading_zero_or_one():
    """UIDAI has never allocated a UID starting 0 or 1, checksum notwithstanding."""
    for prefix in ("0", "1"):
        payload = prefix + "9999999001"
        number = payload + V.verhoeff_check_digit(payload)
        assert V.verhoeff_ok(number)
        assert V.validate("verhoeff_aadhaar", number).error == "invalid_leading_digit"


def test_verhoeff_check_digit_round_trips():
    for payload in ("23456789012", "99999999001", "78901234567"):
        assert V.verhoeff_ok(payload + V.verhoeff_check_digit(payload))


def test_pan_entity_code_is_enforced():
    assert V.validate("pan", "ABCPZ1234A").ok          # P = individual
    assert V.validate("pan", "AAPFU0939F").ok          # F = firm
    # 'D' is not an allocated entity code — this is the most-copied fake PAN on the web.
    assert V.validate("pan", "ABCDE1234F").error == "invalid_entity_code:D"


def test_pan_rejects_malformed_shapes():
    for bad in ("ABCP1234A", "ABCPZ1234", "12345P678A", "ABCPZ12345"):
        assert V.validate("pan", bad).ok is False


def test_pan_surname_initial_mismatch_is_soft():
    """OCR swaps given name and surname constantly; a good PAN survives that."""
    assert V.validate("pan", "ABCPZ1234A", {"surname": "Zaveri"}).error == ""
    soft = V.validate("pan", "ABCPZ1234A", {"surname": "Kumar"})
    assert soft.ok is True
    assert "surname_initial_mismatch" in soft.error


def test_epic_checksum_failure_lowers_confidence_but_does_not_reject():
    """stdnum enforces a Luhn digit that genuine EPICs reportedly fail. Never hard-reject."""
    result = V.validate("epic_voter", "ABC1234567")
    assert V.luhn_ok("1234567") is False           # the digit genuinely does not check out
    assert result.ok is True                       # ...and the value is still usable
    assert result.normalized == "ABC1234567"
    assert "epic_luhn_soft_fail" in result.error
    assert V.verification_level("epic_voter") == "format"   # never checksum_verified


def test_epic_rejects_a_shape_that_is_not_an_epic():
    for bad in ("AB1234567", "ABCD123", "1234567890"):
        assert V.validate("epic_voter", bad).ok is False


def test_gstin_checksum_and_embedded_pan():
    ok, normalized, error = V.validate("gstin", "27AAPFU0939F1ZV")
    assert (ok, normalized, error) == (True, "27AAPFU0939F1ZV", "")
    # Break only the check character.
    assert "check_char_failed" in V.validate("gstin", "27AAPFU0939F1ZA").error
    # Break the embedded PAN's entity code (position 4 of the PAN, 'F' -> 'D').
    assert "embedded_pan_invalid" in V.validate("gstin", "27AAPDU0939F1ZQ").error


def test_gstin_check_char_helper_agrees_with_the_validator():
    assert V.gstin_check_char("27AAPFU0939F1Z") == "V"


def test_in_passport_is_a_format_heuristic():
    assert V.validate("in_passport", "A1234567").ok
    assert V.validate("in_passport", "Q1234567").ok is False   # Q is not issued
    assert V.validate("in_passport", "A0234567").ok is False   # first digit may not be 0
    assert V.validate("in_passport", "A123456").ok is False    # too short


def test_in_dl_has_no_checksum_and_says_so():
    """No national standard, no checksum — and never an identity or dedup key."""
    structured = V.validate("in_dl", "MH14 2011 0062821")
    assert structured.ok is True
    assert "no_checksum_exists" in structured.error
    loose = V.validate("in_dl", "KA0119990001234")
    assert loose.ok is True
    assert "no_checksum_exists" in loose.error
    assert V.validate("in_dl", "12345").ok is False
    assert "no checksum" in (V.VALIDATORS["in_dl"].__doc__ or "").lower()


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
        "GSTIN 27AAPFU0939F1ZV was issued against PAN AAPFU0939F. "
        "UID 9999 9999 0011 is genuine but 9999 9999 0019 is not. "
        "EIN 12-3456789 valid; EIN 07-1234567 has no such IRS prefix."
    )
    hits = {(h.validator, h.normalized) for h in V.sweep(text)}
    assert ("gstin", "27AAPFU0939F1ZV") in hits
    assert ("pan", "AAPFU0939F") in hits
    assert ("verhoeff_aadhaar", "999999990011") in hits
    assert ("ein", "12-3456789") in hits
    assert not any(n == "999999990019" for _v, n in hits)
    assert not any(n == "07-1234567" for _v, n in hits)


def test_sweep_marks_soft_hits_as_unverified():
    hits = V.sweep("EPIC ABC1234567", ["epic_voter"])
    assert len(hits) == 1
    assert hits[0].checksum_verified is False
    assert "epic_luhn_soft_fail" in hits[0].soft_error


def test_sweep_can_be_restricted_and_is_order_preserving():
    text = "first 12-3456789 then 27AAPFU0939F1ZV"
    assert [h.validator for h in V.sweep(text, ["ein", "gstin"])] == ["ein", "gstin"]
    assert V.sweep(text, ["curp"]) == []
    assert V.sweep("") == []
