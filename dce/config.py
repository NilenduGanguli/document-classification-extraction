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
    # Acceptance is a conjunction of four conditions, and every one of them is measured on a
    # quantity that depends ONLY on the document and the doctype being tested — never on how
    # many other doctypes happen to be installed. See dce.classify.cascade for the rule and
    # for why the previous formulation could not be repaired by moving a number.
    #
    # ``classify_accept_probability`` used to live here, at 0.65. It was the accept floor,
    # applied to ``softmax(fused)[top]`` — a probability normalised over the whole registry, so
    # every doctype ever added lowered it for every document, related or not. Measured on one
    # unchanged US W-9 against identical evidence: p = 0.900 at 25 doctypes, 0.411 at 121.
    # It left the accept path in the previous change and was kept defined "because the
    # short-circuit reports a confidence above it". That short-circuit no longer exists, and a
    # setting that nothing reads is a control-review hazard: it appears in `/readyz`, in the
    # container's environment and in a reviewer's mental model of what governs the decision,
    # while governing nothing. Removed rather than deprecated for that reason.
    #
    #: The minimum LEAD the winning doctype must hold over the next-best doctype on the
    #: combined support channel ``S = 1 - (1 - anchor)(1 - explained)``. Not a difference of
    #: two registry-normalised probabilities: it is "how large a gap on absolute evidence
    #: counts as a real gap", a property of the evidence scale and not of the registry.
    #:
    #: **This value is a judgement call, not a calibration, and the previous claim that it was
    #: calibrated does not reproduce.** It was documented as sitting on "a plateau where the
    #: wrong-answer count is unchanged from 0.02 to 0.20, while recall only begins to erode
    #: above ~0.20". Re-swept on the reference corpus against the quantity now being
    #: thresholded (61 text-layer documents; margin 0.00 / 0.02 / 0.04 / 0.08 / 0.16 / 0.32,
    #: other floors at default), the correct/wrong counts are:
    #:
    #:     0.00 -> 43/1   0.02 -> 38/1   0.04 -> 36/1   0.08 -> 32/1   0.16 -> 21/1
    #:
    #: There is no plateau. Recall erodes immediately and monotonically from 0.00 upward, and
    #: the wrong-answer count is *flat at one* across the whole range — on this corpus the
    #: margin floor buys no precision at all and costs seven correct answers at 0.04. Sixty-one
    #: documents cannot settle that: a corpus with one wrong answer in it has no power to
    #: measure a control whose job is to prevent wrong answers, and lowering the floor because
    #: this corpus did not punish it would be fitting a constant to sixty-one files.
    #:
    #: So the value is unchanged and the reasoning is stated instead of a measurement: a lead
    #: of under four points of combined evidence out of a hundred is not distinguishable from
    #: noise on a channel whose inputs are an OCR-dependent anchor match and a BM25 saturation.
    #: The floor also sets the confidence scale — ``lead / (lead + this)`` is one of the three
    #: factors of the reported confidence — so it shapes every document's headline number
    #: whether or not it refuses one.
    #:
    #: **What this is a floor ON changed in round 3, and the value did not.** It used to be
    #: compared against ``S[1] - S[2]``, a difference of two supports each bounded above by 1
    #: and each built on an anchor score clipped at 0.97. That is unfixable by any choice of
    #: number, for an arithmetic reason: once ``S[2] > 0.96`` there is less than 0.04 of
    #: headroom left below the bound, so the gate was **unsatisfiable at any level of evidence
    #: for the winner** — and the anchor tier reaches 0.97 at 5.06 bits, which a title zone's
    #: 2.0x multiplier makes routine. Measured consequence: the same 59 corpus documents scored
    #: 36 correct with every block read as ``body`` and 32 correct with title/heading zones
    #: present, all five losses refused by this gate with the winner's support at 0.98.
    #: Production always has zones, so the better number was the one production could never
    #: produce.
    #:
    #: It is now compared against ``1 - 2^-(B[1] - B[2])`` where ``B`` is the same evidence in
    #: bits (``dce.classify.cascade.separation_of``). Same units, same ``[0, 1)`` range, and
    #: the same meaning wherever nothing is saturated — the two agree to first order as
    #: ``S -> 0`` — so this is not a re-tuning wearing a rewrite's clothes. Where they differ
    #: is where the old one was broken: the ratio has no ceiling to be squeezed against, so
    #: evidence that strengthens the winner and the rival together can no longer *shrink* the
    #: measured separation.
    #:
    #: Read as a likelihood ratio, 0.04 asks the winner to be about 1.04x better supported than
    #: the runner-up — 0.059 bits. That is a weak bar, and weaker than the old rule's effective
    #: bar on well-evidenced documents (which was infinite). **Whether the floor should be
    #: restated in bits at a level like "one distinguishing anchor" (~0.15 bits, the smallest
    #: increment the anchor channel can produce) is a live question this corpus cannot settle**,
    #: for the same reason recorded above: one wrong answer in 59 documents has no power to
    #: measure a precision control. It needs labelled production data, and it is recorded in
    #: the residual risks rather than guessed at here.
    classify_min_margin: float = 0.04
    #: Absolute backstop: the noisy-OR of the winner's own two channels,
    #: ``1 - (1 - anchor)(1 - explained)``, must clear this. A lead says the winner beat its
    #: rivals; this says the winner is actually supported.
    #:
    #: **It is a redundant control on the reference corpus and that is stated rather than
    #: hidden.** It fires on 5 of the 61 text-layer documents, but on every one of them another
    #: gate refuses too, so deleting it would change no verdict there (swept: the accepted set
    #: is identical for every floor from 0.00 to 0.70). It is kept because it is the *only*
    #: control that can catch one specific shape of registry-authoring accident, and that shape
    #: is pinned by a test rather than asserted here: a doctype that declares exactly one weak
    #: anchor has coverage 1.0 the moment that anchor matches — coverage is a fraction of
    #: declared anchors, so it cannot refuse a one-anchor doctype at any floor — and if no
    #: other doctype scores at all, its lead over a field of nothing clears any margin. Neither
    #: of the other two gates can see it. See, in ``tests/test_classify.py``,
    #: ``test_support_floor_is_the_only_gate_that_can_refuse_thin_evidence``.
    classify_min_support: float = 0.30
    #: The fraction of the winning class's own vocabulary that must have been present, taken
    #: as ``max(profile coverage, anchor coverage)``. The one control the cascade has always
    #: called load-bearing, and it is kept live and unchanged in value: it is why a photo-ID
    #: specimen sheet with almost no text abstains cheaply instead of being argued about.
    #: It is also the gate that stopped being decorative when the L1 short-circuits were
    #: deleted. Five corpus documents used to be accepted below it, because the short-circuit
    #: returned before it was evaluated. Two of those five are now abstentions — a Canadian
    #: permanent-resident card that was being accepted as a US green card, and a Mexican INE
    #: card. The other three still accept, and report a *higher* coverage than before rather
    #: than a suppressed one: the short-circuit reported anchor coverage alone because it never
    #: ran the lexical tier, so the number it published understated them.
    #:
    #: On the corpus it fires on 6 of 24 abstentions and is the sole reason for 1 of them.
    classify_min_coverage: float = 0.20
    #: Zone weights: a term in a title is worth far more than the same term in repeated
    #: page furniture. The roles come from the Layout payload we were handed, so this is free.
    #:
    #: **What they can and cannot do to a VERDICT, stated here because it is not obvious from
    #: the numbers and because a reviewer will ask.** Round 3 made the accept rule read the
    #: *un-promoted* reading of a payload: every block whose zone weighs more than ``body`` is
    #: re-read at ``body`` before the four gates are evaluated
    #: (``dce.classify.cascade._unpromoted_view``). So the three weights ABOVE body —
    #: ``title``, ``heading``, ``table`` — no longer change accept-or-abstain. They still shape
    #: the audit trail (``lexical.raw``, ``probability``, the fused runners-up list, the anchor
    #: evidence lines) and they are still what the extraction tiers read; they are not inert,
    #: but they are not deciding either, and calling them "what makes the scorer better than
    #: grep" would now be overstating them.
    #:
    #: This is deliberate and one-directional. A layout provider's ``title`` role is an
    #: opinion, and Azure Document Intelligence routinely gives it to captions, watermarks and
    #: marketing copy; at 3.0x lexical and 2.0x anchor it was worth up to four bits of
    #: manufactured evidence. Measured by injecting ONE line carrying another doctype's
    #: decisive anchor into eight correctly-classified documents, once labelled ``title`` and
    #: once ``body`` — identical text, only the label differing: 23 confident wrong answers
    #: versus 3 before, 5 versus 5 after. The label now buys nothing.
    #:
    #: ``furniture``, below body, is the exception and stays live in the decision: it can only
    #: ever *lower* a score, so a wrong ``furniture`` label costs an accept (an abstention,
    #: which routes to a human) and can never buy a wrong one. It is the control that stops a
    #: term repeated in every page footer reading as evidence, and it is load-bearing — see
    #: ``test_support_floor_is_the_only_gate_that_can_refuse_thin_evidence`` in
    #: ``tests/test_classify.py``.
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

    # ---- EXTRACTION TIERS 2-4: EGRESS, AND THEREFORE OFF ---------------------
    # Everything from here to the review queue can make a network call. Every one of them
    # defaults to off/empty, and that is the whole point: a deployment that wants **no
    # egress at all** gets it by doing nothing. There is no flag to find and no endpoint to
    # unset — the zero-egress build is the one you get if you never read this section.
    #
    # This does not contradict the invariant above, it is the other half of it. The rule is
    # directional: nothing leaves before a doctype is known; once the cascade has *accepted*
    # a doctype, the caller knows what they are holding and can decide where it may go. So
    # every tier below is required to refuse to run when the classification abstained
    # (``doctype_id == UNKNOWN``) — that assertion lives in the tier modules and is enforced
    # again at the call site in :mod:`dce.api.routes`.
    #
    # Each one also costs money per page, which is why they escalate rather than run
    # together: T2 only sees fields T1 could not find, T3 only what T2 missed, T4 only what
    # is still empty after that. Watch ``dce_extraction_tier_cost_calls_total{tier}``.

    # ---- T2/T3: Azure Document Intelligence ----------------------------------
    #: Azure DI resource endpoint, e.g. ``https://<resource>.cognitiveservices.azure.com``.
    #: Empty by default: with no endpoint there is nowhere for a document to go, so the
    #: absence of configuration is itself a control, not just a missing convenience.
    azure_di_endpoint: str = ""
    #: Azure DI key. Inject it from a secret store at runtime; never bake it into an image.
    azure_di_key: str = ""
    #: Pinned rather than "latest" on purpose — a silently newer API version changes field
    #: names and confidence semantics under a KYC pipeline that has already been signed off.
    azure_di_api_version: str = "2024-11-30"

    #: T2 — Azure prebuilt specialists (``prebuilt-idDocument``, ``-invoice``, ``-receipt``,
    #: ``-tax.us.w2`` …). Off by default because it is egress *and* per-page spend, and
    #: because it only helps document types Azure actually ships a model for; for everything
    #: else it is a paid round-trip that returns what T1 already had.
    t2_enabled: bool = False

    #: T3 — Azure ``queryFields``: ask the layout model for named fields no prebuilt model
    #: covers. Off by default for the same two reasons as T2, plus a third: a query field is
    #: a free-text request derived from the schema, so it is the first tier where *we* write
    #: the prompt and can therefore write it wrong.
    t3_enabled: bool = False
    #: Azure caps queryFields per request and bills per field. The cap is here so a doctype
    #: with 60 fields cannot turn one page into one enormous invoice line; the resolver asks
    #: for the highest-value missing fields first and stops.
    t3_max_query_fields: int = 20

    # ---- T4: constrained LLM -------------------------------------------------
    #: T4 — a schema-constrained LLM, last resort for fields no deterministic locator and no
    #: Azure model could bind. Off by default because it is the most expensive tier, the
    #: least predictable, and the only one that can produce a fluent, plausible, wrong value.
    #: Everything it returns is validated against the same FieldSpec pattern/validator as T1
    #: and is never promoted above ``format_valid`` without a checksum.
    t4_enabled: bool = False
    #: OpenAI-compatible base URL. Point it at a self-hosted model to keep documents inside
    #: your own boundary; the tier does not care whose model it is.
    llm_base_url: str = ""
    llm_api_key: str = ""
    #: Pin the model id. "Whatever is newest" is not a reproducible extraction contract.
    llm_model: str = ""
    #: How long to wait for a completion. Short on purpose: T4 runs inside a request a caller
    #: is holding open, and the fallback — the field goes to a human — is where it was going
    #: anyway. Mirrors the tier's own default.
    llm_timeout_seconds: float = 20.0
    #: Cap on how much document text is put in the prompt. The tier sends a *window* around the
    #: missing fields' labels rather than the whole document: less to leak, less to pay for,
    #: and a shorter haystack for a model that is being asked to quote from it.
    llm_max_window_chars: int = 6000

    # ---- T5: human review queue ---------------------------------------------
    #: The queue is *not* egress and is not optional — it is where every abstention and every
    #: unverified required field lands. ``memory`` keeps it in-process, which is honest for a
    #: single replica and a demo and loses everything on restart; ``file`` persists it to
    #: ``review_queue_path``. Anything durable and shared (a database, a workflow tool) is
    #: the deploying team's to own — see docs/DESIGN.md §12.
    review_queue_backend: str = "memory"
    #: Where the ``file`` backend writes. Under ``data_dir`` by default so the container's
    #: read-only root filesystem still works (``/app/data`` is the one writable mount).
    review_queue_path: str = "./data/review_queue.json"

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.bert_enabled and not os.path.isdir(self.bert_model_dir):
            # Loud, not silent: an operator who asked for BERT must know it is absent
            # rather than discover degraded accuracy later.
            raise ValueError(
                f"bert_enabled=true but bert_model_dir does not exist: {self.bert_model_dir}"
            )
        return self

    def tier_problems(self) -> dict[str, str]:
        """Tiers that are switched on but cannot work, mapped to why.

        Deliberately **not** a validator that raises. A missing BERT directory is a boot-time
        lie about a local file and can only be a mistake; a half-configured paid tier is
        usually a deploy whose secret has not landed yet, and taking the whole service down
        for it would trade a degraded extraction for a total outage of classification —
        which still works, still abstains correctly, and is the part nobody is allowed to
        lose. The problems surface on ``/readyz`` (as *degraded*, not *not-ready*), in the
        ``tiers_used`` block of a ``/process`` response, and in the logs.

        Returns:
            ``{tier_id: reason}`` for each enabled-but-unusable tier; empty when fine.
        """
        problems: dict[str, str] = {}
        if self.t2_enabled and not (self.azure_di_endpoint and self.azure_di_key):
            problems["t2_azure_prebuilt"] = "t2_enabled=true but azure_di_endpoint/key is empty"
        if self.t3_enabled and not (self.azure_di_endpoint and self.azure_di_key):
            problems["t3_azure_query"] = "t3_enabled=true but azure_di_endpoint/key is empty"
        if self.t4_enabled and not (self.llm_base_url and self.llm_model):
            problems["t4_llm"] = "t4_enabled=true but llm_base_url/llm_model is empty"
        return problems

    def egress_tiers(self) -> tuple[str, ...]:
        """Ids of the post-classification tiers that are switched on.

        Empty on a default deployment, which is the answer a control reviewer is asking for.
        """
        enabled = (
            ("t2_azure_prebuilt", self.t2_enabled),
            ("t3_azure_query", self.t3_enabled),
            ("t4_llm", self.t4_enabled),
        )
        return tuple(name for name, on in enabled if on)


@lru_cache
def get_settings() -> Settings:
    return Settings()
