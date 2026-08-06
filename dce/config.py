"""Settings for the Document Classification & Extraction service (DCE).

The service is deliberately deployable on its own: other business units send it documents
that have NOT been classified yet, which is exactly why it must not talk to any external
model before a document type is known.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # ---- Server ----
    host: str = "0.0.0.0"
    port: int = 8200
    api_key: str = ""            # optional X-API-Key; empty disables auth
    data_dir: str = "./data"

    # ---- THE INVARIANT ------------------------------------------------------
    #: No network egress is permitted before a document is classified. Other business
    #: units hand us unclassified documents; shipping their bytes (or their text, or an
    #: embedding OF their text) to a third party before we know what the document is, is
    #: the exact failure this service exists to prevent. Classification therefore runs
    #: ENTIRELY in-process: anchors, checksums and lexical scoring only, plus an optional
    #: LOCAL BERT. Enforced in code by dce.egress and covered by a test.
    #: Turning this off is a deliberate, auditable act — it is not a tuning knob.
    allow_preclassification_egress: bool = False

    # ---- Classification cascade ----
    #: Accept a class when the calibrated probability clears its threshold AND it beats
    #: the runner-up by the margin AND enough of its profile terms were actually seen.
    #: Failing any of the three abstains to UNKNOWN, which routes to a human — never to a
    #: model.
    classify_accept_probability: float = 0.65
    classify_min_margin: float = 0.25
    classify_min_coverage: float = 0.20
    #: Zone weights: a term in a title is worth far more than the same term in repeated
    #: page furniture. This is what makes the scorer better than grep, and it is free —
    #: the roles come from the Layout payload we were handed.
    zone_weight_title: float = 3.0
    zone_weight_heading: float = 2.0
    zone_weight_body: float = 1.0
    zone_weight_table: float = 1.2
    zone_weight_furniture: float = 0.25
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    #: Anchors/checksums are near-certain; they dominate the fused score by design.
    fuse_weight_anchor: float = 3.0
    fuse_weight_lexical: float = 1.0
    fuse_weight_bert: float = 0.8
    softmax_temperature: float = 0.6

    # ---- Optional LOCAL BERT kNN (off by default) ---------------------------
    #: Only enable if the anchor+lexical tiers prove insufficient on your corpus. The
    #: model runs in-process from a mounted directory — no request leaves the container.
    #: NOTE the published checkpoint ships TensorFlow + Flax weights and NO PyTorch
    #: bin/safetensors, so transformers needs from_tf=True (requires tensorflow) or
    #: from_flax=True (requires jax/flax). Neither is a base dependency: install the
    #: `bert` extra deliberately.
    bert_enabled: bool = False
    bert_model_dir: str = "/models/bert_uncased_L-12_H-768_A-12"
    bert_max_tokens: int = 256           # first N tokens; the title zone carries the signal
    bert_knn_k: int = 5
    bert_device: str = "cpu"

    # ---- Extraction ----
    extract_accept_confidence: float = 0.60
    #: Distance (as a fraction of page width/height) to search right-of / below a label
    #: before giving up. Tuned for A4/Letter forms.
    label_window_x: float = 0.55
    label_window_y: float = 0.06
    fuzzy_label_min_score: int = 88      # rapidfuzz partial_ratio floor for a label match

    # ---- Optional upstream (post-classification only) ----
    #: DES supplies the Layout payload when a caller sends a document id instead of the
    #: payload itself. Never called before classification.
    des_url: str = ""
    des_api_key: str = ""

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.bert_enabled and not os.path.isdir(self.bert_model_dir):
            # Loud, not silent: an operator who asked for BERT must know it is absent
            # rather than discover degraded accuracy later.
            raise ValueError(
                f"bert_enabled=true but bert_model_dir does not exist: {self.bert_model_dir}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
