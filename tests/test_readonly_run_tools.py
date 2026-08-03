"""The one read-only RunTools builder five auxiliary LLM passes share (doc 25 CT-06).

`trust/verify.py`, the CLI's agentic tagging/briefing diagnostics, engine reflection/distillation,
the run report and the LLM novelty gate each wrote out `rt = RunTools(); rt.bind_state(state, None);
CompositeTools([rt])` inside a try/except that degraded to None. Two of the copies documented their
kinship in a comment ("mirrors trust.verify._verify_tools") instead of sharing the code.

What matters is not the five lines. It is that `bind_state(state, parent)` takes a SECOND argument
(`tools/_base.py`: a provider that implements the hook and omits it raises TypeError at dispatch) and
that failure degrades to None rather than raising — grounding is best-effort and every caller has a
plain non-agentic path. Both of those are cross-package contracts that used to have to be found by
grep in five places.
"""
from __future__ import annotations

from _source_scan import iter_sources

import pytest

from looplab.core.models import RunState
from looplab.tools.run_tools import RunTools, readonly_run_tools


def test_the_builder_returns_tools_that_actually_expose_the_run_readers():
    """The point of binding at all: the model can read the run's real experiments before judging."""
    tools = readonly_run_tools(RunState())
    assert tools is not None
    names = {spec["function"]["name"] for spec in tools.specs()}
    assert {"read_code", "read_experiment", "read_logs", "list_experiments"} <= names


def test_the_state_is_bound_so_the_readers_see_THIS_run(tmp_path):
    """An unbound provider answers about nothing — the whole grounding claim would be empty."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "improve",
                                  "idea": {"operator": "improve", "params": {},
                                           "rationale": "a bound experiment"}})
    store.append("node_evaluated", {"node_id": 0, "metric": 0.42})

    tools = readonly_run_tools(fold(store.read_all()))
    listed = tools.execute("list_experiments", {})
    assert "#0 improve metric=0.42" in listed
    assert "a bound experiment" in tools.execute("read_experiment", {"node_id": 0})


def test_bind_state_is_called_with_the_parent_argument():
    """`tools/_base.py`: a provider that implements `bind_state` and omits the second `parent`
    argument raises TypeError at dispatch. Passing it positionally here is what keeps every one of
    the five call sites on the right side of that contract."""
    seen = []
    original = RunTools.bind_state
    try:
        RunTools.bind_state = lambda self, state, parent=object(): seen.append((state, parent))
        state = RunState()
        readonly_run_tools(state)
    finally:
        RunTools.bind_state = original
    assert seen == [(state, None)]


@pytest.mark.parametrize("boom", [RuntimeError("provider exploded"), MemoryError("no room")])
def test_a_builder_failure_degrades_to_none_rather_than_raising(boom):
    """The shared contract, and the reason this is one function: every caller falls back to a plain
    non-agentic call, so a failure here must never take down the verify / report / distill pass that
    merely wanted grounding. The except is deliberately broad — a provider is third-party-ish code
    and `MemoryError` is as fatal to grounding as a `RuntimeError`."""
    original = RunTools.bind_state

    def _explode(self, state, parent=None):
        raise boom

    try:
        RunTools.bind_state = _explode
        assert readonly_run_tools(RunState()) is None
    finally:
        RunTools.bind_state = original


def test_cancellation_is_not_swallowed_into_a_silent_degrade():
    """A shutdown must end the pass, not be reported as "grounding unavailable" — `BaseException`
    is outside the except by construction."""
    original = RunTools.bind_state

    class _Cancelled(BaseException):
        pass

    def _cancel(self, state, parent=None):
        raise _Cancelled()

    try:
        RunTools.bind_state = _cancel
        with pytest.raises(_Cancelled):
            readonly_run_tools(RunState())
    finally:
        RunTools.bind_state = original


def test_no_caller_hand_builds_a_lone_run_tools_composite():
    """A grep guard on the collapse: a LONE-`RunTools` composite outside `run_tools.py` is a sixth
    copy. Multi-provider composites (RunTools + DataTools / SiblingRunTools) are a different thing
    and stay where they are — this guard is deliberately narrow enough not to catch them."""
    import re
    from pathlib import Path

    lone = re.compile(r"CompositeTools\(\s*\[\s*(rt|RunTools\(\))\s*\]")
    root = Path(__file__).resolve().parents[1] / "looplab"
    offenders = [f"{path.relative_to(root)}:{index}"
                 for path, source in iter_sources(root)
                 for index, line in enumerate(source.split("\n"), 1)
                 if lone.search(line) and path.name != "run_tools.py"]
    assert offenders == [], (
        f"read-only RunTools built by hand outside `readonly_run_tools`: {offenders}")


@pytest.mark.parametrize("module,wrapper", [
    ("looplab.trust.verify", "_verify_tools"),
    ("looplab.cli.concept_cmds", "_run_tools_for"),
    ("looplab.serve.report", "_report_tools"),
])
def test_each_named_wrapper_is_now_a_delegate(module, wrapper):
    """The wrappers keep their names and their per-site "why" docstrings — those are load-bearing —
    but their bodies must be the shared builder, not a private re-derivation of it."""
    import importlib
    import inspect

    source = inspect.getsource(getattr(importlib.import_module(module), wrapper))
    assert "readonly_run_tools(state)" in source
    assert "bind_state" not in source, f"{module}.{wrapper} still binds the provider itself"


def test_the_novelty_gate_never_pays_for_a_BLIND_adjudication(tmp_path, monkeypatch):
    """The one caller whose degradation legitimately differs, and the reason it must be an explicit
    `if tools is None` rather than "the try/except will catch it anyway": `agentic_struct` does NOT
    raise on `tools=None` against a real client — it makes the round-trip with no tools at all. The
    gate would then pay for an adjudicator judging duplication BLIND, from the prompt summary alone,
    which is exactly the heuristic `novelty_mode="llm"` exists to replace.
    """
    from looplab.agents import agent as agent_module
    import looplab.engine.novelty as novelty_mod
    from looplab.core.models import Idea
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "improve",
                                  "idea": {"operator": "improve", "params": {}, "rationale": "x"}})
    state = fold(store.read_all())

    # A model that would happily call EVERY proposal a duplicate — reachable only if the gate spends
    # the call. The real one is not this cooperative, which is what made the break invisible.
    monkeypatch.setattr(agent_module, "agentic_struct", lambda *a, **k: pytest.fail(
        "the gate paid for an adjudication with no tools bound"))

    gate = novelty_mod.NoveltyGateMixin()
    gate._reflect_client = lambda: object()
    idea = Idea(operator="improve", params={}, rationale="a fresh proposal")

    original = RunTools.bind_state
    try:
        RunTools.bind_state = _explode_bind
        assert gate._llm_novelty_gate(state, idea, repropose=lambda: idea) is idea
    finally:
        RunTools.bind_state = original


def _explode_bind(self, state, parent=None):
    raise RuntimeError("no grounding available")
