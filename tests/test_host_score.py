"""B1 host-side scoring: the host scores candidate predictions against private held-out labels."""
from __future__ import annotations

import json
import logging

import pytest

from looplab.runtime.command_eval import host_score, host_score_labels_error, read_metric


def test_host_score_rmse_mae_mse():
    assert host_score("mae", [1.0, 2.0, 3.0], [1.0, 2.0, 4.0]) == 1 / 3
    assert abs(host_score("mse", [1.0, 2.0], [1.0, 4.0]) - 2.0) < 1e-9
    assert abs(host_score("rmse", [1.0, 2.0], [1.0, 4.0]) - 2.0 ** 0.5) < 1e-9


def test_host_score_accuracy_and_error_rate():
    assert host_score("accuracy", [1, 0, 1, 1], [1, 0, 0, 1]) == 0.75
    assert host_score("error_rate", [1, 0, 1, 1], [1, 0, 0, 1]) == 0.25


def test_host_score_shape_mismatch_returns_none():
    assert host_score("rmse", [1.0], [1.0, 2.0]) is None
    assert host_score("rmse", [], []) is None
    assert host_score("unknown", [1], [1]) is None


def test_host_score_extracts_keyed_payload():
    assert host_score("mae", {"predictions": [2.0]}, {"y": [1.0]}) == 1.0


def test_read_metric_host_score_end_to_end(tmp_path):
    # Candidate writes predictions into its workdir; labels live OUTSIDE it (host-held, private).
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "predictions.json").write_text(json.dumps([1.0, 2.0, 3.0]), encoding="utf-8")
    labels = tmp_path / "private_labels.json"   # not under the workdir -> candidate can't see it
    labels.write_text(json.dumps([1.0, 2.0, 4.0]), encoding="utf-8")
    spec = {"kind": "host_score", "predictions": "predictions.json",
            "labels": str(labels), "scorer": "mae"}
    assert read_metric("", str(wd), spec) == 1 / 3
    # missing predictions -> None (no metric, never a crash)
    assert read_metric("", str(tmp_path / "empty"), spec) is None


# --- host_score: label equality + no candidate array-shopping ------------------------------------

def test_host_score_accuracy_matches_numeric_encodings():
    # Class labels encoded as JSON strings / floats must still match the integer answer key.
    assert host_score("accuracy", ["1", "0", "1"], [1, 0, 1]) == 1.0
    assert host_score("accuracy", [1.0, 0.0, 1.0], [1, 0, 1]) == 1.0
    # A genuine non-label (a probability) must NOT count as correct.
    assert host_score("accuracy", [0.999, 0.0, 1.0], [1, 0, 1]) == pytest.approx(2 / 3)


def test_host_score_predictions_cannot_key_shop():
    # The candidate ships a payload with several arrays; only a bare list or the canonical
    # "predictions" key is scored — a candidate can't smuggle a favorable array under "values".
    preds = {"values": [1, 1, 1], "predictions": [1, 0, 1]}
    assert host_score("accuracy", preds, [1, 0, 1]) == 1.0       # uses "predictions", not "values"
    # No recognized predictions key -> no metric (can't fall through to a candidate-chosen array).
    assert host_score("accuracy", {"values": [1, 0, 1]}, [1, 0, 1]) is None


def test_host_score_labels_path_must_be_outside_workspace(tmp_path, caplog):
    """The invariant holds; WHERE it is enforced moved, and this pins both halves.

    At SCORE time the reader used to `raise ValueError`, and that raise escaped `read_metric`,
    `run_command_eval` and `_run_eval` — measured — into an eval worker whose only handler is
    `except GpuPinUnenforceable` while the dispatchers wrap `_evaluate` in try/FINALLY. So an
    operator whose held-out labels happened to sit under the run root got NO TERMINAL EVENT for the
    node and a run that re-died on every resume: the loudest possible complaint, aimed at the one
    audience that cannot act on it. It scores nothing and logs at ERROR now.

    The refusal that an operator CAN act on is `host_score_labels_error`, asked before the run
    starts (`adapters/repo_task.py::EvalSpec` for the spec-local halves,
    `eval_workspace_conflicts` from the launch path for this one). Both are asserted here, because
    "it no longer raises" on its own would be a licence to lose the property.
    """
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "predictions.json").write_text(json.dumps([1, 0, 1]), encoding="utf-8")
    inside = wd / "labels.json"
    inside.write_text(json.dumps([1, 0, 1]), encoding="utf-8")
    spec = {"kind": "host_score", "scorer": "accuracy",
            "predictions": "predictions.json", "labels": str(inside)}
    with caplog.at_level(logging.ERROR, logger="looplab.runtime.command_eval"):
        assert read_metric("", str(wd), spec) is None, "the eval must score nothing, not die"
    assert "inside the candidate workspace" in caplog.text
    # …and the submit-time refusal names the file while the operator can still move it.
    assert "INSIDE the candidate workspace" in host_score_labels_error(spec, workspace_root=wd)
    # An out-of-workspace labels file is accepted, scored, and refused by nothing.
    outside = tmp_path / "labels.json"
    outside.write_text(json.dumps([1, 0, 1]), encoding="utf-8")
    spec["labels"] = str(outside)
    assert read_metric("", str(wd), spec) == 1.0
    assert host_score_labels_error(spec, workspace_root=wd) is None


def test_the_submit_time_refusal_covers_the_two_shapes_a_score_time_reader_cannot_judge(tmp_path):
    """A reader inside an eval worker can only ever return None, so these two never had a voice.

    ABSENT labels made every node fail `no_metric` with nothing naming the cause — the exact
    `_PATHLESS_COST` shape one field over. A RELATIVE path is worse than ambiguous: it resolves
    against the ENGINE process's cwd, so which file grades the run depends on where it was launched
    from, and launching inside the run directory puts the answer key in the candidate's own reach.
    """
    assert "needs `labels`" in host_score_labels_error({"kind": "host_score"})
    assert "needs `labels`" in host_score_labels_error({"kind": "host_score", "labels": "  "})
    assert "RELATIVE" in host_score_labels_error(
        {"kind": "host_score", "labels": "data/holdout.json"})
    # A workspace root is not needed for either — that is the point of splitting the rule.
    assert host_score_labels_error({"kind": "host_score", "labels": str(tmp_path / "l.json")}) is None
    # It is ONE reader's rule, asked of every reader slot; nothing else is its business.
    assert host_score_labels_error({"kind": "file_json", "path": "m.json"}) is None
    assert host_score_labels_error(None) is None
    # A non-string slot belongs to `metric_spec_path_error`, which owns that message.
    assert host_score_labels_error({"kind": "host_score", "labels": 3}) is None


def test_an_eval_spec_refuses_an_unusable_labels_path_at_submit_time(tmp_path):
    """The single seam every submit surface goes through (CLI, /api/start, Genesis, the run tools).
    A `host_score` in ANY reader slot holds the operator's answer key, so it is asked of all four."""
    from pydantic import ValidationError

    from looplab.adapters.repo_task import EvalSpec, eval_workspace_conflicts

    good = {"kind": "host_score", "predictions": "p.json", "labels": str(tmp_path / "l.json"),
            "scorer": "rmse"}
    spec = EvalSpec(command=["python", "eval.py"], metric=dict(good))
    assert spec.metric["labels"] == str(tmp_path / "l.json")
    with pytest.raises(ValidationError, match="RELATIVE"):
        EvalSpec(command=["python", "eval.py"], metric={**good, "labels": "labels/holdout.json"})
    with pytest.raises(ValidationError, match="needs `labels`"):
        EvalSpec(command=["python", "eval.py"],
                 constraints=[{**good, "labels": "", "name": "held_out", "max": 1.0}])
    # The workspace half needs a run directory, which a spec being authored does not have — so it is
    # asked by the launch surface instead, and it does refuse.
    run_dir = tmp_path / "runs" / "demo"
    inside = EvalSpec(command=["python", "eval.py"],
                      metric={**good, "labels": str(run_dir / "nodes" / "labels.json")})
    assert eval_workspace_conflicts(inside, run_dir) and not eval_workspace_conflicts(spec, run_dir)
    assert "eval.metric" in eval_workspace_conflicts(inside, run_dir)[0]
