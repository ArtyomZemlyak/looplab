"""The trace-family polls read NARROW facts, and the SSE stream encodes its frames OFF the loop.

Review 2026-09-22, SRV2-05 and SRV2-06, measured on an 11 MB / 4k-event log:

* SRV2-05 — every trace-family poll (`/nodes/{nid}/trace`, `/logs`, `/conversation`, `/episodes`,
  `/trace`, `/trace/tail`) rebuilt the WHOLE public state — fold, public projection, `model_dump`,
  650-760 ms per poll on a live run — to read three scalars (`trace_scalars`) and one integer
  (`_cached_node_attempt`, before AND after its read). They now read `AppState.trace_facts`: the
  facts of ONE fresh fold, extracted and cached as immutable values per log identity. No folded
  `RunState` outlives the call that made it (the declined `shared-fold-memo-races-the-build-worker`
  item, docs/25: independent snapshots only), and the public projection is never built for them.
* SRV2-06 — the SSE generator ran `json.dumps` / `state_diff` / `json.loads` of the whole payload
  ON the event loop: 199 ms per tick per connection at 3.1 MB, during which every other request
  and every other stream in the process waited.

Driven, never timed: the first half counts public-state builds while the polls run; the second
records, from inside the encoder, whether it ran on the event loop's thread.
"""
from __future__ import annotations

import asyncio
import json

import orjson
from fastapi.testclient import TestClient

from looplab.core.node_evidence import node_attempt_from_payload
from looplab.events.eventstore import EventStore
from looplab.serve.appstate import AppState
from looplab.serve.server import make_app


def _run(tmp_path):
    rd = tmp_path / "demo"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    for nid in (0, 1):
        store.append("node_created", {"node_id": nid, "operator": "draft",
                                      "idea": {"operator": "draft", "params": {}, "rationale": ""}})
        store.append("node_evaluated", {"node_id": nid, "metric": 0.5, "eval_seconds": 3.0})
    spans = []
    for nid in (0, 1):
        root = f"root{nid}"
        spans.append({"name": "create_node", "kind": "operation", "trace_id": f"tr{nid}",
                      "span_id": root, "parent_id": None, "run_id": "demo",
                      "attributes": {"node_id": nid}, "events": [], "status": "OK",
                      "start": 0.0, "duration_s": 1.0})
        spans.append({"name": "llm.generation", "kind": "generation", "trace_id": f"tr{nid}",
                      "span_id": f"g{nid}", "parent_id": root, "run_id": "demo",
                      "attributes": {"node_id": nid, "model": "m", "input": "q", "output": "a"},
                      "events": [], "status": "OK", "start": 0.5, "duration_s": 0.2})
    with open(rd / "spans.jsonl", "wb") as stream:
        for span in spans:
            stream.write(orjson.dumps(span) + b"\n")
    return rd, store


_TRACE_POLLS = (
    "/api/runs/demo/nodes/0/trace?attempt=0",
    "/api/runs/demo/nodes/0/logs",
    "/api/runs/demo/nodes/0/conversation",
    "/api/runs/demo/nodes/0/episodes",
    "/api/runs/demo/trace",
    "/api/runs/demo/trace/tail",
)


def test_a_trace_poll_never_builds_the_public_state(tmp_path, monkeypatch):
    """THE DEFECT: each poll paid a full public-state build (up to two, around its CAS) for three
    scalars and one attempt. MUTATION: `_cached_node_attempt` / `trace_scalars` back to
    `state_payload` -> every poll below builds the public state."""
    rd, store = _run(tmp_path)
    builds = []
    real_build = AppState._build_state_entry

    def _counting_build(self, *args, **kwargs):
        builds.append(args[0])
        return real_build(self, *args, **kwargs)

    monkeypatch.setattr(AppState, "_build_state_entry", _counting_build)
    client = TestClient(make_app(tmp_path))
    for path in _TRACE_POLLS:
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
    # ...and after an APPEND — the live-run case, where the payload cache missed on every poll.
    store.append("hint", {"text": "a live run appends on every provider call"})
    for path in _TRACE_POLLS:
        assert client.get(path).status_code == 200, path
    assert builds == [], f"a trace poll built the public state {len(builds)} time(s)"


def test_the_facts_are_folded_once_per_log_identity_and_refolded_after_an_append(tmp_path,
                                                                                 monkeypatch):
    """The facts are a VALUE cache keyed by the log's file identity: an unchanged log is a stat and
    a lookup, and an append (a new identity) is one fresh fold — which is what keeps the attempt
    CAS around a trace read honest."""
    import looplab.serve.appstate as appstate

    rd, store = _run(tmp_path)
    srv = make_app(tmp_path).state.looplab
    folds = []
    real_fold = appstate.fold
    monkeypatch.setattr(appstate, "fold", lambda events: (folds.append(1), real_fold(events))[1])

    first = srv.trace_facts(rd)
    assert srv.trace_facts(rd) is first and len(folds) == 1
    assert first.attempt(0) == 0 and first.attempt(7) is None

    store.append("node_reset", {"node_id": 0, "from_stage": "eval", "generation": 0})
    after = srv.trace_facts(rd)
    assert len(folds) == 2 and after.attempt(0) == 1, "the reset's new attempt was not observed"


def test_the_facts_agree_with_the_payload_rule_they_replace(tmp_path):
    """One rule, and now one input shape for the trace family: the facts must answer exactly what
    `node_attempt_from_payload` over the public payload answered — for folded nodes, a reset node,
    a pre-create building marker and ids the run never had — or the before/after CAS of a trace
    read would 409 on a lifecycle nothing moved."""
    rd, store = _run(tmp_path)
    store.append("node_reset", {"node_id": 0, "from_stage": "eval", "generation": 0})
    store.append("node_building", {"node_id": 2, "operator": "draft", "generation": 0})
    srv = make_app(tmp_path).state.looplab

    facts = srv.trace_facts(rd)
    payload_state = srv.state_payload(rd)["state"]
    for nid in (-1, 0, 1, 2, 3):
        assert facts.attempt(nid) == node_attempt_from_payload(payload_state, nid), nid
    assert (facts.run_id, facts.task_id, facts.total_eval_seconds) == (
        payload_state["run_id"], payload_state["task_id"], payload_state["total_eval_seconds"])
    assert facts.attempt(0) == 1 and facts.attempt(2) == 0


def test_the_facts_are_immutable_values(tmp_path):
    """Nothing a caller can mutate is shared across requests: the cached facts are strings, a
    float and a read-only mapping of ints."""
    import pytest

    rd, _store = _run(tmp_path)
    facts = make_app(tmp_path).state.looplab.trace_facts(rd)
    with pytest.raises(TypeError):
        facts.attempts[0] = 99


# --------------------------------------------------------------------------- SRV2-06

def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def test_the_sse_stream_encodes_every_frame_off_the_event_loop(tmp_path, monkeypatch):
    """THE DEFECT: `json.dumps(payload)`, `state_diff(...)` and `json.loads(full)` ran inside the
    async generator, i.e. on the loop. MUTATION: call the frame encoder directly in `gen()` (or
    inline its body back) -> every recorded step says it ran on the loop.

    The payloads come from a patched `state_payload` (the seam the stream polls), three ticks of one
    generation ending finished-and-stopped, so the stream sends a full frame, a delta, and `done`."""
    import looplab.serve.appstate as appstate
    import looplab.serve.routers.runs as runs_router

    rd = tmp_path / "demo"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    ticks = [(1, True, False), (2, True, False), (3, False, True)]
    calls = [0]

    def state_payload(_self, _rd, upto_seq=None):
        event_count, alive, finished = ticks[min(calls[0], len(ticks) - 1)]
        calls[0] += 1
        return {"state": {"engine_running": alive, "finished": finished,
                          "phase": "finished" if finished else "search",
                          "large": "x" * 4096},
                "seq": event_count, "max_seq": event_count, "generation": "a" * 64,
                "event_count": event_count}

    steps: list = []

    class _RecordingJson:
        def dumps(self, *args, **kwargs):
            steps.append(("dumps", _on_loop()))
            return json.dumps(*args, **kwargs)

        def loads(self, *args, **kwargs):
            steps.append(("loads", _on_loop()))
            return json.loads(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(json, name)

    real_diff = runs_router.state_diff
    real_run_dir = AppState.run_dir

    def _recording_run_dir(self, run_id):
        steps.append(("run_dir", _on_loop()))
        return real_run_dir(self, run_id)

    monkeypatch.setattr(appstate.AppState, "state_payload", state_payload)
    monkeypatch.setattr(appstate.AppState, "run_dir", _recording_run_dir)
    monkeypatch.setattr(runs_router, "json", _RecordingJson())
    monkeypatch.setattr(runs_router, "state_diff",
                        lambda old, new: (steps.append(("diff", _on_loop())), real_diff(old, new))[1])

    response = TestClient(make_app(tmp_path)).get("/api/runs/demo/events")
    assert response.status_code == 200
    events = [line[7:] for line in response.text.splitlines() if line.startswith("event: ")]
    assert events[0] == "state" and "state_delta" in events and events[-1] == "done", events
    assert {name for name, _ in steps} >= {"dumps", "loads", "diff", "run_dir"}, steps
    assert [step for step in steps if step[1]] == [], f"ran ON the event loop: {steps}"
