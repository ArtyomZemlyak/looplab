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


def test_the_notice_appears_ONLY_when_the_setting_carries_what_the_task_does_not():
    """Four cases, and only one of them is the trap. Read off the source because constructing an
    Engine starts a run; the condition is what is under test."""
    import inspect

    from looplab.engine import orchestrator
    src = inspect.getsource(orchestrator)
    cond = '**({"eval_env_absent_from_task": True}\n                               if self._eval_env and not _task_declared_env(self.task) else {}),'
    assert cond in src, "the notice must require BOTH a live setting and an undeclaring task"


def test_a_healthy_payload_stays_byte_identical():
    """The key is conditional for the reason the neighbouring `eval_env` key is:
    `speculation_quality._CALIBRATION_RUN_STARTED_FIELDS` compares the payload's key SET for
    equality, so an unconditional key would revoke every issued calibration receipt. The calibration
    profile declares no environment, so `self._eval_env` is empty for it and this key can never
    appear — the same argument, one line down."""
    import inspect

    from looplab.engine import orchestrator
    src = inspect.getsource(orchestrator)
    # conditional spread, never a bare assignment
    assert '"eval_env_absent_from_task": True' in src
    assert 'if self._eval_env and not _task_declared_env' in src
    assert '\n                            "eval_env_absent_from_task"' not in src


def test_it_is_a_notice_and_refuses_nothing():
    """Nothing raises, nothing exits, nothing is skipped — the run that trips this is CORRECT."""
    import inspect

    from looplab.engine import orchestrator
    src = inspect.getsource(_task_declared_env)
    assert "raise" not in src
    assert "sys.exit" not in inspect.getsource(orchestrator._task_declared_env)
