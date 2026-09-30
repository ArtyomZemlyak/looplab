"""The Strategist's brief names the GPU pool it is choosing a width on (doc 69 §6.1, 69.23).

`minionerec-backbones-v10` (2026-09-24 17:17): the Strategist set `eval_parallel=2` "without
oversubscribing 192 CPU-only cores" on four H200s. Its brief carried no pool, no per-experiment GPU
budget, nothing the queued work had declared and nothing about the operator's width — the 4-GPU
experiments ran on one card each (a torchrun port conflict, ~2.4 h with no metric), and after the
operator pinned the width it asked to widen four more times. (The "0 GPUs" in its system prompt was
the failed-probe cache 69.23a fixed.) Under `Settings.strategist_gpu_brief` every consult's brief
states the pool the engine schedules on, what admission grants an experiment, the most each may
declare for every experiment at a width to run at once, what the open proposals declare and whether
the width is the operator's; OFF, the brief is byte for byte what it was.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from looplab.agents.strategist import StrategyContext, _gpu_pool_note, _strategist_brief
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings
from looplab.core.models import RunState
from looplab.engine.options import EngineOptions
from tests.factories import make_engine


def _ctx(**kw) -> StrategyContext:
    return StrategyContext(eval_parallel=2, **kw)


# ------------------------------------------------------------------------------------ the line
def test_the_line_states_the_grant_the_concurrent_declaration_the_queue_and_the_pin():
    """The critic (2026-09-30, HIGH): `pool // width` is NOT what admission grants — an undeclared
    experiment gets 1 device at width > 1 and a declared one min(k, pool) — so the line states the
    grant and offers `pool // width` only as the declaration that keeps every experiment running."""
    line = _gpu_pool_note(_ctx(gpu_pool=4, gpu_budget_by_width={1: 4, 2: 2, 4: 1},
                               open_proposals=3, widest_declared_gpus=4, undeclared_proposals=1,
                               waiting_nodes=2, widest_waiting_gpus=4, undeclared_waiting=1,
                               eval_parallel_operator_owned=True))
    assert line == ("GPU POOL: 4 device(s) — admission grants an experiment that declares no GPU "
                    "count 1 device at eval_parallel > 1 (the whole box, unpinned, at 1) and one "
                    "that declares k GPUs min(k, 4), which then waits until they are free; for "
                    "every experiment to run at once each may declare at most: eval_parallel "
                    "1 -> 4, 2 -> 2, 4 -> 1 (now 2); 3 open proposal(s): the widest declares 4 "
                    "GPU(s), 1 declare none; 2 built node(s) waiting to run: the widest declares 4 "
                    "GPU(s), 1 declare none; eval_parallel was set by the operator, so a width "
                    "you choose is not applied.\n")
    assert "the most GPUs ONE experiment may claim" not in line


def test_each_clause_speaks_only_when_it_has_something_to_say():
    assert _gpu_pool_note(_ctx()) == "", "no pool sent: say nothing"
    assert _gpu_pool_note(SimpleNamespace(gpu_pool=True)) == "", "a bool is not a pool"
    assert _gpu_pool_note(_ctx(gpu_pool=3)).endswith("which then waits until they are free.\n"), \
        "no table, no clause"
    bare = _gpu_pool_note(_ctx(gpu_pool=2, gpu_budget_by_width={1: 2, 2: 1}))
    assert "open proposal" not in bare and "operator" not in bare
    zero = _gpu_pool_note(_ctx(gpu_pool=0, open_proposals=1, widest_declared_gpus=2))
    # The critic: "every experiment runs on CPU" was false of a declared-GPU proposal (refused).
    assert zero.startswith("GPU POOL: 0 devices detected — an experiment that declares no GPU "
                           "count runs on CPU, and one that declares GPUs is refused admission")
    assert _gpu_pool_note(SimpleNamespace(gpu_pool=-1)) == "", "a negative pool is not a pool"
    # Exactly one proposal is still named (MUTATION: `if ctx.open_proposals > 1`).
    assert "1 open proposal(s): the widest declares 2 GPU(s)" in zero
    # The width table is sorted whatever order the engine handed it in.
    shuffled = _gpu_pool_note(_ctx(gpu_pool=4, gpu_budget_by_width={4: 1, 1: 4, 2: 2}))
    assert "eval_parallel 1 -> 4, 2 -> 2, 4 -> 1 (now 2)" in shuffled
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
def _engine(tmp_path, *, on: bool, gpus=(0, 1, 2, 3), width=2, gpu_capable=True):
    engine = make_engine(tmp_path / "run", strategist_gpu_brief=on)
    engine._gpu_ids = list(gpus)
    engine._eval_parallel = width
    # The factory's toy task is CPU-locked; the line is about a GPU-capable one unless asked.
    engine._task_gpu_capable = lambda: gpu_capable
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


# ------------------------------------------------------------------------------------ the critic
def test_a_cpu_locked_task_gets_no_gpu_line(tmp_path):
    """Admission grants an undeclared experiment 0 devices there and AUTO width is 1 on purpose;
    the Researcher's GPU cue is silent for the same reason. MUTATION: drop the gate -> red."""
    engine = _engine(tmp_path, on=True, gpu_capable=False)
    ctx = engine._strategy_ctx(RunState())
    assert ctx.gpu_pool is None and ctx.gpu_budget_by_width == {}


def test_a_width_past_the_pool_is_shown_and_a_zero_declaration_counts_as_declared(tmp_path,
                                                                                   monkeypatch):
    """MUTATIONS: cap the widths at the pool alone; drop the 1,024 ceiling; `f > 0` for a declared
    `gpus: 0` (a CPU declaration IS a declaration); count only `None` as undeclared."""
    wide = _engine(tmp_path / "wide", on=True, gpus=(0, 1), width=6)
    ctx = wide._strategy_ctx(RunState())
    assert ctx.gpu_budget_by_width == {1: 2, 2: 1}, (
        "past the pool no declaration keeps every experiment running: not a row of that table")
    assert ("(now 6, above the pool: at most 2 experiments that take a device run at once and "
            "the rest of those queue)") in _gpu_pool_note(ctx)
    huge = _engine(tmp_path / "huge", on=True, gpus=range(2000), width=2)
    assert max(huge._gpu_pool_ctx(RunState())["gpu_budget_by_width"]) <= 1024
    zero = _engine(tmp_path / "zero", on=True)
    monkeypatch.setattr(zero, "_proposal_footprints", lambda state: [0, 0, "junk"])
    ctx = zero._strategy_ctx(RunState())
    assert (ctx.widest_declared_gpus, ctx.undeclared_proposals) == (0, 1)


def test_a_non_int_width_is_dropped_before_the_sort(tmp_path):
    """The critic: `sorted({1, 2, None, 4})` raised inside `_strategy_ctx` with the switch ON."""
    engine = _engine(tmp_path, on=True)
    engine._eval_parallel = None
    assert engine._gpu_pool_ctx(RunState())["gpu_budget_by_width"] == {1: 4, 2: 2, 4: 1}


def test_the_operator_owns_the_width_through_a_set_strategy_pin_too(tmp_path):
    """The critic: a `set_strategy{eval_parallel}` pin overwrites the Strategist's width, and the line
    stayed silent. Read as the consult APPLIES it (crit_v45 L4): the CURRENT pending pin, canonical
    name, a valid width — a legacy `max_parallel` pin and an older `_pinned` decide nothing there."""
    engine = _engine(tmp_path, on=True)
    state = RunState()
    assert engine._strategy_ctx(state).eval_parallel_operator_owned is False
    state.pending_strategy = {"eval_parallel": 3}
    assert engine._strategy_ctx(state).eval_parallel_operator_owned is True
    for not_a_width in ({"max_parallel": 3}, {"eval_parallel": "four"}, {"eval_parallel": 2000},
                        {"eval_parallel": True}):
        state.pending_strategy = not_a_width
        assert engine._strategy_ctx(state).eval_parallel_operator_owned is False, not_a_width
    state.pending_strategy = {"developer": "default"}
    state.active_strategy = {"eval_parallel": 2, "_pinned": ["eval_parallel"]}
    assert engine._strategy_ctx(state).eval_parallel_operator_owned is False, (
        "a later pin REPLACED the pinned set: the next consult applies the Strategist's width")


class _Widen:
    def __init__(self):
        self.ctxs = []

    def decide(self, state, ctx):
        self.ctxs.append(ctx)
        return {"eval_parallel": 4, "source": "rule", "rationale": "widen"}


def _consulted(tmp_path, pins=(), **engine_kwargs):
    """One real consult by a Strategist that asks for width 4, after the operator's `pins`: what the
    brief SAID about the width, and whether 4 was then APPLIED."""
    from looplab.core.models import Idea, Node, NodeStatus
    from looplab.events.replay import fold
    from tests.factories import make_engine

    stub = _Widen()
    engine = make_engine(tmp_path / "run", strategist=stub, strategist_every=1, eval_parallel=1,
                         strategist_gpu_brief=True, **engine_kwargs)
    engine._gpu_ids, engine._task_gpu_capable = [0, 1, 2, 3], lambda: True
    engine.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                        "direction": "min"})
    for count, pin in enumerate(pins, start=1):
        engine.store.append("set_strategy", {"strategy": pin})
        state = fold(engine.store.read_all())
        state.nodes = {i: Node(id=i, operator="draft", idea=Idea(operator="draft"),
                               status=NodeStatus.evaluated, metric=float(i)) for i in range(count)}
        engine._strategist_consulted_at = None
        engine._maybe_consult_strategist(state)
    stub.ctxs.clear()
    engine._strategist_consulted_at = None
    state = fold(engine.store.read_all())
    state.nodes = {i: Node(id=i, operator="draft", idea=Idea(operator="draft"),
                           status=NodeStatus.evaluated, metric=float(i)) for i in range(9)}
    engine._maybe_consult_strategist(state)
    return stub.ctxs[-1].eval_parallel_operator_owned, engine._eval_parallel == 4


@pytest.mark.parametrize("pins, engine_kwargs", [
    ((), {}),
    (({"eval_parallel": 1},), {}),
    (({"max_parallel": 1},), {}),
    (({"eval_parallel": 1}, {"developer": "default"}), {}),
    ((), {"agent_control": "revoke"}),
], ids=["open", "pinned", "legacy-pin", "pin-then-another-pin", "grant-revoked"])
def test_the_brief_says_not_applied_exactly_when_the_width_is_not_applied(tmp_path, pins,
                                                                           engine_kwargs):
    """crit_v45 L4, driven through real consults: the line said "a width you choose is not applied"
    of a legacy pin and of a pin a later `set_strategy` had replaced — both applied — and nothing
    when `agent_control` revoked the grant, which applied nothing. MUTATIONS: read `_pinned` or the
    legacy spelling again; drop the `agent_control` clause."""
    if engine_kwargs.get("agent_control") == "revoke":
        from looplab.core.config import default_agent_control
        control = default_agent_control()
        control["eval_parallel"] = []
        engine_kwargs = {"agent_control": control}
    said_not_applied, applied = _consulted(tmp_path, pins, **engine_kwargs)
    assert said_not_applied is (not applied)


def test_built_nodes_waiting_to_run_are_the_queue_a_width_admits_next(tmp_path):
    """The critic: the incident's 4-GPU work was BUILT nodes, which the Card population misses. A
    node already admitted (its eval started), a tombstone and a finished node are not waiting."""
    from looplab.core.models import Idea, Node, NodeStatus
    engine = _engine(tmp_path, on=True)
    state = RunState()

    def node(i, footprint=None, **kw):
        n = Node(id=i, parent_ids=[], operator="draft", code="",
                 idea=Idea(operator="draft", params={}, rationale="r", footprint=footprint))
        for k, v in kw.items():
            setattr(n, k, v)
        state.nodes[i] = n

    node(0, {"gpus": 4})
    node(1)
    node(2, {"gpus": 8}, eval_activity_started=True)
    node(3, {"gpus": 8}, tombstoned=True)
    node(4, {"gpus": 8}, status=NodeStatus.evaluated)
    ctx = engine._strategy_ctx(state)
    assert (ctx.waiting_nodes, ctx.widest_waiting_gpus, ctx.undeclared_waiting) == (2, 4, 1)
