"""The card named `@njit` and `.pyx` in one breath, and the models stopped at the cheaper one.

MEASURED over this campaign's own champions, scored on the test split:

    task              .pyx              @njit             plain numpy
    edge_expansion    207.48 (n=3)      27.97 (n=6)       27.80 (n=5)
    kcenters          --                123.38 (n=2)      34.25 (n=2)
    convex_hull       --                11.08 (n=3)       2.03 (n=1)

Six edge_expansion champions stopped at `@njit` and landed WITHIN NOISE of solvers that compiled
nothing at all, while the two that shipped a real `.pyx` scored 207 and 286. On kcenters and
convex_hull the same decorator was worth 3.6x and 5.5x. So which one wins is a property of the
task, and a card that offers them as one option lets a model take the cheap branch and never find
out which case it is in.

The clause does NOT tell the model which to use -- that would be a guess about the task the card
has no business making. It states the asymmetry and names the free test that distinguishes them:
apply njit, measure, and if the number did not move, go to an extension rather than tune the
decorator.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "algotune"))
from make_task import toolchain_clause  # noqa: E402

_ROOT = Path("/var/tmp/looplab-bench/AlgoTune")
_INTERP = _ROOT / ".venv" / "bin" / "python"


def _clause() -> str:
    if not _INTERP.exists():                       # off the bench box: the clause text is still the unit
        import pytest
        pytest.skip("arena interpreter not present")
    return toolchain_clause(_ROOT, str(_INTERP))


def test_the_card_says_the_two_compilers_are_not_equivalent():
    text = _clause()
    assert "NOT EQUIVALENT" in text, "the asymmetry has to be stated, not implied"
    assert "property of the task" in text


def test_it_carries_the_numbers_rather_than_an_opinion():
    text = _clause()
    for figure in ("27.97", "27.80", "207", "286", "3.6x", "5.5x"):
        assert figure in text, f"the measured figure {figure} is missing -- this clause is evidence"


def test_it_names_the_action_and_not_just_the_fact():
    """A fact with no next move is a fact the model cannot act on."""
    text = _clause()
    assert "MEASUREMENT, not a destination" in text
    assert re.search(r"if the number did not move.*?\.pyx", text, re.S | re.I), text[-600:]


def test_it_does_not_prescribe_a_technique_for_a_task_it_cannot_see():
    """The card is built per task but this clause is generic; naming a winner would be a guess."""
    text = _clause()
    lowered = text.lower()
    for overreach in ("always use cython", "never use numba", "numba is useless",
                      "do not use numba", "always write a .pyx"):
        assert overreach not in lowered, f"the card must not prescribe: found {overreach!r}"


def test_the_permission_it_qualifies_is_still_there():
    """The asymmetry must not be read as a ban -- the whole point of f4e48bda was that nothing here
    forbids compiling."""
    text = _clause()
    assert "nothing here forbids it" in text
    assert "build_ext --inplace" in text


# ------------------------------------------------- companion files, added 2026-08-28 after dsN3
def test_the_card_says_plainly_that_every_written_file_is_submitted():
    """The omission cost real work, so the claim is pinned rather than left to inference.

    MEASURED on dsN3 node 1: `solver.py` embeds ~90 lines of Cython source in a string and compiles
    it at run time, and its own comment gives the reason -- "embedded so the build works even if the
    evaluation copies only solver.py into the run directory". That is false: `looplab_eval.py`
    submits every file the node wrote and `--solver-file-only` is the opt-OUT, off by default. The
    model hedged against a limitation that does not exist and paid with a compile inside the timed
    call -- the same fragility that took ds3 from 156.43 train to 0.0 test.
    """
    text = _clause()
    assert "EVERY FILE YOU WRITE IS SUBMITTED" in text
    assert "`setup.py`" in text and "`.pyx`" in text
    assert "compile anything at run time" in text, "the wrong move must be named, not only the right one"


def test_it_gives_the_reason_and_not_only_the_instruction():
    text = _clause()
    assert "inside the timed call" in text and "fail scoring" in text
