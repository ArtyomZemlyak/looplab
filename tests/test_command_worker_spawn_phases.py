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
@pytest.mark.parametrize("phase", [RunCommandService._execute, RunCommandService._admit,
                                   RunCommandService._monitor])
def test_no_phase_starts_a_process_itself(phase, callee):
    """Both process-starting calls belong to the two helpers. A call reappearing in any PHASE means a
    third copy of the lease/Popen sequence, which is how the first two drifted."""
    assert _self_calls(phase, callee) == [], (
        f"`{phase.__name__}` calls self.{callee} directly again")


@pytest.mark.parametrize("helper,callee,count", [
    (RunCommandService._spawn_under_claim, "_spawn", 1),
    (RunCommandService._spawn_under_claim, "_record_spawn_claim", 2),   # lease, then the pid
    (RunCommandService._try_restart_claim, "_claim_restart_spawn", 1),
])
def test_each_phase_owns_exactly_one_spawn_sequence(helper, callee, count):
    assert len(_self_calls(helper, callee)) == count


def test_both_spawn_call_sites_go_through_the_shared_helper():
    """One per PHASE: the admission spawn and the monitor's re-spawn after a pre-existing engine
    died. Losing one means that path grew its own copy again."""
    for helper in ("_spawn_under_claim", "_try_restart_claim"):
        assert len(_self_calls(RunCommandService._admit, helper)) == 1, helper
        assert len(_self_calls(RunCommandService._monitor, helper)) == 1, helper


@pytest.mark.parametrize("phase,ceiling", [
    (RunCommandService._execute, 40),      # the spine: short-circuit, admit, monitor
    (RunCommandService._admit, 250),
    (RunCommandService._monitor, 170),
])
def test_no_phase_grows_back_into_the_whole_state_machine(phase, ceiling):
    """The finding measured ONE ~460-line function holding the lock scope, the spawn ladder and the
    deadline slide at once. Three ceilings, so re-merging them is a red test rather than a slow
    drift back."""
    fn = _body(phase)
    assert (fn.end_lineno - fn.lineno + 1) < ceiling, phase.__name__


def test_the_sequencer_is_held_by_the_admission_phase_alone():
    """`_admit` owns the `with self.sequence(rd)` for its whole body, so every early return releases
    it. `_execute` used to hand-roll `__enter__`/`__exit__` with a `sequence_held` flag its `finally`
    had to re-check — one more thing to get right in a 460-line function."""
    admit = _body(RunCommandService._admit)
    body = [n for n in admit.body if not isinstance(n, ast.Expr)]   # drop the docstring
    assert len(body) == 1 and isinstance(body[0], ast.With), (
        "the admission body is no longer ONE lock scope")

    spine = textwrap.dedent(inspect.getsource(RunCommandService._execute))
    assert "__enter__" not in spine and "sequence_held" not in spine


# --- the admission phase's return contract ---------------------------------------------------
#
# `_execute` unpacks `spec, record = self._admit(...)`. That makes every exit from the admission
# phase part of a two-value CONTRACT, and the split introduced one exit that forgot it (below).

def test_every_admission_exit_returns_the_two_value_contract():
    """A bare `return` inside `_admit` does not end the command — it hands `None` to the caller's
    tuple unpacking, which raises TypeError, which the spine's catch-all turns into
    `command_worker_failed` written OVER the terminal status that exit had just recorded. The
    ENSURE_RUNNING success inside the startup poll loop shipped that way: a spawned engine that
    acked fast enough reported a succeeded command as failed."""
    for node in ast.walk(_body(RunCommandService._admit)):
        if isinstance(node, ast.Return):
            shape = "bare" if node.value is None else ast.dump(node.value)[:40]
            assert isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2, (
                f"`_admit` line {node.lineno} returns {shape}, not (spec, record)")


def _ensure_running_record(svc, rd, command_id=COMMAND_ID):
    """The route's own pre-admission record for an ENSURE_RUNNING command.

    Deliberately carries NO `event_seq`: `_admit` is what appends the marked intent, and the spawn
    ladder under test runs only on that first pass. `deadline_at` must be in the future or the
    startup poll loop below is skipped entirely and the `else` clause declares a slow start."""
    import time

    path = svc._path(rd, command_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"id": command_id, "status": "executing", "event_type": "set_strategy",
              "postcondition": "engine_ack", "data": {"strategy": {"policy": "mcts"}},
              "deadline_at": time.time() + 5.0, "updated_at": time.time()}
    svc._save(path, record)
    return path, record


def _acking_service(tmp_path):
    """A service whose spawned "engine" folds the intent and acks IMMEDIATELY — the fast local case,
    and the only way to reach admission's ENSURE_RUNNING success exit inside the startup window."""
    seen: dict = {}
    calls: list = []

    def _spawn(_args, **_kwargs):
        calls.append(_args)
        store = EventStore(seen["rd"] / "events.jsonl")
        intent = next(event for event in reversed(store.read_all())
                      if (event.data or {}).get("_command_id"))
        store.append("command_ack", {"command_id": (intent.data or {})["_command_id"],
                                     "event_seq": intent.seq})
        return 4242

    svc, rd = _service(tmp_path, spawn=_spawn, alive=False)
    seen["rd"] = rd
    return svc, rd, calls


def test_admission_returns_the_pair_when_its_spawned_engine_acks_at_once(tmp_path):
    """Driven at the phase boundary, because that is where the contract lives: the caller UNPACKS
    this. A bare `return` raises TypeError right here, before any record has been re-read."""
    import time

    svc, rd, calls = _acking_service(tmp_path)
    path, record = _ensure_running_record(svc, rd)
    record["absolute_deadline_at"] = time.time() + svc.max_observation_timeout

    spec, admitted = svc._admit(rd, path, dict(record), COMMAND_ID)

    assert spec is None, "admission terminalized, so there is nothing left for the monitor to watch"
    assert admitted is not None
    # The DURABLE status, not `admitted["status"]`: `_terminal` writes a copy and deliberately leaves
    # the caller's dict alone, so every terminalizing exit returns a record still reading "executing".
    assert svc._load(path)["status"] == "succeeded", svc._load(path)
    assert len(calls) == 1, calls


def test_admission_spawn_that_acks_inside_the_startup_window_succeeds(tmp_path):
    """And the same case through the whole spine: the durable record an operator polls must read
    `succeeded`, not the worker's own crash report written over it."""
    svc, rd, calls = _acking_service(tmp_path)
    path, record = _ensure_running_record(svc, rd)

    svc._execute(rd, path, dict(record), claimed=False)

    final = svc._load(path)
    assert final["status"] == "succeeded", final
    assert final["error"] is None, final
    assert len(calls) == 1, calls


def test_a_worker_crash_never_overwrites_the_durable_terminal_status(tmp_path):
    """The spine's catch-all exists to make a worker crash OBSERVABLE, not to re-decide an outcome
    that is already durable. It holds the pre-admission copy of the record, so writing it blind
    demotes a command whose effect actually landed."""
    svc, rd = _service(tmp_path)
    path, record = _record(svc, rd)

    def _admit_then_crash(_rd, _path, current, _command_id):
        svc._succeeded(_rd, _path, current)
        raise RuntimeError("boom")

    svc._admit = _admit_then_crash
    svc._execute(rd, path, dict(record), claimed=False)

    final = svc._load(path)
    assert final["status"] == "succeeded", final
    assert final["error"] is None, final


def test_a_worker_crash_reports_failure_against_the_admitted_record(tmp_path):
    """And when the crash IS the outcome, it must be recorded on what admission persisted.
    `event_seq`/`baseline_seq` are how `/retry` and reconciliation find the marked intent; a
    failure record that dropped them reads as a command whose intent was never appended, which is
    the one state the operator is told never to retry automatically."""
    svc, rd = _service(tmp_path)
    path, record = _record(svc, rd)

    def _admit_then_crash(_rd, _path, current, _command_id):
        svc._save(_path, {**current, "event_seq": 7, "baseline_seq": 3})
        raise RuntimeError("boom")

    svc._admit = _admit_then_crash
    svc._execute(rd, path, dict(record), claimed=False)

    final = svc._load(path)
    assert final["status"] == "failed", final
    assert final["error"]["code"] == "command_worker_failed", final
    assert (final.get("event_seq"), final.get("baseline_seq")) == (7, 3), final
