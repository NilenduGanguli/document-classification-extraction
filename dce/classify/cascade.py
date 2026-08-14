"""The cascade: L0 → L1 → L2 → (L3) → accept or abstain.

Cheap and certain first, expensive and uncertain last, and a hard refusal at the end.

===========  =====================================================================
Tier         What it contributes
===========  =====================================================================
L0           ``log P(c|structure)`` — page count, shape, tables, marks, MRZ shape.
L1           Anchors and checksums. A decisive anchor or a corroborated,
             checksum-verified identifier is the strongest evidence here, and it
             enters the accept rule as evidence — it does not bypass it.
L2           Zone-weighted BM25 over the per-doctype term profiles, plus coverage.
L3           Optional local BERT kNN. Off by default, imported only when on.
L4           Abstain → ``UNKNOWN`` → human queue.
===========  =====================================================================

.. note::

   **Every measurement quoted in this module was taken against the 181-doctype registry,
   which included an India pack (52 ``in_*`` doctypes) and 41 Indian documents in the
   corpus. That pack and those documents have since been removed: the registry is 129
   doctypes and the corpus 117 documents.** The records are kept verbatim rather than
   restated, because restating a number that was not re-measured is worse than carrying a
   dated one — and the reasoning they support (why there is one accept rule, why
   concurrence is pairwise, why a tie is not an opinion) does not turn on which doctypes
   were registered when the measurement was made. **A doctype id beginning ``in_`` anywhere
   below is a citation of that removed pack, not a live doctype.** Re-measure before
   quoting any figure here as current.

**There is exactly one accept rule and exactly one confidence number.** There used to be
two-and-a-half: a checksum short-circuit, a decisive-anchor short-circuit — both of which
returned a :class:`~dce.models.Classification` *before* the accept rule was ever evaluated,
each with its own hard-coded confidence — and the two-channel rule itself, which governed
whatever was left. Measured on the reference corpus, the short-circuits produced 25 of 35
accepts: the documented design decided a minority of the decisions it was documented to make.
That is not a tuning problem, it is three problems:

* **Incoherent margins.** The short-circuit reported ``margin = score - max(other anchor
  scores)`` where ``score`` was a hard-coded constant (0.90) and the runner-up came off the
  squashed anchor channel. Two different scales subtracted from one another, so an *accepted*
  classification could and did report a **negative** margin — three of them, including
  ``in_utility_electricity_2`` at -0.047 and ``us_operating_agreement`` at -0.070. A negative
  margin on an accept is not a number a reviewer can interpret; it is a statement that the
  winner lost.
* **Unordered confidence.** Accepts and abstentions overlapped in reported confidence — an
  abstention at 0.475 outranked an acceptance at 0.409 — so the headline number did not even
  order the two outcomes it exists to distinguish.
* **Dead controls.** ``classify_min_support`` and ``classify_min_coverage`` could not fire on
  the short-circuit path, because that path returned first. Five accepts shipped below the
  coverage floor. A gate that never fires tells a control reviewer a control exists when it
  does not, which is worse than having no gate at all.

Fusion is still a weighted sum, with the weights from config::

    score_c = log P(c|structure) + 3.0*anchor + 1.0*lexical + 0.8*bert

and a temperature softmax still turns it into a probability — **for the audit trail and the
runners-up list only**. Nothing decides on it. That sentence used to read "and a temperature
softmax turns the fused scores into a probability", followed by an accept rule whose first
condition was ``p >= classify_accept_probability``, and that rule was wrong in a way no choice
of threshold could repair:

    ``p_top = exp(s_top/T) / SUM_c exp(s_c/T)``

Every doctype in the registry contributes a strictly positive term to that denominator,
whether or not it has anything to do with the document. So ``p_top`` falls monotonically as
the registry grows, *for every document*, and comparing it to a fixed floor compares a
registry-size-dependent number to a constant. Measured, on one unchanged US W-9 with identical
evidence: ``p = 0.900`` against 25 doctypes, ``0.411`` against 121 — accept, then abstain, with
nothing about the document having changed. Every country pack shipped degraded every doctype
already installed. (``lexical.probability`` is a second, hidden instance of the same defect —
it is itself a softmax over the registry — which is why the lexical term that feeds ``fused``
also shrinks with ``N`` and can *reorder* the ranking, not merely rescale it.)

**The rule: two absolute channels combined into one, plus an explicit null.** Two independent
pieces of evidence are computed per doctype, each a function of ``(document, spec_c)`` and of
nothing else:

===============  ==========================================================================
``A_c``          ``anchor.scores[c]`` — ``min(0.97, 1 - 0.5^raw_c)``, already absolute. Its
                 unclipped form ``anchor.bits[c]`` = ``raw_c`` is what the comparisons read.
``L_c``          ``lexical.explained[c]`` — the fraction of class ``c``'s own idf-weighted
                 profile mass the document exhibited, BM25-saturated. Absolute, in [0, 1].
===============  ==========================================================================

and are combined by noisy-OR into the single quantity the decision is actually made on::

    S_c = 1 - (1 - A_c)(1 - L_c)

``S`` is the *support* for class ``c``: two independent channels, neither of which has to be
right on its own. It is absolute and per-``(document, spec_c)`` exactly as its two inputs are.

**The rule reads that quantity in bits, and this is the round-3 correction.** Take ``-log2``
of the residual and the noisy-OR becomes a sum::

    B_c = -log2(1 - S_c) = anchor_bits_c + lexical_bits_c

``S`` and ``B`` are monotone transforms of one another, so they *rank* identically — and they
do not *subtract* identically, which matters because the accept rule subtracts. ``S`` is
bounded above by 1 and ``A`` is clipped at 0.97 outright, so a difference taken on ``S``
measures the remaining headroom rather than the evidence. Concretely, and this is the whole
defect in one line: **once the runner-up's ``S[2]`` exceeds 0.96 there is less than 0.04 of
headroom left below the bound, so the old gate ``S[1] - S[2] >= 0.04`` was unsatisfiable at
any level of evidence for the winner.** The anchor tier reaches its clip at 5.06 bits and a
``title`` zone multiplies anchor bits by 2.0, so pushing a runner-up past that line is what
zone labels routinely do.

Measured, through the container, on the 59-document reference corpus: 36 correct with every
block read as ``body`` and **32** correct with title/heading zones present — all five losses
refused by the separation gate with the winner's support at 0.98. More information, fewer
answers. Production always has zones (Azure DI → ``ROLE_ZONES`` in :mod:`dce.adapters`), so the
configuration production can never run was the better one and the 36 was a number production
would never see. The separation gate is therefore evaluated as ``1 - 2^-(B[1] - B[2])``
(:func:`separation_of`), which is the same scale and the same first-order meaning as before —
``classify_min_margin`` keeps its value — with no ceiling for stronger evidence to be squeezed
against.

Let ``d = argmax B``. Acceptance of ``d`` requires **all four** of:

1. **identification** — ``d`` is picked out by evidence, not merely by being the least-bad
   thing on the shelf. Two ways to satisfy this and no others:

   *concurrence* — let ``r`` be the runner-up, ``B[2]``'s doctype. Each tier must hold
   evidence for ``d`` and must not prefer ``r`` to it: ``A_d > 0``, ``L_d > 0``,
   ``A_d >= A_r`` and ``L_d >= L_r``. Since ``B = A_bits + L_bits`` and ``d`` leads ``B``, at
   least one tier strictly prefers ``d``, so this cannot be satisfied by two indifferent
   tiers. Read it as: **the two tiers independently agree about the comparison the accept is
   actually making.** See round 4 below for why the opponent has to be ``r``.

   *conclusive L1* — exactly one doctype in the registry holds evidence of the kind that is
   near-proof on its own (a decisive anchor: an issuing-authority header or a form number; or
   a checksum-verified identifier the document also labels), that doctype is ``d``, and no
   confusable peer's decisive claim was muted by a missing zone on this payload.

2. **separation** — ``1 - 2^-(B[1] - B[2]) >= classify_min_margin``. One subtraction, one
   scale, and therefore **never negative on an accepted answer**, which is the property the
   old short-circuit's cross-scale subtraction could not have — and now also **monotone**,
   which the ``S[1] - S[2]`` form was not.
3. **support** — ``S_d >= classify_min_support``, the explicit null hypothesis: "none of the
   above". This is what stops "least-wrong of a tiny registry" — with one rival, a garbage
   winner can hold an enormous lead. Deliberately still on the saturating scale: a *cap on
   what one tier may claim on its own* is the right shape for an absolute floor and the wrong
   shape for a comparison.
4. **coverage** — ``max(profile coverage, anchor coverage) >= classify_min_coverage``.

**And gate 1b: a zone label may sharpen a decision, it may not make one.** When the payload
carries paragraph roles, every quantity the four gates read is computed on the *un-promoted*
reading of it — each block whose zone weighs more than ``body`` (``title`` 3.0x, ``heading``
2.0x, ``table`` 1.2x) re-read at ``body``, ``furniture``'s 0.25x discount kept because it can
only lower a score and therefore can only cost an accept, never buy a wrong one. The reason is
that a role is the layout provider's opinion and Azure DI gives ``title`` to captions,
watermarks and marketing copy as a matter of routine; at 3.0x lexical and 2.0x anchor, and as
the only zone in which 21 of the registry's decisive anchors are audible, one bad label was
worth up to four bits of evidence for an unrelated doctype. Measured by appending ONE line
carrying another doctype's decisive anchor to eight correctly-classified documents, once
labelled ``title`` and once ``body`` — identical text, only the label differing:

=====================  ==============  =============  =======
accept rule            title label     body label     ratio
=====================  ==============  =============  =======
round 2                23 wrong        3 wrong        7.67x
round 3                5 wrong         5 wrong        1.00x
=====================  ==============  =============  =======

What roles still do is *add*: a zone-**restricted** anchor is a registry claim about the
document ("this issuer prints this string as its title"), not a weight, and it still
establishes the conclusive-L1 route. So labels can raise a verdict and cannot lower one, which
is why ``--layout`` scoring at least as well as plain text is a property of the rule rather
than an observation about 59 files. Re-measured after the change: **plain text 42 correct,
inferred zones 43 correct**, two wrong in both.

Every gate is evaluated on every document. The old design's conclusive-L1 evidence used to
*skip* gates 2-4 by returning early; now it satisfies gate 1 and is measured against the other
three like anything else. Measured on the reference corpus (61 text-layer documents; the
run-to-run comparison is against the same registry, same harness, same hour):

=========================================  ===========  ===========  ===========
                                           before       round 1/2    round 3
=========================================  ===========  ===========  ===========
correct / wrong / abstained (plain text)   34 / 1 / 26  36 / 1 / 24  42 / 2 / 15
correct / wrong / abstained (zones)        —            32 / 1 / 26  43 / 2 / 14
precision when it answered (plain text)    97.1%        97.3%        95.5%
accepts reporting a **negative** margin    3            0            0
accepts below the coverage floor           5            0            0
confidence orders accept above abstain     no           yes          yes
zones score at least as well as no zones   —            no           yes
=========================================  ===========  ===========  ===========

The round-3 column costs one wrong answer and it is named rather than netted off, because it
is the whole reason precision reads lower: ``corpus/mx/mx_cif.pdf`` is now answered
``mx_rfc_csf``. That is not a new misreading — the file is *literally* a Constancia de
Situación Fiscal ("Página [1] de [3]", the full CSF body, the CIF cédula as its header block),
it matches six ``mx_rfc_csf`` anchors against three ``mx_cif`` ones, and it is the known-open
``mx_cif``/``mx_rfc_csf`` page-segmentation item. What changed is that the old rule *abstained
on it for the wrong reason*: both doctypes clipped to ``A = 0.97``, so ``S[1] - S[2]`` came to
0.0000 and the margin gate refused. It would have refused identically had the evidence been a
hundred to one. Un-clipping the comparison removed an accidental refusal, and the underlying
ambiguity belongs to the registry and to page segmentation.

Of the five accepts that used to ship below the coverage floor, three are now abstentions —
including a Canadian permanent-resident card being accepted as a US green card and a US
operating agreement being accepted as a foreign memorandum of association, both at the
hard-coded 0.90 — and two are still accepted but now report an honest coverage number: the
short-circuit reported *anchor* coverage alone because it never ran the lexical tier, so the
number it published understated them.

One answer got worse and it is named rather than netted off: a US REAL ID specimen, which used
to abstain, is now accepted as ``us_drivers_license``. A REAL ID *is* a driver licence carrying
a compliance marking, the two doctypes share nearly all their vocabulary, and ``us_real_id``
gates its distinguishing decisive anchor to ``zone=title`` — which the text-layer route cannot
produce. It is a registry-separability and zone-fidelity problem, and it was left to those
owners rather than fixed here by moving a threshold until this one file passed.

That REAL ID answer is still wrong in round 3, in both zone modes, and is still left to the
registry and zone-fidelity owners.

The zone-mode A/B is the number to read, because production always has roles. It went
32 / 1 / 26 with zones against 36 / 1 / 24 without — zones *costing* four answers — and is now
43 / 2 / 14 with zones against 42 / 2 / 15 without. The gap did not close by tuning; it closed
because the two comparisons the rule makes are no longer taken on a saturating scale and are
no longer taken on the provider's roles at all.

**Round 4: gate 1 and gate 2 have to be about the same opponent.** Rounds 1-3 left gate 1 in
its original form, ``argmax(A) == argmax(L) == d`` — a *global rank* statistic over the whole
registry, while gate 2 compares ``d`` against exactly one doctype, the runner-up ``r``. The two
gates therefore asked about different opponents, and gate 1's opponent was whichever doctype
happened to top a channel, which for the lexical channel is routinely a doctype that carries no
anchor evidence, sits far down ``B``, and could not be accepted under any circumstances. It
could not be the answer, and it silently vetoed the answer anyway.

Worse, that veto is **not a function of how much evidence the dissenter holds.** A channel that
prefers a rival by a hair and a channel that prefers it by four bits are the same input to an
argmax. Measured, on ``corpus/us/us_sec_20f.pdf``: the lexical channel scores
``in_mca_aoc4_financial_statements`` at 0.2358 and the correct ``us_sec_20f`` at 0.2267 — a
0.017-bit preference, on a document where the anchor channel holds 10.6 bits for ``us_sec_20f``
and the combined lead is 2.60 bits. The rule could not tell a channel that *contradicts* ``d``
from one that is merely *indifferent between ``d`` and a neighbour*, and refused on both.

And it let a **tie-break** decide a classification. ``_ranked_channel`` breaks ties by doctype
id so the audit record is reproducible across runs — right for a record, wrong for an opinion,
and the argmax test read it as an opinion. Two doctypes printing the same masthead tie on the
anchor channel; the alphabetically-first id was then named "the anchor winner", and the other
failed identification however decisively the lexical channel preferred it. Two registries
identical in every number — same anchors, same profiles, same document, same evidence — and
differing only in whether the true doctype's id sorts before or after its sibling's got
different verdicts: accept, then abstain. Pinned in
``tests/test_classify.py::test_identification_does_not_turn_on_how_a_doctype_id_is_spelled``.
``corpus/ca/ca_bn_letter.pdf`` sat on the lucky side of exactly that coin-toss: it ties
``in_form16`` at 2.200 anchor bits and was accepted because ``c`` sorts before ``i``. A tie is
the statement "this tier has no preference between these two", which is why the pairwise form
reads it as neither concurrence nor dissent, and requires the tier to hold evidence for the
candidate before its indifference counts for anything.

Restated pairwise — each tier must hold evidence for ``d`` and must not prefer ``r`` to it —
the gate asks the two tiers about the comparison the accept is actually making. It keeps
exactly the property the whole design leans on and that the two rejected experiments below
lost: **a tier that positively prefers the rival still refuses.** The rewrite is a strict
*superset* of the old test (leading a channel outright implies both holding evidence on it and
not losing ``r`` on it), so no answer the previous form found can be withdrawn by it; the only
question a measurement has to settle is whether it admits a wrong one.

Measured on the 158-document corpus (150 measured, 8 with no text layer), both arms run
back-to-back in one process against one pinned registry of 181 doctypes, so the registry, the
profiles and the corpus are byte-identical between them:

=========================================  ==============  ==============
                                           round 3         round 4
=========================================  ==============  ==============
correct / wrong / abstained (plain text)   119 / 0 / 31    129 / 0 / 21
correct / wrong / abstained (zones)        117 / 0 / 33    128 / 0 / 22
precision when it answered                 100%            100%
abstention rate                            20.7%           14.0%
answers inferred zones cost                2               1
=========================================  ==============  ==============

The registry was being edited by its own owner while this was measured, so the pair above is
one snapshot (181 doctypes) with both arms run against it back-to-back and the corpus and
registry files hashed identical before and after. The *delta* is the durable claim, and it
reproduced unchanged on an earlier snapshot of the same registry: 118 -> 128 there, 119 -> 129
here, the same ten documents both times, no loss and no wrong answer either time.

Ten documents moved and every one of them moved from ``UNKNOWN`` to its correct doctype:
``ca_ni_43_101_technical_report``, ``in_brsr``, ``in_partnership_reg_cert``,
``mx_acta_asamblea``, ``us_birth_certificate``, ``us_sec_10q``, ``us_sec_20f`` (both the PDF and
the EDGAR HTML), and ``us_secretary_certificate`` (both). No document changed in the other
direction and no wrong answer was created.

The documents that stayed refused are the reason to believe the gate still works, so they are
named. On ``corpus/us/us_1099.pdf`` the anchor channel holds 7.20 bits for ``us_w9`` against
4.90 for the correct ``us_1099`` — a registry defect, not a classifier one — and the lexical
channel prefers ``us_1099``; the gate refuses. On
``corpus/ca/ca_articles_incorporation_provincial.pdf`` — the document the top-k experiment
below got *wrong* — the anchor channel leads with ``us_articles_incorporation`` and the lexical
channel prefers the true Canadian class, so the gate refuses. Same for
``corpus/ca/ca_sin_confirmation.pdf`` (anchors 5.48 bits for ``in_birth_certificate``),
``corpus/us/us_operating_agreement.pdf``, ``corpus/us/us_paystub.pdf`` and
``corpus/mx/mx_cif.pdf``. In every one of them a spuriously-firing anchor channel is caught by
a lexical channel that prefers the rival — which is the independence the two-channel design is
built on, now measured against the rival that matters instead of against a global argmax.

Two properties were re-measured rather than argued. **Scale invariance**: with the term
profiles rebuilt at registry sizes 5 / 10 / 25 / 50 / 181 and the padding restricted to
doctypes provably irrelevant to each document (anchor score exactly 0.0, explained below 0.05),
the ``(doctype, abstained)`` verdict is unchanged on 138 of 150 documents against 137 of 150
for the previous rule, with the same largest confidence swing and a slightly smaller mean one
(0.0697 against 0.0737). The rule reads only two doctypes' per-``(document, spec)`` quantities,
so a doctype that carries no evidence for a document cannot enter the comparison at any
registry size; the residual movement is the pre-existing idf drift inside ``lexical.explained``
described above, and it is not made worse here. **Monotonicity**: supplying inferred zones cost
the previous rule two answers (119 -> 117) and costs this one one (129 -> 128).

That remaining one is named rather than netted off, because it is a real limit of a property
this module states more strongly than it holds. ``corpus/ca/ca_bn_letter.pdf`` is correct on
plain text and abstains with inferred zones, under *both* rules. The cause is not a weight:
a ``zone``-restricted anchor belonging to ``in_form16`` becomes *audible* once a line carries a
label, which is genuinely added evidence for a rival, and it narrows ``ca_bn_letter``'s anchor
standing from a tie to a 0.120-bit deficit. So "labels can raise a verdict and cannot lower
one" is exactly true of the zone *weights* — gate 1b levels those away — and is **not** true of
zone *restrictions*, which are kept on purpose because they are registry claims about the
document rather than opinions about layout. ``corpus/us/us_state_id.pdf`` moves the other way by
the same mechanism and is recovered by zones. Both belong to whoever owns the zone-gated
anchors; neither is a reason to move a threshold here. The document the previous rule *also*
lost to zones, ``corpus/mx/xx_ubo_declaration_2.pdf``, is no longer lost.

**Confidence is a distance to the binding constraint, not a posterior.** It is reported as::

    confidence = min( separation , strength , breadth )   when identification holds
               = 0.0                                      when it does not

    separation = lead    / (lead    + classify_min_margin)
    strength   = S_d     / (S_d     + classify_min_support)
    breadth    = cov_d   / (cov_d   + classify_min_coverage)

Each factor is **exactly 0.5 at its own floor**, so the ``min`` is ``>= 0.5`` if and only if
every gate passed. **0.5 is the decision boundary**: every accepted answer reports at least
0.5, every abstention reports strictly less, and the number therefore *orders outcomes*, which
the previous two-scale arrangement did not (an abstention at 0.494 above an acceptance at
0.409). Reading it is direct: the value is set by whichever control came closest to blocking
the accept, so 0.52 means "one gate barely cleared" and 0.8 means "nothing was close". There
is no free constant in it — each denominator is that gate's own configured floor, which also
means none of the three floors can be inert: every one of them scales the headline number of
every document whether or not it binds.

It is not a probability and is not offered as one. A calibrated posterior would need labelled
production data this service does not have; inventing a number that *looks* like one is how
the registry-normalised softmax got into the accept path in the first place.

The verdict is a function of exactly five numbers: ``B[1]``, ``B[2]``, the winner's support
and coverage, and (through identification) the argmaxes of ``A`` and ``L``. Adding a doctype
``X`` can change
it *only* by entering the top two of a channel, which requires ``X`` to carry real anchor or
real lexical evidence **for this document**. A doctype unrelated to the document has
``A_X = 0`` and ``L_X ≈ 0``, hence ``S_X ≈ 0``, and cannot enter any top two at any registry
size. Verified by experiment: padding the candidate set with doctypes provably irrelevant to
each document (anchor score exactly 0.0 and explained below 0.05) changed the old
registry-normalised rule's verdict on 8 of 59 documents, with a mean confidence swing of
0.1226.

Re-measured against **this** rule, in the harder form of the experiment — registry sizes
5/10/25/50/121 with the term profiles rebuilt at each size, so the idf drift is included
rather than held fixed: the ``(doctype, abstained)`` verdict is unchanged on 60 of 61
documents and the largest confidence swing across all five sizes is 0.038. The one exception
is a document sitting 0.002 above the accept boundary (0.5018 at k=5, 0.4981 at k=50), which
the drift tips across. That residual is not the defect round 1 removed — that one was a
systematic, monotone collapse of the decision quantity with N, of size 0.49 on a single
document — it is the idf term inside ``lexical.explained``, which appears in both the
numerator and the denominator of that ratio and therefore very largely, but not exactly,
cancels. ``tests/test_registry_scale_invariance.py`` pins the property.

**Coverage still carries its weight and is deliberately not retired.** It is the one absolute
control this cascade has always leaned on, it is the reason a photo-ID specimen sheet with
almost no text abstains cheaply, and removing a working control in the same change that
rewrites the scoring is not something a compliance reviewer should be asked to accept.

**The cost of deleting the short-circuits is latency, and it was paid deliberately.** Every
document now runs the lexical tier, where a checksum-verified passport used to skip it. BM25
against pre-built profiles is a dictionary sweep, not a model; the alternative is a decision
path that no gate can see, and there is no latency budget at which that is the right trade in
a KYC control.

**Two things that look like this rule and are not, both measured and both rejected.**

*Normalising over the top-k instead of the whole registry.* Scale-invariant, and it recovers
more documents (26 → 32 correct) — but it admits two new WRONG answers, because the fused
runner-up is not reliably the real competitor. On ``ca_articles_incorporation_provincial`` the
true class ranked #3, so a top-2 test compared the winner against an irrelevant foreign
doctype and passed it. Channel *agreement* is what buys the precision here, not the
scale-invariance:
both of those failures had the winner leading on the saturated anchor channel while the
lexical channel pointed somewhere else. Do not "optimise" the absolute bar into a top-k.

Round 4's pairwise concurrence is **not** that idea wearing a different hat, and the difference
is worth stating because both sentences contain the words "the runner-up". The top-k experiment
changed how the decision *quantity* was normalised and, in doing so, dropped the agreement
requirement — which is why it accepted documents where one channel led and the other pointed
elsewhere. Round 4 changes neither the quantity nor the requirement: the channels are still
absolute and per-``(document, spec)``, and a channel that prefers the rival still refuses. It
changes only *which* opponent agreement is asked about, from "whatever tops this channel across
the whole registry" to "the doctype the accept is actually being made over". The check is that
``ca_articles_incorporation_provincial`` — the document the top-k form got wrong — is still
refused under round 4, and refused for the substantive reason: the lexical channel prefers the
true Canadian class to the ``us_articles_incorporation`` the anchor channel is leading with.

*Standardising a shortlist with* :func:`~dce.classify.lexical.robust_z`. Over two or three
values the MAD is degenerate and the result is ``z = ±1`` however close the inputs were —
manufactured separation. 37 correct but 3 wrong, precision 92.5%, below the baseline.

**L4 is a feature, not a failure.** An abstention routes a document to a human queue. It never
routes it to a model: the whole point of this service is that unclassified content does not
leave the process, and "ask an LLM what this is" is that leak wearing a different hat.
"""
from __future__ import annotations

import importlib
import math
import pkgutil
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from dce.config import Settings, get_settings
from dce.egress import EgressViolation, classification_scope
from dce.models import UNKNOWN, Classification, DocTypeSpec, Evidence, LayoutView, Zone

from . import anchors as anchors_tier
from . import structural as structural_tier
from .lexical import LexicalOutcome, PlattCalibration, lexical_scores, softmax
from .profiles import ProfileSet, build_profiles

__all__ = [
    "Segment",
    "classify",
    "classify_pages",
    "evidence_bits",
    "load_registry",
    "separation_of",
]

#: Evidence entries kept per tier so the audit trail stays readable.
_MAX_EVIDENCE_PER_TIER = 4


@dataclass(frozen=True)
class Segment:
    """A run of consecutive pages that classified the same way.

    Merged PDFs are the normal case in KYC: a customer uploads one file containing a passport,
    two utility bills and a bank statement. Classifying the bundle as a whole answers the
    wrong question.

    Attributes:
        doctype_id: The class of the run (``UNKNOWN`` for a run of abstentions).
        start_page: First page of the run (1-based, inclusive).
        end_page: Last page of the run (inclusive).
        confidence: Mean confidence across the run's pages.
        classification: The full classification of the run's first page, with
            ``page_types`` covering the run.
    """

    doctype_id: str
    start_page: int
    end_page: int
    confidence: float
    classification: Classification

    @property
    def page_count(self) -> int:
        """Number of pages in the run."""
        return self.end_page - self.start_page + 1


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------
def load_registry() -> list[DocTypeSpec]:
    """Load the doctype registry, tolerating its absence.

    The registry package is owned elsewhere in the service, so this reads it through its
    documented surface rather than its internals: ``all_specs()`` on the package or on
    ``dce.registry.loader``, or a ``SPECS``-like collection. Country packs register themselves
    at import, so an empty registry triggers one pass of importing the package's own modules
    before giving up — a service that silently classified against zero doctypes would abstain
    on everything and look like a model problem.

    Returns:
        The doctype specs, or ``[]`` when no registry is installed.
    """
    for module_name in ("dce.registry", "dce.registry.loader"):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - a registry that fails to import must degrade to
            # "no registry" (every document abstains to a human) rather than take the service
            # down. A pack with a syntax error is a deploy problem, not a request problem.
            continue
        specs = _specs_from(module)
        if specs:
            return specs
        if _import_registry_packs():
            specs = _specs_from(module)
            if specs:
                return specs
    return []


def _registry_module(name: str) -> Any | None:
    """Import a registry submodule under the same tolerance :func:`load_registry` uses.

    The classifier deliberately does not import :mod:`dce.registry` at module scope — a pack
    with a syntax error is a deploy problem, and it must degrade to "no registry, everything
    abstains" rather than take ``import dce.classify`` down with it. One consumer below needs
    registry *mechanism* rather than registry *data* (:mod:`dce.registry.loader`, for the
    contested-anchor-claim index), and it must not be the thing that reintroduces a hard
    dependency.

    ``sys.modules`` makes every call after the first a dict lookup, so this is not a per-request
    import.

    Args:
        name: Fully-qualified module name under ``dce.registry``.

    Returns:
        The module, or ``None`` when the registry package is not importable — in which case
        :func:`load_registry` has already returned no specs and every document abstains, so
        there is no decision for the missing control to have protected.
    """
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - see the docstring; identical policy to load_registry.
        return None


def _specs_from(module: Any) -> list[DocTypeSpec]:
    """Pull specs out of a registry module via its documented exports."""
    for name in ("all_specs", "load_registry", "specs", "doctypes"):
        candidate = getattr(module, name, None)
        if callable(candidate):
            try:
                return _as_specs(candidate())
            except Exception:  # noqa: BLE001 - this export did not behave like the documented
                # accessor; try the next shape rather than assuming the registry is broken.
                continue
    for name in ("SPECS", "DOCTYPES", "DOC_TYPES", "REGISTRY"):
        candidate = getattr(module, name, None)
        if candidate is not None:
            return _as_specs(candidate)
    return []


def _import_registry_packs() -> bool:
    """Import every module in ``dce.registry`` so the country packs self-register.

    Returns:
        Whether at least one pack module was imported.
    """
    try:
        package = importlib.import_module("dce.registry")
        paths = list(getattr(package, "__path__", ()))
    except Exception:  # noqa: BLE001 - no registry package at all is a valid state (see above).
        return False
    imported = False
    for info in pkgutil.iter_modules(paths):
        if info.name.startswith("_") or info.name == "loader":
            continue
        try:
            importlib.import_module(f"dce.registry.{info.name}")
            imported = True
        except Exception:  # noqa: BLE001 - one broken country pack must not cost us the other
            # twenty. The registry package owns its own validation and reporting.
            continue
    return imported


def _as_specs(value: Any) -> list[DocTypeSpec]:
    """Coerce a registry export (list or mapping) into a list of specs."""
    if isinstance(value, Mapping):
        value = list(value.values())
    return [item for item in value if isinstance(item, DocTypeSpec)]


# ---------------------------------------------------------------------------
# The cascade
# ---------------------------------------------------------------------------
def classify(
    view: LayoutView,
    specs: Iterable[DocTypeSpec] | None = None,
    *,
    settings: Settings | None = None,
    profiles: ProfileSet | None = None,
    calibration: PlattCalibration | None = None,
) -> Classification:
    """Classify one document, entirely in-process.

    Args:
        view: The layout payload.
        specs: Doctype registry; defaults to :func:`load_registry`.
        settings: Settings override; defaults to :func:`dce.config.get_settings`.
        profiles: Pre-built term profiles; defaults to profiles derived from ``specs``.
        calibration: Platt calibration for the lexical tier; defaults to the identity.

    Returns:
        A fully populated :class:`~dce.models.Classification` — confidence, margin, coverage,
        evidence, runners-up and elapsed milliseconds — with ``abstained`` set and a
        human-readable ``reason`` when it declines.
    """
    started = time.perf_counter()
    resolved = settings if settings is not None else get_settings()
    spec_list = list(specs) if specs is not None else load_registry()

    # Everything below runs inside the scope, so any code that reaches for the network —
    # now or after somebody's future refactor — trips dce.egress.assert_no_egress.
    with classification_scope():
        if not spec_list:
            return _abstain(
                reason="no doctype registry is loaded; nothing to classify against",
                evidence=[],
                runners_up=[],
                started=started,
            )

        # Term profiles are built from the whole registry, always. An idf is a property of the
        # corpus of class profiles, so deriving one from a per-document subset would make the
        # lexical channel depend on which doctypes this particular document happened to remove
        # — exactly the registry-dependence the accept rule was rewritten to eliminate.
        #
        # There is no per-document candidate filtering left, and that is deliberate. An MRZ
        # issuing-State veto used to sit on this line and was removed: it read the whole
        # payload, and a payload here is a KYC *packet*, so one page's foreign MRZ deleted
        # every other jurisdiction's doctypes for the whole packet. Measured on this corpus,
        # appending a single foreign MRZ line took 36 correct answers to 0 and 1 wrong answer
        # to 14 — including a Canadian document returned as ``us_green_card``, the exact defect
        # the veto was built to prevent. The measurement and the two regressions are in
        # ``tests/test_registry_jurisdiction.py``. Nothing payload-wide may filter candidates
        # here again without per-document segmentation to scope it to.
        registry_specs = spec_list

        features = structural_tier.structural_features(view)
        priors = structural_tier.structural_log_priors(features, spec_list)
        anchor = anchors_tier.anchor_scores(view, spec_list, settings=resolved)
        conclusive, contested_by, shared_claims = _conclusive_l1(anchor, spec_list)

        resolved_profiles = profiles or build_profiles(registry_specs)
        lexical = lexical_scores(
            view, resolved_profiles, settings=resolved, calibration=calibration
        )
        bert = _bert_scores(view, resolved)

        fused = {
            spec.doctype_id: (
                priors.get(spec.doctype_id, 0.0)
                + resolved.fuse_weight_anchor * anchor.scores.get(spec.doctype_id, 0.0)
                + resolved.fuse_weight_lexical * lexical.probability.get(spec.doctype_id, 0.0)
                + resolved.fuse_weight_bert * bert.get(spec.doctype_id, 0.0)
            )
            for spec in spec_list
        }
        # Kept for the audit trail and the runners-up list. NOT read by the accept rule —
        # see the module docstring for why an absolute floor on this quantity is unfixable.
        probabilities = softmax(fused, resolved.softmax_temperature)
        ranked = sorted(probabilities.items(), key=lambda kv: (-kv[1], kv[0]))
        fused_top = ranked[0][0]

        verdict = _verdict(
            anchor,
            lexical,
            spec_list,
            conclusive=conclusive,
            settings=resolved,
            unpromoted=_unpromoted_reading(
                view, spec_list, resolved_profiles, settings=resolved,
                calibration=calibration,
            ),
        )
        # The doctype the audit trail is about: the support leader when there is one,
        # otherwise the fused leader, so a reviewer reading a refusal still sees the thing
        # that came closest.
        candidate = verdict.candidate or fused_top

        evidence = _evidence(
            features=features,
            priors=priors,
            anchor=anchor,
            lexical=lexical,
            bert=bert,
            doctype_id=candidate,
        )
        if contested_by:
            evidence.append(
                Evidence(
                    tier="anchor",
                    detail=(
                        "the L1 conclusive-evidence route was suppressed: "
                        f"{', '.join(contested_by)} declare(s) a confusable decisive anchor "
                        "that is present in this document but could not be evaluated on this "
                        "payload's zones, so the uniqueness claim was not established"
                    ),
                    weight=0.0,
                )
            )
        if shared_claims:
            evidence.append(
                Evidence(
                    tier="anchor",
                    detail=(
                        "the L1 conclusive-evidence route was suppressed: every decisive "
                        "anchor matched here is a string the registry says another doctype "
                        "also prints ("
                        + "; ".join(
                            f"{text!r} also claimed by {', '.join(peers)}"
                            for text, peers in shared_claims
                        )
                        + "), so being the only doctype heard saying it is a fact about this "
                        "payload, not about the document"
                    ),
                    weight=0.0,
                )
            )
        evidence.append(verdict.describe())
        runners_up = [(doctype, round(p, 6)) for doctype, p in ranked if doctype != candidate][
            :3
        ]

        if verdict.failures:
            return _abstain(
                reason=(
                    f"best candidate {candidate!r} at lead={verdict.lead:.3f}, "
                    f"support={verdict.support:.3f}, coverage={verdict.coverage:.3f} — "
                    + "; ".join(verdict.failures)
                    + ". Routed to human review; never auto-forwarded to a model."
                ),
                evidence=evidence,
                runners_up=[
                    (candidate, round(probabilities.get(candidate, 0.0), 6)),
                    *runners_up[:2],
                ],
                started=started,
                confidence=verdict.confidence,
                margin=verdict.lead,
                coverage=verdict.coverage,
            )

        spec = _spec_by_id(spec_list, candidate)
        return Classification(
            doctype_id=candidate,
            label=spec.label if spec else "",
            country=spec.country if spec else "",
            confidence=round(verdict.confidence, 6),
            margin=round(verdict.lead, 6),
            coverage=round(verdict.coverage, 6),
            abstained=False,
            evidence=evidence,
            runners_up=runners_up,
            page_types=[candidate],
            ms=_elapsed_ms(started),
        )


def classify_pages(
    view: LayoutView,
    specs: Iterable[DocTypeSpec] | None = None,
    *,
    settings: Settings | None = None,
    profiles: ProfileSet | None = None,
) -> list[Segment]:
    """Classify each page of a merged PDF and aggregate the pages into segments.

    Args:
        view: The layout payload for the whole bundle.
        specs: Doctype registry; defaults to :func:`load_registry`.
        settings: Settings override.
        profiles: Pre-built term profiles, built once and shared across pages.

    Returns:
        Run-length aggregated :class:`Segment` objects in page order. A single-document
        payload yields exactly one segment, so callers do not need a special case.
    """
    resolved = settings if settings is not None else get_settings()
    spec_list = list(specs) if specs is not None else load_registry()
    resolved_profiles = profiles or (build_profiles(spec_list) if spec_list else None)

    page_numbers = _page_numbers(view)
    if not page_numbers:
        return []

    per_page: list[tuple[int, Classification]] = []
    for page in page_numbers:
        page_view = _page_view(view, page)
        result = classify(
            page_view, spec_list, settings=resolved, profiles=resolved_profiles
        )
        per_page.append((page, result))

    segments: list[Segment] = []
    run_start = 0
    for i in range(1, len(per_page) + 1):
        ends_run = i == len(per_page) or (
            per_page[i][1].doctype_id != per_page[run_start][1].doctype_id
        )
        if not ends_run:
            continue
        members = per_page[run_start:i]
        head = members[0][1].model_copy(deep=True)
        head.page_types = [c.doctype_id for _, c in members]
        confidence = sum(c.confidence for _, c in members) / len(members)
        head.confidence = round(confidence, 6)
        segments.append(
            Segment(
                doctype_id=head.doctype_id,
                start_page=members[0][0],
                end_page=members[-1][0],
                confidence=round(confidence, 6),
                classification=head,
            )
        )
        run_start = i
    return segments


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _contested_decisive_claims(
    doctype_id: str,
    anchor: anchors_tier.AnchorOutcome,
    spec_list: Sequence[DocTypeSpec],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Decisive anchors matched for ``doctype_id`` that another doctype also declares.

    A decisive anchor is defined (:class:`dce.models.Anchor`) as near-proof of the doctype —
    a string that "appears on one document type and nowhere else". When two doctypes declare
    the same string, the registry has said in its own data that the premise is false, and the
    decisiveness of one of them is a bookkeeping accident rather than a fact.

    The measured consequence of not checking this: ``PERMANENT RESIDENT CARD`` was decisive for
    ``us_green_card`` and non-decisive for ``ca_pr_card``, whose own decisive anchors are the
    French ``CARTE DE RÉSIDENT PERMANENT`` / ``RÉSIDENT PERMANENT``. Lose the French line to
    OCR — routine on a bilingual card — and exactly one doctype held a decisive anchor, so
    ``us_green_card`` satisfied the identification gate and a Canadian permanent resident was
    classified into US immigration status. Being *the only doctype heard* saying a string that
    two doctypes *print* is a fact about the payload, not about the document.

    **What this rule does NOT do, stated plainly, because assuming otherwise would make it a
    silent partial fix.** It guards the *conclusive-L1* route only. It does not guard the
    *concurrence* route, which reads ``anchor.scores`` — and ``decisive=True`` is worth a 2.0
    multiplier in that score. Measured: with ``PERMANENT RESIDENT CARD`` decisive for
    ``us_green_card``, the overlap declared in ``confusable_with`` in both directions, and this
    rule active, ``corpus/ca/ca_pr_card.pdf`` still classified ``us_green_card`` at 0.545 —
    suppressed on the near-proof route, accepted on concurrence. A *cross-jurisdiction* shared
    claim therefore has to leave the registry outright, and
    :func:`dce.registry.loader._check_decisive_asymmetry` rejects one at import rather than
    letting a pack author declare their way past it.

    What this rule is for is the *same-jurisdiction* case, which is legitimate and must keep
    working: two documents of one issuer's family — a card and its masked reprint — genuinely
    share the issuing authority's header, and the correct handling is exactly this — decline
    the near-proof route and let the two-channel rule arbitrate on the full evidence. It is
    also the backstop for a future overlap nobody noticed, since it derives from the registry
    itself rather than from a maintained list.

    Args:
        doctype_id: The doctype whose decisive hits are being audited.
        anchor: L1's outcome.
        spec_list: The registry (or the subset being classified against).

    Returns:
        ``((anchor_text, other_claimants), ...)`` for every decisive hit whose string is
        claimed by another doctype, empty when at least one decisive hit is exclusive. Empty
        is the "this doctype may still be conclusive" answer, so a doctype with one exclusive
        decisive anchor and three shared ones keeps its route — the exclusive one is real
        proof and the others are simply not needed.
    """
    hits = [hit for hit in anchor.hits.get(doctype_id, ()) if hit.decisive]
    if not hits:
        return ()
    loader = _registry_module("dce.registry.loader")
    if loader is None:  # pragma: no cover - registry absent means no specs and no decision
        return ()
    contested = loader.contested_claims(spec_list)
    shared: list[tuple[str, tuple[str, ...]]] = []
    for hit in hits:
        peers = contested.get(loader.anchor_claim_key(hit.text))
        if peers is None:
            return ()  # an exclusively-claimed decisive anchor: the route stands
        shared.append((hit.text, tuple(sorted(peers - {doctype_id}))))
    return tuple(shared)


def _conclusive_l1(
    anchor: anchors_tier.AnchorOutcome,
    spec_list: Sequence[DocTypeSpec],
) -> tuple[str | None, tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...]]:
    """The one doctype holding near-proof L1 evidence, if there is exactly one.

    Two kinds of L1 evidence are near-proof of a *document type* rather than of a *number*:

    * a **decisive anchor** — an issuing-authority header or a form number that appears on one
      document type and nowhere else ("UNIQUE IDENTIFICATION AUTHORITY OF INDIA", "Request for
      Taxpayer Identification Number", "INSTITUTO NACIONAL ELECTORAL");
    * a **corroborated checksum-verified identifier** — a published check digit that agrees, on
      a number the document itself labels (or on a doctype that also matched one of its own
      anchors). The corroboration requirement is not decoration: a Canadian SIN is nine digits
      with a Luhn check, roughly one in ten arbitrary nine-digit strings passes Luhn, and
      ordinary bank statements and tax forms are full of nine-digit strings. See
      :class:`~dce.classify.anchors.ChecksumHit`.

    **This is an identification route, not an accept.** It satisfies gate 1 of the accept rule
    and nothing else: the candidate must still lead the combined support channel by the margin
    floor, clear the support floor and clear the coverage floor, exactly like a candidate that
    got there by channel concurrence. That is the change this function's predecessors —
    ``_short_circuit``, ``_decisive_short_circuit`` and ``_accept_short_circuit`` — existed to
    avoid, and avoiding it is what let a Canadian permanent-resident card be accepted as a US
    green card at a hard-coded confidence of 0.90 with anchor coverage of 0.167.

    Why keep the route at all, rather than requiring concurrence of everything. Concurrence
    asks the lexical tier to hold evidence for the class and to prefer it to the runner-up, and
    a photo ID carries almost no text: a permanent-resident card whose document number has one
    OCR-damaged character has the issuing authority's header and little else, and there is no
    term profile that can beat a chatty utility bill's on such a page — the tier may well score
    it at zero, which the positivity half of concurrence reads as "this tier has nothing to
    say", correctly.
    Deleting the route entirely was measured on the reference corpus by
    stubbing this function to return nothing: it costs 3 of 37 accepted answers (36 correct
    down to 33) and changes the wrong-answer count not at all. The document's issuer printed a
    string that means "this is a permanent-resident card"; refusing to read it because BM25 is
    unimpressed is not conservatism, it is discarding the strongest evidence on the page.

    **The audibility guard.** A decisive anchor is a claim of uniqueness *in the world*.
    ``len(decisive_doctypes()) == 1`` proves only uniqueness in the registry's bookkeeping, and
    only among the claims this payload was able to evaluate at all. A decisive anchor gated to
    a zone the document does not have is not absent — it is **unmeasurable**, and unmeasurable
    evidence must never be read as evidence of absence.

    That distinction is not theoretical. A US driver-licence calibration sheet classified as a
    *Canadian* driver's licence because: the sheet prints both spellings ("Driver's License"
    and, further down, "Driver's Licence"); the US doctype gates both of its decisive anchors
    to ``zone=title``; the Canadian doctype gates neither; and a text-layer PDF has no title
    zone. The US claim was inaudible, the Canadian claim was heard, and the count came to one.

    So the contesting set is the strict conclusive set *plus*, for any doctype in the
    candidate's ``confusable_with`` cluster in either direction, any decisive anchor that this
    payload could not evaluate but whose text is present
    (:attr:`~dce.classify.anchors.AnchorOutcome.muted_decisive`). If that set is not exactly
    the candidate, there is no conclusive owner.

    The guard is scale-invariant by construction: it reads no probability, no registry size and
    no tuned constant — only a per-payload zone-availability fact and a declared cluster
    membership. A 122nd doctype cannot change it unless that doctype joins the cluster, and in
    that case it *should*.

    Args:
        anchor: L1's outcome.
        spec_list: The registry (or the subset being classified against).

    Returns:
        ``(doctype_id, contested_by)``. The id is ``None`` when zero or several doctypes hold
        near-proof evidence, or when a confusable peer's decisive claim was muted.
        ``contested_by`` names the muted peers, for the audit trail, and is empty otherwise.
        ``shared_claims`` names the decisive anchors that were disqualified for being claimed
        by more than one doctype, with their other claimants — also for the audit trail, and
        also empty in every other case.
    """
    # A decisive anchor whose *string* another doctype also declares is not decisive, whatever
    # the pack that declared it says: the registry has admitted in its own data that the string
    # does not pick out a document type. See _contested_decisive_claims for the measured
    # failure. A doctype is dropped from the decisive owners only when EVERY decisive hit it
    # has is shared — one exclusive decisive anchor is still proof.
    disqualified: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {}
    decisive_owners: set[str] = set()
    for doctype in anchor.decisive_doctypes():
        shared = _contested_decisive_claims(doctype, anchor, spec_list)
        if shared:
            disqualified[doctype] = shared
        else:
            decisive_owners.add(doctype)

    owners = decisive_owners | set(anchor.verified_doctypes())
    if len(owners) != 1:
        # Report the disqualification only when it is what cost us the route — i.e. exactly
        # one doctype would have owned it. Otherwise the evidence line would blame a shared
        # anchor for an ambiguity that several doctypes caused anyway.
        would_have_owned = decisive_owners | set(disqualified) | set(anchor.verified_doctypes())
        if not owners and len(would_have_owned) == 1:
            only = next(iter(would_have_owned))
            return (None, (), disqualified.get(only, ()))
        return (None, (), ())
    candidate = next(iter(owners))
    spec = _spec_by_id(spec_list, candidate)
    if spec is None:
        return (None, (), ())
    contested_by = _muted_contenders(candidate, spec, anchor, spec_list)
    if contested_by:
        return (None, contested_by, ())
    return (candidate, (), ())


def _muted_contenders(
    candidate: str,
    spec: DocTypeSpec,
    anchor: anchors_tier.AnchorOutcome,
    spec_list: Sequence[DocTypeSpec],
) -> tuple[str, ...]:
    """Confusable peers of ``candidate`` whose decisive claim this payload could not hear.

    The cluster is read in both directions: ``confusable_with`` is a declaration by one author
    about one doctype, and an author who declares "my doctype looks like theirs" has told us
    just as much as one who declares the reverse. Only the *declared* cluster is consulted — an
    arbitrary doctype whose decisive anchor happened to be muted is not a contender for this
    document, and treating it as one would abstain on everything.
    """
    cluster = set(spec.confusable_with or {})
    cluster.update(
        other.doctype_id
        for other in spec_list
        if candidate in (other.confusable_with or {})
    )
    return tuple(
        sorted(peer for peer in cluster if peer != candidate and anchor.muted_decisive.get(peer))
    )


def _bert_scores(view: LayoutView, settings: Settings) -> dict[str, float]:
    """Run L3 if and only if it is enabled and usable.

    The import lives inside this function on purpose: :mod:`dce.classify.bert_knn` pulls in
    ``transformers``, and a container running with ``bert_enabled=false`` must never pay for
    it — nor even have it in ``sys.modules``.
    """
    if not settings.bert_enabled:
        return {}
    try:
        from . import bert_knn

        if not bert_knn.tier_available(settings):
            return {}
        return bert_knn.bert_scores(view, settings=settings)
    except EgressViolation:
        # Never swallowed. If the optional tier tried to leave the process, that is the one
        # failure this service must not paper over.
        raise
    except Exception:  # noqa: BLE001 - a missing checkpoint or an unreadable exemplar file
        # degrades the cascade to L0-L2. It must not fail the request: L1+L2 are the tiers
        # this service is designed around, and L3 is explicitly optional.
        return {}


@dataclass(frozen=True)
class _Verdict:
    """The accept decision, and every number that produced it.

    Attributes:
        candidate: The doctype leading the combined evidence channel, or ``None`` when no
            doctype carries any evidence at all.
        route: How gate 1 (identification) was satisfied — ``"concurrence"`` when both the
            anchor and the lexical channel independently preferred the candidate to the
            runner-up, ``"conclusive-l1"`` when exactly one doctype held decisive-anchor or
            corroborated-checksum evidence, ``""`` when neither did, or when gate 1b (the
            zone-free leader) refused, and the accept was refused on identification.
        runner_up: The second doctype on the combined channel — the rival gate 2 measures the
            candidate against, and the one gate 1 asks the two channels about. Empty only when
            the registry holds a single doctype.
        anchor_winner: The doctype leading the anchor channel.
        explained_winner: The doctype leading the lexical explained-mass channel.
        unpromoted_leader: The doctype leading the combined channel once every paragraph role
            is discarded; ``""`` when the payload carried no zone labels and gate 1b was
            vacuous.
        anchor_lead: ``anchor_bits[1] - anchor_bits[2]``, for the audit trail only.
        explained_lead: ``L[1] - L[2]``, for the audit trail only.
        lead_bits: ``B[1] - B[2]`` — the raw separation in bits, before it is mapped onto the
            margin floor's scale. On the record because it is the quantity that composes
            additively and the one a reviewer can reason about ("two bits" = "four times as
            well supported").
        lead: ``1 - 2^-(B[1] - B[2])`` — the reported margin, and the only lead the rule
            reads. Bounded in ``[0, 1)`` on the same scale ``classify_min_margin`` has always
            been expressed on, and never negative on an accepted answer.
        support: ``S_d = 1 - (1 - A_d)(1 - L_d)`` for the candidate — the *absolute* strength
            of its evidence, which is what the null-hypothesis floor is about. Kept on the
            saturating scale deliberately: a ceiling on how certain one tier may claim to be
            is the right shape for an absolute floor and the wrong shape for a comparison.
        coverage: ``max(profile coverage, anchor coverage)`` for the candidate.
        confidence: Distance to the binding constraint; see :func:`_verdict`. ``>= 0.5``
            exactly on the accepted answers.
        considered: The doctypes that were actually in contention, for the audit record.
        failures: Human-readable reasons acceptance was refused. Empty means accept.
    """

    candidate: str | None
    route: str
    runner_up: str
    anchor_winner: str
    explained_winner: str
    unpromoted_leader: str
    anchor_lead: float
    explained_lead: float
    lead_bits: float
    lead: float
    support: float
    coverage: float
    confidence: float
    considered: tuple[str, ...]
    failures: tuple[str, ...]

    def describe(self) -> Evidence:
        """The 'candidates considered' line an auditor asks for.

        Records *who was in contention, how the winner was identified, and which gate blocked
        the accept* — not just the winner's score. A decision record that names only the
        winner cannot be reviewed.
        """
        contenders = ", ".join(self.considered) or "none"
        return Evidence(
            tier="fusion",
            detail=(
                f"route={self.route or 'none'}; evidence leader "
                f"{self.candidate or 'none'!r} at S={self.support:.3f}, "
                f"separation {self.lead:.3f} ({self.lead_bits:.3f} bits) over "
                f"{self.runner_up or 'no second doctype'!r}; zone-free leader "
                f"{self.unpromoted_leader or 'n/a (payload carries no zone labels)'!r}; "
                f"coverage={self.coverage:.3f}; component channels: anchor leader "
                f"{self.anchor_winner or 'none'!r} (lead {self.anchor_lead:.3f} bits), "
                f"explained leader {self.explained_winner or 'none'!r} "
                f"(lead {self.explained_lead:.3f}); candidates considered: {contenders}"
            ),
            weight=round(self.confidence, 4),
        )


#: ``explained`` is published rounded to six decimals, so its residual ``1 - L`` cannot be
#: resolved below 1e-6. Clamping there before taking a logarithm is a numerical guard on a
#: quantity that is *already* discretised, not a threshold: no decision changes at any value
#: below the rounding grid, and nothing is tuned by moving it.
_MIN_RESIDUAL = 1e-6


def evidence_bits(anchor_bits: float, explained: float) -> float:
    """Total evidence for one doctype, in bits — the additive form of the support channel.

    The cascade's support is a noisy-OR of the two tiers, ``S = 1 - (1 - A)(1 - L)``. Take
    ``-log2`` of the residual and the product becomes a sum::

        B = -log2(1 - S) = -log2(1 - A) + -log2(1 - L) = anchor_bits + lexical_bits

    which is the same quantity in the units the anchor tier already scores in: ``A`` is defined
    as ``1 - 2^-anchor_bits``, so ``-log2(1 - A)`` *is* the anchor tier's raw score, recovered
    exactly — except that :attr:`~dce.classify.anchors.AnchorOutcome.bits` is the unclipped
    value, and ``A`` is clipped at 0.97. That is why this reads ``anchor.bits`` rather than
    inverting ``anchor.scores``.

    ``S`` and ``B`` are monotone transforms of one another, so they rank identically. They do
    not *subtract* identically, and the accept rule subtracts.

    Args:
        anchor_bits: :attr:`~dce.classify.anchors.AnchorOutcome.bits` for the doctype.
        explained: :attr:`~dce.classify.lexical.LexicalOutcome.explained` for the doctype.

    Returns:
        Evidence in bits, ``>= 0``.
    """
    residual = max(1.0 - max(0.0, min(1.0, explained)), _MIN_RESIDUAL)
    return max(0.0, anchor_bits) + -math.log2(residual)


def separation_of(lead_bits: float) -> float:
    """Map a bits lead between two doctypes onto the ``[0, 1)`` scale the margin floor is on.

    ``sep = 1 - 2^-lead_bits`` — the anchor tier's own squash, applied to the *difference*
    between the winner and the runner-up rather than to either one's absolute evidence. Read
    it as "the fraction of the runner-up's residual doubt that the winner's extra evidence
    removes", which is the likelihood ratio between the two, bounded.

    Equivalently, and this is the form worth checking against the previous rule::

        sep = 1 - (1 - S[1]) / (1 - S[2])

    where the old rule read ``S[1] - S[2]``. The two agree to first order where nothing is
    saturated (both are ``ln2 * lead_bits`` as ``S -> 0``), which is why
    ``classify_min_margin`` keeps its value and its meaning. They diverge exactly where the old
    one was broken: as ``S[1]`` and ``S[2]`` both approach 1, the *difference* is squeezed
    toward zero by the ceiling while the *ratio* is not, so evidence that strengthens both the
    winner and the rival used to shrink the measured separation and now cannot.

    Negative leads are clamped to 0.0. A negative separation on an accepted answer was one of
    the defects the previous rewrite removed; the clamp keeps it removed for a runner-up that
    is ahead, in which case gate 2 refuses anyway.

    Args:
        lead_bits: ``B[1] - B[2]``.

    Returns:
        Separation in ``[0, 1)``.
    """
    if lead_bits <= 0.0:
        return 0.0
    return 1.0 - 2.0**-lead_bits


@dataclass(frozen=True)
class _Unpromoted:
    """The same two tiers, re-run on a payload whose paragraph roles have been discarded.

    Every quantity the accept rule reads has a member here, and the rule reads **all** of them
    from here when a payload carries roles — not a mixture of readings. A mixture is not merely
    untidy: ``explained`` divides by a zone-weighted document length, so promoting one line
    into the title lowers the BM25 saturation of every *other* term slightly, and a support
    taken from the zone-weighted reading can therefore fall when evidence is added. Measured on
    the fixture in ``tests/test_classify.py::sibling_form_pair``: 0.767857 -> 0.767822 on the
    reported confidence, from promoting the document's own masthead. Small, and a monotonicity
    violation all the same — this class of defect is precisely what round 3 was opened for.

    Attributes:
        bits: ``doctype_id -> `` combined evidence in bits, zone-free.
        anchor_bits: L1's raw bits, zone-free — the anchor channel with every hit credited at
            body weight and every zone restriction ignored (see
            :func:`dce.classify.anchors.anchor_scores`'s ``zone_blind``).
        explained: L2's explained mass, zone-free.
        supports: The noisy-OR ``1 - (1 - A)(1 - L)`` per doctype, zone-free — the quantity
            ``classify_min_support`` reads.
        coverage: ``max(profile coverage, anchor coverage)`` per doctype, zone-free — the
            quantity ``classify_min_coverage`` reads.
    """

    bits: Mapping[str, float]
    anchor_bits: Mapping[str, float]
    explained: Mapping[str, float]
    supports: Mapping[str, float]
    coverage: Mapping[str, float]


def _unpromoted_view(view: LayoutView, settings: Settings) -> LayoutView:
    """The same payload with every *promoting* paragraph role discarded.

    Same words, same tables, same key/value pairs. Every block whose zone is configured to
    weigh **more** than ``body`` — ``title`` at 3.0x, ``heading`` at 2.0x — is read as ``body``
    instead. Zones that weigh **less** are left alone.

    The asymmetry is the whole point and it is one-directional on purpose. The rule this
    supports is "a layout label may not manufacture evidence", not "layout labels are
    ignored":

    * A promoting label is how a caption, a watermark or a line of marketing copy that Azure
      Document Intelligence called a ``title`` turns into four bits of evidence for a doctype
      the page has nothing to do with. That has to be unable to change a verdict.
    * ``furniture`` at 0.25x is the opposite: it is the control that stops a term repeated in
      every page footer being read as evidence, and it can only ever *lower* a doctype's
      score. A wrong ``furniture`` label can cost an accept — an abstention, which routes to a
      human — and can never buy a wrong one. Flattening it away would have removed a working
      control to buy nothing: measured, it takes the fixture in
      ``tests/test_classify.py::test_support_floor_is_the_only_gate_that_can_refuse_thin_evidence``
      from support 0.217 (refused) to 0.401 (accepted), which is a one-anchor doctype matched
      in a page footer being accepted.

    Read weights from settings rather than naming the zones, so a deployment that re-weights
    them gets the rule it configured rather than the rule that was true when this was written.
    """
    body = settings.zone_weight_body
    weights = {
        Zone.title: settings.zone_weight_title,
        Zone.heading: settings.zone_weight_heading,
        Zone.body: body,
        Zone.table: settings.zone_weight_table,
        Zone.furniture: settings.zone_weight_furniture,
    }
    stripped = view.model_copy(deep=True)
    for block in stripped.blocks:
        if weights.get(block.zone, body) > body:
            block.zone = Zone.body
    return stripped


def _has_promoted_zone(view: LayoutView, settings: Settings) -> bool:
    """Whether any block carries a zone that weighs more than ``body``."""
    body = settings.zone_weight_body
    weights = {
        Zone.title: settings.zone_weight_title,
        Zone.heading: settings.zone_weight_heading,
        Zone.table: settings.zone_weight_table,
        Zone.furniture: settings.zone_weight_furniture,
    }
    return any(weights.get(block.zone, body) > body for block in view.blocks)


def _unpromoted_settings(settings: Settings) -> Settings:
    """``settings`` with every promoting zone weight levelled down to ``body``.

    A zone label carries two separable things, and the zone-free reading must discard exactly
    one of them:

    * a **weight** — ``title`` is worth 3.0x — which is the layout provider's *opinion* about
      the page, and the thing a mislabelled caption uses to manufacture evidence. Discarded.
    * a **restriction** — ``anchor.zone = title`` — which is the registry's *claim about the
      document*: this phrase only means what it says when it appears as a masthead.
      ``SOCIAL SECURITY ADMINISTRATION`` identifies an SSN card printed across the top of one;
      in running prose it is a sentence about a US agency, and a Canadian record of landing is
      entitled to mention one. Kept.

    Levelling the weights here rather than reading the flattened view is what keeps those two
    apart. The previous form evaluated the flattened view with ``zone_blind=True``, which was
    forced: flattening rewrites every promoted block to ``body``, so a title-gated anchor finds
    no title left to match and vanishes. But ``zone_blind`` does not restore those anchors to
    their gated position — it un-gates them everywhere, making all 21 title-gated decisive
    anchors audible in ordinary body text. Measured on the tree that shipped it:
    ``corpus/ca/ca_copr.pdf`` plus one body line reading "Benefits paid by the SOCIAL SECURITY
    ADMINISTRATION are not reportable here" classified as ``ca_copr`` (CA, correct) with no
    roles, and as ``us_ssn_card`` (US, 0.667, confidently wrong) once the masthead was labelled
    ``title`` — a *correct* label, on a different line, buying a cross-jurisdiction identity
    determination.

    Evaluating the original view under levelled weights honours the restriction against the
    zones the provider actually reported while denying the label its multiplier, so the guard
    keeps the property it was built for — a label may sharpen a decision, it may not make one —
    without the hole.

    Only weights **above** ``body`` are levelled. ``furniture`` at 0.25x is left alone for the
    reason given in :func:`_unpromoted_view`: it can only lower a score, so it can cost an
    accept and never buy a wrong answer.
    """
    body = settings.zone_weight_body
    levelled = {
        name: body
        for name in ("zone_weight_title", "zone_weight_heading", "zone_weight_table")
        if getattr(settings, name) > body
    }
    return settings.model_copy(update=levelled) if levelled else settings


def _unpromoted_reading(
    view: LayoutView,
    spec_list: Sequence[DocTypeSpec],
    profiles: ProfileSet,
    *,
    settings: Settings,
    calibration: PlattCalibration | None = None,
) -> _Unpromoted | None:
    """Evidence in bits per doctype with every zone label discarded.

    **What this is for.** A zone label is the layout provider's opinion, not the document's
    content, and it is an opinion Azure Document Intelligence gets wrong in a specific,
    routine way: it assigns the ``title`` role to captions, watermarks, letterhead and
    marketing copy. A title is worth a 3.0x lexical weight and a 2.0x anchor weight, and it is
    the only zone in which 21 of the registry's decisive anchors are audible at all, so one
    mislabelled line is worth up to four bits of manufactured evidence for a doctype the
    document has nothing to do with.

    Measured, before this guard existed, by injecting one extra line carrying another
    doctype's decisive-anchor text into eight documents the service classified correctly, once
    labelled ``title`` and once labelled ``body`` — same text, same position, only the label
    differing: **23 confident wrong answers with the title label, 3 with the body label**. The
    label alone bought 20 of the 23, a 7.7x multiplier, and every one of them cleared the
    accept boundary.

    **The rule this supports.** Zone labels may *sharpen* a decision; they may not *make* one.
    The accepted doctype must also lead the combined evidence channel on the zone-free reading
    of the same payload. A lie about layout can then only ever increase the separation of the
    doctype the raw text already leads with — it can no longer choose the answer. That is a
    stronger guarantee than "robust to one bad label" and a cheaper one to state: it holds for
    any number of bad labels, in any combination, without the rule needing to know how many.

    It costs one extra pass of L1 and L2 and it is skipped entirely when the payload carries no
    zones to discard, which is every plain-text request. Ranking is on bits rather than on the
    saturating support channel for the same reason gate 2 is; see :func:`evidence_bits`.

    Args:
        view: The layout payload.
        spec_list: The registry (or the subset being classified against).
        profiles: The term profiles, built from the unfiltered registry.
        settings: Resolved settings.
        calibration: Platt calibration, passed through so the second pass is the same
            computation as the first.

    Returns:
        The zone-free reading, or ``None`` when the payload has no non-body block zones and
        the zone-free reading is the reading we already have.
    """
    if not _has_promoted_zone(view, settings):
        return None
    flat = _unpromoted_view(view, settings)
    flat_anchor = anchors_tier.anchor_scores(
        view, spec_list, settings=_unpromoted_settings(settings)
    )
    flat_lexical = lexical_scores(
        flat, profiles, settings=settings, calibration=calibration
    )
    return _Unpromoted(
        bits={
            spec.doctype_id: evidence_bits(
                flat_anchor.bits.get(spec.doctype_id, 0.0),
                flat_lexical.explained.get(spec.doctype_id, 0.0),
            )
            for spec in spec_list
        },
        anchor_bits=flat_anchor.bits,
        explained=flat_lexical.explained,
        supports=_supports(flat_anchor.scores, flat_lexical.explained, spec_list),
        coverage={
            spec.doctype_id: max(
                flat_lexical.coverage.get(spec.doctype_id, 0.0),
                flat_anchor.coverage.get(spec.doctype_id, 0.0),
            )
            for spec in spec_list
        },
    )


def _supports(
    anchor_scores: Mapping[str, float],
    explained: Mapping[str, float],
    spec_list: Sequence[DocTypeSpec],
) -> dict[str, float]:
    """The noisy-OR ``1 - (1 - A)(1 - L)`` per doctype — the absolute-strength channel.

    Deliberately built on the *clipped* anchor score rather than on bits: this quantity is read
    only by ``classify_min_support``, an absolute floor asking "is this doctype supported at
    all", and the 0.97 ceiling is a cap on what L1 may claim on its own, which is the right
    shape for that question. It is the wrong shape for a *comparison*, which is why the
    comparison gates read :func:`evidence_bits` instead.
    """
    return {
        spec.doctype_id: 1.0
        - (1.0 - anchor_scores.get(spec.doctype_id, 0.0))
        * (1.0 - explained.get(spec.doctype_id, 0.0))
        for spec in spec_list
    }


def _ranked_channel(
    scores: Mapping[str, float], spec_list: Sequence[DocTypeSpec]
) -> list[tuple[str, float]]:
    """Rank one evidence channel over the registry, descending, ties broken by doctype id.

    The tie-break is not cosmetic. Anchor scores saturate at 0.97 and genuinely tie often, and
    ranking a dict by value alone would then depend on registry *insertion order* — which makes
    the abstention reason string, and therefore the audit record, non-reproducible across two
    runs of the same document. The verdict itself is unaffected either way (a tie gives a lead
    of 0.0, which abstains), but "why did it refuse" must not change between deployments.

    Channels that saturate are ranked on their **unsaturated** form where one exists — the
    anchor channel is ranked on ``bits``, not on ``scores`` — because a clipped channel ties
    a great deal more often than the evidence does, and a tie here is broken alphabetically.
    """
    return sorted(
        ((spec.doctype_id, scores.get(spec.doctype_id, 0.0)) for spec in spec_list),
        key=lambda kv: (-kv[1], kv[0]),
    )


def _verdict(
    anchor: anchors_tier.AnchorOutcome,
    lexical: LexicalOutcome,
    spec_list: Sequence[DocTypeSpec],
    *,
    conclusive: str | None,
    settings: Settings,
    unpromoted: _Unpromoted | None = None,
) -> _Verdict:
    """Decide acceptance from one absolute channel and an explicit null hypothesis.

    The channel is the noisy-OR of the two evidence tiers, ``S_c = 1 - (1 - A_c)(1 - L_c)``,
    and the rule reads it in its **additive** form, ``B_c = -log2(1 - S_c) = anchor_bits +
    lexical_bits`` (:func:`evidence_bits`). Both inputs are per-doctype quantities computed
    from ``(document, spec_c)`` alone, so nothing here can move because an unrelated doctype
    was installed, and neither can their sum. Every gate is evaluated **unconditionally**,
    even the ones that are moot once an earlier gate has failed: a refusal reason that stops
    at the first problem tells a reviewer to fix one thing and re-submit into the next refusal.

    **Why bits, and not the difference of two supports.** ``S`` is bounded above by 1 and the
    anchor tier is clipped at 0.97 outright. Two doctypes both holding strong evidence sit
    near that ceiling, where the ceiling — not the evidence — sets how far apart they can be.
    The consequence is not a rounding nuisance, it is a reversal: *adding* evidence for the
    winner and the rival alike pushes both up the saturating curve and makes the measured
    separation **smaller**. Zone weighting does exactly that (a title multiplies both by 2.0),
    so the same corpus measured with production's zone labels scored 32 correct against 36
    with every block flattened to ``body`` — the configuration production can never run was
    the better one, and all five documents it cost were refused by the margin gate with the
    winner's support at 0.98. A rule whose refusals get *more* likely as the evidence gets
    stronger is not a conservative rule, it is an inverted one.

    In bits the same comparison is a likelihood ratio and composes additively: scaling the
    evidence for two candidates by the same zone multiplier scales their *difference* by that
    multiplier too, so more evidence can only ever widen a real gap.
    :func:`separation_of` maps the bits lead back onto the ``[0, 1)`` scale
    ``classify_min_margin`` has always been expressed on, agreeing with the old quantity to
    first order everywhere nothing is saturated — which is why the floor keeps both its value
    and its meaning, and is not being re-tuned under cover of a rewrite.

    Reading the margin off one combined channel rather than off the two component channels is
    what makes the number coherent. The predecessor of this function reported
    ``min(A[1]-A[2], L[1]-L[2])``, which is defensible, but the *other* accept path — the
    short-circuit — reported a hard-coded confidence minus the top anchor score, which is a
    subtraction across two scales and produced negative margins on accepted answers. One
    decision quantity, one subtraction, one scale removes that class of defect rather than
    patching its instances.

    **Gate 1: the two tiers must agree about the comparison the accept is making.** Not about
    the whole registry — about ``runner_up``, the doctype gate 2 measures the candidate against
    and the one it would be accepted over. Each tier must hold evidence for the candidate
    (``> 0``, the silent-tier guard stated per-candidate rather than per-registry) and must not
    prefer the runner-up to it. Since the candidate leads the combined channel and that channel
    is the sum of the two, at least one tier strictly prefers it, so two indifferent tiers
    cannot satisfy this between them.

    The predecessor asked instead for ``argmax(A) == argmax(L) == candidate`` over the entire
    registry, which made the two gates ask about different opponents and let a doctype that
    could never be accepted — no anchor evidence, far down the combined channel — veto by
    topping a channel. It was also insensitive to *how much* a dissenting tier dissented: on
    ``corpus/us/us_sec_20f.pdf`` a 0.017-bit lexical preference for an unrelated doctype
    refused an accept whose combined lead was 2.60 bits. See the module docstring's round 4 for
    the measurement, and for why this is a superset of the old test and therefore cannot
    withdraw an answer it used to find.

    **Gate 1b: zone labels may sharpen a decision, they may not make one.** The winner must
    also lead the same bits channel on the *zone-free* reading of the payload, where every
    paragraph role has been discarded and all text is read as ``body``
    (:func:`_unpromoted_reading`). Provider roles are an opinion about layout, and Azure Document
    Intelligence routinely calls a caption, a watermark or a line of marketing copy a
    ``title`` — worth a 3.0x lexical weight, a 2.0x anchor weight, and audibility for 21
    otherwise-inaudible decisive anchors. Measured: injecting one extra line carrying another
    doctype's decisive anchor into eight correctly-classified documents produced 23 confident
    wrong answers when the line was labelled ``title`` and 3 when the identical line was
    labelled ``body``. This gate is what makes those two numbers the same one.

    The reported confidence carries no free constant — every scale is an accept threshold
    itself::

        separation = lead  / (lead  + classify_min_margin)
        strength   = S_d   / (S_d   + classify_min_support)
        breadth    = cov_d / (cov_d + classify_min_coverage)
        confidence = min(separation, strength, breadth)   if identified else 0.0

    Each factor is 0.5 exactly at its own floor, so the ``min`` is ``>= 0.5`` if and only if
    every gate passed. **0.5 is the decision boundary**, every accept is at or above it and
    every abstention strictly below it, and the value names the control that came closest to
    blocking the accept. ``classify_min_support`` is the explicit null hypothesis — "none of
    the above" — which is what stops a lone candidate in a one-doctype registry being awarded
    certainty for having no opposition.

    Confidence is zero, not merely small, when identification fails. There is no candidate in
    that case: a tier prefers the doctype the accept would be made over, or has nothing to say
    about the candidate at all, and no L1 evidence is conclusive — so there is nothing to be
    confident *in*. How close it came is still on the record — in ``runners_up``, and in the
    refusal reason, which names the dissenting tier and the rival, and prints the lead, the
    support and the coverage.

    Args:
        anchor: L1's outcome.
        lexical: L2's outcome.
        spec_list: The registry (or the subset being classified against).
        conclusive: The doctype holding near-proof L1 evidence, from :func:`_conclusive_l1`,
            or ``None``.
        settings: Resolved settings.
        unpromoted: Evidence in bits per doctype on the zone-free payload, from
            :func:`_unpromoted_reading`; ``None`` when the payload carries no zone labels to
            discard, in which case gate 1b is vacuous and is recorded as such.

    Returns:
        The :class:`_Verdict`.
    """
    # THE DECIDING READING. When the payload carries paragraph roles, EVERY quantity the four
    # gates read is taken from the zone-free reading of that payload, and the zone-weighted one
    # is not consulted by the rule at all — see the docstring's gate 1b. A zone label is the
    # layout provider's opinion, and an opinion that can pick the answer can pick the wrong
    # one. All of them from one reading rather than a mixture: see :class:`_Unpromoted` for the
    # measured reason a mixture is not monotone either.
    #
    # This does NOT make zone labels decorative. A zone-RESTRICTED anchor is a registry claim
    # about the document ("this issuer prints this string as the title"), not a weight, and it
    # still establishes the conclusive-L1 route below; ``conclusive`` is computed from the
    # zone-weighted reading by the caller. So labels can raise a verdict and cannot lower one,
    # which is what makes ``--layout >= plain text`` a property of the rule rather than an
    # observation about 59 files.
    supports = _supports(anchor.scores, lexical.explained, spec_list)
    bits = {
        spec.doctype_id: evidence_bits(
            anchor.bits.get(spec.doctype_id, 0.0),
            lexical.explained.get(spec.doctype_id, 0.0),
        )
        for spec in spec_list
    }
    deciding_bits = unpromoted.bits if unpromoted is not None else bits
    deciding_anchor_bits = unpromoted.anchor_bits if unpromoted is not None else anchor.bits
    deciding_explained = unpromoted.explained if unpromoted is not None else lexical.explained
    deciding_supports = unpromoted.supports if unpromoted is not None else supports

    bits_ranked = _ranked_channel(deciding_bits, spec_list)
    anchor_ranked = _ranked_channel(deciding_anchor_bits, spec_list)
    explained_ranked = _ranked_channel(deciding_explained, spec_list)

    # Rank on bits, not on ``supports``. They are monotone transforms of one another and so
    # rank the same wherever the anchor tier is below its 0.97 clip; above it they do not, and
    # ``supports`` then ties every saturated doctype, leaving the winner to be picked
    # alphabetically by the tie-break in _ranked_channel.
    candidate, b1 = bits_ranked[0]
    runner_up, b2 = bits_ranked[1] if len(bits_ranked) > 1 else ("", 0.0)
    lead = separation_of(b1 - b2)

    # A channel whose top score is zero has no winner. ``_ranked_channel`` still names one,
    # because it breaks ties by doctype id so that the audit record is reproducible — but a
    # tie-break among zeros is bookkeeping, not an opinion, and reading it as one is how a
    # SILENT channel comes to "concur". That is not hypothetical. Install a single doctype and
    # the profile builder produces an empty profile for it (nothing can be surprisingly
    # frequent in a class relative to a background pooled from that same class), so every
    # ``explained`` is 0.0 — and a rule that compared argmaxes alone would find the lexical
    # tier agreeing with the anchor tier about the only doctype on the shelf. "This is the one
    # document type we know of, therefore it is this one" is the least-wrong-of-a-tiny-registry
    # failure the whole design exists to refuse.
    anchor_winner, a1 = anchor_ranked[0]
    explained_winner, l1 = explained_ranked[0]
    a2 = anchor_ranked[1][1] if len(anchor_ranked) > 1 else 0.0
    l2 = explained_ranked[1][1] if len(explained_ranked) > 1 else 0.0
    if a1 <= 0.0:
        anchor_winner = ""
    if l1 <= 0.0:
        explained_winner = ""

    # Gate 1. Concurrence asks whether both tiers agree that the candidate beats the doctype it
    # is being accepted OVER — ``runner_up``, the second doctype on the combined channel, which
    # is the rival gate 2 already measures it against. A tier concurs when it holds evidence
    # for the candidate and does not prefer that rival; the positivity half is the silent-tier
    # guard above restated per-candidate, which is the form that actually bites (a tier can be
    # loud about some other doctype and still have nothing to say about this one). At least one
    # tier must strictly prefer the candidate for it to lead the combined channel at all, so
    # this cannot be satisfied by two indifferent tiers. See the docstring for why the opponent
    # has to be the one gate 2 uses, and for what the global-argmax form cost.
    #
    # It is a superset of that global-argmax form: leading a channel outright implies both
    # holding evidence on it and not losing the runner-up on it. So every answer the previous
    # form identified is still identified, and this change can only add outcomes, never
    # withdraw one.
    # ``lexical_primary`` relaxes exactly one half of that, for exactly one tier. The anchor
    # tier must still hold evidence for the candidate — corroboration is not optional — but it
    # may no longer VETO by preferring the runner-up. The lexical tier keeps both halves and so
    # decides the comparison.
    #
    # The case for it is measured rather than aesthetic: on the documents probed while
    # diagnosing the abstentions the lexical tier held the correct answer while the anchor tier
    # elected something else — ca_sin_confirmation, ca_articles_incorporation_provincial,
    # us_bylaws. Their anchors are generic, so the anchor tier is weak there, and a weak tier
    # that can still veto turns "I have little to say" into "no".
    #
    # The case against it, which is why this is a switch rather than a rewrite: the reliability
    # is not uniform. On ``us_paystub`` the anchor tier is CORRECT and the lexical tier prefers
    # ``us_ssn_card``, so removing the veto there removes the tier that was right. It is a
    # measured trade and the corpus is the measurement: if it turns an abstention into a WRONG
    # answer it is not worth having, whatever else it recovers.
    anchor_may_veto = not settings.lexical_primary
    dissenting = tuple(
        name
        for name, channel, may_veto in (
            ("anchor", deciding_anchor_bits, anchor_may_veto),
            ("lexical", deciding_explained, True),
        )
        if not (
            channel.get(candidate, 0.0) > 0.0
            and (not may_veto or channel.get(candidate, 0.0) >= channel.get(runner_up, 0.0))
        )
    )
    concurred = bool(runner_up) and not dissenting
    conclusive_here = conclusive is not None and conclusive == candidate
    route = "concurrence" if concurred else ("conclusive-l1" if conclusive_here else "")

    # Gate 1b is satisfied by construction rather than tested after the fact: ``candidate``,
    # ``lead`` and ``concurred`` above were all computed on ``unpromoted`` when there was one,
    # so there is no zone-weighted winner left to veto. The leader is recorded for the audit
    # trail, and named ``n/a`` on a payload that carried no roles to discard.
    unpromoted_leader = candidate if unpromoted is not None else ""
    identified = bool(route)

    support = deciding_supports.get(candidate, 0.0)
    coverage = (
        unpromoted.coverage.get(candidate, 0.0)
        if unpromoted is not None
        else max(lexical.coverage.get(candidate, 0.0), anchor.coverage.get(candidate, 0.0))
    )

    # Each factor is 0.5 at its own floor; see the docstring. Every denominator is an accept
    # threshold itself, so there is no extra constant to tune and no scale to re-derive — and
    # no configured floor can be inert, because all three scale the headline number of every
    # document whether or not they bind.
    def _ratio(value: float, floor: float) -> float:
        total = value + floor
        return value / total if total > 0 else 0.0

    confidence = (
        min(
            _ratio(lead, settings.classify_min_margin),
            _ratio(support, settings.classify_min_support),
            _ratio(coverage, settings.classify_min_coverage),
        )
        if identified
        else 0.0
    )

    # The audit record's "who was even in contention" line. Restricted to doctypes carrying
    # some evidence for THIS document, which is what keeps it readable and what keeps it
    # stable: a doctype scoring zero on both channels is not a contender and adding a hundred
    # of them does not lengthen this list.
    considered = tuple(
        doctype
        for doctype, _ in sorted(
            (
                (spec.doctype_id, supports[spec.doctype_id])
                for spec in spec_list
                if anchor.scores.get(spec.doctype_id, 0.0) > 0.0
                or lexical.explained.get(spec.doctype_id, 0.0) > 0.0
            ),
            key=lambda kv: (-kv[1], kv[0]),
        )[:4]
    )

    failures: list[str] = []
    if not route:
        silent = [
            name
            for name, winner in (("anchor", anchor_winner), ("lexical", explained_winner))
            if not winner
        ]
        if silent:
            detail = (
                f"the {' and '.join(silent)} channel"
                f"{'s are' if len(silent) > 1 else ' is'} silent — nothing scored above zero on "
                f"{'them' if len(silent) > 1 else 'it'}, and a silent tier cannot concur with "
                "anything"
            )
        elif not runner_up:
            detail = (
                "there is no second doctype to compare against, so no tier can be said to "
                "prefer this one over anything"
            )
        else:
            detail = (
                f"the {' and '.join(dissenting)} channel"
                f"{'s do' if len(dissenting) > 1 else ' does'} not support {candidate!r} over "
                f"{runner_up!r}, the doctype it would be accepted over "
                f"(channel leaders: anchors {anchor_winner!r}, lexical {explained_winner!r})"
            )
        failures.append(
            f"no doctype was identified — {detail}; and no doctype holds a decisive anchor "
            "or a corroborated checksum that only it could hold, so neither tier may accept "
            "over the other on its own"
        )
    # 'margin below floor' is the exact substring the refusal contract has always used for
    # "the winner did not beat the next candidate by enough". What is being measured changed
    # (a likelihood ratio on the combined absolute evidence channel, not a difference of two
    # saturating supports and not a difference of two registry-normalised probabilities); what
    # it means to a reviewer did not.
    if lead < settings.classify_min_margin:
        failures.append(
            f"margin below floor {settings.classify_min_margin:.2f} — the leading doctype "
            f"leads the next one by {lead:.3f} on combined evidence, {b1 - b2:.3f} bits "
            f"(component leads: {a1 - a2:.3f} bits anchor, {l1 - l2:.3f} lexical)"
        )
    if support < settings.classify_min_support:
        failures.append(
            f"support below floor {settings.classify_min_support:.2f} — the combined anchor "
            "and lexical evidence for that document type is too thin to accept at any margin"
        )
    if coverage < settings.classify_min_coverage:
        failures.append(
            f"coverage below floor {settings.classify_min_coverage:.2f} — too little of that "
            "document type's vocabulary was present"
        )

    return _Verdict(
        candidate=candidate if support > 0.0 else None,
        route=route if identified else "",
        runner_up=runner_up,
        anchor_winner=anchor_winner,
        explained_winner=explained_winner,
        unpromoted_leader=unpromoted_leader,
        anchor_lead=round(a1 - a2, 6),
        explained_lead=round(l1 - l2, 6),
        lead_bits=round(b1 - b2, 6),
        lead=round(lead, 6),
        support=round(support, 6),
        coverage=round(coverage, 6),
        confidence=round(confidence, 6),
        considered=considered,
        failures=tuple(failures),
    )


def _evidence(
    *,
    features: structural_tier.StructuralFeatures,
    priors: Mapping[str, float],
    anchor: anchors_tier.AnchorOutcome,
    lexical: LexicalOutcome,
    bert: Mapping[str, float],
    doctype_id: str,
) -> list[Evidence]:
    """Assemble the audit trail for the winning candidate."""
    evidence = [
        Evidence(
            tier="structural",
            detail=structural_tier.describe(features),
            weight=round(priors.get(doctype_id, 0.0), 4),
        )
    ]
    evidence.extend(list(anchor.evidence.get(doctype_id, ()))[:_MAX_EVIDENCE_PER_TIER])

    matched = lexical.matched.get(doctype_id, ())
    top_terms = ", ".join(term for term, _ in matched[:6]) or "none"
    evidence.append(
        Evidence(
            tier="lexical",
            detail=(
                f"bm25={lexical.raw.get(doctype_id, 0.0):.3f} "
                f"p={lexical.probability.get(doctype_id, 0.0):.3f} "
                f"coverage={lexical.coverage.get(doctype_id, 0.0):.3f}; "
                f"profile terms seen: {top_terms}"
            ),
            weight=round(lexical.probability.get(doctype_id, 0.0), 4),
        )
    )
    if bert:
        evidence.append(
            Evidence(
                tier="bert",
                detail=f"local BERT kNN p={bert.get(doctype_id, 0.0):.3f}",
                weight=round(bert.get(doctype_id, 0.0), 4),
            )
        )
    return evidence


def _abstain(
    *,
    reason: str,
    evidence: Sequence[Evidence],
    runners_up: Sequence[tuple[str, float]],
    started: float,
    confidence: float = 0.0,
    margin: float = 0.0,
    coverage: float = 0.0,
) -> Classification:
    """Build the ``UNKNOWN`` result. The candidate we declined is kept in ``runners_up``."""
    return Classification(
        doctype_id=UNKNOWN,
        label="",
        country="",
        confidence=round(confidence, 6),
        margin=round(margin, 6),
        coverage=round(coverage, 6),
        abstained=True,
        reason=reason,
        evidence=list(evidence),
        runners_up=list(runners_up),
        page_types=[UNKNOWN],
        ms=_elapsed_ms(started),
    )


def _spec_by_id(specs: Sequence[DocTypeSpec], doctype_id: str) -> DocTypeSpec | None:
    """Look up a spec by id."""
    for spec in specs:
        if spec.doctype_id == doctype_id:
            return spec
    return None


def _page_numbers(view: LayoutView) -> list[int]:
    """Ordered page numbers present in the payload."""
    pages = {p.page for p in view.pages}
    pages.update(b.page for b in view.blocks)
    pages.update(t.page for t in view.tables)
    return sorted(pages)


def _page_view(view: LayoutView, page: int) -> LayoutView:
    """Slice a single page out of a multi-page payload."""
    return LayoutView(
        doc_id=f"{view.doc_id}#p{page}" if view.doc_id else "",
        pages=[p for p in view.pages if p.page == page],
        blocks=[b for b in view.blocks if b.page == page],
        tables=[t for t in view.tables if t.page == page],
        marks=[m for m in view.marks if m.page == page],
        key_values=[kv for kv in view.key_values if kv.page == page],
        languages=list(view.languages),
    )


def _elapsed_ms(started: float) -> int:
    """Milliseconds since ``started``."""
    return round((time.perf_counter() - started) * 1000)
