"""A task-arm that exits 0 in seconds is not one that completed, and the record has to say so.

THE FAILURE. Measured on the 2026-08-24 campaign (docs/58 §58.1): 16 of the 20 arm-A markers in
`logs/final-A-wauto.log` and `final-A-w1.log` record `wall` between 3 and 19 seconds with
`rc=0 state=ran_to_completion attempt=a1` — the floor is `queens_with_obstacles wall=3`, the ceiling
`pagerank wall=19` — while the four task-arms that really ran took 2,063–2,179 s. The gateway's model
group had gone to `503 No available workers`. A task-arm cannot complete in five seconds, and the
marker vocabulary had no way to say "exited immediately", so the campaign's own record reported
sixteen instant successes, `final_banner` printed COMPLETE over them, and every later reader that
paired a row off `agent_summary.json` averaged whatever an earlier campaign had left there.

WHAT CHANGED. `campaign.sh::record_done`'s rc=0 arm writes `state=exited_immediately` (with the wall
and the threshold it was judged against) below `IMMEDIATE_EXIT_S` seconds; `marker_is_immediate_exit`
is the shell predicate, mirrored by `compare_arms.py::IMMEDIATE_EXIT_STATE`, and both readers treat
it exactly like a harness cut — terminal, printed, never averaged — through the ONE tuple
`compare_arms.py::NOT_AT_BUDGET_STATES`. It is reopened by its own flag, `RETRY_IMMEDIATE_EXIT=1`,
and NOT by `RETRY_WALL_CUT`: an outage is worth re-running once repaired, a wall cut may not be.

HOW THIS IS TESTED. The shell functions are EXTRACTED from `campaign.sh` and RUN over real marker
directories, the way `test_campaign_marker_evidence.py` and
`test_algotune_operator_skip_is_not_a_finish.py` already do; `compare_arms.py` is driven as an
operator would drive it. Pinning the `case` arm's text would be satisfiable by a comment.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

CAMPAIGN = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "campaign.sh"
COMPARE = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "compare_arms.py"
STATUS = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "campaign_status.py"

_FUNCTIONS = ("run_started_evidence", "successful_calls", "ruler_fields", "marker_is_harness_cut",
              "marker_is_operator_skip", "marker_is_immediate_exit", "already_measured",
              "record_done", "refuse_to_start", "final_banner")

DONE = "wall=2100 rc=0 state=ran_to_completion ok_calls=40 attempt=a1\n"
IMMEDIATE = ("wall=5 rc=0 state=exited_immediately threshold_s=60 cpus=0-21 lanes=4 "
             "cores_per_lane=22 layout=whole_cores ok_calls= attempt=a1\n")


def _harness() -> str:
    """The functions verbatim, plus the preamble variables `record_done` reads.

    `IMMEDIATE_EXIT_S` is set in the campaign's preamble (beside HARD_TIMEOUT), which the extraction
    leaves behind, so the stub mirrors the script's own default — the same treatment
    `test_campaign_marker_evidence.py` gives LANE_LAYOUT. A test that wants another threshold
    exports it before calling, and the `${...:-60}` form lets it.
    """
    src = CAMPAIGN.read_text(encoding="utf-8")
    # `HERE` is the script's own directory, which `ruler_fields` (called by `record_done` for the
    # marker's ruler identity) resolves `looplab_eval.py` against; the preamble sets it from `$0`.
    parts = ["set -u", "LANE_COUNT=4", "CORES_PER_LANE=22", 'LANE_LAYOUT="whole_cores"',
             'IMMEDIATE_EXIT_S="${IMMEDIATE_EXIT_S:-60}"', f'HERE="{CAMPAIGN.parent}"',
             'ARM="${ARM:-B}"', 'T="${T:-svm}"']
    for name in _FUNCTIONS:
        found = re.search(rf"^{name}\(\) \{{.*?^\}}$", src, re.M | re.S)
        assert found, f"campaign.sh no longer defines {name}()"
        assert len(found.group(0).splitlines()) > 2, f"{name}() extracted as an empty body"
        parts.append(found.group(0))
    return "\n".join(parts) + "\n"


def _bash(script: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", _harness() + script], cwd=str(cwd),
                          capture_output=True, text=True, timeout=60)


def _started_run(root: Path) -> Path:
    run = root / "run"
    run.mkdir(parents=True)
    (run / "engine.lock").write_text("")
    rows = ['{"v":1,"seq":0,"ts":1.0,"type":"run_started","data":{}}']
    (run / "events.jsonl").write_text("\n".join(rows) + "\n")
    return run


def test_the_default_threshold_is_the_one_the_script_declares():
    """The stub above mirrors the script's default; if the script moves, the stub must follow, or
    every test here runs against a threshold the campaign does not use."""
    src = CAMPAIGN.read_text(encoding="utf-8")
    assert re.search(r'^IMMEDIATE_EXIT_S="\$\{IMMEDIATE_EXIT_S:-60\}"$', src, re.M), (
        "campaign.sh no longer declares IMMEDIATE_EXIT_S with a default of 60; re-point the stub")


# ---------------------------------------------------------------------------------------------
# The marker itself
# ---------------------------------------------------------------------------------------------

def test_an_rc0_exit_in_seconds_is_not_recorded_as_a_completion(tmp_path):
    """The defect. A start epoch of NOW makes the wall a few seconds, which is the shape of all
    sixteen 2026-08-24 markers."""
    run = _started_run(tmp_path)
    done = tmp_path / "A-pagerank.done"
    got = _bash(f'record_done "{done}" 0 "$(date +%s)" "0-21" "{run}"', tmp_path)
    assert got.returncode == 0, got.stderr
    marker = done.read_text()
    assert "state=exited_immediately" in marker, marker
    assert "state=ran_to_completion" not in marker, marker
    assert "rc=0" in marker and re.search(r"\bwall=\d+\b", marker), marker
    assert "threshold_s=60" in marker, "the marker does not carry the bar it was judged against"
    assert "lanes=4 cores_per_lane=22" in marker, "the regime is still stamped"
    assert "EXITED IMMEDIATELY" in got.stdout, got.stdout


def test_an_rc0_exit_after_the_threshold_is_still_a_completion(tmp_path):
    """The control, so the rung cannot be satisfied by refusing every completion: a start epoch of
    0 makes the wall the whole Unix era, and that is `ran_to_completion` exactly as before."""
    run = _started_run(tmp_path)
    done = tmp_path / "B-svm.done"
    got = _bash(f'record_done "{done}" 0 0 "0-21" "{run}"', tmp_path)
    assert got.returncode == 0, got.stderr
    assert "state=ran_to_completion" in done.read_text(), done.read_text()
    assert "exited_immediately" not in done.read_text()


def test_the_threshold_is_the_operators_to_move(tmp_path):
    """`IMMEDIATE_EXIT_S` is an environment knob like HARD_TIMEOUT; a 5 s wall under a 3 s bar is a
    completion, and the marker records the bar it was judged against either way."""
    run = _started_run(tmp_path)
    done = tmp_path / "B-svm.done"
    got = _bash(f'IMMEDIATE_EXIT_S=1; record_done "{done}" 0 "$(( $(date +%s) - 5 ))" "0-21" "{run}"',
                tmp_path)
    assert got.returncode == 0, got.stderr
    assert "state=ran_to_completion" in done.read_text(), done.read_text()


def test_a_run_the_meter_proves_bought_nothing_still_gets_no_marker_at_all(tmp_path):
    """Precedence. The meter rung (no marker, task still owed) stays ABOVE the clock rung: a run the
    ledger proves paid for nothing is not even an immediate exit, it is nothing."""
    run = _started_run(tmp_path)
    done = tmp_path / "B-svm.done"
    import time
    log = tmp_path / "meter.jsonl"
    # `ts` INSIDE the attempt window: `successful_calls` windows on the start epoch and answers ""
    # (unknowable, marker kept) over a log with no clock at all -- the 2026-08-25 rung's own rule.
    log.write_text(json.dumps({"arm": "B", "task": "svm", "attempt": "a1", "status": 503,
                               "ts": time.time() + 1}) + "\n")
    got = _bash(f'METER_LOG="{log}"; ATTEMPT=a1; record_done "{done}" 0 "$(date +%s)" "0-21" "{run}"',
                tmp_path)
    assert not done.exists(), done.read_text()
    assert "NO SUCCESSFUL CALLS" in got.stdout, got.stdout


# ---------------------------------------------------------------------------------------------
# Is it resumable? Terminal by default, reopened by ITS OWN flag.
# ---------------------------------------------------------------------------------------------

def test_an_immediate_exit_is_terminal_by_default(tmp_path):
    done = tmp_path / "B-svm.done"
    done.write_text(IMMEDIATE)
    assert _bash(f'already_measured "{done}"', tmp_path).returncode == 0


def test_retry_immediate_exit_reopens_it_and_retry_wall_cut_does_not(tmp_path):
    """Two flags, two classes. RETRY_WALL_CUT is the argument about a clock that may bind again;
    an immediate exit is an environment condition, and the flag that reopens it says so by name."""
    done = tmp_path / "B-svm.done"
    done.write_text(IMMEDIATE)
    assert _bash(f'RETRY_IMMEDIATE_EXIT=1; already_measured "{done}"', tmp_path).returncode == 1
    assert _bash(f'RETRY_WALL_CUT=1; already_measured "{done}"', tmp_path).returncode == 0
    # ...and the immediate-exit flag does not reopen a wall cut or a skip in passing.
    wall = tmp_path / "B-wall.done"
    wall.write_text("wall=14400 rc=124 state=wall_cut attempt=a1\n")
    assert _bash(f'RETRY_IMMEDIATE_EXIT=1; already_measured "{wall}"', tmp_path).returncode == 0
    skip = tmp_path / "B-skip.done"
    skip.write_text("wall=0 rc=0 state=operator_skip attempt=a1\n")
    assert _bash(f'RETRY_IMMEDIATE_EXIT=1; already_measured "{skip}"', tmp_path).returncode == 0


# ---------------------------------------------------------------------------------------------
# What the campaign SAYS at the end
# ---------------------------------------------------------------------------------------------

def test_the_banner_does_not_count_an_immediate_exit_among_the_measured(tmp_path):
    """The falsifier for `FINAL CAMPAIGN COMPLETE` over sixteen instant exits."""
    out = tmp_path / "camp"
    out.mkdir()
    (out / "A-alpha.done").write_text(DONE)
    (out / "A-beta.done").write_text(IMMEDIATE)
    (out / "A-gamma.done").write_text(IMMEDIATE.replace("wall=5", "wall=19"))
    got = _bash(f'final_banner "{out}" A 3 "alpha beta gamma"', tmp_path)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "EXITED IMMEDIATELY" in got.stdout, got.stdout
    block = got.stdout.split("EXITED IMMEDIATELY", 1)[1].split("=====")[0]
    assert "A-beta" in block and "A-gamma" in block and "A-alpha" not in block, block
    assert "wall=19" in block, "the banner does not show the walls, which are the whole evidence"
    assert "1 MEASURED" in got.stdout, got.stdout
    assert "2 EXITED IMMEDIATELY" in got.stdout, got.stdout
    assert "COMPLETE (3/3 markers)" not in got.stdout, got.stdout
    assert "RETRY_IMMEDIATE_EXIT=1" in got.stdout


def test_the_banner_is_unchanged_when_nothing_exited_immediately(tmp_path):
    """The control: the clean banner's exact wording is what a watcher greps for."""
    out = tmp_path / "camp"
    out.mkdir()
    (out / "B-alpha.done").write_text(DONE)
    got = _bash(f'final_banner "{out}" B 1 "alpha"', tmp_path)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "EXITED IMMEDIATELY" not in got.stdout
    assert "arm B COMPLETE (1/1 markers)" in got.stdout, got.stdout


# ---------------------------------------------------------------------------------------------
# compare_arms.py and campaign_status.py: printed, never averaged
# ---------------------------------------------------------------------------------------------

def _by_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CA = _by_path(COMPARE, "compare_arms_immediate_exit_under_test")


def test_the_state_is_one_vocabulary_in_both_languages():
    """The shell predicate and the Python constant must name the same string, or a marker the
    driver writes is one the reader files as `done`."""
    src = CAMPAIGN.read_text(encoding="utf-8")
    assert f"*state={CA.IMMEDIATE_EXIT_STATE}*" in src
    assert f"state={CA.IMMEDIATE_EXIT_STATE} threshold_s=" in src
    assert CA.IMMEDIATE_EXIT_STATE in CA.NOT_AT_BUDGET_STATES
    assert CA.IMMEDIATE_EXIT_STATE not in CA.HARNESS_CUT_STATES, (
        "an immediate exit is not a harness cut: RETRY_WALL_CUT must not reopen it")
    assert set(CA.HARNESS_CUT_STATES) < set(CA.NOT_AT_BUDGET_STATES)


def test_marker_state_reads_it(tmp_path):
    (tmp_path / "A-beta.done").write_text(IMMEDIATE)
    assert CA.marker_state(tmp_path, "A", "beta") == CA.IMMEDIATE_EXIT_STATE


def _campaign(tmp: Path, a_marker: str) -> Path:
    root = tmp / "bench"
    (root / "AlgoTune" / "reports").mkdir(parents=True)
    # A number an EARLIER campaign left in the merge target, which is what got averaged in 08-24.
    (root / "AlgoTune" / "reports" / "agent_summary.json").write_text(
        json.dumps({"demo": {"gateway/deepseek-v4-flash": {"final_speedup": 2.5}}}))
    (root / "runs-B" / "demo" / "run").mkdir(parents=True)
    (root / "runs-B" / "demo" / "run" / "events.jsonl").write_text("")
    final = root / "campaign-final"
    final.mkdir()
    (final / "B-demo.final.json").write_text(json.dumps({"speedup": 1.5, "subset": "test"}))
    (final / "B-demo.done").write_text(DONE)
    (final / "A-demo.done").write_text(a_marker)
    return root


def _compare(root: Path) -> str:
    out = subprocess.run(
        [sys.executable, str(COMPARE), "--algotune-root", str(root / "AlgoTune"),
         "--runs-root", str(root / "runs-B"), "--final-dir", str(root / "campaign-final")],
        capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_compare_arms_prints_the_row_and_refuses_to_average_it(tmp_path):
    out = _compare(_campaign(tmp_path, IMMEDIATE))
    row = next(line for line in out.splitlines() if line.strip().startswith("demo"))
    assert "2.5000" in row, f"the number is hidden rather than shown: {row!r}"
    assert "EXITED IMMEDIATELY" in row, row
    assert "mean over 1 complete pair" not in out, out
    assert "EXITED IMMEDIATELY (rc=0 within seconds" in out, "the footer does not count them"


def test_campaign_status_keeps_it_out_of_the_median(tmp_path):
    root = _campaign(tmp_path, IMMEDIATE)
    out = subprocess.run(
        [sys.executable, str(STATUS), "--out", str(root / "campaign-final"),
         "--runs-root", str(root / "runs-B"), "--algotune-root", str(root / "AlgoTune"),
         "--arm", "A"],
        capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert CA.IMMEDIATE_EXIT_STATE in out.stdout, out.stdout
    assert "EXITED IMMEDIATELY" in out.stdout, out.stdout
    assert "scored: median" not in out.stdout, "an immediate exit reached the median"
