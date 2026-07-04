"""Orchestrator internals: workspace fingerprinting + resume-drift detection, the run failure
taxonomy (error_reason), and gap-safe node-id allocation.

Regressions consolidated from the code-review rounds (#4 workspace fingerprint + resume drift,
#6 failure taxonomy) and the hourly review loop (iter 6: gap-safe node-id allocation)."""
from __future__ import annotations

import sys
from pathlib import Path

import anyio

from looplab.engine.orchestrator import Engine, _dir_fingerprint
from looplab.search.policy import GreedyTree
from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.events.replay import fold

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"
_M = {"kind": "stdout_json", "key": "metric"}


def _repo(tmp_path, body):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "run.py").write_text(body, encoding="utf-8")
    return repo


# large data/ref mounts use a cheap shallow fingerprint (no recursive walk), still
# catching top-level add/remove; missing path -> "absent".
def test_shallow_fingerprint(tmp_path):
    from looplab.engine.orchestrator import _shallow_fingerprint
    d = tmp_path / "data"; d.mkdir()
    (d / "a.bin").write_text("x", encoding="utf-8")
    fp1 = _shallow_fingerprint(str(d))
    assert fp1.startswith("dir:")
    (d / "b.bin").write_text("y", encoding="utf-8")          # top-level add -> changes
    assert _shallow_fingerprint(str(d)) != fp1
    assert _shallow_fingerprint(str(tmp_path / "nope")) == "absent"


# --------------------------------- #6b failure taxonomy ------------------------------
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


# --------------------------------------------------------------------------- gap-safe node-id alloc
def test_create_node_id_is_gap_safe(tmp_path):
    # A dropped/malformed node_created leaves a GAP in node ids (fold skips the bad event). The next
    # created node must take max(id)+1, NOT len(nodes) — len would collide with an existing higher id
    # and silently overwrite it (corrupting lineage/best-selection). Regression for that bug.
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.adapters.toytask import ToyTask

    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "gap", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=8))
    eng.store.append("run_started", {"run_id": "gap", "task_id": "t", "direction": "min"})
    eng.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": ""}})
    # node id 2 with NO id 1 -> a gap, as if node 1's event was dropped by fold's malformed-event guard
    eng.store.append("node_created", {"node_id": 2, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {"x": 9.0}, "rationale": ""}})
    assert set(fold(eng.store.read_all()).nodes) == {0, 2}   # gap at 1; len(nodes)==2 would hit node 2

    eng._create_node({"kind": "draft"})

    after = fold(eng.store.read_all())
    assert 2 in after.nodes and after.nodes[2].idea.params["x"] == 9.0   # node 2 NOT overwritten
    assert 3 in after.nodes                                              # new node took max+1, not len(=2)
