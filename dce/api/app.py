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
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: Settings to run with. Defaults to the process settings from the environment.
            Tests pass an explicit instance instead of mutating the environment.

    Returns:
        A configured :class:`fastapi.FastAPI` instance.
    """
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _assert_invariant(app.state.settings)
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

    @app.get("/", include_in_schema=False)
    async def _root() -> JSONResponse:
        return JSONResponse(
            {
                "service": SERVICE_NAME,
                "version": __version__,
                "docs": "/docs",
                "health": "/health",
                "readiness": "/readyz",
                "metrics": "/metrics",
                "api": "/api/v1",
            }
        )

    app.include_router(system_router)
    app.include_router(router)
    return app


#: Module-level app for ``uvicorn dce.api.app:app``.
app = create_app()
