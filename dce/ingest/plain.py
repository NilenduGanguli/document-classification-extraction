"""TXT, CSV and RTF — the formats whose whole content is text.

TXT carries no structure, so every block is :attr:`~dce.models.Zone.body`, exactly as
:func:`dce.adapters.from_plain_text` does. CSV carries a table and nothing else. RTF carries
paragraph breaks and a great deal of markup that is not content.
"""
from __future__ import annotations

import csv
import io

from dce.ingest.builder import LayoutBuilder
from dce.ingest.errors import MalformedDocument
from dce.ingest.limits import Deadline, IngestLimits
from dce.models import Zone


# ---------------------------------------------------------------------------
# TXT
# ---------------------------------------------------------------------------
def parse_txt(text: str, builder: LayoutBuilder, deadline: Deadline) -> None:
    """One block per non-empty line, all :attr:`~dce.models.Zone.body`.

    A plain text file states no structure. Inferring a title from "the first line is short
    and in capitals" is exactly the guess :func:`dce.adapters.from_plain_text` refuses to
    make, and it would be amplified 3x by the title zone weight.
    """
    deadline.check("txt")
    builder.page(1)
    builder.lines(text, zone=Zone.body, page=1)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def _dialect(sample: str) -> type[csv.Dialect] | csv.Dialect:
    """Sniff the delimiter, falling back to a comma.

    ``csv.Sniffer`` raises on input it cannot read; a comma is the right guess when it does,
    because the alternative is refusing a file we can still read as one column.
    """
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def parse_csv(
    text: str, builder: LayoutBuilder, limits: IngestLimits, deadline: Deadline
) -> None:
    """Read the whole file as one table.

    Emits both representations — a :class:`~dce.models.Table` with per-cell text and one
    :attr:`Zone.table` block per row — because that is what
    :func:`dce.adapters.from_azure_layout` emits for a table and the two paths have to score
    a document the same way. See :mod:`dce.ingest.builder` on that convention.
    """
    deadline.check("csv")
    builder.page(1)
    sample = text[:8192]
    reader = csv.reader(io.StringIO(text), _dialect(sample))
    rows: list[list[str]] = []
    try:
        for index, row in enumerate(reader):
            if index % 256 == 0:
                deadline.check(f"csv.row{index}")
            rows.append([str(cell) for cell in row])
            if len(rows) >= limits.max_table_rows:
                # Say so. A CSV silently cut to its first N rows is a different document,
                # and "your file was shortened" must never be something a caller has to
                # infer from a row count.
                builder.truncated = True
                if "max_table_rows" not in builder.limits_hit:
                    builder.limits_hit.append("max_table_rows")
                break
    except csv.Error as exc:
        # A NUL byte or a 1 GB unterminated quoted field. Partial rows are still worth
        # classifying, so this degrades rather than raising when anything was read.
        if not rows:
            raise MalformedDocument(f"CSV is unreadable: {exc}") from exc
    if not rows:
        return
    # A header row is a row whose cells are all non-numeric — the same test a human uses.
    has_header = bool(rows[0]) and all(
        cell.strip() and not cell.strip().replace(".", "", 1).replace("-", "", 1).isdigit()
        for cell in rows[0]
    )
    builder.table(rows, page=1, table_id="csv", header_rows=1 if has_header else 0)


# ---------------------------------------------------------------------------
# RTF
# ---------------------------------------------------------------------------
#: RTF destinations whose content is markup, not document text. Everything inside one of
#: these groups is discarded — font tables, colour tables, embedded images, revision ids.
_SKIP_DESTINATIONS = frozenset(
    {
        "fonttbl", "colortbl", "stylesheet", "listtable", "listoverridetable", "rsidtbl",
        "generator", "info", "pict", "object", "objdata", "datastore", "themedata",
        "colorschememapping", "latentstyles", "filetbl", "revtbl", "xmlnstbl", "mmathPr",
        "wgrffmtfilter", "template", "nonesttables", "shppict", "bkmkstart", "bkmkend",
        "field", "fldinst", "pgdsctbl", "upr", "userprops", "svb", "panose",
    }
)

#: Control words that produce whitespace rather than being dropped.
_BREAKS = frozenset({"par", "line", "sect", "page", "column", "row", "cell", "nestcell"})
_SPACES = frozenset({"tab", "emspace", "enspace", "qmspace"})
#: Symbol control words with a literal meaning. Written as escapes rather than literals:
#: several of these are visually ambiguous with ASCII punctuation, which makes the table
#: unreviewable and trips RUF001 — the same reasoning as ``_MARKS`` in :mod:`dce.models`.
_SYMBOLS = {
    "ldblquote": "\u201c",
    "rdblquote": "\u201d",
    "lquote": "\u2018",
    "rquote": "\u2019",
    "emdash": "\u2014",
    "endash": "\u2013",
    "bullet": "\u2022",
    "~": "\u00a0",
    "_": "\u2011",
}


def rtf_to_text(data: bytes, deadline: Deadline, *, max_chars: int) -> str:
    """De-tokenise RTF into plain text.

    A deliberately small reader: RTF is a huge specification and almost none of it is
    document content. Groups are tracked so that a skipped destination takes its whole
    subtree with it, ``\\'hh`` is decoded through CP1252 (RTF's default ANSI codepage) and
    ``\\uN`` through the Unicode escape with its ``\\uc`` skip count honoured.

    Args:
        data: The RTF bytes. RTF is 7-bit ASCII by specification.
        deadline: Checked every few thousand tokens.
        max_chars: Stop after this much output — the truncating cap, applied here because
            RTF can expand a long way and there is no point building a string we will trim.

    Returns:
        The text, with ``\\par`` rendered as a newline.
    """
    source = data.decode("ascii", "replace")
    out: list[str] = []
    length = 0
    index = 0
    end = len(source)
    depth = 0
    #: Depth at which we started skipping, or None. Everything deeper is skipped too.
    skip_from: int | None = None
    #: ``\uc`` — how many fallback characters follow each ``\uN``. Per group in the spec;
    #: one document-wide value is the pragmatic reading and is right for real files.
    unicode_skip = 1
    pending_skip = 0

    while index < end and length < max_chars:
        if index % 8192 == 0:
            deadline.check("rtf")
        char = source[index]

        if char == "{":
            depth += 1
            index += 1
            continue
        if char == "}":
            if skip_from is not None and depth <= skip_from:
                skip_from = None
            depth -= 1
            index += 1
            continue

        if char == "\\":
            index += 1
            if index >= end:
                break
            nxt = source[index]
            if nxt == "*":
                # \* marks an optional destination: skip the whole group.
                if skip_from is None:
                    skip_from = depth
                index += 1
                continue
            if nxt in "\\{}":
                if skip_from is None and not pending_skip:
                    out.append(nxt)
                    length += 1
                index += 1
                continue
            if nxt == "'":
                hex_digits = source[index + 1 : index + 3]
                index += 3
                if pending_skip:
                    pending_skip -= 1
                    continue
                if skip_from is None:
                    try:
                        out.append(bytes([int(hex_digits, 16)]).decode("cp1252", "replace"))
                        length += 1
                    except ValueError:
                        pass
                continue
            if not nxt.isalpha():
                # A control symbol such as \~ or \_ .
                if skip_from is None and nxt in _SYMBOLS:
                    out.append(_SYMBOLS[nxt])
                    length += 1
                index += 1
                continue

            start = index
            while index < end and source[index].isalpha():
                index += 1
            word = source[start:index]
            number = ""
            if index < end and (source[index] == "-" or source[index].isdigit()):
                num_start = index
                if source[index] == "-":
                    index += 1
                while index < end and source[index].isdigit():
                    index += 1
                number = source[num_start:index]
            if index < end and source[index] == " ":
                index += 1               # the delimiting space is part of the control word

            if word in _SKIP_DESTINATIONS:
                if skip_from is None:
                    skip_from = depth
                continue
            if skip_from is not None:
                continue
            if word == "uc":
                unicode_skip = max(0, int(number or 0))
                continue
            if word == "u":
                try:
                    code = int(number or 0)
                except ValueError:
                    code = 0
                if code < 0:
                    code += 0x10000
                if 0 < code < 0x110000:
                    out.append(chr(code))
                    length += 1
                pending_skip = unicode_skip
                continue
            if word in _BREAKS:
                out.append("\n")
                length += 1
                continue
            if word in _SPACES:
                out.append(" ")
                length += 1
                continue
            if word in _SYMBOLS:
                out.append(_SYMBOLS[word])
                length += 1
                continue
            continue

        if char in "\r\n":
            index += 1
            continue
        if skip_from is None:
            if pending_skip:
                pending_skip -= 1
            else:
                out.append(char)
                length += 1
        index += 1

    return "".join(out)


def parse_rtf(
    data: bytes, builder: LayoutBuilder, limits: IngestLimits, deadline: Deadline
) -> None:
    """De-tokenise, then treat the result as text.

    RTF *does* carry heading styles, in ``\\s`` style references that point into a
    ``\\stylesheet`` we discard. Resolving them would be a second parser for a format that is
    a rounding error in a modern KYC pipeline, so every block is body and the report says so
    rather than the code pretending otherwise.
    """
    text = rtf_to_text(data, deadline, max_chars=limits.max_chars)
    if not text.strip():
        raise MalformedDocument(
            "RTF contains no readable text — the file is markup only, or not really RTF"
        )
    builder.page(1)
    builder.lines(text, zone=Zone.body, page=1)


__all__ = ["parse_csv", "parse_rtf", "parse_txt", "rtf_to_text"]
