"""T4 — the constrained LLM tier. The last automated attempt before a human.

T1 (local locators), T2 (Azure prebuilt specialists) and T3 (Azure query fields) have all
declined to produce a value. T4 asks a language model, and it is built so that the answer is
*evidence*, not prose. Four constraints, each one enforced in code below and pinned by a test:

**1. It refuses to run on an unclassified document.** The tier opens a
:func:`~dce.egress.post_classification_scope`, which raises :class:`~dce.egress.EgressViolation`
for ``unknown``. An abstention goes to the review queue; "ask the model what this is" is
pre-classification egress wearing a different hat.

**2. It is off by default.** ``settings.t4_enabled`` is read with a ``getattr`` default of
``False``, so a deployment that wants zero egress achieves it by doing nothing. The HTTP client
is imported lazily *inside* the call — the base image deliberately ships none (see
``pyproject.toml``), so an image built without one cannot make this call even if the flag were
flipped.

**3. It sends a window, not the document.** Only the pages and blocks that plausibly carry the
missing fields are put in the prompt. A KYC document is somebody's identity; "we sent the whole
file to a model to find one date of birth" is not a sentence to say in a control review.

**4. Every value is span-grounded, or it is thrown away.** The response is constrained to a
JSON Schema built from the :class:`~dce.models.FieldSpec` list — never free-form text — and
each field must come back with the exact substring the model read. That substring is then
checked against the window we actually sent. A value that cannot be located is **discarded**,
not returned at a lower confidence: an ungrounded field is a hallucination with a confidence
score attached, and the grounding check is the only thing that separates the two.

Grounding also buys provenance for free. The block the quote was found in supplies ``page`` and
``bbox``, so a T4 field is reviewable in the same UI as a T1 field — which is the standard every
field in this service is held to.

**Verification ladder.** A T4 value starts at ``unverified`` and is only promoted when a real
validator agrees; a published check digit still earns ``checksum_verified``, because the check
digit is computed here, in-process, over digits that provably appear on the page. Confidence is
capped below the local resolver's checksum floor either way, so a T1 value always outranks a T4
one for the same field.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from dce.egress import post_classification_scope
from dce.extract import validate as V
from dce.extract.locators.base import label_similarity
from dce.models import ExtractedField, FieldSpec, LayoutView, Quad, TextBlock, Zone

__all__ = [
    "LOCATOR_NAME",
    "Window",
    "build_json_schema",
    "build_request",
    "build_window",
    "extract_fields_llm",
    "ground_fields",
]

logger = logging.getLogger(__name__)

#: Provenance stamped on every field this tier produces. A reviewer filtering on it sees
#: exactly which values came from a model.
LOCATOR_NAME = "llm"

#: Label-similarity floor for pulling a block into the window. Deliberately looser than the
#: locator's ``fuzzy_label_min_score`` (88): here we are choosing *what to show the model*, and
#: a near-miss label is worth including as context. Nothing is bound at this score.
_WINDOW_LABEL_MIN = 70.0
#: Blocks either side of a matching block that come along as context — a value often sits on
#: the line after its label, and a lone label line answers nothing.
_WINDOW_CONTEXT_BLOCKS = 2
#: Fallback window size when nothing matched, and the default cap on the whole window.
_DEFAULT_MAX_WINDOW_CHARS = 6000
_DEFAULT_TIMEOUT_SECONDS = 20.0

#: Confidence ladder for a grounded value. All of these sit below :mod:`dce.extract.resolve`'s
#: ``_CHECKSUM_CONFIDENCE_FLOOR`` (0.90) on purpose: a local locator that found the same value
#: must always win the field, because it did not need a third party to do it.
#: ``_CONFIDENCE_REJECTED`` mirrors the resolver's ``_FACTOR_REJECTED``: a value the validator
#: threw out is still *reported* — it is on the page, and the reviewer should see what the
#: extractor saw — but it is never trusted.
_CONFIDENCE_REJECTED = 0.30
_CONFIDENCE_GROUNDED = 0.45
_CONFIDENCE_SOFT = 0.55
_CONFIDENCE_FORMAT = 0.65
_CONFIDENCE_CHECKSUM = 0.80

VERIFICATION_UNVERIFIED = "unverified"
VERIFICATION_FORMAT_VALID = "format_valid"
VERIFICATION_CHECKSUM_VERIFIED = "checksum_verified"

_SYSTEM_PROMPT = (
    "You read a fragment of an already-identified document and report values that are "
    "literally printed in it.\n"
    "Rules:\n"
    "1. Copy values verbatim from the fragment. Never translate, reformat, expand or correct "
    "them, and never infer a value from world knowledge.\n"
    "2. For every field you report, also return `quote`: the exact contiguous substring of the "
    "fragment you read the value from, copied character for character, including the label if "
    "it is on the same line. `value` must appear inside `quote`.\n"
    "3. If a field is not present in the fragment, return null for it. A null is a correct, "
    "useful answer; a guess is not, and will be discarded.\n"
    "4. Return only JSON matching the supplied schema."
)


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Window:
    """The slice of the document that is allowed into the prompt.

    Attributes:
        blocks: The text blocks sent, in reading order.
        pages: 1-based page numbers the blocks came from.
        reason: How the window was chosen — quoted in logs so an auditor can see whether a
            call was label-targeted or a first-page fallback.
        truncated: Whether the character cap dropped trailing blocks.
    """

    blocks: tuple[TextBlock, ...] = ()
    pages: tuple[int, ...] = ()
    reason: str = ""
    truncated: bool = False

    def __bool__(self) -> bool:
        return bool(self.blocks)

    @property
    def text(self) -> str:
        """The window as one string — this is what grounding is checked against."""
        return "\n".join(b.text for b in self.blocks if b.text)

    @property
    def prompt_text(self) -> str:
        """The window with page markers, as the model sees it."""
        out: list[str] = []
        current: int | None = None
        for block in self.blocks:
            if block.page != current:
                current = block.page
                out.append(f"--- page {block.page} ---")
            out.append(block.text)
        return "\n".join(out)

    @property
    def char_count(self) -> int:
        return len(self.text)


def _labels_of(field: FieldSpec) -> list[str]:
    """Every string worth matching a block against for this field.

    Language is ignored here on purpose: the window is a superset, and a Spanish label on a
    bilingual INE card should pull its page in even when the document reported ``en``.
    """
    labels = [label for group in field.labels.values() for label in group if label.strip()]
    fallback = field.name.replace("_", " ").strip()
    if fallback:
        labels.append(fallback)
    return labels


def _block_matches(field: FieldSpec, block: TextBlock) -> bool:
    """Whether this block plausibly carries this field's label or value shape."""
    text = block.text or ""
    if not text.strip():
        return False
    if field.pattern:
        try:
            if re.search(field.pattern, text, flags=re.IGNORECASE):
                return True
        except re.error:
            pass  # a broken declaration must not decide the window
    return any(label_similarity(label, text) >= _WINDOW_LABEL_MIN for label in _labels_of(field))


def build_window(
    missing: list[FieldSpec], view: LayoutView, *, settings: Any = None
) -> Window:
    """Choose the pages and blocks plausibly containing ``missing``.

    A block is a hit when it reads like one of the fields' labels or matches its value shape.
    Hits bring their neighbours (a value often sits on the line *after* its label) and their
    page's title/heading blocks (which tell the model what it is looking at) — but only pages
    that scored anything at all are sent.

    When nothing matches, the window falls back to the **first page only**. That keeps the
    contract honest: a fallback is still a window, never the whole document. Fields that live
    on page 5 of an unlabelled document then come back null, which is the safe failure.

    Args:
        missing: The fields T1-T3 could not resolve.
        view: The classified document's layout.
        settings: Read for ``llm_max_window_chars`` (``getattr``, default 6000).

    Returns:
        The :class:`Window`; falsy when the document had no text at all.
    """
    max_chars = int(getattr(settings, "llm_max_window_chars", _DEFAULT_MAX_WINDOW_CHARS) or 0)
    if max_chars <= 0:
        max_chars = _DEFAULT_MAX_WINDOW_CHARS

    blocks = [b for b in view.blocks if (b.text or "").strip()]
    if not blocks:
        return Window(reason="no_text_blocks")

    hits = {
        i for i, block in enumerate(blocks) if any(_block_matches(f, block) for f in missing)
    }
    if hits:
        pages = {blocks[i].page for i in hits}
        keep = set(hits)
        for i in sorted(hits):
            for j in range(i - _WINDOW_CONTEXT_BLOCKS, i + _WINDOW_CONTEXT_BLOCKS + 1):
                if 0 <= j < len(blocks) and blocks[j].page == blocks[i].page:
                    keep.add(j)
        for i, block in enumerate(blocks):
            if block.page in pages and block.zone in (Zone.title, Zone.heading):
                keep.add(i)
        reason = f"label_or_pattern_match on page(s) {sorted(pages)}"
        selected = [blocks[i] for i in sorted(keep)]
    else:
        first_page = min(b.page for b in blocks)
        selected = [b for b in blocks if b.page == first_page]
        reason = f"no_label_match:fallback_to_first_page({first_page})"

    kept: list[TextBlock] = []
    used = 0
    truncated = False
    for block in selected:
        cost = len(block.text) + 1
        if used + cost > max_chars and kept:
            truncated = True
            break
        kept.append(block)
        used += cost
    return Window(
        blocks=tuple(kept),
        pages=tuple(sorted({b.page for b in kept})),
        reason=reason,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# The constrained response contract
# ---------------------------------------------------------------------------
def build_json_schema(missing: list[FieldSpec], *, doctype_id: str = "") -> dict[str, Any]:
    """Build the JSON Schema the model's response is constrained to.

    One nullable object per field — ``{value, quote, page}`` — with ``additionalProperties``
    false and every property required, which is the shape OpenAI-compatible ``strict`` schema
    decoding demands. ``quote`` is not decoration: it is the thing :func:`ground_fields`
    verifies against the window, so it is required at the protocol level rather than asked for
    politely in the prompt.

    A field's ``pattern`` is passed as a *description*, not as a schema ``pattern`` keyword.
    Constrained decoders vary in which regex dialect they accept, and a rejected request would
    take out the whole tier for one over-specific field declaration.

    Args:
        missing: The fields to ask about. ``multi`` fields still yield at most one value —
            T4 answers "is this on the page", not "enumerate every instance".
        doctype_id: Used only to name the schema.

    Returns:
        A JSON Schema object.
    """
    properties: dict[str, Any] = {}
    for field in missing:
        properties[field.name] = {
            "type": ["object", "null"],
            "description": _field_description(field),
            "properties": {
                "value": {
                    "type": "string",
                    "description": "The value exactly as printed in the fragment.",
                },
                "quote": {
                    "type": "string",
                    "description": (
                        "The exact contiguous substring of the fragment the value was read "
                        "from, copied character for character. Must contain `value`."
                    ),
                },
                "page": {
                    "type": "integer",
                    "description": "1-based page number the quote was read from.",
                },
            },
            "required": ["value", "quote", "page"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "title": f"{doctype_id or 'document'}_missing_fields",
        "properties": properties,
        "required": [f.name for f in missing],
        "additionalProperties": False,
    }


def _field_description(field: FieldSpec) -> str:
    """A one-line description of a field for the schema, from what the spec already declares."""
    parts = [f"{field.name} ({field.type})"]
    labels = [label for group in field.labels.values() for label in group]
    if labels:
        parts.append("printed near: " + ", ".join(labels[:6]))
    if field.pattern:
        parts.append(f"expected shape (regex): {field.pattern}")
    if field.notes:
        parts.append(field.notes)
    parts.append("null if it is not in the fragment")
    return "; ".join(parts)


def build_request(
    missing: list[FieldSpec], window: Window, doctype_id: str, *, model: str
) -> dict[str, Any]:
    """Build the OpenAI-compatible chat-completions body.

    ``temperature=0`` and ``response_format={"type": "json_schema", ...}``: this is an
    extraction call, not a conversation. An endpoint that ignores ``response_format`` still
    gets the schema in the prompt, and anything it returns that is not JSON matching it is
    dropped by :func:`ground_fields` rather than trusted.
    """
    schema = build_json_schema(missing, doctype_id=doctype_id)
    user = (
        f"Document type: {doctype_id}\n"
        f"Fragment (pages {', '.join(str(p) for p in window.pages) or 'n/a'}):\n"
        "<<<FRAGMENT\n"
        f"{window.prompt_text}\n"
        "FRAGMENT\n\n"
        "Report these fields, as JSON matching the schema:\n"
        + "\n".join(f"- {_field_description(f)}" for f in missing)
    )
    return {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": f"{doctype_id or 'document'}_missing_fields",
                "strict": True,
                "schema": schema,
            },
        },
    }


# ---------------------------------------------------------------------------
# Grounding — the part that makes the output evidence
# ---------------------------------------------------------------------------
def _flat(text: str) -> str:
    """Collapse whitespace and case for substring comparison.

    OCR line-wrapping and the model's own re-spacing are not hallucinations, so matching is
    whitespace-insensitive. Nothing else is relaxed: characters must be the document's.
    """
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def _locate_quote(quote: str, window: Window) -> tuple[bool, int | None, Quad | None]:
    """Find ``quote`` in the window.

    Returns:
        ``(found, page, bbox)``. A quote inside a single block carries that block's page and
        bbox — which is what makes a T4 field reviewable. A quote that spans blocks is still
        grounded (it is the document's text) but has no single box to point at.
    """
    needle = _flat(quote)
    if not needle:
        return False, None, None
    for block in window.blocks:
        if needle in _flat(block.text):
            return True, block.page, block.bbox
    if needle in _flat(window.text):
        return True, None, None
    return False, None, None


def _verify(field: FieldSpec, value: str) -> tuple[str, str, float, str]:
    """Run the field's validator over a grounded value.

    Returns:
        ``(normalized, verification, confidence, validator_error)``. With no validator the
        value stays ``unverified`` however plausible it looks — the ladder is about what was
        *checked*, and a model's agreement is not a check.
    """
    if not field.validator:
        return value, VERIFICATION_UNVERIFIED, _CONFIDENCE_GROUNDED, ""
    result = V.validate(field.validator, value)
    if not result.ok:
        return value, VERIFICATION_UNVERIFIED, _CONFIDENCE_REJECTED, result.error
    if result.error:
        return result.normalized, VERIFICATION_FORMAT_VALID, _CONFIDENCE_SOFT, result.error
    if V.verification_level(field.validator) == "checksum":
        return result.normalized, VERIFICATION_CHECKSUM_VERIFIED, _CONFIDENCE_CHECKSUM, ""
    return result.normalized, VERIFICATION_FORMAT_VALID, _CONFIDENCE_FORMAT, ""


def ground_fields(
    missing: list[FieldSpec], answer: dict[str, Any], window: Window
) -> list[ExtractedField]:
    """Turn a model response into extracted fields, discarding everything ungrounded.

    A field survives only if the model returned a ``quote`` that occurs in the window we sent
    **and** a ``value`` that occurs inside that quote. Both halves matter: the first says the
    text is the document's, the second says the value is the quote's and not something appended
    to a real-looking citation.

    A value that fails either check is dropped and logged. It is deliberately not returned at a
    low confidence — a hallucinated value in a review queue still gets skim-approved by a tired
    human, and a value that provably is not on the page has nothing to review.

    Args:
        missing: The fields that were asked about; anything else in ``answer`` is ignored.
        answer: The parsed JSON object from the model.
        window: The exact window that was sent.

    Returns:
        One :class:`~dce.models.ExtractedField` per grounded value, ``locator="llm"``.
    """
    out: list[ExtractedField] = []
    for field in missing:
        item = answer.get(field.name)
        if not isinstance(item, dict):
            continue  # null (not present) or a shape we did not ask for
        value = str(item.get("value") or "").strip()
        quote = str(item.get("quote") or "")
        if not value:
            continue
        found, page, bbox = _locate_quote(quote, window)
        if not found:
            logger.warning(
                "T4 discarded %s: quote is not in the window that was sent (ungrounded)",
                field.name,
            )
            continue
        if _flat(value) not in _flat(quote):
            logger.warning(
                "T4 discarded %s: value is not inside the quote it was cited from", field.name
            )
            continue
        normalized, verification, confidence, error = _verify(field, value)
        reported_page = page if page is not None else _page_hint(item, window)
        out.append(
            ExtractedField(
                name=field.name,
                attribute_key=field.attribute_key,
                value=value,
                normalized=normalized or value,
                confidence=confidence,
                verification=verification,
                locator=LOCATOR_NAME,
                page=reported_page,
                bbox=bbox,
                pii=field.pii,
                validator_error=error,
            )
        )
    return out


def _page_hint(item: dict[str, Any], window: Window) -> int | None:
    """The model's claimed page, kept only when it is a page we actually sent."""
    page = item.get("page")
    if isinstance(page, int) and page in window.pages:
        return page
    return None


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------
async def _post_completion(
    payload: dict[str, Any], *, base_url: str, api_key: str, timeout: float
) -> dict[str, Any]:
    """POST the chat-completions request and return the parsed body.

    ``httpx`` is imported **here**, not at module scope. The base image ships no HTTP client at
    all (see ``pyproject.toml``), which is the strongest form of "the classification path cannot
    call out": there is nothing to call out with. A deployment that turns T4 on installs one
    deliberately, and one that does not gets an :class:`ImportError` on this line instead of a
    surprise network call.
    """
    import httpx

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return dict(response.json())


def parse_answer(body: dict[str, Any]) -> dict[str, Any]:
    """Pull the JSON object out of an OpenAI-compatible response body.

    Returns an empty mapping for anything that is not a JSON object — free-form prose, a
    refusal, a truncated stream. There is no salvage path: the contract was a schema, and
    scraping values out of prose is exactly the ungrounded behaviour this tier refuses.
    """
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {}
    if isinstance(content, list):  # some gateways return content parts
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        return {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("T4 response was not JSON; discarding the whole answer")
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def extract_fields_llm(
    missing: list[FieldSpec],
    view: LayoutView,
    doctype_id: str,
    *,
    settings: Any,
) -> list[ExtractedField]:
    """Ask a constrained LLM for the fields T1-T3 could not find.

    Args:
        missing: Unresolved fields. An empty list short-circuits — this tier never runs
            "just in case".
        view: The classified document's layout. Only the window is sent.
        doctype_id: The **accepted** doctype. ``unknown`` raises: see below.
        settings: Process settings. ``t4_enabled``, ``llm_base_url``, ``llm_api_key``,
            ``llm_model``, ``llm_timeout_seconds`` and ``llm_max_window_chars`` are all read
            with ``getattr`` defaults, so this module works against a settings object that has
            not grown the fields yet — and stays off when it has not.

    Returns:
        Grounded fields only, possibly empty. Empty is a normal outcome: the field goes to the
        review queue, which is where it was going anyway.

    Raises:
        EgressViolation: When ``doctype_id`` is ``unknown``/empty, or when this is somehow
            called from inside the classification cascade. Never swallowed — an unclassified
            document reaching a model is the one failure this service must not paper over.
    """
    if not missing:
        return []
    if not getattr(settings, "t4_enabled", False):
        return []

    # Opened before anything else touches the network — and before a prompt is even built.
    # Refuses outright when the cascade abstained.
    with post_classification_scope(doctype_id) as accepted:
        base_url = str(getattr(settings, "llm_base_url", "") or "")
        model = str(getattr(settings, "llm_model", "") or "")
        if not base_url or not model:
            logger.warning(
                "T4 is enabled but llm_base_url/llm_model are not configured; skipping"
            )
            return []

        window = build_window(missing, view, settings=settings)
        if not window:
            return []

        payload = build_request(missing, window, accepted, model=model)
        logger.info(
            "T4 calling %s for %s: %d field(s), window=%d chars over page(s) %s (%s)",
            model,
            accepted,
            len(missing),
            window.char_count,
            list(window.pages),
            window.reason,
        )
        try:
            body = await _post_completion(
                payload,
                base_url=base_url,
                api_key=str(getattr(settings, "llm_api_key", "") or ""),
                timeout=float(
                    getattr(settings, "llm_timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
                ),
            )
        except ImportError:
            logger.warning(
                "T4 is enabled but no HTTP client is installed. The base image ships none by "
                "design; install httpx deliberately if this tier is wanted."
            )
            return []
        except Exception as exc:  # noqa: BLE001 - T4 is the last *automated* tier, not the
            # last tier: a timeout, a 5xx or a gateway hiccup must degrade to "no value", which
            # sends the field to the human queue it was already headed for. Failing the whole
            # extraction because an optional remote tier was unwell would be a worse trade.
            logger.warning("T4 call failed (%s); falling through to review", type(exc).__name__)
            return []

    return ground_fields(missing, parse_answer(body), window)
