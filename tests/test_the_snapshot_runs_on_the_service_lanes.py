"""The one service process on this box that was not pinned.

Lanes 44-47,92-95 are reserved so service work has cpus of its own while probes hold 0-43,48-91.
`snapshot_timer.sh` ran `snapshot.sh` UNPINNED, which is invisible while the bench is only waiting
on an LLM and is not invisible when it is computing.

Measured 2026-09-04 with §208's per-part breakdown in place: `20260904-135436` took **976 s —
391 s prefix-check + 300 s cp -ru + 285 s repair** — against **118 s (79+7+32)** for the tick half
an hour later on the same 1.2 G archive. All three parts inflated together, which is contention and
not any one step, and the window is exactly when AlgoTune evaluations were saturating lanes 0-32 for
§214's ruler self-check. The two earlier outliers, 1765 s and 608 s, sit over that morning's pytest
and mutation runs.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "benchmarks"
BODY = (HERE / "snapshot_timer.sh").read_text(encoding="utf-8")


def test_the_loop_pins_the_snapshot():
    call = re.search(r'^\s*(\S.*?)"\$HERE/snapshot\.sh"', BODY, re.M)
    assert call, "the loop no longer invokes $HERE/snapshot.sh"
    assert "taskset -c" in call.group(1), (
        f"snapshot.sh is invoked as `{call.group(1).strip()}snapshot.sh` -- unpinned, so it "
        "competes with every pinned lane instead of using the reserved ones")


def test_the_default_is_the_reserved_service_lanes():
    got = re.search(r'SERVICE_LANE="\$\{SNAPSHOT_SERVICE_LANE:-([^}]*)\}"', BODY)
    assert got, "SERVICE_LANE is not overridable, or is not read from SNAPSHOT_SERVICE_LANE"
    assert got.group(1) == "44-47,92-95", (
        f"the default service lane is {got.group(1)}, not the reserved 44-47,92-95 of sweep point 5")


def test_the_snapshot_really_lands_on_those_cpus(tmp_path):
    """Not just that the word `taskset` is in the file: run the loop against a stub `snapshot.sh`
    that records its own affinity."""
    bench = tmp_path / "bench"
    (bench / "logs").mkdir(parents=True)
    (bench / "meter").mkdir()                      # a tree the fingerprint watches, so ticks happen
    run = tmp_path / "run"
    run.mkdir()
    shutil.copy(HERE / "snapshot_timer.sh", run / "snapshot_timer.sh")
    shutil.copy(HERE / "bench_trees.sh", run / "bench_trees.sh")
    (run / "snapshot.sh").write_text(
        "#!/bin/bash\n"
        f"python3 -c \"import os;print(sorted(os.sched_getaffinity(0)))\" >> {bench}/affinity\n"
        f"date +%s.%N > {bench}/meter/moved-$$\n"
        "exit 0\n", encoding="utf-8")
    os.chmod(run / "snapshot.sh", 0o755)
    env = dict(os.environ, BENCH_ROOT=str(bench), SNAPSHOT_DEST=str(tmp_path / "dest"),
               SNAPSHOT_SERVICE_LANE="2,5")
    proc = subprocess.Popen(["bash", str(run / "snapshot_timer.sh"), "_loop", "2"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        time.sleep(6)
    finally:
        proc.kill()
        proc.wait(timeout=10)
    seen = (bench / "affinity").read_text(encoding="utf-8").strip().splitlines()
    assert seen, "the stub snapshot never ran"
    assert all(line.strip() == "[2, 5]" for line in seen), (
        f"the snapshot ran on {seen}, not on the lane it was given")
