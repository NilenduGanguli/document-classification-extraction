"""The tiered extraction path, end to end over the HTTP surface.

Four claims are load-bearing here, and each has a test whose failure means something specific:

* **A default deployment is unchanged.** With every paid tier off, ``/process`` returns what it
  returned before the tiers existed and resolves no tier module at all — the zero-egress build
  is what you get by doing nothing.
* **An abstention reaches no tier.** T2/T3/T4 leave the process. A document the cascade could
  not place must not reach one, and :func:`dce.api.routes.run_tier_cascade` raises rather than
  skipping if it is ever called with one.
* **A tier fills only what is still missing.** What a paid tier returns is *filtered* by the
  router, not trusted: it can never overwrite a checksum-verified local value.
* **What ran is visible.** ``tiers_used`` reports each tier's status, yield, latency and whether
  it cost money, because an operator has to be able to answer "what did this document cost".

Offline: the tiers are stubs installed at the port boundary, exactly where the real ones are
cached, so the routes under test are the production routes.
"""
from __future__ import annotations

import ast
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dce import review
from dce.api.app import create_app
from dce.api.routes import (
    PAID_TIERS,
    ClassifierPort,
    ExtractorPort,
    RegistryPort,
    ReviewPort,
    TierPort,
    run_tier_cascade,
)
from dce.config import Settings
from dce.models import (
    UNKNOWN,
    Anchor,
    Category,
    Classification,
    DocTypeSpec,
    ExtractedField,
    ExtractionResult,
    FieldSpec,
    LayoutView,
    Zone,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
T2, T3, T4 = PAID_TIERS

#: Enough of an Azure configuration to get past the "enabled but unusable" check. Nothing here
#: is ever dialled: every tier in this module is a stub installed at the port.
AZURE = {"azure_di_endpoint": "https://example.invalid", "azure_di_key": "not-a-real-key"}
LLM = {"llm_base_url": "https://example.invalid/v1", "llm_model": "test-model"}
#: Base64 of b"%PDF-1.7 not really a pdf" — T2/T3 analyse the file, so they need one.
CONTENT_B64 = "JVBERi0xLjcgbm90IHJlYWxseSBhIHBkZg=="


# ---------------------------------------------------------------------------
# Fixtures — registry, classifier and T1, stubbed at the port boundary
# ---------------------------------------------------------------------------
SIN_SPEC = DocTypeSpec(
    doctype_id="ca_sin_confirmation",
    label="Confirmation of SIN",
    country="CA",
    category=Category.identity,
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
        ),
        FieldSpec(
            name="holder_name",
            attribute_key="identity.full_name",
            type="name",
            required=True,
            pii=True,
        ),
    ],
)

ACCEPTED = Classification(
    doctype_id="ca_sin_confirmation",
    label="Confirmation of SIN",
    country="CA",
    confidence=0.94,
    margin=0.61,
    coverage=0.55,
)

ABSTAINED = Classification(
    doctype_id=UNKNOWN,
    confidence=0.41,
    margin=0.06,
    abstained=True,
    reason="probability 0.41 < 0.65 and margin 0.06 < 0.25",
)


def t1_result() -> ExtractionResult:
    """What the local resolver gets on its own: the id, checksum-verified; not the name."""
    return ExtractionResult(
        doctype_id="ca_sin_confirmation",
        fields=[
            ExtractedField(
                name="sin_number",
                attribute_key="id.sin",
                value="193-000-007",
                normalized="193-000-007",
                confidence=0.97,
                verification="checksum_verified",
                locator="kv",
                page=1,
                pii=True,
            ),
            ExtractedField(name="holder_name", confidence=0.0),
        ],
        missing_required=["holder_name"],
        needs_review=True,
    )


class StubRegistry:
    def __init__(self, specs: list[DocTypeSpec]) -> None:
        self._specs = {spec.doctype_id: spec for spec in specs}

    def all(self) -> list[DocTypeSpec]:
        return list(self._specs.values())

    def get(self, doctype_id: str) -> DocTypeSpec | None:
        return self._specs.get(doctype_id)


class StubClassifier:
    def __init__(self, result: Classification) -> None:
        self.result = result

    def __call__(self, view: LayoutView, **kwargs: Any) -> Classification:
        return self.result.model_copy(deep=True)


class StubExtractor:
    def __init__(self, result: ExtractionResult) -> None:
        self.result = result

    def __call__(self, view: LayoutView, spec: DocTypeSpec, **kwargs: Any) -> ExtractionResult:
        return self.result.model_copy(deep=True)


class SpyTier:
    """A stand-in for a paid tier, with the real modules' signature shape.

    ``dce.extract.azure_specialist.extract_with_specialist`` and its siblings are ``async`` and
    take the document *bytes*, so the stub is too: it is what proves the router's coroutine
    bridge and its argument matching work against the shape that actually shipped.
    """

    def __init__(self, fields: list[ExtractedField] | None = None, raises: bool = False) -> None:
        self.fields = fields or []
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, data: bytes, doctype_id: str, *, settings: Any
    ) -> list[ExtractedField]:
        self.calls.append({"data": data, "doctype_id": doctype_id})
        if self.raises:
            raise RuntimeError("azure said no")
        return [f.model_copy(deep=True) for f in self.fields]


def build_app(
    *,
    classification: Classification | None = None,
    extraction: ExtractionResult | None = None,
    specs: list[DocTypeSpec] | None = None,
    queue: Any = None,
    **settings_kwargs: Any,
) -> TestClient:
    """An app wired to stubs. ``TestClient`` is used without its context manager on purpose:
    entering it runs the lifespan, which would replace these stubs with the real engines."""
    app = create_app(Settings(_env_file=None, **settings_kwargs))
    app.state.registry = RegistryPort(StubRegistry(specs if specs is not None else [SIN_SPEC]))
    app.state.classifier = ClassifierPort(StubClassifier(classification or ACCEPTED))
    app.state.extractor = ExtractorPort(StubExtractor(extraction or t1_result()))
    # False is the router's "looked, found nothing" sentinel — it stops the real modules from
    # being resolved behind a test's back.
    app.state.tier_t2 = app.state.tier_t3 = app.state.tier_t4 = False
    app.state.review = queue if queue is not None else False
    return TestClient(app)


def real_queue() -> ReviewPort:
    """The real T5 state machine over a private in-memory store, so tests cannot leak into
    each other through ``dce.review``'s process-wide queue."""
    return ReviewPort(review, review.InMemoryReviewQueue())


def install(client: TestClient, tier: Any, fn: Any) -> SpyTier:
    """Install a stub at the port the router caches the real tier in."""
    port = TierPort(fn, tier)
    setattr(client.app.state, tier.state_attr, port)
    return fn


def process(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/api/v1/process", json={"doc_id": "doc-1", "text": "PAN", **body})
    assert response.status_code == 200, response.text
    return response.json()


def tiers_by_id(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["tier"]: entry for entry in body["tiers_used"]}


# ---------------------------------------------------------------------------
# A default deployment: every paid tier off
# ---------------------------------------------------------------------------
def test_with_every_tier_disabled_process_is_what_it_always_was() -> None:
    """The zero-egress build is the one you get by doing nothing."""
    client = build_app(queue=real_queue())

    body = process(client)

    assert body["classification"]["doctype_id"] == "ca_sin_confirmation"
    assert body["extraction"]["fields"][0]["value"] == "193-000-007"
    assert body["extraction"]["missing_required"] == ["holder_name"]
    assert body["needs_review"] is True
    assert body["timings"]["tiers_ms"] == 0

    used = tiers_by_id(body)
    assert set(used) == {"t1_local", "t5_review"}      # no paid tier appears at all
    assert used["t1_local"] == {
        "tier": "t1_local",
        "status": "ran",
        "fields_filled": 1,
        "fields": ["sin_number"],
        "ms": used["t1_local"]["ms"],
        "cost_bearing": False,
        "detail": "",
    }
    assert not any(entry["cost_bearing"] for entry in body["tiers_used"])


def test_a_disabled_tier_is_never_even_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not "resolved and skipped" — never resolved. The tier modules reach for an HTTP client,
    and the cheapest guarantee that the classification path has not imported one is that the
    import has not happened."""
    from dce.api import routes

    resolved: list[str] = []
    monkeypatch.setattr(routes, "load_tier_port", lambda tier: resolved.append(tier.tier))

    client = build_app()
    client.app.state.tier_t2 = client.app.state.tier_t3 = client.app.state.tier_t4 = None
    process(client)

    assert resolved == []


def test_the_router_does_not_import_a_tier_module_at_module_scope() -> None:
    """Enforced against the source, because "we always call it from inside the handler" is a
    habit and this is a guarantee."""
    tree = ast.parse((REPO_ROOT / "dce" / "api" / "routes.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in tree.body:  # module scope only — nested imports are the whole point
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    offenders = [
        name
        for name in imported
        if name.startswith("dce.review")
        or any(name.startswith(module) for tier in PAID_TIERS for module in tier.modules)
    ]
    assert offenders == [], f"tier modules imported at module scope: {offenders}"


# ---------------------------------------------------------------------------
# T2 — and the rule every paid tier is held to
# ---------------------------------------------------------------------------
def test_an_enabled_tier_fills_the_gap_and_says_what_it_cost() -> None:
    client = build_app(t2_enabled=True, queue=real_queue(), **AZURE)
    spy = install(
        client,
        T2,
        SpyTier([ExtractedField(name="holder_name", value="ANNA ERIKSSON", confidence=0.88)]),
    )

    body = process(client, content_base64=CONTENT_B64)

    filled = {f["name"]: f for f in body["extraction"]["fields"]}
    assert filled["holder_name"]["value"] == "ANNA ERIKSSON"
    # Provenance survives the merge: a reviewer must be able to see this came from a vendor.
    assert filled["holder_name"]["locator"] == "t2_azure_prebuilt"
    assert filled["holder_name"]["attribute_key"] == "identity.full_name"  # from the spec
    assert filled["holder_name"]["pii"] is True

    t2 = tiers_by_id(body)["t2_azure_prebuilt"]
    assert t2["status"] == "ran"
    assert t2["fields_filled"] == 1
    assert t2["fields"] == ["holder_name"]
    assert t2["cost_bearing"] is True
    assert body["timings"]["tiers_ms"] >= 0

    # The tier only ever saw the bytes and the accepted doctype.
    assert spy.calls[0]["doctype_id"] == "ca_sin_confirmation"
    assert spy.calls[0]["data"].startswith(b"%PDF")

    # Nothing is left for a human, so nothing was queued.
    assert body["extraction"]["missing_required"] == []
    assert body["needs_review"] is False
    assert body["review_ids"] == []


def test_a_tier_cannot_overwrite_a_value_that_was_already_found() -> None:
    """The filter is at the call site, not in the tier. A model asked for one field will
    volunteer another, and a fluent guess must never displace a checksum."""
    client = build_app(t2_enabled=True, queue=real_queue(), **AZURE)
    install(
        client,
        T2,
        SpyTier(
            [
                ExtractedField(name="sin_number", value="WRONG12345", confidence=0.99),
                ExtractedField(name="holder_name", value="ANNA ERIKSSON", confidence=0.6),
            ]
        ),
    )

    body = process(client, content_base64=CONTENT_B64)

    values = {f["name"]: f for f in body["extraction"]["fields"]}
    assert values["sin_number"]["value"] == "193-000-007"
    assert values["sin_number"]["verification"] == "checksum_verified"
    assert values["sin_number"]["locator"] == "kv"
    assert tiers_by_id(body)["t2_azure_prebuilt"]["fields"] == ["holder_name"]


def test_a_tier_that_needs_the_file_is_skipped_when_the_caller_sent_none() -> None:
    client = build_app(t2_enabled=True, queue=real_queue(), **AZURE)
    spy = install(client, T2, SpyTier([ExtractedField(name="holder_name", value="X")]))

    body = process(client)  # no content_base64

    t2 = tiers_by_id(body)["t2_azure_prebuilt"]
    assert t2["status"] == "skipped"
    assert "content_base64" in t2["detail"]
    assert t2["cost_bearing"] is False
    assert spy.calls == []


def test_a_tier_that_is_enabled_but_unconfigured_is_reported_not_fatal() -> None:
    """A secret that has not landed yet degrades extraction. It must not take down
    classification, which still works and is the part nobody is allowed to lose."""
    client = build_app(t2_enabled=True, queue=real_queue())  # no endpoint, no key

    body = process(client, content_base64=CONTENT_B64)

    t2 = tiers_by_id(body)["t2_azure_prebuilt"]
    assert t2["status"] == "misconfigured"
    assert "azure_di_endpoint" in t2["detail"]
    assert t2["cost_bearing"] is False
    assert body["extraction"]["fields"][0]["value"] == "193-000-007"


def test_a_tier_that_raises_costs_money_and_does_not_fail_the_request() -> None:
    client = build_app(t2_enabled=True, queue=real_queue(), **AZURE)
    install(client, T2, SpyTier(raises=True))

    body = process(client, content_base64=CONTENT_B64)

    t2 = tiers_by_id(body)["t2_azure_prebuilt"]
    assert t2["status"] == "error"
    assert "azure said no" in t2["detail"]
    # The call was made, so it is on the bill whether or not the answer was usable.
    assert t2["cost_bearing"] is True
    assert body["extraction"]["fields"][0]["value"] == "193-000-007"
    assert body["needs_review"] is True


def test_a_tier_whose_module_is_missing_is_reported_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shape of a build that switched a tier on without installing an HTTP client: the
    tier says so in the response, and the document still comes back with what T1 found."""
    from dce.api import routes

    absent = replace(T2, modules=("dce.extract.no_such_tier_module",))
    monkeypatch.setattr(routes, "PAID_TIERS", (absent,))
    client = build_app(t2_enabled=True, queue=real_queue(), **AZURE)
    client.app.state.tier_t2 = None  # let the router try to resolve it for real

    body = process(client, content_base64=CONTENT_B64)
    t2 = tiers_by_id(body)["t2_azure_prebuilt"]

    assert t2["status"] == "unavailable"
    assert "not importable" in t2["detail"]
    assert t2["cost_bearing"] is False
    assert body["extraction"]["fields"][0]["value"] == "193-000-007"


def test_tiers_escalate_and_stop_as_soon_as_nothing_is_missing() -> None:
    """T3 pays for nothing T2 already found — the escalation is the cost control."""
    client = build_app(t2_enabled=True, t3_enabled=True, queue=real_queue(), **AZURE)
    install(client, T2, SpyTier([ExtractedField(name="holder_name", value="ANNA ERIKSSON")]))
    t3_spy = install(client, T3, SpyTier([ExtractedField(name="holder_name", value="SOMEBODY")]))

    body = process(client, content_base64=CONTENT_B64)

    assert "t3_azure_query" not in tiers_by_id(body)
    assert t3_spy.calls == []


def test_the_ledger_reports_every_tier_in_escalation_order() -> None:
    client = build_app(
        t2_enabled=True, t3_enabled=True, t4_enabled=True, queue=real_queue(), **AZURE, **LLM
    )
    install(client, T2, SpyTier([]))
    install(client, T3, SpyTier([]))
    install(client, T4, SpyTier([ExtractedField(name="holder_name", value="ANNA ERIKSSON")]))

    body = process(client, content_base64=CONTENT_B64)

    assert [entry["tier"] for entry in body["tiers_used"]] == [
        "t1_local", "t2_azure_prebuilt", "t3_azure_query", "t4_llm",
    ]
    assert tiers_by_id(body)["t4_llm"]["fields_filled"] == 1


def test_spend_is_visible_in_the_metrics() -> None:
    client = build_app(t2_enabled=True, queue=real_queue(), **AZURE)
    install(client, T2, SpyTier([ExtractedField(name="holder_name", value="ANNA ERIKSSON")]))
    process(client, content_base64=CONTENT_B64)

    body = client.get("/metrics").text

    assert 'dce_extraction_tier_cost_calls_total{provider="azure",tier="t2_azure_prebuilt"}' in body
    assert 'dce_extraction_tier_fields_filled_total{tier="t2_azure_prebuilt"}' in body
    assert 'dce_extraction_tier_invocations_total{outcome="ran",tier="t2_azure_prebuilt"}' in body


# ---------------------------------------------------------------------------
# The half of the invariant that faces the other way
# ---------------------------------------------------------------------------
def test_an_abstention_reaches_no_tier_at_all() -> None:
    """The load-bearing test of this module. T2/T3/T4 leave the process; a document nobody has
    placed must not be in one of those calls."""
    client = build_app(
        classification=ABSTAINED,
        t2_enabled=True,
        t3_enabled=True,
        t4_enabled=True,
        queue=real_queue(),
        **AZURE,
        **LLM,
    )
    spies = [install(client, tier, SpyTier([ExtractedField(name="holder_name", value="X")]))
             for tier in PAID_TIERS]

    body = process(client, content_base64=CONTENT_B64)

    assert body["classification"]["abstained"] is True
    assert body["extraction"] is None
    assert body["needs_review"] is True
    assert [spy.calls for spy in spies] == [[], [], []]
    assert [entry["tier"] for entry in body["tiers_used"]] == ["t5_review"]


def test_the_cascade_refuses_an_unclassified_document_outright() -> None:
    """Not "skips it" — raises. Reaching the paid tiers with an abstention is a programming
    error, and the failure mode it produces is a disclosure, so it fails loudly."""
    client = build_app(t2_enabled=True, **AZURE)

    with pytest.raises(RuntimeError, match="unclassified document"):
        run_tier_cascade(
            _request_of(client),
            view=LayoutView(doc_id="doc-1"),
            spec=SIN_SPEC,
            classification=ABSTAINED,
            result=t1_result(),
            settings=Settings(_env_file=None, t2_enabled=True, **AZURE),
        )


def _request_of(client: TestClient) -> Any:
    """A minimal stand-in for the Request the cascade uses purely as a port cache."""

    class _Req:
        app = client.app

    return _Req()


# ---------------------------------------------------------------------------
# T5 — the review queue
# ---------------------------------------------------------------------------
def test_an_abstention_is_queued_for_a_human_as_one_item() -> None:
    queue = real_queue()
    client = build_app(classification=ABSTAINED, queue=queue)

    body = process(client)

    assert len(body["review_ids"]) == 1
    assert tiers_by_id(body)["t5_review"]["status"] == "queued"

    listed = client.get("/api/v1/review").json()
    assert listed["count"] == 1
    assert listed["depth"] == 1
    item = listed["items"][0]
    assert item["doctype_id"] == UNKNOWN
    assert item["status"] == "pending"
    assert "abstained" in item["reason"]


def test_an_unfinished_extraction_is_queued_per_field() -> None:
    client = build_app(queue=real_queue())

    body = process(client)

    assert body["review_ids"] == ["doc-1:holder_name"]
    listed = client.get("/api/v1/review").json()
    assert [item["field_name"] for item in listed["items"]] == ["holder_name"]


def test_review_filters_by_status_and_doctype() -> None:
    client = build_app(queue=real_queue())
    process(client)

    assert client.get("/api/v1/review?doctype=ca_sin_confirmation").json()["count"] == 1
    assert client.get("/api/v1/review?doctype=us_w9").json()["count"] == 0
    assert client.get("/api/v1/review?status=approved").json()["count"] == 0
    assert client.get("/api/v1/review?status=all").json()["count"] == 1
    assert client.get("/api/v1/review?status=nonsense").status_code == 400


def test_approve_closes_an_ordinary_item() -> None:
    client = build_app(queue=real_queue())
    item_id = process(client)["review_ids"][0]

    body = client.post(
        f"/api/v1/review/{item_id}/approve", json={"reviewer": "asha", "note": "matches the scan"}
    )

    assert body.status_code == 200
    assert body.json()["status"] == "approved"
    assert body.json()["reviewer"] == "asha"
    assert client.get("/api/v1/review").json()["count"] == 0     # off the pending list
    assert "dce_review_decisions_total" in client.get("/metrics").text


def test_reject_takes_one_reviewer_and_records_the_note() -> None:
    client = build_app(queue=real_queue())
    item_id = process(client)["review_ids"][0]

    body = client.post(
        f"/api/v1/review/{item_id}/reject", json={"reviewer": "bo", "note": "wrong person"}
    ).json()

    assert body["status"] == "rejected"
    assert body["decision_note"] == "wrong person"


def test_correct_replaces_the_value() -> None:
    client = build_app(queue=real_queue())
    item_id = process(client)["review_ids"][0]

    body = client.post(
        f"/api/v1/review/{item_id}/correct", json={"reviewer": "asha", "value": "ANNA ERIKSSON"}
    ).json()

    assert body["status"] == "corrected"
    assert body["corrected_value"] == "ANNA ERIKSSON"


def test_an_empty_correction_is_refused_rather_than_treated_as_an_approval() -> None:
    client = build_app(queue=real_queue())
    item_id = process(client)["review_ids"][0]

    response = client.post(f"/api/v1/review/{item_id}/correct", json={"reviewer": "asha"})

    assert response.status_code == 400
    assert "approval" in response.json()["detail"]


def test_a_decision_must_name_its_reviewer() -> None:
    client = build_app(queue=real_queue())
    item_id = process(client)["review_ids"][0]

    assert client.post(f"/api/v1/review/{item_id}/approve", json={}).status_code == 422
    assert (
        client.post(f"/api/v1/review/{item_id}/approve", json={"reviewer": ""}).status_code == 422
    )


def test_double_entry_needs_a_second_pair_of_eyes() -> None:
    """A PII field with a real check digit takes two independent signatures. The same person
    signing twice is the failure the control exists to prevent, and it is a 409 here."""
    queue = real_queue()
    client = build_app(queue=queue)
    item_id = process(client)["review_ids"][0]
    # Re-key the item as a double-entry one: that decision belongs to dce.review, and this test
    # is about what the HTTP surface does with it.
    item = queue.queue.get(item_id)
    queue.queue.update(item.model_copy(update={"required_approvals": 2}))

    first = client.post(f"/api/v1/review/{item_id}/approve", json={"reviewer": "asha"})
    assert first.status_code == 200
    assert first.json()["status"] == "pending"          # still open: one signature is not two

    repeat = client.post(f"/api/v1/review/{item_id}/approve", json={"reviewer": "asha"})
    assert repeat.status_code == 409
    assert "INDEPENDENT" in repeat.json()["detail"]

    second = client.post(f"/api/v1/review/{item_id}/approve", json={"reviewer": "bo"})
    assert second.status_code == 200
    assert second.json()["status"] == "approved"


def test_deciding_the_same_item_twice_is_a_conflict_not_a_silent_overwrite() -> None:
    client = build_app(queue=real_queue())
    item_id = process(client)["review_ids"][0]
    client.post(f"/api/v1/review/{item_id}/approve", json={"reviewer": "asha"})

    again = client.post(f"/api/v1/review/{item_id}/reject", json={"reviewer": "bo"})

    assert again.status_code == 409
    assert "decisions are made once" in again.json()["detail"]


def test_an_unknown_review_item_is_404() -> None:
    client = build_app(queue=real_queue())

    response = client.post("/api/v1/review/nope:field/approve", json={"reviewer": "asha"})

    assert response.status_code == 404
    assert "unknown review item" in response.json()["detail"]


def test_review_endpoints_say_503_when_no_queue_is_installed() -> None:
    """Not an empty list. "The queue is clear" and "there is no queue" are different facts and
    an operator must not have to guess which one they are looking at."""
    client = build_app()  # build_app installs no queue unless a test asks for one

    assert client.get("/api/v1/review").status_code == 503
    assert client.post("/api/v1/review/x/approve", json={"reviewer": "a"}).status_code == 503


def test_a_document_that_needs_no_human_queues_nothing() -> None:
    complete = ExtractionResult(
        doctype_id="ca_sin_confirmation",
        fields=[
            ExtractedField(name="sin_number", value="193-000-007", confidence=0.97, locator="kv"),
            ExtractedField(
                name="holder_name", value="ANNA ERIKSSON", confidence=0.91, locator="kv"
            ),
        ],
    )
    client = build_app(extraction=complete, queue=real_queue())

    body = process(client)

    assert body["needs_review"] is False
    assert body["review_ids"] == []
    assert "t5_review" not in tiers_by_id(body)
    assert client.get("/api/v1/review").json()["count"] == 0


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
def test_readyz_reports_the_tier_posture() -> None:
    client = build_app()

    tiers = {entry["tier"]: entry for entry in client.get("/readyz").json()["tiers"]}

    assert [t["enabled"] for t in tiers.values()] == [True, False, False, False, True]
    assert tiers["t1_local"]["cost_bearing"] is False
    assert all(tiers[name]["cost_bearing"] for name in ("t2_azure_prebuilt", "t3_azure_query"))


def test_readyz_is_degraded_but_still_serving_when_a_tier_is_half_configured() -> None:
    client = build_app(t4_enabled=True)

    body = client.get("/readyz").json()
    tiers = {entry["tier"]: entry for entry in body["tiers"]}

    assert body["ready"] is True                 # classification is unaffected
    assert "tiers" in body["degraded"]
    assert "llm_base_url" in tiers["t4_llm"]["problem"]


def test_content_base64_must_be_base64() -> None:
    client = build_app(t2_enabled=True, **AZURE)

    response = client.post(
        "/api/v1/process", json={"text": "PAN", "content_base64": "not base64 at all!"}
    )

    assert response.status_code == 400
    assert "base64" in response.json()["detail"]


if __name__ == "__main__":  # pragma: no cover - convenience
    sys.exit(pytest.main([__file__, "-q"]))
