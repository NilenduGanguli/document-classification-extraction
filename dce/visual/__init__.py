"""The second classification avenue — a **closed registry that is currently empty**.

This package exists so that ``/api/v1/classify/compare`` has something to ask, and so that
``/readyz`` can answer "is there a second avenue, and what can it cover" with a fact rather
than with silence. It ships **no classifier**, because none of the methods measured against
the real corpus came close to the precision bar the owner set.

**The bar.** ``PRECISION >= 95% on the documents the avenue ANSWERS.`` Coverage is the honest
variable and abstention is free — this service routes abstentions to a human by design, so an
avenue that answers 20% of documents and is right on all of them is a success. An avenue that
answers everything at 60% precision is a compliance incident. Nothing here trades the first
for the second.

**What was measured, and what it returned.** Four visual methods, each validated on
*genuinely distinct files* — never on synthetic degradation, which is the trap that reported
AUC 0.9992 for a method whose real-pair AUC was 0.568:

===========================  =========  ====================  ==============================
method                       real AUC   best end-to-end        why it died
===========================  =========  ====================  ==============================
``sift_homography``            0.568    precision 0.022        keypoints land on GLYPHS
``structure_skeleton``         0.627    precision 0.067        template != doctype
``layout_signature``           0.846    precision 0.080        confusables ARE one layout
``emblem_match``               0.554    precision 0.000        a seal identifies the ISSUER
===========================  =========  ====================  ==============================

For comparison, the lexical cascade on the same corpus: 117 correct, 2 wrong, **98.3%
precision when it answered**, 20.7% abstention. The two are not in the same regime, and the
gap is roughly 14x at the best cell any visual method reached — not a tuning margin.

**Why the registry is closed rather than pluggable-by-import.** A method that ships here is a
method that has cleared 95% precision on genuinely distinct files. Making the set open would
make "somebody dropped a module in" a path to answering KYC classifications, which is exactly
the failure the abstention discipline exists to prevent. :data:`AVENUES` is the whole list,
and it is empty. :data:`RETIRED` is the list of methods that were *tried and killed*, kept in
code so that configuring one names the measurement that killed it instead of returning an
uninformative "unknown method" — the next person to have this idea should meet the numbers,
not a validation error.

See ``docs/specs/2026-08-08-visual-classification-design.md`` §5A and §5B for the full
post-mortems, including the two registry defects the work turned up as a by-product.

Nothing in this module imports ``cv2`` or ``numpy``, and nothing here can. Those stay behind
the ``.[visual]`` extra, which no shipped code path requires.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from dce.models import Classification, LayoutView

__all__ = [
    "AVENUES",
    "RETIRED",
    "AvenueStatus",
    "RetiredMethod",
    "SecondAvenue",
    "avenue_status",
    "resolve_avenue",
]

#: How many doctypes the registry carries, for the coverage denominator on ``/readyz``. Read
#: from the live registry when one is available; this is only the documented fallback so the
#: status block never invents a percentage out of nothing.
REGISTRY_SIZE_HINT = 182


@dataclass(frozen=True)
class RetiredMethod:
    """A visual method that was built, measured against the real corpus, and killed.

    Kept in the codebase deliberately. These are not comments — they are what a deployment
    gets told when it configures the method by name, and what the next engineer to propose
    "let's just match the layout" is handed before they spend a week on it.
    """

    #: The id an operator would have set. Configuring it is an error, not a silent no-op.
    method_id: str
    #: One line a human reads.
    name: str
    #: ISO date the measurement concluded.
    retired_on: str
    #: Discrimination on GENUINELY DISTINCT same-doctype pairs. Chance is 0.500.
    real_pair_auc: float
    #: Best precision-when-answered reached at ANY threshold, end-to-end leave-one-out.
    best_precision: float
    #: The coverage that best precision bought, as a fraction of rasterisable documents.
    coverage_at_best: float
    #: Why it fails — the mechanism, not the symptom. This is the part worth keeping.
    mechanism: str


#: Every visual method measured against the 158-document corpus, and why each one is not
#: shipping. Ordered by the date each was concluded.
RETIRED: Mapping[str, RetiredMethod] = {
    "sift_homography": RetiredMethod(
        method_id="sift_homography",
        name="SIFT/ORB keypoints + Lowe ratio + findHomography(RANSAC), max inliers",
        retired_on="2026-08-08",
        real_pair_auc=0.568,
        best_precision=0.022,
        coverage_at_best=1.0,
        mechanism=(
            "Keypoints land on GLYPH SHAPES, not on layout. Any two pages of text share a "
            "vast vocabulary of letterforms and RANSAC happily fits a homography aligning "
            "rows of type, so unrelated documents outscore related ones: a KYC registry "
            "record paired with an unrelated LLC operating agreement scored 761 inliers, "
            "while an electricity bill paired with its own sibling scored 23 (measured on "
            "the 181-doctype registry, before the India pack was removed). Separately "
            "disqualifying: the score is a function of "
            "MATCH ORDER — permuting the same 1130 correspondences gave inlier counts from "
            "17 to 50 — so the same document does not reliably give the same answer."
        ),
    ),
    "structure_skeleton": RetiredMethod(
        method_id="structure_skeleton",
        name="Glyph-stripped structural skeleton (morphological rules) + segment matching",
        retired_on="2026-08-11",
        real_pair_auc=0.627,
        best_precision=0.067,
        coverage_at_best=0.112,
        mechanism=(
            "Structural similarity measures TEMPLATE identity, and template identity is not "
            "doctype identity. The documents sharing a template most strongly are precisely "
            "the ones that must be told apart (mx_cif x mx_rfc_csf 1.000, us_sec_form4 x "
            "form5 1.000), while 10 of 16 real same-doctype pairs share NO structure at all "
            "— two BRSRs from two companies are not the same form. Stripping the glyphs "
            "deliberately discards the only signal that separates the confusables, so the "
            "method is guaranteed to fail hardest exactly where precision is decided."
        ),
    ),
    "layout_signature": RetiredMethod(
        method_id="layout_signature",
        name="Fixed-length correspondence-free page descriptors (ink grid, profiles, blocks)",
        retired_on="2026-08-11",
        real_pair_auc=0.846,
        best_precision=0.080,
        coverage_at_best=0.187,
        mechanism=(
            "The strongest real-pair signal of the four, and still dead, because the signal "
            "lives entirely in the EASY negatives — a one-page ID card versus a 300-page "
            "prospectus — which the lexical cascade already answers. Against the hard "
            "negatives that decide precision it INVERTS: AUC 0.483, below chance. The "
            "positive band [0.822, 0.946] sits strictly inside the negative band, whose top "
            "is entirely confusables. Precision is anti-correlated with score: at the top of "
            "the ranking (14 documents) precision is 0.000 — every most-confident answer is "
            "wrong. Raising the threshold cannot rescue that."
        ),
    ),
    "emblem_match": RetiredMethod(
        method_id="emblem_match",
        name="Issuer emblem/seal detection + normalised cross-correlation",
        retired_on="2026-08-11",
        real_pair_auc=0.554,
        best_precision=0.000,
        coverage_at_best=0.0,
        mechanism=(
            "An emblem identifies the ISSUER, and one issuer issues many doctypes. Every "
            "corpus pair scoring >= 0.94 is a different doctype from the same issuer (IRS "
            "1099 vs W-2, SEC Form 4 vs Form 5, SAT CIF vs RFC-CSF, five MCA forms), while "
            "every genuine same-doctype pair comes from a DIFFERENT issuer and shares no "
            "mark. Precision is 0.000 at all 54 operating points swept, including with no "
            "threshold at all. The detector itself is sound — identity retrieval is 0.9916 — "
            "so this is the method answering the wrong question, not a bug. A decisive "
            "lexical anchor works because the string is issuer-controlled AND form-unique; a "
            "seal is issuer-controlled and form-AGNOSTIC. It is the letterhead, not the title."
        ),
    ),
}


@dataclass(frozen=True)
class AvenueStatus:
    """What ``/readyz`` says about the second avenue.

    Every field is here so an operator cannot discover by *using* the endpoint that the
    second avenue can only answer for a fraction of the registry. ``doctypes_covered``
    against ``doctypes_total`` is the number that matters and it is reported even when it is
    zero — especially when it is zero.
    """

    available: bool
    method: str
    templates: int
    doctypes_covered: int
    doctypes_total: int
    #: Set of method ids that could be configured. Empty means the avenue cannot be turned on.
    installable: tuple[str, ...]
    #: What is wrong, when something is. Empty when the state is simply "none configured".
    problem: str
    #: One line, always populated, aimed at a human reading a readiness page.
    summary: str
    #: Ids of methods measured and killed, so the page carries the history not just the gap.
    retired: tuple[str, ...]

    @property
    def coverage(self) -> float:
        """Fraction of the registry the avenue could answer for. 0.0 when unavailable."""
        if self.doctypes_total <= 0:
            return 0.0
        return self.doctypes_covered / self.doctypes_total


class SecondAvenue(Protocol):
    """The contract a second avenue must satisfy to be listed in :data:`AVENUES`.

    Deliberately identical in shape to :class:`dce.api.routes.ClassifierPort` so that
    ``/classify/visual`` and ``/classify/compare`` need no special-casing, and so that
    ``tools/corpus_test.py`` scores both avenues with one instrument. Nothing implements it
    today; it is the shape a future candidate has to fit, written down while the reasons are
    fresh.
    """

    #: Stable id, matching the key in :data:`AVENUES`.
    method_id: str

    def classify(
        self, view: LayoutView, *, registry: Any, settings: Any
    ) -> Classification:  # pragma: no cover - no implementation exists
        """Classify, or abstain with a ``reason`` naming the gate that refused."""
        ...

    def status(self, *, registry: Any) -> AvenueStatus:  # pragma: no cover - none exists
        """Template count and registry coverage, for ``/readyz``."""
        ...


#: **The closed registry of second avenues. It is empty, and that is the finding.**
#:
#: An entry here is a claim that the method cleared 95% precision on genuinely distinct
#: files. Four methods were measured; none did; so nothing is listed. Adding an entry
#: requires the measurement, not the code.
AVENUES: Mapping[str, Any] = {}


def _configured_method(settings: Any) -> str:
    """Read the configured method id off settings without requiring the field to exist."""
    return str(getattr(settings, "visual_method", "") or "none").strip().lower()


def resolve_avenue(settings: Any) -> tuple[Any | None, str]:
    """Resolve the configured second avenue.

    Returns:
        ``(avenue, problem)``. ``(None, "")`` is the default and correct state: no second
        avenue is configured, and there is nothing wrong. ``(None, reason)`` means one was
        asked for and cannot be supplied — and when the name is a retired method, ``reason``
        carries the measurement that retired it rather than a bare "unknown".
    """
    name = _configured_method(settings)
    if name in ("", "none", "off", "disabled"):
        return None, ""
    factory = AVENUES.get(name)
    if factory is not None:
        return factory(), ""
    retired = RETIRED.get(name)
    if retired is not None:
        return None, (
            f"visual_method={name!r} was measured against the real corpus on "
            f"{retired.retired_on} and retired: best precision-when-answered "
            f"{retired.best_precision:.3f} at {retired.coverage_at_best:.1%} coverage, "
            f"real-pair AUC {retired.real_pair_auc:.3f} against a chance of 0.500, versus a "
            f"bar of 0.95. {retired.mechanism}"
        )
    return None, (
        f"visual_method={name!r} is not a known method. No second classification avenue has "
        f"cleared the 95% precision bar, so the registry of avenues is empty; "
        f"{len(RETIRED)} methods were measured and retired ({', '.join(sorted(RETIRED))})."
    )


def avenue_status(
    settings: Any, *, doctypes_total: int | None = None, registry: Any = None
) -> AvenueStatus:
    """Build the ``/readyz`` block for the second avenue.

    Args:
        settings: The service settings; only ``visual_method`` is read.
        doctypes_total: Size of the live registry, the denominator for coverage. Falls back
            to :data:`REGISTRY_SIZE_HINT` so the ratio is never silently over a zero.
        registry: The live doctype registry, handed to an avenue so it can report how many
            doctypes it actually holds a template for. Unused while :data:`AVENUES` is empty.
    """
    total = doctypes_total if doctypes_total and doctypes_total > 0 else REGISTRY_SIZE_HINT
    retired = tuple(sorted(RETIRED))
    avenue, problem = resolve_avenue(settings)

    if avenue is None:
        method = _configured_method(settings)
        configured = method not in ("", "none", "off", "disabled")
        return AvenueStatus(
            available=False,
            method="",
            templates=0,
            doctypes_covered=0,
            doctypes_total=total,
            installable=tuple(sorted(AVENUES)),
            problem=problem,
            summary=(
                problem
                if configured
                else (
                    "no second classification avenue is available: four visual methods "
                    f"({', '.join(retired)}) were measured against the real corpus and none "
                    "reached the 95% precision bar at any threshold, so none shipped. "
                    "/classify/compare runs the lexical cascade alone and reports the second "
                    "avenue as unavailable rather than substituting for it."
                )
            ),
            retired=retired,
        )

    return avenue.status(registry=registry)  # pragma: no cover - AVENUES is empty
