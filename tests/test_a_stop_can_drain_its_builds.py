"""`looplab stop --drain-builds`: builds already running finish and COMMIT before the engine exits.

A pause (`looplab stop`) stops a run STARTING work and drains the evaluations already running
(`tests/test_stop_wait.py`), but a Card build in flight was thrown away: `_serve_card_builds` closes
a halted head `run_is_stopping` — with its producer still running — and the finished result is then
an orphan. MEASURED 2026-09-27 on MiniOneRec inf13: every operator restart lost the builds in flight,
each up to an hour of Developer work (card-7 at seq 2547; card-4 committed seconds before a kill).

`--drain-builds` puts `drain_builds: true` on the pause row. While that pause stands
(`speculation.py::_pause_drains_builds`), a head whose producer is still running is left open, a
finished build commits its node (pending — `looplab resume` evaluates it), a head nothing is running
for is closed as before, and nothing new is elected. Never on a finish or an abort.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import anyio
import pytest
from typer.testing import CliRunner

from looplab.agents.toy_roles import ToyObjectiveDeveloper
from looplab.cli import app
from looplab.core.models import Idea, NodeStatus
from looplab.engine.speculation import SpeculationMixin
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_BUILD_DONE, EV_PAUSE
from tests.test_card_speculation_engine import (  # noqa: F401  (autouse receipt fixture)
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _build_result,
    _engine,
    _request,
    _start,
)


# ------------------------------------------------------------------------------------ the fold

def _paused(tmp_path, row):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "goal": "g", "direction": "max"})
    store.append(EV_PAUSE, row)
    return store


def test_the_pause_row_carries_the_drain_and_only_a_real_true_counts(tmp_path):
    drain = fold(_paused(tmp_path / "a", {"reason": "op", "drain_builds": True}).read_all())
    assert drain.pause_drain_builds and SpeculationMixin._pause_drains_builds(drain)
    plain = fold(_paused(tmp_path / "b", {"reason": "op"}).read_all())
    assert not plain.pause_drain_builds and not SpeculationMixin._pause_drains_builds(plain)
    garbled = fold(_paused(tmp_path / "c", {"reason": "op", "drain_builds": "yes"}).read_all())
    assert not SpeculationMixin._pause_drains_builds(garbled)


def test_a_lifted_pause_a_finish_or_an_abort_never_drains(tmp_path):
    store = _paused(tmp_path / "a", {"reason": "op", "drain_builds": True})
    store.append("resume", {})
    assert not SpeculationMixin._pause_drains_builds(fold(store.read_all()))
    store = _paused(tmp_path / "b", {"reason": "op", "drain_builds": True})
    store.append("run_abort", {"reason": "finalized"})
    assert not SpeculationMixin._pause_drains_builds(fold(store.read_all()))


def test_a_second_pause_does_not_rewrite_the_first(tmp_path):
    """Same guard as `pause_reason`: a pause that changes nothing did not take effect."""
    store = _paused(tmp_path, {"reason": "op", "drain_builds": True})
    store.append(EV_PAUSE, {"reason": "again"})
    assert SpeculationMixin._pause_drains_builds(fold(store.read_all()))


# ------------------------------------------------------------ the service of one open request

def _ready(tmp_path, name):
    engine, _producer = _engine(tmp_path / name)
    _start(engine)
    _add_ready_draft(engine, "card-7")
    request = _request(engine)
    return engine, request


def _closes(engine):
    return [e.data for e in engine.store.read_all() if e.type == EV_CARD_BUILD_DONE]


def test_under_a_drain_a_finished_build_commits_its_node(tmp_path):
    engine, request = _ready(tmp_path, "commit")
    result = _build_result(engine, request)
    engine._spec_builds[result.key] = result
    engine.store.append(EV_PAUSE, {"reason": "op", "drain_builds": True})
    paused = fold(engine.store.read_all())
    assert engine._close_card_build_before_terminal_gate(paused) is True
    closes = _closes(engine)
    assert len(closes) == 1 and "node_id" in closes[0], closes
    node = fold(engine.store.read_all()).nodes[closes[0]["node_id"]]
    assert node.status is NodeStatus.pending, "committed, evaluated after `looplab resume`"
    assert not engine._draining_builds_in_flight(fold(engine.store.read_all()))


def test_a_plain_stop_still_discards_it(tmp_path):
    """The historical disposition, unchanged for every pause that did not ask for a drain."""
    engine, request = _ready(tmp_path, "discard")
    result = _build_result(engine, request)
    engine._spec_builds[result.key] = result
    engine.store.append(EV_PAUSE, {"reason": "op"})
    engine._close_card_build_before_terminal_gate(fold(engine.store.read_all()))
    closes = _closes(engine)
    assert closes == [{"card_id": "card-7", "generation": 0, "skipped": "stale",
                       "skipped_reason": "run_is_stopping"}], closes


def test_under_a_drain_a_running_build_is_waited_for_not_closed(tmp_path):
    engine, request = _ready(tmp_path, "running")
    engine._spec_build_inflight.add(engine._request_key(request))   # its producer is still running
    engine.store.append(EV_PAUSE, {"reason": "op", "drain_builds": True})
    paused = fold(engine.store.read_all())
    assert engine._serve_card_builds(allow_commit=False) is False
    assert _closes(engine) == [], "a live producer's head is left open while the drain waits"
    assert engine._draining_builds_in_flight(paused)


def test_under_a_drain_a_head_nothing_is_running_for_is_closed(tmp_path):
    """Nothing new may start under a pause, so a producer-less head would otherwise hold the drain
    open for ever."""
    engine, _request_row = _ready(tmp_path, "idle")
    engine.store.append(EV_PAUSE, {"reason": "op", "drain_builds": True})
    assert engine._serve_card_builds(allow_commit=False) is True
    assert _closes(engine) == [{"card_id": "card-7", "generation": 0, "skipped": "stale",
                                "skipped_reason": "run_is_stopping"}]


def test_the_claim_path_refuses_a_stopping_run_but_not_a_draining_one(tmp_path):
    engine, request = _ready(tmp_path, "claim")
    result = _build_result(engine, request)
    engine.store.append(EV_PAUSE, {"reason": "op", "drain_builds": True})
    outcome, node_id = engine._claim_requested_card_build(request, result)
    assert outcome == "created" and node_id is not None


# ------------------------------------------------------------ end to end, through `engine.run`

class _Gate:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.cards: list = []


class _GatedToyDeveloper(ToyObjectiveDeveloper):
    """The PRODUCER pairs' Developer: its build holds until the test releases it. Serial builds use
    the engine's own, ungated Developer, so the run keeps moving until the pause lands."""

    def __init__(self, gate: _Gate):
        super().__init__()
        self._gate = gate

    def implement(self, idea: Idea) -> str:
        self._gate.cards.append(idea.card_id)
        self._gate.started.set()
        if not self._gate.release.wait(60):
            raise RuntimeError("the test never released this build")
        return super().implement(idea)


def _drive(tmp_path, *, drain: bool, width: int):
    from looplab.adapters.toytask import ToyTask
    from tests.factories import make_engine

    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    gate = _Gate()

    def factory():
        researcher, _developer = task.build_roles()
        return researcher, _GatedToyDeveloper(gate)

    engine = make_engine(tmp_path / f"drain-{drain}-{width}", task=task, max_nodes=40, n_seeds=3,
                         card_driven_selection=True, speculation_depth=1, role_factory=factory,
                         llm_parallel=width)
    if not engine._speculation_enabled() or engine._producer_role_pair() is None:
        pytest.skip("this build does not admit a spelled positive depth on the toy adapter")
    seen: dict = {}

    def controller():
        if not gate.started.wait(90):
            seen["error"] = "no speculative build ever started"
            gate.release.set()
            return
        store = EventStore(engine.run_dir / "events.jsonl")
        row = {"reason": "test stop", **({"drain_builds": True} if drain else {})}
        seen["pause_seq"] = store.append(EV_PAUSE, row).seq
        # Hold the build while the engine acts on the pause: a plain stop closes the head at once.
        time.sleep(3.0)
        seen["released_at"] = len(store.read_all())
        gate.release.set()

    thread = threading.Thread(target=controller, daemon=True)
    thread.start()
    anyio.run(engine.run)
    thread.join(timeout=5)
    assert "error" not in seen, seen.get("error")
    return engine, gate, seen


def _card_closes_after(engine, card_id, seq):
    return [e for e in engine.store.read_all()
            if e.type == EV_CARD_BUILD_DONE and e.seq > seq and e.data.get("card_id") == card_id]


@pytest.mark.parametrize("width", [1, 2])
def test_a_draining_stop_commits_the_build_it_was_holding(tmp_path, width):
    """Width 1: the session that owns the producer waits it out and commits it. Width 2: the build
    is adopted and outlives the session, so the outer loop's pause branch polls it home."""
    engine, gate, seen = _drive(tmp_path, drain=True, width=width)
    card = gate.cards[0]
    closes = _card_closes_after(engine, card, seen["pause_seq"])
    assert closes and "node_id" in closes[-1].data, [c.data for c in closes]
    assert not [c for c in closes if c.data.get("skipped")], [c.data for c in closes]
    state = fold(engine.store.read_all())
    assert state.paused and not state.finished
    assert state.nodes[closes[-1].data["node_id"]].status is NodeStatus.pending


def test_a_plain_stop_throws_the_same_build_away(tmp_path):
    engine, gate, seen = _drive(tmp_path, drain=False, width=2)
    card = gate.cards[0]
    closes = _card_closes_after(engine, card, seen["pause_seq"])
    assert closes and closes[0].data.get("skipped") == "stale", [c.data for c in closes]
    assert closes[0].data.get("skipped_reason") == "run_is_stopping"
    assert closes[0].seq < seen["released_at"], "closed while its producer was still running"
    assert not [e for e in engine.store.read_all()
                if e.type == "node_created" and e.seq > seen["pause_seq"]
                and (e.data.get("idea") or {}).get("card_id") == card]


# ------------------------------------------------------------------------------------- the CLI

def _run_dir(tmp_path: Path) -> Path:
    rd = tmp_path / "run"
    rd.mkdir(parents=True)
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "run", "task_id": "t", "goal": "g", "direction": "max"})
    return rd


def test_the_flag_rides_the_pause_row_and_a_plain_stop_writes_the_row_it_always_did(tmp_path):
    rd = _run_dir(tmp_path / "a")
    out = CliRunner().invoke(app, ["stop", str(rd), "--drain-builds"])
    assert out.exit_code == 0, out.output
    assert "finish and commit first" in out.output
    row = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == EV_PAUSE][-1].data
    assert row == {"reason": "operator stop (`looplab stop --drain-builds`)", "drain_builds": True}
    assert fold(EventStore(rd / "events.jsonl").read_all()).pause_drain_builds

    rd = _run_dir(tmp_path / "b")
    assert CliRunner().invoke(app, ["stop", str(rd)]).exit_code == 0
    row = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == EV_PAUSE][-1].data
    assert row == {"reason": "operator stop (`looplab stop`)"}


def test_with_wait_and_no_engine_it_returns_at_once(tmp_path):
    rd = _run_dir(tmp_path)
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait", "--drain-builds"])
    assert out.exit_code == 0, out.output
    assert "no engine was running" in out.output
