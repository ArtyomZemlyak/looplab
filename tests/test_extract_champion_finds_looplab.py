"""`extract_champion.py` must import looplab when run BY PATH, which is how it is always run.

Running `python benchmarks/algotune/extract_champion.py` puts the SCRIPT's directory on `sys.path`,
not the repository root, so `from looplab.events.replay import fold` raises ModuleNotFoundError
unless looplab happens to be pip-installed into the interpreter — which is true of a
developer checkout (`pip install -e ".[dev,ui]"`) and false of the bench stand, where this
script actually runs. The second test below therefore has to MAKE the import fail rather
than assume it does; see its docstring.

Measured 2026-08-31 on a finished probe, not reasoned about. `accEE` ran to its ceiling (rc=0,
6321 s) and evaluated two nodes -- 27.466 then 221.5387 on train -- and its own summary line read
"champion: NONE", because `run_probe.sh` treats a non-zero exit from this script as "no champion".
The scores were never at risk; they are in events.jsonl. The READING was: a probe that reports
nothing is indistinguishable from a probe that found nothing, and 221.5387 would have been recorded
as a failure.

The identical ModuleNotFoundError closed the 2026-08-29 campaign -- `run_final-relaunch.log` ends
with that traceback out of `compare_arms.py` -- which is why five sibling scripts in this directory
already carry the three lines that put the repo root on the path. This one was the sixth and did not.
"""
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "extract_champion.py"


def test_it_imports_looplab_when_run_by_path_from_an_unrelated_cwd(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text('{"type": "run_started", "data": {}}\n', encoding="utf-8")

    # cwd is deliberately NOT the repo: that is the only reason `import looplab` ever appeared to
    # work here, and it is not how run_probe.sh invokes this.
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--run-dir", str(run_dir), "--out", str(tmp_path / "c.py")],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=180,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})

    combined = result.stdout + result.stderr
    assert "No module named 'looplab'" not in combined, combined
    # Whether THIS synthetic run has a champion is not the claim; being able to look is.
    assert "could not fold" not in combined or "No module named" not in combined, combined


def test_an_import_it_cannot_satisfy_is_a_broken_bridge_and_not_an_empty_run(tmp_path):
    """The classification `c32ebeb0` did not change when it removed the cause.

    That commit put the repo root on `sys.path`, so today's ModuleNotFoundError is gone. The
    `except Exception` that turned it into "could not fold" was left alone -- and `run_probe.sh`
    reads any non-zero as "champion: NONE", which is the sentence accEE's summary carried while its
    own events.jsonl held 27.466 and 221.5387. So the day the script is copied, vendored, or run
    from a tree with no `looplab/` beside it, the same wrong verdict comes back.

    Driven the only way that means anything: the script is copied to a depth where its own
    `parents[2]` is NOT a looplab checkout, so the path insert cannot fire and the import genuinely
    fails. An import failure says nothing at all about the run, and must not be reported as if it
    did.

    `-S` IS PART OF THE FIXTURE, not tuning. The header's "it is not pip-installed on this box" is
    true of the bench stand and false of the setup CLAUDE.md prescribes to everyone else
    (`pip install -e ".[dev,ui]"`), where an `__editable__*.pth` in site-packages makes `looplab`
    importable from any directory — so the stray copy imported fine, printed "run has no champion",
    and the assertion below reddened saying the fixture no longer reproduced its own subject. It
    was right. `-S` skips site processing, which is what makes the import genuinely unsatisfiable
    wherever this runs; it does not weaken the claim, because the claim is about what the script
    says WHEN the import fails.
    """
    stray = tmp_path / "a" / "b" / "c"
    stray.mkdir(parents=True)
    (stray / "extract_champion.py").write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text('{"type": "run_started", "data": {}}\n', encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-S", str(stray / "extract_champion.py"), "--run-dir", str(run_dir),
         "--out", str(tmp_path / "c.py")],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=180,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})

    combined = result.stdout + result.stderr
    assert "No module named 'looplab'" in combined, ("the fixture no longer reproduces the import "
                                                     "failure it is about\n" + combined)
    assert result.returncode == 2, (
        "an import this script cannot satisfy is reported with the same exit code as a run that "
        "has no champion, and run_probe.sh cannot tell them apart\n" + combined)
    assert "BROKEN BRIDGE" in combined
    assert "could not fold" not in combined
