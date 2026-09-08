"""`LLMRepoDeveloper._run`'s three exits publish ONE epilogue, and the fresh path is one method.

Doc 25 RA-07 asked for both cuts and only the `_stage_note` half landed in 2026-08-05. The rest is
here: `_run_fresh` (the plan/implement orchestration), `_fresh_stage_note` (the STAGES decision tree
plus its four locals) and `_record_result` (the `last_files`/`last_edit_calls`/`last_deleted`/
`last_footprint` bookkeeping that was hand-copied at all three exits).

The copies had DRIFTED, which is the finding's own argument arriving late: the `OperatorRefusal`
fault path published three of the four facts and left `last_edit_calls` at the 0 `_run` sets on
entry. So a build that edited files and then hit a provider outage handed the engine those files
under zero edit attempts — and `engine/node_build.py` reads both off the developer through the same
`DEVELOPER_OUTPUT_ATTRS` seam, so the two halves of one receipt came from different facts.

These tests DRIVE the property (a real developer, a real write tool, a real exit) rather than
pinning the text of the epilogue: a `pass  # self._record_result(...)` would satisfy a source scan
and publish nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
from looplab.core.errors import ConfigRefusal
from looplab.core.models import Idea, is_developer_error

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}


# A footprint on the Idea so `last_footprint` is a VALUE rather than the None an undeclared
# footprint resolves to — otherwise "the fourth fact is published" would assert None is None.
_FOOTPRINT = {"gpus": 1}


def _idea(**kw):
    return Idea(operator="draft", params={}, rationale="x", footprint=dict(_FOOTPRINT), **kw)


def _task(**kw):
    return RepoTask(id="r", goal="g", direction="max", editable_path=str(FIXTURE),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "main.py"], metric=_M), **kw)


def _install_loop(monkeypatch, *, on_done, capture=None):
    """Patch the ONE documented seam (`looplab.agents.agent.drive_tool_loop`).

    The stages/plan phases answer minimally; `on_done(tools, finalize)` decides what the implement
    or repair session does — write files, raise, return a summary.
    """
    import looplab.agents.agent as agent_mod

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if capture is not None:
            capture.append({"name": name, "messages": list(messages)})
        if name == "declare_stages":
            return finalize({"stages": [{"name": "train", "command": ["python", "train.py"]}]})
        if name == "propose_plan":
            return finalize({"steps": [{"title": "A", "detail": "a"}, {"title": "B", "detail": "b"}]})
        return on_done(tools, finalize)

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)


# --------------------------------------------------------------------------- #
# _record_result: one epilogue, four facts, three exits.

def _wrote_two_files(tools, finalize):
    tools.execute("write_file", {"path": "solution.py", "content": "print(1)\n"})
    tools.execute("write_file", {"path": "helper.py", "content": "print(2)\n"})
    return finalize({"summary": "wrote two files"})


def test_a_clean_build_publishes_all_four_facts(monkeypatch):
    _install_loop(monkeypatch, on_done=_wrote_two_files)
    dev = LLMRepoDeveloper(object(), _task(), plan_decompose=False)
    assert dev._run(_idea()) == ""
    # `looplab_stages.json` rides along: the STAGES phase materializes its manifest into the same
    # working set, through `declare_stages`, which is deliberately NOT one of the three edit tools.
    assert set(dev.last_files) == {"solution.py", "helper.py", "looplab_stages.json"}
    assert dev.last_edit_calls == 2
    assert dev.last_deleted == []
    assert dev.last_footprint == {"gpus": 1}


@pytest.mark.parametrize("boom,sentinel", [
    # The FAULT exit: an `OperatorRefusal` that is not a budget ceiling (an outage, a bad key) keeps
    # the crash sentinel and used to publish only three of the four facts.
    (ConfigRefusal("endpoint unreachable"), True),
    # The blanket developer-hiccup trap.
    (RuntimeError("boom"), True),
])
def test_a_failed_build_publishes_the_edit_count_it_actually_spent(monkeypatch, boom, sentinel):
    """The drift the extraction closed: files without their edit count is half a receipt.

    Both failing exits reach `_record_result` with the SAME `write` the clean exit would, so the
    engine reads two files and the two edit attempts that produced them — never two files and zero
    attempts, which is what the fault path published while the epilogue was hand-copied.
    """
    def on_done(tools, finalize):
        _wrote_two_files(tools, finalize)
        raise boom

    _install_loop(monkeypatch, on_done=on_done)
    dev = LLMRepoDeveloper(object(), _task(), plan_decompose=False)
    out = dev._run(_idea())
    assert bool(is_developer_error(out)) is sentinel, out
    assert {"solution.py", "helper.py"} <= set(dev.last_files)
    assert dev.last_edit_calls == 2, "the fault exit must publish THIS call's edit attempts"
    assert dev.last_footprint == {"gpus": 1}


def test_the_epilogue_is_cleared_per_call_not_carried_from_the_sibling_before_it(monkeypatch):
    """`_run` clears `last_edit_calls` on entry BECAUSE the developer instance is shared, and
    `_record_result` may therefore only ever publish this call's own count. A build that wrote
    nothing after one that wrote twice must not inherit the 2."""
    _install_loop(monkeypatch, on_done=_wrote_two_files)
    dev = LLMRepoDeveloper(object(), _task(), plan_decompose=False)
    dev._run(Idea(operator="draft", params={}, rationale="x"))
    assert dev.last_edit_calls == 2

    _install_loop(monkeypatch, on_done=lambda tools, finalize: finalize({"summary": "nothing"}))
    dev._run(Idea(operator="draft", params={}, rationale="y"))
    assert dev.last_edit_calls == 0
    assert "solution.py" not in dev.last_files and "helper.py" not in dev.last_files


def test_no_exit_of_run_still_assigns_the_epilogue_by_hand():
    """The other direction: a fourth exit that re-copies the four assignments is the defect again.

    An AST walk over `_run` only — the assignments are legitimate INSIDE `_record_result`, and
    reading the whole class would make this vacuous.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(LLMRepoDeveloper._run)))
    assigned = {t.attr for node in ast.walk(tree) if isinstance(node, ast.Assign)
                for t in node.targets
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                and t.value.id == "self"}
    published = {"last_files", "last_deleted", "last_footprint"}
    assert not (assigned & published), (
        f"_run assigns {sorted(assigned & published)} directly — the epilogue is "
        "`_record_result(write, idea)` and a hand-copy is how the fault exit came to omit "
        "last_edit_calls")
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "_record_result" in called


# --------------------------------------------------------------------------- #
# _run_fresh / _fresh_stage_note: the fresh path is one method, and it is told one pipeline.

def test_only_a_fresh_repo_build_runs_the_fresh_path(monkeypatch):
    """A repair is a single bounded session and must not enter the plan/stages orchestration."""
    seen: list = []
    monkeypatch.setattr(LLMRepoDeveloper, "_run_fresh",
                        lambda self, *a, **kw: seen.append(kw.get("stage_note")))
    _install_loop(monkeypatch, on_done=lambda tools, finalize: finalize({"summary": "s"}))
    dev = LLMRepoDeveloper(object(), _task(), plan_decompose=True, plan_min_steps=2)

    dev._run(Idea(operator="draft", params={}, rationale="x"))
    assert len(seen) == 1 and "PIPELINE for this node" in (seen[0] or "")

    dev._run(Idea(operator="improve", params={}, rationale="x"), error="boom")
    assert len(seen) == 1, "a repair must not enter _run_fresh"


def test_every_plan_step_is_told_the_pipeline_the_user_message_asserts(monkeypatch):
    """`stage_note` is computed ONCE and threaded, so the two readers cannot disagree.

    The user message and every plan step get the SAME string. Recomputing it inside `_run_fresh`
    would look identical here today and drift the first time the stages phase becomes non-pure —
    which is why it is a parameter rather than a second call.
    """
    cap: list = []
    _install_loop(monkeypatch, on_done=lambda tools, finalize: finalize({"summary": "s"}),
                  capture=cap)
    dev = LLMRepoDeveloper(object(), _task(), plan_decompose=True, plan_min_steps=2)
    dev._run(Idea(operator="draft", params={}, rationale="x"))

    note = dev._stage_note(False, [{"name": "train"}], False, False)
    steps = [c for c in cap if c["name"] == "done"]
    assert len(steps) == 2, [c["name"] for c in cap]
    for call in steps:
        assert note in call["messages"][1]["content"]


def test_the_stages_tree_answers_with_the_operator_pipeline_when_there_is_one(monkeypatch):
    """`_fresh_stage_note` owns the four-way precedence; the operator's list wins verbatim."""
    _install_loop(monkeypatch, on_done=lambda tools, finalize: finalize({"summary": "s"}))
    t = RepoTask(id="r", goal="g", direction="max", editable_path=str(FIXTURE),
                 edit_surface=["*.py"], protect=[],
                 eval=EvalSpec(stages=[{"name": "train", "command": ["python", "train.py"]},
                                       {"name": "score", "command": ["python", "test.py"]}],
                               metric=_M))
    dev = LLMRepoDeveloper(object(), t, plan_decompose=False)
    from looplab.adapters.repo_write_tools import RepoWriteTools
    write = RepoWriteTools(dev._surface, dev._protected, dev._prefixes, editables=dev._editables)
    note = dev._fresh_stage_note(Idea(operator="draft", params={}, rationale="x"), write, "sys",
                                 dev._operator_stage_list())
    assert note == dev._stage_note(True, dev._operator_stage_list(), False, False)
    assert "OPERATOR-declared, runs verbatim" in note
