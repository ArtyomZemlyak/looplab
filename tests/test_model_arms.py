"""Doc 52 row 19: an operator × MODEL router on the bandit's yield table.

`search/policy.py::_bandit_pick` learned WHICH OPERATOR fires from folded yields and nothing learned
WHICH MODEL generates it. The router adds a model ARM to the bandit branch: `Settings.model_arms`
declares the candidate models with a relative cost, `model_arm_yields` folds Δmetric-per-second per
arm off the `model_arm` a node was built with, `_model_arm_pick` is the same deterministic UCB over
COST-normalized gain (the iso-budget lever the four 2026 results measured), the action carries
`_model`, the engine builds the node under `core/llm.py::model_override` and records the arm on
`node_created`. INERT until `operator_bandit` is on AND arms are declared — a run without either is
byte-identical.
"""
from __future__ import annotations

import anyio

from looplab.core.llm import OpenAICompatibleClient, model_override
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.search.policy import (
    DEFAULT_MODEL_ARM, META_MODEL, GreedyTree, _model_arm_pick, model_arm_costs,
    model_arm_yields, parse_model_arms)
from tests.factories import make_engine


def _node(nid, metric=None, op="draft", parents=(), eval_seconds=1.0, model_arm=""):
    return Node(id=nid, parent_ids=list(parents), operator=op,
                idea=Idea(operator=op, params={"x": float(nid)}),
                metric=metric, status=NodeStatus.evaluated, eval_seconds=eval_seconds,
                model_arm=model_arm)


def _state(nodes, direction="max"):
    st = RunState(direction=direction)
    st.nodes = {n.id: n for n in nodes}
    pool = [n for n in nodes if n.metric is not None]
    if pool:
        st.best_node_id = (max if direction == "max" else min)(pool, key=lambda n: (n.metric, n.id)).id
    return st


# ------------------------------------------------------------------ the vocabulary

def test_parse_model_arms_reads_model_and_relative_cost_and_refuses_junk():
    arms = parse_model_arms({"cheap": "qwen3:8b@0.25", "strong": "qwen3:32b", "bad": 3,
                             "free": "x@0", "neg": "y@-1", "default": "reserved", "": "z"})
    assert arms == {"cheap": ("qwen3:8b", 0.25), "strong": ("qwen3:32b", 1.0)}
    assert parse_model_arms(None) == {} and parse_model_arms("nope") == {}
    assert model_arm_costs(arms) == {DEFAULT_MODEL_ARM: 1.0, "cheap": 0.25, "strong": 1.0}


def test_model_arm_yields_fold_per_arm_with_the_default_for_unrecorded_nodes():
    p = _node(0, metric=0.5)
    cheap = _node(1, metric=0.9, op="improve", parents=(0,), eval_seconds=2.0, model_arm="cheap")
    plain = _node(2, metric=0.6, op="improve", parents=(0,), eval_seconds=2.0)
    y = model_arm_yields(_state([p, cheap, plain]))
    assert y["cheap"] == {"n": 1, "gain": 0.2} and y[DEFAULT_MODEL_ARM]["n"] == 1
    assert abs(y[DEFAULT_MODEL_ARM]["gain"] - 0.05) < 1e-9


def test_the_arm_pick_explores_untried_arms_then_exploits_cost_normalized_gain():
    arms = [DEFAULT_MODEL_ARM, "cheap"]
    costs = {DEFAULT_MODEL_ARM: 1.0, "cheap": 0.25}
    assert _model_arm_pick({}, arms, costs) == DEFAULT_MODEL_ARM, "untried: the default first"
    assert _model_arm_pick({DEFAULT_MODEL_ARM: {"n": 3, "gain": 0.1}}, arms, costs) == "cheap"
    both = {DEFAULT_MODEL_ARM: {"n": 8, "gain": 0.10}, "cheap": {"n": 8, "gain": 0.06}}
    assert _model_arm_pick(both, arms, costs) == "cheap", "0.06 / 0.25 beats 0.10 / 1.0 at iso-budget"
    assert _model_arm_pick(both, arms, {DEFAULT_MODEL_ARM: 1.0, "cheap": 1.0}) == DEFAULT_MODEL_ARM


def test_the_greedy_bandit_stamps_the_arm_only_when_arms_are_declared():
    st = _state([_node(0, metric=0.5), _node(1, metric=0.6)])
    plain = GreedyTree(n_seeds=2, max_nodes=6, operator_bandit=True).next_actions(st)
    assert plain and META_MODEL not in plain[0], "no arms: the historical action bytes"
    routed = GreedyTree(n_seeds=2, max_nodes=6, operator_bandit=True,
                        model_arms={"cheap": 0.25}).next_actions(st)
    assert routed[0][META_MODEL] == DEFAULT_MODEL_ARM, "every arm untried: the default explores first"
    st2 = _state([_node(0, metric=0.5), _node(1, metric=0.6, op="improve", parents=(0,))])
    routed2 = GreedyTree(n_seeds=2, max_nodes=6, operator_bandit=True,
                         model_arms={"cheap": 0.25}).next_actions(st2)
    assert routed2[0][META_MODEL] == "cheap", "the default has been tried once: the cheap arm is next"
    off = GreedyTree(n_seeds=2, max_nodes=6, operator_bandit=False, model_arms={"cheap": 0.25})
    assert all(META_MODEL not in a for a in off.next_actions(st2)), "inert without the bandit"


# ------------------------------------------------------------------ the override seam

def test_model_override_scopes_the_model_a_client_sends():
    client = OpenAICompatibleClient.__new__(OpenAICompatibleClient)
    client.model = "strong"
    assert client._model_for_call() == "strong"
    with model_override("cheap"):
        assert client._model_for_call() == "cheap"
        with model_override(None):
            assert client._model_for_call() == "cheap", "None keeps the enclosing override"
        with model_override(""):
            assert client._model_for_call() == "cheap"
    assert client._model_for_call() == "strong"


# ------------------------------------------------------------------ the engine

def test_a_routed_toy_run_records_the_arm_and_an_unrouted_one_keeps_its_bytes(tmp_path):
    from looplab.search.policy import GreedyTree as _GT
    eng = make_engine(tmp_path / "routed", n_seeds=2, max_nodes=8,
                      policy=_GT(n_seeds=2, max_nodes=8, operator_bandit=True, model_arms={"cheap": 0.25}),
                      model_arms={"cheap": "toy-cheap@0.25"})
    assert eng._model_arms == {"cheap": ("toy-cheap", 0.25)}
    state = anyio.run(eng.run)
    assert state.finished
    rows = [e.data for e in eng.store.read_all() if e.type == "node_created"]
    arms = [r.get("model_arm") for r in rows if "model_arm" in r]
    assert arms and set(arms) <= {DEFAULT_MODEL_ARM, "cheap"}
    assert {n.model_arm for n in state.nodes.values()} >= {DEFAULT_MODEL_ARM}, "folded onto the node"
    assert all(r.get("model_arm") is None for r in rows if r["operator"] == "draft"), \
        "seed drafts are not the bandit's; they carry no arm"

    bare = make_engine(tmp_path / "bare", n_seeds=2, max_nodes=6)
    anyio.run(bare.run)
    assert all("model_arm" not in e.data for e in bare.store.read_all() if e.type == "node_created")


def test_the_engine_builds_under_the_arms_model_override(tmp_path):
    from tests._source_scan import called_names
    from looplab.engine.orchestrator import Engine
    names = [n for n in called_names(Engine._create_node)]
    assert "model_override" in names, "the build runs under the arm's model override"
    eng = make_engine(tmp_path / "run", model_arms={"cheap": "toy-cheap@0.25"})
    assert eng._model_for_arm({META_MODEL: "cheap"}) == "toy-cheap"
    assert eng._model_for_arm({META_MODEL: DEFAULT_MODEL_ARM}) is None, "the default arm is the configured model"
    assert eng._model_for_arm({}) is None and eng._model_for_arm({META_MODEL: "unknown"}) is None
