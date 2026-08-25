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


# OPEN[fence-walkable-test-red-under-root] this falsifier fails whenever the suite runs as root.
# proof:absent:geteuid@tests/test_algotune_fence_keeps_results_walkable.py
# REVIEW 2026-08-25 (guard-test): the negative control assumes `chmod 000` denies the walk, and
# root (CAP_DAC_OVERRIDE) reads mode-000 directories fine -- measured 2026-08-25 under uid 0:
# `assert raised` fails with this test's own "proves nothing" message while the two real fence
# tests beside it pass (the shipped move-aside design is root-safe, which is part of why it
# replaced the chmod one). Containers routinely run suites as root, so this reds in exactly the
# environments the fence targets. Fix: skip when the effective uid is 0, or probe once whether
# mode 000 actually denies before asserting -- the falsifier's value is real everywhere else and
# should not be deleted.
def test_chmod_000_would_have_failed_this_test(tmp_path):
    """Non-vacuous by construction: the abandoned implementation, run against the same assertion.

    Without this, a fence that did nothing at all would pass the test above's walk.
    """
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
