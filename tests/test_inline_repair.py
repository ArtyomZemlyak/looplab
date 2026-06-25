"""Hybrid in-node crash repair: agent/rule triage + inline repair within one node.

Covers the replay-safety of the new `node_repaired` event, the inline-repair attempt loop in
`_evaluate`, the deterministic rule-based triage fallback, and the `idea_rejected` lineage
suppression in `debug_action`.
"""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.eventstore import EventStore
from looplab.models import Idea, Node, NodeStatus, RunState
from looplab.orchestrator import Engine, _rule_triage
from looplab.policy import GreedyTree, debug_action
from looplab.replay import fold
from looplab.sandbox import SubprocessSandbox
from looplab.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"

# A solution that raises a MECHANICAL crash (ModuleNotFoundError) — the rule-based triage treats
# this as repairable in place.
_BAD = "import definitely_not_a_real_module_zzz\n"
_GOOD = "import json; print(json.dumps({'metric': 0.1}))\n"


class _Stub:
    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})


class _MechCrashThenFixed:
    """Crashes mechanically on first run, then repair() returns a working script."""
    def __init__(self):
        self.repair_calls = 0

    def implement(self, idea):
        return _BAD

    def repair(self, idea, code, error):
        self.repair_calls += 1
        return _GOOD


class _AlwaysMechCrash:
    """Every attempt (implement and repair) crashes mechanically — exercises the attempt bound."""
    def __init__(self):
        self.repair_calls = 0

    def implement(self, idea):
        return _BAD

    def repair(self, idea, code, error):
        self.repair_calls += 1
        return _BAD


def _engine(run_dir, dev, **kw):
    return Engine(run_dir, task=ToyTask.load(TASK), researcher=_Stub(), developer=dev,
                  sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=1, max_nodes=4, debug_depth=1), **kw)


def _events(run_dir):
    return list(EventStore(Path(run_dir) / "events.jsonl").read_all())


# --------------------------------------------------------------------------- replay safety
def test_node_repaired_folds_final_code_once(tmp_path):
    """node_created(BAD) -> node_repaired(GOOD) -> node_evaluated folds to GOOD/evaluated, and the
    eval cost is counted exactly once. Re-folding is identical (determinism)."""
    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": ""},
                              "code": _BAD})
    s.append("node_repaired", {"node_id": 0, "attempt": 1, "code": _GOOD})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.1, "eval_seconds": 3.0})

    a = fold(EventStore(p).read_all())
    b = fold(EventStore(p).read_all())
    assert a.model_dump() == b.model_dump()
    assert a.nodes[0].code == _GOOD
    assert a.nodes[0].status is NodeStatus.evaluated
    assert a.total_eval_seconds == 3.0


def test_node_repaired_after_terminal_is_noop(tmp_path):
    """A stray/corrupt node_repaired AFTER the terminal event must not mutate the (now non-pending)
    node — mirrors the first_terminal idempotency guard."""
    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""},
                              "code": _GOOD})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.1, "eval_seconds": 1.0})
    s.append("node_repaired", {"node_id": 0, "attempt": 9, "code": "print('hijacked')"})

    st = fold(EventStore(p).read_all())
    assert st.nodes[0].code == _GOOD          # unchanged: node already terminal
    assert st.nodes[0].status is NodeStatus.evaluated


# --------------------------------------------------------------------------- engine: happy path
def test_inline_repair_fixes_in_place_without_new_node(tmp_path):
    dev = _MechCrashThenFixed()
    eng = _engine(tmp_path / "on", dev, inline_repair=True, inline_repair_attempts=1)
    anyio.run(eng.run)

    evs = _events(tmp_path / "on")
    repaired = [e for e in evs if e.type == "node_repaired"]
    assert repaired, "expected an inline node_repaired event"
    assert dev.repair_calls >= 1

    st = fold(evs)
    n0 = st.nodes[0]
    assert n0.status is NodeStatus.evaluated      # repaired in place -> evaluated
    assert n0.code == _GOOD
    # The repair did NOT add a debug node for node 0 (inline repair never creates a tree node).
    assert not any(n.operator == "debug" and 0 in n.parent_ids for n in st.nodes.values())


def test_inline_repair_off_restores_debug_node(tmp_path):
    """With inline_repair=False the crash fails normally and the inter-node debug operator repairs
    it via a NEW node (the prior behavior)."""
    dev = _MechCrashThenFixed()
    eng = _engine(tmp_path / "off", dev, inline_repair=False)
    anyio.run(eng.run)

    evs = _events(tmp_path / "off")
    assert not any(e.type == "node_repaired" for e in evs)
    assert any(e.type == "node_failed" and e.data.get("reason") == "crash" for e in evs)
    st = fold(evs)
    assert any(n.operator == "debug" for n in st.nodes.values())   # a debug node was created


def test_inline_repair_attempt_bound(tmp_path):
    """A node that keeps crashing emits exactly `inline_repair_attempts` node_repaired events, then
    fails normally and stays eligible for the budgeted inter-node debug operator."""
    dev = _AlwaysMechCrash()
    eng = _engine(tmp_path / "bound", dev, inline_repair=True, inline_repair_attempts=2)
    anyio.run(eng.run)

    evs = _events(tmp_path / "bound")
    repaired_n0 = [e for e in evs if e.type == "node_repaired" and e.data.get("node_id") == 0]
    assert len(repaired_n0) == 2                 # bounded by inline_repair_attempts
    failed_n0 = [e for e in evs if e.type == "node_failed" and e.data.get("node_id") == 0]
    assert failed_n0 and failed_n0[0].data.get("reason") == "crash"


# --------------------------------------------------------------------------- rule-based triage
def test_rule_triage_repairs_mechanical_only():
    assert _rule_triage("crash", "ModuleNotFoundError: no module", 1, 1)["action"] == "repair"
    assert _rule_triage("crash", "TypeError: unexpected keyword argument 'multi_class'",
                        1, 2)["action"] == "repair"
    # Non-mechanical crash -> abandon (never reject_idea from the rule).
    assert _rule_triage("crash", "AssertionError: metric too low", 1, 2)["action"] == "abandon"
    # Attempts exhausted -> abandon even if mechanical.
    assert _rule_triage("crash", "ImportError: x", 2, 1)["action"] == "abandon"


# --------------------------------------------------------------------------- idea_rejected gating
def test_idea_rejected_lineage_skipped_by_debug_action():
    st = RunState(run_id="r", task_id="t", direction="min")
    st.nodes[0] = Node(id=0, parent_ids=[], operator="draft",
                       idea=Idea(operator="draft", params={}),
                       status=NodeStatus.failed, error="boom", error_reason="idea_rejected")
    assert debug_action(st, debug_depth=1) is None     # rejected idea is not debugged
    # A plain crash leaf IS debugged.
    st.nodes[0].error_reason = "crash"
    act = debug_action(st, debug_depth=1)
    assert act and act["parent_id"] == 0
