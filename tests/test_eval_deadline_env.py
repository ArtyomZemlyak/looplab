"""The eval process is told its own clock (doc 52 row 15): `LOOPLAB_EVAL_DEADLINE` + `LOOPLAB_EVAL_TIMEOUT_S`.

The runtime exported the seed, the fence, the Landlock ruleset and the image, and never when the
stage would be killed — so a training script could not size its last epoch or checkpoint and exit
cleanly. `sandbox.eval_deadline_env` is the one derivation; `run_argv` sets the pair for every host
launch and `command_eval._deadline_wrap` forwards it into a container.
"""
from __future__ import annotations

import json
import os
import sys
import time
from types import SimpleNamespace

from looplab.runtime.command_eval import _deadline_wrap, make_docker_wrap, run_command_eval
from looplab.runtime.sandbox import (EVAL_DEADLINE_ENV, EVAL_TIMEOUT_ENV, eval_deadline_env,
                                     run_argv)

_PRINT = ("import json, os\n"
          "print(json.dumps({'metric': 1.0, 'deadline': os.environ.get('LOOPLAB_EVAL_DEADLINE'), "
          "'timeout': os.environ.get('LOOPLAB_EVAL_TIMEOUT_S')}))\n")
_M = {"kind": "stdout_json", "key": "metric"}


def test_the_pair_is_derived_from_the_timeout_and_the_launch_instant():
    assert eval_deadline_env(30.0, now=1000.0) == {EVAL_DEADLINE_ENV: "1030.000",
                                                    EVAL_TIMEOUT_ENV: "30.000"}
    for bad in (0, -5, float("inf"), float("nan"), None, "x"):
        assert eval_deadline_env(bad) == {}, bad


def test_run_argv_exports_the_pair_to_the_child(tmp_path):
    before = time.time()
    rc, out, _err, timed_out = run_argv(
        [sys.executable, "-c", "import os; print(os.environ['LOOPLAB_EVAL_DEADLINE'], "
                               "os.environ['LOOPLAB_EVAL_TIMEOUT_S'])"],
        str(tmp_path), 42.0)
    assert rc == 0 and not timed_out
    deadline, ceiling = out.split()
    assert float(ceiling) == 42.0
    assert before + 42.0 - 1.0 <= float(deadline) <= time.time() + 42.0 + 1.0


def test_an_explicit_declaration_wins_over_the_derived_pair(tmp_path):
    rc, out, _err, _to = run_argv(
        [sys.executable, "-c", "import os; print(os.environ['LOOPLAB_EVAL_DEADLINE'])"],
        str(tmp_path), 42.0, env={EVAL_DEADLINE_ENV: "12345"})
    assert rc == 0 and out.strip() == "12345"


def test_a_single_command_eval_sees_its_own_deadline(tmp_path):
    (tmp_path / "p.py").write_text(_PRINT, encoding="utf-8")
    before = time.time()
    res = run_command_eval([sys.executable, "p.py"], str(tmp_path), 60, _M)
    assert res.metric == 1.0
    row = json.loads(res.stdout.strip().splitlines()[-1])
    assert float(row["timeout"]) == 60.0
    assert before + 59.0 <= float(row["deadline"]) <= time.time() + 61.0


def test_a_docker_wrap_gets_the_pair_forwarded_as_e_args(tmp_path, monkeypatch):
    monkeypatch.setattr("looplab.runtime.command_eval.require_docker_cli", lambda what: None)
    wrap = make_docker_wrap(str(tmp_path), "python:3.12-slim", env={"LOOPLAB_EVAL_SEED": "7"})
    ex = SimpleNamespace(wrap=wrap)
    wrapped, env = _deadline_wrap(ex, wrap, {"VS_LOCAL_DATA_ROOT": "/data"}, 30.0)
    argv = wrapped(["python", "x.py"], str(tmp_path))
    joined = " ".join(argv)
    assert "-e LOOPLAB_EVAL_DEADLINE=" in joined and "-e LOOPLAB_EVAL_TIMEOUT_S=30.000" in joined
    assert "-e VS_LOCAL_DATA_ROOT=/data" in joined and "-e LOOPLAB_EVAL_SEED=7" in joined
    assert env[EVAL_TIMEOUT_ENV] == "30.000" and env["VS_LOCAL_DATA_ROOT"] == "/data"


def test_a_non_docker_wrap_is_returned_untouched():
    marker = object()
    ex = SimpleNamespace(wrap=marker)
    wrapped, env = _deadline_wrap(ex, marker, {}, 30.0)
    assert wrapped is marker and set(env) == {EVAL_DEADLINE_ENV, EVAL_TIMEOUT_ENV}


def test_no_ceiling_means_no_pair_and_the_wrap_untouched():
    marker = object()
    wrapped, env = _deadline_wrap(SimpleNamespace(wrap=marker), marker, {"K": "v"}, 0)
    assert wrapped is marker and env == {"K": "v"}


def test_the_developer_is_told_the_variables_exist():
    """The eval-side half is only useful if the role writing the code knows it is there."""
    from looplab.adapters.repo_developer import LLMRepoDeveloper

    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    dev._eval_time_budget = lambda: 3600.0
    note = dev._time_budget_note()
    assert "LOOPLAB_EVAL_DEADLINE" in note and "LOOPLAB_EVAL_TIMEOUT_S" in note
    dev._eval_time_budget = lambda: None
    assert dev._time_budget_note() == ""
