"""Local OCR: optional, off by default, and never a network call.

This module exists because of the decision recorded in :mod:`dce.ingest`: an image has no
text, so classifying one *requires* recognition, and sending an unclassified document to a
cloud OCR service is the exact disclosure this service was built to prevent. Local
recognition is the only way to classify an image without breaking the invariant.

**There is a second kind of provider, and it is kept at arm's length from this one.**
:mod:`dce.ingest.ocr_service` implements ``azure_read`` and ``azure_layout``, which recognise
a document by *calling an OCR service over the network* — in this deployment, a service the
operator runs; in another, a vendor's. Which of those it is cannot be read off a hostname, so
it is declared (``DCE_INGEST_OCR_SERVICE_TRUST_BOUNDARY``) rather than guessed at. What the
code knows on its own is narrower and is expressed in the type system rather than in a naming
convention: :data:`PROVIDERS` maps every provider id to an :class:`OcrProvider` record
carrying ``service: bool`` — is this recogniser reached by a call, or does it run in this
process — and every decision that turns on that architectural fact reads the flag.
:data:`ENGINES` below stays what it always was — the closed allowlist of IN-PROCESS engines —
and :func:`load_provider` still refuses everything not in it, including the service ids.

Two local engines are supported, and the difference between them is worth stating plainly
rather than hiding behind the word "local":

``rapidocr``
    RapidOCR's ONNX models, executed by ``onnxruntime`` **inside this process**. No
    subprocess, no binary to install, no socket. This is the recommended engine and the
    default when OCR is switched on.

``tesseract``
    ``pytesseract``, which writes the image to a temporary file and runs the ``tesseract``
    binary as a **subprocess**. That is still zero-egress — the subprocess opens no socket
    and the bytes never leave the host — but it is not literally in-process, and a reviewer
    who was told "in-process" and later found a ``fork``/``exec`` would be right to be
    annoyed. It is offered because tesseract has language packs RapidOCR does not.

Neither engine is a base dependency. Both are declared as optional extras
(``.[ocr-rapidocr]``, ``.[ocr-tesseract]``), so the default install has no OCR code in it at
all and an auditor reading ``pyproject.toml`` can see that in one line.

The provider registry is a closed allowlist. There is deliberately no "load the engine named
in this environment variable" hook: a pluggable OCR backend is a pluggable way to hand an
unclassified document to whatever the operator pointed the plugin at.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Protocol

from dce.ingest.errors import EngineUnavailable, MalformedDocument
from dce.ingest.limits import Deadline, IngestLimits


@dataclass
class OcrLine:
    """One recognised line."""

    text: str
    page: int = 1
    #: Quad in the Azure convention (8 floats), when the engine gives geometry.
    bbox: list[float] | None = None
    confidence: float = 0.0


@dataclass
class OcrPage:
    """Everything recognised on one page/frame."""

    page: int = 1
    width: float = 0.0
    height: float = 0.0
    lines: list[OcrLine] = field(default_factory=list)


class LocalOcrProvider(Protocol):
    """What every engine must offer. Deliberately tiny."""

    name: str

    def recognize(self, image: bytes, *, page: int, deadline: Deadline) -> OcrPage:
        """Recognise one rasterised page. Must not touch the network."""
        ...


def _require(module: str, extra: str):
    try:
        import importlib

        return importlib.import_module(module)
    except ImportError as exc:
        raise EngineUnavailable(
            f"local OCR needs {module!r}, which is not installed. Install the optional "
            f"extra: pip install '.[{extra}]'. Until then an image returns needs_ocr, which "
            "is the honest answer rather than a guess."
        ) from exc


class RapidOcrProvider:
    """RapidOCR (PP-OCR models under ONNX Runtime), executed in this process."""

    name = "rapidocr"

    def __init__(self) -> None:
        module = _require("rapidocr_onnxruntime", "ocr-rapidocr")
        self._np = _require("numpy", "ocr-rapidocr")
        self._pil = _require("PIL.Image", "ocr-rapidocr")
        self._engine = module.RapidOCR()

    def recognize(self, image: bytes, *, page: int, deadline: Deadline) -> OcrPage:
        deadline.check(f"rapidocr.page{page}")
        try:
            with self._pil.open(io.BytesIO(image)) as handle:
                array = self._np.array(handle.convert("RGB"))
                height, width = array.shape[0], array.shape[1]
        except (OSError, ValueError) as exc:
            raise MalformedDocument(f"image is not decodable: {exc}") from exc
        result, _elapsed = self._engine(array)
        lines: list[OcrLine] = []
        for entry in result or []:
            box, text, score = entry[0], entry[1], entry[2]
            quad = [float(coordinate) for point in box for coordinate in point][:8]
            lines.append(
                OcrLine(
                    text=str(text),
                    page=page,
                    bbox=quad if len(quad) == 8 else None,
                    confidence=float(score or 0.0),
                )
            )
        return OcrPage(page=page, width=float(width), height=float(height), lines=lines)


class TesseractProvider:
    """Tesseract via ``pytesseract`` — a local subprocess, not an in-process library."""

    name = "tesseract"

    def __init__(self, languages: str = "eng") -> None:
        self._pytesseract = _require("pytesseract", "ocr-tesseract")
        self._pil = _require("PIL.Image", "ocr-tesseract")
        self._languages = languages or "eng"

    def recognize(self, image: bytes, *, page: int, deadline: Deadline) -> OcrPage:
        deadline.check(f"tesseract.page{page}")
        try:
            handle = self._pil.open(io.BytesIO(image))
        except (OSError, ValueError) as exc:
            raise MalformedDocument(f"image is not decodable: {exc}") from exc
        with handle:
            width, height = handle.size
            data = self._pytesseract.image_to_data(
                handle,
                lang=self._languages,
                output_type=self._pytesseract.Output.DICT,
                timeout=max(1, int(min(deadline.remaining, 600))),
            )

        # image_to_data returns one row per word; group them back into lines by the
        # (block, paragraph, line) key tesseract already assigns.
        grouped: dict[tuple[int, int, int], list[tuple[str, float, list[int]]]] = {}
        for index, text in enumerate(data.get("text", [])):
            if not str(text).strip():
                continue
            key = (
                int(data["block_num"][index]),
                int(data["par_num"][index]),
                int(data["line_num"][index]),
            )
            box = [
                int(data["left"][index]),
                int(data["top"][index]),
                int(data["width"][index]),
                int(data["height"][index]),
            ]
            confidence = float(data.get("conf", [])[index] or 0.0)
            grouped.setdefault(key, []).append((str(text), confidence, box))

        lines: list[OcrLine] = []
        for key in sorted(grouped):
            words = grouped[key]
            x0 = min(b[0] for _, _, b in words)
            y0 = min(b[1] for _, _, b in words)
            x1 = max(b[0] + b[2] for _, _, b in words)
            y1 = max(b[1] + b[3] for _, _, b in words)
            lines.append(
                OcrLine(
                    text=" ".join(w for w, _, _ in words),
                    page=page,
                    bbox=[
                        float(x0), float(y0), float(x1), float(y0),
                        float(x1), float(y1), float(x0), float(y1),
                    ],
                    confidence=sum(c for _, c, _ in words) / len(words) / 100.0,
                )
            )
        return OcrPage(page=page, width=float(width), height=float(height), lines=lines)


#: The closed allowlist of LOCAL engines. Adding an entry here is a code change and a review,
#: which is the point — see the module docstring. Nothing in this dict opens a socket, and
#: :func:`load_provider` accepts nothing outside it.
ENGINES: dict[str, str] = {
    "rapidocr": "in-process ONNX Runtime (recommended)",
    "tesseract": "local tesseract binary via a subprocess",
}

#: The allowlist of SERVICE providers, implemented in :mod:`dce.ingest.ocr_service`. Also
#: closed, also a code change to extend, and deliberately a *separate* dict: whether reading a
#: document is an in-process call or a call to another host is an architectural fact an
#: operator and an auditor both ask about, and it must not be answerable only by recognising a
#: vendor's name in a string.
SERVICE_ENGINES: dict[str, str] = {
    "azure_read": (
        "Azure AI Vision Read v3.2 — lines and words only, no paragraph roles; read by a "
        "service call"
    ),
    "azure_layout": (
        "Azure AI Document Intelligence v4.0 prebuilt-layout — paragraph roles, tables and "
        "selection marks; read by a service call"
    ),
}


#: What each provider hands back, in the only terms that change a decision.
#:
#: ``roles``
#:     Paragraph roles are predicted, so ``Zone.title``/``Zone.heading`` exist and a zone-gated
#:     anchor can fire.
#: ``lines``
#:     Text and geometry only. Every block lands in ``Zone.body`` — see
#:     :func:`ocr_pages_to_builder` and :func:`dce.adapters.from_azure_read` — so a title-gated
#:     anchor cannot fire *however clearly the words are on the page*.
#:
#: Reported on ``/readyz`` rather than left for a console to infer from a vendor name, because
#: it is the whole difference between two providers that both "did OCR", and it decides which
#: evidence was even reachable. A console that guessed this would eventually guess wrong, and
#: the direction it would be wrong in is telling a reviewer an anchor *failed* when in fact it
#: could never have been evaluated.
_STRUCTURE: dict[str, str] = {
    "rapidocr": "lines",
    "tesseract": "lines",
    "azure_read": "lines",
    "azure_layout": "roles",
}


@dataclass(frozen=True)
class OcrProvider:
    """One recognition provider, and the only fact about it that governs anything.

    ``service`` is not documentation. It answers one architectural question — is this
    recogniser reached by a call to another host, or does it run inside this process — and it
    is what :mod:`dce.ingest.pipeline` branches on before it will hand a provider any bytes,
    what ``/readyz`` reports, and what :func:`dce.egress.assert_ocr_egress_permitted` is asked
    about. A future provider that forgets to set it does not silently become in-process: it is
    not in :data:`PROVIDERS` at all, and neither loader will construct it.

    Note what this flag deliberately does **not** say: whose network the call lands on. That is
    the deployment's declaration (``DCE_INGEST_OCR_SERVICE_TRUST_BOUNDARY``), because a
    hostname is not evidence of ownership and code that guessed would be asserting something it
    does not know.
    """

    name: str
    service: bool
    summary: str
    #: ``roles`` or ``lines`` — see :data:`_STRUCTURE`. Defaults to ``lines``, the assumption
    #: that claims *less* evidence was available, so a provider added without one is
    #: under-credited rather than over-credited.
    structure: str = "lines"


#: Every provider this service can be configured to use, in-process and service, with the flag
#: that separates them. The single source of truth for ``/readyz`` and the pipeline.
PROVIDERS: dict[str, OcrProvider] = {
    **{
        name: OcrProvider(
            name=name, service=False, summary=summary, structure=_STRUCTURE.get(name, "lines")
        )
        for name, summary in ENGINES.items()
    },
    **{
        name: OcrProvider(
            name=name, service=True, summary=summary, structure=_STRUCTURE.get(name, "lines")
        )
        for name, summary in SERVICE_ENGINES.items()
    },
}


def provider_info(name: str) -> OcrProvider | None:
    """The :class:`OcrProvider` record for ``name``, or ``None`` when it is not a provider."""
    return PROVIDERS.get((name or "").strip().lower())


def is_service_provider(name: str) -> bool:
    """Whether ``name`` recognises a document by calling an OCR service rather than in-process.

    ``False`` for an unknown name: an id that names no provider cannot be configured, so
    nothing is ever sent to it. Callers that need to *reject* an unknown id do that separately.
    """
    info = provider_info(name)
    return bool(info and info.service)


def load_provider(engine: str, *, languages: str = "eng") -> LocalOcrProvider:
    """Construct the named LOCAL engine.

    Still a closed allowlist over :data:`ENGINES` alone. A service provider id is refused here
    with the same error as a made-up one, and says where it does belong — loading a service
    provider through the in-process loader is exactly the confusion that would put a document
    on the wire without anybody choosing it.

    Raises:
        EngineUnavailable: The name is not in :data:`ENGINES`, or its packages are missing.
    """
    key = (engine or "").strip().lower()
    if key not in ENGINES:
        detail = (
            f"unknown local OCR engine {engine!r}; supported: {', '.join(sorted(ENGINES))}"
        )
        if key in SERVICE_ENGINES:
            detail += (
                f". {key!r} is an OCR SERVICE provider: it recognises a document by sending it "
                "to an OCR endpoint this deployment configures, before the doctype is known. "
                "It is not loadable here. See dce.ingest.ocr_service, and "
                "DCE_INGEST_OCR_SERVICE_ENABLED — configuring an endpoint documents are sent "
                "to is a deliberate, auditable act and not a tuning knob."
            )
        raise EngineUnavailable(detail)
    if key == "rapidocr":
        return RapidOcrProvider()
    return TesseractProvider(languages=languages)


def provider_or_none(engine: str, *, languages: str = "eng") -> LocalOcrProvider | None:
    """:func:`load_provider`, returning ``None`` instead of raising when unavailable.

    Used by the ``/readyz`` style reporting path, where "is OCR usable here" is a question,
    not an error.
    """
    try:
        return load_provider(engine, languages=languages)
    except EngineUnavailable:
        return None


def ocr_pages_to_builder(pages: list[OcrPage], builder, limits: IngestLimits) -> None:
    """Write recognised pages into a :class:`~dce.ingest.builder.LayoutBuilder`.

    **Every block is body.** An OCR engine returns text and geometry, not roles. Inferring
    "this line is big and near the top, so it is the title" is exactly the promotion
    :func:`dce.adapters.from_plain_text` refuses to make, and on an OCR payload — the least
    reliable text in the system — it would be the worst place to start guessing.
    """
    from dce.models import Zone

    for page in pages:
        builder.page(page.page, width=page.width, height=page.height, unit="pixel")
        for line in page.lines:
            if builder.full:
                return
            builder.block(line.text, zone=Zone.body, page=page.page, bbox=line.bbox)


__all__ = [
    "ENGINES",
    "PROVIDERS",
    "SERVICE_ENGINES",
    "LocalOcrProvider",
    "OcrLine",
    "OcrPage",
    "OcrProvider",
    "RapidOcrProvider",
    "TesseractProvider",
    "is_service_provider",
    "load_provider",
    "ocr_pages_to_builder",
    "provider_info",
    "provider_or_none",
]
