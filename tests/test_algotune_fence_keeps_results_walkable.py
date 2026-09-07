"""The fence must hide foreign champions WITHOUT making `results/` unwalkable.

Why this test exists, precisely: the fence's first implementation used `chmod 000`, and it took a
live campaign down within half an hour. The arena's own `scripts/evaluate_results.py` discovers
work by iterating EVERY directory under `results/` and listing the tasks inside each one, so the
first unreadable directory raises `PermissionError: results/Claude Opus 4.1` and the evaluator dies
before it writes `evaluate_summary.json`. Two task-arms scored 0.0 with `eval_seconds: 0.1` and the
run looked like a solver failure rather than a harness fault.

So the fence has two obligations that pull against each other, and both are asserted here:
  1. the foreign solutions must be unreachable — that is the whole point of fencing them;
  2. a walk of `results/` that lists each subdirectory must still succeed — that is what the ruler
     does on every single evaluation.

`chmod 000` satisfies (1) and violates (2). Moving them aside satisfies both.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

FENCE = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "fence_foreign_results.sh"


def _walk_like_the_arena(results: Path):
    """Exactly what `discover_models_and_tasks` does: iterate, then list inside each."""
    return {m.name: sorted(t.name for t in m.iterdir() if t.is_dir())
            for m in results.iterdir() if m.is_dir()}


def _fenced_tree(tmp_path):
    at = tmp_path / "AlgoTune"
    results = at / "results"
    for model, task in [("Claude Opus 4.1", "convex_hull"), ("o4-mini", "pagerank")]:
        d = results / model / task
        d.mkdir(parents=True)
        (d / "solver.py").write_text("# a finished solution by another model\n")
    # THE TREE HAS TO BE A CHECKOUT, because the fence tells a published champion from our own
    # output by `git ls-files` and not by the shape of its name. The two directories above are
    # what the fork ships and are COMMITTED here; `RuleCheck-1` is what a run of ours leaves
    # behind and stays untracked. Before 2026-08-27 the classifier was a list of our prefixes,
    # and it missed `DevEvalTrain-<pid>` -- a directory the Developer's own `eval_train` creates
    # for the length of one evaluation -- so the fence would hold a live probe's artifact.
    subprocess.run(["git", "init", "-q"], cwd=at, check=True)
    subprocess.run(["git", "add", "-A"], cwd=at, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "published results"], cwd=at, check=True)

    ours = results / "RuleCheck-1" / "convex_hull"
    ours.mkdir(parents=True)
    (ours / "solver.py").write_text("# ours\n")
    env = {**os.environ,
           "FENCE_ALGOTUNE_ROOT": str(at),
           "FENCE_STATE": str(tmp_path / "state"),
           "FENCE_HOLD": str(tmp_path / "held")}
    return at, results, env


def _fence(env, verb):
    return subprocess.run(["bash", str(FENCE), verb], env=env,
                          capture_output=True, text=True, timeout=60)


def test_arena_can_still_walk_results_while_the_fence_is_closed(tmp_path):
    at, results, env = _fenced_tree(tmp_path)
    assert _fence(env, "close").returncode == 0

    # (2) The walk that killed the campaign. A PermissionError here IS the regression.
    seen = _walk_like_the_arena(results)
    assert "RuleCheck-1" in seen, "the fence removed our own results, not just the foreign ones"

    # (1) and the foreign work is genuinely gone while closed.
    assert "Claude Opus 4.1" not in seen and "o4-mini" not in seen
    assert not (results / "Claude Opus 4.1" / "convex_hull" / "solver.py").exists()

    assert _fence(env, "check").returncode == 0

    # Restored exactly, because the fork tracks these files and the campaign is not allowed to
    # leave the checkout altered.
    assert _fence(env, "open").returncode == 0
    back = _walk_like_the_arena(results)
    assert back == {"Claude Opus 4.1": ["convex_hull"], "o4-mini": ["pagerank"],
                    "RuleCheck-1": ["convex_hull"]}
    assert (results / "o4-mini" / "pagerank" / "solver.py").read_text().startswith("# a finished")


def _mode_000_denies_a_walk(tmp_path) -> bool:
    """Does `chmod 000` actually stop a directory listing HERE? PROBED, never assumed.

    `CAP_DAC_OVERRIDE` (i.e. root, which is how a container routinely runs a suite) reads a mode-000
    directory perfectly well, and so do some FUSE/overlay mounts that do not enforce owner bits at
    all. The falsifier below is a NEGATIVE CONTROL over the ABANDONED chmod design, so on such a
    box the premise it is built on is simply false and the assertion fails while the fence itself is
    fine — the shipped move-aside design is root-safe, which is part of why it replaced the chmod
    one. Probing beats an `os.geteuid() == 0` skip because it also covers the mount case and it
    keeps the control live wherever the premise really does hold.
    """
    probe = tmp_path / "_chmod_probe"
    (probe / "inner").mkdir(parents=True)
    probe.chmod(0o000)
    try:
        list(probe.iterdir())
        return False
    except PermissionError:
        return True
    finally:
        probe.chmod(0o755)
        shutil.rmtree(probe, ignore_errors=True)


def test_chmod_000_would_have_failed_this_test(tmp_path):
    """Non-vacuous by construction: the abandoned implementation, run against the same assertion.

    Without this, a fence that did nothing at all would pass the test above's walk.
    """
    if not _mode_000_denies_a_walk(tmp_path):
        pytest.skip("mode 000 does not deny a walk on this box (root, or a mount that ignores "
                    "owner bits), so the abandoned chmod design cannot be used as a control here")
    at, results, env = _fenced_tree(tmp_path)
    victim = results / "Claude Opus 4.1"
    victim.chmod(0o000)
    try:
        raised = False
        try:
            _walk_like_the_arena(results)
        except PermissionError:
            raised = True
        assert raised, "mode 000 no longer blocks the walk here, so this test proves nothing"
    finally:
        victim.chmod(0o755)
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_closing_twice_does_not_strand_the_directories(tmp_path):
    """`close` is called twice in a real launch, and the second call must not lose the first's work.

    `check_leaks.sh` fences before it audits, then `run_final.sh` fences again before it launches.
    The first implementation truncated its state file on entry, so the second `close` recorded
    nothing — and `open` then restored nothing, leaving all seventeen tracked directories in the
    holding area. Observed: state file empty, 17 directories stranded, `results/` down to 5.
    """
    at, results, env = _fenced_tree(tmp_path)
    assert _fence(env, "close").returncode == 0
    assert _fence(env, "close").returncode == 0          # the second one, which used to erase the record

    assert _fence(env, "check").returncode == 0
    assert _fence(env, "open").returncode == 0
    assert _walk_like_the_arena(results) == {"Claude Opus 4.1": ["convex_hull"],
                                             "o4-mini": ["pagerank"],
                                             "RuleCheck-1": ["convex_hull"]}
    assert not (tmp_path / "held").exists(), "the holding area still has directories in it"
