"""The agent's own question board says whether a question was ANSWERED.

Every child row on this listing renders `delta=` from the child's `best_delta`, whose baseline is
that child's own PARENT NODE. A question answered by a first-generation DRAFT has no such number,
so the board rendered a row of em-dashes and the Researcher deciding what to propose next read it
as untested. MEASURED on v11: three of the four questions with an evaluated child showed nothing
while their children had measured 0.773951, 0.759164 and 0.718923.

`best_vs_champion` (ed7fba9e) is the champion-relative number the fold already computes on the
parent. Putting it on the question's own line needs no node metrics here and reuses what is
already derived. It is SPELLED differently from the children's `delta=` on purpose: the two have
different baselines and routinely disagree in sign — on v11 the InfoNCE question reads
`best +0.01724` against its parent and `-0.013805` against the champion.
"""
from __future__ import annotations

import math

from looplab.tools.question_board import _champion_verdict


def test_it_names_the_number_and_the_card_that_owns_it():
    """Mutation: drop the owner, or the sign, and the line stops being actionable."""
    line = _champion_verdict({"best_vs_champion": -0.002718,
                              "best_vs_champion_card_id": "card-0"})
    assert line == "answered: best vs champion -0.002718 by card-0", line


def test_a_positive_result_keeps_its_sign():
    """A question that BEAT the champion must not render like one that lost."""
    assert _champion_verdict({"best_vs_champion": 0.0042,
                              "best_vs_champion_card_id": "card-9"}).startswith(
        "answered: best vs champion +0.0042")


def test_absent_is_SILENT_and_never_a_zero():
    """The same rule `_delta` already follows: a question whose experiments produced no number and
    one whose experiments TIED the champion are different findings. A zero would claim the tie."""
    for rollup in (None, {}, {"best_vs_champion": None},
                   {"best_vs_champion": float("nan")},
                   {"best_vs_champion": float("inf")},
                   {"best_vs_champion": "-0.01"},
                   {"children": 2, "evaluated": 1}):
        assert _champion_verdict(rollup) == "", rollup


def test_a_real_tie_IS_rendered():
    """NON-VACUITY of the rule above: an actual 0.0 is a measurement, not an absence, and it must
    survive the filter that drops None/NaN. On v12 the champion's own question reads exactly this."""
    line = _champion_verdict({"best_vs_champion": 0.0, "best_vs_champion_card_id": "card-2"})
    assert line == "answered: best vs champion +0 by card-2", line


def test_the_owner_is_optional_but_the_number_is_not():
    """A rollup that lost its owner id still carries the finding; one that lost the number carries
    nothing worth a line."""
    assert _champion_verdict({"best_vs_champion": -0.5}) == "answered: best vs champion -0.5"
    assert _champion_verdict({"best_vs_champion_card_id": "card-3"}) == ""


def test_the_board_renders_it_on_the_QUESTION_line():
    """Placement is the property: on the question, not on a child. Every child's `delta=` has a
    different baseline, so a champion-relative number rendered there would be read as parent-relative.

    Mutation: move the call inside the `for kid in shown:` loop and this goes red.
    """
    import ast
    import inspect
    from looplab.tools import question_board

    src = inspect.getsource(question_board.QuestionBoardTools)
    tree = ast.parse(src.lstrip())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_champion_verdict"]
    assert len(calls) == 1, f"exactly one verdict call, found {len(calls)}"
    loops = [n for n in ast.walk(tree) if isinstance(n, ast.For)]
    inside_kid_loop = [
        loop for loop in loops
        if getattr(loop.target, "id", None) == "kid"
        for c in ast.walk(loop) if c is calls[0]
    ]
    assert not inside_kid_loop, (
        "the champion verdict belongs on the QUESTION line — under `for kid` it would be read as "
        "the child's own parent-relative delta, which is a different baseline")
