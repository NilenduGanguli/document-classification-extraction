"""Text normalisation shared by every classification tier.

OCR text is not the text that was printed. It is mis-cased, mis-accented, and littered with
the classic confusions (``O``/``0``, ``I``/``l``/``1``, ``S``/``5``, ``B``/``8``). Anchors and
term profiles are written the way a human would write them, so every comparison in this
service happens between two *normalised* forms, never between a hand-written string and a raw
OCR dump.

Three forms, in increasing aggression:

``raw``
    Exactly what we were given. Line structure is preserved because the MRZ sweep needs it.
``folded``
    NFKC + case-fold + whitespace collapse. Still accented: ``"Constancia de Situación"``.
    This is the conservative form; an exact token match here is the strongest lexical signal.
``deaccented``
    ``folded`` with Latin diacritics stripped (NFKD, drop combining marks) and nothing else:
    ``"situación"`` → ``"situacion"``. Readable, and the right form for anything that wants
    accent-insensitivity without the aggression below.
``skeleton``
    ``deaccented`` with the OCR confusions folded onto a single canonical symbol.
    ``"situación"`` becomes ``"51tuac10n"`` and so does ``"SITUACION"`` and so does a scan that
    read it as ``"5ITUACI0N"``. This is the *matching* form — both sides of every comparison in
    the classifier are skeletonised, which makes the collapse symmetric rather than lossy.

Two deliberate constraints:

* The OCR confusion fold is applied **only** to the skeleton. It is lossy — it would ruin an
  identifier — and both sides of every comparison are skeletonised, so the collapse is
  symmetric rather than destructive.
* Diacritic stripping is applied **only** to Latin characters. Devanagari, Bengali, Tamil,
  Arabic and friends keep their marks: in those scripts a "combining mark" is a vowel, not an
  accent, and stripping it changes the word. ``"आधार"`` must stay ``"आधार"``.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache

__all__ = [
    "NormalizedText",
    "deaccent",
    "fold",
    "ngrams",
    "normalize",
    "skeletonize",
    "tokenize_unicode",
]

_WHITESPACE_RE = re.compile(r"\s+")

#: OCR confusion classes folded onto one canonical symbol each. Letters map onto the digit
#: because digits map onto themselves, which makes the fold idempotent and symmetric: the
#: printed ``O`` and the OCR'd ``0`` both end up as ``0``.
_CONFUSION_TABLE = str.maketrans({"o": "0", "i": "1", "l": "1", "s": "5", "b": "8"})


@lru_cache(maxsize=8192)
def _is_latin(ch: str) -> bool:
    """Return whether ``ch`` is an ASCII or Latin-script character.

    Used to decide whether diacritic stripping is safe. Combining marks themselves are not
    Latin by name (``COMBINING ACUTE ACCENT``), so a mark on a non-Latin base is preserved.
    """
    if ch.isascii():
        return True
    return unicodedata.name(ch, "").startswith("LATIN")


def fold(text: str) -> str:
    """Return the conservative form: NFKC, case-folded, whitespace-collapsed.

    Args:
        text: Any string.

    Returns:
        The folded form. Accents and non-Latin scripts are untouched.
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def deaccent(folded_text: str) -> str:
    """Strip Latin diacritics only — no OCR confusion folding.

    This is the halfway form, and it is exposed because it is the one a *human* recognises:
    ``"situación"`` becomes ``"situacion"``, not ``"51tuac10n"``. Use it for display, for logs,
    and for any comparison that should be accent-insensitive but nothing more.

    Non-Latin scripts pass through untouched: in Devanagari, Bengali or Tamil a combining mark
    is a vowel, not an accent, and dropping it changes the word.

    Args:
        folded_text: Output of :func:`fold` (or any string; it is not re-folded).

    Returns:
        The accent-stripped form.
    """
    if not folded_text:
        return ""
    out: list[str] = []
    for ch in folded_text:
        if _is_latin(ch):
            decomposed = unicodedata.normalize("NFKD", ch)
            out.append("".join(c for c in decomposed if not unicodedata.combining(c)))
        else:
            out.append(ch)
    return "".join(out)


def skeletonize(folded_text: str) -> str:
    """Return the aggressive form: Latin diacritics stripped **and** OCR confusions folded.

    ``skeletonize(x) == deaccent(x).translate(OCR confusions)``. This is the matching form —
    both sides of every comparison in the classifier are skeletonised, so the collapse is
    symmetric. It is not meant to be readable: ``"PASSPORT"`` and a scan that read it as
    ``"PA5SPORT"`` both become ``"pa55p0rt"``, which is the entire point.

    Args:
        folded_text: Output of :func:`fold` (or any string; it is not re-folded).

    Returns:
        The skeleton form. Non-Latin scripts pass through unchanged.
    """
    return deaccent(folded_text).translate(_CONFUSION_TABLE)


#: Unicode categories of combining marks: non-spacing, spacing-combining, enclosing.
_MARK_CATEGORIES = frozenset({"Mn", "Mc", "Me"})


def tokenize_unicode(text: str) -> tuple[str, ...]:
    """Tokenise, keeping Indic vowel signs attached to the syllable they belong to.

    :func:`dce.models.tokenize` is the service-wide contract and uses ``[^\\W_]+``. That is
    correct for Latin but wrong for Devanagari and its neighbours: Python's ``\\w`` excludes
    Unicode category ``Mc`` (spacing combining marks), so ``"सरकार"`` splits into ``"सरक"`` and
    ``"र"`` — the vowel signs fall out as separators. A matra is not punctuation; it is part of
    the word.

    This tokeniser accepts alphanumerics *plus* combining marks, which for Latin and digits is
    character-for-character identical to ``[^\\W_]+`` and for Indic scripts keeps words whole.

    Args:
        text: Text to tokenise (already folded, in practice).

    Returns:
        The tokens, in order.
    """
    tokens: list[str] = []
    current: list[str] = []
    for ch in text:
        if ch.isalnum():
            current.append(ch)
        elif current and unicodedata.category(ch) in _MARK_CATEGORIES:
            # A mark only continues a token; it can never start one.
            current.append(ch)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


@dataclass(frozen=True)
class NormalizedText:
    """The three forms of one piece of text plus their tokenisations.

    Attributes:
        raw: The input, verbatim (line structure intact — the MRZ sweep depends on it).
        folded: NFKC + case-fold + whitespace collapse. Accents intact.
        skeleton: ``folded`` with Latin diacritics stripped **and** OCR confusions folded.
            This is the matching form; it is not human-readable by design. If you want the
            accent-stripped-but-readable form, use ``deaccented``.
        tokens: Unicode word tokens of ``folded``.
        token_counts: Frequency of each token in ``tokens``.
        deaccented: ``folded`` with Latin diacritics stripped and nothing else.
        skeleton_tokens: Unicode word tokens of ``skeleton`` — what the tiers match on.
        skeleton_counts: Frequency of each token in ``skeleton_tokens``.
    """

    raw: str
    folded: str
    skeleton: str
    tokens: tuple[str, ...]
    token_counts: Mapping[str, int]
    deaccented: str = ""
    skeleton_tokens: tuple[str, ...] = ()
    skeleton_counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing to match against."""
        return not self.skeleton_tokens


@lru_cache(maxsize=1024)
def _normalize_cached(text: str) -> NormalizedText:
    folded = fold(text)
    deaccented = deaccent(folded)
    skeleton = deaccented.translate(_CONFUSION_TABLE)
    tokens = tokenize_unicode(folded)
    skeleton_tokens = tokenize_unicode(skeleton)
    return NormalizedText(
        raw=text,
        folded=folded,
        skeleton=skeleton,
        tokens=tokens,
        token_counts=dict(Counter(tokens)),
        deaccented=deaccented,
        skeleton_tokens=skeleton_tokens,
        skeleton_counts=dict(Counter(skeleton_tokens)),
    )


def normalize(text: str) -> NormalizedText:
    """Normalise ``text`` into all three forms.

    Cached: every tier normalises the same anchors and the same zone text repeatedly, and the
    cost is dominated by the per-character Unicode work.

    Args:
        text: Any string, including the empty string.

    Returns:
        The :class:`NormalizedText` view of it.
    """
    return _normalize_cached(text or "")


def ngrams(tokens: Sequence[str], n: int) -> Iterable[str]:
    """Yield space-joined contiguous ``n``-grams of ``tokens``.

    Bigrams are what let a profile keep ``"social security"`` as one discriminative term
    rather than two terms that both fire on unrelated documents.

    Args:
        tokens: Token sequence.
        n: Gram size (>= 1).

    Yields:
        Each contiguous n-gram, joined with single spaces.
    """
    if n < 1:
        return
    for i in range(len(tokens) - n + 1):
        yield " ".join(tokens[i : i + n])
