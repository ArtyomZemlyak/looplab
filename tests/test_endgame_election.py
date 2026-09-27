"""The SPECULATIVE election asks the plan before it buys a build (MiniOneRec inf13, 2026-09-27).

`speculation.py::_request_card_build` never met the endgame gate: on inf13 it requested card-16 — the
crossover of node 12 with its own parent 5, a pair node 13 had just failed on — inside an endgame
whose gate refuses exactly that merge. Driven through the engine's own election on a depth-1
speculation engine (the receipt stub is `tests/test_card_speculation_engine.py`'s autouse fixture,
imported for that reason); the pure halves are `tests/test_endgame_admission.py`.
"""
from __future__ import annotations

import anyio

from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from looplab.engine.plan import build_plan, endgame_refused_card_ids, in_endgame
from looplab.events.replay import fold
from looplab.events.types import EV_PLAN
from tests.test_card_speculation_engine import (  # noqa: F401  (autouse receipt fixture)
    _admit_unit_speculation_receipt,
    _engine,
    _start,
)


# The reserve both tests below enter: the endgame from node 5 of an 8-node budget.
_RESERVE = build_plan(max_nodes=8, n_seeds=0, reserve_frac=0.5, at_node=5, reason="stagnation",
                      endgame_start=5)


def _board(run_dir, *, planned: bool):
    """Node 0 (the champion, direction min) with three worse children and one unrelated seed, so
    GreedyTree's merge of the top-two — the champion and its own child — is DUE, and the only Card for
    it is that lineage merge (`card-m`)."""
    engine, producer = _engine(run_dir, depth=1)
    _start(engine)
    store = engine.store
    for nid, op, parents, metric in ((0, "draft", [], 1.0), (1, "improve", [0], 2.0),
                                     (2, "improve", [0], 3.0), (3, "improve", [0], 4.0),
                                     (4, "draft", [], 9.0)):
        store.append("node_created", {"node_id": nid, "parent_ids": parents, "operator": op,
                                      "idea": {"operator": op, "params": {"x": float(nid)}}})
        store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": metric})
    st = fold(store.read_all())
    idea = Idea(operator="merge", params={"x": 0.5}, rationale="ensemble 0 and 1",
                hypothesis="the champion and its child ensemble well", card_id="card-m")
    action = Engine._card_action(idea, [0, 1], {"0": 0, "1": 0}, 0, 0, scored_against_empty=False)
    store.append("card_added", Engine._card_added_payload(
        "card-m", Engine._card_statement(idea), action, idea, source="engine",
        at_node=len(st.nodes)))
    if planned:
        store.append(EV_PLAN, _RESERVE)
    return engine, producer


def test_the_speculative_election_requests_no_build_the_gate_would_refuse(tmp_path):
    """Through the engine's own election (`_request_card_build`) on a depth-1 speculation engine: the
    champion's child and the champion are the metric top-two, GreedyTree's merge is due, and the only
    Card for it is the lineage merge. Outside the reserve it is requested; inside it nothing is."""
    free, _producer = _board(tmp_path / "free", planned=False)
    assert free._request_card_build() is True
    requested = [e.data["card_id"] for e in free.store.read_all() if e.type == "card_build_requested"]
    assert requested == ["card-m"], "precondition: the due lineage merge is elected outside a reserve"

    gated, _producer = _board(tmp_path / "gated", planned=True)
    st = fold(gated.store.read_all())
    assert in_endgame(st.plan, len(st.nodes)) and "card-m" in endgame_refused_card_ids(st, st.plan)
    assert gated._request_card_build() is False
    assert not [e for e in gated.store.read_all() if e.type == "card_build_requested"]


def test_the_serial_claim_revalidates_the_lane_its_election_chose(tmp_path):
    """`_claim_existing_card_builds` re-derives the selected lane under `_id_lock` and refuses a claim
    whose lane moved. Its question must be the election's: with the plan's refusals applied when the
    lane is elected but not when it is revalidated, a width-2 lane the refusal trimmed to one Card read
    as "selection moved" on every turn and the admitted Card was never built."""
    from looplab.search.card_selection import META_CARD_ID
    from tests.factories import make_engine

    eng = make_engine(tmp_path / "run", n_seeds=0, max_nodes=20, card_driven_selection=True)
    eng.policy.card_select_k = 2
    _start(eng)
    for nid, op, parents, metric in ((0, "draft", [], 1.0), (1, "improve", [0], 2.0),
                                     (2, "improve", [0], 3.0)):
        eng.store.append("node_created", {"node_id": nid, "parent_ids": parents, "operator": op,
                                          "idea": {"operator": op, "params": {"x": float(nid)}}})
        eng.store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": metric})
    for card_id, parent in (("card-a", 1), ("card-b", 0)):
        st = fold(eng.store.read_all())
        idea = Idea(operator="improve", params={"x": 0.1 * parent}, rationale=f"refine {parent}",
                    hypothesis=f"refining node {parent} ({card_id}) helps", card_id=card_id)
        action = Engine._card_action(idea, [parent], {str(parent): 0}, 0, 0,
                                     scored_against_empty=False)
        eng.store.append("card_added", Engine._card_added_payload(
            card_id, Engine._card_statement(idea), action, idea, source="researcher",
            at_node=len(st.nodes)))
    st = fold(eng.store.read_all())
    assert [a[META_CARD_ID] for a in eng._select_actions(st)] == ["card-b", "card-a"], (
        "precondition: outside a reserve the lane holds both Cards")

    eng.store.append(EV_PLAN, build_plan(max_nodes=20, n_seeds=0, reserve_frac=0.5, at_node=3,
                                         reason="stagnation", endgame_start=3))
    st = fold(eng.store.read_all())
    assert endgame_refused_card_ids(st, st.plan) == {"card-a"}, "an improve of a non-champion"
    creates = eng._plan_gate(st, eng._select_actions(st))
    assert [a[META_CARD_ID] for a in creates] == ["card-b"]
    reservations = eng._claim_existing_card_builds(creates)
    assert reservations is not None, eng._card_claim_refusal
    assert [r.card_id for r in reservations] == ["card-b"]


def test_a_request_the_plan_now_refuses_is_closed_before_its_build_is_bought(tmp_path):
    """The window the election cannot see: a request elected BEFORE the reserve began — queued behind
    a busy producer at width > 1, or found open by a restart — whose producer has not started. It is
    closed `stale` with its own slug and no producer runs; the Card stays on the board."""
    engine, producer = _board(tmp_path / "late", planned=False)
    assert engine._request_card_build() is True
    engine.store.append(EV_PLAN, _RESERVE)            # the reserve begins before a producer starts
    engine._ensure_speculation_state()
    state = fold(engine.store.read_all())
    assert engine._start_head_producer(state, None) is True
    done = [e.data for e in engine.store.read_all() if e.type == "card_build_done"]
    assert done == [{"card_id": "card-m", "generation": 0, "skipped": "stale",
                     "skipped_reason": "plan_refused"}]
    assert producer.calls == 0 and not engine._spec_build_inflight, "nothing was billed"
    after = fold(engine.store.read_all())
    assert after.cards["card-m"].status == "proposed" and not engine._outstanding_requests(after)


def test_a_committed_build_is_never_dropped_because_the_plan_now_refuses_its_card(tmp_path):
    """The plan refuses PURCHASES, not builds: a speculative node already built for a Card that an
    endgame now refuses is still fresh — the drain's counterfactual exempts the subject exactly as it
    exempts it from the in-flight exclusion — so it is evaluated rather than thrown away. Every OTHER
    refused Card stays out of the counterfactual, which is what keeps it the election's question."""
    from looplab.search.card_selection import SpeculativeSelectionContext, speculative_card_is_fresh
    from tests.test_card_speculation_engine import _commit_speculative_node, _seed_evaluated_node_zero

    engine, _producer = _engine(tmp_path / "run", depth=1)
    _start(engine)
    _seed_evaluated_node_zero(engine)
    engine.store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 5.0,
                                           "eval_seconds": 0.0})
    idea = Idea(operator="improve", params={"x": 0.2, "y": -1.0}, rationale="refine node 0",
                hypothesis="refining node 0 helps (card-x)", card_id="card-x")
    action = Engine._card_action(idea, [0], {"0": 0}, 0, 0, scored_against_empty=False)
    engine.store.append("card_added", Engine._card_added_payload(
        "card-x", Engine._card_statement(idea), action, idea, source="researcher", at_node=1))
    node_id = _commit_speculative_node(engine)
    # A better seed lands and an endgame begins: an improve of node 0 is no longer the reserve's work.
    engine.store.append("node_created", {"node_id": 2, "parent_ids": [], "operator": "draft",
                                         "idea": {"operator": "draft", "params": {"x": 2.0}}})
    engine.store.append("node_evaluated", {"node_id": 2, "generation": 0, "metric": 1.0})
    engine.store.append(EV_PLAN, build_plan(max_nodes=8, n_seeds=0, reserve_frac=0.5, at_node=3,
                                            reason="stagnation", endgame_start=3))
    state = fold(engine.store.read_all())
    assert state.best_node_id == 2 and "card-x" in endgame_refused_card_ids(state, state.plan)
    fresh = speculative_card_is_fresh(
        state, engine.policy, engine._speculative_selection_node_limit(state),
        card_id="card-x", node_id=node_id,
        context=SpeculativeSelectionContext(
            excluded_card_ids=engine._election_excluded_card_ids(state),
            ignored_pending_node_ids=engine._acknowledged_pending_ids(state),
            resource_envelope=engine._resource_envelope(),
            refused_card_ids=engine._plan_refused_card_ids(state)))
    assert fresh is True, "the committed subject is exempt from the plan's refusal"
    assert anyio.run(engine._drop_stale_speculation) is False
    assert fold(engine.store.read_all()).nodes[node_id].status.value == "pending"
