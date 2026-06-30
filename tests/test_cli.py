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


# --- YAML / unified config + no-file runs --------------------------------------------------------

def test_init_writes_parseable_documented_template(tmp_path):
    import yaml
    dest = tmp_path / "looplab.yaml"
    result = runner.invoke(app, ["init", "--out", str(dest)])
    assert result.exit_code == 0, result.output
    doc = yaml.safe_load(dest.read_text())
    assert set(("task", "settings", "out")) <= set(doc)
    # The whole template must be valid YAML, incl. the comment alignment (a `#` glued to a value
    # would corrupt e.g. llm_base_url).
    assert doc["settings"]["llm_base_url"].endswith("/v1")


def test_run_unified_yaml_applies_task_and_settings(tmp_path):
    cfg = tmp_path / "run.yaml"
    cfg.write_text(
        "out: %s\n"
        "task:\n  kind: quadratic\n  goal: min\n  direction: min\n"
        "settings:\n  max_nodes: 2\n" % (tmp_path / "r"))
    result = runner.invoke(app, ["run", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "finished=True" in result.output
    assert "nodes=2" in result.output                       # settings: block was honored
    assert (tmp_path / "r" / "events.jsonl").exists()       # out: was honored


def test_run_no_file_from_flags(tmp_path):
    result = runner.invoke(app, [
        "run", "--kind", "quadratic", "--goal", "min x^2", "--direction", "min",
        "--set", "max_nodes=2", "--out", str(tmp_path / "r"),
    ])
    assert result.exit_code == 0, result.output
    assert "finished=True" in result.output
    # Self-describing: a no-file run still writes a task snapshot it can resume from.
    assert (tmp_path / "r" / "task.snapshot.json").exists()


def test_run_set_unknown_key_errors(tmp_path):
    result = runner.invoke(app, [
        "run", "--kind", "quadratic", "--goal", "g", "--out", str(tmp_path / "r"),
        "--set", "max_node=9",
    ])
    assert result.exit_code != 0
    assert "unknown setting" in result.output


def test_run_set_overrides_file(tmp_path):
    cfg = tmp_path / "run.yaml"
    cfg.write_text("task:\n  kind: quadratic\n  goal: g\n  direction: min\n"
                   "settings:\n  max_nodes: 5\n")
    result = runner.invoke(app, ["run", str(cfg), "--out", str(tmp_path / "r"), "-s", "max_nodes=1"])
    assert result.exit_code == 0, result.output
    assert "nodes=1" in result.output                       # --set wins over the file


def test_run_no_task_errors():
    result = runner.invoke(app, ["run"])
    assert result.exit_code != 0
    assert "no task" in result.output


# --- Genesis: --goal with no --kind lets the LLM infer the task kind ------------------------------

def _patch_genesis(monkeypatch, task):
    """Stub the LLM client construction and the authoring call so the genesis path runs offline."""
    import looplab.cli as cli
    import looplab.genesis as genesis
    monkeypatch.setattr(cli, "make_llm_client", lambda settings, **k: object())
    monkeypatch.setattr(genesis, "author_task",
                        lambda goal, **k: genesis.GenesisResult(task=task, rationale="inferred"))


def test_run_goal_only_infers_kind_and_runs(tmp_path, monkeypatch):
    _patch_genesis(monkeypatch, {"kind": "quadratic", "goal": "g", "direction": "min",
                                 "bounds": {"x": [-10.0, 10.0], "y": [-10.0, 10.0]}})
    result = runner.invoke(app, [
        "run", "--goal", "minimize (x-3)^2", "-s", "max_nodes=2", "--out", str(tmp_path / "g"),
    ])
    assert result.exit_code == 0, result.output
    assert "Genesis -> kind=quadratic" in result.output       # it inferred + announced the kind
    assert "finished=True" in result.output                   # …and actually ran it


def test_run_no_genesis_falls_back_without_llm(tmp_path):
    # --no-genesis must NOT call the model: a goal with no kind uses the legacy default kind.
    result = runner.invoke(app, [
        "run", "--no-genesis", "--goal", "anything", "-s", "max_nodes=1", "--out", str(tmp_path / "n"),
    ])
    assert result.exit_code == 0, result.output
    assert "Genesis ->" not in result.output


def test_run_explicit_kind_skips_genesis(tmp_path, monkeypatch):
    # An explicit --kind must short-circuit genesis entirely (no model call).
    import looplab.cli as cli
    def _boom(*a, **k):  # would raise if genesis tried to build a client
        raise AssertionError("genesis must not run when --kind is given")
    monkeypatch.setattr(cli, "make_llm_client", _boom)
    result = runner.invoke(app, [
        "run", "--kind", "quadratic", "--goal", "g", "--direction", "min",
        "-s", "max_nodes=1", "--out", str(tmp_path / "k"),
    ])
    assert result.exit_code == 0, result.output
    assert "Genesis ->" not in result.output
