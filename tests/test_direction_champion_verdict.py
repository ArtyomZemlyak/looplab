"""A question answered by a DRAFT still gets a verdict.

`best_delta`'s baseline is each child's own PARENT NODE (`events/card_ledger.py::_evidence_verdict`), so
a direction answered by a first-generation draft — which has no parent carrying a metric — gets no
number at all. MEASURED on `runs/e5small-dr-unified-v11`: of the four directions with an evaluated
child, THREE reported `best_delta: null` while their children had measured 0.773951, 0.759164 and
0.718923. The board showed an answered question as unmeasured, which is what the operator saw.

TWO BASELINES WERE TRIED. The direction's own `scored_against` is the semantically ideal answer to
"does X help?" — and it is `None` on all NINE of v11's directions, because a direction is derived
from a belief row and never carries a score fence. A fallback that is null exactly where it is
needed is not a fallback. The RUN CHAMPION's metric exists on every run that has evaluated
anything, and answers what an operator reads this board for.

`best_delta` and the `supported`/`tested` ladder are deliberately UNTOUCHED — `card_ledger.py`
records what loosening them cost last time (a live Researcher on v5 reading "verdict=supported" over
one parentless node and writing "…that's odd").
"""
from __future__ import annotations

import math

from looplab.core.cards import card_child_rollup, card_rollup_brief
from looplab.serve.public_cards import _ROLLUP_KEYS


class _C:
    def __init__(self, cid, status="evaluated", best_delta=None, evidence=()):
        self.id, self.status, self.best_delta, self.evidence = cid, status, best_delta, list(evidence)


def test_a_parentless_draft_still_gets_a_number():
    """THE CASE THIS EXISTS FOR: no child has a `best_delta`, and the direction is still answered."""
    rollup = card_child_rollup([_C("card-0"), _C("card-1")],
                               champion_metric=0.7934,
                               child_metrics={"card-0": 0.7740, "card-1": 0.7592})
    assert rollup["best_delta"] is None, "the parent-relative number is untouched and still absent"
    assert math.isclose(rollup["best_vs_champion"], 0.7740 - 0.7934, abs_tol=1e-12)
    assert rollup["best_vs_champion_card_id"] == "card-0", "the BEST child owns the number"


def test_both_numbers_survive_and_can_disagree_in_sign():
    """v11's InfoNCE direction, exactly: `best +0.01724` (beat its own parent) and
    `best vs champion -0.013805` (still below the run's best). Collapsing them would let a
    direction that LOST to the champion read as a win."""
    rollup = card_child_rollup([_C("card-7", best_delta=0.01724)],
                               champion_metric=0.7934, child_metrics={"card-7": 0.779595})
    assert rollup["best_delta"] > 0 and rollup["best_vs_champion"] < 0
    brief = card_rollup_brief(rollup)
    assert "best +0.01724" in brief and "best vs champion -0.013805" in brief, brief


def test_min_direction_flips_the_sign():
    """Mutation: drop the `direction` branch and a minimising task reports every improvement as a
    loss — the failure `_card_verdict`'s own direction-aware baseline exists to avoid."""
    lower_is_better = card_child_rollup([_C("card-0")], champion_metric=0.50,
                                        child_metrics={"card-0": 0.40}, direction="min")
    assert lower_is_better["best_vs_champion"] > 0, "0.40 beats 0.50 when lower is better"
    higher = card_child_rollup([_C("card-0")], champion_metric=0.50,
                               child_metrics={"card-0": 0.40}, direction="max")
    assert higher["best_vs_champion"] < 0


def test_no_champion_and_no_metric_contribute_NOTHING_not_zero():
    """A zero is a claim that the child tied the champion. Same rule `best_delta` already follows:
    'a child with no measurement contributes nothing rather than a zero'."""
    for kwargs in ({"champion_metric": None, "child_metrics": {"card-0": 0.7}},
                   {"champion_metric": 0.79, "child_metrics": {}},
                   {"champion_metric": 0.79, "child_metrics": {"card-0": None}},
                   {"champion_metric": float("nan"), "child_metrics": {"card-0": 0.7}}):
        rollup = card_child_rollup([_C("card-0")], **kwargs)
        assert rollup["best_vs_champion"] is None, kwargs
        assert rollup["best_vs_champion_card_id"] is None, kwargs


def test_the_pair_is_on_the_PUBLIC_wire():
    """A number the operator's card pane cannot receive is the R1 defect again (#50: applied_params
    was on the wire and rendered nowhere; this is the mirror — computed and not on the wire)."""
    assert {"best_vs_champion", "best_vs_champion_card_id"} <= _ROLLUP_KEYS


def test_the_brief_omits_it_when_absent():
    """NON-VACUITY for the renderer: a direction with no champion-relative number must not gain an
    empty clause, or every unanswered question grows noise."""
    brief = card_rollup_brief(card_child_rollup([_C("card-0", status="running")]))
    assert "champion" not in brief, brief
    assert brief == "1 experiment(s), 1 running"
