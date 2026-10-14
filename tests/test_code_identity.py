"""Doc 79: every attempt's code is a reviewable commit of a PRIVATE run repository, its effective
config is recorded, upstream advances form the base lineage, and the run's fixes export to the task
repository as a new branch — without touching the operator's checkout. Real git throughout."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.engine.code_export import export_code
from looplab.engine.code_identity import (BASE_BRANCH, base_lineage, code_status, record_materialized,
                                          sync_run)
from looplab.engine.code_repo import CodeRepo, CodeRepoError
from looplab.engine.code_worktree import add_worktree, overlay_from_patch, overlay_from_worktree
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.core.effective_config import canonical_key, capture
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

_M = {"kind": "stdout_json", "key": "metric"}


def _git(cwd, *argv):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *argv], cwd=cwd,
                          check=True, capture_output=True).stdout.decode()


def _events(rd: Path) -> list:
    return [json.loads(x) for x in (rd / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def _repo_task(tmp_path):
    """A git source with an UNCOMMITTED edit and an untracked protected scorer (doc 37 §5d)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "train.py").write_text("print('LR: 0.01')\nprint('Batch: 32 (micro=8)')\n")
    (repo / "recipe.env").write_text("SEED=1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "train.py", "recipe.env")
    _git(repo, "commit", "-qm", "init")
    (repo / "train.py").write_text("print('LR: 0.02')\nprint('Batch: 32 (micro=8)')\n")
    (repo / "score.py").write_text('import json\nprint(json.dumps({"metric": 7.5}))\n')
    task = RepoTask(id="p", direction="max", editable_path=str(repo), protect=["score.py"],
                    edit_surface=["train.py", "recipe.env"],
                    eval=EvalSpec(command=[sys.executable, "score.py"], metric=_M,
                                  stages=[{"name": "train", "command": [sys.executable, "train.py"]},
                                          {"name": "score", "command": [sys.executable, "score.py"]}]))
    return repo, task


def _engine(tmp_path, task, max_nodes=1):
    r, d = task.build_roles()
    return Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=max_nodes))


def test_engine_records_a_commit_per_attempt_on_the_seeded_working_tree(tmp_path):
    repo, task = _repo_task(tmp_path)
    eng = _engine(tmp_path, task)
    anyio.run(eng.run)
    rd = tmp_path / "run"
    rows = _events(rd)
    snap = [e["data"] for e in rows if e["type"] == "code_snapshot"]
    assert snap and snap[0]["status"] == "recorded", snap
    code = CodeRepo(rd)
    base = code.rev(BASE_BRANCH)
    assert base == snap[0]["base"]["commit"]
    # The base is the WORKING TREE the seed copied: the uncommitted LR edit and the untracked scorer.
    assert code.blob(base, "train.py") == b"print('LR: 0.02')\nprint('Batch: 32 (micro=8)')\n"
    assert code.blob(base, "score.py") is not None
    assert code.rev(snap[0]["commit"] + "^") == base
    # The operator's repository was never written: one commit, no new refs, still dirty as left.
    assert _git(repo, "rev-list", "--count", "--all").strip() == "1"
    assert " M train.py" in _git(repo, "status", "--porcelain")
    # Effective config: read off the stage log's configuration dump, by source.
    cfg = [e["data"] for e in rows if e["type"] == "effective_config"]
    assert cfg and cfg[0]["effective"] == {"lr": 0.02, "batch_size": 32.0, "micro_batch_size": 8.0}
    assert cfg[0]["values"]["lr"][0]["where"] == "train.log:L1"
    # A materialized node with an overlay: its commit's diff against the base is exactly the overlay.
    node = fold(EventStore(rd / "events.jsonl").read_all()).nodes[0].model_copy(deep=True)
    node.id, node.files = 7, {"recipe.env": "SEED=2\n"}
    seed = eng._materialize(node, rd / "nodes" / "node_7")
    last = [e["data"] for e in _events(rd) if e["type"] == "code_snapshot"][-1]
    assert last["node_id"] == 7 and last["changed"] == [{"path": "recipe.env", "status": "M"}]
    assert code.blob(last["commit"], "recipe.env") == b"SEED=2\n"
    assert seed["seed_event_seq"] == last["seed_event_seq"]
    # A canary/confirm scratch tree is the same code again, not a new attempt.
    eng._materialize(node, rd / "canary" / "node_7")
    assert len([e for e in _events(rd) if e["type"] == "code_snapshot"]) == len(snap) + 1


def test_offline_sync_is_deterministic_and_old_runs_still_read(tmp_path):
    repo, task = _repo_task(tmp_path)
    anyio.run(_engine(tmp_path, task).run)
    rd = tmp_path / "run"
    # An OLD run: the same log without any code row, as every run before doc 79 is.
    old = tmp_path / "old"
    old.mkdir()
    lines = [x for x in (rd / "events.jsonl").read_text().splitlines()
             if json.loads(x)["type"] not in ("code_snapshot", "effective_config")]
    (old / "events.jsonl").write_text("\n".join(lines) + "\n")
    import shutil
    shutil.copytree(rd / "base_snapshots", old / "base_snapshots")
    status = code_status(old)
    assert status["repo"]["exists"] is False and status["unsynced_attempts"] == 1
    built = sync_run(old)["built"]
    assert [b["status"] for b in built] == ["recorded"]
    again = sync_run(old)["built"]
    assert [b["commit"] for b in again] == [b["commit"] for b in built]      # idempotent
    other = tmp_path / "old2"
    shutil.copytree(old, other, ignore=shutil.ignore_patterns("code"))
    assert sync_run(other)["built"][0]["commit"] == built[0]["commit"]       # deterministic ids
    status = code_status(old)
    assert status["attempts"][0]["commit"] == built[0]["commit"]


def test_effective_config_flags_a_declared_value_the_run_did_not_use(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "stage1.log").write_text("LR:       2e-4\nBatch:    1024 (micro=128)\nGPUs: 8\n"
                                   "SFT_TOKENIZE_BATCH_SIZE: 1024\nEval steps: 0.25\n")
    (wd / "stage2.log").write_text("SFT_LEARNING_RATE=1e-4\nSFT_WARMUP_STEPS=1000\n")
    (wd / "experiment.env").write_text("ARM=G1\nSEED=42\n")
    rec = capture(wd, env={"SFT_MAX_STEPS": "500", "API_TOKEN": "x"}, overlay_files=["experiment.env"],
                  stage_names=["stage1", "stage2"],
                  declared={"stage1_lr": 2e-4, "stage2_lr": 2e-4, "stage2_warmup": 1000, "seed": 42,
                            "max_steps": 600, "epochs": 3, "arm": "G1"})
    assert rec["by_stage"]["stage1"] == {"lr": 2e-4, "batch_size": 1024.0, "micro_batch_size": 128.0,
                                         "world_size": 8.0}
    assert {m["param"] for m in rec["mismatches"]} == {"stage2_lr", "max_steps"}
    assert rec["unknown"] == ["epochs"] and rec["checked"] == 3
    assert "batch_size" not in rec["by_stage"].get("stage2", {})
    assert all(r["key"] != "API_TOKEN" for rows in rec["values"].values() for r in rows)
    assert canonical_key("SFT_TOKENIZE_BATCH_SIZE") is None and canonical_key("Eval steps") is None


def _target_repo(tmp_path, files: dict) -> Path:
    target = tmp_path / "taskrepo"
    target.mkdir()
    for name, text in files.items():
        (target / name).write_text(text)
    _git(target, "init", "-q", "-b", "main")
    _git(target, "add", ".")
    _git(target, "commit", "-qm", "task repo")
    return target


def test_upstream_fix_flows_to_later_nodes_and_exports_as_a_branch(tmp_path):
    """The end-to-end case: node 0 repairs a pipeline bug, the measured lane advances the base,
    a later node is seeded on the new base, and the exported branch carries the fix."""
    from tests.test_upstream_repairs import repair_fixture
    from looplab.engine.upstream_state import events_for
    from looplab.engine.upstream_workspace import materialization_plan, write_overlay
    from looplab.engine.workspace_seed import seed_candidate_workspace
    from looplab.runtime.command_eval import run_command_eval
    lane, store, generation, proposal = repair_fixture(tmp_path)
    owner = {p.name: p.read_text() for p in (tmp_path / "owner").iterdir()}
    made = lane.propose(proposal)
    checked = lane.check({"expected_generation": generation, "action_id": "c1",
                          "proposal_id": made["proposal_id"]})
    assert checked["status"] == "succeeded", checked
    assert len(checked["result"]["executions"]) == 9      # tests, regressions, trigger probe, pairs
    lane.advance({"expected_generation": generation, "action_id": "a1", "proposal_id": made["proposal_id"],
                  "expected_base_revision": proposal["expected_base_revision"],
                  "evidence_token": checked["evidence_token"]})
    rd = lane.rd
    # The later node, materialized as the engine does it: the plan, the seed, the overlay, the record.
    node1 = fold(store.read_all()).nodes[1]
    spec, rebased, _ = materialization_plan(lane.task.repo_spec(), node1, events_for(rd))
    work = rd / "nodes" / "node_1"
    rows = seed_candidate_workspace(spec, work, capture_base_revision=True,
                                    base_archive_dir=rd / "base_snapshots")
    receipt = rows[0]["base_revision"]
    seeded = store.append("workspace_seeded", {"node_id": 1, "generation": 0, "materialized": [],
                                               "base_revision": receipt})
    write_overlay(work, rebased.files, rebased.deleted)
    engine = SimpleNamespace(store=store, run_dir=rd, _assets={})
    snap = record_materialized(engine, rebased, work, {**receipt, "seed_event_seq": seeded.seq})
    assert snap["status"] == "recorded", snap
    result = run_command_eval([sys.executable, "score.py"], str(work), 10, lane.task.eval_spec()["metric"],
                              stages=lane.task.eval_spec()["stages"])
    assert result.exit_code == 0 and result.metric is not None        # the trigger recipe now runs
    events = store.read_all()
    lineage = base_lineage(events)
    assert [v["reason"] for v in lineage.values()] == ["initial", "upstream"]
    new_digest = list(lineage)[1]
    assert lineage[new_digest]["advance"]["source_node_id"] == 0
    status = code_status(rd, task=lane.task)
    advanced = [b for b in status["bases"] if b["reason"] == "upstream"][0]
    assert advanced["active"] and advanced["commit"]
    proposal_row = status["upstream"]["proposals"][0]
    assert proposal_row["status"] == "advanced" and proposal_row["gate"]["passed"]
    code = CodeRepo(rd)
    assert code.rev(snap["commit"] + "^") == advanced["commit"]       # node 1 sits on the new base
    assert b'ALLOW_SENTINEL' in code.blob(advanced["commit"], "train.py")
    # Export into the task repository: base unchanged (the task repo IS the seed), then the upstream
    # fix, then node 1's recipe — on a NEW branch, with the operator's checkout untouched.
    target = _target_repo(tmp_path, owner)
    head_before = _git(target, "rev-parse", "HEAD")
    report = export_code(rd, target, branch="looplab/fix", node="1", patches_dir=tmp_path / "patches")
    assert report["status"] == "created", report
    kinds = [(s["kind"], bool(s["commit"])) for s in report["steps"]]
    assert kinds == [("base", False), ("upstream", True), ("node", True)], report["steps"]
    assert "ALLOW_SENTINEL" in _git(target, "show", "looplab/fix~1:train.py")
    assert _git(target, "show", "looplab/fix:recipe.env") == "MOMENTUM=-2\nALLOW_SENTINEL=1\n"
    assert "Looplab-Upstream-Gate-Seq" in _git(target, "log", "-1", "--format=%B", "looplab/fix~1")
    assert _git(target, "rev-parse", "HEAD") == head_before
    assert _git(target, "status", "--porcelain") == ""
    assert len(report["patches"]) == 2
    with pytest.raises(CodeRepoError, match="already exists"):
        export_code(rd, target, branch="looplab/fix")


def test_export_merges_onto_a_moved_target_and_stops_on_a_conflict(tmp_path):
    repo, task = _repo_task(tmp_path)
    anyio.run(_engine(tmp_path, task).run)
    rd = tmp_path / "run"
    target = _target_repo(tmp_path, {"train.py": "print('LR: 0.01')\nprint('Batch: 32 (micro=8)')\n",
                                     "recipe.env": "SEED=1\n", "extra.py": "x = 1\n"})
    report = export_code(rd, target, branch="b1", node="none")
    base_step = report["steps"][0]
    # The uncommitted LR edit and the untracked scorer become the base commit; extra.py is the
    # target's own file the snapshot lacks — counted, never deleted.
    assert base_step["changed"] == 2 and base_step["not_in_snapshot"] == 1
    assert _git(target, "show", "b1:extra.py") == "x = 1\n"
    assert "0.02" in _git(target, "show", "b1:train.py")
    # A conflicting target: train.py rewritten differently — a node change to train.py conflicts.
    eng_run = CodeRepo(rd)
    base = eng_run.rev(BASE_BRANCH)
    eng_run.write_overlay_commit("refs/heads/nodes/n9", base,
                                 {"train.py": (b"print('LR: 0.05')\nprint('Batch: 32 (micro=8)')\n", False)},
                                 message="n9\n", when=5)
    _git(target, "checkout", "-q", "-b", "moved")
    (target / "train.py").write_text("print('LR: 0.03')\nprint('Batch: 64')\n")
    _git(target, "commit", "-qam", "moved")
    from looplab.engine import code_export
    builder = code_export._Builder(target, _git(target, "rev-parse", "HEAD").strip(), "")
    step = code_export._delta_step(eng_run, builder, base, eng_run.rev("refs/heads/nodes/n9"), "m\n", "node")
    assert step["conflicts"] == ["train.py"] and step["commit"] is None


def test_external_agent_worktree_patch_and_http_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    from looplab.serve.control_validation import normalize_control
    from tests.test_upstream_lane import fixture
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    lane, store, generation, _ = fixture(tmp_path)
    rd = lane.rd
    with TestClient(make_app(tmp_path)) as client:
        assert client.post("/api/runs/run/code/sync").status_code == 200
        made = client.post("/api/runs/run/code/worktrees", json={"name": "agent1"})
        assert made.status_code == 200, made.text
        wt = Path(made.json()["path"])
        assert wt.is_relative_to(rd / "code" / "worktrees")
        (wt / "recipe.env").write_text("MOMENTUM=0.4\n")
        _git(wt, "commit", "-qam", "agent: momentum 0.4")
        (wt / "README.md").write_text("Runner with momentum\n")                 # uncommitted
        preview = client.post("/api/runs/run/code/overlay", json={"worktree": "agent1"})
        assert preview.status_code == 200, preview.text
        assert preview.json()["files"] == {"recipe.env": "MOMENTUM=0.4\n",
                                           "README.md": "Runner with momentum\n"}
        (wt / "score.py").write_text("print('{\"metric\": 0}')\n")             # the protected scorer
        refused = client.post("/api/runs/run/code/overlay", json={"worktree": "agent1"})
        assert refused.status_code == 400
        assert refused.json()["detail"]["code"] == "code_overlay_outside_surface"
        _git(wt, "checkout", "--", "score.py")
        status = client.get("/api/runs/run/code").json()
        assert status["attempts"][0]["commit"] and status["upstream"]["lane_configured"] is True
        diff = client.get("/api/runs/run/code/diff", params={"commit": status["attempts"][0]["commit"]})
        assert diff.status_code == 200 and "MOMENTUM = 0.2" in diff.json()["diff"]
        assert client.post("/api/runs/run/code/worktrees", json={"name": "agent1"}).status_code == 409
    # The inject intake turns code_from into the ordinary overlay, edit surface held.
    srv = SimpleNamespace(run_dir=lambda run_id: tmp_path / run_id,
                          state=lambda path: fold(EventStore(Path(path) / "events.jsonl").read_all()))
    from looplab.serve.control_validation import _ControlIntake, _apply_code_from
    ctx = _ControlIntake(srv, rd, "inject_node", {"idea": {"operator": "draft"},
                                                  "code_from": {"worktree": "agent1"}})
    _apply_code_from(ctx)
    assert ctx.data["files"]["recipe.env"] == "MOMENTUM=0.4\n" and "code_from" not in ctx.data
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:      # the whole intake still runs its own rules
        normalize_control(srv, rd, "inject_node", {"idea": {"operator": "draft"},
                                                   "code_from": {"worktree": "agent1"},
                                                   "files": {"recipe.env": "x"}})
    assert caught.value.detail["code"] == "code_from_conflict"
    patch = "--- a/recipe.env\n+++ b/recipe.env\n@@ -1 +1 @@\n-MOMENTUM=0.0\n+MOMENTUM=0.7\n"
    assert overlay_from_patch(rd, patch)["files"] == {"recipe.env": "MOMENTUM=0.7\n"}
    with pytest.raises(CodeRepoError, match="does not apply"):
        overlay_from_patch(rd, patch.replace("0.0", "9.9"))
    assert overlay_from_worktree(rd, "agent1")["stale_base"] is False
    with pytest.raises(CodeRepoError):
        add_worktree(rd, "../escape")


def test_two_runs_deliver_in_sequence_and_the_second_keeps_the_first(tmp_path):
    """Doc 72 O14: change A is delivered first; run B, exported onto A's branch, keeps A."""
    target = _target_repo(tmp_path, {"train.py": "print('LR: 0.01')\nprint('Batch: 32 (micro=8)')\n",
                                     "recipe.env": "SEED=1\n"})
    runs = []
    for name, lr in (("a", "0.02"), ("b", "0.01")):
        root = tmp_path / name
        root.mkdir()
        src = root / "repo"
        _git(target, "worktree", "add", "-q", "--detach", str(src), "main")
        (src / "score.py").write_text('import json\nprint(json.dumps({"metric": 7.5}))\n')
        if name == "a":
            (src / "train.py").write_text(f"print('LR: {lr}')\nprint('Batch: 32 (micro=8)')\n")
        else:
            (src / "recipe.env").write_text("SEED=2\n")
        task = RepoTask(id=name, direction="max", editable_path=str(src), protect=["score.py"],
                        eval=EvalSpec(command=[sys.executable, "score.py"], metric=_M))
        anyio.run(_engine(root, task).run)
        runs.append(root / "run")
    first = export_code(runs[0], target, branch="deliver/a", node="none")
    assert first["status"] == "created" and first["steps"][0]["changed"] == 2   # train.py + score.py
    second = export_code(runs[1], target, branch="deliver/b", onto="deliver/a", node="none")
    # B started from main, not from A: its base step is the DELTA main -> B's seed (recipe.env and
    # the same untracked scorer), carried onto A's branch — a snapshot would have reverted A.
    assert second["status"] == "created" and second["steps"][0]["drift_from"]
    assert second["steps"][0]["changed"] == 1 and second["steps"][0]["already_present"] == 1
    assert "0.02" in _git(target, "show", "deliver/b:train.py")
    assert _git(target, "show", "deliver/b:recipe.env") == "SEED=2\n"
    assert _git(target, "rev-parse", "deliver/b~1") == _git(target, "rev-parse", "deliver/a")
