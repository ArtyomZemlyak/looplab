"""The 3-verb operator control model: STOP (freeze, no finalization), FINALIZE (stop + wrap-up:
report / cross-run lessons+case / cost roll-up), RESUME (continue from any stopped state). Replaces
the pause/abort/resume/reopen tangle — pause≡stop, abort≡finalize, reopen≡resume."""
from __future__ import annotations

import itertools
from contextlib import contextmanager
from pathlib import Path

from looplab.core.models import Event
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _ev(t, **d):
    return Event(seq=next(_ev.c), ts=0.0, type=t, data=d)


_ev.c = itertools.count()


def _base():
    return [_ev("run_started", run_id="r", task_id="t", goal="g", direction="min")]


# ------------------------------------------------------------------ fold semantics
def test_stop_freezes_not_finished():
    """STOP (pause) freezes: paused, NOT finished, no stop_requested — so finalize.py (gated on
    `finished`) SKIPS the wrap-up. This is the whole point of stop vs finalize."""
    s = fold(_base() + [_ev("pause")])
    assert s.paused is True and s.finished is False and s.stop_requested is None


def test_finalize_sets_stop_requested():
    """FINALIZE (run_abort) requests stop; the loop turns it into run_finished -> finalize runs
    (finished=True gates the wrap-up)."""
    s = fold(_base() + [_ev("run_abort", reason="finalized")])
    assert s.stop_requested == "finalized"
    # ... and once the loop finalizes it:
    s2 = fold(_base() + [_ev("run_abort", reason="finalized"), _ev("run_finished", reason="aborted")])
    assert s2.finished is True


def test_finalize_works_after_stop():
    """Operator STOPs, then later FINALIZEs the frozen run: both flags stand, so re-entering the loop
    wraps it up."""
    s = fold(_base() + [_ev("pause"), _ev("run_abort", reason="finalized")])
    assert s.paused is True and s.stop_requested == "finalized"


def test_resume_lifts_every_stopped_state():
    """RESUME is the one 'continue' — it clears paused AND finished AND stop_requested, whether the run
    was stopped, finalized, or naturally finished."""
    # from STOP
    assert fold(_base() + [_ev("pause"), _ev("resume")]).paused is False
    # from FINALIZE (aborted -> finished)
    s = fold(_base() + [_ev("run_abort", reason="x"), _ev("run_finished", reason="aborted"), _ev("resume")])
    assert s.finished is False and s.stop_requested is None
    # from a NATURAL finish
    s = fold(_base() + [_ev("run_finished", reason="budget"), _ev("resume")])
    assert s.finished is False
    # legacy reopen folds identically to resume (back-compat)
    assert fold(_base() + [_ev("run_finished"), _ev("run_reopened")]).finished is False


# ------------------------------------------------------------------ boss action mapping
def test_boss_action_mapping():
    from looplab.serve.routers.boss import _action_to_control, _Action
    from looplab.core.models import RunState
    st = RunState()
    def m(action):
        return _action_to_control(_Action(action=action), st)["type"]
    assert m("stop") == "pause"          # freeze
    assert m("finalize") == "run_abort"  # wrap up
    assert m("resume") == "resume"       # continue
    assert m("pause") == "pause"         # alias of stop
    assert m("abort") == "run_abort"     # alias of finalize
    # finalize carries a reason; stop/resume don't
    assert _action_to_control(_Action(action="finalize"), st)["data"] == {"reason": "finalized"}
    assert _action_to_control(_Action(action="stop"), st)["data"] == {}


# ------------------------------------------------------------------ CLI verbs
def _run_dir(tmp_path: Path) -> Path:
    rd = tmp_path / "run"
    rd.mkdir()
    es = EventStore(rd / "events.jsonl")
    es.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    # a minimal snapshot so `finalize`/`resume` can build an engine if they need to (they won't here)
    (rd / "task.snapshot.json").write_text('{"kind":"quadratic","goal":"g","direction":"min"}',
                                           encoding="utf-8")
    return rd


def test_cli_stop_appends_pause(tmp_path):
    from looplab import cli
    rd = _run_dir(tmp_path)
    cli.stop(rd)
    evs = EventStore(rd / "events.jsonl").read_all()
    assert evs[-1].type == "pause"                    # stop = freeze
    assert fold(evs).paused is True and fold(evs).finished is False


def test_cli_finalize_appends_run_abort(tmp_path):
    from looplab import cli
    rd = _run_dir(tmp_path)
    # no engine + a quadratic task: finalize appends run_abort; the wrap-up re-entry may or may not run
    # here, but the intent event MUST be recorded with the finalize reason.
    try:
        cli.finalize(rd)
    except SystemExit:
        pass
    types = [e.type for e in EventStore(rd / "events.jsonl").read_all()]
    assert "run_abort" in types
    ab = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "run_abort"][0]
    assert ab.data.get("reason") == "finalized"


def test_cli_resume_preserves_pending_finalize_after_error_finish(monkeypatch, tmp_path):
    """A command retry must not append resume and clear the durable stop after wrap-up errored."""
    from looplab.cli import run_cmds

    rd = _run_dir(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append("run_abort", {"reason": "finalized"})
    store.append("run_finished", {"reason": "error", "error": "report failed"})

    class FakeEngine:
        def __init__(self):
            self.store = store

    fake = FakeEngine()

    @contextmanager
    def singleton(_rd):
        yield True

    monkeypatch.setattr(run_cmds, "_load_task", lambda _path: object())
    monkeypatch.setattr(run_cmds, "_engine", lambda *_args, **_kwargs: fake)
    monkeypatch.setattr(run_cmds, "_engine_singleton", singleton)
    monkeypatch.setattr(run_cmds, "_run_engine_guarded", lambda eng: fold(eng.store.read_all()))
    monkeypatch.setattr(run_cmds, "_print_result", lambda _state: None)
    run_cmds.resume(rd, task_file=rd / "task.snapshot.json", max_nodes=None)

    events = store.read_all()
    assert [event.type for event in events].count("resume") == 0
    state = fold(events)
    assert state.finished and state.stop_requested == "finalized" and state.stop_reason == "error"


# ------------------------------------------------------------------ finalize wrap-up (live engine)
def _toy_engine(run_dir, max_nodes=4):
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.adapters.toytask import ToyTask
    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    researcher, developer = task.build_roles()
    return Engine(run_dir, task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=max_nodes),
                  max_parallel=4)


def test_finalize_wrap_up_runs_once_and_re_entry_is_idempotent(tmp_path):
    """FINALIZE runs the end-of-run wrap-up EXACTLY once. A finalize (run_abort) appended to a run that
    already reached a terminal `run_finished`, then re-entered by the engine, must be a pure NO-OP: the
    `finished` guard wins over the pending stop_requested, so there is NO second run_finished and the
    wrap-up (report / cross-run lessons+case / cost roll-up) is not duplicated. (mega-review 07-06 — the
    fold-flag tests above never drove the actual wrap-up / re-entry path.)"""
    import anyio
    rd = tmp_path / "run"
    s1 = anyio.run(_toy_engine(rd).run)
    assert s1.finished
    ev1 = list(EventStore(rd / "events.jsonl").read_all())
    assert sum(e.type == "run_finished" for e in ev1) == 1     # the wrap-up ran once
    assert (rd / "tree.html").exists()                          # a wrap-up artifact was produced
    # operator FINALIZEs the already-finished run; the engine re-enters on the log
    EventStore(rd / "events.jsonl").append("run_abort", {"reason": "finalized"})
    s2 = anyio.run(_toy_engine(rd).run)
    assert s2.finished and sorted(s2.nodes) == sorted(s1.nodes)  # no new work
    ev2 = list(EventStore(rd / "events.jsonl").read_all())
    assert sum(e.type == "run_finished" for e in ev2) == 1      # STILL one — the wrap-up did not re-run


def test_error_finished_pending_finalize_retry_reruns_wrap_up(tmp_path):
    """Unlike an ordinary completed re-entry, an errored wrap-up is incomplete and retryable."""
    import anyio

    rd = tmp_path / "error-finalize"
    eng = _toy_engine(rd)
    store = eng.store
    store.append("run_started", {"run_id": rd.name, "task_id": eng.task.id,
                                 "goal": eng.task.goal, "direction": eng.task.direction})
    store.append("run_abort", {"reason": "finalized"})
    store.append("run_finished", {"reason": "error", "error": "report failed"})

    state = anyio.run(eng.run)
    assert state.finished and state.stop_reason == "aborted"
    events = store.read_all()
    assert [event.data.get("reason") for event in events if event.type == "run_finished"] == [
        "error", "aborted"]
    assert any(event.type == "budget" for event in events)  # finalization side effects really reran


def test_begun_only_finalize_recovers_exact_terminal_without_reopening_setup(monkeypatch, tmp_path):
    """A kill between begun/finished resumes the same terminal scope, never setup or search."""
    import anyio
    import pytest

    rd = tmp_path / "begun-only-finalize"
    eng = _toy_engine(rd)
    store = eng.store
    store.append("run_started", {"run_id": rd.name, "task_id": eng.task.id,
                                 "goal": eng.task.goal, "direction": eng.task.direction})
    scope = "finalize:crash-gap"
    finish_data = {"reason": "time_budget", "winner": 7}
    real_append = store.append

    def crash_after_begun(event_type, data, *args, **kwargs):
        result = real_append(event_type, data, *args, **kwargs)
        if event_type == "finalize_step" and data.get("step") == "begun":
            raise RuntimeError("simulated hard kill after durable begun")
        return result

    store.append = crash_after_begun
    with pytest.raises(RuntimeError, match="simulated hard kill"):
        eng._finish_run(finish_data, scope=scope)
    store.append = real_append
    monkeypatch.setattr(
        eng, "_setup_phase",
        lambda _state: (_ for _ in ()).throw(AssertionError("setup reopened after terminal intent")),
    )
    monkeypatch.setattr(eng, "_store_case", lambda _state: None)
    monkeypatch.setattr(eng, "_write_reflection_note", lambda _state: None)

    state = anyio.run(eng.run)
    assert state.finished and state.stop_reason == "time_budget"
    events = store.read_all()
    finishes = [event.data for event in events if event.type == "run_finished"]
    assert finishes == [{
        **finish_data,
        "finalize_scope": scope,
        "recovered_from_finalize_begun": True,
    }]
    assert not [event for event in events if event.type == "node_created"]
    assert any(event.type == "finalize_step"
               and event.data == {"scope": scope, "step": "complete"} for event in events)


def test_error_finish_after_scoped_append_failure_recovers_original_scope(monkeypatch, tmp_path):
    """An unscoped error guard cannot steal wrap-up from an already-durable terminal intent."""
    import anyio
    import pytest

    rd = tmp_path / "scoped-finish-append-failure"
    eng = _toy_engine(rd)
    store = eng.store
    store.append("run_started", {"run_id": rd.name, "task_id": eng.task.id,
                                 "goal": eng.task.goal, "direction": eng.task.direction})
    scope = "finalize:original-terminal"
    finish_data = {"reason": "time_budget", "winner": 7}
    real_append = store.append

    def fail_scoped_finish(event_type, data, *args, **kwargs):
        if event_type == "run_finished" and data.get("finalize_scope") == scope:
            raise OSError("scoped terminal append failed")
        return real_append(event_type, data, *args, **kwargs)

    store.append = fail_scoped_finish
    with pytest.raises(OSError, match="scoped terminal append failed"):
        eng._finish_run(finish_data, scope=scope)
    store.append = real_append
    # This is the outer CLI/server guard recording the invocation failure after begun was durable.
    store.append("run_finished", {"reason": "error", "error": "scoped terminal append failed"})
    monkeypatch.setattr(
        eng, "_setup_phase",
        lambda _state: (_ for _ in ()).throw(AssertionError("setup reopened after terminal intent")),
    )
    monkeypatch.setattr(eng, "_store_case", lambda _state: None)
    monkeypatch.setattr(eng, "_write_reflection_note", lambda _state: None)

    state = anyio.run(eng.run)
    assert state.finished and state.stop_reason == "time_budget"
    events = store.read_all()
    successful = [event.data for event in events if event.type == "run_finished"
                  and event.data.get("reason") != "error"]
    assert successful == [{
        **finish_data,
        "finalize_scope": scope,
        "recovered_from_finalize_begun": True,
    }]
    assert not [event for event in events if event.type == "node_created"]
    scoped_effects = [event for event in events
                      if event.type in {"budget", "diversity_archive"}]
    assert scoped_effects
    assert {event.data.get("finalize_scope") for event in scoped_effects} == {scope}
    finalize_steps = [event.data for event in events if event.type == "finalize_step"]
    assert finalize_steps[-1] == {"scope": scope, "step": "complete"}
    assert {step.get("scope") for step in finalize_steps} == {scope}


def test_later_error_supersedes_historical_scoped_success_until_exact_republish(
        monkeypatch, tmp_path):
    """A late projection error makes the earlier success non-effective, but not a new scope."""
    import anyio
    import pytest
    from looplab.engine import finalize as finalize_module

    rd = tmp_path / "late-error-after-scoped-success"
    eng = _toy_engine(rd)
    store = eng.store
    store.append("run_started", {"run_id": rd.name, "task_id": eng.task.id,
                                 "goal": eng.task.goal, "direction": eng.task.direction})
    scope = "finalize:exact-original"
    finish_data = {"reason": "time_budget", "winner": 11}
    eng._finish_run(finish_data, scope=scope)
    monkeypatch.setattr(eng, "_store_case", lambda _state: None)
    monkeypatch.setattr(eng, "_write_reflection_note", lambda _state: None)

    original_build_trace = finalize_module.build_trace_view
    monkeypatch.setattr(
        finalize_module,
        "build_trace_view",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("late projection failed")),
    )
    with pytest.raises(RuntimeError, match="late projection failed"):
        finalize_module.finalize_run(eng, entry_finished=False, start_time=0.0)
    store.append("run_finished", {"reason": "error", "error": "late projection failed"})
    monkeypatch.setattr(finalize_module, "build_trace_view", original_build_trace)

    state = anyio.run(eng.run)
    assert state.finished and state.stop_reason == "time_budget"
    events = store.read_all()
    successful = [event.data for event in events if event.type == "run_finished"
                  and event.data.get("reason") != "error"]
    assert successful == [
        {**finish_data, "finalize_scope": scope},
        {
            **finish_data,
            "finalize_scope": scope,
            "recovered_from_finalize_begun": True,
        },
    ]
    assert sum(event.type == "budget" for event in events) == 1
    assert sum(event.type == "diversity_archive" for event in events) == 1
    assert {event.data.get("scope") for event in events
            if event.type == "finalize_step"} == {scope}
    assert any(event.type == "finalize_step"
               and event.data == {"scope": scope, "step": "complete"} for event in events)


def test_late_finalize_failure_retry_does_not_duplicate_completed_steps(monkeypatch, tmp_path):
    """A projection failure after roll-ups/cross-run writes retries only unfinished wrap-up steps."""
    import anyio
    import pytest
    from looplab.engine import finalize as finalize_module

    rd = tmp_path / "late-error-finalize"
    eng = _toy_engine(rd)
    store = eng.store
    store.append("run_started", {"run_id": rd.name, "task_id": eng.task.id,
                                 "goal": eng.task.goal, "direction": eng.task.direction})
    store.append("run_abort", {"reason": "finalized"})
    store.append("run_finished", {"reason": "aborted"})

    original_build_trace = finalize_module.build_trace_view
    monkeypatch.setattr(
        finalize_module, "build_trace_view",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("trace projection failed")))
    with pytest.raises(RuntimeError, match="trace projection failed"):
        finalize_module.finalize_run(eng, entry_finished=False, start_time=0.0)

    after_first = store.read_all()
    assert sum(event.type == "budget" for event in after_first) == 1
    assert sum(event.type == "diversity_archive" for event in after_first) == 1
    completed_steps = [event for event in after_first if event.type == "finalize_step"]
    assert {event.data["step"] for event in completed_steps} == {
        "budget", "diversity", "llm_cost", "case", "reflection_begun", "reflection"}

    store.append("run_finished", {"reason": "error", "error": "trace projection failed"})
    monkeypatch.setattr(finalize_module, "build_trace_view", original_build_trace)
    state = anyio.run(eng.run)
    assert state.finished and state.stop_reason == "aborted"
    after_retry = store.read_all()
    assert sum(event.type == "budget" for event in after_retry) == 1
    assert sum(event.type == "diversity_archive" for event in after_retry) == 1
    retry_steps = [event.data for event in after_retry if event.type == "finalize_step"]
    # Recovery adds only the new lifecycle boundary markers; all paid/external work steps remain
    # singletons in the stable abort scope.
    assert len(retry_steps) == len(completed_steps) + 2
    assert {step["step"] for step in retry_steps} == {
        "begun", "budget", "diversity", "case", "reflection_begun", "reflection",
        "llm_cost", "complete",
    }


def test_partial_reflection_is_not_replayed_or_rebilled_on_finalize_retry(monkeypatch, tmp_path):
    """A begun marker makes multi-file/LLM reflection at-most-once across failure recovery."""
    import anyio
    import pytest
    from looplab.engine import finalize as finalize_module

    rd = tmp_path / "partial-reflection"
    eng = _toy_engine(rd)
    store = eng.store
    store.append("run_started", {"run_id": rd.name, "task_id": eng.task.id,
                                 "goal": eng.task.goal, "direction": eng.task.direction})
    store.append("run_abort", {"reason": "finalized"})
    store.append("run_finished", {"reason": "aborted"})
    calls = []

    def partial_then_raise(_state):
        calls.append("reflection-called")
        raise RuntimeError("failed after a partial shared-memory write")

    monkeypatch.setattr(eng, "_write_reflection_note", partial_then_raise)
    with pytest.raises(RuntimeError, match="partial shared-memory"):
        finalize_module.finalize_run(eng, entry_finished=False, start_time=0.0)
    steps = [event.data for event in store.read_all() if event.type == "finalize_step"]
    assert any(step["step"] == "reflection_begun" for step in steps)
    assert not any(step["step"] == "reflection" for step in steps)

    store.append("run_finished", {"reason": "error", "error": "partial reflection"})
    state = anyio.run(eng.run)
    assert state.finished and state.stop_reason == "aborted"
    assert calls == ["reflection-called"]
    recovered = [event.data for event in store.read_all()
                 if event.type == "finalize_step" and event.data.get("step") == "reflection"]
    assert recovered[-1]["outcome"] == "prior_attempt_incomplete_not_replayed"
