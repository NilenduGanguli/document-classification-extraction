"""L3 — optional local-BERT kNN. Off by default, and imported only when it is on.

The design this service replaces had an embedding-kNN tier that called a remote ``gte``
endpoint. That tier has been **removed**, not ported: embedding an unclassified document
means sending another business unit's customer data to a third party *before* anyone knows
what the document is, which is the exact failure this service exists to prevent.

What remains is a strictly local variant, and it is off unless an operator turns it on:

* the checkpoint is read from a **mounted directory**; nothing is fetched;
* only the TITLE and HEADING zones are encoded, truncated to ``bert_max_tokens`` — the top of
  a document is where the identity of a document lives, and it keeps the tier cheap;
* classification is cosine kNN against per-doctype **exemplar vectors** that were computed
  offline and shipped as a file. No exemplar file means no tier, silently and safely.

This module must never be imported when ``bert_enabled`` is False — importing
:mod:`transformers` costs seconds and pulls a large dependency tree into a container that does
not need it. :mod:`dce.classify.cascade` imports it inside a function, behind the flag, and
there is a test asserting it stays out of ``sys.modules``.

WEIGHT FORMATS, PRECISELY
-------------------------
Two different copies of ``bert_uncased_L-12_H-768_A-12`` are in circulation and they do not
contain the same files. Being vague about which one is in the mount is how an operator ends up
chasing a fix that cannot work:

* **The HuggingFace mirror.** It carries ``pytorch_model.bin`` *and* ``flax_model.msgpack``
  *and* the TensorFlow checkpoint. ``from_pretrained`` reads the ``.bin`` natively and this
  tier just works. (An earlier version of this docstring said the published checkpoint had
  "TensorFlow and Flax weights and no PyTorch bin/safetensors". That is not true of the
  mirror — it has a ``.bin``.)
* **The original Google release, and any company-approved rebuild of it.** That is a
  TensorFlow v1 checkpoint and nothing else::

      bert_config.json  config.json  vocab.txt  README.md
      bert_model.ckpt.data-00000-of-00001  bert_model.ckpt.index  bert_model.ckpt.meta

  No ``pytorch_model.bin``, no ``model.safetensors``, no ``flax_model.msgpack``.

``transformers`` **5.x cannot load the second one at all**, and no install fixes that.
TensorFlow and Flax support were *removed* from the library: there is no ``TFBertModel``, no
``modeling_tf_pytorch_utils``, no ``load_tf_weights_in_bert``, and
``from_pretrained(..., from_tf=True)`` / ``from_flax=True`` are ignored — they fail with the
*same* "no file named model.safetensors, or pytorch_model.bin" as a plain load. This loader
used to try all three and report all three failures, and the message it produced named an
install that would not have helped. The two dead branches are gone.

The supported answer for a TF-only checkpoint is to convert it **once, offline**, with
``tools/convert_bert_tf_checkpoint.py``. It reads the ``.index``/``.data-*`` pair directly —
no TensorFlow needed, and the ``.meta`` graph is never read — and writes ``model.safetensors``
beside them. The runtime then needs ``torch`` + ``transformers`` only, which is the point: one
framework in the image, and no second one to get through an approval process.
"""
from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dce.config import Settings, get_settings
from dce.egress import assert_no_egress
from dce.models import LayoutView, Zone

from .lexical import robust_z, softmax

__all__ = [
    "BertUnavailable",
    "ExemplarBank",
    "LocalBertEncoder",
    "bert_scores",
    "exemplar_path",
    "tier_available",
]

#: Default exemplar file names looked for under ``settings.data_dir``. ``config.Settings`` has
#: no dedicated field yet; when one is added (``bert_exemplars_path``) it is picked up here
#: automatically via ``getattr``, so config and this module can land independently.
_EXEMPLAR_NAMES = ("bert_exemplars.npz", "bert_exemplars.json")


class BertUnavailable(RuntimeError):
    """Raised when the local checkpoint cannot be loaded from the mounted directory."""


def exemplar_path(settings: Settings | None = None) -> Path | None:
    """Resolve the per-doctype exemplar-vector file.

    Args:
        settings: Settings override; defaults to :func:`dce.config.get_settings`.

    Returns:
        The path, or ``None`` when no exemplar file exists — which disables the tier.
    """
    resolved = settings if settings is not None else get_settings()
    configured = getattr(resolved, "bert_exemplars_path", "") or ""
    if configured:
        path = Path(configured)
        return path if path.is_file() else None
    for name in _EXEMPLAR_NAMES:
        candidate = Path(resolved.data_dir) / name
        if candidate.is_file():
            return candidate
    return None


def tier_available(settings: Settings | None = None) -> bool:
    """Whether L3 can run: enabled, a model directory present, and exemplars on disk."""
    resolved = settings if settings is not None else get_settings()
    if not resolved.bert_enabled:
        return False
    if not Path(resolved.bert_model_dir).is_dir():
        return False
    return exemplar_path(resolved) is not None


# ---------------------------------------------------------------------------
# Exemplars
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExemplarBank:
    """Per-doctype exemplar vectors, computed offline.

    Attributes:
        vectors: ``doctype_id -> sequence of L2-normalised vectors``.
        source: Where they were loaded from, for the audit trail.
    """

    vectors: Mapping[str, tuple[tuple[float, ...], ...]] = field(default_factory=dict)
    source: str = ""

    @classmethod
    def load(cls, path: Path) -> ExemplarBank:
        """Load exemplars from ``.json`` (``{doctype_id: [[...], ...]}``) or ``.npz``.

        Args:
            path: File to read.

        Returns:
            The bank, with every vector L2-normalised so scoring is a dot product.

        Raises:
            BertUnavailable: If the file cannot be parsed.
        """
        try:
            if path.suffix == ".npz":
                import numpy as np

                with np.load(path) as payload:
                    raw = {key: payload[key].tolist() for key in payload.files}
            else:
                raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - depends on operator-supplied file
            raise BertUnavailable(f"could not read BERT exemplars at {path}: {exc}") from exc

        vectors: dict[str, tuple[tuple[float, ...], ...]] = {}
        for doctype_id, entries in raw.items():
            rows = entries if entries and isinstance(entries[0], (list, tuple)) else [entries]
            normalised = tuple(_l2(tuple(float(v) for v in row)) for row in rows)
            vectors[str(doctype_id)] = tuple(row for row in normalised if row)
        return cls(vectors=vectors, source=str(path))

    def knn(self, vector: Sequence[float], k: int) -> dict[str, float]:
        """Mean cosine similarity to each doctype's ``k`` nearest exemplars.

        Args:
            vector: The document vector (need not be normalised).
            k: Neighbours per class.

        Returns:
            ``doctype_id -> mean similarity`` in ``[-1, 1]``.
        """
        query = _l2(tuple(float(v) for v in vector))
        if not query:
            return {}
        out: dict[str, float] = {}
        for doctype_id, exemplars in self.vectors.items():
            sims = sorted(
                (sum(a * b for a, b in zip(query, e, strict=False)) for e in exemplars),
                reverse=True,
            )[: max(1, k)]
            if sims:
                out[doctype_id] = sum(sims) / len(sims)
        return out


def _l2(vector: tuple[float, ...]) -> tuple[float, ...]:
    """L2-normalise a vector; an all-zero vector normalises to empty (i.e. unusable)."""
    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 0:
        return ()
    return tuple(v / norm for v in vector)


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------
@dataclass
class LocalBertEncoder:
    """A BERT checkpoint loaded from a mounted directory. Never fetched.

    Attributes:
        tokenizer: The ``transformers`` tokenizer.
        model: The ``transformers`` model, put into inference mode at load.
        max_tokens: Truncation length.
    """

    tokenizer: Any
    model: Any
    max_tokens: int = 256

    @classmethod
    def load(cls, settings: Settings | None = None) -> LocalBertEncoder:
        """Load tokenizer + model from ``settings.bert_model_dir``.

        There is exactly one weight path — ``from_pretrained`` on locally-present PyTorch or
        safetensors weights — because on ``transformers`` 5.x there is exactly one that works.
        See the module docstring.

        Args:
            settings: Settings override; defaults to :func:`dce.config.get_settings`.

        Returns:
            The loaded encoder.

        Raises:
            BertUnavailable: If the directory is missing, ``transformers``/``torch`` are not
                installed, or the directory holds no loadable weights. When the mount looks
                like a TensorFlow-only checkpoint the message says so and names the converter,
                because that is the actual fix.
        """
        resolved = settings if settings is not None else get_settings()
        model_dir = Path(resolved.bert_model_dir)
        if not model_dir.is_dir():
            # The only way transformers could satisfy this load is by reaching a model hub,
            # which is egress. Refuse loudly instead of silently downloading.
            assert_no_egress("bert_knn.hub_download", settings=resolved)
            raise BertUnavailable(
                f"bert_model_dir does not exist: {model_dir}. The checkpoint must be mounted "
                "into the container; this service never downloads one."
            )

        # Belt and braces: even with a valid directory, transformers will happily reach out
        # for a missing auxiliary file unless told not to.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

        try:
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise BertUnavailable(
                "bert_enabled=true but `transformers` is not installed. L3 is optional and its "
                "dependencies are deliberately not in the base install: "
                "`pip install '.[bert]'` (torch + transformers). No other framework is needed "
                "at runtime — a TensorFlow-only checkpoint is converted offline first, with "
                "tools/convert_bert_tf_checkpoint.py."
            ) from exc

        tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
        model = cls._load_weights(model_dir, AutoModel)
        # Inference mode: no dropout, no gradient bookkeeping.
        if hasattr(model, "train"):
            model.train(False)
        return cls(tokenizer=tokenizer, model=model, max_tokens=resolved.bert_max_tokens)

    #: Weight files ``transformers`` 5.x can actually read from a local directory.
    LOADABLE_WEIGHTS = ("model.safetensors", "model.safetensors.index.json", "pytorch_model.bin",
                        "pytorch_model.bin.index.json")
    #: Files that mean "this is a TensorFlow v1 checkpoint" — readable by the offline
    #: converter, and by nothing in this process.
    TF_CHECKPOINT_GLOBS = ("*.ckpt.index", "*.ckpt.data-*", "*.ckpt.meta")

    @classmethod
    def _load_weights(cls, model_dir: Path, auto_model: Any) -> Any:
        """Load PyTorch/safetensors weights, or explain precisely why there are none.

        ``from_tf=True`` and ``from_flax=True`` used to be tried here as fallbacks. They are
        not tried any more: ``transformers`` 5.x removed both frameworks, so the flags are
        accepted and ignored and every attempt fails with the identical missing-file error.
        Keeping them made the failure look like three independent problems and made the error
        message recommend an install that changes nothing.

        Args:
            model_dir: The mounted checkpoint directory.
            auto_model: ``transformers.AutoModel``.

        Returns:
            The loaded model.

        Raises:
            BertUnavailable: With a message that distinguishes "no weights at all" from "a
                TensorFlow checkpoint that has not been converted yet", because those have
                different fixes.
        """
        try:
            return auto_model.from_pretrained(str(model_dir), local_files_only=True)
        except Exception as exc:
            # A missing file, an unreadable file and a bad config all surface here as different
            # exception types; the operator needs the same diagnosis below either way, and it
            # is re-raised as BertUnavailable rather than swallowed.
            has_weights = any((model_dir / name).exists() for name in cls.LOADABLE_WEIGHTS)
            is_tf_only = not has_weights and any(
                any(model_dir.glob(pattern)) for pattern in cls.TF_CHECKPOINT_GLOBS
            )
            if is_tf_only:
                raise BertUnavailable(
                    f"{model_dir} holds a TensorFlow v1 checkpoint (bert_model.ckpt.*) and no "
                    "PyTorch or safetensors weights. transformers 5.x cannot read it: TF and "
                    "Flax support were REMOVED from the library, so `from_tf=True` and "
                    "`from_flax=True` no longer do anything and installing tensorflow or "
                    "jax/flax will NOT help. Convert it once, offline, then restart:\n"
                    f"    python tools/convert_bert_tf_checkpoint.py convert {model_dir}\n"
                    "That writes model.safetensors beside the checkpoint, needs no TensorFlow, "
                    "and downloads nothing. Underlying error: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            raise BertUnavailable(
                f"could not load a BERT model from {model_dir}. Expected one of "
                f"{', '.join(cls.LOADABLE_WEIGHTS[:3])} in that directory; it has "
                f"{', '.join(sorted(p.name for p in model_dir.iterdir())[:8]) or '(nothing)'}. "
                "This service never downloads a checkpoint — mount a complete one. "
                f"Underlying error: {type(exc).__name__}: {exc}"
            ) from exc

    def encode(self, text: str) -> tuple[float, ...]:
        """Mean-pool the last hidden state over the first ``max_tokens`` tokens.

        Args:
            text: Text to encode (already restricted to the title/heading zones).

        Returns:
            The pooled vector, or an empty tuple for empty input.
        """
        if not text.strip():
            return ()
        import torch

        encoded = self.tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=self.max_tokens,
            padding=False,
        )
        with torch.no_grad():
            hidden = self.model(**encoded).last_hidden_state[0]
            mask = encoded.get("attention_mask")
            if mask is not None:
                weights = mask[0].unsqueeze(-1).to(hidden.dtype)
                pooled = (hidden * weights).sum(dim=0) / weights.sum().clamp(min=1)
            else:
                pooled = hidden.mean(dim=0)
        return tuple(float(v) for v in pooled.tolist())


def title_heading_text(view: LayoutView) -> str:
    """Concatenate the TITLE and HEADING zones — where a document says what it is.

    Falls back to the first few body blocks when a payload carries no roles at all, so a
    provider without role detection is degraded rather than silenced.

    Args:
        view: The layout payload.

    Returns:
        The text to encode.
    """
    parts = [b.text for b in view.blocks if b.zone in (Zone.title, Zone.heading)]
    if not parts:
        parts = [b.text for b in view.blocks if b.zone is Zone.body][:5]
    return "\n".join(p for p in parts if p.strip())


def bert_scores(
    view: LayoutView,
    *,
    settings: Settings | None = None,
    encoder: LocalBertEncoder | None = None,
    bank: ExemplarBank | None = None,
) -> dict[str, float]:
    """Score doctypes by cosine kNN against the exemplar bank.

    Args:
        view: The layout payload.
        settings: Settings override; defaults to :func:`dce.config.get_settings`.
        encoder: Pre-loaded encoder (loading is expensive; the caller should cache it).
        bank: Pre-loaded exemplars.

    Returns:
        ``doctype_id -> probability`` in the same units as the lexical tier, so the fusion
        weights are comparable. Empty when the tier cannot run.
    """
    resolved = settings if settings is not None else get_settings()
    resolved_bank = bank
    if resolved_bank is None:
        path = exemplar_path(resolved)
        if path is None:
            return {}
        resolved_bank = ExemplarBank.load(path)

    text = title_heading_text(view)
    if not text:
        return {}

    resolved_encoder = encoder or LocalBertEncoder.load(resolved)
    vector = resolved_encoder.encode(text)
    if not vector:
        return {}

    similarities = resolved_bank.knn(vector, resolved.bert_knn_k)
    if not similarities:
        return {}
    return {
        k: round(v, 6)
        for k, v in softmax(robust_z(similarities), resolved.softmax_temperature).items()
    }
