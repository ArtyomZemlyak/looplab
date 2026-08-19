"""docs/29 F1b — the Researcher is told the PER-EXPERIMENT GPU budget, in both prompts.

`agents/roles.py::_FOOTPRINT_GUIDANCE` has always asked the Researcher to declare
`footprint: {"gpus": N}` and never told it what N may be. The only channel carrying that was the
operator's goal prose. Measured on `rubertlite-dr-unified-v5`: the goal said "two H200 GPUs are
available", both Cards declared `{"gpus": 2}`, `_resource_request_for_node` honoured the declaration
(declared beats AUTO), and a run whose settled `eval_parallel` was 2 ran SERIALLY at double the
per-node cost — `CUDA_VISIBLE_DEVICES=0,1`, `WORLD_SIZE=2` at the process level.

The fix is a registry hint (`_gpu_budget_hint`), and a hint has two halves that fail in different
ways, so each half is driven separately here:

* `RESEARCHER_PROMPT_CUES` — the SPLICE. Missing, the attribute is set and read by nobody: both
  prompts ask for a footprint with no budget, exactly as before, and nothing is red.
  `test_both_researcher_prompts_carry_the_engine_stamped_ceiling` fails without it.
* `RESEARCHER_HINT_ATTRS` — the DELIVERY. Missing, the hint reaches an UNWRAPPED researcher and dies
  at the first wrapper, so the ceiling silently disappears in the shipped wrapper chains.
  `test_the_ceiling_survives_a_real_wrapper_chain` fails without it (and so does
  `test_hint_forwarding.py`'s registry-driven sweep).

Everything here is offline: fake clients, fake GPU ids, the toy task.
"""
from __future__ import annotations

import anyio
import pytest

import looplab.engine.orchestrator as _orch
from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import (RESEARCHER_HINT_ATTRS, RESEARCHER_PROMPT_CUES, LLMResearcher,
                                  ToyObjectiveDeveloper, ToyResearcher)
from looplab.core.models import Idea, RunState
from looplab.engine.orchestrator import Engine
from looplab.engine.widths import per_experiment_gpu_budget
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.foresight import ForesightPanelResearcher
from looplab.search.policy import GreedyTree


# --------------------------------------------------------------------------- fixtures / fakes
class _ToolEmitClient:
    """`parse_structured(tool_call)` fake: records the first message list, emits a valid Idea."""

    def __init__(self):
        self.messages = None

    def complete_tool(self, messages, json_schema):
        if self.messages is None:
            self.messages = [dict(m) for m in messages]
        return {"operator": "draft", "params": {}, "rationale": "r"}


def _gpu_capable(monkeypatch) -> None:
    """Speak for a GPU-capable task.

    ToyTask declares `gpu_capable() -> False` (closed-form arithmetic), and the budget hint is
    deliberately SILENT there — see `_gpu_budget_hint_text`. Patching the production hook is how
    `tests/test_settled_width_pins.py` already gives the toy task a box the width can follow.
    """
    monkeypatch.setattr(ToyTask, "gpu_capable", lambda self: True)


def _engine(run_dir, **kw) -> Engine:
    task = ToyTask()
    researcher = ToyResearcher(task.bounds, seed=task.seed, step=task.step)
    researcher.client = object()          # mark it LLM-backed, as `_build_calls_an_llm` reads it
    return Engine(run_dir, task=task, researcher=researcher,
                  developer=ToyObjectiveDeveloper(), sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=2, max_nodes=2), n_seeds=2, max_nodes=2, **kw)


def _stamp(engine, researcher) -> str:
    """Drive the engine's real per-proposal stamp and return what the Researcher will read."""
    engine._set_complexity_hint(RunState(goal="g", direction="min"), None, researcher=researcher)
    return getattr(researcher, "_gpu_budget_hint", "<UNSET>")


# --------------------------------------------------------------------------- the rule itself
@pytest.mark.parametrize("pool,width,expected", [
    (4, 2, 2),        # the ordinary case: four devices, two concurrent experiments -> two each
    (2, 2, 1),        # the AUTO shape — one experiment per GPU — is exactly one device each
    (8, 1, 8),        # a serial run may legitimately take the whole box
    (3, 2, 1),        # floor, not a fraction: two experiments of 1 fit, one of 2 would not
    (2, 4, 1),        # oversubscribed by an explicit width: devices EXIST, they queue -> 1, never 0
    (0, 2, 0),        # GPU-less host: 0, NOT max(1, …) — a positive gpus fails admission closed
    (0, 1, 0),
])
def test_the_per_experiment_share_truth_table(pool, width, expected):
    assert per_experiment_gpu_budget(pool, width) == expected


@pytest.mark.parametrize("pool,width", [
    (4, 0),           # 0 is the launch-time AUTO sentinel — reading it here means reading too early
    (4, True), (True, 2),                     # a bool is not a count (bool is an int subclass)
    (4, "2"), (4, 2.0), (4.0, 2), (None, 2), (4, None),
    (-1, 2), (4, -2),
])
def test_an_unsettled_width_or_pool_yields_no_number_at_all(pool, width):
    """None means "say nothing". A plausible wrong ceiling is worse than no ceiling: the whole
    point of the hint is that the Researcher stops guessing."""
    assert per_experiment_gpu_budget(pool, width) is None


# --------------------------------------------------------------------------- a real engine stamps it
@pytest.mark.parametrize("gpus,width,ceiling", [(4, 2, 2), (4, 4, 1), (4, 1, 4), (2, 2, 1)])
def test_a_real_engine_stamps_its_own_settled_ceiling(tmp_path, monkeypatch, gpus, width, ceiling):
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: list(range(gpus)))
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / f"box-{gpus}-{width}", eval_parallel=width)
    assert engine._eval_parallel == width and len(engine._gpu_ids) == gpus
    hint = _stamp(engine, LLMResearcher(_ToolEmitClient()))
    assert f"`footprint.gpus = {ceiling}`" in hint
    assert f"up to {width} experiment(s)" in hint and f"pool of {gpus} GPU(s)" in hint
    # The MEANING of the share, which is the half the old `_FOOTPRINT_GUIDANCE` wording got
    # backwards ("`gpus=1` only when the experiment specifically needs one GPU") — and which this
    # cue then got backwards in the OTHER direction until `Settings.gpu_footprint_cue`: it said a
    # larger count "does NOT get this experiment more hardware", which `_acquire_gpus` contradicts.
    # The legacy sentence is now the `false` branch and is pinned in
    # `tests/test_gpu_footprint_choice.py`; what the SHIPPED default owes the role is the trade.
    assert f"`footprint.gpus = {ceiling}` is the ORDINARY declaration" in hint
    assert "a LARGER count is HONOURED" in hint
    assert "does NOT get this experiment more hardware" not in hint


def test_a_cpu_locked_task_is_told_nothing(tmp_path, monkeypatch):
    """`gpu_capable() -> False` (toy/regression/classification/timeseries/offline MLE-bench): an
    undeclared footprint is already a zero-device request that never takes the pool-wide host lease,
    so there is no ceiling to state. Naming GPU counts to a role whose adapter says its code cannot
    touch one is the prompt-side twin of inferring the WORK's needs from the BOX."""
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1, 2, 3])
    engine = _engine(tmp_path / "cpu-locked", eval_parallel=2)   # ToyTask: gpu_capable() is False
    assert engine._task_gpu_capable() is False
    assert _stamp(engine, LLMResearcher(_ToolEmitClient())) == ""


def test_a_gpu_capable_task_on_a_gpu_less_host_is_told_admission_fails(tmp_path, monkeypatch):
    """pool == 0 is the one place `max(1, pool // width)` is actively wrong. A positive `gpus` on a
    GPU-less host is `required_unavailable` in `_resource_request_for_node` — it fails admission
    closed rather than queueing — so "you may have one device" would produce exactly the declaration
    that cannot be served."""
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "no-gpu")
    hint = _stamp(engine, LLMResearcher(_ToolEmitClient()))
    assert "exposes NO GPU" in hint and '`footprint: {"gpus": 0}`' in hint
    assert "REFUSED admission" in hint
    assert "footprint.gpus = " not in hint          # never a device count on a device-less box


# --------------------------------------------------------------------------- both prompts carry it
def test_both_researcher_prompts_carry_the_engine_stamped_ceiling(tmp_path, monkeypatch):
    """The whole point of the registry: the agentic path and the plain path ask the model the SAME
    question. Both prompts are built for real — no source pin — from a hint a real Engine stamped.
    """
    from looplab.agents import agent as agent_mod
    from looplab.agents.agent import ToolUsingResearcher

    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1, 2, 3])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "both", eval_parallel=2)         # 4 GPUs / width 2 -> ceiling 2
    state = RunState(goal="g", direction="min")

    plain_client = _ToolEmitClient()
    plain = LLMResearcher(plain_client)
    stamped = _stamp(engine, plain)
    plain.propose(state, None)
    plain_turn = "".join(m["content"] for m in plain_client.messages)

    seen: dict = {}

    def _fake_run_phase(client, tools, messages, emit_spec, **kw):
        seen["messages"] = [dict(m) for m in messages]
        return Idea(operator="draft", params={}, rationale="ok")

    monkeypatch.setattr(agent_mod, "run_phase", _fake_run_phase)
    agentic = ToolUsingResearcher(client=object(), tools=None)
    _stamp(engine, agentic)
    agentic.propose(state, None)
    agentic_turn = "".join(m["content"] for m in seen["messages"])

    for turn in (plain_turn, agentic_turn):
        assert "GPU BUDGET" in turn
        assert "`footprint.gpus = 2`" in turn
        # The cue is spliced VERBATIM — the two prompts carry the same bytes, not two paraphrases.
        assert stamped in turn
    # And the code-owned system suffix now says what a stated share MEANS, in both variants — the
    # SAME sentence in both, which is this test's whole subject. Under the shipped
    # `gpu_footprint_cue` that is the corrected clause: `_stamp_gpu_budget_hint` (the call `_stamp`
    # above makes) puts the boolean on whichever role is proposing, so a role the engine stamped
    # asks the corrected question on both paths. The legacy wording and the byte-for-byte off path
    # are pinned in `tests/test_gpu_footprint_choice.py`.
    for turn in (plain_turn, agentic_turn):
        assert "the count it names is the ORDINARY per-experiment share" in turn
        assert "a LARGER count IS honoured" in turn
        assert "the count it names is a per-experiment CEILING" not in turn


def test_the_ceiling_survives_a_real_wrapper_chain(tmp_path, monkeypatch):
    """The DELIVERY half. The engine stamps hints on the OUTERMOST active researcher, which in the
    shipped config is a wrapper; only `RESEARCHER_HINT_ATTRS` members are mirrored onto the delegate.
    A ceiling missing from that registry reaches the wrapper and dies there, and the inner prompt —
    the one that actually goes to the provider — is byte-identical to the pre-fix prompt."""
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1, 2, 3])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "wrapped", eval_parallel=2)

    inner_client = _ToolEmitClient()
    panel = ForesightPanelResearcher(LLMResearcher(inner_client), k=1)
    _stamp(engine, panel)                                   # engine-style: on the OUTERMOST object
    panel.propose(RunState(goal="g", direction="min"), None)

    assert "`footprint.gpus = 2`" in "".join(m["content"] for m in inner_client.messages)
    assert "_gpu_budget_hint" in RESEARCHER_HINT_ATTRS and "_gpu_budget_hint" in RESEARCHER_PROMPT_CUES


# --------------------------------------------------------------------------- resume (invariant #6)
def test_a_resume_states_the_ceiling_off_the_runs_own_pinned_width(tmp_path, monkeypatch):
    """The width in the ceiling is the run's OWN, not one re-derived from the box it resumed onto.

    `run_started` pins the settled integers (invariant #6) and `_repin_settled_widths` restores them
    at re-entry — which is AFTER `Engine.__init__` has already resolved launch AUTO off the live
    hardware. So the obvious home for this computation, `__init__` beside the width settling itself,
    is exactly the wrong one: it would capture the pre-re-pin value. This drives the difference
    rather than pinning where the code lives.
    """
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1, 2, 3])
    _gpu_capable(monkeypatch)
    run_dir = tmp_path / "resumed"
    first = _engine(run_dir, eval_parallel=2)                # explicit 2 on a 4-GPU box
    anyio.run(first.run)
    assert "`footprint.gpus = 2`" in _stamp(first, LLMResearcher(_ToolEmitClient()))

    entry = fold(first.store.read_all())          # the run's OWN log, folded
    assert entry.eval_parallel == 2, "the run pinned its own settled width"

    # Re-enter on a BIGGER box, spelled AUTO — the documented "0 = AUTO adopts the pin" path.
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: list(range(8)))
    second = _engine(run_dir, eval_parallel=0)
    assert second._eval_parallel == 8, "startup AUTO resolved off the NEW box, as it always has"
    # What a construction-time computation would have said: 8 GPUs / a width of 8 -> one each.
    assert "`footprint.gpus = 1`" in _stamp(second, LLMResearcher(_ToolEmitClient()))

    second._repin_settled_widths(entry)
    assert second._eval_parallel == 2, "the log outranks the box (invariant #6)"
    # The run's own width, against the pool the resumed box can actually serve: 8 / 2 = 4.
    resumed_hint = _stamp(second, LLMResearcher(_ToolEmitClient()))
    assert "`footprint.gpus = 4`" in resumed_hint
    assert "up to 2 experiment(s)" in resumed_hint and "pool of 8 GPU(s)" in resumed_hint


def test_a_budget_extend_retune_moves_the_ceiling_with_it(tmp_path, monkeypatch):
    """`budget_extend` is the durable way to change a width mid-run, and `_apply_control_overrides`
    re-applies it every turn. The ceiling is read per-proposal for exactly this reason: a stamp taken
    once would keep sizing Cards against a width nobody is running at any more."""
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: list(range(8)))
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / "retuned", eval_parallel=2)
    researcher = LLMResearcher(_ToolEmitClient())
    assert "`footprint.gpus = 4`" in _stamp(engine, researcher)
    engine._eval_parallel = 8                      # what a `budget_extend` control settles to
    assert "`footprint.gpus = 1`" in _stamp(engine, researcher)
