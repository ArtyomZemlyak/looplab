"""B1 host-side scoring: the host scores candidate predictions against private held-out labels."""
from __future__ import annotations

import json

import pytest

from looplab.runtime.command_eval import host_score, read_metric


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


def test_host_score_labels_path_must_be_outside_workspace(tmp_path):
    # read_metric(kind=host_score) must refuse a labels file inside the candidate workspace (it would
    # be mounted/writable by an untrusted candidate). Place predictions + labels both under workdir.
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "predictions.json").write_text(json.dumps([1, 0, 1]), encoding="utf-8")
    inside = wd / "labels.json"
    inside.write_text(json.dumps([1, 0, 1]), encoding="utf-8")
    spec = {"kind": "host_score", "scorer": "accuracy",
            "predictions": "predictions.json", "labels": str(inside)}
    with pytest.raises(ValueError, match="inside the candidate workspace"):
        read_metric("", str(wd), spec)
    # An out-of-workspace labels file is accepted and scored.
    outside = tmp_path / "labels.json"
    outside.write_text(json.dumps([1, 0, 1]), encoding="utf-8")
    spec["labels"] = str(outside)
    assert read_metric("", str(wd), spec) == 1.0
