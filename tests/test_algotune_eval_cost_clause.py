"""What the goal card says one `eval_train` call COSTS, pinned against what it measurably costs.

THE DEFECT. The card offered the capability and then argued against using it, on a price nobody had
measured: "A real evaluation of this task takes roughly half a minute to six minutes ... so ONE call
can be a third of everything you have ... measure ONCE when you have something worth measuring, and
never twice on the same code."

MEASURED, 2026-08-27, over every `eval_train` call this harness has on disk -- the `run_dev_command`
tool spans in `fullctx-probe*/runs/*/run/spans.jsonl` and `model-probes/*/runs/*/run/spans.jsonl`,
56 calls across 10 runs: 54 completed, median 39.6 s, min 28.5 s, max 79.9 s, and the scorer's own
`eval_seconds` in the same runs' `nodes/*/score.log` agrees. Against the 1200 s the Developer's
session is bounded at, one call is 3 % of it. The two that did not complete are the "six minutes":
both hit the THEN-600 s cap and returned `exit=-9` with `(no output)`.

WHY IT MATTERS ENOUGH TO PIN. AlgoTuner's own agent is handed this number automatically after every
edit -- 57 evaluations on `convex_hull` alone. Ours, in the ten runs that had the command, called it
1 to 13 times (median 4.5), and a card that says a measurement costs a third of the session and must
never be repeated is a card that says do not use this tool.

The numbers here cannot be derived at build time -- the cost of an evaluation is a property of runs
that have not happened yet -- so they are stated constants, and what these tests can still pin is
(a) the retired sentence never comes back, (b) the two numbers that ARE derivable (the command's
timeout and the session budget) are the ones the card quotes, and (c) the advice matches the price.
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


def _make_root(tmp_path: Path, task: str) -> Path:
    root = tmp_path / "AlgoTune"
    task_dir = root / "AlgoTuneTasks" / task
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "description.txt").write_text("Find the thing.\n", encoding="utf-8")
    (task_dir / f"{task}.py").write_text(_REFERENCE, encoding="utf-8")
    data = root / ".hf_datasets" / "oripress__AlgoTune" / "data" / task
    data.mkdir(parents=True, exist_ok=True)
    (data / f"{task}_T100ms_n123_size10_train.jsonl").write_text(
        "".join(json.dumps({"id": str(i), "problem": {"a": [1.0]}}) + "\n" for i in range(3)),
        encoding="utf-8")
    return root


def _spec(tmp_path: Path, *flags: str, task: str = "fake_task") -> dict:
    root = _make_root(tmp_path, task)
    out = tmp_path / ("ws_" + ("_".join(f.lstrip("-") for f in flags) or "default"))
    # THE ENGINE HAS TO BE REACHABLE FROM THE CHILD, or this builds the OTHER card. `make_task.py`
    # is run BY PATH, so `sys.path[0]` is `benchmarks/algotune` and the repo root is not on it
    # whatever pytest's own rootdir does; `session_budget_s()` then returns None and the card says
    # "a wall clock nobody shows you" instead of the fraction. That is the same defect the two
    # drivers had (`campaign.sh` exported PYTHONPATH, `run_probe.sh` did not, and their cards
    # differed by exactly this sentence), so the test states it the way they now both do rather
    # than inheriting whatever PYTHONPATH the caller happened to have.
    import os

    subprocess.run([sys.executable, str(MAKE_TASK), "--algotune-root", str(root),
                    "--task", task, "--out-dir", str(out), *flags],
                   check=True, capture_output=True, text=True,
                   env=dict(os.environ, PYTHONPATH=str(ROOT)))
    return json.loads((out / f"algotune_{task}.json").read_text(encoding="utf-8"))


def _module():
    spec = importlib.util.spec_from_file_location("_algotune_make_task", MAKE_TASK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_retired_price_never_comes_back(tmp_path):
    """NEGATIVE pin, substring on purpose (CLAUDE.md): what must not return is the TEXT. Pinned on
    the generated goal AND on the source, because a commented-out copy is one uncomment away."""
    goal = _spec(tmp_path)["goal"]
    source = MAKE_TASK.read_text(encoding="utf-8")
    for retired in ("a third of everything you have",
                    "roughly half a minute to six minutes",
                    "measure ONCE when you have something worth measuring",
                    "never twice on the same code"):
        assert retired not in goal, f"the card is quoting the unmeasured price again: {retired!r}"
        assert retired not in source, f"the retired sentence is back in the generator: {retired!r}"


def test_the_card_states_the_measured_cost_and_the_session_it_is_charged_to(tmp_path):
    goal = _spec(tmp_path)["goal"]
    assert "IT COSTS ABOUT 40 SECONDS" in goal
    assert "40 s median, 29 s fastest, 80 s slowest" in goal
    assert "CHARGED TO A CLOCK YOU CANNOT SEE" in goal, "the honest half of the old sentence stays"
    assert "ends with no file written has produced nothing" in goal


def test_the_card_tells_the_model_to_measure_repeatedly(tmp_path):
    """The behaviour the price exists to permit. AlgoTuner's agent gets this number after every
    edit (57 times on `convex_hull`); a card whose cost paragraph rations it to one call per run is
    the reason ours did not."""
    goal = _spec(tmp_path)["goal"]
    assert "MEASURING IS THE CHEAPEST INFORMATION HERE" in goal
    assert "measure, change ONE thing, measure again" in goal
    assert "write the solver FIRST" in goal, "measuring early must not become measuring instead"


def test_the_quoted_cap_is_the_commands_own_timeout(tmp_path):
    """The one number in that paragraph that CAN drift, so it is derived from the same constant the
    command is pinned at rather than typed twice. A card promising a 450 s ceiling beside a command
    killed at 600 s teaches the model to trust a number that will cost it half a session."""
    spec = _spec(tmp_path)
    command = next(c for c in spec["developer_commands"] if c["name"] == "eval_train")
    assert command["timeout"] == _module().DEV_EVAL_TIMEOUT_S
    assert f"KILLED at {command['timeout']:.0f} s" in spec["goal"]
    assert "returns nothing at all" in spec["goal"], (
        "a killed call returning `(no output)` is the failure that ended two sessions")


def test_the_session_budget_comes_from_the_engine_that_enforces_it(tmp_path):
    """`Settings.developer_session_time_budget_s` is what the run obeys and what every snapshot on
    this box carries (1200.0). The card states the RATIO between one call and the session, so a
    hand-copied number would go on being quoted after the default moved."""
    module = _module()
    from looplab.core.config import Settings

    budget = Settings().developer_session_time_budget_s
    assert module.session_budget_s() == budget
    goal = _spec(tmp_path)["goal"]
    assert f"bounded at {budget:.0f} s of wall clock" in goal
    assert f"about {round(100 * 40.0 / budget):.0f} % of your session" in goal


def test_without_an_engine_to_ask_the_card_states_no_fraction(tmp_path, monkeypatch):
    """No measurement, no claim -- the rule the rest of this file already follows. A generator run
    where `looplab` is not importable must state the seconds and skip the denominator rather than
    invent one."""
    module = _module()
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def fail(name, *args, **kwargs):
        if name.startswith("looplab.core.config"):
            raise ImportError("no engine here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fail)
    assert module.session_budget_s() is None
    sentence = module.eval_cost_sentence()
    assert "IT COSTS ABOUT 40 SECONDS" in sentence
    assert "% of your session" not in sentence
