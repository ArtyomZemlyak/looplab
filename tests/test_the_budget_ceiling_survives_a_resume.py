"""`llm_budget_usd` is a ceiling on THIS RUN, and a resume is the same run.

`run_cost_accountant` went to some trouble to make the ceiling one per RUN rather than one per
client — measured before that fix, two clients from one `Settings` gave an effective ceiling of 2.0.
It was still one per PROCESS. A `CostAccountant` is constructed with `spent = 0.0`, so
`looplab resume` handed a run that had already exhausted its budget a second full budget.

Measured 2026-09-04: `freeB3` auto-paused with **$1.0308 already metered** against a $1.00 ceiling.
Resumed, it ran 27 more minutes and 45 more calls for another $0.0929 with no refusal of any kind,
on course for ~$2.03. It was stopped by pid at **$1.1056** — 10.6 % over a cap every other probe in
the batch respected to within a cent ($1.0082, $1.0098, $1.0102).
"""
from __future__ import annotations

from looplab.engine.costs import seed_prior_spend
from looplab.core.llm import CostAccountant


class _Event:
    def __init__(self, type_, data):
        self.type = type_
        self.data = data


class _Store:
    def __init__(self, events):
        self._events = events

    def read_all(self):
        return list(self._events)


class _Role:
    """`find_cost_accountants` walks roles looking for an `accountant` attribute."""

    def __init__(self, accountant):
        self.accountant = accountant


class _Engine:
    def __init__(self, events, accountant):
        self.store = _Store(events)
        self.researcher = _Role(accountant)


def _usage(cost):
    return _Event("llm_usage", {"cost": cost, "usage_id": f"u{cost}"})


def test_a_resumed_run_starts_charged_for_what_it_already_spent():
    acc = CostAccountant(limit=1.0)
    eng = _Engine([_usage(0.6), _usage(0.4308)], acc)
    assert seed_prior_spend(eng) == 1.0308
    assert abs(acc.spent - 1.0308) < 1e-9, (
        f"a resumed run started at {acc.spent}; freeB3 started at 0.0 against a $1.00 ceiling and "
        "spent another $0.0929 before it was stopped by hand")


def test_a_fresh_run_is_not_charged_for_anything():
    acc = CostAccountant(limit=1.0)
    assert seed_prior_spend(_Engine([], acc)) == 0.0
    assert acc.spent == 0.0


def test_seeding_twice_does_not_charge_twice():
    """The bind path can run more than once in a process; a ceiling that halves each time is worse
    than one that never applied."""
    acc = CostAccountant(limit=1.0)
    eng = _Engine([_usage(0.5)], acc)
    seed_prior_spend(eng)
    seed_prior_spend(eng)
    assert abs(acc.spent - 0.5) < 1e-9, acc.spent


def test_the_prior_is_ADDED_to_what_this_process_has_already_spent():
    """`run_cost_accountant` caches one accountant on the `Settings` object, so a process that
    starts a second engine from the same settings hands over an accountant that already holds
    spend. Overwriting it would erase paid calls -- and every fixture here starts at zero, which is
    why mutation had to point this out."""
    acc = CostAccountant(limit=2.0)
    acc.spent = 0.25                       # already paid in THIS process
    eng = _Engine([_usage(0.6)], acc)
    seed_prior_spend(eng)
    assert abs(acc.spent - 0.85) < 1e-9, (
        f"{acc.spent}: the prior spend replaced this process's own instead of adding to it")


def test_an_unlimited_accountant_is_left_alone():
    """`llm_budget_usd` 0.0 means no ceiling; there is nothing to protect and inflating `spent`
    would only corrupt the UI's cost panel."""
    acc = CostAccountant(limit=None)
    assert seed_prior_spend(_Engine([_usage(0.7)], acc)) == 0.0
    assert acc.spent == 0.0


def test_junk_and_negative_costs_are_not_credited():
    """A malformed or negative row must not BUY budget back. `_safe_cost` degrades an unusable
    amount to 0.0 elsewhere for the same reason."""
    acc = CostAccountant(limit=1.0)
    eng = _Engine([_usage(0.5), _Event("llm_usage", {"cost": "abc"}),
                   _Event("llm_usage", {"cost": -9.0}), _Event("llm_usage", None),
                   _Event("node_evaluated", {"cost": 5.0})], acc)
    assert abs(seed_prior_spend(eng) - 0.5) < 1e-9
    assert abs(acc.spent - 0.5) < 1e-9


def test_an_unreadable_log_does_not_stop_the_run_from_starting():
    class _Bad:
        def read_all(self):
            raise OSError("torn log")
    acc = CostAccountant(limit=1.0)
    eng = _Engine([], acc)
    eng.store = _Bad()
    assert seed_prior_spend(eng) == 0.0
