"""A `.done` marker is a claim that a task-arm was MEASURED, and exit 2 is not evidence of one.

THE FAILURE. `benchmarks/algotune/campaign.sh::record_done` treated `rc=2` as terminal on the
stated reasoning that for this campaign it is "almost always the LLM spend ceiling". But 2 is
`cli/__init__.py::REFUSAL_EXIT_CODE`, worn by EVERY `OperatorRefusal`: `BudgetExceeded` joined that
family on 2026-08-20, every new `ConfigRefusal` raise site joins it, and it already held `LLMError`
— an unreachable base URL, a half-set credential pair, a throttled key. So one bad environment
variable makes all 20 task-arms exit 2 within seconds, writes 20 markers, prints COMPLETE over a
table of nulls, and makes the resume SKIP every task because the markers exist.

WHAT SEPARATES THEM, measured rather than assumed (2026-08-22, this box, plus the preserved arm-B
corpus under `camp-runs/`):

  refused to start   the run directory holds `engine.lock`, zero bytes, and nothing else — there is
                     no `events.jsonl`, because the refusal lands before the engine opens one.
                     Reproduced identically for three different exit-2 refusals (a half-set
                     credential pair, an unreachable endpoint, the reasoning-depth clash), 1-2 s
                     each.
  ran and stopped    `events.jsonl` exists and carries `run_started` plus the accountant's
                     `llm_usage` rows. `camp-runs/convex_hull/run`: 24 kB, 17 `llm_usage` rows,
                     marker `wall=136 rc=2`. All 20 preserved arm-B markers are rc=2 and every one
                     of them has such a log.

so the discriminator is the run's own event log — not the wall clock (any threshold between 2 s and
136 s is a guess a slow endpoint invalidates) and not the exit code, which cannot separate them by
construction.

HOW THIS IS TESTED. `record_done`, `run_started_evidence`, `refuse_to_start` and `final_banner` are
extracted from `campaign.sh` and RUN, over real directories built to the two shapes above. The
campaign as a whole cannot be executed here (it needs an AlgoTune checkout, a venv and a live
endpoint), but the decision this defect lives in is four shell functions, and driving them is the
difference between proving the marker is withheld and pinning the text of a `case` arm.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

CAMPAIGN = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "campaign.sh"

# The functions under test, plus the two variables `record_done` reads out of the campaign's
# preamble (the regime it stamps into every marker).
_FUNCTIONS = ("run_started_evidence", "record_done", "refuse_to_start", "final_banner")


def _harness() -> str:
    """The four functions, verbatim, with the regime variables the campaign sets around them.

    Extraction is by NAME and asserted to have found a body, so renaming or deleting one of these is
    a red test rather than a silently vacuous one — the whole file is otherwise unrunnable here
    (`cd "$AT"`, `source .venv/bin/activate`, a live endpoint).
    """
    src = CAMPAIGN.read_text(encoding="utf-8")
    parts = ["set -u", "LANE_COUNT=4", "CORES_PER_LANE=22"]
    for name in _FUNCTIONS:
        found = re.search(rf"^{name}\(\) \{{.*?^\}}$", src, re.M | re.S)
        assert found, f"campaign.sh no longer defines {name}()"
        body = found.group(0)
        assert len(body.splitlines()) > 2, f"{name}() extracted as an empty body"
        parts.append(body)
    return "\n".join(parts) + "\n"


def _bash(script: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", _harness() + script], cwd=str(cwd),
                          capture_output=True, text=True)


def _run_that_started(root: Path, *, metered: int = 17) -> Path:
    """A run directory shaped like a real arm-B run that reached its spend ceiling."""
    run = root / "run"
    run.mkdir(parents=True)
    (run / "engine.lock").write_text("")
    rows = ['{"v":1,"seq":0,"ts":1.0,"type":"run_started","data":{}}']
    rows += ['{"v":1,"seq":%d,"ts":1.0,"type":"llm_usage","data":{"cost":0.001}}' % (i + 1)
             for i in range(metered)]
    (run / "events.jsonl").write_text("\n".join(rows) + "\n")
    return run


def _run_that_refused(root: Path) -> Path:
    """The exact on-disk shape a typed refusal leaves: the lock file, and nothing else."""
    run = root / "run"
    run.mkdir(parents=True)
    (run / "engine.lock").write_text("")
    return run


# ---------------------------------------------------------------------------------------------
# The marker itself
# ---------------------------------------------------------------------------------------------

def test_a_spent_budget_at_exit_2_still_gets_its_marker(tmp_path):
    """The legitimate rc=2 the branch was written for, and the behaviour that must not regress: a
    task-arm that ran and hit the ceiling is FINISHED, and retrying it would spend the same
    allowance and stop at the same wall."""
    run = _run_that_started(tmp_path)
    done = tmp_path / "B-svm.done"
    got = _bash(f'record_done "{done}" 2 0 "0-21" "{run}"', tmp_path)
    assert got.returncode == 0, got.stderr
    marker = done.read_text()
    assert "rc=2" in marker
    assert "metered=17" in marker                      # the evidence travels into the record
    assert "lanes=4 cores_per_lane=22" in marker       # the regime is still stamped
    assert not list(tmp_path.glob("*.refused"))


def test_a_refusal_to_start_at_exit_2_gets_no_marker(tmp_path):
    """The defect. Same exit code, opposite fact, and the marker is what a resume keys on."""
    run = _run_that_refused(tmp_path)
    done = tmp_path / "B-svm.done"
    got = _bash(f'record_done "{done}" 2 0 "0-21" "{run}"', tmp_path)
    assert not done.exists(), f"a marker was written for a run that never started: {done.read_text()}"
    refused = tmp_path / "B-svm.refused"
    assert refused.exists() and "evidence=none" in refused.read_text()
    assert "REFUSED TO START" in got.stderr
    assert "still owed" in got.stderr
    assert "B-svm.log" in got.stderr                   # names where the cause actually is


def test_an_empty_event_log_is_not_a_started_run(tmp_path):
    """A zero-byte `events.jsonl` is the file the engine creates and never writes to — the same
    "nothing happened" as no file at all, and `-s` rather than `-e` is what makes them agree."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "events.jsonl").write_text("")
    done = tmp_path / "B-svm.done"
    _bash(f'record_done "{done}" 2 0 "0-21" "{run}"', tmp_path)
    assert not done.exists()


def test_arm_a_exit_2_is_not_this_campaigns_refusal_code(tmp_path):
    """Arm A runs AlgoTuner, not LoopLab, so it has no event log AND no claim on exit 2: the branch
    is justified entirely by `cli/__init__.py::REFUSAL_EXIT_CODE`, which AlgoTuner does not
    implement. All 20 preserved arm-A markers are rc=0, so this has never fired on a real arm A."""
    done = tmp_path / "A-svm.done"
    got = _bash(f'record_done "{done}" 2 0 "0-21" ""', tmp_path)
    assert not done.exists()
    assert (tmp_path / "A-svm.refused").exists()
    assert "arm A runs no LoopLab engine" in got.stderr


@pytest.mark.parametrize("rc", [0, 124])
def test_the_terminal_codes_that_need_no_evidence_are_unchanged(rc, tmp_path):
    """rc=0 is the run ending on its own and rc=124 is the wall-clock net; neither is ambiguous
    about whether the task ran, so neither consults the log. Checked with the refusal-shaped run
    dir, so an over-eager evidence gate would show up here."""
    run = _run_that_refused(tmp_path)
    done = tmp_path / "B-svm.done"
    got = _bash(f'record_done "{done}" {rc} 0 "0-21" "{run}"', tmp_path)
    assert got.returncode == 0, got.stderr
    assert f"rc={rc}" in done.read_text()


@pytest.mark.parametrize("rc", [130, 137, 143, 1])
def test_an_interruption_is_still_neither_a_marker_nor_a_refusal(rc, tmp_path):
    """The pre-existing rule, and the boundary of the new one: an interrupted task has no verdict,
    but it also did not refuse to start, so it must not land in the refusal tally either."""
    run = _run_that_started(tmp_path)
    done = tmp_path / "B-svm.done"
    got = _bash(f'record_done "{done}" {rc} 0 "0-21" "{run}"', tmp_path)
    assert not done.exists()
    assert not list(tmp_path.glob("*.refused"))
    assert "interrupted" in got.stdout


def test_a_started_run_that_paid_for_nothing_is_recorded_rather_than_hidden(tmp_path):
    """`metered=0` is a real state (a local model reports no cost, and a refusal can land after
    `run_started`). It is worth SEEING in the marker, but it is not what decides the marker: the run
    started, so whatever it did is a measurement of something."""
    run = _run_that_started(tmp_path, metered=0)
    done = tmp_path / "B-svm.done"
    _bash(f'record_done "{done}" 2 0 "0-21" "{run}"', tmp_path)
    assert "metered=0" in done.read_text()


# ---------------------------------------------------------------------------------------------
# What the campaign SAYS at the end
# ---------------------------------------------------------------------------------------------

def test_the_driver_does_not_say_complete_over_an_arm_that_never_ran(tmp_path):
    """The half that makes the failure loud. A silently-correct marker rule would still have let the
    driver print COMPLETE and hand the operator a summarise command for a table of nothing."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "B-svm.refused").write_text("wall=1 rc=2 cpus=0-21 evidence=none\n")
    (out / "B-pagerank.done").write_text("wall=136 rc=2 cpus=22-43 metered=17\n")
    got = _bash(f'final_banner "{out}" B 20', tmp_path)
    assert got.returncode == 3, got.stdout
    assert "COMPLETE" not in got.stdout
    # "NOT MEASURED" rather than "INCOMPLETE" precisely because the latter CONTAINS the
    # success banner's word, so a watcher grepping for COMPLETE would match the failure.
    assert "NOT MEASURED" in got.stdout and "1 of 20" in got.stdout
    assert "B-svm" in got.stdout                       # names the task, not just a count
    assert "Do NOT summarise" in got.stdout


def test_the_driver_still_says_complete_when_every_task_arm_reached_a_verdict(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for task in ("svm", "pagerank"):
        (out / f"B-{task}.done").write_text("wall=136 rc=2 cpus=0-21 metered=17\n")
    got = _bash(f'final_banner "{out}" B 2', tmp_path)
    assert got.returncode == 0, got.stdout
    assert "COMPLETE" in got.stdout and "NOT MEASURED" not in got.stdout
    assert "2/2 markers" in got.stdout


def test_one_arms_refusals_do_not_count_against_the_other(tmp_path):
    """The tally is per arm, like the markers: an arm-A refusal must not make arm B report
    INCOMPLETE, or a two-arm box could never get a clean banner for either."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "A-svm.refused").write_text("wall=1 rc=2 cpus=0-21 evidence=none\n")
    (out / "B-svm.done").write_text("wall=136 rc=2 cpus=0-21 metered=17\n")
    assert _bash(f'final_banner "{out}" B 1', tmp_path).returncode == 0
    assert _bash(f'final_banner "{out}" A 1', tmp_path).returncode == 3


def test_the_campaign_clears_last_invocations_tally_but_never_a_marker(tmp_path):
    """`.refused` is this invocation's tally, so a fixed-and-re-run arm must not inherit the last
    one's; `.done` is the durable record of a task-arm already measured and must survive.

    Read off the script because the reset happens in its preamble, which is the part that cannot be
    executed here — but the two globs are checked to be exactly one apart, which is the property."""
    src = CAMPAIGN.read_text(encoding="utf-8")
    resets = re.findall(r"^rm -f .*$", src, re.M)
    assert resets == ['rm -f "$OUT/$ARM"-*.refused'], resets


def test_the_exit_code_of_an_unmeasured_arm_is_not_success():
    """A wrapper that summarises on exit 0 must not be handed a table of nothing, and 3 is distinct
    from the 2 this script already uses for its own pre-flight refusals."""
    src = CAMPAIGN.read_text(encoding="utf-8")
    assert 'exit "$CAMPAIGN_RC"' in src
    assert "CAMPAIGN_RC=$?" in src
