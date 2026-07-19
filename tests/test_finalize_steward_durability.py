from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from looplab.core.models import RunState
from looplab.engine.lessons import LessonMemory
from looplab.engine.memory import ConceptCapsuleStore, build_concept_capsule
from looplab.engine.task_facets import record_task_facets


def _seed_paid_inputs(tmp_path) -> None:
    capsule_path = Path(tmp_path) / "concept_capsules.jsonl"
    if not capsule_path.exists():
        ConceptCapsuleStore(capsule_path).add(build_concept_capsule(
            run_id="seed", task_id="task", fingerprint=["dataset"], direction="max",
            concepts=["retrieval/rerank"], concept_outcomes={},
        ))
    lessons_path = Path(tmp_path) / "lessons.jsonl"
    if not lessons_path.exists():
        lessons_path.write_text(json.dumps({
            "statement": "reranking helps", "outcome": "supported", "evidence": [1],
            "run_id": "seed", "task_id": "task",
        }) + "\n", encoding="utf-8")


def _memory(tmp_path) -> LessonMemory:
    _seed_paid_inputs(tmp_path)
    client = SimpleNamespace(complete_text=lambda _messages: "{}")
    engine = SimpleNamespace(
        memory_dir=str(tmp_path),
        _cross_run_curation=True,
        _cross_run_curation_auto=False,
        researcher=SimpleNamespace(client=client, inner=None, fallback=None),
        developer=None,
        task=SimpleNamespace(kind="dataset"),
    )
    return LessonMemory(engine)


_CASES = [
    (
        "store_concept_curation",
        "looplab.engine.concept_steward.propose_concept_curation",
        "concept_curation_log.jsonl",
        {"merges": [], "splits": [], "purges": []},
    ),
    (
        "store_claim_curation",
        "looplab.engine.claim_steward.propose_claim_curation",
        "claim_curation_log.jsonl",
        {"decisions": []},
    ),
    (
        "store_task_facets",
        "looplab.engine.task_facets.propose_task_facets",
        "task_facets_curation_log.jsonl",
        {},
    ),
]


_POISON_CASES = [
    (*_CASES[0], b"{not-json}\n"),
    (*_CASES[1], b'{"v":999}\n'),
    (*_CASES[2], b'{"v":2,"v":1}\n'),
]


@pytest.mark.parametrize(
    "method_name,target,log_name,result,poison", _POISON_CASES)
def test_unhealthy_paid_history_never_becomes_a_key_miss_or_new_append(
        tmp_path, monkeypatch, method_name, target, log_name, result, poison):
    calls: list[int] = []

    def paid(*_args, **_kwargs):
        calls.append(1)
        return result

    monkeypatch.setattr(target, paid)
    path = tmp_path / log_name
    path.write_bytes(poison)
    before = path.read_bytes()
    memory = _memory(tmp_path)

    getattr(memory, method_name)(
        RunState(run_id="poisoned-history", task_id="task", goal="goal"))

    assert calls == []
    assert path.read_bytes() == before
    assert not list((tmp_path / ".curation_invocations").glob("*.json"))


@pytest.mark.parametrize("method_name,target,log_name,result", _CASES)
def test_known_v1_terminal_history_still_deduplicates_paid_finalize(
        tmp_path, monkeypatch, method_name, target, log_name, result):
    calls: list[int] = []

    def paid(*_args, **_kwargs):
        calls.append(1)
        return result

    monkeypatch.setattr(target, paid)
    path = tmp_path / log_name
    path.write_text(json.dumps({
        "run_id": "legacy-terminal",
        "task_id": "task",
        "outcome": "proposed",
        "auto": False,
        "auto_requested": False,
        "proposals": {},
        "receipt": None,
    }) + "\n", encoding="utf-8")
    before = path.read_bytes()

    getattr(_memory(tmp_path), method_name)(
        RunState(run_id="legacy-terminal", task_id="task", goal="goal"))

    assert calls == []
    assert path.read_bytes() == before
    assert not list((tmp_path / ".curation_invocations").glob("*.json"))


@pytest.mark.parametrize("method_name,target,log_name,result", _CASES)
def test_finalize_steward_lost_terminal_receipt_is_not_rebilled(
        tmp_path, monkeypatch, method_name, target, log_name, result):
    calls: list[int] = []

    def paid(*_args, **_kwargs):
        calls.append(1)
        return result

    monkeypatch.setattr(target, paid)
    memory = _memory(tmp_path)
    final = RunState(run_id="run-paid-once", task_id="task", goal="goal")
    append = memory._append_curation_once

    def process_loss(*_args, **_kwargs):
        raise SystemExit("simulated hard loss after provider return")

    monkeypatch.setattr(memory, "_append_curation_once", process_loss)
    with pytest.raises(SystemExit):
        getattr(memory, method_name)(final)
    assert calls == [1]
    assert len(list((tmp_path / ".curation_invocations").glob("*.json"))) == 1

    monkeypatch.setattr(memory, "_append_curation_once", append)
    getattr(memory, method_name)(final)

    assert calls == [1]
    rows = [json.loads(line) for line in (tmp_path / log_name).read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "prior_attempt_incomplete_not_replayed"
    assert rows[0]["ambiguity"] == "provider_outcome_unknown"


@pytest.mark.parametrize("method_name,target,_log_name,result", _CASES)
def test_finalize_steward_requires_durable_claim_before_provider(
        tmp_path, monkeypatch, method_name, target, _log_name, result):
    import looplab.core.atomicio as atomicio_module

    calls: list[int] = []

    def paid(*_args, **_kwargs):
        calls.append(1)
        return result

    monkeypatch.setattr(target, paid)
    monkeypatch.setattr(
        atomicio_module, "strict_fsync",
        lambda _fileno: (_ for _ in ()).throw(OSError("durability unavailable")),
    )

    getattr(_memory(tmp_path), method_name)(
        RunState(run_id="run-no-sync", task_id="task", goal="goal"))

    assert calls == []


@pytest.mark.parametrize("method_name,target,log_name,_result", _CASES)
def test_finalize_steward_records_provider_failure_as_error_not_empty(
        tmp_path, monkeypatch, method_name, target, log_name, _result):
    calls: list[int] = []

    def failed_provider(*_args, **kwargs):
        calls.append(1)
        assert kwargs.get("raise_on_failure") is True
        raise RuntimeError("provider detail must not enter durable memory")

    monkeypatch.setattr(target, failed_provider)
    getattr(_memory(tmp_path), method_name)(
        RunState(run_id="run-provider-error", task_id="task", goal="goal"))

    assert calls == [1]
    rows = [json.loads(line) for line in (tmp_path / log_name).read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "error"
    assert rows[0]["error_type"] == "RuntimeError"
    assert "provider detail" not in json.dumps(rows[0])


def test_concurrent_finalize_stewards_are_single_flight(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    counter_lock = Lock()
    calls: list[int] = []

    def paid(*_args, **_kwargs):
        with counter_lock:
            calls.append(1)
        entered.set()
        assert release.wait(5)
        return {"merges": [], "splits": [], "purges": []}

    monkeypatch.setattr("looplab.engine.concept_steward.propose_concept_curation", paid)
    first_final = RunState(
        run_id="run-concurrent-a", task_id="task", goal="goal", last_finish_seq=10)
    second_final = RunState(
        run_id="run-concurrent-b", task_id="task", goal="goal", last_finish_seq=20)
    first_memory = _memory(tmp_path)
    second_memory = _memory(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_memory.store_concept_curation, first_final)
        assert entered.wait(5)
        second = executor.submit(second_memory.store_concept_curation, second_final)
        release.set()
        first.result(timeout=10)
        second.result(timeout=10)

    assert calls == [1]
    rows = (Path(tmp_path) / "concept_curation_log.jsonl").read_text().splitlines()
    assert len(rows) == 1


@pytest.mark.parametrize("fast_path", ["empty", "already-governed"])
def test_facets_fast_terminal_cannot_discard_inflight_paid_result(
        tmp_path, monkeypatch, fast_path):
    entered, release, contender_waiting = Event(), Event(), Event()
    calls: list[int] = []

    def paid(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {"domain": "paid-result"}

    monkeypatch.setattr("looplab.engine.task_facets.propose_task_facets", paid)
    first_memory = _memory(tmp_path)
    second_memory = _memory(tmp_path)
    original_decision_lock = second_memory._curation_decision_lock

    @contextmanager
    def observed_decision_lock(*args, **kwargs):
        contender_waiting.set()
        with original_decision_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(second_memory, "_curation_decision_lock", observed_decision_lock)
    paid_final = RunState(
        run_id="run-paid", task_id="same-task", goal="classify records",
        last_finish_seq=41)
    if fast_path == "empty":
        contender_final = RunState(
            run_id="run-empty", task_id="same-task", goal="   ", last_finish_seq=42)
    else:
        contender_final = RunState(
            run_id="run-governed", task_id="same-task", goal="changed goal",
            last_finish_seq=43)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_memory.store_task_facets, paid_final)
        assert entered.wait(5)
        if fast_path == "already-governed":
            record_task_facets(
                str(tmp_path), task_id="same-task", facets={"domain": "operator"}, by="operator")
        second = executor.submit(second_memory.store_task_facets, contender_final)
        assert contender_waiting.wait(5)
        release.set()
        first.result(timeout=10)
        second.result(timeout=10)

    assert calls == [1]
    rows = [
        json.loads(line)
        for line in (Path(tmp_path) / "task_facets_curation_log.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "proposed"
    assert rows[0]["proposals"]["facets"] == {"domain": "paid-result"}
