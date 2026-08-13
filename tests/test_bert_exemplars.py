"""The exemplar-bank builder: format fidelity, determinism, and staleness detection.

``tools/build_bert_exemplars.py`` produces the file that turns the optional local-BERT tier
(L3) on. Three things about it can go wrong silently, and this file exists to make each of
them loud:

**The format can drift from the reader.** ``dce.classify.bert_knn.ExemplarBank.load`` is
owned by another module and this tool must match it exactly, so the tests here load every
bank they write *through the real loader* and assert on what comes out. Nothing re-implements
the parse.

**The metadata can start polluting the scores.** The bank carries its provenance in a
reserved key, because the loader has no header slot and its value slot accepts only numbers
(see the tool's module docstring for the measurements behind that). A reserved key that ever
became a scored pseudo-class would shift every probability the tier reports, so its
inertness is *pinned* here rather than asserted in prose: an identical bank with and without
the key must produce identical ``knn`` output.

**The exemplars can stop matching the queries.** L3 encodes
``bert_knn.title_heading_text(view)`` at runtime. An exemplar built from differently-shaped
text is a mismatch nothing else would catch, so there is a test that the tool's text goes
through that same function.

No test here imports ``torch`` or ``transformers``. That is a constraint, not a convenience:
``tests/test_classify.py::test_bert_knn_is_never_imported_when_disabled`` asserts that
``transformers`` is absent from ``sys.modules``, and this file sorts before it. Everything
model-shaped is exercised with a deterministic stub encoder, which is possible because
:func:`build_bank` takes the encoder as a parameter.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dce.classify.bert_knn import ExemplarBank, title_heading_text  # noqa: E402
from dce.models import Anchor, DocTypeSpec, Zone  # noqa: E402
from dce.registry import all_specs  # noqa: E402
from tools.build_bert_exemplars import (  # noqa: E402
    BANK_SCHEMA,
    EXEMPLAR_RECIPE,
    META_PREFIX,
    BankMeta,
    build_bank,
    checkpoint_identity,
    compare_meta,
    exemplar_texts,
    exemplar_view,
    main,
    read_meta,
    registry_fingerprint,
    write_bank,
)

DIM = 16


class StubEncoder:
    """A deterministic stand-in for ``LocalBertEncoder``.

    Hashes the text into a fixed-dimension vector. It is not a language model and makes no
    claim to be one — every test here is about the *plumbing* around the encoder (format,
    determinism, provenance, inertness), and using a real checkpoint for that would make the
    suite depend on a 1.3 GB mount and an ML stack the service deliberately does not have.
    """

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim
        self.seen: list[str] = []

    def encode(self, text: str) -> tuple[float, ...]:
        self.seen.append(text)
        if not text.strip():
            return ()
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return tuple(
            (digest[i % len(digest)] - 127.5) / 127.5 for i in range(self.dim)
        )


def spec(doctype_id: str = "xx_demo", **kwargs) -> DocTypeSpec:
    base = {
        "doctype_id": doctype_id,
        "label": "Demonstration Certificate",
        "country": "XX",
        "issuing_authority": "Office of Demonstrations",
        "anchors": [
            Anchor(text="DEMONSTRATION CERTIFICATE", decisive=True),
            Anchor(text="Certificate number", decisive=False),
            Anchor(text="CERTIFICADO DE DEMOSTRACIÓN", lang="es", decisive=True),
        ],
    }
    base.update(kwargs)
    return DocTypeSpec(**base)


def build_to(path: Path, specs, *, per_doctype: int = 5) -> tuple[dict, BankMeta]:
    """Build a bank with the stub encoder and write it, returning ``(vectors, meta)``."""
    vectors, _ = build_bank(specs, StubEncoder(), per_doctype=per_doctype)
    meta = BankMeta(
        bank_schema=BANK_SCHEMA,
        recipe=EXEMPLAR_RECIPE,
        registry_fingerprint=registry_fingerprint(
            list(specs), per_doctype=per_doctype, max_tokens=256
        ),
        checkpoint_digest="deadbeef",
        checkpoint_files={"config.json": "cafe"},
        model_dir="/models/stub",
        dim=DIM,
        max_tokens=256,
        per_doctype=per_doctype,
        n_doctypes=len(vectors),
        n_vectors=sum(len(rows) for rows in vectors.values()),
    )
    write_bank(path, vectors, meta)
    return vectors, meta


# ---------------------------------------------------------------------------
# (a) The bank is readable by the tier's own loader
# ---------------------------------------------------------------------------
def test_the_real_loader_reads_a_bank_this_tool_wrote(tmp_path: Path):
    """The whole point. Parsed by ``ExemplarBank.load``, not by a re-implementation."""
    path = tmp_path / "bert_exemplars.json"
    build_to(path, [spec("xx_one"), spec("xx_two", label="Second Certificate")])

    bank = ExemplarBank.load(path)

    assert "xx_one" in bank.vectors
    assert "xx_two" in bank.vectors
    assert all(len(row) == DIM for rows in bank.vectors.values() for row in rows)
    assert bank.source == str(path)


def test_loaded_vectors_are_unit_length(tmp_path: Path):
    """``knn`` is a dot product, which is only a cosine if both sides are normalised."""
    path = tmp_path / "bert_exemplars.json"
    build_to(path, [spec()])

    for rows in ExemplarBank.load(path).vectors.values():
        for row in rows:
            assert math.isclose(math.sqrt(sum(v * v for v in row)), 1.0, abs_tol=1e-6)


def test_knn_ranks_a_doctype_first_for_its_own_exemplar_text(tmp_path: Path):
    """End-to-end sanity: encode one doctype's own exemplar, and it wins its own bank.

    This is the weakest possible claim about retrieval quality and the strongest one a stub
    encoder can support — it says the vectors were stored against the right keys and the
    normalisation did not scramble them. What the *real* checkpoint retrieves is a
    measurement, not a unit test, and belongs in the corpus harness.
    """
    specs = [spec("xx_one"), spec("xx_two", label="Entirely Different Thing")]
    path = tmp_path / "bert_exemplars.json"
    build_to(path, specs)

    encoder = StubEncoder()
    query = encoder.encode(
        title_heading_text(exemplar_view(exemplar_texts(specs[0], limit=5)[0]))
    )
    ranked = sorted(ExemplarBank.load(path).knn(query, 5).items(), key=lambda kv: -kv[1])

    assert ranked[0][0] == "xx_one"


# ---------------------------------------------------------------------------
# (b) The provenance key is inert — the property the whole design rests on
# ---------------------------------------------------------------------------
def test_metadata_key_contributes_nothing_to_any_score(tmp_path: Path):
    """A bank with the reserved key must score IDENTICALLY to one without it.

    The metadata lives in a key name because ``ExemplarBank.load`` has nowhere else to put
    it. If that key ever started behaving like a doctype it would enter ``robust_z`` and
    ``softmax`` in ``bert_scores`` and shift every probability the tier reports — quietly,
    and in a way no other test would see. So the equivalence is measured, not argued.
    """
    with_key = tmp_path / "with_meta.json"
    _, meta = build_to(with_key, [spec("xx_one"), spec("xx_two")])

    # Delete the key from the file the tool wrote, rather than re-serialising the vectors:
    # a fresh json.dumps would round differently and the test would be measuring float
    # formatting instead of the thing it is here to measure.
    payload = json.loads(with_key.read_text(encoding="utf-8"))
    stripped = tmp_path / "without_meta.json"
    stripped.write_text(
        json.dumps({k: v for k, v in payload.items() if not k.startswith(META_PREFIX)}),
        encoding="utf-8",
    )

    query = [1.0] * DIM
    with_meta = ExemplarBank.load(with_key).knn(query, 5)
    without_meta = ExemplarBank.load(stripped).knn(query, 5)

    assert with_meta == without_meta
    assert not any(key.startswith("__") for key in with_meta)
    assert meta.n_doctypes == 2


def test_metadata_key_survives_loading_as_an_empty_exemplar_set(tmp_path: Path):
    """It is present in ``vectors`` but holds nothing, which is what makes ``knn`` skip it."""
    path = tmp_path / "bank.json"
    build_to(path, [spec()])

    bank = ExemplarBank.load(path)
    reserved = [key for key in bank.vectors if key.startswith(META_PREFIX)]

    assert len(reserved) == 1
    assert bank.vectors[reserved[0]] == ()


# ---------------------------------------------------------------------------
# (c) Determinism
# ---------------------------------------------------------------------------
def test_same_inputs_give_a_byte_identical_bank(tmp_path: Path):
    """A rebuild must be a no-op, or "is this bank current?" has no answer."""
    specs = [spec("xx_one"), spec("xx_two")]
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    build_to(first, specs)
    build_to(second, specs)

    assert first.read_bytes() == second.read_bytes()


def test_negative_zero_never_reaches_the_file(tmp_path: Path):
    """-0.0 and 0.0 are the same number; only one spelling may ever be written."""
    path = tmp_path / "bank.json"
    write_bank(
        path,
        {"xx_one": [[-1e-12, 1e-12, 1.0] + [0.0] * (DIM - 3)]},
        BankMeta(
            bank_schema=BANK_SCHEMA, recipe=EXEMPLAR_RECIPE, registry_fingerprint="f",
            checkpoint_digest="d", checkpoint_files={}, model_dir="m", dim=DIM,
            max_tokens=256, per_doctype=5, n_doctypes=1, n_vectors=1,
        ),
    )

    assert "-0.000000" not in path.read_text(encoding="utf-8")


def test_exemplar_text_is_a_pure_function_of_the_declaration():
    """Two identical declarations give identical text; order is never incidental."""
    assert exemplar_texts(spec(), limit=5) == exemplar_texts(spec(), limit=5)


# ---------------------------------------------------------------------------
# (d) Staleness detection
# ---------------------------------------------------------------------------
def test_a_changed_registry_marks_the_bank_stale(tmp_path: Path):
    """A doctype gaining an anchor must invalidate a bank built before it did."""
    before = [spec("xx_one")]
    after = [spec("xx_one", anchors=[*spec().anchors, Anchor(text="NEW DECISIVE ANCHOR")])]
    path = tmp_path / "bank.json"
    _, meta = build_to(path, before)

    fresh = registry_fingerprint(before, per_doctype=5, max_tokens=256)
    stale = registry_fingerprint(after, per_doctype=5, max_tokens=256)
    on_disk = read_meta(path)

    assert compare_meta(on_disk, meta) == []
    assert compare_meta(on_disk, _with(meta, registry_fingerprint=fresh)) == []
    reasons = compare_meta(on_disk, _with(meta, registry_fingerprint=stale))
    assert reasons and "registry has changed" in reasons[0]


def test_a_changed_checkpoint_marks_the_bank_stale(tmp_path: Path):
    path = tmp_path / "bank.json"
    _, meta = build_to(path, [spec()])

    reasons = compare_meta(read_meta(path), _with(meta, checkpoint_digest="0" * 8))

    assert reasons and "checkpoint has changed" in reasons[0]


def test_a_changed_recipe_marks_the_bank_stale(tmp_path: Path):
    """Different sentences give different vectors, even from the same registry and model."""
    path = tmp_path / "bank.json"
    _, meta = build_to(path, [spec()])

    reasons = compare_meta(read_meta(path), _with(meta, recipe="something-else-v2"))

    assert reasons and "exemplar recipe" in reasons[0]


def test_changing_a_field_definition_does_not_mark_the_bank_stale():
    """The fingerprint covers what the exemplars READ, and nothing else.

    Field specs, validators and locators never reach :func:`exemplar_texts`. If they were in
    the fingerprint, every unrelated registry edit would order a needless rebuild — and a
    staleness signal that cries wolf is one operators learn to ignore.
    """
    without = spec()
    with_handling = spec(handling="mask the identifier", officially_valid=True)

    assert registry_fingerprint([without], per_doctype=5, max_tokens=256) == (
        registry_fingerprint([with_handling], per_doctype=5, max_tokens=256)
    )


def test_a_bank_with_no_provenance_is_rejected_rather_than_trusted(tmp_path: Path):
    """An unlabelled bank could have been built against anything. Refuse to vouch for it."""
    path = tmp_path / "bank.json"
    path.write_text(json.dumps({"xx_one": [[1.0, 0.0]]}), encoding="utf-8")

    with pytest.raises(ValueError, match="carries no"):
        read_meta(path)


def _with(meta: BankMeta, **changes) -> BankMeta:
    return BankMeta(**{**meta.__dict__, **changes})


# ---------------------------------------------------------------------------
# (e) The exemplars are the text the tier will actually compare against
# ---------------------------------------------------------------------------
def test_exemplars_are_encoded_through_the_tiers_own_zone_extractor(tmp_path: Path):
    """The encoder must see exactly what ``title_heading_text`` would produce at runtime.

    An exemplar built from a differently-joined string is a silent mismatch: cosine distance
    would then be measuring a formatting difference as if it were a semantic one.
    """
    encoder = StubEncoder()
    subject = spec()
    build_bank([subject], encoder, per_doctype=5)

    expected = [
        title_heading_text(exemplar_view(text))
        for text in exemplar_texts(subject, limit=5)
    ]
    assert encoder.seen == expected


def test_exemplar_view_puts_every_line_where_the_tier_looks():
    """Title first, headings after — the only two zones ``title_heading_text`` reads."""
    view = exemplar_view("First Line\nSecond Line\nThird Line")

    assert [b.zone for b in view.blocks] == [Zone.title, Zone.heading, Zone.heading]
    assert title_heading_text(view) == "First Line\nSecond Line\nThird Line"


def test_languages_are_never_spliced_into_one_exemplar():
    """A title zone is written in one language; a bilingual chimera matches no document."""
    texts = exemplar_texts(spec(), limit=5)
    spanish = [t for t in texts if "CERTIFICADO DE DEMOSTRACIÓN" in t]

    assert spanish, "the Spanish anchor must get an exemplar of its own"
    assert all("DEMONSTRATION CERTIFICATE" not in t for t in spanish)


def test_title_zoned_anchors_lead_their_language():
    """An anchor the registry pins to the title zone is the best exemplar text there is."""
    subject = spec(
        anchors=[
            Anchor(text="Ordinary anchor"),
            Anchor(text="DECISIVE ANCHOR", decisive=True),
            Anchor(text="TITLE ZONED ANCHOR", zone=Zone.title),
        ]
    )
    anchor_exemplar = exemplar_texts(subject, limit=5)[2]

    assert anchor_exemplar.startswith("TITLE ZONED ANCHOR")


# ---------------------------------------------------------------------------
# (f) The real registry
# ---------------------------------------------------------------------------
def test_every_registered_doctype_gets_at_least_one_exemplar():
    """A doctype with no exemplar is invisible to L3 — it can never be the answer."""
    missing = [s.doctype_id for s in all_specs() if not exemplar_texts(s, limit=5)]

    assert missing == []


def test_almost_every_doctype_gets_the_full_complement():
    """``knn`` averages the top ``k``, so unequal exemplar counts are unequal treatment.

    A class with one exemplar is judged on its single best phrasing; a class with five is
    judged on the mean of five, which is a harder bar. Keeping the counts uniform keeps the
    comparison like-for-like. It is asserted as a floor rather than an equality because the
    registry is allowed to contain a doctype too thinly declared to yield five distinct
    phrasings — and when that happens it should show up as a registry gap, not a test break.
    """
    counts = [len(exemplar_texts(s, limit=5)) for s in all_specs()]

    assert min(counts) >= 4
    assert sum(1 for c in counts if c == 5) >= 0.95 * len(counts)


def test_no_exemplar_is_empty_or_whitespace():
    for subject in all_specs():
        for text in exemplar_texts(subject, limit=5):
            assert text.strip(), f"{subject.doctype_id} produced a blank exemplar"


def test_exemplars_stay_short_enough_to_survive_truncation():
    """L3 truncates at ``bert_max_tokens`` (256). An exemplar that overflows it is one whose
    tail is silently discarded — and the discarded tail is the part that was supposed to be
    distinguishing."""
    longest = max(
        (len(t.split()) for s in all_specs() for t in exemplar_texts(s, limit=5)),
        default=0,
    )

    assert longest < 128, f"{longest} words risks running into the 256-token truncation"


# ---------------------------------------------------------------------------
# (g) The CLI's guard rails
# ---------------------------------------------------------------------------
def test_dry_run_needs_no_model_and_no_checkpoint(capsys, tmp_path: Path):
    """The text half of the decision must be reviewable without a 1.3 GB mount."""
    code = main(["--dry-run", "--country", "XX", "--out", str(tmp_path / "bank.json")])

    assert code == 0
    assert "exemplars" in capsys.readouterr().out


def test_check_reports_a_missing_bank_as_a_failure(capsys, tmp_path: Path):
    """``tier_available()`` is False without the file, and ``--check`` must say so loudly."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")

    code = main(["--check", "--model-dir", str(model_dir), "--out", str(tmp_path / "no.json")])

    assert code == 1
    assert "NO BANK" in capsys.readouterr().out


def test_a_directory_that_is_not_a_checkpoint_is_refused(tmp_path: Path):
    """Building against an empty directory would write a bank of noise. Fail instead."""
    with pytest.raises(FileNotFoundError, match="not a BERT checkpoint"):
        checkpoint_identity(tmp_path)


def test_checkpoint_identity_tracks_the_files_that_define_the_encoder(tmp_path: Path):
    (tmp_path / "config.json").write_text('{"hidden_size": 768}', encoding="utf-8")
    (tmp_path / "vocab.txt").write_text("[PAD]\n[UNK]\n", encoding="utf-8")
    (tmp_path / "flax_model.msgpack").write_bytes(b"irrelevant sibling format")
    before, files = checkpoint_identity(tmp_path)

    (tmp_path / "vocab.txt").write_text("[PAD]\n[UNK]\nextra\n", encoding="utf-8")
    after, _ = checkpoint_identity(tmp_path)

    assert set(files) == {"config.json", "vocab.txt"}, "unloaded formats must not count"
    assert before != after, "a changed vocab changes what every exemplar encodes to"


def test_an_npz_in_the_way_is_refused_rather_than_shadowed(tmp_path: Path):
    """``_EXEMPLAR_NAMES`` prefers the .npz, so a leftover one silently wins.

    Writing the JSON next to it and reporting success would leave the operator running an
    old bank while believing they had rebuilt it.
    """
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "bert_exemplars.npz").write_bytes(b"stale")

    with pytest.raises(SystemExit, match="would shadow"):
        main([
            "--model-dir", str(model_dir),
            "--out", str(tmp_path / "bert_exemplars.json"),
        ])
