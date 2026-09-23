"""The board no longer halves a memo for space — and the hierarchy is why the bound moved.

MEASURED on `runs/e5small-dr-unified-v13`, seven memos against a cap of 5:

    proposed 24    admitted 10    CAPPED 12    restated 3

Half of everything the run proposed was refused for board space, while only three were genuine
restatements. Memo 7 landed nothing at all.

AND IT COST THE HIERARCHY. Every memo fills `question_parents` aligned with `open_questions`.
Memo 2's THIRD question carried a real parent — `does-widening-the-contrastive-negative-poo`, a
question already on that board — and was CAPPED, while its FIRST (top-level, parent "") was
admitted. Admission is by arrival order inside a memo, so the cap kept opening fresh top-level rows
and refused the single entry that would have nested the tree. The board read flat for the whole run.

The old value was `BOARD_PROMPT_CARDS` (5), justified as "a belief the model cannot see is a belief
it re-proposes in new words". That re-proposal is caught by the DUPLICATE rules, which are separate
from this cap and still apply — they refused 3 of the 24 here.

THE CAP IS NOT GONE; IT IS 20, and the difference is the point of this file. The first cut of this
change set it to a sentinel (100_000_000) meaning "unbounded", which is a bound abandoned rather
than chosen — and three test files size a board as `range(DEEP_RESEARCH_OPEN_BELIEF_CAP)`, so it
also allocated a hundred million Cards (224 GB and 256 GB of RSS, measured, twice). The operator
settled it: "надо чтоб столько сколько нужно карточек добавлялось. ну давай сделаем, что не более
20". 20 is above every memo this box has recorded (the widest is 6 questions), so nothing a real
memo produces is refused — while the board stays a population a test can allocate and a human can
read.
"""
from __future__ import annotations

from looplab.agents.roles import BOARD_PROMPT_CARDS
from looplab.engine.research_cadence import (
    DEEP_RESEARCH_OPEN_BELIEF_CAP, classify_research_beliefs)


def test_a_memo_the_old_cap_would_have_halved_is_admitted_whole():
    """v13 memo 2's shape: three proposals against a board already holding five."""
    board = ["already open %d" % i for i in range(5)]
    verdict = classify_research_beliefs(board, ["q one", "q two", "q three"])
    assert verdict.capped == 0
    assert len(verdict.admitted) == 3


def test_the_nested_entry_is_no_longer_the_one_that_falls_off():
    """The exact v13 loss: the parented question sat THIRD in its memo and was capped while the
    top-level first was admitted. With room for the whole memo, arrival order stops deciding what
    the tree keeps."""
    board = ["already open %d" % i for i in range(5)]
    memo = ["a fresh top-level question", "another top-level one",
            "the one whose parent is already on the board"]
    admitted = classify_research_beliefs(board, memo).admitted
    assert memo[2] in admitted


def test_the_v13_run_lands_TWICE_what_it_did_and_the_bound_still_bites_at_the_end():
    """The run's real volume — seven memos, 24 questions — replayed against the shipped cap.

    This is the honest accounting and not a "nothing is refused" claim: 20 of the 24 land (against
    10 before), and the last four are refused by a bound that has been CHOSEN. The memos that were
    being halved — every one of the first five — now land whole, which is the defect this fixed.
    """
    board: list[str] = []
    capped = 0
    per_memo = []
    for memo_no, memo_size in enumerate((5, 3, 6, 2, 3, 3, 2)):
        # keyed by MEMO, not by size: three of v13's memos held three questions each, and naming
        # them by size made the second and third exact duplicates of the first — refused by the
        # duplicate rule, which is correct behaviour and not what this test is about.
        memo = ["memo%d question %d" % (memo_no, i) for i in range(memo_size)]
        verdict = classify_research_beliefs(board, memo)
        capped += verdict.capped
        per_memo.append(len(verdict.admitted))
        board.extend(verdict.admitted)
    assert len(board) == DEEP_RESEARCH_OPEN_BELIEF_CAP == 20
    assert capped == 4, "24 proposed, 20 admitted — the bound bites once, at the end"
    assert per_memo[:5] == [5, 3, 6, 2, 3], "every memo the old cap halved now lands whole"


def test_the_DUPLICATE_rules_still_refuse():
    """Raising the SPACE bound must not touch the SAMENESS refusal — that is what actually
    defends against the re-proposal the old cap was justified by."""
    verdict = classify_research_beliefs(["does distilling help"], ["Does distilling help"])
    assert not verdict.admitted
    assert verdict.restated == 1


def test_a_blank_is_still_refused():
    verdict = classify_research_beliefs([], ["", "   ", "a real one"])
    assert verdict.blank == 2
    assert verdict.admitted == ["a real one"]


def test_the_cap_is_still_a_KNOB():
    """A caller that wants a tighter board still gets one; only the shipped value changed."""
    verdict = classify_research_beliefs([], ["a", "b", "c"], cap=2)
    assert verdict.capped == 1
    assert len(verdict.admitted) == 2


def test_no_ADMITTED_belief_can_be_invisible_to_the_prompt():
    """The cap and the window must not come apart — and the FIRST cut of this change got the
    lesson backwards, which is why this test is written the way it is.

    The original code read `DEEP_RESEARCH_OPEN_BELIEF_CAP = BOARD_PROMPT_CARDS` with the rule "a
    belief the model cannot see is a belief it re-proposes in new words". That rule is RIGHT; the
    value (5) was wrong. The first version of this file pinned the DECOUPLING as the fix
    (`assert CAP != BOARD_PROMPT_CARDS`), which made a board of 20 legal while the prompt still
    showed 5 — fifteen beliefs a run pays to register and no prompt ever shows. So what is pinned
    now is the INVARIANT rather than either value:

        cap <= window   ->   every admitted belief fits the window that shows it.

    Equality is the shipped case and is asserted separately below, because a cap SMALLER than the
    window is not a defect (it merely admits less than it could) while a cap LARGER than the window
    is exactly the failure above.
    """
    assert DEEP_RESEARCH_OPEN_BELIEF_CAP <= BOARD_PROMPT_CARDS, (
        f"the board admits {DEEP_RESEARCH_OPEN_BELIEF_CAP} beliefs and the prompt shows "
        f"{BOARD_PROMPT_CARDS} whole rows, so "
        f"{DEEP_RESEARCH_OPEN_BELIEF_CAP - BOARD_PROMPT_CARDS} of them are invisible by construction")


def test_the_cap_is_the_window_by_DERIVATION_and_not_by_coincidence():
    """One spelling, so raising the window cannot leave the cap silently behind.

    AST, not a substring: the module's own comment quotes the assignment it pins, and a substring
    check on that text is satisfied by the prose — the defect this file already carries a worked
    example of one test down.
    """
    import ast
    import inspect

    from looplab.engine import research_cadence
    tree = ast.parse(inspect.getsource(research_cadence))
    bound = [node.value for node in ast.walk(tree)
             if isinstance(node, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "DEEP_RESEARCH_OPEN_BELIEF_CAP"
                     for t in node.targets)]
    assert len(bound) == 1, f"the cap must be assigned exactly once; found {len(bound)}"
    assert isinstance(bound[0], ast.Name) and bound[0].id == "BOARD_PROMPT_CARDS", (
        "the cap must READ the window constant, never repeat its value — a literal here is "
        "behaviourally identical today and invisible to every behavioural test, which is how the "
        "two came apart in the first place")


def test_the_cap_stays_small_enough_to_size_a_board_by():
    """Three test files build `range(DEEP_RESEARCH_OPEN_BELIEF_CAP)` Cards, so the cap is an
    ALLOCATION in this suite. Guarded here as well as in those files because this is where the
    value lives: at the sentinel 100_000_000 two of them reached 224 GB and 256 GB of RSS."""
    assert 1 <= DEEP_RESEARCH_OPEN_BELIEF_CAP <= 1_000


def test_the_default_still_READS_the_knob_rather_than_repeating_its_value():
    """One spelling, so the knob and the signatures cannot drift apart.

    A literal in the signature is behaviourally identical today and therefore invisible to every
    behavioural test — that mutant SURVIVED the whole suite. It is still wrong: re-bounding the
    board would then change the constant and leave two callers on the old number, which is the
    drift the removed comment's own "DERIVED, not copied" paragraph was written about.

    AST AND NOT A SUBSTRING, and this test is its own worked example. It counted
    `src.count("cap: int = DEEP_RESEARCH_OPEN_BELIEF_CAP") == 2` and went RED at 3 the moment a
    COMMENT in that module quoted the signature it pins — the satisfiable-by-a-comment defect
    CLAUDE.md's guard-test ladder names, firing in both directions at once: prose could have made a
    deleted default look present, and prose did make a present one look duplicated. Comments are
    not AST nodes.
    """
    import ast
    import inspect

    from looplab.engine import research_cadence
    src = inspect.getsource(research_cadence)
    reads_the_knob, repeats_a_value = [], []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            if arg.arg != "cap" or default is None:
                continue
            if isinstance(default, ast.Name) and default.id == "DEEP_RESEARCH_OPEN_BELIEF_CAP":
                reads_the_knob.append(node.name)
            else:
                repeats_a_value.append(node.name)
    assert sorted(reads_the_knob) == ["admit_research_beliefs", "classify_research_beliefs"], (
        "both admission entry points must read the knob, never a copy of its value")
    assert repeats_a_value == [], (
        f"these bind a cap default that is not the knob: {repeats_a_value}")
