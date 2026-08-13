"""Images: the case where there is nothing to extract.

A JPEG of a passport carries no text. Neither does a TIFF of a bank statement, nor a PDF
whose every page is one scanned picture. That is not a degraded version of a text document —
it is a different situation, and the whole of :mod:`dce.ingest`'s design decision turns on
refusing to blur the two.

What this module does, in order:

1. **Probe the pixels with the standard library** — dimensions, and the frame count of a
   multi-page TIFF — so the ``needs_ocr`` answer can at least say *what* the caller sent
   ("a 3-page TIFF, 2480x3508") rather than "an image".
2. **If local OCR is switched on**, rasterise the frames and recognise them in-process.
3. **Otherwise return ``needs_ocr``** with a reason and a remedy.

Step 3 is the default. It is not a limitation; it is the answer.
"""
from __future__ import annotations

import io
import struct
from dataclasses import dataclass

from dce.ingest.detect import MediaType
from dce.ingest.errors import MalformedDocument
from dce.ingest.limits import Deadline, IngestLimits
from dce.ingest.ocr import LocalOcrProvider, OcrPage

#: Cap on IFDs walked when counting TIFF frames. A malformed chain is the reason this exists.
_MAX_TIFF_IFDS = 4096


@dataclass(frozen=True)
class ImageInfo:
    """What the header says about the picture. Never decoded, so always cheap."""

    width: int = 0
    height: int = 0
    frames: int = 1

    def describe(self) -> str:
        size = f"{self.width}x{self.height}" if self.width and self.height else "unknown size"
        return f"{self.frames} frame(s), {size}"


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24:
        return (0, 0)
    width, height = struct.unpack_from(">II", data, 16)
    return (int(width), int(height))


def _gif_size(data: bytes) -> tuple[int, int]:
    if len(data) < 10:
        return (0, 0)
    width, height = struct.unpack_from("<HH", data, 6)
    return (int(width), int(height))


def _bmp_size(data: bytes) -> tuple[int, int]:
    if len(data) < 26:
        return (0, 0)
    width, height = struct.unpack_from("<ii", data, 18)
    return (abs(int(width)), abs(int(height)))


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Walk the JPEG marker segments to the frame header."""
    index = 2
    end = len(data)
    while index + 9 < end:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = struct.unpack_from(">H", data, index + 2)[0]
        # SOF0..SOF15, excluding the DHT/JPG/DAC markers interleaved in that range.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack_from(">HH", data, index + 5)
            return (int(width), int(height))
        if length <= 0:
            break
        index += 2 + length
    return (0, 0)


def _tiff_info(data: bytes) -> ImageInfo:
    """Dimensions of the first IFD and the number of IFDs (pages) in the chain."""
    if len(data) < 8:
        return ImageInfo()
    endian = "<" if data[:2] == b"II" else ">"
    try:
        offset = struct.unpack_from(f"{endian}I", data, 4)[0]
    except struct.error:
        return ImageInfo()
    frames = 0
    width = height = 0
    seen: set[int] = set()
    while 0 < offset < len(data) - 2 and frames < _MAX_TIFF_IFDS:
        if offset in seen:
            break                                   # a self-referential IFD chain
        seen.add(offset)
        try:
            count = struct.unpack_from(f"{endian}H", data, offset)[0]
        except struct.error:
            break
        if frames == 0:
            for entry in range(count):
                base = offset + 2 + entry * 12
                if base + 12 > len(data):
                    break
                tag, kind = struct.unpack_from(f"{endian}HH", data, base)
                if tag not in (256, 257):
                    continue
                value = (
                    struct.unpack_from(f"{endian}H", data, base + 8)[0]
                    if kind == 3
                    else struct.unpack_from(f"{endian}I", data, base + 8)[0]
                )
                if tag == 256:
                    width = int(value)
                else:
                    height = int(value)
        frames += 1
        next_offset = offset + 2 + count * 12
        if next_offset + 4 > len(data):
            break
        offset = struct.unpack_from(f"{endian}I", data, next_offset)[0]
    return ImageInfo(width=width, height=height, frames=max(1, frames))


def probe(data: bytes, media_type: MediaType) -> ImageInfo:
    """Header-only inspection of an image. Never decodes pixels."""
    if media_type is MediaType.png:
        width, height = _png_size(data)
        return ImageInfo(width, height, 1)
    if media_type is MediaType.jpeg:
        width, height = _jpeg_size(data)
        return ImageInfo(width, height, 1)
    if media_type is MediaType.gif:
        width, height = _gif_size(data)
        return ImageInfo(width, height, 1)
    if media_type is MediaType.bmp:
        width, height = _bmp_size(data)
        return ImageInfo(width, height, 1)
    if media_type is MediaType.tiff:
        return _tiff_info(data)
    # WEBP and HEIC/AVIF carry their dimensions inside typed boxes whose parsing is a real
    # format reader; the size is cosmetic here, so it is left unknown rather than guessed.
    return ImageInfo()


def recognize_image(
    data: bytes,
    media_type: MediaType,
    provider: LocalOcrProvider,
    limits: IngestLimits,
    deadline: Deadline,
) -> tuple[list[OcrPage], bool]:
    """Run a local engine over every frame, up to ``max_ocr_pages``.

    Returns:
        ``(pages, truncated)``.

    Raises:
        MalformedDocument: The image cannot be decoded at all.
    """
    info = probe(data, media_type)
    if info.frames <= 1:
        return [provider.recognize(data, page=1, deadline=deadline)], False

    # Multi-frame: split with Pillow, which is present whenever a provider is (both OCR
    # extras depend on it). If it is somehow not, the first frame is still worth having.
    try:
        import PIL.Image as pil
    except ImportError:
        return [provider.recognize(data, page=1, deadline=deadline)], True

    pages: list[OcrPage] = []
    truncated = info.frames > limits.max_ocr_pages
    try:
        with pil.open(io.BytesIO(data)) as handle:
            for index in range(min(info.frames, limits.max_ocr_pages)):
                deadline.check(f"image.frame{index + 1}")
                handle.seek(index)
                buffer = io.BytesIO()
                handle.convert("RGB").save(buffer, format="PNG")
                pages.append(
                    provider.recognize(buffer.getvalue(), page=index + 1, deadline=deadline)
                )
    except (OSError, ValueError, EOFError) as exc:
        if not pages:
            raise MalformedDocument(f"image is not decodable: {exc}") from exc
        truncated = True
    return pages, truncated


def needs_ocr_reason(media_type: MediaType, info: ImageInfo) -> str:
    """The sentence a caller sees when we refuse to guess at an image."""
    return (
        f"{media_type} image ({info.describe()}) carries no text layer — there is nothing to "
        "extract, and classifying it would require optical recognition"
    )


#: The other half of the message: what to do about it. Three routes, listed in the order a
#: deployment should prefer them. The first two keep every unclassified document inside a
#: boundary somebody already owns; only the third puts one on the wire from here.
NEEDS_OCR_REMEDY = (
    "Either (1) run OCR wherever your policy already allows a third party to see an "
    "unclassified document, and resubmit the result as 'text', 'layout', "
    "'azure_analyze_result', 'azure_read_result' or 'des_ocr' — on that path this service "
    "opens no socket at all; or (2) enable local in-process OCR here "
    "(DCE_INGEST_LOCAL_OCR_ENABLED=true, with the ocr-rapidocr or ocr-tesseract extra "
    "installed), which keeps the document in this process at a real cost in accuracy; or "
    "(3) configure a remote OCR provider (DCE_INGEST_REMOTE_OCR_ENABLED=true), which makes "
    "THIS service transmit documents whose type is not yet known to a third party. (3) is "
    "off by default and /readyz reports every deployment that has taken it."
)


__all__ = ["NEEDS_OCR_REMEDY", "ImageInfo", "needs_ocr_reason", "probe", "recognize_image"]
