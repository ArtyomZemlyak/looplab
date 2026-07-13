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


def test_regex_metric_found_before_a_long_trailing_log():
    # Code-review pass: the ReDoS cap scans only the last 200k chars, but a script that prints the
    # metric EARLY and then dumps a long report would lose it to a tail-only window. The head is a
    # fallback when the tail has no match, so an early metric before >200k of trailing logs is found.
    spec = {"kind": "stdout_regex", "pattern": r"RMSE:\s*([0-9.]+)", "group": 1}
    stdout = "RMSE: 0.1234\n" + ("progress....\n" * 40_000)   # >200k chars of trailing noise
    assert len(stdout) > 200_000
    assert read_metric(stdout, ".", spec) == 0.1234
    # and a metric at the TAIL (the common case) still wins as the LAST match
    assert read_metric("RMSE: 9.9\n" + ("x\n" * 100) + "RMSE: 0.5\n", ".", spec) == 0.5


# #55 — a metric file with a UTF-8 BOM still parses
def test_file_json_strips_bom(tmp_path):
    (tmp_path / "m.json").write_text('﻿{"metric": 0.7}', encoding="utf-8")
    assert read_metric("", str(tmp_path),
                       {"kind": "file_json", "path": "m.json", "key": "metric"}) == 0.7


# §6.3 freshness gate — a FILE reader rejects a metric file older than the eval start (`since`)
def test_read_metric_since_rejects_stale_file(tmp_path):
    import os
    import time
    p = tmp_path / "m.json"
    p.write_text('{"metric": 0.9}', encoding="utf-8")
    spec = {"kind": "file_json", "path": "m.json", "key": "metric"}
    assert read_metric("", str(tmp_path), spec) == 0.9                    # no gate -> read (legacy)
    now = time.time()
    assert read_metric("", str(tmp_path), spec, since=now - 1.0) == 0.9   # written now -> fresh
    old = now - 3600
    os.utime(p, (old, old))                                              # age it: a prior attempt's file
    assert read_metric("", str(tmp_path), spec, since=now) is None        # stale -> rejected


def test_freshness_gate_rejects_stale_metric_file_end_to_end(tmp_path):
    # A "great" metric file left by a PRIOR attempt in a reused workdir must NOT be promoted when this
    # eval's command produces no new output (§6.3 workdir-reuse trap); a file the command REWRITES is read.
    import os
    import time
    stale = tmp_path / "m.json"
    stale.write_text('{"metric": 0.01}', encoding="utf-8")               # a suspiciously perfect score
    old = time.time() - 3600
    os.utime(stale, (old, old))
    spec = {"kind": "file_json", "path": "m.json", "key": "metric"}
    res = run_command_eval([sys.executable, "-c", "pass"], str(tmp_path), 60, spec)   # writes nothing
    assert res.exit_code == 0 and res.metric is None                     # stale file NOT promoted
    res2 = run_command_eval(                                             # this run rewrites m.json
        [sys.executable, "-c", "open('m.json','w').write('{\"metric\": 0.5}')"], str(tmp_path), 60, spec)
    assert res2.exit_code == 0 and res2.metric == 0.5                    # fresh -> read normally


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


def test_stage_start_emits_a_live_band_anchor(tmp_path):
    # A long training stage emits no child spans and its own operation span flushes only on CLOSE, so
    # a live trace would stay blank the whole run. run_command_eval flushes a `stage_started` child the
    # instant each stage begins, carrying that stage's phase stamp, so the live view can band
    # "Train"/"Evaluate · score" immediately. Assert the anchors are on disk, one per stage, stamped.
    import orjson

    from looplab.core.tracing import JsonlSpanExporter, Tracer

    (tmp_path / "train.py").write_text("print('trained')\n", encoding="utf-8")
    (tmp_path / "score.py").write_text(
        'import json; print(json.dumps({"metric": 0.9}))\n', encoding="utf-8")
    spans = tmp_path / "spans.jsonl"
    tr = Tracer(JsonlSpanExporter(str(spans)), run_id="r")
    stages = [{"name": "train", "command": [sys.executable, "train.py"]},
              {"name": "score", "command": [sys.executable, "score.py"]}]
    with tr.span("evaluate", new_trace=True, node_id=7):
        res = run_command_eval([sys.executable, "score.py"], str(tmp_path), 60, _M,
                               tracer=tr, log_dir=str(tmp_path), stages=stages)
    assert res.metric == 0.9
    recs = [orjson.loads(x) for x in spans.read_bytes().splitlines()]
    anchors = [r for r in recs if r["name"] == "stage_started"]
    assert {r["attributes"].get("stage") for r in anchors} == {"train", "score"}   # one per stage
    by_stage = {r["attributes"]["stage"]: r for r in anchors}
    assert by_stage["train"]["attributes"].get("phase") == "train"     # phase-stamped -> bands "Train"
    assert all(r["attributes"].get("node_id") == 7 for r in anchors)   # attributed to the node


# --- untrusted-tier hardening + env forwarding (architecture review H3 / M12) ---
# make_docker_wrap builds the RepoTask command-eval container. It must mirror DockerSandbox.run's
# hardening (cap-drop / no-new-privileges / memory / cpus) and forward engine env via -e, so the
# untrusted/hostile tier actually isolates and LOOPLAB_EVAL_SEED reaches the containerized eval.
# Inspects the produced argv only — no Docker daemon needed (monkeypatch shutil.which).

def _wrap_argv(monkeypatch, **kw):
    import shutil
    from looplab.runtime import command_eval as ce
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    w = ce.make_docker_wrap("/tmp/root", "python:3.12-slim", **kw)
    return w(["python", "solution.py"], "/tmp/root")


def test_docker_wrap_hardens_container(monkeypatch):
    argv = _wrap_argv(monkeypatch, mem="4g", cpus="2")
    joined = " ".join(argv)
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert argv[argv.index("--memory") + 1] == "4g"
    assert argv[argv.index("--cpus") + 1] == "2"
    assert argv[:2] == ["docker", "run"] and argv[-3:] == ["python:3.12-slim", "python", "solution.py"]


def test_docker_wrap_omits_unset_resource_caps_but_keeps_privilege_hardening(monkeypatch):
    argv = _wrap_argv(monkeypatch)   # mem/cpus unset -> --memory/--cpus omitted
    assert "--memory" not in argv and "--cpus" not in argv
    # privilege hardening is unconditional
    assert "--cap-drop" in argv and "--security-opt" in argv


def test_docker_wrap_forwards_env(monkeypatch):
    argv = _wrap_argv(monkeypatch, env={"LOOPLAB_EVAL_SEED": "7"})
    i = argv.index("-e")
    assert argv[i + 1] == "LOOPLAB_EVAL_SEED=7"


def test_docker_wrap_preserves_posix_absolute_bind_path(monkeypatch, tmp_path):
    """A Windows Docker client must not prefix a Linux symlink target with its current drive."""
    import shutil
    from looplab.runtime import command_eval as ce
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    native = (tmp_path / "native-data").resolve().as_posix()
    wrap = ce.make_docker_wrap(
        str(tmp_path), "img",
        binds=[("/data/../data/raw", True), (str(tmp_path / "native-data"), False)],
    )
    argv = wrap(["python", "solution.py"], str(tmp_path))
    assert "type=bind,src=/data/raw,dst=/data/raw,readonly" in argv
    assert f"type=bind,src={native},dst={native}" in argv


# ---------------------------------------------------- path-boundary + timeout hardening (P0-7 / P1-5)
def test_stage_name_must_be_a_safe_slug():
    from looplab.runtime.command_eval import validate_stages, safe_stage_name
    for bad in ("../escape", "..\\escape", "C:\\temp\\x", "\\", "a/b", "..", ".", "a\x00b", "a b"):
        assert not safe_stage_name(bad), bad
        clean, err = validate_stages([{"name": bad, "command": ["python", "x.py"]}])
        assert clean is None and err                       # rejected at authoring/consume time
    for good in ("train", "data_prep", "eval-v1", "stage.1", "s2"):
        assert safe_stage_name(good), good
    clean, err = validate_stages([{"name": "data_prep", "command": ["python", "x.py"]}])
    assert err is None and clean and clean[0]["name"] == "data_prep"


def test_stage_timeout_must_be_finite_positive():
    from looplab.runtime.command_eval import validate_stages
    import math
    for bad in (float("nan"), float("inf"), -1.0, 0):
        clean, err = validate_stages([{"name": "t", "command": ["python", "x.py"], "timeout": bad}])
        assert clean is None and "finite" in err, bad
    clean, err = validate_stages([{"name": "t", "command": ["python", "x.py"], "timeout": 30}])
    assert err is None and clean[0]["timeout"] == 30.0


def test_finite_timeout_bounds_pathological_values():
    from looplab.runtime.sandbox import finite_timeout, MAX_TIMEOUT_S
    assert finite_timeout(float("nan"), 600.0) == 600.0            # fail-open -> the default
    assert finite_timeout(float("inf"), 600.0) == 600.0            # fail-open -> the default
    assert finite_timeout("nope", 600.0) == 600.0                  # unparseable -> the default
    assert finite_timeout(-5, 600.0) == 0.0                        # fail-safe negative -> clamped to 0
    assert finite_timeout(0, 600.0) == 0.0                         # honored sentinel (profile timeout:0)
    assert finite_timeout(1e18, 600.0) == MAX_TIMEOUT_S            # huge finite -> clamped to the ceiling
    assert finite_timeout(45.0, 600.0) == 45.0                    # a normal value is unchanged


def test_adapter_path_cannot_escape_the_workdir(tmp_path):
    # An adapter module OUTSIDE the workdir must NOT be exec'd (a `../` code-exec escape).
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.py").write_text("def read_metric(_): return 9.0\n", encoding="utf-8")
    wd = tmp_path / "wd"
    wd.mkdir()
    assert read_metric("", str(wd), {"kind": "adapter", "path": "../outside/evil.py"}) is None
    assert read_metric("", str(wd), {"kind": "adapter", "path": str(outside / "evil.py")}) is None
    # a contained adapter still works
    (wd / "ok.py").write_text("def read_metric(_): return 0.5\n", encoding="utf-8")
    assert read_metric("", str(wd), {"kind": "adapter", "path": "ok.py"}) == 0.5


def test_host_score_predictions_must_be_inside_the_workdir(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    # a planted predictions file OUTSIDE the workdir must not be read (would score perfectly)
    (tmp_path / "planted.json").write_text('{"predictions": [1, 2, 3]}', encoding="utf-8")
    (tmp_path / "labels.json").write_text('{"predictions": [1, 2, 3]}', encoding="utf-8")
    spec = {"kind": "host_score", "scorer": "rmse", "predictions": "../planted.json",
            "labels": str(tmp_path / "labels.json")}
    assert read_metric("", str(wd), spec) is None
    # an in-workdir predictions file scores normally
    (wd / "predictions.json").write_text('{"predictions": [1, 2, 3]}', encoding="utf-8")
    spec2 = {"kind": "host_score", "scorer": "rmse", "predictions": "predictions.json",
             "labels": str(tmp_path / "labels.json")}
    assert read_metric("", str(wd), spec2) == 0.0
