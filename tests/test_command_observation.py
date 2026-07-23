"""PERF-02: command monitoring scans an append-only log once, then only its event delta."""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import orjson
import pytest

import looplab.serve.command_observation as command_observation
from looplab.events.eventstore import EventStore
from looplab.serve.command_observation import CommandObservationIndex


def _row(seq: int, event_type: str = "metric", data: dict | None = None) -> bytes:
    return orjson.dumps({
        "v": 1,
        "seq": seq,
        "ts": float(seq),
        "type": event_type,
        "data": data or {},
        "trace_id": None,
        "span_id": None,
    }) + b"\n"


def _write_log(path, count: int) -> int:
    raw = b"".join(_row(seq) for seq in range(count))
    path.write_bytes(raw)
    return len(raw)


@pytest.mark.parametrize("count", [50_000, 100_000])
def test_large_cold_scan_then_unchanged_and_one_append_are_linear_delta(tmp_path, count):
    path = tmp_path / "events.jsonl"
    source_bytes = _write_log(path, count)
    index = CommandObservationIndex()

    cold = index.observe(path)
    assert cold.event_count == count
    assert cold.latest_seq == count - 1
    assert index.metrics.last_bytes_read == source_bytes
    assert index.metrics.last_records_parsed == count

    unchanged = index.observe(path)
    assert unchanged.revision == cold.revision
    assert unchanged.event_count == count
    assert index.metrics.last_bytes_read == 0
    assert index.metrics.last_records_parsed == 0

    appended = _row(count, "node_evaluated", {"node_id": 0})
    with path.open("ab") as handle:
        handle.write(appended)
    delta = index.observe(path)
    assert delta.event_count == count + 1
    assert delta.latest_seq == count
    assert delta.has_domain_progress(count - 1)
    assert index.metrics.last_bytes_read == len(appended)
    assert index.metrics.last_records_parsed == 1


def test_atomic_batch_counts_and_indexes_each_logical_event_with_physical_valid_end(tmp_path):
    path = tmp_path / "events.jsonl"
    store = EventStore(path)
    store.append("run_started", {"run_id": "r"})
    store.append_many([
        ("metric", {"value": 1}),
        ("run_abort", {"reason": "operator"}),
    ])
    index = CommandObservationIndex()

    observed = index.observe(path)

    assert observed.event_count == 3
    assert observed.latest_seq == 2
    assert observed.valid_end == path.stat().st_size
    assert observed.torn_tail is False
    assert [event.type for event in observed.events()] == ["run_started", "metric", "run_abort"]
    assert observed.latest_run_abort is not None and observed.latest_run_abort.seq == 2
    assert index.metrics.last_records_parsed == 3


def test_card_drop_command_and_domain_types_have_distinct_progress_semantics(tmp_path):
    """New rows classify by type; the payload check remains only for pre-split log compatibility."""
    path = tmp_path / "events.jsonl"
    path.write_bytes(b"".join([
        _row(0, "run_started", {"run_id": "r"}),
        _row(1, "card_dropped", {
            "id": "card-operator", "reason": "stopped", "dropped_by": "operator",
        }),
    ]))
    index = CommandObservationIndex()
    operator = index.observe(path)
    assert operator.has_domain_progress(0) is False

    with path.open("ab") as handle:
        handle.write(_row(2, "card_auto_dropped", {
            "id": "card-engine", "reason": "stale", "dropped_by": "engine",
        }))
    canonical = index.observe(path)
    assert canonical.has_domain_progress(1) is True

    with path.open("ab") as handle:
        handle.write(_row(3, "card_dropped", {
            "id": "card-legacy", "reason": "duplicate", "dropped_by": "engine",
        }))
    legacy = index.observe(path)
    assert legacy.has_domain_progress(2) is True


def test_monotonic_sequence_gap_ends_command_observation_at_the_eventstore_prefix(tmp_path):
    # A forward seq jump (0 -> 2) is corruption, not a repair artifact — the engine appends densely and
    # repair-log truncates a corrupt TAIL, so a mid-file gap can only be tampering. It ends the
    # recoverable prefix exactly like a duplicate; only the dense run_started prefix survives.
    path = tmp_path / "events.jsonl"
    first = _row(0, "run_started", {"run_id": "r"})
    path.write_bytes(first + _row(2, "resume") + _row(3, "command_ack"))

    observed = CommandObservationIndex().observe(path)

    assert observed.event_count == 1
    assert observed.latest_seq == 0
    assert observed.valid_end == len(first)
    assert observed.torn_tail is True
    assert [event.type for event in observed.events()] == ["run_started"]


def test_duplicate_sequence_ends_command_observation_at_the_eventstore_prefix(tmp_path):
    path = tmp_path / "events.jsonl"
    first = _row(0, "run_started", {"run_id": "r"})
    path.write_bytes(first + _row(0, "resume") + _row(1, "command_ack"))

    observed = CommandObservationIndex().observe(path)

    assert observed.event_count == 1
    assert observed.latest_seq == 0
    assert observed.valid_end == len(first)
    assert observed.torn_tail is True
    assert [event.type for event in observed.events()] == ["run_started"]


def test_exact_intent_ack_finish_and_abort_facts_preserve_command_semantics(tmp_path):
    path = tmp_path / "events.jsonl"
    command_id = "cmd_" + "a" * 32
    rows = [
        _row(0, "run_started", {"run_id": "r", "task_id": "t", "goal": "g"}),
        _row(1, "resume", {"_command_id": command_id}),
        _row(2, "command_ack", {"command_id": command_id, "event_seq": 1}),
        _row(3, "run_finished", {"reason": "error", "error": "boom"}),
        _row(4, "run_abort", {"reason": "operator"}),
        _row(5, "run_finished", {"reason": "done"}),
    ]
    path.write_bytes(b"".join(rows))
    index = CommandObservationIndex()
    observed = index.observe(path)

    assert observed.marked_intent(command_id).seq == 1
    assert observed.has_ack(command_id, 1)
    assert not observed.has_ack(command_id, 2)
    assert observed.domain_failure_after(2).seq == 3
    assert observed.domain_failure_after(3) is None
    assert observed.has_non_error_finish_after(4)
    assert observed.latest_run_abort.seq == 4
    assert observed.latest_seq == 5
    assert observed.state().finished is True

    with path.open("ab") as handle:
        handle.write(_row(6, "resume", {"_command_id": command_id}))
    duplicate = index.observe(path)
    assert duplicate.marked_intent(command_id) is None
    # The earlier logical snapshot must not acquire the later duplicate marker.
    assert observed.marked_intent(command_id).seq == 1


def test_torn_tail_is_ignored_then_completed_from_the_valid_byte_boundary(tmp_path):
    path = tmp_path / "events.jsonl"
    complete = _row(0, "run_started", {"run_id": "r"})
    second = _row(1, "resume", {"_command_id": "cmd_" + "b" * 32})
    split = len(second) // 2
    path.write_bytes(complete + second[:split])
    index = CommandObservationIndex()

    torn = index.observe(path)
    assert torn.torn_tail is True
    assert torn.event_count == 1
    assert torn.valid_end == len(complete)
    assert index.observe(path).revision == torn.revision
    assert index.metrics.last_bytes_read == 0

    with path.open("ab") as handle:
        handle.write(second[split:])
    healed = index.observe(path)
    assert healed.torn_tail is False
    assert healed.event_count == 2
    assert healed.marked_intent("cmd_" + "b" * 32).seq == 1
    # Delta includes the old partial bytes because valid_end, not observed_size, is the resume point.
    assert index.metrics.last_bytes_read == len(second)


def test_shrink_replace_and_same_size_in_place_rewrite_rebuild_facts(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    first_id = "cmd_" + "1" * 32
    second_id = "cmd_" + "2" * 32
    initial = _row(0, "resume", {"_command_id": first_id})
    rewritten = _row(0, "resume", {"_command_id": second_id})
    assert len(initial) == len(rewritten)
    path.write_bytes(initial)
    # Prove the bounded content probe is the reset fence: emulate a Windows timestamp collision
    # instead of letting a high-resolution test filesystem make the metadata check sufficient.
    monkeypatch.setattr(command_observation, "_metadata", lambda _stat: (1, 1))
    index = CommandObservationIndex()
    first = index.observe(path)
    rebuilds = index.metrics.rebuilds

    path.write_bytes(rewritten)
    same_size = index.observe(path)
    assert same_size.revision != first.revision
    assert same_size.marked_intent(first_id) is None
    assert same_size.marked_intent(second_id).seq == 0
    assert index.metrics.rebuilds == rebuilds + 1
    assert index.metrics.last_bytes_read == len(rewritten)

    path.write_bytes(b"")
    shrunk = index.observe(path)
    assert shrunk.event_count == 0
    assert shrunk.latest_seq == -1
    assert index.metrics.rebuilds == rebuilds + 2

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(initial)
    os.replace(replacement, path)
    replaced = index.observe(path)
    assert replaced.marked_intent(first_id).seq == 0
    assert replaced.marked_intent(second_id) is None
    assert index.metrics.rebuilds == rebuilds + 3


def test_probe_change_while_scanning_discards_the_mixed_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    path.write_bytes(_row(0, "run_started", {"run_id": "stable"}))
    original_probe = command_observation._probe_signature
    probes = 0

    def changing_probe(handle, size):
        nonlocal probes
        probes += 1
        signature = original_probe(handle, size)
        # Emulate a sampled same-size rewrite after parsing but before the final snapshot fence.
        return b"changed-during-scan" if probes == 2 else signature

    monkeypatch.setattr(command_observation, "_probe_signature", changing_probe)
    index = CommandObservationIndex()
    observed = index.observe(path)

    assert observed.event_count == 1
    assert observed.state().run_id == "stable"
    assert index.metrics.rebuilds == 2
    assert index.metrics.scan_calls == 2
    assert index.observe(path).revision == observed.revision


def test_public_observation_values_cannot_mutate_the_cached_snapshot(tmp_path):
    path = tmp_path / "events.jsonl"
    command_id = "cmd_" + "c" * 32
    path.write_bytes(b"".join([
        _row(0, "run_started", {"run_id": "immutable", "task_id": "t", "goal": "g"}),
        _row(1, "resume", {"_command_id": command_id}),
        _row(2, "run_abort", {"reason": "operator"}),
    ]))
    index = CommandObservationIndex()
    observed = index.observe(path)

    observed.events()[0].data["run_id"] = "corrupt"
    observed.marked_intent(command_id).data["_command_id"] = "corrupt"
    observed.latest_run_abort.data["reason"] = "corrupt"
    observed.state().run_id = "corrupt"

    sibling = index.observe(path)
    assert sibling.events()[0].data["run_id"] == "immutable"
    assert sibling.marked_intent(command_id).data["_command_id"] == command_id
    assert sibling.latest_run_abort.data["reason"] == "operator"
    assert sibling.state().run_id == "immutable"


def test_concurrent_callers_share_one_consistent_cold_scan(tmp_path):
    path = tmp_path / "events.jsonl"
    source_bytes = _write_log(path, 10_000)
    index = CommandObservationIndex()
    workers = 16
    barrier = threading.Barrier(workers)

    def observe():
        barrier.wait()
        item = index.observe(path)
        return item.revision, item.event_count, item.latest_seq

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _item: observe(), range(workers)))

    assert len(set(results)) == 1
    assert results[0][1:] == (10_000, 9_999)
    assert index.metrics.scan_calls == 1
    assert index.metrics.bytes_read == source_bytes
    assert index.metrics.cache_hits == workers - 1


def test_lru_evicts_ninth_run_and_evicted_run_is_cold_again(tmp_path):
    index = CommandObservationIndex(max_indexed_runs=8)
    paths = []
    for number in range(9):
        path = tmp_path / f"run-{number}" / "events.jsonl"
        path.parent.mkdir()
        path.write_bytes(_row(0, "run_started", {"run_id": str(number)}))
        index.observe(path)
        paths.append(path)

    assert len(index.cached_paths) == 8
    assert str(paths[0]) not in index.cached_paths
    rebuilds = index.metrics.rebuilds

    index.observe(paths[0])
    assert index.metrics.rebuilds == rebuilds + 1
    assert index.metrics.last_bytes_read == paths[0].stat().st_size
    assert str(paths[1]) not in index.cached_paths
    assert len(index.cached_paths) == 8
