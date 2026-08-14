"""Extraction tests — pure, offline, no network, no DB, no model.

Layout views are built by hand with explicit geometry so the locator behaviour under test
is the geometry, not an OCR fixture's accidents. Every identifier is synthetic or a
published specimen; no real PII appears here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # keeps the suite runnable without an installed package
    sys.path.insert(0, str(_ROOT))

from dce.extract import validate as V  # noqa: E402
from dce.extract.induce import induce_schema, suggest_type  # noqa: E402
from dce.extract.locators import LOCATORS, LocatorContext  # noqa: E402
from dce.extract.locators import geometry as geo  # noqa: E402
from dce.extract.locators import kv as kv_locator  # noqa: E402
from dce.extract.locators import label as label_locator  # noqa: E402
from dce.extract.locators import mark as mark_locator  # noqa: E402
from dce.extract.locators import mrz as mrz_locator  # noqa: E402
from dce.extract.locators import regex as regex_locator  # noqa: E402
from dce.extract.locators import table as table_locator  # noqa: E402
from dce.extract.resolve import extract, resolve, resolve_field  # noqa: E402
from dce.extract.schema import (  # noqa: E402
    DocSchema,
    SchemaCompatibilityError,
    SchemaRegistry,
    default_schema_for,
    load_from_registry,
)
from dce.models import (  # noqa: E402
    Category,
    Cell,
    DocTypeSpec,
    FieldSpec,
    KeyValue,
    LayoutView,
    Mark,
    PageInfo,
    Table,
    TextBlock,
    Zone,
)

PAGE_W, PAGE_H = 1000.0, 1400.0
#: A Luhn-valid synthetic Canadian SIN, and the same number with a broken check digit.
#: Synthetic on purpose — see the note at the top of tests/test_validate.py.
GOOD_SIN = "193 000 007"
BAD_SIN = "193 000 000"

TD3_LINE1 = "P<UTOERIKSSON<<ANNA<MARIA".ljust(44, "<")
TD3_LINE2 = "L898902C36UTO7408122F1204159ZE184226B<<<<<10"


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


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def test_geometry_right_of_and_below_windows():
    label_rect = geo.Rect(100, 200, 200, 220)
    same_row = geo.Rect(260, 200, 500, 220)
    directly_below = geo.Rect(100, 260, 300, 280)
    next_column = geo.Rect(260, 260, 500, 280)
    assert geo.is_right_of(label_rect, same_row, max_dx=200)
    assert not geo.is_right_of(label_rect, same_row, max_dx=50)      # outside the window
    assert not geo.is_right_of(label_rect, next_column, max_dx=400)  # no vertical overlap
    assert geo.is_below(label_rect, directly_below, max_dy=100)
    assert not geo.is_below(label_rect, directly_below, max_dy=20)   # outside the window
    assert not geo.is_below(label_rect, next_column, max_dy=100)     # no horizontal overlap


def test_geometry_page_size_falls_back_to_observed_extent():
    """A provider that omits page dimensions must not break fractional windows."""
    bare = LayoutView(
        pages=[PageInfo(page=1)],
        blocks=[TextBlock(text="x", page=1, bbox=q(0, 0, 640, 480))],
    )
    assert geo.page_size(bare, 1) == (640.0, 480.0)
    assert geo.page_size(LayoutView(), 1) == (1.0, 1.0)


def test_reading_order_sorts_rows_then_columns():
    items = [
        (1, geo.Rect(400, 100, 500, 120), "right-top"),
        (1, geo.Rect(100, 100, 200, 120), "left-top"),
        (1, geo.Rect(100, 300, 200, 320), "left-bottom"),
    ]
    assert geo.reading_order(items) == ["left-top", "right-top", "left-bottom"]


# ---------------------------------------------------------------------------
# Label-anchored locator
# ---------------------------------------------------------------------------
def test_label_binds_the_value_to_the_right_within_the_window():
    doc = view(
        blocks=[
            TextBlock(text="Date of Birth", page=1, bbox=q(100, 200, 240, 220)),
            TextBlock(text="27/04/1956", page=1, bbox=q(300, 200, 430, 220)),
            TextBlock(text="12/12/2001", page=1, bbox=q(300, 400, 430, 420)),  # decoy
        ]
    )
    field = FieldSpec(
        name="date_of_birth", type="date", labels={"en": ["Date of Birth"]},
        validator="generic_date", locators=["label"],
    )
    best = label_locator.locate(field, doc, ctx())[0]
    assert best.value == "27/04/1956"
    assert "-> right" in best.detail
    assert best.page == 1 and best.bbox == q(300, 200, 430, 220)


def test_label_does_not_reach_past_the_window():
    """label_window_x is a fraction of page width; beyond it, there is no binding."""
    doc = view(
        blocks=[
            TextBlock(text="Date of Birth", page=1, bbox=q(50, 200, 190, 220)),
            TextBlock(text="27/04/1956", page=1, bbox=q(900, 200, 990, 220)),
        ]
    )
    field = FieldSpec(
        name="date_of_birth", labels={"en": ["Date of Birth"]}, locators=["label"]
    )
    assert label_locator.locate(field, doc, ctx()) == []


def test_label_binds_the_value_below_and_absorbs_continuation_lines():
    doc = view(
        blocks=[
            TextBlock(text="Address", page=1, bbox=q(100, 200, 200, 220)),
            TextBlock(text="12 Long Road", page=1, bbox=q(100, 230, 400, 250)),
            TextBlock(text="Bengaluru 560001", page=1, bbox=q(100, 255, 400, 275)),
            TextBlock(text="Signature:", page=1, bbox=q(100, 285, 400, 305)),
        ]
    )
    field = FieldSpec(
        name="address", type="address", labels={"en": ["Address"]},
        validator="address", locators=["label"],
    )
    best = label_locator.locate(field, doc, ctx())[0]
    assert best.value == "12 Long Road\nBengaluru 560001"
    assert "-> below" in best.detail
    # The union bbox spans both lines so the review UI highlights the whole value.
    assert geo.rect_from_quad(best.bbox) == geo.Rect(100, 230, 400, 275)
    # The line break survives all the way to the validator, which is what lets it produce
    # a properly punctuated single-line form.
    assert V.validate("address", best.value).normalized == "12 Long Road, Bengaluru 560001"


def test_label_stops_a_multiline_value_at_the_next_caption():
    """A trailing 'Signature:' block is another field's caption, not address line three."""
    doc = view(
        blocks=[
            TextBlock(text="Address", page=1, bbox=q(100, 200, 200, 220)),
            TextBlock(text="12 Long Road", page=1, bbox=q(100, 230, 400, 250)),
            TextBlock(text="Signature:", page=1, bbox=q(100, 255, 400, 275)),
        ]
    )
    field = FieldSpec(
        name="address", type="address", labels={"en": ["Address"]}, locators=["label"]
    )
    assert label_locator.locate(field, doc, ctx())[0].value == "12 Long Road"


def test_label_reads_a_same_line_pair_without_geometry_guessing():
    doc = view(blocks=[TextBlock(text="Date of Birth : 27/04/1956", page=1)])
    field = FieldSpec(
        name="date_of_birth", labels={"en": ["Date of Birth"]}, locators=["label"]
    )
    best = label_locator.locate(field, doc, ctx())[0]
    assert best.value == "27/04/1956"
    assert "same line" in best.detail


def test_label_match_is_fuzzy_enough_for_ocr_noise():
    doc = view(
        blocks=[
            TextBlock(text="Date of Birth", page=1, bbox=q(100, 200, 240, 220)),
            TextBlock(text="27/04/1956", page=1, bbox=q(300, 200, 430, 220)),
        ]
    )
    field = FieldSpec(
        name="date_of_birth", labels={"en": ["Date of Birth"]}, locators=["label"]
    )
    assert label_locator.locate(field, doc, ctx())[0].value == "27/04/1956"


def test_a_value_failing_the_fields_pattern_is_rejected():
    """An address must not bind to a date just because the label matched."""
    doc = view(
        blocks=[
            TextBlock(text="Address", page=1, bbox=q(100, 200, 200, 220)),
            TextBlock(text="12/03/1999", page=1, bbox=q(300, 200, 430, 220)),
        ]
    )
    unguarded = FieldSpec(
        name="address", labels={"en": ["Address"]}, locators=["label"]
    )
    assert label_locator.locate(unguarded, doc, ctx())[0].value == "12/03/1999"

    guarded = unguarded.model_copy(update={"pattern": r"[^\W\d_]{3,}"})
    assert label_locator.locate(guarded, doc, ctx()) == []


def test_a_pattern_narrows_a_noisy_value_instead_of_dropping_it():
    doc = view(
        blocks=[
            TextBlock(text="SIN", page=1, bbox=q(100, 200, 160, 220)),
            TextBlock(text=f"{GOOD_SIN} (masked)", page=1, bbox=q(200, 200, 500, 220)),
        ]
    )
    field = FieldSpec(
        name="sin_number", labels={"en": ["SIN"]}, locators=["label"],
        pattern=r"\d{3}\s?\d{3}\s?\d{3}",
    )
    best = label_locator.locate(field, doc, ctx())[0]
    assert best.value == GOOD_SIN
    assert "narrowed to pattern" in best.detail


# ---------------------------------------------------------------------------
# Key/value locator
# ---------------------------------------------------------------------------
def test_kv_matches_a_provider_key_fuzzily_and_keeps_its_confidence():
    doc = view(
        key_values=[
            KeyValue(key="Date of Birth:", value="27/04/1956", page=1,
                     value_bbox=q(300, 200, 430, 220), confidence=0.9),
            KeyValue(key="Place of Issue", value="Bengaluru", page=1),
        ]
    )
    field = FieldSpec(
        name="date_of_birth", labels={"en": ["Date of Birth"]}, locators=["kv"]
    )
    found = kv_locator.locate(field, doc, ctx())
    assert [c.value for c in found] == ["27/04/1956"]
    assert found[0].bbox == q(300, 200, 430, 220)


def test_kv_respects_declared_languages():
    doc = view(key_values=[KeyValue(key="Fecha de nacimiento", value="27/04/1956")])
    field = FieldSpec(
        name="date_of_birth",
        labels={"en": ["Date of Birth"], "es": ["Fecha de nacimiento"]},
        locators=["kv"],
    )
    assert kv_locator.locate(field, doc, ctx(languages=("es",)))[0].value == "27/04/1956"


# ---------------------------------------------------------------------------
# Table locator
# ---------------------------------------------------------------------------
def merged_header_table() -> Table:
    """A table whose 'Taxable Income' header is merged across columns 1 and 2.

    The value is printed under the *second* half of the merge, which is what breaks naive
    "cell directly below the header's own column" addressing.
    """
    return Table(
        table_id="p1-tbl0", page=1, row_count=2, col_count=3,
        cells=[
            Cell(row=0, col=0, text="Assessment Year", is_header=True, bbox=q(50, 100, 200, 130)),
            Cell(row=0, col=1, col_span=2, text="Taxable Income", is_header=True,
                 bbox=q(200, 100, 500, 130)),
            Cell(row=1, col=0, text="2025-26", bbox=q(50, 130, 200, 160)),
            Cell(row=1, col=1, text="", bbox=q(200, 130, 350, 160)),
            Cell(row=1, col=2, text="1,23,456.78", bbox=q(350, 130, 500, 160)),
        ],
    )


def test_table_column_header_addressing_across_a_merged_span():
    doc = view(tables=[merged_header_table()])
    field = FieldSpec(
        name="taxable_income", type="number", labels={"en": ["Taxable Income"]},
        validator="amount", locators=["table"],
    )
    best = table_locator.locate(field, doc, ctx())[0]
    assert best.value == "1,23,456.78"
    assert best.extra["cell"] == "r1c2"
    assert best.bbox == q(350, 130, 500, 160)


def test_table_row_header_addressing_takes_the_cell_to_the_right():
    doc = view(
        tables=[
            Table(
                table_id="p1-tbl1", page=1, row_count=2, col_count=2,
                cells=[
                    Cell(row=0, col=0, text="Name", is_header=True),
                    Cell(row=0, col=1, text="Anna Maria Eriksson"),
                    Cell(row=1, col=0, text="Nationality", is_header=True),
                    Cell(row=1, col=1, text="Utopian"),
                ],
            )
        ]
    )
    field = FieldSpec(name="full_name", labels={"en": ["Name"]}, locators=["table"])
    best = table_locator.locate(field, doc, ctx())[0]
    assert best.value == "Anna Maria Eriksson"
    assert "-> right" in best.detail


def test_table_multi_field_collects_every_row_under_the_header():
    doc = view(
        tables=[
            Table(
                table_id="p1-tbl2", page=1, row_count=3, col_count=1,
                cells=[
                    Cell(row=0, col=0, text="Director", is_header=True),
                    Cell(row=1, col=0, text="Anna Eriksson"),
                    Cell(row=2, col=0, text="Bo Nilsson"),
                ],
            )
        ]
    )
    field = FieldSpec(
        name="director", multi=True, labels={"en": ["Director"]}, locators=["table"]
    )
    values = [c.value for c in table_locator.locate(field, doc, ctx())]
    assert values[:2] == ["Anna Eriksson", "Bo Nilsson"]


# ---------------------------------------------------------------------------
# Checkbox locator
# ---------------------------------------------------------------------------
def gender_form() -> LayoutView:
    """'Gender' heading with two options; 'Female' is the ticked one."""
    return view(
        blocks=[
            TextBlock(text="Gender", page=1, bbox=q(100, 300, 190, 320)),
            TextBlock(text="Male", page=1, bbox=q(150, 340, 210, 360)),
            TextBlock(text="Female", page=1, bbox=q(150, 380, 230, 400)),
        ],
        marks=[
            Mark(state="unselected", page=1, bbox=q(110, 340, 130, 360)),
            Mark(state="selected", page=1, bbox=q(110, 380, 130, 400)),
        ],
    )


def test_checkbox_binds_to_its_nearest_label_and_emits_the_selected_option():
    bound = mark_locator.bind_marks(gender_form())
    assert [text for _mark, _block, text in bound] == ["Male", "Female"]

    field = FieldSpec(name="gender", labels={"en": ["Gender"]}, locators=["mark"])
    best = mark_locator.locate(field, gender_form(), ctx())[0]
    assert best.value == "Female"
    assert best.raw.startswith("☒")
    assert "selected option" in best.detail


def test_checkbox_answers_a_boolean_field_named_after_the_option():
    doc = view(
        blocks=[TextBlock(text="US person", page=1, bbox=q(150, 340, 260, 360))],
        marks=[Mark(state="unselected", page=1, bbox=q(110, 340, 130, 360))],
    )
    field = FieldSpec(
        name="is_us_person", type="bool", labels={"en": ["US person"]}, locators=["mark"]
    )
    assert mark_locator.locate(field, doc, ctx())[0].value == "false"


def test_unselected_options_never_become_a_group_answer():
    doc = gender_form()
    doc.marks[1].state = "unselected"
    field = FieldSpec(name="gender", labels={"en": ["Gender"]}, locators=["mark"])
    assert mark_locator.locate(field, doc, ctx()) == []


# ---------------------------------------------------------------------------
# Regex locator
# ---------------------------------------------------------------------------
def test_regex_sweeps_in_zone_order_and_prefers_the_title():
    doc = view(
        blocks=[
            TextBlock(text="SSN 123-45-6789", zone=Zone.title, page=1, bbox=q(0, 0, 300, 30)),
            TextBlock(text="987-65-4321", zone=Zone.furniture, page=1, bbox=q(0, 1350, 300, 1380)),
        ]
    )
    field = FieldSpec(name="ssn", type="id", validator="ssn", locators=["regex"])
    found = regex_locator.locate(field, doc, ctx())
    assert found[0].value == "123-45-6789"
    assert "title" in found[0].detail
    assert found[-1].value == "987-65-4321"    # furniture is penalised, not discarded


def test_regex_falls_back_to_the_doctype_id_patterns_only_for_id_fields():
    doc = view(blocks=[TextBlock(text=f"SIN {GOOD_SIN}", page=1)])
    context = ctx(id_patterns=(r"\d{3}\s\d{3}\s\d{3}",))
    id_field = FieldSpec(name="uid", type="id", locators=["regex"])
    assert regex_locator.locate(id_field, doc, context)[0].value == GOOD_SIN
    name_field = FieldSpec(name="full_name", type="name", locators=["regex"])
    assert regex_locator.locate(name_field, doc, context) == []


# ---------------------------------------------------------------------------
# MRZ locator
# ---------------------------------------------------------------------------
def passport_view() -> LayoutView:
    return view(
        blocks=[
            TextBlock(text="PASSPORT", zone=Zone.title, page=1, bbox=q(100, 50, 400, 90)),
            TextBlock(text="Surname / Nom", page=1, bbox=q(100, 200, 250, 220)),
            TextBlock(text="ERIKSSON", page=1, bbox=q(300, 200, 450, 220)),
            TextBlock(text=f"{TD3_LINE1}\n{TD3_LINE2}", page=1, bbox=q(50, 1200, 950, 1280)),
        ]
    )


def test_mrz_td3_round_trip():
    """Parse the ICAO specimen zone and get all seven fields back, check digits verified."""
    docs = mrz_locator.find_mrz(passport_view())
    assert len(docs) == 1
    parsed = docs[0]
    assert parsed.kind == "TD3"
    assert parsed.checksum_ok is True
    assert parsed.fields == {
        "document_code": "P",
        "issuing_state": "UTO",
        "surname": "ERIKSSON",
        "given_names": "ANNA MARIA",
        "document_number": "L898902C3",
        "nationality": "UTO",
        "date_of_birth": "1974-08-12",
        "sex": "F",
        "expiry_date": "2012-04-15",
        "personal_number": "ZE184226B",
    }
    # ...and the block it reports is exactly what the validator re-verifies.
    assert V.validate("mrz_td3", parsed.block).ok


def test_mrz_fields_reach_the_result_as_checksum_verified():
    schema = DocSchema(
        doctype_id="passport", version="1",
        fields=[
            FieldSpec(name="surname", attribute_key="identity.surname", type="name",
                      pii=True, locators=["mrz", "label"]),
            FieldSpec(name="given_names", attribute_key="identity.given_names",
                      type="name", pii=True, locators=["mrz"]),
            FieldSpec(name="passport_number", attribute_key="id.passport_number",
                      type="id", required=True, pii=True, locators=["mrz"]),
            FieldSpec(name="date_of_birth", attribute_key="identity.date_of_birth",
                      type="date", validator="iso_date", locators=["mrz"]),
            FieldSpec(name="sex", attribute_key="identity.sex", locators=["mrz"]),
            FieldSpec(name="expiry_date", attribute_key="doc.expiry_date", type="date",
                      validator="iso_date", locators=["mrz"]),
            FieldSpec(name="nationality", attribute_key="identity.nationality",
                      locators=["mrz"]),
        ],
    )
    result = resolve(passport_view(), schema)
    values = {f.name: f.value for f in result.fields}
    assert values == {
        "surname": "ERIKSSON",
        "given_names": "ANNA MARIA",
        "passport_number": "L898902C3",
        "date_of_birth": "1974-08-12",
        "sex": "F",
        "expiry_date": "2012-04-15",
        "nationality": "UTO",
    }
    # The zone's check digits cover the name as much as the document number, so a name
    # pulled from a verified MRZ is checksum-verified even though no name validator could
    # ever prove it.
    assert {f.verification for f in result.fields} == {"checksum_verified"}
    assert all(f.locator == "mrz" for f in result.fields)
    assert result.needs_review is False


def test_mrz_with_a_corrupted_glyph_is_reported_but_not_trusted():
    doc = passport_view()
    doc.blocks[-1].text = f"{TD3_LINE1}\n{TD3_LINE2.replace('L898902C36', 'L898902C35')}"
    parsed = mrz_locator.find_mrz(doc)[0]
    assert parsed.checksum_ok is False
    field = FieldSpec(name="surname", attribute_key="identity.surname", locators=["mrz"])
    candidate = mrz_locator.locate(field, doc, ctx())[0]
    assert candidate.verified is False
    assert candidate.confidence < 0.5
    assert "check digits NOT verified" in candidate.detail


def test_mrz_is_ignored_for_fields_it_cannot_answer():
    field = FieldSpec(name="taxable_income", attribute_key="income.amount", locators=["mrz"])
    assert mrz_locator.locate(field, passport_view(), ctx()) == []


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def test_resolve_prefers_a_checksum_verified_candidate_over_a_fuzzy_label_match():
    """The label points at a number whose checksum fails; a valid one sits elsewhere.

    The label locator runs first and has the higher prior, so the only thing that can
    demote its answer is the checksum — which is the point.
    """
    doc = view(
        blocks=[
            TextBlock(text="Social Insurance Number", page=1, bbox=q(100, 200, 250, 220)),
            TextBlock(text=BAD_SIN, page=1, bbox=q(300, 200, 450, 220)),
            TextBlock(text=GOOD_SIN, page=1, bbox=q(100, 600, 450, 620)),
        ]
    )
    field = FieldSpec(
        name="sin_number", attribute_key="id.sin", type="id", required=True,
        pii=True, labels={"en": ["Social Insurance Number"]}, validator="sin_luhn",
        locators=["label", "regex"],
    )
    extracted = resolve_field(field, doc, ctx())[0]
    assert extracted.value == GOOD_SIN
    assert extracted.normalized == "193-000-007"
    assert extracted.verification == "checksum_verified"
    assert extracted.locator == "regex"
    assert extracted.confidence >= 0.9
    assert extracted.page == 1 and extracted.bbox is not None


def test_resolve_stops_at_the_first_checksum_verified_candidate():
    """Nothing beats a value that carries its own proof, so nothing else is run."""
    doc = view(
        key_values=[KeyValue(key="SIN", value=GOOD_SIN, page=1)],
        blocks=[TextBlock(text=f"SIN {BAD_SIN}", page=1, bbox=q(100, 600, 450, 620))],
    )
    field = FieldSpec(
        name="sin_number", type="id", labels={"en": ["SIN"]},
        validator="sin_luhn", locators=["kv", "label", "regex"],
    )
    extracted = resolve_field(field, doc, ctx())[0]
    assert extracted.locator == "kv"
    assert extracted.verification == "checksum_verified"


def test_resolve_reports_a_rejected_value_rather_than_a_silent_blank():
    """A reviewer needs to see what was on the page and why it was not trusted."""
    doc = view(
        blocks=[
            TextBlock(text="SIN", page=1, bbox=q(100, 200, 250, 220)),
            TextBlock(text=BAD_SIN, page=1, bbox=q(300, 200, 450, 220)),
        ]
    )
    field = FieldSpec(
        name="sin_number", type="id", required=True, labels={"en": ["SIN"]},
        validator="sin_luhn", locators=["label"],
    )
    extracted = resolve_field(field, doc, ctx())[0]
    assert extracted.value == BAD_SIN
    assert extracted.verification == "unverified"
    assert extracted.validator_error == "luhn_check_failed"
    assert extracted.confidence < 0.4


def test_resolve_reports_a_field_nothing_was_found_for():
    field = FieldSpec(name="ghost", labels={"en": ["Nowhere"]}, locators=["label", "kv"])
    extracted = resolve_field(field, view(), ctx())[0]
    assert extracted.value is None
    assert extracted.validator_error == "no_candidate_found"


def test_resolve_soft_validator_failure_stays_usable_but_flagged():
    """An RFC whose homoclave does not check out is still an RFC; never checksum-verified.

    OCR mangles the homoclave constantly, so the shape is strict and the check digit is
    advisory. Hard-rejecting here would throw away real taxpayer identifiers.
    """
    doc = view(key_values=[KeyValue(key="RFC", value="AAA010101AA9", page=1)])
    field = FieldSpec(
        name="rfc", type="id", labels={"en": ["RFC"]},
        validator="rfc", locators=["kv"],
    )
    extracted = resolve_field(field, doc, ctx())[0]
    assert extracted.value == "AAA010101AA9"
    assert extracted.verification == "format_valid"
    assert "check_digit_soft_fail" in extracted.validator_error


def test_resolve_multi_field_deduplicates_values():
    doc = view(
        tables=[
            Table(
                table_id="t", page=1, row_count=4, col_count=1,
                cells=[
                    Cell(row=0, col=0, text="Director", is_header=True),
                    Cell(row=1, col=0, text="Anna Eriksson"),
                    Cell(row=2, col=0, text="Bo Nilsson"),
                    Cell(row=3, col=0, text="anna eriksson"),
                ],
            )
        ]
    )
    field = FieldSpec(
        name="director", attribute_key="ownership.director", multi=True,
        labels={"en": ["Director"]}, validator="name", locators=["table"],
    )
    values = [f.value for f in resolve_field(field, doc, ctx())]
    assert values == ["Anna Eriksson", "Bo Nilsson"]


def test_resolve_populates_missing_required_and_needs_review():
    doc = view(blocks=[TextBlock(text="Nothing useful here", page=1, bbox=q(0, 0, 10, 10))])
    schema = DocSchema(
        doctype_id="ca_sin_confirmation", version="2",
        fields=[
            FieldSpec(name="sin_number", required=True, validator="sin_luhn",
                      locators=["regex"]),
            FieldSpec(name="full_name", locators=["label"]),
        ],
    )
    result = resolve(doc, schema)
    assert result.doctype_id == "ca_sin_confirmation"
    assert result.schema_version == "2"
    assert result.missing_required == ["sin_number"]
    assert result.needs_review is True
    assert result.fill_rate == 0.0
    assert result.ms >= 0


def test_resolve_refuses_an_inactive_schema():
    """An induced draft is a proposal. Running it would make the proposal invisible."""
    draft = DocSchema(doctype_id="unknown_form", version="0.1-draft", active=False)
    with pytest.raises(ValueError, match="inactive"):
        resolve(view(), draft)


def test_a_misbehaving_locator_cannot_take_the_field_down():
    def exploding(field, doc, context):
        raise ValueError("boom")

    LOCATORS["exploding"] = exploding
    try:
        doc = view(key_values=[KeyValue(key="Name", value="Anna Eriksson", page=1)])
        field = FieldSpec(
            name="full_name", labels={"en": ["Name"]}, locators=["exploding", "kv"]
        )
        assert resolve_field(field, doc, ctx())[0].value == "Anna Eriksson"
    finally:
        del LOCATORS["exploding"]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
def sin_spec() -> DocTypeSpec:
    return DocTypeSpec(
        doctype_id="ca_sin_confirmation", label="SIN Confirmation", country="CA",
        category=Category.identity, officially_valid=True,
        fields=[
            FieldSpec(name="sin_number", attribute_key="id.sin", type="id",
                      required=True, pii=True, validator="sin_luhn"),
            FieldSpec(name="full_name", attribute_key="identity.full_name", type="name"),
        ],
    )


def test_default_schema_is_derived_from_the_doctype_spec():
    """Declaring a doctype gives you a working schema for free."""
    schema = default_schema_for(sin_spec())
    assert schema.doctype_id == "ca_sin_confirmation"
    assert schema.source == "derived"
    assert schema.field_names == ["sin_number", "full_name"]
    assert schema.required_fields == ["sin_number"]
    # A copy, not a reference: mutating the schema must not edit the doctype registry.
    schema.fields[0].required = False
    assert sin_spec().fields[0].required is True


def test_load_from_registry_accepts_an_explicit_spec_and_a_duck_typed_registry():
    assert load_from_registry("ca_sin_confirmation", spec=sin_spec()).field_names == [
        "sin_number", "full_name",
    ]

    class FakeRegistry:
        @staticmethod
        def get_doctype(doctype_id: str) -> DocTypeSpec | None:
            return sin_spec() if doctype_id == "ca_sin_confirmation" else None

    loaded = load_from_registry("ca_sin_confirmation", doctype_registry=FakeRegistry)
    assert loaded is not None and loaded.doctype_id == "ca_sin_confirmation"
    assert load_from_registry("nope", doctype_registry=FakeRegistry) is None


def test_schema_registry_latest_resolution_orders_versions_numerically():
    reg = SchemaRegistry()
    for version in ("1", "2", "10", "1.5"):
        reg.register(DocSchema(doctype_id="us_w9", version=version))
    assert reg.versions("us_w9") == ["1", "1.5", "2", "10"]
    assert reg.get("us_w9").version == "10"
    assert reg.get("us_w9", "2").version == "2"
    assert reg.get("us_w9", "99") is None
    assert reg.doctypes() == ["us_w9"]


def test_adding_a_field_within_a_version_is_allowed():
    reg = SchemaRegistry()
    reg.register(
        DocSchema(doctype_id="us_w9", version="1", fields=[FieldSpec(name="pan_number")])
    )
    grown = DocSchema(
        doctype_id="us_w9", version="1",
        fields=[FieldSpec(name="pan_number"), FieldSpec(name="father_name")],
    )
    assert reg.register(grown).field_names == ["pan_number", "father_name"]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"type": "date"}, "changed type"),
        ({"validator": "generic_date"}, "changed validator"),
        ({"multi": True}, "changed multi"),
    ],
)
def test_changing_a_field_within_a_version_is_refused(change, expected):
    """Consumers cache by version string; changing one under them is the bug this stops."""
    reg = SchemaRegistry()
    original = FieldSpec(name="ssn_number", type="id", validator="ssn")
    reg.register(DocSchema(doctype_id="us_w9", version="1", fields=[original]))
    mutated = DocSchema(
        doctype_id="us_w9", version="1", fields=[original.model_copy(update=change)]
    )
    with pytest.raises(SchemaCompatibilityError) as excinfo:
        reg.register(mutated)
    assert expected in str(excinfo.value)
    assert "new version" in str(excinfo.value)
    # The registered version is untouched by the rejected attempt.
    assert reg.get("us_w9", "1").fields[0].type == "id"


def test_removing_a_field_within_a_version_is_refused():
    reg = SchemaRegistry()
    reg.register(
        DocSchema(doctype_id="us_w9", version="1",
                  fields=[FieldSpec(name="pan_number"), FieldSpec(name="father_name")])
    )
    with pytest.raises(SchemaCompatibilityError, match="was removed"):
        reg.register(
            DocSchema(doctype_id="us_w9", version="1", fields=[FieldSpec(name="pan_number")])
        )


def test_a_new_version_is_the_escape_hatch_for_a_breaking_change():
    reg = SchemaRegistry()
    reg.register(
        DocSchema(doctype_id="us_w9", version="1",
                  fields=[FieldSpec(name="pan_number", type="id")])
    )
    reg.register(
        DocSchema(doctype_id="us_w9", version="2",
                  fields=[FieldSpec(name="pan_number", type="string")])
    )
    assert reg.get("us_w9", "1").fields[0].type == "id"
    assert reg.get("us_w9").fields[0].type == "string"


def test_inactive_schemas_are_invisible_until_a_human_activates_them():
    reg = SchemaRegistry()
    reg.register(
        DocSchema(doctype_id="unknown_form", version="0.1-draft", active=False,
                  source="induced")
    )
    assert reg.get("unknown_form") is None
    assert reg.get("unknown_form", "0.1-draft") is None
    assert reg.get("unknown_form", include_inactive=True) is not None
    activated = reg.activate("unknown_form", "0.1-draft")
    assert activated.active is True
    assert reg.get("unknown_form") is not None
    with pytest.raises(KeyError):
        reg.activate("unknown_form", "9")


# ---------------------------------------------------------------------------
# Induction
# ---------------------------------------------------------------------------
def sample_view(index: int, *, dob: str, amount: str) -> LayoutView:
    """One of several examples of the same unseen form, with per-document values."""
    return view(
        doc_id=f"sample-{index}",
        languages=["en"],
        key_values=[
            KeyValue(key="Date of Birth", value=dob, page=1),
            KeyValue(key="Applicant Name", value="Anna Maria Eriksson", page=1),
        ],
        blocks=[
            TextBlock(text="Reference No:", page=1, bbox=q(100, 200, 250, 220)),
            TextBlock(text=f"REF-{index:04d}", page=1, bbox=q(100, 230, 250, 250)),
            TextBlock(text=f"One-off note {index}", page=1, bbox=q(100, 300, 600, 320)),
        ],
        tables=[
            Table(
                table_id=f"p1-tbl-{index}", page=1, row_count=2, col_count=1,
                cells=[
                    Cell(row=0, col=0, text="Declared Income", is_header=True),
                    Cell(row=1, col=0, text=amount),
                ],
            )
        ],
    )


def induced_draft() -> DocSchema:
    views = [
        sample_view(1, dob="27/04/1956", amount="1,23,456.78"),
        sample_view(2, dob="01/11/1974", amount="98,000.00"),
        sample_view(3, dob="15/03/1989", amount="4,50,000.00"),
    ]
    return induce_schema(views, doctype_id="unknown_form")


def test_induce_proposes_fields_from_three_samples_and_marks_the_schema_inactive():
    draft = induced_draft()
    assert draft.doctype_id == "unknown_form"
    assert draft.source == "induced"
    assert draft.active is False              # induction proposes; a human activates
    assert "DRAFT" in draft.notes

    names = set(draft.field_names)
    assert {"date_of_birth", "applicant_name", "declared_income", "reference_no"} <= names
    # A block that differed in every sample is that document's noise, not the form's shape.
    assert not any(name.startswith("one_off") for name in names)


def test_induced_fields_carry_types_locators_and_their_evidence():
    fields = {f.name: f for f in induced_draft().fields}

    dob = fields["date_of_birth"]
    assert (dob.type, dob.validator) == ("date", "generic_date")
    assert dob.locators[0] == "kv"            # it was found as a provider key/value pair
    assert dob.labels["en"][0] == "Date of Birth"
    assert "support 3/3" in dob.notes

    income = fields["declared_income"]
    assert (income.type, income.validator) == ("number", "amount")
    assert income.locators[0] == "table"      # it was found as a table header

    name_field = fields["applicant_name"]
    assert name_field.type == "name"
    assert name_field.pii is True

    # Induction never asserts a field is mandatory, and never guesses an ontology key.
    assert all(not f.required for f in induced_draft().fields)
    assert all(f.attribute_key == "" for f in induced_draft().fields)


def test_an_induced_draft_cannot_be_extracted_with_until_it_is_activated():
    reg = SchemaRegistry()
    draft = reg.register(induced_draft())
    with pytest.raises(ValueError, match="inactive"):
        resolve(sample_view(1, dob="27/04/1956", amount="1"), draft)
    activated = reg.activate(draft.doctype_id, draft.version)
    result = resolve(sample_view(1, dob="27/04/1956", amount="1,23,456.78"), activated)
    assert {f.name: f.normalized for f in result.fields}["date_of_birth"] == "1956-04-27"


def test_induce_clusters_near_duplicate_labels_into_one_field():
    """OCR spells the same caption three ways; that is one field, not three."""
    views = [
        view(key_values=[KeyValue(key="Date of Birth", value="27/04/1956")]),
        view(key_values=[KeyValue(key="Date of Birth.", value="01/11/1974")]),
        view(key_values=[KeyValue(key="Date of  Birth", value="15/03/1989")]),
    ]
    draft = induce_schema(views, doctype_id="unknown_form")
    assert draft.field_names == ["date_of_birth"]
    assert draft.fields[0].labels["en"][0] == "Date of Birth"


def test_induce_honours_the_support_threshold():
    views = [
        view(key_values=[KeyValue(key="Everywhere", value="a")]),
        view(key_values=[KeyValue(key="Everywhere", value="b")]),
        view(key_values=[KeyValue(key="Once only", value="c")]),
    ]
    assert induce_schema(views, doctype_id="x").field_names == ["everywhere"]
    lenient = induce_schema(views, doctype_id="x", min_support=0.3)
    assert set(lenient.field_names) == {"everywhere", "once_only"}


def test_induce_survives_being_given_nothing():
    empty = induce_schema([], doctype_id="x")
    assert empty.fields == [] and empty.active is False


def test_suggest_type_only_proposes_a_validator_the_values_actually_pass():
    """A digit run that fails its checksum is not evidence of a SIN column."""
    assert suggest_type([GOOD_SIN, GOOD_SIN]) == ("id", "sin_luhn")
    assert suggest_type([BAD_SIN, BAD_SIN])[1] != "sin_luhn"
    assert suggest_type(["27/04/1956", "01/11/1974"]) == ("date", "generic_date")
    assert suggest_type(["$1,234.56", "98,000.00"]) == ("number", "amount")
    assert suggest_type(["Anna Eriksson", "Bo Nilsson"]) == ("name", "name")
    assert suggest_type([]) == ("string", None)


# ---------------------------------------------------------------------------
# The public entry point the API layer binds to
# ---------------------------------------------------------------------------
def test_extract_accepts_a_doctype_id_and_derives_a_schema_from_the_spec():
    doc = view(blocks=[TextBlock(text=f"SIN {GOOD_SIN}", page=1, bbox=q(100, 100, 400, 130))])
    result = extract(doc, "ca_sin_confirmation", spec=sin_spec())
    assert result.doctype_id == "ca_sin_confirmation"
    assert result.schema_version == "1"
    by_name = {f.name: f for f in result.fields}
    assert by_name["sin_number"].normalized == "193-000-007"
    assert by_name["sin_number"].verification == "checksum_verified"


def test_extract_accepts_the_call_shape_the_api_layer_uses():
    """``ExtractorPort`` calls ``extract(view, spec, settings=..., schema_version=...)``.

    It passes the DocTypeSpec positionally — it already has it from the classification step
    — and names the version ``schema_version``. Both are pinned here so the seam between
    the API and this tier cannot drift silently.
    """
    import inspect

    from dce.config import get_settings
    from dce.extract import extract as entry_point

    params = inspect.signature(entry_point).parameters
    assert "settings" in params and "schema_version" in params

    doc = view(blocks=[TextBlock(text=f"SIN {GOOD_SIN}", page=1, bbox=q(100, 100, 400, 130))])
    result = entry_point(
        doc, sin_spec(), settings=get_settings(), schema_version="latest"
    )
    assert result.doctype_id == "ca_sin_confirmation"
    assert result.fields[0].normalized == "193-000-007"


def test_extract_on_an_unknown_doctype_flags_review_instead_of_raising(monkeypatch):
    """A doctype with no schema and no spec yields an empty result, flagged, not a crash.

    The doctype lookup is stubbed so this stays a unit test of the extraction tier: whether
    the sibling doctype registry currently imports is not this test's business.
    """
    monkeypatch.setattr(
        "dce.extract.schema._lookup_spec", lambda doctype_id, source=None: None
    )
    result = extract(view(), "not_a_doctype")
    assert result.doctype_id == "not_a_doctype"
    assert result.fields == []
    assert result.needs_review is True


def test_regex_will_not_borrow_a_pattern_another_field_already_claims():
    """A real defect this caught on a shipped identity pack.

    The doctype lists the identifier shape in ``id_patterns`` for classification, and the
    identifier field declares the same shape as its own pattern. Without this guard, a
    second id field with no pattern of its own borrowed the doctype pattern and reported
    the same number again under the wrong field name.
    """
    uid_pattern = r"\b[1-79]\d{2}\s?\d{3}\s?\d{3}\b"
    spec = DocTypeSpec(
        doctype_id="ca_sin_confirmation", label="SIN Confirmation", country="CA",
        id_patterns=[uid_pattern],
        fields=[
            FieldSpec(name="sin_number", type="id", pattern=uid_pattern,
                      validator="sin_luhn", locators=["regex"]),
            FieldSpec(name="application_number", type="id", locators=["regex"]),
        ],
    )
    doc = view(blocks=[TextBlock(text=f"SIN {GOOD_SIN}", page=1, bbox=q(100, 100, 400, 130))])
    context = LocatorContext.for_view(doc, spec=spec)

    claimed_field = spec.fields[0]
    borrower = spec.fields[1]
    assert regex_locator.locate(claimed_field, doc, context)[0].value == GOOD_SIN
    assert regex_locator.locate(borrower, doc, context) == []

    result = resolve(doc, default_schema_for(spec), spec=spec)
    by_name = {f.name: f for f in result.fields}
    assert by_name["sin_number"].value == GOOD_SIN
    assert by_name["application_number"].value is None


def test_regex_still_borrows_a_doctype_pattern_when_exactly_one_field_can_own_it():
    lone_pattern = r"\bEN\d{6}\b"
    spec = DocTypeSpec(
        doctype_id="some_form", label="Form", country="IN",
        id_patterns=[lone_pattern],
        fields=[FieldSpec(name="application_number", type="id", locators=["regex"])],
    )
    doc = view(blocks=[TextBlock(text="Ref EN123456 issued", page=1, bbox=q(0, 0, 400, 30))])
    context = LocatorContext.for_view(doc, spec=spec)
    assert regex_locator.locate(spec.fields[0], doc, context)[0].value == "EN123456"
