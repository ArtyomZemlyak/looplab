"""What the CLI says and writes around a FINISHED run (doc 75 UX-05, UX-07, UX-08, UX-27, UX-33, UX-35).

Driven through the real CLI on the offline demo (`examples/demo.yaml`, 6 experiments, a declared
comparison contract), because every property here is about what an operator SEES and what lands in
the run's log:

* a finished run with no node left under the settings a command carries is not reopened —
  `resume`, `stop` and a repeat `run` append 0 rows (they appended 17 / 1 / 17, re-running the whole
  finalization, paid on a model run) and name the remedy; a raised budget (the flag, or the
  Assistant's `budget_extend`) still continues;
* the summary names the budget, the metric and its direction, and the next step; `inspect` of the
  finished demo is at most six lines and drops the stuck-run diagnostics an interrupted run keeps;
* `timings` picks one unit for a seconds-long run, `tokens` says why an offline run has no spans;
* a run refused before its first event leaves no directory behind; `inspect` with no argument lists
  the runs.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.events.eventstore import EventStore

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo.yaml"


def _invoke(*args):
    return CliRunner().invoke(app, [str(a) for a in args])


def _rows(run_dir: Path) -> int:
    return len((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines())


@pytest.fixture
def demo(tmp_path):
    run_dir = tmp_path / "demo"
    result = _invoke("run", DEMO, "--out", run_dir)
    assert result.exit_code == 0, result.output
    return run_dir, result.output


def test_the_summary_names_the_budget_the_metric_its_direction_and_the_next_step(demo):
    run_dir, output = demo
    assert "stop: finished — node budget spent (6/6 experiments)." in output
    assert "BEST experiment #" in output and ": quadratic_loss = " in output
    assert "(lower is better)" in output
    assert "next: looplab ui" in output and "tree.html" in output
    assert "names no reason" not in output


@pytest.mark.parametrize("command", [("resume",), ("stop",), ("run",)])
def test_a_spent_finished_run_is_not_reopened_and_nothing_is_appended(demo, command):
    run_dir, _ = demo
    before = _rows(run_dir)
    args = (["run", DEMO, "--out", run_dir] if command == ("run",) else [command[0], run_dir])
    result = _invoke(*args)
    assert result.exit_code == 0, result.output
    assert _rows(run_dir) == before, result.output
    if command == ("stop",):
        assert "already finished; nothing to stop" in result.output
    else:
        assert "already finished: 6/6 experiments" in result.output
        assert f"looplab resume {run_dir} --max-nodes 12" in result.output
    if command == ("run",):
        assert "--out" in result.output.split("Or start a fresh run")[1]


def test_a_raised_budget_still_continues_the_run(demo):
    run_dir, _ = demo
    result = _invoke("resume", run_dir, "--max-nodes", "8")
    assert result.exit_code == 0, result.output
    assert "nodes=8" in result.output, result.output


def test_an_assistant_budget_extend_still_continues_through_resume(demo):
    """`serve/assistant.py`'s `extend_budget` appends `budget_extend` and reopens through `resume` —
    the continuation a node-count rule would have refused (doc 75 §12)."""
    run_dir, _ = demo
    EventStore(run_dir / "events.jsonl").append("budget_extend", {"add_nodes": 2})
    result = _invoke("resume", run_dir)
    assert result.exit_code == 0, result.output
    assert "nodes=8" in result.output, result.output


def test_inspect_of_the_finished_demo_is_six_lines_without_stuck_run_diagnostics(demo):
    run_dir, _ = demo
    result = _invoke("inspect", run_dir)
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert len(lines) <= 6, lines
    assert lines[0].startswith("run=") and "nodes=6 evaluated=6" in lines[0]
    for gone in ("names no reason", "phase beacon", "stop evidence"):
        assert gone not in result.output, result.output
    assert any(line.startswith("comparability: declared (") for line in lines), lines


def test_an_interrupted_run_keeps_its_stop_evidence(tmp_path):
    """`--crash-after` hard-exits the process, so it runs in a child, as `test_end_to_end.py` does."""
    import os
    import subprocess
    import sys

    run_dir = tmp_path / "crash"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, "-m", "looplab.cli", "run", str(DEMO), "--out",
                           str(run_dir), "--crash-after", "3"],
                          cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    result = _invoke("inspect", run_dir)
    assert "stop evidence:" in result.output, result.output


def test_timings_of_a_seconds_long_run_reads_in_seconds_and_tokens_says_offline(demo):
    run_dir, _ = demo
    timings = _invoke("timings", run_dir).output
    assert "0.0 min" not in timings and " min" not in timings, timings
    assert timings.startswith("run wall clock ") and " s " in timings.splitlines()[0]
    assert "offline run (backend=toy): no model calls to attribute." in _invoke("tokens", run_dir).output


def test_a_run_refused_before_its_first_event_leaves_no_directory(tmp_path, monkeypatch):
    """doc 75 UX-33: driven through the real preflight refusal, only its network probe replaced."""
    import looplab.agents.preflight as preflight
    monkeypatch.setattr(preflight, "_probe_role_endpoints", lambda *a, **k: [
        preflight._ProbeFailure("unreachable", "the default target: Connection refused")])
    run_dir = tmp_path / "refused"
    result = _invoke("run", ROOT / "examples" / "toy_task.json", "--out", run_dir)
    assert result.exit_code == 2 and "Refused:" in result.output, result.output
    assert not run_dir.exists()


def test_the_preflight_refusal_ends_its_cause_with_one_full_stop(tmp_path, monkeypatch):
    """doc 75 UX-02: the provider's own error ends in a stop ("Connection error."), and the refusal
    appended another, so the first line a newcomer with no model read said "Connection error..".
    Both renderings (the run refusal and the wrap-up warning) share one list builder."""
    import looplab.agents.preflight as preflight
    dead = "strategist (m at http://127.0.0.1:9/v1): LLM request to http://127.0.0.1:9/v1 failed: Connection error."
    monkeypatch.setattr(preflight, "_probe_role_endpoints", lambda *a, **k: [
        preflight._ProbeFailure("unreachable", dead)])
    result = _invoke("run", ROOT / "examples" / "toy_task.json", "--out", tmp_path / "r")
    assert "Connection error.\n" in result.output and ".." not in result.output, result.output
    warning = preflight.wrap_up_endpoint_warning(None, timeout_s=0.1)
    assert "Connection error.\n" in warning and ".." not in warning
    monkeypatch.setattr(preflight, "_probe_role_endpoints", lambda *a, **k: [
        preflight._ProbeFailure("unreachable", "the default target: Connection refused")])
    assert "Connection refused.\n" in preflight.wrap_up_endpoint_warning(None, timeout_s=0.1)


def test_a_refusal_keeps_a_directory_this_command_did_not_create(tmp_path, monkeypatch):
    import looplab.agents.preflight as preflight
    monkeypatch.setattr(preflight, "_probe_role_endpoints", lambda *a, **k: [
        preflight._ProbeFailure("unreachable", "the default target: Connection refused")])
    run_dir = tmp_path / "mine"
    run_dir.mkdir()
    _invoke("run", ROOT / "examples" / "toy_task.json", "--out", run_dir)
    assert run_dir.is_dir()


def test_inspect_with_no_argument_lists_the_runs(demo, monkeypatch):
    run_dir, _ = demo
    monkeypatch.chdir(run_dir.parent.parent)
    (Path("runs")).mkdir()
    run_dir.rename(Path("runs") / "demo")
    result = _invoke("inspect")
    assert result.exit_code == 0, result.output
    assert "runs/demo" in result.output.replace("\\", "/") and "6 experiments" in result.output
    assert "finished" in result.output


def test_inspect_with_no_argument_and_no_runs_says_how_to_find_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _invoke("inspect")
    assert result.exit_code == 2 and "looplab inspect RUN_DIR" in result.output
