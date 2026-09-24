"""An operator's eval/LLM width outranks the Strategist's for the rest of the run.

Incident `runs/minionerec-backbones-v10` (2026-09-24): launched at `max_parallel=1`; a Strategist
`strategy_decision` set `eval_parallel: 2`, two 4-GPU-sized evals then ran on ONE GPU each and both
torchrun canaries bound port 29500. The operator's `budget_extend{max_parallel: 1}` was re-applied
at each loop head, but every later `strategy_decision` (merged onto the active strategy, so it carried
`eval_parallel: 2` forward) and every re-entry re-applied the Strategist's width until that head.

Pinned here: a `budget_extend` of a width axis voids the Strategist's grant for that axis — live, on
resume, and whichever order the two were recorded in — and the verdict is derived from the fold alone.
"""
from __future__ import annotations

from looplab.core.models import Idea, Node, NodeStatus
from looplab.engine.widths import operator_width_axes
from looplab.events.replay import fold
from tests.factories import make_engine


class _WidenStub:
    def __init__(self):
        self.calls = 0

    def decide(self, state, ctx):
        self.calls += 1
        return {"policy": "mcts", "eval_parallel": 2, "llm_parallel": 4,
                "source": "rule", "rationale": "widen"}


def _started(eng):
    eng.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                     "direction": "min"})


def _consultable(state):
    state.nodes = {0: Node(id=0, operator="draft", idea=Idea(operator="draft"),
                           status=NodeStatus.evaluated, metric=1.0)}
    return state


def test_operator_width_axes_reads_both_spellings_per_axis():
    assert operator_width_axes({}) == frozenset()
    assert operator_width_axes(None) == frozenset()
    assert operator_width_axes({"max_parallel": 1}) == {"eval_parallel"}
    assert operator_width_axes({"eval_parallel": 1, "max_seconds": 5.0}) == {"eval_parallel"}
    assert operator_width_axes({"parallel_build": 2}) == {"llm_parallel"}
    assert operator_width_axes({"timeout": 60.0, "add_nodes": 3}) == frozenset()


def test_budget_extend_width_survives_a_later_strategist_decision(tmp_path):
    stub = _WidenStub()
    eng = make_engine(tmp_path / "live", strategist=stub, strategist_every=1, eval_parallel=1)
    _started(eng)
    eng.store.append("budget_extend", {"max_parallel": 1})
    state = fold(eng.store.read_all())
    eng._apply_control_overrides(state)
    assert eng._eval_parallel == 1
    eng._maybe_consult_strategist(_consultable(state))
    assert stub.calls == 1
    # The decision was recorded (policy moved) and applied — except the operator-owned axis.
    decisions = [e for e in eng.store.read_all() if e.type == "strategy_decision"]
    assert len(decisions) == 1
    recorded = decisions[0].data["strategy"]
    assert recorded["policy"] == "mcts"
    assert "eval_parallel" not in recorded and "max_parallel" not in recorded
    assert recorded["llm_parallel"] == 4             # the axis the operator did NOT touch stays granted
    assert eng._eval_parallel == 1 and eng._llm_parallel == 4
    # A direct application of a strategist width (what `_record_strategy` does) is refused too.
    eng._apply_strategy({"eval_parallel": 2, "max_parallel": 3, "source": "agent", "_pinned": []})
    assert eng._eval_parallel == 1


def test_without_an_operator_width_the_strategist_keeps_its_grant(tmp_path):
    stub = _WidenStub()
    eng = make_engine(tmp_path / "free", strategist=stub, strategist_every=1, eval_parallel=1)
    _started(eng)
    state = fold(eng.store.read_all())
    eng._apply_control_overrides(state)
    eng._maybe_consult_strategist(_consultable(state))
    assert eng._eval_parallel == 2
    recorded = [e for e in eng.store.read_all() if e.type == "strategy_decision"][0].data["strategy"]
    assert recorded["eval_parallel"] == 2


def test_resume_keeps_operator_width_over_an_earlier_recorded_strategist_width(tmp_path):
    """The v10 shape: strategy_decision{eval_parallel: 2} BEFORE the operator's budget_extend, then a
    later strategy_decision still carrying it. Re-entry must not widen, even transiently."""
    eng = make_engine(tmp_path / "resume", eval_parallel=1)
    _started(eng)
    eng.store.append("strategy_decision", {"strategy": {"eval_parallel": 2, "source": "agent",
                                                        "_pinned": []}, "at_node": 1, "ctx": None})
    eng.store.append("budget_extend", {"max_parallel": 1})
    eng.store.append("strategy_decision", {"strategy": {"eval_parallel": 2, "policy": "greedy",
                                                        "source": "agent", "_pinned": []},
                                           "at_node": 2, "ctx": None})
    resumed = make_engine(tmp_path / "resume", eval_parallel=1)
    resumed._reentry_repin()
    assert resumed._eval_parallel == 1
    assert resumed._operator_width_axes == {"eval_parallel"}
    resumed._apply_control_overrides(fold(resumed.store.read_all()))
    assert resumed._eval_parallel == 1
