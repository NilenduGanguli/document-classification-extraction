"""Resolution: run a field's locators, score what they found, and pick the winner.

Locators propose; this module disposes. Everything that decides an outcome lives here so
that adding a locator cannot change how any existing field is resolved.

**Scoring.** ``score = locator_prior x locator_confidence x validation_factor``. The prior
says how much the *method* is trusted, the confidence is that locator's own belief in this
particular hit, and the factor is what the validator made of the value. A checksum that
passes is worth more than any amount of layout luck, and a validator that rejects the value
drops the candidate to the rejected pool.

Tightness rides in on the middle term: a locator that had to cut a span short of the next
label, or narrow it to the field's shape, discounts its own confidence (see
:mod:`dce.extract.locators.trim`). What that buys is the ordering that matters —
**a candidate the validator accepted beats one it did not, however clean the span was, and
between two the validator accepted the tighter span wins.** The multiplication does both;
the tie-break below makes the second half deterministic when the scores land equal.

**Verification comes from the validator, never from the locator.** The one exception is a
locator that carries its own proof — an MRZ whose ICAO check digits cover every field it
yields — which sets ``Candidate.verified`` and says so. A pattern the locator matched is not
a validation: it is how the locator found the value in the first place, and reporting it as
``format_valid`` would tell a reviewer that something checked the value when nothing did.

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
from dce.extract.locators import LOCATOR_PRIOR, LOCATORS, Candidate, LocatorContext, trim
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

#: Used only to break a tie between candidates whose fused scores are equal.
_VERIFICATION_RANK = {
    VERIFICATION_UNVERIFIED: 0,
    VERIFICATION_FORMAT_VALID: 1,
    VERIFICATION_CHECKSUM_VERIFIED: 2,
}


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

    # A shaped field only ever reports a value of its shape.
    #
    # ``date``, ``number`` and ``id`` fields — and any field declaring a pattern or a
    # validator with a known one — describe values a machine can recognise. A candidate
    # with no instance of that shape anywhere in it is not a poor reading of the value, it
    # is a different thing entirely: a sentence of form instructions that a caption matched
    # a word inside, a heading, a bare bullet. Reporting it, even flagged, puts prose in a
    # KYC record under a field name, where everything downstream reads it as the value.
    #
    # This is a decision, not a proposal, so it is made here and not in the locator. What it
    # does *not* do is second-guess a validator: a UID whose Verhoeff digit fails has the
    # shape, survives this filter, and still reaches the reviewer exactly as printed.
    shaped = [s for s in scored if trim.has_type_shape(field, s.candidate.value)]
    if not shaped:
        return [_empty(field, error="no_candidate_of_this_type")]
    scored = shaped

    accepted = [s for s in scored if s.accepted]
    if not accepted:
        # Everything was rejected. Surface the best rejected value so a reviewer can see
        # what was on the page and why it was not trusted, rather than a blank field.
        best = min(scored, key=_rank)
        return [_to_field(field, best, rejected=True)]

    accepted.sort(key=_rank)
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


def _rank(item: ScoredCandidate) -> tuple[float, int, bool, str]:
    """Sort key, best first.

    The fused score decides; everything after it exists so that two candidates which scored
    the same are still ordered by something meaningful rather than by dict iteration. A
    value the validator *verified* comes first, then one it accepted without a note, and the
    locator name only ever breaks a total tie so the result is reproducible.
    """
    return (
        -item.score,
        -_VERIFICATION_RANK.get(item.verification, 0),
        bool(item.error),
        item.candidate.locator,
    )


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
        # No validator: nothing checked this value, so nothing may claim it was checked.
        # The field's pattern does not count — the locator matched it to *find* the value,
        # which makes it a search key rather than a verification. The one thing that does
        # count is a locator carrying its own proof: a verified MRZ's check digits cover
        # every field it yields, names included.
        if candidate.verified:
            return ScoredCandidate(
                candidate,
                prior * candidate.confidence * _FACTOR_CHECKSUM,
                candidate.value,
                VERIFICATION_CHECKSUM_VERIFIED,
                "",
                True,
            )
        score = prior * candidate.confidence * _FACTOR_NO_VALIDATOR
        return ScoredCandidate(
            candidate, score, candidate.value, VERIFICATION_UNVERIFIED, "", True
        )

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


#: What a ``type="bool"`` field may legitimately carry. ``mark`` already emits the first two;
#: the rest is what an issuer prints when it states the fact in words, plus AAMVA element
#: ``DDA``'s published ``F``/``N``. Anything else is not a boolean.
_BOOL_TRUE = frozenset({"true", "yes", "y", "checked", "x", "f", "compliant"})
_BOOL_FALSE = frozenset({"false", "no", "n", "unchecked", "not checked", "none"})


def _coerce_bool(value: str) -> str | None:
    """A boolean field's value, or ``None`` when the candidate is not a boolean at all.

    The ``mark`` locator understands ``type="bool"`` and emits ``"true"``/``"false"`` from a
    selection mark. ``label`` and ``regex`` do not — they return *the text following the
    label*, which for a boolean caption is whatever the issuer happened to print next.

    Measured on the Virginia DMV AAMVA calibration sheet, ``real_id_compliant`` (the
    registry's only boolean field) came back as ``"Driver's License - Over 21"`` — a fragment
    of the sheet's own title line — at confidence 0.697 with no validator complaint. A field
    declared ``bool`` reported a string, and a reviewer reading it as a compliance
    determination would have been reading the document's name.

    Rejecting rather than guessing is the direction this service takes everywhere: an empty
    field routes to a human, a wrong one gets acted on. It also keeps the promise the field's
    own spec makes — *never report "not REAL ID" from a blank result* — because a rejected
    candidate yields no value, not ``false``.
    """
    text = value.strip().lower().strip(".:;")
    if text in _BOOL_TRUE:
        return "true"
    if text in _BOOL_FALSE:
        return "false"
    return None


def _to_field(
    field: FieldSpec, scored: ScoredCandidate, *, rejected: bool = False
) -> ExtractedField:
    """Render a scored candidate as the reportable field."""
    confidence = min(scored.score, _MAX_CONFIDENCE)
    if scored.is_checksum_verified:
        confidence = max(confidence, _CHECKSUM_CONFIDENCE_FLOOR)
    if field.type == "bool":
        coerced = _coerce_bool(scored.candidate.value)
        if coerced is None:
            return ExtractedField(
                name=field.name,
                attribute_key=field.attribute_key,
                value=None,
                normalized=None,
                confidence=0.0,
                verification=VERIFICATION_UNVERIFIED,
                locator=scored.candidate.locator,
                page=scored.candidate.page,
                bbox=scored.candidate.bbox,
                pii=field.pii,
                validator_error=(
                    f"not a boolean: {scored.candidate.value[:60]!r} — a bool field "
                    "reports true/false or nothing"
                ),
            )
        return ExtractedField(
            name=field.name,
            attribute_key=field.attribute_key,
            value=coerced,
            normalized=coerced,
            confidence=round(confidence, 4),
            verification=VERIFICATION_UNVERIFIED if rejected else scored.verification,
            locator=scored.candidate.locator,
            page=scored.candidate.page,
            bbox=scored.candidate.bbox,
            pii=field.pii,
            validator_error=scored.error,
        )
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


def _as_spec(schema: DocSchema) -> DocTypeSpec | None:
    """Wrap the active schema so the locators can see the fields *other than* the one
    being resolved.

    Knowing where a value ends means knowing every caption printed on the document, and the
    only place that list exists is the schema. A caller that already holds the
    :class:`~dce.models.DocTypeSpec` passes it; a caller that registered a schema on its own
    would otherwise leave every locator guessing, and the value bound to
    ``Signature of U.S. person`` would run on through ``Date:`` and take the next field's
    value with it.

    Carries no ``id_patterns`` deliberately: those say which identifiers make the *doctype*
    recognisable, a schema does not have them, and inventing some here would change what the
    regex locator is allowed to borrow.
    """
    if not schema.fields:
        return None
    return DocTypeSpec(
        doctype_id=schema.doctype_id,
        label=schema.doctype_id,
        country="XX",
        fields=list(schema.fields),
    )


def _empty(field: FieldSpec, *, error: str = "no_candidate_found") -> ExtractedField:
    """A field nothing was found for — reported, not omitted.

    ``error`` distinguishes "nothing on the page matched" from "something matched and was
    not of this field's kind", because they send a reviewer to different places.
    """
    return ExtractedField(
        name=field.name,
        attribute_key=field.attribute_key,
        value=None,
        confidence=0.0,
        verification=VERIFICATION_UNVERIFIED,
        pii=field.pii,
        validator_error=error,
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
            view, spec=spec or _as_spec(schema), settings=conf, doctype_id=schema.doctype_id
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
