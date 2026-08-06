"""Classification: 100% in-process, by design and by construction.

Nothing in this package opens a socket. Anchors, checksums, structural priors and lexical
scoring are pure Python over the layout payload we were handed; the optional BERT tier reads a
checkpoint from a mounted directory and is imported only when it is switched on. That is not a
convention — :mod:`dce.egress` enforces it, and ``tests/test_egress.py`` proves a full
classification performs zero socket operations.

The reason is the whole reason this service exists: other business units send us documents
that have **not** been classified. Their bytes, their text, and any embedding of their text
must not leave this process before we know what the document is.

Import surface::

    from dce.classify import classify, classify_pages

Everything else here is for callers that want a single tier — a console showing why a document
scored the way it did, an offline calibration job, a registry linter.

Note that :mod:`dce.classify.bert_knn` is deliberately **not** imported here. It stays out of
``sys.modules`` unless ``bert_enabled`` is true.
"""
from __future__ import annotations

from .anchors import AnchorHit, AnchorOutcome, ChecksumHit, anchor_scores, checksum_sweep
from .cascade import Segment, classify, classify_pages, load_registry
from .lexical import (
    DocumentTerms,
    LexicalOutcome,
    PlattCalibration,
    document_terms,
    lexical_scores,
)
from .profiles import ProfileSet, TermProfile, build_profiles, fit_profiles
from .structural import StructuralFeatures, structural_features, structural_log_priors

__all__ = [
    "AnchorHit",
    "AnchorOutcome",
    "ChecksumHit",
    "DocumentTerms",
    "LexicalOutcome",
    "PlattCalibration",
    "ProfileSet",
    "Segment",
    "StructuralFeatures",
    "TermProfile",
    "anchor_scores",
    "build_profiles",
    "checksum_sweep",
    "classify",
    "classify_pages",
    "document_terms",
    "fit_profiles",
    "lexical_scores",
    "load_registry",
    "structural_features",
    "structural_log_priors",
]
