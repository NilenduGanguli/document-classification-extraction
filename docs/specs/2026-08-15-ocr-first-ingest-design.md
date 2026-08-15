# OCR-first ingest: reading a document completely before anything classifies it

**Status:** implemented, phases 0–6. 791 tests pass, ruff and tsc clean.
**Date:** 2026-08-15
**Supersedes the assumption in:** `dce/ingest/__init__.py:46-52`, `dce/ingest/pdf.py:32-35`

---

## Corrections to this plan, found while building it

Two things I asserted in §3 and §5 turned out to be wrong, and the design changed because
of them. Recorded rather than quietly edited, because both were load-bearing arguments.

**1. `max_ocr_pages` does not apply to the OCR-service path.** §5 warned that `always_ocr`
would truncate a 200-page filing at 10 pages. `_cap_service_view` (`pipeline.py:238`)
applies `max_blocks` / `max_chars` / `max_block_chars` / `max_tables` and **not**
`max_ocr_pages` — the service receives the PDF whole and is bounded by block and character
ceilings instead. The 10-page cap binds only the local-engine path, which rasterises page by
page. The cap was therefore left at 10 and made *visible* (a new `max_ocr_pages` entry in
`limits_hit`) rather than raised.

**2. Whole-document escalation is right for the service path and wrong for the local one.**
§3.1 argued a mixed document must be read one way throughout, to stop a zone-gated anchor
firing according to which pages happened to be scanned. That holds only where the two halves
carry *different* zone fidelity — which is the `azure_layout` case. On the local path both
halves are `Zone.body`: a PDF text layer by deliberate refusal to infer roles, and
`ocr_pages_to_builder` for recognised lines. Neither half can satisfy an anchor the other
cannot, so a per-page merge is provably free of that hazard and far cheaper. The
implementation therefore does both: **the service takes a mixed document whole; the local
engine reads only the pages that need it.**

**3. A refinement neither the plan nor the verification anticipated.** A page can be
inadequate and yet hold nothing to recover — a two-word cover sheet with no image. The first
implementation sent those to OCR and broke `test_pdf_pages_carry_geometry`, where three pages
of fifteen characters became `needs_ocr`. `PageVerdict.recoverable` (an image is present)
now separates *"we could not read this page"* from *"there is nothing on this page to read"*,
and the ordering is load-bearing: judging "every page is inadequate" before asking whether
any page has pixels routes every sparse text document to a recogniser. That is pinned by
`test_a_sparse_document_is_never_sent_to_a_recogniser`.

---

## 0. The problem, in one sentence

The service decides "does this document have usable text?" by summing alphanumeric
characters across the **whole document** and comparing to **40**. A document that is part
text and part scan passes that test on the strength of its text pages, and the scanned
pages are then silently discarded — not OCR'd, not classified, not reported as missing.

For a KYC corpus, the discarded part is usually the part that matters.

---

## 1. What is actually broken (all verified by execution, not by reading)

### 1.1 Mixed documents lose their scanned pages, silently — CONFIRMED, high

`dce/ingest/pdf.py:124-129`:

```python
outcome.alnum_chars = sum(_count_alnum(t) for t in page_texts)   # whole-document sum
if outcome.alnum_chars >= MIN_ALNUM_CHARS:                        # 40
    for index, text in enumerate(page_texts):
        builder.lines(text, zone=Zone.body, page=index + 1)
    return outcome                                                # returns BEFORE the OCR branch
```

Measured on a purpose-built 2-page PDF (p1 = 43 alnum chars of text, p2 = a rasterised
ID card carrying 179 alnum characters):

| | |
|---|---|
| `alnum_chars` | 43 (≥ 40 → text-layer branch) |
| `needs_ocr` | `False` |
| `ocr_pages` | 0 |
| blocks produced | 1, page 1 only |
| page-2 blocks | **0** |
| `status` / `truncated` / `limits_hit` / `reason` | `ok` / `false` / `[]` / `''` |

**81% of the document's characters are absent from the result and every completeness
signal reports a clean read.**

Three controls make the mechanism unambiguous:

- **With a working OCR provider passed in**, the provider is *never consulted* —
  `recognize` called for pages `[]`. The function returns before reaching the OCR branch.
- **Shortening page 1 from 43 to 15 characters** flips the outcome completely:
  `ocr_pages=2`, both pages read. 28 characters on an unrelated page decide whether the
  ID card is read at all.
- **A 3-page variant** (p1 text, p2+p3 image) drops both image pages.

Downstream, `classify()` sees only `'Invoice number 4471 issued by Acme Corporation Ltd.'`.
Substring checks for `PERMANENT ACCOUNT`, the name, and the ID number are all `False`.

The loss is invisible to a consumer: `LayoutView.pages` lists page 2 with real geometry
while `blocks` holds nothing for it — indistinguishable from a genuinely blank page.

**Why the suite misses it:** both PDF fixtures in `tests/ingest_fixtures.py` are
homogeneous — `text_pdf()` writes text on every page, `scanned_pdf()` puts an image on
every page. No fixture mixes the two.

### 1.2 The 40-character floor is a document-wide sum, so it decays with page count — CONFIRMED, high

The floor is effectively **40/N per page**. At `max_pages=200` that is 0.2 alnum
characters per page. Real scanner furniture clears it trivially:

- `"Scanned with CamScanner"` = 21 chars — one page fails, **two pages pass**
- a `"Page n of 10"` footer on a 10-page scan = 91 chars — passes comfortably

So the constant's own stated purpose ("below this, the text layer is a scanning artefact")
is defeated by exactly the artefacts it names.

### 1.3 There is no detection of hidden or garbage text — CONFIRMED, high

`alnum_chars >= 40` is the entire instrument. Nothing detects:

| | |
|---|---|
| invisible text (render mode 3) | not detected — `get_text("text")` returns it as ordinary text |
| zero font size (`0 Tf`) | not detected; PyMuPDF fragments it to one block per glyph and nothing notices |
| white-on-white | not detected — `span["color"]` is never read |
| a text layer from a previous bad OCR | not detected — garbage is rejected only if shorter than 40 chars |
| image-vs-text page coverage | not computed anywhere; `get_text("dict")` is never called |

### 1.4 Zone blindness — CONFIRMED, but smaller than assumed

I previously said 21 decisive anchors are title-gated. **The real number is 4.** Measured
against the live registry: 129 doctypes, 992 anchors, 189 decisive, 17 zone-gated in
total, of which **4 decisive** — all `zone=title`:

| doctype | anchor |
|---|---|
| `us_drivers_license` | `DRIVER LICENSE` |
| `ca_drivers_license` | `PERMIS DE CONDUIRE` |
| `ca_nexus` | `NEXUS` |
| `mx_comprobante_agua` | `SACMEX` |

The correction matters in both directions. The blanket claim "lexical reads are always
weaker" is overstated — 185 of 189 decisive anchors are zone-free. But **3 doctypes have
*every* decisive anchor title-gated**, and all three are photo IDs. On a lexical read
those three cannot be decisively identified at all.

Note this is not only a text-layer problem: `ocr_pages_to_builder` (`dce/ingest/ocr.py:347`)
also writes every recognised line as `Zone.body`, and `azure_read` returns no roles either.
**Only `azure_layout` supplies zones.**

### 1.5 Native-text formats have no adequacy test at all — CONFIRMED

`docx`, `xlsx`, `pptx`, `odt`, `rtf`, `msg`, `eml`, `html`, `csv`, `txt` take
`_parse_native` (`pipeline.py:533`). There is **no usable-text test and no OCR branch on
that path**. A `.docx` whose content is one embedded scan yields whatever text the parser
finds — typically nothing — and then hits the empty-document refusal at `pipeline.py:557`
as a **415**, not a `needs_ocr` **422**. That asymmetry is a real gap for KYC, where
"scan pasted into a Word file" is common.

### 1.6 Three copies of the rule, two different thresholds

| location | threshold |
|---|---|
| `dce/ingest/pdf.py:35` (the service) | **40** |
| `tools/corpus_test.py:140` (the harness) | **60** |
| `tools/channel_probe.py:63` | imports the harness's 60 |

`pdf.py:32-34` asserts these match. They do not. Every corpus number ever quoted was
produced by a harness that dispatches documents differently from the service.

---

## 2. The design tension, stated honestly

`dce/ingest/__init__.py:46-52` is an explicit, reasoned commitment **against** making OCR
the default. Its argument: OCR-by-default "would silently lower the quality of the
evidence the cascade scores", and the cascade's precision gates were tuned against
text-layer and Azure-quality input.

That argument was correct for the corpus it was written against, and it is wrong for
yours. It assumes the text layer is *good evidence*. §1.1–1.3 show the service cannot
currently tell a good text layer from a scanner watermark, and that when it guesses wrong
it discards most of the document without saying so. A silent 81% loss is worse evidence
than an OCR read of the same page, by any measure.

So this plan **overturns that commitment deliberately**, and records why. It does not
pretend the commitment was never made.

**What it must not overturn** (verified constraints):

1. **A zero-recogniser deployment must keep working.** `Dockerfile:77` installs `.[pdf]`
   only; there is no `httpx` in base deps. Text documents must still classify with no OCR
   configured and no socket available.
2. **The socket tripwire must still pass.** `tests/test_ingest_egress.py:133` ingests 11
   native formats *and a text-layer PDF* with `socket.socket`/`getaddrinfo` sabotaged.
3. **`needs_ocr` stays a routable answer**, not an error.
4. **Caller asymmetry stays**: a request may always decline recognition, never grant it.
5. **`assert_ocr_egress_permitted` stays a positive gate** on every submit and every poll.
6. **No role inference from font size.** `pdf.py:10-17` calls that "manufactured evidence"
   feeding a 3.0× title weight. This plan does not touch it.

---

## 3. Proposed design

### 3.1 Per-page adequacy, whole-document escalation

Replace the document-wide sum with a **per-page** verdict, then escalate the **whole
document** to OCR if any page read is inadequate.

Whole-document escalation, not per-page merge, on purpose: a merged document would carry
text-layer pages (all `Zone.body`) beside `azure_layout` pages (with roles). A title-gated
anchor would then fire or not depending on which pages happened to be scanned — the same
document, classified differently by an accident of its production. One document, one
reading, one evidence quality.

### 3.2 The adequacy predicate

A page is **adequate** when all hold:

| signal | rule | API | cost |
|---|---|---|---|
| character density | `alnum >= 40` **per page** | `get_text("text")` | already paid |
| image dominance | largest single image, clipped to `page.rect`, covers `< 60%` of the page | `get_image_info()` | ~200 µs |
| glyph sanity | not >90% single repeated char; not >50% control/replacement chars | in-process | ~50 µs |

Total added cost **~400–800 µs/page** on top of the current ~3036 µs/page — **+14% to
+25%**. Measured over 662 corpus pages.

Three measurement traps found the hard way, all of which the implementation must respect:

- **Image bbox can exceed the page.** `us_mortgage_statement.pdf` p1 yields a fraction of
  **1.165** uncorrected. Intersect with `page.rect` first.
- **Use the max over single images, not a sum or union.** `us_utility_bill.pdf` p2 has
  3985 image placements; the sum is meaningless, the max is 0.088.
- **`page.rect` is the CropBox** and MuPDF clips text to it before `get_text()` returns.
  Off-CropBox text is unobservable, so any rule that counts it is measuring zero.

### 3.3 What NOT to build: `get_texttrace()`

Render mode is the obvious way to detect invisible text. **It cannot be used.** Verified
on this venv (PyMuPDF 1.28.0 / MuPDF 1.29.0):

- The C device over-decrements `Py_None` **once per span per call** — measured 174.90
  None-refs/call on a 173-span page. A worker looping the corpus dies at **~130 pages**
  with `Fatal Python error: none_dealloc`. That is **SIGABRT: uncatchable, no traceback,
  kills in-flight requests.** `gc.collect()` does not recover it.
- The pure-Python fallback raises `TypeError: jm_lineart_ignore_text() takes 3 positional
  arguments but 4 were given` on any page containing mode-3 text — precisely the case of
  interest.

**Safe substitute if we want the signal later:** `page.read_contents()` + regex
`rb"(?<![0-9.])([0-7])\s+Tr\b"`, 196 µs/page, no trace device.

And it should not drive routing regardless: mode 3 is **neither necessary nor sufficient**.
`us_sec_10q.pdf` has mode-3 text on all 10 pages with zero images and 2000+ alnum chars
(born-digital tagging). `xx_iso_certificate.pdf` is a 100%-image scan carrying an OCR text
layer with **no explicit `Tr` at all**. Demote to a metadata flag; never a routing input.

### 3.4 The policy setting

```
DCE_INGEST_TEXT_LAYER_POLICY = trust | verify | always_ocr
```

| value | behaviour | for |
|---|---|---|
| `trust` | today's rule, bug fixed to per-page | zero-recogniser deployments |
| `verify` | **default** — per-page predicate; escalate whole document if any page fails | most |
| `always_ocr` | never read the text layer; OCR every document | **your KYC deployment** |

`always_ocr` is what you asked for and it is a legitimate setting — but it should be a
declared deployment posture, not the code default, because it bills Azure per page for
every document including born-digital ones that need nothing.

`read_channel` semantics are unchanged: `lexical` still declines recognition and still
wins over the policy. Caller-declines-always, caller-grants-never is preserved.

### 3.5 When no recogniser is configured

A page fails the predicate and nothing can read it. Today the whole request 422s. Proposed:
return the text we *do* have, and set `truncated=True` with a named cap
`inadequate_pages`, plus a `reason` naming the page numbers. The document still
classifies, but the loss is **declared** rather than silent. This is the single most
important change in the plan — it converts §1.1 from invisible to visible even on
deployments that can never fix it.

---

## 4. Phases

| # | Work | Ships |
|---|---|---|
| **0** | Mixed-doc + garbage-text fixtures in `tests/ingest_fixtures.py`; assert the current wrong behaviour so the change is visible as a diff | tests only |
| **1** | Per-page `alnum` floor + `inadequate_pages` cap. Fixes §1.1/§1.2 alone. | the silent-loss fix |
| **2** | Image-dominance + glyph-sanity signals, with the three measurement traps handled | the full predicate |
| **3** | `DCE_INGEST_TEXT_LAYER_POLICY`, default `verify`; console shows the policy and per-page verdicts | operator control |
| **4** | Unify `MIN_ALNUM_CHARS` (service 40 / harness 60) to one constant | measurement honesty |
| **5** | Native-format adequacy: a `.docx` that is one embedded scan → `needs_ocr` (422), not `UnsupportedFormat` (415) | §1.5 |
| **6** | Re-baseline the corpus under each policy; report the precision delta | evidence |

Phase 1 alone fixes the defect that is losing your documents. Phases 2–3 are what make it
tunable. Phase 6 is where we find out whether OCR-first actually classifies better — I am
not assuming it does.

---

## 5. Cost and risk

**Per-document cost under `always_ocr`.** Every document goes to Azure — including the
born-digital PDFs that classify fine today for free. On this corpus that is roughly 7× the
current OCR call volume. `max_ocr_pages=10` and `max_pages=200` were sized for OCR as the
exceptional case; a 200-page filing that classifies fine on its text layer today would be
truncated at 10 pages under `always_ocr`. **These caps must be revisited in Phase 3 or the
fix for §1.1 creates a new silent truncation at a different boundary.**

**Latency.** OCR is seconds per page. `max_seconds=20.0` and the 30 s service timeout will
both need raising for multi-page scans, and that interacts with the T3 timeout you already
hit.

**Precision is unproven.** The commitment in §2 may be partly right: OCR text is noisier
than a good text layer. Phase 6 measures it rather than assuming. If precision drops, the
default stays `verify` and `always_ocr` remains yours to opt into.

**Test blast radius**, highest coupling first: `tests/test_ingest_egress.py` (the whole
file — the `OFF`/`ON` constants encode the current default posture),
`tests/test_azure_ocr_providers.py` (guards, off-by-default, decline matrix, pin matrix,
`/readyz` wording), `tests/test_ingest_formats.py:253,277,324`.

---

## 6. Decisions I need from you

1. **Default policy for your deployment** — `always_ocr` (what you asked for; bills every
   document) or `verify` (OCR only when a page is inadequate; a born-digital PDF stays
   free)? I lean `verify` for the default and `always_ocr` set explicitly in your env,
   which gets you the behaviour you want without making it everyone's.
2. **No-recogniser behaviour** — degrade-with-`truncated` as proposed in §3.5, or keep
   today's hard 422?
3. **Scope now** — Phase 1 alone (stops the bleeding, small diff), or 1–3 together?
