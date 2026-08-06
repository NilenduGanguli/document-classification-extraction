"""Auto-schema induction: propose a draft schema for a document type nobody declared.

Given a handful of examples of the same unseen document, this finds the structure that
*recurs* — the provider key/value keys, the table headers, and the label-like tokens that
appear in most of them — clusters the near-duplicate surface forms into one field each,
guesses a type from the shapes of the values observed, and returns a schema.

Two rules govern the output, and they are not negotiable:

* **The schema comes back inactive.** ``DocSchema.active`` is ``False`` and
  :func:`dce.extract.resolve.resolve` refuses to run an inactive schema. Induction
  proposes; a human activates.
* **Nothing is registered.** The draft is returned to the caller. Auto-registering would
  make the proposal indistinguishable from a declaration the next time anyone looked.

Recurrence is the entire signal. A key that shows up in one of five samples is that
document's own noise; a key in four of five is the form's structure.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dc_field

from dce.extract import validate as V
from dce.extract.locators.base import label_similarity, normalize_label
from dce.extract.schema import DocSchema
from dce.models import FieldSpec, LayoutView

__all__ = ["Observation", "induce_schema", "suggest_type"]

#: A candidate must appear in at least this fraction of the samples to become a field.
DEFAULT_MIN_SUPPORT = 0.6
#: Labels this similar are the same field wearing two OCR outfits.
_CLUSTER_THRESHOLD = 90.0
#: A label-like block is short; a paragraph is not a caption.
_MAX_LABEL_TOKENS = 6
_MAX_LABEL_CHARS = 48
_DRAFT_VERSION = "0.1-draft"


@dataclass(slots=True)
class Observation:
    """One recurring label and everything seen next to it across the samples."""

    label: str
    origin: str                                   # kv | table | label
    documents: set[int] = dc_field(default_factory=set)
    surface_forms: Counter[str] = dc_field(default_factory=Counter)
    values: list[str] = dc_field(default_factory=list)
    languages: set[str] = dc_field(default_factory=set)

    @property
    def support(self) -> int:
        return len(self.documents)


def induce_schema(
    views: list[LayoutView],
    *,
    doctype_id: str,
    version: str = _DRAFT_VERSION,
    min_support: float = DEFAULT_MIN_SUPPORT,
) -> DocSchema:
    """Propose a draft schema from several examples of one document type.

    Args:
        views: Layout views of the same unseen document type. Two is thin; three or more
            is where recurrence starts meaning something.
        doctype_id: Identifier to stamp on the draft.
        version: Draft version string.
        min_support: Fraction of samples a candidate must appear in to become a field.

    Returns:
        A ``source="induced"``, ``active=False`` :class:`~dce.extract.schema.DocSchema`.
        The caller reviews it and calls :meth:`~dce.extract.schema.SchemaRegistry.activate`
        if it is right.
    """
    total = len(views)
    if total == 0:
        return DocSchema(
            doctype_id=doctype_id, version=version, fields=[], active=False,
            source="induced", notes="no samples supplied",
        )

    observations: list[Observation] = []
    for index, view in enumerate(views):
        languages = set(view.languages or [])
        _observe_key_values(view, index, languages, observations)
        _observe_table_headers(view, index, languages, observations)
        _observe_labels(view, index, languages, observations)

    threshold = max(1, round(min_support * total))
    # A caption that recurs but never had a value printed after it is not an extractable
    # field — it is a heading, a legend, or a run of body text that merely looked like a
    # label. Requiring at least one observed value is what keeps those out.
    kept = [obs for obs in observations if obs.support >= threshold and obs.values]
    kept.sort(key=lambda obs: (-obs.support, obs.label))

    fields = [_to_field_spec(obs, total) for obs in kept]
    fields = _dedupe_names(fields)
    return DocSchema(
        doctype_id=doctype_id,
        version=version,
        fields=fields,
        active=False,
        source="induced",
        notes=(
            f"DRAFT induced from {total} sample(s); kept {len(fields)} of "
            f"{len(observations)} candidates at support >= {threshold}/{total}. "
            "Inactive by design — review the labels, types and validators, then activate."
        ),
    )


# ---------------------------------------------------------------------------
# Observation gathering
# ---------------------------------------------------------------------------
def _observe_key_values(
    view: LayoutView, index: int, languages: set[str], into: list[Observation]
) -> None:
    """Provider key/value pairs are the strongest induction signal: keys already named."""
    for pair in view.key_values:
        label = pair.key.strip()
        if label:
            _record(into, label, "kv", index, languages, pair.value)


def _observe_table_headers(
    view: LayoutView, index: int, languages: set[str], into: list[Observation]
) -> None:
    """A recurring table header names a column, and the cell under it holds its values."""
    for table in view.tables:
        for cell in table.cells:
            if not cell.is_header or not cell.text.strip():
                continue
            value = ""
            below = table.cell_at(cell.row + cell.row_span, cell.col)
            if below is not None and not below.is_header:
                value = below.text
            _record(into, cell.text.strip(), "table", index, languages, value)


def _observe_labels(
    view: LayoutView, index: int, languages: set[str], into: list[Observation]
) -> None:
    """Short blocks that read like captions, with something plausible printed after them."""
    blocks = [b for b in view.blocks if b.text.strip()]
    for position, block in enumerate(blocks):
        text = block.text.strip()
        if not _looks_like_label(text):
            continue
        label = text.rstrip(":\uff1a").strip()
        value = ""
        tail = re.split(r"[:\uff1a]", text, maxsplit=1)
        if len(tail) == 2 and tail[1].strip():
            value = tail[1].strip()
            label = tail[0].strip()
        elif position + 1 < len(blocks):
            value = blocks[position + 1].text.strip()
        if label:
            _record(into, label, "label", index, languages, value)


def _looks_like_label(text: str) -> bool:
    """``True`` for a block short enough, and shaped enough, to be a field caption."""
    if len(text) > _MAX_LABEL_CHARS or len(text.split()) > _MAX_LABEL_TOKENS:
        return False
    if text.endswith((":", "\uff1a")):
        return True
    # A caption with no colon must at least look like words, not like a value.
    stripped = unicodedata.normalize("NFKC", text)
    letters = sum(1 for ch in stripped if ch.isalpha())
    return letters >= 3 and letters >= len(stripped) * 0.6


def _record(
    into: list[Observation],
    label: str,
    origin: str,
    index: int,
    languages: set[str],
    value: str,
) -> None:
    """Fold a sighting into the matching cluster, or start a new one.

    Clustering is greedy and single-pass: OCR variants of one caption ("Date of Birth",
    "Date of Birth.", "Date ofBirth") land in the same bucket, and the most frequently seen
    surface form becomes the canonical label.
    """
    normalized = normalize_label(label)
    if not normalized:
        return
    for obs in into:
        if obs.origin == origin and label_similarity(obs.label, label) >= _CLUSTER_THRESHOLD:
            obs.documents.add(index)
            obs.surface_forms[label] += 1
            obs.languages |= languages
            if value.strip():
                obs.values.append(value.strip())
            # Keep the most-seen surface form as the cluster's name.
            obs.label = obs.surface_forms.most_common(1)[0][0]
            return
    observation = Observation(
        label=label, origin=origin, documents={index}, languages=set(languages)
    )
    observation.surface_forms[label] += 1
    if value.strip():
        observation.values.append(value.strip())
    into.append(observation)


# ---------------------------------------------------------------------------
# Field synthesis
# ---------------------------------------------------------------------------
#: Origin -> the locator order that origin implies.
_LOCATORS_BY_ORIGIN: dict[str, list[str]] = {
    "kv": ["kv", "label", "regex"],
    "table": ["table", "kv", "label"],
    "label": ["label", "kv", "regex"],
}


def suggest_type(values: list[str]) -> tuple[str, str | None]:
    """Guess ``(type, validator)`` from the shapes of the values seen under a label.

    Identifier shapes are checked first and only accepted when the *validator agrees* —
    a run of digits that fails its checksum is not evidence of an Aadhaar column. That
    keeps induction from proposing a validator that will reject every real value.
    """
    samples = [v for v in values if v.strip()][:20]
    if not samples:
        return "string", None

    for validator_name in V.ID_PATTERNS:
        hits = sum(1 for value in samples if V.sweep(value, [validator_name]))
        if hits >= max(1, len(samples) * 0.6):
            return "id", validator_name

    def _share(predicate) -> float:
        return sum(1 for value in samples if predicate(value)) / len(samples)

    if _share(lambda v: V.validate("generic_date", v).ok) >= 0.6:
        return "date", "generic_date"
    if _share(lambda v: V.validate("amount", v).ok) >= 0.6:
        return "number", "amount"
    if _share(lambda v: "\n" in v or len(v) > 60 or v.count(",") >= 2) >= 0.6:
        return "address", "address"
    if _share(_looks_like_name) >= 0.6:
        return "name", "name"
    if _share(lambda v: v.strip().casefold() in {"yes", "no", "true", "false", "y", "n"}) >= 0.6:
        return "bool", None
    return "string", None


def _looks_like_name(value: str) -> bool:
    """Two to four capitalised, digit-free words."""
    tokens = value.split()
    if not 1 < len(tokens) <= 4 or any(ch.isdigit() for ch in value):
        return False
    return all(token[:1].isupper() for token in tokens if token)


def _to_field_spec(obs: Observation, total: int) -> FieldSpec:
    """Turn a recurring observation into a proposed field."""
    field_type, validator = suggest_type(obs.values)
    languages = sorted(obs.languages) or ["en"]
    surface_forms = [form for form, _count in obs.surface_forms.most_common(6)]
    labels = {lang: list(surface_forms) for lang in languages}
    sample = "; ".join(obs.values[:3])
    return FieldSpec(
        name=_slug(obs.label),
        attribute_key="",  # a human maps this into the ontology; guessing it would be wrong
        type=field_type,
        required=False,    # induction never asserts a field is mandatory
        pii=field_type in {"id", "name", "address"},
        multi=False,
        labels=labels,
        pattern=None,
        validator=validator,
        locators=_LOCATORS_BY_ORIGIN.get(obs.origin, ["label", "kv", "regex"]),
        notes=(
            f"INDUCED from {obs.origin}: support {obs.support}/{total}"
            + (f"; sample values: {sample}" if sample else "")
        ),
    )


def _slug(label: str) -> str:
    """``"Date of Birth"`` -> ``"date_of_birth"``."""
    slug = re.sub(r"[^\w]+", "_", normalize_label(label), flags=re.UNICODE).strip("_")
    return slug or "field"


def _dedupe_names(fields: list[FieldSpec]) -> list[FieldSpec]:
    """Ensure field names are unique; two origins can name the same thing."""
    seen: Counter[str] = Counter()
    out: list[FieldSpec] = []
    for spec in fields:
        seen[spec.name] += 1
        if seen[spec.name] == 1:
            out.append(spec)
            continue
        out.append(spec.model_copy(update={"name": f"{spec.name}_{seen[spec.name]}"}))
    return out
