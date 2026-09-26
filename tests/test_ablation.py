"""I7: ablation-driven refinement — probe each param's impact, refine the top one."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

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
        # The probe's eval resource is reserved and released around the clock (ENG2-10); both are
        # stubbed so the clock still brackets exactly one probe.
        async def _reserve_ablation_probe(self, parent_id, generation):
            return {"gpu_ids": []}

        def _release_gpus(self, gpu_ids):
            pass

        async def _run_ablation_probe(self, code, workdir, parent_id, generation, *, reservation):
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


# --- a probe is an eval launch like any other (review 2026-09-22, ENG2-10) ----------------------
#
# `_run_ablation_probe` called `self.sandbox.run(code, workdir, timeout, cancel=...)` with NO env,
# so every ablation probe ran without the read fence, the kernel rungs, the GPU pin and the
# declared `eval_env` — and, since the fence's write rule is what keeps a candidate out of the run
# RECORD, a probe could append to `events.jsonl` two directories above its workdir. These drive the
# probe through `_timed_ablation_probe`, the one seam both ablation loops call, and through
# `_ablate` itself.

_DECLARED = {"ABLATION_DECLARED": "yes"}


def _probe_source() -> str:
    from looplab.runtime.read_fence import FENCE_DIR_ENV

    return ("import json, os\n"
            f"print('FENCE=' + str(bool(os.environ.get({FENCE_DIR_ENV!r}))))\n"
            "print('DECLARED=' + os.environ.get('ABLATION_DECLARED', ''))\n"
            "try:\n"
            "    open(os.path.join('..', '..', 'events.jsonl'), 'a').write('{\"forged\": 1}\\n')\n"
            "    print('RECORD=written')\n"
            "except BaseException as exc:\n"
            "    print('RECORD=refused ' + type(exc).__name__)\n"
            "print(json.dumps({'metric': 1.0}))\n")


def _probe_engine(rd, **overrides):
    """A toy engine with a declared run-level `eval_env` and one parent: two numeric params and two
    code blocks, so either ablation mode has MORE than one probe to run (which is what lets a test
    see a pass that should have stopped at its first abstention keep going)."""
    eng = make_engine(rd, eval_env=dict(_DECLARED), **overrides)
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 2.0}},
        "code": "x = 1\n\nprint(x)\n"})
    return eng


def test_an_ablation_probe_runs_fenced_with_the_declared_env_and_cannot_write_the_record(tmp_path):
    """A real subprocess: the fence marker and the declared env reach the probe, and the write to
    the run record two directories up is REFUSED rather than landing in `events.jsonl`."""
    rd = tmp_path / "run"
    eng = _probe_engine(rd)
    wd = rd / "ablate" / "probe"
    wd.mkdir(parents=True)
    res, seconds, current = anyio.run(
        lambda: eng._timed_ablation_probe(_probe_source(), wd, 0, 0))
    assert current is True and res is not None and res.exit_code == 0, (res and res.stderr)
    lines = res.stdout.splitlines()
    assert "FENCE=True" in lines, res.stdout
    assert "DECLARED=yes" in lines, res.stdout
    assert any(line.startswith("RECORD=refused") for line in lines), res.stdout
    assert "forged" not in (rd / "events.jsonl").read_text(encoding="utf-8")
    assert seconds > 0.0


class _RecordingSandbox:
    def __init__(self, calls):
        self.calls = calls

    def run(self, code, workdir, timeout=30.0, env=None, cancel=None):
        from looplab.runtime.sandbox import RunResult

        self.calls.append(("run", dict(env or {})))
        return RunResult(exit_code=0, stdout="", stderr="", metric=1.0, timed_out=False)


def _pinned_host(eng, calls, *, grant=True):
    """One GPU, physical id "7"; the reservation and release are RECORDED, as in
    `tests/test_confirm_integration.py`'s pool test."""
    eng._gpu_ids = [0]
    eng._gpu_physical_ids = {0: "7"}
    eng._gpu_mem = {0: 16_000}
    eng._free_gpus = [0]

    async def reserve(nd, *, resource_pin=None, wait_once=False):
        calls.append(("reserve", nd.id))
        if not grant:
            return None
        request = eng._resource_request_for_node(nd, resource_pin=resource_pin)
        return {**request, "count": 1, "gpu_ids": [0]}

    eng._wait_reserve_node_resources = reserve
    eng._release_gpus = lambda ids: calls.append(("release", list(ids or [])))


def test_an_ablation_probe_holds_a_pinned_reservation_released_once_and_the_wait_is_not_charged(
        tmp_path, monkeypatch):
    """The GPU pin and the reservation's lifetime, like `noise_floor.py::_run_noise_seed`: reserve
    for the PARENT, launch pinned to that device with the declared env on top, release exactly once
    — and start the probe clock only AFTER the reservation, because the `ablate` event's seconds
    are charged against `max_eval_seconds` and a wait for a device is not evaluation."""
    import types

    import looplab.engine.ablation as ablation

    calls: list = []
    eng = _probe_engine(tmp_path / "run", sandbox=_RecordingSandbox(calls))
    _pinned_host(eng, calls)
    ticks = iter(range(10_000))

    def _clock():
        calls.append(("clock",))
        return 1000.0 + PROBE_STEP * next(ticks)

    monkeypatch.setattr(ablation, "time", types.SimpleNamespace(monotonic=_clock))
    res, seconds, current = anyio.run(
        lambda: eng._timed_ablation_probe("print(1)\n", tmp_path / "wd", 0, 0))
    assert res is not None and res.metric == 1.0 and current is True
    assert seconds == PROBE_STEP
    kinds = [c[0] for c in calls]
    assert kinds == ["reserve", "clock", "run", "clock", "release"], kinds
    env = calls[2][1]
    assert env.get("CUDA_VISIBLE_DEVICES") == "7"
    assert env.get("ABLATION_DECLARED") == "yes"
    assert calls[-1] == ("release", [0])


@pytest.mark.parametrize("code_blocks", [False, True])
def test_a_probe_that_never_got_its_resource_did_not_run_and_is_not_an_essential_block(
        tmp_path, monkeypatch, code_blocks):
    """The bounded wait ABSTAINS. A probe that never launched says nothing about its parameter or
    its block: code-block ablation reads a probe with no metric as "removing this block broke the
    run", so an abstention recorded that way would elect a block nobody measured. The pass stops at
    the first abstention (every later probe would wait out the same bound), charges no seconds for
    the wait, and never starts the sandbox."""
    import looplab.engine.ablation as ablation

    calls: list = []
    eng = _probe_engine(tmp_path / "run", sandbox=_RecordingSandbox(calls))
    eng._ablate_code_blocks = code_blocks
    eng.store.append("node_evaluated", {
        "node_id": 0, "generation": 0, "metric": 1.0, "stdout_tail": "", "eval_seconds": 0.1})
    _pinned_host(eng, calls, grant=False)
    monkeypatch.setattr(ablation, "_ABLATION_RESOURCE_TICKS", 3)
    anyio.run(eng._ablate, 0)
    assert not any(c[0] == "run" for c in calls), "a probe launched without its resource"
    assert [c[0] for c in calls] == ["reserve"] * 3, calls
    events = [e for e in eng.store.read_all() if e.type == "ablate"]
    assert len(events) == 1
    assert events[0].data["impacts"] == {}, events[0].data
    assert events[0].data["eval_seconds"] == 0.0


def test_the_signed_gain_keeps_the_direction_the_sensitivity_drops():
    """Doc 67 67.4: `impacts` is `|Δ|`, so a component the run is BETTER without reads exactly like
    one it cannot do without. The signed gain is positive when the probe measured the objective
    better with the component removed, on either objective direction."""
    from looplab.engine.ablation import _signed_gain

    assert _signed_gain(0.9, 0.8, "max") == pytest.approx(0.1)       # better without it
    assert _signed_gain(0.7, 0.8, "max") == pytest.approx(-0.1)      # it was needed
    assert _signed_gain(0.7, 0.8, "min") == pytest.approx(0.1)       # lower loss without it
    assert _signed_gain(0.9, 0.8, "min") == pytest.approx(-0.1)


def test_every_measured_probe_records_its_signed_gain_beside_the_sensitivity(tmp_path):
    """Driven over the toy objective `(x-3)^2 + (y+1)^2`, MINIMIZED: a probe zeroes one parameter,
    so its measured objective is known in closed form from the parent's params, and the recorded sign
    must say whether the run did better (positive) or worse without that parameter."""
    state = anyio.run(_engine(tmp_path / "run", ablate_every=1).run)
    assert state.direction == "min"
    rows = [e.data for e in EventStore(tmp_path / "run" / "events.jsonl").read_all()
            if e.type == "ablate" and e.data.get("impacts")]
    assert rows, "expected at least one measured ablation pass"
    signs = set()
    for data in rows:
        parent = state.nodes[data["parent_id"]]
        params = {"x": 0.0, "y": 0.0, **parent.idea.params}
        signed = data["signed_impacts"]
        assert set(signed) == set(data["impacts"])
        for name, value in signed.items():
            probe = {**params, name: 0.0}
            probe_metric = (probe["x"] - 3.0) ** 2 + (probe["y"] + 1.0) ** 2
            assert value == pytest.approx(parent.metric - probe_metric), (name, data)
            assert abs(value) == pytest.approx(data["impacts"][name])
            signs.add(value > 0)
    assert False in signs, "a parameter the toy optimum needs must read as NEEDED (negative)"
