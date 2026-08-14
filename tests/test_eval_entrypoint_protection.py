"""The operator's eval entrypoint is the operator's — the FILE, not only the stage.

`adapters/repo_developer.py::_REPO_DEV_SYSTEM_BODY` tells the Developer, verbatim: "the operator's
`cmd` is APPENDED automatically as the final, protected `score` stage — you CANNOT rewrite how the
run is scored (that's the trust boundary), only add work before it." That was true of the STAGE and
false of the FILE. Caught live on `runs/rubertlite-dr-unified-v6` node 0 — `cmd:
["python","-m","vectorsearch.test"]`, `protect: []`, `edit_surface: ["**/*"]` — where the Developer
edited `vectorsearch/test.py` to add exactly the pattern the same prompt forbids two paragraphs
earlier:

    checkpoint_path = config.test.retriever.model_settings.checkpoint_path
    if checkpoint_path is None or not (Path(checkpoint_path) / "model.safetensors").exists():
        subprocess.run([sys.executable, "-m", "vectorsearch.train"], check=True)

The train stage had already finished and printed RECALL@100: 0.708708. The scorer's checkpoint_path
pointed at an ABSOLUTE path in the source repo rather than the node's workdir, found nothing, and
re-ran training inside the score stage — `overwrite: true` destroying the train stage's artifact on
the way. 2x GPU per node, for a number the pipeline never measured. `ps` showed `vectorsearch.test`
as the PARENT of `vectorsearch.train`.

Everything here drives the REAL gates over REAL files (tier 1): the write tool the Developer
actually calls, the real seeder, the real submit surface. No source pins — what has to hold is "the
write is refused" and "the file is on disk when the command runs", not "a call appears".
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from looplab.adapters.repo_task import (EditableSpec, EvalSpec, RepoTask, entrypoint_candidates,
                                        eval_entrypoint_unprotected)
from looplab.adapters.repo_write_tools import RepoWriteTools
from looplab.adapters.tasks import validate_task

_M = {"kind": "stdout_regex", "pattern": r"RECALL@100: ([0-9.]+)"}

# The live shape: a package whose `test` module is the scorer and whose `train` module is the
# training entrypoint the Developer is free (and expected) to edit.
_SCORER = 'import json\nprint("RECALL@100: 0.708708")\n'


def _repo(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def _vectorsearch(tmp_path: Path) -> Path:
    """The live task's repo shape, minus the model."""
    return _repo(tmp_path / "repo", {
        "vectorsearch/__init__.py": "",
        "vectorsearch/test.py": _SCORER,
        "vectorsearch/train.py": "print('trained')\n",
        "vectorsearch/model.py": "CFG = {}\n",
    })


def _task(repo: Path, command: list[str], **kw) -> RepoTask:
    """The live task's own spelling: the composable `repo:` default surface (`**/*`) and no
    operator `protect` at all — the configuration that made the defect reachable."""
    return RepoTask(goal="g", editable_path=str(repo), edit_surface=["**/*"], protect=[],
                    eval=EvalSpec(command=command, metric=_M, **kw))


def _write_tools(task: RepoTask) -> RepoWriteTools:
    """The Developer's write tools, built exactly as `LLMRepoDeveloper._run` builds them from
    `repo_spec()` — so a refusal here is the refusal the model would actually receive."""
    rs = task.repo_spec()
    prefixes = [e["name"].rstrip("/") for e in rs["editables"] if e["name"] not in (".", "")]
    return RepoWriteTools(rs["edit_surface"], rs["protected_names"], prefixes,
                          editables=rs["editables"])


# ------------------------------------------------- the defect: the scorer must refuse the write

def test_the_module_entrypoint_the_operator_scores_with_refuses_a_developer_write(tmp_path):
    """`python -m vectorsearch.test` freezes `vectorsearch/test.py`, for BOTH write paths.

    This is the test that fails without the change: with `protect: []` and `edit_surface:
    ["**/*"]`, both calls used to succeed and the staged file became the score stage's code.
    """
    task = _task(_vectorsearch(tmp_path), ["python", "-m", "vectorsearch.test"])
    w = _write_tools(task)

    assert "vectorsearch/test.py" in task.repo_spec()["protected_names"]
    got = w.execute("write_file", {"path": "vectorsearch/test.py", "content": "print(1)\n"})
    assert got.startswith("(refused:") and "protected" in got
    # The live edit arrived as an edit_file hunk, not a whole-file write — the prompt tells the
    # model to prefer it — so the same gate has to hold on that path.
    patch = {"path": "vectorsearch/test.py", "search": 'print("RECALL@100: 0.708708")',
             "replace": "subprocess.run([sys.executable, '-m', 'vectorsearch.train'])"}
    assert w.execute("edit_file", patch).startswith("(refused:")
    assert w.files == {}                       # nothing staged by either attempt

    # Narrow on purpose: the training entrypoint and the config the experiment is ABOUT stay the
    # Developer's. A rule that froze them would leave it nothing to change.
    assert w.execute("write_file", {"path": "vectorsearch/train.py",
                                    "content": "print('t')\n"}).startswith("wrote ")
    assert w.execute("write_file", {"path": "vectorsearch/model.py",
                                    "content": "CFG = {'lr': 1}\n"}).startswith("wrote ")


def test_a_script_entrypoint_freezes_and_a_neighbouring_script_does_not(tmp_path):
    """The other spelling an operator's cmd takes: `python eval/score.py`."""
    repo = _repo(tmp_path / "repo", {"eval/score.py": _SCORER, "eval/helpers.py": "X = 1\n"})
    w = _write_tools(_task(repo, ["python", "eval/score.py", "--split", "test"]))
    assert w.execute("write_file", {"path": "eval/score.py",
                                    "content": "x=1\n"}).startswith("(refused:")
    assert w.execute("write_file", {"path": "eval/helpers.py",
                                    "content": "X = 2\n"}).startswith("wrote ")


def test_what_the_entrypoint_imports_is_not_frozen(tmp_path):
    """A stated position, not an oversight: the protection is the entrypoint FILE only.

    A scorer's import closure is the repo — on the live task `vectorsearch.test` reaches the model,
    the config and the data loaders. Freezing it would freeze the repo and leave the adapter unable
    to run an experiment at all. So the closure stays editable, and what stops a scorer from being
    subverted THROUGH it is the stage `expect` contract and the artifact rules in the prompt.
    """
    repo = _vectorsearch(tmp_path)
    (repo / "vectorsearch" / "test.py").write_text(
        "from vectorsearch.model import CFG\n" + _SCORER, encoding="utf-8")
    w = _write_tools(_task(repo, ["python", "-m", "vectorsearch.test"]))
    assert w.execute("write_file", {"path": "vectorsearch/model.py",
                                    "content": "CFG = {'x': 1}\n"}).startswith("wrote ")
    assert "vectorsearch/model.py" not in _task(repo, ["python", "-m", "vectorsearch.test"]) \
        .repo_spec()["protected_names"]


# ------------------------------------------- the line: a scorer the repo does not ship stays the
#                                              Developer's to author

def test_a_scorer_the_repo_does_not_ship_stays_the_developers_to_author(tmp_path):
    """The majority flow for this adapter, and the reason existing-in-the-source IS the rule.

    `_REPO_DEV_SYSTEM_BODY` tells the Developer to author the eval entrypoint the cmd runs, and
    `eval_stages.PROTECTED_SCRIPT_MISSING` tells an operator to DROP a protect entry so it can. If
    protection keyed on the argv alone, that write would be refused and `_unrunnable_protected_
    scripts` would then refuse every node for a file nothing is allowed to create.
    """
    repo = _repo(tmp_path / "repo", {"train.py": "print('t')\n"})
    task = _task(repo, ["python", "looplab_eval.py"])
    assert task.repo_spec()["protected_names"] == []
    w = _write_tools(task)
    assert w.execute("write_file", {"path": "looplab_eval.py",
                                    "content": _SCORER}).startswith("wrote ")
    assert eval_entrypoint_unprotected(task) == []      # the designed flow is not a warning


def test_the_operator_can_hand_a_shipped_scorer_back_to_the_developer(tmp_path):
    """`cmd.protect_entrypoint: false` — the explicit off-switch.

    Deliberately its own field rather than "the operator listed some `protect`": `protect` only
    ADDS, so keying the off-switch on it would mean an operator who protects `data/**` silently
    gives up the scorer freeze that `protect` is the vocabulary for.
    """
    repo = _vectorsearch(tmp_path)
    task = _task(repo, ["python", "-m", "vectorsearch.test"], protect_entrypoint=False)
    assert task.repo_spec()["protected_names"] == []
    assert _write_tools(task).execute(
        "write_file", {"path": "vectorsearch/test.py", "content": "print(2)\n"}).startswith("wrote ")
    # Listing an UNRELATED protect entry does NOT disable the freeze.
    still = RepoTask(goal="g", editable_path=str(repo), edit_surface=["**/*"],
                     protect=["vectorsearch/train.py"],
                     eval=EvalSpec(command=["python", "-m", "vectorsearch.test"], metric=_M))
    assert set(still.repo_spec()["protected_names"]) == {"vectorsearch/train.py",
                                                         "vectorsearch/test.py"}


# ------------------------------------------------------------------ materialization + namespaces

def test_the_frozen_entrypoint_is_materialized_into_the_node_workdir(tmp_path):
    """Freezing WRITES without governing MATERIALIZATION is the hole `seed_protected_files` was
    written for: under the default `seed_mode="auto"` only git-TRACKED files reach the workdir, so
    an untracked scorer would be frozen AND absent — and the one file that could fix it is the one
    file the write gate refuses. Routing the derived entry through the editable's own `protect` is
    what makes seeding pick it up, so this drives the real seeder over a real git repo.
    """
    repo = _vectorsearch(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "vectorsearch/train.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
                   cwd=repo, check=True, capture_output=True)   # test.py stays UNTRACKED

    task = _task(repo, ["python", "-m", "vectorsearch.test"])
    mount = task._editable_mounts()[0]
    assert mount["protect"] == ["vectorsearch/test.py"]

    from looplab.engine.workspace import WorkspaceSeeder
    seeder = WorkspaceSeeder.__new__(WorkspaceSeeder)
    wd = tmp_path / "wd"
    seeder.seed_repo_tree(repo, wd, None, "auto")
    assert not (wd / "vectorsearch" / "test.py").exists()       # tracked-only seeding misses it
    assert seeder.seed_protected_files(repo, wd, mount["protect"]) == ["vectorsearch/test.py"]
    assert (wd / "vectorsearch" / "test.py").read_text(encoding="utf-8") == _SCORER


def test_a_named_editable_and_an_eval_cwd_resolve_to_the_same_namespace(tmp_path):
    """`protect` is per-repo and `protected_names` is workspace-relative; the eval `cwd` is where
    the command runs (and, for `-m`, sys.path[0]). All three have to join up, or the write gate
    protects a name the eval never runs."""
    sub = _repo(tmp_path / "model", {"vectorsearch/__init__.py": "", "vectorsearch/test.py": _SCORER})
    root = _repo(tmp_path / "root", {"train.py": "print('t')\n"})
    task = RepoTask(goal="g", editable_path=str(root), edit_surface=["**/*"],
                    editables=[EditableSpec(name="model", path=str(sub), surface=["**/*"])],
                    eval=EvalSpec(command=["python", "-m", "vectorsearch.test"], cwd="model",
                                  metric=_M))
    named = next(e for e in task.repo_spec()["editables"] if e["name"] == "model")
    assert named["protect"] == ["vectorsearch/test.py"]            # source-relative, for seeding
    assert "model/vectorsearch/test.py" in task.repo_spec()["protected_names"]   # workspace-relative
    w = _write_tools(task)
    assert w.execute("write_file", {"path": "model/vectorsearch/test.py",
                                    "content": "x=1\n"}).startswith("(refused:")


def test_the_agent_brief_names_the_frozen_scorer(tmp_path):
    """The brief's "Do NOT modify (the operator runs the evaluation)" list is built from
    `_protected_names`, so the model is TOLD before it burns a turn on a refusal."""
    task = _task(_vectorsearch(tmp_path), ["python", "-m", "vectorsearch.test"])
    assert "vectorsearch/test.py" in task.agent_brief()


# ------------------------------------------- the second defect: a path into the SOURCE tree, which
#                                              a node can never be running in

def test_a_write_naming_the_editable_source_root_says_so_in_its_own_result(tmp_path):
    """What actually made the retrain FIRE, and why the note is worth its bytes.

    The train stage wrote its checkpoint exactly where its manifest declared, workdir-relative. The
    node's `configs/config.yaml` then pointed `checkpoint_path` at
    `/home/jovyan/data/vectorizer-unified/vectorsearch/experiments/<this node's experiment>/final`
    — the SOURCE tree, which cannot contain a node's own output. `.exists()` failed, the scorer
    retrained, and `overwrite: true` destroyed the artifact the train stage had produced.

    A NOTE on a successful write, never a refusal: an absolute source path is wrong for an artifact
    the pipeline produces and merely questionable for a large untracked INPUT the seed mode does not
    copy, and a refusal the model cannot satisfy costs a repair attempt. It goes back the way the
    compile error does — at the moment the model can still act on it.
    """
    repo = _vectorsearch(tmp_path)
    w = _write_tools(_task(repo, ["python", "-m", "vectorsearch.test"]))

    ok = w.execute("write_file", {"path": "configs/config.yaml", "content":
                                  f"checkpoint_path: {repo}/vectorsearch/experiments/x/final\n"})
    assert ok.startswith("wrote ") and "SOURCE" in ok and str(repo) in ok
    assert w.files["configs/config.yaml"]                    # advisory only — the write still landed

    # The workdir-relative form — what the stage manifest declares — says nothing.
    clean = w.execute("write_file", {"path": "configs/other.yaml",
                                     "content": "checkpoint_path: vectorsearch/experiments/x/final\n"})
    assert clean.startswith("wrote ") and "SOURCE" not in clean
    # Neither does an absolute path OUTSIDE the editable repo: mounted data and the base model
    # legitimately live there, and the brief tells the Developer to reference them absolutely.
    outside = w.execute("write_file", {"path": "configs/data.yaml",
                                       "content": "root: /home/jovyan/data/dr-local\n"})
    assert outside.startswith("wrote ") and "SOURCE" not in outside


def test_the_note_follows_the_edit_path_and_only_reports_what_the_hunk_introduced(tmp_path):
    """The live edit arrived as an `edit_file` hunk. Reporting the WHOLE file would re-fire on every
    unrelated hunk in a file the repo already seeded with such a path, which is how a note becomes
    noise the model learns to skip."""
    repo = _vectorsearch(tmp_path)
    (repo / "configs").mkdir()
    (repo / "configs" / "config.yaml").write_text(
        f"legacy: {repo}/old\ncheckpoint_path: null\n", encoding="utf-8")
    w = _write_tools(_task(repo, ["python", "-m", "vectorsearch.test"]))

    hit = w.execute("edit_file", {
        "path": "configs/config.yaml", "search": "checkpoint_path: null",
        "replace": f"checkpoint_path: {repo}/vectorsearch/experiments/x/final"})
    assert not hit.startswith("(refused") and "SOURCE" in hit
    quiet = w.execute("edit_file", {"path": "configs/config.yaml", "search": "legacy:",
                                    "replace": "legacy_path:"})
    # The pre-existing source-root path in the same file is not this hunk's doing.
    assert not quiet.startswith("(refused") and "SOURCE" not in quiet


# ------------------------------------------------------------------- the argv rule's truth table

@pytest.mark.parametrize("argv,expected", [
    (["python", "-m", "vectorsearch.test"], ["vectorsearch/test.py", "vectorsearch/test/__main__.py"]),
    (["python3", "-u", "-m", "pkg"], ["pkg.py", "pkg/__main__.py"]),
    (["python", "-m", "pkg.mod", "--out", "x.py"], ["pkg/mod.py", "pkg/mod/__main__.py"]),  # -m wins
    (["python", "train.py", "--config", "cfg.py"], ["train.py"]),                       # first .py
    (["python", "./eval/score.py"], ["eval/score.py"]),
    (["python", "-W", "ignore", "score.py"], ["score.py"]),
    (["python", "-X", "faulthandler", "score.py"], ["score.py"]),
    # A `-m` AFTER the script is the SCRIPT'S argument. Scanning the whole argv returned
    # `eval.py` here while CPython runs `train.py` — freezing a file the operator never named,
    # leaving the real scorer editable, and suppressing the unprotected-scorer warning because the
    # result was non-empty. Every cost the "say nothing rather than guess" rule exists to avoid.
    (["python", "train.py", "-m", "eval"], ["train.py"]),
    (["python", "score.py", "-m", "pkg.mod", "--out", "x.py"], ["score.py"]),
    # A transparent prefix that names the interpreter verbatim is read through.
    (["srun", "python", "score.py"], ["score.py"]),
    (["env", "FOO=1", "python", "-m", "pkg.mod"], ["pkg/mod.py", "pkg/mod/__main__.py"]),
    (["nohup", "python", "eval/score.py"], ["eval/score.py"]),
    # Everything below names no in-repo file, and says so rather than guessing.
    (["bash", "run.sh"], []),
    # A launcher that does NOT name an interpreter: its own flag grammar decides which of
    # `--nproc_per_node 2 score.py` is the script, and we do not know it.
    (["torchrun", "--nproc_per_node", "2", "score.py"], []),
    (["accelerate", "launch", "score.py"], []),
    (["python", "-um", "pkg.mod"], []),               # a cluster carrying `m`: opaque, like `-mpkg`
    (["python", "-"], []),                            # stdin
    (["python", "run", "x.py"], []),                  # the executed token is not a .py at all
    (["./score"], []),
    (["pytest", "-q"], []),
    (["python", "-c", "import x"], []),
    (["python", "-mpkg.mod"], []),                    # the combined spelling: unhandled, so warned
    (["python", "/abs/score.py"], []),
    (["python", "../outside/score.py"], []),
    ([], []),
])
def test_entrypoint_candidates_truth_table(argv, expected):
    assert entrypoint_candidates(argv) == expected


# ------------------------------------------------------------------------- the submit-time warning

def test_an_unresolvable_entrypoint_warns_at_submit_naming_the_command(tmp_path):
    """Silence is the wrong answer for a wrapper/console-script cmd: the prompt still promises the
    operator that scoring cannot be rewritten, and nothing is enforcing it. A warning (not a
    refusal) because the run is perfectly runnable and the operator may know the scoring code is
    out of reach anyway — but it arrives at SUBMIT, where `protect` can still be edited."""
    repo = _repo(tmp_path / "repo", {"run.sh": "python -m vectorsearch.test\n"})
    warn = eval_entrypoint_unprotected(_task(repo, ["bash", "run.sh"]))
    assert len(warn) == 1 and "bash run.sh" in warn[0] and "protect" in warn[0]
    # A resolvable, frozen entrypoint says nothing — and neither does an explicit opt-out, which is
    # an operator decision already recorded in task.snapshot.json.
    vs = _vectorsearch(tmp_path)
    assert eval_entrypoint_unprotected(_task(vs, ["python", "-m", "vectorsearch.test"])) == []
    assert eval_entrypoint_unprotected(
        _task(vs, ["bash", "run.sh"], protect_entrypoint=False)) == []
    # Total over what the launch surfaces actually hand it (a non-repo adapter, an injected dict).
    assert eval_entrypoint_unprotected(None) == [] and eval_entrypoint_unprotected({}) == []


def test_the_cli_prints_the_warning_before_the_run_directory_exists(tmp_path):
    """Driven through the real CLI so the warning cannot be stranded by a refactor of `run()` — the
    property is that the operator SEES it, not that a helper returns a string. `--backend toy`
    keeps it offline; whatever the toy backend then makes of a repo task is not this test's
    business, only that the warning was printed on the way in."""
    import json
    from typer.testing import CliRunner
    from looplab.cli import app
    repo = _repo(tmp_path / "repo", {"run.sh": "echo hi\n"})
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps({
        "repo": str(repo), "goal": "g", "direction": "max",
        "cmd": {"command": ["bash", "run.sh"], "metric": _M}}), encoding="utf-8")
    result = CliRunner().invoke(app, ["run", str(task_file), "--no-genesis", "--backend", "toy",
                                      "--out", str(tmp_path / "run"), "-s", "max_nodes=1"])
    assert "names no in-repo entrypoint file" in result.output


def test_the_launch_api_returns_the_same_warning_as_the_cli(tmp_path):
    """The two submit surfaces must agree: a browser/API launch is where a Genesis-authored `cmd`
    most often arrives, and it is the surface with no stderr to fall back on."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    repo = _repo(tmp_path / "repo", {"run.sh": "echo hi\n"})
    client = TestClient(make_app(tmp_path / "runs"))
    body = client.post("/api/start/preflight", json={
        "run_id": "entrypoint-warning",
        "task": {"goal": "g", "direction": "max", "repo": str(repo),
                 "cmd": {"command": ["bash", "run.sh"],
                         "metric": {"reader": "stdout_regex", "key": r"RECALL@100: ([0-9.]+)"}}},
    }).json()
    assert body["ok"] is True
    assert any("names no in-repo entrypoint file" in w for w in body["warnings"])


def test_the_composable_repo_default_surface_still_freezes_the_scorer(tmp_path):
    """End to end through `validate_task`, in the exact spelling the live run spec used: `repo:`
    (which defaults `edit_surface` to `**/*`), a bare `cmd` argv, no `protect`."""
    repo = _vectorsearch(tmp_path)
    task = validate_task({"repo": str(repo), "goal": "g", "direction": "max",
                          "cmd": {"command": ["python", "-m", "vectorsearch.test"], "metric": _M}})
    assert task.edit_surface == ["**/*"] and task.protect == []
    assert "vectorsearch/test.py" in task.repo_spec()["protected_names"]
    assert _write_tools(task).execute(
        "write_file", {"path": "vectorsearch/test.py", "content": "x=1\n"}).startswith("(refused:")


def test_an_existing_runs_snapshot_reloads_with_the_freeze(tmp_path):
    """A snapshot written before `protect_entrypoint` existed must still LOAD (invariant #6's
    grandfathering is about refusals, and this rule refuses nothing) and picks the default up — a
    resumed run gets the fix rather than keeping the hole it was started with."""
    repo = _vectorsearch(tmp_path)
    snapshot = {"kind": "repo", "goal": "g", "direction": "max", "editable_path": str(repo),
                "edit_surface": ["**/*"], "protect": [],
                "eval": {"command": ["python", "-m", "vectorsearch.test"], "metric": _M}}
    task = validate_task(snapshot, existing_run=True)
    assert task.eval.protect_entrypoint is True
    assert "vectorsearch/test.py" in task.repo_spec()["protected_names"]
