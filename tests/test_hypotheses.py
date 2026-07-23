"""P1 belief ledger — now the single Card board (1 card = 1 hypothesis).

The ledger turns the loop from "propose the next mutation" into "run experiments that resolve open
questions". It is DERIVED by the fold onto `st.cards` (verdict == the old hypothesis status), audit-only,
and must NEVER change best-selection. Covered:
- a node whose `idea.hypothesis` is set becomes a tracked hypothesis with the node as evidence;
- the verdict is computed from outcomes: supported (an experiment improved over its parent / became
  best), tested (evaluated, no improvement), testing (evidence still running), open (no evidence);
- an explicit `hypothesis_added` (human / deep-research direction) starts open and later accrues
  evidence when a matching node runs; `hypothesis_updated status=abandoned` overrides;
- the same statement from several ideas links to ONE entry; the ledger never perturbs `best_node_id`.
All offline."""
from __future__ import annotations

from looplab.core.models import Event, hypothesis_id
from looplab.events.replay import fold


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
    return {h.statement: h for h in st.cards.values()}


def test_supported_when_experiment_improves():
    b = _by_statement(_run())
    assert b["interaction features help"].verdict == "supported"
    assert b["interaction features help"].best_delta > 0


def test_testing_while_evidence_still_running():
    # "a deeper model helps": node 3 evaluated (no improvement) + node 4 pending -> inconclusive
    b = _by_statement(_run())
    h = b["a deeper model helps"]
    assert h.verdict == "testing" and h.evidence == [3, 4]


def test_tested_when_all_evidence_evaluated_without_improvement():
    # add node 4's (failing) evaluation -> now all evidence evaluated, none improved -> tested
    b = _by_statement(_run(extra=[("node_evaluated", {"node_id": 4, "metric": 0.86})]))
    assert b["a deeper model helps"].verdict == "tested"


def test_explicit_added_hypothesis_starts_open():
    b = _by_statement(_run(extra=[("hypothesis_added",
                                   {"statement": "external data raises accuracy",
                                    "source": "deep_research"})]))
    h = b["external data raises accuracy"]
    assert h.verdict == "open" and h.source == "deep_research" and h.evidence == []


def test_added_hypothesis_accrues_evidence_from_matching_node():
    stmt = "interaction features help"
    # an explicit add for a statement a node later tests -> merges to ONE entry with the node evidence
    st = _run(extra=[("hypothesis_added", {"statement": stmt, "source": "human"})])
    matches = [h for h in st.cards.values() if h.statement == stmt]
    assert len(matches) == 1 and 2 in matches[0].evidence and matches[0].verdict == "supported"


def test_abandon_override():
    hid = hypothesis_id("a deeper model helps")
    st = _run(extra=[("hypothesis_updated", {"id": hid, "status": "abandoned"})])
    assert st.cards[hid].verdict == "abandoned"


def test_ledger_never_changes_best_selection():
    st = _run()
    assert st.best_node_id == 2                    # purely the metric winner; ledger is audit-only
    # a re-fold is deterministic
    assert {h.statement: h.verdict for h in _run().cards.values()} == \
           {h.statement: h.verdict for h in st.cards.values()}


# ── deep-review round: malformed entries, failed-evidence, re-adding an abandoned hypothesis ──────

def test_malformed_hypothesis_added_does_not_brick_fold():
    # A scripted API client can append any JSON via the control endpoint; one malformed entry
    # must not make every later fold of the run raise.
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("hypothesis_added", {"statement": "x helps", "at_node": "soon"}),      # non-numeric at_node
        ("hypothesis_added", {"statement": "y helps", "id": 5}),                # non-string id
        ("hypothesis_added", {"statement": "z helps", "source": {"who": "me"}}),  # non-string source
    ]))
    stmts = {h.statement for h in st.cards.values()}
    assert {"x helps", "y helps", "z helps"} <= stmts


def test_failed_evidence_hypothesis_returns_to_open_not_testing():
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "bigger net helps"}}),
        ("node_failed", {"node_id": 1, "reason": "crash"}),
        ("run_finished", {"reason": "done"}),
    ]))
    h = st.cards[hypothesis_id("bigger net helps")]
    assert h.verdict == "open"          # not "testing": nothing is running in a finished run


def test_re_adding_abandoned_hypothesis_reopens_it():
    hid = hypothesis_id("try polars")
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("hypothesis_added", {"statement": "try polars", "id": hid}),
        ("hypothesis_updated", {"id": hid, "status": "abandoned"}),
        ("hypothesis_added", {"statement": "try polars", "id": hid}),   # mis-click undo: re-add
    ]))
    assert st.cards[hid].verdict == "open"
    # and an explicit non-abandoned status update also clears the override
    st2 = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("hypothesis_added", {"statement": "try polars", "id": hid}),
        ("hypothesis_updated", {"id": hid, "status": "abandoned"}),
        ("hypothesis_updated", {"id": hid, "status": "open"}),
    ]))
    assert st2.cards[hid].verdict == "open"
