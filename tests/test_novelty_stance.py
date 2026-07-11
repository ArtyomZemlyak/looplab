"""Novelty stance (slices 2-5): the Strategist owns a novelty dial that threads into the researcher
proposal, the foresight rank and the novelty gate. "balanced" == today's behavior byte-for-byte;
only "explore"/"exploit" change anything — these tests lock that contract in."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import RESEARCHER_HINT_ATTRS, ToyObjectiveDeveloper, ToyResearcher
from looplab.agents.strategist import (
    StrategyContext, _rule_novelty_stance, _assemble_strategy, _StrategyOut, validate_strategy,
    RuleStrategist,
)
from looplab.core.models import Idea, RunState
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.foresight import _novelty_rank_directive
from looplab.search.policy import GreedyTree, available_policies
from looplab.runtime.sandbox import SubprocessSandbox

ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "toy_task.json"


def _ctx(**kw):
    base = dict(available_policies=available_policies(), available_developers=["default"])
    base.update(kw)
    return StrategyContext(**base)


# --------------------------------------------------------------------------- #
# Strategy plumbing: validate / assemble / change-detection
# --------------------------------------------------------------------------- #

def test_validate_accepts_valid_stance_rejects_junk():
    ctx = _ctx()
    assert validate_strategy({"novelty_stance": "explore"}, ctx)["novelty_stance"] == "explore"
    assert validate_strategy({"novelty_stance": "balanced"}, ctx)["novelty_stance"] == "balanced"
    # an out-of-vocab stance is dropped (never trusted blindly), leaving no stance key
    assert "novelty_stance" not in (validate_strategy({"novelty_stance": "wild"}, ctx) or {})


def test_assemble_carries_stance():
    out = _StrategyOut(policy="mcts", novelty_stance="explore")
    strat = _assemble_strategy(out)
    assert strat["novelty_stance"] == "explore" and strat["policy"] == "mcts"


def test_stance_is_part_of_change_detection():
    # a stance-only change must be a REAL change so the engine records + applies it
    a = {"policy": "greedy", "novelty_stance": "balanced"}
    b = {"policy": "greedy", "novelty_stance": "explore"}
    assert Engine._strategy_core(a) != Engine._strategy_core(b)


# --------------------------------------------------------------------------- #
# Rule stance from coverage (deterministic, pure over ctx)
# --------------------------------------------------------------------------- #

def test_rule_stance_none_without_coverage():
    # a bare ctx (no coverage) must never perturb today's behavior
    assert _rule_novelty_stance(_ctx(phase="exploit", improves_since_best=4)) is None


def test_rule_stance_explore_on_narrowing():
    cov = {"nodes": 8, "dominant_theme_frac": 0.8, "recent_dominant_frac": 1.0, "themes": 1}
    assert _rule_novelty_stance(_ctx(phase="exploit", coverage=cov)) == "explore"


def test_rule_stance_exploit_in_endgame():
    cov = {"nodes": 8, "dominant_theme_frac": 0.3, "recent_dominant_frac": 0.3}
    assert _rule_novelty_stance(_ctx(coverage=cov, node_budget_frac=0.85)) == "exploit"


def test_rule_stance_none_when_broad():
    cov = {"nodes": 8, "dominant_theme_frac": 0.3, "recent_dominant_frac": 0.3}
    assert _rule_novelty_stance(_ctx(phase="explore", coverage=cov)) is None


def test_rule_stance_thresholds_are_exact():
    """Guard the EXACT comparison boundaries (mega-review 07-06): an off-by-one (`>=`→`>`, `<3`→`<=3`)
    in any of the four thresholds would flip a real decision yet still pass the in-region tests above."""
    strong = {"dominant_theme_frac": 0.9, "recent_dominant_frac": 1.0}
    # signal-trust floor: 3 nodes is ENOUGH to steer, 2 is not
    assert _rule_novelty_stance(_ctx(coverage={**strong, "nodes": 3})) == "explore"
    assert _rule_novelty_stance(_ctx(coverage={**strong, "nodes": 2})) is None
    # budget endgame: 0.80 exploits (inclusive), 0.79 does not
    broad = {"nodes": 8, "dominant_theme_frac": 0.3, "recent_dominant_frac": 0.3}
    assert _rule_novelty_stance(_ctx(coverage=broad, node_budget_frac=0.80)) == "exploit"
    assert _rule_novelty_stance(_ctx(coverage=broad, node_budget_frac=0.79)) is None
    # narrowing: recent_dominant_frac>=0.75 and dominant_theme_frac>=0.60 are each INCLUSIVE
    assert _rule_novelty_stance(_ctx(coverage={"nodes": 8, "recent_dominant_frac": 0.75,
                                               "dominant_theme_frac": 0.3})) == "explore"
    assert _rule_novelty_stance(_ctx(coverage={"nodes": 8, "recent_dominant_frac": 0.60,
                                               "dominant_theme_frac": 0.60})) == "explore"
    assert _rule_novelty_stance(_ctx(coverage={"nodes": 8, "recent_dominant_frac": 0.74,
                                               "dominant_theme_frac": 0.59})) is None


def test_rule_decide_overlays_stance_but_keeps_machinery():
    cov = {"nodes": 8, "dominant_theme_frac": 0.9, "recent_dominant_frac": 1.0}
    s = RuleStrategist().decide(RunState(), _ctx(phase="exploit", improves_since_best=4, coverage=cov))
    assert s["policy"] == "mcts"            # machinery decision preserved
    assert s["novelty_stance"] == "explore" # ... with the coverage-driven stance overlaid


def test_rule_decide_unchanged_without_coverage():
    # regression guard: no coverage -> byte-identical to the pre-stance behavior (no stance key)
    s = RuleStrategist().decide(RunState(), _ctx(phase="exploit", improves_since_best=4))
    assert s["policy"] == "mcts" and "novelty_stance" not in s


# --------------------------------------------------------------------------- #
# Foresight rank directive
# --------------------------------------------------------------------------- #

def test_foresight_directive_only_under_nonbalanced():
    assert _novelty_rank_directive("balanced") == ""
    assert "explore" in _novelty_rank_directive("explore").lower()
    assert "exploit" in _novelty_rank_directive("exploit").lower()


def test_novelty_hint_is_a_forwarded_hint_attr():
    assert "_novelty_hint" in RESEARCHER_HINT_ATTRS


# --------------------------------------------------------------------------- #
# Engine integration
# --------------------------------------------------------------------------- #

def _engine(run_dir, *, strategist=None, **kw):
    task = ToyTask.load(TASK_FILE)
    researcher, developer = task.build_roles()
    return Engine(run_dir, task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=8),
                  n_seeds=3, max_nodes=8, strategist=strategist, strategist_every=3, **kw)


class _ExploreStrategist:
    def decide(self, state, ctx):
        return {"policy": "greedy", "novelty_stance": "explore", "source": "rule", "rationale": "x"}


def test_engine_defaults_to_balanced(tmp_path):
    eng = _engine(tmp_path / "d")
    assert eng._novelty_stance == "balanced"


def test_strategist_stance_is_applied_and_recorded(tmp_path):
    # M4: strategist knob application is governance-gated. A real (CLI) run grants the strategist its
    # knobs via the shipped Settings.agent_control default — grant them here to exercise the APPLIED
    # behaviour (a directly-constructed Engine defaults to the conservative all-locked map).
    from looplab.core.config import Settings
    eng = _engine(tmp_path / "s", strategist=_ExploreStrategist(),
                  agent_control=Settings().agent_control)
    state = anyio.run(eng.run)
    assert eng._novelty_stance == "explore"
    decisions = [e for e in EventStore(tmp_path / "s" / "events.jsonl").read_all()
                 if e.type == "strategy_decision"]
    assert decisions and decisions[0].data["strategy"]["novelty_stance"] == "explore"


def test_strategist_stance_locked_when_not_granted(tmp_path):
    """M4 enforcement: with novelty_stance absent from the governance matrix, the strategist's
    decision is still RECORDED but NOT applied — the documented per-setting lock actually holds now
    (before the fix, every strategist knob except timeout/max_parallel was applied ungated)."""
    eng = _engine(tmp_path / "s2", strategist=_ExploreStrategist(), agent_control={})   # all locked
    anyio.run(eng.run)
    assert eng._novelty_stance != "explore"                     # locked: decision not applied
    decisions = [e for e in EventStore(tmp_path / "s2" / "events.jsonl").read_all()
                 if e.type == "strategy_decision"]
    assert decisions and decisions[0].data["strategy"]["novelty_stance"] == "explore"   # still recorded


def _one_node_state(tmp_path) -> RunState:
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": ""}})
    s.append("node_evaluated", {"node_id": 0, "metric": 1.0})
    return fold(s.read_all())


def test_stamp_novelty_hint_sets_and_neutralizes(tmp_path):
    # explore stamps a directive + the stance value; a later "balanced" stamp (the debug/repair path)
    # CLEARS both, so a repair proposal is never mis-instructed by a stale explore hint.
    eng = _engine(tmp_path / "n")
    eng._stamp_novelty_hint(RunState(), "explore")
    assert "EXPLORE" in getattr(eng.researcher, "_novelty_hint", "")
    assert getattr(eng.researcher, "_novelty_stance", None) == "explore"
    eng._stamp_novelty_hint(RunState(), "balanced")
    assert getattr(eng.researcher, "_novelty_hint", "x") == ""
    assert getattr(eng.researcher, "_novelty_stance", None) == "balanced"


def test_novelty_gate_engages_under_explore_even_with_gate_off(tmp_path):
    # gate off (default) + a duplicate param proposal: balanced leaves it untouched (early return),
    # explore engages the numeric dedup and nudges it off the duplicate.
    st = _one_node_state(tmp_path / "st")
    dup = Idea(operator="improve", params={"x": 1.0}, rationale="short")

    eng = _engine(tmp_path / "e")               # novelty_gate off by default
    eng._novelty_mode = "off"                   # off mode: only the explore stance can engage the algo gate
    eng._novelty_stance = "balanced"
    assert eng._apply_novelty_gate(st, dup.model_copy()).params == {"x": 1.0}   # unchanged

    eng._novelty_stance = "explore"
    nudged = eng._apply_novelty_gate(st, dup.model_copy())
    assert nudged.params != {"x": 1.0}          # engaged -> nudged off the near-duplicate
