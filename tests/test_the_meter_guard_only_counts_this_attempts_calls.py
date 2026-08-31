"""A run that bought nothing must not inherit an earlier session's paid calls.

THE RUNG. `campaign.sh::record_done`'s rc=0 arm refuses to write a marker for a run that made no
successful call — the 2026-08-25 defect, where the gateway's model group went to `503 No available
workers` and sixteen arm-A task-arms each exited 0 in three to nineteen seconds having bought
nothing, every one of them marked `ran_to_completion` under a "FINAL CAMPAIGN COMPLETE" banner.

THE HOLE. It weighs that evidence by `(arm, task, attempt)` and nothing else, and that triple does
not name one run:

  * a row whose `attempt` key is absent or empty matches EVERY attempt. Deliberately — rows written
    before 2026-08-23 carry no such key, and the two-segment `/m/<arm>/<task>/v1` path is still
    accepted — but it means every legacy row is credited to whatever runs next.
  * the attempt ledger lives in `$OUT` and the meter log does not, so a fresh `CAMPAIGN_OUT`
    restarts numbering at `a1` over a log that already holds an `a1`.

Reproduced 2026-08-30 by driving the real functions over a meter log holding two three-day-old
untagged 200s: a run that made NO calls was told `ok_calls=2` and `ended_on_failure=no`, and earned
`wall=7 rc=0 state=ran_to_completion ok_calls=2 attempt=a1`.

THE WINDOW. `record_done` already knows when the attempt started, and `meter/proxy.py` stamps `ts`
when it WRITES the row — after the call returned — so every row belonging to this attempt has
`ts >= start`. That is a relation between two clocks on one box, not a guess about session
boundaries, and it closes both holes at once.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

CAMPAIGN = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "campaign.sh"

_FUNCTIONS = ("run_started_evidence", "successful_calls", "ended_on_failure", "next_attempt",
              "marker_is_harness_cut", "marker_is_operator_skip", "already_measured",
              "record_done", "refuse_to_start")


def _bash(script: str, cwd: Path) -> subprocess.CompletedProcess:
    src = CAMPAIGN.read_text(encoding="utf-8")
    parts = ["set -u", "LANE_COUNT=4", "CORES_PER_LANE=22", 'LANE_LAYOUT="whole_cores"',
             'ARM="${ARM:-B}"', 'T="${T:-svm}"']
    for name in _FUNCTIONS:
        found = re.search(rf"^{name}\(\) \{{.*?^\}}$", src, re.M | re.S)
        assert found, f"campaign.sh no longer defines {name}()"
        assert len(found.group(0).splitlines()) > 2, f"{name}() extracted as an empty body"
        parts.append(found.group(0))
    return subprocess.run(["bash", "-c", "\n".join(parts) + "\n" + script], cwd=str(cwd),
                          capture_output=True, text=True, timeout=120)


def _log(tmp: Path, rows: list[dict]) -> Path:
    """A meter log in the shape `benchmarks/meter/proxy.py` writes it."""
    path = tmp / "meter.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _stale_rows(now: float) -> list[dict]:
    """Two paid calls from a session three days ago, carrying no `attempt` key at all."""
    return [{"ts": now - 3 * 86400, "arm": "B", "task": "svm", "status": 200, "cost": 0.5},
            {"ts": now - 3 * 86400 + 1, "arm": "B", "task": "svm", "status": 200, "cost": 0.5}]


def _marker(tmp: Path, rows: list[dict], *, started: int) -> tuple[str | None, str]:
    log = _log(tmp, rows)
    done = tmp / "B-svm.done"
    got = _bash(f'METER_LOG="{log}"; OUT="{tmp}"; ATTEMPT=a1; '
                f'record_done "{done}" 0 {started} "0-21" ""', tmp)
    assert got.returncode == 0, got.stdout + got.stderr
    return (done.read_text(encoding="utf-8") if done.exists() else None), got.stdout


def test_an_earlier_sessions_calls_are_not_this_attempts_evidence(tmp_path):
    """The falsifier. A run that paid for nothing, over a log that remembers a run that did."""
    now = time.time()
    marker, said = _marker(tmp_path, _stale_rows(now), started=int(now) - 7)
    assert marker is None, f"a marker was written over a run that bought nothing: {marker!r}"
    assert "NO SUCCESSFUL CALLS" in said, said


def test_this_attempts_own_call_still_writes_the_marker(tmp_path):
    """The other half, so the window cannot be satisfied by refusing everything: one row inside it
    is positive evidence, and the stale pair beside it is not counted."""
    now = time.time()
    rows = _stale_rows(now) + [{"ts": now - 3, "arm": "B", "task": "svm", "attempt": "a1",
                                "status": 200, "cost": 0.4}]
    marker, said = _marker(tmp_path, rows, started=int(now) - 7)
    assert marker is not None, said
    assert "ok_calls=1" in marker, f"the stale pair is still being counted: {marker!r}"
    assert "state=ran_to_completion" in marker, marker


def test_a_cut_run_is_still_read_off_its_own_last_call(tmp_path):
    """`ended_on_failure` gets the same window, and must still see the failure inside it. The stale
    200s are LATER in the file than the 503, so without a window the last row is a success."""
    now = time.time()
    rows = [{"ts": now - 4, "arm": "B", "task": "svm", "attempt": "a1", "status": 200},
            {"ts": now - 3, "arm": "B", "task": "svm", "attempt": "a1", "status": 503,
             "error": "No available workers"}]
    marker, said = _marker(tmp_path, rows, started=int(now) - 7)
    assert marker is None, f"a cut run earned a marker: {marker!r}"
    assert "ENDED ON A FAILED CALL" in said, said


def test_a_log_with_no_clock_at_all_stays_unknowable(tmp_path):
    """"" and "0" are DIFFERENT answers and only "0" refuses. A log written before `ts` existed
    cannot be windowed, and punishing a run for that bookkeeping gap is the failure this whole
    check is careful not to commit."""
    now = time.time()
    marker, said = _marker(tmp_path, [{"arm": "B", "task": "svm", "status": 200}],
                           started=int(now) - 7)
    assert marker is not None, (
        "an unwindowable log was read as evidence of no calls", said)
    assert "ok_calls=" in marker and "ok_calls=0" not in marker, marker
    assert "NO SUCCESSFUL CALLS" not in said, said


def test_without_a_start_epoch_the_old_behaviour_stands(tmp_path):
    """The window is OPT-IN on the argument, so a caller that has no start time (a test driving
    `record_done` directly, a marker written by hand) is answered exactly as before."""
    now = time.time()
    got = _bash(f'METER_LOG="{_log(tmp_path, _stale_rows(now))}"; '
                f'successful_calls B svm a1 0', tmp_path)
    assert got.stdout.strip() == "2", got.stdout + got.stderr
