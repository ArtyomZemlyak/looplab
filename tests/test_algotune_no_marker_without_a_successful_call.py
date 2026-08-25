"""A run that made no successful call did not complete, whatever its exit code says.

THE DEFECT, measured 2026-08-25. The gateway's model group went to
`503 No available workers (all circuits open or unhealthy)` in the middle of a campaign. Arm A's
remaining sixteen task-arms each exited 0 after THREE TO NINETEEN SECONDS, having made no
successful call at all:

    wall=3  rc=0 state=ran_to_completion ... queens_with_obstacles
    wall=4  rc=0 state=ran_to_completion ... max_clique_cpsat
    wall=5  rc=0 state=ran_to_completion ... kcenters

`final_banner` counted 20/20 and the driver printed "FINAL CAMPAIGN COMPLETE". A total outage of
the endpoint was, in the markers, indistinguishable from a campaign that worked — and a `.done`
marker means "do not re-run this", so a resume would have skipped all sixteen for ever.

`rc=0` now needs one more fact: at least one metered call came back 200. Without it no marker is
written and the task stays owed, exactly as for an interruption.
"""
import json
import re
import subprocess
from pathlib import Path

CAMPAIGN = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "campaign.sh"


def _successful_calls(meter_log, arm, task, attempt="a1", set_env=True) -> str:
    src = CAMPAIGN.read_text(encoding="utf-8")
    m = re.search(r"^successful_calls\(\)\s*\{.*?^\}", src, re.S | re.M)
    assert m, "successful_calls() is gone from campaign.sh — this test needs rewriting"
    env_line = f'METER_LOG={meter_log}\n' if set_env else "unset METER_LOG\n"
    script = env_line + m.group(0) + f'\nsuccessful_calls {arm} {task} {attempt}\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _log(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "meter.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def test_an_outage_leaves_zero_successful_calls(tmp_path):
    """The real shape: rows exist for the arm, none of them for this task came back 200."""
    log = _log(tmp_path, [
        {"arm": "A", "task": "convex_hull", "attempt": "a1", "status": "200"},
        {"arm": "A", "task": "kcenters", "attempt": "a1", "status": "404"},
        {"arm": "A", "task": "kcenters", "attempt": "a1", "status": "503"},
    ])
    assert _successful_calls(log, "A", "kcenters") == "0"


def test_a_working_run_counts_its_calls(tmp_path):
    """The falsifier: a checker that always said 0 would pass the test above and break every run."""
    log = _log(tmp_path, [
        {"arm": "A", "task": "convex_hull", "attempt": "a1", "status": "200"},
        {"arm": "A", "task": "convex_hull", "attempt": "a1", "status": "200"},
        {"arm": "A", "task": "convex_hull", "attempt": "a1", "status": "503"},
    ])
    assert _successful_calls(log, "A", "convex_hull") == "2"


def test_a_200_that_carries_an_error_does_not_count(tmp_path):
    """The proxy records a broken pipe as status 200 with an `error`; nothing was delivered."""
    log = _log(tmp_path, [
        {"arm": "A", "task": "x", "attempt": "a1", "status": "200",
         "error": "BrokenPipeError: [Errno 32] Broken pipe"},
    ])
    assert _successful_calls(log, "A", "x") == "0"


def test_other_attempts_of_the_same_task_are_not_borrowed(tmp_path):
    """A re-run must not inherit the previous attempt's calls as evidence that IT worked."""
    log = _log(tmp_path, [
        {"arm": "A", "task": "x", "attempt": "a1", "status": "200"},
        {"arm": "A", "task": "x", "attempt": "a2", "status": "404"},
    ])
    assert _successful_calls(log, "A", "x", attempt="a2") == "0"


def test_an_unknowable_answer_is_empty_not_zero(tmp_path):
    """"" and "0" are different answers, and only "0" withholds a marker.

    A missing log, or one with no rows for this arm, means the bookkeeping is absent — not that the
    run failed. Refusing markers on that would punish runs for a gap they did not cause.
    """
    log = _log(tmp_path, [{"arm": "B", "task": "x", "attempt": "a1", "status": "200"}])
    assert _successful_calls(log, "A", "x") == ""          # no rows for arm A at all
    assert _successful_calls(tmp_path / "missing.jsonl", "A", "x") == ""
    assert _successful_calls(log, "A", "x", set_env=False) == ""


def test_rc0_consults_the_check_before_writing_the_marker():
    """And the shipped `record_done` must actually use it on the rc=0 path."""
    src = CAMPAIGN.read_text(encoding="utf-8")
    case = src.index('  case "$RC" in')
    rc0 = src.index("    0)", case)
    rc124 = src.index("    124)", rc0)
    # COMMENTS ARE STRIPPED FIRST. The first version compared raw offsets and failed on a correct
    # branch, because the comment explaining the fix names `ran_to_completion` above the check.
    branch = [ln for ln in src[rc0:rc124].splitlines() if not ln.lstrip().startswith("#")]
    code = "\n".join(branch)
    assert "successful_calls" in code, "rc=0 no longer checks for a successful call"
    assert code.index("successful_calls") < code.index("ran_to_completion"), \
        "the check must come before the marker is written"


# ---------------------------------------------------------------------------
# The other half of the same defect, measured 2026-08-25 when the gateway fell a SECOND time.
#
# `successful_calls` asks only whether the run ever paid for anything. That catches a total outage
# — attempt 1 of arm A, sixteen task-arms with zero calls each. It does NOT catch a run that HAD
# spent money and was then cut mid-search: four task-arms exited 0 while the endpoint was returning
# `503 No available workers` and earned `ran_to_completion` markers over runs that had used 15 %,
# 27 %, 37 % and 69 % of their $1.00 — against arm B, which spent the whole of it. Their reported
# numbers are real AND are measurements of a truncated search, which is the one thing a marker must
# not hide: `.done` means "do not re-run this".
#
# The discriminator is the LAST metered row of the attempt: a run that ends on its own terms ends
# after a call that worked. On the live log the one task-arm that reached its ceiling
# (`edge_expansion`, 107 % spent) has a 200 last; all four cut ones have a 503.


def _ended_on_failure(meter_log, arm, task, attempt="a1", set_env=True) -> str:
    src = CAMPAIGN.read_text(encoding="utf-8")
    m = re.search(r"^ended_on_failure\(\)\s*\{.*?^\}", src, re.S | re.M)
    assert m, "ended_on_failure() is gone from campaign.sh — this test needs rewriting"
    env_line = f'METER_LOG={meter_log}\n' if set_env else "unset METER_LOG\n"
    script = env_line + m.group(0) + f'\nended_on_failure {arm} {task} {attempt}\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_a_run_cut_by_the_endpoint_is_not_complete(tmp_path):
    """Spent money, then the endpoint died: the shape that earned four false markers."""
    log = _log(tmp_path, [
        {"arm": "A", "task": "discrete_log", "attempt": "a1", "status": "200"},
        {"arm": "A", "task": "discrete_log", "attempt": "a1", "status": "200"},
        {"arm": "A", "task": "discrete_log", "attempt": "a1", "status": "503"},
    ])
    assert _ended_on_failure(log, "A", "discrete_log") == "yes"


def test_a_run_that_ended_on_a_working_call_is_complete(tmp_path):
    """The falsifier: a check that always said "yes" would pass the test above and refuse every
    marker the campaign ever earns, including the one task-arm that really did reach its ceiling."""
    log = _log(tmp_path, [
        {"arm": "A", "task": "edge_expansion", "attempt": "a1", "status": "503"},
        {"arm": "A", "task": "edge_expansion", "attempt": "a1", "status": "200"},
    ])
    assert _ended_on_failure(log, "A", "edge_expansion") == "no"


def test_a_trailing_broken_pipe_also_counts_as_a_failure(tmp_path):
    """The proxy records a broken pipe as status 200 with an `error`; nothing was delivered, so a
    run whose last row is one did not end on a call that worked."""
    log = _log(tmp_path, [
        {"arm": "A", "task": "x", "attempt": "a1", "status": "200"},
        {"arm": "A", "task": "x", "attempt": "a1", "status": "200",
         "error": "BrokenPipeError: [Errno 32] Broken pipe"},
    ])
    assert _ended_on_failure(log, "A", "x") == "yes"


def test_no_rows_for_this_attempt_is_unknowable(tmp_path):
    """"" is not "yes": a run with no rows of its own gets no verdict from this check, and the
    zero-calls check above is what speaks for it instead."""
    log = _log(tmp_path, [{"arm": "A", "task": "x", "attempt": "a1", "status": "200"}])
    assert _ended_on_failure(log, "A", "x", attempt="a2") == ""
    assert _ended_on_failure(log, "B", "x") == ""


def test_rc0_consults_the_failure_check_before_writing_the_marker():
    src = CAMPAIGN.read_text(encoding="utf-8")
    case = src.index('  case "$RC" in')
    rc0 = src.index("    0)", case)
    rc124 = src.index("    124)", rc0)
    code = "\n".join(ln for ln in src[rc0:rc124].splitlines() if not ln.lstrip().startswith("#"))
    assert "ended_on_failure" in code, "rc=0 no longer checks whether the endpoint cut the run"
    assert code.index("ended_on_failure") < code.index("ran_to_completion"), \
        "the check must come before the marker is written"
