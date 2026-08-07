# Design — Document Classification & Extraction

> Status: v0.2. Adds the post-classification extraction tiers (T2 Azure prebuilt, T3
> queryFields, T4 constrained LLM) and the human review queue (T5).
> Audience: whoever owns this service next, and whoever has to defend it in a control review.

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

### 2.2 The rule is directional, not absolute

This is the part that gets misread, so it is stated plainly: **the invariant is about the
window in which nobody knows what they are handling.** It is not a claim that the service never
makes a network call.

Once the cascade has *accepted* a doctype — on its own in-process evidence, with anchors,
checksums and a lexical score behind it — the document has been placed. The caller now knows
they are holding a W-9, and can make an informed decision about whether a W-9 may be sent to a
vendor that reads W-9s. That decision is theirs, it is now *possible* to make, and the tiered
extractor is how it gets acted on.

So the boundary is:

| | Before acceptance | After acceptance |
|---|---|---|
| Classification (L0-L4) | in-process only, always | — |
| T1 local resolver | — | in-process only, always |
| T2/T3 Azure, T4 LLM | **forbidden** | permitted, off by default, per-deployment flag |
| T5 human review | — | always available; where abstentions go |

An abstention is the one outcome that never escalates. `unknown` does not go to a vendor to be
identified — "ask Azure what this is" is the leak wearing a different hat — it goes to a person.

### 2.3 Threat model

| Actor / event | Concern | Control |
|---|---|---|
| An upstream business unit sends a document they have not reviewed | Their data reaches a third party under our name | No network client exists in the default image; nothing on the classification path imports one |
| A future engineer adds "just one" enrichment call to the classifier | Silent regression of the guarantee | `dce.egress` socket scope + a source-level test |
| A future engineer wires a paid tier into the abstention branch | Unclassified documents reach a vendor | `run_tier_cascade` **raises** on `unknown`; each tier module refuses it again; a test asserts no tier is invoked on an abstention |
| An operator flips a config flag under delivery pressure | Guarantee lost without anyone noticing | `/readyz` → 503, `error`-level log, metric |
| An operator enables a paid tier without noticing it is egress | Documents leave under a "performance" change | Off by default, needs an endpoint *and* a rebuild with an HTTP client; reported on `/readyz` and in every `/process` response |
| A dependency pulls in an HTTP client transitively | Capability exists even if unused | Nothing in the base dependency set requires one; `httpx` is test-only |

### 2.4 How it is enforced, in order of strength

1. **The capability is absent.** `pyproject.toml` has no `httpx`, no `requests`, no
   `aiohttp`, no vendor SDK in the base dependencies. `httpx` appears only in the `dev`
   extra, because `fastapi.testclient` is built on it — and that is called out in a comment so
   nobody promotes it later by accident. The tier modules import their client *inside* the
   function that calls out, so importing `dce.extract` never pulls one in either.
2. **A test greps for it.** `tests/test_api.py::test_no_http_client_in_the_classification_path`
   scans `dce/models.py`, `dce/config.py`, `dce/adapters.py`, `dce/observability.py`,
   `dce/registry/**` and `dce/classify/**` for any network import and fails the build if one
   appears. `dce/extract/**` and `dce/api/**` are excluded on purpose: they run only after a
   doctype has been accepted.
3. **A runtime scope.** `dce.egress` wraps classification in a scope whose socket tripwire
   raises `EgressViolation`, so code that reaches for the network at runtime — including code
   nobody has written yet — fails loudly instead of succeeding quietly.
4. **The abstain rule, at the call site.** `dce/api/routes.py` returns from `/process` on an
   abstention before the tier cascade is considered, and `run_tier_cascade` raises if it is
   ever reached with `doctype_id == unknown`. That is deliberately a raise and not a skip: a
   call site that can reach the paid tiers with an abstention is a bug, and the failure it
   produces is a disclosure.
5. **A visible state.** `allow_preclassification_egress` is reported by `/readyz`, which
   returns **503** when it is on, logged at `error` on boot, and counted by
   `dce_preclassification_egress_blocked_total` when the guard actually stops something. Every
   paid tier's posture is reported there too, and in `tiers_used` on every response.

Layer 1 is the one that matters, and unlike the others it is checkable on the shipped artifact
rather than the source tree:

```console
$ docker run --rm --entrypoint python dce:latest -c \
  "import importlib.util as u; print({m: bool(u.find_spec(m)) for m in ('httpx','requests','aiohttp','openai','azure','boto3')})"
{'httpx': False, 'requests': False, 'aiohttp': False, 'openai': False, 'azure': False, 'boto3': False}
```

**And it is the layer you give up when you enable a paid tier.** T2/T3/T4 need an HTTP client;
the image ships none, so enabling them means `docker build --build-arg
EXTRA_PACKAGES="httpx>=0.27"`. From that build onwards the capability exists in the container,
and the guarantee rests on layers 3, 4 and 5 — the egress scope, the abstain rule, and the
socket tripwire test — rather than on absence. That is a real trade and the README says so in
those words. It is also why `tests/test_egress.py` asserts *zero sockets were opened during
classification* rather than asserting that a particular function was not called: that form of
proof survives the arrival of an HTTP client in the image, and the grep-based one does not.

---

## 3. Shape of the system

```
             ┌──────────────────────────────────────────────────────────────────┐
  payload    │  dce.adapters                                                    │
  ─────────► │    from_azure_layout / from_des_ocr / from_plain_text            │
             │                        ▼                                         │
             │                  LayoutView   (provider-neutral)                 │
             │                        │                                         │
             │   ╔════════════════════▼════════════════════╗                    │
             │   ║  dce.classify — 100% in-process         ║                    │
             │   ║   L0 structural prior                   ║  no socket         │
             │   ║   L1 anchors + checksums   (×3.0)       ║  is opened         │
             │   ║   L2 zone-weighted BM25    (×1.0)       ║  in this box       │
             │   ║   L3 local BERT kNN (opt) (×0.8)        ║                    │
             │   ║   L4 abstain → unknown → human          ║                    │
             │   ╚════════════════════╤════════════════════╝                    │
             │            accepted    │    abstained ─────────────┐             │
             │                        ▼                           │             │
             │  T1  dce.extract.resolve — locators, local, free   │             │
             │      kv → label → table → mark → regex → mrz       │             │
             │                        │ still missing?            │             │
             │                        ▼                           │             │
             │  T2  azure_specialist  ─ per page   ┐               │            │
             │  T3  query_fields      ─ per field  ├ off by default│            │
             │  T4  llm_field         ─ per token  ┘  EGRESS       │            │
             │                        │ still not good enough?     │            │
             │                        ▼                            ▼            │
             │  T5  dce.review — one item per field, double entry on PII+checksum│
             └──────────────────────────────────────────────────────────────────┘
```

| Module | Owns |
|---|---|
| `dce/models.py` | The value types every module codes against. The contract. |
| `dce/config.py` | Thresholds, weights, windows, the invariant flag, the tier switches. |
| `dce/adapters.py` | Provider payload → `LayoutView`. The only module that knows Azure's JSON. |
| `dce/registry/` | The doctype packs — data, plus eager structural validation. |
| `dce/classify/` | The cascade and its tiers. |
| `dce/extract/` | T1's resolver, its locators and validators; T2/T3/T4 as separate modules. |
| `dce/review.py` | T5: the queue's data model and its state machine. Storage-agnostic. |
| `dce/egress.py` | The runtime guard for the invariant, both directions. |
| `dce/api/` | HTTP, ports to the engines, the tier cascade, readiness. |
| `dce/observability.py` | Metrics + the readiness registry. |

---

## 4. `LayoutView`: why provider-neutral

`LayoutView` is the only thing the classifier and T1 are allowed to see. It has no notion of
where the bytes came from, and no reference to the original file — classification never needs
it.

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

**The one exception, and it is deliberate.** T2 and T3 analyse the *file*, not our reading of
it, so `/process` accepts an optional `content_base64`. It is read by nothing until a doctype
has been accepted, and only by those two tiers. A deployment with the paid tiers off should not
send it at all — the smallest way to keep a document out of a network call is not to hand it
over in the first place.

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
p       = softmax(score / T)          # audit trail and runners-up ONLY — decides nothing

A_c = anchor_c                        # min(0.97, 1 − 0.5^raw), absolute, per (doc, spec)
L_c = explained_c                     # share of class c's own idf-weighted profile mass
                                      # that the document exhibited, BM25-saturated

S_c = 1 − (1−A_c)(1−L_c)        d = argmax S

accept d ⟺  argmax(A) = argmax(L) = d       (concurrence)              ┐ identification:
        OR  d is the one doctype holding a decisive anchor or a        ┘ either one
            corroborated checksum, and no confusable peer's decisive
            claim was muted by a missing zone on this payload
      AND  S[1] − S[2]                  ≥ classify_min_margin   (0.04)
      AND  S_d                          ≥ classify_min_support  (0.30)
      AND  max(profile cov, anchor cov) ≥ classify_min_coverage (0.20)

confidence = min( S-lead/(S-lead+margin),
                  S_d   /(S_d   +support),
                  cov_d /(cov_d +coverage) )    if identified else 0.0
```

**There is one accept path, and the near-proof L1 evidence goes through it.** There used to be
a checksum short-circuit and a decisive-anchor short-circuit that returned before this rule
ran, each with its own hard-coded confidence; between them they produced 25 of 35 accepts on
the reference corpus, so the documented rule governed a minority of the decisions. They
reported margins computed across two different scales (three accepts shipped a *negative*
margin), they could not be refused by `classify_min_support` or `classify_min_coverage`
because those gates were evaluated after the early return, and their confidence did not order
accepts above abstentions. All three are structural, not tuning, and all three are fixed by
having one rule: 34/1/26 correct/wrong/abstained before, 36/1/24 after, precision 97.1% →
97.3%, negative margins 3 → 0, sub-coverage accepts 5 → 0. With a title zone present (a
font-size proxy for the Azure DI `title` role, which the shipped text-layer harness never
emits): 31/2/28 at 93.9% before, 31/1/29 at 96.9% after.

**Confidence is a distance to the binding constraint, not a posterior.** Each factor is
exactly 0.5 at its own floor, so the `min` is ≥ 0.5 if and only if every gate passed: **0.5 is
the decision boundary**, and the reported value names the control that came closest to
blocking the accept. It is deliberately not dressed up as a probability — a calibrated
posterior would need labelled production data this service does not have, and a number that
merely looks like one is how the registry-normalised softmax got into the accept path.

**`p` is no longer an accept condition, and could not be repaired into one.** Every doctype in
the registry contributes a strictly positive term to that softmax denominator, related to the
document or not, so `p` is a function of registry size as well as of the document: the same
US W-9, on identical evidence, scored 0.900 against 25 doctypes and 0.411 against 121. Every
country pack shipped degraded every doctype already installed. Lowering the floor would have
relocated the defect, not removed it. `classify_accept_probability` has now been **removed**
from `config.py` — it was kept there, deprecated, on the grounds that "the short-circuit still
reports a confidence above it", and that short-circuit no longer exists. A setting that
nothing reads still appears in the container environment and in a reviewer's model of what
governs the decision, which makes it a control-review hazard rather than harmless history; the
history is recorded in a comment where the field used to be.

Four conditions, because each catches a different way of being wrong:

* **Identification** requires the winner to be picked out by evidence rather than by being the
  least-bad thing on the shelf. Concurrence — two independent tiers reaching the same class —
  is what buys precision, and it is measurable: a rank-relative rule *without* it recovers more
  documents and admits two new wrong answers, because the runner-up in a fused ranking is not
  reliably the real competitor. The conclusive-L1 alternative exists because a photo ID carries
  almost no text for the lexical tier to rank; deleting it costs 3 of 37 accepts on the corpus
  and buys no precision. A channel on which nothing scored above zero is **silent**, not
  agreeing: install a single doctype and its profile is empty, and a rule comparing argmaxes
  alone would find the silent tier "concurring" about the only doctype on the shelf.
* **Separation** forces the winner to actually beat the next candidate, on the combined
  channel, so what counts as a real gap does not depend on how many doctypes exist. Being one
  subtraction on one scale, it also cannot be negative on an accepted answer — which the old
  short-circuit's constant-minus-anchor-score arithmetic could be, and was, twice.
* **Support** is the absolute backstop. A lead says the winner beat its rivals; support says
  the winner is actually supported, which is what stops "least-wrong of a tiny registry". It is
  redundant on the reference corpus — it fires on 5 documents but is never the sole refusal —
  and it is kept because it is the only gate that can see a doctype declaring one weak anchor,
  which gets coverage 1.0 for free and an unopposed lead. That case is pinned by a test.
* **Coverage** — the share of the class's vocabulary actually observed — catches winning by
  elimination. A blank page can score highest on some class simply because nothing contradicts
  it; coverage says *the document does not contain this document type's vocabulary*.

Because the verdict reads five numbers — `S[1]`, `S[2]`, the winner's coverage, and the
argmaxes of `A` and `L` — adding a doctype can change it only by entering the top two of a
channel, which requires that doctype to carry real evidence *for this document*.
`tests/test_registry_scale_invariance.py` pins the property. Re-measured after the accept-path
rewrite, in the harder form (registry sizes 5/10/25/50/121 with the term profiles rebuilt at
each size, so idf drift is included rather than held fixed): the `(doctype, abstained)` verdict
is unchanged on **60 of 61** documents, with a maximum confidence swing of **0.038**. The one
exception sits 0.002 above the accept boundary and the drift tips it across. That residual is
the idf term inside `lexical.explained`, which appears in the numerator and denominator of that
ratio and so very largely — but not exactly — cancels; it is not the removed defect, which was
a systematic monotone collapse of size 0.49 on a single document.

`confidence` changed meaning with the rule, for the second time, and dashboards histogramming
`dce_classification_confidence` need re-baselining. It is now
`min(separation, strength, breadth)` over the three floors above. **0.5 is the decision
boundary in both directions**: every accepted answer is at or above it, every abstention
strictly below it. The previous `separation × strength` form left accepts and abstentions
overlapping (an abstention at 0.494 above an acceptance at 0.409) because the short-circuit
supplied a constant on an unrelated scale; taking the `min` of gate-relative ratios makes the
ordering a property of the arithmetic rather than of the calibration.

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

## 6. Extraction: five tiers, escalating

Extraction runs only after a doctype is accepted. Each tier sees only what the previous ones
could not fill, and the cascade stops the moment nothing is missing.

| Tier | Module | Cost | Verification ceiling |
|---|---|---|---|
| T1 local resolver | `dce/extract/resolve.py` | free | `checksum_verified` |
| T2 Azure prebuilt | `dce/extract/azure_specialist.py` | per page | `format_valid` |
| T3 Azure queryFields | `dce/extract/query_fields.py` | per field, ≤20/request | `format_valid` |
| T4 constrained LLM | `dce/extract/llm_field.py` | per token | `format_valid` |
| T5 human review | `dce/review.py` | a person | `human_verified` |

### 6.1 T1 — locators propose, validators dispose

Per `FieldSpec`, run its locators in priority order, score the candidates, pick the best,
validate it, and emit an `ExtractedField` with provenance.

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

### 6.2 Why tiers rather than "run the good one"

The obvious alternative is to send everything to the best available extractor and be done. Three
reasons not to:

* **Cost.** T1 answers most fields on most document types for nothing. Paying a per-page fee to
  rediscover a value a label locator already found is pure waste, and at KYC volumes it is not
  a rounding error.
* **Verification.** T1 can *prove* a value with a check digit. A vendor model returns a
  confidence, which is a different and weaker kind of claim. Running the cheap prover first and
  the expensive guesser second means the strongest evidence available for each field is the
  evidence that is used.
* **Blast radius.** The tiers that leave the process are the ones you may need to switch off in
  a hurry — a vendor incident, a jurisdiction that will not allow it, a bill. If they are the
  bottom of an escalation, switching them off degrades gracefully: fewer fields filled, more
  items for a human, nothing broken.

### 6.3 The merge rule, and where it is enforced

A tier returns *candidates*. The router (`_merge_tier_fields`) decides what happens to them, and
it applies exactly two rules:

1. **Only fields that were missing when the tier was asked.** Anything else is dropped.
2. **The tier id is prefixed onto the locator.** `t4_llm:llm` and `kv` are distinguishable
   forever after.

This is deliberately *not* left to the tiers. A model asked for three fields will volunteer a
fourth; a vendor model will return a "corrected" version of a value you already proved. If the
merge lived in the tier, every new tier would be a new opportunity to overwrite a
checksum-verified value with a fluent guess — the ladder inverted by accident. One enforcement
point, at the call site, is the only version of this that stays true as tiers are added.

`missing_required` and `needs_review` are recomputed **only** when a paid tier actually filled
something. A document that took the local path alone comes back byte-identical to what it
returned before the tiers existed, which is what makes "tiers off" a genuinely unchanged
deployment rather than a differently-shaped one.

### 6.4 T4's four constraints

The case for an LLM tier is real — unstructured proof-of-address documents resist deterministic
locators — and so is the case against putting one in a KYC pipeline. What makes it acceptable:

1. **A window, not the document.** The tier sends a text window built around the missing fields'
   labels, capped at `llm_max_window_chars`. Less disclosed, less paid for, and a shorter
   haystack for a model being asked to quote from it.
2. **A JSON schema, not a prompt-and-hope.** The response format is derived from the
   `FieldSpec`s.
3. **Grounding.** Every value must come with a quote that can be *located in the window it was
   given*. Values whose quote cannot be found are discarded, and free-form prose is discarded
   whole — there is no salvage path that scrapes a value out of a sentence, because that path
   is how ungrounded values get into records.
4. **The same validators as T1**, and a ceiling of `format_valid` without a check digit.

If all four hold, the worst case is a field that stays empty and goes to a human — which is
where it was going anyway.

---

## 7. T5: the human review queue

### 7.1 What it is

`dce/review.py` is a data model and a state machine, and nothing else. `pending` →
`approved` | `rejected` | `corrected`, decided once, with the deciding reviewer and the
timestamp on the item. A review queue that cannot say *who* accepted a value and *when* is a
spreadsheet, not a control.

Storage is a `Protocol` with two shipped implementations — in-memory (one process, loses
everything on restart) and a JSON file (small deployments, readable with `cat`). §12's rule
still holds: the service is stateless and whoever owns the queue owns its storage. A team with
Postgres implements five methods.

### 7.2 One item per field

Items are keyed `"<doc_id>:<field_name>"` — stable, so re-processing a document does not
resurrect a decision somebody already made. An abstained document produces exactly one item,
for the document itself, because there are no fields to itemise and "what is this?" is the only
question worth asking.

Per-field rather than per-document is a deliberate choice about human behaviour: an approval
that covers a whole document is an approval by somebody who looked at one part of it.

### 7.3 Blind double entry, and why only on some fields

A field that is **both PII and backed by a real check digit** takes two independent decisions
from two different people: two approvals, or two matching corrections. A mismatch discards both
entries rather than picking a winner.

The pairing is not arbitrary. A typo in a checksummed identifier does not produce a value that
looks wrong — it produces a *valid-looking identifier belonging to somebody else*, and no
downstream system can distinguish it from a correct one. That is the exact error class
single-keyed data entry is known to produce and double entry is known to catch. Everything else
takes one decision, because a control everybody is too busy to follow is not a control.
Rejection always takes one: it is the safe direction, and slowing down the person trying to
stop bad data buys nothing.

It is enforced in `approve()` and `correct()`, not in a UI. A control that lives only in a
frontend is a suggestion.

---

## 8. The doctype registry

A `DocTypeSpec` declares *how to recognise* a document type and *what to pull out of it* in
one object. That is deliberate. "This is an Aadhaar card" and "an Aadhaar card has a 12-digit
UID with a Verhoeff check" are the same knowledge; split across two files, they drift within a
quarter.

| Country | Total | Identity | Address proof | Tax | Corporate | Financial | Other |
|---|---:|---:|---:|---:|---:|---:|---:|
| India | 36 | 12 | 7 | 4 | 7 | 5 | 1 |
| United States | 35 | 12 | 2 | 9 | 8 | 4 | — |
| Canada | 25 | 11 | 3 | 4 | 6 | 1 | — |
| Mexico | 20 | 8 | 5 | 4 | 2 | 1 | — |
| Cross-country | 5 | 2 | 1 | — | — | 1 | 1 |
| **Total** | **121** | **45** | **18** | **21** | **23** | **12** | **2** |

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

### 8.1 The honest caveat

**The registry is authored from published specifications, form templates and public
documentation. It has not been validated against a corpus of real specimens.** Anchor strings,
form numbers and field labels are as documented; what an actual scan of a 2016-revision form
OCRs to is an empirical question this repo has not answered.

The practical consequence: on a first deployment, treat a high abstention rate as *information
about the registry*, not as a threshold to tune. Sample the `unknown` queue, compare what the
OCR actually produced against the anchors the pack declares, and fix the pack. That loop is the
intended first month of operation, and it is why the `unknown` queue and per-doctype counters
exist.

See the README for the add-a-doctype walkthrough — that is the one thing an integrator
actually does.

---

## 9. Schema induction

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

## 10. API semantics

The route design follows one rule: **abstention is a first-class outcome, not an error.**

| Situation | Response |
|---|---|
| Classified confidently | `200`, `abstained: false` |
| Cannot decide | `200`, `abstained: true`, `reason` populated, `needs_review` |
| `/process` abstained | `200`, `extraction: null` — **no extraction, no tier, no call** |
| A paid tier failed or is misconfigured | `200`, reported in `tiers_used` with a status |
| No document in the request | `400` |
| Bad or missing API key | `401` |
| `doctype_id` not in the registry | `404` |
| Review decision the queue refused (already decided, double-entry violation) | `409`, with the queue's own sentence |
| Engine returned an unexpected type | `502` |
| Registry/classifier/extractor/review queue not loadable | `503` |

`/process` not extracting on abstention is the load-bearing behaviour and has two tests: one
that the extractor is not called, one that no tier is. Extracting against a guessed doctype
yields confidently wrong fields, which are worse than no fields: no fields routes to a human,
wrong fields route to a database.

An engine that is not needed is not required. `/extract` with a pinned `doctype_id` does not
demand a classifier; `/process` on an abstaining document does not demand an extractor. A
`503` for a dependency the request never touches is a lie about the service's health.

**A failing paid tier is not a failing request.** It is reported and stepped over. The document
still comes back with everything the cheaper tiers found, and the reason the tier did not
contribute is in the response — `misconfigured`, `unavailable`, `skipped`, `error`. A `502`
here would convert a vendor's bad afternoon into our outage.

**`/extract` does not escalate.** It is the surface an integrator calls in a loop while tuning
locators; a route that quietly billed per call would be a trap. `/process` is where escalation
happens, and it reports what it spent.

### 10.1 Engine ports and the tier contract

`dce/api/routes.py` resolves the engines lazily through small adapter ports rather than
importing them at module load. Three reasons, in order:

1. The API boots and reports **honest readiness** while an engine is missing or broken — which
   is exactly when an operator needs `/readyz` and `/metrics` to work.
2. Tests substitute stubs at the port boundary, so the routes under test are the production
   routes.
3. `import dce.api.routes` stays cheap — and, for the paid tiers, **the import must not have
   happened at all** while an unclassified document is in memory. They are resolved inside the
   handler, after acceptance, and only when their flag is on. A test asserts the router has no
   module-scope import of a tier module.

The contract, with optional keyword arguments passed only when the callee's signature accepts
them (so a narrower implementation works unchanged):

```python
dce.registry:  all_specs() -> list[DocTypeSpec];  get(doctype_id) -> DocTypeSpec | None
dce.classify:  classify(view, specs=..., *, settings=...) -> Classification
dce.extract:   extract(view, spec, *, settings=..., schema_version=...) -> ExtractionResult
```

A paid tier is any callable found under its candidate module names, invoked with whichever of
`view` / `data` / `doctype_id` / `spec` / `missing` / `field_names` / `settings` /
`classification` its signature actually names — unrecognised parameters are filled positionally.
Coroutines are awaited. The loose binding is deliberate: T2, T3 and T4 landed as independent
pieces of work, and a router that pinned one exact signature would have meant the first tier to
disagree silently did not run.

### 10.2 Reporting what a document cost

`tiers_used` is a per-request ledger: `{tier, status, fields_filled, fields, ms, cost_bearing,
detail}` for every tier that executed or was deliberately skipped. Tiers that are switched off
do not appear at all — absence is what "we spent nothing" looks like.

`cost_bearing` is an **upper bound**: it means a billable call was attempted, and it stays true
for a call that errored, because that call is on the invoice either way. Where a tier can be
known not to have dialled — Azure ships no specialist for this doctype — it is detected before
the call and reported as `skipped`, so the common no-op case does not inflate the number.

---

## 11. Observability

The number to watch is the **abstention rate**:

```promql
sum(rate(dce_classifications_total{outcome="abstained"}[30m]))
  / sum(rate(dce_classifications_total[30m]))
```

Rising means the corpus drifted away from the registry. Falling to **zero** deserves the same
suspicion: a classifier that never abstains has lost the ability to say "I don't know", which
is the one thing this service must always be able to say.

The second number is **spend**, and it is deliberately three metrics rather than one:

```promql
# what we were billed for, and what it bought
sum by (tier) (rate(dce_extraction_tier_cost_calls_total[1d]))
  / sum by (tier) (rate(dce_extraction_tier_fields_filled_total[1d]))
```

`dce_extraction_tier_invocations_total{tier,outcome}` (did it run at all),
`dce_extraction_tier_cost_calls_total{tier,provider}` (did it bill) and
`dce_extraction_tier_fields_filled_total{tier}` (did it help) answer different questions, and
collapsing them would make cost-per-field — the ratio that decides whether a tier stays
switched on — unanswerable.

The third is the **review SLA**. Depth alone cannot distinguish forty items that are ten minutes
old from forty that have been there a week, so `dce_needs_review_queue_depth` is paired with
`dce_review_time_to_decision_seconds` and `dce_review_decisions_total{decision}`. A rising
rejection rate is a registry signal, not a staffing one.

Supporting signals: `dce_classification_confidence` (are accepts clustering just above the
threshold — i.e. is the margin condition doing all the work?), `dce_classification_margin`,
`dce_classifications_by_doctype_total` (did one class swallow the traffic?),
`dce_classification_tier_seconds{tier}`, `dce_extraction_fill_rate{doctype}`,
`dce_extraction_validator_failures_total{doctype,field,validator}` (the extractor found
something and a checksum rejected it — usually an OCR quality problem, sometimes a wrong
pattern), and `dce_preclassification_egress_blocked_total`, where any nonzero value is a
finding rather than noise.

Every metric label is bounded by the registry — doctype ids, field names, tier names, validator
names, route templates. Nothing is labelled with document content, and no value is ever logged.

---

## 12. Deployment

`python:3.12-slim`, non-root (`uid 10001`), port **8200**, healthcheck on `/health`, read-only
root filesystem with a tmpfs `/tmp` and one writable volume at `/app/data`, all capabilities
dropped.

Two things are deliberately **not** in the image:

* **The BERT checkpoint.** ~1.3 GB, the tier is off by default, and baking it in would make
  every deployment pay for a feature most never enable. Mounted read-only at `/models`;
  `docker-compose.yml` ships the volume commented-but-ready next to `BERT_ENABLED=false`.
* **An HTTP client.** See §2.4. `EXTRA_PACKAGES="httpx>=0.27"` at build time is how a
  deployment that wants T2/T3/T4 gets one, and it is a visible, reviewable line in a build
  command rather than a dependency everybody inherits.

One packaging wrinkle worth recording: the published `bert_uncased_L-12_H-768_A-12` checkpoint
ships TensorFlow + Flax weights and **no** PyTorch `bin`/`safetensors`, so `transformers` needs
`from_tf=True` (requires `tensorflow`) or `from_flax=True` (requires `jax`+`flax`). Neither is
a declared dependency of the `bert` extra, because which one you want depends on your base
image. Converting the checkpoint to `safetensors` once, offline, and mounting that is smaller
and faster than shipping a second framework into a production image.

If `BERT_ENABLED=true` and the directory is absent, `Settings` raises at startup. Loud, not
silent: an operator who asked for BERT should find out in the first thirty seconds, not
discover degraded accuracy three weeks later.

**A half-configured paid tier does not do that**, and the asymmetry is intentional. A missing
BERT directory can only be a mistake about a local file. A missing Azure key is usually a
secret that has not landed yet, and failing the process for it would trade degraded extraction
for a total outage of classification — which still works, still abstains correctly, and is the
part nobody is allowed to lose. So `Settings.tier_problems()` reports rather than raises:
`/readyz` shows the tier as degraded (not not-ready), `/process` says `misconfigured` in
`tiers_used`, and the log says it once, loudly.

---

## 13. Deliberately not built

* **A remote embedding tier.** Removed, see §2.1. Not coming back.
* **Auto-escalation on `/extract`.** The paid tiers run on `/process` only. A route that
  billed per call while an integrator tuned locators would be a trap.
* **Automatic promotion of T2/T3/T4 values to `checksum_verified`.** A vendor confidence is not
  a proof. Only a real check digit moves a value up that rung.
* **A database.** The service is stateless: layout in, classification and fields out. The
  review queue ships two small storage implementations for convenience, but anything durable
  and shared is the deploying team's to own behind `dce.review.ReviewQueue`.
* **Auto-activation of induced schemas.** See §9.
* **Confidence-threshold auto-tuning.** A feedback loop that lowers thresholds to reduce the
  abstention rate optimises exactly the wrong quantity.
* **A review UI.** The API is here; the screen is somebody's frontend. What is *not* negotiable
  is that the double-entry rule stays in `dce/review.py` — a control implemented in a UI is a
  control one API client can walk around.

## 14. Open questions for the next iteration

1. **Validate the registry against real specimens.** §8.1. The largest single source of
   uncertainty in the whole service, and the only one that cannot be fixed by writing code.
2. **Per-page classification on merged PDFs.** `Classification.page_types` exists in the
   contract and `classify_pages` produces segments; the routes currently expose whole-document
   classification. Batch scans of mixed documents are common enough that this should surface.
3. **Calibration data.** The Platt calibration on L2 is identity until someone fits it on a
   labelled corpus. Until then the probabilities are ordered correctly but not calibrated, and
   the accept threshold is doing more work than it should have to.
4. **Feeding review decisions back.** Corrections are the highest-quality labels this system
   will ever see — a human, looking at the pixels, typing the right answer twice. Today they
   stop at the queue. They should refit term profiles and locator priors.
5. **Registry coverage as a metric.** Right now a missing doctype shows up as an abstention
   rate. It would be better to sample the `unknown` queue and cluster it, so "you are missing a
   doctype that accounts for 4% of traffic" is a number rather than an inference.
6. **Per-doctype tier policy.** Today the tier switches are global. A deployment will
   eventually want "T4 for proof-of-address only" — the escalation is per-document, but the
   permission should be per-doctype, and that is a registry field nobody has designed yet.
