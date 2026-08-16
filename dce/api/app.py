"""FastAPI application factory and lifespan.

Startup does two things and neither of them is I/O against a network: it asserts the
pre-classification egress invariant, and it best-effort loads the engine modules so that the
first request is not the one that pays for the import (and so ``/readyz`` tells the truth
immediately rather than after the first classify). A missing engine is recorded, not fatal —
the process still serves ``/health``, ``/readyz`` and ``/metrics``, which is what an operator
needs in order to see *why* it is degraded.

Run it with ``uvicorn dce.api.app:app --port 8200``.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dce import SERVICE_NAME, __version__, observability
from dce.api.routes import (
    load_classifier_port,
    load_extractor_port,
    load_registry_port,
    router,
    system_router,
)
from dce.config import Settings, get_settings
from dce.observability import READINESS

logger = logging.getLogger(__name__)

def _configure_logging() -> None:
    """Set this package's log level from ``DCE_LOG_LEVEL``.

    Uvicorn configures the root logger for its own loggers and leaves everything else at
    WARNING, so before this existed the only way to see a ``logger.info`` from ``dce`` was to
    raise uvicorn's own level and take its access log with it. ``DCE_LOG_LEVEL=DEBUG`` raises
    this package alone.

    Deliberately narrow: it sets the level on the ``dce`` logger and adds a handler only if
    nothing upstream has one, so a deployment that configures logging centrally — a JSON
    formatter, a log shipper — keeps its own configuration and only the level moves.
    """
    level = os.environ.get("DCE_LOG_LEVEL", "").strip().upper()
    if not level:
        return
    package = logging.getLogger("dce")
    package.setLevel(getattr(logging, level, logging.INFO))
    if not package.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        package.addHandler(handler)


#: Client-side routes of the console. A deep link or a refresh on one of these has to be
#: answered with ``index.html`` — the browser router, not the server, resolves the rest.
_UI_ROUTES = ("/analyze", "/registry", "/review", "/posture")

#: Paths that belong to the service and must never be answered with the SPA shell. A 404 under
#: any of these is an API 404 and stays JSON, because a caller parsing a response should not
#: suddenly receive a web page.
_API_PREFIXES = ("/api", "/health", "/readyz", "/metrics", "/docs", "/redoc", "/openapi.json")

_DESCRIPTION = """
Classifies and extracts documents that **have not been classified yet**.

Classification is 100% in-process — anchors, checksums, zone-weighted lexical scoring and an
optional *local* BERT. Nothing about an unclassified document is sent anywhere: no HTTP, no
vendor SDK, no embedding API. When the cascade cannot decide it abstains to `unknown` and the
document goes to a human queue, never to a model.
"""


def _load_engines(app: FastAPI) -> None:
    """Load the engine modules once, recording each outcome in the readiness registry."""
    registry = load_registry_port()
    app.state.registry = registry
    specs = registry.specs() if registry is not None else []
    READINESS.set(
        "registry",
        bool(specs),
        "" if specs else "no doctype registry available",
        doctypes=len(specs),
    )

    classifier = load_classifier_port()
    app.state.classifier = classifier
    READINESS.set("classifier", classifier is not None, "" if classifier else "not importable")

    extractor = load_extractor_port()
    app.state.extractor = extractor
    READINESS.set("extractor", extractor is not None, "" if extractor else "not importable")

    logger.info(
        "engines loaded: registry=%s (%d doctypes) classifier=%s extractor=%s",
        registry is not None,
        len(specs),
        classifier is not None,
        extractor is not None,
    )


def _assert_invariant(settings: Settings) -> None:
    """Record — and shout about — the pre-classification egress invariant.

    Turning it off is a deliberate, auditable act, so it is logged at ``error`` and surfaces on
    ``/readyz`` as not-ready. A service that quietly allowed unclassified documents out would
    be indistinguishable from one that never could.
    """
    allowed = settings.allow_preclassification_egress
    READINESS.set(
        "egress",
        not allowed,
        "" if not allowed else "pre-classification egress is ALLOWED — the invariant is off",
        allow_preclassification_egress=allowed,
    )
    if allowed:
        logger.error(
            "allow_preclassification_egress=true: unclassified documents may leave this process. "
            "This is not a tuning knob; turn it off unless you meant it."
        )
    else:
        logger.info("pre-classification egress: blocked (classification is in-process only)")


def _assert_ocr_posture() -> None:
    """Resolve the ingestion settings at boot, and say how this deployment reads an image.

    Two reasons this is here rather than left to the first request:

    * a contradictory OCR configuration (both kinds of recogniser configured with no default
      named) raises out of :class:`~dce.ingest.settings.IngestSettings`, and it should take the
      process down at boot the way a missing BERT mount does — not surface as a 500 on
      whichever request happened to carry an image;
    * a deployment that sends unclassified documents out of this process should say so in the
      first few lines of its own log, exactly as ``allow_preclassification_egress=true`` does.

    The *level* follows the declared trust boundary, and only the level. An endpoint the
    operator has declared on-premises is logged at ``info``: still on every boot, still naming
    the provider and the host, still saying the document is read before its doctype is known —
    but as configuration, because a ``warning`` on every healthy boot of a deployment that is
    configured exactly as intended is how a log level stops meaning anything. Undeclared or
    external keeps ``error``, and the line says which of the two it is.
    """
    from dce.ingest.settings import (
        TRUST_BOUNDARY_ON_PREMISES,
        get_ingest_settings,
        legacy_env_aliases_in_use,
    )

    legacy = legacy_env_aliases_in_use()
    if legacy:
        logger.warning(
            "deprecated ingestion environment variables in use: %s. They are still honoured "
            "as aliases and nothing is broken; rename them at your convenience.",
            "; ".join(f"{old} -> {new}" for old, new in sorted(legacy.items())),
        )

    settings = get_ingest_settings()
    service_providers = settings.service_providers()
    if service_providers:
        on_premises = settings.trust_boundary() == TRUST_BOUNDARY_ON_PREMISES
        problem = settings.ocr_service_problem()
        logger.log(
            logging.INFO if on_premises else logging.ERROR,
            "OCR service configured (%s -> %s): images and scanned PDFs are read there, "
            "BEFORE their doctype is known. Declared trust boundary: %s%s — the operator's "
            "declaration, recorded here and not verified. /readyz reports "
            "egress.preclassification_ocr=true.%s",
            ", ".join(service_providers),
            settings.ocr_service_endpoint_host() or "(no endpoint configured)",
            settings.trust_boundary(),
            "" if settings.trust_boundary_declared() else " (code default; nothing declared)",
            f" Problem: {problem}" if problem else "",
        )
    if settings.local_ocr_enabled:
        logger.info(
            "OCR: in-process engine %s — no document is sent anywhere to be read",
            settings.local_ocr_engine,
        )
    if not service_providers and not settings.local_ocr_enabled:
        logger.info("OCR: none configured — images return needs_ocr and nothing is sent out")


def _frontend_dist() -> Path | None:
    """Locate the compiled console, or ``None`` when it has not been built.

    Three candidates, in order, because the same code runs from three different working
    directories: an explicit ``DCE_FRONTEND_DIST`` (for a deployment that serves the bundle
    from somewhere else), the path next to the installed package (``/app/frontend/dist`` in
    the image, where ``dce/`` sits at ``/app/dce``), and the repo-root-relative path that a
    developer running ``uvicorn`` from the checkout gets.

    Returns ``None`` rather than raising: a missing bundle is a developer who has not run
    ``npm run build`` yet, and the API must keep working for them.
    """
    override = os.environ.get("DCE_FRONTEND_DIST", "").strip()
    candidates = (
        [Path(override)]
        if override
        else [Path(__file__).resolve().parents[2] / "frontend" / "dist", Path("frontend/dist")]
    )
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: Settings to run with. Defaults to the process settings from the environment.
            Tests pass an explicit instance instead of mutating the environment.

    Returns:
        A configured :class:`fastapi.FastAPI` instance.
    """
    resolved = settings or get_settings()
    _configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _assert_invariant(app.state.settings)
        _assert_ocr_posture()
        _load_engines(app)
        yield

    app = FastAPI(
        title="Document Classification & Extraction",
        description=_DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    # Engine ports are resolved on first use and cached here; the lifespan fills them earlier
    # when it runs. Both paths converge, so TestClient without a context manager still works.
    app.state.registry = None
    app.state.classifier = None
    app.state.extractor = None

    @app.middleware("http")
    async def _timings(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Time every request: ``X-Elapsed-Ms`` on the response, latency in the metrics.

        The metric is labelled with the *route template*, never the raw path — an unmatched
        path is attacker-controlled and would blow up cardinality.
        """
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - started
        response.headers["X-Elapsed-Ms"] = str(int(elapsed * 1000))
        route = getattr(request.scope.get("route"), "path", None) or "unmatched"
        observability.observe_http(request.method, route, response.status_code, elapsed)
        return response

    dist = _frontend_dist()
    app.state.frontend_dist = dist

    def _descriptor() -> dict[str, object]:
        """The service card served at ``/`` when there is no console to serve instead."""
        card: dict[str, object] = {
            "service": SERVICE_NAME,
            "version": __version__,
            "docs": "/docs",
            "health": "/health",
            "readiness": "/readyz",
            "metrics": "/metrics",
            "api": "/api/v1",
        }
        if dist is None:
            card["ui"] = (
                "not built — the console is a vite bundle that is compiled into "
                "frontend/dist and served from this process. Build it with "
                "`cd frontend && npm install && npm run build`, or set DCE_FRONTEND_DIST to "
                "an existing bundle, then restart. The API above works either way."
            )
        return card

    @app.get("/", include_in_schema=False)
    async def _root() -> Response:
        """The console, when it is built; the service card when it is not.

        This route is declared explicitly rather than left to the static mount because a
        ``@app.get("/")`` registered anywhere would win over the mount anyway — better to say
        what ``/`` returns in one place than to have it depend on declaration order.
        """
        if dist is not None:
            return FileResponse(dist / "index.html")
        return JSONResponse(_descriptor())

    app.include_router(system_router)
    app.include_router(router)

    # ------------------------------------------------------------------
    # The console. MOUNTED LAST, AND THE ORDER IS LOAD-BEARING.
    #
    # Starlette matches routes in registration order, and a Mount at "/" matches *every* path
    # under it. Mounted before the routers it would shadow the entire API — every
    # /api/v1/... call would come back as a 404 from StaticFiles, with no hint that a router
    # existed. So: routers first, static last. Do not move this above the include_router
    # calls, and do not add a route after it that you expect to be reachable.
    #
    # When the bundle has not been built there is nothing to mount, and nothing is: the API,
    # the probes and the OpenAPI docs all still serve, and "/" explains how to build it. A
    # developer who has not run `npm run build` gets a sentence, not a stack trace.
    # ------------------------------------------------------------------
    if dist is not None:
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")
        logger.info("console: serving %s", dist)
    else:
        logger.info("console: not built (looked for frontend/dist); serving the API only")

    @app.exception_handler(404)
    async def _spa_fallback(request: Request, exc: Exception) -> Response:
        """Answer the console's client-side routes with the shell; leave the API alone.

        ``/analyze`` and friends exist only in the browser's router, so a deep link or a
        refresh arrives here as a 404 and has to be given ``index.html``. A 404 under an API
        prefix is a real API 404 and stays JSON — a caller parsing a response must never be
        handed a web page because it happened to ask for a doctype that is not installed.
        """
        path = request.url.path
        index = dist / "index.html" if dist is not None else None
        if (
            index is not None
            and request.method in ("GET", "HEAD")
            and not path.startswith(_API_PREFIXES)
        ):
            return FileResponse(index)
        if dist is None and path in _UI_ROUTES:
            return JSONResponse(_descriptor(), status_code=404)
        return JSONResponse({"detail": getattr(exc, "detail", "Not Found")}, status_code=404)

    return app


#: Module-level app for ``uvicorn dce.api.app:app``.
app = create_app()
