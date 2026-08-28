"""The writing session is told how much money is left, the way the reference agent's is.

MEASURED ASYMMETRY. `AlgoTuner/utils/message_writer.py:1442` renders "You have sent N messages and
have used up $X. You have $Y remaining." and `format_message_with_budget` puts it FIRST in every
message. In the live arm-A log (`campaign-final/A-convex_hull.log`) that line appears 112 times and
the run lands on $0.9952 of $1.0000 -- half a cent under, deliberately.

Ours flew blind. Resolving the `input_carry`/`input_from` chain over dsFB3's spans, 0 of 317
generations carried any spend figure, and the ceiling arrives as a node CRASH -- "LLM spend ceiling
reached: $1.0024 of the $1.0000. The run stops here" -- which throws that node's work away. Measured
overshoot across finished probes: $1.002 (fxSpectral) to $1.091 (gpt56luna).

These cases pin what has to be true of the line: the numbers are the accountant's real ones, it
leads the prompt, and it is SILENT in every degenerate case, because an extra rung must never fail
a build. A run without `llm_budget_usd` keeps the byte-identical prompt it had before.
"""
from __future__ import annotations

import sys
from pathlib import Path

from looplab.core.llm import CostAccountant
from looplab.core.models import Idea
from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.adapters.repo_developer import LLMRepoDeveloper


class _Client:
    def __init__(self, accountant=None):
        if accountant is not None:
            self.accountant = accountant


def _task(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("print('source')\n", encoding="utf-8")
    return RepoTask(id="r", goal="g", direction="max", editable_path=str(root),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "main.py"]))


def _dev(root: Path, client, **kw):
    kw.setdefault("plan_decompose", True)
    kw.setdefault("plan_min_steps", 2)
    return LLMRepoDeveloper(client, _task(root), **kw)


def _install_fake_loop(monkeypatch, n_steps, capture):
    import looplab.agents.agent as agent_mod

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        capture.append({"name": name, "messages": list(messages)})
        if name == "declare_stages":
            return finalize({"stages": [{"name": "train", "command": ["python", "main.py"]}]})
        if name == "propose_plan":
            return finalize({"steps": [{"title": f"S{i}", "detail": f"d{i}"}
                                       for i in range(1, n_steps + 1)]})
        idx = sum(1 for c in capture if c["name"] == "done")
        tools.execute("write_file", {"path": "solver.py", "content": f"X = {idx}\n"})
        return finalize({"summary": f"wrote step {idx}"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)


def _steps(capture):
    return [c["messages"][-1]["content"] for c in capture if c["name"] == "done"]


def test_the_writing_session_is_told_what_is_left_and_it_leads_the_prompt(monkeypatch, tmp_path):
    acct = CostAccountant(limit=1.0)
    acct.add(0.25)
    cap: list = []
    _install_fake_loop(monkeypatch, 2, cap)
    _dev(tmp_path / "repo", _Client(acct)).implement(
        Idea(operator="draft", params={}, rationale="a two-part change"))

    for step in _steps(cap):
        assert step.startswith("BUDGET: "), "the reference agent puts the money first; so do we"
        assert "$0.2500 of $1.0000 spent" in step
        assert "$0.7500 left" in step and "(25 % gone)" in step
    assert _steps(cap), "the fake loop produced no step sessions"


def test_the_figure_is_the_accountant_s_own_and_moves_with_it(monkeypatch, tmp_path):
    acct = CostAccountant(limit=2.0)
    cap: list = []
    _install_fake_loop(monkeypatch, 2, cap)
    dev = _dev(tmp_path / "repo", _Client(acct))
    acct.add(1.5)
    dev.implement(Idea(operator="draft", params={}, rationale="r"))
    assert "$1.5000 of $2.0000 spent" in _steps(cap)[0]
    assert "$0.5000 left" in _steps(cap)[0] and "(75 % gone)" in _steps(cap)[0]


def test_no_budget_no_line_and_the_prompt_is_the_old_one(monkeypatch, tmp_path):
    cap: list = []
    _install_fake_loop(monkeypatch, 2, cap)
    _dev(tmp_path / "repo", _Client(CostAccountant())).implement(
        Idea(operator="draft", params={}, rationale="r"))
    for step in _steps(cap):
        assert "BUDGET:" not in step
        assert step.startswith("You are implementing a multi-step plan")


def test_every_degenerate_client_is_silence_not_a_crash(tmp_path):
    """No client attribute, no accountant, a junk limit, a junk spend, an overspend -- all "" or sane."""
    dev = _dev(tmp_path / "repo", object())
    assert dev._budget_note() == ""                                  # no .accountant
    assert _dev(tmp_path / "r2", _Client(None))._budget_note() == ""  # accountant absent

    class _Junk:
        limit = "not a number"
        spent = 0.0
    assert _dev(tmp_path / "r3", _Client(_Junk()))._budget_note() == ""

    nan = CostAccountant(limit=float("nan")); 
    assert _dev(tmp_path / "r4", _Client(nan))._budget_note() == ""
    neg = CostAccountant(limit=-1.0)
    assert _dev(tmp_path / "r5", _Client(neg))._budget_note() == ""

    # `add` past the ceiling RAISES BudgetExceeded, so an overspend can only be reached by the
    # path that actually produces one in the wild: a committed spend already at/over the limit,
    # read by a session that started before the guard fired.
    over = CostAccountant(limit=1.0); over.spent = 1.4
    note = _dev(tmp_path / "r6", _Client(over))._budget_note()
    assert "$0.0000 left" in note and "(100 % gone)" in note, "never a negative remainder"
