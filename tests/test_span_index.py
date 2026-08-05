"""The light span index (`events.span_index`): the accelerator behind the trace views on large runs.

It must be an INVISIBLE optimization — every read served through it matches the same versioned
projection built directly from `spans.jsonl` (`load_spans` + views), while
touching only the light structure (timeline) or one node/span's byte range (detail). These tests pin
that equivalence plus the cache/persistence/invalidation contract (append-only top-up, cold reload
from the persisted index, rebuild on replace/shrink/corruption, graceful degrade)."""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import orjson
import pytest

from looplab.events import span_index
from looplab.events.span_index import get_index, invalidate
from looplab.events.traceview import (
    _MAX_PARENT_HOPS,
    _cap_span_io,
    _iter_parent_spans,
    _normalize_span,
    _tree,
    build_conversation,
    build_trace_view,
    load_spans,
)

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
        # The byte-offset lookup is exact, but the returned record crosses the same versioned security
        # projection as every HTTP view; raw durable dictionaries never become browser payloads.
        assert _canon(idx.full_span(target)) == _canon(_normalize_span(ref))
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


def test_unindexed_anchor_keeps_the_append_only_trace_prefix(run):
    """A live tail span found outside an older index can still hydrate from the indexed ancestors."""
    rd, sp, spans = run
    idx = get_index(sp)
    late = _gen(0, "tr0", "late-tail", "root0", 9)
    with open(sp, "ab") as f:
        f.write(orjson.dumps(late) + b"\n")

    assert idx.full_span("late-tail") is None  # prove the held snapshot has not topped itself up
    got = idx.full_spans_for_trace("tr0", anchor_sid="late-tail")
    expected_ids = [span["span_id"] for span in spans if span["trace_id"] == "tr0"]
    # CODEX AGENT: the endpoint appends `late` after this prefix, then hydrate_inputs can resolve its
    # delta chain. Returning [] here would expose only the raw tail and falsely mark the input partial.
    assert [span["span_id"] for span in got] == expected_ids


def test_malformed_complete_span_is_quarantined_without_hiding_following_rows(tmp_path):
    """A schema-bad JSON object is one lost observation, never a poison pill for the trace tail."""
    rd = tmp_path / "demo"
    rd.mkdir()
    root = {
        "name": "create_node", "kind": "operation", "trace_id": "tr0", "span_id": "root",
        "parent_id": None, "attributes": {"node_id": 0}, "start": 0.0, "duration_s": 1.0,
    }
    bad_attributes = {
        "name": "tool.bad", "kind": "tool", "trace_id": "tr0", "span_id": "attrs-bad",
        "parent_id": "root", "attributes": ["not", "a", "mapping"],
        "start": [], "duration_s": {"not": "numeric"},
    }
    invalid_ids = {
        "name": "invalid", "kind": "generation", "trace_id": "tr0", "span_id": [],
        "parent_id": "root", "attributes": {"node_id": 0}, "start": 1.0,
    }
    bad_numbers = {
        "name": "llm.bad", "kind": "generation", "trace_id": "tr0", "span_id": "dirty",
        "parent_id": ["unhashable"], "start": "1e9999", "duration_s": "NaN",
        "attributes": {
            "node_id": 0, "phase_span": [], "input_from": {}, "cost": "Infinity",
            "usage": {"prompt": "9" * 1_000, "completion": "bogus", "total": -1},
        },
    }
    tail = {
        "name": "llm.good", "kind": "generation", "trace_id": "tr0", "span_id": "tail",
        "parent_id": "root", "start": 2.0, "duration_s": 0.25,
        "attributes": {
            "node_id": 0, "input": [{"role": "user", "content": "continue"}],
            "output": "ok", "cost": 0.25,
            "usage": {"prompt": 5, "completion": 2, "total": 7},
        },
    }
    raw_spans = [root, bad_attributes, invalid_ids, bad_numbers, tail]
    sp = _write_spans(rd, raw_spans)

    loaded = load_spans(sp)
    assert [span["span_id"] for span in loaded] == ["root", "attrs-bad", "dirty", "tail"]
    dirty = next(span for span in loaded if span["span_id"] == "dirty")
    assert dirty["parent_id"] is None and dirty["start"] == dirty["duration_s"] == 0.0
    assert dirty["attributes"]["usage"] == {"prompt": 0, "completion": 0, "total": 0}
    assert dirty["attributes"]["cost"] == 0.0
    assert "phase_span" not in dirty["attributes"] and "input_from" not in dirty["attributes"]

    reference = build_trace_view(ST, loaded, light=True)
    assert reference["summary"]["spans"] == 4 and reference["summary"]["tools"] == 1
    assert reference["summary"]["tokens"] == {
        "prompt": 5, "completion": 2, "total": 7, "context": 5,
    }
    assert reference["summary"]["cost"] == 0.25 and "0" in reference["nodes"]
    assert build_conversation(ST, raw_spans, 0)["stages"]
    assert _tree(raw_spans)

    idx = get_index(sp)
    assert [span["span_id"] for span in idx.light_spans()] == [
        "root", "attrs-bad", "dirty", "tail",
    ]
    assert _canon(reference) == _canon(build_trace_view(ST, idx.light_spans(), light=True))
    assert (idx.full_span("dirty") or {})["attributes"]["usage"]["prompt"] == 0
    assert (idx.full_span("tail") or {}).get("span_id") == "tail"


def test_conversation_parent_cycles_degrade_without_hanging():
    script = r'''
import json
from types import SimpleNamespace
from looplab.events.traceview import build_conversation

state = SimpleNamespace(run_id="demo", task_id="t")
cases = {
    "self": [
        {"name": "self", "kind": "generation", "trace_id": "self-trace", "span_id": "self",
         "parent_id": "self", "start": 1, "attributes": {"node_id": 0, "input": []}},
    ],
    "two": [
        {"name": "gen", "kind": "generation", "trace_id": "two-trace", "span_id": "a",
         "parent_id": "b", "start": 1, "attributes": {"node_id": 0, "input": []}},
        {"name": "tool", "kind": "tool", "trace_id": "two-trace", "span_id": "b",
         "parent_id": "a", "start": 2, "attributes": {"node_id": 0}},
    ],
}
out = {}
for name, spans in cases.items():
    projection = build_conversation(state, spans, 0)
    out[name] = [[turn["type"] for turn in stage["turns"]]
                 for stage in projection["stages"]]
print(json.dumps(out, sort_keys=True))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(completed.stdout) == {
        "self": [["request", "generation"]],
        "two": [["request", "generation", "tool"]],
    }


def test_parent_walk_has_a_hard_depth_limit():
    by_id = {
        str(i): {"span_id": str(i), "parent_id": str(i + 1)}
        for i in range(_MAX_PARENT_HOPS + 10)
    }
    start = {"span_id": "start", "parent_id": "0"}
    assert len(list(_iter_parent_spans(start, by_id))) == _MAX_PARENT_HOPS


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


def test_node_trace_view_is_o_node_and_identical(run):
    """Building the node timeline over ONLY the node's spans (light_spans_for_node, in-memory) yields
    the SAME nodes[nid]/rollup as the whole-run build_trace_view — so node-detail is O(node), not O(run),
    with no change to what the UI renders."""
    rd, sp, spans = run
    idx = get_index(sp)
    whole = build_trace_view(ST, idx.light_spans(), light=True)
    for nid in (0, 1):
        per_node = build_trace_view(ST, idx.light_spans_for_node(nid), light=True)
        assert str(nid) in per_node["nodes"]
        assert _canon(per_node["nodes"][str(nid)]) == _canon(whole["nodes"][str(nid)])
        assert _canon(per_node["rollups"][str(nid)]) == _canon(whole["rollups"][str(nid)])
        # O(node): only this node's traces were read (the fixture has single-node traces)
        assert set(per_node["nodes"].keys()) == {str(nid)}


def test_node_trace_generation_fence_precedes_limit(tmp_path):
    """A reset reuses node_id, but each lifecycle keeps an exact, independently capped trace."""
    rd = tmp_path / "demo"
    rd.mkdir()
    spans = [
        {"name": "old-attempt", "kind": "operation", "trace_id": "old", "span_id": "old-root",
         "parent_id": None, "run_id": "demo", "attributes": {"node_id": 0},
         "events": [], "status": "OK", "start": 0.0, "duration_s": 1.0},
        {"name": "current-attempt", "kind": "operation", "trace_id": "current",
         "span_id": "current-root", "parent_id": None, "run_id": "demo",
         "attributes": {"node_id": 0, "generation": 1},
         "events": [], "status": "OK", "start": 2.0, "duration_s": 1.0},
    ]
    idx = get_index(_write_spans(rd, spans))

    assert [row["name"] for row in idx.light_spans_for_node(
        0, 1, generation=0)] == ["old-attempt"]
    assert [row["name"] for row in idx.light_spans_for_node(
        0, 1, generation=1)] == ["current-attempt"]
    assert idx.node_span_count(0, generation=0) == 1
    assert idx.node_span_count(0, generation=1) == 1


def test_persist_rewrites_are_geometric(run, monkeypatch):
    """The persisted index is re-written only when coverage grows ~1.5x — so a live run's total index-
    write volume is O(n), not an ~O(n^2) full rewrite every few MB (each a full-object PUT on S3/geesefs).
    Grow the file ~18x and assert the re-persists are logarithmic in count and geometric in coverage."""
    rd, sp, spans = run
    covers_at_write: list = []
    import looplab.events.span_index as si
    orig = si.atomic_write_bytes

    def spy(p, b):
        if getattr(p, "name", "") == "spans.index.jsonl":
            try:
                covers_at_write.append(orjson.loads(b.split(b"\n", 1)[0]).get("covers"))
            except Exception:  # noqa: BLE001
                pass
        return orig(p, b)

    monkeypatch.setattr(si, "atomic_write_bytes", spy)
    get_index(sp)                                       # initial build + persist
    for k in range(40):                                 # 40 small appends → ~18x growth
        with open(sp, "ab") as f:
            for s in _spans_for(1000 + k, f"tg{k}"):
                f.write(orjson.dumps(s) + b"\n")
        get_index(sp)
    assert 1 < len(covers_at_write) <= 12, covers_at_write        # logarithmic, not ~40 rewrites
    ratios = [b / a for a, b in zip(covers_at_write, covers_at_write[1:]) if a]
    assert all(r >= 1.49 for r in ratios), covers_at_write        # each re-persist grew ≥ ~1.5x


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
    hdr = json.loads(lines[0])
    hdr["ino"] = hdr.get("ino", 0) + 999999
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


def test_unreadable_source_raises_on_cached_and_cold_lookup(run, monkeypatch):
    """A stable stat plus denied open is unavailable, never a complete-looking cached prefix."""
    import builtins

    _rd, sp, _spans = run
    assert get_index(sp).span_count() > 0                    # warm the in-process cache first
    real_open = builtins.open

    def denied(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if isinstance(file, (str, os.PathLike)) and Path(file) == sp and "r" in str(mode):
            raise PermissionError("simulated trace ACL loss")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied)
    with pytest.raises(PermissionError):
        get_index(sp)                                      # cached index still probes the source
    invalidate(sp)
    with pytest.raises(PermissionError):
        get_index(sp)                                      # cold rebuild cannot publish empty truth


def test_exact_reader_handles_fragmented_reads_and_rejects_early_eof():
    import io

    payload = b"abcdefghijklmnopqrstuvwxyz"

    class Fragmented(io.BytesIO):
        def read(self, size=-1):
            return super().read(max(1, size // 3) if size > 1 else size)

    assert span_index._read_exact(
        Fragmented(payload), len(payload), label="test source") == payload

    class EarlyEof(io.BytesIO):
        def __init__(self, value):
            super().__init__(value)
            self.calls = 0

        def read(self, size=-1):
            self.calls += 1
            return super().read(max(1, size // 2)) if self.calls == 1 else b""

    with pytest.raises(OSError, match="short read of test source"):
        span_index._read_exact(EarlyEof(payload), len(payload), label="test source")


def test_concurrent_topup_and_reads_are_safe(run):
    """Thread safety: the serve threadpool calls the READ methods lock-free while another request's
    get_index() tops up the SAME cached index (appending to node_tids/by_tid/light). Without the
    per-index snapshot lock, `full_spans_for_node`'s `for tid in node_tids[...]` races a concurrent
    `.add()` → 'set changed size during iteration'. Hammer both paths concurrently: no exception, and
    every read returns a self-consistent result. (Also guards lock ORDER — a deadlock would hang.)"""
    import threading

    import orjson
    rd, sp, spans = run
    # Give node 0 many traces so the set-iteration window is wide.
    with open(sp, "ab") as f:
        for k in range(500):
            f.write(orjson.dumps({"name": "o", "kind": "operation", "trace_id": f"x{k}",
                                  "span_id": f"xs{k}", "parent_id": None, "run_id": "demo",
                                  "attributes": {"node_id": 0}, "events": [], "status": "OK",
                                  "start": 0.0, "duration_s": 1.0}) + b"\n")
    get_index(sp)
    errors: list = []
    stop = threading.Event()

    def writer():
        k = 1000
        while not stop.is_set():
            try:
                with open(sp, "ab") as f:
                    f.write(orjson.dumps({"name": "o", "kind": "operation", "trace_id": f"y{k}",
                                          "span_id": f"ys{k}", "parent_id": None, "run_id": "demo",
                                          "attributes": {"node_id": 0}, "events": [], "status": "OK",
                                          "start": 0.0, "duration_s": 1.0}) + b"\n")
                k += 1
                get_index(sp)                       # topup → node_tids["0"].add(...) under the lock
            except Exception as e:                  # noqa: BLE001
                errors.append(repr(e))

    def reader():
        while not stop.is_set():
            try:
                idx = get_index(sp)
                idx.full_spans_for_node(0)          # snapshots node_tids/by_tid under the lock
                idx.light_spans()
            except Exception as e:                  # noqa: BLE001
                errors.append(repr(e))

    ts = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(6)]
    for t in ts:
        t.start()
    stop.wait(2.0)
    stop.set()
    for t in ts:
        t.join(5)
    assert not any(t.is_alive() for t in ts), "a thread hung — possible lock-order deadlock"
    assert errors == [], f"concurrent read/topup raised: {errors[:3]}"


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

    # /spans/{sid}: bounded/redacted I/O projection for one observation, fetched by byte offset.
    span = client.get("/api/runs/demo/spans/g0_1").json()
    assert len(span["attributes"]["output"]) <= 2000
    assert len(span["attributes"]["thinking"]) <= 2000
    assert span["projection"]["truncated"] is True

    # /conversation: the node's linear thread (reads only that node's byte ranges).
    convo = client.get("/api/runs/demo/nodes/0/conversation").json()
    assert convo["node_id"] == "0" and convo["stages"]

    # /trace/by_trace: one operation's tree.
    bt = client.get("/api/runs/demo/trace/by_trace/tr1").json()
    assert bt["count"] == len(_spans_for(1, "tr1"))

    # /nodes/{nid}/trace: the O(node) per-node timeline (hot path for lazy trace-card expand).
    nt = client.get("/api/runs/demo/nodes/0/trace").json()
    assert nt["node_id"] == 0 and nt["attempt"] == 0
    assert isinstance(nt["nodes"], list) and len(nt["nodes"]) >= 1   # node 0's tree (its create_node root)
    assert nt["rollup"].get("generations") == 3                      # node 0's 3 generations, not the run's 6

    # clear_trace rewrites spans.jsonl (invalidating byte offsets) → the index is dropped and the next
    # read rebuilds cleanly against the shrunk file (node 0 gone, node 1 kept).
    # clear_trace requires the exact run/node/trace identities (a destructive whole-file rewrite must
    # not be issuable from a stale view), so submit them the way a real client does.
    _generation = next(r for r in client.get("/api/runs").json()
                       if r["run_id"] == "demo")["generation"]
    _node = client.get("/api/runs/demo/nodes/0").json()
    _cleared = client.post("/api/runs/demo/nodes/0/clear_trace", json={
        "expected_generation": _generation,
        "expected_trace_revision": _node.get("trace_revision"),
        "node_generation": _node.get("attempt", 0),
        "operation_id": "tc_" + "f" * 32,
    })
    assert _cleared.json()["removed"] == len(_spans_for(0, "tr0"))
    tv2 = client.get("/api/runs/demo/trace").json()
    assert set(tv2["nodes"].keys()) == {"1"}
    assert client.get("/api/runs/demo/spans/g0_1").json()["attributes"] == {}   # node 0's span is gone


def test_node_trace_endpoint_can_read_each_reset_attempt_exactly(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    rd = tmp_path / "demo"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {
        "node_id": 0, "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    store.append("node_evaluated", {
        "node_id": 0, "generation": 0, "metric": 1.0, "eval_seconds": 1.0})
    store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    _write_spans(rd, [
        {"name": "old-attempt", "kind": "operation", "trace_id": "old", "span_id": "old-root",
         "parent_id": None, "run_id": "demo", "attributes": {"node_id": 0},
         "events": [], "status": "OK", "start": 0.0, "duration_s": 1.0},
        {"name": "current-attempt", "kind": "operation", "trace_id": "current",
         "span_id": "current-root", "parent_id": None, "run_id": "demo",
         "attributes": {"node_id": 0, "generation": 1},
         "events": [], "status": "OK", "start": 2.0, "duration_s": 1.0},
    ])
    client = TestClient(make_app(tmp_path))

    old = client.get("/api/runs/demo/nodes/0/trace", params={"attempt": 0}).json()
    current = client.get("/api/runs/demo/nodes/0/trace", params={"attempt": 1}).json()
    assert old["node_id"] == 0 and old["attempt"] == 0
    assert current["node_id"] == 0 and current["attempt"] == 1
    assert "old-attempt" in json.dumps(old) and "current-attempt" not in json.dumps(old)
    assert "current-attempt" in json.dumps(current) and "old-attempt" not in json.dumps(current)


def test_trace_cache_rejects_same_size_same_mtime_file_replacement(tmp_path):
    """Atomic replacement must not return generation A's cached trace when size+mtime are preserved."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app

    rd = _http_run(tmp_path)
    sp = rd / "spans.jsonl"
    client = TestClient(make_app(tmp_path))
    first = client.get("/api/runs/demo/trace").json()
    assert "tr0" in json.dumps(first)

    before = sp.stat()
    replacement = rd / "replacement.jsonl"
    replacement.write_bytes(sp.read_bytes().replace(b'"tr0"', b'"zr0"'))
    assert replacement.stat().st_size == before.st_size
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(sp)
    after = sp.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)

    second = client.get("/api/runs/demo/trace").json()
    rendered = json.dumps(second)
    assert "zr0" in rendered and "tr0" not in rendered


def test_trace_carries_run_id_even_with_an_unfoldable_log(tmp_path):
    """Degraded path: `trace_scalars` reads run_id/task_id from the folded state, but if events.jsonl
    can't be folded it must still return the correct run_id (from the run dir name) so /trace's run_id
    matches the pre-index endpoint's behavior — not an empty string — while the span tree still renders."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    rd = _http_run(tmp_path)
    # Corrupt the FIRST event line so the fold yields no run_started (torn/garbage tail rule).
    (rd / "events.jsonl").write_bytes(b"{not valid json at all\n")
    client = TestClient(make_app(tmp_path))
    tv = client.get("/api/runs/demo/trace").json()
    assert tv["run_id"] == "demo"                      # falls back to the run dir name, never ""
    assert set(tv["nodes"].keys()) == {"0", "1"}       # the span tree still renders from the index


def test_persisted_index_negative_length_does_not_slurp_and_rebuilds(run):
    """R6-F1.2: a corrupt persisted `_l` (negative/oversized) must never reach `f.read(length)` (a
    negative length reads the whole file into memory). _load_persisted treats it as a torn tail and the
    caller tops up the rest from spans.jsonl, so the result still matches a full rebuild."""
    rd, sp, spans = run
    get_index(sp)
    span_index._CACHE.clear()
    ip = rd / "spans.index.jsonl"
    lines = ip.read_bytes().split(b"\n")
    # Corrupt a MIDDLE record's length to a negative value (header is line 0; pick line 2).
    rec = json.loads(lines[2])                                  # line 0 = header, line 2 = the 2nd span
    victim_id = rec["span_id"]
    rec["_l"] = -1
    lines[2] = orjson.dumps(rec)
    ip.write_bytes(b"\n".join(lines))
    idx = get_index(sp)                                          # must not slurp/crash; tops up from truth
    ref = build_trace_view(ST, load_spans(sp), light=True)
    assert _canon(ref) == _canon(build_trace_view(ST, idx.light_spans(), light=True))
    # The negative length was rejected at load and the row rebuilt from spans.jsonl, so meta never
    # carries the -1 that would drive `f.read(-1)`; the full span still resolves to the RIGHT span.
    row = idx.by_sid[victim_id]
    assert idx.meta[row][1] >= 0
    assert (idx.full_span(victim_id) or {}).get("span_id") == victim_id


def test_persisted_index_offset_drift_returns_none_not_wrong_span(run):
    """R6-F1.1: if a persisted offset drifts onto a DIFFERENT but still-valid span line (bit-rot on a
    network mount), full_span must return None — never the neighboring span as if it were the requested
    one. The read cross-checks the span_id against the row it indexes."""
    rd, sp, spans = run
    get_index(sp)
    span_index._CACHE.clear()
    ip = rd / "spans.index.jsonl"
    lines = [line for line in ip.read_bytes().split(b"\n") if line]
    recs = [json.loads(line) for line in lines[1:]]                   # skip header
    by_id = {r["span_id"]: r for r in recs}
    victim, other = by_id["g0_1"], by_id["g1_0"]               # both middle spans (last stays intact)
    victim["_o"], victim["_l"] = other["_o"], other["_l"]      # point g0_1's row at g1_0's bytes
    out = [lines[0]] + [orjson.dumps(r) for r in recs]
    ip.write_bytes(b"\n".join(out) + b"\n")
    idx = get_index(sp)                                          # loads the tampered index (spotcheck = last span)
    # Confirm the TAMPERED index actually loaded (g0_1's row now carries g1_0's byte range) so the
    # drift path is genuinely exercised — not silently rebuilt, which would make the check trivial.
    row = idx.by_sid["g0_1"]
    assert idx.meta[row] == (other["_o"], other["_l"])
    # The drifted row reads g1_0's bytes; the span_id mismatch is detected → None, never the wrong span.
    assert idx.full_span("g0_1") is None
    assert (idx.full_span("g1_0") or {}).get("span_id") == "g1_0"   # the intact row still resolves


def test_a_cold_rebuild_never_materializes_the_whole_source(tmp_path):
    """The index is ~30x smaller than `spans.jsonl`; deriving it must not first hold the source whole.

    A cold rebuild read the entire file into one bytes object before parsing a line of it, so peak
    memory tracked the SOURCE (~1.0x) — a multi-GB trace could exhaust the server to produce a
    lightweight index. The scan is chunked now, so peak is set by the derived index instead.
    """
    import tracemalloc

    # FEW spans, each with a lot of generation I/O — the real shape of a long run, and the one that
    # separates "peak tracks the source" from "peak tracks the index the source produces".
    heavy = "x" * 300_000           # the generation I/O the light projection throws away
    source = tmp_path / "spans.jsonl"
    with open(source, "w", encoding="utf-8") as f:
        for i in range(60):
            f.write(json.dumps({
                "span_id": f"s{i}", "trace_id": "t", "parent_id": None, "kind": "generation",
                "name": "gen", "t0": 0.0, "t1": 1.0, "attrs": {"node_id": "0"},
                "input": heavy, "output": heavy,
            }) + "\n")
    size = source.stat().st_size
    assert size > 30 * span_index._SCAN_CHUNK_BYTES, "the source must span many scan chunks"

    span_index._CACHE.clear()
    tracemalloc.start()
    try:
        idx = get_index(source)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
        span_index._CACHE.clear()

    assert idx is not None and idx.covers == size and len(idx.light_spans()) == 60
    assert peak < size * 0.6, (
        f"peak {peak} tracked the {size}-byte source, so a multi-GB trace still OOMs the server "
        "to build an index a fraction of its size")


def test_the_chunked_scan_is_indistinguishable_from_one_that_reads_it_all(tmp_path, monkeypatch):
    """Chunk boundaries are invisible: a line split across reads, a torn tail, and a corrupt line
    mid-file must each land exactly where an unchunked scan put them."""
    rows = [
        json.dumps({"span_id": f"s{i}", "trace_id": "t", "parent_id": None, "kind": "span",
                    "name": "n", "t0": 0.0, "t1": 1.0, "attrs": {"node_id": "0"}})
        for i in range(6)
    ]
    bodies = {
        "clean": "\n".join(rows) + "\n",
        "torn_tail": "\n".join(rows) + "\n" + rows[0][:20],          # no trailing newline
        "corrupt_middle": "\n".join(rows[:3]) + "\n{not json}\n" + "\n".join(rows[3:]) + "\n",
    }
    for label, body in bodies.items():
        source = tmp_path / f"{label}.jsonl"
        source.write_text(body, encoding="utf-8")
        results = {}
        for chunk in (7, 64, 1024 * 1024):         # 7 bytes splits nearly every line
            monkeypatch.setattr(span_index, "_SCAN_CHUNK_BYTES", chunk)
            span_index._CACHE.clear()
            (source.parent / "spans.index.jsonl").unlink(missing_ok=True)
            idx = get_index(source)
            results[chunk] = ([s["span_id"] for s in idx.light_spans()], idx.covers)
        span_index._CACHE.clear()
        assert len(set(map(str, results.values()))) == 1, (
            f"[{label}] the chunk size changed what the scan accepted: {results}")

    # …and the durability rule itself still holds: a corrupt line stops the scan at the last good
    # boundary, so the rows behind it are NOT indexed.
    monkeypatch.setattr(span_index, "_SCAN_CHUNK_BYTES", 64)
    span_index._CACHE.clear()
    (tmp_path / "spans.index.jsonl").unlink(missing_ok=True)
    idx = get_index(tmp_path / "corrupt_middle.jsonl")
    assert [s["span_id"] for s in idx.light_spans()] == ["s0", "s1", "s2"]
    span_index._CACHE.clear()


def test_a_span_line_far_larger_than_one_chunk_is_scanned_linearly(tmp_path, monkeypatch):
    """Multi-MB generation spans are the norm in the files this module targets. Re-joining and
    re-scanning the growing carry at every chunk boundary made ONE such line cost O(L^2 / chunk) —
    a cold rebuild of a big trace paying quadratically for the largest span in it."""
    chunk = 4096
    big = "x" * (200 * chunk)                       # one line spanning ~200 chunks
    rows = [json.dumps({"span_id": "s0", "trace_id": "t", "parent_id": None, "kind": "span",
                        "name": big, "t0": 0.0, "t1": 1.0, "attrs": {"node_id": "0"}}),
            json.dumps({"span_id": "s1", "trace_id": "t", "parent_id": None, "kind": "span",
                        "name": "n", "t0": 0.0, "t1": 1.0, "attrs": {"node_id": "0"}})]
    source = tmp_path / "spans.jsonl"
    source.write_text("\n".join(rows) + "\n", encoding="utf-8")
    size = source.stat().st_size

    scanned = []
    real_scan = span_index._scan_light
    monkeypatch.setattr(span_index, "_scan_light",
                        lambda window, base: (scanned.append(len(window)), real_scan(window, base))[1])
    monkeypatch.setattr(span_index, "_SCAN_CHUNK_BYTES", chunk)
    span_index._CACHE.clear()
    (tmp_path / "spans.index.jsonl").unlink(missing_ok=True)

    idx = get_index(source)
    span_index._CACHE.clear()

    assert [s["span_id"] for s in idx.light_spans()] == ["s0", "s1"]      # still fully indexed
    assert sum(scanned) <= 2 * size, (
        f"scanned {sum(scanned)} bytes over {len(scanned)} calls for a {size}-byte file — the "
        "over-chunk line is being re-scanned at every chunk boundary")


def test_an_unknown_span_id_does_not_re_walk_the_whole_spans_file(tmp_path, monkeypatch):
    """`GET /api/runs/{id}/spans/{sid}` falls back to a scan when the index cannot resolve `sid`.
    That fallback exists for a span appended PAST the indexed tail — a handful of lines — but `sid`
    is an unvalidated path param, so a bogus id took the same path and read the ENTIRE (up to ~1 GB)
    spans.jsonl from byte 0 to find nothing, pinning a threadpool thread per request. Everything
    below the index's coverage boundary was already searched by `full_span`, so re-walking it can
    only ever find nothing; the scan must start there."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.events import eventstore
    from looplab.serve import routers
    from looplab.serve.server import make_app

    rd = _http_run(tmp_path)
    big = _spans_for(0, "tr0") + _spans_for(1, "tr1")
    for i in range(400):                       # a spans.jsonl big enough for a full walk to show up
        big.append(_gen(1, "tr1", f"pad{i}", "root1", i + 1))
    _write_spans(rd, big)
    client = TestClient(make_app(tmp_path))
    assert client.get("/api/runs/demo/trace").status_code == 200      # build + persist the index

    read_bytes = [0]
    real_open = open

    class _CountingFile:
        """Proxy, not attribute patching: `for line in f` resolves `__iter__` on the TYPE, so a
        wrapper set on the file INSTANCE never sees a line-by-line walk at all."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def __iter__(self):
            for line in self._inner:
                read_bytes[0] += len(line)
                yield line

        def read(self, *a, **kw):
            chunk = self._inner.read(*a, **kw)
            read_bytes[0] += len(chunk)
            return chunk

    def counting_open(*a, **kw):
        handle = real_open(*a, **kw)
        if not str(a[0] if a else kw.get("file", "")).endswith("spans.jsonl"):
            return handle                      # only spans.jsonl traffic is under test
        return _CountingFile(handle)

    # BOTH modules: the bounded scan lives in `routers.runs`, the whole-file walk it replaced ran
    # through `iter_jsonl`, whose `open` resolves in `events.eventstore`.
    monkeypatch.setattr(routers.runs, "open", counting_open, raising=False)
    monkeypatch.setattr(eventstore, "open", counting_open, raising=False)
    missing = client.get("/api/runs/demo/spans/no-such-span-id")
    monkeypatch.undo()

    # A span that is not there is reported as unavailable (200 with an empty projection), not 404.
    assert missing.status_code == 200 and not missing.json().get("attributes"), missing.json()
    assert read_bytes[0] < (rd / "spans.jsonl").stat().st_size // 4, (
        read_bytes[0], (rd / "spans.jsonl").stat().st_size)
    # …and a span that IS in the file is still served.
    assert client.get("/api/runs/demo/spans/g0_1").json()["span_id"] == "g0_1"


# --- one span-to-node attribution rule (doc 25 EV-10) -------------------------------------------
#
# "A span's effective node is its own stamped node_id, else its trace ROOT's node_id" was written
# out three times, kept equivalent only by comments — one of which asserted the equivalence
# "exactly" while the two derivations actually disagreed.

def _live_trace():
    """The LIVE shape `build_conversation`'s own comment describes: an operation span is written only
    on CLOSE and `create_node` closes at node END, so a running node's trace has NO root on disk and
    its spans are ORPHANS — their parent_id names a span that is not there yet. Here that orphan
    (node 7) starts before a later true root (node 9), which is what made the two derivations pick
    different spans."""
    return [
        {"name": "implement", "kind": "generation", "trace_id": "T", "span_id": "orphan",
         "parent_id": "not-yet-closed-root", "start": 1.0, "attributes": {"node_id": 7, "input": []}},
        {"name": "later", "kind": "tool", "trace_id": "T", "span_id": "true-root",
         "parent_id": None, "start": 5.0, "attributes": {"node_id": 9}},
        # The span under test: no node_id of its own, so it can only be attributed via the trace root.
        {"name": "child", "kind": "tool", "trace_id": "T", "span_id": "child",
         "parent_id": "orphan", "start": 2.0, "attributes": {}},
    ]


def test_an_orphan_headed_trace_attributes_the_same_way_in_both_views():
    """The divergence itself. A node-idless span in a live trace was placed under node 7 by
    `build_trace_view` and under node 9 by `build_conversation` — the same span, two different nodes,
    depending on which view the operator opened."""
    spans = _live_trace()

    view = build_trace_view(ST, spans)
    owner = next(nid for nid, tree in view["nodes"].items() if "child" in _names(tree))

    # And the conversation must agree: the child belongs to the SAME node, not the other one.
    mine = build_conversation(ST, spans, int(owner))
    other = build_conversation(ST, spans, 9 if int(owner) == 7 else 7)
    assert _turn_names(mine) >= {"child"}, (owner, _turn_names(mine))
    assert "child" not in _turn_names(other), (owner, _turn_names(other))

    # ...and per-span stamping still WINS over the trace root. `later` carries its own node 9 inside
    # a trace whose root is node 7 — the long-lived Developer tool-loop shape that per-span stamping
    # exists for, where keying off the root hands one node's spans to another. Asserted on the trace
    # VIEW, where attribution is directly observable: a lone tool span with no generation parent
    # produces no conversation TURN, so the conversation cannot witness this half.
    assert "later" in _names(view["nodes"]["9"]), view["nodes"].keys()
    assert "later" not in _names(view["nodes"]["7"])


def _names(tree):
    out = set()
    stack = list(tree)
    while stack:
        node = stack.pop()
        out.add(node.get("name"))
        stack.extend(node.get("children") or ())
    return out


def _turn_names(projection):
    return {turn.get("name") for stage in projection["stages"] for turn in stage["turns"]}


def test_the_attribution_root_accepts_an_orphan_but_the_structural_root_does_not():
    """Why the two disagreed, pinned as a property rather than as a story: attribution must accept an
    orphan (the live case), while `build_conversation`'s structural `root` — which names the stage and
    stands in as a band container — deliberately requires `parent_id is None`. Collapsing them in
    either direction reintroduces the bug or breaks stage naming."""
    from looplab.events.traceview import _normalize_spans, trace_root_node_id

    spans = _normalize_spans(_live_trace())
    assert trace_root_node_id(spans, _normalized=True) == 7      # the earliest orphan, not the root

    structural = next((s for s in sorted(spans, key=lambda x: x.get("start", 0.0))
                       if s.get("parent_id") is None), None)
    assert structural is not None and structural["span_id"] == "true-root"


def test_both_views_attribute_through_the_shared_rule():
    """The two projections reach attribution through `trace_root_node_id`/`effective_node_id`.

    Comment-proof (`called_names` resolves real `ast.Call` nodes), but still an ENUMERATION of the two
    views — and that is exactly what it can and cannot do. It says these two have not stopped calling
    the shared rule; it says nothing about a THIRD site, which is the failure that actually happened.
    `test_no_site_re_derives_the_trace_root` below is the half that discovers rather than enumerates.
    """
    from _source_scan import called_names

    from looplab.events import traceview

    for name in ("build_trace_view", "build_conversation"):
        calls = called_names(getattr(traceview, name))
        assert "trace_root_node_id" in calls, f"{name} no longer uses the shared root rule"
        assert "effective_node_id" in calls, f"{name} no longer uses the shared attribution"


def test_indexed_and_unindexed_conversations_agree_on_an_orphan_headed_trace(tmp_path):
    """The equivalence the finding asks for, on the shape that broke. `SpanIndex` selects by TRACE
    (any span carrying the node id), the no-index path re-filters per span — so a disagreement about
    the trace root shows up as a node's turns appearing in one path and not the other."""
    rd = tmp_path / "demo"
    rd.mkdir()
    sp = _write_spans(rd, _live_trace())

    index = get_index(sp)
    for node_id in (7, 9):
        unindexed = build_conversation(ST, load_spans(sp), node_id)
        indexed = build_conversation(ST, index.full_spans_for_node(node_id), node_id)
        assert _turn_names(indexed) == _turn_names(unindexed), node_id


def test_an_unstamped_root_leaves_its_node_idless_spans_unscoped():
    """The rule is the trace ROOT's id, never "the first id found anywhere in the trace".

    A full ancestor walk (or any first-stamped-span scan) bleeds one node's id across a SHARED trace:
    a Developer tool-loop trace serves several nodes in sequence, so a span belonging to none of them
    would inherit whichever node happened to be stamped first. Here the root carries no node_id at
    all, so a node-idless sibling has no attribution and must land in `unscoped` — not under node 4
    merely because node 4's span sits later in the same trace."""
    spans = [
        {"name": "loop", "kind": "operation", "trace_id": "S", "span_id": "root",
         "parent_id": None, "start": 0.0, "attributes": {}},
        {"name": "stamped", "kind": "generation", "trace_id": "S", "span_id": "stamped",
         "parent_id": "root", "start": 1.0, "attributes": {"node_id": 4, "input": []}},
        {"name": "drifter", "kind": "tool", "trace_id": "S", "span_id": "drifter",
         "parent_id": "root", "start": 2.0, "attributes": {}},
    ]
    view = build_trace_view(ST, spans)
    assert "drifter" in _names(view["unscoped"]), sorted(view["nodes"])
    assert "drifter" not in _names(view["nodes"].get("4", []))
    assert "stamped" in _names(view["nodes"]["4"])       # its OWN stamp still places it


# --- ...and the FOURTH copy, which the first cut left behind (doc 25 EV-10, second cut) ----------
#
# EV-10's *Locations* names `span_index.py:388-421` as "a fourth root-resolution" and its
# *Recommendation* says to route it through the shared helper too; the first cut fixed the two VIEWS
# and its resolution note simply did not mention this one. The two derivations are not the same rule:
# `_rows_for_node` took the first root in FILE order, and file order is CLOSE order — spans.jsonl is
# written when a span ENDS — while the views take the earliest `start`. So the span that OPENED a
# trace is usually written LAST, and concurrent spans finish out of the order they began.

def _fan_out_trace():
    """The measured real shape, plus the `generation` stamp that makes the choice matter.

    Modelled on `runs/live-deps4-0804`, trace `c49e58adeb726df798e4d6182855ab7d`: five concurrent LLM
    generations under one still-open operation span, closing in a different order than they started
    (starts …183.883 / …183.880 / …183.879 written in that order). Across four consecutive index
    states the file-order root and the earliest-`start` root were different spans — so the divergence
    is not hypothetical, it is in the logs on disk.

    Here the earliest-`start` root is the lifecycle root that carries `generation`, and the
    first-in-file root is a short concurrent child whose own parent is absent (still open, or
    QUARANTINED — `_scan_light` drops a record that fails `_normalize_span` but still consumes it,
    which orphans its children permanently). Which root you pick decides which ATTEMPT the whole
    trace is fenced to."""
    return [
        # closed first, so written first — but started LAST
        {"name": "concurrent-gen", "kind": "generation", "trace_id": "T", "span_id": "gen-b",
         "parent_id": "gone", "run_id": "demo", "start": 200.0, "duration_s": 1.0,
         "status": "OK", "events": [], "attributes": {"node_id": 0, "input": []}},
        # closed last, so written last — but started FIRST, and carries the lifecycle stamp
        {"name": "evaluate", "kind": "operation", "trace_id": "T", "span_id": "eval-root",
         "parent_id": None, "run_id": "demo", "start": 100.0, "duration_s": 300.0,
         "status": "OK", "events": [], "attributes": {"node_id": 0, "generation": 1}},
    ]


def _file_order_root(spans):
    """The rule `_rows_for_node` used before the extraction, written out once here so the tests can
    show the two answers rather than assert one."""
    span_ids = {s.get("span_id") for s in spans}
    return next((s for s in spans if s.get("parent_id") not in span_ids), spans[0])


def test_file_order_and_start_order_pick_different_roots_on_a_concurrent_fan_out():
    """The disagreement itself, before any consequence: the two orderings name different spans."""
    from looplab.events.traceview import _normalize_spans, trace_root_span

    spans = _normalize_spans(_fan_out_trace())
    assert _file_order_root(spans)["span_id"] == "gen-b"          # what span_index used to read
    assert trace_root_span(spans, _normalized=True)["span_id"] == "eval-root"


def test_the_generation_fence_reads_the_root_the_views_attribute_from(tmp_path):
    """The consequence, driven through the real index and the real view.

    `light_spans_for_node` / `node_span_count` feed `appstate.node_trace_view`, i.e. the
    `/api/runs/{id}/nodes/{nid}/trace?attempt=N` card: its span TREE, its token/cost `rollup` and the
    `total_spans` receipt behind "showing X of Y". Fencing off `gen-b` (no `generation` → 0) instead
    of `eval-root` (generation 1) hands attempt 1 an empty tree and a zeroed rollup while attributing
    the spans to an attempt-0 lifecycle that never existed — and `build_trace_view`, reading the same
    trace through the shared rule, disagrees with it."""
    from looplab.events.traceview import _normalize_spans, trace_root_span

    rd = tmp_path / "demo"
    rd.mkdir()
    idx = get_index(_write_spans(rd, _fan_out_trace()))

    # The view's root for this trace is the stamped lifecycle root...
    root = trace_root_span(_normalize_spans(_fan_out_trace()), _normalized=True)
    assert root["attributes"]["generation"] == 1
    # ...so the fence must agree: generation 1 holds the node's spans, generation 0 holds none.
    assert [row["name"] for row in idx.light_spans_for_node(0, generation=1)] == [
        "concurrent-gen", "evaluate"]
    assert idx.node_span_count(0, generation=1) == 2
    assert idx.light_spans_for_node(0, generation=0) == []
    assert idx.node_span_count(0, generation=0) == 0
    # The unfenced read is unchanged — the fence is the only thing the root decides here.
    assert idx.node_span_count(0) == 2


def test_a_rootless_trace_does_not_nominate_an_arbitrary_span(tmp_path):
    """A parent_id CYCLE (corrupt/crafted source only) has no root. The old fallback took
    `trace_rows[0]` and read ITS `generation` as the whole trace's — so a corrupt log could file a
    node's spans under a lifecycle number found on one arbitrary span. The shared rule declines to
    name a root, the view already reached the same verdict, and the fence lands on the documented
    unstamped default instead."""
    from looplab.events.traceview import _normalize_spans, trace_root_span

    spans = [
        {"name": "a", "kind": "operation", "trace_id": "C", "span_id": "a", "parent_id": "b",
         "run_id": "demo", "start": 1.0, "attributes": {"node_id": 0, "generation": 3}},
        {"name": "b", "kind": "operation", "trace_id": "C", "span_id": "b", "parent_id": "a",
         "run_id": "demo", "start": 2.0, "attributes": {"node_id": 0}},
    ]
    assert trace_root_span(_normalize_spans(spans), _normalized=True) is None
    assert _tree(spans) == []                                  # the view reaches the same verdict

    rd = tmp_path / "demo"
    rd.mkdir()
    idx = get_index(_write_spans(rd, spans))
    assert idx.node_span_count(0, generation=0) == 2            # the unstamped default...
    assert idx.node_span_count(0, generation=3) == 0            # ...not span `a`'s own stamp


def test_the_shared_root_is_the_span_the_forest_puts_first():
    """`trace_root_span` skips `_tree`'s forest for speed (the forest copies every span dict, and this
    runs on the per-node read path over a hundreds-of-MB file). That is only safe while the two agree
    — including on the details nobody thinks about: `start` ties, duplicate span_ids (both collapse to
    the LAST occurrence), orphans, and several roots in one trace."""
    from looplab.events.traceview import _normalize_spans, trace_root_span

    def span(sid, parent, start, **attrs):
        return {"name": sid, "kind": "operation", "trace_id": "T", "span_id": sid,
                "parent_id": parent, "run_id": "demo", "start": start, "attributes": attrs}

    shapes = [
        _fan_out_trace(),
        _live_trace(),
        [],                                                                # nothing at all
        [span("r", None, 0.0), span("c", "r", 1.0)],                       # one plain root
        [span("late", None, 9.0), span("early", None, 1.0)],               # two TRUE roots
        [span("a", None, 5.0), span("b", None, 5.0), span("c", None, 5.0)],  # a three-way tie
        [span("x", "missing", 2.0), span("y", "missing", 1.0)],            # orphans only
        [span("dup", None, 9.0), span("dup", None, 1.0)],                  # duplicate span_id
        [span("only", "self-parent", 1.0)],                                # a lone orphan
        [span("a", "b", 1.0), span("b", "a", 2.0)],                        # a cycle: no root
    ]
    # A randomized sweep on top of the named shapes, because the interesting disagreements are in
    # the COMBINATIONS (a tie among orphans while a true root exists later, and so on).
    rng = random.Random(20260805)
    for _ in range(400):
        sids = [f"s{i}" for i in range(rng.randint(1, 6))]
        shapes.append([span(sid, rng.choice([None, "absent", *sids]), rng.choice([0.0, 1.0, 2.0]))
                       for sid in sids])

    for shape in shapes:
        normalized = _normalize_spans(shape)
        forest = _tree(normalized, _normalized=True)
        shared = trace_root_span(normalized, _normalized=True)
        # `start` as well as the id: the duplicate-span_id shape collapses to ONE id either way, so
        # comparing ids alone would not notice the two disagreeing about WHICH duplicate survived.
        def ident(node):
            return None if node is None else (node["span_id"], node.get("start"))
        assert ident(shared) == ident(forest[0] if forest else None), shape


def test_no_site_re_derives_the_trace_root():
    """The guard that DISCOVERS a re-derivation instead of listing today's callers.

    The first cut of EV-10 pinned the pair `("build_trace_view", "build_conversation")` by name.
    `span_index._rows_for_node` — which doc 25's own *Locations* names as the fourth root-resolution —
    kept deriving its own root underneath that green test, with a resolution note that never
    mentioned it. A guard that enumerates the sites it knows about cannot report the site it does not,
    and "a fifth copy reopens it" is the shape that has now bitten this rule twice.

    The re-derivation has two AST fingerprints, and BOTH are followed through a hoisting variable —
    the original `build_trace_view` copy was `f = _tree(...)` then `f[0]`, one line apart, so a guard
    that only reads the inline spelling would have missed the very bug it is written for:

    * a function that speaks the span-structure vocabulary (reads BOTH the `"span_id"` and
      `"parent_id"` keys) AND asks whether a span's parent is PRESENT in some collection — the root
      rule spelled out. The `"span_id"` half is what keeps this off the node graph, where `parent_id`
      means a NODE's parent and `x not in state.aborted_nodes` is an unrelated question asked ~30
      times across `engine`/`search`/`replay`;
    * subscripting `_tree(...)`, the forest whose first element the rule used to be read off.
      Ungated, because `_tree` is traceview-private and nothing outside it has another use for one.

    Deliberately NOT caught, because it is a different rule: `build_conversation`'s structural `root`
    tests `parent_id is None` — strictly narrower than "parent not in this trace" — and genuinely
    wants that, because it names the stage and stands in as a band container.

    Known blind spot, stated rather than papered over: a re-derivation that never mentions
    `parent_id` (say `sorted(spans, key=start)[0]` on an already-filtered root list) is invisible to
    any source scan. What holds that line is behavioural — the equivalence tests above, which compare
    the index's answer against the view's on the shape where the two orders differ.
    """
    import ast

    from _source_scan import PKG, iter_trees

    # By PATH, not basename: a second file called `traceview.py` somewhere else in the package would
    # otherwise be allow-listed into the exact hiding place this guard exists to close.
    OWNER = "events/traceview.py"

    def literal_keys(node):
        """Every string key this function reads with `.get("k")` or `["k"]`."""
        out = set()
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "get" and sub.args
                    and isinstance(sub.args[0], ast.Constant)):
                out.add(sub.args[0].value)
            if isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant):
                out.add(sub.slice.value)
        return out

    def reads_a_spans_parent(node):
        """`x.get("parent_id")` / `x["parent_id"]` — the VALUE, not a `"parent_id" in d` key check."""
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and node.args):
            return isinstance(node.args[0], ast.Constant) and node.args[0].value == "parent_id"
        if isinstance(node, ast.Subscript):
            return isinstance(node.slice, ast.Constant) and node.slice.value == "parent_id"
        return False

    def builds_the_forest(node):
        """A call to `_tree(...)`, however it is spelled."""
        return (isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", "")) == "_tree")

    def bound_to(func, predicate):
        """Names this function assigns from an expression matching *predicate* — so a re-derivation
        hoisted into a local reads the same as one written inline."""
        names = set()
        for sub in ast.walk(func):
            if isinstance(sub, ast.Assign) and predicate(sub.value):
                names |= {t.id for t in sub.targets if isinstance(t, ast.Name)}
            elif (isinstance(sub, (ast.AnnAssign, ast.NamedExpr)) and sub.value is not None
                    and predicate(sub.value) and isinstance(sub.target, ast.Name)):
                names.add(sub.target.id)
        return names

    offenders = []
    for path, tree in iter_trees():
        rel = path.relative_to(PKG).as_posix()
        if rel == OWNER:
            continue
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            speaks_spans = {"span_id", "parent_id"} <= literal_keys(func)
            parents = bound_to(func, reads_a_spans_parent)
            forests = bound_to(func, builds_the_forest)
            for sub in ast.walk(func):
                if (speaks_spans and isinstance(sub, ast.Compare)
                        and any(isinstance(op, (ast.In, ast.NotIn)) for op in sub.ops)
                        and (reads_a_spans_parent(sub.left)
                             or (isinstance(sub.left, ast.Name) and sub.left.id in parents))):
                    offenders.append(f"{rel}:{func.name}:{sub.lineno} {ast.unparse(sub)}")
                if (isinstance(sub, ast.Subscript)
                        and (builds_the_forest(sub.value)
                             or (isinstance(sub.value, ast.Name) and sub.value.id in forests))):
                    offenders.append(f"{rel}:{func.name}:{sub.lineno} {ast.unparse(sub)}")

    assert offenders == [], (
        "a private trace-root resolution came back; call `traceview.trace_root_span` instead "
        f"(doc 25 EV-10): {offenders}")
