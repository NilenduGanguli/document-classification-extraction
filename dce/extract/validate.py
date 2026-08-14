"""Named value validators — the single place that decides whether a value is real.

Every validator has the same shape::

    validator(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult

and :class:`ValidationResult` is a plain ``(ok, normalized, error)`` tuple, so callers may
unpack it positionally.

**Three-state contract** (this is the part that matters):

* ``ok=False`` — reject. The value is not a member of this identifier family and the
  candidate that proposed it was wrong.
* ``ok=True`` with ``error == ""`` — accepted. When the validator owns a real checksum
  (see :data:`CHECKSUM_VALIDATORS`) the caller may promote the field to
  ``checksum_verified``.
* ``ok=True`` with ``error != ""`` — **soft failure**. The value is usable but something
  is off. It must never be promoted past ``format_valid``, its confidence is discounted,
  and the message lands in ``ExtractedField.validator_error`` for the reviewer.

The soft state is not a convenience; a real identifier requires it:

* **RFC (Mexico)** — OCR mangles the homoclave constantly on a Constancia de Situación
  Fiscal printout, so the structure is strict and the check digit is advisory.

Two identifiers have **no published check digit at all** and are structural rules only:
the US SSN (:func:`ssn` — the SSA publishes area/group/serial rules and nothing more) and
the US EIN (:func:`ein` — only the campus prefix can be checked).

Dependency note: this module imports only the standard library. The classifier's checksum
sweep runs *before* a document type is known — inside the no-egress path — so importing it
must never drag in a model runtime, an HTTP client, or a heavy third-party package.
``python-stdnum`` is consulted opportunistically and lazily, never as a source of truth
(see :func:`itin` for the concrete reason).
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from typing import Any, Final, NamedTuple

__all__ = [
    "CHECKSUM_VALIDATORS",
    "ID_PATTERNS",
    "IdHit",
    "ValidationResult",
    "is_valid",
    "list_validators",
    "luhn_ok",
    "mrz_check_digit",
    "register",
    "sweep",
    "validate",
    "verification_level",
]


class ValidationResult(NamedTuple):
    """Outcome of one validator.

    Attributes:
        ok: ``False`` rejects the value outright.
        normalized: Canonical form (ids compacted, dates ISO-8601, amounts plain decimal).
        error: Empty on a clean pass; non-empty with ``ok=True`` marks a soft failure.
    """

    ok: bool
    normalized: str = ""
    error: str = ""


Validator = Callable[..., ValidationResult]

#: name -> validator. Populated by the :func:`register` decorator below.
VALIDATORS: dict[str, Validator] = {}

#: Validators backed by a genuine, published check digit. Only these may raise a field to
#: ``checksum_verified``, and only when they returned no soft error.
CHECKSUM_VALIDATORS: Final[frozenset[str]] = frozenset(
    {
        "curp",
        "rfc",
        "sin_luhn",
        "mrz_td1",
        "mrz_td2",
        "mrz_td3",
    }
)


def register(name: str) -> Callable[[Validator], Validator]:
    """Register a validator under ``name``.

    Args:
        name: The name a :class:`~dce.models.FieldSpec` refers to.

    Returns:
        A decorator that stores the function and returns it unchanged.
    """

    def _wrap(fn: Validator) -> Validator:
        VALIDATORS[name] = fn
        return fn

    return _wrap


def list_validators() -> list[str]:
    """Return every registered validator name, sorted."""
    return sorted(VALIDATORS)


def validate(
    name: str, value: str, context: Mapping[str, Any] | None = None
) -> ValidationResult:
    """Run the named validator over ``value``.

    An unknown name is deliberately a **soft** failure rather than a hard one: a typo in a
    doctype declaration must not silently delete extracted data, but it must be visible.

    Args:
        name: Registered validator name.
        value: Raw candidate value, as read off the page.
        context: Optional side-channel (``surname`` for PAN, ``date_order`` for dates).

    Returns:
        The validator's :class:`ValidationResult`.
    """
    fn = VALIDATORS.get(name)
    if fn is None:
        return ValidationResult(True, _squash(value), f"unknown_validator:{name}")
    if value is None or not str(value).strip():
        return ValidationResult(False, "", "empty_value")
    try:
        return fn(str(value), context)
    except (ValueError, TypeError, IndexError, KeyError) as exc:  # defensive, never fatal
        return ValidationResult(False, "", f"validator_error:{type(exc).__name__}")


def is_valid(name: str, value: str, context: Mapping[str, Any] | None = None) -> bool:
    """``True`` when the validator accepted the value (soft failures still count)."""
    return validate(name, value, context).ok


def verification_level(name: str) -> str:
    """Return ``"checksum"``, ``"format"`` or ``"none"`` for a validator name."""
    if name in CHECKSUM_VALIDATORS:
        return "checksum"
    if name in VALIDATORS:
        return "format"
    return "none"


# ---------------------------------------------------------------------------
# Small text helpers (stdlib only)
# ---------------------------------------------------------------------------
def _squash(value: str) -> str:
    """NFKC-normalise, collapse all whitespace runs, strip the ends."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip()


def _compact(value: str) -> str:
    """Upper-case and drop everything that is not alphanumeric (keeps ``Ñ`` and ``&``)."""
    return re.sub(r"[^0-9A-Za-zÑñ&]", "", unicodedata.normalize("NFKC", value or "")).upper()


def _digits(value: str) -> str:
    """Every digit in ``value``, in order."""
    return re.sub(r"\D", "", value or "")


def _fail(error: str) -> ValidationResult:
    return ValidationResult(False, "", error)


def _soft(normalized: str, error: str) -> ValidationResult:
    return ValidationResult(True, normalized, error)


def _join_errors(errors: Iterable[str]) -> str:
    return "; ".join(e for e in errors if e)


# ---------------------------------------------------------------------------
# Check-digit primitives
# ---------------------------------------------------------------------------
def luhn_ok(number: str) -> bool:
    """Luhn-validate a digit string whose last digit is the check digit."""
    if not number.isdigit() or len(number) < 2:
        return False
    total = 0
    for i, ch in enumerate(reversed(number)):
        digit = int(ch)
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


_MRZ_WEIGHTS: Final[tuple[int, int, int]] = (7, 3, 1)


def _mrz_value(ch: str) -> int:
    """ICAO 9303 character value: digits are themselves, ``A``-``Z`` are 10-35, ``<`` is 0."""
    if ch.isdigit():
        return int(ch)
    if "A" <= ch <= "Z":
        return ord(ch) - ord("A") + 10
    if ch == "<":
        return 0
    raise ValueError(f"illegal MRZ character: {ch!r}")


def mrz_check_digit(chars: str) -> str:
    """Compute the ICAO 9303 7-3-1 check digit over ``chars``."""
    total = sum(_mrz_value(ch) * _MRZ_WEIGHTS[i % 3] for i, ch in enumerate(chars))
    return str(total % 10)


# ---------------------------------------------------------------------------
# United States
# ---------------------------------------------------------------------------
_SSN_SHAPE_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{3})(\d{2})(\d{4})$")
#: SSNs reserved by the SSA for advertising/specimen use. Structurally legal, never issued.
_SSN_ADVERTISING = frozenset({f"98765{n:04d}" for n in range(4320, 4330)})


@register("ssn")
def ssn(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate a US Social Security Number by the SSA's area/group/serial rules.

    There is no check digit on an SSN. What exists is a set of allocation rules, and they
    reject the overwhelming majority of random 9-digit runs:

    * area (first 3) is never ``000``, ``666``, or ``900``-``999``;
    * group (middle 2) is never ``00``;
    * serial (last 4) is never ``0000``.

    The SSA's advertising block (``987-65-4320`` … ``987-65-4329``) falls *inside* the
    never-issued 900-999 area range, so it is rejected like any other 9xx value — but with
    its own message, because those numbers turn up constantly in sample forms and "reserved
    for advertising" is a more useful thing for a reviewer to read than "bad area".
    """
    digits = _digits(value)
    if len(digits) != 9:
        return _fail(f"bad_length:{len(digits)}!=9")
    match = _SSN_SHAPE_RE.match(digits)
    if match is None:  # pragma: no cover - guarded by the length check above
        return _fail("bad_format")
    area, group, serial = match.groups()
    if digits in _SSN_ADVERTISING:
        return _fail("reserved_for_advertising:never_issued")
    if area == "000" or area == "666" or area[0] == "9":
        return _fail(f"invalid_area:{area}")
    if group == "00":
        return _fail("invalid_group:00")
    if serial == "0000":
        return _fail("invalid_serial:0000")
    return ValidationResult(True, f"{area}-{group}-{serial}", "")


#: IRS campus prefixes that have ever been issued. Everything else is not an EIN. Kept in
#: the IRS's own row layout so it can be diffed against the published table by eye.
_EIN_PREFIXES: Final[frozenset[str]] = frozenset(
    (
        "01", "02", "03", "04", "05", "06", "10", "11", "12", "13", "14", "15",
        "16", "20", "21", "22", "23", "24", "25", "26", "27", "30", "31", "32",
        "33", "34", "35", "36", "37", "38", "39", "40", "41", "42", "43", "44",
        "45", "46", "47", "48", "50", "51", "52", "53", "54", "55", "56", "57",
        "58", "59", "60", "61", "62", "63", "64", "65", "66", "67", "68", "71",
        "72", "73", "74", "75", "76", "77", "80", "81", "82", "83", "84", "85",
        "86", "87", "88", "90", "91", "92", "93", "94", "95", "98", "99",
    )
)


@register("ein")
def ein(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate a US Employer Identification Number (``NN-NNNNNNN``).

    An EIN has no check digit either; the two-digit prefix identifies the issuing IRS
    campus and only the published set is valid, which rejects roughly a fifth of
    well-formed candidates.
    """
    digits = _digits(value)
    if len(digits) != 9:
        return _fail(f"bad_length:{len(digits)}!=9")
    prefix = digits[:2]
    if prefix not in _EIN_PREFIXES:
        return _fail(f"invalid_irs_prefix:{prefix}")
    return ValidationResult(True, f"{digits[:2]}-{digits[2:]}", "")


#: IRS-valid ITIN group ranges (the middle two digits).
_ITIN_GROUPS: Final[frozenset[int]] = frozenset(
    set(range(50, 66)) | set(range(70, 89)) | set(range(90, 93)) | set(range(94, 100))
)


@register("itin")
def itin(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate a US Individual Taxpayer Identification Number (``9NN-GG-NNNN``).

    An ITIN always starts with 9 and its *group* (middle two digits) must fall in one of
    the IRS-published ranges: **50-65**, 70-88, 90-92, 94-99.

    **Do not delegate this to python-stdnum.** ``stdnum.us.itin`` omits the 50-65 range,
    so a legitimate ITIN issued in that band is rejected outright by it. That is precisely
    the class of bug this service cannot ship — a rejected ITIN means a real person is sent
    to a human queue for no reason. The ranges above are implemented here in full, and
    :mod:`tests.test_validate` pins the 50-65 case.
    """
    digits = _digits(value)
    if len(digits) != 9:
        return _fail(f"bad_length:{len(digits)}!=9")
    if digits[0] != "9":
        return _fail(f"invalid_prefix:{digits[0]}!=9")
    group = int(digits[3:5])
    if group not in _ITIN_GROUPS:
        return _fail(f"invalid_group:{digits[3:5]}")
    if digits[5:] == "0000":
        return _fail("invalid_serial:0000")
    return ValidationResult(True, f"{digits[:3]}-{digits[3:5]}-{digits[5:]}", "")


# ---------------------------------------------------------------------------
# Canada
# ---------------------------------------------------------------------------
@register("sin_luhn")
def sin_luhn(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate a Canadian Social Insurance Number (9 digits, Luhn check digit).

    The leading digit encodes the province/programme of registration; 0 and 8 have never
    been allocated, so a Luhn-valid number starting with either is flagged softly (several
    published *test* SINs live in that space and must stay usable in fixtures).
    """
    digits = _digits(value)
    if len(digits) != 9:
        return _fail(f"bad_length:{len(digits)}!=9")
    if not luhn_ok(digits):
        return _fail("luhn_check_failed")
    normalized = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if digits[0] in "08":
        return _soft(normalized, f"unassigned_prefix:{digits[0]}")
    return ValidationResult(True, normalized, "")


# ---------------------------------------------------------------------------
# Mexico
# ---------------------------------------------------------------------------
#: RENAPO check-digit alphabet; the index of a character is its value.
_CURP_ALPHABET: Final[str] = "0123456789ABCDEFGHIJKLMNÑOPQRSTUVWXYZ"
_CURP_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-ZÑ]{4}[0-9]{6}[HMX][A-ZÑ]{5}[0-9A-Z][0-9]$"
)
#: Two-letter federative-entity codes used in CURP positions 12-13 (``NE`` = born abroad).
_CURP_STATES: Final[frozenset[str]] = frozenset(
    (
        "AS", "BC", "BS", "CC", "CL", "CM", "CS", "CH", "DF", "DG", "GT",
        "GR", "HG", "JC", "MC", "MN", "MS", "NT", "NL", "OC", "PL", "QT",
        "QR", "SP", "SL", "SR", "TC", "TS", "TL", "VZ", "YN", "ZS", "NE",
    )
)


def curp_check_digit(first17: str) -> str:
    """Return the RENAPO check digit for the first 17 characters of a CURP."""
    total = sum(_CURP_ALPHABET.index(first17[i]) * (18 - i) for i in range(17))
    return str((10 - total % 10) % 10)


@register("curp")
def curp(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate an 18-character Mexican CURP including its RENAPO check digit.

    The official alphabet assigns only ``H``/``M`` to the sex position; modern non-binary
    CURPs carry ``X``, which is accepted rather than rejected. The embedded birth date is
    checked for calendar validity, and the federative-entity code is a soft check because
    the list has been extended before.
    """
    compact = _compact(value)
    if len(compact) != 18:
        return _fail(f"bad_length:{len(compact)}!=18")
    if not _CURP_RE.match(compact):
        return _fail("bad_format")
    expected = curp_check_digit(compact[:17])
    if compact[17] != expected:
        return _fail(f"check_digit_failed:{compact[17]}!={expected}")
    errors: list[str] = []
    if curp_birth_date(compact) is None:
        errors.append("embedded_birth_date_invalid")
    if compact[11:13] not in _CURP_STATES:
        errors.append(f"unknown_entity_code:{compact[11:13]}")
    return ValidationResult(True, compact, _join_errors(errors))


def curp_birth_date(compact: str) -> date | None:
    """Decode the birth date embedded at CURP positions 4-9.

    RENAPO's century pivot is position 16: a digit there means 19xx, a letter means 20xx.
    """
    try:
        year, month, day = int(compact[4:6]), int(compact[6:8]), int(compact[8:10])
        year += 1900 if compact[16].isdigit() else 2000
        return date(year, month, day)
    except (ValueError, IndexError):
        return None


def curp_sex(compact: str) -> str | None:
    """Map the CURP sex position to ``M`` / ``F`` / ``X``."""
    return {"H": "M", "M": "F", "X": "X"}.get(compact[10:11])


#: RFC check-digit alphabet — note the ``&`` at 24 and the space at 37.
_RFC_ALPHABET: Final[str] = "0123456789ABCDEFGHIJKLMN&OPQRSTUVWXYZ Ñ"
_RFC_MORAL_RE: Final[re.Pattern[str]] = re.compile(r"^[A-ZÑ&]{3}[0-9]{6}[0-9A-Z]{3}$")
_RFC_FISICA_RE: Final[re.Pattern[str]] = re.compile(r"^[A-ZÑ&]{4}[0-9]{6}[0-9A-Z]{3}$")
_RFC_SHORT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-ZÑ&]{4}[0-9]{6}$")


def rfc_check_digit(number: str) -> str:
    """Return the SAT check character for a full RFC (its own last character excluded)."""
    padded = ("   " + number[:-1])[-12:]
    total = sum(_RFC_ALPHABET.index(ch) * (13 - i) for i, ch in enumerate(padded))
    return _RFC_ALPHABET[(11 - total % 11) % 11]


@register("rfc")
def rfc(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate a Mexican RFC: structure strictly, check digit **softly**.

    12 characters for a *persona moral*, 13 for a *persona física*; a bare 10-character
    personal key with no homoclave is accepted with a note. The homoclave's check character
    is the most OCR-hostile glyph on a Constancia de Situación Fiscal, so a mismatch is a
    soft failure — the value survives, flagged, instead of being thrown away.
    """
    compact = _compact(value)
    if len(compact) == 10 and _RFC_SHORT_RE.match(compact):
        return _soft(compact, "no_homoclave:check_digit_unavailable")
    if len(compact) not in (12, 13):
        return _fail(f"bad_length:{len(compact)}")
    matcher = _RFC_MORAL_RE if len(compact) == 12 else _RFC_FISICA_RE
    if not matcher.match(compact):
        return _fail("bad_format")
    if _rfc_embedded_date(compact) is None:
        return _fail("embedded_date_invalid")
    expected = rfc_check_digit(compact)
    if compact[-1] != expected:
        return _soft(compact, f"check_digit_soft_fail:{compact[-1]}!={expected}")
    return ValidationResult(True, compact, "")


def _rfc_embedded_date(compact: str) -> date | None:
    """Decode the YYMMDD block that follows the name letters in an RFC."""
    offset = 3 if len(compact) == 12 else 4
    block = compact[offset : offset + 6]
    try:
        year, month, day = int(block[0:2]), int(block[2:4]), int(block[4:6])
    except ValueError:
        return None
    # Two-digit year: RFCs are issued to entities that exist, so 1930..2029 is the window.
    full_year = 1900 + year if year >= 30 else 2000 + year
    try:
        return date(full_year, month, day)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# ICAO 9303 machine-readable zones
# ---------------------------------------------------------------------------
_MRZ_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z0-9<]+$")


def mrz_lines(value: str) -> list[str]:
    """Split an MRZ block into upper-cased, space-stripped lines.

    OCR emits the ``<`` filler as a left-pointing guillemet often enough to be worth
    mapping back (see the replacements below), and inserts stray spaces inside the zone.
    Alphanumerics are left strictly alone so the check digits stay meaningful.
    """
    text = (value or "").upper().replace("\u00ab", "<").replace("\u2039", "<")
    return [re.sub(r"\s+", "", ln) for ln in text.splitlines() if ln.strip()]


def _mrz_field_checks(pairs: Iterable[tuple[str, str, str]]) -> list[str]:
    """Verify ``(name, payload, printed_digit)`` triples; return the names that failed."""
    failures: list[str] = []
    for name, payload, printed in pairs:
        if printed == "<":
            # ICAO permits a filler where a field is absent; nothing to verify.
            continue
        if not printed.isdigit() or mrz_check_digit(payload) != printed:
            failures.append(name)
    return failures


def _mrz_result(lines: list[str], failures: list[str]) -> ValidationResult:
    normalized = "\n".join(lines)
    if failures:
        return _fail("check_digit_failed:" + ",".join(failures))
    return ValidationResult(True, normalized, "")


@register("mrz_td3")
def mrz_td3(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate a TD3 (passport) MRZ: two lines of 44 characters.

    Verifies the document-number, date-of-birth, expiry, personal-number and composite
    check digits with the ICAO 9303 7-3-1 weighting. A single mis-read glyph flips one of
    them, which is exactly what makes an MRZ worth more than any label-anchored guess.
    """
    lines = mrz_lines(value)
    if len(lines) != 2 or any(len(ln) != 44 for ln in lines):
        return _fail(f"bad_shape:expected_2x44_got_{[len(x) for x in lines]}")
    if not all(_MRZ_LINE_RE.match(ln) for ln in lines):
        return _fail("illegal_characters")
    line2 = lines[1]
    composite = line2[0:10] + line2[13:20] + line2[21:43]
    failures = _mrz_field_checks(
        [
            ("document_number", line2[0:9], line2[9]),
            ("date_of_birth", line2[13:19], line2[19]),
            ("expiry_date", line2[21:27], line2[27]),
            ("personal_number", line2[28:42], line2[42]),
            ("composite", composite, line2[43]),
        ]
    )
    return _mrz_result(lines, failures)


@register("mrz_td2")
def mrz_td2(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate a TD2 MRZ (ID card / visa format): two lines of 36 characters."""
    lines = mrz_lines(value)
    if len(lines) != 2 or any(len(ln) != 36 for ln in lines):
        return _fail(f"bad_shape:expected_2x36_got_{[len(x) for x in lines]}")
    if not all(_MRZ_LINE_RE.match(ln) for ln in lines):
        return _fail("illegal_characters")
    line2 = lines[1]
    composite = line2[0:10] + line2[13:20] + line2[21:35]
    failures = _mrz_field_checks(
        [
            ("document_number", line2[0:9], line2[9]),
            ("date_of_birth", line2[13:19], line2[19]),
            ("expiry_date", line2[21:27], line2[27]),
            ("composite", composite, line2[35]),
        ]
    )
    return _mrz_result(lines, failures)


@register("mrz_td1")
def mrz_td1(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate a TD1 MRZ (credit-card-sized ID): three lines of 30 characters."""
    lines = mrz_lines(value)
    if len(lines) != 3 or any(len(ln) != 30 for ln in lines):
        return _fail(f"bad_shape:expected_3x30_got_{[len(x) for x in lines]}")
    if not all(_MRZ_LINE_RE.match(ln) for ln in lines):
        return _fail("illegal_characters")
    line1, line2 = lines[0], lines[1]
    composite = line1[5:30] + line2[0:7] + line2[8:15] + line2[18:29]
    failures = _mrz_field_checks(
        [
            ("document_number", line1[5:14], line1[14]),
            ("date_of_birth", line2[0:6], line2[6]),
            ("expiry_date", line2[8:14], line2[14]),
            ("composite", composite, line2[29]),
        ]
    )
    return _mrz_result(lines, failures)


# ---------------------------------------------------------------------------
# Dates, names, addresses, amounts
# ---------------------------------------------------------------------------
@register("iso_date")
def iso_date(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate a strict ``YYYY-MM-DD`` date that exists on the calendar."""
    text = _squash(value)
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match is None:
        return _fail("bad_format:expected_YYYY-MM-DD")
    try:
        parsed = date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError as exc:
        return _fail(f"not_a_calendar_date:{exc}")
    return ValidationResult(True, parsed.isoformat(), "")


_MONTHS: Final[dict[str, int]] = {
    "jan": 1, "january": 1, "ene": 1, "enero": 1,
    "feb": 2, "february": 2, "febrero": 2,
    "mar": 3, "march": 3, "marzo": 3,
    "apr": 4, "april": 4, "abr": 4, "abril": 4,
    "may": 5, "mayo": 5,
    "jun": 6, "june": 6, "junio": 6,
    "jul": 7, "july": 7, "julio": 7,
    "aug": 8, "august": 8, "ago": 8, "agosto": 8,
    "sep": 9, "sept": 9, "september": 9, "septiembre": 9, "setiembre": 9,
    "oct": 10, "october": 10, "octubre": 10,
    "nov": 11, "november": 11, "noviembre": 11,
    "dec": 12, "december": 12, "dic": 12, "diciembre": 12,
}
_DATE_YMD_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_DATE_NUMERIC_RE = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})")
_DATE_DMY_WORD_RE = re.compile(r"(\d{1,2})\s*[-/ ]\s*([^\W\d_]{3,12})\.?,?\s*[-/ ]?\s*(\d{4})")
_DATE_MDY_WORD_RE = re.compile(r"([^\W\d_]{3,12})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")


@register("generic_date")
def generic_date(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Parse a human-written date into ISO-8601, offline and deterministically.

    Ambiguity is reported, never hidden. ``01/02/2020`` is a real coin flip: the day/month
    order resolves to ``context["date_order"]`` (``"DMY"`` by default, matching the IN/MX/CA
    document mix this service was built for) and the result carries a soft error so a
    reviewer can see the assumption that was made. Where a component is >12 the order is
    unambiguous and no note is emitted.
    """
    text = _squash(value)
    if not text:
        return _fail("empty_value")
    # Spanish writes "1 de enero de 2020"; the connector carries no information.
    text = re.sub(r"\bde[l]?\b", " ", text, flags=re.IGNORECASE)
    text = _squash(text)
    order = str((context or {}).get("date_order") or "DMY").upper()
    notes: list[str] = []

    match = _DATE_YMD_RE.search(text)
    if match is not None:
        return _finish_date(int(match[1]), int(match[2]), int(match[3]), notes)

    match = _DATE_MDY_WORD_RE.search(text)
    if match is not None:
        month = _MONTHS.get(match[1].lower())
        if month is not None:
            return _finish_date(int(match[3]), month, int(match[2]), notes)

    match = _DATE_DMY_WORD_RE.search(text)
    if match is not None:
        month = _MONTHS.get(match[2].lower())
        if month is not None:
            return _finish_date(int(match[3]), month, int(match[1]), notes)

    match = _DATE_NUMERIC_RE.search(text)
    if match is not None:
        first, second, raw_year = int(match[1]), int(match[2]), match[3]
        year = int(raw_year)
        if len(raw_year) == 2:
            year = 1900 + year if year >= 30 else 2000 + year
            notes.append(f"two_digit_year:assumed_{year}")
        if first > 12:
            day, month = first, second
        elif second > 12:
            day, month = second, first
        else:
            day, month = (first, second) if order == "DMY" else (second, first)
            notes.append(f"ambiguous_day_month:assumed_{order}")
        return _finish_date(year, month, day, notes)

    return _fail("unparseable_date")


def _finish_date(year: int, month: int, day: int, notes: list[str]) -> ValidationResult:
    try:
        parsed = date(year, month, day)
    except ValueError as exc:
        return _fail(f"not_a_calendar_date:{exc}")
    return ValidationResult(True, parsed.isoformat(), _join_errors(notes))


_DATE_LIKE_RE = re.compile(r"^\s*\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4}\s*$")


@register("name")
def name(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate a person or entity name.

    Rejects the two things that actually bind to a name field by mistake: a date, and a
    run of digits. Script-agnostic — accented Latin, ``Ñ`` and any non-Latin script all
    count as letters — because a bilingual card prints the holder's name twice, and the
    second rendering must not be thrown away for having no ASCII in it.
    """
    text = _squash(value).strip(" :;,-\u2013\u2014")
    if not text:
        return _fail("empty_value")
    if _DATE_LIKE_RE.match(text):
        return _fail("looks_like_a_date")
    letters = sum(1 for ch in text if ch.isalpha())
    if letters < 2:
        return _fail("no_letters")
    digits = sum(1 for ch in text if ch.isdigit())
    if digits > letters / 3:
        return _fail(f"too_many_digits:{digits}")
    if len(text) > 120:
        return _fail(f"too_long:{len(text)}")
    notes: list[str] = []
    if len(text.split()) > 8:
        notes.append("suspiciously_many_tokens")
    return ValidationResult(True, text, _join_errors(notes))


@register("address")
def address(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate a postal address.

    An address must contain a word — three consecutive letters — which is what stops a
    label-anchored locator from binding ``12/03/1999`` to an address field when the date
    happens to sit to the right of the ``Address`` label. Line breaks are preserved as
    ``", "`` so the normalized form stays a single line.
    """
    raw = unicodedata.normalize("NFKC", value or "")
    joined = re.sub(r"\s*\n\s*", ", ", raw).strip()
    text = re.sub(r"[ \t]+", " ", joined).strip(" ,;:-")
    if len(text) < 6:
        return _fail(f"too_short:{len(text)}")
    if _DATE_LIKE_RE.match(text):
        return _fail("looks_like_a_date")
    if not re.search(r"[^\W\d_]{3}", text):
        return _fail("no_word_of_three_letters")
    if len(text) > 400:
        return _fail(f"too_long:{len(text)}")
    return ValidationResult(True, text, "")


_CURRENCY_RE = re.compile(r"(?:INR|USD|MXN|CAD|EUR|GBP|RS\.?|₹|\$|€|£|MX\$|C\$)", re.I)


@register("amount")
def amount(value: str, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Parse a currency amount into a plain decimal string.

    Handles the four separator conventions that appear in this fleet's documents:
    ``1,234.56`` (US), ``1.234,56`` (European), ``1,23,456.78`` (Indian lakh grouping) and
    unseparated digits. Accountancy parentheses and a trailing minus both mean negative.
    """
    text = _squash(value)
    if not text:
        return _fail("empty_value")
    negative = bool(re.search(r"^\(.*\)$", text)) or text.rstrip().endswith("-")
    text = _CURRENCY_RE.sub("", text).strip(" ()-+")
    if not re.search(r"\d", text):
        return _fail("no_digits")
    if not re.fullmatch(r"[\d.,\s']*", text):
        return _fail("non_numeric_characters")
    text = re.sub(r"[\s']", "", text)

    if "," in text and "." in text:
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
    elif text.count(",") == 1 and len(text.split(",")[-1]) in (1, 2):
        decimal_sep = ","
    else:
        decimal_sep = "."
    thousands_sep = "." if decimal_sep == "," else ","
    text = text.replace(thousands_sep, "").replace(decimal_sep, ".")
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return _fail("malformed_number")
    normalized = text.rstrip(".")
    return ValidationResult(True, f"-{normalized}" if negative else normalized, "")


# ---------------------------------------------------------------------------
# Candidate patterns + the classifier's checksum sweep
# ---------------------------------------------------------------------------
#: Deliberately over-capturing shapes, one per checksummed/structured identifier. The
#: validators above decide what is real; these only decide what is worth looking at. Used
#: by the regex locator when a FieldSpec declares a validator but no pattern, and by
#: :func:`sweep` for the classifier's L1 tier.
ID_PATTERNS: Final[dict[str, str]] = {
    "ssn": r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)",
    "itin": r"(?<!\d)9\d{2}-\d{2}-\d{4}(?!\d)",
    "ein": r"(?<!\d)\d{2}-\d{7}(?!\d)",
    "sin_luhn": r"(?<!\d)\d{3}[ -]?\d{3}[ -]?\d{3}(?!\d)",
    "curp": r"\b[A-Z]{4}\d{6}[HMX][A-Z]{5}[0-9A-Z]\d\b",
    "rfc": r"\b[A-ZÑ&]{3,4}\d{6}[0-9A-Z]{3}\b",
}


class IdHit(NamedTuple):
    """One checksum/structure-valid identifier found in a text sweep."""

    validator: str
    raw: str
    normalized: str
    checksum_verified: bool
    soft_error: str
    start: int
    end: int


def sweep(text: str, names: Iterable[str] | None = None) -> list[IdHit]:
    """Find every valid identifier in ``text`` — the classifier's L1 checksum tier.

    Pure, in-process and stdlib-only, which is what lets the classifier call it before a
    document type is known without touching the network.

    Args:
        text: Document text, in reading order.
        names: Restrict the sweep to these validator names; ``None`` sweeps them all.

    Returns:
        Hits in document order, de-duplicated by (validator, normalized value). Only
        values their validator accepted are returned; a soft failure is included with its
        ``soft_error`` set and ``checksum_verified`` False.
    """
    if not text:
        return []
    wanted = list(names) if names is not None else list(ID_PATTERNS)
    upper = text.upper()
    hits: list[IdHit] = []
    seen: set[tuple[str, str]] = set()
    for validator_name in wanted:
        pattern = ID_PATTERNS.get(validator_name)
        if pattern is None:
            continue
        for match in re.finditer(pattern, upper):
            raw = match.group(0)
            result = validate(validator_name, raw)
            if not result.ok:
                continue
            key = (validator_name, result.normalized)
            if key in seen:
                continue
            seen.add(key)
            hits.append(
                IdHit(
                    validator=validator_name,
                    raw=raw,
                    normalized=result.normalized,
                    checksum_verified=(
                        validator_name in CHECKSUM_VALIDATORS and not result.error
                    ),
                    soft_error=result.error,
                    start=match.start(),
                    end=match.end(),
                )
            )
    hits.sort(key=lambda h: (h.start, h.validator))
    return hits
