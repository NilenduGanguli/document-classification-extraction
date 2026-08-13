"""Every supported format, and the structure each one is supposed to preserve.

The assertions that matter are not "we got some text" — they are "the DOCX ``Title`` style
became :attr:`~dce.models.Zone.title` and the running header became
:attr:`~dce.models.Zone.furniture`". Zone signal is the thing defect class 3 showed us we
were throwing away, and a parser that returns a flat string passes a text-only test while
silently costing every zone-gated anchor in the registry.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from dce.adapters import ROLE_ZONES  # noqa: E402
from dce.ingest import (  # noqa: E402
    IngestStatus,
    MalformedDocument,
    MediaType,
    TextSource,
    UnsupportedFormat,
    ingest,
)
from dce.models import Zone  # noqa: E402
from tests import ingest_fixtures as fixtures  # noqa: E402

pytest.importorskip("fitz", reason="PyMuPDF is the optional .[pdf] extra")


def zones(result, zone: Zone) -> list[str]:
    return [b.text for b in result.view.blocks if b.zone is zone]


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def test_docx_preserves_styles_tables_and_furniture():
    result = ingest(
        fixtures.docx(
            [
                ("Title", "CERTIFICATE OF INCORPORATION"),
                ("Heading1", "Article I - Name"),
                ("", "The name of the corporation is ACME HOLDINGS INC."),
            ],
            tables=[[["Shareholder", "Shares"], ["Jane Roe", "100"]]],
            header="ACME HOLDINGS INC.",
            footer="Page 1 of 4",
        )
    )
    assert result.status is IngestStatus.ok
    assert result.media_type is MediaType.docx
    assert result.text_source is TextSource.native
    assert zones(result, Zone.title) == ["CERTIFICATE OF INCORPORATION"]
    assert zones(result, Zone.heading) == ["Article I - Name"]
    assert "The name of the corporation is ACME HOLDINGS INC." in zones(result, Zone.body)
    assert set(zones(result, Zone.furniture)) == {"ACME HOLDINGS INC.", "Page 1 of 4"}
    assert result.view.tables and result.view.tables[0].row_count == 2


def test_docx_style_ids_are_matched_regardless_of_spacing_and_case():
    """Word writes ``Heading1``; some producers write ``heading 1``. Both are headings."""
    result = ingest(
        fixtures.docx([("heading 1", "One"), ("HEADING2", "Two"), ("Subtitle", "Three")])
    )
    assert zones(result, Zone.heading) == ["One", "Two", "Three"]


def test_docx_zone_vocabulary_matches_the_azure_adapter():
    """Ingestion must not invent a zone the reference producer cannot emit."""
    result = ingest(
        fixtures.docx([("Title", "A"), ("Heading1", "B"), ("", "C")], header="H")
    )
    produced = {block.zone for block in result.view.blocks}
    assert produced <= set(ROLE_ZONES.values()) | {Zone.body, Zone.table}


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------
def test_xlsx_sheets_become_pages_with_the_name_as_a_heading():
    result = ingest(
        fixtures.xlsx(
            {
                "Balance Sheet": [["Assets", "2025"], ["Cash", "1000"]],
                "Beneficial Owners": [["Name", "Percent"], ["Jane Roe", "51"]],
            }
        )
    )
    assert result.page_count == 2
    assert zones(result, Zone.heading) == ["Balance Sheet", "Beneficial Owners"]
    assert len(result.view.tables) == 2
    assert {t.page for t in result.view.tables} == {1, 2}
    assert "Jane Roe" in result.view.text()


def test_xlsx_cell_references_keep_columns_aligned():
    """A row that skips a column must not shift the remaining cells left."""
    from dce.ingest.ooxml import _column_index

    assert _column_index("A1", 0) == 0
    assert _column_index("Z9", 0) == 25
    assert _column_index("AA1", 0) == 26
    assert _column_index("BC7", 0) == 54
    assert _column_index("", 3) == 3


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------
def test_pptx_title_placeholders_become_titles_and_slides_become_pages():
    result = ingest(
        fixtures.pptx([("KYC Onboarding", ["first point"]), ("Risk Appetite", ["second"])])
    )
    assert result.page_count == 2
    assert zones(result, Zone.title) == ["KYC Onboarding", "Risk Appetite"]
    assert [b.page for b in result.view.blocks if b.zone is Zone.title] == [1, 2]


# ---------------------------------------------------------------------------
# ODT
# ---------------------------------------------------------------------------
def test_odt_outline_levels_map_to_title_and_heading():
    result = ingest(
        fixtures.odt([("h1", "DEED OF TRUST"), ("h2", "Recitals"), ("p", "Made on 1 Jan.")])
    )
    assert zones(result, Zone.title) == ["DEED OF TRUST"]
    assert zones(result, Zone.heading) == ["Recitals"]
    assert zones(result, Zone.body) == ["Made on 1 Jan."]


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def test_html_headings_navigation_and_tables_are_zoned():
    html = (
        b"<html><head><title>Annual Report</title></head><body>"
        b"<nav>Home | Investors</nav>"
        b"<h1>FORM 10-K</h1><h2>Item 1. Business</h2><p>We do things.</p>"
        b"<table><tr><th>Year</th><th>Revenue</th></tr><tr><td>2025</td><td>100</td></tr>"
        b"</table><script>alert('x')</script><footer>Copyright</footer></body></html>"
    )
    result = ingest(html)
    assert result.media_type is MediaType.html
    assert zones(result, Zone.title) == ["Annual Report", "FORM 10-K"]
    assert zones(result, Zone.heading) == ["Item 1. Business"]
    assert set(zones(result, Zone.furniture)) == {"Home | Investors", "Copyright"}
    assert "alert" not in result.view.text()
    assert result.view.tables[0].cells[0].is_header is True


def test_html_script_and_style_content_never_reaches_the_classifier():
    html = b"<html><body><style>.a{color:red}</style><p>real text</p></body></html>"
    assert "color" not in ingest(html).view.text()


# ---------------------------------------------------------------------------
# EML / MSG
# ---------------------------------------------------------------------------
EML = (
    b"From: registrar@example.test\r\n"
    b"To: kyc@example.test\r\n"
    b"Subject: Certificate of Good Standing\r\n"
    b"Date: Mon, 1 Jan 2026 09:00:00 +0000\r\n"
    b'Content-Type: multipart/mixed; boundary="B"\r\n'
    b"\r\n"
    b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
    b"The certificate is attached.\r\n"
    b"--B\r\nContent-Type: application/pdf\r\n"
    b'Content-Disposition: attachment; filename="cert.pdf"\r\n\r\n'
    b"JVBERi0=\r\n"
    b"--B--\r\n"
)


def test_eml_subject_is_the_title_and_envelope_is_furniture():
    result = ingest(EML)
    assert result.media_type is MediaType.eml
    assert zones(result, Zone.title) == ["Certificate of Good Standing"]
    assert any(t.startswith("From: registrar@example.test") for t in zones(result, Zone.furniture))
    assert "The certificate is attached." in zones(result, Zone.body)


def test_eml_attachments_are_named_but_never_opened():
    """An attached PDF is a different document with a different doctype."""
    result = ingest(EML)
    furniture = zones(result, Zone.furniture)
    assert any("Attachment: cert.pdf" in t for t in furniture)
    assert "JVBERi0" not in result.view.text()


def test_msg_reads_the_same_shape_as_eml():
    result = ingest(
        fixtures.msg(
            "Re: KYC documents",
            "Please find the certificate attached.\nRegards",
            attachment="cert.pdf",
        )
    )
    assert result.media_type is MediaType.msg
    assert zones(result, Zone.title) == ["Re: KYC documents"]
    assert any("Attachment: cert.pdf" in t for t in zones(result, Zone.furniture))
    assert "Please find the certificate attached." in zones(result, Zone.body)


def test_msg_reads_streams_from_both_the_mini_fat_and_the_fat():
    """Small properties live in the mini stream; a long body does not. Both must work."""
    body = "PARAGRAPH\n" * 700          # comfortably over the 4096-byte mini cutoff
    result = ingest(fixtures.msg("Short subject", body))
    assert zones(result, Zone.title) == ["Short subject"]
    assert zones(result, Zone.body).count("PARAGRAPH") == 700


# ---------------------------------------------------------------------------
# TXT / CSV / RTF
# ---------------------------------------------------------------------------
def test_txt_states_no_structure_so_nothing_is_promoted():
    """The same reasoning as :func:`dce.adapters.from_plain_text`: no guessed titles."""
    result = ingest(b"FORM W-9\nRequest for Taxpayer Identification Number\n")
    assert {b.zone for b in result.view.blocks} == {Zone.body}


def test_csv_becomes_a_table_with_a_header_row():
    result = ingest(b"Name,Shares,Percent\nJane Roe,100,51\nJohn Doe,96,49\n")
    assert result.media_type is MediaType.csv
    table = result.view.tables[0]
    assert table.row_count == 3
    assert [c.text for c in table.cells if c.is_header] == ["Name", "Shares", "Percent"]


def test_rtf_control_words_and_escapes_are_decoded():
    rtf = (
        b"{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Times;}}"
        b"{\\*\\generator Riched20 10.0}"
        b"\\b SOCIEDAD ANONIMA\\b0\\par Direcci\\'f3n social\\par}"
    )
    result = ingest(rtf)
    assert result.media_type is MediaType.rtf
    assert "SOCIEDAD ANONIMA" in result.view.text()
    assert "Dirección social" in result.view.text()
    # The font table and the generator destination are markup, not content.
    assert "Times" not in result.view.text()
    assert "Riched20" not in result.view.text()


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def test_pdf_with_a_text_layer_is_native_text_and_all_body():
    result = ingest(
        fixtures.text_pdf(
            [
                "FORM W-9",
                "Request for Taxpayer Identification Number and Certification",
                "Department of the Treasury Internal Revenue Service",
            ]
        )
    )
    assert result.media_type is MediaType.pdf
    assert result.status is IngestStatus.ok
    assert result.text_source is TextSource.native
    assert {b.zone for b in result.view.blocks} == {Zone.body}
    assert "FORM W-9" in result.view.text()


def test_pdf_pages_carry_geometry():
    result = ingest(fixtures.text_pdf(["hello there world"], pages=3))
    assert result.page_count == 3
    assert all(page.width > 0 and page.height > 0 for page in result.view.pages)
    assert result.view.pages[0].unit == "point"


def test_scanned_pdf_is_needs_ocr_not_an_empty_classification():
    result = ingest(fixtures.scanned_pdf())
    assert result.status is IngestStatus.needs_ocr
    assert result.view is None
    assert "no usable text layer" in result.reason
    assert "opens no socket at all" in result.remedy
    assert "off by default" in result.remedy


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("data", "media_type"),
    [
        (fixtures.png(), MediaType.png),
        (fixtures.jpeg(), MediaType.jpeg),
        (fixtures.multipage_tiff(), MediaType.tiff),
    ],
)
def test_images_return_a_structured_needs_ocr(data: bytes, media_type: MediaType):
    result = ingest(data)
    assert result.status is IngestStatus.needs_ocr
    assert result.media_type is media_type
    assert result.view is None
    assert result.ocr_available is False
    assert "no text layer" in result.reason
    detail = result.as_detail()
    assert detail["status"] == "needs_ocr"
    assert detail["media_type"] == str(media_type)


def test_multipage_tiff_frames_are_counted():
    assert ingest(fixtures.multipage_tiff(pages=5)).page_count == 5


def test_image_probe_reports_dimensions():
    from dce.ingest.images import ImageInfo, probe

    assert probe(fixtures.png(64, 48), MediaType.png) == ImageInfo(64, 48, 1)
    assert probe(fixtures.jpeg(100, 200), MediaType.jpeg) == ImageInfo(100, 200, 1)
    assert probe(fixtures.multipage_tiff(3, 80, 60), MediaType.tiff) == ImageInfo(80, 60, 3)


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------
def test_a_document_with_no_text_is_refused_rather_than_classified_empty():
    with pytest.raises(UnsupportedFormat, match="contains no text"):
        ingest(fixtures.docx([("", "   ")]))


def test_a_truncated_archive_is_a_clean_error():
    with pytest.raises(MalformedDocument):
        ingest(fixtures.docx([("", "hello world")])[:64])


def test_a_malformed_xml_part_is_a_clean_error():
    with pytest.raises(MalformedDocument, match="not well-formed"):
        ingest(fixtures.zip_bytes({"word/document.xml": "<w:document <<<"}))


def test_a_password_protected_pdf_is_named_as_such():
    import fitz

    document = fitz.open()
    document.new_page().insert_text((72, 72), "SECRET FILING")
    data = document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    document.close()
    with pytest.raises(MalformedDocument, match="password-protected"):
        ingest(data)
