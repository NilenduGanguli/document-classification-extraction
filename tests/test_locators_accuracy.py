"""Locator accuracy — where a value *ends*, and which binding wins when two disagree.

Pure and offline: layout views are built by hand with explicit geometry, no fixture, no
network, no model. Every identifier is synthetic; no real PII appears here.

The defect these tests exist for is over-capture. A locator that finds the right label and
then reads to the end of the block produces a value that contains the *next* field's label
and the next field's value — ``"J. Smith Date: 2026-03-14"`` for a date field. That is worse
than an empty field, because an empty field goes to the review queue and a confidently wrong
one does not. :func:`test_signature_line_does_not_swallow_the_next_label_and_its_value` is
the reproduction; everything after it pins one piece of the fix.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # keeps the suite runnable without an installed package
    sys.path.insert(0, str(_ROOT))

from dce.extract.locators import LocatorContext, trim  # noqa: E402
from dce.extract.locators import geometry as geo  # noqa: E402
from dce.extract.locators import kv as kv_locator  # noqa: E402
from dce.extract.locators import label as label_locator  # noqa: E402
from dce.extract.locators import table as table_locator  # noqa: E402
from dce.extract.resolve import resolve, resolve_field  # noqa: E402
from dce.extract.schema import DocSchema  # noqa: E402
from dce.models import (  # noqa: E402
    Cell,
    DocTypeSpec,
    FieldSpec,
    KeyValue,
    LayoutView,
    PageInfo,
    Table,
    TextBlock,
)

PAGE_W, PAGE_H = 1000.0, 1400.0

#: The line that reproduced the defect, verbatim.
W9_SIGNATURE_LINE = "Signature of U.S. person: J. Smith  Date: 2026-03-14"


def q(x0: float, y0: float, x1: float, y1: float) -> list[float]:
    """Build a clockwise-from-top-left quad from a rectangle."""
    return [x0, y0, x1, y0, x1, y1, x0, y1]


def view(**kwargs) -> LayoutView:
    """A one-page layout view with sane page geometry."""
    kwargs.setdefault("pages", [PageInfo(page=1, width=PAGE_W, height=PAGE_H)])
    kwargs.setdefault("doc_id", "test-doc")
    return LayoutView(**kwargs)


def ctx(**kwargs) -> LocatorContext:
    return LocatorContext(**kwargs)


def signature_date_field() -> FieldSpec:
    """``us_w9.signature_date`` exactly as the registry declares it.

    Both captions on the signature line belong to *this* field, which is what makes the
    over-capture happen: the higher-scoring of the two is not the one the date follows.
    """
    return FieldSpec(
        name="signature_date",
        attribute_key="doc.issue_date",
        type="date",
        labels={"en": ["Date", "Signature of U.S. person"]},
        validator="generic_date",
        locators=["label"],
    )


# ---------------------------------------------------------------------------
# The reproduction
# ---------------------------------------------------------------------------
def test_signature_line_does_not_swallow_the_next_label_and_its_value():
    """THE regression: ``signature_date`` on a W-9 signature line.

    Before the fix the label locator matched ``Signature of U.S. person`` (it scores 100 to
    ``Date``'s 96), took everything after it, and reported
    ``"J. Smith Date: 2026-03-14"`` — the signer's name, the next caption, and the date, in
    one date field.
    """
    doc = view(blocks=[TextBlock(text=W9_SIGNATURE_LINE, page=1, bbox=q(100, 900, 700, 930))])
    field = signature_date_field()

    best = label_locator.locate(field, doc, ctx())[0]
    assert best.value == "2026-03-14"

    extracted = resolve_field(field, doc, ctx())[0]
    assert extracted.value == "2026-03-14"
    assert extracted.normalized == "2026-03-14"
    assert "J. Smith" not in (extracted.value or "")


def test_the_over_captured_binding_survives_but_scores_below_the_tight_one():
    """The wrong binding is not deleted, it is out-ranked — provenance stays auditable."""
    doc = view(blocks=[TextBlock(text=W9_SIGNATURE_LINE, page=1, bbox=q(100, 900, 700, 930))])
    found = label_locator.locate(signature_date_field(), doc, ctx())

    assert [c.value for c in found] == ["2026-03-14", "J. Smith"]
    assert found[0].confidence > found[1].confidence
    assert "cut at next label" in found[1].detail
    assert "cut at next label" not in found[0].detail


def test_the_signature_name_field_on_the_same_line_reads_only_the_name():
    """The other field on that line must come back clean too, not just the date."""
    doc = view(blocks=[TextBlock(text=W9_SIGNATURE_LINE, page=1, bbox=q(100, 900, 700, 930))])
    field = FieldSpec(
        name="signer_name", type="name", labels={"en": ["Signature of U.S. person"]},
        validator="name", locators=["label"],
    )
    spec = DocTypeSpec(
        doctype_id="us_w9", label="W-9", country="US",
        fields=[field, signature_date_field()],
    )
    extracted = resolve_field(field, doc, ctx(spec=spec))[0]
    assert extracted.value == "J. Smith"


# ---------------------------------------------------------------------------
# 1. The span stops at the next label
# ---------------------------------------------------------------------------
def test_a_value_stops_at_the_next_label_on_the_same_line():
    doc = view(
        blocks=[
            TextBlock(text="Name: Anna Eriksson  Nationality: Utopian", page=1,
                      bbox=q(100, 200, 700, 220))
        ]
    )
    field = FieldSpec(
        name="full_name", type="name", labels={"en": ["Name"]}, validator="name",
        locators=["label"],
    )
    assert label_locator.locate(field, doc, ctx())[0].value == "Anna Eriksson"


def test_a_value_stops_at_a_sibling_fields_label_in_the_block_to_the_right():
    """The next caption need not be one of *this* field's labels to end its value."""
    doc = view(
        blocks=[
            TextBlock(text="Applicant", page=1, bbox=q(100, 200, 200, 220)),
            TextBlock(text="Anna Eriksson  Father's Name: Bo Eriksson", page=1,
                      bbox=q(260, 200, 800, 220)),
        ]
    )
    applicant = FieldSpec(
        name="applicant_name", type="name", labels={"en": ["Applicant"]},
        validator="name", locators=["label"],
    )
    spec = DocTypeSpec(
        doctype_id="some_form", label="Form", country="IN",
        fields=[applicant, FieldSpec(name="father_name", labels={"en": ["Father's Name"]})],
    )
    assert label_locator.locate(applicant, doc, ctx(spec=spec))[0].value == "Anna Eriksson"


def test_a_caption_this_schema_never_declared_still_ends_the_value():
    """Most captions on a real form belong to fields nobody declared. A colon is enough."""
    value, cut = trim.cut_at_next_label("Anna Eriksson  Place of Issue: Bengaluru")
    assert (value, cut) == ("Anna Eriksson", True)


def test_a_distinctive_caption_ends_a_value_even_with_no_colon_to_mark_it():
    """A PAN card punctuates nothing: ``Name  ANNA ERIKSSON   Father's Name  BO ERIKSSON``."""
    value, cut = trim.cut_at_next_label(
        "ANNA ERIKSSON Father's Name BO ERIKSSON", ["Father's Name"], matched="Name"
    )
    assert (value, cut) == ("ANNA ERIKSSON", True)


def test_a_short_caption_word_inside_a_value_is_not_a_place_to_cut():
    """The guard on the rule above: ``Date`` is a word before it is a caption.

    With neither a colon nor enough words to be distinctive, a caption cannot claim a word
    out of an address — otherwise every street with a caption's name in it loses its tail.
    """
    span = "12 Date Palm Road, Bengaluru"
    assert trim.cut_at_next_label(span, ["Date", "Name"]) == (span, False)


def test_a_bilingual_caption_does_not_report_its_other_half_as_the_value():
    """``Name / नाम`` is one caption printed in two scripts, not a caption and a value."""
    doc = view(
        blocks=[
            TextBlock(text="Name / नाम", page=1, bbox=q(60, 120, 260, 140)),
            TextBlock(text="Anna Maria Eriksson", page=1, bbox=q(60, 145, 400, 165)),
        ]
    )
    field = FieldSpec(
        name="name", type="name", labels={"en": ["Name"], "hi": ["नाम"]},
        validator="name", locators=["label"],
    )
    best = label_locator.locate(field, doc, ctx(languages=("en", "hi")))[0]
    assert best.value == "Anna Maria Eriksson"


def test_a_caption_the_span_was_anchored_on_never_cuts_its_own_value():
    """``Date (MM-DD-YYYY)`` must not cut its own value at the word ``Date``."""
    value, cut = trim.cut_at_next_label(
        "2026-03-14", ["Date"], matched="Date (MM-DD-YYYY)"
    )
    assert (value, cut) == ("2026-03-14", False)


def test_a_parenthetical_clarifier_in_a_caption_is_not_the_value():
    """``1 Name (as shown on your income tax return)`` is all caption.

    Splitting on ``Name`` leaves the form's own instructions to the taxpayer, and reporting
    them fills ``full_name`` with them. With the clarifier gone the same-line split has
    nothing left, which is the correct answer: the name is printed on the line below.
    """
    doc = view(
        blocks=[
            TextBlock(text="1 Name (as shown on your income tax return)", page=1,
                      bbox=q(60, 140, 520, 160)),
            TextBlock(text="Anna Maria Eriksson", page=1, bbox=q(60, 165, 520, 185)),
        ]
    )
    field = FieldSpec(
        name="full_name", type="name", labels={"en": ["Name"]}, validator="name",
        locators=["label"],
    )
    best = label_locator.locate(field, doc, ctx())[0]
    assert best.value == "Anna Maria Eriksson"
    assert "-> below" in best.detail


def test_a_clarifier_between_a_caption_and_its_value_is_dropped_not_reported():
    doc = view(blocks=[TextBlock(text="Date (MM-DD-YYYY): 2026-03-14", page=1)])
    field = FieldSpec(
        name="signature_date", type="date", labels={"en": ["Date"]},
        validator="generic_date", locators=["label"],
    )
    assert label_locator.locate(field, doc, ctx())[0].value == "2026-03-14"


# ---------------------------------------------------------------------------
# 2. Type-aware trimming
# ---------------------------------------------------------------------------
def test_a_date_field_trims_a_span_down_to_the_date():
    """The longest substring that satisfies the field, not the leftmost and not the span."""
    field = signature_date_field()
    value, tightened, note = trim.tighten(field, "J. Smith Date: 2026-03-14")
    assert (value, tightened) == ("2026-03-14", True)
    assert note == "tightened to date"


def test_a_date_field_trims_furniture_off_a_value_block():
    doc = view(
        blocks=[
            TextBlock(text="Date of Issue", page=1, bbox=q(100, 200, 260, 220)),
            TextBlock(text="Issued on 27/04/1956 by the RTO", page=1, bbox=q(320, 200, 800, 220)),
        ]
    )
    field = FieldSpec(
        name="issue_date", type="date", labels={"en": ["Date of Issue"]},
        validator="generic_date", locators=["label"],
    )
    best = label_locator.locate(field, doc, ctx())[0]
    assert best.value == "27/04/1956"
    assert "tightened to date" in best.detail


def test_an_address_field_is_never_trimmed_to_a_date_inside_it():
    """An address that mentions a date is still the whole address.

    Tightening is only defined for the shaped types (``date``, ``number``, ``id``). Applying
    a date shape to prose would silently replace an address with six characters of it.
    """
    span = "12 Long Road, Bengaluru 560001, occupied since 27/04/1956"
    field = FieldSpec(name="address", type="address", validator="address")
    assert trim.tighten(field, span) == (span, False, "")

    doc = view(
        blocks=[
            TextBlock(text="Address", page=1, bbox=q(100, 200, 200, 220)),
            TextBlock(text=span, page=1, bbox=q(300, 200, 900, 220)),
        ]
    )
    located = FieldSpec(
        name="address", type="address", labels={"en": ["Address"]}, validator="address",
        locators=["label"],
    )
    assert label_locator.locate(located, doc, ctx())[0].value == span


def test_a_value_its_validator_rejects_is_reported_verbatim_not_replaced():
    """Tightening picks among substrings the validator accepts — unless there are none.

    A UID whose Verhoeff digit fails must reach the reviewer exactly as printed. Silently
    swapping in some other digit run that happens to validate would be the worst possible
    outcome: a checksum-clean value that is not what the document says.
    """
    field = FieldSpec(name="aadhaar_number", type="id", validator="verhoeff_aadhaar")
    assert trim.tighten(field, "9999 9999 0019") == ("9999 9999 0019", False, "")


# ---------------------------------------------------------------------------
# 3. Multi-line capture still works
# ---------------------------------------------------------------------------
def test_a_multi_line_address_is_still_captured_whole():
    doc = view(
        blocks=[
            TextBlock(text="Address", page=1, bbox=q(100, 200, 200, 220)),
            TextBlock(text="12 Long Road", page=1, bbox=q(100, 230, 400, 250)),
            TextBlock(text="Bengaluru 560001", page=1, bbox=q(100, 255, 400, 275)),
        ]
    )
    field = FieldSpec(
        name="address", type="address", labels={"en": ["Address"]}, validator="address",
        locators=["label"],
    )
    best = label_locator.locate(field, doc, ctx())[0]
    assert best.value == "12 Long Road\nBengaluru 560001"
    assert geo.rect_from_quad(best.bbox) == geo.Rect(100, 230, 400, 275)


def test_a_multi_line_value_stops_at_a_sibling_fields_caption_line():
    """The stacked-form case: the next field's caption is printed on its own line."""
    doc = view(
        blocks=[
            TextBlock(text="Address", page=1, bbox=q(100, 200, 200, 220)),
            TextBlock(text="12 Long Road", page=1, bbox=q(100, 230, 400, 250)),
            TextBlock(text="Father's Name", page=1, bbox=q(100, 255, 400, 275)),
            TextBlock(text="Bo Eriksson", page=1, bbox=q(100, 280, 400, 300)),
        ]
    )
    address = FieldSpec(
        name="address", type="address", labels={"en": ["Address"]}, validator="address",
        locators=["label"],
    )
    spec = DocTypeSpec(
        doctype_id="some_form", label="Form", country="IN",
        fields=[address, FieldSpec(name="father_name", labels={"en": ["Father's Name"]})],
    )
    assert label_locator.locate(address, doc, ctx(spec=spec))[0].value == "12 Long Road"


# ---------------------------------------------------------------------------
# 4. Key/value pairs
# ---------------------------------------------------------------------------
def test_a_kv_value_does_not_swallow_the_following_key():
    """A provider value box drawn across the whole line contains the next key.

    The other keys the provider detected on this page are the strongest evidence available
    about what is a caption, so they are exactly what the value is cut against.
    """
    doc = view(
        key_values=[
            KeyValue(key="Signature of U.S. person", value="J. Smith  Date: 2026-03-14",
                     page=1, value_bbox=q(300, 900, 900, 930), confidence=0.95),
            KeyValue(key="Date", value="2026-03-14", page=1, value_bbox=q(700, 900, 900, 930)),
        ]
    )
    field = FieldSpec(
        name="signer_name", type="name", labels={"en": ["Signature of U.S. person"]},
        validator="name", locators=["kv"],
    )
    best = kv_locator.locate(field, doc, ctx())[0]
    assert best.value == "J. Smith"
    assert "cut at next label" in best.detail
    # ...and the raw span is kept, so a reviewer can see what the provider actually returned.
    assert best.raw == "J. Smith  Date: 2026-03-14"


def test_a_kv_date_field_still_reads_the_date_out_of_a_swallowed_pair():
    """The caption inside the span is the one we anchored on, so it is not a cut point.

    Cutting there would leave ``"J. Smith"`` — the value in front of our own repeated
    caption — and lose the date entirely. Tightening is what recovers it.
    """
    doc = view(
        key_values=[
            KeyValue(key="Date", value="J. Smith  Date: 2026-03-14", page=1, confidence=0.9)
        ]
    )
    field = signature_date_field().model_copy(update={"locators": ["kv"]})
    extracted = resolve_field(field, doc, ctx())[0]
    assert extracted.value == "2026-03-14"


# ---------------------------------------------------------------------------
# 5. Tables — a header is not a value
# ---------------------------------------------------------------------------
def header_row_grid() -> Table:
    """A grid whose whole top row is captions; the values are the row beneath."""
    return Table(
        table_id="p1-tbl0", page=1, row_count=2, col_count=2,
        cells=[
            Cell(row=0, col=0, text="Name", is_header=True, bbox=q(50, 100, 300, 130)),
            Cell(row=0, col=1, text="Date of Birth", is_header=True, bbox=q(300, 100, 550, 130)),
            Cell(row=1, col=0, text="Anna Eriksson", bbox=q(50, 130, 300, 160)),
            Cell(row=1, col=1, text="1974-08-12", bbox=q(300, 130, 550, 160)),
        ],
    )


def test_a_table_lookup_steps_past_the_next_header_to_the_value_below():
    """Stepping right from ``Name`` lands on ``Date of Birth`` — a caption, not a value."""
    doc = view(tables=[header_row_grid()])
    field = FieldSpec(
        name="full_name", type="name", labels={"en": ["Name"]}, validator="name",
        locators=["table"],
    )
    best = table_locator.locate(field, doc, ctx())[0]
    assert best.value == "Anna Eriksson"
    assert best.extra["cell"] == "r1c0"
    assert "-> below" in best.detail
    assert all(c.value != "Date of Birth" for c in table_locator.locate(field, doc, ctx()))


def test_a_table_lookup_refuses_a_caption_the_provider_forgot_to_flag():
    """Plenty of payloads flag only the first row. The schema's own labels catch the rest."""
    doc = view(
        tables=[
            Table(
                table_id="p1-tbl1", page=1, row_count=2, col_count=2,
                cells=[
                    Cell(row=0, col=0, text="Name"),
                    Cell(row=0, col=1, text="Date of Birth"),
                    Cell(row=1, col=0, text="Anna Eriksson"),
                    Cell(row=1, col=1, text="1974-08-12"),
                ],
            )
        ]
    )
    field = FieldSpec(
        name="full_name", type="name", labels={"en": ["Name"]}, validator="name",
        locators=["table"],
    )
    spec = DocTypeSpec(
        doctype_id="some_form", label="Form", country="IN",
        fields=[field, FieldSpec(name="date_of_birth", labels={"en": ["Date of Birth"]})],
    )
    assert table_locator.locate(field, doc, ctx(spec=spec))[0].value == "Anna Eriksson"


def test_a_table_cell_that_carries_two_fields_is_trimmed_like_any_other_span():
    doc = view(
        tables=[
            Table(
                table_id="p1-tbl2", page=1, row_count=1, col_count=2,
                cells=[
                    Cell(row=0, col=0, text="Signature of U.S. person", is_header=True),
                    Cell(row=0, col=1, text="J. Smith  Date: 2026-03-14"),
                ],
            )
        ]
    )
    field = FieldSpec(
        name="signer_name", type="name", labels={"en": ["Signature of U.S. person"]},
        validator="name", locators=["table"],
    )
    assert table_locator.locate(field, doc, ctx())[0].value == "J. Smith"


# ---------------------------------------------------------------------------
# 6. Two locators disagree
# ---------------------------------------------------------------------------
def test_when_two_locators_disagree_the_validated_candidate_wins():
    """A trimmed candidate the validator accepted beats an untrimmed one it rejected.

    The label locator binds a clean span it never had to touch, and that span is not a date.
    The key/value locator has to cut its span at the next key before it becomes one. The
    tighter *span* loses; the validated *value* wins — which is the ordering that matters.
    """
    doc = view(
        blocks=[
            TextBlock(text="Date of Birth: N/A pending", page=1, bbox=q(100, 200, 600, 220))
        ],
        key_values=[
            KeyValue(key="Date of Birth", value="27/04/1956  Place: Bengaluru", page=1,
                     value_bbox=q(300, 400, 800, 420)),
        ],
    )
    field = FieldSpec(
        name="date_of_birth", type="date", labels={"en": ["Date of Birth"]},
        validator="generic_date", locators=["label", "kv"],
    )

    untrimmed = label_locator.locate(field, doc, ctx())[0]
    trimmed = kv_locator.locate(field, doc, ctx())[0]
    assert untrimmed.value == "N/A pending" and untrimmed.confidence > trimmed.confidence
    assert trimmed.value == "27/04/1956" and "cut at next label" in trimmed.detail

    extracted = resolve_field(field, doc, ctx())[0]
    assert extracted.value == "27/04/1956"
    assert extracted.locator == "kv"
    assert extracted.verification == "format_valid"
    assert extracted.validator_error == ""


def test_verification_comes_from_the_validator_and_not_from_a_matched_pattern():
    """A pattern is how the locator *found* the value. Nothing checked it."""
    doc = view(
        blocks=[
            TextBlock(text="Reference No", page=1, bbox=q(100, 200, 250, 220)),
            TextBlock(text="REF-0042", page=1, bbox=q(300, 200, 450, 220)),
        ]
    )
    field = FieldSpec(
        name="reference_no", labels={"en": ["Reference No"]}, pattern=r"REF-\d{4}",
        locators=["label"],
    )
    extracted = resolve_field(field, doc, ctx())[0]
    assert extracted.value == "REF-0042"
    assert extracted.verification == "unverified"


# ---------------------------------------------------------------------------
# 7. Geometry
# ---------------------------------------------------------------------------
def test_same_line_is_decided_by_vertical_bbox_overlap_not_by_centres():
    """A tall block and a short one on the same row have different centres."""
    tall = geo.Rect(100, 200, 200, 260)
    short = geo.Rect(260, 215, 500, 235)
    next_row = geo.Rect(260, 300, 500, 320)
    assert tall.cy != short.cy
    assert geo.is_same_line(tall, short)
    assert not geo.is_same_line(tall, next_row)
    assert geo.is_right_of(tall, short, max_dx=200)
    assert not geo.is_right_of(tall, next_row, max_dx=400)


def test_overlap_ratios_are_measured_against_the_smaller_box():
    wide = geo.Rect(100, 200, 700, 220)
    narrow = geo.Rect(620, 240, 700, 260)
    assert geo.h_overlap_ratio(wide, narrow) == 1.0
    assert geo.v_overlap_ratio(wide, geo.Rect(0, 205, 50, 215)) == 1.0
    assert geo.h_overlap_ratio(wide, geo.Rect(800, 240, 900, 260)) == 0.0


def test_a_wide_label_still_binds_the_narrow_value_printed_under_one_end_of_it():
    """The asymmetry this fixes: ratio-against-the-label reported 0.13 and refused."""
    wide_label = geo.Rect(100, 200, 700, 220)
    narrow_value = geo.Rect(620, 240, 700, 260)
    assert geo.is_below(wide_label, narrow_value, max_dy=100)

    doc = view(
        blocks=[
            TextBlock(text="Signature of U.S. person", page=1, bbox=q(100, 900, 700, 920)),
            TextBlock(text="J. Smith", page=1, bbox=q(620, 940, 700, 960)),
        ]
    )
    field = FieldSpec(
        name="signer_name", type="name", labels={"en": ["Signature of U.S. person"]},
        validator="name", locators=["label"],
    )
    best = label_locator.locate(field, doc, ctx())[0]
    assert best.value == "J. Smith"
    assert "-> below" in best.detail


# ---------------------------------------------------------------------------
# 8. The schema is what tells a locator where a value ends
# ---------------------------------------------------------------------------
def test_resolving_a_schema_without_a_doctype_spec_still_sees_the_sibling_captions():
    """``resolve(view, schema)`` must not be the degraded path.

    The list of every caption on the document lives in the schema. A caller that registered
    a schema and never built a DocTypeSpec is the ordinary case for an induced form, and it
    is exactly the case where over-capture used to survive.
    """
    doc = view(
        blocks=[
            TextBlock(text="Applicant", page=1, bbox=q(100, 200, 200, 220)),
            TextBlock(text="Anna Eriksson  Father's Name: Bo Eriksson", page=1,
                      bbox=q(260, 200, 800, 220)),
        ]
    )
    schema = DocSchema(
        doctype_id="some_form", version="1",
        fields=[
            FieldSpec(name="applicant_name", type="name", labels={"en": ["Applicant"]},
                      validator="name", locators=["label"]),
            FieldSpec(name="father_name", type="name", labels={"en": ["Father's Name"]},
                      validator="name", locators=["label"]),
        ],
    )
    values = {f.name: f.value for f in resolve(doc, schema).fields}
    assert values["applicant_name"] == "Anna Eriksson"


def test_the_shipped_w9_declaration_extracts_a_realistic_page_field_by_field():
    """End to end against the registry's own ``us_w9``, not a hand-tuned FieldSpec.

    The regression only exists because of how that doctype declares its labels, so the
    declaration is asserted here too: if the two captions on the signature line ever stop
    being declared on one field, this test's premise is gone and it should be re-thought
    rather than silently keep passing.
    """
    from dce.extract.resolve import extract
    from dce.registry.loader import all_specs

    spec = {s.doctype_id: s for s in all_specs()}["us_w9"]
    declared = {f.name: f for f in spec.fields}["signature_date"]
    assert {"Date", "Signature of U.S. person"} <= set(declared.labels["en"])

    doc = view(
        languages=["en"],
        blocks=[
            TextBlock(text="1 Name (as shown on your income tax return)", page=1,
                      bbox=q(60, 140, 520, 160)),
            TextBlock(text="Anna Maria Eriksson", page=1, bbox=q(60, 165, 520, 185)),
            TextBlock(text="Address (number, street, and apt. or suite no.)", page=1,
                      bbox=q(60, 220, 520, 240)),
            TextBlock(text="12 Long Road", page=1, bbox=q(60, 245, 520, 265)),
            TextBlock(text="Social security number", page=1, bbox=q(600, 220, 900, 240)),
            TextBlock(text="123-45-6789", page=1, bbox=q(600, 245, 900, 265)),
            TextBlock(text=W9_SIGNATURE_LINE, page=1, bbox=q(60, 900, 700, 930)),
        ],
    )
    values = {f.name: f.value for f in extract(doc, spec, spec=spec).fields}
    assert values["signature_date"] == "2026-03-14"
    assert values["full_name"] == "Anna Maria Eriksson"
    assert values["address"] == "12 Long Road"
    assert values["ssn"] == "123-45-6789"
