"""The AlgoTune goal card's TIMING clause: how the arena actually takes the number it scores.

THE DEFECT. The card told the model what the instances are (`dataset_clause`) and that it may
measure itself (`MEASURE`), and said nothing about the measurement PROCEDURE. Checked against the
goal the live probe ran on (`/var/tmp/looplab-bench/fullctx-probe/ws/algotune_convex_hull.json`,
9 221 characters): `warm`, `process`, `minimum`, `cache` and `memo` occur ZERO times in it between
them, apart from one "warm start" about CP-SAT. So nothing in the card said that a first call is a
warm-up nobody times -- and across arm B and the probes the models raise numba or Cython 1 204
times and talk themselves out of it every time, while NINE of the seventeen published `convex_hull`
champions compile something.

HOW THIS IS PINNED. Twice, in the two ways that can each go wrong on their own:

  * HERMETICALLY, on the GENERATED GOAL, against a fake arena whose config says a different number
    of runs and whose benchmark module clears differently-named caches -- so the sentence is shown
    to be DERIVED rather than typed, in both directions (a fake without a config states nothing).
  * AGAINST THE REAL ARENA, by RUNNING `run_isolated_benchmark` on a solver built to expose one
    fact each and asserting the card's claims against what came back. A table read and a sentence
    written can still disagree about what the harness does; this is the check that cannot.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAKE_TASK = ROOT / "benchmarks" / "algotune" / "make_task.py"

_REAL_ROOT = Path("/var/tmp/looplab-bench/AlgoTune")
_REAL_PYTHON = _REAL_ROOT / ".venv" / "bin" / "python"

_REFERENCE = '''
import numpy as np

from AlgoTuneTasks.base import Task


class Ref(Task):
    def solve(self, problem):
        return []

    def is_solution(self, problem, solution):
        return True
'''

# The two files the clause is derived FROM, faked with values that are not the real arena's: a
# `runs` the real config does not carry and cache names it does not clear. A hand-typed sentence
# would keep saying 3 / `_cache` here and the test would catch it.
_FAKE_CONFIG = """
benchmark:
  runs: 7
  dev_runs: 3
  baseline_timeout: 10000
tasks: {}
"""
_FAKE_ISOLATED = '''
def _clear(module_name, solver_class, cleared_caches):
    for cache_attr in ["_zzz_one", "_zzz_two"]:
        cache = getattr(solver_class, cache_attr, None)
        if isinstance(cache, dict):
            cache.clear()
'''


def _make_root(tmp_path: Path, task: str, *, config: str | None = _FAKE_CONFIG,
               isolated: str | None = _FAKE_ISOLATED) -> Path:
    """A hermetic arena: the task, a train split (without one `--full-context` degrades off), and
    optionally the two files `timing_clause` reads."""
    root = tmp_path / "AlgoTune"
    task_dir = root / "AlgoTuneTasks" / task
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "description.txt").write_text("Find the thing.\n", encoding="utf-8")
    (task_dir / f"{task}.py").write_text(_REFERENCE, encoding="utf-8")
    data = root / ".hf_datasets" / "oripress__AlgoTune" / "data" / task
    data.mkdir(parents=True, exist_ok=True)
    (data / f"{task}_T100ms_n123_size10_train.jsonl").write_text(
        "".join(json.dumps({"id": str(i), "problem": {"a": [1.0, 2.0]}}) + "\n" for i in range(4)),
        encoding="utf-8")
    if config is not None:
        cfg = root / "AlgoTuner" / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "config.yaml").write_text(config, encoding="utf-8")
    if isolated is not None:
        utils = root / "AlgoTuner" / "utils"
        utils.mkdir(parents=True, exist_ok=True)
        (utils / "isolated_benchmark.py").write_text(isolated, encoding="utf-8")
    return root


def _goal(tmp_path: Path, *flags: str, task: str = "fake_task", **kw) -> str:
    """Run the REAL generator and return the goal it wrote."""
    root = _make_root(tmp_path, task, **kw)
    out = tmp_path / ("ws_" + ("_".join(f.lstrip("-") for f in flags) or "default"))
    proc = subprocess.run([sys.executable, str(MAKE_TASK), "--algotune-root", str(root),
                           "--task", task, "--out-dir", str(out), *flags],
                          check=True, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads((out / f"algotune_{task}.json").read_text(encoding="utf-8"))["goal"]


# ------------------------------------------------------------------ the clause, on the shipped goal


def test_the_card_states_the_warmup_and_the_process_it_happens_in(tmp_path):
    """The three facts a model cannot act on without being told: the call is warmed up, the warm-up
    is on a DIFFERENT instance, and the process is fresh. Missing any one of them, "does my JIT's
    first compile land on my clock?" has no answer in the card."""
    goal = _goal(tmp_path)
    assert "FRESH PROCESS" in goal
    assert "UNTIMED WARM-UP" in goal
    assert "DIFFERENT instance" in goal
    assert "A FIRST-CALL COST IS PAID BY THE WARM-UP" in goal


def test_the_run_count_and_the_reduction_are_the_arenas_own(tmp_path):
    """DERIVED from `benchmark.runs`, and the fake says 7 -- a typed "3" passes on the real box and
    fails here, which is the whole point of the fake carrying a different number."""
    goal = _goal(tmp_path)
    assert "rebuilt 7 times per instance" in goal
    assert "FASTEST of the 7" in goal


def test_the_cleared_caches_are_read_out_of_the_benchmark_module(tmp_path):
    goal = _goal(tmp_path)
    assert "`_zzz_one`, `_zzz_two`" in goal, "the names must come from the module, not from prose"
    assert "`cache_clear()`" in goal
    assert "anything under another name survives" in goal


@pytest.mark.parametrize("missing", ["config", "isolated"])
def test_an_arena_that_does_not_say_is_not_quoted(tmp_path, missing):
    """No config, no claim -- and no benchmark module, no cache sentence. Derived means derived in
    both directions: the half that cannot be read must go silent rather than keep quoting the last
    arena somebody looked at."""
    goal = _goal(tmp_path, **{missing: None})
    if missing == "config":
        assert "HOW YOUR TIME IS TAKEN" not in goal, "no run count, no timing sentence at all"
    else:
        assert "HOW YOUR TIME IS TAKEN" in goal, "the run count is still readable"
        assert "cache_clear()" not in goal and "WIPED BETWEEN THE TWO CALLS" not in goal


def test_the_clause_rides_with_full_context(tmp_path):
    """Same rule as `dataset_clause` and `MEASURE`: it is a measured fact about the ruler, and the
    arm that opted out of those must not silently get this one."""
    assert "HOW YOUR TIME IS TAKEN" in _goal(tmp_path)
    assert "HOW YOUR TIME IS TAKEN" not in _goal(tmp_path, "--no-full-context")


# ------------------------------------------------------------------ the same claims, on the arena

# The `__main__` guard is load-bearing, not decoration: `run_isolated_benchmark` uses a FORKSERVER
# context, whose children re-import this file as `__mp_main__`, and without the guard every child
# ran the benchmark again and printed its own (failed, because it is not the parent) result line
# first. The driver measured itself.
_DRIVER = r'''
import json, sys


def main():
    sys.path.insert(0, sys.argv[1])
    from AlgoTuner.utils.isolated_benchmark import run_isolated_benchmark

    res = run_isolated_benchmark(
        task_name="cardcheck", code_dir=sys.argv[2],
        warmup_problem={"a": 1}, timed_problem={"a": 2},
        num_runs=2, timeout_seconds=120.0)
    print("LOOPLAB_RESULT " + json.dumps({
        "success": res.get("success"),
        "warmup_ms": [t / 1e6 for t in res.get("warmup_times_ns", [])],
        "timed_ms": [t / 1e6 for t in res.get("timed_times_ns", [])],
        "min_time_ms": res.get("min_time_ms"),
        "result": res.get("result"),
    }))


if __name__ == "__main__":
    main()
'''

# One solver, three facts: the first `solve()` call is expensive and the later ones are not, an
# `lru_cache` and a module dict are both filled on the way through, and nothing else is going on.
_SOLVER = r'''
import functools
import time

_CALLS = {"n": 0}
_TABLE = {}


@functools.lru_cache(maxsize=None)
def memo(x):
    return x * 2


class Solver:
    def solve(self, problem, **kwargs):
        _CALLS["n"] += 1
        memo(1)
        if _CALLS["n"] == 1:
            time.sleep(0.40)          # stands in for a first-call JIT compile
            _TABLE["built"] = True
        time.sleep(0.01)
        return {"calls": _CALLS["n"], "table": len(_TABLE),
                "memo_currsize": memo.cache_info().currsize,
                "memo_hits": memo.cache_info().hits}
'''


# The `__init__` half. AlgoTuner promises its own agent that "Compilation time of your init function
# will not count towards your function's runtime"; this is the solver that asks the arena whether
# that is true of a constructor in general.
_SOLVER_HEAVY_INIT = r'''
import time


class Solver:
    def __init__(self):
        time.sleep(0.20)

    def solve(self, problem, **kwargs):
        time.sleep(0.01)
        return {"ok": True}
'''


def _drive(tmp_path: Path, solver: str) -> dict:
    """Run the REAL `run_isolated_benchmark` on `solver` and return its timings."""
    code_dir = tmp_path / "code"
    code_dir.mkdir(parents=True, exist_ok=True)
    (code_dir / "solver.py").write_text(solver, encoding="utf-8")
    driver = tmp_path / "drive.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    proc = subprocess.run([str(_REAL_PYTHON), str(driver), str(_REAL_ROOT), str(code_dir)],
                          capture_output=True, text=True, timeout=600, cwd=str(_REAL_ROOT))
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("LOOPLAB_RESULT ")), None)
    assert line, f"driver produced no result:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    out = json.loads(line[len("LOOPLAB_RESULT "):])
    assert out["success"], out
    return out


@pytest.mark.skipif(not _REAL_PYTHON.exists(), reason="no AlgoTune venv on this box")
def test_the_arena_really_does_absorb_the_first_call_in_an_untimed_warmup(tmp_path):
    """Tier 1. The card tells the model a first-call cost never reaches its number, which is an
    invitation to compile; hand the arena a solver whose first call costs 400 ms and check.

    A card that promised this falsely would cost a whole experiment -- the model would put its
    compile in the hot path and read the compile time as its algorithm's time.
    """
    out = _drive(tmp_path, _SOLVER)
    assert out["result"]["calls"] == 2, "the timed call must be the SECOND call in that process"
    assert min(out["warmup_ms"]) > 300.0, out
    assert max(out["timed_ms"]) < 150.0, (
        f"the 400 ms first call reached the timed number: {out}")
    assert out["min_time_ms"] == pytest.approx(min(out["timed_ms"])), (
        "the reported time is the MINIMUM of the timed calls")


@pytest.mark.skipif(not _REAL_PYTHON.exists(), reason="no AlgoTune venv on this box")
def test_the_arena_really_does_clear_one_kind_of_cache_and_not_the_other(tmp_path):
    """The other half of the same sentence, and the half that is easy to get backwards: the card
    says a `functools` cache is emptied between the two calls and other state is not."""
    out = _drive(tmp_path, _SOLVER)
    assert out["result"]["memo_hits"] == 0 and out["result"]["memo_currsize"] == 1, (
        f"the lru_cache survived the warm-up, so the card's `cache_clear()` sentence is wrong: {out}")
    assert out["result"]["table"] == 1, (
        f"the module dict did NOT survive, so the card's 'another name survives' is wrong: {out}")


@pytest.mark.skipif(not (_REAL_ROOT / "AlgoTuner" / "config" / "config.yaml").exists(),
                    reason="no AlgoTune checkout on this box")
def test_the_real_arena_is_described_by_the_real_numbers(tmp_path):
    """The derivation, run against the arena this box actually scores on, so a drift in either file
    shows up here as well as in the hermetic pins above."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_algotune_make_task", MAKE_TASK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runs = module.benchmark_runs(_REAL_ROOT)
    assert isinstance(runs, int) and runs > 0
    clause = module.timing_clause(_REAL_ROOT)
    assert f"FASTEST of the {runs}" in clause
    assert module.cleared_cache_attrs(_REAL_ROOT) == ["_cache", "cache", "_memo", "_results"], (
        "the arena renamed the caches it clears; the card's list is derived and must follow")


@pytest.mark.skipif(not _REAL_PYTHON.exists(), reason="no AlgoTune venv on this box")
def test_the_arena_charges_a_constructor_to_every_timed_call(tmp_path):
    """Tier 1, and a direct contradiction of the arena's OWN prompt line, so it is driven and not
    argued: `Solver()` is constructed inside the timed region, so a 200 ms `__init__` in front of a
    10 ms `solve()` must show up as ~210 ms and not as ~10 ms.

    If this ever goes green the other way, the card's sentence is the one that has to change.
    """
    out = _drive(tmp_path, _SOLVER_HEAVY_INIT)
    assert min(out["timed_ms"]) > 150.0, (
        "the constructor was NOT charged, so the card's `__init__` sentence is wrong: "
        f"{out['timed_ms']}")
    assert min(out["timed_ms"]) < 400.0, out


@pytest.mark.skipif(not _REAL_PYTHON.exists(), reason="no AlgoTune venv on this box")
def test_the_card_states_that_the_constructor_is_timed(tmp_path):
    assert "`__init__` IS ON THE CLOCK" in _goal(tmp_path)
    assert "constructed INSIDE the timed" in _goal(tmp_path)


def test_the_card_does_not_repeat_the_arenas_own_promise_about_init(tmp_path):
    """NEGATIVE pin, substring on purpose (CLAUDE.md): what must not appear is the TEXT.

    The sentence, verbatim from `campaign-final/A-convex_hull.log:119` (and again at `:459`), which
    is the system prompt every arm-A run opens with:

        IMPORTANT: Compilation time of your init function will not count towards your function's
        runtime.

    It is the one line of that prompt this card must not adopt: measured false against this arena's
    own `run_isolated_benchmark` by the test above, where a 200 ms constructor lands in full on the
    timed number. This file is the only place it is quoted -- `make_task.py` says what it says
    without the words, because a commented-out copy would satisfy the substring pinned below.
    """
    goal = _goal(tmp_path)
    for false_promise in ("will not count towards", "does not count towards",
                          "Compilation time of your init"):
        assert false_promise not in goal, f"the card repeats the arena's false promise: {false_promise}"
    source = MAKE_TASK.read_text(encoding="utf-8")
    for false_promise in ("will not count towards your function", "does not count towards your"):
        assert false_promise not in source
