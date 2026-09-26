"""Every reader of a Card's evidence, not only its verdict, treats a SUBSTITUTED build as what it is.

`events/card_ledger.py::_apply_substituted_builds` (2026-09-26) took a node whose Developer reported
`idea_implemented: different` / `not_implemented` out of its card's verdict. The review of that change
found the readers beside the verdict still reading the raw evidence list or the node's proposal
rationale: the skill distiller quoted the substituted node's code as the card's "verified" technique,
the applied-params arbiter published its coordinates as the card's, the belief-grouped board row
dropped the NOT TESTED clause, the lesson writers explained its metric by the idea it did not build —
and a child built from a substituted parent inherited the parent's report and retired its OWN card.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

from looplab.core.idea_report import (IDEA_REPORT_NAME, idea_not_tested, idea_report_note,
                                      idea_report_of, idea_report_text, inherited_report)
from looplab.core.models import Card, Idea, Node, NodeStatus, RunState

from tests.test_lessons_fingerprint import _captured_causal_prompt, _lineage_state

_DIFFERENT = idea_report_text({"idea_implemented": "different", "built_instead": "the grouped path"})


# ------------------------------------------------------------ the inherited report

def test_a_report_copied_from_the_parent_is_not_the_childs():
    parent = Node(id=0, operator="draft", status=NodeStatus.evaluated, metric=1.0,
                  idea=Idea(operator="draft", params={}), files={IDEA_REPORT_NAME: _DIFFERENT})
    child = Node(id=1, operator="improve", parent_ids=[0], status=NodeStatus.evaluated, metric=0.9,
                 idea=Idea(operator="improve", params={}), files={IDEA_REPORT_NAME: _DIFFERENT})
    nodes = {0: parent, 1: child}
    assert inherited_report(child, nodes) and not inherited_report(parent, nodes)
    assert idea_not_tested(parent, nodes) and not idea_not_tested(child, nodes)
    assert idea_report_note(child, nodes) == ""
    # Without the run's nodes a reader cannot tell — the node alone still reads its file.
    assert idea_not_tested(child)
    # A report of the child's OWN (different bytes) is the child's.
    own = idea_report_text({"idea_implemented": "different", "built_instead": "something new"})
    child.files = {IDEA_REPORT_NAME: own}
    assert idea_not_tested(child, nodes)


def test_an_oversized_or_foreign_report_reads_as_no_report():
    assert idea_report_of(types.SimpleNamespace(files={IDEA_REPORT_NAME: " " * 5000 + _DIFFERENT})) \
        == (None, "")
    assert idea_report_of(types.SimpleNamespace(files={IDEA_REPORT_NAME: 7})) == (None, "")
    assert idea_report_of(None) == (None, "")


def _dev():
    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    return LLMRepoDeveloper(object(), task)


def _silent_done(monkeypatch):
    """A Developer session whose `done` answers nothing about the idea (the field is optional)."""
    import looplab.agents.agent as agent_mod

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": []})
        if name == "propose_plan":
            return finalize({"steps": []})
        tools.execute("write_file", {"path": "solution.py", "content": "print(2)\n"})
        return finalize({"summary": "s"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)


def test_an_improve_build_does_not_carry_its_parents_report(monkeypatch):
    _silent_done(monkeypatch)
    dev = _dev()
    parent = types.SimpleNamespace(id=0, metric=1.0, deleted=[],
                                   files={"solution.py": "print(1)\n", IDEA_REPORT_NAME: _DIFFERENT})
    dev.implement_from(Idea(operator="improve", params={}, rationale="x"), parent)
    assert IDEA_REPORT_NAME not in dev.last_files
    assert dev.last_files["solution.py"] == "print(2)\n"


def test_a_repair_keeps_the_nodes_own_report(monkeypatch):
    _silent_done(monkeypatch)
    dev = _dev()
    node = types.SimpleNamespace(id=3, deleted=[],
                                 files={"solution.py": "print(1)\n", IDEA_REPORT_NAME: _DIFFERENT})
    dev.repair_from(Idea(operator="improve", params={}, rationale="x"), node, "Traceback: boom")
    assert dev.last_files[IDEA_REPORT_NAME] == _DIFFERENT


# ------------------------------------------------------------ the card's other readers

def test_applied_params_never_come_from_a_substituted_node():
    from looplab.events.card_ledger import _apply_card_applied_params

    def node(i, applied):
        return Node(id=i, operator="draft", status=NodeStatus.evaluated, metric=1.0,
                    idea=Idea(operator="draft", params={}),
                    metric_provenance={"applied_params": {"applied": applied}})

    st = RunState(goal="g", direction="max", task_id="t", run_id="r")
    st.nodes = {1: node(1, {"lr": 0.1}), 2: node(2, {"lr": 9.9})}
    card = Card(id="c", statement="s", evidence=[1, 2], substituted_nodes=[2])
    _apply_card_applied_params(st, types.SimpleNamespace(cards={"c": card}))
    assert card.applied_params_node == 1 and card.applied_params == {"lr": 0.1}
    card.substituted_nodes = [1, 2]
    _apply_card_applied_params(st, types.SimpleNamespace(cards={"c": card}))
    assert card.applied_params_node is None and card.applied_params == {}


def test_a_skill_never_quotes_a_substituted_nodes_code(tmp_path):
    from looplab.engine.lessons import LessonMemory

    state = _lineage_state()
    state.nodes[0].code = "real_technique = 1\n"
    state.nodes[1].code = "something_else = 1\n"      # the best metric, and not the card's idea
    state.cards["c1"] = Card(id="c1", statement="warm up the learning rate before the first epoch",
                             verdict="supported", best_delta=0.45, evidence=[0, 1],
                             substituted_nodes=[1])
    mem_dir = tmp_path / "mem"
    mem = LessonMemory.__new__(LessonMemory)
    mem._e = type("_E", (), {
        "_reflection_priors": True, "memory_dir": str(mem_dir), "_comparative_lessons_on": False,
        "task": None,
        "store": type("_S", (), {"read_all": lambda self: [],
                                 "append": lambda self, *a, **k: None})(),
        "_reflect_client": lambda self: None,
        "_task_fingerprint": lambda self, *a: ["kind:toy"],
        "_causal_meta_note": lambda self, *a: "note",
        "_reflect_lessons": lambda self, *a: [],
        "_append_lessons": lambda self, *a, **k: None,
        "_distill_skill_body": lambda self, final, h, ev: mem.distill_skill_body(final, h, ev),
    })()
    mem.write_reflection_note(state)
    body = "\n".join(p.read_text(encoding="utf-8") for p in (mem_dir / "skills").glob("*.md"))
    assert body and "#0 draft" in body
    assert "something_else" not in body and "#1" not in body


def test_the_winner_note_says_the_winner_did_not_build_its_idea(monkeypatch):
    state = _lineage_state()
    state.best_node_id = 1
    state.nodes[1].files = {IDEA_REPORT_NAME: _DIFFERENT}
    state.nodes[1].idea.card_id = "card-2"
    prompt = _captured_causal_prompt(monkeypatch, state)
    row = next(line for line in prompt.splitlines() if line.startswith("#1 "))
    assert "NOT A TEST OF card-2's IDEA (idea different)" in row


def test_the_lineage_lesson_says_it_too():
    from looplab.events.digest import lineage_lessons
    state = _lineage_state()
    state.nodes[1].files = {IDEA_REPORT_NAME: _DIFFERENT}
    text = lineage_lessons(state, state.nodes[0])
    assert "#1 improve improved" in text and "NOT A TEST OF its IDEA" in text


def test_a_question_answered_only_by_substitutions_is_still_open():
    from looplab.events.research_episode import episode
    retired = Card(id="c", statement="s", seed_statement="does the single pass hold recall?",
                   evidence=[4, 5], substituted_nodes=[4, 5])
    mixed = Card(id="m", statement="s", seed_statement="does the cohort cache help?",
                 evidence=[6, 7], substituted_nodes=[7])
    state = types.SimpleNamespace(
        research=[{"at_node": 0, "open_questions": ["does the single pass hold recall?",
                                                    "does the cohort cache help?"]}],
        cards={"c": retired, "m": mixed})
    by_card = {q.card_id: q for q in episode(state).questions}
    assert not by_card["c"].settled and by_card["c"].evidence == ()
    assert by_card["m"].settled and by_card["m"].evidence == (6,)


def test_a_belief_group_row_states_every_members_substitution(monkeypatch):
    from looplab.agents import state_brief
    old = Card(id="card-3", statement="q", seed_statement="does the single pass hold recall?",
               belief_id="b1", evidence=[5, 7], substituted_nodes=[5, 7], status="evaluated")
    new = Card(id="card-9", statement="q", seed_statement="does the single pass hold recall?",
               belief_id="b1", evidence=[12], status="evaluated")
    state = RunState(goal="g", direction="max", task_id="t", run_id="r")
    for nid in (5, 7, 12):
        state.nodes[nid] = Node(id=nid, operator="draft", status=NodeStatus.evaluated, metric=1.0,
                                idea=Idea(operator="draft", params={}),
                                files={IDEA_REPORT_NAME: _DIFFERENT} if nid != 12 else {})
    monkeypatch.setattr(state_brief, "_attempted_belief_groups", lambda _state: {"b1": [old, new]})
    monkeypatch.setattr(state_brief, "attempted_board_prompt_cards", lambda *_a, **_k: [old])
    lines = state_brief.board_prompt_lines(state, board_cards=[], fit=True)
    row = next(line for line in lines if "CARD_ID=card-9" in line)
    assert "NODES=[5, 7, 12]" in row
    assert "card-3: NOT TESTED by node 5 built instead: the grouped path" in row


def test_the_board_window_charges_the_substitution_clause(monkeypatch):
    from looplab.agents import state_brief
    state = RunState(goal="g", direction="max", task_id="t", run_id="r")
    report = idea_report_text({"idea_implemented": "different", "built_instead": "x" * 380})
    cards = []
    for i in range(3):
        state.nodes[i] = Node(id=i, operator="draft", status=NodeStatus.evaluated, metric=1.0,
                              idea=Idea(operator="draft", params={}), files={IDEA_REPORT_NAME: report})
        cards.append(Card(id=f"card-{i}", statement="q", seed_statement="s" * 3000,
                          substituted_nodes=[i]))
    brief = state.card_substitution_brief(cards[0])
    assert len(brief) > 100
    # Room for two seeds and ONE clause: on seed length alone two cards would fit.
    monkeypatch.setattr(state_brief, "BOARD_PROMPT_SEED_BUDGET_CHARS", 2 * 3000 + len(brief))
    monkeypatch.setattr(RunState, "open_research_beliefs", lambda self, *a, **k: cards)
    monkeypatch.setattr(RunState, "cards_being_built", lambda self, *a, **k: set())
    assert [c.id for c in state_brief.next_board_prompt_cards(state)] == ["card-0"]
