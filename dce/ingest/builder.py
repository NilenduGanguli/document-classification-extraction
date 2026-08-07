"""Accumulate blocks, tables and pages into a :class:`~dce.models.LayoutView`, under caps.

Every parser in this package writes through :class:`LayoutBuilder` rather than constructing
``TextBlock`` lists itself. That buys three things:

* **the caps are applied once**, in the place that knows the running totals, instead of being
  re-derived (and mis-derived) in each of eleven parsers;
* **truncation is visible** — when a cap bites, the result says so and names the cap, because
  a silently shortened document classifies as a confidently different document;
* **the zone convention is applied once.**

**The zone convention, and why it matches** :mod:`dce.adapters`. Azure Document Intelligence
maps ``paragraphs[].role`` onto :class:`~dce.models.Zone` — ``title`` -> title,
``sectionHeading`` -> heading, ``pageHeader``/``pageFooter``/``pageNumber`` -> furniture,
everything else -> body — and re-zones any paragraph that falls inside a table to
:attr:`Zone.table` while *also* emitting that table into ``LayoutView.tables``. This package
reproduces that shape exactly: :meth:`LayoutBuilder.table` emits one :attr:`Zone.table` block
per row *and* a :class:`~dce.models.Table` with per-cell text. It is the same double
representation the reference producer emits, so a document ingested here scores the same way
as the same document read by Azure, which is the only property that makes the two paths
comparable.

**What is not done, on purpose:** nothing is promoted to :attr:`Zone.title` on a guess.
Where a format states a title (a DOCX ``Title`` style, an HTML ``<h1>``, a PPTX title
placeholder, an EML ``Subject``) that statement is honoured. Where a format carries no
structure at all (a PDF text layer, a TXT file) every block is :attr:`Zone.body`, exactly as
:func:`dce.adapters.from_plain_text` does, for the reason given there: a wrong title is
amplified by ``zone_weight_title`` and turns an abstention into a confident mistake.
"""
from __future__ import annotations

import re

from dce.ingest.limits import Deadline, IngestLimits
from dce.models import Cell, LayoutView, PageInfo, Table, TextBlock, Zone

#: Truncated block text is marked, so a downstream reader is never shown a sentence that
#: simply stops. The marker is ASCII so it cannot itself become an anchor match.
TRUNCATION_MARKER = " [...truncated]"

#: C0/C1 control characters, minus the whitespace ones ``str.split`` already handles. Every
#: parser is a candidate to emit these — a stray byte in an RTF hex escape, a control code
#: inside a spreadsheet cell — and they are invisible in a report while still counting as
#: characters in every length-normalised score downstream.
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]")


class LayoutBuilder:
    """Builds one :class:`~dce.models.LayoutView`, enforcing the truncating caps."""

    def __init__(self, limits: IngestLimits, deadline: Deadline) -> None:
        self._limits = limits
        self._deadline = deadline
        self._blocks: list[TextBlock] = []
        self._tables: list[Table] = []
        self._pages: dict[int, PageInfo] = {}
        self._chars = 0
        self._cells = 0
        self.truncated = False
        self.limits_hit: list[str] = []

    # -- state --------------------------------------------------------------
    @property
    def full(self) -> bool:
        """True once no further text will be accepted.

        Reading this **records the cap that bit**, which is why it is not a pure predicate.
        Parsers stop at ``if builder.full: break``, so this is the moment the truncation
        happens; leaving the recording to :meth:`block` meant a parser that checked first and
        never called :meth:`block` truncated the document with ``truncated=False`` on the
        result — a silent short read, which is the failure this whole class of cap exists to
        make visible.
        """
        if len(self._blocks) >= self._limits.max_blocks:
            self._hit("max_blocks")
            return True
        if self._chars >= self._limits.max_chars:
            self._hit("max_chars")
            return True
        return False

    def _hit(self, cap: str) -> None:
        self.truncated = True
        if cap not in self.limits_hit:
            self.limits_hit.append(cap)

    # -- content ------------------------------------------------------------
    def block(
        self,
        text: str,
        *,
        zone: Zone = Zone.body,
        page: int = 1,
        role: str | None = None,
        bbox: list[float] | None = None,
    ) -> bool:
        """Add one block. Returns False when it was dropped because a cap was reached.

        Whitespace-only text is dropped silently — it is not a cap, it is not content, and an
        empty block would dilute every length-normalised score downstream.
        """
        cleaned = " ".join(_CONTROLS.sub(" ", text).split()) if text else ""
        if not cleaned:
            return False
        if len(self._blocks) >= self._limits.max_blocks:
            self._hit("max_blocks")
            return False
        if self._chars >= self._limits.max_chars:
            self._hit("max_chars")
            return False
        if len(cleaned) > self._limits.max_block_chars:
            cleaned = cleaned[: self._limits.max_block_chars] + TRUNCATION_MARKER
            self._hit("max_block_chars")
        room = self._limits.max_chars - self._chars
        if len(cleaned) > room:
            cleaned = cleaned[:room] + TRUNCATION_MARKER
            self._hit("max_chars")
        self._blocks.append(
            TextBlock(text=cleaned, zone=zone, page=max(1, page), bbox=bbox, role=role)
        )
        self._chars += len(cleaned)
        return True

    def lines(self, text: str, *, zone: Zone = Zone.body, page: int = 1) -> int:
        """Add one block per non-empty line — the :func:`dce.adapters.from_plain_text` shape.

        Used for formats whose unit of layout really is the line (TXT, an RTF paragraph run,
        an email body). Returns how many blocks were added.
        """
        added = 0
        for index, line in enumerate(text.splitlines() if text else ()):
            if index % 512 == 0:
                self._deadline.check("builder.lines")
            if self.full:
                break
            if self.block(line, zone=zone, page=page):
                added += 1
        return added

    def table(
        self,
        rows: list[list[str]],
        *,
        page: int = 1,
        table_id: str = "",
        header_rows: int = 0,
        emit_blocks: bool = True,
    ) -> Table | None:
        """Add a table: a :class:`~dce.models.Table` plus one row block per row.

        Args:
            rows: Row-major cell text. Ragged rows are fine.
            page: 1-based page/sheet/slide the table sits on.
            table_id: Stable id; generated from ``page`` and position when empty.
            header_rows: How many leading rows are headers (``is_header`` on their cells).
            emit_blocks: Whether to also emit the :attr:`Zone.table` row blocks. Only the
                CSV parser sets this False, because a CSV *is* its table and the row blocks
                would be the entire document counted twice.

        Returns:
            The table, or ``None`` when nothing was added because a cap was reached.
        """
        self._deadline.check("builder.table")
        if len(self._tables) >= self._limits.max_tables:
            self._hit("max_tables")
            return None
        kept = rows[: self._limits.max_table_rows]
        if len(rows) > len(kept):
            self._hit("max_table_rows")

        cells: list[Cell] = []
        for row_index, row in enumerate(kept):
            for col_index, value in enumerate(row):
                if self._cells >= self._limits.max_table_cells:
                    self._hit("max_table_cells")
                    break
                text = " ".join(_CONTROLS.sub(" ", str(value)).split()) if value else ""
                if not text:
                    continue
                if len(text) > self._limits.max_block_chars:
                    text = text[: self._limits.max_block_chars] + TRUNCATION_MARKER
                    self._hit("max_block_chars")
                cells.append(
                    Cell(
                        row=row_index,
                        col=col_index,
                        text=text,
                        is_header=row_index < header_rows,
                    )
                )
                self._cells += 1

        table = Table(
            table_id=table_id or f"p{page}-tbl{len(self._tables)}",
            page=max(1, page),
            row_count=len(kept),
            col_count=max((len(r) for r in kept), default=0),
            cells=cells,
        )
        self._tables.append(table)

        if emit_blocks:
            for row in kept:
                if self.full:
                    break
                joined = "  ".join(str(v).strip() for v in row if str(v).strip())
                self.block(joined, zone=Zone.table, page=page)
        return table

    def page(
        self, number: int, *, width: float = 0.0, height: float = 0.0, unit: str = "pixel"
    ) -> None:
        """Declare a page. Idempotent — the first declaration of a number wins."""
        self._pages.setdefault(
            number, PageInfo(page=max(1, number), width=width, height=height, unit=unit)
        )

    # -- output -------------------------------------------------------------
    @property
    def block_count(self) -> int:
        return len(self._blocks)

    @property
    def char_count(self) -> int:
        return self._chars

    def build(self, *, doc_id: str = "", raw: dict[str, object] | None = None) -> LayoutView:
        """Assemble the view. Pages are synthesised from the blocks when none were declared."""
        pages = [self._pages[n] for n in sorted(self._pages)]
        if not pages:
            numbers = sorted({b.page for b in self._blocks} | {t.page for t in self._tables})
            pages = [PageInfo(page=n) for n in numbers] or [PageInfo(page=1)]
        return LayoutView(
            doc_id=doc_id,
            pages=pages,
            blocks=self._blocks,
            tables=self._tables,
            raw=dict(raw or {}),
        )


__all__ = ["TRUNCATION_MARKER", "LayoutBuilder"]
