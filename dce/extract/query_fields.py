"""T3 — Azure Layout ``queryFields``: ask for the fields T1 and T2 could not find, by name.

T1 finds a value because it knows *where* to look (a label, a table header, an MRZ). T2 finds
it because a model was trained on that document. T3 is for the residue: a field this service
knows it wants, on a layout nobody has modelled — the landlord's own rent-agreement template,
a municipal water bill from one city in one state. Azure's ``queryFields`` add-on takes the
field *names* and returns whatever it believes they are, against ``prebuilt-layout``.

That makes T3 the weakest tier and the one to run last, over the *smallest* possible name
list: everything T1 and T2 already resolved should be excluded before calling.

**Same egress rule as T2**, and for the same reason — see :mod:`dce.extract.azure_specialist`,
whose analyze/poll client this module reuses rather than duplicating:

* pass ``doctype_id`` and an abstention (``unknown``) raises
  :class:`~dce.extract.azure_specialist.UnclassifiedDocumentError`;
* ``t3_enabled`` is False by default and an unconfigured endpoint is a no-op;
* the classification path never imports this module, and the HTTP client is imported inside
  the function that calls out.

**The cap is Azure's, not ours.** ``queryFields`` accepts at most 20 names per request. Asking
for 25 is a ``400`` that loses all 25, so the list is truncated to ``t3_max_query_fields``
(itself clamped to Azure's 20), the overflow is **named** in a warning, and the request goes
ahead with what fits. Losing five fields loudly beats losing twenty silently. Callers that
need the overflow can call again with the remainder.

Settings consumed (owned by ``dce/config.py``, read via :func:`getattr` with defaults):
``azure_di_endpoint``, ``azure_di_key``, ``azure_di_api_version`` (default ``2024-11-30``),
``t3_enabled`` (default ``False``), ``t3_max_query_fields`` (default ``20``).
"""
from __future__ import annotations

import logging
from typing import Any

from dce.extract.azure_specialist import (
    analyze_document,
    fields_from_analyze_result,
    require_classified,
)
from dce.models import ExtractedField

__all__ = [
    "AZURE_MAX_QUERY_FIELDS",
    "LAYOUT_MODEL",
    "LOCATOR",
    "cap_query_fields",
    "extract_query_fields",
]

logger = logging.getLogger(__name__)

#: ``queryFields`` is an add-on capability of the layout model, not a model of its own.
LAYOUT_MODEL = "prebuilt-layout"
#: The add-on capability name, passed as ``features``.
QUERY_FIELDS_FEATURE = "queryFields"
#: Provenance on every field this tier produces.
LOCATOR = "azure:queryFields"

#: Azure's own per-request maximum. ``t3_max_query_fields`` may lower this; it cannot raise it,
#: because the service on the other end will simply reject the request.
AZURE_MAX_QUERY_FIELDS = 20


def cap_query_fields(
    field_names: list[str], limit: int = AZURE_MAX_QUERY_FIELDS
) -> tuple[list[str], list[str]]:
    """Clean, de-duplicate and truncate a query-field list to what Azure will accept.

    Args:
        field_names: Names the caller wants values for, in priority order — truncation keeps
            the head of the list, so the caller's order *is* the priority.
        limit: Requested cap; clamped to :data:`AZURE_MAX_QUERY_FIELDS`.

    Returns:
        ``(kept, dropped)``. ``dropped`` holds the names that did not fit, in the order they
        were asked for, so the caller (and the log line) can name them.
    """
    effective = min(int(limit), AZURE_MAX_QUERY_FIELDS)
    if effective < int(limit):
        logger.warning(
            "t3_max_query_fields=%s exceeds Azure's per-request maximum of %d; using %d",
            limit,
            AZURE_MAX_QUERY_FIELDS,
            AZURE_MAX_QUERY_FIELDS,
        )
    effective = max(effective, 0)

    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in field_names or []:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    return cleaned[:effective], cleaned[effective:]


async def extract_query_fields(
    data: bytes,
    field_names: list[str],
    *,
    settings: Any,
    doctype_id: str | None = None,
) -> list[ExtractedField]:
    """Ask Azure Layout for named fields it was never trained on.

    Args:
        data: The document as it was received. Sent verbatim to Azure.
        field_names: The fields still missing after T1/T2, in priority order.
        settings: Anything exposing the settings named in the module docstring.
        doctype_id: The accepted doctype. Optional only because the tier does not *need* it to
            build the request — but pass it: it is what lets this tier refuse an unclassified
            document the way :mod:`dce.extract.azure_specialist` does.

    Returns:
        One :class:`~dce.models.ExtractedField` per name Azure returned a value for, under the
        caller's exact spelling, with ``locator="azure:queryFields"`` and
        ``verification="format_valid"``. Empty when the tier is off, unconfigured, or asked
        for nothing.

    Raises:
        UnclassifiedDocumentError: When ``doctype_id`` is given and is ``unknown`` or blank.
        ValueError: When ``data`` is empty.
        AzureAnalyzeError: When Azure rejected the request or the job failed.
    """
    if doctype_id is not None:
        require_classified(doctype_id, tier="T3")

    if not bool(getattr(settings, "t3_enabled", False)):
        logger.debug("T3 disabled; %d field(s) stay unresolved", len(field_names or []))
        return []

    configured = getattr(settings, "t3_max_query_fields", None)
    limit = AZURE_MAX_QUERY_FIELDS if configured is None else int(configured)
    kept, dropped = cap_query_fields(field_names, limit)
    if dropped:
        logger.warning(
            "T3 asks Azure for at most %d field(s) per request; dropped %d: %s. "
            "Call again with the remainder if they matter.",
            min(limit, AZURE_MAX_QUERY_FIELDS),
            len(dropped),
            ", ".join(dropped),
        )
    if not kept:
        logger.debug("T3 has no field names to ask for")
        return []

    endpoint = str(getattr(settings, "azure_di_endpoint", "") or "").strip()
    key = str(getattr(settings, "azure_di_key", "") or "").strip()
    if not endpoint or not key:
        logger.warning(
            "t3_enabled is set but azure_di_endpoint/azure_di_key are empty; "
            "skipping query fields for %d name(s)",
            len(kept),
        )
        return []

    analyze_result = await analyze_document(
        data,
        LAYOUT_MODEL,
        settings=settings,
        features=(QUERY_FIELDS_FEATURE,),
        query_fields=kept,
    )
    fields = fields_from_analyze_result(analyze_result, locator=LOCATOR, prefer_names=kept)
    answered = {field.name for field in fields}
    unanswered = [name for name in kept if name not in answered]
    if unanswered:
        logger.info("T3 found no value for %d of %d field(s)", len(unanswered), len(kept))
    return fields
