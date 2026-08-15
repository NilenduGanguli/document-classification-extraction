"""The dispatcher: bytes in, a :class:`~dce.ingest.result.IngestResult` out.

One entry point, :func:`ingest`. It detects the format from content, picks the parser, runs
it under the caps, and returns either a :class:`~dce.models.LayoutView` or the structured
``needs_ocr`` outcome. It raises only the errors in :mod:`dce.ingest.errors`; nothing from a
parser's internals escapes.
"""
from __future__ import annotations

import contextlib
import time

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

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
from dce.ingest.ocr import (
    ENGINES,
    SERVICE_ENGINES,
    LocalOcrProvider,
    ocr_pages_to_builder,
    provider_or_none,
)
from dce.ingest.result import IngestResult, IngestStatus, TextSource
from dce.ingest.settings import (
    TEXT_LAYER_ALWAYS_OCR,
    TEXT_LAYER_TRUST,
    IngestSettings,
    get_ingest_settings,
)
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
    * ``ocr_service`` (accepted under its old name ``remote_ocr``) is the same asymmetry over
      the service providers, and it matters more: a caller who knows this particular document
      may not leave their jurisdiction can always decline, and can never grant. **Either flag
      set to False declines recognition for this request**, whichever recogniser would have
      run — a caller saying "do not run OCR on this" must be honoured whichever kind is
      configured rather than silently applying to the other one.
    * ``ocr_provider`` **chooses among the recognisers this deployment configured, and can
      never add one.** That asymmetry is the whole point. A deployment may configure several
      — an in-process engine and one or both Azure services — and a caller then names the one
      it wants; naming anything else raises
      :class:`~dce.ingest.errors.OcrProviderMismatch` rather than quietly using whatever is
      installed. Pinning ``azure_read`` where no OCR service is configured is therefore a
      refusal, not an enablement.

      Without it the worst failure on this path is silent. A console shows an operator "this
      document will be read by <endpoint>", the operator accepts, the deployment is
      reconfigured to a different provider, and the document is read somewhere nobody
      acknowledged — with every response still reporting, truthfully, that OCR succeeded.
      The pin turns that into an error.
    """

    model_config = ConfigDict(populate_by_name=True)

    filename: str | None = Field(default=None, max_length=512)
    local_ocr: bool | None = None
    #: Decline the OCR service for this request. ``remote_ocr`` is the old name and is still
    #: accepted, so a caller written against the previous API keeps working.
    ocr_service: bool | None = Field(
        default=None, validation_alias=AliasChoices("ocr_service", "remote_ocr")
    )
    #: The provider this request wants, e.g. ``azure_layout``. See the class docstring: chosen
    #: from :meth:`IngestSettings.configured_providers`, never able to extend it.
    ocr_provider: str | None = Field(default=None, max_length=64)
    #: How the document should be READ, before anything classifies it.
    #:
    #: * ``auto`` (default, and what every caller got before this field existed) — read the
    #:   text layer where the file has one, recognise it where it does not.
    #: * ``lexical`` — the text layer only. A file without one comes back ``needs_ocr`` even
    #:   where a recogniser is configured and willing.
    #: * ``optical`` — recognise it, even when a perfectly good text layer is sitting there.
    #:
    #: ``optical`` is why this field exists rather than being inferred. A PDF with a text layer
    #: can be read BOTH ways, and the two readings are not the same document: the text layer
    #: carries the publisher's own characters and no paragraph roles, while Document
    #: Intelligence returns roles — so title and heading zones exist on one reading and not the
    #: other, and the registry's zone-gated anchors can only fire on one of them. Being able to
    #: force the comparison is how an operator finds that out on their own documents instead of
    #: taking it on trust.
    #:
    #: It keeps the asymmetry the other flags have: it selects among what the deployment has
    #: already configured and cannot grant anything. Asking for ``optical`` where no recogniser
    #: is available is a ``needs_ocr`` refusal, exactly as if the file had been a scan.
    read_channel: str = Field(default="auto", pattern="^(auto|lexical|optical)$")


def _selected_provider(settings: IngestSettings, pinned: str | None) -> str:
    """The recogniser this request will use, by name, or ``"none"``.

    A pin is checked whenever it is set, including when the caller also declined recognition.
    It is an assertion about the *deployment*, and an assertion that is wrong is worth saying
    so regardless of whether this particular request would have acted on it — a caller working
    from a stale picture of where documents are read should find that out on the first request,
    not on the first one that happens to contain an image.

    Returns:
        The provider id, or ``"none"`` when this deployment has configured no recogniser.

    Raises:
        OcrProviderMismatch: The pin names something this deployment has not configured.
    """
    wanted = (pinned or "").strip().lower()
    if not wanted:
        return settings.default_provider()
    if settings.is_configured(wanted):
        return wanted
    configured = settings.configured_providers()
    if not configured:
        detail = (
            f"this request asked for ocr_provider={wanted!r}, but no recogniser is configured "
            "on this deployment. The pin chooses among the recognisers an operator configured: "
            "it cannot switch one on"
        )
    else:
        detail = (
            f"this request asked for ocr_provider={wanted!r}, but the recognisers configured "
            f"here are {', '.join(configured)}. Refusing rather than substituting: if a caller "
            "has disclosed one destination to a user, reading the document with a different "
            "one would make that disclosure false"
        )
    raise OcrProviderMismatch(detail)


def _declined(local_ocr: bool | None, ocr_service: bool | None) -> bool:
    """Whether the caller declined recognition, by either flag.

    Either one is enough. A caller saying "do not run OCR on this document" must be obeyed
    whichever kind of recogniser this deployment would have used, rather than silently applying
    to the other one.
    """
    return local_ocr is False or ocr_service is False


def _resolve_local_provider(
    settings: IngestSettings, selected: str, *, declined: bool
) -> LocalOcrProvider | None:
    """The in-process engine for this request, or ``None`` when one will not run."""
    if declined or selected not in ENGINES:
        return None
    return provider_or_none(selected, languages=settings.local_ocr_languages)


def _resolve_service_provider(settings: IngestSettings, selected: str, *, declined: bool):
    """The OCR service provider for this request, or ``None`` when one will not run.

    Constructing the provider is not the permission — :func:`dce.egress.assert_ocr_egress_permitted`
    is, and it runs at every request the provider makes. This returns ``None`` for the three
    ways there is nothing to call: the selected recogniser is not a service provider, the
    caller declined recognition for this request, or the provider is configured but has no
    endpoint (a secret that has not landed, which degrades to ``needs_ocr`` rather than taking
    the service down).
    """
    if declined or selected not in SERVICE_ENGINES:
        return None
    # Imported here, not at module scope: this module is on the default build's import path,
    # and the service module's whole job is to reach another host over a socket.
    from dce.ingest.ocr_service import OcrServiceConfig, load_ocr_service_provider

    read = selected == "azure_read"
    config = OcrServiceConfig(
        provider=selected,
        endpoint=settings.provider_endpoint(selected),
        key=settings.azure_read_key if read else settings.azure_di_key,
        api_version=(
            settings.azure_read_api_version if read else settings.azure_di_api_version
        ),
        model="" if read else settings.azure_di_model,
        timeout_seconds=settings.ocr_service_timeout_seconds,
        poll_interval_seconds=settings.ocr_service_poll_interval_seconds,
        max_polls=settings.ocr_service_max_polls,
    )
    try:
        return load_ocr_service_provider(config, enabled=True)
    except EngineUnavailable:
        # No endpoint, or the HTTP extra is not installed. Both are "we cannot", not "we
        # refuse", and both are already described by IngestSettings.provider_problem().
        return None


def _why_no_engine(settings: IngestSettings, selected: str, *, declined: bool) -> str:
    """The clause that distinguishes the ways ``ocr_available`` can be False.

    "We cannot read this" is only actionable if the caller can tell *why* nothing recognised
    it: an operator who has to go turn a setting on, an operator who has to go install an
    extra, and a caller who turned it off themselves need different next steps.

    Every branch returns a **complete sentence, terminated**. ``reason`` is concatenated with
    further prose by its consumers — the console appends "This is not a failed classification…"
    directly after it — and an unterminated clause runs the two sentences together.
    """
    if selected == "none":
        return (
            ". No recogniser is configured on this deployment — neither an in-process engine "
            "(DCE_INGEST_LOCAL_OCR_ENABLED) nor an OCR service "
            "(DCE_INGEST_OCR_SERVICE_ENABLED)."
        )
    if declined:
        return (
            f". {selected} is configured here but this request declined recognition "
            "(local_ocr=false / ocr_service=false)."
        )
    problem = settings.provider_problem(selected)
    if problem:
        return f". {selected} is configured here but unusable: {problem}."
    if selected in SERVICE_ENGINES:
        return (
            f". The OCR service provider {selected} is configured here but the HTTP client is "
            "not installed — see the azure-ocr extra."
        )
    return (
        f". Local OCR is switched on here but the {selected} engine is not installed — see the "
        "ocr-rapidocr / ocr-tesseract extras."
    )


def _cap_service_view(view: LayoutView, limits: IngestLimits) -> tuple[bool, list[str]]:
    """Apply the truncating caps to a view an OCR service produced.

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


def _service_ingest(
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
    """Recognise a document by **calling the configured OCR service**, and record that we did.

    The document goes out whole and comes back as the provider's own payload, which is mapped
    by the very adapter a caller-supplied payload would go through
    (:func:`dce.adapters.from_azure_layout` / :func:`~dce.adapters.from_azure_read`). That
    equality is deliberate: provider (B) is provider (A) with the call made here, so a
    reviewer comparing the two paths is comparing who dialled, not what was parsed.
    """
    view = provider.recognize(data, media_type=media_type, deadline=deadline)
    truncated, hits = _cap_service_view(view, limits)
    view.doc_id = doc_id
    host = getattr(provider, "endpoint", "")
    view.raw = {
        **view.raw,
        "ingested_by": "dce.ingest.ocr_service",
        "media_type": str(media_type),
        "detected_by": detection_basis,
        "text_source": str(TextSource.ocr_service),
        "ocr_engine": provider.name,
        # Recorded on the view itself, not only on the result: a stored LayoutView must be
        # able to answer "was this document read in this process or by a service" on its own.
        "ocr_via_service": True,
        "ocr_endpoint_host": settings.provider_endpoint_host(provider.name) or host,
        "truncated": truncated,
    }
    result.view = view
    result.status = IngestStatus.ok
    result.text_source = TextSource.ocr_service
    result.ocr_engine = provider.name
    result.ocr_available = True
    result.ocr_via_service = True
    result.ocr_endpoint_host = settings.provider_endpoint_host(provider.name)
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


#: Container members that are pictures. An OOXML package puts them under ``word/media/``,
#: ``xl/media/`` or ``ppt/media/``; ODF uses ``Pictures/``.
_MEDIA_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp")

#: Embedded pictures examined in one container. A deck of a hundred photographs is a scan
#: whichever ten of them are read, and the cap keeps one upload from becoming a hundred
#: recognitions.
MAX_EMBEDDED_IMAGES = 10


def _embedded_images(
    data: bytes, media_type: MediaType, limits: IngestLimits, deadline: Deadline
) -> list[bytes]:
    """Pictures inside a container whose own text came back empty.

    The blind spot this closes: a ``.docx`` whose entire content is a pasted scan parses
    perfectly and yields no text, and the empty-document refusal then called it a document
    with nothing in it — a 415, telling a caller the format was unsupported when in truth the
    format was fine and the content was pixels. For a KYC corpus "scan pasted into a Word
    file" is an ordinary way to receive a document, and it deserves the same ``needs_ocr``
    answer a bare JPEG gets.

    Returns an empty list for formats with no container to look in, which keeps the
    empty-document refusal for files that really are empty.
    """
    if media_type not in {MediaType.docx, MediaType.xlsx, MediaType.pptx, MediaType.odt}:
        return []
    from dce.ingest.zipsafe import open_archive

    found: list[bytes] = []
    try:
        with open_archive(data, limits, deadline) as archive:
            for name in archive.names():
                if len(found) >= MAX_EMBEDDED_IMAGES:
                    break
                if name.lower().endswith(_MEDIA_SUFFIXES):
                    with contextlib.suppress(Exception):
                        found.append(archive.read(name))
    except IngestError:
        # The archive already parsed once to get here, so a failure now is not worth turning
        # into a different error than the caller was about to receive.
        return []
    return found


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
    ocr_service: bool | None = None,
    ocr_provider: str | None = None,
    read_channel: str = "auto",
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
        ocr_service: The same asymmetry over the service providers. Either flag set to
            ``False`` declines whichever recogniser this request would have used.
        ocr_provider: Choose among the recognisers this deployment configured. Never adds
            one.

    Returns:
        An :class:`~dce.ingest.result.IngestResult`, with ``status`` either ``ok`` (``view``
        is populated) or ``needs_ocr`` (there was no text to read).

    Raises:
        IngestError: Any subclass, for an upload that could not be parsed. Never anything
            else — a parser's internal exception is translated at its own boundary.
        OcrProviderMismatch: ``ocr_provider`` names a recogniser this deployment has not
            configured.
        EgressViolation: Only on the OCR-service path, and only when it was reached without
            the deployment having configured one. Not caught and softened here: a refused call
            must not present as a parse failure.
    """
    started = time.perf_counter()
    settings = settings or get_ingest_settings()
    limits = limits or settings.limits()
    selected = _selected_provider(settings, ocr_provider)
    # ``lexical`` declines recognition for this request as firmly as ``local_ocr=False`` does:
    # the caller asked for the text layer and nothing else, so a file without one is a refusal
    # rather than an invitation to go and recognise it. Folding it into ``declined`` rather than
    # branching later means every downstream message — which engine, why not, what to do — is
    # the one the existing refusal path already composes.
    declined = _declined(local_ocr, ocr_service) or read_channel == "lexical"

    if len(data) > limits.max_bytes:
        raise PayloadTooLarge(
            f"upload is {len(data)} bytes, over the {limits.max_bytes}-byte cap"
        )
    deadline = Deadline(limits.max_seconds)
    detection = detect(data, filename=filename, limits=limits, deadline=deadline)
    media_type = detection.media_type
    # At most one of these is ever non-None: `selected` names one recogniser, and it is
    # either an in-process engine or a service provider.
    provider = _resolve_local_provider(settings, selected, declined=declined)
    service = _resolve_service_provider(settings, selected, declined=declined)

    builder = LayoutBuilder(limits, deadline)
    result = IngestResult(
        media_type=media_type,
        detected_by=detection.basis,
        byte_size=len(data),
        ocr_available=provider is not None or service is not None,
    )

    def _no_ocr(reason: str) -> IngestResult:
        """The honest refusal, with the clause that says which of the ways it was."""
        result.status = IngestStatus.needs_ocr
        result.text_source = TextSource.none
        result.reason = reason + _why_no_engine(settings, selected, declined=declined)
        result.remedy = NEEDS_OCR_REMEDY
        result.ms = int((time.perf_counter() - started) * 1000)
        return result

    # -- images: the decision point -----------------------------------------
    if media_type in IMAGE_TYPES:
        info = probe(data, media_type)
        result.page_count = info.frames
        if service is not None:
            return _service_ingest(
                data, media_type, service, settings, limits, deadline, result,
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
        from dce.ingest.pdf import parse_pdf, partial_reason, scanned_reason

        # ``optical`` skips the text layer even where there is one, so the recogniser reads the
        # page the way it would read a scan. This is the only branch where the two readings of
        # one file diverge, and the divergence is the point: the text layer has no paragraph
        # roles, so a zone-gated anchor cannot fire on it, while Document Intelligence supplies
        # roles and it can. Same bytes, different evidence, sometimes a different doctype.
        #
        # ``text_layer_policy=always_ocr`` is the same instruction said by the deployment
        # instead of the request, so it takes the same branch. It cannot override a caller who
        # DECLINED recognition — `declined` has already been folded into `provider`/`service`
        # being None, and the caller-may-decline-never-grant asymmetry outranks a deployment
        # preference — but where recognition is available it means the text layer is not read.
        always_ocr = settings.text_layer() == TEXT_LAYER_ALWAYS_OCR and not declined
        if read_channel == "optical" or always_ocr:
            if service is not None:
                return _service_ingest(
                    data, media_type, service, settings, limits, deadline, result,
                    doc_id=doc_id, detection_basis=detection.basis, started=started,
                )
            if provider is None and read_channel == "optical":
                return _no_ocr(
                    "optical reading was requested for this pdf, but no recogniser is "
                    "available on this deployment, so there is nothing to read it with"
                )

        outcome = parse_pdf(
            data,
            builder,
            limits,
            deadline,
            provider=provider,
            strict=settings.text_layer() != TEXT_LAYER_TRUST,
            force_ocr=always_ocr,
        )
        result.page_count = outcome.page_count
        result.pages_read = outcome.pages_read
        if outcome.pages_read < outcome.page_count:
            builder.truncated = True
            builder.limits_hit.append("max_pages")
        if outcome.ocr_truncated:
            # Named separately from max_pages: "we read fewer pages than the file has" and
            # "we recognised fewer pages than needed recognising" send a reader to different
            # settings, and a single cap name would hide which one bit.
            builder.truncated = True
            builder.limits_hit.append("max_ocr_pages")
        if outcome.needs_ocr or outcome.unread_pages:
            # A scan, or a document that is partly one. The service provider takes the PDF
            # WHOLE — both Azure products read PDFs natively — so nothing is rasterised and no
            # page renderer is involved.
            #
            # Whole, and not just the unread pages, on purpose. azure_layout returns paragraph
            # roles while a PDF text layer carries none, so a page-by-page merge would give one
            # document two grades of evidence and let a zone-gated anchor fire or not according
            # to which pages happened to be scanned. One document, one reading.
            if service is not None:
                return _service_ingest(
                    data, media_type, service, settings, limits, deadline, result,
                    doc_id=doc_id, detection_basis=detection.basis, started=started,
                )
            if outcome.needs_ocr:
                return _no_ocr(scanned_reason(outcome))
            # Partly readable, and nothing here can read the rest. Keep what the text pages
            # hold — a classification on part of a document beats none — but say so, because
            # the alternative is the silent short read this whole branch exists to end.
            builder.truncated = True
            builder.limits_hit.append("unread_pages")
            result.reason = partial_reason(outcome)
            result.remedy = NEEDS_OCR_REMEDY
        if outcome.ocr_pages:
            ocr_pages_to_builder(outcome.ocr_pages, builder, limits)
            # `mixed` when some pages kept their own characters and others were recognised.
            # Folding that into `local_ocr` would misreport the document's provenance, and
            # provenance is the field a reviewer splits every accuracy rate on.
            result.text_source = (
                TextSource.mixed
                if any(v.adequate for v in outcome.page_verdicts)
                else TextSource.local_ocr
            )
            result.ocr_engine = provider.name if provider else ""

    # -- everything else: native text ------------------------------------------
    else:
        result.page_count = _parse_native(data, media_type, builder, limits, deadline)
        result.pages_read = result.page_count

        # A container whose own text came back empty may still be carrying the document as a
        # picture — a scan pasted into a Word file, which for a KYC corpus is an ordinary way
        # to receive one. Until now that reached the empty-document refusal below and came
        # back 415 "unsupported format", which was false twice over: the format was fine, and
        # the document was not empty.
        if not builder.block_count:
            embedded = _embedded_images(data, media_type, limits, deadline)
            if embedded:
                if service is not None:
                    return _service_ingest(
                        data, media_type, service, settings, limits, deadline, result,
                        doc_id=doc_id, detection_basis=detection.basis, started=started,
                    )
                if provider is None:
                    return _no_ocr(
                        f"{media_type} carries no text of its own, but {len(embedded)} "
                        "embedded image(s) — the document is a picture inside a container, "
                        "so classifying it would require optical recognition"
                    )
                for number, image in enumerate(embedded, start=1):
                    deadline.check(f"embedded.image{number}")
                    with contextlib.suppress(IngestError):
                        result_page = provider.recognize(image, page=number, deadline=deadline)
                        ocr_pages_to_builder([result_page], builder, limits)
                if builder.block_count:
                    result.text_source = TextSource.local_ocr
                    result.ocr_engine = provider.name
                    result.pages_read = len(embedded)

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
