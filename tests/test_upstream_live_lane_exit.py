"""The live upstream lane at the edges of a turn (doc 73 §2.5, `engine/upstream_serve.py`), critic
2026-10-08.

Real CPU SGD through the doc-72 fixture, driven turn by turn the way `Engine.run` drives it:

* the loop's exit SETTLES a claimed gate (`drain_upstream_job`) — its charges and verdict — instead
  of leaving an unresolved claim that blocks every later lane operation;
* a halted run, or an automatic step under the operator's kill switch, claims nothing new;
* a proposal the lane refused for good is recorded once (`lane_held refused:<code>`) and passed
  over, so the proposals behind it are served — in this process and the next;
* an admitted advance re-checks the base AND newly queued work when it commits, and an unreadable
  base refuses it on its receipt instead of raising out of the loop;
* the lane read and the queue answer with the mode a LIVE engine armed, and say when the operator
  switched the automation off; an exact retry of a refused queued action answers that refusal.
"""
from __future__ import annotations

import shutil

import anyio

from looplab.core.errors import UpstreamRefusal
from looplab.engine import upstream
from looplab.engine.upstream_serve import (auto_next_op, claims_unresolved,
                                           drain_upstream_job, refused_for_good,
                                           serve_upstream_requests, served_mode)
from looplab.events.replay import fold
from tests.test_upstream_live_lane import _engine, _live, _serve


def _turn(engine) -> None:
    """ONE loop turn: serve once, then wait for whatever worker it started (no second serve)."""
    async def _go():
        await serve_upstream_requests(engine, fold(engine.store.read_all()))
        job = engine._upstream_serve.job
        while job is not None and not job.done:
            await anyio.sleep(0.02)
    anyio.run(_go)


def _types(store, pid=None):
    return [e.type for e in store.read_all()
            if pid is None or e.data.get("proposal_id") == pid]


# ------------------------------------------------------------------------------- the loop's exit
def test_the_loop_exit_settles_a_claimed_gate_instead_of_dropping_it(tmp_path):
    lane, store, generation, body, made = _live(tmp_path)
    pid = made["proposal_id"]
    engine = _engine(lane, store)
    _turn(engine)                                   # the automatic check's admission
    _turn(engine)                                   # its claim lands; the bought gate runs
    assert engine._upstream_serve.job is not None and engine._upstream_serve.job.phase == "work"
    assert "upstream_gate_started" in _types(store, pid)
    assert claims_unresolved(store.read_all()), "the claim is open while the gate runs"
    store.append("pause", {})                       # the operator pauses: the loop breaks
    anyio.run(drain_upstream_job, engine)
    kinds = _types(store, pid)
    assert kinds.count("upstream_execution") == 7 and kinds[-1] == "upstream_gate_finished", kinds
    assert not claims_unresolved(store.read_all()), "the bought verdict is on the record"
    assert engine._upstream_serve.job is None
    assert "base_advanced" not in _types(store), "nothing new starts on the way out"


def test_a_halted_run_claims_nothing_its_worker_admitted(tmp_path):
    lane, store, generation, body, made = _live(tmp_path)
    engine = _engine(lane, store)
    _turn(engine)                                   # the automatic check's admission finished
    assert engine._upstream_serve.job is not None and engine._upstream_serve.job.done
    store.append("pause", {})
    before = _types(store)
    _turn(engine)
    assert _types(store) == before, "no claim — and so no unresolved claim — after the run halted"
    assert engine._upstream_serve.job is None
    store.append("resume", {})
    _serve(_engine(lane, store))
    assert "base_advanced" in _types(store), "the next turns derive the same step again"


def test_the_kill_switch_stops_an_admitted_automatic_step(tmp_path):
    lane, store, generation, body, made = _live(tmp_path)
    engine = _engine(lane, store)
    _turn(engine)
    store.append("upstream_auto_set", {"enabled": False})
    before = _types(store)
    _turn(engine)
    assert _types(store) == before and engine._upstream_serve.job is None


def test_a_draft_is_not_proposed_once_the_switch_is_off_and_is_proposed_unpaid_after(tmp_path, monkeypatch):
    from tests.test_upstream_author import _champion_draft, _live_engine, _model
    from tests.test_upstream_lane import fixture
    lane, store, generation, body = fixture(tmp_path)
    calls = []
    _model(monkeypatch, _champion_draft(), calls=calls)
    engine = _live_engine(lane, store)
    _turn(engine)                                   # the author's two paid calls run
    assert engine._upstream_serve.job.op == "author" and engine._upstream_serve.job.done
    store.append("upstream_auto_set", {"enabled": False})
    _turn(engine)
    _turn(engine)                                   # …and the turn a started propose would claim on
    authored, = [e.data for e in store.read_all() if e.type == "lane_authored"]
    assert authored["outcome"] == "drafted"
    assert not any(t.startswith("upstream_proposal") for t in _types(store)), (
        "a paid draft is recorded, but the switch keeps it from becoming a proposal")
    store.append("upstream_auto_set", {"enabled": True})
    _serve(_reengine(lane, store))
    assert "upstream_proposed" in _types(store), "the retained draft is proposed by the next engine"
    assert len(calls) == 2, "…without paying again"


def _reengine(lane, store):
    engine = _engine(lane, store)
    engine.developer = type("Dev", (), {"client": object()})()
    return engine


# ------------------------------------------------------------------------------- refusals
def test_a_proposal_refused_for_good_is_recorded_once_and_passed_over(tmp_path):
    lane, store, generation, body, made = _live(tmp_path)
    second = lane.propose({**body, "action_id": "propose-2"})
    first = made["proposal_id"]
    # The first proposal's retained manifest is gone: every check of it is refused, for good.
    shutil.rmtree(lane.rd / "upstream" / "proposals" / first)
    events = store.read_all()
    assert auto_next_op(events, lane.task.seed_base)[1]["proposal_id"] == first
    engine = _engine(lane, store)
    _turn(engine)
    _turn(engine)
    held = [e.data for e in store.read_all() if e.type == "lane_held"]
    assert held == [{"op": "check", "reason": "refused:upstream_manifest_unavailable",
                     "proposal_id": first}]
    assert ("check", first) in refused_for_good(store.read_all())
    nxt = auto_next_op(store.read_all(), lane.task.seed_base)
    assert nxt[1]["proposal_id"] == second["proposal_id"], "the proposal behind it is served"
    _serve(_engine(lane, store))                    # a NEW engine: the record, not memory, decides
    assert [e.data for e in store.read_all() if e.type == "lane_held"] == held, "said once"
    assert any(e.type == "base_advanced" and e.data["proposal_id"] == second["proposal_id"]
               for e in store.read_all())


def test_a_passing_refusal_is_skipped_for_a_while_and_never_recorded(tmp_path):
    """`skip` (the serve object's recent PASSING refusals) passes a proposal over without a record."""
    lane, store, generation, body, made = _live(tmp_path)
    events = store.read_all()
    pid = made["proposal_id"]
    assert auto_next_op(events, lane.task.seed_base)[1]["action_id"] == f"auto-check-{pid}"
    assert auto_next_op(events, lane.task.seed_base, skip={f"auto-check-{pid}"}) is None


def test_an_advance_rechecks_queued_work_and_an_unreadable_base_when_it_commits(tmp_path, monkeypatch):
    lane, store, generation, body, made = _live(tmp_path)
    engine = _engine(lane, store)
    for _ in range(40):                             # check, then admit the automatic advance
        job = engine._upstream_serve.job
        if job is not None and job.op == "advance" and job.done:
            break
        _turn(engine)
    job = engine._upstream_serve.job
    assert job is not None and job.op == "advance" and job.done and job.error is None
    # An operator inject is queued AFTER the worker admitted the advance, before its commit.
    store.append("inject_node", {"idea": {"operator": "manual", "params": {}}, "parent_id": None})
    assert fold(store.read_all()).inject_requests
    _turn(engine)
    assert "base_advanced" not in _types(store), "the commit re-asks `upstream_work_pending`"
    assert ("advance", job.body["action_id"]) in engine._upstream_serve.refused_auto
    assert not any(e.type == "lane_held" for e in store.read_all()), "a passing refusal, no record"

    # The base turns unreadable between admission and commit: refused on the receipt, not raised.
    store.append("inject_done", {"idx": 0})
    engine._upstream_serve.refused_auto.clear()
    for _ in range(10):
        job = engine._upstream_serve.job
        if job is not None and job.op == "advance" and job.done:
            break
        _turn(engine)
    assert engine._upstream_serve.job.op == "advance"
    import looplab.engine.upstream_state as upstream_state

    def unreadable(*_a, **_k):
        raise UpstreamRefusal("upstream_source_unavailable", "the archive went away")
    monkeypatch.setattr(upstream_state, "active_base", unreadable)
    _turn(engine)                                   # no exception escapes the loop turn
    assert "base_advanced" not in _types(store) and engine._upstream_serve.job is None


# ------------------------------------------------------------------------------- the per-turn cost
def test_the_loop_verifies_the_active_base_once_per_base_not_once_per_turn(tmp_path, monkeypatch):
    """`active_base` re-reads the selected archive's origin log and re-hashes the archive; the loop
    asked for it on every turn (twice with the author on). It is cached on what moves it."""
    import looplab.engine.upstream_state as upstream_state
    from looplab.engine.upstream_serve import lane_queue_cursor
    lane, store, generation, body, made = _live(tmp_path)
    engine = _engine(lane, store)
    _serve(engine)                                  # checked and advanced: the base moved once
    armed = engine._upstream_serve.armed
    armed["settings"] = armed["settings"].model_copy(update={"upstream_author": False})
    real, calls = upstream_state.active_base, []
    monkeypatch.setattr(upstream_state, "active_base",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    for _ in range(5):
        _turn(engine)
    assert len(calls) == 1, "re-verified once for the new base, then served from the cache"
    events = store.read_all()
    assert lane_queue_cursor(events) == fold(events).lane_ops_done


# ------------------------------------------------------------------------------- what readers are told
def test_a_live_engines_armed_mode_wins_over_the_snapshot(tmp_path, monkeypatch):
    lane, store, generation, body, made = _live(tmp_path)
    store.append("lane_armed", {"mode": "propose", "reason": "", "author": False})
    lane.settings = lane.settings.model_copy(update={"upstream_mode": "off"})
    events = store.read_all()
    assert served_mode(events, lane.settings, lane.task.upstream, alive=True) == ("propose", "")
    assert served_mode(events, lane.settings, lane.task.upstream, alive=False)[0] == "off"
    monkeypatch.setattr(upstream, "engine_alive", lambda rd: True)
    read = lane.read(generation)
    assert read["upstream_mode"]["mode"] == "propose" and "Do NOT pause" in read["instruction"]
    queued = lane.check({"expected_generation": generation, "action_id": "q",
                         "proposal_id": made["proposal_id"]})
    assert queued["status"] == "queued", "a snapshot set off since does not refuse the live queue"


def test_the_instruction_says_when_the_operator_switched_the_automation_off(tmp_path):
    lane, store, generation, body, made = _live(tmp_path)
    assert "on its own" in lane.read(generation)["instruction"]
    store.append("upstream_auto_set", {"enabled": False})
    told = lane.read(generation)["instruction"]
    assert "switched its automatic steps OFF" in told and "on its own." not in told


def test_an_exact_retry_of_a_refused_queued_action_answers_the_refusal(tmp_path, monkeypatch):
    lane, store, generation, body, made = _live(tmp_path)
    monkeypatch.setattr(upstream, "engine_alive", lambda rd: True)
    bogus = {"expected_generation": generation, "action_id": "bogus-check",
             "proposal_id": "up_" + "0" * 24}
    assert lane.check(bogus)["status"] == "queued"
    engine = _engine(lane, store)
    _serve(engine)
    done = [e.data for e in store.read_all() if e.type == "lane_op_done"]
    assert done[0]["outcome"] == "refused" and done[0]["code"] == "upstream_proposal_missing"
    again = lane.check(bogus)
    assert again["status"] == "refused" and again["code"] == "upstream_proposal_missing", again
    assert len([e for e in store.read_all() if e.type == "lane_op_requested"]) == 1


def test_a_stopped_run_reads_its_snapshot_not_a_dead_engines_mode(tmp_path):
    lane, store, generation, body, made = _live(tmp_path)
    store.append("lane_armed", {"mode": "auto", "reason": "", "author": True})
    lane.settings = lane.settings.model_copy(update={"upstream_mode": "off"})
    read = lane.read(generation)
    assert read["upstream_mode"]["mode"] == "off" and read["engine_running"] is False
