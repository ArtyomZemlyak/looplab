"""The upstream lane served by the LIVE engine (doc 73 §2.5, `engine/upstream_serve.py`).

Real CPU SGD through the doc-72 fixture: a proposal made on the stopped lane is CHECKED and ADVANCED
by a running engine's main task under `upstream_mode: auto` (every row appended by it, in the order
`claimed_gate_executions` reads), an operation asked of a live run is QUEUED and served with a
positional receipt, a lifecycle whose evaluation started stays on its base across the advance, and a
node built on the launch base names it so its next lifecycle merges onto the new one correctly.
"""
from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest

from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings
from looplab.engine import upstream
from looplab.engine.upstream_serve import (UpstreamServe, auto_next_op, base_stamp,
                                           resolve_upstream_mode, serve_upstream_requests,
                                           upstream_mode_setting)
from looplab.engine.upstream_state import active_base, events_for
from looplab.engine.upstream_workspace import materialization_plan
from looplab.events.replay import fold
from tests.test_upstream_lane import fixture
from tests.test_upstream_multibase import create, materialize


def _engine(lane, store):
    return SimpleNamespace(run_dir=lane.rd, task=lane.task, _repo_spec=lane.task.repo_spec(),
                           store=store, _write_lock=anyio.Lock(), _upstream_serve=UpstreamServe())


def _serve(engine, *, turns=40):
    """Drive the loop turns a live engine would: serve, wait for the worker, serve again."""
    async def _drive():
        for _ in range(turns):
            await serve_upstream_requests(engine, fold(engine.store.read_all()))
            job = engine._upstream_serve.job
            if job is None and auto_next_op(engine.store.read_all(), engine._repo_spec.get("seed_base")) is None:
                state = fold(engine.store.read_all())
                if state.lane_ops_done >= len(state.lane_op_requests):
                    return
            while job is not None and not job.done:
                await anyio.sleep(0.02)
    anyio.run(_drive)


def _live(tmp_path):
    lane, store, generation, body = fixture(tmp_path)
    made = lane.propose(body)
    store.append("resume", {})                 # the fixture paused its run; a live engine runs
    assert not fold(store.read_all()).halted
    return lane, store, generation, body, made


# ------------------------------------------------------------------------------- the mode
def test_the_mode_has_one_reader_is_auto_and_resumes_off():
    assert Settings().upstream_mode == "auto"
    assert upstream_mode_setting(Settings(upstream_mode="propose")) == "propose"
    assert upstream_mode_setting(object()) == "off"
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["upstream_mode"] == "off"


def test_auto_degrades_to_off_where_nothing_can_be_checked():
    declared = {"tests": []}
    assert resolve_upstream_mode(Settings(), None)[0] == "off"
    assert "no upstream block" in resolve_upstream_mode(Settings(), None)[1]
    assert resolve_upstream_mode(Settings(trust_mode="untrusted"), declared)[0] == "off"
    assert resolve_upstream_mode(Settings(), declared) == ("auto", "")
    assert resolve_upstream_mode(Settings(upstream_mode="off"), declared)[0] == "off"


# ------------------------------------------------------------------------------- auto, driven
def test_a_live_engine_checks_and_advances_a_proposal_without_a_pause(tmp_path):
    lane, store, generation, body, made = _live(tmp_path)
    pid = made["proposal_id"]
    assert auto_next_op(store.read_all(), lane.task.seed_base)[0] == "check"
    engine = _engine(lane, store)
    _serve(engine)
    events = store.read_all()
    kinds = [e.type for e in events if e.data.get("proposal_id") == pid]
    start = kinds.index("upstream_gate_started")
    assert kinds[start:start + 9] == (["upstream_gate_started"] + ["upstream_execution"] * 7
                                      + ["upstream_gate_finished"]), kinds
    advanced = [e for e in events if e.type == "base_advanced"]
    assert len(advanced) == 1 and advanced[0].data["in_engine"] is True
    assert advanced[0].data["action_id"] == f"auto-advance-{pid}"
    assert active_base(events, lane.task.seed_base)["selector"] == made["selector"]
    assert auto_next_op(events, lane.task.seed_base) is None, "nothing left to do; a re-entry is idempotent"
    before = store.path.read_bytes()
    _serve(_engine(lane, store))
    assert store.path.read_bytes() == before


def test_a_lifecycle_whose_evaluation_started_stays_on_its_base(tmp_path):
    lane, store, generation, body, made = _live(tmp_path)
    old = active_base(store.read_all(), lane.task.seed_base)["selector"]
    create(store, 2, {"recipe.env": "MOMENTUM=0.3\n"})
    create(store, 3, {"recipe.env": "MOMENTUM=0.4\n"})
    materialize(lane, store, 2)
    materialize(lane, store, 3)
    store.append("node_eval_started", {"node_id": 2, "generation": 0})
    _serve(_engine(lane, store))
    events = events_for(lane.rd)
    assert active_base(events, lane.task.seed_base)["selector"] == made["selector"]
    state = fold(events)
    spec, _node, receipt = materialization_plan(lane.task.repo_spec(), state.nodes[2], events)
    assert receipt["status"] == "pinned" and spec["effective_seed_base"]["digest"] == old["digest"]
    spec3, _n3, receipt3 = materialization_plan(lane.task.repo_spec(), state.nodes[3], events)
    assert receipt3["status"] != "pinned", "seeded but not started: it migrates, as doc 72 designed"
    assert spec3["effective_seed_base"] == made["selector"]


def test_a_node_built_on_the_launch_base_merges_from_it(tmp_path):
    lane, store, generation, body, made = _live(tmp_path)
    engine = _engine(lane, store)
    launch = base_stamp(engine)
    assert launch is not None and launch["digest"] == lane.task.seed_base["digest"]
    _serve(engine)
    # Built by a Developer bound at launch, created AFTER the live advance: its row names the base.
    store.append("node_created", {"node_id": 7, "operator": "improve", "parent_ids": [0],
                                  "idea": {"operator": "improve"}, "files": {"recipe.env": "MOMENTUM=0.2\n"},
                                  "base_selector": dict(launch)})
    events = events_for(lane.rd)
    _spec, _node, receipt = materialization_plan(lane.task.repo_spec(), fold(events).nodes[7], events)
    assert receipt["from_digest"] == launch["digest"]
    assert receipt["to_digest"] == made["selector"]["digest"]


def test_the_stamp_is_absent_while_the_live_lane_is_off(tmp_path):
    lane, store, *_ = fixture(tmp_path)
    snapshot = lane.rd / "config.snapshot.json"
    snapshot.write_text(Settings.model_validate_json(snapshot.read_bytes()).model_copy(
        update={"upstream_mode": "off"}).model_dump_json(), encoding="utf8")
    assert base_stamp(_engine(lane, store)) is None
    plain = SimpleNamespace(run_dir=tmp_path, _repo_spec={}, _upstream_serve=UpstreamServe())
    assert base_stamp(plain) is None


# ------------------------------------------------------------------------------- the queue
def test_an_operation_asked_of_a_live_run_is_queued_and_served_with_a_receipt(tmp_path, monkeypatch):
    lane, store, generation, body, made = _live(tmp_path)
    lane.settings = lane.settings.model_copy(update={"upstream_mode": "propose"})
    snapshot = lane.rd / "config.snapshot.json"
    snapshot.write_text(lane.settings.model_dump_json(), encoding="utf8")
    check = {"expected_generation": generation, "action_id": "operator-check", "proposal_id": made["proposal_id"]}
    monkeypatch.setattr(upstream, "engine_alive", lambda rd: True)
    queued = lane.check(check)
    assert queued["status"] == "queued" and lane.check(check) == queued, "an exact retry is the same entry"
    with pytest.raises(upstream.UpstreamRefusal) as conflict:
        lane.check({**check, "proposal_id": "up_" + "0" * 24})
    assert conflict.value.code == "upstream_action_conflict"
    _serve(_engine(lane, store))
    state = fold(store.read_all())
    assert state.lane_ops_done == 1
    done = [e for e in store.read_all() if e.type == "lane_op_done"][-1].data
    assert done["outcome"] == "succeeded" and done["action_id"] == "operator-check"
    assert not any(e.type == "base_advanced" for e in store.read_all()), "propose mode: no auto advance"
    monkeypatch.setattr(upstream, "engine_alive", lambda rd: False)
    assert lane.check(check)["event_type"] == "upstream_gate_finished", "the ACK is the lane's own row"


def test_a_queued_operation_on_a_run_that_resolved_off_is_refused_on_its_receipt(tmp_path):
    lane, store, generation, body, made = _live(tmp_path)
    snapshot = lane.rd / "config.snapshot.json"
    snapshot.write_text(lane.settings.model_copy(update={"upstream_mode": "off"}).model_dump_json(),
                        encoding="utf8")
    store.append("lane_op_requested", {"op": "check", "action_id": "x", "request_hash": "0" * 64,
                                       "body": {"action_id": "x"}})
    _serve(_engine(lane, store))
    done = [e for e in store.read_all() if e.type == "lane_op_done"][-1].data
    assert done == {"idx": 0, "op": "check", "action_id": "x", "outcome": "refused",
                    "code": "upstream_mode_off"}


def test_a_halted_run_starts_nothing(tmp_path):
    lane, store, _generation, body = fixture(tmp_path)        # the fixture ends paused
    lane.propose(body)
    before = store.path.read_bytes()
    _serve(_engine(lane, store), turns=3)
    assert store.path.read_bytes() == before


# ------------------------------------------------------------------------------- what agents are told
def test_the_lane_read_says_which_lane_serves_the_run(tmp_path, monkeypatch):
    lane, store, generation, body, made = _live(tmp_path)
    live = lane.read(generation)
    assert live["upstream_mode"] == {"mode": "auto", "reason": ""}
    assert "Do NOT pause" in live["instruction"] and live["live_queue"]["total"] == 0
    lane.settings = lane.settings.model_copy(update={"upstream_mode": "off"})
    off = lane.read(generation)
    assert off["instruction"] == upstream.UpstreamLane._STOPPED_INSTRUCTION, "doc 72's text, byte for byte"
    lane.settings = lane.settings.model_copy(update={"upstream_mode": "propose"})
    monkeypatch.setattr(upstream, "engine_alive", lambda rd: True)
    lane.check({"expected_generation": generation, "action_id": "q", "proposal_id": made["proposal_id"]})
    queue = lane.read(generation)["live_queue"]
    assert queue["pending"] == 1 and queue["rows"][0]["op"] == "check"
    assert queue["rows"][0]["proposal_id"] == made["proposal_id"]


def test_the_assistant_tools_describe_the_live_lane_only_when_wired(tmp_path):
    from looplab.tools.upstream_tools import UpstreamTools

    def text(tools):
        return " ".join(s["function"]["description"] for s in tools.specs())
    stopped = text(UpstreamTools(tmp_path, mode="auto"))
    live = text(UpstreamTools(tmp_path, mode="auto", live_lane=True))
    assert "Stopped engine required" in stopped and "QUEUES the check" not in stopped
    assert "QUEUES the check" in live


# ------------------------------------------------------------------------------- the Developer moves
def test_a_developer_built_at_launch_rebinds_to_the_advanced_base(tmp_path):
    from looplab.adapters.repo_task import LLMRepoDeveloper
    lane, store, generation, body, made = _live(tmp_path)
    dev = LLMRepoDeveloper(object(), lane.task, plan_decompose=False)
    launch = dict(dev.authored_base)
    assert launch["digest"] == lane.task.seed_base["digest"]
    _serve(_engine(lane, store))                        # the live engine advances the base
    assert dev.authored_base == launch, "nothing moves under a Developer between calls by itself"
    dev.rebind_base()
    assert dev.authored_base["digest"] == made["selector"]["digest"]
    assert any(made["selector"]["digest"] in str(ed) for ed in dev._editables), (
        "the code it reads is pinned to the promoted base")


def test_run_developer_rebinds_before_the_call_and_records_what_it_authored_on(tmp_path):
    from factories import make_engine
    from looplab.engine.upstream_serve import _NO_BASE, take_authored

    class Dev:
        def __init__(self):
            self.authored_base, self.rebinds = {"digest": "a"}, 0

        def rebind_base(self, selector=None):
            self.rebinds += 1
            self.authored_base = dict(selector) if selector is not None else {"digest": "b"}

    engine = make_engine(tmp_path / "run")
    engine._upstream_serve.armed = {"mode": "auto", "reason": "", "settings": None,
                                    "stamp": {"digest": "b"}}
    dev = Dev()
    seen = []
    result = engine._run_developer(dev, lambda: seen.append(dev.authored_base) or "code")
    assert dev.rebinds == 1 and seen == [{"digest": "b"}], "rebound BEFORE the call"
    assert result.authored_base == {"digest": "b"} and take_authored() == {"digest": "b"}
    assert take_authored() is _NO_BASE, "consumed once"
    engine._run_developer(dev, lambda: "again")
    assert dev.rebinds == 1, "already on the engine's base: no second rebind"
    pinned = engine._run_developer(dev, lambda: "repair", pinned_base={"digest": "a"})
    assert dev.rebinds == 2 and pinned.authored_base == {"digest": "a"}, (
        "a repair authors on its lifecycle's base, not the engine's current one")


def test_a_repair_reads_the_base_its_lifecycle_was_seeded_on(tmp_path):
    from looplab.adapters.repo_task import LLMRepoDeveloper
    from looplab.engine.upstream_serve import lifecycle_base
    lane, store, generation, body, made = _live(tmp_path)
    old = active_base(store.read_all(), lane.task.seed_base)["selector"]
    create(store, 2, {"recipe.env": "MOMENTUM=0.3\n"})
    materialize(lane, store, 2)
    store.append("node_eval_started", {"node_id": 2, "generation": 0})
    engine = _engine(lane, store)
    _serve(engine)
    assert base_stamp(engine)["digest"] == made["selector"]["digest"], "the engine moved on"
    node = fold(store.read_all()).nodes[2]
    assert lifecycle_base(engine, node) == old, "the lifecycle stays on the base it was seeded from"
    dev = LLMRepoDeveloper(object(), lane.task, plan_decompose=False)
    dev.rebind_base()
    assert dev.authored_base["digest"] == made["selector"]["digest"]
    dev.rebind_base(lifecycle_base(engine, node))
    assert dev.authored_base == old
    assert any(old["digest"] in str(ed) for ed in dev._editables)
    assert lifecycle_base(engine, SimpleNamespace(id=99, attempt=0)) is None, "never seeded"
