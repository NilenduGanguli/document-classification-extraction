# Segmentation: one file, several documents

**Status:** proposed, for review. Nothing implemented.
**Date:** 2026-08-16
**Prerequisite that already landed:** per-page text adequacy (`209f774`)

---

## 0. The finding that decides the design

`classify_pages()` exists, is exported, and is called by no route. The obvious plan is to
wire it to an endpoint. **It must not be wired as-is.**

Measured against the **real 129-doctype registry** on all 78 multi-page corpus documents
(2,273 pages). Every one of those files is a *single* document, so the ideal output is 78
segments — one each:

| | |
|---|---|
| segments emitted | **791** |
| documents split into more than one segment | **67 / 78 (86%)** |
| documents given a segment naming a doctype that is not theirs | **45 / 78 (58%)** |
| pages classified correctly | 28.4% |
| pages abstained | 60.1% |
| pages **confidently wrong** | **11.4% (260 pages)** |

And the number that matters most, on the same 78 files:

| | precision when answered |
|---|---|
| whole-document `classify()` | **100.0%** (69 correct / 9 abstained / 0 wrong) |
| page-scope `classify_pages()` | **71.3%** |

**Root cause: the classifier was never calibrated for page-scope input.** Its accept gates —
the absolute per-class floor, the pairwise separation margin, coverage — were reasoned about
against whole-document evidence. A single page of a bank statement carries a fraction of that
evidence, and the gates were never re-derived for it.

The failure is not subtle. A real "How to read your statement" back page from
`us_bank_statement.pdf` classifies as **`ca_drivers_license` at 0.625 confidence**. A 6-page
statement with one anchorless middle page becomes three segments: statement, *something
else*, statement.

Shipping this would trade the property the entire system is built on — 100% precision, abstain
rather than guess — for a feature. That is the wrong trade, and it is why this document
proposes a different architecture rather than an endpoint over the existing function.

---

## 1. Corrections to the brief

Three premises in the request do not match the code.

**There is no `/analyze` API endpoint.** `/analyze` is a *console* SPA route, served
`index.html` by the 404 fallback (`dce/api/app.py:296-315`). The Analyze page reaches the
service through `/api/v1/classify`, `/api/v1/extract` and `/api/v1/process`. So "analyze
should auto-segment" is a statement about **the console and `/process`**, which is
straightforward — it does not require inventing an endpoint that callers already depend on.

**`/extract` already is the per-segment extractor you described.** It accepts a
caller-declared `doctype_id` and skips classification entirely (`routes.py:1170`,
`:2813-2816`). Extraction's unit of work is `(LayoutView, DocTypeSpec)` with no page
parameter at any level, and because every `LayoutView` element is page-numbered and
`LocatorContext` carries no page scope, **a page-range-sliced view can be handed to
`extract()` unmodified.** The missing piece is a public range slicer, not an extraction
change.

**No response shape can change in place.** There is no version header, no `Accept`
negotiation, no vendor media type, no feature flag on any request model. `X-API-Key` is the
only request header read. The `/api/v1` prefix is the sole versioning lever. A segmented
response therefore needs **a new path**, an opt-in request field, or a new version prefix —
it cannot coexist with the current shape on `/classify`.

---

## 2. The architecture: boundaries first, classification second

The current design is *classify every page, then group runs of equal doctype*. That asks the
classifier the question it is worst at, then builds structure out of its errors.

**Invert it.** Detect candidate boundaries with cheap structural signals that do not involve
the classifier at all; then classify **each candidate span as a whole document**, which is
the scope the classifier is calibrated for and the only scope where its 100% precision
property has ever been measured.

```
pages ──> boundary signals ──> candidate spans ──> classify(span) ──> segments
          (structural, cheap)                      (document-scope, calibrated)
```

Consequences that follow directly:

- **Precision is preserved by construction.** Every classification is a document-scope
  classification. There is no page-scope accept gate to re-derive.
- **A file with no boundary evidence yields one span**, which is exactly today's behaviour
  and today's numbers. Segmentation can only ever *add* structure where evidence supports it.
- **Cost falls out.** Classification runs once per *span*, not once per page. A 12-page
  bundle of three documents costs three classifications, not twelve.

### 2.1 The asymmetry that sets the default

A false split and a missed split are not equally bad here:

- **A missed split** classifies a bundle as its dominant document. One answer, possibly
  incomplete. Recoverable — a human sees a bank statement and notices the passport.
- **A false split** turns one correct classification into two thinner-evidence ones, each
  scored against a fraction of the document, and then runs *extraction* against each. That is
  how 100% precision becomes 71.3%.

So: **split only on positive evidence, never on the absence of it.** An abstaining page is
not a boundary. A blank page is not a boundary. Silence continues the current span.

### 2.2 The signals, and what is actually available

Measured availability, not assumed:

| Signal | Status | Note |
|---|---|---|
| **Per-page text adequacy** (`alnum`, `adequate`, `image_fraction`) | **computed and then discarded** | `PdfOutcome.page_verdicts` — built last week, never carried into the view |
| Page geometry (width/height) | present, per page | a passport scan and an A4 bill differ |
| First-page-only anchors (MRZ, form number, control number) | present, typed by `Controls` | but `AnchorHit` carries `zone`, not `page` — needs a slice first |
| `PageInfo.angle` (skew) | **not obtainable** locally | `LayoutBuilder.page()` has no `angle` parameter; 0.0 on every non-Azure path |
| `Mark` / `KeyValue` per page | **not obtainable** locally | only `dce/adapters.py` constructs them; local ingest produces none |
| Rasterised page similarity | **not obtainable** | the view carries no image bytes, by design |

**The highest-value item is the first, and it is already built.** `PageVerdict` holds `page`,
`alnum`, `adequate`, `reason` and `image_fraction` per page, and `parse_pdf` throws it away.
Carrying it onto `PageInfo` is *plumbing, not new logic* — and it delivers, in one change,
both a native-vs-scanned transition signal and blank-page detection.

It must go on **`PageInfo`**, not `view.raw`: `_page_view` rebuilds the view without `raw`,
so anything stashed there is invisible to per-page work.

### 2.3 Proposed boundary rule

A boundary is proposed between page *n* and *n+1* when **at least one** holds:

1. **Adequacy transition** — the pages differ in `adequate`, or `image_fraction` crosses the
   dominance threshold. A scanned ID after a typed cover page.
2. **Geometry change** — page dimensions differ by more than a tolerance.
3. **First-page anchor** — page *n+1* fires a decisive anchor whose `controls` is
   `mrz_prefix`, `form_number` or `control_number`. These appear on a document's first page
   and essentially nowhere else, making them the strongest available evidence.

And then, critically: **each candidate span is classified whole. If two adjacent spans
classify to the same doctype, they are merged** — the boundary was a false positive and
document-scope classification just said so.

---

## 3. Endpoints

New paths, because no response shape can change in place (§1).

| Path | Behaviour | Status |
|---|---|---|
| `POST /api/v1/classify` | one document, current contract | **unchanged** |
| `POST /api/v1/classify/segments` | segment, then classify each span | **new** |
| `POST /api/v1/process` | one document, current contract | **unchanged** |
| `POST /api/v1/process/segments` | segment, classify and extract per span | **new** |
| `POST /api/v1/extract` | one document, caller-declared doctype | **unchanged** — already the per-segment shape |

Every existing caller keeps working untouched. The console's Analyze page calls the
`/segments` variants by default, which is what "analyze should auto-segment" means in
practice. A caller who knows they have one document uses the existing paths and pays nothing
for segmentation.

**Response shape** — uniform, one segment for a single document:

```jsonc
{
  "segments": [
    { "start_page": 1, "end_page": 2, "classification": { … }, "extraction": { … } },
    { "start_page": 3, "end_page": 6, "classification": { … }, "extraction": { … } }
  ],
  "segmented": true,          // false when no boundary evidence was found
  "boundary_evidence": [ … ]  // why each split was made, for the audit trail
}
```

---

## 4. Defects that must be fixed first

Each found by execution, each a correctness bug in the machinery a design would build on.

1. **`_page_numbers()` ignores marks and key-values** (`cascade.py:1859`). A page whose only
   content is a checkbox and a key/value pair — a scanned form page — is never enumerated,
   never classified, and **vanishes from every segment with no trace a caller can detect**.
2. **Run-length aggregates over list index, not page number** (`cascade.py:795`). A gap in
   the numbering is bridged into one continuous segment rather than broken at the gap.
3. **`_page_view` drops `view.raw`** (`cascade.py:1867`), which is where OCR provider
   provenance lives (`routes.py:1994`). Per-segment responses would lose attribution.
4. **`_page_view` does not renumber and shares `TextBlock` objects with the parent** —
   mutating a slice mutates the bundle. Latent aliasing hazard.
5. **`Segment.classification` is page 1's evidence presented as the segment's**, next to a
   confidence averaged over every page. A reviewer reading a 6-page segment's audit trail is
   reading page 1's evidence, margin and coverage.
6. **The one existing test uses a 7-doctype fixture registry** (`test_classify.py:832`). The
   same payload against the real 129-doctype registry produces a different answer, so the
   test does not measure the shipped system.

---

## 5. Measurement, and the honest gap

**The corpus contains zero real KYC bundles.** All 91 PDFs are single documents. Every
inter-document number the scout produced is a synthetic join of two real documents — fair,
since that is structurally what a concatenated bundle is, but not the real thing.

The corpus is also skewed toward long filings (13.6 pages average; 183-, 164- and 154-page
documents) where a real KYC bundle is 5–15 pages of 1–3 page documents. Long documents
inflate the intra-document join count and therefore **flatter** the false-positive rate.

So the same conclusion as the last change applies: **the corpus is the wrong instrument until
it contains bundles.** Three or four real concatenated KYC files would make this measurable.
That is the single highest-value thing to add before building.

**A note on the Azure path.** Every measurement above rides the local PDF text-layer path. On
`azure_layout` the inputs change materially — zones exist, so title-gated anchors fire that
cannot fire locally; `angle` is real; `unit` is inches; marks and key-values are populated.
Page-scope precision may be materially better there. It has not been measured.

---

## 6. Phases

| # | Work |
|---|---|
| **0** | Real KYC bundle fixtures + corpus documents; a synthetic-join harness |
| **1** | Fix the six defects in §4 |
| **2** | Carry `PageVerdict` onto `PageInfo`; public page-range slicer preserving `raw` |
| **3** | Boundary detector on structural signals, no classifier involvement |
| **4** | Span classification + same-doctype merge; `Segment` reporting its own evidence |
| **5** | `/classify/segments` and `/process/segments`; console Analyze uses them |
| **6** | Measure: false-split rate and precision per segment against whole-document baseline |

Phase 1 is worth doing regardless — those are live bugs in exported, tested code.

---

## 7. Open decisions

1. **Ship order.** Phases 1–2 are corrective and safe. Phases 3–5 are the feature. Do you
   want 1–2 landed independently while bundle fixtures are gathered?
2. **What happens when a span abstains?** Report the segment with `unknown` and let review
   handle it, or merge it into a neighbour? I lean report-and-review: merging invents a claim.
3. **Extraction cost on a bundle.** Three documents means three extractions, and T2/T3 bill
   per call. Should `/process/segments` extract every segment, or only segments whose
   classification was confident?
