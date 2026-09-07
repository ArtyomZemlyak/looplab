"""State deltas on the run stream (doc 52 row 29): the differ's round trip, and the stream itself
sending a full frame first and a delta after — driven through the real server against a real log."""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from looplab.events.eventstore import EventStore
from looplab.events.state_delta import DELTA_VERSION, apply, diff
from looplab.serve.protocol import SSE_DONE, SSE_STATE, SSE_STATE_DELTA
from looplab.serve.server import make_app


@pytest.mark.parametrize("old,new", [
    ({}, {}),
    ({"a": 1}, {"a": 2}),
    ({"a": 1, "b": {"c": [1, 2]}}, {"b": {"c": [1, 2, 3], "d": None}, "e": "x"}),
    ({"nodes": {"0": {"metric": None, "status": "pending"}}},
     {"nodes": {"0": {"metric": 0.5, "status": "evaluated"}, "1": {"status": "pending"}}}),
    ({"list": [{"a": 1}]}, {"list": [{"a": 1}, {"b": 2}]}),
    ({"k": {"deep": {"x": 1}}}, {"k": 3}),
    ({"k": 3}, {"k": {"deep": {"x": 1}}}),
    ({"gone": {"x": 1}, "kept": 1}, {"kept": 1}),
])
def test_diff_then_apply_is_the_identity_and_never_mutates_the_base(old, new):
    frozen = json.dumps(old, sort_keys=True)
    ops = diff(old, new)
    assert apply(old, ops) == new
    assert json.dumps(old, sort_keys=True) == frozen, "the base is untouched"
    assert diff(new, new) == [] and apply(new, []) is new


def test_a_changed_list_arrives_whole_and_an_unchanged_subtree_costs_nothing():
    old = {"a": {"rows": [1, 2, 3], "big": {"x": list(range(100))}}, "b": 1}
    new = {"a": {"rows": [1, 2, 3, 4], "big": {"x": list(range(100))}}, "b": 1}
    assert diff(old, new) == [["set", ["a", "rows"], [1, 2, 3, 4]]]


def test_a_malformed_op_is_refused_not_guessed():
    for bad in ([["mov", ["a"], 1]], [["set", "a", 1]], [["del", []]], [["set", ["a"]]], ["x"]):
        with pytest.raises(ValueError):
            apply({"a": 1}, bad)


def _frames(stream) -> list:
    """`(event, payload)` pairs off a COMPLETE SSE body.

    Starlette's `TestClient` runs the whole ASGI response before `stream()` returns, so the run
    under test must reach the stream's own `done` (a folded `run_finished` with no live engine) or
    the test never gets the body — which is also why every frame, including the delta, is read
    after the fact rather than as it is emitted.
    """
    out, event, data = [], "", []
    for line in stream.iter_lines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data.append(line[6:])
        elif line == "" and data:
            out.append((event, json.loads("\n".join(data))))
            event, data = "", []
    return out


def _run(tmp_path):
    rd = tmp_path / "demo"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"x": 1.0}}, "code": ""})
    return store


def test_the_stream_sends_a_full_frame_then_a_delta_that_reproduces_the_next_full_state(tmp_path):
    store = _run(tmp_path)
    client = TestClient(make_app(tmp_path))

    def append_later():
        # After the first tick has emitted its full frame: one evaluation and the finish, so the
        # next tick emits ONE delta carrying both and then the stream's `done` ends the response.
        time.sleep(0.8)
        store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 0.5,
                                        "violations": []})
        store.append("run_finished", {"stop_reason": "budget"})
    threading.Thread(target=append_later, daemon=True).start()
    with client.stream("GET", "/api/runs/demo/events") as stream:
        assert stream.status_code == 200
        frames = _frames(stream)
    kinds = [kind for kind, _ in frames]
    assert kinds[:2] == [SSE_STATE, SSE_STATE_DELTA] and kinds[-1] == SSE_DONE, kinds
    full, delta = frames[0][1], frames[1][1]
    assert "ops" not in full and "state" in full
    assert delta["version"] == DELTA_VERSION and delta["base_seq"] == full["seq"]
    rebuilt = apply(full, delta["ops"])
    assert rebuilt["seq"] == delta["seq"] > full["seq"]
    assert rebuilt == client.get("/api/runs/demo/state").json()
    assert rebuilt["state"]["nodes"]["0"]["metric"] == 0.5 and rebuilt["state"]["finished"] is True
    assert len(json.dumps(delta)) < len(json.dumps(rebuilt)), "a delta is sent only when smaller"


def test_a_fresh_connection_always_starts_with_a_full_frame(tmp_path):
    store = _run(tmp_path)
    store.append("run_finished", {"stop_reason": "budget"})
    client = TestClient(make_app(tmp_path))
    with client.stream("GET", "/api/runs/demo/events", headers={"Last-Event-ID": "1"}) as stream:
        frames = _frames(stream)
    assert [kind for kind, _ in frames] == [SSE_STATE, SSE_DONE]
    assert "state" in frames[0][1] and "ops" not in frames[0][1]
