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

THE SECOND HALF of this file is the adversarial review of the first (2026-08-12), and every one of
those tests fails on the tree that shipped the attach. They are grouped by what they hold:

  * OWNERSHIP — an `attach` claim names the PARENT's card, and every bare-reservation close path
    dropped what its marker named. One SIGKILL between `node_building` and `node_created` therefore
    deleted card-0 from the board, which is strictly worse than the twin it replaced.
  * AUTHORITY — `retry_attach` is per CALL SITE, and `_reserve_node_build` has five callers.
  * THE CONTRACT — the `attach` plan skipped `_card_added_payload`, i.e. every proposal-shape
    refusal that `mint`/`reuse` get for free.
  * THE PROMPT — the block the same change added invited exactly the duplicate it removes.
  * THE STAGING REFUSAL — which was unobservable, and therefore deletable with all tests green.
"""
from __future__ import annotations

import pytest

from looplab.adapters.toytask import ToyObjectiveDeveloper
from looplab.agents.roles import (_state_brief, attempted_board_prompt_cards,
                                  bind_idea_to_board_card, next_board_prompt_cards)
from looplab.core.models import Idea, durable_idea_payload
from looplab.events.replay import fold
from looplab.events.types import (EV_CARD_ADDED, EV_CARD_AUTO_DROPPED, EV_NODE_BUILDING,
                                  EV_NODE_FAILED)

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

    reservation = engine._reserve_node_build({"kind": "debug", "parent_id": 0}, retry,
                                             retry_attach=True)
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


# =================================================================================================
# OWNERSHIP - an attach reservation joins somebody else's card and may never close it
# =================================================================================================

def _attached_reservation(engine, state):
    """The exact shape `_create_node_scoped` commits: `_link` resolves the attach, the reservation
    agrees, and the log holds a bare `node_building` naming a card THIS reservation did not mint."""
    retry = engine._prepare_node_idea({"kind": "debug", "parent_id": 0}, state,
                                      researcher=engine.researcher, prospective_node_id=1,
                                      source="researcher")
    reservation = engine._reserve_node_build({"kind": "debug", "parent_id": 0}, retry,
                                             retry_attach=True)
    assert reservation is not None and reservation.card_id == "card-0"
    # The RECORD, written where the attach is committed: this claim did not mint card-0. It is what
    # `_reservation_minted_card` reads, and it is the only proof that survives a log whose other
    # rows say nothing (a card joined to its evidence by the legacy statement hash, a future writer).
    claim = [event.data for event in engine.store.read_all()
             if event.type == EV_NODE_BUILDING and event.data.get("node_id") == reservation.node_id]
    assert claim and claim[-1].get("card_attached") is True
    return reservation


def _drop_receipts(engine):
    return [event.data.get("id") for event in engine.store.read_all()
            if event.type == EV_CARD_AUTO_DROPPED]


def test_an_interrupted_attach_never_drops_the_card_it_joined(tmp_path):
    """THE WORST DEFECT the attach shipped, and it is strictly worse than the twin it replaced.

    An `attach` writes `node_building{node_id: 1, card_id: "card-0"}` where card-0 is the PARENT's
    work item. `_recover_interrupted_builds` then sees a marker whose node does not exist - because
    the process died before `node_created` - reads that as "a propose-reset owning a newly minted
    Card", and hands the marker's id to `_fail_reserved_build` with `drop_card=True`. Driven on a
    copy of a real run before the fix: card-0 went `building`/`evidence=[0]` ->
    `dropped`/`build_interrupted`/`actionable=False` after one SIGKILL and one resume, taking the
    parent node's own evidence row off the board - and the retry then minted the twin anyway.
    """
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    _attached_reservation(engine, state)

    # the SIGKILL: the marker is live, `node_created` never landed. Re-entering `run` under the run
    # lock is what proves no worker still owns it, and that is exactly this call.
    assert engine._recover_interrupted_builds(fold(engine.store.read_all())) is True

    final = fold(engine.store.read_all())
    assert _drop_receipts(engine) == [], "the parent's card was dropped by somebody else's close"
    assert final.cards["card-0"].status != "dropped"
    assert final.cards["card-0"].dropped_reason is None
    assert final.cards["card-0"].evidence == [0], "the parent node's evidence row survived"
    # and the reservation still gets its own terminal: the fence bounds the DROP, not the close.
    assert any(event.type == EV_NODE_FAILED and event.data.get("node_id") == 1
               and event.data.get("reason") == "build_interrupted"
               for event in engine.store.read_all())


def test_an_interrupted_mint_still_closes_its_own_card(tmp_path):
    """The counterfactual, so the test above cannot pass by disabling the drop everywhere.

    A bare first build DID mint the card its marker names, nobody else owns it, and leaving it live
    is the original defect `_fail_reserved_build` exists to fix - it would resurrect as a fresh
    proposed work item on the next replay.
    """
    engine = _engine(tmp_path)
    idea = Idea(operator="draft", params={"x": 3.0, "y": 4.0}, rationale="r",
                hypothesis="A brand new question nobody has a node for yet.")
    reservation = engine._reserve_node_build({"kind": "draft", "parent_ids": []}, idea)
    assert reservation.card_id == "card-0"

    assert engine._recover_interrupted_builds(fold(engine.store.read_all())) is True
    assert _drop_receipts(engine) == ["card-0"]
    assert fold(engine.store.read_all()).cards["card-0"].status == "dropped"


def test_only_a_minting_reservation_may_drop_its_card(tmp_path):
    """The rule stated, because on today's log shapes its two proofs OVERLAP and the behavioural
    tests above cannot separate them: every reachable attach target is also named by its parent
    node's own `node_created`, so removing the `card_attached` marker leaves them green. The marker
    is the EXACT record ("this reservation did not mint this card") and the scan is the derived
    backstop; a truth table is what keeps the first one falsifiable.

    Absence of the marker is load-bearing in the other direction: it is what every claim written
    before 2026-08-12 looks like, and those all minted.
    """
    engine = _engine(tmp_path)
    minted = engine._reserve_node_build(
        {"kind": "draft", "parent_ids": []},
        Idea(operator="draft", params={"x": 1.0, "y": 2.0}, rationale="r", hypothesis=SEED))
    events = engine.store.read_all()
    assert engine._reservation_minted_card(events, minted.node_id, "card-0") is True

    engine.store.append(EV_NODE_BUILDING, {
        "node_id": 7, "operator": "debug", "parent_ids": [0],
        "card_id": "card-0", "card_attached": True})
    attached = engine.store.read_all()
    assert engine._reservation_minted_card(attached, 7, "card-0") is False
    # …and the marker binds the NODE that wrote it, not the card globally.
    assert engine._reservation_minted_card(attached, 0, "card-0") is True
    # An unnameable id fails closed: nothing is dropped.
    assert engine._reservation_minted_card(attached, 7, "") is False
    assert engine._reservation_minted_card(attached, 7, None) is False


def test_a_card_another_nodes_idea_names_is_never_dropped_by_a_close(tmp_path):
    """The half that covers logs written BEFORE the claim marker existed.

    `card_attached` is exact but only for claims written from 2026-08-12 on. A resumed run whose
    attach claim predates it - and any future path that hands a reservation a card somebody else
    already owns - is caught by the second proof instead: a card another NODE's durable idea names
    is that node's evidence row.
    """
    engine = _engine(tmp_path)
    _seed_failed_card(engine)
    # A pre-upgrade attach claim: same bytes, minus the marker the writer did not have.
    engine.store.append(EV_NODE_BUILDING, {
        "node_id": 1, "operator": "debug", "parent_ids": [0], "card_id": "card-0"})

    assert engine._recover_interrupted_builds(fold(engine.store.read_all())) is True
    assert _drop_receipts(engine) == []
    assert fold(engine.store.read_all()).cards["card-0"].evidence == [0]


# =================================================================================================
# AUTHORITY - retry_attach is per call site, and `_reserve_node_build` has five callers
# =================================================================================================

def test_an_operator_injection_keeps_its_own_card_and_its_executable_identity(tmp_path):
    """`retry_attach` was hardcoded inside `_reserve_node_build`, so "opt-in per call site" was false
    at four of its five callers. The one that cost something is the operator's.

    An `inject_node` with ready-made code carries TWO receipts an attach silently discards, because
    an attach mints no `card_added` at all: `source="operator"` (the board would credit the
    Researcher's card for a human's experiment) and `implementation_ref`, whose stated purpose is
    that folding two such requests "would lose executable work".
    """
    engine = _engine(tmp_path)
    _seed_failed_card(engine)
    engine._create_injected_node({
        "idea": {"operator": "debug", "params": {"x": 1.0, "y": 2.0},
                 "rationale": "switch the loss to qwen3 cross-batch InfoNCE",
                 "hypothesis": SEED},
        "parent_id": 0,
        "code": "print('operator wrote this')",
    })

    rows = {event.data.get("id"): event.data for event in engine.store.read_all()
            if event.type == EV_CARD_ADDED}
    assert list(rows) == ["card-0", "card-1"], "the operator's experiment got its own work item"
    assert rows["card-1"]["source"] == "operator"
    assert rows["card-1"]["implementation_ref"].startswith("implementation:v1:")
    assert fold(engine.store.read_all()).nodes[1].idea.card_id == "card-1"


def test_a_repair_never_attaches_to_a_parent_that_has_not_failed(tmp_path):
    """The FAILED half of "failed leaf", which the first version did not check at all: a `debug`
    planned against a node still PENDING attached to its live card and made the board report two
    simultaneous attempts at one question."""
    engine = _engine(tmp_path)
    idea = Idea(operator="draft", params={"x": 1.0, "y": 2.0}, rationale="r", hypothesis=SEED)
    reservation = engine._reserve_node_build({"kind": "draft", "parent_ids": []}, idea)
    engine._emit_node_created(node_id=reservation.node_id, parent_ids=[], operator="draft",
                              idea=durable_idea_payload(reservation.idea), code="c", files={})
    pending = fold(engine.store.read_all())
    assert pending.nodes[0].status.value == "pending"

    plan = _plan(engine, pending, _retry_idea(pending), parents=[0])
    assert plan.disposition == "mint"


def test_a_repair_never_attaches_while_the_card_still_has_work_in_flight(tmp_path):
    """and the LEAF half. Node 0 failed, but a second node under the same card is still running, so
    the question is already being re-attempted and does not need a third simultaneous owner."""
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    first_retry = _attached_reservation(engine, state)
    engine._emit_node_created(node_id=first_retry.node_id, parent_ids=[0], operator="debug",
                              idea=durable_idea_payload(first_retry.idea), code="c", files={})
    inflight = fold(engine.store.read_all())
    assert inflight.cards["card-0"].evidence == [0, 1]

    plan = _plan(engine, inflight, _retry_idea(inflight), parents=[0])
    assert plan.disposition == "mint"


# =================================================================================================
# THE CONTRACT - an attach is still a proposal and still has to be a legal one
# =================================================================================================

def test_an_attach_is_refused_when_its_implementation_identity_is_malformed(tmp_path):
    """`_card_added_payload` is not only a payload builder: it is where a proposal is proved to be a
    fixed point of its own validators and where a malformed `implementation_ref`, an out-of-range
    `at_node` and a non-printable `source` are refused, each becoming `invalid` plus a
    `novelty_rejected` receipt. The `attach` branch returned ABOVE all of it, so a node was reserved
    and built from a proposal replay would rebuild differently."""
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    plan = _plan(engine, state, _retry_idea(state), parents=[0],
                 implementation_ref="implementation:v1:not-a-digest")
    assert plan.disposition == "invalid"


def test_an_attach_is_refused_when_its_source_is_not_a_printable_label(tmp_path):
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    plan = engine._plan_native_card(
        engine.store.read_all(), state, _retry_idea(state), parents=[0],
        parent_generations={"0": 0}, scored_against=state.best_node_id,
        source="researcher\t", at_node=1, retry_attach=True)
    assert plan.disposition == "invalid"


def test_a_refused_attach_is_receipted_like_any_other_contract_refusal(tmp_path):
    """`invalid` is not merely a return value: `_reserve_node_build` turns it into a durable
    `novelty_rejected` the operator can read. An attach that skipped the contract produced neither."""
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    retry = engine._prepare_node_idea({"kind": "debug", "parent_id": 0}, state,
                                      researcher=engine.researcher, prospective_node_id=1,
                                      source="researcher")
    assert engine._reserve_node_build(
        {"kind": "debug", "parent_id": 0}, retry, retry_attach=True,
        implementation_ref="implementation:v1:short") is None
    assert any(event.type == "novelty_rejected"
               and event.data.get("kind") == "card_contract"
               for event in engine.store.read_all())


# =================================================================================================
# THE PROMPT - the block must not invite the duplicate the engine half removes
# =================================================================================================

def test_the_attempted_block_makes_no_promise_the_engine_cannot_keep(tmp_path):
    """The block shipped saying: name the CARD_ID in `rationale` "and the engine decides whether that
    becomes a repair under the same card". Nothing decides that - `bind_idea_to_board_card` is handed
    only `next_board_prompt_cards`, so the claim is NULLED, and no code reads `rationale` for an id.
    """
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    brief = _state_brief(state, None)
    assert "ALREADY on the board" in brief
    assert "name the CARD_ID" not in brief
    assert "NOT claimable" in brief
    # and the reason it is not claimable, driven rather than asserted about the wording.
    attempted = attempted_board_prompt_cards(state)
    claimed = Idea(operator="draft", params={"x": 1.0, "y": 2.0}, rationale="r",
                   hypothesis=SEED, card_id=attempted[0].id)
    assert bind_idea_to_board_card(claimed, next_board_prompt_cards(state)).card_id is None


def test_a_dropped_question_is_not_advertised_as_already_on_the_board(tmp_path):
    """"do NOT propose one of these again as if it were new" over a card the operator DROPPED reads a
    deliberate abandonment as a claim on the direction, and fences off the one question most likely
    to want re-scoping."""
    engine = _engine(tmp_path)
    _seed_failed_card(engine)
    engine.store.append(EV_CARD_AUTO_DROPPED, {
        "id": "card-0", "reason": "operator abandoned the direction", "dropped_by": "engine"})
    dropped = fold(engine.store.read_all())
    assert dropped.cards["card-0"].status == "dropped"
    assert attempted_board_prompt_cards(dropped) == []
    assert "ALREADY on the board" not in _state_brief(dropped, None)


def test_the_triage_and_action_briefs_carry_no_claim_contract(tmp_path):
    """`_state_brief` also feeds the crash-triage judge (`crash_repair.py`) and the macro-action
    chooser (`node_build.py`). Their replies are a verdict string and an INDEX - neither has a
    `card_id` field - so a "return its CARD_ID" instruction is one they cannot follow, competing with
    the one they can. The board's CONTENT stays; only the contracts go."""
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    non_proposal = _state_brief(state, None, for_proposal=False)
    assert "CARD_ID=card-0" in non_proposal, "the board is still context worth reading"
    assert "return its CARD_ID" not in non_proposal
    assert "NOT claimable" not in non_proposal
    assert "NOT claimable" in _state_brief(state, None)


# =================================================================================================
# THE STAGING REFUSAL - observable, or it is deletable with every test green
# =================================================================================================

def test_the_staging_lane_names_the_card_it_refused_to_duplicate(tmp_path):
    """Deleting the whole `if plan.disposition == "attach": return None` refusal and leaving it as a
    comment kept all 17 tests in this file green, because an `attach` falls through the `reuse`/
    `mint` tests to the same `None` four lines below. A refusal nobody can observe is also a refusal
    nobody can bound - the branch's own comment cited `_note_card_claim_refusal`, which is charged
    ONLY when `_claim_existing_card_builds` returns None and never sees this lane at all.

    `_card_claim_refusal` is what `_create_stall_diagnosis` reads, so a run that stalls here now says
    which card owns the question instead of "N action(s) planned ... without creating a node"."""
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)

    assert engine._stage_card_creates([{"kind": "debug", "parent_id": 0}], state) == []
    assert engine._card_stage_attached_to == "card-0"
    assert "card-0" in (engine._card_claim_refusal or "")
    assert "card-0" in engine._create_stall_diagnosis([{"kind": "debug", "parent_id": 0}], state)


def test_the_raw_speculative_lane_keeps_the_receipts_of_the_proposal_it_could_not_stage(tmp_path):
    """The isolated producer's whole turn ends in `_stage_prepared_card`, and for a repair that call
    can only ever refuse. Every other refusal there is a MOVED FENCE — the proposal is abandoned and
    re-made, so dropping its buffered audit intents keeps the log honest. This one is not: the
    proposal is being handed to the serial spine, which is where the attach is actually committed,
    and the buffered receipts are the only record that this lane paid for it at all."""
    from looplab.engine.speculation import SpecRawStageResult

    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    retry = engine._prepare_node_idea({"kind": "debug", "parent_id": 0}, state,
                                      researcher=engine.researcher, prospective_node_id=1,
                                      source="researcher")
    engine._ensure_speculation_state()
    engine._spec_raw_stage_result = SpecRawStageResult(
        generation=state.search_epoch,
        action={"kind": "debug", "parent_id": 0},
        proposal_state=state,
        proposal_authority_seq=engine._proposal_authority_seq(engine.store.read_all()),
        proposal_node_ceiling=1,
        at_node=1,
        source="researcher",
        cue_fence=engine._proposal_cue_fence(state),
        success=True,
        idea=retry,
        audit_events=(("novelty_rejected", {
            "node_id": 1, "generation": 0, "kind": "vs_history",
            "reason": "the producer's own audit intent", "action": "kept"}, None, None),),
    )

    assert engine._serve_raw_card_stage() == (True, False), "an attach is never staged inventory"
    assert engine._card_stage_attached_to == "card-0"
    assert [event.data.get("reason") for event in engine.store.read_all()
            if event.type == "novelty_rejected"] == ["the producer's own audit intent"]
    assert _card_added_ids(engine) == ["card-0"], "and still no twin"


def test_a_staging_refusal_that_is_merely_stale_names_no_card(tmp_path):
    """The counterfactual: an ordinary moved-fence refusal is transient and must NOT be reported as
    a permanent one, or the raw speculative lane would stop prefetching every time a tail moved."""
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    idea = Idea(operator="draft", params={"x": 5.0, "y": 6.0}, rationale="r",
                hypothesis="An unrelated new question.")
    assert engine._stage_prepared_card(
        {"kind": "draft", "parent_ids": []}, idea, proposal_state=state,
        proposal_node_ceiling=1, at_node=1, source="researcher",
        proposal_authority_seq=0) is None
    assert engine._card_stage_attached_to is None


# =================================================================================================
# the smaller confirmed ones
# =================================================================================================

def test_a_pre_upgrade_orphan_card_is_still_reusable_after_the_concept_upgrade(tmp_path):
    """`_card_event_matches` compares the WHOLE writer payload, and `_authored_card_concepts` added
    `idea.concepts` to it on 2026-08-12. So a `card_added` written by the pre-upgrade writer could
    never be reused again: a run killed between `card_added` and its claim came back, declined its
    own crash-prefix row, minted a SECOND card and left the orphan selectable - the duplicate
    work-item shape the ledger exists to prevent, reintroduced by the fix for a different one.

    Admitting it is identity-preserving, not merely convenient: `CARD_ACTION_DIGEST_V2_FIELDS`
    excludes `concepts`, so the `ownership_receipt` is byte-identical either way.
    """
    engine = _engine(tmp_path)
    idea = Idea(operator="draft", params={"x": 1.0, "y": 2.0}, rationale="r", hypothesis=SEED,
                concept_mode="full", concepts=["objective/retrieval", "space/loss"])
    state = fold(engine.store.read_all())
    plan = engine._plan_native_card(
        engine.store.read_all(), state, idea, parents=[], parent_generations={},
        scored_against=None, source="researcher", at_node=0)
    assert plan.disposition == "mint" and "concepts" in plan.payload["idea"]

    # the orphan as the OLD writer wrote it: same bytes, no `concepts` key.
    legacy = dict(plan.payload)
    legacy["idea"] = {k: v for k, v in plan.payload["idea"].items() if k != "concepts"}
    engine.store.append(EV_CARD_ADDED, legacy)

    reused = engine._plan_native_card(
        engine.store.read_all(), fold(engine.store.read_all()), idea, parents=[],
        parent_generations={}, scored_against=None, source="researcher", at_node=0)
    assert reused.disposition == "reuse" and reused.card_id == "card-0"


def test_a_concept_membership_that_disagrees_still_declines_reuse(tmp_path):
    """Narrow on purpose: only an ABSENT key is a pre-upgrade row. A DIFFERENT membership is another
    author's claim and must keep declining, or the tolerance becomes a loose semantic comparison."""
    engine = _engine(tmp_path)
    idea = Idea(operator="draft", params={"x": 1.0, "y": 2.0}, rationale="r", hypothesis=SEED,
                concept_mode="full", concepts=["objective/retrieval"])
    state = fold(engine.store.read_all())
    plan = engine._plan_native_card(
        engine.store.read_all(), state, idea, parents=[], parent_generations={},
        scored_against=None, source="researcher", at_node=0)
    other = dict(plan.payload)
    other["idea"] = {**plan.payload["idea"], "concepts": ["objective/something-else"]}
    engine.store.append(EV_CARD_ADDED, other)

    fresh = engine._plan_native_card(
        engine.store.read_all(), fold(engine.store.read_all()), idea, parents=[],
        parent_generations={}, scored_against=None, source="researcher", at_node=0)
    assert fresh.disposition == "mint" and fresh.card_id == "card-1"


def test_a_delta_proposal_carries_no_card_concepts_and_that_is_the_whole_claim(tmp_path):
    """`2acdb825` said "the Card lane no longer loses concepts"; what holds is "no longer loses a
    FULL membership". This pins the narrowed claim as a fact about the code rather than a note about
    it - see `_authored_card_concepts` for what extending it would actually cost."""
    engine = _engine(tmp_path)
    idea = Idea(operator="draft", params={"x": 1.0, "y": 2.0}, rationale="r", hypothesis=SEED,
                concept_mode="delta", concepts_added=["objective/retrieval"], concepts_removed=[])
    plan = engine._plan_native_card(
        engine.store.read_all(), fold(engine.store.read_all()), idea, parents=[],
        parent_generations={}, scored_against=None, source="researcher", at_node=0)
    assert plan.disposition == "mint"
    assert "concepts" not in plan.payload["idea"]


def test_an_attach_that_lapses_between_the_two_planning_passes_still_builds_its_node(tmp_path):
    """THE RACE between `_prepare_node_idea._link` (outside `_id_lock`) and `_reserve_node_build`
    (inside it). A `card_dropped` landing between them makes the first attach and the second mint,
    and the `idea.card_id != plan.card_id` fence then returned None: no reservation, no node, no
    terminal, no event saying why - a turn charged to the runaway counter that nobody could explain.
    The committing pass holds the lock and the tail CAS, so it is the authority."""
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    retry = engine._prepare_node_idea({"kind": "debug", "parent_id": 0}, state,
                                      researcher=engine.researcher, prospective_node_id=1,
                                      source="researcher")
    assert retry.card_id == "card-0"
    # and now the world moves, exactly as an operator drop would move it.
    engine.store.append(EV_CARD_AUTO_DROPPED, {
        "id": "card-0", "reason": "operator abandoned the direction", "dropped_by": "engine"})

    reservation = engine._reserve_node_build({"kind": "debug", "parent_id": 0}, retry,
                                             retry_attach=True)
    assert reservation is not None, "the turn was lost in silence before the fence learned this"
    assert reservation.card_id == "card-1" and reservation.idea.card_id == "card-1"


def test_a_sidecar_bound_card_id_is_still_never_rebound(tmp_path):
    """The fence's real job survives the exception above. A proposal-bound sidecar names a card that
    does not exist YET and is about to be minted under exactly that spelling; rebinding THAT would
    make the audit row and the node disagree about which work item was proposed."""
    engine = _engine(tmp_path)
    state = _seed_failed_card(engine)
    idea = _retry_idea(state, hypothesis="A different question with its own work item.")
    idea.card_id = "card-9"          # not a live Card - a planner sidecar's id
    assert engine._reserve_node_build({"kind": "debug", "parent_id": 0}, idea,
                                      retry_attach=True) is None
