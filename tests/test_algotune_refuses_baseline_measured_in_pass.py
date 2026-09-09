"""A speedup measured against a reference timed in the same pass is not a measurement.

THE DEFECT, found 2026-08-25 by re-timing a champion by hand and not believing the harness.

When AlgoTune has no cached per-instance reference timing for a (task, subset, lane), it measures
one during the evaluation — and in that pass THE CANDIDATE IS NEVER TIMED. The evaluator reports the
reference against itself: `final_speedup` comes back at ~1.0 and every instance validates, whatever
was submitted. The proof was a solver whose `solve()` returns `[]` for every instance:

    fresh timings : speedup 1.0009, 100/100 valid, 326 s
    warm timings  : the real champion scored 0.0 (98/100 valid), 120 s

The same split showed on a second task: `edge_expansion`'s champion scored 0.9996 cold and 24.68
warm. It cost eight of one campaign's twenty final numbers — 1.146, 1.069, 1.0646, 1.0362, 1.0308,
1.0243, 0.9865 — every one of them plausible, none of them about the candidate, and they were read
for hours as a genuine train/test collapse. The only tell was the clock: those eight evaluations
ran ~330 s against ~50 s for the eleven with a warm cache, the extra ~210 s being the reference
pass itself.

So the bridge fingerprints the timings directory before the evaluator runs and again after. A file
that appears or changes means the reference was measured here, and the number is refused with
`no_speedup.reason = "baseline_measured_in_pass"` rather than printed.

HOW THIS IS TESTED, and what changed on 2026-09-08. The decision used to "run" here as a hand-copied
`python -c` re-spelling of fingerprint → mutate → compare → emit, so `looplab_eval.py`'s actual
`_baseline_fingerprint` and refusal block were traversed by nothing in the suite. That simulation
had already diverged from production in both directions that matter: it asserted the refused number
back as the FLOAT `12.5` while `_no_speedup(..., reported=...)` stringifies it, and its fixture
invented three-segment cache names (`t__test__w22x1r3.json`) that `patch_baseline_cache.py` never
produces in the serial regime — which is exactly how the LIVE glob defect (a pattern that could not
match a serial-regime name) stayed green under this file.

What runs now is the bridge itself, against a stub evaluator that writes a bare `<task>__test.json`
mid-run — the shape the campaign really produces — with the bridge's own `--baseline-times-dir`
pointed at a tmp directory. That single test nails the glob, the str-vs-float shape and the ORDER at
once: a fingerprint taken after the evaluator would see the same directory twice and let the number
through, which is the falsifier test below in reverse.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "looplab_eval.py"
TASK = "spectral_clustering"
# What the arena would report for the reference timed against ITSELF: plausible, near 1.0, and about
# nothing. The refusal has to survive being handed a number, not just a missing one.
REPORTED = "12.5"

# The evaluator, as far as the bridge can observe it: an argv contract, a summary file, and — when
# `ALGOTUNE_TEST_TIMINGS_DIR` is set — the per-instance reference timings written DURING the pass,
# which is the entire fact under test. The name is the serial-regime one `patch_baseline_cache.py`
# really writes when workers <= 1 (`<task>__<subset>.json`, no regime segment), which docs/51 §10
# mandates and `campaign.sh` never overrides.
_STUB = '''\
import argparse, json, os, sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--models", nargs="+")
ap.add_argument("--tasks", nargs="+")
ap.add_argument("--output", type=Path)
args = ap.parse_args()

timings = os.environ.get("ALGOTUNE_TEST_TIMINGS_DIR")
if timings:
    d = Path(timings)
    d.mkdir(parents=True, exist_ok=True)
    (d / (args.tasks[0] + "__test.json")).write_text('{{"0": 1.0}}', encoding="utf-8")

sys.stderr.write("WARNING - Evaluation complete\\n")
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps({{args.tasks[0]: {{args.models[0]:
                       {{"final_speedup": "{reported}"}}}}}}), encoding="utf-8")
sys.exit(0)
'''


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "AlgoTune"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "evaluate_results.py").write_text(_STUB.format(reported=REPORTED), encoding="utf-8")
    return root


def _score(tmp_path: Path, *, timings_written: bool) -> dict:
    """Run the REAL bridge on the graded split and parse its one JSON line."""
    root = _checkout(tmp_path)
    # The solver gets its own directory: everything beside it is its SUBMISSION, and a stray
    # `cache.json` in the same tree would be copied into the scored directory.
    work = tmp_path / "candidate"
    work.mkdir()
    (work / "solver.py").write_text("class Solver:\n    def solve(self, problem):\n"
                                    "        return []\n", encoding="utf-8")
    times = tmp_path / "times"
    times.mkdir()
    env = dict(os.environ)
    env.pop("ALGOTUNE_TEST_TIMINGS_DIR", None)
    if timings_written:
        env["ALGOTUNE_TEST_TIMINGS_DIR"] = str(times)
    proc = subprocess.run(
        [sys.executable, str(BRIDGE), "--algotune-root", str(root), "--task", TASK,
         "--solver", str(work / "solver.py"), "--subset", "test",
         # The bridge's own flag, which nothing in this suite used to pass — so the directory it
         # watches was never the directory a test wrote to.
         "--baseline-times-dir", str(times),
         # NEVER the default: that path is the campaign's real, shared parity cache.
         "--baseline-cache", str(tmp_path / "cache.json")],
        capture_output=True, text=True, timeout=300, env=env)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert lines, f"the bridge printed no JSON line\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    return json.loads(lines[-1])


def test_the_reason_is_registered_so_emit_cannot_downgrade_it():
    """`_emit` rewrites any reason outside `NO_SPEEDUP_REASONS` to "unknown".

    Unregistered, the refusal would ship as a bare `unknown` and read like the parse failures it
    must be told apart from. The tuple is IMPORTED, not scraped: the first version searched the
    source text and matched the mention inside a docstring, so it failed against a vocabulary that
    was in fact correct.
    """
    spec = importlib.util.spec_from_file_location("looplab_eval_under_test", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "baseline_measured_in_pass" in mod.NO_SPEEDUP_REASONS


def test_a_number_is_refused_when_the_timings_were_written_during_the_run(tmp_path):
    """The bridge's own refusal block, executed: reference timed here, so the number is not
    about the candidate and is not printed as one."""
    out = _score(tmp_path, timings_written=True)
    assert out["speedup"] is None
    why = out["no_speedup"]
    assert why["reason"] == "baseline_measured_in_pass"
    # The refused number is kept, not thrown away: it is what the arena said, and an operator
    # comparing runs needs to see that it was ~1.0 and not something else. A STRING, because
    # `_no_speedup` stringifies — "N/A" and "0.0000" are different facts that `float()` merges.
    assert why["speedup_reported"] == REPORTED and isinstance(why["speedup_reported"], str)
    # THE SERIAL-REGIME NAME, which the old hand-written fixture could not produce and which the
    # live glob could therefore not match: `<task>__<subset>.json`, no regime segment.
    assert why["timings_written"] == [f"{TASK}__test.json"], why
    assert "cached" in why["remedy"], "an operator has to be told the second pass is now cheap"
    assert "NOT A MEASUREMENT" in out["baseline_source"]


def test_an_untouched_timings_directory_lets_the_number_through(tmp_path):
    """The falsifier — and the ORDER pin. A check that refused everything would pass the test
    above; a fingerprint taken AFTER the evaluator instead of before would pass this one and fail
    that one, since both snapshots would then see the same directory."""
    out = _score(tmp_path, timings_written=False)
    assert out["speedup"] == 12.5
    assert "no_speedup" not in out
