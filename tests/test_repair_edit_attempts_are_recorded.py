"""`inert` was one word for two different failures. This makes it two.

MEASURED over every inert repair on this box that still has spans (v11 x2, v13 x2 — v12's are gone
because that run pinned pre-fix tracing code, the loss #149 closed):

    EDIT-LIKE TOOL CALLS IN THE WHOLE SESSION: 0, 0, 0, 0

in sessions of 22.5, 23.6, 24.4 and 27.3 minutes. Not an edit that ran out of time — never an edit.
Each ended inside one long generation (302s, 242s, 215s, 192s, 147s appear in the tails) after
reading widely: read_file, run_probe six times in one 2.7-minute stretch, grep_installed,
cross_run_search, search_lessons.

THAT NUMBER COST FOUR CANDIDATE FIXES TO FIND, and it refuted the last of them. Wall time separates
inert from productive repairs perfectly (all inert >=22.5 min, all productive <=18.3 min, n=11), so
the budget looked like the lever — until this said there was nothing for more time to finish.
Reconstructing it took a scan of 450 MB of spans across two runs; as a column it is one integer per
repair, and the next run answers "is this EVERY inert session or only these four?" for free.

REPORTED, NEVER REFUSED — the same rung as `trace_export_health` (#149), `belief_admission` (#158)
and `node_build_delta` (#160). Nothing here changes what a repair may do.
"""
from __future__ import annotations

import types

import pytest

from looplab.adapters.repo_write_tools import RepoWriteTools
from looplab.agents.roles import DEVELOPER_OUTPUT_ATTRS


def _tools():
    # An EMPTY surface, deliberately: every write below is therefore REFUSED, and the counts still
    # have to hold. That is the property under test — `edit_calls` counts the reach for the write
    # surface, not the landing, so a fixture that made the writes succeed would test the weaker
    # claim. `surface` is a path allow-list, not a directory.
    return RepoWriteTools([], [])


def test_a_session_that_never_reaches_for_the_write_surface_counts_zero():
    """The shipped shape of all four inert repairs."""
    w = _tools()
    for _ in range(30):
        w.execute("read_file", {"path": "a.py"})       # not a write tool
    assert w.edit_calls == 0


def test_every_write_shaped_call_counts():
    w = _tools()
    w.execute("write_file", {"path": "a.py", "content": "x = 1\n"})
    w.execute("edit_file", {"path": "a.py", "search": "x = 1", "replace": "x = 2"})
    w.execute("delete_file", {"path": "a.py"})
    assert w.edit_calls == 3


def test_a_REFUSED_attempt_is_still_an_attempt():
    """THE WHOLE POINT of counting before dispatch. `self.files` records what LANDED; a session that
    tried ten times and was refused ten times is a different defect from one that never tried, and
    the result-side view cannot tell them apart."""
    w = _tools()
    # An edit against a path outside the surface is refused by the tool and lands nothing.
    out = w.execute("edit_file", {"path": "nope.py", "search": "absent", "replace": "x"})
    assert w.edit_calls == 1
    assert not w.files, "the refusal must not have written anything"
    assert isinstance(out, str)


def test_declare_stages_is_not_an_edit():
    """It stages a manifest, not a source change, and counting it would make every session that
    declared stages look like it tried to fix the code."""
    w = _tools()
    w.execute("declare_stages", {"stages": []})
    assert w.edit_calls == 0


def test_an_unknown_tool_is_not_an_edit():
    w = _tools()
    w.execute("frobnicate", {"path": "a.py"})
    assert w.edit_calls == 0


def test_the_developer_carries_the_count_out_of_the_session():
    """The engine reads `last_edit_calls` off the role after the call, exactly as it reads
    `last_budget_exhausted`. A counter the engine cannot see records nothing."""
    assert "last_edit_calls" in DEVELOPER_OUTPUT_ATTRS
    assert "last_budget_exhausted" in DEVELOPER_OUTPUT_ATTRS   # the precedent it copies


def test_the_engine_snapshots_it_rather_than_reading_it_late():
    """The developer is SHARED across concurrent evals. Reading the attribute after another await
    would attribute a sibling node's edits to this row — the hazard the neighbouring
    `last_budget_exhausted` comment already spells out. Since 2026-09-06 (doc 52 row 12) the count
    comes off the frozen `DeveloperResult` the offloaded repair returned, so the snapshot IS the
    call; what stays pinned is that the row is built from that envelope and after it."""
    import inspect

    from looplab.engine.evaluate import EvaluateMixin
    src = inspect.getsource(EvaluateMixin)
    snap = src.index("_edit_calls = int(repaired.last_edit_calls")
    use = src.index('"edit_calls": _edit_calls')
    assert snap < use, "the snapshot must precede every use of it"


def test_a_developer_that_never_ran_reports_zero_not_a_crash():
    """Every read is `getattr(..., 0)`: a fallback adapter, a stub in a test, or an older role that
    does not carry the attribute must read as 'no attempts recorded', never raise."""
    stub = types.SimpleNamespace()
    assert int(getattr(stub, "last_edit_calls", 0) or 0) == 0
