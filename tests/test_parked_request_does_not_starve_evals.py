"""A node-creating operator request parked on the node budget starves nothing, and is SAID (doc 68 68.8).

MEASURED on `minionerec-backbones-v10` (2026-09-27, `runs-r/.../events.jsonl`): after a restart the
engine served inject idx 12 (seq 5237, node 18, 02:10:09) and the node budget was spent — `max_nodes`
16 plus three refunds, ids 0..18. Inject idx 13 parked in `forced_requests.py::_defer_for_node_budget`,
whose True sent the run loop to `continue` ABOVE the speculation block, the cadences and the eval
dispatch: node 18, built and pending, was not evaluated for 20 minutes of idle GPUs, until the
operator's `budget_extend {add_nodes: 12}` (seq 5238, 02:29:37). Meanwhile every `inject_node` command
read `succeeded` — the postcondition is the engine's ACK — and nothing said the request was waiting.

What these pin, through a real `Engine.run` on both dispatch paths (the Card session the incident ran
on, and the plain dispatcher):
  * the pending node is EVALUATED while the next request is parked — its terminal lands before the
    operator's `budget_extend` (pre-fix it could only land after);
  * the park is said once, with the numbers (`operator_request_parked`), and the attention feed and
    `looplab inspect` report it until the request is served — then stop;
  * the parked request still gets the slot the extension opens, and the run finishes normally.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.events.eventstore import EventStore
from looplab.events.parked_requests import open_parked_requests, parked_request_detail
from looplab.events.replay import fold
from looplab.events.types import (EV_BUDGET_EXTEND, EV_INJECT_NODE, EV_NODE_EVALUATED,
                                  EV_NODE_FAILED, EV_OPERATOR_REQUEST_PARKED)
from tests.factories import make_engine

_INJECT = {"idea": {"operator": "manual", "params": {"x": 1.0, "y": 1.0},
                    "rationale": "operator idea"}}
# The watcher extends the budget as soon as node 0's terminal lands, or at this deadline — which is
# what a pre-fix run needs, since it never evaluates node 0 while the request is parked.
_EXTEND_DEADLINE_S = 20.0


def _engine(run_dir, *, card_mode: bool):
    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    overrides = (dict(card_driven_selection=True, speculation_depth=2) if card_mode else {})
    engine = make_engine(run_dir, task=task, n_seeds=0, max_nodes=1, **overrides)
    # A CPU envelope, whatever the host has (the same shape the occupancy tests use).
    engine._gpu_ids = []
    engine._gpu_physical_ids = {}
    engine._gpu_mem = {}
    engine._free_gpus = []
    return engine


def _terminal_seq(events, node_id):
    return next((e.seq for e in events
                 if e.type in (EV_NODE_EVALUATED, EV_NODE_FAILED)
                 and (e.data or {}).get("node_id") == node_id), None)


@pytest.mark.parametrize("card_mode", [True, False], ids=["card-session", "plain-dispatch"])
def test_a_parked_inject_does_not_starve_the_evaluation_of_an_existing_node(tmp_path, card_mode):
    engine = _engine(tmp_path / "run", card_mode=card_mode)
    server = EventStore(engine.store.path)            # the server process's writer
    # Two injects on a one-node budget: the first becomes node 0, the second must wait for a slot.
    server.append(EV_INJECT_NODE, dict(_INJECT))
    server.append(EV_INJECT_NODE, dict(_INJECT, idea=dict(_INJECT["idea"], params={"x": 2.0,
                                                                                   "y": 2.0})))
    seen: dict = {}
    stop = threading.Event()

    def operator():
        # Extend the budget once node 0 has its terminal (the fix) — or at the deadline (pre-fix,
        # where node 0 is never evaluated while the request is parked).
        deadline = time.monotonic() + _EXTEND_DEADLINE_S
        while not stop.is_set() and time.monotonic() < deadline:
            events = server.read_all()
            if _terminal_seq(events, 0) is not None:
                seen["parked_rows"] = [e.data for e in events
                                       if e.type == EV_OPERATOR_REQUEST_PARKED]
                state = fold(events)
                seen["open"] = [p["detail"] for p in open_parked_requests(events, state)]
                break
            time.sleep(0.05)
        seen["extend_seq"] = server.append(EV_BUDGET_EXTEND, {"add_nodes": 1}).seq

    watcher = threading.Thread(target=operator, daemon=True)
    watcher.start()
    try:
        final = anyio.run(engine.run)
    finally:
        stop.set()
        watcher.join(5)

    events = engine.store.read_all()
    node0_terminal = _terminal_seq(events, 0)
    assert node0_terminal is not None, "node 0 was never evaluated"
    assert node0_terminal < seen["extend_seq"], (
        "node 0 was evaluated only AFTER the operator extended the node budget: a request parked on "
        "the budget held the run loop above the eval dispatch (the v10 20-minute stall)")

    parked = seen.get("parked_rows") or []
    assert parked, "the park was never said — the operator is left reading `succeeded`"
    row = parked[0]
    assert (row["request"], row["idx"], row["reason"]) == ("inject", 1, "node_budget"), row
    assert (row["reserved"], row["held_by_card_requests"], row["limit"]) == (1, 0, 1), row
    assert row["detail"] == parked_request_detail(row)
    assert "budget_extend add_nodes" in row["detail"]
    assert len(parked) == 1, f"one row per parking episode, not one per poll: {parked}"
    assert seen["open"] == [row["detail"]], "the reader must report the open park while it waits"

    # The extension admitted the SAME request exactly once, and the run ended normally.
    assert final.injects_done == 2 and len(final.nodes) == 2
    assert _terminal_seq(events, 1) is not None, "the admitted inject's node was never evaluated"
    assert final.finished, final.stop_reason
    assert open_parked_requests(events, final) == [], "a served request is no longer parked"


def test_the_attention_feed_reports_a_park_until_its_request_is_served(tmp_path):
    """The operator's inbox: one `request_parked` item per parking episode, actionable, with the
    engine's own sentence; its id survives the numbers moving; gone once the receipt lands."""
    from looplab.serve.attention import ATTENTION_NEEDS_ACTION_KINDS, project_run_attention

    engine = _engine(tmp_path / "run", card_mode=False)
    engine.store.append("run_started", {"run_id": "run", "task_id": "toy", "goal": "g",
                                        "direction": "min"})
    engine.store.append(EV_INJECT_NODE, dict(_INJECT))
    engine.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                         "idea": {"operator": "draft"}, "code": ""})
    parked = engine._parked_forced_request(fold(engine.store.read_all()))
    assert parked is not None and parked["request"] == "inject" and parked["idx"] == 0
    assert engine._note_parked_forced_request(parked) is True
    assert engine._note_parked_forced_request(parked) is False, "said twice for one episode"

    def items():
        return [i for i in project_run_attention("run", engine.store.read_all(),
                                                 engine_running=True)
                if i["kind"] == "request_parked"]

    first = items()
    assert len(first) == 1 and first[0]["active"] and first[0]["browser"]
    assert first[0]["severity"] == "action" and "request_parked" in ATTENTION_NEEDS_ACTION_KINDS
    assert first[0]["detail"] == parked["detail"]
    # The numbers move (an open Card request now holds a slot): a new row, the SAME item id.
    moved = dict(parked, held_by_card_requests=1)
    moved["detail"] = parked_request_detail(moved)
    assert engine._note_parked_forced_request(moved) is True
    second = items()
    assert [i["id"] for i in second] == [first[0]["id"]]
    assert "held by open Card build request" in second[0]["detail"]
    # Served: the receipt lands and the item is gone.
    engine.store.append("inject_done", {"idx": 0})
    assert items() == []


def test_a_request_that_would_be_spent_is_never_reported_parked(tmp_path):
    """Only a head that WAITS parks: an inject the validator refuses is spent by the serve path, so
    calling it "waiting for a slot" would send the operator to extend a budget for nothing."""
    engine = _engine(tmp_path / "run", card_mode=False)
    engine.store.append("run_started", {"run_id": "run", "task_id": "toy", "goal": "g",
                                        "direction": "min"})
    engine.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                         "idea": {"operator": "draft"}, "code": ""})
    engine.store.append(EV_INJECT_NODE, {"idea": {"operator": "debug"}})    # refused: F5
    assert engine._parked_forced_request(fold(engine.store.read_all())) is None


def test_the_empty_action_ladder_never_finishes_the_run_over_a_parked_request(tmp_path, monkeypatch):
    """The park's wait used to keep this by never letting the loop reach the ladder; a turn that now
    falls through to its work must keep it itself. Driven with a selector that has NOTHING to start
    (the shape of a pending node the policy will not dispatch): without the guard the empty-action
    ladder finishes the run with the operator's second inject still queued — stranded for good."""
    from looplab.events.types import EV_RUN_FINISHED

    engine = _engine(tmp_path / "run", card_mode=False)
    server = EventStore(engine.store.path)
    server.append(EV_INJECT_NODE, dict(_INJECT))
    server.append(EV_INJECT_NODE, dict(_INJECT, idea=dict(_INJECT["idea"], params={"x": 2.0,
                                                                                   "y": 2.0})))
    monkeypatch.setattr(engine, "_select_actions", lambda _state: [])
    seen: dict = {}

    def operator():
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            events = server.read_all()
            seen["finished_early"] = any(e.type == EV_RUN_FINISHED for e in events)
            if seen["finished_early"] or any(e.type == EV_OPERATOR_REQUEST_PARKED
                                             for e in events):
                break
            time.sleep(0.05)
        time.sleep(1.0)                       # a few loop turns while parked
        seen["finished_early"] = any(e.type == EV_RUN_FINISHED for e in server.read_all())
        seen["extend_seq"] = server.append(EV_BUDGET_EXTEND, {"add_nodes": 1}).seq

    watcher = threading.Thread(target=operator, daemon=True)
    watcher.start()
    final = anyio.run(engine.run)
    watcher.join(15)

    assert not seen["finished_early"], "the run FINISHED over a request parked on the node budget"
    events = engine.store.read_all()
    finished = [e.seq for e in events if e.type == EV_RUN_FINISHED]
    assert finished and finished[0] > seen["extend_seq"]
    assert final.injects_done == 2, "the parked request never got the slot the extension opened"


def test_looplab_inspect_says_what_is_parked_and_stops_once_it_is_served(tmp_path):
    """The CLI half of the signal, over the real `app`: "what is this run doing" names the request
    that waits for a node slot, with the remedy — and says nothing once its receipt has landed."""
    from typer.testing import CliRunner

    from looplab.cli import app

    engine = _engine(tmp_path / "run", card_mode=False)
    engine.store.append("run_started", {"run_id": "run", "task_id": "toy", "goal": "g",
                                        "direction": "min"})
    engine.store.append(EV_INJECT_NODE, dict(_INJECT))
    engine.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                         "idea": {"operator": "draft"}, "code": ""})
    engine._note_parked_forced_request(engine._parked_forced_request(fold(engine.store.read_all())))

    def inspect_output() -> str:
        result = CliRunner().invoke(app, ["inspect", str(engine.run_dir)])
        assert result.exit_code == 0, result.output
        return result.output

    parked_lines = [ln for ln in inspect_output().splitlines() if ln.startswith("parked: ")]
    assert parked_lines == [
        "parked: inject #0 waits for a node slot: the node budget is spent (1 of 1 slots taken — "
        "1 node id reserved). `budget_extend add_nodes` admits it now."], parked_lines
    engine.store.append("inject_done", {"idx": 0})
    assert "parked: " not in inspect_output()
