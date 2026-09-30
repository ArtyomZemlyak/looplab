"""The Strategist's brief names the GPU pool it is choosing a width on (doc 69 §6.1, 69.23).

`minionerec-backbones-v10` (2026-09-24 17:17): the Strategist set `eval_parallel=2` "without
oversubscribing 192 CPU-only cores" on four H200s. Its brief carried no pool, no per-experiment GPU
budget, nothing the queued work had declared and nothing about the operator's width — the 4-GPU
experiments ran on one card each (a torchrun port conflict, ~2.4 h with no metric), and after the
operator pinned the width it asked to widen four more times. (The "0 GPUs" in its system prompt was
the failed-probe cache 69.23a fixed.) Under `Settings.strategist_gpu_brief` every consult's brief
states the pool the engine schedules on, what ONE experiment may claim at each width, what the open
proposals declare and whether the width is the operator's; OFF, the brief is byte for byte what it
was.
"""
from __future__ import annotations

from types import SimpleNamespace

from looplab.agents.strategist import StrategyContext, _gpu_pool_note, _strategist_brief
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings
from looplab.core.models import RunState
from looplab.engine.options import EngineOptions
from tests.factories import make_engine


def _ctx(**kw) -> StrategyContext:
    return StrategyContext(eval_parallel=2, **kw)


# ------------------------------------------------------------------------------------ the line
def test_the_line_states_the_pool_the_budget_per_width_the_queue_and_the_pin():
    line = _gpu_pool_note(_ctx(gpu_pool=4, gpu_budget_by_width={1: 4, 2: 2, 4: 1},
                               open_proposals=3, widest_declared_gpus=4, undeclared_proposals=1,
                               eval_parallel_operator_owned=True))
    assert line == ("GPU POOL: 4 device(s); the most GPUs ONE experiment may claim at "
                    "eval_parallel = 1 -> 4, 2 -> 2, 4 -> 1 (now 2); 3 open proposal(s): the widest "
                    "declares 4 GPU(s), 1 declare none; eval_parallel was set by the operator, so a "
                    "width you choose is not applied.\n")


def test_each_clause_speaks_only_when_it_has_something_to_say():
    assert _gpu_pool_note(_ctx()) == "", "no pool sent: say nothing"
    assert _gpu_pool_note(SimpleNamespace(gpu_pool=True)) == "", "a bool is not a pool"
    assert _gpu_pool_note(_ctx(gpu_pool=3)) == "GPU POOL: 3 device(s).\n", "no table, no clause"
    bare = _gpu_pool_note(_ctx(gpu_pool=2, gpu_budget_by_width={1: 2, 2: 1}))
    assert "open proposal" not in bare and "operator" not in bare
    assert _gpu_pool_note(_ctx(gpu_pool=0)).startswith("GPU POOL: 0 devices detected")
    undeclared = _gpu_pool_note(_ctx(gpu_pool=2, gpu_budget_by_width={1: 2, 2: 1},
                                     open_proposals=2, undeclared_proposals=2))
    assert "2 open proposal(s): none declares a GPU count." in undeclared


def test_off_the_brief_is_byte_for_byte_the_historical_one():
    """MUTATION: render the line unconditionally -> the default context gains text."""
    state = RunState()
    assert _strategist_brief(state, _ctx()) == _strategist_brief(state, _ctx(open_proposals=5))
    on = _strategist_brief(state, _ctx(gpu_pool=4, gpu_budget_by_width={1: 4, 2: 2, 4: 1}))
    assert "GPU POOL: 4 device(s)" in on
    assert on.replace(_gpu_pool_note(_ctx(gpu_pool=4, gpu_budget_by_width={1: 4, 2: 2, 4: 1})),
                      "") == _strategist_brief(state, _ctx())


# ------------------------------------------------------------------------------------ the engine
def _engine(tmp_path, *, on: bool, gpus=(0, 1, 2, 3), width=2):
    engine = make_engine(tmp_path / "run", strategist_gpu_brief=on)
    engine._gpu_ids = list(gpus)
    engine._eval_parallel = width
    return engine


def test_the_engine_hands_the_facts_it_schedules_by_and_nothing_when_off(tmp_path):
    """MUTATIONS: drop the switch; read a fresh probe instead of `_gpu_ids`; drop the pin."""
    engine = _engine(tmp_path / "on", on=True)
    state = RunState()
    ctx = engine._strategy_ctx(state)
    assert (ctx.gpu_pool, ctx.gpu_budget_by_width) == (4, {1: 4, 2: 2, 4: 1})
    assert (ctx.open_proposals, ctx.eval_parallel_operator_owned) == (0, False)
    engine._operator_width_axes = frozenset({"eval_parallel"})
    assert engine._strategy_ctx(state).eval_parallel_operator_owned is True
    off = _engine(tmp_path / "off", on=False)
    ctx = off._strategy_ctx(state)
    assert ctx.gpu_pool is None and ctx.gpu_budget_by_width == {}


def test_the_widths_shown_are_the_ones_that_change_the_answer(tmp_path):
    """1, 2, the current width and one per device, never past the pool or the current width."""
    wide = _engine(tmp_path / "wide", on=True, gpus=range(8), width=3)
    assert wide._strategy_ctx(RunState()).gpu_budget_by_width == {1: 8, 2: 4, 3: 2, 8: 1}
    one = _engine(tmp_path / "one", on=True, gpus=(0,), width=1)
    assert one._strategy_ctx(RunState()).gpu_budget_by_width == {1: 1}
    none = _engine(tmp_path / "none", on=True, gpus=(), width=1)
    ctx = none._strategy_ctx(RunState())
    assert ctx.gpu_pool == 0 and ctx.gpu_budget_by_width == {1: 0}


def test_the_open_proposals_are_the_width_settler_s_own_population(tmp_path, monkeypatch):
    engine = _engine(tmp_path, on=True)
    monkeypatch.setattr(engine, "_proposal_footprints", lambda state: [4, None, 2, None])
    ctx = engine._strategy_ctx(RunState())
    assert (ctx.open_proposals, ctx.widest_declared_gpus, ctx.undeclared_proposals) == (4, 4, 2)


# ------------------------------------------------------------------------------------ the switch
def test_on_for_new_runs_off_for_a_pre_field_snapshot_and_off_at_every_constructor(tmp_path):
    assert Settings().strategist_gpu_brief is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["strategist_gpu_brief"] is False
    assert EngineOptions().strategist_gpu_brief is False
    assert make_engine(tmp_path / "bare")._strategist_gpu_brief is False


def test_the_one_reader_defaults_off_for_an_object_that_never_ran_init():
    from looplab.engine.shared import strategist_gpu_brief
    assert strategist_gpu_brief(SimpleNamespace()) is False
    assert strategist_gpu_brief(SimpleNamespace(_strategist_gpu_brief=True)) is True


class _Capture:
    """A client that records what the Strategist sends and answers nothing usable, so the decision
    falls back to the rule — the PROMPT is the subject here, not the answer."""

    def __init__(self):
        self.sent: list = []

    def complete_tool(self, messages, schema=None, **_kw):
        self.sent.append(messages)
        raise RuntimeError("no answer")

    def complete(self, messages, **_kw):
        self.sent.append(messages)
        raise RuntimeError("no answer")

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.sent.append(messages)
        raise RuntimeError("no answer")


def test_the_consult_s_own_prompt_carries_the_line_only_when_on(tmp_path):
    """Driven: the engine builds the context and the LLM Strategist sends its brief. MUTATIONS: drop
    the note from `_strategist_brief`; stop spreading `_gpu_pool_ctx` into the context."""
    from looplab.agents.strategist import LLMStrategist

    for on in (True, False):
        engine = _engine(tmp_path / str(on), on=on)
        client = _Capture()
        state = RunState()
        LLMStrategist(client).decide(state, engine._strategy_ctx(state))
        user = [m["content"] for msgs in client.sent for m in msgs if m["role"] == "user"]
        assert user, "the Strategist sent nothing"
        assert ("GPU POOL: 4 device(s)" in user[0]) is on, user[0][:400]
