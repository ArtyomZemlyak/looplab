"""A card that cannot find the reference timings becomes a DIFFERENT card, and must say so.

With `.baseline_times` present the timing clause states the measured per-instance cost on this box
-- "THE REFERENCE COSTS 46 ms PER INSTANCE" -- and without it, the clause falls back to the target
encoded in the dataset's file NAME, "about 100 ms ... an order of magnitude". Two different numbers
for the model to size its work against, selected by whether a directory happens to sit beside the
script, with nothing printed either way.

MEASURED 2026-09-02, on myself (docs/56 §113): reconstructing an old card by copying `make_task.py`
into a scratch directory produced the 100 ms wording. I read that as "the shipped card changed since
the control group ran", stopped a four-probe arm and relaunched it. The card had not changed --
`BASELINE_TIMES_DIR` resolves against `Path(__file__).resolve().parent`, and my copy was somewhere
else. One line on stderr would have stopped that.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "benchmarks" / "algotune" / "make_task.py"
ALGOTUNE = Path("/var/tmp/looplab-bench/AlgoTune")
TIMES = REPO / "benchmarks" / "algotune" / ".baseline_times"

needs_algotune = pytest.mark.skipif(
    not (ALGOTUNE / ".hf_datasets").is_dir() or not TIMES.is_dir(),
    reason="needs the AlgoTune checkout and this box's measured baseline timings")


def _build(script: Path, out: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), "--algotune-root", str(ALGOTUNE), "--task", "edge_expansion",
         "--out-dir", str(out), "--deliver", "--one-card", "--enforce-rules"],
        capture_output=True, text=True, cwd=str(cwd), timeout=900)


@needs_algotune
def test_a_card_built_without_its_timings_says_so(tmp_path):
    """MUTATION: delete the `print(... file=sys.stderr)` and this reddens -- which is exactly the
    silence that cost four probes."""
    copy = tmp_path / "make_task.py"
    shutil.copy2(SCRIPT, copy)
    done = _build(copy, tmp_path / "out", tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "no per-instance reference timings" in done.stderr, done.stderr
    goal = json.loads((tmp_path / "out" / "algotune_edge_expansion.json").read_text())["goal"]
    # NOT `"ON THIS BOX" not in goal`: the toolchain clause says "THE TOOLCHAIN ON THIS BOX" in
    # BOTH cards, so that assertion failed on a card that was behaving correctly. The distinguishing
    # phrase is the reference COST, which is the sentence the timings write.
    assert "PER INSTANCE ON THIS BOX" not in goal, (
        "the fallback card must not claim a measured per-instance cost:\n" + goal[:400])
    assert "says the reference took about" in goal, goal[:400]


@needs_algotune
def test_the_real_card_is_silent_and_carries_the_measured_cost(tmp_path):
    done = _build(SCRIPT, tmp_path / "out", REPO)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "no per-instance reference timings" not in done.stderr, done.stderr
    goal = json.loads((tmp_path / "out" / "algotune_edge_expansion.json").read_text())["goal"]
    assert "PER INSTANCE ON THIS BOX" in goal, goal[:400]


@needs_algotune
def test_the_two_are_not_the_same_card(tmp_path):
    """The whole point: the difference is in the card, not only in the log. If these ever match,
    the warning is describing a distinction that no longer exists and should be deleted."""
    copy = tmp_path / "make_task.py"
    shutil.copy2(SCRIPT, copy)
    _build(copy, tmp_path / "a", tmp_path)
    _build(SCRIPT, tmp_path / "b", REPO)
    a = json.loads((tmp_path / "a" / "algotune_edge_expansion.json").read_text())["goal"]
    b = json.loads((tmp_path / "b" / "algotune_edge_expansion.json").read_text())["goal"]
    assert a != b
