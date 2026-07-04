"""I3 sandbox behavior + I10 variance-gate unit tests."""
from __future__ import annotations

import pytest

from looplab.trust.gate import one_se_better
from looplab.runtime.sandbox import SubprocessSandbox, _json_line_extras, _parse_metric


def test_sandbox_captures_metric(tmp_path):
    sb = SubprocessSandbox()
    code = 'import json; print(json.dumps({"metric": 42.5}))'
    res = sb.run(code, str(tmp_path / "ok"), timeout=30.0)
    assert res.exit_code == 0
    assert res.metric == 42.5
    assert not res.timed_out


def test_sandbox_relative_workdir(tmp_path, monkeypatch):
    """Regression: a relative workdir must not double against cwd."""
    monkeypatch.chdir(tmp_path)
    sb = SubprocessSandbox()
    res = sb.run('import json; print(json.dumps({"metric": 1.0}))',
                 "runs/x/nodes/node_0", timeout=30.0)
    assert res.exit_code == 0 and res.metric == 1.0


def test_sandbox_reports_failure_no_metric(tmp_path):
    sb = SubprocessSandbox()
    res = sb.run('print("no metric here")', str(tmp_path / "nometric"), timeout=30.0)
    assert res.metric is None  # -> orchestrator records node_failed


def test_sandbox_nonzero_exit(tmp_path):
    sb = SubprocessSandbox()
    res = sb.run('raise SystemExit(3)', str(tmp_path / "boom"), timeout=30.0)
    assert res.exit_code == 3


def test_sandbox_timeout_is_killed(tmp_path):
    sb = SubprocessSandbox()
    res = sb.run('import time; time.sleep(30)', str(tmp_path / "slow"), timeout=1.0)
    assert res.timed_out
    assert res.metric is None


@pytest.mark.parametrize(
    "cand,inc,std,n,direction,expected",
    [
        (1.0, 5.0, 2.0, 4, "min", True),    # clearly better (>1 SE)
        (4.9, 5.0, 2.0, 4, "min", False),   # within noise -> rejected
        (9.0, 5.0, 2.0, 4, "max", True),    # clearly better for max
        (3.0, 5.0, 0.0, 1, "min", True),    # no variance info -> strict compare
    ],
)
def test_one_se_gate(cand, inc, std, n, direction, expected):
    assert one_se_better(cand, inc, std, n, direction) is expected


# --- sandbox metric parsing: NaN/inf rejected, byte cap honored, tier kwargs tolerated ------------

def test_inf_metric_rejected_in_solution_path():
    assert _parse_metric('{"metric": Infinity}') is None
    assert _parse_metric('{"metric": 1.5}') == 1.5


def test_make_sandbox_tolerates_extra_kwargs():
    from looplab.runtime.sandbox import SubprocessSandbox, make_sandbox
    s = make_sandbox("trusted_local", image="ignored", max_output_bytes=1000)
    assert isinstance(s, SubprocessSandbox) and s.max_output_bytes == 1000


def test_json_line_extras_rejects_nan_and_inf():
    out = '{"metric": 0.5, "loss": NaN, "lr": Infinity, "recall": 0.7}'
    extras = _json_line_extras(out)
    assert extras == {"recall": 0.7}


def test_clamp_tail_bytes_respects_byte_budget_on_multibyte():
    from looplab.runtime.sandbox import _clamp_tail_bytes
    s = "世" * 100                                              # 300 UTF-8 bytes
    out = _clamp_tail_bytes(s, 90)
    assert len(out.encode("utf-8")) <= 90                       # a plain [-90:] would keep 270 bytes
