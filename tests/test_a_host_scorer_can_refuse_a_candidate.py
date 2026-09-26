"""An operator's host scorer can REFUSE a candidate as a stage failure, not only score it 0.0.

MEASURED 2026-09-26 on MiniOneRec inf13: node 1 carried the run's main idea (one ragged pass over a
mixed-length batch), ran a block in 586 ms against the baseline's 1,948 -- and lost 385 of 2,000
users' hits to an indexing bug. The scorer's quality gate printed `speedup: 0.0`, the node landed as
an ordinary evaluated 0.0, and nothing handed it back: an ordinary result is not a repair reason.

`host_scorer.expect.numeric` is the operator's refusal contract on what the scorer prints. A broken
relation fails the host stage as `expect_failed` -- a reason the repair loop already takes -- and the
concern says the SCORER refused the candidate, because "fix the stage or the declaration" is advice
the candidate cannot follow on a protected stage.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from looplab.adapters.repo_task import EvalSpec, HostScorerSpec, RepoTask
from looplab.runtime.command_eval import HOST_STAGE_KEY, run_command_eval
from tests.factories import make_engine

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}
_CONTRACT = {"numeric": [{"key": "refused", "op": "==", "value": 0}]}


def _host(tmp_path: Path, refused: int) -> Path:
    d = tmp_path / "host"
    d.mkdir(exist_ok=True)
    p = d / f"score_{refused}.py"
    p.write_text("import json, sys\nprint(json.dumps({'metric': %s, 'refused': %d, "
                 "'recall50_delta': -0.1665}))\n"
                 "print('REFUSED: lost 385 of 2000 recall@50 users', file=sys.stderr)\n"
                 % ("0.0" if refused else "1.3", refused), encoding="utf-8")
    return p


def _stages(host: Path, expect=None):
    stage = {"name": "score", "command": [sys.executable, str(host)], "timeout": 60,
             HOST_STAGE_KEY: True}
    if expect:
        stage["expect"] = expect
    return [{"name": "work", "command": [sys.executable, "-c", "print('ok')"]}, stage]


def test_a_refusal_fails_the_host_stage_and_says_the_scorer_refused(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    res = run_command_eval(["true"], str(wd), 60, _M, stages=_stages(_host(tmp_path, 1), _CONTRACT),
                           log_dir=str(wd))
    assert res.failed_stage == "score" and res.metric is None
    row = {r["name"]: r for r in res.stages}["score"]
    assert row["status"] == "expect_failed"
    assert "host scorer REFUSED" in row["concern"] and "repair the candidate" in row["concern"]
    assert "do not delete the declaration" not in row["concern"]
    # the repair reads the TAIL of stderr: the scorer's own cause line must be there, and last
    assert "Do NOT edit the score stage" in res.stderr
    assert res.stderr.rstrip().endswith("REFUSED: lost 385 of 2000 recall@50 users")
    assert "REFUSED: lost 385" in res.stderr[-500:]


def test_a_pass_is_the_ordinary_metric(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    res = run_command_eval(["true"], str(wd), 60, _M, stages=_stages(_host(tmp_path, 0), _CONTRACT),
                           log_dir=str(wd))
    assert res.failed_stage is None and res.metric == 1.3


def test_the_spec_takes_numeric_only_and_validates_it():
    ok = HostScorerSpec(command=["/opt/s.py"], expect=_CONTRACT)
    assert ok.expect == {"numeric": [{"key": "refused", "op": "==", "value": 0}]}
    for bad in ({"files": ["x"]}, {"assert": "fine"}, {"numeric": "refused==0"},
                {"numeric": [{"key": "refused", "op": "~", "value": 0}]}):
        with pytest.raises(ValidationError):
            HostScorerSpec(command=["/opt/s.py"], expect=bad)


def test_the_engine_hands_the_contract_to_the_host_stage(tmp_path):
    host = _host(tmp_path, 1)
    task = RepoTask(id="fix", goal="g", direction="max", editable_path=str(FIXTURE),
                    edit_surface=["*.json"], protect=["ttrain.py"],
                    eval=EvalSpec(host_scorer={"command": [sys.executable, str(host)],
                                               "expect": _CONTRACT},
                                  command=[sys.executable, "ttrain.py"], metric=_M, timeout=60))
    researcher, _ = task.build_roles()
    eng = make_engine(tmp_path / "run", task=task, researcher=researcher, n_seeds=1, max_nodes=1)
    stage = eng._host_scorer_stage(eng._eval_spec, {})
    assert stage["expect"] == _CONTRACT and stage[HOST_STAGE_KEY] is True


def test_a_host_scorer_without_a_contract_is_unchanged(tmp_path):
    task = RepoTask(id="fix", goal="g", direction="max", editable_path=str(FIXTURE),
                    edit_surface=["*.json"], protect=["ttrain.py"],
                    eval=EvalSpec(host_scorer={"command": [sys.executable, str(_host(tmp_path, 0))]},
                                  command=[sys.executable, "ttrain.py"], metric=_M, timeout=60))
    researcher, _ = task.build_roles()
    eng = make_engine(tmp_path / "run", task=task, researcher=researcher, n_seeds=1, max_nodes=1)
    assert "expect" not in eng._host_scorer_stage(eng._eval_spec, {})
