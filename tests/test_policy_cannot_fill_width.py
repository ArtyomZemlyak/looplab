"""A POLICY AND A WIDTH ARE CHOSEN IN ONE BREATH, AND NOTHING LOOKED AT BOTH.

WHAT HAPPENED (`runs/e5small-dr-unified-v4`, traced 2026-08-21)
---------------------------------------------------------------
One `strategy_decision`, `source: "agent"`, at node 3, set in a single payload:

    "policy": "bohb"          # an alias for the ASHA factory (search/policy.py)
    "eval_parallel": 2

with a sound argument for the racing schedule — *"Evals are extremely costly (~5h each) … bias
toward an in-process hyperparameter sweep (bohb + prefer_sweep=true) to explore the numeric knobs
efficiently rather than many separate evals."* Nothing in it mentions the width, and nothing
anywhere connected the two fields.

The consequence, from the run's own log: **no `card_added` for 19.5 hours** while hypotheses kept
arriving (last one 18.3 h ago) and a device sat idle throughout. `_stage_card_creates` is the only
writer of `card_added` and is fed by `speculative_raw_actions`, which answered zero.

**THIS IS NOT A BUG IN THE SELECTOR OR THE MASK.** Verified against the live folded board: all four
nodes are rung-0 roots, the census reads 4 against a seed target of 3 so the forced seed prefix
offers nothing, and node 3 is an UNRESOLVED root — so `_asha_mask_is_unsound` is True and the
discretionary lane is correctly skipped, because masking an unresolved root would let ASHA read its
slot as a rung-0 vacancy. Mutating the board settles it: making node 2 succeed -> still 0; removing
node 2 -> still 0; **finishing node 3 -> 1 action**. ASHA behaving exactly as designed: seeds met,
one arm unresolved, so it can neither seed nor promote.

So the run is serial BY POLICY, and the decision that made it so is one event that nobody warned and
nobody recorded. That is what this file pins — a NOTICE and a RECEIPT, never a refusal. The racing
schedule can be the right answer; what it must not be is an unwitting one.

The corpus measurement is already in the tree, in `card_selection._asha_mask_is_unsound`'s docstring:
8.03 starved hours across five runs, 5.94 of them this shape, and **0.00 in every GreedyTree and
EvolutionaryPolicy run**.
"""
from __future__ import annotations

import pytest

from looplab.search.policy import SERIALISING_POLICIES, available_policies, policy_fills_width


# ------------------------------------------------------------------ the rule
def test_a_racing_schedule_cannot_fill_more_than_one_slot():
    for name in ("asha", "bohb", "ASHA", "  bohb  "):
        assert policy_fills_width(name, 2) is False
        assert policy_fills_width(name, 8) is False


def test_it_says_nothing_about_a_serial_run():
    """Width 1 is the shape a racing schedule fits exactly. No cost, no notice."""
    for name in ("asha", "bohb", "greedy"):
        assert policy_fills_width(name, 1) is True
        assert policy_fills_width(name, 0) is True


def test_the_lanes_that_always_answered_are_not_accused():
    """0.00 starved hours under these two across the whole corpus — the measurement this rule rests
    on would be contradicted by flagging them."""
    for name in ("greedy", "evolutionary", "mcts"):
        assert policy_fills_width(name, 2) is True


def test_an_unknown_policy_is_not_accused_of_starving_anything():
    """Fail-quiet, the same rule `per_experiment_gpu_budget` follows for an unprobed pool: a policy
    this table has not heard of gets no claim made about it."""
    assert policy_fills_width("something-new", 4) is True
    assert policy_fills_width(None, 4) is True
    assert policy_fills_width("bohb", "two") is True      # unusable width -> no claim


def test_every_named_policy_is_a_real_one():
    """A serialising name that no factory registers would be a rule about nothing — the vacuous
    green this repo has shipped before."""
    assert SERIALISING_POLICIES <= set(available_policies())
    assert "bohb" in SERIALISING_POLICIES and "asha" in SERIALISING_POLICIES


# ------------------------------------------------------------------ the notice, before the decision
class _Ctx:
    def __init__(self, width, menu):
        self.eval_parallel, self.available_policies = width, menu


def _note(width, menu=("greedy", "evolutionary", "mcts", "asha", "bohb")):
    from looplab.agents.strategist import _policy_width_note

    return _policy_width_note(_Ctx(width, list(menu)))


def test_the_strategist_is_told_the_cost_before_it_answers():
    text = _note(2)
    assert "CONCURRENCY COST OF THE POLICY CHOICE" in text
    assert "asha" in text and "bohb" in text
    assert "eval_parallel=2" in text
    # it names the mechanism, not just the verdict
    assert "promotion needs the rung's survivors" in text
    # …and the measurement, so it is checkable rather than an assertion of authority
    assert "8.03 starved hours" in text


def test_the_notice_forbids_nothing_and_offers_the_alternative():
    """The engine has no standing to overrule a Strategist on a search-design question it argued
    for. It has standing to make the consequence visible and to offer the honest alternative."""
    text = _note(2)
    assert "Choose it if the search argument is worth that" in text
    assert "eval_parallel to 1 in the same answer" in text
    assert "serial on purpose rather than by accident" in text


def test_the_ordinary_brief_is_unchanged():
    """`off == today` for every shape this does not concern — width 1, and a menu with nothing
    serialising in it."""
    assert _note(1) == ""
    assert _note(2, menu=("greedy", "evolutionary")) == ""
    assert _note(None) == ""


# ------------------------------------------------------------------ the receipt, after the decision
def test_the_decision_record_carries_the_unfilled_width(tmp_path):
    """A RECEIPT, not a refusal: the pair is recorded so a later reader can ask "was this run serial
    on purpose?" of the row that decided it, instead of re-deriving it from a starved GPU."""
    import inspect

    from looplab.engine import strategy

    src = inspect.getsource(strategy)
    assert "policy_fills_width" in src
    assert '"width_unfilled"' in src
    # ABSENT when the pair is fine — every decision that fills its width writes what it always did
    assert "if not policy_fills_width" in src


def test_the_receipt_is_built_from_the_same_rule_the_notice_uses():
    """Two spellings of "which policies starve a wide run" would disagree the day one moves. Both
    the pre-decision notice and the post-decision receipt call `policy_fills_width`."""
    import inspect

    from looplab.agents import strategist
    from looplab.engine import strategy

    assert "policy_fills_width" in inspect.getsource(strategist._policy_width_note)
    assert inspect.getsource(strategy).count("policy_fills_width") >= 1
