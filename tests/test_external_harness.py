"""The discovery contract and native CLI presets must agree with actual wiring."""
import json
from types import SimpleNamespace

from typer.testing import CliRunner

from looplab.agents.cli_agent import CliAgentDeveloper, PRESETS
from looplab.agents.external_harness import external_harness_brief
from looplab.cli import app
from looplab.cli.harness_cmds import harness_manifest


def test_harness_discovery_is_json_and_names_only_implemented_backends():
    result = CliRunner().invoke(app, ["harness"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == harness_manifest()
    assert payload["protocol_version"] == 3
    assert payload["mode"] == "external_harness_or_delegated_developer"
    assert "inject_node" in payload["external_run_mode"]["submission"]
    assert {"codex", "claude"} <= set(payload["developer_backends"])
    assert "settings_schema" not in payload
    assert "stages" in [step["id"] for step in payload["node_build"]["optional_agent_actions"]]


def test_harness_can_export_config_schema_without_credentials():
    payload = harness_manifest(include_settings=True)
    assert "developer_backend" in payload["settings_schema"]["properties"]
    assert isinstance(payload["settings_help"], dict)


def test_codex_and_claude_presets_use_native_unattended_edit_modes():
    codex = CliAgentDeveloper(model="native", spec=PRESETS["codex"])
    claude = CliAgentDeveloper(model="native", spec=PRESETS["claude"])
    codex_argv = codex._argv("change one file", "solution.py", PRESETS["codex"].argv)
    claude_argv = claude._argv("change one file", "solution.py", PRESETS["claude"].argv)
    assert codex_argv == ["codex", "exec", "--sandbox", "workspace-write",
                          "--skip-git-repo-check", "change one file"]
    assert claude_argv == ["claude", "--print", "--permission-mode", "acceptEdits",
                           "change one file"]
    assert PRESETS["codex"].needs_git and PRESETS["claude"].needs_git


def test_external_stage_guidance_tracks_operator_and_patch_gate():
    task = SimpleNamespace(eval=SimpleNamespace(stages=[]))
    spec = {"editables": [{"name": "."}], "edit_surface": ["**/*.py"],
            "protected_names": []}
    assert "outside the allowed edit surface" in external_harness_brief(task, spec)
    spec["edit_surface"].append("looplab_stages.json")
    assert "You may, only if useful" in external_harness_brief(task, spec)
    spec["protected_names"] = ["looplab_stages.json"]
    assert "outside the allowed edit surface or protected" in external_harness_brief(task, spec)
    task.eval.stages = [{"name": "train", "command": ["python", "train.py"]}]
    assert "operator already declared" in external_harness_brief(task, spec)
