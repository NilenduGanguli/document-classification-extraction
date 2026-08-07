"""The API surface of ingestion: one new field, and what it does to a request.

Reuses the stub-engine harness from :mod:`tests.test_api`, so these tests exercise the real
routes with the real ingestion path and only the classifier/extractor substituted.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from dce.models import Zone  # noqa: E402
from tests import ingest_fixtures as fixtures  # noqa: E402
from tests.test_api import build_app  # noqa: E402

pytest.importorskip("fitz", reason="PyMuPDF is the optional .[pdf] extra")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


DOCX = fixtures.docx(
    [
        ("Title", "CERTIFICATE OF INCORPORATION"),
        ("Heading1", "Article I"),
        ("", "The name of the corporation is ACME HOLDINGS INC."),
    ],
    header="Registered Office",
)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_a_docx_upload_reaches_the_classifier_with_its_zones_intact():
    client, classifier, _ = build_app()
    response = client.post(
        "/api/v1/classify",
        json={"doc_id": "doc-1", "content_base64": b64(DOCX), "ingest": {}},
    )
    assert response.status_code == 200
    view = classifier.views[-1]
    assert view.doc_id == "doc-1"
    assert [b.text for b in view.blocks if b.zone is Zone.title] == [
        "CERTIFICATE OF INCORPORATION"
    ]
    assert [b.text for b in view.blocks if b.zone is Zone.furniture] == ["Registered Office"]
    assert view.raw["provider"] == "dce.ingest"
    assert view.raw["media_type"] == "docx"


def test_process_works_the_same_way():
    client, classifier, _ = build_app()
    response = client.post(
        "/api/v1/process", json={"content_base64": b64(DOCX), "ingest": {}}
    )
    assert response.status_code == 200
    assert classifier.views


def test_the_filename_hint_is_accepted_but_does_not_choose_the_parser():
    client, classifier, _ = build_app()
    response = client.post(
        "/api/v1/classify",
        json={
            "content_base64": b64(DOCX),
            "ingest": {"filename": "totally-a-spreadsheet.xlsx"},
        },
    )
    assert response.status_code == 200
    assert classifier.views[-1].raw["media_type"] == "docx"


# ---------------------------------------------------------------------------
# needs_ocr is a structured 422, not a guessed classification
# ---------------------------------------------------------------------------
def test_an_image_returns_a_structured_needs_ocr_and_never_reaches_the_classifier():
    client, classifier, _ = build_app()
    response = client.post(
        "/api/v1/classify",
        json={"content_base64": b64(fixtures.jpeg()), "ingest": {"filename": "passport.jpg"}},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["status"] == "needs_ocr"
    assert detail["media_type"] == "jpeg"
    assert detail["ocr_available"] is False
    assert "optical recognition" in detail["reason"]
    assert "will not call a cloud OCR API" in detail["remedy"]
    # The load-bearing half: nothing was classified. An abstention here would be a lie about
    # which stage gave up — the cascade never saw a single character.
    assert classifier.views == []


def test_a_scanned_pdf_gets_the_same_treatment_as_a_jpeg():
    client, classifier, _ = build_app()
    response = client.post(
        "/api/v1/classify",
        json={"content_base64": b64(fixtures.scanned_pdf()), "ingest": {}},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["status"] == "needs_ocr"
    assert classifier.views == []


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
def test_ingest_without_bytes_is_a_400():
    client, _, _ = build_app()
    response = client.post("/api/v1/classify", json={"text": "hello", "ingest": {}})
    assert response.status_code == 400
    assert "content_base64" in response.json()["detail"]


def test_an_unsupported_container_is_a_415_with_a_machine_readable_code():
    client, _, _ = build_app()
    response = client.post(
        "/api/v1/classify",
        json={"content_base64": b64(fixtures.zip_bytes({"a.txt": "x"})), "ingest": {}},
    )
    assert response.status_code == 415
    assert response.json()["detail"]["error"] == "unsupported_format"


def test_a_malformed_office_part_is_a_400_with_a_code():
    client, _, _ = build_app()
    payload = fixtures.zip_bytes({"word/document.xml": "<w:document <<<"})
    response = client.post(
        "/api/v1/classify", json={"content_base64": b64(payload), "ingest": {}}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "malformed_document"


def test_invalid_base64_is_still_a_400():
    client, _, _ = build_app()
    response = client.post(
        "/api/v1/classify", json={"content_base64": "not base64!!", "ingest": {}}
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Backwards compatibility: the field is opt-in and changes nothing when absent
# ---------------------------------------------------------------------------
def test_content_base64_without_ingest_keeps_its_old_meaning():
    """Unset, ``content_base64`` is still carried for the paid tiers and read by nothing."""
    client, classifier, _ = build_app()
    response = client.post(
        "/api/v1/classify",
        json={"text": "INCOME TAX DEPARTMENT", "content_base64": b64(DOCX)},
    )
    assert response.status_code == 200
    view = classifier.views[-1]
    assert view.raw.get("provider") == "plain-text"
    assert "INCOME TAX DEPARTMENT" in view.text()


def test_the_missing_payload_error_names_the_new_route_in():
    client, _, _ = build_app()
    response = client.post("/api/v1/classify", json={"doc_id": "x"})
    assert response.status_code == 400
    assert "content_base64 together with ingest" in response.json()["detail"]


def test_ingest_wins_over_a_layout_sent_in_the_same_request():
    """Setting the field is explicit; it is how a caller says which payload they mean."""
    client, classifier, _ = build_app()
    response = client.post(
        "/api/v1/classify",
        json={
            "layout": {"blocks": [{"text": "IGNORED", "zone": "body"}]},
            "content_base64": b64(DOCX),
            "ingest": {},
        },
    )
    assert response.status_code == 200
    assert "IGNORED" not in classifier.views[-1].text()
    assert "ACME HOLDINGS INC." in classifier.views[-1].text()
