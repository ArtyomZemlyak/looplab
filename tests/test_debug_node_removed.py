"""F5: the Debug node is deleted, and no `draft`/`improve` may be one under another name.

THE DECISION, 2026-08-13: *"дебаг ноду нафиг убираем. У нас репейринг есть. Им вот и должно всё
решаться."* A failure is fixed INSIDE the node that failed, for as long as the repair judgment allows
(F8, `tests/test_repair_judgment.py`). The two land together on purpose — removing the Debug node
while the repair bound was still a fixed count would have turned "give up and open a new node" into
plain "give up", which is strictly worse than what it replaced.

WHY THIS FILE IS MOSTLY NEGATIVE ASSERTIONS, and why they are not the weak kind. What has to be
guarded is an ABSENCE across a set of producers that no longer share a call site, so there is nothing
left to drive: `debug_action` is deleted, the forced prefix is deleted, the build branch is deleted.
The two tiers used are the two that survive a refactor (CLAUDE.md's guard-test ladder):

  * tier 1 — drive the real policies, the real Card lane and the real inject boundary and observe
    that no action, Card or node retries a failed experiment. These are the tests that matter,
    because the way this comes back is not somebody re-adding `debug_action`, it is a new path that
    breeds from a failed node under a different operator;
  * tier 3 — AST over the production source for the residue a behavioural test cannot see: that no
    module still CALLS a producer that does not exist, and that nothing mints `KIND_DEBUG`.

WHAT IS DELIBERATELY STILL HERE, and must stay: the fold's debug-leaf readers in
`events/card_ledger.py`, `KIND_DEBUG` itself, and `Settings.debug_depth`. Preserved runs contain
`debug` nodes and Cards, and a replay that disagrees with the run's own recorded state is a
reproducibility break, not a cleanup. `tests/test_card_selection_guard.py` holds the fold side;
what this file pins is that the same shapes are never SELECTABLE again.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search import card_selection, policy as policy_mod
from looplab.search.policy import (ASHAPolicy, EvolutionaryPolicy, GreedyTree, KIND_DEBUG,
                                   MCTSPolicy, legal_actions)

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"
LOOPLAB = ROOT / "looplab"


def _failed_leaf_state() -> RunState:
    """One evaluated node and one failed leaf bred from it — the exact shape that used to force a
    Debug node ahead of every other action."""
    st = RunState(run_id="r", task_id="t", direction="min")
    st.nodes[0] = Node(id=0, operator="draft", idea=Idea(operator="draft", params={"x": 1.0}),
                       metric=0.5, status=NodeStatus.evaluated)
    st.nodes[1] = Node(id=1, operator="improve", parent_ids=[0],
                       idea=Idea(operator="improve", params={"x": 2.0}),
                       status=NodeStatus.failed, error="boom", error_reason="crash")
    st.best_node_id = 0
    return st


# --------------------------------------------------------------- tier 1: nothing proposes one
@pytest.mark.parametrize("policy", [
    GreedyTree(n_seeds=1, max_nodes=8, debug_depth=2),
    EvolutionaryPolicy(pop=2, max_nodes=8, debug_depth=2),
    MCTSPolicy(n_seeds=2, max_nodes=8, debug_depth=2),
    ASHAPolicy(n_seeds=2, max_nodes=8, eta=2, debug_depth=2),
], ids=lambda p: type(p).__name__)
def test_no_policy_proposes_a_node_that_retries_a_failed_one(policy):
    """Every policy, `debug_depth` deliberately set HIGH — the knob that used to buy Debug nodes is
    inert, and a run that carries `debug_depth: 2` in its snapshot must not resurrect them.

    Both shapes are checked: the `debug` kind, and any action anchored on the failed node. The second
    is the one that matters — "a new node whose actual purpose is another attempt at an experiment
    that just failed" is the thing deleted, not a particular string."""
    for action in policy.next_actions(_failed_leaf_state()):
        assert action["kind"] != KIND_DEBUG, action
        assert action.get("parent_id") != 1, action
        assert 1 not in (action.get("parent_ids") or []), action


def test_the_agents_legal_action_envelope_offers_no_retry_of_a_failed_node():
    """`legal_actions` is what the self-driving agent picks from, so leaving `debug` in it would let
    the agent mint the node the policies no longer can — the widest of the evasion routes."""
    actions = legal_actions(_failed_leaf_state(), GreedyTree(n_seeds=1, max_nodes=8, debug_depth=2),
                            max_nodes=8)
    assert actions, "the envelope must not be empty — this test would pass vacuously"
    for action in actions:
        assert action["kind"] != KIND_DEBUG, action
        assert action.get("parent_id") != 1, action
        assert 1 not in (action.get("parent_ids") or []), action


def test_the_card_lane_neither_forces_nor_scores_a_retry_of_a_failed_node():
    """The Card lane had its own producer — a forced prefix that pre-empted the whole scoring pass
    the moment a failed leaf existed. Driven through the public entry points, not the removed
    internals, so a reintroduction anywhere below them still fails this."""
    state = _failed_leaf_state()
    pol = GreedyTree(n_seeds=1, max_nodes=8, debug_depth=2)
    assert card_selection.forced_card_actions(state, pol, max_nodes=8) is None, (
        "a failed leaf must not pre-empt Card scoring any more")
    for action in card_selection.card_next_actions(state, pol, max_nodes=8):
        assert action["kind"] != KIND_DEBUG, action
        assert action.get("parent_id") != 1, action


def test_a_failed_node_is_never_an_improve_anchor():
    """THE EVASION ROUTE, stated as its own property rather than left implicit.

    An `improve` on a failed node is a Debug node with a different operator label, and the only thing
    standing between the two is that `improve` anchors on `breedable_nodes()`. That is a function
    three layers away from any policy, so the rule is pinned here where the decision lives."""
    state = _failed_leaf_state()
    assert [n.id for n in state.breedable_nodes()] == [0]
    assert 1 not in {n.id for n in state.breedable_nodes()}
    # …including when the failed node is the run's ONLY node, which is the shape a first-node crash
    # produces and the one most likely to tempt a "just improve it" fallback.
    lone = RunState(run_id="r", task_id="t", direction="min")
    lone.nodes[0] = Node(id=0, operator="draft", idea=Idea(operator="draft", params={}),
                         status=NodeStatus.failed, error="boom", error_reason="crash")
    assert lone.breedable_nodes() == []
    for action in GreedyTree(n_seeds=1, max_nodes=8, debug_depth=2).next_actions(lone):
        assert action.get("parent_id") != 0 and action["kind"] != KIND_DEBUG, action


def test_an_operator_may_not_inject_a_debug_node(tmp_path):
    """The last producer, and the one that most looks like an exception worth making — a human asked
    for it. It is refused, with a message that says what to do instead: what the operator gets now is
    strictly better, because the node they would have opened is repaired in place with no budget slot
    spent and no second lineage to reconcile."""
    eng = Engine(tmp_path / "run", task=ToyTask.load(TASK), researcher=None, developer=None,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2))
    state = _failed_leaf_state()
    with pytest.raises(ValueError) as excinfo:
        eng._prepare_injected_node(state, {"idea": {"operator": "debug", "params": {}},
                                           "parent_id": 1})
    assert "debug nodes were removed" in str(excinfo.value)
    # …and the neighbouring operators still work, so the refusal is narrow rather than a broken path.
    assert eng._prepare_injected_node(
        state, {"idea": {"operator": "improve", "params": {}}, "parent_id": 0}).idea.operator == "improve"


def test_a_crashing_first_node_never_grows_a_child(tmp_path):
    """End to end on the real engine, because the failure mode that matters is a whole run, not a
    predicate. A first node that crashes and cannot be repaired must be abandoned and NOT bred from —
    and the run must still finish rather than stalling with nothing to do, which is the way this
    removal could have gone wrong."""

    class _AlwaysCrashes:
        def implement(self, idea):
            return "raise RuntimeError('boom')\n"

    class _Stub:
        def propose(self, state, parent):
            return Idea(operator="x", params={"x": 1.0, "y": 1.0})

    eng = Engine(tmp_path / "crash", task=ToyTask.load(TASK), researcher=_Stub(),
                 developer=_AlwaysCrashes(), sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=1, max_nodes=3), inline_repair=False)

    async def _bounded():
        with anyio.move_on_after(180) as scope:
            await eng.run()
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the run did not terminate"
    state = fold(eng.store.read_all())
    assert state.finished is True
    failed = [n for n in state.nodes.values() if n.status is NodeStatus.failed]
    assert failed, "the fixture must actually fail"
    assert all(n.operator != "debug" for n in state.nodes.values())
    for node in state.nodes.values():
        assert not any(parent in {f.id for f in failed} for parent in node.parent_ids), (
            f"node {node.id} was bred from a failed node")


# --------------------------------------------------------------- tier 3: the residue, over AST
_PRODUCER_NAMES = {"debug_action", "_debug_lineage", "_matching_ready_debug_card", "_debug_depth"}


def test_no_module_still_calls_a_deleted_debug_producer():
    """AST, not substrings (CLAUDE.md's tier 3): a comment naming `debug_action` is fine and several
    of them are load-bearing history, while a CALL to it is an ImportError waiting for the one config
    that reaches it."""
    offenders: list[str] = []
    for path in sorted(LOOPLAB.rglob("*.py")):
        # utf-8-sig: `adapters/repo_task.py` carries a BOM, which `ast.parse` refuses.
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = (fn.id if isinstance(fn, ast.Name)
                        else fn.attr if isinstance(fn, ast.Attribute) else None)
                if name in _PRODUCER_NAMES:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} calls {name}")
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in _PRODUCER_NAMES:
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} imports {alias.name}")
    assert offenders == [], offenders


def test_nothing_in_the_engine_or_search_mints_a_debug_action():
    """The constant survives as an event-log spelling; what must not come back is a producer.

    Substring-based on purpose — this is a NEGATIVE pin, and CLAUDE.md's rule for those is that what
    must not return is the TEXT, a commented-out copy included. `search/policy.py` is exempt because
    it DECLARES `KIND_DEBUG` and documents the removal beside it; `card_selection` and the
    orchestrator are the two that used to mint the action."""
    for module in (card_selection, __import__("looplab.engine.orchestrator", fromlist=["x"])):
        src = inspect.getsource(module)
        assert '{"kind": "debug"' not in src, module.__name__
        assert '"kind": "debug",' not in src, module.__name__


def test_the_inert_knobs_are_kept_on_purpose_and_still_load():
    """`debug_depth` and `KIND_DEBUG` are deliberately NOT deleted — `LOOPLAB_<FIELD>` env vars, every
    `config.snapshot.json` under `runs/`, and `search/speculation_calibration.py`'s pinned runtime
    envelope all name `debug_depth`, so removing it would revoke calibration receipts for a knob that
    no longer does anything. This pins that the compatibility surface is intact WITHOUT pinning that
    it has an effect."""
    from looplab.core.config import Settings
    assert Settings().debug_depth >= 1
    assert KIND_DEBUG == "debug"
    # Every policy still accepts the kwarg, so a snapshot-driven construction cannot TypeError.
    for pol in (GreedyTree(debug_depth=3), EvolutionaryPolicy(debug_depth=3),
                MCTSPolicy(debug_depth=3), ASHAPolicy(debug_depth=3)):
        assert pol.debug_depth == 3
    assert not hasattr(policy_mod, "debug_action")
