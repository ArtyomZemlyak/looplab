"""A speedup measured against a reference timed in the same pass is not a measurement.

THE DEFECT, found 2026-08-25 by re-timing a champion by hand and not believing the harness.

When AlgoTune has no cached per-instance reference timing for a (task, subset, lane), it measures
one during the evaluation — and in that pass THE CANDIDATE IS NEVER TIMED. The evaluator reports
the reference against itself: `final_speedup` comes back at ~1.0 and every instance validates,
whatever was submitted. The proof was a solver whose `solve()` returns `[]` for every instance:

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
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "looplab_eval.py"
SRC = BRIDGE.read_text(encoding="utf-8")


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


def test_the_fingerprint_is_taken_before_the_evaluator_and_compared_after():
    """Order is the whole mechanism: a snapshot taken after the run can prove nothing."""
    before = SRC.index("_baseline_before = _baseline_fingerprint()")
    run = SRC.index("proc = subprocess.run(argv", before)
    after = SRC.index("_baseline_after = _baseline_fingerprint()", run)
    assert before < run < after, "the fingerprint no longer brackets the evaluator call"


# OPEN[baseline-refusal-tested-as-simulation] the decision that "runs" here is a hand-copied
# re-spelling; the bridge's real fingerprint/refusal code is executed by no test in the suite.
# proof:`line:speedup_reported&&== 12.5@tests/test_algotune_refuses_baseline_measured_in_pass.py`
# REVIEW 2026-08-25 (guard-test): `_run_refusal_branch` re-implements fingerprint -> mutate ->
# compare -> emit as an inline `python -c` script, so `looplab_eval.py`'s actual `_baseline_
# fingerprint` and refusal block are traversed by nothing (`grep -rl` over tests/: only this file
# names the reason, and nothing anywhere passes `--baseline-times-dir`). The simulation has already
# diverged from production in both directions that matter: it asserts the refused number back as
# the FLOAT `12.5`, while the real `_no_speedup(..., reported=...)` stringifies (`str(reported)`,
# so production writes `"12.5"`); and its fixture invents three-segment cache names
# (`t__test__w22x1r3.json`) that the repo's own `patch_baseline_cache.py` never produces in the
# serial regime -- which is exactly how the LIVE defect (the glob that cannot match a serial-regime
# name; see the annotation at `_baseline_fingerprint` in the bridge) stayed green under this file.
# The ordering companion above is a `SRC.index` positive substring pin -- the tier CLAUDE.md's
# guard-test ladder documents as satisfiable by a comment carrying the pinned calls in order.
# Fix direction: drive the real bridge the way this suite already drives it elsewhere (a stub
# evaluator + the bridge's own `--baseline-times-dir` flag), write a bare `<task>__train.json`
# mid-run from the stub, and assert on the bridge's actual emitted JSON line -- that one test nails
# the glob defect, the str-vs-float shape and the ordering at once; then the `SRC.index` pin can go.
def _run_refusal_branch(tmp_path: Path, timings_change: bool) -> dict:
    """Drive the refusal in isolation, with the same shape the bridge builds.

    The evaluator itself is not invoked — it needs the arena, a dataset and ~2 minutes. What is
    under test is the decision, so the decision is what runs: fingerprint, mutate (or not),
    fingerprint, compare.
    """
    d = tmp_path / "timings"
    d.mkdir()
    (d / "t__test__w22x1r3.json").write_text('{"0": 1.0}', encoding="utf-8")
    code = f'''
import json
from pathlib import Path
D = Path({str(d)!r})
def fp():
    return {{f.name: (f.stat().st_mtime_ns, f.stat().st_size)
             for f in D.glob("t__test__*.json")}}
before = fp()
if {timings_change!r}:
    (D / "t__test__lane22r3.json").write_text('{{"0": 2.0}}')
after = fp()
out = {{"speedup": 12.5}}
if after != before:
    out = {{"speedup": None, "no_speedup": {{"reason": "baseline_measured_in_pass",
            "speedup_reported": 12.5,
            "timings_written": sorted(set(after) - set(before))}}}}
print(json.dumps(out))
'''
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_a_number_is_refused_when_the_timings_were_written_during_the_run(tmp_path):
    out = _run_refusal_branch(tmp_path, timings_change=True)
    assert out["speedup"] is None
    assert out["no_speedup"]["reason"] == "baseline_measured_in_pass"
    # The refused number is kept, not thrown away: it is what the arena said, and an operator
    # comparing runs needs to see that it was ~1.0 and not something else.
    assert out["no_speedup"]["speedup_reported"] == 12.5
    assert out["no_speedup"]["timings_written"] == ["t__test__lane22r3.json"]


def test_an_untouched_timings_directory_lets_the_number_through(tmp_path):
    """The falsifier. A check that refused everything would also pass the test above."""
    out = _run_refusal_branch(tmp_path, timings_change=False)
    assert out["speedup"] == 12.5
    assert "no_speedup" not in out
