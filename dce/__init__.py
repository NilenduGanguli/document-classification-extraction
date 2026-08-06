"""Document Classification & Extraction (DCE).

A standalone service that answers two questions about a document nobody has classified yet:
*what is this?* and *what does it say?* — and answers the first one **without letting the
document leave the process**.

Other business units hand this service bytes they have not looked at. Sending those bytes,
their text, or an embedding of their text to a third party before the document type is known
is the exact failure the service exists to prevent, so the classification cascade
(structural prior -> anchors/checksums -> zone-weighted BM25 -> optional *local* BERT) is
100% in-process. When it cannot decide, it abstains to ``unknown`` and routes to a human —
never to a remote model. See :mod:`dce.config` for the invariant and ``docs/DESIGN.md`` for
the reasoning.

Public surface:

* :mod:`dce.models` — the value types every module codes against.
* :mod:`dce.adapters` — provider payload (Azure Layout / DES OCR / plain text) -> LayoutView.
* :mod:`dce.api` — the FastAPI application.
* :mod:`dce.observability` — Prometheus metrics and the readiness registry.
"""
from __future__ import annotations

__version__ = "0.1.0"
SERVICE_NAME = "document-classification-extraction"

__all__ = ["SERVICE_NAME", "__version__"]
