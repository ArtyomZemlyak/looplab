"""The speculative claim asks the policy the question its election asked — on the LAST slot too.

MEASURED 2026-09-26 (toy driver, `max_nodes=12, eval_parallel=2, llm_parallel=4,
speculation_depth=2`, two concurrent series): 22 of 50 attempts hot-looped at ~97 % CPU until
killed, appending ~1,000-1,450 `card_build_requested` / `card_build_done{stale, not_selected_now}`
pairs a minute for ONE card, with every node terminal and no `run_finished`. All 21 looping logs
kept began at the run's LAST physical node slot, and every looping Card was the engine's endgame
merge. At `llm_parallel` 0 or 1 (one producer) the same disagreement cannot spin inside a session:
20 of 40 attempts instead bought the same build 84-85 times across outer turns and ended `stuck` on
the `no_mint_turns` bound, skipping confirmation and the noise floor. With the fix: 0 of 90 either
way.

`_refresh_speculation_budget` translates the hard node ceiling into `policy.max_nodes` and charges
every UNMATERIALIZED request against it. The election (`_request_card_build`) runs before its own
request exists; the claim (`_claim_requested_card_build`) runs after, so the request it is
converting was charged against the very denominator the claim's selection hands the policy. On the
last slot that is `policy.max_nodes == card_budget_used(state)`, and `GreedyTree.next_actions`
answers `[]` ("budget spent -> finish"): the protected due merge disappears, every Card drops to
band 0, a different Card wins, and the claim closes the build `stale:not_selected_now`.
`_speculative_selection_node_limit` already added the request back to the SELECTOR's limit; nothing
added it back to the policy object's own `max_nodes`, which is what `next_actions` reads.

At width > 1 the intact result is kept for a re-election (`_spec_reusable`), the election — which
still sees the free slot — elects the same Card, the reuse needs no producer, and the session's
turn loop never has a reason to hand back: every turn `progressed`, and `_card_phase_decide_exit`
sees an outstanding request each time it is asked. The run loop's `no_mint_turns` bound counts
OUTER turns, and this spin never leaves the one `_run_card_session` call it started in.
"""
from __future__ import annotations

import anyio

from looplab.core.models import Idea
from looplab.engine import speculation as speculation_module
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.events.eventstore import EventStoreConcurrencyError
from looplab.events.types import (
    EV_CARD_BUILD_DONE,
    EV_CARD_BUILD_REQUESTED,
    EV_NODE_BUILDING,
    EV_NODE_EVALUATED,
)
from looplab.search.card_selection import card_budget_used
from tests.test_card_speculation_engine import (  # noqa: F401  (autouse receipt fixture)
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _build_result,
    _Developer,
    _engine,
    _Researcher,
    _request,
    _seed_evaluated_node_zero,
    _start,
    _without_research,
)


def _add_ready_improve(engine: Engine, card_id: str, *, parent: int, x: float) -> None:
    """A ready `improve` Card scored against — and parented on — the live champion `parent`."""
    idea = Idea(
        operator="improve",
        params={"x": x, "y": -1.0},
        rationale=f"refine the champion ({card_id})",
        hypothesis=f"refining node {parent} ({card_id}) improves the objective",
        card_id=card_id,
    )
    action = Engine._card_action(
        idea, [parent], {str(parent): 0}, parent, 0, scored_against_empty=False,
    )
    statement = Engine._card_statement(idea)
    assert statement is not None
    engine.store.append("card_added", Engine._card_added_payload(
        card_id, statement, action, idea, source="researcher", at_node=1,
    ))


def _last_slot_board(engine: Engine) -> None:
    """Node 0 evaluated, ONE physical slot left, and two ready Cards the policy tells apart.

    `GreedyTree` proposes `improve 0`, so `card-b` is the exact-match band while one slot is free.
    With the policy told the budget is spent it proposes nothing, both Cards fall to band 0, their
    keys tie, and the stable id tie-break hands the slot to `card-a` — the whole flip, with no
    merge cadence needed to show it.
    """
    _start(engine)
    _seed_evaluated_node_zero(engine)
    engine.store.append(EV_NODE_EVALUATED, {
        "node_id": 0, "generation": 0, "metric": 1.0, "eval_seconds": 0.0})
    _add_ready_draft(engine, "card-a", x=0.1)
    _add_ready_improve(engine, "card-b", parent=0, x=0.2)
    engine._base_max_nodes = 2          # node 0 holds one slot; exactly one is left


def test_the_last_slot_claim_commits_the_card_its_election_chose(tmp_path, monkeypatch):
    engine, producer = _engine(tmp_path / "last-slot", depth=1)
    _last_slot_board(engine)
    seen: list[tuple[str, int]] = []
    phase = ["election"]
    original = engine.policy.next_actions

    def _recording(state):
        seen.append((phase[0], int(engine.policy.max_nodes)))
        return original(state)

    monkeypatch.setattr(engine.policy, "next_actions", _recording)
    limits: list[tuple[str, int]] = []
    selector = speculation_module.speculative_card_actions

    def _recording_selector(state, policy, max_nodes, **kwargs):
        limits.append((phase[0], int(max_nodes)))
        return selector(state, policy, max_nodes, **kwargs)

    monkeypatch.setattr(speculation_module, "speculative_card_actions", _recording_selector)
    request = _request(engine)
    assert request["card_id"] == "card-b", "the election takes the policy's own action"
    result = _build_result(engine, request)
    assert result.success is True and producer.calls == 1
    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result
    phase[0] = "claim"
    assert engine._serve_card_builds() is True

    closes = [e.data for e in engine.store.read_all() if e.type == EV_CARD_BUILD_DONE]
    assert len(closes) == 1
    assert closes[0].get("skipped_reason") != "not_selected_now", (
        f"the claim asked the policy a different question: max_nodes seen {seen}")
    assert closes[0].get("node_id") == 1 and closes[0].get("card_id") == "card-b", closes
    # …and it is the SAME question, not merely a luckier answer: the policy's own denominator AND
    # the pure selector's limit, which must not be credited twice for the one request.
    assert {n for p, n in seen if p == "claim"} == {n for p, n in seen if p == "election"}, seen
    assert [n for p, n in limits if p == "claim"] == [n for p, n in limits if p == "election"], limits


def test_a_wide_session_commits_the_last_slot_instead_of_spinning_on_it(tmp_path, monkeypatch):
    """The livelock itself, driven: one real Card session at build width 2, bounded by a clock.

    Before the fix this never returned — the same Card was requested, reused and refused hundreds
    of times a second — and the bound below is what turns that hang into a named failure."""
    engine, _producer = _engine(tmp_path / "wide", depth=1)
    engine.role_factory = lambda: (_Researcher(), _Developer())
    engine._llm_parallel = 2
    engine._llm_parallel_launched = 2
    engine._llm_parallel_startup_auto = False
    _without_research(monkeypatch, engine)
    _last_slot_board(engine)

    async def _instant_eval(node_id, _limiter, _max_es):
        node = fold(engine.store.read_all()).nodes[node_id]
        engine.store.append(EV_NODE_EVALUATED, {
            "node_id": node_id, "generation": node.attempt, "metric": 0.5, "eval_seconds": 0.0})

    monkeypatch.setattr(engine, "_evaluate", _instant_eval)

    async def _session() -> bool:
        with anyio.move_on_after(10) as scope:
            await engine._run_card_session([], fold(engine.store.read_all()), None)
        return scope.cancelled_caught

    spun = anyio.run(_session)
    events = engine.store.read_all()
    requests = [e.data for e in events if e.type == EV_CARD_BUILD_REQUESTED]
    refused = [e.data for e in events if e.type == EV_CARD_BUILD_DONE
               and e.data.get("skipped_reason") == "not_selected_now"]
    assert not spun, (
        f"the session never handed back: {len(requests)} requests, {len(refused)} "
        f"`not_selected_now` closes of {sorted({r['card_id'] for r in refused})}")
    assert requests == [{"card_id": "card-b", "generation": 0}]
    assert refused == []
    state = fold(events)
    assert state.nodes[1].idea.card_id == "card-b"


def test_a_claim_that_loses_its_append_race_gives_the_open_request_its_slot_back(
        tmp_path, monkeypatch):
    """The credit is true only once the claim commits or closes. A claim whose `node_building` lost
    the tail race leaves its request OPEN, so the policy must again see that slot as owned — the
    strict denominator a fresh refresh computes, which on the last slot is "budget spent"."""
    engine, _producer = _engine(tmp_path / "retry", depth=1)
    _last_slot_board(engine)
    request = _request(engine)
    result = _build_result(engine, request)
    assert result.success is True
    real_append = engine.store.append

    def _lose_the_race(type, data, **kwargs):
        if type == EV_NODE_BUILDING:
            expected = kwargs.get("expected_last_seq")
            raise EventStoreConcurrencyError(engine.store.path, expected, expected + 1)
        return real_append(type, data, **kwargs)

    monkeypatch.setattr(engine.store, "append", _lose_the_race)
    assert engine._claim_requested_card_build(request, result) == ("retry", None)
    after = engine.policy.max_nodes
    state = fold(engine.store.read_all())
    assert engine._head_request(state) is not None, "the request is still open"
    engine._refresh_speculation_budget(state)
    assert after == engine.policy.max_nodes == card_budget_used(state)


def test_a_reuse_refused_again_is_released_and_the_session_hands_back(tmp_path, monkeypatch):
    """DEFENSE IN DEPTH, whatever makes an election and its claim disagree. The claim is forced to
    refuse every build `not_selected_now`: the first refusal keeps the paid result for a re-election
    (the measured MiniOneRec case that rule exists for), the re-election reuses it, and the SECOND
    refusal — of a result that was already a reuse — releases it and yields the session. Before, it
    was kept again and the same free cycle repeated hundreds of times a second inside one session
    call, where the run loop's `no_mint_turns` bound cannot see it."""
    engine, producer = _engine(tmp_path / "wide", depth=1)
    engine.role_factory = lambda: (_Researcher(), producer)
    engine._llm_parallel = 2
    engine._llm_parallel_launched = 2
    engine._llm_parallel_startup_auto = False
    _without_research(monkeypatch, engine)
    _last_slot_board(engine)
    monkeypatch.setattr(engine, "_claim_requested_card_build",
                        lambda request, result, max_eval_seconds=None: (
                            "stale:not_selected_now", None))

    async def _session() -> bool:
        with anyio.move_on_after(10) as scope:
            await engine._run_card_session([], fold(engine.store.read_all()), None)
        return scope.cancelled_caught

    spun = anyio.run(_session)
    events = engine.store.read_all()
    requests = [e.data for e in events if e.type == EV_CARD_BUILD_REQUESTED]
    refused = [e.data for e in events if e.type == EV_CARD_BUILD_DONE
               and e.data.get("skipped_reason") == "not_selected_now"]
    assert not spun, f"the session never handed back: {len(requests)} requests"
    assert len(requests) == len(refused) == 2, (requests, refused)
    assert producer.calls == 1, "the second request reused the first build, and nothing more"
    assert not engine._spec_reusable, "released, not kept a third time"


def test_a_close_that_burns_the_reserved_id_leaves_no_phantom_slot(tmp_path, monkeypatch):
    """The claim's credit is true only for a request that becomes a node. A commit that fails after
    `node_building` reserved the id closes the request `commit_failed` and BURNS that id, so the
    strict denominator is re-derived after the close (critic 2026-09-26, driven: `policy.max_nodes`
    read one slot the ceiling no longer had, for every unrefreshed reader until the next refresh)."""
    engine, _producer = _engine(tmp_path / "burn", depth=1)
    _last_slot_board(engine)
    request = _request(engine)
    result = _build_result(engine, request)
    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result

    def _boom(*_args, **_kwargs):
        raise RuntimeError("the commit failed before node_created")

    monkeypatch.setattr(engine, "_create_node", _boom)
    assert engine._serve_card_builds() is True
    closes = [e.data for e in engine.store.read_all() if e.type == EV_CARD_BUILD_DONE]
    assert closes[-1].get("skipped_reason") == "commit_failed", closes
    after = engine.policy.max_nodes
    state = fold(engine.store.read_all())
    engine._refresh_speculation_budget(state)
    assert after == engine.policy.max_nodes
    assert engine._node_reservation_slots_remaining(state) == 0, "the burned id was the last slot"
