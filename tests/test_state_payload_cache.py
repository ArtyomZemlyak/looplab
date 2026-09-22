"""`AppState.state_payload`'s cache holds ONE live payload per run and audience, and builds it once.

Review 2026-09-22, SRV2-01. The cache was keyed by the WHOLE file identity (plus `upto_seq` and
audience), with FIFO eviction only past 256 entries and no single-flight. A live run appends (an
`llm_usage` row per provider call), so every tick left the superseded payload behind: 20 appends,
one `/state` or SSE tick after each, retained 21 entries — 185.7 MB at 9.3 MB per payload, ~2.4 GB at
the cap from one tab. And four readers arriving after one append ran four full folds (3.28 s),
because nothing made the second reader wait for the first one's build.

The properties are stated on the cache's own maps (`_state_live`, `_state_history`) and on the fold
the payload is built with (`appstate.fold`), because those are what the defect was: entries retained
and folds run. Nothing here asserts a timing.
"""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("fastapi")

from looplab.events.eventstore import EventStore                    # noqa: E402
from looplab.serve import appstate                                  # noqa: E402
from looplab.serve.server import make_app                           # noqa: E402

RUN = "demo"


def _run(root, extra_events: int = 0):
    rd = root / RUN
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": RUN, "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                  "code": "print(1)"})
    for index in range(extra_events):
        store.append("hint", {"text": f"pad {index}"})
    return rd


def _entries_for(mapping, rd) -> list:
    return [key for key in mapping if key[0] == str(rd)]


def test_a_live_run_keeps_one_payload_however_often_it_appends(tmp_path):
    """MUTATION: key the live slot by the file identity again -> twelve entries for one run."""
    rd = _run(tmp_path)
    srv = make_app(tmp_path).state.looplab
    store = EventStore(rd / "events.jsonl")
    seqs = []
    for index in range(12):                       # an append, then one SSE tick, twelve times
        store.append("hint", {"text": f"tick {index}"})
        seqs.append(srv.state_payload(rd)["seq"])
    assert seqs == sorted(set(seqs)), "each tick must see its own append (fresh, never stale)"
    assert len(_entries_for(srv._state_live, rd)) == 1, "superseded live payloads were retained"
    assert _entries_for(srv._state_history, rd) == []


def test_readers_racing_one_append_fold_it_once(tmp_path, monkeypatch):
    """Single flight: the first reader to miss builds, the rest wait for it and re-check. The fold is
    held open so every reader has arrived before it can finish. MUTATION: drop the per-run build
    lock -> four folds."""
    rd = _run(tmp_path)
    srv = make_app(tmp_path).state.looplab
    srv.state_payload(rd)                                             # the warm, pre-append entry
    EventStore(rd / "events.jsonl").append("hint", {"text": "one append"})
    real_fold = appstate.fold
    folds: list[int] = []

    def slow_counting_fold(events):
        folds.append(len(events))
        time.sleep(0.3)
        return real_fold(events)

    monkeypatch.setattr(appstate, "fold", slow_counting_fold)
    gate = threading.Barrier(4)
    seen: list = []

    def _reader():
        gate.wait(10)
        seen.append(srv.state_payload(rd)["seq"])

    readers = [threading.Thread(target=_reader) for _ in range(4)]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join(30)
    assert len(seen) == 4 and len(set(seen)) == 1, seen
    assert len(folds) == 1, f"{len(folds)} folds for one append"


def test_each_audience_has_its_own_live_slot(tmp_path):
    """The review payload is a DIFFERENT projection of the same log; one slot per (run, audience)
    keeps the boundary the old key kept."""
    rd = _run(tmp_path)
    srv = make_app(tmp_path).state.looplab
    owner = srv.state_payload(rd)
    review = srv.state_payload(rd, audience="review")
    assert sorted(key[1] for key in _entries_for(srv._state_live, rd)) == ["owner", "review"]
    assert owner["seq"] == review["seq"]


def test_historical_reads_are_a_small_lru_beside_the_live_slot(tmp_path, monkeypatch):
    rd = _run(tmp_path, extra_events=12)
    srv = make_app(tmp_path).state.looplab
    for seq in range(12):
        assert srv.state_payload(rd, seq)["seq"] == seq
    history = _entries_for(srv._state_history, rd)
    assert 0 < len(history) <= appstate._STATE_HISTORY_MAX, len(history)
    assert _entries_for(srv._state_live, rd) == [], "a historical read is not the live slot"

    folds: list = []
    real_fold = appstate.fold
    monkeypatch.setattr(appstate, "fold", lambda events: folds.append(1) or real_fold(events))
    assert srv.state_payload(rd, 11)["seq"] == 11
    assert folds == [], "the most recent historical read must still be cached"


def test_invalidation_drops_the_live_slot_and_the_history(tmp_path):
    rd = _run(tmp_path)
    srv = make_app(tmp_path).state.looplab
    srv.state_payload(rd)
    srv.state_payload(rd, audience="review")
    srv.state_payload(rd, 0)
    srv.invalidate_run_caches(rd)
    assert _entries_for(srv._state_live, rd) == []
    assert _entries_for(srv._state_history, rd) == []


def test_the_live_slots_are_bounded_across_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(appstate, "_STATE_LIVE_SLOTS_MAX", 3)
    srv = make_app(tmp_path).state.looplab
    for index in range(5):
        rd = tmp_path / f"r{index}"
        rd.mkdir()
        EventStore(rd / "events.jsonl").append(
            "run_started", {"run_id": rd.name, "task_id": "t", "goal": "g", "direction": "min"})
        srv.state_payload(rd)
    assert len(srv._state_live) == 3
    assert {key[0] for key in srv._state_live} == {str(tmp_path / f"r{i}") for i in (2, 3, 4)}
