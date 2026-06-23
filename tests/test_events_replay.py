"""I1 keystone: event store durability + replay determinism (the #1 P0 risk)."""
from __future__ import annotations

from looplab.eventstore import EventStore
from looplab.replay import fold


def _seed_events(store: EventStore) -> None:
    store.append("run_started", {"run_id": "r1", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": ""}})
    store.append("node_evaluated", {"node_id": 0, "metric": 5.0})
    store.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                                  "idea": {"operator": "improve", "params": {"x": 2.0}, "rationale": ""}})
    store.append("node_evaluated", {"node_id": 1, "metric": 2.0})


def test_replay_is_deterministic(tmp_path):
    p = tmp_path / "events.jsonl"
    _seed_events(EventStore(p))

    a = fold(EventStore(p).read_all())
    b = fold(EventStore(p).read_all())
    assert a.model_dump() == b.model_dump()
    # best is the lower metric, deterministically
    assert a.best_node_id == 1
    assert a.best().metric == 2.0


def test_torn_final_line_is_ignored(tmp_path):
    """A crash mid-append leaves a partial last line; read_all must drop it and the
    surviving prefix must replay to a consistent state."""
    p = tmp_path / "events.jsonl"
    _seed_events(EventStore(p))

    full = fold(EventStore(p).read_all())

    # Simulate a torn write: append a partial (no trailing newline) record.
    with open(p, "ab") as f:
        f.write(b'{"seq": 99, "ts": 0, "type": "node_eval')  # truncated, no newline

    after = fold(EventStore(p).read_all())
    assert after.model_dump() == full.model_dump()  # torn record had no effect


def test_seq_is_monotonic_and_resumes(tmp_path):
    p = tmp_path / "events.jsonl"
    s1 = EventStore(p)
    _seed_events(s1)
    last = list(s1.read_all())[-1].seq
    # A fresh store on the same file must continue numbering, not restart.
    s2 = EventStore(p)
    e = s2.append("run_finished", {})
    assert e.seq == last + 1
