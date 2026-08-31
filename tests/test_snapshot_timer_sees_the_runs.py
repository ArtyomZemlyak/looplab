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


# ------------------------------------------------------------------------------------ and the LOOP
#
# The two cases above drive `fingerprint` alone. What DECIDES anything is `_loop`, three lines that
# read: fingerprint moved -> call `snapshot.sh` -> record the fingerprint ONLY when it exited 0.
# Nothing exercised them, and this is the loop the 3.0 GB incident went through: a snapshot that
# kept exiting 1 was correctly never recorded, so every tick re-wrote 110 MB, nine times, with no
# prune (which sits downstream of the completeness check).
#
# The loop is driven for real -- the actual script, in `_loop` mode, on a one-second interval, with
# a STUB `snapshot.sh` beside it so its exit code is the variable and no snapshot is taken.

import os  # noqa: E402
import signal  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

_STUB = """#!/bin/bash
echo call >> "$STUB_CALLS"
echo "stub snapshot"
exit "$(cat "$STUB_RC")"
"""


def _loop_box(tmp_path):
    """A copy of the real timer with a stub `snapshot.sh` as its sibling.

    A copy, because `_loop` resolves the script it calls as `$HERE/snapshot.sh` from its own
    dirname -- which is precisely the seam under test, so it is exercised rather than bypassed.
    """
    binroot = tmp_path / "bin"
    binroot.mkdir()
    (binroot / "snapshot_timer.sh").write_text(TIMER.read_text(encoding="utf-8"), encoding="utf-8")
    stub = binroot / "snapshot.sh"
    stub.write_text(_STUB, encoding="utf-8")
    stub.chmod(0o755)
    return binroot, _bench_root(tmp_path)


def _start_loop(binroot, root, tmp_path, rc="0", tag="calls"):
    (tmp_path / "rc").write_text(rc)
    calls = tmp_path / tag                      # a fresh ledger per loop, never a running total
    proc = subprocess.Popen(
        ["bash", str(binroot / "snapshot_timer.sh"), "_loop", "1"],
        env={**os.environ, "BENCH_ROOT": str(root), "STUB_CALLS": str(calls),
             "STUB_RC": str(tmp_path / "rc")},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    return proc, calls


def _calls(path):
    return len(path.read_text().splitlines()) if path.exists() else 0


def _wait_for_calls(path, n, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if _calls(path) >= n:
            return True
        time.sleep(0.05)
    return False


def _stop_loop(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return proc.communicate(timeout=30)[0]


def test_a_quiet_box_is_snapshotted_once_and_a_change_wakes_it_again(tmp_path):
    """The skip is what keeps the mount from filling with identical copies -- and it must not be a
    skip that never ends: the next real measurement has to wake it."""
    binroot, root = _loop_box(tmp_path)
    proc, calls = _start_loop(binroot, root, tmp_path, rc="0")
    try:
        assert _wait_for_calls(calls, 1), "the timer never snapshotted a box it had not seen"
        time.sleep(3.5)                                    # three more ticks at a 1 s interval
        assert _calls(calls) == 1, (
            f"a quiet box was snapshotted {_calls(calls)} times; at the shipped 1800 s interval "
            "that is the mount filling with identical copies")

        # Exactly what a live probe does: it appends to its own run log.
        (root / "model-probes" / "accPde" / "runs" / "pde_heat1d" / "run" / "spans.jsonl"
         ).write_text('{"name": "generation"}\n')
        assert _wait_for_calls(calls, 2), (
            "the fingerprint moved and the timer did not snapshot; the insurance against a "
            "container restart mid-arm is exactly this tick")
    finally:
        out = _stop_loop(proc)
    assert "nothing new since the last snapshot; skipping" in out, out


def test_a_snapshot_that_exited_nonzero_is_retried_instead_of_being_recorded_as_done(tmp_path):
    """`last` advances ONLY on success, and `${PIPESTATUS[0]}` is what says so.

    `snapshot.sh` exits 1 for an archive that is SHORT of a source. Recording the fingerprint of a
    run it could not archive means the timer sits quiet until something ELSE changes -- so the one
    measurement that failed to be archived is the one nobody retries. And the status has to come
    from `${PIPESTATUS[0]}`: the call is piped into `sed` to indent it, so `$?` is sed's, which is
    0 every time.
    """
    binroot, root = _loop_box(tmp_path)
    proc, calls = _start_loop(binroot, root, tmp_path, rc="1")
    try:
        assert _wait_for_calls(calls, 3), (
            f"a failing snapshot was retried {_calls(calls)} time(s) on an unchanged box; the "
            "fingerprint of an archive that was never written has been recorded as done")
    finally:
        out = _stop_loop(proc)
    assert "NOT recording this fingerprint" in out, out

    # ...and success still closes it, or the retry becomes the 3.0 GB loop in the other direction.
    proc2, calls2 = _start_loop(binroot, root, tmp_path, rc="0", tag="calls-after")
    try:
        assert _wait_for_calls(calls2, 1)
        time.sleep(3.5)
        assert _calls(calls2) == 1, "a successful snapshot did not close the retry"
    finally:
        _stop_loop(proc2)
