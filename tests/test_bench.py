"""D2 capability self-benchmark harness."""
from __future__ import annotations

import json
from pathlib import Path

from looplab.bench import run_benchmark
from looplab.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_run_benchmark_toy(tmp_path):
    s = Settings(backend="toy", max_nodes=6)
    results = run_benchmark([ROOT / "examples" / "toy_task.json"], s, tmp_path / "b")
    assert len(results) == 1
    r = results[0]
    assert r["finished"] and r["best_metric"] is not None
    assert r["nodes"] == 6 and "eval_seconds" in r and "reward_hack_flags" in r
    # report file written + well-formed
    report = json.loads((tmp_path / "b" / "benchmark.json").read_text(encoding="utf-8"))
    assert report["n_tasks"] == 1 and report["solved"] == 1


def test_run_benchmark_multi_and_bad_task(tmp_path):
    s = Settings(backend="toy", max_nodes=4)
    results = run_benchmark(
        [ROOT / "examples" / "toy_task.json", tmp_path / "does_not_exist.json"], s, tmp_path / "b2")
    assert len(results) == 2
    assert results[0]["finished"] is True
    assert results[1]["finished"] is False and "error" in results[1]   # bad task isolated
