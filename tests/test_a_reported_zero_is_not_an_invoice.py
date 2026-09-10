"""One rule for one fact: a reported `cost: 0.0` means "the upstream did not price this".

`benchmarks/meter/proxy.py::_body_cost` settled this on 2026-09-06 and `core/llm.py` was still
accepting the same zero as a stated amount — two rules for one fact, one layer apart, under a
doc-25 COST-01 heading that says the rule is stated once, and the production path was the one
accepting it.

WHERE THE MONEY WAS: `RunBudget`'s reserve estimate is committed / priced_calls, so every zero
counted as "priced" divides the estimate down. Driven through the real add -> ledger -> commit path
with identical traffic and an identical $1.00 cap, 16-way fan-out: a gateway stamping
`usage.cost: 0.0` admitted 16 of 16 and spent $8.50; the same gateway omitting the key admitted 1
of 16 and spent $1.00.
"""
from __future__ import annotations

import pytest

from looplab.core.llm import _safe_cost, cost_is_reported


@pytest.mark.parametrize("value", [0.0, -0.0, 0, -1.0, -0.5])
def test_a_zero_or_negative_amount_is_not_a_stated_price(value):
    assert cost_is_reported(value) is False, (
        f"{value!r} read as an invoice — `benchmarks/meter/proxy.py::_body_cost` refuses the "
        "identical value, and accepting it is the 'budget never binds' defect: the corporate "
        "gateway emits exactly this zero for a model group it has no price for")


@pytest.mark.parametrize("value", [1e-9, 0.25, 2.5, 1_000_000.0])
def test_a_real_amount_still_is(value):
    assert cost_is_reported(value) is True


@pytest.mark.parametrize("value", [None, "0.0", b"0", True, False, float("nan"), float("inf")])
def test_a_malformed_amount_is_not_one_either(value):
    assert cost_is_reported(value) is False


def test_the_number_itself_did_not_move():
    """The change is about `priced_calls`, not about the amount charged — so `_safe_cost` must be
    byte-identical across the boundary. If refusing the zero had also changed what is BILLED, this
    would be a pricing change wearing an accounting change's clothes."""
    for value, expected in ((0.0, 0.0), (-0.0, 0.0), (None, 0.0), ("x", 0.0),
                            (2.5, 2.5), (1e-9, 1e-9)):
        assert _safe_cost(value) == expected


def test_the_two_layers_now_agree_about_the_same_zero():
    """The point of the change, stated against the OTHER implementation rather than against a
    literal — so the day either side moves, this is what says they diverged."""
    from benchmarks.meter.proxy import _body_cost

    for value in (0.0, -0.0, -1.0, "0.0", None):
        assert _body_cost({"cost": value}) is None, f"the meter accepted {value!r}"
        assert cost_is_reported(value) is False, f"core/llm accepted {value!r}"
    assert _body_cost({"cost": 2.5}) == 2.5 and cost_is_reported(2.5) is True
