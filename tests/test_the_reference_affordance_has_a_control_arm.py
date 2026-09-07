"""The clause added in f906ea07 has never been tested, and until now could not be.

§69.1 pinned its acceptance against 4.9-8.3 % -- the share of `run_probe` calls importing the
reference, measured on three probes carrying the OLD card. Those three (dsCH6, dsRBF2, dsPde2) were
in /var/tmp when it was wiped on 2026-08-29. Every probe on this box carries the clause, so §78's
conclusion stands: the experiment lost its control group to an unrelated crash and no quantity of
new data restores it. What it needs is a deliberate arm WITHOUT the clause, and `make_task.py` had
no way to build one -- exactly the blocker `--no-unteachable-rules` removed for the other two.

Same shape as that flag and the same test that matters: not that the clause disappears, which one
grep proves, but that NOTHING ELSE MOVES.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MAKE = REPO / "benchmarks" / "algotune" / "make_task.py"
ARENA = Path("/var/tmp/looplab-bench/AlgoTune")

pytestmark = pytest.mark.skipif(not ARENA.exists(), reason="AlgoTune checkout not on this box")

PROBE_FLAGS = ("--full-context", "--deliver", "--one-card", "--enforce-rules")
MARKER = "AND YOU CAN ASK THE REFERENCE ANYTHING"


def _goal(tmp_path, *flags):
    out = tmp_path / ("ws" + str(len(flags)) + flags[-1].replace("-", ""))
    r = subprocess.run(
        [sys.executable, str(MAKE), "--algotune-root", str(ARENA),
         "--task", "pde_heat1d", "--out-dir", str(out), *flags],
        capture_output=True, text=True, timeout=1800,
    )
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    return json.loads(next(out.rglob("algotune_*.json")).read_text())["goal"]


def test_the_control_arm_drops_the_affordance(tmp_path):
    on = _goal(tmp_path, *PROBE_FLAGS)
    off = _goal(tmp_path, *PROBE_FLAGS, "--no-reference-affordance")
    assert MARKER in on
    assert MARKER not in off, "the control arm still offers the reference as a queryable module"


def test_the_control_arm_drops_NOTHING_ELSE(tmp_path):
    """Byte for byte against a card assembled from the module's own constant.

    A control that also drops the eval-cost sentence or the held-out-split warning would measure the
    bundle, not the clause, and would look exactly like an answer to the question §78 asked.
    """
    sys.path.insert(0, str(MAKE.parent))
    try:
        import make_task
    finally:
        sys.path.pop(0)

    on = _goal(tmp_path, *PROBE_FLAGS)
    off = _goal(tmp_path, *PROBE_FLAGS, "--no-reference-affordance")
    assert make_task.REFERENCE_AFFORDANCE in on, "the constant is not what the card carries"
    expected = on.replace(make_task.REFERENCE_AFFORDANCE, "", 1)
    assert off == expected, (
        "the control differs from the shipped card by more than that one clause: %d characters"
        % abs(len(off) - len(expected))
    )


def test_the_default_is_byte_identical_to_the_shipped_card(tmp_path):
    """Sixteen probes ran the ON arm. If the flag moves the default, none of them compares."""
    implicit = _goal(tmp_path, *PROBE_FLAGS)
    explicit = _goal(tmp_path, *PROBE_FLAGS, "--reference-affordance")
    assert implicit == explicit


def test_the_task_still_says_the_reference_file_EXISTS(tmp_path):
    """The affordance is 'you may query it'; the CONTRACT sentence ('reference_<task>.py holds the
    reference solve() and is_solution() -- read it for the contract') is not part of the experiment.
    Removing that too would change the task rather than the clause, and the arm would answer a
    question nobody asked."""
    off = _goal(tmp_path, *PROBE_FLAGS, "--no-reference-affordance")
    assert "reference_pde_heat1d.py holds the reference solve() and is_solution()" in off, off[:400]


def test_the_two_flags_are_independent(tmp_path):
    """Both control arms must be combinable and must not silently imply each other."""
    only_ref = _goal(tmp_path, *PROBE_FLAGS, "--no-reference-affordance")
    assert "THE BEST EVALUATED SOLVER IS WHAT GETS SUBMITTED" in only_ref, (
        "dropping the affordance also dropped KEEP_BEST"
    )
    only_rules = _goal(tmp_path, *PROBE_FLAGS, "--no-unteachable-rules")
    assert MARKER in only_rules, "dropping the two rules also dropped the affordance"
