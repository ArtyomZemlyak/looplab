"""A Card build whose request closes is STOPPED, not paid to the end (doc 68 68.7).

MEASURED on `minionerec-backbones-v10` (2026-09-27, `runs-r/.../events.jsonl`): card-16's build was
requested at 01:20:35 (seq 5101) and its `Developer·stages` phase started at 01:20:38; the operator
dropped card-16 at 01:33:11 (seq 5142, "do not rebuild"). The build ran on — stages to 01:44:20
(seq 5179), then a FRESH `Developer·plan` for the dropped card at 01:47:21 (seq 5181, its memory
queries about the dropped card's "model soup" idea) — and its open request held the run's last node
slot until an operator `restart` closed it `run_is_stopping` at 02:05:26 (seq 5218), after which the
Developer loop still ran until the process got SIGTERM at 02:09:19.

Two defects, both in `engine/speculation.py`: the dead-Card close of `_serve_card_builds` skipped a
head whose producer was live ("never strand a live producer"), so a drop was never observed while the
build ran; and nothing could tell a running build to stop — a close with the producer live (the
`run_is_stopping` one) closed the request and left the Developer working for it.

What these pin, each on the path the incident took:
  * a drop observed mid-build closes the request at once (`stale`/`card_dropped`, with
    `producer_cancelled`), the running phase ends at its next turn boundary and NO further Developer
    phase starts — driven through the real Card session and the real `run_phase`;
  * a stopping run's close cancels the live build the same way, so the session (and the process)
    does not wait for it;
  * `run_phase` under a `phase_cancel_scope`: refuses to start, ends at a turn boundary, and — with
    the REAL `drive_tool_loop` and a Developer-shaped wall budget — cuts a streamed generation in
    flight, which a token published only through `cancel_check_scope` could not (the loop's own
    per-turn predicate shadows it);
  * outside a scope `run_phase` hands `drive_tool_loop` exactly what it did before.
"""
from __future__ import annotations

import threading
import time

import anyio
import pytest

import looplab.agents.agent as agent_module
from looplab.agents.tool_loop import phase_cancel_scope, phase_cancelled
from looplab.core.errors import LLMCancelled, PhaseCancelled
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_BUILD_DONE, EV_CARD_DROPPED, EV_NODE_CREATED, EV_PAUSE
# The receipt fixture is AUTOUSE in its own module and stays autouse when imported here.
from tests.test_card_speculation_engine import (  # noqa: F401  (imported for its autouse effect)
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _Developer,
    _engine,
    _request,
    _start,
    _without_research,
)

_EMIT = {"type": "function", "function": {"name": "done", "parameters": {"type": "object"}}}
_PHASES = ("Developer·stages", "Developer·plan", "Developer·implement step 1/2")
# How long one simulated phase would run if nothing stopped it. A red test waits this out instead of
# hanging, and every assertion on "stopped promptly" is an order of magnitude below it.
_PHASE_S = 20.0


class _PhasedDeveloper(_Developer):
    """A Card producer shaped like the repo Developer: its build is a CHAIN of `run_phase` calls.

    `drive_tool_loop` itself is stubbed (through `looplab.agents.agent.drive_tool_loop`, the
    documented seam) by `_long_phase_loop` below, which runs "turns" until its `cancel_check` fires.
    Everything between — `run_phase`'s gate, the scope the producer opens, the engine's close — is
    the real code."""

    def __init__(self):
        super().__init__()
        self.phases_started: list[str] = []
        self.phases_cancelled: list[str] = []
        self.first_phase = threading.Event()
        self.raised: list[BaseException] = []

    def implement(self, _idea):
        self.calls += 1
        try:
            for label in _PHASES:
                agent_module.run_phase(
                    None, None, [{"role": "user", "content": "build it"}], _EMIT,
                    label=label, handoff=False, finalize=lambda args: args,
                    fallback=lambda _messages: None)
        except PhaseCancelled as exc:
            self.raised.append(exc)
            raise
        return self.code


def _long_phase_loop(developer: _PhasedDeveloper):
    def loop(client, tools, messages, emit_spec, *, finalize, fallback, phase_label="",
             cancel_check=None, **_kw):
        developer.phases_started.append(phase_label)
        developer.first_phase.set()
        deadline = time.monotonic() + _PHASE_S
        while time.monotonic() < deadline:
            if cancel_check is not None and cancel_check():
                developer.phases_cancelled.append(phase_label)
                return fallback(messages)             # the loop's own exit on a cancelled turn
            time.sleep(0.01)
        return finalize({})
    return loop


async def _until(predicate, *, timeout=15.0, step=0.02):
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(step)


def _session_with_operator(engine, operator):
    """Run ONE Card session beside an `operator` coroutine; returns the session's wall seconds."""
    took: dict = {}

    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(operator)
            t0 = time.monotonic()
            with anyio.fail_after(60):
                await engine._run_card_session([], fold(engine.store.read_all()), None)
            took["s"] = time.monotonic() - t0

    anyio.run(scenario)
    return took["s"]


def _done_rows(engine):
    return [e for e in engine.store.read_all() if e.type == EV_CARD_BUILD_DONE]


def test_a_card_dropped_mid_build_closes_its_request_and_stops_the_build(tmp_path, monkeypatch):
    """The v10 incident, end to end on the Card session: the drop lands while the first Developer
    phase runs; the request closes `card_dropped` at once and the next phase NEVER starts.

    Pre-fix this fails two ways: the session keeps the head open while the producer is live (no
    `card_build_done` until the whole chain has run, ~60 s here), and every phase of the chain runs
    to its end (`phases_started` lists all three)."""
    developer = _PhasedDeveloper()
    monkeypatch.setattr(agent_module, "drive_tool_loop", _long_phase_loop(developer))
    engine, _ = _engine(tmp_path / "run", producer=developer)
    _start(engine)
    _add_ready_draft(engine)
    _request(engine)
    _without_research(monkeypatch, engine)
    server = EventStore(engine.store.path)           # a second writer, like the server process
    seen: dict = {}

    async def operator():
        await _until(developer.first_phase.is_set)
        dropped = server.append(EV_CARD_DROPPED, {
            "id": "card-7", "reason": "operator: do not rebuild", "dropped_by": "operator"})
        t0 = time.monotonic()
        await _until(lambda: bool(_done_rows(engine)))
        seen["closed_after_s"] = time.monotonic() - t0
        seen["drop_seq"] = dropped.seq
        await _until(lambda: developer.phases_cancelled != [])
        seen["cancelled_after_s"] = time.monotonic() - t0

    took = _session_with_operator(engine, operator)

    assert developer.phases_started == [_PHASES[0]], (
        f"a Developer phase STARTED for a dropped Card: {developer.phases_started}")
    assert developer.phases_cancelled == [_PHASES[0]], "the running phase never saw the cancel"
    assert developer.raised and isinstance(developer.raised[0], PhaseCancelled)
    assert seen["closed_after_s"] < 5.0, "the request stayed open while the dropped build ran"
    assert seen["cancelled_after_s"] < 5.0
    assert took < _PHASE_S / 2, f"the session waited the build out ({took:.1f}s)"

    done = _done_rows(engine)
    assert len(done) == 1
    row = done[0].data
    assert (row.get("skipped"), row.get("skipped_reason")) == ("stale", "card_dropped"), row
    assert row.get("producer_cancelled") is True, "the row must say a live build was stopped"
    assert done[0].seq > seen["drop_seq"]
    events = engine.store.read_all()
    assert not [e for e in events if e.type == EV_NODE_CREATED], "a dropped Card minted a node"
    state = fold(events)
    assert state.card_builds_done == 1 and not engine._outstanding_requests(state)
    # Nothing of the cancelled build is kept: no result slot, no inflight key, no token.
    assert engine._spec_builds == {} and engine._spec_build_inflight == set()
    assert engine._spec_build_cancel == {}


def test_a_stopping_run_cancels_the_live_build_its_close_abandons(tmp_path, monkeypatch):
    """`run_is_stopping` closed the head with the producer live — and left it running (v10: the
    Developer loop outlived the close by four minutes, until SIGTERM). The close now stops it, so
    the session returns in seconds instead of waiting for the chain."""
    developer = _PhasedDeveloper()
    monkeypatch.setattr(agent_module, "drive_tool_loop", _long_phase_loop(developer))
    engine, _ = _engine(tmp_path / "run", producer=developer)
    _start(engine)
    _add_ready_draft(engine)
    _request(engine)
    _without_research(monkeypatch, engine)
    server = EventStore(engine.store.path)

    async def operator():
        await _until(developer.first_phase.is_set)
        server.append(EV_PAUSE, {"reason": "operator stop (test)"})

    took = _session_with_operator(engine, operator)

    done = _done_rows(engine)
    assert len(done) == 1
    assert (done[0].data.get("skipped_reason"), done[0].data.get("producer_cancelled")) == (
        "run_is_stopping", True), done[0].data
    assert developer.phases_started == [_PHASES[0]]
    assert developer.phases_cancelled == [_PHASES[0]]
    assert took < _PHASE_S / 2, f"the stopping session waited the abandoned build out ({took:.1f}s)"
    assert engine._spec_build_inflight == set() and engine._spec_builds == {}


def test_a_close_with_no_live_build_says_nothing_about_cancelling(tmp_path):
    """The flag is the record of a STOPPED build, never decoration: a crash-recovered dropped head
    (no producer in this process) closes exactly as it always did."""
    engine, _ = _engine(tmp_path / "run")
    _start(engine)
    _add_ready_draft(engine)
    _request(engine)
    engine.store.append(EV_CARD_DROPPED, {"id": "card-7", "reason": "gone", "dropped_by": "operator"})
    assert engine._serve_card_builds() is True
    row = _done_rows(engine)[0].data
    assert row.get("skipped_reason") == "card_dropped"
    assert "producer_cancelled" not in row


def test_a_request_closed_elsewhere_still_cancels_its_build(tmp_path):
    """The orphan sweep is the net under every close path: a build running for a request that is no
    longer outstanding is told to stop, whoever closed the request."""
    engine, _ = _engine(tmp_path / "run")
    _start(engine)
    _add_ready_draft(engine)
    request = _request(engine)
    key = engine._request_key(request)
    engine._ensure_speculation_state()
    token = threading.Event()
    engine._spec_build_inflight.add(key)          # as `_start_request_producer` leaves it
    engine._spec_build_cancel[key] = token
    engine._discard_orphaned_spec_results(fold(engine.store.read_all()))
    assert not token.is_set(), "an OPEN request's build must never be cancelled"
    # Another path closes the request (a direct close, as a terminal gate or recovery would).
    engine._spec_build_inflight.discard(key)
    assert engine._append_card_build_done(request, skipped="stale", skipped_reason="card_gone")
    engine._spec_build_inflight.add(key)
    engine._discard_orphaned_spec_results(fold(engine.store.read_all()))
    assert token.is_set(), "a build running for a CLOSED request was left running"


# ------------------------------------------------------------------ `run_phase` under the token

def _recording_loop(calls):
    def loop(client, tools, messages, emit_spec, **kwargs):
        calls.append(dict(kwargs))
        return kwargs["finalize"]({"ok": True})
    return loop


def test_outside_a_scope_run_phase_hands_the_loop_exactly_what_it_did_before(monkeypatch):
    calls: list = []
    monkeypatch.setattr(agent_module, "drive_tool_loop", _recording_loop(calls))
    out = agent_module.run_phase(None, None, [], _EMIT, label="Developer·plan", handoff=False,
                                 finalize=lambda a: a, fallback=lambda m: None)
    assert out == {"ok": True}
    assert "cancel_check" not in calls[0], "no scope, no token: the call must be byte-identical"
    assert not phase_cancelled()


def test_a_fired_scope_refuses_to_start_a_phase(monkeypatch):
    calls: list = []
    monkeypatch.setattr(agent_module, "drive_tool_loop", _recording_loop(calls))
    token = threading.Event()
    token.set()
    with phase_cancel_scope(token.is_set), pytest.raises(PhaseCancelled, match="not started"):
        agent_module.run_phase(None, None, [], _EMIT, label="Developer·plan",
                               finalize=lambda a: a, fallback=lambda m: None)
    assert calls == [], "a phase of cancelled work reached the loop"


def test_a_live_scope_hands_the_loop_its_token_composed_with_the_callers(monkeypatch):
    calls: list = []
    monkeypatch.setattr(agent_module, "drive_tool_loop", _recording_loop(calls))
    token = threading.Event()
    caller = {"cancelled": False}
    with phase_cancel_scope(token.is_set):
        agent_module.run_phase(None, None, [], _EMIT, label="Developer·plan", handoff=False,
                               finalize=lambda a: a, fallback=lambda m: None,
                               cancel_check=lambda: caller["cancelled"])
    check = calls[0]["cancel_check"]
    assert check() is False
    caller["cancelled"] = True
    assert check() is True, "the caller's own token must still reach the loop"
    caller["cancelled"] = False
    token.set()
    # Polled from ANOTHER thread, as a tool's watcher would: no scope there, same answer.
    answer: list = []
    worker = threading.Thread(target=lambda: answer.append(check()))
    worker.start()
    worker.join()
    assert answer == [True], "the composed token re-read the ContextVar instead of the predicate"


def test_a_phase_that_ends_on_the_token_raises_and_buys_no_handoff_summary(monkeypatch):
    token = threading.Event()
    summaries: list = []

    def loop(client, tools, messages, emit_spec, *, finalize, fallback, cancel_check=None, **_kw):
        token.set()                                   # the owner cancels mid-phase…
        assert cancel_check() is True                 # …the loop sees it at its turn boundary
        return fallback(messages)

    monkeypatch.setattr(agent_module, "drive_tool_loop", loop)
    monkeypatch.setattr(agent_module, "summarize_phase",
                        lambda *a, **k: summaries.append(1) or "brief")
    from looplab.agents.tool_loop import handoff_scope
    with handoff_scope(True), phase_cancel_scope(token.is_set), \
            pytest.raises(PhaseCancelled, match="turn boundary"):
        agent_module.run_phase(None, None, [], _EMIT, label="Developer·stages",
                               finalize=lambda a: a, fallback=lambda m: "half-done")
    assert summaries == [], "a cancelled phase must not buy the summary its successor would read"


class _StreamingClient:
    """A provider mid-generation: it reads the AMBIENT request token the way the streaming client
    does (`core/llm.py`, `request_cancelled()` per chunk), and raises `LLMCancelled` on it."""

    accountant = None

    def __init__(self):
        self.generating = threading.Event()

    def chat(self, messages, tools, tool_choice="auto"):
        from looplab.core.llm_transient import request_cancelled
        self.generating.set()
        deadline = time.monotonic() + _PHASE_S
        while time.monotonic() < deadline:
            if request_cancelled():
                raise LLMCancelled("the stream was cancelled mid-generation")
            time.sleep(0.01)
        return {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "done", "arguments": "{}"}}]}


def test_the_token_cuts_a_generation_in_flight_under_a_developer_wall_budget():
    """With the REAL loop. Every Developer session has a wall budget, and a loop with one publishes
    its OWN per-turn predicate around `client.chat` — which SHADOWS a token published only as the
    ambient request token. `run_phase` hands the owner's token to the loop as its `cancel_check`,
    and the loop folds that into the predicate the request reads."""
    client = _StreamingClient()
    token = threading.Event()
    outcome: dict = {}

    def build():
        with phase_cancel_scope(token.is_set):
            t0 = time.monotonic()
            try:
                agent_module.run_phase(client, None, [{"role": "user", "content": "go"}], _EMIT,
                                       label="Developer·stages", handoff=False,
                                       finalize=lambda a: a, fallback=lambda m: None,
                                       time_budget_s=1200.0)
            except PhaseCancelled as exc:
                outcome["raised"] = exc
            outcome["s"] = time.monotonic() - t0

    worker = threading.Thread(target=build)
    worker.start()
    assert client.generating.wait(5)
    token.set()
    worker.join(_PHASE_S + 5)
    assert "raised" in outcome, "the generation ran to its end: the token never reached the request"
    assert isinstance(outcome["raised"].__cause__, LLMCancelled)
    assert outcome["s"] < 5.0
