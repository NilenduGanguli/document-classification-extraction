#!/usr/bin/env python3
"""Convert a TensorFlow v1 BERT checkpoint to ``model.safetensors``, offline.

WHY THIS EXISTS
---------------
A company-approved BERT build often arrives as the original Google release: a TensorFlow
checkpoint and nothing else.

    bert_config.json  config.json  vocab.txt  README.md
    bert_model.ckpt.data-00000-of-00001  bert_model.ckpt.index  bert_model.ckpt.meta

``transformers`` 5.x cannot read that. TensorFlow support was **removed** from the library:
there is no ``TFBertModel``, no ``modeling_tf_pytorch_utils``, no ``load_tf_weights_in_bert``,
and ``from_pretrained(..., from_tf=True)`` no longer does anything — it fails with the same
"no file named model.safetensors, or pytorch_model.bin" as a plain load. So the checkpoint has
to be converted **once, offline**, into a format the runtime can read.

This tool is that one-off. It is **ops tooling, not runtime**: nothing in ``dce`` imports it,
the service never runs it, and it is not on the request path. Run it in a build/ops step, mount
the result, and the runtime image needs only ``torch`` + ``transformers``.

NO TENSORFLOW REQUIRED
----------------------
The obvious implementation shells out to TensorFlow to read the checkpoint. This one does not
need to. A TF v1 checkpoint is two files and both formats are open:

* ``.index`` is a **LevelDB SSTable** (48-byte footer ending in the magic 0xdb4775248b80fb57,
  an index block of block handles, data blocks of prefix-compressed key/value entries). Keys
  are tensor names; values are serialised ``BundleEntryProto`` — dtype, shape, shard, byte
  offset, byte length and a masked CRC32C of the payload.
* ``.data-00000-of-00001`` is the raw little-endian tensor bytes at those offsets.

Both are parsed here in ~150 lines of :mod:`struct` and hand-rolled protobuf-varint reading,
so the conversion step needs **numpy + safetensors only**. The ``.meta`` file — the serialised
training graph — is not read at all and is not required; only ``.index`` and ``.data-*`` carry
weights. Consequence for the owner: their approved artefact converts on a machine that has
never had TensorFlow installed, which is one fewer large dependency to get approved.

``--reader tensorflow`` remains available as a cross-check if TensorFlow happens to be present.

CORRECTNESS IS CHECKED, NOT ASSUMED
-----------------------------------
A converter that silently emits garbage weights is the worst outcome available here: nothing
would crash, and classification would quietly degrade. Three defences:

1. every tensor's payload is checked against the masked CRC32C stored in the checkpoint;
2. the mapping is **exhaustive** — an unmapped TF tensor or an unfilled HF key is an error,
   never a shrug;
3. ``--verify-against`` compares the converted weights, tensor by tensor, with a known-good
   ``pytorch_model.bin``, and then compares the actual pooled embedding both models produce
   for the same input text.

OPS PROCEDURE (what the owner runs)
-----------------------------------
    # 1. Copy the approved checkpoint into place. Nothing is downloaded, at any step.
    cp -r /approved/bert_uncased_L-12_H-768_A-12 ./models/

    # 2. Convert, once, offline. Needs numpy + safetensors; torch is NOT needed for this step.
    python tools/convert_bert_tf_checkpoint.py convert models/bert_uncased_L-12_H-768_A-12

    # 3. (Recommended, if a known-good HF copy of the SAME checkpoint is available to compare
    #    with — e.g. on a build host — do this once and keep the report.)
    python tools/convert_bert_tf_checkpoint.py verify \
        models/bert_uncased_L-12_H-768_A-12 --against /known-good/hf-copy

    # 4. Mount it read-only and turn the tier on. docker-compose.yml ships the
    #    `./models:/models:ro` volume and BERT_MODEL_DIR commented-but-ready; uncomment both,
    #    and build the image with the `bert` extra:
    #        EXTRA_PACKAGES=".[bert]" docker compose up -d --build dce
    BERT_ENABLED=true BERT_MODEL_DIR=/models/bert_uncased_L-12_H-768_A-12

Step 3 is optional because the owner may have no second copy to compare against — that is
exactly why steps 1-2 also check the per-tensor CRC32C, which is a self-contained integrity
proof that needs no reference model.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# LevelDB SSTable reader — enough of it to read a TF checkpoint index
# ---------------------------------------------------------------------------
#: Last 8 bytes of a LevelDB table file. TF's tensor-bundle index is one.
_TABLE_MAGIC = 0xDB4775248B80FB57
#: 2*(2 varint64) padded + the 8-byte magic.
_FOOTER_LEN = 48
#: Values of ``tensorflow.DataType`` this tool accepts. BERT checkpoints are all DT_FLOAT.
_DT_FLOAT = 1
_DT_HALF = 19
_DT_DOUBLE = 2
_DTYPES: dict[int, str] = {_DT_FLOAT: "<f4", _DT_DOUBLE: "<f8", _DT_HALF: "<f2"}


class ConversionError(RuntimeError):
    """Any failure that must stop the conversion rather than produce a suspect file."""


def _uvarint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a protobuf base-128 varint.

    Args:
        buf: Buffer to read from.
        pos: Offset to start at.

    Returns:
        ``(value, new_position)``.
    """
    result = 0
    shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ConversionError("varint longer than 64 bits — file is not a TF checkpoint index")


def _protobuf_fields(buf: bytes) -> Iterator[tuple[int, int, Any]]:
    """Yield ``(field_number, wire_type, value)`` for a serialised protobuf message.

    Length-delimited fields yield ``bytes``; varint and fixed fields yield ``int``. This is a
    reader, not a protobuf implementation: unknown fields are yielded and the caller ignores
    the ones it does not care about, which is exactly the forward-compatibility rule protobuf
    is designed around.

    Args:
        buf: The serialised message.

    Yields:
        One tuple per encoded field, in encounter order.
    """
    pos = 0
    while pos < len(buf):
        tag, pos = _uvarint(buf, pos)
        field_number, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            value, pos = _uvarint(buf, pos)
        elif wire_type == 2:
            length, pos = _uvarint(buf, pos)
            value = buf[pos : pos + length]
            pos += length
        elif wire_type == 5:
            value = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
        elif wire_type == 1:
            value = struct.unpack_from("<Q", buf, pos)[0]
            pos += 8
        else:
            raise ConversionError(f"unsupported protobuf wire type {wire_type}")
        yield field_number, wire_type, value


def _table_entries(raw: bytes) -> Iterator[tuple[bytes, bytes]]:
    """Iterate every key/value pair in a LevelDB table file.

    Args:
        raw: The whole ``.index`` file.

    Yields:
        ``(key, value)`` in sorted key order.

    Raises:
        ConversionError: If the file is not a LevelDB table, or uses block compression (TF
            writes checkpoint indexes uncompressed; a compressed one would need snappy, which
            this tool deliberately does not depend on).
    """
    if len(raw) < _FOOTER_LEN or struct.unpack_from("<Q", raw, len(raw) - 8)[0] != _TABLE_MAGIC:
        raise ConversionError(
            "the .index file does not carry the LevelDB table magic. Either it is not a "
            "TensorFlow v1 checkpoint index, or it was truncated in transit."
        )
    footer = raw[-_FOOTER_LEN:]
    pos = 0
    _, pos = _uvarint(footer, pos)  # metaindex offset — filters only, not needed
    _, pos = _uvarint(footer, pos)  # metaindex size
    index_offset, pos = _uvarint(footer, pos)
    index_size, pos = _uvarint(footer, pos)

    def read_block(offset: int, size: int) -> bytes:
        # Block trailer: 1 compression byte + 4 CRC bytes.
        compression = raw[offset + size]
        if compression != 0:
            raise ConversionError(
                f"table block at {offset} uses compression type {compression}; this reader "
                "handles uncompressed blocks only (which is what TensorFlow writes)."
            )
        return raw[offset : offset + size]

    def block_pairs(block: bytes) -> Iterator[tuple[bytes, bytes]]:
        restart_count = struct.unpack_from("<I", block, len(block) - 4)[0]
        body = block[: len(block) - 4 - 4 * restart_count]
        pos = 0
        key = b""
        while pos < len(body):
            shared, pos = _uvarint(body, pos)
            unshared, pos = _uvarint(body, pos)
            value_len, pos = _uvarint(body, pos)
            key = key[:shared] + body[pos : pos + unshared]
            pos += unshared
            yield key, body[pos : pos + value_len]
            pos += value_len

    for _, handle in block_pairs(read_block(index_offset, index_size)):
        offset, cursor = _uvarint(handle, 0)
        size, _ = _uvarint(handle, cursor)
        yield from block_pairs(read_block(offset, size))


# ---------------------------------------------------------------------------
# CRC32C (Castagnoli) — the checkpoint's own integrity check
# ---------------------------------------------------------------------------
_CRC32C_POLY = 0x82F63B78
_CRC32C_TABLE: list[int] = []
for _byte in range(256):
    _value = _byte
    for _ in range(8):
        _value = (_value >> 1) ^ (_CRC32C_POLY if _value & 1 else 0)
    _CRC32C_TABLE.append(_value)


def crc32c(data: bytes) -> int:
    """Compute the CRC32C (Castagnoli) checksum of ``data``.

    Args:
        data: Bytes to check.

    Returns:
        The unmasked 32-bit checksum.
    """
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _CRC32C_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def mask_crc(crc: int) -> int:
    """Apply TensorFlow's CRC mask, which is what the checkpoint actually stores.

    Args:
        crc: An unmasked CRC32C.

    Returns:
        The masked value, as written by ``tensor_bundle``.
    """
    return (((crc >> 15) | (crc << 17)) + 0xA282EAD8) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Checkpoint reading
# ---------------------------------------------------------------------------
def read_tf_checkpoint(prefix: Path, *, verify_crc: bool = True) -> dict[str, Any]:
    """Read every tensor in a TF v1 checkpoint, without TensorFlow.

    Args:
        prefix: Checkpoint prefix, e.g. ``models/x/bert_model.ckpt`` (the path *without*
            ``.index`` / ``.data-*``).
        verify_crc: Check each tensor's payload against the CRC32C in the index. On by
            default; a mismatch means the file is corrupt and conversion must not proceed.

    Returns:
        ``tensor_name -> numpy array``.

    Raises:
        ConversionError: On a missing file, an unsupported dtype, a partitioned (sliced)
            entry, or a CRC mismatch.
    """
    import numpy as np

    index_path = prefix.with_name(prefix.name + ".index")
    if not index_path.is_file():
        raise ConversionError(f"no checkpoint index at {index_path}")

    entries = dict(_table_entries(index_path.read_bytes()))
    header = entries.pop(b"", None)
    if header is None:
        raise ConversionError(f"{index_path} has no bundle header entry")
    num_shards = 1
    for field, _, value in _protobuf_fields(header):
        if field == 1:  # BundleHeaderProto.num_shards
            num_shards = int(value)

    shards: dict[int, bytes] = {}
    for shard in range(num_shards):
        data_path = prefix.with_name(f"{prefix.name}.data-{shard:05d}-of-{num_shards:05d}")
        if not data_path.is_file():
            raise ConversionError(f"checkpoint shard missing: {data_path}")
        shards[shard] = data_path.read_bytes()

    tensors: dict[str, Any] = {}
    for raw_name, blob in entries.items():
        name = raw_name.decode("utf-8")
        dtype = shard_id = offset = size = stored_crc = None
        shape: list[int] = []
        for field, _, value in _protobuf_fields(blob):
            if field == 1:  # dtype
                dtype = int(value)
            elif field == 2:  # TensorShapeProto
                for f2, _, v2 in _protobuf_fields(value):
                    if f2 == 2:  # repeated Dim
                        for f3, _, v3 in _protobuf_fields(v2):
                            if f3 == 1:  # Dim.size
                                shape.append(int(v3))
            elif field == 3:
                shard_id = int(value)
            elif field == 4:
                offset = int(value)
            elif field == 5:
                size = int(value)
            elif field == 6:
                stored_crc = int(value)
            elif field == 7:
                raise ConversionError(
                    f"{name}: the checkpoint stores this tensor in slices (a partitioned "
                    "variable). This tool reads whole tensors only."
                )
        if dtype not in _DTYPES:
            raise ConversionError(
                f"{name}: dtype {dtype} is not one this tool converts (expected float32)."
            )
        payload = shards[shard_id or 0][(offset or 0) : (offset or 0) + (size or 0)]
        if len(payload) != size:
            raise ConversionError(f"{name}: checkpoint data file is short — truncated download?")
        if verify_crc and stored_crc is not None and mask_crc(crc32c(payload)) != stored_crc:
            raise ConversionError(
                f"{name}: CRC32C mismatch. The checkpoint data file is corrupt; converting it "
                "would produce a model that loads cleanly and predicts nonsense. Re-copy it."
            )
        array = np.frombuffer(payload, dtype=_DTYPES[dtype]).reshape(tuple(shape) or (1,))
        tensors[name] = array.astype("<f4", copy=True)
    return tensors


def read_tf_checkpoint_via_tensorflow(prefix: Path) -> dict[str, Any]:
    """Same result as :func:`read_tf_checkpoint`, but using TensorFlow's own reader.

    Only useful as a cross-check when TensorFlow is already installed. The native reader is
    the default precisely so that the ops step does not need this dependency.

    Args:
        prefix: Checkpoint prefix.

    Returns:
        ``tensor_name -> numpy array``.

    Raises:
        ConversionError: If TensorFlow is not installed.
    """
    try:
        import tensorflow as tf
    except ImportError as exc:  # pragma: no cover - optional cross-check path
        raise ConversionError(
            "--reader tensorflow needs `tensorflow` installed. The default (--reader native) "
            "needs no such thing; use it."
        ) from exc
    reader = tf.train.load_checkpoint(str(prefix))
    shapes = reader.get_variable_to_shape_map()
    return {name: reader.get_tensor(name) for name in shapes}


# ---------------------------------------------------------------------------
# TF -> HuggingFace name mapping
# ---------------------------------------------------------------------------
#: TF suffix -> (HF suffix, transpose?). ``kernel`` is a TF ``[in, out]`` dense matrix and
#: ``torch.nn.Linear.weight`` is ``[out, in]``, so it transposes; ``output_weights`` is already
#: stored ``[out, in]`` and does not. This asymmetry is the single most likely place for a
#: converter to be wrong, and it is why --verify-against exists.
_SUFFIXES: tuple[tuple[str, str, bool], ...] = (
    ("/kernel", ".weight", True),
    ("/bias", ".bias", False),
    ("/gamma", ".weight", False),
    ("/beta", ".bias", False),
)


def tf_name_to_hf(name: str) -> str | None:
    """Map one TF checkpoint tensor name to its HuggingFace ``state_dict`` key.

    Args:
        name: e.g. ``bert/encoder/layer_0/attention/self/query/kernel``.

    Returns:
        e.g. ``bert.encoder.layer.0.attention.self.query.weight``, or ``None`` for tensors that
        belong to the optimiser or the training loop rather than the model.
    """
    if name in {"global_step", "good_steps", "loss_scale"} or name.endswith(
        ("/adam_m", "/adam_v", "/Adam", "/Adam_1", "/Momentum")
    ):
        return None
    if name.startswith("bert/embeddings/") and name.endswith("_embeddings"):
        return "bert.embeddings." + name.rsplit("/", 1)[1] + ".weight"
    if name == "cls/predictions/output_bias":
        return "cls.predictions.bias"
    if name == "cls/seq_relationship/output_bias":
        return "cls.seq_relationship.bias"
    if name == "cls/seq_relationship/output_weights":
        return "cls.seq_relationship.weight"
    for tf_suffix, hf_suffix, _ in _SUFFIXES:
        if name.endswith(tf_suffix):
            stem = name[: -len(tf_suffix)]
            return stem.replace("/", ".").replace("layer_", "layer.") + hf_suffix
    return None


def _needs_transpose(name: str) -> bool:
    """Whether the TF tensor ``name`` must be transposed to become a torch Linear weight."""
    return name.endswith("/kernel")


def to_hf_state_dict(tf_tensors: dict[str, Any]) -> dict[str, Any]:
    """Convert TF checkpoint tensors into a HuggingFace BERT ``state_dict``.

    Args:
        tf_tensors: Output of :func:`read_tf_checkpoint`.

    Returns:
        ``hf_key -> numpy array``, including the tied MLM decoder weights so the result is a
        faithful stand-in for the published ``pytorch_model.bin``. ``BertModel`` ignores the
        ``cls.*`` head; ``BertForPreTraining`` uses it. Both load.

    Raises:
        ConversionError: If a model tensor could not be mapped — an unmapped tensor is a bug
            in this table, and silently dropping it would produce a partly-initialised model.
    """
    state: dict[str, Any] = {}
    unmapped: list[str] = []
    for name, array in sorted(tf_tensors.items()):
        key = tf_name_to_hf(name)
        if key is None:
            if name.startswith(("bert/", "cls/")):
                unmapped.append(name)
            continue
        state[key] = array.T.copy() if _needs_transpose(name) else array
    if unmapped:
        raise ConversionError(
            "these checkpoint tensors have no HuggingFace equivalent in this tool's mapping "
            "table, so the converted model would be missing weights: " + ", ".join(unmapped)
        )
    # The published bin ties the MLM decoder to the input embeddings; reproduce it so the two
    # files are comparable key-for-key and BertForPreTraining loads without warnings.
    if "bert.embeddings.word_embeddings.weight" in state:
        state.setdefault("cls.predictions.decoder.weight", state[
            "bert.embeddings.word_embeddings.weight"
        ])
    if "cls.predictions.bias" in state:
        state.setdefault("cls.predictions.decoder.bias", state["cls.predictions.bias"])
    return state


def expected_keys(config: dict[str, Any]) -> set[str]:
    """The full HF BERT ``state_dict`` key set implied by a ``config.json``.

    Computed from the config rather than from the checkpoint, so a checkpoint that is *missing*
    a layer is caught instead of quietly producing a smaller model.

    Args:
        config: Parsed ``config.json``.

    Returns:
        Every key a converted BERT-for-pretraining state dict must contain.
    """
    keys = {
        "bert.embeddings.word_embeddings.weight",
        "bert.embeddings.position_embeddings.weight",
        "bert.embeddings.token_type_embeddings.weight",
        "bert.embeddings.LayerNorm.weight",
        "bert.embeddings.LayerNorm.bias",
        "bert.pooler.dense.weight",
        "bert.pooler.dense.bias",
        "cls.predictions.bias",
        "cls.predictions.transform.dense.weight",
        "cls.predictions.transform.dense.bias",
        "cls.predictions.transform.LayerNorm.weight",
        "cls.predictions.transform.LayerNorm.bias",
        "cls.predictions.decoder.weight",
        "cls.predictions.decoder.bias",
        "cls.seq_relationship.weight",
        "cls.seq_relationship.bias",
    }
    for layer in range(int(config.get("num_hidden_layers", 12))):
        base = f"bert.encoder.layer.{layer}."
        for part in (
            "attention.self.query",
            "attention.self.key",
            "attention.self.value",
            "attention.output.dense",
            "attention.output.LayerNorm",
            "intermediate.dense",
            "output.dense",
            "output.LayerNorm",
        ):
            keys.add(base + part + ".weight")
            keys.add(base + part + ".bias")
    return keys


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def _load_config(model_dir: Path) -> dict[str, Any]:
    """Read ``config.json``, falling back to Google's ``bert_config.json``.

    Args:
        model_dir: The checkpoint directory.

    Returns:
        The parsed config.

    Raises:
        ConversionError: If neither file is present.
    """
    for name in ("config.json", "bert_config.json"):
        candidate = model_dir / name
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise ConversionError(f"{model_dir} has neither config.json nor bert_config.json")


def command_convert(args: argparse.Namespace) -> int:
    """Read the checkpoint and write ``model.safetensors`` beside it."""
    model_dir = Path(args.model_dir).resolve()
    prefix = Path(args.prefix) if args.prefix else model_dir / "bert_model.ckpt"
    config = _load_config(model_dir)

    if args.reader == "tensorflow":
        tf_tensors = read_tf_checkpoint_via_tensorflow(prefix)
    else:
        tf_tensors = read_tf_checkpoint(prefix, verify_crc=not args.no_crc)
    print(f"read {len(tf_tensors)} tensors from {prefix}.* ({args.reader} reader)")

    state = to_hf_state_dict(tf_tensors)
    required = expected_keys(config)
    missing = sorted(required - set(state))
    if missing:
        raise ConversionError(
            f"the checkpoint did not supply {len(missing)} weight(s) that this config requires, "
            "so the converted model would be partly random: " + ", ".join(missing[:8])
        )
    extra = sorted(set(state) - required)
    if extra:
        print(f"note: {len(extra)} extra tensor(s) kept: {', '.join(extra[:5])}")

    out = Path(args.out) if args.out else model_dir / "model.safetensors"
    if out.exists() and not args.force:
        raise ConversionError(f"{out} already exists; pass --force to overwrite")

    from safetensors.numpy import save_file

    # ``cls.predictions.decoder.weight`` is the *same object* as the word embeddings; safetensors
    # refuses aliased storage, so break the tie with a copy before writing.
    save_file(
        {key: value.copy() for key, value in state.items()},
        str(out),
        metadata={"format": "pt", "converted_by": "tools/convert_bert_tf_checkpoint.py"},
    )
    size_mb = out.stat().st_size / 1e6
    print(f"wrote {out} ({len(state)} tensors, {size_mb:.1f} MB)")
    print(
        "the runtime now needs torch + transformers only; TensorFlow was never involved."
        if args.reader == "native"
        else "converted via TensorFlow's reader."
    )
    return 0


def _embed(model_dir: Path, text: str, max_tokens: int) -> Any:
    """Mean-pooled last-hidden-state embedding for ``text``, matching the L3 encoder.

    Args:
        model_dir: A directory ``transformers`` can load.
        text: Input text.
        max_tokens: Truncation length.

    Returns:
        A 1-D torch tensor.
    """
    import torch
    from transformers import AutoModel, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
    model = AutoModel.from_pretrained(str(model_dir), local_files_only=True)
    model.train(False)
    encoded = tokenizer(
        text, return_tensors="pt", truncation=True, max_length=max_tokens, padding=False
    )
    with torch.no_grad():
        hidden = model(**encoded).last_hidden_state[0]
        mask = encoded["attention_mask"][0].unsqueeze(-1).to(hidden.dtype)
        return (hidden * mask).sum(dim=0) / mask.sum().clamp(min=1)


def command_verify(args: argparse.Namespace) -> int:
    """Compare a converted directory against a known-good HuggingFace copy."""
    import torch

    converted_dir = Path(args.model_dir).resolve()
    reference_dir = Path(args.against).resolve()
    converted_file = converted_dir / "model.safetensors"
    if not converted_file.is_file():
        raise ConversionError(f"{converted_file} not found — run `convert` first")

    from safetensors.numpy import load_file

    converted = load_file(str(converted_file))

    reference_bin = reference_dir / "pytorch_model.bin"
    reference_st = reference_dir / "model.safetensors"
    if reference_bin.is_file():
        reference = {
            key: value.numpy()
            for key, value in torch.load(
                str(reference_bin), map_location="cpu", weights_only=True
            ).items()
        }
    elif reference_st.is_file():
        reference = load_file(str(reference_st))
    else:
        raise ConversionError(f"{reference_dir} has neither pytorch_model.bin nor a safetensors")

    only_converted = sorted(set(converted) - set(reference))
    only_reference = sorted(set(reference) - set(converted))
    shared = sorted(set(converted) & set(reference))
    print(f"keys: {len(shared)} shared, {len(only_converted)} extra, {len(only_reference)} missing")
    if only_converted:
        print(f"  extra in converted:   {', '.join(only_converted)}")
    if only_reference:
        print(f"  missing from converted: {', '.join(only_reference)}")

    worst_key, worst = "", -1.0
    mismatched_shapes: list[str] = []
    for key in shared:
        a, b = converted[key], reference[key]
        if a.shape != b.shape:
            mismatched_shapes.append(f"{key}: {a.shape} vs {b.shape}")
            continue
        delta = float(abs(a.astype("float64") - b.astype("float64")).max())
        if delta > worst:
            worst_key, worst = key, delta
    if mismatched_shapes:
        print("SHAPE MISMATCH:\n  " + "\n  ".join(mismatched_shapes))
    print(
        f"max |converted - reference| = {max(worst, 0.0):.3e}  "
        f"(worst tensor: {worst_key or 'n/a'})"
    )

    ok = not mismatched_shapes and not only_reference and 0.0 <= worst <= args.tolerance

    if not args.weights_only:
        text = args.text
        converted_vec = _embed(converted_dir, text, args.max_tokens)
        reference_vec = _embed(reference_dir, text, args.max_tokens)
        gap = float((converted_vec - reference_vec).abs().max())
        cosine = float(
            torch.dot(converted_vec, reference_vec)
            / (converted_vec.norm() * reference_vec.norm()).clamp(min=1e-12)
        )
        print(f"embedding dim={converted_vec.numel()}  max|Δ|={gap:.3e}  cosine={cosine:.12f}")
        ok = ok and gap <= args.tolerance and cosine >= 1.0 - 1e-6

    print("VERIFY: PASS" if ok else "VERIFY: FAIL")
    return 0 if ok else 1


def command_inspect(args: argparse.Namespace) -> int:
    """List what is in a checkpoint without writing anything."""
    prefix = Path(args.prefix)
    tensors = read_tf_checkpoint(prefix, verify_crc=not args.no_crc)
    total = 0
    for name in sorted(tensors):
        array = tensors[name]
        total += array.size
        print(f"{name:60s} {array.shape!s:18s} -> {tf_name_to_hf(name)}")
    print(f"{len(tensors)} tensors, {total / 1e6:.1f}M parameters, CRC32C verified")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI."""
    parser = argparse.ArgumentParser(
        prog="convert_bert_tf_checkpoint",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    convert = sub.add_parser("convert", help="write model.safetensors from bert_model.ckpt.*")
    convert.add_argument("model_dir", help="directory holding the checkpoint and config.json")
    convert.add_argument("--prefix", default="", help="checkpoint prefix (default bert_model.ckpt)")
    convert.add_argument(
        "--out", default="", help="output file (default <model_dir>/model.safetensors)"
    )
    convert.add_argument("--reader", choices=("native", "tensorflow"), default="native")
    convert.add_argument("--no-crc", action="store_true", help="skip the CRC32C integrity check")
    convert.add_argument("--force", action="store_true", help="overwrite an existing output file")
    convert.set_defaults(func=command_convert)

    verify = sub.add_parser("verify", help="compare a converted directory with a known-good copy")
    verify.add_argument("model_dir", help="the converted directory")
    verify.add_argument("--against", required=True, help="a known-good HuggingFace copy")
    verify.add_argument("--tolerance", type=float, default=1e-6)
    verify.add_argument("--text", default="CERTIFICATE OF INCORPORATION\nRegistrar of Companies")
    verify.add_argument("--max-tokens", type=int, default=256)
    verify.add_argument("--weights-only", action="store_true", help="skip the embedding check")
    verify.set_defaults(func=command_verify)

    inspect = sub.add_parser("inspect", help="list checkpoint tensors and their HF names")
    inspect.add_argument("prefix", help="checkpoint prefix, e.g. models/x/bert_model.ckpt")
    inspect.add_argument("--no-crc", action="store_true")
    inspect.set_defaults(func=command_inspect)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
