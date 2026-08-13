# Visual classification (IBTM) as a parallel avenue — design for review

**Status:** CLOSED — **the visual avenue does not ship.** Four methods were built and measured
against the real corpus; none reached the precision bar; the avenue is closed rather than
iterated a fifth time. What shipped is the *evaluation apparatus and the honest report*:
`/api/v1/classify/compare`, a `second_avenue` block on `/readyz` publishing availability and
coverage, and an empty-by-construction registry in `dce/visual/` that hands back the measurement
when somebody configures a retired method.

- §5A — spike 1 (SIFT + homography). NO-GO.
- **§5B — spikes 2, 3 and 4** (structure, layout signature, emblem). All NO-GO. **Read this one.**
- **§10 — what shipped, and why an empty registry is a deliverable.**

**Date:** 2026-08-08, closed 2026-08-11

Two things in this document:

1. A correction to how the service reports on-premises OCR endpoints (small, uncontroversial).
2. A second, independent classification avenue — instance-based template matching with homography
   and RANSAC — running beside the existing lexical cascade so the two can be compared on real
   traffic.

---

## 0. On-premises OCR is not egress — but the code cannot know that

The correction is accepted, and it changes what the service should *say*, not what it should *do*.

An HTTP call to `https://ocr.internal.corp` and one to `https://x.cognitiveservices.azure.com` are
the same operation to the process making it. Nothing in the request distinguishes them, and a
hostname is not evidence — `ocr-onprem.example.com` can resolve anywhere. So the service must not
infer the trust boundary, and it must not silently assume the safe answer.

**Proposal.** The deployment *declares* the boundary, and the declaration is attributable:

```
DCE_INGEST_REMOTE_OCR_TRUST_BOUNDARY = external | on_premises     # default: external
```

- `external` (default, unchanged behaviour): posture reads *"this deployment sends images and
  scanned PDFs to a remote OCR endpoint before their doctype is known."*
- `on_premises`: posture reads *"…sends them to `<host>`, which this deployment **declares** to be
  inside its own trust boundary. That declaration is the operator's; the service cannot verify it."*

Why phrase it that way rather than just suppressing the warning: an auditor reading the posture page
should see a claim with an owner. "The operator asserted this host is on-prem" is a reviewable
statement. A page that simply goes quiet because a config flag was set is not.

The safe default stays `external`, so a deployment that forgets to declare gets the cautious
reading, never the reassuring one.

Cost: one setting, one posture string, one test. No behavioural change.

---

## 1. Why a second avenue at all

The existing cascade classifies from **text**: anchors, checksums, and a zone-weighted lexical
profile. Its failure modes are now well characterised, because we measured them:

| Failure | Evidence |
|---|---|
| No text layer at all | 8 of 158 corpus documents record `needs_ocr` and are never classified |
| Degraded OCR on a scan | A real Aadhaar scan through Azure DI abstained: anchors said `in_aadhaar`, the lexical channel said `in_ckyc_record`, coverage 0.191 against a 0.20 floor |
| Non-Latin script | 123 Devanagari anchors, largely unexercised; OCR mangles Devanagari badly |
| Document-class-name collisions | 11 decisive-anchor asymmetries found; `ARTICLES OF INCORPORATION` is not owned by any one issuer |

A geometric method fails in **different** places. It does not care what the OCR made of the text; it
cares whether the page *looks* like a known form. That is the argument for running both: not that
one is better, but that their errors are uncorrelated. Where they agree, confidence is genuinely
higher than either alone. Where they disagree, that is exactly the document a human should see.

That is the deliverable — not "replace the cascade", but "a second opinion with independent failure
modes, and the machinery to find out where each is right."

---

## 2. What the method actually is, and where it will and will not work

The proposal matches **layout**, not content. That single fact predicts most of its behaviour.

**Should work well — fixed-layout documents.** Identity cards (Aadhaar, PAN, Voter EPIC, INE,
driving licences), statutory forms (W-9, W-2, 1099, MGT-7, AOC-4, Form 16), certificates. These have
a stable printed skeleton — rules, boxes, seals, logos, headings in fixed positions — and two
instances differ only where a human filled them in. Keypoints on the skeleton correspond; keypoints
on the filled values do not. RANSAC is precisely the tool for keeping the former and discarding the
latter.

**Should work badly — free-form documents.** A 10-K, an annual report, a lease, a board resolution,
a bank statement. There is no template. Two annual reports from different issuers share a genre, not
a geometry. Expect near-zero inliers and correct abstention — which is acceptable behaviour, but
means the method contributes nothing for roughly half the registry.

**The risk nobody should skip past: documents are not textured scenes.** SIFT and ORB were built for
natural images. A page of text yields many keypoints that are individually non-distinctive — every
`e` resembles every other `e`. Lowe's ratio test will reject most matches precisely because the
second-nearest neighbour is as good as the nearest. Whether enough distinctive structure survives
(logos, seals, rules, headings) is an empirical question, not a design one, which is why the spike
below runs before any implementation.

---

## 3. Four design corrections to the sketch

The proposal is sound in outline. Four things in it would cause defects we have already been bitten
by once in this codebase, so they are worth fixing before writing code rather than after.

### 3.1 A raw inlier count is a registry-size-style defect waiting to happen

`if inlier_count > max_inliers and inlier_count >= MIN_THRESHOLD` compares a count across templates
of wildly different feature density. A dense text form yields several times the keypoints of a sparse
ID card, so a fixed floor of 20 is a strict test for the card and a loose one for the form.

This is the same shape as the defect already found and fixed in the lexical channel: **an absolute
threshold over a quantity whose scale varies with what it is measured against.** There, adding
doctypes silently degraded every existing one. Here, the threshold would mean something different
per template — and would drift every time a template was replaced with a better scan.

**Correction.** Decide on a scale-invariant quantity: inlier *ratio* (inliers / good matches),
optionally with a per-template expected-inlier normalisation calibrated once at index build time,
plus an absolute floor to reject the degenerate low-match case. The spike measures which of count,
ratio, or normalised score actually separates.

### 3.2 RANSAC is randomised — and determinism is a compliance property here

`cv2.findHomography(..., cv2.RANSAC)` draws random samples. Run twice, get two answers.

That collides directly with a property established for this service and verified across 148
documents, three processes and three `PYTHONHASHSEED` values: **same document plus same rules
produces a byte-identical answer.** An auditor was told that. A randomised classifier would make it
false.

**Correction.** Determinism is a hard acceptance criterion, not a nice-to-have. Seed via
`cv2.setRNGSeed()`, pin thread counts, and prove reproducibility across process restarts as a test —
the same test shape already used for the cascade. If it cannot be made reproducible, the method ships
as advisory-only and never as an authority, and that must be stated on the endpoint.

### 3.3 A linear scan over 182 templates is the wrong architecture

Matching against every template in turn is `O(N)` brute-force matchers per document. The standard
solution to this exact problem is image retrieval's: one approximate index (FLANN/IVF) over *all*
templates' descriptors, a single k-NN query, vote per template to get a shortlist, then run RANSAC
only on the top-k candidates.

That is both faster and better founded — geometric verification is expensive and belongs on
candidates, not on the whole database. The one thing it costs is recall if the shortlist ever misses
the true template, so the spike measures that explicitly.

The user's own optimisations (pre-computed descriptors, early exit on a very high inlier count) are
both correct and included. Early exit needs care to stay deterministic — an exit rule that depends on
iteration order makes the answer depend on template insertion order.

### 3.4 Template images are reference data, and the PII rule applies

The method needs a reference image per doctype. The obvious source — a real Aadhaar, a real driving
licence — is exactly what the corpus sourcing rule forbids, and three files were already deleted from
this repo for carrying real individuals' PANs.

**Correction.** Templates come from blank forms and published specimens only, same rule, same review.
This bounds coverage: the corpus can supply a template for some fraction of 182 doctypes and no more.
The spike counts that fraction, because a method covering 40% of the registry is a different
proposition from one covering all of it, and the plan should not pretend otherwise.

---

## 4. Architecture

Kept deliberately separate from the existing cascade — no shared decision path, no shared thresholds,
no chance of one silently changing the other.

```
                      ┌──────────────────────────────────────────┐
  document bytes ────►│ dce/ingest  (already exists)             │
                      │ bytes → LayoutView, + page raster        │
                      └───────────┬──────────────────┬───────────┘
                                  │ text             │ image
                                  ▼                  ▼
                   ┌──────────────────────┐  ┌────────────────────────┐
                   │ dce/classify         │  │ dce/visual   (NEW)     │
                   │ anchors + lexical    │  │ keypoints → shortlist  │
                   │ UNCHANGED            │  │ → RANSAC → inlier score│
                   └──────────┬───────────┘  └───────────┬────────────┘
                              │                          │
        POST /api/v1/classify │                          │ POST /api/v1/classify/visual
                              │                          │
                              └──────────┬───────────────┘
                                         ▼
                              POST /api/v1/classify/compare   (NEW)
                              runs both, reports agreement,
                              decides nothing
```

**Three endpoints, three jobs.**

- `/api/v1/classify` — untouched. All 774 tests keep passing; this is non-negotiable.
- `/api/v1/classify/visual` — the new avenue, standalone, same response shape (`doctype_id`,
  `confidence`, `abstained`, `reason`, `runners_up`, `evidence`) so the console and the corpus
  harness can read it with no special-casing. Evidence here means matched template id, inlier count
  and ratio, and the homography's condition — the visual equivalent of "which anchors fired".
- `/api/v1/classify/compare` — runs both and reports **agree / disagree / one-abstained**, with both
  decision trails. It deliberately does not adjudicate. Fusing the two is a later decision that
  should be made on data this endpoint produces, not guessed at now.

**New package `dce/visual/`**, mirroring `dce/ingest/`'s shape: a closed provider registry
(SIFT / ORB), settings with everything off by default, a template index built offline by a tool
under `tools/`, and no import of `cv2` unless the feature is switched on — the same discipline that
keeps `transformers` out of the default image.

**Egress:** none. All computation is local; this is pure in-process CPU work. It strengthens the
zero-egress story rather than complicating it — a document that today must be shipped to a remote
OCR to be readable might instead be classified locally by its layout.

**Dependency:** `opencv-python-headless` + `numpy`, as an optional extra (`.[visual]`), never base.
Headless specifically: the GUI build pulls in X11 libraries a server has no use for.

---

## 5. Feasibility — being measured now, not asserted

A spike is running against the real corpus before any implementation. It answers, with numbers:

1. **Separability.** Same-template versus different-template inlier distributions, their overlap, and
   whether any global threshold admits a useful share of true pairs at **zero** false accepts. Uses
   both synthetic degradation (rotate, rescale, JPEG, crop, noise — realistic rescan variation) and
   the genuine near-duplicate pairs the corpus already contains (`*_2` files), reported separately,
   because the real pairs are worth more than the synthetic ones.
2. **Determinism.** Whether seeding makes RANSAC reproducible across runs and restarts.
3. **Family split.** Separability within fixed-layout documents versus free-form ones — the number
   that decides whether the two avenues are complementary or redundant. Plus the hard cases:
   `us_articles_incorporation` vs `us_articles_organization_llc`, `in_aadhaar` vs
   `in_aadhaar_masked`, `mx_cif` vs `mx_rfc_csf` — near-identical layouts that must not be confused.
4. **Cost.** Per-comparison timing, memory per template, naive-scan projection, and the speedup from
   the shortlist architecture, including whether the shortlist ever drops the true template.
5. **Coverage.** How many of the 182 doctypes the corpus can supply a compliant template for.

**Go / no-go.** If the between-class 99th percentile overlaps the within-class 5th percentile — no
usable threshold — the method does not ship as an authority. It may still ship as an advisory signal
on the compare endpoint, or not at all. That criterion is fixed *before* seeing the numbers, on
purpose.

---

## 5A. Spike results — NO-GO on the method as proposed

The go/no-go was fixed in advance: *no usable threshold ⇒ does not ship as an authority.* It
returned no usable threshold, by a wide margin.

### The headline

SIFT keypoints on a rendered document page key on **glyph shapes, not layout**. Any two text-bearing
pages share an enormous vocabulary of letterforms, and RANSAC will happily fit a homography that
aligns rows of text. So unrelated documents score *higher* than related ones.

Verified independently by the lead, SIFT + BF + Lowe 0.75 + `findHomography(RANSAC, 5.0)`:

| pair | inliers | ratio |
|---|---|---|
| **unrelated** — `in_driving_licence` × `mx_aviso_privacidad` | **316** | 0.601 |
| **unrelated** — `in_ckyc_record` × `us_articles_organization_llc` | **761** | 0.679 |
| **same type** — `in_utility_electricity` × `in_utility_electricity_2` | **23** | 0.029 |

### Separability: none

| population | n | min | p5 | median | p95 | max |
|---|---:|---:|---:|---:|---:|---:|
| synthetic same-template (page vs its own degraded copy) | 402 | 169 | 508 | **1720** | 3790 | 6190 |
| **real** same-doctype pairs (genuinely different files) | 16 | 6 | 6.8 | **22.5** | 534 | 1580 |
| different doctype | 2500 | 0 | 3 | **21** | 117 | 1531 |

The real-positive median (22.5) and the negative median (21) are the same number. Inlier ratio is no
better — both medians are 0.105 to three decimals.

**AUC on real pairs: 0.568 (count), 0.528 (ratio). Chance is 0.500.**

At the proposed floor of 20 inliers, **52.6% of different-doctype pairs pass** — the floor is a coin
flip. A zero-false-positive threshold must exceed 1531 (the highest negative observed), which
retains 1 of 16 real positives.

**End to end**, leave-one-out over 134 rendered pages against a 133-page bank at floor 20:
**3 correct, 131 wrong, precision 0.022.** Under the most generous closed-world setting — bank
restricted to the 14 doctypes that have a sibling, correct template guaranteed present — top-1 is
13.8% against a ~7% random baseline. A faint signal, nowhere near a decision.

### The methodological trap, which is the most valuable finding here

Synthetic degradation reported **AUC 0.9992**. Real pairs reported **0.568**.

Rotating, rescaling and JPEG-ing a page and then re-matching it measures *"can you re-find the same
JPEG"* — not *"can you classify a document"*. Validating on synthetic variation alone would have
produced a confident, shippable, useless classifier. This is the same class of error as the corpus
harness that hardcoded `zone: body`: **the measurement instrument answering an easier question than
the one being asked.** Any future visual work must be validated on genuinely distinct files.

### Blank-vs-filled — the property the method was supposed to exploit

All five blank/filled pairs in the corpus: **6, 18, 19, 22, 23 inliers**. They straddle the proposed
floor of 20, in a regime where half of all unrelated pairs also clear it. The case that should have
been easiest is indistinguishable from noise.

### Determinism — reproducible, but not in the way that matters

Good news: on OpenCV 5.0.0, `findHomography(RANSAC)` is deterministic without seeding — identical
across 10 calls, process restarts, thread counts 1/2/4/8/auto, 8 concurrent threads, and after
burning the global RNG stream. `cv2.setRNGSeed()` is not required.

Bad news: **the answer is a function of match order, and that function is violently unstable.**
Permuting the identical 1130 correspondences for one pair gave inlier counts of
`39, 44, 48, 47, 49, 34, 36, 50, 17, 45, 49, 42, 43, 41, 41` — 13 distinct values, range 17–50, a
~3× swing straddling any floor near 20. Each fixed permutation re-runs stably, confirming order and
not randomness.

So the guarantee is *"same bytes + same pinned OpenCV + same match ordering → same answer"*, not
*"same document → same answer"*. An OpenCV upgrade, a rasteriser change, a one-pixel shift, a
different DPI or a re-scan all move the score. The estimator constant is load-bearing too and was
unspecified in the proposal: on one input, `RANSAC=23, USAC_DEFAULT=27, USAC_MAGSAC=30, LMEDS=0,
RHO=0`.

### Cost — fine, and not the binding constraint

SIFT detect 34 ms median; per-comparison match+RANSAC 48.5 ms. Naive scan over 182 templates: 8.9 s
(SIFT) / 3.9 s (ORB). The FLANN shortlist architecture proposed in §3.3 works as expected —
**28.2× speedup**, 0.31 s per document, with no top-5 recall loss on synthetic variants. SIFT
descriptors cost ~4 MB per template (~730 MB for 182); ORB is 25× smaller at 0.16 MB.

Cost was never the problem. Separability was.

---

## 5B. Spikes 2, 3 and 4 — the three §9 candidates, all NO-GO

§9 named three alternatives to the dead SIFT method and recommended running 9.1 first. All
three were built and measured. **All three failed, each for a different reason, and the three
reasons together close the avenue rather than pointing at a fourth descriptor.**

The bar was fixed before any of them ran, and it is the owner's: **precision ≥ 95% on the
documents the method ANSWERS.** Coverage is the honest variable. Abstention is free — this
service routes abstentions to a human by design — so a method answering 20% of documents and
right on all of them is a success. Nothing below gets close enough for that trade to arise.

### The four candidates, side by side

| method | real-pair AUC | AUC vs hard negatives | best precision, any threshold | coverage there |
|---|---:|---:|---:|---:|
| §5A SIFT + homography | 0.568 | — | 0.022 | 100% |
| 9.1 structure skeleton | 0.627 | **0.531** | 0.067 | 11.2% |
| 9.2 layout signature | **0.846** | **0.483** | 0.080 | 18.7% |
| 9.3 emblem / seal match | 0.554 | inverted (see below) | **0.000** | — |
| **lexical cascade** | — | — | **0.983** | **79.3%** |

Chance is 0.500. The bar is 0.950. The best visual result is 0.080 — **the gap is ~12×, not a
tuning margin**, and it does not close at any threshold, on any descriptor, at any resolution.

### 9.1 Structure skeleton — template identity is not doctype identity

Render page 1, Otsu-binarise, morphologically open with a horizontal kernel 5% of page width
(longer than any printed word, so text rows annihilate and rules survive) and a vertical kernel
2.5% of page height, then keep non-line marks taller than 4× the document's *own* median
connected-component height — a per-document self-calibrating reference rather than a global
constant. What survives is the template. Three descriptor generations, each killed by a
measured defect rather than by taste: v1 (occupancy grid + Dice) died of sparsity inflation and
subset inflation; v2 (explicit rule segments, `min(A→B, B→A)`) fixed both and then under-scored
true positives because it matched in absolute page coordinates; v3 normalised to the structure's
own bounding box with an exhaustive shift search. v3 is the number reported.

**Two independent ceilings, both hit before any threshold is chosen:**

- **10 of 16** real same-doctype pairs score exactly **0.000** — they share *no* structure at
  all. An Infosys auditor's report and a Titan auditor's report are not the same form. That is
  correct behaviour and it is also the ceiling.
- **45 of 134** rendered pages carry fewer than 8 rule segments: no template to match.

And the failure is **inverted**, not merely weak. Best positive pair in the whole corpus:
`xx_ubo_declaration` × `_2` at 0.490. Hard negatives above it:

```
mx_cif × mx_rfc_csf                              1.000
us_sec_form4 × us_sec_form5                      1.000
us_articles_incorporation × us_articles_org_llc  0.706
us_drivers_license × us_state_id                 0.664
us_w8bene × us_w9                                0.626
```

Best cell in the entire sweep (score ≥ 0.30, evidence ≥ 40): **1 correct of 15 answered,
precision 0.067**. Precision is **exactly 0.000** at every threshold ≥ 0.50, at every evidence
gate tested. Raising the threshold buys abstention and never precision — at 0.80 it answers 6
documents and is wrong on all 6.

### 9.2 Layout signature — the confusables *are* the same layout

Six L1-normalised, correspondence-free descriptors per page (16×16 ink grid, 128-bin H/V
projection profiles, connected-component geometry histogram, RLSA block-coverage grid, block
column profile) plus aspect ratio and coverage, compared by Hellinger affinity. No matching, no
RANSAC, no ordering — the determinism problem in §5A disappears entirely, and this candidate is
bitwise reproducible (permuting page order reproduces the similarity matrix exactly, max abs
diff 0.0).

**It has the strongest real signal of the four — AUC 0.846 — and is still dead**, which is the
most instructive result in this document. Against hard negatives the AUC is **0.483, below
chance**. The positive band `[0.822, 0.946]` sits *strictly inside* the negative band, whose top
is populated entirely by confusables:

```
mx_cif × mx_rfc_csf                    1.0000     ca_information_circular × _real_filing  0.8224
us_sec_form3 × us_sec_form5            1.0000     in_utility_electricity × _2             0.8247
us_drivers_license × us_state_id       0.9990     in_brsr × in_brsr_3                     0.8394
us_real_id × us_state_id               0.9907        ^ the strongest true positives
us_ead × us_green_card                 0.9827
```

**Precision is anti-correlated with score.** At the top of the ranking — the 14 most confident
documents — precision is **0.000**. Every single highest-confidence answer is wrong. A method
whose confident answers are its wrong answers is strictly worse than abstaining and cannot be
rescued by raising the threshold; raising it to the maximum takes precision *to* zero.

Finer resolution does not help (8/16/32/64 grids: AUC flat at 0.79–0.81) because the confusables
are the same layout *at every scale*.

The weak global signal is real, and it lives entirely in the easy negatives — a one-page ID card
versus a 300-page prospectus — which the lexical cascade already answers confidently. Coverage
added: approximately zero.

### 9.3 Emblem / seal detection — a seal is the letterhead, not the title

Detect compact, isolated, internally-complex ink blobs (Otsu ∪ saturated-colour mask, two
closing scales, min side 40px, aspect within 1:3–3:1, Canny edge density ≥ 0.03, plus a
baseline-neighbour test that rejects anything with ≥2 same-height components on its vertical
midline — characters always have those, a mark does not). 476 marks over 173 pages; 107 of 134
files carry at least one.

**Precision 0.000 at all 54 operating points swept** (floor ∈ {0.30 … 0.99} × margin ∈ {0.00,
0.05, 0.10}, all-marks and top-1-only). With no threshold at all: 0 correct, 107 wrong. On the
19 files where a correct answer is even possible: 0 correct, 19 wrong.

The detector is **not** at fault, and this was checked rather than assumed: identity retrieval —
every mark queried against a bank containing itself — is rank-1 precision **0.9916**, and self-NCC
is exactly 1.0. It retrieves the IRS eagle across two distinct files at NCC 1.000. It is
answering the wrong question, perfectly.

**Mechanism, and it is the cleanest of the four.** An emblem identifies the **issuer**, and one
issuer issues many doctypes. Every corpus pair scoring ≥ 0.94 is a *different doctype from the
same issuer* — IRS 1099 vs W-2, SEC Form 4 vs Form 5, SAT CIF vs RFC-CSF, five MCA forms, Texas
articles of incorporation vs articles of organization. Conversely every genuine same-doctype pair
in the corpus comes from a *different issuer* — two BRSRs from two companies, two ISO certificates
from two certification bodies, two electricity bills from two DISCOMs — so they share no mark and
cannot score high. **Raising the threshold does not filter toward correctness, it filters toward
shared-issuer confusion.**

This misreads the registry principle it was built on. A decisive lexical anchor works because the
string is issuer-controlled **and form-unique** (`INITIAL STATEMENT OF BENEFICIAL OWNERSHIP OF
SECURITIES`). A seal is issuer-controlled and **form-agnostic**. The lexical flow already reads
the title.

### The methodological trap, reproduced deliberately on candidate 9.3

§5A's most valuable finding was that synthetic degradation reported AUC 0.9992 for a method whose
real-pair AUC was 0.568. That trap was re-run on the emblem method **on purpose**, as a control:

| pairs | median score |
|---|---:|
| synthetic (same page rotated 1.5°, rescaled 0.9, JPEG q70, re-matched) | **0.877** |
| real same-doctype pairs | **0.316** |

Had the synthetic row been reported, this method would have looked strong and shippable. **Every
headline number in §5B is computed on genuinely distinct files only.** No synthetic pair appears
in any positive population, in any AUC, or in any end-to-end run — not even as a robustness
check, because there was never a working method whose robustness was worth checking.

### The three population ceilings, which bind before any method is chosen

Any future visual candidate inherits these, and a candidate reporting high coverage *and* high
precision on this corpus should be checked against them before it is believed:

1. **24 of 158** corpus files (22 `.htm`, 2 `.xlsx`) have no canonical page raster at all. A
   visual flow starts at a hard **84.8%** coverage ceiling before it classifies anything.
2. **Only 14 of 119** doctypes have ≥ 2 rasterisable files, so at most **29 of 134** documents
   *can* be right under leave-one-out. Measured against even that generous ceiling, the best
   candidate gets 6/29 = 20.7%.
3. Real same-doctype documents from different issuers **are not the same form**, so the
   population of visually-matchable positives is much smaller than the registry suggests.

### Determinism — the one dimension where all three beat the dead method

Worth recording, because it is the constraint §5A failed and these did not. All three were
rebuilt in fresh processes and compared bitwise:

- structure: 8911/8911 pair scores bit-identical under `PYTHONHASHSEED=12345`, sha256 identical.
- layout signature: all 8 descriptor arrays `np.array_equal` True; permuting page order
  reproduces the full similarity matrix exactly (max abs diff 0.0).
- emblem: `bank.npz` md5 identical across two builds.

None has any analogue of the dead method's match-order dependence (17–50 inliers from the same
1130 correspondences), because none has correspondences to order. **Determinism is not why these
methods die.** They die on separability, as the first one did.

### Why this closes the avenue rather than motivating a fifth descriptor

The three failures are not three near-misses. They are three *different* proofs of one thing:

> Every visual method must discard glyph identity to escape the §5A failure. The signal that
> separates the doctypes precision depends on **is** glyph identity. Putting the glyphs back
> produces the lexical flow.

Concretely: `mx_cif` and `mx_rfc_csf`, `us_sec_form3/4/5`, `us_drivers_license`/`us_real_id`/
`us_state_id`, the W-8/W-9 grid family, and the state articles-of-incorporation forms are all
**template-identical by construction**. They differ in a title word and a checkbox. A layout
descriptor cannot see a title word, at any resolution, under any distance, with any threshold.

The one place structure was genuinely right — ACORD 25 across two unrelated issuers (0.446), and
the OMB/agency form-number families — is exactly where the lexical flow already answers
confidently from the printed form number. **A method whose only correct answers are on documents
the existing flow already gets is not worth its maintenance cost**, however free its abstentions
are.

### Two registry defects found as a by-product, and what the lexical flow actually does with them

Both were surfaced by the visual work and then **checked against a live corpus run** rather than
assumed:

**1. `mx_cif` and `mx_rfc_csf` are the same rendered document under two registry doctype names.**
Same SAT *Constancia de Situación Fiscal*, same taxpayer, same date. Structural similarity 1.000,
layout similarity 1.000, emblem similarity 1.000. **No classifier of any kind — lexical, visual or
otherwise — can separate them**, so the registry is asking for a decision that does not exist.

*What it costs today, measured:* `mx_cif` **abstains**, and the trail names the defect precisely —
*"anchors point at `mx_rfc_csf`, the lexical profile at `mx_cif`; no doctype holds a decisive
anchor that only it could hold."* `mx_rfc_csf` classifies correctly at 0.77. So the two-channel
concurrence rule is *catching* this: it costs one abstention, i.e. **coverage, not precision**.
That is the rule working as designed on an impossible input. The fix is a registry decision —
merge the two doctypes, or give `mx_cif` an anchor the CSF genuinely cannot carry — not a
classifier change.

**2. `us_1099` and `us_w2` share an identical page-1 IRS "Attention" cover sheet.** Any
page-1-only classifier is guessing between them; the only structure surviving glyph-stripping on
either is the IRS flag banner, at structural score 1.000.

*What it costs today, measured:* the lexical flow does **not** classify on page 1 alone, so it is
not exposed the way a visual flow would have been. `us_1099` abstains (anchors point at `us_w9`,
profile at `us_1099`) and `us_w2` is fine. The exposure is real for any *future* page-1 visual
method, and is recorded here for that reason.

**3. The one finding that actually undercuts the premise of this whole document.** §1 argued for a
second avenue on the grounds that *"their errors are uncorrelated."* The corpus run says the
lexical flow's 2 wrong answers are `ca_aif` → `ca_ni_51_101_oil_gas` and **`us_real_id` →
`us_drivers_license`**. That second one is a *named hard negative* on which the layout signature
scored **0.9907** and the emblem method **0.874** — both would have made the same confusion, with
high confidence. In the one place a second avenue would have been worth having, **the errors are
correlated, not independent.** The uncorrelated-errors premise is not merely unproven by this
work; it is contradicted at the single point it was supposed to pay off.

---

## 6. Stability risks, ranked

| # | Risk | Mitigation | Residual |
|---|---|---|---|
| 1 | **Non-determinism from RANSAC** breaks the reproducibility property the compliance story rests on | Seed RNG, pin threads, test across restarts | If unfixable: advisory-only, stated on the endpoint |
| 2 | **Text yields non-distinctive keypoints**; ratio test rejects almost everything | Measured in the spike; consider structure-only preprocessing (morphological line/box extraction) before keypoints | May restrict the method to logo/seal-bearing documents |
| 3 | **Scale-dependent threshold** repeats a defect already fixed once here | Ratio or per-template normalisation; scale-invariance test as an acceptance criterion | — |
| 4 | **Near-identical layouts confused** (masked vs unmasked Aadhaar; CIF vs CSF) | Measured explicitly; a confusable pair that cannot be separated must abstain, never guess | Precision-first: abstention is the correct answer |
| 5 | **Template coverage gap** — no template, no answer | Count it, publish it per doctype on the endpoint | Method covers a subset; the cascade covers the rest |
| 6 | **opencv is a large dependency** with its own CVE surface | Optional extra, headless build, never in the default image | Only deployments that opt in carry it |
| 7 | **Template drift** — an issuer redesigns a form and the template silently stops matching | Templates are versioned reference data with provenance, like the corpus | Needs a refresh process, same as the registry |

---

## 7. Phasing

Each phase ends in a decision, so this can be stopped cheaply if the spike or the bake-off disappoints.

- **Phase 0 — spike (running).** Numbers on separability, determinism, cost, coverage. → go/no-go.
- **Phase 1 — `dce/visual/` + template index tool.** Offline index build from compliant templates;
  deterministic scoring; unit tests including scale-invariance and reproducibility. No endpoint yet.
- **Phase 2 — `/api/v1/classify/visual`.** Standalone endpoint, same response shape. Corpus harness
  gains `--engine visual` so both avenues are scored by the same instrument — the instrument that
  was itself found to be lying once, and is now zone-faithful.
- **Phase 3 — `/api/v1/classify/compare` + console.** Agreement/disagreement reporting, a fourth
  console tab showing both trails side by side. This is the artefact that answers "which works
  better", and it answers it per document family rather than with one number.
- **Phase 4 — fusion, only if the data supports it.** Deliberately last. Fusing two channels is
  exactly where this codebase has produced its worst bugs (the two-channel concurrence rule, the
  zone-free guard, the jurisdiction veto — each fixed a real problem and introduced a new one). No
  fusion rule until Phase 3 has produced evidence about where each avenue is right.

---

## 9. What to do instead

The *goal* — a second classification avenue whose errors are uncorrelated with the lexical
cascade's — remains sound and worth pursuing. The measurement kills one method, not the idea. Three
candidates, in order of how well they fit the constraints already established (no training,
on-premises, deterministic, precision-first):

### 9.1 Structure-only matching — strip the glyphs first (recommended next spike)

The failure has a specific cause: keypoints land on letterforms. So remove the letterforms. Standard
document-image-analysis preprocessing — morphological opening with long horizontal and vertical
kernels, connected-component filtering by aspect and area — isolates the **rules, boxes, seals and
logos** and discards the text. What remains *is* the template.

This directly attacks the measured mechanism rather than hoping around it, and it keeps everything
else in the design (shortlist index, RANSAC verification, abstention discipline). It is a half-day
spike using the same harness that produced the numbers above, with the same pre-registered go/no-go
and — critically — validated on genuinely distinct files, never on synthetic degradation.

### 9.2 A layout signature rather than keypoint matching

Cheaper and more robust than correspondence matching: reduce a page to a low-dimensional descriptor
of its *geometry* — projection profiles, text-block segmentation, ink-density grid, margin and
column structure — and compare descriptors directly. No homography, no match ordering, so the
determinism problem in §5A disappears entirely. Weaker at fine discrimination, but it fails safely
and is trivially deterministic.

### 9.3 Logo and seal detection specifically

Rather than matching whole pages, detect and match only the parts that genuinely identify an issuer:
the UIDAI emblem, the INE seal, the IRS masthead. This is where distinctive, non-repeating visual
structure actually lives, and it aligns exactly with the registry principle already established —
*a decisive anchor must be a string one issuer controls.* A seal is that principle in pixels.

Highest precision of the three, narrowest coverage, and it needs a detector per emblem.

### What I would not do

**Deep visual embeddings** (CNN/ViT page classifier) would very likely work — it is the standard
answer to this problem. But it needs training data and a model, which is what the no-training
constraint exists to avoid; it reintroduces the dependency weight the BERT evaluation just concluded
was not worth carrying; and it produces a score with no decision trail, which is the opposite of what
this service is for. Worth revisiting only if 9.1–9.3 all fail and the coverage gap is judged
business-critical.

### Recommendation

Run the 9.1 spike before committing to anything. It is cheap, it targets the measured cause, and the
harness already exists. Hold the endpoint architecture in §4 — it is independent of which visual
method eventually goes behind it, and `/classify/compare` is worth building regardless, since it is
how any second avenue gets evaluated honestly.

---

## 8. What this plan does not claim

- That the method will work. That is what Phase 0 is for, and the go/no-go is fixed in advance.
- That it will replace the lexical cascade. The intended outcome is complementary coverage.
- That it will cover the registry. Coverage is bounded by template availability and by how many
  doctypes are template-like at all — likely well under half.
- That inlier count is a probability. It is a match score; any confidence exposed on the endpoint
  must be calibrated against measured outcomes, or reported as a raw score and labelled as one.

---

## 10. What shipped — the honest artefact, and why an empty registry is a deliverable

**No classifier shipped.** Shipping a 6–8%-precision classifier into a KYC service would be
worse than shipping nothing: it would produce confident, plausible, wrong doctypes on exactly
the confusable families where a wrong doctype is most expensive, and the abstention path — the
thing this service is actually built around — would be bypassed to do it. The stated bar is
precision. Nothing cleared it. So the deliverable is the apparatus and the report.

### 10.1 `POST /api/v1/classify/compare`

Worth building regardless of which avenue eventually goes behind it, because **it is how any
future avenue gets evaluated honestly.** It runs every available avenue over one document and
reports how they relate.

Response: both decision trails in full (`lexical` and `second`, each the *unmodified*
`Classification` that avenue's own endpoint would return), plus a `verdict`:

| verdict | meaning |
|---|---|
| `agree` | both answered, same doctype |
| `disagree` | both answered, different doctypes — at most one is right, possibly neither |
| `one_abstained` | one answered, one abstained |
| `both_abstained` | neither answered; the document goes to a human, as designed |
| `single_avenue` | only one avenue exists — **today, always this** |

**It adjudicates nothing, and that is enforced rather than intended.** There is no fused answer,
no preferred avenue, no tie-break, and no `doctype_id` at the top level of the response — a test
asserts the absence of those keys on the wire, so adding one is a deliberate act with a diff
somebody has to approve. Fusing two channels is where this codebase has produced its worst
defects (the two-channel concurrence rule, the zone-free guard, the jurisdiction veto — each
fixed a real problem and introduced a new one), and a fusion rule must be chosen on data. This
endpoint is how that data gets produced; it is not allowed to pre-empt it.

Two distinctions the vocabulary insists on, because collapsing either would be a lie:

- **`single_avenue` ≠ `one_abstained`.** An avenue that does not exist did not decline to
  answer. Collapsing them would let an empty registry read as a considered refusal.
- **`agree` is not a correctness claim.** Two avenues can agree and both be wrong — `mx_cif` /
  `mx_rfc_csf` above is a pair where agreement is *guaranteed* and correctness is unavailable to
  any classifier. The `detail` string says so in every `agree` response.

### 10.2 `dce/visual/` — a closed registry that is empty on purpose

`AVENUES` is the complete list of second avenues and it contains nothing. An entry there is a
claim that the method cleared 95% precision on genuinely distinct files; adding one requires the
measurement, not the code. The set is closed rather than discovered-by-import so that "somebody
dropped a module in" is not a path to answering KYC classifications.

`RETIRED` carries all four killed methods **in code**, with their AUC, their best precision, the
coverage it bought, and the mechanism. This is not commentary: setting
`visual_method=layout_signature` returns

> *"…was measured against the real corpus on 2026-08-11 and retired: best
> precision-when-answered 0.080 at 18.7% coverage, real-pair AUC 0.846 against a chance of
> 0.500, versus a bar of 0.95. The strongest real-pair signal of the four, and still dead,
> because…"*

rather than an uninformative validation error. The next engineer to reach for one of these ideas
meets the numbers before they spend the week.

Nothing in the package imports `cv2` or `numpy`. The `.[visual]` extra stays undeclared because
no shipped code path needs it — adding an optional dependency for a feature that does not exist
would be carrying CVE surface for nothing.

### 10.3 `/readyz` reports the second avenue **and its coverage**

```json
"second_avenue": {
  "available": false, "method": "", "templates": 0,
  "doctypes_covered": 0, "doctypes_total": 182, "coverage": 0.0,
  "installable": [],
  "retired": ["emblem_match", "layout_signature", "sift_homography", "structure_skeleton"],
  "summary": "no second classification avenue is available: four visual methods … none
              reached the 95% precision bar at any threshold, so none shipped."
}
```

The coverage fields are the point, and they are published *even though every one is zero*. An
operator must never discover by **using** an endpoint that it can only answer for a fraction of
the registry — that is true of a future avenue covering 12 of 182 doctypes, and it is true of
today's avenue covering 0. The denominator comes from the live registry, not a constant, so a
deployment with 121 doctypes loaded is not quoted a fraction of 182.

Absence of an avenue is **not** a readiness failure: the service classifies perfectly well with
one avenue, and 503-ing over the state of the art would be theatre. A deployment that *asked*
for an avenue it cannot have is reported **degraded**, because somebody is expecting a second
opinion that will never arrive.

### 10.4 What was deliberately not built

- **`POST /api/v1/classify/visual`** — there is nothing to put behind it. A route returning
  `503` forever is worse than a route that does not exist: it appears in the OpenAPI schema, in
  integrators' clients, and in a reviewer's mental model of what this service can do.
- **A template index tool under `tools/`** — an index build is only meaningful for a method that
  works. Building provenance and fingerprinting for templates nothing can match is ceremony.
- **The `.[visual]` extra** — see 10.2.
- **`--engine visual` on the corpus harness** — same reason; the harness already reads
  `/classify/compare`'s `lexical` block with no special-casing, which is what the shape was for.

### 10.5 Verification

- **The lexical flow is untouched.** No file under `dce/classify/`, `dce/registry/` or
  `dce/models.py` was modified. The pre-existing suite passes unchanged (771 passed, 4 skipped);
  23 new tests cover the compare surface and the emptiness of the registry.
- **The corpus harness reproduces the baseline exactly**: 158 documents, 150 measured, **117
  correct / 2 wrong / 31 abstained**, 98.3% precision-when-answered, 20.7% abstention — the same
  three numbers as before this change, which is the point.

### 10.6 Recommendation

**Close the visual avenue.** Do not fund a fifth descriptor. The ceiling is informational and it
can be named: the documents that must be told apart are template-identical by construction, and
every visual method must destroy the signal that separates them in order to exist at all.

If a second avenue is still wanted, it must key on something that **varies within one issuer's
form catalogue** and survives OCR failure — which is the property that made the visual idea
attractive in the first place, and which no page-image descriptor turned out to have. The
honest next question is not "which descriptor" but **"is there a second signal at all, or is the
right investment deepening the one that works to 98.3%?"** §5B's third by-product finding —
that the lexical flow's one ID-family error is a confusion both dead visual methods would have
made with high confidence — is evidence for the latter. `/classify/compare` is now in place to
answer it with data the moment a candidate exists.
