"""Transport regressions for topology-preserving trace JSON projections."""
from __future__ import annotations

import sys

import orjson
import pytest

from looplab.events.eventstore import EventStore
from looplab.events.span_index import invalidate
from looplab.events.traceview import (
    TRACE_DETAIL_SPAN_CAP,
    TRACE_NODE_SPAN_CAP_MAX,
    TRACE_VIEW_SPAN_CAP,
)


def _deep_run(root):
    run_dir = root / "demo"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {
        "run_id": "demo", "task_id": "task", "goal": "goal", "direction": "min"})
    store.append("node_created", {
        "node_id": 0, "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": ""},
    })
    store.append("node_evaluated", {"node_id": 0, "metric": 0.5, "eval_seconds": 1.0})
    spans = [{
        "name": "deep", "kind": "operation", "trace_id": "deep-trace",
        "span_id": f"deep-{index}",
        "parent_id": f"deep-{index - 1}" if index else None,
        "run_id": "demo", "attributes": {"node_id": 0, "generation": 0},
        "events": [], "status": "OK", "start": float(index), "duration_s": 0.1,
    } for index in range(TRACE_NODE_SPAN_CAP_MAX)]
    span_path = run_dir / "spans.jsonl"
    span_path.write_bytes(b"".join(orjson.dumps(span) + b"\n" for span in spans))
    invalidate(span_path)
    return run_dir


def _chain_length(forest) -> int:
    assert len(forest) == 1
    length, current = 0, forest[0]
    while current is not None:
        length += 1
        children = current.get("children") or []
        assert len(children) <= 1
        current = children[0] if children else None
    return length


def test_trace_tree_http_responses_preserve_the_maximum_deep_topology(tmp_path):
    """The framework encoder must not recurse after the iterative projector preserved the tree."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app

    _deep_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    # Parsing a deliberately deep, valid JSON document is a client-test concern; increase only the
    # test process's decoder guard and restore it immediately. The production encoder is iterative.
    old_recursion_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_recursion_limit, TRACE_NODE_SPAN_CAP_MAX * 4))
    try:
        node_response = client.get(
            f"/api/runs/demo/nodes/0/trace?limit={TRACE_NODE_SPAN_CAP_MAX}")
        assert node_response.status_code == 200
        assert node_response.headers["content-type"] == "application/json"
        assert _chain_length(node_response.json()["nodes"]) == TRACE_NODE_SPAN_CAP_MAX

        run_response = client.get("/api/runs/demo/trace")
        assert run_response.status_code == 200
        assert _chain_length(run_response.json()["nodes"]["0"]) == TRACE_VIEW_SPAN_CAP

        operation_response = client.get("/api/runs/demo/trace/by_trace/deep-trace")
        assert operation_response.status_code == 200
        assert _chain_length(operation_response.json()["spans"]) == TRACE_DETAIL_SPAN_CAP
    finally:
        sys.setrecursionlimit(old_recursion_limit)
