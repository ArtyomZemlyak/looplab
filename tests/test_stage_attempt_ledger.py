"""The per-attempt stage ledger (doc 52 row 27; BACKLOG §6 D5).

`Node.stages` is the per-NAME projection — last-wins by stage name, `repairs`-stamped — and after an
inline repair it cannot show the attempt the repair replaced: the attempt that spent the training
wall-clock left no row at all. `Node.stage_attempts` keeps every `stage_finished` row as the
attempt's own statement, and `Node.stage_wall_clock()` sums it. Every test folds a real log.
"""
from __future__ import annotations

from looplab.core.models import Event
from looplab.events.replay import fold

_IDEA = {"operator": "draft", "params": {}, "rationale": ""}


def _fold(rows):
    events = [Event(seq=0, ts=0.0, type="run_started",
                    data={"run_id": "r", "task_id": "t", "direction": "min"}),
              Event(seq=1, ts=1.0, type="node_created",
                    data={"node_id": 0, "parent_ids": [], "operator": "draft", "idea": _IDEA})]
    for i, (kind, data) in enumerate(rows, start=2):
        events.append(Event(seq=i, ts=float(i), type=kind, data={"node_id": 0, **data}))
    return fold(events).nodes[0]


def _stage(name, status, seconds, exit_code=0, generation=0):
    return ("stage_finished", {"name": name, "status": status, "exit_code": exit_code,
                               "seconds": seconds, "generation": generation})


def test_a_repaired_attempt_keeps_its_wall_clock_in_the_ledger_and_loses_it_in_the_projection():
    node = _fold([
        _stage("mine", "ok", 30.0),
        _stage("train", "failed", 7200.0, exit_code=1),
        ("node_repaired", {"attempt": 1, "code": "fixed\n"}),
        _stage("mine", "reused", 0.0),
        _stage("train", "ok", 5400.0),
    ])
    # The projection every surface reads: one row per name, the passing attempt, at epoch 1.
    assert [(s["name"], s["status"], s["seconds"], s["repairs"]) for s in node.stages] == [
        ("mine", "ok", 30.0, 1), ("train", "ok", 5400.0, 1)]
    # The ledger: every attempt, its own epoch, in log order — the two failed hours are on a row.
    assert [(s["name"], s["status"], s["seconds"], s["repairs"], s["seq"])
            for s in node.stage_attempts] == [
        ("mine", "ok", 30.0, 0, 2), ("train", "failed", 7200.0, 0, 3),
        ("mine", "reused", 0.0, 1, 5), ("train", "ok", 5400.0, 1, 6)]
    assert node.stage_wall_clock() == {
        "mine": {"attempts": 2, "seconds": 30.0, "reused": 1, "generations": [0]},
        "train": {"attempts": 2, "seconds": 12600.0, "reused": 0, "generations": [0]},
    }


def test_the_d5_shape_a_reused_row_after_a_real_one_is_two_attempts_and_one_run():
    # BACKLOG §6 D5's reported shape: a checkpoint-reuse re-eval after a real train. The projection
    # keeps the informative row (the 2026-08-07 guard); the ledger holds both attempts.
    node = _fold([_stage("train", "ok", 6900.0),
                  ("node_repaired", {"attempt": 1, "code": "fixed\n"}),
                  _stage("train", "reused", 0.0)])
    assert [(s["status"], s["seconds"]) for s in node.stages] == [("ok", 6900.0)]
    assert [(s["status"], s["seconds"], s["repairs"]) for s in node.stage_attempts] == [
        ("ok", 6900.0, 0), ("reused", 0.0, 1)]
    assert node.stage_wall_clock()["train"] == {
        "attempts": 2, "seconds": 6900.0, "reused": 1, "generations": [0]}


def test_the_ledger_survives_a_reset_that_clears_the_projection():
    node = _fold([_stage("train", "failed", 120.0, exit_code=1),
                  ("node_reset", {"generation": 0}),
                  _stage("train", "ok", 90.0, generation=1)])
    assert node.attempt == 1, "precondition: the reset opened a new lifecycle (`Node.attempt`)"
    assert [(s["status"], s["repairs"]) for s in node.stages] == [("ok", 0)]
    assert [(s["status"], s["generation"]) for s in node.stage_attempts] == [
        ("failed", 0), ("ok", 1)]
    assert node.stage_wall_clock()["train"] == {
        "attempts": 2, "seconds": 210.0, "reused": 0, "generations": [0, 1]}


def test_a_row_from_an_abandoned_lifecycle_reaches_neither_the_projection_nor_the_ledger():
    node = _fold([("node_reset", {"generation": 0}),
                  _stage("train", "ok", 50.0, generation=0),     # the abandoned worker's late row
                  _stage("train", "ok", 60.0, generation=1)])
    assert [s["seconds"] for s in node.stages] == [60.0]
    assert [s["seconds"] for s in node.stage_attempts] == [60.0]


def test_a_legacy_log_folds_to_an_empty_ledger_and_an_empty_clock():
    node = _fold([])
    assert node.stage_attempts == [] and node.stage_wall_clock() == {}
    malformed = _fold([("stage_finished", {"name": None, "status": "ok", "seconds": "fast"})])
    assert malformed.stage_wall_clock() == {}, "a nameless row is counted by nothing"
