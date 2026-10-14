"""END TO END over a multi-stage, GPU-LIKE run on a CPU stub (doc 79 §9 leftover, 2026-10-14):
three stages (prepare -> train -> score) on a git source with uncommitted edits and an untracked
scorer, a two-device pool the engine pins the evaluation into (the child sees CUDA_VISIBLE_DEVICES),
and a second lifecycle with an overlay — every attempt comes out as a commit of the run's private
repository, the evaluated one with the effective configuration its train stage printed, and every
stage's progress beacon closed with its outcome. (The repair-through-the-lane case is
`tests/test_code_identity.py`'s end-to-end test.)"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import anyio

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.engine import orchestrator as _orch
from looplab.engine.code_identity import code_status
from looplab.engine.code_repo import CodeRepo
from looplab.engine.orchestrator import Engine
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

_M = {"kind": "stdout_json", "key": "metric"}
TRAIN = """\
import json, os, pathlib
cfg = dict(l.split("=", 1) for l in pathlib.Path("recipe.env").read_text().split() if "=" in l)
lr, batch, micro = cfg.get("LR", "2e-4"), cfg.get("BATCH", "1024"), cfg.get("MICRO", "128")
print(f"LR: {lr}")
print(f"Batch: {batch} (micro={micro})")
print("GPUS_SEEN=" + os.environ.get("CUDA_VISIBLE_DEVICES", "<none>"))
assert pathlib.Path("prepared/data.json").exists(), "prepare did not run"
pathlib.Path("ckpt").mkdir(exist_ok=True)
pathlib.Path("ckpt/model.json").write_text(json.dumps({"lr": float(lr), "batch": int(batch)}))
"""
SCORE = """\
import json, pathlib
m = json.loads(pathlib.Path("ckpt/model.json").read_text())
print(json.dumps({"metric": round(1.0 / (1 + abs(m["lr"] - 3e-4) * 1e4), 6)}))
"""


def _git(cwd, *argv):
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _task(tmp_path) -> RepoTask:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "prepare.py").write_text("import pathlib, json\npathlib.Path('prepared').mkdir("
                                     "exist_ok=True)\npathlib.Path('prepared/data.json')"
                                     ".write_text(json.dumps([1, 2, 3]))\n")
    (repo / "train.py").write_text(TRAIN)
    (repo / "recipe.env").write_text("LR=2e-4\nBATCH=1024\nMICRO=128\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    (repo / "recipe.env").write_text("LR=2e-4\nBATCH=1024\nMICRO=128\n# uncommitted\n")
    (repo / "score.py").write_text(SCORE)                         # untracked, protected
    stages = [{"name": "prepare", "command": [sys.executable, "prepare.py"],
               "expect": {"files": ["prepared/data.json"]}},
              {"name": "train", "command": [sys.executable, "train.py"], "role": "training"},
              {"name": "score", "command": [sys.executable, "score.py"]}]
    return RepoTask(id="ms", direction="max", editable_path=str(repo), protect=["score.py"],
                    edit_surface=["recipe.env", "train.py"],
                    eval=EvalSpec(command=[sys.executable, "score.py"], metric=_M, stages=stages))


def test_a_multi_stage_gpu_like_run_records_commits_and_effective_config(tmp_path, monkeypatch):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    monkeypatch.setattr(RepoTask, "gpu_capable", lambda self: True, raising=False)
    task = _task(tmp_path)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 eval_parallel=2)       # a pool of 2 with AUTO width: an undeclared node gets 1 GPU
    anyio.run(eng.run)
    rd = tmp_path / "run"
    rows = [json.loads(x) for x in (rd / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "node_evaluated" for e in rows), [e["type"] for e in rows][-12:]
    stages = [(e["data"].get("name"), e["data"].get("status")) for e in rows
              if e["type"] == "stage_finished"]
    assert [s for s, _ in stages][:3] == ["prepare", "train", "score"]
    train_log = (rd / "nodes" / "node_0" / "train.log").read_text()
    assert "GPUS_SEEN=" in train_log and "GPUS_SEEN=<none>" not in train_log, train_log
    snaps = [e["data"] for e in rows if e["type"] == "code_snapshot"]
    assert snaps and all(s["status"] == "recorded" for s in snaps)
    repo = CodeRepo(rd)
    base = repo.rev("base")
    assert repo.blob(base, "recipe.env").endswith(b"# uncommitted\n")     # the WORKING tree
    assert repo.blob(base, "score.py") is not None                         # untracked scorer
    cfg = [e["data"] for e in rows if e["type"] == "effective_config"]
    assert cfg and cfg[-1]["effective"]["lr"] == 2e-4
    assert cfg[-1]["effective"]["batch_size"] == 1024.0
    assert cfg[-1]["effective"]["micro_batch_size"] == 128.0
    # The phase beacons of every stage closed with how it left (doc 78 leftover).
    closes = [e["data"] for e in rows if e["type"] == "phase_progress"
              and e["data"].get("status") == "finished" and e["data"].get("stage") == "eval"]
    assert {c.get("name") for c in closes} >= {"prepare", "train", "score"}
    assert all(c.get("outcome") == "ok" for c in closes if c.get("name") in ("prepare", "score"))

    # A second attempt with an overlay: its own commit, parented on the base, its own config.
    node = eng.store.read_all()
    from looplab.events.replay import fold
    seeded = fold(node).nodes[0].model_copy(deep=True)
    seeded.id, seeded.files = 5, {"recipe.env": "LR=3e-4\nBATCH=512\nMICRO=64\n"}
    eng._materialize(seeded, rd / "nodes" / "node_5")
    last = [json.loads(x)["data"] for x in (rd / "events.jsonl").read_text().splitlines()
            if json.loads(x)["type"] == "code_snapshot"][-1]
    assert last["node_id"] == 5 and repo.rev(last["commit"] + "^") == base
    assert repo.blob(last["commit"], "recipe.env") == b"LR=3e-4\nBATCH=512\nMICRO=64\n"
    status = code_status(rd, task=task)
    assert {a["node_id"] for a in status["attempts"]} >= {0}
    assert status["upstream"]["readiness"]["ready"] is False
