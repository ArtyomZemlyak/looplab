"""D2 capability self-benchmark harness."""
from __future__ import annotations

import json
from pathlib import Path

from looplab.bench import run_benchmark
from looplab.core.config import Settings

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


# ------------------------------------------------ review 2026-09-22, SCJ-05: a bench task is a RUN
#
# `run_benchmark` called `_engine(...).run` directly, so a bench task skipped `looplab run`'s whole
# lifecycle (no snapshots, no engine.lock, no guarded terminal) and every task shared ONE `Settings`
# — whose cached `CostAccountant` made `llm_budget_usd` a ceiling on the SUITE. And the command
# exited 0 when every task errored. Each of those is driven below.

def test_each_task_meters_against_its_own_spend_ceiling(tmp_path, monkeypatch):
    """A stub PAID call of $0.60 per task against a $1.00 ceiling. Sharing the caller's `Settings`
    shared its accountant (`core/llm.py::run_cost_accountant` caches it ON the object), so the second
    task opened at $0.60 already spent and its own call crossed the ceiling: `LLM spend ceiling
    reached`, recorded as that task's error, for a suite in which no task spent more than $0.60."""
    import looplab.cli as cli
    from looplab.core.llm import run_cost_accountant

    real_engine = cli._engine
    accountants = []

    def _engine_with_a_paid_call(run_dir, task, settings, *args, **kwargs):
        engine = real_engine(run_dir, task, settings, *args, **kwargs)
        accountant = run_cost_accountant(settings)
        accountant.add(0.60)                 # the stub paid call, metered like a real client's
        accountants.append(accountant)
        return engine

    monkeypatch.setattr(cli, "_engine", _engine_with_a_paid_call)
    base = Settings(backend="toy", max_nodes=2, llm_budget_usd=1.0)
    toy = ROOT / "examples" / "toy_task.json"
    results = run_benchmark([toy, toy], base, tmp_path / "suite")
    assert [r.get("error") for r in results] == [None, None], results
    assert all(r["finished"] for r in results)
    assert len(accountants) == 2 and accountants[0] is not accountants[1]
    assert [a.spent for a in accountants] == [0.60, 0.60]
    # …and the caller's own object is left alone: no run is attached to it.
    assert run_cost_accountant(base) not in accountants


def test_a_bench_task_dir_is_a_run_that_resume_can_reenter(tmp_path):
    """No `config.snapshot.json` meant `looplab resume` REFUSED a bench task dir (it requires the
    snapshot), so a suite interrupted mid-task could only be restarted from scratch."""
    from typer.testing import CliRunner

    from looplab.cli import app

    results = run_benchmark([ROOT / "examples" / "toy_task.json"],
                            Settings(backend="toy", max_nodes=2), tmp_path / "suite")
    run_dir = tmp_path / "suite" / "toy_task"
    assert results[0]["finished"] is True
    for name in ("config.snapshot.json", "task.snapshot.json", "engine.lock"):
        assert (run_dir / name).exists(), name
    snapshot = json.loads((run_dir / "config.snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["max_nodes"] == 2 and snapshot["backend"] == "toy"
    resumed = CliRunner().invoke(app, ["resume", str(run_dir)])
    assert resumed.exit_code == 0, resumed.output
    assert "no config.snapshot.json" not in resumed.output


def test_a_task_dir_another_engine_holds_is_an_error_not_a_second_writer(tmp_path):
    """No `engine.lock` meant a second suite (or a `looplab run`) could drive the same event log
    concurrently. The bench now takes the run's lock like `run` does, and a held dir is that task's
    error — never a second writer."""
    from looplab.cli import _engine_singleton

    run_dir = tmp_path / "suite" / "toy_task"
    with _engine_singleton(run_dir) as ok:
        assert ok
        results = run_benchmark([ROOT / "examples" / "toy_task.json"],
                                Settings(backend="toy", max_nodes=2), tmp_path / "suite")
    assert results[0]["finished"] is False
    assert "already running" in results[0]["error"]
    assert not (run_dir / "events.jsonl").exists() or not (run_dir / "events.jsonl").read_text()


def test_the_bench_command_exits_nonzero_when_a_task_errors(tmp_path):
    """It exited 0 even when EVERY task errored, so a CI step read a broken suite as a pass."""
    from typer.testing import CliRunner

    from looplab.cli import app

    bad = tmp_path / "bad_task.json"
    bad.write_text(json.dumps({"kind": "quadratic", "goal": "x", "direction": "sideways"}),
                   encoding="utf-8")
    toy = str(ROOT / "examples" / "toy_task.json")
    runner = CliRunner()
    failed = runner.invoke(app, ["bench", toy, str(bad), "--out", str(tmp_path / "b1"),
                                 "--max-nodes", "2"])
    assert failed.exit_code == 1, failed.output
    assert "bad_task: ERROR" in failed.output
    passed = runner.invoke(app, ["bench", toy, "--out", str(tmp_path / "b2"), "--max-nodes", "2"])
    assert passed.exit_code == 0, passed.output
