"""DOCX, XLSX, PPTX and ODT — ZIP containers of XML, read with the standard library only.

No ``python-docx``, no ``openpyxl``, no ``python-pptx``. Not out of asceticism: those
libraries exist to *write* office documents and to model them faithfully, and they pull in
a dependency tree that a service whose entire security argument is "look how little is in
this process" would then have to audit. What this package needs from a DOCX is the text and
the paragraph styles, and both are two element names away in a file the standard library can
already open.

**Namespaces are matched on local name.** ECMA-376 has revised its namespace URIs, ODF has
its own set, and a strict-conformance DOCX uses different ones again from a transitional
one. Matching ``w:p`` by comparing the full ``{uri}p`` string is how a parser silently
returns an empty document for a file that opens fine in Word.

**DTDs are refused**, and refused *structurally* — see :func:`_refuse_dtd`. ``xml.etree`` will
happily expand internal entities, which is the billion-laughs amplification: a 1 KB part
becomes gigabytes inside the C parser, below the level where any of our loops could notice.
Office parts have no legitimate reason to declare a DOCTYPE. The guard asks expat rather than
scanning bytes, because a byte scan has a window and XML prologs do not have a length.
"""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET
from xml.parsers import expat

from dce.ingest.builder import LayoutBuilder
from dce.ingest.errors import MalformedDocument
from dce.ingest.limits import Deadline, IngestLimits
from dce.ingest.zipsafe import SafeArchive
from dce.models import Zone

_DIGITS = re.compile(r"(\d+)")
_COLUMN_REF = re.compile(r"^([A-Z]+)")


def _local(tag: str) -> str:
    """Local name of an ElementTree tag, with any ``{namespace}`` prefix removed."""
    return tag.rsplit("}", 1)[-1]


def _attr(node: ET.Element, name: str) -> str:
    """Attribute by local name, whatever namespace it is in."""
    for key, value in node.attrib.items():
        if _local(key) == name:
            return value
    return ""


class _PrologDone(Exception):  # control flow, not an error condition
    """Raised to stop the prolog scan once the root element begins."""


def _refuse_dtd(data: bytes, *, part: str) -> None:
    """Refuse a DTD or entity declaration *structurally*, when expat announces one.

    The obvious guard — scan the first N bytes for ``<!DOCTYPE`` — is what this replaces,
    because it is bypassable by construction: XML permits unbounded comments and processing
    instructions in the prolog, so padding with a comment longer than the window moves the
    declaration past it. Measured against the 4096-byte form: a 5 KB comment carried a
    billion-laughs payload through to expat, which expanded it, and a 6.5 MB upload reached
    1.86 GB of resident memory against a 2 GB container limit. The amplification happens
    inside the C parser, below the level at which any loop of ours could observe it, so the
    page and byte caps in :mod:`dce.ingest.limits` cannot catch it either.

    Widening the window would not fix this; nothing bounds prolog length. Asking expat instead
    removes the class — the handler fires when the declaration is *parsed*, wherever it sits,
    and raising there stops the parse before one entity is expanded.

    External entity references are refused too. That is the XXE half of the same hole, and it
    matters here more than in most services: a part that fetched a URL while we were still
    deciding what the document is would breach the egress invariant from inside a parser.

    Office parts have no legitimate reason to declare a DTD — every real DOCX, XLSX, PPTX and
    ODT part parses without one.

    A DOCTYPE is only legal in the prolog, so this scan stops at the first start tag and reads
    no further: it costs the prolog, not the document. Malformedness is left to
    :func:`parse_xml`, so there is exactly one place that reports it.

    Raises:
        MalformedDocument: The part declares a DTD or an entity.
    """

    def _reject(kind: str):
        def handler(*_args: object, **_kwargs: object) -> None:
            raise MalformedDocument(
                f"part {part!r} declares an XML {kind}; office parts have no legitimate "
                "reason to, and entity expansion is an unbounded-memory amplification"
            )

        return handler

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = _reject("DTD")
    parser.EntityDeclHandler = _reject("entity")
    parser.UnparsedEntityDeclHandler = _reject("unparsed entity")
    # A false return makes expat treat an external reference as an error rather than fetching
    # it. Belt and braces: the handlers above already refuse the DOCTYPE that would declare one.
    parser.ExternalEntityRefHandler = lambda *_a: False

    def _stop(*_args: object) -> None:
        raise _PrologDone

    parser.StartElementHandler = _stop
    try:
        parser.Parse(data, True)
    except _PrologDone:
        pass
    except expat.ExpatError:
        pass  # not well-formed; parse_xml reports it, in one voice


def parse_xml(data: bytes, *, part: str) -> ET.Element:
    """Parse an office XML part, refusing DTDs and entity declarations.

    Raises:
        MalformedDocument: The part declares a DTD or an entity, or is not well-formed XML.
    """
    _refuse_dtd(data, part=part)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise MalformedDocument(f"part {part!r} is not well-formed XML: {exc}") from exc


def _numeric_key(name: str) -> tuple[int, str]:
    """Sort ``slide10.xml`` after ``slide2.xml`` rather than before it."""
    match = _DIGITS.search(name.rsplit("/", 1)[-1])
    return (int(match.group(1)) if match else 0, name)


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def _docx_zone(style: str) -> Zone:
    """Map a Word paragraph style id onto a zone.

    Style *ids* are stable across localisations (a French Word still writes ``Heading1``),
    which is why this reads the id and not the display name.
    """
    key = style.replace(" ", "").replace("-", "").lower()
    if key == "title":
        return Zone.title
    if key.startswith("heading") or key in {"subtitle", "sectionheading"}:
        return Zone.heading
    if key in {"header", "footer", "pageheader", "pagefooter"}:
        return Zone.furniture
    return Zone.body


def _docx_paragraph_text(node: ET.Element) -> str:
    """Text of one ``w:p``, honouring tabs and soft breaks."""
    parts: list[str] = []
    for child in node.iter():
        tag = _local(child.tag)
        if tag == "t":
            parts.append(child.text or "")
        elif tag == "tab":
            parts.append(" ")
        elif tag in {"br", "cr"}:
            parts.append("\n")
    return "".join(parts)


def _docx_paragraph_style(node: ET.Element) -> str:
    for child in node:
        if _local(child.tag) == "pPr":
            for prop in child:
                if _local(prop.tag) == "pStyle":
                    return _attr(prop, "val")
    return ""


def _docx_walk(
    node: ET.Element,
    builder: LayoutBuilder,
    deadline: Deadline,
    *,
    zone_override: Zone | None,
    depth: int = 0,
) -> None:
    """Emit paragraphs and tables in document order."""
    if depth > 64 or builder.full:
        return
    for child in node:
        deadline.check("docx")
        tag = _local(child.tag)
        if tag == "p":
            text = _docx_paragraph_text(child)
            if text.strip():
                zone = zone_override or _docx_zone(_docx_paragraph_style(child))
                builder.block(
                    text, zone=zone, page=1, role=_docx_paragraph_style(child) or None
                )
        elif tag == "tbl":
            rows: list[list[str]] = []
            for row in child:
                if _local(row.tag) != "tr":
                    continue
                cells: list[str] = []
                for cell in row:
                    if _local(cell.tag) != "tc":
                        continue
                    cells.append(
                        " ".join(
                            _docx_paragraph_text(p)
                            for p in cell.iter()
                            if _local(p.tag) == "p"
                        )
                    )
                if cells:
                    rows.append(cells)
            if rows:
                builder.table(rows, page=1)
        elif tag in {"body", "sdt", "sdtContent", "txbxContent", "tc", "hdr", "ftr"}:
            _docx_walk(child, builder, deadline, zone_override=zone_override, depth=depth + 1)


def parse_docx(
    archive: SafeArchive, builder: LayoutBuilder, limits: IngestLimits, deadline: Deadline
) -> int:
    """Read ``word/document.xml`` plus every header and footer part.

    Headers and footers are read as :attr:`~dce.models.Zone.furniture` — the same zone
    :mod:`dce.adapters` gives Azure's ``pageHeader``/``pageFooter`` roles, and for the same
    reason: a term that appears on every page because it is in the running header is not
    evidence about what the document is.

    Returns:
        Page count. Word does not paginate in the file, so this is always 1 — pagination is
        a rendering decision made by Word at open time and is genuinely not in the bytes.
    """
    root = parse_xml(archive.read("word/document.xml"), part="word/document.xml")
    builder.page(1)
    _docx_walk(root, builder, deadline, zone_override=None)

    for name in archive.match("word/", ".xml"):
        base = name.rsplit("/", 1)[-1]
        if not (base.startswith("header") or base.startswith("footer")):
            continue
        deadline.check(f"docx.{base}")
        part = parse_xml(archive.read(name), part=name)
        _docx_walk(part, builder, deadline, zone_override=Zone.furniture)
    return 1


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------
def _shared_strings(archive: SafeArchive, deadline: Deadline) -> list[str]:
    if not archive.has("xl/sharedStrings.xml"):
        return []
    deadline.check("xlsx.sharedStrings")
    root = parse_xml(archive.read("xl/sharedStrings.xml"), part="xl/sharedStrings.xml")
    strings: list[str] = []
    for item in root:
        if _local(item.tag) != "si":
            continue
        strings.append(
            "".join(t.text or "" for t in item.iter() if _local(t.tag) == "t")
        )
    return strings


def _sheet_names(archive: SafeArchive) -> dict[str, str]:
    """``{relationship id: sheet name}`` from ``xl/workbook.xml``."""
    if not archive.has("xl/workbook.xml"):
        return {}
    root = parse_xml(archive.read("xl/workbook.xml"), part="xl/workbook.xml")
    names: dict[str, str] = {}
    for node in root.iter():
        if _local(node.tag) == "sheet":
            names[_attr(node, "id")] = _attr(node, "name")
    return names


def _sheet_targets(archive: SafeArchive) -> dict[str, str]:
    """``{relationship id: part name}`` from the workbook's rels part."""
    rels = "xl/_rels/workbook.xml.rels"
    if not archive.has(rels):
        return {}
    root = parse_xml(archive.read(rels), part=rels)
    targets: dict[str, str] = {}
    for node in root.iter():
        if _local(node.tag) != "Relationship":
            continue
        target = _attr(node, "Target")
        if not target:
            continue
        target = target.lstrip("/")
        targets[_attr(node, "Id")] = target if target.startswith("xl/") else f"xl/{target}"
    return targets


def _column_index(ref: str, fallback: int) -> int:
    """``"BC7"`` -> 54. Falls back to positional order when the cell has no reference."""
    match = _COLUMN_REF.match(ref.upper())
    if not match:
        return fallback
    index = 0
    for char in match.group(1):
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = _attr(cell, "t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter() if _local(t.tag) == "t")
    value = ""
    for child in cell:
        if _local(child.tag) == "v":
            value = child.text or ""
            break
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    return value


def parse_xlsx(
    archive: SafeArchive, builder: LayoutBuilder, limits: IngestLimits, deadline: Deadline
) -> int:
    """Read every worksheet as a table, with the sheet name as a heading.

    A workbook's sheet names are real signal — "Balance Sheet", "Beneficial Owners",
    "Schedule 3" — and they are the closest thing a spreadsheet has to a section heading, so
    they are emitted as :attr:`~dce.models.Zone.heading` and the cells as
    :attr:`~dce.models.Zone.table`.

    **Known limitation, stated rather than discovered later: number formats are not applied.**
    A cell displaying ``31/03/2026`` is stored as ``46112`` with a date format on the style,
    and this reader emits ``46112``. Resolving it means reading ``xl/styles.xml``, mapping
    ``numFmtId`` through the built-in format table, and reimplementing Excel's epoch
    (including the 1900 leap-year bug) — a real chunk of spreadsheet semantics for a gain
    that lands on *extraction*, not classification: no doctype is recognised by a date. A
    date field extracted from an ingested XLSX will therefore fail its validator and route to
    a human, which is the safe direction. Fix it before relying on XLSX field extraction.

    Returns:
        The number of sheets read; each is a page.
    """
    shared = _shared_strings(archive, deadline)
    names = _sheet_names(archive)
    targets = _sheet_targets(archive)

    ordered: list[tuple[str, str]] = []
    for rel_id, sheet_name in names.items():
        target = targets.get(rel_id)
        if target and archive.has(target):
            ordered.append((sheet_name, target))
    if not ordered:
        ordered = [
            (name.rsplit("/", 1)[-1].removesuffix(".xml"), name)
            for name in sorted(archive.match("xl/worksheets/", ".xml"), key=_numeric_key)
        ]
    if not ordered:
        raise MalformedDocument("XLSX contains no worksheets")

    pages = 0
    for index, (sheet_name, part) in enumerate(ordered, start=1):
        if index > limits.max_pages:
            builder.limits_hit.append("max_pages")
            builder.truncated = True
            break
        deadline.check(f"xlsx.{part}")
        pages = index
        builder.page(index)
        if sheet_name:
            builder.block(sheet_name, zone=Zone.heading, page=index, role="sheetName")

        root = parse_xml(archive.read(part), part=part)
        rows: list[list[str]] = []
        for node in root.iter():
            if _local(node.tag) != "row":
                continue
            if len(rows) >= limits.max_table_rows:
                builder.limits_hit.append("max_table_rows")
                builder.truncated = True
                break
            if len(rows) % 256 == 0:
                deadline.check(f"xlsx.{part}.row{len(rows)}")
            cells: list[str] = []
            for position, cell in enumerate(node):
                if _local(cell.tag) != "c":
                    continue
                column = _column_index(_attr(cell, "r"), position)
                if column >= len(cells):
                    cells.extend([""] * (column - len(cells) + 1))
                cells[column] = _cell_text(cell, shared)
            if any(c.strip() for c in cells):
                rows.append(cells)
        if rows:
            builder.table(rows, page=index, table_id=f"sheet{index}", header_rows=0)
    return max(1, pages)


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------
_TITLE_PLACEHOLDERS = frozenset({"title", "ctrTitle"})


def _pptx_shape_zone(shape: ET.Element) -> Zone:
    """A shape sitting in a title placeholder is the slide's title; everything else is body."""
    for node in shape.iter():
        if _local(node.tag) == "ph" and _attr(node, "type") in _TITLE_PLACEHOLDERS:
            return Zone.title
    return Zone.body


def _pptx_paragraphs(node: ET.Element) -> list[str]:
    """One string per ``a:p`` under ``node``."""
    out: list[str] = []
    for para in node.iter():
        if _local(para.tag) != "p":
            continue
        text = "".join(t.text or "" for t in para.iter() if _local(t.tag) == "t")
        if text.strip():
            out.append(text)
    return out


def parse_pptx(
    archive: SafeArchive, builder: LayoutBuilder, limits: IngestLimits, deadline: Deadline
) -> int:
    """Read each slide as a page: title placeholder to title, other shapes to body.

    Speaker notes are **not** read. A notes page is the presenter's script, not the document,
    and on a KYC deck it is where "draft, do not send" lives — including it would put text
    into the classifier that is not on the document at all.

    Returns:
        The number of slides read.
    """
    slides = sorted(archive.match("ppt/slides/", ".xml"), key=_numeric_key)
    slides = [name for name in slides if "/_rels/" not in name]
    if not slides:
        raise MalformedDocument("PPTX contains no slides")

    pages = 0
    for index, part in enumerate(slides, start=1):
        if index > limits.max_pages:
            builder.limits_hit.append("max_pages")
            builder.truncated = True
            break
        deadline.check(f"pptx.{part}")
        pages = index
        builder.page(index)
        root = parse_xml(archive.read(part), part=part)
        for node in root.iter():
            tag = _local(node.tag)
            if tag == "sp":
                zone = _pptx_shape_zone(node)
                for text in _pptx_paragraphs(node):
                    builder.block(text, zone=zone, page=index)
            elif tag == "tbl":
                rows: list[list[str]] = []
                for row in node:
                    if _local(row.tag) != "tr":
                        continue
                    cells = [
                        " ".join(_pptx_paragraphs(cell))
                        for cell in row
                        if _local(cell.tag) == "tc"
                    ]
                    if cells:
                        rows.append(cells)
                if rows:
                    builder.table(rows, page=index)
    return max(1, pages)


# ---------------------------------------------------------------------------
# ODT
# ---------------------------------------------------------------------------
def _odt_text(node: ET.Element) -> str:
    parts: list[str] = []
    for child in node.iter():
        tag = _local(child.tag)
        if tag in {"tab", "line-break"}:
            parts.append(" ")
        if child.text:
            parts.append(child.text)
        if child is not node and child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _odt_walk(
    node: ET.Element, builder: LayoutBuilder, deadline: Deadline, depth: int = 0
) -> None:
    if depth > 64 or builder.full:
        return
    for child in node:
        deadline.check("odt")
        tag = _local(child.tag)
        if tag == "h":
            level = _attr(child, "outline-level") or "1"
            zone = Zone.title if level == "1" else Zone.heading
            builder.block(_odt_text(child), zone=zone, page=1, role=f"h{level}")
        elif tag == "p":
            style = _attr(child, "style-name").lower()
            zone = Zone.title if style.startswith("title") else Zone.body
            builder.block(_odt_text(child), zone=zone, page=1)
        elif tag == "table":
            rows: list[list[str]] = []
            for row in child.iter():
                if _local(row.tag) != "table-row":
                    continue
                cells = [
                    _odt_text(cell) for cell in row if _local(cell.tag) == "table-cell"
                ]
                if cells:
                    rows.append(cells)
            if rows:
                builder.table(rows, page=1)
        elif tag in {"body", "text", "section", "list", "list-item", "frame", "text-box"}:
            _odt_walk(child, builder, deadline, depth + 1)


def parse_odt(
    archive: SafeArchive, builder: LayoutBuilder, limits: IngestLimits, deadline: Deadline
) -> int:
    """Read ``content.xml``: ``text:h`` becomes a heading, ``text:p`` a body block.

    An outline level of 1 is read as the document title, on the same reasoning as HTML's
    ``<h1>``: it is a structural statement the author made, not an inference we invented.
    """
    if not archive.has("content.xml"):
        raise MalformedDocument("ODT has no content.xml")
    root = parse_xml(archive.read("content.xml"), part="content.xml")
    builder.page(1)
    _odt_walk(root, builder, deadline)
    return 1


__all__ = ["parse_docx", "parse_odt", "parse_pptx", "parse_xlsx", "parse_xml"]
