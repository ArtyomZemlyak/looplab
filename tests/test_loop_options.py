"""The typed `drive_tool_loop` options bundle (doc 25 AG-01).

The property this file exists for is one specific, already-paid-for defect. `drive_tool_loop` takes
26 parameters; its configuration used to travel as an untyped dict that call sites `**`-spread
*beside* explicit keywords:

    drive_tool_loop(..., context_budget_chars=self.context_budget_chars, **loop_opts)

`loop_opts_from_settings` also injects `context_budget_chars` (the `Settings` default is non-None, so
it is ALWAYS there), which makes that `TypeError: got multiple values for keyword argument`. Nobody
saw a crash: `ToolUsingResearcher.propose` wraps its loop in a broad `except Exception` that degrades
to a bounds-filled draft Idea, so the agentic Researcher was simply DEAD in the default config —
every node a "fallback (agent parse failed)" no-op and the only symptom a flat metric.

So the tests below are ordered by how much they would actually catch:

  1. DRIVEN. Each agentic entry point is run against a real `drive_tool_loop` with a bundle carrying
     EVERY option, and asserted to reach the loop rather than its containment fallback. This is the
     bug, reproduced at full strength: the historical bundle only carried 10 of the 12 options, so
     `max_turns` / `time_budget_s` were a latent second copy of it waiting for someone to put a turn
     cap in a bundle (`serve/report.py`, `trust/judge.py`, `engine/genesis.py` and five others
     already do).
  2. STRUCTURAL, by AST. The rule that makes the shape impossible rather than merely fixed: no call
     in `looplab/` may pass an option BOTH as an explicit keyword and through a `**` spread. Comments
     are not AST nodes, so this cannot be satisfied by one.
  3. The registry partition, in both directions, so a new `drive_tool_loop` parameter has to declare
     which side it is on instead of becoming a knob no bundle can carry.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from looplab.agents.agent import (
    LoopOptions, ToolUsingResearcher, agentic_struct, agentic_text, drive_tool_loop, emit_loop,
    loop_opts_from_settings)
from looplab.agents.loop_options import EXPLICIT_ONLY_LOOP_ARGS, LOOP_OPTION_FIELDS, UNSET
from looplab.core.config import Settings
from looplab.core.models import RunState
from tests._source_scan import iter_trees

# A bundle carrying EVERY option, so a call site that also passes one explicitly collides on it.
# Values are the shipped defaults where they exist, chosen so the loop behaves normally: the fake
# clients below emit on their first turn, so no threshold is actually reached.
FULL = LoopOptions(max_turns=5, time_budget_s=60.0, context_budget_chars=1_000_000,
                   stuck_detection=True, stuck_repeat=4, stuck_alternate=4,
                   self_plan=True, plan_reinject_every=5, auto_summary=True,
                   summary_client=None, emit_after=300, emit_force=500,
                   read_loop_nudge_after=25)


def _tool_call(name: str, args: dict) -> dict:
    import json
    return {"tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


class _EmitsAtOnce:
    """A client that answers the FIRST turn with the emit call, so the loop returns `finalize(args)`.

    It deliberately has no `complete_tool` / `complete_text`: every degraded path in the roles under
    test (the forced-emit salvage, the plain-parse fallback) would fail on this client, so a result
    that is not the emitted one proves the loop was never reached.
    """

    def __init__(self, args: dict, emit_name: str = "emit"):
        self._args = args
        self._emit_name = emit_name
        self.turns: list = []

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.turns.append(list(messages))
        return _tool_call(self._emit_name, self._args)


# --------------------------------------------------------------------------- 1. the bug, driven

def test_the_researcher_survives_a_bundle_that_carries_every_loop_option():
    """The exact failure this change closes, at full strength.

    `ToolUsingResearcher` takes `context_budget_chars`, `max_turns` and `time_budget_s` as ctor
    kwargs AND accepts a bundle that can carry all three. Passing both to `run_phase` hands it the
    keyword twice; `propose`'s broad `except Exception` swallows the TypeError and returns a draft
    Idea, which is indistinguishable from a weak model's bad JSON.
    """
    client = _EmitsAtOnce({"operator": "tweak", "params": {"x": 1.0}, "rationale": "REACHED"})
    researcher = ToolUsingResearcher(client, None, context_budget_chars=1_000_000,
                                     max_turns=7, time_budget_s=30.0, loop_opts=FULL)
    idea = researcher.propose(RunState(goal="g", direction="min"), None)
    assert idea.rationale == "REACHED", "the loop was never reached — the emit degraded to fallback"
    assert client.turns, "no LLM turn happened at all"


def test_the_researchers_ctor_kwargs_lose_to_the_bundle_rather_than_colliding_with_it():
    """The merge has a NAMED precedence now, and it is the one the old `setdefault` already had.

    A bundle value came from the operator's config; a ctor default (`max_turns=0` = unlimited) must
    not silently override it. Pinned in both directions so a future switch to `.replace()` — which
    would make an unpassed ctor default win — is a red test, not a quiet re-cap of every loop.
    """
    configured = ToolUsingResearcher(object(), None, loop_opts=LoopOptions(max_turns=11))
    assert configured.loop_opts["max_turns"] == 11, "the bundle's configured value must win"

    from_ctor = ToolUsingResearcher(object(), None, max_turns=11)
    assert from_ctor.loop_opts["max_turns"] == 11, "an unset bundle takes the ctor's value"


def test_the_strategist_survives_a_bundle_that_carries_every_loop_option():
    """Its containment tail degrades to the deterministic RuleStrategist, so a collision here reads
    as "the model can't drive tools" — the agentic Strategist silently reduced to stats-only."""
    from looplab.agents.strategist import StrategyContext, ToolUsingStrategist

    client = _EmitsAtOnce({"policy": "greedy", "rationale": "REACHED"})
    strategist = ToolUsingStrategist(client, tools=None, context_budget_chars=1_000_000,
                                     max_turns=7, time_budget_s=30.0, loop_opts=FULL)
    out = strategist.decide(RunState(goal="g", direction="min"), StrategyContext(node_count=3))
    assert out is not None and out.get("source") == "agent", (
        "the agentic strategist fell through to the rule baseline")


def test_the_deep_researcher_survives_a_bundle_that_carries_every_loop_option():
    client = _EmitsAtOnce({"summary": "REACHED"})
    from looplab.agents.deep_research import DeepResearcher

    memo = DeepResearcher(client, tools=None, loop_opts=FULL).research(RunState(goal="g"))
    assert memo.summary == "REACHED"


def test_the_agentic_upgrades_survive_a_bundle_that_carries_every_loop_option():
    """`agentic_text`/`agentic_struct` contain their failures too (they degrade to the plain
    single-shot call), so a duplicate keyword there is invisible in exactly the same way."""
    from pydantic import BaseModel

    class _Tools:
        def specs(self):
            return []

        def execute(self, name, args):
            return "(no tools)"

    text = agentic_text(_EmitsAtOnce({"text": "REACHED"}, emit_name="answer"), _Tools(),
                        [{"role": "user", "content": "go"}], loop_opts=FULL)
    assert text == "REACHED"

    class _Out(BaseModel):
        note: str = ""

    out = agentic_struct(_EmitsAtOnce({"note": "REACHED"}), _Tools(),
                         [{"role": "user", "content": "go"}], _Out, loop_opts=FULL)
    assert out.note == "REACHED"


def test_emit_loop_survives_a_bundle_that_carries_the_turn_limits(monkeypatch):
    """`emit_loop` reads the limits off Settings AND spreads the bundle; the limits are folded in as
    DEFAULTS, so a bundle that already carries one wins instead of colliding with it."""
    from pydantic import BaseModel

    from looplab.agents import tool_loop

    class _Plan(BaseModel):
        reply: str = ""

    class _Settings:
        agent_max_turns = 7
        agent_time_budget_s = 12.5

    monkeypatch.setattr(tool_loop, "loop_opts_from_settings", lambda _s: FULL)
    plan = emit_loop(_EmitsAtOnce({"reply": "REACHED"}), None,
                     [{"role": "user", "content": "go"}], _Plan, _Settings(), description="d")
    assert plan.reply == "REACHED"


def test_the_settings_bundle_still_spreads_exactly_the_keys_it_always_did():
    """`loop_opts_from_settings` returns a `LoopOptions` now; `**` over it must be byte-identical to
    the dict it replaced, or every call site changes behaviour at once."""
    opts = loop_opts_from_settings(Settings())
    assert dict(opts) == {
        "stuck_detection": True, "stuck_repeat": 4, "stuck_alternate": 4,
        "self_plan": True, "plan_reinject_every": 5, "auto_summary": True,
        "emit_after": 300, "emit_force": 500,
        "context_budget_chars": Settings().context_budget_chars,
    }

    class _Bare:
        pass

    # An unset option is ABSENT, not None: the loop's own default has to survive a stub settings
    # object, and `with_defaults` has to be able to tell "never set" from "set to None".
    assert "context_budget_chars" not in loop_opts_from_settings(_Bare())
    assert "summary_client" not in loop_opts_from_settings(_Bare())


# ------------------------------------------------------------------ 2. the rule, structurally

_LOOP_CALL_NAMES = frozenset({"drive_tool_loop", "run_phase"})


def _loop_calls():
    """Every call to the loop (or its handoff wrapper) in `looplab/`, however it is spelled."""
    for path, tree in iter_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _LOOP_CALL_NAMES:
                yield path, node


def test_no_call_site_passes_a_loop_option_both_ways():
    """The rule that makes the collision impossible instead of merely fixed.

    An option may travel ONLY in the bundle. So a call that spreads a mapping must not also name an
    option as an explicit keyword — that pair is the defect, and it is undetectable at the call site
    because the two spellings look unrelated until the bundle happens to carry the key.
    """
    offenders = []
    for path, call in _loop_calls():
        if not any(kw.arg is None for kw in call.keywords):     # no `**` spread -> nothing to collide
            continue
        for kw in call.keywords:
            if kw.arg in LOOP_OPTION_FIELDS:
                offenders.append(f"{path.name}:{call.lineno} passes {kw.arg}= beside a ** spread")
    assert not offenders, (
        "a loop OPTION must ride the bundle, never sit beside it:\n  " + "\n  ".join(offenders))


def test_every_loop_call_site_is_actually_seen_by_the_scan():
    """A guard that finds nothing because its matcher is wrong is worse than no guard. The loop is
    called through three spellings (`drive_tool_loop`, `_agent.drive_tool_loop`, `run_phase`), so
    pin that the scan reaches all of them, in the files that hold them."""
    seen = {path.name for path, _ in _loop_calls()}
    assert {"tool_loop.py", "agent.py", "strategist.py", "deep_research.py", "unified_agent.py",
            "assistant.py", "repo_developer.py"} <= seen, f"scan missed call sites: {seen}"


# ---------------------------------------------------------------------- 3. the registry partition

def test_the_two_registries_partition_the_loops_keyword_only_parameters():
    """Two-way, like every other registry-guarded seam here: a new `drive_tool_loop` parameter must
    declare whether it is bundle-carried or per-call, and a registry entry must name a real one."""
    signature = inspect.signature(drive_tool_loop)
    kwonly = {name for name, p in signature.parameters.items()
              if p.kind is inspect.Parameter.KEYWORD_ONLY}
    declared = set(LOOP_OPTION_FIELDS) | set(EXPLICIT_ONLY_LOOP_ARGS)
    assert kwonly - declared == set(), f"undeclared drive_tool_loop parameters: {kwonly - declared}"
    assert declared - kwonly == set(), f"registered but not a loop parameter: {declared - kwonly}"
    assert not set(LOOP_OPTION_FIELDS) & set(EXPLICIT_ONLY_LOOP_ARGS), "an argument cannot be both"


def test_a_prompt_contract_can_never_ride_the_bundle():
    """Prompt strings are contracts (CLAUDE.md). `nudge_prompt`/`stuck_prompt` are the two mid-loop
    user nudges a caller folded onto this loop keeps byte-identical; a config bundle that could
    carry them would let a settings file reword an agent's prompt."""
    assert "nudge_prompt" in EXPLICIT_ONLY_LOOP_ARGS and "stuck_prompt" in EXPLICIT_ONLY_LOOP_ARGS
    with pytest.raises(TypeError, match="nudge_prompt"):
        LoopOptions.coerce({"nudge_prompt": "say something else"})


# ------------------------------------------------------------------------------- 4. loudness

def test_an_unknown_option_is_refused_by_name_at_the_boundary():
    """The bundle used to be a plain dict, so `{"stuck_retries": 3}` — a typo of `stuck_repeat` —
    rode all the way to the call. (The suite's own `emit_loop` fixture used that misspelling.)"""
    with pytest.raises(TypeError, match="stuck_retries"):
        LoopOptions.coerce({"stuck_repeat": 3, "stuck_retries": 3})
    with pytest.raises(TypeError, match="stuck_retries"):
        LoopOptions().replace(stuck_retries=3)
    with pytest.raises(TypeError, match="stuck_retries"):
        LoopOptions().with_defaults(stuck_retries=3)
    with pytest.raises(TypeError, match="stuck_retries"):
        LoopOptions().without("stuck_retries")


def test_a_per_call_argument_is_refused_with_a_message_that_says_why():
    """"`finalize` is not an option" is a more confusing mistake than a typo — it IS a real loop
    parameter — so the refusal names the difference instead of only listing the valid set."""
    with pytest.raises(TypeError, match="pass it explicitly at the call site"):
        LoopOptions.coerce({"finalize": lambda a: a})


def test_a_role_refuses_a_bad_bundle_where_nothing_is_catching_it():
    """Coercion happens in `__init__`, not at the drive call: inside `propose`'s containment
    `except` a refusal would degrade to a draft Idea, which is how the original bug hid."""
    with pytest.raises(TypeError, match="stuck_retries"):
        ToolUsingResearcher(object(), None, loop_opts={"stuck_retries": 3})


# ------------------------------------------------------------------------- 5. merge semantics

def test_replace_wins_and_with_defaults_yields():
    base = LoopOptions(max_turns=5)
    assert base.replace(max_turns=9)["max_turns"] == 9
    assert base.with_defaults(max_turns=9)["max_turns"] == 5
    assert base.with_defaults(self_plan=False)["self_plan"] is False

    # `None` is a VALUE for several options ("unset, use the built-in default" vs "0 = off"), so
    # with_defaults must not treat an explicitly-None field as free to overwrite.
    explicit_none = LoopOptions(context_budget_chars=None)
    assert "context_budget_chars" in explicit_none
    assert explicit_none.with_defaults(context_budget_chars=9)["context_budget_chars"] is None
    assert LoopOptions().with_defaults(context_budget_chars=9)["context_budget_chars"] == 9


def test_without_restores_the_loops_own_default():
    """The DeepResearcher's `summary_client` divergence: that stage has always compacted with its
    own client, and the untyped dict had no way to say "everything except this one"."""
    assert "summary_client" in FULL
    assert "summary_client" not in FULL.without("summary_client")
    assert FULL.without("summary_client").replace(self_plan=False)["self_plan"] is False


def test_the_bundle_is_frozen_so_a_shared_one_cannot_be_mutated_under_another_caller():
    """`loop_opts_from_settings`'s result is reused across roles; the old dict could be mutated by
    whichever call site got it first (two did exactly that with `opts["max_turns"] = …`)."""
    with pytest.raises(Exception):        # dataclasses.FrozenInstanceError
        FULL.max_turns = 1                # type: ignore[misc]
    with pytest.raises(TypeError):
        FULL["max_turns"] = 1             # type: ignore[index]


def test_the_cost_roll_up_still_reaches_the_compressor_through_the_bundle():
    """MONEY. The D11 compressor client rides `summary_client` INSIDE the bundle, and the engine's
    cost roll-up walks there by attribute name (`costs._CHILD_ATTRS`) — a `dict` branch it no longer
    matches, since a `LoopOptions` is a Mapping and not a dict. Reaching it by attribute is what
    keeps every compression call billed; if it stopped, the spend would simply vanish from the ledger
    rather than fail, which is the failure mode nobody notices.
    """
    from looplab.engine.costs import find_cost_accountants

    class _Accountant:
        pass

    class _Client:
        def __init__(self, accountant):
            self.accountant = accountant

    accountant = _Accountant()

    class _Researcher:
        loop_opts = LoopOptions(self_plan=True, summary_client=_Client(accountant))

    class _Engine:
        researcher = _Researcher()

    assert find_cost_accountants(_Engine()) == [accountant]


def test_unset_is_falsy_so_an_empty_bundle_keeps_the_legacy_or_fallback():
    """Several sites read `getattr(self, "loop_opts", {}) or {}`. An empty bundle has to stay falsy
    or that idiom silently changes which object is spread."""
    assert not LoopOptions()
    assert LoopOptions(self_plan=False)          # a SET field makes it truthy, even at a falsy value
    assert UNSET is not None and not UNSET
