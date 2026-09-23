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

import pytest

from looplab.agents import agent as agent_mod
from looplab.agents.loop_options import EXPLICIT_ONLY_LOOP_ARGS
from looplab.agents.roles import RESEARCHER_OUTPUT_ATTRS, researcher_budget_exhausted
from looplab.core.models import Idea, Node, NodeStatus, RunState

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
    reads = [call for call in ast.walk(fn)
             if isinstance(call, ast.Call)
             and getattr(call.func, "id", None) == "researcher_budget_exhausted"]
    assert reads, (
        "`_prepare_node_idea` must read the propose cutoff off the researcher — it is the one "
        "funnel every proposal crosses, and a carrier with no consumer is a field that ships while "
        "the record stays silent. It reads through `roles.researcher_budget_exhausted` rather than "
        "a bare getattr because the receipt now has TWO spellings and one object can carry both: "
        "see that helper and `RESEARCHER_OUTPUT_ATTRS`.")


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

    from looplab.agents.roles import researcher_budget_exhausted

    agent = UnifiedAgent.__new__(UnifiedAgent)
    agent.last_propose_budget_exhausted = ""       # what `__init__` establishes
    agent.researcher = _CutResearcher()
    agent.last_budget_exhausted = "time"           # a budget-cut Developer stamped the facade
    assert UnifiedAgent.propose(agent, None, None) is not None
    assert agent.last_propose_budget_exhausted == "turns", (
        "the funnel reads the facade, so the facade must carry THIS propose's bound")
    assert researcher_budget_exhausted(agent) == "turns"
    assert agent.last_budget_exhausted == "time", (
        "AND IT MUST NOT CLOBBER THE DEVELOPER'S. Under `unified_agent=True` these are ONE object: "
        "`_sync_audit` writes the repair receipt to `last_budget_exhausted` and "
        "`engine/evaluate.py` stamps the durable `node_repaired.budget_exhausted` from it, so a "
        "propose writing that name recorded a proposal's cutoff as a fact about a repair — "
        "reachable whenever the repair delegate raised, since `_evaluate` swallows that and "
        "`_sync_audit` is then unreached")

    class _ConvergedResearcher:
        last_budget_exhausted = ""

        def propose(self, state, parent):
            self.last_budget_exhausted = ""        # emitted on its own terms
            return object()

    agent.researcher = _ConvergedResearcher()
    UnifiedAgent.propose(agent, None, None)
    assert agent.last_propose_budget_exhausted == "", (
        "a stale cutoff surviving a converged propose is the repeated false-TRUNCATED warning "
        "coming back")
    assert researcher_budget_exhausted(agent) == "", (
        "and the reader must not fall back to the DEVELOPER's stamp, which is still 'time' here — "
        "the role-scoped name answering at all is what makes the fallback safe for a plain "
        "researcher and unreachable for the facade")


def test_the_attribute_is_REGISTERED_so_a_rename_cannot_be_silent():
    """Both sides read/write it with `getattr(..., default)`, so a one-sided rename fails silently —
    which is the entire argument `DEVELOPER_OUTPUT_ATTRS` was written down for.
    `tests/test_role_output_contract.py` scans producers and consumers against this tuple."""
    assert "last_budget_exhausted" in RESEARCHER_OUTPUT_ATTRS
    assert "last_propose_budget_exhausted" in RESEARCHER_OUTPUT_ATTRS, (
        "the role-scoped spelling is registered too: it is what keeps the propose receipt and the "
        "repair receipt apart on the ONE object the shipped default makes of both roles")


# ------------------------------------------------------------------ through the surrogate wrapper

_TOY_BOUNDS = {"x": (-10.0, 10.0), "y": (-10.0, 10.0)}


class _CutEveryCall:
    """A Researcher whose every propose is cut short by its turn budget — what `_note_cutoff`
    records on `ToolUsingResearcher` — and which can be made to raise mid-propose."""

    def __init__(self):
        self.calls = 0
        self.raises = False
        self.last_budget_exhausted = ""

    def propose(self, _state, _parent):
        self.calls += 1
        self.last_budget_exhausted = "turns"
        if self.raises:
            raise RuntimeError("the provider went away mid-propose")
        return Idea(operator="draft", params={"x": 0.5, "y": 0.5},
                    rationale="an LLM proposal", hypothesis="an LLM hypothesis")


def _warm_state(n: int = 5) -> RunState:
    """Enough evaluated history over the toy bounds for the surrogate to pass its warm-up (4)."""
    state = RunState(goal="g", direction="min")
    for i in range(n):
        state.nodes[i] = Node(id=i, operator="draft", status=NodeStatus.evaluated, feasible=True,
                              metric=float((i - 2) ** 2),
                              idea=Idea(operator="draft", params={"x": float(i), "y": -float(i)}))
    return state


def test_the_surrogate_reports_a_DELEGATED_cutoff_and_never_a_STALE_one():
    """Review 2026-09-22, W5-5 follow-up. Under `surrogate_proposer` / `policy=bohb` the engine's
    researcher handle IS `SurrogateResearcher`, which carried no receipt: a cut-short proposal its
    fallback made was never reported. Both halves, driven:

    * DELEGATED (below warm-up): the fallback's cutoff is THIS proposal's — reported;
    * NUMERIC (past warm-up): no call reached the fallback, whose receipt now describes an EARLIER
      proposal — so "" — and a delegate that RAISES leaves no receipt of an earlier call either.

    MUTATIONS, each red here: no pass-through (the delegated half reads ""); a read-through property
    to the fallback (the numeric half reads the stale "turns"); no reset on entry (the numeric and
    the raising halves keep the previous call's "turns")."""
    from looplab.search.surrogate import SurrogateResearcher

    cut = _CutEveryCall()
    surrogate = SurrogateResearcher(_TOY_BOUNDS, fallback=cut)
    surrogate.propose(RunState(goal="g", direction="min"), None)
    assert cut.calls == 1
    assert researcher_budget_exhausted(surrogate) == "turns"

    idea = surrogate.propose(_warm_state(), None)
    assert "surrogate-guided" in idea.rationale and cut.calls == 1, "a numeric point makes no call"
    assert cut.last_budget_exhausted == "turns", "precondition: the fallback's receipt is STALE now"
    assert researcher_budget_exhausted(surrogate) == ""

    surrogate.propose(RunState(goal="g", direction="min"), None)
    assert researcher_budget_exhausted(surrogate) == "turns"
    cut.raises = True
    with pytest.raises(RuntimeError):
        surrogate.propose(RunState(goal="g", direction="min"), None)
    assert researcher_budget_exhausted(surrogate) == ""


def test_the_proposal_funnel_warns_through_the_surrogate_only_for_a_delegated_cutoff(
        tmp_path, monkeypatch, caplog):
    """The READER the receipt exists for, driven: `_prepare_node_idea._link` on a run whose
    researcher handle is the surrogate. A delegated cut-short proposal must be reported TRUNCATED;
    a numeric point past warm-up — which never called the cut-short fallback — must not be.

    MUTATION: drop the surrogate's pass-through and the first half is silent again."""
    from looplab.adapters.toytask import ToyTask
    from looplab.events.replay import fold
    from looplab.search.surrogate import SurrogateResearcher
    from tests.factories import TOY_TASK, make_engine

    cut = _CutEveryCall()
    task = ToyTask.load(TOY_TASK)
    engine = make_engine(tmp_path / "surrogate-funnel", task=task,
                         researcher=SurrogateResearcher(task.bounds, fallback=cut))
    engine.store.append("run_started", {
        "run_id": engine.run_dir.name, "task_id": "toy", "goal": "g", "direction": "min"})
    monkeypatch.setattr(engine, "_apply_novelty_gate", lambda _state, idea, **_kw: idea)
    proposed: list = []
    surrogate_propose = engine.researcher.propose

    def _spy(state, parent):
        proposed.append(surrogate_propose(state, parent))
        return proposed[-1]

    monkeypatch.setattr(engine.researcher, "propose", _spy)

    def _truncation_warnings(node_id: int) -> list[str]:
        caplog.clear()
        with caplog.at_level("WARNING", logger="looplab.engine.orchestrator"):
            engine._prepare_node_idea(
                {"kind": "draft"}, fold(engine.store.read_all()), researcher=engine.researcher,
                prospective_node_id=node_id, source="researcher")
        return [r.getMessage() for r in caplog.records if "cut short by its" in r.getMessage()]

    assert _truncation_warnings(0), "a delegated cut-short proposal went unreported"
    assert cut.calls == 1
    for i in range(5):
        engine.store.append("node_created", {
            "node_id": i, "parent_ids": [], "operator": "draft", "code": "print(1)",
            "idea": {"operator": "draft", "params": {"x": float(i), "y": -float(i)}}})
        engine.store.append("node_evaluated", {
            "node_id": i, "generation": 0, "metric": float((i - 2) ** 2), "eval_seconds": 0.1})
    assert _truncation_warnings(5) == [], (
        "a numeric point that made no call was reported TRUNCATED off the fallback's stale receipt")
    assert len(proposed) == 2 and "surrogate-guided" in proposed[1].rationale, (
        "the second proposal must be the surrogate's own numeric point, or the half above is vacuous")
    assert cut.calls == 1, "past warm-up the surrogate proposed without calling the fallback"
