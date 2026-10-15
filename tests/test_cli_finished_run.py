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


@pytest.mark.parametrize("command_id", ["cmd_" + "c0f1" * 8, None])
def test_an_operator_intent_queued_after_the_finish_is_served_not_swallowed(demo, command_id):
    """Code review of UX-05: the server appends an intent (Force confirm, Deep research, Set
    strategy, Restart …) and spawns `looplab resume`. A no-op child left it unserved while the
    reconciler re-spawned it until the command timed out. Queued work reopens the run, budget or not
    — a server command intent (`_command_id`) or a plain control intent after the finish."""
    from looplab.events.replay import fold
    from looplab.events.types import EV_FORCE_CONFIRM
    run_dir, _ = demo
    store = EventStore(run_dir / "events.jsonl")
    best = fold(store.read_all()).best()
    data = {"node_id": best.id, "attempt": best.attempt}
    if command_id:
        data["_command_id"] = command_id
    store.append(EV_FORCE_CONFIRM, data)
    result = _invoke("resume", run_dir)
    assert result.exit_code == 0, result.output
    assert "already finished" not in result.output
    state = fold(store.read_all())
    assert best.id in state.confirmed_forced, "the forced confirm was served (confirm_done)"
    if command_id:
        acks = [event for event in store.read_all() if event.type == "command_ack"]
        assert any(event.data.get("command_id") == command_id for event in acks)


def test_an_extended_budget_is_counted_once_in_the_stop_line(demo):
    """Code review of UX-07: the plan re-cut after `budget_extend` already includes `add_nodes`, and
    the stop line added it again — 6 + 2 read as a budget of 10, and the run "finished normally"
    with its 8-node budget spent. The line now uses the engine's own arithmetic."""
    from looplab.events.types import EV_BUDGET_EXTEND
    run_dir, _ = demo
    EventStore(run_dir / "events.jsonl").append(EV_BUDGET_EXTEND, {"add_nodes": 2})
    result = _invoke("resume", run_dir)
    assert result.exit_code == 0, result.output
    assert "stop: finished — node budget spent (8/8 experiments)." in result.output, result.output


def test_stop_wait_on_a_finished_run_does_not_call_it_unfinalized(demo):
    """`stop --wait` still records its stop on a finished run (it must stand against a queued server
    action), but the line said "(frozen, not finalized) — `looplab resume` to continue, `looplab
    finalize` to wrap it up" about the finalized demo."""
    run_dir, _ = demo
    result = _invoke("stop", run_dir, "--wait", "--timeout", "5")
    assert result.exit_code == 0, result.output
    assert "already finished" in result.output
    assert "not finalized" not in result.output and "looplab resume" not in result.output


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


def test_a_refused_runs_cleanup_leaves_a_lock_another_engine_holds(tmp_path):
    """Code review of UX-33: the cleanup ran after our singleton was released, so a concurrent `run`
    on the same new `--out` that had just taken `engine.lock` lost it — and a third process could then
    create a fresh one: two engines on one log. Held elsewhere, nothing is removed."""
    from looplab.cli import _engine_singleton
    from looplab.cli.run_cmds import _remove_if_only_locks
    out = tmp_path / "new"
    with _engine_singleton(out) as other:          # another engine, mid-setup, owns the lock
        assert other
        _remove_if_only_locks(out)
        assert (out / "engine.lock").exists(), "a held lock was unlinked"
    _remove_if_only_locks(out)                      # free now: the empty refused dir goes
    assert not out.exists()


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


def test_inspect_lists_the_run_root_the_ui_uses_with_nested_runs(demo, monkeypatch, tmp_path):
    """Code review of UX-35: the list read a fixed `runs/` one level deep, so LOOPLAB_RUN_ROOT (which
    `ui`/`tui` honour) printed "no runs", and a `<root>/<campaign>/<seed>` layout listed nothing."""
    run_dir, _ = demo
    root = tmp_path / "elsewhere"
    (root / "campaign").mkdir(parents=True)
    run_dir.rename(root / "campaign" / "seed1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOOPLAB_RUN_ROOT", str(root))
    result = _invoke("inspect")
    assert result.exit_code == 0, result.output
    assert "campaign/seed1" in result.output.replace("\\", "/") and "6 experiments" in result.output


def test_inspect_with_no_argument_and_no_runs_says_how_to_find_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _invoke("inspect")
    assert result.exit_code == 2 and "looplab inspect RUN_DIR" in result.output


@pytest.mark.parametrize("task, remedy, absent", [
    # `looplab init --kind quadratic`'s snapshot: a builtin evaluation, no `eval` block to put
    # `eval.inputs` in — the advice named a field this task kind does not have.
    ({"kind": "quadratic", "goal": "min", "direction": "min"},
     "A `quadratic` task's evaluation is built in, with no inputs to declare; a "
     "`comparison_contract` on the task makes it decidable.", "eval.inputs"),
    ({"kind": "repo", "goal": "min", "direction": "min", "eval": {"command": ["python", "e.py"]}},
     "Declare `eval.inputs` on the task (or a `comparison_contract`) to make it decidable.", None),
])
def test_unknown_comparability_names_only_what_this_task_can_declare(tmp_path, task, remedy, absent):
    """`looplab inspect`'s UNKNOWN comparability line (doc 75 UX-07) on a run whose champion carries
    no comparability record, driven through the real command."""
    import json

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"goal": "min", "direction": "min", "task_id": "t"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"params": {"x": 1.0}}})
    store.append("node_evaluated", {"node_id": 0, "metric": 1.0})
    (run_dir / "task.snapshot.json").write_text(json.dumps(task), encoding="utf-8")
    result = _invoke("inspect", run_dir)
    assert result.exit_code == 0, result.output
    line = next(line for line in result.output.splitlines() if line.startswith("comparability:"))
    assert line.startswith("comparability: UNKNOWN"), line
    assert line.endswith(remedy), line
    if absent:
        assert absent not in line, line


def test_a_budget_raised_on_resume_is_the_one_the_summary_and_the_remedy_count(demo):
    """Review 2026-10-15 (driven): `resume --max-nodes` raises the budget for that engine only and
    never rewrites `config.snapshot.json`, so the summary of a demo resumed from 6 to 13 said "node
    budget spent (6/6 experiments)", `inspect` kept saying it, and a plain `resume` then said "13/6"
    and suggested `--max-nodes 12` — a budget the same check refuses, since 13 ids are already used."""
    run_dir, _ = demo
    raised = _invoke("resume", run_dir, "--max-nodes", "13")
    assert raised.exit_code == 0, raised.output
    assert "stop: finished — node budget spent (13/13 experiments)." in raised.output, raised.output
    inspected = _invoke("inspect", run_dir)
    assert "stop: finished — node budget spent (13/13 experiments)." in inspected.output, \
        inspected.output
    before = _rows(run_dir)
    plain = _invoke("resume", run_dir)
    assert plain.exit_code == 0 and _rows(run_dir) == before, plain.output
    assert "13 experiments, past this command's node budget of 6" in plain.output, plain.output
    assert f"looplab resume {run_dir} --max-nodes 19" in plain.output, plain.output
    # …and the remedy it names is one the check admits: the run continues.
    continued = _invoke("resume", run_dir, "--max-nodes", "19")
    assert continued.exit_code == 0 and "nodes=19" in continued.output, continued.output


def test_resume_reads_a_task_files_relative_paths_against_its_directory(tmp_path, monkeypatch):
    """Review 2026-10-15 (driven): `run` reads a task file's relative paths against the file's
    directory (`core/appconfig.py::resolve_task_paths`), but `resume --task-file` loaded the same
    file through the snapshot loader, against the current directory — refused from anywhere else."""
    import shutil
    work = tmp_path / "work"
    work.mkdir()
    shutil.copytree(ROOT / "examples" / "dataset_example", work / "dataset_example")
    task_file = work / "dataset_task.json"
    shutil.copy(ROOT / "examples" / "dataset_task.json", task_file)
    run_dir = tmp_path / "run"
    started = _invoke("run", task_file, "--backend", "toy", "--max-nodes", "1", "--out", run_dir)
    assert started.exit_code == 0, started.output
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    resumed = _invoke("resume", run_dir, "--task-file", task_file, "--max-nodes", "2")
    assert resumed.exit_code == 0, resumed.output
    assert "nodes=2 evaluated=2" in resumed.output, resumed.output


@pytest.mark.posix_only("fcntl advisory locks (flock)")
def test_a_refused_runs_cleanup_unlinks_engine_lock_while_it_still_holds_it(tmp_path, monkeypatch):
    """Review 2026-10-15: `engine.lock` was unlinked AFTER the re-taken singleton was released, so
    a concurrent `run` could take the lock on that inode in between and lose it to the unlink. At
    the moment of the unlink, nobody else may be able to take the lock."""
    import fcntl
    from looplab.cli.run_cmds import _remove_if_only_locks
    out = tmp_path / "new"
    out.mkdir()
    (out / "engine.lock").touch()
    lockable_at_unlink = []
    real_unlink = Path.unlink

    def _probing_unlink(self, *args, **kwargs):
        if self.name == "engine.lock":
            with open(self, "a+") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lockable_at_unlink.append(False)
                else:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                    lockable_at_unlink.append(True)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _probing_unlink)
    _remove_if_only_locks(out)
    assert lockable_at_unlink == [False], lockable_at_unlink
    assert not out.exists()
