"""What is this file, actually? Decided from the bytes.

**The filename never chooses the parser.** A mis-named upload is the common case (a caller's
CMS renames everything to ``.pdf``) and a deliberately mis-named upload is the attack: if the
extension picked the parser, an attacker would choose which of our parsers runs on their
bytes, and the safest parser is not the one they would pick. So detection is:

1. **Magic bytes** decide the *family* — ZIP, OLE2/CFB, PDF, RTF, or one of the image
   signatures. This is not overridable.
2. **Structure inside the container** decides the member of that family — ``word/document.xml``
   makes a ZIP a DOCX, ``xl/workbook.xml`` makes it an XLSX. Also not overridable.
3. **Only within the plain-text family** — where there is genuinely no magic to read, because
   TXT, CSV, HTML and EML are all just text — is a filename *hint* consulted, and only to
   break a tie the content sniffer left open. Content still wins whenever it is confident:
   a file called ``notes.txt`` that begins ``<!DOCTYPE html>`` is HTML.

The practical consequence is that the worst a lying filename can do is make us read text as
text in a slightly different shape. It cannot aim an image decoder or an archive expander at
bytes that were not that format.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass

from dce.ingest.errors import UnsupportedFormat
from dce.ingest.limits import Deadline, IngestLimits
from dce.ingest.zipsafe import open_archive


class MediaType(enum.StrEnum):
    """Every type this package recognises, including the ones it refuses."""

    # -- native text -------------------------------------------------------
    pdf = "pdf"
    docx = "docx"
    xlsx = "xlsx"
    pptx = "pptx"
    odt = "odt"
    rtf = "rtf"
    txt = "txt"
    csv = "csv"
    html = "html"
    eml = "eml"
    msg = "msg"
    # -- images (no text layer, by definition) ------------------------------
    jpeg = "jpeg"
    png = "png"
    tiff = "tiff"
    bmp = "bmp"
    webp = "webp"
    heic = "heic"
    gif = "gif"


#: Types that carry extractable text without any recognition step.
NATIVE_TEXT_TYPES: frozenset[MediaType] = frozenset(
    {
        MediaType.pdf,
        MediaType.docx,
        MediaType.xlsx,
        MediaType.pptx,
        MediaType.odt,
        MediaType.rtf,
        MediaType.txt,
        MediaType.csv,
        MediaType.html,
        MediaType.eml,
        MediaType.msg,
    }
)

#: Types that are pixels. There is no text to extract; see :mod:`dce.ingest` on why that is
#: the interesting case rather than an inconvenient one.
IMAGE_TYPES: frozenset[MediaType] = frozenset(
    {
        MediaType.jpeg,
        MediaType.png,
        MediaType.tiff,
        MediaType.bmp,
        MediaType.webp,
        MediaType.heic,
        MediaType.gif,
    }
)


def is_image(media_type: MediaType) -> bool:
    return media_type in IMAGE_TYPES


@dataclass(frozen=True)
class Detection:
    """What we decided, and on what basis — so a wrong answer is diagnosable."""

    media_type: MediaType
    #: ``"magic"`` | ``"container"`` | ``"text-sniff"`` | ``"text-sniff+hint"``
    basis: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------
_PDF_MAGIC = b"%PDF-"
#: The PDF spec allows the header anywhere in the first 1 KB, and real-world PDFs use it.
_PDF_SEARCH_WINDOW = 1024

_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: Fixed-offset-zero image signatures. Order matters only in that longer ones come first.
_IMAGE_MAGICS: tuple[tuple[bytes, MediaType], ...] = (
    (b"\x89PNG\r\n\x1a\n", MediaType.png),
    (b"\xff\xd8\xff", MediaType.jpeg),
    (b"GIF87a", MediaType.gif),
    (b"GIF89a", MediaType.gif),
    (b"II\x2a\x00", MediaType.tiff),      # little-endian TIFF
    (b"MM\x00\x2a", MediaType.tiff),      # big-endian TIFF
    (b"II\x2b\x00", MediaType.tiff),      # BigTIFF
    (b"MM\x00\x2b", MediaType.tiff),
    (b"BM", MediaType.bmp),
)

#: ISO-BMFF brands that mean "this is a still image", read from the ``ftyp`` box at offset 4.
_HEIF_BRANDS = frozenset(
    {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1", b"avif", b"avis"}
)

_RTF_MAGIC = b"{\\rt"

#: OOXML/ODF marker parts. First match wins, so the more specific entries come first.
_ARCHIVE_MARKERS: tuple[tuple[str, MediaType], ...] = (
    ("word/document.xml", MediaType.docx),
    ("xl/workbook.xml", MediaType.xlsx),
    ("ppt/presentation.xml", MediaType.pptx),
)

_ODF_TEXT_MIMETYPES = (
    b"application/vnd.oasis.opendocument.text",
    b"application/vnd.oasis.opendocument.text-template",
)

#: MSG streams are named ``__substg1.0_<tag>``; the storage prefix is enough to identify one.
#: CFB directory entry names are stored UTF-16LE, so that is the encoding actually searched
#: for — looking for the ASCII form finds nothing in a real ``.msg``, which is the sort of
#: mistake that turns "we support MSG" into "we reject every MSG".
_MSG_MARKERS = tuple(
    marker.encode("utf-16-le")
    for marker in ("__substg1.0_", "__properties_version1.0", "__recip_version1.0")
)


# ---------------------------------------------------------------------------
# Text sniffing
# ---------------------------------------------------------------------------
_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

_HTML_HINTS = re.compile(
    r"<!doctype\s+html|<html[\s>]|<head[\s>]|<body[\s>]|<meta[\s/>]|<div[\s>]|<table[\s>]",
    re.IGNORECASE,
)
#: RFC 5322 header line: a field name, a colon, and no space before the colon.
_HEADER_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9\-_]{0,60}:[ \t]")
#: Headers that, taken together with a run of header lines, mean "this is a message".
_EMAIL_HEADERS = frozenset(
    {
        "from",
        "to",
        "cc",
        "bcc",
        "subject",
        "date",
        "received",
        "message-id",
        "mime-version",
        "return-path",
        "delivered-to",
        "content-type",
        "reply-to",
        "sender",
    }
)

#: Extensions that may refine a *text* sniff. Never used to pick a binary parser.
_TEXT_HINTS: dict[str, MediaType] = {
    ".txt": MediaType.txt,
    ".text": MediaType.txt,
    ".log": MediaType.txt,
    ".md": MediaType.txt,
    ".csv": MediaType.csv,
    ".tsv": MediaType.csv,
    ".htm": MediaType.html,
    ".html": MediaType.html,
    ".xhtml": MediaType.html,
    ".eml": MediaType.eml,
    ".mbox": MediaType.eml,
}


#: Control characters that legitimately occur in text. Everything else in C0/C1 does not.
_ALLOWED_CONTROLS = frozenset("\t\n\r\f\v")
#: Fraction of control characters above which a decoded stream is judged to be binary.
_CONTROL_FRACTION = 0.02


def _is_texty(text: str) -> bool:
    """Whether a successfully decoded string is plausibly a text document."""
    sample = text[:8192]
    if not sample:
        return True
    if "\x00" in sample:
        return False
    controls = sum(
        1
        for char in sample
        if (char < " " or "\x7f" <= char <= "\x9f") and char not in _ALLOWED_CONTROLS
    )
    return controls / len(sample) <= _CONTROL_FRACTION


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode ``data`` as text, or raise.

    Tries a BOM, then UTF-8, then UTF-16 without a BOM (Windows tools emit it), then
    CP1252 as the permissive last resort. A stream with embedded NULs after all that is
    binary, not text, and is refused rather than mangled into mojibake that would then be
    classified.

    Returns:
        ``(text, encoding)``.

    Raises:
        UnsupportedFormat: The bytes are not decodable as text.
    """
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            try:
                return data.decode(encoding), encoding
            except UnicodeDecodeError as exc:
                raise UnsupportedFormat(
                    f"file carries a {encoding} BOM but does not decode as {encoding}"
                ) from exc

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    # Decoding as UTF-8 is necessary but nowhere near sufficient: every byte under 0x80 is
    # valid UTF-8, so an ELF binary, a font file or a random byte stream all "decode" — into
    # control characters. A stream that is mostly control codes is not text, and letting it
    # through would put binary noise in front of the classifier.
    if text and _is_texty(text):
        return text, "utf-8"

    # BOM-less UTF-16 shows up as every other byte being NUL for ASCII-heavy content, which
    # is also exactly what the check above rejects — so it is tried next, not first.
    sample = data[:4096]
    if sample and sample.count(0) > len(sample) // 3:
        for encoding in ("utf-16-le", "utf-16-be"):
            try:
                candidate = data.decode(encoding)
            except UnicodeDecodeError:
                continue
            if _is_texty(candidate):
                return candidate, encoding
        raise UnsupportedFormat("binary content: not decodable as text")

    if text:
        raise UnsupportedFormat(
            "binary content: decodes as UTF-8 but is mostly control characters, so it is "
            "not a text document in any format this service parses"
        )
    if b"\x00" in sample:
        raise UnsupportedFormat("binary content: embedded NUL bytes, not text")

    if not data:
        return "", "utf-8"
    fallback = data.decode("cp1252", "replace")
    if not _is_texty(fallback):
        raise UnsupportedFormat("binary content: not a text document in any parsed format")
    return fallback, "cp1252"


def _looks_like_email(text: str) -> bool:
    """True when ``text`` opens with an RFC 5322 header block.

    Deliberately strict: a run of header-shaped lines *and* at least two recognised header
    names *and* a blank line separating headers from a body. "Subject: hi" in a text file is
    not an email, and treating it as one would drop the rest of the document into a MIME
    walk that finds nothing.
    """
    lines = text.split("\n", 200)[:200]
    seen: set[str] = set()
    header_lines = 0
    for raw in lines:
        line = raw.rstrip("\r")
        if not line.strip():
            break                              # end of the header block
        if line[:1] in (" ", "\t"):
            continue                           # folded continuation
        if not _HEADER_LINE.match(line):
            return False
        header_lines += 1
        seen.add(line.split(":", 1)[0].strip().lower())
    return header_lines >= 2 and len(seen & _EMAIL_HEADERS) >= 2


def _looks_like_csv(text: str) -> bool:
    """True when the first lines agree on a delimiter and a field count of at least two.

    Conservative on purpose: misreading prose as CSV builds a table of nonsense, and the
    cost of the miss in the other direction is only that a CSV is read as lines of text.
    """
    lines = [line for line in text.splitlines()[:20] if line.strip()]
    if len(lines) < 2:
        return False
    for delimiter in (",", ";", "\t", "|"):
        counts = [line.count(delimiter) for line in lines]
        if counts[0] >= 1 and len(set(counts)) == 1:
            return True
    return False


def _text_media_type(text: str, hint: MediaType | None) -> tuple[MediaType, str]:
    """Pick a plain-text subtype from content, consulting ``hint`` only for ties."""
    head = text[:4096].lstrip()
    if head.startswith("<") and _HTML_HINTS.search(text[:8192]):
        return MediaType.html, "text-sniff"
    if _looks_like_email(text):
        return MediaType.eml, "text-sniff"
    if _looks_like_csv(text):
        return MediaType.csv, "text-sniff"
    if hint is not None and hint in _TEXT_HINTS.values():
        return hint, "text-sniff+hint"
    return MediaType.txt, "text-sniff"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _hint_for(filename: str | None) -> MediaType | None:
    if not filename:
        return None
    lowered = filename.lower()
    for suffix, media_type in _TEXT_HINTS.items():
        if lowered.endswith(suffix):
            return media_type
    return None


def _archive_media_type(
    data: bytes, limits: IngestLimits, deadline: Deadline
) -> tuple[MediaType, str]:
    """Which OOXML/ODF type a ZIP is, from the parts it contains."""
    with open_archive(data, limits, deadline) as archive:
        for marker, media_type in _ARCHIVE_MARKERS:
            if archive.has(marker):
                return media_type, marker
        if archive.has("mimetype"):
            mimetype = archive.read("mimetype", max_bytes=256).strip()
            if mimetype in _ODF_TEXT_MIMETYPES:
                return MediaType.odt, mimetype.decode("ascii", "replace")
        names = archive.names()
    raise UnsupportedFormat(
        "ZIP archive with no recognised office part "
        f"(first members: {', '.join(names[:5]) or 'none'}). A bare archive is not a "
        "document: unpack it and submit the documents inside it individually, so each one "
        "is classified on its own evidence."
    )


def _cfb_media_type(data: bytes) -> tuple[MediaType, str]:
    """CFB/OLE2 containers: Outlook ``.msg`` is supported, legacy Office is not."""
    window = data[:1024 * 1024]
    if any(marker in window for marker in _MSG_MARKERS):
        return MediaType.msg, "__substg1.0_ property streams"
    raise UnsupportedFormat(
        "OLE2 compound file that is not an Outlook .msg — legacy .doc/.xls/.ppt are not "
        "supported; re-save as .docx/.xlsx/.pptx or export to PDF"
    )


def detect(
    data: bytes,
    *,
    filename: str | None = None,
    limits: IngestLimits | None = None,
    deadline: Deadline | None = None,
) -> Detection:
    """Identify ``data``. See the module docstring for the rules this obeys.

    Args:
        data: The whole upload.
        filename: A *hint only*, consulted for plain-text subtypes and nothing else.
        limits: Caps, used when a container has to be opened to identify it.
        deadline: Wall-clock budget for the same.

    Returns:
        A :class:`Detection`.

    Raises:
        UnsupportedFormat: Nothing recognised, or a format we deliberately refuse.
    """
    limits = limits or IngestLimits()
    deadline = deadline or Deadline(limits.max_seconds)
    if not data:
        raise UnsupportedFormat("empty upload: zero bytes is not a document")

    head = data[:64]

    for magic, media_type in _IMAGE_MAGICS:
        if head.startswith(magic):
            return Detection(media_type, "magic", magic.hex())

    # RIFF....WEBP — the size field sits between the two markers.
    if head.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return Detection(MediaType.webp, "magic", "RIFF/WEBP")

    # ISO-BMFF: a length-prefixed 'ftyp' box whose brand says "still image".
    if data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return Detection(MediaType.heic, "magic", data[8:12].decode("ascii", "replace"))

    if any(head.startswith(magic) for magic in _ZIP_MAGICS):
        media_type, detail = _archive_media_type(data, limits, deadline)
        return Detection(media_type, "container", detail)

    if head.startswith(_CFB_MAGIC):
        media_type, detail = _cfb_media_type(data)
        return Detection(media_type, "container", detail)

    if data[:_PDF_SEARCH_WINDOW].find(_PDF_MAGIC) >= 0:
        return Detection(MediaType.pdf, "magic", "%PDF-")

    if head.startswith(_RTF_MAGIC):
        return Detection(MediaType.rtf, "magic", "{\\rt")

    text, encoding = decode_text(data)
    media_type, basis = _text_media_type(text, _hint_for(filename))
    return Detection(media_type, basis, encoding)


__all__ = [
    "IMAGE_TYPES",
    "NATIVE_TEXT_TYPES",
    "Detection",
    "MediaType",
    "decode_text",
    "detect",
    "is_image",
]
