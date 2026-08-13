#!/usr/bin/env python3
"""Build the per-doctype exemplar bank that the optional local-BERT tier (L3) needs.

``dce.classify.bert_knn`` classifies by cosine kNN against per-doctype exemplar *vectors*
that were computed offline and shipped as a file. Without that file
:func:`dce.classify.bert_knn.tier_available` is ``False`` and the tier is silently off. This
tool is what produces the file. It is a **build tool, not part of the service**: it imports
``torch``/``transformers``, which the default container deliberately does not have.

Usage::

    python tools/build_bert_exemplars.py --model-dir models/bert_uncased_L-12_H-768_A-12
    python tools/build_bert_exemplars.py --dry-run              # exemplar TEXT only, no model
    python tools/build_bert_exemplars.py --check                # is the bank on disk stale?

--------------------------------------------------------------------------------------
What text represents a doctype? (the decision this tool exists to make)
--------------------------------------------------------------------------------------
An exemplar bank is only as meaningful as the text it was built from, and there were three
candidates. This tool implements **(a), the registry declaration**, and does not implement
the others. The reasoning, in full, because it is the load-bearing choice here:

**(a) The registry declaration — chosen.** Every doctype already carries the words that
identify it: its human label, its issuing authority, and its anchors (some declared
``decisive``, some declared to live in the ``title`` zone outright). This is the same source
:mod:`dce.classify.profiles` derives the lexical profiles from — "the registry is the
training data" — so L3 stays consistent with L2 rather than introducing a second, unrelated
notion of what a doctype is. It covers **all 182 doctypes**, including the ones no corpus
document exists for, and it needs no labelled data to exist first.

**(b) Real corpus documents' title/heading zones — rejected.** It is closer to what will be
seen at runtime, and that is its only advantage. Against it: ``corpus/`` holds 158 documents
covering a *fraction* of the 182 doctypes, so a corpus-fitted bank would score most of the
registry against nothing; and, decisively, **fitting exemplars to the corpus would make the
corpus a training set and destroy its value as a test set**. Every accuracy number this
project has ever reported comes from those 158 files. A bank built from them would be
measured on them, and the measurement would be circular — it would report how well the tier
memorised the test set, which is precisely the overfitting the brief forbids.

**(c) A hybrid — rejected for now, and it is the documented upgrade path.** Mixing curated
declarations with real examples is the right end state, and :func:`dce.classify.profiles.
fit_profiles` is the precedent for how this codebase does that. But a hybrid needs a
*held-out split*, and 158 documents across 182 classes cannot be split into a train half and
a test half that measures anything. It needs labelled production data, not this corpus.

So: registry only. The honest cost of that choice is stated rather than hidden — these
exemplars are what a doctype *declares itself to be*, not what a scanned instance of it
looks like, and OCR noise, layout furniture and vernacular phrasing are all absent from
them. That gap is a reason to expect the tier to be weak, and it is measured, not assumed.

--------------------------------------------------------------------------------------
Encode the same text the tier encodes
--------------------------------------------------------------------------------------
At runtime L3 encodes :func:`dce.classify.bert_knn.title_heading_text` of the payload —
the TITLE and HEADING blocks, newline-joined, truncated to ``bert_max_tokens``. An exemplar
built from *different* text than the query is a silent mismatch that nothing would catch, so
this tool does not format its own strings: it builds a real :class:`~dce.models.LayoutView`
whose blocks are ``title``/``heading``, runs it through that same ``title_heading_text``, and
hands the result to the same :class:`~dce.classify.bert_knn.LocalBertEncoder` the tier uses.
Same function, same encoder, same truncation.

--------------------------------------------------------------------------------------
Determinism
--------------------------------------------------------------------------------------
Same registry + same checkpoint must give a byte-identical bank, so that a rebuild is a
no-op and any diff is a real change. Achieved by: CPU only, inference mode (no dropout), no
grad, a fixed seed, a single intra-op thread (thread count changes float reduction order),
sorted keys, and fixed-precision float formatting.

This is also why the bank is written as **JSON and not ``.npz``**, even though
``ExemplarBank.load`` accepts both and ``_EXEMPLAR_NAMES`` prefers ``.npz``: an ``.npz`` is
a zip archive and ``np.savez`` stamps every member with the current time, so two runs over
identical inputs produce different bytes *by construction*. A format that cannot be
byte-identical cannot support the staleness check below. (The tool refuses to run if a
``.npz`` is already sitting in the data dir, because that file would shadow the JSON one.)

--------------------------------------------------------------------------------------
Staleness metadata, and the one place it can live
--------------------------------------------------------------------------------------
A bank built against last month's registry scores documents against doctypes that no longer
exist, and nothing about the file would say so. It therefore carries the checkpoint identity
and a registry fingerprint, and ``--check`` compares them against what is on disk now.

Where that metadata lives is forced, not chosen. ``ExemplarBank.load`` reads the file as a
flat ``{key: list-of-vectors}`` map with no header slot, and it is owned by another module
this tool must not edit. Measured against the real loader, every obvious in-band carrier
breaks it: a key mapping to an object raises ``KeyError: 0``, a key mapping to a string
raises ``ValueError``, and a nested ``{"meta": ..., "vectors": ...}`` layout raises
``KeyError: 0`` — and a non-``.npy`` member smuggled into an ``.npz`` raises
``AttributeError`` on ``bytes.tolist``. Exactly one shape survives: **a key whose value is
the empty list**. Such a key normalises to an empty exemplar tuple, and
:meth:`ExemplarBank.knn` skips empty tuples, so it contributes nothing to any score and
never appears in the returned mapping. The metadata is therefore encoded *in the key name*,
behind :data:`META_PREFIX`, with ``[]`` as its value. It is ugly, it is deliberate, and
``tests/test_bert_exemplars.py`` pins the inertness rather than asserting it.

The clean fix is three lines in ``bert_knn.ExemplarBank.load`` — skip keys starting with
``__`` and expose them as ``ExemplarBank.meta`` — and it is left to that module's owner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # runnable as `python tools/build_bert_exemplars.py`
    sys.path.insert(0, str(REPO_ROOT))

from dce.config import Settings, get_settings  # noqa: E402
from dce.models import DocTypeSpec, LayoutView, TextBlock, Zone  # noqa: E402
from dce.registry import all_specs  # noqa: E402

__all__ = [
    "BANK_SCHEMA",
    "EXEMPLAR_RECIPE",
    "META_PREFIX",
    "BankMeta",
    "build_bank",
    "checkpoint_identity",
    "compare_meta",
    "exemplar_texts",
    "exemplar_view",
    "main",
    "read_meta",
    "registry_fingerprint",
    "write_bank",
]

#: Bumped when the on-disk layout changes in a way an older reader would misread.
BANK_SCHEMA = 1

#: Bumped when :func:`exemplar_texts` changes what text it produces. Part of the registry
#: fingerprint, so changing the recipe marks every existing bank stale — which is correct:
#: the vectors in it were built from different sentences.
EXEMPLAR_RECIPE = "registry-declarative-v1"

#: Reserved key prefix for the in-band metadata. See the module docstring for why the
#: metadata is in the *key* and the value is ``[]``.
META_PREFIX = "__dce_meta__:"

#: Decimal places each vector component is written with. The vectors are L2-normalised
#: before writing, so every component is in ``[-1, 1]`` and six places is ~1e-6 absolute —
#: four orders of magnitude below anything a cosine comparison can resolve. Fixed precision
#: (rather than ``repr``) is what makes the file byte-identical across runs and platforms.
FLOAT_PLACES = 6

#: Files that determine what the encoder *is*. Hashing these, rather than every file in the
#: directory, means an unrelated sibling weight format (a leftover ``flax_model.msgpack``,
#: say) does not spuriously mark a bank stale — while any change to the weights, the vocab
#: or the architecture config does.
_IDENTITY_FILES = (
    "config.json",
    "vocab.txt",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "model.safetensors",
    "pytorch_model.bin",
)

#: The ``.npz`` name ``exemplar_path`` prefers. If it exists it shadows the JSON bank.
_SHADOWING_NAME = "bert_exemplars.npz"
_BANK_NAME = "bert_exemplars.json"


# ---------------------------------------------------------------------------
# What text represents a doctype
# ---------------------------------------------------------------------------
def _lines_by_language(spec: DocTypeSpec) -> dict[str, list[str]]:
    """Anchor texts grouped by declared language, strongest-evidence first within each.

    Order within a language: anchors declared to sit in the ``title`` or ``heading`` zone
    first (the registry author has said outright that this text appears where L3 looks),
    then decisive anchors, then the rest. Declaration order is preserved inside each band,
    so the output is a pure function of the registry.
    """
    banded: dict[str, tuple[list[str], list[str], list[str]]] = {}
    for anchor in spec.anchors:
        text = anchor.text.strip()
        if not text:
            continue
        zoned, decisive, rest = banded.setdefault(anchor.lang, ([], [], []))
        if anchor.zone in (Zone.title, Zone.heading):
            zoned.append(text)
        elif anchor.decisive:
            decisive.append(text)
        else:
            rest.append(text)
    return {lang: [*z, *d, *r] for lang, (z, d, r) in banded.items()}


def _language_order(by_lang: dict[str, list[str]]) -> list[str]:
    """Languages worth an exemplar of their own, most-declared first, ties broken by name.

    One exemplar per language rather than one exemplar mixing them. A real document's title
    zone is written in one language; an exemplar that splices a French anchor onto a Hindi
    one is a sentence no document will ever contain, and encoding it just puts a chimera in
    the bank for every class to be compared against.
    """
    return sorted(by_lang, key=lambda lang: (-len(by_lang[lang]), lang))


def exemplar_texts(spec: DocTypeSpec, *, limit: int) -> list[str]:
    """The texts that stand for ``spec``, best first, deduplicated and capped at ``limit``.

    Each candidate is a *plausible title/heading zone* for the document — one to three short
    lines, the way an official form actually prints its own identity — not a bag of registry
    strings. They are generated in a fixed priority order and truncated, so ``limit``
    controls quality rather than merely quantity.

    Args:
        spec: The doctype declaration.
        limit: Maximum exemplars to return. Aligning it with ``settings.bert_knn_k`` keeps
            every class's score a mean over the same number of neighbours.

    Returns:
        Distinct exemplar texts, in priority order. Never empty: every registered doctype has
        a non-empty ``label``.
    """
    by_lang = _lines_by_language(spec)
    langs = _language_order(by_lang)
    primary = by_lang.get("en") or (by_lang[langs[0]] if langs else [])
    label = spec.label.strip()
    authority = spec.issuing_authority.strip()

    candidates: list[str] = []

    def add(*lines: str) -> None:
        kept = [line.strip() for line in lines if line and line.strip()]
        if kept:
            candidates.append("\n".join(kept))

    # 1. The label alone — the minimum a title zone can say and still identify the document.
    add(label)
    # 2. Authority over label: the two-line header an issued form actually prints.
    add(authority, label)
    # 3. Per language, that language's strongest anchor lines on their own. This is the
    #    exemplar closest to a real header, and the only one non-English documents get.
    for lang in langs:
        add(*by_lang[lang][:3])
    # 4. Label plus the next English lines below it — a title with its first headings.
    add(label, *primary[:2])
    # 5. Authority plus the strongest lines — a masthead with a form number under it.
    add(authority, *primary[:2])

    seen: set[str] = set()
    unique: list[str] = []
    for text in candidates:
        key = " ".join(text.split()).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(text)
        if len(unique) >= limit:
            break
    return unique


def exemplar_view(text: str) -> LayoutView:
    """Wrap an exemplar text as the payload shape L3 sees at runtime.

    The first line becomes a ``title`` block and the rest ``heading`` blocks, so that
    :func:`dce.classify.bert_knn.title_heading_text` — the *same* function the tier calls on
    a real document — is what produces the string that gets encoded. Nothing here formats
    text for the encoder directly; that is the whole point.
    """
    lines = [line for line in text.split("\n") if line.strip()]
    blocks = [
        TextBlock(text=line, zone=Zone.title if i == 0 else Zone.heading, page=1)
        for i, line in enumerate(lines)
    ]
    return LayoutView(doc_id="exemplar", blocks=blocks)


# ---------------------------------------------------------------------------
# Identity: what was this bank built from
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BankMeta:
    """Provenance recorded inside the bank so a stale bank can be detected.

    Attributes:
        bank_schema: On-disk layout version.
        recipe: :data:`EXEMPLAR_RECIPE` — which text the exemplars were built from.
        registry_fingerprint: Digest of every registry field the exemplars derive from.
        checkpoint_digest: Digest of the encoder's identity files.
        checkpoint_files: ``filename -> sha256`` for each identity file that existed.
        model_dir: Where the checkpoint was read from, for the audit trail only.
        dim: Vector dimensionality.
        max_tokens: Truncation length the exemplars were encoded at.
        per_doctype: Exemplar cap used.
        n_doctypes: Classes in the bank.
        n_vectors: Total exemplar vectors, so a truncated file is obvious.
    """

    bank_schema: int
    recipe: str
    registry_fingerprint: str
    checkpoint_digest: str
    checkpoint_files: dict[str, str]
    model_dir: str
    dim: int
    max_tokens: int
    per_doctype: int
    n_doctypes: int
    n_vectors: int

    def to_key(self) -> str:
        """Encode as the reserved bank key (see the module docstring for why)."""
        payload = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))
        return META_PREFIX + payload

    @classmethod
    def from_key(cls, key: str) -> BankMeta:
        """Decode a reserved bank key.

        Raises:
            ValueError: If the key is not a metadata key or is missing fields.
        """
        if not key.startswith(META_PREFIX):
            raise ValueError(f"not a metadata key: {key[:40]!r}")
        raw = json.loads(key[len(META_PREFIX):])
        known = {f: raw[f] for f in cls.__dataclass_fields__ if f in raw}
        missing = set(cls.__dataclass_fields__) - set(known)
        if missing:
            raise ValueError(f"metadata key is missing fields: {sorted(missing)}")
        return cls(**known)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checkpoint_identity(model_dir: Path) -> tuple[str, dict[str, str]]:
    """Hash the files that determine what the encoder is.

    Args:
        model_dir: The mounted checkpoint directory. Only read; never fetched, never written.

    Returns:
        ``(aggregate_digest, {filename: sha256})``.

    Raises:
        FileNotFoundError: If the directory has none of the identity files — which means it
            is not a checkpoint, and building against it would produce a bank of noise.
    """
    per_file = {
        name: _sha256_file(model_dir / name)
        for name in _IDENTITY_FILES
        if (model_dir / name).is_file()
    }
    if not per_file:
        raise FileNotFoundError(
            f"{model_dir} contains none of {list(_IDENTITY_FILES)} — that is not a BERT "
            "checkpoint directory."
        )
    aggregate = hashlib.sha256(
        "\n".join(f"{name}:{sha}" for name, sha in sorted(per_file.items())).encode()
    ).hexdigest()
    return aggregate, per_file


def registry_fingerprint(
    specs: Sequence[DocTypeSpec], *, per_doctype: int, max_tokens: int
) -> str:
    """Digest every input that could change the exemplar text or its encoding.

    Deliberately *not* a hash of the whole spec: field definitions and validators do not
    reach :func:`exemplar_texts`, so a change there must not mark a good bank stale. What is
    hashed is exactly what is read — id, label, authority, country, and every anchor's text,
    language, decisiveness and zone — plus the recipe id and the two build parameters.
    """
    payload = {
        "recipe": EXEMPLAR_RECIPE,
        "bank_schema": BANK_SCHEMA,
        "per_doctype": per_doctype,
        "max_tokens": max_tokens,
        "doctypes": [
            {
                "doctype_id": spec.doctype_id,
                "label": spec.label,
                "issuing_authority": spec.issuing_authority,
                "country": spec.country,
                "anchors": [
                    [a.text, a.lang, bool(a.decisive), str(a.zone) if a.zone else ""]
                    for a in spec.anchors
                ],
            }
            for spec in sorted(specs, key=lambda s: s.doctype_id)
        ],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def _l2(vector: Sequence[float]) -> list[float]:
    norm = sum(v * v for v in vector) ** 0.5
    return [v / norm for v in vector] if norm > 0 else list(vector)


def _load_encoder(model_dir: Path, max_tokens: int, device: str) -> object:
    """Load the tier's own encoder, pinned to deterministic CPU inference.

    Imports live here rather than at module scope so ``--dry-run`` and ``--check`` work in an
    environment with no ML stack at all — which is the environment the service itself runs
    in.
    """
    import torch

    from dce.classify.bert_knn import BertUnavailable, LocalBertEncoder

    torch.manual_seed(0)
    # Reduction order over threads is not fixed, so >1 thread makes the last bits of a
    # mean-pool non-reproducible. Determinism is worth more here than build speed.
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True, warn_only=True)

    settings = Settings(
        bert_enabled=True,
        bert_model_dir=str(model_dir),
        bert_max_tokens=max_tokens,
        bert_device=device,
        allow_preclassification_egress=False,
    )
    try:
        encoder = LocalBertEncoder.load(settings)
    except BertUnavailable as exc:
        raise SystemExit(f"cannot build the bank: {exc}") from exc
    # Belt and braces: LocalBertEncoder.load already does this, but a bank built with
    # dropout live is silently wrong rather than loudly broken.
    encoder.model.train(False)
    return encoder


def build_bank(
    specs: Iterable[DocTypeSpec],
    encoder: object,
    *,
    per_doctype: int,
) -> tuple[dict[str, list[list[float]]], dict[str, list[str]]]:
    """Encode every doctype's exemplars.

    Args:
        specs: The registry.
        encoder: A loaded :class:`~dce.classify.bert_knn.LocalBertEncoder`.
        per_doctype: Exemplar cap per class.

    Returns:
        ``({doctype_id: [vector, ...]}, {doctype_id: [text, ...]})`` — the vectors and the
        texts they came from, the latter for the human-readable build report.

    ``torch`` is deliberately not imported here — ``LocalBertEncoder.encode`` already wraps
    its own forward pass in ``torch.no_grad()``. That keeps this function testable with a
    stub encoder in an environment with no ML stack, which is the environment the service's
    own test suite runs in (``tests/test_classify.py`` asserts ``transformers`` never
    reaches ``sys.modules``).
    """
    from dce.classify.bert_knn import title_heading_text

    vectors: dict[str, list[list[float]]] = {}
    texts: dict[str, list[str]] = {}
    for spec in sorted(specs, key=lambda s: s.doctype_id):
        rows: list[list[float]] = []
        kept: list[str] = []
        for text in exemplar_texts(spec, limit=per_doctype):
            # Same function the tier calls on a real payload — see exemplar_view.
            vector = encoder.encode(title_heading_text(exemplar_view(text)))  # type: ignore[attr-defined]
            if not vector:
                continue
            rows.append(_l2(vector))
            kept.append(text)
        if rows:
            vectors[spec.doctype_id] = rows
            texts[spec.doctype_id] = kept
    return vectors, texts


_ZERO = "0." + "0" * FLOAT_PLACES


def _format_float(value: float) -> str:
    text = f"{value:.{FLOAT_PLACES}f}"
    # "-0.000000" and "0.000000" are the same number; only one of them may ever be written,
    # or the file stops being byte-identical for a reason that carries no information.
    return _ZERO if text.lstrip("-") == _ZERO else text


def write_bank(path: Path, vectors: dict[str, list[list[float]]], meta: BankMeta) -> None:
    """Write the bank as deterministic JSON: sorted keys, fixed float precision.

    Hand-rolled rather than ``json.dump`` so each doctype occupies exactly one line — a
    single-line 7 MB file is undiffable, and a pretty-printed one is several times larger.
    """
    lines = ["{", f"{json.dumps(meta.to_key())}: [],"]
    ordered = sorted(vectors.items())
    for index, (doctype_id, rows) in enumerate(ordered):
        body = ",".join(
            "[" + ",".join(_format_float(v) for v in row) + "]" for row in rows
        )
        comma = "" if index == len(ordered) - 1 else ","
        lines.append(f"{json.dumps(doctype_id)}: [{body}]{comma}")
    lines.append("}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------
def read_meta(path: Path) -> BankMeta:
    """Read the metadata out of an existing bank.

    Raises:
        ValueError: If the file carries no metadata key — i.e. it was written by something
            other than this tool, and its provenance is unknown.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    for key in raw:
        if key.startswith(META_PREFIX):
            return BankMeta.from_key(key)
    raise ValueError(
        f"{path} carries no {META_PREFIX!r} key: it was not written by this tool and there "
        "is no way to tell what registry or checkpoint it was built from."
    )


def compare_meta(on_disk: BankMeta, expected: BankMeta) -> list[str]:
    """Return the reasons ``on_disk`` is stale relative to ``expected``; empty means fresh."""
    reasons: list[str] = []
    if on_disk.bank_schema != expected.bank_schema:
        reasons.append(f"bank schema {on_disk.bank_schema} != {expected.bank_schema}")
    if on_disk.recipe != expected.recipe:
        reasons.append(f"exemplar recipe {on_disk.recipe!r} != {expected.recipe!r}")
    if on_disk.registry_fingerprint != expected.registry_fingerprint:
        reasons.append(
            "registry has changed since the bank was built "
            f"({on_disk.registry_fingerprint[:12]} != {expected.registry_fingerprint[:12]})"
        )
    if on_disk.checkpoint_digest != expected.checkpoint_digest:
        reasons.append(
            "checkpoint has changed since the bank was built "
            f"({on_disk.checkpoint_digest[:12]} != {expected.checkpoint_digest[:12]})"
        )
    if on_disk.max_tokens != expected.max_tokens:
        reasons.append(f"max_tokens {on_disk.max_tokens} != {expected.max_tokens}")
    return reasons


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_bert_exemplars",
        description="Build the per-doctype exemplar bank the optional local-BERT tier needs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add = parser.add_argument
    add("--model-dir", type=Path, default=None,
        help="Mounted checkpoint directory (default: settings.bert_model_dir).")
    add("--out", type=Path, default=None,
        help=f"Output path (default: <settings.data_dir>/{_BANK_NAME}).")
    add("--per-doctype", type=int, default=None,
        help="Exemplars per doctype (default: settings.bert_knn_k, so every class's score "
             "is a mean over the same number of neighbours).")
    add("--max-tokens", type=int, default=None,
        help="Truncation length (default: settings.bert_max_tokens). Must match the "
             "setting the service runs with, or exemplars and queries disagree.")
    add("--device", default="cpu", help="Torch device. Only cpu is reproducible.")
    add("--country", default="", help="Restrict to one country code, for a fast smoke build.")
    add("--dry-run", action="store_true",
        help="Print the exemplar TEXT for every doctype and exit. Loads no model, so it "
             "runs anywhere.")
    add("--check", action="store_true",
        help="Report whether the bank already on disk is stale. Exit 1 if it is.")
    add("--report", type=Path, default=None,
        help="Also write the exemplar texts to this path, for review.")
    return parser.parse_args(argv)


def _resolve(args: argparse.Namespace) -> tuple[Path, Path, int, int]:
    settings = get_settings()
    model_dir = args.model_dir or Path(settings.bert_model_dir)
    out = args.out or Path(settings.data_dir) / _BANK_NAME
    per_doctype = args.per_doctype if args.per_doctype is not None else settings.bert_knn_k
    max_tokens = args.max_tokens if args.max_tokens is not None else settings.bert_max_tokens
    return Path(model_dir), Path(out), per_doctype, max_tokens


def _selected_specs(country: str) -> list[DocTypeSpec]:
    specs = list(all_specs())
    if country:
        specs = [s for s in specs if s.country.upper() == country.upper()]
        if not specs:
            raise SystemExit(f"no doctypes for country {country!r}")
    return specs


def _run_dry(specs: Sequence[DocTypeSpec], per_doctype: int) -> int:
    total = 0
    for spec in sorted(specs, key=lambda s: s.doctype_id):
        texts = exemplar_texts(spec, limit=per_doctype)
        total += len(texts)
        print(f"\n=== {spec.doctype_id}  ({len(texts)} exemplars)")
        for i, text in enumerate(texts, 1):
            print(f"  [{i}] " + text.replace("\n", "\n      | "))
    print(f"\n{len(specs)} doctypes, {total} exemplars")
    return 0


def _run_check(
    out: Path, model_dir: Path, fingerprint: str, digest: str,
    files: dict[str, str], per_doctype: int, max_tokens: int,
) -> int:
    if not out.is_file():
        print(f"NO BANK: {out} does not exist — L3 is off (tier_available() is False).")
        return 1
    try:
        on_disk = read_meta(out)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"UNREADABLE: {exc}")
        return 1
    expected = BankMeta(
        bank_schema=BANK_SCHEMA, recipe=EXEMPLAR_RECIPE,
        registry_fingerprint=fingerprint, checkpoint_digest=digest,
        checkpoint_files=files, model_dir=str(model_dir), dim=on_disk.dim,
        max_tokens=max_tokens, per_doctype=per_doctype,
        n_doctypes=on_disk.n_doctypes, n_vectors=on_disk.n_vectors,
    )
    reasons = compare_meta(on_disk, expected)
    if reasons:
        print(f"STALE: {out}")
        for reason in reasons:
            print(f"  - {reason}")
        return 1
    print(f"FRESH: {out} ({on_disk.n_doctypes} doctypes, {on_disk.n_vectors} vectors, "
          f"dim {on_disk.dim})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    model_dir, out, per_doctype, max_tokens = _resolve(args)
    specs = _selected_specs(args.country)

    if args.dry_run:
        return _run_dry(specs, per_doctype)

    fingerprint = registry_fingerprint(specs, per_doctype=per_doctype, max_tokens=max_tokens)
    digest, files = checkpoint_identity(model_dir)

    if args.check:
        return _run_check(
            out, model_dir, fingerprint, digest, files, per_doctype, max_tokens
        )

    shadow = out.parent / _SHADOWING_NAME
    if shadow.is_file():
        raise SystemExit(
            f"{shadow} exists and would shadow {out}: dce.classify.bert_knn._EXEMPLAR_NAMES "
            "prefers the .npz. Remove it, or pass --out. (This tool writes JSON on purpose: "
            "np.savez stamps zip members with the build time, so an .npz can never be "
            "byte-identical across runs.)"
        )

    encoder = _load_encoder(model_dir, max_tokens, args.device)
    vectors, texts = build_bank(specs, encoder, per_doctype=per_doctype)
    if not vectors:
        raise SystemExit("no exemplars were produced — refusing to write an empty bank.")

    dims = {len(row) for rows in vectors.values() for row in rows}
    if len(dims) != 1:
        raise SystemExit(f"inconsistent vector dimensionality: {sorted(dims)}")

    meta = BankMeta(
        bank_schema=BANK_SCHEMA,
        recipe=EXEMPLAR_RECIPE,
        registry_fingerprint=fingerprint,
        checkpoint_digest=digest,
        checkpoint_files=files,
        model_dir=str(model_dir),
        dim=dims.pop(),
        max_tokens=max_tokens,
        per_doctype=per_doctype,
        n_doctypes=len(vectors),
        n_vectors=sum(len(rows) for rows in vectors.values()),
    )
    write_bank(out, vectors, meta)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            "\n".join(
                f"{doctype_id}\n" + "\n".join(
                    f"    [{i}] " + t.replace("\n", " | ") for i, t in enumerate(items, 1)
                )
                for doctype_id, items in sorted(texts.items())
            ) + "\n",
            encoding="utf-8",
        )

    counts = sorted(len(rows) for rows in vectors.values())
    print(f"wrote {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    print(f"  doctypes      : {meta.n_doctypes}")
    print(f"  vectors       : {meta.n_vectors} (dim {meta.dim}), "
          f"{counts[0]}-{counts[-1]} per doctype")
    print(f"  recipe        : {meta.recipe}")
    print(f"  registry      : {meta.registry_fingerprint[:16]}")
    print(f"  checkpoint    : {meta.checkpoint_digest[:16]}  <- {model_dir}")
    print("  NOTE: this file is a build artefact under data/, which is gitignored. "
          "Do not commit it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
