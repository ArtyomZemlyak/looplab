"""EventStore incremental read cache (this session, Phase 2 — the log-rescan fix).

The folded loop calls `read_all()` many times per iteration (and the mid-eval abort watcher every
0.3s); the cache reads only the bytes appended since the previous call. These tests pin the
invariant that MATTERS: the cached `read_all()` returns byte-for-byte what a fresh full `iter_jsonl`
scan would, across appends, torn tails, heal-truncation, and concurrent (threaded) readers — so the
optimization can never change engine behavior. All offline."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from looplab.events.eventstore import EventStore, iter_jsonl


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


def test_complete_corrupt_tail_fails_closed_before_next_append(tmp_path):
    """A newline-terminated bad last record is not a torn write. If append accepts it, that append
    immediately becomes an invisible tail behind the reader's stop boundary."""
    from looplab.events.eventstore import EventLogCorruptionError, log_divergence

    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    s.append("ok", {})
    with p.open("ab") as f:
        f.write(b"{bad json}\n")

    detail = log_divergence(p)
    assert detail and detail["dropped_lines"] == 0
    reopened = EventStore(p)
    with pytest.raises(EventLogCorruptionError):
        reopened.append("must_not_be_hidden", {})
    assert b"must_not_be_hidden" not in p.read_bytes()


def test_cache_revalidates_same_size_mid_run_rewrite(tmp_path):
    """A same-size in-place rewrite must invalidate the incremental cache instead of returning an
    Event sequence that no longer exists on disk."""
    import os
    import time

    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    s.append("aaaa", {})
    assert s.read_all()[0].type == "aaaa"                   # warm cache
    raw = p.read_bytes()
    p.write_bytes(raw.replace(b'"aaaa"', b'"bbbb"'))      # exactly the same byte length
    future = time.time_ns() + 2_000_000_000
    os.utime(p, ns=(future, future))                         # deterministic mtime change on coarse FS

    assert s.read_all()[0].type == "bbbb"


def test_shrink_rebases_seq_and_rejects_pre_reset_cas(tmp_path):
    """An EventStore object may outlive an in-place run reset. Its old high-water mark must not make
    the OLD expected tail valid against the replacement history."""
    from looplab.events.eventstore import EventStoreConcurrencyError

    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    for i in range(3):
        s.append("before", {"i": i})
    first = p.read_bytes().splitlines(keepends=True)[0]
    p.write_bytes(first)                                 # replacement tail is seq=0

    with pytest.raises(EventStoreConcurrencyError):
        s.append("stale", {}, expected_last_seq=2)
    assert s.append("current", {}, expected_last_seq=0).seq == 1


def test_same_size_rewrite_rebases_seq_for_cas(tmp_path):
    from looplab.events.eventstore import EventStoreConcurrencyError
    import os
    import time

    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    s.append("before", {})
    s.append("before", {})
    raw = p.read_bytes()
    p.write_bytes(raw.replace(b'"seq":1', b'"seq":7'))  # exact byte length, new disk tail=7
    future = time.time_ns() + 2_000_000_000
    os.utime(p, ns=(future, future))

    with pytest.raises(EventStoreConcurrencyError):
        s.append("stale", {}, expected_last_seq=1)
    assert s.append("current", {}, expected_last_seq=7).seq == 8


def test_atomic_file_replacement_rebases_seq_for_cas(tmp_path):
    from looplab.events.eventstore import EventStoreConcurrencyError
    import os

    p = tmp_path / "events.jsonl"
    s = EventStore(p)
    s.append("old", {})
    s.append("old", {})
    replacement = tmp_path / "replacement.jsonl"
    EventStore(replacement).append("new", {})             # replacement tail is seq=0
    os.replace(replacement, p)

    with pytest.raises(EventStoreConcurrencyError):
        s.append("stale", {}, expected_last_seq=1)
    assert s.append("current", {}, expected_last_seq=0).seq == 1


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


def test_read_all_tolerates_invalid_event_record(tmp_path):
    p = tmp_path / "events.jsonl"
    es = EventStore(p)
    es.append("run_started", {"run_id": "r"})
    es.append("hint", {"text": "x"})
    with open(p, "a", encoding="utf-8") as f:                      # valid JSON dict, invalid Event
        f.write(json.dumps({"seq": 99, "type": "x", "data": [1, 2, 3]}) + "\n")
    first = [e.seq for e in es.read_all()]
    second = [e.seq for e in es.read_all()]
    third = [e.seq for e in es.read_all()]
    assert first == second == third                                # no duplicated prefix growth
    assert len(first) == 2


def test_append_is_thread_safe_for_concurrent_writers(tmp_path):
    """The engine now appends the concurrent deep-research memo from a WORKER THREAD while the main
    loop also appends. `_append_lock` must keep seq-derivation atomic even if the interprocess flock
    degrades to a no-op — every event gets a UNIQUE seq and no line is torn."""
    import threading
    from looplab.events.eventstore import EventStore, iter_jsonl
    st = EventStore(tmp_path / "events.jsonl")
    N, M = 8, 40

    def worker(w):
        for i in range(M):
            st.append("probe", {"w": w, "i": i})

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = list(iter_jsonl(tmp_path / "events.jsonl"))
    assert len(rows) == N * M                               # nothing dropped / torn
    seqs = sorted(r["seq"] for r in rows)
    assert len(set(seqs)) == N * M                          # every seq UNIQUE (no collision)
    assert seqs == list(range(seqs[0], seqs[0] + N * M))    # a dense, gap-free consecutive range
