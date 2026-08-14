"""T2 — Azure Document Intelligence prebuilt specialists. Optional, post-classification only.

T1 (:mod:`dce.extract.resolve`) is layout-anchored and local: it needs no network and it is
what runs by default. T2 exists for the documents where a *trained* model beats any locator —
a passport's photo page, a W-2's box grid, a bank statement's transaction table — and it pays
for that with an egress call, which is why it is off unless a deployment turns it on.

**Where the egress rule actually sits.** The invariant this service exists to defend is *no
egress before classification*, not *no egress ever*. Once the cascade has accepted a doctype,
the document has been placed by this process, on its own evidence, and calling a vendor
specialist for that doctype is the entire point of the tiering. So this module:

* **refuses to run on an unclassified document.** ``doctype_id`` of ``unknown`` (or blank)
  raises :class:`UnclassifiedDocumentError`. An abstention routes to a human queue; it never
  routes to a vendor, because "ask Azure what this is" is the leak wearing a different hat.
* **is off by default.** ``t2_enabled`` defaults to False and an unconfigured endpoint is a
  no-op, so a deployment that wants zero egress at all keeps it by doing nothing.
* **is never imported by the classification path.** Nothing under ``dce/classify`` imports
  this module, and the HTTP client is imported *inside* the functions that call out — see
  :func:`_new_client` — so importing :mod:`dce.extract` never pulls an HTTP client into the
  process at all.
* **calls** :func:`dce.egress.assert_no_egress` immediately before opening a connection, so
  even a future caller that reached T2 from inside a classification scope is stopped at
  runtime rather than by convention.

Settings consumed (owned by ``dce/config.py``; read through :func:`getattr` with defaults so
this module works before they land):

==========================  =============  ====================================================
Name                        Default        Meaning
==========================  =============  ====================================================
``azure_di_endpoint``       ``""``         Resource endpoint, e.g. ``https://x.cognitiveservices.azure.com``
``azure_di_key``            ``""``         Resource key, sent as ``Ocp-Apim-Subscription-Key``
``azure_di_api_version``    2024-11-30     Document Intelligence REST api-version
``t2_enabled``              ``False``      Master switch for this tier
``t3_enabled``              ``False``      Master switch for :mod:`dce.extract.query_fields`
``t3_max_query_fields``     20             Azure's per-request query-field cap
==========================  =============  ====================================================

**Protocol.** ``POST {endpoint}/documentintelligence/documentModels/{model}:analyze`` returns
``202`` with an ``Operation-Location`` header; that URL is polled until ``succeeded`` and
``analyzeResult.documents[0].fields`` is mapped to :class:`~dce.models.ExtractedField`.

**Verification is ``format_valid``, never ``checksum_verified``.** Azure returns a confidence,
and a confidence is not a proof. Only a real check digit (:mod:`dce.extract.validate`) earns
``checksum_verified``, so a value from this tier stays one rung below anything T1 verified —
which is exactly what lets a caller merge the two without T2 ever overwriting a proven value.

**Nothing here logs a value.** Field names, counts and model ids only: these documents are
someone's passport.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

from dce.egress import assert_no_egress
from dce.models import UNKNOWN, ExtractedField, Quad

__all__ = [
    "SPECIALIST_MODELS",
    "AzureAnalyzeError",
    "UnclassifiedDocumentError",
    "analyze_document",
    "extract_with_specialist",
    "fields_from_analyze_result",
    "require_classified",
    "specialist_for",
]

logger = logging.getLogger(__name__)

#: doctype_id -> Azure prebuilt model id.
#:
#: Deliberately conservative. ``prebuilt-idDocument`` is trained on passport photo pages and
#: North-American driving licences / state IDs; pointing it at a document it was not trained on
#: (``mx_ine``, ``us_green_card``, ``ca_pr_card``) returns fields with a
#: plausible confidence and the wrong values, which is worse than returning nothing. Those
#: doctypes stay on T1 until someone has measured a specialist on them. Extending this map is a
#: deliberate act, one doctype at a time.
SPECIALIST_MODELS: dict[str, str] = {
    # Identity — Azure's ID model
    "us_passport": "prebuilt-idDocument",
    "ca_passport": "prebuilt-idDocument",
    "mx_passport": "prebuilt-idDocument",
    "us_drivers_license": "prebuilt-idDocument",
    "us_state_id": "prebuilt-idDocument",
    "ca_drivers_license": "prebuilt-idDocument",
    "ca_provincial_photo_id": "prebuilt-idDocument",
    # US tax forms — one model per form, they are not interchangeable
    "us_w2": "prebuilt-tax.us.w2",
    "us_1099": "prebuilt-tax.us.1099",
    "us_1040": "prebuilt-tax.us.1040",
    # Financial. NOTE the ``.us`` suffix on the bank-statement model: Azure ships no Canadian
    # variant, and Canadian statements are close enough in layout that it is worth running —
    # but its output is worth a little less, which is why nothing here is checksum-verified.
    "us_bank_statement": "prebuilt-bankStatement.us",
    "ca_bank_statement": "prebuilt-bankStatement.us",
    "us_paystub": "prebuilt-payStub.us",
}

#: Fallback when ``azure_di_api_version`` is not configured. 2024-11-30 is the GA v4.0 API:
#: it is the version whose route is ``/documentintelligence/`` (v3.x used ``/formrecognizer/``)
#: and whose add-on capability is spelled ``queryFields``.
DEFAULT_API_VERSION = "2024-11-30"

#: Verification level everything from this tier is reported at. See the module docstring.
VERIFICATION = "format_valid"

_ANALYZE_TIMEOUT_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 1.0
_POLL_BACKOFF = 1.5
_POLL_MAX_INTERVAL_SECONDS = 5.0
#: A large scanned statement genuinely takes tens of seconds; beyond this something is wrong
#: and a caller waiting on a review queue deserves an error rather than an open connection.
_POLL_DEADLINE_SECONDS = 120.0

_STATUS_SUCCEEDED = "succeeded"
_STATUS_FAILED = frozenset({"failed", "canceled", "cancelled"})

#: How deep to walk ``valueObject`` / ``valueArray`` before giving up and reporting the
#: container's own ``content``. Two levels covers ``Accounts[0].AccountNumber``, which is the
#: shape the bank-statement and pay-stub models actually return.
_MAX_FLATTEN_DEPTH = 2
#: Per-array cap. A 400-line transaction table is not a KYC field set; the caller who wants
#: the whole ledger wants a different service.
_MAX_ARRAY_ITEMS = 25

#: ``valueCurrency`` / ``valueAddress`` are objects with no single scalar; handled separately.
_SCALAR_VALUE_KEYS = (
    "valueString",
    "valueDate",
    "valueTime",
    "valuePhoneNumber",
    "valueNumber",
    "valueInteger",
    "valueBoolean",
    "valueSelectionMark",
    "valueCountryRegion",
    "valueSignature",
)

_SNAKE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_WORD = re.compile(r"[^0-9A-Za-z]+")


class AzureAnalyzeError(RuntimeError):
    """An analyze job could not be completed.

    Carries what an operator needs to act: the model, the HTTP status or Azure error code, and
    Azure's own message. A T2 failure is never fatal to extraction — the caller keeps T1's
    fields — but it must be visible, so it is raised rather than swallowed.
    """


class UnclassifiedDocumentError(ValueError):
    """T2/T3 were asked to run on a document whose type is not known.

    Subclasses :class:`ValueError` because it is a caller bug, not an environment failure: the
    only correct thing to do with an abstention is to queue it for a human.
    """


async def specialist_for(doctype_id: str) -> str | None:
    """Return the Azure prebuilt model for a doctype, or ``None`` when there is none.

    ``None`` is the common and correct answer: 121 doctypes are registered and Azure ships a
    specialist for a handful of them. It means "stay on T1", not "something went wrong".

    Args:
        doctype_id: An accepted doctype id. ``unknown`` maps to ``None`` like any other
            unmapped id — the refusal to *act* on an abstention lives in
            :func:`extract_with_specialist`, so that a caller may safely probe the map.

    Returns:
        The Azure model id, or ``None``.
    """
    return SPECIALIST_MODELS.get((doctype_id or "").strip().lower())


async def extract_with_specialist(
    data: bytes, doctype_id: str, *, settings: Any
) -> list[ExtractedField]:
    """Run the doctype's Azure prebuilt specialist over the original document bytes.

    Args:
        data: The document as it was received (PDF/JPEG/PNG/TIFF). Sent verbatim to Azure.
        doctype_id: The **accepted** doctype. Must not be ``unknown``.
        settings: Anything exposing the settings named in the module docstring — normally
            :class:`dce.config.Settings`.

    Returns:
        One :class:`~dce.models.ExtractedField` per field Azure returned a value for, with
        ``locator="azure:{model}"``, ``verification="format_valid"``, and page + bbox taken
        from Azure's ``boundingRegions`` so a reviewer can see where each value came from.
        Empty when the tier is off, unconfigured, or has no specialist for this doctype.

    Raises:
        UnclassifiedDocumentError: When ``doctype_id`` is ``unknown`` or blank.
        ValueError: When ``data`` is empty.
        AzureAnalyzeError: When Azure rejected the request or the job failed.
    """
    require_classified(doctype_id, tier="T2")

    if not _flag(settings, "t2_enabled"):
        logger.debug("T2 disabled; staying on T1 for %s", doctype_id)
        return []

    model = await specialist_for(doctype_id)
    if model is None:
        logger.debug("T2 has no specialist for %s; staying on T1", doctype_id)
        return []

    if _connection(settings) is None:
        # Enabled but unconfigured is a deployment mistake, not a preference: say so once,
        # loudly, and degrade to T1 rather than failing the extraction.
        logger.warning(
            "t2_enabled is set but azure_di_endpoint/azure_di_key are empty; "
            "skipping the T2 specialist for %s",
            doctype_id,
        )
        return []

    analyze_result = await analyze_document(data, model, settings=settings)
    fields = fields_from_analyze_result(analyze_result, locator=f"azure:{model}")
    logger.info(
        "T2 %s returned %d field(s) for %s", model, len(fields), doctype_id
    )
    return fields


def require_classified(doctype_id: str, *, tier: str) -> str:
    """Assert that a doctype has actually been accepted, and return it normalised.

    Args:
        doctype_id: The doctype the caller intends to act on.
        tier: Tier name, quoted in the error so an operator can find the call site.

    Returns:
        The trimmed, lower-cased doctype id.

    Raises:
        UnclassifiedDocumentError: When the id is blank or ``unknown``.
    """
    normalised = (doctype_id or "").strip().lower()
    if not normalised or normalised == UNKNOWN:
        raise UnclassifiedDocumentError(
            f"{tier} refuses to run on an unclassified document (doctype_id="
            f"{doctype_id!r}). Egress is permitted only *after* the cascade has accepted a "
            "doctype; an abstention goes to the human review queue, never to a vendor model. "
            "If this fired inside the classification path, that path is the bug."
        )
    return normalised


async def analyze_document(
    data: bytes,
    model: str,
    *,
    settings: Any,
    features: Sequence[str] = (),
    query_fields: Sequence[str] = (),
) -> dict[str, Any]:
    """POST a document to Azure Document Intelligence and poll until the job settles.

    Shared by T2 (:func:`extract_with_specialist`) and T3
    (:func:`dce.extract.query_fields.extract_query_fields`) — the analyze/poll dance is the
    same call for both, only the model and the add-on features differ. **This function does
    not check the tier flags**: each tier owns its own switch and checks it before calling.

    Args:
        data: The document bytes, sent as ``application/octet-stream``.
        model: Azure model id, e.g. ``prebuilt-idDocument`` or ``prebuilt-layout``.
        settings: Settings carrying the endpoint, key and api-version.
        features: Add-on capabilities, e.g. ``("queryFields",)``.
        query_fields: Field names for the ``queryFields`` add-on.

    Returns:
        The ``analyzeResult`` object, or ``{}`` when the job succeeded with no result body.

    Raises:
        ValueError: When ``data`` is empty.
        AzureAnalyzeError: When the endpoint is unconfigured, Azure returned an error status,
            the ``202`` carried no ``Operation-Location``, the job reported ``failed``, or the
            poll deadline expired.
    """
    if not data:
        raise ValueError("analyze_document called with no document bytes")
    connection = _connection(settings)
    if connection is None:
        raise AzureAnalyzeError(
            f"cannot analyze with {model}: azure_di_endpoint/azure_di_key are not configured"
        )
    endpoint, key, api_version = connection

    # The process boundary is here and nowhere else in this module. Inside a classification
    # scope this raises, whatever the tier flags say.
    assert_no_egress(f"extract.azure.{model}", settings=settings)

    url = f"{endpoint}/documentintelligence/documentModels/{model}:analyze"
    params: dict[str, str] = {"api-version": api_version}
    if features:
        params["features"] = ",".join(features)
    if query_fields:
        params["queryFields"] = ",".join(query_fields)
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/octet-stream",
    }

    client = _new_client(timeout=_ANALYZE_TIMEOUT_SECONDS)
    async with client:
        response = await client.post(url, params=params, headers=headers, content=data)
        if response.status_code >= 400:
            raise AzureAnalyzeError(
                f"{model} analyze failed: HTTP {response.status_code} {_error_detail(response)}"
            )
        if response.status_code != 202:
            # Some proxies collapse the long-running operation into a single 200. Take the
            # result when it is there rather than insisting on the async shape.
            body = _json(response)
            if isinstance(body.get("analyzeResult"), dict):
                return dict(body["analyzeResult"])
        operation_location = response.headers.get("Operation-Location")
        if not operation_location:
            raise AzureAnalyzeError(
                f"{model} analyze returned HTTP {response.status_code} with no "
                "Operation-Location header; there is nothing to poll"
            )
        return await _poll(client, operation_location, model=model, key=key)


async def _poll(client: Any, operation_location: str, *, model: str, key: str) -> dict[str, Any]:
    """Poll an analyze operation until it succeeds, fails, or the deadline passes."""
    headers = {"Ocp-Apim-Subscription-Key": key}
    deadline = time.monotonic() + _POLL_DEADLINE_SECONDS
    interval = _POLL_INTERVAL_SECONDS
    polls = 0
    while True:
        response = await client.get(operation_location, headers=headers)
        polls += 1
        if response.status_code >= 400:
            raise AzureAnalyzeError(
                f"{model} poll failed: HTTP {response.status_code} {_error_detail(response)}"
            )
        body = _json(response)
        status = str(body.get("status", "")).strip().lower()
        if status == _STATUS_SUCCEEDED:
            result = body.get("analyzeResult")
            logger.debug("T2/T3 %s succeeded after %d poll(s)", model, polls)
            return dict(result) if isinstance(result, dict) else {}
        if status in _STATUS_FAILED:
            raise AzureAnalyzeError(f"{model} job {status}: {_error_text(body.get('error'))}")
        if time.monotonic() >= deadline:
            raise AzureAnalyzeError(
                f"{model} job did not finish within {_POLL_DEADLINE_SECONDS:.0f}s "
                f"(last status: {status or 'unknown'}, {polls} poll(s))"
            )
        await asyncio.sleep(interval)
        interval = min(interval * _POLL_BACKOFF, _POLL_MAX_INTERVAL_SECONDS)


def fields_from_analyze_result(
    analyze_result: Mapping[str, Any],
    *,
    locator: str,
    prefer_names: Sequence[str] = (),
) -> list[ExtractedField]:
    """Map ``analyzeResult.documents[0].fields`` into :class:`~dce.models.ExtractedField`.

    Azure names fields in PascalCase (``DateOfBirth``); this service names them in snake_case
    (``date_of_birth``), and a caller merging T2 output over T1 output has to be able to match
    them by name. So the key is snake-cased, deterministically. Nested ``valueObject`` /
    ``valueArray`` structures — which is how the bank-statement and pay-stub models return
    accounts and earnings lines — are flattened to ``accounts[0].account_number``.

    Args:
        analyze_result: The ``analyzeResult`` object from a settled analyze job.
        locator: Provenance string, e.g. ``azure:prebuilt-idDocument``.
        prefer_names: Names the caller asked for (T3). An Azure key that snake-folds onto one
            of these is reported under the caller's exact spelling, so the caller can merge on
            the name it sent.

    Returns:
        One field per value Azure actually returned. Fields Azure left empty are omitted: this
        tier reports what it found, and the schema decides what was required.
    """
    documents = analyze_result.get("documents")
    if not isinstance(documents, list) or not documents:
        logger.debug("%s returned no documents[]", locator)
        return []
    if len(documents) > 1:
        logger.debug("%s returned %d documents; using the first", locator, len(documents))
    first = documents[0] if isinstance(documents[0], dict) else {}
    raw_fields = first.get("fields")
    if not isinstance(raw_fields, dict):
        return []

    preferred = {_snake(name): name for name in prefer_names if name}
    flat: list[tuple[str, Mapping[str, Any]]] = []
    for key, node in raw_fields.items():
        if isinstance(node, Mapping):
            _flatten(_snake(str(key)), node, flat, depth=0)

    out: list[ExtractedField] = []
    # PII: this tier cannot see the doctype's FieldSpecs, so it cannot know which fields are
    # personal data — and every field a passport/W-2/pay-stub specialist returns is about a
    # person. Marked PII here and downgraded by the caller when it merges against the schema.
    # Over-marking costs a redaction that was not needed; under-marking leaks a passport
    # number into a log line, and only one of those is recoverable.
    for name, node in flat:
        value, normalized = _value_of(node)
        if not value:
            continue
        page, bbox = _region_of(node)
        out.append(
            ExtractedField(
                name=preferred.get(name, name),
                value=value,
                normalized=normalized or value,
                confidence=_confidence_of(node),
                verification=VERIFICATION,
                locator=locator,
                page=page,
                bbox=bbox,
                pii=True,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Settings access — every read goes through getattr so this module works before
# dce/config.py grows the T2/T3 names (they are owned by another change).
# ---------------------------------------------------------------------------
def _flag(settings: Any, name: str) -> bool:
    """Read a boolean tier switch; missing means off."""
    return bool(getattr(settings, name, False))


def _connection(settings: Any) -> tuple[str, str, str] | None:
    """Return ``(endpoint, key, api_version)``, or ``None`` when not fully configured."""
    endpoint = str(getattr(settings, "azure_di_endpoint", "") or "").strip().rstrip("/")
    key = str(getattr(settings, "azure_di_key", "") or "").strip()
    api_version = str(
        getattr(settings, "azure_di_api_version", "") or DEFAULT_API_VERSION
    ).strip()
    if not endpoint or not key:
        return None
    return endpoint, key, api_version


def _new_client(*, timeout: float) -> Any:
    """Build the async HTTP client.

    ``httpx`` is imported **here**, not at module scope, for two reasons. It is a dev-only
    dependency — the runtime image deliberately ships without an HTTP client — and importing
    :mod:`dce.extract` must never put one in the process, because that is the thing the
    classification path is not allowed to reach for.

    This is also the single seam the tests replace: swapping this for a client built on an
    ``httpx.MockTransport`` makes the whole tier exercisable with no network at all.

    Args:
        timeout: Per-request timeout in seconds.

    Returns:
        An ``httpx.AsyncClient``.
    """
    import httpx

    return httpx.AsyncClient(timeout=timeout)


# ---------------------------------------------------------------------------
# Response mapping
# ---------------------------------------------------------------------------
def _json(response: Any) -> dict[str, Any]:
    """Parse a JSON body, tolerating an empty or non-JSON one."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_detail(response: Any) -> str:
    """Azure's error code/message from a failed response, or a truncated body."""
    body = _json(response)
    detail = _error_text(body.get("error"))
    if detail:
        return detail
    text = str(getattr(response, "text", "") or "")
    return text[:200]


def _error_text(error: Any) -> str:
    """Render an Azure ``{"code": ..., "message": ...}`` block."""
    if not isinstance(error, Mapping):
        return ""
    code = str(error.get("code", "") or "")
    message = str(error.get("message", "") or "")
    inner = error.get("innererror")
    if isinstance(inner, Mapping) and not message:
        message = str(inner.get("message", "") or "")
    return " ".join(part for part in (code, message) if part).strip()


def _snake(name: str) -> str:
    """``DateOfBirth`` -> ``date_of_birth``; already-snake names are unchanged."""
    text = _NON_WORD.sub("_", (name or "").strip())
    return _SNAKE_BOUNDARY.sub("_", text).strip("_").lower()


def _flatten(
    name: str,
    node: Mapping[str, Any],
    out: list[tuple[str, Mapping[str, Any]]],
    *,
    depth: int,
) -> None:
    """Flatten Azure's nested field tree into ``(dotted_name, leaf_node)`` pairs."""
    field_type = str(node.get("type", "") or "")
    if depth < _MAX_FLATTEN_DEPTH and field_type == "object":
        children = node.get("valueObject")
        if isinstance(children, Mapping) and children:
            for key, child in children.items():
                if isinstance(child, Mapping):
                    _flatten(f"{name}.{_snake(str(key))}", child, out, depth=depth + 1)
            return
    if depth < _MAX_FLATTEN_DEPTH and field_type == "array":
        items = node.get("valueArray")
        if isinstance(items, list) and items:
            for index, child in enumerate(items[:_MAX_ARRAY_ITEMS]):
                if isinstance(child, Mapping):
                    _flatten(f"{name}[{index}]", child, out, depth=depth + 1)
            return
    out.append((name, node))


def _value_of(node: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(value, normalized)`` for one Azure field node.

    ``content`` is the value *as printed on the page*, which is what a reviewer needs to see
    next to the bbox. The typed ``value*`` member is Azure's canonical reading of it — an ISO
    date, a plain decimal — and becomes ``normalized``. When only one of them exists it plays
    both parts.
    """
    content = str(node.get("content", "") or "").strip()
    normalized = ""
    for key in _SCALAR_VALUE_KEYS:
        if key in node and node[key] is not None:
            normalized = str(node[key]).strip()
            break
    else:
        currency = node.get("valueCurrency")
        if isinstance(currency, Mapping) and currency.get("amount") is not None:
            normalized = str(currency["amount"]).strip()
    return (content or normalized), (normalized or content)


def _confidence_of(node: Mapping[str, Any]) -> float:
    """Azure's confidence, clamped to 0..1.

    A missing confidence becomes ``0.0`` rather than an optimistic default: with no number
    from the vendor, the field falls below ``extract_accept_confidence`` and lands in the
    review queue, which is the direction to fail in.
    """
    try:
        value = float(node.get("confidence", 0.0))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value))


def _region_of(node: Mapping[str, Any]) -> tuple[int | None, Quad | None]:
    """Page number and quad from ``boundingRegions[0]``."""
    regions = node.get("boundingRegions")
    if not isinstance(regions, list) or not regions:
        return None, None
    region = regions[0]
    if not isinstance(region, Mapping):
        return None, None
    page: int | None
    try:
        page = int(region["pageNumber"])
    except (KeyError, TypeError, ValueError):
        page = None
    polygon = region.get("polygon")
    if not isinstance(polygon, list):
        polygon = region.get("boundingBox")
    quad: Quad | None = None
    if isinstance(polygon, list) and len(polygon) == 8:
        try:
            quad = [float(coordinate) for coordinate in polygon]
        except (TypeError, ValueError):
            quad = None
    return page, quad
