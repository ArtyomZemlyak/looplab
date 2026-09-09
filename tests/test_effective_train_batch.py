"""THE BATCH THE PROCESS RAN AT, LIFTED INTO A DURABLE EVENT.

`docs/45-claim-surfaces-2026-08-20.md` §3.2 REFUSED `auto_find_batch_size` as the memory answer on a
measurement in the installed library: transformers 4.51.0 keeps the DECLARED
`per_device_train_batch_size` on `args` and writes the reduced one only into
`trainer_state.json::train_batch_size` (plus a `logger.debug` line), so a run that adopted it would
report a batch it never trained at — and LoopLab RANKS nodes, so two nodes whose recorded configs
differ only in batch could have trained at the same one. The refusal named its own lift: *"admissible
only if the effective batch is lifted into a durable LoopLab event"*.

This file drives both halves of that lift. The READER
(`runtime/effective_batch.py::bind_effective_train_batch`) runs over real workdirs on disk — the
agreeing case, the two-trainings case it refuses to settle, the previous attempt's stale artifact,
and every malformed shape a candidate can write. The RECORD half runs a real engine to completion
and reads its `events.jsonl`: the row is there, it is DIAGNOSTIC (the fold cannot move a metric on
it), and a node with no such artifact leaves no row at all — which is the state every task on this
box that is not a transformers training is in permanently.

Nothing here is a source pin: the property is "the number on disk reaches the log, and only when it
exists", which no substring can establish.
"""
from __future__ import annotations

import json
import time

import anyio
import pytest

from factories import make_engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import DIAGNOSTIC_EVENTS, EV_EFFECTIVE_TRAIN_BATCH
from looplab.runtime.effective_batch import (MAX_STATE_FILES, TRAINER_STATE_NAME,
                                             bind_effective_train_batch)
from looplab.runtime.sandbox import RunResult


def _state(workdir, rel: str, **fields) -> None:
    """One `trainer_state.json` where a trainer would have written it (a checkpoint dir)."""
    path = workdir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"global_step": 10, **fields}), encoding="utf-8")


# --------------------------------------------------------------------------- the reader
def test_the_process_number_wins_over_the_declared_one(tmp_path):
    """THE ITEM, in miniature: the config keeps 8192 and the trainer's own state says 1024. The
    record is about the second one — that is the whole reason it exists beside `applied_params`."""
    (tmp_path / "config.yaml").write_text("batch_size: 8192\n", encoding="utf-8")
    _state(tmp_path, f"out/checkpoint-500/{TRAINER_STATE_NAME}", train_batch_size=1024)

    record = bind_effective_train_batch(tmp_path)
    assert record["train_batch_size"] == 1024
    assert record["disagree"] is False
    assert [r["path"] for r in record["readings"]] == [f"out/checkpoint-500/{TRAINER_STATE_NAME}"]
    assert record["readings"][0]["global_step"] == 10
    assert record["readings"][0]["digest"], "the reading must name the bytes it was taken from"


def test_agreeing_checkpoints_settle_one_number(tmp_path):
    """A training writes one state file per checkpoint; they are one fact, not five."""
    for step in (100, 200, 300):
        _state(tmp_path, f"out/checkpoint-{step}/{TRAINER_STATE_NAME}",
               train_batch_size=512, global_step=step)
    record = bind_effective_train_batch(tmp_path)
    assert record["train_batch_size"] == 512 and record["disagree"] is False
    assert len(record["readings"]) == 3


def test_two_trainings_are_two_facts_and_neither_is_picked(tmp_path):
    """A pipeline legitimately trains twice (a mine stage and a train stage each resolve their own).
    Picking one would record a number nobody chose — `applied_params`' conflict rule, and the reason
    the scalar is withheld rather than tie-broken on `global_step`."""
    _state(tmp_path, f"mine/{TRAINER_STATE_NAME}", train_batch_size=256, global_step=99999)
    _state(tmp_path, f"train/{TRAINER_STATE_NAME}", train_batch_size=1024, global_step=10)

    record = bind_effective_train_batch(tmp_path)
    assert record["train_batch_size"] is None
    assert record["disagree"] is True
    assert sorted(r["train_batch_size"] for r in record["readings"]) == [256, 1024]


def test_a_previous_attempts_state_file_is_not_this_attempts(tmp_path):
    """FRESHNESS. A `trainer_state.json` is by definition something THIS attempt's process produced,
    so one that predates the attempt floor belongs to the attempt before it. Same rule, same
    `bind_one` call, as the resolved-config tier."""
    _state(tmp_path, f"out/{TRAINER_STATE_NAME}", train_batch_size=64)
    old = time.time() - 10_000
    import os
    os.utime(tmp_path / "out" / TRAINER_STATE_NAME, (old, old))

    assert bind_effective_train_batch(tmp_path, since=time.time()) is None      # stale -> silence
    assert bind_effective_train_batch(tmp_path, since=old - 100)["train_batch_size"] == 64


def test_absence_is_silence_and_never_an_empty_record(tmp_path):
    """`None`, not `{}`: "the trainer never said" and "the trainer said nothing changed" are opposite
    claims, and every task here that is not a transformers training is in the first one forever."""
    (tmp_path / "solution.py").write_text("print(1)\n", encoding="utf-8")
    assert bind_effective_train_batch(tmp_path) is None


@pytest.mark.parametrize("value", [True, False, 0, -8, "1024", 1024.5, None, {"n": 8}])
def test_a_malformed_batch_is_not_a_reading(tmp_path, value):
    """The candidate writes these bytes. `True` is the sharp one — `bool` is an `int` subclass, so
    `train_batch_size: true` would otherwise be recorded as a batch of 1."""
    _state(tmp_path, f"out/{TRAINER_STATE_NAME}", train_batch_size=value)
    assert bind_effective_train_batch(tmp_path) is None


def test_an_unreadable_state_file_costs_the_record_and_not_the_node(tmp_path):
    """Total over anything a filesystem or a malformed document can do — a record may never cost a
    node its terminal, which is `metric_inputs`' rule and `metric_salvage`'s before it."""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / TRAINER_STATE_NAME).write_text("{not json", encoding="utf-8")
    assert bind_effective_train_batch(tmp_path) is None

    _state(tmp_path, f"good/{TRAINER_STATE_NAME}", train_batch_size=32)
    record = bind_effective_train_batch(tmp_path)          # …and one bad file does not hide a good one
    assert record["train_batch_size"] == 32 and record["files_seen"] == 2
    assert len(record["readings"]) == 1


def test_the_file_bound_under_reports_rather_than_lying(tmp_path):
    """Over `MAX_STATE_FILES` the rest are unread, which can only ever UNDER-report a disagreement —
    the direction every rung here fails in — and the record says so instead of implying a total."""
    for step in range(MAX_STATE_FILES + 5):
        _state(tmp_path, f"out/checkpoint-{step:04d}/{TRAINER_STATE_NAME}", train_batch_size=8)
    record = bind_effective_train_batch(tmp_path)
    assert record["truncated"] is True
    assert len(record["readings"]) == MAX_STATE_FILES
    assert record["train_batch_size"] == 8


# --------------------------------------------------------------------------- the durable record
def _rows(run_dir) -> list:
    return [e.data for e in EventStore(run_dir / "events.jsonl").read_all()
            if e.type == EV_EFFECTIVE_TRAIN_BATCH]


def _run_with(run_dir, record):
    """A real run whose eval reports `record` as what its process ran at."""
    engine = make_engine(run_dir, n_seeds=1, max_nodes=1)

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None,
                      start_stage=None):
        return RunResult(exit_code=0, stdout="", stderr="", metric=1.0, timed_out=False,
                         effective_train_batch=record)

    engine._run_eval = fake_run_eval
    state = anyio.run(engine.run)
    assert state.evaluated_nodes(), "the run did not evaluate a node"
    return state


def test_the_reading_reaches_the_durable_log(tmp_path):
    """THE LIFT. Against the pre-change tree this asserts `[] != []`: the number was on disk, in the
    result object, and in no event anyone could read back."""
    reading = {"path": f"out/{TRAINER_STATE_NAME}", "train_batch_size": 1024,
               "global_step": 10, "digest": "abc"}
    _run_with(tmp_path / "run", {"readings": [reading], "train_batch_size": 1024,
                                 "disagree": False, "files_seen": 1, "truncated": False})
    rows = _rows(tmp_path / "run")
    assert len(rows) == 1
    assert rows[0]["train_batch_size"] == 1024 and rows[0]["disagree"] is False
    assert rows[0]["readings"] == [reading]
    assert rows[0]["node_id"] == 0 and rows[0]["read_at"] > 0


def test_a_node_with_no_trainer_artifact_leaves_no_row(tmp_path):
    """The row is withheld rather than written empty — unlike the trust-scan receipt beside it, whose
    "we looked and it was clean" is the load-bearing claim. Here a row per node would be an unbounded
    log recording that this deployment does not use HuggingFace."""
    _run_with(tmp_path / "run", None)
    assert _rows(tmp_path / "run") == []


def test_the_row_is_diagnostic_and_moves_no_selection(tmp_path):
    """Invariant 1's condition for a row an eval WORKER may append, and invariant 5's for the fold:
    folding the log with and without it gives the same state."""
    assert EV_EFFECTIVE_TRAIN_BATCH in DIAGNOSTIC_EVENTS
    state = _run_with(tmp_path / "run", {"readings": [{"path": "p", "train_batch_size": 8,
                                                       "global_step": 1, "digest": "d"}],
                                         "train_batch_size": 8, "disagree": False,
                                         "files_seen": 1, "truncated": False})
    events = EventStore(tmp_path / "run" / "events.jsonl").read_all()
    without = fold([e for e in events if e.type != EV_EFFECTIVE_TRAIN_BATCH])
    assert fold(events).model_dump(mode="json") == without.model_dump(mode="json")
    assert state.best_node_id == without.best_node_id
