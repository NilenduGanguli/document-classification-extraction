"""HTTP layer: the FastAPI app factory and its routers.

``create_app`` is the only thing most callers need; ``router`` (``/api/v1``) and
``system_router`` (``/health``, ``/readyz``, ``/metrics``) are exported so another service can
mount this one instead of running it standalone.
"""
from __future__ import annotations

from dce.api.app import app, create_app
from dce.api.routes import router, system_router

__all__ = ["app", "create_app", "router", "system_router"]
