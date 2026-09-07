"""`Settings.node_open_budget_floor_usd`: refuse to OPEN a node when the spend ceiling has less
than this much left (docs/60 §60.9 A10; the measurement is docs/56 §156-157).

The ceiling stops a run AFTER a priced call crosses it, in the middle of whatever was buying it:
$5.91 of the 76-run corpus's $76.73 (7.7 %) landed after the last node a run ever evaluated. The
floor is the same stop asked one step earlier, at the decision to open a node cycle, and its value
is the measured knee — at $0.10 it refuses 61 empty cycles and costs exactly one real node (which
scored 0), while the audit's p75 ($0.4481) would have refused 54 real nodes including the corpus's
best. The boundary carries a 1e-9 tolerance because `1.00 - 0.92` is `0.0799…`.

It raises the SAME `BudgetExceeded` the ceiling raises, on the MAIN task, so everything built for
the ceiling — the in-flight-eval drain, `run_finished.reason = budget_exhausted`, the one-line
refusal — handles it unchanged. Both halves are driven below: the arithmetic on the accountant and
the placement on a real engine.
"""
from __future__ import annotations

import time
from pathlib import Path

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.cli.run_cmds import _run_engine_guarded
from looplab.core.config import Settings, settings_from_snapshot
from looplab.core.llm import BudgetExceeded, CostAccountant
from looplab.core.models import NodeStatus
from looplab.engine.options import EngineOptions
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from tests._source_scan import called_names
from tests.factories import make_engine

TOY = Path(__file__).resolve().parents[1] / "examples" / "toy_task.json"


# ------------------------------------------------------------------ the arithmetic

@pytest.mark.parametrize("limit, spent, floor, refused", [
    (1.00, 0.95, 0.10, True),      # $0.05 left, needs $0.10
    (1.00, 0.85, 0.10, False),     # $0.15 left
    (1.00, 0.90, 0.10, False),     # exactly the floor is NOT below it
    (1.00, 0.92, 0.08, False),     # §156's boundary: 1.00-0.92 = 0.0799… sits 4e-17 under 0.08
    (1.00, 0.92, 0.0800001, True), # …and a real cent-fraction above it still refuses
    (1.00, 1.20, 0.10, True),      # already over the ceiling: nothing may open
    (1.00, 0.95, 0.0, False),      # 0 = off
    (1.00, 0.95, -1.0, False),     # a negative floor is off, not "always refuse"
    (None, 0.95, 0.10, False),     # no ceiling: no remainder to test, inert
    (0.50, 0.00, 0.10, False),     # a fresh budget above the floor
    # A DECISION, re-affirmed 2026-09-07 against a review that read it as a defect ("a
        # --llm-budget-usd 0.05 smoke run becomes a no-op"). It is what the guard is FOR: on a
        # $0.05 ceiling the node is discarded unfinished exactly as it is on the last $0.05 of a
        # $1.00 one, and the refusal names both remedies (raise the budget, or set the floor to
        # 0). Clamping the floor to the ceiling would make the guard inert precisely where the
        # operator's whole budget cannot buy one node.
        (0.05, 0.00, 0.10, True),      # a budget smaller than one node is refused before the first
])
def test_require_headroom_truth_table(limit, spent, floor, refused):
    accountant = CostAccountant(limit=limit)
    accountant.spent = spent
    if refused:
        with pytest.raises(BudgetExceeded):
            accountant.require_headroom(floor, "node 3")
    else:
        accountant.require_headroom(floor, "node 3")


@pytest.mark.parametrize("floor", ["x", None, float("nan"), float("inf")])
def test_junk_floors_are_off_rather_than_refusing_everything(floor):
    accountant = CostAccountant(limit=1.0)
    accountant.spent = 0.99
    accountant.require_headroom(floor, "node")


def test_the_refusal_is_the_ceilings_own_class_and_names_the_decision_it_pre_empted():
    accountant = CostAccountant(limit=1.0)
    accountant.spent = 0.95
    with pytest.raises(BudgetExceeded) as caught:
        accountant.require_headroom(0.10, "a new draft node")
    text = str(caught.value)
    assert text.startswith("LLM spend ceiling reached"), (
        "the sentence keeps the ceiling's opening words: `events/stop_account.py` recovers 'this "
        "was the operator's spend ceiling' from them")
    assert "before opening a new draft node" in text
    assert "$0.0500" in text and "$1.0000" in text and "`llm_budget_usd`" in text
    assert "`node_open_budget_floor_usd` of $0.1000" in text, "the refusal names the floor"
    assert "config.snapshot.json" in text and "invariant #6" in text, (
        "same remedy as the ceiling: the snapshot, not an env var")


def test_a_refused_headroom_check_spends_and_counts_nothing():
    accountant = CostAccountant(limit=1.0)
    accountant.spent = 0.95
    with pytest.raises(BudgetExceeded):
        accountant.require_headroom(0.10, "n")
    assert accountant.spent == 0.95 and accountant.calls == 0


# ------------------------------------------------------------------ the settings side

def test_the_defaults_and_the_legacy_pin():
    assert Settings().node_open_budget_floor_usd == 0.10, "the measured knee, not the audit's p75"
    assert EngineOptions().node_open_budget_floor_usd == 0.0, (
        "the library default is OFF: a bare Engine must not acquire a stop it never had")
    resumed = settings_from_snapshot({"llm_budget_usd": 1.0})
    assert resumed.node_open_budget_floor_usd == 0.0, (
        "a snapshot written before the field existed resumes with NO floor — re-entry never adds a "
        "stop to a run already in flight")
    assert resumed.developer_crash_pause_after == 1, "and the historical crash rule"
    assert settings_from_snapshot(
        {"llm_budget_usd": 1.0, "node_open_budget_floor_usd": 0.25}).node_open_budget_floor_usd == 0.25


# ------------------------------------------------------------------ the engine

class _SlowSandbox(SubprocessSandbox):
    """The toy eval takes tens of milliseconds; the drain property needs one still in flight when
    the next node is refused, so every eval holds for a moment first."""

    def run(self, *args, **kwargs):
        time.sleep(0.6)
        return super().run(*args, **kwargs)


def _engine(run_dir, *, spent, floor, limit=1.0, bump=0.0, **knobs):
    """A real toy engine whose Developer meters on ONE accountant that already stands at `spent`.

    `find_cost_accountants` walks the roles for `.accountant`, exactly as it finds a live client's;
    `bump` charges the accountant per build, so the remainder crosses the floor MID-run. The same
    charging pair is offered as `role_factory`, so a speculative producer (an ISOLATED role pair)
    meters on the run's one accountant too — as `run_cost_accountant` makes every live client do."""
    task = ToyTask.load(TOY)
    accountant = CostAccountant(limit=limit)
    accountant.spent = spent

    def roles():
        researcher, developer = task.build_roles()
        developer.accountant = accountant
        if bump:
            real = developer.implement

            def implement(idea, *args, **kwargs):
                accountant.spent += bump
                return real(idea, *args, **kwargs)
            developer.implement = implement
        return researcher, developer

    researcher, developer = roles()
    return make_engine(run_dir, task=task, researcher=researcher, developer=developer,
                       n_seeds=2, max_nodes=4, node_open_budget_floor_usd=floor,
                       role_factory=roles, **knobs), accountant


def _events(run_dir):
    return EventStore(run_dir / "events.jsonl").read_all()


def test_below_the_floor_the_next_node_is_not_opened_and_the_run_stops_naming_it(tmp_path):
    eng, _ = _engine(tmp_path / "r", spent=0.95, floor=0.10)
    with pytest.raises(BudgetExceeded) as caught:
        anyio.run(eng.run)
    assert "`node_open_budget_floor_usd` of $0.1000" in str(caught.value)
    assert "$0.0500" in str(caught.value)
    types = [e.type for e in _events(tmp_path / "r")]
    assert "node_created" not in types and "node_building" not in types, (
        "refused BEFORE opening: no node id was reserved, no build was paid for")


def test_above_the_floor_the_node_opens(tmp_path):
    eng, _ = _engine(tmp_path / "r", spent=0.85, floor=0.10)
    state = anyio.run(eng.run)
    assert state.finished and len(state.nodes) == 4


def test_the_cli_records_the_designed_end_of_a_budgeted_run_not_a_crash(tmp_path):
    """Same class, same path: `_run_engine_guarded` writes `run_finished {reason: budget_exhausted}`
    with the floor's own sentence, exactly as it does for the ceiling."""
    eng, _ = _engine(tmp_path / "r", spent=0.95, floor=0.10)
    with pytest.raises(BudgetExceeded):
        _run_engine_guarded(eng)
    finished = [e for e in _events(tmp_path / "r") if e.type == "run_finished"]
    assert len(finished) == 1
    assert finished[0].data["reason"] == "budget_exhausted"
    assert "before opening" in finished[0].data["error"]


def _skeleton(events):
    volatile = {"run_id", "run_uid", "config_hash", "eval_seconds", "ts", "started_at",
                "finalize_scope"}                # a digest over the run's own identity
    return [(e.type, {k: v for k, v in e.data.items() if k not in volatile}
             if e.type in ("node_created", "node_evaluated", "node_failed", "run_finished") else None)
            for e in events]


def test_a_zero_floor_is_byte_identical_to_no_floor_and_no_ceiling_is_inert(tmp_path):
    off, _ = _engine(tmp_path / "off", spent=0.95, floor=0.0)
    unlimited, _ = _engine(tmp_path / "unlimited", spent=0.95, floor=0.10, limit=None)
    bare, _ = _engine(tmp_path / "bare", spent=0.95, floor=0.10, limit=None)
    bare.node_open_budget_floor_usd = EngineOptions().node_open_budget_floor_usd  # the library default
    for eng in (off, unlimited, bare):
        assert anyio.run(eng.run).finished
    assert _skeleton(_events(tmp_path / "off")) == _skeleton(_events(tmp_path / "unlimited")) \
        == _skeleton(_events(tmp_path / "bare"))
    assert [e.type for e in _events(tmp_path / "off")].count("node_evaluated") == 4


def test_an_evaluation_in_flight_when_the_floor_fires_still_lands_its_terminal(tmp_path):
    """The drain property, on the path where an eval outlives the turn that admitted it: Card
    selection with a speculative producer, which elects the NEXT build precisely while the current
    node is being scored. The floor refuses that election (`_request_card_build`) with a node
    pending, and its score is not lost — `Engine.run` drains the eval before re-raising, exactly
    as for the ceiling (`tests/test_budget_ceiling_drains_the_inflight_eval.py`)."""
    eng, accountant = _engine(tmp_path / "r", spent=0.80, floor=0.10, bump=0.10,
                              sandbox=_SlowSandbox(), card_driven_selection=True,
                              speculation_depth=1)
    if not eng._speculation_enabled() or eng._producer_role_pair() is None:
        pytest.skip("this build does not admit a spelled positive depth on the toy adapter")
    pending_at_refusal: list[list[int]] = []
    real = eng._refuse_node_open_below_floor

    def spy(what):
        try:
            real(what)
        except BudgetExceeded:
            state = fold(eng.store.read_all())
            pending_at_refusal.append(
                [n.id for n in state.nodes.values() if n.status is NodeStatus.pending])
            raise
    eng._refuse_node_open_below_floor = spy

    with pytest.raises(BudgetExceeded) as caught:
        anyio.run(eng.run)
    assert pending_at_refusal and pending_at_refusal[-1], (
        f"the refusal fired with no evaluation in flight ({pending_at_refusal}); the property "
        "under test is what happens to the one that WAS")
    assert "speculative Card build" in str(caught.value)
    state = fold(_events(tmp_path / "r"))
    for node_id in pending_at_refusal[-1]:
        assert state.nodes[node_id].status is NodeStatus.evaluated, (
            f"node {node_id} was mid-score when the floor fired and lost its `node_evaluated`")
    assert accountant.spent >= 0.90, "the floor fired because the builds charged the remainder away"


# ------------------------------------------------------------------ placement

def test_the_gate_is_asked_on_the_main_task_at_every_open_decision_and_never_in_a_worker():
    """Placement is the safety argument: raised inside `_create_node` under the `llm_parallel`
    fan-out, a `BudgetExceeded` is swallowed by `_create_node_guarded` into one node's terminal
    and the run goes on spending. So the call lives at the main-task decision sites and is ABSENT
    from the three methods that can run in a worker."""
    from looplab.engine import card_reservation, orchestrator, speculation
    gate = "self._refuse_node_open_below_floor"
    for owner, name in ((orchestrator.Engine, "_handle_create_actions"),
                        (speculation.SpeculationMixin, "_request_card_build"),
                        (card_reservation.CardReservationMixin, "_stage_card_creates")):
        assert gate in called_names(getattr(owner, name)), f"{name} lost the node-open gate"
    creates = called_names(orchestrator.Engine._handle_create_actions)
    assert creates.count(gate) == 3, (
        "three decisions in the create branch: the Card lane before its claim, each parallel "
        "chunk before its paid proposal, and each serial node")
    for name in ("_create_node", "_create_node_scoped", "_create_node_guarded"):
        assert gate not in called_names(getattr(orchestrator.Engine, name)), (
            f"{name} may run in an anyio worker thread; a raise there does not end the run")


def test_the_gate_asks_every_reachable_accountant_and_tolerates_a_legacy_one(tmp_path):
    eng, accountant = _engine(tmp_path / "r", spent=0.95, floor=0.10)
    eng.researcher.accountant = object()       # a fake with no `require_headroom`: skipped
    with pytest.raises(BudgetExceeded):
        eng._refuse_node_open_below_floor("a probe")
    accountant.spent = 0.5
    eng._refuse_node_open_below_floor("a probe")


def test_the_mixin_rule_is_off_on_a_bare_engine_and_for_junk():
    from looplab.engine.orchestrator import Engine
    eng = Engine.__new__(Engine)                 # no __init__, no roles: nothing to ask, no raise
    eng._refuse_node_open_below_floor("n")
    for junk in (0, -1, "x", None, True):
        eng.node_open_budget_floor_usd = junk
        eng._refuse_node_open_below_floor("n")
