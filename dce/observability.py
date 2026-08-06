"""Prometheus metrics and the process-local readiness registry.

Both concerns are import-safe (no I/O at import) and never fatal: if ``prometheus_client`` is
missing every ``observe_*`` becomes a silent no-op and ``/metrics`` serves an explanatory
comment. Instrumentation must never be the reason a service fails to boot.

**The number to watch is the abstention rate.** ``dce_classifications_total{outcome}`` splits
every classification into ``accepted`` / ``abstained``; the ratio of the second to the total is
the health of the whole cascade:

.. code-block:: promql

   sum(rate(dce_classifications_total{outcome="abstained"}[30m]))
     / sum(rate(dce_classifications_total[30m]))

Rising means the corpus drifted away from the registry (a new form revision, a new
correspondent, a new language) and the fix is a doctype or a term profile — not a lower
threshold. Falling to zero is equally suspicious: a classifier that never abstains has stopped
being able to say "I don't know", which is the one thing this service must always be able to
say. Pair it with ``dce_classification_confidence`` (are accepts clustering just above the
threshold?) and ``dce_classifications_by_doctype_total`` (did one class swallow the traffic?).

**The second number to watch is spend.** Extraction tiers 2-4 leave the process and bill per
page, so their calls are counted separately from their invocations
(``dce_extraction_tier_cost_calls_total{tier,provider}`` vs
``dce_extraction_tier_invocations_total{tier,outcome}``) and separately again from what they
actually produced (``dce_extraction_tier_fields_filled_total{tier}``). Cost per field, per
tier, is the ratio that decides whether a paid tier stays switched on:

.. code-block:: promql

   sum by (tier) (rate(dce_extraction_tier_cost_calls_total[1d]))
     / sum by (tier) (rate(dce_extraction_tier_fields_filled_total[1d]))

Cardinality: every label here is bounded by the registry — doctype ids, field names, tier names
and validator names are all authored, never taken from document content.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from dce.models import Classification, ExtractionResult

logger = logging.getLogger(__name__)

try:  # prometheus_client is a declared dependency, but must not be load-bearing for boot.
    import prometheus_client as _prom
except ImportError:  # pragma: no cover - depends on the installed dependency set
    _prom = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
#: Components that must be healthy for ``/readyz`` to report ready. The registry is the only
#: hard requirement: with no doctypes the service can do nothing but abstain, which is worse
#: than being taken out of the load balancer. The egress invariant is *always* reported and is
#: required — a process that came up with pre-classification egress allowed must not serve.
REQUIRED_COMPONENTS: tuple[str, ...] = ("registry", "egress")

#: The component vocabulary the boot path is expected to report.
KNOWN_COMPONENTS: tuple[str, ...] = ("registry", "egress", "classifier", "extractor", "bert")


class ComponentState(BaseModel):
    """Health of a single component as last reported."""

    ok: bool
    detail: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class Readiness:
    """A mutable registry of component health, safe to write from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, ComponentState] = {}

    def set(self, component: str, ok: bool, detail: str = "", **extra: Any) -> None:
        """Record (or replace) the state of ``component``.

        Args:
            component: Component name — see :data:`KNOWN_COMPONENTS`.
            ok: Whether the component is healthy.
            detail: Human-readable reason, most useful when ``ok`` is ``False``.
            **extra: Structured context stored on :attr:`ComponentState.extra`.
        """
        with self._lock:
            self._state[component] = ComponentState(ok=ok, detail=detail, extra=dict(extra))

    def get(self, component: str) -> ComponentState | None:
        """Return the state of ``component``, or ``None`` if it was never reported."""
        with self._lock:
            state = self._state.get(component)
        return state.model_copy(deep=True) if state is not None else None

    def snapshot(self) -> dict[str, ComponentState]:
        """Return a deep copy of every reported component state."""
        with self._lock:
            return {name: state.model_copy(deep=True) for name, state in self._state.items()}

    def ready(self) -> bool:
        """Whether every component in :data:`REQUIRED_COMPONENTS` is healthy.

        A required component that was never reported counts as not ready: unknown is never
        assumed healthy, so a boot path that died before reporting cannot fail open.
        """
        snap = self.snapshot()
        return all(
            (state := snap.get(name)) is not None and state.ok for name in REQUIRED_COMPONENTS
        )

    def degraded(self) -> list[str]:
        """Sorted names of components that are unhealthy or never reported."""
        snap = self.snapshot()
        names = {name for name, state in snap.items() if not state.ok}
        names.update(name for name in REQUIRED_COMPONENTS if name not in snap)
        return sorted(names)

    def reset(self) -> None:
        """Drop all state. Used by tests that build several apps in one process."""
        with self._lock:
            self._state.clear()


#: Process-wide readiness registry. Written by the boot path, read by ``/readyz``.
READINESS = Readiness()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
_REGISTRY: Any = _prom.REGISTRY if _prom is not None else None

#: Classification is an in-process, CPU-bound path: milliseconds, not seconds. The buckets are
#: tight at the bottom so a regression from 3ms to 30ms is visible instead of hiding in a
#: single bucket. The tail still reaches 5s for the optional local BERT tier on cold CPU.
_TIER_BUCKETS = (
    0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, float("inf"),
)
#: Probabilities and fill rates: a fixed 0..1 spread straddling the default accept threshold
#: (0.65), so "how many accepts land just over the line" is answerable from the histogram.
_UNIT_BUCKETS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.65, 0.7, 0.8, 0.9, 0.95, 1.0)
_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, float("inf"))
#: Extraction tiers 2-4 leave the process: a remote analyse call is seconds, sometimes tens of
#: seconds, never microseconds. Sharing the millisecond buckets with the local tiers would put
#: every paid call in the overflow bucket and make "is Azure slow today" unanswerable.
_PAID_TIER_BUCKETS = (
    0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, float("inf"),
)
#: Time a document waits for a human, in seconds: minutes to a week. This is an SLA curve, not
#: a latency curve — the interesting question is what share of the queue is older than a day.
_REVIEW_BUCKETS = (
    60.0, 300.0, 900.0, 3600.0, 4 * 3600.0, 8 * 3600.0, 24 * 3600.0,
    2 * 24 * 3600.0, 7 * 24 * 3600.0, float("inf"),
)

_DISABLED_CONTENT_TYPE = "text/plain; charset=utf-8"
_DISABLED_PAYLOAD = b"# metrics disabled: prometheus_client is not installed\n"


@dataclass(frozen=True)
class _Metrics:
    """The collector set, built once at import. ``Any`` because the dep may be absent."""

    tier_seconds: Any
    classify_seconds: Any
    confidence: Any
    margin: Any
    classifications: Any
    by_doctype: Any
    extract_seconds: Any
    fill_rate: Any
    fields: Any
    validator_failures: Any
    needs_review: Any
    review_depth: Any
    egress_blocked: Any
    http_seconds: Any
    tier_runs: Any
    tier_fields: Any
    extract_tier_seconds: Any
    tier_cost_calls: Any
    review_enqueued: Any
    review_decisions: Any
    review_decision_seconds: Any


def _find_collector(name: str) -> Any | None:
    """Return an already-registered collector for ``name``, if any."""
    mapping = getattr(_REGISTRY, "_names_to_collectors", {})
    for key in (name, name.removesuffix("_total")):
        collector = mapping.get(key)
        if collector is not None:
            return collector
    return None


def _get_or_create(factory: Any, name: str, documentation: str, **kwargs: Any) -> Any:
    """Register a collector, reusing an identically-named one already in the registry.

    The registry is a module-global that outlives this module's globals, so a re-import (pytest
    collecting several test modules) would otherwise trip the duplicate-timeseries guard.
    """
    try:
        return factory(name, documentation, registry=_REGISTRY, **kwargs)
    except ValueError:
        existing = _find_collector(name)
        if existing is None:
            raise
        return existing


def _build_metrics() -> _Metrics | None:
    """Construct every collector, or return ``None`` if metrics cannot be set up."""
    if _prom is None:
        return None
    try:
        return _Metrics(
            tier_seconds=_get_or_create(
                _prom.Histogram,
                "dce_classification_tier_seconds",
                "Time spent in each classification tier (structural|anchor|lexical|bert|fuse).",
                labelnames=("tier",),
                buckets=_TIER_BUCKETS,
            ),
            classify_seconds=_get_or_create(
                _prom.Histogram,
                "dce_classification_seconds",
                "End-to-end classification latency, by outcome.",
                labelnames=("outcome",),
                buckets=_TIER_BUCKETS,
            ),
            confidence=_get_or_create(
                _prom.Histogram,
                "dce_classification_confidence",
                "Calibrated probability of the winning class, by outcome.",
                labelnames=("outcome",),
                buckets=_UNIT_BUCKETS,
            ),
            margin=_get_or_create(
                _prom.Histogram,
                "dce_classification_margin",
                "Probability margin of the winner over the runner-up, by outcome.",
                labelnames=("outcome",),
                buckets=_UNIT_BUCKETS,
            ),
            classifications=_get_or_create(
                _prom.Counter,
                "dce_classifications_total",
                "Classifications by outcome (accepted|abstained) — the abstention-rate signal.",
                labelnames=("outcome",),
            ),
            by_doctype=_get_or_create(
                _prom.Counter,
                "dce_classifications_by_doctype_total",
                "Accepted classifications by doctype (plus 'unknown' for abstentions).",
                labelnames=("doctype",),
            ),
            extract_seconds=_get_or_create(
                _prom.Histogram,
                "dce_extraction_seconds",
                "End-to-end extraction latency, by doctype.",
                labelnames=("doctype",),
                buckets=_TIER_BUCKETS,
            ),
            fill_rate=_get_or_create(
                _prom.Histogram,
                "dce_extraction_fill_rate",
                "Share of a doctype's fields that came back with a value.",
                labelnames=("doctype",),
                buckets=_UNIT_BUCKETS,
            ),
            fields=_get_or_create(
                _prom.Counter,
                "dce_extraction_fields_total",
                "Extracted fields by doctype and outcome (filled|empty|missing_required).",
                labelnames=("doctype", "outcome"),
            ),
            validator_failures=_get_or_create(
                _prom.Counter,
                "dce_extraction_validator_failures_total",
                "Field values rejected by their validator (checksum/format), by doctype+field.",
                labelnames=("doctype", "field", "validator"),
            ),
            needs_review=_get_or_create(
                _prom.Counter,
                "dce_needs_review_total",
                "Documents routed to the human queue, by stage (classification|extraction).",
                labelnames=("stage",),
            ),
            review_depth=_get_or_create(
                _prom.Gauge,
                "dce_needs_review_queue_depth",
                "Documents currently waiting in the human review queue.",
            ),
            egress_blocked=_get_or_create(
                _prom.Counter,
                "dce_preclassification_egress_blocked_total",
                "Network calls refused because the document was not classified yet.",
                labelnames=("component",),
            ),
            http_seconds=_get_or_create(
                _prom.Histogram,
                "dce_http_request_seconds",
                "HTTP request latency by route and status class.",
                labelnames=("method", "route", "status"),
                buckets=_HTTP_BUCKETS,
            ),
            tier_runs=_get_or_create(
                _prom.Counter,
                "dce_extraction_tier_invocations_total",
                "Extraction tier invocations by tier and outcome "
                "(ran|error|unavailable|misconfigured).",
                labelnames=("tier", "outcome"),
            ),
            tier_fields=_get_or_create(
                _prom.Counter,
                "dce_extraction_tier_fields_filled_total",
                "Fields filled by each extraction tier — what the tier actually bought you.",
                labelnames=("tier",),
            ),
            extract_tier_seconds=_get_or_create(
                _prom.Histogram,
                "dce_extraction_tier_seconds",
                "Latency of one extraction tier, by tier.",
                labelnames=("tier",),
                buckets=_PAID_TIER_BUCKETS,
            ),
            tier_cost_calls=_get_or_create(
                _prom.Counter,
                "dce_extraction_tier_cost_calls_total",
                "Calls to a tier that BILLS: T2/T3 (Azure) and T4 (LLM). Kept separate from "
                "the invocation counter so spend is legible without a PromQL filter.",
                labelnames=("tier", "provider"),
            ),
            review_enqueued=_get_or_create(
                _prom.Counter,
                "dce_review_enqueued_total",
                "Documents placed on the human review queue, by reason.",
                labelnames=("reason",),
            ),
            review_decisions=_get_or_create(
                _prom.Counter,
                "dce_review_decisions_total",
                "Human decisions taken on the review queue (approve|reject|correct).",
                labelnames=("decision",),
            ),
            review_decision_seconds=_get_or_create(
                _prom.Histogram,
                "dce_review_time_to_decision_seconds",
                "Time from enqueue to human decision, by decision.",
                labelnames=("decision",),
                buckets=_REVIEW_BUCKETS,
            ),
        )
    except Exception:
        logger.warning("could not build prometheus collectors; metrics disabled", exc_info=True)
        return None


_METRICS: _Metrics | None = _build_metrics()


def metrics_enabled() -> bool:
    """Whether metrics are being collected."""
    return _METRICS is not None


def metrics_response() -> tuple[bytes, str]:
    """Render the current metrics for the ``/metrics`` route.

    Returns:
        ``(payload, content_type)``. When metrics are disabled the payload is an explanatory
        comment rather than an error, so a scraper still gets a valid, empty exposition.
    """
    if _prom is None or _METRICS is None:
        return _DISABLED_PAYLOAD, _DISABLED_CONTENT_TYPE
    return _prom.generate_latest(_REGISTRY), _prom.CONTENT_TYPE_LATEST


def observe_tier(tier: str, seconds: float) -> None:
    """Record time spent in one classification tier.

    Args:
        tier: ``structural`` | ``anchor`` | ``lexical`` | ``bert`` | ``fuse``.
        seconds: Wall-clock duration of the tier.
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.tier_seconds.labels(tier=str(tier)).observe(seconds)


@contextmanager
def tier_timer(tier: str) -> Iterator[None]:
    """Time a classification tier and report it via :func:`observe_tier`.

    An exception still records the elapsed time — a tier that blew up after 400ms is exactly
    the kind of thing you want on the latency chart.

    Args:
        tier: Tier name passed through to :func:`observe_tier`.

    Yields:
        ``None`` — the block runs inside the timing window.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        observe_tier(tier, time.perf_counter() - start)


def observe_classification(result: Classification, seconds: float | None = None) -> None:
    """Record a completed classification.

    Feeds the abstention rate, the confidence/margin histograms and the per-doctype counters
    from the single object the classifier already produced, so no caller has to remember which
    label goes where.

    Args:
        result: The classification, abstained or not.
        seconds: End-to-end duration; falls back to ``result.ms``.
    """
    metrics = _METRICS
    if metrics is None:
        return
    outcome = "abstained" if result.abstained else "accepted"
    elapsed = seconds if seconds is not None else result.ms / 1000.0
    metrics.classifications.labels(outcome=outcome).inc()
    metrics.by_doctype.labels(doctype=str(result.doctype_id)).inc()
    metrics.confidence.labels(outcome=outcome).observe(max(0.0, min(1.0, result.confidence)))
    metrics.margin.labels(outcome=outcome).observe(max(0.0, min(1.0, result.margin)))
    metrics.classify_seconds.labels(outcome=outcome).observe(max(0.0, elapsed))
    if result.abstained:
        metrics.needs_review.labels(stage="classification").inc()


def observe_extraction(result: ExtractionResult, seconds: float | None = None) -> None:
    """Record a completed extraction: latency, fill rate, per-field outcomes, validator misses.

    Args:
        result: The extraction result.
        seconds: End-to-end duration; falls back to ``result.ms``.
    """
    metrics = _METRICS
    if metrics is None:
        return
    doctype = str(result.doctype_id)
    elapsed = seconds if seconds is not None else result.ms / 1000.0
    metrics.extract_seconds.labels(doctype=doctype).observe(max(0.0, elapsed))
    metrics.fill_rate.labels(doctype=doctype).observe(result.fill_rate)
    for field in result.fields:
        outcome = "filled" if field.value else "empty"
        metrics.fields.labels(doctype=doctype, outcome=outcome).inc()
        if field.validator_error:
            metrics.validator_failures.labels(
                doctype=doctype, field=field.name, validator=field.verification or "unknown"
            ).inc()
    for name in result.missing_required:
        metrics.fields.labels(doctype=doctype, outcome="missing_required").inc()
        logger.debug("required field %s missing on %s", name, doctype)
    if result.needs_review:
        metrics.needs_review.labels(stage="extraction").inc()


def observe_validator_failure(doctype: str, field: str, validator: str) -> None:
    """Count one value rejected by a named validator.

    Args:
        doctype: Doctype id the field belongs to.
        field: Field name.
        validator: Validator that rejected the value (e.g. ``verhoeff_aadhaar``).
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.validator_failures.labels(doctype=doctype, field=field, validator=validator).inc()


def observe_extraction_tier(
    tier: str,
    *,
    seconds: float,
    fields_filled: int = 0,
    outcome: str = "ran",
    cost_bearing: bool = False,
    provider: str = "local",
) -> None:
    """Record one extraction tier's contribution to a single document.

    The two questions an operator has about the tiered extractor are *what did this document
    cost* and *was it worth it*, and neither is answerable from a single counter. So the spend
    signal (:data:`dce_extraction_tier_cost_calls_total`) is separate from the invocation
    signal, and the yield signal (:data:`dce_extraction_tier_fields_filled_total`) is separate
    from both. Divide the third by the first and you have cost per field, per tier, which is
    the number that decides whether a tier stays switched on.

    A tier that raised still counts as a cost-bearing call when it is one: the remote call was
    made and will appear on the bill whether or not we could parse the answer.

    Args:
        tier: Tier id — ``t1_local`` | ``t2_azure_prebuilt`` | ``t3_azure_query`` | ``t4_llm``.
        seconds: Wall-clock duration of the tier.
        fields_filled: Fields this tier filled that were still missing when it ran.
        outcome: ``ran`` | ``error`` | ``unavailable`` | ``misconfigured`` | ``skipped``.
        cost_bearing: Whether a billable call was made.
        provider: Who bills for it (``azure`` | ``llm`` | ``local``).
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.tier_runs.labels(tier=str(tier), outcome=str(outcome)).inc()
    metrics.extract_tier_seconds.labels(tier=str(tier)).observe(max(0.0, seconds))
    if fields_filled:
        metrics.tier_fields.labels(tier=str(tier)).inc(fields_filled)
    if cost_bearing:
        metrics.tier_cost_calls.labels(tier=str(tier), provider=str(provider)).inc()


def observe_review_enqueued(reason: str) -> None:
    """Count one document placed on the human review queue.

    Args:
        reason: Why it was queued — ``abstained`` | ``missing_required`` | ``low_confidence``.
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.review_enqueued.labels(reason=str(reason)).inc()


def observe_review_decision(decision: str, seconds: float | None = None) -> None:
    """Count one human decision, and how long the document waited for it.

    Time-to-decision is the queue's real SLA: depth alone cannot distinguish a queue of 40
    documents that are each ten minutes old from a queue of 40 that have been there a week.

    Args:
        decision: ``approve`` | ``reject`` | ``correct``.
        seconds: Seconds between enqueue and this decision, when it can be determined.
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.review_decisions.labels(decision=str(decision)).inc()
    if seconds is not None and seconds >= 0:
        metrics.review_decision_seconds.labels(decision=str(decision)).observe(seconds)


def set_needs_review_depth(depth: int) -> None:
    """Set the human-review queue depth gauge.

    Args:
        depth: Number of documents currently waiting for a human.
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.review_depth.set(max(0, depth))


#: The queue is now run in-process (``dce.review``) rather than only reported about, so the
#: gauge has a name that says what it measures. The old name stays: it is what the existing
#: alerting calls.
set_review_queue_depth = set_needs_review_depth


def observe_egress_block(component: str) -> None:
    """Count a network call refused because the document was not classified yet.

    Any nonzero value is a finding, not noise: something in the pre-classification path tried
    to talk to the outside world and the invariant stopped it.

    Args:
        component: The caller that was blocked (e.g. ``des_client``).
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.egress_blocked.labels(component=str(component)).inc()


def observe_http(method: str, route: str, status: int, seconds: float) -> None:
    """Record one HTTP request.

    Args:
        method: HTTP method.
        route: Matched route template (never the raw path — that would be unbounded).
        status: Response status code; bucketed to ``2xx``/``4xx``/``5xx``.
        seconds: Wall-clock duration.
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.http_seconds.labels(
        method=str(method), route=str(route), status=f"{status // 100}xx"
    ).observe(seconds)
