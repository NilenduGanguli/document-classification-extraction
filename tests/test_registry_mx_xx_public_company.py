"""The public-company / due-diligence additions: MX listed-issuer filings and the xx_* globals.

Three properties are pinned here, and the middle one is the reason the file exists.

**A generic must never outrank a specific doctype on the specific one's own document.**
:mod:`dce.registry.crosscountry` promises it, ``_check_generic_not_greedy`` enforces the
*data* half of it (no shared anchor strings), and ``tests/test_registry_generic_specificity``
asserts that data property. None of that is a measurement: a generic can still out-score a
country pack by matching more of its own vocabulary than the pack matches of its own, which
is exactly how ``xx_bank_statement`` beat ``us_bank_statement`` on a Bank of America
statement while every data invariant held. This round adds ten cross-jurisdiction doctypes,
several of which sit directly on top of a statutory country document — ``xx_ubo_declaration``
against ``us_fincen_boir`` above all — so the property is measured here by running the
classifier, not argued from the registry.

**The Mexican listed-issuer filings separate from each other.** ``mx_reporte_anual_cnbv``
and ``mx_reporte_trimestral_bmv`` come out of the same exchange XBRL template and share
their whole page header, so if they were going to collide anywhere it is here.

**The new decisive anchors are decisive.** One doctype in the whole registry claims each of
them, which is what the word means.

Views are built by placing every anchor in the zone the pack declared for it. That is not
incidental: an earlier harness hardcoded ``zone=body`` while 27 decisive anchors were
title-gated, so those anchors were unreachable in every number the harness ever reported.
A test that cannot see the evidence it is testing measures nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dce.adapters import from_plain_text
from dce.classify import anchor_scores, classify
from dce.models import DocTypeSpec, LayoutView, PageInfo, TextBlock, Zone
from dce.registry import all_specs, crosscountry, get, mexico

SPECS = all_specs()
NEW_XX = (
    "xx_lei_certificate",
    "xx_fatca_crs_self_certification",
    "xx_wolfsberg_questionnaire",
    "xx_isda_master_agreement",
    "xx_sanctions_screening_report",
    "xx_ubo_declaration",
    "xx_audited_financial_statements",
    "xx_certificate_of_insurance",
    "xx_iso_certificate",
    "xx_duns_record",
)
NEW_MX = (
    "mx_acta_asamblea",
    "mx_informe_comisario",
    "mx_imss_alta_patronal",
    "mx_reporte_anual_cnbv",
    "mx_reporte_trimestral_bmv",
    "mx_prospecto_colocacion",
    "mx_aviso_privacidad",
)

#: ``(specific, generic)``. Every specific here predates this round, so the pairing does not
#: depend on any other pack landing — except ``mx_reporte_anual_cnbv``, which is added by the
#: same change as the generic it is paired against.
#:
#: Three pairs were dropped when the India pack was removed from the registry:
#: ``in_ckyc_record``/``xx_sanctions_screening_report``,
#: ``in_certificate_incorporation``/``xx_lei_certificate`` and
#: ``in_gst_certificate``/``xx_iso_certificate``. A pair whose specific half no longer
#: exists cannot be measured, and substituting an unrelated doctype to keep the row would
#: be a regression test for a regression nobody observed. The LEI rivalry is kept under a
#: specific that does exist: ``us_articles_incorporation`` is what an LEI record must never
#: be mistaken for, and ``xx_lei_certificate`` declares it in ``confusable_with``.
#: ``xx_sanctions_screening_report`` and ``xx_iso_certificate`` now have no country-pack
#: rival in this registry at all; they are still covered by
#: ``test_each_new_generic_still_wins_its_own_document`` below.
RIVALS = (
    ("us_fincen_boir", "xx_ubo_declaration"),
    ("us_w8bene", "xx_fatca_crs_self_certification"),
    ("us_certificate_good_standing", "xx_duns_record"),
    ("us_articles_incorporation", "xx_lei_certificate"),
    ("mx_reporte_anual_cnbv", "xx_audited_financial_statements"),
    ("us_operating_agreement", "xx_isda_master_agreement"),
    # The original inversion, kept as a standing regression.
    ("us_bank_statement", "xx_bank_statement"),
)


def _view(blocks: list[tuple[str, Zone]], doc_id: str = "t") -> LayoutView:
    return LayoutView(
        doc_id=doc_id,
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")],
        blocks=[TextBlock(text=text, zone=zone, page=1) for text, zone in blocks],
    )


def _view_of(spec: DocTypeSpec) -> LayoutView:
    """A document made of exactly what ``spec`` says its document says.

    Each anchor goes into the zone the pack gated it to, so a title-gated anchor is audible
    in the title and a plain one in the body. Field labels are added as body text because a
    real document prints its own labels, and leaving them out would make every view thinner
    than any document the service will ever see.
    """
    blocks = [(a.text, a.zone or Zone.body) for a in spec.anchors]
    blocks += [
        (label, Zone.body)
        for field in spec.fields
        for labels in field.labels.values()
        for label in labels
    ]
    return _view(blocks, doc_id=spec.doctype_id)


# ---------------------------------------------------------------------------
# Guard the guard
# ---------------------------------------------------------------------------
def test_the_new_doctypes_are_actually_registered() -> None:
    """These tests are vacuous if the packs did not load or an id was renamed."""
    for doctype_id in NEW_XX + NEW_MX:
        assert get(doctype_id) is not None, f"{doctype_id} is not registered"
    for specific, generic in RIVALS:
        assert get(specific) is not None, f"rival fixture is stale: {specific}"
        assert get(generic) is not None, f"rival fixture is stale: {generic}"


def test_the_anchor_tier_can_hear_every_new_anchor() -> None:
    """Defect 3: verify the instrument before trusting it.

    A view built from a spec's own anchors must score that spec above zero on the anchor
    tier. If it does not, the view is not reaching the evidence and every other assertion in
    this file is measuring nothing.
    """
    for doctype_id in NEW_XX + NEW_MX:
        spec = get(doctype_id)
        assert spec is not None
        scored = anchor_scores(_view_of(spec), SPECS)
        assert scored.scores.get(doctype_id, 0.0) > 0.0, (
            f"{doctype_id} scores zero on a document made of its own anchors — the view "
            "cannot see its evidence, most likely a zone gate"
        )


# ---------------------------------------------------------------------------
# The property this file exists for
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("specific", "generic"), RIVALS)
def test_a_generic_never_outranks_the_specific_doctype(specific: str, generic: str) -> None:
    """On the country doctype's own document, the generic must lose on both channels.

    Checked at the anchor tier *and* end to end. The anchor tier is where the count-based
    inversion lives, and the end-to-end answer is what a caller actually receives — a
    registry that ranks correctly and then returns the wrong doctype has not fixed anything.
    """
    spec = get(specific)
    assert spec is not None
    view = _view_of(spec)

    scored = anchor_scores(view, SPECS)
    assert scored.scores.get(specific, 0.0) > scored.scores.get(generic, 0.0), (
        f"{generic} matches at least as much of its own vocabulary as {specific} does of "
        f"its own, on a {specific} document — the greedy-generic inversion has returned"
    )

    result = classify(view)
    assert result.doctype_id != generic, (
        f"a {specific} document was classified as the generic {generic}"
    )
    for runner, _score in result.runners_up:
        if runner == generic:
            assert result.doctype_id == specific, (
                f"{generic} is a live runner-up on a {specific} document and {specific} did "
                f"not win it — got {result.doctype_id!r}"
            )


@pytest.mark.parametrize("doctype_id", NEW_XX)
def test_each_new_generic_still_wins_its_own_document(doctype_id: str) -> None:
    """A doctype that can never win is dead weight that only adds noise to the softmax.

    The mirror of the test above: having established that these lose to a country pack on
    the pack's document, they have to win on their own, or adding them made the registry
    worse for nothing.
    """
    spec = get(doctype_id)
    assert spec is not None
    scored = anchor_scores(_view_of(spec), SPECS)
    ranked = sorted(scored.scores.items(), key=lambda kv: (-kv[1], kv[0]))
    assert ranked[0][0] == doctype_id, (
        f"{doctype_id} does not lead the anchor tier on its own document; {ranked[:3]}"
    )


# ---------------------------------------------------------------------------
# Mexican listed-issuer filings
# ---------------------------------------------------------------------------
#: Page furniture the BMV's XBRL template stamps on every page of both filings. Declared
#: once here so each fixture below has to be separated by the taxonomy headings alone,
#: which is the only thing that actually distinguishes them.
_BMV_HEADER = [
    ("Bolsa Mexicana de Valores S.A.B. de C.V.", Zone.furniture),
    ("Clave de Cotización: SPORT", Zone.furniture),
]


def test_the_anexo_n_and_the_quarterly_do_not_collide() -> None:
    """Same template, same header, same ticker — separated only by their section roles."""
    annual = _view(
        [
            *_BMV_HEADER,
            ("[411000-AR] Datos generales - Reporte Anual", Zone.title),
            ("[412000-N] Portada reporte anual", Zone.heading),
            ("[417000-N] La emisora", Zone.heading),
            ("[424000-N] Información financiera", Zone.heading),
            ("[427000-N] Administración", Zone.heading),
            ("Registro Nacional de Valores", Zone.body),
            ("Comisión Nacional Bancaria y de Valores", Zone.body),
        ],
        doc_id="annual",
    )
    quarterly = _view(
        [
            *_BMV_HEADER,
            ("Información Financiera Trimestral", Zone.title),
            ("Trimestre: 1 Año: 2025", Zone.furniture),
            ("Consolidado", Zone.furniture),
            ("Cantidades monetarias expresadas en Unidades", Zone.furniture),
            ("[105000] Comentarios y Análisis de la Administración", Zone.heading),
            (
                "[813000] Notas - Información financiera intermedia de conformidad con la NIC 34",
                Zone.heading,
            ),
        ],
        doc_id="quarterly",
    )

    annual_scores = anchor_scores(annual, SPECS).scores
    quarterly_scores = anchor_scores(quarterly, SPECS).scores

    assert annual_scores["mx_reporte_anual_cnbv"] > annual_scores.get(
        "mx_reporte_trimestral_bmv", 0.0
    )
    assert quarterly_scores["mx_reporte_trimestral_bmv"] > quarterly_scores.get(
        "mx_reporte_anual_cnbv", 0.0
    )
    assert classify(annual).doctype_id in {"mx_reporte_anual_cnbv", "unknown"}
    assert classify(quarterly).doctype_id in {"mx_reporte_trimestral_bmv", "unknown"}


def test_the_prospectus_legend_is_not_the_annual_reports_legend() -> None:
    """Both filings carry the RNV non-certification legend; only one carries the offering one.

    The shared prefix is declared non-decisively on both precisely so that it cannot decide
    between them, and this is the assertion that the offering legend still can.
    """
    prospectus = _view(
        [
            ("PROSPECTO DEFINITIVO", Zone.title),
            (
                "no podrán ser ofrecidos ni vendidos fuera de los Estados Unidos Mexicanos, "
                "a menos que sea permitido por las leyes de otros países",
                Zone.body,
            ),
            (
                "La inscripción en el Registro Nacional de Valores no implica certificación "
                "sobre la bondad de los valores",
                Zone.body,
            ),
            ("Intermediario Colocador", Zone.body),
            ("Oferta Pública", Zone.body),
        ],
        doc_id="prospectus",
    )
    scores = anchor_scores(prospectus, SPECS).scores
    assert scores["mx_prospecto_colocacion"] > scores.get("mx_reporte_anual_cnbv", 0.0)
    assert classify(prospectus).doctype_id in {"mx_prospecto_colocacion", "unknown"}


# ---------------------------------------------------------------------------
# Registry-level invariants over the additions
# ---------------------------------------------------------------------------
def test_no_new_generic_carries_a_decisive_anchor() -> None:
    """The loader forbids it; assert it here too so a regression names this round."""
    for doctype_id in NEW_XX:
        spec = get(doctype_id)
        assert spec is not None
        assert not any(a.decisive for a in spec.anchors), f"{doctype_id} has a decisive anchor"


def test_every_new_decisive_anchor_is_claimed_by_exactly_one_doctype() -> None:
    """A decisive anchor asserts the string appears on one document type and nowhere else.

    ``_check_decisive_asymmetry`` already refuses an *undeclared* overlap. This is the
    stronger statement for the anchors added this round: there is no overlap at all, so
    none of them relies on a confusable_with declaration to be safe.
    """
    claims: dict[str, set[str]] = {}
    for spec in SPECS:
        for anchor in spec.anchors:
            claims.setdefault(anchor.text.casefold(), set()).add(spec.doctype_id)

    for doctype_id in NEW_MX:
        spec = get(doctype_id)
        assert spec is not None
        for anchor in spec.anchors:
            if not anchor.decisive:
                continue
            owners = claims[anchor.text.casefold()]
            assert owners == {doctype_id}, (
                f"{doctype_id} calls {anchor.text!r} decisive but {sorted(owners)} claim it"
            )


def test_adding_these_doctypes_changes_no_existing_verdict() -> None:
    """Defect 1, in the form the synthetic scale-invariance test cannot reach.

    ``tests/test_registry_scale_invariance`` pads the registry with doctypes built from a
    private syllable alphabet, so their vocabulary provably cannot touch the document under
    test. That isolates the acceptance rule and proves it is size-independent — but it
    cannot see the other channel through which registry size leaks into a verdict, because
    it deliberately removes it.

    That channel is the lexical idf. ``build_profiles`` derives document frequency from the
    whole registry, so a new doctype that keeps a *shared* term in its profile lowers that
    term's idf for every doctype already relying on it. A real doctype does keep shared
    terms; a syllable-alphabet filler by construction does not.

    It is not hypothetical. ``xx_certificate_of_insurance``, first drafted with ACORD's full
    twenty-word legend as an anchor and a bare "Date" label, took
    ``corpus/ca/ca_cra_noa.pdf`` from CORRECT to an abstention — fifteen ordinary English
    terms entered the profile vocabulary, ``ca_cra_noa`` and a foreign tax acknowledgement
    (in the India pack, since removed) swapped places in the lexical channel, the two
    channels stopped agreeing and the cascade declined. The spec never scored on that
    document and was never a candidate on it. It degraded it purely by existing.

    This test replays that measurement: classify the whole reference corpus against the
    registry with these doctypes and without them, and require that no document was
    **degraded**.

    Two things it is careful to get right, because an earlier form of it got both wrong and
    was red for a benign reason — worse than no test, since a red guard nobody trusts is a
    guard nobody reads:

    *Only documents these doctypes are not FOR.* A specimen of ``xx_ubo_declaration`` going
    ``unknown -> xx_ubo_declaration`` when that doctype is added is the feature, not the leak.
    Counting it as a changed verdict made the test fail on its own success. The subject is the
    documents that are *not* one of the new types: those must not move because a stranger
    joined the registry.

    *Degradation, not change.* The measured incident was a CORRECT answer becoming an
    abstention. An abstention becoming CORRECT is the same mechanism pushing the other way and
    is not a defect — asserting bit-identical verdicts fails on an improvement, and invites
    someone to "fix" it by making the service worse. So the assertion is one-sided: nothing
    that was right may stop being right, and nothing may newly become wrong. Correctness is
    judged against the manifest's ``expected_doctype``, not against the previous answer, which
    may itself have been wrong.

    It is a local guard, not a CI gate — ``corpus/`` is not checked in, so this skips where
    the corpus is absent. That is stated rather than hidden: a test that silently passes
    when its evidence is missing is worse than no test.
    """
    fitz = pytest.importorskip("fitz", reason="PyMuPDF is needed to read the corpus")
    corpus = Path(__file__).resolve().parents[1] / "corpus"
    pdfs = sorted(corpus.glob("*/*.pdf"))
    if len(pdfs) < 20:
        pytest.skip(f"reference corpus not present at {corpus} (found {len(pdfs)} pdfs)")

    added = set(NEW_XX) | set(NEW_MX)
    without = [s for s in SPECS if s.doctype_id not in added]

    expected: dict[str, str] = {}
    for manifest in corpus.glob("*/manifest.jsonl"):
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                expected[Path(row["file"]).name] = row.get("expected_doctype", "")

    views = []
    for pdf in pdfs:
        # A specimen OF a new doctype is not evidence about what adding it does to others.
        if expected.get(pdf.name) in added:
            continue
        try:
            doc = fitz.open(pdf)
            text = "\n".join(page.get_text() for page in doc)
        except Exception:  # noqa: BLE001 - a corrupt corpus file is not this test's subject
            continue
        if len(text.strip()) < 40:  # no text layer; the harness would skip it too
            continue
        views.append((pdf.name, from_plain_text(text)))

    assert len(views) >= 20, "too few readable corpus documents to make this a measurement"

    degraded = []
    for name, view in views:
        want = expected.get(name, "")
        before, after = classify(view, without), classify(view, SPECS)
        was_right = not before.abstained and before.doctype_id == want
        is_right = not after.abstained and after.doctype_id == want
        was_wrong = not before.abstained and before.doctype_id != want
        now_wrong = not after.abstained and after.doctype_id != want
        if (was_right and not is_right) or (now_wrong and not was_wrong):
            degraded.append(
                (
                    name,
                    "abstained" if before.abstained else before.doctype_id,
                    "abstained" if after.abstained else after.doctype_id,
                    want,
                )
            )

    assert not degraded, (
        "adding this round's doctypes degraded a document none of them are for — the lexical "
        "idf is registry-size dependent again:\n  "
        + "\n  ".join(
            f"{n}: {before} -> {after} (expected {want})" for n, before, after, want in degraded
        )
    )


def test_the_acta_de_asamblea_deliberately_has_no_decisive_anchor() -> None:
    """Minutes are written by the company, so no string on them is issuer-controlled.

    Pinned as a test rather than left as a comment because the tempting change — promoting
    "ASAMBLEA GENERAL ORDINARIA DE ACCIONISTAS" to decisive to lift its recall — is exactly
    the document-class claim that produces confident cross-issuer wrong answers.
    """
    spec = get("mx_acta_asamblea")
    assert spec is not None
    assert not any(a.decisive for a in spec.anchors)
    assert spec.anchors, "it still needs anchors; it just must not claim any of them proves it"


def test_the_new_packs_declare_the_attribute_keys_they_use() -> None:
    """Both files contribute their own keys, so either can be imported alone."""
    from dce.registry import loader

    for module in (mexico, crosscountry):
        for spec in module.SPECS:
            for field in spec.fields:
                if field.attribute_key:
                    assert field.attribute_key in loader.ATTRIBUTE_KEYS, (
                        f"{spec.doctype_id}.{field.name} -> {field.attribute_key}"
                    )


def test_personal_names_on_the_new_doctypes_are_flagged_pii() -> None:
    """Officers, signatories and beneficial owners are identified individuals.

    A corporate filing is public; the people it names are not thereby waived. Entity-name
    fields are exempt — a company is not a person.
    """
    entity_keys = {"entity.legal_name", "entity.trade_name", "entity.auditor"}
    person_keys = ("ownership.", "identity.", "entity.statutory_examiner")
    for doctype_id in NEW_XX + NEW_MX:
        spec = get(doctype_id)
        assert spec is not None
        for field in spec.fields:
            if field.type != "name" or field.attribute_key in entity_keys:
                continue
            if field.attribute_key.startswith(person_keys):
                assert field.pii, f"{doctype_id}.{field.name} names a person and is not pii"
