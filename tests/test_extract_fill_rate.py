"""Fill rate: what a field is allowed to be filled *with*, and what it must stay empty for.

The corpus measured a low extraction fill rate, and the first thing that had to be settled
was whether that was a defect at all. Most of the corpus is blank official forms, and a
blank form has no values in it — 0/11 on an empty GST certificate template is the right
answer, not a bug.

Opening the documents turned the finding inside out. The fill rate was not too low; it was
made of the wrong things. On a blank CRA T4 the service reported five of seven fields, and
all five were furniture: ``full_name`` was the French half of its own caption, ``address``
was another caption, and two amounts were the digits ``101`` and ``437`` sliced out of the
line numbers ``10100`` and ``43700`` printed in the form's instructions. Meanwhile the one
genuinely filled document in the set — a completed Ontario property assessment — reported
``roll_number`` as ``"and access key as noted below."`` while the roll number itself sat on
the page, and its ``$264,000`` assessed value came back as ``201``, carved out of the year
``2016``.

Both halves of that are the same defect, and it is the one this suite pins:

**A caption is not a value, and a fragment of a number is not a number.** Every test below
is a line taken from a real corpus document, reduced to the smallest layout that reproduces
what went wrong. The bindings they reject are not near misses — they are grammatical
English reported under a KYC field name, where nothing downstream can tell. An empty field
routes to a human; a wrong one is a compliance incident.

The tests are grouped by the evidence that decides each case:

1. What a span is *made of* — fill rule, punctuation, a truncated digit run.
2. Whether the label was printed *as a caption* — line position and caption marker.
3. Whether the caption *ended* where the label did — connectors, qualifiers, bilingual halves.
4. Reading order, which is all a plain-text view has, and the fences it needs to be safe.
5. The decision, in :mod:`dce.extract.resolve`, that a shaped field reports only its shape.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # keeps the suite runnable without an installed package
    sys.path.insert(0, str(_ROOT))

from dce.adapters import from_plain_text  # noqa: E402
from dce.extract.locators import LocatorContext, trim  # noqa: E402
from dce.extract.locators import label as label_locator  # noqa: E402
from dce.extract.locators.base import label_similarity  # noqa: E402
from dce.extract.resolve import resolve_field  # noqa: E402
from dce.models import DocTypeSpec, FieldSpec, LayoutView, PageInfo, TextBlock  # noqa: E402

PAGE_W, PAGE_H = 1000.0, 1400.0


def q(x0: float, y0: float, x1: float, y1: float) -> list[float]:
    """A clockwise-from-top-left quad for a rectangle."""
    return [x0, y0, x1, y0, x1, y1, x0, y1]


def view(**kwargs) -> LayoutView:
    kwargs.setdefault("pages", [PageInfo(page=1, width=PAGE_W, height=PAGE_H)])
    kwargs.setdefault("doc_id", "test-doc")
    return LayoutView(**kwargs)


def ctx(**kwargs) -> LocatorContext:
    return LocatorContext(**kwargs)


def one_line(text: str) -> LayoutView:
    """A single positioned block — the same-line bindings, with geometry present."""
    return view(blocks=[TextBlock(text=text, page=1, bbox=q(60, 200, 940, 220))])


def values(field: FieldSpec, doc: LayoutView, **kwargs) -> list[str]:
    return [c.value for c in label_locator.locate(field, doc, ctx(**kwargs))]


# ---------------------------------------------------------------------------
# 1. What a span is made of
# ---------------------------------------------------------------------------
def test_a_number_is_never_a_prefix_of_a_longer_number():
    """``101`` out of ``10100`` is the T4 defect, and it is the worst kind of wrong.

    The back of a CRA T4 prints ``14 -- Employment income -- Enter on line 10100.`` The
    amount pattern matches ``101`` inside that line number, the amount validator accepts it,
    and a blank form reports an employment income of 101 dollars at ``format_valid``.
    A shape match has to account for the whole digit run it sits in.
    """
    field = FieldSpec(
        name="employment_income", type="number",
        pattern=r"\$?\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?", validator="amount",
    )
    assert trim.tighten(field, "Enter on line 10100.") == ("Enter on line 10100.", False, "")
    assert not trim.has_type_shape(field, "Enter on line 10100.")

    # The same cut, one document over: "$264,000" was reported as "201", out of "2016".
    assert trim.tighten(field, "as of January 1, 2016")[0] != "201"
    assert trim.tighten(field, "Assessed value: $264,000")[0] == "$264,000"


def test_a_bounded_amount_is_still_found_inside_furniture():
    """The boundary rule must not cost a real amount its binding."""
    field = FieldSpec(name="rent", type="number", validator="amount")
    assert trim.tighten(field, "Rent payable 1,250.00 per month")[0] == "1,250.00"
    assert trim.tighten(field, "Total (Rs.) 45,000")[0] == "45,000"


def test_a_fill_rule_is_not_a_value():
    """``Verified today, the _____ day`` — a blank form's rule is where a value *would* be."""
    assert trim.strip_fill_rules("of the declarant ____________") == "of the declarant"
    assert not trim.has_substance("________________")
    assert not trim.has_substance("...........................")
    assert trim.has_substance("12 Long Road")

    field = FieldSpec(
        name="declaration_date", type="date", labels={"en": ["Date"]},
        validator="generic_date", locators=["label"],
    )
    assert values(field, one_line("Date  : ________________")) == []


def test_a_span_with_no_alphanumerics_at_all_is_not_a_value():
    """Form 16 reported ``certificate_number`` as ``"."``."""
    field = FieldSpec(
        name="certificate_number", labels={"en": ["Certificate No"]}, locators=["label"],
    )
    assert values(field, one_line("Certificate No.  .")) == []


# ---------------------------------------------------------------------------
# 2. Was the label printed as a caption?
# ---------------------------------------------------------------------------
def test_a_label_matched_inside_a_sentence_is_not_a_caption():
    """``Please have your roll number available when you contact us.``

    The words are there; the document is not using them as a caption. Binding this reported
    a property's roll number as ``"available when you contact us."``.
    """
    field = FieldSpec(
        name="roll_number", type="id", labels={"en": ["Roll Number"]}, locators=["label"],
    )
    doc = one_line("Please have your roll number available when you contact us.")
    assert values(field, doc) == []


def test_a_mid_line_caption_still_binds_when_the_form_marks_it():
    """The two conventions that *do* mark a caption, both mid-line.

    A colon, and the column gap a form sets between a caption and its value. Losing either
    would cost the multi-field form line the whole trimming module exists for.
    """
    date = FieldSpec(
        name="signature_date", type="date", labels={"en": ["Date"]},
        validator="generic_date", locators=["label"],
    )
    colon = one_line("Signature of U.S. person: J. Smith  Date: 2026-03-14")
    assert "2026-03-14" in values(date, colon)

    name = FieldSpec(
        name="father_name", type="name", labels={"en": ["Father's Name"]},
        validator="name", locators=["label"],
    )
    gap = one_line("Name  ANNA ERIKSSON   Father's Name  BO ERIKSSON")
    assert values(name, gap)[0] == "BO ERIKSSON"


def test_a_caption_that_opens_its_line_needs_no_marker():
    field = FieldSpec(
        name="date_of_birth", type="date", labels={"en": ["Date of Birth"]},
        validator="generic_date", locators=["label"],
    )
    assert values(field, one_line("Date of Birth 01/02/1990"))[0] == "01/02/1990"
    assert values(field, one_line("3. Date of Birth 01/02/1990"))[0] == "01/02/1990"


# ---------------------------------------------------------------------------
# 3. Did the caption end where the label did?
# ---------------------------------------------------------------------------
def test_a_label_inside_a_longer_caption_does_not_bind_what_follows_it():
    """``1. Full name and address of the declarant ____`` — Form 60.

    ``address`` is printed there, but as part of a longer noun phrase. The caption has not
    ended, so what follows the match is the rest of the caption: the field was reported as
    ``"of the declarant ____"``.
    """
    field = FieldSpec(
        name="address", type="address", labels={"en": ["Address"]}, validator="address",
        locators=["label"],
    )
    doc = one_line("1.  Full name and address of the declarant ____________________")
    assert values(field, doc) == []


def test_a_label_that_continues_into_a_connector_does_not_bind():
    """``Year of construction:  1999`` on a property assessment is not the tax year."""
    field = FieldSpec(
        name="tax_year", type="number", pattern=r"\b(19|20)\d{2}\b",
        labels={"en": ["Year"]}, locators=["label"],
    )
    assert values(field, one_line("Year of construction:  1999")) == []
    # The caption marker is what decides it: with the colon straight after the label, the
    # caption has closed and the same label binds the value that follows.
    assert values(field, one_line("Year: 2026"))[0] == "2026"


def test_a_value_that_opens_with_a_connector_still_binds_after_a_colon():
    """The colon closes the caption, so ``El Paso`` and ``de Souza`` keep their bindings."""
    city = FieldSpec(
        name="city", labels={"en": ["City"]}, locators=["label"],
    )
    assert values(city, one_line("City: El Paso"))[0] == "El Paso"
    name = FieldSpec(
        name="full_name", type="name", labels={"en": ["Name"]}, validator="name",
        locators=["label"],
    )
    assert values(name, one_line("Name: de Souza, Joao"))[0] == "de Souza, Joao"


def test_a_caption_qualified_by_a_comma_is_still_a_caption():
    """``Trade Name, if any`` on a GST certificate reported a trade name of ``"if any"``."""
    field = FieldSpec(
        name="trade_name", labels={"en": ["Trade Name"]}, locators=["label"],
    )
    assert values(field, one_line("Trade Name, if any")) == []


def test_a_bilingual_caption_with_a_clarifier_is_not_a_surname():
    """The CRA T4 line, verbatim.

    Anchoring ``full_name`` on ``Last name`` leaves ``Nom de famille (en lettres moulées)``.
    The parenthetical pushes the caption under the coverage floor that would otherwise have
    caught it, so the form's own French caption was reported as the employee's surname.
    """
    field = FieldSpec(
        name="full_name", type="name", validator="name", locators=["label"],
        labels={"en": ["Last Name", "Name"], "fr": ["Nom de famille", "Nom"]},
    )
    doc = one_line("Last name (in capital letters) \u2013 Nom de famille (en lettres moulées)")
    assert values(field, doc) == []


def test_a_value_never_ends_on_a_dangling_connector():
    """``Summary of amount paid/credited and`` is half a phrase, not a period of employment."""
    field = FieldSpec(
        name="employment_period", labels={"en": ["Period"]}, locators=["label"],
    )
    doc = one_line("Period: Summary of amount paid/credited and")
    assert values(field, doc) == []


# ---------------------------------------------------------------------------
# 4. Reading order — the only adjacency a plain-text view has
# ---------------------------------------------------------------------------
def stacked(*lines: str) -> LayoutView:
    """A plain-text view: one block per line, and **no geometry at all**.

    This is what :func:`dce.adapters.from_plain_text` produces for a caller with no layout
    provider, and it is the payload the corpus harness sends. Every geometric binding in the
    label locator is dead on it.
    """
    return from_plain_text("\n".join(lines))


def test_a_stacked_form_extracts_nothing_without_a_reading_order_binding():
    """The reproduction: geometry is what the plain-text path does not have."""
    doc = stacked("Roll number:", "12 34 567 899 94004 0000")
    assert all(block.bbox is None for block in doc.blocks)


def test_a_caption_binds_the_line_printed_under_it():
    """Ontario property assessment: the roll number the service used to miss entirely."""
    field = FieldSpec(
        name="roll_number", type="id", labels={"en": ["Roll Number"]}, locators=["label"],
    )
    doc = stacked(
        "To register, enter in your roll number and access key as noted below.",
        "Roll number:",
        "12 34 567 899 94004 0000",
        "Access key:",
    )
    best = label_locator.locate(field, doc, ctx())[0]
    assert best.value == "12 34 567 899 94004 0000"
    assert "next line" in best.detail


def test_a_caption_line_that_is_a_whole_sentence_still_binds_below_it():
    """``Your property's assessed value as of January 1, 2016 is:`` then the amount.

    Split on the same line this used to yield ``201``, out of ``2016``. The line ends in a
    colon, so it is all caption and the value is underneath.
    """
    field = FieldSpec(
        name="assessed_value", type="number", validator="amount",
        pattern=r"\$?\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?",
        labels={"en": ["Assessed Value"]}, locators=["label"],
    )
    doc = stacked(
        "Your property\u2019s assessed value as of January 1, 2016 is:",
        "$264,000",
    )
    assert label_locator.locate(field, doc, ctx())[0].value == "$264,000"


def test_the_reading_order_binding_refuses_a_field_with_no_shape():
    """The fence that matters, and the one every caption-as-a-value came through.

    Reading order carries nothing spatial, so the value's own shape is the only evidence
    left that the binding was right. A ``name`` has no shape — and on a blank form the line
    after a caption is the *next caption*: ``Last name``, ``Street address``, ``(a) Full
    name``. Every one of those was reported as somebody's name until this fence existed.
    """
    named = FieldSpec(
        name="full_name", type="name", labels={"en": ["Name"]}, validator="name",
        locators=["label"],
    )
    assert label_locator.locate(named, stacked("Name", "Last name"), ctx()) == []

    # The same layout, for a field whose value can be checked: bound, because it can be.
    dated = FieldSpec(
        name="issue_date", type="date", labels={"en": ["Date of Issue"]},
        validator="generic_date", locators=["label"],
    )
    doc = stacked("Some preceding sentence about the document.", "Date of Issue", "27/04/1956")
    assert label_locator.locate(dated, doc, ctx())[0].value == "27/04/1956"


def test_the_reading_order_binding_refuses_a_run_of_captions():
    """``RFC`` / ``Folio`` / value / value — a column header, read down instead of across.

    On the Mexican *opinión de cumplimiento* this bound ``folio`` to the line after it,
    which is the **RFC**: a well-formed identifier, of the wrong field, with nothing to
    show anything went wrong. Nothing in a geometry-free view can pair the captions with
    their columns.
    """
    field = FieldSpec(
        name="folio", type="id", labels={"en": ["Folio"]}, locators=["label"],
    )
    spec = DocTypeSpec(
        doctype_id="mx_opinion", label="Opinión", country="MX",
        fields=[field, FieldSpec(name="rfc", type="id", labels={"en": ["RFC"]})],
    )
    doc = stacked("RFC", "Folio", "UQR9105241R5", "26NF0657372")
    assert label_locator.locate(field, doc, ctx(spec=spec)) == []


def test_the_reading_order_binding_refuses_the_forms_own_numbering():
    """A blank GST certificate reads ``1.`` / ``Legal Name`` / ``2.`` / ``Trade Name``."""
    field = FieldSpec(
        name="registration_type", type="id", labels={"en": ["Type of Registration"]},
        locators=["label"],
    )
    assert label_locator.locate(field, stacked("Type of Registration", "8."), ctx()) == []

    numbered = FieldSpec(
        name="consumer_number", type="id", labels={"en": ["Consumer Number"]},
        locators=["label"],
    )
    doc = stacked("Consumer Number", "2. GHS shall bill Energy charges to its members")
    assert label_locator.locate(numbered, doc, ctx()) == []


def test_the_reading_order_binding_refuses_a_wrapped_phrase():
    """``Location`` / ``Quality of`` — the next line stops on a connector, so it is half of one."""
    field = FieldSpec(
        name="roll_number", type="id", labels={"en": ["Location"]}, locators=["label"],
    )
    assert label_locator.locate(field, stacked("Location", "Quality of"), ctx()) == []


def test_the_reading_order_binding_refuses_an_approximated_caption():
    """``…domestic consumers i.e. FY 2019-20 as follows:`` is not a ``Consumer ID`` caption.

    It ends in a colon, which is all :func:`trim.reads_as_caption` needs, and ``Consumer ID``
    fuzzy-matches ``consumers i.e.`` at 91. A binding with no geometry behind it does not
    also get to guess at the caption.
    """
    field = FieldSpec(
        name="consumer_number", type="id", labels={"en": ["Consumer ID"]}, locators=["label"],
    )
    doc = stacked(
        "for consumption in respective slab of domestic consumers i.e. FY 2019-20 as follows:",
        "2000",
    )
    assert label_locator.locate(field, doc, ctx()) == []


def test_a_stacked_caption_binds_only_one_line_not_a_page():
    """No geometry means no column and no line height, so no continuation walk.

    With one, an ``address`` bound under a caption absorbed every following line until the
    next caption — on the property assessment that was eleven lines of prose about how MPAC
    assesses property.
    """
    field = FieldSpec(
        name="address", type="address", labels={"en": ["Address"]}, validator="address",
        pattern=r"\d", locators=["label"],
    )
    doc = stacked("Address", "12 Long Road", "Bengaluru 560001", "and a further line")
    located = label_locator.locate(field, doc, ctx())
    assert [c.value for c in located] == ["12 Long Road"]


# ---------------------------------------------------------------------------
# 5. Label matching
# ---------------------------------------------------------------------------
def test_a_label_never_matches_a_text_too_short_to_contain_it():
    """``partial_ratio`` aligns the shorter string inside the longer one either way round.

    So ``Trade Name`` scored 100 against a block reading only ``Name``, and on a blank GST
    certificate ``trade_name`` bound to whatever followed the *legal* name's caption.
    """
    assert label_similarity("Trade Name", "Name") < 88
    assert label_similarity("Name", "Trade Name") >= 88
    assert label_similarity("Date of Birth", "Date of Birth / Fecha de nacimiento") >= 88


def test_a_two_letter_label_does_not_claim_every_line_using_the_word():
    """``To`` and ``AY`` are declared labels on real forms.

    Whole-token containment handed ``To`` every sentence starting with it — Form 16's
    ``employment_period`` came back as ``"update PAN details in Income Tax Department
    database…"``.
    """
    assert label_similarity("To", "To update PAN details, apply for a change request") < 88
    assert label_similarity("To", "To") == 100.0
    # Three characters is enough to be a name for something, in any script.
    assert label_similarity("नाम", "नाम / Name") >= 88


# ---------------------------------------------------------------------------
# 6. The decision: a shaped field reports its shape or nothing
# ---------------------------------------------------------------------------
def test_a_shaped_field_is_left_empty_rather_than_filled_with_prose():
    """Resolution reports the best *rejected* value so a reviewer sees what was on the page.

    That is right for a value of the field's kind and wrong for a sentence: reported under
    ``filing_date``, an ITR-V instruction paragraph reads as data to everything downstream.
    A shaped field with no instance of its shape anywhere is empty, and says why.
    """
    field = FieldSpec(
        name="filing_date", type="date", labels={"en": ["date of furnishing"]},
        validator="generic_date", locators=["label"],
    )
    doc = one_line(
        "the date of furnishing: the return of income and all consequences shall follow"
    )
    extracted = resolve_field(field, doc, ctx())[0]
    assert extracted.value in (None, "")
    assert extracted.validator_error == "no_candidate_of_this_type"


def test_an_identifier_that_fails_its_checksum_still_reaches_the_reviewer():
    """The line the filter above must not cross.

    A SIN whose Luhn digit fails *has* the shape. It is the identifier printed on the
    document, the reviewer needs to see exactly it, and dropping it would hide a real
    finding behind an empty field.
    """
    field = FieldSpec(
        name="sin_number", type="id", labels={"en": ["SIN"]},
        validator="sin_luhn", locators=["label"],
    )
    extracted = resolve_field(field, one_line("SIN: 193 000 000"), ctx())[0]
    assert extracted.value == "193 000 000"
    assert extracted.verification == "unverified"
    assert extracted.validator_error


def test_a_bare_id_field_needs_a_digit_before_it_is_an_identifier():
    """``Content`` and ``Marriages`` satisfied ``[0-9A-Z]+`` on their leading capital."""
    field = FieldSpec(name="licence_number", type="id")
    assert not trim.has_type_shape(field, "Content")
    assert not trim.has_type_shape(field, "Marriages")
    assert trim.has_type_shape(field, "DL-1420110012345")


# ---------------------------------------------------------------------------
# 7. A blank form stays blank
# ---------------------------------------------------------------------------
def test_a_blank_form_fills_nothing_at_all():
    """The Form W-9 (Rev. March 2024) template, as the corpus ships it.

    Zero filled is the correct answer here: there is nothing printed to extract. The failure
    this pins is a locator that binds a form's *own furniture* — a line number, the word
    ``Signature``, a section heading — as if it were somebody's answer.
    """
    fields = [
        FieldSpec(name="ein", type="id", labels={"en": ["Employer identification number"]},
                  validator="ein", locators=["label"]),
        FieldSpec(name="legal_name", type="name", labels={"en": ["Name of entity/individual"]},
                  validator="name", locators=["label"]),
        FieldSpec(
            name="business_name",
            labels={"en": ["Business name/disregarded entity name, if different from above."]},
            locators=["label"],
        ),
        FieldSpec(name="tax_classification",
                  labels={"en": ["federal tax classification"]}, locators=["label"]),
    ]
    spec = DocTypeSpec(doctype_id="us_w9", label="Form W-9", country="US", fields=fields)
    doc = stacked(
        "Request for Taxpayer Identification Number and Certification",
        "1",
        "Name of entity/individual. An entry is required.",
        "2",
        "Business name/disregarded entity name, if different from above.",
        "3a",
        "Check the appropriate box for federal tax classification of the entity/individual",
        "Employer identification number",
        "Sign Here",
        "Signature of U.S. person",
        "Date",
    )
    for field in fields:
        extracted = resolve_field(field, doc, ctx(spec=spec))[0]
        assert not (extracted.value or "").strip(), (
            f"{field.name} filled with {extracted.value!r} on a blank form"
        )
