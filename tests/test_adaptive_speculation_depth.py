"""AUTO speculation depth adapts to the run's OWN measured evaluation duration.

`speculation_depth = -1` (AUTO, the shipped default) used to resolve once, at startup, from the
settled eval width — how many experiments can run at ONCE. That answers a capacity question, not the
one that decides whether a prefetch pays: a prefetch exists to overlap the Developer's PROVIDER
latency with a RUNNING evaluation, and when the evaluation takes 0.1 s there is nothing to overlap.

Measured on `examples/classification_task.json` AS IT SHIPPED BEFORE 2026-08-05 (the flat two-blob
variant; that example is now the concentric-rings task, which evaluates in 0.05-0.6 s — still far
under provider latency, so the conclusion carries), same command, both arms 8/8 nodes and the
identical champion: AUTO -> depth 1 cost **109 LLM calls / 1,265,911 tokens / 2348.8 s** against
`speculation_depth=0`'s **75 calls / 817,201 tokens / 2448.6 s** — 45% more calls and 55% more tokens
for a 4% wall-clock saving, and not from wrong predictions (one stale prefetch in nine requests).

What these tests pin is the mechanism: the depth may move, every move is a durable event, the fold
READS that event rather than re-deriving anything from the box, and the movement is a one-way ratchet
that cannot thrash.
"""
from __future__ import annotations

import json

import pytest

from looplab.core.models import NodeStatus, RunState
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.events.types import EV_SPECULATION_DEPTH_SETTLED
from tests.factories import make_engine


def _log(run_dir, *, depth: int, evals: list[float], builds: list[float],
         pinned: dict | None = None) -> None:
    """Write a run log whose build spans and eval seconds are exactly what this test measures.

    The event `ts` is what `_measured_build_seconds` reads, and `EventStore.append` stamps it from
    the clock — so the log is written directly here rather than through the store. That is also the
    honest shape for this property: the rule must be a pure function of a LOG, including one written
    by another process on another day.

    ``pinned`` merges a REAL `Engine._run_start_pinned_values()` into the run-start row — the lane
    token and policy scope a resume is checked against — so a test can drive the actual re-entry
    guards rather than a log they would refuse for an unrelated reason.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = [{"v": 1, "seq": 0, "ts": 0.0, "type": "run_started",
             "data": {"run_id": run_dir.name, "task_id": "toy", "goal": "g", "direction": "min",
                      "card_driven_selection": True, **(pinned or {}),
                      "speculation_depth": depth}}]
    seq = 1
    for index, span in enumerate(builds):
        base = 1000.0 + index * 1000.0
        rows.append({"v": 1, "seq": seq, "ts": base, "type": "node_building",
                     "data": {"node_id": index, "operator": "draft", "parent_ids": []}})
        seq += 1
        rows.append({"v": 1, "seq": seq, "ts": base + span, "type": "node_created",
                     "data": {"node_id": index, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}}, "code": "print(1)"}})
        seq += 1
    for index, seconds in enumerate(evals):
        rows.append({"v": 1, "seq": seq, "ts": 9000.0 + index,
                     "type": "node_evaluated",
                     "data": {"node_id": index, "generation": 0, "metric": 1.0,
                              "eval_seconds": seconds}})
        seq += 1
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows))


def _engine(tmp_path, name, *, auto: bool, depth: int = 1,
            evals: list[float] | None = None, builds: list[float] | None = None):
    run_dir = tmp_path / name
    _log(run_dir, depth=depth, evals=evals or [], builds=builds or [])
    engine = make_engine(run_dir, card_driven_selection=True, max_nodes=8)
    engine.speculation_depth = depth
    engine._speculation_depth_auto = auto
    return engine


def _measured(engine, **_ignored) -> RunState:
    return fold(engine.store.read_all())


class _LLMishDeveloper:
    """A Developer that declares provider latency, which is what AUTO admits the lane FOR.

    `_build_calls_an_llm` reads the ROLES, and the toy factory's Developer holds no client — so an
    engine built straight from `make_engine` settles AUTO to 0 and never enters the speculation lane
    at all. The re-entry guards below have to run against a real admitted lane or they refuse for
    reasons that have nothing to do with the depth.
    """

    is_code_generating = True
    client = None

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _admitted(run_dir, depth: int):
    """An Engine over ``run_dir`` on the real product speculation lane at ``depth``."""
    probe = make_engine(run_dir, card_driven_selection=True, max_nodes=8)
    return make_engine(run_dir, card_driven_selection=True, max_nodes=8,
                       developer=_LLMishDeveloper(probe.developer), speculation_depth=depth)


def _ratcheted_run(tmp_path, name: str, *, depth: int = 1):
    """A run dir whose log is a REAL admitted-lane run start that then ratcheted itself to 0."""
    run_dir = tmp_path / name
    launcher = _admitted(run_dir, depth)
    assert launcher._speculation_gate_admitted is True
    _log(run_dir, depth=depth, evals=[0.1, 0.1], builds=[120.0, 120.0],
         pinned=launcher._run_start_pinned_values())
    launcher.speculation_depth = depth
    launcher._speculation_depth_auto = True
    assert launcher._settle_speculation_depth(_measured(launcher)) is True
    entry = fold(launcher.store.read_all())
    assert (entry.speculation_depth_pinned, entry.speculation_depth) == (depth, 0)
    return run_dir, entry


def test_a_spelled_depth_is_never_settled_away(tmp_path):
    """AUTO-only, like every other AUTO settling rule: an operator who SPELLED a depth gets it."""
    engine = _engine(tmp_path, "spelled", auto=False,
                     evals=[0.1, 0.1, 0.1], builds=[120.0, 130.0, 125.0])
    state = _measured(engine)
    assert engine._settle_speculation_depth(state) is False
    assert engine.speculation_depth == 1
    assert not [e for e in engine.store.read_all() if e.type == EV_SPECULATION_DEPTH_SETTLED]


def test_one_fast_node_cannot_switch_off_the_treatment(tmp_path):
    engine = _engine(tmp_path, "one-sample", auto=True, evals=[0.1], builds=[120.0])
    state = _measured(engine)
    assert engine._settle_speculation_depth(state) is False
    assert engine.speculation_depth == 1


def test_evals_too_short_to_hide_a_build_ratchet_the_depth_to_zero(tmp_path):
    # The live fast side: ~0.1 s evals against builds measured in minutes.
    engine = _engine(tmp_path, "fast-evals", auto=True,
                     evals=[0.11, 0.09, 0.12], builds=[131.0, 147.0, 122.0])
    state = _measured(engine)
    assert engine._settle_speculation_depth(state) is True
    assert engine.speculation_depth == 0

    rows = [e for e in engine.store.read_all() if e.type == EV_SPECULATION_DEPTH_SETTLED]
    assert len(rows) == 1
    data = rows[0].data
    assert data["depth"] == 0 and data["previous"] == 1
    # The evidence must be complete enough that the fold never has to re-measure anything.
    evidence = data["evidence"]
    assert evidence["eval_samples"] == 3 and evidence["build_samples"] == 3
    assert evidence["median_eval_seconds"] == pytest.approx(0.11)
    assert evidence["median_build_seconds"] == pytest.approx(131.0)
    assert evidence["eval_fraction_of_build"] < evidence["min_eval_fraction"]
    # …and the fold adopts it.
    assert fold(engine.store.read_all()).speculation_depth == 0


def test_evals_long_enough_to_hide_a_build_keep_the_prefetch(tmp_path):
    """The slow side. A prefetch here overlaps real latency, which is what AUTO turned it on for."""
    engine = _engine(tmp_path, "slow-evals", auto=True,
                     evals=[600.0, 540.0, 660.0], builds=[131.0, 147.0, 122.0])
    state = _measured(engine)
    assert engine._settle_speculation_depth(state) is False
    assert engine.speculation_depth == 1
    assert fold(engine.store.read_all()).speculation_depth == 1


def test_the_threshold_is_a_ratio_not_an_absolute_number_of_seconds(tmp_path):
    """A run whose builds are slow because the ENDPOINT is slow should keep prefetching at eval
    durations a fast endpoint would not justify — "fast" only means anything relative to the provider
    latency the overlap is supposed to hide."""
    quick = _engine(tmp_path, "quick-endpoint", auto=True,
                    evals=[2.0, 2.0], builds=[5.0, 5.0])
    assert quick._settle_speculation_depth(_measured(quick)) is False   # 40% of a build
    slow = _engine(tmp_path, "slow-endpoint", auto=True,
                   evals=[2.0, 2.0], builds=[900.0, 900.0])
    assert slow._settle_speculation_depth(_measured(slow)) is True      # 0.2% of a build


def test_the_fold_takes_the_minimum_so_it_is_order_tolerant_and_idempotent(tmp_path):
    """Invariant #5. A ratchet folded as last-write-wins would land on a different treatment when two
    rows are spliced in the other order, and a duplicated stale row could raise the depth back up."""
    engine = _engine(tmp_path, "fold-order", auto=True, depth=4)
    engine.store.append(EV_SPECULATION_DEPTH_SETTLED, {"depth": 2, "previous": 4})
    engine.store.append(EV_SPECULATION_DEPTH_SETTLED, {"depth": 1, "previous": 2})
    assert fold(engine.store.read_all()).speculation_depth == 1
    engine.store.append(EV_SPECULATION_DEPTH_SETTLED, {"depth": 2, "previous": 4})   # replayed/stale
    assert fold(engine.store.read_all()).speculation_depth == 1
    # A malformed row cannot change the search treatment at all.
    for bad in ({"depth": True}, {"depth": "0"}, {"depth": -1}, {"depth": 65}, {}):
        engine.store.append(EV_SPECULATION_DEPTH_SETTLED, bad)
    assert fold(engine.store.read_all()).speculation_depth == 1


def test_the_fold_is_order_tolerant_against_run_started_itself():
    """SPLICE AGAINST `run_started`, the one event whose order the minimum could not survive.

    The sibling above only ever splices settle rows against each OTHER, which the minimum trivially
    handles. `run_started` ASSIGNS, so while both facts lived in one field a settle row folded BEFORE
    it was simply overwritten: measured on this exact log, depth 4 at splice position 0 and 0 at every
    other position. That was latent — the engine appends `run_started` first by construction — but it
    made the handler's own "order-tolerant and idempotent" claim false, and this codebase writes
    ordering preconditions down (invariant #1 does it for `EV_NODE_EVAL_STARTED`) rather than leaving
    them as properties of an event. Here the precondition could be REMOVED instead, by folding the
    launch pin and the adaptive floor as two independent facts, so there is now no order requirement
    between these two handlers at all — which is what this pins.
    """
    from looplab.events.eventstore import Event

    rows = [
        ("run_started", {"run_id": "r", "task_id": "toy", "goal": "g", "direction": "min",
                         "card_driven_selection": True, "speculation_depth": 4}),
        ("node_building", {"node_id": 0, "operator": "draft", "parent_ids": []}),
        ("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                          "idea": {"operator": "draft", "params": {}}, "code": "print(1)"}),
    ]
    settle = ("speculation_depth_settled", {"depth": 0, "previous": 4})

    folded = []
    for position in range(len(rows) + 1):
        spliced = rows[:position] + [settle] + rows[position:]
        events = [Event(v=1, seq=index, ts=float(index), type=kind, data=data)
                  for index, (kind, data) in enumerate(spliced)]
        state = fold(events)
        folded.append((state.speculation_depth_pinned, state.speculation_depth_settled,
                       state.speculation_depth))
    # Every splice position agrees, on all three facts — including position 0, where the settle row
    # is folded before the pin that bounds it even exists.
    assert folded == [(4, 0, 0)] * (len(rows) + 1), folded

    # And the guarantee is exactly a MINIMUM against the pin, not "the last settle row wins": a row
    # the engine never wrote, naming a depth ABOVE the pin, is inert in either order.
    for order in ([rows[0], ("speculation_depth_settled", {"depth": 9, "previous": 4})],
                  [("speculation_depth_settled", {"depth": 9, "previous": 4}), rows[0]]):
        events = [Event(v=1, seq=index, ts=float(index), type=kind, data=data)
                  for index, (kind, data) in enumerate(order)]
        assert fold(events).speculation_depth == 4


def test_a_log_with_no_settle_rows_keeps_exactly_its_pinned_depth(tmp_path):
    """Reader-side default (invariant #5): every pre-existing log behaves as it always did."""
    engine = _engine(tmp_path, "legacy", auto=True, depth=3)
    assert fold(engine.store.read_all()).speculation_depth == 3


def test_a_resume_adopts_the_last_recorded_depth_not_the_box(tmp_path):
    """The property the startup pin was protecting, kept — by reading the LOG rather than re-deriving.

    A resume on a box with a different eval width must continue this run's own search treatment.
    `_require_pinned_speculation_receipt` reads the LAUNCH pin and the ADAPTIVE floor as two separate
    facts and adopts the second, so the resuming process runs at 0 rather than re-resolving 1 off its
    own hardware.

    DRIVES THE REAL METHOD. This used to hand-simulate the adoption with
    `resumed.speculation_depth = entry.speculation_depth  # _reentry_repin's adoption` — which is the
    one thing a resume test must not do, because the defect it is guarding against is precisely "the
    adoption and the check drifted apart". While it said that, `_require_pinned_speculation_receipt`
    was in fact REFUSING this exact resume whenever the log carried a receipt.
    """
    run_dir, entry = _ratcheted_run(tmp_path, "resume")

    resumed = _admitted(run_dir, -1)       # AUTO, re-resolved off THIS box
    assert resumed.speculation_depth == 1 and resumed._speculation_depth_auto is True
    resumed._require_pinned_speculation_receipt(entry)     # the real guard, and it must NOT raise
    assert resumed.speculation_depth == 0
    assert resumed._reentry_repin() is False               # ...and the real re-entry agrees
    assert resumed.speculation_depth == 0
    assert resumed._speculation_enabled() is False


def test_a_spelled_depth_that_matches_the_launch_pin_resumes_a_ratcheted_run(tmp_path):
    """DOOR 1. The advice the ratchet prints must work on the run directory it is printed for.

    The warning used to end "`-s speculation_depth=N` keeps it on" with the PRE-settle depth, and
    following it on the same run dir raised `SpeculationAuthorizationError` saying the depth "was
    pinned at 0" — a value `run_started` never contained; the 0 came from the settle row. Spelling the
    LAUNCH PIN is now admitted (it agrees with what run start recorded), and the run still runs at the
    settled depth, because the ratchet is durable and no resume flag lifts it. That is why the warning
    now points at LAUNCH instead.
    """
    from looplab.engine.orchestrator import SpeculationAuthorizationError

    run_dir, entry = _ratcheted_run(tmp_path, "advice")

    followed = _admitted(run_dir, 1)               # `-s speculation_depth=1`, exactly as advised
    assert followed._speculation_depth_auto is False        # spelling it means it is not AUTO
    followed._require_pinned_speculation_receipt(entry)     # must NOT raise
    assert followed._reentry_repin() is False
    assert followed.speculation_depth == 0         # the ratchet is one-way and the log owns it

    # A depth that genuinely disagrees with the LAUNCH pin still fails closed, and names both facts.
    changed = _admitted(run_dir, 3)
    with pytest.raises(SpeculationAuthorizationError,
                       match=r"pinned at run start to 1 \(and settled by this run to 0\), "
                             r"and this process resolved 3"):
        changed._require_pinned_speculation_receipt(entry)


def test_the_two_re_entry_adoption_rules_are_one_rule(tmp_path, caplog):
    """A marker-less log met by a SPELLED depth: accepted, then silently clamped — by a second rule.

    Two adoption rules used to coexist. `_require_pinned_speculation_receipt` adopted the log's depth
    only for AUTO, and its docstring said "An EXPLICITLY spelled depth is never adopted";
    `_reentry_repin` then did `self.speculation_depth = _entry.speculation_depth` unconditionally.
    They agree on every log that carries a speculative marker, because the guard refuses a spelled
    disagreement before the second rule can run. On a log that carries NONE the guard returns early,
    and the second rule silently clamped an operator's explicit `-s speculation_depth=2` to 0.

    The clamp itself is right — turning the treatment ON mid-run would write a speculative prefix into
    a log whose `run_started` carries no receipt authorizing one — so what changes is that it is no
    longer silent. Both boundaries now state the same rule: the log's own treatment wins.
    """
    import logging

    run_dir = tmp_path / "nomarker"
    plain = _admitted(run_dir, 0)               # a run that pinned NO speculative prefix at all
    _log(run_dir, depth=0, evals=[], builds=[], pinned=plain._run_start_pinned_values())
    entry = fold(plain.store.read_all())
    assert (entry.speculation_depth_pinned, entry.speculation_depth) == (0, 0)

    resumed = _admitted(run_dir, 2)             # `-s speculation_depth=2` on the resume command
    assert resumed.speculation_depth == 2 and resumed._speculation_depth_auto is False
    resumed._require_pinned_speculation_receipt(entry)      # marker-less: returns without a word
    assert resumed.speculation_depth == 2
    with caplog.at_level(logging.WARNING, logger="looplab.engine.orchestrator"):
        assert resumed._reentry_repin() is False
    assert resumed.speculation_depth == 0                   # the log still wins...
    assert resumed._speculation_enabled() is False
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "ignoring speculation_depth=2 on re-entry" in text      # ...and now it says so
    assert "run_started pinned 0" in text

    # ...and it says it only where it MEANS it. A depth spelled with Layer 3 off never entered the
    # lane at all, so re-entry is not what is ignoring it — and `_run_start_pinned_values` omits the
    # key in that case, which would otherwise make every FRESH such run warn about its own run_started.
    caplog.clear()
    inert_dir = tmp_path / "layer3-off"
    inert = make_engine(inert_dir, card_driven_selection=False, max_nodes=8, speculation_depth=2)
    assert inert.speculation_depth == 2 and inert._speculation_gate_admitted is False
    inert.store.append("run_started", {
        "run_id": inert_dir.name, "task_id": "toy", "goal": "g", "direction": "min",
        **inert._run_start_pinned_values(), **inert._run_start_settled_widths()})
    with caplog.at_level(logging.WARNING, logger="looplab.engine.orchestrator"):
        inert._reentry_repin()
    assert inert.speculation_depth == 0
    assert not [r for r in caplog.records if "on re-entry" in r.getMessage()]


def test_the_warning_names_advice_that_works(tmp_path, caplog):
    """The printed remedy is a CONTRACT with the operator; drive the real ratchet and read it.

    Two things must hold of the text: it must not tell the operator to re-spell the depth on the run
    it just settled (that is the door the guard used to refuse, and the fold would ignore it anyway),
    and it must name the surface where the choice IS available.
    """
    import logging

    engine = _engine(tmp_path, "warned", auto=True, depth=1,
                     evals=[0.1, 0.1], builds=[120.0, 120.0])
    with caplog.at_level(logging.WARNING, logger="looplab.engine.speculation"):
        assert engine._settle_speculation_depth(_measured(engine)) is True
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "speculation_depth_settled" in text            # where the durable record is
    assert "ONE-WAY" in text and "no resume flag lifts it" in text
    assert "LAUNCH a run with the depth spelled" in text
    assert "keeps it on" not in text                      # the advice that refused


def test_the_ratchet_only_ever_goes_down(tmp_path):
    """A second settle can never re-enable speculation, in the engine or in the fold."""
    engine = _engine(tmp_path, "ratchet", auto=True, evals=[0.1, 0.1], builds=[120.0, 120.0])
    state = _measured(engine)
    assert engine._settle_speculation_depth(state) is True
    # Now the evals look slow (a genuinely long node lands later). The depth must NOT come back.
    engine.speculation_depth = 0
    slow = fold(engine.store.read_all())
    assert engine._settle_speculation_depth(slow) is False
    assert fold(engine.store.read_all()).speculation_depth == 0


def test_the_ratchet_never_fires_while_a_prefetch_is_outstanding(tmp_path):
    """Turning the depth to 0 also turns off the lane that SERVES an open request.

    `_speculation_enabled()` gates `_run_card_session`, `_serve_card_builds` and the whole request
    queue. Settling with a head request open would leave that request holding a physical node
    reservation with nothing left able to close it — the budget leaks and the run stalls, which is the
    same shape as the defect this sits beside. The ratchet is therefore quiescent-only.
    """
    engine = _engine(tmp_path, "quiescent", auto=True, evals=[0.1, 0.1], builds=[120.0, 120.0])
    state = _measured(engine)

    engine.store.append("card_added", {"id": "card-0", "statement": "s", "source": "researcher",
                                       "at_node": 0})
    engine.store.append("card_build_requested", {"card_id": "card-0", "generation": 0})
    with_request = fold(engine.store.read_all())
    assert engine._head_request(with_request) is not None
    assert engine._settle_speculation_depth(with_request) is False
    assert engine.speculation_depth == 1

    # …and an in-flight producer in THIS process blocks it too, even with the queue closed.
    engine.store.append("card_build_done", {"card_id": "card-0", "generation": 0,
                                            "skipped": "producer_failed"})
    closed = fold(engine.store.read_all())
    engine._ensure_speculation_state()
    engine._spec_build_inflight.add(("card-0", 0))
    assert engine._settle_speculation_depth(closed) is False
    engine._spec_build_inflight.discard(("card-0", 0))
    assert engine._settle_speculation_depth(closed) is True
