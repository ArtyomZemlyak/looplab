"""`Settings.developer_crash_pause_after`: how many Developer-session crashes a RUN absorbs before
the circuit breaker auto-pauses it (docs/60 §60.9 A12).

The breaker's rule was "pause on the FIRST `developer_crash`", and its argument is intact: a
Developer that could not finish one node even after the client's own retries has hit something a
NEW node cannot fix. What the 2026-08-24 campaign measured is the price of that rule on a stand
nobody is watching — 9 of 20 arm-B runs ended `PAUSED — a Developer session crashed` and were
never resumed (docs/58 §58.2). A threshold of 2 is one automatic retry; 1 is byte-identical to
before, and is the default.

The count is the LOG's, in log order (`node_build.developer_crash_rank`), not a process counter:
a run outlives its process, and the every-turn recovery sweep (`_close_developer_sentinel_once`)
reads the log to find a crash terminal owning no pause — which, above a threshold of one, is what
a below-threshold crash looks like BY DESIGN. Both readers therefore consult the same rank.
"""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.node_build import developer_crash_rank
from looplab.engine.orchestrator import Engine
from looplab.engine.options import EngineOptions
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_NODE_FAILED, EV_PAUSE
from tests.factories import make_engine

TOY = Path(__file__).resolve().parents[1] / "examples" / "toy_task.json"
CRASH = "(developer error: LLM unreachable)"


class _CrashingDev:
    """Every session dies in band — the `(developer error: …)` sentinel `repo_developer.py::_run`
    returns when its provider raises, which is what the engine sees of a crashed session."""

    def implement(self, idea: Idea) -> str:
        return CRASH


def _run(tmp_path, name: str, **knobs):
    eng = make_engine(tmp_path / name, developer=_CrashingDev(), n_seeds=3, max_nodes=8, **knobs)
    state = anyio.run(eng.run)
    events = EventStore(tmp_path / name / "events.jsonl").read_all()
    return state, events


def _shape(events):
    """The crash-relevant skeleton of a log: every terminal (with its reason) and every pause
    (whose `reason` is the operator sentence, so only the node it names is kept), in order."""
    return [(e.type, e.data.get("node_id"),
             e.data.get("reason") if e.type == EV_NODE_FAILED else None)
            for e in events if e.type in (EV_NODE_FAILED, EV_PAUSE)]


# ------------------------------------------------------------------ the shipped default

def test_the_default_is_one_and_is_the_historical_pause_on_the_first_crash(tmp_path):
    from looplab.core.config import Settings
    assert Settings().developer_crash_pause_after == 1
    assert EngineOptions().developer_crash_pause_after == 1
    state, events = _run(tmp_path, "default")
    assert _shape(events) == [(EV_NODE_FAILED, 0, "developer_crash"), (EV_PAUSE, 0, None)]
    assert state.paused and state.pause_node_id == 0


def _skeleton(events):
    """Every row's type, and every crash-path row's payload, with the per-run identity fields
    (`run_id`, `run_uid`, the config hash, wall-clock seconds) that differ between any two runs
    of the same program set aside."""
    volatile = {"run_id", "run_uid", "config_hash", "eval_seconds", "ts", "started_at"}
    return [(e.type, {k: v for k, v in e.data.items() if k not in volatile}
             if e.type in (EV_NODE_FAILED, EV_PAUSE, "node_created", "run_finished") else None)
            for e in events]


def test_an_explicit_one_is_byte_identical_to_the_default_run(tmp_path):
    _, default = _run(tmp_path, "default")
    _, explicit = _run(tmp_path, "explicit", developer_crash_pause_after=1)
    assert _skeleton(default) == _skeleton(explicit)


# ------------------------------------------------------------------ a threshold above one

def test_at_two_the_first_crash_fails_its_node_and_the_run_goes_on_the_second_pauses(tmp_path):
    state, events = _run(tmp_path, "two", developer_crash_pause_after=2)
    assert _shape(events) == [
        (EV_NODE_FAILED, 0, "developer_crash"),          # crash 1: terminal, NO pause, next seed
        (EV_NODE_FAILED, 1, "developer_crash"),          # crash 2: terminal …
        (EV_PAUSE, 1, None),                             # … and the breaker, naming THIS node
    ]
    assert state.paused and state.pause_node_id == 1
    assert state.nodes[0].status is NodeStatus.failed
    assert state.nodes[0].error_reason == "developer_crash", (
        "a below-threshold crash keeps exactly the terminal it always got; only the pause waits")


def test_the_pause_carries_the_same_words_and_fields_as_before(tmp_path):
    """Only WHEN the pause is requested changed. Its payload is `developer_crash_records`'s."""
    _, one = _run(tmp_path, "one")
    _, two = _run(tmp_path, "two", developer_crash_pause_after=2)
    pause_one = next(e.data for e in one if e.type == EV_PAUSE)
    pause_two = next(e.data for e in two if e.type == EV_PAUSE)
    assert pause_one["reason"] == pause_two["reason"]
    assert {**pause_one, "node_id": None} == {**pause_two, "node_id": None}


def test_the_pause_is_still_appended_by_the_main_task_after_the_worker_join(tmp_path):
    """Invariant #1: the fan-out site QUEUES the pause; `_drain_create_pause` appends it. A
    threshold does not move the seam — it decides whether `_request_create_pause` is asked."""
    eng = make_engine(tmp_path / "seam", developer=_CrashingDev(), developer_crash_pause_after=2)
    eng.store.append("run_started", {"run_id": "seam", "task_id": "toy", "direction": "min"})
    eng._create_node({"kind": "draft"})
    # Both attributes are created lazily (the run loop resets them per turn; a build can crash on
    # a path that has not reached that reset yet), hence `getattr` with the "nothing" default.
    assert (getattr(eng, "_pending_create_pause", []) == []
            and not getattr(eng, "_create_paused", False)), (
        "crash 1 of 2: nothing queued for the main task, the batch may continue")
    eng._create_node({"kind": "draft"})
    assert len(eng._pending_create_pause) == 1 and eng._create_paused, (
        "crash 2 of 2: the pause is REQUESTED, not appended, from the build path")
    assert not any(e.type == EV_PAUSE for e in eng.store.read_all())
    eng._drain_create_pause()
    assert [e.data["node_id"] for e in eng.store.read_all() if e.type == EV_PAUSE] == [1]


# ------------------------------------------------------------------ the rank rule

def _state(*crash_seqs, extra=()):
    st = RunState(direction="min")
    for nid, seq in enumerate(crash_seqs):
        node = Node(id=nid, operator="draft", idea=Idea(operator="draft", params={"x": 0.0}),
                    status=NodeStatus.failed, error_reason="developer_crash", code=CRASH)
        node.terminal_event_seq = seq
        st.nodes[nid] = node
    for nid, node in extra:
        st.nodes[nid] = node
    return st


def test_rank_is_the_terminals_position_in_log_order():
    st = _state(40, 10, 25)                  # node 0 crashed at seq 40, node 1 at 10, node 2 at 25
    assert developer_crash_rank(st, 1) == 1
    assert developer_crash_rank(st, 2) == 2
    assert developer_crash_rank(st, 0) == 3


def test_a_node_whose_terminal_is_not_folded_yet_takes_the_next_rank():
    """The speculation lane's tail-CAS plan and the recovery sweep decide BEFORE appending."""
    assert developer_crash_rank(_state(), 7) == 1
    assert developer_crash_rank(_state(3, 9), 7) == 3


def test_only_current_developer_crash_terminals_count():
    other = Node(id=5, operator="draft", idea=Idea(operator="draft", params={"x": 0.0}),
                 status=NodeStatus.failed, error_reason="crash", code="x")
    other.terminal_event_seq = 1
    stuck = Node(id=6, operator="draft", idea=Idea(operator="draft", params={"x": 0.0}),
                 status=NodeStatus.failed, error_reason="developer_stuck", code="(developer stuck: y)")
    stuck.terminal_event_seq = 2
    st = _state(30, extra=[(5, other), (6, stuck)])
    assert developer_crash_rank(st, 0) == 1, "an eval crash and a stuck build are not sessions dying"


def test_the_engine_rule_settles_junk_to_the_historical_one():
    eng = Engine.__new__(Engine)                # ~170 tests build the engine this way: no __init__
    st = _state(1)
    assert eng._developer_crash_pause_due(st, 0) is True, "no attribute at all: pause on the first"
    for junk in (0, -3, "x", None, True):
        eng.developer_crash_pause_after = junk
        assert eng._developer_crash_pause_due(st, 0) is True, junk
    eng.developer_crash_pause_after = 2
    assert eng._developer_crash_pause_due(st, 0) is False
    assert eng._developer_crash_pause_due(_state(1, 2), 1) is True


# ------------------------------------------------------------------ the recovery sweep

def _crashed_pending_log(eng, node_id: int):
    eng.store.append("node_building", {"node_id": node_id, "generation": 0})
    eng.store.append("node_created", {
        "node_id": node_id, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": float(node_id)}}, "code": CRASH})


def test_the_sweep_does_not_recover_a_below_threshold_crash_into_a_pause(tmp_path):
    """Under the old rule a `developer_crash` terminal with no pause after it was a LOST append,
    and the sweep repaired it every turn. Above a threshold of one that shape is the design."""
    eng = make_engine(tmp_path / "sweep", developer=_CrashingDev(), developer_crash_pause_after=2)
    eng.store.append("run_started", {"run_id": "sweep", "task_id": "toy", "direction": "min"})
    _crashed_pending_log(eng, 0)
    assert anyio.run(eng._close_developer_sentinel_once) is True, "the pending sentinel is closed"
    events = eng.store.read_all()
    assert _shape(events) == [(EV_NODE_FAILED, 0, "developer_crash")], (
        "crash 1 of 2: the sweep appends the terminal it owes and NO pause")
    assert not fold(events).paused
    assert anyio.run(eng._close_developer_sentinel_once) is False, (
        "…and a later turn does not 'recover' that missing pause: the sweep and the live "
        "decision read the same rank")
    _crashed_pending_log(eng, 1)
    assert anyio.run(eng._close_developer_sentinel_once) is True
    events = eng.store.read_all()
    assert _shape(events)[-2:] == [(EV_NODE_FAILED, 1, "developer_crash"), (EV_PAUSE, 1, None)]
    assert fold(events).paused and fold(events).pause_node_id == 1


def test_the_sweep_still_recovers_a_lost_pause_at_the_threshold(tmp_path):
    """The recovery the sweep exists for is untouched where the crash DOES owe a pause."""
    eng = make_engine(tmp_path / "lost", developer=_CrashingDev(), developer_crash_pause_after=2)
    eng.store.append("run_started", {"run_id": "lost", "task_id": "toy", "direction": "min"})
    for nid in (0, 1):
        _crashed_pending_log(eng, nid)
        eng.store.append(EV_NODE_FAILED, {"node_id": nid, "generation": 0, "error": CRASH,
                                          "reason": "developer_crash", "eval_seconds": 0.0})
    assert anyio.run(eng._close_developer_sentinel_once) is True
    events = eng.store.read_all()
    pauses = [e.data["node_id"] for e in events if e.type == EV_PAUSE]
    assert pauses == [1], "crash 2 of 2 lost its pause: recovered, naming the second node only"
    assert anyio.run(eng._close_developer_sentinel_once) is False
