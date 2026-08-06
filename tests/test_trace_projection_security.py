"""Adversarial regressions for the versioned trace-to-browser projection boundary."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

from looplab.events.traceview import (
    TRACE_CONVERSATION_SPAN_CAP,
    TRACE_DETAIL_SPAN_CAP,
    TRACE_NODE_SPAN_CAP,
    TRACE_NODE_SPAN_CAP_MAX,
    TRACE_PROJECTION_SCHEMA,
    TRACE_VIEW_SPAN_CAP,
    _normalize_span,
    build_conversation,
    build_trace_view,
    settle_node_span_cap,
)


SECRET = "trace-secret-must-not-leak"
URL = f"https://alice:{SECRET}@example.test/private?token={SECRET}"


def _span(index: int, *, trace_id: str = "trace", kind: str = "tool") -> dict:
    return {
        "trace_id": trace_id,
        "span_id": f"s{index}",
        "parent_id": None,
        "run_id": "demo",
        "name": "tool" if kind == "tool" else "generation",
        "kind": kind,
        "start": float(index),
        "duration_s": 0.1,
        "status": "OK",
        "attributes": {
            "node_id": 0,
            "tool": "fetch",
            "input": {"path": URL, "password": SECRET, "nested": list(range(100))},
            "output": f"Authorization: Bearer {SECRET} " + "X" * 20_000,
            "unknown_path": rf"C:\Users\private\{SECRET}\file.txt",
            "arbitrary_provider_payload": {"credential": SECRET},
        },
        "events": [
            {"name": "exception", "error": URL, "type": "ProviderError",
             "stacktrace": f"raw stack {SECRET}", "credential": SECRET}
            for _ in range(40)
        ],
        "raw_exception": f"top-level {SECRET}",
    }


def test_span_projection_allowlists_redacts_and_reports_every_bound():
    projected = _normalize_span(_span(0))
    assert projected is not None
    rendered = json.dumps(projected)

    assert SECRET not in rendered
    assert "unknown_path" not in rendered
    assert "arbitrary_provider_payload" not in rendered
    assert "raw_exception" not in rendered
    assert "stacktrace" not in rendered
    assert projected["attributes"]["input"]["password"] == "***"
    assert len(projected["attributes"]["output"]) <= 2000
    assert len(projected["events"]) == 16

    meta = projected["_projection"]
    assert meta["schema"] == TRACE_PROJECTION_SCHEMA
    assert meta["truncated"] is True
    assert meta["omitted_attributes"] >= 2
    assert meta["omitted_events"] == 24
    assert meta["omitted_fields"] >= 1
    assert meta["omitted_chars"] > 0


def test_span_projection_is_byte_stable_across_python_hash_seeds():
    root = Path(__file__).resolve().parents[1]
    program = """
import json
from looplab.events.traceview import _normalize_span

span = {
    "name": "tool", "kind": "tool", "trace_id": "trace", "span_id": "span",
    "parent_id": None, "run_id": "demo", "status": "OK", "start": 1.0,
    "attributes": {
        "node_id": 7, "phase": "implement", "model": "m", "op": "chat",
        "tool": "shell", "level": "info", "stage": "evaluate", "reason": "r",
        "package": "pkg", "trigger": "manual", "operator": "improve",
        "error_reason": "none", "materialized": "yes", "handoff_from": "a",
        "handoff_to": "b", "input_partial": False, "timed_out": False,
        "reused": True, "sandboxed": True, "proxy_skipped": False, "ok": True,
        "drift": False, "feasible": True, "input_carry": 2, "exit_code": 0,
        "seed": 3, "blocks": 4, "attempt": 5, "repair_attempts": 1,
        "violations": 0, "proxy_score": 0.5, "eval_seconds": 1.25,
        "metric": 0.75, "robust_metric": 0.7,
    },
}
print(json.dumps(_normalize_span(span), separators=(",", ":"), ensure_ascii=True))
"""
    outputs = []
    for seed in ("1", "2", "3", "4"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
        outputs.append(subprocess.check_output(
            [sys.executable, "-c", program], cwd=root, env=env, text=True,
        ))

    # CODEX AGENT: A projection feeds caches and browser diffs; identical raw spans must allocate
    # their bounded text budget and serialize attributes identically in every interpreter process.
    assert len(set(outputs)) == 1


def test_secret_shaped_identity_is_quarantined_instead_of_rewritten():
    secret_id = "sk-" + "A" * 24
    assert _normalize_span({**_span(0), "span_id": secret_id}) is None
    assert _normalize_span({**_span(0), "trace_id": f"trace?token={SECRET}"}) is None
    # Standard tracing identities and a rejected secret parent preserve topology fail-closed.
    safe = _normalize_span({**_span(0), "trace_id": "0123456789abcdef" * 2,
                            "span_id": "0123456789abcdef", "parent_id": secret_id})
    assert safe is not None and safe["parent_id"] is None
    assert safe["_projection"]["truncated"] is True
    assert safe["_projection"]["omitted_fields"] >= 1


def test_huge_run_trace_has_strict_total_span_and_payload_bounds():
    state = SimpleNamespace(run_id="demo", task_id="task", total_eval_seconds=0.0)
    raw = [_span(index) for index in range(TRACE_VIEW_SPAN_CAP + 137)]

    view = build_trace_view(state, raw, light=True)
    projection = view["projection"]
    assert projection["schema"] == TRACE_PROJECTION_SCHEMA
    assert projection["total_spans"] == len(raw)
    assert projection["visible_spans"] == TRACE_VIEW_SPAN_CAP
    assert projection["omitted_spans"] == 137
    assert projection["truncated"] is True
    assert view["summary"]["rollup_partial"] is True
    # Heavy I/O, arbitrary paths and raw exception payloads are absent before the response is cached.
    rendered = json.dumps(view)
    assert SECRET not in rendered and "unknown_path" not in rendered
    assert len(rendered.encode("utf-8")) < 3_000_000


def test_direct_conversation_fallback_reports_spans_hidden_before_grouping():
    state = SimpleNamespace(run_id="demo", task_id="task", total_eval_seconds=0.0)
    raw = [_span(index, trace_id="one-trace")
           for index in range(TRACE_CONVERSATION_SPAN_CAP + 1)]
    conversation = build_conversation(state, raw, 0)
    assert conversation["projection"]["total_spans"] == len(raw)
    assert conversation["projection"]["visible_spans"] == TRACE_CONVERSATION_SPAN_CAP
    assert conversation["projection"]["omitted_spans"] == 1
    assert conversation["projection"]["truncated"] is True


def test_conversation_filters_target_traces_before_global_tail_cap():
    """Newer activity from other nodes cannot evict an older node from the fallback projection."""
    state = SimpleNamespace(run_id="demo", task_id="task", total_eval_seconds=0.0)
    target = [{
        "trace_id": "target-trace",
        "span_id": "target-root",
        "parent_id": None,
        "run_id": "demo",
        "name": "create_node",
        "kind": "operation",
        "start": 0.0,
        "duration_s": 1.0,
        "status": "OK",
        "attributes": {"node_id": 0},
        "events": [],
    }]
    for index in range(TRACE_CONVERSATION_SPAN_CAP + 2):
        target.append({
            "trace_id": "target-trace",
            "span_id": f"target-{index}",
            "parent_id": "target-root",
            "run_id": "demo",
            "name": "generation",
            "kind": "generation",
            "start": float(index + 1),
            "duration_s": 0.1,
            "status": "OK",
            "attributes": {
                "node_id": 0,
                "phase": "implement",
                "phase_span": "target-root",
                "input": [{"role": "user", "content": "work"}],
                "output": f"target output {index}",
            },
            "events": [],
        })
    unrelated = [
        {
            "trace_id": f"other-trace-{index}",
            "span_id": f"other-{index}",
            "parent_id": None,
            "run_id": "demo",
            "name": "other",
            "kind": "operation",
            "start": float(len(target) + index),
            "duration_s": 0.1,
            "status": "OK",
            "attributes": {"node_id": 1},
            "events": [],
        }
        for index in range(TRACE_CONVERSATION_SPAN_CAP + 7)
    ]

    whole_run = build_conversation(state, target + unrelated, 0)
    node_scoped = build_conversation(state, target, 0)

    assert whole_run == node_scoped
    assert whole_run["stages"]
    assert whole_run["projection"]["total_spans"] == len(target)
    assert whole_run["projection"]["visible_spans"] == TRACE_CONVERSATION_SPAN_CAP
    assert whole_run["projection"]["omitted_spans"] == len(target) - TRACE_CONVERSATION_SPAN_CAP
    assert whole_run["projection"]["truncated"] is True


def test_trace_routes_bound_large_trace_and_redact_secret_url_path(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    run_dir = tmp_path / "demo"
    run_dir.mkdir()
    EventStore(run_dir / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "task", "goal": "g", "direction": "min"})
    count = TRACE_DETAIL_SPAN_CAP + 11
    spans = [_span(index, trace_id="one-trace", kind="tool") for index in range(count)]
    spans[-1]["attributes"]["node_id"] = URL
    (run_dir / "spans.jsonl").write_text(
        "".join(json.dumps(span) + "\n" for span in spans), encoding="utf-8")

    client = TestClient(make_app(tmp_path))
    trace = client.get("/api/runs/demo/trace/by_trace/one-trace")
    assert trace.status_code == 200
    body = trace.json()
    assert body["schema"] == TRACE_PROJECTION_SCHEMA
    assert body["count"] == count
    assert body["visible_count"] == TRACE_DETAIL_SPAN_CAP
    assert body["omitted_count"] == 11
    rendered = trace.text
    assert SECRET not in rendered and "unknown_path" not in rendered and "stacktrace" not in rendered
    assert len(rendered.encode("utf-8")) < 2_000_000

    detail = client.get(f"/api/runs/demo/spans/s{count - 1}").json()
    assert SECRET not in json.dumps(detail)
    assert detail["projection"]["truncated"] is True
    # A non-generation detail seeks only its own row; it does not materialize 256 unrelated peers.
    assert detail["projection"]["omitted_trace_spans"] == count - 1

    tail = client.get("/api/runs/demo/trace/tail", params={"limit": 5}).json()
    assert SECRET not in json.dumps(tail)
    assert tail["projection"]["visible_spans"] <= 5


def test_trace_tail_limit_pages_further_back_up_to_the_raised_ceiling(tmp_path):
    # The Dock's "load earlier spans" control raises the tail limit; the server must honor limits well
    # past the old 100 cap (so paging back is meaningful) yet still clamp at the 400 ceiling.
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    run_dir = tmp_path / "demo"
    run_dir.mkdir()
    EventStore(run_dir / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "task", "goal": "g", "direction": "min"})
    total = 260
    spans = [_span(index, trace_id="one-trace", kind="tool") for index in range(total)]
    (run_dir / "spans.jsonl").write_text(
        "".join(json.dumps(span) + "\n" for span in spans), encoding="utf-8")
    client = TestClient(make_app(tmp_path))

    # A limit past the old cap of 100 returns that many rows (pages further back than before); the feed
    # is still truncated, so the UI keeps offering "load earlier". (The backward scan stops once it has
    # `limit` lines, so omitted_spans reflects the scan window, not the whole file — assert the window.)
    wide = client.get("/api/runs/demo/trace/tail", params={"limit": 200}).json()
    assert 100 < wide["projection"]["visible_spans"] <= 200        # >100: cap raised, pages back further
    assert wide["projection"]["truncated"] is True                 # more history remains
    assert SECRET not in json.dumps(wide)                          # redaction holds at the larger window

    # Above the 400 ceiling the request clamps to 400; here the 260 available rows all fit within it, and
    # the scan reaches BOF (< _MAX_TAIL), so every row is shown with nothing omitted or truncated.
    maxed = client.get("/api/runs/demo/trace/tail", params={"limit": 100000}).json()
    assert maxed["projection"]["visible_spans"] == total
    assert maxed["projection"]["omitted_spans"] == 0
    assert maxed["projection"]["truncated"] is False


def _flatten_trace(nodes):
    out = []
    for n in nodes or []:
        out.append(n)
        out.extend(_flatten_trace(n.get("children")))
    return out


def test_node_trace_limit_pages_a_heavily_repaired_node_past_the_default_cap(tmp_path):
    # The node-trace "load more spans" control raises this node's span ceiling on demand. The default
    # (no limit) stays capped at TRACE_NODE_SPAN_CAP=512; a bigger limit surfaces more of THAT node's
    # spans, clamped at TRACE_NODE_SPAN_CAP_MAX=4096. Redaction must hold at the larger window.
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    run_dir = tmp_path / "demo"
    run_dir.mkdir()
    EventStore(run_dir / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "task", "goal": "g", "direction": "min"})
    total = 700                                        # > the 512 default cap for ONE node (node_id=0)
    spans = [_span(index, trace_id=f"t{index}", kind="tool") for index in range(total)]
    (run_dir / "spans.jsonl").write_text(
        "".join(json.dumps(span) + "\n" for span in spans), encoding="utf-8")
    client = TestClient(make_app(tmp_path))

    default = client.get("/api/runs/demo/nodes/0/trace").json()
    default_n = len(_flatten_trace(default["nodes"]))
    assert default_n <= 512                            # capped at the default
    assert default["projection"].get("truncated") is True   # more remains -> UI offers "load more"
    assert SECRET not in json.dumps(default)

    more = client.get("/api/runs/demo/nodes/0/trace", params={"limit": 2000}).json()
    assert len(_flatten_trace(more["nodes"])) > default_n   # the raised cap surfaces more of the node
    assert SECRET not in json.dumps(more)


def _conversation_run(tmp_path, *, stages: int, spans_per_stage: int = 1):
    """A run whose node 0 holds `stages` separate one-band traces. Returns its TestClient.

    Each trace is a root operation plus generations stamped with `phase_span`, which is exactly the
    shape `_stage_of` groups into one band per trace — so the number of bands is controlled directly
    rather than inferred from a span count.
    """
    from fastapi.testclient import TestClient

    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    run_dir = tmp_path / "demo"
    run_dir.mkdir()
    EventStore(run_dir / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "task", "goal": "g", "direction": "min"})
    rows = []
    for stage in range(stages):
        root = f"root-{stage}"
        rows.append({
            "trace_id": f"trace-{stage}", "span_id": root, "parent_id": None, "run_id": "demo",
            "name": "implement", "kind": "operation", "start": float(stage * 100),
            "duration_s": 1.0, "status": "OK", "attributes": {"node_id": 0}, "events": [],
        })
        for turn in range(spans_per_stage):
            rows.append({
                "trace_id": f"trace-{stage}", "span_id": f"gen-{stage}-{turn}", "parent_id": root,
                "run_id": "demo", "name": "generation", "kind": "generation",
                "start": float(stage * 100 + turn + 1), "duration_s": 0.1, "status": "OK",
                "attributes": {
                    "node_id": 0, "phase": "implement", "phase_span": root,
                    # Both carry the secret in shapes the projection is expected to redact, so the
                    # widened-window assertions below are a real claim about the bigger response.
                    "input": [{"role": "user", "content": f"work {URL}"}],
                    "output": f"turn {turn} Authorization: Bearer {SECRET}",
                },
                "events": [],
            })
    (run_dir / "spans.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return TestClient(make_app(tmp_path)), len(rows)


def test_conversation_limit_unhides_stages_the_default_window_already_held(tmp_path):
    """The operator's defect: "N steps hidden" with no way to see them, in the DEFAULT view.

    This node is deliberately built SMALLER than the span window — 400 spans against a 512 default —
    so `omitted_spans` is zero at every limit and the span read cannot be what changes. Everything
    hidden is hidden by the stage/turn RENDER caps, which is what the live server showed on
    runs/rubert-dr-0804 node 1: 192 stages and 320 turns withheld from a response that already held
    every span they were derived from. A `limit` wired only to the span read passes nothing here.
    """
    pytest.importorskip("fastapi")

    client, span_count = _conversation_run(tmp_path, stages=200)
    assert span_count < TRACE_CONVERSATION_SPAN_CAP   # the span window is NOT the binding constraint

    default = client.get("/api/runs/demo/nodes/0/conversation").json()
    assert default["projection"]["omitted_spans"] == 0
    assert len(default["stages"]) == 64               # every band past the render cap is withheld…
    assert default["projection"]["omitted_stages"] == 136   # …and the receipt says so
    assert default["projection"]["truncated"] is True
    hidden_turns = default["projection"]["omitted_turns"]
    assert hidden_turns > 0

    # One click of the shared window (512 -> 1024) must actually deliver bands, not just re-read.
    paged = client.get("/api/runs/demo/nodes/0/conversation", params={"limit": 1024}).json()
    assert len(paged["stages"]) == 128
    assert paged["projection"]["omitted_stages"] == 72
    assert paged["projection"]["visible_turns"] > default["projection"]["visible_turns"]

    # Paging to the ceiling reaches the whole conversation on a node this size.
    whole = client.get("/api/runs/demo/nodes/0/conversation",
                       params={"limit": TRACE_NODE_SPAN_CAP_MAX}).json()
    assert len(whole["stages"]) == 200
    assert whole["projection"]["omitted_stages"] == 0
    assert whole["projection"]["omitted_turns"] == 0
    # Every widened window stays inside the same browser contract as the default one.
    assert SECRET not in json.dumps(whole)


def test_conversation_limit_widens_the_span_window_and_clamps_to_the_one_ceiling(tmp_path):
    """The other half: a node bigger than the ceiling. The window grows, then stops at ONE number."""
    pytest.importorskip("fastapi")

    client, span_count = _conversation_run(
        tmp_path, stages=TRACE_NODE_SPAN_CAP_MAX, spans_per_stage=1)
    assert span_count > TRACE_NODE_SPAN_CAP_MAX

    default = client.get("/api/runs/demo/nodes/0/conversation").json()
    assert default["projection"]["visible_spans"] == TRACE_CONVERSATION_SPAN_CAP
    assert default["projection"]["omitted_spans"] > 0

    paged = client.get("/api/runs/demo/nodes/0/conversation", params={"limit": 2048}).json()
    assert paged["projection"]["visible_spans"] == 2048

    # Both per-node surfaces stop at the SAME ceiling, and an absurd request clamps rather than
    # refusing — a second ceiling anywhere would show up here as two different windows.
    for absurd in (TRACE_NODE_SPAN_CAP_MAX + 1, 10 ** 9):
        maxed = client.get("/api/runs/demo/nodes/0/conversation", params={"limit": absurd}).json()
        assert maxed["projection"]["visible_spans"] == TRACE_NODE_SPAN_CAP_MAX
        tree = client.get("/api/runs/demo/nodes/0/trace", params={"limit": absurd}).json()
        assert tree["projection"]["visible_spans"] == TRACE_NODE_SPAN_CAP_MAX


def test_node_span_window_limit_is_settled_never_trusted(tmp_path):
    """`limit` comes off the wire. Its truth table, and the refusal both routes owe a bad one."""
    pytest.importorskip("fastapi")

    # Absent / no-request / malformed all mean "the default", and can never widen a window by
    # accident. A request below the default cannot SHRINK one the client never asked about.
    for unrequested in (0, None, -1, -(10 ** 9), "512", 512.0, True, False, [512]):
        assert settle_node_span_cap(unrequested, default=TRACE_NODE_SPAN_CAP) == TRACE_NODE_SPAN_CAP
    assert settle_node_span_cap(1, default=TRACE_NODE_SPAN_CAP) == TRACE_NODE_SPAN_CAP
    assert settle_node_span_cap(1024, default=TRACE_NODE_SPAN_CAP) == 1024
    assert settle_node_span_cap(10 ** 9, default=TRACE_NODE_SPAN_CAP) == TRACE_NODE_SPAN_CAP_MAX
    # The conversation's default differs from the tree's in principle; the CEILING may not.
    assert settle_node_span_cap(10 ** 9,
                                default=TRACE_CONVERSATION_SPAN_CAP) == TRACE_NODE_SPAN_CAP_MAX

    client, _ = _conversation_run(tmp_path, stages=4)
    for route in ("trace", "conversation"):
        for bad in ("-1", "abc", "1e9", "4.5"):
            response = client.get(f"/api/runs/demo/nodes/0/{route}", params={"limit": bad})
            assert response.status_code == 422, (route, bad)
        # A well-formed request still succeeds on both, so the refusals above are about the VALUE.
        assert client.get(f"/api/runs/demo/nodes/0/{route}",
                          params={"limit": 1024}).status_code == 200


def test_browser_and_server_share_one_node_span_window(tmp_path):
    """The UI cannot import Python. Pin the mirror, because drift here is silent in both directions.

    A UI ceiling below the server's leaves spans the operator can never reach; above it, the pager
    keeps offering clicks that return the identical response. Both read as "load more is broken".
    """
    source = (Path(__file__).resolve().parents[1] / "ui" / "src" / "traceProjection.js").read_text()
    mirrored = dict(
        re.findall(r"^export const (NODE_TRACE_SPAN_WINDOW(?:_MAX)?) = (\d+)$", source, re.M))
    assert mirrored == {
        "NODE_TRACE_SPAN_WINDOW": str(TRACE_NODE_SPAN_CAP),
        "NODE_TRACE_SPAN_WINDOW_MAX": str(TRACE_NODE_SPAN_CAP_MAX),
    }
    # The conversation pages against the same window, so its default must be the same number too.
    assert TRACE_CONVERSATION_SPAN_CAP == TRACE_NODE_SPAN_CAP


def test_trace_route_read_failures_are_not_reported_as_complete_empty_data(tmp_path, monkeypatch):
    """I/O failure has unknown cardinality; it must differ from a successful empty sidecar read."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    run_dir = tmp_path / "demo"
    run_dir.mkdir()
    EventStore(run_dir / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "task", "goal": "g", "direction": "min"})
    (run_dir / "spans.jsonl").write_bytes(b"")
    client = TestClient(make_app(tmp_path))

    # A readable empty source may truthfully carry exact zero counters.
    empty = client.get("/api/runs/demo/trace/tail").json()
    assert empty["schema"] == TRACE_PROJECTION_SCHEMA
    assert empty["tail"] == []
    assert empty["projection"]["visible_spans"] == 0
    assert empty["projection"]["omitted_spans"] == 0
    assert empty["projection"]["truncated"] is False
    assert empty["projection"].get("unavailable") is not True

    (run_dir / "spans.jsonl").unlink()
    missing = client.get("/api/runs/demo/trace/tail").json()
    assert missing["tail"] == []
    assert missing["projection"]["visible_spans"] == 0
    assert missing["projection"]["omitted_spans"] == 0
    assert missing["projection"]["truncated"] is False
    assert missing["projection"].get("unavailable") is not True
    (run_dir / "spans.jsonl").write_bytes(b"")

    import os
    from looplab.events import span_index

    def unreadable_index(_path):
        raise OSError("simulated unreadable trace source")

    real_getsize = os.path.getsize

    def unreadable_tail(path):
        if str(path).endswith("spans.jsonl"):
            raise OSError("simulated unreadable trace tail")
        return real_getsize(path)

    monkeypatch.setattr(span_index, "get_index", unreadable_index)
    monkeypatch.setattr(os.path, "getsize", unreadable_tail)

    failed = [
        client.get("/api/runs/demo/trace").json(),
        client.get("/api/runs/demo/nodes/0/trace").json(),
        client.get("/api/runs/demo/nodes/0/conversation").json(),
        client.get("/api/runs/demo/spans/missing").json(),
        client.get("/api/runs/demo/trace/by_trace/example").json(),
        client.get("/api/runs/demo/trace/tail").json(),
    ]
    count_fields = {
        "total_spans", "visible_spans", "omitted_spans", "truncated_spans",
        "trace_total_spans", "trace_visible_spans", "omitted_trace_spans",
    }
    for body in failed:
        assert body["schema"] == TRACE_PROJECTION_SCHEMA
        assert body["projection"] == {
            "schema": TRACE_PROJECTION_SCHEMA,
            "unavailable": True,
            "truncated": True,
        }
        assert count_fields.isdisjoint(body["projection"])

    assert failed[0]["summary"] == {}
    assert {"count", "visible_count", "omitted_count"}.isdisjoint(failed[4])


@pytest.mark.parametrize("failure", [PermissionError, FileNotFoundError])
def test_real_source_open_failure_is_unavailable_on_every_trace_route(
        tmp_path, monkeypatch, failure):
    """Exercise real cache/index/tail seams when stat succeeds but the following open fails."""
    import builtins
    import os
    from pathlib import Path

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    run_dir = tmp_path / "demo"
    run_dir.mkdir()
    EventStore(run_dir / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "task", "goal": "g", "direction": "min"})
    source = run_dir / "spans.jsonl"
    source.write_text(json.dumps(_span(0, trace_id="trace")) + "\n", encoding="utf-8")
    client = TestClient(make_app(tmp_path))

    # Warm both cache layers. Permission loss or disappearance must be detected even when neither
    # the derived run view nor the SpanIndex would otherwise need to read source bytes again.
    assert client.get("/api/runs/demo/trace").json()["projection"].get("unavailable") is not True
    assert client.get("/api/runs/demo/nodes/0/trace").json()["projection"].get("unavailable") is not True
    real_open = builtins.open

    def denied(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if (isinstance(file, (str, os.PathLike)) and Path(file) == source
                and "r" in str(mode)):
            raise failure("simulated trace source read failure")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied)
    failed = [
        client.get("/api/runs/demo/trace").json(),
        client.get("/api/runs/demo/nodes/0/trace").json(),
        client.get("/api/runs/demo/nodes/0/conversation").json(),
        client.get("/api/runs/demo/spans/s0").json(),
        client.get("/api/runs/demo/trace/by_trace/trace").json(),
        client.get("/api/runs/demo/trace/tail").json(),
    ]
    for body in failed:
        assert body["schema"] == TRACE_PROJECTION_SCHEMA
        assert body["projection"] == {
            "schema": TRACE_PROJECTION_SCHEMA,
            "unavailable": True,
            "truncated": True,
        }
        assert "total_spans" not in body["projection"]
    assert failed[0]["summary"] == {}
    assert {"count", "visible_count", "omitted_count"}.isdisjoint(failed[4])


def test_span_scan_fallback_does_not_invent_trace_cardinality(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.events import span_index
    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    run_dir = tmp_path / "demo"
    run_dir.mkdir()
    EventStore(run_dir / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "task", "goal": "g", "direction": "min"})
    spans = [_span(index, trace_id="shared") for index in range(3)]
    (run_dir / "spans.jsonl").write_text(
        "".join(json.dumps(span) + "\n" for span in spans), encoding="utf-8")
    monkeypatch.setattr(span_index, "get_index", lambda _path: None)

    body = TestClient(make_app(tmp_path)).get("/api/runs/demo/spans/s0").json()
    projection = body["projection"]
    assert body["schema"] == TRACE_PROJECTION_SCHEMA
    assert projection["trace_cardinality_unavailable"] is True
    assert projection["truncated"] is True
    assert {"trace_total_spans", "trace_visible_spans", "omitted_trace_spans"}.isdisjoint(projection)


def test_span_index_reads_one_inode_from_open_to_scan(tmp_path, monkeypatch):
    """`stat(path)` then `open(path)` resolves the pathname twice.

    A rewrite in between would cache-key and validate inode A while every later read observed inode
    B — a trace view stitched from two generations of the file. The index derives identity, size and
    content from ONE descriptor, so a replace can only be seen as a whole, never spliced.
    """
    import os
    from pathlib import Path

    from looplab.events import span_index

    span_index._CACHE.clear()
    source = tmp_path / "spans.jsonl"
    first = json.dumps(_span(0, trace_id="generation-one")) + "\n"
    source.write_text(first, encoding="utf-8")

    replacement = json.dumps(_span(1, trace_id="generation-two")) + "\n"
    real_open = span_index.open if hasattr(span_index, "open") else open
    swapped = {"n": 0}

    def replacing_open(file, *args, **kwargs):
        # Swap the file the instant the index opens it: the OLD code stat()'d before this point, so
        # it keyed on the pre-swap inode and then read the post-swap bytes.
        handle = real_open(file, *args, **kwargs)
        if Path(str(file)) == source and swapped["n"] == 0:
            swapped["n"] = 1
            other = tmp_path / "replacement.jsonl"
            other.write_text(replacement, encoding="utf-8")
            os.replace(other, source)
        return handle

    monkeypatch.setattr("builtins.open", replacing_open)
    idx = span_index.get_index(source)
    monkeypatch.undo()
    assert idx is not None

    # Whatever generation was indexed, the identity recorded for it must be the identity of the
    # bytes that were actually parsed — never the other inode's.
    traces = {span.get("trace_id") for span in idx.light_spans()}
    on_disk = os.stat(source)
    if idx.identity == (on_disk.st_dev, on_disk.st_ino):
        assert traces == {"generation-two"}
    else:
        assert traces == {"generation-one"}, (
            "identity was keyed on the pre-swap inode while the content came from the post-swap one")
    span_index._CACHE.clear()
