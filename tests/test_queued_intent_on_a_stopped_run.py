"""A QUEUED intent sent to a STOPPED run waits for the operator's resume (doc 69 69.30).

On MiniOneRec v10 the operator stopped the run to queue eight injects and a hint for its next start.
The first inject's command started `looplab resume` — the command service's `ENSURE_RUNNING` ladder
— and the CLI's resume LIFTED the pause: the search began before the rest had arrived, and the hint
landed after the Strategist's first decision. A fork, an inject, a forced confirm or ablation, a
deep-research request and a strategy pin are served by the SEARCH, never by an engine start of their
own (`serve/protocol.py::QUEUED_WHILE_STOPPED`): on a stopped run their command records the intent,
settles `succeeded` with `deferred_until_resume`, and starts nothing — the operator's resume serves
the whole queue at once. Everything else keeps its policy: a reset or a budget extension still
starts the engine it asks for.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from factories import command_terminal as _terminal  # noqa: E402
from factories import post_command as _post  # noqa: E402
from test_run_command_service import _ack_marked, _client, _Driver, _seed, _types  # noqa: E402

from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.serve.control_validation import CONTROL_SPECS  # noqa: E402
from looplab.serve.protocol import (  # noqa: E402
    QUEUED_WHILE_STOPPED, EnginePolicy, stop_holds_queued_intents, waits_for_resume)

_IDEA = {"operator": "manual", "params": {"x": 1.0}, "rationale": "queued while stopped"}
# One valid payload per queued intent, on `_seed`'s node 0 (generation 0).
_QUEUED = {
    "inject_node": {"idea": _IDEA, "code": "print(1)"},
    "fork": {"from_node_id": 0, "generation": 0},
    "force_confirm": {"node_id": 0, "generation": 0},
    "force_ablate": {"node_id": 0, "generation": 0},
    "deep_research": {},
    "set_strategy": {"strategy": {"policy": "mcts"}},
}


def _store(rd):
    return EventStore(rd / "events.jsonl")


# --------------------------------------------------------------------------------- the rule itself

def test_the_queued_set_is_exactly_the_search_served_intents():
    """Pinned as a set, so widening or narrowing it is a decision the diff shows. Every member is
    an `ENSURE_RUNNING` + `engine_ack` command (its ack is what the resumed search writes); the
    intents that ASK the run to go on — the resume family, a reset, a budget extension, an approval —
    are not queued, and nor is anything that starts no engine anyway."""
    assert QUEUED_WHILE_STOPPED == set(_QUEUED)
    for event_type in QUEUED_WHILE_STOPPED:
        spec = CONTROL_SPECS[event_type]
        assert (spec.engine_policy, spec.postcondition) == (
            EnginePolicy.ENSURE_RUNNING, "engine_ack"), event_type
    for goes_on in ("resume", "run_reopened", "restart", "node_reset", "budget_extend",
                    "approval_granted", "spec_approved", "run_abort", "pause", "hint"):
        assert goes_on not in QUEUED_WHILE_STOPPED, goes_on


def _state(*, paused=True, finished=False, stop_requested=None, resume_pending=False):
    return SimpleNamespace(paused=paused, finished=finished, stop_requested=stop_requested,
                           resume_pending=lambda: resume_pending)


@pytest.mark.parametrize("state, holds", [
    (_state(), True),
    (_state(paused=False), False),
    (_state(finished=True), False),                  # a finished run is not paused away
    (_state(stop_requested="finalized"), False),     # a pending finalize wraps the run up
    (_state(stop_requested=""), True),               # an empty reason stops nothing (`halted`)
    (_state(resume_pending=True), False),            # a restart's owner serves the queue
])
def test_what_a_stop_holds_is_the_folds_truth_table(state, holds):
    assert stop_holds_queued_intents(state) is holds


def test_what_waits_for_the_resume_is_a_stated_rule():
    ack = {"postcondition": "engine_ack"}
    inject = {"event_type": "inject_node", **ack}
    assert waits_for_resume(inject, True) is True
    assert waits_for_resume(inject, False) is False, "a run that is not stopped starts its engine"
    assert waits_for_resume({"event_type": "node_reset", **ack}, True) is False
    assert waits_for_resume({"event_type": "budget_extend", **ack}, True) is False
    assert waits_for_resume({"event_type": "inject_node"}, True) is False, "only an ack waits"
    assert waits_for_resume(None, True) is False
    # A child THIS command launched may still be starting: its own ladder accounts for it, unless
    # the caller knows that launch is over (the server, once the lease is gone).
    launched = {**inject, "spawned_by_command": True}
    assert waits_for_resume(launched, True) is False
    assert waits_for_resume(launched, True, own_launch_over=True) is True
    assert waits_for_resume({**launched, "spawn_claim_released": True}, True) is True


def test_the_observation_reads_the_stop_off_the_fold(tmp_path):
    """The fold decides; the index's latest pause boundary only spares the fold when no `pause`
    stands. A reset that lifts a scoped auto-pause, a pending resume request, a finish and a pending
    finalize each leave no stop to hold the queue."""
    rd = _seed(tmp_path)
    _unused_client, srv = _client(tmp_path, _Driver())
    svc = srv.commands
    store = _store(rd)

    def holds():
        return svc._observe(rd).stop_holds_queued_intents()

    assert holds() is False
    store.append("pause", {"reason": "operator stop"})
    assert holds() is True
    store.append("resume", {})
    assert holds() is False
    store.append("pause", {})
    store.append("resume_requested", {})              # a pending resume request (legacy route)
    assert holds() is False
    store.append("resume", {})
    store.append("restart", {})                       # pause + resume request in one row
    assert fold(store.read_all()).paused and holds() is False
    store.append("resume", {})
    store.append("resume_served", {})
    # A scoped developer-crash auto-pause holds the queue; the reset that lifts it frees it.
    store.append("node_failed", {"node_id": 0, "generation": 0, "reason": "developer_crash",
                                 "error": "boom"})
    store.append("pause", {"node_id": 0, "generation": 0, "reason": "developer crashed"})
    assert holds() is True
    store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "implement"})
    assert fold(store.read_all()).paused is False and holds() is False
    store.append("pause", {})
    store.append("run_abort", {"reason": "finalized"})
    assert holds() is False


def test_the_observation_folds_nothing_while_no_pause_stands(tmp_path, monkeypatch):
    """The pre-filter's whole point: a running search's command is asked on every admission, and
    its growing log would otherwise cost a full fold per ask."""
    rd = _seed(tmp_path)
    _unused_client, srv = _client(tmp_path, _Driver())
    observation = srv.commands._observe(rd)
    monkeypatch.setattr(type(observation._owner), "_fold",
                        lambda *_a, **_k: pytest.fail("folded with no pause standing"))
    assert observation.stop_holds_queued_intents() is False


# ------------------------------------------------------------------------------------- end to end

def test_a_batch_queued_on_a_stopped_run_starts_nothing_until_the_resume(tmp_path):
    """THE INCIDENT, driven: eight injects and a hint onto a stopped run with no engine. Each inject
    settles at once as waiting for the resume, none starts an engine, the stop stands, the hint is
    in the log before any search runs — and the operator's own resume then starts exactly one."""
    rd = _seed(tmp_path, paused=True)
    driver = _Driver()
    client, _srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    for index in range(8):
        record = _terminal(client, _post(client, "inject_node",
                                         {"idea": {**_IDEA, "rationale": f"batch {index}"},
                                          "code": f"print({index})"}, key=f"inject-{index}").json())
        assert record["status"] == "succeeded", record
        assert record.get("deferred_until_resume") is True, record
    hint = _terminal(client, _post(client, "hint", {"text": "look at the batch"}, key="h").json())
    assert hint["status"] == "succeeded"
    assert driver.calls == [], "a queued intent started an engine on a stopped run"
    state = fold(_store(rd).read_all())
    assert state.paused, "the operator's stop was lifted"
    assert len(state.inject_requests) == 8 and state.injects_done == 0
    assert "resume" not in _types(rd)

    driver.on_spawn = lambda: (setattr(driver, "alive", True), _ack_marked(rd))
    resumed = _terminal(client, _post(client, "resume", {}, key="resume").json())
    assert resumed["status"] == "succeeded", resumed
    assert "deferred_until_resume" not in resumed
    assert len(driver.calls) == 1 and driver.calls[0][0][0] == "resume"


@pytest.mark.parametrize("event_type", sorted(_QUEUED))
def test_every_queued_intent_waits_on_a_stopped_run(tmp_path, event_type):
    rd = _seed(tmp_path, paused=True)
    driver = _Driver(on_spawn=lambda: _ack_marked(rd))
    client, _srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    record = _terminal(client, _post(client, event_type, _QUEUED[event_type]).json())
    assert record["status"] == "succeeded" and record.get("deferred_until_resume") is True, record
    assert driver.calls == []
    assert event_type in _types(rd) and fold(_store(rd).read_all()).paused


@pytest.mark.parametrize("event_type, data", [
    ("budget_extend", {"add_nodes": 1}),
    ("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"}),
])
def test_an_intent_that_asks_the_run_to_go_on_still_starts_it(tmp_path, event_type, data):
    """The control group: a reset rescores inside a resumed search and an extension is how a run
    stopped by its budget goes on — each still starts the engine it asks for."""
    rd = _seed(tmp_path, paused=True)
    driver = _Driver()
    driver.on_spawn = lambda: (setattr(driver, "alive", True), _ack_marked(rd))
    client, _srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    record = _terminal(client, _post(client, event_type, data).json())
    assert record["status"] == "succeeded" and "deferred_until_resume" not in record, record
    assert len(driver.calls) == 1


def test_a_queued_intent_on_a_running_run_starts_its_engine_as_before(tmp_path):
    """No stop, no engine (a crashed owner): the inject still starts the search that serves it."""
    rd = _seed(tmp_path)
    driver = _Driver()
    driver.on_spawn = lambda: (setattr(driver, "alive", True), _ack_marked(rd))
    client, _srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    record = _terminal(client, _post(client, "inject_node", _QUEUED["inject_node"]).json())
    assert record["status"] == "succeeded" and "deferred_until_resume" not in record, record
    assert len(driver.calls) == 1


def test_a_stopped_engine_still_finishing_does_not_hold_the_queue(tmp_path):
    """The engine is ALIVE — stopped, finishing a multi-hour evaluation, past its last loop head —
    so it will never acknowledge the intent. Left `executing`, the first inject held the run's one
    driver-command slot and the second answered 409 `command_in_progress`; both now settle at once,
    and nothing is started when that engine later exits."""
    rd = _seed(tmp_path, paused=True)
    driver = _Driver(alive=True)
    client, _srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    started = time.monotonic()
    for key in ("first", "second"):
        response = _post(client, "inject_node", _QUEUED["inject_node"], key=key)
        assert response.status_code == 200, response.text
        record = _terminal(client, response.json())
        assert record["status"] == "succeeded", record
        assert record.get("deferred_until_resume") is True, record
    assert time.monotonic() - started < 20, "a command waited out its deadline"
    driver.alive = False
    assert driver.calls == []


def test_a_pending_resume_is_not_a_standing_stop(tmp_path):
    """A `restart` pauses AND asks for a resume: its replacement owner lifts the pause and serves
    the queue, so an inject sent meanwhile rides on that launch and is acknowledged by it."""
    rd = _seed(tmp_path, paused=True)
    _store(rd).append("restart", {})
    driver = _Driver()
    driver.on_spawn = lambda: (setattr(driver, "alive", True), _ack_marked(rd))
    client, _srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    record = _terminal(client, _post(client, "inject_node", _QUEUED["inject_node"]).json())
    assert record["status"] == "succeeded" and "deferred_until_resume" not in record, record


def test_a_command_that_rode_on_a_running_engine_waits_once_the_stop_lands(tmp_path):
    """The monitor's rung: admitted while a search ran, the inject waits for its ack; the operator's
    stop lands (`looplab stop`, straight into the log) and the engine exits without acknowledging.
    The re-spawn it used to make is exactly the start that lifted the stop."""
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    client, _srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    current = _executing(client, _post(client, "inject_node", _QUEUED["inject_node"]).json())
    assert current["status"] == "executing", current
    _store(rd).append("pause", {"reason": "operator stop (`looplab stop`)"})
    driver.alive = False
    settled = _terminal(client, current)
    assert settled["status"] == "succeeded", settled
    assert settled.get("deferred_until_resume") is True, settled
    assert driver.calls == [], "the monitor started the search the stop was meant to hold"


def _executing(client, record):
    deadline = time.time() + 10
    while time.time() < deadline:
        current = client.get(f"/api/runs/demo/commands/{record['id']}").json()
        if current.get("event_seq") is not None:
            return current
        time.sleep(0.01)
    raise AssertionError(f"the intent was never appended: {current}")


def test_the_rung_reads_the_stop_again_once_no_engine_is_found(tmp_path):
    """The monitor's rung decides on the log it reads AFTER its own lock probe
    (`_settled_without_an_engine`): a stop that lands between the rung's first read and that probe
    is seen, and the plain `resume` the stale read would start is not."""
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    client, srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    svc = srv.commands
    real_state = svc._engine_state
    phase = {"now": "alive"}

    def _state(rd_):
        if phase["now"] == "gone":            # the monitor's loop head: no engine, no stop yet
            phase["now"] = "probe"
            driver.alive = False
            return False
        if phase["now"] == "probe":           # the rung's own probe: the operator's stop lands
            phase["now"] = "done"
            _store(rd).append("pause", {"reason": "operator stop (`looplab stop`)"})
            return False
        return real_state(rd_)

    svc._engine_state = _state
    sent = _executing(client, _post(client, "inject_node", _QUEUED["inject_node"]).json())
    assert sent["status"] == "executing", sent
    phase["now"] = "gone"
    settled = _terminal(client, sent)
    assert phase["now"] == "done", phase
    assert settled["status"] == "succeeded", settled
    assert settled.get("deferred_until_resume") is True, settled
    assert driver.calls == [], "the stale read started the search over the stop"


def test_a_stopped_run_needs_no_lock_verdict_to_hold_the_queue(tmp_path):
    """A queued intent on a stop is decided BEFORE the rung's lock probe: it needs no engine, so an
    unreadable lock (`None`) — which fails any command that must start one — does not fail it."""
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    client, srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    svc = srv.commands
    real_state = svc._engine_state
    phase = {"now": "alive"}

    def _state(rd_):
        if phase["now"] == "gone":            # the loop head: the engine exited after the stop
            phase["now"] = "unknown"
            driver.alive = False
            return False
        if phase["now"] == "unknown":         # the rung's probe: the lock cannot be read
            return None
        return real_state(rd_)

    svc._engine_state = _state
    sent = _executing(client, _post(client, "inject_node", _QUEUED["inject_node"]).json())
    _store(rd).append("pause", {"reason": "operator stop (`looplab stop`)"})
    phase["now"] = "gone"
    settled = _terminal(client, sent)
    assert settled["status"] == "succeeded", settled
    assert settled.get("deferred_until_resume") is True, settled


def test_a_child_this_command_launched_is_left_to_its_own_ladder(tmp_path, monkeypatch):
    """A record whose OWN `looplab resume` child may still be starting is not settled under it: the
    child serves the intent, and settling would drop the lease that keeps a second child out. Once
    that lease no longer holds, the launch is over and the intent waits for the resume."""
    rd = _seed(tmp_path, paused=True)
    _unused_client, srv = _client(tmp_path, _Driver())
    svc = srv.commands
    observation = svc._observe(rd)
    record = {"event_type": "fork", "postcondition": "engine_ack", "spawned_by_command": True}
    monkeypatch.setattr(svc, "_recent_spawn_claim", lambda _rd: True)
    assert svc._left_for_the_operators_resume(rd, record, observation) is False
    monkeypatch.setattr(svc, "_recent_spawn_claim", lambda _rd: False)
    assert svc._left_for_the_operators_resume(rd, record, observation) is True
    probed = []
    monkeypatch.setattr(svc, "_recent_spawn_claim", lambda _rd: probed.append(_rd) or True)
    assert svc._left_for_the_operators_resume(
        rd, {"event_type": "fork", "postcondition": "engine_ack"}, observation) is True
    assert probed == [], "the lease is asked about only for a record that launched a child"


# ------------------------------------------------------------------------------- the three clients

def test_the_tui_says_the_command_waits_for_the_resume():
    from looplab.serve.tui import _done_suffix

    assert _done_suffix({"status": "succeeded"}) == ""
    assert _done_suffix({"status": "noop"}) == " (already satisfied)"
    queued = _done_suffix({"status": "succeeded", "deferred_until_resume": True})
    assert "queued" in queued and "resume" in queued
    assert _done_suffix(None) == ""


def test_the_assistant_is_not_told_a_queued_intent_completed():
    from looplab.tools.run_command_adapter import _render_command_result

    plain = _render_command_result({"status": "succeeded", "id": "cmd_1"}, name="inject_node",
                                   run_id="demo", completed="inject_node for demo")
    assert plain.startswith("(completed:")
    queued = _render_command_result(
        {"status": "succeeded", "id": "cmd_1", "deferred_until_resume": True},
        name="inject_node", run_id="demo", completed="inject_node for demo")
    assert "completed" not in queued and "not yet applied" in queued
    assert "run is stopped" in queued and "resumed" in queued and "cmd_1" in queued
