"""The occupancy pace must REACH production, and an ASHA bracket it cannot know must still refuse.

WHAT THIS FILE IS FOR. `engine/cadence.py::occupancy_due` — "an evaluation is running and the supply
behind it does not cover the slots" — has been wired since 2026-08-14 and read as due for five
straight hours on 2026-08-20 while producing nothing. `runs/e5small-dr-unified-v4`: node 0 scored
0.758851, node 2 was killed by the freshness gate having never been admitted, and GPU 0 then sat
dark from 09:45:42 to 15:04:57 while node 1 trained alone on GPU 1. `phase_progress` for node 3
started in the same MINUTE as node 1's terminal. The run was not stuck, not looping and not out of
budget (`max_nodes` 14, three nodes created); the Strategist itself had set `eval_parallel: 2`.

The cause was not the pace. It was `card_selection._speculative_selection`, whose two ASHA masking
guards returned `[], []` for the WHOLE query — both producer lanes — whenever one of the masked
in-flight Nodes was an unresolved rung-0 root. The Strategist had moved that run to `bohb`, which is
an `ASHAPolicy`. So the one situation the pace exists for — an in-flight Node holding a slot, masked
so the producer can ask what ELSE to build — was the exact situation in which the answer was
structurally empty. Replayed over every run on this box: 8.03 starved hours across the five runs
that had the pace, of which 5.94 were this, and 0.00 in every GreedyTree/EvolutionaryPolicy run.

WHY THESE TESTS ARE DRIVEN AND NOT STRUCTURAL. docs/47 §6: nine mechanisms in one day shipped a
guard that named the MECHANISM rather than the PROPERTY and passed without the behaviour. An AST
test that `_occupancy_paced_creates` calls `occupancy_due` was GREEN throughout the five dark hours,
because the call exists and is reached — it is the answer that was empty. So every test here runs
the real selection over a real board and asserts on what came back, and the scope tests below assert
the guard STILL refuses, so "delete the guard" cannot pass this file either.
"""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from looplab.core.models import Card, Idea, Node, NodeStatus, RunState
from looplab.engine.cadence import occupancy_due
from looplab.events.replay import fold
from looplab.search.card_selection import (
    SpeculativeSelectionContext,
    card_budget_used,
    speculative_card_actions,
    speculative_raw_actions,
)
from looplab.search.policy import make_policy

# EVERY built-in family, named as a LIST so a test can assert it covered all of them rather than
# silently covering whichever ones a parametrize happened to enumerate. `mcts` is here because the
# registry guard below caught its absence on the first run of this file — which is the whole reason
# that guard exists (docs/47 §6).
POLICY_FAMILIES = ("greedy", "asha", "bohb", "evolutionary", "mcts")


def _node(node_id: int, *, status: NodeStatus, parents: tuple[int, ...] = (),
          metric: float | None = None, card_id: str | None = None) -> Node:
    return Node(
        id=node_id,
        parent_ids=list(parents),
        operator="improve" if parents else "draft",
        idea=Idea(operator="improve" if parents else "draft", card_id=card_id,
                  hypothesis=f"hypothesis {node_id}"),
        status=status,
        metric=metric,
    )


def _board(*nodes: Node, cards: tuple[Card, ...] = ()) -> RunState:
    """The `e5small-dr-unified-v4` shape: whatever nodes the caller names, and nothing else."""
    return RunState(nodes={node.id: node for node in nodes},
                    cards={card.id: card for card in cards})


def _lanes(state: RunState, policy_name: str, *, running: frozenset[int], max_nodes: int = 14,
           n_seeds: int = 3):
    """Exactly the two lanes `_occupancy_paced_creates` consults, in its order."""
    policy = make_policy(policy_name, n_seeds=n_seeds, max_nodes=max_nodes)
    context = SpeculativeSelectionContext(scoring=None, ignored_pending_node_ids=running,
                                          resource_envelope=None)
    owned = speculative_card_actions(state, policy, max_nodes, context=context)
    return owned or speculative_raw_actions(state, policy, max_nodes, context=context)


# --------------------------------------------------------------------------------------------
# 1. THE PROPERTY: a held slot and an empty board must reach production.

def test_the_live_board_that_went_dark_for_five_hours_now_produces():
    """The exact `e5small-dr-unified-v4` board, at the fold inside the hole.

    node 0 evaluated at 0.758851, node 1 pending and burning GPU 1, node 2 killed by the Card
    freshness gate having never been admitted. `occupancy_due` reads True; before the fix BOTH lanes
    answered `[]` and the engine had nothing to build for five hours and nineteen minutes.

    THE ARITHMETIC IS THE BOARD and it is asserted rather than assumed. Node 2 is absent here
    because it was absent from the budget: it never ran, so `core/models.py::
    is_unevaluated_speculative_discard` refunded its slot and
    `node_counts_toward_card_budget` answered False — measured on the run itself,
    `card_budget_used` is 2 against a seed target of 3, which is the state in which the forced seed
    prefix has an answer and the run built nothing anyway. Modelling node 2 as an ordinary `failed`
    node would make `card_budget_used` 3, satisfy the seed target, and quietly test a DIFFERENT
    board — the one where the refusal below is correct.
    """
    state = _board(
        _node(0, status=NodeStatus.evaluated, metric=0.758851, card_id="card-0"),
        _node(1, status=NodeStatus.pending, card_id="card-1"),
    )
    policy = make_policy("bohb", n_seeds=3, max_nodes=14)
    assert card_budget_used(state) == 2, "the live board spent 2 of its 14 node slots"
    running = frozenset({1})
    queued = {node.id for node in state.pending_nodes()} - running
    assert queued == set(), "precondition: the board behind the running node must be empty"
    assert occupancy_due(inflight=len(running), queued=len(queued), width=2) is True

    produced = _lanes(state, "bohb", running=running)
    assert produced, (
        "a free slot, an empty board and a running ASHA rung-0 root produced nothing — this is the "
        "5 h 19 m hole in runs/e5small-dr-unified-v4 on 2026-08-20")
    assert all(action.get("kind") in ("draft", "improve", "merge") for action in produced), produced


@pytest.mark.parametrize("policy_name", POLICY_FAMILIES)
def test_every_policy_family_reaches_production_while_a_slot_is_held(policy_name):
    """The defect was one FAMILY answering nothing where the others answered work.

    Parametrized over every built-in policy so the fix cannot be "ASHA happens to work on the one
    board I tried": the companion test below asserts this list is the whole registry, which is what
    stops this from being a count that cannot tell "all passed" from "nothing was checked".
    """
    state = _board(
        _node(0, status=NodeStatus.evaluated, metric=0.5, card_id="card-0"),
        _node(1, status=NodeStatus.pending, card_id="card-1"),
    )
    produced = _lanes(state, policy_name, running=frozenset({1}))
    assert produced, f"{policy_name} produced nothing while a slot was held and the board was empty"


def test_the_family_list_this_file_drives_is_the_whole_registry():
    """Every count needs to distinguish "all passed" from "nothing was checked" (docs/47 §6).

    Without this, a policy added to the registry tomorrow would inherit the defect and every test
    above would stay green.
    """
    from looplab.search.policy import available_policies

    assert set(POLICY_FAMILIES) == set(available_policies()), (
        "a policy family exists that this file never drives: "
        f"{set(available_policies()) ^ set(POLICY_FAMILIES)}")


# --------------------------------------------------------------------------------------------
# 2. THE SCOPE: the guard still refuses where the MASKED POLICY VIEW would answer unsoundly.

def test_an_asha_bracket_the_masked_view_cannot_know_is_still_refused():
    """The negative control, and the reason this is a scoping change and not a deletion.

    Seeding is COMPLETE here, so the forced seed prefix answers `None` and the query falls through
    to the discretionary lane — the one place `_speculative_policy_state` deletes the masked Nodes
    out of what ASHA reads. A masked, unresolved rung-0 root there really would read as a rung-0
    vacancy, so the refusal stands. Deleting `_asha_mask_is_unsound` turns this red.
    """
    state = _board(
        _node(0, status=NodeStatus.evaluated, metric=0.5, card_id="card-0"),
        _node(1, status=NodeStatus.evaluated, metric=0.6, card_id="card-1"),
        _node(2, status=NodeStatus.pending, card_id="card-2"),
    )
    # three roots against n_seeds=3: the seed prefix is satisfied and cannot be what answers.
    assert _lanes(state, "asha", running=frozenset({2}), n_seeds=3) == [], (
        "the discretionary lane answered for an ASHA bracket whose rung-0 root has no metric yet — "
        "the masked view reads that slot as free and the policy would propose a replacement draft")


def test_a_masked_promotion_without_its_exact_durable_action_is_still_refused():
    """The second clause of the predicate, unchanged in force.

    The masked node is a PROMOTION (it has parents) whose Card does not carry
    `("improve", its parents)`. Masking it would make the parent look unexpanded and permit a
    duplicate same-rung child, so the whole query still refuses.
    """
    state = _board(
        _node(0, status=NodeStatus.evaluated, metric=0.5, card_id="card-0"),
        _node(1, status=NodeStatus.evaluated, metric=0.6, card_id="card-1"),
        _node(2, status=NodeStatus.evaluated, metric=0.7, card_id="card-2"),
        # a promotion child of node 2, pending, carrying NO card at all
        _node(3, status=NodeStatus.pending, parents=(2,), card_id=None),
    )
    assert _lanes(state, "asha", running=frozenset({3}), n_seeds=3) == [], (
        "a masked promotion with no exact durable action was allowed to drive selection — the "
        "parent reads as unexpanded and the same rung can be filled twice")


# --------------------------------------------------------------------------------------------
# 3. THE BOUND: what production is allowed to cost.

def test_the_seed_census_counts_the_masked_root_so_production_cannot_overfill_rung_zero():
    """The soundness argument for the fix, driven as a COUNT rather than asserted in prose.

    The forced seed prefix computes `seeded` over the UNMASKED board, so the running root is still
    counted and the prefix offers only the seeds that are genuinely MISSING. Two boards, one seed
    apart, must answer one draft apart — a bound that cannot be satisfied by a lane that ignores the
    mask (which would offer 2 in both cases) or by one that refuses (0 in both).
    """
    two_seeded = _board(
        _node(0, status=NodeStatus.evaluated, metric=0.5, card_id="card-0"),
        _node(1, status=NodeStatus.pending, card_id="card-1"),
    )
    one_seeded = _board(_node(1, status=NodeStatus.pending, card_id="card-1"))
    with_two = _lanes(two_seeded, "asha", running=frozenset({1}), n_seeds=3)
    with_one = _lanes(one_seeded, "asha", running=frozenset({1}), n_seeds=3)
    assert len(with_two) == 1, with_two
    assert len(with_one) == 2, with_one


def test_a_board_that_covers_its_slots_is_not_due_at_all():
    """The pace's own arithmetic, at the two boundaries production must not cross."""
    assert occupancy_due(inflight=2, queued=0, width=2) is False   # saturated
    assert occupancy_due(inflight=1, queued=1, width=2) is False   # supply covers the free slot
    assert occupancy_due(inflight=0, queued=0, width=2) is False   # the ordinary create turn
    assert occupancy_due(inflight=1, queued=0, width=2) is True    # the one due shape


# --------------------------------------------------------------------------------------------
# 4. NO BUSY-POLL: the engine gate, driven through the real method.

def _engine(run_dir, policy_name="asha", *, max_nodes=14):
    from looplab.adapters.toytask import ToyTask
    from tests.factories import make_engine

    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    engine = make_engine(
        run_dir, task=task,
        policy=make_policy(policy_name, n_seeds=3, max_nodes=max_nodes),
        max_nodes=max_nodes,
        card_driven_selection=True, speculation_depth=2, eval_parallel=2)
    engine._gpu_ids, engine._gpu_physical_ids, engine._gpu_mem, engine._free_gpus = [], {}, {}, []
    engine._eval_parallel = 2
    return engine


def test_the_pace_does_not_re_fire_while_its_own_build_is_outstanding(tmp_path):
    """Production is self-clearing, and the three ways it clears are asserted separately.

    A pace that fired again on the next turn would be a busy-poll paying a proposal per turn for the
    whole of a multi-hour evaluation. The first assertion is the positive control: without it the
    three refusals below would pass on an engine that can never produce anything at all.
    """
    engine = _engine(tmp_path / "occupancy-bound")
    engine._eval_inflight = {(1, 0)}
    board = _board(
        _node(0, status=NodeStatus.evaluated, metric=0.5, card_id="card-0"),
        _node(1, status=NodeStatus.pending, card_id="card-1"),
    )
    evals = [{"kind": "evaluate", "node_id": 1}]

    produced = engine._occupancy_paced_creates(board, evals)
    assert produced, "the engine gate produced nothing on the board the pace exists for"
    assert len(produced) == 1, ("production is bounded by the FREE slots, not by the lane's width: "
                                f"{produced}")

    # …its own build is in flight -> a build is already answering this.
    building = board.model_copy(update={"buildings": {9: {"card_id": "card-9"}}})
    assert engine._occupancy_paced_creates(building, evals) == []

    # …the node it built now sits on the board -> the supply covers the slot, so it is not due.
    filled = _board(
        _node(0, status=NodeStatus.evaluated, metric=0.5, card_id="card-0"),
        _node(1, status=NodeStatus.pending, card_id="card-1"),
        _node(3, status=NodeStatus.pending, card_id="card-3"),
    )
    assert engine._occupancy_paced_creates(filled, evals) == []

    # …a slot could be filled from the board instead -> do that, do not mint.
    assert engine._occupancy_paced_creates(
        board, [{"kind": "evaluate", "node_id": 7}]) == []


def test_a_saturated_engine_produces_nothing(tmp_path):
    engine = _engine(tmp_path / "occupancy-saturated")
    engine._eval_inflight = {(0, 0), (1, 0)}
    board = _board(
        _node(0, status=NodeStatus.pending, card_id="card-0"),
        _node(1, status=NodeStatus.pending, card_id="card-1"),
    )
    evals = [{"kind": "evaluate", "node_id": 0}, {"kind": "evaluate", "node_id": 1}]
    assert engine._occupancy_paced_creates(board, evals) == [], (
        "both slots are burning and the pace still asked for more work")


# --------------------------------------------------------------------------------------------
# 5. TIER 1: the whole engine, the real loop, an ASHA policy, a held evaluation.

def _node_created_ids(engine) -> set[int]:
    return {event.data.get("node_id") for event in engine.store.read_all()
            if event.type == "node_created"}


def test_a_running_asha_root_no_longer_holds_the_whole_board_dark(tmp_path, monkeypatch):
    """The `runs/e5small-dr-unified-v4` regression at Tier 1 (CLAUDE.md's ladder).

    The sibling greedy test — `test_card_refill_unequal_durations.py::
    test_the_board_is_refilled_because_an_evaluation_is_running` — is the POSITIVE CONTROL for this
    harness and was green throughout the five dark hours. Swapping in an ASHA policy is the input
    that made it fail: measured on the pre-fix tree, `refilled` is False here and every node is
    built serially after its predecessor's terminal.
    """
    engine = _engine(tmp_path / "asha-occupancy", max_nodes=4)
    real_evaluate = engine._evaluate
    order: list[int] = []
    refilled = anyio.Event()

    async def _held_eval(node_id, limiter, max_es):
        order.append(node_id)
        if len(order) == 1:
            # Hold the slot only until the run builds something else; a bounded wait so a red
            # assertion reports a dark GPU instead of hanging the suite.
            with anyio.move_on_after(30):
                while _node_created_ids(engine) <= {node_id}:
                    await anyio.sleep(0.02)
                refilled.set()
        return await real_evaluate(node_id, limiter, max_es)

    monkeypatch.setattr(engine, "_evaluate", _held_eval)
    anyio.run(engine.run)

    assert order, "no evaluation ran at all, so nothing ever held a slot"
    assert refilled.is_set(), (
        "under an ASHA-family policy nothing was built for the whole of an evaluation that held a "
        "slot: the board stayed empty and the build latency was paid serially after the terminal — "
        "5 h 19 m of it in runs/e5small-dr-unified-v4 on 2026-08-20")
    state = fold(engine.store.read_all())
    assert len(state.nodes) >= 2
