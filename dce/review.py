"""T5 — the human-in-the-loop review queue. The tier that makes the other four safe.

Every automated tier is allowed to decline. L4 abstains to ``unknown``; a required field can
come back empty; a value can arrive below the confidence floor or with a validator complaining.
All of those end in the same place: a person looks at it. This module is that place's data
model and its state machine — and nothing else.

**Storage-agnostic on purpose.** ``docs/DESIGN.md`` §12 says the service is stateless and
whoever owns the queue owns its storage. So :class:`ReviewQueue` is a
:class:`~typing.Protocol`, and two implementations ship: :class:`InMemoryReviewQueue` (tests, a
single process) and :class:`JsonFileReviewQueue` (a small deployment, an on-disk file an
operator can read with ``cat``). A team with Postgres implements the same five methods and
loses nothing here.

**The transitions are the point.** ``pending`` → ``approved`` | ``rejected`` | ``corrected``,
decided once, with the deciding reviewer and the timestamp recorded on the item. A review queue
that cannot say *who* accepted a value and *when* is a spreadsheet, not a control.

**Blind double entry, where it actually matters.** A field that is both PII and backed by a real
checksum — an Aadhaar number, a CURP, a SIN — takes **two independent approvals** before it is
accepted, and a *correction* to one must be typed twice, by different people, matching. That is
the classic four-eyes control for keyed identifiers, and it is enforced in :func:`approve` and
:func:`correct` rather than left to a UI: a control that lives only in the frontend is a
suggestion. Rejection stays a one-person decision, because it is the safe direction — sending
something back for another look never puts bad data into a KYC record.
"""
from __future__ import annotations

import enum
import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from dce.models import UNKNOWN, ExtractedField, ExtractionResult, FieldSpec, Quad

__all__ = [
    "DOCUMENT_FIELD",
    "REASON_ABSTAINED",
    "REASON_BELOW_THRESHOLD",
    "REASON_MISSING_REQUIRED",
    "REASON_VALIDATOR_ERROR",
    "InMemoryReviewQueue",
    "JsonFileReviewQueue",
    "ReviewError",
    "ReviewItem",
    "ReviewNotFound",
    "ReviewQueue",
    "ReviewStatus",
    "approve",
    "correct",
    "enqueue_from_result",
    "pending_items",
    "queue_from_settings",
    "reject",
    "requires_double_entry",
]

logger = logging.getLogger(__name__)

#: ``field_name`` for an item that is about the whole document rather than one field — the
#: shape an abstained classification takes in the queue.
DOCUMENT_FIELD = "__document__"

REASON_ABSTAINED = "classification_abstained"
REASON_MISSING_REQUIRED = "missing_required"
REASON_BELOW_THRESHOLD = "below_confidence_threshold"
REASON_VALIDATOR_ERROR = "validator_error"

_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class ReviewStatus(enum.StrEnum):
    """Where an item is in its life. Terminal states are everything but ``pending``."""

    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    corrected = "corrected"


class ReviewError(ValueError):
    """An illegal transition: a re-decision, or a broken double-entry rule.

    Raised rather than returned. Every one of these means the caller's model of the queue is
    wrong, and quietly no-oping would let a UI report "approved" for something that is not.

    A :class:`ValueError` on purpose: the argument was refused, and an HTTP layer maps that to
    ``409 Conflict`` without having to import this module's exception hierarchy.
    """


class ReviewNotFound(ReviewError, LookupError):
    """The queue has never heard of this item.

    Also a :class:`LookupError`, so it reads as ``404`` to a router and as a ``ReviewError`` to
    anything that just wants to know the operation failed.
    """


class ReviewItem(BaseModel):
    """One thing a human has to look at.

    Attributes:
        id: Stable within a document — ``"<doc_id>:<field_name>"`` — so re-processing the same
            document does not spawn a second copy of a decision somebody already made.
        doc_id: The document this came from.
        doctype_id: Accepted doctype, or ``unknown`` for an abstained document.
        field_name: The field under review, or :data:`DOCUMENT_FIELD`.
        value: What was extracted, if anything.
        confidence: The extractor's confidence in it.
        reason: Why this is in the queue, machine-readable prefix first.
        page: 1-based page for the review UI.
        bbox: Region on that page, so the reviewer is shown the pixels.
        created_at: When it was enqueued (UTC).
        status: :class:`ReviewStatus`.
        reviewer: Who made the final decision.
        corrected_value: The human's replacement value.
        decided_at: When it reached a terminal state (UTC).
        approvals: Reviewers who have approved so far — the double-entry ledger. Modelled as
            data rather than a counter so "who signed this off" survives in the record.
        required_approvals: How many independent approvals this item needs. Stored on the item
            rather than recomputed, because the reason for it (a PII checksum field) comes from
            a :class:`~dce.models.FieldSpec` the queue does not otherwise hold.
        pii: Whether the value is personal data — a UI must mask it (UIDAI requires it for
            Aadhaar) and a log must not carry it.
        decision_note: Free text from the reviewer.
    """

    id: str
    doc_id: str = ""
    doctype_id: str = UNKNOWN
    field_name: str = DOCUMENT_FIELD
    value: str | None = None
    confidence: float = 0.0
    reason: str = ""
    page: int | None = None
    bbox: Quad | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: ReviewStatus = ReviewStatus.pending
    reviewer: str = ""
    corrected_value: str | None = None
    decided_at: datetime | None = None
    approvals: list[str] = Field(default_factory=list)
    required_approvals: int = 1
    pii: bool = False
    decision_note: str = ""

    @property
    def is_pending(self) -> bool:
        return self.status is ReviewStatus.pending

    @property
    def is_double_entry(self) -> bool:
        """Whether this item needs more than one independent approval."""
        return self.required_approvals > 1

    @property
    def outstanding_approvals(self) -> int:
        """How many more approvals are needed before this can be accepted."""
        return max(0, self.required_approvals - len(self.approvals))

    @property
    def final_value(self) -> str | None:
        """The value that should be believed: the human's correction, else the extraction."""
        return self.corrected_value if self.corrected_value is not None else self.value


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
@runtime_checkable
class ReviewQueue(Protocol):
    """The five operations every backing store must provide.

    Deliberately tiny and free of transactions: the state machine lives in this module's
    functions, so a store is only ever asked to persist a whole item and hand it back.
    """

    def put(self, item: ReviewItem) -> ReviewItem:
        """Insert or replace an item, keyed by ``item.id``."""

    def get(self, item_id: str) -> ReviewItem | None:
        """Return an item, or ``None``."""

    def update(self, item: ReviewItem) -> ReviewItem:
        """Persist a changed item.

        Raises:
            ReviewNotFound: When the item is not in the queue.
        """

    def list(
        self,
        *,
        status: ReviewStatus | str | None = None,
        doc_id: str | None = None,
        doctype_id: str | None = None,
        limit: int = 100,
    ) -> list[ReviewItem]:
        """Items, oldest first, optionally filtered."""

    def depth(self) -> int:
        """How many items are still ``pending`` — the number the gauge reports."""


class ReviewOperations:
    """The state machine, offered as methods on the shipped queues.

    :class:`ReviewQueue` stays five storage operations, because that is all a *store* should
    have to implement. But a caller holding a queue almost always wants the transitions too, and
    making it import three module functions to use the object it was handed is friction for no
    gain. These are thin delegations to :func:`approve`, :func:`reject` and :func:`correct`; the
    rules — including double entry — live there, once, where they can be audited.
    """

    def approve(self, item_id: str, *, reviewer: str, note: str = "") -> ReviewItem:
        """Approve an item — see :func:`approve`."""
        return approve(self, item_id, reviewer=reviewer, note=note)  # type: ignore[arg-type]

    def reject(self, item_id: str, *, reviewer: str, note: str = "") -> ReviewItem:
        """Reject an item — see :func:`reject`."""
        return reject(self, item_id, reviewer=reviewer, note=note)  # type: ignore[arg-type]

    def correct(self, item_id: str, *, reviewer: str, value: str, note: str = "") -> ReviewItem:
        """Correct an item — see :func:`correct`."""
        return correct(self, item_id, reviewer=reviewer, value=value, note=note)  # type: ignore[arg-type]


class InMemoryReviewQueue(ReviewOperations):
    """A queue in a dict. Correct, fast, and gone when the process is.

    Right for tests and for a single-process deployment that hands items to a UI over the same
    API. Not right for anything that needs the queue to survive a restart — use
    :class:`JsonFileReviewQueue` or a real database for that.
    """

    def __init__(self, items: Iterable[ReviewItem] = ()) -> None:
        self._items: dict[str, ReviewItem] = {item.id: item for item in items}
        self._lock = threading.Lock()

    def put(self, item: ReviewItem) -> ReviewItem:
        """Insert or replace an item."""
        with self._lock:
            self._items[item.id] = item
        return item

    def get(self, item_id: str) -> ReviewItem | None:
        """Return an item, or ``None``."""
        with self._lock:
            found = self._items.get(item_id)
        return found.model_copy(deep=True) if found is not None else None

    def update(self, item: ReviewItem) -> ReviewItem:
        """Persist a changed item.

        Raises:
            ReviewNotFound: When the item was never enqueued.
        """
        with self._lock:
            if item.id not in self._items:
                raise ReviewNotFound(f"review item {item.id!r} is not in the queue")
            self._items[item.id] = item
        return item

    def list(
        self,
        *,
        status: ReviewStatus | str | None = None,
        doc_id: str | None = None,
        doctype_id: str | None = None,
        limit: int = 100,
    ) -> list[ReviewItem]:
        """Items, oldest first, optionally filtered by status and document."""
        with self._lock:
            items = list(self._items.values())
        return _filtered(
            items, status=status, doc_id=doc_id, doctype_id=doctype_id, limit=limit
        )

    def depth(self) -> int:
        """Pending items."""
        with self._lock:
            return sum(1 for item in self._items.values() if item.is_pending)


class JsonFileReviewQueue(ReviewOperations):
    """A queue in one JSON file, written atomically.

    The whole file is read per operation and rewritten per mutation. That is not a performance
    strategy, it is a durability one: a human queue is measured in hundreds of items, the file
    is legible to an operator with ``cat``, and every write lands via ``os.replace`` so a crash
    mid-write leaves the previous queue intact rather than a truncated one.

    Concurrency: safe across threads in this process. Two *processes* sharing one file will
    interleave writes — at that point the queue has outgrown a file, and the
    :class:`ReviewQueue` protocol exists so it can be swapped for a database without touching
    the state machine.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Any) -> JsonFileReviewQueue:
        """Build the queue from configuration.

        Reads ``review_queue_path`` and falls back to ``<data_dir>/review_queue.json``. Both are
        read with ``getattr`` defaults so this works against a settings object that has not
        grown the field yet.
        """
        configured = str(getattr(settings, "review_queue_path", "") or "")
        if configured:
            return cls(configured)
        data_dir = str(getattr(settings, "data_dir", "./data") or "./data")
        return cls(Path(data_dir) / "review_queue.json")

    def _load(self) -> dict[str, ReviewItem]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            payload = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise ReviewError(f"review queue at {self.path} is not valid JSON: {exc}") from exc
        items = [ReviewItem(**row) for row in payload if isinstance(row, dict)]
        return {item.id: item for item in items}

    def _save(self, items: dict[str, ReviewItem]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(items.values(), key=lambda i: (i.created_at, i.id))
        payload = "[" + ",\n".join(item.model_dump_json() for item in ordered) + "]"
        tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    def put(self, item: ReviewItem) -> ReviewItem:
        """Insert or replace an item."""
        with self._lock:
            items = self._load()
            items[item.id] = item
            self._save(items)
        return item

    def get(self, item_id: str) -> ReviewItem | None:
        """Return an item, or ``None``."""
        with self._lock:
            return self._load().get(item_id)

    def update(self, item: ReviewItem) -> ReviewItem:
        """Persist a changed item.

        Raises:
            ReviewNotFound: When the item was never enqueued.
        """
        with self._lock:
            items = self._load()
            if item.id not in items:
                raise ReviewNotFound(f"review item {item.id!r} is not in the queue")
            items[item.id] = item
            self._save(items)
        return item

    def list(
        self,
        *,
        status: ReviewStatus | str | None = None,
        doc_id: str | None = None,
        doctype_id: str | None = None,
        limit: int = 100,
    ) -> list[ReviewItem]:
        """Items, oldest first, optionally filtered by status and document."""
        with self._lock:
            items = list(self._load().values())
        return _filtered(
            items, status=status, doc_id=doc_id, doctype_id=doctype_id, limit=limit
        )

    def depth(self) -> int:
        """Pending items."""
        with self._lock:
            return sum(1 for item in self._load().values() if item.is_pending)


#: The process-wide in-memory queue. A module global on purpose: with ``review_queue_backend
#: = "memory"`` every request must land in the *same* queue, and a factory that returned a
#: fresh dict per call would give each request its own empty one and lose every item.
_MEMORY_QUEUE = InMemoryReviewQueue()


def queue_from_settings(settings: Any) -> ReviewQueue:
    """Build the queue this deployment configured.

    ``memory`` (the default) is honest for a single replica and loses everything on restart;
    ``file`` persists to ``review_queue_path``. Anything durable and shared is the deploying
    team's to own — see ``docs/DESIGN.md`` §12 — and implements :class:`ReviewQueue`.

    Args:
        settings: Read for ``review_queue_backend`` and ``review_queue_path``, both with
            ``getattr`` defaults.

    Returns:
        The queue. An unrecognised backend name falls back to memory with a warning rather
        than failing the request: losing the queue on restart is bad, refusing to process
        documents because of a typo in an env var is worse.
    """
    backend = str(getattr(settings, "review_queue_backend", "memory") or "memory").strip().lower()
    if backend == "file":
        return JsonFileReviewQueue.from_settings(settings)
    if backend != "memory":
        logger.warning(
            "unknown review_queue_backend %r; falling back to the in-memory queue", backend
        )
    return _MEMORY_QUEUE


def _as_status(value: ReviewStatus | str | None) -> ReviewStatus | object | None:
    """Coerce a status filter that may have arrived as a query-string.

    Returns:
        The status, ``None`` for "no filter", or :data:`_NO_MATCH` for a name no status has —
        which filters everything out. A typo must return nothing, not everything: "everything"
        in a review queue is a page of somebody else's PII.
    """
    if value is None or isinstance(value, ReviewStatus):
        return value
    try:
        return ReviewStatus(str(value).strip().lower())
    except ValueError:
        logger.warning("unknown review status filter %r; returning nothing", value)
        return _NO_MATCH


#: Sentinel for a status filter that cannot match anything.
_NO_MATCH = object()


def _filtered(
    items: list[ReviewItem],
    *,
    status: ReviewStatus | str | None,
    doc_id: str | None,
    doctype_id: str | None = None,
    limit: int,
) -> list[ReviewItem]:
    """Sort oldest-first and apply the filters both queues share."""
    wanted = _as_status(status)
    if wanted is _NO_MATCH:
        return []
    out = [
        item
        for item in items
        if (wanted is None or item.status is wanted)
        and (doc_id is None or item.doc_id == doc_id)
        and (doctype_id is None or item.doctype_id == doctype_id)
    ]
    out.sort(key=lambda i: (i.created_at, i.id))
    return out[: max(0, limit)] if limit else out


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------
def _item_id(doc_id: str, field_name: str) -> str:
    """A readable, stable id. Falls back to a random one when there is no document id."""
    if not doc_id:
        return f"anon-{uuid.uuid4().hex[:12]}:{field_name}"
    return f"{_ID_SAFE.sub('_', doc_id)[:80]}:{field_name}"


def _as_field_specs(source: Any) -> list[FieldSpec]:
    """Accept a list of field specs, or anything with a ``fields`` attribute holding them.

    Callers already hold a :class:`~dce.models.DocTypeSpec` (classification produced it) or a
    ``DocSchema`` (extraction used it); making them unpack ``.fields`` first would be a rule to
    remember for no benefit.
    """
    if source is None:
        return []
    if isinstance(source, FieldSpec):
        return [source]
    if not isinstance(source, list | tuple):
        source = getattr(source, "fields", None) or []
    return [item for item in source if isinstance(item, FieldSpec)]


def requires_double_entry(spec: FieldSpec | None) -> bool:
    """Whether a field needs two independent approvals.

    True for a field that is **both** PII **and** validated by a real check digit. That pair is
    not arbitrary: a checksummed identifier is the kind of value a typo silently corrupts into
    another *valid-looking* identifier belonging to somebody else, and it is the kind of value
    that ends up in a regulated record. Four eyes on those; one pair on everything else, because
    a control everybody is too busy to follow is not a control.

    Args:
        spec: The field's declaration, or ``None`` when the caller did not supply one.

    Returns:
        Whether double entry applies. ``None`` is ``False``: without the spec there is no way to
        know the validator is a checksum, and the correct response to that is to pass the spec,
        not to guess.
    """
    if spec is None or not spec.pii or not spec.validator:
        return False
    # Imported here rather than at module scope: reaching it through the ``dce.extract``
    # package pulls in the locators and the resolver, and a review queue has no use for either.
    from dce.extract.validate import verification_level

    return verification_level(spec.validator) == "checksum"


def enqueue_from_result(
    result: ExtractionResult,
    *,
    doc_id: str,
    doctype_id: str,
    queue: ReviewQueue | None = None,
    field_specs: Any = None,
    settings: Any = None,
) -> list[ReviewItem]:
    """Turn an extraction result into the items a human has to look at.

    One item per **missing required** field, one per field whose confidence is **below the
    accept threshold** or whose **validator complained**, and — for a document the cascade
    abstained on — exactly one item for the whole document, because there are no fields to
    itemise and "what is this?" is the only question worth asking.

    Args:
        result: The extraction result. For an abstained document this is the empty result
            ``/process`` returns.
        doc_id: The document id; also the stable half of every item id.
        doctype_id: The accepted doctype, or ``unknown``.
        queue: Where to put the items. **Optional** — with no queue the items are built and
            returned, which is what makes this usable by a caller whose storage is elsewhere.
            When a queue is given, ids that are already in it are left alone: re-processing a
            document must not resurrect a decision a human already made.
        field_specs: The doctype's :class:`~dce.models.FieldSpec` list (or the
            ``DocTypeSpec``/``DocSchema`` holding it). Supplies ``pii`` and the validator, which
            is how :func:`requires_double_entry` is decided. Without it every item needs one
            approval.
        settings: Read for ``extract_accept_confidence`` (``getattr``, default 0.60).

    Returns:
        The items created, in a stable order. Empty when nothing needs a human.
    """
    threshold = float(getattr(settings, "extract_accept_confidence", 0.60) or 0.60)
    specs = {spec.name: spec for spec in _as_field_specs(field_specs)}
    accepted = (doctype_id or "").strip() or UNKNOWN
    items: list[ReviewItem] = []

    # An abstention is the caller's ``doctype_id``, not the result's: a caller that built an
    # ExtractionResult without stamping the doctype on it has a bug, not an abstained document.
    # The result is consulted only to catch the empty result ``/process`` returns on abstention.
    abstained = accepted.casefold() == UNKNOWN or (
        (result.doctype_id or UNKNOWN) == UNKNOWN and not result.fields
    )
    if abstained:
        items.append(
            ReviewItem(
                id=_item_id(doc_id, DOCUMENT_FIELD),
                doc_id=doc_id,
                doctype_id=UNKNOWN,
                field_name=DOCUMENT_FIELD,
                reason=(
                    f"{REASON_ABSTAINED}: the cascade could not place this document, so nothing "
                    "was extracted and nothing was sent to any remote tier. A human decides "
                    "what it is."
                ),
            )
        )
        return _store(items, queue)

    by_name = {f.name: f for f in result.fields}
    for name in result.missing_required:
        extracted = by_name.get(name)
        items.append(
            _item_for(
                doc_id=doc_id,
                doctype_id=accepted,
                name=name,
                extracted=extracted,
                spec=specs.get(name),
                reason=(
                    f"{REASON_MISSING_REQUIRED}: required field not found"
                    + (f" ({extracted.validator_error})" if extracted and
                       extracted.validator_error else "")
                ),
            )
        )

    missing = set(result.missing_required)
    for extracted in result.fields:
        if extracted.name in missing or not extracted.value:
            continue
        if extracted.confidence < threshold:
            reason = (
                f"{REASON_BELOW_THRESHOLD}: {extracted.confidence:.2f} < {threshold:.2f} "
                f"(locator={extracted.locator or 'none'}, {extracted.verification})"
            )
        elif extracted.validator_error:
            reason = f"{REASON_VALIDATOR_ERROR}: {extracted.validator_error}"
        else:
            continue
        items.append(
            _item_for(
                doc_id=doc_id,
                doctype_id=accepted,
                name=extracted.name,
                extracted=extracted,
                spec=specs.get(extracted.name),
                reason=reason,
            )
        )
    return _store(items, queue)


def _item_for(
    *,
    doc_id: str,
    doctype_id: str,
    name: str,
    extracted: ExtractedField | None,
    spec: FieldSpec | None,
    reason: str,
) -> ReviewItem:
    """Build one field-level item, carrying the provenance the reviewer needs."""
    double = requires_double_entry(spec)
    return ReviewItem(
        id=_item_id(doc_id, name),
        doc_id=doc_id,
        doctype_id=doctype_id,
        field_name=name,
        value=extracted.value if extracted else None,
        confidence=extracted.confidence if extracted else 0.0,
        reason=reason,
        page=extracted.page if extracted else None,
        bbox=extracted.bbox if extracted else None,
        required_approvals=2 if double else 1,
        pii=bool(spec.pii if spec is not None else (extracted.pii if extracted else False)),
    )


def _store(items: list[ReviewItem], queue: ReviewQueue | None) -> list[ReviewItem]:
    """Put the items in the queue, skipping ids that are already there."""
    if queue is None:
        return items
    stored: list[ReviewItem] = []
    for item in items:
        if queue.get(item.id) is not None:
            continue
        stored.append(queue.put(item))
    _report_depth(queue)
    return stored


def _report_depth(queue: ReviewQueue) -> None:
    """Publish the queue depth to the gauge ``docs/DESIGN.md`` §12 promised."""
    try:
        from dce import observability

        observability.set_needs_review_depth(queue.depth())
    except Exception:  # noqa: BLE001 - a metrics problem must never fail a review operation
        return


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------
def _pending(queue: ReviewQueue, item_id: str) -> ReviewItem:
    """Fetch an item that is still open, or explain why it is not."""
    item = queue.get(item_id)
    if item is None:
        raise ReviewNotFound(f"review item {item_id!r} is not in the queue")
    if not item.is_pending:
        raise ReviewError(
            f"review item {item_id!r} was already {item.status.value} by "
            f"{item.reviewer or 'someone'}; decisions are made once"
        )
    return item


def _reviewer(name: str) -> str:
    """Normalise and require a reviewer identity."""
    who = (name or "").strip()
    if not who:
        raise ReviewError("a review decision must name its reviewer")
    return who


def approve(queue: ReviewQueue, item_id: str, *, reviewer: str, note: str = "") -> ReviewItem:
    """Accept the extracted value.

    For an ordinary item one approval decides it. For a **double-entry** item (PII + checksum)
    the first approval is recorded and the item stays ``pending``; only a *different* reviewer's
    approval closes it. The same person approving twice is rejected outright — two signatures
    from one pair of eyes is the failure this control exists to prevent, and it is enforced here
    rather than in the UI.

    Args:
        queue: The backing store.
        item_id: Which item.
        reviewer: Who is approving.
        note: Free text kept with the item.

    Returns:
        The updated item — still ``pending`` when a second approval is outstanding.

    Raises:
        ReviewNotFound: When the queue does not have this item.
        ReviewError: Already-decided item, empty reviewer, or a repeat approval from a reviewer
            who has already signed this item.
    """
    item = _pending(queue, item_id)
    who = _reviewer(reviewer)
    if who in item.approvals:
        raise ReviewError(
            f"{who!r} has already approved {item_id!r}; this field needs "
            f"{item.required_approvals} INDEPENDENT approvals (PII + checksum), so the second "
            "one must come from somebody else"
        )
    approvals = [*item.approvals, who]
    changes: dict[str, Any] = {"approvals": approvals}
    if note:
        changes["decision_note"] = note
    if len(approvals) >= item.required_approvals:
        changes |= {
            "status": ReviewStatus.approved,
            "reviewer": who,
            "decided_at": datetime.now(UTC),
        }
    return queue.update(item.model_copy(update=changes))


def reject(queue: ReviewQueue, item_id: str, *, reviewer: str, note: str = "") -> ReviewItem:
    """Refuse the extracted value.

    One reviewer, always. Rejection is the safe direction: it puts nothing into a record, so
    requiring a second signature would only slow down the person trying to stop bad data.

    Args:
        queue: The backing store.
        item_id: Which item.
        reviewer: Who is rejecting.
        note: Why — shown to whoever picks the document up next.

    Returns:
        The rejected item.

    Raises:
        ReviewError: Unknown item, already-decided item, or an empty reviewer.
    """
    item = _pending(queue, item_id)
    who = _reviewer(reviewer)
    return queue.update(
        item.model_copy(
            update={
                "status": ReviewStatus.rejected,
                "reviewer": who,
                "decision_note": note,
                "decided_at": datetime.now(UTC),
            }
        )
    )


def correct(
    queue: ReviewQueue, item_id: str, *, reviewer: str, value: str, note: str = ""
) -> ReviewItem:
    """Replace the extracted value with what the document actually says.

    On an ordinary item this closes the item. On a **double-entry** item this is blind double
    entry in the literal sense: the first reviewer's value is recorded and the item stays
    ``pending``; a second, different reviewer must type the same value independently. A mismatch
    **discards both entries** and raises — the item goes back to square one rather than
    inheriting a value one of the two people got wrong, which is the entire reason keyed
    identifiers are entered twice.

    Args:
        queue: The backing store.
        item_id: Which item.
        reviewer: Who is entering the value.
        value: The corrected value.
        note: Free text kept with the item.

    Returns:
        The updated item — still ``pending`` when a second entry is outstanding.

    Raises:
        ReviewNotFound: When the queue does not have this item.
        ReviewError: Already-decided item, empty reviewer or value, a repeat entry from the same
            reviewer, or a double-entry mismatch.
    """
    item = _pending(queue, item_id)
    who = _reviewer(reviewer)
    entered = (value or "").strip()
    if not entered:
        raise ReviewError("a correction must carry a value; use reject() to discard the field")
    if who in item.approvals:
        raise ReviewError(
            f"{who!r} has already entered a value for {item_id!r}; blind double entry needs a "
            "second, independent pair of eyes"
        )
    if item.is_double_entry and not item.approvals:
        return queue.update(
            item.model_copy(
                update={"approvals": [who], "corrected_value": entered}
            )
        )
    if item.is_double_entry and item.corrected_value != entered:
        queue.update(
            item.model_copy(
                update={
                    "approvals": [],
                    "corrected_value": None,
                    "decision_note": f"double-entry mismatch; {who} disagreed with the first entry",
                }
            )
        )
        raise ReviewError(
            f"double-entry mismatch on {item_id!r}: the two independent entries differ, so both "
            "were discarded. Re-enter the value from the document."
        )
    changes: dict[str, Any] = {
        "approvals": [*item.approvals, who],
        "corrected_value": entered,
        "status": ReviewStatus.corrected,
        "reviewer": who,
        "decided_at": datetime.now(UTC),
    }
    if note:
        changes["decision_note"] = note
    return queue.update(item.model_copy(update=changes))


def pending_items(
    queue: ReviewQueue, *, doc_id: str | None = None, limit: int = 100
) -> Sequence[ReviewItem]:
    """The work list, oldest first."""
    return queue.list(status=ReviewStatus.pending, doc_id=doc_id, limit=limit)
