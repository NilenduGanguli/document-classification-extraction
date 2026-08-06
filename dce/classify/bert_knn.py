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

The published ``bert_uncased_L-12_H-768_A-12`` checkpoint ships **TensorFlow and Flax weights
and no PyTorch bin/safetensors**, so a plain ``from_pretrained`` fails on a base install. The
loader tries the native path, then ``from_tf``, then ``from_flax``, and if all three fail it
says which extra to install rather than emitting a stack trace about a missing file.
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

        Tries the native weights, then ``from_tf=True``, then ``from_flax=True``, because the
        published checkpoint ships TF and Flax weights only.

        Args:
            settings: Settings override; defaults to :func:`dce.config.get_settings`.

        Returns:
            The loaded encoder.

        Raises:
            BertUnavailable: If the directory is missing, ``transformers`` is not installed,
                or none of the three weight formats can be read. The message names the extra
                to install.
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
                "bert_enabled=true but `transformers` is not installed. Install the optional "
                "extra deliberately: `pip install 'dce[bert-tf]'` (TensorFlow weights) or "
                "`pip install 'dce[bert-flax]'` (Flax weights)."
            ) from exc

        tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
        model = cls._load_weights(model_dir, AutoModel)
        # Inference mode: no dropout, no gradient bookkeeping.
        if hasattr(model, "train"):
            model.train(False)
        return cls(tokenizer=tokenizer, model=model, max_tokens=resolved.bert_max_tokens)

    @staticmethod
    def _load_weights(model_dir: Path, auto_model: Any) -> Any:
        """Try native, then TensorFlow, then Flax weights."""
        failures: list[str] = []
        for label, kwargs in (
            ("native (pytorch/safetensors)", {}),
            ("from_tf", {"from_tf": True}),
            ("from_flax", {"from_flax": True}),
        ):
            try:
                return auto_model.from_pretrained(
                    str(model_dir), local_files_only=True, **kwargs
                )
            except Exception as exc:  # noqa: BLE001 - each weight format fails in its own way
                # (missing file, missing framework, bad config); we collect all three and
                # report them together, which is the actionable message.
                # pragma: no cover - depends on the mounted checkpoint
                failures.append(f"{label}: {type(exc).__name__}: {exc}")
        raise BertUnavailable(
            f"could not load a BERT checkpoint from {model_dir}. The published "
            "bert_uncased_L-12_H-768_A-12 checkpoint ships TensorFlow and Flax weights and no "
            "PyTorch bin/safetensors, so `from_tf=True` needs `tensorflow` and `from_flax=True`"
            " needs `jax`+`flax`. Install one extra deliberately: `pip install 'dce[bert-tf]'` "
            "or `pip install 'dce[bert-flax]'`. Attempts:\n  - " + "\n  - ".join(failures)
        )

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
