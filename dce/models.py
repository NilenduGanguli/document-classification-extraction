"""Core value types: the layout view, doctype specs, classification and extracted fields.

Every module codes against these. Two shapes carry the weight:

* :class:`LayoutView` — a provider-neutral read of an OCR payload. Azure Document
  Intelligence ``prebuilt-layout`` is the reference producer, but nothing below imports
  Azure: a caller can hand us any payload that maps onto this, which is what lets other
  business units use the service without adopting our OCR stack.
* :class:`DocTypeSpec` — one document type's *classification anchors* and its *extraction
  fields*, declared together. Keeping them in one object is deliberate: the thing that
  tells you "this is an Aadhaar card" and the thing that tells you "an Aadhaar card has a
  12-digit UID with a Verhoeff check" are the same knowledge, and they drift apart the
  moment you split them across two files.
"""
from __future__ import annotations

import enum
import re
from typing import Any

from pydantic import BaseModel, Field

Quad = list[float]  # 8 floats: 4 (x, y) points, clockwise from top-left


# ---------------------------------------------------------------------------
# Layout view — provider-neutral
# ---------------------------------------------------------------------------
class Zone(enum.StrEnum):
    """Where on the page a piece of text sits. Drives lexical weighting."""

    title = "title"
    heading = "heading"
    body = "body"
    table = "table"
    furniture = "furniture"      # pageHeader / pageFooter / pageNumber — repeated noise


class TextBlock(BaseModel):
    """One paragraph/line of text with its zone and geometry."""

    text: str
    zone: Zone = Zone.body
    page: int = 1
    bbox: Quad | None = None
    role: str | None = None      # verbatim provider role, when it had one


class Cell(BaseModel):
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    text: str = ""
    is_header: bool = False
    bbox: Quad | None = None


class Table(BaseModel):
    table_id: str
    page: int = 1
    row_count: int = 0
    col_count: int = 0
    cells: list[Cell] = Field(default_factory=list)
    bbox: Quad | None = None

    def cell_at(self, row: int, col: int) -> Cell | None:
        for c in self.cells:
            if (c.row <= row < c.row + c.row_span) and (c.col <= col < c.col + c.col_span):
                return c
        return None


class Mark(BaseModel):
    """A checkbox / radio. On a KYC form, which box is ticked is often the answer."""

    state: str                   # "selected" | "unselected"
    page: int = 1
    bbox: Quad | None = None

    @property
    def selected(self) -> bool:
        return self.state == "selected"


class KeyValue(BaseModel):
    """A provider-detected key/value pair (Azure ``features=keyValuePairs``)."""

    key: str
    value: str
    page: int = 1
    key_bbox: Quad | None = None
    value_bbox: Quad | None = None
    confidence: float | None = None


class PageInfo(BaseModel):
    page: int
    width: float = 0.0
    height: float = 0.0
    unit: str = "pixel"
    angle: float = 0.0


class LayoutView(BaseModel):
    """Everything the classifier and extractors are allowed to see.

    Constructed by an adapter (``dce.adapters``) from a provider payload. Deliberately has
    no notion of *where the bytes came from* — the service never needs the original file.
    """

    doc_id: str = ""
    pages: list[PageInfo] = Field(default_factory=list)
    blocks: list[TextBlock] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    marks: list[Mark] = Field(default_factory=list)
    key_values: list[KeyValue] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pages) or (max((b.page for b in self.blocks), default=0))

    def text(self, *, include_furniture: bool = True) -> str:
        return "\n".join(
            b.text for b in self.blocks
            if include_furniture or b.zone is not Zone.furniture
        )

    def zone_text(self, zone: Zone) -> str:
        return "\n".join(b.text for b in self.blocks if b.zone is zone)

    @property
    def has_structure(self) -> bool:
        return bool(self.tables or self.key_values or
                    any(b.zone is not Zone.body for b in self.blocks))


# ---------------------------------------------------------------------------
# Doctype registry
# ---------------------------------------------------------------------------
class Category(enum.StrEnum):
    identity = "identity"
    address_proof = "address_proof"
    tax = "tax"
    corporate = "corporate"
    financial = "financial"
    other = "other"


class FieldSpec(BaseModel):
    """One extractable field on a document type."""

    name: str                                   # snake_case, e.g. "aadhaar_number"
    attribute_key: str = ""                     # maps into the fleet ontology, e.g. "id.aadhaar"
    type: str = "string"                        # string|date|number|name|address|id|bool
    required: bool = False
    pii: bool = False
    multi: bool = False
    #: Labels that appear next to the value, per language. Drives label-anchored lookup.
    labels: dict[str, list[str]] = Field(default_factory=dict)
    #: Value-shape regex; also used to reject a wrong binding (an address must not bind
    #: to a date just because the label matched).
    pattern: str | None = None
    #: Named validator in dce.extract.validate (e.g. "verhoeff_aadhaar", "pan", "curp").
    validator: str | None = None
    #: Locator hints in priority order, e.g. ["kv", "label", "table", "regex", "mrz"].
    locators: list[str] = Field(default_factory=lambda: ["kv", "label", "regex"])
    notes: str = ""


class Anchor(BaseModel):
    """A high-signal string that appears in this doctype's OCR dump."""

    text: str
    lang: str = "en"
    #: A decisive anchor alone is near-proof of the doctype (an issuing-authority header,
    #: a form number). Non-decisive anchors only contribute to the lexical score.
    decisive: bool = False
    zone: Zone | None = None     # when set, only counts if found in that zone


class DocTypeSpec(BaseModel):
    """A document type: how to recognise it, and what to pull out of it."""

    doctype_id: str                              # e.g. "in_aadhaar", "us_w9", "mx_ine"
    label: str
    country: str                                 # IN | US | CA | MX | XX (cross-country)
    category: Category = Category.other
    issuing_authority: str = ""
    applies_to: str = "individual"               # individual | corporate
    #: RBI "Officially Valid Document" and equivalents — regulatory weight, not just a tag.
    officially_valid: bool = False

    anchors: list[Anchor] = Field(default_factory=list)
    #: Identifier regexes that, when they match AND their checksum validates, are decisive.
    id_patterns: list[str] = Field(default_factory=list)
    #: Doctypes this is most confused with, and the term that separates them.
    confusable_with: dict[str, str] = Field(default_factory=dict)
    #: Terms that, if present, argue AGAINST this doctype.
    negative_anchors: list[str] = Field(default_factory=list)

    fields: list[FieldSpec] = Field(default_factory=list)
    #: Handling constraints that are legal, not technical (e.g. UIDAI Aadhaar masking).
    handling: str = ""

    def anchor_texts(self, lang: str | None = None) -> list[str]:
        return [a.text for a in self.anchors if lang is None or a.lang == lang]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
UNKNOWN = "unknown"


class Evidence(BaseModel):
    """Why the classifier believed something. Always populated — an unexplainable
    classification is not auditable, and this is a KYC system."""

    tier: str                    # "anchor" | "checksum" | "lexical" | "bert" | "structural"
    detail: str
    weight: float = 0.0


class Classification(BaseModel):
    doctype_id: str = UNKNOWN
    label: str = ""
    country: str = ""
    confidence: float = 0.0
    margin: float = 0.0          # over the runner-up
    coverage: float = 0.0        # fraction of the class profile actually observed
    abstained: bool = False
    reason: str = ""             # why abstained, when it did
    evidence: list[Evidence] = Field(default_factory=list)
    runners_up: list[tuple[str, float]] = Field(default_factory=list)
    page_types: list[str] = Field(default_factory=list)   # per-page, for merged PDFs
    ms: int = 0


class ExtractedField(BaseModel):
    name: str
    attribute_key: str = ""
    value: str | None = None
    normalized: str | None = None     # canonical form (dates ISO, ids stripped)
    confidence: float = 0.0
    #: unverified | format_valid | checksum_verified | cross_verified | human_verified
    verification: str = "unverified"
    locator: str = ""                 # which locator found it — provenance for review
    page: int | None = None
    bbox: Quad | None = None
    pii: bool = False
    validator_error: str = ""


class ExtractionResult(BaseModel):
    doctype_id: str = UNKNOWN
    schema_version: str = ""
    fields: list[ExtractedField] = Field(default_factory=list)
    missing_required: list[str] = Field(default_factory=list)
    needs_review: bool = False
    ms: int = 0

    @property
    def fill_rate(self) -> float:
        if not self.fields:
            return 0.0
        return sum(1 for f in self.fields if f.value) / len(self.fields)


#: Combining marks (Unicode categories Mn/Mc). Python's ``re`` has no ``\p{M}``, and
#: ``\w`` does NOT include marks, so a naive ``[^\W_]+`` splits every Indic word at each
#: matra: "आधार" tokenised to ["आध", "र"]. Since the India pack carries 123 Devanagari
#: anchors, that silently breaks classification for every bilingual Indian document — the
#: exact failure this service exists to avoid. These ranges cover the Indic blocks plus
#: general/Latin/Arabic/Hebrew combining marks.
_MARKS = (
    # Generated from unicodedata: every Mn/Mc codepoint in the Latin/Greek/Cyrillic/
    # Hebrew/Arabic combining blocks and the whole Indic range U+0900-U+0DFF
    # (Devanagari, Bengali, Gurmukhi, Gujarati, Odia, Tamil, Telugu, Kannada,
    # Malayalam, Sinhala). Written as escapes, not literals: the literal forms include
    # characters that are visually ambiguous with ASCII punctuation, which makes the
    # class unreviewable and trips RUF001.
    "\u0300-\u036f\u0483-\u0487\u0591-\u05bd\u05bf\u05c1-\u05c2\u05c4-\u05c5\u05c7\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06dc\u0900-\u0903\u093a-\u093c\u093e-\u094f\u0951-\u0957\u0962-\u0963\u0981-\u0983\u09bc\u09be-\u09c4\u09c7-\u09c8\u09cb-\u09cd\u09d7\u09e2-\u09e3\u09fe\u0a01-\u0a03\u0a3c\u0a3e-\u0a42\u0a47-\u0a48\u0a4b-\u0a4d\u0a51\u0a70-\u0a71\u0a75\u0a81-\u0a83\u0abc\u0abe-\u0ac5\u0ac7-\u0ac9\u0acb-\u0acd\u0ae2-\u0ae3\u0afa-\u0aff\u0b01-\u0b03\u0b3c\u0b3e-\u0b44\u0b47-\u0b48\u0b4b-\u0b4d\u0b55-\u0b57\u0b62-\u0b63\u0b82\u0bbe-\u0bc2\u0bc6-\u0bc8\u0bca-\u0bcd\u0bd7\u0c00-\u0c04\u0c3c\u0c3e-\u0c44\u0c46-\u0c48\u0c4a-\u0c4d\u0c55-\u0c56\u0c62-\u0c63\u0c81-\u0c83\u0cbc\u0cbe-\u0cc4\u0cc6-\u0cc8\u0cca-\u0ccd\u0cd5-\u0cd6\u0ce2-\u0ce3\u0cf3\u0d00-\u0d03\u0d3b-\u0d3c\u0d3e-\u0d44\u0d46-\u0d48\u0d4a-\u0d4d\u0d57\u0d62-\u0d63\u0d81-\u0d83\u0dca\u0dcf-\u0dd4\u0dd6\u0dd8-\u0ddf\u0df2-\u0df3"
)
#: A token starts with a letter/digit and may continue with letters, digits or marks.
_WORD_RE = re.compile(rf"[^\W_][\w{_MARKS}]*", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Unicode word tokenisation — the fix for the substring false-positive class.

    Matching anchors with ``needle in haystack`` made ``DL`` fire inside "mi**dl**e",
    ``EIN`` inside "b**ein**g" and ``SIN`` inside "u**sin**g". Everything lexical works on
    tokens, never on raw substrings.

    Combining marks are treated as word continuation so Indic scripts survive intact
    (see :data:`_MARKS`); accented Latin is preserved as-is and folded separately by
    :mod:`dce.normalize`.
    """
    return _WORD_RE.findall(text.lower())
