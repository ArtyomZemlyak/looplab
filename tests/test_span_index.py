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
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import orjson
import pytest

from looplab.events import span_index
from looplab.events.span_index import get_index, invalidate
from looplab.events.traceview import (
    _MAX_PARENT_HOPS,
    _bounded_node_trace_tail,
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


def test_full_span_same_id_body_rewrite_fails_unavailable_on_row_digest(run):
    """A same-length row can keep its span_id while its diagnostic body becomes different evidence."""
    _rd, source, _spans = run
    idx = get_index(source)
    row = idx.by_sid["g0_1"]
    off, length = idx.meta[row]
    before = source.stat()
    with open(source, "r+b") as stream:
        stream.seek(off)
        original = stream.read(length)
        rewritten = original.replace(b'"output":"OOOO', b'"output":"PPPP', 1)
        assert len(rewritten) == len(original) and rewritten != original
        assert orjson.loads(rewritten)["span_id"] == orjson.loads(original)["span_id"] == "g0_1"
        stream.seek(off)
        stream.write(rewritten)
        stream.flush()
        os.fsync(stream.fileno())
    assert source.stat().st_ino == before.st_ino and source.stat().st_size == before.st_size

    with pytest.raises(OSError, match="source digest"):
        idx.full_span("g0_1")


def test_conversation_identical_via_node_offsets(run):
    rd, sp, spans = run
    for nid in (0, 1):
        ref = build_conversation(ST, load_spans(sp), nid)
        idx = get_index(sp)
        got = build_conversation(ST, idx.full_spans_for_node(nid), nid)
        assert _canon(ref) == _canon(got)
        assert got["stages"]                                     # non-empty (sanity)


def test_node_window_snapshot_tracks_full_rows_but_ignores_foreign_appends(run):
    """The conversation cursor is exact for one selected window, not global spans mtime."""
    from looplab.core.tracing import JsonlSpanExporter

    rd, source, _spans = run
    idx = get_index(source)
    original, total, _ = idx.node_window_snapshot(0, 512, generation=0)
    assert total == len(_spans_for(0, "tr0"))

    exporter = JsonlSpanExporter(source)
    exporter.export(_gen(1, "tr1", "foreign-late", "root1", 20))
    foreign, foreign_total, _ = get_index(source).node_window_snapshot(0, 512, generation=0)
    assert (foreign, foreign_total) == (original, total)

    exporter.export(_gen(0, "tr0", "own-late", "root0", 21))
    own, own_total, _ = get_index(source).node_window_snapshot(0, 512, generation=0)
    assert own != original and own_total == total + 1

    # A cold reader can derive the same candidate, but cannot use untrusted persisted digests for a
    # conditional response until that exact window has been checked against source bytes.
    with span_index._CACHE_LOCK:
        span_index._CACHE.clear()
    cold_idx = get_index(source)
    cold, cold_total, conditional_ok = cold_idx.node_window_snapshot(
        0, 512, generation=0)
    assert (cold, cold_total) == (own, own_total)
    assert conditional_ok is False
    assert cold_idx.full_spans_for_node(0, 512, generation=0)
    assert cold_idx.node_window_snapshot(0, 512, generation=0) == (
        own, own_total, True)


def test_node_window_snapshot_fails_closed_across_same_inode_and_atomic_rewrites(run):
    """Full-row rewrites rotate the revision even when ids/lengths or all bytes stay the same."""
    _rd, source, _spans = run
    before = get_index(source).node_window_snapshot(0, 512, generation=0)[0]
    raw = source.read_bytes()
    rewritten = raw.replace(b'"output":"OOOO', b'"output":"PPPP', 1)
    assert len(rewritten) == len(raw) and rewritten != raw
    with open(source, "r+b") as stream:
        stream.write(rewritten)
        stream.flush()
        os.fsync(stream.fileno())
    after_in_place = get_index(source).node_window_snapshot(0, 512, generation=0)[0]
    assert after_in_place != before

    replacement = source.with_name("spans.replacement")
    replacement.write_bytes(source.read_bytes())
    os.replace(replacement, source)
    after_replace = get_index(source).node_window_snapshot(0, 512, generation=0)[0]
    assert after_replace != after_in_place


def test_full_node_spans_filter_generation_before_the_window(tmp_path):
    """An abandoned attempt cannot consume the current conversation's bounded tail."""
    rd = tmp_path / "demo"
    rd.mkdir()
    spans = [
        {"name": "old-root", "kind": "operation", "trace_id": "old", "span_id": "or",
         "parent_id": None, "attributes": {"node_id": 0}, "start": 0.0},
        {"name": "old-tail", "kind": "generation", "trace_id": "old", "span_id": "og",
         "parent_id": "or", "attributes": {"node_id": 0}, "start": 1.0},
        {"name": "new-root", "kind": "operation", "trace_id": "new", "span_id": "nr",
         "parent_id": None, "attributes": {"node_id": 0, "generation": 1}, "start": 2.0},
        {"name": "new-tail", "kind": "generation", "trace_id": "new", "span_id": "ng",
         "parent_id": "nr", "attributes": {"node_id": 0}, "start": 3.0},
    ]
    idx = get_index(_write_spans(rd, spans))

    assert idx.node_span_count(0, generation=0) == 2
    assert idx.node_span_count(0, generation=1) == 2
    assert [row["span_id"] for row in idx.full_spans_for_node(
        0, 1, generation=0)] == ["og"]
    assert [row["span_id"] for row in idx.full_spans_for_node(
        0, 1, generation=1)] == ["ng"]


def test_shared_trace_filters_effective_node_before_total_and_window(tmp_path):
    """A newer foreign row in one shared trace cannot erase or inflate the target node."""
    rd = tmp_path / "demo"
    rd.mkdir()
    spans = [
        {"name": "shared-root", "kind": "operation", "trace_id": "shared",
         "span_id": "root", "parent_id": None,
         "attributes": {"node_id": 0, "generation": 2}, "start": 0.0},
        {"name": "node-zero", "kind": "generation", "trace_id": "shared",
         "span_id": "n0", "parent_id": "root",
         "attributes": {"node_id": 0, "output": "zero"}, "start": 1.0},
        {"name": "node-one-newer", "kind": "generation", "trace_id": "shared",
         "span_id": "n1", "parent_id": "root",
         "attributes": {"node_id": 1, "output": "one"}, "start": 2.0},
    ]
    idx = get_index(_write_spans(rd, spans))

    assert idx.node_span_count(0, generation=2) == 2
    assert idx.node_span_count(1, generation=2) == 1
    assert [row["span_id"] for row in idx.light_spans_for_node(
        0, 1, generation=2)] == ["n0"]
    assert [row["span_id"] for row in idx.full_spans_for_node(
        0, 1, generation=2)] == ["n0"]
    assert [row["span_id"] for row in idx.full_spans_for_node(
        1, 1, generation=2)] == ["n1"]

    # The no-index conversation selector must have identical count/filter/cap semantics.
    fallback, fallback_total = _bounded_node_trace_tail(
        spans, 0, 1, generation=2)
    assert fallback_total == 2
    assert [row["span_id"] for row in fallback] == ["n0"]


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
    initial = idx
    n0 = len(idx.light_spans())
    more = _spans_for(2, "tr2")
    from looplab.core.tracing import JsonlSpanExporter
    exporter = JsonlSpanExporter(sp)
    for span in more:                                          # trusted writer receipts every append
        exporter.export(span)
    idx = get_index(sp)                                          # tops up only the appended tail
    assert idx is initial
    assert len(idx.light_spans()) == n0 + len(more)
    assert len(idx.full_spans_for_node(2)) == len(more)
    # identical to a from-scratch parse of the grown file
    ref = build_trace_view(ST, load_spans(sp), light=True)
    assert _canon(ref) == _canon(build_trace_view(ST, idx.light_spans(), light=True))


def test_receipted_topup_reads_only_new_tail_and_unchanged_lookup_reads_none(
        run, monkeypatch):
    """The trusted append proof is O(new bytes), never a re-hash of the old trace prefix."""
    from looplab.core.tracing import JsonlSpanExporter

    _rd, source, _spans = run
    index = get_index(source)
    before_size = source.stat().st_size
    appended = _spans_for(9, "tail-proof")[0]
    JsonlSpanExporter(source).export(appended)
    appended_bytes = source.stat().st_size - before_size

    hashed: list[tuple[int, int]] = []
    scanned: list[tuple[int, int]] = []
    real_hash = span_index._source_region_sha256
    real_scan = span_index._scan_light_stream

    def hash_spy(stream, offset, length):
        hashed.append((offset, length))
        return real_hash(stream, offset, length)

    def scan_spy(stream, base, size, **kwargs):
        scanned.append((base, size))
        return real_scan(stream, base, size, **kwargs)

    monkeypatch.setattr(span_index, "_source_region_sha256", hash_spy)
    monkeypatch.setattr(span_index, "_scan_light_stream", scan_spy)

    refreshed = get_index(source)
    assert refreshed is index
    assert hashed == [(before_size, appended_bytes)]
    assert scanned == [(before_size, appended_bytes)]

    hashed.clear()
    scanned.clear()
    assert get_index(source) is index
    assert hashed == [] and scanned == []


def test_cold_persisted_index_validates_receipts_then_scans_only_tail(run, monkeypatch):
    from looplab.core.tracing import JsonlSpanExporter

    _rd, source, _spans = run
    persisted = get_index(source)
    before_size = source.stat().st_size
    JsonlSpanExporter(source).export(_spans_for(10, "cold-tail")[0])
    appended_bytes = source.stat().st_size - before_size
    with span_index._CACHE_LOCK:
        span_index._CACHE.clear()

    scanned: list[tuple[int, int]] = []
    real_scan = span_index._scan_light_stream

    def scan_spy(stream, base, size, **kwargs):
        scanned.append((base, size))
        return real_scan(stream, base, size, **kwargs)

    monkeypatch.setattr(span_index, "_scan_light_stream", scan_spy)
    refreshed = get_index(source)

    assert refreshed is not persisted
    assert scanned == [(before_size, appended_bytes)]
    assert refreshed.full_span("root10") is not None


def test_source_growth_without_append_receipt_rebuilds_fail_closed(run):
    _rd, source, _spans = run
    initial = get_index(source)
    appended = _spans_for(8, "unreceipted")[0]
    with open(source, "ab") as stream:
        stream.write(orjson.dumps(appended) + b"\n")

    refreshed = get_index(source)

    assert refreshed is not initial
    assert refreshed.full_span(appended["span_id"]) is not None


def test_torn_append_receipt_forces_rebuild(run):
    from looplab.core.trace_append import SPAN_APPEND_JOURNAL_NAME
    from looplab.core.tracing import JsonlSpanExporter

    rd, source, _spans = run
    initial = get_index(source)
    appended = _spans_for(7, "torn-receipt")[0]
    JsonlSpanExporter(source).export(appended)
    with open(rd / SPAN_APPEND_JOURNAL_NAME, "ab") as stream:
        stream.write(b'{"schema":1,"crash":')

    refreshed = get_index(source)

    assert refreshed is not initial
    assert refreshed.full_span(appended["span_id"]) is not None


def test_forged_append_transition_forces_rebuild(run):
    from looplab.core.trace_append import (
        SPAN_APPEND_JOURNAL_NAME, SPAN_APPEND_RECEIPT_SCHEMA)

    rd, source, _spans = run
    initial = get_index(source)
    before = source.stat()
    appended = _spans_for(6, "forged-receipt")[0]
    encoded = orjson.dumps(appended) + b"\n"
    with open(source, "ab") as stream:
        stream.write(encoded)
    after = source.stat()
    forged = {
        "schema": SPAN_APPEND_RECEIPT_SCHEMA,
        "dev": after.st_dev,
        "ino": after.st_ino,
        "before_size": before.st_size,
        "before_mtime_ns": before.st_mtime_ns,
        "before_ctime_ns": before.st_ctime_ns,
        "after_size": after.st_size,
        "after_mtime_ns": after.st_mtime_ns,
        "after_ctime_ns": after.st_ctime_ns,
        "append_sha256": "0" * 64,                 # valid shape, false byte-range proof
    }
    (rd / SPAN_APPEND_JOURNAL_NAME).write_bytes(orjson.dumps(forged) + b"\n")

    refreshed = get_index(source)

    assert refreshed is not initial
    assert refreshed.full_span(appended["span_id"]) is not None


def test_missing_append_journal_after_checkpoint_forces_rebuild(tmp_path):
    from looplab.core.trace_append import SPAN_APPEND_JOURNAL_NAME
    from looplab.core.tracing import JsonlSpanExporter

    source = tmp_path / "spans.jsonl"
    exporter = JsonlSpanExporter(source)
    exporter.export(_spans_for(4, "journal-life")[0])
    initial = get_index(source)
    exporter.export(_spans_for(5, "journal-life-next")[0])
    (tmp_path / SPAN_APPEND_JOURNAL_NAME).unlink()

    refreshed = get_index(source)

    assert refreshed is not initial


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
    from looplab.core.tracing import JsonlSpanExporter
    exporter = JsonlSpanExporter(sp)
    for k in range(40):                                 # 40 small appends → ~18x growth
        for span in _spans_for(1000 + k, f"tg{k}"):
            exporter.export(span)
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


def test_persisted_last_row_digest_mismatch_falls_back_to_source_rebuild(run):
    """A damaged derived digest is rejected by the cold spotcheck, not exposed as read failure."""
    rd, source, _spans = run
    get_index(source)
    with span_index._CACHE_LOCK:
        span_index._CACHE.clear()
    index_path = rd / "spans.index.jsonl"
    lines = [line for line in index_path.read_bytes().splitlines() if line]
    last = orjson.loads(lines[-1])
    last["_h"] = "0" * 64
    lines[-1] = orjson.dumps(last)
    index_path.write_bytes(b"\n".join(lines) + b"\n")

    rebuilt = get_index(source)

    assert rebuilt.row_digests[-1] != "0" * 64
    assert rebuilt.full_span(rebuilt.light[-1]["span_id"]) is not None


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
@pytest.mark.parametrize("cold_reload", [False, True], ids=["warm-cache", "persisted-cold"])
def test_same_inode_non_growth_rewrite_rebuilds_when_coverage_lags(
        tmp_path, cold_reload):
    """covers<size (corrupt suffix) cannot hide a same-size rewrite of an indexed prefix.

    The unchanged final indexed row makes the persisted index's O(1) tail spotcheck pass, so both
    cases specifically require the observed source size/mtime fence: the warm case must discard its
    object and the cold case must reject the otherwise-plausible persisted header.
    """
    rd = tmp_path / "demo"
    rd.mkdir()
    source = rd / "spans.jsonl"

    def row(span_id, start):
        return {"name": "op", "kind": "operation", "trace_id": "trace",
                "span_id": span_id, "parent_id": None, "run_id": "demo",
                "attributes": {"node_id": 0}, "events": [], "status": "OK",
                "start": start, "duration_s": 1.0}

    old_first = orjson.dumps(row("old-a", 0.0)) + b"\n"
    new_first = orjson.dumps(row("new-a", 0.0)) + b"\n"
    unchanged_last = orjson.dumps(row("last-z", 1.0)) + b"\n"
    corrupt_suffix = b'{"kind": definitely-not-json}\n'
    assert len(old_first) == len(new_first)
    source.write_bytes(old_first + unchanged_last + corrupt_suffix)
    invalidate(source)
    (rd / "spans.index.jsonl").unlink(missing_ok=True)

    initial = get_index(source)
    before = source.stat()
    assert initial.covers == len(old_first) + len(unchanged_last) < before.st_size
    assert initial.source_size == before.st_size
    assert initial.full_span("old-a") is not None

    replacement = new_first + unchanged_last + corrupt_suffix
    assert len(replacement) == before.st_size
    with open(source, "r+b") as stream:
        stream.write(replacement)
        stream.flush()
        os.fsync(stream.fileno())
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    assert source.stat().st_ino == before.st_ino
    if cold_reload:
        with span_index._CACHE_LOCK:
            span_index._CACHE.clear()

    refreshed = get_index(source)

    assert refreshed is not initial
    assert refreshed.full_span("old-a") is None
    assert refreshed.full_span("new-a") is not None
    assert [span["span_id"] for span in refreshed.light_spans()] == ["new-a", "last-z"]


def _await_inode_clock_past(reference_ns: int, directory: Path, timeout_s: float = 5.0) -> None:
    """Block until THIS filesystem would stamp an inode change strictly later than ``reference_ns``.

    Linux stamps inode times from the COARSE clock (`ktime_get_coarse_real_ts64`), which only advances
    on the timer tick: measured on this box's overlayfs, `st_ctime_ns` moves in steps of exactly
    3,999,897 ns (CONFIG_HZ=250) while the rewrite below — write, fsync, utime, stat — completes in a
    median 0.68 ms. So both stats routinely land inside ONE tick, the kernel records the change it
    really did make with the SAME ctime value it already had, and a strict `!=` fails for a reason
    that has nothing to do with `span_index`. Driven at HEAD, the assertion below failed 3 of 5 runs
    of this test ALONE and the observed delta was only ever 0 or exactly one tick, never anything
    between — which is what identifies the clock rather than a lost update. (The "it fails only after
    certain other test files" reading is the same race seen through timing noise: 11c29fb0, the
    commit it was reported to pass at, fails here too.)

    A PROBE file in the same directory is the measurement: it shares the source's filesystem and
    therefore its clock, so once the probe carries a ctime past ``reference_ns`` any subsequent write
    to the source must too. That keeps the assertion strict — the alternative, relaxing it to `>=`,
    would retire the guard while leaving it looking present. A filesystem that never advances
    `st_ctime_ns` at all (the geesefs/S3 mount on this box is the near case) cannot witness a
    same-size rewrite with a restored mtime by ANY spelling of this test, so it skips — detected by
    driving the clock, never by mount name.
    """
    probe = directory / ".ctime-clock-probe"
    probe.write_bytes(b"")
    deadline = time.monotonic() + timeout_s
    try:
        while probe.stat().st_ctime_ns <= reference_ns:
            if time.monotonic() > deadline:
                pytest.skip("this filesystem does not advance st_ctime_ns; the ctime fence "
                            f"`span_index` relies on is unobservable here (stuck at {reference_ns})")
            os.utime(probe)
    finally:
        probe.unlink(missing_ok=True)


@pytest.mark.skipif(os.name == "nt", reason="Windows uses descriptor ChangeTime, tested below")
def test_posix_cold_same_size_rewrite_with_restored_mtime_is_caught_by_ctime(tmp_path):
    source = tmp_path / "spans.jsonl"

    def row(span_id, start):
        return {"name": "op", "kind": "operation", "trace_id": "trace",
                "span_id": span_id, "parent_id": None, "attributes": {"node_id": 0},
                "start": start}

    old = [row("head-aa", 0.0), row("old-mid", 1.0), row("tail-zz", 2.0)]
    source.write_bytes(b"".join(orjson.dumps(item) + b"\n" for item in old))
    invalidate(source)
    initial = get_index(source)
    before = source.stat()

    new = [old[0], row("new-mid", 1.0), old[2]]
    replacement = b"".join(orjson.dumps(item) + b"\n" for item in new)
    assert len(replacement) == before.st_size
    # The rewrite has to be able to LAND on a later ctime than the one just observed, or the strict
    # assertion below is testing the timer tick and not the fence. See `_await_inode_clock_past`.
    _await_inode_clock_past(before.st_ctime_ns, tmp_path)
    with open(source, "r+b") as stream:
        stream.write(replacement)
        stream.flush()
        os.fsync(stream.fileno())
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = source.stat()
    assert after.st_ino == before.st_ino and after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ctime_ns != before.st_ctime_ns
    with span_index._CACHE_LOCK:
        span_index._CACHE.clear()

    refreshed = get_index(source)

    assert refreshed is not initial
    assert refreshed.full_span("old-mid") is None
    assert refreshed.full_span("new-mid") is not None


def test_windows_change_time_is_descriptor_bound_and_controls_warm_reuse(run, monkeypatch):
    """The mocked WinAPI token, not Windows creation time, is the mutation authority."""
    _rd, source, _spans = run
    state = {"token": 101}
    descriptors = []

    def change_time(fd):
        descriptors.append(fd)
        return state["token"]

    monkeypatch.setattr(span_index, "_IS_WINDOWS", True)
    monkeypatch.setattr(span_index, "_windows_change_time", change_time)
    first = get_index(source)
    epoch = first.source_epoch

    assert first.source_change_token == 101 and epoch is not None
    assert get_index(source) is first
    state["token"] = 102
    changed = get_index(source)

    assert changed is not first
    assert changed.source_change_token == 102
    assert changed.source_epoch is not None and changed.source_epoch != epoch
    assert descriptors and all(isinstance(fd, int) for fd in descriptors)


def test_unavailable_windows_change_time_disables_warm_and_persisted_reuse(
        run, monkeypatch):
    """No proven ChangeTime means rebuild-per-observation and deliberately unstable validators."""
    rd, source, _spans = run
    state = {"token": 201}
    monkeypatch.setattr(span_index, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        span_index, "_windows_change_time", lambda _fd: state["token"])
    trusted = get_index(source)
    persisted = rd / "spans.index.jsonl"
    assert persisted.exists() and trusted.source_epoch is not None

    with span_index._CACHE_LOCK:
        span_index._CACHE.clear()
    state["token"] = None

    def persisted_reuse_forbidden(*_args, **_kwargs):
        raise AssertionError("an unproven source must not load a persisted index")

    monkeypatch.setattr(span_index, "_load_persisted", persisted_reuse_forbidden)
    first = get_index(source)
    first_revision = first.node_window_snapshot(0, 512, generation=0)[0]
    second = get_index(source)
    second_revision = second.node_window_snapshot(0, 512, generation=0)[0]

    assert first is not trusted and second is not first
    assert first.source_epoch is None and second.source_epoch is None
    assert first.source_change_token is None and second.source_change_token is None
    assert second_revision != first_revision


@pytest.mark.parametrize("cold_reload", [False, True], ids=["warm-cache", "persisted-cold"])
def test_windows_growth_rebuilds_before_trusting_creation_time_receipts(
        run, monkeypatch, cold_reload):
    """Prefix rewrite + legitimate append cannot stitch through Windows creation-time receipts."""
    from looplab.core.tracing import JsonlSpanExporter

    _rd, source, _spans = run
    state = {"token": 301}
    monkeypatch.setattr(span_index, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        span_index, "_windows_change_time", lambda _fd: state["token"])
    initial = get_index(source)
    original_revision = initial.node_window_snapshot(0, 512, generation=0)[0]
    row = initial.by_sid["g0_1"]
    off, length = initial.meta[row]
    before = source.stat()
    with open(source, "r+b") as stream:
        stream.seek(off)
        original = stream.read(length)
        rewritten = original.replace(b'"output":"OOOO', b'"output":"PPPP', 1)
        assert len(rewritten) == len(original) and rewritten != original
        stream.seek(off)
        stream.write(rewritten)
        stream.flush()
        os.fsync(stream.fileno())
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    rewritten_stat = source.stat()
    assert (rewritten_stat.st_ino, rewritten_stat.st_size, rewritten_stat.st_mtime_ns) == (
        before.st_ino, before.st_size, before.st_mtime_ns)

    # This is a real exporter append with its normal durable receipt. On Windows that receipt's ctime
    # fields are creation time, so ctime alone cannot prove the old prefix above was unchanged — the
    # schema-2 mutation token is what witnesses it, and the in-place rewrite already moved that token
    # away from the one this index recorded. The chain therefore cannot close over the rewrite.
    JsonlSpanExporter(source).export(_gen(1, "tr1", "foreign-after-rewrite", "root1", 30))
    state["token"] = 302
    if cold_reload:
        with span_index._CACHE_LOCK:
            span_index._CACHE.clear()

    # The validator is consulted now rather than skipped, so pin the OUTCOME it must produce: a
    # refusal. Asserting "never called" would pin the mechanism, and would go green again for the
    # wrong reason the moment the platform gate moved somewhere else.
    verdicts: list[object] = []
    real_transition = span_index._validated_append_transition

    def spy(*args, **kwargs):
        verdict = real_transition(*args, **kwargs)
        verdicts.append(verdict)
        return verdict

    monkeypatch.setattr(span_index, "_validated_append_transition", spy)
    refreshed = get_index(source)

    assert verdicts and all(v is None for v in verdicts), (
        "a broken change-token chain must refuse the receipt top-up, not stitch through it")
    assert refreshed is not initial
    assert refreshed.node_window_snapshot(0, 512, generation=0)[0] != original_revision
    assert (refreshed.full_span("g0_1") or {})["attributes"]["output"].startswith("PPPP")
    assert refreshed.full_span("foreign-after-rewrite") is not None


@pytest.mark.parametrize("writer_has_token", [True, False],
                         ids=["schema2-token", "token-unavailable"])
def test_windows_growth_tops_up_only_with_a_proven_change_token_chain(
        run, monkeypatch, writer_has_token):
    """Windows earns the incremental path from the schema-2 token, and only from it.

    Its ``st_ctime_ns`` is a creation stamp, so before schema 2 there was no witness for an in-place
    prefix rewrite and every growth rebuilt. The receipt now carries the descriptor-bound mutation
    token on both sides; when that chain closes, the append is proven and only the appended bytes are
    read. When the writer could not record a token (schema 1, or an API failure), nothing is proven
    and the reader must still rebuild — the same fail-closed answer as before.
    """
    from looplab.core import tracing as tracing_mod
    from looplab.core.tracing import JsonlSpanExporter

    _rd, source, _spans = run
    # Monotone stand-in for FILE_BASIC_INFO.ChangeTime, read by BOTH the writer and the reader so the
    # chain is exercised end to end on any host. It advances with the file, which is the property the
    # happy path depends on; the rewrite-with-restored-metadata refusal is the sibling test above.
    def fake_token(fd, info=None):
        return 1_000_000 + os.fstat(fd).st_size

    monkeypatch.setattr(span_index, "_IS_WINDOWS", True)
    monkeypatch.setattr(span_index, "_windows_change_time", lambda fd: fake_token(fd))
    monkeypatch.setattr(
        tracing_mod, "trace_file_change_token",
        (lambda fd, info=None: fake_token(fd)) if writer_has_token
        else (lambda fd, info=None: None))

    index = get_index(source)
    before_size = source.stat().st_size
    JsonlSpanExporter(source).export(
        _gen(1, "tr1", "windows-token-proof", "root1", 30))
    appended_bytes = source.stat().st_size - before_size

    scanned: list[tuple[int, int]] = []
    real_scan = span_index._scan_light_stream
    monkeypatch.setattr(span_index, "_scan_light_stream",
                        lambda stream, base, size, **kw: (scanned.append((base, size))
                                                          or real_scan(stream, base, size, **kw)))
    refreshed = get_index(source)

    assert refreshed.full_span("windows-token-proof") is not None      # the span is served either way
    if writer_has_token:
        assert refreshed is index, "a closed token chain must reuse the index, not rebuild it"
        assert scanned == [(before_size, appended_bytes)], (
            "a proven append must read only the appended bytes")
    else:
        assert refreshed is not index, "no token means nothing is proven; Windows must rebuild"
        assert scanned == [(0, source.stat().st_size)], "a rebuild re-reads the whole source"


@pytest.mark.skipif(os.name != "nt", reason="FILE_BASIC_INFO.ChangeTime is a Windows-only authority")
def test_the_real_windows_change_time_sees_a_rewrite_that_size_mtime_and_ctime_cannot(tmp_path):
    """The one Windows guard here that stubs NOTHING.

    Every other Windows assertion in this file monkeypatches `_windows_change_time`, so they prove the
    reader's logic given a token and say nothing about whether the platform actually supplies one that
    moves. The whole schema-2 receipt rests on that: if ChangeTime did not witness an in-place rewrite,
    the incremental path would be trusting a constant. Drive it against the real filesystem instead —
    rewrite in place at the SAME size and put mtime back, which is precisely the shape that size/mtime
    cannot see and that Windows `st_ctime_ns` cannot see either, because there it is creation time.
    """
    from looplab.core.trace_files import trace_file_change_token

    source = tmp_path / "spans.jsonl"
    source.write_bytes(b'{"span_id":"a"}\n')
    before = source.stat()
    with open(source, "rb") as stream:
        token_before = trace_file_change_token(stream.fileno(), os.fstat(stream.fileno()))

    with open(source, "r+b") as stream:
        stream.seek(0)
        stream.write(b'{"span_id":"b"}\n')      # same length, different bytes
        stream.flush()
        os.fsync(stream.fileno())
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))

    after = source.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns), (
        "the rewrite must be invisible to size and mtime, or this proves nothing")
    assert after.st_ctime_ns == before.st_ctime_ns, (
        "Windows st_ctime_ns is creation time — blind to this rewrite. That blindness is the reason "
        "the receipt needs a separate mutation token at all")

    with open(source, "rb") as stream:
        token_after = trace_file_change_token(stream.fileno(), os.fstat(stream.fileno()))
    assert token_before is not None and token_after is not None, (
        "no token available on this filesystem: the incremental path must then fail closed, which is "
        "what `require_change_token` does — but this host cannot exercise the proven path at all")
    assert token_after != token_before, "ChangeTime must witness the rewrite the other stamps missed"


@pytest.mark.parametrize("cold_reload", [False, True], ids=["warm-cache", "persisted-cold"])
def test_same_inode_prefix_rewrite_plus_growth_rebuilds_instead_of_stitching(
        tmp_path, cold_reload):
    """A larger same-inode file is not necessarily append-only: validate its old prefix first.

    The old index's final row and byte offsets deliberately remain valid, so inode/size/mtime and
    the historical last-row spotcheck all accept this shape. Without a prefix digest a top-up would
    return the removed ``old-a`` light row plus the newly appended row — a projection assembled from
    two different source generations. The complete accepted-prefix digest prevents that stitch.
    """
    rd = tmp_path / "demo"
    rd.mkdir()
    source = rd / "spans.jsonl"

    def row(span_id, start):
        return {"name": "op", "kind": "operation", "trace_id": "trace",
                "span_id": span_id, "parent_id": None, "run_id": "demo",
                "attributes": {"node_id": 0}, "events": [], "status": "OK",
                "start": start, "duration_s": 1.0}

    old_first = orjson.dumps(row("old-a", 0.0)) + b"\n"
    new_first = orjson.dumps(row("new-a", 0.0)) + b"\n"
    unchanged_last = orjson.dumps(row("last-z", 1.0)) + b"\n"
    appended = orjson.dumps(row("tail-n", 2.0)) + b"\n"
    assert len(old_first) == len(new_first)
    source.write_bytes(old_first + unchanged_last)
    invalidate(source)
    (rd / "spans.index.jsonl").unlink(missing_ok=True)

    initial = get_index(source)
    before = source.stat()
    persisted_header = orjson.loads((rd / "spans.index.jsonl").read_bytes().splitlines()[0])
    assert "append_journal_covers" in persisted_header
    assert initial.full_span("old-a") is not None

    replacement = new_first + unchanged_last + appended
    with open(source, "r+b") as stream:
        stream.write(replacement)
        stream.truncate(len(replacement))
        stream.flush()
        os.fsync(stream.fileno())
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    after = source.stat()
    assert after.st_ino == before.st_ino and after.st_size > before.st_size
    if cold_reload:
        with span_index._CACHE_LOCK:
            span_index._CACHE.clear()

    refreshed = get_index(source)

    assert refreshed is not initial
    assert refreshed.full_span("old-a") is None
    assert refreshed.full_span("new-a") is not None
    assert [span["span_id"] for span in refreshed.light_spans()] == [
        "new-a", "last-z", "tail-n"]


def test_same_inode_gap_rewrite_plus_growth_cannot_evade_prefix_digest(tmp_path):
    """A rewrite between bounded sample windows must still invalidate the complete prefix.

    The changed span id is around byte 200 KiB of a >1 MiB accepted prefix. A head/middle/tail
    sampling scheme misses that deterministic gap; hashing every accepted prefix byte cannot.
    """
    rd = tmp_path / "demo"
    rd.mkdir()
    source = rd / "spans.jsonl"

    def row(span_id, start):
        return {"name": "op", "kind": "operation", "trace_id": "trace",
                "span_id": span_id, "parent_id": None, "run_id": "demo",
                "attributes": {"node_id": 0, "input": "x" * (96 * 1024)},
                "events": [], "status": "OK", "start": start, "duration_s": 1.0}

    old_rows = [row(f"row-{index:02d}", float(index)) for index in range(12)]
    old_rows[2] = row("old-gap", 2.0)
    old_blob = b"".join(orjson.dumps(item) + b"\n" for item in old_rows)
    target_offset = old_blob.index(b'"old-gap"')
    assert len(old_blob) > 1024 * 1024
    assert 128 * 1024 < target_offset < 400 * 1024
    source.write_bytes(old_blob)
    invalidate(source)
    (rd / "spans.index.jsonl").unlink(missing_ok=True)

    initial = get_index(source)
    before = source.stat()
    assert initial.full_span("old-gap") is not None

    new_rows = list(old_rows)
    new_rows[2] = row("new-gap", 2.0)
    appended = row("tail-new", 12.0)
    replacement = b"".join(orjson.dumps(item) + b"\n" for item in [*new_rows, appended])
    with open(source, "r+b") as stream:
        stream.write(replacement)
        stream.truncate(len(replacement))
        stream.flush()
        os.fsync(stream.fileno())
    after = source.stat()
    assert after.st_ino == before.st_ino and after.st_size > before.st_size

    refreshed = get_index(source)

    assert refreshed is not initial
    assert refreshed.full_span("old-gap") is None
    assert refreshed.full_span("new-gap") is not None
    assert refreshed.full_span("tail-new") is not None


def test_symlink_trace_source_is_rejected_by_warm_cold_and_offset_reads(tmp_path):
    """A run-local filename is not permission to follow another run's private trace sidecar."""
    local_run = tmp_path / "local"
    foreign_run = tmp_path / "foreign"
    local_run.mkdir()
    foreign_run.mkdir()

    def row(output):
        return {"name": "generation", "kind": "generation", "trace_id": "trace",
                "span_id": "shared", "parent_id": None, "run_id": "run",
                "attributes": {"node_id": 0, "output": output}, "events": [],
                "status": "OK", "start": 0.0, "duration_s": 1.0}

    source = local_run / "spans.jsonl"
    foreign = foreign_run / "spans.jsonl"
    source.write_bytes(orjson.dumps(row("local evidence")) + b"\n")
    foreign.write_bytes(orjson.dumps(row("FOREIGN PRIVATE EVIDENCE")) + b"\n")
    invalidate(source)
    index = get_index(source)                       # warm cache + persisted offset index
    assert index.full_span("shared")["attributes"]["output"] == "local evidence"

    source.unlink()
    try:
        source.symlink_to(foreign)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")

    # A cached get must inspect the path before reusing light rows; an already-returned index must
    # also no-follow when it later seeks full-span offsets.
    with pytest.raises(OSError):
        get_index(source)
    with pytest.raises(OSError):
        index.full_span("shared")

    # Clearing process memory exercises the persisted cold path. It must reject the source before
    # trusting its otherwise-valid index header/offsets and must never project the foreign output.
    with span_index._CACHE_LOCK:
        span_index._CACHE.clear()
    with pytest.raises(OSError):
        get_index(source)


def test_non_regular_trace_source_is_rejected_before_open(tmp_path):
    source = tmp_path / "spans.jsonl"
    source.mkdir()

    with pytest.raises(IsADirectoryError, match="directory"):
        get_index(source)


def test_hard_link_trace_source_is_rejected_without_reading_victim(tmp_path):
    victim = tmp_path / "victim-private.jsonl"
    victim.write_bytes(orjson.dumps({
        "name": "private", "kind": "generation", "trace_id": "foreign",
        "span_id": "secret", "parent_id": None,
        "attributes": {"node_id": 9, "output": "PRIVATE"}, "start": 0.0,
    }) + b"\n")
    source = tmp_path / "spans.jsonl"
    try:
        os.link(victim, source)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable on this filesystem: {exc}")

    with pytest.raises(OSError, match="hard-link"):
        get_index(source)


def test_unreceipted_torn_tail_completion_rebuilds_fail_closed(tmp_path):
    """An out-of-band append has no writer receipt, so correctness wins over object reuse."""
    rd = tmp_path / "demo"
    rd.mkdir()
    source = rd / "spans.jsonl"
    first = orjson.dumps({
        "name": "one", "kind": "operation", "trace_id": "t", "span_id": "one",
        "parent_id": None, "attributes": {"node_id": 0}, "start": 0.0,
    }) + b"\n"
    second = orjson.dumps({
        "name": "two", "kind": "operation", "trace_id": "t", "span_id": "two",
        "parent_id": None, "attributes": {"node_id": 0}, "start": 1.0,
    }) + b"\n"
    cut = len(second) - 9
    source.write_bytes(first + second[:cut])
    invalidate(source)

    initial = get_index(source)
    assert initial.covers == len(first) < initial.source_size
    with open(source, "ab") as stream:
        stream.write(second[cut:])

    refreshed = get_index(source)

    assert refreshed is not initial
    assert refreshed.source_size == source.stat().st_size
    assert [span["span_id"] for span in refreshed.light_spans()] == ["one", "two"]


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


def test_cold_builds_for_unrelated_runs_overlap(tmp_path, monkeypatch):
    """A slow cold scan is serialized with reset/get for ITS run, not every run in the process."""
    import threading

    paths = []
    for name, node_id in (("a", 10), ("b", 20)):
        rd = tmp_path / name
        rd.mkdir()
        paths.append(_write_spans(rd, _spans_for(node_id, f"trace-{name}")))

    rendezvous = threading.Barrier(2)
    original = span_index._scan_light_stream

    def scan_together(*args, **kwargs):
        # With a process-global writer lock the first scan waits here while the second can never
        # enter, and the barrier breaks. Per-path locks let both independent cold builds make
        # progress at once.
        rendezvous.wait(timeout=3)
        return original(*args, **kwargs)

    monkeypatch.setattr(span_index, "_scan_light_stream", scan_together)
    results = []
    errors = []

    def build(path):
        try:
            results.append(get_index(path))
        except Exception as exc:  # noqa: BLE001 - collect failures from worker threads
            errors.append(exc)

    threads = [threading.Thread(target=build, args=(path,)) for path in paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert not any(thread.is_alive() for thread in threads), "independent cold builds deadlocked"
    assert errors == []
    assert len(results) == 2 and all(result.span_count() == 7 for result in results)


def test_process_writer_guard_remains_exclusive_for_the_same_path(tmp_path, monkeypatch):
    """The per-path optimization must retain same-run get/reset/delete mutual exclusion."""
    import contextlib
    import threading
    import time

    @contextlib.contextmanager
    def no_file_lock(*_args, **_kwargs):
        # Isolate the process-local guarantee; an OS flock must not be what makes this test pass.
        yield

    monkeypatch.setattr(span_index, "_interprocess_lock", no_file_lock)
    source = tmp_path / "spans.jsonl"
    start = threading.Barrier(2)
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def guarded_writer():
        nonlocal active, max_active
        start.wait(timeout=2)
        with span_index.span_index_write_guard(source):
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.08)
            with state_lock:
                active -= 1

    threads = [threading.Thread(target=guarded_writer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)

    assert not any(thread.is_alive() for thread in threads)
    assert max_active == 1


def test_process_cache_evicts_by_represented_source_bytes(tmp_path):
    """Three huge sources must not survive merely because the entry-count cap is three."""
    with span_index._CACHE_LOCK:
        span_index._CACHE.clear()
    try:
        for name in ("a", "b", "c"):
            index = span_index.SpanIndex(tmp_path / name / "spans.jsonl")
            index.source_size = 600 * 1024 * 1024
            span_index._cache_store(str(index.path), index)
        with span_index._CACHE_LOCK:
            assert list(span_index._CACHE) == [str(tmp_path / "c" / "spans.jsonl")]
    finally:
        with span_index._CACHE_LOCK:
            span_index._CACHE.clear()


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


def test_conversation_conditional_read_is_exact_for_node_lifecycle_and_window(
        tmp_path, monkeypatch):
    """An unchanged poll is bodyless/cheap without borrowing another representation's validator."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.core.tracing import JsonlSpanExporter
    from looplab.events import traceview
    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    rd = _http_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    path = "/api/runs/demo/nodes/0/conversation"
    params = {"attempt": 0, "limit": 512}
    first = client.get(path, params=params)

    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    etag = first.headers["etag"]
    assert etag.startswith('W/"llconv1-') and etag.endswith('"')
    assert first.json()["cursor"] == etag

    builds = 0
    real_build = traceview.build_conversation

    def counted_build(*args, **kwargs):
        nonlocal builds
        builds += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(traceview, "build_conversation", counted_build)
    exporter = JsonlSpanExporter(rd / "spans.jsonl")

    # The global file stat changes, but this node's selected full rows do not: its validator and the
    # zero-build fast path remain valid while a parallel node appends.
    exporter.export(_gen(1, "tr1", "foreign-late", "root1", 20))
    unchanged = client.get(path, params=params, headers={"If-None-Match": etag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == etag
    assert unchanged.headers["cache-control"] == "no-store"
    assert builds == 0

    # GET uses weak comparison, including a comma-list carrying a strong spelling of the same tag.
    listed = client.get(path, params=params, headers={
        "If-None-Match": f'"not-this-one", {etag[2:]}'})
    assert listed.status_code == 304 and listed.content == b""
    assert builds == 0

    # Invalid list syntax is a miss, never permission to send a bodyless response.
    malformed = client.get(path, params=params, headers={
        "If-None-Match": f"{etag} trailing-garbage"})
    assert malformed.status_code == 200
    assert malformed.headers["etag"] == etag
    assert malformed.json()["cursor"] == etag
    assert builds == 1

    # A selected-row append and a different settled span window are different representations.
    exporter.export(_gen(0, "tr0", "own-late", "root0", 21))
    changed = client.get(path, params=params, headers={"If-None-Match": etag})
    assert changed.status_code == 200
    changed_etag = changed.headers["etag"]
    assert changed_etag != etag and changed.json()["cursor"] == changed_etag
    assert builds == 2

    wider = client.get(path, params={"attempt": 0, "limit": 1024},
                       headers={"If-None-Match": changed_etag})
    assert wider.status_code == 200
    assert wider.headers["etag"] != changed_etag
    assert wider.json()["cursor"] == wider.headers["etag"]
    assert builds == 3

    # A reset opens attempt 1 and does NOT invalidate attempt 0's read. The refusal that used to
    # stand here was a consequence of treating any explicit non-current attempt as an error; now an
    # earlier attempt is a historical read, so "your copy of attempt 0 is still current" is simply
    # true — attempt 0's spans are append-only and nothing rewrote them.
    #
    # The property that actually needs holding is that the two lifecycles have DISTINCT validators,
    # so a historical label can never be satisfied by the current attempt's bytes. That is asserted
    # directly: the same `If-None-Match` that 304s for attempt 0 must force a rebuild for attempt 1
    # and come back with a different ETag.
    EventStore(rd / "events.jsonl").append(
        "node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    historical = client.get(path, params=params, headers={"If-None-Match": changed_etag})
    assert historical.status_code == 304 and historical.content == b""
    assert builds == 3, "an unchanged historical attempt must not rebuild"

    current_attempt = client.get(path, params={"attempt": 1, "limit": 512},
                                 headers={"If-None-Match": changed_etag})
    assert current_attempt.status_code == 200, "attempt 0's validator may not answer for attempt 1"
    assert current_attempt.headers["etag"] != changed_etag
    assert current_attempt.json()["attempt"] == 1
    assert builds == 4

    # And an attempt this node has never reached is still refused outright.
    unreachable = client.get(path, params={"attempt": 9, "limit": 512})
    assert unreachable.status_code == 409
    assert unreachable.json()["detail"]["code"] == "node_attempt_changed"


def test_conversation_cold_persisted_window_requires_source_verification_before_304(
        tmp_path, monkeypatch):
    """A crafted cold accelerator cannot replay an old ETag without an exact source-row read."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.core.trace_files import open_private_trace_file
    from looplab.serve.server import make_app

    rd = _http_run(tmp_path)
    source = rd / "spans.jsonl"
    index_path = rd / "spans.index.jsonl"
    path = "/api/runs/demo/nodes/0/conversation"
    params = {"attempt": 0, "limit": 512}
    client = TestClient(make_app(tmp_path))
    seed = client.get(path, params=params)
    old_etag = seed.headers["etag"]
    assert seed.status_code == 200 and index_path.exists()

    cold_loads = []
    native_load_persisted = span_index._load_persisted

    def observed_load(*args, **kwargs):
        loaded = native_load_persisted(*args, **kwargs)
        cold_loads.append(loaded)
        return loaded

    monkeypatch.setattr(span_index, "_load_persisted", observed_load)
    with span_index._CACHE_LOCK:
        span_index._CACHE.clear()

    # The candidate bytes are unchanged, but a fresh process has only untrusted persisted hashes.
    # It must send one verifying 200; that read promotes the exact window, so the next poll is cheap.
    first_cold = client.get(
        path, params=params, headers={"If-None-Match": old_etag})
    assert first_cold.status_code == 200 and first_cold.content
    assert first_cold.headers["etag"] == old_etag
    assert first_cold.json()["cursor"] == old_etag
    assert cold_loads and cold_loads[-1] is not None
    assert cold_loads[-1]._source_derived is False

    verified = client.get(path, params=params, headers={"If-None-Match": old_etag})
    assert verified.status_code == 304 and verified.content == b""

    # Rewrite one selected non-tail row in place, but craft the untrusted persisted header to match
    # the new descriptor metadata while retaining its old epoch/digests. The unchanged final row
    # keeps the loader's O(1) spotcheck green — this is the exact historical false-304 shape.
    current = get_index(source)
    row = current.by_sid["g0_1"]
    off, length = current.meta[row]
    before = source.stat()
    with open(source, "r+b") as stream:
        stream.seek(off)
        raw = stream.read(length)
        rewritten = raw.replace(b'"output":"OOOO', b'"output":"PPPP', 1)
        assert len(rewritten) == len(raw) and rewritten != raw
        stream.seek(off)
        stream.write(rewritten)
        stream.flush()
        os.fsync(stream.fileno())
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    with open_private_trace_file(source) as stream:
        source_stat = os.fstat(stream.fileno())
        change_token = span_index._source_change_token(stream, source_stat)

    lines = [line for line in index_path.read_bytes().splitlines() if line]
    header = orjson.loads(lines[0])
    retained_epoch = header["source_epoch"]
    header.update({
        "source_size": source_stat.st_size,
        "mtime_ns": source_stat.st_mtime_ns,
        "ctime_ns": source_stat.st_ctime_ns,
        "source_change_token": change_token,
    })
    assert header["source_epoch"] == retained_epoch
    lines[0] = orjson.dumps(header)
    index_path.write_bytes(b"\n".join(lines) + b"\n")
    with span_index._CACHE_LOCK:
        span_index._CACHE.clear()
    cold_loads.clear()

    tampered = client.get(path, params=params, headers={"If-None-Match": old_etag})

    # The epoch/header binding rejects the crafted cold accelerator before its unchanged-tail
    # spotcheck can make it authoritative. Rebuilding from source yields the changed representation.
    assert cold_loads and cold_loads[-1] is None
    assert tampered.status_code == 200 and tampered.content
    assert (tampered.json().get("projection") or {}).get("unavailable") is not True
    changed_etag = tampered.headers["etag"]
    assert changed_etag != old_etag and tampered.json()["cursor"] == changed_etag
    assert "PPPP" in tampered.text

    # Membership is the harder adversarial shape: all OLD selected rows remain byte-exact, while a
    # formerly foreign non-tail row becomes selected. Selected-only verification cannot discover it;
    # the descriptor-bound cold epoch must force a source rebuild before the old ETag is considered.
    rebuilt = get_index(source)
    old_total = rebuilt.node_span_count(0, generation=0)
    foreign_row = rebuilt.by_sid["g1_0"]
    foreign_off, foreign_length = rebuilt.meta[foreign_row]
    before = source.stat()
    with open(source, "r+b") as stream:
        stream.seek(foreign_off)
        raw = stream.read(foreign_length)
        rewritten = raw.replace(b'"node_id":1', b'"node_id":0', 1)
        assert len(rewritten) == len(raw) and rewritten != raw
        stream.seek(foreign_off)
        stream.write(rewritten)
        stream.flush()
        os.fsync(stream.fileno())
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    with open_private_trace_file(source) as stream:
        source_stat = os.fstat(stream.fileno())
        change_token = span_index._source_change_token(stream, source_stat)
    lines = [line for line in index_path.read_bytes().splitlines() if line]
    header = orjson.loads(lines[0])
    header.update({
        "source_size": source_stat.st_size,
        "mtime_ns": source_stat.st_mtime_ns,
        "ctime_ns": source_stat.st_ctime_ns,
        "source_change_token": change_token,
    })
    lines[0] = orjson.dumps(header)
    index_path.write_bytes(b"\n".join(lines) + b"\n")
    with span_index._CACHE_LOCK:
        span_index._CACHE.clear()
    cold_loads.clear()

    membership_changed = client.get(
        path, params=params, headers={"If-None-Match": changed_etag})

    assert cold_loads and cold_loads[-1] is None
    assert membership_changed.status_code == 200 and membership_changed.content
    assert membership_changed.headers["etag"] != changed_etag
    assert get_index(source).node_span_count(0, generation=0) == old_total + 1


def test_conversation_source_change_during_projection_never_labels_a_raced_body(
        tmp_path, monkeypatch):
    """The post-build source CAS must not attach a new/current ETag to an older assembled body."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.core.tracing import JsonlSpanExporter
    from looplab.events import traceview
    from looplab.serve.server import make_app

    rd = _http_run(tmp_path)
    exporter = JsonlSpanExporter(rd / "spans.jsonl")
    real_build = traceview.build_conversation
    appended = False

    def append_after_build(*args, **kwargs):
        nonlocal appended
        body = real_build(*args, **kwargs)
        if not appended:
            appended = True
            exporter.export(_gen(0, "tr0", "raced-own-row", "root0", 23))
        return body

    monkeypatch.setattr(traceview, "build_conversation", append_after_build)
    response = TestClient(make_app(tmp_path)).get(
        "/api/runs/demo/nodes/0/conversation", params={"attempt": 0, "limit": 512})

    assert appended is True and response.status_code == 200
    assert "etag" not in response.headers
    assert response.json()["cursor"] is None


def test_conversation_windows_change_time_aba_between_snapshots_never_returns_304(
        tmp_path, monkeypatch):
    """Same-inode/same-size/restored-mtime mutation after the first match invalidates the 304."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app

    rd = _http_run(tmp_path)
    source = rd / "spans.jsonl"
    state = {"token": 401}
    monkeypatch.setattr(span_index, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        span_index, "_windows_change_time", lambda _fd: state["token"])
    client = TestClient(make_app(tmp_path))
    path = "/api/runs/demo/nodes/0/conversation"
    params = {"attempt": 0, "limit": 512}
    first = client.get(path, params=params)
    old_etag = first.headers["etag"]
    real_snapshot = span_index.SpanIndex.node_window_snapshot
    observed = {}

    def mutate_after_first_match(index, *args, **kwargs):
        snapshot = real_snapshot(index, *args, **kwargs)
        if not observed:
            before = source.stat()
            raw = source.read_bytes()
            rewritten = raw.replace(b'"output":"OOOO', b'"output":"PPPP', 1)
            assert len(rewritten) == len(raw) and rewritten != raw
            with open(source, "r+b") as stream:
                stream.write(rewritten)
                stream.flush()
                os.fsync(stream.fileno())
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
            after = source.stat()
            observed.update(before=before, after=after)
            state["token"] = 402
        return snapshot

    monkeypatch.setattr(
        span_index.SpanIndex, "node_window_snapshot", mutate_after_first_match)
    raced = client.get(path, params=params, headers={"If-None-Match": old_etag})

    before, after = observed["before"], observed["after"]
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino, before.st_size, before.st_mtime_ns)
    assert raced.status_code == 200 and raced.content
    assert raced.headers["etag"] != old_etag
    assert raced.json()["cursor"] == raced.headers["etag"]
    assert "PPPP" in raced.text


def test_node_trace_endpoint_rejects_abandoned_attempt_and_reads_current_exactly(
        tmp_path, monkeypatch):
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
        {"name": "generation", "kind": "generation", "trace_id": "old", "span_id": "old-gen",
         "parent_id": "old-root", "run_id": "demo",
         "attributes": {"node_id": 0, "phase": "implement", "phase_span": "old-root",
                        "output": "old conversation"},
         "events": [], "status": "OK", "start": 0.5, "duration_s": 0.1},
        {"name": "current-attempt", "kind": "operation", "trace_id": "current",
         "span_id": "current-root", "parent_id": None, "run_id": "demo",
         "attributes": {"node_id": 0, "generation": 1},
         "events": [], "status": "OK", "start": 2.0, "duration_s": 1.0},
        {"name": "generation", "kind": "generation", "trace_id": "current",
         "span_id": "current-gen", "parent_id": "current-root", "run_id": "demo",
         "attributes": {"node_id": 0, "phase": "implement", "phase_span": "current-root",
                        "output": "current conversation"},
         "events": [], "status": "OK", "start": 2.5, "duration_s": 0.1},
    ])
    client = TestClient(make_app(tmp_path))

    # An EARLIER attempt is a HISTORICAL READ, served under its own label. It used to 409, which
    # made a repaired node's earlier traces — the ones that actually crashed — unopenable, including
    # from the attempt picker and from every historical Dock event row. Each generation renders
    # ALONE: the index fences a trace by its root's generation, so the two never concatenate.
    old = client.get("/api/runs/demo/nodes/0/trace", params={"attempt": 0}).json()
    current = client.get("/api/runs/demo/nodes/0/trace", params={"attempt": 1}).json()
    never_ran = client.get("/api/runs/demo/nodes/0/trace", params={"attempt": 2})
    assert old["node_id"] == 0 and old["attempt"] == 0, "the served attempt is echoed, not settled"
    assert "old-attempt" in json.dumps(old) and "current-attempt" not in json.dumps(old)
    # An attempt the node has NEVER reached is still refused: no such lifecycle exists, and
    # answering it with the current one would publish live spans under a future label.
    assert never_ran.status_code == 409
    assert never_ran.json()["detail"] == {
        "code": "node_attempt_changed",
        "node_id": 0,
        "expected_attempt": 2,
        "current_attempt": 1,
        "message": "The node was reset before its trace was read.",
        "remediation": "Reload node state and request the current attempt.",
    }
    assert current["node_id"] == 0 and current["attempt"] == 1
    assert "current-attempt" in json.dumps(current) and "old-attempt" not in json.dumps(current)

    # The conversation twin reads the same way — these are two VIEWS of one trace surface and the
    # attempt picker switches between them over one selection.
    stale = client.get("/api/runs/demo/nodes/0/conversation", params={"attempt": 0}).json()
    assert stale["node_id"] == "0" and stale["attempt"] == 0
    assert "old conversation" in json.dumps(stale)
    assert "current conversation" not in json.dumps(stale)
    assert client.get(
        "/api/runs/demo/nodes/0/conversation", params={"attempt": 2}).status_code == 409
    conversation = client.get(
        "/api/runs/demo/nodes/0/conversation", params={"attempt": 1}).json()
    assert conversation["node_id"] == "0" and conversation["attempt"] == 1
    assert "current conversation" in json.dumps(conversation)
    assert "old conversation" not in json.dumps(conversation)

    # Force the no-index path: it must resolve the same trace root generation before applying the
    # span cap, not fall back to a whole-file conversation that mixes both attempts.
    monkeypatch.setattr(span_index, "get_index", lambda _path: None)
    fallback = client.get(
        "/api/runs/demo/nodes/0/conversation", params={"attempt": 1}).json()
    assert fallback["node_id"] == "0" and fallback["attempt"] == 1
    assert "current conversation" in json.dumps(fallback)
    assert "old conversation" not in json.dumps(fallback)


def test_conversation_rechecks_attempt_after_its_span_read(tmp_path, monkeypatch):
    """A reset racing the slow offset reads returns 409, never a mixed-attempt 200 envelope."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    rd = _http_run(tmp_path)
    store = EventStore(rd / "events.jsonl")
    original = span_index.SpanIndex.full_spans_for_node
    reset_once = False

    def reset_after_read(index, *args, **kwargs):
        nonlocal reset_once
        result = original(index, *args, **kwargs)
        if not reset_once:
            reset_once = True
            store.append("node_reset", {
                "node_id": 0, "generation": 0, "from_stage": "eval"})
        return result

    monkeypatch.setattr(span_index.SpanIndex, "full_spans_for_node", reset_after_read)
    response = TestClient(make_app(tmp_path)).get(
        "/api/runs/demo/nodes/0/conversation", params={"attempt": 0})

    assert reset_once is True
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "node_attempt_changed"
    assert detail["expected_attempt"] == 0 and detail["current_attempt"] == 1


def test_conversation_rechecks_run_generation_when_node_attempt_is_unchanged(
        tmp_path, monkeypatch):
    """A whole-run replacement commonly reuses node attempt zero; that must still fail the CAS."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app

    rd = _http_run(tmp_path)
    app = make_app(tmp_path)
    commands = app.state.looplab.commands
    expected = commands.run_generation(rd)
    client = TestClient(app)

    current = client.get("/api/runs/demo/nodes/0/conversation", params={
        "attempt": 0, "expected_generation": expected,
    })
    assert current.status_code == 200
    assert current.json()["run_generation"] == expected

    replacement = ("e" if expected.startswith("f") else "f") * 64
    calls = 0

    def generation_moves_after_read(_rd):
        nonlocal calls
        calls += 1
        return expected if calls == 1 else replacement

    monkeypatch.setattr(commands, "run_generation", generation_moves_after_read)
    raced = client.get("/api/runs/demo/nodes/0/conversation", params={
        "attempt": 0, "expected_generation": expected,
    }, headers={"If-None-Match": current.headers["etag"]})
    assert raced.status_code == 409
    detail = raced.json()["detail"]
    assert detail["code"] == "run_generation_changed"
    assert detail["expected_generation"] == expected
    assert detail["current_generation"] == replacement


@pytest.mark.parametrize("suffix", [
    "/nodes/0/trace?attempt=0",
    "/nodes/0/logs?attempt=0",
])
def test_node_trace_sidecars_recheck_run_generation_when_attempt_is_reused(
        tmp_path, monkeypatch, suffix):
    """A replacement run can reuse node 0/attempt 0; trace and logs still need a run CAS."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app

    rd = _http_run(tmp_path)
    app = make_app(tmp_path)
    commands = app.state.looplab.commands
    expected = commands.run_generation(rd)
    client = TestClient(app)
    separator = "&" if "?" in suffix else "?"
    current = client.get(
        f"/api/runs/demo{suffix}{separator}expected_generation={expected}")
    assert current.status_code == 200
    assert current.json()["run_generation"] == expected

    replacement = ("e" if expected.startswith("f") else "f") * 64
    calls = 0

    def generation_moves_after_read(_rd):
        nonlocal calls
        calls += 1
        return expected if calls == 1 else replacement

    monkeypatch.setattr(commands, "run_generation", generation_moves_after_read)
    raced = client.get(
        f"/api/runs/demo{suffix}{separator}expected_generation={expected}")
    assert raced.status_code == 409
    detail = raced.json()["detail"]
    assert detail["code"] == "run_generation_changed"
    assert detail["expected_generation"] == expected
    assert detail["current_generation"] == replacement


def test_node_trace_rechecks_attempt_after_its_projection(tmp_path, monkeypatch):
    """A node reset racing the indexed projection must not publish abandoned-attempt spans."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    rd = _http_run(tmp_path)
    app = make_app(tmp_path)
    original = app.state.looplab.node_trace_view
    reset_once = False

    def trace_then_reset(*args, **kwargs):
        nonlocal reset_once
        result = original(*args, **kwargs)
        if not reset_once:
            reset_once = True
            EventStore(rd / "events.jsonl").append(
                "node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
        return result

    monkeypatch.setattr(app.state.looplab, "node_trace_view", trace_then_reset)
    response = TestClient(app).get(
        "/api/runs/demo/nodes/0/trace", params={"attempt": 0})

    assert reset_once is True
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "node_attempt_changed"
    assert detail["expected_attempt"] == 0 and detail["current_attempt"] == 1


def test_node_trace_uses_attempt_zero_for_setup_and_legacy_unfolded_rows(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    rd = tmp_path / "demo"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    client = TestClient(make_app(tmp_path))

    current = client.get("/api/runs/demo/nodes/-1/trace", params={"attempt": 0})
    assert current.status_code == 200
    assert current.json()["node_id"] == -1 and current.json()["attempt"] == 0
    stale = client.get("/api/runs/demo/nodes/-1/trace", params={"attempt": 1})
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_attempt"] == 0


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


def test_trace_cache_uses_descriptor_change_time_for_windows_same_file_rewrite(
        tmp_path, monkeypatch):
    """Windows creation time must not authorize reuse after an in-place A/B rewrite."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.serve import appstate
    from looplab.serve.server import make_app

    rd = _http_run(tmp_path)
    sp = rd / "spans.jsonl"
    state = {"token": 101}
    descriptors = []

    def change_token(fd, _status=None):
        descriptors.append(fd)
        return state["token"]

    # Model Windows' creation-time file_identity on this POSIX test host: the native descriptor
    # token below must be the only signature component that changes.
    monkeypatch.setattr(
        appstate, "file_identity",
        lambda status: (
            int(status.st_dev), int(status.st_ino), int(status.st_size),
            int(status.st_mtime_ns), int(getattr(status, "st_file_attributes", 0))))
    monkeypatch.setattr(appstate, "trace_file_change_token", change_token)
    client = TestClient(make_app(tmp_path))
    first = client.get("/api/runs/demo/trace")
    assert first.status_code == 200 and "tr0" in first.text

    before = sp.stat()
    replacement = sp.read_bytes().replace(b'"tr0"', b'"zr0"')
    assert len(replacement) == before.st_size
    with open(sp, "r+b") as stream:
        stream.write(replacement)
        stream.flush()
        os.fsync(stream.fileno())
    os.utime(sp, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = sp.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino, before.st_size, before.st_mtime_ns)
    state["token"] = 102

    second = client.get("/api/runs/demo/trace")

    assert second.status_code == 200
    assert "zr0" in second.text and "tr0" not in second.text
    assert descriptors and all(isinstance(fd, int) for fd in descriptors)


def test_trace_cache_does_not_reuse_when_windows_change_time_is_unavailable(
        tmp_path, monkeypatch):
    """An unavailable ChangeTime may rebuild a view, but cannot validate a cached one."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.events import span_index as span_index_module
    from looplab.serve import appstate
    from looplab.serve.server import make_app

    _http_run(tmp_path)
    calls = []
    native_get_index = span_index_module.get_index

    def observed_get_index(path):
        calls.append(path)
        return native_get_index(path)

    monkeypatch.setattr(appstate, "trace_file_change_token", lambda _fd, _status=None: None)
    monkeypatch.setattr(span_index_module, "get_index", observed_get_index)
    client = TestClient(make_app(tmp_path))

    first = client.get("/api/runs/demo/trace")
    calls_after_first = len(calls)
    second = client.get("/api/runs/demo/trace")

    assert first.status_code == 200 and second.status_code == 200
    assert calls_after_first >= 1 and len(calls) > calls_after_first


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


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink", "fifo"])
def test_persisted_index_alias_is_rejected_promptly_and_rebuilt(run, alias_kind):
    """Derived metadata is not a capability and a FIFO must never strand a cold reader.

    ``spans.index.jsonl`` is expendable, so rejecting an unsafe entry should fall back to source
    truth and atomically publish a fresh private index.  In particular, neither a symlink nor a hard
    link may expose/overwrite its victim, and opening a FIFO must not wait for a peer.
    """
    rd, source, spans = run
    expected = get_index(source).span_count()
    index_path = rd / "spans.index.jsonl"
    assert index_path.exists()
    with span_index._CACHE_LOCK:
        span_index._CACHE.clear()
    index_path.unlink()

    victim = rd / "private-index-victim"
    marker = b"PRIVATE INDEX TARGET\n"
    if alias_kind == "symlink":
        victim.write_bytes(marker)
        try:
            index_path.symlink_to(victim)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"symlinks are unavailable on this filesystem: {exc}")
    elif alias_kind == "hardlink":
        victim.write_bytes(marker)
        try:
            os.link(victim, index_path)
        except OSError as exc:
            pytest.skip(f"hard links are unavailable on this filesystem: {exc}")
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO requires POSIX")
        os.mkfifo(index_path)

    started = time.monotonic()
    rebuilt = get_index(source)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"persisted-index {alias_kind} handling blocked for {elapsed:.2f}s"
    assert rebuilt is not None and rebuilt.span_count() == expected == len(spans)
    info = index_path.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if alias_kind != "fifo":
        assert victim.read_bytes() == marker


def test_persisted_index_offset_drift_fails_unavailable_not_wrong_span(run):
    """R6-F1.1: if a persisted offset drifts onto a DIFFERENT but still-valid span line (bit-rot on a
    network mount), full_span must fail unavailable — never return the neighboring span or silently
    publish incomplete trace evidence. The exact source-row digest detects the drift before parsing."""
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
    # The drifted row reads g1_0's bytes; its digest mismatch is an availability failure, never the
    # wrong span and never a complete-looking None/empty projection.
    with pytest.raises(OSError, match="source digest"):
        idx.full_span("g0_1")
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


def test_oversized_physical_span_row_ends_the_index_prefix(tmp_path, monkeypatch):
    """The light-index carry has the same hard row ceiling as direct trace projection.

    A well-formed span over that ceiling is not quarantined-and-skipped: it ends the append-log
    prefix, so rows behind it cannot become visible through the accelerator. It crosses many tiny
    read chunks to exercise the old unbounded carry path.
    """
    before_obj = _spans_for(0, "before-trace")[0]
    after_obj = _spans_for(1, "after-trace")[0]
    before = orjson.dumps(before_obj) + b"\n"
    after = orjson.dumps(after_obj) + b"\n"
    ceiling = max(len(before), len(after)) + 16
    oversized_obj = {
        **before_obj,
        "span_id": "oversized",
        "trace_id": "oversized-trace",
        "attributes": {"node_id": 0, "output": "x" * (ceiling * 6)},
    }
    oversized = orjson.dumps(oversized_obj) + b"\n"
    assert len(oversized) > ceiling
    source = tmp_path / "spans.jsonl"
    source.write_bytes(before + oversized + after)

    scanned = []
    real_scan = span_index._scan_light
    monkeypatch.setattr(span_index, "_scan_light",
                        lambda raw, base: (scanned.append(len(raw)), real_scan(raw, base))[1])
    monkeypatch.setattr(span_index, "TRACE_JSONL_ROW_MAX_BYTES", ceiling)
    monkeypatch.setattr(span_index, "_SCAN_CHUNK_BYTES", 17)
    span_index._CACHE.clear()
    (tmp_path / "spans.index.jsonl").unlink(missing_ok=True)

    idx = get_index(source)
    span_index._CACHE.clear()

    assert idx.covers == len(before)
    assert [span["span_id"] for span in idx.light_spans()] == [before_obj["span_id"]]
    assert scanned and max(scanned) <= ceiling


def test_span_detail_fallback_stops_at_oversized_poison_row(tmp_path, monkeypatch):
    """A guessed id cannot make the post-index fallback allocate or skip a giant physical row."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.core import trace_files
    from looplab.serve.server import make_app

    rd = _http_run(tmp_path)
    before_obj = _spans_for(0, "before-trace")[0]
    after_obj = {**_spans_for(0, "after-trace")[0], "span_id": "after-poison"}
    before = orjson.dumps(before_obj) + b"\n"
    after = orjson.dumps(after_obj) + b"\n"
    ceiling = max(len(before), len(after)) + 32
    poison_obj = {
        **before_obj,
        "span_id": "poison",
        "attributes": {"node_id": 0, "output": "x" * (ceiling * 4)},
    }
    poison = orjson.dumps(poison_obj) + b"\n"
    assert len(poison) > ceiling
    (rd / "spans.jsonl").write_bytes(before + poison + after)
    monkeypatch.setattr(span_index, "TRACE_JSONL_ROW_MAX_BYTES", ceiling)
    monkeypatch.setattr(trace_files, "TRACE_JSONL_ROW_MAX_BYTES", ceiling)
    span_index._CACHE.clear()
    (rd / "spans.index.jsonl").unlink(missing_ok=True)

    body = TestClient(make_app(tmp_path)).get(
        "/api/runs/demo/spans/after-poison").json()

    assert body["span_id"] == "after-poison"
    assert body["attributes"] == {}
    assert body["projection"]["unavailable"] is True


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
    """The projections reach attribution through `trace_root_node_id`/`effective_node_id`.

    Comment-proof (`called_names` resolves real `ast.Call` nodes), but still an ENUMERATION of the
    views — and that is exactly what it can and cannot do. It says these have not stopped calling
    the shared rule; it says nothing about a THIRD site, which is the failure that actually happened.
    `test_no_site_re_derives_the_trace_root` below is the half that discovers rather than enumerates.

    The band builder `_conversation_bands` is now where the root rule is CALLED, because the per-node
    and per-trace conversations share it (the trace one is what makes a Researcher proposal readable
    on the same surface as a node's build). So the pin follows the call: whichever of the two spells
    the rule, the OTHER must reach it through this one helper — a conversation that re-derived either
    half for itself is exactly the drift this rule exists to catch.
    """
    from _source_scan import called_names

    from looplab.events import traceview

    for name in ("build_trace_view", "_conversation_bands"):
        calls = called_names(getattr(traceview, name))
        assert "trace_root_node_id" in calls, f"{name} no longer uses the shared root rule"
    # `build_conversation` owns the node ATTRIBUTION predicate (which of a trace's spans are this
    # node's); the band helper owns the structural root the predicate is resolved against.
    node_conversation = called_names(traceview.build_conversation)
    assert "effective_node_id" in node_conversation, \
        "build_conversation no longer uses the shared attribution"
    for name in ("build_conversation", "build_trace_conversation"):
        assert "_conversation_bands" in called_names(getattr(traceview, name)), \
            f"{name} builds its own bands again — the root rule would be re-derived next"


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
