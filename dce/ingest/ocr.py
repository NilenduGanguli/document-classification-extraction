"""Local OCR: optional, off by default, and never a network call.

This module exists because of the decision recorded in :mod:`dce.ingest`: an image has no
text, so classifying one *requires* recognition, and sending an unclassified document to a
cloud OCR service is the exact disclosure this service was built to prevent. Local
recognition is the only way to classify an image without breaking the invariant.

Two engines are supported, and the difference between them is worth stating plainly rather
than hiding behind the word "local":

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


#: The closed allowlist. Adding an entry here is a code change and a review, which is the
#: point — see the module docstring.
ENGINES: dict[str, str] = {
    "rapidocr": "in-process ONNX Runtime (recommended)",
    "tesseract": "local tesseract binary via a subprocess",
}


def load_provider(engine: str, *, languages: str = "eng") -> LocalOcrProvider:
    """Construct the named engine.

    Raises:
        EngineUnavailable: The name is not in :data:`ENGINES`, or its packages are missing.
    """
    key = (engine or "").strip().lower()
    if key not in ENGINES:
        raise EngineUnavailable(
            f"unknown local OCR engine {engine!r}; supported: {', '.join(sorted(ENGINES))}"
        )
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
    "LocalOcrProvider",
    "OcrLine",
    "OcrPage",
    "RapidOcrProvider",
    "TesseractProvider",
    "load_provider",
    "ocr_pages_to_builder",
    "provider_or_none",
]
