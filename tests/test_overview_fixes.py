"""Items #4 (workspace fingerprint + resume drift) and #6 (event-envelope version + failure
taxonomy)."""
from __future__ import annotations

import sys

import anyio

from looplab.events.eventstore import EventStore
from looplab.engine.orchestrator import Engine, _dir_fingerprint
from looplab.search.policy import GreedyTree
from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.runtime.sandbox import SubprocessSandbox

_M = {"kind": "stdout_json", "key": "metric"}


# --------------------------------- #6a event version ---------------------------------
def test_event_envelope_has_version(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("x", {"a": 1})
    e = list(s.read_all())[0]
    assert e.v == 1                                  # ADR-1 envelope version present


# --------------------------------- #6b failure taxonomy ------------------------------
def _repo(tmp_path, body):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "run.py").write_text(body, encoding="utf-8")
    return repo


def test_failure_reason_no_metric(tmp_path):
    repo = _repo(tmp_path, "print('hello, no metric here')\n")
    t = RepoTask(id="f", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    state = anyio.run(Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                             sandbox=SubprocessSandbox(),
                             policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    assert state.nodes[0].error_reason == "no_metric"


def test_failure_reason_crash(tmp_path):
    repo = _repo(tmp_path, "raise SystemExit('boom')\n")
    t = RepoTask(id="f", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    state = anyio.run(Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                             sandbox=SubprocessSandbox(),
                             policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    assert state.nodes[0].error_reason == "crash"


# --------------------------------- #4 workspace fingerprint --------------------------
def test_dir_fingerprint_changes_with_content(tmp_path):
    d = tmp_path / "r"; d.mkdir()
    (d / "a.py").write_text("x=1\n", encoding="utf-8")
    fp1 = _dir_fingerprint(str(d))
    (d / "b.py").write_text("y=2\n", encoding="utf-8")     # add a file -> different signature
    assert _dir_fingerprint(str(d)) != fp1
    assert _dir_fingerprint(str(tmp_path / "missing")) == "absent"


def test_run_records_workspace_and_resume_detects_change(tmp_path):
    repo = _repo(tmp_path, 'import json; print(json.dumps({"metric": 1.0}))\n')
    t = RepoTask(id="w", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    run_dir = tmp_path / "run"
    s1 = anyio.run(Engine(run_dir, task=t, researcher=r, developer=d,
                          sandbox=SubprocessSandbox(),
                          policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    assert s1.workspace and "editable:." in s1.workspace
    assert s1.workspace_changed is False
    # The operator's source changes after the run started; a resume must NOT pretend it's the
    # same workspace.
    (repo / "run.py").write_text(
        'import json; print(json.dumps({"metric": 2.0}))\n', encoding="utf-8")
    r2, d2 = t.build_roles()
    s2 = anyio.run(Engine(run_dir, task=t, researcher=r2, developer=d2,
                          sandbox=SubprocessSandbox(),
                          policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    assert s2.workspace_changed is True
