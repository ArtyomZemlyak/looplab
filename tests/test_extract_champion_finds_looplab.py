"""`extract_champion.py` must import looplab when run BY PATH, which is how it is always run.

Running `python benchmarks/algotune/extract_champion.py` puts the SCRIPT's directory on `sys.path`,
not the repository root, so `from looplab.events.replay import fold` raises ModuleNotFoundError
unless looplab happens to be pip-installed into the interpreter. It is not on this box.

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
