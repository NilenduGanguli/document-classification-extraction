"""Content-based type detection, and the guarantee that a filename cannot choose a parser.

The security property under test is narrow and important: an attacker who controls both the
bytes and the name must not be able to aim one of our parsers at bytes that are not that
format. Everything else in this file is in service of that.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from dce.ingest import MediaType, UnsupportedFormat, detect  # noqa: E402
from dce.ingest.detect import decode_text  # noqa: E402
from tests import ingest_fixtures as fixtures  # noqa: E402


# ---------------------------------------------------------------------------
# Magic bytes decide
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", MediaType.pdf),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, MediaType.png),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 32, MediaType.jpeg),
        (b"GIF89a" + b"\x00" * 32, MediaType.gif),
        (b"II\x2a\x00" + b"\x00" * 32, MediaType.tiff),
        (b"MM\x00\x2a" + b"\x00" * 32, MediaType.tiff),
        (b"BM" + b"\x00" * 32, MediaType.bmp),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", MediaType.webp),
        (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00", MediaType.heic),
        (b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00", MediaType.heic),
        (b"{\\rtf1\\ansi hello}", MediaType.rtf),
    ],
)
def test_magic_bytes_identify_binary_formats(data: bytes, expected: MediaType):
    assert detect(data).media_type is expected
    assert detect(data).basis == "magic"


def test_pdf_header_is_found_within_the_first_kilobyte():
    """Real PDFs sometimes carry junk before ``%PDF-``; the spec allows it and so do we."""
    data = b"\n" * 300 + b"%PDF-1.4\n" + b"x" * 100
    assert detect(data).media_type is MediaType.pdf


@pytest.mark.parametrize(
    ("builder", "expected"),
    [
        (lambda: fixtures.docx([("", "hello world")]), MediaType.docx),
        (lambda: fixtures.xlsx({"Sheet1": [["a", "b"]]}), MediaType.xlsx),
        (lambda: fixtures.pptx([("Title", ["body"])]), MediaType.pptx),
        (lambda: fixtures.odt([("p", "hello")]), MediaType.odt),
        (lambda: fixtures.msg("subject", "body"), MediaType.msg),
    ],
)
def test_containers_are_identified_by_their_parts(builder, expected: MediaType):
    detection = detect(builder())
    assert detection.media_type is expected
    assert detection.basis == "container"


# ---------------------------------------------------------------------------
# THE test: the filename never chooses the parser
# ---------------------------------------------------------------------------
def test_a_lying_extension_cannot_choose_the_parser():
    """A PNG named ``passport.pdf`` is detected as a PNG, not handed to the PDF engine."""
    assert detect(fixtures.png(), filename="passport.pdf").media_type is MediaType.png
    assert detect(fixtures.png(), filename="statement.docx").media_type is MediaType.png


def test_a_docx_named_txt_is_still_a_docx():
    assert detect(fixtures.docx([("", "x y z")]), filename="notes.txt").media_type is (
        MediaType.docx
    )


def test_content_beats_the_hint_within_the_text_family_too():
    html = b"<!DOCTYPE html><html><body><p>hello</p></body></html>"
    assert detect(html, filename="notes.txt").media_type is MediaType.html


def test_the_hint_only_breaks_a_tie_the_sniffer_left_open():
    """Prose that is neither HTML, email nor CSV: the hint decides between txt and csv."""
    prose = b"one two three four five\n"
    assert detect(prose).media_type is MediaType.txt
    # A single prose line has no delimiter agreement, so the sniffer abstains and the hint
    # is allowed to pick csv — the worst case is that one text file is read as one column.
    assert detect(prose, filename="ledger.csv").media_type is MediaType.csv


# ---------------------------------------------------------------------------
# Text sniffing
# ---------------------------------------------------------------------------
def test_email_needs_a_real_header_block_not_just_a_colon():
    assert detect(b"Subject: hello\n\nbody text here\n").media_type is MediaType.txt
    message = b"From: a@b.test\nTo: c@d.test\nSubject: hi\n\nbody\n"
    assert detect(message).media_type is MediaType.eml


def test_csv_needs_agreement_across_lines():
    assert detect(b"a,b,c\n1,2,3\n4,5,6\n").media_type is MediaType.csv
    # Prose that happens to contain commas does not agree on a field count.
    assert detect(b"Hello, world\nThis is a sentence.\nAnd, another, one\n").media_type is (
        MediaType.txt
    )


@pytest.mark.parametrize(
    "encoded",
    [
        b"ACTA CONSTITUTIVA",
        "﻿ACTA CONSTITUTIVA".encode("utf-8-sig"),
        "ACTA CONSTITUTIVA".encode("utf-16-le"),
        "ACTA CONSTITUTIVA".encode("utf-16"),
    ],
)
def test_text_decoding_handles_boms_and_utf16(encoded: bytes):
    text, _encoding = decode_text(encoded)
    assert "ACTA CONSTITUTIVA" in text


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def test_empty_upload_is_refused():
    with pytest.raises(UnsupportedFormat, match="empty upload"):
        detect(b"")


def test_binary_noise_is_not_read_as_text():
    """Every byte under 0x80 is valid UTF-8, so "it decoded" is not enough."""
    with pytest.raises(UnsupportedFormat):
        detect(bytes(range(8)) * 64)


def test_legacy_ole2_office_is_refused_with_a_remedy():
    """A ``.doc`` is a compound file too; it must not fall into the MSG parser."""
    legacy = fixtures.compound_file({"WordDocument": b"\x00" * 8192})
    with pytest.raises(UnsupportedFormat, match="not an Outlook"):
        detect(legacy)


def test_a_bare_zip_is_not_a_document():
    with pytest.raises(UnsupportedFormat, match="no recognised office part"):
        detect(fixtures.zip_bytes({"a.txt": "hello", "b.txt": "world"}))
