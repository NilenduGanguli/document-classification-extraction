"""Extraction — runs only after a document type has been accepted.

The classifier decides *what* a document is, entirely in-process. Only then does anything
here run, against a schema chosen for that doctype. In this build the resolver is local and
deterministic: locators propose values, validators decide whether they are real, and every
reported field carries the locator, page and bbox that produced it.

Public surface::

    from dce.extract import extract, resolve, validate, DocSchema, induce_schema

* :func:`~dce.extract.resolve.extract` — doctype id in, :class:`~dce.models.ExtractionResult` out.
* :func:`~dce.extract.resolve.resolve` — same, with an explicit schema.
* :mod:`dce.extract.validate` — the validator registry, stdlib-only and import-safe, also
  used by the classifier's pre-classification checksum sweep.
* :mod:`dce.extract.schema` — versioned, additive-only extraction contracts.
* :func:`~dce.extract.induce.induce_schema` — propose a draft schema for an unseen doctype.
"""
from __future__ import annotations

from dce.extract import induce, locators, schema, validate
from dce.extract.induce import induce_schema
from dce.extract.locators import LOCATORS, Candidate, LocatorContext
from dce.extract.resolve import extract, resolve, resolve_field
from dce.extract.schema import (
    DocSchema,
    SchemaCompatibilityError,
    SchemaRegistry,
    default_schema_for,
    get_schema,
    load_from_registry,
    register_schema,
    registry,
)
from dce.extract.validate import ValidationResult, sweep

__all__ = [
    "LOCATORS",
    "Candidate",
    "DocSchema",
    "LocatorContext",
    "SchemaCompatibilityError",
    "SchemaRegistry",
    "ValidationResult",
    "default_schema_for",
    "extract",
    "get_schema",
    "induce",
    "induce_schema",
    "load_from_registry",
    "locators",
    "register_schema",
    "registry",
    "resolve",
    "resolve_field",
    "schema",
    "sweep",
    "validate",
]
