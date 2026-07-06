"""The 3-verb operator control model: STOP (freeze, no finalization), FINALIZE (stop + wrap-up:
report / cross-run lessons+case / cost roll-up), RESUME (continue from any stopped state). Replaces
the pause/abort/resume/reopen tangle — pause≡stop, abort≡finalize, reopen≡resume."""
from __future__ import annotations

import itertools
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
    m = lambda a: _action_to_control(_Action(action=a), st)["type"]
    assert m("stop") == "pause"          # freeze
    assert m("finalize") == "run_abort"  # wrap up
    assert m("resume") == "resume"       # continue
    assert m("pause") == "pause"         # alias of stop
    assert m("abort") == "run_abort"     # alias of finalize
    # finalize carries a reason; stop/resume don't
    assert _action_to_control(_Action(action="finalize"), st)["data"] == {"reason": "finalized"}
    assert _action_to_control(_Action(action="stop"), st)["data"] == {}


def test_finalize_and_resume_need_engine_respawn_but_stop_does_not():
    """A finalize/resume on a run whose engine EXITED must (re)spawn the engine to act; a bare stop
    just freezes (no respawn)."""
    from looplab.serve.tui import action_needs_engine
    assert action_needs_engine({"type": "run_abort"}) is True    # finalize -> wrap-up needs the engine
    assert action_needs_engine({"type": "resume"}) is True       # continue needs the engine
    assert action_needs_engine({"type": "pause"}) is False       # stop just freezes


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
