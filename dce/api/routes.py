"""HTTP surface: classify, extract, process, and the doctype/schema registry.

Route design follows one rule: **abstention is a first-class outcome, not an error.** A
document the cascade cannot place comes back ``200`` with ``abstained=true`` and a reason, and
``/process`` stops there — it does not extract, and it never falls forward to a remote model.
The only 4xx/5xx are genuine client and server faults.

The engine modules (``dce.registry``, ``dce.classify``, ``dce.extract``) are resolved lazily
through small ports rather than imported at module load. Three reasons, in order of
importance: the API boots and reports honest readiness while an engine module is missing or
broken; tests can substitute a stub through ``app.dependency_overrides`` without monkeypatching
imports; and ``import dce.api.routes`` stays cheap, which matters because the optional local
BERT tier pulls in torch when it is enabled.

The contract a port expects — everything else is adaptation:

* ``dce.registry``: ``get_registry()`` (or ``REGISTRY``/``DOCTYPES``) exposing ``get(doctype_id)``
  and an iterable of :class:`~dce.models.DocTypeSpec`.
* ``dce.classify``: ``classify(view, *, registry, settings) -> Classification``.
* ``dce.extract``: ``extract(view, spec, *, settings, schema_version) -> ExtractionResult``.

Optional keyword arguments are passed only when the callee's signature accepts them, so a
narrower implementation (``classify(view)``) works unchanged.

The extraction tiers
--------------------
``/process`` escalates through five tiers, each of which only ever sees the fields the tiers
before it could not fill:

===== ============================ ======== ==========================================
Tier  Implementation               Cost     Runs when
===== ============================ ======== ==========================================
T1    local layout resolver        free     always
T2    Azure prebuilt specialists   per page ``t2_enabled`` and fields are still missing
T3    Azure ``queryFields``        per field ``t3_enabled`` and fields are still missing
T4    constrained LLM              per token ``t4_enabled`` and fields are still missing
T5    human review queue           a person  the result still needs a human
===== ============================ ======== ==========================================

Three rules hold the tiering together, and all three are enforced *here*, at the call site,
rather than trusted to the tier modules:

1. **No tier runs on an unclassified document.** :func:`run_tier_cascade` raises if it is ever
   reached with an abstention. T2/T3/T4 are egress; egress before a doctype is known is the
   single failure this service exists to prevent (see :mod:`dce.egress`).
2. **Every tier is off by default.** A deployment that wants zero egress keeps it by doing
   nothing at all — see :class:`dce.config.Settings`.
3. **Tier modules are imported lazily, inside the handler, after classification has already
   accepted a doctype.** They pull in an HTTP client; the classification path must never
   import one, and the surest way to guarantee that is for the import not to have happened
   yet when the classifier runs.

The tier contract is deliberately loose, because these modules land independently. A tier is
any callable found under one of :data:`PAID_TIERS`' candidate module names, called with
whichever of ``view``, ``spec``, ``result``, ``missing``, ``settings`` and ``classification``
its signature names (unnamed parameters are filled positionally in that order), and returning
an :class:`~dce.models.ExtractionResult`, a list of :class:`~dce.models.ExtractedField`, or
anything that coerces to either. **What it returns is filtered, not trusted**: values for
fields that were not missing are dropped by :func:`_merge_tier_fields`, so no tier can quietly
overwrite a checksum-verified local result with a fluent guess.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import importlib
import importlib.util
import inspect
import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dce import SERVICE_NAME, __version__, adapters, logs, observability, visual
from dce.config import Settings
from dce.ingest import IngestError, IngestOptions
from dce.ingest import ingest as ingest_bytes
from dce.ingest.ocr import PROVIDERS, provider_info
from dce.ingest.settings import (
    TEXT_LAYER_ALWAYS_OCR,
    TEXT_LAYER_TRUST,
    TRUST_BOUNDARY_ON_PREMISES,
    IngestSettings,
    get_ingest_settings,
)
from dce.models import (
    UNKNOWN,
    Category,
    Classification,
    DocTypeSpec,
    ExtractedField,
    ExtractionResult,
    FieldSpec,
    LayoutView,
)
from dce.observability import READINESS, ComponentState
from dce.visual.compare import compare_classifications

logger = logging.getLogger(__name__)

#: Where each engine may live. The first module that yields a usable implementation wins; a
#: peer is free to expose it from the package root or from a submodule.
_REGISTRY_MODULES = ("dce.registry", "dce.registry.loader", "dce.registry.registry")
_CLASSIFY_MODULES = ("dce.classify", "dce.classify.cascade", "dce.classify.classifier")
_EXTRACT_MODULES = ("dce.extract", "dce.extract.resolver", "dce.extract.pipeline")
_SCHEMA_MODULES = ("dce.registry.schema", "dce.schema", "dce.registry")
_BERT_MODULES = ("dce.classify.bert_knn", "dce.classify.bert")

#: Accessor names, matched to what ``dce.registry`` and ``dce.classify.cascade`` already probe
#: for, so every consumer of the registry agrees on the same vocabulary.
_REGISTRY_NAMES = (
    "all_specs", "load_registry", "get_registry", "specs", "doctypes",
    "SPECS", "DOCTYPES", "DOC_TYPES", "REGISTRY",
)
_REGISTRY_ACCESSORS = ("all_specs", "all", "specs", "load_registry", "doctypes", "values")
_CLASSIFY_NAMES = ("classify", "classify_layout", "classify_view", "Classifier")
_EXTRACT_NAMES = ("extract", "extract_fields", "extract_document", "Extractor")

#: Generic accessors a tier module may expose, tried after the tier's own entrypoint names.
#: ``extract`` is last on purpose: a tier module that re-exports :func:`dce.extract.extract`
#: for convenience must not have that re-export mistaken for the tier itself.
_TIER_NAMES = (
    "fill_missing", "fill_gaps", "run_tier", "run", "fill", "apply", "extract",
)
#: Where the human review queue may live.
_REVIEW_MODULES = ("dce.review", "dce.review.queue", "dce.extract.review")
#: Queue factories a review module may expose, in preference order.
_REVIEW_FACTORIES = (
    "queue_from_settings", "get_queue", "get_review_queue", "review_queue", "QUEUE", "queue",
)

#: The local tier. Always runs, never bills, and is reported alongside the paid tiers so a
#: ``tiers_used`` block reads as the whole story rather than only the expensive part.
TIER_LOCAL = "t1_local"
#: The review queue's entry in ``tiers_used``. Not an extractor — it is where a document goes
#: when the extractors are done and the answer still is not good enough.
TIER_REVIEW = "t5_review"


@dataclass(frozen=True)
class TierSpec:
    """One post-classification extraction tier: how to switch it on, and what it costs.

    Attributes:
        tier: Stable tier id, used as a metric label and in ``tiers_used``.
        flag: :class:`~dce.config.Settings` attribute that enables it. Always defaults False.
        state_attr: ``app.state`` slot the resolved port is cached in — and the slot a test
            installs a stub into.
        modules: Candidate module names, tried in order. Imported **lazily**.
        entrypoints: Function names to look for, most specific first.
        provider: Who bills for a call (``azure`` | ``llm``).
        needs_bytes: Whether the tier sends the original file. T2/T3 do — Azure analyses the
            document, not our reading of it — so they are skipped when the caller supplied
            only a layout payload.
        applies_attr: Optional module-level mapping keyed by doctype id. When present, a
            doctype that is not in it means the tier does not apply and is skipped *without a
            call*, which keeps the spend counter honest.
        summary: One line for ``/readyz``.
    """

    tier: str
    flag: str
    state_attr: str
    modules: tuple[str, ...]
    entrypoints: tuple[str, ...]
    provider: str
    summary: str
    needs_bytes: bool = False
    applies_attr: str = ""


#: The paid tiers, in escalation order. Each only ever sees what the previous one left empty.
PAID_TIERS: tuple[TierSpec, ...] = (
    TierSpec(
        tier="t2_azure_prebuilt",
        flag="t2_enabled",
        state_attr="tier_t2",
        modules=("dce.extract.azure_specialist", "dce.extract.tiers.azure_prebuilt"),
        entrypoints=("extract_with_specialist", *_TIER_NAMES),
        provider="azure",
        needs_bytes=True,
        # Azure ships a specialist for a handful of the 121 registered doctypes. For the rest
        # the correct answer is "stay on T1", and finding that out must not cost a call.
        applies_attr="SPECIALIST_MODELS",
        summary="Azure prebuilt specialists (idDocument/tax.us.*/bankStatement) — billed per page",
    ),
    TierSpec(
        tier="t3_azure_query",
        flag="t3_enabled",
        state_attr="tier_t3",
        modules=("dce.extract.query_fields", "dce.extract.tiers.azure_query"),
        entrypoints=("extract_query_fields", *_TIER_NAMES),
        provider="azure",
        needs_bytes=True,
        summary="Azure queryFields for schema fields no prebuilt model covers — billed per field",
    ),
    TierSpec(
        tier="t4_llm",
        flag="t4_enabled",
        state_attr="tier_t4",
        modules=("dce.extract.llm_field", "dce.extract.tiers.llm"),
        entrypoints=("extract_fields_llm", *_TIER_NAMES),
        provider="llm",
        summary="Schema-constrained LLM, last resort — billed per token, never trusted unvalidated",
    ),
)


# ---------------------------------------------------------------------------
# Engine resolution
# ---------------------------------------------------------------------------
def _import(module_name: str) -> Any | None:
    """Import a module, returning ``None`` when it is absent or fails to import."""
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None
    except Exception:
        logger.warning("engine module %s failed to import", module_name, exc_info=True)
        return None


def _first_attr(module: Any, *names: str) -> Any | None:
    """First non-module attribute of ``module`` named in ``names``.

    Submodules are skipped: on a package, ``dce.registry.doctypes`` resolves as an attribute
    once imported, and mistaking a module for a registry would leave us "loaded" but empty.
    """
    for name in names:
        candidate = getattr(module, name, None)
        if candidate is not None and not inspect.ismodule(candidate):
            return candidate
    return None


def _call_supported(fn: Any, *args: Any, **optional: Any) -> Any:
    """Call ``fn``, passing only the optional keyword arguments its signature accepts."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args)
    takes_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    kwargs = {k: v for k, v in optional.items() if v is not None and (takes_kwargs or k in params)}
    return fn(*args, **kwargs)


def _as_specs(value: Any) -> list[DocTypeSpec]:
    """Coerce whatever a registry exposes into a list of specs."""
    if isinstance(value, DocTypeSpec):
        return [value]
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list | tuple | set | frozenset):
        try:
            value = list(value)
        except TypeError:
            return []
    return [item for item in value if isinstance(item, DocTypeSpec)]


class RegistryPort:
    """Adapter over whatever ``dce.registry`` exposes."""

    def __init__(self, impl: Any) -> None:
        self._impl = impl

    @property
    def impl(self) -> Any:
        """The registry object the engine built, passed back to it unchanged."""
        return self._impl

    def specs(self) -> list[DocTypeSpec]:
        """Every registered doctype spec, sorted by id for a stable API response."""
        for name in _REGISTRY_ACCESSORS:
            candidate = getattr(self._impl, name, None)
            if candidate is None:
                continue
            try:
                value = candidate() if callable(candidate) else candidate
            except Exception:  # noqa: BLE001 - try the next accessor
                continue
            specs = _as_specs(value)
            if specs:
                return sorted(specs, key=lambda s: s.doctype_id)
        return sorted(_as_specs(self._impl), key=lambda s: s.doctype_id)

    def get(self, doctype_id: str) -> DocTypeSpec | None:
        """Look up one spec by id, or ``None`` when the registry does not have it."""
        getter = getattr(self._impl, "get", None)
        if callable(getter):
            try:
                found = getter(doctype_id)
            except Exception:  # noqa: BLE001 - fall through to the linear scan
                found = None
            if isinstance(found, DocTypeSpec):
                return found
        return next((s for s in self.specs() if s.doctype_id == doctype_id), None)

    def __len__(self) -> int:
        return len(self.specs())


class ClassifierPort:
    """Adapter over ``dce.classify``. Always returns a :class:`Classification`."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn

    def classify(
        self, view: LayoutView, *, registry: RegistryPort, settings: Settings
    ) -> Classification:
        """Run the in-process cascade over ``view``.

        Args:
            view: The layout view to classify. Never leaves the process.
            registry: The doctype registry the cascade scores against.
            settings: Thresholds, zone weights and fusion weights.

        Returns:
            The classification, abstained or not.

        Raises:
            HTTPException: ``502`` when the engine returned something that is not a
                :class:`Classification` — a wrong shape here would otherwise be reported to
                the caller as a confident answer.
        """
        result = _call_supported(
            self._fn, view, registry=registry.impl, specs=registry.specs(), settings=settings
        )
        if not isinstance(result, Classification):
            raise HTTPException(status_code=502, detail="classifier returned an unexpected type")
        return result


class ExtractorPort:
    """Adapter over ``dce.extract``. Always returns an :class:`ExtractionResult`."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn

    def extract(
        self,
        view: LayoutView,
        spec: DocTypeSpec,
        *,
        settings: Settings,
        schema_version: str | None = None,
    ) -> ExtractionResult:
        """Resolve ``spec``'s fields against ``view``.

        Args:
            view: The layout view to extract from.
            spec: The accepted doctype's spec, including its field locators.
            settings: Extraction windows, fuzzy-label floor and accept confidence.
            schema_version: Pin a schema version, when the caller asked for one.

        Returns:
            The extraction result, with per-field provenance.

        Raises:
            HTTPException: ``502`` when the engine returned an unexpected type.
        """
        result = _call_supported(
            self._fn, view, spec, settings=settings, schema_version=schema_version
        )
        if not isinstance(result, ExtractionResult):
            raise HTTPException(status_code=502, detail="extractor returned an unexpected type")
        return result


#: Names a tier's parameters may go by, and the order unnamed parameters are filled in.
_TIER_POSITIONAL = ("view", "spec", "result", "missing", "settings")
_UNSET = object()


def _tier_pool(
    *,
    view: LayoutView,
    spec: DocTypeSpec,
    result: ExtractionResult,
    missing: list[str],
    settings: Settings,
    classification: Classification | None,
    content: bytes | None,
) -> dict[str, Any]:
    """Everything a tier could plausibly ask for, under every name it might ask for it.

    Two distinctions matter and are kept apart deliberately, because getting them the wrong way
    round is a silent bug rather than a crash:

    * ``missing`` is a list of :class:`~dce.models.FieldSpec` — the declarations, which is what
      a tier needs in order to build a schema or a prompt. The bare *names* are ``field_names``.
    * ``doctype_id`` is the string, ``spec`` is the :class:`~dce.models.DocTypeSpec`.
    """
    wanted = set(missing)
    field_specs = [f for f in spec.fields if f.name in wanted]
    return {
        # the document, in each form a tier might want it
        "view": view, "layout": view, "layout_view": view, "document": view,
        "data": content, "content": content, "document_bytes": content, "file_bytes": content,
        # the doctype
        "doctype_id": spec.doctype_id, "doctype": spec.doctype_id,
        "spec": spec, "doctype_spec": spec, "doc_spec": spec,
        # what is still missing — declarations and names, never confused
        "missing": field_specs, "missing_fields": field_specs, "fields": field_specs,
        "field_specs": field_specs, "targets": field_specs,
        "field_names": missing, "missing_names": missing, "names": missing,
        # context
        "result": result, "extraction": result, "current": result, "partial": result,
        "settings": settings, "config": settings, "cfg": settings,
        "classification": classification,
    }


def _resolve_awaitable(value: Any) -> Any:
    """Run a coroutine to completion from a synchronous handler.

    The tier modules are ``async`` — they speak HTTP — while ``/process`` is a **sync** handler
    on purpose: classification and local extraction are CPU-bound, and FastAPI runs sync
    handlers in a worker thread where they cannot stall the event loop for every other request.
    That worker thread has no running loop, so :func:`asyncio.run` is the correct bridge.

    The fallback matters for tests, which may call the cascade from inside a loop: driving the
    coroutine on a private thread keeps a blocking tier off an event loop rather than
    deadlocking on it.

    Args:
        value: A coroutine/awaitable, or anything else (returned unchanged).

    Returns:
        The awaited result.
    """
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)  # the normal path: a threadpool worker, no loop
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, value).result()


def _invoke_tier(fn: Any, pool: dict[str, Any]) -> Any:
    """Call a tier with whatever its signature actually asks for.

    T2, T3 and T4 land as independent pieces of work, and pinning them to one exact signature
    would mean the first one to disagree silently does not run. So parameters are matched by
    *name* against :func:`_tier_pool`, and any parameter whose name is not recognised is filled
    positionally in :data:`_TIER_POSITIONAL` order — which is the signature everyone would have
    written anyway.

    Args:
        fn: The tier callable.
        pool: Candidate arguments by name.

    Returns:
        Whatever the tier returned, uncoerced.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # builtins and C callables have no signature
        return fn(pool["view"], pool["spec"], pool["result"])

    fallback = [pool[name] for name in _TIER_POSITIONAL]
    positional: list[Any] = []
    keywords: dict[str, Any] = {}
    for index, (name, param) in enumerate(params.items()):
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        value = pool.get(name, _UNSET)
        if value is _UNSET:
            if param.default is not inspect.Parameter.empty:
                continue
            if index >= len(fallback):
                continue
            value = fallback[index]
        if param.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        else:
            keywords[name] = value
    return fn(*positional, **keywords)


def _as_extracted_fields(value: Any) -> list[ExtractedField]:
    """Coerce whatever a tier returned into extracted fields; unusable shapes yield nothing."""
    if value is None:
        return []
    if isinstance(value, ExtractionResult):
        return list(value.fields)
    if isinstance(value, ExtractedField):
        return [value]
    if isinstance(value, dict):
        inner = value.get("fields")
        if isinstance(inner, list | tuple):
            return _as_extracted_fields(list(inner))
        out: list[ExtractedField] = []
        for key, item in value.items():
            if isinstance(item, ExtractedField):
                out.append(item)
            elif isinstance(item, dict):
                out.extend(_as_extracted_fields([{"name": str(key), **item}]))
            elif isinstance(item, str):
                out.append(ExtractedField(name=str(key), value=item))
        return out
    if isinstance(value, list | tuple):
        out = []
        for item in value:
            if isinstance(item, ExtractedField):
                out.append(item)
            elif isinstance(item, dict):
                try:
                    out.append(ExtractedField(**item))
                except ValidationError:
                    logger.warning("tier returned an unusable field payload: %r", item)
        return out
    nested = getattr(value, "fields", None)
    if nested is not None and nested is not value:
        return _as_extracted_fields(nested)
    return []


class TierPort:
    """Adapter over one paid tier (T2/T3/T4). Always returns extracted fields."""

    def __init__(self, fn: Any, tier: TierSpec, module: Any = None) -> None:
        self._fn = fn
        self._module = module
        self.tier = tier

    def applies_to(self, doctype_id: str) -> bool:
        """Whether this tier has anything to offer for this doctype.

        Azure ships a prebuilt specialist for a handful of the 121 registered doctypes; for the
        rest T2's honest answer is "stay on T1". Asking the module's own mapping first means
        discovering that costs nothing — and, more importantly, that the spend counter is not
        incremented for a call that never happened.
        """
        if not self.tier.applies_attr:
            return True
        mapping = getattr(self._module, self.tier.applies_attr, None)
        if not isinstance(mapping, dict):
            return True
        return (doctype_id or "").strip().lower() in mapping

    def run(
        self,
        *,
        view: LayoutView,
        spec: DocTypeSpec,
        result: ExtractionResult,
        missing: list[str],
        settings: Settings,
        classification: Classification | None = None,
        content: bytes | None = None,
    ) -> list[ExtractedField]:
        """Ask the tier for the fields that are still missing.

        Args:
            view: The layout view. The document type is already known at this point.
            spec: The accepted doctype's spec.
            result: The extraction so far — a tier may read it, but what it returns is
                filtered by the caller.
            missing: Names of fields still without a value. The tier is asked for these only.
            settings: Endpoints, keys and per-tier limits.
            classification: The accepted classification, for tiers that want the evidence.
            content: The original document bytes, for the tiers that analyse the file itself.

        Returns:
            Candidate fields, in no particular order. Unusable shapes come back empty rather
            than raising: a paid tier that returns nonsense costs money, not a request.
        """
        pool = _tier_pool(
            view=view,
            spec=spec,
            result=result,
            missing=missing,
            settings=settings,
            classification=classification,
            content=content,
        )
        return _as_extracted_fields(_resolve_awaitable(_invoke_tier(self._fn, pool)))


class ReviewConflict(RuntimeError):
    """The queue refused a decision it understood: already decided, or double-entry refused."""


class ReviewPort:
    """Adapter over T5 — :mod:`dce.review`: the state machine plus the store it writes to.

    The queue is the tier that makes every other tier safe to switch on: an abstention, a
    missing required field and a value no checksum could confirm all end here rather than in a
    database. Two objects make that work and this port holds both, because that is how
    :mod:`dce.review` is built and the split is right: the **module** owns the transitions
    (including blind double entry on PII checksum fields, which is a control and belongs in one
    auditable place), and the **queue** owns storage and is a
    :class:`~typing.Protocol` a team can reimplement over Postgres without touching the rules.

    Items are **per field**, not per document: ``"<doc_id>:<field_name>"``. A reviewer decides
    one field at a time, because that is what a reviewer actually does, and because an
    approval that covered a whole document would silently cover the field nobody looked at.
    """

    _ENQUEUE = ("enqueue_from_result", "enqueue")
    _LIST = ("list", "list_items", "items")
    _GET = ("get", "item", "find")
    _DEPTH = ("depth", "size", "count", "pending_count")
    _DECIDE: ClassVar[dict[str, tuple[str, ...]]] = {
        "approve": ("approve", "accept"),
        "reject": ("reject", "decline"),
        "correct": ("correct", "amend", "update"),
    }

    def __init__(self, module: Any, queue: Any) -> None:
        self._module = module
        self._queue = queue

    @property
    def queue(self) -> Any:
        """The backing store, passed to the module's functions unchanged."""
        return self._queue

    @staticmethod
    def _attr(holder: Any, names: tuple[str, ...]) -> Any | None:
        for name in names:
            candidate = getattr(holder, name, None)
            if callable(candidate):
                return candidate
        return None

    def usable(self) -> bool:
        """Whether this pair can actually serve the review endpoints."""
        if self._queue is None:
            return False
        decides = all(
            self._attr(self._module, names) or self._attr(self._queue, names)
            for names in self._DECIDE.values()
        )
        return bool(self._attr(self._queue, self._LIST)) and bool(decides)

    def _status(self, status: str | None) -> Any:
        """Coerce a status string to the module\'s enum.

        The store filters with ``is``, so a bare string silently matches nothing — which would
        make ``?status=pending`` return an empty queue and read as "all clear".
        """
        if status is None:
            return None
        enum_type = getattr(self._module, "ReviewStatus", None)
        if enum_type is None:
            return status
        try:
            return enum_type(status)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"unknown status {status!r}; expected one of: "
                + ", ".join(sorted(str(member.value) for member in enum_type)),
            ) from exc

    def enqueue(
        self,
        result: ExtractionResult,
        *,
        doc_id: str,
        doctype_id: str,
        field_specs: Any = None,
        settings: Settings | None = None,
    ) -> list[str]:
        """Turn an extraction result into items a human has to look at.

        Args:
            result: The extraction result — for an abstention, the empty one ``/process``
                would have returned.
            doc_id: Document id; the stable half of every item id.
            doctype_id: Accepted doctype, or ``unknown``.
            field_specs: The doctype spec, so the queue can tell which fields are PII +
                checksum and therefore need two pairs of eyes.
            settings: Read for the confidence threshold.

        Returns:
            The ids created. Empty when nothing needed a human — or when the same document was
            already queued, because re-processing must not resurrect a decision somebody made.
        """
        fn = self._attr(self._module, self._ENQUEUE)
        if fn is None:
            return []
        created = _call_supported(
            fn,
            result,
            doc_id=doc_id,
            doctype_id=doctype_id,
            queue=self._queue,
            field_specs=field_specs,
            settings=settings,
        )
        return [str(_review_dict(item).get("id", "")) for item in (created or [])]

    def list(
        self, *, status: str | None, doctype: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """List queue items, filtered by status and doctype.

        The store filters by status and document; the doctype filter is applied here, because
        the store does not have one and a caller asking for ``?doctype=us_w9`` must not be
        handed a page of everybody else\'s PII by way of an ignored keyword.
        """
        fn = self._attr(self._queue, self._LIST)
        if fn is None:
            return []
        raw = _call_supported(fn, status=self._status(status), limit=limit)
        items = [_review_dict(item) for item in (raw or [])]
        if doctype:
            items = [i for i in items if str(i.get("doctype_id", "")) == doctype]
        return items[: max(1, limit)]

    def decide(
        self,
        item_id: str,
        decision: str,
        *,
        reviewer: str,
        note: str = "",
        value: str = "",
    ) -> dict[str, Any] | None:
        """Record one human decision.

        Args:
            item_id: Queue item id (``"<doc_id>:<field_name>"``).
            decision: ``approve`` | ``reject`` | ``correct``.
            reviewer: Who decided. Never blank — an unattributed KYC decision is not a
                decision, and double entry needs two distinct identities to mean anything.
            note: Free text kept with the item.
            value: The corrected value, for ``correct``.

        Returns:
            The updated item, or ``None`` when no implementation could be found. Note that an
            item may still come back ``pending``: that is what a first entry on a double-entry
            field looks like, and it is a success, not a failure.

        Raises:
            ReviewConflict: The queue understood the request and refused it — already decided,
                the same reviewer signing twice, or a double-entry mismatch.
        """
        fn = self._attr(self._module, self._DECIDE[decision])
        args: tuple[Any, ...] = (self._queue, item_id)
        if fn is None:
            fn = self._attr(self._queue, self._DECIDE[decision])
            args = (item_id,)
        if fn is None:
            return None
        try:
            updated = _call_supported(
                fn,
                *args,
                reviewer=reviewer or None,
                note=note or None,
                value=value or None,
            )
        except (RuntimeError, ValueError) as exc:
            # dce.review.ReviewError subclasses RuntimeError. Everything it raises is a refusal
            # with a sentence in it, and that sentence is what the reviewer needs to read.
            raise ReviewConflict(str(exc)) from exc
        return _review_dict(updated) if updated is not None else None

    def get(self, item_id: str) -> dict[str, Any] | None:
        """One item by id, or ``None``."""
        fn = self._attr(self._queue, self._GET)
        if fn is None:
            return None
        found = fn(item_id)
        return _review_dict(found) if found is not None else None

    def depth(self) -> int | None:
        """Number of items still pending, when the queue can say."""
        fn = self._attr(self._queue, self._DEPTH)
        try:
            if fn is not None:
                return int(fn())
            return len(self._queue)
        except (TypeError, ValueError):
            return None


def _review_dict(raw: Any) -> dict[str, Any]:
    """Coerce a queue item — model, dict or object — into a JSON-safe dict."""
    if raw is None:
        return {}
    if isinstance(raw, BaseModel):
        data: dict[str, Any] = raw.model_dump(mode="json")
    elif isinstance(raw, dict):
        data = dict(raw)
    elif hasattr(raw, "__dict__") and vars(raw):
        data = {k: v for k, v in vars(raw).items() if not k.startswith("_")}
    else:
        data = {"id": str(raw)}
    return dict(jsonable_encoder(data))


def _unavailable(what: str, modules: tuple[str, ...]) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=f"{what} is not available (looked in: {', '.join(modules)})",
    )


def _load(modules: tuple[str, ...], names: tuple[str, ...], method: str) -> Any | None:
    """Find the first usable callable named in ``names`` across ``modules``."""
    for module_name in modules:
        module = _import(module_name)
        if module is None:
            continue
        impl = _first_attr(module, *names)
        if impl is None:
            continue
        try:
            if inspect.isclass(impl):
                impl = _call_supported(impl)
            if hasattr(impl, method):
                impl = getattr(impl, method)
        except Exception:
            logger.warning("could not instantiate engine from %s", module_name, exc_info=True)
            continue
        if callable(impl):
            return impl
    return None


def load_registry_port() -> RegistryPort | None:
    """Resolve the doctype registry, or ``None`` when no module provides one.

    The module itself is tried first: a registry exposed as module-level ``all_specs()`` +
    ``get()`` is the shape this codebase uses, and going through it keeps the engine's own
    indexed lookup instead of falling back to a linear scan over a copied list.
    """
    for module_name in _REGISTRY_MODULES:
        module = _import(module_name)
        if module is None:
            continue
        port = RegistryPort(module)
        if port.specs():
            return port
        impl = _first_attr(module, *_REGISTRY_NAMES)
        if impl is None:
            continue
        try:
            if callable(impl):
                impl = _call_supported(impl)
        except Exception:
            logger.warning("registry factory in %s raised", module_name, exc_info=True)
            continue
        port = RegistryPort(impl)
        if port.specs():
            return port
    return None


def load_classifier_port() -> ClassifierPort | None:
    """Resolve the classification cascade, or ``None`` when no module provides one."""
    fn = _load(_CLASSIFY_MODULES, _CLASSIFY_NAMES, "classify")
    return ClassifierPort(fn) if fn is not None else None


def load_extractor_port() -> ExtractorPort | None:
    """Resolve the field extractor, or ``None`` when no module provides one."""
    fn = _load(_EXTRACT_MODULES, _EXTRACT_NAMES, "extract")
    return ExtractorPort(fn) if fn is not None else None


def load_tier_port(tier: TierSpec) -> TierPort | None:
    """Resolve one paid tier, or ``None`` when its module is absent or not importable.

    **Called from inside a request handler, never at import.** These modules reach for an HTTP
    client; the classification path must not import one, and the strongest way to say that is
    for the import not to have happened yet while the classifier is running.

    Args:
        tier: Which tier to resolve.

    Returns:
        The port, or ``None`` when nothing usable was found — which is also what a deployment
        that installed no HTTP client sees, and is reported rather than raised.
    """
    for module_name in tier.modules:
        module = _import(module_name)
        if module is None:
            continue
        fn = _first_attr(module, *tier.entrypoints)
        if fn is None:
            continue
        if inspect.isclass(fn):
            try:
                fn = _call_supported(fn)
            except Exception:
                logger.warning("could not instantiate %s from %s", tier.tier, module_name,
                               exc_info=True)
                continue
            fn = getattr(fn, "run", fn)
        if callable(fn):
            return TierPort(fn, tier, module)
    return None


def load_review_port(settings: Settings | None = None) -> ReviewPort | None:
    """Resolve the human review queue, or ``None`` when no module provides one.

    Args:
        settings: Passed to the queue factory, so the backend and path this deployment
            configured are the ones the queue uses.

    Returns:
        A port over ``(module, queue)``, or ``None``.
    """
    for module_name in _REVIEW_MODULES:
        module = _import(module_name)
        if module is None:
            continue
        factory = _first_attr(module, *_REVIEW_FACTORIES)
        queue: Any = None
        if factory is not None:
            try:
                queue = (
                    _call_supported(factory, settings=settings) if callable(factory) else factory
                )
            except Exception:
                logger.warning("review queue factory in %s raised", module_name, exc_info=True)
                queue = None
        port = ReviewPort(module, queue)
        if port.usable():
            return port
    return None


def bert_status(settings: Settings) -> dict[str, Any]:
    """Report the optional local BERT tier: enabled, loaded, and where it is mounted."""
    loaded = False
    # Probed only when the tier is switched on: importing the BERT module pulls in torch (and
    # possibly tensorflow), which a readiness check has no business doing on a deployment that
    # deliberately left the tier off.
    if settings.bert_enabled:
        for module_name in _BERT_MODULES:
            module = _import(module_name)
            probe = (
                _first_attr(module, "tier_available", "is_loaded", "loaded", "LOADED")
                if module
                else None
            )
            if probe is None:
                continue
            try:
                loaded = bool(
                    _call_supported(probe, settings=settings) if callable(probe) else probe
                )
            except Exception:  # noqa: BLE001 - a probe must never break readiness
                loaded = False
            break
    return {
        "enabled": settings.bert_enabled,
        "loaded": loaded,
        "model_dir": settings.bert_model_dir,
        "device": settings.bert_device,
    }


# ---------------------------------------------------------------------------
# Dependencies (override these in tests)
# ---------------------------------------------------------------------------
def get_app_settings(request: Request) -> Settings:
    """The settings this app was built with."""
    return request.app.state.settings


def _cached_port(request: Request, name: str, loader: Any) -> Any | None:
    """Resolve an engine port once per app, caching the result on ``app.state``."""
    port = getattr(request.app.state, name, None)
    if port is None:
        port = loader()
        setattr(request.app.state, name, port)
    return port


def get_registry_or_none(request: Request) -> RegistryPort | None:
    """The doctype registry, or ``None`` when no module provides one."""
    return _cached_port(request, "registry", load_registry_port)


def get_classifier_or_none(request: Request) -> ClassifierPort | None:
    """The classification cascade, or ``None`` when no module provides one."""
    return _cached_port(request, "classifier", load_classifier_port)


def get_extractor_or_none(request: Request) -> ExtractorPort | None:
    """The field extractor, or ``None`` when no module provides one."""
    return _cached_port(request, "extractor", load_extractor_port)


def tier_port(request: Request, tier: TierSpec) -> TierPort | None:
    """Resolve a paid tier for this request, caching the outcome on ``app.state``.

    Deliberately **not** a FastAPI dependency. Dependencies resolve before the handler body
    runs — that is, before classification — and the one thing these modules must not do is get
    imported while an unclassified document is in memory. This is called from inside the
    handler, after a doctype has been accepted, and only when the tier's flag is on.

    A tier that could not be resolved is remembered as ``False`` so a missing module is not
    re-probed on every request.
    """
    cached = getattr(request.app.state, tier.state_attr, None)
    if cached is None:
        cached = load_tier_port(tier) or False
        setattr(request.app.state, tier.state_attr, cached)
    return cached or None


def review_port(request: Request) -> ReviewPort | None:
    """Resolve the review queue for this request, caching it on ``app.state``."""
    cached = getattr(request.app.state, "review", None)
    if cached is None:
        cached = load_review_port(request.app.state.settings) or False
        request.app.state.review = cached
    return cached or None


def require_review_queue(request: Request) -> ReviewPort:
    """The review queue.

    Raises:
        HTTPException: ``503`` when no review module could be loaded. A review endpoint that
            answered ``200`` with an empty list would tell an operator the queue is clear when
            in fact it does not exist.
    """
    port = review_port(request)
    if port is None:
        raise _unavailable("review queue", _REVIEW_MODULES)
    return port


def get_registry(request: Request) -> RegistryPort:
    """The doctype registry.

    Raises:
        HTTPException: ``503`` when no registry module could be loaded.
    """
    port = get_registry_or_none(request)
    if port is None:
        raise _unavailable("doctype registry", _REGISTRY_MODULES)
    return port


def get_classifier(request: Request) -> ClassifierPort:
    """The classification cascade.

    Raises:
        HTTPException: ``503`` when no classifier module could be loaded.
    """
    port = get_classifier_or_none(request)
    if port is None:
        raise _unavailable("classifier", _CLASSIFY_MODULES)
    return port


def get_extractor(request: Request) -> ExtractorPort:
    """The field extractor.

    Raises:
        HTTPException: ``503`` when no extractor module could be loaded.
    """
    port = get_extractor_or_none(request)
    if port is None:
        raise _unavailable("extractor", _EXTRACT_MODULES)
    return port


def require_api_key(
    request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")
) -> None:
    """Enforce ``X-API-Key`` when one is configured.

    An empty ``api_key`` setting disables the gate — the service is often deployed on an
    internal network behind a mesh that already authenticates. Comparison is constant-time.

    Raises:
        HTTPException: ``401`` when a key is configured and the header is missing or wrong.
    """
    expected = request.app.state.settings.api_key
    if not expected:
        return
    try:
        presented = (x_api_key or "").encode("utf-8")
        if presented and hmac.compare_digest(presented, expected.encode("utf-8")):
            return
    except (AttributeError, TypeError):  # pragma: no cover - defensive
        pass
    raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class DocumentRequest(BaseModel):
    """One document, in whichever form the caller has it.

    Exactly one payload field is used, in this order: ``layout`` (already adapted),
    ``azure_analyze_result``, ``azure_read_result``, ``des_ocr``, ``text``. Sending only
    ``text`` is supported and degrades gracefully — see :func:`dce.adapters.from_plain_text`.

    **These fields are the zero-egress way to use a cloud recogniser, and the recommended
    one.** An upstream service that already holds the document runs Azure Read or Azure
    Layout under its own authorisation and posts the result here; this service opens no
    socket, so the pre-classification invariant is not weighed against anything. The
    alternative — having *this* service call an OCR endpoint during ingestion — exists
    (``DCE_INGEST_OCR_SERVICE_ENABLED``, :mod:`dce.ingest.ocr_service`), is off by default,
    and is reported on ``/readyz``.
    """

    doc_id: str = ""
    layout: LayoutView | None = None
    text: str | None = None
    #: An Azure payload of **either** product. The shape decides which adapter runs — Read
    #: v3.2 is the only shape with ``analyzeResult.readResults``, Document Intelligence v4.0
    #: uses ``pages``/``paragraphs``/``tables`` — and the choice is reported back on every
    #: response (the ``X-Document-Source`` header, and ``source`` on ``/process``) rather than
    #: applied silently. Auto-detection is a convenience; being vague about which provider
    #: read a document is not, because the two do not classify equally well.
    azure_analyze_result: dict[str, Any] | None = None
    #: The same payload, declared unambiguously as Read v3.2. Use it when you want the Read
    #: adapter whatever the shape sniffer would have concluded. **Read carries no paragraph
    #: roles**, so every block lands in ``body`` and no title-gated decisive anchor can fire —
    #: see :func:`dce.adapters.from_azure_read` for exactly what that costs.
    azure_read_result: dict[str, Any] | None = None
    des_ocr: dict[str, Any] | None = None
    #: The original file, base64. **Optional, and read by nothing until a doctype has been
    #: accepted.** T2 and T3 send the document itself to Azure — a prebuilt specialist analyses
    #: the file, not our reading of it — so those tiers are skipped, with a reason, when it is
    #: absent. T1 and T4 never look at it. Classification never looks at it. A deployment with
    #: the paid tiers off should not send it at all: the smallest way to keep a document out of
    #: a network call is not to hand it over in the first place.
    content_base64: str | None = None
    #: Set this to have ``content_base64`` **parsed in this process** into a layout, instead
    #: of being carried untouched for the paid tiers. That is the whole difference, and it is
    #: how a caller holding a ``.docx``, an ``.xlsx``, an ``.eml`` or a photograph — and no
    #: OCR stack of their own — gets in at all. The format is decided from the bytes, never
    #: from the filename (:mod:`dce.ingest.detect`); an image or a scanned PDF returns a
    #: structured ``422`` naming ``needs_ocr`` rather than a guess, because recognising an
    #: unclassified document on somebody else's servers is the disclosure this service exists
    #: to prevent. Leave it unset and nothing changes: ``content_base64`` keeps exactly its
    #: old meaning.
    ingest: IngestOptions | None = None


class ExtractRequest(DocumentRequest):
    """A document plus an optional doctype pin.

    Omitting ``doctype_id`` makes this ``/process`` without the envelope: the document is
    classified first, and an abstention returns an empty result flagged for review rather than
    guessing a doctype.
    """

    doctype_id: str | None = None
    schema_version: str | None = None


class InduceRequest(BaseModel):
    """Sample documents to draft a schema from."""

    doctype_id: str = Field(min_length=1)
    label: str = ""
    country: str = "XX"
    samples: list[DocumentRequest] = Field(min_length=1)
    #: Fraction of samples a candidate field must appear in to make the draft.
    min_support: float = Field(default=0.5, ge=0.0, le=1.0)


class Timings(BaseModel):
    """Server-side timings, in milliseconds. Also mirrored on the ``X-Elapsed-Ms`` header."""

    total_ms: int = 0
    adapt_ms: int = 0
    classify_ms: int = 0
    extract_ms: int = 0
    #: Time spent in the paid tiers (T2-T4). Zero on a default deployment, and the number that
    #: explains a ``/process`` that took eleven seconds instead of eleven milliseconds.
    tiers_ms: int = 0


class TierRun(BaseModel):
    """What one extraction tier did to this document.

    Reported for every tier that actually executed, so an operator can answer "what did this
    document cost, and what did I get for it" from the response itself rather than by
    correlating three metrics after the fact. Tiers that are switched off do not appear at all
    — absence is what "we did not spend anything" looks like.
    """

    tier: str
    #: ``ran`` | ``error`` | ``unavailable`` | ``misconfigured`` | ``queued``.
    status: str = "ran"
    fields_filled: int = 0
    #: Which fields this tier filled. The per-field provenance is on the field's ``locator``.
    fields: list[str] = Field(default_factory=list)
    ms: int = 0
    #: True when the tier made a call somebody bills for. An ``error`` after the call was made
    #: is still cost-bearing: the request happened, and it will be on the invoice.
    cost_bearing: bool = False
    detail: str = ""


class DocumentSource(BaseModel):
    """Which reading of the document the cascade actually scored, and where it came from.

    Reported on every request — as the ``X-Document-Source`` header on ``/classify`` and
    ``/extract``, and as ``source`` here — for two reasons that are really the same reason:

    * **The adapter is not an implementation detail.** ``azure-read-v3.2`` and
      ``azure-prebuilt-layout`` are two different products with two different JSON shapes and
      two different ceilings on accuracy: Read predicts no paragraph roles, so a Read payload
      can never satisfy a title-gated decisive anchor. A caller who meant to send Layout and
      sent Read gets a *worse* answer, not an error, and has to be able to see that from the
      response. Auto-detection is only kind if it is also loud.
    * **Whether reading the document required a call out of this process is an architectural
      fact**, not a performance note. ``remote`` is True exactly when this service sent the
      bytes to an OCR endpoint to have them read — which only happens on a deployment that
      configured one (``DCE_INGEST_OCR_SERVICE_ENABLED``). Whose network that endpoint is on
      is the deployment's declaration, reported on ``/readyz`` and echoed in ``note``.
    """

    #: ``azure-prebuilt-layout`` | ``azure-read-v3.2`` | ``des-ocr`` | ``plain-text`` |
    #: ``dce.ingest`` | ``dce.ingest.ocr_service`` | ``caller-layout``.
    provider: str = ""
    #: True when obtaining this text required a call to an OCR service **made by this
    #: service**, before the doctype was known. False on every caller-supplied path.
    remote: bool = False
    #: Host the document was sent to, when ``remote``. Never the full URL.
    endpoint_host: str = ""
    #: One sentence an operator can read without knowing the field names.
    note: str = ""


class ProcessResponse(BaseModel):
    """Classification plus extraction, or classification alone when the cascade abstained."""

    classification: Classification
    #: Which adapter read this document, and whether reading it involved egress.
    source: DocumentSource = Field(default_factory=DocumentSource)
    extraction: ExtractionResult | None = None
    needs_review: bool = False
    detail: str = ""
    #: The tier ledger, in the order the tiers ran: T1 always, then whichever paid tiers were
    #: enabled and still had something to look for, then the review queue if it was used.
    tiers_used: list[TierRun] = Field(default_factory=list)
    #: Ids of the review-queue items this document produced. Plural because the queue is
    #: per-field: three unverified fields are three things for a human to look at, and one item
    #: covering all of them would be approved once by somebody who checked one.
    review_ids: list[str] = Field(default_factory=list)
    timings: Timings = Field(default_factory=Timings)


class BoundaryEvidence(BaseModel):
    """Why one split was proposed. Present so a segmentation can be argued with."""

    #: First page of the new document, 1-based.
    page: int
    #: ``adequacy`` | ``geometry`` | ``first_page_anchor``.
    signal: str
    detail: str = ""


class DocumentSegment(BaseModel):
    """One document found inside an upload, and what it turned out to be."""

    start_page: int
    end_page: int
    page_count: int
    #: The classification of **these pages alone**, produced by classifying the span whole.
    #: Not a page's classification promoted to stand for its neighbours.
    classification: Classification
    #: Populated on ``/process/segments``; ``None`` when the span abstained, because nothing
    #: is extracted from a document nobody has identified.
    extraction: ExtractionResult | None = None
    needs_review: bool = False
    #: What ran for THIS document, and what it cost. Reported per segment rather than once
    #: per file because a bundle's tiers are per document: one segment can abstain and run
    #: nothing while its neighbour extracts seven fields, and a single ledger would have to
    #: pick one of those to describe. An empty list means the segment abstained.
    tiers_used: list[TierRun] = Field(default_factory=list)


class PageRead(BaseModel):
    """How one page was read, as the reader measured it.

    Reported because "is the view right?" has to be answerable before "is the classifier
    right?", and until now the only way to ask it was from a Python shell. A page that
    contributed nothing to a classification looks identical, in every other field of every
    other response, to a page that genuinely held nothing.
    """

    page: int
    width: float = 0.0
    height: float = 0.0
    #: Alphanumeric characters in this page's own text layer.
    alnum_chars: int = 0
    #: Whether that text was judged worth classifying on. ``None`` means nothing measured it,
    #: which is not the same as ``false``.
    text_adequate: bool | None = None
    #: Share of the page covered by its largest single image.
    image_fraction: float = 0.0


class SegmentsResponse(BaseModel):
    """What an upload turned out to contain.

    ``segments`` always holds at least one entry: a file with no boundary evidence comes back
    as a single segment covering every page, classified exactly as ``/classify`` would have
    classified it. That uniformity is deliberate — a caller never needs to branch on whether
    the upload happened to be a bundle.
    """

    segments: list[DocumentSegment] = Field(default_factory=list)
    #: True when more than one document was found. The plain answer to "is this a bundle?",
    #: so a caller does not have to infer it from a list length.
    segmented: bool = False
    #: Every surviving split and what proposed it. Empty for a single-document upload.
    boundaries: list[BoundaryEvidence] = Field(default_factory=list)
    source: DocumentSource = Field(default_factory=DocumentSource)
    #: How each page was read. The answer to "did the reader see this page at all", which has
    #: to come before any question about the classifier.
    pages: list[PageRead] = Field(default_factory=list)
    page_count: int = 0
    ms: int = 0


class ReviewItem(BaseModel):
    """One document waiting for (or already seen by) a human.

    ``extra="allow"``: the queue is :mod:`dce.review`'s to shape, and a router that dropped
    fields it did not recognise would quietly hide whatever the queue added last week.
    """

    model_config = ConfigDict(extra="allow")

    id: str = ""
    doc_id: str = ""
    doctype_id: str = UNKNOWN
    #: The field under review. One item is one field — that is what a reviewer decides.
    field_name: str = ""
    value: str | None = None
    confidence: float = 0.0
    status: str = "pending"
    #: Machine-readable prefix first: ``classification_abstained``, ``missing_required``,
    #: ``below_confidence_threshold``, ``validator_error``.
    reason: str = ""
    #: Page and region, so a review UI can show the pixels the value came from.
    page: int | None = None
    bbox: list[float] | None = None
    pii: bool = False
    #: The double-entry ledger: who has signed, and how many signatures this field needs.
    approvals: list[str] = Field(default_factory=list)
    required_approvals: int = 1
    corrected_value: str | None = None
    decision_note: str = ""
    created_at: str = ""
    decided_at: str | None = None
    reviewer: str = ""


class ReviewListResponse(BaseModel):
    count: int
    #: Items currently waiting, oldest first if the queue orders them.
    items: list[ReviewItem] = Field(default_factory=list)
    #: Total queue depth, when the queue can say — ``count`` is after filtering and paging.
    depth: int | None = None
    timings: Timings = Field(default_factory=Timings)


class ReviewDecision(BaseModel):
    """A human's decision on one queue item — which is one **field** of one document.

    ``reviewer`` is required and not defaulted. An unattributed decision in a KYC system is not
    a decision, and blind double entry — two independent approvals, or two matching keyings, on
    a field that is both PII and checksum-backed — is meaningless without two distinct
    identities to check against each other.
    """

    reviewer: str = Field(min_length=1)
    note: str = ""
    #: The corrected value. Required by ``/correct``, ignored by the others. One value, because
    #: one item is one field: a reviewer types the identifier they can see on the page.
    value: str = ""


class DocTypeSummary(BaseModel):
    """One registry entry, as listed by ``GET /api/v1/doctypes``."""

    doctype_id: str
    label: str
    country: str
    category: Category
    issuing_authority: str = ""
    applies_to: str = "individual"
    officially_valid: bool = False
    anchors: int = 0
    fields: list[str] = Field(default_factory=list)


class DocTypeListResponse(BaseModel):
    count: int
    doctypes: list[DocTypeSummary]
    timings: Timings = Field(default_factory=Timings)


class SchemaResponse(BaseModel):
    """A doctype's field schema. ``active`` is ``False`` for a freshly induced draft."""

    doctype_id: str
    schema_version: str
    active: bool
    source: str
    label: str = ""
    country: str = ""
    fields: list[FieldSpec] = Field(default_factory=list)
    sample_count: int = 0
    notes: str = ""
    timings: Timings = Field(default_factory=Timings)


class RegistryStatus(BaseModel):
    loaded: bool
    doctypes: int = 0
    countries: list[str] = Field(default_factory=list)


class EgressStatus(BaseModel):
    """The invariant, reported so an operator can see it without reading the config.

    ``enforced`` is the *guard's* state: would :func:`dce.egress.assert_no_egress` refuse a
    call made during classification. ``preclassification_ocr`` is a deliberately separate
    question — whether this deployment sends documents out **before** the cascade runs at
    all, to have them read. A deployment can have the guard fully armed and still be doing
    that, because ingestion completes before the classification scope is ever entered. So
    reporting only ``enforced: true`` would be a true sentence that leaves an auditor with a
    false impression, which is exactly what this block exists to prevent.
    """

    preclassification_allowed: bool
    enforced: bool
    note: str
    #: True when an OCR service provider is configured and usable here: unclassified
    #: documents are sent out of this process, over a socket, during ingestion.
    #:
    #: **Whether that leaves the organisation's control is a separate question**, answered by
    #: ``preclassification_ocr_trust_boundary``. This flag stays true on an on-premises
    #: deployment because the operation is the same one and an auditor asking "does anything
    #: leave the process before classification" must not get "no" from a host declaration. The
    #: two fields together say: yes, to that host, which the deployment declares is its own.
    preclassification_ocr: bool = False
    #: Host they are sent to. Empty unless ``preclassification_ocr``.
    preclassification_ocr_endpoint: str = ""
    #: ``external`` | ``on_premises`` — the deployment's own declaration about that host, from
    #: ``DCE_INGEST_OCR_SERVICE_TRUST_BOUNDARY``. Empty unless ``preclassification_ocr``. It
    #: is a claim, not a finding: see :class:`OcrStatus.trust_boundary_attribution`.
    preclassification_ocr_trust_boundary: str = ""


class OcrProviderStatus(BaseModel):
    """One recogniser this build knows how to use, and whether it is usable *here*.

    Every known provider is listed on every deployment, including the ones that are switched
    off. That is deliberate and it is the difference between a console that can tell an
    operator "this deployment does not send documents to Azure Read" and one that can only
    tell them "nobody mentioned Azure Read". Absence is not a disclosure; a row saying
    ``available: false`` with a reason is.

    It also gives a caller the exact strings the ``ocr_provider`` pin accepts, so the pin is
    something a client can use correctly rather than guess at.
    """

    #: The wire name, and the value :class:`IngestOptions.ocr_provider` must be given to
    #: select this provider: ``rapidocr`` | ``tesseract`` | ``azure_read`` | ``azure_layout``.
    name: str
    #: True for every provider this deployment configured. More than one can be true: a
    #: deployment may offer an in-process engine and one or both OCR services side by side,
    #: and a request chooses between them with ``ingest.ocr_provider``.
    available: bool = False
    #: True for the single provider that runs when a request names none.
    default: bool = False
    #: **The architectural fact.** True when this provider reads a document by calling another
    #: host rather than in this process. From the provider record's ``service`` flag, never
    #: from its name. It says nothing about *whose* host that is — see
    #: :class:`OcrStatus.trust_boundary`, which is the deployment's own declaration.
    network: bool = False
    #: ``roles`` or ``lines``. Reported because it decides which anchors could fire at all —
    #: see :data:`dce.ingest.ocr._STRUCTURE`.
    structure: str = "lines"
    #: Host that would receive documents, when ``network`` and configured. Host, never a URL.
    endpoint: str = ""
    #: Why it is not available here. Never empty when ``available`` is false.
    reason: str = ""
    summary: str = ""


class OcrStatus(BaseModel):
    """How this deployment turns an image into text, and whether that leaves the process.

    Reported unconditionally — including the ordinary answer, ``provider: "none"`` — so that
    "images are read by a service at <host>" is a value in a field an operator already reads,
    rather than something that appears only once it is true and can therefore only be noticed
    by somebody who already knew to look for it.
    """

    #: The recogniser that runs when a request names none:
    #: ``none`` | ``rapidocr`` | ``tesseract`` | ``azure_read`` | ``azure_layout``.
    provider: str = "none"
    enabled: bool = False
    #: **The architectural fact.** True when *any* configured recogniser reads a document by
    #: calling another host — not merely when the default one does, because a request may
    #: select any configured provider and an operator asking "can reading an image here
    #: involve a call out" must not get "no" because the default happens to be in-process.
    #: Taken from the provider records' ``service`` flag, never from anything about a name.
    network: bool = False
    #: Host that reads documents: the default provider's when that is a service, otherwise the
    #: first configured service endpoint. The host, never the full URL.
    endpoint_host: str = ""
    #: Every configured service endpoint host, when more than one is selectable.
    service_endpoint_hosts: list[str] = Field(default_factory=list)
    #: Every recogniser a request may select here, in the order ``ocr_provider`` accepts them.
    configured_providers: list[str] = Field(default_factory=list)
    #: ``external`` | ``on_premises`` — where the DEPLOYMENT declares ``endpoint_host`` sits
    #: relative to its own trust boundary. Reported on every deployment, including ones with
    #: no remote provider, so that "external" is a value in a field rather than an absence.
    #:
    #: This service does not and cannot verify it: an internal-looking hostname and a vendor
    #: one are the same socket from here. It changes how the posture reads — under
    #: ``on_premises`` it is configuration rather than a warning — but never what the process
    #: does: ``network`` above and ``egress.preclassification_ocr`` stay true either way,
    #: because the bytes leave this process either way.
    trust_boundary: str = "external"
    #: True when an operator set ``DCE_INGEST_OCR_SERVICE_TRUST_BOUNDARY``; false when the
    #: value above is the code default. "We chose external" and "nobody said" are different
    #: claims and an auditor reading ``external`` should be able to tell them apart.
    trust_boundary_declared: bool = False
    #: The attribution sentence: who says so, and that this service did not check. Empty when
    #: no service provider is configured and the question does not arise. **Consoles must render
    #: this wherever they render a reassuring boundary** — a page that goes quiet because a
    #: flag was set is worse than one that shouts, and this is what keeps it a claim with an
    #: owner instead.
    trust_boundary_attribution: str = ""
    #: Set when the provider is switched on but cannot work (no endpoint, extra not installed).
    problem: str = ""
    summary: str = ""
    #: Whether an in-process engine is configured. Reported separately from ``provider``
    #: because a console has to distinguish "local OCR is off here, an image returns
    #: needs_ocr" from "we have not been told" — and silence read as "no" is exactly the
    #: wrong default on a page an auditor uses.
    local_ocr_enabled: bool = False
    #: The local engine that is or would be used. Reported even when disabled.
    local_ocr_engine: str = ""
    #: Every recogniser this build supports, switched on or not. See
    #: :class:`OcrProviderStatus`.
    providers: list[OcrProviderStatus] = Field(default_factory=list)
    #: ``trust`` | ``verify`` | ``always_ocr`` — how far a PDF's own text layer is believed
    #: here. Reported because it decides, before any provider is chosen, whether a document
    #: is recognised at all: a deployment on ``always_ocr`` bills a recognition for every
    #: file, and one on ``trust`` will read a scanner's watermark as a document's text.
    text_layer_policy: str = ""
    #: What that policy means for a document, in one sentence a console can print.
    text_layer_attribution: str = ""


class TierStatus(BaseModel):
    """One extraction tier as configured on this deployment."""

    tier: str
    enabled: bool
    #: Whether running it costs money. T1 and T5 do not; T2/T3/T4 do.
    cost_bearing: bool = False
    #: Populated when the tier is switched on but cannot work (no endpoint, no key, no module).
    problem: str = ""
    summary: str = ""


class SecondAvenueStatus(BaseModel):
    """The second classification avenue, and — the load-bearing part — its COVERAGE.

    Reported on every ``/readyz`` whether or not an avenue exists, because the failure this
    block prevents is an operator discovering *by using the endpoint* that a second avenue
    can only answer for a fraction of the registry. ``doctypes_covered`` over
    ``doctypes_total`` is that number, and it is published even when it is zero — especially
    when it is zero, which is the current state.
    """

    #: True only when a second avenue is loaded and can classify. Currently always false.
    available: bool = False
    #: Method id, when one is loaded. Empty otherwise — never a placeholder name.
    method: str = ""
    #: Templates in the loaded index. Zero when there is no avenue.
    templates: int = 0
    #: How many registry doctypes the avenue could answer for, and out of how many. An
    #: avenue covering 12 of 182 doctypes is a legitimate thing to ship; discovering it in
    #: production is not.
    doctypes_covered: int = 0
    doctypes_total: int = 0
    #: ``doctypes_covered / doctypes_total``, precomputed so nobody has to.
    coverage: float = 0.0
    #: Method ids that could be configured. **Empty**: no method cleared the precision bar.
    installable: list[str] = Field(default_factory=list)
    #: Method ids that were built, measured against the real corpus, and retired. Published
    #: so the readiness page carries the history and not just the gap.
    retired: list[str] = Field(default_factory=list)
    #: Set when an avenue was asked for and cannot be supplied.
    problem: str = ""
    summary: str = ""


class AvenueResult(BaseModel):
    """One avenue's decision trail, or the reason it did not produce one.

    ``classification`` is exactly what that avenue's own endpoint would have returned — same
    model, same fields, no summarising — so a caller comparing the two is comparing the real
    answers and not a reduction of them.
    """

    #: ``lexical`` or the second avenue's method id.
    avenue: str
    #: True when the avenue ran at all. False means ``classification`` is ``None`` and
    #: ``detail`` says why; it does **not** mean the avenue abstained.
    ran: bool = False
    classification: Classification | None = None
    ms: int = 0
    #: Why the avenue did not run, when it did not.
    detail: str = ""


class ComparisonResponse(BaseModel):
    """Both decision trails, side by side, with **nothing adjudicated**.

    This is a reporting surface. It has no fused answer, no preferred avenue and no
    tie-break, and it deliberately does not expose one — the question "which avenue should
    win, and when" has to be settled on data, and this endpoint is how that data gets
    produced. See :mod:`dce.visual.compare` for why fusion is held back.

    Read ``verdict`` as an observation about the two trails, never as a confidence: two
    avenues can agree and both be wrong. The corpus contains a pair (``mx_cif`` /
    ``mx_rfc_csf``) that renders as the *same document* under two registry doctype names,
    where agreement is guaranteed and correctness is not available to any classifier at all.
    """

    doc_id: str = ""
    #: ``agree`` | ``disagree`` | ``one_abstained`` | ``both_abstained`` | ``single_avenue``.
    verdict: str
    #: True only when both avenues answered and named the same doctype.
    same_doctype: bool = False
    #: How many of the two produced a non-abstaining answer: 0, 1 or 2.
    answered: int = 0
    #: What happened, and — said out loud — what was not concluded from it.
    detail: str = ""
    lexical: AvenueResult
    second: AvenueResult
    #: The second avenue's availability at the time of the call, so a run of this endpoint is
    #: self-describing when it is read back months later out of a log.
    second_avenue: SecondAvenueStatus = Field(default_factory=lambda: SecondAvenueStatus())
    source: DocumentSource | None = None
    ms: int = 0


class ReadinessResponse(BaseModel):
    ready: bool
    service: str
    version: str
    registry: RegistryStatus
    bert: dict[str, Any]
    egress: EgressStatus
    #: How images and scanned PDFs are read here, and whether reading one is a network call.
    ocr: OcrStatus = Field(default_factory=OcrStatus)
    #: The second classification avenue and its registry coverage. Unavailable on every
    #: deployment today — see :mod:`dce.visual`.
    second_avenue: SecondAvenueStatus = Field(default_factory=SecondAvenueStatus)
    #: The extraction tiers and their posture. On a default deployment every paid tier reads
    #: ``enabled: false``, which is the answer a control reviewer is actually asking for.
    tiers: list[TierStatus] = Field(default_factory=list)
    components: dict[str, ComponentState] = Field(default_factory=dict)
    degraded: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _ingest_to_layout(req: DocumentRequest) -> LayoutView:
    """Parse ``content_base64`` in-process. The one call site of :mod:`dce.ingest`.

    Raises:
        HTTPException: ``400`` when no bytes were sent or the upload could not be parsed;
            ``422`` with a structured ``needs_ocr`` body when the file carries no text at
            all; ``415``/``413``/``408``/``503`` for the corresponding ingest errors.
    """
    data = _document_bytes(req)
    if data is None:
        raise HTTPException(
            status_code=400, detail="ingest was requested but content_base64 is empty"
        )
    options = req.ingest or IngestOptions()
    try:
        outcome = ingest_bytes(
            data,
            doc_id=req.doc_id,
            filename=options.filename,
            local_ocr=options.local_ocr,
            ocr_service=options.ocr_service,
            ocr_provider=options.ocr_provider,
            read_channel=options.read_channel,
        )
    except IngestError as exc:
        raise HTTPException(
            status_code=exc.http_status, detail={"error": exc.code, "detail": str(exc)}
        ) from exc
    if outcome.view is None:
        # needs_ocr. Not a classification and not a failure: there was nothing to read. 422
        # rather than 200-with-an-abstention, because an abstention is a decision the cascade
        # made about text it saw, and reporting one here would be a lie about which stage
        # gave up.
        raise HTTPException(status_code=422, detail=outcome.as_detail())
    return outcome.view


def _to_layout(req: DocumentRequest) -> LayoutView:
    """Adapt a request payload into a :class:`LayoutView`.

    Raises:
        HTTPException: ``400`` when the request carried no document at all.
    """
    if req.ingest is not None:
        # Asking for ingestion is explicit, so it wins: a caller that sends both a layout and
        # a file has told us which one they mean by setting this field.
        return _ingest_to_layout(req)
    if req.layout is not None:
        view = req.layout
    elif req.azure_read_result is not None:
        # Declared, not sniffed: the caller said this is Read v3.2, so it is mapped as Read
        # even if it happened to be shaped like something else.
        view = adapters.from_azure_read(req.azure_read_result)
    elif req.azure_analyze_result is not None:
        # Either Azure product. `from_azure` picks the mapper from the payload's own shape and
        # records which one in view.raw["provider"], which every response then reports.
        view = adapters.from_azure(req.azure_analyze_result)
    elif req.des_ocr is not None:
        view = adapters.from_des_ocr(req.des_ocr)
    elif req.text is not None:
        view = adapters.from_plain_text(req.text)
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                "supply one of: layout, azure_analyze_result, azure_read_result, des_ocr, "
                "text, or content_base64 together with ingest"
            ),
        )
    if req.doc_id:
        view.doc_id = req.doc_id
    return view


def _local_engine_installed(engine: str) -> bool:
    """Whether the named local engine's package is importable.

    ``find_spec`` rather than constructing the engine: a readiness probe runs every few
    seconds and RapidOCR's constructor loads ONNX models.
    """
    module = {"rapidocr": "rapidocr_onnxruntime", "tesseract": "pytesseract"}.get(engine)
    return module is not None and importlib.util.find_spec(module) is not None


def _text_layer_attribution(resolved: IngestSettings) -> str:
    """One sentence saying what this deployment's text-layer policy does to a document.

    Printed on ``/readyz`` for the reason the trust boundary is: the value alone is a word,
    and an operator needs to know what the word costs them.
    """
    policy = resolved.text_layer()
    if policy == TEXT_LAYER_ALWAYS_OCR:
        return (
            "no PDF's text layer is read here (DCE_INGEST_TEXT_LAYER_POLICY=always_ocr): every "
            "document is handed to the recogniser whatever characters it already carries, "
            "which costs a recognition on every file including born-digital ones"
        )
    if policy == TEXT_LAYER_TRUST:
        return (
            "a page's own characters are taken at face value wherever it has enough of them "
            "(DCE_INGEST_TEXT_LAYER_POLICY=trust). A page is not escalated for being mostly one "
            "image or for carrying a previous recogniser's garbage, so a scanned page with a "
            "bad OCR layer is classified on that layer"
        )
    return (
        "each page is judged on its own characters, and a page that is mostly one image or "
        "carries a previous recogniser's garbage is recognised rather than believed "
        "(DCE_INGEST_TEXT_LAYER_POLICY=verify). Where no recogniser is available such pages "
        "are reported as unread rather than dropped"
    )


def _ocr_providers(resolved: IngestSettings) -> list[OcrProviderStatus]:
    """Every recogniser this build supports, each with whether it is usable *here* and why not.

    Built from :data:`dce.ingest.ocr.PROVIDERS` rather than from a list written out here, so a
    provider added to the registry cannot be missing from the report — the failure mode that
    matters is a provider that is configurable but never listed, and iterating the registry
    makes that unrepresentable.

    A deployment may configure several — an in-process engine and one or both OCR services —
    and every one of them is ``available``, so a caller can select between them with
    ``ingest.ocr_provider``. Exactly one carries ``default``: the one that runs when a request
    names none.
    """
    configured = resolved.configured_providers()
    default = resolved.default_provider()
    out: list[OcrProviderStatus] = []
    for name, info in sorted(PROVIDERS.items()):
        endpoint = resolved.provider_endpoint_host(name) if info.service else ""
        if name in configured:
            reason = (
                resolved.provider_problem(name)
                if info.service
                else (
                    ""
                    if _local_engine_installed(name)
                    else f"local_ocr_engine={name!r} is switched on but not installed"
                )
            )
        elif info.service:
            others = ", ".join(resolved.service_providers())
            reason = (
                (
                    f"no endpoint is configured for {name} here, so no document is sent to it; "
                    f"this deployment's OCR service reads through {others}"
                )
                if resolved.ocr_service_enabled and others
                else (
                    "no OCR service is configured here (DCE_INGEST_OCR_SERVICE_ENABLED), so no "
                    "document is sent to this provider to be read"
                )
            )
        else:
            reason = (
                f"this deployment's in-process engine is {resolved.local_ocr_engine!r}"
                if resolved.local_ocr_enabled
                else "local OCR is switched off here (DCE_INGEST_LOCAL_OCR_ENABLED)"
            )
        out.append(
            OcrProviderStatus(
                name=name,
                available=name in configured and not reason,
                default=name == default,
                network=info.service,
                structure=info.structure,
                endpoint=endpoint,
                reason=reason,
                summary=info.summary,
            )
        )
    return out


def _egress_note(ocr: OcrStatus) -> str:
    """The one-line headline on ``/readyz``, given this deployment's OCR posture.

    Three sentences, one per posture, and the middle one is the point of the trust-boundary
    declaration. Under ``on_premises`` this is **configuration, not a warning**: the deployment
    has said the endpoint is its own, so the sentence describes how images are read and where,
    and stops there. Under ``external`` — declared or, more importantly, merely defaulted — it
    stays cautious, because a deployment that has declared nothing must not get the reassuring
    reading.

    The invariant claim — *classification* opens no socket — is true in all three and is never
    quietly widened into "nothing leaves", because ingestion runs before the classification
    scope is ever entered.

    Args:
        ocr: The already-built OCR block, so the note and the block cannot disagree.

    Returns:
        One sentence for an operator who reads only this field.
    """
    if not ocr.network:
        return (
            "classification is in-process only: no HTTP, no vendor SDK, no embedding API "
            "before the doctype is known"
        )
    where = ocr.endpoint_host or "the configured OCR endpoint"
    # Name the provider only when it is the one that actually runs unpinned; on a deployment
    # whose default is in-process the service is a selectable alternative, not the reader.
    reader = next(
        (p.name for p in ocr.providers if p.default and p.network), "an OCR service"
    )
    if ocr.trust_boundary == TRUST_BOUNDARY_ON_PREMISES:
        return (
            "classification itself is in-process only. Images and scanned PDFs are read by "
            f"{reader} at {where}, before their doctype is known; this deployment declares "
            "that host is on its own network — the operator's declaration, recorded here and "
            "not verified — see the `ocr` block"
        )
    return (
        "classification itself is in-process only, BUT this deployment sends images and "
        f"scanned PDFs to {where} to be read, before their doctype is known — see the `ocr` "
        "block"
    )


def _ocr_status(ingest_settings: IngestSettings | None = None) -> OcrStatus:
    """How this deployment reads an image, for ``/readyz``.

    The ``network`` flag comes from :func:`dce.ingest.ocr.provider_info`, i.e. from the
    provider registry's ``service`` flag, not from string-matching a vendor name here. A
    provider added later without that flag set is not a provider at all, so it cannot report
    itself as in-process.
    """
    resolved = ingest_settings or get_ingest_settings()
    configured = resolved.configured_providers()
    # Deduplicated, order preserved: two providers commonly sit behind one host, and listing
    # it twice would read as two destinations.
    service_hosts = list(
        dict.fromkeys(
            host
            for host in (
                resolved.provider_endpoint_host(p) for p in resolved.service_providers()
            )
            if host
        )
    )
    common = {
        "local_ocr_enabled": resolved.local_ocr_enabled,
        "local_ocr_engine": resolved.local_ocr_engine,
        "providers": _ocr_providers(resolved),
        "configured_providers": list(configured),
        "service_endpoint_hosts": service_hosts,
        # Reported on every deployment, not only the ones with a service provider: a field that
        # appears only once it is interesting can only be found by somebody who already knew
        # to look. The attribution below is empty when the question does not arise.
        "trust_boundary": resolved.trust_boundary(),
        "trust_boundary_declared": resolved.trust_boundary_declared(),
        "trust_boundary_attribution": resolved.trust_boundary_attribution(),
        "text_layer_policy": resolved.text_layer(),
        "text_layer_attribution": _text_layer_attribution(resolved),
    }
    default = resolved.default_provider()
    info = provider_info(default)
    # More than one recogniser can be configured at once, so an operator reading the headline
    # needs to know that the default is a default and not the only option.
    choice = (
        ""
        if len(configured) < 2
        else (
            f". A request may select any of {', '.join(configured)} with ingest.ocr_provider; "
            f"{default} runs when it names none"
        )
    )
    if info is not None and info.service:
        host = resolved.provider_endpoint_host(default)
        where = host or "(no endpoint configured)"
        # The same operation, described two ways, because the operator has told us two
        # different things about where those bytes land. The on-premises wording is
        # configuration — this is how this deployment reads an image, and where — while the
        # external wording stays a disclosure, because nobody has said the far end is theirs.
        summary = (
            (
                f"images and scanned PDFs are read by {default} at {where}, before their "
                "doctype is known. This deployment declares that host is on its own network, "
                "so documents stay within the operator's infrastructure — an operator "
                "declaration recorded here, not a fact this service verified"
            )
            if resolved.trust_boundary() == TRUST_BOUNDARY_ON_PREMISES
            else (
                "THIS DEPLOYMENT TRANSMITS UNCLASSIFIED DOCUMENTS to "
                f"{where} — images and scanned PDFs are sent to {default} to be read, before "
                "their doctype is known"
            )
        )
        return OcrStatus(
            **common,
            provider=default,
            enabled=True,
            network=True,
            endpoint_host=host,
            problem=resolved.provider_problem(default),
            summary=summary + choice,
        )
    if info is not None:
        installed = _local_engine_installed(default)
        return OcrStatus(
            **common,
            provider=default,
            enabled=True,
            # True when the deployment also configured a service provider, even though the
            # default is in-process: a request may select it, so "can reading an image here
            # involve a call out" is yes.
            network=bool(service_hosts),
            endpoint_host=service_hosts[0] if service_hosts else "",
            problem=(
                ""
                if installed
                else f"local_ocr_engine={default!r} is switched on but not installed"
            ),
            summary=(
                (
                    f"images are recognised in this process by {default} unless a request "
                    f"selects otherwise; {', '.join(service_hosts)} is also configured and "
                    "reads documents a request sends it"
                    if service_hosts
                    else (
                        f"images are recognised in this process by {default}; no document is "
                        "sent anywhere to be read"
                    )
                )
                + choice
            ),
        )
    return OcrStatus(
        **common,
        provider="none",
        enabled=False,
        network=False,
        summary=(
            "images and scanned PDFs return needs_ocr; no recogniser is configured, so no "
            "document is sent anywhere and none is guessed at"
        ),
    )


#: How each provider id reads in one sentence. Keyed by ``LayoutView.raw["provider"]``.
_SOURCE_NOTES: dict[str, str] = {
    adapters.PROVIDER_AZURE_LAYOUT: (
        "caller-supplied Azure Document Intelligence prebuilt-layout payload: paragraph "
        "roles, tables and selection marks are available, and this service opened no socket"
    ),
    adapters.PROVIDER_AZURE_READ: (
        "caller-supplied Azure AI Vision Read v3.2 payload: lines and words only. Read "
        "predicts no paragraph roles, so every block is body and no title-gated decisive "
        "anchor can fire — prefer prebuilt-layout where you have it. This service opened no "
        "socket"
    ),
    adapters.PROVIDER_DES_OCR: "caller-supplied DES OCR payload; this service opened no socket",
    adapters.PROVIDER_PLAIN_TEXT: (
        "plain text: no zones, so title weighting and title-gated anchors do not apply"
    ),
    "caller-layout": "a LayoutView the caller had already adapted; this service opened no socket",
    "dce.ingest": "parsed in this process from the uploaded bytes; no network call",
}


def _source_of(view: LayoutView) -> DocumentSource:
    """Describe where ``view`` came from, from the provenance the adapters recorded.

    Reads ``LayoutView.raw``, which every adapter and the ingestion pipeline populate, so this
    stays a lookup rather than a second place that has to be told about a new provider.
    """
    raw = view.raw if isinstance(view.raw, dict) else {}
    provider = str(raw.get("provider") or "caller-layout")
    via_service = bool(raw.get("ocr_via_service"))
    host = str(raw.get("ocr_endpoint_host") or "")
    if via_service:
        engine = str(raw.get("ocr_engine") or "an OCR service")
        where = host or "the configured endpoint"
        on_premises = (
            get_ingest_settings().trust_boundary() == TRUST_BOUNDARY_ON_PREMISES
        )
        # `provider` stays the ADAPTER that mapped the payload, so path (B) reports the same
        # value path (A) would have for the same provider; `remote` is what distinguishes
        # who dialled. The note follows the declared boundary: configuration where the
        # operator has said the endpoint is theirs, a disclosure where nobody has said so.
        note = (
            (
                f"recognised by {engine} at {where} and mapped by the {provider} adapter — "
                "read by an OCR service this deployment declares is on its own network, "
                "before the doctype was known"
            )
            if on_premises
            else (
                f"recognised by {engine} at {where} and mapped by the {provider} adapter — "
                "THIS DEPLOYMENT TRANSMITTED AN UNCLASSIFIED DOCUMENT outside its own "
                "boundary to obtain its text"
            )
        )
        return DocumentSource(
            provider=provider, remote=True, endpoint_host=host, note=note
        )
    note = _SOURCE_NOTES.get(provider, "")
    if not note and provider == "dce.ingest":
        note = _SOURCE_NOTES["dce.ingest"]
    if str(raw.get("text_source") or "") == "local_ocr":
        note = (
            f"recognised in this process by {raw.get('ocr_engine') or 'a local engine'}: "
            "no network call, and OCR text scores lower than a publisher's own text"
        )
    return DocumentSource(provider=provider, remote=False, note=note)


def _document_bytes(req: DocumentRequest) -> bytes | None:
    """Decode the original file, when the caller sent one.

    Only T2 and T3 ever receive this, and only after a doctype has been accepted: Azure
    analyses the document, not our reading of it. Nothing on the classification path reads it.

    Raises:
        HTTPException: ``400`` when ``content_base64`` is not valid base64. Treating corrupt
            input as "no bytes" would present to the caller as a mysteriously skipped tier.
    """
    if not req.content_base64:
        return None
    try:
        return base64.b64decode(req.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="content_base64 is not valid base64") from exc


def _summarize(spec: DocTypeSpec) -> DocTypeSummary:
    return DocTypeSummary(
        doctype_id=spec.doctype_id,
        label=spec.label,
        country=spec.country,
        category=spec.category,
        issuing_authority=spec.issuing_authority,
        applies_to=spec.applies_to,
        officially_valid=spec.officially_valid,
        anchors=len(spec.anchors),
        fields=[f.name for f in spec.fields],
    )


def _schema_version_for(spec: DocTypeSpec) -> str:
    """Derive a stable schema version from a spec's field surface.

    Content-addressed rather than a counter: the version changes exactly when the fields
    change, and two replicas of the service always agree on it without coordination.
    """
    payload = "|".join(
        f"{f.name}:{f.type}:{f.pattern or ''}:{f.validator or ''}:{','.join(f.locators)}"
        for f in spec.fields
    )
    digest = hashlib.sha256(f"{spec.doctype_id}::{payload}".encode()).hexdigest()[:12]
    return f"reg-{digest}"


_NON_WORD = re.compile(r"[^a-z0-9]+")
_DATE_HINT = re.compile(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b")
_NUMBER_HINT = re.compile(r"^[\s$€₹£]*-?[\d,]+(?:\.\d+)?\s*%?$")


def _snake(text: str) -> str:
    """Field-name-safe snake_case, truncated to something a human will still read."""
    return _NON_WORD.sub("_", text.strip().lower()).strip("_")[:60]


def _guess_type(values: list[str]) -> str:
    """Infer a field type from observed values; ``string`` unless every sample agrees."""
    seen = [v.strip() for v in values if v and v.strip()]
    if not seen:
        return "string"
    if all(_DATE_HINT.search(v) for v in seen):
        return "date"
    if all(_NUMBER_HINT.match(v) for v in seen):
        return "number"
    return "string"


def _induce_fields(views: list[LayoutView], min_support: float) -> list[FieldSpec]:
    """Draft field specs from the named slots the samples agree on.

    Two sources, both of which are *named* by the document itself rather than guessed:
    provider key-value pairs, and table column headers. A candidate has to appear in at least
    ``min_support`` of the samples, which is what stops a one-off stamp or handwritten note
    from becoming a field. The draft is a starting point for a human — it never becomes active
    on its own.

    Args:
        views: Adapted sample documents.
        min_support: Fraction of samples a candidate must appear in.

    Returns:
        Field specs sorted by support (most consistently present first).
    """
    support: dict[str, int] = {}
    labels: dict[str, str] = {}
    values: dict[str, list[str]] = {}
    locators: dict[str, list[str]] = {}

    for view in views:
        seen: set[str] = set()
        for kv in view.key_values:
            name = _snake(kv.key)
            if not name:
                continue
            seen.add(name)
            labels.setdefault(name, kv.key.strip().rstrip(":"))
            locators.setdefault(name, ["kv", "label"])
            values.setdefault(name, []).append(kv.value)
        for table in view.tables:
            for cell in table.cells:
                name = _snake(cell.text) if cell.is_header else ""
                if not name:
                    continue
                seen.add(name)
                labels.setdefault(name, cell.text.strip())
                locators.setdefault(name, ["table", "kv"])
        for name in seen:
            support[name] = support.get(name, 0) + 1

    threshold = max(1, math.ceil(min_support * len(views)))
    drafted = [
        FieldSpec(
            name=name,
            type=_guess_type(values.get(name, [])),
            labels={"en": [labels.get(name, name)]},
            locators=locators.get(name, ["kv", "label"]),
            notes=f"induced from {count}/{len(views)} sample(s); review before activating",
        )
        for name, count in support.items()
        if count >= threshold
    ]
    drafted.sort(key=lambda f: (-support[f.name], f.name))
    return drafted


def _schema_from_peer(doctype_id: str) -> Any | None:
    """Ask a schema module for the active schema, when one exists."""
    fn = _load(_SCHEMA_MODULES, ("get_active_schema", "active_schema", "get_schema"), "get_schema")
    if fn is None:
        return None
    try:
        return _call_supported(fn, doctype_id)
    except Exception:
        logger.warning("schema module raised for %s", doctype_id, exc_info=True)
        return None


def _fields_of(obj: Any) -> list[FieldSpec]:
    """Pull ``FieldSpec``s out of whatever a schema module returned."""
    raw = obj.get("fields") if isinstance(obj, dict) else getattr(obj, "fields", None)
    out: list[FieldSpec] = []
    for item in raw or []:
        if isinstance(item, FieldSpec):
            out.append(item)
        elif isinstance(item, dict):
            try:
                out.append(FieldSpec(**item))
            except Exception:  # noqa: BLE001 - skip a field we cannot understand
                continue
    return out


def _attr(obj: Any, name: str, default: Any = "") -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


# ---------------------------------------------------------------------------
# The tier cascade (T1 -> T2 -> T3 -> T4 -> T5)
# ---------------------------------------------------------------------------
def _missing_field_names(result: ExtractionResult, spec: DocTypeSpec) -> list[str]:
    """Fields the schema expects that still have no value, in schema order."""
    filled = {f.name for f in result.fields if f.value}
    ordered = [f.name for f in spec.fields]
    seen = set(ordered)
    ordered += [f.name for f in result.fields if f.name not in seen]
    return [name for name in ordered if name not in filled]


def _merge_tier_fields(
    result: ExtractionResult,
    produced: list[ExtractedField],
    missing: list[str],
    tier: str,
    spec: DocTypeSpec,
) -> list[str]:
    """Fold a tier's output into the result, filling **only** what was still missing.

    This is where "each tier only fills fields still missing" is actually enforced. It is not
    left to the tiers: a remote model asked for three fields will happily volunteer a fourth,
    and letting it replace a checksum-verified local value with a fluent guess would invert the
    whole verification ladder. Values for fields that were not in ``missing`` are dropped, and
    the first tier to fill a field wins.

    Provenance survives the fold: the field's ``locator`` is prefixed with the tier that
    produced it, so ``t4_llm:regex`` and ``regex`` are distinguishable in a review UI — a
    human needs to know which values came from a model.

    Args:
        result: Extraction accumulated so far. **Mutated in place.**
        produced: What the tier returned.
        missing: Field names that were missing when the tier was asked.
        tier: Tier id, used as the locator prefix.
        spec: The doctype spec, for attribute keys and PII flags the tier did not set.

    Returns:
        Names of the fields this tier actually filled.
    """
    allowed = set(missing)
    field_specs = {f.name: f for f in spec.fields}
    index = {f.name: i for i, f in enumerate(result.fields)}
    filled: list[str] = []

    for candidate in produced:
        name = candidate.name
        if name not in allowed or not candidate.value:
            continue
        allowed.discard(name)
        field = candidate.model_copy(deep=True)
        field.locator = f"{tier}:{field.locator}" if field.locator else tier
        declared = field_specs.get(name)
        if declared is not None:
            field.attribute_key = field.attribute_key or declared.attribute_key
            field.pii = field.pii or declared.pii
        if name in index:
            result.fields[index[name]] = field
        else:
            result.fields.append(field)
            index[name] = len(result.fields) - 1
        filled.append(name)
    return filled


def _refresh_review_state(result: ExtractionResult, spec: DocTypeSpec) -> None:
    """Recompute ``missing_required`` and ``needs_review`` after a tier filled something.

    Only called when a paid tier actually contributed. A document that took the local path
    alone must come back byte-identical to what it returned before the tiers existed — a
    recomputation that ran unconditionally would quietly rewrite T1's own judgement.

    ``needs_review`` is recomputed rather than left latched, because a flag T1 raised for a
    field T2 then resolved would send a finished document to a human. It survives on the two
    grounds that a later tier cannot clear: a required field still empty, and a value some
    validator rejected.
    """
    filled = {f.name for f in result.fields if f.value}
    result.missing_required = [
        f.name for f in spec.fields if f.required and f.name not in filled
    ]
    result.needs_review = bool(result.missing_required) or any(
        f.validator_error for f in result.fields
    )


def run_tier_cascade(
    request: Request,
    *,
    view: LayoutView,
    spec: DocTypeSpec,
    classification: Classification,
    result: ExtractionResult,
    settings: Settings,
    content: bytes | None = None,
) -> list[TierRun]:
    """Escalate through the enabled paid tiers, filling only what is still missing.

    Args:
        request: Used for the per-app tier port cache — and nothing else.
        view: The layout view.
        spec: The **accepted** doctype spec.
        classification: The accepted classification.
        result: T1's result. Mutated in place as tiers contribute.
        settings: Which tiers are on, and how to reach them.
        content: The original document bytes, when the caller supplied them. T2/T3 need the
            file itself; without it they are skipped with a reason rather than guessing.

    Returns:
        One :class:`TierRun` per tier that executed or was deliberately skipped, in escalation
        order. A tier whose flag is off produces nothing at all.

    Raises:
        RuntimeError: If called for a document that was not classified. This is the invariant
            restated as code: T2/T3/T4 leave the process, and leaving the process with a
            document whose type nobody knows is the exact disclosure this service exists to
            prevent. The tier modules assert it too (:class:`UnclassifiedDocumentError`,
            :func:`dce.egress.post_classification_scope`); this is the outer of the two locks,
            and it is here because a call site that could reach them wrongly is the bug.
    """
    if classification.abstained or classification.doctype_id == UNKNOWN:
        raise RuntimeError(
            "the extraction tier cascade was reached with an unclassified document "
            f"(doctype_id={classification.doctype_id!r}, abstained={classification.abstained}). "
            "T2/T3/T4 leave this process; nothing may leave until a doctype is accepted."
        )

    problems = settings.tier_problems()
    runs: list[TierRun] = []

    for tier in PAID_TIERS:
        if not getattr(settings, tier.flag, False):
            continue                        # off by default; silence is the whole point
        missing = _missing_field_names(result, spec)
        if not missing:
            break                           # nothing left to buy

        problem = problems.get(tier.tier, "")
        if problem:
            runs.append(TierRun(tier=tier.tier, status="misconfigured", detail=problem))
            observability.observe_extraction_tier(
                tier.tier, seconds=0.0, outcome="misconfigured", provider=tier.provider
            )
            logger.warning("%s is enabled but unusable: %s", tier.tier, problem)
            continue

        if tier.needs_bytes and not content:
            detail = (
                "no document bytes in the request; this tier analyses the file itself. "
                "Send content_base64 to use it."
            )
            runs.append(TierRun(tier=tier.tier, status="skipped", detail=detail))
            observability.observe_extraction_tier(
                tier.tier, seconds=0.0, outcome="skipped", provider=tier.provider
            )
            continue

        port = tier_port(request, tier)
        if port is None:
            detail = f"module not importable (looked in: {', '.join(tier.modules)})"
            runs.append(TierRun(tier=tier.tier, status="unavailable", detail=detail))
            observability.observe_extraction_tier(
                tier.tier, seconds=0.0, outcome="unavailable", provider=tier.provider
            )
            logger.warning("%s is enabled but %s", tier.tier, detail)
            continue

        if not port.applies_to(spec.doctype_id):
            # Not a failure: Azure ships a specialist for a handful of doctypes and T1 is the
            # right answer for the rest. Detected before the call, so it costs nothing and is
            # not counted as spend.
            runs.append(
                TierRun(
                    tier=tier.tier,
                    status="skipped",
                    detail=f"no {tier.provider} model covers {spec.doctype_id}; staying on T1",
                )
            )
            observability.observe_extraction_tier(
                tier.tier, seconds=0.0, outcome="skipped", provider=tier.provider
            )
            continue

        started = time.perf_counter()
        try:
            produced = port.run(
                view=view,
                spec=spec,
                result=result,
                missing=missing,
                settings=settings,
                classification=classification,
                content=content,
            )
        except ImportError:
            # The tiers import their HTTP client inside the call, so a build that never
            # installed one lands here rather than at import. Nothing was dialled, so nothing
            # is counted as spend — and the message says exactly what is missing.
            detail = (
                "no HTTP client is installed. The base image ships none by design; install "
                "httpx deliberately to use this tier."
            )
            logger.warning("%s is enabled but %s", tier.tier, detail)
            runs.append(TierRun(tier=tier.tier, status="unavailable", detail=detail))
            observability.observe_extraction_tier(
                tier.tier, seconds=0.0, outcome="unavailable", provider=tier.provider
            )
            continue
        except Exception as exc:  # a paid tier must not take down the request
            elapsed = time.perf_counter() - started
            logger.warning("%s raised; continuing without it", tier.tier, exc_info=True)
            # Billed anyway: the call was made. A failed paid tier that logged nothing would
            # make spend invisible at exactly the moment it is least expected.
            logs.event(
                logger,
                "tier.failed",
                level=logging.WARNING,
                tier=tier.tier,
                provider=tier.provider,
                error=type(exc).__name__,
                billed=True,
                ms=int(elapsed * 1000),
            )
            runs.append(
                TierRun(
                    tier=tier.tier,
                    status="error",
                    ms=int(elapsed * 1000),
                    # The call was made, so it is on the bill whether or not we could use it.
                    cost_bearing=True,
                    detail=f"{type(exc).__name__}: {exc}"[:200],
                )
            )
            observability.observe_extraction_tier(
                tier.tier,
                seconds=elapsed,
                outcome="error",
                cost_bearing=True,
                provider=tier.provider,
            )
            continue

        elapsed = time.perf_counter() - started
        filled = _merge_tier_fields(result, produced, missing, tier.tier, spec)
        # A COST-BEARING call completed. Field NAMES, never values — a filled field's value is
        # the customer's name or account number. This line is what lets an operator answer
        # "what did this request spend" from logs alone.
        logs.event(
            logger,
            "tier.billed",
            tier=tier.tier,
            provider=tier.provider,
            doctype=spec.doctype_id,
            fields_filled=len(filled),
            fields=",".join(filled) or None,
            ms=int(elapsed * 1000),
        )
        runs.append(
            TierRun(
                tier=tier.tier,
                status="ran",
                fields_filled=len(filled),
                fields=filled,
                ms=int(elapsed * 1000),
                cost_bearing=True,
            )
        )
        observability.observe_extraction_tier(
            tier.tier,
            seconds=elapsed,
            fields_filled=len(filled),
            outcome="ran",
            cost_bearing=True,
            provider=tier.provider,
        )

    if any(run.status == "ran" and run.fields_filled for run in runs):
        _refresh_review_state(result, spec)
    return runs


def _review_reason(classification: Classification, extraction: ExtractionResult | None) -> str:
    """Why this document needs a human — the label ``dce_review_enqueued_total`` is keyed on.

    Document-level, and deliberately using :mod:`dce.review`'s vocabulary: the per-*field*
    reason (below threshold, validator complained) is the queue's to decide, and two competing
    sets of names for the same fact is how a dashboard ends up lying.
    """
    if classification.abstained or classification.doctype_id == UNKNOWN:
        return "classification_abstained"
    if extraction is not None and extraction.missing_required:
        return "missing_required"
    return "unverified_fields"


def enqueue_for_review(
    request: Request,
    *,
    classification: Classification,
    extraction: ExtractionResult | None,
    view: LayoutView,
    settings: Settings,
    spec: DocTypeSpec | None = None,
    reason: str | None = None,
) -> tuple[TierRun, list[str]]:
    """T5: put what a machine could not finish in front of a human, best-effort.

    Best-effort on purpose. The queue is downstream of the answer; the caller has already been
    told ``needs_review`` in the response body, and failing their request because our queue is
    missing would convert "a human should look at this" into "you get nothing".

    The doctype spec is passed through because it carries ``pii`` and the validator name, which
    is how :mod:`dce.review` decides that a field needs **two** independent reviewers. Without
    it every item would quietly become a one-signature item, which is the control failing open.

    Args:
        request: For the per-app queue cache.
        classification: The classification, abstained or not.
        extraction: The extraction, when there was one.
        view: The layout view, for the document id.
        settings: Queue backend and the confidence threshold.
        spec: The accepted doctype spec, when there is one.
        reason: Logged/metric reason; derived from the outcome when omitted.

    Returns:
        ``(tier_run, review_ids)``. The run is reported even when there is no queue installed —
        "this needed a human and there was nowhere to put it" is exactly the kind of thing an
        operator must not have to infer.
    """
    why = reason or _review_reason(classification, extraction)
    observability.observe_review_enqueued(why)
    port = review_port(request)
    if port is None:
        logger.info(
            "document %s needs review (%s) but no review queue is installed", view.doc_id, why
        )
        return (
            TierRun(
                tier=TIER_REVIEW,
                status="unavailable",
                detail=f"needs review ({why}) but no queue is installed "
                f"(looked in: {', '.join(_REVIEW_MODULES)})",
            ),
            [],
        )

    started = time.perf_counter()
    result = extraction if extraction is not None else ExtractionResult(
        doctype_id=classification.doctype_id, needs_review=True
    )
    try:
        item_ids = port.enqueue(
            result,
            doc_id=view.doc_id,
            doctype_id=classification.doctype_id,
            field_specs=spec,
            settings=settings,
        )
    except Exception as exc:  # the queue must never fail the request
        logger.warning("review enqueue failed", exc_info=True)
        return (
            TierRun(tier=TIER_REVIEW, status="error", detail=f"{type(exc).__name__}: {exc}"[:200]),
            [],
        )

    depth = port.depth()
    if depth is not None:
        observability.set_review_queue_depth(depth)
    return (
        TierRun(
            tier=TIER_REVIEW,
            status="queued",
            ms=int((time.perf_counter() - started) * 1000),
            detail=f"{len(item_ids)} item(s) queued ({why})"
            if item_ids
            else f"nothing new to queue ({why}); already awaiting a decision",
        ),
        item_ids,
    )


def _review_item(raw: Any) -> ReviewItem:
    """Coerce a queue item into the response model without losing what it carried."""
    data = _review_dict(raw)
    for key in ("id", "doc_id", "doctype_id", "field_name", "status", "reason", "reviewer",
                "created_at", "decided_at"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            data[key] = str(value)
    try:
        return ReviewItem.model_validate(data)
    except ValidationError:
        logger.warning("review item did not validate; reporting it raw")
        return ReviewItem(id=str(data.get("id", "")), raw=data)


def _second_avenue_status(
    settings: Settings, registry: RegistryPort | None
) -> SecondAvenueStatus:
    """Availability and **registry coverage** of the second classification avenue.

    The coverage denominator comes from the live registry rather than from a constant, so a
    deployment that has loaded 121 doctypes is not told a fraction of 182. Reported even
    though every field is currently zero: the point of the block is that an operator can read
    "there is no second avenue" off the readiness page instead of inferring it.

    Args:
        settings: The process settings; only ``visual_method`` is read.
        registry: The live registry, for the coverage denominator. ``None`` falls back to the
            documented registry size.
    """
    total = len(registry.specs()) if registry is not None else 0
    status = visual.avenue_status(settings, doctypes_total=total or None, registry=registry)
    return SecondAvenueStatus(
        available=status.available,
        method=status.method,
        templates=status.templates,
        doctypes_covered=status.doctypes_covered,
        doctypes_total=status.doctypes_total,
        coverage=round(status.coverage, 4),
        installable=list(status.installable),
        retired=list(status.retired),
        problem=status.problem,
        summary=status.summary,
    )


def _tier_statuses(settings: Settings) -> list[TierStatus]:
    """The five tiers as this deployment has them configured.

    Probes a paid tier's module only when its flag is on — the same rule ``bert_status``
    follows, and for the same reason: a deployment that left a tier off has no business paying
    the import cost of an HTTP client it will never use.

    Args:
        settings: The process settings.

    Returns:
        One status per tier, T1 through T5, in escalation order.
    """
    problems = settings.tier_problems()
    statuses = [
        TierStatus(
            tier=TIER_LOCAL,
            enabled=True,
            summary="local layout-anchored resolver — always on, free, no egress",
        )
    ]
    for tier in PAID_TIERS:
        enabled = bool(getattr(settings, tier.flag, False))
        problem = problems.get(tier.tier, "")
        if enabled and not problem and load_tier_port(tier) is None:
            problem = f"module not importable (looked in: {', '.join(tier.modules)})"
        statuses.append(
            TierStatus(
                tier=tier.tier,
                enabled=enabled,
                cost_bearing=True,
                problem=problem,
                summary=tier.summary,
            )
        )
    queue_missing = load_review_port(settings) is None
    statuses.append(
        TierStatus(
            tier=TIER_REVIEW,
            enabled=True,
            problem="no review queue module installed" if queue_missing else "",
            summary=f"human review queue (backend={settings.review_queue_backend})",
        )
    )
    return statuses


def _waited_seconds(item: dict[str, Any]) -> float | None:
    """Seconds between enqueue and now, when ``created_at`` can be parsed."""
    created = item.get("created_at")
    if not isinstance(created, str) or not created:
        return None
    try:
        stamp = datetime.fromisoformat(created)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - stamp).total_seconds())


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
#: Business routes. The API-key gate is applied here and not to the system routes, so a probe
#: and a Prometheus scraper keep working when a key is configured.
router = APIRouter(prefix="/api/v1", tags=["dce"], dependencies=[Depends(require_api_key)])
system_router = APIRouter(tags=["system"])


# Classification and extraction are CPU-bound and fully in-process, so these handlers are
# declared sync on purpose: FastAPI runs them in the threadpool, where they cannot stall the
# event loop for every other request.
def _report_source(response: Response, view: LayoutView) -> DocumentSource:
    """Stamp ``X-Document-Source`` on a response and return the structured form.

    The header is ``<provider>`` normally and ``<provider>; remote=<host>`` when this service
    called an OCR endpoint to read the document — short enough for a log line, specific enough
    that "which adapter ran" and "was it read here or by a service" are both answerable from
    it.
    """
    source = _source_of(view)
    response.headers["X-Document-Source"] = (
        f"{source.provider}; remote={source.endpoint_host or 'yes'}"
        if source.remote
        else source.provider
    )
    return source


@router.post("/classify", response_model=Classification)
def classify_document(
    response: Response,
    req: DocumentRequest,
    registry: RegistryPort = Depends(get_registry),
    classifier: ClassifierPort = Depends(get_classifier),
    settings: Settings = Depends(get_app_settings),
) -> Classification:
    """Classify a document entirely in-process.

    Returns ``200`` with ``abstained=true`` when the cascade cannot clear its thresholds; that
    is a valid answer, and the document goes to a human rather than to a model.

    The ``X-Document-Source`` response header names the adapter that read the payload — the
    two Azure products are auto-detected from the shape, and they do not classify equally
    well, so which one ran is part of the answer.
    """
    started = time.perf_counter()
    view = _to_layout(req)
    _report_source(response, view)
    result = classifier.classify(view, registry=registry, settings=settings)
    result.ms = result.ms or _ms(started)
    observability.observe_classification(result, time.perf_counter() - started)
    return result


def _segment_response(
    view: LayoutView, settings: Settings, started: float
) -> SegmentsResponse:
    """Split ``view`` into documents and classify each span whole.

    A separate path rather than a flag on ``/classify`` because there is no version header,
    no ``Accept`` negotiation and no media-type variance anywhere in this service — the
    ``/api/v1`` prefix is the only versioning lever, so a second response shape cannot
    coexist with the first on one path without breaking every existing caller.
    """
    from dce.classify.segments import segment_document

    segments, boundaries = segment_document(view, settings=settings)
    pages = [p.page for p in view.pages] or [b.page for b in view.blocks]
    return SegmentsResponse(
        segments=[
            DocumentSegment(
                start_page=s.start_page,
                end_page=s.end_page,
                page_count=s.page_count,
                classification=s.classification,
                needs_review=s.classification.abstained,
            )
            for s in segments
        ],
        segmented=len(segments) > 1,
        boundaries=[
            BoundaryEvidence(page=b.page, signal=b.signal, detail=b.detail)
            for b in boundaries
        ],
        pages=[
            PageRead(
                page=p.page,
                width=p.width,
                height=p.height,
                alnum_chars=p.alnum_chars,
                text_adequate=p.text_adequate,
                image_fraction=p.image_fraction,
            )
            for p in view.pages
        ],
        page_count=max(pages) if pages else 0,
        ms=_ms(started),
    )


@router.post("/classify/segments", response_model=SegmentsResponse)
def classify_segments(
    response: Response,
    req: DocumentRequest,
    settings: Settings = Depends(get_app_settings),
) -> SegmentsResponse:
    """Classify an upload that may hold more than one document.

    A KYC upload is routinely a bundle — a passport, two utility bills and a bank statement in
    one PDF — and classifying that whole answers the wrong question. Sending such a file to
    ``/classify`` returns one doctype, silently omitting everything else in it.

    **Boundaries are proposed from structure, never from per-page classification.** Page-scope
    classification was built and measured: against the real registry on 78 single-document
    corpus files it emitted 791 segments and dropped precision from 100% to 71.3%, because the
    cascade's accept gates were calibrated against whole-document evidence. So each candidate
    span here is classified *whole*, at the scope where those gates hold.

    **A single document costs nothing.** With no boundary evidence the response is one segment
    covering every page, carrying the same classification ``/classify`` would have returned —
    so a caller who does not know whether an upload is a bundle can always send it here.
    """
    started = time.perf_counter()
    view = _to_layout(req)
    source = _report_source(response, view)
    out = _segment_response(view, settings, started)
    out.source = source
    return out


@router.post("/classify/compare", response_model=ComparisonResponse)
def classify_compare(
    response: Response,
    req: DocumentRequest,
    registry: RegistryPort = Depends(get_registry),
    classifier: ClassifierPort = Depends(get_classifier),
    settings: Settings = Depends(get_app_settings),
) -> ComparisonResponse:
    """Run every available classification avenue over one document and report how they relate.

    **This endpoint adjudicates nothing.** It returns both decision trails in full plus a
    ``verdict`` describing their relationship — agree, disagree, one abstained, both
    abstained, or only one avenue exists. It does not fuse, rank, tie-break or promote a
    confidence. Fusing two channels is where this codebase has produced its worst defects,
    and a fusion rule has to be chosen on data; this endpoint is how that data gets produced,
    which is why it is worth having even while there is only one avenue to report on.

    **Today there is only one avenue.** Four visual methods were built and measured against
    the 158-document corpus and none reached the 95% precision-when-answered the bar demands
    — the best end-to-end result was 0.080, against a lexical cascade at 0.983 — so none
    shipped. ``verdict`` is therefore ``single_avenue`` on every deployment, ``second``
    carries ``ran: false`` with the reason, and ``second_avenue`` reports availability and
    registry coverage. That is deliberately *not* an abstention: an avenue that does not
    exist did not decline to answer, and collapsing the two would let an empty registry read
    as a considered refusal.

    A caller who wants the plain answer should call ``/classify``; the ``lexical`` block here
    is what that route returns for the same payload, unmodified.
    """
    started = time.perf_counter()
    view = _to_layout(req)
    source = _report_source(response, view)

    lex_started = time.perf_counter()
    lexical = classifier.classify(view, registry=registry, settings=settings)
    lexical.ms = lexical.ms or _ms(lex_started)
    # Observed once, exactly as /classify observes it. A second avenue, when one exists, must
    # be observed under its own label rather than adding to this counter: the metric answers
    # "how often did the cascade abstain", and two writers would make it answer nothing.
    observability.observe_classification(lexical, time.perf_counter() - lex_started)

    avenue, problem = visual.resolve_avenue(settings)
    second_result: Classification | None = None
    second_ms = 0
    if avenue is not None:  # pragma: no cover - dce.visual.AVENUES is empty
        second_started = time.perf_counter()
        second_result = avenue.classify(view, registry=registry, settings=settings)
        second_ms = _ms(second_started)
    method_id = str(getattr(avenue, "method_id", "")) if avenue is not None else ""

    verdict = compare_classifications(
        lexical, second_result, second_method=method_id, second_problem=problem
    )
    return ComparisonResponse(
        doc_id=req.doc_id,
        verdict=verdict.verdict,
        same_doctype=verdict.same_doctype,
        answered=verdict.answered,
        detail=verdict.detail,
        lexical=AvenueResult(avenue="lexical", ran=True, classification=lexical, ms=lexical.ms),
        second=AvenueResult(
            avenue=method_id,
            ran=second_result is not None,
            classification=second_result,
            ms=second_ms,
            detail=""
            if second_result is not None
            else (
                problem
                or "no second classification avenue is available: none of the four visual "
                "methods measured against the real corpus reached the 95% precision bar"
            ),
        ),
        second_avenue=_second_avenue_status(settings, registry),
        source=source,
        ms=_ms(started),
    )


@router.post("/extract", response_model=ExtractionResult)
def extract_document(
    response: Response,
    req: ExtractRequest,
    registry: RegistryPort = Depends(get_registry),
    classifier: ClassifierPort | None = Depends(get_classifier_or_none),
    extractor: ExtractorPort = Depends(get_extractor),
    settings: Settings = Depends(get_app_settings),
) -> ExtractionResult:
    """Extract a doctype's fields from a document — **T1 only, free, no egress**.

    Pin the doctype with ``doctype_id``, or omit it to classify first. An unknown
    ``doctype_id`` is a ``404`` — silently classifying instead would hide an integrator's typo
    behind plausible-looking output.

    The paid tiers deliberately do not run here. ``/extract`` is the surface an integrator
    calls in a loop while tuning locators, and a route that quietly billed per call would be a
    trap; ``/process`` is where escalation happens, and it reports what it spent.
    """
    started = time.perf_counter()
    view = _to_layout(req)
    _report_source(response, view)

    if req.doctype_id:
        spec = registry.get(req.doctype_id)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"unknown doctype: {req.doctype_id}")
    else:
        if classifier is None:
            raise _unavailable("classifier", _CLASSIFY_MODULES)
        classification = classifier.classify(view, registry=registry, settings=settings)
        observability.observe_classification(classification)
        if classification.abstained:
            return ExtractionResult(
                doctype_id=UNKNOWN,
                schema_version=req.schema_version or "",
                needs_review=True,
                ms=_ms(started),
            )
        spec = registry.get(classification.doctype_id)
        if spec is None:
            raise HTTPException(
                status_code=404,
                detail=f"classified as {classification.doctype_id}, which is not in the registry",
            )

    extract_started = time.perf_counter()
    result = extractor.extract(
        view, spec, settings=settings, schema_version=req.schema_version
    )
    result.doctype_id = result.doctype_id or spec.doctype_id
    result.schema_version = result.schema_version or req.schema_version or _schema_version_for(spec)
    result.ms = result.ms or _ms(started)
    observability.observe_extraction(result, time.perf_counter() - extract_started)
    return result


@router.post("/process/segments", response_model=SegmentsResponse)
def process_segments(
    response: Response,
    req: DocumentRequest,
    registry: RegistryPort = Depends(get_registry),
    extractor: ExtractorPort = Depends(get_extractor),
    settings: Settings = Depends(get_app_settings),
) -> SegmentsResponse:
    """Segment an upload, then classify and extract each document in it separately.

    Extraction is per document by nature — a field schema belongs to one doctype — so a bundle
    yields one extraction per segment, each run against that segment's pages alone.

    **A segment that abstained is not extracted.** Extraction needs a doctype to know what to
    look for, and running it against a document nobody has identified would produce fields
    attributed to a guess. Those segments come back with ``extraction: null`` and
    ``needs_review: true``, which is the same posture ``/process`` takes for a whole document.

    Paid tiers are deliberately **not** run here. T2/T3 bill per call, and a bundle multiplies
    the call count by its segment count; turning that on silently would let one upload cost
    what an operator budgeted for one document. Use ``/extract`` per segment when a bundle
    should escalate.
    """
    started = time.perf_counter()
    view = _to_layout(req)
    source = _report_source(response, view)
    out = _segment_response(view, settings, started)
    out.source = source

    from dce.classify.cascade import page_range_view

    for segment in out.segments:
        if segment.classification.abstained:
            # Recorded rather than left blank. "Nothing ran" and "nothing ran BECAUSE the
            # cascade abstained here" are different facts, and only the second tells a reader
            # that the silence was a decision.
            segment.tiers_used = [
                TierRun(
                    tier=TIER_LOCAL,
                    status="skipped",
                    detail="classification abstained for these pages; nothing was extracted",
                )
            ]
            continue
        spec = registry.get(segment.classification.doctype_id)
        if spec is None:
            # Classified as something the registry does not hold. Not a 404 as it would be on
            # /extract — one odd segment must not fail the other three — so the segment keeps
            # its classification, carries no extraction, and goes to review.
            segment.needs_review = True
            continue
        extract_started = time.perf_counter()
        result = extractor.extract(
            page_range_view(view, segment.start_page, segment.end_page),
            spec,
            settings=settings,
        )
        result.doctype_id = result.doctype_id or spec.doctype_id
        result.schema_version = result.schema_version or _schema_version_for(spec)
        result.ms = result.ms or _ms(extract_started)
        observability.observe_extraction(result, time.perf_counter() - extract_started)
        segment.extraction = result
        segment.needs_review = result.needs_review
        # The ledger, per segment. T1 ran; saying so is the difference between "nothing was
        # asked to fill a field" and "the free tier filled four of seven". Omitting it made a
        # console report no tier activity on a document it had just extracted.
        filled = [f.name for f in result.fields if f.value]
        segment.tiers_used = [
            TierRun(
                tier=TIER_LOCAL,
                status="ran",
                fields_filled=len(filled),
                fields=filled,
                ms=result.ms,
            )
        ]
    out.ms = _ms(started)
    return out


@router.post("/process", response_model=ProcessResponse)
def process_document(
    request: Request,
    response: Response,
    req: DocumentRequest,
    registry: RegistryPort = Depends(get_registry),
    classifier: ClassifierPort = Depends(get_classifier),
    extractor: ExtractorPort | None = Depends(get_extractor_or_none),
    settings: Settings = Depends(get_app_settings),
) -> ProcessResponse:
    """Classify, then extract through the tier cascade — the common path.

    T1 (local) always runs. T2 (Azure prebuilt), T3 (Azure queryFields) and T4 (constrained
    LLM) run **only** when their flag is on and only over the fields their predecessors left
    empty; each is a network call and is off by default. T5 puts what is left in front of a
    human. Which tiers ran, what each filled and what each cost is reported in ``tiers_used``.

    When classification abstains this returns the classification with ``needs_review`` set and
    **extracts nothing, calls nobody**: extracting against a guessed doctype produces
    confidently wrong fields, and sending an unclassified document to Azure or a model is the
    exact disclosure this service exists to prevent. Abstention short-circuits both.
    """
    started = time.perf_counter()
    view = _to_layout(req)
    source = _report_source(response, view)
    adapt_ms = _ms(started)

    classify_started = time.perf_counter()
    classification = classifier.classify(view, registry=registry, settings=settings)
    classify_seconds = time.perf_counter() - classify_started
    classification.ms = classification.ms or int(classify_seconds * 1000)
    observability.observe_classification(classification, classify_seconds)

    timings = Timings(
        total_ms=_ms(started), adapt_ms=adapt_ms, classify_ms=classification.ms
    )
    if classification.abstained:
        # Nothing below this line runs. Not the extractor, and — the load-bearing half — not
        # one of the paid tiers, which would be egress about a document nobody has identified.
        queued, review_ids = enqueue_for_review(
            request,
            classification=classification,
            extraction=None,
            view=view,
            settings=settings,
        )
        return ProcessResponse(
            classification=classification,
            source=source,
            extraction=None,
            needs_review=True,
            detail=classification.reason
            or "classification abstained; queued for human review, nothing was extracted",
            tiers_used=[queued],
            review_ids=review_ids,
            timings=timings,
        )

    spec = registry.get(classification.doctype_id)
    if spec is None:
        queued, review_ids = enqueue_for_review(
            request,
            classification=classification,
            extraction=None,
            view=view,
            settings=settings,
            reason="unregistered_doctype",
        )
        return ProcessResponse(
            classification=classification,
            source=source,
            extraction=None,
            needs_review=True,
            detail=f"doctype {classification.doctype_id} is not in the registry",
            tiers_used=[queued],
            review_ids=review_ids,
            timings=timings,
        )

    if extractor is None:
        raise _unavailable("extractor", _EXTRACT_MODULES)

    # ---- T1: the local resolver. Free, deterministic, and always first. ----
    extract_started = time.perf_counter()
    extraction = extractor.extract(view, spec, settings=settings)
    extract_seconds = time.perf_counter() - extract_started
    extraction.doctype_id = extraction.doctype_id or spec.doctype_id
    extraction.schema_version = extraction.schema_version or _schema_version_for(spec)
    extraction.ms = extraction.ms or int(extract_seconds * 1000)
    local_filled = [f.name for f in extraction.fields if f.value]
    tiers_used = [
        TierRun(
            tier=TIER_LOCAL,
            status="ran",
            fields_filled=len(local_filled),
            fields=local_filled,
            ms=extraction.ms,
        )
    ]
    observability.observe_extraction_tier(
        TIER_LOCAL, seconds=extract_seconds, fields_filled=len(local_filled)
    )

    # ---- T2 -> T3 -> T4: only what is still missing, only when switched on. ----
    tiers_started = time.perf_counter()
    tiers_used += run_tier_cascade(
        request,
        view=view,
        spec=spec,
        classification=classification,
        result=extraction,
        settings=settings,
        content=_document_bytes(req),
    )
    tiers_ms = int((time.perf_counter() - tiers_started) * 1000)

    # Metrics for the *finished* extraction, so fill rate reflects every tier that touched it.
    observability.observe_extraction(extraction, extract_seconds)

    # ---- T5: a human, if the cascade still could not finish the job. ----
    needs_review = extraction.needs_review or bool(extraction.missing_required)
    review_ids: list[str] = []
    if needs_review:
        queued, review_ids = enqueue_for_review(
            request,
            classification=classification,
            extraction=extraction,
            view=view,
            settings=settings,
            spec=spec,
        )
        tiers_used.append(queued)

    return ProcessResponse(
        classification=classification,
        source=source,
        extraction=extraction,
        needs_review=needs_review,
        detail="missing required fields" if extraction.missing_required else "",
        tiers_used=tiers_used,
        review_ids=review_ids,
        timings=Timings(
            total_ms=_ms(started),
            adapt_ms=adapt_ms,
            classify_ms=classification.ms,
            extract_ms=extraction.ms,
            tiers_ms=tiers_ms,
        ),
    )


@router.get("/doctypes", response_model=DocTypeListResponse)
async def list_doctypes(
    registry: RegistryPort = Depends(get_registry),
    country: str | None = None,
    category: Category | None = None,
) -> DocTypeListResponse:
    """List the doctype registry, optionally filtered by country or category."""
    started = time.perf_counter()
    specs = registry.specs()
    if country:
        specs = [s for s in specs if s.country.upper() == country.upper()]
    if category:
        specs = [s for s in specs if s.category is category]
    return DocTypeListResponse(
        count=len(specs),
        doctypes=[_summarize(s) for s in specs],
        timings=Timings(total_ms=_ms(started)),
    )


@router.get("/doctypes/{doctype_id}", response_model=DocTypeSpec)
async def get_doctype(
    doctype_id: str, registry: RegistryPort = Depends(get_registry)
) -> DocTypeSpec:
    """The full spec for one doctype: anchors, id patterns, confusables and field locators."""
    spec = registry.get(doctype_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"unknown doctype: {doctype_id}")
    return spec


@router.get("/schemas/{doctype_id}", response_model=SchemaResponse)
async def get_schema(
    doctype_id: str, registry: RegistryPort = Depends(get_registry)
) -> SchemaResponse:
    """The active field schema for a doctype.

    Served by the schema store when one is deployed; otherwise derived from the registry spec,
    which *is* the schema in a build with no schema store — with a content-addressed version so
    a caller can still detect drift.
    """
    started = time.perf_counter()
    spec = registry.get(doctype_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"unknown doctype: {doctype_id}")

    peer = _schema_from_peer(doctype_id)
    fields = _fields_of(peer) if peer is not None else []
    if fields:
        return SchemaResponse(
            doctype_id=doctype_id,
            schema_version=str(_attr(peer, "schema_version") or _schema_version_for(spec)),
            active=bool(_attr(peer, "active", True)),
            source="schema-store",
            label=spec.label,
            country=spec.country,
            fields=fields,
            timings=Timings(total_ms=_ms(started)),
        )
    return SchemaResponse(
        doctype_id=doctype_id,
        schema_version=_schema_version_for(spec),
        active=True,
        source="registry",
        label=spec.label,
        country=spec.country,
        fields=spec.fields,
        timings=Timings(total_ms=_ms(started)),
    )


@router.post("/schemas/induce", response_model=SchemaResponse, status_code=200)
def induce_schema(
    req: InduceRequest, settings: Settings = Depends(get_app_settings)
) -> SchemaResponse:
    """Draft a schema from sample documents. **The draft is never activated.**

    Induction is a labour-saver for the integrator who is adding a doctype, not an autonomous
    path into production: a schema that activated itself would silently change what the service
    extracts, and in a KYC system that is a change nobody signed off on. Review the draft, put
    it in the registry, deploy.
    """
    started = time.perf_counter()
    views = [_to_layout(sample) for sample in req.samples]

    fn = _load(_SCHEMA_MODULES, ("induce_schema", "induce"), "induce_schema")
    if fn is not None:
        try:
            drafted = _call_supported(
                fn, views, doctype_id=req.doctype_id, min_support=req.min_support,
                settings=settings,
            )
            fields = _fields_of(drafted)
            if fields:
                return SchemaResponse(
                    doctype_id=req.doctype_id,
                    schema_version=str(_attr(drafted, "schema_version") or "draft"),
                    active=False,
                    source="schema-store",
                    label=req.label,
                    country=req.country,
                    fields=fields,
                    sample_count=len(views),
                    notes="draft — inactive until reviewed and registered",
                    timings=Timings(total_ms=_ms(started)),
                )
        except Exception:
            logger.warning("schema induction module raised; using built-in", exc_info=True)

    fields = _induce_fields(views, req.min_support)
    return SchemaResponse(
        doctype_id=req.doctype_id,
        schema_version="draft",
        active=False,
        source="induced",
        label=req.label,
        country=req.country,
        fields=fields,
        sample_count=len(views),
        notes=(
            "draft — inactive until reviewed and registered. Induced from provider key-value "
            f"pairs and table headers present in >= {req.min_support:.0%} of samples; anchors, "
            "validators and attribute keys still need a human."
        ),
        timings=Timings(total_ms=_ms(started)),
    )


# ---------------------------------------------------------------------------
# T5 — the human review queue
# ---------------------------------------------------------------------------
@router.get("/review", response_model=ReviewListResponse)
def list_review_queue(
    queue: ReviewPort = Depends(require_review_queue),
    status: str = "pending",
    doctype: str | None = None,
    limit: int = 100,
) -> ReviewListResponse:
    """List what is waiting for a human.

    Args:
        queue: The review queue.
        status: Filter by status; ``all`` (or empty) returns every status. Defaults to
            ``pending``, because the queue's job is the backlog and everything else is history.
        doctype: Filter by doctype id.
        limit: Maximum items returned.

    Returns:
        The filtered page, plus the queue's total depth when it can report one.
    """
    started = time.perf_counter()
    wanted = None if status in ("", "all") else status
    items = queue.list(status=wanted, doctype=doctype, limit=limit)
    depth = queue.depth()
    if depth is not None:
        observability.set_review_queue_depth(depth)
    return ReviewListResponse(
        count=len(items),
        items=[_review_item(item) for item in items],
        depth=depth,
        timings=Timings(total_ms=_ms(started)),
    )


def _decide(queue: ReviewPort, item_id: str, decision: str, body: ReviewDecision) -> ReviewItem:
    """Apply one human decision and record how long the document waited for it.

    Raises:
        HTTPException: ``404`` when the queue does not know the id; ``409`` when the queue
            understood the request and refused it — an item somebody already decided, the same
            reviewer trying to be both halves of a double entry, or two independent entries
            that disagree. The ``409`` body carries the queue's own sentence, because that
            sentence says what the reviewer has to do next.
    """
    before = queue.get(item_id)
    if before is None:
        raise HTTPException(status_code=404, detail=f"unknown review item: {item_id}")
    waited = _waited_seconds(before)
    try:
        updated = queue.decide(
            item_id, decision, reviewer=body.reviewer, note=body.note, value=body.value
        )
    except ReviewConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=501, detail=f"the review queue cannot {decision} an item")

    observability.observe_review_decision(decision, waited)
    depth = queue.depth()
    if depth is not None:
        observability.set_review_queue_depth(depth)
    # The item id and the reviewer, never the value: these are somebody's identifiers.
    logger.info("review %s: item=%s reviewer=%s", decision, item_id, body.reviewer)
    return _review_item(updated)


@router.post("/review/{item_id}/approve", response_model=ReviewItem)
def approve_review_item(
    item_id: str,
    body: ReviewDecision,
    queue: ReviewPort = Depends(require_review_queue),
) -> ReviewItem:
    """Confirm the extracted value.

    One approval closes an ordinary item. A **double-entry** item — a field that is both PII
    and backed by a real check digit — needs two, from two different people; the first approval
    is recorded and the item comes back still ``pending`` with ``approvals`` naming who has
    signed. That is a success, not a failure: check ``status``, not the HTTP code.
    """
    return _decide(queue, item_id, "approve", body)


@router.post("/review/{item_id}/reject", response_model=ReviewItem)
def reject_review_item(
    item_id: str,
    body: ReviewDecision,
    queue: ReviewPort = Depends(require_review_queue),
) -> ReviewItem:
    """Throw the answer away.

    A rejection is a data point, not a dead end: a doctype whose items are routinely rejected
    is a registry problem, and the queue is the only place that fact is visible.
    """
    return _decide(queue, item_id, "reject", body)


@router.post("/review/{item_id}/correct", response_model=ReviewItem)
def correct_review_item(
    item_id: str,
    body: ReviewDecision,
    queue: ReviewPort = Depends(require_review_queue),
) -> ReviewItem:
    """Type in what the document actually says.

    **Blind double entry.** On a field that is both PII and checksum-backed, the first
    reviewer's value is recorded and the item stays ``pending``; a second, different reviewer
    must type the same value independently. A mismatch discards *both* entries and returns
    ``409`` — the item goes back to square one rather than inheriting the value one of the two
    people got wrong. That is the whole point of keying an identifier twice: a typo in a
    checksummed id silently becomes a *valid-looking* identifier belonging to somebody else,
    and no downstream system can tell. The rule lives in :mod:`dce.review`, not in a UI; this
    route supplies the identity it needs to enforce it.

    Raises:
        HTTPException: ``400`` when no value was supplied — an empty correction is an approval,
            and it should be sent as one so the audit trail says what actually happened.
    """
    if not body.value.strip():
        raise HTTPException(
            status_code=400,
            detail="correct requires a value; an empty correction is an approval — "
            "use /approve, or /reject to discard the field",
        )
    return _decide(queue, item_id, "correct", body)


# ---------------------------------------------------------------------------
# System routes (never behind the API key: probes and scrapers need them)
# ---------------------------------------------------------------------------
@system_router.get("/health")
async def health() -> dict[str, str]:
    """Liveness only: the process is up and serving. Never touches an engine."""
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


@system_router.get("/readyz", response_model=ReadinessResponse)
async def readyz(
    response: Response,
    registry: RegistryPort | None = Depends(get_registry_or_none),
    settings: Settings = Depends(get_app_settings),
) -> ReadinessResponse:
    """Readiness: registry size, the optional BERT tier, and the egress invariant.

    Returns ``503`` when the service is not ready, so a load balancer is told the truth
    instead of getting a green light over a process that can only abstain.
    """
    specs = registry.specs() if registry is not None else []
    READINESS.set(
        "registry",
        bool(specs),
        "" if specs else "no doctypes loaded",
        doctypes=len(specs),
    )
    egress_ok = not settings.allow_preclassification_egress
    READINESS.set(
        "egress",
        egress_ok,
        "" if egress_ok else "pre-classification egress is ALLOWED — the invariant is off",
    )

    ocr = _ocr_status()
    # Reported as a component, not as an outage. A remote recogniser is a decision somebody
    # took on purpose; failing readiness over it would take a working service out of rotation
    # for having been configured the way it was configured. What it must never be is *quiet*,
    # so the disclosure is in the component's own note as well as in `ocr` and `egress`.
    READINESS.set(
        "ocr",
        not ocr.problem,
        ocr.problem or ("" if not ocr.network else ocr.summary),
        provider=ocr.provider,
        network=ocr.network,
        endpoint_host=ocr.endpoint_host,
    )

    bert = bert_status(settings)
    if settings.bert_enabled:
        READINESS.set("bert", bert["loaded"], "" if bert["loaded"] else "model not loaded")

    # Reported, never a readiness failure. There being no second avenue is the measured state
    # of the art here, not an outage — the service classifies perfectly well with one — but a
    # deployment that *asked* for an avenue and cannot have it is degraded, because somebody
    # is expecting a second opinion that will never arrive.
    second_avenue = _second_avenue_status(settings, registry)
    READINESS.set(
        "second_avenue",
        not second_avenue.problem,
        second_avenue.problem,
        available=second_avenue.available,
        method=second_avenue.method,
        templates=second_avenue.templates,
        doctypes_covered=second_avenue.doctypes_covered,
        doctypes_total=second_avenue.doctypes_total,
        coverage=second_avenue.coverage,
    )

    tiers = _tier_statuses(settings)
    broken = [t.problem for t in tiers if t.problem]
    # Degraded, not not-ready: a half-configured paid tier costs you those fields; taking the
    # process out of rotation would cost you classification, which still works perfectly.
    READINESS.set(
        "tiers",
        not broken,
        "; ".join(broken),
        enabled=list(settings.egress_tiers()),
        review_queue_backend=settings.review_queue_backend,
    )

    ready = READINESS.ready()
    if not ready:
        response.status_code = 503
    return ReadinessResponse(
        ready=ready,
        service=SERVICE_NAME,
        version=__version__,
        registry=RegistryStatus(
            loaded=registry is not None,
            doctypes=len(specs),
            countries=sorted({s.country for s in specs if s.country}),
        ),
        bert=bert,
        egress=EgressStatus(
            preclassification_allowed=settings.allow_preclassification_egress,
            enforced=egress_ok,
            note=_egress_note(ocr),
            preclassification_ocr=ocr.network,
            preclassification_ocr_endpoint=ocr.endpoint_host if ocr.network else "",
            preclassification_ocr_trust_boundary=ocr.trust_boundary if ocr.network else "",
        ),
        ocr=ocr,
        second_avenue=second_avenue,
        tiers=tiers,
        components=READINESS.snapshot(),
        degraded=READINESS.degraded(),
    )


@system_router.get("/metrics")
async def metrics() -> Response:
    """Prometheus exposition. Watch ``dce_classifications_total{outcome="abstained"}``."""
    payload, content_type = observability.metrics_response()
    return Response(content=payload, media_type=content_type)
