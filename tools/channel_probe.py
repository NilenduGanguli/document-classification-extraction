#!/usr/bin/env python3
"""Report the ANCHOR-channel and LEXICAL-channel leader for named corpus documents.

The fusion evidence line the service already publishes names both component leaders; the
corpus report truncates it, so this asks for one document at a time and prints them. It is
a measurement instrument, not part of the service.

The payload is built with :mod:`corpus_test`'s own functions and dispatched by its own rule
(a text layer of at least ``MIN_ALNUM_CHARS`` goes as text, anything else goes to the
service's ingestion path as bytes), so a leader printed here is the leader the corpus run
saw — not the leader of a differently-built request.

Usage::

    python tools/channel_probe.py                       # the documents named in the brief
    python tools/channel_probe.py --layout              # ...with inferred zones
    python tools/channel_probe.py ca/ca_t4.pdf us/us_w9.pdf
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from corpus_test import (
    MIN_ALNUM_CHARS,
    build_ingest_payload,
    build_payload,
    infer_zones,
    load_pdf_text,
)

ROOT = Path(__file__).resolve().parent.parent
URL = "http://localhost:8200/api/v1/classify"

#: The five documents the brief names as the symptom, then the six abstentions that already
#: ranked their correct doctype first.
DEFAULT = [
    "ca/ca_sin_confirmation.pdf",
    "ca/ca_citizenship_certificate.pdf",
    "us/us_passport.jpg",
    "us/us_paystub.pdf",
    "ca/ca_articles_incorporation_provincial.pdf",
    "ca/ca_aif__oilgas_issuer.pdf",
    "mx/mx_ine.pdf",
    "mx/mx_prospecto_colocacion_2.pdf",
    "us/us_bylaws.pdf",
    "us/us_green_card.pdf",
    "us/us_sec_10q.pdf",
]

_ANCHOR = re.compile(r"anchor leader '([^']*)' \(lead ([-\d.]+) bits\)")
_LEX = re.compile(r"explained leader '([^']*)' \(lead ([-\d.]+)\)")


def probe(rel: str, *, layout: bool) -> dict[str, str]:
    path = ROOT / "corpus" / rel
    pdf = load_pdf_text(path, allow_images=True)
    if pdf.alnum_chars < MIN_ALNUM_CHARS:
        payload = build_ingest_payload(rel, path)
    else:
        if layout:
            infer_zones(pdf)
        payload = build_payload(rel, pdf, layout)
    request = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    result = json.load(urllib.request.urlopen(request, timeout=300))
    detail = ""
    for note in result.get("evidence") or []:
        if note.get("tier") == "fusion":
            detail = str(note.get("detail") or "")
    anchor = _ANCHOR.search(detail)
    lexical = _LEX.search(detail)
    return {
        "expected": path.stem.split("__")[0],
        "answer": str(result.get("doctype_id") or ""),
        "abstained": "yes" if result.get("abstained") else "no",
        "anchor_leader": anchor.group(1) if anchor else "none",
        "anchor_lead": anchor.group(2) if anchor else "0",
        "lexical_leader": lexical.group(1) if lexical else "none",
    }


def main() -> int:
    argv = list(sys.argv[1:])
    layout = "--layout" in argv
    docs = [a for a in argv if not a.startswith("-")] or DEFAULT
    rows = [probe(d, layout=layout) for d in docs]
    width = max(len(r["expected"]) for r in rows)
    print(f"zones: {'inferred_pymupdf (--layout)' if layout else 'none_all_body (plain)'}")
    print(f"{'expected':<{width}}  {'ANCHOR leader':<40}  {'LEXICAL leader':<36}  answer")
    for row in rows:
        anchor = f"{row['anchor_leader']} ({row['anchor_lead']})"
        answer = row["answer"] + (" ABSTAINED" if row["abstained"] == "yes" else "")
        print(f"{row['expected']:<{width}}  {anchor:<40}  {row['lexical_leader']:<36}  {answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
