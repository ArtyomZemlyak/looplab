"""`extract_champion.py` separates "this run has no champion" from "this harness is broken", and both callers threw the distinction away.

THE EXTRACTOR'S OWN RULE, from its source:

    1 - the FOLD says there is no champion: no event log, no best node, no `solver.py` in the
        committed working set. A fact about the RUN, and a legitimate null.
    2 - the log could not be READ, or `looplab` could not be IMPORTED. A fact about THIS HARNESS,
        which says nothing whatever about the run: the scores are still in `events.jsonl` and the
        champion can be re-extracted without spending the budget again.

It was rewritten for exactly that separation, after the `accEE` probe reported "champion: NONE"
while its own event log held 27.466 and 221.5387. And then `run_probe.sh` and `campaign.sh` both
wrote `if python extract_champion.py …; then … else … fi`, where 1 and 2 are one branch — so the
distinction died at both ends and a broken bridge was recorded, again, as a run that found nothing.

Reproduced 2026-08-31 against the real script: a run directory whose `events.jsonl` cannot be read
exits 2, one with a log and no evaluated node exits 1, and before this fix both callers emitted the
same "no champion" verdict.

Driven by extracting each caller's real branch and RUNNING it over both shapes, with the real
extractor in the middle.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT / "benchmarks" / "algotune" / "campaign.sh"
PROBE = ROOT / "benchmarks" / "algotune" / "run_probe.sh"
EXTRACT = ROOT / "benchmarks" / "algotune" / "extract_champion.py"

_RUN_STARTED = {"v": 1, "seq": 0, "ts": 1.0, "type": "run_started",
                "data": {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"}}


def _run_dir(parent: Path, shape: str) -> Path:
    """`broken` = a log that cannot be read; `nochamp` = a readable log with no champion."""
    run = parent / "run"
    run.mkdir(parents=True)
    if shape == "broken":
        # A DIRECTORY where the log should be: `IsADirectoryError` is an `OSError`, which is the
        # class the extractor calls a broken bridge. No permission games, so this behaves the same
        # for every uid the suite might run as.
        (run / "events.jsonl").mkdir()
    else:
        (run / "events.jsonl").write_text(json.dumps(_RUN_STARTED) + "\n", encoding="utf-8")
    return run


def test_the_extractor_really_does_separate_them(tmp_path):
    """The premise, re-derived rather than assumed: if these ever collapse, the callers below are
    pinning a distinction that no longer exists."""
    codes = {}
    for shape in ("broken", "nochamp"):
        run = _run_dir(tmp_path / shape, shape)
        got = subprocess.run(
            [sys.executable, str(EXTRACT), "--run-dir", str(run), "--all-files",
             "--out", str(tmp_path / shape / "champion" / "solver.py")],
            capture_output=True, text=True, timeout=120)
        codes[shape] = got.returncode
    assert codes == {"broken": 2, "nochamp": 1}, codes


# ------------------------------------------------------------------------------------------------
# campaign.sh
# ------------------------------------------------------------------------------------------------
def _campaign_branch() -> str:
    """`run_one`'s champion branch, from `if [ "$RC" = 2 ]` to just before `record_done`."""
    lines = CAMPAIGN.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith('    if [ "$RC" = 2 ] && ! run_started_evidence')), None)
    assert start is not None, "campaign.sh no longer branches on the champion extraction"
    end = next(i for i, ln in enumerate(lines[start:], start)
               if ln.startswith('    record_done "$MARKER" "$RC" "$S" "$CPUS" "$TASK_ROOT/run"'))
    return "\n".join(lines[start:end])


def _campaign_final(tmp: Path, shape: str) -> tuple[dict, str]:
    out = tmp / "out"
    out.mkdir(parents=True)
    task_root = tmp / "task"
    _run_dir(task_root, shape)
    # `RC=0` -- the LoopLab run itself ended normally, so the first arm is not taken and the
    # extraction is what decides this row.
    script = (f'set -u\nRC=0\nREPO="{ROOT}"\nAT="{tmp}/at"\nWS="{tmp}/ws"\nT=demo\nCPUS=0\n'
              f'OUT="{out}"\nTASK_ROOT="{task_root}"\nCHAMPION_TIMEOUT=60\n'
              + _campaign_branch() + "\n")
    got = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=180,
                         env=dict(os.environ, PYTHONPATH=str(ROOT)))
    assert got.returncode == 0, got.stdout + got.stderr
    final = out / "B-demo.final.json"
    assert final.exists(), (got.stdout, got.stderr)
    return json.loads(final.read_text(encoding="utf-8")), got.stdout


def test_the_campaign_records_a_broken_bridge_as_a_harness_failure(tmp_path):
    row, said = _campaign_final(tmp_path, "broken")
    assert row["speedup"] is None, row
    assert row.get("harness_failure") == "extract_champion_rc2", row
    assert "no champion to score" not in json.dumps(row), (
        "a broken bridge is still filed as a run that found nothing", row)
    assert "BROKEN BRIDGE" in said, said
    assert "extract_champion.py" in said, "it does not say how to recover without re-running"


def test_the_campaign_still_calls_a_real_empty_run_empty(tmp_path):
    """The falsifier for a fix that turns every null into an alarm."""
    row, said = _campaign_final(tmp_path, "nochamp")
    assert row == {"speedup": None, "error": "no champion to score"}, row
    assert "BROKEN BRIDGE" not in said, said


# ------------------------------------------------------------------------------------------------
# run_probe.sh
# ------------------------------------------------------------------------------------------------
def _probe_champion(tmp: Path, shape: str) -> subprocess.CompletedProcess:
    src = PROBE.read_text(encoding="utf-8")
    start = src.find("# ЧЕМПИОНА ВЫБИРАЕТ СВЁРТКА СОБЫТИЙ")
    assert start != -1, "run_probe.sh no longer carries its champion block"
    end = src.find('\nsay "чемпион:', start)
    assert end != -1, "run_probe.sh no longer says which champion it picked"
    stand = tmp / "stand"
    stand.mkdir(parents=True)
    (stand / "looplab").symlink_to(ROOT)
    out = tmp / "out"
    _run_dir(out / "runs" / "demo", shape)
    script = (f'set -u\nROOT="{stand}"\nOUT="{out}"\nTASK=demo\nLOG="{out}/probe.log"\n'
              'say() { echo "$*"; }\n' + src[start:end] + '\nsay "чемпион: ${CH:-НЕТ}"\n')
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=180,
                          env=dict(os.environ, PYTHONPATH=str(ROOT)))


def test_the_probe_refuses_to_call_a_broken_bridge_an_empty_run(tmp_path):
    got = _probe_champion(tmp_path, "broken")
    assert "СЛОМАН МОСТ" in got.stdout, got.stdout + got.stderr
    assert "чемпион: НЕТ" not in got.stdout, (
        "the probe still reports the accEE sentence over a harness fault", got.stdout)
    assert got.returncode != 0, "no number was produced and the probe exits as if one was"
    assert "events.jsonl" in got.stdout, "it does not say that the scores survived"


def test_the_probe_still_reports_a_genuinely_empty_run(tmp_path):
    got = _probe_champion(tmp_path, "nochamp")
    assert got.returncode == 0, got.stdout + got.stderr
    assert "чемпион: НЕТ" in got.stdout, got.stdout
    assert "СЛОМАН МОСТ" not in got.stdout, got.stdout
