"""T5, the human review queue — the tier that makes the other four safe.

Two things are worth reading closely. First, what actually reaches a human: a missing required
field, a value under the confidence floor, a value its validator complained about, and — for a
document the cascade declined to place — the document itself, once. Second, the double-entry
control: a field that is both PII and checksum-backed takes two *independent* signatures, and a
correction to one has to be typed twice, by different people, matching. Everything else takes
one, because a control nobody has time to follow is not a control.

All offline; the queue is a dict or a file.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:  # keeps the suite runnable without an installed package
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from dce.config import Settings  # noqa: E402
from dce.models import (  # noqa: E402
    UNKNOWN,
    DocTypeSpec,
    ExtractedField,
    ExtractionResult,
    FieldSpec,
)
from dce.review import (  # noqa: E402
    DOCUMENT_FIELD,
    InMemoryReviewQueue,
    JsonFileReviewQueue,
    ReviewError,
    ReviewItem,
    ReviewNotFound,
    ReviewQueue,
    ReviewStatus,
    approve,
    correct,
    enqueue_from_result,
    pending_items,
    queue_from_settings,
    reject,
    requires_double_entry,
)

SETTINGS = Settings(_env_file=None)  # extract_accept_confidence = 0.60
AADHAAR_BBOX = [0.1, 0.5, 0.6, 0.5, 0.6, 0.56, 0.1, 0.56]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def aadhaar_spec() -> FieldSpec:
    """PII **and** a real check digit — the pair that earns blind double entry."""
    return FieldSpec(
        name="aadhaar_number",
        attribute_key="id.aadhaar",
        type="id",
        required=True,
        pii=True,
        validator="verhoeff_aadhaar",
    )


def name_spec() -> FieldSpec:
    """PII, but no checksum to typo your way into somebody else's valid identifier."""
    return FieldSpec(name="full_name", type="name", required=True, pii=True, validator="name")


def address_spec() -> FieldSpec:
    return FieldSpec(name="address", type="address", validator="address")


def doctype() -> DocTypeSpec:
    return DocTypeSpec(
        doctype_id="in_aadhaar",
        label="Aadhaar Card",
        country="IN",
        fields=[aadhaar_spec(), name_spec(), address_spec()],
    )


def result_with(*fields: ExtractedField, missing: list[str] | None = None) -> ExtractionResult:
    return ExtractionResult(
        doctype_id="in_aadhaar",
        schema_version="1",
        fields=list(fields),
        missing_required=missing or [],
        needs_review=True,
    )


def queue_with(*items: ReviewItem) -> InMemoryReviewQueue:
    return InMemoryReviewQueue(items)


def one_pending(**overrides) -> tuple[InMemoryReviewQueue, ReviewItem]:
    """A queue holding a single pending item; overrides go straight onto the item."""
    fields = {
        "id": "doc-1:full_name",
        "doc_id": "doc-1",
        "doctype_id": "in_aadhaar",
        "field_name": "full_name",
        "value": "ANNA ERIKSSON",
        "confidence": 0.41,
        "reason": "below_confidence_threshold: 0.41 < 0.60",
    }
    item = ReviewItem(**{**fields, **overrides})
    return queue_with(item), item


# ---------------------------------------------------------------------------
# What reaches a human
# ---------------------------------------------------------------------------
def test_a_missing_required_field_is_enqueued():
    result = result_with(
        ExtractedField(name="aadhaar_number", value=None, validator_error="no_candidate_found"),
        ExtractedField(name="full_name", value="ANNA ERIKSSON", confidence=0.91),
        missing=["aadhaar_number"],
    )

    items = enqueue_from_result(
        result, doc_id="doc-1", doctype_id="in_aadhaar", field_specs=doctype(), settings=SETTINGS
    )

    assert [i.field_name for i in items] == ["aadhaar_number"]
    item = items[0]
    assert item.status is ReviewStatus.pending
    assert item.reason.startswith("missing_required")
    assert "no_candidate_found" in item.reason, "tell the reviewer what the extractor saw"
    assert item.value is None


def test_a_below_threshold_value_is_enqueued_with_its_provenance():
    result = result_with(
        ExtractedField(
            name="aadhaar_number",
            value="9999 9999 0011",
            confidence=0.42,
            verification="unverified",
            locator="regex",
            page=1,
            bbox=AADHAAR_BBOX,
            pii=True,
        )
    )

    (item,) = enqueue_from_result(
        result, doc_id="doc-1", doctype_id="in_aadhaar", field_specs=doctype(), settings=SETTINGS
    )

    assert item.reason.startswith("below_confidence_threshold")
    assert "0.42" in item.reason and "0.60" in item.reason
    assert item.page == 1
    assert item.bbox == AADHAAR_BBOX, "the reviewer gets shown the pixels, not just the string"
    assert item.pii is True


def test_a_soft_validator_failure_is_enqueued_even_at_high_confidence():
    """``ok=True`` with a note is exactly the state a human is for."""
    result = result_with(
        ExtractedField(
            name="address",
            value="12 Long Road",
            confidence=0.88,
            verification="format_valid",
            validator_error="ambiguous_day_month:assumed_DMY",
        )
    )

    (item,) = enqueue_from_result(
        result, doc_id="doc-1", doctype_id="in_aadhaar", field_specs=doctype(), settings=SETTINGS
    )

    assert item.reason.startswith("validator_error")


def test_a_clean_confident_field_is_not_enqueued():
    result = result_with(
        ExtractedField(
            name="aadhaar_number",
            value="9999 9999 0011",
            confidence=0.95,
            verification="checksum_verified",
        )
    )

    assert (
        enqueue_from_result(
            result,
            doc_id="doc-1",
            doctype_id="in_aadhaar",
            field_specs=doctype(),
            settings=SETTINGS,
        )
        == []
    )


def test_an_abstained_document_becomes_exactly_one_item():
    """No fields to itemise: the only question is 'what is this?', and it is asked once."""
    result = ExtractionResult(doctype_id=UNKNOWN, needs_review=True)

    items = enqueue_from_result(result, doc_id="doc-9", doctype_id=UNKNOWN, settings=SETTINGS)

    assert len(items) == 1
    item = items[0]
    assert item.field_name == DOCUMENT_FIELD
    assert item.doctype_id == UNKNOWN
    assert item.reason.startswith("classification_abstained")
    assert "remote tier" in item.reason


def test_enqueueing_into_a_queue_is_idempotent():
    """Re-processing a document must not resurrect a decision somebody already made."""
    queue = InMemoryReviewQueue()
    result = result_with(
        ExtractedField(name="aadhaar_number", value=None), missing=["aadhaar_number"]
    )
    kwargs = {
        "doc_id": "doc-1",
        "doctype_id": "in_aadhaar",
        "queue": queue,
        "field_specs": doctype(),
        "settings": SETTINGS,
    }

    first = enqueue_from_result(result, **kwargs)
    approve(queue, first[0].id, reviewer="alice")
    approve(queue, first[0].id, reviewer="bob")
    second = enqueue_from_result(result, **kwargs)

    assert second == []
    assert queue.get(first[0].id).status is ReviewStatus.approved
    assert len(queue.list()) == 1


def test_without_a_queue_the_items_are_just_returned():
    """Storage-agnostic: a caller whose queue is Postgres still gets the items to insert."""
    result = result_with(ExtractedField(name="full_name", value=None), missing=["full_name"])

    items = enqueue_from_result(result, doc_id="doc-1", doctype_id="in_aadhaar")

    assert len(items) == 1


def test_item_ids_are_stable_per_document_and_field():
    result = result_with(ExtractedField(name="full_name", value=None), missing=["full_name"])
    kwargs = {"doc_id": "doc-1", "doctype_id": "in_aadhaar"}

    assert (
        enqueue_from_result(result, **kwargs)[0].id
        == enqueue_from_result(result, **kwargs)[0].id
        == "doc-1:full_name"
    )


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------
def test_approve_closes_an_ordinary_item():
    queue, item = one_pending()

    decided = approve(queue, item.id, reviewer="alice")

    assert decided.status is ReviewStatus.approved
    assert decided.reviewer == "alice"
    assert decided.approvals == ["alice"]
    assert decided.decided_at is not None
    assert queue.depth() == 0


def test_reject_takes_one_reviewer_because_it_is_the_safe_direction():
    queue, item = one_pending(required_approvals=2)

    decided = reject(queue, item.id, reviewer="alice", note="not legible")

    assert decided.status is ReviewStatus.rejected
    assert decided.decision_note == "not legible"
    assert decided.decided_at is not None


def test_correct_replaces_the_value():
    queue, item = one_pending()

    decided = correct(queue, item.id, reviewer="alice", value="ANNA-MARIA ERIKSSON")

    assert decided.status is ReviewStatus.corrected
    assert decided.corrected_value == "ANNA-MARIA ERIKSSON"
    assert decided.final_value == "ANNA-MARIA ERIKSSON"
    assert decided.value == "ANNA ERIKSSON", "the original is kept; a correction is not an edit"


def test_a_decision_is_made_once():
    queue, item = one_pending()
    approve(queue, item.id, reviewer="alice")

    for act in (
        lambda: approve(queue, item.id, reviewer="bob"),
        lambda: reject(queue, item.id, reviewer="bob"),
        lambda: correct(queue, item.id, reviewer="bob", value="X"),
    ):
        with pytest.raises(ReviewError, match="already approved"):
            act()


def test_a_decision_must_name_its_reviewer():
    queue, item = one_pending()

    with pytest.raises(ReviewError, match="must name its reviewer"):
        approve(queue, item.id, reviewer="   ")


def test_an_unknown_item_is_an_error_not_a_silent_no_op():
    queue = InMemoryReviewQueue()

    with pytest.raises(ReviewError, match="not in the queue"):
        approve(queue, "doc-1:nope", reviewer="alice")


def test_a_correction_needs_a_value():
    queue, item = one_pending()

    with pytest.raises(ReviewError, match="use reject"):
        correct(queue, item.id, reviewer="alice", value="  ")


# ---------------------------------------------------------------------------
# Blind double entry
# ---------------------------------------------------------------------------
def test_double_entry_applies_to_pii_checksum_fields_only():
    assert requires_double_entry(aadhaar_spec()) is True, "PII + a real check digit"
    assert requires_double_entry(name_spec()) is False, "PII, but 'name' is not a checksum"
    assert requires_double_entry(address_spec()) is False
    assert requires_double_entry(FieldSpec(name="gstin", validator="gstin")) is False, (
        "a checksum on a company number is not personal data"
    )
    assert requires_double_entry(None) is False, "no spec means no guessing"


def test_a_pii_checksum_field_is_enqueued_needing_two_approvals():
    result = result_with(
        ExtractedField(name="aadhaar_number", value=None), missing=["aadhaar_number"]
    )

    (item,) = enqueue_from_result(
        result, doc_id="doc-1", doctype_id="in_aadhaar", field_specs=doctype(), settings=SETTINGS
    )

    assert item.required_approvals == 2
    assert item.is_double_entry is True
    assert item.outstanding_approvals == 2


def test_two_independent_approvals_are_required_and_the_first_does_not_decide():
    queue, item = one_pending(required_approvals=2, field_name="aadhaar_number", pii=True)

    first = approve(queue, item.id, reviewer="alice")
    assert first.status is ReviewStatus.pending, "one signature is not a decision"
    assert first.approvals == ["alice"]
    assert first.outstanding_approvals == 1
    assert queue.depth() == 1

    second = approve(queue, item.id, reviewer="bob")
    assert second.status is ReviewStatus.approved
    assert second.approvals == ["alice", "bob"]
    assert queue.depth() == 0


def test_the_same_person_cannot_sign_twice():
    """Two signatures from one pair of eyes is the failure the control exists to prevent."""
    queue, item = one_pending(required_approvals=2)
    approve(queue, item.id, reviewer="alice")

    with pytest.raises(ReviewError, match="INDEPENDENT"):
        approve(queue, item.id, reviewer="alice")

    assert queue.get(item.id).status is ReviewStatus.pending


def test_an_ordinary_field_still_needs_only_one_approval():
    queue, item = one_pending()

    assert approve(queue, item.id, reviewer="alice").status is ReviewStatus.approved


def test_a_matching_second_entry_closes_a_double_entry_correction():
    queue, item = one_pending(required_approvals=2, field_name="aadhaar_number", pii=True)

    first = correct(queue, item.id, reviewer="alice", value="9999 9999 0011")
    assert first.status is ReviewStatus.pending, "one keying is not a correction"
    assert first.corrected_value == "9999 9999 0011"

    second = correct(queue, item.id, reviewer="bob", value="9999 9999 0011")
    assert second.status is ReviewStatus.corrected
    assert second.approvals == ["alice", "bob"]


def test_a_mismatched_second_entry_discards_both_and_raises():
    """The whole point of keying twice: disagreement means neither entry is trusted."""
    queue, item = one_pending(required_approvals=2, field_name="aadhaar_number", pii=True)
    correct(queue, item.id, reviewer="alice", value="9999 9999 0011")

    with pytest.raises(ReviewError, match="double-entry mismatch"):
        correct(queue, item.id, reviewer="bob", value="9999 9999 0012")

    reset = queue.get(item.id)
    assert reset.status is ReviewStatus.pending
    assert reset.corrected_value is None, "neither entry survives a disagreement"
    assert reset.approvals == []
    assert "mismatch" in reset.decision_note


def test_the_same_person_cannot_key_a_double_entry_twice():
    queue, item = one_pending(required_approvals=2)
    correct(queue, item.id, reviewer="alice", value="9999 9999 0011")

    with pytest.raises(ReviewError, match="independent"):
        correct(queue, item.id, reviewer="alice", value="9999 9999 0011")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def test_both_queues_satisfy_the_protocol(tmp_path: Path):
    assert isinstance(InMemoryReviewQueue(), ReviewQueue)
    assert isinstance(JsonFileReviewQueue(tmp_path / "q.json"), ReviewQueue)


def test_the_json_queue_survives_a_new_process(tmp_path: Path):
    path = tmp_path / "review" / "queue.json"
    queue = JsonFileReviewQueue(path)
    result = result_with(
        ExtractedField(name="aadhaar_number", value=None), missing=["aadhaar_number"]
    )
    (item,) = enqueue_from_result(
        result,
        doc_id="doc-1",
        doctype_id="in_aadhaar",
        queue=queue,
        field_specs=doctype(),
        settings=SETTINGS,
    )
    approve(queue, item.id, reviewer="alice")

    reopened = JsonFileReviewQueue(path)  # a fresh instance, as a restart would be

    stored = reopened.get(item.id)
    assert stored is not None
    assert stored.approvals == ["alice"]
    assert stored.required_approvals == 2
    assert stored.status is ReviewStatus.pending
    assert reopened.depth() == 1


def test_the_json_queue_reads_its_path_from_settings(tmp_path: Path):
    path = tmp_path / "configured.json"
    queue = JsonFileReviewQueue.from_settings(
        Settings(_env_file=None, review_queue_path=str(path))
    )

    queue.put(ReviewItem(id="doc-1:x", doc_id="doc-1"))

    assert path.exists()
    assert queue.get("doc-1:x") is not None


def test_without_a_configured_path_the_queue_lands_under_data_dir(tmp_path: Path):
    """The container's one writable mount. Works against a settings object without the field."""
    queue = JsonFileReviewQueue.from_settings(SimpleNamespace(data_dir=str(tmp_path)))

    queue.put(ReviewItem(id="doc-1:x", doc_id="doc-1"))

    assert (tmp_path / "review_queue.json").exists()


def test_the_backend_switch_picks_the_queue(tmp_path: Path):
    path = tmp_path / "queue.json"
    file_queue = queue_from_settings(
        Settings(_env_file=None, review_queue_backend="file", review_queue_path=str(path))
    )
    assert isinstance(file_queue, JsonFileReviewQueue)

    memory = queue_from_settings(Settings(_env_file=None))
    assert isinstance(memory, InMemoryReviewQueue)
    assert memory is queue_from_settings(Settings(_env_file=None)), (
        "every request must land in the same in-memory queue, not a fresh empty one"
    )


def test_an_unknown_backend_degrades_to_memory_rather_than_failing():
    assert isinstance(
        queue_from_settings(SimpleNamespace(review_queue_backend="postgres")),
        InMemoryReviewQueue,
    )


def test_a_corrupt_queue_file_is_an_error_not_an_empty_queue(tmp_path: Path):
    path = tmp_path / "queue.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ReviewError, match="not valid JSON"):
        JsonFileReviewQueue(path).list()


def test_updating_something_that_was_never_enqueued_raises(tmp_path: Path):
    for queue in (InMemoryReviewQueue(), JsonFileReviewQueue(tmp_path / "q.json")):
        with pytest.raises(ReviewError, match="not in the queue"):
            queue.update(ReviewItem(id="ghost"))


def test_the_work_list_is_filtered_and_oldest_first():
    queue = InMemoryReviewQueue()
    for i in range(3):
        queue.put(ReviewItem(id=f"doc-1:f{i}", doc_id="doc-1", field_name=f"f{i}"))
    queue.put(ReviewItem(id="doc-2:f0", doc_id="doc-2", field_name="f0"))
    approve(queue, "doc-1:f1", reviewer="alice")

    work = pending_items(queue, doc_id="doc-1")

    assert [i.field_name for i in work] == ["f0", "f2"]
    assert len(queue.list(status=ReviewStatus.approved)) == 1
    assert len(queue.list(limit=2)) == 2


def test_the_in_memory_queue_hands_out_copies():
    """A caller mutating what it read must not silently rewrite the queue."""
    queue, item = one_pending()

    queue.get(item.id).value = "TAMPERED"

    assert queue.get(item.id).value == "ANNA ERIKSSON"


def test_a_status_filter_that_arrived_as_a_string_still_matches():
    """``?status=pending`` reaches the store as ``str``; the store compares enums."""
    queue, item = one_pending()

    assert [i.id for i in queue.list(status="pending")] == [item.id]
    assert queue.list(status="approved") == []


def test_an_unknown_status_filter_returns_nothing_not_everything():
    """The failure mode to avoid: a typo that hands back a page of somebody else's PII."""
    queue, _ = one_pending()

    assert queue.list(status="pendign") == []


def test_the_queues_also_expose_the_transitions():
    """A caller holding a queue should not have to import three functions to use it."""
    queue, item = one_pending()

    decided = queue.approve(item.id, reviewer="alice")

    assert decided.status is ReviewStatus.approved
    assert queue.list(status=ReviewStatus.approved, doctype_id="in_aadhaar")


# ---------------------------------------------------------------------------
# The contract the HTTP layer relies on
# ---------------------------------------------------------------------------
def test_a_refusal_is_a_value_error_and_a_missing_item_is_a_lookup_error():
    """So a router maps them to 409 and 404 without importing this module's exceptions."""
    queue, item = one_pending()
    approve(queue, item.id, reviewer="alice")

    with pytest.raises(ValueError):
        approve(queue, item.id, reviewer="bob")
    with pytest.raises(LookupError):
        approve(queue, "doc-1:ghost", reviewer="bob")
    assert issubclass(ReviewNotFound, ReviewError)


def test_the_http_layer_can_discover_and_drive_this_queue():
    """The wiring nobody notices is broken until a document needs a human.

    Deliberately tolerant about *how* the router finds the queue — that is the router's to
    change — but not about whether it can.
    """
    routes = pytest.importorskip("dce.api.routes")
    loader = getattr(routes, "load_review_port", None)
    if loader is None:
        pytest.skip("the router does not expose load_review_port")

    port = loader(Settings(_env_file=None))

    assert port is not None, "the API could not find dce.review; documents would have nowhere to go"
    assert port.usable() is True
