"""The fence decided what was foreign from a hand-kept list of prefixes, and we keep minting names.

THE DEFECT. `fence_foreign_results.sh` skipped `LoopLab*|diag*|recheck*|REC-*|RuleCheck-*|CTL*` and
treated everything else under `AlgoTune/results/` as another model's published solution. But the
arena writes a directory named after whatever `--model` it was handed, and `make_task.py:787` hands
it `DevEvalTrain` for the Developer's own `eval_train` command. That name is on no list.

MEASURED 2026-08-27 05:56 on the live box: `AlgoTune/results/DevEvalTrain-2668122/convex_hull/
solver.py` appeared while the full-context probe was mid-evaluation and was gone by 05:57 — it
lives exactly as long as one `eval_train` call. Inside that window `close` would have MOVED the
running probe's own artifact into the hold directory, and the later `open` would have restored it
into `results/` as though it were somebody else's champion. The same window makes any launch guard
built on the same prefix list refuse a probe for no reason.

THE RULE THAT DOES NOT NEED MAINTAINING: the seventeen published model directories are TRACKED by
the AlgoTune fork's git; everything either arm generates at runtime is untracked. Verified on the
live checkout — `GPT-5`, `Claude Opus 4.6` and `R1` are tracked; `REC-90409` and all three
`RuleCheck-*` are not. So `git ls-files` is the classifier, and a new name we invent tomorrow needs
no edit here.

AND IT MUST FAIL LOUDLY: if `$AT` is not a git checkout, `git ls-files` says "not tracked" for
everything, which would make the fence a silent no-op that reports success. It refuses instead.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FENCE = ROOT / "benchmarks" / "algotune" / "fence_foreign_results.sh"


def _run(args, at, state, hold):
    return subprocess.run(
        ["bash", str(FENCE), *args], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(at),
             "FENCE_ALGOTUNE_ROOT": str(at), "FENCE_STATE": str(state), "FENCE_HOLD": str(hold)})


@pytest.fixture()
def checkout(tmp_path):
    """An AlgoTune-shaped git checkout: one COMMITTED foreign champion, one untracked artifact of
    ours with a name no prefix list knows."""
    at = tmp_path / "AlgoTune"
    (at / "results" / "Foreign Model 9" / "convex_hull").mkdir(parents=True)
    (at / "results" / "Foreign Model 9" / "convex_hull" / "solver.py").write_text("# theirs\n")
    subprocess.run(["git", "init", "-q"], cwd=at, check=True)
    subprocess.run(["git", "add", "-A"], cwd=at, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "published results"], cwd=at, check=True)

    ours = at / "results" / "DevEvalTrain-2668122" / "convex_hull"
    ours.mkdir(parents=True)
    (ours / "solver.py").write_text("# ours, written by eval_train\n")
    return at, tmp_path / "state", tmp_path / "hold"


def test_close_holds_the_tracked_champion_and_leaves_our_artifact(checkout):
    at, state, hold = checkout
    out = _run(["close"], at, state, hold)
    assert out.returncode == 0, out.stdout + out.stderr

    assert not (at / "results" / "Foreign Model 9").exists(), "the foreign champion was not held"
    assert (hold / "Foreign Model 9").is_dir()

    ours = at / "results" / "DevEvalTrain-2668122" / "convex_hull" / "solver.py"
    assert ours.is_file(), (
        "the fence swept up a live eval_train artifact: " + out.stdout + out.stderr)


def test_check_does_not_report_our_own_artifact_as_exposed(checkout):
    at, state, hold = checkout
    _run(["close"], at, state, hold)
    out = _run(["check"], at, state, hold)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "DevEvalTrain" not in out.stdout


def test_open_puts_back_exactly_what_was_held(checkout):
    at, state, hold = checkout
    _run(["close"], at, state, hold)
    out = _run(["open"], at, state, hold)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (at / "results" / "Foreign Model 9" / "convex_hull" / "solver.py").is_file()
    assert (at / "results" / "DevEvalTrain-2668122" / "convex_hull" / "solver.py").is_file()


def test_a_non_git_root_is_refused_rather_than_silently_fencing_nothing(tmp_path):
    """Without git there is no way to tell a champion from our own output, and `git ls-files`
    answers "untracked" for both. Reporting `closed 0` there would be a lie with a zero exit."""
    at = tmp_path / "NotACheckout"
    (at / "results" / "Foreign Model 9").mkdir(parents=True)
    out = _run(["close"], at, tmp_path / "s", tmp_path / "h")
    assert out.returncode != 0, out.stdout + out.stderr
    assert (at / "results" / "Foreign Model 9").is_dir(), "it fenced despite not knowing what is what"


# ------------------------------------------------------------------------------------------------
# A GIT TREE IS NOT THE SAME THING AS A TRACKED `results/`
# ------------------------------------------------------------------------------------------------
# `require_git` asked `rev-parse --git-dir` and stopped there. But the classifier is "tracked =
# foreign", so if git knows NO file under `results/`, `ls-files` says "untracked" about everything,
# `is_foreign` never fires, and the fence is exactly the silent no-op the git check exists to
# prevent -- one level deeper, and past it, because the tree really is a git tree.
#
# This is not hypothetical on this box. `benchmarks/box-jhub-l40s.sh` records that `/var/tmp` does
# not survive a container restart, so the stand is restored BY COPY from a snapshot; a copy whose
# `git init` was re-run, or whose snapshot did not carry `results/`, is precisely this shape.
# Reproduced 2026-08-30: `check` printed "all foreign result directories are closed" and exited 0
# while `results/Foreign Model 9/convex_hull/solver.py` sat in place, and `close` said "closed 0"
# and also exited 0.
def _copy_restored(tmp_path):
    """A git tree whose `results/` holds a foreign champion that git has never heard of."""
    at = tmp_path / "AlgoTune"
    (at / "results" / "Foreign Model 9" / "convex_hull").mkdir(parents=True)
    (at / "results" / "Foreign Model 9" / "convex_hull" / "solver.py").write_text("# theirs\n")
    (at / "AlgoTuner").mkdir()
    (at / "AlgoTuner" / "x.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=at, check=True)
    subprocess.run(["git", "add", "AlgoTuner"], cwd=at, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "everything but results"], cwd=at, check=True)
    return at, tmp_path / "state", tmp_path / "hold"


def test_check_refuses_a_tree_whose_results_git_has_never_seen(tmp_path):
    """The falsifier for "closed" over a visible champion."""
    at, state, hold = _copy_restored(tmp_path)
    out = _run(["check"], at, state, hold)
    assert out.returncode != 0, out.stdout + out.stderr
    assert "closed" not in out.stdout, (
        "it reported the fence closed over a champion it could not classify", out.stdout)
    assert (at / "results" / "Foreign Model 9" / "convex_hull" / "solver.py").is_file()


def test_close_refuses_the_same_tree_rather_than_fencing_nothing(tmp_path):
    """`closed 0 foreign result directories` with exit 0 is the same lie in the other command."""
    at, state, hold = _copy_restored(tmp_path)
    out = _run(["close"], at, state, hold)
    assert out.returncode != 0, out.stdout + out.stderr
    assert "closed 0" not in out.stdout, out.stdout


def test_an_empty_results_directory_is_not_refused(tmp_path):
    """The control. There is nothing to classify and nothing to leak, so demanding tracked files
    there would break a clean tree over a danger that does not exist."""
    at = tmp_path / "AlgoTune"
    (at / "results").mkdir(parents=True)
    (at / "f").write_text("x\n")
    subprocess.run(["git", "init", "-q"], cwd=at, check=True)
    subprocess.run(["git", "add", "f"], cwd=at, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=at, check=True)
    out = _run(["check"], at, tmp_path / "s", tmp_path / "h")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "all foreign result directories are closed" in out.stdout


def test_a_real_fork_checkout_is_still_fenced(checkout):
    """And the rung must not refuse the tree it was written to protect: the fixture's champion IS
    tracked, so `close` still moves it and `check` still passes afterwards."""
    at, state, hold = checkout
    assert _run(["check"], at, state, hold).returncode == 1, "the exposed champion was not seen"
    assert _run(["close"], at, state, hold).returncode == 0
    out = _run(["check"], at, state, hold)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "all foreign result directories are closed" in out.stdout
