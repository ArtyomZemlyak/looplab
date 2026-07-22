from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

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
        self.research_claim_calls = 0
        self._cross_run_concepts = False
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

    def _store_research_claims(self, _state):
        self.research_claim_calls += 1

    # Keep the rest of the real Engine's optional finalization surface explicit on the stub. Concepts and
    # curation are disabled here; these methods ensure a future gate change fails in the test body rather
    # than as an unrelated missing-attribute error.
    def _store_concept_capsule(self, _state):
        pass

    def _store_concept_curation(self, _state):
        pass

    def _store_claim_curation(self, _state):
        pass

    def _store_task_facets(self, _state):
        pass

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
    # D8 persistence is independent of the concept-capsule flag: concepts are off on this stub, but the
    # first-class research projection is still written exactly once with the case step.
    assert eng.case_calls == 1 and eng.research_claim_calls == 1 and eng.reflection_calls == 1


def test_finalize_reflects_before_stewards_and_counts_stewards_before_cost(tmp_path, monkeypatch):
    import looplab.engine.finalize as finalize_module

    run_dir = tmp_path / "ordered-finalize"
    _terminal_store(run_dir)
    eng = _EngineStub(run_dir)
    eng._cross_run_curation = True
    order: list[str] = []

    eng._write_reflection_note = lambda _state: order.append("reflection")
    eng._store_concept_curation = lambda _state: order.append("concept")

    def claim(_state):
        assert order and order[0] == "reflection"
        order.append("claim")

    eng._store_claim_curation = claim
    eng._store_task_facets = lambda _state: order.append("facets")

    def cost(*_args, **_kwargs):
        order.append("llm_cost")
        return True

    monkeypatch.setattr(finalize_module, "emit_llm_cost", cost)
    finalize_module.finalize_run(eng, entry_finished=True, start_time=0.0)

    assert order == ["reflection", "concept", "claim", "facets", "llm_cost"]


def test_upgrade_refreshes_old_cost_rollup_after_new_steward_usage(tmp_path):
    """A pre-steward cost marker must not hide usage appended by upgraded finalization."""
    from looplab.engine.finalize import finalize_run

    run_dir = tmp_path / "upgraded-cost-order"
    store, finish_seq = _terminal_store(run_dir)
    scope = f"finish:{finish_seq}"
    store.append("llm_cost", {
        "cost": 0.0, "calls": 0, "prompt_tokens": 0,
        "completion_tokens": 0, "total_tokens": 0,
        "finalize_scope": scope, "finish_seq": finish_seq,
    })
    store.append("finalize_step", {"scope": scope, "step": "llm_cost"})

    eng = _EngineStub(run_dir)
    eng._cross_run_curation = True

    def append_new_usage(_state):
        eng.store.append("llm_usage", {
            "usage_id": "upgraded-steward-usage", "cost": 0.125, "calls": 1,
            "prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12,
        })

    eng._store_concept_curation = append_new_usage
    final = finalize_run(eng, entry_finished=True, start_time=0.0)

    events = eng.store.read_all()
    usage = next(event for event in events
                 if event.type == "llm_usage" and event.data.get("usage_id") == "upgraded-steward-usage")
    rollups = [event for event in events if event.type == "llm_cost"]
    assert len(rollups) == 2
    assert rollups[-1].seq > usage.seq
    assert rollups[-1].data == {
        "cost": 0.125, "calls": 1, "prompt_tokens": 8,
        "completion_tokens": 4, "total_tokens": 12,
        "finalize_scope": scope, "finish_seq": finish_seq,
    }
    assert final.llm_cost["cost"] == pytest.approx(0.125)
    assert not final.finalization_pending()


def test_engine_reentry_skips_setup_for_finish_seq_pending(tmp_path, monkeypatch):
    import anyio
    from looplab import cli
    from looplab.adapters.tasks import validate_task
    from looplab.core.config import Settings

    run_dir = tmp_path / "run"
    store, finish_seq = _terminal_store(run_dir)
    task = validate_task({
        "id": "t", "kind": "quadratic", "goal": "g", "direction": "min",
        "bounds": {"x": [-1.0, 1.0]},
    })
    eng = cli._engine(run_dir, task, Settings(max_nodes=1), crash_after=None)
    monkeypatch.setattr(
        eng, "_setup_phase",
        lambda _state: pytest.fail("setup must not run before pending finish recovery"),
    )

    state = anyio.run(eng.run)

    assert state.finished and not state.finalization_pending()
    markers = [event for event in store.read_all()
               if event.type == "finalization_finished"]
    assert len(markers) == 1 and markers[0].data.get("finish_seq") == finish_seq


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
    def _capture_engine(_out, _task, settings, crash_after=None, **_kwargs):
        eng.settings = settings
        return eng
    monkeypatch.setattr(cli, "_engine", _capture_engine)
    monkeypatch.setattr(cmds, "_load_task", lambda _p: object())
    return eng


def test_cli_finalize_repairs_pending_finish_without_resume_or_new_finish(tmp_path, monkeypatch):
    import looplab.cli.run_cmds as cmds

    run_dir = tmp_path / "run"
    store, finish_seq = _terminal_store(run_dir)
    (run_dir / "config.snapshot.json").write_text("{}", encoding="utf-8")
    eng = _install_cli_engine(monkeypatch, run_dir)

    cmds.finalize(run_dir)

    events = store.read_all()
    assert not any(e.type in ("resume", "run_abort") for e in events)
    assert [e.seq for e in events if e.type == "run_finished"] == [finish_seq]
    state = fold(events)
    assert not state.finalization_pending() and state.search_epoch == 0
    assert eng.finish_calls == 0
    assert eng.settings.train_monitor is False and eng.settings.asha_live is False
    assert eng.settings.concurrent_research_repeat is False
    assert eng.settings.concurrent_consolidate is False


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
    snapshot = run_dir / "config.snapshot.json"
    snapshot.write_text("{}", encoding="utf-8")
    snapshot_before = snapshot.read_bytes()
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
    assert snapshot.read_bytes() == snapshot_before
    assert eng.settings.train_monitor is False and eng.settings.asha_live is False
    assert eng.settings.watchdog_reflection is False
    assert eng.settings.concurrent_research_repeat is False
    assert eng.settings.concurrent_consolidate is False


@pytest.mark.parametrize("entry", ["run", "resume"])
def test_cli_entry_repairs_finish_seq_pending_without_reopening_search(tmp_path, entry):
    from typer.testing import CliRunner
    from looplab.cli import app

    run_dir = tmp_path / f"pending-{entry}"
    run_dir.mkdir()
    task = tmp_path / f"{entry}-task.json"
    task_doc = (
        '{"id":"t","kind":"quadratic","goal":"g","direction":"min",'
        '"bounds":{"x":[-1,1]}}'
    )
    task.write_text(task_doc, encoding="utf-8")
    (run_dir / "task.snapshot.json").write_text(task_doc, encoding="utf-8")
    (run_dir / "config.snapshot.json").write_text("{}", encoding="utf-8")
    store = EventStore(run_dir / "events.jsonl")
    store.append(
        "run_started", {"run_id": run_dir.name, "task_id": "t", "goal": "g",
                        "direction": "min"})
    store.append("run_finished", {"reason": "done", "finalization_required": True})

    args = (["run", str(task), "--out", str(run_dir)]
            if entry == "run" else ["resume", str(run_dir)])
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    events = store.read_all()
    assert not any(event.type in {"resume", "run_reopened"} for event in events)
    state = fold(events)
    assert state.finished and not state.finalization_pending() and state.search_epoch == 0


@pytest.mark.parametrize("protocol", ["finish_seq", "scoped"])
def test_cli_run_pending_finalize_preserves_and_uses_original_snapshots(
        tmp_path, monkeypatch, protocol):
    """Same-id changed inputs belong to a later epoch, never an already-accepted terminal wrap-up."""
    from typer.testing import CliRunner
    from looplab import cli
    from looplab.cli import app
    import looplab.cli.run_cmds as cmds
    from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings

    run_dir = tmp_path / f"pending-{protocol}"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    store.append(
        "run_started", {"run_id": run_dir.name, "task_id": "t", "goal": "old goal",
                        "direction": "min"})
    finish_data = {"reason": "done"}
    if protocol == "finish_seq":
        finish_data["finalization_required"] = True
    else:
        finish_data["finalize_scope"] = "finish:original"
    store.append("run_finished", finish_data)

    old_task = {
        "id": "t", "kind": "quadratic", "goal": "old goal", "direction": "min",
        "bounds": {"x": [-1.0, 1.0]},
    }
    new_task = {**old_task, "goal": "new goal"}
    task_snap = run_dir / "task.snapshot.json"
    config_snap = run_dir / "config.snapshot.json"
    task_snap.write_text(json.dumps(old_task, indent=2), encoding="utf-8")
    legacy_config = Settings(max_nodes=3).masked_snapshot()
    for field in LEGACY_CONFIG_SNAPSHOT_DEFAULTS:
        legacy_config.pop(field, None)
    config_snap.write_text(json.dumps(legacy_config, indent=2), encoding="utf-8")
    task_before, config_before = task_snap.read_bytes(), config_snap.read_bytes()
    new_task_file = tmp_path / f"new-{protocol}.json"
    new_task_file.write_text(json.dumps(new_task), encoding="utf-8")

    captured = {}

    def fake_engine(run_path, task, settings, crash_after):
        captured.update(
            run_path=run_path, task=task, settings=settings, crash_after=crash_after)
        return SimpleNamespace(store=EventStore(run_path / "events.jsonl"))

    monkeypatch.setattr(cli, "_engine", fake_engine)
    monkeypatch.setattr(
        cmds, "_run_engine_guarded", lambda eng: fold(eng.store.read_all()))
    monkeypatch.setattr(cmds, "_print_result", lambda _state: None)

    result = CliRunner().invoke(
        app, ["run", str(new_task_file), "--out", str(run_dir), "--max-nodes", "9"])

    assert result.exit_code == 0, result.output
    assert task_snap.read_bytes() == task_before
    assert config_snap.read_bytes() == config_before
    assert captured["run_path"] == run_dir
    assert captured["task"].id == "t" and captured["task"].goal == "old goal"
    assert captured["settings"].max_nodes == 3
    assert captured["settings"].train_monitor is False
    assert captured["settings"].asha_live is False
    assert captured["settings"].watchdog_reflection is False
    assert captured["settings"].card_driven_selection is False
    assert captured["settings"].concurrent_research_repeat is False
    assert captured["settings"].concurrent_consolidate is False
    assert captured["settings"].eval_parallel is None
    assert captured["settings"].llm_parallel is None
    assert captured["crash_after"] is None


def test_cli_run_pending_finalize_refuses_missing_original_snapshot_before_writes(
        tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from looplab import cli
    from looplab.cli import app

    run_dir = tmp_path / "pending-missing-config"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    store.append(
        "run_started", {"run_id": run_dir.name, "task_id": "t", "goal": "old goal",
                        "direction": "min"})
    store.append("run_finished", {"reason": "done", "finalization_required": True})
    old_task = {
        "id": "t", "kind": "quadratic", "goal": "old goal", "direction": "min",
        "bounds": {"x": [-1.0, 1.0]},
    }
    new_task = {**old_task, "goal": "new goal"}
    task_snap = run_dir / "task.snapshot.json"
    task_snap.write_text(json.dumps(old_task, indent=2), encoding="utf-8")
    task_before = task_snap.read_bytes()
    events_before = (run_dir / "events.jsonl").read_bytes()
    new_task_file = tmp_path / "new-missing-config.json"
    new_task_file.write_text(json.dumps(new_task), encoding="utf-8")
    monkeypatch.setattr(
        cli, "_engine", lambda *_args, **_kwargs: pytest.fail("Engine must not be constructed"))

    result = CliRunner().invoke(app, ["run", str(new_task_file), "--out", str(run_dir)])

    assert result.exit_code == 2
    assert task_snap.read_bytes() == task_before
    assert not (run_dir / "config.snapshot.json").exists()
    assert (run_dir / "events.jsonl").read_bytes() == events_before


def test_cli_run_engine_construction_failure_preserves_existing_snapshots(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from looplab import cli
    from looplab.cli import app
    from looplab.core.config import Settings

    run_dir = tmp_path / "finished-constructor-failure"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    store.append(
        "run_started", {"run_id": run_dir.name, "task_id": "t", "goal": "old goal",
                        "direction": "min"})
    store.append("run_finished", {"reason": "done"})  # legacy-complete: ordinary reopen path
    old_task = {
        "id": "t", "kind": "quadratic", "goal": "old goal", "direction": "min",
        "bounds": {"x": [-1.0, 1.0]},
    }
    new_task = {**old_task, "goal": "new goal"}
    task_snap = run_dir / "task.snapshot.json"
    config_snap = run_dir / "config.snapshot.json"
    task_snap.write_text(json.dumps(old_task, indent=2), encoding="utf-8")
    config_snap.write_text(
        json.dumps(Settings(max_nodes=3).masked_snapshot(), indent=2), encoding="utf-8")
    task_before, config_before = task_snap.read_bytes(), config_snap.read_bytes()
    events_before = (run_dir / "events.jsonl").read_bytes()
    new_task_file = tmp_path / "new-constructor-failure.json"
    new_task_file.write_text(json.dumps(new_task), encoding="utf-8")

    def fail_engine(*_args, **_kwargs):
        raise RuntimeError("role initialization failed")

    monkeypatch.setattr(cli, "_engine", fail_engine)
    result = CliRunner().invoke(
        app, ["run", str(new_task_file), "--out", str(run_dir), "--max-nodes", "9"])

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert task_snap.read_bytes() == task_before
    assert config_snap.read_bytes() == config_before
    assert (run_dir / "events.jsonl").read_bytes() == events_before


def test_scoped_error_finish_converges_and_does_not_loop(tmp_path):
    """R8-A1 (P1, reproduced): a SCOPED error finish (a begun marker whose finish_data has
    reason='error' plus a run_finished reason=error+finalize_scope) must CONVERGE. Before the fix,
    _recover_scoped_terminal re-appended a duplicate error run_finished on every resume forever
    (the scope never gained a complete/abandoned marker), violating invariants #2/#3."""
    import looplab.engine.finalize as mod
    from looplab.engine.finalize import incomplete_finalize_scope

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    (run_dir / "task.snapshot.json").write_text("{}", encoding="utf-8")
    scope = "finalize:deadbeef00000000"
    begun = store.append("finalize_step", {
        "scope": scope, "step": "begun",
        "finish_data": {"reason": "error", "error": "boom"},
        "finish_report_planned": False})
    store.append("run_finished", {
        "reason": "error", "error": "boom", "finalize_scope": scope,
        "after_seq": begun.seq, "finalization_required": True})
    eng = _EngineStub(run_dir)

    baseline = sum(e.type == "run_finished" for e in store.read_all())
    for _ in range(5):                          # each iteration models one resume
        mod.finalize_run(eng, entry_finished=True, start_time=0.0)
    events = store.read_all()
    # No duplicate terminals appended across resumes; the scope is closed and no longer pending.
    assert sum(e.type == "run_finished" for e in events) == baseline
    assert incomplete_finalize_scope(events) is None
    st = fold(events)
    assert st.finished and not st.finalization_pending()
