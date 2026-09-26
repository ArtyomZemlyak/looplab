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


# --------------------------------------------------------------------- the engine's own readers (5th pass)
def _repo_engine(tmp_path):
    from looplab.adapters.repo_task import NoOpRepoDeveloper, RepoParamResearcher
    from tests.factories import make_engine
    from tests.test_eval_protocol_comparability import _task

    return make_engine(tmp_path / "run", task=_task(), researcher=RepoParamResearcher({}),
                       developer=NoOpRepoDeveloper(), n_seeds=1, max_nodes=1)


@POSIX_ONLY_OS_CALLS
def test_the_stage_manifest_is_read_by_the_untrusted_rule(tmp_path, monkeypatch):
    """Critic 2026-09-26, driven: a FIFO planted as `looplab_stages.json` blocked the engine's event
    loop in `_resolve_stages` for good, and a link to `/dev/zero` cost a gigabyte of RSS."""
    import json

    from looplab.runtime import command_eval

    engine = _repo_engine(tmp_path)
    es = dict(engine._eval_spec)
    wd = _workdir(tmp_path)
    score = ([sys.executable, "ttrain_cli.py"], 60.0)
    os.mkfifo(wd / "looplab_stages.json")
    assert _returns_promptly(engine._resolve_stages, str(wd), es, None, *score) is None
    os.unlink(wd / "looplab_stages.json")
    (wd / "looplab_stages.json").symlink_to("/dev/zero")
    assert _returns_promptly(engine._resolve_stages, str(wd), es, None, *score) is None
    os.unlink(wd / "looplab_stages.json")
    manifest = {"stages": [{"name": "prep", "command": [sys.executable, "prep.py"]}]}
    (wd / "looplab_stages.json").write_text(json.dumps(manifest), encoding="utf-8")
    stages = engine._resolve_stages(str(wd), es, None, *score)
    assert [stage["name"] for stage in stages] == ["prep", "score"], "a regular manifest is read"
    monkeypatch.setattr(command_eval, "STAGE_MANIFEST_MAX_BYTES", 10)
    assert engine._resolve_stages(str(wd), es, None, *score) is None, "over the bound: no manifest"


@POSIX_ONLY_OS_CALLS
def test_a_module_the_reuse_closure_cannot_read_makes_the_stage_opaque(tmp_path):
    """Skipping an unreadable module — what the closure did on any error — drops its imports, the
    MISSED-dependency direction that scores a stale checkpoint; a FIFO also blocked the read."""
    from looplab.engine.eval_stages import EvalStagesMixin

    wd = _workdir(tmp_path)
    (wd / "train.py").write_text("import loss\nprint(1)\n", encoding="utf-8")
    (wd / "loss.py").write_text("X = 1\n", encoding="utf-8")
    stages = [{"name": "train", "command": [sys.executable, "train.py"]}]
    reached = EvalStagesMixin._stage_reachable_files(stages, wd)
    assert reached is not None and {"train.py", "loss.py"} <= set(reached)
    os.unlink(wd / "loss.py")
    os.mkfifo(wd / "loss.py")
    assert _returns_promptly(EvalStagesMixin._stage_reachable_files, stages, wd) is None


@POSIX_ONLY_OS_CALLS
def test_the_tamper_audit_never_follows_a_planted_link_or_reads_unbounded(tmp_path, monkeypatch):
    """Critic 2026-09-26, driven: `grader.py -> big.bin` had the audit read the whole target on the
    event loop. A link, a FIFO or a directory in place of the file the engine wrote is a tamper."""
    from looplab.core import node_evidence
    from looplab.engine import audit as audit_module

    engine = _repo_engine(tmp_path)
    engine._assets = {"grader.py": "ANSWERS = [1, 2]\n", "key.bin": b"\x00\x01"}
    wd = _workdir(tmp_path)
    big = wd / "big.bin"
    big.write_bytes(b"x" * (1 << 20))
    (wd / "grader.py").symlink_to(big)
    os.mkfifo(wd / "key.bin")
    reads = []
    real = node_evidence.read_bounded_regular_file
    monkeypatch.setattr(audit_module, "read_bounded_regular_file",
                        lambda path, limit, **kw: reads.append((str(path), limit))
                        or real(path, limit, **kw))
    sigs = _returns_promptly(engine._audit_workdir_writes, wd, {"grader.py", "key.bin"})
    assert sorted(s["signal"] for s in sigs) == ["protected_write", "protected_write"], sigs
    assert all("link or a non-regular file" in s["detail"] for s in sigs)
    assert reads == [], "nothing a link points at is read"
    # An honest copy is clean, and the read is bounded by the baseline, not the file.
    os.unlink(wd / "grader.py")
    os.unlink(wd / "key.bin")
    (wd / "grader.py").write_bytes(b"ANSWERS = [1, 2]\r\n")          # a text-mode writer's newline
    (wd / "key.bin").write_bytes(b"\x00\x01")
    assert engine._audit_workdir_writes(wd, {"grader.py", "key.bin"}) == []
    assert all(limit <= 2 * 17 + 2 for _path, limit in reads), reads


def test_the_server_and_the_engine_bound_the_stage_manifest_alike():
    from looplab.runtime.command_eval import STAGE_MANIFEST_MAX_BYTES
    from looplab.serve.routers import runs

    assert runs._STAGE_MANIFEST_MAX_BYTES == STAGE_MANIFEST_MAX_BYTES


# --------------------------------------------------------------------- review of the 5th pass
# Six of the twelve mutants the review drove against the pass above survived its tests; each test
# below names the one it kills. Every mutant was re-applied to a throwaway copy of the tree and the
# named test went red there.
_MANIFEST = {"stages": [{"name": "prep", "command": [sys.executable, "prep.py"]}]}
_EVAL_STAGES_LOG = "looplab.engine.eval_stages"


def _refusals(caplog) -> list:
    return [r.getMessage() for r in caplog.records
            if r.name == _EVAL_STAGES_LOG and r.levelname == "WARNING"]


def test_a_module_with_lone_cr_line_ends_keeps_its_imports_in_the_reuse_closure(tmp_path):
    """Review 2026-09-26, driven through the LIVE reuse decision. `read_text` read universal
    newlines; the bounded byte read that replaced it did not, and both import scans are `re.M`, which
    ends a line at LF only — so a module written with lone-CR line ends (valid Python) read as ONE
    line and credited none of its imports, and the comment strip ran from its first `#` to the end
    of the file. A repair that changed only `loss.py` then REUSED `prep`'s stale output: the
    missed-dependency direction the closure exists to refuse. CRLF was never affected — the scan's
    own `strip()` eats the CR — so the case has to be a LONE one."""
    from looplab.engine.eval_stages import EvalStagesMixin

    wd = _workdir(tmp_path)
    prep = b"import json  # the config (see below)\rimport loss\rprint(json.dumps(loss.X))\r"
    compile(prep, "prep.py", "exec")                  # the premise: Python runs this module
    (wd / "prep.py").write_bytes(prep)
    (wd / "loss.py").write_text("import helper\nX = helper.Y\n", encoding="utf-8")
    (wd / "helper.py").write_text("Y = 1\n", encoding="utf-8")
    (wd / "score.py").write_text("print(1)\n", encoding="utf-8")
    stages = [{"name": "prep", "command": [sys.executable, "prep.py"]},
              {"name": "score", "command": [sys.executable, "score.py"]}]
    assert EvalStagesMixin._stage_reachable_files(stages[:1], wd) == {
        "prep.py", "loss.py", "helper.py"}, "the closure `import` would load, transitively"
    assert EvalStagesMixin()._safe_reuse_start(stages, "score", {"loss.py"}, wd) is None, (
        "a repair to a module `prep` imports must re-run `prep`, never reuse its output")
    assert EvalStagesMixin()._safe_reuse_start(stages, "score", {"score.py"}, wd) == "score"


@POSIX_ONLY_OS_CALLS
def test_a_link_to_a_valid_manifest_is_refused_and_said(tmp_path, caplog):
    """KILLS: the manifest read through the link-FOLLOWING reader. The pass's own link case targets
    `/dev/zero`, which any regular-only reader refuses, so it could not tell the two readers apart;
    a link to a manifest that WOULD resolve can. And the refusal is SAID (review 2026-09-26): a
    refused manifest lands on the same single-command fallback as no manifest at all, which ran the
    operator's command alone with nothing anywhere saying why."""
    import json
    import logging

    engine = _repo_engine(tmp_path)
    es, wd = dict(engine._eval_spec), _workdir(tmp_path)
    score = ([sys.executable, "ttrain_cli.py"], 60.0)
    real = wd / "declared_stages.json"
    real.write_text(json.dumps(_MANIFEST), encoding="utf-8")
    (wd / "looplab_stages.json").write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
    assert [s["name"] for s in engine._resolve_stages(str(wd), es, None, *score)] == [
        "prep", "score"], "the premise: these bytes, as a regular file, ARE a pipeline"
    os.unlink(wd / "looplab_stages.json")
    (wd / "looplab_stages.json").symlink_to(real)
    with caplog.at_level(logging.WARNING, logger=_EVAL_STAGES_LOG):
        assert engine._resolve_stages(str(wd), es, None, *score) is None
        assert engine._resolve_stages(str(wd), es, None, *score) is None
    said = _refusals(caplog)
    assert len(said) == 1, f"one refused manifest, one sentence — not one per resolution: {said}"
    assert "looplab_stages.json" in said[0] and "a link" in said[0]
    assert "no stage the manifest declares will run" in said[0]


def test_a_valid_manifest_padded_past_the_bound_is_refused_not_truncated(tmp_path, caplog):
    """KILLS: removing `len(raw) <= STAGE_MANIFEST_MAX_BYTES`. The pass's test shrank the bound to 10
    bytes, so the truncated read failed to PARSE and the size check was never what refused it. Here
    the first bound-plus-one bytes are a complete, valid manifest (JSON allows trailing whitespace),
    so only the size check stands between the read and a pipeline."""
    import json
    import logging

    from looplab.runtime.command_eval import STAGE_MANIFEST_MAX_BYTES

    engine = _repo_engine(tmp_path)
    es, wd = dict(engine._eval_spec), _workdir(tmp_path)
    score = ([sys.executable, "ttrain_cli.py"], 60.0)
    body = json.dumps(_MANIFEST).encode("utf-8")
    padding = b" " * (STAGE_MANIFEST_MAX_BYTES + 16 - len(body))
    (wd / "looplab_stages.json").write_bytes(body + padding)
    assert json.loads((wd / "looplab_stages.json").read_bytes()[:STAGE_MANIFEST_MAX_BYTES + 1]) == \
        _MANIFEST, "the premise: the bounded read alone would parse"
    with caplog.at_level(logging.WARNING, logger=_EVAL_STAGES_LOG):
        assert engine._resolve_stages(str(wd), es, None, *score) is None
    said = _refusals(caplog)
    assert len(said) == 1 and "larger than" in said[0], said


@POSIX_ONLY_OS_CALLS
def test_a_fifo_manifest_is_said_once_and_an_absent_one_says_nothing(tmp_path, caplog):
    """The resolution runs several times per eval attempt (the dispatcher, the watchdogs' log plan,
    the repair and salvage planners), so the sentence is keyed on the refused file's STATE: said once
    for it, said again when the state changes, and never for the ordinary no-manifest eval."""
    import logging

    engine = _repo_engine(tmp_path)
    es, wd = dict(engine._eval_spec), _workdir(tmp_path)
    score = ([sys.executable, "ttrain_cli.py"], 60.0)
    with caplog.at_level(logging.WARNING, logger=_EVAL_STAGES_LOG):
        assert engine._resolve_stages(str(wd), es, None, *score) is None
        assert _refusals(caplog) == [], "no manifest is not a refused one"
        os.mkfifo(wd / "looplab_stages.json")
        for _ in range(3):
            assert _returns_promptly(engine._resolve_stages, str(wd), es, None, *score) is None
        said = _refusals(caplog)
        assert len(said) == 1 and "a FIFO" in said[0], said
        os.unlink(wd / "looplab_stages.json")
        (wd / "looplab_stages.json").mkdir()                  # a new state: said again
        assert engine._resolve_stages(str(wd), es, None, *score) is None
    said = _refusals(caplog)
    assert len(said) == 2 and "a directory" in said[1], said


@POSIX_ONLY_OS_CALLS
def test_a_module_that_is_a_link_is_read_through_it_not_made_opaque(tmp_path):
    """KILLS: the closure read through the NO-follow reader. `_stage_reachable_files` answers what
    the stage's imports REACH, and `import loss` follows `loss.py -> <a reference checkout>` — so
    must the closure: the linked module's own imports are in it. A no-follow read refused the link
    and made every stage importing a symlinked reference module silently opaque, i.e. never reused."""
    from looplab.engine.eval_stages import EvalStagesMixin

    wd = _workdir(tmp_path)
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "losses.py").write_text("import helper\nX = helper.Y\n", encoding="utf-8")
    (wd / "loss.py").symlink_to(reference / "losses.py")
    (wd / "helper.py").write_text("Y = 1\n", encoding="utf-8")
    (wd / "train.py").write_text("import loss\nprint(loss.X)\n", encoding="utf-8")
    stages = [{"name": "train", "command": [sys.executable, "train.py"]},
              {"name": "score", "command": [sys.executable, "score.py"]}]
    assert EvalStagesMixin._stage_reachable_files(stages[:1], wd) == {
        "train.py", "loss.py", "helper.py"}
    assert EvalStagesMixin()._safe_reuse_start(stages, "score", {"helper.py"}, wd) is None
    assert EvalStagesMixin()._safe_reuse_start(stages, "score", {"score.py"}, wd) == "score"


def test_a_module_past_the_read_bound_makes_the_stage_opaque(tmp_path):
    """KILLS: removing the closure's size bound. A module over `_REACHABLE_SOURCE_MAX_BYTES` is
    OPAQUE — the bounded read holds only its head, and a closure traced from a head is the
    missed-dependency direction for every import below it — so a repair anywhere re-runs the stage.
    The real bound, not a patched one: the mutant must lose on the tree as it ships."""
    from looplab.engine import eval_stages
    from looplab.engine.eval_stages import EvalStagesMixin

    wd = _workdir(tmp_path)
    (wd / "train.py").write_text("import model\nprint(model.W)\n", encoding="utf-8")
    body = b"import layers\nW = 1\n"
    padding = b"#" * (eval_stages._REACHABLE_SOURCE_MAX_BYTES + 1 - len(body))
    (wd / "model.py").write_bytes(body + padding)
    (wd / "layers.py").write_text("K = 3\n", encoding="utf-8")
    stages = [{"name": "train", "command": [sys.executable, "train.py"]},
              {"name": "score", "command": [sys.executable, "score.py"]}]
    why: list = []
    assert EvalStagesMixin._stage_reachable_files(stages[:1], wd, refusal=why) is None
    assert why == ["unreadable_workdir"], "a module that would not read, not an opaque entry point"
    assert EvalStagesMixin()._safe_reuse_start(stages, "score", {"score.py"}, wd) is None
    (wd / "model.py").write_bytes(body)                    # the same module, under the bound
    assert EvalStagesMixin._stage_reachable_files(stages[:1], wd) == {
        "train.py", "model.py", "layers.py"}


def _audit(assets: dict, wd) -> list:
    from looplab.engine.audit import AuditMixin

    return AuditMixin._audit_workdir_writes(SimpleNamespace(_assets=assets), wd, set(assets))


def test_a_crlf_asset_is_judged_with_newlines_normalized_on_both_sides(tmp_path):
    """KILLS: one-sided newline normalization — the defect the pass's own commit message names. Its
    test put the CRLF on the FILE side, where normalizing only the file already worked; the ORIGINAL
    has to carry it. An asset whose text has CRLF is clean byte-exact AND read back with LF (the
    tolerance is symmetric), and still a tamper when a value moves."""
    wd = _workdir(tmp_path)
    key = "id,label\r\n1,cat\r\n2,dog\r\n"
    (wd / "answers.csv").write_bytes(key.encode("utf-8"))
    assert _audit({"answers.csv": key}, wd) == [], "byte-exact, as `write_assets` writes it"
    (wd / "answers.csv").write_bytes(key.replace("\r\n", "\n").encode("utf-8"))
    assert _audit({"answers.csv": key}, wd) == [], "newline-only: both sides normalize alike"
    (wd / "answers.csv").write_bytes(key.replace("cat", "dog").encode("utf-8"))
    assert [s["signal"] for s in _audit({"answers.csv": key}, wd)] == ["protected_write"]


def test_an_appended_or_rewritten_asset_is_flagged_and_the_read_stays_bounded(tmp_path, monkeypatch):
    """KILLS: a read limit of `len(expected)`. A prefix-bounded read of `original + appended` IS the
    original, so it compared clean; the file must END where the baseline does. Value and appended
    tampers, text and bytes assets — and however large the tail, the audit reads at most the
    baseline plus one byte to see it, and the text fallback at most twice the baseline plus two."""
    from looplab.core import node_evidence
    from looplab.engine import audit as audit_module

    wd = _workdir(tmp_path)
    assets = {"grader.py": "ANSWERS = [1, 2]\n", "key.bin": b"\x00\x01\x02"}
    streamed = []
    real_open = node_evidence.open_untrusted_regular

    class _Counting:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            data = self._fh.read(n)
            streamed.append(len(data))
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

    monkeypatch.setattr(audit_module, "open_untrusted_regular", lambda p: _Counting(real_open(p)))
    limits = []
    real_read = node_evidence.read_bounded_regular_file
    monkeypatch.setattr(audit_module, "read_bounded_regular_file",
                        lambda p, limit, **kw: limits.append(limit) or real_read(p, limit, **kw))
    honest = {"grader.py": b"ANSWERS = [1, 2]\n", "key.bin": b"\x00\x01\x02"}
    for name, data in honest.items():
        (wd / name).write_bytes(data)
    assert _audit(assets, wd) == []
    assert limits == [], "the honest file is answered by the streamed bytes, never the text read"
    tail = b"ANSWERS = [2, 2]\n" + b"#" * (4 << 20)
    tampers = {"appended": {n: d + tail for n, d in honest.items()},
               "rewritten": {"grader.py": b"ANSWERS = [2, 1]\n", "key.bin": b"\x00\x01\x03"}}
    for kind, files in tampers.items():
        streamed.clear()
        for name, data in files.items():
            (wd / name).write_bytes(data)
        sigs = _audit(assets, wd)
        assert sorted(s["signal"] for s in sigs) == ["protected_write"] * 2, (kind, sigs)
        assert sum(streamed) <= sum(len(d) + 1 for d in honest.values()), (kind, streamed)
    assert limits and all(limit <= 2 * len(honest["grader.py"]) + 2 for limit in limits), limits
