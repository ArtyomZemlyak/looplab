"""The Replay reset transaction's lock ladder, and each phase's stated lock precondition.

`reset_route._reset_blocking` was one ~446-line function nesting the whole ladder inline (doc 25
SC-14). It is now a driver over nine phase functions, and the split is only safe if two things stay
true — neither of which any existing test could tell you, because every reset test drives the route
from the outside and passes whether the phases are separate or interleaved:

1. **The ladder's ORDER.** Outermost first: command sequencer -> run lifecycle -> `engine.lock` ->
   (run config, event log, span index). Two processes that take these in different orders deadlock,
   and a route that publishes reset ownership before owning the event-log lock races a direct
   `looplab run/resume`, which owns only `engine.lock`.
2. **Where each phase sits in it.** A phase is only correct under an exact set of ALREADY-HELD
   locks: `_publish_and_archive` performs every durable mutation this route makes and needs all six;
   `_launch_replacement_engine` crosses Popen and must hold NONE, because holding a run lock across
   a process start is how a spawned child ends up waiting on the server that spawned it.

Both are checked twice, and deliberately never by a substring pin (CLAUDE.md: a guard test must not
be satisfiable by a comment):

* the DRIVEN pair replaces every lock context manager in `reset_route` with a recorder and runs two
  real Replays end to end — a completed one that climbs the whole ladder, and a rejoin of an
  uncertain launch that stops half way — asserting the acquire/release order and the exact stack
  held at every phase entry;
* the STRUCTURAL pair reads the driver's own AST, so what is pinned is the real `with` nesting a
  phase call sits inside, and a registry check makes a new phase state a precondition or fail.
"""
from __future__ import annotations

import ast
import contextlib
import inspect
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve import reset_route  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402
from tests._source_scan import function_tree  # noqa: E402
from tests.test_server import _build_run, _make_resumable, _replacement_spawn  # noqa: E402


# The ladder, by short name, outermost first. The keys are the exact call targets the route uses;
# an unregistered `with` inside `_reset_blocking` fails `test_reset_driver_locks_are_registered`
# rather than being silently skipped, because an unnamed lock cannot appear in a precondition.
LADDER = {
    "srv.commands.sequence": "sequence",
    "run_lifecycle_lock_http": "lifecycle",
    "engine_write_lock_http": "engine_write",
    "run_config_write_lock": "config",
    "_interprocess_lock": "events",
    "span_index_write_guard": "spans",
}

# Phase -> the exact lock stack that must be held when it is ENTERED, outermost first. This is the
# property, so it is spelled out here rather than derived from the code it guards.
PRECONDITIONS: dict[str, tuple[str, ...]] = {
    "_discover_or_rejoin": (),
    "_flush_retained_paid_activity": (),
    "_resolve_reset_ownership": ("sequence",),
    "_restore_existing_fence": ("sequence",),
    "_admit_destructive_reset": ("sequence",),
    "_classify_launch_evidence": ("sequence", "lifecycle"),
    "_publish_and_archive": (
        "sequence", "lifecycle", "engine_write", "config", "events", "spans"),
    "_launch_replacement_engine": (),
    "_await_replacement_generation": (),
}


class _LockRecorder:
    """Every lock the route takes, in acquisition order, plus the stack held right now."""

    def __init__(self) -> None:
        self.trace: list[str] = []          # "+name" acquired, "-name" released
        self.held: list[str] = []
        self.entries: list[tuple[str, tuple[str, ...]]] = []   # (phase, stack at entry)

    def lock(self, name, real):
        @contextlib.contextmanager
        def wrapper(*args, **kwargs):
            with real(*args, **kwargs) as value:
                self.trace.append("+" + name)
                self.held.append(name)
                try:
                    yield value
                finally:
                    self.held.pop()
                    self.trace.append("-" + name)
        return wrapper

    def phase(self, name, real):
        def wrapper(*args, **kwargs):
            self.entries.append((name, tuple(self.held)))
            return real(*args, **kwargs)
        return wrapper

    def install(self, monkeypatch, srv) -> None:
        for target, short in LADDER.items():
            if target == "srv.commands.sequence":
                monkeypatch.setattr(
                    srv.commands, "sequence", self.lock(short, srv.commands.sequence))
            else:
                monkeypatch.setattr(
                    reset_route, target, self.lock(short, getattr(reset_route, target)))
        for phase in PRECONDITIONS:
            monkeypatch.setattr(
                reset_route, phase, self.phase(phase, getattr(reset_route, phase)))

    def check_preconditions(self) -> None:
        for phase, stack in self.entries:
            assert stack == PRECONDITIONS[phase], (
                f"{phase} was entered holding {stack}, not {PRECONDITIONS[phase]}")


def _recorded_reset(tmp_path, monkeypatch, *, spawn) -> tuple[_LockRecorder, object, object]:
    """A real Replay of a real finished run, with every lock and phase instrumented."""
    from looplab.serve.routers import control as control_router

    _build_run(tmp_path)
    rd = tmp_path / "demo"
    _make_resumable(rd)
    (rd / "spans.jsonl").write_text('{"span":1}\n', encoding="utf-8")
    monkeypatch.setattr(control_router, "_spawn_engine", spawn(rd))

    app = make_app(tmp_path)
    recorder = _LockRecorder()
    recorder.install(monkeypatch, app.state.looplab)
    with TestClient(app) as client:
        response = client.post("/api/runs/demo/reset")
    return recorder, response, rd


def test_a_completed_replay_climbs_the_whole_ladder_in_order(tmp_path, monkeypatch):
    """The full transaction: acquire order is the ladder, release order is its exact reverse."""
    recorder, response, rd = _recorded_reset(
        tmp_path, monkeypatch, spawn=lambda rd: _replacement_spawn(rd))
    assert response.status_code == 200, response.json()
    assert list(rd.glob("events.jsonl.reset-*")), "the archive roll-forward must really have run"

    # The one nesting that matters, and it must NEST — not merely appear in this order. Slicing at
    # the outermost sequencer's acquire/release pins that every inner lock is taken AND dropped
    # inside it: a phase that leaked `engine.lock` past the sequencer would trail out of this window.
    start = recorder.trace.index("+sequence")
    ladder = recorder.trace[start:recorder.trace.index("-sequence", start) + 1]
    assert ladder == [
        "+sequence", "+lifecycle", "+engine_write", "+config", "+events", "+spans",
        "-spans", "-events", "-config", "-engine_write", "-lifecycle", "-sequence",
    ], recorder.trace

    # Popen happens with nothing held: `_launch_replacement_engine` re-takes the sequencer only to
    # record its outcome, so the ladder above is closed before the child is started.
    assert recorder.held == []
    assert recorder.trace.count("+lifecycle") == 1 and recorder.trace.count("+engine_write") == 1
    recorder.check_preconditions()


def test_every_phase_is_reached_holding_exactly_its_declared_precondition(tmp_path, monkeypatch):
    """A completed Replay reaches all nine phases, each with the stack its docstring claims."""
    recorder, response, _rd = _recorded_reset(
        tmp_path, monkeypatch, spawn=lambda rd: _replacement_spawn(rd))
    assert response.status_code == 200, response.json()
    recorder.check_preconditions()
    assert {phase for phase, _ in recorder.entries} == set(PRECONDITIONS), (
        "a completed Replay must exercise every phase, or this guard covers less than it claims")


def test_a_rejoined_uncertain_launch_never_climbs_past_its_phase(tmp_path, monkeypatch):
    """The recovery path stops at `_classify_launch_evidence`, still under exactly two locks.

    A retry of a launch whose Popen outcome is unknown answers 425 from inside the lifecycle lock —
    so it must NEVER have reached `engine.lock`, the writer locks, or the archive: taking them would
    mean a rejoin can mutate durable state a possibly-live child owns.
    """
    from looplab.serve.routers import control as control_router

    recorder, first, rd = _recorded_reset(
        tmp_path, monkeypatch, spawn=lambda _rd: (
            lambda *_a, **_kw: (_ for _ in ()).throw(OSError("injected Popen failure"))))
    assert first.status_code == 503 and first.json()["detail"]["code"] == "reset_launch_uncertain"

    monkeypatch.setattr(control_router, "_spawn_engine", _replacement_spawn(rd))
    app = make_app(tmp_path)
    rejoin_recorder = _LockRecorder()
    rejoin_recorder.install(monkeypatch, app.state.looplab)
    with TestClient(app) as client:
        rejoined = client.post("/api/runs/demo/reset")
    assert rejoined.status_code == 425
    assert rejoined.json()["detail"]["phase"] == "launch_uncertain"

    rejoin_recorder.check_preconditions()
    reached = [phase for phase, _ in rejoin_recorder.entries]
    assert "_classify_launch_evidence" in reached
    assert "_publish_and_archive" not in reached and "_launch_replacement_engine" not in reached
    assert "+engine_write" not in rejoin_recorder.trace
    assert "+spans" not in rejoin_recorder.trace


# ------------------------------------------------------------------- the structural half (AST)
#
# Comments are not AST nodes, so none of the below can be satisfied by a commented-out call: the
# chain is built from the real `ast.With` ancestors of each real `ast.Call`.


def _phase_calls_with_lock_chain(func) -> list[tuple[str, tuple[str, ...]]]:
    found: list[tuple[str, tuple[str, ...]]] = []

    def walk(node: ast.AST, chain: tuple[str, ...]) -> None:
        if isinstance(node, ast.With):
            inner = chain
            for item in node.items:
                expr = item.context_expr
                target = ast.unparse(expr.func) if isinstance(expr, ast.Call) else ""
                assert target in LADDER, (
                    f"unregistered lock {target!r} in the reset driver; add it to LADDER so it can "
                    f"appear in a phase precondition")
                inner += (LADDER[target],)
            for statement in node.body:
                walk(statement, inner)
            return
        if isinstance(node, ast.Call):
            target = ast.unparse(node.func)
            if target in PRECONDITIONS:
                found.append((target, chain))
        for child in ast.iter_child_nodes(node):
            walk(child, chain)

    walk(function_tree(func), ())
    return found


def test_the_driver_calls_every_phase_at_its_declared_rung(tmp_path):
    """Static twin of the driven check: the `with` nesting each phase call literally sits inside."""
    calls = _phase_calls_with_lock_chain(reset_route._reset_blocking)
    assert [phase for phase, _ in calls] == list(PRECONDITIONS), (
        "every phase is called exactly once, in receipt order, by the driver")
    for phase, chain in calls:
        assert chain == PRECONDITIONS[phase], (
            f"{reset_route._reset_blocking.__name__} calls {phase} under {chain}, "
            f"not {PRECONDITIONS[phase]}")


def test_every_reset_phase_states_a_lock_precondition(tmp_path):
    """The registry is two-way: a phase without a stated precondition, or a stated precondition
    without a phase, is a hole in the guard above rather than a style lapse."""
    documented = {
        name for name, obj in vars(reset_route).items()
        # The driver is excluded by name, not by accident: its own docstring quotes the marker to
        # explain what the phases below carry, and matching on it would make this check circular.
        if inspect.isfunction(obj) and obj.__module__ == reset_route.__name__
        and name != "_reset_blocking"
        and (obj.__doc__ or "").find("Lock precondition:") >= 0
    }
    assert documented == set(PRECONDITIONS), (
        f"undeclared phases {documented - set(PRECONDITIONS)}, "
        f"stale entries {set(PRECONDITIONS) - documented}")
    # The no-lock phases are the ones a reader is most likely to "tidy" into the ladder, so their
    # docstrings must say so in words as well as by their empty tuple above.
    for phase, expected in PRECONDITIONS.items():
        if not expected:
            doc = getattr(reset_route, phase).__doc__ or ""
            assert "one held" in doc or "ONE held" in doc, phase


def test_reset_route_is_not_one_function_again(tmp_path):
    """The driver stays short. Not a line-count fetish: SC-14's cost was that every change had to
    re-establish the whole lock ordering mentally, and that returns the moment a phase is inlined."""
    source = Path(inspect.getsourcefile(reset_route)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    driver = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_reset_blocking")
    assert driver.end_lineno - driver.lineno + 1 < 120, (
        "the reset driver grew back past the phase split")
