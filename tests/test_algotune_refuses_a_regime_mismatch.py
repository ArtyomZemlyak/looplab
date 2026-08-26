"""A baseline keyed for the wrong regime must be refused in a second, not in ten minutes of silence.

`baseline_measured_in_pass` is the right refusal and it works — but it can only speak AFTER the
evaluator returns, and the case that produces it is the case that stops the evaluator returning.
Measured twice on 2026-08-26 through the pinned `eval_train` developer command: a wrong
`ALGOTUNE_EVAL_WORKERS` picks a different baseline REGIME, the cache misses, the reference is
re-timed (~200 s) on top of the evaluation (~330 s), the whole thing exceeds the command's 600 s cap
and is SIGKILLed. `exit=-9; TIMEOUT after 600s`, `(no output)`, twice, and the refusal never printed
a word.

The regimes are `__lane{N}r3` at workers <= 1 and `__w{W}x{C}r3` otherwise, so `w22x1r3` means
TWENTY-TWO workers of one core — not "one eval at a time". That misreading is what put
`ALGOTUNE_EVAL_WORKERS=1` in the probe script and cost two runs; the two references sum to 3898 ms
and 2976 ms over the same hundred instances, a 24 % difference in the denominator of every speedup.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "looplab_eval.py"


def _marker() -> str:
    """The bridge's OWN marker constant, read from its source so the two cannot drift."""
    import re

    src = BRIDGE.read_text(encoding="utf-8")
    m = re.search(r'_SUBSET_PATCH_MARKER = "([^"]+)"', src)
    assert m, "the bridge no longer defines _SUBSET_PATCH_MARKER"
    return m.group(1)


def _run(tmp: Path, cache_names: list[str], workers: str) -> dict:
    """Drive the real bridge with a real cache dir. It must answer BEFORE touching an evaluator."""
    cache = tmp / "times"
    cache.mkdir(exist_ok=True)
    for name in cache_names:
        (cache / name).write_text("{}", encoding="utf-8")
    root = tmp / "AlgoTune"
    (root / "AlgoTuneTasks" / "demo").mkdir(parents=True, exist_ok=True)
    # A STUB EVALUATOR THAT LEAVES A FOOTPRINT. The whole claim is that the guard answers WITHOUT
    # running it, so the test has to be able to tell "it ran and said nothing" from "it never ran".
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    # It carries the SUBSET PATCH MARKER, without which the bridge reports that it is scoring the
    # upstream default ('test') and reassigns `subset` — and the guard, which globs
    # `<task>__<subset>__*`, would then look for the wrong half. That is not a hypothetical: the
    # first version of this test omitted the marker and the guard silently did not fire.
    MARKER = _marker()
    (root / "scripts" / "evaluate_results.py").write_text(
        f"{MARKER}\n"
        "import pathlib, sys\n"
        "print(\"LOOPLAB scoring on the 'train' split (1 problems)\", file=sys.stderr)\n"
        "pathlib.Path(sys.argv[0]).parent.parent.joinpath('EVALUATOR_RAN').write_text('yes')\n",
        encoding="utf-8")
    (tmp / "solver.py").write_text("class Solver:\n    def solve(self, p):\n        return []\n",
                                   encoding="utf-8")
    import os

    env = dict(os.environ, ALGOTUNE_EVAL_WORKERS=workers)
    out = subprocess.run(
        [sys.executable, str(BRIDGE), "--algotune-root", str(root), "--task", "demo",
         "--model", "T", "--solver", str(tmp / "solver.py"), "--subset", "train",
         "--baseline-times-dir", str(cache), "--timeout", "30"],
        capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp))
    for line in reversed((out.stdout or "").splitlines()):
        if line.strip().startswith("{"):
            row = json.loads(line)
            row["_evaluator_ran"] = (root / "EVALUATOR_RAN").exists()
            return row
    raise AssertionError(f"the bridge printed no JSON line:\n{out.stdout}\n{out.stderr}")


def test_a_foreign_regime_on_disk_is_refused_before_anything_is_timed(tmp_path):
    """workers=1 keys `__lane<N>r3`; only a `__w..x..r3` entry exists. It must refuse, and fast."""
    import time

    started = time.time()
    row = _run(tmp_path, ["demo__train__w22x1r3.json"], workers="1")
    assert time.time() - started < 60, "it went off to measure instead of refusing"
    assert row["_evaluator_ran"] is False, (
        "the evaluator RAN — the refusal came too late to save the ten minutes it exists for")
    assert row.get("speedup") is None, row
    assert (row.get("no_speedup") or {}).get("reason") == "baseline_regime_mismatch", row
    detail = (row.get("no_speedup") or {}).get("detail") or ""
    assert "w22x1r3" in detail, "the refusal does not name what IS on disk"
    assert "ALGOTUNE_EVAL_WORKERS" in detail, "it does not say which knob to turn"


def test_an_empty_cache_is_left_alone(tmp_path):
    """The falsifier for a guard that blocks a legitimate FIRST measurement."""
    row = _run(tmp_path, [], workers="1")
    assert (row.get("no_speedup") or {}).get("reason") != "baseline_regime_mismatch", row


def test_the_matching_regime_is_not_refused(tmp_path):
    """workers=1 keys `__lane<N>r3` and that entry is present — nothing to complain about."""
    import os

    width = len(os.sched_getaffinity(0))
    row = _run(tmp_path, [f"demo__train__lane{width}r3.json"], workers="1")
    assert (row.get("no_speedup") or {}).get("reason") != "baseline_regime_mismatch", row


def test_a_different_subset_is_not_the_same_baseline(tmp_path):
    """`__test__` entries say nothing about what `__train__` will key."""
    row = _run(tmp_path, ["demo__test__w22x1r3.json"], workers="1")
    assert (row.get("no_speedup") or {}).get("reason") != "baseline_regime_mismatch", row


def test_the_replicated_rule_matches_the_arenas_own():
    """The guard replicates `resolve_workers` rather than importing it (see its docstring). This is
    the drift check: where the arena IS importable, the two must agree on every setting."""
    import os

    try:
        from AlgoTuner.utils.evaluator.looplab_parallel import resolve_workers
    except Exception:                                   # noqa: BLE001
        import pytest

        pytest.skip("AlgoTuner not importable here; the replica has nothing to be checked against")

    width = len(os.sched_getaffinity(0))
    for raw in ("auto", "max", "1", "4", "", "nonsense"):
        theirs_w, theirs_c = resolve_workers({"ALGOTUNE_EVAL_WORKERS": raw} if raw else {})
        theirs = (f"__lane{width}r3" if theirs_w <= 1 else f"__w{theirs_w}x{theirs_c}r3")
        cores = 1
        if raw in ("auto", "max"):
            mine_w = max(1, width // cores)
        else:
            try:
                mine_w = int(raw)
            except ValueError:
                mine_w = 1
        mine = f"__lane{width}r3" if mine_w <= 1 else f"__w{mine_w}x{cores}r3"
        assert mine == theirs, f"{raw!r}: replica says {mine}, arena says {theirs}"


def test_auto_keys_a_worker_regime_not_a_lane_one(tmp_path):
    """The `auto` branch, driven through the bridge so it needs no arena import.

    This is the misreading that cost two runs: `w22x1r3` is TWENTY-TWO workers of one core, not
    "one eval at a time". With `auto` the run keys `__w<N>x1r3`, so a lane-keyed cache is the
    foreign one — the mirror image of the first test, and a mutation that collapses `auto` to a
    single worker turns it red.
    """
    import os

    width = len(os.sched_getaffinity(0))
    row = _run(tmp_path, [f"demo__train__lane{width}r3.json"], workers="auto")
    assert (row.get("no_speedup") or {}).get("reason") == "baseline_regime_mismatch", row
    detail = (row.get("no_speedup") or {}).get("detail") or ""
    assert "__w" in detail and "x1r3" in detail, f"auto did not key a worker regime: {detail}"
    assert f"lane{width}r3" in detail, "the refusal does not name the lane-keyed entry it found"
