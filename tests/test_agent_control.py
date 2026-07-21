"""Agent governance (Settings.agent_control): which roles (strategist/boss/researcher) may change a
setting at runtime. Covers the matrix gate, the Researcher's per-node eval_timeout (the neural-net
case), the Strategist's run-wide timeout/max_parallel, and the boss budget_extend fold.
"""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.core.models import Idea, Node, RunState
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
        return Idea(
            operator="x", params={"x": 1.0}, eval_timeout=self.eval_timeout,
            rationale="exercise the governed per-node timeout",
        )


class _Dev:
    def implement(self, idea):
        return "print('{\"metric\": 0.1}')\n"


def _engine(run_dir, *, eval_timeout=None, timeout=30.0, max_eval_timeout=3600.0,
            agent_control=None, sandbox=None, **engine_kwargs):
    return Engine(run_dir, task=ToyTask.load(TASK), researcher=_Researcher(eval_timeout),
                  developer=_Dev(), sandbox=sandbox or _RecordSandbox(), timeout=timeout,
                  max_eval_timeout=max_eval_timeout,
                  policy=GreedyTree(n_seeds=1, max_nodes=1, debug_depth=0),
                  agent_control=agent_control, auto_install_deps=False, **engine_kwargs)


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


def test_researcher_eval_timeout_is_clamped_after_governance(tmp_path):
    sb = _RecordSandbox()
    eng = _engine(tmp_path / "clamped", eval_timeout=3600.0, timeout=30.0,
                  max_eval_timeout=90.0,
                  agent_control={"timeout": ["researcher"]}, sandbox=sb)
    anyio.run(eng.run)
    assert sb.timeouts == [90.0]
    state = fold(eng.store.read_all())
    node = next(iter(state.nodes.values()))
    assert node.idea.eval_timeout == 90.0
    assert state.cards[node.idea.card_id].eval_timeout == 90.0
    added = [event for event in eng.store.read_all() if event.type == "card_added"][-1]
    assert added.data["idea"]["eval_timeout"] == 90.0


def test_locked_researcher_timeout_falls_back_instead_of_clamping(tmp_path):
    sb = _RecordSandbox()
    eng = _engine(tmp_path / "locked-clamp", eval_timeout=3600.0, timeout=30.0,
                  max_eval_timeout=90.0,
                  agent_control={"timeout": ["strategist"]}, sandbox=sb)
    anyio.run(eng.run)
    assert sb.timeouts == [30.0]
    state = fold(eng.store.read_all())
    node = next(iter(state.nodes.values()))
    assert node.idea.eval_timeout is None
    assert state.cards[node.idea.card_id].eval_timeout is None


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


def test_command_eval_ignores_researcher_timeout_override(tmp_path, monkeypatch):
    """Repo/command eval executes its operator-owned timeout, not Idea.eval_timeout."""
    eng = _engine(
        tmp_path / "command-eval",
        agent_control={"timeout": ["researcher"]},
    )
    eng._eval_spec = {
        "command": ["python", "score.py"],
        "metric": {"kind": "stdout_json", "key": "metric"},
        "timeout": 17,
    }
    workdir = tmp_path / "eval-workdir"
    workdir.mkdir()
    seen = {}

    def _record_command(_command, _cwd, timeout, _metric, *args, **kwargs):
        seen["timeout"] = timeout
        return RunResult(0, '{"metric": 1}', "", 1.0, False)

    monkeypatch.setattr("looplab.runtime.command_eval.run_command_eval", _record_command)
    node = Node(
        id=0,
        operator="draft",
        idea=Idea(operator="draft", params={"x": 1}, eval_timeout=3600),
        code="",
    )

    eng._run_eval(node, workdir)
    assert eng._effective_researcher_eval_timeout(node.idea) is None
    assert seen["timeout"] == 17.0


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


def test_strategist_applies_parallel_build_with_auto_when_granted(tmp_path):
    # Live updates settle 0 to safe serial width 1. Only startup Settings use AUTO coupling.
    eng = _engine(tmp_path / "pb_on",
                  agent_control={"parallel_build": ["strategist"], "max_parallel": ["strategist"]})
    eng._apply_strategy({"max_parallel": 3, "parallel_build": 2})
    assert eng.max_parallel == 3 and eng.parallel_build == 2
    eng._apply_strategy({"parallel_build": 0})
    assert eng.parallel_build == 1


def test_strategist_parallel_build_blocked_when_not_granted(tmp_path):
    eng = _engine(tmp_path / "pb_off", agent_control={})               # nothing granted
    eng._apply_strategy({"parallel_build": 4})
    assert eng.parallel_build == 1                                     # unchanged (serial default)


def test_strategist_named_lane_allocation_is_governed_and_settles_zero(tmp_path):
    proposed = {"build": 0, "deep_research": 2, "novelty_dedup": 1}
    locked = _engine(tmp_path / "lanes-off", llm_parallel=4, agent_control={})
    before = locked._llm_broker.snapshot()["lane_limits"]
    locked._apply_strategy({"llm_lane_limits": proposed})
    assert locked._llm_broker.snapshot()["lane_limits"] == before

    granted = _engine(
        tmp_path / "lanes-on", llm_parallel=4,
        agent_control={"llm_lane_limits": ["strategist"]})
    granted._apply_strategy({"llm_lane_limits": proposed})
    assert granted._llm_broker.snapshot()["lane_limits"] == {
        "build": 1, "deep_research": 2, "novelty_dedup": 1}


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
    out = validate_strategy({"timeout": 120.0, "max_parallel": 3, "parallel_build": 2}, ctx)
    assert out["timeout"] == 120.0 and out["max_parallel"] == 3 and out["parallel_build"] == 2
    assert validate_strategy({"parallel_build": 0}, ctx)["parallel_build"] == 0   # live 0 -> serial 1
    # bad shapes dropped
    assert "timeout" not in (validate_strategy({"timeout": -5}, ctx) or {})
    assert "parallel_build" not in (validate_strategy({"parallel_build": -1}, ctx) or {})
    assert "parallel_build" not in (validate_strategy({"parallel_build": 99}, ctx) or {})   # >64 dropped
    assert "max_parallel" not in (validate_strategy({"max_parallel": -1}, ctx) or {})      # negative dropped
    assert validate_strategy({"max_parallel": 0}, ctx)["max_parallel"] == 0   # live 0 -> serial 1
    assert "max_parallel" not in (validate_strategy({"max_parallel": True}, ctx) or {})   # bool != int budget
    assert "max_parallel" not in (validate_strategy({"max_parallel": 1025}, ctx) or {})
    assert "timeout" not in (validate_strategy({"timeout": float("inf")}, ctx) or {})


# ----------------------------------------------------- boss budget_extend fold
def test_budget_extend_folds_timeout_and_parallel(tmp_path):
    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    s.append("budget_extend", {"timeout": 600.0, "max_parallel": 5,
                                "eval_parallel": 4, "llm_parallel": 2,
                                "max_eval_seconds": 1000.0})
    st = fold(EventStore(p).read_all())
    assert st.budget_overrides["timeout"] == 600.0
    assert st.budget_overrides["eval_parallel"] == 4
    assert st.budget_overrides["llm_parallel"] == 2
    assert "max_parallel" not in st.budget_overrides
    assert st.budget_overrides["max_eval_seconds"] == 1000.0


def test_default_settings_matrix_shape():
    s = Settings()
    assert s.agent_control["timeout"] == ["researcher", "strategist"]
    assert "boss" in s.agent_control["max_nodes"]      # budget_extend was already a boss power
    assert s.agent_control["eval_parallel"] == ["strategist"]
    assert s.agent_control["llm_parallel"] == ["strategist"]
    assert s.agent_control["llm_lane_limits"] == ["strategist"]
    assert "max_parallel" not in s.agent_control and "parallel_build" not in s.agent_control
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


def test_budget_extend_canonical_zero_settles_both_axes_to_one(tmp_path):
    eng = _engine(tmp_path / "canonical-zero", agent_control={})
    st = RunState(budget_overrides={"eval_parallel": 0, "llm_parallel": 0})
    eng._apply_control_overrides(st)
    assert (eng.max_parallel, eng._eval_parallel) == (1, 1)
    assert (eng.parallel_build, eng._llm_parallel) == (1, 1)


def test_folded_budget_alias_lww_applies_latest_values_on_both_axes(tmp_path):
    events = tmp_path / "alias-lww.jsonl"
    store = EventStore(events)
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    store.append("budget_extend", {"eval_parallel": 8, "llm_parallel": 5})
    store.append("budget_extend", {"max_parallel": 3, "parallel_build": 2})
    eng = _engine(tmp_path / "alias-lww-engine", agent_control={})
    eng._apply_control_overrides(fold(store.read_all()))
    assert (eng.max_parallel, eng.parallel_build) == (3, 2)

    store.append("budget_extend", {"eval_parallel": 7, "llm_parallel": 4})
    eng._apply_control_overrides(fold(store.read_all()))
    assert (eng.max_parallel, eng.parallel_build) == (7, 4)


def test_control_override_apply_is_total_for_poisoned_state(tmp_path):
    eng = _engine(tmp_path / "poisoned-control", timeout=30.0, agent_control={})
    eng.max_seconds = 120.0
    eng.max_eval_seconds = 80.0
    state = RunState(budget_overrides={
        "max_seconds": float("inf"), "max_eval_seconds": float("nan"),
        "timeout": True, "eval_parallel": 2.5, "llm_parallel": 1.5,
    })
    max_s, max_es = eng._apply_control_overrides(state)
    assert (max_s, max_es, eng.timeout) == (120.0, 80.0, 30.0)
    assert (eng.max_parallel, eng.parallel_build) == (1, 1)


def test_legacy_governance_snapshot_grants_canonical_counterparts(tmp_path):
    eng = _engine(tmp_path / "old-grants", agent_control={
        "max_parallel": ["strategist"], "parallel_build": ["strategist"],
    })
    eng._apply_strategy({"eval_parallel": 4, "llm_parallel": 3})
    assert (eng.max_parallel, eng.parallel_build) == (4, 3)


def test_canonical_governance_defaults_grant_legacy_counterparts(tmp_path):
    eng = _engine(tmp_path / "new-grants", agent_control={
        "eval_parallel": ["strategist"], "llm_parallel": ["strategist"],
    })
    eng._apply_strategy({"max_parallel": 5, "parallel_build": 2})
    assert (eng.max_parallel, eng.parallel_build) == (5, 2)


def test_canonical_governance_revocation_beats_stale_legacy_grant(tmp_path):
    # CODEX AGENT: migration fallback is absence-only. An explicit empty canonical grant is a lock,
    # not an invitation to union a stale legacy snapshot entry back into the authority decision.
    eng = _engine(tmp_path / "canonical-locks", agent_control={
        "eval_parallel": [], "max_parallel": ["strategist"],
        "llm_parallel": [], "parallel_build": ["strategist"],
    })
    eng._apply_strategy({"eval_parallel": 4, "llm_parallel": 3})
    assert (eng.max_parallel, eng.parallel_build) == (1, 1)
    assert eng._agent_may("strategist", "max_parallel") is False
    assert eng._agent_may("strategist", "parallel_build") is False


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
