"""P1 hypothesis ledger (this session, Phase 3 — the main bet).

The ledger turns the loop from "propose the next mutation" into "run experiments that resolve open
questions". It is DERIVED by the fold, audit-only, and must NEVER change best-selection. Covered:
- a node whose `idea.hypothesis` is set becomes a tracked hypothesis with the node as evidence;
- the verdict is computed from outcomes: supported (an experiment improved over its parent / became
  best), tested (evaluated, no improvement), testing (evidence still running), open (no evidence);
- an explicit `hypothesis_added` (human / deep-research direction) starts open and later accrues
  evidence when a matching node runs; `hypothesis_updated status=abandoned` overrides;
- the same statement from several ideas links to ONE entry; the ledger never perturbs `best_node_id`.
All offline."""
from __future__ import annotations

from looplab.models import Event, hypothesis_id
from looplab.replay import fold


def _mk(evs):
    return [Event(type=t, data=d) for t, d in evs]


def _run(direction="max", extra=None):
    evs = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": direction}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "a linear baseline is enough"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.80}),
        ("node_created", {"node_id": 2, "operator": "improve", "parent_ids": [1],
                          "idea": {"operator": "improve", "hypothesis": "interaction features help"}}),
        ("node_evaluated", {"node_id": 2, "metric": 0.88}),           # improved over parent -> supported
        ("node_created", {"node_id": 3, "operator": "improve", "parent_ids": [2],
                          "idea": {"operator": "improve", "hypothesis": "a deeper model helps"}}),
        ("node_evaluated", {"node_id": 3, "metric": 0.85}),           # worse than parent(0.88)
        ("node_created", {"node_id": 4, "operator": "improve", "parent_ids": [2],
                          "idea": {"operator": "improve", "hypothesis": "a deeper model helps"}}),
    ]
    return fold(_mk(evs + (extra or [])))


def _by_statement(st):
    return {h.statement: h for h in st.hypotheses.values()}


def test_supported_when_experiment_improves():
    b = _by_statement(_run())
    assert b["interaction features help"].status == "supported"
    assert b["interaction features help"].best_delta > 0


def test_testing_while_evidence_still_running():
    # "a deeper model helps": node 3 evaluated (no improvement) + node 4 pending -> inconclusive
    b = _by_statement(_run())
    h = b["a deeper model helps"]
    assert h.status == "testing" and h.evidence == [3, 4]


def test_tested_when_all_evidence_evaluated_without_improvement():
    # add node 4's (failing) evaluation -> now all evidence evaluated, none improved -> tested
    b = _by_statement(_run(extra=[("node_evaluated", {"node_id": 4, "metric": 0.86})]))
    assert b["a deeper model helps"].status == "tested"


def test_explicit_added_hypothesis_starts_open():
    b = _by_statement(_run(extra=[("hypothesis_added",
                                   {"statement": "external data raises accuracy",
                                    "source": "deep_research"})]))
    h = b["external data raises accuracy"]
    assert h.status == "open" and h.source == "deep_research" and h.evidence == []


def test_added_hypothesis_accrues_evidence_from_matching_node():
    stmt = "interaction features help"
    # an explicit add for a statement a node later tests -> merges to ONE entry with the node evidence
    st = _run(extra=[("hypothesis_added", {"statement": stmt, "source": "human"})])
    matches = [h for h in st.hypotheses.values() if h.statement == stmt]
    assert len(matches) == 1 and 2 in matches[0].evidence and matches[0].status == "supported"


def test_abandon_override():
    hid = hypothesis_id("a deeper model helps")
    st = _run(extra=[("hypothesis_updated", {"id": hid, "status": "abandoned"})])
    assert st.hypotheses[hid].status == "abandoned"


def test_ledger_never_changes_best_selection():
    st = _run()
    assert st.best_node_id == 2                    # purely the metric winner; ledger is audit-only
    # a re-fold is deterministic
    assert {h.statement: h.status for h in _run().hypotheses.values()} == \
           {h.statement: h.status for h in st.hypotheses.values()}
