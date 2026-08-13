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
from typing import ClassVar

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


# ---------------------------------------------------------------------------
# What the loader TELLS the operator when it cannot load
# ---------------------------------------------------------------------------
# These drive ``_load_weights`` with a stub in place of ``AutoModel``, on purpose: the message
# an operator gets is the thing under test, and testing it must not drag ``transformers`` into
# ``sys.modules`` — ``tests/test_classify.py`` asserts it stays out.
class _RefusingAutoModel:
    """Stands in for ``transformers.AutoModel``, failing the way the real one does."""

    calls: ClassVar[list[dict]] = []

    @classmethod
    def from_pretrained(cls, path, **kwargs):
        cls.calls.append(kwargs)
        raise OSError(
            "Error no file named model.safetensors, or pytorch_model.bin, found in "
            f"directory {path}."
        )


def _tf_only_checkpoint(directory: Path) -> Path:
    """A directory shaped exactly like a company-approved TensorFlow BERT build."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text('{"model_type": "bert"}', encoding="utf-8")
    (directory / "bert_config.json").write_text('{"model_type": "bert"}', encoding="utf-8")
    (directory / "vocab.txt").write_text("[PAD]\n[UNK]\n", encoding="utf-8")
    (directory / "bert_model.ckpt.index").write_bytes(b"\x00")
    (directory / "bert_model.ckpt.data-00000-of-00001").write_bytes(b"\x00")
    (directory / "bert_model.ckpt.meta").write_bytes(b"\x00")
    return directory


def test_the_loader_makes_exactly_one_attempt(tmp_path):
    """``from_tf`` and ``from_flax`` are not retried, because they cannot work.

    transformers 5.x removed TensorFlow and Flax support, so those keywords are accepted and
    ignored and all three attempts failed with the identical missing-file error. Retrying made
    one problem look like three, and produced a message recommending an install that changes
    nothing.
    """
    from dce.classify import bert_knn

    _RefusingAutoModel.calls = []
    with pytest.raises(bert_knn.BertUnavailable):
        bert_knn.LocalBertEncoder._load_weights(_tf_only_checkpoint(tmp_path), _RefusingAutoModel)

    assert len(_RefusingAutoModel.calls) == 1
    assert _RefusingAutoModel.calls[0] == {"local_files_only": True}


def test_a_tf_only_checkpoint_is_diagnosed_and_the_real_fix_named(tmp_path):
    """The operator is told what they actually have, and the one command that resolves it."""
    from dce.classify import bert_knn

    _RefusingAutoModel.calls = []
    with pytest.raises(bert_knn.BertUnavailable) as excinfo:
        bert_knn.LocalBertEncoder._load_weights(_tf_only_checkpoint(tmp_path), _RefusingAutoModel)

    message = str(excinfo.value)
    assert "TensorFlow v1 checkpoint" in message
    assert "tools/convert_bert_tf_checkpoint.py convert" in message
    assert "will NOT help" in message


def test_the_error_never_recommends_an_install_that_cannot_fix_it(tmp_path):
    """A named fix that fixes nothing is worse than a stack trace.

    The previous message told operators to ``pip install 'dce[bert-tf]'`` or
    ``'dce[bert-flax]'``. Neither extra existed in ``pyproject.toml``, and neither would have
    helped if it had: transformers 5.x cannot read TF or Flax weights at all.
    """
    from dce.classify import bert_knn

    empty = tmp_path / "empty"
    empty.mkdir()
    for directory in (_tf_only_checkpoint(tmp_path / "tf"), empty):
        with pytest.raises(bert_knn.BertUnavailable) as excinfo:
            bert_knn.LocalBertEncoder._load_weights(directory, _RefusingAutoModel)
        message = str(excinfo.value)
        for dead_end in ("bert-tf", "bert-flax", "install tensorflow", "install jax"):
            assert dead_end not in message, f"the error still recommends {dead_end!r}"


def test_an_empty_directory_is_not_misreported_as_a_tf_checkpoint(tmp_path):
    """Two different failures with two different fixes get two different messages."""
    from dce.classify import bert_knn

    (tmp_path / "config.json").write_text('{"model_type": "bert"}', encoding="utf-8")
    with pytest.raises(bert_knn.BertUnavailable) as excinfo:
        bert_knn.LocalBertEncoder._load_weights(tmp_path, _RefusingAutoModel)

    message = str(excinfo.value)
    assert "TensorFlow v1 checkpoint" not in message
    assert "mount a complete one" in message


# ---------------------------------------------------------------------------
# THE other central test: a classification with L3 SWITCHED ON opens zero sockets
# ---------------------------------------------------------------------------
#: Run in a child interpreter, deliberately. Two reasons, both load-bearing:
#:
#: * the tripwire is armed **before** ``transformers`` and ``torch`` are imported, so the
#:   checkpoint load happens under it too — the proof covers loading the model, not just
#:   running it, and loading is the step that would reach a model hub;
#: * ``tests/test_classify.py`` asserts ``transformers`` stays out of ``sys.modules``, and an
#:   in-process version of this test would put it there for every test that ran afterwards.
_BERT_TRIPWIRE_CHILD = """
import sys
sys.path.insert(0, sys.argv[1])

from dce.classify import classify
from dce.classify.cascade import load_registry
from dce.config import Settings
from dce.egress import socket_tripwire
from dce.models import LayoutView, PageInfo, TextBlock, Zone

settings = Settings(
    _env_file=None,
    allow_preclassification_egress=False,
    bert_enabled=True,
    bert_model_dir=sys.argv[2],
    data_dir=sys.argv[3],
)
view = LayoutView(
    doc_id="bert-tripwire",
    pages=[PageInfo(page=1, width=8.5, height=11.0)],
    blocks=[
        TextBlock(text="INCOME TAX DEPARTMENT", zone=Zone.title),
        TextBlock(text="Permanent Account Number Card", zone=Zone.heading),
        TextBlock(text="ABCDE1234F", zone=Zone.body),
    ],
)
specs = load_registry()

with socket_tripwire() as attempts:
    from dce.classify import bert_knn

    available = bert_knn.tier_available(settings)
    channel = bert_knn.bert_scores(view, settings=settings)
    result = classify(view, specs, settings=settings)

print("TIER_AVAILABLE " + str(available))
print("CHANNEL_SIZE " + str(len(channel)))
print("SOCKET_ATTEMPTS " + str(len(attempts)))
print("ABSTAINED " + str(result.abstained))
print("TRANSFORMERS_LOADED " + str("transformers" in sys.modules))
"""


def _bert_runtime_or_skip() -> tuple[str, str]:
    """Locate a loadable checkpoint and an exemplar bank, or skip the test.

    L3 is optional and its checkpoint is gitignored, so this cannot be a hard requirement of
    the suite. It is also not allowed to pass *vacuously*: the caller asserts the tier really
    produced scores, so a skip means "not exercised" and never "exercised and fine".

    Returns:
        ``(model_dir, data_dir)``.
    """
    import importlib.util
    import os

    for module in ("torch", "transformers"):
        if importlib.util.find_spec(module) is None:
            pytest.skip(f"L3 is not installed: no {module} (pip install '.[bert]')")

    # The same env names Settings itself reads — there is no prefix.
    data_dir = os.environ.get("DATA_DIR") or str(_REPO_ROOT / "data")
    if not any(
        (Path(data_dir) / name).is_file()
        for name in ("bert_exemplars.npz", "bert_exemplars.json")
    ):
        pytest.skip(f"no exemplar bank under {data_dir} (see tools/build_bert_exemplars.py)")

    configured = os.environ.get("BERT_MODEL_DIR", "")
    candidates = (
        [Path(configured)] if configured else sorted((_REPO_ROOT / "models").glob("*"))
    )
    for candidate in candidates:
        if candidate.is_dir() and any(
            (candidate / name).is_file()
            for name in ("model.safetensors", "pytorch_model.bin")
        ):
            return str(candidate), data_dir
    pytest.skip(
        "no mounted checkpoint with PyTorch/safetensors weights under models/ — a TF-only "
        "checkpoint must be converted first (tools/convert_bert_tf_checkpoint.py)"
    )


def test_classification_with_bert_enabled_opens_zero_sockets():
    """The invariant with the optional tier ON — the configuration an operator deploys.

    Everything L3 does happens inside the tripwire: loading the tokenizer, reading the
    checkpoint off the mounted directory, encoding the title/heading zones, and the kNN. If
    ``transformers`` reached a model hub for any auxiliary file, this fails.
    """
    import subprocess

    model_dir, data_dir = _bert_runtime_or_skip()

    completed = subprocess.run(
        [sys.executable, "-c", _BERT_TRIPWIRE_CHILD, str(_REPO_ROOT), model_dir, data_dir],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert completed.returncode == 0, f"child failed:\n{completed.stderr[-4000:]}"
    report = dict(
        line.split(" ", 1)
        for line in completed.stdout.splitlines()
        if line.count(" ") == 1 and line.split(" ", 1)[0].isupper()
    )

    # Not vacuous: the tier was genuinely live for this document.
    assert report.get("TIER_AVAILABLE") == "True"
    assert int(report.get("CHANNEL_SIZE", "0")) > 0, "L3 scored nothing — nothing was proven"
    assert report.get("TRANSFORMERS_LOADED") == "True"
    # The point of the test.
    assert report.get("SOCKET_ATTEMPTS") == "0"
    assert report.get("ABSTAINED") == "False"
