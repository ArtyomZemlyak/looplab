"""A7 Strategist + A1 ASHA + A0 operator tests (config-first, replay-safe, off==today)."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.models import Event, Idea, Node, NodeStatus, RunState
from looplab.orchestrator import Engine
from looplab.policy import ASHAPolicy, GreedyTree, available_policies, make_policy
from looplab.replay import fold
from looplab.sandbox import SubprocessSandbox
from looplab.strategist import (
    LLMStrategist,
    RuleStrategist,
    StrategyContext,
    make_strategist,
    validate_strategy,
)
from looplab.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "toy_task.json"


def _ctx(**kw):
    base = dict(available_policies=available_policies(), available_developers=["default"])
    base.update(kw)
    return StrategyContext(**base)


# --------------------------------------------------------------------------- #
# RuleStrategist heuristics (deterministic, pure over ctx)
# --------------------------------------------------------------------------- #

def test_rule_seed_phase_picks_cheap_breadth():
    s = RuleStrategist(n_seeds=3).decide(RunState(), _ctx(phase="seed"))
    assert s["policy"] == "greedy" and s["fidelity"] == "smoke"


def test_rule_stall_flips_greedy_to_mcts():
    s = RuleStrategist().decide(RunState(),
                                _ctx(phase="exploit", improves_since_best=4, failure_rate=0.1))
    assert s["policy"] == "mcts", s


def test_rule_stall_without_mcts_bumps_ablation():
    s = RuleStrategist().decide(
        RunState(), _ctx(phase="exploit", improves_since_best=4, failure_rate=0.1,
                         available_policies=["greedy", "asha"]))
    assert s["policy"] == "greedy" and s["operators"]["ablate_every"] >= 1


def test_rule_high_failure_narrows():
    s = RuleStrategist().decide(RunState(), _ctx(phase="explore", failure_rate=0.6))
    assert s["policy"] == "greedy"
    # agentless not available here -> no Developer swap proposed
    assert "developer" not in s


def test_rule_low_budget_exploits_full():
    s = RuleStrategist().decide(
        RunState(), _ctx(phase="exploit", eval_budget_remaining=5.0,
                         defaults={"_budget_frac": 0.1}))
    assert s["policy"] == "greedy" and s["fidelity"] == "full"


def test_rule_explore_prefers_asha_when_available():
    s = RuleStrategist().decide(RunState(),
                                _ctx(phase="explore", failure_rate=0.1, improves_since_best=0))
    assert s["policy"] == "asha"


def test_rule_healthy_exploit_keeps_current():
    # exploit, numeric, no stall, asha not in menu -> nothing to change
    s = RuleStrategist().decide(
        RunState(), _ctx(phase="exploit", failure_rate=0.0, improves_since_best=0,
                         available_policies=["greedy", "mcts"]))
    assert s is None


# --------------------------------------------------------------------------- #
# validate_strategy (whitelist)
# --------------------------------------------------------------------------- #

def test_validate_rejects_unknown_policy():
    out = validate_strategy({"policy": "zzz", "rationale": "x"}, _ctx())
    assert out is None   # nothing valid survived


def test_validate_keeps_known_policy_and_clean_ops():
    out = validate_strategy(
        {"policy": "mcts", "policy_params": {"c": 1.4, "evil": object()},
         "operators": {"ablate_every": 2, "bogus": 9}, "fidelity": "smoke"}, _ctx())
    assert out["policy"] == "mcts"
    assert out["policy_params"] == {"c": 1.4}      # non-scalar dropped
    assert out["operators"] == {"ablate_every": 2}  # unknown op key dropped
    assert out["fidelity"] == "smoke"


def test_validate_rejects_unknown_developer():
    out = validate_strategy({"developer": "ghost", "fidelity": "full"},
                            _ctx(available_developers=["default"]))
    assert "developer" not in out and out["fidelity"] == "full"


# --------------------------------------------------------------------------- #
# make_strategist (config-first)
# --------------------------------------------------------------------------- #

def test_make_strategist_off_is_none():
    from looplab.config import Settings
    assert make_strategist(Settings(strategist_backend="off")) is None


def test_make_strategist_rule():
    from looplab.config import Settings
    assert isinstance(make_strategist(Settings(strategist_backend="rule")), RuleStrategist)


def test_make_strategist_llm_without_client_falls_back_to_rule():
    from looplab.config import Settings
    s = make_strategist(Settings(strategist_backend="llm"), client=None)
    assert isinstance(s, RuleStrategist)


# --------------------------------------------------------------------------- #
# Replay safety: fold reconstructs strategy from the log; no model call on replay
# --------------------------------------------------------------------------- #

def test_fold_reconstructs_active_strategy():
    strat = {"policy": "mcts", "fidelity": "smoke", "source": "rule", "rationale": "stall"}
    evs = [
        Event(seq=0, type="run_started",
              data={"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"}),
        Event(seq=1, type="strategy_decision", data={"strategy": strat, "at_node": 3}),
    ]
    st = fold(evs)
    assert st.active_strategy == strat
    assert st.strategy_history and st.strategy_history[0]["at_node"] == 3
    # deterministic: folding twice yields the same reconstruction (no side effects, no model call)
    assert fold(evs).active_strategy == fold(evs).active_strategy


def test_set_strategy_control_folds_to_pending():
    evs = [Event(seq=0, type="set_strategy", data={"strategy": {"policy": "asha"}})]
    assert fold(evs).pending_strategy == {"policy": "asha"}


# --------------------------------------------------------------------------- #
# Engine integration: off == today (golden); a strategist records + applies
# --------------------------------------------------------------------------- #

class _StubStrategist:
    """Deterministic: switch to mcts once, then keep it. Counts decide() calls."""
    def __init__(self):
        self.calls = 0

    def decide(self, state, ctx):
        self.calls += 1
        return {"policy": "mcts", "fidelity": "smoke", "source": "rule", "rationale": "stub"}


def _engine(run_dir, *, strategist=None, policy=None, **kw):
    task = ToyTask.load(TASK_FILE)
    researcher, developer = task.build_roles()
    return Engine(run_dir, task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(),
                  policy=policy or GreedyTree(n_seeds=3, max_nodes=8),
                  n_seeds=3, max_nodes=8, strategist=strategist, **kw)


def test_off_emits_no_strategy_decision(tmp_path):
    state = anyio.run(_engine(tmp_path / "off").run)
    assert state.finished
    assert state.active_strategy is None
    evs = list(_read(tmp_path / "off"))
    assert not any(e.type == "strategy_decision" for e in evs)


def test_strategist_records_and_applies(tmp_path):
    stub = _StubStrategist()
    state = anyio.run(_engine(tmp_path / "on", strategist=stub, strategist_every=3).run)
    assert state.finished
    assert state.active_strategy and state.active_strategy["policy"] == "mcts"
    assert stub.calls >= 1
    evs = list(_read(tmp_path / "on"))
    decisions = [e for e in evs if e.type == "strategy_decision"]
    assert len(decisions) == 1   # act-only-on-change: no duplicate re-records


def test_strategist_resume_reapplies_without_recall(tmp_path):
    # First run with a strategist that records a strategy_decision, finishing the run.
    stub = _StubStrategist()
    anyio.run(_engine(tmp_path / "r", strategist=stub, strategist_every=3).run)
    calls_after_run = stub.calls
    # "Resume" the finished run with a strategist that would RAISE if consulted: a finished run
    # re-entry must not re-consult (fold reconstructs the strategy from the log).
    class _Boom:
        def decide(self, state, ctx):
            raise AssertionError("strategist re-called on replay")
    state = anyio.run(_engine(tmp_path / "r", strategist=_Boom(), strategist_every=3).run)
    assert state.active_strategy and state.active_strategy["policy"] == "mcts"
    assert stub.calls == calls_after_run   # original stub untouched


# --------------------------------------------------------------------------- #
# A1 ASHA policy
# --------------------------------------------------------------------------- #

def test_asha_seeds_rung0_then_promotes():
    st = RunState(direction="min")
    pol = ASHAPolicy(n_seeds=4, max_nodes=12, eta=2)
    # empty -> draft the rung-0 width
    acts = pol.next_actions(st)
    assert acts and all(a["kind"] == "draft" for a in acts) and len(acts) == 4
    # 4 evaluated drafts -> promote top half (eta=2) via an improve carrying rung meta
    for i in range(4):
        st.nodes[i] = Node(id=i, operator="draft",
                           idea=Idea(operator="draft", params={"x": float(i), "y": 0.0}),
                           metric=float(i), status=NodeStatus.evaluated)
    st.best_node_id = 0
    acts = pol.next_actions(st)
    assert acts[0]["kind"] == "improve"
    assert acts[0]["_rung"] == 1
    assert set(acts[0]["_promoted"]) <= {0, 1}   # top-2 by min metric


def test_asha_end_to_end_emits_rung_promoted(tmp_path):
    state = anyio.run(_engine(tmp_path / "asha",
                              policy=ASHAPolicy(n_seeds=4, max_nodes=10, eta=2)).run)
    assert state.finished and len(state.nodes) == 10
    assert state.rungs, "expected at least one rung_promoted event"
    assert any(n.operator == "improve" for n in state.nodes.values())


def test_make_policy_registers_asha():
    assert "asha" in available_policies()
    assert isinstance(make_policy("asha", n_seeds=4, max_nodes=10, eta=3), ASHAPolicy)


def test_asha_eta3_still_promotes_ceil_survivors():
    # Regression: with floor(4/3)=1 survivor ASHA could never halve and degenerated to greedy
    # exploit (caught in live testing). ceil(4/3)=2 must keep >=2 survivors so a rung promotes.
    pol = make_policy("asha", n_seeds=4, max_nodes=12, eta=3)
    st = RunState(direction="min")
    for i in range(4):
        st.nodes[i] = _eval_node(i, i, i)
    st.best_node_id = 0
    a = pol.next_actions(st)[0]
    assert a["kind"] == "improve" and a.get("_rung") == 1, a


# --------------------------------------------------------------------------- #
# A0b ensemble merge + A0d complexity cue
# --------------------------------------------------------------------------- #

def test_ensemble_merge_mode_sets_recombination_rationale(tmp_path):
    eng = _engine(tmp_path / "ens", merge_mode="ensemble")
    parents = [
        Node(id=0, operator="improve", idea=Idea(operator="improve", params={"x": 2.0, "y": 0.0}), metric=1.0),
        Node(id=1, operator="improve", idea=Idea(operator="improve", params={"x": 4.0, "y": 2.0}), metric=2.0),
    ]
    idea = eng._ensemble_idea(parents)
    assert idea.operator == "merge"
    assert "ensemble" in idea.rationale.lower() or "recombine" in idea.rationale.lower()
    assert idea.params == {"x": 3.0, "y": 1.0}   # mean payload preserved for Toy fallback


def test_budget_aware_hint_includes_remaining(tmp_path):
    eng = _engine(tmp_path / "ba", budget_aware=True, max_eval_seconds=100.0)
    st = RunState(direction="min")
    st.nodes[0] = Node(id=0, operator="draft", idea=Idea(operator="draft", params={"x": 1.0}),
                       metric=1.0, status=NodeStatus.evaluated)
    st.total_eval_seconds = 90.0   # 10% of the budget left -> "nearly spent"
    eng._set_complexity_hint(st, None)
    hint = getattr(eng.researcher, "_complexity_hint", "")
    assert "Budget guidance" in hint and "10%" in hint
    # off -> no budget line
    eng._budget_aware = False
    eng._set_complexity_hint(st, None)
    assert "Budget guidance" not in getattr(eng.researcher, "_complexity_hint", "")


def test_complexity_cue_sets_hint_on_researcher(tmp_path):
    eng = _engine(tmp_path / "cue", complexity_cue=True)
    st = RunState(direction="min")
    st.nodes[0] = Node(id=0, operator="draft", idea=Idea(operator="draft", params={"x": 1.0}))
    st.nodes[1] = Node(id=1, parent_ids=[0], operator="improve",
                       idea=Idea(operator="improve", params={"x": 1.1}))
    eng._set_complexity_hint(st, st.nodes[0])   # node 0 has 1 child -> "minimal baseline"
    assert "minimal baseline" in getattr(eng.researcher, "_complexity_hint", "")
    # off -> empty hint
    eng._complexity_cue = False
    eng._set_complexity_hint(st, st.nodes[0])
    assert getattr(eng.researcher, "_complexity_hint", "x") == ""


# --------------------------------------------------------------------------- #
# A0a code-block ablation
# --------------------------------------------------------------------------- #

def test_segment_blocks_splits_paragraphs():
    code = "import json\nx = 1\n\ny = 2\nprint(json.dumps({'metric': 0.1}))\n"
    blocks = Engine._segment_blocks(code)
    assert blocks == [(0, 2), (3, 5)]
    out = Engine._comment_block(code, blocks[0])
    assert "# [ablated] import json" in out and "# [ablated] x = 1" in out
    assert "print(json.dumps" in out and "# [ablated] y = 2" not in out   # 2nd block intact


def test_code_block_ablation_end_to_end(tmp_path):
    eng = _engine(tmp_path / "cba",
                  policy=GreedyTree(n_seeds=3, max_nodes=10, ablate_every=2),
                  ablate_code_blocks=True)
    state = anyio.run(eng.run)
    evs = list(_read(tmp_path / "cba"))
    ablates = [e for e in evs if e.type == "ablate"]
    assert ablates, "expected an ablate event"
    assert any(e.data.get("mode") == "code_blocks" for e in ablates)
    assert any(n.operator == "refine_block" for n in state.nodes.values())


# --------------------------------------------------------------------------- #
# A6 proxy/predictive scoring
# --------------------------------------------------------------------------- #

def _eval_node(i, x, m):
    return Node(id=i, operator="improve", idea=Idea(operator="improve", params={"x": float(x), "y": 0.0}),
                metric=float(m), status=NodeStatus.evaluated)


def test_proxy_predicts_from_neighbours():
    from looplab.proxy import ProxyScorer
    st = RunState(direction="min")
    st.nodes[0] = _eval_node(0, 0.0, 10.0)
    st.nodes[1] = _eval_node(1, 10.0, 1.0)
    st.best_node_id = 1
    cand = Node(id=2, operator="improve", idea=Idea(operator="improve", params={"x": 9.5, "y": 0.0}))
    pred = ProxyScorer().score(st, cand)
    assert pred is not None and pred < 6.0   # near the good neighbour (x=10 -> metric 1)


def test_proxy_off_never_skips():
    from looplab.proxy import ProxyScorer
    st = RunState(direction="min")
    for i in range(6):
        st.nodes[i] = _eval_node(i, i, i)
    st.best_node_id = 0
    cand = Node(id=9, operator="improve", idea=Idea(operator="improve", params={"x": 99.0, "y": 0.0}))
    sc = ProxyScorer(kill_fraction=0.0)
    assert sc.should_skip(st, cand, 99.0) is False   # off => never skip


def test_proxy_skips_doomed_after_warmup():
    from looplab.proxy import ProxyScorer
    st = RunState(direction="min")           # lower is better
    for i in range(6):
        st.nodes[i] = _eval_node(i, i, i)    # metrics 0..5
    st.best_node_id = 0
    sc = ProxyScorer(kill_fraction=0.34, warmup=4)
    assert sc.should_skip(st, _eval_node(9, 99, 0), 99.0) is True    # predicted worst -> skip
    assert sc.should_skip(st, _eval_node(8, 0, 0), 0.0) is False     # predicted best -> keep


def test_proxy_end_to_end_records_and_can_skip(tmp_path):
    from looplab.proxy import ProxyScorer
    state = anyio.run(_engine(tmp_path / "px", proxy_scorer=ProxyScorer(kill_fraction=0.5, warmup=3),
                              proxy_kill_fraction=0.5).run)
    assert state.finished
    evs = list(_read(tmp_path / "px"))
    assert any(e.type == "proxy_scored" for e in evs)   # the proxy ran and was audited


# --------------------------------------------------------------------------- #

def _read(run_dir: Path):
    from looplab.eventstore import EventStore
    return EventStore(run_dir / "events.jsonl").read_all()
