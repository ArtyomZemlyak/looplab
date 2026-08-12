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
    lost its own remediation line, and would now lose the near-miss entirely."""
    import inspect

    from looplab.runtime import command_eval

    source = inspect.getsource(command_eval._run_stages)
    assert '_artifact_problem)[:700]' in source
    _write(tmp_path, ACTUAL)
    problem = verify_stage_artifacts(
        {"files": [DECLARED]}, str(tmp_path), time.time() - 60, stage="train")
    assert len(problem) <= 700, "the message must fit the cap it is given"
    assert ACTUAL in problem[:700]
