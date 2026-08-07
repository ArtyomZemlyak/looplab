"""The operator's PROTECTED files have to BE in the node workdir the protected `score` stage runs in.

Two independent holes met on `runs/rubert-dr-0807`: the task protected `looplab_eval.py`, the engine
appended `python looplab_eval.py` as the final protected `score` stage, and the node workdir never
contained the file — `seed_mode="auto"` copies git-TRACKED files and the scorer had never been
committed. It took until this run to surface only because no earlier rubert run ever REACHED the
score stage. `protect` was never the discriminator anyone assumed: it governs WRITES, and nothing
made it govern MATERIALIZATION.

The second hole is why nothing caught it from the agent side: the file the Developer would have to
create to fix this is precisely the file the write gate refuses. So the failure is unrepairable by
construction and must be reported to the OPERATOR, not fed to the repair loop — and it must be
detected BEFORE the pipeline runs, because `score` is last and the node had already paid for a full
train when it died in 0.06 s.

Everything here drives the real seeder / the real engine over a real git repo (tier 1): no source
pins, because what has to hold is "the file is on disk when the command runs", not "a call appears".
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import anyio

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.engine.orchestrator import Engine
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

_M = {"kind": "stdout_json", "key": "metric"}

# What the untracked operator scorer prints. A distinctive value so "the metric was read" cannot be
# confused with a default.
_SCORER = 'import json\nprint(json.dumps({"metric": 7.5}))\n'


def _git_repo(root: Path, tracked: dict[str, str], untracked: dict[str, str] | None = None) -> Path:
    """A real git repo: `tracked` committed, `untracked` written but never added. The distinction IS
    the bug's mechanism, so it is created for real rather than simulated by stubbing `git ls-files`."""
    root.mkdir(parents=True, exist_ok=True)
    for name, text in tracked.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", *tracked], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
                   cwd=root, check=True, capture_output=True)
    for name, text in (untracked or {}).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def _engine(tmp_path: Path, task: RepoTask, **kw) -> Engine:
    r, d = task.build_roles()
    return Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)


def _node_errors(run_dir: Path) -> list[str]:
    out: list[str] = []
    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
        ev = json.loads(line)
        if ev.get("type") == "node_failed":
            out.append(str(ev.get("data", {}).get("error", "")))
    return out


# ---------------------------------------------------------------- root cause 1: materialization

def test_an_untracked_protected_scorer_is_still_in_the_node_workdir(tmp_path):
    """The measured failure, end to end: the eval command runs and its metric is read.

    `seed_mode` is left at its default ("" -> the run-wide "auto"), so this is the shipped
    configuration — a tracked `train.py` and an UNTRACKED `looplab_eval.py`, exactly the
    dense-retrieval testbed's shape. Before `seed_protected_files` the node died with
    `python: can't open file '<workdir>/looplab_eval.py'` and the run finished with no best node.
    """
    repo = _git_repo(tmp_path / "repo", {"train.py": "print('train')\n"},
                     {"looplab_eval.py": _SCORER})
    task = RepoTask(id="p", direction="max", editable_path=str(repo), protect=["looplab_eval.py"],
                    eval=EvalSpec(command=[sys.executable, "looplab_eval.py"], metric=_M))
    eng = _engine(tmp_path, task)
    state = anyio.run(eng.run)

    best = state.best()
    assert best is not None and best.metric == 7.5, _node_errors(tmp_path / "run")
    # …and it is the OPERATOR's bytes that ran, not something reconstructed.
    assert (tmp_path / "run" / "nodes" / "node_0" / "looplab_eval.py").read_text(
        encoding="utf-8") == _SCORER


def test_the_protected_seed_is_recorded_in_workspace_seeded(tmp_path):
    """An operator debugging "why is my scorer missing" must be able to SEE the decision in the log —
    the same reason the tracked-vs-copytree seed mode is recorded there."""
    repo = _git_repo(tmp_path / "repo", {"train.py": "print('t')\n"},
                     {"looplab_eval.py": _SCORER})
    task = RepoTask(id="p", direction="max", editable_path=str(repo), protect=["looplab_eval.py"],
                    eval=EvalSpec(command=[sys.executable, "looplab_eval.py"], metric=_M))
    eng = _engine(tmp_path, task)
    eng._seed_workspace(tmp_path / "ws")

    seeded = [json.loads(x)["data"]["materialized"]
              for x in (tmp_path / "run" / "events.jsonl").read_text(encoding="utf-8").splitlines()
              if json.loads(x)["type"] == "workspace_seeded"][-1]
    assert any("protected[1]:looplab_eval.py" in row for row in seeded), seeded
    assert any("[auto]:1 tracked" in row for row in seeded), seeded    # the scorer was NOT tracked


def test_seeding_protected_files_never_shadows_or_writes_through_a_mount(tmp_path):
    """`protect` entries are copied into the workspace root, where the read-only data mounts also
    live. Two ways that could go wrong, both refused:

      * a `datasets/…` protect entry materializing BEFORE the shadow guard would create a top-level
        `datasets/` and raise the mount-collision RuntimeError on a perfectly valid task — the guard
        has no try/except above it, so that aborts every node with no terminal event;
      * the same entry materializing AFTER the mount would follow the symlink and write into the
        OPERATOR'S ORIGINAL dataset.
    """
    repo = _git_repo(tmp_path / "repo", {"train.py": "print('t')\n"},
                     {"datasets/labels.csv": "protected\n"})
    real = tmp_path / "real_data"
    real.mkdir()
    (real / "x.csv").write_text("1,2,3\n", encoding="utf-8")
    task = RepoTask(id="p", direction="max", editable_path=str(repo),
                    protect=["datasets/labels.csv"], data={"datasets": str(real)},
                    eval=EvalSpec(command=[sys.executable, "train.py"], metric=_M))
    eng = _engine(tmp_path, task)

    wd = tmp_path / "ws"
    eng._seed_workspace(wd)                             # must NOT raise the collision RuntimeError
    assert (wd / "datasets" / "x.csv").is_file()        # the REAL mount, not the repo's directory
    assert not (real / "labels.csv").exists()           # the original dataset was not written into


def test_tree_protect_entries_and_out_of_repo_matches_are_skipped(tmp_path):
    """A `dir/**` entry is a mounted data source (added by `RepoTask._protected_names`), never a file
    to copy; and a protected name whose match resolves outside the editable source is dropped rather
    than dragged into the workdir."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("print('secret')\n", encoding="utf-8")
    repo = _git_repo(tmp_path / "repo", {"train.py": "print('t')\n"})
    (repo / "escape.py").symlink_to(outside / "secret.py")
    (repo / "tree").mkdir()
    (repo / "tree" / "a.txt").write_text("a\n", encoding="utf-8")

    seeder = _engine(tmp_path, RepoTask(
        id="p", direction="max", editable_path=str(repo),
        eval=EvalSpec(command=[sys.executable, "train.py"], metric=_M))).workspace
    wd = tmp_path / "ws"
    wd.mkdir()
    copied = seeder.seed_protected_files(repo, wd, ["tree/**", "escape.py", "../outside/secret.py",
                                                    "absent.py"])
    assert copied == []
    assert not (wd / "tree").exists() and not (wd / "escape.py").exists()
    assert not (wd / "secret.py").exists() and not (tmp_path / "outside" / "secret.py").is_symlink()


def test_a_glob_protect_entry_seeds_every_match(tmp_path):
    repo = _git_repo(tmp_path / "repo", {"train.py": "print('t')\n"},
                     {"graders/a.py": "a\n", "graders/b.py": "b\n"})
    seeder = _engine(tmp_path, RepoTask(
        id="p", direction="max", editable_path=str(repo),
        eval=EvalSpec(command=[sys.executable, "train.py"], metric=_M))).workspace
    wd = tmp_path / "ws"
    wd.mkdir()
    assert seeder.seed_protected_files(repo, wd, ["graders/*.py"]) == ["graders/a.py",
                                                                       "graders/b.py"]
    assert (wd / "graders" / "b.py").read_text(encoding="utf-8") == "b\n"


# -------------------------------------------------- root cause 2: the unrepairable failure, named

# A `train` stage that leaves a marker: whether it RAN is how "the preflight fired before the
# pipeline" is observed, rather than trusting a message.
_MARKER = "import pathlib\npathlib.Path('trained.marker').write_text('x')\nprint('trained')\n"


def _staged_task(repo: Path, protect: list[str]) -> RepoTask:
    return RepoTask(id="p", direction="max", editable_path=str(repo), protect=protect,
                    eval=EvalSpec(command=[sys.executable, "looplab_eval.py"], metric=_M))


def test_a_missing_protected_script_fails_the_node_before_the_train_stage_runs(tmp_path):
    """The operator protected a scorer that is not in the repo at all — "one eval command but no
    file". Nothing can materialize it and no edit can create it, so the node must fail IMMEDIATELY
    with a message aimed at the operator, not after paying for the train stage."""
    repo = _git_repo(tmp_path / "repo", {
        "mk.py": _MARKER,
        "looplab_stages.json": json.dumps(
            {"stages": [{"name": "train", "command": [sys.executable, "mk.py"]}]}),
    })
    eng = _engine(tmp_path, _staged_task(repo, ["looplab_eval.py"]))
    anyio.run(eng.run)

    errors = _node_errors(tmp_path / "run")
    assert errors and "PROTECTED" in errors[0] and "looplab_eval.py" in errors[0]
    assert "seed_mode" in errors[0] and "`protect`" in errors[0]     # both operator-side remedies
    assert not (tmp_path / "run" / "nodes" / "node_0" / "trained.marker").exists()


def test_a_missing_EDITABLE_script_still_goes_to_the_eval_and_the_repair_loop(tmp_path):
    """The narrowness that keeps the preflight honest. The same missing scorer, NOT protected, is a
    file the Developer may author — so the old path stands: the pipeline runs, the train stage's work
    is done and kept, and the failure is an ordinary repairable eval error. A preflight that fired
    here would turn every "the agent hasn't written its entrypoint yet" node into an operator
    refusal, which is the whole normal workflow of a repo task."""
    repo = _git_repo(tmp_path / "repo", {
        "mk.py": _MARKER,
        "looplab_stages.json": json.dumps(
            {"stages": [{"name": "train", "command": [sys.executable, "mk.py"]}]}),
    })
    eng = _engine(tmp_path, _staged_task(repo, []))
    anyio.run(eng.run)

    errors = _node_errors(tmp_path / "run")
    assert errors and "PROTECTED" not in errors[0]
    assert (tmp_path / "run" / "nodes" / "node_0" / "trained.marker").exists()


def test_the_preflight_reads_the_protected_name_through_the_eval_cwd(tmp_path):
    """`protected_names` are WORKSPACE-relative, the stage scripts are named relative to the eval
    `cwd`. Joining them without that offset would both miss a real hit under a subdir cwd and
    manufacture one at the root, so the namespacing is checked rather than assumed."""
    repo = _git_repo(tmp_path / "repo", {"sub/keep.txt": "k\n"})
    task = RepoTask(id="p", direction="max", editable_path=str(repo), protect=["sub/score.py"],
                    eval=EvalSpec(command=[sys.executable, "score.py"], cwd="sub", metric=_M))
    eng = _engine(tmp_path, task)
    wd = tmp_path / "ws"
    (wd / "sub").mkdir(parents=True)

    hits = eng._unrunnable_protected_scripts([("score", [sys.executable, "score.py"])],
                                             str(wd), str(wd / "sub"))
    assert hits == [("score", "sub/score.py")]
    # Present on disk under that cwd -> no hit at all (the check is about ABSENCE, not protection).
    (wd / "sub" / "score.py").write_text("x\n", encoding="utf-8")
    assert eng._unrunnable_protected_scripts([("score", [sys.executable, "score.py"])],
                                             str(wd), str(wd / "sub")) == []
