"""Resource caps, and the clock that enforces the one cap the others cannot.

Ingestion is the first place in this service that touches attacker-controlled *bytes* rather
than attacker-controlled *text*. Every parser below it is therefore written against a single
:class:`IngestLimits` object rather than against its own private constants, so an operator can
see the whole exposure in one place and lower it in one place.

Two kinds of cap, and the difference matters:

* **Truncating caps** (``max_blocks``, ``max_chars``, ``max_table_rows`` …) stop the parser
  adding more and set ``truncated`` on the result. Half a spreadsheet still classifies, and a
  document that is merely *large* is not a document that is *hostile*.
* **Raising caps** (``max_bytes``, ``max_seconds``, the archive guards) abort. These are the
  ones where continuing means spending unbounded time or memory for one request.

``max_seconds`` is a cooperative deadline checked inside the parse loops, not a signal or a
watchdog thread. It cannot interrupt a single blocking call inside a C extension, which is
exactly why the structural caps exist alongside it rather than instead of it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace

from dce.ingest.errors import IngestTimeout


@dataclass(frozen=True)
class IngestLimits:
    """Everything one ingestion is allowed to spend.

    Defaults are sized for a KYC/onboarding pipeline: a 200-page mortgage pack or a
    50-sheet workbook goes through; a 500 MB upload and a 42 KB zip bomb do not.
    """

    # -- raising caps -------------------------------------------------------
    #: Largest upload accepted, before a parser is chosen. 32 MB covers a long scanned pack.
    max_bytes: int = 32 * 1024 * 1024
    #: Wall clock for the whole parse. Checked in every loop.
    max_seconds: float = 20.0
    #: Pages/slides/sheets read. Beyond this the document is truncated, not refused —
    #: classification reads the front of a document, so the tail is rarely the evidence.
    max_pages: int = 200

    # -- truncating caps ----------------------------------------------------
    #: Most :class:`~dce.models.TextBlock` we will build. 50k is far past any real form.
    max_blocks: int = 50_000
    #: Most characters of extracted text we will keep, across all blocks.
    max_chars: int = 4 * 1024 * 1024
    #: Longest single block. A 2 MB "paragraph" is a parser confusion, not a paragraph.
    max_block_chars: int = 32 * 1024
    #: Rows kept per table, and cells kept per table.
    max_table_rows: int = 5_000
    max_table_cells: int = 100_000
    #: Tables kept per document.
    max_tables: int = 2_000

    # -- container (ZIP) guards --------------------------------------------
    #: DOCX/XLSX/PPTX/ODT are ZIP archives; these three lines are the zip-bomb guard.
    max_archive_entries: int = 4_096
    #: Total bytes we will decompress out of one archive.
    max_archive_uncompressed_bytes: int = 256 * 1024 * 1024
    #: Bytes we will decompress out of one archive member.
    max_archive_entry_bytes: int = 64 * 1024 * 1024
    #: Refuse a member whose declared expansion ratio is beyond this. A 42 KB file that
    #: claims to expand to 4.5 GB is the canonical zip bomb and its ratio is ~10^5.
    max_compression_ratio: float = 500.0

    # -- email --------------------------------------------------------------
    #: MIME parts walked in an EML/MSG. Nested multiparts are a cheap amplification.
    max_mime_parts: int = 256

    # -- OCR ----------------------------------------------------------------
    #: Pages/frames sent to a local OCR engine. Much lower than ``max_pages``: OCR costs
    #: seconds per page even locally, and a 200-page scan would blow the deadline anyway.
    max_ocr_pages: int = 10
    #: Rasterisation DPI for scanned PDFs handed to a local engine.
    ocr_dpi: int = 300

    def with_(self, **changes: object) -> IngestLimits:
        """Return a copy with ``changes`` applied — the frozen-dataclass idiom."""
        return replace(self, **changes)  # type: ignore[arg-type]


#: The limits used when a caller does not supply any.
DEFAULT_LIMITS = IngestLimits()


class Deadline:
    """A cooperative wall-clock budget.

    Constructed once per ingestion and threaded through every parser. Parsers call
    :meth:`check` at the top of each page/row/part loop; the cost is one ``time.monotonic()``
    per iteration and the benefit is that no input shape can make ingestion run forever.
    """

    __slots__ = ("_end", "_seconds")

    def __init__(self, seconds: float) -> None:
        self._seconds = float(seconds)
        self._end = time.monotonic() + self._seconds if self._seconds > 0 else None

    @property
    def seconds(self) -> float:
        """The budget this deadline was created with."""
        return self._seconds

    @property
    def expired(self) -> bool:
        return self._end is not None and time.monotonic() > self._end

    @property
    def remaining(self) -> float:
        """Seconds left; ``inf`` when the deadline is disabled."""
        if self._end is None:
            return float("inf")
        return max(0.0, self._end - time.monotonic())

    def check(self, stage: str) -> None:
        """Raise if the budget is spent.

        Args:
            stage: Where we were, e.g. ``"xlsx.sheet3"``. Quoted in the exception so an
                operator can tell a slow parser from a slow engine.

        Raises:
            IngestTimeout: When the deadline has passed.
        """
        if self.expired:
            raise IngestTimeout(
                f"ingestion exceeded max_seconds={self._seconds:g} at {stage!r}; "
                "the upload was refused rather than allowed to run unbounded"
            )


__all__ = ["DEFAULT_LIMITS", "Deadline", "IngestLimits"]
