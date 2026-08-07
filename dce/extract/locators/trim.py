"""Span trimming: stop at the next label, then tighten the span to the field's shape.

Every layout-anchored locator does the same two-step: find the label, then take the text
next to it. The second step is where accuracy is lost, because "the text next to it" has no
natural right-hand end. The locator reads to the end of the block, the end of the cell or
the end of the provider's value string — and a form line carries *several* fields::

    Signature of U.S. person: J. Smith  Date: 2026-03-14

Anchoring ``signature_date`` on ``Signature of U.S. person`` and taking the rest of the line
yields ``"J. Smith Date: 2026-03-14"``: a confidently wrong value, which is worse than a
blank one because nothing downstream can tell it was wrong.

This module is the shared fix, used by the label, key/value and table locators:

1. :func:`cut_at_next_label` — **a value never contains a subsequent field's label.** The
   span terminates at the first caption inside it: any label declared by any field of the
   active schema (plus, for a key/value or table lookup, the other keys and header cells the
   provider detected), or the generic "word immediately before a colon" shape that a caption
   has on every form ever printed.
2. :func:`tighten` — **a typed field has a shape.** For ``date`` / ``number`` / ``id``, the
   longest substring of the span that satisfies the field's pattern (or its validator, or
   the default shape for its type) beats the raw span. Untyped fields — ``name``,
   ``address``, ``string`` — are never tightened, because an address that happens to contain
   a date must not be trimmed down to that date.

Both steps are reported, not silent: :class:`Trimmed` carries a confidence penalty and a
provenance note, so :mod:`dce.extract.resolve` ranks a binding that needed surgery below an
equally-scored one that did not. Tightness is evidence.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from functools import lru_cache
from typing import NamedTuple

from dce.extract import validate as V
from dce.extract.locators.base import (
    LocatorContext,
    clean_value,
    match_label,
    normalize_label,
)
from dce.models import FieldSpec

__all__ = [
    "CONNECTORS",
    "Trimmed",
    "cut_at_next_label",
    "has_shape",
    "has_substance",
    "has_type_shape",
    "known_labels",
    "reads_as_caption",
    "starts_with_caption",
    "strip_fill_rules",
    "tighten",
    "trim_span",
]

#: A span that had to be cut short of its next label was over-captured; the binding was
#: still real, so this discounts rather than rejects.
CUT_PENALTY = 0.85
#: A span that had to be narrowed to the field's shape carried furniture with it.
TIGHTEN_PENALTY = 0.90
#: How much of a block a matched caption must account for before the block is dismissed as
#: a caption outright, rather than trimmed as a value that happens to be followed by one.
_CAPTION_COVERAGE = 0.6

#: Types whose values have a machine-checkable shape. Everything else (``name``,
#: ``address``, ``string``, ``bool``) is prose and is never tightened.
_SHAPED_TYPES = frozenset({"date", "number", "id"})

#: ASCII and fullwidth colon — the caption marker on every form this service sees.
_COLON = "[:\uff1a]"
_COLON_RE = re.compile(_COLON)
#: A caption word; the walk back from a colon steps over these.
_WORD_RE = re.compile(r"[^\W\d_][\w.&\u2019'/-]*")
#: Words that never *start* a caption but routinely sit inside one ("Place **of** Issue",
#: "Fecha **de** nacimiento", "Nombre **del** contribuyente"). They are what lets the walk
#: back from a colon reach a multi-word caption without reaching into the value in front of
#: it: a token boundary is only crossed when one of the two tokens is a connector. So
#: "J. Smith  Date:" stops at ``Date`` — ``Smith`` and ``Date`` are both content words —
#: while "Anna Eriksson  Place of Issue:" reaches back to ``Place``.
#: A caption is "distinctive" — safe to match without a colon — at this many words, or
#: this many characters in one word.
_DISTINCTIVE_WORDS = 2
_DISTINCTIVE_CHARS = 10

CONNECTORS = frozenset(
    {
        "of", "the", "a", "an", "and", "for", "in", "on", "to", "or",
        "de", "del", "la", "el", "los", "las", "y", "en", "por",
        "du", "des", "d", "et", "da", "das", "dos", "do",
    }
)
_CONNECTORS = CONNECTORS

#: Printed fill-in furniture: the rule a blank form leaves for a human to write on, and the
#: dot leader a printed one uses instead. Never part of a value, in any document type.
_FILL_RULE_RE = re.compile(r"_{2,}|\.{4,}|…+|·{4,}|•{2,}")
#: Collapses the whitespace a removed fill rule leaves behind, per line.
_HSPACE_RE = re.compile(r"[^\S\n]+")


def strip_fill_rules(text: str) -> str:
    """Remove the blank-form fill rule (``____``, ``....``, ``…``) from a span.

    A rule is where a value *would* be written, so it is furniture in exactly the sense a
    caption is: ``"Verified today, the _____ day"`` carries no value, and ``"________"``
    on its own is an empty field, not a value that happens to look odd. Removing it is what
    lets :func:`has_substance` tell the two apart.
    """
    if not text:
        return ""
    lines = [
        _HSPACE_RE.sub(" ", _FILL_RULE_RE.sub(" ", line)).strip()
        for line in text.splitlines()
    ]
    return "\n".join(line for line in lines if line)


def has_substance(text: str) -> bool:
    """``True`` when a span carries something a value could be made of.

    A span with no alphanumeric character left once the fill rule is gone — ``"________"``,
    ``"."``, ``"-- / --"`` — is punctuation the locator captured, not a value. Reporting it
    fills a KYC field with a mark off the page, which is strictly worse than leaving the
    field empty for a human.
    """
    return any(ch.isalnum() for ch in strip_fill_rules(text or ""))

#: Human-written dates, in the four shapes :func:`dce.extract.validate.generic_date` parses.
_DATE_SHAPE = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
    r"|\d{1,2}\s*(?:de\s+)?[^\W\d_]{3,12}\.?,?\s*(?:de\s+)?\d{4}"
    r"|[^\W\d_]{3,12}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}",
    re.IGNORECASE,
)
#: An amount: digits with any of the four separator conventions, optionally signed/bracketed.
_NUMBER_SHAPE = re.compile(r"-?\(?[ \t]*\d[\d,.' \t]*\d[ \t]*\)?|-?\d")
#: An identifier: runs of upper-case letters and digits, at least one digit among them.
#: Case-sensitive on purpose — a lower-cased word is prose, not an identifier.
_ID_SHAPE = re.compile(r"[0-9A-Z]+(?:[ /-][0-9A-Z]+)*")

_TYPE_SHAPES: dict[str, re.Pattern[str]] = {
    "date": _DATE_SHAPE,
    "number": _NUMBER_SHAPE,
    "id": _ID_SHAPE,
}


class Trimmed(NamedTuple):
    """The outcome of trimming one span.

    Attributes:
        value: The span as it should be reported.
        cut: The span was terminated at a following label.
        tightened: The span was narrowed to the field's pattern/type shape.
        note: Human-readable provenance for the candidate's ``detail``.
    """

    value: str
    cut: bool = False
    tightened: bool = False
    note: str = ""

    @property
    def clean(self) -> bool:
        """``True`` when the locator's span needed no surgery at all."""
        return not (self.cut or self.tightened)

    @property
    def penalty(self) -> float:
        """Multiplier on the locator's confidence — tighter is better."""
        factor = 1.0
        if self.cut:
            factor *= CUT_PENALTY
        if self.tightened:
            factor *= TIGHTEN_PENALTY
        return factor


# ---------------------------------------------------------------------------
# Known labels
# ---------------------------------------------------------------------------
def known_labels(
    field: FieldSpec, ctx: LocatorContext, *, extra: Iterable[str] = ()
) -> tuple[str, ...]:
    """Every caption that could legally follow this field's value.

    That is: the labels of **every** field on the active doctype — including this field's
    own *other* labels, which is not a corner case (``signature_date`` on a W-9 declares
    both ``"Signature of U.S. person"`` and ``"Date"``, and they sit on the same printed
    line) — plus anything the caller knows is a caption on this particular page.

    Args:
        field: The field being resolved; always contributes its own labels.
        ctx: Locator context; ``ctx.spec`` supplies the sibling fields when it is known.
        extra: Page-specific captions — the other provider-detected keys, the table's
            header cells.

    Returns:
        Labels in declaration order, de-duplicated by normalised form.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        key = normalize_label(text)
        if key and key not in seen:
            seen.add(key)
            out.append(text)

    specs = [field, *(ctx.spec.fields if ctx.spec is not None else ())]
    for spec_field in specs:
        declared = [lab for labels in spec_field.labels.values() for lab in labels]
        for label in declared:
            add(label)
        if not declared:
            # A field with no declared labels is looked up by its own name, so its name is
            # what would be printed as its caption.
            add(spec_field.name.replace("_", " "))
    for text in extra:
        add(text)
    return tuple(out)


def reads_as_caption(text: str, captions: Iterable[str], min_score: float) -> bool:
    """``True`` when a block or cell **is** a caption rather than a value.

    The block to the right of ``Date of Birth`` on a two-column form is frequently the next
    field's caption, and binding it hands the reviewer a field whose value is another
    field's name.

    "Is" is load-bearing. A block that merely *contains* a caption is a value with a caption
    printed after it, and that is :func:`cut_at_next_label`'s job, not a reason to throw the
    block away — so the matched caption has to account for most of the text.
    :func:`~dce.extract.locators.base.match_label` on its own scores 96 for a caption buried
    anywhere inside a long line, which would reject every such value.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.endswith((":", "\uff1a")):
        return True
    matched, _score = match_label(stripped, [(c, 1.0) for c in captions], min_score)
    if not matched:
        return False
    return len(normalize_label(matched)) >= _CAPTION_COVERAGE * len(normalize_label(stripped))


def _is_distinctive(label: str) -> bool:
    """``True`` when a caption is specific enough to be recognised without a colon."""
    normalized = normalize_label(label)
    return len(normalized.split()) >= _DISTINCTIVE_WORDS or len(normalized) >= _DISTINCTIVE_CHARS


@lru_cache(maxsize=2048)
def _label_patterns(label: str) -> tuple[re.Pattern[str], ...]:
    """Compile the ways a known label can appear as a caption inside a span."""
    tokens = [re.escape(tok) for tok in normalize_label(label).split()]
    if not tokens:
        return ()
    body = r"[\s:.\-\u2013\u2014_]*".join(tokens)
    patterns = (
        # "... Date: 2026-03-14" — the caption marker makes this unambiguous.
        re.compile(rf"(?:(?<=\s)|^){body}[ \t]*{_COLON}", re.IGNORECASE | re.MULTILINE),
        # A caption printed on a line of its own, which is how stacked forms separate
        # a multi-line value from the next field.
        re.compile(rf"^[ \t]*{body}[ \t]*{_COLON}?[ \t]*$", re.IGNORECASE | re.MULTILINE),
    )
    if _is_distinctive(label):
        # A caption with no colon at all. A PAN card prints
        # "Name  ANNA ERIKSSON   Father's Name  BO ERIKSSON" on one line and punctuates
        # none of it, so requiring a colon would leave that value swallowed whole.
        #
        # Only *distinctive* captions get this, because it is the one rule that can cut a
        # value at a word the value legitimately contains. A declared multi-word caption
        # appearing verbatim inside someone's name or address does not happen; a short
        # common one ("Date", "Name") happens constantly, and those still need their colon.
        patterns += (
            re.compile(rf"(?:(?<=\s)|^){body}(?![\w\u2019\'])", re.IGNORECASE | re.MULTILINE),
        )
    return patterns


def _covered_by(label: str, matched: str) -> bool:
    """``True`` when ``label`` is the caption we anchored on, or part of it.

    Anchoring on ``"Date (MM-DD-YYYY)"`` must not then cut the value at ``"Date"``.
    """
    left, right = normalize_label(label).split(), normalize_label(matched).split()
    if not left or not right:
        return False
    return any(right[i : i + len(left)] == left for i in range(len(right) - len(left) + 1))


def _caption_starts(value: str, labels: Iterable[str], skip: str) -> Iterator[int]:
    """Yield every offset in ``value`` at which a following field's caption begins.

    Two sources, and both are needed. A *known* label catches multi-word and non-Latin
    captions ("Fecha de nacimiento", a Devanagari caption) that no generic shape could
    recognise; the colon scan catches the captions of fields this schema never declared,
    which on a real form is most of them.
    """
    for label in labels:
        if _covered_by(label, skip):
            continue
        for pattern in _label_patterns(label):
            for match in pattern.finditer(value):
                text = match.group(0)
                # The own-line pattern matches the line's indent too; report the caption.
                yield match.start() + (len(text) - len(text.lstrip()))
    for colon in _COLON_RE.finditer(value):
        head = value[: colon.start()]
        start = _colon_caption_start(head)
        if start is not None and not _covered_by(head[start:], skip):
            yield start


def _colon_caption_start(head: str) -> int | None:
    """Offset in ``head`` at which the caption closed by the colon after it begins.

    Walks back from the colon over caption words, crossing a token boundary only when one
    of the two tokens is a connector (see :data:`_CONNECTORS`). Anything else is treated as
    the end of the value that precedes the caption, which is the whole point: without that
    stop, ``"J. Smith  Date:"`` walks all the way back to ``J.`` and the cut removes the
    value instead of the furniture.
    """
    words = list(_WORD_RE.finditer(head))
    if not words:
        return None
    chosen = words[-1]
    if head[chosen.end() :].strip(" \t"):
        return None  # something other than spaces between the word and the colon
    for index in range(len(words) - 2, -1, -1):
        previous = words[index]
        if head[previous.end() : chosen.start()].strip(" \t"):
            break  # punctuation between them: two phrases, not one caption
        if (
            previous.group(0).casefold() not in _CONNECTORS
            and chosen.group(0).casefold() not in _CONNECTORS
        ):
            break
        chosen = previous
    return chosen.start()


def cut_at_next_label(
    value: str, labels: Iterable[str] = (), *, matched: str = ""
) -> tuple[str, bool]:
    """Terminate ``value`` at the first caption inside it.

    Args:
        value: The captured span.
        labels: Known captions (see :func:`known_labels`).
        matched: The label this span was anchored on; it and its sub-phrases are ignored,
            since a span already starts *after* its own caption.

    Returns:
        ``(value, cut)`` — the span up to the caption, and whether anything was removed. A
        caption at offset 0 is left alone: there is nothing in front of it to keep, and
        :func:`tighten` is a better tool for that case than deleting the whole span.
    """
    if not value:
        return value, False
    starts = [start for start in _caption_starts(value, labels, matched) if start > 0]
    if not starts:
        return value, False
    head = clean_value(value[: min(starts)])
    if not head:
        return value, False
    return head, True


def starts_with_caption(value: str, labels: Iterable[str] = (), *, matched: str = "") -> bool:
    """``True`` when a span *begins* with another field's caption.

    The mirror image of :func:`cut_at_next_label`, which deliberately ignores a caption at
    offset 0 because there is nothing in front of it to keep. For a span carved out of the
    line its own label was printed on, a caption sitting at offset 0 means something else:
    the label matched **part of a longer caption**, and everything after it is the rest of
    that caption rather than a value.

    ``Last name (in capital letters) -- Nom de famille`` is the case. Anchoring ``full_name``
    on ``Last Name`` leaves the French half of the same caption, which
    :func:`reads_as_caption` will not reject — the parenthetical means the caption accounts
    for well under its coverage floor — and which would otherwise be reported as somebody's
    surname.
    """
    if not value:
        return False
    return any(start == 0 for start in _caption_starts(value, labels, matched))


# ---------------------------------------------------------------------------
# Type-aware tightening
# ---------------------------------------------------------------------------
def _shape_for(field: FieldSpec) -> tuple[re.Pattern[str] | None, str]:
    """The tightest shape known for a field, and the note to record when it is used."""
    if field.pattern:
        try:
            return re.compile(field.pattern, re.IGNORECASE), "narrowed to pattern"
        except re.error:
            # A broken declaration must not reject every value; fall through to the type.
            pass
    if field.validator:
        default = V.ID_PATTERNS.get(field.validator)
        if default:
            return re.compile(default, re.IGNORECASE), f"narrowed to {field.validator} shape"
    if field.type in _SHAPED_TYPES:
        return _TYPE_SHAPES[field.type], f"tightened to {field.type}"
    return None, ""


def _shape_matches(shape: re.Pattern[str], value: str) -> list[re.Match[str]]:
    """Shape matches in ``value`` that are whole tokens, not the front of a longer one.

    ``\\d{1,3}(?:,\\d{3})*`` finds ``101`` inside ``"Enter on line 10100."`` and ``201``
    inside ``"January 1, 2016"``. Both satisfy the pattern; neither is a number printed on
    the document. A truncated number reported as a KYC amount is the exact failure this
    module exists to prevent — it is confidently wrong, and nothing downstream can tell.

    So a match is kept only when the character on either side of it is not a digit: the
    shape has to account for the whole numeric token it sits in. A match bounded by
    ``$``, a space, a comma or the end of the span is a value; one bounded by ``0`` is a
    piece of one.
    """
    kept: list[re.Match[str]] = []
    for match in shape.finditer(value):
        before = value[match.start() - 1] if match.start() > 0 else ""
        after = value[match.end()] if match.end() < len(value) else ""
        if before.isdigit() or after.isdigit():
            continue
        kept.append(match)
    return kept


def has_shape(field: FieldSpec) -> bool:
    """``True`` when this field's values are machine-recognisable at all.

    True for ``date``, ``number`` and ``id``, and for any field declaring a pattern or a
    validator with a known one. False for ``name``, ``address`` and ``string``, whose values
    are prose and look exactly like the captions printed around them.
    """
    return _shape_for(field)[0] is not None


def has_type_shape(field: FieldSpec, value: str) -> bool:
    """``True`` when ``value`` contains this field's shape as a whole token.

    A field with no known shape (``name``, ``address``, ``string``) always passes: prose has
    no shape to check it against.
    """
    shape, _note = _shape_for(field)
    if shape is None:
        return True
    found = _shape_matches(shape, value or "")
    if field.type == "id" and not field.pattern and not field.validator:
        # Same rule :func:`tighten` narrows by: an identifier declared only as ``type="id"``
        # is a run of capitals and digits *containing a digit*. Without it, the leading
        # capital of any ordinary word satisfies the shape — ``"Content"``, ``"Marriages"``
        # — and the check passes on prose it was written to reject.
        found = [m for m in found if any(ch.isdigit() for ch in m.group(0))]
    return bool(found)


def tighten(field: FieldSpec, value: str) -> tuple[str, bool, str]:
    """Narrow ``value`` to the longest substring that satisfies the field's shape.

    Longest, not leftmost: ``"J. Smith Date: 2026-03-14"`` read as a date must come back as
    ``"2026-03-14"`` and not as ``"2026"``. When the field also declares a validator, only
    substrings the validator accepts are considered — and if none is accepted the raw span
    survives untouched, so a value that fails its checksum still reaches the reviewer
    verbatim instead of being silently replaced by some other digit run.

    Args:
        field: The field being resolved.
        value: The captured span.

    Returns:
        ``(value, tightened, note)``.
    """
    shape, note = _shape_for(field)
    if shape is None or not value:
        return value, False, ""
    found = [match.group(0).strip() for match in _shape_matches(shape, value)]
    if field.type == "id" and not field.pattern and not field.validator:
        found = [item for item in found if any(ch.isdigit() for ch in item)]
    found = [item for item in found if item]
    if not found:
        return value, False, ""
    if field.validator:
        accepted = [item for item in found if V.validate(field.validator, item).ok]
        found = accepted or found
    best = max(found, key=len)
    if best == value:
        return value, False, ""
    return best, True, note


def trim_span(
    field: FieldSpec, value: str, *, labels: Iterable[str] = (), matched: str = ""
) -> Trimmed:
    """Run both trimming steps over one captured span.

    Args:
        field: The field being resolved.
        value: The span the locator captured, already cleaned.
        labels: Known captions (see :func:`known_labels`).
        matched: The label the span was anchored on.

    Returns:
        The :class:`Trimmed` outcome. ``Trimmed("")`` — which every caller reads as "no
        candidate" — when the span is only fill rule and punctuation, because there is
        nothing there for a locator to propose or for a reviewer to read.
    """
    cleaned = clean_value(strip_fill_rules(value or ""))
    if not cleaned or not has_substance(cleaned):
        return Trimmed("")
    cut_value, cut = cut_at_next_label(cleaned, labels, matched=matched)
    tight_value, tightened, note = tighten(field, cut_value)
    notes = []
    if cut:
        notes.append("cut at next label")
    if tightened and note:
        notes.append(note)
    return Trimmed(clean_value(tight_value), cut, tightened, "; ".join(notes))
