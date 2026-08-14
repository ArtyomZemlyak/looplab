"""The run's WIDTH comes from what the research proposed, not only from what the box has.

docs/29 F1, asked as: *"if I want to run one experiment per card — who decides that and how? Ideally
automatically, from the propose."*

Until now `eval_parallel`/`llm_parallel` settled ONCE at run start and were pinned into `run_started`,
and AUTO meant one experiment per detected GPU — the width was a fact about the BOX. A per-Card
footprint moved that node's reservation and nothing let the proposals move the run. The measured cost
is in docs/29 F1b: on `rubertlite-dr-unified-v5` AUTO settled `eval_parallel: 2` off two H200s while
both Cards declared `{"gpus": 2}`, so node 0 held BOTH devices for its whole lifecycle and card-1
blocked on `_acquire_gpus(2)` — a run that `run_started` described as width 2 and that ran serially at
double the per-node cost.

The width cannot simply be recomputed per turn, and that constraint is what shapes everything here.
It is pinned so a resume on a different box continues the run's own treatment (engine invariant #6),
so a proposal-derived width needs a DURABLE event that re-pins it mid-run. These tests drive that
event: a real Card board, the real decision method, a real event log, and a real resume onto a
different box that must reach the SAME width from the log alone.
"""
from __future__ import annotations

import anyio
import pytest

import looplab.engine.orchestrator as _orch
from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import ToyObjectiveDeveloper, ToyResearcher
from looplab.core.models import Idea, RunState
from looplab.engine.orchestrator import Engine
from looplab.engine.widths import per_experiment_gpu_budget, proposal_derived_width
from looplab.events.replay import _on_run_started, _on_run_width_settled, fold
from looplab.events.types import EV_CARD_ADDED, EV_RUN_WIDTH_SETTLED
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree


def _gpu_capable(monkeypatch) -> None:
    """Speak for a GPU-capable task — the same TaskAdapter hook the scheduler reads.

    ToyTask declares `gpu_capable() -> False` (closed-form arithmetic), and a CPU-locked task has no
    GPU-derived width at all. Every property about a width that follows the DEVICES needs a task the
    devices can serve, so this is the production signal, not a test-only seam.
    """
    monkeypatch.setattr(ToyTask, "gpu_capable", lambda self: True)


def _engine(run_dir, *, gpus=(0, 1), **kw) -> Engine:
    task = ToyTask()
    researcher = ToyResearcher(task.bounds, seed=task.seed, step=task.step)
    researcher.client = object()          # what `_build_calls_an_llm` reads (agents/roles.py)
    kw.setdefault("card_driven_selection", True)
    kw.setdefault("proposal_width", True)
    kw.setdefault("eval_parallel", 0)
    kw.setdefault("llm_parallel", 0)
    return Engine(run_dir, task=task, researcher=researcher,
                  developer=ToyObjectiveDeveloper(), sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=2, max_nodes=4), n_seeds=2, max_nodes=4, **kw)


def _propose(engine, *footprints) -> None:
    """Mint one native, selection-ready Card per declared footprint, through the real planner.

    `_plan_native_card` + a real `card_added` append is the production mint; the resulting Card is
    `selection_ready` for the same reason a live proposal's is, so `_proposal_footprints` reads the
    same population the Card queue selects from rather than a hand-built RunState.
    """
    for index, gpus in enumerate(footprints):
        idea = Idea(operator="draft", params={"x": float(index)}, rationale="because",
                    hypothesis=f"hypothesis {index}",
                    footprint=None if gpus is None else {"gpus": gpus})
        events = engine.store.read_all()
        plan = engine._plan_native_card(events, fold(events), idea, parents=[],
                                        parent_generations={}, scored_against=None,
                                        source="researcher", at_node=index + 1)
        assert plan.disposition == "mint", plan.disposition
        engine.store.append(EV_CARD_ADDED, plan.payload)


def _width_rows(engine) -> list[dict]:
    return [e.data for e in engine.store.read_all() if e.type == EV_RUN_WIDTH_SETTLED]


# --------------------------------------------------------------------------- #
# The rule itself
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("pool,footprints,ceiling,expected,why", [
    # THE MEASURED CASE (docs/29 F1b): two Cards at {"gpus": 2} on two H200s. AUTO said 2 and the run
    # went serial; the truth is 1, and saying 1 is what makes `run_started` describe the real run.
    (2, [2, 2], 2, 1, "two 2-GPU proposals on a 2-GPU box run one at a time"),
    (2, [1, 1], 2, 2, "two 1-GPU proposals fill the box"),
    # PROPOSALS EXCEEDING THE BOX: the surplus queues, the width never oversubscribes the pool.
    (2, [1, 1, 1, 1, 1], 2, 2, "five 1-GPU proposals on a 2-GPU box still run two at a time"),
    # …and the COUNT of open proposals is not a term at all: a board that momentarily holds one
    # ready card is board churn, not a proposal to go serial.
    (2, [1], 2, 2, "one ready 1-GPU card does not narrow a 2-GPU run to serial"),
    (2, [4], 2, 1, "a proposal wider than the whole box is queued at width 1, not refused"),
    # NEVER PAST THE LAUNCH PIN: a resume onto a bigger box keeps the run's own treatment (#6).
    (8, [1, 1, 1, 1], 2, 2, "a resumed run does not widen past what its launch authorized"),
    # An undeclared footprint takes exactly one device from `_resource_request_for_node`, so it is
    # one unit of demand and one device of need — not zero, and not the whole box.
    (4, [None, None], 4, 4, "an undeclared proposal needs one device, so the box stays full"),
    (4, [0, 0, 0], 4, 4, "CPU-only proposals need no device and cannot narrow the width"),
    # SAY NOTHING rather than guess. Each of these would otherwise produce a plausible wrong number.
    (2, [], 2, None, "an empty board is not a proposal to go serial"),
    (0, [1, 1], 2, None, "with no devices there is nothing to share out"),
    (2, [1], 0, None, "a ceiling below 1 is not a width"),
    (True, [1], 2, None, "a bool is not a pool"),
    (2, [1], True, None, "a bool is not a ceiling"),
])
def test_the_rule(pool, footprints, ceiling, expected, why):
    assert proposal_derived_width(pool, footprints, ceiling=ceiling) == expected, why


def test_a_malformed_declaration_falls_back_to_one_device_and_cannot_reshape_the_run():
    """A bool/float/string `gpus` is not a device count. It must degrade to the undeclared case.

    The alternative — trusting it — lets one malformed Researcher-authored number set the run's
    execution treatment, which is the class of thing `settle_width` refuses for every other width.
    """
    assert proposal_derived_width(4, [True, "2", 2.5, None], ceiling=4) == 4
    # …and a NEGATIVE declaration is malformed the same way, not a request for more devices: it
    # degrades to one device of need, so it leaves the width where the box put it.
    assert proposal_derived_width(4, [-8], ceiling=4) == 4
    # …while ONE valid wide declaration beside the malformed ones still narrows the run.
    assert proposal_derived_width(4, [True, "2", 4], ceiling=4) == 1


# --------------------------------------------------------------------------- #
# The property F1b's announcement depends on
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("pool", [1, 2, 4, 8])
@pytest.mark.parametrize("declared", [1, 2, 3, 4])
def test_the_per_experiment_gpu_budget_the_researcher_was_quoted_stays_true(pool, declared):
    """F1b tells the Researcher it may declare `pool // eval_parallel` GPUs per experiment.

    That announcement is made per proposal and read off the live width, so a width this feature moves
    RE-QUOTES it on the next turn. Capping the width at `pool // need` is what keeps the new quote at
    least as large as what the open proposals already declared — i.e. the engine can never admit a
    Card at `{"gpus": 2}` and then, one turn later, tell the next Card it may have one. Without the
    cap the width could exceed the pool and the budget would collapse to the oversubscribed `1`.
    """
    count = 3
    width = proposal_derived_width(pool, [declared] * count, ceiling=pool)
    budget = per_experiment_gpu_budget(pool, width)
    assert budget is not None and budget >= 1
    if declared <= pool:
        assert budget >= declared, (
            f"the run settled to width {width} on a pool of {pool}, which quotes the next proposal "
            f"{budget} GPU(s) after admitting proposals that declared {declared}")


# --------------------------------------------------------------------------- #
# The fold: reconstructible by replay, order-tolerant, total on a hand-edited row
# --------------------------------------------------------------------------- #

def _folded(*rows, run_started=None):
    state = RunState()
    payload = {"run_id": "r", "task_id": "t", "direction": "min", **(run_started or {})}
    for row in rows:
        if row == "run_started":
            _on_run_started(state, None, payload, None)
        else:
            _on_run_width_settled(state, None, row, None)
    return state


def test_the_repin_and_the_run_start_pin_may_be_spliced_in_either_order():
    """Invariant #5. Each fact has ONE writer and neither handler reads the other's field.

    This is the property the `speculation_depth` pair had to be re-cut to obtain: folding a re-pin
    onto `run_started`'s own field is NOT order-tolerant, because `_on_run_started` ASSIGNS, so a
    re-pin folded first is silently overwritten (measured there as 4 at splice position 0 and 0
    everywhere else).
    """
    pinned = {"eval_parallel": 4, "llm_parallel": 4}
    row = {"eval_parallel": 2, "llm_parallel": 2}
    before = _folded(row, "run_started", run_started=pinned)
    after = _folded("run_started", row, run_started=pinned)
    for state in (before, after):
        assert (state.eval_parallel, state.eval_parallel_settled) == (4, 2)
        assert (state.llm_parallel, state.llm_parallel_settled) == (4, 2)


def test_the_last_repin_wins_because_the_width_is_allowed_to_move_both_ways():
    """LWW, not the minimum the depth ratchet takes — and the difference is not cosmetic.

    A depth only ever narrows, so a minimum reproduced the engine's own sequence for free. A width
    follows the proposals: it narrows when they declare wide footprints and widens back when they
    stop. A minimum would make one wide proposal a permanent serialization of the run.
    """
    state = _folded("run_started", {"eval_parallel": 1}, {"eval_parallel": 3},
                    run_started={"eval_parallel": 4, "llm_parallel": 4})
    assert state.eval_parallel_settled == 3


def test_one_poisoned_axis_does_not_discard_a_valid_repin_of_the_other():
    state = _folded("run_started", {"eval_parallel": 2, "llm_parallel": "wide"},
                    run_started={"eval_parallel": 4, "llm_parallel": 4})
    assert state.eval_parallel_settled == 2
    assert state.llm_parallel_settled is None


@pytest.mark.parametrize("row", [
    {"eval_parallel": True},        # a bool is not a width (it is an int subclass)
    {"eval_parallel": 0},           # a LIVE 0 is serial, never AUTO — and never a re-pin
    {"eval_parallel": 2.5},
    {"eval_parallel": 2000},
    {"eval_parallel": "2"},
])
def test_a_hand_edited_row_cannot_reshape_the_execution_treatment(row):
    state = _folded("run_started", row, run_started={"eval_parallel": 4})
    assert state.eval_parallel_settled is None


# --------------------------------------------------------------------------- #
# The engine decision, over a real Card board
# --------------------------------------------------------------------------- #

def test_two_two_gpu_proposals_on_a_two_gpu_box_repin_the_run_to_serial(tmp_path, monkeypatch):
    """The measured v5 case, driven end to end: the log now says what the run actually does."""
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "run")
    assert engine._eval_parallel == 2 and engine._llm_parallel == 2   # AUTO off the box

    _propose(engine, 2, 2)
    state = fold(engine.store.read_all())
    # A hand-built state would prove nothing here: the population is the Card queue's own.
    assert len(engine._proposal_footprints(state)) == 2

    assert engine._settle_proposal_width(state) is True
    assert engine._eval_parallel == 1, "the run kept a width its own proposals cannot use"
    assert engine._llm_parallel == 1, "an AUTO build width did not follow the eval width down"

    rows = _width_rows(engine)
    assert len(rows) == 1 and rows[0]["eval_parallel"] == 1 and rows[0]["llm_parallel"] == 1
    # The decision's whole input is recorded, so the fold never re-derives anything from the box.
    assert rows[0]["evidence"] == {
        "gpu_pool": 2, "open_proposals": 2, "widest_declared_gpus": 2,
        "undeclared_proposals": 0, "launch_ceiling": 2,
    }
    assert rows[0]["previous"] == {"eval_parallel": 2, "llm_parallel": 2}

    # IDEMPOTENT: the same board must not append a second row every turn.
    assert engine._settle_proposal_width(fold(engine.store.read_all())) is False
    assert len(_width_rows(engine)) == 1


def test_one_gpu_proposals_leave_a_box_shaped_width_exactly_where_it_was(tmp_path, monkeypatch):
    """The common case must be byte-identical to today — no row, no log noise, no warning."""
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "run")
    _propose(engine, 1, 1)
    assert engine._settle_proposal_width(fold(engine.store.read_all())) is False
    assert _width_rows(engine) == []
    assert engine._eval_parallel == 2


def test_more_proposals_than_the_box_can_serve_do_not_oversubscribe_it(tmp_path, monkeypatch):
    """Five one-GPU proposals on a two-GPU box: the width stays 2 and the surplus QUEUES.

    Widening to 5 would put three nodes into eval slots that can only block in `_acquire_gpus`, and
    would re-quote the next proposal a per-experiment budget of `2 // 5 -> 1` while telling nobody.
    """
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "run")
    _propose(engine, 1, 1, 1, 1, 1)
    assert engine._settle_proposal_width(fold(engine.store.read_all())) is False
    assert engine._eval_parallel == 2
    assert _width_rows(engine) == [], "no row: the box-derived width was already the honest one"


def test_an_explicitly_spelled_width_is_never_repinned_by_the_proposals(tmp_path, monkeypatch):
    """An explicit number is the operator's, and the codebase's standing rule is that it is honoured.

    AUTO is a request to let something else decide, and this feature changes WHAT decides — from the
    box to the work. It does not acquire the right to overrule a number somebody typed.
    """
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "run", eval_parallel=2)
    _propose(engine, 2, 2)
    assert engine._settle_proposal_width(fold(engine.store.read_all())) is False
    assert engine._eval_parallel == 2 and _width_rows(engine) == []


def test_an_axis_an_operator_retuned_live_stays_the_operators(tmp_path, monkeypatch):
    """`budget_extend` is the top layer: pin < proposals < operator.

    They said it last, `_apply_control_overrides` re-applies it every turn, and a row here would be
    durable noise that changes nothing — the same reasoning, and the same key list, as
    `_repin_settled_widths`'s own stand-aside clause.
    """
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "run")
    _propose(engine, 2, 2)
    state = fold(engine.store.read_all())
    state.budget_overrides["eval_parallel"] = 2
    assert engine._settle_proposal_width(state) is False
    assert _width_rows(engine) == []


def test_a_calibration_run_never_repins_its_width(tmp_path, monkeypatch):
    """A calibration lane is launch-only and its complete execution envelope is receipt-bound.

    The same refusal `_apply_control_overrides` makes for `budget_extend`, one lane over — and it is
    an outright check rather than a consequence of the AUTO gate, because a refusal that depends on
    another rule holding is not a refusal.
    """
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "run")
    _propose(engine, 2, 2)
    engine._speculation_gate_calibration = True
    assert engine._settle_proposal_width(fold(engine.store.read_all())) is False
    assert _width_rows(engine) == []


def test_a_repinned_run_is_refused_as_speculation_calibration_evidence():
    """LOUDLY, and by name. A re-pin means the run changed execution treatment mid-log.

    That is exactly what the receipt asserts did not happen — the width is inside the runtime-scope
    digest and inside the replicate `_comparable_config` equality. The allow-list already refused an
    unlisted type, but anonymously; six GPU runs is too expensive a thing to debug from "unexpected
    event type".
    """
    from looplab.search.speculation_quality import _FORBIDDEN_CALIBRATION_LIFECYCLE_EVENTS
    assert EV_RUN_WIDTH_SETTLED in _FORBIDDEN_CALIBRATION_LIFECYCLE_EVENTS


def test_a_run_without_a_card_board_has_no_proposals_to_derive_a_width_from(tmp_path, monkeypatch):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "run", card_driven_selection=False)
    assert engine._settle_proposal_width(fold(engine.store.read_all())) is False


def test_the_setting_off_is_byte_identical_to_today(tmp_path, monkeypatch):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "run", proposal_width=False)
    _propose(engine, 2, 2)
    assert engine._settle_proposal_width(fold(engine.store.read_all())) is False
    assert engine._eval_parallel == 2 and _width_rows(engine) == []


# --------------------------------------------------------------------------- #
# Replay: the SAME width from the log alone, on a different box
# --------------------------------------------------------------------------- #

def test_a_resume_on_a_bigger_box_reaches_the_repinned_width_from_the_log_alone(tmp_path, monkeypatch):
    """The whole reason this is a durable event and not a per-turn recomputation.

    The derivation reads the LIVE GPU pool. Re-deriving on resume would reproduce, one layer up,
    exactly the defect pinning the widths was introduced to close: a run that narrowed itself to 1
    would silently widen again on a bigger host, mid-log, with nothing recording the change.
    """
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    first = _engine(tmp_path / "run")
    anyio.run(first.run)                      # writes run_started with the pin
    _propose(first, 2, 2)
    assert first._settle_proposal_width(fold(first.store.read_all())) is True
    assert first._eval_parallel == 1

    # A FOUR-GPU box. Startup AUTO resolves 4 before the re-pin; the log must pull it back to 1 —
    # and to the RE-PIN, not to `run_started`'s own 2.
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1, 2, 3])
    resumed = _engine(tmp_path / "run")
    assert resumed._eval_parallel == 4        # re-derived off the new box, before the re-pin
    state = fold(resumed.store.read_all())
    assert (state.eval_parallel, state.eval_parallel_settled) == (2, 1)
    resumed._repin_settled_widths(state)
    assert resumed._eval_parallel == 1, "the resumed engine re-derived its width from the new box"
    assert resumed._llm_parallel == 1


def test_the_launch_pin_and_not_the_last_repin_is_the_ceiling(tmp_path, monkeypatch):
    """A re-pin must never become its own ceiling, or the width ratchets monotonically down.

    Once the wide proposals are gone the run has to be able to return to the width it launched at —
    and to no more than that. Reading the ceiling off the LIVE `_eval_parallel` (or off the last
    re-pin) would make one 2-GPU Card a permanent serialization of the run.
    """
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "run")
    anyio.run(engine.run)
    _propose(engine, 2, 2)
    assert engine._settle_proposal_width(fold(engine.store.read_all())) is True
    assert engine._eval_parallel == 1
    assert fold(engine.store.read_all()).eval_parallel_settled == 1

    # The wide cards are claimed and leave the ready set; only one-GPU work remains. The run must
    # widen back to the width it LAUNCHED at (2) — never to the four the box now offers, and never
    # stay stuck at the 1 the last re-pin put it at.
    _propose(engine, 1, 1)
    state = fold(engine.store.read_all())
    for card in state.cards.values():
        if (card.footprint or {}).get("gpus") == 2:
            card.selection_ready = False
    assert [f for f in engine._proposal_footprints(state) if f == 2] == []
    assert engine._settle_proposal_width(state) is True
    assert engine._eval_parallel == 2


def test_a_legacy_log_that_never_repinned_keeps_the_pre_feature_behaviour(tmp_path, monkeypatch):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "run")
    anyio.run(engine.run)
    state = fold(engine.store.read_all())
    assert state.eval_parallel_settled is None
    assert engine._recorded_settled_width(state, "eval_parallel", 1024) == 2


def test_a_malformed_repin_degrades_to_the_pin_rather_than_to_not_recorded():
    """Losing the PIN as well would hand a hand-edited row the power to re-derive off the box.

    `0` from `_recorded_settled_width` means "not recorded", which every caller reads as "keep this
    process's own startup resolution" — i.e. exactly the box-derived width invariant #6 exists to
    stop. A malformed re-pin must fall back to the pin, not past it.
    """
    state = _folded("run_started", run_started={"eval_parallel": 4})
    state.eval_parallel_settled = "wide"      # only a hand-edited log reaches this
    assert Engine._recorded_settled_width(state, "eval_parallel", 1024) == 4


def test_the_refusal_names_the_row_that_actually_owns_the_width():
    """A refusal that blames `run_started` for a number `run_started` does not carry is a wrong map.

    Since this feature the recorded width may come from a `run_width_settled` re-pin, and telling an
    operator "run_started pinned 1" when their log's `run_started` plainly says 4 sends them to argue
    with the wrong event.
    """
    from looplab.engine.widths import settled_width_refusal
    plain = settled_width_refusal("eval_parallel", resolved=2, recorded=1)
    repinned = settled_width_refusal("eval_parallel", resolved=2, recorded=1, repinned=True)
    assert "run_started pinned 1" in plain
    assert "this run re-pinned itself to 1" in repinned and "run_started pinned" not in repinned


# --------------------------------------------------------------------------- #
# The cross-turn dispatcher
# --------------------------------------------------------------------------- #

def test_the_parallel_dispatcher_asks_its_own_semaphore_how_many_slots_it_has():
    """`anyio.Semaphore` cannot be resized, so its token TOTAL is the batch's real concurrency.

    The drained-pool test that releases an aged head's claim compares free tokens against that total.
    Against the LIVE `self._eval_parallel` it broke in both directions once any writer moved the
    width mid-batch — and there are three (the Strategist, `budget_extend`, and now this feature):
    LOWERED, it fires with siblings still holding GPUs and starves a wide head for the rest of its
    queue lifetime; RAISED, it can never fire and a genuinely unsatisfiable head wedges the batch on
    `_wait_for_gpu_change` with nothing running to move the epoch.

    An AST pin rather than a substring: what must hold is that the comparison reads the captured
    local and NOT the live attribute, and a commented-out copy of either would satisfy a substring.
    """
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(Engine._dispatch_evals))
    tree = ast.parse(source)
    compares = [node for node in ast.walk(tree)
                if isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Attribute)
                and node.left.attr == "value"]
    drained = [node for node in compares
               if isinstance(node.comparators[0], ast.BinOp)]
    assert drained, "the drained-pool test is gone; the aged-head escape hatch has no guard"
    for node in drained:
        assert isinstance(node.comparators[0].left, ast.Name), (
            "the drained-pool test reads a live attribute; it must read the batch's captured width")
        assert node.comparators[0].left.id == "batch_width"
