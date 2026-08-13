"""THE CONTROL PLANE MUST NEVER WEDGE — «Пульт управления никогда не должен залипать никаким образом».

The 2026-08-11 incident on `rubertlite-dr-unified-v2` is the reason this file exists: after 30 hours,
8 failed nodes, 0 evaluated and 2,323 provider calls, `kill` on the engine PID was the only way to
stop the run. Three faults compounded into a closed loop with no exit:

  1. `pause`'s postcondition was `paused_and_stopped`, which requires the engine PROCESS to exit — a
     pause cannot deliver that while a multi-hour evaluation is in flight, so on exactly the runs
     where pausing matters it could only ever time out;
  2. `reject_if_active` then refused EVERY later control with `command_retry_required` (fixed
     2026-08-12 by `_intent_spent`; `tests/test_command_intent_spent.py` holds that half);
  3. the documented remedy `POST /commands/{id}/retry` re-drove the ORIGINAL command, whose
     `event_seq` pointed at an intent a later `resume` had consumed, so it re-observed a superseded
     event and never appended a new `pause`.

Re-measured against master on 2026-08-13, faults 1 and 3 were still live, and the loop was tighter
than the backlog recorded: a FRESH `pause` (new idempotency key) was refused 409
`retry_existing_command` naming the spent command, whose `/retry` could only time out again. Both
doors, closed, pointing at each other.

WHAT THIS FILE PROVES, and it is deliberately stronger than "those three are fixed":

  * `test_the_recorded_incident_now_has_a_way_out` replays the exact recorded sequence;
  * `test_a_legacy_timed_out_pause_record_can_be_retried_into_a_fresh_intent` covers the UPGRADE
     path — a run wedged under the old build has a durable `paused_and_stopped` record on disk that
     is never rewritten, so the fix has to reach it too;
  * `test_no_reachable_control_state_is_absorbing` is the general property, and the only one of the
     three that can catch the NEXT wedge: a breadth-first search over real commands driven against
     real runs, which at EVERY reachable state follows each control to the remedy its refusal NAMES
     and asserts the control comes back. It found a wedge this change had not yet closed — a run
     carrying the durable record a pre-2026-08-13 server left behind could not be paused at all —
     which is why `_postcondition` reads the legacy spelling under the new rule and
     `_unresolved_equivalent` reconciles before it blocks.

The state machine it searches is written down in `_ALPHABET`, `_ADMITTED`/`_MUST_SUCCEED` and the
remedy table below.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException

from looplab.events.eventstore import EventStore
from looplab.events.types import (
    EV_COMMAND_ACK, EV_HINT, EV_PAUSE, EV_RESUME, EV_RUN_ABORT, EV_RUN_FINISHED, EV_RUN_STARTED,
    EV_SET_STRATEGY)


# ------------------------------------------------------------ the universe the search runs inside
class _World:
    """ONE control plane, one runs root, many runs — everything the search needs.

    A run's engine is reduced to the two behaviours the command plane can observe: it holds
    `engine.lock` (here, a per-run flag) and it ACKNOWLEDGES every marked control intent it folds.
    The second is the real `Engine._drain_command_acks` (`engine/orchestrator.py`), which reads
    `_command_id` off each new event and appends `command_ack {command_id, event_seq}` — reproducing
    exactly that is what lets an `engine_ack` command (resume, budget extend, set_strategy, …)
    succeed here for the same reason it does in production, rather than by being special-cased.

    One app and one acknowledging thread for the WHOLE search, deliberately: a per-state `make_app`
    dominated the runtime and bought nothing, and a control plane serving many runs at once is the
    shipped shape anyway.
    """

    def __init__(self, root: Path):
        from looplab.serve.run_commands import RunCommandService
        from looplab.serve.server import make_app

        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.alive: dict[str, bool] = {}
        # An engine can be ALIVE and yet acknowledge nothing — it is inside a multi-hour evaluation.
        # That is the incident's engine, and it is the only way the search can reach a state with an
        # unresolved durable intent in it, so it is a separate flag rather than a synonym for dead.
        self.acking: dict[str, bool] = {}
        srv = make_app(self.root).state.looplab
        srv.commands = RunCommandService(
            srv, engine_alive=self._is_alive, spawn_engine=self._spawn,
            startup_timeout=0.05, command_timeout=0.12, poll_interval=0.01,
            max_observation_timeout=0.2)
        self.srv = srv
        self.commands = srv.commands
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._ack_loop, daemon=True, name="fake-engines")
        self._thread.start()

    # --- the probes RunCommandService is constructed with
    def _is_alive(self, rd) -> bool:
        return bool(self.alive.get(Path(rd).name))

    def _spawn(self, _args, *, run_dir=None, **_kwargs):
        # An ensure-running command legitimately launches a driver. Model the whole startup: the
        # process comes up, takes the lock, and begins acknowledging.
        self.alive[Path(run_dir).name] = True
        return 4242

    # --- runs
    def seed(self, name: str, *, alive: bool) -> Path:
        rd = self.root / name
        rd.mkdir(parents=True)
        EventStore(rd / "events.jsonl").append(
            EV_RUN_STARTED, {"run_id": name, "goal": "g", "direction": "min"})
        (rd / "task.snapshot.json").write_text(
            '{"kind":"quadratic","goal":"g","direction":"min"}', encoding="utf-8")
        self.alive[name] = bool(alive)
        self.acking[name] = True
        return rd

    def clone(self, rd: Path, name: str) -> Path:
        target = self.root / name
        shutil.copytree(rd, target)
        self.alive[name] = self.alive.get(rd.name, False)
        self.acking[name] = self.acking.get(rd.name, True)
        # The copy is the same DURABLE state seen by a fresh server process — which is exactly what
        # a UI restart is. Execution/activity claims name a worker of the ORIGINAL run and no thread
        # is driving the copy, so carrying them over would model a live owner that does not exist and
        # would stop the copy's own recovery from ever claiming it.
        for claim in (target / ".commands").glob(".*"):
            claim.unlink()
        return target

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _ack_loop(self) -> None:
        while not self._stop.wait(0.01):
            for name, alive in list(self.alive.items()):
                if not alive or not self.acking.get(name, True):
                    continue
                path = self.root / name / "events.jsonl"
                if not path.exists():
                    continue
                store = EventStore(path)
                try:
                    events = store.read_all()
                except OSError:
                    continue
                acked = {(str((event.data or {}).get("command_id")),
                          (event.data or {}).get("event_seq"))
                         for event in events if event.type == EV_COMMAND_ACK}
                for event in events:
                    marker = (event.data or {}).get("_command_id")
                    if not marker or (str(marker), event.seq) in acked:
                        continue
                    try:
                        store.append(EV_COMMAND_ACK,
                                     {"command_id": str(marker), "event_seq": event.seq})
                    except Exception:  # noqa: BLE001 — the engine's own append is best-effort here
                        pass
                # …and it SERVES a stop: a real engine answers `run_abort` by finishing the run and
                # releasing its lock. Without this half every finalize state in the search would be
                # an artefact of an engine that never finishes anything, and `finalize_in_progress`
                # would read as an absorbing state of LoopLab rather than of the stub.
                if any(event.type == EV_RUN_ABORT for event in events) and not any(
                        event.type == EV_RUN_FINISHED for event in events):
                    try:
                        store.append(EV_RUN_FINISHED, {"reason": "operator"})
                        self.alive[name] = False
                    except Exception:  # noqa: BLE001
                        pass


_TERMINAL = frozenset({"succeeded", "noop", "failed", "rejected", "timed_out"})
_SUCCESS = frozenset({"succeeded", "noop"})


def _settle(commands, rd: Path, record: dict, budget: float = 20.0) -> dict:
    """Poll a submitted command to a terminal status, exactly as every client does."""
    deadline = time.time() + budget
    while record.get("status") not in _TERMINAL and time.time() < deadline:
        time.sleep(0.02)
        record = commands.get(rd, record["id"])
    assert record.get("status") in _TERMINAL, record
    return record


def _settle_active(commands, rd: Path, budget: float = 20.0) -> str:
    """Observe whatever command is in flight to a terminal status, and PROVE that it gets there.

    This is the operator's own recourse when a control is refused `command_in_progress`, so the
    liveness property may lean on it — and it may lean on it only because the monitor's deadlines are
    finite. A record that never terminalizes is itself the wedge (`PAUSE_OBSERVATION_MAX_EXTENSIONS`
    exists because one re-armed its own absolute deadline every pass), so failing to settle fails the
    test rather than being skipped over.
    """
    _path, active = commands._active_record(rd)
    if active is None:
        return ""
    command_id = str(active.get("id") or "")
    deadline = time.time() + budget
    record = active
    while record.get("status") not in _TERMINAL and time.time() < deadline:
        time.sleep(0.02)
        record = commands.get(rd, command_id)
    assert record.get("status") in _TERMINAL, (
        f"{command_id} never reached a terminal status: {record}. An `executing` record refuses "
        f"every other control with 409, so an unbounded one IS the frozen control plane.")
    return command_id


_KEY = [0]


def _drive(commands, rd: Path, event_type: str, data=None) -> dict:
    """Submit one control the way an operator does — fresh idempotency key — and settle it.

    Returns the durable record, or a synthetic `{"status": "refused", ...}` for an HTTP refusal, so
    the caller can treat "the door was closed" and "the command ran and failed" uniformly.
    """
    _KEY[0] += 1
    try:
        record = commands.submit(
            rd, f"liveness-key-{_KEY[0]}", event_type, data or {},
            expected_generation=commands.run_generation(rd))
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": str(exc.detail)}
        return {"status": "refused", "code": str(detail.get("code") or ""),
                "detail": detail, "http_status": exc.status_code}
    return _settle(commands, rd, record)


# ------------------------------------------------------------------- THE COMMAND STATE MACHINE
#
# One durable command record walks:
#
#     accepted -> executing -> { succeeded | noop | failed | rejected | timed_out }
#                                 \___ retryable failed/timed_out --(/retry)--> accepted
#
# `succeeded`/`noop`/`rejected`/non-retryable `failed` are terminal and BLOCK NOTHING. The states
# that can hold the plane are the ones a later command is refused BECAUSE of:
#
#   `command_in_progress`      an accepted/executing record   -> GET it; the monitor's deadlines are
#                                                                 bounded, and GET re-drives a record
#                                                                 whose worker died
#   `command_retry_required`   a retryable terminal whose durable intent is INTACT and still in force
#                                                              -> POST its /retry
#   `retry_existing_command`   an identical unresolved intent  -> the same /retry
#   `engine_start_uncertain`   an unresolved Popen lease       -> wait for the lock/child exit, or
#                                                                 POST /api/start/{id}/resolve-claim
#   `finalize_in_progress`     incomplete terminal projections -> submit `run_abort`, which ATTACHES
#                                                                 to the pending finalize and drives it
#   `run_generation_changed`   the log was reset/replaced      -> refresh and form a new command
#
# EVERY one of those names a move that is still available. The wedge was the pair
# `retry_existing_command` + a /retry that could not succeed: two refusals pointing at each other.
# The property below is that no such pair exists anywhere in the reachable state space.

# What an operator can do, as the search's alphabet. `engine_dies`/`engine_returns` are the
# ENVIRONMENT moves — the incident's decisive transition (the operator resumed out-of-band while the
# engine kept running) is `legacy_resume`, an append made by the CLI, not through the command plane.
_ALPHABET = ("pause", "resume", "hint", "steer", "finalize", "legacy_resume", "legacy_wedge",
             "engine_dies", "engine_returns", "engine_stalls")

# The two halves of the liveness property, stated separately because they are different claims.
#
# ADMISSION liveness — every one of these must REACH ITS OWN DURABLE RECORD, given the named-remedy
# hops AND a HEALTHY ENGINE. The engine clause is what keeps the property honest in both directions:
#
#   * a control blocked behind an intent the engine has simply not got to yet is NOT wedged — the
#     operator's request is durably recorded and will be served, and refusing a duplicate is the
#     anti-double-apply rule working. The search reaches that state (a live engine that acknowledges
#     nothing, three hours into an evaluation) and it must not read as a defect;
#   * a control that STILL cannot be issued once the engine is healthy is exactly the incident. On
#     `rubertlite-dr-unified-v2` the engine was alive and working the whole time, and `pause` could
#     not be issued at all — because the door named a `/retry` that pointed at a consumed event.
#
# So: heal the engine, and control must come back. Nothing else is a fair excuse.
_ADMITTED = ("pause", "resume", "hint", "steer", "finalize")

# EFFECT liveness — `pause` must actually SUCCEED, with no engine cooperation of any kind, in every
# state where the plane's own decision is to append it (`_decide_pause`: a run that is neither
# finished nor finalizing). Its postcondition is a fold of the operator's own intent, so a stalled or
# dead engine is no excuse — and requiring the engine PROCESS to exit first, which `paused_and_stopped`
# did, is what made a pause unobservable on exactly the runs where pausing matters.
_MUST_SUCCEED = ("pause",)

_LEGACY_WEDGE_ID = "cmd_" + "d1ce" * 8


def _plant_legacy_pause_record(world: _World, rd: Path) -> None:
    """Leave behind exactly what a pre-2026-08-13 server left on `rubertlite-dr-unified-v2`.

    A durable command record is NEVER rewritten, so upgrading the server does not clear the wedge —
    every run that was stuck keeps its `paused_and_stopped` record, pointing at a real pause event,
    marked retryable. That is a reachable state of the shipped system on the day this lands, so the
    search has to walk it like any other, not merely a fixture in one regression test.
    """
    # ONE fixed id for the whole search, so planting the wedge again on a descendant is a no-op
    # rather than a second, third, … record. Stacking copies of the same historical defect only grows
    # the frontier; what has to be walked is the state, and one wedged record IS the state.
    command_id = _LEGACY_WEDGE_ID
    path = rd / ".commands" / f"{command_id}.json"
    if path.exists():
        return
    intent = EventStore(rd / "events.jsonl").append(EV_PAUSE, {"_command_id": command_id})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": command_id, "event_type": EV_PAUSE, "event_seq": intent.seq,
        "baseline_seq": intent.seq - 1, "data": {},
        "postcondition": "paused_and_stopped", "status": "timed_out",
        "created_at": 1.0, "updated_at": 2.0, "engine_policy": "no_spawn",
        "run_generation": world.commands.run_generation(rd),
        "error": {"code": "postcondition_timeout", "retryable": True,
                  "message": "paused_and_stopped was not observed in time"},
    }))


_EVENT_FOR = {"pause": EV_PAUSE, "resume": EV_RESUME, "hint": EV_HINT,
              "steer": EV_SET_STRATEGY, "finalize": EV_RUN_ABORT}
_DATA_FOR = {"hint": {"text": "try a smaller lr"},
             "steer": {"strategy": {"policy": "mcts"}},
             "finalize": {"reason": "operator"}}


def _apply(world: _World, rd: Path, action: str) -> None:
    if action == "engine_dies":
        world.alive[rd.name] = False
    elif action == "engine_returns":
        world.alive[rd.name] = True
        world.acking[rd.name] = True
    elif action == "engine_stalls":
        # Alive, holding its lock, acknowledging nothing: the engine of 2026-08-11, three hours into
        # an evaluation. Every `engine_ack` command submitted here will time out with its intent
        # durable — which is how the search reaches the states that used to wedge.
        world.acking[rd.name] = False
    elif action == "legacy_wedge":
        _plant_legacy_pause_record(world, rd)
    elif action == "legacy_resume":
        # The out-of-band resume of the incident: the CLI/engine appends it, no command record.
        EventStore(rd / "events.jsonl").append(EV_RESUME, {})
    elif action in _EVENT_FOR:
        _drive(world.commands, rd, _EVENT_FOR[action], _DATA_FOR.get(action, {}))
    else:  # pragma: no cover - the alphabet is closed
        raise AssertionError(f"unknown action {action!r}")


def _signature(world: _World, rd: Path) -> tuple:
    """Everything about a run that can decide whether the NEXT control is admitted.

    Not a full state hash — deliberately. Search over trails does not terminate (every command adds a
    record, so no two trails are ever equal), and it also re-tests the same situation dozens of times.
    What actually determines admission is a small tuple: engine liveness, the two folded lifecycle
    flags, an unresolved spawn lease, and — for the records that are NOT settled history — the exact
    facts `_active_record` / `_unresolved_terminal_record` / `_unresolved_equivalent` read. Succeeded,
    noop, rejected and non-retryable-failed records collapse away because they block nothing, which
    is what makes the space finite.
    """
    state = world.commands.srv.state(rd)
    blocking = []
    for _path, record in world.commands._scan_command_records(rd, on_symlink="refuse"):
        if record is None:
            blocking.append("<unreadable>")
            continue
        status = record.get("status")
        retryable = bool((record.get("error") or {}).get("retryable"))
        if status in {"succeeded", "noop", "rejected"} or (status == "failed" and not retryable):
            continue
        blocking.append(repr((
            record.get("event_type"), status, retryable,
            record.get("event_seq") is not None,
            world.commands._intent_spent(rd, record),
        )))
    return (bool(world.alive.get(rd.name)), bool(world.acking.get(rd.name, True)),
            bool(state.paused), bool(state.finished), bool(state.stop_requested),
            world.commands._spawn_claim_path(rd).exists(), tuple(sorted(blocking)))


# THE REMEDY TABLE. Every refusal the command plane can answer a control with, and the move it
# NAMES in its own `remediation`. The property below is that following the named move gets somewhere:
# a refusal whose remedy leads back to the same refusal is an absorbing state, and that exact pair —
# `retry_existing_command` naming a `/retry` that could only time out — is what cost 30 hours.
#
#   command_in_progress      -> observe the named command to a terminal status
#   command_retry_required   -> POST the named command's /retry
#   retry_existing_command   -> POST the named command's /retry
#   command_intent_spent     -> submit a NEW command (the spent record no longer blocks admission)
#   finalize_in_progress     -> let the pending finalize complete, then act
#   engine_finishing         -> wait for the engine to release its lock
#   engine_start_uncertain   -> wait for the lock or definitive child exit, or resolve the claim
#   run_generation_changed   -> refresh and form a new command against the current generation
_SETTLE_NAMED = frozenset({"command_in_progress"})
_RETRY_NAMED = frozenset({"command_retry_required", "retry_existing_command"})
_WAIT_OUT = frozenset({"finalize_in_progress", "engine_finishing", "engine_start_uncertain",
                       "command_intent_spent", "postcondition_timeout"})


def _wait_for_quiet(commands, rd: Path, budget: float = 10.0) -> None:
    """Wait out a pending finalize / in-flight command, bounded. Never asserts: this is a REMEDY
    hop, and whether it was enough is decided by the re-submission that follows it."""
    deadline = time.time() + budget
    while time.time() < deadline:
        try:
            if not commands._finalize_incomplete(rd) and commands._active_record(rd)[1] is None:
                return
        except HTTPException:
            return
        time.sleep(0.05)


def _path_forward(world: _World, rd: Path, action: str, tag: str) -> tuple[str, str, list[str]]:
    """Take a control as far as its own refusals allow, following each named remedy.

    Returns `(status without engine help, status after the engine is healthy, trail)`.
    `refused/<code>` means the control never reached a durable record at all.
    """
    probe_rd = world.clone(rd, f"probe-{tag}")
    commands = world.commands
    trail: list[str] = []
    record: dict = {}
    status = ""
    for _hop in range(3):
        record = _drive(commands, probe_rd, _EVENT_FOR[action], _DATA_FOR.get(action, {}))
        code = str(record.get("code") or (record.get("error") or {}).get("code") or "")
        status = str(record.get("status"))
        trail.append(f"{status}/{code}" if code else status)
        if status in _SUCCESS:
            return status, status, trail
        named = str((record.get("detail") or {}).get("existing_command_id") or "")
        if status == "refused" and code in _SETTLE_NAMED:
            _settle_active(commands, probe_rd)
            continue
        if status == "refused" and code in _RETRY_NAMED and named:
            try:
                _settle(commands, probe_rd, commands.retry(probe_rd, named))
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                trail.append(f"retry({named[:12]})->{detail.get('code')}")
                # A remedy may itself refuse — legitimately, if IT names the next move. The one that
                # does is `command_intent_spent`, whose answer is "submit a new command", which is
                # precisely what the next hop does.
                if str(detail.get("code")) not in _WAIT_OUT:
                    break
            continue
        if status == "refused" and code in _WAIT_OUT:
            _wait_for_quiet(commands, probe_rd)
            continue
        if status in {"failed", "timed_out"} and bool((record.get("error") or {}).get("retryable")):
            try:
                record = _settle(commands, probe_rd, commands.retry(probe_rd, str(record["id"])))
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                trail.append(f"retry(self)->{detail.get('code')}")
                if str(detail.get("code")) not in _WAIT_OUT:
                    status = str(record.get("status"))
                    break
                continue
            status = str(record.get("status"))
            trail.append(status)
            break
        break

    # The final hop: give the run a HEALTHY engine and ask once more. See `_ADMITTED`.
    world.alive[probe_rd.name] = True
    world.acking[probe_rd.name] = True
    _wait_for_quiet(commands, probe_rd)
    healed = _drive(commands, probe_rd, _EVENT_FOR[action], _DATA_FOR.get(action, {}))
    healed_code = str(healed.get("code") or (healed.get("error") or {}).get("code") or "")
    healed_status = str(healed.get("status"))
    if healed_status == "refused" and healed_code in _RETRY_NAMED:
        named = str((healed.get("detail") or {}).get("existing_command_id") or "")
        if named:
            try:
                healed = _settle(commands, probe_rd, commands.retry(probe_rd, named))
                healed_status = str(healed.get("status"))
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                healed_status = f"refused/{detail.get('code')}"
    trail.append(f"healed:{healed_status}"
                 + (f"/{healed_code}" if healed_status == "refused" else ""))
    return status or str(record.get("status")), healed_status, trail


# Bounds on the search. It terminates on the signature frontier long before these bite; they exist so
# a future alphabet cannot silently turn a guard test into a multi-hour job.
_MAX_STATES = 48
_MAX_DEPTH = 2


def test_no_reachable_control_state_is_absorbing(tmp_path):
    """From EVERY reachable state, every control is still ISSUABLE and the server-deliverable ones
    still WORK.

    This is the operator's requirement stated as a property — «Пульт управления никогда не должен
    залипать никаким образом» — rather than three regression cases. Every edge is a real
    `submit`/`get`/`retry` against a real event log and the real `RunCommandService`; every probe runs
    on a real COPY of the run, so the search never measures a state it has disturbed; and every
    refusal is followed to the move it NAMES, because a refusal that names a move nobody can take is
    the whole defect.

    The frontier is keyed on `_signature`, i.e. on what can actually refuse the next command, so the
    search closes instead of running forever over ever-longer command histories.
    """
    world = _World(tmp_path / "runs")
    try:
        frontier = []
        for index, alive in enumerate((True, False)):
            rd = world.seed(f"root{index}", alive=alive)
            frontier.append((rd, 0))
        seen = {_signature(world, rd) for rd, _depth in frontier}
        checked = 0
        while frontier and checked < _MAX_STATES:
            rd, depth = frontier.pop(0)
            signature = _signature(world, rd)
            state = world.commands.srv.state(rd)
            for action in _ADMITTED:
                status, healed, trail = _path_forward(
                    world, rd, action, f"{rd.name}-{checked}-{action}")
                assert not healed.startswith("refused"), (
                    f"{action} CANNOT BE ISSUED from {rd.name} {signature} even with a healthy "
                    f"engine: {trail}. Every refusal must name a move that leads somewhere; this "
                    f"one leads back to itself.")
                # `pause` must also LAND, without the engine's help, wherever the plane's own
                # decision is to append it.
                if action in _MUST_SUCCEED and not state.finished and not state.stop_requested:
                    assert status in _SUCCESS, (
                        f"{action} CANNOT BE COMPLETED from {rd.name} {signature}: {trail}. Its "
                        f"postcondition is a fold of the operator's own intent, so no state of the "
                        f"engine can be a reason for it not to land.")
            checked += 1
            if depth >= _MAX_DEPTH:
                continue
            for action in _ALPHABET:
                child = world.clone(rd, f"{rd.name}.{action}")
                _apply(world, child, action)
                child_signature = _signature(world, child)
                if child_signature in seen:
                    continue
                seen.add(child_signature)
                frontier.append((child, depth + 1))
        # Non-vacuous: the search really did walk a state SPACE, and it really did close. A version
        # that explored one node, or that never terminated, must not read as a pass.
        # Non-vacuous, and it CLOSED. 22 distinct signatures on the shipped tree (2026-08-13); the
        # floor is deliberately far below that so ordinary drift does not fail the test, while a
        # search that collapsed to a handful of nodes — or never terminated — still does.
        assert 8 <= checked < _MAX_STATES, checked
        assert not frontier, "the search hit its state cap instead of closing"
    finally:
        world.close()


def test_the_recorded_incident_now_has_a_way_out(tmp_path):
    """The exact 2026-08-11 sequence, driven end to end against a LIVE engine.

    Live is load-bearing: `paused_and_stopped` is satisfied the instant the engine process is gone,
    so with a dead engine the defect does not exist at all.
    """
    world = _World(tmp_path / "runs")
    srv = world.srv
    rd = world.seed("rubertlite", alive=True)
    try:
        # 1. The operator pauses a run whose evaluation will keep the engine alive for hours.
        first = _drive(srv.commands, rd, EV_PAUSE, {})
        assert first["status"] == "succeeded", first
        # The half that is no longer a gate is still reported.
        assert first["engine_stopped"] is False
        assert srv.commands.srv.state(rd).paused is True

        # 2. The operator resumes out of band, and the run goes on for another day.
        EventStore(rd / "events.jsonl").append(EV_RESUME, {})
        assert srv.commands.srv.state(rd).paused is False

        # 3. Every later control still works — this is the `_intent_spent` half (2026-08-12).
        srv.commands.reject_if_active(rd, "pause the run")

        # 4. …and so does a NEW pause. This is the rung that was still live on 2026-08-13: every
        #    pause has the same (empty) payload, so the spent record matched `_unresolved_equivalent`
        #    and the fresh command was refused 409 `retry_existing_command`, pointing at a command
        #    whose retry could only time out.
        second = _drive(srv.commands, rd, EV_PAUSE, {})
        assert second["status"] == "succeeded", second
        assert second["id"] != first["id"]
        assert srv.commands.srv.state(rd).paused is True

        pauses = [event for event in EventStore(rd / "events.jsonl").read_all()
                  if event.type == EV_PAUSE]
        assert len(pauses) == 2, "the second pause must be a real second intent, not a replay"
    finally:
        world.close()


def test_a_legacy_timed_out_pause_record_can_be_retried_into_a_fresh_intent(tmp_path):
    """THE UPGRADE PATH — the run that is wedged RIGHT NOW.

    A durable command record is never rewritten, so a run stuck under the old build carries a
    `paused_and_stopped` record on disk whose intent a later resume consumed. Changing the
    postcondition for NEW commands does nothing for it. `/retry` must mint a FRESH intent instead of
    re-driving the consumed one — the backlog's own second floor requirement.
    """
    world = _World(tmp_path / "runs")
    srv = world.srv
    rd = world.seed("wedged", alive=True)
    store = EventStore(rd / "events.jsonl")
    command_id = "cmd_" + "a1b2" * 8
    spent = store.append(EV_PAUSE, {"_command_id": command_id})
    store.append(EV_RESUME, {})
    record = {
        "id": command_id, "event_type": EV_PAUSE, "event_seq": spent.seq,
        "baseline_seq": spent.seq - 1, "data": {},
        "postcondition": "paused_and_stopped", "status": "timed_out",
        "created_at": 1.0, "updated_at": 2.0, "engine_policy": "no_spawn",
        "run_generation": srv.commands.run_generation(rd),
        "error": {"code": "postcondition_timeout", "retryable": True,
                  "message": "paused_and_stopped was not observed in time"},
    }
    (rd / ".commands").mkdir(parents=True, exist_ok=True)
    (rd / ".commands" / f"{command_id}.json").write_text(json.dumps(record))

    # NON-VACUOUS: the durable intent really is intact and really is retryable. What changed is only
    # that re-driving it can no longer be the answer.
    assert srv.commands._find_intent(rd, command_id, record) is not None
    assert srv.commands._intent_spent(rd, record) is True
    assert srv.commands._intent_superseded(rd, record) is True

    try:
        retried = _settle(srv.commands, rd, srv.commands.retry(rd, command_id))
    finally:
        world.close()
    assert retried["status"] == "succeeded", retried
    assert srv.commands.srv.state(rd).paused is True

    pauses = [event for event in EventStore(rd / "events.jsonl").read_all()
              if event.type == EV_PAUSE]
    assert len(pauses) == 2, "the retry must APPEND a pause, not re-observe the consumed one"
    # The fresh intent carries its own marker: two events sharing one `_command_id` resolve to
    # `_DUPLICATE_INTENT` in the observation index and neither would ever be findable again.
    markers = [(event.data or {}).get("_command_id") for event in pauses]
    assert markers[0] == command_id and markers[1] != command_id
    stored = json.loads((rd / ".commands" / f"{command_id}.json").read_text())
    assert stored["intent_marker"] == markers[1]
    assert stored["superseded_event_seqs"] == [spent.seq]


def test_a_retry_that_could_only_time_out_again_refuses_and_names_the_way_out(tmp_path):
    """The other side of the same rule: where a fresh intent must NOT be minted, `/retry` says so.

    A pause on a FINISHED run cannot be re-issued (nothing will ever fold it, and re-issuing an
    additive intent would double-apply it after a reopen), so re-driving could only return
    `timed_out` forever — which is precisely what the operator measured for a day. The refusal names
    the move that works, and that move works because a spent record no longer blocks admission.
    """
    from looplab.events.types import EV_RUN_FINISHED

    world = _World(tmp_path / "runs")
    srv = world.srv
    rd = world.seed("done", alive=False)
    store = EventStore(rd / "events.jsonl")
    command_id = "cmd_" + "c3d4" * 8
    spent = store.append(EV_PAUSE, {"_command_id": command_id})
    store.append(EV_RESUME, {})
    store.append(EV_RUN_FINISHED, {"reason": "done"})
    record = {
        "id": command_id, "event_type": EV_PAUSE, "event_seq": spent.seq,
        "baseline_seq": spent.seq - 1, "data": {},
        "postcondition": "paused_and_stopped", "status": "timed_out",
        "created_at": 1.0, "updated_at": 2.0, "engine_policy": "no_spawn",
        "run_generation": srv.commands.run_generation(rd),
        "error": {"code": "postcondition_timeout", "retryable": True, "message": "…"},
    }
    (rd / ".commands").mkdir(parents=True, exist_ok=True)
    (rd / ".commands" / f"{command_id}.json").write_text(json.dumps(record))

    try:
        with pytest.raises(HTTPException) as caught:
            srv.commands.retry(rd, command_id)
        assert caught.value.detail["code"] == "command_intent_spent"
        assert "new idempotency key" in caught.value.detail["remediation"]

        # …and the named way out is real: a spent record does not hold a fresh command at the door.
        assert srv.commands._unresolved_equivalent(
            rd, EV_PAUSE, srv.commands._payload(EV_PAUSE, {})[1]) == (None, None)
    finally:
        world.close()


def test_an_unreadable_command_record_has_a_confirmed_escape(tmp_path):
    """The same hazard one layer up, and it had no escape at all.

    `_active_record` treats a record it cannot parse as `executing` — fail closed, deliberately,
    because destructive mutation must not erase the evidence of a command whose state is unknown —
    and `_active_command_ids` counts it too. So ONE corrupt `cmd_*.json` refuses every later command
    with `command_in_progress`, refuses reset and delete through `refuse_unless_quiescent`, and
    answers 503 to the very GET its own refusal names. Nothing clears it and nothing expires it.

    This is the shape the operator's requirement allows an absorbing state to have and no other: it
    is reachable only through an explicit confirmation, and the refusal NAMES that escape.
    """
    world = _World(tmp_path / "runs")
    rd = world.seed("corrupt", alive=True)
    commands = world.commands
    try:
        damaged = rd / ".commands" / ("cmd_" + "ab" * 16 + ".json")
        damaged.parent.mkdir(parents=True, exist_ok=True)
        damaged.write_text("{not json at all")
        os.utime(damaged, (time.time() - 600, time.time() - 600))

        # Wedged, in both directions, and the refusal names its own way out.
        assert _drive(commands, rd, EV_PAUSE, {})["code"] == "command_in_progress"
        with pytest.raises(HTTPException) as caught:
            commands.reject_if_active(rd, "pause the run")
        assert caught.value.detail["code"] == "command_in_progress"
        assert "resolve-activity-claims" in caught.value.detail["remediation"]

        # The escape is gated: an unconfirmed call refuses, and says the exact phrase it wants.
        with pytest.raises(HTTPException) as gate:
            commands.resolve_active_claims(rd)
        assert gate.value.detail["code"] == "active_claim_confirmation_required"

        resolved = commands.resolve_active_claims(
            rd, "I verified no LoopLab command or run activity is active")
        assert resolved["resolved"] is True and resolved["count"] == 1

        # QUARANTINED, not deleted: the plane is free and the bytes are still on disk.
        assert not damaged.exists()
        kept = list((rd / ".commands").glob("cmd_*.json.quarantined-*"))
        assert len(kept) == 1 and kept[0].read_text() == "{not json at all"
        assert _drive(commands, rd, EV_PAUSE, {})["status"] == "succeeded"
    finally:
        world.close()
