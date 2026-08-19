"""The footprint is the AGENT's decision, and both prompts asking for it said it was not.

Two paragraphs reach the Researcher about `footprint.gpus` and, until this change, both closed on
the same claim: that declaring more than the ordinary per-experiment share "does NOT get this
experiment more hardware" and that "the run serialises at the SAME per-experiment cost".

* `engine/proposal_cues.py::_gpu_budget_hint_text` — the engine-stamped GPU BUDGET cue.
* `agents/roles.py::_FOOTPRINT_GUIDANCE` — the code-owned capability suffix both variants append.

The scheduler contradicts both halves, and `test_the_scheduler_honours_the_declaration_the_old_text
_denied` is the proof rather than an appeal to the docstrings: a declared `{"gpus": 2}` is taken
over AUTO (`resources.py::_resource_request_for_node`), reserved all-or-nothing
(`_acquire_gpus`) and written into the child's `CUDA_VISIBLE_DEVICES` (`_resource_eval_env`) — and
the per-experiment cost is precisely the thing that changes, which is the whole reason the choice
exists. The wording was written from the `rubertlite-dr-unified-v5` incident, which was a WIDTH
defect (`run_started` went on claiming 2 while one node held both cards); `Settings.proposal_width`
closed that in the scheduler and the sentence outlived its cause.

WHAT THIS DOES NOT CLAIM. Nothing here says two devices are faster or slower — this repo cannot
say: every node of all six preserved runs with a footprint declared `{"gpus": 1}` (four of them
under a task statement that ordered it), and the only 2-GPU population on the box
(`runs/rubertlite-dense-retrieval`) is a different repo, model and framework. The cue states the
arithmetic that holds regardless, names the per-device memory the scheduler itself reserves
against, and invites a probe — the same remedy `_cue_experiment_time_budget` already offers for
per-step time.

docs/36: a wider ACTION space (the role may now ask for more, with a reason), never a wider trusted
set. The scheduler still clamps to the pool, `proposal_derived_width` still owns the width, and
nothing here reaches a metric, a champion, selectability or a violation.

Everything is offline: fake GPU ids, fake clients, the toy task.
"""
from __future__ import annotations

import pytest

import looplab.engine.orchestrator as _orch
from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import (RESEARCHER_HINT_ATTRS, LLMResearcher, ToyObjectiveDeveloper,
                                  ToyResearcher, footprint_guidance)
from looplab.core.models import Idea, Node, RunState
from looplab.engine.orchestrator import Engine
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.card_selection import CardResourceEnvelope
from looplab.search.policy import GreedyTree


# --------------------------------------------------------------------------- fixtures / fakes
class _ToolEmitClient:
    def __init__(self):
        self.messages = None

    def complete_tool(self, messages, json_schema):
        if self.messages is None:
            self.messages = [dict(m) for m in messages]
        return {"operator": "draft", "params": {}, "rationale": "r"}


def _gpu_capable(monkeypatch) -> None:
    """ToyTask declares itself CPU-locked; the budget cue is deliberately silent there."""
    monkeypatch.setattr(ToyTask, "gpu_capable", lambda self: True)


def _engine(run_dir, **kw) -> Engine:
    task = ToyTask()
    researcher = ToyResearcher(task.bounds, seed=task.seed, step=task.step)
    researcher.client = object()          # mark it LLM-backed (`_build_calls_an_llm`)
    return Engine(run_dir, task=task, researcher=researcher,
                  developer=ToyObjectiveDeveloper(), sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=2, max_nodes=2), n_seeds=2, max_nodes=2, **kw)


def _two_gpu_engine(tmp_path, monkeypatch, name, **kw) -> Engine:
    """This box: two devices, the AUTO width the engine settles from them, real memory sizes."""
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1])
    _gpu_capable(monkeypatch)
    engine = _engine(tmp_path / name, eval_parallel=2, **kw)
    engine._gpu_mem = {0: 143771, 1: 143771}
    return engine


def _stamp(engine, researcher) -> str:
    engine._set_complexity_hint(RunState(goal="g", direction="min"), None, researcher=researcher)
    return getattr(researcher, "_gpu_budget_hint", "<UNSET>")


# The historical paragraph, verbatim, as the `false` path must still produce it. Kept as a literal
# here rather than imported from the module: importing whatever the module happens to build is a
# pin that moves with the code it is pinning.
_LEGACY_TAIL = (
    "\nGPU BUDGET — this run evaluates up to 2 experiment(s) concurrently on a pool of 2 GPU(s), "
    "so ONE experiment may declare at most `footprint.gpus = 1`. That is a CEILING, and declaring "
    "more does NOT get this experiment more hardware: the extra devices are taken from the sibling "
    "experiments that would otherwise run at the same time, so the run serialises at the same "
    "per-experiment cost. Declaring `gpus: 1` is the ordinary case, not an escalation. Whatever "
    "you declare, the training/eval command must target that SAME count.")


# --------------------------------------------------------------------------- the claim vs the code
def test_the_scheduler_honours_the_declaration_the_old_text_denied(tmp_path, monkeypatch):
    """CHARACTERIZATION, and it holds before this change too — that is the point. It is what makes
    the old sentence false rather than merely pessimistic, so it is pinned here beside the wording
    it refutes: the declaration reaches admission, takes BOTH devices all-or-nothing, and the child
    is fenced to exactly those two."""
    engine = _two_gpu_engine(tmp_path, monkeypatch, "honoured")
    node = Node(id=0, operator="draft",
                idea=Idea(operator="draft", params={}, footprint={"gpus": 2}), code="")

    request = engine._resource_request_for_node(node)
    assert request["count"] == 2 and request["unspecified"] is False

    reservation = engine._try_reserve_node_resources(node)
    assert reservation is not None and sorted(reservation["gpu_ids"]) == [0, 1]
    assert engine._free_gpus == []                       # the sibling lane really is taken
    env = engine._resource_eval_env(reservation, inherit_host=False)
    assert env["CUDA_VISIBLE_DEVICES"] == "0,1"
    engine._release_gpus(reservation["gpu_ids"])


def test_the_cue_states_the_trade_instead_of_denying_it(tmp_path, monkeypatch):
    """The shipped default must not tell the role something the scheduler contradicts."""
    engine = _two_gpu_engine(tmp_path, monkeypatch, "trade")
    hint = _stamp(engine, LLMResearcher(_ToolEmitClient()))

    assert "does NOT get this experiment more hardware" not in hint
    assert "serialises at the same per-experiment cost" not in hint
    assert "a LARGER count is HONOURED" in hint
    # The numbers the pre-change cue carried are unchanged — this corrects a claim, not a ceiling.
    assert "up to 2 experiment(s)" in hint and "pool of 2 GPU(s)" in hint
    assert "`footprint.gpus = 1` is the ORDINARY declaration" in hint
    assert "the training/eval command must target that SAME count" in hint


def test_the_cue_states_both_directions_of_the_arithmetic(tmp_path, monkeypatch):
    """Only one of the two is a throughput WIN, and quoting either alone is the old defect with the
    sign flipped: same per-device batch buys latency at constant throughput (and changes the
    experiment), same global batch buys latency at a throughput LOSS."""
    engine = _two_gpu_engine(tmp_path, monkeypatch, "arithmetic")
    hint = _stamp(engine, LLMResearcher(_ToolEmitClient()))

    assert "SAME per-device batch" in hint and "~1/K the steps" in hint
    assert "a DIFFERENT experiment" in hint
    assert "GLOBAL batch" in hint and "speedup BELOW K" in hint
    # The case where the count is not a preference at all, and the substitute that is not one.
    assert "does not FIT on one device" in hint
    assert "gradient accumulation restores the effective batch and never the in-batch negative pool" \
        in hint
    # The negative-pool half is CONDITIONAL and must stay so: whether K devices enlarge the pool is a
    # property of the LOSS, not the box. `vectorsearch/training/loss.py::NLLCosLoss` — configured by
    # all five evaluated nodes of the live run — documents itself "Operates on the per-device batch
    # (no cross-process gather)", while its CrossBatch/SigLIP/Qwen3 siblings and the legacy
    # dense-retrieval repo do gather. A cue that asserted the pool always scales would be the old
    # defect's shape with a different claim.
    assert "for a contrastive loss that GATHERS across devices" in hint
    assert "check which your loss does" in hint
    # It must not invent a speedup this box has never measured.
    assert "UNMEASURED" in hint and "fixed-step probe" in hint


def test_the_memory_clause_is_the_schedulers_own_inventory(tmp_path, monkeypatch):
    """The role is asked whether the experiment fits on ONE device and was never told how big one
    is. The number is `_gpu_mem` — what `_clamp_resource_footprint` already clamps `gpu_mem_mib`
    against — so the prompt and admission cannot disagree."""
    engine = _two_gpu_engine(tmp_path, monkeypatch, "memory")
    assert "(each holding ~140 GiB)" in _stamp(engine, LLMResearcher(_ToolEmitClient()))

    # The SMALLEST device, because first-fit may hand this experiment either one.
    engine._gpu_mem = {0: 143771, 1: 40960}
    assert "(each holding ~40 GiB)" in _stamp(engine, LLMResearcher(_ToolEmitClient()))


@pytest.mark.parametrize("inventory", [{}, {0: 143771}, {0: 143771, 1: 0}, {0: 143771, 1: "big"}])
def test_a_partial_memory_inventory_says_nothing_rather_than_guessing(
        tmp_path, monkeypatch, inventory):
    """`detect_gpu_inventory` returns `({}, {})` rather than guessing a join, and admission degrades
    to count-only there. A prompt that named one device's capacity for a pool it cannot describe
    would be the plausible-wrong-number failure the whole cue exists to stop."""
    engine = _two_gpu_engine(tmp_path, monkeypatch, f"partial-{len(inventory)}-{inventory}")
    engine._gpu_mem = dict(inventory)
    hint = _stamp(engine, LLMResearcher(_ToolEmitClient()))
    assert "GiB" not in hint
    assert "pool of 2 GPU(s), so `footprint.gpus = 1`" in hint      # the clause simply closes up


# --------------------------------------------------------------------------- the off path
def test_off_restores_the_historical_paragraph_byte_for_byte(tmp_path, monkeypatch):
    engine = _two_gpu_engine(tmp_path, monkeypatch, "legacy", gpu_footprint_cue=False)
    assert _stamp(engine, LLMResearcher(_ToolEmitClient())) == _LEGACY_TAIL


def test_off_restores_the_historical_capability_suffix_byte_for_byte():
    """The role-side half. Both alternatives are spliced at the SAME position, so `false` is the old
    clause and not a shorter paragraph — the head and tail are byte-identical either way."""
    legacy, choice = footprint_guidance(False), footprint_guidance(True)
    assert "the run SERIALISES at the same per-experiment cost" in legacy
    assert "the run SERIALISES at the same per-experiment cost" not in choice
    assert "a LARGER count IS honoured" in choice
    for shared in ("Optionally set `footprint` to a JSON object",
                   "unspecified is distinct from `gpus=1`",
                   "Size the training/eval command to the count you declare.",
                   "the engine/operator own authority fields. "):
        assert shared in legacy and shared in choice
    head = "Optionally set `footprint`"
    assert legacy.index(head) == choice.index(head) == 0
    assert legacy.split("When the user turn states")[0] == choice.split(
        "When the user turn states")[0]


def test_a_gpu_less_host_is_unaffected_by_the_flag(tmp_path, monkeypatch):
    """`budget == 0` is a REFUSAL, not a trade — there is no larger count to weigh."""
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [])
    _gpu_capable(monkeypatch)
    on = _stamp(_engine(tmp_path / "nogpu-on"), LLMResearcher(_ToolEmitClient()))
    off = _stamp(_engine(tmp_path / "nogpu-off", gpu_footprint_cue=False),
                 LLMResearcher(_ToolEmitClient()))
    assert on == off and "exposes NO GPU" in on


# --------------------------------------------------------------------------- delivery, both prompts
def test_both_researcher_prompts_ask_the_corrected_question(tmp_path, monkeypatch):
    """The DELIVERY half, and the reason the flag is in `RESEARCHER_HINT_ATTRS`: the two variants
    must ask the SAME question about the same declaration. Threaded onto the outermost researcher
    like `_memo_verdict_cue`; an unstamped role keeps the historical clause, so a library caller
    with no engine sees no change at all."""
    from looplab.agents import agent as agent_mod
    from looplab.agents.agent import ToolUsingResearcher

    assert "_gpu_footprint_cue" in RESEARCHER_HINT_ATTRS
    engine = _two_gpu_engine(tmp_path, monkeypatch, "delivery")
    state = RunState(goal="g", direction="min")

    plain_client = _ToolEmitClient()
    plain = LLMResearcher(plain_client)
    setattr(plain, "_gpu_footprint_cue", engine._gpu_footprint_cue)
    plain.propose(state, None)
    plain_turn = "".join(m["content"] for m in plain_client.messages)

    seen: dict = {}

    def _fake_run_phase(client, tools, messages, emit_spec, **kw):
        seen["messages"] = [dict(m) for m in messages]
        return Idea(operator="draft", params={}, rationale="ok")

    monkeypatch.setattr(agent_mod, "run_phase", _fake_run_phase)
    agentic = ToolUsingResearcher(client=object(), tools=None)
    setattr(agentic, "_gpu_footprint_cue", engine._gpu_footprint_cue)
    agentic.propose(state, None)
    agentic_turn = "".join(m["content"] for m in seen["messages"])

    for turn in (plain_turn, agentic_turn):
        assert "a LARGER count IS honoured" in turn
        assert "the run SERIALISES at the same per-experiment cost" not in turn
        assert "Say WHY in your rationale" in turn

    # …and an UNSTAMPED role is byte-identical to the historical prompt.
    bare_client = _ToolEmitClient()
    LLMResearcher(bare_client).propose(state, None)
    bare_turn = "".join(m["content"] for m in bare_client.messages)
    assert "the run SERIALISES at the same per-experiment cost" in bare_turn


def test_the_engine_stamps_the_flag_onto_its_own_researcher(tmp_path, monkeypatch):
    """Both deliveries come off ONE resolved knob, so the two paragraphs cannot come apart."""
    engine = _two_gpu_engine(tmp_path, monkeypatch, "stamped")
    assert engine._gpu_footprint_cue is True
    assert getattr(engine.researcher, "_gpu_footprint_cue") is True
    off = _two_gpu_engine(tmp_path, monkeypatch, "stamped-off", gpu_footprint_cue=False)
    assert off._gpu_footprint_cue is False
    assert getattr(off.researcher, "_gpu_footprint_cue") is False


@pytest.mark.parametrize("on", [True, False])
def test_a_POOLED_researcher_asks_the_same_question_as_the_primary(tmp_path, monkeypatch, on):
    """The `__init__` setattr covers the primary role and NOTHING ELSE, and a run with a build
    fan-out proposes on pooled ones.

    `_build_role_pairs` builds its extra pairs from `role_factory()` AFTER `__init__` and caches
    them in `_role_pool`; `_prepare_node_idea` is then handed one of those as `researcher`. An
    `__init__`-only stamp therefore has the primary asking the corrected question while every
    pooled sibling asks the pre-change one — the two-variants-disagree drift
    `_researcher_capability_suffix` exists to stop, arriving through the other door. The boolean
    rides on `_stamp_gpu_budget_hint`, which runs per proposal on whichever role is proposing."""
    engine = _two_gpu_engine(tmp_path, monkeypatch, f"pooled-{on}", gpu_footprint_cue=on)
    engine.role_factory = lambda: (LLMResearcher(_ToolEmitClient()), ToyObjectiveDeveloper())

    pairs = engine._build_role_pairs(3)
    pooled = [researcher for researcher, _ in pairs if researcher is not engine.researcher]
    assert pooled, "the pool did not produce a second researcher"
    for researcher in pooled:
        assert not hasattr(researcher, "_gpu_footprint_cue")      # fresh from the factory
        _stamp(engine, researcher)                                 # the real per-proposal path
        assert getattr(researcher, "_gpu_footprint_cue") is on
        researcher.propose(RunState(goal="g", direction="min"), None)
        turn = "".join(m["content"] for m in researcher.client.messages)
        assert ("a LARGER count IS honoured" in turn) is on
        assert ("the run SERIALISES at the same per-experiment cost" in turn) is not on


# --------------------------------------------------------------------------- speculation keeps up
def test_a_node_holding_every_card_does_not_starve_the_speculative_build(tmp_path, monkeypatch):
    """The operator's condition on widening the choice: speculation stays, just for one experiment
    at a time. It already holds and this pins it, because a wider footprint is exactly the state
    that would expose it.

    A speculative BUILD is a Developer call on a producer lane and reserves no device — the
    freshness envelope it is gated on is the PERMANENT machine
    (`speculation.py::_resource_envelope` reads `_gpu_ids`/`_gpu_mem`, never `_free_gpus`), which is
    `card_selection.py`'s stated rule: "a busy GPU makes a Card wait; only a declaration that cannot
    fit on this machine makes speculative work stale". What a full pool stops is DISPATCH, which is
    the intended cost of the choice and not a starved prefetch."""
    engine = _two_gpu_engine(tmp_path, monkeypatch, "spec", speculation_depth=2)
    idle_envelope = engine._resource_envelope()
    idle_ceiling = engine._speculative_prefetch_ceiling()
    assert idle_ceiling >= 1

    node = Node(id=0, operator="draft",
                idea=Idea(operator="draft", params={}, footprint={"gpus": 2}), code="")
    reservation = engine._try_reserve_node_resources(node)
    assert sorted(reservation["gpu_ids"]) == [0, 1] and engine._free_gpus == []

    # Neither the freshness envelope nor the prefetch ceiling moved when the pool went to zero.
    assert engine._resource_envelope() == idle_envelope
    assert idle_envelope == CardResourceEnvelope(gpu_count=2, gpu_memory_mib=(143771, 143771))
    assert engine._speculative_prefetch_ceiling() == idle_ceiling

    # DISPATCH is what waits, and it waits without refusing: the reservation attempt fails now and
    # is retried when a device comes back, rather than retiring the prefetched work.
    waiting = Node(id=1, operator="draft",
                   idea=Idea(operator="draft", params={}, footprint={"gpus": 1}), code="")
    assert engine._try_reserve_node_resources(waiting) is None
    engine._release_gpus(reservation["gpu_ids"])
    assert engine._try_reserve_node_resources(waiting)["gpu_ids"] == [0]


def test_the_speculative_build_path_reserves_no_device(tmp_path, monkeypatch):
    """The other half, stated where a reader can check it: no reservation call appears anywhere on
    the producer's build path, so the prefetch cannot be blocked by a device at all. AST, not a
    substring — and tier 3 (CLAUDE.md), which is why the behavioural half above is the primary."""
    from tests._source_scan import called_names

    reserving = {"_try_reserve_node_resources", "_wait_reserve_node_resources", "_acquire_gpus",
                 "_resource_eval_env"}
    for method in (Engine._produce_card_build, Engine._build_requested_card,
                   Engine._request_card_build, Engine._speculative_prefetch_ceiling):
        assert not (set(called_names(method)) & reserving), method.__name__
