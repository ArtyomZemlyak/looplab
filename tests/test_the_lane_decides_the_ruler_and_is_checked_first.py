"""A lane whose width keys a baseline the box does not have is refused before the first dollar.

WHAT THIS IS ABOUT, measured 2026-09-18. The first smoke probe after the stand was restored ran on
lane `0-10` and died on its FIRST evaluated node with

    no_speedup{reason: baseline_regime_mismatch}: this invocation would key its baseline
    '__w11x1r3', which is not on disk, while edge_expansion__train__w22x1r3.json already is --
    so it would re-time the reference in this pass and divide by a different denominator than
    whoever wrote those entries.

That refusal is correct and it is the one thing standing between this bench and a corpus measured
on two different rulers. But it arrives after the proposal, after the build and after $0.2176 --
50 minutes in -- and the fact it refuses on is a property of the LANE, known at launch.

`ALGOTUNE_EVAL_WORKERS=auto` resolves workers from the lane's width, so the key is a function of
the lane: `0-10` is 11 CPUs and keys `__w11x1r3`; `0-10,48-58` is those 11 cores plus their
hyperthread siblings, 22 CPUs, and keys `__w22x1r3`. Every one of the 141 archived probes carries a
lane of TWO ranges; not one carries a single range. The single-range lane was mine, and nothing on
the way in disagreed with it.

The key is derived by the EVALUATOR's own `eval_regime()` under the same `taskset`, not by a copy
of the rule in shell: a copy would part company with it on the first edit.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "benchmarks" / "algotune" / "run_probe.sh"
SRC = SCRIPT.read_text(encoding="utf-8")
STAND = Path("/var/tmp/looplab-bench")

sys.path.insert(0, str(REPO / "benchmarks" / "algotune"))


def _key_under(lane: str) -> str:
    """What the shipped evaluator keys under this lane -- the function the guard calls."""
    out = subprocess.run(
        ["taskset", "-c", lane, sys.executable, "-c",
         "import sys; sys.path.insert(0, %r)\n"
         "from looplab_eval import eval_regime\nprint(eval_regime().get('key') or '')"
         % str(REPO / "benchmarks" / "algotune")],
        capture_output=True, text=True, timeout=120,
        env=dict(os.environ, ALGOTUNE_EVAL_WORKERS="auto"))
    return out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""


def test_the_lane_width_is_what_picks_the_ruler():
    """The fact the guard rests on, driven rather than asserted: one range and two of the same
    cores key different baselines, so a lane is not a scheduling detail."""
    if len(os.sched_getaffinity(0)) < 60:
        pytest.skip("this box cannot offer both lanes")
    assert _key_under("0-10") == "__w11x1r3"
    assert _key_under("0-10,48-58") == "__w22x1r3"


@pytest.mark.skipif(not (STAND / "AlgoTune").is_dir(), reason="no bench stand on this box")
def test_a_lane_with_no_cached_baseline_is_refused_at_launch(tmp_path):
    from tests._bench_fixtures import stand_launch_env
    env = {**stand_launch_env(), "PROBE_DRY_RUN": "1", "PROBE_OUT_ROOT": str(tmp_path)}
    got = subprocess.run(
        ["bash", str(SCRIPT), "deepseek-v4-flash", "lanecheck", "0-10", "edge_expansion",
         "http://127.0.0.1:8801", "0.35"],
        capture_output=True, text=True, timeout=600, env=env)
    assert got.returncode == 1, got.stdout + got.stderr
    assert "__w11x1r3" in got.stdout, "the refusal must name the ruler the lane would key"
    assert "0-10,48-58" in got.stdout, "and the lane that keys the one on disk"
    assert not (tmp_path / "lanecheck" / "INSTRUMENT.txt").exists(), \
        "it refused after writing the record, which is the ordering the guard exists to fix"


@pytest.mark.skipif(not (STAND / "AlgoTune").is_dir(), reason="no bench stand on this box")
def test_the_corpus_lane_passes_every_check(tmp_path):
    """A guard that refuses the lane the corpus was measured on would stop the bench dead."""
    from tests._bench_fixtures import stand_launch_env
    env = {**stand_launch_env(), "PROBE_DRY_RUN": "1", "PROBE_OUT_ROOT": str(tmp_path)}
    got = subprocess.run(
        ["bash", str(SCRIPT), "deepseek-v4-flash", "lanecheck2", "0-10,48-58", "edge_expansion",
         "http://127.0.0.1:8801", "0.35"],
        capture_output=True, text=True, timeout=600, env=env)
    assert got.returncode == 0, got.stdout + got.stderr
    assert (tmp_path / "lanecheck2" / "INSTRUMENT.txt").is_file()


def test_the_rule_is_the_evaluators_own_and_the_filename_is_not_reinvented():
    """Two things a shell copy got wrong once each: the derivation, and the underscores. The key
    carries its own (`__w22x1r3`), so the file is `<task>__train` + key -- a third underscore
    names a path no lane can produce, and the guard then refuses every lane alike."""
    assert "from looplab_eval import eval_regime" in SRC
    assert '${TASK}__train${REGIME_KEY}.json' in SRC
    assert '${TASK}__train__${REGIME_KEY}.json' not in SRC


def test_it_runs_before_the_card_the_record_and_the_money():
    guard = SRC.index("REGIME_KEY=$(")
    assert guard < SRC.index('algotune/make_task.py"'), "before the card"
    assert guard < SRC.index('echo "probe:          $LABEL"'), "before the instrument record"
    assert guard < SRC.index('taskset -c "$LANE" python -m looplab.cli run'), "before the money"
