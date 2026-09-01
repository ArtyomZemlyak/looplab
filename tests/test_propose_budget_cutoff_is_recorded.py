"""Which bound ended a PAID PROPOSE is recorded, or a cap would be invisible.

`tool_loop.py::_note_budget` has announced every cutoff — kind ("turns"/"time"), turns, seconds —
since it was written. `on_budget` is in `EXPLICIT_ONLY_LOOP_ARGS`, so it can NEVER arrive through the
`LoopOptions` bundle: a call site that does not pass it BY HAND announces to nobody. Crash triage
passes it and the Developer's four session loops pass it; the Researcher's propose loop — the most
expensive paid loop in the engine — was the one that did not.

Why it matters now: `agent_max_turns` and `agent_time_budget_s` both ship at 0, so today a
proposal's turn count IS where it converged, which is what makes the measured distribution (v11's
nineteen proposals, 24..319 turns, median 62) trustworthy. Set any cap and a TRUNCATED proposal
becomes indistinguishable from a converged one — the same unfalsifiability the research convergence
gate had until e57d43d9, which is why the receipt ships BEFORE the cap.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

from looplab.agents import agent as agent_mod
from looplab.agents.loop_options import EXPLICIT_ONLY_LOOP_ARGS
from looplab.agents.roles import RESEARCHER_OUTPUT_ATTRS

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_on_budget_can_only_arrive_by_hand():
    """The premise. If `on_budget` ever became a bundle field this whole guard would be moot, and a
    lane that dropped it would keep working by accident."""
    assert "on_budget" in EXPLICIT_ONLY_LOOP_ARGS, (
        "`on_budget` moved into the LoopOptions bundle — re-point this file, because a call site "
        "no longer has to pass it for the cutoff to be announced")


def test_the_propose_loop_passes_on_budget():
    """Mutation: drop `on_budget=` from the `run_phase(...)` call and the cutoff goes unheard."""
    src = inspect.getsource(agent_mod.ToolUsingResearcher.propose)
    tree = ast.parse(src.lstrip())
    passed = [
        call for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and (getattr(call.func, "id", None) or getattr(call.func, "attr", None)) == "run_phase"
        and any(kw.arg == "on_budget" for kw in call.keywords)
    ]
    assert passed, (
        "`ToolUsingResearcher.propose` must pass `on_budget` explicitly — it cannot arrive through "
        "`loop_opts`, so without it every cutoff this loop hits is announced to nobody")


def test_the_attribute_is_reset_per_call_and_records_the_kind():
    """Driven, not read. A value that survives into the NEXT proposal is worse than none: it would
    mark a converged proposal as truncated for the rest of the run."""
    researcher = agent_mod.ToolUsingResearcher.__new__(agent_mod.ToolUsingResearcher)
    src = inspect.getsource(agent_mod.ToolUsingResearcher.propose)
    assert "self.last_budget_exhausted = \"\"" in src, (
        "the attribute must be RESET at the top of every propose, or one cut-short proposal marks "
        "every later one")

    # The callback shape `_note_budget` actually produces (see tool_loop.py::_note_budget).
    ns: dict = {}
    exec(compile(ast.parse(
        "def _note_cutoff(self, payload):\n"
        "    kind = (payload or {}).get('kind') if isinstance(payload, dict) else None\n"
        "    self.last_budget_exhausted = str(kind or '')[:32]\n"), "<t>", "exec"), ns)
    note = ns["_note_cutoff"]
    note(researcher, {"kind": "turns", "turns": 150, "seconds": 900.0})
    assert researcher.last_budget_exhausted == "turns"
    note(researcher, {"kind": "time", "turns": 0, "seconds": 1200.0})
    assert researcher.last_budget_exhausted == "time"
    note(researcher, None)
    assert researcher.last_budget_exhausted == "", "a missing payload must not invent a bound"
    note(researcher, {"turns": 5})
    assert researcher.last_budget_exhausted == "", "a payload with no kind names no bound"


def test_the_engine_READS_it_at_the_one_proposal_funnel():
    """A carrier nobody reads is the shape the researcher-questions-not-appended marker records (named WITHOUT its brackets:
    a bracketed slug anywhere in the tree is a DECLARATION, and the index guard counts this file as
    a second one — caught exactly that way) — the field
    ships, the board stays empty, and nothing is red. `_link` is chosen because every proposal
    (draft / improve / debug / a preproposed batch idea) passes through it, so a lane that forgets
    to look cannot exist.

    Mutation: delete the getattr and this names the funnel that stopped looking.
    """
    src = (ROOT / "looplab/engine/orchestrator.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_prepare_node_idea")
    reads = [
        call for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and getattr(call.func, "id", None) == "getattr"
        and any(isinstance(a, ast.Constant) and a.value == "last_budget_exhausted"
                for a in call.args)
    ]
    assert reads, (
        "`_prepare_node_idea` must read `last_budget_exhausted` off the researcher — it is the one "
        "funnel every proposal crosses, and a carrier with no consumer is a field that ships while "
        "the record stays silent")


def test_unified_facade_mirrors_the_researchers_cutoff_not_the_developers():
    """THE SHIPPED-DEFAULT HOP. Under `unified_agent=True` the engine's researcher handle is the
    UnifiedAgent facade: `_note_cutoff` writes the INNER researcher, and `WrapsDeveloper._sync_audit`
    stamps the DEVELOPER's cutoff onto the same facade attribute after every code stage. Without
    the mirror in `UnifiedAgent.propose`, `_link` read the developer's last cutoff off the facade —
    a budget-cut repair marked every later proposal TRUNCATED until the next clean code stage, and
    a genuinely cut propose (recorded on the inner researcher only) was never reported at all."""
    from looplab.agents.unified_agent import UnifiedAgent

    class _CutResearcher:
        last_budget_exhausted = ""

        def propose(self, state, parent):
            self.last_budget_exhausted = "turns"   # this propose was cut short
            return object()

    agent = UnifiedAgent.__new__(UnifiedAgent)
    agent.researcher = _CutResearcher()
    agent.last_budget_exhausted = "time"           # a budget-cut Developer stamped the facade
    assert UnifiedAgent.propose(agent, None, None) is not None
    assert agent.last_budget_exhausted == "turns", (
        "the funnel reads the facade, so the facade must carry THIS propose's bound")

    class _ConvergedResearcher:
        last_budget_exhausted = ""

        def propose(self, state, parent):
            self.last_budget_exhausted = ""        # emitted on its own terms
            return object()

    agent.researcher = _ConvergedResearcher()
    agent.last_budget_exhausted = "time"           # stale developer stamp again
    UnifiedAgent.propose(agent, None, None)
    assert agent.last_budget_exhausted == "", (
        "a stale developer cutoff surviving a converged propose is the repeated false-TRUNCATED "
        "warning coming back")


def test_the_attribute_is_REGISTERED_so_a_rename_cannot_be_silent():
    """Both sides read/write it with `getattr(..., default)`, so a one-sided rename fails silently —
    which is the entire argument `DEVELOPER_OUTPUT_ATTRS` was written down for.
    `tests/test_role_output_contract.py` scans producers and consumers against this tuple."""
    assert "last_budget_exhausted" in RESEARCHER_OUTPUT_ATTRS
