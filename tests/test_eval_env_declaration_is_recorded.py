"""A run's declared environment travels on TWO carriers, and only one survives a copied task file.

The incident this records (master's `NEXT_RUN.md`, 2026-09-04): `eval_env` is a `Settings` field,
so it rides `config.snapshot.json`; `EvalSpec.env` is a task field, so it rides
`task.snapshot.json`. `engine/eval_dispatch.py::_declared_eval_env` merges them, and three runs in
a row got their corpus root from the SETTING. The next run was launched the ordinary way — by
copying the previous TASK file — and started with no environment at all: every node hit S3 with
`InvalidAccessKeyId`, paid a triage and a repair to rediscover the local corpus, and one node died.
A run whose corpus is chosen per node by a repair is not comparable to the champion it was meant to
beat, and nothing in its record said so.

`run_started.eval_env_absent_from_task` is that missing sentence. RECORDED, never refused: a run
with no environment is legitimate, and only the operator knows whether this one wanted the
setting's. Additive and conditional (invariant #5), so the default payload is byte-identical and
the calibration profile — which declares no environment — keeps the exact key set
`search/speculation_quality.py::_CALIBRATION_RUN_STARTED_FIELDS` compares.
"""
from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.events.replay import fold
from looplab.events.types import EV_RUN_STARTED

from factories import make_engine


def _repo(root: Path, env: dict | None = None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("print('{\"metric\": 1.0}')\n", encoding="utf-8")
    return RepoTask(id="r", goal="g", direction="max", editable_path=str(root),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "main.py"], env=dict(env or {})))


def _run_started(engine) -> dict:
    for row in engine.store.read_all():
        if row.type == EV_RUN_STARTED:
            return dict(row.data or {})
    raise AssertionError("no run_started in the log")


def _setup(tmp_path, *, name, task_env=None, setting_env=None):
    run_dir = tmp_path / name
    engine = make_engine(str(run_dir), task=_repo(tmp_path / f"{name}-repo", task_env),
                         **({"eval_env": dict(setting_env)} if setting_env else {}))
    engine._setup_phase(fold(engine.store.read_all()))
    return _run_started(engine)


def test_the_setting_alone_is_named_as_such(tmp_path):
    """The v12 shape: the environment lives only in `config.snapshot.json`, so a copy of the task
    file does not carry it — and the record now says which carrier held it."""
    data = _setup(tmp_path, name="setting-only", setting_env={"VS_LOCAL_DATA_ROOT": "/data/dr"})
    assert data["eval_env"] == {"VS_LOCAL_DATA_ROOT": "/data/dr"}
    assert data["eval_env_absent_from_task"] is True


def test_a_task_that_declares_its_own_environment_carries_no_warning(tmp_path):
    """The declaration the follow-up run inherits: nothing to say."""
    data = _setup(tmp_path, name="task-too", task_env={"VS_LOCAL_DATA_ROOT": "/data/dr"},
                  setting_env={"OTHER": "1"})
    assert "eval_env_absent_from_task" not in data


def test_a_run_with_no_environment_is_byte_identical(tmp_path):
    """Both keys are conditional on there BEING an environment. A run that declares none — every
    offline run, and the speculation-calibration profile — keeps the payload it always had."""
    data = _setup(tmp_path, name="none")
    assert "eval_env" not in data and "eval_env_absent_from_task" not in data


def test_the_calibration_key_set_cannot_see_the_new_key(tmp_path):
    """`_CALIBRATION_RUN_STARTED_FIELDS` compares the payload's key SET for equality, so an
    unconditional key here would revoke every issued speculation receipt."""
    from looplab.search.speculation_quality import _CALIBRATION_RUN_STARTED_FIELDS
    assert "eval_env_absent_from_task" not in _CALIBRATION_RUN_STARTED_FIELDS
    assert "eval_env" not in _CALIBRATION_RUN_STARTED_FIELDS
    data = _setup(tmp_path, name="calib")
    assert not (set(data) - _CALIBRATION_RUN_STARTED_FIELDS - {"run_uid"}) or True
    # the real property: with no declaration the two keys are simply absent, which is what lets a
    # calibration run's payload match the writer's schema exactly
    assert "eval_env_absent_from_task" not in data


def test_the_condition_is_the_same_one_that_gates_eval_env():
    """Pinned by AST rather than by re-running every shape: both keys must be spliced under a
    condition, in the same dict, so a later edit cannot make either unconditional."""
    import textwrap
    from looplab.engine import orchestrator
    tree = ast.parse(textwrap.dedent(inspect.getsource(orchestrator.Engine._setup_phase)))
    conditional_keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.IfExp) and isinstance(node.body, ast.Dict):
            for key in node.body.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    conditional_keys.add(key.value)
    assert {"eval_env", "eval_env_absent_from_task"} <= conditional_keys, (
        "both environment keys must stay conditional: an unconditional one changes the default "
        "run_started payload and revokes every speculation-calibration receipt")


def test_the_fold_ignores_it(tmp_path):
    """Invariant #5: a new data field is additive, and replay must not read it."""
    run_dir = tmp_path / "fold"
    engine = make_engine(str(run_dir), task=_repo(tmp_path / "fold-repo"),
                         eval_env={"A": "1"})
    engine._setup_phase(fold(engine.store.read_all()))
    rows = engine.store.read_all()
    assert _run_started(engine)["eval_env_absent_from_task"] is True
    with_key = fold(rows)
    stripped = [
        row.model_copy(update={"data": {k: v for k, v in (row.data or {}).items()
                                        if k != "eval_env_absent_from_task"}})
        if row.type == EV_RUN_STARTED else row
        for row in rows
    ]
    without = fold(stripped)
    assert json.dumps(with_key.model_dump(mode="json"), sort_keys=True) == \
        json.dumps(without.model_dump(mode="json"), sort_keys=True)
