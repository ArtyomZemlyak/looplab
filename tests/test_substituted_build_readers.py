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
from tests.test_card_speculation_engine import (  # noqa: F401 — the receipt fixture is autouse
    _admit_unit_speculation_receipt,
)

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


def test_a_belief_group_row_states_every_members_substitution():
    """The REAL grouping (`_attempted_belief_groups`, no stand-ins): two live cards on one belief
    render as ONE row — the newest card's — listing both cards' nodes, so it states both cards'
    substitutions: the older card's prefixed with its id, the row card's own unprefixed."""
    from looplab.agents import state_brief
    seed = "does the single pass hold recall?"
    old = Card(id="card-3", statement="q", seed_statement=seed, belief_id="b1",
               evidence=[5, 7], substituted_nodes=[5, 7], status="failed")
    new = Card(id="card-9", statement="q", seed_statement=seed, belief_id="b1",
               evidence=[12, 13], substituted_nodes=[13], status="evaluated")
    state = RunState(goal="g", direction="max", task_id="t", run_id="r")
    for nid in (5, 7, 12, 13):
        state.nodes[nid] = Node(id=nid, operator="draft", status=NodeStatus.evaluated, metric=1.0,
                                idea=Idea(operator="draft", params={}),
                                files={} if nid == 12 else {IDEA_REPORT_NAME: _DIFFERENT})
    state.cards = {"card-3": old, "card-9": new}
    lines = state_brief.board_prompt_lines(state, board_cards=[], fit=True)
    [row] = [line for line in lines if "CARD_ID=card-" in line]
    assert "CARD_ID=card-9" in row and "NODES=[5, 7, 12, 13]" in row
    assert "card-3: NOT TESTED by node 5 built instead: the grouped path" in row
    assert " NOT TESTED by node 13 built instead: the grouped path — not counted" in row
    assert "card-9: NOT TESTED" not in row


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


def test_the_token_report_marks_a_card_whose_only_build_was_substituted(tmp_path):
    """`looplab tokens` per-card rows: the spend is real, so it stays; the row says what it bought."""
    import json as _json

    from typer.testing import CliRunner

    from looplab.cli import app
    from tests.test_a_substituted_build_is_not_its_cards_evidence import (
        _build, _evaluate, _report, _setup)

    engine, producer = _setup(tmp_path, "tokens")
    real = _build(engine, producer, "card-1", _report("as_proposed", ""), x=0.2)
    _evaluate(engine, real, 1.0)
    sub = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, sub, 0.5)
    run_dir = Path(engine.store.path).parent
    with (run_dir / "spans.jsonl").open("w", encoding="utf-8") as f:
        for i, card in enumerate(("card-1", "card-2")):
            f.write(_json.dumps({
                "name": "generation", "kind": "generation", "trace_id": f"{i}" * 32,
                "span_id": f"{i}" * 16, "run_id": "r",
                "attributes": {"op": "implement", "model": "m", "phase": "card_build",
                               "card_id": card, "usage": {"prompt": 30, "completion": 10,
                                                          "total": 40}}}) + "\n")
    result = CliRunner().invoke(app, ["tokens", str(run_dir)])
    assert result.exit_code == 0, result.output
    rows = {line.split()[-1]: line for line in result.output.splitlines()
            if line.rstrip().endswith(("card-1", "card-2"))}
    assert "SUBSTITUTED" in rows["card-2"] and "SUBSTITUTED" not in rows["card-1"]


def _plan_build(monkeypatch, answers):
    """A real two-step plan build; `answers[i]` is step i+1's `done` args beyond the summary."""
    import looplab.agents.agent as agent_mod
    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": []})
        if name == "propose_plan":
            return finalize({"steps": [{"title": "A", "detail": "a"}, {"title": "B", "detail": "b"}]})
        step = 1 if "STEP 1 of 2" in str(messages[-1].get("content", "")) else 2
        tools.execute("write_file", {"path": "solution.py", "content": f"print({step})\n"})
        return finalize({"summary": "s", **answers[step - 1]})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    dev = LLMRepoDeveloper(object(), task, plan_decompose=True, plan_min_steps=2)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    return dev.last_files


def test_an_intermediate_plan_step_does_not_answer_for_the_build(monkeypatch):
    """Step 1 of 2, told "do the minimum for this step", honestly says `not_implemented`. If the last
    step's `done` omits the optional field, that answer was the NODE's report — a real test read as a
    substitution, its card returned or retired on it."""
    files = _plan_build(monkeypatch, [{"idea_implemented": "not_implemented"}, {}])
    assert IDEA_REPORT_NAME not in files
    files = _plan_build(monkeypatch, [{"idea_implemented": "not_implemented"},
                                      {"idea_implemented": "as_proposed"}])
    assert idea_report_of(types.SimpleNamespace(files=files))[0] == "as_proposed"


def test_two_honest_reports_on_different_ideas_are_never_one_copied_report(monkeypatch):
    """`not_implemented` with no `built_instead` is the same bytes for every build — unless the report
    names its idea. A child answering honestly about ITS idea must not read as its parent's copy."""
    _answer = {"idea_implemented": "not_implemented"}
    import looplab.agents.agent as agent_mod

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name in ("declare_stages", "propose_plan"):
            return finalize({"stages": []} if name == "declare_stages" else {"steps": []})
        tools.execute("write_file", {"path": "solution.py", "content": "print(1)\n"})
        return finalize({"summary": "s", **_answer})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    dev = _dev()
    dev.implement(Idea(operator="draft", params={}, rationale="parent idea"))
    parent_files = dict(dev.last_files)
    parent = Node(id=0, operator="draft", status=NodeStatus.evaluated, metric=1.0,
                  idea=Idea(operator="draft", params={}), files=parent_files)
    dev.implement_from(Idea(operator="improve", params={}, rationale="child idea"),
                       types.SimpleNamespace(id=0, metric=1.0, deleted=[], files=parent_files))
    child = Node(id=1, operator="improve", parent_ids=[0], status=NodeStatus.evaluated, metric=0.9,
                 idea=Idea(operator="improve", params={}), files=dict(dev.last_files))
    nodes = {0: parent, 1: child}
    assert idea_not_tested(parent, nodes) and idea_not_tested(child, nodes)


# ------------------------------------------------------------ every reader of "what the node did"

_LABEL = "NOT A TEST OF card-2's IDEA (idea different) — built instead: the grouped path"


def _run_with_substitution():
    """#0 an honest root, #1 its child whose Developer built something else under card-2, #2 a failed
    child that did the same."""
    state = RunState(goal="g", direction="max", task_id="t", run_id="r")
    state.nodes[0] = Node(id=0, operator="draft", status=NodeStatus.evaluated, metric=0.5,
                          idea=Idea(operator="draft", params={"lr": 0.1}, rationale="baseline"))
    for nid, status, metric in ((1, NodeStatus.evaluated, 0.9), (2, NodeStatus.failed, None)):
        state.nodes[nid] = Node(
            id=nid, operator="improve", parent_ids=[0], status=status, metric=metric,
            error_reason="crash" if metric is None else "",
            idea=Idea(operator="improve", params={"lr": 0.3}, rationale="single ragged pass",
                      card_id="card-2"),
            files={IDEA_REPORT_NAME: _DIFFERENT})
    return state


def test_read_experiment_says_what_was_built():
    from looplab.tools.run_tools import readonly_run_tools
    state = _run_with_substitution()
    tools = readonly_run_tools(state)
    assert f"built: [{_LABEL}]" in tools.execute("read_experiment", {"node_id": 1})
    assert "built:" not in tools.execute("read_experiment", {"node_id": 0})


def test_the_lesson_guard_and_the_lineage_lessons_say_it_for_both_outcomes():
    from looplab.events.digest import lineage_lessons
    from looplab.trust.lesson_guard import _evidence_text
    state = _run_with_substitution()
    text = _evidence_text({"node_ids": [0, 1]}, state)
    assert _LABEL in text.splitlines()[1] and "NOT A TEST" not in text.splitlines()[0]
    lessons = lineage_lessons(state, state.nodes[0])
    assert all(_LABEL in line for line in lessons.splitlines() if line.startswith("  #"))


def test_the_memo_claim_verifier_row_carries_it():
    from looplab.trust.memo_verify import _evidence_snapshot
    state = _run_with_substitution()
    snapshot, _ids = _evidence_snapshot({"node_ids": [0, 1]}, state)
    rows = {row["node_id"]: row for row in snapshot["experiments"]}
    assert _LABEL in rows[1]["built"] and "built" not in rows[0]


def test_the_ensemble_parent_description_carries_it():
    from looplab.engine.node_build import NodeBuildMixin
    state = _run_with_substitution()
    idea = NodeBuildMixin._ensemble_idea(None, [state.nodes[0], state.nodes[1]], state.nodes)
    assert f"single ragged pass [{_LABEL}]" in idea.rationale
    assert "baseline [NOT A TEST" not in idea.rationale


def test_the_value_estimate_and_the_verifier_tiebreak_and_graded_novelty_see_it(monkeypatch):
    from looplab.engine.value_estimate import ValueEstimateMixin
    from looplab.engine.verifier_tiebreak import VerifierTiebreakMixin
    from looplab.search import graded_novelty
    from looplab.trust import judge, verifier

    state = _run_with_substitution()
    sent: list = []
    monkeypatch.setattr(judge, "structured_judge",
                        lambda client, msgs, *a, **k: sent.append(msgs[-1]["content"]))
    ValueEstimateMixin._branch_headroom(types.SimpleNamespace(), state, state.nodes[0], object())
    ValueEstimateMixin._branch_headroom(types.SimpleNamespace(), state, state.nodes[1], object())
    assert f"#1 tried single ragged pass [{_LABEL}]" in sent[0]
    assert f"What it tried: single ragged pass [{_LABEL}]" in sent[1]

    evidence: list = []
    monkeypatch.setattr(verifier, "verify",
                        lambda subject, ev, *a, **k: evidence.append(ev))
    state.select_verifier_samples = 1
    VerifierTiebreakMixin._verifier_soundness(types.SimpleNamespace(), state, state.nodes[1], object())
    assert f"What it did: single ragged pass [{_LABEL}]" in evidence[0]
    graph = types.SimpleNamespace()
    monkeypatch.setattr(graded_novelty, "tag_idea", lambda idea, g: set())
    try:
        graded_novelty.reexamine_failed_direction(state, 2, graph, client=object())
    except AttributeError:
        pass        # the fake verdict has no per_criterion; the evidence was already built and sent
    assert f"What it did: single ragged pass [{_LABEL}]" in evidence[1]


def test_the_attempted_window_charges_the_substitution_clause_too():
    """The context-only window spent its 8k on seed length alone; a retired card's NOT TESTED clause
    rides on its row, so two 3.9k seeds that fit on length alone no longer both fit."""
    from looplab.agents import state_brief
    state = RunState(goal="g", direction="max", task_id="t", run_id="r")
    report = idea_report_text({"idea_implemented": "different", "built_instead": "x" * 380})
    for i in range(2):
        for nid in (10 * i, 10 * i + 1):
            state.nodes[nid] = Node(id=nid, operator="draft", status=NodeStatus.evaluated, metric=1.0,
                                    idea=Idea(operator="draft", params={}),
                                    files={IDEA_REPORT_NAME: report})
        state.cards[f"card-{i}"] = Card(
            id=f"card-{i}", statement="q", seed_statement=f"{i}" * 3_900, belief_id=f"b{i}",
            evidence=[10 * i, 10 * i + 1], substituted_nodes=[10 * i, 10 * i + 1],
            status="failed")
    assert len(state_brief.attempted_board_prompt_cards(state)) == 1
    for card in state.cards.values():
        card.substituted_nodes = []
    assert len(state_brief.attempted_board_prompt_cards(state)) == 2


def test_the_legacy_repair_carry_does_not_carry_another_nodes_report(monkeypatch):
    """`repair()` with no explicit base carries the shared Developer's LAST build — almost never the
    node being repaired — so that build's report is another node's answer about another idea."""
    _silent_done(monkeypatch)
    dev = _dev()
    dev.last_files = {"solution.py": "print(1)\n", IDEA_REPORT_NAME: _DIFFERENT}
    dev.repair(Idea(operator="improve", params={}, rationale="x"), "", "Traceback: boom")
    assert IDEA_REPORT_NAME not in dev.last_files and "solution.py" in dev.last_files


def test_a_parents_copied_report_is_not_a_label_in_the_sibling_digest_or_the_novelty_rows():
    """Both callers hand the run's nodes to the note, so a child carrying its parent's bytes — a log
    written before the preload dropped them — is not called a substitution anywhere it is shown."""
    from looplab.engine.novelty import _prior_outcome
    from looplab.events.digest import sibling_digest
    state = _run_with_substitution()
    state.nodes[0].files = {IDEA_REPORT_NAME: _DIFFERENT}       # the parent reported it FIRST…
    state.nodes[3] = Node(id=3, operator="improve", parent_ids=[0], status=NodeStatus.evaluated,
                          metric=0.7, idea=Idea(operator="improve", params={}, rationale="copy"),
                          files={IDEA_REPORT_NAME: _DIFFERENT})    # …and #3 only carries the copy
    rows = {line.split()[0]: line for line in sibling_digest(state, state.nodes[0]).splitlines()
            if line.lstrip().startswith("#")}
    assert "NOT A TEST" not in rows["#3"]
    assert "NOT A TEST" not in _prior_outcome(state.nodes[3], state.nodes)
    assert "NOT A TEST" in _prior_outcome(state.nodes[3])           # the file alone cannot tell


def test_the_deep_research_brief_labels_a_substituted_experiment():
    """The memo brief lists experiments by their proposal's rationale; a node that built something
    else is labelled there, or the memo reads its metric as that proposal's."""
    from looplab.agents.deep_research import state_brief
    state = _run_with_substitution()
    brief = state_brief(state)
    row = next(line for line in brief.splitlines() if line.lstrip().startswith("#1 "))
    assert _LABEL in row
    assert "NOT A TEST" not in next(line for line in brief.splitlines()
                                    if line.lstrip().startswith("#0 "))
