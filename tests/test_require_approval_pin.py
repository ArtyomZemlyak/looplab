"""`require_approval` is pinned into `run_started` (engine invariant #6, 2026-09-06).

The HITL gate was the one setting that gates a PAID finish and the one invariant #6 did not cover:
`cli/__init__.py::load_run_settings(require_snapshot=True)` refuses a DELETED snapshot on a run
that has a log, but an EDITED one — `require_approval: false` written into `config.snapshot.json`
of a paused approval-pending run — was read live off `Settings` and finished the run unapproved.
Now the record wins in both directions, and a log written before the pin keeps its snapshot's
value, because folding "absent" to False would turn the gate OFF on the resume of every older
approval-pending run — the exact defect the pin exists to stop.

Driven, not pinned: each test starts a real Toy run, reopens the SAME run directory with the live
value changed underneath it, and reads what the resumed engine does.
"""
from __future__ import annotations

import json
from pathlib import Path

import anyio

from looplab.adapters.toytask import ToyTask
from looplab.core.config import RUN_START_PINNED_FIELDS, run_start_pinned_settings
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

TASK = Path(__file__).resolve().parents[1] / "examples" / "toy_task.json"


def _engine(rd, **kw):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    return Engine(rd, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=2, max_nodes=4), **kw)


def _started(rd):
    return next(e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "run_started")


def test_the_pin_is_in_the_registry():
    assert "require_approval" in RUN_START_PINNED_FIELDS


def test_a_recorded_gate_wins_over_a_snapshot_edited_to_drop_it(tmp_path):
    """THE DEFECT. MUTATION: drop the re-pin in `Engine._reentry_repin` -> the resumed engine keeps
    its live False and finishes the paused run with no approval."""
    rd = tmp_path / "run"
    s1 = anyio.run(_engine(rd, require_approval=True).run)
    assert s1.awaiting_approval and not s1.finished
    assert _started(rd).data["require_approval"] is True
    assert s1.require_approval is True
    assert run_start_pinned_settings(s1)["require_approval"] is True

    # The snapshot edited to `require_approval: false`: the re-entry engine is built from it.
    resumed = _engine(rd, require_approval=False)
    assert resumed.require_approval is False            # the live value, before the re-pin
    s2 = anyio.run(resumed.run)
    assert resumed.require_approval is True, "the record must win over the edited snapshot"
    assert s2.awaiting_approval and not s2.finished, "the run finished without an approval"


def test_a_recorded_false_wins_over_a_snapshot_edited_to_add_the_gate(tmp_path):
    """Both values are a record. A run launched WITHOUT the gate finished; reopened with the live
    value flipped to True, the engine adopts the recorded False rather than the edit."""
    rd = tmp_path / "run"
    s1 = anyio.run(_engine(rd, require_approval=False).run)
    assert s1.finished and not s1.awaiting_approval
    assert _started(rd).data["require_approval"] is False
    assert run_start_pinned_settings(s1)["require_approval"] is False

    resumed = _engine(rd, require_approval=True)
    assert resumed.require_approval is True
    anyio.run(resumed.run)
    assert resumed.require_approval is False


def test_a_log_written_before_the_pin_keeps_the_snapshots_value(tmp_path):
    """LEGACY. A `run_started` with no `require_approval` key folds None, is reported by
    `run_start_pinned_settings` as NOT pinned, and the live (snapshot) value is kept. MUTATION: fold
    absent to False -> the older approval-pending run below resumes with the gate off."""
    rd = tmp_path / "run"
    s1 = anyio.run(_engine(rd, require_approval=True).run)
    assert s1.awaiting_approval and not s1.finished
    # Rewrite the log as the pre-2026-09-06 writer would have emitted it.
    path = rd / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    for row in rows:
        if row["type"] == "run_started":
            row["data"].pop("require_approval")
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    legacy = fold(EventStore(path).read_all())
    assert legacy.require_approval is None
    assert "require_approval" not in run_start_pinned_settings(legacy)

    resumed = _engine(rd, require_approval=True)
    s2 = anyio.run(resumed.run)
    assert resumed.require_approval is True
    assert s2.awaiting_approval and not s2.finished


def test_only_a_json_boolean_is_a_record(tmp_path):
    """A hand-edited row carrying `"yes"` or `1` must not become a pin either way."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min",
                                 "require_approval": "yes"})
    state = fold(store.read_all())
    assert state.require_approval is None
    assert "require_approval" not in run_start_pinned_settings(state)
