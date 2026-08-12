"""The RUN-OPENING deep-research think: a fresh run grounds its FIRST proposal, not its second.

The Deep-Research cadence has always been able to fire at node-count 1 at the earliest, because
`_maybe_deep_research` / `_due_research_trigger` both answered "nothing to do" while `len(state.nodes)`
was 0. 2026-08-07 removed the node-counted WINDOW from the front of the run (`deep_research_window`:
`0` = start immediately) and that bought the overlap with the FIRST eval — but not the grounding of
the first PROPOSAL, which is the one idea in the run with no results behind it and, in Card-driven
mode, the one that mints card-0's action identity.

Measured over the shipped run corpus on 2026-08-12: of 22 runs carrying any research row, NOT ONE
ever recorded one at `at_node=0`. The earliest anywhere is `at_node=1`
(`runs/lt-recovery-0811`, the only corpus run on the shipped `deep_research_every=0`); every other
run's first memo landed at `at_node=2..6`. `runs/rubertlite-dr-unified-v5` (`deep_research_every=3`,
`concurrent_research=true`, hours per node) spent its first 13.5 minutes on an ungrounded proposal
and had recorded zero research rows of either kind by node 0's eval.

These drive the property rather than the call: a real toy run whose event log is then read for
ORDER, and a real `EventStore` re-entered the way a resume re-enters it.
"""
from __future__ import annotations

import types

import anyio

from factories import make_engine
from looplab.core.models import ResearchMemo
from looplab.engine.cadence import DEEP_RESEARCH_OFF
from looplab.engine.orchestrator import Engine
from looplab.engine.research_cadence import ResearchCadenceMixin
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


class _CountingResearcher:
    """A DeepResearcher stand-in that records the node-count it was asked at."""

    def __init__(self):
        self.at_nodes: list[int] = []
        self.triggers: list[str] = []

    def research(self, state, *, trigger: str) -> ResearchMemo:
        self.at_nodes.append(len(state.nodes))
        self.triggers.append(trigger)
        return ResearchMemo(at_node=len(state.nodes), trigger=trigger,
                            summary=f"opening memo at {len(state.nodes)}",
                            recommended_directions=["start from a ridge baseline"])


# ------------------------------------------------------ the operator-facing property, end to end

def test_the_first_memo_lands_before_the_first_node_of_a_real_run(tmp_path):
    """A real offline run, then its own event log read for ORDER.

    This is the whole complaint: the run must not propose its first experiment before it has
    thought. `research_completed` has to precede the first `node_created` in the durable log —
    which is also what puts the memo in the folded state `_cue_research_memo` reads when the
    Researcher is asked for that first idea.
    """
    researcher = _CountingResearcher()
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=2,
                      deep_researcher=researcher, deep_research_every=0)
    anyio.run(eng.run)

    kinds = [e.type for e in eng.store.read_all()]
    assert "research_completed" in kinds, "the run never thought at all"
    assert "node_created" in kinds, "the run never proposed anything — the ordering is vacuous"
    assert kinds.index("research_completed") < kinds.index("node_created"), (
        "the first experiment was proposed before the first memo was durable")
    assert kinds.index("research_attempted") < kinds.index("research_completed")

    opening = next(e for e in eng.store.read_all() if e.type == "research_completed")
    assert opening.data["at_node"] == 0
    assert opening.data["trigger"] == "run_start"
    assert researcher.at_nodes[0] == 0, "the opening think saw a run that had already started"

    # …and it is visible as advisory state while the run still has no nodes, which is what makes it
    # reach the first proposal rather than the second.
    prefix = []
    for event in eng.store.read_all():
        prefix.append(event)
        if event.type == "research_completed":
            break
    grounded = fold(prefix)
    assert grounded.nodes == {}
    assert [m["at_node"] for m in grounded.research] == [0]


# ------------------------------------------------------------------ replay safety (invariant 3)

def _harness(store: EventStore, *, every: int, researcher) -> Engine:
    eng = Engine.__new__(Engine)
    eng.store = store
    eng.deep_researcher = researcher
    eng.deep_research_every = every
    eng._research_verify = False
    eng._track_hypotheses = False
    return eng


def _run_start_rows(store: EventStore) -> list:
    return [e for e in store.read_all()
            if e.type in ("research_attempted", "research_completed")
            and e.data.get("at_node") == 0]


def test_the_opening_think_is_bought_once_across_a_resume(tmp_path):
    """Re-entering the gate over the SAME log must be a no-op — the side effect is gated on the
    events it wrote (invariant 3), not on process memory."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    researcher = _CountingResearcher()

    eng = _harness(store, every=0, researcher=researcher)
    eng._maybe_deep_research(fold(store.read_all()))
    assert researcher.at_nodes == [0], "the opening think never happened at all"

    for _ in range(2):                       # two more processes over the same run directory
        eng = _harness(store, every=0, researcher=researcher)
        eng._maybe_deep_research(fold(store.read_all()))

    assert researcher.at_nodes == [0], "the opening think was re-purchased on resume"
    kinds = [e.type for e in _run_start_rows(store)]
    assert kinds.count("research_attempted") == 1
    assert kinds.count("research_completed") == 1


def test_a_killed_opening_think_is_spent_not_re_paid(tmp_path):
    """The receipt goes down BEFORE the provider call, so a log carrying an `at_node=0` attempt with
    no memo is a think that was already bought. A resume must not buy it again."""
    def _drive(seed_attempt: bool):
        store = EventStore(tmp_path / f"events-{seed_attempt}.jsonl")
        store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
        if seed_attempt:
            store.append("research_attempted", {"attempt_id": "a0", "trigger": "run_start",
                                                "manual": False, "at_node": 0})
        researcher = _CountingResearcher()
        _harness(store, every=0, researcher=researcher)._maybe_deep_research(fold(store.read_all()))
        return researcher.at_nodes

    # The control: with no receipt in the log this exact harness DOES buy the think, so the
    # assertion below is about the receipt and not about some other gate declining.
    assert _drive(seed_attempt=False) == [0]
    assert _drive(seed_attempt=True) == [], (
        "a paid-but-unreconciled opening think was bought twice")


# --------------------------------------------------------------------------- what turns it off

def test_off_stays_off_and_an_unwired_stage_records_nothing(tmp_path):
    """`-1` is the off switch (`cadence.DEEP_RESEARCH_OFF`), and it must also mean no run-opening
    think — an operator who spelled the stage off does not acquire a paid call at run start. With no
    model wired the branch declines for the cadence's own reason: `_run_deep_research` would record a
    STUB memo, spending the run's one opening slot on a think nobody could have run."""
    def _drive(every, researcher):
        store = EventStore(tmp_path / f"events-{every}-{researcher is None}.jsonl")
        store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
        _harness(store, every=every, researcher=researcher)._maybe_deep_research(
            fold(store.read_all()))
        return [e.type for e in _run_start_rows(store)]

    # The control: the ONLY thing separating each case below from a recorded opening think.
    assert _drive(0, _CountingResearcher()) == ["research_attempted", "research_completed"]
    assert _drive(3, _CountingResearcher()) == ["research_attempted", "research_completed"]

    for every, researcher in ((DEEP_RESEARCH_OFF, _CountingResearcher()),
                              (-7, _CountingResearcher()),
                              (0, None), (3, None)):
        assert _drive(every, researcher) == [], (
            f"deep_research_every={every} / researcher={researcher!r} recorded an opening think")
        if researcher is not None:
            assert researcher.at_nodes == []


def test_a_manual_request_at_run_start_still_wins_over_the_opening_branch(tmp_path):
    """The manual branch is evaluated first and answers even with no model wired — the operator is
    waiting on the recorded answer. It must keep doing so at node-count 0, including when the
    cadence is spelled off."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    store.append("deep_research", {})
    eng = _harness(store, every=DEEP_RESEARCH_OFF, researcher=None)
    eng._maybe_deep_research(fold(store.read_all()))

    memo = next(e for e in store.read_all() if e.type == "research_completed")
    assert memo.data["trigger"] == "manual" and memo.data["served_manual"] is True


# ------------------------------------------------------------- the window is not re-phased by it

def test_the_opening_think_does_not_move_the_ordinary_window():
    """The opening pass leaves an `at_node=0` mark, and `max(marks, default=0)` already reads 0 as
    "never fired" — so an operator's `every` still measures from the start of the run. Driven
    through the real gate at each node-count rather than asserted about the arithmetic.
    """
    fired: list[str] = []

    def _host(marks):
        return types.SimpleNamespace(
            deep_researcher=object(),
            deep_research_every=3,
            _outstanding_manual_research=lambda _s: 0,
            _already_researched_at=lambda _s, n: n in marks,
            _cadence_research_marks=lambda _s: marks,
            _cadence_due=Engine._cadence_due,
            _ground_run_start=ResearchCadenceMixin._ground_run_start,
            _run_deep_research=lambda state, *, trigger, manual: (
                fired.append(f"{trigger}@{len(state.nodes)}") or state))

    def _state(n):
        return types.SimpleNamespace(
            nodes={i: object() for i in range(n)}, research=[], strategy_history=[],
            research_requests=[], research_served=0, pending_nodes=lambda: [])

    marks: set[int] = set()
    for n in range(0, 5):
        host = _host(marks)
        host._ground_run_start = lambda state: ResearchCadenceMixin._ground_run_start(host, state)
        ResearchCadenceMixin._maybe_deep_research(host, _state(n))
        if fired and fired[-1].endswith(f"@{n}"):
            marks.add(n)                      # the memo/attempt row the real gate would have written

    assert fired == ["run_start@0", "cadence@3"], (
        "an `every=3` operator's window must still be three nodes wide from the run's start")
