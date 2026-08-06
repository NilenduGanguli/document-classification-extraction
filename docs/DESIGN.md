# Design — Document Classification & Extraction

> Status: v0.1, initial build. Audience: whoever owns this service next, and whoever has to
> defend it in a control review.

---

## 1. The problem this service exists for

A large organisation has documents arriving from everywhere: onboarding portals, branch
scanners, email attachments, partner SFTP drops. At the moment of arrival, nobody knows what
any of them are. Downstream systems — a KYC engine, a records archive, an LLM summariser —
all need to know two things before they can do anything useful:

1. **What is this document?** A PAN card, a W-9, a passport page, a utility bill, an internal
   memo that should never have been uploaded at all.
2. **What does it say?** The specific fields that document type carries, with enough
   provenance that a human reviewer can check them.

The second question is only answerable after the first, and the first has a nasty property:
answering it with the obvious modern tool — send the text to a model, ask it — is the one thing
you must not do, because you do not yet know what you are sending.

That constraint is the reason this is its own service, its own container, and its own
dependency set.

---

## 2. The invariant

> **Nothing about an unclassified document leaves this process.**
> Not the bytes. Not the extracted text. Not an embedding of the text.

### 2.1 Why an embedding counts as disclosure

The tempting shortcut is "we won't send the document, we'll just embed it and compare
vectors". That is not a smaller disclosure; it is the same disclosure with extra steps. The
text is transmitted to the embedding endpoint in order to be embedded. The vector is derived
from the content, is invertible enough in practice to be treated as personal data, and the
request is logged by whoever runs the endpoint. A control reviewer asking "did unclassified
customer data leave the boundary?" gets the same answer either way: yes.

The original design for this service had an embedding-kNN tier that called a remote `gte`
endpoint. **It was removed.** It is not disabled behind a flag, not left in as an optional
path — it is gone, and the tier that replaced it (L3) runs a checkpoint you mounted, on your
CPU, in this process.

### 2.2 Threat model

| Actor / event | Concern | Control |
|---|---|---|
| An upstream business unit sends a document they have not reviewed | Their data reaches a third party under our name | No network client exists in the classification path |
| A future engineer adds "just one" enrichment call to the classifier | Silent regression of the guarantee | `dce.egress` socket scope + a source-level test |
| An operator flips a config flag under delivery pressure | Guarantee lost without anyone noticing | `/readyz` → 503, `error`-level log, metric |
| A dependency pulls in an HTTP client transitively | Capability exists even if unused | Nothing in the base dependency set requires one; `httpx` is test-only |

### 2.3 How it is enforced, in order of strength

1. **The capability is absent.** `pyproject.toml` has no `httpx`, no `requests`, no
   `aiohttp`, no vendor SDK in the base dependencies. `httpx` appears only in the `dev`
   extra, because `fastapi.testclient` is built on it — and that is called out in a comment so
   nobody promotes it later by accident.
2. **A test greps for it.** `tests/test_api.py::test_no_http_client_in_the_classification_path`
   scans `dce/models.py`, `dce/config.py`, `dce/adapters.py`, `dce/observability.py`,
   `dce/registry/**` and `dce/classify/**` for any network import and fails the build if one
   appears. `dce/extract/**` and `dce/api/**` are excluded on purpose: they run only after a
   doctype has been accepted.
3. **A runtime scope.** `dce.egress` wraps classification in a scope whose socket tripwire
   raises `EgressViolation`, so code that reaches for the network at runtime — including code
   nobody has written yet — fails loudly instead of succeeding quietly.
4. **A visible state.** `allow_preclassification_egress` is reported by `/readyz`, which
   returns **503** when it is on, logged at `error` on boot, and counted by
   `dce_preclassification_egress_blocked_total` when the guard actually stops something.

Layer 1 is the one that matters. The others catch mistakes; layer 1 makes the mistake harder
to make in the first place — and unlike the others it is checkable on the shipped artifact
rather than the source tree:

```console
$ docker run --rm --entrypoint python dce:latest -c \
  "import importlib.util as u; print({m: bool(u.find_spec(m)) for m in ('httpx','requests','aiohttp','openai','azure','boto3')})"
{'httpx': False, 'requests': False, 'aiohttp': False, 'openai': False, 'azure': False, 'boto3': False}
```

That is the answer to give a control reviewer: the running container does not contain the
capability, so the guarantee does not depend on anyone's discipline.

### 2.4 What is *not* restricted

Anything after a doctype is accepted. Once the document is known to be a W-9, the calling
system can make its own, informed decision about where that W-9 may go, and the extraction
tier may legitimately call out (e.g. to DES for a layout payload the caller referenced by id).
The invariant is about the window in which nobody knows what they are handling.

---

## 3. Shape of the system

```
             ┌─────────────────────────────────────────────────────────────┐
  payload    │  dce.adapters                                               │
  ─────────► │    from_azure_layout / from_des_ocr / from_plain_text       │
             │                        ▼                                    │
             │                  LayoutView   (provider-neutral)            │
             │                        │                                    │
             │   ╔════════════════════▼════════════════════╗               │
             │   ║  dce.classify — 100% in-process         ║               │
             │   ║   L0 structural prior                   ║  no socket    │
             │   ║   L1 anchors + checksums   (×3.0)       ║  is opened    │
             │   ║   L2 zone-weighted BM25    (×1.0)       ║  in this box  │
             │   ║   L3 local BERT kNN (opt) (×0.8)        ║               │
             │   ║   L4 abstain → unknown → human          ║               │
             │   ╚════════════════════╤════════════════════╝               │
             │            accepted    │    abstained ──► needs_review      │
             │                        ▼                                    │
             │  dce.extract — per FieldSpec, locators in priority order    │
             │    kv → label → table → mark → regex → mrz                  │
             │                        ▼                                    │
             │  ExtractionResult: value + normalized + locator + page/bbox │
             └─────────────────────────────────────────────────────────────┘
```

| Module | Owns |
|---|---|
| `dce/models.py` | The value types every module codes against. The contract. |
| `dce/config.py` | Thresholds, weights, windows, the invariant flag. |
| `dce/adapters.py` | Provider payload → `LayoutView`. The only module that knows Azure's JSON. |
| `dce/registry/` | The doctype packs — data, plus eager structural validation. |
| `dce/classify/` | The cascade and its tiers. |
| `dce/extract/` | The field resolver, its locators, and the validators. |
| `dce/egress.py` | The runtime guard for the invariant. |
| `dce/api/` | HTTP, ports to the engines, readiness. |
| `dce/observability.py` | Metrics + the readiness registry. |

---

## 4. `LayoutView`: why provider-neutral

`LayoutView` is the only thing the classifier and extractors are allowed to see. It has no
notion of where the bytes came from, and no reference to the original file — the service never
needs it.

Two consequences worth stating:

**A business unit can use this without adopting our OCR stack.** They send whatever their
provider produced, or plain text, and it maps. Azure `prebuilt-layout` is the reference
producer because it predicts `paragraphs[].role`, real table grids with merge spans, and
selection marks — all three are load-bearing — but nothing in the core imports Azure.

**Zones, not geometry, drive the lexical score.** The same word is worth `3.0` in a title and
`0.25` in a page footer that repeats on all 40 pages. That single ratio is most of what makes
L2 better than grep, and it is *free*: Azure already predicted the role, so the adapter is a
lookup rather than a font-height heuristic.

Two adapter decisions that took thought:

* **Table text is re-zoned, not re-emitted.** Azure's `paragraphs[]` stream already contains
  table-cell content. Emitting cells as extra blocks would count every cell twice in the
  lexical score. So paragraphs whose character spans overlap a table's spans are re-zoned to
  `Zone.table` (falling back to bbox containment when spans are absent), and the `tables[]`
  collection carries the grid separately for the `table` locator.
* **`from_plain_text` promotes nothing.** Everything lands in `Zone.body`. It would be easy to
  call the first line a title, and it would be wrong often enough to matter: a wrong title is
  amplified ×3 and converts an abstention into a confident mistake. Degrading honestly beats
  guessing structure.

Every mapping step is defensive and never raises. A malformed table costs you that table, not
the classification.

---

## 5. The cascade

### 5.1 Tiers

**L0 — structural prior.** Page count, aspect ratio, number of selection marks, number of
tables, digital-vs-scanned. Free, computed from `LayoutView` alone, and it eliminates whole
branches: a single-page 3.37×2.13 card is not a 40-page bank statement, whatever words are on
it. Contributes `log P(c | structure)`.

**L1 — anchors + checksums.** Decisive anchor strings (an issuing-authority header, a form
number, an OMB control number) and identifiers whose *checksum validates*. Weighted `3.0`,
the heaviest term in the fusion, because a checksum-valid identifier next to the right header
is close to proof. Matching is on **word tokens, never substrings** — the substring approach
fired `DL` inside "mi**dl**e", `EIN` inside "b**ein**g" and `SIN` inside "u**sin**g", a whole
class of false positives that tokenisation deletes.

**L2 — zone-weighted BM25.** Per-doctype term profiles scored over the zones, with BM25's
`k1`/`b` saturating term frequency so a word repeated forty times does not outweigh forty
distinct matching words. Produces the calibrated probability.

**L3 — local BERT kNN (optional, default off).** A mounted checkpoint, in-process, CPU,
weighted `0.8`. It exists for one specific failure: two doctypes that share vocabulary and
neither has a decisive anchor. It is off by default because most document types announce
themselves in print, and 1.3 GB of weights is a real operational cost to carry for a tier that
usually changes nothing.

**L4 — abstain.** `unknown` → human queue. This tier is what makes the other four safe.

### 5.2 Fusion and the accept rule

```
score_c = log P(c | structure) + 3.0·anchor_c + 1.0·lexical_c + 0.8·bert_c
p       = softmax(score / T)          # T = softmax_temperature, default 0.6

accept  ⟺  p ≥ classify_accept_probability   (0.65)
      AND  margin ≥ classify_min_margin      (0.25)
      AND  coverage ≥ classify_min_coverage  (0.20)
```

All three conditions, because each catches a different way of being wrong:

* **Probability** alone accepts a document that merely resembles a PAN card more than anything
  else in the registry. With a 100-entry registry, "most likely" is a low bar.
* **Margin** forces the winner to actually beat the runner-up. This is the condition that
  catches near-identical form revisions, which is the registry's characteristic failure.
* **Coverage** — the share of the class's term profile actually observed — catches winning by
  elimination. A blank page can score highest on some class simply because nothing contradicts
  it; coverage says *the document does not contain this document type's vocabulary*.

Failing any of them abstains. The `reason` string names the condition that failed and the
numbers, because "abstained" without a number is not actionable.

### 5.3 Why the weights are what they are

They are a deliberate ordering, not a fit: anchors ≫ lexical > BERT. A decisive anchor is
categorical evidence and should dominate; lexical evidence is real but diffuse; the BERT tier
is a tie-breaker and is weighted so it can *shift* a decision but not *make* one on its own.
When calibration data exists, tune the temperature and the Platt calibration on the lexical
tier first — the fusion weights encode the epistemics and should move last.

### 5.4 Tuning guidance that belongs next to the thresholds

If the abstention rate climbs, **the fix is almost never a lower threshold.** Lowering the
threshold does not make the classifier more accurate; it converts abstentions into silent
misclassifications, and a silent misclassification costs far more downstream than a human
glance. In order:

1. Is a doctype missing from the registry? (Check `dce_classifications_by_doctype_total` for a
   class absorbing traffic, and sample the `unknown` queue.)
2. Do the top-two runners-up look like the same form? Add `confusable_with` with the
   discriminating term.
3. Is coverage the failing condition? The term profile is stale — refit it.
4. Only then consider L3.

---

## 6. Extraction

Runs only after a doctype is accepted. This build ships **T1: the local resolver.** Per
`FieldSpec`, run its locators in priority order, score the candidates, pick the best, validate
it, and emit an `ExtractedField` with provenance.

| Locator | Binds by | Good at | Fails on |
|---|---|---|---|
| `kv` | provider key/value pair | forms the provider already parsed | providers without the feature |
| `label` | label anchor + right-of/below window, fuzzy (rapidfuzz) match | labelled forms | multi-column layouts with wide windows |
| `table` | cell addressing incl. header lookup | statements, schedules | tables without headers |
| `mark` | checkbox / radio binding | KYC forms where a tick *is* the answer | ambiguous mark→label association |
| `regex` | value shape | identifiers with distinctive form | OCR noise that still matches the shape |
| `mrz` | ICAO 9303 machine-readable zone | passports, TD1/TD2/TD3 cards | damaged or cropped MRZ |

**Provenance is not optional.** Every field carries `locator`, `page`, `bbox`, so a reviewer
can be shown the exact pixels a value came from. A KYC extraction that cannot be traced back to
a location on the page is not reviewable, and an unreviewable extraction is not usable.

**The verification ladder** — `unverified` → `format_valid` → `checksum_verified` →
`cross_verified` → `human_verified` — is a first-class field, not a confidence score. A
checksum-verified Aadhaar number and a 0.97-confidence regex match are different kinds of
claim, and collapsing them into one number loses exactly the distinction a reviewer needs.

**Prefer a validator over a longer regex.** A shape regex accepts OCR noise of the right shape;
a checksum rejects it and promotes the field a rung up the ladder.

---

## 7. The doctype registry

A `DocTypeSpec` declares *how to recognise* a document type and *what to pull out of it* in
one object. That is deliberate. "This is an Aadhaar card" and "an Aadhaar card has a 12-digit
UID with a Verhoeff check" are the same knowledge; split across two files, they drift within a
quarter.

Rules the registry enforces at import time, not at request time:

* **Registration validates eagerly.** A pack with an uncompilable regex or an unknown
  validator name raises on import. A KYC classifier that silently carries a broken doctype is
  worse than one that refuses to start.
* **Decisive anchors must stay distinguishing.** Two doctypes claiming the same decisive
  anchor make each other unclassifiable at the tier that matters most. Where the collision is
  genuine (a masked and a full Aadhaar really do share the UIDAI header), both must declare
  each other in `confusable_with` and each must keep a decisive anchor of its own.
* **Attribute keys reuse the fleet ontology** (`identity.*`, `id.*`, `address.*`, `doc.*`,
  `entity.*`, `ownership.*`), so a fact extracted here merges with the same fact from another
  document instead of becoming a parallel truth.

See the README for the add-a-doctype walkthrough — that is the one thing an integrator
actually does.

---

## 8. Schema induction

`POST /api/v1/schemas/induce` drafts a field list from sample layouts by taking the *named
slots* the samples agree on: provider key-value pairs and table column headers, kept when they
appear in at least `min_support` of the samples. Support filtering is what stops a one-off
stamp or a handwritten margin note from becoming a field.

**It always returns `active: false`, and always will.** Induction saves an integrator typing;
it does not get to decide what the service extracts. A schema that activated itself would
silently change the output of a KYC system, which is a change nobody signed off on. The draft
is a starting point for a human: anchors, validators and attribute keys still need judgement,
and the endpoint's `notes` field says so.

---

## 9. API semantics

The route design follows one rule: **abstention is a first-class outcome, not an error.**

| Situation | Response |
|---|---|
| Classified confidently | `200`, `abstained: false` |
| Cannot decide | `200`, `abstained: true`, `reason` populated, `needs_review` |
| `/process` abstained | `200`, `extraction: null` — **extraction is not attempted** |
| No document in the request | `400` |
| Bad or missing API key | `401` |
| `doctype_id` not in the registry | `404` |
| Engine returned an unexpected type | `502` |
| Registry/classifier/extractor not loadable | `503` |

`/process` not extracting on abstention is the load-bearing behaviour and has its own test.
Extracting against a guessed doctype yields confidently wrong fields, which are worse than no
fields: no fields routes to a human, wrong fields route to a database.

An engine that is not needed is not required. `/extract` with a pinned `doctype_id` does not
demand a classifier; `/process` on an abstaining document does not demand an extractor. A
`503` for a dependency the request never touches is a lie about the service's health.

### 9.1 Engine ports

`dce/api/routes.py` resolves the engines lazily through small adapter ports rather than
importing them at module load. Three reasons, in order:

1. The API boots and reports **honest readiness** while an engine is missing or broken — which
   is exactly when an operator needs `/readyz` and `/metrics` to work.
2. Tests substitute stubs at the port boundary, so the routes under test are the production
   routes.
3. `import dce.api.routes` stays cheap, which matters because the L3 tier pulls in torch.

The contract, with optional keyword arguments passed only when the callee's signature accepts
them (so a narrower implementation works unchanged):

```python
dce.registry:  all_specs() -> list[DocTypeSpec];  get(doctype_id) -> DocTypeSpec | None
dce.classify:  classify(view, specs=..., *, settings=...) -> Classification
dce.extract:   extract(view, spec, *, settings=..., schema_version=...) -> ExtractionResult
```

---

## 10. Observability

The number to watch is the **abstention rate**:

```promql
sum(rate(dce_classifications_total{outcome="abstained"}[30m]))
  / sum(rate(dce_classifications_total[30m]))
```

Rising means the corpus drifted away from the registry. Falling to **zero** deserves the same
suspicion: a classifier that never abstains has lost the ability to say "I don't know", which
is the one thing this service must always be able to say.

Supporting signals: `dce_classification_confidence` (are accepts clustering just above the
threshold — i.e. is the margin condition doing all the work?), `dce_classification_margin`,
`dce_classifications_by_doctype_total` (did one class swallow the traffic?),
`dce_classification_tier_seconds{tier}`, `dce_extraction_fill_rate{doctype}`,
`dce_extraction_validator_failures_total{doctype,field,validator}` (the extractor found
something and a checksum rejected it — usually an OCR quality problem, sometimes a wrong
pattern), and `dce_preclassification_egress_blocked_total`, where any nonzero value is a
finding rather than noise.

Every metric label is bounded by the registry — doctype ids, field names, tier names, validator
names, route templates. Nothing is labelled with document content.

---

## 11. Deployment

`python:3.12-slim`, non-root (`uid 10001`), port **8200**, healthcheck on `/health`, read-only
root filesystem with a tmpfs `/tmp`, all capabilities dropped.

The BERT checkpoint is **not** in the image. It is ~1.3 GB, the tier is off by default, and
baking it in would make every deployment pay for a feature most never enable. It is mounted
read-only at `/models`, and `docker-compose.yml` ships the volume commented-but-ready next to
`BERT_ENABLED=false`.

One packaging wrinkle worth recording: the published `bert_uncased_L-12_H-768_A-12` checkpoint
ships TensorFlow + Flax weights and **no** PyTorch `bin`/`safetensors`, so `transformers` needs
`from_tf=True` (requires `tensorflow`) or `from_flax=True` (requires `jax`+`flax`). Neither is
a declared dependency of the `bert` extra, because which one you want depends on your base
image. Converting the checkpoint to `safetensors` once, offline, and mounting that is smaller
and faster than shipping a second framework into a production image.

If `BERT_ENABLED=true` and the directory is absent, `Settings` raises at startup. Loud, not
silent: an operator who asked for BERT should find out in the first thirty seconds, not
discover degraded accuracy three weeks later.

---

## 12. Deliberately not built

* **A remote embedding tier.** Removed, see §2.1. Not coming back.
* **An LLM extraction tier (T2).** There is a real case for one — unstructured proof-of-address
  documents resist deterministic locators — but it belongs *behind* the classification gate,
  with its own per-doctype egress policy and its own audit trail. It is a separate decision
  from this build, and shipping it in the same release would have blurred the boundary the
  service exists to draw.
* **A database.** The service is stateless by design: layout in, classification and fields out.
  Whoever owns the human-review queue owns its storage. `dce_needs_review_queue_depth` is a
  gauge this service can be *told* about; it does not run the queue.
* **Auto-activation of induced schemas.** See §8.
* **Confidence-threshold auto-tuning.** A feedback loop that lowers thresholds to reduce the
  abstention rate optimises exactly the wrong quantity.

## 13. Open questions for the next iteration

1. **Per-page classification on merged PDFs.** `Classification.page_types` exists in the
   contract and `classify_pages` produces segments; the routes currently expose whole-document
   classification. Batch scans of mixed documents are common enough that this should surface.
2. **Calibration data.** The Platt calibration on L2 is identity until someone fits it on a
   labelled corpus. Until then the probabilities are ordered correctly but not calibrated, and
   the accept threshold is doing more work than it should have to.
3. **Registry coverage as a metric.** Right now a missing doctype shows up as an abstention
   rate. It would be better to sample the `unknown` queue and cluster it, so "you are missing a
   doctype that accounts for 4% of traffic" is a number rather than an inference.
