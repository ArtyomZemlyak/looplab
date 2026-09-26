"""Two ways a run lost its second build lane, and the operator's lane width that keeps it.

Measured on MiniOneRec inf13 (one GPU, build width 2, evaluations of 1-3 minutes, builds of hours):

1. THE SPIN. 11:50-12:44 the evolutionary policy's due crossover (nodes 2 and 0, then 2 and 7) was
   "proposed" by the speculative raw lane every ~1.1 s while card-8 and then card-9 were BUILDING
   exactly that merge. A merge's Idea is a pure function of its parents
   (`node_build.py::_ensemble_idea`), so each proposal took 0.001 s, staged as `reuse` of the Card
   being built, could not be elected (it is the Card being built), and the session handed back.
   5,190 `phase_progress` rows, 58% of the log; each row moved the tail, so the hand-back rate limit
   (which waits for the tail to move) never held.
2. THE DEAD INVENTORY. card-8 was discarded `not_selected_now` once node 7 and then node 5 overtook
   node 0, and sat `proposed/open` with no evidence. `_live_card_action` makes a crossover live only
   on the current metric top-two, so nothing could ever elect it, but `unconsumed_card_inventory`
   counted it: `supply - outstanding = 1` against a prefetch ceiling of min(depth 1, lane 1) = 1
   refused the raw lane every turn, and the run held ONE build for hours with the GPU idle.
3. THE LANE WIDTH. `card_lane_width` has always read a policy's `card_select_k`; nothing set it.
   `Settings.card_select_k` is the operator's handle on it, carried through every Strategist rebuild.
"""
from __future__ import annotations

import time

import anyio
import pytest

from looplab.core.config import Settings
from looplab.core.models import NodeStatus, RunState
from looplab.engine.options import EngineOptions
from looplab.engine.speculation import SpeculationMixin
from looplab.events.replay import fold
from looplab.events.types import EV_NODE_EVALUATED
from looplab.search.card_selection import (
    SpeculativeSelectionContext,
    card_lane_width,
    speculative_raw_actions,
    unconsumed_card_inventory,
)
from looplab.search.policy import (
    RUN_OWNED_POLICY_KNOBS,
    ASHAPolicy,
    EvolutionaryPolicy,
    GreedyTree,
    MCTSPolicy,
    make_policy,
    policy_knobs,
)
from tests.test_card_speculation_engine import (  # noqa: F401  (autouse receipt fixture)
    _admit_unit_speculation_receipt,
)
from tests.test_card_speculative_selection import _node, _ready_card


def _board(*, merge_parents=(0, 1), extra_nodes=(), cards=()):
    nodes = {
        0: _node(0, metric=0.9),
        1: _node(1, metric=0.8),
        2: _node(2, metric=0.7),
        **{node.id: node for node in extra_nodes},
    }
    state = RunState(direction="max", nodes=nodes, best_node_id=0,
                     cards={card.id: card for card in cards})
    return state


# ------------------------------------------------------------------ 1. the raw lane and a building merge

def test_the_due_crossover_is_not_proposed_while_its_card_is_being_built():
    policy = EvolutionaryPolicy(pop=3, max_nodes=12, elite=2, debug_depth=0)
    building = _ready_card("building", operator="merge", parents=(0, 1))
    state = _board(cards=[building])
    assert policy.next_actions(state) == [{"kind": "merge", "parent_ids": [0, 1]}], (
        "the fixture must make the crossover of the top two the policy's due action")
    in_flight = SpeculativeSelectionContext(excluded_card_ids={"building"})
    assert speculative_raw_actions(state, policy, 12, context=in_flight) == [], (
        "MUTATION: drop the `building_merges` filter and this returns the same merge — the proposal "
        "that staged as `reuse` of the Card being built every second on inf13")


def test_the_crossover_is_proposed_when_no_card_is_building_it():
    policy = EvolutionaryPolicy(pop=3, max_nodes=12, elite=2, debug_depth=0)
    other = _ready_card("other", operator="merge", parents=(1, 2))
    state = _board(cards=[other])
    raw = speculative_raw_actions(
        state, policy, 12, context=SpeculativeSelectionContext(excluded_card_ids={"other"}))
    assert [(a["kind"], sorted(a["parent_ids"])) for a in raw] == [("merge", [0, 1])]


def test_an_improve_of_the_same_parent_stays_proposable_beside_a_building_improve():
    """An improve is a different paid proposal every time — legitimate parallel work, not a twin."""
    policy = GreedyTree(n_seeds=0, max_nodes=12, debug_depth=0)
    building = _ready_card("building", operator="improve", parents=(0,))
    state = _board(cards=[building])
    raw = speculative_raw_actions(
        state, policy, 12, context=SpeculativeSelectionContext(excluded_card_ids={"building"}))
    assert raw and raw[0]["kind"] == "improve" and raw[0]["parent_id"] == 0


# ------------------------------------------------------------------ 2. the dead inventory

def test_a_crossover_the_board_moved_past_is_not_inventory():
    stale = _ready_card("card-8", operator="merge", parents=(1, 2))   # top two are 0 and 1
    state = _board(cards=[stale])
    assert unconsumed_card_inventory(state) == 0, (
        "MUTATION: drop `_dead_on_this_board` and this is 1 — card-8, which filled the prefetch "
        "ceiling of one by itself on inf13")


def test_the_current_top_two_crossover_is_still_inventory():
    live = _ready_card("live", operator="merge", parents=(0, 1))
    assert unconsumed_card_inventory(_board(cards=[live])) == 1


def test_a_crossover_with_a_parent_still_pending_keeps_counting():
    """Its metric can still make the pair the top two: not moved past yet."""
    pending = _node(3, metric=None, status=NodeStatus.pending)
    card = _ready_card("waiting", operator="merge", parents=(1, 3))
    assert unconsumed_card_inventory(_board(extra_nodes=[pending], cards=[card])) == 1


def test_a_debug_card_is_never_inventory():
    card = _ready_card("dbg", operator="debug", parents=(0,))
    assert unconsumed_card_inventory(_board(cards=[card])) == 0


def test_an_ordinary_improve_still_counts_by_kind_not_readiness():
    card = _ready_card("imp", operator="improve", parents=(2,))
    card.selection_ready = False
    card.selection_blockers = ["freshness_unknown"]
    assert unconsumed_card_inventory(_board(cards=[card])) == 1


def test_the_inf13_shape_reopens_the_raw_lane():
    """One build in flight (excluded) plus the discarded crossover: the raw-lane gate reads
    `supply - outstanding < ceiling`, and the dead crossover alone used to make it 1 < 1."""
    stale = _ready_card("card-8", operator="merge", parents=(1, 2))
    in_flight = _ready_card("card-11", operator="improve", parents=(2,))
    state = _board(cards=[stale, in_flight])
    state.card_build_requests = [{"card_id": "card-11", "generation": 0}]
    ceiling = 1
    assert SpeculationMixin._prefetch_supply_used(state) - len(
        SpeculationMixin._outstanding_requests(state)) < ceiling


# ------------------------------------------------------------------ 3. the operator's lane width

@pytest.mark.parametrize("name, expected_default", [("greedy", 1), ("evolutionary", 2), ("mcts", 3)])
def test_make_policy_stamps_the_operator_width(name, expected_default):
    base = dict(n_seeds=3, max_nodes=12, ablate_every=0, debug_depth=1, operator_bandit=False,
                asha_eta=3, asha_rung_nodes=0, mcts_cost_weight=0.0, mcts_value_weight=0.0,
                model_arms={})
    assert card_lane_width(make_policy(name, **policy_knobs(**base, card_select_k=None))) \
        == expected_default
    assert card_lane_width(make_policy(name, **policy_knobs(**base, card_select_k=2))) == 2


def test_asha_keeps_its_own_lane():
    base = dict(n_seeds=3, max_nodes=12, ablate_every=0, debug_depth=1, operator_bandit=False,
                asha_eta=3, asha_rung_nodes=0, mcts_cost_weight=0.0, mcts_value_weight=0.0,
                model_arms={}, card_select_k=4)
    for name in ("asha", "bohb"):
        policy = make_policy(name, **policy_knobs(**base))
        assert isinstance(policy, ASHAPolicy)
        assert not hasattr(policy, "card_select_k")
        assert card_lane_width(policy) == 1


def test_the_strategist_cannot_restate_the_operator_width():
    assert "card_select_k" in RUN_OWNED_POLICY_KNOBS


def test_the_setting_reaches_the_engine_knob():
    assert Settings().card_select_k is None
    options = EngineOptions.from_settings(Settings(card_select_k=2))
    assert options.card_select_k == 2
    with pytest.raises(Exception):
        Settings(card_select_k=0)


def test_a_strategist_policy_switch_keeps_the_operator_width(tmp_path):
    from tests.test_card_speculation_engine import _engine, _start
    engine, _unused = _engine(tmp_path / "switch", depth=0)
    engine.__dict__["_card_select_k"] = 2
    _start(engine)
    engine._apply_strategy({"policy": "greedy", "policy_params": {"card_select_k": 7}})
    assert isinstance(engine.policy, GreedyTree)
    assert card_lane_width(engine.policy) == 2, (
        "a rebuild must carry the operator's width, and a policy_params entry must not restate it")
    engine._apply_strategy({"policy": "mcts"})
    assert isinstance(engine.policy, MCTSPolicy) and card_lane_width(engine.policy) == 2


# ------------------------------------------------------------------ 4. the spin, through the engine

def _merge_board_engine(tmp_path, monkeypatch):
    from tests.test_card_speculation_engine import (
        _add_ready_draft,
        _commit_speculative_node,
        _engine,
        _Researcher,
        _start,
        _without_research,
    )
    from tests.test_several_card_producers_build_at_once import _GatedDeveloper

    engine, _unused = _engine(tmp_path / "merge", depth=1)
    log: list = []
    engine.role_factory = lambda: (_Researcher(), _GatedDeveloper(log))
    engine._llm_parallel = 2
    engine._llm_parallel_launched = 2
    engine._llm_parallel_startup_auto = False
    _without_research(monkeypatch, engine)
    _start(engine)
    _GatedDeveloper.gate = False
    for index, x in enumerate((0.1, 0.2)):
        _add_ready_draft(engine, f"card-{index}", x=x)
        node_id = _commit_speculative_node(engine)
        engine.store.append(EV_NODE_EVALUATED, {
            "node_id": node_id, "generation": 0, "metric": 1.0 + index, "eval_seconds": 0.0})
    engine.policy = EvolutionaryPolicy(pop=2, max_nodes=8, elite=2, debug_depth=0)
    state = fold(engine.store.read_all())
    assert engine.policy.next_actions(state)[0]["kind"] == "merge"
    return engine, log, _GatedDeveloper


def _merge_proposals(engine) -> int:
    return sum(
        1 for e in engine.store.read_all()
        if e.type == "phase_progress" and (e.data or {}).get("speculative")
        and (e.data or {}).get("operator") == "merge" and (e.data or {}).get("phase") == "propose"
        and (e.data or {}).get("status") == "started")


def test_a_building_crossover_is_not_re_proposed_every_turn(tmp_path, monkeypatch):
    engine, log, gated = _merge_board_engine(tmp_path, monkeypatch)
    gated.gate = True

    async def scenario():
        async with anyio.create_task_group() as eval_tg:
            engine._eval_task_group = eval_tg
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                with anyio.move_on_after(max(0.0, deadline - time.monotonic())):
                    await engine._run_card_session([], fold(engine.store.read_all()), None)
                engine._card_boundary_debt = False
                await anyio.sleep(0.02)
            from tests.test_several_card_producers_build_at_once import _live
            assert _live(log) == 1, f"exactly the one merge build is running, got {_live(log)}"
            gated.gate = False
            for _what, _card, developer in list(log):
                developer.release.set()
            with anyio.fail_after(20):
                while fold(engine.store.read_all()).card_builds_done < 3:
                    await engine._run_card_session([], fold(engine.store.read_all()), None)
                    engine._card_boundary_debt = False
                    await anyio.sleep(0.02)

    anyio.run(scenario)
    assert _merge_proposals(engine) == 1, (
        f"{_merge_proposals(engine)} speculative merge proposals: the crossover being built was "
        "re-proposed while it built (inf13: every ~1.1 s for 54 minutes)")
