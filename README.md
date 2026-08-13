# Document Classification & Extraction (DCE)

Tells you **what a document is** and **what it says** — and answers the first question without
letting the document leave the process.

Other business units send this service files nobody has looked at yet: a scan that might be a
PAN card, might be a utility bill, might be a passport page, might be an internal memo that
should never have been uploaded. DCE classifies it against a registry of 121 document types,
and only once a type is accepted does it extract that type's fields, escalating from a free
local resolver to paid tiers only for the fields the cheaper ones could not find — each value
carrying the locator, page and bounding box that produced it.

When it cannot tell, it says so. `unknown` goes to a human queue. It never guesses, and it
never forwards an unclassified document to anybody.

---

## The invariant, in both directions

> **Before a doctype is accepted: nothing leaves this process.**
> No HTTP call, no vendor SDK, no embedding API — not the bytes, not the text, not an
> embedding *of* the text.
>
> **After a doctype is accepted: calling out is allowed, and that is the whole point.**

The first half is why the service exists as its own container. A business unit that hands you
an unclassified document has not yet decided whether it may leave their trust boundary; that
decision needs the document type, which is precisely what you do not have yet. "We'll just get
an embedding first" is the same disclosure as sending the text — an embedding is derived from
the content, computed by someone else's endpoint, over the wire.

The second half is why the extraction tiers exist at all. Once the cascade has placed a
document *on its own evidence*, the caller knows what they are holding and can make an informed
decision about where it may go. A W-9 that has been identified as a W-9 can be sent to a model
that reads W-9s. An unidentified scan cannot be sent anywhere, by anyone, for any reason.

So the whole classification cascade is in-process arithmetic: string anchors, checksum
validators, a zone-weighted BM25 scorer, and — only if you turn it on — a BERT checkpoint you
mounted yourself, running on your own CPU. The design's original embedding-kNN tier, which
called a remote `gte` endpoint, was **removed** for this reason and is not coming back.

How the first half is held up, in descending order of how much you should trust it:

| Mechanism | Where |
|---|---|
| No HTTP client in the runtime dependencies at all | `pyproject.toml` — no `httpx`, no `requests`, no SDK |
| The paid tiers import their client *inside* the call, so nothing imports one on the way in | `dce/extract/azure_specialist.py`, `dce/extract/llm_field.py` |
| A test that greps the pre-classification modules for a network import | `tests/test_api.py::test_no_http_client_in_the_classification_path` |
| A runtime scope guard, `dce.egress`, that raises inside classification | `dce/egress.py` |
| Every paid tier refuses `doctype_id == unknown`, and the router raises before it can call one | `dce/api/routes.py::run_tier_cascade`, `tests/test_api_tiers.py` |
| `/readyz` reports the invariant's state and returns **503** if it is off | `dce/api/routes.py` |

The first row is checkable on the shipped artifact, not just in the source:

```console
$ docker run --rm --entrypoint python dce:latest -c \
  "import importlib.util as u; print({m: bool(u.find_spec(m)) for m in ('httpx','requests','aiohttp','openai','azure','boto3')})"
{'httpx': False, 'requests': False, 'aiohttp': False, 'openai': False, 'azure': False, 'boto3': False}
```

`allow_preclassification_egress=true` takes the service out of rotation and logs at `error`.
It is an auditable act, not a tuning knob.

### What changes when you enable a paid tier

Be clear-eyed about this. T2/T3/T4 need an HTTP client, and the default image has none — so
enabling them means building with `EXTRA_PACKAGES="httpx>=0.27"`, and **from that build on, the
first row of that table no longer applies to you.** The capability is present in the image.

What still applies, and what the guarantee then rests on:

* `dce/egress.py` — `assert_no_egress` raises inside a classification scope, and
  `post_classification_scope` refuses to open at all for `unknown`.
* The abstain rule — an abstention returns from `/process` before any tier is considered, and
  `run_tier_cascade` **raises** rather than skipping if it is ever reached with one.
* `tests/test_egress.py` — the socket tripwire: the classification path is run with `socket`
  patched to raise, so the test asserts *zero connections were attempted*, not that a code path
  was not taken. That test is the proof that survives adding an HTTP client to the image, and
  it is why it exists in that form.

Run the zero-egress build unless you have a documented reason not to. Enabling a tier is a
per-deployment decision with a per-page price and a data-flow consequence, not a default.

### Reading an image: three answers, and only one of them is egress

An image carries no text. Classifying one **requires** optical recognition, and recognition
happens either in this process or on another host — there is no third place. That is a genuine
trade-off, not a bug to code around, so the service offers all three answers and makes it
obvious which one a deployment has taken.

| | Who calls the recogniser | Zone roles | Call out of this process | Default |
|---|---|---|---|---|
| **(A) Caller-supplied** — post the result to `/classify` as `azure_analyze_result` (either product), `azure_read_result` or `des_ocr` | your upstream service, under its own authorisation | Layout: yes. Read: no | **none — no socket is opened** | recommended |
| **(B) Local OCR** — `DCE_INGEST_LOCAL_OCR_ENABLED=true` plus the `ocr-rapidocr` / `ocr-tesseract` extra | nobody | no | none | off |
| **(C) An OCR service** — `DCE_INGEST_OCR_SERVICE_ENABLED=true` plus an endpoint plus `EXTRA_PACKAGES="httpx>=0.27"` | **this service, before the doctype is known** | Layout: yes. Read: no | **yes** | off |

**Prefer (A).** It gives you Azure-quality text *and* leaves the invariant untouched, because
the recognition happens where the document already legitimately is. All three feed the same
adapters and the same cascade — the only difference is who dialled.

**(B) and (C) can be configured together, and more than one provider at a time.** Set
`DCE_INGEST_OCR_DEFAULT_PROVIDER` to say which one runs when a request names none, and a
caller then selects among them per request with `ingest.ocr_provider`. The pin chooses among
what the deployment configured; it can never add a provider. Configuring both without naming
a default stops the process at boot rather than the code picking one silently.

**(C) is an auditable act, not a tuning knob.** It is off by default; the default image has no
HTTP client, so it cannot be taken by accident; every request passes
`dce.egress.assert_ocr_egress_permitted`, which names the provider and the endpoint and refuses
inside a classification scope; and a deployment that has taken it says so. **Whose network the
endpoint is on is declared, not inferred** — `DCE_INGEST_OCR_SERVICE_TRUST_BOUNDARY` is
`external` by default, so a deployment that declares nothing gets the cautious reading below;
declaring `on_premises` makes the same block read as configuration ("images are read by
`azure_layout` at `<host>`, which this deployment declares is on its own network") without
changing a single fact it reports. The settings were once called `DCE_INGEST_REMOTE_OCR_*`;
those names are still read as aliases and the service names them at boot.

```console
$ curl -s localhost:8200/readyz | jq '{ocr, preclassification_ocr: .egress.preclassification_ocr}'
{
  "ocr": {
    "provider": "azure_layout",
    "enabled": true,
    "network": true,
    "endpoint_host": "contoso.cognitiveservices.azure.com",
    "problem": "",
    "summary": "THIS DEPLOYMENT TRANSMITS UNCLASSIFIED DOCUMENTS to contoso.cognitiveservices.azure.com — …"
  },
  "preclassification_ocr": true
}
```

Per document, the same fact rides on every response: `X-Document-Source` on `/classify` and
`/extract`, and a `source` block on `/process`.

**Read v3.2 and Layout v4.0 are not interchangeable.** Read returns lines and words only — no
`paragraphs[].role` — so every block lands in `body`, and the 30 registry anchors gated on
`zone=title` (21 of them decisive) can never fire on a Read payload. The cascade records those
as *unevaluable* rather than *failed*, so Read is a lower-**recall** provider and not a
lower-precision one: it abstains where Layout accepts. Prefer `prebuilt-layout` where you have
it, on either path.

---

## The cascade — deciding what a document is

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

---

## Extraction — the five tiers

Extraction runs **only** after a doctype is accepted, and escalates: each tier is asked only
for the fields the tiers before it could not fill, and stops the moment nothing is missing.
`POST /api/v1/process` reports what actually ran in `tiers_used`.

| Tier | What it is | Cost | Runs when | Needs |
|---|---|---|---|---|
| **T1 local resolver** | layout-anchored locators + validators, in-process | **free** | always | nothing |
| **T2 Azure prebuilt** | `prebuilt-idDocument`, `tax.us.w2/1099/1040`, `bankStatement.us`, `payStub.us` | per **page** | `t2_enabled`, fields still missing, **and Azure ships a model for this doctype** (14 of 121 today) | `AZURE_DI_*`, `content_base64`, httpx |
| **T3 Azure queryFields** | the layout model asked for named fields nobody trained it on | per **field** (max 20/request) | `t3_enabled`, fields still missing | `AZURE_DI_*`, `content_base64`, httpx |
| **T4 constrained LLM** | JSON-schema-constrained completion over a *window* of the text | per **token** | `t4_enabled`, fields still missing | `LLM_*`, httpx |
| **T5 human review** | a person, one field at a time | a person's minutes | the result still needs one | nothing |

**Every one of T2/T3/T4 is off by default.** A deployment that wants no egress at all gets it
by doing nothing: there is no flag to find and no endpoint to unset.

Three rules hold the escalation together, and all three are enforced at the call site in
`dce/api/routes.py` rather than trusted to the tiers:

1. **No tier runs on an unclassified document.** An abstention returns from `/process` before
   the cascade is even considered, and `run_tier_cascade` raises if it is ever reached with
   one. The tier modules assert it a second time.
2. **A tier can only fill what is missing.** What it returns is filtered: a value for a field
   that already has one is dropped. A model asked for three fields will volunteer a fourth, and
   letting a fluent guess displace a checksum-verified value would invert the entire
   verification ladder.
3. **Provenance survives the merge.** A field filled by a tier has that tier prefixed onto its
   `locator` (`t4_llm:llm`), so a reviewer can always see which values came from a model.

### T1 — the local resolver

Each `FieldSpec` lists its locators in priority order; the first that produces a candidate
surviving validation wins, and the result records which one it was.

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

### T2 — Azure prebuilt specialists

For the documents where a *trained* model beats any locator: a passport photo page, a W-2's box
grid, a bank statement's transaction table. The doctype→model map is deliberately conservative
(`dce/extract/azure_specialist.py::SPECIALIST_MODELS`): pointing `prebuilt-idDocument` at a
document it was **not** trained on returns fields with a plausible confidence and the wrong
values, which is worse than returning nothing. A doctype with no specialist is skipped *before*
the call — it costs nothing and is not counted as spend.

T2 analyses the **file**, not our reading of it, so `/process` must be given `content_base64`.
Without it the tier reports `skipped` and says why.

Nothing from T2 is ever `checksum_verified`: Azure returns a confidence, and a confidence is
not a proof. That one rule is what lets T1 and T2 results be merged without a vendor's guess
ever displacing a real check digit.

### T3 — Azure `queryFields`

The layout model, asked for named fields no prebuilt model covers. Billed per field and capped
at 20 per request (Azure's own maximum); `T3_MAX_QUERY_FIELDS` can lower it. Fields are asked
for in schema priority order, so the cap truncates the tail rather than something arbitrary.

### T4 — the constrained LLM

Last resort, for fields no deterministic locator and no Azure model could bind — unstructured
proof-of-address documents are the motivating case. Four things make it usable in a KYC system:

* **It sees a window, not the document.** The tier builds a text window around the missing
  fields' labels (`LLM_MAX_WINDOW_CHARS`, default 6000). Less to disclose, less to pay for, and
  a shorter haystack for a model being asked to quote from it.
* **The answer is schema-constrained.** A JSON schema built from the `FieldSpec`s, not free
  text. Prose answers are discarded whole — there is no salvage path that scrapes a value out
  of a sentence, because that is the habit that produces ungrounded values.
* **Every value must be quoted from the window.** A returned value whose supporting quote
  cannot be located in the text it was given is dropped. That is the difference between
  extraction and generation.
* **It is validated like everything else.** The same `pattern` and `validator` as T1, and it
  never rises above `format_valid` without a real check digit.

### T5 — the human review queue

See below. It is not a fallback for the others; it is where the whole cascade is *allowed* to
give up, which is what makes every tier above it safe to switch on.

---

## The review queue and the double-entry rule

Every automated tier is allowed to decline, and all the declines land in one place. `dce/review.py`
holds the state machine; `GET /api/v1/review` and the three decision routes are its surface.

**What reaches a human**, one queue item per problem:

| Reason | What happened |
|---|---|
| `classification_abstained` | the cascade could not place the document. One item for the whole document — there are no fields to itemise, and "what is this?" is the only question worth asking. |
| `missing_required` | a required field came back empty after every enabled tier |
| `below_confidence_threshold` | a value arrived under `EXTRACT_ACCEPT_CONFIDENCE` |
| `validator_error` | a value was found and its validator complained |

**Items are per field, not per document** (`"<doc_id>:<field_name>"`). A reviewer decides one
field at a time, because that is what a reviewer actually does — and because one approval
covering a whole document would be an approval by somebody who looked at one field of it. Ids
are stable, so re-processing a document does not resurrect a decision somebody already made.

**Blind double entry, where it actually matters.** A field that is **both PII and backed by a
real check digit** — an Aadhaar number, a CURP, a SIN — takes **two independent decisions**:

* `approve` twice, by two *different* reviewers. The first is recorded and the item stays
  `pending` (a `200` with `status: pending` is a success, not a failure). The same person
  signing twice is a `409`: two signatures from one pair of eyes is precisely the failure the
  control exists to prevent.
* `correct` twice, by two different reviewers, **matching**. A mismatch discards *both* entries
  and returns `409` — the item goes back to square one rather than inheriting the value one of
  the two people got wrong.

Everything else takes one decision, because a control nobody has time to follow is not a
control. Rejection always takes one: it is the safe direction, and it puts nothing into a
record. Which fields need four eyes is decided from the `FieldSpec` (`pii` + a checksum
validator), not from a UI setting — a control that lives in a frontend is a suggestion.

Why this rule and not "review everything twice": a typo in a checksummed identifier silently
becomes a *valid-looking* identifier belonging to somebody else, and no downstream system can
tell. That is the class of error single-keyed data entry is known to produce and double entry is
known to catch.

Storage is a `Protocol` with two implementations shipped: `memory` (a single process; loses
everything on restart) and `file` (a JSON file an operator can read with `cat`). A team with
Postgres implements five methods and loses nothing. The queue is intentionally not a database
this service owns.

---

## The doctype registry — 121 types across four countries

| Country | Total | Identity | Address proof | Tax | Corporate | Financial | Other |
|---|---:|---:|---:|---:|---:|---:|---:|
| 🇮🇳 India | **36** | 12 | 7 | 4 | 7 | 5 | 1 |
| 🇺🇸 United States | **35** | 12 | 2 | 9 | 8 | 4 | — |
| 🇨🇦 Canada | **25** | 11 | 3 | 4 | 6 | 1 | — |
| 🇲🇽 Mexico | **20** | 8 | 5 | 4 | 2 | 1 | — |
| 🌐 Cross-country (`XX`) | **5** | 2 | 1 | — | — | 1 | 1 |
| **Total** | **121** | **45** | **18** | **21** | **23** | **12** | **2** |

28 of them are flagged `officially_valid` — RBI "Officially Valid Document" and equivalents,
which is regulatory weight rather than a tag. Between them the packs carry ~1,270 classification
anchors and ~909 field specifications. `GET /api/v1/doctypes?country=IN` lists them.

> **Be honest about what this is.** The registry was **authored from published specifications,
> form templates and public documentation — it has not been validated against a corpus of real
> specimens.** Anchor strings, form numbers and field labels are as documented; what an actual
> scan of a 2016-revision form OCRs to is an empirical question this repo has not yet answered.
> Expect to correct anchors and term profiles against your own traffic before trusting the
> abstention rate as a quality signal. The first deployment's job is to produce that corpus:
> sample the `unknown` queue, and fix the registry rather than the thresholds.

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
   noise; a checksum rejects it, promotes the field to `checksum_verified` — and, if the field
   is also `pii`, is what puts it under the double-entry rule in review.
6. **Add both languages** where the document is bilingual. Anchors carry `lang`.
7. **Ship a test document.** A doctype with no fixture is a doctype nobody can refactor.

`POST /api/v1/schemas/induce` will draft the `fields` list from sample layouts (it reads
provider key-value pairs and table headers that recur across your samples). It returns
`active: false` and always will — induction saves typing, it does not get to decide what the
service extracts.

---

## Enabling a tier

Each tier takes three deliberate steps, and `/readyz` reports any tier that has some of them
and not the others (`degraded`, never `not ready` — a half-configured paid tier costs you those
fields; taking the process out of rotation would cost you classification, which still works).

**Common to T2/T3/T4 — install an HTTP client.** The image ships none:

```bash
docker compose build --build-arg EXTRA_PACKAGES="httpx>=0.27"
# or: EXTRA_PACKAGES="httpx>=0.27" docker compose build
```

Without it, an enabled tier reports `status: "unavailable"` in `tiers_used` and the document
falls through to the next tier. Nothing breaks; nothing is silently skipped either.

**T2 — Azure prebuilt specialists**

```bash
T2_ENABLED=true
AZURE_DI_ENDPOINT=https://<resource>.cognitiveservices.azure.com
AZURE_DI_KEY=<from your secret store>
```

Callers must send `content_base64` on `/process`. Check `SPECIALIST_MODELS` first: if your
doctypes are not in it, T2 will correctly do nothing and you have configured a cost centre for
no benefit. Extending that map is a deliberate act, one doctype at a time, after measuring the
specialist on real documents of that type.

**T3 — Azure `queryFields`**

```bash
T3_ENABLED=true          # same AZURE_DI_* credentials as T2
T3_MAX_QUERY_FIELDS=20   # Azure's cap; lower it to cap spend per page
```

**T4 — constrained LLM**

```bash
T4_ENABLED=true
LLM_BASE_URL=https://<your-endpoint>/v1     # OpenAI-compatible
LLM_API_KEY=<from your secret store>
LLM_MODEL=<pinned model id>
LLM_MAX_WINDOW_CHARS=6000
LLM_TIMEOUT_SECONDS=20
```

Point `LLM_BASE_URL` at a self-hosted model if you want the documents to stay inside your own
boundary; the tier does not care whose model it is.

**T5 — the review queue** is on by default with the `memory` backend. For anything beyond one
replica, either use `file` or implement `dce.review.ReviewQueue` over your own store:

```bash
REVIEW_QUEUE_BACKEND=file
REVIEW_QUEUE_PATH=/app/data/review_queue.json   # a writable mount; the rootfs is read-only
```

---

## The API

Base path `/api/v1`. `X-API-Key` is enforced when `API_KEY` is set, and is never required on
the probe/scrape routes. Every response carries `X-Elapsed-Ms`; composite responses also carry
a `timings` object.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/classify` | Classify. `200` with `abstained: true` is a valid answer, not an error. |
| POST | `/api/v1/extract` | Extract with **T1 only** — free, no egress. Pin `doctype_id`, or omit it to classify first. Unknown id → `404`. |
| POST | `/api/v1/process` | **The common path.** Classify, then escalate T1 → T2 → T3 → T4 → T5. Reports `tiers_used`. On abstention it returns the classification and calls nobody. |
| GET | `/api/v1/doctypes` | The registry: id, label, country, category, field names. Filter by `?country=` / `?category=`. |
| GET | `/api/v1/doctypes/{id}` | Full spec: anchors, id patterns, confusables, field locators. |
| GET | `/api/v1/schemas/{id}` | The active schema for a doctype. |
| POST | `/api/v1/schemas/induce` | Draft a schema from sample layouts. Always returns `active: false`. |
| GET | `/api/v1/review` | The human queue. `?status=pending\|approved\|rejected\|corrected\|all`, `?doctype=`, `?limit=`. |
| POST | `/api/v1/review/{id}/approve` | Accept the value. Double-entry items need two different reviewers. |
| POST | `/api/v1/review/{id}/reject` | Discard it. One reviewer — rejection is the safe direction. |
| POST | `/api/v1/review/{id}/correct` | Type the correct value. Double-entry items need two matching, independent entries. |
| GET | `/health` | Liveness. Never touches an engine. |
| GET | `/readyz` | Registry size, BERT, **egress invariant**, and the tier posture. `503` when not ready. |
| GET | `/metrics` | Prometheus exposition. |

`/extract` deliberately does not escalate: it is the surface an integrator calls in a loop while
tuning locators, and a route that quietly billed per call would be a trap.

Every document-bearing request accepts whichever form you have:

```jsonc
{ "doc_id": "abc",
  "layout":               { /* an already-adapted LayoutView */ },
  "azure_analyze_result": { /* Azure prebuilt-layout, job or analyzeResult */ },
  "des_ocr":              { /* DES /api/runs/{id}/pages/{n}/ocr */ },
  "text":                 "…",  /* the degraded path — still works */
  "content_base64":       "…"   /* optional; ONLY T2/T3 read it, only after acceptance */ }
```

The first of the first four present wins, in that order. `text` alone works, but loses the zone
weighting that makes L2 good; nothing gets promoted to `title` on a guess, because a wrong title
is amplified ×3 and turns an abstention into a confident mistake.

```bash
curl -s localhost:8200/api/v1/process \
  -H 'content-type: application/json' -H "X-API-Key: $API_KEY" \
  -d '{"text": "INCOME TAX DEPARTMENT\nPermanent Account Number\nABCDE1234F"}'
```

A `/process` response now carries a tier ledger — what ran, what it produced, what it cost:

```jsonc
{ "classification": { "doctype_id": "in_pan", "confidence": 0.94, "…": "…" },
  "extraction":     { "fields": [ /* … each with locator, page, bbox, verification */ ] },
  "needs_review": false,
  "tiers_used": [
    { "tier": "t1_local",          "status": "ran",     "fields_filled": 1, "fields": ["pan_number"],  "ms": 4,    "cost_bearing": false },
    { "tier": "t2_azure_prebuilt", "status": "skipped", "fields_filled": 0, "detail": "no azure model covers in_pan; staying on T1" },
    { "tier": "t4_llm",            "status": "ran",     "fields_filled": 1, "fields": ["holder_name"], "ms": 1840, "cost_bearing": true }
  ],
  "review_ids": [],
  "timings": { "total_ms": 1851, "classify_ms": 3, "extract_ms": 4, "tiers_ms": 1841 } }
```

`status` is one of `ran` · `skipped` (not applicable, or no file supplied) · `misconfigured`
(enabled, no endpoint) · `unavailable` (no module, or no HTTP client) · `error` · `queued` (T5).
`cost_bearing` means a billable call was **attempted** — an error after the call was made is
still on the invoice.

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

Install the extra deliberately: `pip install '.[bert]'` (torch + transformers). That is the only
framework the runtime needs, and the only one it can use.

### If your approved checkpoint is a TensorFlow checkpoint, convert it first

The original Google release — and most company-approved rebuilds of it — is `bert_config.json`,
`config.json`, `vocab.txt` and `bert_model.ckpt.{index,data-*,meta}`, with **no**
`pytorch_model.bin` and **no** `model.safetensors`. `transformers` 5.x **removed** TensorFlow and
Flax support, so `from_tf=True` / `from_flax=True` are ignored and installing `tensorflow` or
`jax`+`flax` will not help. Convert it once, offline. The converter reads the checkpoint's
`.index`/`.data-*` files directly and needs neither TensorFlow nor torch:

```bash
pip install '.[bert-convert]'                       # numpy + safetensors, nothing else
python tools/convert_bert_tf_checkpoint.py convert /srv/models/bert_uncased_L-12_H-768_A-12
# -> model.safetensors written beside the checkpoint; every tensor CRC32C-checked on the way in
```

Then mount the directory as above. Nothing is downloaded at any point, by the service or by the
converter. If a known-good HuggingFace copy of the same checkpoint is available on a build host,
`... verify <dir> --against <known-good-dir>` compares every tensor and the pooled embedding both
models produce for the same text; on this repo's copies it reports a max absolute difference of
exactly `0.000e+00`.

If `BERT_ENABLED=true` and the directory is missing, the service refuses to start. An operator
who asked for BERT should find out immediately, not discover degraded accuracy three weeks
later. If the directory is present but holds only a TF checkpoint, the tier reports that
specifically and names the converter — see `dce/classify/bert_knn.py`.

---

## Running it

```bash
uv venv && uv pip install -e '.[dev]'
uvicorn dce.api.app:app --port 8200 --reload

pytest -q          # offline and pure: no network, no DB, no model download
ruff check .
```

```bash
cp .env.example .env               # every value in it is already the default
docker compose up --build          # http://localhost:8200/docs
```

The image is `python:3.12-slim`, runs as a non-root user, exposes **8200**, has a healthcheck on
`/health`, a read-only root filesystem and all capabilities dropped. The BERT volume is present
but commented out with `BERT_ENABLED=false`, ready to uncomment.

### The console

A review console is served by this same process at `/` — four pages: `/analyze` (run a document
and read the decision trail), `/registry`, `/review`, `/posture`. It is a React + TypeScript
bundle under `frontend/`, and it is **entirely self-contained**: no CDN, no web font, no remote
anything. A service whose whole argument is that documents do not leave must not ship a console
that phones out, so everything is in the bundle.

`docker compose up --build` needs no extra step — the Dockerfile's `ui` stage compiles it, and
the runtime image carries the built assets and no JavaScript toolchain at all. From a checkout:

```bash
cd frontend && npm install && npm run build     # -> frontend/dist, which is committed
npm run dev                                     # :5173, proxies /api and /readyz to :8200
```

`frontend/dist` is committed, so a plain `uvicorn dce.api.app:app` serves the console as well.
If it is missing nothing breaks: the API, the probes and `/docs` all serve as normal, and `/`
returns the service card with the build command in it. `DCE_FRONTEND_DIST` overrides where the
bundle is read from.

### Configuration

Everything in `dce/config.py`, settable by environment variable. See `.env.example` for the
annotated set; these are the ones you will touch:

| Variable | Default | What it does |
|---|---|---|
| `API_KEY` | *(empty)* | Enables the `X-API-Key` gate. Empty = open (fine behind a mesh). |
| `ALLOW_PRECLASSIFICATION_EGRESS` | `false` | **The invariant.** True takes the service out of rotation. |
| `CLASSIFY_ACCEPT_PROBABILITY` | `0.65` | Accept threshold. |
| `CLASSIFY_MIN_MARGIN` | `0.25` | Required lead over the runner-up. |
| `CLASSIFY_MIN_COVERAGE` | `0.20` | Required share of the class profile observed. |
| `ZONE_WEIGHT_TITLE` / `_HEADING` / `_BODY` / `_TABLE` / `_FURNITURE` | `3.0 / 2.0 / 1.0 / 1.2 / 0.25` | Lexical zone weights. |
| `EXTRACT_ACCEPT_CONFIDENCE` | `0.60` | Below this, a value goes to review even when it was found. |
| `LABEL_WINDOW_X` / `LABEL_WINDOW_Y` | `0.55 / 0.06` | How far right-of / below a label to look, as a fraction of the page. |
| `FUZZY_LABEL_MIN_SCORE` | `88` | rapidfuzz floor for a label match. |
| `BERT_ENABLED` / `BERT_MODEL_DIR` | `false` / `/models/…` | The optional local classification tier. |
| `T2_ENABLED` / `T3_ENABLED` / `T4_ENABLED` | `false` | **The paid tiers. Each one is egress.** |
| `AZURE_DI_ENDPOINT` / `AZURE_DI_KEY` / `AZURE_DI_API_VERSION` | *(empty)* / *(empty)* / `2024-11-30` | T2 + T3. |
| `T3_MAX_QUERY_FIELDS` | `20` | Azure's per-request cap; lower it to cap spend. |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | *(empty)* | T4. |
| `LLM_MAX_WINDOW_CHARS` / `LLM_TIMEOUT_SECONDS` | `6000` / `20` | How much text T4 may send, and how long it may take. |
| `REVIEW_QUEUE_BACKEND` / `REVIEW_QUEUE_PATH` | `memory` / `./data/review_queue.json` | T5 storage. |

### Metrics worth an alert

```promql
# abstention rate — the health of the whole cascade
sum(rate(dce_classifications_total{outcome="abstained"}[30m]))
  / sum(rate(dce_classifications_total[30m]))

# anything above zero here is a finding, not noise
sum(rate(dce_preclassification_egress_blocked_total[1h]))

# spend, per tier — and what it bought
sum by (tier) (rate(dce_extraction_tier_cost_calls_total[1d]))
sum by (tier) (rate(dce_extraction_tier_fields_filled_total[1d]))

# the review SLA: depth alone cannot tell 40 fresh items from 40 week-old ones
dce_needs_review_queue_depth
histogram_quantile(0.9, sum by (le) (rate(dce_review_time_to_decision_seconds_bucket[1d])))

# fields the extractor found but a checksum rejected
sum by (doctype, field) (rate(dce_extraction_validator_failures_total[1h]))
```

Also exposed: `dce_classification_tier_seconds{tier}`, `dce_classification_confidence`,
`dce_classification_margin`, `dce_classifications_by_doctype_total`,
`dce_extraction_fill_rate{doctype}`, `dce_extraction_tier_invocations_total{tier,outcome}`,
`dce_extraction_tier_seconds{tier}`, `dce_review_enqueued_total{reason}`,
`dce_review_decisions_total{decision}`.

An abstention rate of **zero** deserves the same suspicion as a high one: a classifier that
never abstains has stopped being able to say "I don't know", which is the one thing this
service must always be able to say.

---

## Known limits

* **The registry is unvalidated against real specimens** — see the note above. This is the
  biggest one, and it is a data problem, not a code problem.
* **Calibration is identity.** The Platt calibration on L2 is a no-op until somebody fits it on
  a labelled corpus, so probabilities are ordered correctly but not calibrated, and the accept
  threshold is doing more work than it should.
* **T2 covers 14 of 121 doctypes.** Everything else stays on T1/T3/T4 by design; the map only
  grows after somebody measures a specialist on real documents of that type.
* **Per-page classification of merged PDFs is not exposed.** `Classification.page_types` exists
  in the contract and the cascade can produce segments; the routes classify whole documents.
* **The review queue is single-process unless you back it.** `memory` loses everything on
  restart, `file` does not lock across replicas. Anything real implements `ReviewQueue`.
* **`cost_bearing` is an upper bound.** It means a billable call was attempted. A tier that
  returned early without dialling — no window to send, no specialist for the doctype — is
  reported as `skipped` where that is detectable, but the counter should be read as "what we
  might have been billed for", not a reconciliation against the invoice.

---

See [`docs/DESIGN.md`](docs/DESIGN.md) for the reasoning behind the cascade, the fusion weights,
the tier boundaries, and what was deliberately left out.
