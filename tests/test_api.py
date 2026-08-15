"""API tests: offline, pure, and stubbed at the engine boundary.

No network, no database, no model download. The engine modules (``dce.registry``,
``dce.classify``, ``dce.extract``) are substituted with recording stubs installed on
``app.state``, which is the same place :mod:`dce.api.routes` caches the real ports — so the
routes under test are exercised exactly as they run in production, minus the engines.

The load-bearing test here is :func:`test_process_abstention_does_not_extract`: an abstaining
classification must not reach the extractor. Extracting against a guessed doctype produces
confidently wrong fields, and in a KYC system that is worse than no fields at all.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dce.api.app import create_app
from dce.api.routes import ClassifierPort, ExtractorPort, RegistryPort
from dce.config import Settings
from dce.models import (
    UNKNOWN,
    Anchor,
    Category,
    Classification,
    DocTypeSpec,
    Evidence,
    ExtractedField,
    ExtractionResult,
    FieldSpec,
    LayoutView,
    Zone,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
class StubRegistry:
    """A doctype registry with the accessor shape :class:`RegistryPort` adapts."""

    def __init__(self, specs: list[DocTypeSpec]) -> None:
        self._specs = {spec.doctype_id: spec for spec in specs}

    def all(self) -> list[DocTypeSpec]:
        return list(self._specs.values())

    def get(self, doctype_id: str) -> DocTypeSpec | None:
        return self._specs.get(doctype_id)


class StubClassifier:
    """Returns a canned classification and records the views it was handed."""

    def __init__(self, result: Classification) -> None:
        self.result = result
        self.views: list[LayoutView] = []

    def __call__(self, view: LayoutView, **kwargs: Any) -> Classification:
        self.views.append(view)
        return self.result.model_copy(deep=True)


class StubExtractor:
    """Returns a canned extraction and records every call — including the ones that
    should never happen."""

    def __init__(self, result: ExtractionResult | None = None) -> None:
        self.result = result or ExtractionResult()
        self.calls: list[tuple[LayoutView, DocTypeSpec]] = []

    def __call__(self, view: LayoutView, spec: DocTypeSpec, **kwargs: Any) -> ExtractionResult:
        self.calls.append((view, spec))
        return self.result.model_copy(deep=True)


SIN_SPEC = DocTypeSpec(
    doctype_id="ca_sin_confirmation",
    label="Confirmation of SIN",
    country="CA",
    category=Category.identity,
    issuing_authority="Service Canada",
    officially_valid=True,
    anchors=[Anchor(text="social insurance number", decisive=True, zone=Zone.title)],
    fields=[
        FieldSpec(
            name="sin_number",
            attribute_key="id.sin",
            type="id",
            required=True,
            pii=True,
            validator="sin_luhn",
            locators=["kv", "label", "regex"],
        ),
        FieldSpec(name="holder_name", attribute_key="identity.full_name", type="name", pii=True),
    ],
)

W9_SPEC = DocTypeSpec(
    doctype_id="us_w9",
    label="IRS Form W-9",
    country="US",
    category=Category.tax,
    fields=[FieldSpec(name="ein", attribute_key="id.ein", type="id")],
)

ACCEPTED = Classification(
    doctype_id="ca_sin_confirmation",
    label="Confirmation of SIN",
    country="CA",
    confidence=0.94,
    margin=0.61,
    coverage=0.55,
    abstained=False,
    evidence=[Evidence(tier="anchor", detail="social insurance number (title)", weight=3.0)],
    runners_up=[("us_w2", 0.33)],
)

ABSTAINED = Classification(
    doctype_id=UNKNOWN,
    confidence=0.41,
    margin=0.06,
    coverage=0.10,
    abstained=True,
    reason="probability 0.41 < 0.65 and margin 0.06 < 0.25",
    runners_up=[("ca_sin_confirmation", 0.41), ("us_w2", 0.35)],
)

EXTRACTED = ExtractionResult(
    doctype_id="ca_sin_confirmation",
    fields=[
        ExtractedField(
            name="sin_number",
            attribute_key="id.sin",
            value="12-3456789",
            normalized="12-3456789",
            confidence=0.97,
            verification="checksum_verified",
            locator="kv",
            page=1,
            pii=True,
        ),
        ExtractedField(name="holder_name", value="ANNA ERIKSSON", confidence=0.9, locator="table"),
    ],
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def build_app(
    *,
    api_key: str = "",
    specs: list[DocTypeSpec] | None = None,
    classification: Classification | None = None,
    extraction: ExtractionResult | None = None,
) -> tuple[TestClient, StubClassifier, StubExtractor]:
    """An app wired to stub engines.

    ``TestClient`` is used without its context manager on purpose: entering it runs the
    lifespan, which would replace these stubs with the real (absent) engines.
    """
    app = create_app(Settings(api_key=api_key, allow_preclassification_egress=False))
    classifier = StubClassifier(classification or ACCEPTED)
    extractor = StubExtractor(extraction or EXTRACTED)
    app.state.registry = RegistryPort(StubRegistry(specs if specs is not None else [SIN_SPEC]))
    app.state.classifier = ClassifierPort(classifier)
    app.state.extractor = ExtractorPort(extractor)
    return TestClient(app), classifier, extractor


@pytest.fixture
def client() -> TestClient:
    return build_app()[0]


AZURE_ANALYZE_RESULT: dict[str, Any] = {
    "status": "succeeded",
    "analyzeResult": {
        "apiVersion": "2024-11-30",
        "modelId": "prebuilt-layout",
        "content": (
            "DEPARTMENT OF THE TREASURY\n"
            "Employer identification number\n"
            "Name\nANNA ERIKSSON\n"
            "Page 1 of 1"
        ),
        "pages": [
            {
                "pageNumber": 1,
                "width": 8.5,
                "height": 11.0,
                "unit": "inch",
                "angle": 0.0,
                "selectionMarks": [
                    {"state": "selected", "polygon": [1, 1, 1.2, 1, 1.2, 1.2, 1, 1.2]}
                ],
            }
        ],
        "paragraphs": [
            {
                "role": "title",
                "content": "DEPARTMENT OF THE TREASURY",
                "spans": [{"offset": 0, "length": 21}],
                "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 3, 0, 3, 0.3, 0, 0.3]}],
            },
            {
                "role": "sectionHeading",
                "content": "Employer identification number",
                "spans": [{"offset": 22, "length": 24}],
                "boundingRegions": [{"pageNumber": 1, "polygon": [0, 1, 3, 1, 3, 1.3, 0, 1.3]}],
            },
            {
                "content": "Name",
                "spans": [{"offset": 47, "length": 4}],
                "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 2.3, 0, 2.3]}],
            },
            {
                "content": "ANNA ERIKSSON",
                "spans": [{"offset": 52, "length": 12}],
                "boundingRegions": [{"pageNumber": 1, "polygon": [1, 2, 3, 2, 3, 2.3, 1, 2.3]}],
            },
            {
                "role": "pageFooter",
                "content": "Page 1 of 1",
                "spans": [{"offset": 65, "length": 11}],
                "boundingRegions": [{"pageNumber": 1, "polygon": [0, 10, 3, 10, 3, 10.3, 0, 10.3]}],
            },
        ],
        "tables": [
            {
                "rowCount": 1,
                "columnCount": 2,
                "spans": [{"offset": 47, "length": 17}],
                "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 3, 2, 3, 2.3, 0, 2.3]}],
                "cells": [
                    {
                        "rowIndex": 0,
                        "columnIndex": 0,
                        "kind": "columnHeader",
                        "content": "Name",
                        "spans": [{"offset": 47, "length": 4}],
                        "boundingRegions": [
                            {"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 2.3, 0, 2.3]}
                        ],
                    },
                    {
                        "rowIndex": 0,
                        "columnIndex": 1,
                        "content": "ANNA ERIKSSON",
                        "spans": [{"offset": 52, "length": 12}],
                        "boundingRegions": [
                            {"pageNumber": 1, "polygon": [1, 2, 3, 2, 3, 2.3, 1, 2.3]}
                        ],
                    },
                ],
            }
        ],
        "keyValuePairs": [
            {
                "key": {
                    "content": "EIN",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [0, 3, 1, 3, 1, 3.2, 0, 3.2]}],
                },
                "value": {
                    "content": "12-3456789",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [1, 3, 3, 3, 3, 3.2, 1, 3.2]}],
                },
                "confidence": 0.95,
            }
        ],
        "languages": [{"locale": "en", "confidence": 0.99}],
    },
}


# ---------------------------------------------------------------------------
# System routes
# ---------------------------------------------------------------------------
def test_health_is_liveness_only(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    # Every response carries a timing.
    assert int(response.headers["X-Elapsed-Ms"]) >= 0


def test_readyz_reports_registry_bert_and_the_egress_invariant() -> None:
    client, _, _ = build_app()
    body = client.get("/readyz").json()

    assert body["ready"] is True
    assert body["registry"] == {"loaded": True, "doctypes": 1, "countries": ["CA"]}
    assert body["bert"]["enabled"] is False and body["bert"]["loaded"] is False
    assert body["egress"] == {
        "preclassification_allowed": False,
        "enforced": True,
        "note": body["egress"]["note"],
        # The guard being armed is not the same question as "does anything leave before the
        # cascade runs" — ingestion finishes before the classification scope is entered, so a
        # remote OCR provider would sit in this blind spot. It is reported here explicitly.
        "preclassification_ocr": False,
        "preclassification_ocr_endpoint": "",
        # No remote provider, so the question of where one sits does not arise.
        "preclassification_ocr_trust_boundary": "",
    }
    # The default deployment recognises nothing and therefore transmits nothing.
    assert body["ocr"] == {
        "provider": "none",
        "enabled": False,
        "network": False,
        "endpoint_host": "",
        # Reported on every deployment, including this one: a field that appears only once it
        # is interesting can only be found by somebody who already knew to look for it. The
        # code default is the cautious value, and nothing here declared otherwise.
        "trust_boundary": "external",
        "trust_boundary_declared": False,
        "trust_boundary_attribution": "",
        # How far a PDF's own text layer is believed here, reported for the same reason:
        # it decides whether a document is recognised at all, before any provider is chosen.
        "text_layer_policy": "verify",
        "text_layer_attribution": body["ocr"]["text_layer_attribution"],
        "problem": "",
        "summary": body["ocr"]["summary"],
        "local_ocr_enabled": False,
        "local_ocr_engine": body["ocr"]["local_ocr_engine"],
        "providers": body["ocr"]["providers"],
        "configured_providers": [],
        "service_endpoint_hosts": [],
    }
    # Every provider this build supports is listed even when switched off, and *especially*
    # when switched off. A console that could only see the providers a deployment happens to
    # have enabled could never say "this deployment does not send documents to Azure Read" —
    # it could only fail to mention it, and silence is not a disclosure.
    listed = {p["name"]: p for p in body["ocr"]["providers"]}
    assert set(listed) == {"rapidocr", "tesseract", "azure_read", "azure_layout"}
    assert not any(p["available"] for p in listed.values())
    # Nothing is unavailable without saying why: the reason is what tells an operator whether
    # to go turn a setting on or to stop looking.
    assert all(p["reason"] for p in listed.values())
    assert listed["azure_read"]["network"] is True
    assert listed["azure_layout"]["network"] is True
    assert listed["rapidocr"]["network"] is False
    # The fact that decides which anchors could fire at all, reported rather than left for a
    # console to infer from a vendor's name.
    assert listed["azure_read"]["structure"] == "lines"
    assert listed["azure_layout"]["structure"] == "roles"
    # No endpoint is named, because none is configured — a host here would be a claim that
    # documents go somewhere.
    assert all(p["endpoint"] == "" for p in listed.values())


def test_readyz_is_503_when_the_egress_invariant_is_off() -> None:
    """Allowing pre-classification egress takes the service out of rotation, loudly."""
    app = create_app(Settings(allow_preclassification_egress=True))
    app.state.registry = RegistryPort(StubRegistry([SIN_SPEC]))
    response = TestClient(app).get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["egress"]["enforced"] is False
    assert "egress" in body["degraded"]


def test_a_real_boot_reports_honest_readiness() -> None:
    """Exercises the lifespan against the engines that are actually installed.

    Deliberately does not assert *which* engines exist — this repo's registry, classifier and
    extractor land independently. What must hold either way is that the invariant is enforced
    and that readiness agrees with what actually loaded, rather than a green light over a
    process that can only abstain.
    """
    with TestClient(create_app(Settings())) as booted:
        body = booted.get("/readyz").json()

    assert body["egress"]["enforced"] is True
    assert body["ready"] is (body["registry"]["doctypes"] > 0)
    if not body["ready"]:
        assert "registry" in body["degraded"]


def test_metrics_exposes_the_abstention_signal() -> None:
    client, _, _ = build_app(classification=ABSTAINED)
    client.post("/api/v1/classify", json={"text": "something unrecognisable"})
    body = client.get("/metrics").text

    assert 'dce_classifications_total{outcome="abstained"}' in body
    assert "dce_classification_confidence" in body


# ---------------------------------------------------------------------------
# /classify
# ---------------------------------------------------------------------------
def test_classify_plain_text(client: TestClient) -> None:
    response = client.post(
        "/api/v1/classify",
        json={
            "doc_id": "doc-1",
            "text": "DEPARTMENT OF THE TREASURY\nEmployer identification number",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["doctype_id"] == "ca_sin_confirmation"
    assert body["abstained"] is False
    assert body["evidence"][0]["tier"] == "anchor"
    assert body["ms"] >= 0


def test_classify_plain_text_is_all_body_zone() -> None:
    """The degraded path stays honest: nothing is promoted to title on a guess."""
    client, classifier, _ = build_app()
    client.post("/api/v1/classify", json={"text": "DEPARTMENT OF THE TREASURY\nName: ANNA"})

    view = classifier.views[0]
    assert [b.text for b in view.blocks] == ["DEPARTMENT OF THE TREASURY", "Name: ANNA"]
    assert {b.zone for b in view.blocks} == {Zone.body}


def test_classify_adapts_an_azure_layout_payload() -> None:
    client, classifier, _ = build_app()
    response = client.post(
        "/api/v1/classify", json={"azure_analyze_result": AZURE_ANALYZE_RESULT}
    )
    assert response.status_code == 200

    view = classifier.views[0]
    zones = {block.text: block.zone for block in view.blocks}
    assert zones["DEPARTMENT OF THE TREASURY"] is Zone.title
    assert zones["Employer identification number"] is Zone.heading
    assert zones["Page 1 of 1"] is Zone.furniture
    # Paragraphs whose spans fall inside a table are re-zoned rather than emitted twice.
    assert zones["Name"] is Zone.table and zones["ANNA ERIKSSON"] is Zone.table
    assert len(view.blocks) == 5

    assert view.pages[0].unit == "inch" and view.pages[0].width == 8.5
    assert [(kv.key, kv.value) for kv in view.key_values] == [("EIN", "12-3456789")]
    assert len(view.marks) == 1 and view.marks[0].selected
    assert view.languages == ["en"]
    assert view.tables[0].cell_at(0, 1).text == "ANNA ERIKSSON"


def test_classify_adapts_a_des_ocr_page_envelope() -> None:
    """DES hands back ``{"page": …, "raw": …}``; the row's page number wins."""
    client, classifier, _ = build_app()
    azure = AZURE_ANALYZE_RESULT["analyzeResult"]
    page_payload = dict(azure["pages"][0])
    page_payload.pop("pageNumber")
    payload = {
        "page": {"page_number": 4, "width": 8.5, "height": 11.0, "unit": "inch"},
        "raw": {**page_payload, "paragraphs": azure["paragraphs"], "tables": azure["tables"]},
    }

    response = client.post("/api/v1/classify", json={"des_ocr": payload})
    assert response.status_code == 200

    view = classifier.views[0]
    assert [p.page for p in view.pages] == [4]
    assert {b.page for b in view.blocks} == {4}
    assert any(b.zone is Zone.title for b in view.blocks)


def test_classify_without_a_document_is_400(client: TestClient) -> None:
    response = client.post("/api/v1/classify", json={"doc_id": "doc-1"})
    assert response.status_code == 400
    assert "supply one of" in response.json()["detail"]


# ---------------------------------------------------------------------------
# /process
# ---------------------------------------------------------------------------
def test_process_abstention_does_not_extract() -> None:
    """The invariant of the common path: no doctype, no extraction, no model."""
    client, _, extractor = build_app(classification=ABSTAINED)

    response = client.post("/api/v1/process", json={"text": "a page of unrecognisable text"})

    assert response.status_code == 200
    body = response.json()
    assert body["classification"]["abstained"] is True
    assert body["classification"]["doctype_id"] == UNKNOWN
    assert body["extraction"] is None
    assert body["needs_review"] is True
    assert body["detail"] == ABSTAINED.reason
    assert extractor.calls == []


def test_process_extracts_after_an_accepted_classification() -> None:
    client, _, extractor = build_app()

    body = client.post("/api/v1/process", json={"text": "DEPARTMENT OF THE TREASURY"}).json()

    assert body["needs_review"] is False
    assert body["extraction"]["doctype_id"] == "ca_sin_confirmation"
    assert body["extraction"]["fields"][0]["verification"] == "checksum_verified"
    assert body["extraction"]["schema_version"].startswith("reg-")
    assert body["timings"]["total_ms"] >= 0
    assert len(extractor.calls) == 1
    assert extractor.calls[0][1].doctype_id == "ca_sin_confirmation"


def test_process_flags_review_when_a_required_field_is_missing() -> None:
    incomplete = ExtractionResult(doctype_id="ca_sin_confirmation", missing_required=["sin_number"])
    client, _, _ = build_app(extraction=incomplete)

    body = client.post("/api/v1/process", json={"text": "DEPARTMENT OF THE TREASURY"}).json()

    assert body["needs_review"] is True
    assert body["detail"] == "missing required fields"


def test_process_does_not_extract_for_a_doctype_outside_the_registry() -> None:
    """A classifier that names a doctype the registry does not have is a config drift bug,
    not something to paper over with a guessed spec."""
    client, _, extractor = build_app(specs=[W9_SPEC])

    body = client.post("/api/v1/process", json={"text": "DEPARTMENT OF THE TREASURY"}).json()

    assert body["needs_review"] is True
    assert "not in the registry" in body["detail"]
    assert body["extraction"] is None
    assert extractor.calls == []


# ---------------------------------------------------------------------------
# /extract
# ---------------------------------------------------------------------------
def test_extract_with_an_unknown_doctype_is_404(client: TestClient) -> None:
    response = client.post(
        "/api/v1/extract", json={"text": "whatever", "doctype_id": "xx_not_a_doctype"}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "unknown doctype: xx_not_a_doctype"


def test_extract_with_a_pinned_doctype_skips_classification() -> None:
    client, classifier, extractor = build_app()

    body = client.post(
        "/api/v1/extract",
        json={"text": "SOCIAL INSURANCE NUMBER", "doctype_id": "ca_sin_confirmation"},
    ).json()

    assert body["doctype_id"] == "ca_sin_confirmation"
    assert [f["name"] for f in body["fields"]] == ["sin_number", "holder_name"]
    assert classifier.views == []
    assert len(extractor.calls) == 1


def test_extract_without_a_doctype_classifies_and_abstains_to_review() -> None:
    client, _, extractor = build_app(classification=ABSTAINED)

    body = client.post("/api/v1/extract", json={"text": "unrecognisable"}).json()

    assert body["doctype_id"] == UNKNOWN
    assert body["needs_review"] is True
    assert body["fields"] == []
    assert extractor.calls == []


# ---------------------------------------------------------------------------
# Registry + schemas
# ---------------------------------------------------------------------------
def test_doctypes_lists_the_registry(client: TestClient) -> None:
    body = client.get("/api/v1/doctypes").json()

    assert body["count"] == 1
    entry = body["doctypes"][0]
    assert entry["doctype_id"] == "ca_sin_confirmation"
    assert entry["country"] == "CA"
    assert entry["category"] == "identity"
    assert entry["officially_valid"] is True
    assert entry["fields"] == ["sin_number", "holder_name"]


def test_doctypes_filters_by_country() -> None:
    client, _, _ = build_app(specs=[SIN_SPEC, W9_SPEC])

    assert client.get("/api/v1/doctypes?country=US").json()["count"] == 1
    assert client.get("/api/v1/doctypes?country=CA").json()["count"] == 1
    assert client.get("/api/v1/doctypes?category=tax").json()["doctypes"][0]["doctype_id"] == (
        "us_w9"
    )


def test_doctype_detail_carries_anchors_and_field_locators(client: TestClient) -> None:
    body = client.get("/api/v1/doctypes/ca_sin_confirmation").json()

    assert body["anchors"][0]["decisive"] is True
    assert body["fields"][0]["validator"] == "sin_luhn"
    assert client.get("/api/v1/doctypes/nope").status_code == 404


def test_schema_falls_back_to_the_registry_spec(client: TestClient) -> None:
    body = client.get("/api/v1/schemas/ca_sin_confirmation").json()

    assert body["source"] == "registry"
    assert body["active"] is True
    assert body["schema_version"].startswith("reg-")
    assert [f["name"] for f in body["fields"]] == ["sin_number", "holder_name"]
    assert client.get("/api/v1/schemas/nope").status_code == 404


def test_induced_schemas_are_never_active(client: TestClient) -> None:
    """Induction drafts; a human activates. A schema that activated itself would silently
    change what the service extracts."""
    sample = {"azure_analyze_result": AZURE_ANALYZE_RESULT}
    response = client.post(
        "/api/v1/schemas/induce",
        json={"doctype_id": "us_w9_draft", "samples": [sample, sample], "min_support": 0.5},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["active"] is False
    assert body["source"] == "induced"
    assert body["sample_count"] == 2
    names = [f["name"] for f in body["fields"]]
    assert "ein" in names  # from the key-value pair
    assert "name" in names  # from the table's column header


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_api_key_gate() -> None:
    client, _, _ = build_app(api_key="s3cret")

    assert client.get("/api/v1/doctypes").status_code == 401
    assert client.get("/api/v1/doctypes", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/v1/doctypes", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_probes_and_scrapers_bypass_the_api_key() -> None:
    client, _, _ = build_app(api_key="s3cret")

    assert client.get("/health").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/metrics").status_code == 200


# ---------------------------------------------------------------------------
# The invariant, statically
# ---------------------------------------------------------------------------
_HTTP_CLIENT_IMPORT = re.compile(
    r"^\s*(?:import|from)\s+(httpx|requests|aiohttp|urllib3|openai|anthropic|google\.cloud"
    r"|azure|boto3|socket|http\.client)\b",
    re.MULTILINE,
)

#: Everything that runs before a doctype is known. ``dce/extract`` and ``dce/api`` are excluded
#: on purpose: they only ever run after classification has accepted a doctype.
_PRECLASSIFICATION_PATHS = ("dce/models.py", "dce/config.py", "dce/adapters.py",
                            "dce/observability.py", "dce/registry", "dce/classify")


def test_no_http_client_in_the_classification_path() -> None:
    """The invariant, enforced against the source rather than trusted.

    Other business units send this service documents nobody has classified. If any module on
    the pre-classification path could open a socket, the guarantee that their bytes stay in
    this process would rest on nobody ever calling it — which is not a guarantee.
    """
    offenders: list[str] = []
    for entry in _PRECLASSIFICATION_PATHS:
        path = REPO_ROOT / entry
        files = sorted(path.rglob("*.py")) if path.is_dir() else ([path] if path.is_file() else [])
        for file in files:
            match = _HTTP_CLIENT_IMPORT.search(file.read_text(encoding="utf-8"))
            if match:
                offenders.append(f"{file.relative_to(REPO_ROOT)}: {match.group(0).strip()}")
    assert offenders == [], f"network client on the pre-classification path: {offenders}"
