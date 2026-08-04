"""The command worker's spawn phases are one implementation, not two (doc 25 SC-07).

`RunCommandService._execute` drove the whole worker state machine in one ~460-line function, and the
part that actually starts a process appeared TWICE nearly verbatim: once at admission, once in the
monitor loop after a pre-existing engine died. The copies had already drifted — the same
uncertain-boundary condition produced two different remediation strings, and a heartbeat added to the
second poll loop never reached the first. Duplicating a spawn path is the worst place for drift,
because what it guards is "never launch a second engine into the same run".

The lease-before-Popen ordering is the guarantee: if the server dies after process creation but
before it can persist the PID, another server must still wait for engine.lock instead of launching a
second engine. These tests drive the phases directly, so a break shows up as a named failure rather
than as an intermittent double-spawn under a monitor loop.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

pytest.importorskip("fastapi")

from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.serve.engine_proc import EngineSpawnOutcomeUnknown  # noqa: E402
from looplab.serve.run_commands import RunCommandService  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


def _service(root, *, spawn=None, alive=False):
    rd = root / "demo"
    rd.mkdir(parents=True, exist_ok=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    # `_spawn` refuses a run it cannot rebuild a command line for, BEFORE reaching the injected
    # spawner — without this the tests below would exercise that refusal instead of the phases.
    (rd / "task.snapshot.json").write_text(
        '{"kind":"quadratic","goal":"g","direction":"min"}', encoding="utf-8")
    srv = make_app(root).state.looplab
    srv.commands = RunCommandService(
        srv, engine_alive=lambda _rd: alive, spawn_engine=(spawn or (lambda *a, **k: 4242)),
        process_alive=lambda _pid: True, process_identity=lambda _pid: "child",
        startup_timeout=0.05, command_timeout=0.2, poll_interval=0.01,
        max_observation_timeout=0.8)
    return srv.commands, rd


COMMAND_ID = "cmd_" + "a1b2" * 8              # `_COMMAND_ID_RE`: cmd_ + 32 hex


def _record(svc, rd, command_id=COMMAND_ID):
    path = svc._path(rd, command_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"id": command_id, "status": "executing", "event_type": "pause",
              "postcondition": "paused", "deadline_at": 0.0, "updated_at": 0.0}
    svc._save(path, record)
    return path, record


# --------------------------------------------------------------------------------------------
# _spawn_under_claim — the lease/Popen ordering and the two failure taxonomies
# --------------------------------------------------------------------------------------------

def test_the_spawn_lease_is_written_before_the_process_exists(tmp_path):
    """The anti-double-spawn invariant. If the lease were written AFTER Popen, a server that died in
    that window would leave a live detached engine with no durable evidence of it, and the next
    server would launch a second one into the same run."""
    order: list[str] = []

    def spawn(*_a, **_k):
        order.append("popen")
        return 4242

    svc, rd = _service(tmp_path, spawn=spawn)
    path, record = _record(svc, rd)
    original = svc._record_spawn_claim

    def observing(run_dir, cid, pid):
        order.append(f"lease:{pid}")
        return original(run_dir, cid, pid)

    svc._record_spawn_claim = observing
    terminalized, pid = svc._spawn_under_claim(rd, path, record, COMMAND_ID, restarting=False)
    assert (terminalized, pid) == (False, 4242)
    assert order == ["lease:None", "popen", "lease:4242"], order


def test_a_certain_spawn_failure_releases_the_claim_it_took(tmp_path):
    """A launch that definitively failed created no process, so holding the lease would block every
    later attempt on evidence of a child that does not exist.

    `_spawn` treats EVERYTHING it raises after entering the Popen boundary as uncertain, so the only
    certain failure is one raised before it — here, a run whose command line cannot be rebuilt.
    """
    svc, rd = _service(tmp_path)
    (rd / "task.snapshot.json").unlink()
    path, record = _record(svc, rd)
    terminalized, pid = svc._spawn_under_claim(rd, path, record, COMMAND_ID, restarting=False)
    assert (terminalized, pid) == (True, None)
    assert not svc._recent_spawn_claim(rd), "the lease outlived a spawn that definitely did not happen"
    assert svc._load(path)["error"]["code"] == "spawn_failed"


def test_an_uncertain_spawn_failure_keeps_the_claim(tmp_path):
    """The opposite direction, and the one that matters: the boundary was crossed, so a process may
    exist. Clearing the lease here is what turns one uncertain launch into two engines."""
    def spawn(*_a, **_k):
        raise EngineSpawnOutcomeUnknown("crossed the boundary")

    svc, rd = _service(tmp_path, spawn=spawn)
    path, record = _record(svc, rd)
    terminalized, _pid = svc._spawn_under_claim(rd, path, record, COMMAND_ID, restarting=False)
    assert terminalized is True
    assert svc._recent_spawn_claim(rd), "the only anti-double-spawn evidence was thrown away"
    error = svc._load(path)["error"]
    assert error["code"] == "engine_start_uncertain" and error["retryable"] is False


@pytest.mark.parametrize("restarting,expected", [(False, "creation"), (True, "restart")])
def test_the_operator_text_is_the_only_thing_the_two_call_sites_differ_on(tmp_path, restarting,
                                                                          expected):
    """`restarting` exists so the message says what happened; everything else is shared. Before the
    split these were two hand-maintained copies whose remediation strings had already diverged."""
    def spawn(*_a, **_k):
        raise EngineSpawnOutcomeUnknown("boundary")

    svc, rd = _service(tmp_path, spawn=spawn)
    path, record = _record(svc, rd)
    svc._spawn_under_claim(rd, path, record, COMMAND_ID, restarting=restarting)
    assert expected in svc._load(path)["error"]["message"]


def test_a_certain_failure_names_start_or_restart(tmp_path):
    svc, rd = _service(tmp_path)
    (rd / "task.snapshot.json").unlink()
    path, record = _record(svc, rd)
    svc._spawn_under_claim(rd, path, record, COMMAND_ID, restarting=True)
    assert "could not restart the run engine" in svc._load(path)["error"]["message"]


# --------------------------------------------------------------------------------------------
# _try_restart_claim
# --------------------------------------------------------------------------------------------

def test_a_won_restart_claim_is_recorded_durably(tmp_path):
    svc, rd = _service(tmp_path)
    path, record = _record(svc, rd)
    svc._claim_restart_spawn = lambda _rd: True
    assert svc._try_restart_claim(rd, path, record) is True
    assert svc._load(path)["replacement_launch_claimed"] is True


def test_a_lost_restart_claim_is_not_recorded_and_does_not_terminalize(tmp_path):
    """Another worker got there first. That is a normal race, not a failure — the command keeps
    waiting for the replacement THAT worker launched."""
    svc, rd = _service(tmp_path)
    path, record = _record(svc, rd)
    svc._claim_restart_spawn = lambda _rd: False
    assert svc._try_restart_claim(rd, path, record) is True
    assert "replacement_launch_claimed" not in svc._load(path)


def test_an_uncertain_restart_claim_tells_the_operator_not_to_resubmit(tmp_path):
    svc, rd = _service(tmp_path)
    path, record = _record(svc, rd)

    def _boom(_rd):
        raise EngineSpawnOutcomeUnknown("boundary")

    svc._claim_restart_spawn = _boom
    assert svc._try_restart_claim(rd, path, record) is False
    error = svc._load(path)["error"]
    assert error["code"] == "resume_start_uncertain" and error["retryable"] is False
    assert "do not submit another restart" in error["remediation"]


# --------------------------------------------------------------------------------------------
# Structural: the copies must not come back
# --------------------------------------------------------------------------------------------

def _body(fn):
    return ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]


def _self_calls(fn, name):
    return [node for node in ast.walk(_body(fn))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == name and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"]


@pytest.mark.parametrize("callee", ["_spawn", "_claim_restart_spawn"])
def test_execute_no_longer_starts_a_process_itself(callee):
    """Both process-starting calls belong to the phase helpers. A call reappearing inside `_execute`
    means a third copy of the lease/Popen sequence, which is how the first two drifted."""
    assert _self_calls(RunCommandService._execute, callee) == [], (
        f"`_execute` calls self.{callee} directly again")


@pytest.mark.parametrize("helper,callee,count", [
    (RunCommandService._spawn_under_claim, "_spawn", 1),
    (RunCommandService._spawn_under_claim, "_record_spawn_claim", 2),   # lease, then the pid
    (RunCommandService._try_restart_claim, "_claim_restart_spawn", 1),
])
def test_each_phase_owns_exactly_one_spawn_sequence(helper, callee, count):
    assert len(_self_calls(helper, callee)) == count


def test_both_spawn_call_sites_go_through_the_shared_helper():
    """Two, not one: the admission spawn and the monitor's re-spawn after a pre-existing engine died.
    Losing one means that path grew its own copy again."""
    assert len(_self_calls(RunCommandService._execute, "_spawn_under_claim")) == 2
    assert len(_self_calls(RunCommandService._execute, "_try_restart_claim")) == 2


def test_execute_stays_below_the_size_that_made_it_unreviewable():
    fn = _body(RunCommandService._execute)
    assert (fn.end_lineno - fn.lineno + 1) < 420, (
        "`_execute` is drifting back toward the ~460-line state machine the finding measured")
