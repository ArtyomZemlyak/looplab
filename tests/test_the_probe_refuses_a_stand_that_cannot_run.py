"""`run_probe.sh` refuses a stand whose python has no engine, and a card with a different ruler.

WHAT THIS IS ABOUT, measured on 2026-09-18 while bringing the stand back up after the 2026-09-10
wipe. Four launches in a row died, each one blaming a different layer:

    прогон rc=1 за 0с
    no event log at .../model-probes/smoke1/runs/edge_expansion/run
    чемпион: НЕТ

and the cause was four lines down in a file nobody reads, `run.log`:

    File ".../looplab/cli/__init__.py", line 30, in <module>
        import typer
    ModuleNotFoundError: No module named 'typer'

The script calls `python -m looplab.cli run` and takes `python` from PATH. On the restored stand
that was `/opt/conda/bin/python`, which has no `typer`. The probe checked the fence, built the
card and WROTE THE INSTRUMENT RECORD before finding out -- so every symptom appeared downstream of
the cause, and the last layer to speak was the one that had nothing to do with it.

The second refusal is the same morning's other silent state. `.baseline_times` is not in git, so
the restored stand had none, and `make_task.py` says on stderr that the timing clause "falls back
to the dataset name's target, which is a DIFFERENT card" -- and then builds it. That probe would
have run to completion and produced a number comparable to nothing.

Both guards are free, and both must fire before the run that spends.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from _posix_gates import BASH_HARNESS

SCRIPT = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "run_probe.sh"
SRC = SCRIPT.read_text(encoding="utf-8")


def _drive_engine_guard(python_body: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run the SHIPPED guard block against a `python` of our choosing.

    The block is extracted from the script rather than retyped: a copy is what stops matching the
    day the real one changes (the rule `test_the_campaign_refuses_a_half_credential.py` is built on).
    """
    lines = SRC.splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines) if l.startswith("if ! ENGINE_ERR="))
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("fi"))
    block = "".join(lines[start:end + 1])
    assert "looplab.cli" in block, "the guard did not extract"

    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "python").write_text("#!/bin/sh\n" + python_body + "\n")
    (stub / "python").chmod(0o755)

    harness = tmp_path / "harness.sh"
    harness.write_text('say() { echo "$*"; }\nROOT=/stand\n' + block)
    env = dict(os.environ, PATH=f"{stub}:{os.environ['PATH']}", PYTHONPATH="/stand/looplab")
    return subprocess.run(["bash", str(harness)], capture_output=True, text=True, env=env,
                          timeout=60)


@BASH_HARNESS
def test_an_engine_that_does_not_import_is_refused(tmp_path):
    """The exact failure of 2026-09-18, with its own words on the operator's screen."""
    got = _drive_engine_guard(
        'echo "ModuleNotFoundError: No module named \'typer\'" >&2; exit 1', tmp_path)
    assert got.returncode == 1, got.stdout + got.stderr
    assert "ОТКАЗ" in got.stdout and "не может импортировать движок" in got.stdout
    assert "No module named 'typer'" in got.stdout, "the cause must be ON the refusal, not in run.log"
    assert "/stand/looplab/.venv/bin" in got.stdout, "and so must the fix"


@BASH_HARNESS
def test_a_working_engine_passes_silently(tmp_path):
    """A guard that speaks on a healthy stand is a guard the operator learns to scroll past."""
    got = _drive_engine_guard("exit 0", tmp_path)
    assert got.returncode == 0, got.stdout + got.stderr
    assert got.stdout.strip() == "", got.stdout


def test_it_imports_the_CLI_and_not_merely_the_package(tmp_path):
    """`import looplab` succeeds on the very stand that cannot run: the broken import is INSIDE
    `looplab/cli/__init__.py`. A guard that checked the package would have passed all four times."""
    assert "python -c 'import looplab.cli'" in SRC


def test_the_guard_runs_before_the_card_before_the_instrument_and_before_the_money():
    """Source-pinned, because the ORDER is the property. Each of those three was reached on
    2026-09-18 by a stand that could not run a single node."""
    guard = SRC.index("if ! ENGINE_ERR=")
    assert guard < SRC.index('algotune/make_task.py"'), "before the card"
    assert guard < SRC.index('echo "probe:          $LABEL"'), "before the instrument record"
    assert guard < SRC.index('taskset -c "$LANE" python -m looplab.cli run'), "before the first dollar"


def test_it_checks_the_same_tree_the_run_will_import():
    """The guard sits BELOW the `PYTHONPATH` export on purpose: the stand's clone is put on the
    path there, and a check run above it would confirm some other checkout."""
    assert SRC.index('export PYTHONPATH="$ROOT/looplab') < SRC.index("if ! ENGINE_ERR=")


def test_a_card_built_without_the_reference_timings_is_refused():
    """`make_task.py` writes its warning to stderr and returns 0, and `run_probe.sh` sends that
    stderr to `$LOG` -- so the ONLY reader is a grep like this one. Pinned on the sentence
    `make_task.py` actually prints, both halves, so a reworded warning fails here rather than
    silently reopening the hole."""
    assert 'grep -q "no per-instance reference timings" "$LOG"' in SRC
    maker = (SCRIPT.parent / "make_task.py").read_text(encoding="utf-8")
    assert "no per-instance reference timings" in maker, "the two sentences have drifted"
    ruler = SRC.index('grep -q "no per-instance reference timings"')
    assert SRC.index('algotune/make_task.py"') < ruler < SRC.index('taskset -c "$LANE" python -m looplab.cli run')
