"""Agent governance (Settings.agent_control): which roles (strategist/boss/researcher) may change a
setting at runtime. Covers the matrix gate, the Researcher's per-node eval_timeout (the neural-net
case), the Strategist's run-wide timeout/max_parallel, and the boss budget_extend fold.
"""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.core.models import Idea, RunState
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.events.replay import fold
from looplab.runtime.sandbox import RunResult
from looplab.agents.strategist import StrategyContext, validate_strategy
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


class _RecordSandbox:
    """Records the wall-clock budget each eval is launched with; always returns a clean metric."""
    def __init__(self):
        self.timeouts: list[float] = []

    def run(self, code, workdir, timeout=30.0, env=None, cancel=None):
        self.timeouts.append(timeout)
        return RunResult(exit_code=0, stdout='{"metric": 0.1}', stderr="", metric=0.1, timed_out=False)


class _Researcher:
    """Proposes a single Idea carrying a per-node eval_timeout (the heavy-experiment case)."""
    def __init__(self, eval_timeout=None):
        self.eval_timeout = eval_timeout

    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0}, eval_timeout=self.eval_timeout)


class _Dev:
    def implement(self, idea):
        return "print('{\"metric\": 0.1}')\n"


def _engine(run_dir, *, eval_timeout=None, timeout=30.0, agent_control=None, sandbox=None):
    return Engine(run_dir, task=ToyTask.load(TASK), researcher=_Researcher(eval_timeout),
                  developer=_Dev(), sandbox=sandbox or _RecordSandbox(), timeout=timeout,
                  policy=GreedyTree(n_seeds=1, max_nodes=1, debug_depth=0),
                  agent_control=agent_control, auto_install_deps=False)


# ----------------------------------------------------------------- the gate
def test_agent_may_matrix_gate():
    eng = _engine(Path("/tmp/x_unused"), agent_control={"timeout": ["researcher"], "policy": ["strategist"]})
    assert eng._agent_may("researcher", "timeout") is True
    assert eng._agent_may("strategist", "timeout") is False        # not in the list
    assert eng._agent_may("strategist", "policy") is True
    assert eng._agent_may("boss", "max_parallel") is False         # absent key => locked for everyone
    assert eng._agent_may("researcher", "nonexistent") is False


# ----------------------------------------------------- researcher per-node eval timeout
def test_researcher_eval_timeout_used_when_granted(tmp_path):
    sb = _RecordSandbox()
    eng = _engine(tmp_path / "on", eval_timeout=123.0, timeout=30.0,
                  agent_control={"timeout": ["researcher"]}, sandbox=sb)
    anyio.run(eng.run)
    assert sb.timeouts == [123.0]            # the researcher-sized per-node budget was applied


def test_researcher_eval_timeout_ignored_when_not_granted(tmp_path):
    sb = _RecordSandbox()
    eng = _engine(tmp_path / "off", eval_timeout=123.0, timeout=30.0,
                  agent_control={"timeout": ["strategist"]}, sandbox=sb)   # researcher NOT granted
    anyio.run(eng.run)
    assert sb.timeouts == [30.0]             # falls back to the run-wide default


def test_no_eval_timeout_uses_run_default(tmp_path):
    sb = _RecordSandbox()
    eng = _engine(tmp_path / "none", eval_timeout=None, timeout=42.0,
                  agent_control={"timeout": ["researcher"]}, sandbox=sb)
    anyio.run(eng.run)
    assert sb.timeouts == [42.0]


# ----------------------------------------------------- strategist run-wide retune
def test_strategist_applies_timeout_and_parallel_when_granted(tmp_path):
    eng = _engine(tmp_path / "s_on", timeout=30.0,
                  agent_control={"timeout": ["strategist"], "max_parallel": ["strategist"]})
    eng._apply_strategy({"timeout": 99.0, "max_parallel": 4})
    assert eng.timeout == 99.0 and eng.max_parallel == 4


def test_strategist_blocked_when_not_granted(tmp_path):
    eng = _engine(tmp_path / "s_off", timeout=30.0, agent_control={})   # nothing granted
    eng._apply_strategy({"timeout": 99.0, "max_parallel": 4})
    assert eng.timeout == 30.0 and eng.max_parallel == 1               # unchanged


def test_validate_strategy_whitelists_resource_budgets():
    ctx = StrategyContext(available_policies=["greedy"], available_developers=["default"])
    out = validate_strategy({"timeout": 120.0, "max_parallel": 3}, ctx)
    assert out["timeout"] == 120.0 and out["max_parallel"] == 3
    # bad shapes dropped
    assert "timeout" not in (validate_strategy({"timeout": -5}, ctx) or {})
    assert "max_parallel" not in (validate_strategy({"max_parallel": 0}, ctx) or {})
    assert "max_parallel" not in (validate_strategy({"max_parallel": True}, ctx) or {})   # bool != int budget


# ----------------------------------------------------- boss budget_extend fold
def test_budget_extend_folds_timeout_and_parallel(tmp_path):
    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    s.append("budget_extend", {"timeout": 600.0, "max_parallel": 5, "max_eval_seconds": 1000.0})
    st = fold(EventStore(p).read_all())
    assert st.budget_overrides["timeout"] == 600.0
    assert st.budget_overrides["max_parallel"] == 5
    assert st.budget_overrides["max_eval_seconds"] == 1000.0


def test_default_settings_matrix_shape():
    s = Settings()
    assert s.agent_control["timeout"] == ["researcher", "strategist"]
    assert "boss" in s.agent_control["max_nodes"]      # budget_extend was already a boss power
    assert "llm_model" not in s.agent_control           # infra stays locked
