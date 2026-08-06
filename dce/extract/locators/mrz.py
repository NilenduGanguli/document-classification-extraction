"""MRZ locator — parse an ICAO 9303 machine-readable zone and answer seven fields at once.

The MRZ is the only part of an identity document that was designed to be read by a machine
and carries its own check digits. When one is present and its check digits pass, nothing
else on the page competes: :mod:`dce.extract.resolve` stops as soon as it sees a
checksum-verified candidate, and this is where most of them come from.

Supported formats:

* **TD3** — passports: two lines of 44.
* **TD2** — older ID cards and visas: two lines of 36.
* **TD1** — credit-card-sized ID cards: three lines of 30.

Check digits are verified through :mod:`dce.extract.validate`, so the 7-3-1 weighting lives
in exactly one place and the classifier's checksum sweep and this locator can never drift.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, date, datetime

from dce.extract import validate as V
from dce.extract.locators.base import Candidate, LocatorContext, passes_pattern
from dce.models import FieldSpec, LayoutView, Quad

__all__ = ["MrzDocument", "find_mrz", "locate"]

_CONF_VERIFIED = 0.97
_CONF_PARSED_ONLY = 0.45
_MRZ_LINE_RE = re.compile(r"^[A-Z0-9<]{28,50}$")

#: Canonical MRZ field name -> the attribute keys and field names it answers. Attribute
#: keys reuse the fleet ontology namespace so a merge view can group across doctypes.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "surname": ("identity.surname", "surname", "last_name", "family_name", "apellidos"),
    "given_names": (
        "identity.given_names", "given_names", "given_name", "first_name", "nombres",
    ),
    "document_number": (
        "id.passport_number", "id.document_number", "document_number", "passport_number",
        "doc_number",
    ),
    "nationality": ("identity.nationality", "nationality", "nacionalidad"),
    "date_of_birth": (
        "identity.date_of_birth", "date_of_birth", "dob", "birth_date", "fecha_nacimiento",
    ),
    "sex": ("identity.sex", "sex", "gender", "sexo"),
    "expiry_date": (
        "doc.expiry_date", "expiry_date", "date_of_expiry", "expiration_date", "valid_until",
    ),
    "issuing_state": (
        "doc.issuing_state", "issuing_state", "issuing_country", "country",
    ),
}


@dataclass(slots=True)
class MrzDocument:
    """A parsed machine-readable zone."""

    kind: str                       # "TD1" | "TD2" | "TD3"
    lines: list[str]
    fields: dict[str, str] = dc_field(default_factory=dict)
    checksum_ok: bool = False
    error: str = ""
    page: int = 1
    bbox: Quad | None = None

    @property
    def block(self) -> str:
        """The zone as a single newline-joined string."""
        return "\n".join(self.lines)


def locate(field: FieldSpec, view: LayoutView, ctx: LocatorContext) -> list[Candidate]:
    """Answer a field from the document's MRZ, if it has one.

    Args:
        field: The field being resolved; matched to an MRZ field by ``attribute_key``
            first, then by ``name``.
        view: The layout view to search.
        ctx: Locator context (unused beyond the interface, kept for symmetry).

    Returns:
        At most one candidate per MRZ found, ordered verified-first.
    """
    key = _mrz_key_for(field)
    if key is None:
        return []

    out: list[Candidate] = []
    for doc in find_mrz(view):
        value = doc.fields.get(key, "")
        if not value or not passes_pattern(field, value):
            continue
        confidence = _CONF_VERIFIED if doc.checksum_ok else _CONF_PARSED_ONLY
        detail = f"{doc.kind} MRZ field {key}"
        if not doc.checksum_ok:
            detail += f"; check digits NOT verified ({doc.error})"
        out.append(
            Candidate(
                value=value,
                locator="mrz",
                confidence=confidence,
                page=doc.page,
                bbox=doc.bbox,
                raw=doc.block,
                detail=detail,
                extra=dict(doc.fields),
                verified=doc.checksum_ok,
            )
        )
    out.sort(key=lambda c: -c.confidence)
    return out


def _mrz_key_for(field: FieldSpec) -> str | None:
    """Map a FieldSpec onto an MRZ field name, or ``None`` if the MRZ cannot answer it."""
    wanted = {field.attribute_key.strip().lower(), field.name.strip().lower()} - {""}
    for key, aliases in _FIELD_ALIASES.items():
        if wanted & {alias.lower() for alias in aliases}:
            return key
    return None


def find_mrz(view: LayoutView) -> list[MrzDocument]:
    """Find and parse every machine-readable zone in the view.

    MRZ lines survive OCR as their own text blocks, or bundled into one block with
    newlines, or (occasionally) with spaces inserted. All three are normalised here before
    the fixed-offset slicing, which only works on exact line lengths.

    Args:
        view: The layout view to scan.

    Returns:
        Parsed zones in page order; empty when the document has none.
    """
    candidates: list[tuple[int, str, Quad | None]] = []
    for block in view.blocks:
        for line in V.mrz_lines(block.text):
            if _MRZ_LINE_RE.match(line):
                candidates.append((block.page, line, block.bbox))

    out: list[MrzDocument] = []
    index = 0
    while index < len(candidates):
        parsed = _try_parse(candidates, index)
        if parsed is None:
            index += 1
            continue
        doc, consumed = parsed
        out.append(doc)
        index += consumed
    return out


def _try_parse(
    candidates: list[tuple[int, str, Quad | None]], index: int
) -> tuple[MrzDocument, int] | None:
    """Try TD1 (3x30), then TD3 (2x44), then TD2 (2x36) starting at ``index``."""
    page, line, bbox = candidates[index]

    def _run(count: int, length: int) -> list[str] | None:
        if index + count > len(candidates):
            return None
        window = candidates[index : index + count]
        if any(len(text) != length or pg != page for pg, text, _ in window):
            return None
        return [text for _pg, text, _bbox in window]

    for kind, count, length, parser in (
        ("TD1", 3, 30, _parse_td1),
        ("TD3", 2, 44, _parse_td3),
        ("TD2", 2, 36, _parse_td2),
    ):
        if len(line) != length:
            continue
        lines = _run(count, length)
        if lines is None:
            continue
        result = V.validate(f"mrz_{kind.lower()}", "\n".join(lines))
        doc = MrzDocument(
            kind=kind,
            lines=lines,
            fields=parser(lines),
            checksum_ok=result.ok and not result.error,
            error=result.error,
            page=page,
            bbox=bbox,
        )
        return doc, count
    return None


def _parse_td3(lines: list[str]) -> dict[str, str]:
    """Slice a TD3 passport zone by its ICAO 9303 fixed offsets."""
    line1, line2 = lines[0], lines[1]
    surname, given = _split_names(line1[5:44])
    return _finalise(
        {
            "document_code": _strip_filler(line1[0:2]),
            "issuing_state": _strip_filler(line1[2:5]),
            "surname": surname,
            "given_names": given,
            "document_number": _strip_filler(line2[0:9]),
            "nationality": _strip_filler(line2[10:13]),
            "date_of_birth": line2[13:19],
            "sex": line2[20],
            "expiry_date": line2[21:27],
            "personal_number": _strip_filler(line2[28:42]),
        }
    )


def _parse_td2(lines: list[str]) -> dict[str, str]:
    """Slice a TD2 zone by its ICAO 9303 fixed offsets."""
    line1, line2 = lines[0], lines[1]
    surname, given = _split_names(line1[5:36])
    return _finalise(
        {
            "document_code": _strip_filler(line1[0:2]),
            "issuing_state": _strip_filler(line1[2:5]),
            "surname": surname,
            "given_names": given,
            "document_number": _strip_filler(line2[0:9]),
            "nationality": _strip_filler(line2[10:13]),
            "date_of_birth": line2[13:19],
            "sex": line2[20],
            "expiry_date": line2[21:27],
            "optional_data": _strip_filler(line2[28:35]),
        }
    )


def _parse_td1(lines: list[str]) -> dict[str, str]:
    """Slice a TD1 zone; note the name line is third, not first."""
    line1, line2, line3 = lines[0], lines[1], lines[2]
    surname, given = _split_names(line3)
    return _finalise(
        {
            "document_code": _strip_filler(line1[0:2]),
            "issuing_state": _strip_filler(line1[2:5]),
            "document_number": _strip_filler(line1[5:14]),
            "optional_data": _strip_filler(line1[15:30]),
            "date_of_birth": line2[0:6],
            "sex": line2[7],
            "expiry_date": line2[8:14],
            "nationality": _strip_filler(line2[15:18]),
            "surname": surname,
            "given_names": given,
        }
    )


def _finalise(fields: dict[str, str]) -> dict[str, str]:
    """Normalise dates to ISO, sex to a marker, and drop anything that came back empty."""
    out: dict[str, str] = {}
    for key, value in fields.items():
        if key in ("date_of_birth", "expiry_date"):
            parsed = _mrz_date(value, is_birth=key == "date_of_birth")
            out[key] = parsed.isoformat() if parsed else value
        elif key == "sex":
            marker = {"M": "M", "F": "F", "X": "X"}.get(value.strip().upper(), "")
            if marker:
                out[key] = marker
        elif value:
            out[key] = value
    return {k: v for k, v in out.items() if v}


def _split_names(identifier: str) -> tuple[str, str]:
    """Split the ``SURNAME<<GIVEN<NAMES`` identifier field."""
    parts = identifier.split("<<", 1)
    surname = _strip_filler(parts[0])
    given = _strip_filler(parts[1]) if len(parts) > 1 else ""
    return surname, given


def _strip_filler(value: str) -> str:
    """Turn MRZ filler into spaces and squeeze."""
    return re.sub(r"\s+", " ", value.replace("<", " ")).strip()


def _mrz_date(yymmdd: str, *, is_birth: bool) -> date | None:
    """Resolve a 6-digit MRZ date using ICAO century windowing.

    A birth date cannot be in the future, so a two-digit year above the current one belongs
    to the previous century; an expiry date is at or after issuance and resolves forward.
    """
    if not re.fullmatch(r"\d{6}", yymmdd):
        return None
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
    current = datetime.now(UTC).year % 100
    if is_birth:  # noqa: SIM108 - one nested ternary would bury two distinct rules
        # A birth date cannot be in the future.
        century = 1900 if yy > current else 2000
    else:
        # An expiry date is at or after issuance, so it resolves forward.
        century = 2000 if yy <= current + 50 else 1900
    try:
        return date(century + yy, mm, dd)
    except ValueError:
        return None
