"""B1 host-side scoring: the host scores candidate predictions against private held-out labels."""
from __future__ import annotations

import json

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
