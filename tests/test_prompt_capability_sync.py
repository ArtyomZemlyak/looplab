"""Prompt/capability sync (docs/PROMPT_REVIEW.md P5-P8, P14, P21, P25, P30 + the loop-nudge
wording): the shared researcher fragments (sweep offer, eval_timeout, operator note, hardware
attention points) reach BOTH role variants; the sweep offer is gated on a Developer that actually
implements `idea.space`; the wasted non-repo handoff summary is skipped; the pilot menu renders
merge parents. Offline (fake clients only)."""
from __future__ import annotations

import json
from pathlib import Path

from looplab.core.models import Idea, RunState
from looplab.agents.roles import (
    _EVAL_TIMEOUT_GUIDANCE, _OPERATOR_NOTE, _SWEEP_CONTRACT, _SWEEP_OFFER, LLMDeveloper,
    LLMResearcher)

_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- fakes
class _ToolEmitClient:
    """parse_structured(tool_call) fake: records the messages, emits a minimal valid object."""

    def __init__(self, out=None):
        self.messages = None
        self.out = out or {"operator": "draft", "params": {}, "rationale": "r"}

    def complete_tool(self, messages, json_schema):
        if self.messages is None:
            self.messages = [dict(m) for m in messages]
        return self.out


class _TextClient:
    """complete_text fake for the Developer paths: records messages, returns a code block."""

    def __init__(self):
        self.messages = None

    def complete_text(self, messages):
        self.messages = [dict(m) for m in messages]
        return "```python\nprint(1)\n```"


# ------------------------------------------------------- P6/P14/P21: the plain researcher's prompt
def _plain_prompt(offer_sweep: bool):
    c = _ToolEmitClient()
    LLMResearcher(c, offer_sweep=offer_sweep).propose(RunState(goal="g", direction="min"), None)
    return c.messages


def test_plain_researcher_sweep_offer_gated_and_numeric_note_present():
    sys_on = _plain_prompt(offer_sweep=True)[0]["content"]
    assert "propose a SWEEP" in sys_on
    assert "grid values must be NUMERIC" in sys_on                       # P21
    sys_off = _plain_prompt(offer_sweep=False)[0]["content"]
    assert "SWEEP" not in sys_off                                        # P6: offer dropped entirely
    for s in (sys_on, sys_off):
        assert "set `eval_timeout`" in s                                 # eval_timeout ask stays
        assert "stage manifest" in s                                     # honestly scoped (P6)
        assert _OPERATOR_NOTE in s                                       # P14: audit-only operator


def test_plain_researcher_user_turn_sweep_clause_gated():
    on = _plain_prompt(offer_sweep=True)[1]["content"]
    off = _plain_prompt(offer_sweep=False)[1]["content"]
    assert "`space` grid for a sweep" in on
    assert "`space` grid" not in off


# ------------------------------------------- P5/P6/P8/P14/P25: the tool-using researcher's prompt
def _tool_researcher_call(monkeypatch, **ctor):
    from looplab.agents import agent as agent_mod
    from looplab.agents.agent import ToolUsingResearcher
    seen = {}

    def fake_run_phase(client, tools, messages, emit_spec, **kw):
        seen["messages"] = messages
        seen["kw"] = kw
        return Idea(operator="draft", params={}, rationale="ok")

    monkeypatch.setattr(agent_mod, "run_phase", fake_run_phase)
    r = ToolUsingResearcher(client=object(), tools=None, **ctor)
    r.propose(RunState(goal="g", direction="min"), None)
    return seen


def test_tool_researcher_gets_shared_capability_fragments(monkeypatch):
    sys_on = _tool_researcher_call(monkeypatch)["messages"][0]["content"]
    assert _SWEEP_OFFER in sys_on                                        # P6: default researcher too
    assert _EVAL_TIMEOUT_GUIDANCE in sys_on
    assert _OPERATOR_NOTE in sys_on                                      # P14
    assert "Operational attention points" in sys_on                      # P8: hardware cues
    sys_off = _tool_researcher_call(monkeypatch, offer_sweep=False)["messages"][0]["content"]
    assert _SWEEP_OFFER not in sys_off and _EVAL_TIMEOUT_GUIDANCE in sys_off


def test_tool_researcher_prompt_names_only_real_tools(monkeypatch):
    # P5: the default toolset has no `read_file`; the paginating reader is `repo_read` (repo tasks).
    sys = _tool_researcher_call(monkeypatch)["messages"][0]["content"]
    assert "read_file" not in sys
    assert "repo_read" in sys
    assert "truncation marker" in sys       # reconciled with the loop's explicit marker (P3)


def test_tool_researcher_handoff_flag_controls_summary_and_label(monkeypatch):
    # P25: default True keeps the historical repo-phase label; False (single-shot developers)
    # skips the unread summary call and names the developer that actually runs.
    kw_repo = _tool_researcher_call(monkeypatch)["kw"]
    assert kw_repo["handoff"] is True
    assert kw_repo["next_label"] == "the Developer (stages → plan → implement)"
    kw_single = _tool_researcher_call(monkeypatch, handoff=False)["kw"]
    assert kw_single["handoff"] is False
    assert kw_single["next_label"] == "the Developer (single-shot implement)"


# ----------------------------------------------------------------- P7: sweep-contract wording
def test_sweep_contract_conditions_the_looplab_import():
    # The unconditional `from looplab.sweep import run_sweep` crashed in Docker tiers where the
    # package isn't importable — the recommendation is now importability-conditional with a
    # self-written-loop / Optuna fallback, and the JSON `trials` line stays mandatory.
    assert "IF the `looplab` package is importable" in _SWEEP_CONTRACT
    assert "NOT importable" in _SWEEP_CONTRACT and "write the loop yourself" in _SWEEP_CONTRACT
    assert "trials" in _SWEEP_CONTRACT


# ------------------------------------------------------------ P8: strategists + developer repair
def test_llm_strategist_prompt_has_attention_points():
    from looplab.agents.strategist import LLMStrategist, StrategyContext
    c = _ToolEmitClient(out={"rationale": "keep"})
    LLMStrategist(c).decide(RunState(goal="g", direction="min"), StrategyContext())
    assert "Operational attention points" in c.messages[0]["content"]


def test_tool_strategist_prompt_has_attention_points(monkeypatch):
    from looplab.agents import agent as agent_mod
    from looplab.agents.strategist import StrategyContext, ToolUsingStrategist
    seen = {}

    def fake_loop(client, tools, messages, emit_spec, **kw):
        seen["messages"] = messages
        return kw["fallback"](messages)

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    ToolUsingStrategist(object()).decide(RunState(goal="g", direction="min"), StrategyContext())
    assert "Operational attention points" in seen["messages"][0]["content"]


def test_developer_repair_prompt_has_attention_points():
    c = _TextClient()
    LLMDeveloper(c).repair(Idea(operator="debug", params={}), "print(1)", "boom")
    assert "Operational attention points" in c.messages[0]["content"]


# --------------------------------------------------- P6/P25: make_roles gates by developer backend
def test_make_roles_offers_sweep_only_off_repo_tasks(tmp_path):
    from looplab.core.config import Settings
    from looplab.adapters.tasks import load_task, make_roles, validate_task

    # Non-repo task -> in-house LLMDeveloper honors idea.space -> sweep offered; single-shot
    # developer -> no handoff brief reader -> the summary call is skipped.
    task = load_task(_ROOT / "examples" / "code_regression_task.json")
    r, _d = make_roles(task, Settings(backend="llm", unified_agent=False))
    assert getattr(r, "offer_sweep", None) is True
    assert getattr(r, "handoff", None) is False

    # Repo task -> LLMRepoDeveloper never reads idea.space -> no sweep offer; its stages/plan
    # phases DO read the Researcher's handoff brief -> the summary call stays.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "train.py").write_text("print(1)\n")
    rtask = validate_task({"id": "rt", "goal": "g", "direction": "min",
                           "repo": str(repo), "cmd": ["python", "train.py"]})
    rr, _rd = make_roles(rtask, Settings(backend="llm", unified_agent=False))
    assert getattr(rr, "offer_sweep", None) is False
    assert getattr(rr, "handoff", None) is True


# --------------------------------------------------------------- P30: pilot menu + merge parents
class _PilotClient:
    def __init__(self, index=0):
        self.messages = None
        self.index = index

    def chat(self, messages, tools, tool_choice="auto"):
        if self.messages is None:
            self.messages = [dict(m) for m in messages]
        return {"content": "", "tool_calls": [{"id": "c", "function": {
            "name": "choose_action", "arguments": json.dumps({"index": self.index})}}]}


def _unified(pilot_client=None):
    from looplab.agents.unified_agent import UnifiedAgent

    class _R:
        def propose(self, state, parent):
            return Idea(operator="draft", params={})

    class _D:
        def implement(self, idea):
            return "code"

    return UnifiedAgent(researcher=_R(), developer=_D(), pilot_client=pilot_client)


def test_pilot_menu_renders_merge_parents():
    legal = [{"kind": "draft"},
             {"kind": "improve", "parent_id": 3},
             {"kind": "merge", "parent_ids": [1, 2]}]
    client = _PilotClient()
    _unified(client).choose_action(RunState(goal="g", direction="min"), legal)
    menu = client.messages[1]["content"]
    assert "[1] improve parent=3" in menu            # single-parent rendering unchanged
    assert "[2] merge parents=1,2" in menu           # P30: the pilot sees WHAT the merge merges


def test_pilot_recommended_matcher_distinguishes_merge_pairs():
    # kind + parent_id alone matched the FIRST merge regardless of the recommended pair.
    legal = [{"kind": "merge", "parent_ids": [1, 2]},
             {"kind": "merge", "parent_ids": [4, 5]}]
    choice = _unified(pilot_client=None).choose_action(
        RunState(goal="g", direction="min"), legal,
        recommended={"kind": "merge", "parent_ids": [4, 5]})
    assert choice["index"] == 1


# ------------------------------------------------------------------ role-neutral emit-after nudge
def test_emit_after_nudge_is_role_neutral():
    from looplab.agents.agent import drive_tool_loop

    class _Tools:
        def specs(self):
            return [{"type": "function", "function": {
                "name": "peek", "description": "", "parameters": {"type": "object", "properties": {}}}}]

        def execute(self, name, args):
            return f"obs-{args.get('q')}"

    def _call(name, args):
        return {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}

    client_script = [_call("peek", {"q": 1}), _call("peek", {"q": 2}),
                     _call("emit", {"ok": True})]

    class _C:
        def chat(self, messages, tools, tool_choice="auto"):
            return client_script.pop(0)

    emit = {"type": "function", "function": {
        "name": "emit", "description": "final", "parameters": {"type": "object", "properties": {}}}}
    messages = [{"role": "user", "content": "go"}]
    drive_tool_loop(_C(), _Tools(), messages, emit, emit_after=1,
                    finalize=lambda a: ("emit", a), fallback=lambda _m: ("fb", None))
    nudges = [m["content"] for m in messages
              if m.get("role") == "user" and "investigated enough" in str(m.get("content"))]
    assert nudges and "best final output" in nudges[0]
    assert "best idea" not in nudges[0]              # the researcher-flavored wording is gone
