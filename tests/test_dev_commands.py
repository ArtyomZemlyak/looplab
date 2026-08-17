"""Operator-pinned repo Developer commands: exact authority, disposable effects, typed receipts."""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from looplab.adapters.repo_developer import LLMRepoDeveloper
from looplab.adapters.repo_task import DeveloperCommandSpec, EvalSpec, RepoTask
from looplab.adapters.tasks import validate_task
from looplab.engine import workspace_seed
from looplab.tools.dev_commands import (DevCommandTools, DeveloperCommandRuntime)


def _repo_task(root: Path, commands) -> RepoTask:
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("print('source')\n", encoding="utf-8")
    return RepoTask(editable_path=str(root), seed_mode="all",
                    eval=EvalSpec(command=[sys.executable, "main.py"]),
                    developer_commands=commands)


@pytest.mark.parametrize("bad", [
    {"name": "x", "command": "python -V"},
    {"name": "x", "command": ["python"], "cwd": "../outside"},
    {"name": "x", "command": ["python"], "cwd": "/tmp"},
    {"name": "x", "command": ["python"], "cwd": "C:outside"},
    {"name": "x", "command": ["python"], "timeout": 601},
    {"name": "x", "command": ["python"], "timeout": True},
    {"name": "9bad", "command": ["python"]},
    {"name": "x", "command": ["python"], "env": {"API_TOKEN": "secret"}},
    {"name": "x", "command": ["python"], "shell": True},
])
def test_command_contract_rejects_ambient_or_unbounded_authority(bad):
    with pytest.raises(ValidationError):
        DeveloperCommandSpec.model_validate(bad)


def test_command_environment_uses_the_shared_declared_environment_coercion():
    spec = DeveloperCommandSpec(name="check", command=["python", "-V"],
                                env={"OMP_NUM_THREADS": 4})
    assert spec.env == {"OMP_NUM_THREADS": "4"}
    with pytest.raises(ValidationError):
        DeveloperCommandSpec(name="check", command=["python", "-V"], env={"DEBUG": True})


def test_repo_task_refuses_duplicate_command_names(tmp_path):
    with pytest.raises(ValidationError, match="duplicate developer command"):
        _repo_task(tmp_path, [
            {"name": "check", "command": ["python", "-V"]},
            {"name": "check", "command": ["python", "-m", "compileall", "."]},
        ])


def test_composable_task_keeps_the_command_grant_in_its_snapshot_model(tmp_path):
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")
    task = validate_task({
        "repo": str(tmp_path), "goal": "g", "direction": "max",
        "cmd": {"command": [sys.executable, "main.py"]},
        "developer_commands": [{"name": "compile", "command": [
            sys.executable, "-m", "compileall", "-q", "."]}],
    })
    assert task.developer_commands[0].name == "compile"
    assert task.repo_spec()["developer_commands"][0]["command"][-1] == "."


def test_model_can_choose_only_a_name_and_command_runs_on_the_staged_overlay(tmp_path):
    task = _repo_task(tmp_path, [{
        "name": "check", "command": [sys.executable, "main.py"], "timeout": 10,
    }])
    staged = SimpleNamespace(files={"main.py": "print('staged')\n"}, deleted=[])
    tool = DevCommandTools(task.repo_spec(), staged=staged)
    # Extra model-authored argv/cwd fields are inert: only the pinned spec is read.
    result = tool.execute_result("run_dev_command", {
        "name": "check", "command": ["sh", "-c", "echo PWNED"], "cwd": "/",
    })
    assert result.is_error is False
    assert "staged" in result.content and "PWNED" not in result.content
    assert result.structured["argv"] == (sys.executable, "main.py")
    assert result.structured["workspace"] == "disposable"
    assert result.structured["changes_retained"] is False
    assert result.structured["retention_scope"] == "candidate_tree"
    assert task.repo_spec()["developer_commands"][0]["command"] == [sys.executable, "main.py"]
    assert tool.capabilities()[0].risk == "high"


def test_candidate_uses_the_shared_eval_workspace_seeding_rule(tmp_path, monkeypatch):
    task = _repo_task(tmp_path, [{"name": "check", "command": [sys.executable, "main.py"]}])
    original = workspace_seed.seed_repo_tree
    seen = []

    def recording_seed(*args, **kwargs):
        seen.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(workspace_seed, "seed_repo_tree", recording_seed)
    result = DevCommandTools(task.repo_spec()).execute_result(
        "run_dev_command", {"name": "check"})
    assert result.is_error is False and len(seen) == 1


def test_command_created_files_and_overlay_changes_never_touch_the_source(tmp_path):
    command = [sys.executable, "-c",
               "from pathlib import Path; Path('made.txt').write_text('x'); "
               "Path('main.py').write_text('changed')"]
    task = _repo_task(tmp_path, [{"name": "write_scratch", "command": command}])
    before = (tmp_path / "main.py").read_bytes()
    result = DevCommandTools(task.repo_spec()).execute_result(
        "run_dev_command", {"name": "write_scratch"})
    assert result.is_error is False
    assert not (tmp_path / "made.txt").exists()
    assert (tmp_path / "main.py").read_bytes() == before


def test_operator_can_pin_a_bash_validator_without_granting_a_shell_string(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is not installed")
    task = _repo_task(tmp_path, [{
        "name": "bash_validate", "command": ["bash", "scripts/validate.sh"],
    }])
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "validate.sh").write_text(
        "set -euo pipefail\npython -m py_compile main.py\nprintf 'validator-ok\\n'\n",
        encoding="utf-8")
    result = DevCommandTools(task.repo_spec()).execute_result(
        "run_dev_command", {"name": "bash_validate", "script": "echo model-authored"})
    assert result.is_error is False and "validator-ok" in result.content
    assert result.structured["argv"] == ("bash", "scripts/validate.sh")


def test_compile_failure_is_a_typed_error_with_policy_and_overlay_receipts(tmp_path):
    task = _repo_task(tmp_path, [{
        "name": "compile", "command": [sys.executable, "-m", "compileall", "-q", "."],
    }])
    staged = SimpleNamespace(files={"broken.py": "def nope(:\n"}, deleted=[])
    result = DevCommandTools(task.repo_spec(), staged=staged).execute_result(
        "run_dev_command", {"name": "compile"})
    assert result.is_error is True and result.structured["exit_code"] != 0
    assert "SyntaxError" in result.content
    assert set(result.receipt) >= {
        "policy_sha256", "runtime_policy_sha256", "command_sha256", "overlay_sha256",
    }
    assert result.structured["trust_mode"] == "trusted_local"
    assert not (tmp_path / "broken.py").exists()


def test_in_flight_command_is_cooperatively_cancelled(tmp_path):
    task = _repo_task(tmp_path, [{
        "name": "wait", "command": [sys.executable, "-c", "import time; time.sleep(30)"],
        "timeout": 60,
    }])
    started = time.monotonic()
    result = DevCommandTools(task.repo_spec()).execute_result(
        "run_dev_command", {"name": "wait"},
        cancel_check=lambda: time.monotonic() - started > 0.15)
    assert time.monotonic() - started < 5
    assert result.is_error is True and result.structured["cancelled"] is True
    assert "CANCELLED" in result.content


def test_untrusted_command_reuses_the_hardened_docker_tier(tmp_path, monkeypatch):
    task = _repo_task(tmp_path, [{"name": "check", "command": ["python", "main.py"]}])
    seen = {}

    monkeypatch.setattr("looplab.runtime.command_eval.require_docker_cli", lambda _what: None)

    def fake_run(argv, workdir, timeout, **kwargs):
        seen["argv"] = list(argv)
        return 0, "ok", "", False

    monkeypatch.setattr("looplab.runtime.sandbox.run_argv", fake_run)
    runtime = DeveloperCommandRuntime(trust_mode="hostile", sandbox_memory="4g")
    result = DevCommandTools(task.repo_spec(), runtime=runtime).execute_result(
        "run_dev_command", {"name": "check"})
    assert result.is_error is False
    assert result.structured["trust_mode"] == "hostile"
    assert result.receipt["runtime_policy_sha256"]
    argv = seen["argv"]
    assert argv[:2] == ["docker", "run"]
    assert "--network" in argv and "none" in argv
    assert "--memory" in argv and "4g" in argv
    assert "--runtime" in argv and "runsc" in argv


def test_developer_prompt_and_tools_advertise_only_declared_commands(tmp_path):
    task = _repo_task(tmp_path, [{
        "name": "validate", "command": [sys.executable, "main.py"],
        "description": "validate the candidate",
    }])
    dev = LLMRepoDeveloper(object(), task, probe=False)
    system = dev._system_body(lambda _store, _key, default: default)
    assert "run_dev_command(name)" in system and "NO arbitrary shell" in system
    assert "operator-pinned validation command receives the task's declared inputs" in system
    assert "NOT readable by your tools here" not in system
    names = [spec["function"]["name"] for provider in dev._scout_tools()
             for spec in provider.specs()]
    assert names.count("run_dev_command") == 1 and "run_probe" not in names

    plain = _repo_task(tmp_path / "plain", [])
    plain_dev = LLMRepoDeveloper(object(), plain, probe=False)
    plain_system = plain_dev._system_body(lambda _store, _key, default: default)
    assert "run_dev_command" not in plain_system
    assert "There is NO shell / bash / run-command tool" in plain_system
    # The generic brief is also consumed by external CLI agents, which do not receive this in-house
    # tool; it must not advertise an authority that their native shell does not enforce.
    assert "run_dev_command" not in task.agent_brief()
