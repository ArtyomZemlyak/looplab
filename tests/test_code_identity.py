"""Doc 79: every attempt's code is a reviewable commit of a PRIVATE run repository, its effective
config is recorded, upstream advances form the base lineage, and the run's fixes export to the task
repository as a new branch — without touching the operator's checkout. Real git throughout."""
from __future__ import annotations

import json
import os
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
from tests._posix_gates import MODE_BITS

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


def test_effective_config_reads_no_progress_line_as_the_epoch_budget(tmp_path):
    """Review 2026-10-15 (driven): any two-token name with `epoch` in it mapped to `epochs`, so the
    training loop's own progress (`Epoch 1: loss 0.52`, `Epoch time: 12.3`) became
    `effective.epochs` and a declared `epochs` read as a false `mismatch`. A genuine configuration
    line still reads, and a numeric coordinate never takes a word as its value."""
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "train.log").write_text("Epoch 1: loss 0.52\nEpoch 2: loss 0.41\nEpoch time: 12.3\n"
                                  "train epoch: 2\nEpoch: 3/10\nLR: warm\n")
    rec = capture(wd, stage_names=["train"], declared={"epochs": 10})
    assert rec["values"].get("epochs") is None and rec["values"].get("lr") is None, rec["values"]
    assert rec["mismatches"] == [] and rec["unknown"] == ["epochs"]
    (wd / "train.log").write_text("num_train_epochs: 10\nmax_epoch = 10\nEPOCHS=10\nEpoch: 10\n"
                                  "scheduler: cosine\n")
    rec = capture(wd, stage_names=["train"], declared={"epochs": 10})
    assert rec["effective"] == {"epochs": 10.0, "scheduler": "cosine"}, rec["effective"]
    assert rec["checked"] == 1 and rec["mismatches"] == []
    assert canonical_key("EPOCH") == "epochs" and canonical_key("Epoch 1") is None


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


# ------------------------------------------------------------------ review 2026-10-15 regressions

def _rewrite_rows(rd: Path, edit) -> None:
    """Edit log rows IN PLACE (`edit(row) -> row`): deleting one would leave a seq gap, and the
    store serves only the dense prefix before it."""
    path = rd / "events.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    path.write_text("".join(json.dumps(edit(r)) + "\n" for r in rows), encoding="utf-8")


def _materialized_node(eng, rd: Path, node_id: int, files: dict):
    node = fold(EventStore(rd / "events.jsonl").read_all()).nodes[0].model_copy(deep=True)
    node.id, node.files = node_id, files
    seed = eng._materialize(node, rd / "nodes" / f"node_{node_id}")
    return node, seed


def test_an_in_place_repair_commit_is_written_and_read_back(tmp_path):
    """Finding 1: the repair ref was nested under the attempt ref (`.../s5/repair` beside the FILE
    `.../s5`), which git refuses — every repair commit came back `unavailable`."""
    from looplab.engine.code_identity import record_after_terminal
    _, task = _repo_task(tmp_path)
    eng = _engine(tmp_path, task)
    anyio.run(eng.run)
    rd = tmp_path / "run"
    node, seed = _materialized_node(eng, rd, 7, {"recipe.env": "SEED=2\n"})
    first = [e["data"] for e in _events(rd) if e["type"] == "code_snapshot"][-1]
    (rd / "nodes" / "node_7" / "recipe.env").write_text("SEED=3\n")       # repaired during eval
    repair = record_after_terminal(eng, SimpleNamespace(node=node, workdir=rd / "nodes" / "node_7",
                                                        base_revision=seed))
    assert repair["status"] == "recorded", repair
    code = CodeRepo(rd)
    assert repair["repair_of"] == first["commit"]
    assert code.rev(repair["commit"] + "^") == first["commit"]
    assert code.rev(first["ref"]) == first["commit"]                      # the first commit stays
    assert code.blob(repair["commit"], "recipe.env") == b"SEED=3\n"
    row = [a for a in code_status(rd)["attempts"] if a["node_id"] == 7]
    assert len(row) == 1 and row[0]["commit"] == repair["commit"]


def test_canary_confirm_and_noise_seeds_are_not_attempts(tmp_path):
    """Finding 6: a canary / confirm / noise-floor materialization carries the node's id, generation
    and base receipt; `sync_run` made each a phantom attempt commit that moved the node's branch,
    and `code_status` listed it twice. New rows say which tree they built; legacy rows keep the
    first seed per lifecycle."""
    _, task = _repo_task(tmp_path)
    eng = _engine(tmp_path, task)
    anyio.run(eng.run)
    rd = tmp_path / "run"
    node, _ = _materialized_node(eng, rd, 7, {"recipe.env": "SEED=2\n"})
    code = CodeRepo(rd)
    branch = code.rev("refs/heads/nodes/n7")
    for scratch in (rd / "canary" / "node_7", rd / "confirm" / "node_7_g0_seed_1",
                    rd / "noise" / "node_7_g0_seed_2"):
        eng._materialize(node, scratch)
    seeds = [e["data"] for e in _events(rd) if e["type"] == "workspace_seeded"]
    assert [s.get("workdir") for s in seeds if s["node_id"] == 7] == [
        "nodes/node_7", "canary/node_7", "confirm/node_7_g0_seed_1", "noise/node_7_g0_seed_2"]
    assert sync_run(rd)["built"] == []
    assert code.rev("refs/heads/nodes/n7") == branch
    assert len([a for a in code_status(rd)["attempts"] if a["node_id"] == 7]) == 1
    # A LEGACY log (no `workdir`, no code rows): one attempt per lifecycle, not four.
    old = tmp_path / "old"
    old.mkdir()
    (old / "events.jsonl").write_text((rd / "events.jsonl").read_text())
    import shutil
    shutil.copytree(rd / "base_snapshots", old / "base_snapshots")

    def legacy(row):
        row["data"].pop("workdir", None)
        if row["type"] == "code_snapshot":
            row["type"] = "code_snapshot_dropped"          # an unknown type: what an old log lacks
        return row
    _rewrite_rows(old, legacy)
    assert [b["node_id"] for b in sync_run(old)["built"]].count(7) == 1
    assert len([a for a in code_status(old)["attempts"] if a["node_id"] == 7]) == 1


def test_a_worktree_or_patch_from_a_node_carries_that_nodes_change(tmp_path):
    """Finding 2: the overlay was diffed against the START commit, so from `node:<id>` it held the
    agent's edits alone and — materialized over the base — reverted the start node's own change."""
    _, task = _repo_task(tmp_path)
    eng = _engine(tmp_path, task)
    anyio.run(eng.run)
    rd = tmp_path / "run"
    _materialized_node(eng, rd, 7, {"recipe.env": "SEED=2\n"})
    meta = add_worktree(rd, "from7", start="node:7")
    wt = Path(meta["path"])
    (wt / "train.py").write_text("print('LR: 0.05')\n")
    overlay = overlay_from_worktree(rd, "from7")
    assert overlay["files"] == {"recipe.env": "SEED=2\n", "train.py": "print('LR: 0.05')\n"}
    assert overlay["overlay_base"] == CodeRepo(rd).rev(BASE_BRANCH) != overlay["start_commit"]
    patch = ("--- a/train.py\n+++ b/train.py\n@@ -1,2 +1,2 @@\n"
             "-print('LR: 0.02')\n+print('LR: 0.07')\n print('Batch: 32 (micro=8)')\n")
    from_node = overlay_from_patch(rd, patch, start="node:7")
    assert from_node["files"] == {"recipe.env": "SEED=2\n",
                                  "train.py": "print('LR: 0.07')\nprint('Batch: 32 (micro=8)')\n"}
    assert overlay_from_patch(rd, patch)["files"] == {      # from the base: unchanged behaviour
        "train.py": "print('LR: 0.07')\nprint('Batch: 32 (micro=8)')\n"}
    # A start node's commit holds what its materialization wrote, task assets included: a path
    # outside the surface the agent left exactly as the start had it is inherited, not refused ...
    from looplab.engine.code_identity import _digest, record_attempt
    (rd / "task.snapshot.json").write_text(task.model_dump_json(), encoding="utf8")
    events = EventStore(rd / "events.jsonl").read_all()
    seed = [e for e in events if e.type == "workspace_seeded"][-1]
    made = record_attempt(rd, events, node_id=8, generation=0, seed_event_seq=seed.seq,
                          base_digest=_digest(seed.data["base_revision"]), source="workspace",
                          changes={"recipe.env": (b"SEED=8\n", False),
                                   "grader_asset.py": (b"ANSWERS = 1\n", False)})
    assert made["status"] == "recorded", made
    meta = add_worktree(rd, "from8", start="node:8")
    (Path(meta["path"]) / "train.py").write_text("print('LR: 0.08')\n")
    inherited = overlay_from_worktree(rd, "from8")
    assert inherited["files"] == {"recipe.env": "SEED=8\n", "train.py": "print('LR: 0.08')\n"}
    assert inherited["inherited_outside_surface"] == ["grader_asset.py"]
    # ... and one the agent CHANGED is refused as before.
    (Path(meta["path"]) / "grader_asset.py").write_text("ANSWERS = 2\n")
    with pytest.raises(CodeRepoError, match="outside the task's edit surface"):
        overlay_from_worktree(rd, "from8")


def _run_from_target_worktree(tmp_path, prepare=None):
    """A run whose editable is a worktree of the target repository, so the source commit the run
    recorded at start exists there (the doc 72 O14 delivery shape). `prepare(src)` edits the
    source before the run seeds from it."""
    target = _target_repo(tmp_path, {"train.py": "print('LR: 0.01')\nprint('Batch: 32 (micro=8)')\n",
                                     "recipe.env": "SEED=1\n"})
    src = tmp_path / "src"
    _git(target, "worktree", "add", "-q", "--detach", str(src), "main")
    (src / "score.py").write_text('import json\nprint(json.dumps({"metric": 7.5}))\n')
    (src / "train.py").write_text("print('LR: 0.02')\nprint('Batch: 32 (micro=8)')\n")
    if prepare is not None:
        prepare(src)
    task = RepoTask(id="p", direction="max", editable_path=str(src), protect=["score.py"],
                    edit_surface=["train.py", "recipe.env"],
                    eval=EvalSpec(command=[sys.executable, "score.py"], metric=_M))
    eng = _engine(tmp_path, task)
    anyio.run(eng.run)
    return target, tmp_path / "run", eng


def test_export_drifts_from_the_editables_commit_not_a_git_data_mounts(tmp_path):
    """Finding 3: `run_started.workspace` lists the editables first and the data/ref mounts after,
    and the export took the LAST `git:` value — a git-backed data mount's HEAD."""
    target, rd, _ = _run_from_target_worktree(tmp_path)
    main = _git(target, "rev-parse", "main").strip()
    (target / "data.csv").write_text("x\n1\n")
    _git(target, "add", "data.csv")
    _git(target, "commit", "-qm", "a data repository's commit")
    other = _git(target, "rev-parse", "HEAD").strip()

    def with_data_mount(row):
        if row["type"] == "run_started":
            assert row["data"]["workspace"] == {"editable:.": "git:" + main}
            row["data"]["workspace"]["data:d"] = "git:" + other
        return row
    _rewrite_rows(rd, with_data_mount)
    report = export_code(rd, target, branch="x", node="none", dry_run=True)
    assert report["onto"] == main and report["steps"][0]["drift_from"] == main
    from looplab.engine.code_export import _recorded_source_commit
    assert _recorded_source_commit({"editable:.": "file:1:2", "data:d": "git:" + other}) is None
    assert _recorded_source_commit({"editable:a": "git:" + main, "editable:b": "git:" + main,
                                    "ref:r": "git:" + other}) == main


def test_export_names_only_the_target_lifecycles_own_commit(tmp_path):
    """Findings 4 and 5: an OLD run's commits exist only as what `sync_run` built, and a named node's
    export was skipped; and a champion with no commit fell back to `refs/heads/champion`, which still
    named an EARLIER champion — exported under the current one's id, title and metric."""
    target, rd, _ = _run_from_target_worktree(tmp_path)
    old = tmp_path / "old"
    old.mkdir()
    (old / "events.jsonl").write_text((rd / "events.jsonl").read_text())
    for name in ("base_snapshots", "task.snapshot.json"):
        src = rd / name
        if src.is_dir():
            import shutil
            shutil.copytree(src, old / name)
        elif src.exists():
            (old / name).write_bytes(src.read_bytes())

    def no_code_rows(row):
        if row["type"] == "code_snapshot":
            row["type"] = "code_snapshot_dropped"
        return row
    _rewrite_rows(old, no_code_rows)
    report = export_code(old, target, branch="n0", node="0", dry_run=True)
    node_step = report["steps"][-1]
    assert node_step["kind"] == "node" and "skipped" not in node_step, report["steps"]
    # The champion (node 0) has NO commit — its record says unavailable — while the champion branch
    # still names an EARLIER champion's commit: the step is skipped, never that diff under node 0.
    def unavailable(row):
        if row["type"] == "code_snapshot_dropped" and row["data"]["node_id"] == 0:
            row["type"] = "code_snapshot"
            row["data"] = {k: v for k, v in row["data"].items() if k not in ("commit", "ref")}
            row["data"].update(status="unavailable", reason="git_failed")
        return row
    _rewrite_rows(old, unavailable)
    code = CodeRepo(old)
    code.write_overlay_commit("refs/heads/champion", code.rev(BASE_BRANCH),
                              {"train.py": (b"print('an earlier champion')\n", False)},
                              message="node 3 g0: an earlier champion\n", when=1)
    report = export_code(old, target, branch="champ", node="champion", dry_run=True)
    node_step = report["steps"][-1]
    assert node_step == {"kind": "node", "commit": None, "node_id": 0,
                         "skipped": "no recorded attempt commit"}, report["steps"]
    assert not any(c["subject"].startswith("LoopLab node") for c in report["commits"])


def test_export_prefix_reads_the_task_as_the_adapter_does(tmp_path, monkeypatch):
    """Finding 8: the default prefix read the raw `editable_path` key, so the composable `repo`
    alias, a `~` spelling and the multi-editable `editables` list all exported at the root."""
    from looplab.engine.code_export import ExportRefusal, _default_prefix
    target = _target_repo(tmp_path, {"README": "x\n"})
    for sub in ("svc", "pkgs/a", "pkgs/b", "elsewhere/b"):
        (target / sub).mkdir(parents=True)
        (target / sub / "train.py").write_text("print(1)\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    rd = tmp_path / "snap"
    rd.mkdir()
    cmd = {"command": [sys.executable, "train.py"], "metric": {"reader": "stdout_json", "key": "m"}}

    def prefix_for(**task):
        (rd / "task.snapshot.json").write_text(json.dumps({"id": "p", "direction": "max",
                                                           "cmd": cmd, **task}))
        return _default_prefix(target, rd)
    assert prefix_for(repo="~/taskrepo/svc") == ("svc", None)
    assert prefix_for(editable_path=str(target)) == ("", None)
    multi = [{"name": "a", "path": str(target / "pkgs/a")},
             {"name": "b", "path": str(target / "pkgs/b")}]
    assert prefix_for(editables=multi) == ("pkgs", None)
    prefix, note = prefix_for(repo=str(target / "svc"), editables=multi[:1])
    assert prefix == "svc" and "a" in note                  # the root editable decides, said so
    with pytest.raises(ExportRefusal, match="--prefix"):
        prefix_for(editables=[multi[0], {"name": "b", "path": str(target / "elsewhere/b")}])


# ------------------------------------------------- review 2026-10-15: weekly-export-file-mode

def _mode(target: Path, rev: str, path: str) -> str:
    return _git(target, "ls-tree", rev, "--", path).split(" ", 1)[0]


@MODE_BITS
def test_export_carries_a_chmod_in_the_base_with_and_without_a_content_change(tmp_path):
    """The seed's `chmod +x` exports as 100755: on a changed file (equality was blob-only and the
    target's `have[0]` was kept) and on an unchanged one (a chmod-only drift was `unchanged`)."""
    def chmod(src):
        os.chmod(src / "train.py", 0o755)           # content changed too (LR 0.01 -> 0.02)
        os.chmod(src / "recipe.env", 0o755)         # mode only
    target, rd, _ = _run_from_target_worktree(tmp_path, chmod)
    report = export_code(rd, target, branch="modes", node="none")
    assert report["status"] == "created", report
    assert report["steps"][0]["changed"] == 3, report["steps"]      # train.py, recipe.env, score.py
    assert _mode(target, "modes", "train.py") == "100755"
    assert "0.02" in _git(target, "show", "modes:train.py")
    assert _mode(target, "modes", "recipe.env") == "100755"
    assert _git(target, "show", "modes:recipe.env") == "SEED=1\n"
    assert _mode(target, "modes", "score.py") == "100644"
    # A target that does not count a working tree's mode (`core.fileMode=false`, Windows' default)
    # takes the seed's st_mode as no evidence: the bytes move, the source commit's modes stay.
    _git(target, "config", "core.fileMode", "false")
    report = export_code(rd, target, branch="modes-off", node="none")
    assert report["steps"][0]["changed"] == 2, report["steps"]      # train.py, score.py
    assert _mode(target, "modes-off", "train.py") == "100644"
    assert _mode(target, "modes-off", "recipe.env") == "100644"


def _node_commit(rd: Path, changes: dict) -> str:
    """Point node 0's recorded attempt commit at one made of `changes` on the run's base."""
    code = CodeRepo(rd)
    base = code.rev(BASE_BRANCH)
    commit = code.write_overlay_commit("refs/heads/nodes/n0-modes", base, changes,
                                       message="node 0 g0: modes\n", when=7)

    def point(row):
        if row["type"] == "code_snapshot" and row["data"].get("node_id") == 0:
            row["data"].update(status="recorded", commit=commit,
                               base={**(row["data"].get("base") or {}), "commit": base})
        return row
    _rewrite_rows(rd, point)
    return commit


def test_export_carries_a_nodes_chmod_with_and_without_a_content_change(tmp_path):
    """A node commit's 100644 -> 100755 survives the node step: with new bytes (exported as 100644
    before) and alone (skipped as `already_present` before). Then the target's OWN chmod survives
    a node that changed only the bytes — the run did not move the mode, so there is no conflict."""
    target, rd, _ = _run_from_target_worktree(tmp_path)
    _node_commit(rd, {"train.py": (b"print('LR: 0.03')\nprint('Batch: 32 (micro=8)')\n", True),
                      "recipe.env": (b"SEED=1\n", True)})
    report = export_code(rd, target, branch="node-modes", node="0")
    assert report["status"] == "created", report
    node_step = report["steps"][-1]
    assert node_step["kind"] == "node" and node_step["commit"], report["steps"]
    assert node_step["changed"] == 2 and node_step["already_present"] == 0, node_step
    assert _mode(target, "node-modes", "train.py") == "100755"
    assert "0.03" in _git(target, "show", "node-modes:train.py")
    assert _mode(target, "node-modes", "recipe.env") == "100755"
    _git(target, "checkout", "-q", "-b", "own-chmod", "node-modes~1")
    _git(target, "update-index", "--chmod=+x", "recipe.env")
    _git(target, "commit", "-qm", "chmod recipe")
    _node_commit(rd, {"recipe.env": (b"SEED=4\n", False)})
    report = export_code(rd, target, branch="kept", node="0", onto="own-chmod", include_base=False)
    assert report["status"] == "created", report
    assert _mode(target, "kept", "recipe.env") == "100755"
    assert _git(target, "show", "kept:recipe.env") == "SEED=4\n"


@MODE_BITS
def test_export_names_a_mode_conflict_instead_of_picking_a_side(tmp_path):
    """Both sides added `run.sh` with the same bytes and different modes: a conflict that names the
    modes, and no branch — never one side's mode silently."""
    def add_script(src):
        (src / "run.sh").write_text("echo hi\n")
        os.chmod(src / "run.sh", 0o755)
        _git(src, "add", "run.sh")                  # staged, never committed: the seed copies it
    target, rd, _ = _run_from_target_worktree(tmp_path, add_script)
    _git(target, "checkout", "-q", "-b", "moved", "main")
    (target / "run.sh").write_text("echo hi\n")
    os.chmod(target / "run.sh", 0o644)
    _git(target, "add", "run.sh")
    _git(target, "commit", "-qm", "add run.sh")
    report = export_code(rd, target, branch="clash", onto="moved", node="none")
    assert report["status"] == "conflict", report["steps"]
    step = report["steps"][0]
    assert step["conflicts"] == ["run.sh"] and step["commit"] is None, step
    assert step["mode_conflicts"] == [{"path": "run.sh", "base": None, "run": "100755",
                                       "target": "100644"}]
    assert _git(target, "branch", "--list", "clash") == ""


def test_merged_mode_truth_table():
    """The mode rule apart from the text: the run's move wins over an unmoved target, the target's
    move survives an unmoved run, agreement is agreement, and two different moves are None."""
    from looplab.engine.code_export import _merged_mode
    x, f, link = ("100755", "a"), ("100644", "a"), ("120000", "a")
    assert _merged_mode(f, x, f) == "100755"            # run chmod +x, target untouched
    assert _merged_mode(f, f, x) == "100755"            # target chmod +x, run untouched
    assert _merged_mode(f, x, x) == "100755"            # both
    assert _merged_mode(f, x, link) is None             # the target moved it elsewhere
    assert _merged_mode(None, x, f) is None             # both added, different modes
    assert _merged_mode(None, x, None) == "100755"      # a new path


def test_an_unsubmitted_agent_worktree_survives_mirror_wipe_restore_and_sync(tmp_path):
    """weekly-worktree-restore-loss (review 2026-10-15): the mirror skips `code/repo.git` and
    `code/worktrees` as "rebuilt by `code-status --sync`", but an agent worktree that was never
    injected is in no event — mirror -> restore -> sync lost `agent/draft`, its unique committed
    proposal and every uncommitted byte. Real git, real mirror, real tmpfs-style wipe."""
    import os
    import shutil
    from looplab.engine.code_worktree import AGENT_BACKUP_REL, agent_backup, list_worktrees
    from looplab.events.run_mirror import MirrorConfig, mirror_pass, restore_run
    _, task = _repo_task(tmp_path)
    r, d = task.build_roles()
    rd = tmp_path / "src" / "run"
    anyio.run(Engine(rd, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                     policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    wt = Path(add_worktree(rd, "draft")["path"])
    (wt / "train.py").write_text("print('LR: 0.03')\n")
    _git(wt, "commit", "-qam", "proposal")                  # the unique, unsubmitted commit
    proposal = _git(wt, "rev-parse", "HEAD").strip()
    (wt / "recipe.env").write_text("SEED=9\n")              # an uncommitted edit ...
    (wt / "notes.txt").write_text("try warmup\n")           # ... and an untracked new file
    status = _git(wt, "status", "--porcelain")
    overlay = overlay_from_worktree(rd, "draft")
    index = Path(_git(wt, "rev-parse", "--git-path", "index").strip())
    index_before = (index.stat().st_mtime_ns, index.read_bytes())
    objects_before = sorted(p.name for p in (rd / "code" / "repo.git" / "objects").rglob("*"))

    cfg = MirrorConfig(src_root=tmp_path / "src", dst_root=tmp_path / "dst", max_bytes_per_s=0,
                       max_files_per_s=0, run_extras=agent_backup)
    first = mirror_pass(cfg)
    assert first.stuck is None and not first.errors, first.as_dict()
    # Read-only on the run: the agent's index, its worktree and the run repository are untouched.
    assert (index.stat().st_mtime_ns, index.read_bytes()) == index_before
    assert _git(wt, "status", "--porcelain") == status
    assert sorted(p.name for p in (rd / "code" / "repo.git" / "objects").rglob("*")) == objects_before
    assert (cfg.dst_root / "run" / AGENT_BACKUP_REL / "agents.bundle").is_file()
    assert mirror_pass(cfg).runs["run"]["copied"] == 0      # unchanged worktree: no re-bundle

    shutil.rmtree(tmp_path / "src")                          # the pod restart wipes tmpfs
    os.makedirs(tmp_path / "src")
    assert not restore_run(cfg.dst_root, "run", cfg.src_root)["failures"]
    # Until a sync consumed it, the restored backup IS the backup: the producer defers to it.
    assert agent_backup(rd, tmp_path / "spool") == {}
    synced = sync_run(rd, restore_agents=True)
    assert synced["agent_worktrees"]["restored"] == ["draft"], synced["agent_worktrees"]
    assert not (rd / AGENT_BACKUP_REL).exists()              # consumed

    assert [m["name"] for m in list_worktrees(rd)] == ["draft"]
    assert CodeRepo(rd).rev(proposal) == proposal            # the unique commit resolves
    assert CodeRepo(rd).rev("refs/heads/agent/draft") == proposal
    assert _git(wt, "rev-parse", "HEAD").strip() == proposal
    assert (wt / "train.py").read_text() == "print('LR: 0.03')\n"
    assert (wt / "recipe.env").read_text() == "SEED=9\n"     # the dirty edit is back
    assert (wt / "notes.txt").read_text() == "try warmup\n"
    assert _git(wt, "status", "--porcelain") == status       # and reads as it did
    after = overlay_from_worktree(rd, "draft")
    assert (after["files"], after["deleted"]) == (overlay["files"], overlay["deleted"])
    assert sync_run(rd, restore_agents=True).get("agent_worktrees") is None  # nothing left
    # The restored worktree is backed up again by the next pass.
    assert not mirror_pass(cfg).errors
    assert (tmp_path / "src" / ".looplab-mirror" / "spool" / "run" / "agents.bundle").is_file()
    # Never overwrites: a second copy restored where something already sits at the worktree path
    # keeps the backup (pending) and the stray directory, and a later sync finishes the job.
    assert not restore_run(cfg.dst_root, "run", cfg.src_root, as_name="run2")["failures"]
    rd2 = tmp_path / "src" / "run2"
    (rd2 / "code" / "worktrees" / "draft").mkdir(parents=True)
    (rd2 / "code" / "worktrees" / "draft" / "mine.txt").write_text("keep\n")
    held = sync_run(rd2, restore_agents=True)["agent_worktrees"]
    assert held["pending"] and held["skipped"][0]["reason"] == "worktree_path_exists", held
    assert (rd2 / AGENT_BACKUP_REL / "agents.bundle").is_file()
    assert (rd2 / "code" / "worktrees" / "draft" / "mine.txt").read_text() == "keep\n"
    shutil.rmtree(rd2 / "code" / "worktrees" / "draft")
    assert sync_run(rd2, restore_agents=True)["agent_worktrees"]["restored"] == ["draft"]
    wt2 = Path(list_worktrees(rd2)[0]["path"])
    assert wt2 == rd2 / "code" / "worktrees" / "draft"       # re-pointed at the restored run
    assert (wt2 / "recipe.env").read_text() == "SEED=9\n"


def test_large_and_ignored_agent_files_ride_as_versioned_plain_files_and_heads_come_back(
        tmp_path, monkeypatch):
    """Large files are not git's: a checkpoint (ignored), an ignored dataset shard and a big edit
    of a tracked file are copied as PLAIN files named `<path>.v<version>` beside the bundle, a new
    version replaces the old name in the spool, and the restore lays each back. A worktree that
    had switched to a branch of its own comes back ON it; a detached one comes back detached."""
    import os
    import shutil
    import looplab.engine.code_worktree as cw
    from looplab.events.run_mirror import MirrorConfig, mirror_pass, restore_run
    monkeypatch.setattr(cw, "LARGE_FILE_BYTES", 64)          # "large" without writing 8 MiB
    _, task = _repo_task(tmp_path)
    r, d = task.build_roles()
    rd = tmp_path / "src" / "run"
    anyio.run(Engine(rd, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                     policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    wt = Path(add_worktree(rd, "draft")["path"])
    (wt / "checkpoint-1").mkdir()
    (wt / "checkpoint-1" / "model.pt").write_bytes(b"\x01" * 300)   # an ignored directory
    (wt / "shard.npy").write_bytes(b"\x02" * 200)                    # an ignored file
    (wt / "train.py").write_text("x = 1\n" * 40)                     # a LARGE tracked edit
    (wt / "notes.txt").write_text("small\n")                         # stays in git
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "a.pyc").write_bytes(b"junk")              # never backed up
    status = _git(wt, "status", "--porcelain")
    side = Path(add_worktree(rd, "side")["path"])
    _git(side, "switch", "-q", "-c", "exp")                          # a branch of its own
    (side / "train.py").write_text("print('exp')\n")
    _git(side, "commit", "-qam", "exp work")
    exp_head = _git(side, "rev-parse", "HEAD").strip()
    loose = Path(add_worktree(rd, "loose")["path"])
    _git(loose, "commit", "-q", "--allow-empty", "-m", "detached work")
    loose_head = _git(loose, "rev-parse", "HEAD").strip()
    _git(loose, "checkout", "-q", "--detach", loose_head)
    _git(loose, "branch", "-q", "-f", "agent/loose", "HEAD~1")     # its branch stays behind

    cfg = MirrorConfig(src_root=tmp_path / "src", dst_root=tmp_path / "dst", max_bytes_per_s=0,
                       max_files_per_s=0, run_extras=cw.agent_backup)
    assert not mirror_pass(cfg).errors
    # Mirrored straight from the worktree (no spool copy) under versioned names.
    spool = tmp_path / "src" / ".looplab-mirror" / "spool" / "run" / "files"
    assert not spool.exists()
    dst_files = cfg.dst_root / "run" / "code" / "agent_backup" / "files" / "draft"
    listing = lambda: sorted(p.relative_to(dst_files).as_posix()
                             for p in dst_files.rglob("*") if p.is_file())
    stored = listing()
    assert [s.rsplit(".v", 1)[0] for s in stored] == ["checkpoint-1/model.pt", "shard.npy",
                                                      "train.py"], stored
    # A new version of the checkpoint is a new name; the old one's copy leaves the destination.
    (wt / "checkpoint-1" / "model.pt").write_bytes(b"\x03" * 301)
    assert not mirror_pass(cfg).errors
    now = listing()
    assert len(now) == 3 and set(now) != set(stored)

    shutil.rmtree(tmp_path / "src")
    os.makedirs(tmp_path / "src")
    assert not restore_run(cfg.dst_root, "run", cfg.src_root)["failures"]
    report = sync_run(rd, restore_agents=True)["agent_worktrees"]
    assert sorted(report["restored"]) == ["draft", "loose", "side"], report
    assert not report["pending"], report
    assert (wt / "checkpoint-1" / "model.pt").read_bytes() == b"\x03" * 301
    assert (wt / "shard.npy").read_bytes() == b"\x02" * 200
    assert (wt / "train.py").read_text() == "x = 1\n" * 40
    assert (wt / "notes.txt").read_text() == "small\n"
    assert not (wt / "__pycache__").exists()
    assert _git(wt, "status", "--porcelain") == status.replace("?? __pycache__/\n", "")
    assert _git(side, "symbolic-ref", "HEAD").strip() == "refs/heads/exp"   # back ON its branch
    assert _git(side, "rev-parse", "HEAD").strip() == exp_head
    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=loose).returncode == 1
    assert _git(loose, "rev-parse", "HEAD").strip() == loose_head          # detached, as it was


# ------------------------------------------------- 2026-10-16: base parented on the operator's HEAD

def _history_repo_task(tmp_path):
    """`_repo_task` with HISTORY: two operator commits under the uncommitted edit, so a worktree's
    `git log` has something of the operator's to show."""
    repo, task = _repo_task(tmp_path)
    (repo / "recipe.env").write_text("SEED=1\nWARMUP=10\n")
    _git(repo, "commit", "-qm", "operator: add warmup to the recipe", "--", "recipe.env")
    return repo, task


def _git_dir_snapshot(repo: Path) -> dict:
    """Every file under the operator's `.git` (refs, objects, config, index, logs …) by bytes."""
    out = {}
    for p in sorted((repo / ".git").rglob("*")):
        if p.is_file():
            out[p.relative_to(repo).as_posix()] = p.read_bytes()
    return out


def test_the_first_base_is_parented_on_the_operators_head_and_the_operator_repo_is_untouched(
        tmp_path):
    from looplab.engine.code_origin import ORIGIN_REL
    repo, task = _history_repo_task(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    before, count = _git_dir_snapshot(repo), _git(repo, "rev-list", "--count", "--all").strip()
    status_before = _git(repo, "status", "--porcelain")
    anyio.run(_engine(tmp_path, task).run)
    rd = tmp_path / "run"
    code = CodeRepo(rd)
    base = code.rev(BASE_BRANCH)
    assert code.commit_parents(base) == [head]
    snap = [e["data"] for e in _events(rd) if e["type"] == "code_snapshot"][0]
    assert snap["version"] == 2 and snap["base"]["origin_parent"] == head
    origin = snap["base"]["origin"]
    assert (origin["status"], origin["via"], origin["depth"]) == ("parented", "fetched", 200), origin
    assert origin["sidecar"] == "written"
    assert (rd / ORIGIN_REL / f"{head}.pack").is_file()
    # The tree is still EXACTLY the seed (materialization reads the archive, never this tree) ...
    assert code.blob(base, "train.py") == b"print('LR: 0.02')\nprint('Batch: 32 (micro=8)')\n"
    # ... and the diff from the operator's HEAD is exactly what was never committed (doc 37 §5d).
    assert code.changed(head, base) == [{"path": "score.py", "status": "A"},
                                        {"path": "train.py", "status": "M"}]
    assert "-print('LR: 0.01')\n+print('LR: 0.02')" in code.diff(head, base, paths=["train.py"])[0]
    # A worktree of the private repository shows the operator's history, and blame reaches it.
    wt = Path(add_worktree(rd, "hist")["path"])
    log = _git(wt, "log", "--format=%s")
    assert "operator: add warmup to the recipe\ninit\n" in log, log
    assert "\nsummary init\n" in _git(wt, "blame", "--porcelain", "-L", "2,2", "train.py")
    assert subprocess.run(["git", "--git-dir", str(code.path), "fsck", "--no-dangling"],
                          capture_output=True).returncode == 0
    # The operator's repository: byte-identical .git, the same commits, still dirty as left.
    assert _git_dir_snapshot(repo) == before
    assert _git(repo, "rev-list", "--count", "--all").strip() == count == "2"
    assert _git(repo, "status", "--porcelain") == status_before


def test_a_rebuild_reproduces_the_parented_base_with_or_without_the_operator_repo(tmp_path):
    """The run mirror skips `code/repo.git`: a restore rebuilds it with `sync_run`. The base id is a
    function of the log and the run's own files (`code/origin/<sha>.pack` carries the parent), so it
    is the SAME with the operator's repository gone — measured: a `shallow` line naming an ABSENT
    parent leaves `log`/`rev-list`/`fsck`/`bundle` failing, so the parent's objects must be there."""
    import shutil
    repo, task = _history_repo_task(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    anyio.run(_engine(tmp_path, task).run)
    rd = tmp_path / "run"
    (rd / "task.snapshot.json").write_text(task.model_dump_json(), encoding="utf-8")  # as the CLI
    base = CodeRepo(rd).rev(BASE_BRANCH)
    # The operator moved on since: the recorded commit is no ref tip any more, and is still fetched
    # (by sha over protocol v2; v0 refuses an unadvertised object).
    _git(repo, "commit", "--allow-empty", "-qm", "operator: later work")
    for name, keep_source in (("restored", True), ("restored-offline", False)):
        other = tmp_path / name
        shutil.copytree(rd, other, ignore=shutil.ignore_patterns("repo.git", "worktrees"))
        if not keep_source:
            repo.rename(tmp_path / "moved-away")
        sync_run(other)
        code = CodeRepo(other)
        assert code.rev(BASE_BRANCH) == base, name
        assert subprocess.run(["git", "--git-dir", str(code.path), "fsck", "--no-dangling"],
                              capture_output=True).returncode == 0, name
        log = code.git("log", "--format=%H", BASE_BRANCH).stdout.decode().split()
        # With the source: its history fetched again; without: the parent alone, a shallow cut.
        assert log[:2] == [base, head] and len(log) == (3 if keep_source else 2), (name, log)
        assert (code.shallow() == [head]) is (not keep_source)


def test_an_agent_backup_of_a_shallow_run_repository_bundles_and_restores(tmp_path, monkeypatch):
    """A history fetched to a depth leaves the run repository SHALLOW; the agent backup's scratch
    store walks it through an alternate and failed `bundle create` on the never-fetched parent until
    it carried the same cut. Restored with the operator's repository gone, the worktree's history
    still reads: the bases rebuild first (the sidecar), the cut is laid back from the backup."""
    import shutil
    from looplab.engine import code_origin
    from looplab.engine.code_worktree import AGENT_BACKUP_REL, agent_backup, list_worktrees
    monkeypatch.setattr(code_origin, "HISTORY_DEPTH", 1)
    repo, task = _history_repo_task(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    anyio.run(_engine(tmp_path, task).run)
    rd = tmp_path / "run"
    assert CodeRepo(rd).shallow() == [head]
    wt = Path(add_worktree(rd, "draft")["path"])
    (wt / "train.py").write_text("print('LR: 0.05')\n")
    _git(wt, "commit", "-qam", "agent: lr 0.05")
    proposal = _git(wt, "rev-parse", "HEAD").strip()
    spool = tmp_path / "spool"
    files = agent_backup(rd, spool)
    assert json.loads((spool / "agents.json").read_text())["shallow"] == [head]
    restored = tmp_path / "restored"
    shutil.copytree(rd, restored, ignore=shutil.ignore_patterns("repo.git", "worktrees"))
    for rel, src in files.items():
        (restored / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, restored / rel)
    repo.rename(tmp_path / "moved-away")
    assert sync_run(restored, restore_agents=True)["agent_worktrees"]["restored"] == ["draft"]
    assert not (restored / AGENT_BACKUP_REL).exists()
    back = Path(list_worktrees(restored)[0]["path"])
    assert _git(back, "log", "--format=%H").split() == [proposal, CodeRepo(rd).rev(BASE_BRANCH),
                                                        head]


def test_a_version_1_run_rebuilds_its_root_base_byte_for_byte(tmp_path):
    """Per-version: a run whose rows are version 1 (every run before 2026-10-16) rebuilds the ROOT
    base it always had — same message, same tree, no parent, the same id — although its source is a
    git repository the new rule would parent on."""
    import shutil
    from looplab.engine.code_identity import _base_message, _run_meta, archive_files
    _, task = _history_repo_task(tmp_path)
    anyio.run(_engine(tmp_path, task).run)
    rd = tmp_path / "run"
    old = tmp_path / "v1"
    shutil.copytree(rd, old, ignore=shutil.ignore_patterns("code"))

    def as_v1(row):
        if row["type"] == "code_snapshot":
            row["data"]["version"] = 1
            for key in ("origin_parent", "origin"):
                row["data"].get("base", {}).pop(key, None)
        return row
    _rewrite_rows(old, as_v1)
    sync_run(old)
    code = CodeRepo(old)
    base = code.rev(BASE_BRANCH)
    assert base and code.commit_parents(base) == []
    events = EventStore(old / "events.jsonl").read_all()
    lineage = base_lineage(events)
    digest = next(iter(lineage))
    run_id, started = _run_meta(events)
    expected = CodeRepo(tmp_path / "expected").ensure().write_tree_commit(
        "refs/looplab/bases/" + digest, archive_files(old, events, digest),
        message=_base_message(run_id, digest, lineage[digest], started),
        when=int(lineage[digest]["ts"] or 0))
    assert base == expected                              # the version-1 algorithm, byte for byte
    assert "Looplab-Source-Commit" not in code.git("log", "-1", "--format=%B", base).stdout.decode()


def test_seed_base_of_follows_the_first_parent_through_a_merge(tmp_path):
    """`rev-list --topo-order` lists a merge's second-parent side first, so a start that is a merge
    of node A (on base X) and node B (on base Y) answered Y — the overlay was then diffed against the
    wrong base. The first-parent chain lands on X."""
    from looplab.engine.code_identity import BASE_REF
    from looplab.engine.code_worktree import _seed_base_of
    code = CodeRepo(tmp_path / "run").ensure()
    x = code.write_tree_commit(BASE_REF + "a" * 64, [("a.py", b"x\n", False)], message="X\n", when=1)
    y = code.write_tree_commit(BASE_REF + "b" * 64, [("a.py", b"y\n", False)], message="Y\n", when=2)
    a = code.write_overlay_commit("refs/looplab/attempts/n1/g0/s1", x,
                                  {"b.py": (b"A\n", False)}, message="A\n", when=3)
    b = code.write_overlay_commit("refs/looplab/attempts/n2/g0/s2", y,
                                  {"c.py": (b"B\n", False)}, message="B\n", when=4)
    merge = code.write_overlay_commit("refs/heads/agent/m", a, {"c.py": (b"B\n", False)},
                                      message="merge\n", when=5, merges=[b])
    assert code.commit_parents(merge) == [a, b]
    topo = code.git("rev-list", "--topo-order", merge).stdout.decode().split()
    assert [s for s in topo if s in (x, y)][0] == y      # the defect this guards against
    assert _seed_base_of(code, merge) == x
    assert _seed_base_of(code, b) == y and _seed_base_of(code, x) == x


def _inject(rd: Path, data: dict) -> dict:
    """The real intake (`normalize_control`), then the durable append the command service makes."""
    from looplab.serve.control_validation import normalize_control
    srv = SimpleNamespace(run_dir=lambda run_id: rd.parent / run_id,
                          state=lambda path: fold(EventStore(Path(path) / "events.jsonl").read_all()))
    out = normalize_control(srv, rd, "inject_node", data)
    EventStore(rd / "events.jsonl").append("inject_node", out)
    return out


def _injected(rd: Path, files: dict) -> int:
    nodes = fold(EventStore(rd / "events.jsonl").read_all()).nodes
    return next(n.id for n in nodes.values() if n.operator == "manual" and n.files == files)


def test_a_real_merge_becomes_git_parents_and_an_ensemble_only_a_trailer(tmp_path):
    """Merge parents, honestly. An agent that ran `git merge nodes/nB` in a worktree from `node:A`
    and injected it: the attempt commit's parents are [base, A, B], each verified in the worktree's
    history, so `git merge` of that node elsewhere knows both are merged. A node that only NAMES
    several parents (an ensemble, a merge operator, an inject with `parent_ids`) keeps ONE git
    parent and names the others in `Looplab-Co-Parents` — a synthesized merge would tell git the
    other branch is merged when its change may have been dropped."""
    from fastapi import HTTPException
    _, task = _repo_task(tmp_path)
    rd = tmp_path / "run"
    store = EventStore(rd / "events.jsonl")
    one = {"recipe.env": "SEED=2\n"}
    two = {"train.py": "print('LR: 0.03')\nprint('Batch: 32 (micro=8)')\n"}
    for files in (one, two):
        store.append("inject_node", {"idea": {"operator": "manual", "params": {}}, "files": files})
    anyio.run(_engine(tmp_path, task, max_nodes=3).run)
    a, b = _injected(rd, one), _injected(rd, two)
    code = CodeRepo(rd)
    base = code.rev(BASE_BRANCH)
    a_commit, b_commit = code.rev(f"refs/heads/nodes/n{a}"), code.rev(f"refs/heads/nodes/n{b}")
    wt = Path(add_worktree(rd, "merger", start=f"node:{a}")["path"])
    _git(wt, "merge", "-q", "--no-edit", f"nodes/n{b}")
    merge_head = _git(wt, "rev-parse", "HEAD").strip()
    store.append("run_reopened", {})
    store.append("budget_extend", {"add_nodes": 2})
    with pytest.raises(HTTPException) as forged:          # server-stamped, never submitted
        _inject(rd, {"idea": {"operator": "manual"}, "code_from": {"worktree": "merger"},
                     "code_commit": a_commit})
    assert forged.value.detail["code"] == "code_commit_forged"
    sent = _inject(rd, {"idea": {"operator": "manual", "params": {}},
                        "code_from": {"worktree": "merger"}, "parent_ids": [a, b]})
    assert sent["code_commit"] == merge_head and "code_from" not in sent
    both = {**one, **two}
    assert sent["files"] == both
    ensemble = {"recipe.env": "SEED=4\n"}
    store.append("inject_node", {"idea": {"operator": "manual", "params": {}}, "files": ensemble,
                                 "parent_ids": [a, b]})
    anyio.run(_engine(tmp_path, task, max_nodes=3).run)
    c, d = _injected(rd, both), _injected(rd, ensemble)
    created = [e.data for e in store.read_all() if e.type == "node_created"
               and e.data["node_id"] == c]
    assert created[0]["code_commit"] == merge_head
    rows = {e.data["node_id"]: e.data for e in store.read_all() if e.type == "code_snapshot"}
    c_commit, d_commit = rows[c]["commit"], rows[d]["commit"]
    assert code.commit_parents(c_commit) == [base, a_commit, b_commit]
    assert [m["node_id"] for m in rows[c]["merge_parents"]] == [a, b]
    for parent in (a_commit, b_commit):
        assert code.git("merge-base", "--is-ancestor", parent, c_commit,
                        check=False).returncode == 0
    # A fresh worktree from the base: merging that node brings BOTH changes, as git understands it.
    fresh = Path(add_worktree(rd, "fresh")["path"])
    _git(fresh, "merge", "-q", "--no-edit", f"nodes/n{c}")
    assert (fresh / "recipe.env").read_text() == "SEED=2\n"
    assert (fresh / "train.py").read_text().startswith("print('LR: 0.03')")
    # ... and from node A it is a fast-forward: A is already in it.
    side = Path(add_worktree(rd, "side", start=f"node:{a}")["path"])
    assert "Fast-forward" in _git(side, "merge", "--no-edit", f"nodes/n{c}")
    # The ensemble: one git parent, the co-parent named — with the commit the log recorded.
    assert code.commit_parents(d_commit) == [base]
    message = code.git("log", "-1", "--format=%B", d_commit).stdout.decode()
    assert f"Looplab-Co-Parents: n{b}={b_commit}\n" in message, message
    assert rows[d]["co_parents"] == [{"node_id": b, "commit": b_commit}]
    assert "merge_parents" not in rows[d]
    # A worktree started on that real merge still diffs against its FIRST parent's base.
    assert overlay_from_worktree(rd, "side")["overlay_base"] == base
