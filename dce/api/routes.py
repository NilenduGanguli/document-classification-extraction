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
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import inspect
import logging
import math
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from dce import SERVICE_NAME, __version__, adapters, observability
from dce.config import Settings
from dce.models import (
    UNKNOWN,
    Category,
    Classification,
    DocTypeSpec,
    ExtractionResult,
    FieldSpec,
    LayoutView,
)
from dce.observability import READINESS, ComponentState

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
    ``azure_analyze_result``, ``des_ocr``, ``text``. Sending only ``text`` is supported and
    degrades gracefully — see :func:`dce.adapters.from_plain_text`.
    """

    doc_id: str = ""
    layout: LayoutView | None = None
    text: str | None = None
    azure_analyze_result: dict[str, Any] | None = None
    des_ocr: dict[str, Any] | None = None


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


class ProcessResponse(BaseModel):
    """Classification plus extraction, or classification alone when the cascade abstained."""

    classification: Classification
    extraction: ExtractionResult | None = None
    needs_review: bool = False
    detail: str = ""
    timings: Timings = Field(default_factory=Timings)


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
    """The invariant, reported so an operator can see it without reading the config."""

    preclassification_allowed: bool
    enforced: bool
    note: str


class ReadinessResponse(BaseModel):
    ready: bool
    service: str
    version: str
    registry: RegistryStatus
    bert: dict[str, Any]
    egress: EgressStatus
    components: dict[str, ComponentState] = Field(default_factory=dict)
    degraded: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _to_layout(req: DocumentRequest) -> LayoutView:
    """Adapt a request payload into a :class:`LayoutView`.

    Raises:
        HTTPException: ``400`` when the request carried no document at all.
    """
    if req.layout is not None:
        view = req.layout
    elif req.azure_analyze_result is not None:
        view = adapters.from_azure_layout(req.azure_analyze_result)
    elif req.des_ocr is not None:
        view = adapters.from_des_ocr(req.des_ocr)
    elif req.text is not None:
        view = adapters.from_plain_text(req.text)
    else:
        raise HTTPException(
            status_code=400,
            detail="supply one of: layout, azure_analyze_result, des_ocr, text",
        )
    if req.doc_id:
        view.doc_id = req.doc_id
    return view


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
# Routers
# ---------------------------------------------------------------------------
#: Business routes. The API-key gate is applied here and not to the system routes, so a probe
#: and a Prometheus scraper keep working when a key is configured.
router = APIRouter(prefix="/api/v1", tags=["dce"], dependencies=[Depends(require_api_key)])
system_router = APIRouter(tags=["system"])


# Classification and extraction are CPU-bound and fully in-process, so these handlers are
# declared sync on purpose: FastAPI runs them in the threadpool, where they cannot stall the
# event loop for every other request.
@router.post("/classify", response_model=Classification)
def classify_document(
    req: DocumentRequest,
    registry: RegistryPort = Depends(get_registry),
    classifier: ClassifierPort = Depends(get_classifier),
    settings: Settings = Depends(get_app_settings),
) -> Classification:
    """Classify a document entirely in-process.

    Returns ``200`` with ``abstained=true`` when the cascade cannot clear its thresholds; that
    is a valid answer, and the document goes to a human rather than to a model.
    """
    started = time.perf_counter()
    view = _to_layout(req)
    result = classifier.classify(view, registry=registry, settings=settings)
    result.ms = result.ms or _ms(started)
    observability.observe_classification(result, time.perf_counter() - started)
    return result


@router.post("/extract", response_model=ExtractionResult)
def extract_document(
    req: ExtractRequest,
    registry: RegistryPort = Depends(get_registry),
    classifier: ClassifierPort | None = Depends(get_classifier_or_none),
    extractor: ExtractorPort = Depends(get_extractor),
    settings: Settings = Depends(get_app_settings),
) -> ExtractionResult:
    """Extract a doctype's fields from a document.

    Pin the doctype with ``doctype_id``, or omit it to classify first. An unknown
    ``doctype_id`` is a ``404`` — silently classifying instead would hide an integrator's typo
    behind plausible-looking output.
    """
    started = time.perf_counter()
    view = _to_layout(req)

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


@router.post("/process", response_model=ProcessResponse)
def process_document(
    req: DocumentRequest,
    registry: RegistryPort = Depends(get_registry),
    classifier: ClassifierPort = Depends(get_classifier),
    extractor: ExtractorPort | None = Depends(get_extractor_or_none),
    settings: Settings = Depends(get_app_settings),
) -> ProcessResponse:
    """Classify, then extract — the common path.

    When classification abstains this returns the classification with ``needs_review`` set and
    **does not extract**: extracting against a guessed doctype produces confidently wrong
    fields, which is worse than no fields at all.
    """
    started = time.perf_counter()
    view = _to_layout(req)
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
        return ProcessResponse(
            classification=classification,
            extraction=None,
            needs_review=True,
            detail=classification.reason
            or "classification abstained; queued for human review, nothing was extracted",
            timings=timings,
        )

    spec = registry.get(classification.doctype_id)
    if spec is None:
        return ProcessResponse(
            classification=classification,
            extraction=None,
            needs_review=True,
            detail=f"doctype {classification.doctype_id} is not in the registry",
            timings=timings,
        )

    if extractor is None:
        raise _unavailable("extractor", _EXTRACT_MODULES)
    extract_started = time.perf_counter()
    extraction = extractor.extract(view, spec, settings=settings)
    extract_seconds = time.perf_counter() - extract_started
    extraction.doctype_id = extraction.doctype_id or spec.doctype_id
    extraction.schema_version = extraction.schema_version or _schema_version_for(spec)
    extraction.ms = extraction.ms or int(extract_seconds * 1000)
    observability.observe_extraction(extraction, extract_seconds)

    needs_review = extraction.needs_review or bool(extraction.missing_required)
    return ProcessResponse(
        classification=classification,
        extraction=extraction,
        needs_review=needs_review,
        detail="missing required fields" if extraction.missing_required else "",
        timings=Timings(
            total_ms=_ms(started),
            adapt_ms=adapt_ms,
            classify_ms=classification.ms,
            extract_ms=extraction.ms,
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
    bert = bert_status(settings)
    if settings.bert_enabled:
        READINESS.set("bert", bert["loaded"], "" if bert["loaded"] else "model not loaded")

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
            note=(
                "classification is in-process only: no HTTP, no vendor SDK, no embedding API "
                "before the doctype is known"
            ),
        ),
        components=READINESS.snapshot(),
        degraded=READINESS.degraded(),
    )


@system_router.get("/metrics")
async def metrics() -> Response:
    """Prometheus exposition. Watch ``dce_classifications_total{outcome="abstained"}``."""
    payload, content_type = observability.metrics_response()
    return Response(content=payload, media_type=content_type)
