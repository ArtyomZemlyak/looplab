"""A fact that lives only in the launch line is lost by the one operation between runs.

MEASURED: `eval.env` is null on EVERY task file on this box — e5small_v12.json, e5small_v13.json,
and the task snapshots of v11, v12 and v13. The corpus root reaches a run only as
`-s eval_env=VS_LOCAL_DATA_ROOT=...`.

WHY THAT IS A TRAP RATHER THAN A STYLE POINT. A SETTING rides `config.snapshot.json`, so a RESUME
reproduces it (engine invariant #6). It does NOT ride `task.snapshot.json` — and `NEXT_RUN.md`
documents starting the next run by COPYING that snapshot. So the value survives every resume and is
lost by the one operation an operator actually performs between runs.

IT HAPPENED. v12 was launched from such a copy without the flag: every node crashed in `botocore
ListObjects` on an unset `VS_LOCAL_DATA_ROOT`, each paid a triage+repair to rediscover the local
corpus, and node 14 died of it (#147).

A NOTICE, NOT A REFUSAL, and deliberately about the SUCCESSOR: the run carrying the setting is
correct — the next one copied from it is the one at risk. `adapters/repo_task.py::eval.env` is where
the tree already says the fact belongs ("a fact about the TASK ... every node inherits it instead of
each one spending a repair attempt rediscovering it").
"""
from __future__ import annotations

import types

import pytest

from looplab.engine.orchestrator import _task_declared_env


def _task(env):
    return types.SimpleNamespace(eval=types.SimpleNamespace(env=env))


def test_a_task_that_declares_its_environment_is_carried():
    assert _task_declared_env(_task({"VS_LOCAL_DATA_ROOT": "/home/jovyan/data/dr-local"})) is True


def test_the_shipped_shape_of_every_task_file_on_this_box_is_UNDECLARED():
    """`eval.env` is null on v11, v12, v13 and all three snapshots — this is the live case."""
    assert _task_declared_env(_task(None)) is False
    assert _task_declared_env(_task({})) is False


def test_a_task_model_with_no_eval_section_is_undeclared_not_an_error():
    """Not every task model has an `eval` section; a missing one must read as 'declares nothing',
    never raise inside run_started."""
    assert _task_declared_env(types.SimpleNamespace()) is False
    assert _task_declared_env(types.SimpleNamespace(eval=None)) is False


def test_the_notice_appears_ONLY_when_the_setting_carries_what_the_task_does_not(tmp_path):
    """Four cases, and only one of them is the trap — DRIVEN over a real run's own `run_started`.

    This asserted a 31-column-indented source substring, which is wrong twice: re-wrapping the dict
    (which this same file's change does elsewhere in `orchestrator.py`) turns it red with no
    behaviour change, and commenting the spread out while leaving the text keeps it green. The key
    is a `run_started` field; one fold observes it directly.
    """
    from looplab.core.models import RunState
    from factories import make_engine

    def _started(**kw):
        engine = make_engine(tmp_path / f"run-{len(list(tmp_path.iterdir()))}", **kw)
        engine._setup_phase(RunState())          # the phase that writes `run_started`
        rows = [e for e in engine.store.read_all() if e.type == "run_started"]
        assert rows, "the engine must have written its run_started"
        return rows[0].data

    NOTICE = "eval_env_absent_from_task"
    assert NOTICE not in _started(), "no setting, nothing declared: no notice"
    assert _started(eval_env={"HF_HOME": "/data/hf"}).get(NOTICE) is True, (
        "a live setting carrying what the task does not declare IS the trap")


def test_a_healthy_payload_stays_byte_identical(tmp_path):
    """The key is conditional for the reason the neighbouring `eval_env` key is:
    `speculation_quality._CALIBRATION_RUN_STARTED_FIELDS` compares the payload's key SET for
    equality, so an unconditional key would revoke every issued calibration receipt. The calibration
    profile declares no environment, so `self._eval_env` is empty for it and this key can never
    appear — the same argument, one line down.

    THE KEY SET IS THE PROPERTY, and no assertion in this file touched it: a healthy run's payload
    must differ from a notice-carrying one by exactly this one key, and by nothing else.
    """
    from looplab.core.models import RunState
    from factories import make_engine

    def _keys(**kw):
        engine = make_engine(tmp_path / f"run-{len(list(tmp_path.iterdir()))}", **kw)
        engine._setup_phase(RunState())
        return set(next(e for e in engine.store.read_all() if e.type == "run_started").data)

    healthy = _keys()
    assert "eval_env_absent_from_task" not in healthy, (
        "a run whose task and settings agree must carry no notice at all")
    assert _keys(eval_env={"HF_HOME": "/data/hf"}) - healthy == {
        "eval_env", "eval_env_absent_from_task"}, (
        "the notice may add ITSELF and nothing else beyond the `eval_env` key it qualifies — an "
        "unconditional one would move the key set of EVERY run, and "
        "`_CALIBRATION_RUN_STARTED_FIELDS` compares that set for equality")


def test_the_notice_can_never_appear_on_a_CALIBRATION_payload():
    """Which is the argument the conditional rests on, stated as a check rather than as prose: the
    calibration profile declares no environment, so `self._eval_env` is empty and the left half of
    the condition is False before the task is even consulted."""
    from looplab.search.speculation_quality import _CALIBRATION_RUN_STARTED_FIELDS

    assert "eval_env_absent_from_task" not in _CALIBRATION_RUN_STARTED_FIELDS, (
        "if it were, the gate would be accepting a key that only appears on a misconfigured run")


def test_it_is_a_notice_and_refuses_nothing():
    """Nothing raises, nothing exits, nothing is skipped — the run that trips this is CORRECT."""
    import inspect

    from looplab.engine import orchestrator
    src = inspect.getsource(_task_declared_env)
    assert "raise" not in src
    assert "sys.exit" not in inspect.getsource(orchestrator._task_declared_env)
