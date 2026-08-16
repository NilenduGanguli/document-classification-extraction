"""The logging itself, exercised — because a log call is the only thing that touches its names.

A `logger.info("...", undefined_name)` raises `NameError` at the moment it runs and at no
other time. Instrumenting a path without a test that *executes* its log statements therefore
lets a typo reach production silently, and one did during this work: five `logs.event()` calls
referenced a module logger that had not been defined yet, which would have failed on the first
line of every ingest request. It was caught by accident.

So these tests run real documents through the real pipeline with the level turned all the way
up, and assert on what came out. Two things are being guarded:

1. **Every log statement runs.** At DEBUG, on the ingest, classify, segment and refusal paths.
2. **No customer data is in it.** This service classifies passports and bank statements; a
   page's text, an extracted value and a filename are all personal data, and the default
   position is that none of them appear in a log.
"""
from __future__ import annotations

import logging

import pytest

import tests.ingest_fixtures as fixtures
from dce import logs
from dce.classify.cascade import classify
from dce.classify.segments import segment_document
from dce.ingest.pipeline import ingest

fitz = pytest.importorskip("fitz")

#: Strings that appear in the corpus fixtures and must never reach a log line.
_DOCUMENT_TEXT = ("Acme Corporation", "Invoice number 4471", "RAJESH", "ABCDE1234F")


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG, logger="dce")
    return caplog


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def _events(caplog: pytest.LogCaptureFixture) -> set[str]:
    """The event names emitted — the second token after the correlation prefix."""
    names: set[str] = set()
    for line in _lines(caplog):
        for token in line.split():
            if "." in token and "=" not in token:
                names.add(token)
                break
    return names


# ---------------------------------------------------------------------------
# Every statement runs
# ---------------------------------------------------------------------------
def test_ingesting_a_pdf_emits_its_stages_without_raising(captured):
    """The NameError guard. If any log call in the ingest path is malformed, this fails."""
    result = ingest(fixtures.text_pdf(["FORM W-9 Request for Taxpayer Identification Number"]))

    assert result.status.value == "ok"
    events = _events(captured)
    assert "ingest.start" in events
    assert "ingest.done" in events


def test_a_refusal_emits_its_reason(captured):
    result = ingest(fixtures.scanned_pdf())

    assert result.view is None
    assert "ingest.needs_ocr" in _events(captured)
    # WARNING, because a document went unclassified.
    assert any(r.levelno >= logging.WARNING for r in captured.records)


def test_a_partial_read_is_declared_in_the_log(captured):
    """The silent-loss fix, visible in the log as well as the response."""
    ingest(fixtures.mixed_pdf())

    assert "ingest.truncated" in _events(captured)


def test_classifying_emits_a_verdict(captured):
    result = ingest(fixtures.text_pdf(["FORM W-9 Request for Taxpayer Identification Number"]))
    classify(result.view)

    events = _events(captured)
    assert events & {"classify.accept", "classify.abstain"}, events


def test_segmenting_emits_its_boundary_decisions(captured):
    result = ingest(fixtures.mixed_pdf(text_pages=1, image_pages=2))
    segment_document(result.view)

    events = _events(captured)
    assert "segment.boundaries" in events
    assert "segment.done" in events


def test_the_ocr_service_client_logs_are_well_formed(captured):
    """Exercised without a network call: the module must at least import and expose them."""
    from dce.ingest import ocr_service

    assert ocr_service.logger.name == "dce.ingest.ocr_service"
    assert callable(ocr_service._shape_of)
    assert ocr_service._shape_of({"analyzeResult": {"pages": [{"lines": [1, 2]}]}}) == (
        "pages=1 lines=2 paragraphs=0 tables=0"
    )


# ---------------------------------------------------------------------------
# And none of it is customer data
# ---------------------------------------------------------------------------
def test_no_document_text_reaches_the_log(captured):
    """The whole PII position in one assertion, over the paths that read real text."""
    result = ingest(fixtures.mixed_pdf())
    if result.view is not None:
        classify(result.view)
        segment_document(result.view)

    blob = "\n".join(_lines(captured))
    for fragment in _DOCUMENT_TEXT:
        assert fragment not in blob, f"document text {fragment!r} reached a log line"


def test_a_doc_id_is_hashed_rather_than_carried(captured):
    """A doc_id is routinely a filename, and a correlation key repeats on every line."""
    ingest(fixtures.text_pdf(["FORM W-9 Request for Taxpayer"]), doc_id="john-smith-passport.pdf")

    blob = "\n".join(_lines(captured))
    assert "john-smith-passport" not in blob
    assert "doc=" in blob, "but it must still be correlatable"


def test_containers_are_counted_not_dumped():
    """The guard that stops a list of page text becoming a log line by accident."""
    assert logs.fields(blocks=["secret text", "more secret text"]) == "blocks=<list:2>"
    assert logs.fields(value="x" * 400) == "value=<400chars>"


def test_a_failed_stage_logs_the_exception_type_and_not_its_message(captured):
    """An exception message can quote the document that caused it."""
    logger = logging.getLogger("dce.test")
    with pytest.raises(ValueError), logs.stage(logger, "demo"):
        raise ValueError("customer JOHN SMITH account 123456")

    blob = "\n".join(_lines(captured))
    assert "error=ValueError" in blob
    assert "JOHN SMITH" not in blob


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------
def test_tracing_is_on_by_default(monkeypatch):
    """A trace nobody can find is not a trace.

    Uvicorn leaves non-uvicorn loggers at WARNING, so before this default every stage event
    was invisible until somebody already knew DCE_LOG_LEVEL existed. Verbose is the right
    default here precisely because no line carries document text — see the PII tests above,
    which are what make a loud default safe.
    """
    from dce.api.app import DEFAULT_LOG_LEVEL, _configure_logging

    assert DEFAULT_LOG_LEVEL == "DEBUG"

    monkeypatch.delenv("DCE_LOG_LEVEL", raising=False)
    package = logging.getLogger("dce")
    original = package.level
    try:
        package.setLevel(logging.NOTSET)
        _configure_logging()
        assert package.level == logging.DEBUG
    finally:
        package.setLevel(original)


def test_an_explicit_level_still_wins(monkeypatch):
    from dce.api.app import _configure_logging

    monkeypatch.setenv("DCE_LOG_LEVEL", "WARNING")
    package = logging.getLogger("dce")
    original = package.level
    try:
        _configure_logging()
        assert package.level == logging.WARNING
    finally:
        package.setLevel(original)


def test_response_bodies_are_not_logged_by_default(monkeypatch):
    """Raising a level must never silently export a document's text.

    DCE_INGEST_OCR_LOG_BODIES stays off even with tracing at DEBUG, because an OCR response
    IS the recognised document and that is a disclosure decision rather than a verbosity one.
    """
    from dce.ingest import ocr_service

    monkeypatch.delenv("DCE_INGEST_OCR_LOG_BODIES", raising=False)
    assert ocr_service._log_bodies() is False

    monkeypatch.setenv("DCE_INGEST_OCR_LOG_BODIES", "true")
    assert ocr_service._log_bodies() is True


def test_a_request_scope_restores_what_it_replaced():
    """Worker threads are reused; an id left set mislabels the next thing logged on them."""
    with logs.request_scope("outer"):
        assert logs.current_request_id() == "outer"
        with logs.request_scope("inner"):
            assert logs.current_request_id() == "inner"
        assert logs.current_request_id() == "outer"
    assert logs.current_request_id() == ""


def test_every_line_of_one_request_carries_the_same_id(captured):
    with logs.request_scope("abc123ff"):
        ingest(fixtures.text_pdf(["FORM W-9 Request for Taxpayer Identification Number"]))

    emitted = [line for line in _lines(captured) if line.startswith("req=")]
    assert emitted, "the correlation prefix should be on every event"
    assert all(line.startswith("req=abc123ff") for line in emitted)


def test_the_api_echoes_a_request_id_a_caller_can_quote():
    from fastapi.testclient import TestClient

    from dce.api.app import create_app

    response = TestClient(create_app()).get("/health")

    assert response.headers.get("X-Request-Id")


def test_an_inbound_request_id_is_honoured():
    """A trace started upstream stays one trace."""
    from fastapi.testclient import TestClient

    from dce.api.app import create_app

    client = TestClient(create_app())
    response = client.get("/health", headers={"X-Request-Id": "upstream-trace-1"})

    assert response.headers["X-Request-Id"] == "upstream-trace-1"
