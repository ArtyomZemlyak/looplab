"""A name the candidate plants in its own workdir must not redirect an ENGINE write, or block one.

The fourth critic pass over the candidate-input hardening (2026-09-26) drove the WRITE side of the
class the earlier passes closed on the read side. The node workdir is the candidate's cwd, the
engine keeps writing into it after the candidate's first stage has run, and a native child (`ln`,
`mkfifo` from a shell or a compiled binary) never meets the read fence's audit hook:

  * a stage that ran `ln -s ../../events.jsonl train.log` had the NEXT stage's stdout appended to
    the run's event log by the eval tee — after which every append failed and the loop spun (6,052
    spans in 90 s, no terminal); a stage that ran `os.mkfifo('train.log')` hung the tee's open;
  * `ln -sf ../../events.jsonl mlebench_report.json` let the finish-phase report write TRUNCATE the
    event log — the run restarted at seq 0 with `holdout_evaluated`;
  * the same shape reached the workdir stamps, the task assets written into a reused workdir, and
    `solution.py` rewritten for a repair; a hard link reaches all of them past `resolve()`;
  * and a FIFO swapped in behind a check hung the numeric contract's log tail, the live judges' log
    reads, the candidate-file reader and the configuration-carrier reader.

Each is driven here against a sentinel standing in for `events.jsonl`, which must come out untouched.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._posix_gates import MODE_BITS, POSIX_ONLY_OS_CALLS

SENTINEL = "{\"seq\": 0, \"type\": \"run_started\"}\n"


def _returns_promptly(fn, *args, seconds=10.0, **kwargs):
    """`fn(*args, **kwargs)` on a daemon thread: its value, or its exception RE-RAISED here — and a
    failure if it is still running after `seconds`, which is what blocking on a FIFO looks like."""
    import threading

    box: dict = {}

    def _call():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — handed to the test thread and re-raised there
            box["error"] = exc

    worker = threading.Thread(target=_call, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"{getattr(fn, '__name__', fn)} blocked"
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _sentinel(tmp_path) -> Path:
    events = tmp_path / "events.jsonl"
    events.write_text(SENTINEL, encoding="utf-8")
    return events


def _workdir(tmp_path) -> Path:
    wd = tmp_path / "nodes" / "node_0"
    wd.mkdir(parents=True)
    return wd


# --------------------------------------------------------------------- the eval tee's append
@POSIX_ONLY_OS_CALLS
def test_the_tee_log_open_refuses_a_link_a_hard_link_and_a_fifo(tmp_path):
    from looplab.core.node_evidence import open_untrusted_append

    events, wd = _sentinel(tmp_path), _workdir(tmp_path)
    (wd / "link.log").symlink_to(events)
    os.link(events, wd / "hard.log")
    os.mkfifo(wd / "fifo.log")
    for name in ("link.log", "hard.log", "fifo.log"):
        with pytest.raises(OSError):
            _returns_promptly(open_untrusted_append, wd / name)
    assert events.read_text(encoding="utf-8") == SENTINEL
    # …and a log it owns is created, then appended to, as `open(path, "a")` did.
    for line in ("a\n", "b\n"):
        with open_untrusted_append(wd / "train.log") as fh:
            fh.write(line)
    assert (wd / "train.log").read_text(encoding="utf-8") == "a\nb\n"


@POSIX_ONLY_OS_CALLS
@pytest.mark.parametrize("plant", ["symlink", "hardlink", "fifo"])
def test_a_planted_stage_log_neither_receives_the_output_nor_blocks_the_stage(tmp_path, plant):
    from looplab.runtime.sandbox import run_argv

    events, wd = _sentinel(tmp_path), _workdir(tmp_path)
    log = wd / "train.log"
    {"symlink": lambda: log.symlink_to(events), "hardlink": lambda: os.link(events, log),
     "fifo": lambda: os.mkfifo(log)}[plant]()
    rc, out, _err, timed_out = _returns_promptly(
        run_argv, [sys.executable, "-c", "print('stage output')"], str(wd), 30.0,
        log_path=str(log), seconds=30.0)
    assert rc == 0 and not timed_out and "stage output" in out, "the stage still ran and drained"
    assert events.read_text(encoding="utf-8") == SENTINEL


@POSIX_ONLY_OS_CALLS
def test_the_numeric_contract_log_tail_does_not_wait_on_a_fifo(tmp_path):
    from looplab.runtime.command_eval import _attempt_log_tail

    wd = _workdir(tmp_path)
    os.mkfifo(wd / "train.log")
    assert _returns_promptly(_attempt_log_tail, wd / "train.log", None) is None


# --------------------------------------------------------------------- engine writes into the workdir
@POSIX_ONLY_OS_CALLS
@pytest.mark.parametrize("plant", ["symlink", "hardlink"])
def test_the_report_and_the_stamps_replace_a_planted_name(tmp_path, monkeypatch, plant):
    from looplab.adapters import mlebench_grade
    from looplab.core.models import Idea, Node
    from looplab.engine.evaluate import EvalAttempt, _workdir_manifest_digest
    from looplab.engine.holdout import HoldoutGrader

    events, wd = _sentinel(tmp_path), _workdir(tmp_path)
    for name in ("mlebench_report.json", ".looplab-manifest", ".looplab-superseded"):
        (wd / name).symlink_to(events) if plant == "symlink" else os.link(events, wd / name)
    (wd / "submission.csv").write_text("id,label\n1,a\n", encoding="utf-8")
    monkeypatch.setattr(mlebench_grade, "grade_in_subprocess",
                        lambda *a, **k: (0.5, {"medal": "gold"}))
    engine = SimpleNamespace(_host_grader={"kind": "mlebench", "competition": "c"},
                             _holdout_idx=None, _search_answers=None)
    res = SimpleNamespace(metric=None, extra_metrics={})
    HoldoutGrader(engine).apply_host_grade(res, str(wd))
    assert res.metric == 0.5
    attempt = EvalAttempt(node_id=0)
    attempt.workdir, attempt.generation = wd, 3
    attempt._manifest_stamp = wd / ".looplab-manifest"
    attempt._superseded_marker = wd / ".looplab-superseded"
    node = Node(id=0, operator="draft", idea=Idea(operator="draft", params={}, rationale="r"),
                code="print(1)")
    attempt.stamp_workdir(node)
    attempt.mark_superseded_workdir()
    assert events.read_text(encoding="utf-8") == SENTINEL
    assert (wd / "mlebench_report.json").read_text(encoding="utf-8") == '{"medal": "gold"}'
    assert attempt.workdir_matches(node)
    assert (wd / ".looplab-manifest").read_text() == _workdir_manifest_digest(node)
    assert (wd / ".looplab-superseded").read_text() == "3"
    for name in ("mlebench_report.json", ".looplab-manifest", ".looplab-superseded"):
        assert not (wd / name).is_symlink() and os.stat(wd / name).st_nlink == 1


@POSIX_ONLY_OS_CALLS
@pytest.mark.parametrize("plant", ["symlink", "hardlink"])
def test_assets_node_files_and_the_solution_replace_a_planted_name(tmp_path, plant):
    from looplab.engine.workspace import WorkspaceSeeder
    from looplab.runtime.sandbox import SubprocessSandbox

    events, wd = _sentinel(tmp_path), _workdir(tmp_path)
    for name in ("grader.py", "helper.py", "solution.py"):
        (wd / name).symlink_to(events) if plant == "symlink" else os.link(events, wd / name)
    engine = SimpleNamespace(_assets={"grader.py": "ANSWERS = 1\n"},
                             _repo_spec={"protected_names": []})
    seeder = WorkspaceSeeder(engine)
    seeder.write_node_files(SimpleNamespace(files={"helper.py": "X = 2\n"}, deleted=[]), wd)
    seeder.write_assets(wd)
    result = SubprocessSandbox().run("print('{\"metric\": 1.0}')\n", str(wd), timeout=30.0)
    assert result.metric == 1.0
    assert events.read_text(encoding="utf-8") == SENTINEL
    assert (wd / "grader.py").read_text(encoding="utf-8") == "ANSWERS = 1\n"
    if plant == "hardlink":
        # A symlink's target sits OUTSIDE the workdir, so `write_node_files` skips that name
        # (`resolve()` escapes); a hard link is invisible to `resolve()` and is replaced instead.
        assert (wd / "helper.py").read_text(encoding="utf-8") == "X = 2\n"


@MODE_BITS
def test_what_a_sandboxed_child_must_read_is_published_world_readable(tmp_path):
    from looplab.core.atomicio import atomic_write_text

    atomic_write_text(tmp_path / "asset.py", "x\n", mode=0o644)
    atomic_write_text(tmp_path / "stamp", "y\n")
    assert stat.S_IMODE(os.stat(tmp_path / "asset.py").st_mode) == 0o644
    assert stat.S_IMODE(os.stat(tmp_path / "stamp").st_mode) == 0o600


# --------------------------------------------------------------------- readers that reopen by path
@POSIX_ONLY_OS_CALLS
def test_the_candidate_file_readers_do_not_wait_on_a_fifo_and_still_follow_a_link(tmp_path):
    from looplab.runtime import command_eval
    from looplab.runtime.applied_params import _read

    wd = _workdir(tmp_path)
    os.mkfifo(wd / "predictions.json")
    assert _returns_promptly(command_eval.read_candidate_file, wd / "predictions.json") is None
    assert _returns_promptly(_read, wd / "predictions.json") is None
    # A link INSIDE the workdir is still read — confinement is the caller's — with `read_text`'s
    # own decoding, BOM and universal newlines included.
    (wd / "out").mkdir()
    (wd / "out" / "submission.csv").write_bytes(b"\xef\xbb\xbfid,label\r\n1,a\r2,b\n")
    (wd / "submission.csv").symlink_to(wd / "out" / "submission.csv")
    expected = (wd / "out" / "submission.csv").read_text(encoding="utf-8-sig", errors="replace")
    assert command_eval.read_candidate_file(wd / "submission.csv") == expected == "id,label\n1,a\n2,b\n"
    assert _read(wd / "submission.csv").startswith("﻿id,label")


def test_a_candidate_file_over_the_ceiling_is_refused_not_truncated(tmp_path, monkeypatch):
    from looplab.runtime import command_eval

    (tmp_path / "big.json").write_text("1234", encoding="utf-8")
    monkeypatch.setattr(command_eval, "_MAX_METRIC_FILE_BYTES", 3)
    assert command_eval.read_candidate_file(tmp_path / "big.json") is None
    monkeypatch.setattr(command_eval, "_MAX_METRIC_FILE_BYTES", 4)
    assert command_eval.read_candidate_file(tmp_path / "big.json") == "1234"


@POSIX_ONLY_OS_CALLS
def test_a_live_judge_reading_a_log_swapped_for_a_fifo_does_not_block(tmp_path):
    from looplab.tools.log_tools import LogSource, _read_window

    wd = _workdir(tmp_path)
    os.mkfifo(wd / "train.log")
    source = LogSource(name="train", path=wd / "train.log", floor=0)
    with pytest.raises(OSError):
        _returns_promptly(_read_window, source, want=100, where="tail", start=None, ceiling=1000)


# --------------------------------------------------------------------- the rest of the class
def test_a_csv_the_module_cannot_parse_is_an_undecidable_split(tmp_path):
    """`csv.Error` is not a `ValueError`: one field past the csv module's limit raised out of the
    PRIVATE grade at finish, which caught only `(OSError, ValueError)` (critic 2026-09-26, driven)."""
    from looplab.adapters.mlebench_split import SplitUndecidable, filter_submission

    huge = "id,label\n1," + "x" * 200_000 + "\n"
    with pytest.raises(SplitUndecidable):
        filter_submission(huge, ["1"], keep=False)
    assert issubclass(SplitUndecidable, ValueError)


def test_a_name_no_filesystem_call_can_spell_is_unreadable_not_escaping(tmp_path):
    from looplab.runtime.metric_subject import bind_one

    row = bind_one(str(tmp_path), "\ud800.bin")
    assert row["bound"] is False and row["reason"] == "unreadable"
    assert "\ud800" not in row["path"]
