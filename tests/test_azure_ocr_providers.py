"""Azure Read v3.2 and Azure Layout as OCR providers — both ways, and the line between them.

There are two legitimate ways to use a cloud recogniser here, and this file exists to keep
them from being confused with one another:

**(A) Caller-supplied — the invariant is untouched.** Somebody upstream, under their own
authorisation, runs Read or Layout and posts the result to ``/classify``. This service opens
no socket. The tests below prove that with the socket tripwire armed, which is a stronger
statement than "we did not call anything": nothing *could* have been called.

**(B) Service-side — egress, on purpose, and only when a deployment says so.** This service
calls Read or Layout during ingestion. That is pre-classification egress by definition: you
cannot classify an image without first reading it, and reading it happens either here or on
somebody else's machine. The tests below prove it is refused by default, that the refusal
happens *before* any socket is opened, and that a deployment which has enabled it is visible
on ``/readyz`` rather than merely configured.

The end-to-end cases run against the two local mocks (Read v3.2 on :5006, Document
Intelligence v4.0 on :5007) and skip when they are not running — a skip means "not
exercised", never "exercised and fine".
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from dce import adapters  # noqa: E402
from dce.classify import classify  # noqa: E402
from dce.egress import (  # noqa: E402
    EgressViolation,
    assert_ocr_egress_permitted,
    classification_scope,
    socket_tripwire,
)
from dce.ingest import IngestSettings, IngestStatus, ingest  # noqa: E402
from dce.ingest.errors import EngineUnavailable, OcrProviderMismatch  # noqa: E402
from dce.ingest.ocr import (  # noqa: E402
    ENGINES,
    PROVIDERS,
    SERVICE_ENGINES,
    is_service_provider,
    load_provider,
)
from dce.ingest.result import TextSource  # noqa: E402
from dce.models import Zone  # noqa: E402
from tests import ingest_fixtures as fixtures  # noqa: E402
from tests.test_egress import LOCKED_DOWN, block_all_sockets, specs  # noqa: E402

READ_ENDPOINT = "http://localhost:5006"
LAYOUT_ENDPOINT = "http://localhost:5007"


# ---------------------------------------------------------------------------
# Fixtures: the two payload shapes, and a picture with text in it
# ---------------------------------------------------------------------------
def read_v32_payload() -> dict:
    """A Read v3.2 job, in the shape the real service and the mock both return.

    Note what is absent and cannot be added: no ``paragraphs``, no ``role``, no ``tables``,
    no ``selectionMarks``. ``boundingBox`` is a flat 8-number array, not a ``polygon``.
    """
    return {
        "status": "succeeded",
        "createdDateTime": "2026-01-01T00:00:00Z",
        "analyzeResult": {
            "version": "3.2.0",
            "readResults": [
                {
                    "page": 1,
                    "angle": 0.0,
                    "width": 1000,
                    "height": 1400,
                    "unit": "pixel",
                    "language": "en",
                    "lines": [
                        {
                            "text": "PASSPORT",
                            "boundingBox": [10, 10, 300, 10, 300, 60, 10, 60],
                            "words": [
                                {
                                    "text": "PASSPORT",
                                    "boundingBox": [10, 10, 300, 10, 300, 60, 10, 60],
                                    "confidence": 0.99,
                                }
                            ],
                        },
                        {
                            "text": "Authority: DEPARTMENT OF STATE",
                            "boundingBox": [10, 80, 620, 80, 620, 120, 10, 120],
                            "words": [],
                        },
                        {
                            "text": "P<USAERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<",
                            "boundingBox": [10, 900, 980, 900, 980, 940, 10, 940],
                            "words": [],
                        },
                    ],
                }
            ],
        },
    }


def layout_v40_payload() -> dict:
    """A Document Intelligence v4.0 ``prebuilt-layout`` job — the same document, with roles."""
    return {
        "status": "succeeded",
        "analyzeResult": {
            "apiVersion": "2024-11-30",
            "modelId": "prebuilt-layout",
            "content": "PASSPORT\nAuthority: DEPARTMENT OF STATE",
            "pages": [
                {"pageNumber": 1, "width": 8.5, "height": 11.0, "unit": "inch", "lines": []}
            ],
            "paragraphs": [
                {
                    "role": "title",
                    "content": "PASSPORT",
                    "boundingRegions": [
                        {"pageNumber": 1, "polygon": [1, 1, 3, 1, 3, 1.5, 1, 1.5]}
                    ],
                },
                {
                    "content": "Authority: DEPARTMENT OF STATE",
                    "boundingRegions": [
                        {"pageNumber": 1, "polygon": [1, 2, 6, 2, 6, 2.4, 1, 2.4]}
                    ],
                },
                {
                    "content": "P<USAERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<",
                    "boundingRegions": [
                        {"pageNumber": 1, "polygon": [1, 9, 8, 9, 8, 9.4, 1, 9.4]}
                    ],
                },
            ],
        },
    }


def text_image() -> bytes:
    """A PNG a real OCR engine can actually read, rendered from a one-page PDF."""
    fitz = pytest.importorskip("fitz", reason="PyMuPDF is the optional .[pdf] extra")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Form W-2 Wage and Tax Statement", fontsize=26)
    page.insert_text((72, 130), "Department of the Treasury", fontsize=18)
    page.insert_text((72, 180), "Wages, tips, other compensation", fontsize=13)
    page.insert_text((72, 210), "Employer identification number (EIN)", fontsize=13)
    return page.get_pixmap(dpi=150).tobytes("png")


def mock_or_skip(endpoint: str) -> str:
    """Return ``endpoint`` if the mock behind it answers, otherwise skip."""
    host, port = endpoint.rsplit(":", 1)
    try:
        with socket.create_connection((host.rsplit("/", 1)[-1], int(port)), timeout=1):
            return endpoint
    except OSError:
        pytest.skip(f"no mock listening on {endpoint} (docker compose up in DES)")


# ---------------------------------------------------------------------------
# The adapter: Read v3.2 is a different shape, and a weaker one
# ---------------------------------------------------------------------------
def test_from_azure_read_maps_lines_pages_and_flat_bounding_boxes():
    view = adapters.from_azure_read(read_v32_payload())

    assert [b.text for b in view.blocks] == [
        "PASSPORT",
        "Authority: DEPARTMENT OF STATE",
        "P<USAERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<",
    ]
    assert view.pages[0].page == 1
    assert (view.pages[0].width, view.pages[0].height) == (1000.0, 1400.0)
    assert view.pages[0].unit == "pixel"
    assert view.languages == ["en"]
    # Read's flat 8-number boundingBox is already the quad convention.
    assert view.blocks[0].bbox == [10.0, 10.0, 300.0, 10.0, 300.0, 60.0, 10.0, 60.0]
    assert view.raw["provider"] == adapters.PROVIDER_AZURE_READ


def test_read_has_no_zones_at_all_and_says_so():
    """The accuracy difference between the two providers, pinned rather than described.

    Every block is ``body`` because Read predicts no ``role``. That is what makes a
    title-gated decisive anchor unreachable on a Read payload, and it is a property of the
    product, so it must not drift into a font-height guess later.
    """
    view = adapters.from_azure_read(read_v32_payload())

    assert {b.zone for b in view.blocks} == {Zone.body}
    assert view.has_structure is False
    assert "body only" in view.raw["zones"]

    # The same document through Layout does have a title, which is the whole point.
    layout = adapters.from_azure_layout(layout_v40_payload())
    assert Zone.title in {b.zone for b in layout.blocks}


def test_a_title_gated_decisive_anchor_is_unreachable_on_read_and_reachable_on_layout():
    """Not an opinion about the two products: the gate is evaluated, and it is not satisfied."""
    from dce.classify.anchors import anchor_scores
    from dce.models import Anchor, Category, DocTypeSpec

    gated = [
        DocTypeSpec(
            doctype_id="passport",
            label="Passport",
            country="XX",
            category=Category.identity,
            anchors=[Anchor(text="PASSPORT", decisive=True, zone=Zone.title)],
        )
    ]
    read_channel = anchor_scores(
        adapters.from_azure_read(read_v32_payload()), gated, settings=LOCKED_DOWN
    )
    layout_channel = anchor_scores(
        adapters.from_azure_layout(layout_v40_payload()), gated, settings=LOCKED_DOWN
    )

    assert read_channel.hits.get("passport", ()) == ()
    assert read_channel.muted_decisive.get("passport"), (
        "the anchor should be recorded as unevaluable, not as evaluated-and-failed"
    )
    assert layout_channel.hits.get("passport")


def test_the_payload_shape_decides_which_adapter_runs():
    assert adapters.azure_payload_kind(read_v32_payload()) == "read"
    assert adapters.azure_payload_kind(layout_v40_payload()) == "layout"
    # Unrecognisable payloads go to the more forgiving mapper rather than raising.
    assert adapters.azure_payload_kind({"nonsense": 1}) == "layout"

    assert adapters.from_azure(read_v32_payload()).raw["provider"] == (
        adapters.PROVIDER_AZURE_READ
    )
    assert adapters.from_azure(layout_v40_payload()).raw["provider"] == (
        adapters.PROVIDER_AZURE_LAYOUT
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"analyzeResult": {"readResults": []}},
        {"analyzeResult": {"readResults": [{"page": 1, "lines": [{"text": ""}]}]}},
        {"readResults": [{"lines": "not-a-list"}]},
    ],
)
def test_the_read_adapter_never_raises_on_a_malformed_payload(payload):
    """A partially-understood payload is worth more than a failed request."""
    view = adapters.from_azure_read(payload)
    assert view.blocks == []


def test_a_bare_read_result_without_its_envelope_still_maps():
    entry = read_v32_payload()["analyzeResult"]["readResults"][0]
    assert len(adapters.from_azure_read(entry).blocks) == 3
    assert len(adapters.from_azure_read({"readResults": [entry]}).blocks) == 3


# ---------------------------------------------------------------------------
# (A) CALLER-SUPPLIED: the invariant is untouched
# ---------------------------------------------------------------------------
def test_a_caller_supplied_read_payload_classifies_and_opens_zero_sockets():
    """Path (A), proven the way the rest of this service proves it: nothing was attempted."""
    with socket_tripwire() as attempts:
        view = adapters.from_azure_read(read_v32_payload())
        result = classify(view, specs(), settings=LOCKED_DOWN)

    assert attempts == []
    assert result.doctype_id == "passport"
    assert result.abstained is False


def test_a_caller_supplied_layout_payload_classifies_and_opens_zero_sockets():
    with socket_tripwire() as attempts:
        view = adapters.from_azure(layout_v40_payload())
        result = classify(view, specs(), settings=LOCKED_DOWN)

    assert attempts == []
    assert result.doctype_id == "passport"


def block_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make any **outbound** socket raise, while leaving ``AF_UNIX`` socketpairs alone.

    :func:`dce.egress.socket_tripwire` blocks every socket constructor, which is the right
    tool everywhere else in the suite. It cannot be used around Starlette's ``TestClient``:
    the in-process ASGI transport starts an asyncio event loop per request, and every asyncio
    loop builds an ``AF_UNIX`` self-pipe with ``socket.socketpair()``. That pipe never leaves
    the machine and is not what is under test, so this variant refuses exactly the things that
    would: name resolution, ``create_connection``, and any ``AF_INET``/``AF_INET6`` socket.
    """
    attempts: list[str] = []
    real_socket = socket.socket

    def refuse(name: str):
        def raiser(*args, **kwargs):
            attempts.append(name)
            raise AssertionError(f"the caller-supplied path attempted {name}: egress")

        return raiser

    def guarded_socket(family=socket.AF_INET, *args, **kwargs):
        if family in (socket.AF_INET, socket.AF_INET6):
            attempts.append("socket.socket")
            raise AssertionError("the caller-supplied path opened an outbound socket: egress")
        return real_socket(family, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", guarded_socket)
    monkeypatch.setattr(socket, "create_connection", refuse("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("socket.getaddrinfo"))
    monkeypatch.setattr(socket, "gethostbyname", refuse("socket.gethostbyname"))
    return attempts


def test_both_payloads_reach_classify_through_the_api_without_a_socket(
    monkeypatch: pytest.MonkeyPatch,
):
    """The same proof through the real routes, including the field that names the shape."""
    from tests.test_api import build_app

    client, classifier, _ = build_app()
    attempts = block_outbound_sockets(monkeypatch)
    read = client.post("/api/v1/classify", json={"azure_read_result": read_v32_payload()})
    sniffed = client.post("/api/v1/classify", json={"azure_analyze_result": read_v32_payload()})
    layout = client.post("/api/v1/classify", json={"azure_analyze_result": layout_v40_payload()})

    assert attempts == []
    assert [r.status_code for r in (read, sniffed, layout)] == [200, 200, 200]
    # Which adapter ran is in the response, not left to be inferred.
    assert read.headers["X-Document-Source"] == adapters.PROVIDER_AZURE_READ
    assert sniffed.headers["X-Document-Source"] == adapters.PROVIDER_AZURE_READ
    assert layout.headers["X-Document-Source"] == adapters.PROVIDER_AZURE_LAYOUT
    assert [v.raw["provider"] for v in classifier.views] == [
        adapters.PROVIDER_AZURE_READ,
        adapters.PROVIDER_AZURE_READ,
        adapters.PROVIDER_AZURE_LAYOUT,
    ]


def test_process_reports_the_source_in_the_body_and_says_nothing_left_the_process():
    from tests.test_api import build_app

    client, _, _ = build_app()
    body = client.post(
        "/api/v1/process", json={"azure_analyze_result": read_v32_payload()}
    ).json()

    assert body["source"]["provider"] == adapters.PROVIDER_AZURE_READ
    assert body["source"]["remote"] is False
    assert body["source"]["endpoint_host"] == ""
    assert "opened no socket" in body["source"]["note"]


def test_the_declared_field_wins_over_the_sniffer():
    """``azure_read_result`` means Read, whatever the shape sniffer would have said."""
    view = adapters.from_azure_read(layout_v40_payload())
    assert view.blocks == []  # a Layout payload has no readResults to map — and it says so


# ---------------------------------------------------------------------------
# The provider registry: network-ness is a flag, not a naming convention
# ---------------------------------------------------------------------------
def test_the_local_allowlist_is_unchanged_and_still_closed():
    assert set(ENGINES) == {"rapidocr", "tesseract"}
    assert set(SERVICE_ENGINES) == {"azure_read", "azure_layout"}
    assert set(PROVIDERS) == {"rapidocr", "tesseract", "azure_read", "azure_layout"}


def test_the_code_distinguishes_service_providers_by_a_flag():
    assert [name for name, info in PROVIDERS.items() if info.service] == [
        "azure_read",
        "azure_layout",
    ]
    assert is_service_provider("azure_layout") is True
    assert is_service_provider("rapidocr") is False
    assert is_service_provider("azure-read") is False  # not a provider id at all


def test_the_local_loader_refuses_a_service_provider_and_says_where_it_belongs():
    """A service provider must not be reachable through the in-process engine loader."""
    for name in ("azure_read", "azure_layout"):
        with pytest.raises(EngineUnavailable, match="unknown local OCR engine") as excinfo:
            load_provider(name)
        message = str(excinfo.value)
        assert "OCR SERVICE provider" in message
        assert "DCE_INGEST_OCR_SERVICE_ENABLED" in message

    # And the old hole is still closed.
    with pytest.raises(EngineUnavailable, match="unknown local OCR engine"):
        load_provider("https://westeurope.api.cognitive.microsoft.com")


# ---------------------------------------------------------------------------
# (B) SERVICE-SIDE: refused unless a deployment said so
# ---------------------------------------------------------------------------
def test_the_guard_refuses_when_the_deployment_has_not_enabled_it():
    with pytest.raises(EgressViolation) as excinfo:
        assert_ocr_egress_permitted("azure_layout", LAYOUT_ENDPOINT, enabled=False)

    message = str(excinfo.value)
    assert "azure_layout" in message and LAYOUT_ENDPOINT in message
    assert "DCE_INGEST_OCR_SERVICE_ENABLED" in message


def test_the_guard_permits_only_when_enabled():
    assert assert_ocr_egress_permitted("azure_layout", LAYOUT_ENDPOINT, enabled=True) is None


def test_the_guard_refuses_inside_a_classification_scope_however_it_is_configured():
    """No setting makes "ask a third party what this document is" acceptable mid-cascade."""
    with classification_scope(), pytest.raises(EgressViolation, match="classification_scope"):
        assert_ocr_egress_permitted("azure_layout", LAYOUT_ENDPOINT, enabled=True)


def test_the_guard_fires_before_a_socket_is_opened(monkeypatch: pytest.MonkeyPatch):
    """The refusal must not be "the request failed" — the request must never be made."""
    from dce.ingest.detect import MediaType
    from dce.ingest.limits import Deadline
    from dce.ingest.ocr_service import OcrServiceConfig, load_ocr_service_provider

    provider = load_ocr_service_provider(
        OcrServiceConfig(
            provider="azure_layout",
            endpoint=LAYOUT_ENDPOINT,
            key="",
            api_version="2024-11-30",
            model="prebuilt-layout",
        ),
        enabled=False,
    )
    attempts = block_all_sockets(monkeypatch)
    with pytest.raises(EgressViolation):
        provider.recognize(b"\x89PNG\r\n\x1a\n", media_type=MediaType.png, deadline=Deadline(5))
    assert attempts == []


def test_with_remote_ocr_off_an_image_is_needs_ocr_and_no_socket_is_opened(
    monkeypatch: pytest.MonkeyPatch,
):
    """The default deployment. (B) is not merely unused here — it is unreachable."""
    off = IngestSettings(_env_file=None)
    assert off.ocr_service_enabled is False

    attempts = block_all_sockets(monkeypatch)
    with socket_tripwire() as blocked:
        result = ingest(fixtures.jpeg(), settings=off)

    assert result.status is IngestStatus.needs_ocr
    assert result.view is None
    assert result.ocr_via_service is False
    assert attempts == [] and blocked == []
    assert "No recogniser is configured on this deployment" in result.reason


def test_a_request_cannot_switch_remote_ocr_on():
    """Same asymmetry as ``local_ocr``: a caller may decline, never grant."""
    off = IngestSettings(_env_file=None)
    with socket_tripwire() as blocked:
        result = ingest(fixtures.png(), settings=off, ocr_service=True)
    assert result.status is IngestStatus.needs_ocr
    assert blocked == []


def test_a_request_can_decline_the_ocr_service_with_either_flag():
    """A caller who says "do not run OCR on this" is obeyed whichever kind is configured."""
    on = IngestSettings(
        _env_file=None,
        ocr_service_enabled=True,
        ocr_service_provider="azure_layout",
        azure_di_endpoint=LAYOUT_ENDPOINT,
    )
    for kwargs in ({"ocr_service": False}, {"local_ocr": False}):
        with socket_tripwire() as blocked:
            result = ingest(fixtures.png(), settings=on, **kwargs)
        assert result.status is IngestStatus.needs_ocr
        assert blocked == [], f"{kwargs} still reached the network"
        assert "this request declined recognition" in result.reason


# ---------------------------------------------------------------------------
# The ocr_provider pin: an assertion about the deployment, never a selector
# ---------------------------------------------------------------------------
# The failure this exists to prevent is silent and it is the worst one on this path. A console
# tells an operator "this document will be sent to <endpoint>"; the operator accepts; the
# deployment is meanwhile configured to a different provider; the document goes to a third
# party nobody acknowledged — and every response still reports, truthfully, that OCR worked.
# Nothing else in the system notices, because from the service's point of view it did exactly
# what it was configured to do.
def test_a_pin_naming_the_configured_provider_is_honoured():
    """The agreeing case must not become an error, or callers will stop sending the pin."""
    on = IngestSettings(
        _env_file=None,
        ocr_service_enabled=True,
        ocr_service_provider="azure_layout",
        azure_di_endpoint=LAYOUT_ENDPOINT,
    )
    # Declining as well keeps this off the network: what is under test is the pin check, not
    # the mock.
    result = ingest(fixtures.png(), settings=on, ocr_service=False, ocr_provider="azure_layout")
    assert result.status is IngestStatus.needs_ocr  # because it was declined, not pinned away


def test_a_pin_naming_a_different_provider_is_refused_rather_than_substituted():
    """The whole point: the document is not read by a provider nobody disclosed."""
    on = IngestSettings(
        _env_file=None,
        ocr_service_enabled=True,
        ocr_service_provider="azure_layout",
        azure_di_endpoint=LAYOUT_ENDPOINT,
    )
    with socket_tripwire() as blocked, pytest.raises(OcrProviderMismatch) as caught:
        ingest(fixtures.png(), settings=on, ocr_provider="azure_read")
    # Refused *before* the wire, not after a call that would already have disclosed the bytes.
    assert blocked == []
    assert "azure_read" in str(caught.value) and "azure_layout" in str(caught.value)
    assert "Refusing rather than substituting" in str(caught.value)


def test_a_pin_cannot_switch_a_provider_on():
    """The pin carries the same asymmetry as every other caller flag: decline, never grant."""
    off = IngestSettings(_env_file=None)
    with socket_tripwire() as blocked, pytest.raises(OcrProviderMismatch) as caught:
        ingest(fixtures.png(), settings=off, ocr_provider="azure_read")
    assert blocked == []
    assert "no recogniser is configured" in str(caught.value)


def test_an_absent_pin_changes_nothing():
    """The field is optional and the default path must be exactly as it was."""
    off = IngestSettings(_env_file=None)
    assert ingest(fixtures.png(), settings=off).status is IngestStatus.needs_ocr
    assert ingest(fixtures.png(), settings=off, ocr_provider=None).status is (
        IngestStatus.needs_ocr
    )


def test_a_mismatched_pin_is_a_structured_400_through_the_api(monkeypatch: pytest.MonkeyPatch):
    """The seam the console actually hits: a refusal it can branch on, not a 500."""
    import base64

    from dce.api import routes
    from tests.test_api import build_app

    monkeypatch.setattr(
        routes,
        "get_ingest_settings",
        lambda: IngestSettings(
            _env_file=None,
            ocr_service_enabled=True,
            ocr_service_provider="azure_layout",
            azure_di_endpoint=LAYOUT_ENDPOINT,
        ),
    )
    client, _, _ = build_app()
    attempts = block_outbound_sockets(monkeypatch)
    response = client.post(
        "/api/v1/classify",
        json={
            "content_base64": base64.b64encode(fixtures.png()).decode(),
            "ingest": {"filename": "x.png", "ocr_provider": "azure_read"},
        },
    )

    assert response.status_code == 400
    assert attempts == []
    assert response.json()["detail"]["error"] == "ocr_provider_mismatch"


def test_the_pin_values_are_exactly_what_readyz_advertises():
    """A pin a caller cannot spell correctly is a trap.

    ``/readyz`` lists provider names; the pin is compared against
    :meth:`IngestSettings.active_provider`. If those two ever drift, every pinning caller gets
    a mismatch error on a correctly-configured deployment. Tie them together here.
    """
    from dce.api.routes import _ocr_status

    for provider, field in (
        ("azure_layout", "azure_di_endpoint"),
        ("azure_read", "azure_read_endpoint"),
    ):
        settings = IngestSettings(
            _env_file=None,
            ocr_service_enabled=True,
            ocr_service_provider=provider,
            **{field: "https://example.invalid"},
        )
        advertised = [p.name for p in _ocr_status(settings).providers if p.available]
        assert advertised == [settings.default_provider()] == [provider]
        assert [p.name for p in _ocr_status(settings).providers if p.default] == [provider]


# ---------------------------------------------------------------------------
# Configuration that can only be a mistake is refused at boot
# ---------------------------------------------------------------------------
def test_configuring_both_recognisers_without_a_default_is_refused():
    """Both may be configured — but not with the precedence left to the code.

    This replaces the older rule that refused the combination outright. A deployment whose OCR
    runs on its own network legitimately wants every provider selectable, and
    ``ingest.ocr_provider`` is how a request picks one. What must not happen is the *unpinned*
    request silently getting whichever the code happened to prefer, so the deployment names it.
    """
    with pytest.raises(ValueError, match="ocr_default_provider is empty"):
        IngestSettings(
            _env_file=None,
            local_ocr_enabled=True,
            ocr_service_enabled=True,
            azure_di_endpoint=LAYOUT_ENDPOINT,
        )


def test_configuring_both_recognisers_with_a_declared_default_is_allowed():
    settings = IngestSettings(
        _env_file=None,
        local_ocr_enabled=True,
        local_ocr_engine="rapidocr",
        ocr_service_enabled=True,
        ocr_service_provider="azure_layout",
        ocr_default_provider="rapidocr",
        azure_di_endpoint=LAYOUT_ENDPOINT,
        azure_read_endpoint=READ_ENDPOINT,
    )
    assert settings.configured_providers() == ("rapidocr", "azure_layout", "azure_read")
    assert settings.default_provider() == "rapidocr"
    assert settings.service_providers() == ("azure_layout", "azure_read")
    assert settings.local_providers() == ("rapidocr",)


def test_a_default_naming_an_unconfigured_provider_is_refused():
    """The default selects among what is configured; it cannot switch anything on."""
    with pytest.raises(ValueError, match="is not configured on this deployment"):
        IngestSettings(
            _env_file=None,
            local_ocr_enabled=True,
            ocr_default_provider="azure_read",
        )


def test_a_pin_selects_among_several_configured_providers():
    """The owner's deployment: every provider configured, each request choosing one.

    The asymmetry survives — see the next test — but with more than one recogniser configured
    the pin is how a caller reaches the one it wants instead of the deployment's default.
    """
    settings = IngestSettings(
        _env_file=None,
        local_ocr_enabled=True,
        ocr_service_enabled=True,
        ocr_service_provider="azure_layout",
        ocr_default_provider="rapidocr",
        azure_di_endpoint=LAYOUT_ENDPOINT,
        azure_read_endpoint=READ_ENDPOINT,
    )
    # Declining keeps this off the wire: what is under test is the selection, not the mock.
    for pinned in ("rapidocr", "azure_layout", "azure_read"):
        with socket_tripwire() as blocked:
            result = ingest(
                fixtures.png(), settings=settings, ocr_service=False, ocr_provider=pinned
            )
        assert blocked == []
        assert result.status is IngestStatus.needs_ocr
        assert pinned in result.reason


def test_a_pin_still_cannot_reach_a_provider_the_deployment_did_not_configure():
    """More providers configured does not weaken the asymmetry: choose among, never add."""
    settings = IngestSettings(
        _env_file=None,
        local_ocr_enabled=True,
        ocr_service_enabled=True,
        ocr_service_provider="azure_layout",
        ocr_default_provider="rapidocr",
        azure_di_endpoint=LAYOUT_ENDPOINT,
    )
    assert "azure_read" not in settings.configured_providers()
    with socket_tripwire() as blocked, pytest.raises(OcrProviderMismatch) as caught:
        ingest(fixtures.png(), settings=settings, ocr_provider="azure_read")
    assert blocked == []
    assert "azure_read" in str(caught.value)


def test_a_local_engine_cannot_be_named_as_the_ocr_service_provider():
    with pytest.raises(ValueError, match="not an OCR service provider"):
        IngestSettings(_env_file=None, ocr_service_enabled=True, ocr_service_provider="rapidocr")


def test_a_missing_endpoint_degrades_rather_than_taking_the_service_down():
    """A secret that has not landed is not a reason to stop classifying text documents."""
    settings = IngestSettings(
        _env_file=None, ocr_service_enabled=True, ocr_service_provider="azure_layout"
    )
    assert "DCE_INGEST_AZURE_DI_ENDPOINT is empty" in settings.ocr_service_problem()
    with socket_tripwire() as blocked:
        result = ingest(fixtures.jpeg(), settings=settings)
    assert result.status is IngestStatus.needs_ocr
    assert blocked == []
    assert "unusable" in result.reason


def test_the_endpoint_host_is_reported_without_the_rest_of_the_url():
    settings = IngestSettings(
        _env_file=None,
        ocr_service_enabled=True,
        ocr_service_provider="azure_read",
        azure_read_endpoint="https://example.cognitiveservices.azure.com/some/path",
    )
    assert settings.ocr_service_endpoint_host() == "example.cognitiveservices.azure.com"
    assert settings.ocr_service_problem() == ""


# ---------------------------------------------------------------------------
# Posture: an operator must see the disclosure without being told to look
# ---------------------------------------------------------------------------
def test_readyz_reports_a_local_only_deployment_as_transmitting_nothing():
    from tests.test_api import build_app

    client, _, _ = build_app()
    body = client.get("/readyz").json()

    assert body["ocr"]["network"] is False
    assert body["egress"]["preclassification_ocr"] is False
    assert body["egress"]["preclassification_ocr_endpoint"] == ""


def test_readyz_names_the_endpoint_when_this_deployment_ships_unclassified_documents(
    monkeypatch: pytest.MonkeyPatch,
):
    """The requirement, tested as an operator would check it: read /readyz, see the host."""
    from dce.api import routes
    from tests.test_api import build_app

    remote = IngestSettings(
        _env_file=None,
        ocr_service_enabled=True,
        ocr_service_provider="azure_layout",
        azure_di_endpoint="https://contoso.cognitiveservices.azure.com",
    )
    monkeypatch.setattr(routes, "get_ingest_settings", lambda: remote)

    client, _, _ = build_app()
    body = client.get("/readyz").json()

    assert body["ocr"] == {
        "provider": "azure_layout",
        "enabled": True,
        "network": True,
        "endpoint_host": "contoso.cognitiveservices.azure.com",
        # Nothing was declared, so the cautious reading applies and says so.
        "trust_boundary": "external",
        "trust_boundary_declared": False,
        "trust_boundary_attribution": body["ocr"]["trust_boundary_attribution"],
        "text_layer_policy": "verify",
        "text_layer_attribution": body["ocr"]["text_layer_attribution"],
        "problem": "",
        "summary": body["ocr"]["summary"],
        "local_ocr_enabled": False,
        "local_ocr_engine": body["ocr"]["local_ocr_engine"],
        "providers": body["ocr"]["providers"],
        "configured_providers": ["azure_layout"],
        "service_endpoint_hosts": ["contoso.cognitiveservices.azure.com"],
    }
    assert "TRANSMITS UNCLASSIFIED DOCUMENTS" in body["ocr"]["summary"]
    assert "contoso.cognitiveservices.azure.com" in body["ocr"]["summary"]
    assert "no trust boundary has been declared" in body["ocr"]["trust_boundary_attribution"]

    # Exactly one provider is available, it is the configured one, and it is the only row
    # carrying the endpoint. The other network provider is still listed, so an operator can
    # see that Read exists here and is not what this deployment uses, rather than having to
    # infer that from an absence.
    listed = {p["name"]: p for p in body["ocr"]["providers"]}
    assert [name for name, p in listed.items() if p["available"]] == ["azure_layout"]
    assert listed["azure_layout"]["endpoint"] == "contoso.cognitiveservices.azure.com"
    assert listed["azure_read"]["endpoint"] == ""
    assert "azure_layout" in listed["azure_read"]["reason"]

    # The egress block must not read "in-process only" while this is true.
    assert body["egress"]["preclassification_ocr"] is True
    assert (
        body["egress"]["preclassification_ocr_endpoint"]
        == "contoso.cognitiveservices.azure.com"
    )
    assert body["egress"]["preclassification_ocr_trust_boundary"] == "external"
    assert "BUT this deployment sends" in body["egress"]["note"]
    # A deliberate configuration is not an outage: the service still serves.
    assert body["ready"] is True


# ---------------------------------------------------------------------------
# The trust boundary is a DECLARATION: attributable, defaulted cautiously, and
# incapable of making a transmission stop being reported
# ---------------------------------------------------------------------------
def test_the_code_default_is_external_so_silence_cannot_produce_the_reassuring_answer():
    """A deployment that declares nothing must get the cautious reading.

    This is the asymmetry the whole feature rests on. The code cannot tell an internal host
    from an external one, so it must not guess in the direction that reassures.
    """
    settings = IngestSettings(_env_file=None)
    assert settings.trust_boundary() == "external"
    assert settings.trust_boundary_declared() is False


def test_a_trust_boundary_that_is_not_one_of_the_two_is_refused_rather_than_guessed_at():
    """Both fallbacks are wrong, so there is no fallback.

    Falling back to ``external`` gives a deployment that wrote ``on-premises`` an alarming page
    it believed it had answered; falling back to ``on_premises`` lets a typo produce the
    reassuring reading. Refusing is the only behaviour that cannot mislead.
    """
    with pytest.raises(ValueError, match="not a trust boundary"):
        IngestSettings(_env_file=None, ocr_service_trust_boundary="on-premises")


def test_declaring_on_premises_changes_the_wording_and_nothing_about_the_operation(
    monkeypatch: pytest.MonkeyPatch,
):
    """The load-bearing property: a declaration may re-describe a hop, never conceal it.

    An operator could otherwise set one variable and watch the disclosure disappear. So the
    two deployments below differ in exactly one setting, and every field that says *something
    leaves this process* must be identical between them.
    """
    from dce.api import routes
    from tests.test_api import build_app

    def readyz(boundary: str | None) -> dict:
        extra = {} if boundary is None else {"ocr_service_trust_boundary": boundary}
        settings = IngestSettings(
            _env_file=None,
            ocr_service_enabled=True,
            ocr_service_provider="azure_layout",
            azure_di_endpoint="https://ocr.internal.corp",
            **extra,
        )
        monkeypatch.setattr(routes, "get_ingest_settings", lambda: settings)
        client, _, _ = build_app()
        return client.get("/readyz").json()

    external = readyz(None)
    on_prem = readyz("on_premises")

    # Identical where it counts. The bytes leave in both, to the same named host, and /readyz
    # says so in both.
    for body in (external, on_prem):
        assert body["ocr"]["network"] is True
        assert body["ocr"]["enabled"] is True
        assert body["ocr"]["endpoint_host"] == "ocr.internal.corp"
        assert body["egress"]["preclassification_ocr"] is True
        assert body["egress"]["preclassification_ocr_endpoint"] == "ocr.internal.corp"
        assert body["egress"]["enforced"] is True
        assert "before their doctype is known" in body["ocr"]["summary"]
        assert "ocr.internal.corp" in body["ocr"]["summary"]

    # Different only in how it reads, and in the declaration each one reports.
    assert external["ocr"]["trust_boundary"] == "external"
    assert on_prem["ocr"]["trust_boundary"] == "on_premises"
    assert on_prem["egress"]["preclassification_ocr_trust_boundary"] == "on_premises"
    assert "TRANSMITS UNCLASSIFIED DOCUMENTS" in external["ocr"]["summary"]
    assert "TRANSMITS UNCLASSIFIED DOCUMENTS" not in on_prem["ocr"]["summary"]


def test_the_on_premises_reading_is_attributed_to_the_operator_and_marked_unverified():
    """A page that goes quiet because a flag was set is worse than one that shouts.

    What makes the quieter wording legitimate is that it is a *claim with an owner*: the
    variable that produced it is named, the operator is named as its source, and the service
    says plainly that it did not check.
    """
    settings = IngestSettings(
        _env_file=None,
        ocr_service_enabled=True,
        ocr_service_provider="azure_layout",
        azure_di_endpoint="https://ocr.internal.corp",
        ocr_service_trust_boundary="on_premises",
    )
    attribution = settings.trust_boundary_attribution()

    assert "DCE_INGEST_OCR_SERVICE_TRUST_BOUNDARY=on_premises" in attribution
    assert "declaration" in attribution
    assert "not verified" in attribution or "has not verified" in attribution
    # And it still states the operation, so the attribution cannot be read as a denial. The
    # wording is descriptive now rather than alarmed — "over a call from this process" instead
    # of "does leave this process" — but the fact it has to carry is the same one.
    assert "over a call from this process" in attribution
    assert "before the doctype is known" in attribution


def test_declared_external_and_defaulted_external_are_told_apart():
    """"We chose external" and "nobody said" are different claims about the same value."""
    common = {
        "_env_file": None,
        "ocr_service_enabled": True,
        "ocr_service_provider": "azure_layout",
        "azure_di_endpoint": "https://contoso.cognitiveservices.azure.com",
    }
    chose = IngestSettings(**common, ocr_service_trust_boundary="external")
    silent = IngestSettings(**common)

    assert chose.trust_boundary() == silent.trust_boundary() == "external"
    assert chose.trust_boundary_declared() is True
    assert silent.trust_boundary_declared() is False
    assert "declares" in chose.trust_boundary_attribution()
    assert "no trust boundary has been declared" in silent.trust_boundary_attribution()


def test_the_boundary_question_does_not_arise_without_a_remote_provider():
    """No endpoint, nothing to make a claim about — and so no claim is made."""
    settings = IngestSettings(_env_file=None, ocr_service_trust_boundary="on_premises")
    assert settings.trust_boundary_attribution() == ""


def test_declaring_on_premises_does_not_let_a_document_out_during_classification():
    """The invariant is untouched: this setting describes ingestion, and cannot widen it."""
    settings = IngestSettings(
        _env_file=None,
        ocr_service_enabled=True,
        ocr_service_provider="azure_layout",
        ocr_service_trust_boundary="on_premises",
    )
    # No endpoint configured, so there is nowhere to send it — and the socket tripwire proves
    # nothing was attempted anyway.
    with socket_tripwire() as blocked:
        result = ingest(fixtures.jpeg(), settings=settings)
    assert result.status is IngestStatus.needs_ocr
    assert blocked == []


# ---------------------------------------------------------------------------
# End to end against the live mocks
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("provider", "endpoint", "expected_adapter", "zoned"),
    [
        ("azure_layout", LAYOUT_ENDPOINT, adapters.PROVIDER_AZURE_LAYOUT, True),
        ("azure_read", READ_ENDPOINT, adapters.PROVIDER_AZURE_READ, False),
    ],
)
def test_a_live_mock_drives_a_real_end_to_end_classification(
    provider, endpoint, expected_adapter, zoned
):
    """Bytes in, 202 + Operation-Location polled, adapter, cascade, accepted doctype out."""
    from dce.classify.cascade import load_registry
    from dce.config import Settings

    mock_or_skip(endpoint)
    field = "azure_di_endpoint" if provider == "azure_layout" else "azure_read_endpoint"
    settings = IngestSettings(
        _env_file=None,
        ocr_service_enabled=True,
        ocr_service_provider=provider,
        **{field: endpoint},
    )

    result = ingest(text_image(), doc_id="pan-1", settings=settings)

    assert result.status is IngestStatus.ok
    assert result.text_source is TextSource.ocr_service
    assert result.ocr_engine == provider
    assert result.ocr_via_service is True
    assert result.ocr_endpoint_host == "localhost"
    assert result.view.raw["provider"] == expected_adapter
    assert result.view.raw["ocr_via_service"] is True

    # The provider difference, observed rather than asserted from a docstring.
    zones = {b.zone for b in result.view.blocks}
    assert (Zone.title in zones) is zoned, f"{provider} zones: {zones}"

    classification = classify(result.view, load_registry(), settings=Settings(_env_file=None))
    assert classification.doctype_id == "us_w2"
    assert classification.abstained is False


def test_the_same_bytes_classify_the_same_way_through_the_caller_supplied_path():
    """Path (B) is path (A) with the call made here — so the two must agree on one document.

    Any drift between them would mean the service-side provider is doing something to the
    payload that a caller's own Azure result does not get, which is exactly the divergence
    that makes two paths impossible to reason about together.
    """
    from dce.classify.cascade import load_registry
    from dce.config import Settings

    mock_or_skip(LAYOUT_ENDPOINT)
    settings = IngestSettings(
        _env_file=None,
        ocr_service_enabled=True,
        ocr_service_provider="azure_layout",
        azure_di_endpoint=LAYOUT_ENDPOINT,
    )
    image = text_image()

    # (B): we call the mock.
    service_side = ingest(image, settings=settings).view

    # (A): fetch the same payload the way an upstream service would, then hand it over with
    # the socket tripwire armed — the adapter half of the work must open nothing.
    httpx = pytest.importorskip("httpx")
    with httpx.Client(timeout=30.0) as client:
        submitted = client.post(
            f"{LAYOUT_ENDPOINT}/documentintelligence/documentModels/prebuilt-layout:analyze",
            params={"api-version": "2024-11-30"},
            content=image,
            headers={"Content-Type": "image/png"},
        )
        assert submitted.status_code == 202
        operation = submitted.headers["Operation-Location"]
        job = {}
        for _ in range(60):
            job = client.get(operation).json()
            if str(job.get("status", "")).lower() in {"succeeded", "failed"}:
                break
        assert job.get("status") == "succeeded"

    registry = load_registry()
    config = Settings(_env_file=None)
    with socket_tripwire() as attempts:
        caller_supplied = adapters.from_azure(job)
        caller_result = classify(caller_supplied, registry, settings=config)

    assert attempts == []
    assert [b.text for b in caller_supplied.blocks] == [b.text for b in service_side.blocks]
    assert [b.zone for b in caller_supplied.blocks] == [b.zone for b in service_side.blocks]
    assert caller_result.doctype_id == classify(
        service_side, registry, settings=config
    ).doctype_id


# ---------------------------------------------------------------------------
# The zero-egress build must stay zero-egress by construction, not by discipline
# ---------------------------------------------------------------------------
def test_no_ingest_module_imports_an_http_client_at_module_scope():
    """Ingestion runs BEFORE classification, so this is the invariant's real front line.

    ``tests/test_api.py`` makes the same assertion over the classifier's modules. It does not
    cover ``dce/ingest``, and it predates both this package and the remote provider — so the
    module that exists to reach the network would have slipped through it. The rule here is
    stricter than "no HTTP client in this package", because there now legitimately is one:
    **no module-scope import of one, including in** :mod:`dce.ingest.ocr_service`. A default
    build that configures no OCR service must never load an HTTP client at all, and the only
    way to guarantee that is for the import to sit inside the function that needs it.
    """
    import re

    pattern = re.compile(
        r"^\s*(?:import|from)\s+(httpx|requests|aiohttp|urllib3|http\.client|socket)\b",
        re.MULTILINE,
    )
    offenders: list[str] = []
    for path in sorted((_REPO_ROOT / "dce" / "ingest").rglob("*.py")):
        match = pattern.search(path.read_text("utf-8"))
        if match:
            offenders.append(f"{path.relative_to(_REPO_ROOT)}: {match.group(0).strip()}")
    assert offenders == [], f"HTTP client imported at module scope in ingestion: {offenders}"


def test_the_service_module_gets_its_client_lazily_and_names_the_extra():
    """The one place allowed to reach the network says how, and how to not install it."""
    source = (_REPO_ROOT / "dce" / "ingest" / "ocr_service.py").read_text("utf-8")
    assert 'importlib.import_module("httpx")' in source
    assert "azure-ocr" in source


# ---------------------------------------------------------------------------
# The rename: the old environment variables still configure the same deployment
# ---------------------------------------------------------------------------
# `remote_ocr` described a disclosure. On a deployment whose OCR runs inside its own network
# that description was wrong, so the concept is now `ocr_service` — an endpoint this
# deployment configures. A rename that broke a running deployment on upgrade would be a worse
# failure than the wording it fixed, so every old name is still read.
def test_the_old_environment_variables_still_configure_the_new_settings(
    monkeypatch: pytest.MonkeyPatch,
):
    for name, value in (
        ("DCE_INGEST_REMOTE_OCR_ENABLED", "true"),
        ("DCE_INGEST_REMOTE_OCR_PROVIDER", "azure_read"),
        ("DCE_INGEST_REMOTE_OCR_TRUST_BOUNDARY", "on_premises"),
        ("DCE_INGEST_REMOTE_OCR_TIMEOUT_SECONDS", "12"),
        ("DCE_INGEST_REMOTE_OCR_MAX_POLLS", "7"),
        ("DCE_INGEST_AZURE_READ_ENDPOINT", "https://ocr.internal.corp"),
    ):
        monkeypatch.setenv(name, value)

    settings = IngestSettings(_env_file=None)

    assert settings.ocr_service_enabled is True
    assert settings.ocr_service_provider == "azure_read"
    assert settings.default_provider() == "azure_read"
    assert settings.trust_boundary() == "on_premises"
    assert settings.trust_boundary_declared() is True
    assert settings.ocr_service_timeout_seconds == 12.0
    assert settings.ocr_service_max_polls == 7
    assert settings.ocr_service_endpoint_host() == "ocr.internal.corp"


def test_the_new_environment_variable_wins_when_both_are_set(monkeypatch: pytest.MonkeyPatch):
    """An upgrade that sets the new name must not be overridden by a stale old one."""
    monkeypatch.setenv("DCE_INGEST_REMOTE_OCR_PROVIDER", "azure_read")
    monkeypatch.setenv("DCE_INGEST_OCR_SERVICE_PROVIDER", "azure_layout")
    monkeypatch.setenv("DCE_INGEST_OCR_SERVICE_ENABLED", "true")
    monkeypatch.setenv("DCE_INGEST_AZURE_DI_ENDPOINT", LAYOUT_ENDPOINT)

    assert IngestSettings(_env_file=None).ocr_service_provider == "azure_layout"


def test_the_deprecated_names_are_reported_so_a_boot_log_can_name_them(
    monkeypatch: pytest.MonkeyPatch,
):
    """Silent aliasing would leave a deployment on the old names forever."""
    from dce.ingest.settings import legacy_env_aliases_in_use

    assert legacy_env_aliases_in_use({"DCE_INGEST_OCR_SERVICE_ENABLED": "true"}) == {}
    assert legacy_env_aliases_in_use({"DCE_INGEST_REMOTE_OCR_ENABLED": "true"}) == {
        "DCE_INGEST_REMOTE_OCR_ENABLED": "DCE_INGEST_OCR_SERVICE_ENABLED"
    }


def test_a_caller_may_still_send_the_old_request_flag():
    """``ingest.remote_ocr`` is the old spelling of ``ingest.ocr_service``."""
    from dce.ingest import IngestOptions

    assert IngestOptions(remote_ocr=False).ocr_service is False
    assert IngestOptions(ocr_service=False).ocr_service is False
    assert IngestOptions().ocr_service is None


# ---------------------------------------------------------------------------
# Posture wording: configuration under on_premises, caution under external
# ---------------------------------------------------------------------------
# Both paths are tested because the asymmetry is the point. A deployment that has declared
# its OCR is in-network should not be told it is disclosing documents; a deployment that has
# declared nothing must not be reassured. The *facts* — provider, endpoint host, "before the
# doctype is known" — are in both, because the wording may re-describe the operation and may
# never conceal it.
def _readyz_with(monkeypatch: pytest.MonkeyPatch, **overrides) -> dict:
    from dce.api import routes
    from tests.test_api import build_app

    settings = IngestSettings(
        _env_file=None,
        ocr_service_enabled=True,
        ocr_service_provider="azure_layout",
        azure_di_endpoint="https://ocr.internal.corp",
        **overrides,
    )
    monkeypatch.setattr(routes, "get_ingest_settings", lambda: settings)
    client, _, _ = build_app()
    return client.get("/readyz").json()


def test_under_on_premises_the_posture_reads_as_configuration_not_a_warning(
    monkeypatch: pytest.MonkeyPatch,
):
    body = _readyz_with(monkeypatch, ocr_service_trust_boundary="on_premises")

    note = body["egress"]["note"]
    summary = body["ocr"]["summary"]

    # It still says WHERE, WHAT reads it, and WHEN — an operator needs the endpoint.
    assert "ocr.internal.corp" in note and "ocr.internal.corp" in summary
    assert "azure_layout" in note and "azure_layout" in summary
    assert "before their doctype is known" in note
    assert body["egress"]["preclassification_ocr"] is True
    assert body["egress"]["preclassification_ocr_endpoint"] == "ocr.internal.corp"
    assert body["egress"]["preclassification_ocr_trust_boundary"] == "on_premises"

    # And it does not describe that as a disclosure.
    assert "BUT this deployment sends" not in note
    assert "TRANSMITS UNCLASSIFIED DOCUMENTS" not in summary
    assert "third party" not in summary and "third party" not in note
    assert "on its own network" in note and "on its own network" in summary


def test_under_external_the_posture_stays_cautious_declared_or_merely_defaulted(
    monkeypatch: pytest.MonkeyPatch,
):
    """A deployment that declares nothing must not get the reassuring reading."""
    for overrides in ({}, {"ocr_service_trust_boundary": "external"}):
        body = _readyz_with(monkeypatch, **overrides)

        assert "BUT this deployment sends" in body["egress"]["note"]
        assert "TRANSMITS UNCLASSIFIED DOCUMENTS" in body["ocr"]["summary"]
        assert body["egress"]["preclassification_ocr_trust_boundary"] == "external"
        assert body["ocr"]["endpoint_host"] == "ocr.internal.corp"

    # Only the attribution distinguishes "we chose external" from "nobody said".
    assert "no trust boundary has been declared" in (
        _readyz_with(monkeypatch)["ocr"]["trust_boundary_attribution"]
    )


def test_readyz_lists_every_configured_provider_as_selectable(
    monkeypatch: pytest.MonkeyPatch,
):
    """The deployment this compose file describes: all of them usable, one of them default."""
    body = _readyz_with(
        monkeypatch,
        local_ocr_enabled=True,
        local_ocr_engine="rapidocr",
        ocr_default_provider="azure_layout",
        azure_read_endpoint="https://ocr.internal.corp",
        ocr_service_trust_boundary="on_premises",
    )

    assert body["ocr"]["configured_providers"] == ["rapidocr", "azure_layout", "azure_read"]
    listed = {p["name"]: p for p in body["ocr"]["providers"]}
    assert [name for name, p in listed.items() if p["default"]] == ["azure_layout"]
    # Both Azure rows name their endpoint, so a picker can say where each one reads.
    assert listed["azure_layout"]["endpoint"] == "ocr.internal.corp"
    assert listed["azure_read"]["endpoint"] == "ocr.internal.corp"
    # The Read-vs-Layout accuracy fact stays on the label at the point of choice.
    assert listed["azure_read"]["structure"] == "lines"
    assert listed["azure_layout"]["structure"] == "roles"
    assert "no paragraph roles" in listed["azure_read"]["summary"]
