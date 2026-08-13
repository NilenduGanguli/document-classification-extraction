"""The comparison surface, and the honest emptiness behind it.

Two things are under test, and the second one is the more important:

1. ``/api/v1/classify/compare`` reports both decision trails and **adjudicates nothing**.
2. The second-avenue registry is **empty**, and everything downstream says so out loud —
   ``/readyz`` publishes availability *and coverage*, the compare endpoint distinguishes
   "there is no second avenue" from "the second avenue abstained", and naming a retired
   method returns the measurement that retired it rather than a bare validation error.

Point 2 is the deliverable. Four visual methods were built and measured against the
158-document real corpus; none reached 95% precision-when-answered at any threshold; the best
end-to-end result was 0.080 against a lexical cascade at 0.983. The tests below are how that
finding stays true in code — a future commit that quietly adds an avenue to
:data:`dce.visual.AVENUES` without the measurement has to delete
:func:`test_no_avenue_ships_without_clearing_the_bar` to do it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_api import ABSTAINED, build_app

from dce import visual
from dce.config import Settings
from dce.models import UNKNOWN, Classification
from dce.visual.compare import (
    AGREE,
    BOTH_ABSTAINED,
    DISAGREE,
    ONE_ABSTAINED,
    SINGLE_AVENUE,
    compare_classifications,
)

DOC = {"doc_id": "d1", "text": "INCOME TAX DEPARTMENT\nPermanent Account Number"}


# ---------------------------------------------------------------------------
# The registry is empty — and that is the finding, not an oversight
# ---------------------------------------------------------------------------
def test_no_avenue_ships_without_clearing_the_bar() -> None:
    """No second avenue is installable, because none cleared 95% precision.

    If you are here because this test failed after you added an avenue: the bar is precision
    >= 0.95 on the documents the avenue ANSWERS, measured on genuinely distinct files —
    never on synthetically degraded copies of the same file, which reported AUC 0.9992 for a
    method whose real-pair AUC was 0.568. Coverage is free to be small. Precision is not
    free to be smaller.
    """
    assert visual.AVENUES == {}


def test_every_retired_method_records_why_it_died() -> None:
    """A retired method carries its measurement, not just its name.

    The mechanism is the part worth keeping: the next person to propose "just match the
    layout" should meet the numbers rather than repeat the week.
    """
    assert set(visual.RETIRED) == {
        "sift_homography",
        "structure_skeleton",
        "layout_signature",
        "emblem_match",
    }
    for method in visual.RETIRED.values():
        assert method.best_precision < 0.95, method.method_id
        assert method.mechanism.strip()
        assert method.retired_on


def test_resolve_none_is_not_an_error() -> None:
    """The default is "no second avenue", and that is a clean state, not a problem."""
    avenue, problem = visual.resolve_avenue(Settings())
    assert avenue is None
    assert problem == ""


def test_configuring_a_retired_method_returns_the_measurement() -> None:
    """Naming a killed method reports why it was killed.

    An operator who sets ``visual_method=layout_signature`` because it sounded plausible
    gets the number that ended it, in the response, at the moment they ask.
    """
    avenue, problem = visual.resolve_avenue(Settings(visual_method="layout_signature"))
    assert avenue is None
    assert "0.080" in problem
    assert "retired" in problem
    assert "0.95" in problem
    # And the mechanism, which is the durable part.
    assert "INVERTS" in problem or "inverts" in problem


def test_configuring_an_unknown_method_says_the_registry_is_empty() -> None:
    avenue, problem = visual.resolve_avenue(Settings(visual_method="my_great_idea"))
    assert avenue is None
    assert "not a known method" in problem
    assert "empty" in problem


def test_status_reports_zero_coverage_not_silence() -> None:
    """Coverage is published even when it is zero — especially when it is zero."""
    status = visual.avenue_status(Settings(), doctypes_total=182)
    assert status.available is False
    assert status.method == ""
    assert status.templates == 0
    assert status.doctypes_covered == 0
    assert status.doctypes_total == 182
    assert status.coverage == 0.0
    assert status.installable == ()
    assert len(status.retired) == 4
    assert "95%" in status.summary


def test_status_never_divides_by_a_zero_registry() -> None:
    status = visual.avenue_status(Settings(), doctypes_total=0)
    assert status.doctypes_total == visual.REGISTRY_SIZE_HINT
    assert status.coverage == 0.0


# ---------------------------------------------------------------------------
# The comparison primitive adjudicates nothing
# ---------------------------------------------------------------------------
def _answer(doctype_id: str) -> Classification:
    return Classification(doctype_id=doctype_id, confidence=0.9)


def _abstain(reason: str = "coverage 0.10 < 0.20") -> Classification:
    return Classification(doctype_id=UNKNOWN, abstained=True, reason=reason)


def test_agreement_is_not_a_correctness_claim() -> None:
    verdict = compare_classifications(_answer("us_w9"), _answer("us_w9"), second_method="v")
    assert verdict.verdict == AGREE
    assert verdict.same_doctype is True
    assert verdict.answered == 2
    # The detail must refuse the inference a reader wants to make.
    assert "not evidence of correctness" in verdict.detail


def test_disagreement_nominates_no_winner() -> None:
    verdict = compare_classifications(_answer("us_w9"), _answer("us_w8ben"), second_method="v")
    assert verdict.verdict == DISAGREE
    assert verdict.same_doctype is False
    assert verdict.answered == 2
    assert "us_w9" in verdict.detail and "us_w8ben" in verdict.detail
    assert "Nothing is adjudicated" in verdict.detail
    # No field names a preferred avenue.
    assert not hasattr(verdict, "winner")
    assert not hasattr(verdict, "fused")


def test_an_abstention_is_silence_not_assent() -> None:
    verdict = compare_classifications(_answer("us_w9"), _abstain(), second_method="v")
    assert verdict.verdict == ONE_ABSTAINED
    assert verdict.answered == 1
    assert "silence, not assent" in verdict.detail
    # The abstaining avenue's own reason is carried through, not summarised away.
    assert "coverage 0.10 < 0.20" in verdict.detail


def test_both_abstaining_is_the_designed_outcome() -> None:
    verdict = compare_classifications(_abstain(), _abstain(), second_method="v")
    assert verdict.verdict == BOTH_ABSTAINED
    assert verdict.answered == 0
    assert "not a failure" in verdict.detail


def test_absent_avenue_is_distinct_from_an_abstaining_one() -> None:
    """An avenue that does not exist did not decline to answer.

    Collapsing the two would let an empty registry read as a considered refusal, which is
    exactly the misreading this whole exercise exists to prevent.
    """
    verdict = compare_classifications(_answer("us_w9"), None, second_problem="nothing installed")
    assert verdict.verdict == SINGLE_AVENUE
    assert verdict.verdict != ONE_ABSTAINED
    assert verdict.answered == 1
    assert "nothing installed" in verdict.detail
    assert "not a second opinion" in verdict.detail


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------
@pytest.fixture
def client() -> TestClient:
    return build_app()[0]


def test_compare_returns_the_lexical_trail_in_full(client: TestClient) -> None:
    body = client.post("/api/v1/classify/compare", json=DOC).json()
    assert body["lexical"]["ran"] is True
    lexical = body["lexical"]["classification"]
    # Same shape as /classify, field for field — the console and corpus harness read both
    # with no special-casing.
    plain = client.post("/api/v1/classify", json=DOC).json()
    for key in ("doctype_id", "confidence", "abstained", "reason", "runners_up", "evidence"):
        assert lexical[key] == plain[key], key


def test_compare_reports_single_avenue_and_says_why(client: TestClient) -> None:
    body = client.post("/api/v1/classify/compare", json=DOC).json()
    assert body["verdict"] == "single_avenue"
    assert body["answered"] == 1
    assert body["second"]["ran"] is False
    assert body["second"]["classification"] is None
    assert "95% precision bar" in body["second"]["detail"]


def test_compare_carries_the_coverage_block(client: TestClient) -> None:
    """A run of this endpoint is self-describing when it is read back out of a log."""
    block = client.post("/api/v1/classify/compare", json=DOC).json()["second_avenue"]
    assert block["available"] is False
    assert block["coverage"] == 0.0
    assert block["installable"] == []
    assert len(block["retired"]) == 4


def test_compare_adjudicates_nothing_in_its_response_shape(client: TestClient) -> None:
    """There is no fused answer on the wire, and adding one must be a deliberate act."""
    body = client.post("/api/v1/classify/compare", json=DOC).json()
    for forbidden in ("winner", "fused", "final", "doctype_id", "confidence", "decision"):
        assert forbidden not in body, forbidden


def test_compare_abstention_is_reported_not_repaired() -> None:
    """An abstaining cascade stays abstained. Compare has no way to rescue it."""
    client = build_app(classification=ABSTAINED)[0]
    body = client.post("/api/v1/classify/compare", json=DOC).json()
    assert body["lexical"]["classification"]["abstained"] is True
    assert body["answered"] == 0
    assert body["verdict"] == "single_avenue"


def test_compare_classifies_once() -> None:
    """The cascade runs exactly once per call — this is a comparison, not a re-run."""
    compare_client, classifier, _ = build_app()
    compare_client.post("/api/v1/classify/compare", json=DOC)
    assert len(classifier.views) == 1


def test_compare_honours_the_api_key() -> None:
    client = build_app(api_key="secret")[0]
    assert client.post("/api/v1/classify/compare", json=DOC).status_code == 401
    assert (
        client.post(
            "/api/v1/classify/compare", json=DOC, headers={"X-API-Key": "secret"}
        ).status_code
        == 200
    )


def test_compare_rejects_an_empty_document(client: TestClient) -> None:
    assert client.post("/api/v1/classify/compare", json={"doc_id": "x"}).status_code == 400


# ---------------------------------------------------------------------------
# /readyz
# ---------------------------------------------------------------------------
def test_readyz_publishes_the_second_avenue_and_its_coverage(client: TestClient) -> None:
    """An operator must not discover the coverage gap by using the endpoint."""
    body = client.get("/readyz").json()
    block = body["second_avenue"]
    assert block["available"] is False
    assert block["method"] == ""
    assert block["templates"] == 0
    assert block["doctypes_covered"] == 0
    # Denominator is the LIVE registry, not a constant, so a 121-doctype deployment is not
    # told a fraction of 182.
    assert block["doctypes_total"] == body["registry"]["doctypes"]
    assert block["coverage"] == 0.0
    assert block["problem"] == ""
    assert "none reached the 95% precision bar" in block["summary"]


def test_readyz_stays_ready_with_no_second_avenue(client: TestClient) -> None:
    """There being no second avenue is the state of the art, not an outage."""
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert "second_avenue" not in response.json()["degraded"]


def test_readyz_degrades_when_an_avenue_was_asked_for_and_cannot_be_had() -> None:
    """Asking for an avenue that does not exist is degraded: somebody is expecting a second
    opinion that will never arrive. Still not a 503 — classification works."""
    client = build_app()[0]
    client.app.state.settings = Settings(visual_method="emblem_match")
    body = client.get("/readyz").json()
    assert body["second_avenue"]["problem"]
    assert "0.000" in body["second_avenue"]["problem"]
    assert "second_avenue" in body["degraded"]
