# Fixing the six segmentation defects

**Status:** proposed, for review. Nothing implemented.
**Date:** 2026-08-16
**Companion to:** `2026-08-16-segmentation-design.md`

These are live bugs in `dce/classify/cascade.py` — exported code with a passing test. They
are worth fixing whether or not segmentation ships, because `classify_pages` is importable
today and anything that calls it inherits all six.

Ordered by severity. D1 and D2 are silent-data-loss bugs; D3–D5 are correctness-of-reporting;
D6 is why none of them were caught.

---

## D1 — a page can vanish from the bundle entirely

**Severity: highest.** Silent data loss with no signal a caller can detect.

`_page_numbers()` (`cascade.py:1859`) builds the page set from `pages`, `blocks` and `tables`
and omits `marks` and `key_values`:

```python
pages = {p.page for p in view.pages}
pages.update(b.page for b in view.blocks)
pages.update(t.page for t in view.tables)
return sorted(pages)
```

A page whose only content is a checkbox and a provider-detected key/value pair — **exactly
what a scanned form page looks like on the Azure path** — is never enumerated, never
classified, and appears in no segment. Measured: a payload with page 1 (blocks) and page 2
(one `Mark`, one `KeyValue`) returns `[1]`, and `classify_pages` returns a single segment
`(1, 1)`. Page 2 is gone.

**Fix**

```python
def _page_numbers(view: LayoutView) -> list[int]:
    """Ordered page numbers present in the payload.

    Every page-bearing collection, not only the three that carry prose. A page whose only
    content is a checkbox and a key/value pair is a scanned form page, and omitting it here
    deletes it from every downstream segment with nothing to indicate it was ever there.
    """
    pages = {p.page for p in view.pages}
    for collection in (view.blocks, view.tables, view.marks, view.key_values):
        pages.update(item.page for item in collection)
    return sorted(pages)
```

**Verification** — a view whose page 2 holds only a `Mark` and a `KeyValue`:
`_page_numbers(view) == [1, 2]`, and `classify_pages` returns a segment covering page 2.

**Risk:** low. Strictly widens the page set. A page that now appears will classify as
`unknown` if it holds no anchors, which is the honest answer and better than absence.

---

## D2 — a gap in page numbering is bridged into one segment

The run-length loop (`cascade.py:795`) compares **list index positions**, never page numbers:

```python
ends_run = i == len(per_page) or (
    per_page[i][1].doctype_id != per_page[run_start][1].doctype_id
)
```

A bundle whose page 3 is absent yields one segment reporting `start_page=1, end_page=6,
page_count=6` while `len(classification.page_types) == 5`. The segment claims six pages and
holds five.

**Fix** — break a run at a discontinuity as well as at a doctype change:

```python
ends_run = (
    i == len(per_page)
    or per_page[i][1].doctype_id != per_page[run_start][1].doctype_id
    # A gap in the numbering is a break in the document, whatever the doctypes say. Pages
    # 1-2 and 4-6 of the same class are two runs, because page 3 is not evidence either way
    # and a segment that claims it would be claiming a page it never saw.
    or per_page[i][0] != per_page[i - 1][0] + 1
)
```

**Verification** — pages `[1, 2, 4, 5]` all classifying alike yield two segments, `(1,2)` and
`(4,5)`, and for every segment `page_count == len(classification.page_types)`. That equality
is worth asserting as a permanent invariant.

**Risk:** low. Only affects payloads with non-contiguous pages, which today are silently
misreported.

---

## D3 + D4 — the page slicer loses provenance and aliases its parent

`_page_view` (`cascade.py:1867`) has three problems, best fixed together since they are one
function:

1. **`raw` is dropped.** The rebuilt `LayoutView` takes no `raw=`, so
   `{'provider': 'azure_layout', …}` becomes `{}`. That dict is what `routes.py:1994` reads
   to report which OCR provider read the document — so any per-segment API response built on
   these slices silently loses attribution.
2. **Blocks are shared, not copied.** The slice holds the *same* `TextBlock` objects as the
   parent; mutating a slice mutates the bundle. `classify()` is safe today only because
   `_unpromoted_view` deep-copies before rewriting zones. It is a latent hazard for any new
   caller.
3. **It is single-page only**, and segmentation needs ranges.

**Fix** — one public range slicer, deep-copying, preserving `raw`:

```python
def page_range_view(view: LayoutView, start: int, end: int) -> LayoutView:
    """The pages ``start``..``end`` (inclusive, 1-based) as a standalone view.

    Deep-copied on purpose: the single-page slicer this replaces shared TextBlock objects
    with its parent, so mutating a slice mutated the bundle. Nothing relied on that, and
    anything that came to rely on it would be a bug nobody could see.

    ``raw`` is carried through because it is where provider provenance lives, and a segment
    that cannot say which recogniser read it is not auditable.

    **Page numbers stay absolute.** A slice of pages 4-6 reports pages 4, 5 and 6, not 1, 2
    and 3. `page 4` in a response must mean page 4 of the file the caller uploaded; renumbering
    would make every segment's page references true only relative to a slice the caller never
    sees.
    """
    def keep(page: int) -> bool:
        return start <= page <= end

    return LayoutView(
        doc_id=f"{view.doc_id}#p{start}-{end}" if view.doc_id else "",
        pages=[p.model_copy(deep=True) for p in view.pages if keep(p.page)],
        blocks=[b.model_copy(deep=True) for b in view.blocks if keep(b.page)],
        tables=[t.model_copy(deep=True) for t in view.tables if keep(t.page)],
        marks=[m.model_copy(deep=True) for m in view.marks if keep(m.page)],
        key_values=[kv.model_copy(deep=True) for kv in view.key_values if keep(kv.page)],
        languages=list(view.languages),
        raw=dict(view.raw),
    )


def _page_view(view: LayoutView, page: int) -> LayoutView:
    return page_range_view(view, page, page)
```

**Verification**
- `page_range_view(view, 2, 2).raw == view.raw` — provenance survives.
- Mutating a slice's block text leaves the parent unchanged.
- `page_range_view(view, 4, 6)` reports pages `[4, 5, 6]`.
- The existing single-page behaviour is unchanged apart from `raw` and the copying.

**Risk:** low, one caveat. Deep-copying every block costs memory on a 183-page filing. It
runs once per span under the new architecture, not once per page, so the cost is bounded by
span count — but worth measuring on the largest corpus document rather than assuming.

**Decision recorded:** page numbers stay absolute. The alternative — renumbering to 1..N —
was considered and rejected: it makes a segment's page references meaningless outside the
slice, and `classify()` reads only `len(view.pages)`, so nothing needs them contiguous.

---

## D5 — a segment reports page 1's evidence as its own

`Segment.classification` is a deep copy of the **first page's** `Classification` with two
fields overwritten (`cascade.py:801-804`). Everything else — `evidence`, `runners_up`,
`margin`, `coverage`, `reason` — belongs to page 1 and is presented as the segment's.
Meanwhile `confidence` is the **arithmetic mean** across the run.

So a reviewer reading a 6-page segment's audit trail sees page 1's evidence and page 1's
margin next to a confidence derived from all six. The two halves of that record do not
describe the same thing.

**The real fix is architectural.** Under the design's boundary-first approach each span is
classified *whole*, so `classification` becomes genuinely the segment's and this defect
disappears rather than being patched.

**Until then**, two changes make the current record honest:

1. **Report the minimum, not the mean.** A segment is only as trustworthy as its weakest
   page; a mean lets one strong page carry four weak ones into a confident-looking number.
   For a system whose whole posture is abstain-rather-than-guess, min is the aggregate that
   matches.
2. **Carry the per-page classifications** rather than implying one stands for all:

```python
    #: Every page's own classification, in page order. The segment's `classification` is
    #: page 1's, and its evidence/margin/coverage describe page 1 alone — this is where a
    #: reviewer looks for the rest.
    page_classifications: tuple[Classification, ...] = ()
```

And the docstring must stop saying `classification` is the run's; it is page 1's.

**Verification** — a run whose pages score `[0.9, 0.4, 0.85]` reports `confidence == 0.4`,
not `0.717`, and `len(page_classifications) == 3`.

**Risk:** moderate — this changes a reported number. Any consumer comparing segment
confidence against a threshold sees lower values. Since no route calls `classify_pages`,
today there are no such consumers.

---

## D6 — the test measures a system that is not the one that ships

`test_classify_pages_run_length_aggregates_a_merged_bundle` (`test_classify.py:832`) runs
against `registry()`, a **7-doctype fixture** whose ids — `passport`, `bank_statement`,
`driver_license` — are not real registry ids at all (the real ones are `us_passport`,
`us_drivers_license`). Against the real 129-doctype registry the same payload produces a
different answer.

The test passes. It has never exercised the shipped system, which is why 86% mis-segmentation
went unnoticed.

**Fix** — keep the fixture test (it pins run-length mechanics in isolation, which is
legitimate) and add a **characterisation test against the real registry** that records the
true behaviour:

```python
def test_classify_pages_against_the_real_registry_is_not_yet_trustworthy():
    """Pins the measured reality so the architecture change has a baseline to beat.

    Against the real registry, page-scope classification scores 71.3% precision where
    whole-document classification scores 100%. This test exists so that number cannot drift
    unnoticed and so the boundary-first rewrite has something to be measured against — NOT
    because the behaviour is acceptable.
    """
```

Plus a corpus-scale check in `tools/` reporting segments-emitted against
documents-that-are-actually-one — the 791-vs-78 number — so the ratio is tracked rather than
rediscovered.

**Risk:** none. Tests only.

---

## Ordering

```
D1 ─┐
D2 ─┼─> independent, land together      (silent data loss; no API surface)
D3 ─┤
D4 ─┘

D5 ──> after D3/D4                      (changes a reported number)
D6 ──> last, and again after the rewrite (needs the others to be stable)
```

**D1–D4 are safe to land now.** They are strictly corrective, touch no route, and change no
response shape — `classify_pages` is called by nothing. They also make the boundary-first
architecture buildable: a range slicer that preserves `raw` and does not alias is a
prerequisite for span classification.

**D5 is best folded into the architecture change**, where it resolves rather than gets
patched. If the architecture is not imminent, take the interim fix — the current record is
actively misleading in an audit trail.

**D6 last**, because a characterisation test written before D1–D4 would pin behaviour that is
about to change.

---

## What this does not fix

Fixing all six leaves page-scope precision at roughly 71.3%. **These are correctness bugs in
the machinery, not the reason segmentation mis-segments.** That cause is that the classifier
was never calibrated for page-scope input, and the remedy is the boundary-first architecture,
not these fixes.

Landing D1–D4 makes the machinery sound and unblocks that work. It does not make
`classify_pages` shippable, and no endpoint should be wired to it on the strength of these
fixes alone.
