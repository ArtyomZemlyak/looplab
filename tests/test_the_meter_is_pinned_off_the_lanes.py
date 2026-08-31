"""The meter must not land on a lane, and the launcher must be the thing that knows it.

On 2026-08-29 the meter WAS pinned off the lanes -- `run_final-relaunch.log` records
"meter restarted ... pinned off the lanes" -- but the pinning lived in `run_final.sh`, a driver that
was never committed. It went with `/var/tmp` when the container restarted, and when the meter was
brought back up on 2026-08-31 by `start_meter.sh` alone it came back on `0-95`. Measured at 0.0 %
CPU, so nothing was actually spoiled; it was one busy proxy away from putting its own CPU into a
lane's timings, and nothing in the tree would have said so.

The meter is infrastructure every lane talks to. A lane it shares is a lane whose measurement
includes a proxy. So the fact belongs in the box profile and its application in the launcher, where
both are committed, and this test asserts the real process affinity rather than the shape of the
command line.
"""
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

START = Path(__file__).resolve().parents[1] / "benchmarks" / "meter" / "start_meter.sh"
PORT = "8899"          # not 8801: start_meter kills by port, and a live campaign meter must survive


def _affinity(pid: int) -> str:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("Cpus_allowed_list:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("no Cpus_allowed_list")


def _pids():
    out = subprocess.run(["pgrep", "-f", f"meter/proxy.py --port {PORT}"],
                         capture_output=True, text=True)
    return [int(p) for p in out.stdout.split()]


def _stop():
    for p in _pids():
        try: os.kill(p, signal.SIGTERM)
        except ProcessLookupError: pass
    time.sleep(0.5)


@pytest.fixture
def meter(tmp_path):
    _stop()
    yield
    _stop()


def _start(tmp_path, cpus):
    env = {**os.environ, "METER_PORT": PORT, "METER_UPSTREAM": "http://127.0.0.1:1",
           "METER_API_KEY": "x", "METER_LOG": str(tmp_path / "m.jsonl"),
           "METER_STDOUT": str(tmp_path / "m.log")}
    if cpus is None:
        env.pop("METER_CPUS", None)
    else:
        env["METER_CPUS"] = cpus
    return subprocess.run(["bash", str(START)], env=env, capture_output=True, text=True, timeout=120)


def test_the_meter_lands_on_the_cores_the_box_profile_named(tmp_path, meter):
    result = _start(tmp_path, "44-45")
    pids = _pids()
    assert pids, result.stdout + result.stderr
    assert _affinity(pids[0]) == "44-45", (
        "the meter is on cores the box profile did not give it; on this box that means a lane\n"
        + result.stdout)
    assert "pinned to 44-45" in result.stdout


def test_an_unpinned_meter_says_so_instead_of_going_quietly(tmp_path, meter):
    """The 2026-08-31 shape exactly: it came up on 0-95 and said nothing, so nobody looked."""
    result = _start(tmp_path, None)
    assert "UNPINNED" in result.stderr, result.stdout + result.stderr
