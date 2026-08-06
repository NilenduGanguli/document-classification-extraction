"""L2 — zone-weighted BM25 over the per-doctype term profiles.

This is the tier that decides most real documents, because most real documents do not carry a
checksummed identifier. Three things make it better than counting keywords, and all three come
free from the layout payload we were already given:

**Zone weighting.** A term in the title is not the same evidence as the same term in a page
footer. Term frequency is accumulated *per zone* and multiplied by that zone's weight (title
3.0 … furniture 0.25) before BM25 sees it. "STATEMENT" printed once as a heading outranks
"statement" repeated on every footer, which is the correct reading of the page and something a
flat bag of words gets exactly backwards.

**BM25 saturation.** ``k1`` bounds what repetition can buy, ``b`` normalises for document
length. A ten-page bundle cannot win by volume.

**Coverage.** The one that stops the failure this service exists to prevent. Two doctypes that
share a few generic terms will both score; coverage asks *what fraction of the class's own
profile mass did we actually observe*. A bank statement matching two of the utility bill's
forty profile terms has high overlap and low coverage — and the cascade refuses to accept a
class whose coverage is below the floor, no matter how the softmax came out.

Raw scores become probabilities through a robust z-score (median/MAD, so one runaway class
cannot drag the scale) and a temperature softmax, with a Platt-scaling hook that is the
identity until somebody fits it on labelled data.
"""
from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field

from dce.config import Settings, get_settings
from dce.models import LayoutView, Zone
from dce.normalize import fold, ngrams, normalize, skeletonize

from .profiles import ProfileSet

__all__ = [
    "DocumentTerms",
    "LexicalOutcome",
    "PlattCalibration",
    "document_terms",
    "lexical_scores",
    "robust_z",
    "softmax",
]

#: Below this, MAD is treated as degenerate and we fall back to the standard deviation.
_MIN_SCALE = 1e-6

#: Tokens this short only count when the document printed them in caps — the same rule the
#: anchor tier applies, for the same reason. ``sin`` is Spanish for "without" and ``SIN`` is a
#: Canadian identity document; the difference between them is capitalisation, and it is the
#: only signal available. Longer terms are not gated: ``passport`` is ``passport`` in any case.
_CASE_SENSITIVE_MAX_CHARS = 3
_CASED_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class DocumentTerms:
    """The document as zone-weighted term counts.

    Attributes:
        counts: ``term -> weighted frequency``. Contains unigrams and bigrams.
        length: Zone-weighted unigram length, BM25's ``dl``.
        zone_lengths: Unweighted token count per zone, for evidence and debugging.
    """

    counts: Mapping[str, float] = field(default_factory=dict)
    length: float = 0.0
    zone_lengths: Mapping[str, int] = field(default_factory=dict)


def _caps_skeletons(view: LayoutView) -> frozenset[str]:
    """Skeleton forms of every token the document printed in ALL CAPS.

    Used to gate short terms. Built from the raw text of blocks, table cells and key/value
    pairs, before any case folding — this is the one place where the original capitalisation
    still exists, and throwing it away is what let ``sin``/``SIN`` collide.
    """
    parts = [b.text for b in view.blocks]
    parts.extend(c.text for t in view.tables for c in t.cells if c.text)
    parts.extend(f"{kv.key} {kv.value}" for kv in view.key_values)
    return frozenset(
        skeletonize(fold(token))
        for token in _CASED_WORD_RE.findall("\n".join(parts))
        if token.isupper()
    )


def _zone_weights(settings: Settings) -> dict[Zone, float]:
    return {
        Zone.title: settings.zone_weight_title,
        Zone.heading: settings.zone_weight_heading,
        Zone.body: settings.zone_weight_body,
        Zone.table: settings.zone_weight_table,
        Zone.furniture: settings.zone_weight_furniture,
    }


def document_terms(
    view: LayoutView, *, settings: Settings | None = None
) -> DocumentTerms:
    """Accumulate zone-weighted term counts for a layout payload.

    Table cells are counted at the table weight and provider key/value pairs at the body
    weight. Selection marks contribute nothing lexically — they are structure, and L0 already
    read them.

    Args:
        view: The layout payload.
        settings: Settings override; defaults to :func:`dce.config.get_settings`.

    Returns:
        The :class:`DocumentTerms` view of the document.
    """
    resolved = settings if settings is not None else get_settings()
    weights = _zone_weights(resolved)
    counts: Counter[str] = Counter()
    zone_lengths: Counter[str] = Counter()
    length = 0.0
    caps = _caps_skeletons(view)

    def absorb(text: str, zone: Zone) -> None:
        nonlocal length
        tokens = normalize(text).skeleton_tokens
        if not tokens:
            return
        weight = weights.get(zone, resolved.zone_weight_body)
        for token in tokens:
            if len(token) <= _CASE_SENSITIVE_MAX_CHARS and token not in caps:
                continue
            counts[token] += weight
        # Bigrams ride at the same zone weight; they are not counted into ``dl`` because BM25
        # length normalisation is defined over the unigram token stream.
        for bigram in ngrams(tokens, 2):
            counts[bigram] += weight
        length += weight * len(tokens)
        zone_lengths[zone.value] += len(tokens)

    for block in view.blocks:
        absorb(block.text, block.zone)
    for table in view.tables:
        for cell in table.cells:
            absorb(cell.text, Zone.table)
    for kv in view.key_values:
        absorb(f"{kv.key} {kv.value}", Zone.body)

    return DocumentTerms(
        counts=dict(counts), length=length, zone_lengths=dict(zone_lengths)
    )


@dataclass(frozen=True)
class PlattCalibration:
    """Platt scaling on the log-odds of the softmax probability.

    The default ``a=1, b=0`` is the identity, so nothing is calibrated until somebody fits it.
    This exists so that fitting later is a configuration change rather than a code change —
    callers already pass a calibration through, they just pass the identity today.
    """

    a: float = 1.0
    b: float = 0.0

    @property
    def is_identity(self) -> bool:
        """Whether this calibration leaves probabilities untouched."""
        return self.a == 1.0 and self.b == 0.0

    def apply(self, probability: float) -> float:
        """Map a raw probability through the fitted sigmoid.

        Args:
            probability: Uncalibrated probability in ``[0, 1]``.

        Returns:
            The calibrated probability.
        """
        if self.is_identity:
            return probability
        p = min(max(probability, 1e-9), 1 - 1e-9)
        logit = math.log(p / (1 - p))
        return 1.0 / (1.0 + math.exp(-(self.a * logit + self.b)))

    @classmethod
    def fit(
        cls,
        probabilities: list[float],
        labels: list[int],
        *,
        iterations: int = 500,
        learning_rate: float = 0.05,
    ) -> PlattCalibration:
        """Fit ``a`` and ``b`` by gradient descent on log-loss.

        The documented calibration hook: give it the uncalibrated top-class probabilities from
        a labelled validation set and 1/0 correctness labels, store the result, and pass it to
        :func:`lexical_scores`.

        Args:
            probabilities: Uncalibrated probabilities.
            labels: 1 when the classification was correct, else 0.
            iterations: Gradient steps.
            learning_rate: Step size.

        Returns:
            The fitted calibration (the identity when there is nothing to fit).
        """
        if not probabilities or len(probabilities) != len(labels):
            return cls()
        logits = [
            math.log(min(max(p, 1e-9), 1 - 1e-9) / (1 - min(max(p, 1e-9), 1 - 1e-9)))
            for p in probabilities
        ]
        a, b = 1.0, 0.0
        n = float(len(logits))
        for _ in range(iterations):
            grad_a = grad_b = 0.0
            for logit, label in zip(logits, labels, strict=True):
                pred = 1.0 / (1.0 + math.exp(-(a * logit + b)))
                error = pred - label
                grad_a += error * logit
                grad_b += error
            a -= learning_rate * grad_a / n
            b -= learning_rate * grad_b / n
        return cls(a=a, b=b)


@dataclass(frozen=True)
class LexicalOutcome:
    """What L2 concluded.

    Attributes:
        raw: BM25 score per doctype.
        z: Robust z-score of ``raw``.
        probability: Calibrated probability per doctype (sums to 1 across the registry).
        coverage: Fraction of each class profile's weighted mass that was observed.
        matched: The profile terms that fired per doctype, with their profile weights.
        terms: The document's zone-weighted term counts (reused for evidence).
    """

    raw: Mapping[str, float] = field(default_factory=dict)
    z: Mapping[str, float] = field(default_factory=dict)
    probability: Mapping[str, float] = field(default_factory=dict)
    coverage: Mapping[str, float] = field(default_factory=dict)
    matched: Mapping[str, tuple[tuple[str, float], ...]] = field(default_factory=dict)
    terms: DocumentTerms = field(default_factory=DocumentTerms)


def robust_z(scores: Mapping[str, float]) -> dict[str, float]:
    """Standardise scores with median/MAD rather than mean/stdev.

    One class scoring far above the rest is the *normal* case here, and it would inflate a
    mean-based scale enough to flatten everything else. The median absolute deviation is
    unaffected by it.

    Args:
        scores: Raw scores per class.

    Returns:
        Z-scores per class; all zeros when every score is identical.
    """
    if not scores:
        return {}
    values = list(scores.values())
    centre = statistics.median(values)
    deviations = [abs(v - centre) for v in values]
    scale = 1.4826 * statistics.median(deviations)
    if scale < _MIN_SCALE:
        scale = statistics.pstdev(values) if len(values) > 1 else 0.0
    if scale < _MIN_SCALE:
        return dict.fromkeys(scores, 0.0)
    return {k: (v - centre) / scale for k, v in scores.items()}


def softmax(scores: Mapping[str, float], temperature: float) -> dict[str, float]:
    """Temperature softmax over a score mapping.

    Args:
        scores: Scores per class.
        temperature: ``T``; lower is sharper. Non-positive values are clamped.

    Returns:
        Probabilities summing to 1.0 (uniform when ``scores`` is empty of variation).
    """
    if not scores:
        return {}
    t = max(float(temperature), 1e-3)
    top = max(scores.values())
    exponentials = {k: math.exp((v - top) / t) for k, v in scores.items()}
    total = sum(exponentials.values()) or 1.0
    return {k: v / total for k, v in exponentials.items()}


def lexical_scores(
    view: LayoutView,
    profiles: ProfileSet,
    *,
    settings: Settings | None = None,
    calibration: PlattCalibration | None = None,
    terms: DocumentTerms | None = None,
) -> LexicalOutcome:
    """Score every doctype profile against the document with zone-weighted BM25.

    Args:
        view: The layout payload.
        profiles: The profile set to score against.
        settings: Settings override; defaults to :func:`dce.config.get_settings`.
        calibration: Platt calibration; defaults to the identity.
        terms: Pre-computed document terms, when the caller already built them.

    Returns:
        The :class:`LexicalOutcome`.
    """
    resolved = settings if settings is not None else get_settings()
    platt = calibration or PlattCalibration()
    doc = terms if terms is not None else document_terms(view, settings=resolved)

    k1 = resolved.bm25_k1
    b = resolved.bm25_b
    avgdl = profiles.avg_doc_len or 1.0
    norm = k1 * (1.0 - b + b * (doc.length / avgdl))

    raw: dict[str, float] = {}
    coverage: dict[str, float] = {}
    matched: dict[str, tuple[tuple[str, float], ...]] = {}

    for doctype_id, profile in profiles.profiles.items():
        score = 0.0
        seen_mass = 0.0
        total_mass = 0.0
        hits: list[tuple[str, float]] = []
        for term, weight in profile.terms.items():
            total_mass += weight
            tf = doc.counts.get(term, 0.0)
            if tf <= 0.0:
                continue
            seen_mass += weight
            hits.append((term, weight))
            score += weight * profiles.idf(term) * (tf * (k1 + 1.0)) / (tf + norm)
        raw[doctype_id] = round(score, 6)
        coverage[doctype_id] = round(seen_mass / total_mass, 6) if total_mass else 0.0
        matched[doctype_id] = tuple(sorted(hits, key=lambda kv: -kv[1]))

    z = robust_z(raw)
    probability = {
        k: round(platt.apply(v), 6)
        for k, v in softmax(z, resolved.softmax_temperature).items()
    }
    return LexicalOutcome(
        raw=raw,
        z={k: round(v, 6) for k, v in z.items()},
        probability=probability,
        coverage=coverage,
        matched=matched,
        terms=doc,
    )
