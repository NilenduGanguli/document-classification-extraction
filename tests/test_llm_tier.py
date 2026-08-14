"""T4, the constrained LLM tier — and the two claims that make it publishable.

The tier's value is not that it can call a model; anything can call a model. It is that a
value the model returns either **provably occurs in the fragment we sent** or never reaches the
caller, and that the whole thing refuses to run at all for a document the cascade could not
place. Those are the two tests to read first:
:func:`test_an_ungrounded_value_is_discarded_not_downgraded` and
:func:`test_it_refuses_when_the_cascade_abstained`.

Every test here is offline. The one function that would touch the network is replaced with a
stub, and several tests assert it was never called at all.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:  # keeps the suite runnable without an installed package
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from dce.config import Settings  # noqa: E402
from dce.egress import (  # noqa: E402
    EgressViolation,
    assert_no_egress,
    classification_scope,
    in_classification_scope,
    post_classification_doctype,
    post_classification_scope,
)
from dce.extract import llm_field  # noqa: E402
from dce.models import UNKNOWN, FieldSpec, LayoutView, PageInfo, TextBlock, Zone  # noqa: E402

EIN_BBOX = [0.1, 0.4, 0.5, 0.4, 0.5, 0.45, 0.1, 0.45]
#: Luhn-valid synthetic Canadian SIN; the same number tests/test_validate.py pins.
SIN = "193 000 007"


@dataclass
class LlmSettings:
    """A settings stand-in.

    The tier reads every T4 field with a ``getattr`` default, so it works against a
    :class:`~dce.config.Settings` that has grown them and one that has not. Tests use this so
    they pin the tier's behaviour rather than the order two agents landed their changes in;
    :func:`test_the_shipped_settings_object_leaves_the_tier_off` covers the real object.
    """

    t4_enabled: bool = True
    llm_base_url: str = "http://llm.invalid/v1"
    llm_api_key: str = "test-key"
    llm_model: str = "test-model"
    llm_timeout_seconds: float = 5.0
    llm_max_window_chars: int = 4000


def ein_field() -> FieldSpec:
    return FieldSpec(
        name="ein_number",
        attribute_key="id.ein",
        type="id",
        required=True,
        pii=True,
        labels={"en": ["Employer identification number"]},
        pattern=r"\d{2}-\d{7}",
        validator="ein",
    )


def sin_field() -> FieldSpec:
    return FieldSpec(
        name="sin_number",
        attribute_key="id.sin",
        type="id",
        pii=True,
        labels={"en": ["Social Insurance Number"]},
        validator="sin_luhn",
    )


def dob_field() -> FieldSpec:
    return FieldSpec(name="date_of_birth", type="date", labels={"en": ["Date of Birth"]})


def two_page_view() -> LayoutView:
    """Page 1 carries the field and its label; page 2 is unrelated boilerplate."""
    return LayoutView(
        doc_id="doc-1",
        pages=[PageInfo(page=1, width=8.5, height=11.0), PageInfo(page=2, width=8.5, height=11.0)],
        blocks=[
            TextBlock(text="Request for Taxpayer Identification Number", zone=Zone.title, page=1),
            TextBlock(text="Employer identification number", zone=Zone.body, page=1),
            TextBlock(text="12-3456789", zone=Zone.body, page=1, bbox=EIN_BBOX),
            TextBlock(
                text="Terms and conditions of use apply to this booklet.",
                zone=Zone.body,
                page=2,
            ),
            TextBlock(text="Return to the nearest office if found.", zone=Zone.body, page=2),
        ],
    )


def answer_body(payload: dict) -> dict:
    """Wrap a field answer in an OpenAI-compatible response envelope."""
    import json

    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


def stub_endpoint(monkeypatch: pytest.MonkeyPatch, body: dict) -> list[dict]:
    """Replace the one function that would leave the process. Returns the captured payloads."""
    seen: list[dict] = []

    async def fake_post(payload, *, base_url, api_key, timeout):
        seen.append(payload)
        return body

    monkeypatch.setattr(llm_field, "_post_completion", fake_post)
    return seen


def forbid_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any call to the endpoint an outright test failure."""

    async def refuse(payload, *, base_url, api_key, timeout):
        raise AssertionError("T4 called the endpoint when it must not have")

    monkeypatch.setattr(llm_field, "_post_completion", refuse)


def run(coro):
    """Drive a coroutine without pytest-asyncio, which is not a dependency."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The refusal: no accepted doctype, no call
# ---------------------------------------------------------------------------
def test_it_refuses_when_the_cascade_abstained(monkeypatch: pytest.MonkeyPatch):
    """``unknown`` means a human decides. It must not mean 'ask the model instead'."""
    forbid_endpoint(monkeypatch)

    with pytest.raises(EgressViolation) as excinfo:
        run(
            llm_field.extract_fields_llm(
                [ein_field()], two_page_view(), UNKNOWN, settings=LlmSettings()
            )
        )

    assert "abstained" in str(excinfo.value)


def test_it_refuses_on_an_empty_doctype(monkeypatch: pytest.MonkeyPatch):
    """An empty doctype is an abstention that lost its label on the way here."""
    forbid_endpoint(monkeypatch)

    with pytest.raises(EgressViolation):
        run(
            llm_field.extract_fields_llm(
                [ein_field()], two_page_view(), "", settings=LlmSettings()
            )
        )


def test_it_refuses_when_called_from_inside_the_cascade(monkeypatch: pytest.MonkeyPatch):
    """A real doctype id does not make a call from inside classification legitimate."""
    forbid_endpoint(monkeypatch)

    with classification_scope(), pytest.raises(EgressViolation):
        run(
            llm_field.extract_fields_llm(
                [ein_field()], two_page_view(), "us_w9", settings=LlmSettings()
            )
        )


# ---------------------------------------------------------------------------
# The gate itself — dce.egress.post_classification_scope
# ---------------------------------------------------------------------------
def test_the_scope_permits_egress_for_a_known_doctype():
    """The other direction of the invariant: after a doctype is accepted, calling out is fine."""
    with post_classification_scope("us_w9") as accepted:
        assert accepted == "us_w9"
        assert post_classification_doctype() == "us_w9"
        assert_no_egress("t4.llm_call", settings=Settings(_env_file=None))  # must not raise


def test_the_scope_normalises_and_restores():
    with post_classification_scope("  us_w9  ") as accepted:
        assert accepted == "us_w9"
    assert post_classification_doctype() is None
    assert in_classification_scope() is False


def test_the_scope_is_restored_even_when_the_body_raises():
    with pytest.raises(ValueError), post_classification_scope("us_w9"):
        raise ValueError("boom")
    assert post_classification_doctype() is None


def test_the_scope_follows_an_asyncio_task():
    """A tier that fans out over ``gather`` must keep the scope in every child task."""

    async def child() -> str | None:
        await asyncio.sleep(0)
        return post_classification_doctype()

    async def parent() -> list[str | None]:
        with post_classification_scope("us_w9"):
            return list(await asyncio.gather(child(), child()))

    assert run(parent()) == ["us_w9", "us_w9"]


def test_the_scope_refuses_an_abstention_before_anything_is_built():
    for doctype_id in (UNKNOWN, "UNKNOWN", "", "   "):
        with pytest.raises(EgressViolation), post_classification_scope(doctype_id):
            raise AssertionError("the body must never run")


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------
def test_disabled_is_the_default_and_makes_no_call(monkeypatch: pytest.MonkeyPatch):
    forbid_endpoint(monkeypatch)

    result = run(
        llm_field.extract_fields_llm(
            [ein_field()], two_page_view(), "us_w9", settings=LlmSettings(t4_enabled=False)
        )
    )

    assert result == []


def test_the_shipped_settings_object_leaves_the_tier_off(monkeypatch: pytest.MonkeyPatch):
    """The real Settings, untouched: a deployment gets zero egress by doing nothing."""
    forbid_endpoint(monkeypatch)

    result = run(
        llm_field.extract_fields_llm(
            [ein_field()], two_page_view(), "us_w9", settings=Settings(_env_file=None)
        )
    )

    assert result == []


def test_nothing_to_ask_about_means_no_call(monkeypatch: pytest.MonkeyPatch):
    """T1-T3 resolved everything. T4 does not run 'just in case'."""
    forbid_endpoint(monkeypatch)

    empty: list = []
    assert (
        run(llm_field.extract_fields_llm(empty, two_page_view(), "us_w9", settings=LlmSettings()))
        == []
    )


def test_an_unconfigured_endpoint_does_not_call_anything(monkeypatch: pytest.MonkeyPatch):
    forbid_endpoint(monkeypatch)

    result = run(
        llm_field.extract_fields_llm(
            [ein_field()], two_page_view(), "us_w9", settings=LlmSettings(llm_base_url="")
        )
    )

    assert result == []


# ---------------------------------------------------------------------------
# The constrained response contract
# ---------------------------------------------------------------------------
def test_the_json_schema_is_built_from_the_field_specs():
    schema = llm_field.build_json_schema([ein_field(), dob_field()], doctype_id="us_w9")

    assert set(schema["properties"]) == {"ein_number", "date_of_birth"}
    assert schema["required"] == ["ein_number", "date_of_birth"]
    assert schema["additionalProperties"] is False

    ein = schema["properties"]["ein_number"]
    assert ein["type"] == ["object", "null"], "a field that is absent must be reportable as null"
    assert ein["required"] == ["value", "quote", "page"], "the quote is part of the contract"
    assert ein["additionalProperties"] is False
    assert "Employer identification number" in ein["description"]
    assert ein_field().pattern in ein["description"]


def test_the_request_constrains_the_output_and_carries_only_the_window():
    window = llm_field.build_window([ein_field()], two_page_view(), settings=LlmSettings())
    payload = llm_field.build_request([ein_field()], window, "us_w9", model="test-model")

    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert set(response_format["json_schema"]["schema"]["properties"]) == {"ein_number"}
    assert payload["temperature"] == 0

    prompt = payload["messages"][-1]["content"]
    assert "12-3456789" in prompt
    assert "Terms and conditions" not in prompt, "page 2 has nothing to do with the EIN field"


def test_free_form_prose_is_discarded_whole():
    """No salvage path: the contract was a schema, and scraping prose is the ungrounded habit."""
    prose = {"choices": [{"message": {"content": "The EIN is 12-3456789"}}]}
    assert llm_field.parse_answer(prose) == {}
    assert llm_field.parse_answer({}) == {}


# ---------------------------------------------------------------------------
# Grounding — the tests this tier exists for
# ---------------------------------------------------------------------------
def test_an_ungrounded_value_is_discarded_not_downgraded(monkeypatch: pytest.MonkeyPatch):
    """THE test. A value that is not in the fragment we sent is a hallucination.

    It is not returned at a lower confidence and it is not put in the review queue with a
    caveat: a plausible fake in a queue still gets skim-approved by a tired human at 5pm, and
    there is nothing on the page for them to check it against.
    """
    seen = stub_endpoint(
        monkeypatch,
        answer_body({"ein_number": {"value": "99-9999999", "quote": "EIN: 99-9999999", "page": 1}}),
    )

    result = run(
        llm_field.extract_fields_llm(
            [ein_field()], two_page_view(), "us_w9", settings=LlmSettings()
        )
    )

    assert seen, "the tier should have called the endpoint"
    assert result == [], "a value whose quote is absent from the window must be dropped"


def test_a_value_that_is_not_inside_its_own_quote_is_discarded(monkeypatch: pytest.MonkeyPatch):
    """A real citation with an invented value appended to it is still an invented value."""
    stub_endpoint(
        monkeypatch,
        answer_body(
            {
                "ein_number": {
                    "value": "ZZZZZ9999Z",
                    "quote": "Permanent Account Number",  # genuinely in the window
                    "page": 1,
                }
            }
        ),
    )

    result = run(
        llm_field.extract_fields_llm(
            [ein_field()], two_page_view(), "us_w9", settings=LlmSettings()
        )
    )

    assert result == []


def test_a_grounded_value_is_kept_with_its_provenance(monkeypatch: pytest.MonkeyPatch):
    stub_endpoint(
        monkeypatch,
        answer_body({"ein_number": {"value": "12-3456789", "quote": "12-3456789", "page": 1}}),
    )

    (field,) = run(
        llm_field.extract_fields_llm(
            [ein_field()], two_page_view(), "us_w9", settings=LlmSettings()
        )
    )

    assert field.name == "ein_number"
    assert field.value == "12-3456789"
    assert field.locator == "llm", "provenance says a model produced this"
    assert field.page == 1
    assert field.bbox == EIN_BBOX, "the block the quote was found in supplies the review box"
    assert field.pii is True


def test_whitespace_differences_do_not_break_grounding(monkeypatch: pytest.MonkeyPatch):
    """OCR wrapping and re-spacing are not hallucinations; different characters are."""
    stub_endpoint(
        monkeypatch,
        answer_body(
            {
                "ein_number": {
                    "value": "12-3456789",
                    "quote": "  12-3456789  ",
                    "page": 1,
                }
            }
        ),
    )

    (field,) = run(
        llm_field.extract_fields_llm(
            [ein_field()], two_page_view(), "us_w9", settings=LlmSettings()
        )
    )

    assert field.value == "12-3456789"


def test_a_null_field_is_simply_absent(monkeypatch: pytest.MonkeyPatch):
    """"Not in the fragment" is a correct answer and must not become an empty field."""
    stub_endpoint(monkeypatch, answer_body({"ein_number": None}))

    assert (
        run(
            llm_field.extract_fields_llm(
                [ein_field()], two_page_view(), "us_w9", settings=LlmSettings()
            )
        )
        == []
    )


# ---------------------------------------------------------------------------
# The verification ladder
# ---------------------------------------------------------------------------
def test_without_a_validator_a_grounded_value_stays_unverified():
    window = llm_field.build_window([dob_field()], two_page_view(), settings=LlmSettings())
    (field,) = llm_field.ground_fields(
        [dob_field()],
        {
            "date_of_birth": {
                "value": "Request for",
                "quote": "Request for Taxpayer Identification Number",
                "page": 1,
            }
        },
        window,
    )

    assert field.verification == "unverified", "the model agreeing with itself is not a check"
    assert field.confidence < 0.60


def test_a_format_validator_promotes_one_rung_only():
    view = two_page_view()
    window = llm_field.build_window([ein_field()], view, settings=LlmSettings())
    (field,) = llm_field.ground_fields(
        [ein_field()],
        {"ein_number": {"value": "12-3456789", "quote": "12-3456789", "page": 1}},
        window,
    )

    assert field.verification == "format_valid"
    assert field.normalized == "12-3456789"


def test_a_checksum_still_earns_checksum_verified_but_never_outranks_the_local_tier():
    """The check digit is computed here, over digits that provably appear on the page.

    Confidence stays under :mod:`dce.extract.resolve`'s 0.90 checksum floor, so a local locator
    that found the same value always wins the field.
    """
    view = LayoutView(
        pages=[PageInfo(page=1, width=3.37, height=2.13)],
        blocks=[
            TextBlock(text="Social Insurance Number", zone=Zone.body, page=1),
            TextBlock(text=SIN, zone=Zone.body, page=1),
        ],
    )
    window = llm_field.build_window([sin_field()], view, settings=LlmSettings())
    (field,) = llm_field.ground_fields(
        [sin_field()],
        {"sin_number": {"value": SIN, "quote": SIN, "page": 1}},
        window,
    )

    assert field.verification == "checksum_verified"
    assert field.normalized == "193-000-007"
    assert field.confidence < 0.90


def test_a_rejected_value_is_reported_unverified_with_the_validators_reason():
    view = LayoutView(
        pages=[PageInfo(page=1, width=3.37, height=2.13)],
        blocks=[
            TextBlock(text="Social Insurance Number", zone=Zone.body, page=1),
            TextBlock(text="193 000 000", zone=Zone.body, page=1),
        ],
    )
    window = llm_field.build_window([sin_field()], view, settings=LlmSettings())
    (field,) = llm_field.ground_fields(
        [sin_field()],
        {"sin_number": {"value": "193 000 000", "quote": "193 000 000", "page": 1}},
        window,
    )

    assert field.verification == "unverified"
    assert field.validator_error == "luhn_check_failed"
    assert field.confidence < 0.45, "reported so the reviewer sees it; never trusted"


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------
def test_the_window_excludes_unrelated_pages():
    window = llm_field.build_window([ein_field()], two_page_view(), settings=LlmSettings())

    assert window.pages == (1,)
    assert "12-3456789" in window.text
    assert "Terms and conditions" not in window.text
    assert "nearest office" not in window.text


def test_the_window_keeps_the_page_title_for_context():
    window = llm_field.build_window([ein_field()], two_page_view(), settings=LlmSettings())

    assert "Request for Taxpayer Identification Number" in window.text
    assert "--- page 1 ---" in window.prompt_text


def test_with_no_label_match_the_window_is_the_first_page_not_the_document():
    """The fallback is still a window. 'Send everything' is never an option."""
    view = LayoutView(
        pages=[PageInfo(page=1), PageInfo(page=2)],
        blocks=[
            TextBlock(text="An unremarkable page of prose.", zone=Zone.body, page=1),
            TextBlock(text="Another unremarkable page.", zone=Zone.body, page=2),
        ],
    )

    window = llm_field.build_window([dob_field()], view, settings=LlmSettings())

    assert window.pages == (1,)
    assert "fallback_to_first_page" in window.reason


def test_the_window_is_capped_in_characters():
    view = LayoutView(
        pages=[PageInfo(page=1)],
        blocks=[
            TextBlock(text="Date of Birth", zone=Zone.body, page=1),
            *[TextBlock(text="x" * 200, zone=Zone.body, page=1) for _ in range(50)],
        ],
    )

    window = llm_field.build_window(
        [dob_field()], view, settings=LlmSettings(llm_max_window_chars=300)
    )

    assert window.char_count <= 300
    assert window.truncated is True


def test_a_document_with_no_text_asks_nothing(monkeypatch: pytest.MonkeyPatch):
    forbid_endpoint(monkeypatch)

    result = run(
        llm_field.extract_fields_llm(
            [ein_field()], LayoutView(pages=[PageInfo(page=1)]), "us_w9", settings=LlmSettings()
        )
    )

    assert result == []


# ---------------------------------------------------------------------------
# Failure handling and import hygiene
# ---------------------------------------------------------------------------
def test_a_failing_endpoint_degrades_to_review(monkeypatch: pytest.MonkeyPatch):
    """T4 is the last *automated* tier, not the last tier: a 500 sends the field to a human."""

    async def explode(payload, *, base_url, api_key, timeout):
        raise TimeoutError("gateway said no")

    monkeypatch.setattr(llm_field, "_post_completion", explode)

    result = run(
        llm_field.extract_fields_llm(
            [ein_field()], two_page_view(), "us_w9", settings=LlmSettings()
        )
    )

    assert result == []


def test_no_http_client_is_imported_at_module_scope():
    """``httpx`` is imported inside the call, so importing this module cannot reach for one."""
    assert not hasattr(llm_field, "httpx")


def test_the_classification_path_does_not_import_this_tier():
    """The invariant's structural half: the cascade cannot even see the LLM tier."""
    for name in [m for m in list(sys.modules) if m.startswith("dce.classify")]:
        sys.modules.pop(name, None)
    sys.modules.pop("dce.extract.llm_field", None)

    importlib.import_module("dce.classify")

    assert "dce.extract.llm_field" not in sys.modules
