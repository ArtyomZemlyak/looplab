from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _terminal_store(run_dir: Path, *, marked: bool = False) -> tuple[EventStore, int]:
    run_dir.mkdir(parents=True, exist_ok=True)
    store = EventStore(run_dir / "events.jsonl")
    store.append(
        "run_started",
        {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"},
    )
    finished = store.append(
        "run_finished", {"reason": "done", "finalization_required": True})
    if marked:
        store.append("finalization_finished", {"finish_seq": finished.seq})
    (run_dir / "task.snapshot.json").write_text("{}", encoding="utf-8")
    return store, finished.seq


class _EngineStub:
    def __init__(self, run_dir: Path, *, run_error: Exception | None = None):
        self.run_dir = run_dir
        self.store = EventStore(run_dir / "events.jsonl")
        self.archive_resolution = 1.0
        self.researcher = None
        self.developer = None
        self.run_error = run_error
        self.case_calls = 0
        self.reflection_calls = 0
        self.finish_calls = 0

    async def run(self):
        if self.run_error is not None:
            raise self.run_error
        return fold(self.store.read_all())

    def _finish_with_report_if_quiescent(self, state, data, *, after_seq):
        self.finish_calls += 1
        event = self.store.append(
            "run_finished",
            {**data, "after_seq": after_seq, "finalization_required": True},
        )
        return fold(self.store.read_all()).last_finish_seq == event.seq

    def _store_case(self, _state):
        self.case_calls += 1

    def _write_reflection_note(self, _state):
        self.reflection_calls += 1


def test_finalize_retries_only_missing_steps_for_exact_finish(tmp_path):
    from looplab.engine.finalize import finalize_run

    run_dir = tmp_path / "run"
    store, finish_seq = _terminal_store(run_dir)
    # Simulate a crash after the first durable step but before archive/case/reflection/marker.
    store.append("budget", {"nodes": 0, "finish_seq": finish_seq})
    eng = _EngineStub(run_dir)

    first = finalize_run(eng, entry_finished=True, start_time=0.0)
    assert first.finished and not first.finalization_pending()
    events = eng.store.read_all()
    assert sum(e.type == "budget" and e.data.get("finish_seq") == finish_seq for e in events) == 1
    assert sum(
        e.type == "diversity_archive" and e.data.get("finish_seq") == finish_seq
        for e in events
    ) == 1, [(e.type, e.data) for e in events]
    assert sum(
        e.type == "finalization_finished" and e.data.get("finish_seq") == finish_seq
        for e in events
    ) == 1

    before = [(e.type, e.data) for e in events]
    second = finalize_run(eng, entry_finished=True, start_time=0.0)
    assert not second.finalization_pending()
    assert [(e.type, e.data) for e in eng.store.read_all()] == before
    assert eng.case_calls == 1 and eng.reflection_calls == 1


def test_llm_cost_append_failure_keeps_marker_pending_then_recovers_once(
        tmp_path, monkeypatch):
    from looplab.engine.finalize import finalize_run

    run_dir = tmp_path / "run"
    _store, finish_seq = _terminal_store(run_dir)
    eng = _EngineStub(run_dir)

    class _Accountant:
        calls = 1
        spent = 0.25
        prompt_tokens = 10
        completion_tokens = 5
        total_tokens = 15

    class _Role:
        accountant = _Accountant()

    eng.researcher = _Role()
    real_append = eng.store.append
    failed = []

    def _fail_cost_once(event_type, data, **kwargs):
        if event_type == "llm_cost" and not failed:
            failed.append(True)
            raise OSError("injected llm-cost append failure")
        return real_append(event_type, data, **kwargs)

    monkeypatch.setattr(eng.store, "append", _fail_cost_once)
    first = finalize_run(eng, entry_finished=False, start_time=0.0)
    first_events = eng.store.read_all()
    assert failed == [True]
    assert first.finalization_pending()
    assert not any(e.type == "llm_cost" for e in first_events)
    assert not any(e.type == "finalization_finished" for e in first_events)

    monkeypatch.setattr(eng.store, "append", real_append)
    second = finalize_run(eng, entry_finished=True, start_time=0.0)
    events = eng.store.read_all()
    assert not second.finalization_pending()
    assert sum(
        e.type == "llm_cost" and e.data.get("finish_seq") == finish_seq
        for e in events
    ) == 1
    assert sum(
        e.type == "finalization_finished" and e.data.get("finish_seq") == finish_seq
        for e in events
    ) == 1
    assert sum(
        e.type == "budget" and e.data.get("finish_seq") == finish_seq
        for e in events
    ) == 1
    assert sum(
        e.type == "diversity_archive" and e.data.get("finish_seq") == finish_seq
        for e in events
    ) == 1


def test_derived_projection_failures_are_atomic_and_non_terminal(tmp_path, monkeypatch):
    import looplab.engine.finalize as mod

    run_dir = tmp_path / "run"
    store, _finish_seq = _terminal_store(run_dir)
    readmodel = run_dir / "readmodel.sqlite"
    readmodel.write_bytes(b"known-good")
    eng = _EngineStub(run_dir)

    def _broken_readmodel(_events, temp_path):
        Path(temp_path).write_bytes(b"partial")
        raise OSError("sqlite unavailable")

    trace_attempts = []
    tree_attempts = []
    monkeypatch.setattr(mod, "build_readmodel", _broken_readmodel)
    monkeypatch.setattr(
        mod, "atomic_write_bytes",
        lambda *_a, **_k: trace_attempts.append(True) or (_ for _ in ()).throw(OSError("trace")),
    )
    monkeypatch.setattr(
        mod, "atomic_write_text",
        lambda *_a, **_k: tree_attempts.append(True) or (_ for _ in ()).throw(OSError("tree")),
    )

    state = mod.finalize_run(eng, entry_finished=False, start_time=0.0)
    events = store.read_all()
    assert state.finished
    assert readmodel.read_bytes() == b"known-good"
    assert trace_attempts and tree_attempts  # independent best-effort publications
    assert sum(e.type == "run_finished" for e in events) == 1
    assert any(e.type == "readmodel_skipped" for e in events)
    assert not fold(events).finalization_pending()


def test_guarded_exception_uses_common_finish_and_durable_finalize(tmp_path):
    from looplab.cli.run_cmds import _run_engine_guarded

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    store.append(
        "run_started",
        {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"},
    )
    root = ValueError("root failure")
    eng = _EngineStub(run_dir, run_error=root)

    with pytest.raises(ValueError) as raised:
        _run_engine_guarded(eng)
    assert raised.value is root
    events = eng.store.read_all()
    finishes = [e for e in events if e.type == "run_finished"]
    assert len(finishes) == 1 and finishes[0].data["reason"] == "error"
    assert eng.finish_calls == 1
    state = fold(events)
    assert state.finished and not state.finalization_pending()
    assert state.finalized_finish_seq == finishes[0].seq


def test_guarded_exception_refolds_after_one_lost_finish_cas(tmp_path, monkeypatch):
    from looplab.cli.run_cmds import _run_engine_guarded
    from looplab.events.eventstore import EventStoreConcurrencyError

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    store.append(
        "run_started",
        {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"},
    )
    root = RuntimeError("fatal")
    eng = _EngineStub(run_dir, run_error=root)
    monkeypatch.setattr(eng, "_finish_with_report_if_quiescent", lambda *_a, **_k: False)
    real_append = eng.store.append
    lost = []

    def _append(event_type, data, **kwargs):
        if event_type == "run_finished" and not lost:
            lost.append(True)
            expected = kwargs.get("expected_last_seq", -1)
            raise EventStoreConcurrencyError(eng.store.path, expected, expected + 1)
        return real_append(event_type, data, **kwargs)

    monkeypatch.setattr(eng.store, "append", _append)
    with pytest.raises(RuntimeError) as raised:
        _run_engine_guarded(eng)

    assert raised.value is root and lost == [True]
    state = fold(eng.store.read_all())
    assert state.finished and not state.finalization_pending()


def _install_cli_engine(monkeypatch, run_dir: Path):
    from looplab import cli
    import looplab.cli.run_cmds as cmds

    eng = _EngineStub(run_dir)
    monkeypatch.setattr(cli, "_engine", lambda *_a, **_k: eng)
    monkeypatch.setattr(cmds, "_load_task", lambda _p: object())
    return eng


def test_cli_finalize_repairs_pending_finish_without_resume_or_new_finish(tmp_path, monkeypatch):
    import looplab.cli.run_cmds as cmds

    run_dir = tmp_path / "run"
    store, finish_seq = _terminal_store(run_dir)
    eng = _install_cli_engine(monkeypatch, run_dir)

    cmds.finalize(run_dir)

    events = store.read_all()
    assert not any(e.type in ("resume", "run_abort") for e in events)
    assert [e.seq for e in events if e.type == "run_finished"] == [finish_seq]
    state = fold(events)
    assert not state.finalization_pending() and state.search_epoch == 0
    assert eng.finish_calls == 0


def test_cli_finalize_fully_complete_is_a_pure_noop(tmp_path):
    import looplab.cli.run_cmds as cmds

    run_dir = tmp_path / "run"
    store, _ = _terminal_store(run_dir, marked=True)
    before = [(e.type, e.data) for e in store.read_all()]

    cmds.finalize(run_dir)

    assert [(e.type, e.data) for e in store.read_all()] == before
    state = fold(store.read_all())
    assert state.last_stop_request_seq <= state.last_finish_seq
    assert not state.finalization_pending()


def test_cli_finalize_serves_preexisting_request_on_finished_without_search(tmp_path, monkeypatch):
    import looplab.cli.run_cmds as cmds

    run_dir = tmp_path / "run"
    store, old_finish_seq = _terminal_store(run_dir, marked=True)
    stop = store.append("run_abort", {"reason": "finalized"})  # server already recorded the request
    store.append("resume_requested", {})
    eng = _install_cli_engine(monkeypatch, run_dir)

    cmds.finalize(run_dir)

    events = store.read_all()
    assert [e.seq for e in events if e.type == "run_abort"] == [stop.seq]
    finishes = [e for e in events if e.type == "run_finished"]
    assert len(finishes) == 1 and finishes[0].seq == old_finish_seq
    assert not any(e.type == "resume" for e in events)
    state = fold(events)
    assert state.last_finish_seq == old_finish_seq
    assert not state.resume_pending()
    assert not state.finalization_pending()
    assert state.search_epoch == 0
    assert eng.finish_calls == 0


def test_cli_finalize_accepts_explicit_task_file_for_legacy_run(tmp_path, monkeypatch):
    import looplab.cli.run_cmds as cmds

    run_dir = tmp_path / "run"
    store, _ = _terminal_store(run_dir)
    (run_dir / "task.snapshot.json").unlink()
    legacy_task = tmp_path / "legacy-task.json"
    legacy_task.write_text("{}", encoding="utf-8")
    eng = _EngineStub(run_dir)
    loaded = []
    from looplab import cli
    monkeypatch.setattr(cli, "_engine", lambda *_a, **_k: eng)
    monkeypatch.setattr(cmds, "_load_task", lambda path: loaded.append(path) or object())

    cmds.finalize(run_dir, task_file=legacy_task)

    assert loaded == [legacy_task]
    assert not fold(store.read_all()).finalization_pending()


def test_direct_cli_resume_waits_for_finished_owner_tail(tmp_path, monkeypatch):
    import looplab.cli.run_cmds as cmds

    run_dir = tmp_path / "run"
    store, _ = _terminal_store(run_dir, marked=True)
    eng = _install_cli_engine(monkeypatch, run_dir)
    attempts = []

    @contextmanager
    def _singleton(_run_dir):
        attempts.append(True)
        yield len(attempts) > 1

    sleeps = []
    guarded = []
    monkeypatch.setattr(cmds, "_engine_singleton", _singleton)
    monkeypatch.setattr(cmds.time, "sleep", lambda delay: sleeps.append(delay))
    monkeypatch.setattr(
        cmds, "_run_engine_guarded",
        lambda _eng: guarded.append(True) or fold(_eng.store.read_all()),
    )
    monkeypatch.setattr(cmds, "_print_result", lambda _state: None)

    cmds.resume(run_dir)

    events = store.read_all()
    assert len(attempts) == 2 and sleeps == [0.05]
    assert guarded == [True]
    assert sum(e.type == "resume" for e in events) == 1
    assert not fold(events).finished
    assert eng.store.path == store.path
