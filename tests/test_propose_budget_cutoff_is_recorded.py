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

    Mutation: delete the read and this names the funnel that stopped looking.
    """
    # The one funnel lives with the build spine in `node_build.py` since ENG1-04 step 4c.
    src = (ROOT / "looplab/engine/node_build.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_prepare_node_idea")
    called = {getattr(call.func, "id", None) for call in ast.walk(fn) if isinstance(call, ast.Call)}
    assert {"propose_receipt_scope", "scoped_budget_exhausted"} <= called, (
        "`_prepare_node_idea` must read the propose cutoff — it is the one funnel every proposal "
        "crosses, and a carrier with no consumer is a field that ships while the record stays "
        "silent. It reads the call's OWN receipt (`propose_receipts.propose_receipt_scope` + "
        "`scoped_budget_exhausted`, doc 69 69.37): the role's attributes are a shared instance's, "
        "and they are read only when nothing inside the scope noted a receipt.")


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


def test_the_endgame_sweep_reads_its_OWN_receipt_not_the_researchers_stale_one(
        tmp_path, monkeypatch, caplog):
    """The champion sweep proposes through its own surrogate (`orchestrator.py::_sweep_researcher`)
    while the funnel read the receipt off `researcher` — the handle the sweep WRAPS. Past warm-up
    the sweep's point makes no call, so that read reported the Researcher's LAST cut-short proposal
    against it: driven before the fix, two "cut short" warnings for a sweep node whose fallback was
    never called. Below warm-up the sweep delegates, and a cut-short delegated proposal must still
    be reported.

    And WHICH handle is asked, spied at the funnel: the sweep's surrogate for its own point, and
    `researcher` again once the novelty gate re-proposes through it.

    MUTATIONS: propose the sweep through `researcher` in `_propose` -> the warm half warns; drop the
    gate's receipt hand-back -> the final candidate keeps the sweep's receipt. Each propose is
    asked ONCE, at its own call (doc 69 69.37); the final candidate carries the receipt it was
    proposed with instead of asking again."""
    # Patched where `_prepare_node_idea` READS it — its module since ENG1-04 step 4c.
    import looplab.engine.node_build as build_module
    from looplab.adapters.toytask import ToyTask
    from looplab.engine.plan import META_SWEEP
    from looplab.events.replay import fold
    from tests.factories import TOY_TASK, make_engine

    cut = _CutEveryCall()
    engine = make_engine(tmp_path / "sweep", task=ToyTask.load(TOY_TASK), researcher=cut,
                         endgame_reserve_frac=0.25)
    engine.store.append("run_started", {
        "run_id": engine.run_dir.name, "task_id": "toy", "goal": "g", "direction": "min"})
    monkeypatch.setattr(engine, "_apply_novelty_gate", lambda _state, idea, **_kw: idea)
    asked: list = []
    real_reader = build_module.scoped_budget_exhausted

    def _asked(box, handle):
        asked.append(handle)
        return real_reader(box, handle)

    monkeypatch.setattr(build_module, "scoped_budget_exhausted", _asked)

    def _evaluated(i: int) -> None:
        engine.store.append("node_created", {
            "node_id": i, "parent_ids": [], "operator": "draft", "code": "print(1)",
            "idea": {"operator": "draft", "params": {"x": float(i), "y": -float(i)}}})
        engine.store.append("node_evaluated", {
            "node_id": i, "generation": 0, "metric": float((i - 2) ** 2), "eval_seconds": 0.1})

    def _sweep(parent_id: int, node_id: int):
        caplog.clear()
        asked.clear()
        with caplog.at_level("WARNING", logger="looplab.engine.orchestrator"):
            idea = engine._prepare_node_idea(
                {"kind": "improve", "parent_id": parent_id, META_SWEEP: True},
                fold(engine.store.read_all()), researcher=engine.researcher,
                prospective_node_id=node_id, source="researcher")
        return idea, [r.getMessage() for r in caplog.records if "cut short by its" in r.getMessage()]

    for i in range(2):
        _evaluated(i)
    _idea, warned = _sweep(1, 2)                   # below warm-up: the sweep DELEGATES
    assert cut.calls == 1 and warned, "a delegated cut-short sweep proposal went unreported"
    for i in range(2, 5):
        _evaluated(i)
    idea, warned = _sweep(2, 5)                    # past warm-up: its own numeric point
    assert idea is not None and "surrogate-guided" in idea.rationale and cut.calls == 1
    assert cut.last_budget_exhausted == "turns", "precondition: the Researcher's receipt is STALE"
    assert warned == [], "a sweep point that made no call was reported TRUNCATED"
    sweeper = engine._sweep_researcher(cut)
    assert asked == [sweeper], "the funnel must ask the handle that proposed"

    # The novelty gate re-proposes through `researcher` (cut short again): from then on the funnel
    # asks `researcher`, for the re-proposal and for the final candidate it became.
    monkeypatch.setattr(engine, "_apply_novelty_gate",
                        lambda _state, _idea, *, repropose=None, **_kw: repropose())
    _idea, warned = _sweep(2, 5)
    assert cut.calls == 2 and warned, "the gate's cut-short re-proposal went unreported"
    assert asked == [sweeper, cut], asked
    assert len(warned) == 2, "the re-proposal and the final candidate it became, each once"


# ------------------------------------------------------------ the call's own receipt (doc 69 69.37)

def test_a_scope_holds_the_receipts_of_the_proposes_inside_it_and_no_other_thread_s():
    """The channel's truth table: a propose notes into the scope its CALLER opened; the last note
    is the call's receipt; a note outside any scope, or on another thread, reaches no scope; with
    nothing noted the role's attributes answer (a researcher that predates the channel)."""
    import threading

    from looplab.agents.propose_receipts import (note_propose_receipt, propose_receipt_scope,
                                                 scoped_budget_exhausted)

    note_propose_receipt("turns")                          # outside any scope: nothing, no error
    with propose_receipt_scope() as box:
        note_propose_receipt("turns")
        worker = threading.Thread(target=lambda: note_propose_receipt("time"))
        worker.start()
        worker.join()
        with propose_receipt_scope() as inner:
            note_propose_receipt("tokens")
        note_propose_receipt("")
    assert (box, inner) == (["turns", ""], ["tokens"])
    plain = type("R", (), {"last_budget_exhausted": "time"})()
    assert scoped_budget_exhausted(box, plain) == ""       # the call's own, not the attribute
    assert scoped_budget_exhausted([], plain) == "time"    # nothing noted: the attribute answers


class _SharedAndRacing:
    """ONE Researcher shared by two lanes. Lane A's propose is cut short; before A's caller reads its
    receipt, lane B's whole propose runs on another thread and ends cleanly — rewriting the shared
    attribute, as the card lane and the offloaded serial build do (critic crit_v53 N6)."""

    def __init__(self):
        self.calls = 0
        self.last_budget_exhausted = ""

    def propose(self, state, parent):
        import threading

        from looplab.agents.propose_receipts import note_propose_receipt
        self.calls += 1
        bound = "turns" if threading.current_thread().name == "lane-A" else ""
        self.last_budget_exhausted = bound
        note_propose_receipt(bound)
        if bound:
            lane_b = threading.Thread(target=self.propose, args=(state, parent), name="lane-B")
            lane_b.start()
            lane_b.join()
        return Idea(operator="draft", params={"x": 0.5, "y": 0.5},
                    rationale="an LLM proposal", hypothesis="an LLM hypothesis")


def test_the_funnel_reads_its_own_call_s_receipt_not_the_shared_attribute(tmp_path, monkeypatch,
                                                                          caplog):
    """doc 69 69.37, driven: lane A's proposal was cut short by its turn budget, and lane B's clean
    propose rewrote the shared attribute between A's return and the funnel's read — the warning
    that marks A's proposal TRUNCATED was lost (and B's would have been reported for A's).
    MUTATION: read `researcher_budget_exhausted(handle)` in `_propose` -> no warning."""
    import threading

    from looplab.adapters.toytask import ToyTask
    from looplab.events.replay import fold
    from tests.factories import TOY_TASK, make_engine

    racing = _SharedAndRacing()
    engine = make_engine(tmp_path / "race", task=ToyTask.load(TOY_TASK), researcher=racing)
    engine.store.append("run_started", {
        "run_id": engine.run_dir.name, "task_id": "toy", "goal": "g", "direction": "min"})
    monkeypatch.setattr(engine, "_apply_novelty_gate", lambda _state, idea, **_kw: idea)
    out = {}

    def _lane_a():
        out["idea"] = engine._prepare_node_idea(
            {"kind": "draft"}, fold(engine.store.read_all()), researcher=engine.researcher,
            prospective_node_id=0, source="researcher")

    with caplog.at_level("WARNING", logger="looplab.engine.orchestrator"):
        lane = threading.Thread(target=_lane_a, name="lane-A")
        lane.start()
        lane.join()
    warned = [r.getMessage() for r in caplog.records if "cut short by its" in r.getMessage()]
    assert racing.calls == 2 and racing.last_budget_exhausted == "", "premise: B wrote last"
    assert out["idea"] is not None and warned and all("turns" in w for w in warned), warned


@pytest.mark.parametrize("raises", [False, True])
def test_a_real_propose_notes_its_receipt_into_the_caller_s_scope(tmp_path, monkeypatch, raises):
    """`ToolUsingResearcher.propose` notes the bound its loop announced into the scope its caller
    opened — on a clean return and on a loop that raised (the fallback idea is still this call's).
    MUTATION: drop the note from `propose`'s `finally` -> the scope is empty and the caller falls
    back to the shared attribute."""
    from looplab.agents.propose_receipts import propose_receipt_scope
    from looplab.tools.knowledge_tools import KnowledgeTools

    def _phase(client, tools, messages, emit_spec, *, on_budget=None, finalize=None, **_kw):
        on_budget({"kind": "turns", "turns": 3, "seconds": 1.0})
        if raises:
            raise RuntimeError("the endpoint went away after the cut")
        return finalize({"operator": "draft", "params": {"x": 0.5}, "rationale": "r"})

    monkeypatch.setattr(agent_mod, "run_phase", _phase)
    researcher = agent_mod.ToolUsingResearcher(object(), KnowledgeTools(str(tmp_path)))
    monkeypatch.setattr(researcher, "_fallback", lambda _messages, _exc=None: Idea(
        operator="draft", params={"x": 0.1}, rationale="fallback (endpoint)"))
    with propose_receipt_scope() as box:
        researcher.propose(RunState(goal="g", direction="min"), None)
    assert box == ["turns"] and researcher.last_budget_exhausted == "turns"


class _CleanThenCut:
    """The first propose ends on its own terms, every later one is cut by its turn budget."""

    def __init__(self):
        self.calls = 0
        self.last_budget_exhausted = ""

    def propose(self, _state, _parent):
        from looplab.agents.propose_receipts import note_propose_receipt
        self.calls += 1
        self.last_budget_exhausted = "" if self.calls == 1 else "turns"
        note_propose_receipt(self.last_budget_exhausted)
        return Idea(operator="draft", params={"x": 0.1 * self.calls, "y": 0.5},
                    rationale=f"proposal {self.calls}", hypothesis="h")


def test_a_draft_the_gate_re_proposed_carries_the_re_proposal_s_receipt(tmp_path, monkeypatch,
                                                                         caplog):
    """The final candidate is the gate's RE-proposal, so it carries that call's receipt, not the
    first one's. MUTATION: link the final draft with the first proposal's receipt -> one warning."""
    from looplab.adapters.toytask import ToyTask
    from looplab.events.replay import fold
    from tests.factories import TOY_TASK, make_engine

    researcher = _CleanThenCut()
    engine = make_engine(tmp_path / "draft", task=ToyTask.load(TOY_TASK), researcher=researcher)
    engine.store.append("run_started", {
        "run_id": engine.run_dir.name, "task_id": "toy", "goal": "g", "direction": "min"})
    monkeypatch.setattr(engine, "_apply_novelty_gate",
                        lambda _state, _idea, *, repropose=None, **_kw: repropose())
    with caplog.at_level("WARNING", logger="looplab.engine.orchestrator"):
        idea = engine._prepare_node_idea({"kind": "draft"}, fold(engine.store.read_all()),
                                         researcher=engine.researcher, prospective_node_id=0,
                                         source="researcher")
    warned = [r.getMessage() for r in caplog.records if "cut short by its" in r.getMessage()]
    assert idea is not None and idea.rationale == "proposal 2" and researcher.calls == 2
    assert len(warned) == 2, "the re-proposal and the final candidate it became, each once"


def test_a_batch_roll_reads_its_own_receipt_not_the_shared_attribute(tmp_path, caplog):
    """doc 69 69.37, the batch lane: its rolls go straight to the stager and warn at their own
    propose site — from the roll's own scope, not the shared researcher's attribute a concurrent
    lane rewrites. MUTATION: read the attribute -> no warning."""
    import threading

    from looplab.events.replay import fold
    from tests.factories import make_engine

    engine = make_engine(tmp_path / "batch", n_seeds=3, max_nodes=6)
    engine.store.append("run_started", {"run_id": "r", "task_id": "toy", "direction": "min"})
    engine._novelty_mode = "off"
    racing = _SharedAndRacing()
    engine.researcher = racing
    out = {}

    def _lane_a():
        out["proposal"] = engine._propose_batch(fold(engine.store.read_all()), 1)

    with caplog.at_level("WARNING"):
        lane = threading.Thread(target=_lane_a, name="lane-A")
        lane.start()
        lane.join()
    warned = [r.getMessage() for r in caplog.records if "was cut short by its" in r.getMessage()]
    assert racing.last_budget_exhausted == "", "premise: the concurrent lane wrote last"
    assert out["proposal"].ideas and len(warned) == 1 and "turns" in warned[0], warned


# ------------------------------------------ the panels and the gate pick; the receipt follows the pick

class _NotedMembers:
    """A base Researcher whose members note their OWN receipts, in order (`receipts`), proposing the
    point x=2 first — which the warm history (`_warm_state`) predicts best — and x=9 after. Its
    SHARED attribute is a decoy: another lane's propose rewrites it between each member's note and
    any read of it, so only a receipt taken from the member's own scope is the member's."""

    def __init__(self, receipts, client=None):
        self.receipts = list(receipts)
        self.calls = 0
        self.client = client
        self.bounds = None
        self.last_budget_exhausted = ""

    def propose(self, _state, _parent):
        from looplab.agents.propose_receipts import note_propose_receipt
        self.calls += 1
        note_propose_receipt(self.receipts[self.calls - 1])
        self.last_budget_exhausted = "time"          # another lane's propose, in between
        x = 2.0 if self.calls == 1 else 9.0
        return Idea(operator="draft", params={"x": x, "y": -x},
                    rationale=f"member {self.calls}", hypothesis=f"hypothesis {self.calls}")


@pytest.mark.parametrize("receipts", [["", "turns"], ["turns", ""]])
def test_the_surrogate_panel_notes_its_PICK_s_receipt_last(receipts):
    """crit_v58 L3, driven: the K members each noted into the CALLER's scope, so the engine read the
    LAST member's receipt whichever was picked — a converged pick warned TRUNCATED off a later
    member's cut, and a cut pick read converged off a later clean one. Each member now proposes in
    its own scope and the pick's receipt is noted last. MUTATIONS, each red here: no note of the
    pick's (the path before the fix, where the members' own notes came last); the last member's
    receipt noted instead of the pick's; a member's receipt read off the shared attribute (its
    scope dropped) — the decoy's "time"."""
    from looplab.agents.propose_receipts import propose_receipt_scope, scoped_budget_exhausted
    from looplab.search.panel import PanelResearcher

    base = _NotedMembers(receipts)
    panel = PanelResearcher(base, k=2, bounds=_TOY_BOUNDS, warmup=3)
    with propose_receipt_scope() as box:
        idea = panel.propose(_warm_state(), None)
    assert base.calls == 2 and idea.rationale.startswith("member 1 "), "premise: member 1 is picked"
    assert scoped_budget_exhausted(box, panel) == receipts[0], box


@pytest.mark.parametrize("receipts", [["", "turns"], ["turns", ""]])
def test_the_foresight_panel_notes_its_PICK_s_receipt_last_on_the_independent_path(receipts):
    """crit_v58 L3, the foresight panel's independent fan-out (no `alternatives` session): the same
    defect and the same rule as the surrogate panel's above. MUTATIONS, each red here: the members in
    the caller's scope with `receipts=None` handed to `_pick` (the path before the fix) -> the last
    member's; a member's scope dropped -> the decoy's "time"."""
    from looplab.agents.propose_receipts import propose_receipt_scope, scoped_budget_exhausted
    from looplab.search.foresight import ForesightPanelResearcher

    class _RanksMemberOneFirst:
        def complete_tool(self, _messages, _json_schema):
            return {"order": [0, 1], "confidence": 0.9, "reason": "test"}

        def complete_text(self, _messages):
            return "not json"

    base = _NotedMembers(receipts, client=_RanksMemberOneFirst())
    panel = ForesightPanelResearcher(base, k=2)
    with propose_receipt_scope() as box:
        idea = panel.propose(_warm_state(), None)
    assert base.calls == 2 and idea.rationale.startswith("member 1 "), "premise: member 1 is picked"
    assert panel.last_foresight and panel.last_foresight["chosen"] == 0
    assert scoped_budget_exhausted(box, panel) == receipts[0], box


def test_the_surrogate_notes_every_receipt_into_the_caller_s_scope():
    """crit_v58 L4, driven: past warm-up the surrogate proposes WITHOUT a call and noted nothing, so
    the engine's read fell back to this SHARED wrapper's attribute — which a concurrent lane's
    delegated, cut-short propose rewrites between this call's return and the read (that write is
    made by hand below). Below warm-up the delegate's OWN receipt is forwarded — a delegate that
    writes only its attribute included — and the random bootstrap notes "" too. MUTATIONS, each red
    here: no note on the numeric path; the delegate's receipt left on the attribute alone."""
    from looplab.agents.propose_receipts import propose_receipt_scope, scoped_budget_exhausted
    from looplab.search.surrogate import SurrogateResearcher

    cut = _CutEveryCall()
    surrogate = SurrogateResearcher(_TOY_BOUNDS, fallback=cut)
    with propose_receipt_scope() as box:
        idea = surrogate.propose(_warm_state(), None)
    assert "surrogate-guided" in idea.rationale and cut.calls == 0
    surrogate.last_budget_exhausted = "turns"          # another lane's delegated propose, in between
    assert box == [""] and scoped_budget_exhausted(box, surrogate) == ""
    with propose_receipt_scope() as box:
        surrogate.propose(RunState(goal="g", direction="min"), None)
    assert cut.calls == 1 and box == ["turns"], "the delegate's own receipt, forwarded"
    with propose_receipt_scope() as box:
        idea = SurrogateResearcher(_TOY_BOUNDS).propose(RunState(goal="g", direction="min"), None)
    assert "bootstrap" in idea.rationale and box == [""]


def test_the_receipt_of_the_returned_proposal_is_found_by_identity():
    """`node_build._receipt_of`'s truth table: the RETURNED proposal's own receipt by identity (the
    gate may keep the original after asking for another, crit_v58 N3); a proposal no call returned
    as-is (the gate's nudged copy) takes the latest receipt; nothing answered, ""."""
    from looplab.engine.node_build import _receipt_of

    first, again = Idea(operator="draft", params={}), Idea(operator="draft", params={})
    answered = [(first, ""), (again, "turns")]
    assert _receipt_of(first, answered) == "" and _receipt_of(again, answered) == "turns"
    assert _receipt_of(first.model_copy(), answered) == "turns"
    assert _receipt_of(first, []) == ""


@pytest.mark.parametrize("kind", ["draft", "improve"])
def test_a_proposal_the_gate_KEPT_carries_its_own_receipt_not_the_re_proposal_s(
        tmp_path, monkeypatch, caplog, kind):
    """crit_v58 N3, driven on both proposal paths: the gate asked for a second proposal — cut short by
    its turn budget — and then KEPT the original, which had converged. The node is built from the
    original, so it carries the original's receipt: the re-proposal is warned about once, at its
    own link, and the kept original never. MUTATION: link the final candidate with the LAST
    receipt (the reading before the fix) -> two warnings."""
    from looplab.adapters.toytask import ToyTask
    from looplab.events.replay import fold
    from tests.factories import TOY_TASK, make_engine

    researcher = _CleanThenCut()
    engine = make_engine(tmp_path / kind, task=ToyTask.load(TOY_TASK), researcher=researcher)
    engine.store.append("run_started", {
        "run_id": engine.run_dir.name, "task_id": "toy", "goal": "g", "direction": "min"})
    action: dict = {"kind": "draft"}
    if kind == "improve":
        engine.store.append("node_created", {
            "node_id": 0, "parent_ids": [], "operator": "draft", "code": "print(1)",
            "idea": {"operator": "draft", "params": {"x": 0.0, "y": 0.0}}})
        engine.store.append("node_evaluated", {
            "node_id": 0, "generation": 0, "metric": 1.0, "eval_seconds": 0.1})
        action = {"kind": "improve", "parent_id": 0}

    def _gate_keeps_the_original(_state, idea, *, repropose=None, **_kw):
        again = repropose()
        assert again is not None and again.rationale == "proposal 2"
        return idea

    monkeypatch.setattr(engine, "_apply_novelty_gate", _gate_keeps_the_original)
    with caplog.at_level("WARNING", logger="looplab.engine.orchestrator"):
        idea = engine._prepare_node_idea(action, fold(engine.store.read_all()),
                                         researcher=engine.researcher, prospective_node_id=1,
                                         source="researcher")
    warned = [r.getMessage() for r in caplog.records if "cut short by its" in r.getMessage()]
    assert idea is not None and idea.rationale == "proposal 1" and researcher.calls == 2
    assert len(warned) == 1, "the re-proposal once, at its own link; the kept original never"
