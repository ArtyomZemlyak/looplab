"""The endgame on MiniOneRec inf13 (2026-09-27): whom it merges, who may buy inside it, what counts as
a stall, and when a stall endgame ends.

inf13 (`max_nodes` 100000, one GPU) wrote a `stagnation` plan row at node 12 that made the endgame
PERMANENT (reserve 99,988, kinds merge + sweep). Four defects, each driven here:

  P0-a  the ensemble paired node 5 with node 12 — its OWN CHILD — and the same pair was minted twice
        (card-15 -> node 13, recall -0.262; card-16 building); `_is_endgame_action` accepted ANY merge
        Card. `engine/plan.py::_EndgameView.merge_admissible` is the rule for both.
  P0-b  the raw lane paid for card-14 (an improve of node 12) that the gate then displaced, and the
        speculative election built card-16, which the gate refuses — neither lane asked the gate.
        `endgame_admits` is the one predicate; `endgame_refused_card_ids` leaves every election.
  P1-a  the stall rung counted nodes 6-10 — builds proposed against node 2 before node 5 won (ids are
        reserved at BUILD START) — as failed pushes on node 5. `stall_rung` now counts attempts ON the
        champion only.
  P1-b  the endgame never ended; `Settings.endgame_stall_nodes` bounds a stall episode and reopens
        the plan, one episode per champion, carried through the Card-mode budget flicker.
"""
from __future__ import annotations

import random

import anyio
import pytest

from looplab.agents.strategist import STALL_OPERATORS, stall_rung
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.orchestrator import Engine
from looplab.engine.plan import (
    HARD_STALL_RUNGS, META_SWEEP, PLAN_REASONS, REOPEN_CAUSES, build_plan, endgame_actions,
    endgame_admits, endgame_admitted, endgame_refused_card_ids, in_endgame, replan)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_PLAN
from looplab.search.card_selection import (
    META_CARD_ID, SpeculativeSelectionContext, card_action, card_budget_used, card_next_actions,
    speculative_card_actions, speculative_raw_actions)
from looplab.search.policy import EvolutionaryPolicy, GreedyTree
from tests.factories import make_engine


# ------------------------------------------------------------------------------------ the board
class _Board:
    """A run log written event by event (the shapes the engine writes), folded on demand."""

    def __init__(self, path, direction: str = "max"):
        self.store = EventStore(path / "events.jsonl")
        self.store.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g",
                                          "direction": direction})

    def state(self) -> RunState:
        return fold(self.store.read_all())

    def card(self, card_id: str, operator: str, parents=(), *, scored_against=None) -> str:
        """A native `card_added` receipt, fenced on the parents' and the anchor's current attempts."""
        st = self.state()
        idea = Idea(operator=operator, params={"x": float(len(st.cards))},
                    rationale=f"work item {card_id}", hypothesis=f"hypothesis of {card_id}",
                    card_id=card_id)
        anchor = st.nodes.get(scored_against) if scored_against is not None else None
        action = Engine._card_action(
            idea, list(parents), {str(p): st.nodes[p].attempt for p in parents}, scored_against,
            anchor.attempt if anchor is not None else None,
            scored_against_empty=scored_against is None)
        self.store.append("card_added", Engine._card_added_payload(
            card_id, Engine._card_statement(idea), action, idea,
            source="engine" if operator == "merge" else "researcher", at_node=len(st.nodes)))
        return card_id

    def node(self, nid: int, operator: str, parents=(), *, metric=None, failed=False, card=None,
             scored_against=None) -> None:
        idea = {"operator": operator, "params": {"x": float(nid)}}
        if card is not None:
            self.card(card, operator, parents, scored_against=scored_against)
            idea.update(card_id=card, hypothesis=f"hypothesis of {card}")
        self.store.append("node_created", {"node_id": nid, "parent_ids": list(parents),
                                           "operator": operator, "idea": idea})
        if metric is not None:
            self.evaluate(nid, metric)
        elif failed:
            self.store.append("node_failed", {"node_id": nid, "generation": 0, "error": "boom",
                                              "reason": "crash"})

    def evaluate(self, nid: int, metric: float) -> None:
        self.store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": metric})

    def plan(self, row: dict) -> dict:
        self.store.append(EV_PLAN, row)
        return row


def _inf13(board: _Board, *, through: int = 13) -> _Board:
    """MiniOneRec inf13's DAG and Card fences as its log records them (lifecycles flattened). Node 5
    (improve of node 2) was BUILT fifth and won last: nodes 6-10 were proposed against node 2 (or 0)
    while node 5 was still unscored, and only node 11 — the merge of 5 and 10 — was aimed at it."""
    b = board
    b.node(0, "draft", metric=1.3269, card="card-0")
    b.node(1, "draft", metric=0.0, card="card-1", scored_against=0)
    b.node(2, "draft", metric=1.3726, card="card-2", scored_against=0)
    b.node(3, "improve", [2], metric=1.2382, card="card-5", scored_against=2)
    b.node(4, "draft", metric=1.014, card="card-3", scored_against=0)
    b.node(5, "improve", [2], card="card-6", scored_against=2)
    b.node(6, "improve", [0], failed=True, card="card-4", scored_against=2)
    b.node(7, "improve", [2], metric=1.3289, card="card-7", scored_against=2)
    b.node(8, "merge", [2, 7], metric=1.2751, card="card-9", scored_against=2)
    b.card("card-10", "improve", [7], scored_against=2)
    b.card("card-11", "improve", [7], scored_against=2)
    b.evaluate(5, 4.165769)                       # node 5 wins, at seq 8866 on inf13
    b.node(9, "improve", [7], failed=True, card="card-10")
    b.node(10, "improve", [7], metric=1.655387, card="card-11")
    b.node(11, "merge", [5, 10], failed=True, card="card-12", scored_against=5)
    if through >= 12:
        b.node(12, "improve", [5], metric=4.102438, card="card-13", scored_against=5)
    if through >= 13:
        b.node(13, "merge", [5, 12], failed=True, card="card-15", scored_against=5)
    return b


def _inf13_stall_row() -> dict:
    """The row inf13 wrote at node 12, seq 10235 — unbounded, and naming no champion."""
    return build_plan(max_nodes=100000, n_seeds=3, reserve_frac=0.2, at_node=12,
                      reason="stagnation", endgame_start=12)


def _card(state: RunState, card_id: str) -> dict:
    action = card_action(state.cards[card_id])
    assert action is not None
    return action


# ======================================================================= P0-a: the merge partner
def test_the_ensemble_partner_is_outside_the_leaders_lineage_and_never_an_old_pair(tmp_path):
    """inf13 at node 12, driven through the gate: the historical pick was [5, 12], node 12 being node
    5's own child. Walking the ranking — 12 (a descendant), 10 (the pair node 11 already failed on),
    2 (node 5's parent) — the first partner that qualifies is node 7, node 5's SIBLING."""
    b = _inf13(_Board(tmp_path), through=12)
    b.plan(_inf13_stall_row())
    st = b.state()
    assert st.best_node_id == 5 and in_endgame(st.plan, len(st.nodes))
    (merge,) = endgame_actions(st, st.plan, [{"kind": "improve", "parent_id": 12}])
    assert merge["kind"] == "merge" and merge["parent_ids"] == [5, 7], merge
    assert merge["_chosen"] == 5
    assert merge["_reason"] == ("endgame: ensemble of the leader and its best partner outside its "
                                "lineage that no merge has paired it with")


def test_once_the_pair_exists_as_a_merge_node_the_reserve_sweeps_and_refuses_card_16(tmp_path):
    """The re-mint, driven: node 13 (the merge of 5 and 12, failed at recall -0.262) is in the reserve,
    and card-16 — the SAME pair in the other order — is selected. `_is_endgame_action` kept any merge,
    so card-16 kept its slot; now it is refused and the reserve spends the slot on a sweep."""
    b = _inf13(_Board(tmp_path), through=13)
    b.plan(_inf13_stall_row())
    b.card("card-16", "merge", [12, 5], scored_against=5)
    st = b.state()
    card_16 = _card(st, "card-16")
    assert card_16 == {"kind": "merge", "parent_ids": [12, 5], META_CARD_ID: "card-16"}
    assert endgame_admits(st, st.plan, card_16) is False
    (own,) = endgame_actions(st, st.plan, [card_16])
    assert own == {"kind": "improve", "parent_id": 5, META_SWEEP: True, "_chosen": 5,
                   "_reason": "endgame: champion sweep (k-NN surrogate)"}


def test_a_live_merge_card_owns_its_pair_in_either_order(tmp_path):
    """A pair a live merge Card already holds is not the reserve's own merge (in either order), and of
    two live Cards of one pair the OLDEST keeps it — they would otherwise refuse each other forever."""
    b = _inf13(_Board(tmp_path), through=12)
    b.plan(_inf13_stall_row())
    b.card("card-x", "merge", [7, 5], scored_against=5)
    b.card("card-y", "merge", [5, 7], scored_against=5)
    st = b.state()
    (merge,) = endgame_actions(st, st.plan, [{"kind": "draft"}])
    assert merge["parent_ids"] == [5, 0], "node 7 is spoken for; the next unrelated partner is node 0"
    assert endgame_admits(st, st.plan, _card(st, "card-x")) is True
    assert endgame_admits(st, st.plan, _card(st, "card-y")) is False
    assert endgame_actions(st, st.plan, [_card(st, "card-x")]) == [_card(st, "card-x")]


def test_a_merge_card_needs_two_evaluated_parents_and_no_lineage(tmp_path):
    b = _inf13(_Board(tmp_path), through=12)
    b.plan(_inf13_stall_row())
    b.card("card-f", "merge", [5, 11], scored_against=5)      # node 11 failed
    b.card("card-l", "merge", [2, 5], scored_against=5)       # node 2 is node 5's parent
    b.card("card-u", "merge", [0, 4], scored_against=5)       # two unrelated drafts, never merged
    st = b.state()
    assert endgame_admits(st, st.plan, _card(st, "card-f")) is False
    assert endgame_admits(st, st.plan, _card(st, "card-l")) is False
    assert endgame_admits(st, st.plan, _card(st, "card-u")) is True, (
        "a merge of two evaluated, unrelated, unmerged nodes is still an endgame action")


def test_no_qualifying_partner_falls_through_to_the_sweep(tmp_path):
    b = _Board(tmp_path)
    b.node(0, "draft", metric=1.0)
    b.node(1, "improve", [0], metric=0.9)
    b.node(2, "improve", [1], metric=0.8)
    b.plan(build_plan(max_nodes=6, n_seeds=1, reserve_frac=0.5, at_node=0))    # endgame at 3
    st = b.state()
    assert endgame_actions(st, st.plan, [{"kind": "draft"}]) == [
        {"kind": "improve", "parent_id": 0, META_SWEEP: True, "_chosen": 0,
         "_reason": "endgame: champion sweep (k-NN surrogate)"}]
    assert endgame_actions(st, st.plan, [{"kind": "draft"}], sweep=False)[0]["_reason"] == (
        "endgame: refine the champion")


# ================================================================ P0-b: one predicate for every lane
def test_endgame_admits_is_the_gate_asked_about_one_action(tmp_path):
    b = _inf13(_Board(tmp_path), through=12)
    b.card("card-17", "improve", [5], scored_against=5)
    b.card("card-14", "improve", [12], scored_against=5)
    b.card("card-d", "draft")
    row = b.plan(_inf13_stall_row())
    st = b.state()
    later = {**row, "endgame_start": 50}
    assert endgame_admits(st, later, {"kind": "draft"}) is True, "outside the reserve: everything"
    assert endgame_admits(st, None, {"kind": "draft"}) is True
    assert endgame_admits(st, row, {"kind": "evaluate", "node_id": 3}) is True
    assert endgame_admits(st, row, _card(st, "card-17")) is True, "an improve of the champion"
    assert endgame_admits(st, row, _card(st, "card-14")) is False, "inf13's card-14"
    assert endgame_admits(st, row, _card(st, "card-d")) is False
    # A RAW action passes only when it IS the reserve's own next action (here: the merge of 5 and 7).
    assert endgame_admits(st, row, {"kind": "merge", "parent_ids": [7, 5]}) is True
    assert endgame_admits(st, row, {"kind": "merge", "parent_ids": [12, 5]}) is False
    assert endgame_admits(st, row, {"kind": "improve", "parent_id": 5}) is False
    assert endgame_admits(st, row, {"kind": "draft"}) is False
    assert endgame_admitted(st, [{"kind": "draft"}, {"kind": "merge", "parent_ids": [5, 7]}]) == [
        {"kind": "merge", "parent_ids": [5, 7]}]
    # With the ensemble spent the reserve's own action is the sweep — and with the sweep switched
    # off, the refine the policy's own improve of the champion already is.
    b.node(13, "merge", [5, 7], metric=1.0, card="card-e", scored_against=5)
    st = b.state()
    policy_improve = {"kind": "improve", "parent_id": 5, "_reason": "exploit best"}
    assert endgame_admits(st, row, policy_improve) is False
    assert endgame_admits(st, row, policy_improve, sweep=False) is True
    assert endgame_admits(st, row, {"kind": "improve", "parent_id": 5, META_SWEEP: True}) is True


def test_with_nothing_of_its_own_to_spend_the_reserve_refuses_nothing(tmp_path):
    b = _Board(tmp_path)
    b.node(0, "draft", failed=True)
    b.node(1, "draft", failed=True)
    row = b.plan(build_plan(max_nodes=4, n_seeds=1, reserve_frac=0.5, at_node=0))  # endgame at 2
    st = b.state()
    assert st.best_node_id is None
    assert endgame_admits(st, row, {"kind": "draft"}) is True
    assert endgame_actions(st, row, [{"kind": "draft"}]) == [{"kind": "draft"}]
    assert endgame_refused_card_ids(st, row) == frozenset()


def test_the_refused_set_names_the_live_cards_the_gate_displaces_and_is_empty_outside(tmp_path):
    b = _inf13(_Board(tmp_path), through=13)
    b.card("card-14", "improve", [12], scored_against=5)
    b.card("card-16", "merge", [12, 5], scored_against=5)
    b.card("card-17", "improve", [5], scored_against=5)
    before = b.state()
    assert endgame_refused_card_ids(before, before.plan) == frozenset(), "no plan, no refusal"
    b.plan(_inf13_stall_row())
    st = b.state()
    refused = endgame_refused_card_ids(st, st.plan)
    assert {"card-14", "card-16"} <= refused and "card-17" not in refused
    assert all(st.cards[c].status in {"proposed", "building", "coded", "running"} for c in refused)


def test_every_election_leaves_the_refused_cards_out_and_charges_them_no_slot(tmp_path):
    """inf13 at node 14, the SPECULATIVE election driven: the evolutionary policy's due crossover of
    the two elites is [12, 5], so the protected due action filters the lane to card-16 — which the
    gate refuses. Without the plan's refusals the election requests it (what inf13 did); with them it
    elects nothing, and the raw lane's re-derived crossover is refused too, so nothing is bought.

    A refused Card is NOT a reservation: `_reserved_speculative_slots` charges every EXCLUDED id
    without evidence, so carrying refusals there would have spent the last slot on them."""
    b = _inf13(_Board(tmp_path), through=13)
    b.card("card-16", "merge", [12, 5], scored_against=5)
    policy = EvolutionaryPolicy(pop=4, max_nodes=100000, elite=2)
    st = b.state()
    free = SpeculativeSelectionContext()
    assert [a.get(META_CARD_ID) for a in speculative_card_actions(st, policy, 100000, context=free)] \
        == ["card-16"], "precondition: outside a reserve the election builds inf13's card-16"
    b.plan(_inf13_stall_row())
    st = b.state()
    refused = endgame_refused_card_ids(st, st.plan)
    context = SpeculativeSelectionContext(refused_card_ids=refused)
    assert speculative_card_actions(st, policy, 100000, context=context) == []
    raw = speculative_raw_actions(st, policy, 100000, context=context)
    assert raw and raw[0]["kind"] == "merge" and sorted(raw[0]["parent_ids"]) == [5, 12]
    assert endgame_admitted(st, raw) == [], "the raw lane may not pay for the refused crossover"
    assert card_next_actions(st, policy, 100000, refused_card_ids=refused) == raw, (
        "the serial election falls back to the policy's raw action, which the gate then replaces")

    # The slot: one physical slot left, a refused Card beside an admitted one.
    b.card("card-17", "improve", [5], scored_against=5)
    st = b.state()
    greedy = GreedyTree(n_seeds=0, max_nodes=100000, enable_merge=False)
    refused = endgame_refused_card_ids(st, st.plan)
    assert "card-16" in refused and "card-17" not in refused
    one_slot = card_budget_used(st) + 1
    elected = speculative_card_actions(
        st, greedy, one_slot, context=SpeculativeSelectionContext(refused_card_ids=refused))
    assert [a.get(META_CARD_ID) for a in elected] == ["card-17"]
    as_reservations = speculative_card_actions(
        st, greedy, one_slot, context=SpeculativeSelectionContext(excluded_card_ids=refused))
    assert as_reservations == [], "charged as reservations, the refusals would have spent the slot"


# ====================================================================== P1-a: the stall's count
def _historical_rung(state: RunState, window: int) -> int:
    """The count before 2026-09-27: every stall-family node with a higher id than the leader."""
    best = state.best_node_id
    return 0 if best is None else len(
        [n for n in state.nodes.values() if n.id > best and n.operator in STALL_OPERATORS]) // window


def test_the_stall_counts_attempts_on_the_champion_not_every_later_id(tmp_path):
    """inf13's hard stall re-counted. At seq 10235 the historical rung was 2 (nodes 6-11); only node
    11 was aimed at node 5, so the corrected rung is 0 and no endgame starts. After nodes 12 and 13
    (both built on node 5) it is ONE window — still no hard stall."""
    b = _inf13(_Board(tmp_path), through=11)
    st = b.state()
    assert _historical_rung(st, 3) == 2 and stall_rung(st, 3) == (0, 0)
    b.node(12, "improve", [5], metric=4.102438, card="card-13", scored_against=5)
    b.node(13, "merge", [5, 12], failed=True, card="card-15", scored_against=5)
    st = b.state()
    assert _historical_rung(st, 3) == 2 and stall_rung(st, 3) == (1, 14)


def test_a_card_scored_against_the_champion_counts_without_lineage(tmp_path):
    """The Card fence is the receipt of what a proposal tried to beat: an improve of ANOTHER node,
    proposed while the champion reigned, is an attempt on it."""
    b = _Board(tmp_path)
    b.node(0, "draft", metric=0.5, card="c0")
    b.node(1, "draft", metric=1.0, card="c1", scored_against=0)      # the champion
    for nid in (2, 3, 4):
        b.node(nid, "improve", [0], metric=0.1, card=f"c{nid}", scored_against=1)
    assert stall_rung(b.state(), 3) == (1, 5)
    b.node(5, "improve", [0], metric=0.1, card="c5", scored_against=0)   # aimed at node 0: not counted
    assert stall_rung(b.state(), 3) == (1, 5)


def test_a_child_of_an_earlier_lifecycle_and_a_legacy_fence_answer_by_the_folds_order():
    """Two receipts the rule reads directly: `parent_generations` (a child of the champion's EARLIER
    attempt, before a reset re-scored it into the lead, was not built on this champion) and — for a
    Card fence written without a generation — the fold's terminal ORDER."""
    from looplab.core.cards import Card

    st = RunState(direction="max")
    champion = Node(id=0, operator="draft", idea=Idea(operator="draft"), metric=1.0,
                    status=NodeStatus.evaluated, attempt=1, terminal_event_seq=50)
    st.nodes[0] = champion
    st.best_node_id = 0
    old = Node(id=1, operator="improve", idea=Idea(operator="improve"), parent_ids=[0],
               parent_generations={"0": 0}, status=NodeStatus.evaluated, metric=0.2)
    new = Node(id=2, operator="improve", idea=Idea(operator="improve"), parent_ids=[0],
               parent_generations={"0": 1}, status=NodeStatus.evaluated, metric=0.2)
    before = Node(id=3, operator="improve", idea=Idea(operator="improve", card_id="k3"),
                  status=NodeStatus.evaluated, metric=0.2, terminal_event_seq=40)
    after = Node(id=4, operator="improve", idea=Idea(operator="improve", card_id="k4"),
                 status=NodeStatus.evaluated, metric=0.2, terminal_event_seq=60)
    for node in (old, new, before, after):
        st.nodes[node.id] = node
    for cid in ("k3", "k4"):
        st.cards[cid] = Card(id=cid, statement=cid, seed_statement=cid, scored_against=0)
    assert stall_rung(st, 1) == (2, 5), "node 2 (this lifecycle) and node 4 (settled after it) count"


def test_the_corrected_rung_never_exceeds_the_historical_one():
    """The claim that makes a setting unnecessary, over random DAGs with random Card fences."""
    from looplab.core.cards import Card

    rng = random.Random(20260927)
    for _trial in range(300):
        st = RunState(direction="max")
        count = rng.randint(1, 18)
        for nid in range(count):
            op = rng.choice(["draft", "improve", "merge", "ablate", "improve"])
            parents = sorted(rng.sample(range(nid), min(nid, 2 if op == "merge" else 1))) \
                if nid and op != "draft" else []
            card_id = f"k{nid}" if rng.random() < 0.6 else None
            st.nodes[nid] = Node(id=nid, operator=op, parent_ids=parents,
                                 idea=Idea(operator=op, card_id=card_id),
                                 status=NodeStatus.evaluated, metric=rng.random(),
                                 terminal_event_seq=rng.randint(0, 99))
            if card_id is not None:
                st.cards[card_id] = Card(id=card_id, statement=card_id, seed_statement=card_id,
                                         scored_against=rng.choice([None, *range(count)]))
        st.best_node_id = rng.randrange(count)
        for window in (1, 2, 3):
            assert stall_rung(st, window)[0] <= _historical_rung(st, window)


# ================================================================== P1-b: a stall endgame that ends
def test_with_the_setting_off_replan_is_the_historical_rule():
    plan = build_plan(max_nodes=10, n_seeds=3, reserve_frac=0.2, at_node=0)
    stalled = replan(plan, max_nodes=10, n_seeds=3, reserve_frac=0.2, at_node=5,
                     stall_rung=HARD_STALL_RUNGS, champion=0)
    assert stalled == build_plan(max_nodes=10, n_seeds=3, reserve_frac=0.2, at_node=5,
                                 reason="stagnation", endgame_start=5)
    assert "endgame_end" not in stalled and in_endgame(stalled, 9999)
    # The flicker still drops the stall start and the next turn re-starts it — today's rule, kept
    # byte for byte for a run that has not opted in.
    flicker = replan(stalled, max_nodes=9, n_seeds=3, reserve_frac=0.2, at_node=6, stall_rung=2)
    assert flicker["reason"] == "budget_changed" and flicker["endgame_start"] == 7
    assert replan(flicker, max_nodes=9, n_seeds=3, reserve_frac=0.2, at_node=6,
                  stall_rung=2)["endgame_start"] == 6


def _cut(**overrides):
    return {"max_nodes": 100, "n_seeds": 3, "reserve_frac": 0.2, "stall_nodes": 3, **overrides}


def test_a_stall_episode_is_bounded_carried_through_the_flicker_and_reopened():
    plan = build_plan(max_nodes=100, n_seeds=3, reserve_frac=0.2, at_node=0)
    assert replan(plan, at_node=12, stall_rung=1, champion=5, **_cut()) is None
    episode = replan(plan, at_node=12, stall_rung=2, champion=5, **_cut())
    assert episode["reason"] == "stagnation"
    assert (episode["endgame_start"], episode["endgame_end"], episode["champion"]) == (12, 15, 5)
    assert episode["stall_champions"] == [5] and episode["reserve"] == 3
    assert episode["phases"][-1]["nodes"] == 3
    assert [in_endgame(episode, n) for n in (11, 12, 14, 15)] == [False, True, True, False]
    assert replan(episode, at_node=13, stall_rung=3, champion=5, **_cut()) is None
    # THE FLICKER: the Card-mode ceiling subtracts an open build request (100 -> 99 -> 100).
    carried = replan(episode, at_node=13, stall_rung=3, champion=5, **_cut(max_nodes=99))
    assert carried["reason"] == "budget_changed" and carried["max_nodes"] == 99
    assert (carried["endgame_start"], carried["endgame_end"], carried["champion"]) == (12, 15, 5)
    back = replan(carried, at_node=14, stall_rung=3, champion=5, **_cut())
    assert (back["endgame_start"], back["endgame_end"]) == (12, 15) and back["max_nodes"] == 100
    spent = replan(back, at_node=15, stall_rung=3, champion=5, **_cut())
    assert spent["reason"] == "reopened" and spent["reopen_cause"] == "episode_spent"
    assert spent["endgame_start"] == 80 and "endgame_end" not in spent
    assert spent["stall_champions"] == [5] and not in_endgame(spent, 15)
    # ONE episode per champion: the same stall on the same champion starts nothing …
    assert replan(spent, at_node=16, stall_rung=4, champion=5, **_cut()) is None
    # … a flicker after the episode keeps the memory …
    recut = replan(spent, at_node=16, stall_rung=4, champion=5, **_cut(max_nodes=99))
    assert recut["reason"] == "budget_changed" and recut["stall_champions"] == [5]
    # … and a new champion's own stall starts its own episode.
    second = replan(recut, at_node=20, stall_rung=2, champion=17, **_cut(max_nodes=99))
    assert (second["endgame_start"], second["endgame_end"], second["champion"]) == (20, 23, 17)
    assert second["stall_champions"] == [5, 17]


def test_a_new_champion_closes_the_episode_early():
    plan = build_plan(max_nodes=100, n_seeds=3, reserve_frac=0.2, at_node=0)
    episode = replan(plan, at_node=12, stall_rung=2, champion=5, **_cut())
    closed = replan(episode, at_node=13, stall_rung=0, champion=12, **_cut())
    assert closed["reason"] == "reopened" and closed["reopen_cause"] == "champion_changed"
    assert closed["stall_champions"] == [5]
    assert set(REOPEN_CAUSES) == {"champion_changed", "episode_spent", "stall_retracted"}
    assert "reopened" in PLAN_REASONS


def test_a_legacy_unbounded_stall_row_is_re_evaluated_once():
    legacy = build_plan(max_nodes=100, n_seeds=3, reserve_frac=0.2, at_node=12,
                        reason="stagnation", endgame_start=12)
    retracted = replan(legacy, at_node=14, stall_rung=1, champion=5, **_cut())
    assert (retracted["reason"], retracted["reopen_cause"]) == ("reopened", "stall_retracted")
    assert "stall_champions" not in retracted, "a retracted stall charges no champion its episode"
    bounded = replan(legacy, at_node=14, stall_rung=2, champion=5, **_cut())
    assert (bounded["reason"], bounded["endgame_start"], bounded["endgame_end"]) == (
        "stagnation", 12, 15)
    assert bounded["champion"] == 5 and bounded["stall_champions"] == [5]
    over = replan(legacy, at_node=15, stall_rung=2, champion=5, **_cut())
    assert (over["reason"], over["reopen_cause"], over["stall_champions"]) == (
        "reopened", "episode_spent", [5])
    crowned_inside = replan(legacy, at_node=14, stall_rung=2, champion=13, **_cut())
    assert crowned_inside["reopen_cause"] == "champion_changed"
    assert replan(legacy, at_node=14, stall_rung=9, champion=5, **_cut(stall_nodes=0)) is None, (
        "with the setting off the legacy row stays what it was: permanent")


def test_inf13_escapes_its_permanent_endgame_once_it_opts_in(tmp_path):
    """What inf13 does on its first turn with `endgame_stall_nodes` 3: its row at node 12 is
    re-measured with the corrected rung (one window: nodes 11, 12, 13), so it is REOPENED — the
    ordinary cut, endgame at node 80,000 of 100,000 — and node 5 is not charged an episode."""
    b = _inf13(_Board(tmp_path), through=13)
    b.plan(_inf13_stall_row())
    st = b.state()
    rung, _ = stall_rung(st, 3)
    row = replan(st.plan, max_nodes=100000, n_seeds=3, reserve_frac=0.2, at_node=len(st.nodes),
                 stall_rung=rung, stall_nodes=3, champion=st.best_node_id)
    assert rung == 1
    assert (row["reason"], row["reopen_cause"], row["endgame_start"]) == (
        "reopened", "stall_retracted", 80000)
    b.plan(row)
    st = b.state()
    assert not in_endgame(st.plan, len(st.nodes))
    assert endgame_refused_card_ids(st, st.plan) == frozenset()


def test_the_plan_rows_fold_identically_on_replay(tmp_path):
    b = _Board(tmp_path)
    plan = build_plan(max_nodes=100, n_seeds=3, reserve_frac=0.2, at_node=0)
    rows = [plan]
    rows.append(replan(rows[-1], at_node=12, stall_rung=2, champion=5, **_cut()))
    rows.append(replan(rows[-1], at_node=13, stall_rung=2, champion=5, **_cut(max_nodes=99)))
    rows.append(replan(rows[-1], at_node=15, stall_rung=2, champion=5, **_cut(max_nodes=99)))
    for row in rows:
        b.plan(row)
    events = b.store.read_all()
    first, second = fold(events), fold(list(events))
    assert first.plan == second.plan == rows[-1]
    assert first.plan_history == second.plan_history
    assert [h["reason"] for h in first.plan_history] == [
        "initial", "stagnation", "budget_changed", "reopened"]
    assert fold(EventStore(tmp_path / "events.jsonl").read_all()).plan == rows[-1]


def test_the_node_budget_cue_names_the_episode_not_the_budget(tmp_path):
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=100, endgame_reserve_frac=0.2,
                      node_budget_cue=True)
    st = RunState()
    for i in range(13):
        st.nodes[i] = Node(id=i, operator="draft", idea=Idea(operator="draft"))
    plan = build_plan(max_nodes=100, n_seeds=1, reserve_frac=0.2, at_node=0)
    st.plan = replan(plan, max_nodes=100, n_seeds=1, reserve_frac=0.2, at_node=12,
                     stall_rung=2, stall_nodes=3, champion=0)
    text, _ = eng._cue_node_budget(st, None, None)
    assert "falls inside the plan's endgame reserve (experiments #12–#14)" in text
    for i in range(13, 15):
        st.nodes[i] = Node(id=i, operator="draft", idea=Idea(operator="draft"))
    spent, _ = eng._cue_node_budget(st, None, None)
    assert "endgame" not in spent, "past its end the episode is not where this proposal falls"


# ============================================================ the real dispatch, end to end
class _Stalled:
    """Two seeds — one near the optimum, one far — then every improve lands far from it: a search
    that has stopped paying. The counter is the paid-proposal ledger the tests read."""

    def __init__(self):
        self.drafts = 0
        self.improves: list[int] = []

    def propose(self, state, parent):
        if parent is None:
            self.drafts += 1
            x, y = (3.2, -1.0) if self.drafts == 1 else (-8.0, 8.0)
            return Idea(operator="draft", params={"x": x, "y": y}, rationale=f"seed {self.drafts}")
        self.improves.append(len(state.nodes))
        return Idea(operator="improve", params={"x": 9.0, "y": 9.0}, rationale=f"push on {parent.id}")


def _plans(events):
    return [e for e in events if e.type == EV_PLAN]


def _decisions(events):
    """Each `node_building` with the fold it was decided on (the log up to it)."""
    for index, event in enumerate(events):
        if event.type == "node_building":
            yield event, fold(events[:index])


@pytest.mark.parametrize("card_driven", [False, True], ids=["policy", "cards"])
def test_a_stalled_run_spends_k_nodes_on_its_endgame_then_reopens_once(tmp_path, card_driven):
    researcher = _Stalled()
    eng = make_engine(tmp_path / "run", n_seeds=2, max_nodes=16, endgame_reserve_frac=0.2,
                      endgame_stall_nodes=2, researcher=researcher,
                      card_driven_selection=card_driven)
    eng._endgame_sweep = False            # the Researcher refines: no surrogate crowns a new leader
    state = anyio.run(eng.run)
    events = eng.store.read_all()
    assert state.finished and state.best_node_id == 0
    plans = [e.data for e in _plans(events)]
    assert [p["reason"] for p in plans] == ["initial", "stagnation", "reopened"]
    episode, reopened = plans[1], plans[2]
    start, end = episode["endgame_start"], episode["endgame_end"]
    assert end - start == 2 and episode["champion"] == 0 and episode["stall_champions"] == [0]
    assert reopened["reopen_cause"] == "episode_spent" and reopened["at_node"] == end
    assert reopened["stall_champions"] == [0] and reopened["endgame_start"] == 13
    inside = [n for n in state.nodes.values() if start <= n.id < end]
    assert [n.operator for n in inside] == ["merge", "improve"]
    assert inside[0].parent_ids == [0, 1], "the champion and the one seed outside its lineage"
    assert all(n.parent_ids == [0] for n in state.nodes.values() if n.id >= end), (
        "breadth after the episode, and the normal reserve re-merges nothing: every partner is the "
        "champion's lineage or the pair already merged")
    merges = [frozenset(n.parent_ids) for n in state.nodes.values() if n.operator == "merge"]
    assert len(merges) == len(set(merges)), "no pair merged twice"
    for building, before in _decisions(events):
        if in_endgame(before.plan, len(before.nodes)) and building.data.get("card_id"):
            action = _card(before, building.data["card_id"])
            assert endgame_admits(before, before.plan, action, sweep=False), (building.seq, action)
    replayed = fold(events)
    assert replayed.plan == plans[-1] and replayed.plan_history == fold(events).plan_history


def test_a_card_run_builds_the_gates_own_sweep_and_pays_no_researcher_inside_it(tmp_path):
    """Card mode with the sweep on. The raw lane used to re-derive the POLICY's improve of the champion
    and stage it as a paid Researcher Card, which the gate then kept — so a Card run never swept. Now
    the raw lane asks `endgame_admits`, finds the policy's action is not the reserve's own, and the
    serial path builds the surrogate's sweep: no Researcher proposal inside any reserve."""
    researcher = _Stalled()
    eng = make_engine(tmp_path / "run", n_seeds=2, max_nodes=16, endgame_reserve_frac=0.2,
                      endgame_stall_nodes=2, researcher=researcher, card_driven_selection=True)
    state = anyio.run(eng.run)
    events = eng.store.read_all()
    plans = [e.data for e in _plans(events)]
    episode = next(p for p in plans if p["reason"] == "stagnation")
    start, end = episode["endgame_start"], episode["endgame_end"]
    sweep = state.nodes[start + 1]
    assert sweep.operator == "improve" and sweep.idea.rationale.startswith("surrogate-guided"), (
        sweep.idea.rationale)
    reserve_counts = [n for n in researcher.improves
                      if start <= n < end or n >= plans[-1]["endgame_start"]]
    assert reserve_counts == [], f"a Researcher improve was paid for inside a reserve at {reserve_counts}"
    for building, before in _decisions(events):
        if in_endgame(before.plan, len(before.nodes)) and building.data.get("card_id"):
            assert endgame_admits(before, before.plan, _card(before, building.data["card_id"]))
