"""I7: ablation-driven refinement — probe each param's impact, refine the top one."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.events.eventstore import EventStore
from factories import make_engine
from looplab.search.policy import GreedyTree

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def _engine(rd, ablate_every):
    return make_engine(rd, policy=GreedyTree(n_seeds=3, max_nodes=12, ablate_every=ablate_every,
                                             enable_merge=False))


def test_ablation_produces_refine_block_and_impacts(tmp_path):
    state = anyio.run(_engine(tmp_path / "run", ablate_every=1).run)
    assert state.finished

    events = list(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    ablate_events = [e for e in events if e.type == "ablate"]
    assert ablate_events, "expected at least one ablation pass"
    # Impacts were measured for both params of the toy objective.
    imp = ablate_events[0].data["impacts"]
    assert set(imp) == {"x", "y"} and all(v >= 0 for v in imp.values())

    # A refine_block node exists and is a single-parent child.
    refines = [n for n in state.nodes.values() if n.operator == "refine_block"]
    assert refines and all(len(n.parent_ids) == 1 for n in refines)


def test_ablation_off_by_default(tmp_path):
    state = anyio.run(_engine(tmp_path / "run", ablate_every=0).run)
    events = list(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    assert not any(e.type == "ablate" for e in events)
    assert not any(n.operator == "refine_block" for n in state.nodes.values())


# --- one refine_block child-construction tail (doc 25 EC-06) ------------------------------------
#
# `_ablate` (numeric params) and `_ablate_code` (pipeline blocks) differ in how they SCORE and how
# they build the Idea. Everything after that was verbatim in both, including three abandon paths
# that each have to do TWO things. These pin the properties a second copy would let drift.

def _tail():
    import ast
    import inspect
    import textwrap

    from looplab.engine.ablation import AblationMixin

    return ast.parse(textwrap.dedent(inspect.getsource(AblationMixin._build_refine_block_child)))


def _mode_sources():
    import inspect
    import textwrap

    from looplab.engine.ablation import AblationMixin

    return {name: textwrap.dedent(inspect.getsource(getattr(AblationMixin, name)))
            for name in ("_ablate", "_ablate_code")}


def test_both_ablation_modes_build_their_child_through_the_one_tail():
    """Neither mode may grow its own reservation → implement → emit sequence back. `_reserve_node_build`
    and `_emit_node_created` are the two ends of that tail; either appearing in a mode body means a
    second copy is back."""
    for name, source in _mode_sources().items():
        assert "_build_refine_block_child(" in source, f"{name} no longer uses the shared tail"
        for spelling in ("_reserve_node_build", "_emit_node_created", "_fail_reserved_build"):
            assert spelling not in source, f"{name} re-grew its own {spelling} call"


def test_every_abandon_path_both_releases_the_reservation_and_drops_telemetry():
    """The tail's three abandon paths — reservation refused, parent superseded mid-build, creation
    rejected on replay — must each drop the developer telemetry, or it leaks onto whichever node is
    created next. Two of them must ALSO fail the reservation they already hold, or the card stays
    stuck building forever. Getting one half of one pair wrong is exactly what a second copy hides."""
    import ast

    tail = _tail()
    fn = tail.body[0]
    returns = [node for node in ast.walk(fn) if isinstance(node, ast.Return)]
    assert len(returns) == 3, f"expected the three abandon paths, found {len(returns)}"

    # Each `return` must be immediately preceded by the telemetry discard in its own block.
    discards = 0
    fails = 0
    for block in ast.walk(fn):
        body = getattr(block, "body", None)
        if not isinstance(body, list):
            continue
        for index, stmt in enumerate(body):
            if not isinstance(stmt, ast.Return):
                continue
            before = ast.dump(ast.Module(body=body[:index], type_ignores=[]))
            assert "_discard_node_build_telemetry" in before, (
                f"the abandon path returning at line {stmt.lineno} does not drop developer telemetry")
            discards += 1
            fails += "_fail_reserved_build" in before
    assert discards == 3 and fails == 2, (discards, fails)


def test_the_probe_wall_clock_is_returned_so_neither_loop_can_drop_it():
    """P1-2: probe seconds are budgeted on the `ablate` event, so a probe whose time is not summed
    spends entirely outside `max_eval_seconds`. Returning it (rather than accumulating inside the
    helper) is what makes a loop that forgets to add it visibly wrong at the call site."""
    import ast
    import inspect
    import textwrap

    from looplab.engine.ablation import AblationMixin

    probe = textwrap.dedent(inspect.getsource(AblationMixin._timed_ablation_probe))
    returns = [node for node in ast.walk(ast.parse(probe)) if isinstance(node, ast.Return)]
    assert len(returns) == 1 and isinstance(returns[0].value, ast.Tuple)
    assert len(returns[0].value.elts) == 3, "expected (result, seconds, parent_current)"

    for name, source in _mode_sources().items():
        if "_timed_ablation_probe" not in source:
            continue
        assert "abl_seconds += seconds" in source, f"{name} drops the probe wall-clock"
        assert "time.monotonic()" not in source, f"{name} re-grew its own probe timing"


# The clock the probe timing is measured against, stepped by a fixed amount per reading. A constant
# (`return (res, 0.0, ...)`) is the whole property deleted while every shape assertion above stays
# green, so the tests below pin the VALUE, not just that a number came back.
PROBE_STEP = 2.5


def _stub_ablation_clock(monkeypatch):
    """`time.monotonic` in `engine.ablation` advancing exactly `PROBE_STEP` per reading, so one probe
    is exactly `PROBE_STEP` seconds and N probes are exactly `N * PROBE_STEP`."""
    import types

    import looplab.engine.ablation as ablation

    ticks = iter(range(10_000))
    monkeypatch.setattr(
        ablation, "time",
        types.SimpleNamespace(monotonic=lambda: 1000.0 + PROBE_STEP * next(ticks)))


def test_the_probe_reports_the_wall_clock_it_actually_measured(tmp_path, monkeypatch):
    """The helper's own contract, against a stubbed clock: the seconds it returns are the elapsed
    time of the probe it just awaited. A constant satisfies the tuple shape and the `>= 0` floor
    both, and deletes the entire budgeting property."""
    from looplab.engine.ablation import AblationMixin

    _stub_ablation_clock(monkeypatch)

    class _Host(AblationMixin):
        async def _run_ablation_probe(self, code, workdir, parent_id, generation):
            return f"result:{code}"

        def _ablation_parent_current(self, parent_id, generation):
            return True

    res, seconds, current = anyio.run(
        lambda: _Host()._timed_ablation_probe("src", tmp_path, 1, 0))
    assert (res, current) == ("result:src", True)
    assert seconds == PROBE_STEP, f"the probe reported {seconds}s for a {PROBE_STEP}s probe"


def test_ablation_event_budgets_the_probe_seconds(tmp_path, monkeypatch):
    """And behaviourally through the real loop: the audit event carries the summed wall-clock of the
    probes it ran, which is the number the fold charges against the eval budget. Under the stubbed
    clock every probe is exactly `PROBE_STEP`, so the event must read a POSITIVE whole multiple of it
    — a floor of zero is a probe pass that spends entirely outside `max_eval_seconds`."""
    _stub_ablation_clock(monkeypatch)
    anyio.run(_engine(tmp_path / "run", ablate_every=1).run)
    events = list(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    scored = [e for e in events if e.type == "ablate" and e.data.get("impacts")]
    assert scored, "expected an ablation pass that actually probed"
    for e in scored:
        secs = e.data.get("eval_seconds")
        assert isinstance(secs, (int, float)) and not isinstance(secs, bool), e.data
        # Every scored impact came from a completed probe, so the pass ran at least that many.
        assert secs >= PROBE_STEP * len(e.data["impacts"]), e.data
        assert secs == PROBE_STEP * round(secs / PROBE_STEP), (
            f"{secs}s is not a whole number of {PROBE_STEP}s probes", e.data)
