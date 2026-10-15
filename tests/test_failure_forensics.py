"""Failure diagnostics (doc 78 §3; audit 2026-10-14 L-07, incidents C1 and G0): the record names the
ORIGINAL exception under a wrapper, deaths by signal per rank and exit codes; earlier attempts'
logs survive a workdir rebuild; an external run's evaluations get crash diagnostics by default."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

from looplab.engine.attempt_logs import archive_attempt_logs, list_attempt_logs
from looplab.core.failure_forensics import (failure_forensics, parse_exceptions,
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


class _Host:
    """The engine members `_failure_forensics` reads: the redaction funnel and the run directory
    (where a failed canary's scratch tree lives)."""

    def __init__(self, run_dir=None, redact=lambda s: s):
        from looplab.engine.evaluate import EvaluateMixin

        self.run_dir = run_dir
        self._redact = redact
        for name in ("_failure_forensics", "_failure_forensics_job", "_canary_scratch"):
            setattr(self, name, getattr(EvaluateMixin, name).__get__(self))


def _attempt(workdir, *, snapshot=None, failed_stage="stage2", stderr=""):
    return SimpleNamespace(workdir=workdir, _log_snapshot=snapshot, _marker_snapshot=None,
                           canary_failed=False, canary_failed_stage=None, node_id=0,
                           res=SimpleNamespace(exit_code=1, timed_out=False, stdout="",
                                               stderr=stderr, failed_stage=failed_stage,
                                               stages=[{"name": failed_stage, "exit_code": 1}]))


SECOND_ATTEMPT_LOG = """\
Traceback (most recent call last):
  File "sft.py", line 12, in <module>
    lr = float(args.lr)
ValueError: could not convert string to float: 'fast'
"""


def test_engine_attaches_forensics_from_the_failed_stage_log(tmp_path):
    from looplab.engine.eval_log_plan import snapshot_training_logs

    workdir = tmp_path / "node_0"
    workdir.mkdir()
    before = snapshot_training_logs(workdir)          # taken before the attempt wrote a byte
    (workdir / "stage2.log").write_text(C1_LOG)
    host = _Host(redact=lambda s: s.replace("/data", "<data>"))
    a = _attempt(workdir, snapshot=before, stderr="AssertionError: ('Stage failed',)")
    rec = host._failure_forensics(a)
    assert rec["root_exception"]["type"] == "FileNotFoundError"
    assert "<data>/view" in rec["root_exception"]["message"]
    assert rec["log_tails"]["stage2.log"][-1].startswith("AssertionError")


def test_a_repair_attempt_s_record_reads_only_its_own_bytes_of_an_appended_stage_log(tmp_path):
    """Stage logs APPEND across the repair attempts of one lifecycle (the workdir is reused), so the
    record of attempt N must start at N's byte floor — the snapshot taken when N started — or it
    names N-1's `FileNotFoundError` as the cause of N's `ValueError`."""
    from looplab.engine.eval_log_plan import snapshot_training_logs

    workdir = tmp_path / "node_0"
    workdir.mkdir()
    (workdir / "stage2.log").write_text(C1_LOG)                  # attempt N-1
    before = snapshot_training_logs(workdir)
    with open(workdir / "stage2.log", "a") as fh:                # attempt N appends
        fh.write(SECOND_ATTEMPT_LOG)
    rec = _Host()._failure_forensics(_attempt(workdir, snapshot=before))
    assert rec["root_exception"]["type"] == "ValueError", rec["root_exception"]
    assert [e["type"] for e in rec["exceptions"]] == ["ValueError"], rec["exceptions"]
    assert "compact_pieces" not in json.dumps(rec), "attempt N-1's bytes reached attempt N's record"


def test_with_no_attempt_floor_the_record_reads_no_log_at_all(tmp_path):
    """No snapshot means nobody can say where this attempt's bytes begin: the record is built from
    this attempt's own streams, never from a log tail that may be an earlier attempt's."""
    workdir = tmp_path / "node_0"
    workdir.mkdir()
    (workdir / "stage2.log").write_text(C1_LOG)
    rec = _Host()._failure_forensics(_attempt(workdir, stderr=SECOND_ATTEMPT_LOG))
    assert rec["root_exception"]["type"] == "ValueError"
    assert "stage2.log" not in rec.get("log_tails", {}) and "FileNotFoundError" not in json.dumps(rec)


def test_a_failed_canary_s_record_reads_the_canary_scratch_not_the_node_workdir(tmp_path):
    """The canary ran in `<run>/canary/node_<id>`; the node's workdir holds an EARLIER attempt's
    logs, and the canary's failure result carries no `failed_stage` by design."""
    from looplab.engine.eval_log_plan import snapshot_training_logs

    run = tmp_path / "run"
    workdir = run / "nodes" / "node_0"
    workdir.mkdir(parents=True)
    (workdir / "eval.log").write_text(C1_LOG)                    # the previous full eval's
    (workdir / "train.log").write_text(C1_LOG)
    scratch = run / "canary" / "node_0"
    scratch.mkdir(parents=True)
    (scratch / "train.log").write_text(SECOND_ATTEMPT_LOG)
    a = _attempt(workdir, snapshot=snapshot_training_logs(workdir), failed_stage=None)
    a.res.stages = None
    a.canary_failed, a.canary_failed_stage = True, "train"
    rec = _Host(run_dir=run)._failure_forensics(a)
    assert rec["root_exception"]["type"] == "ValueError", rec["root_exception"]
    assert "train.log" in rec["log_tails"] and "FileNotFoundError" not in json.dumps(rec)


def test_salvage_builds_the_record_off_the_loop_and_goes_on_without_a_hung_one(tmp_path,
                                                                                monkeypatch):
    """The record opens workdir files (0.1-1 s a lookup on geesefs/S3, forever on a hung mount): it
    runs in a worker thread, and SALVAGE waits at most `_FORENSICS_TIMEOUT_S` for it."""
    import threading

    import anyio
    from factories import make_engine
    from looplab.engine import evaluate
    from looplab.events.replay import fold
    from looplab.runtime.command_eval import RunResult

    eng = make_engine(tmp_path / "run")
    eng._inline_repair = False
    eng.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {"x": 0.0}},
                                      "code": "print(1)"})
    eng._run_eval = lambda *a, **k: RunResult(exit_code=1, stdout="", metric=None,
                                              timed_out=False, stderr=SECOND_ATTEMPT_LOG)
    release = threading.Event()
    seen: dict = {}

    def job_for(a):
        def job():
            seen["worker"] = threading.get_ident()
            release.wait(10)                                     # a read on a hung mount
            return {"root_exception": {"type": "LateError"}}
        return job

    monkeypatch.setattr(eng, "_failure_forensics_job", job_for)
    monkeypatch.setattr(evaluate, "_FORENSICS_TIMEOUT_S", 0.3)

    async def main():
        seen["loop"] = threading.get_ident()
        await eng._evaluate(0, anyio.CapacityLimiter(1), None)

    try:
        anyio.run(main)
    finally:
        release.set()
    assert seen["worker"] != seen["loop"], "the record was built on the loop thread"
    assert fold(eng.store.read_all()).nodes[0].status.value == "failed"
    failed = [e for e in eng.store.read_all() if e.type == "node_failed"]
    assert failed and "failure_forensics" not in failed[-1].data, "a timed-out record must be absent"


def test_only_the_node_s_own_workdir_is_archived_as_its_earlier_attempts(tmp_path):
    """A canary scratch, a confirm and a noise-floor seed dir are materialized through the same
    method; their logs are not the node's earlier attempts and must not use up (and prune) the
    node's archive slots."""
    from looplab.engine import workspace

    run = tmp_path / "run"
    engine = SimpleNamespace(_repo_spec={}, run_dir=run, store=None,
                             _seed_workspace=lambda wd, node=None: None,
                             _write_node_files=lambda node, wd: None,
                             _write_assets=lambda wd: None)
    seeder = workspace.WorkspaceSeeder(engine)
    node = SimpleNamespace(id=0, attempt=1, files={}, deleted=[])
    for other in (run / "canary" / "node_0", run / "confirm" / "node_0_g1_seed_0",
                  run / "noise" / "node_0_g1_seed_1"):
        other.mkdir(parents=True)
        (other / "eval.log").write_text("canary / confirm / noise output\n")
        seeder.materialize(node, other)
    assert list_attempt_logs(run, 0) == [], "a non-lifecycle tree was archived as node 0's attempt"
    own = run / "nodes" / "node_0"
    own.mkdir(parents=True)
    (own / "eval.log").write_text("the node's own earlier attempt\n")
    seeder.materialize(node, own)
    assert len(list_attempt_logs(run, 0)) == 1


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
