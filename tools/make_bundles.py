#!/usr/bin/env python3
"""Build the bundle corpus: real corpus documents concatenated into multi-document files.

**Why these are not in `corpus/<cc>/manifest.jsonl`.** That manifest maps one file to one
``expected_doctype`` and `corpus_test.py` globs `corpus/*/manifest.jsonl` to compute the
precision figure the whole project is judged on. A bundle has three expected doctypes, so a
bundle sitting in that manifest would be scored as one wrong answer per bundle and would
corrupt the number. These live in `corpus/bundles/` under `bundles.jsonl` — a filename the
glob deliberately does not match.

Each entry declares the page range every constituent occupies, which is the ground truth a
segmenter is measured against: slices of length (3, 3) give a true boundary at page 4.

Regenerate with:  .venv/bin/python tools/make_bundles.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "corpus"
OUT_DIR = CORPUS / "bundles"

#: (name, [(corpus-relative source, pages to take), …], what the bundle is for).
#:
#: The set deliberately includes shapes segmentation CANNOT currently solve. A corpus that
#: held only the winnable cases would report a recall this system does not have, and the
#: same-size/all-text pair is the shape measured at 0% — it is here precisely so that number
#: stays visible and so a future signal has something to be tested against.
BUNDLES: list[tuple[str, list[tuple[str, int]], str]] = [
    (
        "bundle_w9_bankstatement",
        [("us/us_w9.pdf", 3), ("us/us_bank_statement.pdf", 3)],
        "two documents, different page stock — the geometry signal should find the seam",
    ),
    (
        "bundle_w9_bankstatement_1040",
        [("us/us_w9.pdf", 2), ("us/us_bank_statement.pdf", 2), ("us/us_1040.pdf", 2)],
        "three documents; the landscape statement gives both junctions a geometry change",
    ),
    (
        "bundle_w9_1040_sameshape",
        [("us/us_w9.pdf", 2), ("us/us_1040.pdf", 2)],
        "Same page size (612x792), both text-bearing, no image anywhere — so geometry and "
        "adequacy are both silent and the ONLY signal is the 1040's 'OMB No. 1545-0074'. "
        "Splits correctly at page 3 today, and is here to catch a regression in the "
        "first-page-anchor rule, which nothing else in this set would notice.",
    ),
    (
        "bundle_bylaws_articles_noanchor",
        [("us/us_bylaws.pdf", 2), ("us/us_articles_incorporation.pdf", 2)],
        "THE KNOWN BLIND SPOT, measured: same page size, both text-bearing, and NEITHER "
        "carries a form or control number. All three signals are silent — candidate_"
        "boundaries returns nothing at all — so this comes back as ONE segment. That is a "
        "recall limit, not a bug: there is no structural difference to detect, and no "
        "threshold reveals a signal that is not there. It is committed so the limit stays "
        "visible and so a future signal has something to be measured against.",
    ),
    (
        "bundle_id_and_utility",
        [("us/us_drivers_license.pdf", 1), ("us/us_utility_bill.pdf", 2)],
        "a photo ID followed by a proof of address — the commonest real KYC pairing",
    ),
    (
        "bundle_crosscountry",
        [("ca/ca_cra_noa.pdf", 1), ("us/us_w9.pdf", 2), ("mx/mx_curp_constancia.pdf", 1)],
        "three jurisdictions in one upload; also tests that country does not leak between "
        "segments",
    ),
    (
        "single_w9_control",
        [("us/us_w9.pdf", 3)],
        "CONTROL: not a bundle. Must come back as exactly one segment — proof that "
        "segmentation costs a single document nothing.",
    ),
]


def build(name: str, parts: list[tuple[str, int]], note: str) -> dict | None:
    import fitz

    out = fitz.open()
    members: list[dict] = []
    for rel, pages in parts:
        source = CORPUS / rel
        if not source.exists():
            print(f"  SKIP {name}: missing {rel}", file=sys.stderr)
            out.close()
            return None
        src = fitz.open(source)
        take = min(pages, src.page_count)
        start = out.page_count + 1
        out.insert_pdf(src, from_page=0, to_page=take - 1)
        members.append(
            {
                "source": f"corpus/{rel}",
                "expected_doctype": Path(rel).stem,
                "start_page": start,
                "end_page": out.page_count,
            }
        )
        src.close()

    target = OUT_DIR / f"{name}.pdf"
    out.save(target)
    page_count = out.page_count
    out.close()

    return {
        "file": f"corpus/bundles/{name}.pdf",
        "page_count": page_count,
        "documents": len(members),
        # The pages a new document starts on. This is what a segmenter is scored against.
        "true_boundaries": [m["start_page"] for m in members[1:]],
        "members": members,
        "notes": note,
    }


def main() -> int:
    try:
        import fitz  # noqa: F401
    except ImportError:
        print("PyMuPDF is required: pip install '.[pdf]'", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    entries = [e for e in (build(*b) for b in BUNDLES) if e]
    manifest = OUT_DIR / "bundles.jsonl"
    manifest.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")

    print(f"{len(entries)} bundle(s) -> {OUT_DIR}")
    for e in entries:
        boundaries = e["true_boundaries"] or ["none"]
        print(
            f"  {Path(e['file']).name:34s} {e['page_count']}p  "
            f"{e['documents']} doc(s)  true boundaries: {boundaries}"
        )
    print(f"manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
