"""The one permission-gate ceremony six tool providers share (doc 25 TO-04).

Each hand-rolled the same three steps: build the action, `decide_action`, map `deny` to a plan-mode
refusal, and on `ask` put `approver(action) or "deny"` through `approval_allows`. The bodies differed
only in wording.

That is not a tidiness finding. One of the six copies had ALREADY been caught checking `deny` alone
and killing a process-global background task in the DEFAULT `ask` mode with no approval at all
(arch-review §3 P0-6) — because plan-mode deny does not satisfy ask-mode approval semantics, and a
gate that forgets the second half still LOOKS gated to every reader and every test that only tries
plan mode. So the tests here run the WHOLE matrix (mode × decision × verdict) against the shared
helper, and then assert that each of the six providers actually refuses in ask mode without an
approval — provider by provider, not just once.
"""
from __future__ import annotations

import pytest

from looplab.tools import perm_modes
from looplab.tools.perm_modes import (APPROVAL_ALLOW_ALWAYS, APPROVAL_ALLOW_ONCE, authorize,
                                      refusal_for)


_WRITE = {"tool": "write_file", "tool_kind": "write", "label": "write a.py",
          "verb": "write `a.py`", "preview": "x = 1", "cwd": "/w"}


def _authorize(mode, verdict, action=None):
    return authorize(mode, lambda _action: verdict, action if action is not None else _WRITE,
                     denied="(denied wording)", declined="the subject")


# ------------------------------------------------------------------ the three outcomes

def test_plan_mode_returns_the_callers_own_deny_wording():
    """The refusal is a contract the MODEL reads — it names which capability is off and how to turn
    it on — so the helper renders the caller's string rather than inventing a generic one."""
    assert _authorize("plan", APPROVAL_ALLOW_ONCE) == "(denied wording)"


def test_an_inline_decision_proceeds_without_asking_anyone():
    called = []
    assert authorize("auto", lambda action: called.append(action) or "deny", _WRITE,
                     denied="(denied)", declined="subject") is None
    assert called == [], "an inline decision must not pay for an approval round-trip"


@pytest.mark.parametrize("verdict", [APPROVAL_ALLOW_ONCE, APPROVAL_ALLOW_ALWAYS])
def test_an_approved_ask_proceeds(verdict):
    assert _authorize("default", verdict) is None


@pytest.mark.parametrize("verdict", ["deny", None, "", "allow", "allow_onc", "allow_once_please",
                                     "ALLOW_ONCE", 0, False])
def test_anything_that_is_not_an_exact_approval_declines(verdict):
    """`approval_allows` accepts only the two exact wire values. Prefix, suffix and case lookalikes
    all fail closed — a truthiness check here would authorize `"allow"` and `"allow_onc"` alike."""
    assert _authorize("default", verdict) == "(declined by the user: the subject)"


def test_a_provider_with_no_approver_at_all_declines():
    """Being unable to ask is not permission. One provider guarded this locally
    (`machine_runs_tools`); making it the helper's rule is what stops the next one from omitting it.
    """
    for approver in (None, "not callable", 7):
        assert authorize("default", approver, _WRITE,
                         denied="(denied)", declined="s") == "(declined by the user: s)"


def test_the_approver_is_handed_the_exact_action_it_will_be_asked_about():
    """The approval is bound to a described action — a prompt built from a DIFFERENT dict would ask
    the user about something other than what runs."""
    seen = []
    authorize("default", lambda action: seen.append(action) or "deny", _WRITE,
              denied="(denied)", declined="s")
    assert seen == [_WRITE]


def test_the_approver_is_called_exactly_once():
    """Twice would double-prompt; the second call could also legitimately answer differently, which
    would make the gate's verdict depend on which call it happened to read."""
    calls = []
    authorize("default", lambda action: calls.append(1) or APPROVAL_ALLOW_ONCE, _WRITE,
              denied="(denied)", declined="s")
    assert len(calls) == 1


# ------------------------------------------------------------------ the split for the odd caller

def test_refusal_for_lets_a_caller_work_between_the_deny_check_and_the_ask():
    """`machine_runs_tools._gate` captures the run generation there, so its mutation fence describes
    the run as it was BEFORE the user was asked. That ordering is the reason the helper is split."""
    order = []
    decision = perm_modes.decide_action("default", _WRITE)
    assert decision == "ask"
    order.append("captured the generation")
    refusal_for(decision, lambda action: order.append("asked") or "deny", _WRITE,
                denied="(denied)", declined="s")
    assert order == ["captured the generation", "asked"]


def test_refusal_for_and_authorize_agree_on_every_decision():
    for mode in ("plan", "default", "acceptEdits", "auto"):
        for verdict in (APPROVAL_ALLOW_ONCE, "deny", None):
            through_authorize = authorize(mode, lambda _a: verdict, _WRITE,
                                          denied="(denied)", declined="s")
            through_split = refusal_for(perm_modes.decide_action(mode, _WRITE),
                                        lambda _a: verdict, _WRITE,
                                        denied="(denied)", declined="s")
            assert through_authorize == through_split, (mode, verdict)


# ------------------------------------------------------------------ every provider still gates

def _provider_gates():
    """(name, callable(mode, approver) -> refusal-or-None) for each of the six sites."""
    from looplab.tools.concept_tools import ConceptGovernanceTools
    from looplab.tools.knowledge_tools import KnowledgeWriteTools
    from looplab.tools.shell_tools import ShellTools
    from looplab.tools.write_tools import WriteTools

    def write(tmp, mode, approver):
        tools = WriteTools([tmp], mode=mode, approver=approver)
        return tools._authorize("write", dict(_WRITE))

    def shell(tmp, mode, approver):
        tools = ShellTools([tmp], mode=mode, approver=approver)
        return tools.execute("run_command", {"command": ["echo", "hi"], "cwd": str(tmp)})

    def kill_background(tmp, mode, approver):
        tools = ShellTools([tmp], mode=mode, approver=approver)
        return tools.execute("kill_background", {"task_id": "nope"})

    def concept(tmp, mode, approver):
        tools = ConceptGovernanceTools(tmp, mode=mode, approver=approver)
        return tools._gate("concept_merge", "merge a into b", "a -> b", {"operation": "merge"})

    def knowledge(tmp, mode, approver):
        tools = KnowledgeWriteTools(tmp, mode=mode, approver=approver)
        return tools.execute("remember", {"title": "t", "note": "n"})

    return [("write_tools", write), ("shell_tools.run_command", shell),
            ("shell_tools.kill_background", kill_background),
            ("concept_tools", concept), ("knowledge_tools", knowledge)]


@pytest.mark.parametrize("name,gate", _provider_gates(), ids=[n for n, _ in _provider_gates()])
def test_every_provider_refuses_in_plan_mode(tmp_path, name, gate):
    result = gate(tmp_path, "plan", lambda action: APPROVAL_ALLOW_ONCE)
    assert result and "plan mode" in str(result).lower(), f"{name} did not refuse in plan mode"


@pytest.mark.parametrize("name,gate", _provider_gates(), ids=[n for n, _ in _provider_gates()])
def test_every_provider_refuses_an_UNAPPROVED_ask(tmp_path, name, gate):
    """THE regression this finding is really about. `default` is the normal mode, and a gate that
    only handles `deny` sails straight through it — which is how a background task got killed with
    no approval. Plan mode passing proves nothing about this."""
    result = gate(tmp_path, "default", lambda action: "deny")
    assert result and "declined by the user" in str(result), (
        f"{name} performed a mutating action in ask mode without an approval")


@pytest.mark.parametrize("name,gate", _provider_gates(), ids=[n for n, _ in _provider_gates()])
def test_every_provider_asks_the_approver_rather_than_assuming(tmp_path, name, gate):
    asked = []
    gate(tmp_path, "default", lambda action: asked.append(action.get("tool")) or "deny")
    assert asked, f"{name} never consulted the approver in ask mode"


def test_no_provider_hand_rolls_the_ceremony_again():
    """A grep guard: `approval_allows(` outside `perm_modes.py` means a seventh copy. The decision
    function alone is fine — a provider may legitimately branch on `decide_action` — but the approval
    half is the half that gets forgotten."""
    from pathlib import Path

    tools = Path(__file__).resolve().parents[1] / "looplab" / "tools"
    offenders = [f"{path.name}:{index}"
                 for path in sorted(tools.glob("*.py")) if path.name != "perm_modes.py"
                 for index, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
                 if "approval_allows(" in line]
    assert offenders == [], (
        f"the approval half of the gate is re-implemented outside perm_modes: {offenders}")
