"""The read model says what it covers, and every uncertainty answers "not current".

These drive the property rather than pinning source: each test builds a REAL projection over a REAL
event log with `EventStore`/`fold`/`sqlite3`, then asks the shipped reader what it sees. A comment
carrying the word "watermark" satisfies none of them.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.events.eventstore import EventStore
from looplab.events.readmodel import (
    READMODEL_SCHEMA_VERSION,
    READMODEL_STATUSES,
    STATUS_CURRENT,
    STATUS_STALE,
    STATUS_UNKNOWN,
    WATERMARK_TABLE,
    ReadModelWatermark,
    build_readmodel,
    coverage_watermark,
    publish_readmodel,
    read_watermark,
    readmodel_is_current,
    readmodel_status,
)

# The preserved corpus is not part of the repo. Where it exists, the back-compat claim is checked
# against a file an OLDER LoopLab actually wrote, which no synthetic fixture can stand in for.
_CORPUS = Path(__file__).resolve().parents[1] / "runs"


def _corpus_readmodels() -> list[Path]:
    if not _CORPUS.is_dir():
        return []
    try:
        return sorted(p for p in _CORPUS.glob("*/readmodel.sqlite") if p.is_file())
    except OSError:
        return []


def _run_log(run_dir: Path) -> EventStore:
    """A real event log with a real evaluated node, so the projection has rows to be stale about."""
    run_dir.mkdir(parents=True, exist_ok=True)
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"x": 1.0}}, "code": ""})
    store.append("node_evaluated", {"node_id": 0, "metric": 0.5, "violations": []})
    return store


def test_a_freshly_built_model_is_current_and_carries_the_prefix_it_folded(tmp_path):
    store = _run_log(tmp_path / "run")
    events = store.read_all()
    db = tmp_path / "run" / "readmodel.sqlite"
    publish_readmodel(events, db)

    assert readmodel_status(db, events) == STATUS_CURRENT
    assert readmodel_is_current(db, events)
    wm = read_watermark(db)
    assert wm is not None
    assert wm.schema_version == READMODEL_SCHEMA_VERSION
    assert wm.event_count == len(events)
    assert wm.covered_seq == max(e.seq for e in events)
    # The rows are still the rows: nothing about the watermark changed what the projection serves.
    with sqlite3.connect(str(db)) as con:
        assert con.execute("SELECT id, is_best FROM nodes").fetchall() == [(0, 1)]


def test_an_appended_event_is_detected_not_silently_served(tmp_path):
    """The C5 case: a control event lands after the model was published."""
    store = _run_log(tmp_path / "run")
    db = tmp_path / "run" / "readmodel.sqlite"
    publish_readmodel(store.read_all(), db)

    store.append("pause", {"reason": "operator"})
    after = store.read_all()
    assert len(after) == 4

    assert readmodel_status(db, after) == STATUS_STALE
    assert not readmodel_is_current(db, after)
    # ...and the stored watermark still honestly names the SHORTER prefix it really covers.
    assert read_watermark(db).event_count == 3


def test_the_negative_control_an_untouched_model_is_never_reported_stale(tmp_path):
    """Repeated reads of an unchanged log must not drift into `stale` — the false-alarm direction."""
    store = _run_log(tmp_path / "run")
    db = tmp_path / "run" / "readmodel.sqlite"
    publish_readmodel(store.read_all(), db)
    for _ in range(3):
        assert readmodel_status(db, store.read_all()) == STATUS_CURRENT
    # A rebuild over the grown log returns to `current` rather than staying stuck stale.
    store.append("pause", {"reason": "operator"})
    assert readmodel_status(db, store.read_all()) == STATUS_STALE
    publish_readmodel(store.read_all(), db)
    assert readmodel_status(db, store.read_all()) == STATUS_CURRENT


def test_a_rewrite_preserving_count_and_max_seq_is_still_stale(tmp_path):
    """Why the watermark carries a digest and not just `(count, max_seq)`.

    A heal-truncate that replaces a row keeps both of those numbers. Comparing them alone would
    certify a model built from a DIFFERENT prefix as current.
    """
    store = _run_log(tmp_path / "run")
    events = store.read_all()
    db = tmp_path / "run" / "readmodel.sqlite"
    publish_readmodel(events, db)

    rewritten = list(events)
    rewritten[1] = rewritten[1].model_copy(update={"type": "node_failed"})
    assert len(rewritten) == len(events)
    assert max(e.seq for e in rewritten) == max(e.seq for e in events)
    assert readmodel_status(db, rewritten) == STATUS_STALE


def test_a_model_written_before_the_watermark_existed_still_loads_and_is_never_current(tmp_path):
    """Back-compat + fail-closed at once: old rows readable, old coverage unknowable."""
    store = _run_log(tmp_path / "run")
    events = store.read_all()
    db = tmp_path / "legacy.sqlite"
    # Exactly the pre-2026-08-14 on-disk shape: the nodes table and nothing else.
    build_readmodel(events, db)
    with sqlite3.connect(str(db)) as con:
        con.execute(f"DROP TABLE {WATERMARK_TABLE}")

    with sqlite3.connect(str(db)) as con:  # the projection still serves its rows unchanged
        assert con.execute("SELECT id, status FROM nodes").fetchall() == [(0, "evaluated")]
    assert read_watermark(db) is None
    assert readmodel_status(db, events) == STATUS_UNKNOWN
    assert not readmodel_is_current(db, events)  # never `current` by default


@pytest.mark.skipif(not _corpus_readmodels(), reason="no preserved runs/*/readmodel.sqlite on this box")
def test_preserved_corpus_readmodels_load_and_report_unknown():
    """Against artefacts an OLDER LoopLab really wrote, in `runs/`. Read-only."""
    checked = 0
    for db in _corpus_readmodels():
        with sqlite3.connect("file:" + str(db) + "?mode=ro", uri=True) as con:
            con.execute("SELECT id, metric, status, is_best FROM nodes").fetchall()
        assert read_watermark(db) is None      # no watermark -> unknowable, not assumed current
        assert readmodel_status(db, []) == STATUS_UNKNOWN
        assert not readmodel_is_current(db, [])
        checked += 1
    assert checked


def test_an_absent_model_is_unknown_and_the_reader_does_not_create_one(tmp_path):
    """`sqlite3.connect(path)` CREATES a database. A reader that did would turn "no read model"
    into "an empty read model" — a projection claiming zero nodes for a run that has some."""
    missing = tmp_path / "nope.sqlite"
    assert read_watermark(missing) is None
    assert readmodel_status(missing, []) == STATUS_UNKNOWN
    assert not missing.exists()


def test_a_corrupt_or_ambiguous_watermark_fails_closed(tmp_path):
    store = _run_log(tmp_path / "run")
    events = store.read_all()

    not_a_db = tmp_path / "garbage.sqlite"
    not_a_db.write_bytes(b"this is not a sqlite file" * 64)
    assert readmodel_status(not_a_db, events) == STATUS_UNKNOWN

    two_rows = tmp_path / "two.sqlite"
    build_readmodel(events, two_rows)
    assert readmodel_status(two_rows, events) == STATUS_CURRENT
    with sqlite3.connect(str(two_rows)) as con:
        con.execute(
            f"INSERT INTO {WATERMARK_TABLE} VALUES (?,?,?,?,?)",
            (READMODEL_SCHEMA_VERSION, 99, 99, "rmcov1:deadbeef", 0.0))
    assert readmodel_status(two_rows, events) == STATUS_UNKNOWN  # ambiguous, so unusable

    other_version = tmp_path / "v9.sqlite"
    build_readmodel(events, other_version)
    with sqlite3.connect(str(other_version)) as con:
        con.execute(f"UPDATE {WATERMARK_TABLE} SET schema_version = ?",
                    (READMODEL_SCHEMA_VERSION + 1,))
    assert readmodel_status(other_version, events) == STATUS_UNKNOWN

    blank_digest = tmp_path / "blank.sqlite"
    build_readmodel(events, blank_digest)
    with sqlite3.connect(str(blank_digest)) as con:
        con.execute(f"UPDATE {WATERMARK_TABLE} SET digest = ''")
    assert readmodel_status(blank_digest, events) == STATUS_UNKNOWN


def test_an_empty_coverage_digest_can_never_certify_anything():
    """The `same_coverage` guard, stated as a truth table rather than reached through a build."""
    blank = ReadModelWatermark(READMODEL_SCHEMA_VERSION, 3, 4, "")
    assert not blank.same_coverage(blank)
    assert not blank.same_coverage(ReadModelWatermark(READMODEL_SCHEMA_VERSION, 3, 4, "rmcov1:a"))
    good = ReadModelWatermark(READMODEL_SCHEMA_VERSION, 3, 4, "rmcov1:a")
    assert good.same_coverage(ReadModelWatermark(READMODEL_SCHEMA_VERSION, 3, 4, "rmcov1:a", 9.0))
    assert not good.same_coverage(None)
    assert not good.same_coverage(ReadModelWatermark(READMODEL_SCHEMA_VERSION, 4, 4, "rmcov1:a"))


def test_coverage_watermark_of_an_empty_log_is_comparable_not_absent():
    wm = coverage_watermark([])
    assert wm is not None and wm.event_count == 0 and wm.covered_seq == -1
    assert STATUS_UNKNOWN in READMODEL_STATUSES and STATUS_STALE in READMODEL_STATUSES


# ----------------------------------------------------------------- the live/crashed-run half (CLI)

def test_cli_builds_a_read_model_for_a_run_that_never_finalized(tmp_path):
    """The operator-visible gap: a still-running or crashed run has no readmodel.sqlite at all."""
    run_dir = tmp_path / "run"
    store = _run_log(run_dir)
    db = run_dir / "readmodel.sqlite"
    assert not db.exists()

    stale_first = CliRunner().invoke(app, ["readmodel", str(run_dir), "--check"])
    assert stale_first.exit_code == 1              # absent is not current
    assert "status=unknown" in stale_first.stdout

    built = CliRunner().invoke(app, ["readmodel", str(run_dir)])
    assert built.exit_code == 0, built.stdout
    assert db.exists()
    assert readmodel_is_current(db, store.read_all())

    checked = CliRunner().invoke(app, ["readmodel", str(run_dir), "--check"])
    assert checked.exit_code == 0 and "status=current" in checked.stdout

    # The run keeps going: the published snapshot is a PREFIX and says so.
    store.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                                  "idea": {"operator": "improve", "params": {"x": 2.0}}})
    after = CliRunner().invoke(app, ["readmodel", str(run_dir), "--check"])
    assert after.exit_code == 1 and "status=stale" in after.stdout


def test_cli_writes_no_event_and_the_engine_still_folds_the_log(tmp_path):
    """Invariant #1 and #4 in one drive: the sidecar appends nothing and changes no folded state."""
    from looplab.events.replay import fold

    run_dir = tmp_path / "run"
    store = _run_log(run_dir)
    before = store.read_all()
    before_state = fold(before).model_dump(mode="json")

    assert CliRunner().invoke(app, ["readmodel", str(run_dir)]).exit_code == 0

    after = EventStore(run_dir / "events.jsonl").read_all()
    assert [(e.seq, e.type) for e in after] == [(e.seq, e.type) for e in before]
    assert fold(after).model_dump(mode="json") == before_state


def test_cli_refuses_to_write_into_a_fenced_run(tmp_path):
    """A second process must not resurrect a sidecar under a deletion/reset transaction."""
    from looplab.core.run_deletion import publish_run_deletion_fence

    run_dir = tmp_path / "run"
    _run_log(run_dir)
    publish_run_deletion_fence(
        run_dir, operation_id="11111111-2222-3333-4444-555555555555",
        expected_generation="0" * 64, expected_seq=2, receipt_name="r.json")

    result = CliRunner().invoke(app, ["readmodel", str(run_dir)])
    assert result.exit_code == 2
    assert not (run_dir / "readmodel.sqlite").exists()


def test_cli_refuses_when_the_fence_itself_cannot_be_read(tmp_path):
    """A malformed fence is not an absent one: the writer fails closed on an unreadable marker."""
    from looplab.core.run_deletion import run_deletion_fence_path

    run_dir = tmp_path / "run"
    _run_log(run_dir)
    run_deletion_fence_path(run_dir).write_text("{not json", encoding="utf-8")

    result = CliRunner().invoke(app, ["readmodel", str(run_dir)])
    assert result.exit_code == 2
    assert not (run_dir / "readmodel.sqlite").exists()
