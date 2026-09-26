"""A task's declared `reference_score` and the run's `headroom` (doc 67 67.14).

A task could not say where its scale starts, so a run's best could not be read across tasks.
`reference_score: {baseline: {value, source}, target?: {value, source}}` is reporting only: it is
kept OUT of every task model's dump, so `run_started.config_hash` — which the speculation calibration
validator re-derives — never moves, and it is pinned on `run_started` only when declared.
"""
from __future__ import annotations

import importlib.util
import json
import math

import pytest
from typer.testing import CliRunner

from looplab.adapters.tasks import validate_task
from looplab.cli import app
from looplab.core.headroom import headroom, headroom_line, normalized_reference
from looplab.core.setup_identity import setup_config_hash
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

_REF = {"baseline": {"value": 9.0, "source": "x=0, the untuned start"},
        "target": {"value": 0.0, "source": "the analytic optimum"}}


def test_the_headroom_arithmetic_in_both_directions():
    down = headroom(3.0, _REF, "min")                     # 9 -> 0, landed on 3: two thirds closed
    assert down["gain"] == 6.0 and math.isclose(down["gap_closed"], 2 / 3)
    up = headroom(0.8, {"baseline": {"value": 0.5, "source": "majority class"},
                        "target": {"value": 0.9, "source": "leaderboard #1"}}, "max")
    assert math.isclose(up["gain"], 0.3) and math.isclose(up["gap_closed"], 0.75)
    assert math.isclose(headroom(10.0, _REF, "min")["gap_closed"], -1 / 9), "worse than baseline"
    assert math.isclose(headroom(-1.0, _REF, "min")["gap_closed"], 1 + 1 / 9), "past the target"
    only_baseline = headroom(3.0, {"baseline": _REF["baseline"]}, "min")
    assert only_baseline["gain"] == 6.0 and only_baseline["gap_closed"] is None
    backwards = headroom(3.0, _REF, "max")                # a target WORSE in this run's direction
    assert backwards["gap_closed"] is None and backwards["target_not_better"] is True
    assert headroom(None, _REF, "min") is None and headroom(float("nan"), _REF, "min") is None
    assert headroom(3.0, None, "min") is None


def test_the_fold_keeps_only_a_sourced_finite_reference():
    assert normalized_reference(_REF) == _REF
    assert normalized_reference({"baseline": {"value": 1.0}}) is None, "no source, no reference"
    assert normalized_reference({"baseline": {"value": 1.0, "source": "   "}}) is None, \
        "a blank source is no source: the fold holds a hand-edited log to the submit-time rule"
    assert normalized_reference({"baseline": {"value": float("inf"), "source": "s"}}) is None
    assert normalized_reference({"baseline": {"value": True, "source": "s"}}) is None
    partial = normalized_reference({"baseline": {"value": 1.0, "source": "s"},
                                    "target": {"value": "2", "source": "t"}})
    assert partial == {"baseline": {"value": 1.0, "source": "s"}}, "a bad target drops the target"


@pytest.mark.parametrize("task", [
    {"kind": "quadratic", "goal": "g"},
    {"kind": "dataset", "goal": "g", "data_path": "/tmp"},
    {"kind": "repo", "goal": "g", "editable_path": "/tmp",
     "eval": {"command": ["python", "score.py"],
              "metric": {"kind": "stdout_json", "key": "loss"}}},
    pytest.param({"kind": "mlebench_real", "goal": "g", "competition": "spaceship-titanic"},
                 marks=pytest.mark.skipif(importlib.util.find_spec("mlebench") is None,
                                          reason="mlebench is not installed")),
])
def test_every_task_kind_declares_it_outside_its_identity(task):
    declared = validate_task({**task, "reference_score": _REF})
    plain = validate_task(task)
    assert declared.reference_score.baseline.value == 9.0
    assert "reference_score" not in declared.model_dump(mode="json")
    assert (setup_config_hash(declared.model_dump(mode="json"))
            == setup_config_hash(plain.model_dump(mode="json"))), (
        "the config hash every calibration receipt re-derives must not move")


def test_every_registered_task_model_carries_the_field_excluded():
    """Model-level, so a kind whose validation needs an optional package is covered too."""
    from looplab.adapters.tasks import _KINDS

    for kind, model in _KINDS.items():
        field = model.model_fields.get("reference_score")
        assert field is not None and field.exclude is True, kind


@pytest.mark.parametrize("bad", [
    {"baseline": {"value": 1.0}},
    {"baseline": {"value": 1.0, "source": "  "}},
    {"baseline": {"value": float("nan"), "source": "s"}},
    {"baseline": {"value": 1.0, "source": "s", "sorce": "t"}},
    {"target": {"value": 1.0, "source": "s"}},
    # A NUMBER, as the fold holds it: the lax float coercion took `true` as 1.0 and "9.5" as 9.5
    # while the fold refuses a bool (critic 2026-09-26).
    {"baseline": {"value": True, "source": "s"}},
    {"baseline": {"value": "9.5", "source": "s"}},
])
def test_an_unsourced_or_malformed_reference_is_refused_at_submit(bad):
    with pytest.raises(ValueError):
        validate_task({"kind": "quadratic", "goal": "g", "reference_score": bad})


def _run(tmp_path, monkeypatch, task):
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(tmp_path / "kn"))
    path = tmp_path / "task.json"
    path.write_text(json.dumps(task))
    out = CliRunner().invoke(app, ["run", str(path), "--backend", "toy", "--max-nodes", "4",
                                   "--out", str(tmp_path / "run")])
    assert out.exit_code == 0, out.output
    return out, EventStore(tmp_path / "run" / "events.jsonl").read_all()


def test_a_declared_reference_is_pinned_folded_and_reported(tmp_path, monkeypatch):
    task = {"kind": "quadratic", "id": "toy_ref", "goal": "min (x-3)^2", "direction": "min",
            "reference_score": _REF}
    out, events = _run(tmp_path, monkeypatch, task)
    started = next(e.data for e in events if e.type == "run_started")
    assert started["reference_score"] == _REF
    state = fold(events)
    assert state.reference_score == _REF
    best = state.best().robust_metric
    assert f"headroom: {9.0 - best:+.6g} over the baseline 9 (x=0, the untuned start)" in out.output
    assert "of the gap to the target 0 (the analytic optimum)" in out.output
    assert json.loads((tmp_path / "run" / "task.snapshot.json").read_text())["reference_score"] == _REF
    pytest.importorskip("fastapi")
    from looplab.serve import run_projections
    from looplab.serve.server import make_app
    srv = make_app(tmp_path).state.looplab
    row = next(r for r in run_projections.run_summaries(srv) if r["run_id"] == "run")
    assert math.isclose(row["headroom"]["gap_closed"], (best - 9.0) / (0.0 - 9.0))


def test_an_undeclared_reference_leaves_run_started_as_it_was(tmp_path, monkeypatch):
    out, events = _run(tmp_path, monkeypatch, {"kind": "quadratic", "id": "toy", "goal": "g"})
    assert "reference_score" not in next(e.data for e in events if e.type == "run_started")
    assert fold(events).reference_score is None and "headroom:" not in out.output


def test_a_web_launch_keeps_the_declaration(tmp_path):
    """HIGH (critic 2026-09-26, driven): the field is excluded from the task's dump — which the web
    preflight used as the canonical task — so every web, TUI and assistant launch wrote a
    `task.input.json` without it, and `run_started` is written once. The preflight carries it back."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.serve.server import make_app

    body = TestClient(make_app(tmp_path)).post("/api/start/preflight", json={
        "run_id": "web-ref",
        "task": {"benchmark": "quadratic", "goal": "minimize the objective", "direction": "min",
                 "reference_score": _REF}}).json()
    assert body["ok"] is True, body
    assert body["preview"]["task"]["reference_score"] == _REF
    # …and what the spawned run re-validates from that file still declares it.
    assert validate_task(body["preview"]["task"]).reference_score.baseline.value == 9.0


def test_headroom_is_finite_or_none_and_names_the_best_it_measured(tmp_path):
    """LOW (critic 2026-09-26, driven): two finite marks can overflow, and a non-finite float is not
    JSON — the run list answered 500 for every run. And the row showed the raw `best_metric` beside a
    gain measured from the robust metric: `best` now rides the result."""
    room = headroom(5.0, {"baseline": {"value": 0.0, "source": "s"},
                          "target": {"value": -1e-310, "source": "t"}}, "min")
    assert room["gap_closed"] is None and room["best"] == 5.0 and room["gain"] == -5.0
    json.dumps(room, allow_nan=False)
    assert "is not a finite number" in headroom_line(room)
    huge = headroom(1.7e308, {"baseline": {"value": -1.7e308, "source": "s"}}, "max")
    assert huge["gain"] is None
    json.dumps(huge, allow_nan=False)
    assert "a gain that is not a finite number" in headroom_line(huge)


def test_the_calibration_lane_never_pins_a_reference(tmp_path, monkeypatch):
    """LOW (critic 2026-09-26): the calibration envelope reads the task's dump, which excludes the
    field, so a declaring Toy task was admitted — and its receipt pins the `run_started` KEY SET, so
    the paid evidence would have been refused as a non-writer schema. The lane pins none. (The
    lane's own run-start envelope needs a GPU profile; its pinned VALUES are borrowed from the plain
    lane, since the question here is only which keys the payload carries.)"""
    from factories import make_engine

    task = validate_task({"kind": "quadratic", "goal": "g", "direction": "min",
                          "reference_score": _REF})
    pinned = {}
    for lane in (False, True):
        eng = make_engine(tmp_path / f"lane_{lane}", task=task)
        values = eng._run_start_pinned_values()
        eng._speculation_gate_calibration = lane
        monkeypatch.setattr(eng, "_run_start_pinned_values", lambda values=values: values)
        eng._setup_phase(fold(eng.store.read_all()))
        started = next(e.data for e in eng.store.read_all() if e.type == "run_started")
        pinned[lane] = started.get("reference_score")
    assert pinned == {False: _REF, True: None}
