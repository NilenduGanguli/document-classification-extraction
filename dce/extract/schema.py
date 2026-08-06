"""Document schemas: what a doctype's extraction contract is, and how it may change.

A :class:`DocSchema` is the versioned list of fields to pull out of one document type. The
registry that holds them enforces one rule, and it is the rule that keeps downstream
consumers from breaking silently:

**Within a version, schemas are additive-only.** Adding a field to an existing version is
fine — nobody's stored data becomes wrong. Changing an existing field's ``type`` or
``validator``, or removing it, is not: a consumer that parsed ``date_of_birth`` as a date
would start receiving something else under a version string it has already cached. Those
changes require a **new version**, which is exactly what a version is for.

Declaring a doctype should also give you a working schema for free: :func:`load_from_registry`
derives the default schema from a :class:`~dce.models.DocTypeSpec`'s own ``fields``, so the
common case needs no separate schema declaration at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field

from pydantic import BaseModel, Field

from dce.models import DocTypeSpec, FieldSpec

__all__ = [
    "DEFAULT_VERSION",
    "DocSchema",
    "SchemaCompatibilityError",
    "SchemaRegistry",
    "check_additive",
    "default_schema_for",
    "get_schema",
    "load_from_registry",
    "register_schema",
    "registry",
]

DEFAULT_VERSION = "1"

#: Attributes of a field that downstream consumers bind to. Changing any of them within a
#: version is a breaking change; adding a whole new field is not.
_BREAKING_ATTRS = ("type", "validator", "multi")


class SchemaCompatibilityError(ValueError):
    """Raised when a re-registration would break an already-published version."""


class DocSchema(BaseModel):
    """A versioned extraction contract for one document type."""

    doctype_id: str
    version: str = DEFAULT_VERSION
    fields: list[FieldSpec] = Field(default_factory=list)
    #: Induced schemas start inactive and are invisible to ``get()`` until a human says so.
    active: bool = True
    source: str = "declared"          # declared | derived | induced
    notes: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.doctype_id, self.version

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def field(self, name: str) -> FieldSpec | None:
        """Return the named field, or ``None``."""
        for spec in self.fields:
            if spec.name == name:
                return spec
        return None

    @property
    def required_fields(self) -> list[str]:
        return [f.name for f in self.fields if f.required]


def check_additive(old: DocSchema, new: DocSchema) -> list[str]:
    """Return the reasons ``new`` is not an additive successor of ``old``.

    Args:
        old: The schema already registered under this version.
        new: The schema being registered over it.

    Returns:
        A list of human-readable violations; empty when the change is purely additive.
    """
    violations: list[str] = []
    new_by_name = {f.name: f for f in new.fields}
    for existing in old.fields:
        replacement = new_by_name.get(existing.name)
        if replacement is None:
            violations.append(f"field {existing.name!r} was removed")
            continue
        for attr in _BREAKING_ATTRS:
            before, after = getattr(existing, attr), getattr(replacement, attr)
            if before != after:
                violations.append(
                    f"field {existing.name!r} changed {attr}: {before!r} -> {after!r}"
                )
        if existing.required and not replacement.required:
            # Relaxing `required` is safe for consumers but changes the review queue, so
            # it is reported rather than silently allowed.
            violations.append(f"field {existing.name!r} stopped being required")
    return violations


@dataclass
class SchemaRegistry:
    """In-memory schema store keyed by ``(doctype_id, version)``.

    Deliberately a plain object rather than a module global with hidden state: tests build
    their own, and the service builds one at startup.
    """

    _schemas: dict[tuple[str, str], DocSchema] = dc_field(default_factory=dict)

    def register(self, schema: DocSchema) -> DocSchema:
        """Store a schema, enforcing the additive-only rule within a version.

        Args:
            schema: The schema to store.

        Returns:
            The stored schema.

        Raises:
            SchemaCompatibilityError: When a schema already exists under this
                ``(doctype_id, version)`` and the new one is not a purely additive
                superset of it.
        """
        existing = self._schemas.get(schema.key)
        if existing is not None:
            violations = check_additive(existing, schema)
            if violations:
                raise SchemaCompatibilityError(
                    f"{schema.doctype_id} v{schema.version} is not an additive change to the "
                    f"registered v{schema.version}: " + "; ".join(violations)
                    + f". Register these under a new version (e.g. "
                    f"{_next_version(schema.version)}) instead."
                )
        self._schemas[schema.key] = schema
        return schema

    def get(
        self, doctype_id: str, version: str = "latest", *, include_inactive: bool = False
    ) -> DocSchema | None:
        """Look up a schema.

        Args:
            doctype_id: The document type.
            version: An exact version, or ``"latest"``.
            include_inactive: Include draft/induced schemas. Off by default — an induced
                schema must be activated by a human before anything extracts with it.

        Returns:
            The schema, or ``None``.
        """
        if version != "latest":
            found = self._schemas.get((doctype_id, version))
            if found is None or (not found.active and not include_inactive):
                return None
            return found
        versions = self.versions(doctype_id, include_inactive=include_inactive)
        if not versions:
            return None
        return self._schemas[(doctype_id, versions[-1])]

    def versions(self, doctype_id: str, *, include_inactive: bool = False) -> list[str]:
        """Every registered version of a doctype, oldest first."""
        found = [
            schema.version
            for (did, _v), schema in self._schemas.items()
            if did == doctype_id and (schema.active or include_inactive)
        ]
        return sorted(found, key=_version_key)

    def doctypes(self) -> list[str]:
        """Every doctype id with at least one registered schema."""
        return sorted({did for did, _v in self._schemas})

    def activate(self, doctype_id: str, version: str) -> DocSchema:
        """Mark a draft schema active — the deliberate human step after induction.

        Args:
            doctype_id: The document type.
            version: The exact draft version to activate.

        Returns:
            The activated schema.

        Raises:
            KeyError: When no such schema is registered.
        """
        schema = self._schemas.get((doctype_id, version))
        if schema is None:
            raise KeyError(f"no schema {doctype_id} v{version}")
        activated = schema.model_copy(update={"active": True})
        self._schemas[activated.key] = activated
        return activated

    def clear(self) -> None:
        """Drop everything (tests)."""
        self._schemas.clear()

    def all(self) -> list[DocSchema]:
        """Every registered schema, ordered by doctype then version."""
        return sorted(
            self._schemas.values(), key=lambda s: (s.doctype_id, _version_key(s.version))
        )


def _version_key(version: str) -> tuple[int, ...]:
    """Sort key for a version string.

    Dotted-integer versions sort numerically ("2" after "1.9"); anything else sorts after
    them by code point so a non-numeric scheme still gets a stable, total order.
    """
    parts = re.findall(r"\d+", version or "")
    if parts:
        return (0, *[int(p) for p in parts])
    return (1, *[ord(ch) for ch in (version or "")])


def _next_version(version: str) -> str:
    """Suggest the next version string, for the compatibility error message."""
    parts = re.findall(r"\d+", version or "")
    if not parts:
        return f"{version}-v2"
    head = int(parts[0])
    return re.sub(r"\d+", str(head + 1), version, count=1)


#: Process-wide registry. Callers that need isolation build their own SchemaRegistry.
registry = SchemaRegistry()


def register_schema(schema: DocSchema) -> DocSchema:
    """Register a schema in the process-wide :data:`registry`."""
    return registry.register(schema)


def get_schema(doctype_id: str, version: str = "latest") -> DocSchema | None:
    """Look a schema up in the process-wide :data:`registry`."""
    return registry.get(doctype_id, version)


def default_schema_for(spec: DocTypeSpec, *, version: str = DEFAULT_VERSION) -> DocSchema:
    """Derive a schema from a doctype's own declared fields.

    This is what makes declaring a doctype sufficient: the knowledge of what an Aadhaar
    card contains already lives on :class:`~dce.models.DocTypeSpec`, and duplicating it in
    a separate schema file is how the two drift apart.

    Args:
        spec: The doctype specification.
        version: Version to stamp on the derived schema.

    Returns:
        A ``source="derived"`` schema. Empty ``fields`` is legal — a doctype may be
        classifiable without being extractable.
    """
    return DocSchema(
        doctype_id=spec.doctype_id,
        version=version,
        fields=[f.model_copy(deep=True) for f in spec.fields],
        active=True,
        source="derived",
        notes=f"derived from DocTypeSpec {spec.doctype_id!r} ({spec.label})",
    )


def load_from_registry(
    doctype_id: str,
    *,
    version: str = DEFAULT_VERSION,
    spec: DocTypeSpec | None = None,
    doctype_registry: object | None = None,
) -> DocSchema | None:
    """Build the default schema for a doctype from the doctype registry.

    Args:
        doctype_id: The document type to look up.
        version: Version to stamp on the derived schema.
        spec: Supply the spec directly and skip the lookup entirely (tests, and callers
            that already resolved it during classification).
        doctype_registry: Override the module consulted for the lookup.

    Returns:
        The derived schema, or ``None`` when the doctype is unknown.
    """
    if spec is None:
        spec = _lookup_spec(doctype_id, doctype_registry)
    if spec is None:
        return None
    return default_schema_for(spec, version=version)


def _lookup_spec(doctype_id: str, source: object | None = None) -> DocTypeSpec | None:
    """Find a DocTypeSpec in the doctype registry without hard-coupling to its shape.

    The doctype registry is a sibling module owned by another part of the service and is
    imported lazily: extraction must remain importable, and testable, on its own.
    """
    if source is None:
        try:
            from dce import registry as source  # type: ignore[no-redef]
        except ImportError:
            return None
    for accessor in ("get_doctype", "get_spec", "get", "by_id", "lookup"):
        fn = getattr(source, accessor, None)
        if callable(fn):
            try:
                found = fn(doctype_id)
            except (KeyError, LookupError, TypeError):
                continue
            if isinstance(found, DocTypeSpec):
                return found
    for attr in ("DOCTYPES", "DOC_TYPES", "SPECS", "REGISTRY"):
        table = getattr(source, attr, None)
        if isinstance(table, dict):
            found = table.get(doctype_id)
            if isinstance(found, DocTypeSpec):
                return found
        elif isinstance(table, (list, tuple)):
            for item in table:
                if isinstance(item, DocTypeSpec) and item.doctype_id == doctype_id:
                    return item
    return None
