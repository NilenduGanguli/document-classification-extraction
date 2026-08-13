"""Compare two classification avenues on one document, and **adjudicate nothing**.

This module is the reason ``/api/v1/classify/compare`` is worth building even though no
second avenue shipped: it is the instrument by which any future avenue gets evaluated
honestly, and it is the only place that will ever be allowed to hold both decision trails at
once.

**It does not fuse.** There is no rule here that picks a winner, breaks a tie, promotes a
confidence, or turns two abstentions into an answer. Fusing two channels is where this
codebase has produced its worst defects — the two-channel concurrence rule, the zone-free
guard, the jurisdiction veto — each of which fixed a real problem and introduced a new one.
A fusion rule must be chosen on data, and this endpoint is how that data gets produced. If
you are here to add ``if visual.confidence > lexical.confidence``, stop: that decision needs
a corpus run behind it, and it belongs in the cascade where the abstention discipline lives,
not in a reporting surface.

The verdict vocabulary is small on purpose, and every value is an observation rather than a
judgement.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from dce.models import Classification

__all__ = ["Verdict", "compare_classifications"]

#: Both avenues answered and named the same doctype.
AGREE = "agree"
#: Both answered and named different doctypes. **At most one of them is right, and possibly
#: neither.** This is the document a human should see, and the population a fusion rule has
#: to be measured on.
DISAGREE = "disagree"
#: Exactly one answered; the other abstained. Not agreement and not conflict — the abstaining
#: avenue said nothing, and "nothing" must not be read as assent.
ONE_ABSTAINED = "one_abstained"
#: Neither answered. The document goes to a human, which is the designed outcome.
BOTH_ABSTAINED = "both_abstained"
#: Only one avenue ran at all, because no second avenue is configured or available. Reported
#: distinctly from ``one_abstained``: an avenue that does not exist did not abstain, and
#: collapsing the two would let an empty registry read as a considered refusal.
SINGLE_AVENUE = "single_avenue"


@dataclass(frozen=True)
class Verdict:
    """The relationship between two decision trails. Nothing here is a decision."""

    #: One of ``agree`` | ``disagree`` | ``one_abstained`` | ``both_abstained`` |
    #: ``single_avenue``.
    verdict: str
    #: True only when both avenues answered and named the same doctype.
    same_doctype: bool
    #: How many of the two avenues produced a non-abstaining answer. 0, 1 or 2.
    answered: int
    #: A human-readable line naming what happened and, explicitly, what was NOT concluded.
    detail: str


def compare_classifications(
    lexical: Classification | None,
    second: Classification | None,
    *,
    second_method: str = "",
    second_problem: str = "",
) -> Verdict:
    """Describe how two classifications relate.

    Args:
        lexical: The in-process cascade's answer. ``None`` only if the cascade did not run.
        second: The second avenue's answer, or ``None`` when no second avenue ran.
        second_method: Id of the second avenue, for the detail line.
        second_problem: Why the second avenue did not run, when it did not.

    Returns:
        A :class:`Verdict`. **No field of it is an adjudication**: ``agree`` does not mean
        the answer is right, and ``disagree`` does not nominate a winner. Two avenues can
        agree and both be wrong — the corpus contains at least one pair (``mx_cif`` /
        ``mx_rfc_csf``) that renders as the *same document* under two registry names, where
        agreement is guaranteed and correctness is not available to any classifier.
    """
    if second is None:
        answered = 0 if (lexical is None or lexical.abstained) else 1
        why = second_problem or "no second classification avenue is configured or available"
        return Verdict(
            verdict=SINGLE_AVENUE,
            same_doctype=False,
            answered=answered,
            detail=(
                f"only the lexical cascade ran: {why}. This is not a second opinion and must "
                "not be read as one — the lexical answer stands exactly as /classify would "
                "have returned it."
            ),
        )

    lex_answered = lexical is not None and not lexical.abstained
    vis_answered = not second.abstained
    method = second_method or "second avenue"

    if lex_answered and vis_answered:
        same = lexical is not None and lexical.doctype_id == second.doctype_id
        if same:
            return Verdict(
                verdict=AGREE,
                same_doctype=True,
                answered=2,
                detail=(
                    f"lexical and {method} both answered "
                    f"{second.doctype_id!r}. Agreement is a fact about the two trails, not "
                    "evidence of correctness: nothing here checks either against ground "
                    "truth, and two avenues can agree and both be wrong."
                ),
            )
        return Verdict(
            verdict=DISAGREE,
            same_doctype=False,
            answered=2,
            detail=(
                f"lexical answered {lexical.doctype_id!r} and {method} answered "
                f"{second.doctype_id!r}. At most one is right and possibly neither. Nothing "
                "is adjudicated here: both trails are returned in full and the document "
                "belongs in front of a human."
            ),
        )

    if lex_answered or vis_answered:
        answerer, abstainer = ("lexical", method) if lex_answered else (method, "lexical")
        reason = (second.reason if lex_answered else (lexical.reason if lexical else "")) or (
            "no reason given"
        )
        return Verdict(
            verdict=ONE_ABSTAINED,
            same_doctype=False,
            answered=1,
            detail=(
                f"{answerer} answered; {abstainer} abstained ({reason}). An abstention is "
                "silence, not assent — it neither corroborates nor contradicts the answer."
            ),
        )

    return Verdict(
        verdict=BOTH_ABSTAINED,
        same_doctype=False,
        answered=0,
        detail=(
            "neither avenue answered; the document goes to a human. That is the designed "
            "outcome, not a failure of either avenue."
        ),
    )
