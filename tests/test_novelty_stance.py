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
    # M4: strategist knob application is governance-gated. A directly-constructed Engine defaults
    # agent_control to the SHIPPED DEFAULT_AGENT_CONTROL (which grants the strategist novelty_stance),
    # so the default _engine already exercises the APPLIED behaviour — no explicit grant needed.
    eng = _engine(tmp_path / "s", strategist=_ExploreStrategist())
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


def test_the_off_switch_buys_no_paid_stage_at_all(tmp_path, monkeypatch):
    """`novelty_mode="off"` must cost NOTHING — including the graded pre-gate above the flat one.

    THE DEFECT THIS RECORDS. `_apply_novelty_gate`'s docstring claimed until 2026-08-20 that
    "gate off" left it a no-op, naming `novelty_gate`. That field defaults False and only means "do
    not force the algo path"; `novelty_mode` defaults `"llm"` and was dispatched BEFORE the no-op
    check. So every run of the 2026-08-20 AlgoTune campaign — `novelty_gate = false` in all 20
    config snapshots — paid a twelve-turn agentic adjudication per proposal, plus a whole second
    Researcher proposal on each of the 10 rejections: $1.77 of a $15.73 budget and 6.6 of 60.8
    run-hours.

    Both paid stages are asserted, and the pre-gate is the half that is easy to miss: it ran ABOVE
    the mode check, and its agentic path calls `graded_novelty.tag_idea_llm` once per proposal. Its
    only power is to short-circuit the flat gate (levels 4/5 only), so with no flat gate running it
    could not change the answer — it was pure cost. Every assertion has its `mode="llm"` control
    beside it, so a fixture that never reached the dispatcher would fail rather than read green.
    """
    state = _one_node_state(tmp_path / "off-switch")
    idea = Idea(operator="improve", params={"x": 1.0}, rationale="a duplicate of node 0",
                hypothesis="the gate would have plenty to say about this")
    entered: list[str] = []

    def _engine_with_spies(run_dir, mode):
        eng = _engine(run_dir)
        eng._novelty_mode = mode
        eng._novelty_stance = "balanced"
        eng._graded_novelty = True
        monkeypatch.setattr(eng, "_graded_novelty_precheck",
                            lambda *_a, **_k: entered.append("pre_gate"))
        monkeypatch.setattr(eng, "_llm_novelty_gate",
                            lambda _state, _idea, *_a, **_k: entered.append("llm_gate") or _idea)
        return eng

    off = _engine_with_spies(tmp_path / "gate-off", "off")
    assert off._apply_novelty_gate(state, idea) is idea, "the off gate rewrote the proposal"
    assert entered == [], f"a disabled novelty gate still entered {entered}"

    entered.clear()
    on = _engine_with_spies(tmp_path / "gate-llm", "llm")
    assert on._apply_novelty_gate(state, idea) is idea
    assert entered == ["pre_gate", "llm_gate"], (
        "the control never reached the paid stages, so the case above proves nothing")

    # …and the STANCE still overrides the mode, which is the one door "off" deliberately leaves open.
    entered.clear()
    explore = _engine_with_spies(tmp_path / "gate-off-explore", "off")
    explore._novelty_stance = "explore"
    explore._apply_novelty_gate(state, idea)
    assert entered == ["pre_gate"], (
        "an explore stance must still engage the deterministic gate below the pre-check")


def test_idea_embedding_cache_is_keyed_by_content_and_stays_bounded():
    """`_idea_vec` memoizes the embedding of an idea's text. Keyed on `hash(text)`, two distinct
    ideas that collide would silently share one vector — no error, just a wrong nearest-node score
    and therefore a wrong semantic-dup verdict. And an unbounded cache grows for the whole run."""
    from types import SimpleNamespace

    from looplab.engine import novelty

    calls: list[str] = []
    eng = SimpleNamespace(
        _idea_vecs={},
        _embedder=lambda text: (calls.append(text), [float(len(text))])[1],
    )
    vec = novelty.NoveltyGateMixin._idea_vec

    a, b = "sparse attention over long docs", "distil the teacher with r-drop"
    assert vec(eng, a) == vec(eng, a) and calls == [a]          # memoized on the second read
    vec(eng, b)
    assert calls == [a, b]                                      # a DIFFERENT text is not served `a`'s
    assert len(eng._idea_vecs) == 2
    # The key CARRIES the content, so it cannot alias two texts — the property a hash cannot give.
    for text in (a, b, "", "z" * (novelty._IDEA_VEC_KEY_CHARS + 500)):
        length, prefix = novelty._idea_vec_key(text)
        assert length == len(text)
        assert prefix == text[:novelty._IDEA_VEC_KEY_CHARS + 1]
    # Texts differing only past the prefix are still distinguished (by the length half).
    long_a = "z" * novelty._IDEA_VEC_KEY_CHARS + "tail-one"
    long_b = "z" * novelty._IDEA_VEC_KEY_CHARS + "tail-two-longer"
    assert novelty._idea_vec_key(long_a) != novelty._idea_vec_key(long_b)

    # Bounded: crossing the cap clears rather than growing without limit.
    eng._idea_vecs = {("k", i): [0.0] for i in range(novelty._IDEA_IDENTITY_CACHE_MAX)}
    vec(eng, "fresh idea")
    assert len(eng._idea_vecs) == 1
