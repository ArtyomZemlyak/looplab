"""A re-attempt at a question already on the board joins its card; it does not mint a twin.

THE DEFECT, live in `runs/rubertlite-dr-unified-v5` and — identically — in `runs/rubert-dr-0807`
one run earlier: node 0 was built for card-0 and failed (`no_metric`), the policy answered with
`{"kind": "debug", "parent_id": 0}`, `orchestrator.py::_prepare_node_idea` filled that in with the
PARENT'S OWN IDEA verbatim (only `operator` flipped — the run has three `propose` spans, one per
draft, and none for this), and the mint saw a different action digest and wrote card-3 with a
statement BYTE-IDENTICAL to card-0's. Two rows, one research question, and the operator reading the
board saw the same experiment proposed twice.

`Card.belief_id` / `Card.retry_of` were the previous answer and they only NAMED it. These drive the
engine half: `engine/card_reservation.py::_retry_attach_card` and the `attach` disposition, plus the
proposal-context half (`agents/roles.py::attempted_board_prompt_cards`) that stopped the proposer
from seeing a card at all once it had a node.

Every test here drives a real `Engine` over a real event log and reads the FOLD, not the source: the
property is "how many `card_added` rows exist and which nodes hang off them", which no call-presence
pin can distinguish from a mint that happens to be spelled the same way.
"""
from __future__ import annotations

import pytest

from looplab.adapters.toytask import ToyObjectiveDeveloper
from looplab.agents.roles import _state_brief, attempted_board_prompt_cards, next_board_prompt_cards
from looplab.core.models import Idea, durable_idea_payload
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_ADDED, EV_CARD_AUTO_DROPPED, EV_NODE_FAILED

from tests.factories import make_engine

SEED = ("Reproducing the human-proven qwen3 cross-batch InfoNCE recipe on rubert-tiny-lite v2 "
        "reaches recall@100 = 0.95, far above the current mnr baseline (~0.81).")


class _RepairingDeveloper(ToyObjectiveDeveloper):
    """The toy Developer with a `repair`, which is what selects the mechanical debug branch.

    `_prepare_node_idea` takes the verbatim-copy path only when the Developer can repair; the plain
    toy one cannot, so without this the debug action would reach a fresh ToyResearcher proposal and
    the run would never reproduce the shape the operator reported.
    """

    def repair(self, idea, code, error):
        return self.implement(idea)


def _engine(tmp_path, **overrides):
    return make_engine(tmp_path / "run", card_driven_selection=True,
                       developer=_RepairingDeveloper(), **overrides)


def _seed_failed_card(engine, *, hypothesis: str = SEED):
    """Card-0 with one node that FAILED — the exact prefix v5 had when it minted the twin."""
    idea = Idea(operator="draft", params={"x": 1.0, "y": 2.0},
                rationale="switch the loss to qwen3 cross-batch InfoNCE", hypothesis=hypothesis)
    reservation = engine._reserve_node_build({"kind": "draft", "parent_ids": []}, idea)
    assert reservation is not None and reservation.card_id == "card-0"
    engine._emit_node_created(node_id=reservation.node_id, parent_ids=[], operator="draft",
                              idea=durable_idea_payload(reservation.idea),
                              code="print(1)", files={})
    engine.store.append(EV_NODE_FAILED, {
        "node_id": reservation.node_id, "generation": 0,
        "error": "stage 'train' exited 0 but produced no metric",
        "reason": "no_metric", "eval_seconds": 1.0})
    return fold(engine.store.read_all())


def _card_added_ids(engine):
    return [event.data.get("id") for event in engine.store.read_all()
            if event.type == EV_CARD_ADDED]


# --- the engine half: the mint ------------------------------------------------------------------

def test_a_repair_becomes_another_node_under_the_card_it_retries(tmp_path):
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    assert state.cards["card-0"].evidence == [0]

    retry = engine._prepare_node_idea({"kind": "debug", "parent_id": 0}, state,
                                      researcher=engine.researcher, prospective_node_id=1,
                                      source="researcher")
    assert retry is not None and retry.operator == "debug"
    assert retry.card_id == "card-0", "the proposal funnel must already resolve the attach target"

    reservation = engine._reserve_node_build({"kind": "debug", "parent_id": 0}, retry)
    assert reservation is not None and reservation.card_id == "card-0"
    engine._emit_node_created(node_id=reservation.node_id, parent_ids=[0], operator="debug",
                              idea=durable_idea_payload(reservation.idea),
                              code="print(2)", files={})

    final = fold(engine.store.read_all())
    # THE property: one durable receipt, one board row, two nodes under it.
    assert _card_added_ids(engine) == ["card-0"]
    assert list(final.cards) == ["card-0"]
    assert final.cards["card-0"].evidence == [0, 1]
    assert final.nodes[1].idea.card_id == "card-0"
    assert final.nodes[1].idea.operator == "debug", "the NODE still records the repair action"


def test_the_inventory_staging_lane_writes_no_twin(tmp_path):
    """`_stage_card_creates` is the lane that actually wrote v5's card-3.

    Its row had no `node_building` beside it and was followed by `card_build_requested`, which is the
    staged-inventory shape and not `_reserve_node_build`'s crash-atomic pair. So fixing only the
    reservation would have left the live defect exactly where it was.
    """
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)

    assert engine._stage_card_creates([{"kind": "debug", "parent_id": 0}], state) == []
    assert _card_added_ids(engine) == ["card-0"]
    assert list(fold(engine.store.read_all()).cards) == ["card-0"]


def test_without_the_attach_the_same_lane_still_mints_the_twin(tmp_path, monkeypatch):
    """The counterfactual, so the two tests above cannot pass for some unrelated reason.

    Neutralize ONLY the attach resolver — every other guard (`hypothesis_merged`, the novelty gate,
    the belief collapse, the exact-action dedupe) stays live — and the v5 board comes straight back:
    two cards, byte-identical seeds, one `belief_id`.
    """
    from looplab.engine.card_reservation import CardReservationMixin

    monkeypatch.setattr(CardReservationMixin, "_retry_attach_card",
                        classmethod(lambda cls, *args, **kwargs: None))
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)

    assert engine._stage_card_creates([{"kind": "debug", "parent_id": 0}], state) == ["card-1"]
    twins = fold(engine.store.read_all()).cards
    assert twins["card-0"].seed_statement == twins["card-1"].seed_statement
    assert twins["card-0"].belief_id == twins["card-1"].belief_id
    assert twins["card-1"].retry_of == "card-0"


# --- the rules that keep the attach narrow -------------------------------------------------------

def _plan(engine, state, idea, *, parents, retry_attach=True, **kwargs):
    return engine._plan_native_card(
        engine.store.read_all(), state, idea, parents=parents,
        parent_generations={str(pid): state.nodes[pid].attempt for pid in parents},
        scored_against=state.best_node_id, source="researcher", at_node=1,
        retry_attach=retry_attach, **kwargs)


def _retry_idea(state, *, operator="debug", hypothesis=None):
    idea = state.nodes[0].idea.model_copy(deep=True)
    idea.operator = operator
    idea.card_id = None
    if hypothesis is not None:
        idea.hypothesis = hypothesis
    return idea


def test_a_retry_that_re_scopes_its_question_still_mints_its_own_card(tmp_path):
    """The belief check is what stops this from collapsing a genuine change of question.

    `_retry_attach_card` reads `Card.belief_id` — the digest the fold publishes — so a repair whose
    statement was rewritten is a DIFFERENT question and gets its own work item, exactly as before.
    """
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    plan = _plan(engine, state,
                 _retry_idea(state, hypothesis="Hard-negative mining raises recall@100 instead."),
                 parents=[0])
    assert plan.disposition == "mint" and plan.card_id == "card-1"


@pytest.mark.parametrize("operator", ["improve", "merge", "draft"])
def test_only_a_repair_attaches_never_a_child_that_proposes_a_new_point(tmp_path, operator):
    """`improve`/`merge` also name parent nodes. Attaching those would claim every child re-runs its
    parent's question, which is the opposite of what a search does."""
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    plan = _plan(engine, state, _retry_idea(state, operator=operator), parents=[0])
    assert plan.disposition == "mint"


def test_a_dropped_card_is_never_re_opened_by_an_attach(tmp_path):
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    engine.store.append(EV_CARD_AUTO_DROPPED, {
        "id": "card-0", "reason": "operator abandoned the direction", "dropped_by": "engine"})
    dropped = fold(engine.store.read_all())
    assert dropped.cards["card-0"].status == "dropped"
    plan = _plan(engine, dropped, _retry_idea(dropped), parents=[0])
    assert plan.disposition == "mint"


def test_the_attach_is_opt_in_per_call_site(tmp_path):
    """An `attach` plan mints nothing, so a site that appends `card_added` for `mint` and nothing
    otherwise would reserve a node under a card it never wrote. The re-proposal reset path also
    `_drop_card_once`s what it supersedes, and an attach would hand it the PARENT's card to drop —
    taking the parent node's own evidence row with it. So the default must stay off."""
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    idea = _retry_idea(state)
    assert _plan(engine, state, idea, parents=[0], retry_attach=False).disposition == "mint"
    assert _plan(engine, state, idea, parents=[0], retry_attach=True).disposition == "attach"


def test_the_card_a_re_proposal_supersedes_is_not_an_attach_target(tmp_path):
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    plan = _plan(engine, state, _retry_idea(state), parents=[0], superseded_card_id="card-0")
    assert plan.disposition == "mint"


# --- the proposal-context half -------------------------------------------------------------------

def test_the_proposer_can_see_a_question_whose_experiment_is_still_running(tmp_path):
    """v5's node-2 proposal is the measurement: two cards were live, both had a node in flight, and
    the rendered user turn carried no board section at all — `open_research_beliefs()` is untested
    cards only, and `experiments_digest` lists winners and failures, of which a PENDING node is
    neither."""
    engine = _engine(tmp_path)
    idea = Idea(operator="draft", params={"x": 1.0, "y": 2.0}, rationale="r", hypothesis=SEED)
    reservation = engine._reserve_node_build({"kind": "draft", "parent_ids": []}, idea)
    engine._emit_node_created(node_id=reservation.node_id, parent_ids=[], operator="draft",
                              idea=durable_idea_payload(reservation.idea), code="c", files={})
    state = fold(engine.store.read_all())

    assert next_board_prompt_cards(state) == [], "the untested window is empty — that was the bug"
    assert [c.id for c in attempted_board_prompt_cards(state)] == ["card-0"]
    brief = _state_brief(state, None)
    assert "CARD_ID=card-0" in brief and SEED in brief


def test_a_failed_cards_question_stays_visible_to_the_proposer(tmp_path):
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    assert next_board_prompt_cards(state) == []
    brief = _state_brief(state, None)
    assert "ALREADY on the board" in brief
    assert "CARD_ID=card-0" in brief and "NODES=[0]" in brief


def test_the_two_board_blocks_never_show_the_same_card_twice(tmp_path):
    """An untested card is the CLAIMABLE queue ('return its CARD_ID'); an attempted one is context
    whose work item is already owned. Rendering a card in both would offer a claim the reservation
    fence must then refuse."""
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    engine._record_node_less_card(
        Idea(operator="draft", params={"x": 2.0, "y": 1.0}, rationale="r",
             hypothesis="An unrelated open question with no node."),
        reason="proposal_rejected")
    board = fold(engine.store.read_all())
    untested = next_board_prompt_cards(board)
    attempted = attempted_board_prompt_cards(board, untested)
    assert not ({c.id for c in untested} & {c.id for c in attempted})
