"""A task-arm the operator skipped must not be counted among the finished ones.

`campaign.sh::already_measured` skips on ANY non-empty marker that is not a wall cut. That is how a
running campaign is told to stop taking new work without editing the driver a live bash is reading
incrementally — and it is the right mechanism. What it leaves behind looks, to every later reader,
exactly like a completed run.

Used for real on 2026-08-26: five CP-SAT task-arms were skipped by decision so the campaign could
be closed after the batch in flight. The pairs are already excluded from the means, because
`agent_summary.json` carries no score for a task that never ran — what these tests pin is that the
table says the WORD, and says how many of its rows are missing an arm on purpose.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "compare_arms.py"

# Both arms carry the ruler identity `compare_arms.py::pair_refusal` requires since 2026-09-06, so
# a pair here is refused for being a SKIP and for nothing else.
_SHA = "cd" * 32
DONE = (f"wall=100 rc=0 state=ran_to_completion ok_calls=5 eval_workers=22 regime=__w22x1r3 "
        f"baseline_sha256={_SHA} attempt=a1\n")
SKIP = "wall=0 rc=0 state=operator_skip attempt=a1\n"


def _campaign(tmp: Path, *, a_marker: str, a_score: bool) -> Path:
    root = tmp / "bench"
    (root / "AlgoTune" / "reports").mkdir(parents=True)
    summary = ({"demo": {"gateway/deepseek-v4-flash": {"final_speedup": 2.5}}} if a_score else {})
    (root / "AlgoTune" / "reports" / "agent_summary.json").write_text(json.dumps(summary),
                                                                     encoding="utf-8")
    (root / "runs-B" / "demo" / "run").mkdir(parents=True)
    (root / "runs-B" / "demo" / "run" / "events.jsonl").write_text("", encoding="utf-8")
    final = root / "campaign-final"
    final.mkdir(parents=True)
    (final / "B-demo.final.json").write_text(
        json.dumps({"speedup": 1.5, "subset": "test", "eval_workers": "22",
                    "eval_regime": {"key": "__w22x1r3"}, "baseline_cache_sha256": _SHA}),
        encoding="utf-8")
    (final / "B-demo.done").write_text(DONE, encoding="utf-8")
    (final / "A-demo.done").write_text(a_marker, encoding="utf-8")
    return root


def _run(root: Path) -> str:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--algotune-root", str(root / "AlgoTune"),
         "--runs-root", str(root / "runs-B"), "--final-dir", str(root / "campaign-final")],
        capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_a_skipped_arm_is_named_as_skipped(tmp_path):
    out = _run(_campaign(tmp_path, a_marker=SKIP, a_score=False))
    assert "SKIPPED by the operator and never ran" in out, out
    assert "demo" in out
    assert "over 0 of 1 tasks" in out, "it does not say how much of the table is missing"


def test_a_skipped_arm_is_not_averaged(tmp_path):
    """The falsifier for a skip that quietly becomes a zero or a win."""
    out = _run(_campaign(tmp_path, a_marker=SKIP, a_score=False))
    assert "mean over" not in out or "mean over 0" in out, out
    # THE ROW, not the page. The footer explains what a `0.0000` means in prose, so scanning the
    # whole output for that string tests the explanation rather than the table -- which is what the
    # first version of this assertion did.
    row = next(line for line in out.splitlines() if line.strip().startswith("demo"))
    assert "0.0000" not in row, f"a skip was rendered as a scored zero: {row!r}"
    assert row.split()[1] == "--", f"arm A shows a number for a task that never ran: {row!r}"


def test_a_real_finish_is_still_a_finish(tmp_path):
    """The other falsifier: normal markers must not start reading as skips."""
    out = _run(_campaign(tmp_path, a_marker=DONE, a_score=True))
    assert "SKIPPED by the operator" not in out, out
    assert "mean over 1 complete pair" in out, out


def test_a_wall_cut_is_still_a_wall_cut(tmp_path):
    """`state=` is read in order and the older classes must keep winning their own marker text."""
    out = _run(_campaign(tmp_path, a_marker="wall=100 rc=124 state=wall_cut attempt=a1\n",
                         a_score=True))
    assert "WALL CLOCK" in out
    assert "SKIPPED by the operator" not in out


# ------------------------------------------------------------------------------------------------
# and the OTHER end of the same fact: the driver's own banner
# ------------------------------------------------------------------------------------------------
# `compare_arms.py` above learned the word on 2026-08-26. `campaign.sh` did not: it knew
# `wall_cut`, `stall_cut` and the legacy `rc=124`, and every other marker was a finish. So an arm
# whose task-arms were skipped counted them into its own success banner. Reproduced 2026-08-30 over
# a five-task marker directory holding two skips: `===== arm B COMPLETE (5/5 markers) =====`,
# exit 0, three measurements. At the campaign's twenty tasks and the eight skips of that session it
# reads `COMPLETE (20/20 markers)`.
#
# The functions are EXTRACTED and RUN rather than pattern-matched: the property is what the banner
# says over a directory that holds a skip, and the rest of the script cannot execute here.
CAMPAIGN = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "campaign.sh"
_BANNER_FUNCTIONS = ("marker_is_harness_cut", "marker_is_operator_skip",
                     "marker_is_immediate_exit", "already_measured", "final_banner")


def _campaign_bash(script: str) -> subprocess.CompletedProcess:
    import re

    src = CAMPAIGN.read_text(encoding="utf-8")
    parts = ["set -u", "LANE_COUNT=1", "CORES_PER_LANE=2"]
    for name in _BANNER_FUNCTIONS:
        found = re.search(rf"^{name}\(\) \{{.*?^\}}$", src, re.M | re.S)
        assert found, f"campaign.sh no longer defines {name}()"
        assert len(found.group(0).splitlines()) > 2, f"{name}() extracted as an empty body"
        parts.append(found.group(0))
    return subprocess.run(["bash", "-c", "\n".join(parts) + "\n" + script],
                          capture_output=True, text=True, timeout=60)


def _markers(tmp: Path, **markers: str) -> Path:
    out = tmp / "camp"
    out.mkdir(exist_ok=True)
    for task, text in markers.items():
        (out / f"B-{task}.done").write_text(text, encoding="utf-8")
    return out


def test_the_banner_does_not_count_a_skip_among_the_measured(tmp_path):
    """The falsifier for `COMPLETE (20/20 markers)` over twelve measurements."""
    out = _markers(tmp_path, alpha=DONE, beta=DONE, gamma=SKIP, delta=SKIP)
    got = _campaign_bash(f'final_banner "{out}" B 4 "alpha beta gamma delta"')
    assert got.returncode == 0, got.stdout + got.stderr
    assert "SKIPPED BY THE OPERATOR" in got.stdout, got.stdout
    assert "B-gamma" in got.stdout and "B-delta" in got.stdout, got.stdout
    assert "2 MEASURED" in got.stdout, "the banner still claims every marker is a measurement"
    assert "COMPLETE (4/4 markers)" not in got.stdout, got.stdout
    # The measured ones are not dragged into the skip list with them.
    assert "B-alpha" not in got.stdout.split("SKIPPED BY THE OPERATOR", 1)[1].split("=====")[0]


def test_the_banner_says_nothing_about_skips_when_there_are_none(tmp_path):
    """The control: a line that always prints is a line nobody reads, and the exact wording of the
    clean banner is what a watcher greps for."""
    out = _markers(tmp_path, alpha=DONE, beta=DONE)
    got = _campaign_bash(f'final_banner "{out}" B 2 "alpha beta"')
    assert got.returncode == 0, got.stdout + got.stderr
    assert "SKIPPED" not in got.stdout, got.stdout
    assert "arm B COMPLETE (2/2 markers)" in got.stdout, got.stdout


def test_a_skip_is_still_terminal_and_retry_wall_cut_does_not_reopen_it(tmp_path):
    """Writing the marker IS the mechanism, so a resume must keep skipping it — and `RETRY_WALL_CUT`
    reopens CLOCK kills, not decisions. Folding `operator_skip` into `marker_is_harness_cut` would
    have undone the operator's own instruction on the next resume."""
    out = _markers(tmp_path, gamma=SKIP)
    assert _campaign_bash(f'already_measured "{out}/B-gamma.done"').returncode == 0
    assert _campaign_bash(
        f'RETRY_WALL_CUT=1; already_measured "{out}/B-gamma.done"').returncode == 0
    # ...while a real wall cut still reopens, so the flag has not been broken in the process.
    wall = _markers(tmp_path, omega="wall=14400 rc=124 state=wall_cut\n")
    assert _campaign_bash(
        f'RETRY_WALL_CUT=1; already_measured "{wall}/B-omega.done"').returncode == 1
