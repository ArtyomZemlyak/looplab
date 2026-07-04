"""command_eval metric readers: NaN/inf rejection, drift corroboration, regex/file readers, BOM
tolerance, adapter-reader refusal, and setup-vs-eval cwd separation.

Regressions consolidated from the code-review rounds (feasibility/NaN round, deep audit, RepoTask
/code-review findings)."""
from __future__ import annotations

import sys

import pytest

from looplab.runtime.command_eval import _drift, read_metric, run_command_eval

_M = {"kind": "stdout_json", "key": "metric"}


# #3 — NaN/inf metric is rejected at read time (never enters best-selection)
def test_nan_metric_rejected(tmp_path):
    (tmp_path / "p.py").write_text(
        'import json; print(json.dumps({"metric": float("nan")}))\n', encoding="utf-8")
    res = run_command_eval([sys.executable, "p.py"], str(tmp_path), 60, _M)
    assert res.metric is None                          # NaN -> no metric, not a NaN best


def test_drift_nan_is_not_corroborated():
    assert _drift(float("nan"), float("nan"), 1e-6) is True   # NaN never "agrees"
    assert _drift(1.0, 1.0, 1e-6) is False


# B3 — a bad regex metric pattern reads as no-metric, not a crash
def test_regex_metric_bad_pattern_is_none():
    assert read_metric("acc=0.9", ".", {"kind": "stdout_regex", "pattern": "(", "group": 1}) is None
    assert read_metric("acc=0.9", ".", {"kind": "stdout_regex", "pattern": r"acc=([0-9.]+)",
                                        "group": 5}) is None       # group out of range


# #55 — a metric file with a UTF-8 BOM still parses
def test_file_json_strips_bom(tmp_path):
    (tmp_path / "m.json").write_text('﻿{"metric": 0.7}', encoding="utf-8")
    assert read_metric("", str(tmp_path),
                       {"kind": "file_json", "path": "m.json", "key": "metric"}) == 0.7


# #54 — a constraint/metric reader may not be an agent-authored adapter
def test_constraints_adapter_reader_rejected(tmp_path):
    from looplab.runtime.command_eval import run_command_eval
    (tmp_path / "p.py").write_text('print("{\\"metric\\": 1.0}")', encoding="utf-8")
    with pytest.raises(ValueError, match="built-in, not 'adapter'"):
        run_command_eval([sys.executable, "p.py"], str(tmp_path), 60, _M,
                         constraints=[{"kind": "adapter", "path": "x.py", "max": 1}])


# #8 — setup runs at its own cwd (repo root), separate from the eval command's cwd (a subdir)
def test_setup_cwd_separate_from_eval_cwd(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "main.py").write_text(
        'import json, os\n'
        'print(json.dumps({"metric": 1.0 if os.path.exists(os.path.join("..","dep.txt")) else 0.0}))\n',
        encoding="utf-8")
    res = run_command_eval([sys.executable, "main.py"], str(sub), 60, _M,
                           setup=[sys.executable, "-c", "open('dep.txt','w').write('x')"],
                           setup_cwd=str(tmp_path))
    assert res.metric == 1.0                            # setup created dep.txt at root, not in sub


# docker wall-clock timeout is BOTH exit 124 (SIGTERM at deadline) and 137 (SIGKILL escalation
# past the `timeout -k` grace). command_eval used to flag only 124, so a docker eval killed by the
# grace escalation (common for a tight BLAS/numpy loop) fell through to the OOM heuristic and got
# the wrong repair directive. Both must read as timed_out — shared with the sandbox via one helper.
def test_docker_timed_out_covers_124_and_137():
    from looplab.runtime.sandbox import docker_timed_out
    assert docker_timed_out(124) and docker_timed_out(137)
    assert not docker_timed_out(0) and not docker_timed_out(1) and not docker_timed_out(2)
