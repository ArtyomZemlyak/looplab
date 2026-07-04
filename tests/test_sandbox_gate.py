"""I3 sandbox behavior + I10 variance-gate unit tests."""
from __future__ import annotations

import pytest

from looplab.gate import one_se_better
from looplab.sandbox import SubprocessSandbox


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
