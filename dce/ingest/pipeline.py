"""The dispatcher: bytes in, a :class:`~dce.ingest.result.IngestResult` out.

One entry point, :func:`ingest`. It detects the format from content, picks the parser, runs
it under the caps, and returns either a :class:`~dce.models.LayoutView` or the structured
``needs_ocr`` outcome. It raises only the errors in :mod:`dce.ingest.errors`; nothing from a
parser's internals escapes.
"""
from __future__ import annotations

import time

from pydantic import BaseModel, Field

from dce.ingest.builder import LayoutBuilder
from dce.ingest.detect import IMAGE_TYPES, MediaType, decode_text, detect
from dce.ingest.errors import (
    EngineUnavailable,
    IngestError,
    OcrProviderMismatch,
    PayloadTooLarge,
    UnsupportedFormat,
)
from dce.ingest.images import (
    NEEDS_OCR_REMEDY,
    needs_ocr_reason,
    probe,
    recognize_image,
)
from dce.ingest.limits import Deadline, IngestLimits
from dce.ingest.ocr import LocalOcrProvider, ocr_pages_to_builder, provider_or_none
from dce.ingest.result import IngestResult, IngestStatus, TextSource
from dce.ingest.settings import IngestSettings, get_ingest_settings
from dce.models import LayoutView


class IngestOptions(BaseModel):
    """Per-request ingestion options — the one new field on the API request model.

    Deliberately small, and deliberately unable to *raise* this deployment's capabilities:

    * ``filename`` is a hint for plain-text subtypes only. It can never choose a binary
      parser (see :mod:`dce.ingest.detect`).
    * ``local_ocr`` can turn local OCR **off** for one request, but cannot turn it on where
      the operator has not enabled it. Whether an unclassified document may be run through a
      recognition engine, and whether this deployment stands behind that engine's accuracy,
      are operator decisions; a caller flag that could switch them on would make the default
      meaningless.
    * ``remote_ocr`` is the same asymmetry over the network providers, and it matters more:
      a caller who knows this particular document may not leave their jurisdiction can always
      decline, and can never grant. **Either flag set to False declines whichever recogniser
      this deployment has configured** — only one is ever active (:meth:`IngestSettings._check`
      refuses both at once), and a caller saying "do not run OCR on this" must be honoured
      whichever kind is installed rather than silently applying to the other one.
    * ``ocr_provider`` is an **assertion, not a selector**, and the distinction is the whole
      point. It says "read this only if the recogniser configured here is the one I was told
      about"; a mismatch raises
      :class:`~dce.ingest.errors.OcrProviderMismatch` rather than quietly using whatever is
      installed. It therefore cannot grant either: pinning ``azure_read`` where no remote
      recogniser is enabled is a refusal, not an enablement.

      Without it the worst failure on this path is silent. A console shows an operator "this
      document will be sent to <endpoint>", the operator accepts, the deployment is
      reconfigured to a different provider, and the document goes to a third party nobody
      acknowledged — with every response still reporting, truthfully, that OCR succeeded.
      The pin turns that into an error.
    """

    filename: str | None = Field(default=None, max_length=512)
    local_ocr: bool | None = None
    remote_ocr: bool | None = None
    #: The provider the caller expects to run, e.g. ``azure_layout``. See the class docstring:
    #: checked against :meth:`IngestSettings.active_provider`, never used to choose one.
    ocr_provider: str | None = Field(default=None, max_length=64)


def _check_provider_pin(settings: IngestSettings, pinned: str | None) -> None:
    """Verify a caller's ``ocr_provider`` assertion against what is actually configured.

    Checked whenever it is set, including when the caller also declined recognition. It is an
    assertion about the *deployment*, and an assertion that is wrong is worth saying so
    regardless of whether this particular request would have acted on it — a caller working
    from a stale picture of where documents go should find that out on the first request, not
    on the first one that happens to contain an image.

    Raises:
        OcrProviderMismatch: The pin does not name the active recogniser.
    """
    if pinned is None:
        return
    wanted = pinned.strip().lower()
    if not wanted:
        return
    active = settings.active_provider()
    if wanted == active:
        return
    if active == "none":
        detail = (
            f"this request pinned ocr_provider={wanted!r}, but no recogniser is configured on "
            "this deployment. A pin cannot switch one on: it only refuses to let a document "
            "be read by a provider other than the one the caller was told about"
        )
    else:
        detail = (
            f"this request pinned ocr_provider={wanted!r}, but the recogniser configured here "
            f"is {active!r}. Refusing rather than substituting: if a caller has disclosed one "
            "destination to a user, reading the document with a different one would make that "
            "disclosure false"
        )
    raise OcrProviderMismatch(detail)


def _declined(local_ocr: bool | None, remote_ocr: bool | None) -> bool:
    """Whether the caller declined recognition, by either flag.

    Either one is enough. Only one recogniser is ever active on a deployment, and a caller
    saying "do not run OCR on this document" must be obeyed whichever kind happens to be
    installed rather than silently applying to the other one.
    """
    return local_ocr is False or remote_ocr is False


def _resolve_provider(
    settings: IngestSettings, requested: bool | None
) -> tuple[LocalOcrProvider | None, bool]:
    """``(provider, enabled_on_this_deployment)``."""
    if not settings.local_ocr_enabled:
        return None, False
    if requested is False:
        return None, True
    return (
        provider_or_none(settings.local_ocr_engine, languages=settings.local_ocr_languages),
        True,
    )


def _resolve_remote_provider(settings: IngestSettings, *, declined: bool):
    """``(provider, enabled_on_this_deployment)`` for the NETWORK recogniser.

    Constructing the provider is not the permission — :func:`dce.egress.assert_ocr_egress_permitted`
    is, and it runs at every request the provider makes. This returns ``None`` for the three
    ways there is nothing to call: the deployment did not switch remote OCR on, the caller
    declined it for this request, or it is on but has no endpoint (a secret that has not
    landed, which degrades to ``needs_ocr`` rather than taking the service down).

    Returns:
        ``(provider_or_none, enabled_on_this_deployment)``.
    """
    if not settings.remote_ocr_enabled:
        return None, False
    if declined:
        return None, True
    # Imported here, not at module scope: this module is on the default, zero-egress build's
    # import path, and the remote module's whole job is to reach the network.
    from dce.ingest.remote_ocr import RemoteOcrConfig, load_remote_provider

    read = settings.remote_ocr_provider == "azure_read"
    config = RemoteOcrConfig(
        provider=settings.remote_ocr_provider,
        endpoint=(settings.azure_read_endpoint if read else settings.azure_di_endpoint).strip(),
        key=settings.azure_read_key if read else settings.azure_di_key,
        api_version=(
            settings.azure_read_api_version if read else settings.azure_di_api_version
        ),
        model="" if read else settings.azure_di_model,
        timeout_seconds=settings.remote_ocr_timeout_seconds,
        poll_interval_seconds=settings.remote_ocr_poll_interval_seconds,
        max_polls=settings.remote_ocr_max_polls,
    )
    try:
        return load_remote_provider(config, enabled=True), True
    except EngineUnavailable:
        # No endpoint, or the HTTP extra is not installed. Both are "we cannot", not "we
        # refuse", and both are already described by IngestSettings.remote_ocr_problem().
        return None, True


def _why_no_engine(
    settings: IngestSettings,
    enabled: bool,
    requested: bool | None,
    remote_requested: bool | None = None,
) -> str:
    """The clause that distinguishes the ways ``ocr_available`` can be False.

    "We cannot read this" is only actionable if the caller can tell *why* nothing recognised
    it: an operator who has to go turn a setting on, an operator who has to go install an
    extra, and a caller who turned it off themselves need different next steps.

    Every branch returns a **complete sentence, terminated**. ``reason`` is concatenated with
    further prose by its consumers — the console appends "This is not a failed classification…"
    directly after it — and an unterminated clause runs the two sentences together.
    """
    if settings.remote_ocr_enabled:
        if _declined(requested, remote_requested):
            return (
                ". A remote OCR provider is configured here but this request declined it "
                "(local_ocr=false / remote_ocr=false)."
            )
        problem = settings.remote_ocr_problem()
        if problem:
            return f". Remote OCR is switched on here but unusable: {problem}."
        return (
            ". Remote OCR is switched on here but the HTTP client is not installed — see the "
            "azure-ocr extra."
        )
    if not enabled:
        return (
            ". Local OCR is switched off on this deployment, and no remote provider is "
            "configured."
        )
    if requested is False:
        return ". Local OCR is available here but this request declined it (local_ocr=false)."
    return (
        ". Local OCR is switched on here but its engine is not installed — see the "
        "ocr-rapidocr / ocr-tesseract extras."
    )


def _cap_remote_view(view: LayoutView, limits: IngestLimits) -> tuple[bool, list[str]]:
    """Apply the truncating caps to a view a remote provider produced.

    Path (A) applies no caps — a caller-supplied payload is the caller's own problem, and the
    request-body limit already bounds it. Path (B) is different: **we** asked for this payload,
    over a socket, from a service whose response size we do not control, so the same
    ``max_blocks`` / ``max_chars`` ceilings that bound every local parser bound it too.

    Mutates ``view`` in place.

    Returns:
        ``(truncated, caps_hit)``.
    """
    hits: list[str] = []
    if len(view.blocks) > limits.max_blocks:
        del view.blocks[limits.max_blocks :]
        hits.append("max_blocks")
    total = 0
    for index, block in enumerate(view.blocks):
        if total >= limits.max_chars:
            del view.blocks[index:]
            hits.append("max_chars")
            break
        if len(block.text) > limits.max_block_chars:
            block.text = block.text[: limits.max_block_chars]
            if "max_block_chars" not in hits:
                hits.append("max_block_chars")
        total += len(block.text)
    if len(view.tables) > limits.max_tables:
        del view.tables[limits.max_tables :]
        hits.append("max_tables")
    return bool(hits), hits


def _remote_ingest(
    data: bytes,
    media_type: MediaType,
    provider,
    settings: IngestSettings,
    limits: IngestLimits,
    deadline: Deadline,
    result: IngestResult,
    *,
    doc_id: str,
    detection_basis: str,
    started: float,
) -> IngestResult:
    """Recognise a document by **sending it to a third party**, and record that we did.

    The document goes out whole and comes back as the provider's own payload, which is mapped
    by the very adapter a caller-supplied payload would go through
    (:func:`dce.adapters.from_azure_layout` / :func:`~dce.adapters.from_azure_read`). That
    equality is deliberate: provider (B) is provider (A) with the call made here, so a
    reviewer comparing the two paths is comparing who dialled, not what was parsed.
    """
    view = provider.recognize(data, media_type=media_type, deadline=deadline)
    truncated, hits = _cap_remote_view(view, limits)
    view.doc_id = doc_id
    host = getattr(provider, "endpoint", "")
    view.raw = {
        **view.raw,
        "ingested_by": "dce.ingest.remote_ocr",
        "media_type": str(media_type),
        "detected_by": detection_basis,
        "text_source": str(TextSource.remote_ocr),
        "ocr_engine": provider.name,
        # Recorded on the view itself, not only on the result: a stored LayoutView must be
        # able to answer "did this document leave the building to become readable" on its own.
        "ocr_is_remote": True,
        "ocr_endpoint_host": settings.remote_ocr_endpoint_host() or host,
        "truncated": truncated,
    }
    result.view = view
    result.status = IngestStatus.ok
    result.text_source = TextSource.remote_ocr
    result.ocr_engine = provider.name
    result.ocr_available = True
    result.ocr_is_remote = True
    result.ocr_endpoint_host = settings.remote_ocr_endpoint_host()
    result.block_count = len(view.blocks)
    result.char_count = sum(len(b.text) for b in view.blocks)
    result.page_count = len(view.pages) or result.page_count
    result.pages_read = len(view.pages)
    result.truncated = truncated
    result.limits_hit = hits
    result.ms = int((time.perf_counter() - started) * 1000)
    if not view.blocks and not view.tables:
        raise UnsupportedFormat(
            f"{media_type} was analysed by {provider.name} and came back with no text at "
            "all — an empty document cannot be classified, and an empty classification "
            "would read as a model decision"
        )
    return result


def _parse_native(
    data: bytes,
    media_type: MediaType,
    builder: LayoutBuilder,
    limits: IngestLimits,
    deadline: Deadline,
) -> int:
    """Run the parser for a text-bearing format. Returns the page count."""
    # Imported here, not at module scope: each of these pulls in a stdlib parser, and a
    # deployment that only ever sees PDFs should not pay for the XML machinery at import.
    if media_type in {MediaType.docx, MediaType.xlsx, MediaType.pptx, MediaType.odt}:
        from dce.ingest import ooxml
        from dce.ingest.zipsafe import open_archive

        parsers = {
            MediaType.docx: ooxml.parse_docx,
            MediaType.xlsx: ooxml.parse_xlsx,
            MediaType.pptx: ooxml.parse_pptx,
            MediaType.odt: ooxml.parse_odt,
        }
        with open_archive(data, limits, deadline) as archive:
            return parsers[media_type](archive, builder, limits, deadline)

    if media_type is MediaType.msg:
        from dce.ingest.cfb import parse_msg

        parse_msg(data, builder, limits, deadline)
        return 1

    if media_type is MediaType.eml:
        from dce.ingest.markup import parse_eml

        parse_eml(data, builder, limits, deadline)
        return 1

    if media_type is MediaType.rtf:
        from dce.ingest.plain import parse_rtf

        parse_rtf(data, builder, limits, deadline)
        return 1

    text, _encoding = decode_text(data)
    if media_type is MediaType.html:
        from dce.ingest.markup import parse_html

        parse_html(text, builder, limits, deadline)
    elif media_type is MediaType.csv:
        from dce.ingest.plain import parse_csv

        parse_csv(text, builder, limits, deadline)
    else:
        from dce.ingest.plain import parse_txt

        parse_txt(text, builder, deadline)
    return 1


def ingest(
    data: bytes,
    *,
    doc_id: str = "",
    filename: str | None = None,
    limits: IngestLimits | None = None,
    settings: IngestSettings | None = None,
    local_ocr: bool | None = None,
    remote_ocr: bool | None = None,
    ocr_provider: str | None = None,
) -> IngestResult:
    """Turn an uploaded file into a :class:`~dce.models.LayoutView`, or say why not.

    Args:
        data: The whole file.
        doc_id: Carried onto the resulting view.
        filename: Hint for plain-text subtypes only; never chooses a binary parser.
        limits: Resource caps; the deployment's configured limits by default.
        settings: Ingestion settings; :func:`~dce.ingest.settings.get_ingest_settings` by
            default.
        local_ocr: ``False`` to suppress recognition for this request. ``True`` requests it
            but cannot enable it where the deployment has not.
        remote_ocr: The same asymmetry over the network providers. Either flag set to
            ``False`` declines whichever recogniser this deployment configured.
        ocr_provider: Assert which recogniser must be the configured one. Never selects one.

    Returns:
        An :class:`~dce.ingest.result.IngestResult`, with ``status`` either ``ok`` (``view``
        is populated) or ``needs_ocr`` (there was no text to read).

    Raises:
        IngestError: Any subclass, for an upload that could not be parsed. Never anything
            else — a parser's internal exception is translated at its own boundary.
        OcrProviderMismatch: ``ocr_provider`` names something other than the recogniser this
            deployment is configured to use.
        EgressViolation: Only on the remote-OCR path, and only when it was reached without
            the deployment permitting it. Not caught and softened here: a refused disclosure
            must not present as a parse failure.
    """
    started = time.perf_counter()
    settings = settings or get_ingest_settings()
    limits = limits or settings.limits()
    _check_provider_pin(settings, ocr_provider)

    if len(data) > limits.max_bytes:
        raise PayloadTooLarge(
            f"upload is {len(data)} bytes, over the {limits.max_bytes}-byte cap"
        )
    deadline = Deadline(limits.max_seconds)
    detection = detect(data, filename=filename, limits=limits, deadline=deadline)
    media_type = detection.media_type
    provider, ocr_enabled = _resolve_provider(settings, local_ocr)
    # Mutually exclusive with the local one by construction: IngestSettings refuses a
    # configuration with both switched on, so at most one of these is ever non-None.
    remote, _remote_enabled = _resolve_remote_provider(
        settings, declined=_declined(local_ocr, remote_ocr)
    )

    builder = LayoutBuilder(limits, deadline)
    result = IngestResult(
        media_type=media_type,
        detected_by=detection.basis,
        byte_size=len(data),
        ocr_available=(ocr_enabled and provider is not None) or remote is not None,
    )

    def _no_ocr(reason: str) -> IngestResult:
        """The honest refusal, with the clause that says which of the ways it was."""
        result.status = IngestStatus.needs_ocr
        result.text_source = TextSource.none
        result.reason = reason + _why_no_engine(settings, ocr_enabled, local_ocr, remote_ocr)
        result.remedy = NEEDS_OCR_REMEDY
        result.ms = int((time.perf_counter() - started) * 1000)
        return result

    # -- images: the decision point -----------------------------------------
    if media_type in IMAGE_TYPES:
        info = probe(data, media_type)
        result.page_count = info.frames
        if remote is not None:
            return _remote_ingest(
                data, media_type, remote, settings, limits, deadline, result,
                doc_id=doc_id, detection_basis=detection.basis, started=started,
            )
        if provider is None:
            return _no_ocr(needs_ocr_reason(media_type, info))
        pages, truncated = recognize_image(data, media_type, provider, limits, deadline)
        ocr_pages_to_builder(pages, builder, limits)
        result.pages_read = len(pages)
        result.text_source = TextSource.local_ocr
        result.ocr_engine = provider.name
        if truncated:
            builder.truncated = True
            builder.limits_hit.append("max_ocr_pages")

    # -- PDF: text layer, or a scan --------------------------------------------
    elif media_type is MediaType.pdf:
        from dce.ingest.pdf import parse_pdf, scanned_reason

        outcome = parse_pdf(data, builder, limits, deadline, provider=provider)
        result.page_count = outcome.page_count
        result.pages_read = outcome.pages_read
        if outcome.truncated:
            builder.truncated = True
            builder.limits_hit.append("max_pages")
        if outcome.needs_ocr:
            # A scan. The remote provider takes the PDF whole — both Azure products read PDFs
            # natively — so nothing is rasterised and no page renderer is involved.
            if remote is not None:
                return _remote_ingest(
                    data, media_type, remote, settings, limits, deadline, result,
                    doc_id=doc_id, detection_basis=detection.basis, started=started,
                )
            return _no_ocr(scanned_reason(outcome))
        if outcome.ocr_pages:
            ocr_pages_to_builder(outcome.ocr_pages, builder, limits)
            result.text_source = TextSource.local_ocr
            result.ocr_engine = provider.name if provider else ""

    # -- everything else: native text ------------------------------------------
    else:
        result.page_count = _parse_native(data, media_type, builder, limits, deadline)
        result.pages_read = result.page_count

    view = builder.build(
        doc_id=doc_id,
        raw={
            "provider": "dce.ingest",
            "media_type": str(media_type),
            "detected_by": detection.basis,
            "detection_detail": detection.detail,
            "text_source": str(result.text_source),
            "ocr_engine": result.ocr_engine,
            "truncated": builder.truncated,
        },
    )
    result.view = view
    result.block_count = builder.block_count
    result.char_count = builder.char_count
    result.truncated = builder.truncated
    result.limits_hit = list(builder.limits_hit)
    result.page_count = result.page_count or len(view.pages)
    result.ms = int((time.perf_counter() - started) * 1000)

    if not view.blocks and not view.tables:
        # A parseable file with no text at all. Not an abstention and not a scan: the file
        # really is empty, and saying so beats classifying nothing and reporting "unknown".
        raise UnsupportedFormat(
            f"{media_type} parsed cleanly but contains no text — an empty document cannot "
            "be classified, and an empty classification would read as a model decision"
        )
    return result


__all__ = ["IngestError", "IngestOptions", "ingest"]
