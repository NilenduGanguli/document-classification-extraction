"""Proof that classification never leaves the process.

The invariant is not "we tried not to call anything". It is "a classification cannot open a
socket", and the difference between those two statements is this file. The central test
replaces :mod:`socket`'s constructors with functions that raise, then classifies a document
successfully — so the pass condition is not "no vendor SDK was imported" but "no connection
was attempted, at any layer, by any dependency".
"""
from __future__ import annotations

import importlib
import socket
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:  # keeps the suite runnable without an installed package
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from dce.classify import classify  # noqa: E402
from dce.config import Settings  # noqa: E402
from dce.egress import (  # noqa: E402
    EgressViolation,
    assert_no_egress,
    classification_scope,
    in_classification_scope,
    no_egress,
    socket_tripwire,
)
from dce.models import (  # noqa: E402
    UNKNOWN,
    Anchor,
    Category,
    DocTypeSpec,
    FieldSpec,
    LayoutView,
    PageInfo,
    TextBlock,
    Zone,
)

LOCKED_DOWN = Settings(_env_file=None, allow_preclassification_egress=False)
DELIBERATELY_OPEN = Settings(_env_file=None, allow_preclassification_egress=True)

MRZ_LINE_1 = "P<USAERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<"
MRZ_LINE_2 = "X123456785USA7408122F3204153ZE184226B<<<<<18"


def specs() -> list[DocTypeSpec]:
    """A two-doctype registry: enough to exercise every tier."""
    return [
        DocTypeSpec(
            doctype_id="passport",
            label="Passport",
            country="XX",
            category=Category.identity,
            anchors=[Anchor(text="PASSPORT", decisive=True), Anchor(text="Authority")],
            id_patterns=[r"P<[A-Z]{3}"],
            fields=[
                FieldSpec(
                    name="passport_number",
                    attribute_key="id.passport_number",
                    validator="mrz_td3",
                    locators=["mrz"],
                    labels={"en": ["Passport No"]},
                )
            ],
        ),
        DocTypeSpec(
            doctype_id="bank_statement",
            label="Bank Statement",
            country="XX",
            category=Category.financial,
            anchors=[Anchor(text="STATEMENT OF ACCOUNT"), Anchor(text="CLOSING BALANCE")],
            fields=[
                FieldSpec(name="account_number", labels={"en": ["Account Number"]}),
                FieldSpec(name="closing_balance", labels={"en": ["Closing Balance"]}),
            ],
        ),
    ]


def passport_view() -> LayoutView:
    return LayoutView(
        doc_id="egress-test",
        pages=[PageInfo(page=1, width=8.5, height=6.0, unit="inch")],
        blocks=[
            TextBlock(text="PASSPORT", zone=Zone.title),
            TextBlock(text="Authority: DEPARTMENT OF STATE", zone=Zone.body),
            TextBlock(text=f"{MRZ_LINE_1}\n{MRZ_LINE_2}", zone=Zone.body),
        ],
    )


def block_all_sockets(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace every socket entry point with one that raises. Returns the attempt log."""
    attempts: list[str] = []

    def refuse(name: str):
        def raiser(*args, **kwargs):
            attempts.append(name)
            raise AssertionError(f"classification attempted {name}: egress during L0-L3")

        return raiser

    monkeypatch.setattr(socket, "socket", refuse("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", refuse("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("socket.getaddrinfo"))
    monkeypatch.setattr(socket, "gethostbyname", refuse("socket.gethostbyname"))
    return attempts


# ---------------------------------------------------------------------------
# THE test: a full classification performs zero socket operations
# ---------------------------------------------------------------------------
def test_classification_opens_zero_sockets(monkeypatch: pytest.MonkeyPatch):
    """Classify a real document with the socket module sabotaged. It must still succeed."""
    attempts = block_all_sockets(monkeypatch)

    result = classify(passport_view(), specs(), settings=LOCKED_DOWN)

    assert attempts == []
    assert result.doctype_id == "passport"
    assert result.abstained is False


def test_abstaining_also_opens_zero_sockets(monkeypatch: pytest.MonkeyPatch):
    """The abstain path must not 'ask something' about the document either."""
    attempts = block_all_sockets(monkeypatch)
    view = LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0)],
        blocks=[TextBlock(text="An unremarkable page of prose.", zone=Zone.body)],
    )

    result = classify(view, specs(), settings=LOCKED_DOWN)

    assert attempts == []
    assert result.doctype_id == UNKNOWN
    assert result.abstained is True


def test_importing_the_whole_classification_path_opens_zero_sockets(
    monkeypatch: pytest.MonkeyPatch,
):
    """Import-time work counts too: a module that phones home on import is still egress."""
    for name in [m for m in list(sys.modules) if m.startswith("dce.classify")]:
        sys.modules.pop(name, None)
    attempts = block_all_sockets(monkeypatch)

    module = importlib.import_module("dce.classify")
    result = module.classify(passport_view(), specs(), settings=LOCKED_DOWN)

    assert attempts == []
    assert result.doctype_id == "passport"


def test_socket_tripwire_blocks_and_then_restores():
    """The audit utility itself works, and leaves the socket module as it found it."""
    original = socket.socket
    with socket_tripwire() as attempts, pytest.raises(EgressViolation):
        socket.create_connection(("example.invalid", 443))
    assert attempts
    assert socket.socket is original


def test_classification_under_the_tripwire_succeeds():
    """Belt and braces: the same proof, through the shipped audit helper."""
    with socket_tripwire() as attempts:
        result = classify(passport_view(), specs(), settings=LOCKED_DOWN)

    assert attempts == []
    assert result.doctype_id == "passport"


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------
def test_assert_no_egress_is_a_noop_outside_classification():
    """Post-classification egress is normal: fetching a payload, calling a model."""
    assert in_classification_scope() is False
    assert_no_egress("post_classification.des_fetch", settings=LOCKED_DOWN)


def test_assert_no_egress_raises_inside_classification():
    with classification_scope():
        assert in_classification_scope() is True
        with pytest.raises(EgressViolation) as excinfo:
            assert_no_egress("l2.embedding_api", settings=LOCKED_DOWN)

    message = str(excinfo.value)
    assert "l2.embedding_api" in message
    assert "allow_preclassification_egress" in message


def test_the_override_is_the_only_way_through():
    """Turning the invariant off is an auditable act, and it does work when taken."""
    with classification_scope():
        assert_no_egress("l3.remote_embedding", settings=DELIBERATELY_OPEN)


def test_scope_is_restored_after_the_block():
    with classification_scope():
        pass
    assert in_classification_scope() is False


def test_nested_scopes_do_not_leak():
    with classification_scope():
        with classification_scope():
            assert in_classification_scope() is True
        assert in_classification_scope() is True, "the inner exit must not clear the outer"
    assert in_classification_scope() is False


def test_scope_is_restored_even_when_the_body_raises():
    with pytest.raises(ValueError), classification_scope():
        raise ValueError("boom")
    assert in_classification_scope() is False


def test_no_egress_decorator_guards_a_whole_function():
    @no_egress("l1.vendor_lookup")
    def lookup(value: str) -> str:
        return value.upper()

    assert lookup("ok") == "OK"
    with classification_scope(), pytest.raises(EgressViolation):
        lookup("ok")


def test_the_scope_does_not_leak_into_a_thread():
    """A worker thread starts from its own context — it must not inherit the flag."""
    import threading

    seen: list[bool] = []
    with classification_scope():
        thread = threading.Thread(target=lambda: seen.append(in_classification_scope()))
        thread.start()
        thread.join()
    assert seen == [False]


def test_bert_loader_refuses_to_reach_a_model_hub():
    """The one place that could legitimately want the network is wired to the guard."""
    from dce.classify import bert_knn

    settings = Settings(
        _env_file=None,
        bert_enabled=False,  # the directory check below is what matters, not the flag
        bert_model_dir="/nonexistent/models/bert_uncased_L-12_H-768_A-12",
    )
    with classification_scope(), pytest.raises(EgressViolation):
        bert_knn.LocalBertEncoder.load(settings)


def test_bert_loader_outside_classification_reports_the_missing_mount():
    """Outside the scope the same call is not egress — it is a misconfiguration."""
    from dce.classify import bert_knn

    settings = Settings(
        _env_file=None,
        bert_enabled=False,
        bert_model_dir="/nonexistent/models/bert_uncased_L-12_H-768_A-12",
    )
    with pytest.raises(bert_knn.BertUnavailable) as excinfo:
        bert_knn.LocalBertEncoder.load(settings)

    assert "must be mounted" in str(excinfo.value)
