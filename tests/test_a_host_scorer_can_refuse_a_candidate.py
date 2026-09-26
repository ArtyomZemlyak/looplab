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


# ------------------------------------------------ what the refusing scorer SAYS, read off its row
#
# 2026-09-26 (the refusal-to-repair pipeline): the stderr TAIL every repair-facing text was built from
# opened with progress-bar or CUDA residue and cut the protected-scorer warning off in 5 of 5 inf13
# refusals. `host_scorer.diagnosis_key` / `would_be_key` name two keys of the scorer's own result row,
# read with the numeric contract's last-occurrence rule; the account then reaches the judge and the
# repair whole, and the would-be number is what `host_refusal_deferral` holds against the champion.

from looplab.engine.failure_diagnosis import REASON_SOURCE_ENGINE, diagnosis_repair_lead  # noqa: E402
from looplab.runtime.command_eval import (HOST_DIAGNOSIS_CHARS, HOST_DIAGNOSIS_KEY,  # noqa: E402
                                          HOST_WOULD_BE_KEY, _host_refusal_readings)
from looplab.runtime.numeric_contract import last_json_string  # noqa: E402


def _talking_host(tmp_path: Path, diagnosis) -> Path:
    d = tmp_path / "host"
    d.mkdir(exist_ok=True)
    p = d / "score_talks.py"
    p.write_text("import json\n"
                 "print(json.dumps({'metric': 0.0, 'refused': 1, 'would_be_speedup': 1.0,"
                 " 'refusal_diagnosis': 'an earlier row'}))\n"
                 f"print(json.dumps({{'metric': 0.0, 'refused': 1, 'would_be_speedup': 4.27,"
                 f" 'refusal_diagnosis': {diagnosis!r}}}))\n", encoding="utf-8")
    return p


def _talking_stages(host: Path, *, stamp=True):
    stage = {"name": "score", "command": [sys.executable, str(host)], "timeout": 60,
             HOST_STAGE_KEY: True, "expect": _CONTRACT}
    if stamp:
        stage[HOST_WOULD_BE_KEY] = "would_be_speedup"
        stage[HOST_DIAGNOSIS_KEY] = "refusal_diagnosis"
    return [{"name": "work", "command": [sys.executable, "-c", "print('ok')"]}, stage]


def test_the_refusing_scorers_own_account_and_would_be_number_are_read_off_its_last_row(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    why = "REFUSED: recall@50 -0.218; 3 short requests ALONE: BROKEN (43,48,49/200)"
    res = run_command_eval(["true"], str(wd), 60, _M,
                           stages=_talking_stages(_talking_host(tmp_path, why)), log_dir=str(wd))
    assert res.failed_stage == "score"
    assert res.host_would_be == 4.27 and res.host_diagnosis == why       # the LAST row decides


def test_undeclared_keys_are_not_read(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    res = run_command_eval(["true"], str(wd), 60, _M,
                           stages=_talking_stages(_talking_host(tmp_path, "x"), stamp=False),
                           log_dir=str(wd))
    assert res.failed_stage == "score" and res.host_would_be is None and res.host_diagnosis is None


def test_the_readings_rule():
    stage = {HOST_WOULD_BE_KEY: "w", HOST_DIAGNOSIS_KEY: "d"}
    assert _host_refusal_readings(stage, '{"w": 2.0, "d": "a"}\n{"w": 3.0, "d": null}\n') == (3.0, None)
    assert _host_refusal_readings(stage, '{"w": "nan?", "d": "   "}\n') == (None, None)
    long = "y" * (HOST_DIAGNOSIS_CHARS + 50)
    assert _host_refusal_readings(stage, '{"d": "%s"}\n' % long)[1] == "y" * HOST_DIAGNOSIS_CHARS
    assert _host_refusal_readings({}, '{"w": 2.0, "d": "a"}\n') == (None, None)
    assert last_json_string('{"D": "case"}\nnot json {"d": "x"\n', "d") == "case"


def test_the_spec_takes_row_keys_and_the_engine_stamps_them(tmp_path):
    ok = HostScorerSpec(command=["/opt/s.py"], expect=_CONTRACT, would_be_key="would_be_speedup",
                        diagnosis_key="refusal_diagnosis")
    assert ok.would_be_key == "would_be_speedup" and ok.diagnosis_key == "refusal_diagnosis"
    for bad in ("", " ", "has space", "x" * 200, "1starts_with_digit"):
        with pytest.raises(ValidationError):
            HostScorerSpec(command=["/opt/s.py"], would_be_key=bad)
    host = _host(tmp_path, 1)
    task = RepoTask(id="fix", goal="g", direction="max", editable_path=str(FIXTURE),
                    edit_surface=["*.json"], protect=["ttrain.py"],
                    eval=EvalSpec(host_scorer={"command": [sys.executable, str(host)],
                                               "expect": _CONTRACT,
                                               "would_be_key": "would_be_speedup",
                                               "diagnosis_key": "refusal_diagnosis"},
                                  command=[sys.executable, "ttrain.py"], metric=_M, timeout=60))
    researcher, _ = task.build_roles()
    eng = make_engine(tmp_path / "run", task=task, researcher=researcher, n_seeds=1, max_nodes=1)
    stage = eng._host_scorer_stage(eng._eval_spec, {})
    assert stage[HOST_WOULD_BE_KEY] == "would_be_speedup"
    assert stage[HOST_DIAGNOSIS_KEY] == "refusal_diagnosis"


def test_the_failure_text_carries_the_account_fenced_with_no_stderr_tail(tmp_path):
    from looplab.core.evidence import EVIDENCE_LABEL
    from looplab.runtime.sandbox import RunResult
    host = _host(tmp_path, 1)
    task = RepoTask(id="fix", goal="g", direction="max", editable_path=str(FIXTURE),
                    edit_surface=["*.json"], protect=["ttrain.py"],
                    eval=EvalSpec(host_scorer={"command": [sys.executable, str(host)]},
                                  command=[sys.executable, "ttrain.py"], metric=_M, timeout=60))
    researcher, _ = task.build_roles()
    eng = make_engine(tmp_path / "run", task=task, researcher=researcher, n_seeds=1, max_nodes=1)
    res = RunResult(exit_code=0, stdout="", metric=None, timed_out=False, failed_stage="score",
                    stderr="Loading weights: 100%|██████| 338/338\nCUDA residue " * 40,
                    host_diagnosis="REFUSED: short requests ALONE: BROKEN")
    text = eng._eval_failure_text(res)
    assert text.startswith("[failed stage: score]\n")
    assert "Do NOT edit the score stage" in text
    assert f"{EVIDENCE_LABEL}\nREFUSED: short requests ALONE: BROKEN\nEND {EVIDENCE_LABEL}" in text
    assert "CUDA residue" not in text and "Loading weights" not in text
    # …and a refusal with no declared account keeps the historical tail, byte for byte.
    plain = RunResult(exit_code=0, stdout="", metric=None, timed_out=False, failed_stage="score",
                      stderr="the host scorer REFUSED this candidate (refused == 0)")
    assert eng._eval_failure_text(plain).endswith("the host scorer REFUSED this candidate (refused == 0)")


# ---------------------------------------- the diagnosis of a host refusal leads the repair's text

@pytest.mark.parametrize("summary, source, host_refusal, lead", [
    ("positions use the batch max", REASON_SOURCE_ENGINE, False, False),   # engine-final: withheld
    ("positions use the batch max", REASON_SOURCE_ENGINE, True, True),     # …except a host refusal
    ("", REASON_SOURCE_ENGINE, True, False),                               # nothing to lead with
    ("already in the text", REASON_SOURCE_ENGINE, True, False),            # not said twice
])
def test_the_host_refusal_exception_to_the_engine_final_rule(summary, source, host_refusal, lead):
    got = diagnosis_repair_lead(summary, source, "error: already in the text", host_refusal=host_refusal)
    assert bool(got) is lead
    if lead:
        assert summary in got


def test_the_repair_is_led_by_the_diagnosis_only_with_the_switch_on(tmp_path):
    from tests.test_a_first_host_refusal_buys_a_repair import _passed, _refused, _run
    verdict = {"action": "repair", "rationale": "fix the decode positions",
               "summary": "decode positions use the batch max M+s instead of each request's L+s"}
    for lead in (True, False):
        run = tmp_path / str(lead)
        run.mkdir()
        _events, _judge, dev, _nid = _run(run, [_refused(), _passed()], verdicts=[verdict],
                                          deferral=False, lead=lead)
        said = "concluded: decode positions use the batch max" in dev.errors[0]
        assert said is lead, (lead, dev.errors[0][:300])
