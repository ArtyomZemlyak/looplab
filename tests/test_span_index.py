"""The light span index (`events.span_index`): the accelerator behind the trace views on large runs.

It must be an INVISIBLE optimization — every read served through it is byte-identical to reading the
whole `spans.jsonl` the old way (`load_spans` + `build_trace_view`/`build_conversation`), while
touching only the light structure (timeline) or one node/span's byte range (detail). These tests pin
that equivalence plus the cache/persistence/invalidation contract (append-only top-up, cold reload
from the persisted index, rebuild on replace/shrink/corruption, graceful degrade)."""
from __future__ import annotations

import json
import os
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

    # /spans/{sid}: full (uncapped) I/O for one observation, fetched by byte offset.
    span = client.get("/api/runs/demo/spans/g0_1").json()
    assert span["attributes"]["output"] == "O" * 4000 and span["attributes"]["thinking"] == "T" * 4000

    # /conversation: the node's linear thread (reads only that node's byte ranges).
    convo = client.get("/api/runs/demo/nodes/0/conversation").json()
    assert convo["node_id"] == "0" and convo["stages"]

    # /trace/by_trace: one operation's tree.
    bt = client.get("/api/runs/demo/trace/by_trace/tr1").json()
    assert bt["count"] == len(_spans_for(1, "tr1"))

    # /nodes/{nid}/trace: the O(node) per-node timeline (hot path for lazy trace-card expand).
    nt = client.get("/api/runs/demo/nodes/0/trace").json()
    assert isinstance(nt["nodes"], list) and len(nt["nodes"]) >= 1   # node 0's tree (its create_node root)
    assert nt["rollup"].get("generations") == 3                      # node 0's 3 generations, not the run's 6

    # clear_trace rewrites spans.jsonl (invalidating byte offsets) → the index is dropped and the next
    # read rebuilds cleanly against the shrunk file (node 0 gone, node 1 kept).
    assert client.post("/api/runs/demo/nodes/0/clear_trace").json()["removed"] == len(_spans_for(0, "tr0"))
    tv2 = client.get("/api/runs/demo/trace").json()
    assert set(tv2["nodes"].keys()) == {"1"}
    assert client.get("/api/runs/demo/spans/g0_1").json()["attributes"] == {}   # node 0's span is gone


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
