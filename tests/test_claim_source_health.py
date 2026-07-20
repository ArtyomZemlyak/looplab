from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import orjson
import pytest

from looplab.engine.lessons import LessonMemory
from looplab.events.eventstore import (
    _interprocess_lock,
    read_jsonl_lenient,
    read_jsonl_lenient_with_health,
)


def _lesson(statement: str, run_id: str) -> dict:
    return {
        "statement": statement,
        "outcome": "supported",
        "claim_stance": "support",
        "evidence": [1],
        "run_id": run_id,
        "task_id": "t",
        "direction": "min",
        "fingerprint": [],
    }


@pytest.mark.parametrize("field,value", [
    ("evidence", [True]),
    ("evidence", [1.25]),
    ("evidence", [{"node": 1}]),
    ("evidence", ["not-a-node"]),
    ("evidence", ["9" * 10_000]),
    ("outcome", {"verdict": "supported"}),
    ("outcome", "invented-verdict"),
    ("claim_stance", "definitely"),
    ("run_id", 7),
    ("task_id", {"scope": "t"}),
    ("direction", "sideways"),
    ("direction", 1),
])
def test_poisoned_lesson_semantics_quarantine_whole_row_and_lower_authority(
        tmp_path, field, value):
    from looplab.engine.claims import claims_for_memory

    good = _lesson("retained evidence", "good-run")
    poisoned = {**_lesson("poisoned evidence", "bad-run"), field: value}
    (tmp_path / "lessons.jsonl").write_bytes(
        orjson.dumps(poisoned) + b"\n" + orjson.dumps(good) + b"\n")

    rows = claims_for_memory(tmp_path, structured=True)

    assert [row["statement"] for row in rows] == ["retained evidence"]
    assert rows[0]["support"] == ["good-run:1"]
    # Retained positive evidence remains visible, but one-sided truth is fail-closed while a source row
    # is quarantined. No invalid element is silently normalized under an exact receipt.
    assert rows[0]["epistemic"] == "inconclusive"
    source = rows.claim_source
    assert source["source_complete"] is source["read_complete"] is False
    assert source["lessons"] == {
        "read_complete": False,
        "rows_total": 2,
        "rows_retained": 1,
        "rows_quarantined": 1,
        "malformed_rows": 0,
        "invalid_rows": 1,
    }


def test_bounded_integer_string_node_source_remains_legacy_compatible(tmp_path):
    from looplab.engine.claims import claims_for_memory

    row = {**_lesson("numeric-string evidence", "legacy-run"), "evidence": ["-42", "7"]}
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(row) + b"\n")

    claims = claims_for_memory(tmp_path, structured=True)

    assert claims[0]["support"] == ["legacy-run:-42", "legacy-run:7"]
    assert claims.claim_source["source_complete"] is True


def test_lenient_health_quarantines_invalid_utf8_and_keeps_later_rows(tmp_path):
    path = tmp_path / "mixed.jsonl"
    # CRLF is one ordinary delimiter; a blank physical row remains a keep_bad placeholder; a UTF-8 BOM is
    # valid text but invalid JSON under the existing str-parser contract; an undecodable byte is malformed;
    # and neither poison may hide the valid tail.
    path.write_bytes(
        b'{"first":1}\r\n'
        b'\xff\n'
        b'\n'
        b'\xef\xbb\xbf{"bom":true}\n'
        b'{"last":2}\n'
    )

    rows, health = read_jsonl_lenient_with_health(
        path, keep_bad=True, loads=orjson.loads)

    assert rows == [{"first": 1}, None, None, None, {"last": 2}]
    assert health == {
        "read_complete": False,
        "source_lines": 4,
        "accepted_rows": 2,
        "invalid_lines": 2,
        "malformed_lines": 2,
        "invalid_shape_lines": 0,
    }


def test_lenient_health_splits_only_on_lf_not_bare_control_bytes(tmp_path):
    path = tmp_path / "bare-cr.jsonl"
    path.write_bytes(b'{"one":1}\r{"two":2}\n{"tail":3}')

    rows, health = read_jsonl_lenient_with_health(path, keep_bad=True)

    assert rows == [None, {"tail": 3}]
    assert health["source_lines"] == 2 and health["malformed_lines"] == 1


def test_lesson_consolidate_and_compact_preserve_quarantine_bytes(tmp_path):
    path = tmp_path / "lessons.jsonl"
    malformed = b"\xff{not-json"
    future = orjson.dumps({
        "v": 99, "record_kind": "future-lesson", "statement": "future contract",
    })
    valid = [_lesson("duplicate", "r1"), _lesson("duplicate", "r2")]
    path.write_bytes(b"\n".join([malformed, future, *(orjson.dumps(row) for row in valid)]) + b"\n")

    with _interprocess_lock(Path(str(path) + ".lock"), required=True):
        LessonMemory.consolidate_lessons_file(path)
    after_consolidate = path.read_bytes().splitlines()
    assert after_consolidate[:2] == [malformed, future]
    understood = read_jsonl_lenient(path)
    assert len([row for row in understood if "v" not in row]) == 1

    with path.open("ab") as f:
        for index in range(5):
            f.write(orjson.dumps(_lesson(f"retained-{index}", f"run-{index}")) + b"\n")
    with _interprocess_lock(Path(str(path) + ".lock"), required=True):
        LessonMemory.compact_lessons(path, max_lines=3, keep=2)
    after_compact = path.read_bytes().splitlines()
    assert after_compact[:2] == [malformed, future]
    current = [row for row in read_jsonl_lenient(path) if "v" not in row]
    assert [row["statement"] for row in current] == ["retained-3", "retained-4"]


def test_lesson_append_refuses_to_mutate_without_required_lock(tmp_path, monkeypatch):
    class _Engine:
        memory_dir = str(tmp_path)

    seen = []

    def _unavailable(path, *, required=False):
        from looplab.events.eventstore import EventStoreLockError

        seen.append(required)
        raise EventStoreLockError(path, OSError("locking unavailable"))

    monkeypatch.setattr("looplab.events.eventstore._interprocess_lock", _unavailable)
    with pytest.raises(Exception, match="locking unavailable"):
        LessonMemory(_Engine()).append_lessons([_lesson("must not land", "r")], hygiene=False)
    assert seen == [True]
    assert not (tmp_path / "lessons.jsonl").exists()


def test_claim_decision_holds_both_evidence_locks_through_fsync(tmp_path, monkeypatch):
    import threading

    from looplab.engine.claims import claim_evidence_digest, claims_for_memory, record_claim_decision

    lesson_path = tmp_path / "lessons.jsonl"
    lesson_path.write_bytes(orjson.dumps(_lesson("locked evidence", "r")) + b"\n")
    claim = claims_for_memory(tmp_path, structured=True)[0]
    active: set[str] = set()
    mutexes: dict[str, threading.Lock] = {}
    mutation_started = threading.Event()
    mutation_landed = threading.Event()
    mutation_thread = None

    @contextmanager
    def _tracked_lock(path, *, required=False):
        assert required is True
        key = str(path)
        mutex = mutexes.setdefault(key, threading.Lock())
        with mutex:
            active.add(key)
            try:
                yield
            finally:
                active.remove(key)

    fsync_observations = []

    def _observe_fsync(_fd):
        fsync_observations.append(set(active))

    monkeypatch.setattr("looplab.events.eventstore._interprocess_lock", _tracked_lock)
    monkeypatch.setattr("looplab.core.atomicio.strict_fsync", _observe_fsync)

    def _validate(snapshot):
        nonlocal mutation_thread
        current = next(row for row in snapshot if row["claim_uid"] == claim["claim_uid"])
        assert current["evidence_digest"] == claim_evidence_digest(claim)
        assert any(name.endswith("lessons.jsonl.lock") for name in active)
        assert any(name.endswith("research_claims.jsonl.lock") for name in active)

        def _concurrent_writer():
            mutation_started.set()
            with _tracked_lock(Path(str(lesson_path) + ".lock"), required=True):
                lesson_path.write_bytes(orjson.dumps(_lesson("new evidence", "r2")) + b"\n")
                mutation_landed.set()

        mutation_thread = threading.Thread(target=_concurrent_writer)
        mutation_thread.start()
        assert mutation_started.wait(1)
        assert not mutation_landed.wait(0.05)  # writer is fenced until decision fsync releases both sources

    record_claim_decision(
        tmp_path, statement=claim["statement"], scope="t", decision="ratified",
        evidence_digest=claim["evidence_digest"], validate_evidence=_validate,
    )
    assert fsync_observations
    assert any(name.endswith("lessons.jsonl.lock") for name in fsync_observations[-1])
    assert any(name.endswith("research_claims.jsonl.lock") for name in fsync_observations[-1])
    assert mutation_thread is not None
    mutation_thread.join(timeout=1)
    assert mutation_landed.is_set()


def test_claim_decision_evidence_lock_failure_appends_nothing(tmp_path, monkeypatch):
    from looplab.engine.claims import record_claim_decision
    from looplab.events.eventstore import EventStoreLockError

    @contextmanager
    def _lock(path, *, required=False):
        if str(path).endswith("lessons.jsonl.lock"):
            raise EventStoreLockError(path, OSError("evidence lock unavailable"))
        yield

    monkeypatch.setattr("looplab.events.eventstore._interprocess_lock", _lock)
    with pytest.raises(EventStoreLockError, match="evidence lock unavailable"):
        record_claim_decision(
            tmp_path, statement="x", decision="ratified",
            validate_evidence=lambda _snapshot: None,
        )
    decision_path = tmp_path / "claim_decisions.jsonl"
    assert not decision_path.exists() or not decision_path.read_bytes()
