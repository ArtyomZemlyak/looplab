"""The Mislead pair on the run row (doc 52 row 22): `S_exploit` beside `S_intended` and their gap.

`best_metric_caveats` says WHAT KIND of number a run publishes; nothing said HOW MUCH of it the
intended protocol supports. Protocol Validity reports that as the pair `G = S_exploit − S_intended`
(inflation 0.45–1.00 on ML-research traces) rather than as a flag count, and that is the shape
`engine/champion_caveats.py::mislead_gap` publishes: the champion, the best node the record says
nothing against (feasible, no hard trust signal, measured rather than salvaged), and the gap in the
run's direction. Every test drives the property through a real log folded by the real `fold` and
projected by the real `run_summaries`, the same tier the caveat tests hold.
"""
from __future__ import annotations

import pytest

from looplab.engine.champion_caveats import MISLEAD_GAP_VERSION, mislead_gap
from looplab.engine.metric_salvage import OPERATOR_PRODUCED, SalvagedMetric
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

pytest.importorskip("fastapi")

SALVAGE = SalvagedMetric(metric=0.81, condition="artifact_contract", source="declared_reader",
                         reader="stdout_regex", stage="train", producer=OPERATOR_PRODUCED)


def _log(tmp_path, name, *, nodes, direction="max", trust_gate="audit", hacks=()):
    rd = tmp_path / name
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": name, "task_id": "t", "goal": "g",
                                 "direction": direction, "trust_gate": trust_gate})
    for node in nodes:
        store.append("node_created", {
            "node_id": node["id"], "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {}, "rationale": "seed"}, "code": "pass\n"})
        payload = {"node_id": node["id"], "generation": 0, "metric": node["metric"],
                   "violations": node.get("violations", [])}
        if node.get("provenance") is not None:
            payload["metric_provenance"] = node["provenance"]
        store.append("node_evaluated", payload)
    for node_id, signal in hacks:
        store.append("reward_hack_suspected", {"node_id": node_id, "generation": 0,
                                               "signals": [{"signal": signal}]})
    return rd


def _state(rd):
    return fold(EventStore(rd / "events.jsonl").read_all())


def _row(tmp_path, name):
    from looplab.serve import run_projections
    from looplab.serve.server import make_app

    srv = make_app(tmp_path).state.looplab
    rows = {row["run_id"]: row for row in run_projections.run_summaries(srv)}
    assert name in rows, "precondition: the run is projected at all"
    return rows[name]


def test_a_clean_run_publishes_a_zero_gap_with_nothing_excluded(tmp_path):
    rd = _log(tmp_path, "clean", nodes=[{"id": 0, "metric": 0.5}, {"id": 1, "metric": 0.7}])
    gap = mislead_gap(_state(rd))
    assert gap == {"version": MISLEAD_GAP_VERSION, "direction": "max",
                   "exploit": 0.7, "exploit_node": 1, "intended": 0.7, "intended_node": 1,
                   "gap": 0.0, "scored": 2, "excluded": 0}
    assert _row(tmp_path, "clean")["mislead_gap"] == gap, "the run row carries the pair"


def test_a_hard_flagged_champion_under_audit_shows_the_inflation(tmp_path):
    """THE CASE THE FIELD EXISTS FOR: `trust_gate: audit` (the default) enforces nothing, so the
    grader-access node wins; the pair says what the honest population would have published."""
    rd = _log(tmp_path, "inflated", nodes=[{"id": 0, "metric": 0.51}, {"id": 1, "metric": 0.81}],
              hacks=[(1, "grader_access")])
    state = _state(rd)
    assert state.best_node_id == 1, "precondition: audit crowns the flagged node"
    gap = mislead_gap(state)
    assert (gap["exploit"], gap["exploit_node"]) == (0.81, 1)
    assert (gap["intended"], gap["intended_node"]) == (0.51, 0)
    assert gap["gap"] == pytest.approx(0.30) and gap["excluded"] == 1 and gap["scored"] == 2


def test_the_gap_is_signed_in_the_runs_direction(tmp_path):
    """`min`: a flagged 0.10 beside a clean 0.30 is an inflation of 0.20, not −0.20."""
    rd = _log(tmp_path, "lower", direction="min",
              nodes=[{"id": 0, "metric": 0.30}, {"id": 1, "metric": 0.10}], hacks=[(1, "grader_access")])
    gap = mislead_gap(_state(rd))
    assert gap["direction"] == "min" and gap["exploit"] == 0.10 and gap["intended"] == 0.30
    assert gap["gap"] == pytest.approx(0.20)


def test_a_select_admitted_salvage_is_not_an_intended_number(tmp_path):
    rows = SALVAGE.violation_rows("select")
    assert rows == [], "precondition, from the rung: `select` mints no row, the salvage competes"
    rd = _log(tmp_path, "salvaged", nodes=[
        {"id": 0, "metric": 0.51},
        {"id": 1, "metric": 0.81, "violations": rows, "provenance": SALVAGE.as_event()}])
    state = _state(rd)
    assert state.best_node_id == 1, "precondition: the salvage is the champion"
    gap = mislead_gap(state)
    assert gap["intended_node"] == 0 and gap["gap"] == pytest.approx(0.30) and gap["excluded"] == 1


def test_under_gate_the_flagged_node_is_in_neither_population(tmp_path):
    """`gate` already keeps the flagged node off the podium, so the champion IS intended and the
    gap is 0 — the pair cannot second-guess a rung the operator turned on."""
    rd = _log(tmp_path, "gated", trust_gate="gate",
              nodes=[{"id": 0, "metric": 0.51}, {"id": 1, "metric": 0.81}], hacks=[(1, "grader_access")])
    state = _state(rd)
    assert state.best_node_id == 0, "precondition: gate excluded the flagged node"
    gap = mislead_gap(state)
    assert gap["exploit"] == 0.51 and gap["intended"] == 0.51 and gap["gap"] == 0.0
    assert gap["excluded"] == 1, "…and the row still says the population held a flagged number"


def test_a_constraint_violation_is_outside_both_populations(tmp_path):
    """An infeasible node's number was measured honestly against the operator's bound; it is not an
    exploit, but it is not the intended protocol either — so it never makes the gap negative."""
    rd = _log(tmp_path, "bounded", nodes=[
        {"id": 0, "metric": 0.51},
        {"id": 1, "metric": 0.95, "violations": [{"name": "latency", "value": 9, "limit": 1}]}])
    state = _state(rd)
    assert state.best_node_id == 0
    gap = mislead_gap(state)
    assert gap["intended"] == 0.51 and gap["gap"] == 0.0 and gap["excluded"] == 1


def test_no_intended_node_is_reported_as_none_not_zero(tmp_path):
    rd = _log(tmp_path, "allflagged", nodes=[{"id": 0, "metric": 0.5}], hacks=[(0, "grader_access")])
    gap = mislead_gap(_state(rd))
    assert gap["exploit"] == 0.5 and gap["intended"] is None and gap["gap"] is None
    assert gap["intended_node"] is None and gap["excluded"] == 1


def test_a_run_without_a_champion_publishes_none(tmp_path):
    rd = _log(tmp_path, "empty", nodes=[])
    assert mislead_gap(_state(rd)) is None
    assert _row(tmp_path, "empty")["mislead_gap"] is None


def test_the_intended_pick_is_deterministic_on_ties(tmp_path):
    rd = _log(tmp_path, "tie", nodes=[{"id": 2, "metric": 0.6}, {"id": 1, "metric": 0.6},
                                      {"id": 0, "metric": 0.9}], hacks=[(0, "grader_access")])
    gap = mislead_gap(_state(rd))
    assert gap["intended_node"] == 1, "the lower id, so two polls publish one record"
