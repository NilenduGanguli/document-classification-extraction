"""Resource guards: a malformed or hostile upload gets a clean error, never a hang.

Ingestion is the first code in this service that touches attacker-controlled *bytes*. The
properties asserted here are the ones an attacker would try to break: unbounded memory
(zip bombs, XML entity expansion), unbounded time (the deadline), and silent truncation
(a document that was shortened without saying so classifies as a different document).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from dce.ingest import (  # noqa: E402
    ArchiveBomb,
    Deadline,
    IngestError,
    IngestLimits,
    IngestTimeout,
    MalformedDocument,
    PayloadTooLarge,
    ingest,
)
from dce.ingest.ooxml import parse_xml  # noqa: E402
from tests import ingest_fixtures as fixtures  # noqa: E402


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------
def test_an_oversized_upload_is_refused_before_a_parser_is_chosen():
    with pytest.raises(PayloadTooLarge, match="over the 1024-byte cap"):
        ingest(b"%PDF-1.4\n" + b"x" * 4096, limits=IngestLimits(max_bytes=1024))


# ---------------------------------------------------------------------------
# Zip bombs
# ---------------------------------------------------------------------------
def test_an_archive_with_too_many_members_is_refused_before_it_is_opened():
    members = {f"junk{index}.bin": b"x" for index in range(300)}
    members["word/document.xml"] = fixtures.docx([("", "hello world")])
    with pytest.raises(ArchiveBomb, match="declares 301 members"):
        ingest(fixtures.zip_bytes(members), limits=IngestLimits(max_archive_entries=50))


def test_a_bomb_in_the_part_we_actually_read_is_refused():
    """``word/document.xml`` itself expanding past the per-member cap."""
    with pytest.raises(ArchiveBomb, match="per-member cap"):
        ingest(
            fixtures.oversized_docx(paragraphs=4_000),
            limits=IngestLimits(max_archive_entry_bytes=64 * 1024),
        )


def test_an_implausible_compression_ratio_is_refused():
    with pytest.raises(ArchiveBomb, match="compression ratio"):
        ingest(
            fixtures.oversized_docx(paragraphs=2_000),
            limits=IngestLimits(max_compression_ratio=5.0),
        )


def test_a_bomb_in_a_member_we_never_read_costs_nothing():
    """We inflate only the parts the parser asks for, so the other members are free.

    This is the property that makes the per-member cap sufficient rather than a lottery: an
    attacker cannot make us pay for a member we do not open.
    """
    started = time.perf_counter()
    result = ingest(fixtures.zip_bomb(entries=8, uncompressed=32 * 1024 * 1024))
    assert result.status.value == "ok"
    assert time.perf_counter() - started < 5.0


# ---------------------------------------------------------------------------
# XML entity expansion
# ---------------------------------------------------------------------------
def test_an_xml_part_declaring_a_dtd_is_refused():
    """Billion laughs expands inside the C parser, below anything our loops could see."""
    payload = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&lol2;</w:t></w:r></w:p></w:body></w:document>"
    )
    with pytest.raises(MalformedDocument, match=r"DTD|entity"):
        ingest(fixtures.zip_bytes({"word/document.xml": payload}))


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def test_the_deadline_stops_a_long_parse():
    with pytest.raises(IngestTimeout, match="max_seconds"):
        ingest(("a line of text\n" * 200_000).encode(), limits=IngestLimits(max_seconds=0.001))


def test_deadline_is_a_no_op_when_disabled():
    deadline = Deadline(0)
    deadline.check("stage")            # must not raise
    assert deadline.remaining == float("inf")
    assert deadline.expired is False


# ---------------------------------------------------------------------------
# Truncation is never silent
# ---------------------------------------------------------------------------
def test_hitting_the_block_cap_is_reported_not_hidden():
    result = ingest(("line\n" * 5_000).encode(), limits=IngestLimits(max_blocks=10))
    assert result.block_count == 10
    assert result.truncated is True
    assert "max_blocks" in result.limits_hit


def test_hitting_the_page_cap_is_reported():
    sheets = {f"Sheet{index}": [["value", str(index)]] for index in range(1, 9)}
    result = ingest(fixtures.xlsx(sheets), limits=IngestLimits(max_pages=3))
    assert result.truncated is True
    assert "max_pages" in result.limits_hit
    assert max(page.page for page in result.view.pages) == 3


def test_hitting_the_table_row_cap_is_reported():
    rows = "\n".join(f"row{index},{index}" for index in range(2_000))
    result = ingest(
        f"name,value\n{rows}\n".encode(), limits=IngestLimits(max_table_rows=25)
    )
    assert result.truncated is True
    assert "max_table_rows" in result.limits_hit
    assert result.view.tables[0].row_count == 25


def test_an_overlong_single_block_is_marked_where_it_was_cut():
    from dce.ingest.builder import TRUNCATION_MARKER

    result = ingest(("W" * 5_000).encode(), limits=IngestLimits(max_block_chars=100))
    assert result.view.blocks[0].text.endswith(TRUNCATION_MARKER)
    assert "max_block_chars" in result.limits_hit


# ---------------------------------------------------------------------------
# Malformed compound files
# ---------------------------------------------------------------------------
def test_a_cfb_with_a_looping_sector_chain_does_not_hang():
    """A FAT entry pointing at its own sector is the cheapest possible infinite loop."""
    data = bytearray(fixtures.msg("subject line", "body text"))
    # Corrupt the first directory sector pointer into a self-referential chain by pointing
    # the header's directory start at a sector whose FAT entry is itself.
    header_dir_start = 0x30
    data[header_dir_start : header_dir_start + 4] = (0).to_bytes(4, "little")
    started = time.perf_counter()
    with pytest.raises(MalformedDocument):
        ingest(bytes(data))
    assert time.perf_counter() - started < 5.0


def test_a_truncated_compound_file_is_a_clean_error():
    """Which error depends on how much survived; that it is a clean ``IngestError`` does not.

    Cut short enough and the property streams are gone, so it stops looking like a ``.msg``
    at detection (``UnsupportedFormat``); cut later and the reader refuses it
    (``MalformedDocument``). Both are the contract: one of our own exceptions, promptly.
    """
    full = fixtures.msg("subject line", "body text")
    for cut in (600, len(full) // 2, len(full) - 64):
        with pytest.raises(IngestError):
            ingest(full[:cut])


# ---------------------------------------------------------------------------
# DTD refusal must not depend on where in the prolog the declaration sits
# ---------------------------------------------------------------------------
_ENTITY_BOMB = (
    b'<!DOCTYPE lolz [<!ENTITY a "AAAAAAAAAA">'
    b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
    b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
    b'<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">]>'
)


@pytest.mark.parametrize(
    ("label", "prolog"),
    [
        ("no padding", b""),
        ("comment past a 4 KB window", b"<!-- " + b"A" * 5000 + b" -->"),
        ("processing instruction past it", b"<?pi " + b"B" * 6000 + b"?>"),
        ("both", b"<!-- " + b"A" * 5000 + b" --><?pi " + b"B" * 6000 + b"?>"),
    ],
)
def test_entity_bomb_is_refused_wherever_the_doctype_sits(label, prolog):
    """The guard must be structural, not a fixed-size byte scan.

    An earlier form read only ``data[:4096]``. XML allows unbounded comments and processing
    instructions before the DOCTYPE, so padding the prolog moved the declaration past the
    window and expat expanded the entities: a 6.5 MB upload reached 1.86 GB of resident
    memory against a 2 GB container. Widening the window fixes nothing — nothing bounds a
    prolog. Each parametrisation is a way to push the declaration further down.
    """
    payload = b'<?xml version="1.0"?>' + prolog + _ENTITY_BOMB + b"<r>&d;</r>"
    with pytest.raises(MalformedDocument, match=r"DTD|entity"):
        parse_xml(payload, part=f"word/document.xml [{label}]")


def test_external_entity_reference_is_refused():
    """XXE, which here is also an egress question: a parser must not fetch anything."""
    payload = (
        b'<?xml version="1.0"?>' + b"<!-- " + b"A" * 5000 + b" -->"
        b'<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>'
    )
    with pytest.raises(MalformedDocument, match=r"DTD|entity"):
        parse_xml(payload, part="word/document.xml")


def test_a_legitimate_office_part_still_parses():
    """The guard must not cost the ordinary case — every real part has no DTD."""
    element = parse_xml(
        b'<?xml version="1.0"?><w:p xmlns:w="u"><w:t>hello</w:t></w:p>', part="word/document.xml"
    )
    assert element is not None
