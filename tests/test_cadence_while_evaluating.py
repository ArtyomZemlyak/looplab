"""The cadence family that stopped firing when evaluations began outliving their turn (F1i).

FIVE periodic phases opened with `if state.pending_nodes(): return False` — the Strategist consult
and the coverage snapshot (`strategy.py::_should_consult`), the concept classifier re-tag /
consolidation / edge / hypothesis-tag / concept-coverage pass
(`concept_cadence.py::_should_consult_concepts`), comparative-lesson distillation and the serial
deep-research + report refresh. Backlog F1f (2026-08-13) hoisted the eval task group to run scope,
so a node stays `pending` across outer-loop turns and that predicate is false for the whole life of
any evaluation. Measured over `runs/` on 2026-08-18, prefixes with nodes and none pending:

    rubertlite-dr-unified-v6    850 (5 mid-run windows)   classifier fired
    rubertlite-dr-unified-v7      0                       nothing, ever
    rubertlite-dr-unified-v8    148 (ONE window: the last 8.1 min of a 47.6 h run)
    rubertlite-dr-unified-v9      0                       nothing, ever
    e5small-dr-unified-v2         0                       nothing, ever  (live, 11.6 h)

v8 is not an older baseline — it started after every F1f commit and its config is byte-identical to
v9's — so the difference is RUN SHAPE, and the family now fires at most once per run, in the drain.

What this file drives, in the order the risk runs:
  1. the predicate's truth table and its kill switch;
  2. THE PROPERTY — a run whose nodes never stop being pending fires the family;
  3. THE MONEY — the pace is unchanged, so a fixed node count buys exactly one paid pass however
     many times the outer loop turns at it;
  4. THE LINE — a tag produced beside a live evaluation is invisible to the graded-novelty
     admission channel, so no concept can reach selectability (docs/36);
  5. the negative controls — kill switch off, and a quiescent run, both behaving as before;
  6. THE OTHER THREE CONSUMERS — the first cut of this fix converted two of the five and section 6
     was added on 2026-08-19 after the corpus said so: `lessons_distilled` and
     `report_generated (trigger=cadence)` are zero in exactly the three runs with no quiescent
     prefix. `_maybe_deep_research` is the one that stays on the old predicate, and its refusal is
     pinned there too, because its concurrent half already covers it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from looplab.core.models import (Idea, Node, NodeStatus,
                                 classifier_verified_node_concepts)
from looplab.engine.cadence import at_creation_boundary
from looplab.engine.concept_cadence import _RETAG_CAP, ConceptCadenceMixin
from looplab.engine.orchestrator import Engine
from looplab.engine.strategy import StrategyCadenceMixin
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from tests.factories import make_engine


# --------------------------------------------------------------------- 1. the predicate itself
def test_the_boundary_is_reached_while_an_evaluation_runs_and_not_when_switched_off():
    assert at_creation_boundary(0, while_evaluating=False) is True     # the historical case
    assert at_creation_boundary(3, while_evaluating=False) is False    # the historical refusal
    assert at_creation_boundary(3, while_evaluating=True) is True      # the fix
    assert at_creation_boundary(0, while_evaluating=True) is True


def test_it_reads_no_pace_and_records_nothing():
    """`cadence.py`'s rule for a new pace is that it must record no `at_node` — this is not a pace
    at all, so the rule has nothing to bind. Pinned by signature: it cannot see n, last or every."""
    import inspect

    params = list(inspect.signature(at_creation_boundary).parameters)
    assert params == ["pending", "while_evaluating"]


# ------------------------------------------------------------------------------ shared fixtures
def _busy_store(tmp_path, *, evaluated=2, pending=2, task_id="dense-retrieval") -> EventStore:
    """A run shaped like v9: some experiments finished, some still training, never quiescent."""
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": task_id, "goal": "g", "direction": "max"})
    nid = 0
    for _ in range(evaluated):
        s.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"seed": float(nid)},
                                           "theme": f"dcl-{nid}", "rationale": "hard negatives"}})
        s.append("node_evaluated", {"node_id": nid, "metric": 0.8 + nid * 0.001})
        nid += 1
    for _ in range(pending):
        s.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"seed": float(nid)},
                                           "theme": f"dcl-{nid}", "rationale": "hard negatives"}})
        nid += 1
    return s


class _Host(ConceptCadenceMixin, StrategyCadenceMixin):
    """The engine members these two cadences actually read."""

    n_seeds = 3
    strategist_every = 3
    concept_retag_every = 5

    def __init__(self, store, *, while_evaluating: bool, snap=None):
        self.store = store
        self._cadence_while_evaluating = while_evaluating
        self._concept_pivot = True
        self.calls = 0
        self._snap = snap

    def _concept_coverage_snapshot(self, state):
        self.calls += 1
        return self._snap if self._snap is not None else Engine._concept_coverage_snapshot(
            None, state)


# ------------------------------------------------------------------ 2. THE PROPERTY (tier 1)
def test_a_run_that_is_never_quiescent_fires_the_concept_cadence(tmp_path):
    store = _busy_store(tmp_path)
    state = fold(store.read_all())
    assert state.pending_nodes(), "the fixture must reproduce the state the defect needs"

    host = _Host(store, while_evaluating=True)
    out = host._maybe_snapshot_concept_coverage(state)

    kinds = [e.type for e in store.read_all()]
    assert "concept_coverage_snapshot" in kinds, "the classifier cadence is still unreachable"
    assert out.concept_coverage_snapshots, "and its record must be in the folded projection"


def test_the_same_run_under_the_kill_switch_fires_nothing(tmp_path):
    """NEGATIVE CONTROL: `cadence_while_evaluating=false` restores the historical predicate."""
    store = _busy_store(tmp_path)
    before = len(store.read_all())
    host = _Host(store, while_evaluating=False)
    out = host._maybe_snapshot_concept_coverage(fold(store.read_all()))
    assert len(store.read_all()) == before and host.calls == 0
    assert not out.concept_coverage_snapshots


def test_a_quiescent_run_fires_identically_either_way(tmp_path):
    """NEGATIVE CONTROL: at pending == 0 the two branches must be the same decision."""
    quiet = _busy_store(tmp_path / "q", evaluated=4, pending=0)
    state = fold(quiet.read_all())
    assert not state.pending_nodes()
    on = _Host(quiet, while_evaluating=True)._should_consult_concepts(state, marks=[])
    off = _Host(quiet, while_evaluating=False)._should_consult_concepts(state, marks=[])
    assert on is off is True
    on_s = _Host(quiet, while_evaluating=True)._should_consult(state, marks=[])
    off_s = _Host(quiet, while_evaluating=False)._should_consult(state, marks=[])
    assert on_s is off_s is True


def test_the_strategist_consults_on_a_run_with_evaluations_in_flight(tmp_path):
    """The blast radius beyond concepts: `strategy_decision` shares the gate, and the Strategist is
    what adapts a run's strategy. It has recorded nothing since `rubertlite-dr-unified-v8`."""
    class _Stub:
        def __init__(self):
            self.calls = 0

        def decide(self, state, ctx):
            self.calls += 1
            return {"policy": "mcts", "source": "rule", "rationale": "widen"}

    def _drive(while_evaluating):
        stub = _Stub()
        eng = make_engine(tmp_path / f"s-{while_evaluating}", strategist=stub, strategist_every=1,
                          cadence_while_evaluating=while_evaluating)
        eng.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                         "direction": "min"})
        state = fold(eng.store.read_all())
        state.nodes = {
            0: Node(id=0, operator="draft", idea=Idea(operator="draft"),
                    status=NodeStatus.evaluated, metric=1.0),
            1: Node(id=1, operator="draft", idea=Idea(operator="draft"),
                    status=NodeStatus.pending),
        }
        assert state.pending_nodes()
        eng._maybe_consult_strategist(state)
        return stub.calls, [e.type for e in eng.store.read_all()]

    calls_on, kinds_on = _drive(True)
    calls_off, kinds_off = _drive(False)
    assert calls_on == 1 and "strategy_decision" in kinds_on
    assert calls_off == 0 and "strategy_decision" not in kinds_off


# -------------------------------------------------------------------------- 3. THE MONEY BOUND
def test_a_fixed_node_count_buys_exactly_one_concept_pass_however_often_the_loop_turns(tmp_path):
    """The pace is unchanged, so the paid passes per node count must be unchanged too. The loop now
    turns freely at a fixed `n` while an evaluation runs, and `_concept_coverage_snapshot` can
    return None AFTER paying for the tagging — so without the attempted-at-n memo this is one paid
    LLM pass per outer-loop turn."""
    store = _busy_store(tmp_path)
    host = _Host(store, while_evaluating=True, snap=None)
    # A producer that pays and then yields nothing: exactly the shape the durable at_node gate
    # cannot close, because nothing is appended.
    host._concept_coverage_snapshot = lambda state: (
        setattr(host, "calls", host.calls + 1) or None)
    state = fold(store.read_all())
    for _ in range(25):
        host._maybe_snapshot_concept_coverage(state)
    assert host.calls == 1, f"paid {host.calls} tagging passes at one node count"


def test_a_fixed_node_count_buys_exactly_one_strategist_consult(tmp_path):
    """Same bound on the other consumer, and it is reachable through the ORDINARY outcome: the
    Strategist agreeing with itself records nothing, so both durable gates stay open."""
    class _Stub:
        def __init__(self):
            self.calls = 0

        def decide(self, state, ctx):
            self.calls += 1
            return {}                      # no change -> nothing recorded -> window stays open

    stub = _Stub()
    eng = make_engine(tmp_path / "money", strategist=stub, strategist_every=1,
                      cadence_while_evaluating=True)
    eng.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                     "direction": "min"})
    state = fold(eng.store.read_all())
    state.nodes = {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft"),
                status=NodeStatus.evaluated, metric=1.0),
        1: Node(id=1, operator="draft", idea=Idea(operator="draft"), status=NodeStatus.pending),
    }
    for _ in range(25):
        eng._maybe_consult_strategist(state)
    assert stub.calls == 1, f"paid {stub.calls} strategist consults at one node count"


def test_the_memo_re_arms_when_the_run_actually_moves_at_the_same_node_count(tmp_path):
    """The bound must not become an OSSIFICATION, and this is where the first cut of it was wrong.

    `search/coverage.py::already_covered_at` deliberately pairs `at_node` with the analytics
    projection token, because "an abort / reset / tag edit changes the live steering inputs WITHOUT
    allocating a node". Under F1i a node TERMINATING is that same shape — `n` does not move — and
    that is exactly the moment the cadence has new evidence to read. An `n`-only memo silenced it
    for the rest of the node count, which on a multi-hour run is most of the run.
    """
    store = _busy_store(tmp_path, evaluated=2, pending=2)
    host = _Host(store, while_evaluating=True)
    host._concept_coverage_snapshot = lambda state: (
        setattr(host, "calls", host.calls + 1) or None)

    state = fold(store.read_all())
    for _ in range(10):
        host._maybe_snapshot_concept_coverage(state)
    assert host.calls == 1

    # One of the running experiments finishes. The node COUNT is unchanged; the evidence is not.
    store.append("node_evaluated", {"node_id": 2, "metric": 0.9})
    moved = fold(store.read_all())
    assert len(moved.nodes) == len(state.nodes)
    for _ in range(10):
        host._maybe_snapshot_concept_coverage(moved)
    assert host.calls == 2, "a terminal at the same node count must re-arm the cadence"


def test_the_strategist_memo_re_arms_on_the_same_projection_rule(tmp_path):
    class _Stub:
        def __init__(self):
            self.calls = 0

        def decide(self, state, ctx):
            self.calls += 1
            return {}                      # no change -> nothing recorded -> durable window open

    stub = _Stub()
    eng = make_engine(tmp_path / "rearm", strategist=stub, strategist_every=1,
                      cadence_while_evaluating=True)
    eng.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                     "direction": "min"})
    state = fold(eng.store.read_all())
    state.nodes = {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft"),
                status=NodeStatus.evaluated, metric=1.0),
        1: Node(id=1, operator="draft", idea=Idea(operator="draft"), status=NodeStatus.pending),
    }
    for _ in range(10):
        eng._maybe_consult_strategist(state)
    assert stub.calls == 1
    # node 1 lands. Same node count, a brief the Strategist has not seen.
    state.nodes[1] = Node(id=1, operator="draft", idea=Idea(operator="draft"),
                          status=NodeStatus.evaluated, metric=0.4)
    for _ in range(10):
        eng._maybe_consult_strategist(state)
    assert stub.calls == 2


def test_a_raising_strategist_still_spends_the_memo(tmp_path):
    """The memo is spent BEFORE the provider call. Spending it after would leave the very failure
    mode it bounds reachable through the error path."""
    class _Boom:
        def __init__(self):
            self.calls = 0

        def decide(self, state, ctx):
            self.calls += 1
            raise RuntimeError("endpoint down")

    stub = _Boom()
    eng = make_engine(tmp_path / "boom", strategist=stub, strategist_every=1,
                      cadence_while_evaluating=True)
    eng.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                     "direction": "min"})
    state = fold(eng.store.read_all())
    state.nodes = {0: Node(id=0, operator="draft", idea=Idea(operator="draft"),
                           status=NodeStatus.pending)}
    for _ in range(5):
        try:
            eng._maybe_consult_strategist(state)
        except RuntimeError:
            pass                      # the engine contains it; either way the memo must be spent
    assert stub.calls == 1


# ------------------------------------------------------------------------------- 4. THE LINE
def _tagged_store(tmp_path, *, at_pending, still_running=0):
    """A run whose experiments all carry a CLASSIFIER tag, stamped with the given in-flight count.

    `still_running` adds untagged nodes that never terminate, so the run is still BUSY now — which
    is a different question from what it was when the tags were made, and the parity rule turns on
    exactly that difference."""
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dense-retrieval", "goal": "g",
                             "direction": "max"})
    for nid in (0, 1):
        s.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"seed": float(nid)},
                                           "theme": f"dcl-{nid}", "rationale": "hard negatives"}})
        s.append("node_evaluated", {"node_id": nid, "metric": 0.8 + nid * 0.001})
        row = {"node_id": nid, "concepts": ["loss/decoupled-contrastive"], "mode": "llm",
               "at_vocab": 46, "generation": 0}
        if at_pending is not None:
            row["at_pending"] = at_pending
        s.append("node_concepts", row)
    for k in range(still_running):
        s.append("node_created", {"node_id": 2 + k, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"seed": 9.0 + k},
                                           "theme": f"live-{k}", "rationale": "still training"}})
    return s


def test_an_in_flight_tag_is_not_evidence_and_a_quiescent_one_is(tmp_path):
    quiet = fold(_tagged_store(tmp_path / "q", at_pending=0).read_all())
    busy = fold(_tagged_store(tmp_path / "b", at_pending=2).read_all())
    legacy = fold(_tagged_store(tmp_path / "l", at_pending=None).read_all())

    assert classifier_verified_node_concepts(quiet, 0) == ["loss/decoupled-contrastive"]
    assert classifier_verified_node_concepts(busy, 0) == []
    # Every log written before this cadence could fire mid-eval carries no field, and every one of
    # them was PROVABLY quiescent — the gate could not fire otherwise. So absent must read as 0.
    assert classifier_verified_node_concepts(legacy, 0) == ["loss/decoupled-contrastive"]


def test_the_graded_novelty_channel_cannot_see_an_in_flight_tag(tmp_path):
    """THE LINE (docs/36). The precheck's agentic path activates on classifier-provenance
    `node_concepts`; a level-4/5 grade there SHORT-CIRCUITS the flat dedup gate, i.e. it is an
    admission decision. An in-flight row must be INVISIBLE here — not "present but rejected",
    because a non-empty `classifier_ids` arms the completeness rule and would flip a run that grades
    on the curated skeleton today into one that returns None."""
    seen: list[dict] = []

    def _spy(state, idea, graph, **kw):
        # `tags` is the tell and the graph is not: with a curated skeleton wired, the agentic path
        # MERGES the classifier tags into that same skeleton, so both graphs list the same ids. What
        # separates them is whether per-node tags reached the grader at all — None is the
        # deterministic fallback, a dict is the agentic bypass this gate exists to withhold.
        seen.append({"tags": kw.get("tags")})
        raise RuntimeError("stop once the evidence channel has been chosen")

    import looplab.search.graded_novelty as gn

    busy = fold(_tagged_store(tmp_path / "b", at_pending=2).read_all())
    quiet = fold(_tagged_store(tmp_path / "q", at_pending=0).read_all())
    none = fold(_busy_store(tmp_path / "n", evaluated=2, pending=0).read_all())

    host = SimpleNamespace(_graded_novelty=True, _reflect_client=None,
                           _cross_run_prior=lambda st: (set(), {}, {}, {}))
    idea = Idea(operator="draft", theme="dcl-9", rationale="hard negatives")

    def _graph_for(state):
        seen.clear()
        original = gn.grade_novelty
        gn.grade_novelty = _spy
        try:
            Engine._graded_novelty_precheck(host, state, idea)
        finally:
            gn.grade_novelty = original
        return seen[0]["tags"] if seen else "NOT REACHED"

    busy_tags, quiet_tags, none_tags = (_graph_for(busy), _graph_for(quiet), _graph_for(none))
    # A run whose only classifier rows are in-flight must grade on EXACTLY what a run with no
    # classifier rows at all grades on — the same inputs, so no admission can move.
    assert busy_tags == none_tags is None
    # …and a quiescent row must still reach the channel, or this would be a silent removal of the
    # feature rather than a gate on it.
    assert isinstance(quiet_tags, dict) and quiet_tags


# ------------------------------------------------- 5. the writer, the re-tag and the withholding
def test_the_writer_stamps_the_pending_count_it_ran_beside(tmp_path):
    store = _busy_store(tmp_path)
    state = fold(store.read_all())
    host = _Host(store, while_evaluating=True)
    graph = SimpleNamespace(concepts=lambda: [SimpleNamespace(id="loss/contrastive")])
    host._record_node_concept_tags(
        state, {"graph": graph, "mode": "llm",
                "raw_tags": {0: ["loss/contrastive"]}, "raw_tag_modes": {}}, {})
    row = next(e for e in store.read_all() if e.type == "node_concepts")
    assert row.data["at_pending"] == len(state.pending_nodes()) == 2


def test_a_quiescent_pass_re_tags_what_an_in_flight_pass_wrote(tmp_path):
    """The parity rule. Without it, a node tagged mid-eval sits in `known` forever, so the quiescent
    pass that WOULD have produced the only evidence row under the old gate never produces one and
    the graded-novelty channel silently loses evidence it has today."""
    still_busy = fold(_tagged_store(tmp_path / "b", at_pending=2, still_running=1).read_all())
    drained = fold(_tagged_store(tmp_path / "d", at_pending=2).read_all())
    quiet = fold(_tagged_store(tmp_path / "q", at_pending=0).read_all())
    host = _Host(EventStore(tmp_path / "unused.jsonl"), while_evaluating=True)

    # While the run is still busy the in-flight row is REUSED — a continuously-busy run pays nothing
    # extra for this rule, because its condition is unreachable there.
    assert still_busy.pending_nodes()
    busy_known, _ = host._reusable_node_tags(still_busy)
    assert set(busy_known) == {0, 1}

    # Once it drains, the in-flight rows become re-taggable and the quiescent ones do not.
    assert not drained.pending_nodes()
    drained_known, _ = host._reusable_node_tags(drained)
    assert set(drained_known) == set(), "an in-flight row must be re-tagged once the run quiesces"
    quiet_known, _ = host._reusable_node_tags(quiet)
    assert set(quiet_known) == {0, 1}, "a quiescent row is never re-tagged for this reason"


def test_the_re_tag_is_bounded_by_the_existing_cap(tmp_path):
    """The money bound for the parity rule is the EXISTING `_RETAG_CAP`, not a new constant."""
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dense-retrieval", "goal": "g",
                             "direction": "max"})
    total = _RETAG_CAP + 7
    for nid in range(total):
        s.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "theme": f"t{nid}"}})
        s.append("node_evaluated", {"node_id": nid, "metric": 0.5})
        s.append("node_concepts", {"node_id": nid, "concepts": ["loss/contrastive"], "mode": "llm",
                                   "at_vocab": 46, "at_pending": 3, "generation": 0})
    state = fold(s.read_all())
    host = _Host(s, while_evaluating=True)
    known, _ = host._reusable_node_tags(state)
    assert len(state.node_concepts) - len(known) == _RETAG_CAP


def test_an_in_flight_pass_records_no_consolidation_rename(tmp_path):
    """A rename is the one output of this cadence that is RETROACTIVE and run-wide: the fold applies
    it backwards to every authored-delta node's stored membership and every read surface resolves
    ids through it. Measured on `rubertlite-dr-unified-v8`, its 9 renames change what 11 of its 16
    nodes are reported as being about. The per-row evidence gate cannot express that, so an
    in-flight pass withholds it — which costs nothing that exists today, since a run that never
    quiesces records no consolidation now either."""
    def _renames(pending):
        store = _busy_store(tmp_path / f"c{pending}", evaluated=2, pending=pending)
        state = fold(store.read_all())
        host = _Host(store, while_evaluating=True)
        graph = SimpleNamespace(concepts=lambda: [SimpleNamespace(id="loss/contrastive")])
        host._refresh_concept_tags = ConceptCadenceMixin._refresh_concept_tags.__get__(host)
        cmap = {"graph": graph, "mode": "llm", "raw_tags": {}, "raw_tag_modes": {},
                "consolidated": {"loss/dcl": "loss/decoupled-contrastive"}}
        import looplab.search.concept_map as cm
        original = cm.build_concept_map
        cm.build_concept_map = lambda *a, **k: cmap
        try:
            host._refresh_concept_tags(state, object(), "tool_call", None)
        finally:
            cm.build_concept_map = original
        return [e for e in store.read_all() if e.type == "concept_consolidation"]

    assert _renames(2) == [], "a mid-eval pass must not rewrite the run's vocabulary"
    assert len(_renames(0)) == 1, "a quiescent pass still records it, exactly as before"


# --------------------------------------------------------------------------- the fold contract
@pytest.mark.parametrize("value,expected", [
    (0, 0), (4, 4),
    (-1, 1),            # malformed -> fail CLOSED to in-flight: a row that cannot say when it was
    ("2", 1),           #   made is not evidence.
    (True, 1),
    (1.5, 1),
])
def test_a_malformed_in_flight_receipt_fails_closed(tmp_path, value, expected):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dense-retrieval", "goal": "g",
                             "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "theme": "t"}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    s.append("node_concepts", {"node_id": 0, "concepts": ["loss/contrastive"], "mode": "llm",
                               "at_vocab": 46, "at_pending": value, "generation": 0})
    state = fold(s.read_all())
    assert state.node_concepts_at_pending.get(0, 0) == expected
    if expected:
        assert classifier_verified_node_concepts(state, 0) == []


# ---------------------------------- 6. THE THREE CONSUMERS THE FIRST CUT OF THIS FIX LEFT BEHIND
#
# The module docstring above names FIVE phases that opened with `if state.pending_nodes()`, and the
# 2026-08-18 change moved TWO of them onto `at_creation_boundary`. The other three kept the dead
# proxy, and the corpus says so directly — over `runs/`, in exactly the runs with zero quiescent
# prefixes (v7, v9, the live e5small):
#
#   consumer                                 dr    v6    v7    v8    v9   live
#   lessons_distilled  (trigger=cadence)     19     1     0     1     0      0
#   report_generated   (trigger=cadence)     26     1     0     1     0      0
#
# — against `research_completed (cadence)` 27/5/2/14/6/9, which is alive in every run because the
# CONCURRENT half of that decision (`_spawn_research` -> `_due_research_trigger`) never carried the
# guard at all. That asymmetry is why the serial deep-research gate is deliberately NOT moved here;
# the last test in this section pins the refusal so nobody "completes the family" into a race.
def _lesson_engine(tmp_path, name, *, while_evaluating: bool, distilled):
    """An engine with comparative lessons wired and the paid producer replaced by a counter."""
    eng = make_engine(tmp_path / name, lessons_every=1, comparative_lessons=True,
                      reflection_priors=True, memory_dir=str(tmp_path / name / "mem"),
                      cadence_while_evaluating=while_evaluating)
    eng.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                     "direction": "min"})
    # The producer is the paid half; the store write is the shared cross-run file. Neither is what
    # this section is about — the question is only whether the loop ever REACHES them.
    eng._comparative_lessons = lambda state, fp, exclude=(): (distilled(), [])
    eng._append_lessons = lambda lessons, hygiene=True, state=None: None
    return eng


def _busy_nodes() -> dict:
    """One node landed, one still training — the state every GPU run on this box is in ~always."""
    return {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft"),
                status=NodeStatus.evaluated, metric=1.0),
        1: Node(id=1, operator="draft", idea=Idea(operator="draft"), status=NodeStatus.pending),
    }


def test_lessons_distil_on_a_run_with_evaluations_in_flight(tmp_path):
    """THE PROPERTY for the write side of M6. `lessons_distilled` is also the ONLY route to
    auto-skill promotion (`lessons_distill.py` -> `memory.write_auto_skill`), so a dead distiller
    takes the whole cross-run skill channel with it."""
    calls = []

    def _drive(while_evaluating):
        calls.clear()
        eng = _lesson_engine(tmp_path, f"L{while_evaluating}", while_evaluating=while_evaluating,
                             distilled=lambda: (calls.append(1) or []))
        state = fold(eng.store.read_all())
        state.nodes = _busy_nodes()
        assert state.pending_nodes(), "the fixture must reproduce the state the defect needs"
        eng._maybe_distill_lessons(state)
        return len(calls), [e.type for e in eng.store.read_all()]

    on_calls, on_kinds = _drive(True)
    off_calls, off_kinds = _drive(False)
    assert on_calls == 1 and "lessons_distilled" in on_kinds
    # NEGATIVE CONTROL: the kill switch restores the historical predicate byte for byte.
    assert off_calls == 0 and "lessons_distilled" not in off_kinds


def test_the_report_refreshes_on_a_run_with_evaluations_in_flight(tmp_path):
    class _Writer:
        def __init__(self):
            self.calls = 0

        def generate(self, state, trigger=""):
            self.calls += 1
            return {"headline": "h", "verdict": "v", "at_node": len(state.nodes),
                    "trigger": trigger}

    def _drive(while_evaluating):
        writer = _Writer()
        eng = make_engine(tmp_path / f"R{while_evaluating}", report_writer=writer, report_every=1,
                          cadence_while_evaluating=while_evaluating)
        eng.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                         "direction": "min"})
        state = fold(eng.store.read_all())
        state.nodes = _busy_nodes()
        eng._maybe_refresh_report(state)
        return writer.calls, [e.type for e in eng.store.read_all()]

    on_calls, on_kinds = _drive(True)
    off_calls, off_kinds = _drive(False)
    assert on_calls == 1 and "report_generated" in on_kinds
    assert off_calls == 0 and "report_generated" not in off_kinds


def test_a_fixed_node_count_buys_exactly_one_distill_and_one_report(tmp_path):
    """THE MONEY BOUND, and for these two it needs no in-process memo: both record their `at_node`
    on EVERY path — `lessons_distilled` is appended even with zero lessons (its own comment says
    why), and `serve/report.py` sets `at_node` outside the try, so even a provider failure closes
    the window. So the durable gate alone bounds the loop, which is why neither needs the
    attempted-at-n memo the Strategist and the concept snapshot carry."""
    calls = []
    eng = _lesson_engine(tmp_path, "Lmoney", while_evaluating=True,
                         distilled=lambda: (calls.append(1) or []))
    state = fold(eng.store.read_all())
    state.nodes = _busy_nodes()
    for _ in range(25):
        state = eng._maybe_distill_lessons(state) or state
        state.nodes = _busy_nodes()          # the run has not moved; only the loop has turned
    assert len(calls) == 1, f"paid {len(calls)} distillations at one node count"

    class _Writer:
        def __init__(self):
            self.calls = 0

        def generate(self, state, trigger=""):
            self.calls += 1
            return {"headline": "h", "verdict": "v", "at_node": len(state.nodes)}

    writer = _Writer()
    eng2 = make_engine(tmp_path / "Rmoney", report_writer=writer, report_every=1,
                       cadence_while_evaluating=True)
    eng2.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                      "direction": "min"})
    state2 = fold(eng2.store.read_all())
    state2.nodes = _busy_nodes()
    for _ in range(25):
        state2 = eng2._maybe_refresh_report(state2) or state2
        state2.nodes = _busy_nodes()
    assert writer.calls == 1, f"paid {writer.calls} reports at one node count"


def test_the_serial_deep_research_gate_is_deliberately_left_on_the_old_predicate(tmp_path):
    """THE ONE THAT MUST NOT BE 'COMPLETED'. `_maybe_deep_research` is the SERIAL half of a decision
    whose CONCURRENT half (`_spawn_research`) never carried this guard and fires throughout every
    eval — measured, `research_completed` has cadence rows in all six runs in `runs/`, including the
    three with zero quiescent prefixes. Moving this gate would put a main-task think and a
    background think at the SAME node count with only a read-then-write window between their shared
    `_cadence_research_marks` check and their receipts, i.e. it buys a double-spend to reach work
    that is already being done. The hole it leaves is `concurrent_research=false` (not the shipped
    default), and it is filed rather than patched — `docs/BACKLOG.md` F1i-b."""
    class _Researcher:
        def __init__(self):
            self.calls = 0

        def run(self, *a, **k):
            self.calls += 1
            return {}

    stub = _Researcher()
    eng = make_engine(tmp_path / "dr", deep_researcher=stub, deep_research_every=1,
                      cadence_while_evaluating=True)
    eng.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                     "direction": "min"})
    state = fold(eng.store.read_all())
    state.nodes = _busy_nodes()
    out = eng._maybe_deep_research(state)
    assert stub.calls == 0
    assert "research_attempted" not in [e.type for e in eng.store.read_all()]
    assert out is state
