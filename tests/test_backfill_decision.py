"""When a free device that is reserved for wider work may be used anyway.

`proposal_derived_width` settles the run's width from the WIDEST footprint any open proposal
declares, so one card asking for two GPUs on a two-GPU box settles the width to 1 — correctly, since
that experiment will need both devices when it runs. What nothing filled was the gap: measured on
`runs/e5small-dr-unified-v4`, a node declaring `{"gpus": 1}` held one card for a nine-hour
evaluation while the other idled the whole time, reserved for a two-GPU proposal nobody had started.

The rule weighs the trade instead of testing a deadline, which is what strict EASY backfilling does
and why it is wrong here: with seven hours left and a nine-hour candidate, EASY refuses and spends
SEVEN device-hours of idleness to avoid TWO hours of delay.

    benefit = min(candidate, remaining)        cost = max(0, candidate - remaining)
    admit  ⟺  benefit > lam * cost
"""
import math

import pytest

from looplab.engine.cadence import backfill_admits, backfill_receipt

H = 3600.0


# --------------------------------------------------------------------------- the trade

def test_the_case_strict_backfilling_gets_wrong():
    """Seven hours left, a nine-hour candidate: two hours of delay buys seven device-hours."""
    assert backfill_admits(9 * H, 7 * H) is True
    row = backfill_receipt(9 * H, 7 * H)
    assert row["benefit_s"] == 7 * H and row["cost_s"] == 2 * H
    assert row["why"] == "worth_the_delay"


def test_a_hopeless_candidate_is_refused():
    """One hour left, a nine-hour candidate: eight hours of delay to reclaim one."""
    assert backfill_admits(9 * H, 1 * H) is False
    assert backfill_receipt(9 * H, 1 * H)["why"] == "delay_exceeds_the_gain"


def test_a_candidate_that_fits_costs_nothing_and_needs_no_special_case():
    """The EASY case falls out of the arithmetic: cost is zero, so any positive benefit wins."""
    assert backfill_admits(2 * H, 9 * H) is True
    row = backfill_receipt(2 * H, 9 * H)
    assert row["cost_s"] == 0.0 and row["why"] == "fits_inside_the_gap"


def test_the_boundary_is_strict():
    """`benefit > lam*cost`, not `>=`. At exactly break-even the reservation keeps its place — the
    tie goes to the work that was already promised, which is the conservative direction."""
    # remaining 4h, candidate 8h: benefit 4h, cost 4h — equal at lam=1
    assert backfill_admits(8 * H, 4 * H, lam=1.0) is False
    assert backfill_admits(8 * H - 1, 4 * H, lam=1.0) is True


# --------------------------------------------------------------------------- the price

def test_lam_is_a_price_and_moves_the_decision_both_ways():
    """The 7-vs-9 case at three prices: cheap delay admits, expensive delay refuses."""
    assert backfill_admits(9 * H, 7 * H, lam=0.5) is True     # delay is cheap
    assert backfill_admits(9 * H, 7 * H, lam=1.0) is True     # 7 > 1.0 * 2
    assert backfill_admits(9 * H, 7 * H, lam=3.4) is True     # 7 > 3.4 * 2 = 6.8, still worth it
    assert backfill_admits(9 * H, 7 * H, lam=3.5) is False    # 7 > 7 is false: break-even refuses
    assert backfill_admits(9 * H, 7 * H, lam=3.6) is False


def test_lam_zero_is_utilisation_at_any_cost():
    """A price of zero says delay is free: anything with a positive benefit is admitted. Extreme but
    coherent, and it must not be mistaken for the refusal path."""
    assert backfill_admits(100 * H, 1 * H, lam=0.0) is True


# --------------------------------------------------------------------------- the refusals

@pytest.mark.parametrize("cand,rem", [
    (None, 7 * H), (7 * H, None), ("9h", 7 * H), (float("nan"), 7 * H), (7 * H, float("nan")),
    (float("inf"), 7 * H), (7 * H, float("inf")), (0, 7 * H), (-1, 7 * H), (9 * H, 0), (9 * H, -1),
])
def test_any_unknown_or_impossible_input_refuses(cand, rem):
    """A missing ETA means the engine cannot say how long something takes, and admitting on a guess
    is how a scheduler turns one idle device into two late experiments. A zero `remaining` admits
    nothing either: the reservation can start now, so there is no gap to fill."""
    assert backfill_admits(cand, rem) is False


def test_a_negative_price_refuses_rather_than_inverting_the_rule():
    """A negative lam would make cost a REWARD. Refuse the input instead of honouring it."""
    assert backfill_admits(9 * H, 7 * H, lam=-1.0) is False


# --------------------------------------------------------------------------- the receipt

def test_the_receipt_carries_the_arithmetic_not_just_the_verdict():
    """Recorded rather than acted on while the rule is observed, so the row has to be enough to
    re-derive the decision from the log alone."""
    row = backfill_receipt(9 * H, 7 * H, lam=2.0)
    assert set(row) >= {"admits", "lam", "candidate_s", "remaining_s", "benefit_s", "cost_s", "why"}
    assert row["lam"] == 2.0
    assert row["admits"] is (row["benefit_s"] > row["lam"] * row["cost_s"])


def test_an_unknown_receipt_says_so_and_admits_nothing():
    row = backfill_receipt(None, 7 * H)
    assert row["admits"] is False and row["why"] == "unknown_duration"
    assert "benefit_s" not in row


def test_a_closed_gap_is_unknown_and_not_a_verdict_about_a_trade():
    """`remaining <= 0` means the reservation can start NOW: there is no gap, so there is no trade
    to weigh. Saying "the delay exceeds the gain" would be a verdict about a comparison that does
    not exist — and without this the guard is unreachable, because the arithmetic happens to answer
    False anyway. Mutation found exactly that."""
    for rem in (0, -1, -3600):
        row = backfill_receipt(9 * H, rem)
        assert row["admits"] is False
        assert row["why"] == "unknown_duration", (rem, row)
        assert "cost_s" not in row


def test_a_fitting_candidate_reports_zero_cost_through_the_shared_terms():
    """Pins `max(0.0, ...)` where BOTH spellings read it. As two copies, flipping the predicate's
    turned no test red: the receipt kept a correct copy and the boolean happens not to change when
    cost goes negative."""
    row = backfill_receipt(2 * H, 9 * H)
    assert row["cost_s"] == 0.0 and row["benefit_s"] == 2 * H


def test_the_receipt_agrees_with_the_predicate_across_a_grid():
    """One rule, two spellings — they must not drift."""
    for cand in (0.5, 1, 3, 7, 9, 24):
        for rem in (0.5, 1, 3, 7, 9, 24):
            for lam in (0.0, 0.5, 1.0, 3.0):
                a = backfill_admits(cand * H, rem * H, lam=lam)
                b = backfill_receipt(cand * H, rem * H, lam=lam)["admits"]
                assert a is b, (cand, rem, lam)
