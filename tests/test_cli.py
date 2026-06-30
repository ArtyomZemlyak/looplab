"""CLI surface (Typer) smoke tests — catch broken option wiring that unit tests miss
(e.g. an option assigned to a non-existent Settings field)."""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from looplab.cli import app

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


def test_run_mlebench_offline_with_agent_flags(tmp_path):
    # Default backend=toy -> mlebench's templated k-NN runs offline. The agent flags must
    # be accepted and assigned to real Settings fields (regression: --agent-cmd used to
    # crash by assigning to a non-existent `aider_cmd`).
    result = runner.invoke(app, [
        "run", str(ROOT / "examples" / "mlebench_task.json"),
        "--out", str(tmp_path / "run"), "--max-nodes", "2",
        "--agent-cmd", "dummy", "--agent-surface", "*.py,*.txt",
        "--no-validate-agent", "--no-agent-patch-gate",
    ])
    assert result.exit_code == 0, result.output
    assert "BEST" in result.output


def test_run_toy_task_offline(tmp_path):
    result = runner.invoke(app, [
        "run", str(ROOT / "examples" / "toy_task.json"),
        "--out", str(tmp_path / "run"), "--max-nodes", "3",
    ])
    assert result.exit_code == 0, result.output
    assert "finished=True" in result.output


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert "LoopLab" in result.output


def test_run_bad_backend_errors_with_choices(tmp_path):
    # A typo'd backend must fail loudly (not silently degrade to the offline `toy` backend) and
    # name the valid choices.
    result = runner.invoke(app, [
        "run", str(ROOT / "examples" / "toy_task.json"),
        "--out", str(tmp_path / "run"), "--backend", "ll",
    ])
    assert result.exit_code != 0
    assert "toy, llm" in result.output


def test_run_missing_task_file_is_friendly(tmp_path):
    # A missing task file becomes a one-line error, not a raw Python traceback.
    result = runner.invoke(app, ["run", str(tmp_path / "nope.json")])
    assert result.exit_code != 0
    assert "not found" in result.output
    assert "Traceback" not in result.output


def test_inspect_missing_run_dir_errors(tmp_path):
    # A path with no events.jsonl must error clearly instead of printing a blank, exit-0 empty run.
    result = runner.invoke(app, ["inspect", str(tmp_path / "no_such_run")])
    assert result.exit_code != 0
    assert "no run found" in result.output


def test_ui_help_keeps_extra_name():
    # Regression: rich markup mode used to strip `[ui]`, turning the install hint into
    # `pip install 'looplab'` (wrong). Markdown mode keeps the extra name intact.
    result = runner.invoke(app, ["ui", "--help"])
    assert result.exit_code == 0, result.output
    assert "looplab[ui]" in result.output
