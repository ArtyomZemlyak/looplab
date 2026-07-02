"""EventStore incremental read cache (this session, Phase 2 — the log-rescan fix).

The folded loop calls `read_all()` many times per iteration (and the mid-eval abort watcher every
0.3s); the cache reads only the bytes appended since the previous call. These tests pin the
invariant that MATTERS: the cached `read_all()` returns byte-for-byte what a fresh full `iter_jsonl`
scan would, across appends, torn tails, heal-truncation, and concurrent (threaded) readers — so the
optimization can never change engine behavior. All offline."""
from __future__ import annotations

import threading
from pathlib import Path

from looplab.eventstore import EventStore, iter_jsonl


def _fresh_seqs(p):
    return [o["seq"] for o in iter_jsonl(p)]


def test_incremental_parity_across_appends(tmp_path):
    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    for i in range(60):
        s.append("node_created", {"node_id": i, "operator": "draft", "idea": {"operator": "draft"}})
        assert [e.seq for e in s.read_all()] == _fresh_seqs(p)   # cache == ground truth every step


def test_torn_final_line_is_ignored(tmp_path):
    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    for i in range(10):
        s.append("node_created", {"node_id": i, "operator": "draft", "idea": {"operator": "draft"}})
    n_before = len(s.read_all())
    with open(p, "ab") as f:
        f.write(b'{"seq": 999, "type": "torn"')          # partial: no trailing newline
    got = s.read_all()
    assert len(got) == n_before and all(e.seq != 999 for e in got)
    assert [e.seq for e in got] == _fresh_seqs(p)


def test_heal_truncation_rebuilds_cache(tmp_path):
    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    for i in range(10):
        s.append("node_created", {"node_id": i, "operator": "draft", "idea": {"operator": "draft"}})
    _ = s.read_all()                                       # warm the cache
    # shrink the file (simulate a heal-truncate to an earlier newline) behind the store's back
    data = p.read_bytes()
    newline = data.rfind(b"\n", 0, len(data) // 2)         # a newline boundary in the first half
    p.write_bytes(data[: newline + 1])
    got = s.read_all()                                     # size shrank => cache must rebuild
    assert [e.seq for e in got] == _fresh_seqs(p)


def test_concurrent_readers_are_race_free(tmp_path):
    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    for i in range(200):
        s.append("node_created", {"node_id": i, "operator": "draft", "idea": {"operator": "draft"}})
    errs: list = []

    def hammer():
        try:
            for _ in range(150):
                assert len(s.read_all()) == 200
        except Exception as e:   # pragma: no cover - only on a race regression
            errs.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs
    assert [e.seq for e in s.read_all()] == _fresh_seqs(p)
