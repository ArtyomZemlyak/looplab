"""Layer-2 shared LLM broker: atomic limits, fairness, transport wiring and compatibility."""
from __future__ import annotations

import threading
import time

import pytest

from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import ToyObjectiveDeveloper, ToyResearcher
from looplab.core.llm import CostAccountant, LiteLLMClient, OpenAICompatibleClient
from looplab.core.llm_broker import (LLMConcurrencyBroker, current_llm_lane,
                                     default_llm_lane_limits, llm_broker_scope,
                                     llm_lane_scope, llm_request_permit,
                                     normalize_llm_lane_limits)
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.concept_graph import dense_retrieval_skeleton, tag_nodes_llm
from looplab.search.policy import GreedyTree


def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true before timeout")


def _join(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.join(3)
    assert not [thread for thread in threads if thread.is_alive()]


def test_total_and_lane_admission_share_one_atomic_decision():
    broker = LLMConcurrencyBroker(total=3, lane_limits={"build": 2, "deep_research": 1})
    release = threading.Event()
    threads = []

    def work(lane: str) -> None:
        with broker.borrow(lane):
            release.wait(3)

    for lane in ["build"] * 4 + ["deep_research"] * 2:
        thread = threading.Thread(target=work, args=(lane,), daemon=True)
        threads.append(thread)
        thread.start()

    _wait_for(lambda: broker.snapshot()["borrowed"] == 3)
    snap = broker.snapshot()
    assert snap["borrowed_by_lane"]["build"] == 2
    assert snap["borrowed_by_lane"]["deep_research"] == 1
    release.set()
    _join(threads)
    snap = broker.snapshot()
    assert snap["peak"] == 3
    assert snap["peak_by_lane"]["build"] == 2
    assert snap["peak_by_lane"]["deep_research"] == 1


def test_exception_releases_permit():
    broker = LLMConcurrencyBroker(total=1, lane_limits={"build": 1})
    with pytest.raises(RuntimeError, match="boom"):
        with broker.borrow("build"):
            raise RuntimeError("boom")
    assert broker.snapshot()["borrowed"] == 0
    with broker.borrow("build"):
        assert broker.snapshot()["borrowed"] == 1


def test_interrupted_waiter_removes_its_fifo_ticket(monkeypatch):
    broker = LLMConcurrencyBroker(total=1, lane_limits=default_llm_lane_limits(1))
    release = threading.Event()

    def holder() -> None:
        with broker.borrow("build"):
            release.wait(3)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    _wait_for(lambda: broker.snapshot()["borrowed"] == 1)

    def interrupted_wait():
        raise KeyboardInterrupt("cancelled")

    monkeypatch.setattr(broker._condition, "wait", interrupted_wait)
    with pytest.raises(KeyboardInterrupt, match="cancelled"):
        with broker.borrow("deep_research"):
            raise AssertionError("interrupted waiter must never enter")
    assert broker.snapshot()["waiting_by_lane"]["deep_research"] == 0
    release.set()
    _join([thread])


def test_reconfigure_in_place_does_not_revoke_borrowers_or_overadmit():
    broker = LLMConcurrencyBroker(total=2, lane_limits={"build": 2})
    releases = [threading.Event(), threading.Event()]
    active = []
    active_lock = threading.Lock()

    def incumbent(index: int) -> None:
        with broker.borrow("build"):
            with active_lock:
                active.append(index)
            releases[index].wait(3)

    incumbents = [threading.Thread(target=incumbent, args=(i,), daemon=True) for i in range(2)]
    for thread in incumbents:
        thread.start()
    _wait_for(lambda: broker.snapshot()["borrowed"] == 2)

    broker.reconfigure(total=1, lane_limits={"build": 1})
    newcomer_entered = threading.Event()

    def newcomer() -> None:
        with broker.borrow("build"):
            newcomer_entered.set()

    newcomer_thread = threading.Thread(target=newcomer, daemon=True)
    newcomer_thread.start()
    _wait_for(lambda: broker.snapshot()["waiting_by_lane"]["build"] == 1)
    releases[0].set()
    _wait_for(lambda: broker.snapshot()["borrowed"] == 1)
    assert not newcomer_entered.wait(0.1)  # usage == the lowered ceiling: no over-admission
    releases[1].set()
    assert newcomer_entered.wait(2)
    _join([*incumbents, newcomer_thread])


def test_reconfigure_expand_then_shrink_preserves_fairness_and_both_limits():
    broker = LLMConcurrencyBroker(total=1, lane_limits=default_llm_lane_limits(1))
    releases = {name: threading.Event() for name in ("active", "research", "queued-build")}
    entered: list[str] = []
    entered_lock = threading.Lock()

    def work(name: str, lane: str) -> None:
        with broker.borrow(lane):
            with entered_lock:
                entered.append(name)
            releases[name].wait(3)

    active = threading.Thread(target=work, args=("active", "build"), daemon=True)
    active.start()
    _wait_for(lambda: entered == ["active"])
    research = threading.Thread(target=work, args=("research", "deep_research"), daemon=True)
    queued_build = threading.Thread(target=work, args=("queued-build", "build"), daemon=True)
    research.start()
    _wait_for(lambda: broker.snapshot()["waiting_by_lane"]["deep_research"] == 1)
    queued_build.start()
    _wait_for(lambda: broker.snapshot()["waiting_by_lane"]["build"] == 1)

    broker.reconfigure(total=3, lane_limits={"build": 2, "deep_research": 1, "enrichment": 1})
    _wait_for(lambda: broker.snapshot()["borrowed"] == 3)
    snap = broker.snapshot()
    assert snap["borrowed_by_lane"]["build"] == 2
    assert snap["borrowed_by_lane"]["deep_research"] == 1
    assert set(entered) == {"active", "research", "queued-build"}

    broker.reconfigure(total=1, lane_limits={"build": 1, "deep_research": 1, "enrichment": 1})
    newcomer_entered = threading.Event()

    def newcomer() -> None:
        with broker.borrow("enrichment"):
            newcomer_entered.set()

    newcomer_thread = threading.Thread(target=newcomer, daemon=True)
    newcomer_thread.start()
    _wait_for(lambda: broker.snapshot()["waiting_by_lane"]["enrichment"] == 1)
    for name, expected_borrowed in (("active", 2), ("research", 1)):
        releases[name].set()
        _wait_for(lambda expected=expected_borrowed: broker.snapshot()["borrowed"] == expected)
        assert not newcomer_entered.wait(0.05)
    releases["queued-build"].set()
    assert newcomer_entered.wait(2)
    _join([active, research, queued_build, newcomer_thread])


def test_round_robin_prevents_deep_research_starvation_and_keeps_lane_fifo():
    broker = LLMConcurrencyBroker(total=1, lane_limits=default_llm_lane_limits(1))
    first_release = threading.Event()
    order: list[str] = []
    order_lock = threading.Lock()

    def work(label: str, lane: str, hold: threading.Event | None = None) -> None:
        with broker.borrow(lane):
            with order_lock:
                order.append(label)
            if hold is not None:
                hold.wait(3)

    first = threading.Thread(target=work, args=("build-active", "build", first_release), daemon=True)
    first.start()
    _wait_for(lambda: order == ["build-active"])

    backlog = []
    for i in range(3):
        thread = threading.Thread(target=work, args=(f"build-{i}", "build"), daemon=True)
        backlog.append(thread)
        thread.start()
        _wait_for(lambda i=i: broker.snapshot()["waiting_by_lane"]["build"] == i + 1)
    research = threading.Thread(target=work, args=("research", "deep_research"), daemon=True)
    research.start()
    _wait_for(lambda: broker.snapshot()["waiting_by_lane"]["deep_research"] == 1)

    first_release.set()
    _join([first, research, *backlog])
    assert order[1] == "research"  # not queued behind the permanent build lane backlog
    assert [item for item in order if item.startswith("build-")] == [
        "build-active", "build-0", "build-1", "build-2"]


def test_disabled_broker_preserves_unbounded_research_overlap():
    broker = LLMConcurrencyBroker(total=None, lane_limits={})
    width = 12
    all_entered = threading.Barrier(width + 1)
    release = threading.Event()

    def work() -> None:
        with broker.borrow("deep_research"):
            all_entered.wait(3)
            release.wait(3)

    threads = [threading.Thread(target=work, daemon=True) for _ in range(width)]
    for thread in threads:
        thread.start()
    all_entered.wait(3)
    assert broker.snapshot()["borrowed"] == width
    assert broker.enabled is False
    release.set()
    _join(threads)


def _transport_client(transport):
    """Minimal OpenAICompatibleClient using the real _post admission path and a fake transport."""
    client = OpenAICompatibleClient.__new__(OpenAICompatibleClient)
    client.model = "fake"
    client.temperature = 0.7
    client._cache = None
    client._max_retries = 0
    client.stream = False
    client._stream_stalls = 0
    client.reasoning = {}
    client._reasoning_ok = True
    client.base_url = "http://fake"
    client.accountant = CostAccountant()
    client._sdk_chat = transport
    return client


def test_openai_transport_attempts_borrow_but_producer_scopes_do_not_nest():
    seen = []

    def transport(payload, use_stream):
        seen.append(current_llm_lane())
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}

    client = _transport_client(transport)
    broker = LLMConcurrencyBroker(total=1, lane_limits=default_llm_lane_limits(1))
    with llm_broker_scope(broker):
        # A producer scope only labels. Its provider request releases before the nested producer runs.
        with llm_lane_scope("build"):
            client._post({"model": "fake", "messages": [], "temperature": 0.7})
            with llm_lane_scope("novelty_dedup"):
                client._post({"model": "fake", "messages": [], "temperature": 0.7})
    assert seen == ["build", "novelty_dedup"]
    assert broker.snapshot()["borrowed"] == 0


def test_litellm_transport_attempts_share_the_same_total(monkeypatch):
    broker = LLMConcurrencyBroker(total=1, lane_limits=default_llm_lane_limits(1))
    active = 0
    peak = 0
    lock = threading.Lock()

    class _Gateway:
        @staticmethod
        def completion(**kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return object()

    client = LiteLLMClient("fake")
    monkeypatch.setattr(client, "_litellm", lambda: _Gateway())

    def work() -> None:
        with llm_broker_scope(broker), llm_lane_scope("build"):
            client._completion(messages=[])

    threads = [threading.Thread(target=work, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    _join(threads)
    assert peak == 1
    assert broker.snapshot()["peak"] == 1


def test_concept_thread_pool_copies_broker_and_lane_context(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "t", "task_id": "x", "goal": "g", "direction": "max"})
    for node_id in range(4):
        store.append("node_created", {
            "node_id": node_id, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {"x": node_id}, "rationale": f"idea {node_id}"},
        })
        store.append("node_evaluated", {"node_id": node_id, "metric": float(node_id)})
    state = fold(store.read_all())
    lanes: list[str] = []

    class _ContextClient:
        def complete_tool(self, messages, json_schema):
            lanes.append(current_llm_lane())
            # Fake clients bypass core.llm; borrow explicitly so this test proves the copied broker too.
            with llm_request_permit():
                time.sleep(0.01)
            return {"concept_ids": []}

        def complete_text(self, messages):
            return "{}"

    broker = LLMConcurrencyBroker(total=1, lane_limits=default_llm_lane_limits(1))
    with llm_broker_scope(broker), llm_lane_scope("enrichment"):
        tag_nodes_llm(state, dense_retrieval_skeleton(), _ContextClient(), max_workers=4)
    assert lanes == ["enrichment"] * 4
    assert broker.snapshot()["peak"] == 1


def _engine(tmp_path, **kwargs) -> Engine:
    task = ToyTask()
    return Engine(
        tmp_path,
        task=task,
        researcher=ToyResearcher(task.bounds, seed=task.seed, step=task.step),
        developer=ToyObjectiveDeveloper(),
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=2, max_nodes=3),
        **kwargs,
    )


def test_engine_enables_shared_total_only_for_positive_canonical_value(tmp_path):
    default = _engine(tmp_path / "default")
    legacy = _engine(tmp_path / "legacy", parallel_build=4)
    auto = _engine(tmp_path / "auto", eval_parallel=3, llm_parallel=0)
    finite = _engine(tmp_path / "finite", llm_parallel=3)

    assert default._llm_broker.snapshot()["total"] is None
    assert legacy._llm_broker.snapshot()["total"] is None
    assert legacy._llm_parallel == 4
    assert auto._llm_broker.snapshot()["total"] is None
    assert auto._llm_parallel == 3  # startup AUTO still couples build fan-out to resolved eval width
    assert finite._llm_broker.snapshot()["total"] == 3


def test_env_legacy_parallel_build_does_not_enable_shared_broker_end_to_end(tmp_path, monkeypatch):
    """End-to-end: a config/startup load with only LOOPLAB_PARALLEL_BUILD sets the build width but must
    NOT enable the finite shared broker — the legacy value cannot silently serialize every provider call
    through the env→Settings→Engine path."""
    from looplab.core.config import Settings
    from looplab.engine.options import EngineOptions

    monkeypatch.setenv("LOOPLAB_PARALLEL_BUILD", "3")
    settings = Settings()
    assert settings.llm_parallel is None and settings.parallel_build == 3

    engine = _engine(tmp_path / "env-legacy", options=EngineOptions.from_settings(settings))
    assert engine._llm_parallel == 3                        # legacy build width is honored
    assert engine._llm_broker.snapshot()["total"] is None   # ...but the shared broker stays off


def test_canonical_operator_override_reconfigures_broker_but_legacy_does_not(tmp_path):
    from looplab.core.models import RunState

    canonical = _engine(tmp_path / "canonical-control")
    canonical._apply_control_overrides(RunState(budget_overrides={"llm_parallel": 2}))
    assert canonical._llm_broker.snapshot()["total"] == 2

    legacy = _engine(tmp_path / "legacy-control")
    legacy._apply_control_overrides(RunState(budget_overrides={"parallel_build": 2}))
    assert legacy._llm_parallel == 2
    assert legacy._llm_broker.snapshot()["total"] is None


def test_total_only_live_reconfiguration_preserves_explicit_lane_allocation(tmp_path):
    from looplab.core.models import RunState

    engine = _engine(tmp_path / "preserve-lanes", llm_parallel=4)
    # Deliberately equals the total=4 default numerically: provenance, not value comparison, must
    # remember that the Strategy explicitly owns this allocation when the total later changes.
    expected = {"build": 4, "deep_research": 1, "novelty_dedup": 1,
                "enrichment": 1, "engine": 1}
    engine._apply_strategy({"llm_lane_limits": expected})
    state = RunState(budget_overrides={"llm_parallel": 2})
    # Control overrides persist in the fold and are applied each loop; neither the first nor a later
    # pass may silently reset the durable Strategist allocation to default_llm_lane_limits(total).
    engine._apply_control_overrides(state)
    engine._apply_control_overrides(state)
    assert engine._llm_broker.snapshot()["total"] == 2
    assert engine._llm_broker.snapshot()["lane_limits"] == expected


def test_last_canonical_total_survives_newer_legacy_build_override_and_resume(tmp_path):
    store = EventStore(tmp_path / "broker-lww-events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    store.append("budget_extend", {"llm_parallel": 4})
    store.append("budget_extend", {"parallel_build": 2})
    state = fold(store.read_all())
    assert state.budget_overrides["parallel_build"] == 2
    assert "llm_parallel" not in state.budget_overrides
    assert state.budget_overrides["llm_broker_total"] == 4

    # CODEX AGENT: a live engine and a fresh resume must reconstruct the same two independent facts:
    # legacy controls build fan-out, while the last explicit canonical intent owns the shared total.
    for suffix in ("live", "resume"):
        engine = _engine(tmp_path / suffix)
        engine._apply_control_overrides(state)
        assert engine._llm_parallel == 2
        assert engine._llm_broker.snapshot()["total"] == 4


def test_poisoned_broker_override_does_not_mutate_the_existing_limit(tmp_path):
    engine = _engine(tmp_path / "poisoned-broker")
    for value in (True, -1, 65, 1.5, float("inf"), "not-an-int"):
        engine._reconfigure_llm_broker(value)
        assert engine._llm_broker.snapshot()["total"] is None


def test_lane_allocation_validation_is_closed_and_typed():
    assert normalize_llm_lane_limits({"build": 2, "enrichment": None}) == {
        "build": 2, "enrichment": None}
    for bad in ({"unknown": 1}, {"build": 0}, {"build": True}, {"build": "2"}):
        with pytest.raises(ValueError):
            normalize_llm_lane_limits(bad)
