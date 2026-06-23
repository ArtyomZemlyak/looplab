"""ADR-7: tool-agnostic external CLI coding agent as a Developer backend. Offline
plumbing uses a stub agent (a script that edits solution.py); a guarded live test uses
real OpenCode + Ollama."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest

from looplab.cli_agent import PRESETS, CliAgentDeveloper
from looplab.models import Idea

ROOT = Path(__file__).resolve().parents[1]
_OPENCODE = Path(os.environ.get("APPDATA", "")) / "npm" / "opencode.cmd"


def _stub(tmp_path) -> list[str]:
    """A fake agent: ignores its flags, writes a known solution.py in cwd."""
    s = tmp_path / "stub_agent.py"
    s.write_text(
        'import pathlib\n'
        'pathlib.Path("solution.py").write_text('
        '\'import json\\nprint(json.dumps({"metric": 0.5}))\\n\')\n',
        encoding="utf-8")
    return [sys.executable, str(s)]


def test_presets_exist():
    assert {"opencode", "aider", "goose", "continue"} <= set(PRESETS)


def test_cli_agent_implement_plumbing(tmp_path):
    dev = CliAgentDeveloper(model="ollama/x", brief="solve it",
                            spec=PRESETS["opencode"], cmd_override=_stub(tmp_path))
    code = dev.implement(Idea(operator="draft", params={"degree": 2.0}))
    assert 'json.dumps({"metric": 0.5})' in code   # read back the agent's edit


def test_cli_agent_repair_plumbing(tmp_path):
    dev = CliAgentDeveloper(model="ollama/x", spec=PRESETS["opencode"],
                            cmd_override=_stub(tmp_path))
    fixed = dev.repair(Idea(operator="debug", params={}), "raise RuntimeError()", "boom")
    assert "metric" in fixed


def test_cli_agent_missing_binary_leaves_seed(tmp_path):
    # nonexistent launcher -> OSError swallowed -> seed returned (loop's eval/debug copes)
    dev = CliAgentDeveloper(model="ollama/x", spec=PRESETS["opencode"],
                            cmd_override=[str(tmp_path / "nope.exe")])
    code = dev.implement(Idea(operator="draft", params={}))
    assert "TODO" in code and "metric" in code


def test_make_roles_selects_cli_agent():
    # Default wraps the CLI agent in a ValidatingDeveloper (audit + fallback, ADR-7),
    # drops a self-contained opencode.json in the workdir, and falls back to the LLM dev.
    from looplab.config import Settings
    from looplab.roles import LLMDeveloper, ValidatingDeveloper
    from looplab.tasks import load_task, make_roles
    s = Settings()
    s.backend, s.developer_backend = "llm", "opencode"
    task = load_task(ROOT / "examples" / "code_regression_task.json")
    _, developer = make_roles(task, s)
    assert isinstance(developer, ValidatingDeveloper)
    inner = developer.inner
    assert isinstance(inner, CliAgentDeveloper)
    assert inner.model == "ollama/qwen3:8b" and inner.spec.name == "opencode"
    assert developer.brief
    assert "opencode.json" in inner.workdir_files            # self-contained provider cfg
    assert "localhost:11434/v1" in inner.workdir_files["opencode.json"]
    assert isinstance(developer.fallback, LLMDeveloper)       # known-good fallback path


def test_make_roles_raw_agent_when_validation_off():
    from looplab.config import Settings
    from looplab.tasks import load_task, make_roles
    s = Settings()
    s.backend, s.developer_backend, s.validate_agent = "llm", "opencode", False
    task = load_task(ROOT / "examples" / "code_regression_task.json")
    _, developer = make_roles(task, s)
    assert isinstance(developer, CliAgentDeveloper)          # no wrapper


def _opencode_ready():
    # Opt-in only (a live model call): enable with LOOPLAB_TEST_OPENCODE=1 on a box with
    # a working `opencode` + a local Ollama serving qwen3:8b. A self-contained
    # opencode.json (see opencode_config) points OpenCode at local Ollama so it does NOT
    # fetch the external model registry — the call that otherwise hangs behind a proxy.
    if os.environ.get("LOOPLAB_TEST_OPENCODE") != "1" or not _OPENCODE.exists():
        return False
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as r:
            return any("qwen3:8b" in m.get("name", "")
                       for m in json.loads(r.read()).get("models", []))
    except Exception:
        return False


def _live_agent(model="ollama/qwen3:8b"):
    from looplab.cli_agent import opencode_config
    return CliAgentDeveloper(
        model=model,
        brief='Write solution.py so it prints exactly one line of JSON: {"metric": 42.0}.',
        spec=PRESETS["opencode"], cmd_override=[str(_OPENCODE)], timeout=600.0,
        workdir_files={"opencode.json": opencode_config("http://localhost:11434/v1", model)})


@pytest.mark.skipif(not _opencode_ready(),
                    reason="set LOOPLAB_TEST_OPENCODE=1 with a working opencode + Ollama")
def test_live_opencode_integration_mechanics(tmp_path):
    # Proves the *integration* works (launcher resolved, headless run, output captured &
    # parseable). NOT the model's edit quality in one shot — qwen3:8b's edit tool is
    # flaky, so single-shot content is asserted only via the validated/retry test below.
    dev = _live_agent()
    code = dev.implement(Idea(operator="draft", params={}, rationale="set metric to 42"))
    import ast
    assert dev.last_run is not None and dev.last_run.launched   # subprocess actually ran
    assert not dev.last_run.timed_out
    assert code.strip()                                         # produced something
    ast.parse(code)                                            # and it's valid Python


@pytest.mark.skipif(not _opencode_ready(),
                    reason="set LOOPLAB_TEST_OPENCODE=1 with a working opencode + Ollama")
def test_live_opencode_validated_ships_valid_code(tmp_path):
    # End-to-end through the validator: with retries + LLM fallback the developer ALWAYS
    # ships valid code. With enough retries the flaky agent usually succeeds; if it can't,
    # the fallback guarantees a valid result — that robustness is the contract we assert.
    from looplab.roles import LLMDeveloper, ValidatingDeveloper
    from looplab.parse import LLMClient  # noqa: F401 (type hint only)
    from looplab.tasks import make_llm_client
    from looplab.config import Settings
    fallback = LLMDeveloper(make_llm_client(Settings()),
                            brief='Print exactly: {"metric": 42.0}')
    dev = ValidatingDeveloper(_live_agent(), fallback=fallback, max_retries=3)
    code = dev.implement(Idea(operator="draft", params={}, rationale="set metric to 42"))
    import ast
    ast.parse(code)                                  # shipped code is always valid Python
    assert dev.last_shipped_ok                        # validator confirms the shipped code
    if not dev.last_fell_back:                        # when the agent itself succeeded …
        assert dev.last_report.ok                     # … its report is clean (modified seed, parses)
    assert dev.last_report is not None and dev.last_report.ok, dev.last_report.feedback()
