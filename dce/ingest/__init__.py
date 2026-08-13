"""Accept the file formats a KYC/onboarding pipeline actually receives.

Before this package, a caller had to arrive already holding a layout payload, an Azure
``analyzeResult``, a DES OCR response, or a string. A business unit with a ``.docx`` from a
company secretary, an ``.xlsx`` cap table, a forwarded ``.msg``, or a phone photograph of a
passport had no way in at all. This package is the way in: **bytes** to a
:class:`~dce.models.LayoutView`, in-process, under caps.

--------------------------------------------------------------------------------
THE CENTRAL DESIGN DECISION
--------------------------------------------------------------------------------
Converting a ``.docx`` to text is pure computation: the characters are already in the file
and unzipping them is arithmetic. Nothing leaves the process, so the egress invariant
(``dce.config.allow_preclassification_egress = False``) is untouched and there is nothing to
decide.

**An image is not that.** A JPEG of a passport contains no text. Classifying it *requires*
optical recognition, and the accurate recognisers are cloud services — which is exactly the
egress this service exists to prevent. "Just call Azure Read on it first" is
pre-classification egress wearing a helpful hat: the document whose type we do not yet know
is precisely the document we are not allowed to send anywhere.

Three options are on the table:

(a) **Local, in-process OCR.** Preserves the invariant. Costs a dependency and accepts
    materially lower accuracy than a cloud recogniser.
(b) **Return a structured ``needs_ocr`` outcome.** The service refuses to guess, names what
    the file is and why it cannot read it, and hands the decision back to the caller — who
    knows their own jurisdiction's rules about where an unclassified customer document may
    go, and this service does not.
(c) **An OCR service** (:mod:`dce.ingest.ocr_service`). The best text by a distance, and a
    call out of this process before the doctype is known. Off by default, behind its own
    setting, behind a named guard (:func:`dce.egress.assert_ocr_egress_permitted`), and
    reported on ``/readyz`` with the provider, the endpoint host and the trust boundary the
    deployment declares for it. See below.

**What was chosen: (b) always, (a) as an optional extra that defaults OFF, (c) as an
optional extra that defaults OFF and announces itself when it is not.**

The reasoning, in order of weight:

1. **(b) is the only answer that is true in every deployment.** An image genuinely has no
   text. A service that always has an honest answer available is worth more than one whose
   honesty depends on an install-time flag, and ``needs_ocr`` is a *routable* outcome: it
   goes to the same human queue an abstention goes to, with a better reason attached.
2. **(a) as a default would silently lower the quality of the evidence** the cascade scores.
   Local OCR on a passport's machine-readable zone is not Azure Read on the same MRZ. The
   cascade's precision gates were reasoned about against text-layer and Azure-quality input;
   feeding them noticeably noisier text by default changes the meaning of every published
   accuracy number without anyone choosing that. The corpus harness already separates
   ``text_layer`` from ``ocr`` results for this reason, and it separates them because the
   difference showed up.
3. **(a) as an option is genuinely needed.** Onboarding really does receive phone
   photographs, and a deployment that has accepted the accuracy trade should not have to
   fork the service to make them work. So it is one setting
   (``DCE_INGEST_LOCAL_OCR_ENABLED``) plus one optional extra, and the engines are a closed
   allowlist — ``rapidocr`` (ONNX, genuinely in this process) and ``tesseract`` (a local
   subprocess; see :mod:`dce.ingest.ocr` for why that distinction is stated rather than
   glossed).
4. **The zero-dependency, zero-egress build stays the default.** ``pip install .`` still
   gives a service with no OCR code in it, no HTTP client, and no PDF engine. Everything
   this package adds is either standard library or an extra you have to name.

What local OCR is **not** allowed to be: a network call. Both engines run on this host, and
``tests/test_ingest_egress.py`` pushes an image through the whole ingestion path with the
socket tripwire armed to prove it. An engine that needs an endpoint is not a local engine and
is not in :data:`dce.ingest.ocr.ENGINES`; it is in :data:`~dce.ingest.ocr.SERVICE_ENGINES`,
carries ``service=True`` on its :class:`~dce.ingest.ocr.OcrProvider` record, and every
decision that turns on "is this read here or by another host" reads that flag rather than the
name.

--------------------------------------------------------------------------------
(c) THE OCR SERVICE PATH — WHAT IT MEANS, AND WHY IT IS NOT THE DEFAULT
--------------------------------------------------------------------------------
``azure_read`` (Vision Read v3.2) and ``azure_layout`` (Document Intelligence v4.0
``prebuilt-layout``) recognise a document by **handing it to an OCR endpoint, before anyone
knows what it is**. Whose endpoint that is — a vendor's, or a pod in the operator's own
cluster — is the deployment's declaration and not something this code can work out, so the
architecture is stated plainly and the ownership question is answered by
``DCE_INGEST_OCR_SERVICE_TRUST_BOUNDARY``:

* off by default, and the base install has no HTTP client at all, so the default build
  physically cannot do it (``pip install '.[azure-ocr]'``);
* its own setting, ``DCE_INGEST_OCR_SERVICE_ENABLED`` — deliberately **not** an extra value of
  ``local_ocr_engine``, because the word "local" would then be a lie in the place an operator
  reads fastest, and deliberately **not** ``allow_preclassification_egress``, which is the
  blanket switch, governs the paid vendor tiers, and stays off;
* a deployment may configure it *alongside* local OCR, and then a request selects between them
  with ``ingest.ocr_provider``; what is refused at boot is configuring both and naming no
  default, because there is no precedence between them that would not silently override one
  of the two decisions;
* every request it makes passes :func:`dce.egress.assert_ocr_egress_permitted`, which names
  the provider and the endpoint and refuses inside a classification scope;
* ``/readyz`` reports every configured provider, whether each is in-process or a service call,
  the endpoint host, and the declared boundary with its provenance.

**The caller-supplied path remains the recommended answer** and the one the console offers
first: an upstream service that already holds the document runs Read or Layout under its own
authorisation and posts the result here as ``azure_analyze_result`` / ``azure_read_result`` /
``des_ocr``. The classification is identical — the same adapters map both paths — and on that
one this service opens no socket, so the invariant is not weighed against anything.

--------------------------------------------------------------------------------
Format support
--------------------------------------------------------------------------------
================  =========================  ==========================================
Format            Dependency                 Structure recovered
================  =========================  ==========================================
TXT               none                       body only (nothing is stated to recover)
CSV               none                       table + header row
HTML              none                       ``<title>``/h1 -> title, h2-h6 -> heading,
                                             header/footer/nav -> furniture, tables
EML               none                       Subject -> title, envelope -> furniture,
                                             body, attachment names
MSG               none                       as EML (CFB reader in ``dce.ingest.cfb``)
RTF               none                       body only (styles live in a discarded
                                             stylesheet destination)
DOCX              none                       Title/Heading styles, tables, headers and
                                             footers -> furniture
XLSX              none                       sheet names -> heading, cells -> table
PPTX              none                       title placeholders -> title, per-slide pages
ODT               none                       outline level 1 -> title, others heading
PDF (text layer)  ``.[pdf]`` (PyMuPDF)       body only; pages and geometry
PDF (scanned)     ``.[pdf]`` + an OCR extra  ``needs_ocr``, or local OCR when enabled
JPEG PNG TIFF     an OCR extra               ``needs_ocr``, or local OCR when enabled
BMP WEBP HEIC GIF an OCR extra               ``needs_ocr``, or local OCR when enabled
================  =========================  ==========================================

Type is decided by **content**, never by filename — see :mod:`dce.ingest.detect`.

Where each parser stops, stated here so nobody has to discover it:

* **XLSX** does not apply number formats, so a formatted date reads as its serial number.
  Harmless for classification, wrong for date extraction — see :func:`dce.ingest.ooxml.parse_xlsx`.
* **RTF** recovers no headings: its styles live in the ``\\stylesheet`` destination this
  reader discards. Every block is body.
* **PDF** assigns no zones — a text layer states none, and inferring them is the guess
  :func:`dce.adapters.from_plain_text` refuses to make. It also detects no tables.
* **PPTX** does not read speaker notes (deliberate: a notes page is not the document).
  **DOCX** does not read footnotes, endnotes or comments.
* **HEIC/AVIF and multi-frame GIF** are detected and routed correctly, but under local OCR
  only their first frame is recognised, and HEIC decoding needs a Pillow build with HEIF
  support.
* **MSG** is exercised against compound files this repository writes
  (``tests/ingest_fixtures.compound_file``), covering both the FAT and mini-FAT paths. It
  has not been run against an Outlook-produced ``.msg``.
* **Attachments are never opened**, in EML or MSG. An attached PDF is a different document
  with a different doctype; submit it separately.
"""
from __future__ import annotations

from dce.ingest.detect import IMAGE_TYPES, NATIVE_TEXT_TYPES, Detection, MediaType, detect
from dce.ingest.errors import (
    ArchiveBomb,
    EngineUnavailable,
    IngestError,
    IngestTimeout,
    LimitExceeded,
    MalformedDocument,
    PayloadTooLarge,
    UnsupportedFormat,
)
from dce.ingest.limits import DEFAULT_LIMITS, Deadline, IngestLimits
from dce.ingest.pipeline import IngestOptions, ingest
from dce.ingest.result import IngestResult, IngestStatus, TextSource
from dce.ingest.settings import IngestSettings, get_ingest_settings

__all__ = [
    "DEFAULT_LIMITS",
    "IMAGE_TYPES",
    "NATIVE_TEXT_TYPES",
    "ArchiveBomb",
    "Deadline",
    "Detection",
    "EngineUnavailable",
    "IngestError",
    "IngestLimits",
    "IngestOptions",
    "IngestResult",
    "IngestSettings",
    "IngestStatus",
    "IngestTimeout",
    "LimitExceeded",
    "MalformedDocument",
    "MediaType",
    "PayloadTooLarge",
    "TextSource",
    "UnsupportedFormat",
    "detect",
    "get_ingest_settings",
    "ingest",
]
