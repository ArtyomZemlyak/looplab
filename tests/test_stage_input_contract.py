"""`needs` — the INPUT half of the stage contract.

`expect` describes what a stage WRITES. Nothing in the manifest ever described what a stage READS,
and both of the most expensive defects on this box are that shape:

  * `rubertlite-dr-unified-v5` node 0 — trained 76 minutes on two H200s and wrote its checkpoint
    exactly where its manifest declared. The scorer read a different directory. Every contract
    passed; the node died `no_metric` having computed one.
  * `rubertlite-dr-unified-v6` node 4 — trained a model that reached recall@100 0.726 (`train.log`)
    and then scored a HUMAN's July checkpoint, because an absolute path in an editable config named
    it. `score.log` says 0.224975, and 0.224975 is what the run recorded as the merge's result. The
    artifact contract PASSED — the stage did write everything it promised.

The check is a stat() before the command runs, so the failure costs one second instead of an hour,
and — where an earlier stage declared the missing path as its own output — the refusal names BOTH
declarations instead of surfacing as a traceback inside somebody's loader.

What is deliberately NOT here: a freshness rule. `expect` refuses an artifact older than the stage
because a stale output means the stage did not produce it. An INPUT is legitimately older than the
stage by any amount — the seeded repo, a mounted dataset, a base checkpoint, or a `train` output the
engine deliberately reused across repair attempts. Applying the output rule here would refuse the
reuse the persistent workdir exists for.
"""
from __future__ import annotations

import os
import sys

import pytest

from looplab.core.models import FAILURE_REASONS
from looplab.engine.crash_repair import CrashRepairMixin
from looplab.engine.triage import _failure_reason
from looplab.runtime.command_eval import (MAX_STAGE_NEEDS_FILES, run_command_eval,
                                          stage_output_producers, validate_stages,
                                          verify_stage_inputs)

_M = {"kind": "stdout_json", "key": "metric"}


def _py(*lines):
    return [sys.executable, "-c", "\n".join(lines)]


# --------------------------------------------------------------------------------------------
# Authoring: the same shape rules `expect.files` goes through
# --------------------------------------------------------------------------------------------

def test_a_declared_input_survives_validation_in_canonical_form():
    clean, err = validate_stages([{"name": "train", "command": ["python", "t.py"],
                                   "needs": ["./data/train.parquet", "data/train.parquet"]}])
    assert err is None
    assert clean[0]["needs"] == ["data/train.parquet"], "'./' stripped and duplicates collapsed"


@pytest.mark.parametrize("bad, why", [
    ("/etc/passwd", "absolute"),
    ("../../vectorizer-unified/ckpt.pt", "traversal"),
    ("", "empty"),
])
def test_an_input_that_names_something_outside_this_node_is_refused_at_authoring(bad, why):
    """The same containment rule as `expect.files`, and for a sharper reason: an ABSOLUTE input path
    is the literal shape of the v6 node 4 corruption. A manifest may not declare it at all."""
    clean, err = validate_stages([{"name": "score", "command": ["python", "s.py"], "needs": [bad]}])
    assert clean is None and err is not None, why
    assert "needs" in err and "score" in err


def test_the_input_list_is_bounded_and_type_checked():
    ok, err = validate_stages([{"name": "t", "command": ["x"],
                                "needs": [f"f{i}" for i in range(MAX_STAGE_NEEDS_FILES + 1)]}])
    assert ok is None and "at most" in err

    ok, err = validate_stages([{"name": "t", "command": ["x"], "needs": "ckpt.pt"}])
    assert ok is None and "list of workdir-relative path strings" in err, (
        "a bare string must be refused at authoring, not iterated per character at run time")


def test_omitting_needs_stays_legal():
    """Not every stage reads a produced artifact — a data-prep stage may read only the seeded repo.
    An empty or absent declaration must not become a required field, or the manifest fills up with
    `needs: []` rows that assert nothing."""
    clean, err = validate_stages([{"name": "prep", "command": ["python", "p.py"]}])
    assert err is None and "needs" not in clean[0]
    clean, err = validate_stages([{"name": "prep", "command": ["python", "p.py"], "needs": []}])
    assert err is None and "needs" not in clean[0]


# --------------------------------------------------------------------------------------------
# The check itself
# --------------------------------------------------------------------------------------------

def test_a_present_input_passes_and_an_absent_one_names_the_fix(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "train.parquet").write_text("rows")
    assert verify_stage_inputs(["data/train.parquet"], str(tmp_path), stage="train") is None

    problem = verify_stage_inputs(["ckpt/model.pt"], str(tmp_path), stage="score")
    assert problem and "did not start" in problem and "ckpt/model.pt" in problem
    assert "do not delete the declaration" in problem.lower()


def test_the_refusal_names_the_EARLIER_STAGE_that_promised_the_file(tmp_path):
    """This is the sentence the whole feature is for. Two declarations disagree about where a file
    lives; without this, the disagreement surfaces as a loader traceback in the SECOND stage and the
    repair goes looking at the second stage's code."""
    stages = [{"name": "train", "command": ["x"], "expect": {"files": ["ckpt/model.pt"]}},
              {"name": "score", "command": ["y"], "needs": ["ckpt/model.pt"]}]
    producers = stage_output_producers(stages, upto=1)
    assert producers == {"ckpt/model.pt": "train"}

    problem = verify_stage_inputs(["ckpt/model.pt"], str(tmp_path), stage="score",
                                  producers=producers)
    assert "'train'" in problem and "expect.files" in problem
    assert "disagree about where the file" in problem


def test_a_file_of_that_name_elsewhere_is_reported(tmp_path):
    """The near-miss half, mirroring `EXPECT_MISSING_NEARBY`: 'your training worked and one of the
    two paths is wrong' is a different repair from 'nothing was produced'."""
    (tmp_path / "experiments" / "final").mkdir(parents=True)
    (tmp_path / "experiments" / "final" / "model.pt").write_text("weights")

    problem = verify_stage_inputs(["ckpt/model.pt"], str(tmp_path), stage="score")
    assert "DOES exist at" in problem and "experiments" in problem


def test_an_empty_input_is_refused_as_its_own_condition(tmp_path):
    """A 0-byte file passes existence and carries nothing — the shape a crashed-then-swallowed writer
    leaves. Running on it either crashes in a loader or silently trains on nothing."""
    (tmp_path / "hard_negs.pkl").write_text("")
    problem = verify_stage_inputs(["hard_negs.pkl"], str(tmp_path), stage="train")
    assert problem and "EMPTY" in problem


def test_a_non_empty_directory_counts_as_present(tmp_path):
    """A HF `save_pretrained` output is a directory. Its emptiness is the meaningful condition."""
    ckpt = tmp_path / "final"
    ckpt.mkdir()
    assert verify_stage_inputs(["final"], str(tmp_path), stage="score") is not None
    (ckpt / "model.safetensors").write_text("w")
    assert verify_stage_inputs(["final"], str(tmp_path), stage="score") is None


def test_an_input_under_an_engine_MOUNT_is_readable_not_an_escape(tmp_path):
    """`engine/workspace.py::link_input` materializes a `mount:true` data/reference source as a
    SYMLINK in the workdir pointing at the operator's path outside it. Resolving that link before
    testing containment refused every declared input under a mount — so a repo task with
    `data: {train: {path: /mnt/ds, mount: true}}` had every node terminate `needs_failed` before any
    compute, while the sibling message forbids the only fix the model can apply ("do not delete the
    declaration to make this pass"). The read fence allow-lists mount SOURCES for the same reason;
    the two halves of one change must not disagree about mounts."""
    wd, ds = tmp_path / "wd", tmp_path / "ds"
    wd.mkdir()
    ds.mkdir()
    (ds / "train.parquet").write_text("rows")
    os.symlink(ds, wd / "train", target_is_directory=True)

    assert verify_stage_inputs(["train/train.parquet"], str(wd), stage="score") is None
    # The contract still WORKS through the mount — a path that is really absent still reports.
    missing = verify_stage_inputs(["train/absent.parquet"], str(wd), stage="score")
    assert missing and "does not exist in the eval workdir" in missing
    # …and a real escape is still refused, which is what containment was for.
    assert verify_stage_inputs(["../ds/train.parquet"], str(wd), stage="score") is not None


def test_the_producer_link_survives_two_legal_spellings_of_one_path(tmp_path):
    """`ckpt/model.pt` and `ckpt/./model.pt` both validate, so keying the producer map on the raw
    string lost the one sentence the feature exists to produce — the repair loop got the generic
    message instead of "stage 'train' DECLARES this path as one of its own outputs"."""
    stages = [{"name": "train", "command": ["x"], "expect": {"files": ["ckpt/model.pt"]}},
              {"name": "score", "command": ["y"], "needs": ["ckpt/./model.pt"]}]
    producers = stage_output_producers(stages, 1)
    assert producers == {"ckpt/model.pt": "train"}
    message = verify_stage_inputs(["ckpt/./model.pt"], str(tmp_path), stage="score",
                                  producers=producers)
    assert message and "DECLARES this path as one of its own outputs" in message


def test_an_input_older_than_the_stage_is_NOT_stale(tmp_path):
    """The rule `expect` has and `needs` deliberately does not. If a freshness filter ever appears
    here, a repair attempt that reuses a completed `train` stage would have its checkpoint refused
    as an input by the very next stage.

    There is no `since` parameter to pass: one was accepted and immediately `del`'d, which is worse
    than absent — the next person adding a freshness rule passes it, sees no behaviour change and no
    error, and debugs the caller. The signature assertion below is what keeps it absent."""
    import inspect
    import os
    import time
    (tmp_path / "base.pt").write_text("weights")
    old = time.time() - 86_400
    os.utime(tmp_path / "base.pt", (old, old))
    assert verify_stage_inputs(["base.pt"], str(tmp_path), stage="train") is None
    assert "since" not in inspect.signature(verify_stage_inputs).parameters


# --------------------------------------------------------------------------------------------
# End to end: a real pipeline, really refused, before the expensive stage runs
# --------------------------------------------------------------------------------------------

def test_the_pipeline_stops_BEFORE_the_stage_runs_and_says_which_stage(tmp_path):
    """The v5/v6 shape reproduced: `train` writes its checkpoint one directory over from where the
    next stage declares it. The second stage must never start, and its side effect must not exist."""
    stages = [
        {"name": "train", "command": _py("import pathlib",
                                         "pathlib.Path('experiments').mkdir(exist_ok=True)",
                                         "pathlib.Path('experiments/model.pt').write_text('w')",
                                         "print('trained')"),
         "expect": {"files": ["experiments/model.pt"]}},
        {"name": "score", "needs": ["ckpt/model.pt"],
         "command": _py("import pathlib", "pathlib.Path('SCORED').write_text('x')",
                        "print('{\"metric\": 0.9}')")},
    ]
    res = run_command_eval(["true"], str(tmp_path), 60, _M, stages=stages)

    assert res.failed_stage == "score"
    assert res.metric is None, "a pipeline that never scored must not report a metric"
    assert not (tmp_path / "SCORED").exists(), "the refused stage's command actually ran"

    rows = {r["name"]: r for r in res.stages}
    assert rows["train"]["status"] == "ok", "the stage that DID its job keeps its success"
    assert rows["score"]["status"] == "needs_failed"
    assert rows["score"]["seconds"] == 0.0, "a refused stage costs no runtime"
    # No producer is named here, and that is CORRECT: `train` declared `experiments/model.pt`, so
    # nothing in the manifest ever claimed to write `ckpt/model.pt`. What the operator needs is the
    # near-miss — the file exists, one of the two paths is wrong — and it is in the durable row.
    assert "experiments/model.pt" in rows["score"]["concern"]
    assert "DOES exist at" in rows["score"]["concern"]

    assert _failure_reason(res) == "needs_failed"


def test_a_pipeline_whose_inputs_line_up_runs_to_its_metric(tmp_path):
    """The control. The contract must not cost a correct pipeline anything — including one whose
    input is a DIRECTORY and one whose input was seeded before the eval started."""
    (tmp_path / "seed.txt").write_text("given")
    stages = [
        {"name": "train", "needs": ["seed.txt"],
         "command": _py("import pathlib",
                        "pathlib.Path('ckpt').mkdir(exist_ok=True)",
                        "pathlib.Path('ckpt/model.pt').write_text('w')", "print('trained')"),
         "expect": {"files": ["ckpt/model.pt"]}},
        {"name": "score", "needs": ["ckpt", "ckpt/model.pt"],
         "command": _py("print('{\"metric\": 0.42}')")},
    ]
    res = run_command_eval(["true"], str(tmp_path), 60, _M, stages=stages)
    assert res.failed_stage is None and res.metric == 0.42
    assert [r["status"] for r in res.stages] == ["ok", "ok"]


# --------------------------------------------------------------------------------------------
# What the engine then does with it
# --------------------------------------------------------------------------------------------

def test_needs_failed_is_a_selectable_repair_reason():
    assert "needs_failed" in FAILURE_REASONS


class _Repairer(CrashRepairMixin):
    def __init__(self):
        self._deep_repair = False
        self._repo_spec = None
        self._eval_parallel = 1
        self._gpu_ids = None


def test_the_directive_sends_the_developer_to_the_DECLARATIONS_not_to_the_code():
    """The stage did not execute, so its code is not evidence of anything. A generic 'diagnose the
    crash' directive would send the Developer to read a file that never ran — and the one repair
    that must be refused outright is deleting the `needs` entry, which makes the check pass and
    moves the identical failure into the loader."""
    text = _Repairer()._repair_error_context("needs_failed", "[failed stage: score]\n<the reason>")

    assert "[failure kind: needs_failed]" in text
    assert "<the reason>" in text
    assert "did not execute" in text
    assert "Removing the `needs` entry is not a fix" in text
    for wrong in ("out-of-memory", "reduce compute", "smaller batch"):
        assert wrong not in text
