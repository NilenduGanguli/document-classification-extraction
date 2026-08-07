"""Shared vocabulary for every locator: candidates, context, and label matching.

A locator is a pure function with one signature::

    locate(field: FieldSpec, view: LayoutView, ctx: LocatorContext) -> list[Candidate]

It proposes; it never decides. Scoring, validation and the pick happen once, in
:mod:`dce.extract.resolve`, so that adding a locator can never change how the winner is
chosen.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from dataclasses import field as dc_field
from functools import lru_cache

from dce.config import Settings, get_settings
from dce.models import DocTypeSpec, FieldSpec, LayoutView, Quad

__all__ = [
    "LOCATOR_PRIOR",
    "Candidate",
    "LocatorContext",
    "clean_value",
    "field_labels",
    "label_similarity",
    "match_label",
    "normalize_label",
    "partition_on_label",
    "passes_pattern",
    "refine_to_pattern",
    "split_on_label",
]

#: How much each locator is trusted before any evidence is looked at. An MRZ or a
#: provider-detected key/value pair is structural; a regex sweep is a guess with a shape.
LOCATOR_PRIOR: dict[str, float] = {
    "mrz": 1.00,
    "kv": 0.95,
    "table": 0.90,
    "mark": 0.90,
    "label": 0.85,
    "regex": 0.70,
}


@dataclass(slots=True)
class Candidate:
    """One proposed value for one field, with the provenance a reviewer needs.

    Attributes:
        value: The value as it will be reported (already trimmed of label furniture).
        locator: Name of the locator that produced it — provenance, and a scoring prior.
        confidence: The locator's own belief, before validation. 0..1.
        page: 1-based page the value was read from.
        bbox: Quad of the region the value was read from, for the review UI.
        raw: The verbatim text the value was carved out of.
        detail: Human-readable provenance ("label 'Date of Birth' -> right", "cell r2c3").
        extra: Sibling values the locator happened to parse (an MRZ yields seven at once).
        verified: The *locator* proved this value's integrity independently of the field's
            own validator. Only an MRZ sets this today: its check digits cover the surname
            and the date of birth just as much as the document number, so a name pulled
            from a verified zone is checksum-verified even though no name validator could
            ever say so.
    """

    value: str
    locator: str
    confidence: float = 0.0
    page: int | None = None
    bbox: Quad | None = None
    raw: str = ""
    detail: str = ""
    extra: dict[str, str] = dc_field(default_factory=dict)
    verified: bool = False


@dataclass(slots=True)
class LocatorContext:
    """Everything a locator may look at besides the field and the page.

    Deliberately small and inert: no clients, no sessions, nothing that could reach the
    network. A locator that needed more than this would be doing something the invariant
    forbids.
    """

    settings: Settings = dc_field(default_factory=get_settings)
    doctype_id: str = ""
    languages: tuple[str, ...] = ("en",)
    #: Doctype-level identifier regexes, used only by the regex locator and only for
    #: ``type="id"`` fields that declare no pattern of their own.
    id_patterns: tuple[str, ...] = ()
    spec: DocTypeSpec | None = None
    #: Passed to validators (``surname`` for PAN's 5th character, ``date_order`` for dates).
    validation_context: dict[str, str] = dc_field(default_factory=dict)

    @classmethod
    def for_view(
        cls,
        view: LayoutView,
        *,
        spec: DocTypeSpec | None = None,
        settings: Settings | None = None,
        doctype_id: str = "",
        id_patterns: tuple[str, ...] = (),
        validation_context: dict[str, str] | None = None,
    ) -> LocatorContext:
        """Build a context from a view, and from the doctype spec when one is known.

        Every field is named explicitly rather than passed through ``**kwargs``: the spec
        supplies defaults for two of them, and a keyword pass-through silently collides
        with those defaults the moment a caller also names them.

        Args:
            view: The layout view about to be extracted from; supplies detected languages.
            spec: The accepted doctype's spec; supplies id patterns and the doctype id.
            settings: Override the process settings (tests pass a tuned copy).
            doctype_id: Used when no spec was supplied.
            id_patterns: Used when no spec was supplied.
            validation_context: Side-channel passed to validators.

        Returns:
            A populated context.
        """
        languages = tuple(dict.fromkeys([*(view.languages or []), "en"]))
        return cls(
            settings=settings or get_settings(),
            doctype_id=spec.doctype_id if spec is not None else doctype_id,
            languages=languages,
            id_patterns=tuple(spec.id_patterns) if spec is not None else id_patterns,
            spec=spec,
            validation_context=dict(validation_context or {}),
        )

    @property
    def min_label_score(self) -> int:
        """The rapidfuzz floor a label match must clear."""
        return int(self.settings.fuzzy_label_min_score)


# ---------------------------------------------------------------------------
# Label normalisation and fuzzy matching
# ---------------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[:\uff1a\-\u2013\u2014_.,;*()\[\]/\\]+")
_WS_RE = re.compile(r"\s+")
_HSPACE_RE = re.compile(r"[^\S\n]+")

#: Below this many characters, ``partial_ratio`` matches almost anything ("DL" scores 100
#: against "middle"), so short labels are matched on whole-token equality only. Same class
#: of bug that ``dce.models.tokenize`` exists to kill.
_PARTIAL_RATIO_MIN_CHARS = 6
#: A one-word label shorter than this is a word, not a name for something. ``To`` and ``AY``
#: are declared labels on real forms, and whole-token containment hands them every line that
#: happens to use the word — ``"To update PAN details, apply for a change request…"`` binds
#: to ``employment_period`` on the strength of its first word. A label of two or more words
#: is distinctive however short its parts are, and a script that writes words in three
#: characters (``नाम``) is not being penalised for it.
_CONTAINMENT_MIN_CHARS = 3


def normalize_label(text: str) -> str:
    """Lower-case, strip punctuation and collapse whitespace for label comparison."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", folded)).strip()


@lru_cache(maxsize=4096)
def _ratios(label: str, text: str) -> tuple[float, float, float]:
    """Return ``(ratio, token_sort, partial)`` for two already-normalised strings."""
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover - rapidfuzz is a declared dependency
        from difflib import SequenceMatcher

        base = SequenceMatcher(None, label, text).ratio() * 100
        sorted_ratio = (
            SequenceMatcher(None, " ".join(sorted(label.split())),
                            " ".join(sorted(text.split()))).ratio() * 100
        )
        return base, sorted_ratio, base
    return (
        float(fuzz.ratio(label, text)),
        float(fuzz.token_sort_ratio(label, text)),
        float(fuzz.partial_ratio(label, text)),
    )


def label_similarity(label: str, text: str) -> float:
    """Score how strongly ``text`` reads as ``label``, 0..100.

    Whole-token containment ("Date of Birth" inside "Date of Birth / Fecha") scores 96 —
    high enough to win, low enough that an exact match still beats it. ``partial_ratio``
    only participates for labels long enough for it to be safe, and only in the direction
    that means anything.
    """
    left, right = normalize_label(label), normalize_label(text)
    if not left or not right:
        return 0.0
    if left == right:
        return 100.0
    ratio, token_sort, partial = _ratios(left, right)
    score = max(ratio, token_sort)
    label_tokens, text_tokens = left.split(), right.split()
    if _is_token_subsequence(label_tokens, text_tokens) and (
        len(label_tokens) > 1 or len(left) >= _CONTAINMENT_MIN_CHARS
    ):
        score = max(score, 96.0)
    if len(left) >= _PARTIAL_RATIO_MIN_CHARS and len(left) <= len(right):
        # ``partial_ratio`` aligns the shorter string inside the longer one whichever way
        # round they are given, so ``Trade Name`` scores 100 against a block that reads only
        # ``Name`` — and every field whose label ends in a common word then claims every
        # block printing that word. The question being asked is "does this text contain my
        # label", which a text shorter than the label cannot.
        score = max(score, partial)
    return score


def _is_token_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """``True`` when ``needle``'s tokens appear consecutively in ``haystack``."""
    if not needle or len(needle) > len(haystack):
        return False
    for start in range(len(haystack) - len(needle) + 1):
        if haystack[start : start + len(needle)] == needle:
            return True
    return False


def field_labels(field: FieldSpec, ctx: LocatorContext) -> list[tuple[str, float]]:
    """Return ``(label, weight)`` pairs for a field, best language first.

    Labels declared for a language the document was detected in weigh full; labels from
    other languages still participate (KYC documents are routinely bilingual) at a small
    discount. A field with no declared labels falls back to its own name, which is what
    makes an induced or minimally-declared schema work at all.
    """
    pairs: list[tuple[str, float]] = []
    seen: set[str] = set()
    for lang, labels in field.labels.items():
        weight = 1.0 if lang in ctx.languages else 0.95
        for label in labels:
            key = normalize_label(label)
            if key and key not in seen:
                seen.add(key)
                pairs.append((label, weight))
    if not pairs:
        fallback = field.name.replace("_", " ").strip()
        if fallback:
            pairs.append((fallback, 0.9))
    pairs.sort(key=lambda pair: -pair[1])
    return pairs


def match_label(text: str, labels: list[tuple[str, float]], min_score: float) -> tuple[str, float]:
    """Best ``(label, weighted_score)`` for ``text``; ``("", 0.0)`` when nothing clears."""
    best_label, best_score = "", 0.0
    for label, weight in labels:
        score = label_similarity(label, text) * weight
        if score > best_score:
            best_label, best_score = label, score
    return (best_label, best_score) if best_score >= min_score else ("", 0.0)


def partition_on_label(text: str, label: str) -> tuple[str, str, str] | None:
    """Split ``text`` around the first printing of ``label``.

    Matches the label's tokens with flexible separators so ``Date of Birth :``,
    ``Date  of Birth-`` and ``DATE OF BIRTH`` all split identically.

    Args:
        text: The line the label was matched in.
        label: The declared label.

    Returns:
        ``(before, matched, after)`` verbatim (over the NFKC-normalised text, which is what
        the match offsets are measured against), or ``None`` when the label is not printed
        in ``text`` at all. **Nothing is cleaned** \u2014 a caller that needs to know whether the
        caption was closed by a colon, or what word preceded it, cannot recover either fact
        once the punctuation has been stripped.
    """
    tokens = [re.escape(tok) for tok in normalize_label(label).split()]
    if not tokens:
        return None
    pattern = r"\b" + r"[\s:.\-\u2013\u2014_]*".join(tokens) + r"\b"
    normalized = unicodedata.normalize("NFKC", text or "")
    match = re.search(pattern, normalized, flags=re.IGNORECASE)
    if match is None:
        return None
    return normalized[: match.start()], match.group(0), normalized[match.end() :]


def split_on_label(text: str, label: str) -> str:
    """Return the value that follows ``label`` on the same line, or ``""``."""
    parts = partition_on_label(text, label)
    return clean_value(parts[2]) if parts is not None else ""


def clean_value(text: str) -> str:
    """Strip the punctuation a label leaves behind, without flattening the value.

    Horizontal whitespace collapses; **line breaks survive**. A multi-line address that
    reaches its validator as one run of words loses the boundaries that let the validator
    normalise it to ``"12 Long Road, Bengaluru 560001"``, and loses them irrecoverably.
    """
    lines = [_HSPACE_RE.sub(" ", line).strip() for line in (text or "").splitlines()]
    return "\n".join(line for line in lines if line).strip(" :\uff1a;,-\u2013\u2014=|")


def passes_pattern(field: FieldSpec, value: str) -> bool:
    """``True`` when the field declares no pattern, or the value contains a match.

    Search semantics, not full-match: a pattern describes the *shape of the value*, and
    real OCR wraps that shape in furniture ("UID: 9999 9999 0011 (masked)"). Callers pair
    this with :func:`refine_to_pattern` to carve the shape back out.
    """
    if not field.pattern:
        return True
    try:
        return re.search(field.pattern, value or "", flags=re.IGNORECASE) is not None
    except re.error:
        # A broken pattern in a doctype declaration must not silently reject every value.
        return True


def refine_to_pattern(field: FieldSpec, value: str) -> str:
    """Narrow ``value`` to the substring its pattern matched, when that is a strict subset."""
    if not field.pattern or not value:
        return value
    try:
        match = re.search(field.pattern, value, flags=re.IGNORECASE)
    except re.error:
        return value
    if match is None:
        return value
    carved = match.group(0).strip()
    return carved or value
