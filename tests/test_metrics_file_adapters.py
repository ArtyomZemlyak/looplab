"""Curves from `trainer_state.json` and `*.metrics.jsonl` reach the Metrics view.

UI review 2026-10-14 §7: the reader knew only TensorBoard, so a Hugging Face `Trainer` run (which
logs loss / learning rate / grad norm into `trainer_state.json`) or a script appending JSON lines
showed no curve. No tensorboard install is needed for these two adapters.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from looplab.serve.metrics_adapters import FileMetricsAdapter, read_node_metrics


def _state(path, steps):
    path.parent.mkdir(parents=True, exist_ok=True)
    history = [{"step": s, "epoch": s / 10, "loss": 1.0 / (s + 1), "learning_rate": 1e-4,
                "grad_norm": 0.5} for s in steps]
    history.append({"step": steps[-1], "eval_loss": 0.25, "eval_runtime": 3.0})
    path.write_text(json.dumps({"global_step": steps[-1], "log_history": history}))


def test_trainer_state_reads_the_newest_history_once(tmp_path):
    _state(tmp_path / "out" / "checkpoint-2" / "trainer_state.json", [0, 1, 2])
    _state(tmp_path / "out" / "trainer_state.json", [0, 1, 2, 3, 4])
    series = FileMetricsAdapter().read(str(tmp_path))
    assert [p["step"] for p in series["trainer/loss"]] == [0, 1, 2, 3, 4], (
        "checkpoint copies hold the same history; merging them would plot each point twice")
    assert "trainer/eval_loss" in series and "trainer/epoch" not in series
    assert "trainer/step" not in series


def test_jsonl_rows_become_per_file_tags_and_skip_torn_lines(tmp_path):
    path = tmp_path / "logs" / "stage1.metrics.jsonl"
    path.parent.mkdir()
    path.write_text("\n".join([
        json.dumps({"step": 0, "loss": 2.0, "recall@20": 0.1, "time": 100.0}),
        json.dumps({"global_step": 1, "loss": 1.5, "note": "text is not a curve", "time": 101.0}),
        json.dumps({"step": 2, "loss": float("nan")}).replace("NaN", "NaN"),
        '{"step": 3, "loss": 1.',                       # torn tail while the trainer appends
    ]))
    series = FileMetricsAdapter().read(str(tmp_path))
    assert [p["value"] for p in series["stage1/loss"]] == [2.0, 1.5]
    assert series["stage1/loss"][0]["wall_time"] == 100.0
    assert "stage1/note" not in series and "stage1/time" not in series


def test_the_attempt_fence_drops_a_file_older_than_the_attempt(tmp_path):
    _state(tmp_path / "trainer_state.json", [0, 1])
    stale = time.time() - 3600
    os.utime(tmp_path / "trainer_state.json", (stale, stale))
    assert read_node_metrics(str(tmp_path), since_wall_time=time.time() - 60) == {}
    assert "trainer/loss" in read_node_metrics(str(tmp_path), since_wall_time=stale - 1)


def test_a_linked_or_oversized_file_is_not_read(tmp_path):
    outside = tmp_path / "outside"
    _state(outside / "trainer_state.json", [0, 1])
    node = tmp_path / "node"
    node.mkdir()
    try:
        os.symlink(outside / "trainer_state.json", node / "trainer_state.json")
        os.symlink(outside, node / "linked_dir")
    except OSError:
        pytest.skip("symlinks unavailable")
    assert FileMetricsAdapter().read(str(node)) == {}
    big = tmp_path / "big"
    big.mkdir()
    (big / "x.metrics.jsonl").write_bytes(b" " * (32 * 1024 * 1024 + 1))
    assert FileMetricsAdapter().read(str(big)) == {}
