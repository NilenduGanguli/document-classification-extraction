#!/usr/bin/env python3
"""Measure the DCE service against a corpus of real documents.

Reads every ``corpus/<cc>/manifest.jsonl``, pulls the text layer out of each PDF with
PyMuPDF, sends it to a running DCE service, and scores what came back against the doctype
the manifest says the document is. Writes ``reports/corpus-results.json`` (everything) and
``reports/corpus-results.md`` (the table a human reads).

Three decisions worth stating, because they are the difference between a measurement and a
number that flatters us:

**A scan is not a failure, and it is not a guess either.** Most official blank forms are
digital PDFs with a real text layer. When a PDF has none, this tool records it as
``needs_ocr`` and *skips* it. It does not fall back to rasterise-and-hope, and it does not
quietly drop the document from the denominator — it is reported in its own section, with a
count, so the OCR decision is made deliberately and not smuggled in here. Two flags are that
deliberate decision made explicitly, and both are **off by default and change nothing when
off** — the numbers a plain run produces are the numbers a plain run has always produced:

* ``--ingest`` is the one to reach for. The file goes to the service as bytes and **the
  service's own OCR path** reads it, with the provider ``/readyz`` reports this deployment
  configured (``--ocr-provider`` overrides it, and can only name a provider ``/readyz``
  already lists). Nothing is rasterised here and no OCR endpoint is called from here: the
  harness posts a document and the service does what it does in production, which is the
  only reason a number from this path is evidence about production rather than about this
  tool's rasteriser.
* ``--ocr`` is the older, harness-side path: *this tool* rasterises the pages and calls an
  Azure Read v3.2 endpoint itself, then posts text. It measures the classifier given OCR
  text, not the service's ingestion. When both flags are on, ``--ingest`` wins for every
  document with no text layer — one rule, so a scanned PDF and a JPEG are never read by two
  different engines in the same run.

**OCR results are reported apart from text-layer results, always.** OCR error is a confound:
a wrong doctype on an OCR'd scan may be the classifier's fault or the OCR engine's, and
averaging the two together produces a number nobody can act on. Every summary is split by
text source — ``text_layer``, ``service_ingest``, ``ocr`` (harness-side) and
``service_ingest_ocr`` (the service's own recogniser) — with a second split by *reader*, so
each provider that read documents in a run carries its own row count and its own rate. The
per-document table names the reader in its ``src`` column and recognised documents get their
own section. Compare a run against its own ``text_layer`` bucket, never against ``overall``.

**Abstention is a distinct outcome, not a wrong answer.** The service is built to refuse
rather than guess; scoring an abstention as a miss would push a reader toward exactly the
thresholds this system exists to avoid. ``CORRECT``/``WRONG``/``ABSTAINED`` are counted
separately, and both an accuracy over everything the classifier saw *and* a
precision-when-it-answered are reported.

**A zone is a claim, and the report always says who made it.** The classifier weights a term
by the zone it sits in (``title`` 3x, ``heading`` 2x, ``furniture`` 0.25x) and 34 registry
anchors are gated to ``title`` outright — an anchor declared ``zone=title`` cannot match a
payload that has no title. Until v1.2.0 this harness stamped ``"zone": "body"`` on every block
it sent, so every one of those anchors was unreachable in every number it has ever produced,
and the instrument disagreed with production, where ``dce/adapters.py`` maps Azure Document
Intelligence's ``paragraphs[].role`` onto real zones. Three zone sources exist now, they are
*never* averaged, and each one is named in the report next to the number it produced:

* ``azure_di_roles`` — a real Azure DI ``analyzeResult`` (a ``<doc>.di.json`` sidecar, or
  ``--di-dir``) is posted verbatim as ``azure_analyze_result`` and the **service's own**
  adapter assigns the zones. Production-identical, and the only zone source that is evidence
  about production rather than about this harness.
* ``inferred_pymupdf`` / ``inferred_ocr_bbox`` — ``--layout``. There are no roles here, so the
  zones are *inferred* from geometry (font size, page position, line length) by
  :func:`infer_zones`. An approximation, deliberately a conservative one, and weaker evidence
  than the row above it.
* ``none_all_body`` — the default plain-text payload. The service's ``from_plain_text`` labels
  every block ``body``, honestly, and every ``zone=title`` anchor stays unreachable. The run
  says so in a banner rather than letting the number pass as a general result.

**The report carries no document values by default.** The corpus is blank forms and
official specimens, so any value here is fabricated — but a harness that dumps extracted
fields into a checked-in report is one corpus mistake away from writing a real person's
identifier to disk. Field-level output records *whether* a field filled, not what it said.
``--show-values`` opts in for non-PII fields; fields the registry marks ``pii`` stay masked
even then.

The tool talks HTTP with :mod:`urllib` from the standard library rather than httpx: it is
meant to be runnable next to a service whose whole premise is that it has no HTTP client,
and its only third-party dependency is the PDF reader.

The service's egress invariant is untouched by any of this. The harness is a client, not
part of the service: under ``--ocr`` *the harness* calls the OCR endpoint, obtains text, and
only then sends text to DCE. The service still makes no network call before a doctype is
accepted, and ``allow_preclassification_egress`` stays ``False``.

Usage::

    python tools/corpus_test.py                       # whole corpus, classify + extract
    python tools/corpus_test.py --country in --verbose
    python tools/corpus_test.py --only us_w9,us_1040 --classify-only
    python tools/corpus_test.py --corpus-root /tmp/fake-corpus --out-dir /tmp/out
    python tools/corpus_test.py --ingest               # scans and images measured too, by
                                                      # the service's own OCR path
    python tools/corpus_test.py --ingest --ocr-provider azure_read    # ...with Read instead
    python tools/corpus_test.py --ocr --layout        # the harness-side OCR path
    python tools/corpus_test.py --layout --zone-dump-dir /tmp/zones   # audit the inference

Exit status is always ``0``. This is a measurement tool, not a gate; a CI job that wants a
threshold should read ``reports/corpus-results.json`` and decide for itself.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOOL_VERSION = "1.3.0"

#: Repo root, assuming this file stays at ``<repo>/tools/corpus_test.py``.
REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_BASE_URL = os.environ.get("DCE_URL", "http://localhost:8200")
DEFAULT_CORPUS_ROOT = REPO_ROOT / "corpus"
DEFAULT_OUT_DIR = REPO_ROOT / "reports"

#: A PDF must start with this and be bigger than this to be worth sending anywhere. The
#: size floor catches the classic failure of a corpus run: an HTML error page, a login
#: wall or a truncated download saved under a ``.pdf`` name.
PDF_MAGIC = b"%PDF"
MIN_PDF_BYTES = 5 * 1024

#: An image has no container overhead, so the floor that catches a saved error page is
#: lower than the PDF one. Same purpose: a 400-byte "us_passport.jpg" is a failed download.
MIN_IMAGE_BYTES = 2 * 1024

#: Below this many alphanumeric characters over the whole document there is no usable text
#: layer — it is a scan (or an image-only form) and belongs in the ``needs_ocr`` bucket.
MIN_ALNUM_CHARS = 60

STATUS_CORRECT = "CORRECT"
STATUS_WRONG = "WRONG"
STATUS_ABSTAINED = "ABSTAINED"
STATUS_NEEDS_OCR = "NEEDS_OCR"
STATUS_ERROR = "ERROR"

#: The three statuses that put a document into an accuracy figure. NEEDS_OCR and ERROR
#: documents were never classified, so they contribute to no rate — and, as of the ingestion
#: round, they must not contribute to the *zone-honesty* guards either: two unread JPEGs
#: carrying a production-faithful zone source would otherwise suppress the "these numbers are
#: not production figures" warning about the fifty-nine documents that actually were scored.
SCORED_STATUSES = frozenset({STATUS_CORRECT, STATUS_WRONG, STATUS_ABSTAINED})

#: Where a document's text came from. Recorded per document and carried into every summary,
#: because a result built on OCR output is not the same measurement as one built on a
#: publisher's own text layer.
SOURCE_TEXT_LAYER = "text_layer"
SOURCE_OCR = "ocr"
#: The service parsed the bytes itself (``dce.ingest``). Its own third bucket: the text is
#: the publisher's, like ``text_layer``, but it was produced by the service's parsers rather
#: than by this harness's, so a difference between the two buckets is a difference between
#: two readers of the same file and not between two qualities of text.
SOURCE_INGEST = "service_ingest"
#: The service **recognised** the bytes: the file carried no text at all, and ``dce.ingest``
#: handed it to the OCR provider this deployment configured. A fourth bucket rather than a
#: shade of ``service_ingest``, for the reason the whole file is split on text source: a wrong
#: doctype here may be the recogniser's error and not the classifier's, and a bucket that
#: mixed a DOCX's own XML with a photograph of a passport would hide exactly that. Kept
#: distinct from ``ocr`` too — that one is *this harness's* rasteriser and endpoint, this one
#: is production's.
SOURCE_INGEST_OCR = "service_ingest_ocr"

#: Text sources whose text came out of a recognition engine, whoever called it. Every rate
#: built on these carries OCR error; the report says so wherever it prints one.
SOURCES_RECOGNISED = frozenset({SOURCE_OCR, SOURCE_INGEST_OCR})

# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------
#: Where a document's *zones* came from. This is a separate axis from where its text came
#: from, and it is the more important of the two for classification: zone drives the lexical
#: weight of every term and gates 34 registry anchors outright. Reported per document, and
#: bucketed in every summary, because these are not interchangeable grades of the same
#: measurement — only ``azure_di_roles`` is evidence about production.
ZONE_SOURCE_DI_ROLES = "azure_di_roles"        # the service's own adapter, real DI roles
ZONE_SOURCE_PYMUPDF = "inferred_pymupdf"       # geometry heuristic over a PDF text layer
ZONE_SOURCE_OCR_BBOX = "inferred_ocr_bbox"     # geometry heuristic over OCR line boxes
ZONE_SOURCE_NONE = "none_all_body"             # plain text: the service labels everything body
#: ``dce.ingest`` parsed the file *inside the service* and mapped the format's OWN stated
#: structure — a DOCX ``Title`` style, an HTML ``<h1>``, a PPTX title placeholder, an EML
#: ``Subject`` — onto the zone model. Not a geometry heuristic, and not this harness's opinion.
ZONE_SOURCE_INGEST = "service_ingest"
#: ``dce.ingest`` called this deployment's OCR provider and mapped *its* structure. What that
#: is worth depends entirely on the provider, which is why the provider is in every row: an
#: ``azure_layout`` read comes back with paragraph roles and reaches ``dce/adapters.py``'s
#: ``from_azure_layout``, so it carries real title/heading zones; ``azure_read`` and the
#: in-process engines return lines only and the service labels every block ``body``. Either
#: way the mapping is the service's own, so it is what production does — with the provider's
#: ceiling on it, and OCR error underneath it.
ZONE_SOURCE_INGEST_OCR = "service_ingest_ocr"

#: Zone sources that reproduce production. Three of them, for the same reason: production
#: assigns these zones itself. ``azure_di_roles`` is the service's adapter reading a real
#: Document Intelligence payload; ``service_ingest`` is the service's own parser reading a
#: DOCX/XLSX/PPTX/ODT/HTML/EML/MSG; ``service_ingest_ocr`` is the service calling its own
#: configured recogniser and mapping the answer with the same adapters. Every other source in
#: this file is the harness guessing. "Production-faithful zones" is *not* "trustworthy text":
#: the OCR bucket's text still came from a recogniser, and the text-source split is where that
#: is accounted for.
ZONE_SOURCES_REAL = frozenset(
    {ZONE_SOURCE_DI_ROLES, ZONE_SOURCE_INGEST, ZONE_SOURCE_INGEST_OCR}
)

ZONE_TITLE = "title"
ZONE_HEADING = "heading"
ZONE_BODY = "body"
ZONE_FURNITURE = "furniture"
ZONE_NAMES = (ZONE_TITLE, ZONE_HEADING, ZONE_BODY, ZONE_FURNITURE)

#: ``paragraphs[].role`` -> zone, for *reporting* a DI sidecar's zone mix. It mirrors
#: ``dce.adapters.ROLE_ZONES`` and must keep mirroring it; the zones that actually reach the
#: classifier are assigned by the service, not here, so a drift shows up as a wrong count in
#: the report and never as a wrong measurement.
DI_ROLE_ZONES: dict[str, str] = {
    "title": ZONE_TITLE,
    "sectionHeading": ZONE_HEADING,
    "pageHeader": ZONE_FURNITURE,
    "pageFooter": ZONE_FURNITURE,
    "pageNumber": ZONE_FURNITURE,
}

# --- inference thresholds --------------------------------------------------
# These are a priori settings, chosen from what a title and a running header look like on a
# page, and they are NOT calibrated against this corpus: 67 documents cannot calibrate a
# threshold, and a number tuned until these files score well would make the instrument a
# function of its own corpus. They are deliberately biased toward *under*-promotion — a title
# this harness invents is amplified 3x in the score and turns a font accident into a confident
# classification, while a title it misses only costs the recall production would have had.
TITLE_TOP_FRACTION = 0.40      # a title sits in the top of page 1, not halfway down it
TITLE_SIZE_RATIO = 1.25        # ...and is meaningfully larger than the body text
TITLE_SIZE_ABS = 1.0           # ...by at least a point, so 8pt vs 9.9pt is not a "title"
TITLE_TIE_RATIO = 0.95         # a two-line title shares one size; take the near-ties too
TITLE_MAX_CHARS = 100          # a 300-character line is a paragraph in a large font
TITLE_MAX_LINES = 3            # cap the blast radius of a document with a huge cover font
HEADING_SIZE_RATIO = 1.15      # a section heading is larger than body text...
HEADING_SIZE_ABS = 0.5
HEADING_MAX_CHARS = 80         # ...and short
FURNITURE_BAND = 0.10          # running heads/feet live in the top/bottom tenth of the page
FURNITURE_MIN_PAGES = 2        # "running" means it runs: one appearance is not furniture

#: Page-number lines: "3", "Page 3", "Page 3 of 12", "- 3 -", "3/12", "Página 3 de 12".
_PAGE_NUMBER_RE = re.compile(
    # The en and em dashes are the point, not a typo: typesetters use them for "— 3 —".
    r"^(?:[-–—]\s*)?(?:page|pág(?:ina)?|pagina|seite)?\s*\d{1,4}"  # noqa: RUF001
    r"(?:\s*(?:of|/|de|sur|von)\s*\d{1,4})?\s*(?:[-–—])?$",  # noqa: RUF001
    re.IGNORECASE,
)

#: Digits collapsed so "Page 3 of 12" and "Page 4 of 12" count as the same running footer.
_DIGITS_RE = re.compile(r"\d+")

PII_MASK = "«pii-redacted»"

#: Image magic bytes -> the ``filetype`` hint PyMuPDF wants. Mirrors
#: ``des/ocr/raster.py::_detect_filetype`` so the harness accepts exactly what the DES
#: pipeline accepts, rather than a second, subtly different list.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"BM", "bmp"),
    (b"GIF8", "gif"),
)

#: Suffixes that make a file an image candidate even before magic bytes are read.
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif"}

#: Azure Computer Vision Read v3.2 — the same two calls ``des/ocr/azure_read.py`` makes.
AZURE_READ_ANALYZE_PATH = "/vision/v3.2/read/analyze"
AZURE_READ_TERMINAL = {"succeeded", "failed"}

DEFAULT_OCR_ENDPOINT = os.environ.get("AZURE_VISION_ENDPOINT", "http://localhost:5006")
DEFAULT_OCR_KEY = os.environ.get("AZURE_VISION_KEY", "")

#: Render resolution for the OCR path. DES rasterises at 144 for its viewer; 200 is the
#: bottom of the band Azure Read documents for small print, and the corpus is full of
#: 6-point statutory footnotes. Tunable with ``--ocr-dpi`` — it is a knob, not a contract.
DEFAULT_OCR_DPI = 200

#: Latin, Devanagari and other Indic letters plus digits — enough to tell "has a text
#: layer" from "is a scan" for US/CA/MX/IN documents without importing unicodedata.
_ALNUM = re.compile(r"[0-9A-Za-zÀ-ɏऀ-ॿঀ-ൿ]")

#: Second root that report paths are shown relative to, set to the corpus's parent at run
#: time so a corpus outside the repo (a /tmp fixture) still prints short paths.
_DISPLAY_ROOT: Path | None = None


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass
class ManifestEntry:
    """One line of a ``manifest.jsonl``, resolved against the filesystem."""

    country: str
    file: str
    expected_doctype: str
    kind: str = ""
    source_url: str = ""
    notes: str = ""
    manifest: str = ""
    line_no: int = 0
    path: Path | None = None


@dataclass
class ManifestError:
    """A manifest line that could not be used, and why."""

    manifest: str
    line_no: int
    detail: str
    raw: str = ""


@dataclass
class OcrOutcome:
    """What the OCR provider did for one document, kept so a result can be audited.

    No recognised text is stored here. A per-page character count tells a reader whether
    OCR produced anything; ``--ocr-dump-dir`` is the deliberate, opt-in way to get the text
    itself, and it never writes into the report directory.
    """

    engine: str = ""
    endpoint: str = ""
    dpi: int = 0
    pages_sent: int = 0
    pages_ok: int = 0
    lines: int = 0
    chars_per_page: list[int] = field(default_factory=list)
    ms: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class PdfText:
    """The text of one document, kept page by page.

    ``source`` says where the text came from: the publisher's own text layer, or an OCR
    engine. Every downstream rate is split on it.
    """

    pages: list[str] = field(default_factory=list)
    page_sizes: list[tuple[float, float]] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)
    page_count: int = 0
    pages_read: int = 0
    alnum_chars: int = 0
    empty_pages: list[int] = field(default_factory=list)
    encrypted: bool = False
    source: str = SOURCE_TEXT_LAYER
    unit: str = "point"
    is_image: bool = False
    filetype: str = "pdf"
    ocr: OcrOutcome | None = None
    #: Set by :func:`infer_zones`, and only when a layout payload is actually built. A
    #: plain-text run leaves it at ``ZONE_SOURCE_NONE``, which is what the service does with
    #: that payload.
    zone_source: str = ZONE_SOURCE_NONE
    zone_counts: dict[str, int] = field(default_factory=dict)
    #: How the inference reached its verdict, in one line, for the report.
    zone_basis: str = ""

    @property
    def chars_per_page(self) -> list[int]:
        return [len(p) for p in self.pages]

    @property
    def text(self) -> str:
        return "\n".join(self.pages)


class HarnessError(Exception):
    """A document could not be measured. Recorded against the document, never fatal."""


class NeedsOcrError(HarnessError):
    """The document carries no text layer at all — a scan, or an image file.

    Separate from :class:`HarnessError` because the two mean opposite things about the
    corpus. An error is a *fault* (a truncated download, a password, an HTML page saved as a
    PDF) and someone has to go fix the file. This is not a fault: a JPEG of a passport has no
    text layer in the same way a scanned PDF has none, and both are measurable the moment
    ``--ocr`` is on. Raising the same exception for both put ``us_passport.jpg`` and
    ``us_passport_card.jpg`` in the ERROR column, where they read as broken corpus files.
    """


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------
def read_manifests(corpus_root: Path) -> tuple[list[ManifestEntry], list[ManifestError]]:
    """Read every ``<corpus_root>/*/manifest.jsonl``.

    A bad line is recorded and skipped — one agent's typo must not cost the other three
    countries their run.

    Args:
        corpus_root: Directory holding one subdirectory per country code.

    Returns:
        ``(entries, errors)``, entries in manifest order, countries alphabetical.
    """
    entries: list[ManifestEntry] = []
    errors: list[ManifestError] = []

    for manifest in sorted(corpus_root.glob("*/manifest.jsonl")):
        country = manifest.parent.name.lower()
        name = _relpath(manifest)
        try:
            raw_lines = manifest.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            errors.append(ManifestError(name, 0, f"cannot read manifest: {exc}"))
            continue

        for line_no, raw in enumerate(raw_lines, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(ManifestError(name, line_no, f"invalid JSON: {exc}", line[:200]))
                continue
            if not isinstance(obj, dict):
                errors.append(ManifestError(name, line_no, "not a JSON object", line[:200]))
                continue

            file_value = str(obj.get("file") or "").strip()
            expected = str(obj.get("expected_doctype") or "").strip()
            if not file_value or not expected:
                detail = "missing 'file' or 'expected_doctype'"
                errors.append(ManifestError(name, line_no, detail, line[:200]))
                continue

            entries.append(
                ManifestEntry(
                    country=country,
                    file=file_value,
                    expected_doctype=expected,
                    kind=str(obj.get("kind") or ""),
                    source_url=str(obj.get("source_url") or ""),
                    notes=str(obj.get("notes") or ""),
                    manifest=name,
                    line_no=line_no,
                    path=_resolve_file(file_value, manifest, corpus_root),
                )
            )

    return entries, errors


def _resolve_file(file_value: str, manifest: Path, corpus_root: Path) -> Path:
    """Resolve a manifest ``file`` to a real path.

    Manifests are written repo-root-relative (``corpus/us/us_w9.pdf``), which is what the
    corpus agents were told to produce. An absolute path, a path relative to the manifest's
    own directory, or a bare filename all work too — being strict here would fail runs for
    a reason that has nothing to do with classification.
    """
    candidate = Path(file_value)
    if candidate.is_absolute():
        return candidate

    tries = [
        corpus_root.parent / candidate,  # repo-root-relative: corpus/us/x.pdf
        REPO_ROOT / candidate,
        manifest.parent / candidate.name,  # bare filename, or same-dir relative
        manifest.parent / candidate,
    ]
    for path in tries:
        if path.exists():
            return path
    return tries[0]


def _relpath(path: Path) -> str:
    """Shortest readable form of ``path``: relative to the repo, else to the corpus."""
    resolved = path.resolve()
    for root in (REPO_ROOT, _DISPLAY_ROOT):
        if root is None:
            continue
        try:
            return str(resolved.relative_to(root))
        except ValueError:
            continue
    return str(resolved)


# ---------------------------------------------------------------------------
# PDF text
# ---------------------------------------------------------------------------
def detect_filetype(path: Path) -> str | None:
    """``"pdf"``, a PyMuPDF image filetype, or ``None`` for "not something we can open".

    Magic bytes decide, not the suffix: a ``.pdf`` that is really an HTML error page is the
    single most common corpus fault, and trusting the name would let it through.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return None
    if head.startswith(PDF_MAGIC):
        return "pdf"
    for magic, filetype in _IMAGE_MAGIC:
        if head.startswith(magic):
            return filetype
    return None


def load_pdf_text(path: Path, max_pages: int = 0, *, allow_images: bool = False) -> PdfText:
    """Extract the text layer of ``path``, page by page, with PyMuPDF.

    Args:
        path: The document. A PDF always; an image too when ``allow_images`` is set.
        max_pages: Stop after this many pages; ``0`` reads all of them.
        allow_images: Accept PNG/JPEG/TIFF/BMP/GIF as single-page documents instead of
            erroring on them. Only the ``--ocr`` path sets this, because an image has no
            text layer to read and would otherwise be measured as a document with none —
            an abstention that looks like a classifier decision but is not one.

    Returns:
        A :class:`PdfText`. An empty text layer is *not* an error here — the caller
        decides that it means ``needs_ocr``.

    Raises:
        HarnessError: The file is missing, too small, not a readable document, encrypted,
            or unreadable.
    """
    try:
        import fitz  # PyMuPDF; imported late so --help works without it installed
    except ImportError as exc:  # pragma: no cover - environment problem, not a doc problem
        raise HarnessError(
            "PyMuPDF is not installed — run: uv pip install pymupdf  (or pip install pymupdf)"
        ) from exc

    if not path.exists():
        raise HarnessError(f"file not found: {path}")
    filetype = detect_filetype(path)
    is_image = filetype is not None and filetype != "pdf"

    # An image is only a document when the caller has an OCR provider to hand. Without
    # ``--ocr`` it is *unmeasured*, exactly like a scanned PDF with no text layer — same
    # cause, same bucket, same remedy. It is not an ERROR; nothing about the file is wrong.
    if not filetype or (is_image and not allow_images):
        # Magic bytes decide, not the suffix: a ``.jpg`` that is really an HTML error page
        # stays an ERROR, because that one *is* a fault and re-running with --ocr will not
        # fix it.
        if is_image:
            raise NeedsOcrError(
                f"{filetype} image has no text layer by definition — this is a scan in a "
                "different container; rerun with --ocr to rasterise and recognise it"
            )
        raise HarnessError("does not start with %PDF — not a PDF, delete it and re-source")

    size = path.stat().st_size
    floor = MIN_IMAGE_BYTES if is_image else MIN_PDF_BYTES
    if size < floor:
        raise HarnessError(
            f"file is {size} bytes, under the {floor}-byte floor — failed download?"
        )

    out = PdfText(is_image=is_image, filetype=filetype)
    try:
        # PyMuPDF presents an image as a one-page document, which is why one code path
        # serves both — the same trick DES's rasteriser relies on.
        doc = fitz.open(str(path), filetype=filetype)
    except Exception as exc:  # PyMuPDF raises a wide family; all mean "unreadable"
        raise HarnessError(f"PyMuPDF cannot open the file: {exc}") from exc

    with doc:
        if doc.needs_pass:
            out.encrypted = True
            raise HarnessError("PDF is password-protected")
        out.page_count = doc.page_count
        limit = doc.page_count if max_pages <= 0 else min(max_pages, doc.page_count)

        for index in range(limit):
            page = doc.load_page(index)
            page_no = index + 1
            rect = page.rect
            out.page_sizes.append((float(rect.width), float(rect.height)))
            try:
                page_text = page.get_text("text") or ""
            except Exception as exc:  # noqa: BLE001 - a broken page costs that page, not the doc
                page_text = ""
                out.lines.append({"page": page_no, "text": "", "error": str(exc), "bbox": None})
            out.pages.append(page_text)
            if not page_text.strip():
                out.empty_pages.append(page_no)
            out.lines.extend(_page_lines(page, page_no))

        out.pages_read = limit
        out.alnum_chars = sum(len(_ALNUM.findall(p)) for p in out.pages)

    return out


def _page_lines(page: Any, page_no: int) -> list[dict[str, Any]]:
    """Line-level text with geometry and font metrics, for the ``--layout`` payload.

    One record per rendered line, because the service's plain-text adapter makes one block
    per line and the label-anchored locator depends on that granularity.

    ``size`` is the largest span size on the line and ``bold`` is PyMuPDF's bold flag
    (``span["flags"] & 16``). Neither is a zone: :func:`infer_zones` decides that, later and
    over the whole document, because a font size only means something relative to the rest of
    the page. ``block`` is the index of the PyMuPDF text block the line came from — a
    one-line block is a standalone line rather than a sentence inside a paragraph.
    """
    lines: list[dict[str, Any]] = []
    try:
        data = page.get_text("dict")
    except Exception:  # noqa: BLE001 - lose the geometry, not the page
        return lines

    for block_no, block in enumerate(data.get("blocks", [])):
        if block.get("type") != 0:  # 0 = text, 1 = image
            continue
        block_lines = block.get("lines", [])
        for line in block_lines:
            spans = line.get("spans", [])
            text = "".join(span.get("text", "") for span in spans).strip()
            if not text:
                continue
            bbox = line.get("bbox") or None
            sizes = [float(s.get("size") or 0.0) for s in spans if s.get("text", "").strip()]
            flags = [int(s.get("flags") or 0) for s in spans if s.get("text", "").strip()]
            lines.append(
                {
                    "page": page_no,
                    "text": text,
                    "bbox": list(bbox) if bbox else None,
                    "size": max(sizes) if sizes else 0.0,
                    "bold": bool(flags and all(f & 16 for f in flags)),
                    "block": block_no,
                    "block_lines": len(block_lines),
                }
            )
    return lines


def _quad(bbox: list[float] | None) -> list[float] | None:
    """``[x0, y0, x1, y1]`` -> the 8-float clockwise quad the service's LayoutView uses.

    An 8-float input is already a quad and passes through untouched: Azure Read returns
    ``boundingBox`` in exactly that form, and re-deriving a rectangle from it would throw
    away the rotation a scanned page actually has.
    """
    if not bbox:
        return None
    if len(bbox) >= 8:
        return [float(v) for v in bbox[:8]]
    if len(bbox) < 4:
        return None
    x0, y0, x1, y1 = (float(v) for v in bbox[:4])
    return [x0, y0, x1, y0, x1, y1, x0, y1]


# ---------------------------------------------------------------------------
# Zone inference — an approximation of what production's provider roles say
# ---------------------------------------------------------------------------
def _extent(bbox: list[float] | None) -> tuple[float, float, float, float] | None:
    """``(x0, y0, x1, y1)`` from a 4-float rectangle or an 8-float quad."""
    if not bbox:
        return None
    values = [float(v) for v in bbox]
    if len(values) >= 8:
        xs, ys = values[0:8:2], values[1:8:2]
        return min(xs), min(ys), max(xs), max(ys)
    if len(values) >= 4:
        x0, y0, x1, y1 = values[:4]
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    return None


def _line_metric(line: dict[str, Any]) -> float:
    """How big this line's text is, in whatever unit the payload uses.

    PyMuPDF gives a real font size, which is the honest measure. OCR gives a box and no font
    metadata at all, so the box height stands in for it — a worse proxy (it moves with
    ascenders, descenders and skew), and the report says which of the two was used so nobody
    reads an OCR-derived title as a font-size-derived one.
    """
    size = float(line.get("size") or 0.0)
    if size > 0:
        return size
    extent = _extent(line.get("bbox"))
    return (extent[3] - extent[1]) if extent else 0.0


def _dominant_metric(lines: list[dict[str, Any]]) -> float:
    """The document's body-text size: the character-weighted modal line metric.

    Character-weighted, because a form with forty 6-point footnote lines and eight 10-point
    body lines has 10-point body text; counting lines would say otherwise. Ties go to the
    *smaller* size, which raises the bar for everything promoted above it.
    """
    weights: Counter[float] = Counter()
    for line in lines:
        metric = _line_metric(line)
        if metric > 0:
            weights[round(metric * 2) / 2] += len(str(line.get("text") or ""))
    if not weights:
        return 0.0
    return max(weights, key=lambda size: (weights[size], -size))


def _norm_furniture_key(text: str) -> str:
    """Normalise a line for the "does this repeat on every page" test.

    Digits collapse to ``#`` so ``Page 3 of 12`` and ``Page 4 of 12`` are one running footer
    rather than twelve distinct lines.
    """
    return _DIGITS_RE.sub("#", " ".join(text.split())).casefold()


def infer_zones(pdf: PdfText) -> None:
    """Assign a zone to every line of ``pdf`` from its geometry. Mutates ``pdf``.

    **This is an approximation and is labelled as one everywhere it surfaces.** Production
    does not do this: it reads Azure Document Intelligence's ``paragraphs[].role`` and looks
    it up in ``dce.adapters.ROLE_ZONES``. PyMuPDF has no roles to read, so the choice is
    between inferring them and shipping the previous behaviour, which stamped ``body`` on
    every block and thereby made 34 ``zone=title`` registry anchors unreachable in every
    number this harness has ever produced. An approximation that is named is better than a
    silent falsehood; a *generous* approximation is not, because a title is weighted 3x and
    an invented one inflates the score of whatever doctype it happens to favour. Every rule
    below is therefore written to fail closed:

    * **title** — page 1 only, inside ``TITLE_TOP_FRACTION`` of the page height, a line whose
      metric clears both ``TITLE_SIZE_RATIO`` x the body metric and body + ``TITLE_SIZE_ABS``,
      within ``TITLE_TIE_RATIO`` of the largest such line, at most ``TITLE_MAX_CHARS``
      characters, at most ``TITLE_MAX_LINES`` lines. A document set in one uniform font gets
      no title at all, which is the honest outcome: nothing on that page says "title" except
      the words themselves, and reading the words is the classifier's job, not the harness's.
    * **furniture** — a page-number line in the top/bottom ``FURNITURE_BAND``, or a line whose
      text (digits collapsed) repeats in the same band on at least ``FURNITURE_MIN_PAGES``
      pages *and* on at least half the pages read. Repetition is what makes a running head a
      running head; a lone top-of-page line stays ``body``, where it costs nothing.
    * **heading** — what is left, if it clears ``HEADING_SIZE_RATIO`` x the body metric and is
      at most ``HEADING_MAX_CHARS`` characters. Boldness is recorded but deliberately *not*
      sufficient: bold runs inside a paragraph are common, and no registry anchor is gated to
      ``heading``, so promoting on a weak signal here buys nothing and inflates BM25.
    * **body** — everything else, which on most documents is almost everything.

    Titles are assigned before furniture, so a form whose title also runs as a header on
    later pages keeps its title on page 1 and gets furniture on the rest — which is what a
    DI payload looks like.
    """
    lines = [line for line in pdf.lines if str(line.get("text") or "").strip()]
    for line in lines:
        line["zone"] = ZONE_BODY

    pdf.zone_source = (
        ZONE_SOURCE_OCR_BBOX if pdf.source == SOURCE_OCR else ZONE_SOURCE_PYMUPDF
    )
    if not lines:
        pdf.zone_counts = dict.fromkeys(ZONE_NAMES, 0)
        pdf.zone_basis = "no lines with geometry — nothing to infer from"
        return

    for line in lines:
        line["size_metric"] = _line_metric(line)
    has_font = any(float(line.get("size") or 0.0) > 0 for line in lines)
    metric_kind = "font size (pt)" if has_font else "line-box height (no font metadata)"

    heights = {
        index + 1: float(size[1]) for index, size in enumerate(pdf.page_sizes) if size[1]
    }
    body = _dominant_metric(lines)

    # -- title (first, so a running header cannot steal page 1's title) ------
    title_floor = max(body * TITLE_SIZE_RATIO, body + TITLE_SIZE_ABS) if body > 0 else 0.0
    titles: list[dict[str, Any]] = []
    page_one_height = heights.get(1, 0.0)
    if body > 0 and page_one_height > 0:
        candidates = []
        for line in lines:
            if line["page"] != 1 or line["size_metric"] < title_floor:
                continue
            text = str(line["text"])
            if len(text) > TITLE_MAX_CHARS or not _ALNUM.search(text):
                continue
            extent = _extent(line.get("bbox"))
            if extent is None or extent[1] > TITLE_TOP_FRACTION * page_one_height:
                continue
            candidates.append(line)
        if candidates:
            largest = max(line["size_metric"] for line in candidates)
            winners = [
                line for line in candidates if line["size_metric"] >= largest * TITLE_TIE_RATIO
            ]
            winners.sort(key=lambda line: (_extent(line["bbox"]) or (0, 0, 0, 0))[1])
            titles = winners[:TITLE_MAX_LINES]
            for line in titles:
                line["zone"] = ZONE_TITLE

    # -- furniture ----------------------------------------------------------
    pages_read = max(1, pdf.pages_read or len({line["page"] for line in lines}))
    banded: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for line in lines:
        if line["zone"] == ZONE_TITLE:
            continue
        height = heights.get(line["page"], 0.0)
        extent = _extent(line.get("bbox"))
        if height <= 0 or extent is None:
            continue
        if extent[3] <= FURNITURE_BAND * height:
            band = "top"
        elif extent[1] >= (1.0 - FURNITURE_BAND) * height:
            band = "bottom"
        else:
            continue
        line["_band"] = band
        if _PAGE_NUMBER_RE.match(str(line["text"]).strip()):
            line["zone"] = ZONE_FURNITURE
            continue
        banded.setdefault((band, _norm_furniture_key(str(line["text"]))), []).append(line)

    running = 0
    for group in banded.values():
        pages = {line["page"] for line in group}
        if len(pages) >= FURNITURE_MIN_PAGES and len(pages) * 2 >= pages_read:
            for line in group:
                line["zone"] = ZONE_FURNITURE
                running += 1

    # -- heading ------------------------------------------------------------
    heading_floor = (
        max(body * HEADING_SIZE_RATIO, body + HEADING_SIZE_ABS) if body > 0 else 0.0
    )
    if body > 0:
        for line in lines:
            if line["zone"] != ZONE_BODY:
                continue
            if line["size_metric"] >= heading_floor and len(str(line["text"])) <= (
                HEADING_MAX_CHARS
            ):
                line["zone"] = ZONE_HEADING

    counts = Counter(line["zone"] for line in lines)
    pdf.zone_counts = {name: counts.get(name, 0) for name in ZONE_NAMES}
    pdf.zone_basis = (
        f"{metric_kind}; body={body:g}; title>={title_floor:g} -> {len(titles)} line(s); "
        f"heading>={heading_floor:g} -> {counts.get(ZONE_HEADING, 0)}; "
        f"furniture: {counts.get(ZONE_FURNITURE, 0)} ({running} running head/foot)"
    )


def dump_zones(dump_dir: Path, rel: str, pdf: PdfText) -> None:
    """Write one document's inferred zones, line by line, so a human can audit them.

    Opt-in only, and never into the report directory: this writes document text, and the
    whole point of the inference is that it can be wrong in ways only a person looking at the
    page can see. ``body`` lines are omitted — they are the default and there are thousands.
    """
    target = dump_dir / (rel.replace("/", "__") + ".zones.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    out = [
        f"# inferred zones — {rel}",
        f"# source={pdf.zone_source}  basis={pdf.zone_basis}",
        f"# counts={pdf.zone_counts}",
        "",
    ]
    for line in pdf.lines:
        zone = line.get("zone", ZONE_BODY)
        if zone == ZONE_BODY:
            continue
        out.append(
            f"p{line.get('page', 0)} {zone:<9} {line.get('size_metric', 0):>6.1f}  "
            f"{str(line.get('text') or '')[:120]}"
        )
    target.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# OCR (Azure Computer Vision Read v3.2)
# ---------------------------------------------------------------------------
@dataclass
class OcrConfig:
    """Everything the OCR path needs, resolved once from the CLI and the environment."""

    enabled: bool = False
    endpoint: str = DEFAULT_OCR_ENDPOINT
    key: str = ""
    dpi: int = DEFAULT_OCR_DPI
    max_pages: int = 0
    poll_interval: float = 0.3
    poll_timeout: float = 120.0
    dump_dir: Path | None = None

    @property
    def engine(self) -> str:
        return "azure-read-v3.2"


def rasterize_pages(path: Path, dpi: int, max_pages: int = 0) -> list[dict[str, Any]]:
    """Render a PDF or image to per-page PNG bytes, in page order.

    Mirrors ``des/ocr/raster.py``: ``zoom = dpi / 72``, PyMuPDF opens images as one-page
    documents, and a page that will not render costs that page rather than the document.
    Pages stay in memory — the harness has no reason to leave rendered copies of identity
    documents on disk.

    Raises:
        HarnessError: The document cannot be opened at all, or no page rendered.
    """
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - environment problem
        raise HarnessError("PyMuPDF is not installed") from exc

    filetype = detect_filetype(path) or "pdf"
    try:
        doc = fitz.open(str(path), filetype=filetype)
    except Exception as exc:
        raise HarnessError(f"cannot rasterise {path.name}: {exc}") from exc

    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pages: list[dict[str, Any]] = []
    failures: list[str] = []
    with doc:
        limit = doc.page_count if max_pages <= 0 else min(max_pages, doc.page_count)
        for index in range(limit):
            number = index + 1
            try:
                pix = doc.load_page(index).get_pixmap(matrix=matrix)
                pages.append(
                    {
                        "page": number,
                        "png": pix.tobytes("png"),
                        "width_px": pix.width,
                        "height_px": pix.height,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - a bad page costs that page
                failures.append(f"page {number}: {exc}")

    if not pages:
        detail = "; ".join(failures) or "document has no pages"
        raise HarnessError(f"nothing rendered from {path.name} — {detail}")
    return pages


def azure_read_analyze(png: bytes, cfg: OcrConfig) -> dict[str, Any]:
    """Submit one PNG to Azure Read v3.2 and poll the operation to a terminal status.

    The two-call contract is the real Azure one, copied from
    ``des/ocr/azure_read.py::analyze_bytes`` rather than reinvented, so the same code path
    works against the local mock and against a real Cognitive Services resource:

    1. ``POST {endpoint}/vision/v3.2/read/analyze`` with ``application/octet-stream``
       returns ``202`` and an ``Operation-Location`` header.
    2. ``GET`` that URL until ``status`` is ``succeeded`` or ``failed``.

    The subscription key is sent as ``Ocp-Apim-Subscription-Key`` only when configured;
    the mock needs no key, a real endpoint rejects the call without one.

    Returns:
        The verbatim job JSON.

    Raises:
        HarnessError: On a non-202 submit, a missing ``Operation-Location``, a transport
            failure, or a poll that never reaches a terminal status.
    """
    url = cfg.endpoint.rstrip("/") + AZURE_READ_ANALYZE_PATH
    headers = {"Content-Type": "application/octet-stream"}
    if cfg.key:
        headers["Ocp-Apim-Subscription-Key"] = cfg.key

    request = urllib.request.Request(url, data=png, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=cfg.poll_timeout) as response:
            status = response.status
            operation_url = response.headers.get("Operation-Location")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise HarnessError(f"OCR analyze returned HTTP {exc.code} from {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise HarnessError(f"cannot reach OCR endpoint {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise HarnessError(f"OCR analyze timed out calling {url}") from exc

    if status != 202:
        raise HarnessError(f"OCR analyze returned HTTP {status}, expected 202")
    if not operation_url:
        raise HarnessError("OCR 202 response carried no Operation-Location header")

    poll_headers = {"Accept": "application/json"}
    if cfg.key:
        poll_headers["Ocp-Apim-Subscription-Key"] = cfg.key
    deadline = time.monotonic() + cfg.poll_timeout
    while True:
        if time.monotonic() > deadline:
            raise HarnessError(
                f"OCR polling exceeded {cfg.poll_timeout:.0f}s for {operation_url}"
            )
        time.sleep(cfg.poll_interval)
        poll = urllib.request.Request(operation_url, headers=poll_headers, method="GET")
        try:
            with urllib.request.urlopen(poll, timeout=cfg.poll_timeout) as response:
                job = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise HarnessError(f"OCR poll returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise HarnessError(f"OCR poll cannot reach {operation_url}: {exc.reason}") from exc
        except (TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HarnessError(f"bad OCR poll response: {exc}") from exc
        if not isinstance(job, dict):
            raise HarnessError("OCR poll returned a non-object body")
        if str(job.get("status", "")).lower() in AZURE_READ_TERMINAL:
            return job


def ocr_document(path: Path, cfg: OcrConfig) -> PdfText:
    """Rasterise ``path`` and OCR every page, returning text in the same shape as a PDF.

    Deliberately the *same* :class:`PdfText` a text-layer read produces, so exactly one
    payload builder and one scorer serve both paths — an OCR-only code branch through
    classification would be a second thing to get wrong. The only differences are
    ``source="ocr"``, ``unit="pixel"`` (Azure reports pixel geometry), and a populated
    :class:`OcrOutcome`.

    A page whose OCR fails is recorded as empty and the run continues, matching DES's
    behaviour; the document only fails when every page fails.

    Raises:
        HarnessError: The document cannot be rendered, or every page's OCR failed.
    """
    started = time.perf_counter()
    rendered = rasterize_pages(path, cfg.dpi, cfg.max_pages)

    out = PdfText(source=SOURCE_OCR, unit="pixel", page_count=len(rendered))
    outcome = OcrOutcome(
        engine=cfg.engine, endpoint=cfg.endpoint, dpi=cfg.dpi, pages_sent=len(rendered)
    )

    for rp in rendered:
        page_no = rp["page"]
        try:
            job = azure_read_analyze(rp["png"], cfg)
        except HarnessError as exc:
            outcome.errors.append(f"page {page_no}: {exc}")
            job = {"status": "failed"}

        status = str(job.get("status", "")).lower()
        if status != "succeeded":
            if status == "failed":
                messages = "; ".join(
                    str(e.get("message", ""))
                    for e in (job.get("errors") or [])
                    if isinstance(e, dict)
                )
                detail = f": {messages}" if messages else ""
                outcome.errors.append(f"page {page_no}: OCR job failed{detail}")
            out.pages.append("")
            out.empty_pages.append(page_no)
            out.page_sizes.append((float(rp["width_px"]), float(rp["height_px"])))
            continue

        results = job.get("analyzeResult", {}).get("readResults") or []
        read_result = results[0] if results else {}
        width = float(read_result.get("width") or rp["width_px"])
        height = float(read_result.get("height") or rp["height_px"])
        out.page_sizes.append((width, height))

        texts: list[str] = []
        for line in read_result.get("lines") or []:
            if not isinstance(line, dict):
                continue
            text = str(line.get("text") or "").strip()
            if not text:
                continue
            texts.append(text)
            bbox = line.get("boundingBox") or None
            out.lines.append(
                {
                    "page": page_no,
                    "text": text,
                    "bbox": [float(v) for v in bbox] if bbox else None,
                }
            )

        page_text = "\n".join(texts)
        out.pages.append(page_text)
        if not page_text.strip():
            out.empty_pages.append(page_no)
        outcome.pages_ok += 1

    if outcome.pages_ok == 0:
        detail = outcome.errors[0] if outcome.errors else "every page returned no result"
        raise HarnessError(f"OCR produced nothing for {path.name} — {detail}")

    out.pages_read = len(out.pages)
    out.alnum_chars = sum(len(_ALNUM.findall(p)) for p in out.pages)
    outcome.lines = len(out.lines)
    outcome.chars_per_page = [len(p) for p in out.pages]
    outcome.ms = int((time.perf_counter() - started) * 1000)
    out.ocr = outcome
    return out


def dump_ocr_text(dump_dir: Path, rel: str, pdf: PdfText) -> None:
    """Write one document's OCR text under ``dump_dir``, for debugging OCR quality.

    Opt-in only. Recognised text is the one thing in this tool that can carry a real
    identifier off a real document, so it is never written next to the reports and never
    written unless a path was named on the command line.
    """
    target = dump_dir / (rel.replace("/", "__") + ".txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    header = f"# OCR text — {rel}\n# engine={pdf.ocr.engine if pdf.ocr else '?'}\n\n"
    target.write_text(header + pdf.text + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Azure Document Intelligence sidecars — the production-faithful zone source
# ---------------------------------------------------------------------------
#: A saved DI ``analyzeResult`` for ``corpus/us/us_w9.pdf`` lives at
#: ``corpus/us/us_w9.pdf.di.json``, or at ``<--di-dir>/us_w9.di.json``.
DI_SIDECAR_SUFFIX = ".di.json"


def find_di_sidecar(path: Path, di_dir: Path | None) -> Path | None:
    """The saved Azure DI ``analyzeResult`` for ``path``, if one exists.

    A sidecar is worth looking for on every document because it is the *only* input this
    harness can send that reproduces production's zones. Without one, zones are inferred and
    every number carries that caveat; with one, the service's own ``from_azure_layout`` does
    the mapping and the measurement is production-identical.
    """
    candidates = [path.with_name(path.name + DI_SIDECAR_SUFFIX)]
    if di_dir is not None:
        candidates.append(di_dir / (path.stem + DI_SIDECAR_SUFFIX))
        candidates.append(di_dir / (path.name + DI_SIDECAR_SUFFIX))
    return next((c for c in candidates if c.is_file()), None)


def load_di_sidecar(sidecar: Path) -> tuple[dict[str, Any], PdfText]:
    """Read a DI ``analyzeResult`` and summarise it, without mapping it.

    The payload goes to the service verbatim as ``azure_analyze_result``: the zones that
    reach the classifier are then assigned by ``dce/adapters.py``, exactly as in production,
    and this harness has no opportunity to disagree with it. The :class:`PdfText` returned
    alongside is *only* for the report — page counts, character counts, and the role mix, so
    a reader can see how much title/heading a real DI payload actually carries.

    Raises:
        HarnessError: unreadable, not JSON, or carrying no recognisable content.
    """
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HarnessError(f"cannot read DI sidecar {sidecar.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise HarnessError(f"DI sidecar {sidecar.name} is not a JSON object")

    result = payload.get("analyzeResult")
    result = result if isinstance(result, dict) else payload

    out = PdfText(source=SOURCE_TEXT_LAYER, zone_source=ZONE_SOURCE_DI_ROLES)
    out.unit = "pixel"
    pages = [p for p in (result.get("pages") or []) if isinstance(p, dict)]
    for index, page in enumerate(pages):
        out.page_sizes.append((float(page.get("width") or 0.0), float(page.get("height") or 0.0)))
        if index == 0 and page.get("unit"):
            out.unit = str(page.get("unit"))

    counts: Counter[str] = Counter()
    roles: Counter[str] = Counter()
    by_page: dict[int, list[str]] = {}
    paragraphs = [p for p in (result.get("paragraphs") or []) if isinstance(p, dict)]
    for node in paragraphs:
        text = str(node.get("content") or node.get("text") or "").strip()
        if not text:
            continue
        role = str(node.get("role") or "")
        zone = DI_ROLE_ZONES.get(role, ZONE_BODY)
        roles[role or "(none)"] += 1
        counts[zone] += 1
        regions = node.get("boundingRegions") or []
        page_no = 1
        if regions and isinstance(regions[0], dict):
            page_no = int(regions[0].get("pageNumber") or 1)
        by_page.setdefault(page_no, []).append(text)
        out.lines.append({"page": page_no, "text": text, "zone": zone, "role": role})

    if not out.lines:
        # prebuilt-read shape, or paragraphs stripped: the service falls back to lines, which
        # carry no roles at all. Say so rather than reporting a title count of zero as though
        # DI had considered the question.
        for index, page in enumerate(pages):
            page_no = int(page.get("pageNumber") or index + 1)
            for line in page.get("lines") or []:
                if not isinstance(line, dict):
                    continue
                text = str(line.get("content") or line.get("text") or "").strip()
                if not text:
                    continue
                counts[ZONE_BODY] += 1
                roles["(no paragraphs — lines only)"] += 1
                by_page.setdefault(page_no, []).append(text)
                out.lines.append({"page": page_no, "text": text, "zone": ZONE_BODY, "role": ""})

    if not out.lines:
        raise HarnessError(f"DI sidecar {sidecar.name} carries no paragraphs and no lines")

    out.page_count = len(pages) or len(by_page)
    out.pages_read = out.page_count
    out.pages = ["\n".join(by_page.get(n, [])) for n in sorted(by_page)]
    out.alnum_chars = sum(len(_ALNUM.findall(p)) for p in out.pages)
    out.zone_counts = {name: counts.get(name, 0) for name in ZONE_NAMES}
    mix = ", ".join(f"{role}={n}" for role, n in roles.most_common(6))
    out.zone_basis = f"Azure DI paragraph roles, mapped by the service: {mix}"
    return payload, out


# ---------------------------------------------------------------------------
# The service's own OCR path — asked about, not assumed
# ---------------------------------------------------------------------------
@dataclass
class ServiceOcr:
    """How the **service** reads a document with no text layer, read from ``/readyz``.

    None of this is a constant in the harness, and that is the point. Which recogniser runs
    is a property of the deployment under test: an operator switches providers with an
    environment variable, and a harness carrying its own default would keep reporting the
    provider it was written against long after the service stopped using it. So the default
    is whatever ``/readyz`` says (``ocr.provider``), ``--ocr-provider`` may only name
    something in ``ocr.configured_providers``, and both facts land in the report.

    The pin is always sent explicitly once resolved, even when it equals the default. That
    turns "which engine read this document" from an inference into a contract: the service
    honours the pin or refuses the request with ``ocr_provider_mismatch`` — it never
    substitutes — so the provider recorded in a row is the provider that read the document.
    """

    #: The provider this run pins. Empty when no recogniser is usable here.
    provider: str = ""
    #: What ``/readyz`` reports as ``ocr.provider`` — the one that runs unpinned.
    default_provider: str = ""
    #: Everything ``ocr_provider`` will accept on this deployment.
    configured: tuple[str, ...] = ()
    #: True when :attr:`provider` came from ``--ocr-provider`` rather than from ``/readyz``.
    pinned_by_operator: bool = False
    #: True when a document with no text layer can actually be read here.
    available: bool = False
    #: Whether reading a document with :attr:`provider` is a call to another host, and where.
    network: bool = False
    endpoint_host: str = ""
    #: ``roles`` (paragraph roles survive, so title-gated anchors can fire) or ``lines``.
    structure: str = ""
    #: Why :attr:`available` is false, or why the deployment's own status block is unhappy.
    problem: str = ""


def fetch_service_ocr(
    base_url: str, api_key: str, timeout: float, requested: str = ""
) -> ServiceOcr:
    """Ask ``/readyz`` which recogniser this deployment uses, and resolve the pin against it.

    Never raises: a service too old to publish an ``ocr`` block, or one with no recogniser
    configured, is a *finding* about that deployment and belongs in the report next to the
    documents it could not read — not a traceback that costs the other 150 documents their
    run.

    Args:
        requested: ``--ocr-provider``, or ``""`` to take the deployment's default.

    Returns:
        A :class:`ServiceOcr`. ``available`` is false, with a ``problem`` that says why,
        whenever a document with no text layer cannot be read on this deployment.
    """
    try:
        body = get_json(f"{base_url}/readyz", api_key, min(timeout, 15.0))
    except HarnessError as exc:
        return ServiceOcr(problem=f"cannot read {base_url}/readyz: {exc}")

    block = body.get("ocr")
    if not isinstance(block, dict):
        return ServiceOcr(
            problem=(
                "this service's /readyz carries no 'ocr' block, so it cannot say which "
                "recogniser it uses; documents with no text layer stay unmeasured"
            )
        )

    configured = tuple(str(p) for p in (block.get("configured_providers") or []))
    default = str(block.get("provider") or "")
    rows = {
        str(p.get("name")): p
        for p in (block.get("providers") or [])
        if isinstance(p, dict) and p.get("name")
    }
    wanted = (requested or "").strip().lower()

    if not configured or default in ("", "none"):
        return ServiceOcr(
            default_provider=default,
            configured=configured,
            problem=(
                "this deployment has configured no recogniser (/readyz reports "
                f"ocr.provider={default or 'none'!r}), so it cannot read a document that "
                "carries no text. Documents with no text layer stay unmeasured, which is "
                "the service's own honest answer and not a harness limitation"
            ),
        )
    if wanted and wanted not in configured:
        return ServiceOcr(
            default_provider=default,
            configured=configured,
            pinned_by_operator=True,
            problem=(
                f"--ocr-provider={wanted!r} is not configured on this deployment. /readyz "
                f"lists {', '.join(configured)}. Refusing to substitute: the service would "
                "refuse the pin too, and a run that quietly used a different engine would "
                "put the wrong provider in every row of this report"
            ),
        )

    provider = wanted or default
    row = rows.get(provider) or {}
    problem = str(row.get("reason") or "") if not row.get("available", True) else ""
    return ServiceOcr(
        provider=provider,
        default_provider=default,
        configured=configured,
        pinned_by_operator=bool(wanted),
        available=not problem,
        network=bool(row.get("network")),
        endpoint_host=str(row.get("endpoint") or ""),
        structure=str(row.get("structure") or ""),
        problem=problem,
    )


# ---------------------------------------------------------------------------
# Payloads and HTTP
# ---------------------------------------------------------------------------
def build_ingest_payload(
    doc_id: str, path: Path, limits_mb: int = 0, ocr_provider: str = ""
) -> dict[str, Any]:
    """The request body for the service-side ingestion path.

    The whole file goes over as base64 with ``ingest`` set, and the service decides what it
    is from the bytes. The filename is passed as the hint it is — the service will not let it
    choose a parser — because it is genuinely useful for telling a ``.csv`` from a ``.txt``.

    Args:
        ocr_provider: Sent as ``ingest.ocr_provider`` when non-empty, and only for documents
            that actually need recognising. It is deliberately **not** sent on every request:
            the pin is validated against the deployment even when nothing would be
            recognised, so pinning a provider while uploading a DOCX would turn a
            perfectly readable file into a ``400`` on a deployment with no OCR configured.

    Raises:
        HarnessError: the file cannot be read, or is over ``limits_mb``.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise HarnessError(f"cannot read {path}: {exc}") from exc
    if limits_mb and len(data) > limits_mb * 1024 * 1024:
        raise HarnessError(f"file is {len(data) / 1e6:.1f} MB, over the --ingest-max-mb cap")
    ingest: dict[str, Any] = {"filename": path.name}
    if ocr_provider:
        ingest["ocr_provider"] = ocr_provider
    return {
        "doc_id": doc_id,
        "content_base64": base64.b64encode(data).decode("ascii"),
        "ingest": ingest,
    }


def build_payload(doc_id: str, pdf: PdfText, use_layout: bool) -> dict[str, Any]:
    """The request body for ``/process`` or ``/classify``.

    Default is ``{"doc_id", "text"}`` — the documented degraded path, and the honest floor
    for "what does this service do with nothing but a text layer". The service's
    ``from_plain_text`` puts every block in ``body``, so a plain run cannot reach a
    ``zone=title`` anchor at all; that is a property of the payload, not a harness choice,
    and the report states it.

    ``--layout`` sends a LayoutView-shaped payload: the same lines, with real page geometry,
    page numbers, and **the zones :func:`infer_zones` inferred** — which is an approximation
    of the roles Azure Document Intelligence would have supplied, labelled as one in the
    report. Before v1.2.0 this function hard-coded ``"zone": "body"`` here too, which made
    ``--layout`` and plain text identical as far as zone weighting was concerned and hid 34
    title-gated anchors from every measurement.
    """
    if not use_layout:
        return {"doc_id": doc_id, "text": pdf.text}

    pages = [
        {"page": i + 1, "width": w, "height": h, "unit": pdf.unit, "angle": 0.0}
        for i, (w, h) in enumerate(pdf.page_sizes)
    ]
    blocks = [
        {
            "text": line["text"],
            "zone": line.get("zone", ZONE_BODY),
            "page": line["page"],
            "bbox": _quad(line.get("bbox")),
        }
        for line in pdf.lines
        if line.get("text")
    ]
    provider = (
        f"corpus_test/{pdf.ocr.engine}" if pdf.source == SOURCE_OCR and pdf.ocr
        else "corpus_test/pymupdf"
    )
    return {
        "doc_id": doc_id,
        "layout": {
            "doc_id": doc_id,
            "pages": pages or [{"page": 1}],
            "blocks": blocks,
            "tables": [],
            "marks": [],
            "key_values": [],
            "languages": [],
            "raw": {
                "provider": provider,
                "text_source": pdf.source,
                "tool_version": TOOL_VERSION,
            },
        },
    }


def post_json(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    timeout: float,
    *,
    allow_statuses: frozenset[int] = frozenset(),
) -> tuple[int, dict[str, Any], int]:
    """POST JSON, return ``(status, body, elapsed_ms)``.

    Args:
        allow_statuses: Non-2xx statuses to return as a normal result instead of raising.
            The ingestion path passes ``{422}``: a ``needs_ocr`` refusal is an *answer* about
            the document — the service read the bytes and found no text — not a transport
            failure, and turning it into a HarnessError would put it in the ERROR column
            where it would read as a broken corpus file.

    Raises:
        HarnessError: transport failure, non-JSON body, or a status that is neither 2xx nor
            in ``allow_statuses``.
    """
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        if exc.code in allow_statuses:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            try:
                allowed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                allowed = {}
            return exc.code, allowed if isinstance(allowed, dict) else {}, elapsed_ms
        body = raw.decode("utf-8", "replace")[:500]
        raise HarnessError(f"HTTP {exc.code} from {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise HarnessError(f"cannot reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise HarnessError(f"timed out after {timeout:.0f}s calling {url}") from exc
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"non-JSON response from {url}: {exc}") from exc
    if not isinstance(body, dict):
        raise HarnessError(f"unexpected response shape from {url}: {type(body).__name__}")
    return status, body, elapsed_ms


def get_json(url: str, api_key: str, timeout: float) -> dict[str, Any]:
    """GET JSON. Raises :class:`HarnessError` on anything that is not a JSON object."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HarnessError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise HarnessError(f"cannot reach {url}: {exc.reason}") from exc
    except (TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"bad response from {url}: {exc}") from exc
    if not isinstance(body, dict):
        raise HarnessError(f"unexpected response shape from {url}")
    return body


def fetch_zone_gates(
    base_url: str, api_key: str, timeout: float, doctype_ids: list[str]
) -> dict[str, Any]:
    """Ask the live registry which decisive anchors are gated to which zone.

    This is the audit that would have caught the hard-coded ``"zone": "body"`` on day one.
    An anchor declared ``zone=title`` cannot match a payload with no title zone, so a run
    whose payloads carry no titles is *structurally unable* to fire those anchors — and a
    doctype whose decisive anchors are all title-gated is inaudible on such a run while its
    confusable peers are heard. The report prints these counts next to the zone mix the run
    actually sent, so "we measured with the title channel switched off" can never again be an
    invisible property of the harness.

    Reads ``GET /api/v1/doctypes/{id}`` per doctype: local, cheap, and it means the numbers
    come from the registry that is running rather than from a count someone typed into a
    comment. Never fatal — an older service that does not expose ``zone`` simply yields an
    empty audit.
    """
    gated: dict[str, dict[str, Any]] = {}
    fully_gated: list[str] = []
    errors = 0
    for doctype_id in doctype_ids:
        try:
            spec = get_json(
                f"{base_url}/api/v1/doctypes/{doctype_id}", api_key, min(timeout, 15.0)
            )
        except HarnessError:
            errors += 1
            continue
        anchors = [a for a in (spec.get("anchors") or []) if isinstance(a, dict)]
        decisive = [a for a in anchors if a.get("decisive")]
        if not decisive:
            continue
        zones = [str(a.get("zone")) for a in decisive if a.get("zone")]
        for zone in zones:
            entry = gated.setdefault(zone, {"anchors": 0, "doctypes": []})
            entry["anchors"] += 1
            if doctype_id not in entry["doctypes"]:
                entry["doctypes"].append(doctype_id)
        if zones and len(zones) == len(decisive):
            fully_gated.append(doctype_id)
    return {
        "gated_decisive_anchors": gated,
        "doctypes_wholly_gated": sorted(fully_gated),
        "doctypes_read": len(doctype_ids) - errors,
        "read_errors": errors,
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_document(
    entry: ManifestEntry,
    response: dict[str, Any],
    classify_only: bool,
    show_values: bool,
) -> dict[str, Any]:
    """Turn one service response into a scored per-document record."""
    classification = response if classify_only else (response.get("classification") or {})
    extraction = None if classify_only else response.get("extraction")

    got = str(classification.get("doctype_id") or "unknown")
    abstained = bool(classification.get("abstained")) or got in ("", "unknown")

    if abstained:
        status = STATUS_ABSTAINED
        stated = str(classification.get("reason") or "").strip()
        reason = stated or "service abstained without a stated reason"
    elif got == entry.expected_doctype:
        status = STATUS_CORRECT
        reason = ""
    else:
        status = STATUS_WRONG
        reason = f"expected {entry.expected_doctype}, got {got}"

    runners = []
    for item in (classification.get("runners_up") or [])[:3]:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            runners.append([str(item[0]), _float(item[1])])

    record: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "classification": {
            "doctype_id": got,
            "label": str(classification.get("label") or ""),
            "country": str(classification.get("country") or ""),
            "confidence": _float(classification.get("confidence")),
            "margin": _float(classification.get("margin")),
            "coverage": _float(classification.get("coverage")),
            "abstained": abstained,
            "reason": str(classification.get("reason") or ""),
            "runners_up": runners,
            "page_types": [str(p) for p in (classification.get("page_types") or [])],
            "ms": _int(classification.get("ms")),
            "evidence": [
                {
                    "tier": str(e.get("tier", "")),
                    "detail": str(e.get("detail", ""))[:300],
                    "weight": _float(e.get("weight")),
                }
                for e in (classification.get("evidence") or [])
                if isinstance(e, dict)
            ],
        },
        "extraction": None,
        "needs_review": bool(response.get("needs_review")) if not classify_only else None,
        "detail": str(response.get("detail") or "") if not classify_only else "",
        "tiers_used": response.get("tiers_used") if not classify_only else None,
        "timings": response.get("timings") if not classify_only else None,
    }

    if isinstance(extraction, dict):
        record["extraction"] = _score_extraction(extraction, show_values)
    return record


def _score_extraction(extraction: dict[str, Any], show_values: bool) -> dict[str, Any]:
    """Fill-rate and per-field outcome. Values are withheld unless explicitly asked for."""
    fields_out: list[dict[str, Any]] = []
    filled = 0
    for raw in extraction.get("fields") or []:
        if not isinstance(raw, dict):
            continue
        value = raw.get("value")
        is_filled = value is not None and str(value).strip() != ""
        filled += int(is_filled)
        pii = bool(raw.get("pii"))
        entry: dict[str, Any] = {
            "name": str(raw.get("name", "")),
            "attribute_key": str(raw.get("attribute_key", "")),
            "filled": is_filled,
            "confidence": _float(raw.get("confidence")),
            "verification": str(raw.get("verification", "")),
            "locator": str(raw.get("locator", "")),
            "pii": pii,
            "page": raw.get("page"),
            "validator_error": str(raw.get("validator_error", "")),
        }
        if is_filled:
            # Never write a PII value to a report file, even from a specimen document.
            shown = str(value)[:120] if show_values else None
            entry["value"] = PII_MASK if pii else shown
        fields_out.append(entry)

    total = len(fields_out)
    return {
        "doctype_id": str(extraction.get("doctype_id") or ""),
        "schema_version": str(extraction.get("schema_version") or ""),
        "field_count": total,
        "filled": filled,
        "fill_rate": round(filled / total, 4) if total else 0.0,
        "missing_required": [str(m) for m in (extraction.get("missing_required") or [])],
        "needs_review": bool(extraction.get("needs_review")),
        "ms": _int(extraction.get("ms")),
        "fields": fields_out,
    }


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# The service-side ingestion path
# ---------------------------------------------------------------------------
#: A ``source.note`` from the service that begins this way is the service saying it *read*
#: the document rather than parsed it — the remote wording ("recognised by <engine> at
#: <host>…") and the in-process wording ("recognised in this process by <engine>…") both
#: start here. Used only to **corroborate** what this harness already decided from the bytes,
#: never as the sole basis: if the two disagree the disagreement is recorded in the row.
_RECOGNISED_NOTE_PREFIX = "recognised"


def _service_read(body: dict[str, Any]) -> dict[str, Any]:
    """The service's own account of which reading of the document it scored.

    ``/process`` returns this as ``source``; ``--classify-only`` currently gets ``null``, so
    every caller has to cope with an empty answer rather than assume one.
    """
    source = body.get("source")
    if not isinstance(source, dict):
        return {}
    return {
        "provider": str(source.get("provider") or ""),
        "remote": bool(source.get("remote")),
        "endpoint_host": str(source.get("endpoint_host") or ""),
        "note": str(source.get("note") or "")[:400],
    }


def _unmeasured(record: dict[str, Any], status: str, reason: str) -> None:
    """Record a document that produced no classification, and take its zone source back.

    "Unmeasured" is not a zone source. A row that never reached the classifier must not
    appear in a zone bucket, or a deployment with no recogniser would show a
    ``service_ingest_ocr`` bucket of documents it never read — which is exactly the kind of
    number that gets quoted.
    """
    record["status"] = status
    record["reason"] = reason
    record["zones"] = dict(record.get("zones") or {}, source=None, counts={})


def post_service_ingest(
    entry: ManifestEntry,
    record: dict[str, Any],
    doc_path: Path,
    doc_id: str,
    *,
    base_url: str,
    endpoint: str,
    api_key: str,
    timeout: float,
    max_mb: int,
    classify_only: bool,
    show_values: bool,
    needs_recognition: bool,
    service_ocr: ServiceOcr,
) -> None:
    """Post one file's bytes to the service and score what comes back. Mutates ``record``.

    One function for both reasons a document takes this path, because they are the same
    request and splitting them would be two places to get the reporting wrong:

    * **the file has a text layer this harness cannot read** — a DOCX, an XLSX, an HTML
      filing — and ``dce.ingest`` parses it in-process. ``needs_recognition`` is False, no
      provider is pinned, and the row lands in ``service_ingest``.
    * **the file has no text at all** — a JPEG, or a PDF whose pages are pictures. Then
      ``needs_recognition`` is True, the resolved provider is pinned, the service's own OCR
      path reads it, and the row lands in ``service_ingest_ocr``, apart from every text-layer
      rate in the report.

    A ``422`` is the service saying there was nothing to read; a ``400`` from a document that
    needed recognising is the recogniser failing on it. Neither is a classification, so both
    stay out of every rate — the first has always been ``needs_ocr`` here and the second joins
    it, because scoring "the OCR engine choked" as an abstention would credit the classifier
    with a decision it never made. A ``400`` that is ``ocr_provider_mismatch`` is the one
    exception: that is this harness pinning a provider the deployment does not have, a fault
    in the run rather than in the document, and it stays an ERROR so it cannot be mistaken
    for a property of the corpus.
    """
    provider = service_ocr.provider if needs_recognition else ""
    record["text_source"] = SOURCE_INGEST_OCR if needs_recognition else SOURCE_INGEST
    record["zones"] = {
        "source": ZONE_SOURCE_INGEST_OCR if needs_recognition else ZONE_SOURCE_INGEST,
        "counts": {},
        "basis": (
            (
                f"dce.ingest handed the file to {provider} inside the service and mapped what "
                f"came back with the service's own adapters ({service_ocr.structure or '?'} "
                "structure); this harness saw neither the text nor the zones"
            )
            if needs_recognition
            else (
                "dce.ingest parsed the file inside the service and mapped the format's "
                "own stated structure onto the zone model; this harness saw no text"
            )
        ),
        "payload": "content_base64+ingest",
        "sidecar": "",
    }
    record["service_ocr"] = {
        "requested_provider": provider,
        "needs_recognition": needs_recognition,
        "network": service_ocr.network if needs_recognition else False,
        "endpoint_host": service_ocr.endpoint_host if needs_recognition else "",
        "structure": service_ocr.structure if needs_recognition else "",
        "pinned_by_operator": service_ocr.pinned_by_operator if needs_recognition else False,
        "reported_by_service": {},
        "agrees_with_service": None,
    }

    if needs_recognition and not service_ocr.available:
        # Nothing was sent. The deployment cannot read this document, and saying so is a
        # measurement — of the deployment — rather than a gap in the harness.
        _unmeasured(
            record,
            STATUS_NEEDS_OCR,
            "no usable text layer, and the service cannot recognise it: "
            f"{service_ocr.problem or 'no recogniser is available on this deployment'}",
        )
        return

    try:
        payload = build_ingest_payload(doc_id, doc_path, max_mb, provider)
        status, body, elapsed = post_json(
            f"{base_url}{endpoint}",
            payload,
            api_key,
            timeout,
            allow_statuses=frozenset({400, 422}),
        )
    except HarnessError as exc:
        _unmeasured(record, STATUS_ERROR, str(exc))
        return

    record["http"] = {"status": status, "elapsed_ms": elapsed}
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}

    if status == 422:
        # The service read the bytes and found no text. Same bucket as a scanned PDF —
        # unmeasured, not broken — but with the service's own reason attached instead of
        # the harness's guess at one.
        _unmeasured(
            record,
            STATUS_NEEDS_OCR,
            str(detail.get("reason") or "the service returned 422 without a reason"),
        )
        record["ingest"] = detail
        return

    if status == 400:
        code = str(detail.get("error") or "")
        text = str(detail.get("detail") or body.get("detail") or "")[:400]
        record["ingest"] = detail
        if needs_recognition and code != "ocr_provider_mismatch":
            _unmeasured(
                record,
                STATUS_NEEDS_OCR,
                f"the service's recogniser ({provider}) could not read it "
                f"— {code or 'HTTP 400'}: {text}",
            )
        else:
            _unmeasured(
                record,
                STATUS_ERROR,
                f"HTTP 400 from the service ({code or 'no code'}): {text}",
            )
        return

    record.update(score_document(entry, body, classify_only, show_values))

    # What the SERVICE says it did, next to what this harness expected it to do. They should
    # agree; when they do not, the row says so rather than one of them silently winning. The
    # honest reading of a disagreement is that this harness's "no usable text layer" floor
    # (60 alphanumeric characters) and the service's own (40, in dce/ingest/pdf.py) are not
    # the same number, so a document between the two is parsed by the service and merely
    # *expected* to be recognised here.
    reported = _service_read(body)
    record["service_ocr"]["reported_by_service"] = reported
    if reported:
        recognised = reported["remote"] or reported["note"].strip().lower().startswith(
            _RECOGNISED_NOTE_PREFIX
        )
        record["service_ocr"]["agrees_with_service"] = recognised == needs_recognition
        record["text_source"] = SOURCE_INGEST_OCR if recognised else SOURCE_INGEST
        record["zones"]["source"] = (
            ZONE_SOURCE_INGEST_OCR if recognised else ZONE_SOURCE_INGEST
        )
        if recognised != needs_recognition:
            record["zones"]["basis"] = (
                f"{record['zones']['basis']} — NOTE: the service reports it "
                f"{'recognised' if recognised else 'parsed a text layer from'} this document, "
                "which is not what this harness expected; the service's account wins and this "
                "row is bucketed by it"
            )
    else:
        record["text_source"] = SOURCE_INGEST_OCR if needs_recognition else SOURCE_INGEST


#: Text sources that are not a reading at all. A document nothing could read is attributed to
#: no reader — filing six unread scans under "PyMuPDF" would make PyMuPDF look like it had
#: failed on them, when in truth it was never asked.
_UNREAD_READER = "(unread — needs_ocr / error)"


def _reader_of(document: dict[str, Any]) -> str:
    """Which engine produced the text this document was scored on, named in full.

    The provider is part of the measurement, not a footnote: ``azure_layout`` returns
    paragraph roles and ``azure_read`` does not, so the same document scored through the two
    is two different experiments. Every table that carries a rate carries this next to it.
    """
    if document.get("status") not in SCORED_STATUSES:
        return _UNREAD_READER
    source = document.get("text_source")
    if source == SOURCE_OCR:
        engine = (document.get("ocr") or {}).get("engine") or "?"
        return f"harness OCR — {engine}"
    if source == SOURCE_INGEST_OCR:
        service = document.get("service_ocr") or {}
        provider = (
            service.get("requested_provider")
            or (service.get("reported_by_service") or {}).get("provider")
            or "?"
        )
        return f"service OCR — {provider}"
    if source == SOURCE_INGEST:
        return "dce.ingest — the file's own text"
    return "PyMuPDF — the file's own text layer"


def _reader_short(document: dict[str, Any]) -> str:
    """The same fact, narrow enough for a table column. Always names the OCR provider."""
    source = document.get("text_source")
    if source == SOURCE_OCR:
        return f"OCR/{(document.get('ocr') or {}).get('engine') or '?'}"
    if source == SOURCE_INGEST_OCR:
        service = document.get("service_ocr") or {}
        provider = (
            service.get("requested_provider")
            or (service.get("reported_by_service") or {}).get("provider")
            or "?"
        )
        return f"OCR/{provider}"
    if source == SOURCE_INGEST:
        return "ingest"
    return "text"


def summarise(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts and the two accuracies: overall, per country, per text source, per zone source.

    ``text_layer``, ``service_ingest``, ``ocr`` and ``service_ingest_ocr`` are reported as
    separate buckets and not merely as a breakdown of ``overall``. A run that switches on
    either OCR path adds documents whose text came from a recognition engine, so its
    ``overall`` is not comparable with a previous run's; its ``text_layer`` bucket is,
    exactly. Regression checks belong there.

    ``by_reader`` is the same split one level finer, keyed by the engine that actually
    produced the text. Two OCR providers in one run are two experiments, and a single
    ``service_ingest_ocr`` rate covering both would answer neither.

    ``by_zone_source`` splits on the same principle and for a stronger reason. A number
    produced with real Azure DI roles says something about production; the same number
    produced with zones this harness inferred from font sizes says something about this
    harness's heuristic *and* production, inseparably; and a number produced with no zones at
    all was measured with the title channel switched off. Averaging those three would produce
    a figure that answers no question anybody has.
    """

    def bucket(docs: list[dict[str, Any]]) -> dict[str, Any]:
        counts = Counter(d["status"] for d in docs)
        correct = counts[STATUS_CORRECT]
        wrong = counts[STATUS_WRONG]
        abstained = counts[STATUS_ABSTAINED]
        scored = correct + wrong + abstained
        answered = correct + wrong
        return {
            "documents": len(docs),
            "scored": scored,
            "correct": correct,
            "wrong": wrong,
            "abstained": abstained,
            "needs_ocr": counts[STATUS_NEEDS_OCR],
            "errors": counts[STATUS_ERROR],
            # Over everything the classifier actually saw.
            "accuracy": round(correct / scored, 4) if scored else 0.0,
            # Over the documents it was willing to answer on — abstentions excluded.
            "precision_when_answered": round(correct / answered, 4) if answered else 0.0,
            "abstention_rate": round(abstained / scored, 4) if scored else 0.0,
        }

    by_country: dict[str, Any] = {}
    for country in sorted({d["country"] for d in documents}):
        by_country[country] = bucket([d for d in documents if d["country"] == country])

    by_source: dict[str, Any] = {}
    for source in (SOURCE_TEXT_LAYER, SOURCE_INGEST, SOURCE_OCR, SOURCE_INGEST_OCR):
        subset = [d for d in documents if d.get("text_source") == source]
        if subset:
            by_source[source] = bucket(subset)

    # One row per *reader*, which is the finer question the text-source split cannot answer.
    # "service_ingest_ocr 12 documents, 9 correct" is only actionable once you know whether
    # azure_layout or rapidocr read them: they are different products with different ceilings,
    # and a run may use more than one. So each reader gets its own count and its own rate.
    by_reader: dict[str, Any] = {}
    for reader in sorted({_reader_of(d) for d in documents}):
        by_reader[reader] = bucket([d for d in documents if _reader_of(d) == reader])

    by_zone: dict[str, Any] = {}
    for source in (
        ZONE_SOURCE_DI_ROLES,
        ZONE_SOURCE_PYMUPDF,
        ZONE_SOURCE_OCR_BBOX,
        ZONE_SOURCE_INGEST,
        ZONE_SOURCE_INGEST_OCR,
        ZONE_SOURCE_NONE,
    ):
        subset = [d for d in documents if (d.get("zones") or {}).get("source") == source]
        if subset:
            by_zone[source] = bucket(subset)

    return {
        "overall": bucket(documents),
        "by_country": by_country,
        "by_text_source": by_source,
        "by_reader": by_reader,
        "by_zone_source": by_zone,
    }


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def render_markdown(report: dict[str, Any]) -> str:
    """The human table. Every section states what it means, including the empty ones."""
    docs: list[dict[str, Any]] = report["documents"]
    summary = report["summary"]
    overall = summary["overall"]
    service = report["service"]
    lines: list[str] = []
    add = lines.append

    add("# DCE corpus results")
    add("")
    add(f"- **Generated**: {report['generated_at']}")
    add(f"- **Service**: `{service['url']}` — {service['doctype_count']} doctypes in registry")
    add(f"- **Mode**: `{service['endpoint']}` · payload `{service['payload']}`")
    zones_block = report.get("zones") or {}
    used = zones_block.get("sources_used") or {}
    measured = {k: v for k, v in used.items() if k != "unmeasured"}
    if measured:
        add(
            "- **Zone source**: "
            + " · ".join(f"`{name}` x{count}" for name, count in sorted(measured.items()))
            + (
                ""
                if set(zones_block.get("sources_scored") or {}) <= ZONE_SOURCES_REAL
                else "  ← **not production-faithful**, see *Zones* below"
            )
        )
    ocr_cfg = report.get("ocr") or {}
    if ocr_cfg.get("enabled"):
        auth = "keyed" if ocr_cfg.get("authenticated") else "unauthenticated"
        add(
            f"- **OCR**: on — `{ocr_cfg.get('engine')}` at `{ocr_cfg.get('endpoint')}` "
            f"· {ocr_cfg.get('dpi')} dpi · {auth}"
        )
        if _is_local_endpoint(str(ocr_cfg.get("endpoint") or "")):
            add(
                "- **OCR provider warning**: that endpoint is local. The repo's mock speaks "
                "the real Azure Read v3.2 contract and does real recognition (Tesseract), "
                "so these are genuine results — but its Tesseract carries only the `eng` "
                "traineddata. **Any document whose text is primarily Devanagari, or any "
                "other non-Latin script, comes back as transliteration noise, and its "
                "result measures the mock, not the classifier.** Real Azure Read recognises "
                "those scripts; point `--ocr-endpoint`/`--ocr-key` at a real resource "
                "before drawing conclusions about non-Latin documents."
            )
    elif not (report.get("service_ocr") or {}).get("enabled"):
        add("- **OCR**: off — documents with no text layer were skipped, not guessed")
    svc_ocr = report.get("service_ocr") or {}
    if svc_ocr.get("enabled"):
        where = (
            f" at `{svc_ocr.get('endpoint_host')}`"
            if svc_ocr.get("network") and svc_ocr.get("endpoint_host")
            else " in the service process"
        )
        chosen = (
            "pinned with `--ocr-provider`"
            if svc_ocr.get("pinned_by_operator")
            else "this deployment's default, from `/readyz`"
        )
        add(
            f"- **Service OCR**: on — documents with no text layer were read by "
            f"`{svc_ocr.get('provider')}`{where} ({chosen}, `{svc_ocr.get('structure')}` "
            "structure). The service did the reading, not this harness."
        )
        if svc_ocr.get("problem"):
            add(f"- **Service OCR problem**: {svc_ocr['problem']}")
    elif svc_ocr.get("consulted"):
        add(
            "- **Service OCR**: unavailable — "
            + str(svc_ocr.get("problem") or "no recogniser is configured on this deployment")
        )
    add(f"- **Corpus**: `{report['corpus_root']}`")
    filters = report["filters"]
    if filters["country"] or filters["only"]:
        add(
            f"- **Filters**: country={filters['country'] or 'all'} · "
            f"only={filters['only'] or 'all'}"
        )
    add(f"- **Harness**: `tools/corpus_test.py` v{report['tool_version']}")
    add("")

    add("## Overall")
    add("")
    add("| metric | value |")
    add("| --- | --- |")
    add(f"| documents in manifests | {overall['documents']} |")
    add(f"| sent to the service | {overall['scored']} |")
    add(f"| CORRECT | {overall['correct']} |")
    add(f"| WRONG | {overall['wrong']} |")
    add(f"| ABSTAINED | {overall['abstained']} |")
    add(f"| needs OCR (skipped) | {overall['needs_ocr']} |")
    add(f"| errors (skipped) | {overall['errors']} |")
    add(f"| **accuracy** (correct / sent) | **{_pct(overall['accuracy'])}** |")
    answered = _pct(overall["precision_when_answered"])
    add(f"| precision when it answered (correct / non-abstained) | {answered} |")
    add(f"| abstention rate | {_pct(overall['abstention_rate'])} |")
    add("")
    add(
        "*Accuracy counts only documents that reached the classifier. `needs_ocr` and error "
        "documents are excluded from every rate and listed in full below — they are missing "
        "measurements, not results.*"
    )
    add("")

    add(_zones_section(report))

    by_source = summary.get("by_text_source") or {}
    if len(by_source) > 1:
        add("## By text source — read this before the overall number")
        add("")
        add(
            "OCR error is a confound. A wrong doctype on an OCR'd scan may be the "
            "classifier's fault or the recognition engine's, and this harness cannot tell "
            "you which. The two are therefore never averaged into one claim about the "
            "classifier. **Compare a run against the `text_layer` row of another run, "
            "never against `overall`** — `overall` moves whenever `--ocr` is toggled."
        )
        add("")
        add(
            "| text source | docs | sent | correct | wrong | abstained | needs OCR | errors "
            "| accuracy | precision |"
        )
        add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for source, stats in by_source.items():
            add(
                f"| `{source}` | {stats['documents']} | {stats['scored']} "
                f"| {stats['correct']} | {stats['wrong']} | {stats['abstained']} "
                f"| {stats['needs_ocr']} | {stats['errors']} | {_pct(stats['accuracy'])} "
                f"| {_pct(stats['precision_when_answered'])} |"
            )
        add("")
        add(
            "`text_layer` is PyMuPDF reading the publisher's own text. `service_ingest` is "
            "`dce.ingest` doing the same job inside the service for a format this harness "
            "cannot read. `ocr` is **this harness** rasterising pages and calling an OCR "
            "endpoint. `service_ingest_ocr` is **the service** handing a document with no "
            "text at all to the recogniser its operator configured — the production path, "
            "and the only one of the four whose result is evidence about how production "
            "reads a scan."
        )
        add("")

    by_reader = summary.get("by_reader") or {}
    if len([r for r in by_reader if r != _UNREAD_READER]) > 1:
        add("## By reader — which engine produced the text")
        add("")
        add(
            "One row per engine that actually read documents in this run. `azure_layout` and "
            "`azure_read` are different products with different ceilings — Read predicts no "
            "paragraph roles, so a Read payload can never satisfy a title-gated decisive "
            "anchor — and an in-process engine is different again. A single OCR rate spanning "
            "them would answer no question about any of them."
        )
        add("")
        add(
            "| reader | docs | sent | correct | wrong | abstained | needs OCR | errors "
            "| accuracy | precision |"
        )
        add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for reader, stats in by_reader.items():
            add(
                f"| {reader} | {stats['documents']} | {stats['scored']} "
                f"| {stats['correct']} | {stats['wrong']} | {stats['abstained']} "
                f"| {stats['needs_ocr']} | {stats['errors']} | {_pct(stats['accuracy'])} "
                f"| {_pct(stats['precision_when_answered'])} |"
            )
        add("")

    add("## By country")
    add("")
    add(
        "| country | docs | sent | correct | wrong | abstained | needs OCR | errors "
        "| accuracy | precision |"
    )
    add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for country, stats in summary["by_country"].items():
        add(
            f"| {country.upper()} | {stats['documents']} | {stats['scored']} "
            f"| {stats['correct']} | {stats['wrong']} | {stats['abstained']} "
            f"| {stats['needs_ocr']} | {stats['errors']} | {_pct(stats['accuracy'])} "
            f"| {_pct(stats['precision_when_answered'])} |"
        )
    add("")

    # ---- confusions -------------------------------------------------------
    wrongs = [d for d in docs if d["status"] == STATUS_WRONG]
    add("## Confusions (WRONG)")
    add("")
    if not wrongs:
        add("None — every document the classifier answered on, it got right.")
    else:
        add("| file | src | expected | got | conf | margin | cov | runners-up (top 3) |")
        add("| --- | --- | --- | --- | ---: | ---: | ---: | --- |")
        for d in wrongs:
            c = d["classification"]
            add(
                f"| `{d['file']}` | {_reader_short(d)} | `{d['expected_doctype']}` "
                f"| `{c['doctype_id']}` "
                f"| {c['confidence']:.2f} | {c['margin']:.2f} | {c['coverage']:.2f} "
                f"| {_runners(c['runners_up'])} |"
            )
        if any(d.get("text_source") in SOURCES_RECOGNISED for d in wrongs):
            add("")
            add(
                "*`OCR/...` rows may be misrecognition rather than misclassification, and the "
                "engine named in `src` is the one to suspect first. Read the recognised text "
                "before counting one as a classifier defect.*"
            )
    add("")

    # ---- abstentions ------------------------------------------------------
    abstentions = [d for d in docs if d["status"] == STATUS_ABSTAINED]
    add("## Abstentions")
    add("")
    if not abstentions:
        add("None.")
    else:
        add("| file | src | expected | reason | conf | margin | cov | runners-up (top 3) |")
        add("| --- | --- | --- | --- | ---: | ---: | ---: | --- |")
        for d in abstentions:
            c = d["classification"]
            reason = (c["reason"] or d["reason"]).replace("|", "/")
            add(
                f"| `{d['file']}` | {_reader_short(d)} | `{d['expected_doctype']}` | {reason} "
                f"| {c['confidence']:.2f} | {c['margin']:.2f} | {c['coverage']:.2f} "
                f"| {_runners(c['runners_up'])} |"
            )
    add("")

    # ---- OCR'd documents --------------------------------------------------
    ocr_docs = [d for d in docs if d.get("text_source") in SOURCES_RECOGNISED]
    if ocr_docs:
        add("## Recognised documents (measured, but through an OCR engine)")
        add("")
        add(
            "These had no text layer of their own — scans and photo IDs, the least-tested "
            "path in the service and the one production sees most. Their text came from a "
            "recognition engine, so **every result here carries OCR error**. A WRONG row is "
            "a lead, not a verdict: check the recognised text before blaming the classifier, "
            "and check it again before clearing it. **`read by` is the first column to "
            "look at** — a `service OCR` row is the service's own ingestion path doing what "
            "it does in production, while a `harness OCR` row is this tool's rasteriser and "
            "endpoint standing in for it, and the two are not the same measurement."
        )
        add("")
        add(
            "| file | read by | expected | status | got | conf | margin | cov | pages "
            "| lines/blocks | chars | ms |"
        )
        add(
            "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
        )
        for d in sorted(ocr_docs, key=lambda x: (x["country"], x["file"])):
            c = d.get("classification") or {}
            pdf = d.get("pdf") or {}
            o = d.get("ocr") or {}
            if d.get("text_source") == SOURCE_OCR:
                pages = f"{o.get('pages_ok', 0)}/{o.get('pages_sent', 0)}"
                extra = f"{o.get('lines', 0)} | {pdf.get('chars', 0)} | {o.get('ms', 0)}"
            else:
                # The service read it, so this harness has no line count or character count
                # to report — it never saw the text. Saying "0" would read as "found nothing".
                pages = str(pdf.get("page_count", 0) or "—")
                extra = f"— | — | {(d.get('http') or {}).get('elapsed_ms', 0)}"
            add(
                f"| `{d['file']}` | {_reader_short(d)} | `{d['expected_doctype']}` "
                f"| {d['status']} "
                f"| `{c.get('doctype_id', '—')}` | {c.get('confidence', 0):.2f} "
                f"| {c.get('margin', 0):.2f} | {c.get('coverage', 0):.2f} | {pages} "
                f"| {extra} |"
            )
        add("")
        add(
            "*`lines/blocks`, `chars` and `ms` are dashes on `service OCR` rows on purpose: "
            "the service read the document and this harness never saw the text, so it has "
            "nothing to count. `ms` there is the whole round trip, recognition included.*"
        )
        add("")
        disagreed = [
            d
            for d in ocr_docs
            if (d.get("service_ocr") or {}).get("agrees_with_service") is False
        ]
        if disagreed:
            add(
                "The service reported a different reading from the one this harness expected "
                "on these — the harness's no-text-layer floor is 60 alphanumeric characters "
                "and the service's is 40, so a document between the two is parsed rather than "
                "recognised. The service's account is what the rows above are bucketed by:"
            )
            add("")
            for d in disagreed:
                note = (d.get("service_ocr") or {}).get("reported_by_service") or {}
                add(f"- `{d['file']}` — {note.get('note', '')}")
            add("")
        failed = [(d, d.get("ocr") or {}) for d in ocr_docs if (d.get("ocr") or {}).get("errors")]
        if failed:
            add("Pages the OCR engine could not read:")
            add("")
            for d, o in failed:
                for err in o["errors"][:5]:
                    add(f"- `{d['file']}` — {err}")
            add("")

    # ---- needs OCR --------------------------------------------------------
    ocr = [d for d in docs if d["status"] == STATUS_NEEDS_OCR]
    add("## Needs OCR (not measured)")
    add("")
    if not ocr:
        add(
            "None — every document either had a usable text layer or was successfully "
            "OCR'd."
            if ocr_docs
            else "None — every PDF in the corpus had a usable text layer."
        )
    else:
        recognition_ran = bool(report.get("ocr", {}).get("enabled")) or bool(
            report.get("service_ocr", {}).get("enabled")
        )
        add(
            "These have no text layer, so nothing classifiable was ever produced for them. "
            "Rerun with `--ingest` to have the **service** read them with the recogniser its "
            "operator configured — the production path — or with `--ocr` to have this harness "
            "rasterise and recognise them itself. Without one of those, guessing here would "
            "produce numbers that mean nothing."
            if not recognition_ran
            else "These reached a recogniser and still produced nothing classifiable, or the "
            "deployment had no recogniser to offer. They remain unmeasured — an OCR failure "
            "is not a classifier result and is not scored as one. The `note` column says "
            "which of the two it was, and names the engine."
        )
        add("")
        add("| file | expected | pages | alnum chars | note |")
        add("| --- | --- | ---: | ---: | --- |")
        for d in ocr:
            pdf = d.get("pdf") or {}
            add(
                f"| `{d['file']}` | `{d['expected_doctype']}` | {pdf.get('page_count', 0)} "
                f"| {pdf.get('alnum_chars', 0)} | {d['reason'].replace('|', '/')} |"
            )
    add("")

    # ---- errors -----------------------------------------------------------
    errors = [d for d in docs if d["status"] == STATUS_ERROR]
    add("## Errors (not measured)")
    add("")
    if not errors:
        add("None.")
    else:
        add("| file | expected | detail |")
        add("| --- | --- | --- |")
        for d in errors:
            add(f"| `{d['file']}` | `{d['expected_doctype']}` | {d['reason'].replace('|', '/')} |")
    add("")

    # ---- field fill -------------------------------------------------------
    corrects = [d for d in docs if d["status"] == STATUS_CORRECT and d.get("extraction")]
    add("## Extraction fill-rate (correct classifications only)")
    add("")
    if not corrects:
        add(
            "No fill-rate to report — either nothing classified correctly, or the run used "
            "`--classify-only`."
        )
    else:
        add(
            "Fill-rate is filled fields / fields in the doctype schema, T1-local unless the "
            "service has paid tiers switched on. Blank forms fill few fields by design: an "
            "empty specimen has no name to find, so a low rate here is not automatically a "
            "locator bug."
        )
        add("")
        add("| file | doctype | filled / total | fill-rate | missing required | review |")
        add("| --- | --- | ---: | ---: | --- | :---: |")
        for d in corrects:
            ex = d["extraction"]
            missing = ", ".join(f"`{m}`" for m in ex["missing_required"]) or "—"
            add(
                f"| `{d['file']}` | `{ex['doctype_id']}` | {ex['filled']} / {ex['field_count']} "
                f"| {_pct(ex['fill_rate'])} | {missing} "
                f"| {'yes' if d.get('needs_review') else 'no'} |"
            )
        add("")

        add("### Fields most often missing (across correct classifications)")
        add("")
        misses: Counter[str] = Counter()
        seen: Counter[str] = Counter()
        for d in corrects:
            for f in d["extraction"]["fields"]:
                key = f"{d['extraction']['doctype_id']}.{f['name']}"
                seen[key] += 1
                if not f["filled"]:
                    misses[key] += 1
        if not misses:
            add("None — every field filled on every correctly classified document.")
        else:
            add("| doctype.field | missed / seen |")
            add("| --- | ---: |")
            for key, count in misses.most_common(30):
                add(f"| `{key}` | {count} / {seen[key]} |")
    add("")

    # ---- inventory --------------------------------------------------------
    add("## All documents")
    add("")
    add("`src` is where the text came from and, when a recogniser produced it, **which "
        "one**: `text` = the publisher's own text layer read here by PyMuPDF, `ingest` = "
        "the same, read inside the service by `dce.ingest`, `OCR/<provider>` = recognised, "
        "and any result on an `OCR/` row carries that provider's error. "
        "`zones` is where that document's zones came from — `di` = real provider roles, "
        "`geo` = inferred from PDF geometry, `geo-ocr` = inferred from OCR boxes, "
        "`ingest` = the format's own structure, `ingest-ocr` = the recogniser's, `none` = "
        "plain text, everything `body`. `t/h/f` counts the title, heading and furniture "
        "blocks actually sent — dashes where the service assigned the zones and this "
        "harness never saw them.")
    add("")
    add("| file | expected | status | src | zones | t/h/f | got | conf | margin | cov "
        "| pages | chars |")
    add("| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for d in sorted(docs, key=lambda x: (x["country"], x["file"])):
        c = d.get("classification") or {}
        pdf = d.get("pdf") or {}
        got = c.get("doctype_id", "—")
        zones = d.get("zones") or {}
        counts = zones.get("counts") or {}
        thf = (
            f"{counts.get(ZONE_TITLE, 0)}/{counts.get(ZONE_HEADING, 0)}/"
            f"{counts.get(ZONE_FURNITURE, 0)}"
            if zones.get("source") and counts
            else "—"
        )
        add(
            f"| `{d['file']}` | `{d['expected_doctype']}` | {d['status']} | {_reader_short(d)} "
            f"| {_ZONE_SHORT.get(zones.get('source') or '', '—')} | {thf} | `{got}` "
            f"| {c.get('confidence', 0):.2f} | {c.get('margin', 0):.2f} "
            f"| {c.get('coverage', 0):.2f} | {pdf.get('page_count', 0)} "
            f"| {pdf.get('chars', 0)} |"
        )
    add("")

    if report["manifest_errors"]:
        add("## Manifest problems")
        add("")
        add("| manifest | line | detail |")
        add("| --- | ---: | --- |")
        for err in report["manifest_errors"]:
            detail = err["detail"].replace("|", "/")
            add(f"| `{err['manifest']}` | {err['line_no']} | {detail} |")
        add("")

    if report["unknown_doctypes"]:
        add("## Expected doctypes not in the registry")
        add("")
        add(
            "These manifest entries name an `expected_doctype` the service does not know, so "
            "they can never score CORRECT. Fix the manifest — the IDs come from "
            "`GET /api/v1/doctypes`."
        )
        add("")
        for item in report["unknown_doctypes"]:
            add(f"- `{item['expected_doctype']}` — `{item['file']}`")
        add("")

    add("---")
    add("")
    add(
        "Field *values* are deliberately absent from this report. Run with `--show-values` "
        "to include non-PII values; fields the registry marks PII stay masked either way."
    )
    add("")
    return "\n".join(lines)


#: Column-width abbreviations for the inventory table. The full name is in every summary.
_ZONE_SHORT: dict[str, str] = {
    ZONE_SOURCE_DI_ROLES: "di",
    ZONE_SOURCE_PYMUPDF: "geo",
    ZONE_SOURCE_OCR_BBOX: "geo-ocr",
    ZONE_SOURCE_INGEST: "ingest",
    ZONE_SOURCE_INGEST_OCR: "ingest-ocr",
    ZONE_SOURCE_NONE: "none",
}

#: One line per zone source, saying plainly what a number measured with it is worth.
_ZONE_SOURCE_NOTES: dict[str, str] = {
    ZONE_SOURCE_DI_ROLES: (
        "**Production-faithful.** A saved Azure Document Intelligence `analyzeResult` was "
        "posted verbatim and the service's own `dce/adapters.py` assigned the zones from "
        "`paragraphs[].role`, exactly as it does in production. Numbers in this bucket are "
        "evidence about the service."
    ),
    ZONE_SOURCE_PYMUPDF: (
        "**Approximation.** PyMuPDF supplies no roles, so zones were *inferred* from font "
        "size, page position and line length (`infer_zones`, thresholds below, not calibrated "
        "against this corpus). Numbers in this bucket are evidence about the service **and** "
        "about the heuristic, inseparably. A title the heuristic misses costs recall a real "
        "DI payload would have had; a title it invents is weighted 3x and inflates whatever "
        "doctype it favours."
    ),
    ZONE_SOURCE_OCR_BBOX: (
        "**Weaker approximation.** Same inference, but over OCR line boxes, which carry no "
        "font metadata at all — box height stands in for font size. Compounded with OCR "
        "error."
    ),
    ZONE_SOURCE_INGEST: (
        "**Production-faithful.** The file was posted as bytes and `dce/ingest/` parsed it "
        "inside the service, mapping the format's own stated structure — a DOCX `Title` "
        "style, an HTML `<h1>`, a PPTX title placeholder, an EML `Subject`, a running "
        "header — onto the zone model, exactly as it does in production. Nothing here is "
        "inferred from geometry and nothing is this harness's opinion. The caveat is a "
        "different one: for formats that state no structure (a PDF text layer, a TXT file, "
        "OCR output) the service labels everything `body` on purpose, so those documents "
        "carry no title channel in this bucket either."
    ),
    ZONE_SOURCE_INGEST_OCR: (
        "**Production-faithful zones, recognised text.** The file carried no text at all, so "
        "`dce/ingest/` handed it to the recogniser this deployment configured and mapped the "
        "answer with the service's own adapters — exactly what production does with a scan. "
        "Two caveats, and they pull in opposite directions. What zones exist depends on the "
        "provider: `azure_layout` returns paragraph roles and reaches `from_azure_layout`, so "
        "title-gated anchors can fire; `azure_read` and the in-process engines return lines "
        "only and everything is `body`. And the text underneath is recognised text, so every "
        "number in this bucket carries OCR error as well — see the reader table, which names "
        "the engine."
    ),
    ZONE_SOURCE_NONE: (
        "**No zones at all.** The plain-text payload makes the service label every block "
        "`body` (`from_plain_text`). Every zone-gated anchor in the registry was unreachable "
        "for the whole run. A number from this bucket is a floor, not a result."
    ),
}


def _zones_section(report: dict[str, Any]) -> str:
    """The section that says who assigned this run's zones — and what that is worth.

    Printed before every breakdown except the headline counts, because zone source is the
    single largest lever on the numbers underneath it: it changes the lexical weight of every
    term and decides whether a zone-gated anchor can fire at all.
    """
    block = report.get("zones") or {}
    summary = report.get("summary") or {}
    by_zone = summary.get("by_zone_source") or {}
    used = {k: v for k, v in (block.get("sources_used") or {}).items() if k != "unmeasured"}
    out: list[str] = ["## Zones — where these numbers came from", ""]

    out.append(
        "Zone drives lexical weighting (`title` 3x, `heading` 2x, `furniture` 0.25x) and "
        "gates anchors outright: an anchor declared `zone=title` cannot match a payload with "
        "no title. **Until harness v1.2.0 every block this tool sent was stamped "
        "`\"zone\": \"body\"`, in both plain and `--layout` mode, so every zone-gated anchor "
        "was unreachable in every number it ever produced.** Zone source is therefore "
        "reported per document and bucketed here, and the buckets are never averaged."
    )
    out.append("")

    if not used:
        out.append("No document reached the classifier, so no zones were sent.")
        out.append("")
        return "\n".join(out)

    out.append("| zone source | docs | sent | correct | wrong | abstained | accuracy "
               "| precision |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for source, stats in by_zone.items():
        out.append(
            f"| `{source}` | {stats['documents']} | {stats['scored']} | {stats['correct']} "
            f"| {stats['wrong']} | {stats['abstained']} | {_pct(stats['accuracy'])} "
            f"| {_pct(stats['precision_when_answered'])} |"
        )
    out.append("")
    for source in used:
        note = _ZONE_SOURCE_NOTES.get(source)
        if note:
            out.append(f"- `{source}` — {note}")
    out.append("")

    scored_sources = set(report.get("zones", {}).get("sources_scored") or used)
    if not scored_sources <= ZONE_SOURCES_REAL:
        out.append(
            "> **Do not quote any accuracy in this report as a production figure.** No "
            "bucket above is `azure_di_roles` except where the table says so. To produce a "
            "production-faithful number, save real Azure DI `analyzeResult` payloads beside "
            "the documents as `<file>.di.json` (or point `--di-dir` at them) and re-run."
            if not (scored_sources & ZONE_SOURCES_REAL)
            else "> Rows above are a mix of production-faithful and approximated zone "
            "sources. Read them separately; the `overall` figure spans both and answers no "
            "single question."
        )
        out.append("")

    # -- what the registry gates --------------------------------------------
    gates = block.get("registry_gates") or {}
    gated = gates.get("gated_decisive_anchors") or {}
    if gated:
        out.append("### Zone-gated decisive anchors in the live registry")
        out.append("")
        out.append(
            "Read from `GET /api/v1/doctypes/{id}` on the service under test, not from a "
            "constant in this file. An anchor here can only fire on a payload that carries "
            "its zone."
        )
        out.append("")
        out.append("| gated to zone | decisive anchors | doctypes | reachable this run |")
        out.append("| --- | ---: | ---: | --- |")
        for zone, stats in sorted(gated.items()):
            carried = [
                source
                for source in used
                if any(
                    (d.get("zones") or {}).get("source") == source
                    and ((d.get("zones") or {}).get("counts") or {}).get(zone, 0) > 0
                    for d in report["documents"]
                )
            ]
            docs_with = sum(
                1
                for d in report["documents"]
                if ((d.get("zones") or {}).get("counts") or {}).get(zone, 0) > 0
            )
            reach = (
                f"yes — on {docs_with} of {sum(used.values())} documents sent"
                if carried
                else "**no — this run carried no such zone on any document**"
            )
            out.append(
                f"| `{zone}` | {stats['anchors']} | {len(stats['doctypes'])} | {reach} |"
            )
        out.append("")
        wholly = gates.get("doctypes_wholly_gated") or []
        if wholly:
            out.append(
                f"{len(wholly)} doctype(s) have **every** decisive anchor zone-gated, so on a "
                "payload without that zone they are structurally silent while their "
                "confusable peers are heard: "
                + ", ".join(f"`{d}`" for d in wholly[:20])
                + (" …" if len(wholly) > 20 else "")
            )
            out.append("")

    # -- the inference's own settings ---------------------------------------
    if ZONE_SOURCE_PYMUPDF in used or ZONE_SOURCE_OCR_BBOX in used:
        inference = block.get("inference") or {}
        out.append("### Inference thresholds (a priori, not calibrated on this corpus)")
        out.append("")
        out.append(
            "These were chosen from what a title and a running header look like on a page, "
            "before this corpus was run, and were not adjusted afterwards. Tuning them until "
            "67 documents scored well would make the instrument a function of its own corpus."
        )
        out.append("")
        out.append("| setting | value |")
        out.append("| --- | ---: |")
        for key, value in inference.items():
            out.append(f"| `{key}` | {value} |")
        out.append("")
        out.append(
            "Audit the inference on real pages with `--zone-dump-dir DIR`: it writes every "
            "promoted line per document so a human can check them against the document. "
            "Under-promotion is the intended failure mode."
        )
        out.append("")
    return "\n".join(out)


def _is_local_endpoint(url: str) -> bool:
    """True for a loopback OCR endpoint — i.e. almost certainly the repo's mock."""
    lowered = url.lower()
    return any(host in lowered for host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _runners(runners: list[list[Any]]) -> str:
    if not runners:
        return "—"
    return ", ".join(f"`{r[0]}` {float(r[1]):.2f}" for r in runners)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="corpus_test.py",
        description="Score a running DCE service against the document corpus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit status is always 0 — this measures, it does not gate.\n"
            "Reports land in reports/corpus-results.{json,md}."
        ),
    )
    add = parser.add_argument
    add("--corpus-root", type=Path, default=DEFAULT_CORPUS_ROOT,
        help="directory holding corpus/<cc>/ (default: %(default)s)")
    add("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
        help="where the two reports are written (default: %(default)s)")
    add("--url", default=DEFAULT_BASE_URL,
        help="DCE base URL (default: %(default)s, or $DCE_URL)")
    add("--api-key", default=os.environ.get("DCE_API_KEY", ""),
        help="X-API-Key, if the service requires one (or $DCE_API_KEY)")
    add("--only", default="",
        help="comma-separated doctype_ids to run (default: all)")
    add("--country", default="",
        help="comma-separated country codes to run, e.g. us,in (default: all)")
    add("--classify-only", action="store_true",
        help="POST /api/v1/classify instead of /api/v1/process")
    add("--layout", action="store_true",
        help="send a LayoutView payload with page geometry and INFERRED zones (title/heading/"
             "furniture) instead of plain text; the inference is an approximation of the "
             "provider roles production reads, and every report says so")
    add("--show-values", action="store_true",
        help="include non-PII extracted values in the reports (PII stays masked)")
    add("--max-pages", type=int, default=0,
        help="read at most N pages per PDF (0 = all)")
    add("--timeout", type=float, default=120.0,
        help="per-request timeout in seconds (default: %(default)s)")
    add("-v", "--verbose", action="store_true",
        help="one line per document as it runs, with reasons and runners-up")

    zones = parser.add_argument_group(
        "zones",
        "Zone (title / heading / body / table / furniture) drives lexical weighting and gates "
        "34 registry anchors outright, so where a run's zones came from is as important as "
        "its accuracy. A saved Azure Document Intelligence analyzeResult is the only "
        "production-faithful source; --layout infers zones from geometry instead; plain text "
        "has none. All three are reported separately and never averaged.",
    )
    zones.add_argument("--di-dir", type=Path, default=None,
        help="directory of saved Azure DI analyzeResult sidecars (<stem>.di.json). Sidecars "
             "found next to the document (<file>.di.json) are used with or without this")
    zones.add_argument("--no-di-sidecar", action="store_true",
        help="ignore DI sidecars even when present — measures the degraded path deliberately")
    zones.add_argument("--zone-dump-dir", type=Path, default=None,
        help="write the inferred non-body zones per document here so a human can check them "
             "against the actual page; writes document text, so never a checked-in directory")
    zones.add_argument("--no-zone-audit", action="store_true",
        help="skip the per-doctype registry read that reports which decisive anchors are "
             "zone-gated (one local GET per doctype)")

    ocr = parser.add_argument_group(
        "OCR (off by default)",
        "Rasterise documents with no text layer — and images, which have none by "
        "definition — and recognise text with an Azure Read v3.2 endpoint before "
        "classifying. Results are reported in their own bucket because OCR error is a "
        "confound. Without --ocr nothing here has any effect and the run is byte-for-byte "
        "the run it was before.",
    )
    ocr.add_argument("--ocr", action="store_true",
        help="OCR documents that have no usable text layer, instead of skipping them")
    ocr.add_argument("--ocr-endpoint", default=DEFAULT_OCR_ENDPOINT,
        help="Azure Read v3.2 base URL (default: %(default)s, or $AZURE_VISION_ENDPOINT)")
    ocr.add_argument("--ocr-key", default=DEFAULT_OCR_KEY,
        help="Ocp-Apim-Subscription-Key; omit for a local mock (or $AZURE_VISION_KEY)")
    ocr.add_argument("--ocr-dpi", type=int, default=DEFAULT_OCR_DPI,
        help="raster resolution before OCR (default: %(default)s)")
    ocr.add_argument("--ocr-max-pages", type=int, default=0,
        help="OCR at most N pages per document (0 = all); bounds cost on a paid endpoint")
    ocr.add_argument("--ocr-dump-dir", type=Path, default=None,
        help="write raw OCR text per document here for debugging — NEVER point this at a "
             "checked-in directory; recognised text can carry real identifiers")

    ingest = parser.add_argument_group(
        "service-side ingestion (off by default)",
        "Send corpus files to the service as raw bytes and let dce.ingest read them "
        "in-process. This is the only way to score a .docx, .xlsx, .pptx, .odt, .rtf, .csv, "
        ".html, .eml, .msg or an image, none of which this harness can read itself — AND "
        "the only way to measure a scan the way production measures one, because the "
        "service's own OCR path reads it. A PDF with a text layer is unaffected: it keeps "
        "the PyMuPDF path, because that is what --layout's zone inference measures. A PDF "
        "with none no longer stays NEEDS_OCR: it goes to the service like any other "
        "unreadable file. Whether the service can actually read it is the deployment's "
        "business — if it has configured no recogniser, the documents stay NEEDS_OCR with "
        "the service's own reason, which is a measurement of that deployment.",
    )
    ingest.add_argument("--ingest", action="store_true",
        help="POST files this harness cannot read as content_base64 with ingest set — "
             "non-PDF formats, images, and PDFs with no text layer — instead of skipping them")
    ingest.add_argument("--ingest-max-mb", type=int, default=24,
        help="skip files larger than this before uploading (default: %(default)s; 0 = no cap)")
    ingest.add_argument("--ocr-provider", default="",
        help="which recogniser the SERVICE should use for documents with no text layer, e.g. "
             "azure_layout, azure_read, rapidocr. Default: whatever /readyz reports as this "
             "deployment's configured provider — never a constant in this harness. It can "
             "only name a provider /readyz already lists; it cannot switch one on. Requires "
             "--ingest")
    return parser.parse_args(argv)


def _csv_set(value: str) -> set[str]:
    return {v.strip().lower() for v in value.split(",") if v.strip()}


def run(args: argparse.Namespace) -> int:
    global _DISPLAY_ROOT

    corpus_root: Path = args.corpus_root.expanduser().resolve()
    out_dir: Path = args.out_dir.expanduser().resolve()
    _DISPLAY_ROOT = corpus_root.parent
    base_url = args.url.rstrip("/")
    endpoint = "/api/v1/classify" if args.classify_only else "/api/v1/process"

    print(f"corpus_test v{TOOL_VERSION} — {base_url}{endpoint}")
    print(f"corpus: {corpus_root}")

    if not corpus_root.exists():
        print(f"\nNo corpus at {corpus_root}.")
        print("Create it as corpus/<cc>/manifest.jsonl — see tools/README-corpus.md.")
        return 0

    entries, manifest_errors = read_manifests(corpus_root)
    for err in manifest_errors:
        print(f"  manifest problem: {err.manifest}:{err.line_no} — {err.detail}")

    if not entries:
        found = sorted(p.name for p in corpus_root.glob("*") if p.is_dir())
        print(f"\nNo usable manifest entries under {corpus_root}.")
        print(f"Country directories present: {', '.join(found) or 'none'}")
        print("Each needs a manifest.jsonl with one JSON object per document —")
        print('  {"file": "corpus/us/us_w9.pdf", "expected_doctype": "us_w9", '
              '"source_url": "...", "kind": "blank_form"}')
        print("See tools/README-corpus.md. Nothing to measure; exiting 0.")
        return 0

    try:
        import fitz  # noqa: F401 - presence check only; one clear message beats N identical ones
    except ImportError:
        print(f"\n{len(entries)} document(s) found, but PyMuPDF is not installed.")
        print("  uv pip install pymupdf      (or: pip install pymupdf)")
        print("Nothing measured; exiting 0.")
        return 0

    only = _csv_set(args.only)
    countries = _csv_set(args.country)
    selected = [
        e
        for e in entries
        if (not countries or e.country in countries)
        and (not only or e.expected_doctype.lower() in only)
    ]
    filtered_out = len(entries) - len(selected)
    if not selected:
        print(f"\n{len(entries)} manifest entries, none matched the filters "
              f"(country={args.country or 'all'}, only={args.only or 'all'}). Nothing to do.")
        return 0

    # -- service preflight --------------------------------------------------
    registry_ids: set[str] = set()
    doctype_count = 0
    try:
        health = get_json(f"{base_url}/health", args.api_key, min(args.timeout, 15.0))
        doctypes = get_json(f"{base_url}/api/v1/doctypes", args.api_key, min(args.timeout, 30.0))
        registry_ids = {
            str(d.get("doctype_id")) for d in doctypes.get("doctypes", []) if isinstance(d, dict)
        }
        doctype_count = _int(doctypes.get("count"), len(registry_ids))
        print(f"service: ok ({health.get('status', '?')}) — {doctype_count} doctypes loaded")
    except HarnessError as exc:
        print(f"\nCannot talk to the service: {exc}")
        print("Start it, or point --url elsewhere. Nothing measured; exiting 0.")
        return 0

    unknown = [
        {
            "file": e.file,
            "expected_doctype": e.expected_doctype,
            "manifest": e.manifest,
            "line_no": e.line_no,
        }
        for e in selected
        if registry_ids and e.expected_doctype not in registry_ids
    ]
    for item in unknown:
        print(f"  warning: '{item['expected_doctype']}' is not a registry doctype_id "
              f"({item['manifest']}:{item['line_no']}) — it can never score CORRECT")

    # -- zone preflight -----------------------------------------------------
    zone_gates: dict[str, Any] = {}
    if not args.no_zone_audit and registry_ids:
        zone_gates = fetch_zone_gates(base_url, args.api_key, args.timeout, sorted(registry_ids))
        for zone, stats in sorted((zone_gates.get("gated_decisive_anchors") or {}).items()):
            print(
                f"registry: {stats['anchors']} decisive anchor(s) across "
                f"{len(stats['doctypes'])} doctype(s) are gated to zone '{zone}' — they can "
                f"only fire on a payload that carries that zone"
            )
    di_dir: Path | None = args.di_dir.expanduser().resolve() if args.di_dir else None
    zone_dump_dir: Path | None = (
        args.zone_dump_dir.expanduser().resolve() if args.zone_dump_dir else None
    )
    if not args.layout and not args.no_di_sidecar:
        print(
            "zones: plain-text payload — the service labels every block 'body', so any "
            "zone-gated anchor is unreachable in this run. Use --layout for inferred zones, "
            "or supply Azure DI sidecars for production-faithful ones."
        )
    if zone_dump_dir:
        print(f"zones: dumping inferred zones to {zone_dump_dir} — do not commit it")

    # -- OCR preflight ------------------------------------------------------
    ocr_cfg = OcrConfig(
        enabled=bool(args.ocr),
        endpoint=str(args.ocr_endpoint).rstrip("/"),
        key=str(args.ocr_key or ""),
        dpi=int(args.ocr_dpi),
        max_pages=int(args.ocr_max_pages),
        poll_timeout=float(args.timeout),
        dump_dir=(args.ocr_dump_dir.expanduser().resolve() if args.ocr_dump_dir else None),
    )
    if ocr_cfg.enabled:
        keyed = "with a subscription key" if ocr_cfg.key else "unauthenticated (local mock?)"
        print(f"ocr: on — {ocr_cfg.engine} at {ocr_cfg.endpoint}, {ocr_cfg.dpi} dpi, {keyed}")
        if _is_local_endpoint(ocr_cfg.endpoint):
            print("ocr: local endpoint — the mock's Tesseract is English-only; non-Latin "
                  "scripts come back as noise and their results measure the mock, not DCE")
        if ocr_cfg.dump_dir:
            print(f"ocr: dumping recognised text to {ocr_cfg.dump_dir} — do not commit it")

    # -- service-side ingestion preflight ------------------------------------
    # Which recogniser reads a scan is a property of the DEPLOYMENT, so it is asked for
    # rather than assumed. This is the only place the provider is decided, and it is decided
    # once per run so every row can name it.
    service_ocr = ServiceOcr()
    non_pdf = sum(1 for e in selected if e.path and detect_filetype(e.path) != "pdf")
    if args.ingest:
        service_ocr = fetch_service_ocr(
            base_url, args.api_key, args.timeout, args.ocr_provider
        )
        print(
            f"ingest: on — {non_pdf} file(s) this harness cannot read will be parsed by the "
            "service (dce.ingest), which assigns their zones from the format's own structure"
        )
        if service_ocr.available:
            where = (
                f"{service_ocr.endpoint_host or 'the configured endpoint'} (a call out of "
                "the service process)"
                if service_ocr.network
                else "in the service process"
            )
            chosen = (
                "pinned with --ocr-provider"
                if service_ocr.pinned_by_operator
                else f"this deployment's default from /readyz (ocr.provider="
                f"{service_ocr.default_provider})"
            )
            print(
                f"ingest: documents with no text layer will be read by the SERVICE using "
                f"{service_ocr.provider} at {where} — {chosen}"
            )
            if service_ocr.structure != "roles":
                print(
                    f"ingest: {service_ocr.provider} returns '{service_ocr.structure}' and no "
                    "paragraph roles, so the service labels every block 'body' for these "
                    "documents and no zone-gated anchor can fire on them"
                )
        else:
            print(f"ingest: documents with no text layer stay unmeasured — {service_ocr.problem}")
        if ocr_cfg.enabled:
            print(
                "ocr: --ocr and --ingest are both on. --ingest wins for every document with "
                "no text layer, so the service reads all of them and no document in this run "
                "is read by two different engines. --ocr affects nothing here"
            )
    else:
        if args.ocr_provider:
            print(
                f"ocr-provider: --ocr-provider={args.ocr_provider} was given without "
                "--ingest, so it selects nothing — the service only reads a document this "
                "harness hands it as bytes. Ignored"
            )
        if non_pdf:
            print(
                f"ingest: off — {non_pdf} non-PDF file(s) cannot be read by this harness and "
                "will be reported unmeasured; use --ingest to have the service parse them"
            )

    tail = f", {filtered_out} filtered out" if filtered_out else ""
    print(f"running {len(selected)} document(s){tail}")
    print("")

    # -- the run ------------------------------------------------------------
    documents: list[dict[str, Any]] = []
    for index, entry in enumerate(selected, start=1):
        rel = _relpath(entry.path) if entry.path else entry.file
        record: dict[str, Any] = {
            "file": rel,
            "path": str(entry.path) if entry.path else "",
            "country": entry.country,
            "expected_doctype": entry.expected_doctype,
            "expected_in_registry": (
                (entry.expected_doctype in registry_ids) if registry_ids else None
            ),
            "kind": entry.kind,
            "source_url": entry.source_url,
            "notes": entry.notes,
            "manifest": entry.manifest,
            "line_no": entry.line_no,
            "status": STATUS_ERROR,
            "reason": "",
            # Every document is a text-layer measurement until an OCR engine is what
            # actually produced its text. Documents that never reached the classifier keep
            # this value, so the text_layer bucket stays identical to a no-OCR run.
            "text_source": SOURCE_TEXT_LAYER,
            # Where this document's zones came from, filled in below. A document that never
            # reached the classifier keeps ``source: null`` and lands in no zone bucket,
            # because "unmeasured" is not a zone source.
            "zones": {"source": None, "counts": {}, "basis": "", "payload": ""},
            "pdf": None,
            "ocr": None,
            "classification": None,
            "extraction": None,
            "http": None,
        }
        doc_path = entry.path or Path(entry.file)

        # -- Azure DI sidecar: the one input that reproduces production's zones ------
        sidecar = None if args.no_di_sidecar else find_di_sidecar(doc_path, di_dir)
        di_payload: dict[str, Any] | None = None
        if sidecar is not None:
            try:
                di_payload, pdf = load_di_sidecar(sidecar)
            except HarnessError as exc:
                # A broken sidecar must not silently downgrade the run to the inferred path:
                # that would turn "production-faithful" into "not, and nobody said".
                record["reason"] = f"DI sidecar unusable ({_relpath(sidecar)}): {exc}"
                documents.append(record)
                _log(args.verbose, index, len(selected), record)
                continue

        # -- service-side ingestion: everything this harness cannot read itself ------
        # A PDF *with a text layer* keeps this harness's own PyMuPDF path, because that is
        # what carries the zone *inference* --layout exists to measure. Everything else — a
        # .docx, an .xlsx, an .eml, the two passport JPEGs — has no harness path at all and
        # goes to the service, which parses it in-process and assigns the zones itself. A
        # file whose bytes are an image needs recognising by definition, so the resolved
        # provider is pinned on the request; the rest are parsed, not read, and are sent
        # without a pin.
        filetype = detect_filetype(doc_path)
        if di_payload is None and args.ingest and filetype != "pdf":
            record["pdf"] = {
                "bytes": doc_path.stat().st_size if doc_path.exists() else 0,
                "filetype": filetype or "",
                "is_image": filetype not in (None, "pdf"),
                "page_count": 0,
                "pages_read": 0,
                "chars": 0,
                "alnum_chars": 0,
                "chars_per_page": [],
                "empty_pages": [],
                "lines": 0,
            }
            post_service_ingest(
                entry,
                record,
                doc_path,
                rel or entry.file,
                base_url=base_url,
                endpoint=endpoint,
                api_key=args.api_key,
                timeout=args.timeout,
                max_mb=args.ingest_max_mb,
                classify_only=args.classify_only,
                show_values=args.show_values,
                # An image carries no text layer in the same way a scan carries none; that
                # is the definition of the format, not a judgement about this file.
                needs_recognition=filetype is not None,
                service_ocr=service_ocr,
            )
            documents.append(record)
            _log(args.verbose, index, len(selected), record)
            continue

        if di_payload is None:
            try:
                pdf = load_pdf_text(doc_path, args.max_pages, allow_images=ocr_cfg.enabled)
            except NeedsOcrError as exc:
                # An image, or something else with no text layer to read. Same bucket as a
                # scanned PDF: unmeasured, not broken.
                record["status"] = STATUS_NEEDS_OCR
                record["reason"] = str(exc)
                record["pdf"] = {
                    "bytes": doc_path.stat().st_size if doc_path.exists() else 0,
                    "filetype": detect_filetype(doc_path) or "",
                    "is_image": True,
                    "page_count": 0,
                    "pages_read": 0,
                    "chars": 0,
                    "alnum_chars": 0,
                    "chars_per_page": [],
                    "empty_pages": [],
                    "lines": 0,
                }
                documents.append(record)
                _log(args.verbose, index, len(selected), record)
                continue
            except HarnessError as exc:
                record["reason"] = str(exc)
                documents.append(record)
                _log(args.verbose, index, len(selected), record)
                continue

        exists = bool(entry.path and entry.path.exists())
        record["pdf"] = {
            "bytes": entry.path.stat().st_size if exists and entry.path else 0,
            "filetype": pdf.filetype,
            "is_image": pdf.is_image,
            "page_count": pdf.page_count,
            "pages_read": pdf.pages_read,
            "chars": len(pdf.text),
            "alnum_chars": pdf.alnum_chars,
            "chars_per_page": pdf.chars_per_page,
            "empty_pages": pdf.empty_pages,
            "lines": len(pdf.lines),
        }

        # A DI sidecar has already been through a recognition engine; there is no second OCR
        # pass to make, and its own line count is the provider's answer about this document.
        if di_payload is None and pdf.alnum_chars < MIN_ALNUM_CHARS:
            if args.ingest:
                # A scan, and --ingest is on: send the PDF whole and let the SERVICE read it
                # with the recogniser its operator configured. This is the production path —
                # both Azure products take a PDF natively, so nothing is rasterised anywhere
                # — and it is why these six documents stop being a permanent hole in every
                # rate. --ingest wins over --ocr here deliberately: one rule for every
                # document with no text layer means a run never has a scanned PDF read by one
                # engine and a JPEG by another.
                record["pdf"]["text_layer_alnum_chars"] = pdf.alnum_chars
                post_service_ingest(
                    entry,
                    record,
                    doc_path,
                    rel or entry.file,
                    base_url=base_url,
                    endpoint=endpoint,
                    api_key=args.api_key,
                    timeout=args.timeout,
                    max_mb=args.ingest_max_mb,
                    classify_only=args.classify_only,
                    show_values=args.show_values,
                    needs_recognition=True,
                    service_ocr=service_ocr,
                )
                documents.append(record)
                _log(args.verbose, index, len(selected), record)
                continue

            if not ocr_cfg.enabled:
                record["status"] = STATUS_NEEDS_OCR
                record["reason"] = (
                    f"no usable text layer ({pdf.alnum_chars} alphanumeric chars over "
                    f"{pdf.pages_read} page(s)) — this is a scan; skipped rather than guessed"
                )
                documents.append(record)
                _log(args.verbose, index, len(selected), record)
                continue

            text_layer_chars = pdf.alnum_chars
            try:
                pdf = ocr_document(doc_path, ocr_cfg)
            except HarnessError as exc:
                # An OCR failure is an OCR failure, not a classification result. It stays
                # out of every rate rather than being scored as an abstention.
                record["status"] = STATUS_NEEDS_OCR
                record["reason"] = f"OCR failed: {exc}"
                documents.append(record)
                _log(args.verbose, index, len(selected), record)
                continue

            record["text_source"] = SOURCE_OCR
            record["ocr"] = asdict(pdf.ocr) if pdf.ocr else None
            record["pdf"].update(
                {
                    "text_layer_alnum_chars": text_layer_chars,
                    "page_count": pdf.page_count,
                    "pages_read": pdf.pages_read,
                    "chars": len(pdf.text),
                    "alnum_chars": pdf.alnum_chars,
                    "chars_per_page": pdf.chars_per_page,
                    "empty_pages": pdf.empty_pages,
                    "lines": len(pdf.lines),
                }
            )
            if ocr_cfg.dump_dir:
                with contextlib.suppress(OSError):
                    dump_ocr_text(ocr_cfg.dump_dir, rel or entry.file, pdf)

            if pdf.alnum_chars < MIN_ALNUM_CHARS:
                record["status"] = STATUS_NEEDS_OCR
                record["reason"] = (
                    f"OCR recognised only {pdf.alnum_chars} alphanumeric chars over "
                    f"{pdf.pages_read} page(s) — not enough to classify on; still unmeasured"
                )
                documents.append(record)
                _log(args.verbose, index, len(selected), record)
                continue

        # -- zones --------------------------------------------------------------
        if di_payload is not None:
            # Verbatim. The service's own adapter assigns the zones, which is the whole point:
            # this path measures production's mapping, not the harness's opinion of it.
            payload: dict[str, Any] = {
                "doc_id": rel or entry.file,
                "azure_analyze_result": di_payload,
            }
        else:
            if args.layout:
                infer_zones(pdf)
                if zone_dump_dir:
                    with contextlib.suppress(OSError):
                        dump_zones(zone_dump_dir, rel or entry.file, pdf)
            else:
                pdf.zone_source = ZONE_SOURCE_NONE
                pdf.zone_counts = {
                    ZONE_BODY: sum(1 for line in pdf.text.splitlines() if line.strip())
                }
                pdf.zone_basis = (
                    "plain-text payload — dce.adapters.from_plain_text labels every block "
                    "body; no zone-gated anchor can fire"
                )
            payload = build_payload(rel or entry.file, pdf, args.layout)

        record["zones"] = {
            "source": pdf.zone_source,
            "counts": dict(pdf.zone_counts),
            "basis": pdf.zone_basis,
            "payload": (
                "azure_analyze_result" if di_payload is not None
                else ("layout" if args.layout else "text")
            ),
            "sidecar": _relpath(sidecar) if sidecar is not None else "",
        }

        try:
            status, body, elapsed = post_json(
                f"{base_url}{endpoint}", payload, args.api_key, args.timeout
            )
        except HarnessError as exc:
            record["reason"] = str(exc)
            documents.append(record)
            _log(args.verbose, index, len(selected), record)
            continue

        record["http"] = {"status": status, "elapsed_ms": elapsed}
        record.update(score_document(entry, body, args.classify_only, args.show_values))
        documents.append(record)
        _log(args.verbose, index, len(selected), record)

    # -- reports ------------------------------------------------------------
    summary = summarise(documents)
    report = {
        "tool_version": TOOL_VERSION,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ"),
        "corpus_root": str(corpus_root),
        "service": {
            "url": base_url,
            "endpoint": endpoint,
            "payload": "layout" if args.layout else "text",
            "doctype_count": doctype_count,
        },
        "ingest": {
            "enabled": bool(args.ingest),
            "max_mb": int(args.ingest_max_mb),
            "documents": sum(
                1
                for d in documents
                if d.get("text_source") in (SOURCE_INGEST, SOURCE_INGEST_OCR)
            ),
            "parsed": sum(1 for d in documents if d.get("text_source") == SOURCE_INGEST),
            "recognised": sum(
                1 for d in documents if d.get("text_source") == SOURCE_INGEST_OCR
            ),
        },
        # How the SERVICE read the documents that carried no text, asked of /readyz rather
        # than assumed here. ``requested`` is what the operator typed; every other field is
        # the deployment's own answer, so a report can be read years later without anyone
        # having to remember which provider was default at the time.
        "service_ocr": {
            "consulted": bool(args.ingest),
            "enabled": bool(args.ingest) and service_ocr.available,
            "requested": args.ocr_provider,
            "provider": service_ocr.provider,
            "default_provider": service_ocr.default_provider,
            "configured_providers": list(service_ocr.configured),
            "pinned_by_operator": service_ocr.pinned_by_operator,
            "network": service_ocr.network,
            "endpoint_host": service_ocr.endpoint_host,
            "structure": service_ocr.structure,
            "problem": service_ocr.problem,
            "documents": sum(
                1 for d in documents if d.get("text_source") == SOURCE_INGEST_OCR
            ),
        },
        "ocr": {
            "enabled": ocr_cfg.enabled,
            "engine": ocr_cfg.engine if ocr_cfg.enabled else "",
            "endpoint": ocr_cfg.endpoint if ocr_cfg.enabled else "",
            "authenticated": bool(ocr_cfg.key) if ocr_cfg.enabled else False,
            "dpi": ocr_cfg.dpi if ocr_cfg.enabled else 0,
            "max_pages": ocr_cfg.max_pages,
        },
        "zones": {
            # What this run asked for...
            "requested": (
                "azure_di_sidecars_then_inferred" if not args.no_di_sidecar and args.layout
                else "azure_di_sidecars_then_plain_text" if not args.no_di_sidecar
                else "inferred" if args.layout
                else "none"
            ),
            # ...and what it actually sent, per document, counted. This is the line to read
            # before any accuracy number in this file.
            "sources_used": dict(
                Counter(
                    (d.get("zones") or {}).get("source") or "unmeasured" for d in documents
                )
            ),
            # ...and, separately, the sources that produced a document anybody scored. This
            # is the one the honesty guards read: a zone source attached to a document the
            # classifier never saw says nothing about any number in this report.
            "sources_scored": dict(
                Counter(
                    (d.get("zones") or {}).get("source") or "unmeasured"
                    for d in documents
                    if d.get("status") in SCORED_STATUSES
                )
            ),
            "di_sidecars_found": sum(
                1 for d in documents if (d.get("zones") or {}).get("sidecar")
            ),
            "di_dir": str(di_dir) if di_dir else "",
            "inference": {
                "title_top_fraction": TITLE_TOP_FRACTION,
                "title_size_ratio": TITLE_SIZE_RATIO,
                "title_size_abs": TITLE_SIZE_ABS,
                "title_tie_ratio": TITLE_TIE_RATIO,
                "title_max_chars": TITLE_MAX_CHARS,
                "title_max_lines": TITLE_MAX_LINES,
                "heading_size_ratio": HEADING_SIZE_RATIO,
                "heading_max_chars": HEADING_MAX_CHARS,
                "furniture_band": FURNITURE_BAND,
                "furniture_min_pages": FURNITURE_MIN_PAGES,
                "calibrated_against_this_corpus": False,
            },
            "registry_gates": zone_gates,
        },
        "filters": {"country": args.country, "only": args.only},
        "summary": summary,
        "unknown_doctypes": unknown,
        "manifest_errors": [asdict(e) for e in manifest_errors],
        "documents": documents,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "corpus-results.json"
    md_path = out_dir / "corpus-results.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")

    _print_summary(report, json_path, md_path)
    return 0


def _log(verbose: bool, index: int, total: int, record: dict[str, Any]) -> None:
    if not verbose:
        # One character per document keeps a long run visibly alive without flooding a
        # terminal: "." for correct, else the first letter of the status.
        sys.stdout.write("." if record["status"] == STATUS_CORRECT else record["status"][0])
        sys.stdout.flush()
        if index == total:
            sys.stdout.write("\n")
        return

    c = record.get("classification") or {}
    got = c.get("doctype_id", "—")
    src = _reader_short(record)
    bits = [f"[{index}/{total}]", f"{record['status']:<10}", f"[{src}]", f"{record['file']}"]
    if record["status"] in (STATUS_CORRECT, STATUS_WRONG, STATUS_ABSTAINED):
        bits.append(
            f"expected={record['expected_doctype']} got={got} "
            f"conf={c.get('confidence', 0):.2f} margin={c.get('margin', 0):.2f} "
            f"cov={c.get('coverage', 0):.2f}"
        )
    if record["status"] in (STATUS_WRONG, STATUS_ABSTAINED):
        bits.append(f"runners={_runners(c.get('runners_up') or [])}")
        if record["reason"]:
            bits.append(f"reason={record['reason']}")
    if record["status"] in (STATUS_NEEDS_OCR, STATUS_ERROR):
        bits.append(record["reason"])
    ex = record.get("extraction")
    if ex:
        bits.append(f"fill={ex['filled']}/{ex['field_count']}")
        if ex["missing_required"]:
            bits.append(f"missing_required={','.join(ex['missing_required'])}")
    print("  ".join(bits))


def _print_summary(report: dict[str, Any], json_path: Path, md_path: Path) -> None:
    overall = report["summary"]["overall"]
    print("")
    print("=" * 72)
    print(f"  {overall['documents']} document(s) in the corpus, {overall['scored']} measured")
    print(
        f"  CORRECT {overall['correct']}   WRONG {overall['wrong']}   "
        f"ABSTAINED {overall['abstained']}   NEEDS_OCR {overall['needs_ocr']}   "
        f"ERROR {overall['errors']}"
    )
    print(
        f"  accuracy {_pct(overall['accuracy'])} of measured"
        f"   |   {_pct(overall['precision_when_answered'])} of the ones it answered"
        f"   |   abstained on {_pct(overall['abstention_rate'])}"
    )
    by_source = report["summary"].get("by_text_source") or {}
    if len(by_source) > 1:
        print("  by text source: " + "   ".join(
            f"{name} {s['correct']}/{s['scored']} ({_pct(s['accuracy'])}, "
            f"{s['wrong']} wrong)"
            for name, s in by_source.items()
        ))
        print("  OCR rows carry recognition error — compare runs on text_layer, not overall")
    by_reader = report["summary"].get("by_reader") or {}
    readers = {n: s for n, s in by_reader.items() if n != _UNREAD_READER}
    if len(readers) > 1:
        print("  by reader: " + "   ".join(
            f"{name} {s['correct']}/{s['scored']} ({_pct(s['accuracy'])}, "
            f"{s['wrong']} wrong)"
            for name, s in readers.items()
        ))
    by_zone = report["summary"].get("by_zone_source") or {}
    if by_zone:
        print("  by zone source: " + "   ".join(
            f"{name} {s['correct']}/{s['scored']} ({_pct(s['accuracy'])}, "
            f"{s['wrong']} wrong)"
            for name, s in by_zone.items()
        ))
        scored_real = {n for n, b in by_zone.items() if b["scored"]} & ZONE_SOURCES_REAL
        if not scored_real:
            print("  NO document carried provider zone roles — every figure above was "
                  "measured with")
            print("  approximated or absent zones and is NOT a production figure "
                  "(see the report's Zones section)")
    if report["summary"]["by_country"]:
        print("  by country: " + "   ".join(
            f"{cc.upper()} {s['correct']}/{s['scored']} ({_pct(s['accuracy'])})"
            for cc, s in report["summary"]["by_country"].items()
        ))
    svc = report.get("service_ocr") or {}
    if svc.get("enabled") and svc.get("documents"):
        print(
            f"  {svc['documents']} document(s) had no text layer and were read by the "
            f"SERVICE with {svc['provider']}"
            + (f" at {svc['endpoint_host']}" if svc.get("network") else " in-process")
        )
    elif svc.get("consulted") and not svc.get("enabled"):
        print(f"  service OCR unavailable: {svc.get('problem', '')}")
    if overall["needs_ocr"]:
        print(f"  {overall['needs_ocr']} document(s) have no text layer and were skipped "
              "— see the report's OCR section")
    if overall["errors"]:
        print(f"  {overall['errors']} document(s) errored — see the report's error section")
    if report["unknown_doctypes"]:
        print(f"  {len(report['unknown_doctypes'])} manifest entr(ies) name a doctype the "
              "registry does not have")
    print("=" * 72)
    # Absolute, so the two lines a reader most wants to act on are copy-pasteable.
    print(f"  {json_path}")
    print(f"  {md_path}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 0
    except BrokenPipeError:
        # Someone piped us into `head`. Not an error, and printing about it would raise again.
        with contextlib.suppress(OSError):
            sys.stdout.close()
        return 0
    except Exception:  # noqa: BLE001 - a measurement tool must not be the thing that fails
        print("\n" + "!" * 72)
        print("HARNESS ERROR — the tool itself broke. This is a bug in tools/corpus_test.py.")
        print("!" * 72)
        traceback.print_exc()
        return 0


if __name__ == "__main__":
    sys.exit(main())
