"""ADR-7: tool-agnostic external CLI coding agent as a Developer backend. Offline
plumbing uses a stub agent (a script that edits solution.py); a guarded live test uses
real OpenCode + Ollama."""
from __future__ import annotations

import json
import os
import signal
import sys
import urllib.request
from pathlib import Path

import pytest

from looplab.agents.cli_agent import PRESETS, CliAgentDeveloper
from looplab.core.models import Idea

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


def _pid_alive(pid: int) -> bool:
    """True while `pid` is a live (non-zombie) process. A reaped-pending zombie counts as dead — the
    tree-kill's job is to STOP the work, and a zombie no longer runs."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                              # Linux: a 'Z' state is a reaped-pending corpse
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rsplit(") ", 1)[1].split(" ", 1)[0] != "Z"
    except (FileNotFoundError, ProcessLookupError, IndexError, OSError):
        return False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group liveness probe")
def test_cli_agent_timeout_kills_the_whole_process_tree(tmp_path):
    # G4a: a CLI agent that spawns a grandchild (a nested train/LSP/git subprocess) then hangs. The
    # plain subprocess.run timeout SIGKILLs only the DIRECT child, orphaning the grandchild to keep
    # burning compute past the deadline; the own-process-group tree-kill must reap the grandchild too.
    import time
    pidfile = tmp_path / "grandchild.pid"
    child = tmp_path / "hang_agent.py"
    child.write_text(
        "import subprocess, sys, pathlib, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(g.pid))\n"
        "time.sleep(120)\n",
        encoding="utf-8")
    dev = CliAgentDeveloper(model="ollama/x", spec=PRESETS["opencode"],
                            cmd_override=[sys.executable, str(child), str(pidfile)], timeout=1.5)
    dev.implement(Idea(operator="draft", params={}))

    assert dev.last_run is not None and dev.last_run.timed_out is True
    for _ in range(60):                               # the stub records the grandchild pid promptly
        if pidfile.exists():
            break
        time.sleep(0.05)
    assert pidfile.exists(), "stub never recorded the grandchild pid"
    gpid = int(pidfile.read_text().strip())
    try:
        dead = False
        for _ in range(100):                          # give the tree-kill a moment to propagate
            if not _pid_alive(gpid):
                dead = True
                break
            time.sleep(0.05)
        assert dead, f"grandchild {gpid} survived the timeout — the tree-kill orphaned it"
    finally:
        # Self-cleaning: if the fix ever regresses the grandchild is a live `sleep(120)`; never leak
        # it out of the test (a CI re-run must not accumulate one detached sleeper per failing run).
        try:
            os.kill(gpid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


def test_make_roles_selects_cli_agent():
    # Default wraps the CLI agent in a ValidatingDeveloper (audit + fallback, ADR-7),
    # drops a self-contained opencode.json in the workdir, and falls back to the LLM dev.
    from looplab.core.config import Settings
    from looplab.agents.roles import LLMDeveloper, ValidatingDeveloper
    from looplab.adapters.tasks import load_task, make_roles
    s = Settings()
    s.backend, s.developer_backend, s.unified_agent = "llm", "opencode", False
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
    from looplab.core.config import Settings
    from looplab.adapters.tasks import load_task, make_roles
    s = Settings()
    s.backend, s.developer_backend, s.validate_agent = "llm", "opencode", False
    s.unified_agent = False
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
    from looplab.agents.cli_agent import opencode_config
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
    from looplab.agents.roles import LLMDeveloper, ValidatingDeveloper
    from looplab.core.parse import LLMClient  # noqa: F401 (type hint only)
    from looplab.adapters.tasks import make_llm_client
    from looplab.core.config import Settings
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


# #53 — opencode_config tolerates a trailing-slash model id
def test_opencode_config_trailing_slash():
    import json as _j
    from looplab.agents.cli_agent import opencode_config
    cfg = _j.loads(opencode_config("http://h:1", "ollama/"))
    assert "ollama" in cfg["provider"]
    models = cfg["provider"]["ollama"]["models"]
    assert "ollama/" not in models                     # not the broken empty-name id
