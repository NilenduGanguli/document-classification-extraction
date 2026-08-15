"""Synthetic documents for the ingestion tests, built byte by byte.

No binary blobs are committed. Every fixture here is constructed from the format
specification in code, which has three advantages over checking in sample files: a reviewer
can see exactly what is being parsed, the fixtures cannot rot into "whatever this file
happened to contain", and a specimen document is never confused for a training example (see
the standing NO OVERFITTING constraint).

The CFB writer is the interesting one: ``.msg`` is a compound file, so proving
:mod:`dce.ingest.cfb` reads one means writing one, including the mini-FAT path that small
property streams take.
"""
from __future__ import annotations

import io
import struct
import zipfile
import zlib

# ---------------------------------------------------------------------------
# ZIP-based office formats
# ---------------------------------------------------------------------------
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def zip_bytes(members: dict[str, bytes | str], *, compress: bool = True) -> bytes:
    """Build a ZIP in memory from ``{name: content}``."""
    buffer = io.BytesIO()
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", mode) as zf:
        for name, content in members.items():
            zf.writestr(name, content if isinstance(content, bytes) else content.encode("utf-8"))
    return buffer.getvalue()


def docx(
    paragraphs: list[tuple[str, str]],
    *,
    tables: list[list[list[str]]] | None = None,
    header: str = "",
    footer: str = "",
) -> bytes:
    """A DOCX. ``paragraphs`` is ``[(style_id, text), …]``; an empty style means body."""
    body: list[str] = []
    for style, text in paragraphs:
        style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body.append(f"<w:p>{style_xml}<w:r><w:t>{text}</w:t></w:r></w:p>")
    for table in tables or []:
        rows = "".join(
            "<w:tr>"
            + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row)
            + "</w:tr>"
            for row in table
        )
        body.append(f"<w:tbl>{rows}</w:tbl>")

    members: dict[str, bytes | str] = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "word/document.xml": (
            f'<?xml version="1.0"?><w:document xmlns:w="{W_NS}"><w:body>'
            + "".join(body)
            + "</w:body></w:document>"
        ),
    }
    if header:
        members["word/header1.xml"] = (
            f'<?xml version="1.0"?><w:hdr xmlns:w="{W_NS}">'
            f"<w:p><w:r><w:t>{header}</w:t></w:r></w:p></w:hdr>"
        )
    if footer:
        members["word/footer1.xml"] = (
            f'<?xml version="1.0"?><w:ftr xmlns:w="{W_NS}">'
            f"<w:p><w:r><w:t>{footer}</w:t></w:r></w:p></w:ftr>"
        )
    return zip_bytes(members)


def docx_of_one_scan(*, images: int = 1) -> bytes:
    """A DOCX whose whole content is a pasted picture: no text, ``word/media/`` populated.

    An ordinary way to receive a KYC document — somebody photographs an ID, pastes it into
    Word and sends that. The file parses perfectly and yields not one character.
    """
    members: dict[str, bytes | str] = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "word/document.xml": (
            f'<?xml version="1.0"?><w:document xmlns:w="{W_NS}"><w:body>'
            "<w:p><w:r><w:drawing/></w:r></w:p></w:body></w:document>"
        ),
    }
    for index in range(1, images + 1):
        members[f"word/media/image{index}.png"] = png(200, 260)
    return zip_bytes(members)


S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def xlsx(sheets: dict[str, list[list[str]]]) -> bytes:
    """An XLSX with inline strings, one worksheet part per sheet name."""
    members: dict[str, bytes | str] = {"[Content_Types].xml": _CONTENT_TYPES}
    sheet_nodes: list[str] = []
    rel_nodes: list[str] = []
    for index, (name, rows) in enumerate(sheets.items(), start=1):
        rid = f"rId{index}"
        sheet_nodes.append(f'<sheet name="{name}" sheetId="{index}" r:id="{rid}"/>')
        rel_nodes.append(
            f'<Relationship Id="{rid}" Target="worksheets/sheet{index}.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/worksheet"/>'
        )
        row_xml: list[str] = []
        for row_index, row in enumerate(rows, start=1):
            cells = "".join(
                f'<c r="{chr(65 + col)}{row_index}" t="inlineStr"><is><t>{value}</t></is></c>'
                for col, value in enumerate(row)
            )
            row_xml.append(f'<row r="{row_index}">{cells}</row>')
        members[f"xl/worksheets/sheet{index}.xml"] = (
            f'<?xml version="1.0"?><worksheet xmlns="{S_NS}"><sheetData>'
            + "".join(row_xml)
            + "</sheetData></worksheet>"
        )
    members["xl/workbook.xml"] = (
        f'<?xml version="1.0"?><workbook xmlns="{S_NS}" xmlns:r="{R_NS}"><sheets>'
        + "".join(sheet_nodes)
        + "</sheets></workbook>"
    )
    members["xl/_rels/workbook.xml.rels"] = (
        f'<?xml version="1.0"?><Relationships xmlns="{PKG_REL_NS}">'
        + "".join(rel_nodes)
        + "</Relationships>"
    )
    return zip_bytes(members)


P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def pptx(slides: list[tuple[str, list[str]]]) -> bytes:
    """A PPTX. Each slide is ``(title, [body lines])``."""
    members: dict[str, bytes | str] = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "ppt/presentation.xml": f'<?xml version="1.0"?><p:presentation xmlns:p="{P_NS}"/>',
    }
    for index, (title, lines) in enumerate(slides, start=1):
        shapes = [
            f'<p:sp><p:nvSpPr><p:nvPr><p:ph type="ctrTitle"/></p:nvPr></p:nvSpPr>'
            f"<p:txBody><a:p><a:r><a:t>{title}</a:t></a:r></a:p></p:txBody></p:sp>"
        ]
        body = "".join(f"<a:p><a:r><a:t>{line}</a:t></a:r></a:p>" for line in lines)
        shapes.append(
            f"<p:sp><p:nvSpPr><p:nvPr/></p:nvSpPr><p:txBody>{body}</p:txBody></p:sp>"
        )
        members[f"ppt/slides/slide{index}.xml"] = (
            f'<?xml version="1.0"?><p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}">'
            f"<p:cSld><p:spTree>{''.join(shapes)}</p:spTree></p:cSld></p:sld>"
        )
    return zip_bytes(members)


ODF_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
ODF_OFFICE_NS = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"


def odt(items: list[tuple[str, str]]) -> bytes:
    """An ODT. ``items`` is ``[("h1"|"h2"|"p", text), …]``."""
    nodes: list[str] = []
    for kind, text in items:
        if kind.startswith("h"):
            nodes.append(f'<text:h text:outline-level="{kind[1:]}">{text}</text:h>')
        else:
            nodes.append(f"<text:p>{text}</text:p>")
    content = (
        f'<?xml version="1.0"?><office:document-content xmlns:office="{ODF_OFFICE_NS}" '
        f'xmlns:text="{ODF_TEXT_NS}"><office:body><office:text>'
        + "".join(nodes)
        + "</office:text></office:body></office:document-content>"
    )
    return zip_bytes(
        {
            "mimetype": "application/vnd.oasis.opendocument.text",
            "content.xml": content,
        }
    )


def zip_bomb(*, entries: int = 4, uncompressed: int = 64 * 1024 * 1024) -> bytes:
    """A DOCX-shaped archive whose *unread* members expand enormously.

    The interesting property is that this one is harmless: nothing inflates a member the
    parser never asks for. Use :func:`oversized_docx` for the case where the part we do read
    is the bomb.
    """
    members: dict[str, bytes | str] = {
        "word/document.xml": (
            f'<?xml version="1.0"?><w:document xmlns:w="{W_NS}"><w:body>'
            "<w:p><w:r><w:t>a short but genuine document body</w:t></w:r></w:p>"
            "</w:body></w:document>"
        )
    }
    payload = b"\x00" * uncompressed
    for index in range(entries):
        members[f"word/bomb{index}.bin"] = payload
    return zip_bytes(members)


def oversized_docx(*, paragraphs: int = 20_000, chars: int = 900) -> bytes:
    """A DOCX whose ``word/document.xml`` itself expands past the per-member cap."""
    filler = "A" * chars
    body = f"<w:p><w:r><w:t>{filler}</w:t></w:r></w:p>" * paragraphs
    return zip_bytes(
        {
            "word/document.xml": (
                f'<?xml version="1.0"?><w:document xmlns:w="{W_NS}"><w:body>'
                f"{body}</w:body></w:document>"
            )
        }
    )


# ---------------------------------------------------------------------------
# Compound File Binary (.msg)
# ---------------------------------------------------------------------------
_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_FATSECT = 0xFFFFFFFD
_SECTOR = 512
_MINI_SECTOR = 64
_MINI_CUTOFF = 4096


def _dir_entry(name: str, obj_type: int, start: int, size: int) -> bytes:
    raw = bytearray(128)
    encoded = name.encode("utf-16-le")[:62]
    raw[: len(encoded)] = encoded
    struct.pack_into("<H", raw, 64, len(encoded) + 2)
    raw[66] = obj_type
    raw[67] = 1                                   # colour: black
    struct.pack_into("<III", raw, 0x44, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
    struct.pack_into("<I", raw, 0x74, start)
    struct.pack_into("<II", raw, 0x78, size & 0xFFFFFFFF, size >> 32)
    return bytes(raw)


def compound_file(streams: dict[str, bytes]) -> bytes:
    """Write a valid CFB v3 container holding ``streams``.

    Small streams (< 4096 bytes) go through the mini-FAT, exactly as Outlook writes them, so
    the reader's mini-stream path is exercised rather than assumed.
    """
    big = {n: d for n, d in streams.items() if len(d) >= _MINI_CUTOFF}
    small = {n: d for n, d in streams.items() if len(d) < _MINI_CUTOFF}

    sectors: list[bytes] = []
    fat: list[int] = []

    def allocate(payload: bytes, sector_size: int, store: list[bytes]) -> int:
        """Append ``payload`` as a chain of sectors; return the first sector index."""
        count = max(1, -(-len(payload) // sector_size))
        first = len(store)
        for index in range(count):
            store.append(payload[index * sector_size : (index + 1) * sector_size].ljust(
                sector_size, b"\x00"
            ))
        return first

    def chain(first: int, count: int, table: list[int]) -> None:
        while len(table) < first + count:
            table.append(_FREESECT)
        for index in range(count):
            table[first + index] = (
                first + index + 1 if index < count - 1 else _ENDOFCHAIN
            )

    # 1. Big streams straight into sectors.
    big_starts: dict[str, int] = {}
    for name, payload in big.items():
        count = max(1, -(-len(payload) // _SECTOR))
        start = allocate(payload, _SECTOR, sectors)
        chain(start, count, fat)
        big_starts[name] = start

    # 2. Small streams into the mini stream, tracked by the mini-FAT.
    mini_stream = bytearray()
    minifat: list[int] = []
    small_starts: dict[str, int] = {}
    for name, payload in small.items():
        count = max(1, -(-len(payload) // _MINI_SECTOR))
        start = len(mini_stream) // _MINI_SECTOR
        mini_stream.extend(payload.ljust(count * _MINI_SECTOR, b"\x00"))
        for index in range(count):
            minifat.append(start + index + 1 if index < count - 1 else _ENDOFCHAIN)
        small_starts[name] = start

    mini_start = _ENDOFCHAIN
    if mini_stream:
        count = max(1, -(-len(mini_stream) // _SECTOR))
        mini_start = allocate(bytes(mini_stream), _SECTOR, sectors)
        chain(mini_start, count, fat)

    minifat_start = _ENDOFCHAIN
    minifat_sectors = 0
    if minifat:
        packed = b"".join(struct.pack("<I", value) for value in minifat)
        per_sector = _SECTOR // 4
        packed = packed.ljust(-(-len(packed) // _SECTOR) * _SECTOR, b"\xff")
        minifat_sectors = len(packed) // _SECTOR
        minifat_start = allocate(packed, _SECTOR, sectors)
        chain(minifat_start, minifat_sectors, fat)
        assert per_sector > 0

    # 3. Directory: root first, then one entry per stream.
    entries = [_dir_entry("Root Entry", 5, mini_start, len(mini_stream))]
    for name, payload in streams.items():
        start = big_starts.get(name, small_starts.get(name, _ENDOFCHAIN))
        entries.append(_dir_entry(name, 2, start, len(payload)))
    directory = b"".join(entries)
    directory = directory.ljust(-(-len(directory) // _SECTOR) * _SECTOR, b"\x00")
    dir_sectors = len(directory) // _SECTOR
    dir_start = allocate(directory, _SECTOR, sectors)
    chain(dir_start, dir_sectors, fat)

    # 4. The FAT itself — solved iteratively, since FAT sectors are also allocated sectors.
    per_sector = _SECTOR // 4
    fat_sector_count = 1
    while True:
        total = len(sectors) + fat_sector_count
        needed = max(1, -(-total // per_sector))
        if needed == fat_sector_count:
            break
        fat_sector_count = needed

    fat_first = len(sectors)
    while len(fat) < fat_first + fat_sector_count:
        fat.append(_FREESECT)
    for index in range(fat_sector_count):
        fat[fat_first + index] = _FATSECT
    while len(fat) < fat_sector_count * per_sector:
        fat.append(_FREESECT)
    fat_bytes = b"".join(struct.pack("<I", value) for value in fat)
    for index in range(fat_sector_count):
        sectors.append(fat_bytes[index * _SECTOR : (index + 1) * _SECTOR].ljust(_SECTOR, b"\xff"))

    # 5. Header.
    header = bytearray(_SECTOR)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HH", header, 0x18, 0x003E, 0x0003)   # minor / major version
    struct.pack_into("<H", header, 0x1C, 0xFFFE)            # little-endian marker
    struct.pack_into("<HH", header, 0x1E, 9, 6)             # sector / mini sector shift
    struct.pack_into(
        "<IIIIIIII",
        header,
        0x2C,
        fat_sector_count,
        dir_start,
        0,
        _MINI_CUTOFF,
        minifat_start,
        minifat_sectors,
        _ENDOFCHAIN,
        0,
    )
    difat = [fat_first + index for index in range(fat_sector_count)]
    difat += [_FREESECT] * (109 - len(difat))
    struct.pack_into("<109I", header, 0x4C, *difat[:109])

    return bytes(header) + b"".join(sectors)


def msg(
    subject: str,
    body: str,
    *,
    sender: str = "compliance@example.test",
    to: str = "kyc@example.test",
    attachment: str = "",
) -> bytes:
    """An Outlook ``.msg`` carrying the properties :func:`dce.ingest.cfb.parse_msg` reads."""
    streams: dict[str, bytes] = {
        "__substg1.0_0037001F": subject.encode("utf-16-le"),
        "__substg1.0_1000001F": body.encode("utf-16-le"),
        "__substg1.0_0C1F001F": sender.encode("utf-16-le"),
        "__substg1.0_0E04001F": to.encode("utf-16-le"),
    }
    if attachment:
        streams["__substg1.0_3704001F"] = attachment.encode("utf-16-le")
    return compound_file(streams)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def png(width: int = 64, height: int = 48) -> bytes:
    """A valid single-colour PNG."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\xff" * (width * 3) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def jpeg(width: int = 100, height: int = 200) -> bytes:
    """Enough of a JPEG for detection and the header probe: SOI, SOF0, EOI."""
    sof = struct.pack(">BBHBHHB", 0xFF, 0xC0, 11, 8, height, width, 1) + b"\x01\x11\x00"
    jfif = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    return jfif + sof + b"\xff\xd9"


def multipage_tiff(pages: int = 3, width: int = 80, height: int = 60) -> bytes:
    """A little-endian TIFF with ``pages`` IFDs chained, so frame counting is exercised."""
    entries_per_ifd = 2
    ifd_size = 2 + entries_per_ifd * 12 + 4
    out = bytearray(b"II\x2a\x00")
    out.extend(struct.pack("<I", 8))
    offsets = [8 + index * ifd_size for index in range(pages)]
    for index, offset in enumerate(offsets):
        assert len(out) == offset
        out.extend(struct.pack("<H", entries_per_ifd))
        out.extend(struct.pack("<HHII", 256, 3, 1, width))       # ImageWidth
        out.extend(struct.pack("<HHII", 257, 3, 1, height))      # ImageLength
        nxt = offsets[index + 1] if index + 1 < pages else 0
        out.extend(struct.pack("<I", nxt))
    return bytes(out)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def text_pdf(lines: list[str], *, pages: int = 1) -> bytes:
    """A PDF with a real text layer, built with PyMuPDF (skipped when absent)."""
    import fitz

    document = fitz.open()
    for _ in range(pages):
        page = document.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=12)
            y += 18
    data = document.tobytes()
    document.close()
    return data


def scanned_pdf(*, pages: int = 1) -> bytes:
    """A PDF whose pages carry an image and no text — the scan case."""
    import fitz

    document = fitz.open()
    image = png(200, 260)
    for _ in range(pages):
        page = document.new_page()
        page.insert_image(fitz.Rect(50, 50, 250, 310), stream=image)
    data = document.tobytes()
    document.close()
    return data


def mixed_pdf(
    lines: list[str] | None = None,
    *,
    text_pages: int = 1,
    image_pages: int = 1,
) -> bytes:
    """A PDF that is partly a text layer and partly a scan — the shape a KYC corpus is full of.

    The first ``text_pages`` carry real characters; the remaining ``image_pages`` carry one
    full-bleed image each and no text at all. A typed cover page in front of photographed
    attachments, an e-filed wrapper around a scanned ID: the text pages are the boilerplate
    and the image pages are the payload.

    Neither existing fixture covers this shape. :func:`text_pdf` writes the same lines onto
    every page and :func:`scanned_pdf` puts an image on every page, so the branch where one
    document is both was never exercised — which is how a whole-document character floor
    survived review.
    """
    import fitz

    lines = lines or ["Invoice number 4471 issued by Acme Corporation Ltd."]
    document = fitz.open()
    for _ in range(text_pages):
        page = document.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=12)
            y += 18
    image = png(200, 260)
    for _ in range(image_pages):
        page = document.new_page()
        page.insert_image(page.rect, stream=image)
    data = document.tobytes()
    document.close()
    return data


def hidden_text_pdf(text: str = "hidden marker text here", *, pages: int = 1) -> bytes:
    """A PDF whose only text is drawn invisibly (render mode 3), over a full-page image.

    What a scan carrying a previous tool's OCR layer looks like: a picture of a document,
    plus characters nobody can see. ``get_text`` returns them like any others, so a
    character count alone cannot tell this from a genuine text layer.
    """
    import fitz

    document = fitz.open()
    image = png(200, 260)
    for _ in range(pages):
        page = document.new_page()
        page.insert_image(page.rect, stream=image)
        page.insert_text((72, 72), text, fontsize=12, render_mode=3)
    data = document.tobytes()
    document.close()
    return data


def garbage_text_pdf(*, pages: int = 1) -> bytes:
    """A PDF whose text layer is a bad prior OCR's output — long enough, and meaningless.

    Clears any character-count floor comfortably while carrying no word a classifier could
    anchor on. The point of the fixture is that length is not quality.
    """
    import fitz

    document = fitz.open()
    garbage = (
        "lllllllllllllllllllllllll",
        "llllllllllllllllllllllllll",
        "lllllllllllllllllllllllll",
    )
    for _ in range(pages):
        page = document.new_page()
        y = 72
        for line in garbage:
            page.insert_text((72, y), line, fontsize=12)
            y += 18
    data = document.tobytes()
    document.close()
    return data


__all__ = [
    "compound_file",
    "docx",
    "docx_of_one_scan",
    "garbage_text_pdf",
    "hidden_text_pdf",
    "jpeg",
    "mixed_pdf",
    "msg",
    "multipage_tiff",
    "odt",
    "oversized_docx",
    "png",
    "pptx",
    "scanned_pdf",
    "text_pdf",
    "xlsx",
    "zip_bomb",
    "zip_bytes",
]
