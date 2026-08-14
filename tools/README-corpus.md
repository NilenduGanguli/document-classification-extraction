# The document corpus and `corpus_test.py`

A corpus of real documents, and a harness that measures the DCE service against it. The
corpus tells us what the classifier actually does on documents we did not write anchors
for; the harness turns that into two files a human and a script can both read.

---

## 1. The privacy rule — read this before you add anything

**Only blank official forms and official specimens go in this corpus. Never a real
person's document.**

Allowed:

| Kind | What it means | Examples |
| --- | --- | --- |
| `blank_form` | An empty form published by the issuing authority itself | IRS Form W-9 from irs.gov, CRA T1 from canada.ca, SAT/RFC forms from sat.gob.mx, SEC forms from sec.gov |
| `specimen` | A sample image the authority published for public education, carrying fabricated names and numbers | The INE credential specimen, a state DMV "sample" driving licence, a Wikimedia Commons image explicitly marked *specimen* |
| `sample` | A vendor sample document published for developers | Azure AI Document Intelligence sample invoices/IDs, AWS Textract samples |

Never, under any circumstance:

- A filled-in identity document belonging to a real person — passport, driving licence,
  SSN card, SIN letter, INE, health card.
- A real bank statement, payslip, utility bill, tax notice or KYC packet, however
  "anonymised" it looks. Redaction boxes are not deletion, and a name in the letterhead is
  still a name.
- Anything from a forum upload, a document-sharing site, an image search result, a scraped
  dataset or a leak — these are where real people's documents live, no matter how the page
  labels them.

**If the only copies of a doctype you can find are real people's documents, skip that
doctype.** Record it as unavailable and move on. A smaller corpus with a clean provenance
is the correct outcome; it is not a failure, and it is not something to work around.

When a document is *about* a real person, do not download it, do not "check it first", do
not keep it locally to decide later. The check is: *could this page have been published by
the issuing authority as a blank form or an educational specimen?* If not, it does not
belong here.

Two operational consequences:

- **Nothing in `corpus/` is committed.** The corpus is local working material.
- **The harness writes no field values by default.** Reports record whether a field
  filled, not what it said. `--show-values` opts in for non-PII fields; anything the
  registry marks `pii` stays masked either way. That is deliberate belt-and-braces: it
  means a single corpus mistake does not also become a checked-in file full of
  identifiers.

---

## 2. Layout

```
corpus/
  us/  manifest.jsonl  us_w9.pdf  us_1040.pdf  …
  ca/  manifest.jsonl  ca_sin_confirmation.pdf  …
  mx/  manifest.jsonl  mx_ine.pdf  …
reports/
  corpus-results.json      # full per-document detail, machine-readable
  corpus-results.md        # the human table
tools/
  corpus_test.py           # the harness
  README-corpus.md         # this file
```

---

## 3. Adding a document

**1. Get the exact `doctype_id` from the registry.** Never invent one — a manifest entry
naming a doctype the service does not have can never score `CORRECT`, and the run will
warn about it.

```bash
curl -s http://localhost:8200/api/v1/doctypes | python3 -m json.tool | less
# or just the ids for one country:
curl -s http://localhost:8200/api/v1/doctypes \
  | python3 -c "import json,sys; [print(d['doctype_id']) for d in json.load(sys.stdin)['doctypes'] if d['country']=='CA']"
```

(IDs are not always what you would guess — the Canadian SIN letter is
`ca_sin_confirmation`, not `ca_sin`.)

**2. Save the file as `corpus/<cc>/<doctype_id>.pdf`.** Prefer PDF: official blank forms
are usually digital PDFs with a real text layer, which is exactly what the harness wants.
If a doctype legitimately needs more than one example, suffix it —
`us_w9__2018rev.pdf` — and keep the `expected_doctype` correct.

**3. Verify the download is real.**

```bash
head -c 4 corpus/us/us_w9.pdf   # must print %PDF
ls -l   corpus/us/us_w9.pdf     # must be > 5 KB
```

A "PDF" that is actually an HTML error page, a login wall or a truncated download is the
most common corpus defect. **Delete it and record the failure** rather than leaving a
broken file behind — the harness will flag it as `ERROR`, but a deleted file is honest and
a broken one is noise.

**4. Append one line to `corpus/<cc>/manifest.jsonl`.** One JSON object per line, no
trailing commas, no wrapping array:

```json
{"file": "corpus/us/us_w9.pdf", "expected_doctype": "us_w9", "source_url": "https://www.irs.gov/pub/irs-pdf/fw9.pdf", "kind": "blank_form", "notes": ""}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `file` | yes | Path to the PDF, repo-root-relative (`corpus/us/us_w9.pdf`). Absolute paths and paths relative to the manifest also resolve. |
| `expected_doctype` | yes | Exact `doctype_id` from `GET /api/v1/doctypes`. |
| `source_url` | yes in practice | Where it came from. This is the provenance record that makes the privacy rule auditable — an entry with no URL cannot be checked by anyone else. |
| `kind` | yes | `blank_form`, `specimen` or `sample`. |
| `notes` | no | Anything a reader of the report needs: revision year, language, "instructions pages included", "specimen watermark present". |

Blank lines and `#` comment lines are ignored. A malformed line is reported with its line
number and skipped — it does not cost the rest of the country its run.

---

## 4. Running the harness

Once, to install the only third-party dependency (the service itself deliberately has no
HTTP client and no PDF reader; the harness uses `urllib` from the standard library and
PyMuPDF for text):

```bash
uv pip install pymupdf        # or: pip install pymupdf
```

Then, with the service running on `http://localhost:8200`:

```bash
python tools/corpus_test.py                       # everything: classify + extract
python tools/corpus_test.py --verbose             # one line per document as it goes
python tools/corpus_test.py --country ca          # one country
python tools/corpus_test.py --only us_w9,us_1040  # named doctypes
python tools/corpus_test.py --classify-only       # POST /api/v1/classify, no extraction
python tools/corpus_test.py --layout              # send page geometry, not flat text
python tools/corpus_test.py --ocr                 # also measure scans and photo IDs
```

| Flag | Default | What it does |
| --- | --- | --- |
| `--corpus-root` | `corpus/` | Where the `<cc>/manifest.jsonl` files live. |
| `--out-dir` | `reports/` | Where the two report files are written. |
| `--url` | `http://localhost:8200` (or `$DCE_URL`) | Service base URL. |
| `--api-key` | `$DCE_API_KEY` | Sent as `X-API-Key` when the service requires one. |
| `--only` | all | Comma-separated `doctype_id`s. |
| `--country` | all | Comma-separated country codes, e.g. `us,in`. |
| `--classify-only` | off | Hit `/api/v1/classify`; no extraction, no fill-rate. |
| `--layout` | off | Send a `LayoutView` payload (per-page blocks with real bboxes) instead of flat text. Usually raises fill-rate, because page-scoped locators have geometry to work with. |
| `--show-values` | off | Include non-PII extracted values in the reports. PII fields stay masked. |
| `--max-pages` | all | Read only the first N pages of each PDF. Useful when a form ships with 20 pages of instructions. |
| `--timeout` | 120 s | Per-request timeout. |
| `--verbose` | off | Per-document line with confidence, margin, coverage, runners-up and the abstention reason. |

**Exit status is always 0.** This measures; it does not gate. A CI job that wants a
threshold should read `reports/corpus-results.json` and decide for itself.

---

## 4a. `--ocr` — measuring the scans and photo IDs

Most official forms are digital PDFs. Identity documents are not: passports, PR cards,
green cards and driving licences reach a KYC system as photographs and scans, which is why
they are in the corpus as `.pdf` scans and `.jpg` images. Without `--ocr` they have no text layer to
read, so they are recorded `NEEDS_OCR` (or `ERROR`, for an image) and **excluded from every
rate** — which quietly leaves the least-tested path in the service unmeasured.

`--ocr` measures them. The harness rasterises each page with PyMuPDF, sends the PNG to an
**Azure Computer Vision Read v3.2** endpoint, and classifies the recognised text through
exactly the same payload builder and scorer as a text-layer document. `.jpg`, `.png`,
`.tif`, `.bmp` and `.gif` are accepted as single-page documents; magic bytes decide the
type, not the filename.

```bash
# against the repo's local mock (nothing to configure, no cost)
python tools/corpus_test.py --ocr

# against a real Azure Cognitive Services resource
AZURE_VISION_ENDPOINT=https://<resource>.cognitiveservices.azure.com \
AZURE_VISION_KEY=<key> python tools/corpus_test.py --ocr
```

| Flag | Default | What it does |
| --- | --- | --- |
| `--ocr` | **off** | Turn the OCR path on. Off, nothing below has any effect. |
| `--ocr-endpoint` | `http://localhost:5006` (or `$AZURE_VISION_ENDPOINT`) | Azure Read v3.2 base URL. The DES stack's `azure-ocr-mock` listens here. |
| `--ocr-key` | `$AZURE_VISION_KEY` | Sent as `Ocp-Apim-Subscription-Key`. Omit for the mock; required by real Azure. |
| `--ocr-dpi` | `200` | Raster resolution. DES uses 144 for its viewer; 200 reads the 6-point statutory footnotes this corpus is full of. |
| `--ocr-max-pages` | all | OCR at most N pages per document. Bounds cost on a paid endpoint — a 32-page pension order is 32 billable calls. |
| `--ocr-dump-dir` | none | Write the recognised text per document, for debugging OCR quality. **Never point this at a checked-in directory** — see below. |

The two-call contract (`POST .../read/analyze` → `202` + `Operation-Location`, then `GET`
that URL until `succeeded`/`failed`) is copied from `des/ocr/azure_read.py`, not reinvented,
so the same code path serves the mock and a real resource. A page whose OCR fails is
recorded empty and the run continues; the document only fails when every page fails.

### What `--ocr` deliberately does *not* do

- **It does not change a run without it.** Statuses, classifications and every rate are
  identical to the pre-OCR harness. Verified by diffing a default run against the recorded
  baseline: 67 documents, zero status changes, zero classification changes.
- **It does not rescue a failure into a rate.** If OCR errors, or recognises too little
  text to classify on, the document stays `NEEDS_OCR` and stays out of every denominator.
  An OCR failure is not a classifier result and is never scored as one.
- **It does not write recognised text into the reports.** The reports record page counts,
  line counts and character counts — never the text. `--ocr-dump-dir` is the one way to get
  the text, it is opt-in, it writes only where you point it, and it must not be a directory
  under version control: recognised text is the one thing this tool handles that can carry
  a real identifier off a real document.

### Reading OCR results — the confound

**OCR error and classifier error are not separable by this harness.** A `WRONG` on an OCR'd
document may be the classifier's fault or the recognition engine's, and averaging the two
into one number produces a claim nobody can act on. So:

- Every summary is split into `overall`, `text_layer` and `ocr` buckets.
- The per-document table carries a `src` column (`text`/`OCR`), and OCR'd documents get
  their own report section with per-document page/line/char counts and timings.
- **Compare a run against another run's `text_layer` bucket, never against `overall`.**
  `overall` moves the moment `--ocr` is toggled; `text_layer` is the stable series.

Before recording an OCR `WRONG` as a classifier defect, dump the text and read it. If the
engine mangled the title, it is an OCR finding. If the engine read the title correctly and
the classifier still answered something else, it is a classifier finding — and those are
the valuable ones, because they come from the document population production actually sees.

### The local mock: real OCR, but English-only

The DES stack's `azure-ocr-mock` (port 5006) is **not** a canned-response stub. It speaks
the real Azure Read v3.2 contract and performs genuine recognition with Tesseract, so its
output reflects the uploaded image and its results are real measurements of the classifier.

**But its Tesseract carries only the `eng` and `osd` traineddata** (verify with
`docker exec <ocr-mock-container> tesseract --list-langs`). Consequences, stated plainly
because a fake pass is worse than a known gap:

- **Latin-script documents (US, CA, MX) are measured honestly.** Spanish loses diacritics
  and nothing else material; `mx_acta_nacimiento` reaches coverage 0.80 through the mock.
- **A document in a script the mock's `eng`-only Tesseract cannot read comes back as
  transliteration noise.** Every corpus document is Latin-script today, so this does not
  currently bite — but the moment a non-Latin pack is added, its results through the mock
  measure the missing language pack and not the classifier. Do not draw conclusions about
  non-Latin documents from a mock run, and do not file registry defects from one.

The harness prints this warning at start-up and writes it into the report whenever the OCR
endpoint is a loopback address, so the caveat travels with the numbers.

### The egress invariant is untouched

The service's rule is that **no network call may happen before a doctype is accepted**, and
`--ocr` does not bend it. The harness is a client, not part of the service: *the harness*
rasterises, *the harness* calls the OCR endpoint, and only then does text go to DCE over
the normal API. `allow_preclassification_egress` stays `False` and the socket tripwire test
is unaffected.

---

## 5. Reading the report

### Statuses

| Status | Meaning |
| --- | --- |
| `CORRECT` | Returned `doctype_id` equals `expected_doctype`. |
| `WRONG` | It answered, and answered something else. Listed in the confusion table with the top-3 runners-up. |
| `ABSTAINED` | The cascade could not clear its thresholds and refused. **Not** a wrong answer — the service is built to route to a human rather than guess. Listed with the reason it gave. |
| `NEEDS_OCR` | No usable text layer. **Nothing was sent to the service**, and the document is excluded from every rate. Rerun with `--ocr` to measure it. Under `--ocr` this status is narrower: it means OCR itself failed, or recognised too little text to classify on — still excluded, because an OCR failure is not a classifier result. |
| `ERROR` | The document could not be measured at all — file missing, under the size floor, not a document type we can open, password-protected, or the request failed. Also excluded from the rates. Images (`.jpg`/`.png`/…) land here without `--ocr` and are measured with it. |

### The two accuracies

- **accuracy = correct / sent** — over every document that reached the classifier,
  abstentions included. This is the number to quote.
- **precision when it answered = correct / (correct + wrong)** — over the documents it was
  willing to answer on. A high precision with a high abstention rate is the intended
  posture for a KYC system; a high accuracy with confident errors is not.
- **abstention rate** — how often it refused. Read it *with* the two above: driving it to
  zero by lowering thresholds trades human review for confident mistakes.

`NEEDS_OCR` and `ERROR` documents are in neither numerator nor denominator, and are listed
in full so the gap is visible rather than silently absorbed.

### Sections

| Section | What to do with it |
| --- | --- |
| **By text source** | Only when `--ocr` ran. `text_layer` vs `ocr` as separate populations. Regression-check against `text_layer`; treat `ocr` as its own, noisier series. |
| **OCR'd documents** | Only when `--ocr` ran. Every document whose text came from the recognition engine, with pages OCR'd, line and character counts, and timings. Read this before believing any OCR row elsewhere in the report. |
| **Confusions (WRONG)** | `expected -> got` with confidence, margin, coverage and the top-3 runners-up. A pair that recurs (W-9 vs W-8BEN, PAN vs Form 60) is an anchor problem: the loser needs a decisive anchor, or the winner needs a negative anchor. |
| **Abstentions** | The service's own reason string, which names which floor it missed — probability, margin or coverage. "Coverage below floor" on a document that *is* that type means the doctype's vocabulary is too thin for the real form. |
| **Needs OCR** | The list of documents the corpus cannot measure as-is. If it is large, that is the argument for `--ocr` — made with a number rather than a hunch. |
| **Errors** | Broken downloads and bad manifest paths. Fix or delete; do not leave them. |
| **Extraction fill-rate** | Filled fields / fields in the schema, for correct classifications only, plus which *required* fields came back empty. |
| **Fields most often missing** | Aggregated across the corpus. A field missing on every document of a doctype is a locator to fix; a field missing on one is usually that document. |
| **Manifest problems** | Lines that could not be parsed, with line numbers. |
| **Expected doctypes not in the registry** | Manifest entries naming a `doctype_id` the service does not have. These can never score `CORRECT` — fix the manifest. |

### Fill-rate on a blank form

**A low fill-rate on a blank form is expected and is not automatically a bug.** An empty
W-9 has no name to find, because nobody filled it in; what it does have is labels, so a
locator that fires on the label and returns the label's own trailing text is a real
finding, and one worth reading the `--show-values` output for. Judge extraction on
specimens (which carry fabricated values) and judge *classification* on blank forms.

### `corpus-results.json`

Same data, complete: per document you get the manifest entry, the PDF's page count and
per-page character counts, the full classification with its evidence list, every field
with its confidence/verification/locator, the tiers that ran, and the server-side timings.
That is the file to diff between runs when you change anchors.

---

## 6. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Cannot talk to the service` | The service is not running, or is on another port. Start it, or pass `--url`. |
| `PyMuPDF is not installed` | `uv pip install pymupdf`. |
| Everything is `NEEDS_OCR` | The corpus is scans. For a *form*, re-source from the issuing authority — official forms are almost always digital PDFs. For an *identity document*, a scan is the real thing: run `--ocr`. |
| `does not start with %PDF` | The download saved an HTML page (login wall, 404, cookie interstitial). Delete the file and fetch the direct PDF URL. |
| `image has no text layer to read` | An image in the manifest and `--ocr` is off. That is correct behaviour, not a fault — rerun with `--ocr`. |
| `cannot reach OCR endpoint` | The Azure mock is not up. It ships with the DES stack: `docker compose up azure-ocr-mock` in `document-enrichment-services/`, then check `http://localhost:5006/health`. |
| `OCR analyze returned HTTP 401` | Real Azure without a key. Set `$AZURE_VISION_KEY` or pass `--ocr-key`. |
| OCR text is garbage on a non-Latin document | Expected against the local mock — its Tesseract is `eng`-only. Point `--ocr-endpoint` at real Azure. Do not file a registry defect from a mock run on a non-Latin document. |
| `is not a registry doctype_id` | Typo, or a guessed ID. Get the exact string from `GET /api/v1/doctypes`. |
| `file not found` | The manifest's `file` path is wrong. Use the repo-root-relative form: `corpus/us/us_w9.pdf`. |
| Report paths look absolute | The corpus is outside the repo (a `/tmp` fixture). Cosmetic only. |
