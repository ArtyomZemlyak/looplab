"""The rule fallback does not switch a wide run onto a schedule that cannot fill its slots.

`RuleStrategist` is the fallback for EVERY LLM failure — `ToolUsingStrategist` falls back to it "on
any parse/transport failure, so a flaky model never crashes the run" — so this arm runs on a
perfectly healthy run whose only problem was one bad response from the endpoint.

It read no width. `agents/strategist.py`'s own brief to the model says why that matters: "a racing
schedule (`asha`/`bohb`) fills one slot once its seed target is met: a promotion needs the rung's
survivors, so an unresolved arm blocks both seeding and promotion", and
`search/card_selection.py::_asha_mask_is_unsound` measured the consequence across the corpus at 5.94
of 8.03 starved GPU-hours, with 0.00 in every GreedyTree and EvolutionaryPolicy run. So an endpoint
hiccup at width 2 could hand the run the exact schedule the brief warns the model against.

`policy_fills_width` is the predicate that brief already cites, asked rather than re-derived. It is
False ONLY for a racing schedule asked to fill more than one slot, so width 1 is untouched.
"""
from __future__ import annotations

import pytest

from looplab.agents.strategist import RuleStrategist, StrategyContext
from looplab.core.models import RunState
from looplab.search.policy import available_policies, policy_fills_width


def _ctx(**kw):
    base = {"available_policies": available_policies(), "available_developers": ["default"]}
    base.update(kw)
    return StrategyContext(**base)


def _explore(width):
    return RuleStrategist().decide(RunState(), _ctx(
        phase="explore", failure_rate=0.1, improves_since_best=0, eval_parallel=width))


def test_at_width_one_the_racing_schedule_is_still_chosen():
    """The behaviour this must not change. `policy_fills_width` is False only for a RACER asked to
    fill more than one slot, so a serial run keeps exactly the arm it had."""
    assert _explore(1)["policy"] == "asha"


@pytest.mark.parametrize("width", [2, 4, 8])
def test_at_a_wider_run_it_declines_rather_than_starving_the_slots(width):
    """MUTATION: drop the conjunct -> one bad LLM response puts a healthy width-N run on a schedule
    that fills one slot, which is 5.94 of the corpus's 8.03 starved GPU-hours."""
    assert _explore(width) is None, (
        "declining is 'nothing to change' — the run keeps the policy it has rather than being "
        "switched onto one that cannot keep its slots busy")


def test_declining_is_not_the_same_as_having_no_opinion_at_all():
    """The fallback still decides everything else. MUTATION: guard the whole method on width ->
    seeding and the budget-exhausted exploit arm stop firing on every parallel run.
    """
    seed = RuleStrategist().decide(RunState(), _ctx(
        phase="seed", failure_rate=0.0, improves_since_best=0, eval_parallel=4))

    assert seed and seed["policy"] == "greedy"


def test_the_predicate_is_asked_and_not_re_derived():
    """One rule. The brief the model reads and the arm the fallback takes must agree, or the run is
    told one thing and does another.

    MUTATION: inline `width > 1` here -> it drifts from `policy_fills_width`, whose unknown-name
    answer is deliberately True, and a policy this table has not heard of starts being refused.
    """
    assert policy_fills_width("asha", 1) is True
    assert policy_fills_width("asha", 4) is False
    assert policy_fills_width("greedy", 4) is True
    assert policy_fills_width("a-policy-nobody-registered", 4) is True, (
        "an unknown name is not accused of starving anything; the arm inherits that")


def test_an_unreadable_width_does_not_refuse_the_arm():
    """`policy_fills_width` is total and answers True for a width it cannot parse. A strategist that
    refused on a missing width would silently stop choosing this arm wherever the context is thin.
    """
    assert RuleStrategist().decide(RunState(), _ctx(
        phase="explore", failure_rate=0.1, improves_since_best=0))["policy"] == "asha"
