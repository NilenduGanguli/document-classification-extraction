"""Proof that ingestion — including OCR of an image — never leaves the process.

:mod:`tests.test_egress` proves the classification cascade opens no socket. Ingestion moved
the process boundary earlier: the service now accepts raw bytes, and one of the formats it
accepts (an image) genuinely *cannot* be classified without optical recognition. That makes
this the single most likely place for the invariant to be broken by accident, because the
tempting implementation — "just call Azure Read first" — is pre-classification egress on a
document nobody has identified yet.

So the pass condition here is the same as in that file and for the same reason: not "no
vendor SDK was imported" but "no connection was attempted, at any layer, by any dependency",
asserted by sabotaging :mod:`socket` and then doing the work anyway.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from dce.classify import classify  # noqa: E402
from dce.egress import EgressViolation, classification_scope, socket_tripwire  # noqa: E402
from dce.ingest import IngestSettings, IngestStatus, MediaType, ingest  # noqa: E402
from dce.ingest import pipeline as ingest_pipeline  # noqa: E402
from dce.ingest.limits import Deadline  # noqa: E402
from dce.ingest.ocr import OcrLine, OcrPage  # noqa: E402
from dce.models import LayoutView  # noqa: E402
from tests import ingest_fixtures as fixtures  # noqa: E402
from tests.test_egress import LOCKED_DOWN, specs  # noqa: E402

OFF = IngestSettings(_env_file=None, local_ocr_enabled=False)
ON = IngestSettings(_env_file=None, local_ocr_enabled=True, local_ocr_engine="rapidocr")


class InProcessOcr:
    """A stand-in for a real local engine: same protocol, no I/O of any kind.

    The engines this service ships (RapidOCR under ONNX Runtime, tesseract as a local
    subprocess) are not installed in CI, and installing a 200 MB model to assert "it did not
    open a socket" would test the model, not the plumbing. What the tripwire has to prove is
    that the *ingestion path* — detection, frame splitting, block building, the hand-off into
    the classifier — reaches the network nowhere. That is exactly what this exercises.
    """

    name = "in-process-fake"

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.calls = 0

    def recognize(self, image: bytes, *, page: int, deadline: Deadline) -> OcrPage:
        self.calls += 1
        assert image, "the engine must be handed real bytes"
        return OcrPage(
            page=page,
            width=1000.0,
            height=1400.0,
            lines=[OcrLine(text=line, page=page) for line in self._lines],
        )


@pytest.fixture
def local_ocr(monkeypatch: pytest.MonkeyPatch) -> InProcessOcr:
    """Install the fake engine as the deployment's configured local provider."""
    provider = InProcessOcr(
        [
            "PASSPORT",
            "UNITED STATES OF AMERICA",
            "Authority: DEPARTMENT OF STATE",
            "P<USAERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<",
        ]
    )
    monkeypatch.setattr(ingest_pipeline, "provider_or_none", lambda *a, **k: provider)
    return provider


def block_all_sockets(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace every socket entry point with one that raises. Returns the attempt log."""
    attempts: list[str] = []

    def refuse(name: str):
        def raiser(*args, **kwargs):
            attempts.append(name)
            raise AssertionError(f"ingestion attempted {name}: egress before classification")

        return raiser

    monkeypatch.setattr(socket, "socket", refuse("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", refuse("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("socket.getaddrinfo"))
    monkeypatch.setattr(socket, "gethostbyname", refuse("socket.gethostbyname"))
    return attempts


# ---------------------------------------------------------------------------
# THE test: an image goes through the whole path with the tripwire armed
# ---------------------------------------------------------------------------
def test_an_image_ingested_and_classified_opens_zero_sockets(
    monkeypatch: pytest.MonkeyPatch, local_ocr: InProcessOcr
):
    """Image bytes -> local OCR -> LayoutView -> classification, with sockets sabotaged."""
    attempts = block_all_sockets(monkeypatch)

    result = ingest(fixtures.png(1000, 1400), filename="passport.jpg", settings=ON)

    assert result.status is IngestStatus.ok
    assert result.media_type is MediaType.png
    assert result.text_source.value == "local_ocr"
    assert result.ocr_engine == "in-process-fake"
    assert local_ocr.calls == 1
    assert isinstance(result.view, LayoutView)

    classification = classify(result.view, specs(), settings=LOCKED_DOWN)
    assert classification.doctype_id in {"passport", "unknown"}
    assert attempts == []


def test_the_socket_tripwire_itself_stays_clean_through_ingestion(local_ocr: InProcessOcr):
    """The stronger form: :func:`dce.egress.socket_tripwire`, the audit utility, armed."""
    with socket_tripwire() as blocked:
        result = ingest(fixtures.multipage_tiff(pages=2), settings=ON)
        assert result.status is IngestStatus.ok
        classification = classify(result.view, specs(), settings=LOCKED_DOWN)
    assert blocked == []
    assert classification is not None


def test_every_native_format_is_pure_computation(monkeypatch: pytest.MonkeyPatch):
    """No format that carries its own text has any reason to touch the network."""
    attempts = block_all_sockets(monkeypatch)
    payloads = [
        fixtures.docx([("Title", "PASSPORT"), ("", "Authority DEPARTMENT OF STATE")]),
        fixtures.xlsx({"Sheet1": [["PASSPORT", "Authority"]]}),
        fixtures.pptx([("PASSPORT", ["Authority"])]),
        fixtures.odt([("h1", "PASSPORT"), ("p", "Authority")]),
        fixtures.msg("PASSPORT", "Authority: DEPARTMENT OF STATE"),
        b"<html><h1>PASSPORT</h1><p>Authority</p></html>",
        b"From: a@b.test\nTo: c@d.test\nSubject: PASSPORT\n\nAuthority\n",
        b"{\\rtf1\\ansi PASSPORT\\par Authority}",
        b"PASSPORT\nAuthority: DEPARTMENT OF STATE\n",
        b"field,value\nPASSPORT,Authority\n",
        fixtures.text_pdf(["PASSPORT", "Authority DEPARTMENT OF STATE", "United States"]),
    ]
    for payload in payloads:
        outcome = ingest(payload, settings=OFF)
        assert outcome.view is not None
        classify(outcome.view, specs(), settings=LOCKED_DOWN)
    assert attempts == []


# ---------------------------------------------------------------------------
# The default: refuse rather than recognise
# ---------------------------------------------------------------------------
def test_with_ocr_off_an_image_is_needs_ocr_and_no_engine_is_loaded(
    monkeypatch: pytest.MonkeyPatch
):
    def explode(*args, **kwargs):
        raise AssertionError("no OCR engine may be constructed when local OCR is off")

    monkeypatch.setattr(ingest_pipeline, "provider_or_none", explode)
    attempts = block_all_sockets(monkeypatch)

    result = ingest(fixtures.jpeg(), settings=OFF)
    assert result.status is IngestStatus.needs_ocr
    assert result.view is None
    assert result.ocr_available is False
    assert attempts == []


def test_a_request_cannot_switch_local_ocr_on(monkeypatch: pytest.MonkeyPatch):
    """``local_ocr=True`` asks; only the deployment can grant.

    Whether an unclassified customer document may be run through a recognition engine — and
    whether this deployment stands behind that engine's accuracy — are operator decisions. A
    caller flag that could raise them would make the default meaningless.
    """
    monkeypatch.setattr(
        ingest_pipeline,
        "provider_or_none",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("engine must not load")),
    )
    result = ingest(fixtures.png(), settings=OFF, local_ocr=True)
    assert result.status is IngestStatus.needs_ocr


def test_a_request_can_switch_local_ocr_off(local_ocr: InProcessOcr):
    """The other direction is allowed: a caller may always decline recognition."""
    result = ingest(fixtures.png(), settings=ON, local_ocr=False)
    assert result.status is IngestStatus.needs_ocr
    assert local_ocr.calls == 0
    # ``ocr_available`` is per-request, so it is False — and the reason distinguishes
    # "declined" from "switched off" and from "engine not installed".
    assert result.ocr_available is False
    assert "declined it" in result.reason


# ---------------------------------------------------------------------------
# Ingestion runs before the cascade, and must not be called from inside it
# ---------------------------------------------------------------------------
def test_ingestion_inside_a_classification_scope_still_performs_no_egress():
    """Belt and braces: even if a future caller nests it, nothing leaves.

    ``assert_no_egress`` would already refuse a network call made in this scope. The point
    here is that ingestion never gets that far, because it makes no such call at all.
    """
    with classification_scope(), socket_tripwire() as blocked:
        result = ingest(fixtures.docx([("Title", "PASSPORT")]), settings=OFF)
    assert result.view is not None
    assert blocked == []


def test_a_remote_ocr_engine_could_not_be_configured_by_name():
    """The engine registry is a closed allowlist, not a plugin hook."""
    from dce.ingest.errors import EngineUnavailable
    from dce.ingest.ocr import ENGINES, load_provider

    assert set(ENGINES) == {"rapidocr", "tesseract"}
    with pytest.raises(EngineUnavailable, match="unknown local OCR engine"):
        load_provider("azure-read")
    with pytest.raises(EngineUnavailable):
        load_provider("https://westeurope.api.cognitive.microsoft.com")


def test_egress_violation_is_still_what_a_network_call_would_raise():
    """Sanity: the guard ingestion relies on has not been weakened by this round."""
    from dce.egress import assert_no_egress

    with classification_scope(), pytest.raises(EgressViolation):
        assert_no_egress("ingest.hypothetical_cloud_ocr")
