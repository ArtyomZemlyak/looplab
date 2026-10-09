"""An operator's Ctrl-C in `looplab run` / `looplab resume` is a STOP, not a crash.

With `LOOPLAB_UI_AUTO_RESUME=1` the server resumes every run a dead engine left in progress on its next
start (`serve/engine_proc.py::_request_auto_resume`), and it leaves every HALTED run alone. A Ctrl-C
used to leave a log indistinguishable from a crash, so the run the operator stopped was restarted —
and spent — by the next server start. `cli/run_cmds.py::_record_interrupt_pause` records the
interrupt as the same durable `pause` `looplab stop` writes, after the engine loop has stopped. These
deliver a REAL SIGINT to the process while a real engine is mid-run.
"""
import signal
import threading

import pytest

from looplab.cli.run_cmds import INTERRUPT_PAUSE_REASON, _record_interrupt_pause, _run_engine_guarded
from looplab.events.replay import fold
from looplab.serve.engine_proc import _request_auto_resume
from factories import make_engine

pytestmark = pytest.mark.skipif(threading.current_thread() is not threading.main_thread(),
                                reason="a SIGINT is handled on the main thread")


class _InterruptingResearcher:
    """The toy Researcher, until its `n`-th proposal: then the operator presses Ctrl-C."""

    def __init__(self, inner, n=2):
        self.inner, self.n, self.calls = inner, n, 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def propose(self, *args, **kwargs):
        self.calls += 1
        if self.calls >= self.n:
            signal.raise_signal(signal.SIGINT)
        return self.inner.propose(*args, **kwargs)


def _engine(tmp_path, n=2):
    eng = make_engine(tmp_path / "run", max_nodes=6)
    eng.researcher = _InterruptingResearcher(eng.researcher, n=n)
    return eng


def _interrupt(eng):
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    with pytest.raises(KeyboardInterrupt):
        _run_engine_guarded(eng)
    return eng.store.read_all()


def test_a_ctrl_c_mid_run_is_recorded_as_the_operators_pause(tmp_path):
    eng = _engine(tmp_path)
    events = _interrupt(eng)
    pauses = [e for e in events if e.type == "pause"]
    assert [p.data for p in pauses] == [{"reason": INTERRUPT_PAUSE_REASON}]
    # Not necessarily the LAST row: a build thread the cancelled loop abandoned may still append its
    # own node's diagnostic rows — the same as under a `looplab stop` landing mid-build.
    state = fold(events)
    assert state.halted and state.paused and not state.finished
    assert state.pause_reason == INTERRUPT_PAUSE_REASON
    # The auto-resume decision leaves it alone: it is the operator's stop, not a dead engine's run.
    assert _request_auto_resume(eng.run_dir, eng.store, events, state) is False
    # …and a second interrupt never writes a second row.
    assert _record_interrupt_pause(eng) is False
    assert sum(e.type == "pause" for e in eng.store.read_all()) == 1


def test_a_run_already_halted_or_finished_gets_no_pause(tmp_path):
    eng = make_engine(tmp_path / "run", max_nodes=1)
    state = _run_engine_guarded(eng)          # a natural finish
    assert state.finished
    before = eng.store.read_all()
    assert _record_interrupt_pause(eng) is False
    assert eng.store.read_all() == before


def test_a_failing_append_never_masks_the_interrupt(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    # The engine's own rows go through the same store, so refuse only the CLI's pause row.
    real = type(eng.store).append

    def append(event_type, data, **kwargs):
        if event_type == "pause" and data.get("reason") == INTERRUPT_PAUSE_REASON:
            raise OSError("disk full")
        return real(eng.store, event_type, data, **kwargs)

    monkeypatch.setattr(eng.store, "append", append, raising=False)
    events = _interrupt(eng)
    assert not any(e.type == "pause" for e in events)


def test_the_pause_outlasts_more_lost_races_than_the_old_four_tries(tmp_path, monkeypatch):
    """Master CI run 2213 (`pytest (4)`): on a loaded runner the abandoned build thread won all four
    of the old fixed CAS attempts and the Ctrl-C left NO pause. Here the store loses the race six
    times in a row, deterministically: the pause still lands, exactly once."""
    from looplab.events.eventstore import EventStoreConcurrencyError

    eng = make_engine(tmp_path / "run", max_nodes=2)
    eng.store.append("note", {"text": "a run that has started"})
    real_append = eng.store.append
    lost = {"n": 0}

    def racing_append(type_, data, **kwargs):
        if type_ == "pause" and lost["n"] < 6:
            lost["n"] += 1
            real_append("note", {"text": f"abandoned build row {lost['n']}"})
            raise EventStoreConcurrencyError(eng.store.path, kwargs.get("expected_last_seq", -1), -1)
        return real_append(type_, data, **kwargs)

    monkeypatch.setattr(eng.store, "append", racing_append)
    assert _record_interrupt_pause(eng) is True
    pauses = [e.data for e in eng.store.read_all() if e.type == "pause"]
    assert lost["n"] == 6 and pauses == [{"reason": INTERRUPT_PAUSE_REASON}]


def test_a_race_lost_every_time_ends_bounded_with_no_pause(tmp_path, monkeypatch):
    """The other end of the same loop: a log that moves under EVERY attempt exhausts the house CAS
    budget (`retry_tail_cas`'s 64) and answers False — no pause, no hang, the handler restored."""
    from looplab.events.eventstore import EventStoreConcurrencyError

    eng = make_engine(tmp_path / "run", max_nodes=2)
    eng.store.append("note", {"text": "a run that has started"})
    tries = {"n": 0}

    def always_lost(type_, data, **kwargs):
        tries["n"] += 1
        raise EventStoreConcurrencyError(eng.store.path, kwargs.get("expected_last_seq", -1), -1)

    monkeypatch.setattr(eng.store, "append", always_lost)
    assert _record_interrupt_pause(eng) is False
    assert tries["n"] == 64
    assert not any(e.type == "pause" for e in eng.store.read_all())
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_a_second_ctrl_c_while_the_pause_is_written_does_not_abandon_it(tmp_path, monkeypatch):
    """The operator presses Ctrl-C again while the pause is being recorded: SIGINT is held for the
    write, so the pause still lands and no second `KeyboardInterrupt` escapes from inside it."""
    eng = make_engine(tmp_path / "run", max_nodes=2)
    eng.store.append("note", {"text": "a run that has started"})
    real_append = eng.store.append

    def impatient_append(type_, data, **kwargs):
        if type_ == "pause":
            signal.raise_signal(signal.SIGINT)
        return real_append(type_, data, **kwargs)

    monkeypatch.setattr(eng.store, "append", impatient_append)
    try:
        paused = _record_interrupt_pause(eng)
    except KeyboardInterrupt:   # a regression must fail THIS test, not end the session
        pytest.fail("the second SIGINT escaped from inside the pause write")
    assert paused is True
    assert [e.data for e in eng.store.read_all() if e.type == "pause"] == [
        {"reason": INTERRUPT_PAUSE_REASON}]
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_diagnostic_rows_from_the_abandoned_thread_cost_no_refold(tmp_path, monkeypatch):
    """Rows the fold skips cannot change `halted`, so a race lost to them re-decides without folding
    the whole log again; a FOLDED row still forces the re-fold."""
    from looplab.cli import run_cmds
    from looplab.events.eventstore import EventStoreConcurrencyError
    from looplab.events.types import DIAGNOSTIC_EVENTS, EV_PHASE_PROGRESS

    assert EV_PHASE_PROGRESS in DIAGNOSTIC_EVENTS
    eng = make_engine(tmp_path / "run", max_nodes=2)
    eng.store.append("note", {"text": "a run that has started"})
    real_append = eng.store.append
    lost = {"n": 0}

    def racing_append(type_, data, **kwargs):
        if type_ == "pause" and lost["n"] < 3:
            lost["n"] += 1
            real_append(EV_PHASE_PROGRESS, {"phase": "build", "n": lost["n"]})
            raise EventStoreConcurrencyError(eng.store.path, kwargs.get("expected_last_seq", -1), -1)
        return real_append(type_, data, **kwargs)

    folds = {"n": 0}
    real_fold = run_cmds.fold

    def counting_fold(events):
        folds["n"] += 1
        return real_fold(events)

    monkeypatch.setattr(eng.store, "append", racing_append)
    monkeypatch.setattr(run_cmds, "fold", counting_fold)
    assert _record_interrupt_pause(eng) is True
    assert lost["n"] == 3 and folds["n"] == 1
