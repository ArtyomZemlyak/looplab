"""Four pieces of API surface that existed for nobody (doc 25 AG-09, TO-10).

Dead surface is not free. Each of these cost something specific beyond the line count:

* the `agent.py` facade forwarded six tool_loop privates nothing imported through it, WIDENING a
  two-path ambiguity that is already subtle — for a module-level constant the re-import is a
  rebinding, not an alias, so patching `tool_loop._X` and patching `agent._X` do different things;
* `perm_modes.decide` answered a permission question with a COARSER rule than the one production
  uses, so reaching for the shorter name would have silently widened a provider's permissions;
* `VectorStore.delete`/`rebuild` made the backend Protocol wrong in both directions;
* `RunTools.parent` implied a back-reference the provider does not have.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.agents import agent as agent_mod
from looplab.agents import tool_loop
from looplab.tools import perm_modes
from looplab.tools.run_tools import RunTools
from looplab.tools.vectorstore import InMemoryVectorStore, Item, VectorStore


# ------------------------------------------------------------------ AG-09: the facade's forwards

_FORWARDED_PRIVATES = ("_cap_tool_result", "_flatten_transcript", "_force_emit", "_handoff_ctx")


@pytest.mark.parametrize("name", _FORWARDED_PRIVATES)
def test_a_private_with_a_real_consumer_is_still_forwarded(name):
    assert getattr(agent_mod, name) is getattr(tool_loop, name), (
        "both paths must resolve to the SAME object or a monkeypatch through one misses the other")


@pytest.mark.parametrize("name", ["_PLAN_TOOL_NAME", "_REPEAT_NOTE", "_TRUNC_NOTE",
                                  "_plan_spec", "_render_plan", "_summarizer"])
def test_a_private_nobody_imports_through_the_facade_is_not_forwarded(name):
    assert hasattr(tool_loop, name), f"{name} must still exist where it lives"
    assert not hasattr(agent_mod, name), (
        f"{name} is re-exported by the facade with no consumer; a caller patching agent.{name} "
        "instead of tool_loop.{name} would silently patch nothing that runs")


def test_the_public_seams_all_still_resolve_through_the_facade():
    """The facade EXISTS for these; trimming privates must not touch them."""
    for name in ("CompositeTools", "agentic_struct", "agentic_text", "drive_tool_loop",
                 "emit_loop", "handoff_scope", "loop_opts_from_settings", "summarize_phase"):
        assert getattr(agent_mod, name) is getattr(tool_loop, name), name
    assert callable(agent_mod.run_phase), "run_phase is defined here, not forwarded"


def test_the_forwarding_rule_is_written_down():
    """The next tool_loop private must not be added by reflex."""
    source = inspect.getsource(agent_mod)
    assert "NOT auto-forwarded" in source


# ------------------------------------------------------------------ TO-10: three dead surfaces

def test_the_kind_only_permission_matrix_is_gone():
    assert not hasattr(perm_modes, "decide"), (
        "a second, coarser answer to 'may this tool run' is worse than none")
    assert callable(perm_modes.decide_action)


def test_the_deletion_says_why_so_it_is_not_reintroduced():
    assert "doc 25 TO-10" in inspect.getsource(perm_modes)


def test_the_surviving_matrix_is_strictly_the_safer_one():
    """The exact case the deleted helper got wrong: it keyed only on `write`, so `acceptEdits` would
    have applied an edit from a provider with no recovery-receipt path without asking."""
    assert perm_modes.decide_action(
        "acceptEdits", {"tool_kind": "write", "tool": "write_file",
                        "recovery_available": True}) == "inline"
    assert perm_modes.decide_action(
        "acceptEdits", {"tool_kind": "write", "tool": "write_file"}) == "ask"


def test_the_backend_protocol_states_only_what_a_backend_must_implement():
    declared = {name for name in vars(VectorStore) if not name.startswith("_")}
    assert declared == {"upsert", "search"}


def test_the_in_memory_store_keeps_its_own_delete_and_rebuild():
    """They are that CLASS's API — one test exercises `delete` — just not a promise every future
    backend has to keep."""
    store = InMemoryVectorStore()
    store.upsert("i", [Item(id="a", vector=[1.0, 0.0]), Item(id="b", vector=[0.0, 1.0])])
    assert {hit.id for hit in store.search("i", [1.0, 0.0], 2)} == {"a"}
    store.delete("i", ["a"])
    assert store.search("i", [1.0, 0.0], 2) == []
    store.rebuild("i")          # in-memory has no derived state; must stay a no-op, not a raise


def test_run_tools_accepts_the_parent_argument_and_ignores_it():
    """`bind_state`'s second parameter is contractual — a provider that implements the hook without
    it raises TypeError at dispatch — but storing a value nobody reads implied a back-reference these
    read-only tools do not have."""
    tools = RunTools()
    sentinel = object()
    tools.bind_state(None, sentinel)          # must not raise
    assert not hasattr(tools, "parent")
    assert "parent" in inspect.signature(RunTools.bind_state).parameters
