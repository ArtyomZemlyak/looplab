"""The arm can see WHERE its time goes, not only THAT it is slow.

MEASURED, 2026-08-27. AlgoTuner's own agent has `profile <file> <input>` and `profile_lines` in its
system prompt (`campaign-final/A-convex_hull.log:181`), implemented by `AlgoTuner/utils/profiler.py`
(`from line_profiler import LineProfiler` at line 8, `self.line_profiler(solve_method)` at 122), and
`tests/test_algotune_full_context.py` already records that it calls them 58-194 times per task.
`AlgoTune/.venv/bin/python -c "import line_profiler; print(line_profiler.__version__)"` prints
5.0.2 -- so the capability was installed in the very venv our scores are computed in, and only our
arm could not reach it. Its one measurement command, `eval_train`, prints a speedup and nothing
about where the seconds went.

These tests pin the four halves of the repair: the command EXISTS and is named in the card, it runs
on the REAL instance from the REAL split, it profiles the candidate's OWN helpers and not just
`solve`, and its output FITS the budget a developer-command result is clipped to.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "benchmarks" / "algotune" / "make_task.py"
PROFILER = REPO / "benchmarks" / "algotune" / "looplab_profile.py"

# The real checkout the campaign scores against, and the venv `line_profiler` is installed in.
# These tests are about a command whose whole content is "run the arena's data through the arena's
# loader", so a mock of either would test the mock.
ALGOTUNE = Path("/var/tmp/looplab-bench/AlgoTune")
VENV_PY = ALGOTUNE / ".venv" / "bin" / "python"

REFERENCE = '''
class Task:
    def solve(self, problem):
        return []

    def is_solution(self, problem, solution):
        return True
'''


def _fixture_root(tmp: Path, task: str = "demo") -> Path:
    """A minimal checkout `make_task.py` will build a card from (same shape as
    `test_algotune_full_context.py::_root`, which is where this fixture comes from)."""
    import numpy

    root = tmp / "AlgoTune"
    src = root / "AlgoTuneTasks" / task
    src.mkdir(parents=True)
    (src / "description.txt").write_text("a demo task\n", encoding="utf-8")
    (src / f"{task}.py").write_text(REFERENCE, encoding="utf-8")
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    data = root / ".hf_datasets" / "oripress__AlgoTune" / "data" / task
    npy = data / "_npy_data"
    npy.mkdir(parents=True)
    numpy.save(npy / "a.npy", numpy.zeros((4408, 2), dtype=numpy.float64))
    record = {"k": 4408, "seed": 42, "problem": {
        "points": {"__type__": "ndarray_ref", "npy_path": "_npy_data/a.npy"}}}
    for subset in ("train", "test"):
        (data / f"{task}_T100ms_n4408_size100_{subset}.jsonl").write_text(
            "\n".join(json.dumps(record) for _ in range(100)) + "\n", encoding="utf-8")
    return root


def _card(tmp: Path, *flags: str, task: str = "demo") -> dict:
    out = tmp / "out"
    out.mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--algotune-root", str(_fixture_root(tmp, task)),
         "--task", task, "--out-dir", str(out), *flags],
        capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    return json.loads((out / f"algotune_{task}.json").read_text(encoding="utf-8"))


def test_the_card_offers_a_profile_command_and_names_it_in_the_goal(tmp_path):
    """The falsifier for the whole change. A capability the goal never names is one the model does
    not use -- `test_algotune_full_context.py` records that for `eval_train` and it is why the
    assertion on the goal text is here rather than left to a reader."""
    spec = _card(tmp_path, "--deliver", "--full-context")
    commands = {c["name"]: c for c in spec.get("developer_commands") or []}
    assert "profile" in commands, f"no profiler for this arm: {sorted(commands)}"
    profile = commands["profile"]
    assert "looplab_profile.py" in " ".join(profile["command"]), profile["command"]
    assert profile["command"][0].endswith(".venv/bin/python"), (
        "it must run in the venv the scores are computed in -- the one holding line_profiler")
    argv = profile["command"]
    assert "--subset" in argv and argv[argv.index("--subset") + 1] == "train", argv
    assert profile["timeout"] <= 600, "DeveloperCommandSpec refuses more than 600 s"
    assert profile["timeout"] < commands["eval_train"]["timeout"], (
        "the cheap command must be cheaper to hang on than the expensive one")
    assert 'run_dev_command("profile")' in spec["goal"], spec["goal"][-1200:]


def test_the_opt_out_still_has_no_commands_at_all(tmp_path):
    """`--no-full-context` regenerates the goal card the 2026-08-24 arm B ran on. If a profiler
    leaked into it, those twenty numbers stop being reproducible."""
    spec = _card(tmp_path, "--deliver", "--no-full-context")
    assert "developer_commands" not in spec
    assert "profile" not in spec["goal"]


def test_the_spec_validates_as_a_repo_task(tmp_path):
    """A task the engine refuses to load is not a feature."""
    from looplab.adapters.repo_task import RepoTask

    task = RepoTask.model_validate(_card(tmp_path, "--deliver", "--full-context"))
    assert [c.name for c in task.developer_commands] == ["eval_train", "profile", "check"]


# --------------------------------------------------------------------------- the script itself
#
# These run the REAL profiler against the REAL dataset in the REAL venv, because every claim they
# make is about that wiring. They skip rather than fail where the checkout is absent, so the suite
# still runs on a box that has no AlgoTune.
needs_algotune = pytest.mark.skipif(
    not (VENV_PY.exists() and (ALGOTUNE / ".hf_datasets").is_dir()),
    reason="needs the AlgoTune checkout with its venv and downloaded datasets")


def _task_data(task: str) -> Path:
    """Where this box keeps ONE task's instances."""
    return ALGOTUNE / ".hf_datasets" / "oripress__AlgoTune" / "data" / task


def needs_task(task: str):
    """Skip when THIS TASK's data is absent, not merely when the dataset directory is.

    The old guard asked whether `.hf_datasets/` exists. It does here, and holds four tasks --
    discrete_log, edge_expansion, pde_heat1d, pagerank, two files each -- and NOT convex_hull, whose
    202 instance files never downloaded and cannot now, the box being offline. So the one test that
    profiles convex_hull ran and failed on every sweep from 2026-08-30, and I twice called it
    "background" instead of a defect.

    A permanent red is not a cheap thing to carry: it trains a reader to skim this file, which is
    where a real regression would appear. And failing here says nothing about the profiler -- with
    no instances the tool cannot be exercised at all, so there is nothing to regress against. That
    is what a skip is FOR, and it is the honest verdict only when the guard names the task, which
    this one does and the old one could not.
    """
    d = _task_data(task)
    return pytest.mark.skipif(
        not (VENV_PY.exists() and d.is_dir() and any(d.iterdir())),
        reason=f"no cached instances for AlgoTune task {task!r} at {d}")


def _profile(workspace: Path, task: str, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(VENV_PY), str(PROFILER), "--algotune-root", str(ALGOTUNE), "--task", task, *flags],
        cwd=str(workspace), capture_output=True, text=True, timeout=600)


@needs_algotune
def test_it_profiles_a_helper_module_the_scorer_would_have_accepted(tmp_path):
    """TWO claims in one solver, and each of them is a defect this caught.

    (a) A NODE MAY WRITE MORE THAN ONE FILE. `edit_surface` allows it and
    `scripts/evaluate_results.py:396` puts the candidate's directory on `sys.path` before importing,
    so the scorer accepts a two-file solver. `load_solver_module` does NOT do that on its own, and
    the first implementation answered such a solver with `ImportError: No module named 'helper'` --
    a profiler that refuses inputs the grader accepts reads as "your solver is broken".

    (b) THE HOT LOOP IS IN THE HELPER. The arena wraps exactly one callable (`profiler.py:122`), so
    its own table would stop at the line that called `bsgs`. Measured on this fixture: 1011 ms on
    `return {"x": bsgs(...)}` and nothing else. Our arm gets ONE fixed call with no `profile_lines`
    to follow up with, so the helper's lines have to be on the first table or they are never seen.
    """
    (tmp_path / "helper.py").write_text(
        "def bsgs(p, g, h):\n"
        "    m = int(p ** 0.5) + 1\n"
        "    table = {}\n"
        "    val = 1\n"
        "    for j in range(m):\n"
        "        table[val] = j\n"
        "        val = (val * g) % p\n"
        "    return len(table)\n", encoding="utf-8")
    (tmp_path / "solver.py").write_text(
        "from helper import bsgs\n"
        "class Solver:\n"
        "    def solve(self, problem, **kw):\n"
        "        return {\"x\": bsgs(problem[\"p\"], problem[\"g\"], problem[\"h\"])}\n",
        encoding="utf-8")
    done = _profile(tmp_path, "discrete_log")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "helper.py::bsgs" in done.stdout, done.stdout
    assert "table[val] = j" in done.stdout, "the hot line inside the helper was never named"


@needs_task("convex_hull")
def test_it_profiles_the_real_instance_at_the_graded_size(tmp_path):
    """No toy input. `convex_hull` is n = 267 021 and the probes that chose its champion ran at
    n = 100, 1 000 and 10 000 (`test_algotune_full_context.py`). A profiler that quietly shrinks
    the instance would re-create exactly that failure inside the tool built to end it."""
    (tmp_path / "solver.py").write_text(
        "import numpy as np\n"
        "from scipy.spatial import ConvexHull\n"
        "class Solver:\n"
        "    def solve(self, problem, **kw):\n"
        "        pts = np.asarray(problem[\"points\"], dtype=np.float64)\n"
        "        h = ConvexHull(pts)\n"
        "        return {\"hull_vertices\": h.vertices.tolist(),\n"
        "                \"hull_points\": pts[h.vertices].tolist()}\n", encoding="utf-8")
    done = _profile(tmp_path, "convex_hull")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "(267021, 2)" in done.stdout, f"not the graded size:\n{done.stdout[:600]}"
    assert "ConvexHull(pts)" in done.stdout, done.stdout


@needs_algotune
def test_a_solver_that_never_returns_still_says_which_line_it_was_inside(tmp_path):
    """The case a profiler is MOST needed in is the one `eval_train` cannot report at all: a
    candidate that blows the evaluator's clock comes back as a timeout with no line attached."""
    (tmp_path / "solver.py").write_text(
        "class Solver:\n"
        "    def solve(self, problem, **kw):\n"
        "        total = 0\n"
        "        while True:\n"
        "            total += 1\n", encoding="utf-8")
    done = _profile(tmp_path, "discrete_log", "--solve-timeout", "2")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "partial" in done.stdout, done.stdout
    assert "total += 1" in done.stdout, "the line it was stuck inside was never named"


@needs_algotune
def test_the_report_fits_the_budget_a_dev_command_result_is_clipped_to(tmp_path):
    """THE FALSIFIER FOR THE SHRINK LOOP, and it was written because the first implementation
    failed it at 6,300 characters.

    The fence is `looplab/core/context_budget.py::RESULT_CAP`
    is, at 4000 characters, and `tools/dev_commands.py::_project` keeps the TAIL of stdout when it
    overflows. So an oversized profile does not lose its least useful rows, it loses the header,
    the timings and the hottest function, and hands the agent the coldest helpers instead. Each
    function on the table costs two header lines, so the size depends on how the hot lines are
    SPREAD: thirty helpers with one hot line each is the worst case, and it is this fixture.
    """
    from looplab.core.context_budget import RESULT_CAP

    body = ["import math\n", "FNS = []\n"]
    for i in range(30):
        body.append(
            f"\ndef some_rather_long_helper_function_name_number_{i:02d}(n):\n"
            f"    return sum(math.sqrt(k + {i}) * 1.000001 for k in range(n))\n"
            f"FNS.append(some_rather_long_helper_function_name_number_{i:02d})\n")
    body.append("\nclass Solver:\n"
                "    def solve(self, problem, **kw):\n"
                "        return {\"x\": int(sum(f(20000) for f in FNS)) % 7}\n")
    (tmp_path / "solver.py").write_text("".join(body), encoding="utf-8")

    done = _profile(tmp_path, "discrete_log")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not done.stderr, f"stderr competes with stdout for the same budget:\n{done.stderr[:400]}"
    assert len(done.stdout) < RESULT_CAP - 700, (
        f"{len(done.stdout)} characters against RESULT_CAP={RESULT_CAP} minus the ~450-character "
        f"argv/exit-code header the tool prepends: the agent would be handed the tail")
    # And what survives is the part worth keeping: the header and the hottest function.
    assert done.stdout.startswith("profile: discrete_log"), done.stdout[:200]
    assert "Solver.solve" in done.stdout, done.stdout


# --- the guard itself ----------------------------------------------------------------------------

def test_the_guard_skips_only_the_task_whose_data_is_missing():
    """A blanket skip is how a real regression hides, so the guard must be per-task and provable.

    On this box `.hf_datasets/` holds discrete_log, edge_expansion, pde_heat1d and pagerank, and not
    convex_hull. The old guard asked only whether the DIRECTORY exists, so the convex_hull test ran
    and failed on every sweep from 2026-08-30 while the rest of the file passed -- a permanent red
    that trains a reader to skim exactly the file a regression would appear in.
    """
    if not VENV_PY.exists() or not (ALGOTUNE / ".hf_datasets").is_dir():
        pytest.skip("no AlgoTune checkout on this box")

    present = [t for t in ("discrete_log", "edge_expansion", "pde_heat1d", "pagerank")
               if _task_data(t).is_dir()]
    assert present, (
        "no task data at all — then this whole file is skipped and proves nothing; that is a "
        "stand problem, not a test problem"
    )
    for task in present:
        assert not needs_task(task).args[0], f"{task} has data and the guard would skip it anyway"

    missing = _task_data("definitely_not_a_task_zzz")
    assert not missing.exists()
    mark = needs_task("definitely_not_a_task_zzz")
    assert mark.args[0], "the guard does not skip a task with no data"
    assert "definitely_not_a_task_zzz" in mark.kwargs["reason"], mark.kwargs["reason"]


def test_the_file_still_exercises_the_profiler_on_this_box():
    """The fix must not have turned the file into a no-op. At least one profiling test must run
    here, or "green" means "skipped" and the profiler is unguarded."""
    if not VENV_PY.exists() or not (ALGOTUNE / ".hf_datasets").is_dir():
        pytest.skip("no AlgoTune checkout on this box")
    assert _task_data("discrete_log").is_dir(), (
        "the three discrete_log profiling tests are the only ones this box can run, and its data "
        "is gone — nothing here is being tested"
    )
