"""The eval's two run paths return ONE object, so the metric read cannot see an unbound name.

Doc 25 RA-02's second half. `run_command_eval` ran the staged pipeline and the single-command path
as two inline branches and then read `rc`/`out`/`err`/`to`/`_sig` off whichever one happened to
execute. That is a leak by construction, and it bit: `_sig` was bound only inside the stage loop, so
a pipeline where NO stage ran (every stage reused via `start_stage`, or a stage whose command
expands to `[]`) reached the closing `RunResult(..., stalled=_salvageable_stall(_sig))` with the
name unbound — UnboundLocalError straight out of the eval worker, and the node got no terminal event
at all. It was fixed by pre-binding one more name in one branch, which fixes the instance and leaves
the class.

Both paths now return an `_EvalRun` whose every field has a default, and the tail reads that object.
This file pins the structural property, and gives the two run helpers their first direct coverage —
until the split they could only be reached through the 23-parameter front door.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from looplab.runtime import command_eval
from looplab.runtime.command_eval import _EvalExec, _EvalRun, _run_single, _run_stages

METRIC = {"kind": "stdout_json", "key": "metric"}
PRINTS_METRIC = [sys.executable, "-c", 'print(\'{"metric": 1.0}\')']
CRASHES = [sys.executable, "-c", 'import sys; sys.stderr.write("boom"); sys.exit(3)']


def _exec(tmp_path, **over) -> _EvalExec:
    """The knobs an offline, un-sandboxed, un-traced eval runs under."""
    knobs = dict(wd=Path(tmp_path), env=None, cancel=None, max_output_bytes=64_000, grace=0.0,
                 is_docker=False, wrap=None, stall_timeout=None, stall_cap=None,
                 bound=lambda argv, secs: list(argv), wrap_argv=lambda argv, host_cwd: argv,
                 log=lambda name: None, span=lambda *a, **k: nullcontext(None))
    knobs.update(over)
    return _EvalExec(**knobs)


def _stages(tmp_path, stages, **over):
    return _run_stages(stages, _exec(tmp_path), timeout=30.0, start_stage=over.get("start_stage"),
                       metric=METRIC, eval_started=0.0, check_fn=over.get("check_fn"))


# ------------------------------------------------------------------ the leak is unrepresentable

def test_every_field_the_metric_read_needs_has_a_default():
    """The whole point. A path that returns without touching a field must still hand the tail a
    usable value — that is what `_sig` did not do."""
    missing = [f.name for f in dataclasses.fields(_EvalRun)
               if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING]
    assert missing == [], (
        f"{missing} has no default, so a run path that does not set it re-opens the "
        "UnboundLocalError this split exists to make impossible")
    blank = _EvalRun()
    assert (blank.rc, blank.out, blank.err, blank.timed_out) == (0, "", "", False)
    assert blank.signals == {} and blank.stage_results is None and blank.early is None


def test_the_signals_default_is_a_fresh_dict_per_run():
    """A shared mutable default would carry one eval's watchdog verdict into the next — and the
    verdict decides whether a crashed run's self-reported metric is salvaged."""
    first, second = _EvalRun(), _EvalRun()
    first.signals["stalled"] = True
    assert second.signals == {}


def test_the_metric_read_no_longer_lives_downstream_of_two_inline_branches():
    """`run_command_eval` keeps exactly one run site — setup. The eval's own two are in the helpers,
    so the tail has no branch-local name left to read."""
    tree = ast.parse(inspect.getsource(command_eval.run_command_eval).lstrip())
    run_argv_calls = [node for node in ast.walk(tree)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                      and node.func.id == "run_argv"]
    assert len(run_argv_calls) == 1, (
        "run_command_eval runs the eval inline again; the metric read below it can then observe "
        "whatever that branch happened to bind")


# ------------------------------------------------------------------ the staged path

def test_a_pipeline_where_no_stage_runs_returns_usable_defaults(tmp_path):
    """The exact shape that raised UnboundLocalError, now asked of the helper directly."""
    run = _stages(tmp_path, [{"name": "train", "command": []}, {"name": "score", "command": []}])

    assert run.early is None
    assert run.stage_results == []
    assert run.signals == {} and run.rc == 0 and run.timed_out is False


def test_a_failing_stage_comes_back_as_an_early_result_naming_the_stage(tmp_path):
    run = _stages(tmp_path, [{"name": "train", "command": CRASHES},
                             {"name": "score", "command": PRINTS_METRIC}])

    assert run.early is not None, "a failed stage must not fall through to the metric read"
    assert run.early.failed_stage == "train"
    assert run.early.exit_code == 3
    assert "stage 'train' failed" in run.early.stderr
    assert [s["status"] for s in run.early.stages] == ["fail"], (
        "the later stage must not have run")


def test_the_last_stages_output_is_what_flows_to_the_metric_read(tmp_path):
    run = _stages(tmp_path, [{"name": "prep", "command": [sys.executable, "-c", "print('prep')"]},
                             {"name": "score", "command": PRINTS_METRIC}])

    assert run.early is None
    assert '{"metric": 1.0}' in run.out and "prep" not in run.out
    assert [s["status"] for s in run.stage_results] == ["ok", "ok"]


def test_start_stage_marks_earlier_stages_reused_without_running_them(tmp_path):
    marker = tmp_path / "ran.txt"
    writes = [sys.executable, "-c", f"open({str(marker)!r}, 'a').write('x')"]

    run = _stages(tmp_path, [{"name": "train", "command": writes},
                             {"name": "score", "command": PRINTS_METRIC}],
                  start_stage="score")

    assert not marker.exists(), "a reused stage must not be paid for again"
    assert [s["status"] for s in run.stage_results] == ["reused", "ok"]


def test_an_unknown_start_stage_re_runs_everything(tmp_path):
    """The fail-safe direction: resuming on a typo would score a stale artifact."""
    run = _stages(tmp_path, [{"name": "train", "command": PRINTS_METRIC},
                             {"name": "score", "command": PRINTS_METRIC}],
                  start_stage="typo")

    assert [s["status"] for s in run.stage_results] == ["ok", "ok"]


def test_an_inter_stage_concern_stops_the_pipeline_before_the_next_stage(tmp_path):
    seen = []

    def check(name, tail):
        seen.append(name)
        return "the loss diverged"

    run = _stages(tmp_path, [{"name": "train", "command": PRINTS_METRIC, "check": True},
                             {"name": "score", "command": PRINTS_METRIC}],
                  check_fn=check)

    assert seen == ["train"]
    assert run.early is not None and run.early.failed_stage == "train"
    assert "failed verification" in run.early.stderr
    assert run.early.metric is None
    assert run.early.stages[-1]["status"] == "check_failed"


def test_a_checker_that_raises_does_not_crash_the_eval(tmp_path):
    def check(_name, _tail):
        raise RuntimeError("checker is broken")

    run = _stages(tmp_path, [{"name": "train", "command": PRINTS_METRIC, "check": True}],
                  check_fn=check)

    assert run.early is None, "a broken checker must not fail the candidate"


# ------------------------------------------------------------------ the single-command path

def test_the_single_command_path_fills_the_same_shape(tmp_path):
    run = _run_single(PRINTS_METRIC, _exec(tmp_path), timeout=30.0)

    assert isinstance(run, _EvalRun)
    assert run.rc == 0 and '{"metric": 1.0}' in run.out
    assert run.early is None, "there is no early return on this path"
    assert run.stage_results is None, "no stages ran, so the result carries no stage list"
    # `run_argv` fills the dict in place with its OUT-OF-BAND watchdog verdict — the whole reason
    # `signals` is passed down rather than parsed back out of the candidate's forgeable stderr.
    assert run.signals == {"stalled": False, "diverged": False}


def test_the_single_command_path_reports_a_crash_without_an_early_result(tmp_path):
    """A plain crash still flows to the metric read — that is what lets `evaluate.py` decide
    salvage, rather than the runtime deciding it here."""
    run = _run_single(CRASHES, _exec(tmp_path), timeout=30.0)

    assert run.rc == 3 and "boom" in run.err
    assert run.early is None


@pytest.mark.parametrize("is_docker,rc,expected", [(True, 124, True), (False, 124, False)])
def test_both_paths_fold_the_container_timeout_into_timed_out(tmp_path, is_docker, rc, expected):
    """Exit 124 is how a coreutils `timeout` inside the container reports the deadline; the host
    subprocess exited normally. Asked of BOTH helpers, because the fold living at only one of them
    is the drift `_timed_out` exists to prevent."""
    exits = [sys.executable, "-c", f"import sys; sys.exit({rc})"]
    ex = _exec(tmp_path, is_docker=is_docker)

    single = _run_single(exits, ex, timeout=30.0)
    staged = _run_stages([{"name": "train", "command": exits}], ex, timeout=30.0,
                         start_stage=None, metric=METRIC, eval_started=0.0, check_fn=None)

    assert single.timed_out is expected
    assert staged.early is not None and staged.early.timed_out is expected
