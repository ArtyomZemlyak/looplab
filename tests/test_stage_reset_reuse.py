from __future__ import annotations

import json
import sys

import anyio

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree


def _run(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "train.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path('model.txt').write_text('trained')\n"
        "with open(os.environ['TEST_CALLS'], 'a') as fh: fh.write('train\\n')\n",
        encoding="utf-8")
    (src / "score.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "assert Path('model.txt').read_text() == 'trained'\n"
        "with open(os.environ['TEST_CALLS'], 'a') as fh: fh.write('score\\n')\n"
        "print('{\"metric\": 1.0}')\n",
        encoding="utf-8")
    calls = tmp_path / "calls.log"
    task = RepoTask(
        id="reset-reuse", direction="max", editable_path=str(src),
        edit_surface=["*.py", "*.json"],
        eval=EvalSpec(
            command=[sys.executable, "score.py"],
            metric={"kind": "stdout_json", "key": "metric"},
            env={"TEST_CALLS": str(calls)}, cwd="."))
    researcher, developer = task.build_roles()
    engine = Engine(
        tmp_path / "run", task=task, researcher=researcher, developer=developer,
        sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
        auto_install_deps=False, inline_repair=False)
    engine.store.append("run_started", {
        "run_id": "reset-reuse", "run_uid": "uid-reset-reuse", "task_id": task.id,
        "goal": "exercise safe reset reuse", "direction": "max"})
    manifest = json.dumps({"stages": [{
        "name": "train", "command": [sys.executable, "train.py"],
        "expect": {"files": ["model.txt"]}}]})
    engine.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "full",
        "idea": {"operator": "full", "params": {}}, "code": "",
        "files": {"looplab_stages.json": manifest}})
    return engine, calls


def _evaluate(engine):
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1))


def _reset(engine):
    attempt = fold(engine.store.read_all()).nodes[0].attempt
    engine.store.append("node_reset", {
        "node_id": 0, "generation": attempt, "from_stage": "score"})


def _strip_identity(engine):
    rows = []
    for line in (engine.run_dir / "events.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row.get("type") == "stage_finished":
            row["data"].pop("stage_input_key", None)
            row["data"].pop("stage_outputs", None)
        rows.append(json.dumps(row))
    (engine.run_dir / "events.jsonl").write_text("\n".join(rows) + "\n")


def _amend(engine, kind):
    with open(engine.run_dir / "protocol_amendments.jsonl", "a") as fh:
        fh.write(json.dumps({
            "run_uid": "uid-reset-reuse", "kind": kind, "at_event_seq": 999,
            "payload": {"applied": True}}) + "\n")


def test_score_reset_reuses_verified_train_on_consecutive_retries(tmp_path):
    engine, calls = _run(tmp_path)
    _evaluate(engine)
    _reset(engine)
    _evaluate(engine)
    _reset(engine)
    _evaluate(engine)
    assert calls.read_text().splitlines() == ["train", "score", "score", "score"]
    state = fold(engine.store.read_all())
    reused = [row for row in state.nodes[0].stage_attempts
              if row["name"] == "train" and row["status"] == "reused"]
    assert len(reused) == 2
    assert state.nodes[0].metric == 1.0


def test_changed_declared_output_forces_a_full_rerun(tmp_path):
    engine, calls = _run(tmp_path)
    _evaluate(engine)
    workdir = engine.run_dir / "nodes" / "node_0"
    (workdir / "model.txt").write_text("foreign")
    _reset(engine)
    _evaluate(engine)
    assert calls.read_text().splitlines() == ["train", "score", "train", "score"]
    assert (workdir / "model.txt").read_text() == "trained"


def test_legacy_stage_rows_without_identity_fall_back_to_full_rerun(tmp_path):
    engine, calls = _run(tmp_path)
    _evaluate(engine)
    _strip_identity(engine)
    _reset(engine)
    _evaluate(engine)
    assert calls.read_text().splitlines() == ["train", "score", "train", "score"]


def test_only_input_protocol_amendments_invalidate_reuse(tmp_path):
    engine, calls = _run(tmp_path)
    _evaluate(engine)
    _amend(engine, "budget")
    _reset(engine)
    _evaluate(engine)
    assert calls.read_text().splitlines() == ["train", "score", "score"]
    _amend(engine, "task")
    _reset(engine)
    _evaluate(engine)
    assert calls.read_text().splitlines() == ["train", "score", "score", "train", "score"]
