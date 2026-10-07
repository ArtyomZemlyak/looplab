"""The plateau stop (doc 70 §3, item 70.4).

A hard stall moved the endgame earlier (`engine/plan.py::replan`) and never ENDED the run: the budget
ran on to `max_nodes` without a new leader. `Settings.plateau_stop_nodes` K > 0 ends the SEARCH once
K settled nodes inside the plan's endgame window after the search leader have not taken its place
(`engine/plan.py::plateau_stop_due`), and the run then ends the way a spent node budget ends it —
what is built is evaluated, and the empty-action ladder (noise floor, confirm, holdout, approval)
finishes it (`orchestrator.py::_plateau_stop_turn`). 0 — the default everywhere — never stops.
"""
from __future__ import annotations

from types import SimpleNamespace

import anyio

from looplab.core.config import Settings
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.options import EngineOptions
from looplab.engine.plan import (
    PLATEAU_STOP_REASON, build_plan, plateau_leader, plateau_nodes, plateau_rearm_floor,
    plateau_stop_due, replan)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.replay_requests import accepted_add_nodes
from looplab.events.types import (EV_BUDGET_EXTEND, EV_CONFIRM_EVAL, EV_INJECT_DONE, EV_INJECT_NODE,
                                  EV_NODE_ABORT, EV_NODE_CREATED, EV_NODE_EVALUATED, EV_PLAN,
                                  EV_RESUME, EV_RUN_FINISHED, EV_RUN_REOPENED)
from tests.factories import make_engine


def _state(*, leader=3, n=10, plan=None, settled=(), statuses=None, metrics=None):
    """Nodes 0..n-1: the leader holds the best metric (1.0), every other settled node 5.0, and a
    node neither settled nor at or before the leader is pending."""
    st = RunState()
    statuses = statuses or {}
    metrics = metrics or {}
    for i in range(n):
        status = statuses.get(i, NodeStatus.evaluated if i in settled or i <= leader
                              else NodeStatus.pending)
        metric = metrics.get(i, 1.0 if i == leader else 5.0)
        st.nodes[i] = Node(id=i, operator="draft", idea=Idea(operator="draft"), status=status,
                           metric=metric if status is NodeStatus.evaluated else None)
    st.best_node_id = leader
    st.plan = plan if plan is not None else build_plan(max_nodes=12, n_seeds=2, reserve_frac=0.5,
                                                       at_node=0)
    return st


# ------------------------------------------------------------------------------------ the rule
def test_the_count_is_settled_endgame_nodes_after_the_search_leader():
    """MUTATIONS: count pending nodes; count from the reserve start rather than after the leader;
    ignore the window's start."""
    st = _state(leader=3, settled=(6, 7))                 # reserve [6, 12)
    assert st.plan["endgame_start"] == 6
    assert plateau_nodes(st) == (3, 2)
    assert plateau_stop_due(st, 2).startswith("plateau: 2 endgame node(s) after node 3")
    assert plateau_stop_due(st, 3) is None
    better = _state(leader=3, settled=(6, 7), metrics={7: 0.5})
    assert plateau_nodes(better) == (7, 0), "a new leader inside the window restarts the count"
    before = _state(leader=3, settled=(4, 5))             # settled, but before the reserve
    assert plateau_nodes(before) == (3, 0)


def test_the_leader_is_the_search_s_not_the_champion_s():
    """The champion is re-ranked by the ladder the stop hands the run to (confirm, holdout, an
    approval); the search leader is not (MUTATION: read `best_node_id`). A later TIE did not improve
    on the leader, and a node that may not count toward the best cannot lead (MUTATIONS: `<=` for
    the improvement; drop the `counts_toward_best` filter)."""
    st = _state(leader=3, settled=(6, 7, 8), metrics={6: 1.0})
    st.best_node_id = 8                                   # e.g. confirm crowned another node
    assert plateau_leader(st) == 3 and plateau_nodes(st) == (3, 3)
    st.nodes[7].feasible = False
    st.nodes[7].metric = 0.1                              # better, but infeasible
    st.nodes[8].metric = 0.2
    st.breed_excluded = [8]                               # better, but trust-flagged
    assert plateau_leader(st) == 3
    st.aborted_nodes = [6]
    st.nodes[6].metric = 0.3                              # better, but operator-aborted
    assert plateau_leader(st) == 3
    up = _state(leader=3, settled=(6, 7), metrics={3: 9.0, 6: 9.0, 7: 1.0})
    up.direction = "max"
    assert plateau_leader(up) == 3, "the run's own direction decides what better is"


def test_what_is_not_an_attempt_does_not_count():
    st = _state(leader=3, settled=(6, 7, 8, 9),
                statuses={8: NodeStatus.failed, 9: NodeStatus.failed})
    st.nodes[8].error_reason = "superseded"               # a benign terminal: not an attempt
    st.nodes[9].error_reason = "crash"                    # a real failure IS an attempt
    st.nodes[7].tombstoned = True
    st.aborted_nodes = [6]
    assert plateau_nodes(st) == (3, 1)


def test_an_engine_error_is_the_box_not_an_attempt():
    """Incident 2026-10-06: a box fault (`engine_error`: a full disk, a read-only run dir) is no
    attempt at the experiment, and counting it let a broken box end the search as a plateau.
    MUTATION: count it like `crash`."""
    st = _state(leader=3, settled=(6, 7, 8),
                statuses={7: NodeStatus.failed, 8: NodeStatus.failed})
    st.nodes[7].error_reason = "engine_error"
    st.nodes[8].error_reason = "crash"
    assert plateau_nodes(st) == (3, 2)
    assert plateau_stop_due(st, 3) is None


def test_a_bounded_episode_counts_only_while_its_row_is_current():
    plan = build_plan(max_nodes=100, n_seeds=2, reserve_frac=0.2, at_node=0)
    episode = replan(plan, max_nodes=100, n_seeds=2, reserve_frac=0.2, at_node=5, stall_rung=2,
                     stall_nodes=3, champion=3)
    assert (episode["endgame_start"], episode["endgame_end"]) == (5, 8)
    st = _state(leader=3, n=10, plan=episode, settled=(5, 6, 7, 8, 9))
    assert plateau_nodes(st) == (3, 3), "the window's end bounds it"
    reopened = replan(episode, max_nodes=100, n_seeds=2, reserve_frac=0.2, at_node=8,
                      stall_rung=3, stall_nodes=3, champion=3)
    assert reopened["reason"] == "reopened"
    st.plan = reopened
    assert plateau_nodes(st) == (3, 0), "breadth resumed: the episode no longer counts"


def test_off_no_plan_and_no_leader_never_stop():
    st = _state(leader=3, settled=(6, 7, 8))
    for k in (0, -1, None, True, "2"):
        assert plateau_stop_due(st, k) is None, k
    st.plan = None
    assert plateau_nodes(st) == (None, 0) and plateau_stop_due(st, 1) is None
    st = _state(leader=3, settled=(6, 7, 8))
    for node in st.nodes.values():
        node.feasible = False                             # nothing may count toward the best
    assert plateau_nodes(st) == (None, 0) and plateau_stop_due(st, 1) is None


def test_the_floor_restarts_the_count():
    st = _state(leader=3, settled=(6, 7, 8, 9))
    assert plateau_nodes(st) == (3, 4)
    assert plateau_nodes(st, floor=8) == (3, 2)
    assert plateau_stop_due(st, 3, floor=8) is None and plateau_stop_due(st, 2, floor=8)


def _ev(type_, **data):
    return SimpleNamespace(type=type_, data=data)


def test_an_operator_s_extension_or_the_reopen_of_a_finished_run_re_arms_it():
    """MUTATIONS: re-arm on an extension the fold refuses; re-arm on a resume from a pause; miss the
    reopen of a finished run."""
    created = [_ev(EV_NODE_CREATED, node_id=i) for i in range(5)]
    assert plateau_rearm_floor(created) == 0
    assert plateau_rearm_floor(created + [_ev(EV_BUDGET_EXTEND, add_nodes=3)]) == 5
    for refused in (0, -2, True, 2.5, "x", None):
        assert plateau_rearm_floor(created + [_ev(EV_BUDGET_EXTEND, add_nodes=refused)]) == 0
    assert plateau_rearm_floor(created + [_ev(EV_BUDGET_EXTEND, max_seconds=600)]) == 0
    assert plateau_rearm_floor(created + [_ev(EV_RESUME)]) == 0, "a pause's resume: same search"
    for reopen in (EV_RESUME, EV_RUN_REOPENED):
        log = created + [_ev(EV_RUN_FINISHED, reason="plateau"), _ev(reopen)]
        assert plateau_rearm_floor(log) == 5, reopen
    later = created + [_ev(EV_BUDGET_EXTEND, add_nodes=1), _ev(EV_NODE_CREATED, node_id=5),
                       _ev(EV_NODE_CREATED, node_id=6)]
    assert plateau_rearm_floor(later) == 5, "the floor is where the extension landed"


def test_the_extension_reading_is_the_fold_s(tmp_path):
    """`accepted_add_nodes` is the fold's own acceptance rule, hoisted — the budget the fold sums is
    exactly what it grants per row."""
    store = EventStore(tmp_path / "events.jsonl")
    rows = (3, "4", 2.0, 2.5, True, 0, -1, 1_000_001, "x", None)
    for raw in rows:
        store.append(EV_BUDGET_EXTEND, {"add_nodes": raw})
    assert fold(store.read_all()).budget_overrides["add_nodes"] == \
        sum(accepted_add_nodes(raw) for raw in rows) == 9


def test_the_switch_is_off_everywhere_by_default(tmp_path):
    assert Settings().plateau_stop_nodes == 0 and EngineOptions().plateau_stop_nodes == 0
    assert EngineOptions.from_settings(Settings(plateau_stop_nodes=4)).plateau_stop_nodes == 4
    assert make_engine(tmp_path / "run")._plateau_stop_nodes == 0


def test_the_attention_feed_names_the_reason():
    from looplab.serve.attention import _BUDGET_REASONS
    assert PLATEAU_STOP_REASON == "plateau" and "plateau_stop_nodes" in _BUDGET_REASONS["plateau"]


# ------------------------------------------------------------------------------------ driven
class _Stalled:
    """Two seeds — one near the optimum, one far — then every improve lands far from it."""

    def propose(self, state, parent):
        if parent is None:
            near = not any(n.operator == "draft" for n in state.nodes.values())
            x, y = (3.2, -1.0) if near else (-8.0, 8.0)
            return Idea(operator="draft", params={"x": x, "y": y}, rationale="seed")
        return Idea(operator="improve", params={"x": 9.0, "y": 9.0}, rationale=f"push {parent.id}")


def _engine(tmp_path, name, **kw):
    eng = make_engine(tmp_path / name, n_seeds=2, max_nodes=16, endgame_reserve_frac=0.25,
                      researcher=_Stalled(), **kw)
    eng._endgame_sweep = False
    return eng


def _run(tmp_path, name, **kw):
    eng = _engine(tmp_path, name, **kw)
    state = anyio.run(eng.run)
    return eng, state, eng.store.read_all()


def _finished(events):
    return [e for e in events if e.type == EV_RUN_FINISHED]


def test_a_stalled_run_stops_on_its_plateau_and_off_it_spends_the_budget(tmp_path):
    """MUTATION: drop the gate -> the stopped run spends all 16 nodes."""
    eng, state, events = _run(tmp_path, "on", plateau_stop_nodes=2)
    assert state.finished and _finished(events)[-1].data["reason"] == PLATEAU_STOP_REASON
    assert state.best_node_id == 0 and len(state.nodes) < 16
    assert not state.pending_nodes(), "nothing built was left unmeasured"
    leader, count = plateau_nodes(state)
    assert leader == 0 and count >= 2
    assert any(e.type == EV_PLAN for e in events)
    _eng, off, off_events = _run(tmp_path, "off")
    assert off.finished and len(off.nodes) == 16
    assert all(e.data.get("reason") != PLATEAU_STOP_REASON for e in _finished(off_events))


def test_the_run_ends_through_the_ladder_the_budget_s_end_takes(tmp_path):
    """A run that stopped improving still owes its champion the confirmation its operator configured
    (MUTATION: finish through `_settle_terminal_gate`, the ceilings' ending, which skips it)."""
    _eng, state, events = _run(tmp_path, "confirm", plateau_stop_nodes=2, confirm_top_k=1,
                               confirm_seeds=2)
    finish = _finished(events)[-1]
    assert finish.data["reason"] == PLATEAU_STOP_REASON and state.confirmed_done
    assert any(e.type == EV_CONFIRM_EVAL and e.seq < finish.seq for e in events)
    assert len(state.nodes) < 16


def test_a_queued_inject_is_served_and_measured_before_the_stop(tmp_path, monkeypatch):
    """An operator's inject that lands the turn the stop comes due is new breadth the operator asked
    for: it is served, its node is EVALUATED, and only then does the run end (MUTATIONS: stop over
    the queued head; end over a built node without evaluating it)."""
    from looplab.engine import orchestrator
    eng = _engine(tmp_path, "queued", plateau_stop_nodes=2)
    real, queued = orchestrator.plateau_stop_due, []

    def queue_an_inject_when_due(state, stop_nodes, **kw):
        why = real(state, stop_nodes, **kw)
        if why is not None and not queued:
            queued.append(len(state.nodes))
            eng.store.append(EV_INJECT_NODE, {"idea": {
                "operator": "manual", "params": {"x": -9.0, "y": 9.0},
                "rationale": "the operator's last idea"}})
        return why

    monkeypatch.setattr(orchestrator, "plateau_stop_due", queue_an_inject_when_due)
    state = anyio.run(eng.run)
    events = eng.store.read_all()
    assert queued, "the stop came due"
    finish = _finished(events)[-1]
    assert finish.data["reason"] == PLATEAU_STOP_REASON
    done = [e.data for e in events if e.type == EV_INJECT_DONE]
    assert done and "skipped" not in done[0]
    injected = next(n for n in state.nodes.values() if n.operator == "manual")
    assert injected.status is NodeStatus.evaluated
    measured = next(e for e in events if e.type == EV_NODE_EVALUATED
                    and e.data.get("node_id") == injected.id)
    assert measured.seq < finish.seq


def test_a_finished_run_reopened_with_more_nodes_searches_again(tmp_path):
    """MUTATION: no re-arm floor -> the reopened run re-reads the same K nodes after the same leader
    and finishes again without building one."""
    eng, first, _events = _run(tmp_path, "reopen", plateau_stop_nodes=2)
    assert _finished(_events)[-1].data["reason"] == PLATEAU_STOP_REASON
    built = len(first.nodes)
    eng.store.append(EV_RUN_REOPENED, {})
    eng.store.append(EV_BUDGET_EXTEND, {"add_nodes": 4})
    again = _engine(tmp_path, "reopen", plateau_stop_nodes=2)
    state = anyio.run(again.run)
    assert state.finished and len(state.nodes) > built
    assert not state.pending_nodes()


# ------------------------------------------------------------------------------------ the turn
def _seeded_engine(tmp_path, *, pending=(1,), aborted=()):
    """An engine whose log holds node 0 evaluated and each of `pending` built, not yet measured."""
    eng = make_engine(tmp_path / "turn")
    eng.store.append("run_started", {"run_id": "turn", "task_id": "toy", "direction": "min"})
    for node_id in (0, *pending):
        eng.store.append(EV_NODE_CREATED, {
            "node_id": node_id, "parent_ids": [], "operator": "draft",
            "idea": Idea(operator="draft").model_dump(mode="json"), "code": ""})
    eng.store.append(EV_NODE_EVALUATED, {"node_id": 0, "generation": 0, "metric": 1.0})
    for node_id in aborted:
        eng.store.append(EV_NODE_ABORT, {"node_id": node_id, "generation": 0})
    return eng


def _turn(eng, state, seq=7):
    return anyio.run(lambda: eng._plateau_stop_turn(state, "why", decision_seq=seq, max_es=None))


def _record(eng, calls, *, card=False, inflight=False, dispatch=None):
    eng._close_card_build_before_terminal_gate = (
        lambda state, max_es: calls.append("card") or card)
    eng._evals_inflight = lambda: inflight

    async def drain():
        calls.append("drain")

    async def raise_deferred():
        calls.append("deferred")

    async def dispatch_evals(evals, state, max_es, *, research=True):
        calls.append(("dispatch", [a["node_id"] for a in evals], research))
        if dispatch is not None:
            dispatch()

    async def ladder(state, *, decision_seq, finish_reason=None):
        calls.append(("ladder", decision_seq, finish_reason, len(state.nodes)))
        return "break"

    eng._drain_adopted_evals = drain
    eng._raise_deferred_eval_budget_stop = raise_deferred
    eng._dispatch_evals = dispatch_evals
    eng._handle_no_actions = ladder


def test_the_turn_settles_a_card_head_then_drains_then_measures_then_ends(tmp_path):
    """The order IS the rule (MUTATIONS: skip the Card head; skip the drain; skip the owed
    evaluation — each would let the ladder run over live or paid-for work)."""
    eng = _seeded_engine(tmp_path)
    state = fold(eng.store.read_all())
    calls: list = []
    _record(eng, calls, card=True)
    assert _turn(eng, state) == "continue" and calls == ["card"]
    calls.clear()
    _record(eng, calls, inflight=True)
    assert _turn(eng, state) == "continue" and calls == ["card", "drain", "deferred"]
    calls.clear()
    measured = lambda: eng.store.append(EV_NODE_EVALUATED,  # noqa: E731
                                        {"node_id": 1, "generation": 0, "metric": 5.0})
    _record(eng, calls, dispatch=measured)
    assert _turn(eng, state) == "continue"
    assert calls == ["card", ("dispatch", [1], False)], "evaluation only, no research overlap"
    calls.clear()
    _record(eng, calls)
    settled = fold(eng.store.read_all())
    assert _turn(eng, settled, seq=11) == "break"
    assert calls == ["card", ("ladder", 11, PLATEAU_STOP_REASON, 2)]


def test_an_aborted_node_is_not_handed_to_the_dispatch(tmp_path):
    eng = _seeded_engine(tmp_path, pending=(1, 2), aborted=(2,))
    state = fold(eng.store.read_all())
    assert 2 in state.aborted_nodes
    calls: list = []
    _record(eng, calls, dispatch=lambda: eng.store.append(
        EV_NODE_EVALUATED, {"node_id": 1, "generation": 0, "metric": 5.0}))
    _turn(eng, state)
    assert ("dispatch", [1], False) in calls


def test_a_dispatch_that_moves_nothing_ends_on_a_fresh_fold_instead_of_looping(tmp_path):
    """An admission that refuses every owed node would hand the same nodes back on every turn; the
    ladder runs over them instead, on the fold and the tail AFTER the dispatch (MUTATION: always
    `continue` after a dispatch)."""
    eng = _seeded_engine(tmp_path)
    state = fold(eng.store.read_all())
    calls: list = []
    _record(eng, calls, dispatch=lambda: eng.store.append("phase_progress", {"phase": "x"}))
    assert _turn(eng, state, seq=3) == "break"
    tail = eng.store.read_all()[-1].seq
    assert calls[-1] == ("ladder", tail, PLATEAU_STOP_REASON, 2) and tail != 3
