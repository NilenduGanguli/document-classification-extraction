"""Per-doctype term profiles: the vocabulary that separates one document type from another.

The lexical tier needs to know which words are *discriminative* for a class. The usual way to
learn that is a labelled corpus, which we do not have yet — and waiting for one would mean
shipping a substring matcher in the meantime, which is how this service got its bug.

So the profiles are derived **declaratively** from the registry itself. Every doctype already
carries the words that characterise it: its anchors, its issuing authority, its human label,
its field labels in every language, and its field names. Those become weighted pseudo-counts,
and the class profile is the set of terms that are surprisingly frequent in *that* class
relative to the pooled vocabulary of *all* classes.

"Surprisingly frequent" is the weighted log-odds ratio with an informative Dirichlet prior
(Monroe, Colaresi & Quinn, *Fightin' Words*, 2008). Compared with raw counts or plain TF-IDF
it does the one thing this service needs: it refuses to get excited about a rare term that
appeared once. ``account`` appears in half the registry and scores near zero; ``renapo``
appears in one doctype and scores high; a typo that appears once scores low because its
variance term punishes it. Every profile weight is a z-score, so the weights are comparable
across classes — which is what makes coverage (see :mod:`dce.classify.lexical`) meaningful.

When a labelled corpus does arrive, :func:`fit_profiles` takes it and runs the *same*
estimator over real counts, optionally still mixing in the declarative pseudo-counts. Callers
do not change: they still ask for a :class:`ProfileSet`.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from dce.models import DocTypeSpec
from dce.normalize import ngrams, normalize, skeletonize

__all__ = [
    "ProfileSet",
    "TermProfile",
    "build_profiles",
    "fit_profiles",
]

#: How many discriminative terms to keep per class. Beyond ~40 the tail is noise that only
#: dilutes coverage.
DEFAULT_TOP_N = 40
#: Dirichlet prior strength. Large relative to the pseudo-counts on purpose: it is what stops
#: a doctype with three anchors from claiming certainty about three words.
DEFAULT_PRIOR_STRENGTH = 500.0
#: Weighted-token length of a "typical" document, used by BM25 length normalisation. A
#: one-page form after zone weighting. Refit it from a corpus when there is one.
DEFAULT_AVG_DOC_LEN = 400.0

#: Pseudo-count weight per declarative source. A decisive anchor is the strongest declaration
#: a registry author can make, so it seeds the profile most heavily.
SOURCE_WEIGHTS: Mapping[str, float] = {
    "decisive_anchor": 6.0,
    "anchor": 3.0,
    "issuing_authority": 3.0,
    "doctype_label": 2.0,
    "field_label": 1.5,
    "field_name": 0.8,
}

#: Function words in the languages the registry is written in (EN/ES/PT/FR). Stored in
#: skeleton form because that is the form every comparison happens in.
_STOPWORD_SOURCE = (
    "a an and or of the to in on for by with no not is are as at from "
    "de del la el los las un una y e o en por con para sobre su sus "
    "da do das dos um uma que se ao aos "
    "le les des du et au aux sur pour"
)
_STOPWORDS: frozenset[str] = frozenset(
    skeletonize(word) for word in _STOPWORD_SOURCE.split()
)


@dataclass(frozen=True)
class TermProfile:
    """One doctype's discriminative vocabulary.

    Attributes:
        doctype_id: The class this profile describes.
        terms: ``term -> weight``, weights normalised to sum to 1.0 so that "how much of this
            class did we actually see" is a fraction, not an unbounded score.
        z_scores: The raw log-odds z-score behind each weight, kept for explainability.
    """

    doctype_id: str
    terms: Mapping[str, float] = field(default_factory=dict)
    z_scores: Mapping[str, float] = field(default_factory=dict)

    def top(self, n: int = 5) -> tuple[tuple[str, float], ...]:
        """Return the ``n`` heaviest terms, for evidence strings."""
        return tuple(sorted(self.terms.items(), key=lambda kv: -kv[1])[:n])


@dataclass(frozen=True)
class ProfileSet:
    """All profiles plus the corpus statistics BM25 needs.

    Attributes:
        profiles: ``doctype_id -> TermProfile``.
        doc_freq: How many profiles contain each term — the ``df`` in IDF. With declarative
            profiles the "corpus" is the registry itself, which is exactly right: a term that
            appears in every doctype's profile carries no information about which one we are
            looking at.
        avg_doc_len: Expected zone-weighted document length, for BM25's ``b`` normalisation.
        fitted_from: ``"registry"`` or ``"corpus"`` — provenance for the audit trail.
    """

    profiles: Mapping[str, TermProfile] = field(default_factory=dict)
    doc_freq: Mapping[str, int] = field(default_factory=dict)
    avg_doc_len: float = DEFAULT_AVG_DOC_LEN
    fitted_from: str = "registry"

    @property
    def n_classes(self) -> int:
        """Number of classes in the set."""
        return len(self.profiles)

    def idf(self, term: str) -> float:
        """Robertson/Sparck-Jones IDF of ``term`` over the profile collection."""
        df = self.doc_freq.get(term, 0)
        n = max(1, self.n_classes)
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))


# ---------------------------------------------------------------------------
# Declarative pseudo-counts
# ---------------------------------------------------------------------------
def _add_phrase(counts: Counter[str], phrase: str, weight: float) -> None:
    """Add a phrase's unigrams and bigrams to ``counts`` at ``weight``.

    Bigrams matter: ``social`` and ``security`` are each generic, ``social security`` is not.
    Unigram stopwords are dropped; a bigram survives if it is not entirely stopwords, so
    ``"estado de cuenta"`` still contributes ``"estado de"`` and ``"de cuenta"``.
    """
    tokens = normalize(phrase).skeleton_tokens
    if not tokens:
        return
    for token in tokens:
        if token not in _STOPWORDS and len(token) > 1:
            counts[token] += weight
    for bigram in ngrams(tokens, 2):
        parts = bigram.split(" ")
        if any(p not in _STOPWORDS for p in parts):
            counts[bigram] += weight * 0.75


def declarative_counts(spec: DocTypeSpec) -> Counter[str]:
    """Turn one :class:`~dce.models.DocTypeSpec` into weighted pseudo-counts.

    Args:
        spec: The doctype declaration.

    Returns:
        ``term -> pseudo-count``. The registry is the training data.
    """
    counts: Counter[str] = Counter()
    for anchor in spec.anchors:
        source = "decisive_anchor" if anchor.decisive else "anchor"
        _add_phrase(counts, anchor.text, SOURCE_WEIGHTS[source])
    if spec.issuing_authority:
        _add_phrase(counts, spec.issuing_authority, SOURCE_WEIGHTS["issuing_authority"])
    if spec.label:
        _add_phrase(counts, spec.label, SOURCE_WEIGHTS["doctype_label"])
    for field_spec in spec.fields:
        for labels in field_spec.labels.values():
            for label in labels:
                _add_phrase(counts, label, SOURCE_WEIGHTS["field_label"])
        _add_phrase(counts, field_spec.name.replace("_", " "), SOURCE_WEIGHTS["field_name"])
    return counts


# ---------------------------------------------------------------------------
# The estimator
# ---------------------------------------------------------------------------
def _log_odds_profiles(
    counts_by_class: Mapping[str, Counter[str]],
    *,
    top_n: int,
    prior_strength: float,
) -> dict[str, TermProfile]:
    """Weighted log-odds with an informative Dirichlet prior, per class.

    For class ``c`` and term ``w``, with background counts ``y_w`` pooled over all classes and
    prior ``alpha_w = a0 * y_w / sum(y)``:

    ``delta = log((y_cw + alpha_w) / (n_c + a0 - y_cw - alpha_w))
              - log((y_w - y_cw + alpha_w) / (n - n_c + a0 - (y_w - y_cw) - alpha_w))``

    with ``var(delta) ~= 1/(y_cw + alpha_w) + 1/(y_w - y_cw + alpha_w)`` and the reported
    weight ``z = delta / sqrt(var)``. Dividing by the standard error is the whole point: it is
    what makes a term seen once in a tiny class score lower than a term seen consistently.

    Args:
        counts_by_class: ``doctype_id -> term counts``.
        top_n: Terms kept per class.
        prior_strength: ``a0``.

    Returns:
        ``doctype_id -> TermProfile``.
    """
    background: Counter[str] = Counter()
    for counts in counts_by_class.values():
        background.update(counts)
    total = float(sum(background.values())) or 1.0
    a0 = float(prior_strength)

    profiles: dict[str, TermProfile] = {}
    for doctype_id, counts in counts_by_class.items():
        n_c = float(sum(counts.values()))
        z_scores: dict[str, float] = {}
        for term, y_cw in counts.items():
            y_w = float(background[term])
            alpha_w = a0 * (y_w / total)
            rest = max(y_w - y_cw, 0.0)

            in_num = y_cw + alpha_w
            in_den = max(n_c + a0 - y_cw - alpha_w, 1e-9)
            out_num = rest + alpha_w
            out_den = max(total - n_c + a0 - rest - alpha_w, 1e-9)

            delta = math.log(in_num / in_den) - math.log(out_num / out_den)
            variance = (1.0 / in_num) + (1.0 / out_num)
            z = delta / math.sqrt(variance) if variance > 0 else 0.0
            if z > 0:
                z_scores[term] = z

        kept = sorted(z_scores.items(), key=lambda kv: -kv[1])[:top_n]
        mass = sum(z for _, z in kept) or 1.0
        profiles[doctype_id] = TermProfile(
            doctype_id=doctype_id,
            terms={term: round(z / mass, 6) for term, z in kept},
            z_scores={term: round(z, 4) for term, z in kept},
        )
    return profiles


def _doc_freq(profiles: Mapping[str, TermProfile]) -> dict[str, int]:
    """Count how many profiles each term appears in."""
    df: Counter[str] = Counter()
    for profile in profiles.values():
        df.update(profile.terms.keys())
    return dict(df)


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------
_CACHE: dict[tuple[object, ...], ProfileSet] = {}


def _fingerprint(specs: Sequence[DocTypeSpec], top_n: int, prior_strength: float) -> tuple:
    """Cheap, exact-enough cache key: what a profile is actually derived from."""
    return (
        top_n,
        prior_strength,
        tuple(
            (
                spec.doctype_id,
                spec.label,
                spec.issuing_authority,
                tuple((a.text, a.decisive) for a in spec.anchors),
                tuple(f.name for f in spec.fields),
                tuple(
                    label
                    for f in spec.fields
                    for labels in f.labels.values()
                    for label in labels
                ),
            )
            for spec in specs
        ),
    )


def build_profiles(
    specs: Iterable[DocTypeSpec],
    *,
    top_n: int = DEFAULT_TOP_N,
    prior_strength: float = DEFAULT_PRIOR_STRENGTH,
    avg_doc_len: float = DEFAULT_AVG_DOC_LEN,
    use_cache: bool = True,
) -> ProfileSet:
    """Derive a :class:`ProfileSet` from the registry, with no corpus.

    Args:
        specs: The doctype registry (or a subset).
        top_n: Discriminative terms kept per class.
        prior_strength: Dirichlet prior ``a0``; higher is more sceptical.
        avg_doc_len: Expected zone-weighted document length for BM25.
        use_cache: Reuse a previously built set for an identical registry. Building is cheap
            but it happens on every request, and the registry does not change per request.

    Returns:
        The profile set, tagged ``fitted_from="registry"``.
    """
    spec_list = list(specs)
    key = _fingerprint(spec_list, top_n, prior_strength) if use_cache else None
    if key is not None and key in _CACHE:
        return _CACHE[key]

    counts = {spec.doctype_id: declarative_counts(spec) for spec in spec_list}
    profiles = _log_odds_profiles(counts, top_n=top_n, prior_strength=prior_strength)
    result = ProfileSet(
        profiles=profiles,
        doc_freq=_doc_freq(profiles),
        avg_doc_len=avg_doc_len,
        fitted_from="registry",
    )
    if key is not None:
        _CACHE[key] = result
    return result


def fit_profiles(
    specs: Iterable[DocTypeSpec],
    corpus: Mapping[str, Sequence[str]],
    *,
    top_n: int = DEFAULT_TOP_N,
    prior_strength: float = DEFAULT_PRIOR_STRENGTH,
    declarative_weight: float = 1.0,
    use_cache: bool = False,
) -> ProfileSet:
    """Re-fit the profiles from a labelled corpus. The documented upgrade path.

    Same estimator, real counts. Once a few hundred labelled documents per class exist, call
    this at startup and hand the result to :func:`dce.classify.cascade.classify` — nothing
    else in the service changes, because the shape of a :class:`ProfileSet` is the contract.

    Keeping ``declarative_weight`` above zero is recommended: the registry's anchors are
    curated knowledge, and a modest corpus should refine them, not overwrite them.

    Args:
        specs: The doctype registry.
        corpus: ``doctype_id -> raw document texts`` for that class. Classes absent from the
            corpus keep their declarative-only profile.
        top_n: Discriminative terms kept per class.
        prior_strength: Dirichlet prior ``a0``. Raise it with corpus size.
        declarative_weight: Multiplier on the registry pseudo-counts before pooling with the
            observed counts. ``0.0`` fits from the corpus alone.
        use_cache: Off by default; a fit is done once at startup, not per request. There is no
            ``avg_doc_len`` argument here — it is measured from the corpus.

    Returns:
        The profile set, tagged ``fitted_from="corpus"``.
    """
    spec_list = list(specs)
    counts_by_class: dict[str, Counter[str]] = {}
    lengths: list[float] = []

    for spec in spec_list:
        counts: Counter[str] = Counter()
        if declarative_weight:
            for term, value in declarative_counts(spec).items():
                counts[term] += value * declarative_weight
        for document in corpus.get(spec.doctype_id, ()):
            tokens = normalize(document).skeleton_tokens
            lengths.append(float(len(tokens)))
            for token in tokens:
                if token not in _STOPWORDS and len(token) > 1:
                    counts[token] += 1.0
            for bigram in ngrams(tokens, 2):
                if any(p not in _STOPWORDS for p in bigram.split(" ")):
                    counts[bigram] += 0.75
        counts_by_class[spec.doctype_id] = counts

    profiles = _log_odds_profiles(
        counts_by_class, top_n=top_n, prior_strength=prior_strength
    )
    avg_len = (sum(lengths) / len(lengths)) if lengths else DEFAULT_AVG_DOC_LEN
    result = ProfileSet(
        profiles=profiles,
        doc_freq=_doc_freq(profiles),
        avg_doc_len=avg_len,
        fitted_from="corpus",
    )
    if use_cache:
        _CACHE[_fingerprint(spec_list, top_n, prior_strength)] = result
    return result
