"""Failure diagnostics (doc 78 §3; audit 2026-10-14 L-07, incidents C1 and G0): the record names the
ORIGINAL exception under a wrapper, deaths by signal per rank and exit codes; earlier attempts'
logs survive a workdir rebuild; an external run's evaluations get crash diagnostics by default."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

from looplab.engine.attempt_logs import archive_attempt_logs, list_attempt_logs
from looplab.runtime.failure_forensics import (failure_forensics, parse_exceptions,
                                               parse_torchrun, signal_name)

G0_LOG = """\
[rank3]: step 5861 loss=0.41
[rank5]: step 5861 loss=0.40
W1014 08:43:10.1 torch/distributed/elastic/multiprocessing/api.py] Sending process 12 closing signal SIGTERM
Traceback (most recent call last):
  File "/usr/bin/torchrun", line 8, in <module>
    sys.exit(main())
torch.distributed.elastic.multiprocessing.errors.ChildFailedError:
============================================================
sft.py FAILED
------------------------------------------------------------
Failures:
[1]:
  time      : 2026-10-14_08:43:10
  host      : box
  rank      : 5 (local_rank: 5)
  exitcode  : -11 (pid: 4242)
  error_file: <N/A>
  traceback : Signal 11 (SIGSEGV) received by PID 4242
------------------------------------------------------------
Root Cause (first observed failure):
[0]:
  time      : 2026-10-14_08:43:09
  host      : box
  rank      : 3 (local_rank: 3)
  exitcode  : -11 (pid: 4240)
  error_file: <N/A>
  traceback : Signal 11 (SIGSEGV) received by PID 4240
============================================================
Traceback (most recent call last):
  File "MiniOneRec/looplab/g_stage_guard.py", line 115, in <module>
    assert rc == 0, ('G0', 'stage1', rc)
AssertionError: ('G0', 'stage1', 1)
"""

C1_LOG = """\
loading view
Traceback (most recent call last):
  File "sft.py", line 175, in <module>
    pieces = json.load(open(path))
FileNotFoundError: [Errno 2] No such file or directory: '/data/view/compact_pieces.json'
Traceback (most recent call last):
  File "two_stage_guard.py", line 43, in <module>
    raise AssertionError('Stage failed', 'stage2')
AssertionError: ('Stage failed', 'stage2')
"""


def test_g0_sigsegv_on_ranks_is_named_beside_the_wrapper():
    rec = failure_forensics(exit_code=1, stdout="", stderr="", logs={"stage1.log": G0_LOG},
                            failed_stage="stage1",
                            stage_rows=[{"name": "stage1", "exit_code": 1, "status": "fail"}])
    torchrun = [s for s in rec["signals"] if s.get("source") == "torchrun"]
    assert torchrun == [{"signal": "SIGSEGV", "ranks": [3, 5], "source": "torchrun"}]
    assert rec["exit_codes"] == {"stage stage1": 1, "rank 3": -11, "rank 5": -11}
    assert "SIGSEGV on rank(s) 3,5" in rec["headline"] and rec["headline"].startswith("stage stage1:")
    assert rec["root_exception"]["type"] == "AssertionError"   # no child exception was printed
    assert next(r for r in rec["ranks"] if r["rank"] == 3)["root_cause"] is True
    assert rec["rank_tails"]["rank 3"] == ["step 5861 loss=0.41"]


def test_c1_original_file_not_found_wins_over_the_wrapper():
    rec = failure_forensics(exit_code=1, logs={"stage2.log": C1_LOG}, failed_stage="stage2")
    assert rec["root_exception"]["type"] == "FileNotFoundError"
    assert "compact_pieces.json" in rec["root_exception"]["message"]
    assert [e["type"] for e in rec["exceptions"]] == ["FileNotFoundError", "AssertionError"]
    assert "FileNotFoundError" in rec["headline"] and "(wrapper: AssertionError)" in rec["headline"]


def test_chained_exceptions_mark_the_handler_side_as_wrapper():
    text = ("Traceback (most recent call last):\n  File 'a.py'\nKeyError: 'lr'\n\n"
            "During handling of the above exception, another exception occurred:\n\n"
            "Traceback (most recent call last):\n  File 'b.py'\nRuntimeError: config broken\n")
    excs = parse_exceptions(text, source="stderr")
    assert [(e["type"], e["chained"]) for e in excs] == [("KeyError", False), ("RuntimeError", True)]
    rec = failure_forensics(exit_code=1, stderr=text)
    assert rec["root_exception"]["type"] == "KeyError"


def test_signals_from_exit_codes_shells_subprocess_and_faulthandler():
    assert signal_name(-11) == "SIGSEGV" and signal_name(139) == "SIGSEGV"
    assert signal_name(1) is None and signal_name(None) is None
    text = ("Fatal Python error: Segmentation fault\n\nCurrent thread 0x1 (most recent call first):\n"
            "  File \"model.py\", line 10 in forward\n"
            "subprocess.CalledProcessError: Command died with <Signals.SIGKILL: 9>.\n"
            "run.sh: line 3: 99 Segmentation fault (core dumped) python x.py\n")
    rec = failure_forensics(exit_code=-6, stderr=text)
    names = [s["signal"] for s in rec["signals"]]
    assert names[0] == "SIGABRT" and "SIGKILL" in names and "SIGSEGV" in names
    assert rec["faulthandler"]["error"] == "Segmentation fault"
    assert any("forward" in line for line in rec["faulthandler"]["stack"])


def test_redaction_reaches_every_string_and_the_record_stays_bounded():
    secret = "sk-ant-api03-" + "Z" * 40
    big = "\n".join(f"[rank{r}]: line {i} {secret}" for r in range(64) for i in range(50))
    rec = failure_forensics(exit_code=1, stderr=f"ValueError: {secret}\n", logs={"x.log": big},
                            redact=lambda s: s.replace(secret, "***"))
    blob = json.dumps(rec)
    assert secret not in blob and len(blob) <= 24_000


def test_torchrun_parser_merges_repeated_root_cause_rows():
    rows = parse_torchrun(G0_LOG)
    assert [(r["rank"], r["exitcode"], r["signal"]) for r in rows] == [
        (3, -11, "SIGSEGV"), (5, -11, "SIGSEGV")]


def test_engine_attaches_forensics_from_the_failed_stage_log(tmp_path):
    from looplab.engine.evaluate import EvaluateMixin

    workdir = tmp_path / "node_0"
    workdir.mkdir()
    (workdir / "stage2.log").write_text(C1_LOG)
    host = SimpleNamespace(_redact=lambda s: s.replace("/data", "<data>"))
    a = SimpleNamespace(workdir=workdir, res=SimpleNamespace(
        exit_code=1, timed_out=False, stdout="", stderr="AssertionError: ('Stage failed',)",
        failed_stage="stage2", stages=[{"name": "stage2", "exit_code": 1}]))
    rec = EvaluateMixin._failure_forensics(host, a)
    assert rec["root_exception"]["type"] == "FileNotFoundError"
    assert "<data>/view" in rec["root_exception"]["message"]
    assert rec["log_tails"]["stage2.log"][-1].startswith("AssertionError")


def test_a_rebuilt_workdir_keeps_the_previous_attempts_logs(tmp_path):
    run = tmp_path / "run"
    wd = run / "nodes" / "node_2"
    wd.mkdir(parents=True)
    (wd / "stage1.log").write_text("first attempt FileNotFoundError\n")
    (wd / "eval.log").write_text("eval\n")
    (wd / "empty.log").write_text("")
    (wd / "model.bin").write_bytes(b"x")
    target = archive_attempt_logs(run, wd, 2, 1, now=0)
    assert target.name == "before-gen1-19700101T000000Z"
    assert sorted(p.name for p in target.iterdir()) == ["eval.log", "stage1.log"]
    assert (wd / "model.bin").exists() and not (wd / "stage1.log").exists()
    listing = list_attempt_logs(run, 2)
    assert listing[0]["archive"] == target.name
    assert listing[0]["files"][1]["path"] == f"attempt_logs/node_2/{target.name}/stage1.log"
    assert archive_attempt_logs(run, wd, 2, 2) is None          # nothing left to keep


def test_workspace_materialize_archives_before_it_removes(tmp_path, monkeypatch):
    from looplab.engine import workspace

    run = tmp_path / "run"
    wd = run / "nodes" / "node_0"
    wd.mkdir(parents=True)
    (wd / "stage1.log").write_text("old attempt\n")
    seen = {}

    def fake_archive(run_dir, workdir, node_id, generation):
        seen["args"] = (str(run_dir), str(workdir), node_id, generation)
        seen["log_present"] = (workdir / "stage1.log").exists()

    monkeypatch.setattr("looplab.engine.attempt_logs.archive_attempt_logs", fake_archive)
    engine = SimpleNamespace(_repo_spec={}, run_dir=run, store=None,
                             _seed_workspace=lambda wd, node=None: None,
                             _write_node_files=lambda node, wd: None,
                             _write_assets=lambda wd: None)
    seeder = workspace.WorkspaceSeeder(engine)
    seeder.materialize(SimpleNamespace(id=0, attempt=3, files={}, deleted=[]), wd)
    assert seen["log_present"] is True and seen["args"][2:] == (0, 3)


def test_external_runs_get_crash_diagnostics_env_lowest_precedence():
    from looplab.engine.eval_dispatch import CRASH_DIAGNOSTICS_ENV, EvalDispatchMixin

    ext = SimpleNamespace(external_harness=True, _eval_env={})
    env = EvalDispatchMixin._declared_eval_env(ext, None, {})
    assert env == CRASH_DIAGNOSTICS_ENV
    override = SimpleNamespace(external_harness=True, _eval_env={"PYTHONFAULTHANDLER": ""})
    assert EvalDispatchMixin._declared_eval_env(override, {"A": "1"}, {})["PYTHONFAULTHANDLER"] == ""
    internal = SimpleNamespace(external_harness=False, _eval_env={})
    assert EvalDispatchMixin._declared_eval_env(internal, None, {}) is None


def test_attempt_logs_route_lists_archives_readable_through_the_artifact_route(tmp_path):
    from fastapi.testclient import TestClient
    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    root = tmp_path / "runs"
    run = root / "demo"
    wd = run / "nodes" / "node_0"
    wd.mkdir(parents=True)
    EventStore(run / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    (wd / "stage1.log").write_text("FileNotFoundError: x\n")
    archive_attempt_logs(run, wd, 0, 1)
    client = TestClient(make_app(root))
    body = client.get("/api/runs/demo/attempt-logs", params={"node_id": 0}).json()
    path = body["archives"][0]["files"][0]["path"]
    assert path.startswith("attempt_logs/node_0/before-gen1-")
    assert client.get("/api/runs/demo/attempt-logs", params={"node_id": 7}).json()["archives"] == []


def test_failure_forensics_route_returns_newest_attempt_first(tmp_path):
    from fastapi.testclient import TestClient
    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    root = tmp_path / "runs"
    run = root / "demo"
    run.mkdir(parents=True)
    store = EventStore(run / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print(1)"})
    first = failure_forensics(exit_code=1, logs={"stage2.log": C1_LOG}, failed_stage="stage2")
    store.append("node_failed", {"node_id": 0, "generation": 0, "error": "x", "reason": "crash",
                                 "failure_forensics": first})
    body = TestClient(make_app(root)).get("/api/runs/demo/nodes/0/failure-forensics").json()
    assert body["attempts"][0]["forensics"]["root_exception"]["type"] == "FileNotFoundError"
    assert body["attempt_logs"] == []
