"""Phase 2 (docs/12): stagnation-adaptive strategy, semantic novelty rejection, weighted
parent selection, operator bandit, normalized error signatures, debug-depth knob."""
from __future__ import annotations

import pytest

from looplab.core.errors import BudgetExceeded
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.agents.strategist import RuleStrategist, StrategyContext
from looplab.engine.orchestrator import Engine, _normalize_error_sig
from looplab.search.policy import (EvolutionaryPolicy, GreedyTree, _bandit_pick, make_policy,
                                   operator_yields, weighted_parent)


def _node(nid, metric=None, op="draft", parents=(), status=NodeStatus.evaluated,
          eval_seconds=1.0, rationale=""):
    return Node(id=nid, parent_ids=list(parents), operator=op,
                idea=Idea(operator=op, params={"x": float(nid)}, rationale=rationale),
                metric=metric, status=status, eval_seconds=eval_seconds)


def _state(nodes, direction="max"):
    st = RunState(direction=direction)
    st.nodes = {n.id: n for n in nodes}
    # recompute best like fold does (feasible, metric-bearing)
    pool = [n for n in nodes if n.status is NodeStatus.evaluated and n.metric is not None]
    if pool:
        st.best_node_id = (max if direction == "max" else min)(
            pool, key=lambda n: (n.metric, n.id)).id
    return st


# --------------------------------------------------------------------------- #
# 2.1 adaptive strategist: greedy -> broad on stall, BACK to greedy on fresh leader
# --------------------------------------------------------------------------- #

def _ctx(**kw):
    base = dict(node_count=6, phase="exploit", failure_rate=0.0, improves_since_best=0,
                is_numeric_space=True, current_policy="greedy",
                available_policies=["greedy", "evolutionary", "mcts", "asha", "bohb"],
                available_developers=[], defaults={})
    base.update(kw)
    return StrategyContext(**base)


def test_rule_strategist_switches_back_to_greedy_after_stall_resolves():
    rs = RuleStrategist(n_seeds=3, stall_window=3)
    # stalled under greedy -> broaden (mcts)
    out = rs.decide(RunState(), _ctx(improves_since_best=4))
    assert out and out["policy"] == "mcts"
    # fresh leader under mcts (no stall) -> back to greedy exploitation
    out2 = rs.decide(RunState(), _ctx(improves_since_best=0, current_policy="mcts"))
    assert out2 and out2["policy"] == "greedy"
    assert "greedy" in out2["rationale"]


def test_rule_strategist_no_flap_when_already_greedy():
    rs = RuleStrategist(n_seeds=3, stall_window=3)
    out = rs.decide(RunState(), _ctx(improves_since_best=0, current_policy="greedy",
                                     is_numeric_space=False, phase="exploit"))
    assert out is None                     # healthy exploit under greedy: nothing to change


# --------------------------------------------------------------------------- #
# 2.4 weighted parent selection (deterministic ShinkaEvolve shape)
# --------------------------------------------------------------------------- #

def test_weighted_parent_prefers_underexpanded_good_nodes():
    # node 0: best metric but already has 2 children; node 1: slightly worse, unexpanded
    n0 = _node(0, metric=1.0)
    n1 = _node(1, metric=0.9)
    c1 = _node(2, metric=0.5, op="improve", parents=(0,))
    c2 = _node(3, metric=0.4, op="improve", parents=(0,))
    st = _state([n0, n1, c1, c2])
    # weights: n0 -> (1/1)/(1+2)=0.333; n1 -> (1/2)/(1+0)=0.5 -> n1 wins
    assert weighted_parent(st) == 1


def test_weighted_parent_deterministic():
    st = _state([_node(0, metric=1.0), _node(1, metric=0.9)])
    assert weighted_parent(st) == weighted_parent(st) == 0   # best + unexpanded wins


def test_evolutionary_mutation_uses_weighted_parent():
    # gen odd (3 improves exist) -> mutate; the weighted pick must avoid the over-expanded elite:
    # n0 weight = (1/1)/(1+3) = 0.25 < n1 weight = (1/2)/(1+0) = 0.5
    n0 = _node(0, metric=1.0)
    n1 = _node(1, metric=0.9)
    kids = [_node(i, metric=0.2, op="improve", parents=(0,)) for i in (2, 3, 4)]
    st = _state([n0, n1, *kids])
    acts = EvolutionaryPolicy(pop=2, max_nodes=10).next_actions(st)
    assert acts[0]["kind"] == "improve" and acts[0]["parent_id"] == 1


# --------------------------------------------------------------------------- #
# 2.5 operator bandit
# --------------------------------------------------------------------------- #

def test_operator_yields_folds_gain_per_second():
    p = _node(0, metric=0.5)
    good = _node(1, metric=0.9, op="improve", parents=(0,), eval_seconds=2.0)
    bad = _node(2, metric=0.1, op="merge", parents=(0,), eval_seconds=2.0)
    y = operator_yields(_state([p, good, bad]))
    assert y["improve"]["n"] == 1 and y["improve"]["gain"] == pytest.approx(0.2)
    assert y["merge"]["gain"] == 0.0       # negative delta clamps to 0


def test_bandit_pick_explores_untried_then_exploits():
    yields = {"improve": {"n": 8, "gain": 0.05}}
    # merge untried -> exploration bonus dominates
    assert _bandit_pick(yields, ["improve", "merge"]) == "merge"
    # merge tried and worthless, improve productive -> exploit improve
    yields["merge"] = {"n": 8, "gain": 0.0}
    assert _bandit_pick(yields, ["improve", "merge"]) == "improve"


def test_greedy_bandit_stays_legal(tmp_path):
    """With the bandit on, GreedyTree still only proposes legal actions and terminates."""
    pol = GreedyTree(n_seeds=2, max_nodes=6, operator_bandit=True, ablate_every=2)
    st = _state([_node(0, metric=0.5), _node(1, metric=0.6)])
    acts = pol.next_actions(st)
    assert acts and acts[0]["kind"] in ("improve", "merge", "ablate", "draft")


def test_make_policy_passes_new_knobs():
    p = make_policy("greedy", n_seeds=2, max_nodes=5, debug_depth=3, operator_bandit=True)
    assert p.debug_depth == 3 and p.operator_bandit is True
    p2 = make_policy("mcts", n_seeds=2, max_nodes=5, debug_depth=3)
    assert p2.debug_depth == 3


# --------------------------------------------------------------------------- #
# T10 normalized error signatures
# --------------------------------------------------------------------------- #

def test_normalize_error_sig_matches_semantically_identical_errors():
    a = 'File "/tmp/x1/solution.py", line 42, in <module>\nValueError: shape (128, 10) mismatch'
    b = 'File "/tmp/x2/solution.py", line 57, in <module>\nValueError: shape (256, 10) mismatch'
    assert _normalize_error_sig(a) == _normalize_error_sig(b)
    c = "TypeError: unsupported operand"
    assert _normalize_error_sig(a) != _normalize_error_sig(c)


# --------------------------------------------------------------------------- #
# 2.3 semantic novelty gate (engine-level)
# --------------------------------------------------------------------------- #

class _T:
    id = "t"
    goal = "g"
    direction = "max"
    def model_dump(self, mode="json"):
        return {"id": "t"}


def _mk_engine(tmp_path, **kw):
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree as GT

    class _R:
        def propose(self, state, parent):
            return Idea(operator="draft", params={"x": 1.0},
                        rationale="try gradient boosting with deep trees and early stopping")

    class _D:
        def implement(self, idea):
            return "print({'metric': 1})"

    return Engine(tmp_path / "run", task=_T(), researcher=_R(), developer=_D(),
                  sandbox=SubprocessSandbox(), policy=GT(n_seeds=1, max_nodes=2),
                  novelty_gate=True, **kw)


def test_semantic_duplicate_detected_and_reproposed(tmp_path):
    # novelty_semantic explicitly ON: this test exercises the semantic-dedup MECHANISM. The
    # library default flipped to False (matching Settings + the documented rationale) in the
    # P4.4 divergence realignment — relying on the old default was the audited breakage.
    eng = _mk_engine(tmp_path, novelty_semantic=True)
    st = RunState(direction="max")
    dup = _node(0, metric=0.4, status=NodeStatus.failed,
                rationale="try gradient boosting with deep trees and early stopping")
    dup.error, dup.error_reason = "boom", "crash"
    st.nodes = {0: dup}
    idea = Idea(operator="improve", params={"x": 2.0},
                rationale="try gradient boosting with deep trees and early stopping")
    called = {}

    def repropose():
        called["fb"] = getattr(eng.researcher, "_novelty_feedback", "")
        return Idea(operator="improve", params={"x": 9.0}, rationale="switch to a linear model")

    out = eng._apply_novelty_gate(st, idea, repropose=repropose)
    assert out.rationale == "switch to a linear model"     # informed re-propose accepted
    assert "near-duplicate" in called["fb"] and "FAILED" in called["fb"]
    ev = [e for e in eng.store.read_all() if e.type == "novelty_rejected"]
    assert ev and ev[0].data["kind"] == "semantic" and ev[0].data["action"] == "reproposed"


def test_semantic_reproposal_action_reports_the_actual_same_proposal_result(tmp_path):
    eng = _mk_engine(tmp_path, novelty_semantic=True)
    eng._novelty_mode = "algo"
    dup = _node(0, metric=0.4, rationale="a prior sufficiently long duplicate proposal")
    st = _state([dup])
    idea = Idea(operator="improve", params={"x": 99.0},
                rationale="a candidate sufficiently long duplicate proposal")
    eng._semantic_duplicate = lambda state, proposal: (dup, 0.99)

    out = eng._apply_novelty_gate(st, idea, repropose=lambda: idea.model_copy(deep=True))
    receipt = [event for event in eng.store.read_all() if event.type == "novelty_rejected"][0]
    assert out == idea and receipt.data["action"] == "kept"


def test_budget_exceeded_during_reproposal_records_an_honest_terminal_receipt(tmp_path):
    eng = _mk_engine(tmp_path, novelty_semantic=True)
    eng._novelty_mode = "algo"
    dup = _node(0, metric=0.4, rationale="a prior sufficiently long duplicate proposal")
    st = _state([dup])
    idea = Idea(operator="improve", params={"x": 99.0},
                rationale="a candidate sufficiently long duplicate proposal")
    eng._semantic_duplicate = lambda state, proposal: (dup, 0.99)

    def _exhausted():
        raise BudgetExceeded("test budget exhausted")

    with pytest.raises(BudgetExceeded, match="test budget exhausted"):
        eng._apply_novelty_gate(st, idea, repropose=_exhausted, prospective_node_id=7)
    receipt = [event for event in eng.store.read_all() if event.type == "novelty_rejected"][0]
    assert receipt.data["action"] == "budget_exceeded"
    assert receipt.data["node_id"] == 7 and receipt.data["generation"] == 0
    assert receipt.data["near_node"] == 0 and receipt.data["near_generation"] == 0
    assert receipt.data["proposal_ref"] == eng._proposal_binding(st, idea, 7)["proposal_ref"]


def test_semantic_gate_skips_short_toy_rationales(tmp_path):
    # explicit: this test exercises the semantic gate's short-rationale skip (see the
    # sibling above for the default-flip rationale)
    eng = _mk_engine(tmp_path, novelty_semantic=True)
    st = RunState(direction="max")
    st.nodes = {0: _node(0, metric=0.4, rationale="x")}
    idea = Idea(operator="improve", params={"x": 99.0}, rationale="y")
    out = eng._apply_novelty_gate(st, idea)
    assert out.params == {"x": 99.0}       # too short for semantic identity -> untouched


def test_settings_phase2_defaults():
    from looplab.core.config import PROFILES, Settings
    s = Settings()
    assert s.failure_reflection is True
    # novelty_semantic ships OFF: novelty is the agentic Researcher's call (reads prior
    # experiments via tools), not an embedding auto-reject — see config.py's field comment.
    assert s.novelty_semantic is False
    assert s.debug_depth == 2
    assert s.operator_bandit is False
    assert PROFILES["thorough"]["operator_bandit"] is True
