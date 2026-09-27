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

HARDENED the same day (critic review of the first cut). Each fix is driven below through the real
engine, and each test names the mutation that turns it red:

* a spend ceiling HELD for the loop head makes the drain a plain stop (`_drains_builds_now`): the
  producer that parked it stored no result, and the drain read its head as a dead process's;
* the loop's pause branch commits against the run's REAL eval-seconds ceiling, not None;
* the drain waits only for OPEN requests, and a result no node slot can take is closed rather than
  polled for ever;
* `--drain-builds` is refused on a run already halted, verified after its append, and a `--timeout`
  names the builds it gave up on;
* any later pause — a plain `looplab stop`, the engine's own auto-pause — CANCELS a drain, and the
  engine still writes its auto-pause under one (`speculation.py::auto_pause_is_redundant`).
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import anyio
import anyio.from_thread
import anyio.lowlevel
import pytest
from typer.testing import CliRunner

from looplab.agents.toy_roles import ToyObjectiveDeveloper
from looplab.cli import app
from looplab.core.errors import BudgetExceeded
from looplab.core.models import Idea, NodeStatus
from looplab.engine.speculation import SpeculationMixin, auto_pause_is_redundant
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import (EV_BUDGET_EXTEND, EV_CARD_BUILD_DONE, EV_CARD_BUILD_REQUESTED,
                                  EV_NODE_CREATED, EV_PAUSE)
from tests._posix_gates import FLOCK
from tests.test_card_speculation_engine import (  # noqa: F401  (autouse receipt fixture)
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _build_result,
    _Developer,
    _engine,
    _request,
    _start,
    _without_research,
)
from tests.test_stop_wait import _hold_lock

# The row `looplab stop --drain-builds` writes, and the node-less row the engine's provider circuit
# breaker writes (`evaluate.py::_auto_pause_provider_failure`).
_DRAIN = {"reason": "operator stop (`looplab stop --drain-builds`)", "drain_builds": True}
_AUTO_PAUSE = {"reason": "auto-paused: the Developer's LLM provider failed. Every other node "
                         "reaches the same endpoint; fix it and resume."}
CEILING = ("LLM spend ceiling reached: $1.0031 of the $1.0000 set by `llm_budget_usd`. "
           "The run stops here rather than spending more.")


# ------------------------------------------------------------------------------------ the fold

def _paused(tmp_path, *rows):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "goal": "g", "direction": "max"})
    for row in rows:
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


@pytest.mark.parametrize("later", [
    {"reason": "operator stop (`looplab stop`)"},
    _AUTO_PAUSE,
    # The scoped Developer-crash breaker. The fold drops it as the pause's OWNER (an operator stop
    # stands), and it still ends the drain: the Developer's endpoint just crashed a build.
    {"node_id": 0, "generation": 0, "reason": "auto-paused: a Developer session crashed"},
], ids=["plain_stop", "engine_auto_pause", "scoped_auto_pause"])
def test_any_later_pause_cancels_a_standing_drain(tmp_path, later):
    """Whether or not it moves the pause triple: the FIRST pause's reason still answers "why is it
    paused", but the drain is over. MUTATION: set `pause_drain_builds` only under the triple's guard
    (the first cut) — a provider outage then lands on a drain that goes on building against the
    dead endpoint, and a later plain `looplab stop` can no longer cancel it."""
    state = fold(_paused(tmp_path, _DRAIN, later).read_all())
    assert state.paused and state.pause_reason == _DRAIN["reason"]
    assert not SpeculationMixin._pause_drains_builds(state)


def test_a_restart_cancels_a_drain_too(tmp_path):
    store = _paused(tmp_path, _DRAIN)
    store.append("restart", {})
    assert not SpeculationMixin._pause_drains_builds(fold(store.read_all()))


def test_a_drain_starts_only_on_the_pause_that_takes_effect(tmp_path):
    """A drain row landing on a run already paused changes nothing, exactly as its reason does not
    (the CLI refuses the flag there); a repeated drain keeps the one standing; and a row naming a
    node is the engine's scoped breaker, never an operator's drain."""
    late = fold(_paused(tmp_path / "late", {"reason": "op"}, _DRAIN).read_all())
    assert late.paused and not SpeculationMixin._pause_drains_builds(late)
    again = fold(_paused(tmp_path / "again", _DRAIN, _DRAIN).read_all())
    assert SpeculationMixin._pause_drains_builds(again)
    scoped = fold(_paused(tmp_path / "scoped", {**_DRAIN, "node_id": 0, "generation": 0}).read_all())
    assert not scoped.pause_drain_builds


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


def test_a_held_ceiling_makes_the_drain_a_plain_stop_at_the_claim_and_the_serve(tmp_path):
    """`_drains_builds_now` is the one predicate the serve, the claim and the materialization CAS ask.
    A run whose loop head is about to raise a spend ceiling ends there; a node committed under it is
    work nobody evaluates. MUTATION: drop `and self._eval_budget_stop is None`."""
    engine, request = _ready(tmp_path, "held-ceiling")
    result = _build_result(engine, request)
    engine._spec_builds[result.key] = result
    engine.store.append(EV_PAUSE, dict(_DRAIN))
    state = fold(engine.store.read_all())
    assert engine._drains_builds_now(state), "precondition: a standing drain"
    engine._eval_budget_stop = BudgetExceeded(CEILING)
    try:
        assert not engine._drains_builds_now(state)
        assert engine._claim_requested_card_build(request, result) == ("stale:run_is_stopping", None)
        # …and the serve closes the finished build as any halt would, even with the commit allowed.
        assert engine._serve_card_builds(allow_commit=True) is True
        assert _closes(engine) == [{"card_id": "card-7", "generation": 0, "skipped": "stale",
                                    "skipped_reason": "run_is_stopping"}], _closes(engine)
        assert not fold(engine.store.read_all()).nodes, "nothing committed under the ceiling"
    finally:
        engine._eval_budget_stop = None


def test_the_drain_waits_only_for_open_requests(tmp_path):
    """A producer whose request is already CLOSED builds a result nothing will commit; waiting for
    it made the drain as long as that producer, hours or never. MUTATION: count every in-flight
    producer (the first cut)."""
    engine, request = _ready(tmp_path, "open-only")
    engine.store.append(EV_PAUSE, dict(_DRAIN))
    state = fold(engine.store.read_all())
    engine._spec_build_inflight.add(("card-3", 0))           # its request was closed long ago
    assert not engine._draining_builds_in_flight(state)
    engine._spec_build_inflight.add(engine._request_key(request))
    assert engine._draining_builds_in_flight(state)


# ------------------------------------------------------------ through `engine.run`, unit engine

async def _bounded(engine, seconds: float = 30.0):
    # BOUNDED: a drain that never lets the loop exit is a red test, not a hung suite.
    with anyio.fail_after(seconds):
        return await engine.run()


class _StoppedThenCeilingDeveloper(_Developer):
    """The producer's build: `looplab stop --drain-builds` lands while it runs, then its provider call
    crosses the run's spend ceiling. The producer PARKS the stop (`_run_isolated_producer`) and stores
    no result — so its head carries this process's receipt, no live producer and no result."""

    def __init__(self, run_dir: Path):
        super().__init__()
        self.run_dir = Path(run_dir)
        self.stop = BudgetExceeded(CEILING)

    def implement(self, _idea):
        self.calls += 1
        EventStore(self.run_dir / "events.jsonl").append(EV_PAUSE, dict(_DRAIN))
        raise self.stop


def test_a_ceiling_held_under_a_drain_closes_the_build_stale_never_producer_failed(tmp_path,
                                                                                    monkeypatch):
    """End to end through `engine.run` at width 1, where the session owns the producer and turns on
    (`closing_holds`) after it parks the ceiling. MUTATION: let the drain ignore the held stop and the
    session's next turn closes the head `producer_failed` inside the process that made the attempt —
    the race `_session_gates` exists to close — barring the Card for the run's own ceiling."""
    run_dir = tmp_path / "drain-ceiling"
    producer = _StoppedThenCeilingDeveloper(run_dir)
    engine, _producer = _engine(run_dir, producer=producer)
    _start(engine)
    idea = _add_ready_draft(engine)
    _without_research(monkeypatch, engine)

    with pytest.raises(BudgetExceeded) as caught:
        anyio.run(_bounded, engine)
    assert caught.value is producer.stop and producer.calls == 1
    assert _closes(engine) == [{"card_id": idea.card_id, "generation": 0, "skipped": "stale",
                                "skipped_reason": "run_is_stopping"}], _closes(engine)
    assert engine._card_requires_serial_fallback(idea.card_id) is False


def test_a_result_no_node_slot_can_take_is_closed_not_polled_for_ever(tmp_path):
    """Outside a drain the claim keeps such a head open for a later `add_nodes`
    (`test_speculative_last_slot_request_waits_for_budget_extend_without_rebuild`). Under a drain the
    engine exits once the drain ends and the result dies with it — and the open result kept
    `_draining_builds_in_flight` true, so the pause branch polled it every two seconds for ever.
    Driven through `engine.run`, bounded. MUTATION: drop the drain branch of the `budget` outcome."""
    engine, _producer = _engine(tmp_path / "no-slot")
    _start(engine)
    _add_ready_draft(engine)
    engine._base_max_nodes = 0
    engine.policy.max_nodes = 0
    engine.store.append(EV_CARD_BUILD_REQUESTED, {
        "card_id": "card-7", "generation": fold(engine.store.read_all()).search_epoch})
    request = engine._head_request(fold(engine.store.read_all()))
    result = _build_result(engine, request)
    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result
    assert engine._claim_requested_card_build(request, result)[0] == "budget", "precondition"
    engine.store.append(EV_PAUSE, dict(_DRAIN))

    final = anyio.run(_bounded, engine)
    assert _closes(engine) == [{"card_id": "card-7", "generation": 0, "skipped": "stale",
                                "skipped_reason": "run_is_stopping"}], _closes(engine)
    assert final.paused and not final.nodes


# ------------------------------------------------ the engine's auto-pause, written under a drain

def _pending_node(engine, node_id: int = 0) -> int:
    engine.store.append("node_created", {
        "node_id": node_id, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "base"},
        "code": "print(1)"})
    return node_id


@pytest.mark.parametrize("writer", ["provider_outage", "engine_error"])
def test_the_engines_auto_pause_is_written_under_a_drain_and_only_there(tmp_path, writer):
    """The writers' "already halted, write nothing" skip read a drain as halted, so the one row that
    ends a drain was never written. Under a plain pause the auto-pause is still redundant and still
    skipped. MUTATION: guard the writer on the bare `halted` again."""
    from tests.factories import make_engine

    def _write(engine):
        if writer == "provider_outage":
            anyio.run(engine._auto_pause_provider_failure, "the Developer's LLM provider failed")
        else:
            anyio.run(engine._contain_eval_crash, _pending_node(engine), 0,
                      OSError(28, "No space left on device"))

    def _pauses(engine):
        return [e for e in engine.store.read_all() if e.type == EV_PAUSE]

    plain = make_engine(tmp_path / "plain")
    plain.store.append(EV_PAUSE, {"reason": "operator stop (`looplab stop`)"})
    assert auto_pause_is_redundant(fold(plain.store.read_all()))
    _write(plain)
    assert len(_pauses(plain)) == 1, "a plain pause already says everything an auto-pause would"

    draining = make_engine(tmp_path / "drain")
    draining.store.append(EV_PAUSE, dict(_DRAIN))
    assert not auto_pause_is_redundant(fold(draining.store.read_all()))
    _write(draining)
    assert len(_pauses(draining)) == 2, "under a drain the auto-pause must be written"
    state = fold(draining.store.read_all())
    assert state.paused and not SpeculationMixin._pause_drains_builds(state), "…and it ends the drain"
    assert auto_pause_is_redundant(state), "every sibling after it finds the run plainly paused"


# ------------------------------------------------------------ end to end, through `engine.run`

class _Gate:
    def __init__(self, hold_when=None):
        self.started = threading.Event()
        self.release = threading.Event()
        self.cards: list = []            # the Cards whose build HELD, in order
        self.hold_when = hold_when       # None: every build holds until released


class _GatedToyDeveloper(ToyObjectiveDeveloper):
    """The PRODUCER pairs' Developer: its build holds until the test releases it. Serial builds use
    the engine's own, ungated Developer, so the run keeps moving until the pause lands."""

    def __init__(self, gate: _Gate):
        super().__init__()
        self._gate = gate

    def implement(self, idea: Idea) -> str:
        if self._gate.hold_when is None or self._gate.hold_when():
            self._gate.cards.append(idea.card_id)
            self._gate.started.set()
            if not self._gate.release.wait(60):
                raise RuntimeError("the test never released this build")
        return super().implement(idea)


def _gated(tmp_path, *, width: int, name: str, hold_when=None):
    from looplab.adapters.toytask import ToyTask
    from tests.factories import make_engine

    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    gate = _Gate(hold_when)

    def factory():
        researcher, _developer = task.build_roles()
        return researcher, _GatedToyDeveloper(gate)

    engine = make_engine(tmp_path / name, task=task, max_nodes=40, n_seeds=3,
                         card_driven_selection=True, speculation_depth=1, role_factory=factory,
                         llm_parallel=width)
    if not engine._speculation_enabled() or engine._producer_role_pair() is None:
        pytest.skip("this build does not admit a spelled positive depth on the toy adapter")
    return engine, gate


def _run_while(engine, gate, controller) -> dict:
    """`engine.run`, while `controller(store, seen)` plays the operator from another thread once the
    first speculative build is holding. The gate is released in a `finally`, so a controller that
    fails ends the build instead of hanging the suite; `seen["token"]` lets it run an engine
    coroutine on the engine's own loop, as an adopted evaluation would."""
    seen: dict = {}

    def body():
        try:
            if not gate.started.wait(90):
                seen["error"] = "no speculative build ever started"
                return
            controller(EventStore(engine.run_dir / "events.jsonl"), seen)
        except BaseException as exc:  # noqa: BLE001 — reported as this test's failure below
            seen.setdefault("error", f"the controller raised {exc!r}")
        finally:
            gate.release.set()

    async def main():
        seen["token"] = anyio.lowlevel.current_token()
        return await engine.run()

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    anyio.run(main)
    thread.join(timeout=10)
    assert "error" not in seen, seen.get("error")
    return seen


def _await(seen, what: str, predicate, seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            seen["error"] = what
            return False
        time.sleep(0.1)
    return True


def _closes_after(store, card_id, seq):
    return [e for e in store.read_all()
            if e.type == EV_CARD_BUILD_DONE and e.seq > seq and e.data.get("card_id") == card_id]


def _card_closes_after(engine, card_id, seq):
    return _closes_after(engine.store, card_id, seq)


def _drive(tmp_path, *, drain: bool, width: int):
    engine, gate = _gated(tmp_path, width=width, name=f"drain-{drain}-{width}")

    def controller(store, seen):
        row = {"reason": "test stop", **({"drain_builds": True} if drain else {})}
        seen["pause_seq"] = store.append(EV_PAUSE, row).seq
        # Hold the build while the engine acts on the pause: a plain stop closes the head at once.
        time.sleep(3.0)
        seen["released_at"] = len(store.read_all())

    return engine, gate, _run_while(engine, gate, controller)


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


def test_a_drain_commits_nothing_past_the_eval_budget_and_stops_waiting_on_what_it_closed(
        tmp_path, monkeypatch):
    """Width 2: the build is ADOPTED, so the outer loop's pause branch is what commits it — and that
    branch ran before the loop derived `max_es`, so it committed with no ceiling in view: a node past
    the budget, which `looplab resume` would finish the run over without evaluating. Now the head is
    closed `eval_budget_exhausted` while its producer still runs, and the drain stops polling for a
    producer whose request it closed: the loop reaches its exit drain with the build still held.

    MUTATIONS: pass None in the pause branch (the head stays open and the build commits past the
    budget); count every in-flight producer in `_draining_builds_in_flight` (the loop polls on while
    the closed build is held); read the bare terminal intent in the close's reason ladder (the close
    says `run_is_stopping`, which is not why this build was refused)."""
    run_dir = tmp_path / "drain-budget"
    # The first builds pass (the run blocks behind a held FIRST build, so nothing would ever be
    # evaluated); the first one to start after an evaluation second is charged holds.
    engine, gate = _gated(tmp_path, width=2, name="drain-budget", hold_when=lambda: fold(
        EventStore(run_dir / "events.jsonl").read_all()).total_eval_seconds > 0)
    # The loop's own exit drain (`_run_with_llm_broker`'s fall-through), observed rather than
    # replaced: it WAITS for the adopted producer, so the `run_loop_exited` row that follows it
    # cannot say whether the drain's poll let go first.
    loop_left, exit_drain = threading.Event(), engine._drain_adopted_evals

    async def _observed_exit_drain():
        if fold(engine.store.read_all()).paused:
            loop_left.set()
        await exit_drain()

    monkeypatch.setattr(engine, "_drain_adopted_evals", _observed_exit_drain)

    def controller(store, seen):
        spent: dict = {}

        def _charged():
            spent["s"] = fold(store.read_all()).total_eval_seconds
            return spent["s"] > 0

        if not _await(seen, "no evaluation second was ever charged", _charged, 60.0):
            return
        seen["pause_seq"] = store.append(EV_PAUSE, dict(_DRAIN)).seq
        # The operator lowers the ceiling below what is already spent — AFTER the pause, so no loop
        # turn ever sees the ceiling without the drain (its eval-budget gate would finish the run).
        store.append(EV_BUDGET_EXTEND, {"max_eval_seconds": spent["s"] / 2})
        card = gate.cards[0]
        if not _await(seen, "the drain never closed the build the eval budget refuses",
                      lambda: _closes_after(store, card, seen["pause_seq"])):
            return
        if not _await(seen, "the drain kept polling a build whose request it had closed",
                      loop_left.is_set):
            return
        seen["released_at"] = len(store.read_all())

    seen = _run_while(engine, gate, controller)
    card = gate.cards[0]
    closes = _card_closes_after(engine, card, seen["pause_seq"])
    assert closes and closes[0].data.get("skipped") == "stale", [c.data for c in closes]
    assert closes[0].data.get("skipped_reason") == "eval_budget_exhausted", closes[0].data
    assert closes[0].seq < seen["released_at"], "closed while its producer was still running"
    assert not [e for e in engine.store.read_all()
                if e.type == EV_NODE_CREATED and e.seq > seen["pause_seq"]], (
        "a node was committed past the eval-seconds budget")


@pytest.mark.parametrize("cancel,width", [("plain_stop", 1), ("plain_stop", 2),
                                          ("provider_outage", 2)])
def test_a_later_pause_cancels_the_drain_and_the_held_build_is_closed_as_a_plain_stop_would(
        tmp_path, cancel, width):
    """The drain takes hold (the held build's head stays open), then a later pause lands: the
    operator's plain `looplab stop`, or the engine's own provider circuit breaker run on the
    engine's loop the way an adopted evaluation's repair runs it. The build is then closed
    `stale`/`run_is_stopping` while its producer is still held — never committed, never a
    `producer_failed` against the dead endpoint. MUTATIONS: the fold's cancel (the head stays open
    and this times out); the writer's drain-aware guard (no cancelling row is ever written)."""
    engine, gate = _gated(tmp_path, width=width, name=f"cancel-{cancel}-{width}")

    def controller(store, seen):
        seen["pause_seq"] = store.append(EV_PAUSE, dict(_DRAIN)).seq
        card = gate.cards[0]
        time.sleep(1.5)
        if _closes_after(store, card, seen["pause_seq"]):
            seen["error"] = "precondition: the drain did not hold the running build's head open"
            return
        if cancel == "plain_stop":
            from looplab.cli.run_cmds import stop
            stop(engine.run_dir)                     # the operator's own `looplab stop`, no flag
        else:
            anyio.from_thread.run(engine._auto_pause_provider_failure,
                                  "the Developer's LLM provider failed", token=seen["token"])
        if not _await(seen, "the cancelled drain never closed the held build",
                      lambda: _closes_after(store, card, seen["pause_seq"])):
            return
        seen["released_at"] = len(store.read_all())

    seen = _run_while(engine, gate, controller)
    card = gate.cards[0]
    closes = _card_closes_after(engine, card, seen["pause_seq"])
    # `producer_cancelled`: the close lands while the held producer still runs, so it also tells that
    # build to stop and says so on the row (doc 68 68.7) — the plain stop's own close, since then.
    assert closes[0].data == {"card_id": card, "generation": closes[0].data["generation"],
                              "skipped": "stale", "skipped_reason": "run_is_stopping",
                              "producer_cancelled": True}, closes[0].data
    assert closes[0].seq < seen["released_at"], "closed while its producer was still running"
    assert not [c for c in closes if c.data.get("skipped") == "producer_failed"]
    assert not [e for e in engine.store.read_all()
                if e.type == EV_NODE_CREATED and e.seq > seen["pause_seq"]
                and (e.data.get("idea") or {}).get("card_id") == card]
    pauses = [e for e in engine.store.read_all() if e.type == EV_PAUSE and e.seq > seen["pause_seq"]]
    assert pauses, "the cancelling pause was written"
    state = fold(engine.store.read_all())
    assert state.paused and not SpeculationMixin._pause_drains_builds(state)


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


def _unboxed(text: str) -> str:
    """A `typer.BadParameter` renders in a Rich panel wrapped to the terminal's width, so a phrase
    can land across two box lines ("is │\n│ already") — at 80 columns CI failed all four cases
    below, locally one. Colour codes and box-drawing characters removed and the whitespace
    collapsed, the refusal reads whole at any width."""
    return " ".join(re.sub(r"[\u2500-\u257f]", " ", re.sub(r"\x1b\[[0-9;]*m", "", text)).split())


@pytest.mark.parametrize("halt", [
    (EV_PAUSE, {"reason": "operator stop (`looplab stop`)"}),
    (EV_PAUSE, _AUTO_PAUSE),
    (EV_PAUSE, _DRAIN),
    ("run_abort", {"reason": "finalized"}),
], ids=["plain_stop", "engine_auto_pause", "drain_standing", "finalizing"])
def test_the_flag_is_refused_on_a_run_already_halted_and_nothing_is_appended(tmp_path, halt):
    """The fold starts a drain only on the pause that takes effect, so on a halted run the flag was
    dropped while the command promised it. MUTATION: drop the refusal — exit 0, a row, no drain."""
    rd = _run_dir(tmp_path)
    EventStore(rd / "events.jsonl").append(*halt)
    before = len(EventStore(rd / "events.jsonl").read_all())
    out = CliRunner().invoke(app, ["stop", str(rd), "--drain-builds"])
    assert out.exit_code == 2, out.output
    flat = _unboxed(out.output)
    assert "is already" in flat and "Nothing was appended" in flat, flat
    assert len(EventStore(rd / "events.jsonl").read_all()) == before, "nothing may be appended"
    assert CliRunner().invoke(app, ["stop", str(rd)]).exit_code == 0, "a plain stop still records"


def test_a_drain_an_earlier_pause_swallowed_fails_loudly(tmp_path, monkeypatch):
    """The refusal reads the log one append before the stop row: an engine auto-pause landing in
    between halts the run first, and the drain row then changes nothing. Verified after the append —
    the stop is recorded, the command says the drain did not take effect, exit 1. MUTATION: drop the
    verification and this prints "builds already running will finish and commit first" at exit 0."""
    import looplab.cli.run_cmds as run_cmds

    rd = _run_dir(tmp_path)

    class _RacedStore(EventStore):
        def append(self, type, data, **kwargs):
            if type == EV_PAUSE and data.get("drain_builds") is True:
                super().append(EV_PAUSE, dict(_AUTO_PAUSE))       # the engine gets there first
            return super().append(type, data, **kwargs)

    monkeypatch.setattr(run_cmds, "_require_run_dir",
                        lambda run_dir, **_kw: _RacedStore(Path(run_dir) / "events.jsonl"))
    out = CliRunner().invoke(app, ["stop", str(rd), "--drain-builds"])
    assert out.exit_code == 1, out.output
    assert "did NOT take effect" in out.output and "already halted" in out.output
    assert "will finish and commit first" not in out.output
    state = fold(EventStore(rd / "events.jsonl").read_all())
    assert state.paused and not state.pause_drain_builds


@FLOCK
def test_a_timeout_names_the_builds_it_gave_up_on(tmp_path):
    """Under a drain the builds are what the engine is most likely still waiting on; the line named
    only evaluations. MUTATION: the old sentence."""
    rd = _run_dir(tmp_path)
    EventStore(rd / "events.jsonl").append(EV_CARD_BUILD_REQUESTED,
                                           {"card_id": "card-7", "generation": 0})
    release, held = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_lock, args=(rd, release, held), daemon=True)
    holder.start()
    assert held.wait(5)
    try:
        out = CliRunner().invoke(app, ["stop", str(rd), "--wait", "--timeout", "0.5",
                                       "--drain-builds"])
    finally:
        release.set()
        holder.join(5)
    assert out.exit_code == 1, out.output
    assert "gave up after 0.5s" in out.output
    assert "Card build(s) it is draining commit or close (still open: card-7)" in out.output


@FLOCK
def test_the_wait_says_when_a_later_pause_cancelled_the_drain(tmp_path):
    """A drain a later pause ended closed its builds the plain stop's way; the per-Card line alone
    ("closed without a node") would not say why."""
    rd = _run_dir(tmp_path)
    EventStore(rd / "events.jsonl").append(EV_CARD_BUILD_REQUESTED,
                                           {"card_id": "card-7", "generation": 0})
    release, held = threading.Event(), threading.Event()

    def _cancelled_and_closed():
        store = EventStore(rd / "events.jsonl")
        store.append(EV_PAUSE, {"reason": "operator stop (`looplab stop`)"})
        store.append(EV_CARD_BUILD_DONE, {"card_id": "card-7", "generation": 0, "skipped": "stale",
                                          "skipped_reason": "run_is_stopping"})

    holder = threading.Thread(target=_hold_lock, args=(rd, release, held),
                              kwargs={"land": _cancelled_and_closed}, daemon=True)
    holder.start()
    assert held.wait(5)
    threading.Timer(1.0, release.set).start()
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait", "--drain-builds"])
    holder.join(5)
    assert out.exit_code == 0, out.output
    assert "and for 1 build(s) to finish and commit (card-7)" in out.output
    assert ("the drain did not hold to the end: a later pause cancelled it "
            "(operator stop (`looplab stop`))") in out.output
    assert "card-7: closed without a node" in out.output
