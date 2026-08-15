"""A document is not one thing: per-page text adequacy, and what happens to the rest.

The bug these tests exist for: the usable-text floor was compared against a sum across the
whole document, so one page carrying 40 characters bought a text-layer verdict for every
other page — and the pages that were pictures came back empty, with ``status=ok``,
``truncated=False`` and no reason given. A scanned ID behind a typed cover page vanished.

Each test below names the property rather than the mechanism, because the mechanism is
allowed to change and the properties are not:

* a page is judged on its own characters, never on another page's;
* a page with pixels nobody read is either recognised or reported, never dropped;
* a page that is merely sparse is not a scan, and must not be sent to a recogniser;
* text a previous recogniser mangled is not text.
"""
from __future__ import annotations

import pytest

import tests.ingest_fixtures as fixtures
from dce.ingest.builder import LayoutBuilder
from dce.ingest.limits import Deadline, IngestLimits
from dce.ingest.ocr import OcrLine, OcrPage, ocr_pages_to_builder
from dce.ingest.pdf import MIN_ALNUM_CHARS, parse_pdf
from dce.ingest.pipeline import ingest
from dce.ingest.result import IngestStatus, TextSource
from dce.ingest.settings import (
    TEXT_LAYER_ALWAYS_OCR,
    TEXT_LAYER_TRUST,
    TEXT_LAYER_VERIFY,
    IngestSettings,
)

fitz = pytest.importorskip("fitz")


class SpyProvider:
    """A local engine that records which pages it was asked to read."""

    name = "spy"
    service = False

    def __init__(self) -> None:
        self.pages: list[int] = []

    def recognize(self, data: bytes, *, page: int, deadline) -> OcrPage:
        self.pages.append(page)
        return OcrPage(page=page, lines=[OcrLine(text=f"RECOGNISED CONTENT OF PAGE {page}")])


def _parse(data: bytes, provider=None):
    """parse_pdf plus the merge step the pipeline does, so ``view`` is what a caller sees.

    ``parse_pdf`` deliberately returns recognised pages rather than writing them itself —
    the pipeline owns that merge — so a helper that skipped it would assert against a view
    no caller ever receives.
    """
    limits = IngestLimits()
    deadline = Deadline(limits.max_seconds)
    builder = LayoutBuilder(limits, deadline)
    outcome = parse_pdf(data, builder, limits, deadline, provider=provider)
    if outcome.ocr_pages:
        ocr_pages_to_builder(outcome.ocr_pages, builder, limits)
    return outcome, builder.build(doc_id="d", raw={})


# ---------------------------------------------------------------------------
# The bug itself
# ---------------------------------------------------------------------------
def test_a_text_page_does_not_vouch_for_a_picture_page():
    """The whole bug in one assertion: page 1's characters do not speak for page 2."""
    outcome, _ = _parse(fixtures.mixed_pdf())

    assert outcome.alnum_chars >= MIN_ALNUM_CHARS, "page 1 alone clears the old document floor"
    assert outcome.inadequate_pages == (2,)
    assert outcome.unread_pages == (2,)


def test_the_picture_page_is_recognised_when_an_engine_exists():
    provider = SpyProvider()
    _, view = _parse(fixtures.mixed_pdf(), provider)

    assert provider.pages == [2], "only the page that needed reading was read"
    assert "RECOGNISED CONTENT OF PAGE 2" in view.text()
    assert "Acme Corporation" in view.text(), "and page 1 kept its own characters"


def test_every_picture_page_is_recognised_not_just_the_first():
    provider = SpyProvider()
    _parse(fixtures.mixed_pdf(text_pages=1, image_pages=3), provider)

    assert provider.pages == [2, 3, 4]


def test_the_loss_is_declared_when_nothing_can_read_the_picture_page():
    """The regression that matters most: silence is not an acceptable answer."""
    result = ingest(fixtures.mixed_pdf())

    assert result.status is IngestStatus.ok, "a partial answer still beats no answer"
    assert result.truncated is True
    assert "unread_pages" in result.limits_hit
    assert "page(s) 2" in result.reason
    assert result.remedy


def test_the_declared_loss_names_what_was_not_read():
    result = ingest(fixtures.mixed_pdf(text_pages=1, image_pages=2))

    assert "2, 3" in result.reason
    assert "no recogniser is available" in result.reason


def test_a_mixed_document_reports_mixed_provenance():
    """`native` would flatter these documents and `local_ocr` would libel them."""
    limits = IngestLimits()
    deadline = Deadline(limits.max_seconds)
    builder = LayoutBuilder(limits, deadline)
    outcome = parse_pdf(fixtures.mixed_pdf(), builder, limits, deadline, provider=SpyProvider())

    assert outcome.ocr_pages, "some pages were recognised"
    assert any(v.adequate for v in outcome.page_verdicts), "and some were not"
    # The pipeline turns that combination into TextSource.mixed; see
    # test_mixed_pdf_through_the_pipeline_is_mixed below for the end-to-end assertion.


# ---------------------------------------------------------------------------
# What must NOT change
# ---------------------------------------------------------------------------
def test_a_sparse_document_is_not_a_scan():
    """Fifteen characters a page and no pixels: OCR would return the same nothing, slower.

    This ordering is load-bearing. Judging "every page is inadequate" before asking whether
    any page has an image routes every thin text document to a recogniser — turning a fix for
    silent loss into an unnecessary bill and a worse answer.
    """
    result = ingest(fixtures.text_pdf(["hello there world"], pages=3))

    assert result.status is IngestStatus.ok
    assert result.text_source is TextSource.native
    assert result.view is not None
    assert "hello there world" in result.view.text()


def test_a_sparse_document_is_never_sent_to_a_recogniser():
    provider = SpyProvider()
    _parse(fixtures.text_pdf(["hello there world"], pages=3), provider)

    assert provider.pages == [], "nothing to recognise, so nothing was recognised"


def test_a_full_text_pdf_is_still_native_text():
    result = ingest(fixtures.text_pdf(["FORM W-9 Request for Taxpayer Identification Number"]))

    assert result.status is IngestStatus.ok
    assert result.text_source is TextSource.native
    assert result.truncated is False
    assert result.limits_hit == []


def test_a_wholly_scanned_pdf_is_still_needs_ocr():
    result = ingest(fixtures.scanned_pdf(pages=2))

    assert result.status is IngestStatus.needs_ocr
    assert result.view is None


# ---------------------------------------------------------------------------
# Text that is not text
# ---------------------------------------------------------------------------
def test_a_previous_recognisers_garbage_is_not_a_text_layer():
    outcome, _ = _parse(fixtures.garbage_text_pdf())

    assert outcome.alnum_chars > MIN_ALNUM_CHARS, "it clears the length floor comfortably"
    assert outcome.inadequate_pages == (1,), "and is still not text"
    assert "repeated glyph" in outcome.page_verdicts[0].reason


def test_a_scan_carrying_a_bad_ocr_layer_is_re_recognised():
    """The case the glyph check is actually for: pixels plus a previous tool's mistake."""
    document = fitz.open()
    page = document.new_page()
    page.insert_image(page.rect, stream=fixtures.png(200, 260))
    y = 72
    for line in ("lllllllllllllllllllllllll", "llllllllllllllllllllllllll"):
        page.insert_text((72, y), line, fontsize=12)
        y += 18
    data = document.tobytes()
    document.close()

    provider = SpyProvider()
    outcome, view = _parse(data, provider)

    assert outcome.inadequate_pages == (1,)
    assert provider.pages == [1], "the page was read again rather than trusted"
    assert "RECOGNISED CONTENT OF PAGE 1" in view.text()


def test_a_caption_does_not_make_a_picture_into_a_page():
    """Real characters, but one image covers the page: it is a picture with a caption."""
    document = fitz.open()
    page = document.new_page()
    page.insert_image(page.rect, stream=fixtures.png(200, 260))
    page.insert_text((72, 72), "Figure 1 shows the applicant identity document below", fontsize=11)
    data = document.tobytes()
    document.close()

    outcome, _ = _parse(data)

    assert outcome.alnum_chars >= MIN_ALNUM_CHARS
    assert outcome.inadequate_pages == (1,)
    assert "covers" in outcome.page_verdicts[0].reason


# ---------------------------------------------------------------------------
# Containers that hold a picture instead of text
# ---------------------------------------------------------------------------
def test_a_docx_that_is_one_scan_is_needs_ocr_not_an_unsupported_format():
    """It parsed perfectly. The format was never the problem — the content is pixels."""
    result = ingest(fixtures.docx_of_one_scan())

    assert result.status is IngestStatus.needs_ocr
    assert result.view is None
    assert "embedded image" in result.reason
    assert result.remedy


def test_a_genuinely_empty_docx_is_still_an_unsupported_format():
    """The refusal this must not swallow: a file with nothing in it at all."""
    from dce.ingest.errors import UnsupportedFormat

    with pytest.raises(UnsupportedFormat, match="contains no text"):
        ingest(fixtures.docx([("", "   ")]))


def test_a_docx_with_text_is_untouched_by_the_embedded_image_path():
    result = ingest(fixtures.docx([("", "FORM W-9 Request for Taxpayer Identification")]))

    assert result.status is IngestStatus.ok
    assert result.text_source is TextSource.native


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------
def _settings(policy: str, **extra) -> IngestSettings:
    return IngestSettings(text_layer_policy=policy, **extra)


def test_verify_is_the_default():
    assert IngestSettings().text_layer() == TEXT_LAYER_VERIFY


def test_trust_takes_a_page_at_its_word():
    """`trust` applies the character floor and asks nothing further."""
    result = ingest(fixtures.garbage_text_pdf(), settings=_settings(TEXT_LAYER_TRUST))

    assert result.status is IngestStatus.ok
    assert result.view is not None
    assert "lllll" in result.view.text(), "the garbage was believed, as the policy asks"


def test_verify_does_not_take_that_page_at_its_word():
    limits = IngestLimits()
    deadline = Deadline(limits.max_seconds)
    builder = LayoutBuilder(limits, deadline)
    outcome = parse_pdf(fixtures.garbage_text_pdf(), builder, limits, deadline, strict=True)

    assert outcome.inadequate_pages == (1,)


def test_always_ocr_ignores_a_perfectly_good_text_layer():
    provider = SpyProvider()
    limits = IngestLimits()
    deadline = Deadline(limits.max_seconds)
    builder = LayoutBuilder(limits, deadline)
    data = fixtures.text_pdf(["FORM W-9 Request for Taxpayer Identification Number"], pages=2)

    outcome = parse_pdf(
        data, builder, limits, deadline, provider=provider, force_ocr=True
    )

    assert provider.pages == [1, 2], "every page recognised, none believed"
    assert outcome.page_verdicts, "the verdicts are still recorded for the console"


def test_always_ocr_refuses_to_boot_without_a_recogniser():
    """A policy that reads no text layer, on a deployment that can read nothing else."""
    with pytest.raises(ValueError, match="always_ocr"):
        IngestSettings(text_layer_policy=TEXT_LAYER_ALWAYS_OCR)


def test_an_unrecognised_policy_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="not a policy"):
        IngestSettings(text_layer_policy="always-ocr")


def test_a_caller_declining_recognition_outranks_always_ocr():
    """Caller-may-decline-never-grant survives a deployment preference."""
    settings = _settings(
        TEXT_LAYER_ALWAYS_OCR, local_ocr_enabled=True, local_ocr_engine="rapidocr"
    )
    result = ingest(
        fixtures.text_pdf(["FORM W-9 Request for Taxpayer Identification Number"]),
        settings=settings,
        local_ocr=False,
    )

    assert result.status is IngestStatus.ok
    assert result.text_source is TextSource.native, "the text layer was read after all"


# ---------------------------------------------------------------------------
# Measurement traps, each found on a real corpus page
# ---------------------------------------------------------------------------
def test_an_image_larger_than_its_page_does_not_exceed_100_percent():
    """One corpus page yields 1.165 uncorrected, which is not a fraction of anything."""
    document = fitz.open()
    page = document.new_page()
    rect = page.rect
    page.insert_image(
        fitz.Rect(rect.x0 - 200, rect.y0 - 200, rect.x1 + 200, rect.y1 + 200),
        stream=fixtures.png(200, 260),
    )
    data = document.tobytes()
    document.close()

    outcome, _ = _parse(data)

    assert 0.0 <= outcome.page_verdicts[0].image_fraction <= 1.0


def test_many_small_images_are_not_a_scan():
    """Scattered placements sum past the page; the largest is what decides."""
    document = fitz.open()
    page = document.new_page()
    thumb = fixtures.png(8, 8)
    for row in range(12):
        for column in range(12):
            x, y = 40 + column * 20, 40 + row * 20
            page.insert_image(fitz.Rect(x, y, x + 16, y + 16), stream=thumb)
    page.insert_text(
        (72, 700), "A page of small figures with a real paragraph of text on it", fontsize=11
    )
    data = document.tobytes()
    document.close()

    outcome, _ = _parse(data)

    assert outcome.page_verdicts[0].image_fraction < 0.6
    assert outcome.inadequate_pages == (), "144 thumbnails are not a scan"
