#!/usr/bin/env python3
"""Segmentation RECALL: synthetic KYC bundles built from real corpus documents.

`corpus_test.py` measures the opposite half of segmentation. Every corpus file is a *single*
document, so every split it sees is a false positive, and the numbers it reports — 19.3% false
splits before the absorb/merge rules, 0.08 splits per document after — are all about
precision. Nothing measured whether `segment_document` finds a seam that is really there,
because the corpus contains no file that is really two documents.

This harness builds those files. Real corpus documents are sliced to their first 1-3 pages —
a KYC bundle's shape, unlike the corpus's 100-page filings — and concatenated with PyMuPDF,
which preserves each source's own page geometry, text layer and images. The bundle bytes then
go through the same `dce.ingest.pipeline.ingest` the service uses, so the page verdicts
(alnum floor, glyph sanity, image dominance) that `segment_document` reads are the real ones
rather than a fixture's. Ground truth is the concatenation order: slices of length (2, 3, 2)
give true boundaries at pages 3 and 6.

Deliberately covered, because recall is not uniform across them and an average would hide it:

* documents of the same page size against documents of different page sizes;
* documents that carry a first-page-only anchor (a form number, a control number) against the
  large majority of the registry that does not;
* text-layer documents against the corpus's genuinely scanned ones.

Two controls keep this a measurement of SEGMENTATION rather than of classification:

* every slice is ALSO ingested and classified alone, so a span the segmenter located
  perfectly but whose 2-page slice does not classify to its manifest doctype is scored apart
  from one the segmenter mis-located;
* boundary precision is reported alongside recall, since the two trade directly and a recall
  number on its own can always be improved by splitting more.

Offline and deterministic: local OCR and the OCR services are declined explicitly, so the
harness needs no network and no credentials, and two runs on one machine agree.

    python tools/bundle_recall.py
    python tools/bundle_recall.py --json reports/bundle-recall.json --verbose
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - the harness is useless without it
    sys.exit("PyMuPDF is required: pip install pymupdf")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dce.classify.cascade import classify, load_registry  # noqa: E402
from dce.classify.profiles import build_profiles  # noqa: E402
from dce.classify.segments import candidate_boundaries, segment_document  # noqa: E402
from dce.ingest.pipeline import ingest  # noqa: E402
from dce.ingest.result import IngestStatus  # noqa: E402
from dce.models import UNKNOWN  # noqa: E402


@dataclass(frozen=True)
class Source:
    """One slice of one corpus document.

    ``geom``, ``read`` and ``anchor`` are the bundle-shape axes, recorded so recall can be
    reported conditioned on them. They describe the SOURCE, and are never used to score —
    scoring reads only what the service produced.
    """

    key: str
    rel: str
    doctype: str
    pages: int
    geom: str
    read: str  # "text" | "scan"
    anchor: bool  # page 1 carries a FIRST_PAGE_CONTROLS marker (verified, not assumed)


SOURCES: dict[str, Source] = {
    s.key: s
    for s in [
        # --- US ---------------------------------------------------------------
        Source("us_w9", "corpus/us/us_w9.pdf", "us_w9", 3, "letterP", "text", False),
        Source("us_w9_2", "corpus/us/us_w9.pdf", "us_w9", 2, "letterP", "text", False),
        Source("us_w8ben", "corpus/us/us_w8ben.pdf", "us_w8ben", 1, "letterP", "text", False),
        Source("us_1040", "corpus/us/us_1040.pdf", "us_1040", 2, "letterP", "text", True),
        Source("us_bank_statement", "corpus/us/us_bank_statement.pdf", "us_bank_statement",
               3, "letterL", "text", False),
        Source("us_bank_statement_2", "corpus/us/us_bank_statement.pdf", "us_bank_statement",
               2, "letterL", "text", False),
        Source("us_paystub", "corpus/us/us_paystub.pdf", "us_paystub", 1,
               "letterL", "text", False),
        Source("us_utility_bill", "corpus/us/us_utility_bill.pdf", "us_utility_bill", 3,
               "letterP", "text", False),
        Source("us_utility_bill_2", "corpus/us/us_utility_bill.pdf", "us_utility_bill", 2,
               "letterP", "text", False),
        Source("us_mortgage_statement", "corpus/us/us_mortgage_statement.pdf",
               "us_mortgage_statement", 2, "letterP", "scan", False),
        Source("us_green_card", "corpus/us/us_green_card.pdf", "us_green_card", 1,
               "letterP", "text", False),
        Source("us_ead", "corpus/us/us_ead.pdf", "us_ead", 1, "letterP", "text", False),
        Source("us_drivers_license", "corpus/us/us_drivers_license.pdf", "us_drivers_license",
               2, "letterP", "text", False),
        Source("us_state_id", "corpus/us/us_state_id.pdf", "us_state_id", 2,
               "letterP", "text", False),
        Source("us_bylaws", "corpus/us/us_bylaws.pdf", "us_bylaws", 2,
               "letterP", "text", False),
        Source("us_articles_incorporation", "corpus/us/us_articles_incorporation.pdf",
               "us_articles_incorporation", 2, "letterP", "text", False),
        Source("us_certificate_good_standing", "corpus/us/us_certificate_good_standing.pdf",
               "us_certificate_good_standing", 1, "letterP", "scan", False),
        Source("us_sec_form3", "corpus/us/us_sec_form3.pdf", "us_sec_form3", 2,
               "letterL", "text", True),
        Source("us_sec_form4", "corpus/us/us_sec_form4.pdf", "us_sec_form4", 2,
               "letterL", "text", True),
        Source("us_passport", "corpus/us/us_passport.jpg", "us_passport", 1,
               "photo", "scan", False),
        # --- CA ---------------------------------------------------------------
        Source("ca_t4", "corpus/ca/ca_t4.pdf", "ca_t4", 2, "letterP", "text", False),
        Source("ca_t1_general", "corpus/ca/ca_t1_general.pdf", "ca_t1_general", 2,
               "letterP", "text", False),
        Source("ca_sin_confirmation", "corpus/ca/ca_sin_confirmation.pdf",
               "ca_sin_confirmation", 2, "letterP", "text", False),
        Source("ca_cra_noa", "corpus/ca/ca_cra_noa.pdf", "ca_cra_noa", 2,
               "letterP", "text", False),
        Source("ca_bn_letter", "corpus/ca/ca_bn_letter.pdf", "ca_bn_letter", 2,
               "letterP", "text", False),
        Source("ca_lease_agreement", "corpus/ca/ca_lease_agreement.pdf", "ca_lease_agreement",
               2, "letterP", "text", False),
        Source("ca_pr_card", "corpus/ca/ca_pr_card.pdf", "ca_pr_card", 1,
               "card", "scan", False),
        Source("ca_copr", "corpus/ca/ca_copr.pdf", "ca_copr", 1, "halfsheet", "text", False),
        Source("ca_property_tax_assessment", "corpus/ca/ca_property_tax_assessment.pdf",
               "ca_property_tax_assessment", 2, "legal", "text", False),
        Source("ca_mda", "corpus/ca/ca_mda.pdf", "ca_mda", 2, "letterP", "text", True),
        Source("ca_ni_52_109_certification", "corpus/ca/ca_ni_52_109_certification.pdf",
               "ca_ni_52_109_certification", 2, "letterP", "text", True),
        Source("ca_mcr_blank", "corpus/ca/ca_material_change_report__blank_form.pdf",
               "ca_material_change_report", 2, "letterP", "text", True),
        Source("ca_prospectus", "corpus/ca/ca_prospectus.pdf", "ca_prospectus", 2,
               "letterP", "text", False),
        # --- MX / XX -----------------------------------------------------------
        Source("mx_ine", "corpus/mx/mx_ine.pdf", "mx_ine", 2, "letterP", "text", False),
        Source("mx_rfc_csf", "corpus/mx/mx_rfc_csf.pdf", "mx_rfc_csf", 3,
               "letterP", "text", False),
        Source("mx_rfc_csf_2", "corpus/mx/mx_rfc_csf.pdf", "mx_rfc_csf", 2,
               "letterP", "text", False),
        Source("mx_cif", "corpus/mx/mx_cif.pdf", "mx_cif", 1, "letterP", "text", False),
        Source("mx_opinion", "corpus/mx/mx_opinion_cumplimiento.pdf",
               "mx_opinion_cumplimiento", 1, "letterP", "text", False),
        Source("mx_curp", "corpus/mx/mx_curp_constancia.pdf", "mx_curp_constancia", 1,
               "wide", "text", False),
        Source("mx_acta_nacimiento", "corpus/mx/mx_acta_nacimiento.pdf", "mx_acta_nacimiento",
               1, "wide", "scan", False),
        Source("mx_acta_asamblea", "corpus/mx/mx_acta_asamblea.pdf", "mx_acta_asamblea", 2,
               "letterP", "text", False),
        Source("mx_aviso", "corpus/mx/mx_aviso_privacidad.pdf", "mx_aviso_privacidad", 2,
               "a4", "text", False),
        Source("mx_informe_comisario", "corpus/mx/mx_informe_comisario.pdf",
               "mx_informe_comisario", 2, "a4", "scan", False),
        Source("xx_ubo", "corpus/mx/xx_ubo_declaration.pdf", "xx_ubo_declaration", 2,
               "a4", "text", False),
        Source("xx_duns", "corpus/mx/xx_duns_record.pdf", "xx_duns_record", 2,
               "a4", "text", False),
        Source("xx_fatca", "corpus/mx/xx_fatca_crs_self_certification.pdf",
               "xx_fatca_crs_self_certification", 2, "letterP", "text", False),
        Source("xx_coi", "corpus/mx/xx_certificate_of_insurance.pdf",
               "xx_certificate_of_insurance", 2, "letterP", "text", False),
    ]
}

#: Bundles, as ordered lists of source keys. Two-, three- and four-document shapes, chosen so
#: that every cell of (same/different page size) x (with/without first-page anchor) x
#: (text/scan) is populated — including the cell where no signal can possibly fire, which is
#: the one an easier bundle set would quietly omit.
BUNDLES: list[tuple[str, list[str]]] = [
    ("B01_us_w9+bank", ["us_w9", "us_bank_statement"]),
    ("B02_us_1040+w9", ["us_1040", "us_w9"]),
    ("B03_us_w9+1040", ["us_w9", "us_1040"]),
    ("B04_us_dl+bank", ["us_drivers_license", "us_bank_statement"]),
    ("B05_us_greencard+utility", ["us_green_card", "us_utility_bill"]),
    ("B06_ca_t4+noa", ["ca_t4", "ca_cra_noa"]),
    ("B07_ca_prcard+t1", ["ca_pr_card", "ca_t1_general"]),
    ("B08_ca_proptax+sin", ["ca_property_tax_assessment", "ca_sin_confirmation"]),
    ("B09_mx_ine+rfc", ["mx_ine", "mx_rfc_csf"]),
    ("B10_mx_curp+opinion", ["mx_curp", "mx_opinion"]),
    ("B11_a4_aviso+ubo", ["mx_aviso", "xx_ubo"]),
    ("B12_us_w9+passport", ["us_w9", "us_passport"]),
    ("B13_us_1040+w9+bank", ["us_1040", "us_w9", "us_bank_statement_2"]),
    ("B14_us_gc+dl+utility", ["us_green_card", "us_drivers_license", "us_utility_bill_2"]),
    ("B15_us_w8ben+w9+1040", ["us_w8ben", "us_w9", "us_1040"]),
    ("B16_ca_t4+mda+lease", ["ca_t4", "ca_mda", "ca_lease_agreement"]),
    ("B17_ca_copr+t1+noa", ["ca_copr", "ca_t1_general", "ca_cra_noa"]),
    ("B18_ca_sin+bn+t4", ["ca_sin_confirmation", "ca_bn_letter", "ca_t4"]),
    ("B19_mx_ine+cif+asamblea", ["mx_ine", "mx_cif", "mx_acta_asamblea"]),
    ("B20_mx_acta+curp+rfc", ["mx_acta_nacimiento", "mx_curp", "mx_rfc_csf_2"]),
    ("B21_a4_ubo+comisario+aviso", ["xx_ubo", "mx_informe_comisario", "mx_aviso"]),
    ("B22_us_paystub+utility+bank", ["us_paystub", "us_utility_bill_2",
                                     "us_bank_statement_2"]),
    ("B23_us_mortgage+w9+bank", ["us_mortgage_statement", "us_w9_2", "us_bank_statement_2"]),
    ("B24_ca_52109+mcr+prospectus", ["ca_ni_52_109_certification", "ca_mcr_blank",
                                     "ca_prospectus"]),
    ("B25_us_1040+w9+bank+dl", ["us_1040", "us_w9_2", "us_bank_statement_2",
                                "us_drivers_license"]),
    ("B26_us_gc+ead+stateid+utility", ["us_green_card", "us_ead", "us_state_id",
                                       "us_utility_bill_2"]),
    ("B27_ca_prcard+copr+t1+noa", ["ca_pr_card", "ca_copr", "ca_t1_general", "ca_cra_noa"]),
    ("B28_ca_t4+sin+bn+lease", ["ca_t4", "ca_sin_confirmation", "ca_bn_letter",
                                "ca_lease_agreement"]),
    ("B29_mx_ine+curp+rfc+opinion", ["mx_ine", "mx_curp", "mx_rfc_csf_2", "mx_opinion"]),
    ("B30_a4_aviso+ubo+duns+comisario", ["mx_aviso", "xx_ubo", "xx_duns",
                                         "mx_informe_comisario"]),
    ("B31_xc_passport+w9+bank+t4", ["us_passport", "us_w9_2", "us_bank_statement_2", "ca_t4"]),
    ("B32_xc_dl+t4+ine+ubo", ["us_drivers_license", "ca_t4", "mx_ine", "xx_ubo"]),
    ("B33_us_form3+form4+w9+1040", ["us_sec_form3", "us_sec_form4", "us_w9_2", "us_1040"]),
    ("B34_us_cgs+bylaws+articles+w9", ["us_certificate_good_standing", "us_bylaws",
                                       "us_articles_incorporation", "us_w9_2"]),
    ("B35_xx_fatca+coi", ["xx_fatca", "xx_coi"]),
    ("B36_mx_comisario+asamblea", ["mx_informe_comisario", "mx_acta_asamblea"]),
]


# ---------------------------------------------------------------------------
# Building bundles
# ---------------------------------------------------------------------------
def slice_bytes(src: Source, root: Path) -> bytes:
    """The first ``src.pages`` pages of ``src``, as a standalone PDF.

    ``insert_pdf`` copies pages whole, so each slice keeps its own page box, its own text
    layer and its own images — which is the point. Rasterising or re-laying-out would destroy
    exactly the geometry and adequacy differences segmentation reads.
    """
    path = root / src.rel
    doc = fitz.open(str(path))
    if path.suffix.lower() != ".pdf":
        converted = fitz.open("pdf", doc.convert_to_pdf())
        doc.close()
        doc = converted
    out = fitz.open()
    out.insert_pdf(doc, from_page=0, to_page=min(src.pages, doc.page_count) - 1)
    data = out.tobytes()
    out.close()
    doc.close()
    return data


def bundle_bytes(keys: list[str], root: Path) -> tuple[bytes, list[tuple[int, int, Source]]]:
    """Concatenate slices; return the bytes and the true ``(start, end, source)`` spans."""
    out = fitz.open()
    spans: list[tuple[int, int, Source]] = []
    cursor = 1
    for key in keys:
        src = SOURCES[key]
        piece = fitz.open("pdf", slice_bytes(src, root))
        out.insert_pdf(piece)
        spans.append((cursor, cursor + piece.page_count - 1, src))
        cursor += piece.page_count
        piece.close()
    data = out.tobytes()
    out.close()
    return data, spans


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
@dataclass
class BundleResult:
    name: str
    keys: list[str]
    n_docs: int
    n_pages: int
    shape: str
    true_boundaries: list[int]
    true_spans: list[tuple[int, int, str]]
    pred_boundaries: list[int] = field(default_factory=list)
    pred_spans: list[tuple[int, int, str]] = field(default_factory=list)
    proposals: dict[int, str] = field(default_factory=dict)
    verdict: str = ""
    note: str = ""

    @property
    def found(self) -> set[int]:
        return set(self.pred_boundaries) & set(self.true_boundaries)


def shape_of(spans: list[tuple[int, int, Source]]) -> str:
    geoms = {s.geom for _, _, s in spans}
    reads = {s.read for _, _, s in spans}
    anchors = [s.anchor for _, _, s in spans]
    return "/".join((
        "same-size" if len(geoms) == 1 else "mixed-size",
        "all-text" if reads == {"text"} else "all-scan" if reads == {"scan"} else "text+scan",
        "anchors:none" if not any(anchors)
        else "anchors:all" if all(anchors) else "anchors:some",
    ))


def run(root: Path, verbose: bool) -> dict:
    specs = load_registry()
    if not specs:
        sys.exit("no doctype registry loaded — segmentation cannot be measured against it")
    profiles = build_profiles(specs)

    # Control: what each slice classifies to on its own. A span the segmenter located
    # perfectly can still carry the wrong doctype because a 2-page slice is thin evidence,
    # and that is a classification result, not a segmentation one.
    standalone: dict[str, str] = {}
    for key, src in SOURCES.items():
        res = ingest(slice_bytes(src, root), doc_id=key, local_ocr=False, ocr_service=False)
        if res.status is not IngestStatus.ok or res.view is None:
            standalone[key] = "<needs_ocr>"
            continue
        verdict = classify(res.view, specs, profiles=profiles)
        standalone[key] = UNKNOWN if verdict.abstained else verdict.doctype_id

    results: list[BundleResult] = []
    for name, keys in BUNDLES:
        data, spans = bundle_bytes(keys, root)
        result = BundleResult(
            name=name,
            keys=keys,
            n_docs=len(keys),
            n_pages=spans[-1][1],
            shape=shape_of(spans),
            true_boundaries=[a for a, _, _ in spans[1:]],
            true_spans=[(a, b, s.doctype) for a, b, s in spans],
        )
        res = ingest(data, doc_id=name, local_ocr=False, ocr_service=False)
        if res.status is not IngestStatus.ok or res.view is None:
            result.verdict = "INGEST_FAILED"
            result.note = res.reason
            results.append(result)
            continue
        if res.truncated:
            result.note = f"truncated ({', '.join(res.limits_hit)})"

        segments, _ = segment_document(res.view, specs, profiles=profiles)
        result.pred_boundaries = [s.start_page for s in segments][1:]
        result.pred_spans = [(s.start_page, s.end_page, s.doctype_id) for s in segments]
        result.proposals = {b.page: b.signal for b in candidate_boundaries(res.view, specs)}

        if set(result.pred_boundaries) == set(result.true_boundaries):
            result.verdict = "EXACT"
        elif result.found:
            result.verdict = "PARTIAL"
        elif len(segments) == 1:
            result.verdict = "ONE_SEGMENT"
        else:
            result.verdict = "MISSED"
        results.append(result)

    return report(results, standalone, verbose)


def report(results: list[BundleResult], standalone: dict[str, str], verbose: bool) -> dict:
    scored = [r for r in results if r.verdict != "INGEST_FAILED"]
    true_total = sum(len(r.true_boundaries) for r in scored)
    pred_total = sum(len(r.pred_boundaries) for r in scored)
    found_total = sum(len(r.found) for r in scored)

    verdicts = Counter(r.verdict for r in results)
    found_by_signal = Counter()
    proposed_by_signal = Counter()
    killed_by_signal = Counter()
    false_split_by_signal = Counter()
    internal_proposals = Counter()
    missed_unseen = 0
    shapes: dict[str, Counter] = {}

    segments_total = exact_range = exact_and_manifest = exact_and_standalone = 0
    pages_total = pages_right = 0

    for r in scored:
        bucket = shapes.setdefault(r.shape, Counter())
        bucket["bundles"] += 1
        bucket["true"] += len(r.true_boundaries)
        bucket["found"] += len(r.found)
        for b in r.true_boundaries:
            signal = r.proposals.get(b)
            if signal is None:
                missed_unseen += 1
                continue
            proposed_by_signal[signal] += 1
            if b in r.pred_boundaries:
                found_by_signal[signal] += 1
            else:
                killed_by_signal[signal] += 1
        for page, signal in r.proposals.items():
            if page not in r.true_boundaries:
                internal_proposals[signal] += 1
                if page in r.pred_boundaries:
                    false_split_by_signal[signal] += 1

        truth = {(a, b): d for a, b, d in r.true_spans}
        by_key = dict(zip(r.keys, [(a, b) for a, b, _ in r.true_spans], strict=True))
        for a, b, doctype in r.pred_spans:
            segments_total += 1
            if (a, b) in truth:
                exact_range += 1
                exact_and_manifest += doctype == truth[(a, b)]
                key = next(k for k, rng in by_key.items() if rng == (a, b))
                exact_and_standalone += doctype == standalone.get(key)
        page_truth = {p: d for a, b, d in r.true_spans for p in range(a, b + 1)}
        pages_total += r.n_pages
        for a, b, doctype in r.pred_spans:
            pages_right += sum(1 for p in range(a, b + 1) if page_truth.get(p) == doctype)

    if verbose:
        print("=" * 100)
        print("PER-BUNDLE")
        print("=" * 100)
        for r in results:
            print(f"\n{r.name}  [{r.n_docs} docs, {r.n_pages} pages]  {r.shape}")
            print(f"  true : {r.true_spans}")
            print(f"  pred : {r.pred_spans}")
            if r.note:
                print(f"  note : {r.note}")
            print(f"  {r.verdict}")
            for b in r.true_boundaries:
                sig = r.proposals.get(b)
                if b in r.pred_boundaries:
                    print(f"    + p{b} found via {sig}")
                elif sig:
                    print(f"    - p{b} proposed by {sig}, then absorbed or merged away")
                else:
                    print(f"    - p{b} no signal fired")
            for page, sig in sorted(r.proposals.items()):
                if page not in r.true_boundaries:
                    fate = "SURVIVED as a false split" if page in r.pred_boundaries \
                        else "killed downstream"
                    print(f"    ~ p{page} proposed by {sig} INSIDE a document: {fate}")

    print("\n" + "=" * 100)
    print(f"AGGREGATE — {len(scored)} bundles measured, {len(results) - len(scored)} unusable")
    print("=" * 100)
    print(f"boundary recall            : {found_total}/{true_total} = "
          f"{found_total / true_total:.1%}")
    print(f"boundary precision         : {found_total}/{pred_total} = "
          f"{(found_total / pred_total if pred_total else 1):.1%}  "
          f"({pred_total - found_total} false splits)")
    print(f"segment precision (strict) : {exact_and_manifest}/{segments_total} = "
          f"{exact_and_manifest / segments_total:.1%}   right doctype over right page range")
    print(f"  exact page range only    : {exact_range}/{segments_total} = "
          f"{exact_range / segments_total:.1%}")
    print(f"  vs the slice's solo answer: {exact_and_standalone}/{segments_total} = "
          f"{exact_and_standalone / segments_total:.1%}")
    print(f"page-level doctype accuracy: {pages_right}/{pages_total} = "
          f"{pages_right / pages_total:.1%}")
    print()
    for v in ("EXACT", "PARTIAL", "ONE_SEGMENT", "MISSED", "INGEST_FAILED"):
        if verdicts[v]:
            print(f"  {v:<14}: {verdicts[v]:>3}/{len(results)} "
                  f"({verdicts[v] / len(results):.1%})")
    print(f"\nbundles returned as ONE segment (total miss): {verdicts['ONE_SEGMENT']}/"
          f"{len(scored)} = {verdicts['ONE_SEGMENT'] / len(scored):.1%}")

    print("\nWHICH SIGNAL FOUND THE BOUNDARY")
    for sig, n in found_by_signal.most_common():
        proposed = proposed_by_signal[sig]
        print(f"  {sig:<20} found {n:>3}   proposed {proposed:>3}   "
              f"killed downstream {killed_by_signal[sig]:>3}   "
              f"fired inside a document {internal_proposals[sig]:>3}   "
              f"survived as a false split {false_split_by_signal[sig]:>3}")
    for sig in ("adequacy", "geometry", "first_page_anchor"):
        if sig not in found_by_signal:
            print(f"  {sig:<20} found   0   proposed {proposed_by_signal[sig]:>3}")
    print(f"\ntrue boundaries NO signal fired at: {missed_unseen}/{true_total} = "
          f"{missed_unseen / true_total:.1%}")

    print("\nRECALL BY BUNDLE SHAPE")
    for shape, c in sorted(shapes.items()):
        print(f"  {shape:<44} bundles={c['bundles']:>2}  boundaries={c['true']:>3}  "
              f"found={c['found']:>3}  recall={(c['found'] / c['true'] if c['true'] else 0):.0%}")

    print("\nSTANDALONE CONTROL (each slice classified alone)")
    for key in sorted(standalone):
        got = standalone[key]
        flag = "" if got == SOURCES[key].doctype else "   <- not its manifest doctype"
        print(f"  {key:<30} -> {got}{flag}")

    return {
        "bundles": [
            {
                "name": r.name, "keys": r.keys, "shape": r.shape, "verdict": r.verdict,
                "n_docs": r.n_docs, "n_pages": r.n_pages,
                "true_spans": r.true_spans, "pred_spans": r.pred_spans,
                "true_boundaries": r.true_boundaries,
                "pred_boundaries": r.pred_boundaries,
                "proposals": r.proposals, "note": r.note,
            }
            for r in results
        ],
        "aggregate": {
            "bundles_measured": len(scored),
            "true_boundaries": true_total,
            "found": found_total,
            "boundary_recall": found_total / true_total if true_total else None,
            "predicted_boundaries": pred_total,
            "false_splits": pred_total - found_total,
            "boundary_precision": found_total / pred_total if pred_total else None,
            "segments": segments_total,
            "exact_range": exact_range,
            "segment_precision": exact_and_manifest / segments_total if segments_total else None,
            "segment_precision_vs_standalone": (
                exact_and_standalone / segments_total if segments_total else None),
            "page_doctype_accuracy": pages_right / pages_total if pages_total else None,
            "verdicts": dict(verdicts),
            "one_segment_rate": verdicts["ONE_SEGMENT"] / len(scored) if scored else None,
            "found_by_signal": dict(found_by_signal),
            "proposed_by_signal": dict(proposed_by_signal),
            "killed_by_signal": dict(killed_by_signal),
            "internal_proposals_by_signal": dict(internal_proposals),
            "false_split_by_signal": dict(false_split_by_signal),
            "boundaries_no_signal_saw": missed_unseen,
            "by_shape": {k: dict(v) for k, v in shapes.items()},
            "standalone_control": standalone,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--corpus-root", type=Path, default=REPO,
                        help="repo root holding corpus/ (default: this repo)")
    parser.add_argument("--json", type=Path, help="write the full record here")
    parser.add_argument("--verbose", action="store_true", help="print every bundle")
    args = parser.parse_args(argv)

    missing = [s.rel for s in SOURCES.values() if not (args.corpus_root / s.rel).exists()]
    if missing:
        print("corpus files missing — the corpus is local working material and is not "
              "committed. Fetch them per tools/README-corpus.md:", file=sys.stderr)
        for rel in sorted(set(missing)):
            print(f"  {rel}", file=sys.stderr)
        return 2

    record = run(args.corpus_root, args.verbose)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(record, indent=2, default=str))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
