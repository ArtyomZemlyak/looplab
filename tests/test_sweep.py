"""Intra-node sweep: a single node runs a grid of trials in one process and reports them all on
ONE node_evaluated event. node.metric is the best feasible trial (under the task direction); the
full trial list survives a re-fold unchanged (idempotency). Plus a direct check of the
looplab.sweep helper's contract.
"""
from __future__ import annotations

import io
import os
import random
from contextlib import redirect_stdout
from pathlib import Path

import anyio

from looplab.events.eventstore import EventStore
from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.events.replay import fold
from looplab.runtime.sandbox import RunResult, SubprocessSandbox, _json_line_trials
from looplab.sweep import enumerate_grid, run_sweep
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "toy_task.json"


class _SweepResearcher:
    """Always proposes a one-axis grid (an intra-node sweep)."""
    def propose(self, state, parent):
        return Idea(operator="draft", params={}, space={"x": [1.0, 2.0, 3.0]},
                    rationale="sweep over x")


class _SweepDeveloper:
    """Emits a self-contained script that evaluates the grid and prints the trials line (no
    dependency on looplab being importable inside the sandbox subprocess — the helper's own
    behavior is unit-tested separately)."""
    def implement(self, idea: Idea) -> str:
        xs = list(idea.space.get("x", []))
        return (
            "import json\n"
            f"xs = {xs}\n"
            "trials = [{'params': {'x': x}, 'metric': (x - 2.0) ** 2, 'seconds': 0.01,\n"
            "           'extra_metrics': {}, 'error': ''} for x in xs]\n"
            "print(json.dumps({'trials': trials}))\n"
        )


def _engine(run_dir, direction="min"):
    task = ToyTask.load(TASK_FILE)
    task.direction = direction
    return Engine(
        run_dir,
        task=task,
        researcher=_SweepResearcher(),
        developer=_SweepDeveloper(),
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=1, max_nodes=1),   # exactly one (sweep) node
    )


def test_sweep_node_collapses_to_best_min(tmp_path):
    rd = tmp_path / "run"
    state = anyio.run(_engine(rd, "min").run)
    node = state.nodes[0]
    assert len(node.trials) == 3                      # whole grid reported
    assert node.metric == 0.0                          # best (min) trial = (2-2)^2
    # Exactly ONE node_evaluated event for the whole sweep (atomic eval, one budget charge).
    evs = list(EventStore(rd / "events.jsonl").read_all())
    assert sum(1 for e in evs if e.type == "node_evaluated") == 1
    # Re-fold reproduces the trials identically (idempotency).
    refold = fold(evs)
    assert [t.model_dump() for t in refold.nodes[0].trials] == [t.model_dump() for t in node.trials]
    assert refold.nodes[0].metric == node.metric


def test_sweep_node_best_max(tmp_path):
    state = anyio.run(_engine(tmp_path / "run", "max").run)
    node = state.nodes[0]
    assert node.metric == 1.0                           # best (max) trial = (1-2)^2 == (3-2)^2 == 1
    assert state.best_node_id == 0


def test_helper_grid_order_and_emit():
    # Deterministic cartesian product over SORTED keys.
    assert enumerate_grid({"b": [1, 2], "a": [10]}) == [{"a": 10, "b": 1}, {"a": 10, "b": 2}]
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_sweep({"lr": [0.1, 0.01]}, lambda p, s: p["lr"], direction="max")
    parsed = _json_line_trials(buf.getvalue())
    assert parsed is not None and len(parsed) == 2


def test_helper_seed_honors_env(monkeypatch):
    monkeypatch.setenv("LOOPLAB_EVAL_SEED", "5")
    seen = []
    run_sweep({"x": [0.0, 0.0]}, lambda p, s: seen.append(s) or 0.0, direction="min", emit=False)
    # base=5 -> trial seeds 5*1_000_003 + i
    assert seen == [5 * 1_000_003 + 0, 5 * 1_000_003 + 1]


def test_helper_failed_trial_isolated():
    def tf(p, s):
        if p["x"] == 2:
            raise ValueError("boom")
        return float(p["x"])
    trials = run_sweep({"x": [1, 2, 3]}, tf, direction="min", emit=False)
    metrics = [t["metric"] for t in trials]
    assert metrics == [1.0, None, 3.0]
    assert "boom" in trials[1]["error"]


def test_malformed_trial_items_cannot_crash_the_host_engine(tmp_path):
    eng = _engine(tmp_path / "run", "min")
    res = RunResult(exit_code=0, stdout="", stderr="", metric=999.0, timed_out=False,
                    trials=[1, "bad", {"metric": "junk"},
                            {"metric": 0.5, "extra_metrics": ["not", "a", "mapping"]}])

    eng._apply_sweep_best(res)

    assert res.metric == 0.5
    assert res.extra_metrics is None

    res.trials = [1, "bad", None]
    eng._apply_sweep_best(res)
    assert res.metric is None


def test_clamp_fill_leaves_swept_dims_to_the_grid():
    """Architecture review: _clamp_fill must NOT midpoint-fill a dimension the Idea SWEEPS (present in
    idea.space). Injecting a fixed midpoint made the Developer prompt render 'sweep degree in [1,2,3]'
    AND 'degree=<midpoint>' — telling the model a swept dim is simultaneously fixed."""
    from looplab.agents.roles import _clamp_fill
    from looplab.core.models import Idea
    idea = Idea(operator="improve", params={"lam": 0.1}, space={"degree": [1, 2, 3]})
    out = _clamp_fill(idea, {"degree": (0.0, 6.0), "lam": (0.0, 100.0)})
    assert "degree" not in out.params            # swept dim left to its grid, not fixed at 3.0
    assert out.params["lam"] == 0.1              # a fixed param present is still clamped/kept
    # a NON-swept missing bound is still midpoint-filled (crash-guard preserved)
    idea2 = Idea(operator="improve", params={}, space={"degree": [1, 2]})
    out2 = _clamp_fill(idea2, {"degree": (0.0, 6.0), "lam": (0.0, 10.0)})
    assert "degree" not in out2.params and out2.params["lam"] == 5.0


# The bounds `classification.py` carried at the time of the incident (it has since gained `degree`;
# these three are unchanged, and the properties below are about the clamp, not the task).
_INCIDENT_BOUNDS = {"lr": (0.001, 1.0), "l2": (0.0, 1.0), "iters": (10.0, 500.0)}


def _clamp_fill_population():
    """(label, params, space, bounds) over the whole shape space `_clamp_fill` decides in.

    A structured table FIRST, because the interesting cases are structural rather than statistical:
    a grid entirely above / below / straddling / inside the task bounds, a SINGLE-VALUE grid (which
    `Idea._clamp_params_to_space` deliberately ignores — it needs two points — so it is the one class
    of sweep whose breakage is invisible to a revalidation check and visible only in the Developer
    prompt), a swept key present in params vs absent, and a NON-swept key inside vs outside its
    bounds vs missing entirely. Then 400 seeded random draws over the same axes, so a mutant that
    special-cases the named instances has nowhere to hide. Seeded, so any failure names a case id.
    """
    yield ("live incident /tmp/ll-s1/spec",
           {"lr": 0.5, "l2": 0.001, "iters": 5000.0},
           {"lr": [0.1, 0.5, 1.0], "iters": [1000.0, 5000.0]}, _INCIDENT_BOUNDS)
    # The SECOND independent live reproduction, `/tmp/ll-s4/run` card-0 — a different task run, a
    # different grid, the identical failure. Both battery runs that died "stuck" carry exactly one
    # such Card and no other run does, which is the perfect correlation the report noticed from the
    # other end (`producer_failed` forces the serial claim, and the serial claim can never succeed).
    yield ("live incident /tmp/ll-s4/run",
           {"lr": 0.3, "l2": 0.001, "iters": 2000.0, "degree": 2.0},
           {"lr": [0.05, 0.1, 0.3, 0.5], "l2": [0.0001, 0.001, 0.01, 0.1],
            "iters": [1000.0, 2000.0]}, _INCIDENT_BOUNDS)
    grids = {
        "grid above bounds": [1000.0, 5000.0],
        "grid below bounds": [-8.0, -6.0],
        "grid straddling hi": [400.0, 900.0],
        "grid straddling lo": [-5.0, 60.0],
        "grid inside bounds": [100.0, 200.0],
        "single-value grid inside": [100.0],
        "single-value grid above": [4000.0],
        "single-value grid below": [-40.0],
        "grid spanning the bounds": [-900.0, 900.0],
    }
    for name, grid in grids.items():
        for present, value in (("param present in grid", grid[0]),
                               ("param present off grid", 0.5),
                               ("param absent", None)):
            params = {} if value is None else {"iters": value}
            yield (f"{name} / {present}", params, {"iters": list(grid)}, _INCIDENT_BOUNDS)
    for name, value in (("non-swept above hi", 9_000.0), ("non-swept below lo", -9_000.0),
                        ("non-swept inside", 250.0)):
        yield (name, {"iters": value}, {}, _INCIDENT_BOUNDS)
    yield ("non-swept missing entirely", {}, {}, _INCIDENT_BOUNDS)

    rng = random.Random(20260806)
    keys = ("lr", "l2", "iters", "degree")
    for case in range(400):
        params, space, bounds = {}, {}, {}
        for key in keys:
            if rng.random() < 0.75:
                params[key] = round(rng.uniform(-50.0, 50.0), 4)
            if rng.random() < 0.6:
                # 1-value grids on purpose, and grids that may sit anywhere relative to the bounds.
                size = rng.choice([1, 1, 2, 3, 4])
                start = rng.uniform(-50.0, 50.0)
                step = rng.uniform(0.001, 20.0)
                space[key] = [round(start + step * i, 6) for i in range(size)]
            if rng.random() < 0.8:
                edges = sorted((round(rng.uniform(-50.0, 50.0), 4),
                                round(rng.uniform(-50.0, 50.0), 4)))
                bounds[key] = (edges[0], edges[1])
        yield (f"random case {case}", params, space, bounds)


def test_clamp_fill_pins_the_general_property_not_the_two_live_instances():
    """A bounds clamp on a SWEPT key used to break the Idea's fixed-point property, and that killed runs.

    `_clamp_fill` mutates `idea.params` directly, which bypasses `Idea._clamp_params_to_space`. When
    the Researcher's sweep grid lies outside the task bounds, clamping the same key to the bounds wrote
    a `params` value its own `space` forbids — so rebuilding the Idea from the durable Card re-ran the
    space clamp and produced a DIFFERENT number. Every Card's ownership digest is minted from those
    params and re-derived on claim, so such a Card could never be claimed again: the create lane
    re-selected it, re-refused it, and spun. Measured live (`/tmp/ll-s1/spec`, blob_classification,
    `iters` bound (10, 500)): the Researcher proposed `space.iters=[1000,5000]`, the Card stored
    `params.iters=500`, reconstruction said 1000, and the run burned 74 loop turns in one second before
    dying "stuck: 1 action(s) planned … without creating a node" at 2 of 8 nodes.

    This test used to CLAIM to pin "the general property, not the one instance" and pinned two
    hand-written instances instead. Measured by mutating a throwaway tree: three mutants passed it and
    the test above while breaking what both docstrings say — `swept = {k for k, v in space.items() if
    len(v) > 1}` (a single-value grid stops being a sweep, so it gets midpoint-filled and the Developer
    prompt says "sweep degree in [3.0]" AND "degree=2.0" — the exact contradiction the test above is
    about, 132/400 cases), clamping a swept dim to its own grid instead of leaving it alone (135/400),
    and DELETING the bounds clamp entirely, which the old `out.params["l2"] == 0.001` assertion could
    not see because 0.001 was already inside `(0.0, 1.0)`.

    So state the three properties over the whole shape space instead:
      1. the GRID owns a swept dimension — `_clamp_fill` neither fills nor changes it, whatever the
         grid's size or its relation to the task bounds (this is what the prompt contract needs);
      2. whatever comes back is a FIXED POINT of `Idea`'s validators (this is what the Card claim
         needs — it is the free-spin property);
      3. a bounded NON-swept dimension is still present and inside its bounds (the crash-guard the
         function exists for, which properties 1 and 2 alone would let a mutant delete).
    """
    from looplab.agents.roles import _clamp_fill
    from looplab.core.models import Idea

    for label, params, space, bounds in _clamp_fill_population():
        idea = Idea(operator="draft", params=dict(params), space=dict(space),
                    rationale="clamp population", hypothesis="clamp population")
        # `Idea` already normalizes params into their grid at construction; the property is about what
        # `_clamp_fill` does to that starting point, so compare against the VALIDATED starting point.
        before = dict(idea.params)
        out = _clamp_fill(idea, bounds)

        for key in space:
            # (1) neither filled nor clamped — the grid owns the dimension ENTIRELY.
            assert (key in out.params) == (key in before), f"{label}: swept {key} gained/lost a param"
            if key in before:
                assert out.params[key] == before[key], f"{label}: swept {key} was moved"

        # (2) THE free-spin property: revalidating (what every Card claim does) must move nothing.
        assert Idea.model_validate(out.model_dump()).params == out.params, \
            f"{label}: result is not a fixed point of Idea's validators"

        for key, (lo, hi) in bounds.items():
            # (3) the crash guard, stated where it is not vacuous: a bounded non-swept dimension is
            # always present and always inside its bounds.
            if key in space:
                continue
            assert key in out.params, f"{label}: non-swept {key} was not filled"
            assert lo <= out.params[key] <= hi, f"{label}: non-swept {key} left its bounds"


def test_emit_survives_a_non_json_value_after_every_trial_trained(capsys):
    """The final `json.dumps` runs AFTER all training — a TypeError there destroys the whole sweep.

    `looplab/sweep.py` is the prompted default for LLM-written `train_fn`s, and numpy return values
    are the norm there: `np.float32`/`np.int64` are NOT float/int subclasses, so json refuses them.
    Without `default=`, one such value in any trial's extras (or one numpy grid value in `params`)
    meant the `{"trials": …}` line was never printed, the sandbox found no trials, and a multi-hour
    grid died with an opaque traceback at the last instruction.
    """
    import json

    class _NumpyLike:                      # exposes .item() like every numpy scalar
        def __init__(self, value):
            self._value = value

        def item(self):
            return self._value

    class _Opaque:                         # no .item, no .tolist, not float()-able
        def __repr__(self):
            return "Opaque()"

    def train(params, seed):
        return {"metric": 0.5, "n_correct": _NumpyLike(7), "note": _Opaque()}

    trials = run_sweep({"lr": [0.1, 0.2]}, train, emit=True)
    assert len(trials) == 2

    printed = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(printed)          # must be parseable — this is what the sandbox scans for
    assert len(payload["trials"]) == 2
    # Fidelity order: a numpy-like scalar keeps its exact value; an opaque value is still REPORTED.
    assert payload["trials"][0]["extra_metrics"]["n_correct"] == 7
    assert payload["trials"][0]["extra_metrics"]["note"] == "Opaque()"
