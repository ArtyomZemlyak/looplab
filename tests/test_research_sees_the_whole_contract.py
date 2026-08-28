"""The phase that decides WHAT to try was the one phase not told which tools exist.

`state_brief` cut the goal to its first 800 characters. That is not a short goal — it is a goal
whose last section was deleted, and the last section is where an operator states the environment.

MEASURED 2026-08-28 on the AlgoTune card (13,686 chars). The sentence "the harness runs `setup.py
build_ext --inplace`" reaches `propose` in 26/32 prompts, `plan` in 45/47, `plan_step` in 118/139 —
and `deep_research` in **0 of 23, 0 of 38, 0 of 49 and 0 of 22** across dsBud, dsNew, dsFB3 and the
live dsPyx, because it sits past character 800. The Researcher then wrote, in dsFB3 span
`bdddbc45104b97e3`: *"What about using numba/cython? Not available in this environment presumably
… Pure stdlib is safest."* Cython 3.2.9 is installed and the card says so.

Not a space problem: the whole brief is allowed 32,000 characters and the board rows take the rest.
"""
from __future__ import annotations

from looplab.agents.deep_research import (_STATE_BRIEF_GOAL_CHARS, _STATE_BRIEF_GOAL_TAIL_CHARS,
                                          _goal_excerpt)


def _plain(value, max_chars):
    """`brief_text`'s contract, minus the redaction: truncate to max_chars."""
    return str(value)[:max_chars]


def test_the_end_of_a_long_goal_survives():
    head = "H" * _STATE_BRIEF_GOAL_CHARS
    middle = "M" * 5000
    tail = "the harness runs `setup.py build_ext --inplace` over your whole submission"
    out = _goal_excerpt(head + middle + tail, _plain)
    assert "build_ext --inplace" in out, "the toolchain sentence was deleted again"
    assert out.startswith("H"), "the head must still lead"


def test_the_elision_is_named_and_counted_not_hidden():
    out = _goal_excerpt("A" * 10_000, _plain)
    dropped = 10_000 - _STATE_BRIEF_GOAL_CHARS - _STATE_BRIEF_GOAL_TAIL_CHARS
    assert f"{dropped} chars of the goal elided" in out, out[:200]


def test_a_short_goal_is_untouched():
    goal = "Rewrite solver.py so that Solver.solve returns what is_solution accepts."
    assert _goal_excerpt(goal, _plain) == goal


def test_a_goal_at_the_boundary_is_not_elided():
    goal = "x" * (_STATE_BRIEF_GOAL_CHARS + _STATE_BRIEF_GOAL_TAIL_CHARS)
    assert "elided" not in _goal_excerpt(goal, _plain)


def test_the_excerpt_stays_bounded():
    """An unbounded goal must not become an unbounded prompt row."""
    out = _goal_excerpt("z" * 500_000, _plain)
    assert len(out) < _STATE_BRIEF_GOAL_CHARS + _STATE_BRIEF_GOAL_TAIL_CHARS + 120


def test_a_goal_that_cannot_be_stringified_degrades_instead_of_raising():
    class _Hostile:
        def __str__(self):
            raise RuntimeError("no")
    _goal_excerpt(_Hostile(), _plain)     # must not raise
