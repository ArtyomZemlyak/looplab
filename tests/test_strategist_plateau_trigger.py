"""The Strategist consult is due on a PLATEAU, not only on the node-count cadence (doc 52 row 7).

The stall reading was a year older than its use: `agents/strategist.py::improves_since_best` is in
every `StrategyContext` and `RuleStrategist` branches on it against `stall_window` (greedy⇄broad,
deep research at 2x), but `engine/strategy.py::_should_consult` fired on `cadence_due` alone, so the
reaction to a stalled leader waited for the next tick. This file drives the trigger that closes that
gap, in the order the risk runs:

  1. the rule's truth table (`cadence.plateau_due`) and the signal it reads (`stall_rung`);
  2. THE PROPERTY — the gate fires at the stall and at the hard stall, and not before either;
  3. THE MONEY — a stalled run buys exactly ONE paid consult per stall rung however many nodes
     land, through the ordinary outcome (the Strategist agrees with itself and records nothing);
  4. DURABILITY — a recorded plateau decision closes the rung for a fresh engine; an unrecorded one
     re-asks exactly once, which is the memo's stated contract;
  5. the window comes from the Strategist that will act on it, with a safe default;
  6. the other consumer of the gate — the coverage snapshot — takes one extra sample per rung.
"""
from __future__ import annotations

from types import SimpleNamespace

from looplab.agents.strategist import (DEFAULT_STALL_WINDOW, LLMStrategist, RuleStrategist,
                                       STALL_OPERATORS, ToolUsingStrategist, improves_since_best,
                                       stall_rung, strategist_stall_window)
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.cadence import plateau_due
from looplab.engine.strategy import StrategyCadenceMixin
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from tests.factories import make_engine


# ------------------------------------------------------------------------------ shared fixtures
def _stalled_store(path, *, improves: int, leader_recorded: bool = True) -> EventStore:
    """A run whose leader is node 0 and whose every later node tried to beat it and lost.

    `leader_recorded` appends the strategy decision a real run records at the seed boundary, so the
    seed-boundary firing is CLOSED and only the plateau (or the far cadence) can open the gate.
    """
    s = EventStore(path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"x": 1.0}}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.9})
    if leader_recorded:
        s.append("strategy_decision", {"strategy": {"policy": "greedy", "source": "rule",
                                                    "rationale": "seed boundary"},
                                       "at_node": 1, "ctx": None})
    for _ in range(improves):
        _push(s)
    return s


def _push(s: EventStore, *, metric: float = 0.5, operator: str = "improve") -> int:
    """One more attempt on the leader, evaluated and worse (or better, to crown a new leader)."""
    nid = sum(1 for e in s.read_all() if e.type == "node_created")
    s.append("node_created", {"node_id": nid, "parent_ids": [0], "operator": operator,
                              "idea": {"operator": operator, "params": {"x": float(nid)}}})
    s.append("node_evaluated", {"node_id": nid, "metric": metric})
    return nid


class _Gate(StrategyCadenceMixin):
    """The engine members `_should_consult` and `_maybe_snapshot_coverage` actually read."""
    n_seeds = 1
    strategist_every = 100          # far enough that only the plateau can open the gate
    _cadence_while_evaluating = True
    _coverage_context = True
    archive_resolution = 1.0

    def __init__(self, store=None, strategist=None):
        self.store = store
        self.strategist = strategist


class _Stub:
    """A Strategist that counts its (paid) consults and answers what it is told to."""

    def __init__(self, answer=None):
        self.calls = 0
        self.answer = answer if answer is not None else {}

    def decide(self, state, ctx):
        self.calls += 1
        return dict(self.answer)


# ----------------------------------------------------------------------- 1. the rule and the signal
def test_plateau_due_truth_table():
    key = (0, 1)
    assert plateau_due(1, 4, 1, seen=None, key=key) is True         # stalled, nothing since it began
    assert plateau_due(0, 0, 0, seen=None, key=(0, 0)) is False     # not stalled
    assert plateau_due(1, 4, 4, seen=None, key=key) is False        # a mark inside the rung closes it
    assert plateau_due(1, 4, 7, seen=None, key=key) is False        # ... or after it
    assert plateau_due(1, 4, 1, seen=key, key=key) is False         # the consumer's own memo closes it
    assert plateau_due(2, 7, 4, seen=key, key=(0, 2)) is True       # the NEXT rung is a new fact
    assert plateau_due(1, 4, 1, seen=(3, 1), key=key) is True       # a memo about another leader is not


def test_stall_rung_counts_windows_of_the_stall_family_and_starts_at_a_node_count():
    """Interleaved drafts and an ablation do not count toward the stall, and `started_at` is the
    node COUNT once the window filled — the unit a consumer's `at_node` mark is written in."""
    st = RunState()
    ops = ["draft", "improve", "draft", "improve", "improve", "ablate", "improve", "merge"]
    for i, op in enumerate(ops):
        st.nodes[i] = Node(id=i, operator=op, idea=Idea(operator=op),
                           status=NodeStatus.evaluated, metric=0.5)
    st.best_node_id = 0
    assert improves_since_best(st) == 5
    assert stall_rung(st, 3) == (1, 5)      # the third stall node is id 4 -> five nodes existed
    assert stall_rung(st, 2) == (2, 7)      # the fourth is id 6 -> seven nodes
    assert stall_rung(st, 5) == (1, 8)
    assert stall_rung(st, 6) == (0, 0)      # window not filled once
    assert stall_rung(st, 0) == stall_rung(st, 1) == (5, 8)    # junk window clamps to one
    assert stall_rung(RunState(), 3) == (0, 0)                 # no leader yet
    assert set(STALL_OPERATORS) == {"improve", "refine_block", "merge", "expand"}


# ------------------------------------------------------------------------------- 2. THE PROPERTY
def test_the_gate_fires_at_the_stall_and_at_the_hard_stall_and_not_before_either(tmp_path):
    store = _stalled_store(tmp_path, improves=2)
    marks = [{"at_node": 1}]                       # the seed-boundary decision, already recorded
    gate = _Gate(store)
    assert gate._should_consult(fold(store.read_all()), marks=marks) is False
    _push(store)                                   # the third failed attempt: the stall
    stalled = fold(store.read_all())
    assert improves_since_best(stalled) == DEFAULT_STALL_WINDOW
    assert gate._should_consult(stalled, marks=marks) is True
    # A decision recorded inside the rung closes it durably; the consumer's memo closes it too.
    assert gate._should_consult(stalled, marks=[{"at_node": 4}]) is False
    assert gate._should_consult(stalled, marks=marks, plateau_seen=(0, 1)) is False
    # Two more failed attempts are the SAME plateau, not a new one.
    _push(store)
    _push(store)
    assert gate._should_consult(fold(store.read_all()), marks=marks, plateau_seen=(0, 1)) is False
    # The sixth is the hard stall the rule requests deep research at: a new rung, fires once more.
    _push(store)
    hard = fold(store.read_all())
    assert stall_rung(hard, DEFAULT_STALL_WINDOW) == (2, 7)
    assert gate._should_consult(hard, marks=[{"at_node": 4}], plateau_seen=(0, 1)) is True
    assert gate._should_consult(hard, marks=[{"at_node": 7}], plateau_seen=(0, 1)) is False
    # A new leader ends the plateau: the count restarts from zero improves.
    _push(store, metric=0.95)
    crowned = fold(store.read_all())
    assert crowned.best_node_id == 7 and improves_since_best(crowned) == 0
    assert gate._should_consult(crowned, marks=[{"at_node": 7}]) is False


def test_the_plateau_does_not_move_the_cadence_window_unless_it_records(tmp_path):
    """A plateau firing that records nothing leaves `cadence_due` exactly where it was."""
    store = _stalled_store(tmp_path, improves=3)
    gate = _Gate(store)
    gate.strategist_every = 5
    marks = [{"at_node": 1}]
    st = fold(store.read_all())                    # n == 4: plateau yes, cadence 4 - 1 < 5
    assert gate._should_consult(st, marks=marks) is True
    assert gate._should_consult(st, marks=marks, plateau_seen=(0, 1)) is False
    _push(store)
    _push(store)                                   # n == 6: cadence 6 - 1 >= 5 fires as before
    assert gate._should_consult(fold(store.read_all()), marks=marks, plateau_seen=(0, 1)) is True


# ---------------------------------------------------------------------------------- 3. THE MONEY
def test_a_stalled_run_buys_one_consult_per_rung_however_many_nodes_land(tmp_path):
    """Through the ORDINARY outcome: the Strategist agrees with itself, records nothing, and both
    durable gates stay open — the shape the `(n, projection token)` memo bounds at ONE node count
    and this trigger would have re-opened at every next one."""
    store = _stalled_store(tmp_path, improves=2)
    stub = _Stub({})
    eng = make_engine(tmp_path, strategist=stub, strategist_every=100,
                      cadence_while_evaluating=True)
    assert eng.store.path == store.path
    eng._maybe_consult_strategist(fold(eng.store.read_all()))
    assert stub.calls == 0, "two failed attempts are not a stall"
    for _ in range(3):                             # attempts 3, 4, 5: one rung
        _push(eng.store)
        for _ in range(4):                         # and the loop turns several times per count
            eng._maybe_consult_strategist(fold(eng.store.read_all()))
    assert stub.calls == 1, f"paid {stub.calls} consults for one stall rung"
    _push(eng.store)                               # attempt 6: the hard stall, a new rung
    for _ in range(4):
        eng._maybe_consult_strategist(fold(eng.store.read_all()))
    assert stub.calls == 2
    recorded = [e for e in eng.store.read_all() if e.type == "strategy_decision"]
    assert len(recorded) == 1 and recorded[0].data["at_node"] == 1, "nothing but the seed decision"


# ------------------------------------------------------------------------------- 4. DURABILITY
def test_a_recorded_plateau_decision_closes_the_rung_for_a_fresh_engine(tmp_path):
    store = _stalled_store(tmp_path, improves=3)
    first = _Stub({"policy": "mcts", "source": "rule", "rationale": "stalled: explore"})
    eng = make_engine(tmp_path, strategist=first, strategist_every=100,
                      cadence_while_evaluating=True)
    eng._maybe_consult_strategist(fold(eng.store.read_all()))
    rows = [e for e in eng.store.read_all() if e.type == "strategy_decision"]
    assert first.calls == 1 and rows[-1].data["at_node"] == 4
    # Resume: a NEW process with an empty memo, one more failed attempt on the same plateau.
    _push(eng.store)
    second = _Stub({})
    resumed = make_engine(tmp_path, strategist=second, strategist_every=100,
                          cadence_while_evaluating=True)
    for _ in range(3):
        resumed._maybe_consult_strategist(fold(resumed.store.read_all()))
    assert second.calls == 0, "the recorded decision inside the rung must close it durably"


def test_an_unrecorded_plateau_consult_re_asks_exactly_once_after_a_resume(tmp_path):
    """The memo's own contract: in-process only, so a resumed engine re-asks once — never per node."""
    store = _stalled_store(tmp_path, improves=3)
    eng = make_engine(tmp_path, strategist=_Stub({}), strategist_every=100,
                      cadence_while_evaluating=True)
    eng._maybe_consult_strategist(fold(eng.store.read_all()))
    _push(eng.store)
    stub = _Stub({})
    resumed = make_engine(tmp_path, strategist=stub, strategist_every=100,
                          cadence_while_evaluating=True)
    for _ in range(3):
        resumed._maybe_consult_strategist(fold(resumed.store.read_all()))
    _push(resumed.store)
    for _ in range(3):
        resumed._maybe_consult_strategist(fold(resumed.store.read_all()))
    assert stub.calls == 1


# --------------------------------------------------------------------- 5. the window is the rule's
def test_the_window_is_read_off_the_strategist_that_acts_on_it(tmp_path):
    assert strategist_stall_window(RuleStrategist(stall_window=5)) == 5
    assert strategist_stall_window(RuleStrategist()) == DEFAULT_STALL_WINDOW
    assert strategist_stall_window(LLMStrategist(object())) == DEFAULT_STALL_WINDOW
    assert strategist_stall_window(ToolUsingStrategist(object())) == DEFAULT_STALL_WINDOW
    assert strategist_stall_window(None) == DEFAULT_STALL_WINDOW
    assert strategist_stall_window(_Stub()) == DEFAULT_STALL_WINDOW
    for junk in (True, 0, -2, "3", 2.5):
        assert strategist_stall_window(SimpleNamespace(stall_window=junk)) == DEFAULT_STALL_WINDOW
    # And the gate reads it: a five-improve window does not fire at three.
    store = _stalled_store(tmp_path, improves=3)
    wide = _Gate(store, strategist=RuleStrategist(stall_window=5))
    marks = [{"at_node": 1}]
    assert wide._should_consult(fold(store.read_all()), marks=marks) is False
    _push(store)
    _push(store)
    assert wide._should_consult(fold(store.read_all()), marks=marks) is True
    assert wide._plateau_key(fold(store.read_all())) == (0, 1)


# ------------------------------------------------------------------- 6. the other consumer
def test_the_coverage_snapshot_takes_one_extra_sample_per_rung(tmp_path):
    """The snapshot shares the gate and always records a mark, so it needs no memo: one sample at
    the plateau, none at the next node of the same plateau, the cadence otherwise untouched."""
    store = _stalled_store(tmp_path, improves=3)
    gate = _Gate(store)

    def _snapshots():
        return [e.data["at_node"] for e in store.read_all() if e.type == "coverage_snapshot"]

    st = gate._maybe_snapshot_coverage(fold(store.read_all()))
    assert _snapshots() == [4] and st.coverage_snapshots
    gate._maybe_snapshot_coverage(fold(store.read_all()))
    assert _snapshots() == [4]
    _push(store)
    gate._maybe_snapshot_coverage(fold(store.read_all()))
    assert _snapshots() == [4], "the same plateau is not a new sample"
    for _ in range(2):
        _push(store)
    gate._maybe_snapshot_coverage(fold(store.read_all()))
    assert _snapshots() == [4, 7], "the hard stall is"
