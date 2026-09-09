"""Two campaign.sh repairs, driven as shell over real directories.

1. THE STALL CLOCK WATCHES THE TREE, NOT ONE FILE (closed 2026-09-08). `run_bounded`'s guard read
   `stat -c %Y` over the arm-B watch path, which was `events.jsonl`. An evaluation appends NOTHING
   to the event log while it runs — stage events land at stage END — so a healthy lane inside a long
   `score` stage looked identical to a wedged one: the per-instance timeout is max(10x baseline,
   floor), a valid slow solver runs ~50 min over 100 instances against `STALL_TIMEOUT=2400`, and the
   README records an 87-minute evaluation. The lane was killed, `record_done` filed `state=stall_cut`
   — terminal, never averaged, and `already_measured` never re-runs it. The champion pass was
   exempted from the guard for exactly this reason; the in-run evaluations were not.

   `newest_activity` now answers with the newest mtime anywhere under the run directory. The
   file/directory distinction stays the CALLER's: arm A's watch is its own lane log, and its parent
   holds every other lane's log, so scanning that directory would let one busy lane mask another's
   silence. That is the second property below.

2. A REFUSED CHAMPION PASS CAN BE RE-SCORED (closed 2026-09-08). `record_done` wrote its marker
   whatever the graded pass said, so a task-arm whose champion pass was refused with
   `baseline_measured_in_pass` — the arena timed the REFERENCE in that pass, so the candidate was
   never timed — was terminal with no number. It is the one refusal a second pass clears, because
   that same pass CACHED the timings. `RETRY_REFUSED=1` re-runs the SCORING PASS only: the champion
   is already on disk and no model is called. It deliberately does not go through
   `already_measured`, whose flags re-run the whole task-arm — and `run_one` starts by deleting the
   task root, which is where that champion lives.

HOW THIS IS TESTED. The functions are EXTRACTED from `campaign.sh` and run under `bash`, the way
`test_algotune_immediate_exit_is_not_a_finish.py` and `test_campaign_marker_evidence.py` already do.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

CAMPAIGN = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "campaign.sh"

_FUNCTIONS = ("newest_activity", "champion_refusal", "marker_says_champion_refused",
              "score_champion", "rescore_refused_champion")


def _harness(**preamble: str) -> str:
    """The functions verbatim, plus the globals they read, so nothing is re-implemented here."""
    src = CAMPAIGN.read_text(encoding="utf-8")
    parts = ["set -u"]
    # The one CONSTANT the extraction leaves behind, mirrored from the script's own declaration —
    # `champion_refusal` reads it, and a test that invented its own value would prove nothing about
    # which reasons the campaign reopens.
    found = re.search(r'^RESCORABLE_NO_SPEEDUP_REASONS="([^"]*)"$', src, re.M)
    assert found, "campaign.sh no longer declares RESCORABLE_NO_SPEEDUP_REASONS"
    parts.append(found.group(0))
    parts.append('RETRY_REFUSED="${RETRY_REFUSED:-0}"')
    for key, value in preamble.items():
        parts.append(f'{key}="{value}"')
    for name in _FUNCTIONS:
        body = re.search(rf"^{name}\(\) \{{.*?^\}}$", src, re.M | re.S)
        assert body, f"campaign.sh no longer defines {name}()"
        assert len(body.group(0).splitlines()) > 2, f"{name}() extracted as an empty body"
        parts.append(body.group(0))
    return "\n".join(parts) + "\n"


def _bash(script: str, cwd: Path, **preamble: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", _harness(**preamble) + script], cwd=str(cwd),
                          capture_output=True, text=True, timeout=120)


def _touch(path: Path, age_s: float) -> Path:
    """A file whose mtime is `age_s` seconds in the past."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    when = time.time() - age_s
    os.utime(path, (when, when))
    return path


# ------------------------------------------------------------------------------------------- (1)

def test_an_evaluation_that_writes_only_a_stage_log_reads_as_alive(tmp_path):
    """THE DEFECT ITSELF. `events.jsonl` is an hour old, the stage log is a minute old.

    Under the old rule the lane is 3600 s silent and gets killed. Under the tree rule it is 60 s
    silent, which is what actually happened.
    """
    run = tmp_path / "run"
    t0 = int(time.time()) - 4000
    _touch(run / "events.jsonl", 3600)
    _touch(run / "nodes" / "node_3" / "score.log", 60)
    out = _bash(f'newest_activity "{run}" {t0}', tmp_path)
    assert out.returncode == 0, out.stderr
    silence = int(time.time()) - int(out.stdout.strip())
    assert silence < 120, f"a lane writing its stage log must read as alive, not {silence}s silent"


def test_a_tree_nothing_has_touched_reads_as_silent(tmp_path):
    """The falsifier: a rule that always answers "alive" would pass the test above and disarm the
    guard entirely, which is the "endpoint down, lane hung for ever" case it exists for."""
    run = tmp_path / "run"
    t0 = int(time.time()) - 4000
    _touch(run / "events.jsonl", 3600)
    _touch(run / "nodes" / "node_3" / "score.log", 3500)
    out = _bash(f'newest_activity "{run}" {t0}', tmp_path)
    silence = int(time.time()) - int(out.stdout.strip())
    assert silence > 3000, f"nothing has been written for an hour; got {silence}s"


def test_a_watch_path_that_does_not_exist_yet_is_silence_not_life(tmp_path):
    """The rule the file form was already fixed for, kept on the tree form: an absent watch path
    falls back to the START of the run, so a hang in preflight is bounded rather than unbounded."""
    t0 = int(time.time()) - 4000
    out = _bash(f'newest_activity "{tmp_path}/nothing/here" {t0}', tmp_path)
    assert out.stdout.strip() == str(t0), out.stdout


def test_a_file_watch_never_scans_its_directory(tmp_path):
    """Arm A hands over its OWN lane log, and $OUT holds every lane's log. If a file watch scanned
    its parent, one busy lane would mask another's silence — a stall guard that cannot fire."""
    campaign_out = tmp_path / "campaign"
    t0 = int(time.time()) - 4000
    mine = _touch(campaign_out / "A-svm.log", 3600)
    _touch(campaign_out / "A-other.log", 5)          # a NEIGHBOUR lane, busy right now
    out = _bash(f'newest_activity "{mine}" {t0}', tmp_path)
    silence = int(time.time()) - int(out.stdout.strip())
    assert silence > 3000, "a file watch answered with a sibling lane's activity"


def test_the_run_is_what_the_driver_hands_the_guard(tmp_path):
    """The call site half: watching the event log again would restore the defect with the fix in
    place. A directory is what makes `newest_activity` scan at all."""
    src = CAMPAIGN.read_text(encoding="utf-8")
    assert 'run_bounded "$TASK_ROOT/run" taskset' in src, (
        "arm B must hand the guard its run DIRECTORY")
    assert 'run_bounded "$TASK_ROOT/run/events.jsonl"' not in src


# ------------------------------------------------------------------------------------------- (2)

_REFUSED = {"speedup": None, "no_speedup": {"reason": "baseline_measured_in_pass",
                                            "speedup_reported": "1.0009"}}
_INVALID = {"speedup": None, "no_speedup": {"reason": "invalid_results",
                                            "instances_valid": 95}}
_SCORED = {"speedup": 3.12, "eval_seconds": 47.0}


def test_only_the_refusal_a_second_pass_could_clear_is_reopenable(tmp_path):
    """The vocabulary, driven. `invalid_results` is a fact about the CANDIDATE — re-running it buys
    the same answer and a fresh evaluation's worth of clock."""
    for name, row, expected in (("refused", _REFUSED, "baseline_measured_in_pass"),
                                ("invalid", _INVALID, ""), ("scored", _SCORED, "")):
        path = tmp_path / f"B-{name}.final.json"
        path.write_text(json.dumps(row), encoding="utf-8")
        out = _bash(f'champion_refusal "{path}"', tmp_path)
        assert out.stdout.strip() == expected, (name, out.stdout, out.stderr)


def test_an_unreadable_or_missing_row_reopens_nothing(tmp_path):
    """Silence, not a guess: a truncated row is not evidence that the pass was refused."""
    broken = tmp_path / "B-x.final.json"
    broken.write_text('{"speedup": nul', encoding="utf-8")
    assert _bash(f'champion_refusal "{broken}"', tmp_path).stdout.strip() == ""
    assert _bash(f'champion_refusal "{tmp_path}/gone.json"', tmp_path).stdout.strip() == ""


def _refused_campaign(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A finished arm-B task-arm whose champion pass was refused: marker, champion, final row."""
    out, runs = tmp_path / "campaign", tmp_path / "camp-runs"
    (runs / "svm" / "champion").mkdir(parents=True)
    (runs / "svm" / "champion" / "solver.py").write_text("class Solver: pass\n", encoding="utf-8")
    out.mkdir()
    marker = out / "B-svm.done"
    marker.write_text("wall=2100 rc=0 state=ran_to_completion cpus=0-21 "
                      "champion_refused=baseline_measured_in_pass ok_calls=40 attempt=a1\n",
                      encoding="utf-8")
    final = out / "B-svm.final.json"
    final.write_text(json.dumps(_REFUSED), encoding="utf-8")
    return marker, final, out


def test_the_flag_is_off_by_default_so_a_blind_resume_is_safe(tmp_path):
    """Every reopening in this driver is opt-in: a resume must be safe to run without reading the
    markers first."""
    marker, _final, out = _refused_campaign(tmp_path)
    res = _bash('rescore_refused_champion svm 0-21 "$MARKER"; echo "rc=$?"', tmp_path,
                OUT=str(out), RUNS_ROOT=str(tmp_path / "camp-runs"), MARKER=str(marker))
    assert "rc=1" in res.stdout, res.stdout
    assert "RE-SCORING" not in res.stdout


def test_with_the_flag_the_scoring_pass_runs_again_and_the_search_does_not(tmp_path, monkeypatch):
    """The whole point: one evaluation, no model call, no new run directory.

    `score_champion` is replaced by a stub that RECORDS its arguments and writes the row a second,
    warm-cache pass would produce — the real one needs the arena and ~2 minutes. What is under test
    is the driver's decision and its bookkeeping, so those are what run.
    """
    marker, final, out = _refused_campaign(tmp_path)
    stub = ('score_champion() { echo "$1 $2 $3" > "$OUT/called"; '
            f'printf \'%s\' \'{json.dumps(_SCORED)}\' > "$OUT/B-$1.final.json"; }}\n')
    res = _bash(stub + 'rescore_refused_champion svm 0-21 "$MARKER"; echo "rc=$?"', tmp_path,
                OUT=str(out), RUNS_ROOT=str(tmp_path / "camp-runs"), MARKER=str(marker),
                RETRY_REFUSED="1")
    assert "rc=0" in res.stdout, (res.stdout, res.stderr)
    assert "RE-SCORING" in res.stdout
    assert (out / "called").read_text(encoding="utf-8").split() == \
        ["svm", "0-21", str(tmp_path / "camp-runs" / "svm")]
    assert json.loads(final.read_text(encoding="utf-8"))["speedup"] == 3.12
    # The marker keeps every other field and loses exactly the one that made it reopenable.
    text = marker.read_text(encoding="utf-8")
    assert "champion_refused" not in text, text
    assert "state=ran_to_completion" in text and "attempt=a1" in text


def test_a_second_refusal_keeps_the_marker_reopenable(tmp_path):
    """A re-score that fails the same way may not quietly promote a task-arm that still has no
    number — the flag's meaning has to stay stable across resumes."""
    marker, _final, out = _refused_campaign(tmp_path)
    stub = ('score_champion() { printf \'%s\' \'' + json.dumps(_REFUSED) +
            '\' > "$OUT/B-$1.final.json"; }\n')
    res = _bash(stub + 'rescore_refused_champion svm 0-21 "$MARKER"', tmp_path,
                OUT=str(out), RUNS_ROOT=str(tmp_path / "camp-runs"), MARKER=str(marker),
                RETRY_REFUSED="1")
    assert "refused again" in res.stdout, res.stdout
    assert "champion_refused=baseline_measured_in_pass" in marker.read_text(encoding="utf-8")


def test_a_missing_champion_says_re_extract_rather_than_re_running_the_search(tmp_path):
    """The cheapness IS the argument for the flag. With no champion on disk the honest answer is
    "re-extract it" — the scores are in the event log — and never "spend the budget again"."""
    marker, _final, out = _refused_campaign(tmp_path)
    (tmp_path / "camp-runs" / "svm" / "champion" / "solver.py").unlink()
    res = _bash('rescore_refused_champion svm 0-21 "$MARKER"; echo "rc=$?"', tmp_path,
                OUT=str(out), RUNS_ROOT=str(tmp_path / "camp-runs"), MARKER=str(marker),
                RETRY_REFUSED="1")
    assert "rc=0" in res.stdout, "it must still swallow the task-arm rather than re-running it"
    assert "extract_champion.py" in res.stdout
