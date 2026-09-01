"""A session bound that cannot be observed is a bound nobody can trust.

Measured 2026-08-31 over all 22 run trees on this box: the field carrying WHICH bound ended a
session appears **zero** times. `_note_session_budget` stores the kind on the developer, and the only
place it is ever snapshotted into a durable row is the node-REPAIR path in `engine/evaluate.py`. A
plan step is not a repair, so a step cut by turns, by wall clock or by money left no trace anywhere.

That became urgent the same day a MONEY ceiling was added to those sessions: the ceiling could fire
on every run and the evidence would look identical to it never firing. It did look identical -- the
first search for it found nothing and meant nothing, because the instrument could not have seen it.

So the plan-step attribution row carries `cutoff`, and the `plan_steps` span carries `cut_steps`.
"""
import pytest

from looplab.adapters.repo_developer import plan_step_attribution


def _steps(n):
    return [{"title": f"step {i}", "detail": "d"} for i in range(1, n + 1)]


def test_a_cut_step_says_which_bound_cut_it():
    obs = [{"wrote": ["a.py"], "deleted": [], "cutoff": "cost", "error": ""},
           {"wrote": ["b.py"], "deleted": [], "cutoff": "", "error": ""}]
    out = plan_step_attribution(_steps(2), obs, {"a.py": "x", "b.py": "y"})
    rows = {r["step"]: r for r in out["steps"]}
    assert rows[1].get("cutoff") == "cost", (
        "a step cut by the money ceiling is indistinguishable from one that finished: " + str(rows[1])
    )
    assert "cutoff" not in rows[2], "a step that ended on its own terms must not carry a bound"
    assert out["cut_steps"] == [1], f"the summary does not name the cut step: {out['cut_steps']}"


@pytest.mark.parametrize("kind", ["cost", "time", "turns", "stuck", "stalled", "emit_force"])
def test_every_loop_cutoff_kind_survives_into_the_row(kind):
    """The vocabulary is the loop's, not this function's -- it may not filter it down."""
    from looplab.agents.tool_loop import LOOP_CUTOFF_KINDS
    assert kind in LOOP_CUTOFF_KINDS, "premise: this is one of the loop's kinds"
    out = plan_step_attribution(_steps(1), [{"wrote": [], "deleted": [], "cutoff": kind}], {})
    assert out["steps"][0].get("cutoff") == kind


def test_no_cut_anywhere_is_an_empty_list_not_a_missing_key():
    out = plan_step_attribution(_steps(2),
                                [{"wrote": ["a"], "deleted": [], "cutoff": "", "error": ""},
                                 {"wrote": ["b"], "deleted": [], "cutoff": "", "error": ""}],
                                {"a": "1", "b": "2"})
    assert out["cut_steps"] == [], (
        "a reader has to be able to tell 'nothing was cut' from 'this run predates the field'"
    )


def test_a_cut_step_is_still_scored_for_what_it_wrote():
    """Being cut is not being useless: the salvage keeps whatever the session wrote."""
    out = plan_step_attribution(_steps(1),
                                [{"wrote": ["solver.py"], "deleted": [], "cutoff": "cost"}],
                                {"solver.py": "code"})
    row = out["steps"][0]
    assert row["wrote"] == ["solver.py"]
    assert not row.get("noop"), "a cut step that WROTE something must not be recorded as a no-op"
    assert out["authors"] == {"solver.py": 1}, "authorship was lost because the step was cut"


def test_the_developer_clears_the_kind_between_steps():
    """`last_budget_exhausted` is sticky; without a reset one cut step marks every later one."""
    import inspect
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    src = inspect.getsource(LLMRepoDeveloper)
    i = src.index('with tracing.operation("plan_step"')
    window = src[max(0, i - 500):i]
    assert 'self.last_budget_exhausted = ""' in window, (
        "nothing clears the sticky kind immediately before a plan step, so the first cut step "
        "would mark the rest of the plan"
    )


# --- the numbers behind the word ------------------------------------------------------------------
# §85 added the seconds and the spend to a cut session because the corpus recorded only "time" --
# 14 cutoffs across 30 probes, every one of them the wall clock, and NOT ONE saying how close the
# money ceiling came. The repair was verified from `drive_tool_loop` down to `last_budget_facts`
# and no further. As of 2026-09-01 16:30 not a single real span carries the new fields: every probe
# holding a cutoff was launched before the fix, and a running process keeps the module it imported.
# So the last hop -- observed row into the durable span -- was shipped and never observed. That is
# the exact shape of defect this stand keeps finding, and waiting for a probe to prove it is not a
# test.

def test_the_cut_row_carries_the_seconds_and_the_spend():
    obs = [{"wrote": ["a.py"], "deleted": [], "cutoff": "time",
            "cutoff_seconds": 1200.44, "cutoff_detail": "$0.1837 of $0.2500 for this session"}]
    out = plan_step_attribution(_steps(1), obs, {"a.py": "x"})
    row = out["steps"][0]
    assert row["cutoff"] == "time"
    assert row["cutoff_seconds"] == 1200.4, row          # rounded to a tenth, not carried raw
    assert row["cutoff_spend"] == "$0.1837 of $0.2500 for this session", row


def test_an_UNKNOWABLE_spend_leaves_the_key_out_rather_than_writing_null():
    """`None` and `0.0` are different readings: no accountant means the spend cannot be known, and
    a `"cutoff_spend": null` in the durable span would make that look like a step that spent
    nothing. The span is what a later sweep reads; it must not invent a zero."""
    obs = [{"wrote": [], "deleted": [], "cutoff": "time",
            "cutoff_seconds": None, "cutoff_detail": ""}]
    row = plan_step_attribution(_steps(1), obs, {})["steps"][0]
    assert row["cutoff"] == "time"
    assert "cutoff_seconds" not in row, row
    assert "cutoff_spend" not in row, row


def test_a_step_that_was_not_cut_carries_neither_number():
    obs = [{"wrote": ["a.py"], "deleted": [], "cutoff": "",
            "cutoff_seconds": 12.0, "cutoff_detail": "$0.01 for this session, no money ceiling set"}]
    row = plan_step_attribution(_steps(1), obs, {"a.py": "x"})["steps"][0]
    assert "cutoff" not in row and "cutoff_seconds" not in row and "cutoff_spend" not in row, row


def test_a_zero_second_cut_is_recorded_and_not_swallowed_as_falsy():
    """0.0 seconds is a real reading -- a session cut on its first turn -- and `if seconds:` would
    drop it. The guard is `is not None` for exactly this."""
    obs = [{"wrote": [], "deleted": [], "cutoff": "cost",
            "cutoff_seconds": 0.0, "cutoff_detail": "$0.2501 of $0.2500 for this session"}]
    row = plan_step_attribution(_steps(1), obs, {})["steps"][0]
    assert row["cutoff_seconds"] == 0.0, row
    assert row["cutoff_spend"].startswith("$0.2501"), row
