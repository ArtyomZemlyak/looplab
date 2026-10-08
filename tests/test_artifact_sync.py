"""`eval.artifact_sync`: the operator's copy-out runs after a node's terminal, off the eval slot.

Incident 2026-10-06: a ten-hour training's checkpoint lived only on a mount that went away. Driven
through the real `_evaluate` with a real copy command; the copy never moves the node.
"""
from __future__ import annotations

import sys

import anyio

from factories import make_engine
from looplab.engine import artifact_sync
from looplab.events.replay import fold
from looplab.runtime.command_eval import RunResult

_COPY = ("import shutil, sys; shutil.copytree(sys.argv[1], sys.argv[2]); "
         "sys.exit(int(sys.argv[3]) if len(sys.argv) > 3 else 0)")


def _engine(tmp_path, command):
    engine = make_engine(tmp_path / "run")
    engine._eval_spec = {"artifact_sync": {"command": command, "timeout": 60.0}} if command else {}
    engine.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "r"}, "code": "print(1)"})

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        from pathlib import Path
        (Path(workdir) / "ckpt.bin").write_bytes(b"weights")
        return RunResult(exit_code=0, stdout='{"metric": 0.5}', metric=0.5, timed_out=False,
                         stderr="")

    engine._run_eval = fake_run_eval
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    assert artifact_sync.wait_for_inflight(30)
    events = engine.store.read_all()
    return engine, events, fold(events)


def test_a_finished_node_s_workdir_is_copied_and_receipted(tmp_path):
    dest = tmp_path / "durable" / "node_0"
    _engine_, events, st = _engine(
        tmp_path, [sys.executable, "-c", _COPY, "{workdir}", str(tmp_path / "durable") + "/node_{node_id}"])
    assert st.nodes[0].status.value == "evaluated"
    assert (dest / "ckpt.bin").read_bytes() == b"weights"
    rows = [e.data for e in events if e.type == "artifact_synced"]
    assert len(rows) == 1 and rows[0]["exit_code"] == 0 and rows[0]["node_id"] == 0
    assert rows[0]["command"][-1].endswith("/node_0"), "placeholders are rendered in the receipt"
    terminal = max(e.seq for e in events if e.type == "node_evaluated")
    assert min(e.seq for e in events if e.type == "artifact_synced") > terminal, (
        "the copy-out runs AFTER the terminal")


def test_a_failed_copy_is_reported_and_never_moves_the_node(tmp_path):
    _e, events, st = _engine(
        tmp_path, [sys.executable, "-c", _COPY, "{workdir}", str(tmp_path / "d"), "3"])
    assert st.nodes[0].status.value == "evaluated" and st.nodes[0].metric == 0.5
    rows = [e.data for e in events if e.type == "artifact_synced"]
    assert [r["exit_code"] for r in rows] == [3]
    assert (tmp_path / "run" / "artifact_sync.log").exists()


def test_a_missing_tool_is_a_receipt_not_a_crash(tmp_path):
    _e, events, st = _engine(tmp_path, ["/nonexistent/mc", "cp", "{workdir}", "x/"])
    assert st.nodes[0].status.value == "evaluated"
    rows = [e.data for e in events if e.type == "artifact_synced"]
    assert len(rows) == 1 and rows[0]["exit_code"] != 0


def test_nothing_declared_runs_nothing(tmp_path):
    _e, events, st = _engine(tmp_path, None)
    assert st.nodes[0].status.value == "evaluated"
    assert not any(e.type == "artifact_synced" for e in events)


def test_only_known_placeholders_are_rendered():
    argv = artifact_sync.render_argv(
        ["{workdir}", "{run_id}/{node_id}.{generation}", "{unknown}", "{}", "a{b"],
        {"workdir": "/w", "run_dir": "/r", "run_id": "v1", "node_id": 4, "generation": 2})
    assert argv == ["/w", "v1/4.2", "{unknown}", "{}", "a{b"]


def test_the_spec_refuses_an_empty_command():
    import pytest
    from pydantic import ValidationError

    from looplab.adapters.repo_task import ArtifactSyncSpec
    with pytest.raises(ValidationError):
        ArtifactSyncSpec(command=[])
    with pytest.raises(ValidationError):
        ArtifactSyncSpec(command=["mc"], timeout=0)
