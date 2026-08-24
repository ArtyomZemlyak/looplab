"""`compare_arms.py` must run under an interpreter that has never heard of `looplab`.

Seven driver scripts invoke it with ALGOTUNE's virtualenv — that is the venv nearest to hand when
the summary is reading the arena's own reports — and `looplab` is not installed there. Every one of
them died at the last step of a campaign that had already run for hours:

    File "benchmarks/algotune/compare_arms.py", line 66, in _to_float
        from looplab.core.parse import to_float
    ModuleNotFoundError: No module named 'looplab'

The comparison table is the entire point of the campaign, and it was never printed once. Fixing the
seven call sites would leave the eighth to get it wrong, so the file locates its own package
instead: it sits at `<repo>/benchmarks/algotune/`, and `parents[2]` is the tree it belongs to.
"""
import json
import runpy
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "benchmarks" / "algotune" / "compare_arms.py"

# HIDING THE PACKAGE TAKES MORE THAN `sys.path`. This repo is installed PEP 660 editable
# (`__editable__.looplab-0.1.0.pth`), which registers a finder on `sys.meta_path` — so stripping
# path entries leaves the package perfectly importable, and a test that only did that would prove
# nothing. Found by the falsifier below, which reported STILL-REACHABLE. Both routes are cut here.
# The path filter must be PRECISE: drop an entry only if it actually provides the package. A
# substring match on "looplab" also deletes this repo's own virtualenv — whose path contains the
# repo name — and with it pydantic, so the script then failed on an unrelated import and the test
# said nothing about the bootstrap.
HIDE = (
    'import os as _os; '
    'sys.meta_path = [f for f in sys.meta_path '
    'if "looplab" not in (getattr(f, "__name__", "") or "").lower() '
    'and "looplab" not in getattr(f, "__module__", "").lower()]; '
    'sys.path = [p for p in sys.path '
    'if not _os.path.exists(_os.path.join(p or ".", "looplab", "__init__.py"))]'
)


def _run_without_the_package(args: list[str]) -> subprocess.CompletedProcess:
    """Run the script with every route to `looplab` removed from `sys.path` first.

    This is what ALGOTUNE's interpreter looks like from the script's point of view: the repo is not
    installed, no `.pth` points at it, and `PYTHONPATH` does not name it. The bootstrap under test
    works off `__file__`, which survives all of that.
    """
    code = textwrap.dedent(f"""
        import sys, runpy
        {HIDE}
        sys.argv = ["compare_arms.py"] + {args!r}
        runpy.run_path({str(SCRIPT)!r}, run_name="__main__")
    """)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd="/", env={"PATH": "/usr/bin:/bin"}, timeout=120)


def test_it_runs_where_looplab_is_not_importable(tmp_path):
    # A minimal campaign output: one finished arm-B task with a marker. Without real input the
    # script exits 1 saying "no campaign output found", which is correct and would hide whether the
    # import worked — the point here is that it gets far enough to PRINT A TABLE.
    final = tmp_path / "campaign-final"
    final.mkdir()
    # The task list comes from the run directories, so there has to be one.
    (tmp_path / "runs-B" / "convex_hull").mkdir(parents=True)
    (final / "B-convex_hull.final.json").write_text(
        json.dumps({"speedup": 1.0892, "eval_seconds": 85.3, "subset": "test"}), encoding="utf-8")
    (final / "B-convex_hull.done").write_text(
        "wall=13559 rc=0 state=ran_to_completion attempt=a1\n", encoding="utf-8")

    r = _run_without_the_package(["--algotune-root", str(tmp_path),
                                  "--runs-root", str(tmp_path / "runs-B"),
                                  "--final-dir", str(final)])
    assert "ModuleNotFoundError" not in r.stderr, r.stderr[-800:]
    assert r.returncode == 0, (r.stdout[-400:], r.stderr[-800:])
    assert "A: AlgoTuner" in r.stdout and "B: LoopLab" in r.stdout, r.stdout[:400]
    assert "1.0892" in r.stdout, r.stdout[:600]      # the value survived `_to_float`'s import


def test_the_stripped_path_really_hides_the_package(tmp_path):
    """The falsifier: prove the harness above actually removes `looplab` from reach.

    Without this, a `sys.path` that still found the package would make the test above pass while
    proving nothing about the bootstrap.
    """
    code = textwrap.dedent(f"""
        import sys
        {HIDE}
        try:
            import looplab
            print("STILL-REACHABLE", looplab.__file__)
        except ModuleNotFoundError:
            print("HIDDEN")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd="/", env={"PATH": "/usr/bin:/bin"}, timeout=60)
    assert r.stdout.strip() == "HIDDEN", r.stdout


def test_the_bootstrap_points_at_this_tree():
    """And it must resolve against the copy being run, not whatever tree is on the path.

    The campaign runs the PINNED `looplab-armb` checkout; if the bootstrap resolved to the working
    clone, the summary would be produced by different code than the one the run was measured with.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "parents[2]" in src, "the bootstrap no longer derives the repo from __file__"
    ns = runpy.run_path(str(SCRIPT), run_name="not_main")
    assert ns["_REPO"] == REPO, f"{ns['_REPO']} != {REPO}"
