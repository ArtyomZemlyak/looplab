"""What the run tells itself about money, and which phases hear it.

MEASURED 2026-09-06 over eight capped probes, counting `generation` spans whose prompt carries the
`BUDGET:` line:

    deep_research   395/475  83 %      propose            0/538   0 %
    plan_step       279/897  31 %      repropose          0/158   0 %
    plan             44/216  20 %      foresight_rank      0/64   0 %
                                       hyp_prioritize      0/60   0 %

This corrects §278, which concluded from `RESEARCHER_PROMPT_CUES` that no money hint existed at all.
It does exist; it is just built twice, by hand, in two adapters, and reaches neither the phases the
sweep list names nor the two that spend the most.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from looplab.core.costs_text import budget_line  # noqa: E402


def test_it_reports_spend_share_and_remainder():
    got = budget_line(0.25, 1.0)
    assert got.startswith("BUDGET: $0.2500 of $1.0000 spent, $0.7500 left (25 % gone).")


def test_a_run_with_no_ceiling_gets_no_line():
    """A run with no limit has no share to report, and "0 % gone" would be a number it cannot act
    on."""
    assert budget_line(0.25, None) == ""
    assert budget_line(0.25, 0.0) == ""


def test_a_caller_sentence_is_appended_not_replaced():
    got = budget_line(0.5, 1.0, "Size this memo to what is left.")
    assert "50 % gone" in got and got.rstrip().endswith("Size this memo to what is left.")


def test_overspend_reads_as_nothing_left_not_as_negative_money():
    """And as 100 % gone, not 140 %: the two live prompts always clamped the share, and since
    review 2026-09-22 (CORE-12) they ARE this function, so it says what they said."""
    got = budget_line(1.4, 1.0)
    assert "$0.0000 left" in got and "100 % gone" in got, got


def test_junk_is_not_a_line():
    assert budget_line("x", 1.0) == ""          # type: ignore[arg-type]
    assert budget_line(0.5, "y") == ""          # type: ignore[arg-type]


def _historical(spent, limit, tail):
    """The line exactly as both prompt copies built it before CORE-12, kept here as the oracle."""
    return ("BUDGET: ${spent:.4f} of ${limit:.4f} spent, ${remaining:.4f} left ({pct:.0f} % gone). "
            + tail + "\n\n").format(spent=spent, limit=limit, remaining=max(0.0, limit - spent),
                                    pct=min(100.0, 100.0 * spent / limit))


def test_both_prompts_build_the_line_they_always_built():
    """Review 2026-09-22, CORE-12. The line was hand-spelled twice (`deep_research.py`,
    `repo_developer.py`) beside this helper, which nothing in production called. Both now CALL it with
    their own second sentence; this proves the prompt bytes did not move, over a grid that includes
    zero spend, fractional cents, the exact ceiling and overspend. MUTATION: drop the clamp in
    `budget_line` -> the overspend points differ."""
    from looplab.adapters.repo_developer import _REPO_DEV_BUDGET_TAIL
    from looplab.agents.deep_research import _RESEARCH_BUDGET_TAIL

    for tail in (_RESEARCH_BUDGET_TAIL, _REPO_DEV_BUDGET_TAIL):
        for limit in (0.01, 0.5, 1.0, 2.5, 37.0):
            for frac in (0.0, 0.00013, 0.25, 0.5, 0.999, 1.0, 1.0001, 1.4, 3.0):
                spent = limit * frac
                assert budget_line(spent, limit, tail) == _historical(spent, limit, tail), (
                    spent, limit)


def test_the_prompt_builders_call_the_one_helper():
    """AST, not text: each `_budget_note` returns `budget_line(...)` — a comment naming it is not a
    call (CLAUDE.md, "A guard test must not be satisfiable by a COMMENT")."""
    import ast
    import inspect

    from looplab.adapters.repo_developer import LLMRepoDeveloper
    from looplab.agents.deep_research import DeepResearcher

    for owner in (LLMRepoDeveloper, DeepResearcher):
        tree = ast.parse(inspect.getsource(owner._budget_note).lstrip())
        calls = {n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "budget_line" in calls, owner.__name__
