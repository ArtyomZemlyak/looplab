"""A stage that wrote its output ONE DIRECTORY OVER must be told so.

`rubertlite-dr-unified-v5` node 0 trained for 76 minutes on two H200s, exited 0, wrote a complete
SentenceTransformer checkpoint, and computed recall@100 = 0.743. The node was then failed with
`reason: no_metric`, because the manifest declared

    vectorsearch/experiments/unified-baseline_rubert-tiny-lite/final/model.safetensors

while the testbed composes its output directory as `<run_name>_<model>` and wrote

    vectorsearch/experiments/unified-baseline_rubert-tiny-lite_rubert-tiny-lite/final/model.safetensors

The message said "either the stage's code never wrote it (fix the code) or the declaration names the
wrong path". Both halves were on the table and nothing distinguished them — so the one fact that
decides which repair to make, and which was sitting on disk the whole time, went unsaid.
"""
from __future__ import annotations

import os
import time

from looplab.runtime.command_eval import verify_stage_artifacts

DECLARED = "vectorsearch/experiments/unified-baseline_rubert-tiny-lite/final/model.safetensors"
ACTUAL = ("vectorsearch/experiments/unified-baseline_rubert-tiny-lite_rubert-tiny-lite/final/"
          "model.safetensors")


def _write(root, rel, body=b"weights", age=0.0):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


def test_the_real_v5_failure_now_names_where_the_checkpoint_actually_went(tmp_path):
    started = time.time() - 60
    _write(tmp_path, ACTUAL)
    problem = verify_stage_artifacts({"files": [DECLARED]}, str(tmp_path), started, stage="train")
    assert problem is not None, "the contract must still FAIL — the declared path is wrong"
    assert "did NOT produce its declared artifact" in problem
    assert ACTUAL in problem, "the near-miss path is the whole point"
    assert "so the code ran and produced output" in problem


def test_a_stage_that_really_produced_nothing_still_says_only_that(tmp_path):
    """The two halves must stay distinguishable. Claiming a near-miss that does not exist would send
    the repair at a path rename when the code genuinely never wrote anything."""
    problem = verify_stage_artifacts(
        {"files": [DECLARED]}, str(tmp_path), time.time() - 60, stage="train")
    assert problem is not None
    assert "A file of that name WAS written" not in problem


def test_a_leftover_from_an_earlier_attempt_is_not_reported_as_this_stage_s_output(tmp_path):
    """Same rule the main contract's staleness gate exists for: the workdir persists across repair
    attempts on purpose, so an OLD file of the right name is exactly what a wrong answer looks like.
    Pointing the repair at it would rename the declaration onto a foreign experiment's checkpoint."""
    _write(tmp_path, ACTUAL, age=7200)
    problem = verify_stage_artifacts(
        {"files": [DECLARED]}, str(tmp_path), time.time() - 60, stage="train")
    assert "A file of that name WAS written" not in problem


def test_an_empty_near_miss_is_not_evidence_the_stage_worked(tmp_path):
    _write(tmp_path, ACTUAL, body=b"")
    problem = verify_stage_artifacts(
        {"files": [DECLARED]}, str(tmp_path), time.time() - 60, stage="train")
    assert "A file of that name WAS written" not in problem


def test_the_scan_is_bounded_and_skips_the_directories_that_make_it_expensive(tmp_path):
    """It runs on a workdir holding a materialized repo plus every checkpoint, on a FUSE mount. It is
    affordable only because the stage it diagnoses has already cost its full runtime — which is an
    argument for bounding it, not for skipping the bound."""
    from looplab.runtime.command_eval import _NEARBY_MAX_DIRS, _NEARBY_SKIP

    assert _NEARBY_MAX_DIRS > 0
    for noisy in (".git", "node_modules", "__pycache__"):
        assert noisy in _NEARBY_SKIP
    _write(tmp_path, f".git/objects/{os.path.basename(ACTUAL)}")
    _write(tmp_path, f"node_modules/pkg/{os.path.basename(ACTUAL)}")
    problem = verify_stage_artifacts(
        {"files": [DECLARED]}, str(tmp_path), time.time() - 60, stage="train")
    assert "A file of that name WAS written" not in problem, (
        "a hit inside .git or node_modules is never the stage's output")


def test_a_present_artifact_is_still_a_pass_and_costs_no_scan(tmp_path, monkeypatch):
    from looplab.runtime import command_eval

    _write(tmp_path, DECLARED)
    monkeypatch.setattr(command_eval, "_artifact_written_elsewhere",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("scanned on success")))
    assert verify_stage_artifacts(
        {"files": [DECLARED]}, str(tmp_path), time.time() - 60, stage="train") is None


def test_a_scan_failure_degrades_to_the_plain_message_rather_than_crashing_the_eval(
        tmp_path, monkeypatch):
    """This runs inside the eval worker. An exception here would leave the node with no terminal and
    the run re-dying on every resume — the failure class CLAUDE.md names for the unregistered
    metric-reader path slot."""
    from looplab.runtime import command_eval

    def boom(*_a, **_k):
        raise OSError("mount went away")

    monkeypatch.setattr(command_eval.os, "walk", boom)
    problem = verify_stage_artifacts(
        {"files": [DECLARED]}, str(tmp_path), time.time() - 60, stage="train")
    assert problem is not None and "did NOT produce its declared artifact" in problem


def test_the_stage_row_keeps_the_whole_answer(tmp_path):
    """The per-stage `concern` is where an operator reads this. At the old 300-character cap the row
    lost its own remediation line, and would now lose the near-miss entirely.

    DRIVEN, not pinned. This used to assert `'_artifact_problem)[:700]' in inspect.getsource(...)`,
    which is one comment away from vacuous — deleting the slice and leaving `# [:700]` behind would
    satisfy it while the row went back to being truncated. Run a real stage that exits 0, writes its
    artifact one directory over, and read the `concern` the pipeline actually recorded.
    """
    from looplab.runtime.command_eval import run_command_eval

    _write(tmp_path, ACTUAL)
    res = run_command_eval(
        ["python", "-c", "print('RECALL@100: 0.743250')"], str(tmp_path), 60.0,
        {"kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)"},
        stages=[{"name": "train", "command": ["python", "-c", "print('done')"],
                 "timeout": 60.0, "expect": {"files": [DECLARED]}}])
    row = res.stages[-1]
    assert row["status"] == "expect_failed" and res.failed_stage == "train"
    concern = row["concern"]
    # The three things the row has to carry at once — and at the old 300-char cap it carried one.
    assert "did NOT produce its declared artifact" in concern
    assert ACTUAL in concern, "the near-miss is what tells the operator which repair to make"
    assert "Do not delete the declaration" in concern, "the remediation line was the first casualty"
    assert 300 < len(concern) <= 700, "a cap that truncates the answer is not a bound, it is a bug"


def test_two_candidates_produce_a_stable_answer_rather_than_a_filesystem_order_one(tmp_path):
    """`os.walk` yields directories in FILESYSTEM order (`os.scandir`), so "the first match" was not
    a rule: with two fresh same-basename files the message — and, through
    `metric_salvage._relocated`, the file a SALVAGED metric is read out of — could differ between two
    reads of the identical workdir. The diagnostic half now shows the first in sorted order, which is
    the same on every read; the salvage half refuses to choose at all."""
    from looplab.runtime.command_eval import (_artifact_written_elsewhere,
                                              _artifacts_written_elsewhere)

    root = "vectorsearch/experiments"
    for sub in ("zzz_last", "aaa_first"):
        _write(tmp_path, f"{root}/{sub}/final/model.safetensors")
    found = _artifacts_written_elsewhere(str(tmp_path), DECLARED, time.time() - 60)
    assert found == sorted(found) and len(found) == 2
    assert _artifact_written_elsewhere(str(tmp_path), DECLARED, time.time() - 60) == found[0]
    assert "aaa_first" in found[0], "sorted, not whichever the filesystem yielded first"
    # The operator still gets told SOMETHING — an ambiguous answer is more useful than none here,
    # because a human can weigh two paths. (The salvage rung cannot: see
    # tests/test_metric_salvage.py::test_two_fresh_same_basename_files_refuse_the_relocation…)
    problem = verify_stage_artifacts(
        {"files": [DECLARED]}, str(tmp_path), time.time() - 60, stage="train")
    assert found[0] in problem
