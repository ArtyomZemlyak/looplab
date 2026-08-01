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

    row = {**_lesson("numeric-string evidence", "legacy-run"), "evidence": ["42", "7"]}
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(row) + b"\n")

    claims = claims_for_memory(tmp_path, structured=True)

    assert claims[0]["support"] == ["legacy-run:42", "legacy-run:7"]
    assert claims.claim_source["source_complete"] is True

    # A SIGNED numeric string is not part of that legacy compatibility: a node id indexes the run's
    # node table, so `-42` would qualify into a citation to a node that cannot exist. It quarantines
    # the row and says so in the receipt instead of being coerced like an unsigned one.
    signed = tmp_path / "signed"
    signed.mkdir()
    (signed / "lessons.jsonl").write_bytes(
        orjson.dumps({**_lesson("signed evidence", "legacy-run"), "evidence": ["-42"]}) + b"\n")
    rejected = claims_for_memory(signed, structured=True)
    assert list(rejected) == []
    assert rejected.claim_source["source_complete"] is False


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

    # Called with NO lock held — hygiene takes `lessons.jsonl.lock` itself now, because the
    # paraphrase pass inside it is a provider call and must not run under a lock every concurrent
    # run's lesson writes queue behind. Holding it here would self-deadlock.
    LessonMemory.consolidate_lessons_file(path)
    after_consolidate = path.read_bytes().splitlines()
    assert after_consolidate[:2] == [malformed, future]
    understood = read_jsonl_lenient(path)
    assert len([row for row in understood if "v" not in row]) == 1

    with path.open("ab") as f:
        for index in range(5):
            f.write(orjson.dumps(_lesson(f"retained-{index}", f"run-{index}")) + b"\n")
    LessonMemory.compact_lessons(path, max_lines=3, keep=2)      # ditto: takes the lock itself
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


def test_an_unwritable_shared_store_degrades_the_append_instead_of_failing_the_run(
        tmp_path, monkeypatch):
    """The SHARED lessons store lives on a DIFFERENT filesystem from the run dir, so a read-only /
    full / quota'd mount raises OSError from the append while the run's own events.jsonl append
    moments earlier succeeded. Unguarded that propagated out of `maybe_distill_lessons` through
    `_run_cadences` into the run() spine and FAILED the run — and since the EV_LESSONS_DISTILLED
    gate had already advanced, every LATER distill cadence re-crashed the same way."""
    from looplab.events.types import EV_LESSONS_STORE_UNAVAILABLE

    appended = []

    class _Store:
        def append(self, type_, data):
            appended.append((type_, data))

    class _Engine:
        memory_dir = str(tmp_path)
        store = _Store()

    def _no_space(path, payload):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("looplab.engine.lessons.append_jsonl_bytes_locked", _no_space)
    LessonMemory(_Engine()).append_lessons([_lesson("lost to a full mount", "r")], hygiene=False)

    assert [t for t, _ in appended] == [EV_LESSONS_STORE_UNAVAILABLE]     # disclosed, not silent
    assert appended[0][1]["mode"] == "write" and appended[0][1]["count"] == 1
    assert "No space left" in appended[0][1]["error"]


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


# --------------------------------------------------------------------------- #
# Hygiene must not hold the shared store's lock across a provider call
# --------------------------------------------------------------------------- #
# `lessons.jsonl.lock` gates a file every concurrent run appends to. The paraphrase-merge pass
# inside consolidation is a paid model call, and it used to run with that lock held: one slow — or
# hung — provider froze every other run's lesson writes, and the governed readers behind them, for
# the model's whole latency. The pass now snapshots under the lock, pays for the merge unlocked, and
# re-acquires to compare-and-swap before the rewrite.

def _lock_is_free(path: Path) -> bool:
    """Can an INDEPENDENT holder take lessons.jsonl.lock right now? flock is per open-file
    description, so a second open() in this process is a faithful stand-in for another run."""
    import fcntl
    with open(str(path) + ".lock", "a+") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return True


def test_paid_paraphrase_merge_does_not_run_under_the_shared_store_lock(tmp_path, monkeypatch):
    """Exercised through the real writer, `append_lessons(hygiene=True)` — that is the caller that
    used to hold the lock for the whole append-plus-hygiene block."""
    class _Engine:
        memory_dir = str(tmp_path)
        researcher = developer = None
        _embedder = None
        _reflect_client = staticmethod(lambda: object())
        _consolidate_lessons_file = staticmethod(LessonMemory.consolidate_lessons_file)
        _compact_lessons = staticmethod(LessonMemory.compact_lessons)

    path = tmp_path / "lessons.jsonl"
    path.write_bytes(orjson.dumps(_lesson("dup", "r1")) + b"\n")
    observed = []

    def _merge(rows, **kw):
        observed.append(_lock_is_free(path))     # this stands in for the provider call
        return rows[:1]                          # fewer rows -> a rewrite is attempted

    monkeypatch.setattr("looplab.engine.memory.consolidate_lessons", _merge)
    LessonMemory(_Engine()).append_lessons([_lesson("dup", "r2")], hygiene=True)
    assert observed == [True]                    # was False: the lock was held across the call
    assert len(read_jsonl_lenient(path)) == 1    # ...and the merge still landed


def test_a_concurrent_append_during_the_merge_is_never_clobbered(tmp_path, monkeypatch):
    """Paying for the merge unlocked opens a window another run can append into. The rewrite then
    compare-and-swaps: a moved store means the merged snapshot is stale and must be DROPPED, or the
    whole-file replace erases the append — the exact loss the lock exists to prevent."""
    path = tmp_path / "lessons.jsonl"
    path.write_bytes(b"\n".join(orjson.dumps(_lesson("dup", r)) for r in ("r1", "r2")) + b"\n")

    def _merge(rows, **kw):
        with path.open("ab") as f:               # another run appends while the model is thinking
            f.write(orjson.dumps(_lesson("from a concurrent run", "r3")) + b"\n")
        return rows[:1]

    monkeypatch.setattr("looplab.engine.memory.consolidate_lessons", _merge)
    LessonMemory.consolidate_lessons_file(path, client=object())
    statements = [row["statement"] for row in read_jsonl_lenient(path)]
    assert "from a concurrent run" in statements     # survived: the stale rewrite was declined
    assert len(statements) == 3


def test_compaction_takes_the_lock_itself(tmp_path, monkeypatch):
    """It is a read-modify-write of a shared file and used to inherit its caller's lock. Now that
    the caller releases before hygiene, compaction has to hold the lock on its own or a concurrent
    append lands in its read->write window and is replaced away."""
    path = tmp_path / "lessons.jsonl"
    path.write_bytes(b"\n".join(orjson.dumps(_lesson(f"s{i}", f"r{i}")) for i in range(5)) + b"\n")
    held = []
    from looplab.events import eventstore
    original = eventstore.replace_jsonl_rows_atomic_preserving_quarantine

    def _probe(p, rows, **kw):
        held.append(_lock_is_free(path))
        return original(p, rows, **kw)

    monkeypatch.setattr(eventstore, "replace_jsonl_rows_atomic_preserving_quarantine", _probe)
    LessonMemory.compact_lessons(path, max_lines=3, keep=2)
    assert held == [False]                        # the rewrite ran with the lock held
    assert len(read_jsonl_lenient(path)) == 2
