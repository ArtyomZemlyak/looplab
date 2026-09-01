"""The LIVE STAGE CURSOR: which step of an evaluation is running right now.

`stage_finished` is folded but lands only at a stage's COMPLETION, so between two rows the durable
record could not say whether a node was mining, training or scoring. Everything downstream guessed:
`train_monitor.resolve_stage_log` picked the freshest-mtime log (its own docstring concedes the
cursor "genuinely is unobservable from here") and every UI status surface called the whole
multi-hour pipeline "Training / evaluating" — false for two stages of a three-stage pipeline.

These drive the real runtime rather than pinning its source: a source pin here would be satisfied by
a comment, and the property that matters is that a beacon is actually emitted, actually paired, and
actually says nothing the manifest did not authorise.
"""
from __future__ import annotations

import json
import sys

import pytest

from looplab.engine.orchestrator import Engine
from looplab.events.types import (DIAGNOSTIC_EVENTS, EV_PHASE_PROGRESS, PROGRESS_PHASES,
                                  PROGRESS_STAGE_EVAL, PROGRESS_STAGES)
from looplab.runtime.command_eval import run_command_eval


_METRIC = {"kind": "stdout_json", "key": "metric"}


def _py(*lines):
    return [sys.executable, "-c", "\n".join(lines)]


def _unclosed(events):
    """Stage names whose `started` never got a `finished`, in arrival order."""
    open_: list[str] = []
    for e in events:
        if e["status"] == "started":
            open_.append(e["name"])
        elif e["status"] == "finished" and e["name"] in open_:
            open_.remove(e["name"])
    return open_


def test_the_cursor_names_each_stage_as_it_runs():
    """The whole point: three stages, three named steps, in pipeline order."""
    seen: list[dict] = []
    stages = [
        {"name": "mine", "command": _py("import pathlib; pathlib.Path('neg.parquet').write_text('n')"),
         "expect": {"files": ["neg.parquet"]}},
        {"name": "train", "needs": ["neg.parquet"],
         "command": _py("import pathlib; pathlib.Path('ckpt.pt').write_text('w')"),
         "expect": {"files": ["ckpt.pt"]}},
        {"name": "score", "needs": ["ckpt.pt"], "command": _py("print('{\"metric\": 0.9}')")},
    ]
    import tempfile
    with tempfile.TemporaryDirectory() as wd:
        res = run_command_eval(["true"], wd, 60, _METRIC, stages=stages, on_stage_event=seen.append)

    assert res.metric == 0.9, "precondition: the pipeline really ran"
    assert [(e["name"], e["status"]) for e in seen] == [
        ("mine", "started"), ("mine", "finished"),
        ("train", "started"), ("train", "finished"),
        ("score", "started"), ("score", "finished")]
    # The position rides along so a surface can render "2 of 3" without re-deriving the plan.
    assert [(e["index"], e["total"]) for e in seen if e["status"] == "started"] == [(0, 3), (1, 3), (2, 3)]


@pytest.mark.parametrize("stages, ran", [
    # A stage that CRASHES: the cursor must close, or a status surface reports a dead node as
    # training forever — the one failure mode that makes a live signal worse than none.
    ([{"name": "mine", "command": _py("print('ok')")},
      {"name": "train", "command": _py("import sys; sys.exit(3)")},
      {"name": "score", "command": _py("print('{\"metric\": 1.0}')")}], ["mine", "train"]),
    # An unmet `needs` returns EARLY, before any process of that stage exists. The cursor must not
    # open at all: naming a stage that never started is the same lie in the other direction.
    ([{"name": "mine", "command": _py("print('ok')")},
      {"name": "train", "needs": ["missing.bin"], "command": _py("print('x')")}], ["mine"]),
])
def test_the_cursor_is_always_closed_and_never_opens_on_a_stage_that_did_not_run(stages, ran):
    seen: list[dict] = []
    import tempfile
    with tempfile.TemporaryDirectory() as wd:
        run_command_eval(["true"], wd, 60, _METRIC, stages=stages, on_stage_event=seen.append)
    assert _unclosed(seen) == []
    assert [e["name"] for e in seen if e["status"] == "started"] == ran


def test_a_single_command_eval_reports_one_step():
    """No manifest is not the same as no signal: that command IS the eval, and `eval_log_plan` calls
    it the training. `1 of 1` is the honest count."""
    seen: list[dict] = []
    import tempfile
    with tempfile.TemporaryDirectory() as wd:
        res = run_command_eval(_py("print('{\"metric\": 0.5}')"), wd, 60, _METRIC,
                               on_stage_event=seen.append)
    assert res.metric == 0.5
    assert [(e["name"], e["index"], e["total"], e["status"]) for e in seen] == [
        ("eval", 0, 1, "started"), ("eval", 0, 1, "finished")]


def test_the_instrument_never_takes_down_the_eval_it_reports_on():
    """`Engine._progress`'s rule, one layer down. A beacon that can fail a multi-hour training is a
    downgrade, not an improvement."""
    def boom(_payload):
        raise RuntimeError("the instrument exploded")

    import tempfile
    with tempfile.TemporaryDirectory() as wd:
        res = run_command_eval(["true"], wd, 60, _METRIC, on_stage_event=boom, stages=[
            {"name": "score", "command": _py("print('{\"metric\": 0.7}')")}])
    assert res.metric == 0.7


def test_an_eval_with_no_injected_cursor_is_unchanged():
    """`run_command_eval` is the public library entry point; a caller that injects nothing must
    behave exactly as it did before the cursor existed."""
    import tempfile
    with tempfile.TemporaryDirectory() as wd:
        res = run_command_eval(["true"], wd, 60, _METRIC, stages=[
            {"name": "score", "command": _py("print('{\"metric\": 0.31}')")}])
    assert res.metric == 0.31
    assert [s["name"] for s in (res.stages or [])] == ["score"]


class _Store:
    def __init__(self):
        self.rows: list[tuple[str, dict]] = []

    def append(self, event_type, data):
        self.rows.append((event_type, data))


def _engine_with_store():
    engine = Engine.__new__(Engine)
    engine.store = _Store()
    return engine


def test_the_role_comes_from_the_manifest_and_never_from_the_stage_name():
    """THE claim boundary. `train` is a slug the agent chose — `eval_log_plan` spends a page on why a
    stage NAME proves nothing, and a status surface that read "training" off the name would be
    applying a looser rule than the one the engine uses for kill authority."""
    engine = _engine_with_store()
    declared = [
        {"name": "mine", "command": ["x"]},
        {"name": "train", "command": ["y"], "role": "training", "expect": {"files": ["c"]}},
        {"name": "score", "command": ["z"]},
    ]
    engine._stage_progress_fn(7, 2, declared)(
        {"name": "train", "index": 1, "total": 3, "status": "started"})

    undeclared = [{"name": "mine", "command": ["x"]}, {"name": "train", "command": ["y"]},
                  {"name": "score", "command": ["z"]}]
    engine._stage_progress_fn(8, 0, undeclared)(
        {"name": "train", "index": 1, "total": 3, "status": "started"})

    roles = [data["role"] for _t, data in engine.store.rows]
    assert roles == ["training", "work"], (
        "an identically-named stage must be `training` only where the MANIFEST said so")


def test_the_single_command_role_is_bridged_rather_than_lost():
    """That plan keys its one role under `None` and the runtime calls the step `eval`. A miss would
    degrade the one case where the role is unambiguous to `unknown`."""
    engine = _engine_with_store()
    engine._stage_progress_fn(3, 0, [])({"name": "eval", "index": 0, "total": 1, "status": "started"})
    assert engine.store.rows[0][1]["role"] == "training"


def test_the_beacon_is_generation_scoped_and_registered():
    engine = _engine_with_store()
    engine._stage_progress_fn(5, 4, [{"name": "score", "command": ["z"]}])(
        {"name": "score", "index": 0, "total": 1, "status": "started"})
    event_type, data = engine.store.rows[0]
    assert event_type == EV_PHASE_PROGRESS
    assert (data["stage"], data["phase"]) == (PROGRESS_STAGE_EVAL, "stage")
    assert data["phase"] in PROGRESS_PHASES[PROGRESS_STAGE_EVAL]
    # Without the generation a reset's abandoned lifecycle and its replacement publish the same key.
    assert (data["node_id"], data["generation"]) == (5, 4)


def test_an_unregistered_phase_is_a_hard_error_and_not_a_silent_skip():
    """A beacon has no reader that fails loudly, so the registry is the only thing standing between a
    typo and a signal that silently stops existing."""
    engine = _engine_with_store()
    with pytest.raises(ValueError):
        engine._emit_progress(PROGRESS_STAGE_EVAL, "not_a_phase", "started", {})
    assert engine.store.rows == []


def test_the_cursor_is_diagnostic_so_no_reader_may_key_on_its_position():
    """Invariant #1 permits a concurrent producer exactly the diagnostic types, and the load-bearing
    half is that `speculation.py::_proposal_authority_seq` excludes the whole set — an eval runs on a
    worker thread, so a beacon lands inside paid proposals' CAS windows as a matter of course."""
    assert EV_PHASE_PROGRESS in DIAGNOSTIC_EVENTS
    assert PROGRESS_STAGE_EVAL in PROGRESS_STAGES


def test_the_cursor_changes_no_folded_state(tmp_path):
    """It must be possible to delete every beacon from a log and fold the same RunState (invariant
    #5): the rows are transient progress, and a resume has to rebuild state with or without them."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    log = tmp_path / "events.jsonl"
    store = EventStore(str(log))
    store.append("run_started", {"run_id": "cursor", "task_id": "toy_quadratic"})
    store.append("node_created", {"node_id": 0, "operator": "draft", "parent_ids": [],
                                  "idea": {"operator": "draft", "params": {}}, "code": "x"})
    engine = Engine.__new__(Engine)
    engine.store = store
    emit = engine._stage_progress_fn(0, 0, [{"name": "train", "command": ["y"]}])
    emit({"name": "train", "index": 0, "total": 1, "status": "started"})
    store.append("node_evaluated", {"node_id": 0, "metric": 1.0})
    emit({"name": "train", "index": 0, "total": 1, "status": "finished"})

    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert sum(1 for r in rows if r["type"] == EV_PHASE_PROGRESS) == 2, "precondition: beacons landed"

    # RENUMBER, and this is the trap worth recording rather than working around silently: dropping
    # rows from an event log leaves GAPS in `seq`, and a gapped log does not fold to the same state —
    # here the surviving `node_evaluated` was skipped entirely, so the node read `pending` with no
    # metric. Written the naive way this test appears to prove a fold divergence the beacons did not
    # cause, which is the sort of false positive that gets a real invariant re-litigated.
    kept = [dict(r) for r in rows if r["type"] != EV_PHASE_PROGRESS]
    for i, row in enumerate(kept):
        row["seq"] = i
    without = tmp_path / "without.jsonl"
    without.write_text("".join(json.dumps(r) + "\n" for r in kept))
    folded = fold(EventStore(str(log)).read_all())
    assert folded.nodes[0].metric == 1.0, "precondition: the beacon-bearing log folds a real terminal"
    assert folded.model_dump_json() == fold(EventStore(str(without)).read_all()).model_dump_json()


def test_a_failing_phase_keeps_its_own_exception():
    """`_progress` emits its `finished` row from a `finally`, so anything that raises there REPLACES
    the exception the phase was propagating — a body reporting what it learned would destroy the
    failure it was reporting. That is why the validating entry (`_emit_progress`) and the contained
    append (`_append_progress_row`) are split: this path must reach one that cannot raise."""
    from looplab.events.types import PROGRESS_STAGE_BUILD

    engine = _engine_with_store()
    with pytest.raises(RuntimeError, match="the build failed"):
        with engine._progress(PROGRESS_STAGE_BUILD, "implement", node_id=1) as learned:
            learned["ok"] = "a body key that collides with the beacon's own"
            raise RuntimeError("the build failed")
    # …and the beacon still recorded both ends of the phase it was narrating.
    assert [d["status"] for _t, d in engine.store.rows] == ["started", "finished"]
    assert engine.store.rows[-1][1]["ok"] is False
