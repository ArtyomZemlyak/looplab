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


def test_strategist_search_knobs_gated_by_matrix(tmp_path):
    """M4: EVERY strategist knob (not just timeout/max_parallel) is governance-gated. Under an empty
    matrix a policy/operator/novelty change is blocked; granting it in the matrix lets it through."""
    pol0 = tmp_path / "locked"
    locked = _engine(pol0, agent_control={})                          # all locked
    before = locked._policy_name
    locked._apply_strategy({"policy": "mcts", "novelty_stance": "explore",
                            "operators": {"ablate_every": 5}})
    assert locked._policy_name == before                             # policy switch blocked
    assert locked._novelty_stance != "explore"                       # stance blocked
    assert locked._ablate_every != 5                                 # operator blocked

    granted = _engine(tmp_path / "granted",
                      agent_control={"policy": ["strategist"], "novelty_stance": ["strategist"],
                                     "ablate_every": ["strategist"]})
    granted._apply_strategy({"policy": "mcts", "novelty_stance": "explore",
                             "operators": {"ablate_every": 5}})
    assert granted._policy_name == "mcts"                            # granted -> applied
    assert granted._novelty_stance == "explore"
    assert granted._ablate_every == 5


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


def test_budget_extend_is_a_human_intent_applied_regardless_of_matrix(tmp_path):
    """A `budget_extend` is a HUMAN control intent (the boss action-builder only emits add_nodes), so
    its resource fields are applied AS-IS even under a fully-locked matrix — an earlier pass gated
    max_eval_seconds/timeout/max_parallel on the boss grant and silently DROPPED the operator's own
    override, pinning the run to the old cap (code-review). max_seconds was always applied as-is."""
    eng = _engine(tmp_path / "locked", timeout=30.0, agent_control={})   # boss granted NOTHING
    st = RunState()
    st.budget_overrides = {"max_seconds": 111.0, "max_eval_seconds": 222.0,
                           "timeout": 44.0, "max_parallel": 6}
    max_s, max_es = eng._apply_control_overrides(st)
    assert max_s == 111.0                 # operator max_seconds honoured (always was)
    assert max_es == 222.0                # operator max_eval_seconds honoured despite the locked matrix
    assert eng.timeout == 44.0            # operator timeout retune applied as-is
    assert eng.max_parallel == 6          # operator max_parallel retune applied as-is


def test_operator_policy_params_pin_applies_even_when_policy_is_locked(tmp_path):
    """M4/code-review: `policy_params` is EXEMPT when the operator pins it (`_pinned`), independently of
    the `policy` NAME grant. Locking `policy` against the strategist must NOT also drop an operator's
    params-only pin — the policy is REBUILT (with the current name + the pinned params) rather than the
    whole block being skipped as a silent no-op. The old `if pol and may("policy")` gate dropped it."""
    eng = _engine(tmp_path / "pp", agent_control={})     # policy NAME locked against every agent
    name0, pol0 = eng._policy_name, eng.policy
    # Operator pins policy_params alone (the active policy NAME rides in the record). `_pinned` lists
    # exactly the operator-pinned fields; `policy` is NOT pinned, so its name grant stays locked.
    eng._apply_strategy({"policy": name0, "policy_params": {"c": 0.5},
                         "_pinned": ["policy_params"]})
    assert eng._policy_name == name0                     # name unchanged (still locked to the current one)
    assert eng.policy is not pol0                        # but the policy WAS rebuilt (pin took effect)

    # Control: with NO policy_params pin and `policy` locked, the block stays skipped (no rebuild).
    eng2 = _engine(tmp_path / "pp2", agent_control={})
    pol_before = eng2.policy
    eng2._apply_strategy({"policy": eng2._policy_name})   # policy-name-only, locked -> no-op
    assert eng2.policy is pol_before                      # untouched


def test_strategist_policy_name_granted_but_params_locked(tmp_path):
    """arch-review §4 P1-11: `policy` NAME and `policy_params` are gated INDEPENDENTLY. When the name
    grant is present but params are LOCKED, switching the policy must rebuild it from the NEW policy's
    OWN defaults — the raw params in the record must NOT ride past the params lock. The old code built
    `pp` from the raw policy_params regardless of the params gate, so `{policy: mcts, policy_params:
    {c: 9}}` produced MCTSPolicy(c=9) even though params were locked (a governance bypass)."""
    eng = _engine(tmp_path / "np", agent_control={"policy": ["strategist"]})   # name granted, params NOT
    eng._apply_strategy({"policy": "mcts", "policy_params": {"c": 9.0}})
    assert eng._policy_name == "mcts"                     # the NAME switch is authorized -> applied
    assert getattr(eng.policy, "c", None) == 1.4          # but params are LOCKED -> MCTS default, NOT 9


def test_strategist_policy_name_locked_but_params_granted(tmp_path):
    """The inverse asymmetry: `policy_params` granted, the NAME locked. A params change rebuilds the
    CURRENT policy with the new params (the name stays put), and the strategist cannot smuggle a name
    switch through the params grant."""
    eng = _engine(tmp_path / "pl", agent_control={"policy_params": ["strategist"]})  # params granted, name NOT
    name0 = eng._policy_name
    eng._apply_strategy({"policy": "mcts", "policy_params": {}})   # try to switch name (locked) + params
    assert eng._policy_name == name0                     # NAME locked -> unchanged (no smuggled switch)
