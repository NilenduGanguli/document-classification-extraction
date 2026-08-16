# Runbook: making this work on real KYC documents

For the person holding a document that behaved wrongly. Organised by **symptom**, because
that is what you have when you start.

Read [`ARCHITECTURE.md`](ARCHITECTURE.md) once before your first real debugging session; after
that, come straight here.

---

## 0. First, get the facts out of the service

Almost every diagnosis starts with these three. Run them before forming a theory.

**What does the deployment think it is?**
```bash
curl -s localhost:8200/readyz | python3 -m json.tool
```
Look at `ocr.text_layer_policy`, `ocr.configured_providers`, `ocr.trust_boundary` and
`registry.doctypes`.

**How was this document read?**
```bash
curl -s localhost:8200/api/v1/classify/segments \
  -H 'content-type: application/json' \
  -d "{\"ingest\":{\"filename\":\"x.pdf\"},\"content_base64\":\"$(base64 -i x.pdf)\"}" \
  | python3 -m json.tool | head -60
```

**What did the reader actually see, page by page?** This is the single most useful command in
this file, because it answers "is the view right?" before you ask "is the classifier right?":
```bash
python3 - <<'EOF'
from dce.ingest.pipeline import ingest
r = ingest(open('x.pdf','rb').read())
print(r.status, r.text_source, 'truncated=', r.truncated, r.limits_hit)
print('reason:', r.reason or '(none)')
for p in (r.view.pages if r.view else []):
    print(f'  p{p.page}: alnum={p.alnum_chars:5d} adequate={p.text_adequate} '
          f'image={p.image_fraction:.2f} {p.width:.0f}x{p.height:.0f}')
EOF
```

---

## 1. Symptom: HTTP 422 `needs_ocr`

The file carries no usable text and nothing here could read it. **This is a correct answer,
not a failure** — the question is why nothing read it.

Check, in order:

1. **Is a recogniser configured at all?** `/readyz` → `ocr.configured_providers`. Empty means
   no. Set the `DCE_INGEST_*` variables (§7 of ARCHITECTURE).
2. **Did the request decline it?** The reason string says *"this request declined
   recognition"*. The console sends `local_ocr: false` when the read-channel toggle is on
   **lexical** — and that setting is sticky in the URL (`?read=lexical`) across reloads and
   across documents. Switch to `auto`.
3. **Is it the prefix mistake?** `AZURE_DI_ENDPOINT` configures extraction tiers, *not* ingest
   OCR. You need `DCE_INGEST_AZURE_DI_ENDPOINT`. Symptom: T2/T3 work fine, the OCR picker
   shows "not configured here".
   ```bash
   podman exec dce env | grep -i azure | sed -E 's/(KEY|SECRET|TOKEN)=.*/\1=<redacted>/I' | sort
   ```
4. **Is the endpoint reachable from inside the container?**
   ```bash
   podman exec dce python3 -c "import urllib.request;print(urllib.request.urlopen('http://YOUR-HOST:5000',timeout=5).status)"
   ```

---

## 2. Symptom: a document classified as the wrong doctype

**Precision is the property this system exists to protect.** A wrong doctype is a compliance
incident; an abstention is safe. Treat every one of these as serious.

1. **Confirm the view is right first.** Run the per-page dump from §0. If pages are missing or
   `truncated=True` with `unread_pages`, the classifier never saw the document — fix the
   reading, not the registry.
2. **Get the decision trail.** The console's Analyze page shows evidence, contenders and
   margins. Or:
   ```bash
   curl -s localhost:8200/api/v1/classify -d @body.json -H 'content-type: application/json' \
     | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['doctype_id'], d['confidence'], d['margin']); [print(' ', e['tier'], e['detail'], e['weight']) for e in d['evidence']]"
   ```
3. **Look at what fired.** If a *decisive* anchor fired for the wrong doctype, that anchor is
   the bug. Decisive means near-proof; if it is not, it must be demoted. This has happened
   before: `PERMANENT RESIDENT CARD` was decisive for `us_green_card` and matched Canadian PR
   cards.
4. **Check `controls`.** Every decisive anchor declares what justifies it
   (`dce/models.py:222-269`). `class_name_uncontested` is documented as *the tier of no
   evidence yet* — a decisive claim resting on it is weak by construction and is the first
   place to look.

**Fixing it:** demote the anchor to non-decisive, or narrow its text. Then **re-run the whole
corpus**, because anchors interact — a change that fixes one document routinely costs two
others. Do not ship an anchor change without a before/after corpus run.

---

## 3. Symptom: a document abstained that should not have

Abstention is safe but not free. Check in this order:

1. **Zones.** If `text_source` is `native`, `local_ocr` or `ocr_service` **with
   `azure_read`**, every block is `Zone.body` and no title-gated anchor can fire. Three
   doctypes — `us_drivers_license`, `ca_drivers_license`, `ca_nexus` — have *every* decisive
   anchor title-gated, so on those paths they can never be decisively identified. **Use
   `azure_layout`.** This is the single most common cause of a photo ID abstaining.
2. **Coverage.** A low `coverage` in the response means the document's vocabulary barely
   overlaps the doctype's profile — usually a real signal that the registry entry does not
   match this issuer's template.
3. **Margin.** A small `margin` with the right doctype leading means two doctypes are
   genuinely confusable. Look at `runners_up`. The fix is a discriminating anchor, not a lower
   threshold.

**Resist lowering thresholds.** They are what hold precision at 100%. If you lower one, the
corpus run must show precision held.

---

## 4. Symptom: a bundle was not split, or split wrongly

### It came back as one segment

Ask what evidence there was to split on. The three signals are `adequacy`, `geometry` and
`first_page_anchor` (§3 of ARCHITECTURE):

```bash
python3 - <<'EOF'
from dce.ingest.pipeline import ingest
from dce.classify.segments import candidate_boundaries, segment_document
r = ingest(open('bundle.pdf','rb').read())
for b in candidate_boundaries(r.view):
    print(f'  p{b.page} [{b.signal}] {b.detail}')
segs, kept = segment_document(r.view)
for s in segs:
    print(f'  [{s.start_page}-{s.end_page}] {s.doctype_id} {s.confidence:.3f}')
EOF
```

If `candidate_boundaries` is empty, the documents in the bundle are structurally
indistinguishable — same page size, all text-bearing, no first-page markers. **That is the
known blind spot and it is total: measured recall on that shape is 0% (0 of 17 boundaries).**
No threshold change reveals a signal that is not there. Expect it, and do not spend time
hunting for a misconfiguration.

Overall measured recall is **35.3%** with **100% precision** — when it proposes a split, the
split is real; it simply misses seams it cannot see. Run `tools/bundle_recall.py` for the
current numbers and the by-shape breakdown.

Recall is much better when documents *differ physically* (different paper size, one scanned)
or when one carries a form or control number — 75% on mixed-size bundles with anchors. If
your real bundles are mostly same-size typed documents, **segmentation will mostly return one
segment**, and that is the honest limit of the current signals rather than a bug.

If boundaries were proposed but the segments merged, the spans classified the same or one was
unidentifiable and got absorbed (§3 of ARCHITECTURE). Check what each span classifies as on
its own.

### It split a single document

This is the failure mode that matters. Current measured rate: **6.0%** on the corpus, all long
regulatory filings. Report it with the boundary evidence from the command above — the `signal`
field tells you which rule misfired, and the fix belongs in that rule rather than in a
threshold.

**Do not raise recall by weakening the absorption rule.** Emitting unidentifiable spans as
their own documents was measured at **19.3%** false splits versus 6.0% with absorption.

---

### Test bundles you already have

`corpus/bundles/` holds real multi-document files built from corpus documents, with
`bundles.jsonl` declaring which pages each constituent occupies. Drag any onto the console, or
run `.venv/bin/python -m pytest tests/test_bundle_corpus.py`.

| File | Shape | Today |
|---|---|---|
| `bundle_w9_bankstatement` | different page stock | splits at p4 (geometry) |
| `bundle_w9_bankstatement_1040` | three documents | splits at p3 and p5 |
| `bundle_w9_1040_sameshape` | same size, but the 1040 has an OMB number | splits at p3 (anchor) |
| `bundle_bylaws_articles_noanchor` | same size, no marker | **one segment — the blind spot** |
| `bundle_id_and_utility` | photo ID + proof of address | missed today |
| `bundle_crosscountry` | CA + US + MX | finds 1 of 2 |
| `single_w9_control` | not a bundle | one segment, `segmented=false` |

**Do not add bundles to `corpus/<cc>/manifest.jsonl`.** That manifest maps one file to one
`expected_doctype` and feeds the precision figure; a bundle there is scored as a wrong answer
per bundle. Regenerate with `tools/make_bundles.py`. If you add a test that walks corpus PDFs,
exclude `corpus/bundles/` — one already had to be fixed for exactly this.

---

## 5. Symptom: extraction returns nothing, or wrong values

1. `t1_local` runs against the doctype's locators. Empty usually means the locators do not
   match this issuer's layout — expected on a new template.
2. Check the field's `type`. A `type="bool"` field returning a title fragment was a real bug
   (`_coerce_bool` in `dce/extract/resolve.py`); if you see a value that is obviously a label
   rather than a value, the locator matched the caption.
3. **T3 timing out at 120 s** is hardcoded (`azure_specialist.py:120`). Long filings exceed
   it. Note that a client-side timeout does **not** cancel the Azure job — the pages may still
   be billed.
4. On a bundle, extraction runs **per segment**, against that segment's pages only. If a field
   lives on a page the segmentation put in a different segment, it will not be found. Check
   the page ranges first.

---

## 6. Adding a doctype

1. Add the spec to the right pack under `dce/registry/`.
2. Anchors: prefer **specific** strings. A decisive anchor must declare `controls` justifying
   the claim, and the loader enforces this (`dce/registry/loader.py:350`).
3. `class_name_uncontested` is a known-weak tier and the loader forbids *any* other doctype
   from declaring the same string — the pack fails to import if two collide. That is the
   design working.
4. Add a corpus document and a registry test.
5. **Run the full corpus.** A new doctype changes the IDF profile and can move documents that
   have nothing to do with it.

```bash
.venv/bin/python -m pytest -q
.venv/bin/python tools/corpus_test.py --ingest --url http://localhost:8200 --verbose
```

---

## 7. Tuning a threshold, honestly

Every threshold here was set by measurement, and each has a counter-example behind it. Before
changing one:

1. **Capture a before baseline** with the *exact* command you will re-run.
2. **Rebuild the container** — it mounts no source, so a host edit does not reach a running
   service. Without this the two runs are identical for the wrong reason.
3. Use `--ingest`, or the harness reads PDFs itself and never exercises `dce/ingest/pdf.py`.
4. Re-run and compare **precision first**, then accuracy. Precision is the property; accuracy
   is the ambition.

```bash
podman build -t dce:local . && podman restart dce   # or your compose equivalent
podman exec dce python3 -c "import dce.ingest.pdf as m; print(hasattr(m,'PageVerdict'))"
```

That last line is the check that the rebuild actually took. It has caught a silent no-op
before.

---

## 8. When real KYC documents arrive — the order I would work in

The corpus this was built against has **zero real bundles** and is skewed toward long
regulatory filings. Almost every open question is answered by better material, not better code.

1. **Add 3–5 real bundles** (typed cover page + scanned attachments; two or three documents
   concatenated). Every segmentation number in these docs is measured on synthetic joins and
   long filings — the least favourable material available. This is the highest-value thing you
   can do.
2. **Add mixed documents** — part text, part scan. The corpus has none, so the ingest fix is
   proven only by unit fixtures.
3. **Re-measure the false-split rate** on that material. If it is materially better than 6%,
   the console default is settled. If worse, make segmentation a toggle rather than the
   default.
4. **Measure on `azure_layout`.** Every number in these documents rides the local text-layer
   path. With DI the inputs change materially — zones exist, so title-gated anchors fire that
   cannot fire locally; `angle` is real; marks and key-values are populated. Page-scope
   precision may be much better there. **It has never been measured.**
5. **Only then** consider tuning thresholds. Three attempts have already been made and
   measured, and all three were reverted for costing precision.

---

## 9. What to collect when something is wrong

Paste these into the report. They are what makes a problem diagnosable by somebody who does
not have the document.

- `/readyz` output (redact keys)
- the per-page dump from §0
- `status`, `text_source`, `truncated`, `limits_hit`, `reason`
- for a classification problem: `doctype_id`, `confidence`, `margin`, `coverage`,
  `runners_up`, and the `evidence` list
- for a segmentation problem: the `candidate_boundaries` output, including each `signal`
- the container's image id and whether it was rebuilt after the last source change

**Never paste the document itself, or extracted field values, into a shared channel.** The
per-page dump and the decision trail are enough to diagnose almost everything and contain no
customer data.
