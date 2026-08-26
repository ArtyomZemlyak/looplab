"""The question board as a PULL, and the two wirings that decide who can reach it.

THE GAP, measured 2026-08-26 over the whole tool surface: 83 `fn_spec` tools and not one reads the
run's questions. The concept tools read the TAXONOMY, `read_research_memo` reads the memo that
PRODUCED a question, and `agents/roles.py::_state_brief` PUSHES the open directions to whichever
roles the engine pushes it to. Nobody could ask.

WHO WAS BLIND is the part that shaped the fix: `RunTools` is built for the RESEARCHER only, and the
Developer's `_scout_tools` has no reader for the board at all — the `read_run_experiment` calls in
its `stages`/`plan`/`card_build` phases are a FOREIGN-run reader. So the role writing an
experiment's code could not see the question it answers.

Every assertion below has an input that makes it fail; the mutations are named in the messages.

WHY THESE TESTS DID NOT CATCH THE CONTRACT BREAK, recorded because it is the reusable part: every one
of them called `.execute(...)` — the name the provider happened to define — so they confirmed MY OWN
NAMING rather than the contract's. `tools/_base.py::ToolProvider` requires `execute`, the protocol is
STRUCTURAL ("no provider inherits this"), and the mismatch surfaced only when a live agent
dispatched: the first run to load this provider lost its entire deep-research stage to
"(deep research unavailable: 'QuestionBoardTools' object has no attribute 'execute')".
A test that drives the method the object defines can never find that. `test_tool_provider_contract.py`
is the guard that can.
"""
from __future__ import annotations

from looplab.core.cards import Card, CardSelectionProvenance
from looplab.core.models import RunState
from looplab.tools.question_board import QuestionBoardTools


def _question(cid: str, *, statement: str = "", concepts=()) -> Card:
    """A direction: `action_source` "none" means no action owner, which is what makes it a question."""
    return Card(id=cid, statement=cid, seed_statement=statement or f"Does {cid} help?",
                concept_tags=list(concepts),
                selection_provenance=CardSelectionProvenance(action_source="none"))


def _experiment(cid: str, parent: str, *, delta=None, status="evaluated", verdict="supported") -> Card:
    card = Card(id=cid, statement=cid, seed_statement=f"try {cid}", parent_card_id=parent,
                status=status, verdict=verdict,
                selection_provenance=CardSelectionProvenance(
                    action_source="card_added", action_owner_count=1))
    card.best_delta = delta
    return card


def _bound(*cards: Card) -> QuestionBoardTools:
    state = RunState(run_id="r", task_id="t", direction="max")
    state.cards = {card.id: card for card in cards}
    tool = QuestionBoardTools()
    tool.bind_state(state)
    return tool


def test_a_question_lists_its_experiments_and_what_they_measured():
    out = _bound(
        _question("q-distill", statement="Does distillation help recall?",
                  concepts=["loss/contrastive"]),
        _experiment("card-0", "q-distill", delta=0.021),
        _experiment("card-1", "q-distill", status="running", verdict="open"),
    ).execute("read_questions", {})

    assert "QUESTION_ID=q-distill" in out
    assert "Does distillation help recall?" in out
    assert "loss/contrastive" in out, "the concepts are the join the ladder renders; they ride here too"
    assert "card-0" in out and "+0.021" in out, (
        "MUTATION: drop the children loop and this goes red — a question with no measured children "
        "shown is the blindness this tool exists to remove")
    assert "card-1" in out and "running" in out, "work in flight is part of 'has this been tried'"


def test_an_UNMEASURED_experiment_reads_as_absent_and_never_as_zero():
    """A run that produced no number and one that produced no improvement are different findings."""
    out = _bound(_question("q1"), _experiment("card-0", "q1", delta=None)).execute("read_questions", {})
    assert "delta=—" in out, "MUTATION: default a missing best_delta to 0.0 and this goes red"
    assert "delta=+0" not in out


def test_a_question_with_NO_experiment_says_so_rather_than_being_omitted():
    """It is the most actionable row on the board — the same choice the operator's ladder makes."""
    out = _bound(_question("q-open")).execute("read_questions", {})
    assert "q-open" in out
    assert "no experiment filed under this yet" in out, (
        "MUTATION: skip childless questions and this goes red — filtering them out hides exactly "
        "the questions that need work")


def test_an_EXPERIMENT_is_never_listed_as_a_question():
    """The kind is read from ACTION OWNERSHIP, the same rule the board, the fold and the UI use."""
    out = _bound(_experiment("card-0", "")).execute("read_questions", {})
    assert "QUESTION_ID" not in out
    assert "no research question registered" in out


def test_an_EMPTY_board_is_reported_as_a_state_and_not_as_an_error():
    """Before the opening memo a healthy run has no questions; saying "none" as though something
    were missing would misreport its first minutes — and v7 spent 90 of them in exactly that state."""
    out = _bound().execute("read_questions", {})
    assert "not produced any" in out
    assert "error" not in out.lower()


def test_asking_for_ONE_question_returns_its_children_in_full():
    """The whole-board view clips a question's children; naming one lifts the clip.

    A direction may hold up to `CARD_CHILD_LIMIT` (256) experiments, so the listing has to choose —
    and the choice must be answerable, not final.
    """
    kids = [_experiment(f"c{i:02d}", "q1", delta=float(i) / 1000) for i in range(12)]
    tool = _bound(_question("q1"), *kids)

    whole = tool.execute("read_questions", {})
    assert "more experiment(s) under this question" in whole, (
        "MUTATION: remove the clip receipt and a partial listing reads as complete")
    assert 'read_questions(question_id="q1")' in whole, (
        "the remedy must be a call the caller has NOT already spent — log_tools rule 3")

    one = tool.execute("read_questions", {"question_id": "q1"})
    assert "c11" in one, "naming the question returns every child"
    assert "more experiment(s)" not in one


def test_an_UNKNOWN_question_id_is_refused_by_name():
    out = _bound(_question("q1")).execute("read_questions", {"question_id": "nope"})
    assert "no question with id" in out and "nope" in out


def test_it_is_TOTAL_over_junk_and_over_an_unbound_provider():
    tool = QuestionBoardTools()
    assert "no run state bound" in tool.execute("read_questions", {})
    assert "unknown tool" in _bound().execute("something_else", {})


def test_BOTH_wirings_exist_and_the_developer_binds_the_attribute_it_actually_has():
    """The two call sites, and the one that nearly shipped inert.

    `_scout_tools` holds the run state on `_memory_state`; binding `_state` (which does not exist)
    would leave the provider answering "no run state bound" on every call. Driven by AST rather than
    a substring — comments are not AST nodes — and by the SPEC surface for the researcher half.
    """
    import ast
    import inspect

    from looplab.adapters import repo_developer
    from looplab.agents import factory

    dev = inspect.getsource(repo_developer.LLMRepoDeveloper._scout_tools)
    tree = ast.parse(dev.strip().replace("\n    ", "\n"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "QuestionBoardTools" in names, "the Developer must build the provider"
    assert "QuestionBoardTools" in inspect.getsource(factory), "the researcher half must exist too"

    # DRIVEN, not pinned. A source check for "_memory_state" is VACUOUS here and I shipped one:
    # that name already appears in this same function for the lessons and cross-run tools, so
    # binding the board from a `_state` that does not exist left the assertion green. The mutation
    # is what caught it. This builds the real provider list and asks the board whether it is bound.
    dev_obj = repo_developer.LLMRepoDeveloper.__new__(repo_developer.LLMRepoDeveloper)
    state = RunState(run_id="r", task_id="t", direction="max")
    state.cards = {"q1": _question("q1")}
    dev_obj._memory_state = state
    try:
        providers = dev_obj._scout_tools(None)
    except Exception as exc:                                  # noqa: BLE001
        raise AssertionError(f"_scout_tools must build with a bare developer: {exc!r}") from exc
    board = next((p for p in providers if isinstance(p, QuestionBoardTools)), None)
    assert board is not None, "the board provider must be in the Developer's scout set"
    assert "QUESTION_ID=q1" in board.execute("read_questions", {}), (
        "MUTATION: bind `_state` instead of `_memory_state` and this answers 'no run state bound'")


def test_the_tool_declares_exactly_one_spec_named_read_questions():
    specs = QuestionBoardTools().specs()
    assert [s["function"]["name"] for s in specs] == ["read_questions"]
    described = specs[0]["function"]["description"]
    assert "not runnable as it stands" in described, (
        "the description must say a question is not an experiment; a role told otherwise will try "
        "to claim one, which is the `action_owner_missing` confusion this vocabulary exists to end")
