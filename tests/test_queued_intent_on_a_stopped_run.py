"""A QUEUED intent sent to a STOPPED run waits for the operator's resume (doc 69 69.30).

On MiniOneRec v10 the operator stopped the run to queue eight injects and a hint for its next start.
The first inject's command started `looplab resume` — the command service's `ENSURE_RUNNING` ladder
— and the CLI's resume LIFTED the pause: the search began before the rest had arrived, and the hint
landed after the Strategist's first decision. A fork, an inject, a forced confirm or ablation, a
deep-research request and a strategy pin are served by the SEARCH, never by an engine start of their
own (`serve/protocol.py::QUEUED_WHILE_STOPPED`): on a stopped run their command records the intent,
settles `succeeded` with `deferred_until_resume`, and starts nothing — the operator's resume serves
the whole queue at once. A budget extension and the two approvals wait the same way (critic
2026-09-29: a batch holding a budget extension re-ran the incident), except a budget extension on an
external run's obligations pause — the one paused budget stop. A reset still starts the engine it
asks for.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from factories import command_terminal as _terminal  # noqa: E402
from factories import post_command as _post  # noqa: E402
from test_run_command_service import _ack_marked, _client, _Driver, _seed, _types  # noqa: E402

from looplab.core.models import Event  # noqa: E402
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.serve.control_validation import CONTROL_SPECS  # noqa: E402
from looplab.serve.protocol import (  # noqa: E402
    QUEUED_WHILE_STOPPED, EnginePolicy, stop_holds_queued_intents, waits_for_resume)
from looplab.events.types import PAUSE_REASON_EXTERNAL_OBLIGATIONS  # noqa: E402

_IDEA = {"operator": "manual", "params": {"x": 1.0}, "rationale": "queued while stopped"}
# One valid payload per queued intent, on `_seed`'s node 0 (generation 0).
_QUEUED = {
    "inject_node": {"idea": _IDEA, "code": "print(1)"},
    "fork": {"from_node_id": 0, "generation": 0},
    "force_confirm": {"node_id": 0, "generation": 0},
    "force_ablate": {"node_id": 0, "generation": 0},
    "deep_research": {},
    "set_strategy": {"strategy": {"policy": "mcts"}},
    "budget_extend": {"add_nodes": 1},
}
# …and the two approvals, which need the gate they answer to be open (`_approval_seed`).
_APPROVALS = ("approval_granted", "spec_approved")


def _store(rd):
    return EventStore(rd / "events.jsonl")


# --------------------------------------------------------------------------------- the rule itself

def test_the_queued_set_is_exactly_the_search_served_intents():
    """Pinned as a set, so widening or narrowing it is a decision the diff shows. Every member is
    an `ENSURE_RUNNING` + `engine_ack` command (its ack is what the resumed search writes); the
    intents that ASK the run to go on — the resume family and a reset — are not queued, and nor is
    anything that starts no engine anyway. MUTATION: drop `budget_extend` -> the incident again."""
    assert QUEUED_WHILE_STOPPED == set(_QUEUED) | set(_APPROVALS)
    for event_type in QUEUED_WHILE_STOPPED:
        spec = CONTROL_SPECS[event_type]
        assert (spec.engine_policy, spec.postcondition) == (
            EnginePolicy.ENSURE_RUNNING, "engine_ack"), event_type
    for goes_on in ("resume", "run_reopened", "restart", "node_reset", "run_abort", "pause",
                    "hint"):
        assert goes_on not in QUEUED_WHILE_STOPPED, goes_on


def _state(*, paused=True, finished=False, stop_requested=None, resume_pending=False):
    return SimpleNamespace(paused=paused, finished=finished, stop_requested=stop_requested,
                           resume_pending=lambda: resume_pending, last_resume_served_seq=-1)


def _request(seq, **data):
    return Event(seq=seq, ts=0.0, type="resume_requested", data=data)


_AUTO = _request(5, mode="resume", auto_resume=True)


@pytest.mark.parametrize("state, events, holds", [
    (_state(), (), True),
    (_state(paused=False), (), False),
    (_state(finished=True), (), False),                  # a finished run is not paused away
    (_state(stop_requested="finalized"), (), False),     # a pending finalize wraps the run up
    (_state(stop_requested=""), (), True),               # an empty reason stops nothing (`halted`)
    (_state(resume_pending=True), (), False),            # a restart's owner serves the queue
    (_state(resume_pending=True), (_request(5),), False),  # the operator's own pending resume
    # Review 2026-10-08: a request ONLY the server's auto-resume minted lifts no halt (the spawner
    # refuses to start it over one), so the operator's stop still holds the queue…
    (_state(resume_pending=True), (_AUTO,), True),
    (_state(resume_pending=True), (_AUTO, _request(6, launch_claim=True)), True),
    # …until the operator asks too.
    (_state(resume_pending=True), (_AUTO, _request(6, mode="resume")), False),
    (_state(resume_pending=True), (_AUTO, Event(seq=6, ts=0.0, type="restart", data={})), False),
])
def test_what_a_stop_holds_is_the_folds_truth_table(state, events, holds):
    assert stop_holds_queued_intents(state, events=events) is holds


def test_the_stop_rule_and_the_spawner_read_one_rule():
    """The spawner's refusal to start an auto-only request over a halt and the stop rule above
    must never disagree about who asked: one function, under both names."""
    from looplab.serve import engine_proc, protocol
    assert engine_proc._pending_intent_is_auto_only is protocol.pending_resume_is_auto_only


def test_what_waits_for_the_resume_is_a_stated_rule():
    ack = {"postcondition": "engine_ack"}
    inject = {"event_type": "inject_node", **ack}
    assert waits_for_resume(inject, True) is True
    assert waits_for_resume(inject, False) is False, "a run that is not stopped starts its engine"
    assert waits_for_resume({"event_type": "node_reset", **ack}, True) is False
    extend = {"event_type": "budget_extend", **ack}
    assert waits_for_resume(extend, True) is True
    assert waits_for_resume(extend, True, pause_reason="operator stop") is True
    # …but an external run's obligations pause is a budget stop, and the extension is how it goes on.
    assert waits_for_resume(extend, True, pause_reason=PAUSE_REASON_EXTERNAL_OBLIGATIONS) is False
    assert waits_for_resume(inject, True, pause_reason=PAUSE_REASON_EXTERNAL_OBLIGATIONS) is True
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


def test_a_stranded_auto_resume_request_does_not_free_the_queue(tmp_path):
    """Review 2026-10-08: the server's auto-resume minted a request (`auto_resume: true`) and its
    child died before `resume_served`, then the operator stopped the run. The request stays pending
    for good — the spawner refuses an auto-only request over a halt — and the stop rule read it as
    "already asked to resume": a queued inject was left waiting on an engine that never came, or
    spawned one that lifted the stop. It waits for the operator's resume now, and starts nothing.
    MUTATION: read `resume_pending()` alone -> the inject is not deferred."""
    rd = _seed(tmp_path)
    store = _store(rd)
    store.append("resume_requested", {"mode": "resume", "auto_resume": True})
    store.append("pause", {"reason": "operator stop"})
    state = fold(store.read_all())
    assert state.paused and state.resume_pending()
    _unused, srv = _client(tmp_path, _Driver())
    assert srv.commands._observe(rd).stop_holds_queued_intents() is True
    driver = _Driver()
    driver.on_spawn = lambda: (setattr(driver, "alive", True), _ack_marked(rd))
    client, _srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    record = _terminal(client, _post(client, "inject_node", _QUEUED["inject_node"]).json())
    assert driver.calls == [] and record.get("deferred_until_resume") is True, record


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


@pytest.mark.parametrize("event_type, data, pause", [
    ("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"}, None),
    ("budget_extend", {"add_nodes": 1}, {"reason": PAUSE_REASON_EXTERNAL_OBLIGATIONS,
                                         "terminal_reason": "budget"}),
], ids=["reset", "extension-on-the-obligations-pause"])
def test_an_intent_that_asks_the_run_to_go_on_still_starts_it(tmp_path, event_type, data, pause):
    """The control group: a reset rescores inside a resumed search, and an extension is how an
    external run its engine paused on the budget (its finish obligations due) goes on — each still
    starts the engine it asks for. MUTATION: drop the obligations exemption -> no engine."""
    rd = _seed(tmp_path, paused=pause is None)
    if pause is not None:
        _store(rd).append("pause", pause)
    driver = _Driver()
    driver.on_spawn = lambda: (setattr(driver, "alive", True), _ack_marked(rd))
    client, _srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    record = _terminal(client, _post(client, event_type, data).json())
    assert record["status"] == "succeeded" and "deferred_until_resume" not in record, record
    assert len(driver.calls) == 1


def _approval_seed(rd, event_type):
    """Open the gate `event_type` answers on node 0, as the engine does before it exits."""
    if event_type == "approval_granted":
        _store(rd).append("approval_requested", {"node_id": 0, "generation": 0, "metric": 1.0})
        return {"node_id": 0, "generation": 0}
    _store(rd).append("spec_proposed", {"spec": {"command": ["python", "eval.py"]}})
    _store(rd).append("spec_approval_requested", {})
    return {}


@pytest.mark.parametrize("event_type", _APPROVALS)
def test_an_approval_sent_to_a_stopped_run_waits_for_the_resume(tmp_path, event_type):
    """The gate an approval answers is an EXIT, not a pause: a paused run awaiting it was stopped
    by its operator, and starting `looplab resume` for the approval lifted that stop (critic
    2026-09-29). MUTATION: drop the approval from `QUEUED_WHILE_STOPPED` -> one engine start."""
    rd = _seed(tmp_path, paused=True)
    data = _approval_seed(rd, event_type)
    driver = _Driver(on_spawn=lambda: _ack_marked(rd))
    client, _srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    record = _terminal(client, _post(client, event_type, data).json())
    assert record["status"] == "succeeded" and record.get("deferred_until_resume") is True, record
    assert driver.calls == [] and fold(_store(rd).read_all()).paused


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


# ------------------------------------------------------------ critic 2026-09-29 (8d8bece8), driven

def test_a_command_admitted_before_the_stop_does_not_hold_the_slot_while_the_engine_lives(tmp_path):
    """MEDIUM: inject #1 rode a running search whose loop was busy (no loop head, so no ack); the
    operator stopped the run to batch the rest, and the engine stayed alive finishing its work. The
    rule was asked only in the monitor's `not alive` rung, so inject #1 stayed `executing` and the
    batch's next inject answered 409 `command_in_progress`. It is asked on every tick now.
    MUTATION: drop the per-tick ask (`_settled_mid_watch`) -> 409."""
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    client, _srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    first = _executing(client, _post(client, "inject_node", _QUEUED["inject_node"], key="first")
                       .json())
    assert first["status"] == "executing", first
    _store(rd).append("pause", {"reason": "operator stop (`looplab stop`)"})
    settled = _terminal(client, first)
    assert settled["status"] == "succeeded" and settled.get("deferred_until_resume") is True, settled
    second = _post(client, "inject_node", {**_QUEUED["inject_node"], "code": "print(2)"},
                   key="second")
    assert second.status_code == 200, second.text
    assert _terminal(client, second.json()).get("deferred_until_resume") is True
    driver.alive = False
    assert driver.calls == []


def _stopped_finish_reopened_by_a_reset(rd):
    store = _store(rd)
    store.append("run_abort", {"reason": "finalized"})
    store.append("run_finished", {"reason": "finalized"})
    # A reset re-opens a finished run (`_on_node_reset` clears finished + stop_requested) and leaves
    # the operator's pause alone — and it is not one of the pause boundaries.
    store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})


def test_the_prefilter_folds_when_a_reset_reopened_a_stopped_finish(tmp_path):
    """LOW-MEDIUM: the latest boundary is `run_finished`, and the pre-filter read any non-`pause`
    boundary as "no stop" while the fold says the operator's stop holds — so a queued inject there
    started the search over it. MUTATION: the old `!= EV_PAUSE` skip -> False."""
    rd = _seed(tmp_path, paused=True)
    _stopped_finish_reopened_by_a_reset(rd)
    state = fold(_store(rd).read_all())
    assert state.paused and not state.finished and not state.stop_requested
    assert stop_holds_queued_intents(state, events=_store(rd).read_all()) is True
    _unused, srv = _client(tmp_path, _Driver())
    assert srv.commands._observe(rd).stop_holds_queued_intents() is True
    driver = _Driver()
    driver.on_spawn = lambda: (setattr(driver, "alive", True), _ack_marked(rd))
    client, _srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    record = _terminal(client, _post(client, "inject_node", _QUEUED["inject_node"]).json())
    assert driver.calls == [] and record.get("deferred_until_resume") is True, record


def test_a_resumed_runs_command_pays_no_fold(tmp_path, monkeypatch):
    """The pre-filter's cost claim on a run that WAS paused: a latest `resume` clears `paused`, so
    no fold can say the stop holds. MUTATION: fold whenever any boundary exists -> the fold runs."""
    rd = _seed(tmp_path, paused=True)
    _store(rd).append("resume", {})
    _unused, srv = _client(tmp_path, _Driver())
    observation = srv.commands._observe(rd)
    monkeypatch.setattr(type(observation._owner), "_fold",
                        lambda *_a, **_k: pytest.fail("folded on a resumed run"))
    assert observation.stop_holds_queued_intents() is False


def test_a_redriven_queued_intent_needs_no_lock_verdict_at_admission(tmp_path):
    """LOW: re-driving (`/retry`) a queued intent onto a stopped run whose lock cannot be read must
    hold the queue, not fail `engine_unknown`. MUTATION: ask the lock before the stop -> failed."""
    rd = _seed(tmp_path)
    snapshot = (rd / "task.snapshot.json").read_text(encoding="utf-8")
    (rd / "task.snapshot.json").unlink()            # the spawn fails BEFORE the Popen boundary
    driver = _Driver()
    client, srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    failed = _terminal(client, _post(client, "inject_node", _QUEUED["inject_node"]).json())
    assert failed["status"] == "failed" and failed["error"]["code"] == "spawn_failed", failed
    (rd / "task.snapshot.json").write_text(snapshot, encoding="utf-8")
    _store(rd).append("pause", {"reason": "operator stop (`looplab stop`)"})
    srv.commands._engine_state = lambda _rd: None   # the lock cannot be read from here on
    retried = client.post(f"/api/runs/demo/commands/{failed['id']}/retry")
    assert retried.status_code == 200, retried.text
    settled = _terminal(client, retried.json())
    assert settled["status"] == "succeeded" and settled.get("deferred_until_resume") is True
    assert driver.calls == []


def test_stop_wait_counts_an_inject_the_server_would_drive_on_a_stopped_finished_run(tmp_path):
    """LOW: `looplab stop --wait` reads the SAME rule the server re-drives by. On a stopped FINISHED
    run the stop does not hold a queued intent (a finished run is not paused away), so the server
    starts `looplab resume` for an unacked inject — and the wait must say the stop does not stand.
    MUTATION: `stopped=True` or `stopped=bool(current().paused)` -> exit 0, "stands"."""
    from typer.testing import CliRunner

    from looplab.cli import app
    from test_stop_wait import _command_record, _run_dir

    rd = _run_dir(tmp_path, in_flight=False)
    _store(rd).append("run_finished", {"reason": "done"})
    _command_record(rd, event_type="inject_node", policy="ensure_running")
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 1, out.output
    assert "does not stand: server command(s) `inject_node`" in out.output, out.output


def test_the_rule_asks_nothing_for_a_type_that_can_never_wait(tmp_path, monkeypatch):
    """NIT: the record's TYPE first — a reset, asked on every monitor tick, pays neither the fold nor
    the lease probe (which can quarantine or unlink a lease)."""
    rd = _seed(tmp_path, paused=True)
    _unused, srv = _client(tmp_path, _Driver())
    svc = srv.commands
    observation = svc._observe(rd)
    probed, folded = [], []
    monkeypatch.setattr(svc, "_recent_spawn_claim", lambda _rd: probed.append(_rd) or True)
    real_fold = type(observation._owner)._fold
    monkeypatch.setattr(type(observation._owner), "_fold",
                        lambda self, obs: folded.append(obs) or real_fold(self, obs))
    reset = {"event_type": "node_reset", "postcondition": "engine_ack", "spawned_by_command": True}
    assert svc._left_for_the_operators_resume(rd, reset, observation) is False
    assert (len(probed), len(folded)) == (0, 0), (len(probed), len(folded))


def _tui_with(fake_api):
    from test_tui import _command_tui

    return _command_tui(fake_api)


class _QueuedApi:
    """A command API whose every command settles waiting for the resume."""

    def _record(self, event_type="inject_node"):
        return {"id": "cmd_" + "1" * 32, "status": "succeeded", "event_type": event_type,
                "deferred_until_resume": True}

    def run_command(self, run_id, event_type, data, **_kwargs):
        return self._record(event_type)

    def get_run_command(self, run_id, command_id):
        return self._record()


@pytest.mark.parametrize("site", ["control", "plan", "reconcile"])
def test_every_tui_site_says_the_intent_waits(site):
    """LOW: only `_done_suffix` was tested, so each of its three call sites could drop it.
    MUTATION: any site back on the old inline noop-only suffix -> no "queued"."""
    app = _tui_with(_QueuedApi())
    if site == "control":
        app._control("demo", "inject_node", dict(_QUEUED["inject_node"]))
    elif site == "plan":
        app._apply_plan("demo", [], [{"type": "inject_node", "data": dict(_QUEUED["inject_node"]),
                                      "label": "inject"}])
    else:
        history = [{"role": "action", "status": "pending",
                    "action": {"type": "inject_node", "data": {}, "label": "inject"},
                    "command": {"id": "cmd_" + "1" * 32, "event_type": "inject_node"}}]
        app._persist_command_status = lambda *_a, **_k: None
        assert app._reconcile_pending("demo", history) is True
    assert "queued" in app.console.file.getvalue(), app.console.file.getvalue()


def test_the_tui_gives_a_drain_s_account_in_the_web_ui_s_words():
    from looplab.serve.tui import _done_suffix

    assert "next search" in _done_suffix({"status": "succeeded", "deferred_to_next_search": True})
    assert "drain" in _done_suffix({"status": "succeeded", "served_by_drain": True})


# ------------------------------------------------------ critic 2026-09-29 (a8774): the STANDING pause
def _pause(seq, reason=PAUSE_REASON_EXTERNAL_OBLIGATIONS, **extra):
    data = {"reason": reason, **extra} if reason is not None else dict(extra)
    return Event(seq=seq, type="pause", data=data)


def test_the_obligations_pause_holds_the_stop_only_while_it_stands_alone():
    """`RunState.pause_reason` keeps the FIRST of two pauses on one lifecycle, and the latest pause
    alone is the engine's obligations row when that lands after the operator's stop (it is written by
    CAS on a log that can already hold the stop) — so the exemption folds every pause that stands.
    MUTATIONS: "the latest pause decides" -> the second order reads obligations; "the first pause
    decides" -> the first order does."""
    from looplab.serve.protocol import standing_pause_reason

    obligations = PAUSE_REASON_EXTERNAL_OBLIGATIONS
    stop_on_top = [_pause(1), _pause(2, "operator")]
    assert fold(stop_on_top).pause_reason == obligations       # the fold's first
    assert standing_pause_reason(stop_on_top) == "operator"
    engine_row_on_top = [_pause(1, "operator"), _pause(2)]
    assert standing_pause_reason(engine_row_on_top) == "operator"
    assert standing_pause_reason([_pause(1)]) == obligations
    assert standing_pause_reason([_pause(1), _pause(2)]) == obligations
    # A pause that names no reason (or a garbled one) still stands, and is not the obligations one.
    assert standing_pause_reason([_pause(1), _pause(2, None)]) == ""
    assert standing_pause_reason([_pause(1, None), _pause(2)]) == ""
    assert standing_pause_reason([_pause(1), _pause(2, 7)]) == ""
    # A resume, reopen, restart or finish ends every pause that stood; a later one starts afresh.
    for ender in ("resume", "run_reopened", "restart", "run_finished"):
        ended = [_pause(1, "operator"), Event(seq=2, type=ender, data={})]
        assert standing_pause_reason(ended) is None, ender
        assert standing_pause_reason(ended + [_pause(3)]) == obligations, ender
    # Rows that move no pause change nothing.
    assert standing_pause_reason([_pause(1), Event(seq=2, type="node_created", data={})]) == obligations
    assert standing_pause_reason([]) is None


def test_the_server_index_folds_the_same_rule_across_its_appends(tmp_path):
    """The command service reads the rule off its INCREMENTAL index, one delta per observation, so
    the reducer's state must ride from one delta to the next. MUTATION: start each delta from None
    -> the stop appended after an observed obligations pause reads as no pause at all."""
    from looplab.serve.command_observation import CommandObservationIndex

    path = tmp_path / "events.jsonl"
    store = EventStore(path)
    store.append("run_started", {"run_id": "r"})
    index = CommandObservationIndex()
    assert index.observe(path)._standing_pause is None
    store.append("pause", {"reason": PAUSE_REASON_EXTERNAL_OBLIGATIONS, "terminal_reason": "budget"})
    assert index.observe(path)._standing_pause == PAUSE_REASON_EXTERNAL_OBLIGATIONS
    store.append("pause", {"reason": "operator"})
    assert index.observe(path)._standing_pause == "operator"
    store.append("pause", {"reason": PAUSE_REASON_EXTERNAL_OBLIGATIONS, "terminal_reason": "budget"})
    assert index.observe(path)._standing_pause == "operator"
    store.append("resume", {})
    assert index.observe(path)._standing_pause is None


@pytest.mark.parametrize("first, second", [
    ({"reason": PAUSE_REASON_EXTERNAL_OBLIGATIONS, "terminal_reason": "budget"},
     {"reason": "operator"}),
    ({"reason": "operator"},
     {"reason": PAUSE_REASON_EXTERNAL_OBLIGATIONS, "terminal_reason": "budget"}),
], ids=["stop-on-the-obligations-pause", "obligations-row-after-the-stop"])
def test_an_operator_stop_beside_the_obligations_pause_holds_a_budget_extension(tmp_path, first,
                                                                                 second):
    """The operator stopped an external run on (or just before) its obligations pause: a budget
    extension must wait for their resume, not start `looplab resume` over the stop — in either
    order. MUTATIONS: read the fold's `pause_reason` again -> the first order starts an engine;
    read the latest pause alone -> the second does."""
    rd = _seed(tmp_path, paused=False)
    _store(rd).append("pause", first)
    _store(rd).append("pause", second)
    driver = _Driver(on_spawn=lambda: _ack_marked(rd))
    client, _srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    record = _terminal(client, _post(client, "budget_extend", {"add_nodes": 1}).json())
    assert record["status"] == "succeeded" and record.get("deferred_until_resume") is True, record
    assert driver.calls == []


def test_stop_wait_holds_a_stop_laid_on_the_obligations_pause(tmp_path):
    """`looplab stop` lays the operator's own pause beside an external run's obligations pause, and
    the extension the server would re-drive now waits for their resume — so the stop stands. (It
    once asserted the opposite, exit 1 "does not stand": the lifting this critic found.) MUTATION:
    pass the fold's `pause_reason` from `stop` -> exit 1."""
    from typer.testing import CliRunner

    from looplab.cli import app
    from test_stop_wait import _command_record, _run_dir

    rd = _run_dir(tmp_path, in_flight=False)
    _store(rd).append("pause", {"reason": PAUSE_REASON_EXTERNAL_OBLIGATIONS,
                                "terminal_reason": "budget"})
    _command_record(rd, event_type="budget_extend", policy="ensure_running")
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert "does not stand" not in out.output, out.output
    assert out.exit_code == 0, out.output

