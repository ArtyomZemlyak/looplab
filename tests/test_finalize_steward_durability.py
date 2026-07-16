from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from looplab.core.models import RunState
from looplab.engine.lessons import LessonMemory


def _memory(tmp_path) -> LessonMemory:
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
        "looplab.engine.concept_steward.steward_concepts",
        "concept_curation_log.jsonl",
        {"proposals": {"merges": [], "splits": [], "purges": []}, "receipt": None},
    ),
    (
        "store_claim_curation",
        "looplab.engine.claim_steward.steward_claims",
        "claim_curation_log.jsonl",
        {"proposals": {"decisions": []}, "receipt": None},
    ),
    (
        "store_task_facets",
        "looplab.engine.task_facets.propose_task_facets",
        "task_facets_curation_log.jsonl",
        {},
    ),
]


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


def test_concurrent_finalize_stewards_are_single_flight(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    counter_lock = Lock()
    calls: list[int] = []

    def paid(*_args, **_kwargs):
        with counter_lock:
            calls.append(1)
        entered.set()
        assert release.wait(5)
        return {"proposals": {"merges": [], "splits": [], "purges": []}, "receipt": None}

    monkeypatch.setattr("looplab.engine.concept_steward.steward_concepts", paid)
    final = RunState(run_id="run-concurrent", task_id="task", goal="goal")
    first_memory = _memory(tmp_path)
    second_memory = _memory(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_memory.store_concept_curation, final)
        assert entered.wait(5)
        second = executor.submit(second_memory.store_concept_curation, final)
        release.set()
        first.result(timeout=10)
        second.result(timeout=10)

    assert calls == [1]
    rows = (Path(tmp_path) / "concept_curation_log.jsonl").read_text().splitlines()
    assert len(rows) == 1
