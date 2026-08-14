"""T2/T3 tests — entirely offline. No socket is opened by anything in this file.

Every request is served by an ``httpx.MockTransport``, installed through the one seam the
tiers expose (:func:`dce.extract.azure_specialist._new_client`), so the analyze/poll protocol,
the field mapping and the failure paths are all exercised against a fake Azure that lives in
this process. The tier switches are tested from the other direction too: with T2/T3 off, the
call must not merely avoid Azure, it must avoid the network — which is asserted with the
service's own :func:`dce.egress.socket_tripwire`.

The analyze results below are shaped like the real ones (``documents[0].fields`` with
``content``/``value*``/``confidence``/``boundingRegions``), because the mapping is the part
that breaks when Azure's payload is imagined rather than copied. No real identifiers appear:
the document number, dates and names are synthetic.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # keeps the suite runnable without an installed package
    sys.path.insert(0, str(_ROOT))

from dce.config import Settings  # noqa: E402
from dce.egress import socket_tripwire  # noqa: E402
from dce.extract import azure_specialist  # noqa: E402
from dce.extract.azure_specialist import (  # noqa: E402
    SPECIALIST_MODELS,
    AzureAnalyzeError,
    UnclassifiedDocumentError,
    extract_with_specialist,
    specialist_for,
)
from dce.extract.query_fields import (  # noqa: E402
    AZURE_MAX_QUERY_FIELDS,
    cap_query_fields,
    extract_query_fields,
)
from dce.models import UNKNOWN  # noqa: E402

ENDPOINT = "https://kyc-di.cognitiveservices.azure.com"
OPERATION_URL = f"{ENDPOINT}/documentintelligence/documentModels/x/analyzeResults/42"
PDF = b"%PDF-1.7 fake bytes"


def stub_settings(**overrides: Any) -> SimpleNamespace:
    """A settings object carrying the T2/T3 names.

    Deliberately **not** :class:`dce.config.Settings`: the flags are added by a separate
    change, and until they land ``Settings(t2_enabled=True)`` silently drops the field
    (``extra="ignore"``). Both tiers read every setting through ``getattr`` with a default for
    exactly that reason, and :func:`test_the_tier_is_inert_against_the_real_settings_object`
    pins the consequence.
    """
    base: dict[str, Any] = {
        "azure_di_endpoint": ENDPOINT,
        "azure_di_key": "not-a-real-key",
        "azure_di_api_version": "2024-11-30",
        "t2_enabled": True,
        "t3_enabled": True,
        "t3_max_query_fields": AZURE_MAX_QUERY_FIELDS,
        "allow_preclassification_egress": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def quad(x0: float, y0: float, x1: float, y1: float) -> list[float]:
    """A clockwise-from-top-left quad, the Azure convention."""
    return [x0, y0, x1, y0, x1, y1, x0, y1]


def azure_field(
    value_key: str,
    value: Any,
    content: str,
    *,
    confidence: float = 0.95,
    page: int = 1,
    box: list[float] | None = None,
    field_type: str = "string",
) -> dict[str, Any]:
    """One field node in Azure's ``documents[0].fields`` shape."""
    node: dict[str, Any] = {
        "type": field_type,
        value_key: value,
        "content": content,
        "confidence": confidence,
        "spans": [{"offset": 0, "length": len(content)}],
    }
    if box is not None:
        node["boundingRegions"] = [{"pageNumber": page, "polygon": box}]
    return node


DOB_BOX = quad(1.05, 2.40, 3.10, 2.72)

ID_DOCUMENT_RESULT: dict[str, Any] = {
    "apiVersion": "2024-11-30",
    "modelId": "prebuilt-idDocument",
    "stringIndexType": "textElements",
    "content": "PASSPORT ...",
    "pages": [{"pageNumber": 1, "width": 8.5, "height": 11.0, "unit": "inch"}],
    "documents": [
        {
            "docType": "idDocument.passport",
            "boundingRegions": [{"pageNumber": 1, "polygon": quad(0, 0, 8.5, 11.0)}],
            "confidence": 0.94,
            "fields": {
                "FirstName": azure_field(
                    "valueString", "ANNA MARIA", "ANNA MARIA",
                    confidence=0.962, box=quad(1.05, 1.80, 3.40, 2.10),
                ),
                "LastName": azure_field(
                    "valueString", "ERIKSSON", "ERIKSSON",
                    confidence=0.971, box=quad(1.05, 2.10, 3.00, 2.40),
                ),
                "DocumentNumber": azure_field(
                    "valueString", "X1234567", "X1234567",
                    confidence=0.988, box=quad(5.10, 1.80, 7.20, 2.10),
                ),
                "DateOfBirth": azure_field(
                    "valueDate", "1974-08-12", "12 AUG 1974",
                    confidence=0.913, box=DOB_BOX, field_type="date",
                ),
                # Present but empty: Azure looked and found nothing. Must not be reported.
                "Sex": {"type": "string", "content": "", "confidence": 0.0},
                "MachineReadableZone": {
                    "type": "object",
                    "content": "P<UTOERIKSSON<<ANNA<MARIA<<<",
                    "confidence": 0.90,
                    "valueObject": {
                        "DocumentNumber": azure_field(
                            "valueString", "X1234567", "X1234567",
                            confidence=0.997, box=quad(0.9, 9.4, 4.0, 9.7),
                        ),
                    },
                },
            },
        }
    ],
}

BANK_STATEMENT_RESULT: dict[str, Any] = {
    "modelId": "prebuilt-bankStatement.us",
    "documents": [
        {
            "docType": "bankStatement.us",
            "fields": {
                "AccountHolderName": azure_field(
                    "valueString", "A M ERIKSSON", "A M ERIKSSON", box=quad(1, 1, 3, 1.3)
                ),
                "Accounts": {
                    "type": "array",
                    "valueArray": [
                        {
                            "type": "object",
                            "valueObject": {
                                "AccountNumber": azure_field(
                                    "valueString", "0001234567", "0001234567",
                                    box=quad(1, 2, 3, 2.3),
                                ),
                                "EndingBalance": {
                                    "type": "currency",
                                    "valueCurrency": {
                                        "amount": 1234.56,
                                        "currencyCode": "USD",
                                        "currencySymbol": "$",
                                    },
                                    "content": "$1,234.56",
                                    "confidence": 0.88,
                                    "boundingRegions": [
                                        {"pageNumber": 2, "polygon": quad(5, 2, 6.5, 2.3)}
                                    ],
                                },
                            },
                        }
                    ],
                },
            },
        }
    ],
}


@dataclass
class FakeAzure:
    """A Document Intelligence endpoint that lives in this process.

    Records every request so the test can assert the *protocol* (URL, api-version, add-on
    features, key header, raw body) and not just the mapped output.
    """

    analyze_result: dict[str, Any] = field(default_factory=dict)
    #: One entry per poll; the last one repeats if polled again.
    poll_statuses: list[str] = field(default_factory=lambda: ["succeeded"])
    analyze_status_code: int = 202
    analyze_error: dict[str, Any] | None = None
    job_error: dict[str, Any] | None = None
    operation_location: str | None = OPERATION_URL
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST":
            return self._analyze()
        return self._poll()

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    @property
    def polls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET"]

    def _analyze(self) -> httpx.Response:
        if self.analyze_status_code >= 400:
            return httpx.Response(
                self.analyze_status_code, json={"error": self.analyze_error or {}}
            )
        headers = (
            {"Operation-Location": self.operation_location} if self.operation_location else {}
        )
        return httpx.Response(self.analyze_status_code, headers=headers, json={})

    def _poll(self) -> httpx.Response:
        index = min(len(self.polls) - 1, len(self.poll_statuses) - 1)
        status = self.poll_statuses[index]
        body: dict[str, Any] = {"status": status, "createdDateTime": "2026-08-06T00:00:00Z"}
        if status == "succeeded":
            body["analyzeResult"] = self.analyze_result
        if status in {"failed", "canceled"}:
            body["error"] = self.job_error or {}
        return httpx.Response(200, json=body)


def install(monkeypatch: pytest.MonkeyPatch, fake: FakeAzure) -> FakeAzure:
    """Point the tiers at ``fake`` and make polling instantaneous."""
    monkeypatch.setattr(azure_specialist, "_POLL_INTERVAL_SECONDS", 0.0)

    def new_client(*, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(fake))

    monkeypatch.setattr(azure_specialist, "_new_client", new_client)
    return fake


def run(coro: Any) -> Any:
    """Drive a coroutine. The suite has no async plugin, and does not need one."""
    return asyncio.run(coro)


def run_under_tripwire(make_coro: Any) -> tuple[Any, list[str]]:
    """Run a coroutine with every socket constructor blocked, and report the attempts.

    The event loop is built **before** the tripwire goes up on purpose: asyncio's loop
    constructor opens a socketpair for its own self-pipe, so building it underneath would trip
    on asyncio's plumbing rather than on anything the tier did. Everything the tier itself does
    happens inside the block.

    Args:
        make_coro: Zero-argument callable returning the coroutine to run.

    Returns:
        ``(result, attempts)`` — ``attempts`` is empty when nothing tried to reach the network.
    """
    loop = asyncio.new_event_loop()
    try:
        with socket_tripwire() as attempts:
            result = loop.run_until_complete(make_coro())
        return result, attempts
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# T2 — the specialist map
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("doctype_id", "model"),
    [
        ("us_passport", "prebuilt-idDocument"),
        ("ca_passport", "prebuilt-idDocument"),
        ("mx_passport", "prebuilt-idDocument"),
        ("us_drivers_license", "prebuilt-idDocument"),
        ("us_state_id", "prebuilt-idDocument"),
        ("ca_drivers_license", "prebuilt-idDocument"),
        ("ca_provincial_photo_id", "prebuilt-idDocument"),
        ("us_w2", "prebuilt-tax.us.w2"),
        ("us_1099", "prebuilt-tax.us.1099"),
        ("us_1040", "prebuilt-tax.us.1040"),
        ("us_bank_statement", "prebuilt-bankStatement.us"),
        ("ca_bank_statement", "prebuilt-bankStatement.us"),
        ("us_paystub", "prebuilt-payStub.us"),
    ],
)
def test_specialist_mapping_resolves(doctype_id: str, model: str) -> None:
    assert run(specialist_for(doctype_id)) == model


@pytest.mark.parametrize(
    "doctype_id",
    ["us_green_card", "us_passport_card", "mx_ine", "us_utility_bill", "ca_pr_card", UNKNOWN, ""],
)
def test_doctypes_without_a_specialist_resolve_to_none(doctype_id: str) -> None:
    """``None`` means "stay on T1" — the common, correct answer for 116 of 129 doctypes.

    ``mx_ine`` and ``ca_pr_card`` are the interesting entries: they are photo IDs, and mapping
    them onto ``prebuilt-idDocument`` (which is trained on passports and North-American
    licences) would return confident, wrong values. Unmapped is the deliberate choice.
    """
    assert run(specialist_for(doctype_id)) is None


def test_the_map_only_names_real_azure_prebuilt_models() -> None:
    for doctype_id, model in SPECIALIST_MODELS.items():
        assert model.startswith("prebuilt-"), doctype_id


# ---------------------------------------------------------------------------
# T2 — mapping a realistic analyzeResult
# ---------------------------------------------------------------------------
def test_extract_with_specialist_maps_fields_with_page_and_bbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(monkeypatch, FakeAzure(analyze_result=ID_DOCUMENT_RESULT))

    fields = run(extract_with_specialist(PDF, "us_passport", settings=stub_settings()))

    by_name = {f.name: f for f in fields}
    assert set(by_name) == {
        "first_name",
        "last_name",
        "document_number",
        "date_of_birth",
        "machine_readable_zone.document_number",
    }

    dob = by_name["date_of_birth"]
    #: value is what the page says; normalized is Azure's canonical reading of it.
    assert dob.value == "12 AUG 1974"
    assert dob.normalized == "1974-08-12"
    assert dob.confidence == pytest.approx(0.913)
    assert dob.page == 1
    assert dob.bbox == DOB_BOX
    assert dob.locator == "azure:prebuilt-idDocument"
    assert dob.pii is True
    assert fake.polls  # the job really was polled


def test_azure_confidence_is_never_promoted_to_checksum_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vendor confidence is not a proof, however high it is.

    ``DocumentNumber`` comes back at 0.988 and the MRZ copy at 0.997; neither is a check digit,
    so both stay at ``format_valid``. Only :mod:`dce.extract.validate` can promote a field, and
    that is what stops a merge from letting T2 overwrite a value T1 proved.
    """
    install(monkeypatch, FakeAzure(analyze_result=ID_DOCUMENT_RESULT))

    fields = run(extract_with_specialist(PDF, "us_passport", settings=stub_settings()))

    assert {f.verification for f in fields} == {"format_valid"}


def test_fields_azure_returned_empty_are_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeAzure(analyze_result=ID_DOCUMENT_RESULT))

    fields = run(extract_with_specialist(PDF, "us_passport", settings=stub_settings()))

    assert "sex" not in {f.name for f in fields}


def test_nested_objects_and_arrays_are_flattened(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bank-statement and pay-stub models return accounts and lines, not flat fields."""
    install(monkeypatch, FakeAzure(analyze_result=BANK_STATEMENT_RESULT))

    fields = run(extract_with_specialist(PDF, "us_bank_statement", settings=stub_settings()))

    by_name = {f.name: f for f in fields}
    assert by_name["accounts[0].account_number"].value == "0001234567"
    balance = by_name["accounts[0].ending_balance"]
    assert balance.value == "$1,234.56"          # as printed
    assert balance.normalized == "1234.56"       # plain decimal, per the field contract
    assert balance.page == 2
    assert by_name["account_holder_name"].locator == "azure:prebuilt-bankStatement.us"


def test_the_request_carries_the_document_the_model_and_the_api_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(monkeypatch, FakeAzure(analyze_result=ID_DOCUMENT_RESULT))

    run(extract_with_specialist(PDF, "us_w2", settings=stub_settings()))

    post = fake.posts[0]
    assert post.url.path == "/documentintelligence/documentModels/prebuilt-tax.us.w2:analyze"
    assert post.url.params["api-version"] == "2024-11-30"
    assert post.headers["Ocp-Apim-Subscription-Key"] == "not-a-real-key"
    assert post.headers["Content-Type"] == "application/octet-stream"
    assert post.content == PDF
    assert str(fake.polls[0].url) == OPERATION_URL


def test_a_202_then_running_then_succeeded_sequence_is_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(
        monkeypatch,
        FakeAzure(
            analyze_result=ID_DOCUMENT_RESULT,
            poll_statuses=["notStarted", "running", "running", "succeeded"],
        ),
    )

    fields = run(extract_with_specialist(PDF, "us_passport", settings=stub_settings()))

    assert len(fake.polls) == 4
    assert fields


# ---------------------------------------------------------------------------
# T2 — failure paths
# ---------------------------------------------------------------------------
def test_a_failed_job_surfaces_azures_own_error(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        FakeAzure(
            poll_statuses=["running", "failed"],
            job_error={
                "code": "InvalidImage",
                "message": "The input image is corrupted or in an unsupported format.",
            },
        ),
    )

    with pytest.raises(AzureAnalyzeError) as excinfo:
        run(extract_with_specialist(PDF, "us_passport", settings=stub_settings()))

    message = str(excinfo.value)
    assert "prebuilt-idDocument" in message
    assert "InvalidImage" in message
    assert "unsupported format" in message


def test_a_rejected_request_surfaces_the_status_and_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(
        monkeypatch,
        FakeAzure(
            analyze_status_code=401,
            analyze_error={"code": "PermissionDenied", "message": "Access denied."},
        ),
    )

    with pytest.raises(AzureAnalyzeError) as excinfo:
        run(extract_with_specialist(PDF, "us_passport", settings=stub_settings()))

    assert "401" in str(excinfo.value)
    assert "PermissionDenied" in str(excinfo.value)


def test_a_202_without_an_operation_location_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, FakeAzure(operation_location=None))

    with pytest.raises(AzureAnalyzeError, match="Operation-Location"):
        run(extract_with_specialist(PDF, "us_passport", settings=stub_settings()))


def test_a_job_that_never_settles_hits_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """An open connection is not an answer: the review queue gets an error instead."""
    install(monkeypatch, FakeAzure(poll_statuses=["running"]))
    monkeypatch.setattr(azure_specialist, "_POLL_DEADLINE_SECONDS", 0.0)

    with pytest.raises(AzureAnalyzeError, match="did not finish"):
        run(extract_with_specialist(PDF, "us_passport", settings=stub_settings()))


def test_empty_bytes_are_rejected_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakeAzure(analyze_result=ID_DOCUMENT_RESULT))

    with pytest.raises(ValueError, match="no document bytes"):
        run(extract_with_specialist(b"", "us_passport", settings=stub_settings()))

    assert fake.requests == []


# ---------------------------------------------------------------------------
# The egress rule: T2/T3 refuse an abstention, and are off by default
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("doctype_id", [UNKNOWN, "", "   ", "UNKNOWN"])
def test_t2_refuses_to_run_on_an_unclassified_document(
    monkeypatch: pytest.MonkeyPatch, doctype_id: str
) -> None:
    """The whole point of the tiering: bytes leave only after a doctype is accepted."""
    fake = install(monkeypatch, FakeAzure(analyze_result=ID_DOCUMENT_RESULT))

    with pytest.raises(UnclassifiedDocumentError) as excinfo:
        run(extract_with_specialist(PDF, doctype_id, settings=stub_settings()))

    assert "T2" in str(excinfo.value)
    assert fake.requests == []


def test_t3_refuses_to_run_on_an_unclassified_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(monkeypatch, FakeAzure())

    with pytest.raises(UnclassifiedDocumentError, match="T3"):
        run(
            extract_query_fields(
                PDF, ["landlord_name"], settings=stub_settings(), doctype_id=UNKNOWN
            )
        )

    assert fake.requests == []


def test_the_refusal_fires_even_when_the_tier_is_switched_off() -> None:
    """A caller reaching T2 with an abstention is a bug, not a preference.

    Returning ``[]`` because the tier happens to be off would hide it until the day someone
    enables T2 in production, which is the worst possible moment to find out.
    """
    with pytest.raises(UnclassifiedDocumentError):
        run(extract_with_specialist(PDF, UNKNOWN, settings=stub_settings(t2_enabled=False)))


def test_t2_disabled_returns_nothing_and_opens_no_socket() -> None:
    """Off by default means a deployment that wants zero egress gets it by doing nothing."""
    fields, attempts = run_under_tripwire(
        lambda: extract_with_specialist(
            PDF, "us_passport", settings=stub_settings(t2_enabled=False)
        )
    )

    assert fields == []
    assert attempts == []


def test_t3_disabled_returns_nothing_and_opens_no_socket() -> None:
    fields, attempts = run_under_tripwire(
        lambda: extract_query_fields(
            PDF,
            ["landlord_name"],
            settings=stub_settings(t3_enabled=False),
            doctype_id="us_lease_agreement",
        )
    )

    assert fields == []
    assert attempts == []


def test_an_unconfigured_endpoint_is_a_loud_no_op(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Enabled-but-unconfigured is a deployment mistake: degrade to T1, but say so."""
    fake = install(monkeypatch, FakeAzure(analyze_result=ID_DOCUMENT_RESULT))

    with caplog.at_level(logging.WARNING, logger="dce.extract.azure_specialist"):
        fields = run(
            extract_with_specialist(
                PDF, "us_passport", settings=stub_settings(azure_di_endpoint="")
            )
        )

    assert fields == []
    assert fake.requests == []
    assert "azure_di_endpoint" in caplog.text


def test_a_doctype_with_no_specialist_is_a_silent_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(monkeypatch, FakeAzure(analyze_result=ID_DOCUMENT_RESULT))

    assert run(extract_with_specialist(PDF, "mx_ine", settings=stub_settings())) == []
    assert fake.requests == []


def test_the_tier_is_inert_against_the_real_settings_object() -> None:
    """Until ``t2_enabled``/``t3_enabled`` land in ``Settings``, both tiers stay off.

    ``Settings`` ignores unknown fields, so a config that has not grown the flags yet reports
    nothing for them — and ``getattr(settings, "t2_enabled", False)`` must read that as *off*.
    A default of "on" here would silently start calling Azure on the next deploy.
    """
    settings = Settings(_env_file=None)

    t2, t2_attempts = run_under_tripwire(
        lambda: extract_with_specialist(PDF, "us_passport", settings=settings)
    )
    t3, t3_attempts = run_under_tripwire(
        lambda: extract_query_fields(PDF, ["x"], settings=settings, doctype_id="us_passport")
    )

    assert (t2, t3) == ([], [])
    assert t2_attempts == []
    assert t3_attempts == []


# ---------------------------------------------------------------------------
# T3 — query fields
# ---------------------------------------------------------------------------
def test_query_fields_caps_at_twenty_and_names_what_it_dropped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """25 names is a 400 from Azure that loses all 25. Truncate, and say which five went."""
    wanted = [f"field_{i:02d}" for i in range(25)]
    fake = install(monkeypatch, FakeAzure(analyze_result={"documents": [{"fields": {}}]}))

    with caplog.at_level(logging.WARNING, logger="dce.extract.query_fields"):
        run(
            extract_query_fields(
                PDF, wanted, settings=stub_settings(), doctype_id="us_lease_agreement"
            )
        )

    asked = fake.posts[0].url.params["queryFields"].split(",")
    assert asked == wanted[:20]
    assert fake.posts[0].url.params["features"] == "queryFields"
    assert fake.posts[0].url.path == "/documentintelligence/documentModels/prebuilt-layout:analyze"
    for dropped in wanted[20:]:
        assert dropped in caplog.text


def test_a_configured_cap_above_azures_limit_is_clamped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="dce.extract.query_fields"):
        kept, dropped = cap_query_fields([f"f{i}" for i in range(30)], limit=50)

    assert len(kept) == AZURE_MAX_QUERY_FIELDS
    assert len(dropped) == 10
    assert "20" in caplog.text


def test_cap_query_fields_cleans_and_deduplicates_in_priority_order() -> None:
    kept, dropped = cap_query_fields(["  landlord_name ", "", "landlord_name", "rent"], limit=20)

    assert kept == ["landlord_name", "rent"]
    assert dropped == []


def test_a_lower_configured_cap_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakeAzure(analyze_result={"documents": [{"fields": {}}]}))

    run(
        extract_query_fields(
            PDF,
            ["a", "b", "c"],
            settings=stub_settings(t3_max_query_fields=2),
            doctype_id="us_lease_agreement",
        )
    )

    assert fake.posts[0].url.params["queryFields"] == "a,b"


def test_query_field_values_come_back_under_the_name_that_was_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller merges on the name it sent, so that name is what is reported back.

    Azure echoes the key verbatim — including a PascalCase one — and snake-casing it silently
    would break the merge for exactly the fields T3 exists to rescue.
    """
    result = {
        "documents": [
            {
                "fields": {
                    "landlord_name": azure_field(
                        "valueString", "R. Sharma", "R. Sharma",
                        confidence=0.71, box=quad(1, 3, 4, 3.3),
                    ),
                    "MonthlyRent": azure_field(
                        "valueString", "18,000", "Rs. 18,000",
                        confidence=0.64, box=quad(1, 4, 4, 4.3),
                    ),
                }
            }
        ]
    }
    install(monkeypatch, FakeAzure(analyze_result=result))

    fields = run(
        extract_query_fields(
            PDF,
            ["landlord_name", "MonthlyRent", "notary_stamp"],
            settings=stub_settings(),
            doctype_id="us_lease_agreement",
        )
    )

    by_name = {f.name: f for f in fields}
    assert set(by_name) == {"landlord_name", "MonthlyRent"}   # notary_stamp went unanswered
    assert by_name["MonthlyRent"].value == "Rs. 18,000"
    assert by_name["landlord_name"].locator == "azure:queryFields"
    assert by_name["landlord_name"].verification == "format_valid"
    assert by_name["landlord_name"].confidence == pytest.approx(0.71)


def test_asking_for_nothing_calls_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakeAzure())

    assert (
        run(
            extract_query_fields(
                PDF, ["", "  "], settings=stub_settings(), doctype_id="us_lease_agreement"
            )
        )
        == []
    )
    assert fake.requests == []


def test_query_fields_tolerates_a_result_with_no_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A layout that matched nothing is an empty answer, not an exception."""
    install(monkeypatch, FakeAzure(analyze_result={"pages": [{"pageNumber": 1}]}))

    fields = run(
        extract_query_fields(
            PDF, ["landlord_name"], settings=stub_settings(), doctype_id="us_lease_agreement"
        )
    )

    assert fields == []


# ---------------------------------------------------------------------------
# The HTTP client stays out of the process until a tier actually calls out
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "module_path",
    ["dce/extract/azure_specialist.py", "dce/extract/query_fields.py"],
)
def test_the_http_client_is_imported_inside_a_function_never_at_module_scope(
    module_path: str,
) -> None:
    """``httpx`` is a dev-only dependency and the runtime image ships without one.

    Importing :mod:`dce.extract` must not put an HTTP client in the process at all — a module
    that imports one at load time is reachable from anywhere, including a future caller on the
    pre-classification path. Asserted against the AST rather than ``sys.modules``, which is
    polluted by the rest of the suite.
    """
    tree = ast.parse((_ROOT / module_path).read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        and any(
            (alias.name if isinstance(node, ast.Import) else (node.module or "")).split(".")[0]
            in {"httpx", "requests", "aiohttp", "urllib3", "socket"}
            for alias in node.names
        )
        and not _inside_a_function(tree, node)
    ]
    assert offenders == [], f"{module_path} imports an HTTP client at module scope: {offenders}"


def _inside_a_function(tree: ast.AST, target: ast.AST) -> bool:
    """``True`` when ``target`` sits inside a function body somewhere in ``tree``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            child is target for child in ast.walk(node)
        ):
            return True
    return False
