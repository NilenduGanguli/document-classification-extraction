# Document Classification & Extraction (DCE)

Tells you **what a document is** and **what it says** — and answers the first question without
letting the document leave the process.

Other business units send this service files nobody has looked at yet: a scan that might be a
PAN card, might be a utility bill, might be a passport page, might be an internal memo that
should never have been uploaded. DCE classifies it against a registry of document types, and
only once a type is accepted does it extract that type's fields, each one carrying the locator,
page and bounding box that produced it.

When it cannot tell, it says so. `unknown` goes to a human queue. It never guesses, and it
never forwards an unclassified document to a model.

---

## The invariant

> **Nothing about an unclassified document leaves this process.**
> No HTTP call, no vendor SDK, no embedding API — not the bytes, not the text, not an
> embedding *of* the text.

This is the reason the service exists as its own container. A business unit that hands you an
unclassified document has not yet decided whether it may leave their trust boundary; that
decision needs the document type, which is precisely what you do not have yet. "We'll just get
an embedding first" is the same disclosure as sending the text — an embedding is derived from
the content, computed by someone else's endpoint, over the wire.

So the whole classification cascade is in-process arithmetic: string anchors, checksum
validators, a zone-weighted BM25 scorer, and — only if you turn it on — a BERT checkpoint you
mounted yourself, running on your own CPU. The design's original embedding-kNN tier, which
called a remote `gte` endpoint, was **removed** for this reason and is not coming back.

How it is held up, in descending order of how much you should trust it:

| Mechanism | Where |
|---|---|
| No HTTP client in the runtime dependencies at all | `pyproject.toml` — no `httpx`, no `requests`, no SDK |
| A test that greps the pre-classification modules for a network import | `tests/test_api.py::test_no_http_client_in_the_classification_path` |
| A runtime guard, `dce.egress`, keyed on `allow_preclassification_egress` | `dce/config.py` |
| `/readyz` reports the invariant's state and returns **503** if it is off | `dce/api/routes.py` |

The first row is checkable on the shipped artifact, not just in the source:

```console
$ docker run --rm --entrypoint python dce:latest -c \
  "import importlib.util as u; print({m: bool(u.find_spec(m)) for m in ('httpx','requests','aiohttp','openai','azure','boto3')})"
{'httpx': False, 'requests': False, 'aiohttp': False, 'openai': False, 'azure': False, 'boto3': False}
```

`allow_preclassification_egress=true` takes the service out of rotation and logs at `error`.
It is an auditable act, not a tuning knob.

---

## The cascade

Each tier is cheap, explainable, and adds evidence to a fused score. Every classification comes
back with the `evidence[]` that produced it, because an unexplainable classification is not
auditable and this is a KYC system.

| Tier | What it does | Why it is there |
|---|---|---|
| **L0 structural prior** | page count, aspect ratio, number of marks and tables, digital vs scanned | A 1-page 3.37×2.13 card is not a 40-page statement. Free, and it kills whole branches. |
| **L1 anchors + checksums** | decisive anchor strings (`INCOME TAX DEPARTMENT`), plus identifiers whose checksum validates (Verhoeff/Aadhaar, PAN, SSN, SIN, CURP, RFC, MRZ) | A checksum-valid identifier in the right context is near-proof. Weighted **3.0** on purpose. |
| **L2 zone-weighted BM25** | per-doctype term profiles scored over the layout's zones | The same word is worth 3.0 in a title and 0.25 in a page footer. This is what makes it better than grep — and the zones come free from the layout payload. |
| **L3 local BERT kNN** *(optional, off)* | a mounted checkpoint, in-process, CPU | Only if L1+L2 prove insufficient on your corpus. See below. |
| **L4 abstain** | `unknown` → human queue | The tier that makes the other four safe. |

```
score_c = log P(c | structure) + 3.0·anchor + 1.0·lexical + 0.8·bert     # weights in config
accept  ⟺  p ≥ 0.65  AND  margin ≥ 0.25  AND  coverage ≥ 0.20
```

All three accept conditions must hold. Probability alone accepts a document that merely looks
*more* like a PAN card than anything else in the registry; the margin makes it beat the
runner-up, and coverage makes it actually contain the class's vocabulary rather than winning by
elimination.

**Tuning note.** The number to watch is the abstention rate
(`dce_classifications_total{outcome="abstained"}`). If it climbs, the fix is almost always a
doctype or a term profile — not a lower threshold. Lowering the threshold does not make the
service more accurate; it converts abstentions into silent misclassifications, which cost far
more downstream than a human glance.

## Extraction

Runs **only** after a doctype is accepted. Each `FieldSpec` lists its locators in priority
order; the first one that produces a candidate surviving validation wins, and the result records
which one it was.

| Locator | Binds a field by |
|---|---|
| `kv` | a provider-detected key/value pair |
| `label` | a label anchored in the layout, looking right-of / below within the configured window, with a fuzzy (rapidfuzz) label match |
| `table` | table cell addressing, including header lookup |
| `mark` | a checkbox/radio — on a KYC form, which box is ticked often *is* the answer |
| `regex` | a value-shape pattern |
| `mrz` | the ICAO 9303 machine-readable zone |

Every `ExtractedField` carries `locator`, `page`, `bbox`, and a `verification` level
(`unverified` → `format_valid` → `checksum_verified` → `cross_verified` → `human_verified`), so
a reviewer can see the exact pixels a value came from and how much the machine trusts it.

---

## The API

Base path `/api/v1`. `X-API-Key` is enforced when `API_KEY` is set, and is never required on
the probe/scrape routes. Every response carries `X-Elapsed-Ms`; composite responses also carry
a `timings` object.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/classify` | Classify. `200` with `abstained: true` is a valid answer, not an error. |
| POST | `/api/v1/extract` | Extract. Pin `doctype_id`, or omit it to classify first. Unknown id → `404`. |
| POST | `/api/v1/process` | **The common path.** Classify, then extract. On abstention it returns the classification with `needs_review` and does *not* extract. |
| GET | `/api/v1/doctypes` | The registry: id, label, country, category, field names. Filter by `?country=` / `?category=`. |
| GET | `/api/v1/doctypes/{id}` | Full spec: anchors, id patterns, confusables, field locators. |
| GET | `/api/v1/schemas/{id}` | The active schema for a doctype. |
| POST | `/api/v1/schemas/induce` | Draft a schema from sample layouts. Always returns `active: false`. |
| GET | `/health` | Liveness. Never touches an engine. |
| GET | `/readyz` | Registry size, BERT enabled/loaded, **egress invariant state**. `503` when not ready. |
| GET | `/metrics` | Prometheus exposition. |

Every document-bearing request accepts whichever form you have:

```jsonc
{ "doc_id": "abc",
  "layout":               { /* an already-adapted LayoutView */ },
  "azure_analyze_result": { /* Azure prebuilt-layout, job or analyzeResult */ },
  "des_ocr":              { /* DES /api/runs/{id}/pages/{n}/ocr */ },
  "text":                 "…"   /* the degraded path — still works */ }
```

The first one present wins, in that order. `text` alone works, but loses the zone weighting
that makes L2 good; nothing gets promoted to `title` on a guess, because a wrong title is
amplified ×3 and turns an abstention into a confident mistake.

```bash
curl -s localhost:8200/api/v1/process \
  -H 'content-type: application/json' -H "X-API-Key: $API_KEY" \
  -d '{"text": "INCOME TAX DEPARTMENT\nPermanent Account Number\nABCDE1234F"}'
```

---

## Adding a doctype

This is the one thing an integrator actually does. A doctype is a single `DocTypeSpec`: how to
recognise it and what to pull out of it, declared together — because "this is an Aadhaar card"
and "an Aadhaar card has a 12-digit UID with a Verhoeff check" are the same knowledge, and they
drift apart the moment you split them across two files.

```python
DocTypeSpec(
    doctype_id="in_pan",                       # stable id; never renamed once in use
    label="Permanent Account Number card",
    country="IN",
    category=Category.identity,
    issuing_authority="Income Tax Department",
    officially_valid=True,                     # RBI OVD — regulatory weight, not a tag
    anchors=[
        Anchor(text="income tax department", decisive=True, zone=Zone.title),
        Anchor(text="permanent account number", decisive=True),
        Anchor(text="आयकर विभाग", lang="hi"),
    ],
    id_patterns=[r"\b[A-Z]{5}\d{4}[A-Z]\b"],   # decisive when the checksum validates too
    confusable_with={"in_tan": "deductor"},    # and the term that separates them
    negative_anchors=["challan"],
    fields=[
        FieldSpec(
            name="pan_number", attribute_key="id.pan", type="id",
            required=True, pii=True,
            labels={"en": ["Permanent Account Number", "PAN"]},
            pattern=r"[A-Z]{5}\d{4}[A-Z]",
            validator="pan",
            locators=["kv", "label", "regex"],
        ),
    ],
    handling="mask all but last 4 in logs and exports",
)
```

Checklist that actually matters:

1. **`decisive=True` is a promise.** Use it for issuing-authority headers and form numbers —
   strings that cannot plausibly appear on another document type. `Anchor(text="account
   number")` is not decisive; it is on every statement ever printed.
2. **Anchors are matched on word tokens, never substrings.** That is why `DL` no longer fires
   inside "mi**dl**e" and `SIN` no longer fires inside "u**sin**g" (see `dce.models.tokenize`).
3. **Fill in `confusable_with`.** The registry's failure mode is two near-identical forms, and
   the discriminating term is the cheapest fix there is.
4. **Reuse `attribute_key`.** Use the existing dotted namespace (`id.*`, `identity.*`,
   `address.*`, `doc.*`) so a value extracted here merges with the same fact from another
   document.
5. **Prefer a `validator` over a longer `pattern`.** A regex that matches shape accepts OCR
   noise; a checksum rejects it, and promotes the field to `checksum_verified`.
6. **Add both languages** where the document is bilingual. Anchors carry `lang`.
7. **Ship a test document.** A doctype with no fixture is a doctype nobody can refactor.

`POST /api/v1/schemas/induce` will draft the `fields` list from sample layouts (it reads
provider key-value pairs and table headers that recur across your samples). It returns
`active: false` and always will — induction saves typing, it does not get to decide what the
service extracts.

---

## The optional local BERT tier

**Off by default, and most deployments should leave it off.** Anchors and lexical scoring
handle the registry's document types well because those types announce themselves in print. The
tier earns its place only on a specific failure: a corpus where two doctypes share vocabulary
and neither has a decisive anchor, and where the abstention rate stays high after you have
already added term profiles and confusables. Check the metrics first — if abstentions are
spread across many doctypes, BERT is not your problem; your registry is incomplete.

It runs **in-process on a mounted checkpoint**, which is what keeps it inside the invariant:

```bash
# the model is NOT in the image (~1.3 GB, and most deployments never use it)
docker run -v /srv/models:/models:ro -e BERT_ENABLED=true \
           -e BERT_MODEL_DIR=/models/bert_uncased_L-12_H-768_A-12 …
```

Install the extra deliberately: `pip install '.[bert]'`. Note that the published
`bert_uncased_L-12_H-768_A-12` checkpoint ships **TensorFlow + Flax** weights and no PyTorch
`bin`/`safetensors`, so `transformers` needs `from_tf=True` (which requires `tensorflow`) or
`from_flax=True` (which requires `jax`+`flax`). Converting the checkpoint to `safetensors` once,
offline, and mounting that is smaller and faster than shipping a second framework.

If `BERT_ENABLED=true` and the directory is missing, the service refuses to start. An operator
who asked for BERT should find out immediately, not discover degraded accuracy three weeks
later.

---

## Running it

```bash
uv venv && uv pip install -e '.[dev]'
uvicorn dce.api.app:app --port 8200 --reload

pytest -q          # offline and pure: no network, no DB, no model download
ruff check .
```

```bash
docker compose up --build          # http://localhost:8200/docs
```

The image is `python:3.12-slim`, runs as a non-root user, exposes **8200**, and has a
healthcheck on `/health`. The BERT volume is present but commented out in `docker-compose.yml`
with `BERT_ENABLED=false`, ready to uncomment.

### Configuration

Everything in `dce/config.py`, settable by environment variable. The ones you will touch:

| Variable | Default | What it does |
|---|---|---|
| `API_KEY` | *(empty)* | Enables the `X-API-Key` gate. Empty = open (fine behind a mesh). |
| `ALLOW_PRECLASSIFICATION_EGRESS` | `false` | **The invariant.** True takes the service out of rotation. |
| `CLASSIFY_ACCEPT_PROBABILITY` | `0.65` | Accept threshold. |
| `CLASSIFY_MIN_MARGIN` | `0.25` | Required lead over the runner-up. |
| `CLASSIFY_MIN_COVERAGE` | `0.20` | Required share of the class profile observed. |
| `ZONE_WEIGHT_TITLE` / `_HEADING` / `_BODY` / `_TABLE` / `_FURNITURE` | `3.0 / 2.0 / 1.0 / 1.2 / 0.25` | Lexical zone weights. |
| `LABEL_WINDOW_X` / `LABEL_WINDOW_Y` | `0.55 / 0.06` | How far right-of / below a label to look, as a fraction of the page. |
| `FUZZY_LABEL_MIN_SCORE` | `88` | rapidfuzz floor for a label match. |
| `BERT_ENABLED` / `BERT_MODEL_DIR` | `false` / `/models/…` | The optional local tier. |

### Metrics worth an alert

```promql
# abstention rate — the health of the whole cascade
sum(rate(dce_classifications_total{outcome="abstained"}[30m]))
  / sum(rate(dce_classifications_total[30m]))

# anything above zero here is a finding, not noise
sum(rate(dce_preclassification_egress_blocked_total[1h]))

# fields the extractor found but a checksum rejected
sum by (doctype, field) (rate(dce_extraction_validator_failures_total[1h]))
```

Also exposed: `dce_classification_tier_seconds{tier}`, `dce_classification_confidence`,
`dce_classification_margin`, `dce_classifications_by_doctype_total`,
`dce_extraction_fill_rate{doctype}`, `dce_needs_review_queue_depth`.

An abstention rate of **zero** deserves the same suspicion as a high one: a classifier that
never abstains has stopped being able to say "I don't know", which is the one thing this
service must always be able to say.

---

See [`docs/DESIGN.md`](docs/DESIGN.md) for the reasoning behind the cascade, the fusion weights,
and what was deliberately left out.
