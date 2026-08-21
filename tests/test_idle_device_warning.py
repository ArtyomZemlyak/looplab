"""A SMALLER FOOTPRINT BUYS CONCURRENCY ONLY ON A RUN WHOSE WIDTH CAN SPEND IT.

WHAT THIS COST (`runs/e5small-dr-unified-v4`, measured 2026-08-21)
------------------------------------------------------------------
Pool 2, width 1. `per_experiment_gpu_budget(2, 1)` is **2**, so the engine's own GPU BUDGET
paragraph told the Researcher that `footprint.gpus = 2` was the ordinary declaration. Hand-written
goal prose in the SAME message said the opposite: *"declare {"gpus": 1} and two experiments run
concurrently."*

Four nodes in a row declared `{"gpus": 1}`. The second card sat idle for the entire run — roughly
**fifteen GPU-hours** — and in 2.93 h after a restart the engine requested ZERO card builds with
FIVE cards sitting `proposed`, because there was one eval slot and it was busy.

The engine was right and said so. The prose was wrong and won, because **nothing anywhere stated
what a SMALLER declaration does not buy.** A reader supplies the obvious inference — fewer devices
for me means more experiments at once — and it is true only when the width can spend them.

THE SAME INVARIANT BROKE MIRRORED ONE RUN EARLIER, and `engine/widths.py::per_experiment_gpu_budget`
already records it: on `rubertlite-dr-unified-v5` the goal said "two H200 GPUs are available", both
Cards declared `{"gpus": 2}`, and a run with `eval_parallel: 2` went serial at double the per-node
cost. Width 2 with footprints of 2; width 1 with footprints of 1. Both leave the box half-used, and
neither was announced.

So the cue now says it, and only when there is something to say.
"""
from __future__ import annotations

import pytest

from looplab.core.models import RunState
from looplab.engine import orchestrator as _orch
from looplab.engine.widths import per_experiment_gpu_budget

from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import ToyObjectiveDeveloper, ToyResearcher
from looplab.engine.orchestrator import Engine
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree


def _gpu_capable(monkeypatch) -> None:
    """ToyTask declares itself CPU-locked; the budget cue is deliberately silent there."""
    monkeypatch.setattr(ToyTask, "gpu_capable", lambda self: True)


def _engine_with(tmp_path, monkeypatch, *, pool, width, name):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: list(range(pool)))
    _gpu_capable(monkeypatch)
    task = ToyTask()
    researcher = ToyResearcher(task.bounds, seed=task.seed, step=task.step)
    researcher.client = object()
    eng = Engine(tmp_path / name, task=task, researcher=researcher,
                 developer=ToyObjectiveDeveloper(), sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=2, max_nodes=2), n_seeds=2, max_nodes=2,
                 eval_parallel=width, gpu_footprint_cue=True)
    eng._gpu_mem = {i: 143771 for i in range(pool)}
    eng._set_complexity_hint(RunState(goal="g", direction="min"), None, researcher=researcher)
    return getattr(researcher, "_gpu_budget_hint", "") or ""


# ------------------------------------------------------------------ the case that cost the hours
def test_the_live_shape_is_warned_about(tmp_path, monkeypatch):
    """Pool 2, width 1 — v4 exactly. The budget is 2, so a one-device declaration wastes a card."""
    assert per_experiment_gpu_budget(2, 1) == 2
    text = _engine_with(tmp_path, monkeypatch, pool=2, width=1, name="v4shape")
    assert "GPU BUDGET" in text
    assert "DOES NOT BUY CONCURRENCY HERE" in text
    assert "width is fixed at 1 experiment(s) at a time" in text
    assert "leaves the other 1 idle" in text


def test_it_says_which_paragraph_wins_when_prose_disagrees():
    """The specific failure was a hand-written sentence in the same prompt contradicting the
    computed one, and the agent believing the prose. The cue now names itself as the authority —
    which is a claim it can actually make, because it is derived from the width the run launched
    with rather than typed by somebody."""
    import inspect

    from looplab.engine import proposal_cues

    src = inspect.getsource(proposal_cues)
    # It must NOT simply claim to outrank the task statement — the paragraph already ends by
    # deferring to it ("a count the operator's own task statement names wins over this paragraph"),
    # and that deference is right: the count is the operator's CHOICE. What is not a choice is what
    # the count buys in CONCURRENCY, which is arithmetic over the width the run launched with. Two
    # contradicting sentences in one paragraph would be worse than the one wrong sentence this
    # exists to answer, so the split is asserted rather than the override.
    assert "wins on the count it names" in src.lower()
    assert "arithmetic over the width this run launched with, not a preference" in src
    assert "take the count and not the reason" in src


# ------------------------------------------------------------------ and only when there is something to say
def test_a_fully_spent_width_says_nothing_new(tmp_path, monkeypatch):
    """Pool 2, width 2 -> budget 1: a one-device declaration IS the ordinary one and wastes nothing.
    `off == today` for this shape, which is the common one."""
    assert per_experiment_gpu_budget(2, 2) == 1
    text = _engine_with(tmp_path, monkeypatch, pool=2, width=2, name="spent")
    assert "GPU BUDGET" in text
    assert "DOES NOT BUY CONCURRENCY" not in text


def test_a_single_device_box_says_nothing_new(tmp_path, monkeypatch):
    """Budget 1 on a one-GPU host: there is no smaller declaration and nothing to warn about."""
    text = _engine_with(tmp_path, monkeypatch, pool=1, width=1, name="single")
    assert "DOES NOT BUY CONCURRENCY" not in text


def test_the_warning_scales_with_the_pool(tmp_path, monkeypatch):
    """Eight devices at width 1: a one-device declaration idles SEVEN. The number is derived, not
    a fixed sentence about two cards."""
    assert per_experiment_gpu_budget(8, 1) == 8
    text = _engine_with(tmp_path, monkeypatch, pool=8, width=1, name="eight")
    assert "leaves the other 7 idle" in text
    assert "DECLARING FEWER THAN 8" in text
