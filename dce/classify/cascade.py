"""The cascade: L0 → L1 → L2 → (L3) → accept or abstain.

Cheap and certain first, expensive and uncertain last, and a hard refusal at the end.

===========  =====================================================================
Tier         What it contributes
===========  =====================================================================
L0           ``log P(c|structure)`` — page count, shape, tables, marks, MRZ shape.
L1           Anchors and checksums. A checksum-verified decisive identifier
             short-circuits the whole thing and never pays for BM25.
L2           Zone-weighted BM25 over the per-doctype term profiles, plus coverage.
L3           Optional local BERT kNN. Off by default, imported only when on.
L4           Abstain → ``UNKNOWN`` → human queue.
===========  =====================================================================

Fusion is a weighted sum, with the weights from config::

    score_c = log P(c|structure) + 3.0*anchor + 1.0*lexical + 0.8*bert

and a temperature softmax turns the fused scores into a probability. Acceptance requires
**all three** of:

* ``p >= classify_accept_probability`` — we are confident in the winner;
* ``margin >= classify_min_margin`` — and confident it is not the runner-up;
* ``coverage >= classify_min_coverage`` — and we actually saw enough of that class to say so.

Coverage is the condition that carries its weight. Probability and margin are relative: with
a registry of twenty doctypes, the least-wrong class always wins something. Coverage is
absolute — it asks how much of the class's own vocabulary was present — and it is what turns
"the closest thing in the registry" into "nothing in the registry", which is the answer a KYC
system should give for a document it has never seen.

**L4 is a feature, not a failure.** An abstention routes a document to a human queue. It never
routes it to a model: the whole point of this service is that unclassified content does not
leave the process, and "ask an LLM what this is" is that leak wearing a different hat.
"""
from __future__ import annotations

import importlib
import pkgutil
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from dce.config import Settings, get_settings
from dce.egress import EgressViolation, classification_scope
from dce.models import UNKNOWN, Classification, DocTypeSpec, Evidence, LayoutView

from . import anchors as anchors_tier
from . import structural as structural_tier
from .lexical import LexicalOutcome, PlattCalibration, lexical_scores, softmax
from .profiles import ProfileSet, build_profiles

__all__ = [
    "Segment",
    "classify",
    "classify_pages",
    "load_registry",
]

#: How far ahead of the runner-up an anchor score must be before a checksum-verified doctype
#: is allowed to skip the lexical tier. Prevents a valid CURP on an INE card from short-
#: circuiting to "CURP document" when the card's own anchors are equally strong.
_MIN_SHORT_CIRCUIT_LEAD = 0.10
#: Evidence entries kept per tier so the audit trail stays readable.
_MAX_EVIDENCE_PER_TIER = 4
#: Confidence granted when a UNIQUE decisive anchor matched but no checksum verified.
#: Below the checksum path deliberately — we are sure what the document is, less sure the
#: identifier on it is intact.
_DECISIVE_ANCHOR_CONFIDENCE = 0.90


@dataclass(frozen=True)
class Segment:
    """A run of consecutive pages that classified the same way.

    Merged PDFs are the normal case in KYC: a customer uploads one file containing a passport,
    two utility bills and a bank statement. Classifying the bundle as a whole answers the
    wrong question.

    Attributes:
        doctype_id: The class of the run (``UNKNOWN`` for a run of abstentions).
        start_page: First page of the run (1-based, inclusive).
        end_page: Last page of the run (inclusive).
        confidence: Mean confidence across the run's pages.
        classification: The full classification of the run's first page, with
            ``page_types`` covering the run.
    """

    doctype_id: str
    start_page: int
    end_page: int
    confidence: float
    classification: Classification

    @property
    def page_count(self) -> int:
        """Number of pages in the run."""
        return self.end_page - self.start_page + 1


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------
def load_registry() -> list[DocTypeSpec]:
    """Load the doctype registry, tolerating its absence.

    The registry package is owned elsewhere in the service, so this reads it through its
    documented surface rather than its internals: ``all_specs()`` on the package or on
    ``dce.registry.loader``, or a ``SPECS``-like collection. Country packs register themselves
    at import, so an empty registry triggers one pass of importing the package's own modules
    before giving up — a service that silently classified against zero doctypes would abstain
    on everything and look like a model problem.

    Returns:
        The doctype specs, or ``[]`` when no registry is installed.
    """
    for module_name in ("dce.registry", "dce.registry.loader"):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - a registry that fails to import must degrade to
            # "no registry" (every document abstains to a human) rather than take the service
            # down. A pack with a syntax error is a deploy problem, not a request problem.
            continue
        specs = _specs_from(module)
        if specs:
            return specs
        if _import_registry_packs():
            specs = _specs_from(module)
            if specs:
                return specs
    return []


def _specs_from(module: Any) -> list[DocTypeSpec]:
    """Pull specs out of a registry module via its documented exports."""
    for name in ("all_specs", "load_registry", "specs", "doctypes"):
        candidate = getattr(module, name, None)
        if callable(candidate):
            try:
                return _as_specs(candidate())
            except Exception:  # noqa: BLE001 - this export did not behave like the documented
                # accessor; try the next shape rather than assuming the registry is broken.
                continue
    for name in ("SPECS", "DOCTYPES", "DOC_TYPES", "REGISTRY"):
        candidate = getattr(module, name, None)
        if candidate is not None:
            return _as_specs(candidate)
    return []


def _import_registry_packs() -> bool:
    """Import every module in ``dce.registry`` so the country packs self-register.

    Returns:
        Whether at least one pack module was imported.
    """
    try:
        package = importlib.import_module("dce.registry")
        paths = list(getattr(package, "__path__", ()))
    except Exception:  # noqa: BLE001 - no registry package at all is a valid state (see above).
        return False
    imported = False
    for info in pkgutil.iter_modules(paths):
        if info.name.startswith("_") or info.name == "loader":
            continue
        try:
            importlib.import_module(f"dce.registry.{info.name}")
            imported = True
        except Exception:  # noqa: BLE001 - one broken country pack must not cost us the other
            # twenty. The registry package owns its own validation and reporting.
            continue
    return imported


def _as_specs(value: Any) -> list[DocTypeSpec]:
    """Coerce a registry export (list or mapping) into a list of specs."""
    if isinstance(value, Mapping):
        value = list(value.values())
    return [item for item in value if isinstance(item, DocTypeSpec)]


# ---------------------------------------------------------------------------
# The cascade
# ---------------------------------------------------------------------------
def classify(
    view: LayoutView,
    specs: Iterable[DocTypeSpec] | None = None,
    *,
    settings: Settings | None = None,
    profiles: ProfileSet | None = None,
    calibration: PlattCalibration | None = None,
) -> Classification:
    """Classify one document, entirely in-process.

    Args:
        view: The layout payload.
        specs: Doctype registry; defaults to :func:`load_registry`.
        settings: Settings override; defaults to :func:`dce.config.get_settings`.
        profiles: Pre-built term profiles; defaults to profiles derived from ``specs``.
        calibration: Platt calibration for the lexical tier; defaults to the identity.

    Returns:
        A fully populated :class:`~dce.models.Classification` — confidence, margin, coverage,
        evidence, runners-up and elapsed milliseconds — with ``abstained`` set and a
        human-readable ``reason`` when it declines.
    """
    started = time.perf_counter()
    resolved = settings if settings is not None else get_settings()
    spec_list = list(specs) if specs is not None else load_registry()

    # Everything below runs inside the scope, so any code that reaches for the network —
    # now or after somebody's future refactor — trips dce.egress.assert_no_egress.
    with classification_scope():
        if not spec_list:
            return _abstain(
                reason="no doctype registry is loaded; nothing to classify against",
                evidence=[],
                runners_up=[],
                started=started,
            )

        features = structural_tier.structural_features(view)
        priors = structural_tier.structural_log_priors(features, spec_list)
        anchor = anchors_tier.anchor_scores(view, spec_list, settings=resolved)

        short_circuit = _short_circuit(anchor, spec_list)
        if short_circuit is not None:
            return _accept_short_circuit(
                doctype_id=short_circuit,
                spec_list=spec_list,
                features=features,
                anchor=anchor,
                started=started,
            )

        decisive = _decisive_short_circuit(anchor, spec_list)
        if decisive is not None:
            return _accept_short_circuit(
                doctype_id=decisive,
                spec_list=spec_list,
                features=features,
                anchor=anchor,
                started=started,
                confidence=_DECISIVE_ANCHOR_CONFIDENCE,
                note=(
                    "unique decisive anchor (issuing-authority header or form number) — "
                    "accepted at L1 without a checksum. Classification answers *what "
                    "document this is*; whether an identifier on it validates is a "
                    "separate question that sets the extracted field's verification "
                    "status, not the doctype"
                ),
            )

        resolved_profiles = profiles or build_profiles(spec_list)
        lexical = lexical_scores(
            view, resolved_profiles, settings=resolved, calibration=calibration
        )
        bert = _bert_scores(view, resolved)

        fused = {
            spec.doctype_id: (
                priors.get(spec.doctype_id, 0.0)
                + resolved.fuse_weight_anchor * anchor.scores.get(spec.doctype_id, 0.0)
                + resolved.fuse_weight_lexical * lexical.probability.get(spec.doctype_id, 0.0)
                + resolved.fuse_weight_bert * bert.get(spec.doctype_id, 0.0)
            )
            for spec in spec_list
        }
        probabilities = softmax(fused, resolved.softmax_temperature)
        ranked = sorted(probabilities.items(), key=lambda kv: -kv[1])

        top_id, top_p = ranked[0]
        runner_p = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = top_p - runner_p
        coverage = lexical.coverage.get(top_id, 0.0)

        evidence = _evidence(
            features=features,
            priors=priors,
            anchor=anchor,
            lexical=lexical,
            bert=bert,
            doctype_id=top_id,
        )
        runners_up = [(doctype, round(p, 6)) for doctype, p in ranked[1:4]]

        failures = _accept_failures(
            probability=top_p, margin=margin, coverage=coverage, settings=resolved
        )
        if failures:
            return _abstain(
                reason=(
                    f"best candidate {top_id!r} at p={top_p:.3f}, margin={margin:.3f}, "
                    f"coverage={coverage:.3f} — " + "; ".join(failures) +
                    ". Routed to human review; never auto-forwarded to a model."
                ),
                evidence=evidence,
                runners_up=[(top_id, round(top_p, 6)), *runners_up[:2]],
                started=started,
                confidence=top_p,
                margin=margin,
                coverage=coverage,
            )

        spec = _spec_by_id(spec_list, top_id)
        return Classification(
            doctype_id=top_id,
            label=spec.label if spec else "",
            country=spec.country if spec else "",
            confidence=round(top_p, 6),
            margin=round(margin, 6),
            coverage=round(coverage, 6),
            abstained=False,
            evidence=evidence,
            runners_up=runners_up,
            page_types=[top_id],
            ms=_elapsed_ms(started),
        )


def classify_pages(
    view: LayoutView,
    specs: Iterable[DocTypeSpec] | None = None,
    *,
    settings: Settings | None = None,
    profiles: ProfileSet | None = None,
) -> list[Segment]:
    """Classify each page of a merged PDF and aggregate the pages into segments.

    Args:
        view: The layout payload for the whole bundle.
        specs: Doctype registry; defaults to :func:`load_registry`.
        settings: Settings override.
        profiles: Pre-built term profiles, built once and shared across pages.

    Returns:
        Run-length aggregated :class:`Segment` objects in page order. A single-document
        payload yields exactly one segment, so callers do not need a special case.
    """
    resolved = settings if settings is not None else get_settings()
    spec_list = list(specs) if specs is not None else load_registry()
    resolved_profiles = profiles or (build_profiles(spec_list) if spec_list else None)

    page_numbers = _page_numbers(view)
    if not page_numbers:
        return []

    per_page: list[tuple[int, Classification]] = []
    for page in page_numbers:
        page_view = _page_view(view, page)
        result = classify(
            page_view, spec_list, settings=resolved, profiles=resolved_profiles
        )
        per_page.append((page, result))

    segments: list[Segment] = []
    run_start = 0
    for i in range(1, len(per_page) + 1):
        ends_run = i == len(per_page) or (
            per_page[i][1].doctype_id != per_page[run_start][1].doctype_id
        )
        if not ends_run:
            continue
        members = per_page[run_start:i]
        head = members[0][1].model_copy(deep=True)
        head.page_types = [c.doctype_id for _, c in members]
        confidence = sum(c.confidence for _, c in members) / len(members)
        head.confidence = round(confidence, 6)
        segments.append(
            Segment(
                doctype_id=head.doctype_id,
                start_page=members[0][0],
                end_page=members[-1][0],
                confidence=round(confidence, 6),
                classification=head,
            )
        )
        run_start = i
    return segments


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _short_circuit(
    anchor: anchors_tier.AnchorOutcome, specs: Sequence[DocTypeSpec]
) -> str | None:
    """Decide whether L1 alone is conclusive.

    Conclusive means: exactly one doctype holds a checksum-verified identifier, that doctype
    also matched at least one of its anchors, and its anchor score leads the field. All three
    matter — a valid CURP appears on an INE card, and a valid SSN appears on a W-2, so a check
    digit on its own identifies a *number*, not a *document*.

    Args:
        anchor: L1's outcome.
        specs: The registry.

    Returns:
        The doctype id to accept immediately, or ``None`` to continue down the cascade.
    """
    verified = anchor.verified_doctypes()
    if len(verified) != 1:
        return None
    candidate = verified[0]
    if not anchor.hits.get(candidate):
        return None
    own = anchor.scores.get(candidate, 0.0)
    others = [s for c, s in anchor.scores.items() if c != candidate]
    if others and own < max(others) + _MIN_SHORT_CIRCUIT_LEAD:
        return None
    return candidate


def _decisive_short_circuit(
    anchor: anchors_tier.AnchorOutcome,
    spec_list: Sequence[DocTypeSpec],
) -> str | None:
    """Accept on a *unique* decisive anchor, with no checksum required.

    A decisive anchor is an issuing-authority header or form number that appears on one
    document type and nowhere else — "UNIQUE IDENTIFICATION AUTHORITY OF INDIA",
    "Request for Taxpayer Identification Number", "INSTITUTO NACIONAL ELECTORAL". Seeing
    one is near-proof of the doctype.

    Without this path the cascade abstained on a genuine Aadhaar whose 12-digit number had
    an OCR-damaged digit: the UIDAI header matched, but the fused score landed at 0.515,
    under the 0.65 accept threshold. That is the same failure mode as the legacy DAS
    classifier scoring a real passport at 0.5335 — and it would be far worse in practice,
    because OCR misreads digits constantly and every damaged ID would go to human review,
    destroying the throughput this service is built for.

    The rule that keeps it honest: a failed checksum degrades the *extracted field's*
    verification status; it must not veto the *document type*. Those are different
    questions and conflating them costs precision on both.

    Returns:
        The doctype id to accept, or ``None`` when zero or several doctypes matched a
        decisive anchor (a decisive conflict is genuinely ambiguous — fall through and let
        the lexical tier arbitrate).
    """
    with_decisive = anchor.decisive_doctypes()
    if len(with_decisive) != 1:
        return None
    candidate = with_decisive[0]
    if _spec_by_id(spec_list, candidate) is None:
        return None
    return candidate


def _accept_short_circuit(
    *,
    doctype_id: str,
    spec_list: Sequence[DocTypeSpec],
    features: structural_tier.StructuralFeatures,
    anchor: anchors_tier.AnchorOutcome,
    started: float,
    confidence: float | None = None,
    note: str | None = None,
) -> Classification:
    """Build the classification for the checksum short-circuit path.

    ``coverage``, ``margin`` and ``runners_up`` here are all *anchor-tier* quantities, because
    this path deliberately never built the term profiles: coverage is the fraction of the
    doctype's declared anchors that were observed, and the runners-up are ranked by anchor
    score rather than by fused probability. The evidence string says so, so a reviewer is
    never misled about which number they are reading.
    """
    spec = _spec_by_id(spec_list, doctype_id)
    score = confidence if confidence is not None else anchor.scores.get(doctype_id, 0.0)
    others = [s for c, s in anchor.scores.items() if c != doctype_id]
    margin = score - (max(others) if others else 0.0)
    evidence = [
        Evidence(
            tier="structural",
            detail=structural_tier.describe(features),
            weight=0.0,
        ),
        *list(anchor.evidence.get(doctype_id, ()))[:_MAX_EVIDENCE_PER_TIER],
        Evidence(
            tier="checksum" if note is None else "anchor",
            detail=note or (
                "checksum-verified decisive identifier — accepted at L1; the lexical tier was "
                "not run, so coverage is anchor coverage"
            ),
            weight=score,
        ),
    ]
    return Classification(
        doctype_id=doctype_id,
        label=spec.label if spec else "",
        country=spec.country if spec else "",
        confidence=round(score, 6),
        margin=round(margin, 6),
        coverage=round(anchor.coverage.get(doctype_id, 0.0), 6),
        abstained=False,
        evidence=evidence,
        runners_up=[
            (c, round(s, 6))
            for c, s in sorted(anchor.scores.items(), key=lambda kv: -kv[1])
            if c != doctype_id
        ][:3],
        page_types=[doctype_id],
        ms=_elapsed_ms(started),
    )


def _bert_scores(view: LayoutView, settings: Settings) -> dict[str, float]:
    """Run L3 if and only if it is enabled and usable.

    The import lives inside this function on purpose: :mod:`dce.classify.bert_knn` pulls in
    ``transformers``, and a container running with ``bert_enabled=false`` must never pay for
    it — nor even have it in ``sys.modules``.
    """
    if not settings.bert_enabled:
        return {}
    try:
        from . import bert_knn

        if not bert_knn.tier_available(settings):
            return {}
        return bert_knn.bert_scores(view, settings=settings)
    except EgressViolation:
        # Never swallowed. If the optional tier tried to leave the process, that is the one
        # failure this service must not paper over.
        raise
    except Exception:  # noqa: BLE001 - a missing checkpoint or an unreadable exemplar file
        # degrades the cascade to L0-L2. It must not fail the request: L1+L2 are the tiers
        # this service is designed around, and L3 is explicitly optional.
        return {}


def _accept_failures(
    *, probability: float, margin: float, coverage: float, settings: Settings
) -> list[str]:
    """Return the human-readable reasons acceptance was refused (empty means accept)."""
    failures: list[str] = []
    if probability < settings.classify_accept_probability:
        failures.append(
            f"probability below floor {settings.classify_accept_probability:.2f}"
        )
    if margin < settings.classify_min_margin:
        failures.append(f"margin below floor {settings.classify_min_margin:.2f}")
    if coverage < settings.classify_min_coverage:
        failures.append(
            f"coverage below floor {settings.classify_min_coverage:.2f} — too little of that "
            "document type's vocabulary was present"
        )
    return failures


def _evidence(
    *,
    features: structural_tier.StructuralFeatures,
    priors: Mapping[str, float],
    anchor: anchors_tier.AnchorOutcome,
    lexical: LexicalOutcome,
    bert: Mapping[str, float],
    doctype_id: str,
) -> list[Evidence]:
    """Assemble the audit trail for the winning candidate."""
    evidence = [
        Evidence(
            tier="structural",
            detail=structural_tier.describe(features),
            weight=round(priors.get(doctype_id, 0.0), 4),
        )
    ]
    evidence.extend(list(anchor.evidence.get(doctype_id, ()))[:_MAX_EVIDENCE_PER_TIER])

    matched = lexical.matched.get(doctype_id, ())
    top_terms = ", ".join(term for term, _ in matched[:6]) or "none"
    evidence.append(
        Evidence(
            tier="lexical",
            detail=(
                f"bm25={lexical.raw.get(doctype_id, 0.0):.3f} "
                f"p={lexical.probability.get(doctype_id, 0.0):.3f} "
                f"coverage={lexical.coverage.get(doctype_id, 0.0):.3f}; "
                f"profile terms seen: {top_terms}"
            ),
            weight=round(lexical.probability.get(doctype_id, 0.0), 4),
        )
    )
    if bert:
        evidence.append(
            Evidence(
                tier="bert",
                detail=f"local BERT kNN p={bert.get(doctype_id, 0.0):.3f}",
                weight=round(bert.get(doctype_id, 0.0), 4),
            )
        )
    return evidence


def _abstain(
    *,
    reason: str,
    evidence: Sequence[Evidence],
    runners_up: Sequence[tuple[str, float]],
    started: float,
    confidence: float = 0.0,
    margin: float = 0.0,
    coverage: float = 0.0,
) -> Classification:
    """Build the ``UNKNOWN`` result. The candidate we declined is kept in ``runners_up``."""
    return Classification(
        doctype_id=UNKNOWN,
        label="",
        country="",
        confidence=round(confidence, 6),
        margin=round(margin, 6),
        coverage=round(coverage, 6),
        abstained=True,
        reason=reason,
        evidence=list(evidence),
        runners_up=list(runners_up),
        page_types=[UNKNOWN],
        ms=_elapsed_ms(started),
    )


def _spec_by_id(specs: Sequence[DocTypeSpec], doctype_id: str) -> DocTypeSpec | None:
    """Look up a spec by id."""
    for spec in specs:
        if spec.doctype_id == doctype_id:
            return spec
    return None


def _page_numbers(view: LayoutView) -> list[int]:
    """Ordered page numbers present in the payload."""
    pages = {p.page for p in view.pages}
    pages.update(b.page for b in view.blocks)
    pages.update(t.page for t in view.tables)
    return sorted(pages)


def _page_view(view: LayoutView, page: int) -> LayoutView:
    """Slice a single page out of a multi-page payload."""
    return LayoutView(
        doc_id=f"{view.doc_id}#p{page}" if view.doc_id else "",
        pages=[p for p in view.pages if p.page == page],
        blocks=[b for b in view.blocks if b.page == page],
        tables=[t for t in view.tables if t.page == page],
        marks=[m for m in view.marks if m.page == page],
        key_values=[kv for kv in view.key_values if kv.page == page],
        languages=list(view.languages),
    )


def _elapsed_ms(started: float) -> int:
    """Milliseconds since ``started``."""
    return round((time.perf_counter() - started) * 1000)
