"""Resolution: run a field's locators, score what they found, and pick the winner.

Locators propose; this module disposes. Everything that decides an outcome lives here so
that adding a locator cannot change how any existing field is resolved.

**Scoring.** ``score = locator_prior x locator_confidence x validation_factor``. The prior
says how much the *method* is trusted, the confidence is that locator's own belief in this
particular hit, and the factor is what the validator made of the value. A checksum that
passes is worth more than any amount of layout luck, and a validator that rejects the value
drops the candidate to the rejected pool.

**Early stop.** The moment a checksum-verified candidate appears, resolution of that field
stops. Nothing beats a value that carries its own proof, and continuing would only spend
time discovering that.

**Nothing is silently dropped.** A field whose every candidate failed validation still
reports its best rejected value, with ``validator_error`` populated, at a heavily
discounted confidence and with ``needs_review`` set. An empty field with no explanation is
useless to the human who has to look at it.
"""
from __future__ import annotations

import time

from dce.config import Settings, get_settings
from dce.extract import validate as V
from dce.extract.locators import LOCATOR_PRIOR, LOCATORS, Candidate, LocatorContext
from dce.extract.schema import DocSchema
from dce.models import DocTypeSpec, ExtractedField, ExtractionResult, FieldSpec, LayoutView

__all__ = ["ScoredCandidate", "extract", "resolve", "resolve_field"]

#: Validation outcome -> multiplier on the candidate's score. Note that clearing a real
#: validator is itself evidence, so ``_FACTOR_FORMAT`` sits *above* the implicit 1.0 a
#: field with no validator gets: "this parses as an address" beats "nobody checked".
_FACTOR_CHECKSUM = 1.25
_FACTOR_FORMAT = 1.10
_FACTOR_NO_VALIDATOR = 1.00
_FACTOR_SOFT = 0.85
#: Applied to a candidate every validator rejected. It is reported, never trusted.
_FACTOR_REJECTED = 0.30

#: A checksum-verified value is never reported below this, whatever the locator thought.
_CHECKSUM_CONFIDENCE_FLOOR = 0.90
_MAX_CONFIDENCE = 0.995
#: Cap on how many values a ``multi`` field reports.
_MULTI_LIMIT = 10

VERIFICATION_UNVERIFIED = "unverified"
VERIFICATION_FORMAT_VALID = "format_valid"
VERIFICATION_CHECKSUM_VERIFIED = "checksum_verified"


class ScoredCandidate:
    """A candidate with its validation outcome attached.

    Attributes:
        candidate: The proposal as the locator made it.
        score: Fused score used to rank.
        normalized: Canonical value from the validator, when there was one.
        verification: ``unverified`` | ``format_valid`` | ``checksum_verified``.
        error: Validator error message; a soft note when ``accepted`` is still True.
        accepted: Whether the validator allowed the value at all.
    """

    __slots__ = ("accepted", "candidate", "error", "normalized", "score", "verification")

    def __init__(
        self,
        candidate: Candidate,
        score: float,
        normalized: str,
        verification: str,
        error: str,
        accepted: bool,
    ) -> None:
        self.candidate = candidate
        self.score = score
        self.normalized = normalized
        self.verification = verification
        self.error = error
        self.accepted = accepted

    @property
    def is_checksum_verified(self) -> bool:
        return self.verification == VERIFICATION_CHECKSUM_VERIFIED


def resolve_field(
    field: FieldSpec, view: LayoutView, ctx: LocatorContext
) -> list[ExtractedField]:
    """Resolve one field to one value — or to several, when the field is ``multi``.

    Args:
        field: The field to resolve.
        view: The layout view to extract from.
        ctx: Locator context.

    Returns:
        One :class:`~dce.models.ExtractedField` (a list, so ``multi`` fields fit the same
        shape). Always non-empty: a field with no candidate is still reported, empty.
    """
    scored = _collect(field, view, ctx)
    if not scored:
        return [_empty(field)]

    accepted = [s for s in scored if s.accepted]
    if not accepted:
        # Everything was rejected. Surface the best rejected value so a reviewer can see
        # what was on the page and why it was not trusted, rather than a blank field.
        best = max(scored, key=lambda s: s.score)
        return [_to_field(field, best, rejected=True)]

    accepted.sort(key=lambda s: -s.score)
    if not field.multi:
        return [_to_field(field, accepted[0])]

    out: list[ExtractedField] = []
    seen: set[str] = set()
    for item in accepted:
        key = (item.normalized or item.candidate.value).casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(_to_field(field, item))
        if len(out) >= _MULTI_LIMIT:
            break
    return out


def _collect(field: FieldSpec, view: LayoutView, ctx: LocatorContext) -> list[ScoredCandidate]:
    """Run the field's locators in priority order, stopping at a checksum-verified hit."""
    scored: list[ScoredCandidate] = []
    for name in field.locators:
        locate = LOCATORS.get(name)
        if locate is None:
            continue
        try:
            candidates = locate(field, view, ctx)
        except (ValueError, TypeError, IndexError, KeyError):
            # One misbehaving locator must not cost us the other five.
            continue
        prior = LOCATOR_PRIOR.get(name, 0.5)
        stop = False
        for candidate in candidates:
            item = _score(field, candidate, prior, ctx)
            scored.append(item)
            if item.accepted and item.is_checksum_verified:
                stop = True
        if stop:
            break
    return scored


def _score(
    field: FieldSpec, candidate: Candidate, prior: float, ctx: LocatorContext
) -> ScoredCandidate:
    """Validate a candidate and fuse the locator prior, its confidence and the outcome."""
    if not field.validator:
        # No validator: the field's pattern (already enforced by the locator) is all the
        # structure we have — unless the locator proved the value itself, which is what a
        # verified MRZ does for every field it yields, names included.
        if candidate.verified:
            return ScoredCandidate(
                candidate,
                prior * candidate.confidence * _FACTOR_CHECKSUM,
                candidate.value,
                VERIFICATION_CHECKSUM_VERIFIED,
                "",
                True,
            )
        verification = (
            VERIFICATION_FORMAT_VALID if field.pattern else VERIFICATION_UNVERIFIED
        )
        score = prior * candidate.confidence * _FACTOR_NO_VALIDATOR
        return ScoredCandidate(candidate, score, candidate.value, verification, "", True)

    result = V.validate(field.validator, candidate.value, ctx.validation_context)
    level = V.verification_level(field.validator)
    if not result.ok:
        return ScoredCandidate(
            candidate,
            prior * candidate.confidence * _FACTOR_REJECTED,
            candidate.value,
            VERIFICATION_UNVERIFIED,
            result.error,
            False,
        )
    if result.error:
        # Soft failure: usable, flagged, and never promoted to checksum_verified.
        return ScoredCandidate(
            candidate,
            prior * candidate.confidence * _FACTOR_SOFT,
            result.normalized,
            VERIFICATION_FORMAT_VALID,
            result.error,
            True,
        )
    if level == "checksum" or candidate.verified:
        return ScoredCandidate(
            candidate,
            prior * candidate.confidence * _FACTOR_CHECKSUM,
            result.normalized,
            VERIFICATION_CHECKSUM_VERIFIED,
            "",
            True,
        )
    return ScoredCandidate(
        candidate,
        prior * candidate.confidence * _FACTOR_FORMAT,
        result.normalized,
        VERIFICATION_FORMAT_VALID,
        "",
        True,
    )


def _to_field(
    field: FieldSpec, scored: ScoredCandidate, *, rejected: bool = False
) -> ExtractedField:
    """Render a scored candidate as the reportable field."""
    confidence = min(scored.score, _MAX_CONFIDENCE)
    if scored.is_checksum_verified:
        confidence = max(confidence, _CHECKSUM_CONFIDENCE_FLOOR)
    return ExtractedField(
        name=field.name,
        attribute_key=field.attribute_key,
        value=scored.candidate.value,
        normalized=scored.normalized or scored.candidate.value,
        confidence=round(confidence, 4),
        verification=VERIFICATION_UNVERIFIED if rejected else scored.verification,
        locator=scored.candidate.locator,
        page=scored.candidate.page,
        bbox=scored.candidate.bbox,
        pii=field.pii,
        validator_error=scored.error,
    )


def _empty(field: FieldSpec) -> ExtractedField:
    """A field nothing was found for — reported, not omitted."""
    return ExtractedField(
        name=field.name,
        attribute_key=field.attribute_key,
        value=None,
        confidence=0.0,
        verification=VERIFICATION_UNVERIFIED,
        pii=field.pii,
        validator_error="no_candidate_found",
    )


def resolve(
    view: LayoutView,
    schema: DocSchema,
    *,
    ctx: LocatorContext | None = None,
    spec: DocTypeSpec | None = None,
    settings: Settings | None = None,
) -> ExtractionResult:
    """Extract every field in a schema from a layout view.

    Args:
        view: The layout view to extract from.
        schema: The doctype's extraction contract. Must be active — an induced draft is
            a proposal, not something to run.
        ctx: Locator context; built from the view and spec when omitted.
        spec: The accepted doctype's spec, used to build the context.
        settings: Override process settings.

    Returns:
        An :class:`~dce.models.ExtractionResult` with per-field provenance,
        ``missing_required`` and ``needs_review`` populated.

    Raises:
        ValueError: When handed an inactive schema.
    """
    if not schema.active:
        raise ValueError(
            f"schema {schema.doctype_id} v{schema.version} is inactive "
            "(induced schemas must be activated by a human before use)"
        )
    started = time.perf_counter()
    conf = settings or get_settings()
    if ctx is None:
        ctx = LocatorContext.for_view(
            view, spec=spec, settings=conf, doctype_id=schema.doctype_id
        )

    fields: list[ExtractedField] = []
    for field_spec in schema.fields:
        fields.extend(resolve_field(field_spec, view, ctx))

    missing = sorted(
        {
            spec_field.name
            for spec_field in schema.fields
            if spec_field.required
            and not any(f.name == spec_field.name and f.value for f in fields)
        }
    )
    needs_review = bool(missing) or any(
        (f.value and f.confidence < conf.extract_accept_confidence)
        or (f.value and f.validator_error)
        for f in fields
    )
    return ExtractionResult(
        doctype_id=schema.doctype_id,
        schema_version=schema.version,
        fields=fields,
        missing_required=missing,
        needs_review=needs_review,
        ms=int((time.perf_counter() - started) * 1000),
    )


def extract(
    view: LayoutView,
    doctype: str | DocTypeSpec,
    *,
    version: str = "latest",
    schema_version: str | None = None,
    spec: DocTypeSpec | None = None,
    settings: Settings | None = None,
) -> ExtractionResult:
    """The extraction tier's public entry point: look the schema up, then resolve.

    Falls back to deriving a schema from the doctype spec when none is registered, so a
    freshly declared doctype extracts without a separate registration step.

    ``doctype`` accepts either a doctype id or the :class:`~dce.models.DocTypeSpec` itself,
    because the API layer already holds the spec by the time it gets here — the
    classification step it just ran produced it — and making it look the id back up would
    be a round trip for nothing. ``schema_version`` is accepted as an alias for ``version``
    for the same reason: it is the name the request field carries.

    Args:
        view: The layout view to extract from.
        doctype: The accepted document type, as an id or a spec.
        version: Schema version, or ``"latest"``.
        schema_version: Alias for ``version``; wins when both are given.
        spec: The doctype spec, when ``doctype`` was passed as an id.
        settings: Override process settings.

    Returns:
        The extraction result; an empty one, flagged for review, when no schema can be
        found or derived for the doctype.
    """
    from dce.extract.schema import get_schema, load_from_registry

    if isinstance(doctype, DocTypeSpec):
        spec = spec or doctype
        doctype_id = doctype.doctype_id
    else:
        doctype_id = str(doctype)
    wanted = schema_version or version

    schema = get_schema(doctype_id, wanted)
    if schema is None:
        schema = load_from_registry(doctype_id, spec=spec)
    if schema is None:
        return ExtractionResult(doctype_id=doctype_id, needs_review=True)
    return resolve(view, schema, spec=spec, settings=settings)
