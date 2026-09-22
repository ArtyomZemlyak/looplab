"""A resumed run's spend is read off the ledger ONE way — the fold's (review 2026-09-22, CORE-01).

Three readers of the same durable `llm_usage` rows used to disagree. On one log holding a row
appended twice under the same `usage_id` (outbox recovery after a crash between the append and the
forget) plus one legacy id-less row:

    fold(...).llm_cost["cost"]      0.50   what `inspect`, the UI and the budget summary show
    seed_prior_spend  (accountants) 0.70   summed every row, no dedupe
    seed_run_budget   (RunBudget)   0.20   id-bearing rows only

so a resumed run enforced a ceiling that was neither the number it displayed nor the same number in
its two halves. Both seeders now read through `engine/costs.py::persisted_usage_deltas`, which is
held here to the fold over generated logs so the mirror cannot drift from the rule it copies.
"""
from __future__ import annotations

import math
import random

from looplab.core.llm import CostAccountant
from looplab.core.llm_budget import RunBudget
from looplab.engine.costs import persisted_usage_deltas, seed_prior_spend, seed_run_budget
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


class _Role:
    def __init__(self, accountant):
        self.accountant = accountant


class _Engine:
    """What the two seeders read: a store, and (for the accountant half) a role carrying one."""

    def __init__(self, store, *, accountant=None, budget=None):
        self.store = store
        if accountant is not None:
            self.researcher = _Role(accountant)
        if budget is not None:
            self._llm_budget = budget


def _store(tmp_path, rows):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    for kind, data in rows:
        store.append(kind, data)
    return store


def _seeded(store):
    accountant = CostAccountant(limit=1e6)
    seed_prior_spend(_Engine(store, accountant=accountant))
    budget = RunBudget(cost_limit=1e6)
    seed_run_budget(_Engine(store, budget=budget))
    return accountant.spent, budget.committed_cost


def test_the_reviewers_log_seeds_both_halves_with_what_the_fold_reports(tmp_path):
    store = _store(tmp_path, [
        ("llm_usage", {"usage_id": "a" * 32, "cost": 0.20, "calls": 1, "priced_calls": 1}),
        ("llm_usage", {"usage_id": "a" * 32, "cost": 0.20, "calls": 1, "priced_calls": 1}),
        ("llm_usage", {"cost": 0.30, "calls": 1, "priced_calls": 1}),
    ])
    shown = fold(store.read_all()).llm_cost["cost"]
    accountant_spent, budget_committed = _seeded(store)
    assert math.isclose(shown, 0.50)
    assert math.isclose(accountant_spent, shown), (
        f"the accountants were charged {accountant_spent} for a run the fold says spent {shown}")
    assert math.isclose(budget_committed, shown), (
        f"the budget committed {budget_committed} for a run the fold says spent {shown}")


def test_a_pre_ledger_log_is_seeded_from_its_summary(tmp_path):
    """Before `llm_usage` existed a run recorded only `llm_cost` summaries, and the fold's base is
    the latest one before the first usage row. A resume of such a run used to start at zero."""
    store = _store(tmp_path, [
        ("llm_cost", {"cost": 0.10, "calls": 2}),
        ("llm_cost", {"cost": 0.40, "calls": 5}),
    ])
    shown = fold(store.read_all()).llm_cost["cost"]
    accountant_spent, budget_committed = _seeded(store)
    assert math.isclose(shown, 0.40)
    assert math.isclose(accountant_spent, shown) and math.isclose(budget_committed, shown)


def _generated_log(rng: random.Random) -> list[tuple[str, dict]]:
    """Summaries, id-bearing rows (some repeated verbatim, as outbox recovery repeats them),
    id-less rows, and the junk a hand-edited log carries."""
    ids = [f"{i:032x}" for i in range(rng.randint(1, 6))]
    rows: list[tuple[str, dict]] = []
    for _ in range(rng.randint(0, 14)):
        roll = rng.random()
        cost = rng.choice([0.0, 0.01, 0.125, 0.3, 1.5, -1.0, "x", None, float("nan")])
        calls = rng.choice([0, 1, 2, 7, -3, True, "2"])
        if roll < 0.15:
            rows.append(("llm_cost", {"cost": cost, "calls": calls}))
        elif roll < 0.65:
            usage_id = rng.choice(ids)
            earlier = [d for kind, d in rows if kind == "llm_usage" and d.get("usage_id") == usage_id]
            # a repeat is the same row again — a physical call's identity, re-recorded
            data = dict(earlier[0]) if earlier and rng.random() < 0.7 else {
                "usage_id": usage_id, "cost": cost, "calls": calls,
                "prompt_tokens": rng.choice([0, 10, 250]), "total_tokens": rng.choice([0, 10, 400])}
            rows.append(("llm_usage", data))
        else:
            rows.append(("llm_usage", {"cost": cost, "calls": calls,
                                       "completion_tokens": rng.choice([0, 5, 99])}))
    return rows


def test_the_mirror_agrees_with_the_fold_on_generated_logs(tmp_path):
    rng = random.Random(20260922)
    for case in range(250):
        case_dir = tmp_path / str(case)
        case_dir.mkdir()
        store = _store(case_dir, _generated_log(rng))
        events = store.read_all()
        folded = fold(events).llm_cost or {}           # None: the log never recorded a cost
        mirrored = persisted_usage_deltas(events)
        for key in ("cost", "calls", "prompt_tokens", "completion_tokens", "total_tokens"):
            total = sum(delta[key] for delta in mirrored)
            shown = folded.get(key, 0)
            assert math.isclose(total, shown, rel_tol=1e-9, abs_tol=1e-12), (
                f"case {case}: {key} — the seeders would count {total}, the fold reports {shown}")
