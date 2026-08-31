"""The timer must notice a run tree growing, or archiving it changes nothing.

`snapshot.sh` learned on 2026-08-30 to archive `runs-*` and `model-probes/` -- each run's
events.jsonl and spans.jsonl, the evidence docs/56 is written from and the thing the 2026-08-29
container restart actually destroyed. But `snapshot_timer.sh` decides whether to call it at all, by
fingerprinting a fixed list of directories, and that list was written before run trees were a source.

Measured 2026-08-31 with two probes live: the fingerprint did move, but only because `meter/` was
moving -- the runs were covered by accident, not by design. A run that evaluates locally for twenty
minutes makes no LLM calls, and a probe metered on another port makes none on this meter at all; in
either case the timer reports "nothing new" while the one irreplaceable directory on the box fills
up. That is the same failure the function's own docstring already records for campaign directories,
left in place for a source added after it was written.

The test drives the shell function itself rather than the whole timer, because the claim is about
what the fingerprint can SEE.
"""
import subprocess
import textwrap
from pathlib import Path

TIMER = Path(__file__).resolve().parents[1] / "benchmarks" / "snapshot_timer.sh"


def _fingerprint(root: Path) -> str:
    """Run just the `fingerprint` function against a synthetic BENCH_ROOT."""
    script = textwrap.dedent(f"""
        ROOT={root}
        source <(sed -n '/^fingerprint()/,/^}}/p' {TIMER})
        fingerprint
    """)
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _bench_root(tmp_path: Path) -> Path:
    root = tmp_path / "bench"
    for name in ("meter", "AlgoTune/reports", "looplab/benchmarks/algotune/.baseline_times"):
        (root / name).mkdir(parents=True)
    (root / "meter" / "meter.jsonl").write_text("{}\n")
    run = root / "model-probes" / "accPde" / "runs" / "pde_heat1d" / "run"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text('{"type": "node_evaluated"}\n')
    return root


def test_a_growing_run_tree_moves_the_fingerprint(tmp_path):
    root = _bench_root(tmp_path)
    before = _fingerprint(root)

    # Exactly what a live probe does and nothing else: it appends to its own run log. No LLM call,
    # so `meter/` -- the directory that was covering runs by accident -- does not move.
    run = root / "model-probes" / "accPde" / "runs" / "pde_heat1d" / "run"
    (run / "spans.jsonl").write_text('{"name": "generation"}\n')

    assert _fingerprint(root) != before, (
        "the timer cannot see a run tree grow, so it will report 'nothing new' and snapshot "
        "nothing while the only irreplaceable directory on the box fills up")


def test_a_quiet_box_still_fingerprints_the_same(tmp_path):
    """The other half: the skip has to keep working, or the mount fills with identical copies."""
    root = _bench_root(tmp_path)
    assert _fingerprint(root) == _fingerprint(root)
