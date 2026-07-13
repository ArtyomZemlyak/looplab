"""The light span index (`events.span_index`): the accelerator behind the trace views on large runs.

It must be an INVISIBLE optimization — every read served through it is byte-identical to reading the
whole `spans.jsonl` the old way (`load_spans` + `build_trace_view`/`build_conversation`), while
touching only the light structure (timeline) or one node/span's byte range (detail). These tests pin
that equivalence plus the cache/persistence/invalidation contract (append-only top-up, cold reload
from the persisted index, rebuild on replace/shrink/corruption, graceful degrade)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import orjson
import pytest

from looplab.events import span_index
from looplab.events.span_index import get_index, invalidate
from looplab.events.traceview import build_trace_view, build_conversation, load_spans, _tree, _cap_span_io

ST = SimpleNamespace(run_id="demo", task_id="t", total_eval_seconds=7.5)


def _canon(x) -> bytes:
    return orjson.dumps(x, option=orjson.OPT_SORT_KEYS)


def _gen(node_id, trace_id, span_id, parent_id, turn):
    """A generation span with HEAVY I/O (the bytes the index drops), like a real repo-developer turn."""
    return {"name": "llm.generate", "kind": "generation", "trace_id": trace_id, "span_id": span_id,
            "parent_id": parent_id, "run_id": "demo",
            "attributes": {"node_id": node_id, "phase": "implement", "phase_span": parent_id,
                           "model": "m", "input": [{"role": "system", "content": "S" * 4000},
                                                    {"role": "user", "content": "U" * 4000}],
                           "output": "O" * 4000, "thinking": "T" * 4000,
                           "usage": {"prompt": 100 * turn, "completion": 30, "total": 100 * turn + 30},
                           "cost": 0.01, "tool_calls": [{"name": "read_file"}]},
            "events": [], "status": "OK", "start": float(turn), "duration_s": 1.0}


def _spans_for(node_id, trace_id):
    root = f"root{node_id}"
    out = [{"name": "create_node", "kind": "operation", "trace_id": trace_id, "span_id": root,
            "parent_id": None, "run_id": "demo", "attributes": {"node_id": node_id},
            "events": [], "status": "OK", "start": 0.0, "duration_s": 9.0}]
    for turn in range(3):
        out.append(_gen(node_id, trace_id, f"g{node_id}_{turn}", root, turn + 1))
        out.append({"name": "tool.read_file", "kind": "tool", "trace_id": trace_id,
                    "span_id": f"tl{node_id}_{turn}", "parent_id": f"g{node_id}_{turn}", "run_id": "demo",
                    "attributes": {"node_id": node_id, "tool": "read_file",
                                   "input": {"path": "a.py"}, "output": "R" * 2000},
                    "events": [], "status": "OK", "start": turn + 1.5, "duration_s": 0.2})
    return out


def _write_spans(rd: Path, spans: list[dict]) -> Path:
    sp = rd / "spans.jsonl"
    with open(sp, "wb") as f:
        for s in spans:
            f.write(orjson.dumps(s) + b"\n")
    invalidate(sp)                                  # forget any prior in-process cache for this path
    (rd / "spans.index.jsonl").unlink(missing_ok=True)
    return sp


@pytest.fixture
def run(tmp_path):
    rd = tmp_path / "demo"
    rd.mkdir()
    spans = _spans_for(0, "tr0") + _spans_for(1, "tr1")
    sp = _write_spans(rd, spans)
    return rd, sp, spans


# --------------------------------------------------------------------------- equivalence
def test_timeline_is_byte_identical_to_load_spans(run):
    rd, sp, spans = run
    ref = build_trace_view(ST, load_spans(sp), light=True)
    idx = get_index(sp)
    got = build_trace_view(ST, idx.light_spans(), light=True)
    assert _canon(ref) == _canon(got)


def test_index_drops_heavy_io_but_keeps_structure(run):
    rd, sp, spans = run
    idx = get_index(sp)
    for s in idx.light_spans():
        a = s.get("attributes") or {}
        assert not ({"input", "output", "thinking"} & a.keys())   # heavy I/O dropped
    # usage/cost/model survive (the timeline rollup needs them)
    gens = [s for s in idx.light_spans() if s.get("kind") == "generation"]
    assert gens and all("usage" in (g.get("attributes") or {}) for g in gens)


def test_full_span_roundtrip_by_offset(run):
    rd, sp, spans = run
    idx = get_index(sp)
    for target in ("g0_1", "tl1_2", "root0"):
        ref = next(s for s in spans if s["span_id"] == target)
        assert _canon(idx.full_span(target)) == _canon(ref)      # complete span incl. heavy I/O
    assert idx.full_span("nope") is None


def test_conversation_identical_via_node_offsets(run):
    rd, sp, spans = run
    for nid in (0, 1):
        ref = build_conversation(ST, load_spans(sp), nid)
        idx = get_index(sp)
        got = build_conversation(ST, idx.full_spans_for_node(nid), nid)
        assert _canon(ref) == _canon(got)
        assert got["stages"]                                     # non-empty (sanity)


def test_by_trace_identical(run):
    rd, sp, spans = run
    idx = get_index(sp)
    ref = _tree([_cap_span_io(s) for s in load_spans(sp) if s.get("trace_id") == "tr0"])
    got = _tree([_cap_span_io(s) for s in idx.full_spans_for_trace("tr0")])
    assert _canon(ref) == _canon(got)


# --------------------------------------------------------------------------- incremental + persist
def test_incremental_topup_matches_full_rebuild(run):
    rd, sp, spans = run
    idx = get_index(sp)
    n0 = len(idx.light_spans())
    more = _spans_for(2, "tr2")
    with open(sp, "ab") as f:                                    # append a whole new node's spans
        for s in more:
            f.write(orjson.dumps(s) + b"\n")
    idx = get_index(sp)                                          # tops up only the appended tail
    assert len(idx.light_spans()) == n0 + len(more)
    assert len(idx.full_spans_for_node(2)) == len(more)
    # identical to a from-scratch parse of the grown file
    ref = build_trace_view(ST, load_spans(sp), light=True)
    assert _canon(ref) == _canon(build_trace_view(ST, idx.light_spans(), light=True))


def test_persisted_index_is_written_and_smaller(run):
    rd, sp, spans = run
    get_index(sp)
    ip = rd / "spans.index.jsonl"
    assert ip.exists()
    assert ip.stat().st_size < sp.stat().st_size                # the light index is smaller than payload


def test_cold_reload_from_persisted_matches(run):
    rd, sp, spans = run
    get_index(sp)                                               # build + persist
    span_index._CACHE.clear()                                   # simulate a fresh server process
    idx = get_index(sp)                                         # must load the persisted index, not rescan
    ref = build_trace_view(ST, load_spans(sp), light=True)
    assert _canon(ref) == _canon(build_trace_view(ST, idx.light_spans(), light=True))
    # detail reads still resolve after a cold reload (offsets persisted)
    assert idx.full_span("g1_0") is not None


def test_corrupt_persisted_index_falls_back_to_rebuild(run):
    rd, sp, spans = run
    get_index(sp)
    span_index._CACHE.clear()
    (rd / "spans.index.jsonl").write_bytes(b"not a valid index\n{garbage\n")
    idx = get_index(sp)                                         # rebuilds from spans.jsonl, never crashes
    ref = build_trace_view(ST, load_spans(sp), light=True)
    assert _canon(ref) == _canon(build_trace_view(ST, idx.light_spans(), light=True))


def test_persisted_index_with_wrong_identity_is_rejected(run):
    rd, sp, spans = run
    get_index(sp)
    span_index._CACHE.clear()
    # Tamper the header's inode → the persisted index no longer matches spans.jsonl → rebuild.
    ip = rd / "spans.index.jsonl"
    lines = ip.read_bytes().split(b"\n")
    hdr = json.loads(lines[0]); hdr["ino"] = hdr.get("ino", 0) + 999999
    lines[0] = orjson.dumps(hdr)
    ip.write_bytes(b"\n".join(lines))
    idx = get_index(sp)
    ref = build_trace_view(ST, load_spans(sp), light=True)
    assert _canon(ref) == _canon(build_trace_view(ST, idx.light_spans(), light=True))


# --------------------------------------------------------------------------- staleness / degrade
def test_rebuild_when_file_shrinks(run):
    rd, sp, spans = run
    get_index(sp)                                              # cache the full index
    smaller = _spans_for(5, "tr5")                            # replace with a DIFFERENT, shorter file
    with open(sp, "wb") as f:
        for s in smaller:
            f.write(orjson.dumps(s) + b"\n")
    idx = get_index(sp)                                       # must NOT serve the stale (larger) prefix
    ref = build_trace_view(ST, load_spans(sp), light=True)
    assert _canon(ref) == _canon(build_trace_view(ST, idx.light_spans(), light=True))
    assert idx.full_span("root5") is not None and idx.full_span("root0") is None


def test_missing_spans_returns_none(tmp_path):
    assert get_index(tmp_path / "nope.jsonl") is None


def test_torn_final_line_is_ignored(run):
    rd, sp, spans = run
    with open(sp, "ab") as f:
        f.write(b'{"span_id": "torn", "kind": "tool"')          # no trailing newline → torn write
    idx = get_index(sp)
    assert idx.full_span("torn") is None                        # not indexed (matches iter_jsonl)
    assert _canon(build_trace_view(ST, load_spans(sp), light=True)) == \
        _canon(build_trace_view(ST, idx.light_spans(), light=True))


# --------------------------------------------------------------------------- HTTP endpoint wiring
def _http_run(tmp_path):
    from looplab.events.eventstore import EventStore
    rd = tmp_path / "demo"
    rd.mkdir()
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    for nid in (0, 1):
        s.append("node_created", {"node_id": nid, "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": ""}})
        s.append("node_evaluated", {"node_id": nid, "metric": 0.5, "eval_seconds": 3.0})
    _write_spans(rd, _spans_for(0, "tr0") + _spans_for(1, "tr1"))
    return rd


def test_endpoints_serve_through_the_index(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    rd = _http_run(tmp_path)
    client = TestClient(make_app(tmp_path))

    # /trace: the LIGHT run-level timeline — both nodes present, NO heavy I/O shipped, and the
    # persisted index is created as a side effect.
    tv = client.get("/api/runs/demo/trace").json()
    assert set(tv["nodes"].keys()) == {"0", "1"} and tv["summary"]["generations"] == 6
    assert "input" not in json.dumps(tv) and "S" * 100 not in json.dumps(tv)   # heavy I/O stripped
    assert (rd / "spans.index.jsonl").exists()

    # /spans/{sid}: full (uncapped) I/O for one observation, fetched by byte offset.
    span = client.get("/api/runs/demo/spans/g0_1").json()
    assert span["attributes"]["output"] == "O" * 4000 and span["attributes"]["thinking"] == "T" * 4000

    # /conversation: the node's linear thread (reads only that node's byte ranges).
    convo = client.get("/api/runs/demo/nodes/0/conversation").json()
    assert convo["node_id"] == "0" and convo["stages"]

    # /trace/by_trace: one operation's tree.
    bt = client.get("/api/runs/demo/trace/by_trace/tr1").json()
    assert bt["count"] == len(_spans_for(1, "tr1"))

    # clear_trace rewrites spans.jsonl (invalidating byte offsets) → the index is dropped and the next
    # read rebuilds cleanly against the shrunk file (node 0 gone, node 1 kept).
    assert client.post("/api/runs/demo/nodes/0/clear_trace").json()["removed"] == len(_spans_for(0, "tr0"))
    tv2 = client.get("/api/runs/demo/trace").json()
    assert set(tv2["nodes"].keys()) == {"1"}
    assert client.get("/api/runs/demo/spans/g0_1").json()["attributes"] == {}   # node 0's span is gone
