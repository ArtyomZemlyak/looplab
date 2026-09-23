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


def _log(tmp_path, name, *, nodes, direction="max", trust_gate="audit", hacks=(), **started):
    rd = tmp_path / name
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": name, "task_id": "t", "goal": "g",
                                 "direction": direction, "trust_gate": trust_gate, **started})
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


def _append(rd, *rows):
    store = EventStore(rd / "events.jsonl")
    for event_type, data in rows:
        store.append(event_type, data)


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
    """Two polls of an unchanged run publish one record — and the tie is broken the way the SELECTOR
    breaks it, because the intended node is the selector's own pick (review 2026-09-22, ENG2-09):
    `SearchFitness.best` over `(robust_metric, id)`, which on a `max` run is the HIGHER id. A tie
    rule of this function's own would name a node the selector would never crown."""
    rd = _log(tmp_path, "tie", nodes=[{"id": 2, "metric": 0.6}, {"id": 1, "metric": 0.6},
                                      {"id": 0, "metric": 0.9}], hacks=[(0, "grader_access")])
    gap = mislead_gap(_state(rd))
    assert gap == mislead_gap(_state(rd)), "one record per unchanged log"
    _append(rd, ("node_tombstoned", {"node_ids": [0]}))
    assert gap["intended_node"] == _state(rd).best_node_id == 2, (
        "the intended pick broke the tie differently from the selector")


# ------------------------------------------ the SELECTOR's selection (review 2026-09-22, ENG2-09)
#
# `mislead_gap` used to spell its own population (every node carrying a metric — tombstoned and
# aborted ones included) and its own selection (the raw-metric maximum), while the champion beside
# it is crowned by `replay.py::_select_best` over `promotion_eligible_nodes` with the confirmed mean,
# the confirm certificate, the holdout pick and the verifier. So a negative gap — which the docstring
# called impossible — was routine: every confirm-phase demotion, every tombstone, every holdout pick.
# Reproduced by review/ENG2/repro_mislead.py: gap -0.05 on a clean confirmed run and on a tombstone.
# The intended node is now the selector's OWN pick over the intended population
# (`replay.py::select_champion` over `promotion_eligible_nodes` with the hard flags, minus salvages).

def _two_nodes(tmp_path, name, **started):
    """The reviewer's shape: a seed-lucky leader (0.90) beside a steadier runner-up (0.85)."""
    return _log(tmp_path, name, nodes=[{"id": 0, "metric": 0.90}, {"id": 1, "metric": 0.85}],
                **started)


def test_a_confirm_phase_demotion_is_not_an_inflation(tmp_path):
    rd = _two_nodes(tmp_path, "confirm")
    _append(rd, ("node_confirmed", {"node_id": 0, "generation": 0, "mean": 0.80, "std": 0.01,
                                    "seeds": 3}),
            ("node_confirmed", {"node_id": 1, "generation": 0, "mean": 0.84, "std": 0.01,
                                "seeds": 3}))
    state = _state(rd)
    assert state.best_node_id == 1, "precondition: the confirm phase demoted the lucky leader"
    gap = mislead_gap(state)
    assert gap["intended_node"] == 1 and gap["gap"] == 0.0, (
        f"a clean run whose champion IS an intended node published a gap: {gap}")
    assert gap["excluded"] == 0 and gap["scored"] == 2


def test_a_tombstoned_node_is_in_neither_population(tmp_path):
    rd = _two_nodes(tmp_path, "tombstone")
    _append(rd, ("node_tombstoned", {"node_ids": [0]}))
    state = _state(rd)
    assert state.best_node_id == 1 and state.nodes[0].tombstoned
    gap = mislead_gap(state)
    assert gap["intended_node"] == 1 and gap["gap"] == 0.0, gap
    assert gap["scored"] == 1 and gap["excluded"] == 0, (
        "a logically-deleted node is not part of the run's scored population")


def test_an_aborted_node_is_in_neither_population(tmp_path):
    rd = _two_nodes(tmp_path, "aborted")
    _append(rd, ("node_abort", {"node_id": 0, "generation": 0}))
    state = _state(rd)
    assert state.best_node_id == 1 and 0 in state.aborted_nodes
    gap = mislead_gap(state)
    assert gap["intended_node"] == 1 and gap["gap"] == 0.0, gap
    assert gap["scored"] == 1 and gap["excluded"] == 0


def test_a_holdout_elected_champion_is_the_intended_one(tmp_path):
    rd = _two_nodes(tmp_path, "holdout", holdout_select=True)
    _append(rd, ("holdout_evaluated", {"node_id": 0, "generation": 0, "metric": 0.70}),
            ("holdout_evaluated", {"node_id": 1, "generation": 0, "metric": 0.80}))
    state = _state(rd)
    assert state.best_node_id == 1, "precondition: the unseen partition elected the runner-up"
    gap = mislead_gap(state)
    assert gap["intended_node"] == 1 and gap["gap"] == 0.0, gap


def test_the_intended_node_is_the_selectors_pick_among_the_intended_nodes(tmp_path):
    """A flagged champion under `audit`, on a confirmed run: the intended protocol would have
    crowned the best CONFIRMED MEAN among the honest nodes (node 1), not their best raw number
    (node 0, the seed-lucky one) — and the gap is measured to the node it would have published."""
    rd = _log(tmp_path, "flagged-confirmed", nodes=[
        {"id": 0, "metric": 0.90}, {"id": 1, "metric": 0.85}, {"id": 2, "metric": 0.95}],
        hacks=[(2, "grader_access")])
    _append(rd, *[("node_confirmed", {"node_id": nid, "generation": 0, "mean": mean, "std": 0.01,
                                      "seeds": 3})
                  for nid, mean in ((0, 0.70), (1, 0.80), (2, 0.93))])
    state = _state(rd)
    assert state.best_node_id == 2, "precondition: audit crowns the flagged node"
    gap = mislead_gap(state)
    assert (gap["exploit_node"], gap["intended_node"]) == (2, 1), gap
    assert gap["intended"] == 0.85 and gap["gap"] == pytest.approx(0.10), gap


def test_the_confirm_certificate_decides_the_intended_node_as_it_decides_the_champion(tmp_path):
    """The certificate is part of the selector: an operator-approved flagged champion, and a
    confirm certificate naming node 0 among the honest nodes. Without the approval the fold would
    crown node 0 — so that is the intended node, not the best confirmed mean (node 1)."""
    rd = _log(tmp_path, "certificate", nodes=[
        {"id": 0, "metric": 0.90}, {"id": 1, "metric": 0.85}, {"id": 2, "metric": 0.95}],
        hacks=[(2, "grader_access")])
    _append(rd, *[("node_confirmed", {"node_id": nid, "generation": 0, "mean": mean, "std": 0.01,
                                      "seeds": 3})
                  for nid, mean in ((0, 0.70), (1, 0.80), (2, 0.93))])
    _append(rd, ("best_confirmed", {"node_id": 0, "significant": True}),
            ("approval_granted", {"node_id": 2, "generation": 0}))
    state = _state(rd)
    assert state.best_node_id == 2, "precondition: the approval crowned the flagged node"
    gap = mislead_gap(state)
    assert gap["intended_node"] == 0 and gap["gap"] == pytest.approx(0.05), gap


def test_a_removed_node_is_never_the_intended_pick(tmp_path):
    """The population half, where it DECIDES something: a flagged champion under `audit`, and the
    honest node with the best number was tombstoned by the operator. The intended protocol would
    have published the best LIVE honest node (node 1), never a logically-deleted one."""
    rd = _log(tmp_path, "removed", nodes=[
        {"id": 0, "metric": 0.90}, {"id": 1, "metric": 0.85}, {"id": 2, "metric": 0.95}],
        hacks=[(2, "grader_access")])
    _append(rd, ("node_tombstoned", {"node_ids": [0]}))
    state = _state(rd)
    assert state.best_node_id == 2, "precondition: audit crowns the flagged node"
    gap = mislead_gap(state)
    assert gap["intended_node"] == 1 and gap["gap"] == pytest.approx(0.10), gap
    assert gap["scored"] == 2 and gap["excluded"] == 1


@pytest.mark.parametrize("trust_gate", ["audit", "gate"])
def test_a_certificate_naming_an_excluded_node_crowns_nothing_outside_the_pool(tmp_path,
                                                                                 trust_gate):
    """The certificate is honoured only INSIDE the population the selector is asked about. Under
    `gate` the fold's own pool excludes the flagged node the certificate names, so the champion is
    the best eligible one; under `audit` the certificate crowns it, and the intended pick — asked of
    the honest nodes only — must not be it either."""
    rd = _log(tmp_path, f"cert-{trust_gate}", trust_gate=trust_gate, nodes=[
        {"id": 0, "metric": 0.90}, {"id": 1, "metric": 0.85}, {"id": 2, "metric": 0.95}],
        hacks=[(2, "grader_access")])
    _append(rd, *[("node_confirmed", {"node_id": nid, "generation": 0, "mean": mean, "std": 0.01,
                                      "seeds": 3})
                  for nid, mean in ((0, 0.70), (1, 0.80), (2, 0.60))])
    _append(rd, ("best_confirmed", {"node_id": 2, "significant": True}))
    state = _state(rd)
    if trust_gate == "gate":
        assert state.best_node_id == 1, "a certificate crowned a node the gate excludes"
    else:
        assert state.best_node_id == 2, "precondition: under audit the certificate crowns node 2"
    gap = mislead_gap(state)
    assert gap["intended_node"] == 1, gap


@pytest.mark.parametrize("confirmed,holdout", [(False, False), (True, False), (True, True)])
def test_the_intended_node_is_the_champion_the_fold_crowns_without_the_excluded_nodes(
        tmp_path, confirmed, holdout):
    """THE PROPERTY, driven through the real fold: the same run WITHOUT the nodes the record says
    something against (a flagged one and a `select`-admitted salvage) crowns the intended node. One
    selector, so the pair cannot disagree with the rung that crowns the champion. (A second log
    that never held them, rather than a tombstone: removing a holdout-scored node invalidates the
    disclosed holdout, which would change the question.)"""
    nodes = {0: {"id": 0, "metric": 0.90}, 1: {"id": 1, "metric": 0.85},
             2: {"id": 2, "metric": 0.95},
             3: {"id": 3, "metric": 0.97, "violations": SALVAGE.violation_rows("select"),
                 "provenance": SALVAGE.as_event()}}
    means = {0: 0.70, 1: 0.80, 2: 0.93, 3: 0.96}
    unseen = {0: 0.75, 1: 0.71, 2: 0.99, 3: 0.98}

    def _run(name, ids):
        rd = _log(tmp_path, name, nodes=[nodes[i] for i in ids],
                  hacks=[(2, "grader_access")] if 2 in ids else [],
                  **({"holdout_select": True} if holdout else {}))
        if confirmed:
            _append(rd, *[("node_confirmed", {"node_id": i, "generation": 0, "mean": means[i],
                                              "std": 0.01, "seeds": 3}) for i in ids])
        if holdout:
            _append(rd, *[("holdout_evaluated", {"node_id": i, "generation": 0,
                                                 "metric": unseen[i]}) for i in ids])
        return _state(rd)

    tag = f"{confirmed}-{holdout}"
    gap = mislead_gap(_run(f"full-{tag}", (0, 1, 2, 3)))
    assert gap["exploit_node"] in (2, 3), "precondition: a non-intended node is the champion"
    honest = _run(f"honest-{tag}", (0, 1))
    assert gap["intended_node"] == honest.best_node_id, (gap, honest.best_node_id)
    assert gap["intended"] == honest.best().metric
