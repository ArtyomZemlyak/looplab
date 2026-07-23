"""command_eval metric readers: NaN/inf rejection, drift corroboration, regex/file readers, BOM
tolerance, adapter-reader refusal, and setup-vs-eval cwd separation.

Regressions consolidated from the code-review rounds (feasibility/NaN round, deep audit, RepoTask
/code-review findings)."""
from __future__ import annotations

import os
import sys

import pytest

from looplab.runtime.command_eval import (_MAX_STALL_S, _drift, _fmt, _stall_window,
                                           cap_gpu_flags, read_metric, run_command_eval)

_M = {"kind": "stdout_json", "key": "metric"}


def test_stall_window_respects_the_configurable_cap():
    # #6: the silence-before-kill window is min(cap, the stage's own deadline); cap defaults to
    # _MAX_STALL_S, and 0/None-cap DISABLES the watchdog. Never longer than the stage deadline.
    assert _stall_window(3600.0) == _MAX_STALL_S              # default cap (1800) clamps a long stage
    assert _stall_window(3600.0, 600.0) == 600.0             # a smaller operator cap wins
    assert _stall_window(120.0, 3600.0) == 120.0             # the stage's own deadline still bounds it
    assert _stall_window(3600.0, 0) is None                  # cap 0 => watchdog OFF (opt-out)
    assert _stall_window(0, 1800.0) is None                  # no/❭0 stage deadline => no stall window


def test_stall_cap_zero_disables_the_watchdog_while_a_small_cap_still_kills(tmp_path):
    # A permanently-silent script under a short deadline: a small stall_cap tree-kills it early (stalled),
    # while stall_cap=0 lets it run to the hard DEADLINE instead (timed_out, not stalled) — proving the
    # operator opt-out threads all the way to the subprocess watchdog.
    (tmp_path / "p.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    import time as _t

    t0 = _t.time()
    killed = run_command_eval([sys.executable, "p.py"], str(tmp_path), 8, _M, stall_cap=2)
    assert _t.time() - t0 < 7                                 # stall-killed at ~2s, not the 8s deadline
    assert killed.stalled is True and killed.timed_out is False

    t1 = _t.time()
    deadlined = run_command_eval([sys.executable, "p.py"], str(tmp_path), 4, _M, stall_cap=0)
    assert _t.time() - t1 >= 3                                # NOT stall-killed early: ran to the 4s deadline
    assert deadlined.timed_out is True and deadlined.stalled is False


# --- GPU-pin reconciliation: cap a hardcoded multi-GPU request to one device when pinned ----------
def test_cap_gpu_flags_caps_count_and_list_but_leaves_singles_and_unknowns():
    # A device COUNT >1 -> 1; a device LIST -> the single visible index 0; both `--flag N` and
    # `--flag=N` forms. A single/already-safe or non-numeric value is untouched, and a flag NOT in the
    # allowlist (here `--batch_size`) is never rewritten — capping an unrelated number would corrupt it.
    assert cap_gpu_flags(["train.py", "--gpus", "2"]) == ["train.py", "--gpus", "1"]
    assert cap_gpu_flags(["train.py", "--gpus=4"]) == ["train.py", "--gpus=1"]
    assert cap_gpu_flags(["train.py", "--gpus", "0,1"]) == ["train.py", "--gpus", "0"]
    assert cap_gpu_flags(["train.py", "--devices=0,1,2,3"]) == ["train.py", "--devices=0"]
    assert cap_gpu_flags(["t.py", "--num_gpus", "8", "--world_size=2"]) == \
        ["t.py", "--num_gpus", "1", "--world_size=1"]
    # COUNT flag carrying a device LIST/RANGE (really an index spec) -> the single visible index 0
    assert cap_gpu_flags(["t.py", "--gpus", "0-3"]) == ["t.py", "--gpus", "0"]        # range
    assert cap_gpu_flags(["t.py", "--devices", "3,"]) == ["t.py", "--devices", "0"]   # trailing-comma list
    # INDEX flag (WHICH gpu): only ordinal 0 is visible under the pin, so a count-style "1" would still
    # crash -> any positive index / list / range -> "0"; index 0 and "-1"/"auto" left alone.
    assert cap_gpu_flags(["train.py", "--gpu", "3"]) == ["train.py", "--gpu", "0"]
    assert cap_gpu_flags(["train.py", "--gpu=1"]) == ["train.py", "--gpu=0"]
    assert cap_gpu_flags(["train.py", "--gpu_id", "2"]) == ["train.py", "--gpu_id", "0"]
    assert cap_gpu_flags(["train.py", "--gpu", "0"]) == ["train.py", "--gpu", "0"]
    assert cap_gpu_flags(["train.py", "--gpu", "-1"]) == ["train.py", "--gpu", "-1"]
    # left alone: already-single, non-numeric, and non-GPU numeric flags
    assert cap_gpu_flags(["train.py", "--gpus", "1"]) == ["train.py", "--gpus", "1"]
    assert cap_gpu_flags(["train.py", "--gpus", "auto"]) == ["train.py", "--gpus", "auto"]
    assert cap_gpu_flags(["train.py", "--gpus", "-1"]) == ["train.py", "--gpus", "-1"]
    assert cap_gpu_flags(["train.py", "--batch_size", "256"]) == ["train.py", "--batch_size", "256"]
    # a trailing gpu flag with no value token must not IndexError
    assert cap_gpu_flags(["train.py", "--gpus"]) == ["train.py", "--gpus"]


def _gpus_probe(tmp_path):
    # A tiny eval whose METRIC is the numeric value it actually received for --gpus, so a run can
    # observe whether the engine rewrote the command it launched.
    (tmp_path / "p.py").write_text(
        'import sys,json; a=sys.argv\n'
        'g=a[a.index("--gpus")+1] if "--gpus" in a else "0"\n'
        'print(json.dumps({"metric": float(g)}))\n', encoding="utf-8")
    return [sys.executable, "p.py", "--gpus", "2"]


def test_run_command_eval_caps_multi_gpu_only_when_pinned_to_one_device(tmp_path):
    # When the engine pinned this eval to ONE gpu (single-index CUDA_VISIBLE_DEVICES in env), the
    # hardcoded `--gpus 2` is reconciled to `--gpus 1` so the 1-visible-GPU subprocess doesn't crash.
    cmd = _gpus_probe(tmp_path)
    pinned = run_command_eval(cmd, str(tmp_path), 60, _M,
                              env={**os.environ, "CUDA_VISIBLE_DEVICES": "3"})
    assert pinned.metric == 1.0                         # capped: the subprocess saw --gpus 1
    # Unpinned (single-experiment run: env is None) leaves the command verbatim — a serial run
    # legitimately has the whole multi-GPU box, so `--gpus 2` must stand.
    unpinned = run_command_eval(_gpus_probe(tmp_path), str(tmp_path), 60, _M, env=None)
    assert unpinned.metric == 2.0
    # A multi-device CVD (env present but naming >1 device) is NOT a single-GPU pin -> not capped.
    multi = run_command_eval(_gpus_probe(tmp_path), str(tmp_path), 60, _M,
                             env={**os.environ, "CUDA_VISIBLE_DEVICES": "0,1"})
    assert multi.metric == 2.0


def test_fmt_non_finite_param_does_not_crash():
    # _fmt does int(float(v)); int(inf/nan) raises OverflowError/ValueError, which would propagate out
    # of build_command with NO terminal event and crash the whole run. A non-finite param must format
    # to a harmless string instead (the node then fails on its own arg-parse, if at all).
    assert _fmt("inf") == "inf" and _fmt("-inf") == "-inf" and _fmt("nan") == "nan"
    assert _fmt("1e400") == "inf"
    huge = 10**400
    assert _fmt(huge) == str(huge)
    assert _fmt("50") == "50" and _fmt("3.5") == "3.5" and _fmt("x") == "x"   # normal cases unchanged


def test_host_score_spec_without_labels_fails_node_not_run(tmp_path):
    # A host_score metric spec missing its `labels` answer-key path is malformed; read_metric must
    # return None (fail the node) rather than raise KeyError (no terminal event -> crashes the run).
    assert read_metric("", str(tmp_path),
                       {"kind": "host_score", "predictions": "predictions.json", "scorer": "rmse"}) is None


# #3 — NaN/inf metric is rejected at read time (never enters best-selection)
def test_nan_metric_rejected(tmp_path):
    (tmp_path / "p.py").write_text(
        'import json; print(json.dumps({"metric": float("nan")}))\n', encoding="utf-8")
    res = run_command_eval([sys.executable, "p.py"], str(tmp_path), 60, _M)
    assert res.metric is None                          # NaN -> no metric, not a NaN best


def test_stall_watchdog_salvages_metric_printed_before_the_hang(tmp_path):
    # A single-command eval that prints its metric then HANGS silently (a distributed finalize deadlock /
    # wedged CUDA op) is stall-killed early AND still returns the metric it printed, flagged stalled=True,
    # so a train+eval that only deadlocked on teardown is salvaged rather than wasted.
    (tmp_path / "p.py").write_text(
        "import time,sys\nprint('RECALL@100: 0.5', flush=True)\ntime.sleep(60)\n", encoding="utf-8")
    spec = {"kind": "stdout_regex", "pattern": r"RECALL@100: ([0-9.]+)", "group": 1}
    import time as _t
    t0 = _t.time()
    res = run_command_eval([sys.executable, "p.py"], str(tmp_path), 30, spec, stall_timeout=2)
    assert _t.time() - t0 < 20            # killed at the stall window, not the 30s timeout / 60s sleep
    assert res.metric == 0.5             # the printed metric is SALVAGED
    assert res.stalled is True           # flagged as a stall (so the engine gate accepts it)
    assert res.timed_out is False        # a stall is not a deadline timeout


def test_stall_watchdog_does_not_salvage_when_no_metric_was_printed(tmp_path):
    # A stall with NO metric printed before the silence (hung mid-training) must NOT invent one: salvage
    # is self-gating on `metric is not None`.
    (tmp_path / "p.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    spec = {"kind": "stdout_regex", "pattern": r"RECALL@100: ([0-9.]+)", "group": 1}
    res = run_command_eval([sys.executable, "p.py"], str(tmp_path), 30, spec, stall_timeout=2)
    assert res.metric is None and res.stalled is True


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


def test_docker_wrap_never_forwards_secret_named_env(monkeypatch):
    """SECURITY: the untrusted tier's -e forwarding must strip secret-named host vars (mirrors
    sandbox.run_argv's SECRET_ENV filter). A pinned/CPU reservation's eval env carries the whole host
    environment; without this filter `docker run -e LLM_API_KEY=...` would hand credentials to
    adversarial candidate code (docker, unlike the subprocess tier, does not inherit — only -e reaches
    the container)."""
    argv = _wrap_argv(monkeypatch, env={
        "LOOPLAB_EVAL_SEED": "7", "LLM_API_KEY": "sk-secret", "AWS_SECRET_ACCESS_KEY": "creds",
        "GITHUB_TOKEN": "ghp_x", "DB_PASSWORD": "pw", "MY_CREDENTIAL": "c", "PATH": "/usr/bin",
    })
    forwarded = {argv[i + 1].split("=", 1)[0] for i, tok in enumerate(argv) if tok == "-e"}
    assert "LOOPLAB_EVAL_SEED" in forwarded and "PATH" in forwarded   # non-secret vars still pass
    for secret in ("LLM_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "DB_PASSWORD",
                   "MY_CREDENTIAL"):
        assert secret not in forwarded, secret
    joined = " ".join(argv)
    assert "sk-secret" not in joined and "creds" not in joined and "ghp_x" not in joined


def test_docker_gpu_env_strips_secrets_at_the_shared_choke_point():
    """SECURITY: the secret-strip lives in docker_gpu_env — the ONE choke point BOTH untrusted Docker
    callers (command_eval.make_docker_wrap and sandbox.DockerSandbox.run) forward via -e — so neither
    tier can leak a host credential to candidate code, and neither re-implements the filter."""
    from looplab.runtime.sandbox import docker_gpu_env
    env = {"LOOPLAB_EVAL_SEED": "7", "PATH": "/usr/bin", "LLM_API_KEY": "sk-secret",
           "AWS_SECRET_ACCESS_KEY": "creds", "HF_TOKEN": "hf_x", "CUDA_VISIBLE_DEVICES": "0"}
    # Positive pin: docker_gpu_argv owns the fence, so CUDA/NVIDIA are dropped; secrets are stripped too.
    out = docker_gpu_env(env, gpu_args=["--gpus", "device=0"])
    assert out == {"LOOPLAB_EVAL_SEED": "7", "PATH": "/usr/bin"}
    # No-GPU path (early return branch) must strip secrets just the same.
    out2 = docker_gpu_env({"LLM_API_KEY": "sk", "LOOPLAB_EVAL_SEED": "1"}, gpu_args=[])
    assert out2 == {"LOOPLAB_EVAL_SEED": "1"}


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
    for bad in (float("nan"), float("inf"), -1.0, 0):
        clean, err = validate_stages([{"name": "t", "command": ["python", "x.py"], "timeout": bad}])
        assert clean is None and "finite" in err, bad
    clean, err = validate_stages([{"name": "t", "command": ["python", "x.py"], "timeout": 30}])
    assert err is None and clean[0]["timeout"] == 30.0


def test_stage_count_is_bounded_before_execution_or_log_projection():
    from looplab.runtime.command_eval import MAX_STAGE_COUNT, validate_stages
    stages = [
        {"name": f"stage_{i}", "command": ["python", "x.py"]}
        for i in range(MAX_STAGE_COUNT + 1)
    ]
    clean, err = validate_stages(stages)
    assert clean is None and f"at most {MAX_STAGE_COUNT}" in err


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


def test_oversized_candidate_metric_file_is_rejected_not_slurped(tmp_path, monkeypatch):
    # DoS guard: the stdout path is byte-capped, but the FILE readers slurped a candidate-written file
    # whole via read_text on the HOST. Under the untrusted tier a candidate can write a multi-GB file
    # into its bind-mounted workdir, OOM-killing the engine. A file above the read ceiling must fail the
    # NODE (return None), not be read into host RAM.
    from looplab.runtime import command_eval
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "m.json").write_text('{"metric": 0.5}', encoding="utf-8")
    fj = {"kind": "file_json", "path": "m.json", "key": "metric"}
    assert read_metric("", str(wd), fj) == 0.5                             # within the ceiling: read normally
    monkeypatch.setattr(command_eval, "_MAX_METRIC_FILE_BYTES", 5, raising=False)
    assert read_metric("", str(wd), fj) is None                            # above the ceiling: rejected, not OOM
    # host_score predictions (also candidate-controlled) get the same bound; labels stay OUTSIDE the workdir
    (tmp_path / "labels.json").write_text('{"predictions": [1, 2, 3]}', encoding="utf-8")
    (wd / "predictions.json").write_text('{"predictions": [1, 2, 3]}', encoding="utf-8")
    hs = {"kind": "host_score", "scorer": "rmse", "predictions": "predictions.json",
          "labels": str(tmp_path / "labels.json")}
    assert read_metric("", str(wd), hs) is None                            # oversized preds rejected


def test_non_integer_regex_group_fails_the_node_not_the_run(tmp_path):
    # A non-integer `group` in an operator-authored regex metric must read as "no metric" (None), never
    # raise ValueError/TypeError out of read_metric and tear down the eval task with no terminal event
    # (a deterministic re-crash on every resume) — the "malformed spec fails the NODE, not the run"
    # contract every other branch of read_metric already honors.
    for bad in ("x", "1.5", None, [1], {}):
        assert read_metric("val=0.9", str(tmp_path),
                           {"kind": "stdout_regex", "pattern": r"val=([\d.]+)", "group": bad}) is None
    # a valid group still works, tolerant of a string spelling ("1") and a JSON float (1.0 -> group 1)
    for good in (1, "1", 1.0):
        assert read_metric("val=0.9", str(tmp_path),
                           {"kind": "stdout_regex", "pattern": r"val=([\d.]+)", "group": good}) == 0.9
