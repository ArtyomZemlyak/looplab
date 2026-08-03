"""The two expressions every run site in `run_command_eval` must agree on (doc 25 RA-02).

`run_command_eval` is a ~265-line function with 23 parameters and two hand-mirrored eval branches.
Two expressions were written out at every run site, and both are correctness-relevant rather than
cosmetic:

* the DOCKER timeout fold. Under the container tier the deadline is enforced by coreutils `timeout`
  INSIDE the container, so the host subprocess exits normally and `run_argv` reports `to=False` —
  the timeout appears only as exit 124 or 137. A site that forgets the fold reports a timed-out eval
  as an ordinary non-zero failure, which sends the Developer to repair code that never finished.
* the stall window. The staged pipeline and the single-command path must derive the same no-output
  window from a command's own budget, or a staged run kills healthy long stages the serial path
  would have let run.

Written once each now. This file pins the semantics AND that every site goes through them, because
the failure mode is a NEW site added without the fold, which no behavioural test of the old sites
would catch.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from looplab.runtime import command_eval
from looplab.runtime.command_eval import _stall_window, _stall_window_for, _timed_out


# ------------------------------------------------------------------ the docker timeout fold

@pytest.mark.parametrize("to,rc,is_docker,expected", [
    (True, 0, False, True),        # the subprocess itself timed out — always authoritative
    (True, 0, True, True),
    (False, 124, True, True),      # coreutils SIGTERM inside the container
    (False, 137, True, True),      # SIGKILL escalation
    (False, 124, False, False),    # NOT docker: 124 is just an exit code the program chose
    (False, 137, False, False),
    (False, 1, True, False),       # an ordinary container failure is not a timeout
    (False, 0, True, False),
])
def test_the_docker_exit_codes_fold_into_timed_out_only_under_docker(to, rc, is_docker, expected):
    assert _timed_out(to, rc, is_docker) is expected


def test_the_fold_returns_a_bool_not_a_truthy_object():
    """`to` reaches `RunResult.timed_out` and is serialized into events, so a falsy NON-bool leaking
    through would store `0`/`None` where every reader compares against `False`.

    The inputs matter: `or`/`and` already yield real bools for most combinations, so
    `_timed_out(0, 0, False)` is `False` with or without the wrapper and proves nothing. It is the
    all-falsy-non-bool case that returns the raw `0` unwrapped — which is the one this pins.
    """
    assert _timed_out(0, 0, 0) is False
    assert _timed_out(None, 1, None) is False
    assert _timed_out(None, 124, True) is True


# ------------------------------------------------------------------ the stall window

def test_an_explicit_stall_timeout_wins_over_the_derived_window():
    assert _stall_window_for(12.5, 900.0, None) == 12.5
    assert _stall_window_for(12.5, 900.0, 60.0) == 12.5
    # ...including an explicit zero, which means "no stall guard", not "unset"
    assert _stall_window_for(0.0, 900.0, None) == 0.0


def test_without_one_it_derives_from_that_command_s_own_budget():
    assert _stall_window_for(None, 900.0, None) == _stall_window(900.0, None)
    assert _stall_window_for(None, 900.0, 60.0) == _stall_window(900.0, 60.0)
    # a STAGE's budget, not the whole eval's — this is why the helper takes `budget` rather than
    # reading `timeout` off the enclosing scope.
    assert _stall_window_for(None, 30.0, None) != _stall_window_for(None, 900.0, None)


# ------------------------------------------------------------------ every site goes through them

def _calls_in_run_command_eval(name: str) -> int:
    source = inspect.getsource(command_eval.run_command_eval)
    tree = ast.parse(source.lstrip())
    return sum(1 for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == name)


def test_every_run_site_folds_the_docker_timeout_through_the_helper():
    """Three run sites: setup, each staged command, and the single-command path."""
    assert _calls_in_run_command_eval("_timed_out") == 3
    source = inspect.getsource(command_eval.run_command_eval)
    assert "docker_timed_out(rc)" not in source, (
        "a run site re-derives the fold; a fourth site can then omit it silently")


def test_every_run_site_derives_its_stall_window_through_the_helper():
    assert _calls_in_run_command_eval("_stall_window_for") == 2
    source = inspect.getsource(command_eval.run_command_eval)
    assert "if stall_timeout is not None" not in source, (
        "a run site re-derives the stall window; the two paths can then disagree")


def test_the_helpers_are_module_level_so_a_new_site_can_reach_them():
    """Nested in `run_command_eval` they would be invisible to the staged-pipeline helper this
    function is still waiting to be split into (see doc 25)."""
    assert command_eval._timed_out.__qualname__ == "_timed_out"
    assert command_eval._stall_window_for.__qualname__ == "_stall_window_for"
