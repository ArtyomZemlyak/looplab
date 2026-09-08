"""The bridge's evaluator deadline must sit UNDER the ceiling of whatever is running it.

THE DEFECT, closed 2026-09-08. `make_task.py` built both invocations of `looplab_eval.py` -- the
operator `score` stage and the pinned `eval_train` developer command -- with no `--timeout`, so the
bridge ran its own default 7200 s evaluator clock inside a stage whose ceiling is the SAME
`args.timeout` (default 7200) and inside a dev command capped at 450 s. Whenever the evaluator would
have reached the bridge's deadline, the outer kill landed first: SIGKILL, no stdout, engine reason
`timeout` -- which is in `metric_salvage.NEVER_SALVAGED_REASONS`, so the node is DISCARDED rather
than scored. The bridge's own timeout branch exists precisely to prevent that ("a timed-out solver
is a wrong solver, not a missing measurement", `looplab_eval.py`), and it was unreachable through
the only pipeline the campaign runs.

WHAT IS PINNED HERE. The ordering property, on the REAL generated card (a hermetic arena, the same
fixture shape `test_algotune_timing_clause.py` uses) -- every invocation of the bridge in the spec
carries a `--timeout`, and every one of them is strictly under the ceiling of the command that
carries it. Plus the truth table of `bridge_timeout` itself, including the short ceiling where a
flat "minus the build ceiling" rule would go negative.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKE_TASK = ROOT / "benchmarks" / "algotune" / "make_task.py"

_REFERENCE = '''
import numpy as np

from AlgoTuneTasks.base import Task


class Ref(Task):
    def solve(self, problem):
        return []

    def is_solution(self, problem, solution):
        return True
'''


def _by_path(path: Path, name: str):
    """Import a `benchmarks/algotune/*.py` script by path -- they are scripts, not a package."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MT = _by_path(MAKE_TASK, "make_task_under_test")


def _spec(tmp_path: Path, *flags: str, task: str = "fake_task") -> dict:
    """Run the REAL generator against a hermetic arena and return the task card it wrote."""
    root = tmp_path / "AlgoTune"
    task_dir = root / "AlgoTuneTasks" / task
    task_dir.mkdir(parents=True)
    (task_dir / "description.txt").write_text("Find the thing.\n", encoding="utf-8")
    (task_dir / f"{task}.py").write_text(_REFERENCE, encoding="utf-8")
    data = root / ".hf_datasets" / "oripress__AlgoTune" / "data" / task
    data.mkdir(parents=True)
    (data / f"{task}_T100ms_n123_size10_train.jsonl").write_text(
        "".join(json.dumps({"id": str(i), "problem": {"a": [1.0]}}) + "\n" for i in range(4)),
        encoding="utf-8")
    out = tmp_path / "ws"
    proc = subprocess.run([sys.executable, str(MAKE_TASK), "--algotune-root", str(root),
                           "--task", task, "--out-dir", str(out), *flags],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    return json.loads((out / f"algotune_{task}.json").read_text(encoding="utf-8"))


def _deadline(command: list[str]) -> int:
    """The `--timeout` the card hands the bridge, or -1 when it hands it none."""
    for i, word in enumerate(command):
        if word == "--timeout":
            return int(command[i + 1])
    return -1


def test_the_score_stage_hands_the_bridge_a_deadline_under_its_own(tmp_path):
    """The stage that produces the run's metric. A deadline equal to the stage's is the defect."""
    spec = _spec(tmp_path, "--timeout", "7200")
    stage = spec["eval"]["stages"][0]
    assert stage["name"] == "score"
    inner, outer = _deadline(stage["command"]), stage["timeout"]
    assert inner > 0, "the bridge would run its own 7200 s default inside this stage"
    assert inner < outer, f"the bridge's deadline {inner} must fire before the stage's {outer}"


def test_the_pinned_eval_train_command_does_the_same(tmp_path):
    """`--full-context` is what puts `eval_train` on the card; its 450 s cap is the tighter case."""
    spec = _spec(tmp_path, "--full-context")
    commands = {c["name"]: c for c in spec["developer_commands"]}
    train = commands["eval_train"]
    inner, outer = _deadline(train["command"]), train["timeout"]
    assert inner > 0 and inner < outer, (inner, outer)
    assert outer == MT.DEV_EVAL_TIMEOUT_S, "the cap moved; this test must follow the ONE constant"


def test_a_shorter_stage_ceiling_still_leaves_room_to_evaluate(tmp_path):
    """A non-default `--timeout` must not produce a deadline that is negative, zero, or the
    ceiling itself -- the three ways a reserve rule goes wrong on a short clock."""
    spec = _spec(tmp_path, "--timeout", "600")
    stage = spec["eval"]["stages"][0]
    inner = _deadline(stage["command"])
    assert 60 <= inner < stage["timeout"], inner


def test_the_reserve_covers_the_build_the_bridge_runs_before_the_evaluator_clock_starts():
    """The rule's truth table, where the constants that make it are declared.

    The bridge compiles the submission with its own 1800 s ceiling BEFORE `subprocess.run(argv,
    timeout=...)` starts, so on a generous ceiling the whole build has to fit in the reserve; on a
    tight one a flat subtraction would go negative, so the reserve is capped at a quarter.
    """
    assert MT.bridge_timeout(7200) + MT.BRIDGE_BUILD_CEILING_S <= 7200, (
        "a build that uses its full ceiling must still leave the inner clock to fire first")
    assert MT.bridge_timeout(450) < 450
    assert MT.bridge_timeout(80) >= 60, "the floor keeps a hand-set tiny ceiling usable"
    assert MT.bridge_timeout(7200) > MT.bridge_timeout(450), "it must scale with the ceiling"


def test_the_build_ceiling_the_reserve_is_made_of_is_still_the_bridges():
    """The one number this rule borrows from ANOTHER file, joined to it.

    `BRIDGE_BUILD_CEILING_S` mirrors the `timeout=1800` `looplab_eval.py` gives its
    `setup.py build_ext --inplace` child -- seconds spent inside the stage's ceiling and outside the
    evaluator's, which is the whole reason the reserve exists. Nothing at runtime reads the bridge's
    constant, so this is the tier-3 join CLAUDE.md's guard ladder allows for the residue: if the two
    come apart, the reserve is sized against a build that no longer happens.
    """
    bridge_src = (ROOT / "benchmarks" / "algotune" / "looplab_eval.py").read_text(encoding="utf-8")
    assert "timeout=1800" in bridge_src, (
        "looplab_eval.py's build_ext ceiling moved; re-derive BRIDGE_BUILD_CEILING_S from it")
    assert MT.BRIDGE_BUILD_CEILING_S == 1800.0
