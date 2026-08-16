# DCE architecture: how a document becomes an answer

Written to be read by somebody debugging a real document at 2am, not by somebody evaluating
the design. Every claim carries a `file:line` so you can check it rather than trust it.

**Companion documents**
- [`RUNBOOK.md`](RUNBOOK.md) — what to do when a specific document behaves wrongly
- [`specs/2026-08-16-segmentation-design.md`](specs/2026-08-16-segmentation-design.md) — why segmentation works the way it does
- [`specs/2026-08-15-ocr-first-ingest-design.md`](specs/2026-08-15-ocr-first-ingest-design.md) — why the text layer is judged per page

---

## 1. The pipeline, end to end

```
bytes
  │
  ├─ detect ─────────── what format is this?              dce/ingest/detect.py
  │
  ├─ read ───────────── turn it into text + layout        dce/ingest/pipeline.py
  │   ├─ PDF          text layer per page, or recognise   dce/ingest/pdf.py
  │   ├─ image        always recognise                    dce/ingest/images.py
  │   ├─ ooxml/eml    parse; recognise embedded scans     dce/ingest/ooxml.py
  │   └─ OCR service  hand the file to Azure              dce/ingest/ocr_service.py
  │
  ▼
LayoutView ─────────── the ONLY thing anything downstream sees     dce/models.py:105
  │
  ├─ segment ────────── how many documents is this?       dce/classify/segments.py
  │
  ├─ classify ───────── what is each one?                 dce/classify/cascade.py:584
  │
  └─ extract ────────── what fields does it hold?         dce/extract/resolve.py
```

**The `LayoutView` is the boundary.** Nothing downstream of ingestion ever sees the original
bytes — `dce/models.py:105-110` states this as a design commitment. If a document classifies
wrongly, the question is always *first* whether the view is right, and only then whether the
classifier is.

---

## 2. Reading: where "does this have usable text" is decided

One rule, applied per page, at [`pdf.py`](../dce/ingest/pdf.py):

| Signal | Constant | Meaning |
|---|---|---|
| character floor | `MIN_ALNUM_CHARS = 40` | fewer alphanumerics than this and the page is a scanning artefact |
| image dominance | `MAX_IMAGE_FRACTION = 0.6` | largest **single** image covering more than this makes the page a picture |
| repeated glyph | `MAX_REPEAT_RATIO = 0.9` | `lllllll` is a previous OCR's failure, not text |
| control glyph | `MAX_CONTROL_RATIO = 0.5` | ditto |

**Per page, and that is load-bearing.** This floor was once summed across the whole document,
which made the effective bar 40/N — two pages of "Scanned with CamScanner" cleared it, and a
200-page scan cleared it on 0.2 characters a page. Worse, clearing it took the text-layer
branch for *every* page, so a typed cover page in front of nine photographed ones was
classified on the cover page alone, with `truncated=False` and no reason given.

Three measurement traps the implementation handles, each found on a real corpus page:

- **Image bboxes can exceed the page.** `us_mortgage_statement.pdf` p1 yields a fraction of
  1.165 uncorrected. Intersect with `page.rect` first.
- **Use the largest single image, not a sum or union.** `us_utility_bill.pdf` p2 has 3985
  placements; the sum is meaningless, the max is 0.088.
- **`page.rect` is the CropBox**, which is also what MuPDF clips text to — so both halves of
  the comparison are measured against the same rectangle. Text outside it is unobservable.

**Do not use `get_texttrace()`.** It is the obvious way to detect invisible (render mode 3)
text. On PyMuPDF 1.28.0 it over-decrements `Py_None` once per span per call and aborts the
interpreter after ~130 pages with an **uncatchable SIGABRT** — no traceback, kills in-flight
requests. The pure-Python fallback raises `TypeError` on exactly the mode-3 pages of interest.
If you ever need the signal, use `page.read_contents()` with a `Tr` regex (196 µs/page). It
should not drive routing anyway: mode 3 is neither necessary nor sufficient for "scan with an
OCR layer".

### Outcomes

| Situation | Result |
|---|---|
| every page adequate | `status=ok`, `text_source=native` |
| every page a scan, recogniser available | recognised; `text_source=local_ocr` or `ocr_service` |
| every page a scan, none available | **422 `needs_ocr`** — a routable answer, not an error |
| some pages adequate, some pictures, recogniser available | mixed: text pages keep their characters, picture pages recognised; `text_source=mixed` |
| some pages pictures, none available | `status=ok` **plus** `truncated=True`, `limits_hit=["unread_pages"]`, and `reason` naming the pages |

That last row is the one that matters most for a KYC corpus. The loss is **declared**. Before
this existed, the document came back `status=ok`, `truncated=False`, `limits_hit=[]` and short
by most of its content.

### The policy

`DCE_INGEST_TEXT_LAYER_POLICY` = `trust` | `verify` (default) | `always_ocr`
([`settings.py`](../dce/ingest/settings.py)).

- `trust` — character floor only; no glyph or image checks
- `verify` — the full predicate above
- `always_ocr` — never read a text layer; every document goes to the recogniser. **Refuses to
  boot without a recogniser configured**, because otherwise every PDF would return `needs_ocr`.

`always_ocr` costs a recognition on every document including born-digital ones, which is why
it is a declared posture and not the default.

---

## 3. Segmentation: how many documents is this?

[`dce/classify/segments.py`](../dce/classify/segments.py).

**Boundaries first, classification second, and the order is the whole design.** The obvious
alternative — classify every page, group runs of equal doctype — was built and measured:
against the real registry on 78 single-document corpus files it emitted **791 segments**, 86%
of documents split, and precision fell from **100% to 71.3%**. A real "How to read your
statement" back page classifies as `ca_drivers_license` at 0.625.

That is not a bug to fix. The cascade's accept gates were calibrated against *whole-document*
evidence; one page carries a fraction of it. Page-scope classification is a different
instrument that was never calibrated.

So:

```
pages → structural signals → candidate spans → classify(span WHOLE) → absorb → merge → segments
```

### The three signals

| Signal | Fires when | Source |
|---|---|---|
| `adequacy` | `text_adequate` flips, or `image_fraction` crosses 0.6 | measured at ingest, carried on `PageInfo` |
| `geometry` | width or height differs by more than `GEOMETRY_TOLERANCE` (2%) | `PageInfo` |
| `first_page_anchor` | a decisive anchor whose `controls` is `form_number`, `control_number` or `mrz_prefix` appears on the later page and **not** the earlier one | the registry |

The `and not on the earlier page` half matters: a marker on both pages is a running header,
and treating it as a boundary splits a document at every page of itself.

Deliberately **excluded** from `FIRST_PAGE_CONTROLS`: `issuer_name` (repeats in headers),
`statute_title` and `issuer_template` (recur in body prose), and `class_name_uncontested`
(documented in the registry as the tier of *no evidence yet*). Using any of them manufactures
boundaries out of page furniture.

### The two rules that make it conservative

1. **An unidentifiable span is absorbed, not emitted.** A span that classifies `unknown` is
   evidence we cannot tell, not evidence of a separate document. Absorbed into its neighbour,
   backwards by preference. This took false splits from **19.3% to 6.0%**.
2. **Any span whose range grows is re-classified.** Keeping the head's verdict over a wider
   range reports a classification drawn from a *subset* of the pages it claims — it turned a
   correctly identified 47-page circular into `us_bylaws`, which is what its first page says
   read alone.

### The asymmetry that sets every default

A **missed** split leaves one answer that is merely incomplete — recoverable, a human sees the
bank statement and notices the passport. A **false** split turns one correct classification
into two thinner-evidence ones and then runs extraction against each. That is how 100%
precision became 71.3%.

**So: split only on positive evidence.** An abstaining page is not a boundary. A blank page is
not a boundary. An unmeasured payload proposes none.

### Current measured behaviour

| | |
|---|---|
| false splits on single documents | **3 / 83 (3.6%)** |
| documents that stayed whole and changed their answer | **0 / 80** |
| boundary **precision** on synthetic bundles | **24 / 24 (100%)** — no false splits |
| boundary **recall** on synthetic bundles | **24 / 68 (35.3%)** |
| bundles returned as one segment (total miss) | **18 / 36 (50%)** |

Reproduce with `tools/bundle_recall.py`.

### Recall is the weak half, and it is weak in a knowable way

Precision is excellent — on 36 synthetic bundles it proposed 24 boundaries and **every one was
real**. Recall is 35%, and the by-shape breakdown says exactly where it fails:

| bundle shape | recall |
|---|---|
| same size / all text / no anchors | **0%** (0 of 17) |
| mixed size / all text / no anchors | 36% |
| same size / all text / some anchors | 38% |
| same size / text + scan | 50% |
| mixed size / text + scan | 46% |
| mixed size / all text / **some anchors** | **75%** |

**This is not a tuning problem.** Two documents of the same page size, both text-bearing,
neither carrying a first-page marker, are structurally indistinguishable — there is no signal
to find, and no threshold change reveals one. 47% of true boundaries had **no signal fire at
all**.

The honest consequence: segmentation currently finds a bundle when the documents *differ*
physically (different paper, one scanned) or when one carries a form/control number. It does
not find a seam between two same-size typed documents. **Raising recall means a new signal**
— per-page classification is the obvious one and is exactly what was measured and rejected
(71.3% precision) — not loosening what is here.

### The three that still split, and why they are left alone

All three are long regulatory filings split by **geometry**, and each was diagnosed against
the real pages:

- `ca_business_acquisition_report.pdf` (19p) — page 9 is landscape because *"Statement of
  Changes in Shareholders' Deficiency"* is a wide table. Pages 8–19 are one continuous set of
  financial statements, internally numbered, with the company name as a running header.
- `mx_reporte_anual_cnbv.pdf` (141p) — pages 118–120 are 1224pt wide, **exactly 2× the 612pt
  page**: fold-out spreads. Pages 117–141 carry one continuous run of note numbers.
- `us_sec_form5.pdf` (13p) — same shape.

**Four candidate fixes were built and measured. All were rejected**, and the numbers are why:

| Candidate | False splits | Cost |
|---|---|---|
| (e) treat a landscape/portrait flip as furniture | — | **breaks the shipped bundle test outright** — `us_w9`→`us_bank_statement` is itself an exact w/h swap |
| (i) suppress a geometry boundary that returns to earlier stock | 3.7% | destroys `bill / landscape statement / bill`, an ordinary KYC upload: the outer two sharing stock makes the middle read as an insert |
| (k) inside a long document, only a first-page anchor may split | **0.0%** | near-total recall collapse |

A 0.0% false-split rate is available and is not worth having. Each of these buys precision on
long filings by destroying the ability to detect the bundles the feature exists for. Given
that recall is already the weak half at 35%, spending more of it is the wrong direction.

*The recall costs in this table were measured by a one-off script during diagnosis, not by
`tools/bundle_recall.py`, and their absolute values disagreed with the harness. The ordering
and the direction held under both; treat the specific percentages as indicative and re-measure
with the committed harness before acting on any of them.*

**The corpus contains no real KYC bundles**, so every number here is measured on synthetic
joins and long filings — the least favourable material available.

---

## 4. Classification

[`cascade.py:584`](../dce/classify/cascade.py). The accept rule is **evidence in bits** with
four gates:

1. **identification** — concurrence between tiers, or a conclusive L1
2. **separation** — `1 - 2^-(B[1]-B[2]) >= classify_min_margin`
3. **support**
4. **coverage**

`LEXICAL_PRIMARY` (`dce/config.py:170`, default `False`) drops the anchor tier's veto while
keeping corroboration. Measured: on 98/1/18 versus off 97/0/20 — it buys one extra correct
answer at the cost of the first wrong one, so it ships available and disabled.

**Zones.** A PDF text layer carries fonts and positions, not roles, so every text-layer block
is `Zone.body` by deliberate refusal to infer ([`pdf.py:10-20`](../dce/ingest/pdf.py) calls
inference "manufactured evidence"). Local OCR is also all body. `azure_read` is all body.
**Only `azure_layout` supplies real zones.**

Consequence: 4 decisive anchors are title-gated, and three doctypes — `us_drivers_license`,
`ca_drivers_license`, `ca_nexus` — have *every* decisive anchor title-gated. **Those three
cannot be decisively identified without `azure_layout`.** All three are photo IDs.

---

## 5. Extraction

Unit of work is `(LayoutView, DocTypeSpec)` — no page parameter at any level. Because every
`LayoutView` element is page-numbered and `LocatorContext` carries no page scope, **a
page-range slice can be handed to `extract()` unmodified**. That is what makes per-segment
extraction possible without touching the extractor.

Tiers, in escalation order — each only ever sees what the previous left empty:

| Tier | What | Bills |
|---|---|---|
| `t1_local` | deterministic locators, in-process | no |
| `t2_azure_prebuilt` | Azure prebuilt models | **yes**, per page |
| `t3_azure_query` | Azure `queryFields` | **yes**, per field |
| `t4_llm` | LLM | **yes** |
| `t5_review` | human queue | no |

All paid tiers are **off by default**. `/extract` and `/process/segments` deliberately never
run them: `/extract` is what an integrator calls in a loop while tuning locators, and a bundle
multiplies paid calls by its segment count.

**T3's poll deadline is hardcoded at 120 s** (`dce/extract/azure_specialist.py:120`,
`_POLL_DEADLINE_SECONDS`). No env var raises it. A client-side timeout does **not** cancel the
Azure job, so the pages may still be billed.

---

## 6. The API

| Path | Purpose |
|---|---|
| `POST /api/v1/classify` | one document |
| `POST /api/v1/classify/segments` | may hold several; splits first |
| `POST /api/v1/classify/compare` | every avenue, adjudicating nothing |
| `POST /api/v1/extract` | one document, caller may pin `doctype_id` |
| `POST /api/v1/process` | classify + extract + tiers, one document |
| `POST /api/v1/process/segments` | segment, then classify and extract each |
| `GET /readyz` | posture: registry, OCR providers, trust boundary, text-layer policy |

**There is no `/analyze` API route** — it is a console SPA path. The console's Analyze page
calls `/classify/segments` and `/process/segments`.

**No version header, no `Accept` negotiation.** `X-API-Key` is the only request header read;
the `/api/v1` prefix is the sole versioning lever. A new response shape needs a **new path** —
which is why segmentation lives on `/segments` rather than a flag.

`segments[]` always holds at least one entry. A single document comes back as one segment
covering every page, classified exactly as `/classify` would have. **Callers never branch on
whether the upload was a bundle.**

---

## 7. Configuration that changes behaviour

Two prefixes, and confusing them is the most common misconfiguration:

| Variable | Configures |
|---|---|
| `AZURE_DI_ENDPOINT` (no prefix) | post-classification **extraction** tiers T2/T3 |
| `DCE_INGEST_AZURE_DI_ENDPOINT` | **ingest OCR** — reading an image before its type is known |

They are separate on purpose: reading a document before its type is known and sending it to a
paid vendor tier after are two different authorisations, and one variable granting both would
let enabling T2 quietly enable pre-classification egress.

### Ingest OCR (all `DCE_INGEST_`-prefixed)

```
OCR_SERVICE_ENABLED=true
OCR_SERVICE_PROVIDER=azure_layout          # prefer over azure_read: Read returns no zones
OCR_SERVICE_TRUST_BOUNDARY=on_premises     # declared, never inferred; defaults to external
AZURE_DI_ENDPOINT=http://host:5000
AZURE_DI_KEY=...
AZURE_READ_ENDPOINT=http://host:5000
AZURE_READ_KEY=...
LOCAL_OCR_ENABLED=true                     # optional
OCR_DEFAULT_PROVIDER=azure_layout          # MANDATORY if local and service are both on
TEXT_LAYER_POLICY=verify                   # trust | verify | always_ocr
MAX_OCR_PAGES=10                           # local-engine only; the service reads whole PDFs
```

**Boot refusals** — these fail loudly rather than guessing, by design:
- both local and service enabled with no `OCR_DEFAULT_PROVIDER`
- `TRUST_BOUNDARY` not exactly `external` or `on_premises` (`on-premises` with a hyphen fails)
- `OCR_SERVICE_PROVIDER` not a service provider (`azure_di` is wrong; it is `azure_layout`)
- `TEXT_LAYER_POLICY=always_ocr` with no recogniser configured

**Quoting.** `DCE_INGEST_OCR_SERVICE_ENABLED="true"` in a `--env-file` arrives as the literal
`"true"` with quotes — podman and docker env-files do not strip them. Never quote in an
env-file.

---

## 8. Invariants that must not break

Each is enforced by a test. If you are changing this codebase, these are what you are not
allowed to break.

| Invariant | Enforced by |
|---|---|
| No socket opens during native-format ingestion | `tests/test_ingest_egress.py:133` sabotages `socket.socket`/`getaddrinfo` |
| No HTTP client imported at module scope under `dce/ingest/` | `tests/test_azure_ocr_providers.py` greps every file |
| A request may **decline** recognition and may never **grant** it | `tests/test_ingest_egress.py:175` |
| `needs_ocr` is a routable answer, not an error | `dce/ingest/result.py:1-18` |
| An empty document is refused, never classified as `unknown` | `pipeline.py` `UnsupportedFormat` |
| A zero-recogniser deployment still classifies text documents | `Dockerfile` installs `.[pdf]` only; no `httpx` in base deps |
| Every OCR-service call is gated | `assert_ocr_egress_permitted` on submit **and every poll** |
| `allow_preclassification_egress` stays `False` and governs only the paid tiers | `dce/config.py:36-49` |

---

## 9. Measurement tools

| Tool | Answers |
|---|---|
| `tools/corpus_test.py` | correct / wrong / abstained / precision over the corpus |
| `tools/corpus_test.py --ingest` | the same **through the service's own parser** — without this the harness reads PDFs itself and never exercises `dce/ingest/pdf.py` |
| `tools/channel_probe.py` | anchor-channel vs lexical-channel leaders per document |

**The `--ingest` distinction has bitten before.** A baseline captured without it measured
nothing about an ingest change, because the harness's own PyMuPDF path handled every PDF whose
whole-document character count cleared its floor.

**The container mounts no source.** A host-side edit does not reach a running service; rebuild
the image or the before/after runs are identical for the wrong reason.
