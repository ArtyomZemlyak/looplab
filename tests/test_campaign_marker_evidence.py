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

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

CAMPAIGN = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "campaign.sh"

# The functions under test, plus the variables `record_done` reads out of the campaign's preamble
# (the regime it stamps into every marker, and the arm/task its rc=0 evidence check is ABOUT).
#
# `successful_calls` is in this list because `record_done`'s rc=0 arm CALLS it. Without it the
# harness ran that arm against an undefined command AND an unbound `$ARM`, so under `set -u` the
# command substitution aborted, `OK_CALLS` came back empty, and the "a run that paid for nothing
# gets NO MARKER" rung was never once executed by this file — while every marker assertion below
# still passed. A guard whose subject is missing from the extraction list is a guard that cannot
# go red when the subject is deleted, which is the whole reason the extraction asserts by NAME.
# `marker_is_harness_cut` and `marker_is_operator_skip` are here because `already_measured` and
# `final_banner` CALL them. Without the first, the extracted bash died with "command not found",
# `final_banner` printed COMPLETE with no WALL-CUT line and four tests here were red against a
# production script that was fine (the 2026-08-29 merge introduced the helper; this list lagged it,
# the way it lagged LANE_LAYOUT at 41d111a). Both are top-level `name() {` blocks like the rest.
# THE BANNER'S OWN HEADING, checked against the production script rather than only typed here.
# These assertions said _CUT_BANNER while `final_banner` prints "STOPPED BY THE HARNESS (wall cut or
# stall cut)": two of them were red against a script that was fine, and the third -- the CONTROL
# that asserts the line is ABSENT when nothing was cut -- was green VACUOUSLY, because the string it
# looked for never appears at all. A control that cannot fail is the shape this file exists to
# refuse, so the heading is asserted to exist in the source and a rename goes red here instead of
# silently retiring all three.
_CUT_BANNER = "STOPPED BY THE HARNESS"
assert _CUT_BANNER in CAMPAIGN.read_text(encoding="utf-8"), (
    "final_banner no longer prints this heading; re-point these assertions and RE-CHECK that the "
    "cut task-arms are still named inside the banner rather than only making the tests green")

# `marker_is_immediate_exit` joined 2026-09-06 with the `exited_immediately` state: `already_measured`
# and `final_banner` call it, and `record_done` reads `IMMEDIATE_EXIT_S`, which the preamble sets
# and this harness therefore has to stub (mirroring the script's own default, like LANE_LAYOUT).
_FUNCTIONS = ("run_started_evidence", "successful_calls", "next_attempt",
              "marker_is_harness_cut", "marker_is_operator_skip", "marker_is_immediate_exit",
              "already_measured", "record_done", "refuse_to_start", "final_banner")


def _harness() -> str:
    """The four functions, verbatim, with the regime variables the campaign sets around them.

    Extraction is by NAME and asserted to have found a body, so renaming or deleting one of these is
    a red test rather than a silently vacuous one — the whole file is otherwise unrunnable here
    (`cd "$AT"`, `source .venv/bin/activate`, a live endpoint).
    """
    src = CAMPAIGN.read_text(encoding="utf-8")
    # `ARM` and `T` are read as GLOBALS by `record_done`'s rc=0 arm (the marker path is a
    # positional but the meter lookup is not), so the harness has to stand in for `run_one`'s
    # assignments or `set -u` aborts the substitution. `METER_LOG` is deliberately left unset by
    # default: that is the "no meter" shape, in which `successful_calls` answers "" and the rung
    # keeps its pre-2026-08-25 behaviour. A test that wants the rung ARMED exports it itself.
    # `LANE_LAYOUT` joined this stub on 2026-08-29, when the merge brought in the campaign's own
    # `layout=$LANE_LAYOUT` in the REGIME line. The production script assigns it at line 265, well
    # before `record_done` reads it, so `set -u` is satisfied there; what was incomplete was this
    # harness, which extracts the function away from its assignment. The value mirrors the script's
    # own default rather than inventing one.
    parts = ["set -u", "LANE_COUNT=4", "CORES_PER_LANE=22", 'LANE_LAYOUT="whole_cores"',
             'IMMEDIATE_EXIT_S="${IMMEDIATE_EXIT_S:-60}"', 'ARM="${ARM:-B}"', 'T="${T:-svm}"']
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


# ---------------------------------------------------------------------------------------------
# A WALL-CLOCK KILL IS NOT A FINISHED TASK
#
# `HARD_TIMEOUT` sends SIGTERM at 4 h and `record_done` wrote a `.done` for rc=124 in the SAME
# branch as a clean exit -- one marker, two opposite facts, and every reader downstream had to
# recover "a clock killed this" from an integer. Only `compare_arms.py` ever learned to; the driver
# counted a wall cut into its own COMPLETE banner and `campaign_status.py` printed it as finished.
#
# Measured 2026-08-23 on `/var/tmp/looplab-bench/campaign-paired`: FIVE task-arms were cut at the
# wall (A-convex_hull, A-count_riemann_zeta_zeros, B-max_weighted_independent_set, B-pde_heat1d,
# B-sparse_eigenvectors_complex) and THREE of them had not spent the budget they are compared at --
# $0.70, $0.139 and $0.866 of $1.00 in the meter log. `A-count_riemann_zeta_zeros` reached $0.14
# because forty nginx 504s at 300 s each ate three and a half of its four hours.
# ---------------------------------------------------------------------------------------------

# The five markers exactly as `campaign-paired/` holds them: written BEFORE `state=` existed, which
# is the shape the back-compat branch has to keep reading.
_LEGACY_WALL_CUT = "wall=14400 rc=124 cpus=22-43 lanes=4 cores_per_lane=22\n"


@pytest.mark.parametrize("rc,state", [(0, "ran_to_completion"), (124, "wall_cut")])
def test_the_marker_says_in_words_what_happened(rc, state, tmp_path):
    """rc=0 and rc=124 shared one `case` arm and one marker format, so the two states were spelled
    only by an integer nobody but `compare_arms.py` read. `rc=` stays beside `state=` -- the 27
    markers already on disk carry only the integer."""
    run = _run_that_started(tmp_path)
    done = tmp_path / "B-svm.done"
    got = _bash(f'record_done "{done}" {rc} 0 "0-21" "{run}"', tmp_path)
    assert got.returncode == 0, got.stderr
    marker = done.read_text()
    assert f"state={state}" in marker, marker
    assert f"rc={rc}" in marker, marker


def test_a_refusal_after_the_run_started_is_its_own_state(tmp_path):
    """The third member, so the vocabulary is closed rather than "wall_cut and everything else"."""
    run = _run_that_started(tmp_path)
    done = tmp_path / "B-svm.done"
    _bash(f'record_done "{done}" 2 0 "0-21" "{run}"', tmp_path)
    marker = done.read_text()
    assert "state=stopped_after_start" in marker, marker
    assert "state=wall_cut" not in marker, marker


def _meter_log(root, rows) -> str:
    """A `meter.jsonl` in the shape `benchmarks/meter/proxy.py` writes it (stdlib `json.dumps`,
    i.e. `": "` after every key — which is the spacing `successful_calls`' own `grep` matches)."""
    path = root / "meter.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path)


def test_rc0_with_no_successful_call_writes_no_marker(tmp_path):
    """The 2026-08-25 rung, DRIVEN rather than pinned. A total endpoint outage exits 0 in seconds
    having bought nothing; a marker would make every later resume skip the task for ever."""
    run = _run_that_started(tmp_path)
    done = tmp_path / "B-svm.done"
    log = _meter_log(tmp_path, [{"arm": "B", "task": "svm", "attempt": "a1", "status": 503,
                                 "error": "no available workers"}])
    got = _bash(f'METER_LOG="{log}"; ATTEMPT=a1; record_done "{done}" 0 0 "0-21" "{run}"', tmp_path)
    assert not done.exists(), done.read_text()
    assert "NO SUCCESSFUL CALLS" in got.stdout, got.stdout + got.stderr


def test_rc0_with_a_successful_call_still_writes_its_marker(tmp_path):
    """The other half, so the rung above cannot be satisfied by refusing every marker: positive
    evidence for THIS attempt writes the marker and records the count it was decided on."""
    run = _run_that_started(tmp_path)
    done = tmp_path / "B-svm.done"
    log = _meter_log(tmp_path, [
        {"arm": "B", "task": "svm", "attempt": "a1", "status": "200"},
        {"arm": "B", "task": "svm", "attempt": "a0", "status": "200"},   # another attempt: not ours
        {"arm": "A", "task": "svm", "attempt": "a1", "status": "200"},   # the other arm
    ])
    _bash(f'METER_LOG="{log}"; ATTEMPT=a1; record_done "{done}" 0 0 "0-21" "{run}"', tmp_path)
    assert done.exists()
    assert "ok_calls=1" in done.read_text(), done.read_text()


def test_an_unreadable_meter_log_leaves_the_old_behaviour(tmp_path):
    """"" and "0" are different answers and only "0" refuses. A bookkeeping gap is not evidence
    that a run bought nothing, so the marker is still written."""
    run = _run_that_started(tmp_path)
    done = tmp_path / "B-svm.done"
    got = _bash(f'METER_LOG="{tmp_path}/nope.jsonl"; ATTEMPT=a1; '
                f'record_done "{done}" 0 0 "0-21" "{run}"', tmp_path)
    assert done.exists(), got.stdout + got.stderr
    assert "NO SUCCESSFUL CALLS" not in got.stdout


def test_the_marker_carries_the_attempt_that_wrote_it(tmp_path):
    """The join to the meter. `attempt=` names the `/m/<arm>/<task>/<attempt>/v1` path this run's
    calls went to, so a per-task cost can be summed for THIS attempt and not for every attempt ever
    made at the task. A marker written outside `run_one` says `none` rather than guessing."""
    run = _run_that_started(tmp_path)
    done = tmp_path / "B-svm.done"
    _bash(f'ATTEMPT=a7; record_done "{done}" 0 0 "0-21" "{run}"', tmp_path)
    assert "attempt=a7" in done.read_text()
    other = tmp_path / "B-other.done"
    _bash(f'record_done "{other}" 0 0 "0-21" "{run}"', tmp_path)
    assert "attempt=none" in other.read_text()


# ---------------------------------------------------------------------------------------------
# Is a wall cut resumable? By default NO, and the flag is the argument.
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("marker_text", [
    "wall=14400 rc=124 state=wall_cut cpus=0-21 lanes=4 cores_per_lane=22 attempt=a1\n",
    _LEGACY_WALL_CUT,          # written before `state=` existed -- five of these are on disk
])
def test_a_wall_cut_is_terminal_by_default_and_a_blind_resume_leaves_it_alone(marker_text, tmp_path):
    """`.done` means "do not run this again", and a resume must be safe to run blind.

    The driver cannot tell "the wall bound because forty gateway 504s ate the clock" from "this task
    genuinely needs more than four hours", and auto-retrying the second kind spends four hours and a
    dollar to reproduce the same cut on every resume, forever.
    """
    done = tmp_path / "B-svm.done"
    done.write_text(marker_text)
    assert _bash(f'already_measured "{done}"', tmp_path).returncode == 0


@pytest.mark.parametrize("marker_text", [
    "wall=14400 rc=124 state=wall_cut cpus=0-21 attempt=a1\n",
    _LEGACY_WALL_CUT,
])
def test_retry_wall_cut_reopens_a_wall_cut_without_deleting_its_marker(marker_text, tmp_path):
    """The retry is ONE FLAG away rather than zero, because the alternative is deleting `.done`
    files by hand -- which is how a marker over a real measurement gets destroyed.
    `PENDING_FIXES.md` item 4 (raise the wall once the transport fixes land) is this operation."""
    done = tmp_path / "B-svm.done"
    done.write_text(marker_text)
    assert _bash(f'RETRY_WALL_CUT=1; already_measured "{done}"', tmp_path).returncode == 1
    assert done.exists(), "the flag must not delete the marker it reopens"


@pytest.mark.parametrize("marker_text", [
    "wall=100 rc=0 state=ran_to_completion cpus=0-21 attempt=a1\n",
    "wall=8808 rc=2 state=stopped_after_start cpus=0-21 metered=371 attempt=a1\n",
    "wall=7775 rc=0 cpus=44-65 lanes=4 cores_per_lane=22\n",     # a real pre-`state=` marker
])
def test_retry_wall_cut_reopens_nothing_else(marker_text, tmp_path):
    """THE BOUNDARY. The flag must reopen exactly the wall cuts: a task-arm that ran to completion
    or spent its ceiling is a MEASUREMENT, and re-running it spends the allowance again to reach the
    same place. A flag that reopened everything would just be a slower `rm *.done`."""
    done = tmp_path / "B-svm.done"
    done.write_text(marker_text)
    assert _bash(f'RETRY_WALL_CUT=1; already_measured "{done}"', tmp_path).returncode == 0


def test_no_marker_at_all_is_still_owed_with_or_without_the_flag(tmp_path):
    """The pre-existing rule the hoisted predicate must not have changed: an interrupted task-arm
    has no marker, and `already_measured` has to answer "run it" for both flag values."""
    done = tmp_path / "B-svm.done"
    assert _bash(f'already_measured "{done}"', tmp_path).returncode == 1
    done.write_text("")                              # the zero-byte case `-s` exists to catch
    assert _bash(f'already_measured "{done}"', tmp_path).returncode == 1
    assert _bash(f'RETRY_WALL_CUT=1; already_measured "{done}"', tmp_path).returncode == 1


# ---------------------------------------------------------------------------------------------
# "rc=124 produces no number" was FALSE for arm B, and the behaviour is what proves it
# ---------------------------------------------------------------------------------------------

def test_a_wall_cut_does_not_erase_the_number_the_run_left_behind(tmp_path):
    """`campaign.sh:309` said rc=124 "produces no number (see docs/51)". docs/51 item 4 measures
    ARM A, where a cut AlgoTuner run writes no `final_speedup` into `agent_summary.json` at all --
    and the live corpus agrees, neither wall-cut arm-A task has an entry. It is FALSE for arm B:
    the champion extraction and the TEST scoring pass run in `run_one` AFTER `timeout` has killed
    the run, so `B-pde_heat1d.final.json` = 3.1223, `B-sparse_eigenvectors_complex` = 1.0045 and
    `B-max_weighted_independent_set` = 1.0393 are all real scores behind an rc=124 marker.

    THE COMMENT WAS CORRECTED AND THE BEHAVIOUR KEPT, so this is the behaviour: the number survives
    the marker, and the marker says the number is not a measurement at the budget.
    """
    run = _run_that_started(tmp_path)
    final = tmp_path / "B-pde_heat1d.final.json"
    final.write_text('{"speedup": 3.1223, "eval_seconds": 47.2, "subset": "test"}')
    done = tmp_path / "B-pde_heat1d.done"
    _bash(f'record_done "{done}" 124 0 "66-87" "{run}"', tmp_path)
    assert "3.1223" in final.read_text(), "the wall cut destroyed a real score"
    assert "state=wall_cut" in done.read_text()


def test_the_falsified_sentence_is_not_back():
    """A negative pin over the DEFECT'S OWN TEXT (CLAUDE.md's ladder, tier 2): red if anyone
    restores the claim, which is a claim about arm B that three live files falsify."""
    src = CAMPAIGN.read_text(encoding="utf-8")
    assert "produces no number" not in src, (
        "rc=124 produces no number for arm A only; arm B's champion is extracted and scored after "
        "the kill (B-pde_heat1d.final.json = 3.1223)")


# ---------------------------------------------------------------------------------------------
# What the banner SAYS about a wall cut it counted as complete
# ---------------------------------------------------------------------------------------------

def test_the_banner_names_the_wall_cut_task_arms_inside_its_own_complete(tmp_path):
    """A wall cut IS terminal, so it counts into the marker total and the arm really is complete.
    But a banner that prints only a count hides the one fact an operator needs to decide whether to
    raise HARD_TIMEOUT: that the wall bound at all, on three of the five cuts before the budget did.
    """
    out = tmp_path / "out"
    out.mkdir()
    (out / "B-alpha.done").write_text("wall=100 rc=0 state=ran_to_completion cpus=0-21\n")
    (out / "B-beta.done").write_text("wall=14447 rc=124 state=wall_cut cpus=66-87\n")
    got = _bash(f'final_banner "{out}" B 2 "alpha beta"', tmp_path)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "COMPLETE (2/2 markers)" in got.stdout, got.stdout
    assert _CUT_BANNER in got.stdout, got.stdout
    assert "B-beta" in got.stdout, got.stdout
    assert "B-alpha" not in got.stdout.split(_CUT_BANNER, 1)[1], got.stdout
    assert "RETRY_WALL_CUT=1" in got.stdout


def test_the_banner_says_nothing_about_wall_cuts_when_there_are_none(tmp_path):
    """The control. A line that always prints is a line nobody reads."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "B-alpha.done").write_text("wall=100 rc=0 state=ran_to_completion cpus=0-21\n")
    got = _bash(f'final_banner "{out}" B 1 "alpha"', tmp_path)
    assert got.returncode == 0, got.stdout
    assert _CUT_BANNER not in got.stdout, got.stdout


def test_the_banner_reads_a_marker_written_before_state_existed(tmp_path):
    """Five real markers under `campaign-paired/` carry only `rc=124`. A banner that keyed on
    `state=wall_cut` alone would silently reclassify all five as clean finishes -- the defect,
    arriving through the fix for it."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "A-convex_hull.done").write_text(_LEGACY_WALL_CUT)
    got = _bash(f'final_banner "{out}" A 1 "convex_hull"', tmp_path)
    assert _CUT_BANNER in got.stdout, got.stdout
    assert "A-convex_hull" in got.stdout, got.stdout


# ---------------------------------------------------------------------------------------------
# The attempt ledger: an id the CAMPAIGN mints, not one the proxy invents
# ---------------------------------------------------------------------------------------------

def test_attempts_at_one_task_arm_are_numbered_and_recorded(tmp_path):
    """`(arm, task)` is not an identity. Measured on `meter/meter.jsonl`: `B/kcenters` holds $2.0086
    over 816 calls in four sessions against ONE `.done` marker whose run cost $1.0070, so a naive
    per-task sum reads 2x a $1.00 ceiling and looks like a breach that never happened.

    The ledger is append-only and survives the `rm -rf "$TASK_ROOT"` a re-run does, so an attempt
    the marker no longer mentions is still on record.
    """
    out = tmp_path / "out"
    out.mkdir()
    got = _bash(f'OUT="{out}"; next_attempt B kcenters; next_attempt B kcenters; '
                f'next_attempt B discrete_log', tmp_path)
    assert got.returncode == 0, got.stderr
    assert got.stdout.split() == ["a1", "a2", "a1"], got.stdout
    ledger = (out / "B-kcenters.attempts").read_text().splitlines()
    assert len(ledger) == 2, ledger
    assert ledger[0].startswith("a1 started=") and ledger[1].startswith("a2 started="), ledger
    assert "epoch=" in ledger[0]
    # per task-arm, not per task: discrete_log starts at a1 of its own
    assert (out / "B-discrete_log.attempts").read_text().startswith("a1 ")


def test_the_id_the_campaign_mints_is_the_id_the_proxy_reads_back(tmp_path):
    """THE TWO HALVES OF ONE FIX, JOINED. `campaign.sh` mints the id and `meter/proxy.py` parses it
    out of the URL, and the whole point is that the marker and the meter row name the SAME attempt.

    Driven rather than pinned: the id comes out of the real allocator, is pasted into the real URL
    template `run_one` builds, and is handed to the real `_split_path`. It also pins the one thing
    an id may not be -- `v1` -- because the parser tells the two URL shapes apart by whether the
    fourth segment is the upstream prefix, so an attempt named `v1` would silently lose its name.
    """
    out = tmp_path / "out"
    out.mkdir()
    got = _bash(f'OUT="{out}"; next_attempt B kcenters', tmp_path)
    attempt = got.stdout.strip()
    assert attempt and attempt != "v1", attempt

    spec = importlib.util.spec_from_file_location(
        "meter_proxy_under_test", CAMPAIGN.parent.parent / "meter" / "proxy.py")
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)

    class _Path(proxy.Handler):                     # the parser, without a socket
        def __init__(self, path):
            self.path = path

    url_tail = f"/m/B/kcenters/{attempt}/v1/chat/completions"
    assert _Path(url_tail)._split_path() == ("B", "kcenters", attempt, "/v1/chat/completions")
    # and the marker for that same run names it, so the two are joinable by equality alone
    run = _run_that_started(tmp_path)
    done = tmp_path / "B-kcenters.done"
    _bash(f'ATTEMPT={attempt}; record_done "{done}" 0 0 "0-21" "{run}"', tmp_path)
    assert f"attempt={attempt}" in done.read_text()
