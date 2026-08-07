"""Label-anchored locator — find the label, then take what is printed next to it.

Three bindings, in descending order of how certain they are:

1. **Same line** — ``Date of Birth: 01/02/1990``. The label and its value share a text
   block, so there is nothing to guess.
2. **Right of** — the value block starts to the right of the label within
   ``label_window_x`` of the page width and shares the label's row. The dominant form
   layout.
3. **Below** — the value block starts under the label within ``label_window_y`` of the page
   height and overlaps it horizontally. The stacked layout, and the one that carries
   multi-line addresses.

Nearest wins within each binding, and the whole thing is gated on the field's pattern: an
``address`` field whose label happens to sit left of a date must not bind that date. That
rejection is the entire reason ``FieldSpec.pattern`` exists.

**Where the span ends is as important as where it begins.** A form line carries several
fields — ``Signature of U.S. person: J. Smith  Date: 2026-03-14`` — so taking everything to
the right of a matched label swallows the next field's label *and its value*. Every span
this locator captures therefore goes through :mod:`dce.extract.locators.trim`, which
terminates it at the next known caption and narrows it to the field's own shape. A span that
needed either is reported with a lower confidence than one that did not, so a clean binding
elsewhere on the page wins on tightness alone.

That line is also why a block is matched against **every** label the field declares rather
than only its best-scoring one: ``signature_date`` declares both captions on it, and only
one of them is followed by the date.
"""
from __future__ import annotations

import re

from dce.extract.locators import geometry as geo
from dce.extract.locators import trim
from dce.extract.locators.base import (
    Candidate,
    LocatorContext,
    clean_value,
    field_labels,
    label_similarity,
    match_label,
    partition_on_label,
    passes_pattern,
)
from dce.models import FieldSpec, LayoutView, TextBlock, Zone

__all__ = ["locate"]

_CONF_SAME_LINE = 0.82
_CONF_RIGHT = 0.76
_CONF_BELOW = 0.70
#: Reading order, used only where there is no geometry to bind with instead. "The next
#: line" is what "below" degrades to when nobody measured the page, so it scores under it.
_CONF_NEXT_LINE = 0.62
#: Fraction of the window distance that erodes confidence — a value 10pt right of its label
#: is a far better bet than one at the far edge of the search window.
_DISTANCE_PENALTY = 0.35
#: Field types whose value legitimately runs over several printed lines.
_MULTILINE_TYPES = frozenset({"address"})
#: A bracketed clarifier printed as part of a caption, not as part of the value.
_CLARIFIER_RE = re.compile(r"^[\s:.\-\u2013\u2014]*[(\[][^)\]]*[)\]]")
#: The caption marker: what a form prints between a caption and the value it introduces.
_TERMINATOR_RE = re.compile(r"^[\s.\-\u2013\u2014]*[:\uff1a]")
#: First word of a span \u2014 for the connector test below.
_FIRST_WORD_RE = re.compile(r"[^\W\d_][\w\u2019'-]*")
#: Last word before the label \u2014 same test, looking the other way.
_LAST_WORD_RE = re.compile(r"[^\W\d_][\w\u2019'-]*[\s]*$")
#: Everything a form prints in front of a caption that opens its line: nothing, its own
#: item numbering (``1.``, ``(a)``, ``iv.``), a bullet, or a box number.
_LINE_START_RE = re.compile(
    r"^[\s]*(?:[(\[]?[0-9ivxIVX]{1,4}[).\]]?|[-\u2022\u25aa*\u2013\u2014])?[\s]*$"
)
#: A bare item number or bullet standing alone on a line \u2014 form furniture, never a value.
_ORDINAL_ONLY_RE = re.compile(r"^[\s]*[(\[]?[0-9ivxIVX]{1,4}[).\]]?[\s]*$")
#: A line that *opens* with item numbering and runs on \u2014 "2. GHS shall bill Energy charges
#: to individual members\u2026". A numbered paragraph, not a value printed under a caption.
_NUMBERED_PARAGRAPH_RE = re.compile(r"^[\s]*[(\[]?[0-9ivxIVX]{1,4}[).\]][\s]+[^\W\d_]")
#: The reading-order binding will not accept an approximated caption; the label has to be
#: printed on the line verbatim (exactly, or as a whole-token substring of it).
_VERBATIM = 96.0
#: A comma or semicolon straight after a caption: the caption is being qualified, not closed.
_QUALIFIER_RE = re.compile(r"^[\s]*[,;]")
#: The last word of a span, for the dangling-connector test on a wrapped line.
_TRAILING_WORD_RE = re.compile(r"([^\W\d_][\w\u2019'-]*)[\s.]*$")


def locate(field: FieldSpec, view: LayoutView, ctx: LocatorContext) -> list[Candidate]:
    """Locate a value by anchoring on the field's label.

    Args:
        field: The field being resolved.
        view: The layout view to search.
        ctx: Locator context; supplies the fuzzy floor and the page-relative windows.

    Returns:
        Candidates ordered best-first, every one of which satisfies the field's pattern.
    """
    labels = field_labels(field, ctx)
    if not labels or not view.blocks:
        return []

    captions = trim.known_labels(field, ctx)
    blocks = list(view.blocks)
    rects = [geo.rect_from_quad(b.bbox) for b in blocks]
    out: list[Candidate] = []

    for index, block in enumerate(blocks):
        if not block.text.strip():
            continue
        matches = _matching_labels(block.text, labels, ctx.min_label_score)
        if not matches:
            continue

        caption_line = trim.reads_as_caption(block.text, captions, ctx.min_label_score)
        if not caption_line:
            # A line that *is* a caption — "Roll number:", "Assessment Year" — has no value
            # printed on it by definition, so splitting it can only yield the rest of the
            # caption. Only a line carrying something besides its caption is worth splitting.
            same_line = _same_line_candidates(
                field, block, matches, captions, ctx.min_label_score
            )
            if same_line:
                out.extend(same_line)
                continue

        matched, score = matches[0]
        weight = score / 100.0
        label_rect = rects[index]
        if label_rect is None:
            # No geometry: this is the plain-text degradation, where the only adjacency
            # anyone can observe is reading order.
            reading_order = _next_line_candidate(
                field, blocks, index, matched, weight, labels, captions, ctx,
                caption_line=caption_line,
            )
            if reading_order is not None:
                out.append(reading_order)
            continue
        width, height = geo.page_size(view, block.page)
        max_dx = ctx.settings.label_window_x * width
        max_dy = ctx.settings.label_window_y * height

        right = _nearest(
            blocks, rects, index, block.page, label_rect, labels, captions, ctx,
            horizontal=True, limit=max_dx,
        )
        if right is not None:
            candidate_block, gap = right
            decay = 1.0 - _DISTANCE_PENALTY * min(1.0, gap / max_dx if max_dx else 0.0)
            out.append(
                _make(
                    field, clean_value(candidate_block.text), block, candidate_block,
                    _CONF_RIGHT * weight * decay,
                    f"label {matched!r} -> right, gap {gap:.1f} ({score:.0f})",
                    captions=captions, matched=matched,
                    min_label_score=ctx.min_label_score,
                )
            )

        below = _nearest(
            blocks, rects, index, block.page, label_rect, labels, captions, ctx,
            horizontal=False, limit=max_dy,
        )
        if below is not None:
            candidate_block, gap = below
            decay = 1.0 - _DISTANCE_PENALTY * min(1.0, gap / max_dy if max_dy else 0.0)
            text, last_block = _extend_multiline(
                field, blocks, rects, candidate_block, labels, captions, ctx, max_dy
            )
            out.append(
                _make(
                    field, text, block, candidate_block, _CONF_BELOW * weight * decay,
                    f"label {matched!r} -> below, gap {gap:.1f} ({score:.0f})",
                    span_to=last_block, captions=captions, matched=matched,
                    min_label_score=ctx.min_label_score,
                )
            )

    accepted = [c for c in out if c is not None]
    accepted.sort(key=lambda c: -c.confidence)
    return accepted


def _matching_labels(
    text: str, labels: list[tuple[str, float]], min_score: float
) -> list[tuple[str, float]]:
    """Every declared label this block clears the fuzzy floor for, best-scoring first.

    :func:`~dce.extract.locators.base.match_label` returns only the winner, which is the
    right answer when asking "is this block my label?" and the wrong one when asking "where
    does my value start?": a single printed line can carry two of a field's own captions,
    and the higher-scoring one is not necessarily the one the value follows.
    """
    scored = [
        (label, label_similarity(label, text) * weight) for label, weight in labels
    ]
    clearing = [(label, score) for label, score in scored if score >= min_score]
    clearing.sort(key=lambda pair: -pair[1])
    return clearing


def _same_line_candidates(
    field: FieldSpec,
    block: TextBlock,
    matches: list[tuple[str, float]],
    captions: tuple[str, ...],
    min_label_score: float,
) -> list[Candidate]:
    """Candidates for every one of the field's captions that this line carries."""
    out: list[Candidate] = []
    seen: set[str] = set()
    for matched, score in matches:
        parts = partition_on_label(block.text, matched)
        if parts is None:
            continue
        before, _hit, after = parts
        if not _reads_as_a_caption_here(matched, before, after):
            continue
        if _caption_still_running(before, after):
            continue
        tail = _strip_clarifier(clean_value(after))
        if not tail:
            continue
        candidate = _make(
            field, tail, block, block, _CONF_SAME_LINE * (score / 100.0),
            f"label {matched!r} same line ({score:.0f})",
            captions=captions, matched=matched, min_label_score=min_label_score,
            same_line=True,
        )
        if candidate is None or candidate.value in seen:
            continue
        seen.add(candidate.value)
        out.append(candidate)
    return out


def _reads_as_a_caption_here(matched: str, before: str, after: str) -> bool:
    """``True`` when this printing of the label is a caption and not a word in a sentence.

    Matching a label inside a line says the *characters* are there; it does not say the
    document used them as a caption. ``Please have your roll number available when you
    contact us.`` contains ``roll number``, and binding it reports ``"available when you
    contact us."`` as the property's roll number. That is not a near miss — it is a
    grammatical sentence sitting in a KYC field, and every one of the confidently wrong
    values this locator produced on the corpus came in this way.

    A form separates a caption from its value by exactly two conventions, and running prose
    obeys neither:

    * **Line start.** A caption that opens its line (after the form's own numbering —
      ``1.``, ``(a)``, a bullet) needs no marker; whatever follows it is its value.
    * **A caption marker.** A caption printed mid-line is followed by a colon, or by the
      column gap a form sets between the two — a tab, or more than one space. ``Name  ANNA
      ERIKSSON   Father's Name  BO ERIKSSON`` binds both fields on that evidence alone.

    A single space after a mid-line match is a sentence, and it costs us the binding. That
    is the intended direction: the same convention is what a human reads the form by, and
    where the text layer has flattened a real column gap to one space the field goes to
    review empty rather than to a consumer wrong.
    """
    if _TERMINATOR_RE.match(after) or after[:1] in ("\t", "") or after[:2] == "  ":
        return True
    return _LINE_START_RE.match(before) is not None or not matched


def _caption_still_running(before: str, after: str) -> bool:
    """``True`` when the matched label is only *part* of the caption printed on this line.

    A caption is a noun phrase, and a noun phrase does not stop at a connector. When the
    word on either side of the match is one — ``Full name and **address** of the declarant``,
    ``Employee's name and **address** -- Nom et adresse``, ``(The **deductor** to provide
    payment wise details…)``, ``**Year** of construction:`` — the label is a fragment of a
    longer phrase, and what follows it is the rest of that phrase, not a value. Reported as
    a value it produces the exact failure this module exists to stop: a field filled with
    the form's own wording, confidently and wrongly.

    The one thing that overrides it is a caption marker. ``City: el Paso`` has closed its
    caption with a colon, so a value beginning with a connector is a value — which matters,
    because plenty of real names and places start with one (``de Souza``, ``El Paso``,
    ``van der Berg``). Without that colon a leading connector costs us the binding, and the
    field goes to a human empty rather than to a consumer wrong.
    """
    if _TERMINATOR_RE.match(after):
        return False
    if _QUALIFIER_RE.match(after):
        # "Trade Name, if any" — a comma after a caption qualifies it; no form introduces a
        # value with one. Splitting here reports ``"if any"`` as the trade name.
        return True
    # Lower case, deliberately un-folded: a connector printed in a caption is set in
    # running case ("… of the declarant"), while a value that happens to open with one is
    # set as a value ("DE SOUZA", "El Paso"). Folding here would cost those.
    leading = _FIRST_WORD_RE.match(after.lstrip(" \t"))
    if leading is not None and leading.group(0) in trim.CONNECTORS:
        return True
    trailing = _LAST_WORD_RE.search(before)
    return trailing is not None and trailing.group(0).strip().casefold() in trim.CONNECTORS


def _strip_clarifier(tail: str) -> str:
    """Drop the parenthetical a caption trails behind it.

    ``1 Name (as shown on your income tax return)`` is one caption, not a caption and a
    value: splitting on ``Name`` leaves the clarifier, and reporting it fills ``full_name``
    with the form's own instructions. Stripping it leaves nothing, which correctly sends the
    lookup on to the geometry bindings and finds the name printed below.

    It also does the useful half of the same job for ``Date (MM-DD-YYYY): 2026-03-14``.
    """
    previous = None
    while tail != previous:
        previous = tail
        tail = clean_value(_CLARIFIER_RE.sub("", tail, count=1))
    return tail


def _make(
    field: FieldSpec,
    value: str,
    label_block: TextBlock,
    value_block: TextBlock,
    confidence: float,
    detail: str,
    *,
    span_to: TextBlock | None = None,
    captions: tuple[str, ...] = (),
    matched: str = "",
    min_label_score: float = 0.0,
    same_line: bool = False,
    caption_headed: bool = False,
) -> Candidate | None:
    """Build a candidate, or ``None`` when what is left is not a value at all.

    The last guard is the one that catches a bilingual card: ``Name / नाम`` splits on
    ``Name`` and leaves the *other half of its own caption*, which would be reported as the
    holder's name. Whatever survives trimming still has to not be a caption.
    """
    trimmed = trim.trim_span(field, value, labels=captions, matched=matched)
    if not trimmed.value:
        return None
    if trim.reads_as_caption(trimmed.value, captions, min_label_score):
        return None
    if (same_line or caption_headed) and trim.starts_with_caption(
        trimmed.value, captions, matched=matched
    ):
        # A span that *starts* with another field's caption is not a value that happens to
        # be followed by one. Carved out of its own caption's line it is the other half of a
        # bilingual or compound caption ("Last name ... -- Nom de famille"); taken from the
        # line after a caption it is simply the next caption, qualified past the coverage
        # floor that would otherwise have caught it ("Trade Name, if any").
        return None
    if not passes_pattern(field, trimmed.value):
        return None
    if _ends_mid_phrase(trimmed.value):
        # "Summary of amount paid/credited and" — the span was cut at the next caption and
        # what is left stops on a connector. A value is a complete thing; half a phrase is
        # the form's wording, reported under a field name.
        return None
    confidence *= trimmed.penalty
    if trimmed.note:
        detail += f"; {trimmed.note}"

    bbox = value_block.bbox
    if span_to is not None and span_to is not value_block:
        merged = geo.union(
            r for r in (geo.rect_from_quad(value_block.bbox), geo.rect_from_quad(span_to.bbox))
            if r is not None
        )
        if merged is not None:
            bbox = geo.quad_from_rect(merged)
    return Candidate(
        value=trimmed.value,
        locator="label",
        confidence=round(min(confidence, 0.95), 4),
        page=value_block.page,
        bbox=bbox,
        raw=value_block.text,
        detail=detail,
        extra={"label_page": str(label_block.page)},
    )


def _nearest(
    blocks: list[TextBlock],
    rects: list[geo.Rect | None],
    label_index: int,
    page: int,
    label_rect: geo.Rect,
    labels: list[tuple[str, float]],
    captions: tuple[str, ...],
    ctx: LocatorContext,
    *,
    horizontal: bool,
    limit: float,
) -> tuple[TextBlock, float] | None:
    """Nearest block right of (or below) the label inside ``limit``, skipping the label."""
    best: tuple[TextBlock, float] | None = None
    for index, block in enumerate(blocks):
        if index == label_index or block.page != page or not block.text.strip():
            continue
        rect = rects[index]
        if rect is None:
            continue
        if horizontal:
            if not geo.is_right_of(label_rect, rect, max_dx=limit):
                continue
            gap = max(0.0, geo.right_gap(label_rect, rect))
        else:
            if not geo.is_below(label_rect, rect, max_dy=limit):
                continue
            gap = max(0.0, geo.below_gap(label_rect, rect))
        # A block that restates the label (bilingual caption) is furniture, not a value —
        # and so is a block that states some *other* field's label.
        if match_label(block.text, labels, ctx.min_label_score)[0]:
            continue
        if trim.reads_as_caption(block.text, captions, ctx.min_label_score):
            continue
        if best is None or gap < best[1]:
            best = (block, gap)
    return best


def _next_line_candidate(
    field: FieldSpec,
    blocks: list[TextBlock],
    label_index: int,
    matched: str,
    weight: float,
    labels: list[tuple[str, float]],
    captions: tuple[str, ...],
    ctx: LocatorContext,
    *,
    caption_line: bool,
) -> Candidate | None:
    """Bind a caption to the line printed after it, where there is no geometry to use.

    :func:`dce.adapters.from_plain_text` is the documented degradation for a caller with no
    layout provider, and it produces blocks with no bounding boxes at all. Every geometric
    binding in this locator is therefore dead on that path: only the same-line split
    survives, and a stacked form — ``Roll number:`` on one line, the roll number on the
    next — extracts nothing. Reading order is what "below" means when nobody measured the
    page, and it is the only adjacency such a view carries.

    It is also much weaker evidence, so it is fenced in on both sides:

    * **The label's line must be a caption.** Not "must contain the label" — a word matched
      inside a sentence of instructions ("(The deductor to provide payment wise details…)")
      is followed by the next sentence, and binding that would fill a KYC field with prose.
      Requiring the line to *be* a caption is what separates a form's stacked layout from a
      paragraph that happens to use the word.
    * **The next line must not be a caption either** — its own label, any sibling field's,
      or anything closed by a colon. Two captions in a row is a form's column header block,
      and there is no value between them.
    * **The next line must not be the form's own numbering.** A blank GST certificate reads
      ``1.`` / ``Legal Name`` / ``2.`` / ``Trade Name``: the line after every caption is the
      next item's number, and binding it fills the whole certificate with ordinals.
    * **One line only, never a run of them.** :func:`_extend_multiline` can absorb an
      address's continuation lines because it can check that they stayed in the column and
      within a line-height. Reading order can check neither, and a walk that stops only at
      the next caption swallows entire pages of prose into an ``address``.
    * **The field must have a shape.** This is the fence that matters, and it is the one
      that follows from what reading order actually is: the weakest evidence in the system,
      with nothing spatial behind it. A ``date``, ``number`` or ``id`` — anything with a
      pattern — can be checked against the value itself, so the shape stands in for the
      geometry nobody measured. ``name``, ``address`` and ``string`` cannot be checked
      against anything, and on a blank form the line after a caption is *another caption* —
      ``Last name``, ``Street address``, ``Mother's Name*``, ``(a) Full name``. Every
      caption-reported-as-a-value this binding produced across the corpus was a shapeless
      field; every value it recovered was a shaped one.

    Args:
        field: The field being resolved.
        blocks: Every block in the view, in reading order.
        label_index: Index of the block the label matched in.
        matched: The label that matched.
        weight: Language weight x fuzzy score for that match, 0..1.
        labels: The field's own labels, for rejecting a restated caption.
        captions: Every caption on the doctype.
        ctx: Locator context.
        caption_line: Whether the label's own block reads as a caption.

    Returns:
        The candidate, or ``None`` when either fence rejects the binding.
    """
    if not caption_line or not trim.has_shape(field):
        return None
    label_block = blocks[label_index]
    if label_similarity(matched, label_block.text) < _VERBATIM:
        # The caption has to be *printed*, not approximated. ``reads_as_caption`` accepts any
        # line closed by a colon, and a sentence of instructions ending in one — "…for
        # consumption in respective slab of domestic consumers i.e. FY 2019-20 as follows:"
        # — is then a caption whose label ``Consumer ID`` matched ``consumers i.e.`` at 91.
        # A binding with no geometry behind it does not get to also guess at the caption.
        return None
    if _follows_a_caption(blocks, label_index, captions, ctx):
        # Two captions in a row is a column header block — "RFC" / "Folio" / a value / a
        # value — and reading order runs down it, not across. Bound to the line after it,
        # `Folio` takes the *RFC*: a well-formed identifier, of the wrong field, reported
        # with no sign anything went wrong. Nothing in a geometry-free view can pair the
        # captions with their columns, so this refuses instead of guessing.
        return None
    for offset in range(label_index + 1, len(blocks)):
        following = blocks[offset]
        if not following.text.strip() or following.page != label_block.page:
            continue
        if match_label(following.text, labels, ctx.min_label_score)[0]:
            return None
        if trim.reads_as_caption(following.text, captions, ctx.min_label_score):
            return None
        if _ORDINAL_ONLY_RE.match(following.text):
            return None
        if _NUMBERED_PARAGRAPH_RE.match(following.text):
            return None
        if _ends_mid_phrase(following.text):
            return None
        if following.zone is Zone.furniture:
            return None
        return _make(
            field, following.text, label_block, following, _CONF_NEXT_LINE * weight,
            f"label {matched!r} -> next line (no geometry)",
            captions=captions, matched=matched, min_label_score=ctx.min_label_score,
            caption_headed=True,
        )
    return None


def _follows_a_caption(
    blocks: list[TextBlock],
    label_index: int,
    captions: tuple[str, ...],
    ctx: LocatorContext,
) -> bool:
    """``True`` when the block before this caption is a caption too.

    A run of captions is a column header, and its values are printed *under* the run rather
    than after each caption. Reading order walks the run and then the values, in that order,
    so pairing the nth caption with the line after it pairs it with the 1st value.
    """
    for offset in range(label_index - 1, -1, -1):
        previous = blocks[offset]
        if not previous.text.strip():
            continue
        return trim.reads_as_caption(previous.text, captions, ctx.min_label_score)
    return False


def _ends_mid_phrase(text: str) -> bool:
    """``True`` when a line stops on a connector, i.e. it is half of a wrapped phrase.

    Reading order cannot tell a value printed under its caption from the first line of some
    other block that happens to come next — but a *value* is a complete thing, and a line
    ending ``"Quality of"`` or ``"Last updated on"`` is a caption the layout wrapped onto
    two lines. Binding it reports half a phrase as a property address.
    """
    trailing = _TRAILING_WORD_RE.search((text or "").strip())
    return trailing is not None and trailing.group(1).casefold() in trim.CONNECTORS


def _extend_multiline(
    field: FieldSpec,
    blocks: list[TextBlock],
    rects: list[geo.Rect | None],
    start: TextBlock,
    labels: list[tuple[str, float]],
    captions: tuple[str, ...],
    ctx: LocatorContext,
    max_dy: float,
) -> tuple[str, TextBlock]:
    """Absorb the continuation lines of a multi-line value (addresses, mostly).

    Only runs for field types that genuinely wrap. Stops at the first block that reads like
    a caption — its own label, any other field's label, or anything ending in a colon — or
    that sits too far below or drifts out of the column: the conditions under which the next
    printed line belongs to a different field.
    """
    if field.type not in _MULTILINE_TYPES and not field.multi:
        return start.text, start

    start_rect = geo.rect_from_quad(start.bbox)
    if start_rect is None:
        return start.text, start

    parts = [start.text]
    current, current_rect, last = start, start_rect, start
    line_height = start_rect.height or max_dy
    for index, block in enumerate(blocks):
        if block is current or block.page != current.page or not block.text.strip():
            continue
        rect = rects[index]
        if rect is None or rect.y0 < current_rect.y1 - 0.25 * line_height:
            continue
        if geo.below_gap(current_rect, rect) > 1.6 * line_height:
            continue
        if geo.h_overlap(current_rect, rect) / (current_rect.width or 1e-9) < 0.35:
            continue
        if trim.reads_as_caption(block.text, captions, ctx.min_label_score):
            break
        if match_label(block.text, labels, ctx.min_label_score)[0]:
            break
        if block.zone is Zone.furniture:
            break
        parts.append(block.text)
        current, current_rect, last = block, rect, block
    return "\n".join(parts), last
