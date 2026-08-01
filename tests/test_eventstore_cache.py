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

from looplab.events.eventstore import EventStore, decode_event_record, iter_jsonl


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


def test_cache_revalidates_rewritten_prefix_before_external_growth(tmp_path):
    """Growth is not append-only proof: a rewritten cached prefix must not be joined to a new tail."""
    p = tmp_path / "events.jsonl"
    cached = EventStore(p)
    cached.append("aaaa", {})
    cached.append("middle", {})
    assert [event.type for event in cached.read_all()] == ["aaaa", "middle"]

    raw = p.read_bytes()
    p.write_bytes(raw.replace(b'"aaaa"', b'"bbbb"', 1))
    EventStore(p).append("tail", {})

    assert [event.type for event in cached.read_all()] == ["bbbb", "middle", "tail"]


def test_ordinary_future_event_envelope_is_rejected():
    with pytest.raises(ValueError, match="unsupported event envelope version"):
        decode_event_record({
            "v": 2, "seq": 0, "ts": 1.0, "type": "run_started", "data": {},
            "trace_id": None, "span_id": None,
        })


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


def test_same_size_rewrite_with_seq_gap_fails_closed(tmp_path):
    from looplab.events.eventstore import EventLogCorruptionError
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

    with pytest.raises(EventLogCorruptionError):
        s.append("stale", {}, expected_last_seq=1)
    with pytest.raises(EventLogCorruptionError):
        s.append("current", {}, expected_last_seq=7)
    assert s.divergence == {
        "good_records": 1, "corrupt_line": 2, "dropped_lines": 0,
    }


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


def test_event_sequence_continues_empty_is_not_a_continuation():
    """`event_sequence_continues([])` must be False, not a vacuously-True `all([])`: every caller
    then dereferences `events[-1].seq` (IndexError). A dense prefix from `expected_seq` is True; a
    forward seq gap or a non-int seq is False (fail-closed)."""
    from types import SimpleNamespace
    from looplab.events.eventstore import event_sequence_continues
    ev = lambda s: SimpleNamespace(seq=s)  # noqa: E731 - tiny local Event stand-in (duck-typed .seq)

    assert event_sequence_continues([], 5) is False              # empty -> not a continuation
    assert event_sequence_continues([ev(5), ev(6), ev(7)], 5) is True   # dense prefix from expected
    assert event_sequence_continues([ev(5), ev(7)], 5) is False         # forward gap -> fail closed
    assert event_sequence_continues([ev("5")], 5) is False              # non-int seq -> fail closed


def test_a_reader_only_store_does_not_re_read_the_whole_prefix_on_every_external_append(
        tmp_path, monkeypatch):
    """A store that only READS a log another process writes (the UI server tailing the engine, the
    engine observing UI control appends) never gets a trusted-growth receipt, so every poll that sees
    new bytes has to re-validate the cached prefix. Doing that with a full-prefix sha256 every time is
    O(prefix) per external append — O(n^2) over a run, the exact quadratic cost this cache exists to
    avoid. The steady-state poll must instead cost a bounded two windows plus the new bytes, no
    matter how large the log has already grown."""
    from looplab.events import eventstore

    p = tmp_path / "events.jsonl"
    writer = EventStore(p)
    blob = "x" * 4096
    while not p.exists() or p.stat().st_size < 12 * eventstore._CACHE_ANCHOR_BYTES:
        writer.append("seed", {"blob": blob})

    reader = EventStore(p)
    assert len(reader.read_all()) == len(writer.read_all())
    # One external append BEFORE counting: the first observation is the one that legitimately pays
    # the full-prefix proof (and seeds the windows from it). What must not scale with the log is
    # every poll AFTER that.
    writer.append("warm", {})
    reader.read_all()
    prefix_bytes = p.stat().st_size

    read_bytes = 0
    counting = False                              # the WRITER shares this module-level `open`, and
    real_open = open                              # its own append path reads too — count only polls

    def counting_open(*a, **kw):
        handle = real_open(*a, **kw)
        real_read = handle.read

        def read(*ra, **rkw):
            nonlocal read_bytes
            chunk = real_read(*ra, **rkw)
            if counting:
                read_bytes += len(chunk)
            return chunk

        handle.read = read
        return handle

    monkeypatch.setattr(eventstore, "open", counting_open, raising=False)
    polls = 12
    for _ in range(polls):
        writer.append("tail", {})                 # the OTHER process appends…
        counting = True
        reader.read_all()                         # …and this one polls
        counting = False
    monkeypatch.undo()

    # Two windows plus a handful of new bytes; the doubling proof cannot fire on growth this small.
    # The quadratic version reads `prefix_bytes` (12 windows) per poll and blows straight past this.
    assert read_bytes <= polls * 3 * eventstore._CACHE_ANCHOR_BYTES, (read_bytes, polls)
    assert read_bytes < polls * prefix_bytes // 3, (read_bytes, polls, prefix_bytes)
    # …and the reader still sees exactly what a fresh scan does.
    assert _fresh_seqs(p) == [event.seq for event in reader.read_all()]


def test_the_cheap_prefix_windows_still_catch_a_rewrite_at_either_end(tmp_path):
    """The windows are the per-poll arm of the prefix check, so they must catch both shapes the full
    digest caught: a file rebuilt from the start (the HEAD moves) and one edited up to the append
    point (the TAIL moves). Each is followed by an external append, which is the only way a rewritten
    prefix ever gets joined to a fresh tail."""
    from looplab.events import eventstore

    for edit_head in (True, False):
        p = tmp_path / f"events-{edit_head}.jsonl"
        writer = EventStore(p)
        writer.append("head-marker", {"blob": "h" * 4096})
        while p.stat().st_size < 3 * eventstore._CACHE_ANCHOR_BYTES:
            writer.append("filler", {"blob": "f" * 4096})
        writer.append("tail-marker", {"blob": "t" * 4096})

        reader = EventStore(p)
        reader.read_all()
        # One external append first, so the windows are SEEDED and the next poll takes the cheap arm.
        EventStore(p).append("warm", {})
        assert [e.type for e in reader.read_all()][-1] == "warm"

        raw = p.read_bytes()
        target = b'"head-marker"' if edit_head else b'"tail-marker"'
        p.write_bytes(raw.replace(target, b'"REWRITTEN___"', 1))   # same length -> size is unchanged
        EventStore(p).append("after", {})

        types = [e.type for e in reader.read_all()]
        assert "REWRITTEN___" in types, (edit_head, types[:3], types[-3:])
        assert types[-1] == "after"
        assert _fresh_seqs(p) == [e.seq for e in reader.read_all()]
