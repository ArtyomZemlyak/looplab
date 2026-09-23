"""The research EPISODE: what a run has settled across memos, and why it is a projection.

Doc 28 DR-01's acceptance gate is "replay yields identical episode state across every event splice;
resume never repeats a settled question". The first half is met BY CONSTRUCTION here — `episode()`
is a pure function of a folded `RunState` — and this file drives it rather than asserting it: the
same rows in a different order must project the same episode.
"""
from __future__ import annotations

import ast
import inspect

from looplab.core.models import Card, CardSelectionProvenance, RunState
from looplab.events import research_episode as re_mod
from looplab.events.research_episode import (MAX_QUESTIONS, episode, episode_digest_inputs)


def _card(card_id: str, seed: str, evidence=()) -> Card:
    return Card(id=card_id, statement=seed, seed_statement=seed, evidence=list(evidence),
                selection_provenance=CardSelectionProvenance(action_source="card_added",
                                                             action_owner_count=1))


def _state(memos=(), cards=(), attempts=0, plan=None, evidence=None, literature=()) -> RunState:
    st = RunState(goal="g", direction="max")
    st.research = list(memos)
    st.cards = {c.id: c for c in cards}
    st.research_attempts = [{"attempt_id": f"a{i}"} for i in range(attempts)]
    st.research_plan = plan
    st.research_evidence = evidence or {}
    st.literature = list(literature)
    return st


# ---------------------------------------------------------------------------------------------
# The settled / open split is the board's fact, not a new judgement.

def test_a_question_whose_card_carries_evidence_is_SETTLED():
    memos = [{"at_node": 0, "open_questions": ["does distilling help"]}]
    cards = [_card("card-1", "does distilling help", evidence=[3, 7])]
    ep = episode(_state(memos=memos, cards=cards))
    assert ep.settled == ("does distilling help",)
    assert ep.open == ()
    assert ep.questions[0].evidence == (3, 7)
    assert ep.questions[0].card_id == "card-1"


def test_a_question_with_a_card_but_NO_evidence_is_still_OPEN():
    """An experiment in flight is not an answer. This is the same population
    `open_research_beliefs()` keeps, and reading `status` instead would call a running node a
    settled question."""
    memos = [{"at_node": 0, "open_questions": ["does distilling help"]}]
    cards = [_card("card-1", "does distilling help")]
    ep = episode(_state(memos=memos, cards=cards))
    assert ep.open == ("does distilling help",) and ep.settled == ()


def test_a_question_NOBODY_CARDED_is_open_and_says_how_often_it_was_RAISED():
    """The re-proposal signal: a question three memos asked and no card ever took up is the thing
    the duplicate rules leave visible and nothing counted."""
    memos = [{"at_node": 0, "open_questions": ["widen the negative pool"]},
             {"at_node": 1, "open_questions": ["  WIDEN the  negative pool "]},
             {"at_node": 2, "open_questions": ["widen the negative pool", "a second question"]}]
    ep = episode(_state(memos=memos))
    assert ep.questions[0].memos == 3, "case and whitespace are not semantics"
    assert ep.questions[0].card_id is None
    assert len(ep.open) == 2


# ---------------------------------------------------------------------------------------------
# DR-01's acceptance gate.

def test_the_SAME_ROWS_IN_ANY_ORDER_project_the_same_episode():
    """"Replay yields identical episode state across every event splice" — driven.

    MUTATION: make the question order depend on dict insertion instead of the stated sort key and
    this goes red.
    """
    memos = [{"at_node": 0, "open_questions": ["q one", "q two"]},
             {"at_node": 1, "open_questions": ["q two", "q three"]},
             {"at_node": 2, "open_questions": ["q one"]}]
    cards = [_card("card-1", "q two", evidence=[4])]
    forward = episode(_state(memos=memos, cards=cards))
    reversed_rows = episode(_state(memos=list(reversed(memos)), cards=cards))
    assert forward.questions == reversed_rows.questions
    assert forward.settled == reversed_rows.settled and forward.open == reversed_rows.open


def test_the_episode_names_a_KILL_between_the_attempt_receipt_and_the_memo():
    """`attempts > memos` is a hard kill between the receipt and the memo — DR-05's durability gap.
    One count would hide it; `unfulfilled` names it."""
    ep = episode(_state(memos=[{"at_node": 0, "open_questions": ["q"]}], attempts=3))
    assert (ep.memos, ep.attempts, ep.unfulfilled) == (1, 3, 2)


def test_unfulfilled_is_never_NEGATIVE():
    """An older log whose memo path wrote no attempt receipt must not report a negative kill count,
    which reads as a corrupt record rather than as the missing receipt it is."""
    ep = episode(_state(memos=[{"at_node": 0, "open_questions": ["q"]}] * 3, attempts=0))
    assert ep.unfulfilled == 0


# ---------------------------------------------------------------------------------------------
# Additive tolerance: every older log shape projects rather than raising.

def test_a_run_with_NO_RESEARCH_projects_an_empty_episode():
    ep = episode(RunState(goal="g", direction="max"))
    assert (ep.memos, ep.attempts, ep.questions, ep.plan) == (0, 0, (), None)
    assert ep.rounds_used == 0


def test_a_PRE_PLAN_log_projects_none_rather_than_raising():
    """`runs/e5small-dr-unified-v13`'s seven memo rows carry no plan, no evidence and no literature
    — the shape every log written before doc 52 row 16 has (invariant #5)."""
    ep = episode(_state(memos=[{"at_node": 0, "open_questions": ["q"], "claims": []}], attempts=1))
    assert ep.plan is None and ep.evidence_ids == () and ep.literature_ids == ()
    assert ep.todos_open == ()


def test_junk_rows_are_skipped_and_never_raise():
    st = _state(memos=["not a dict", None, {"open_questions": "not a list"},
                       {"open_questions": ["real"]}])
    ep = episode(st)
    assert [q.statement for q in ep.questions] == ["real"]


def test_the_memo_FALLS_BACK_to_recommended_directions_but_never_unions_them():
    """`_record_research_steering` registers `open_questions` when the memo drew the split and the
    union only as a fallback — so an episode that read both would credit the run with
    `next_experiments` entries that never became board rows (measured on v11: 4/3/2 registered
    against a 10/11/7 union)."""
    split = episode(_state(memos=[{"open_questions": ["registered"],
                                   "recommended_directions": ["registered", "not registered"]}]))
    assert [q.statement for q in split.questions] == ["registered"]
    no_split = episode(_state(memos=[{"recommended_directions": ["the whole list"]}]))
    assert [q.statement for q in no_split.questions] == ["the whole list"]


# ---------------------------------------------------------------------------------------------
# The bound, and the layering rule.

def test_the_question_list_is_BOUNDED_and_the_overflow_is_COUNTED():
    memos = [{"at_node": 0, "open_questions": [f"question {i}" for i in range(MAX_QUESTIONS + 5)]}]
    ep = episode(_state(memos=memos))
    assert len(ep.questions) == MAX_QUESTIONS
    assert ep.omitted == 5, "an overflow must be counted, never silently dropped"


def test_the_open_todos_drop_the_DONE_ones():
    plan = {"plan": "p", "todos": [{"item": "read the loss", "status": "done"},
                                   {"item": "mine negatives", "status": "in_progress"},
                                   {"item": "", "status": "open"}]}
    ep = episode(_state(memos=[{"open_questions": ["q"]}], plan=plan))
    assert ep.todos_open == ("mine negatives",)


def test_the_digest_inputs_EXCLUDE_settled_questions():
    """A round that settles nothing new but re-states what is already answered has made no
    progress; including the settled set would make that round look like movement."""
    memos = [{"at_node": 0, "open_questions": ["answered", "still open"]}]
    cards = [_card("card-1", "answered", evidence=[1])]
    open_gaps, evidence_ids = episode_digest_inputs(episode(_state(memos=memos, cards=cards)))
    assert open_gaps == ("still open",) and evidence_ids == ()


def test_this_module_imports_NOTHING_ABOVE_CORE():
    """CLAUDE.md layering: `events` may import only `core`. The first version of this module
    reached into `trust` for one hash — as a DEFERRED import, which the rule still forbids — so the
    convenience wrapper was replaced by `episode_digest_inputs`, which returns the two lists and
    lets the caller compose them.

    AST, so the docstrings that NAME `trust/verifier_routing.py` cannot satisfy or break it.
    """
    tree = ast.parse(inspect.getsource(re_mod))
    reached = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("looplab"):
            reached.add(node.module)
        elif isinstance(node, ast.Import):
            reached.update(alias.name for alias in node.names
                           if alias.name.startswith("looplab"))
    allowed = {"looplab.core", "looplab.events"}
    for module in reached:
        assert any(module == base or module.startswith(base + ".") for base in allowed), (
            f"`events` may import only `core`; this reaches {module}")
