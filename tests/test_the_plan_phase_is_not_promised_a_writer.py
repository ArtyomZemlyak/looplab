"""The `plan` phase has no writer and its system prompt opens by promising one.

MEASURED over the 76-run probe corpus on 2026-09-03: `write_file` is called 51 times from the
`plan` phase and **all 51 error**, against 716 calls from `plan_step`, which does have the tool.
504 of the 528 `plan` chain-roots (95.5 %) carry a system prompt that names `write_file`. The user
message directly under it says "you CANNOT write code yet"; the system prompt wins.

`read_only_intro` is the repair. It was held back while §115's arm ran and wired into
`_propose_plan` on 2026-09-04, once that arm closed (§180); the test that guarded the held-back
state was deleted in the same commit, as its own docstring required. What remains pins the two ways
the rewrite could go wrong: hitting text it does not own, and leaving the tool names behind.
"""
from __future__ import annotations

import inspect

from looplab.adapters import repo_developer as rd


def test_the_promise_is_replaced_and_the_tools_are_no_longer_offered():
    got = rd.read_only_intro(rd._REPO_DEV_SYSTEM_INTRO)
    assert got != rd._REPO_DEV_SYSTEM_INTRO, "the anchor sentence drifted; the rewrite is now a no-op"
    assert "WRITING code with the write_file and edit_file tools" not in got
    assert "write_file and edit_file are not available to you" in got
    # Everything after the opening promise is the role's actual job and must survive intact.
    assert "YOU decide how to realise it in code" in got


def test_it_leaves_text_it_does_not_own_alone():
    """An operator can override the intro through the prompt store; a half-rewrite would be worse
    than no rewrite, because the contradiction would then be invisible to this test too."""
    assert rd.read_only_intro("") == ""
    foreign = "You are a careful engineer. Write code with write_file when asked."
    assert rd.read_only_intro(foreign) == foreign


def test_it_rewrites_once_not_everywhere():
    doubled = rd._REPO_DEV_SYSTEM_INTRO * 2
    got = rd.read_only_intro(doubled)
    assert got.count("write_file and edit_file are not available to you") == 1
    assert got.count("WRITING code with the write_file and edit_file tools") == 1


def test_the_plan_phase_actually_uses_it_now():
    """The wiring itself, pinned — one line at the top of `_propose_plan`.

    Until 2026-09-04 the opposite was pinned: a test that failed the day the call site was made, so
    the held-back repair could be neither forgotten nor shipped silently. §180 closed §115's arm at
    twenty-four probes; that test was deleted in the same commit that made this one true.
    """
    src = inspect.getsource(rd.LLMRepoDeveloper._propose_plan)
    assert "read_only_intro(system)" in src, (
        "the plan phase is being handed the writer-promising intro again")
    assert "NOT WIRED IN YET" not in inspect.getsource(rd), (
        "the note says the repair is held back while the call site says otherwise")
