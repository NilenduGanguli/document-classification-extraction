"""The regression guard for DEFECT 1: a doctype's verdict must not depend on registry size.

This is the test that would have caught the defect on day one, and it is deliberately written
so that it cannot pass by construction.

**The defect.** Acceptance used to require ``softmax(fused, T)[top] >= 0.65``. Every doctype in
the registry contributes a strictly positive term to that softmax denominator, whether or not
it has anything to do with the document, so ``p_top`` fell monotonically as the registry grew —
for every document. The same US W-9, with identical evidence, scored 0.900 against 25 doctypes
and 0.411 against 121: accept, then abstain, with nothing about the document having changed.
Every country pack shipped degraded every doctype already installed.

**The experiment.** Classify a document against a plausible core, then against that core plus a
large number of doctypes that are *provably irrelevant to this document* — anchor score exactly
0.0 and explained mass exactly 0.0 — and require the classification to be bit-identical.

Two things about that design are load bearing:

*Irrelevance is a property of* ``(document, spec)``, never of the acceptance rule's own
admission test. A padding test whose padding set is defined as "whatever the rule excludes"
cannot fail, and one earlier proposal's invariance evidence was circular in exactly that way.

*Padding, not subsetting.* Random registry subsets also remove **real** competitors, and a rule
that ignored a real competitor would be broken rather than invariant — ``mx_cif`` genuinely
does compete with ``mx_rfc_csf``. Only the irrelevant additions are the defect.

Measured with this harness over the 59-document reference corpus, padding each document with
every doctype provably irrelevant to it (median 87, max 116 of them): the old rule changed its
verdict on 4 of 59 documents with a mean confidence swing of 0.091 and a maximum of 0.618; the
current rule changes 0 of 59, with a confidence and margin swing of exactly 0.000000.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from dce.classify import anchor_scores, build_profiles, classify, lexical_scores  # noqa: E402
from dce.config import Settings  # noqa: E402
from dce.models import (  # noqa: E402
    Anchor,
    Category,
    DocTypeSpec,
    FieldSpec,
    LayoutView,
    PageInfo,
    TextBlock,
    Zone,
)

SETTINGS = Settings(_env_file=None)

#: How many irrelevant doctypes to bury the real ones under. Comfortably more than the whole
#: production registry, because "one more country pack" is the thing that must stay free.
PADDING = 120


# ---------------------------------------------------------------------------
# Fixtures: a real decision, and a mountain of noise to bury it under
# ---------------------------------------------------------------------------
def _view(blocks: list[tuple[str, Zone]]) -> LayoutView:
    return LayoutView(
        doc_id="scale",
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")],
        blocks=[TextBlock(text=text, zone=zone, page=1) for text, zone in blocks],
    )


def tax_statement_spec() -> DocTypeSpec:
    """The document under test: a payroll tax statement."""
    return DocTypeSpec(
        doctype_id="wage_statement",
        label="Wage and Tax Statement",
        country="US",
        category=Category.tax,
        issuing_authority="Internal Revenue Service",
        anchors=[
            Anchor(text="WAGE AND TAX STATEMENT"),
            Anchor(text="Social security wages"),
            Anchor(text="Medicare wages and tips"),
            Anchor(text="Federal income tax withheld"),
        ],
        fields=[
            FieldSpec(name="wages", type="number", labels={"en": ["Wages, tips"]}),
            FieldSpec(name="employer", labels={"en": ["Employer name"]}),
        ],
    )


def rival_spec() -> DocTypeSpec:
    """A genuine competitor: shares the payroll vocabulary, differs in its own."""
    return DocTypeSpec(
        doctype_id="payroll_advice",
        label="Payroll Advice",
        country="US",
        category=Category.financial,
        anchors=[
            Anchor(text="EARNINGS STATEMENT"),
            Anchor(text="Federal income tax withheld"),
            Anchor(text="Net pay"),
            Anchor(text="Pay period"),
        ],
        fields=[
            FieldSpec(name="net_pay", type="number", labels={"en": ["Net Pay"]}),
            FieldSpec(name="pay_period", type="date", labels={"en": ["Pay Period"]}),
        ],
    )


def irrelevant_specs(n: int) -> list[DocTypeSpec]:
    """``n`` doctypes whose vocabulary shares nothing with the document under test.

    Built from a private syllable alphabet so that no term, unigram or bigram, can collide with
    the payroll vocabulary above. These stand in for "the next country pack": doctypes that are
    perfectly legitimate and completely unrelated to the document in hand.
    """
    specs = []
    for i in range(n):
        tag = f"zqx{i:03d}"
        specs.append(
            DocTypeSpec(
                doctype_id=f"filler_{i:03d}",
                label=f"Filler Instrument {tag}",
                country="XX",
                category=Category.other,
                issuing_authority=f"Bureau of {tag}",
                anchors=[
                    Anchor(text=f"{tag.upper()} REGISTRATION INSTRUMENT"),
                    Anchor(text=f"{tag.upper()} SCHEDULE"),
                ],
                fields=[
                    FieldSpec(name=f"{tag}_reference", labels={"en": [f"{tag} Reference"]})
                ],
            )
        )
    return specs


def wage_statement_view() -> LayoutView:
    return _view(
        [
            ("WAGE AND TAX STATEMENT", Zone.title),
            ("Social security wages 61,204.00", Zone.body),
            ("Medicare wages and tips 61,204.00", Zone.body),
            ("Federal income tax withheld 7,940.12", Zone.body),
            ("Wages, tips, other compensation 61,204.00", Zone.body),
            ("Employer name: NORTHWIND TRADING LLC", Zone.body),
        ]
    )


def _decision(result) -> tuple:
    """Every field of a classification that a caller could act on.

    Deliberately excludes ``ms`` (wall clock) and ``evidence`` prose. It does include
    ``confidence``, ``margin`` and ``coverage``: those are written into a KYC decision record,
    and a number that drifts because an unrelated country pack shipped is not a number anyone
    can audit.
    """
    return (
        result.doctype_id,
        result.abstained,
        result.confidence,
        result.margin,
        result.coverage,
    )


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------
def test_irrelevant_doctypes_cannot_change_the_verdict():
    """The core property. Same document, same evidence, 120 more doctypes, same answer.

    Profiles are pinned to the padded registry and shared by both runs, which isolates the
    *acceptance rule* from the profile estimator. That separation is not a convenience: there
    is a known, separate, measured residual coupling in ``dce.classify.profiles``
    (``_log_odds_profiles`` pools background counts across all specs, so adding a doctype
    perturbs every existing doctype's weights — mean L1/2 shift 0.164 going from 58 to 121
    doctypes). That is a real defect and it is tracked separately; it is not this rule's, and
    conflating the two would let a fix to either one hide a regression in the other. The
    following test covers the end-to-end behaviour with profiles rebuilt.
    """
    core = [tax_statement_spec(), rival_spec()]
    padded = core + irrelevant_specs(PADDING)
    profiles = build_profiles(padded)
    view = wage_statement_view()

    small = classify(view, core, settings=SETTINGS, profiles=profiles)
    large = classify(view, padded, settings=SETTINGS, profiles=profiles)

    assert small.abstained is False, "the fixture must actually decide something"
    assert _decision(small) == _decision(large), (
        f"registry size changed the answer: {len(core)} doctypes -> {_decision(small)}, "
        f"{len(padded)} doctypes -> {_decision(large)}"
    )


@pytest.mark.parametrize("padding", [0, 5, 15, 40, PADDING])
def test_the_verdict_is_flat_across_every_registry_size(padding: int):
    """Not just the endpoints. The old rule was non-monotone, not merely degrading.

    ``ca_t1_general`` went unknown -> accept -> unknown -> accept as irrelevant doctypes were
    added, which is not a drift anyone can reason about — it is denominator noise. Checking
    only ``N=2`` against ``N=122`` would let that back in.
    """
    core = [tax_statement_spec(), rival_spec()]
    profiles = build_profiles(core + irrelevant_specs(PADDING))
    view = wage_statement_view()

    reference = classify(view, core, settings=SETTINGS, profiles=profiles)
    padded = classify(
        view, core + irrelevant_specs(padding), settings=SETTINGS, profiles=profiles
    )

    assert _decision(reference) == _decision(padded)


def test_the_verdict_survives_the_profile_estimator_being_rebuilt():
    """End to end, with profiles refitted per registry, as production does it.

    Weaker than the test above — it asserts the *verdict* rather than every digit — because the
    profile estimator's background pooling genuinely does move the weights when the registry
    changes. That coupling is small, real, and outside the acceptance rule. Pinning it here
    too would hide it; asserting bit-equality here would fail for a reason this module is not
    about. Asserting the verdict is the honest middle.
    """
    core = [tax_statement_spec(), rival_spec()]
    padded = core + irrelevant_specs(PADDING)
    view = wage_statement_view()

    small = classify(view, core, settings=SETTINGS, profiles=build_profiles(core))
    large = classify(view, padded, settings=SETTINGS, profiles=build_profiles(padded))

    assert (small.doctype_id, small.abstained) == (large.doctype_id, large.abstained)


def test_the_lexical_probability_moves_with_the_registry_and_the_explained_mass_does_not():
    """Pin the *mechanism*, so a future author cannot quietly re-gate on the wrong quantity.

    ``LexicalOutcome.probability`` is a softmax over the registry and is therefore a function
    of registry size as well as of the document. ``LexicalOutcome.explained`` is defined from
    ``(document, spec_c)`` alone.

    Note what the numbers do here, because it is more damning than "it shrinks". Padding this
    fixture with *unrelated* doctypes drives the probability **up** (0.905 at N=2 to 0.9998 at
    N=122): the winner becomes an ever-larger outlier, so the robust z-score inflates and the
    softmax saturates. On the real corpus, where the added doctypes are genuine competitors,
    the same quantity moves **down** (0.900 to 0.411 for a US W-9). A number whose direction of
    travel depends on which other doctypes happen to be installed cannot gate an accept, in
    either direction. This test therefore asserts *instability*, not a sign.
    """
    core = [tax_statement_spec(), rival_spec()]
    padded = core + irrelevant_specs(PADDING)
    view = wage_statement_view()

    small = lexical_scores(view, build_profiles(core), settings=SETTINGS)
    large = lexical_scores(view, build_profiles(padded), settings=SETTINGS)

    probability_drift = abs(
        large.probability["wage_statement"] - small.probability["wage_statement"]
    )
    explained_drift = abs(
        large.explained["wage_statement"] - small.explained["wage_statement"]
    )

    assert probability_drift > 0.05, (
        "probability stopped being registry-normalised; if that is genuinely true now, the "
        "warning on LexicalOutcome.probability needs rewriting, not deleting"
    )
    # Explained mass is computed per (document, spec), so the definition contributes nothing
    # here. The residual is entirely the profile estimator's background pooling (see
    # profiles.py), which is a separate, tracked defect — measured at ~0.04 across this 60x
    # registry growth. The assertion is that it stays an order of magnitude below the accept
    # margin, not that it is zero.
    assert explained_drift < 0.05
    assert explained_drift < probability_drift


def test_an_irrelevant_doctype_scores_zero_on_both_channels():
    """The padding really is irrelevant — otherwise every test above proves nothing."""
    padded = [tax_statement_spec(), rival_spec(), *irrelevant_specs(PADDING)]
    view = wage_statement_view()
    anchor = anchor_scores(view, padded, settings=SETTINGS)
    lexical = lexical_scores(view, build_profiles(padded), settings=SETTINGS)

    for spec in padded:
        if not spec.doctype_id.startswith("filler_"):
            continue
        assert anchor.scores.get(spec.doctype_id, 0.0) == 0.0, spec.doctype_id
        assert lexical.explained.get(spec.doctype_id, 0.0) == 0.0, spec.doctype_id


# ---------------------------------------------------------------------------
# The second invariant: contention must not grow with the registry
# ---------------------------------------------------------------------------
def test_the_contending_set_does_not_grow_with_the_registry():
    """The guard the padding test cannot see, and the one a future 'optimisation' would trip.

    A rule can be padding-invariant on a fixed corpus and still be O(N) underneath — one
    rejected proposal admitted candidates on "coverage >= 0.20 OR any anchor hit", which
    measured at a dead-flat 15% of the registry at N=20, 40, 80 and 121. Its own invariance
    test padded only with doctypes its own admission rule excluded, so the growth was invisible
    to it. At 500 doctypes that shortlist projects to ~75 members and the original size-
    dependence returns wearing a smaller denominator.

    So: measure how many doctypes carry ANY evidence for one document as the registry grows,
    and require that number to grow sublinearly. Anything that keeps a constant fraction of the
    registry in contention has reintroduced DEFECT 1.
    """
    view = wage_statement_view()
    fractions = []
    for size in (10, 40, 80, PADDING):
        specs = [tax_statement_spec(), rival_spec(), *irrelevant_specs(size)]
        anchor = anchor_scores(view, specs, settings=SETTINGS)
        lexical = lexical_scores(view, build_profiles(specs), settings=SETTINGS)
        contending = sum(
            1
            for spec in specs
            if anchor.scores.get(spec.doctype_id, 0.0) > 0.0
            or lexical.explained.get(spec.doctype_id, 0.0) > 0.0
        )
        fractions.append(contending / len(specs))

    assert fractions == sorted(fractions, reverse=True), (
        f"the contending fraction of the registry is not shrinking: {fractions}"
    )
    assert fractions[-1] < fractions[0] / 2, (
        f"contention is growing as a near-constant fraction of the registry: {fractions}"
    )


# ---------------------------------------------------------------------------
# The degenerate cases the lead test cannot see
# ---------------------------------------------------------------------------
def test_a_lone_candidate_must_still_clear_the_bar_unaided():
    """With one doctype installed, the lead test is vacuous — support must carry it.

    This is the explicit null hypothesis. A sole survivor is measured against "none of the
    above", not awarded certainty for having nobody to beat. An auditor will ask this question
    in exactly these words, so it gets its own test rather than living in a fallthrough.
    """
    lonely = [tax_statement_spec()]
    unrelated = _view([("Chapter four: the harvest was late that year.", Zone.body)])

    result = classify(unrelated, lonely, settings=SETTINGS)

    assert result.doctype_id == "unknown"
    assert result.abstained is True
    assert "coverage below floor" in result.reason


def test_a_lone_candidate_is_not_accepted_even_when_the_document_is_its_own():
    """A one-doctype registry cannot accept, and that is the intended answer.

    The log-odds estimator produces an *empty* profile for a registry of one — nothing can be
    surprisingly frequent in a class relative to a background pooled from that same class — so
    the lexical channel has nothing to say about anything. The rule requires two independent
    tiers to concur, and a silent tier cannot concur, so this abstains even though the anchor
    channel is at 0.969 and anchor coverage is 1.0.

    That is the correct behaviour and not a gap to be patched. Accepting here would mean "this
    is the only document type we know of, therefore it is this one", which is exactly the
    least-wrong-of-a-tiny-registry failure the whole design exists to refuse. The document goes
    to a human, which in KYC is safe; a confident wrong doctype is a compliance incident.
    """
    result = classify(wage_statement_view(), [tax_statement_spec()], settings=SETTINGS)

    assert result.doctype_id == "unknown"
    assert result.abstained is True
    assert result.confidence == 0.0, (
        "no doctype was identified, so there is nothing to report confidence in"
    )
    # The refusal names the mechanism directly instead of reporting it as a thin margin. A
    # tie-break among zeros is bookkeeping, not an opinion: the lexical channel did not
    # narrowly agree, it said nothing at all, and the reason has to say which it was — the
    # remedy for a silent tier (install a second doctype) is not the remedy for a thin margin.
    assert "no doctype was identified" in result.reason
    assert "silent" in result.reason
    assert "lexical" in result.reason, "the reason must name the channel that went silent"


def test_two_doctypes_are_enough_for_the_lexical_channel_to_speak():
    """The floor of usability: with a real rival installed, the same document is accepted.

    Pinned so that the previous test reads as "one is not enough" rather than "this cascade
    cannot accept anything".
    """
    result = classify(
        wage_statement_view(), [tax_statement_spec(), rival_spec()], settings=SETTINGS
    )

    assert result.doctype_id == "wage_statement"
    assert result.abstained is False


def test_a_two_doctype_registry_of_junk_still_abstains():
    """At N=2 one garbage rival gives a garbage winner an enormous lead.

    The lead gate is useless here — it is satisfied trivially — which is precisely why the
    support and coverage floors are not decoration. They are inert on the reference corpus
    (sweeping support over 0.20-0.50 and coverage over 0.15-0.30 changes nothing in any of the
    sixteen combinations), and that is not permission to remove them: this is the case they
    exist for.
    """
    result = classify(
        _view([("Chapter four: the harvest was late that year.", Zone.body)]),
        [tax_statement_spec(), rival_spec()],
        settings=SETTINGS,
    )

    assert result.doctype_id == "unknown"
    assert result.abstained is True
