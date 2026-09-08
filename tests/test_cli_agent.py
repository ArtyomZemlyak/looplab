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


def test_batch_shim_never_carries_the_prompt_on_its_command_line(tmp_path):
    """A `.cmd`/`.bat` launcher must not receive model-authored text in argv.

    cmd.exe re-parses the command line it is handed even though Popen gets an argv LIST with
    shell=False — CPython's list2cmdline implements MS-C-runtime quoting only, so `%VAR%`, `&`, `^`
    or a closing quote inside the prompt reaches the host shell. The launcher stays supported (it is
    how several agents ship on Windows); what changes is that the untrusted text moves into a file in
    the agent's own worktree and argv carries only a fixed ASCII sentence plus our own constant name.
    """
    from looplab.agents.cli_agent import PRESETS, CliAgentDeveloper, _PROMPT_FILE

    hostile = 'fix it & echo %PATH% ^ "; del *.*'
    dev = CliAgentDeveloper(model="m", spec=PRESETS["opencode"],
                            cmd_override=["C:\\\\npm\\\\opencode.cmd"])
    base = dev._launch_base()
    argv_message, via_file = dev._prompt_delivery(hostile, base)
    assert via_file is True
    assert hostile not in argv_message and "&" not in argv_message and "%" not in argv_message
    assert _PROMPT_FILE in argv_message
    assert hostile not in " ".join(dev._argv(argv_message, "solution.py", base))

    # an ordinary launcher is byte-identical to before: argv IS a safe channel with shell=False,
    # so the preset's contract must not change for everyone to fix a Windows-only path.
    plain = CliAgentDeveloper(model="m", spec=PRESETS["opencode"], cmd_override=["/usr/bin/opencode"])
    pbase = plain._launch_base()
    pmsg, pvia = plain._prompt_delivery(hostile, pbase)
    assert pvia is False and pmsg == hostile
    assert hostile in plain._argv(pmsg, "solution.py", pbase)


def test_a_timed_out_agent_keeps_what_it_printed_before_it_hung(tmp_path):
    """A hung agent usually says where it got stuck right before it stops making progress. Recording
    only `str(TimeoutExpired)` threw that away, so validation and the repair loop saw a bare
    "timed out" for a run that had already explained itself."""
    child = tmp_path / "chatty_hang.py"
    child.write_text(
        "import sys, time\n"
        "print('resolving workspace deps')\n"
        "print('ERROR: language server never became ready', file=sys.stderr)\n"
        "sys.stdout.flush(); sys.stderr.flush()\n"
        "time.sleep(120)\n",
        encoding="utf-8")
    dev = CliAgentDeveloper(model="ollama/x", spec=PRESETS["opencode"],
                            cmd_override=[sys.executable, str(child)], timeout=1.5)
    dev.implement(Idea(operator="draft", params={}))

    run = dev.last_run
    assert run is not None and run.timed_out is True
    assert "resolving workspace deps" in (run.stdout_tail or "")
    assert "language server never became ready" in (run.stderr_tail or "")
    assert "timed out" in (run.stderr_tail or "").lower()      # the notice is kept too


def test_a_cli_agents_captured_tails_are_screened_before_anything_can_read_them(tmp_path,
                                                                                monkeypatch):
    """C2 sweep: `cli_agent` builds three `AgentRun`s and NONE of them could call `Engine._redact`.

    That is a LAYERING fact — the funnel is an engine method and `agents/` may not import upward —
    so the screen lives on the `core` type every producer constructs (`core/validate.py::AgentRun`),
    not as a copied regex at three call sites. Driven through the real subprocess path: a stub agent
    prints a credential shape and one of THIS box's own secret env values, and the tails are read
    back off `last_run`.
    """
    # The value is planted in the PARENT's environment under a secret NAME. `cli_agent` strips such
    # names out of the child's env on purpose, so the child cannot read it — which is exactly how
    # these bytes reach a tail in the field: echoed by something that was configured with them
    # (a DSN in a library traceback, a tool printing its own resolved config), not by `os.environ`.
    monkeypatch.setenv("C2CLI_DB_PASSWORD", "hunter2-correct-horse")
    child = tmp_path / "leaky_agent.py"
    child.write_text(
        "import sys\n"
        "print('resolving workspace deps from /home/ci/models/rubert-tiny-lite-v2/final')\n"
        "print('auth sk-abcdefABCDEF0123456789TOKEN')\n"
        "print('psycopg2.OperationalError: password hunter2-correct-horse rejected', file=sys.stderr)\n"
        "open('solution.py', 'w').write('import json\\nprint(json.dumps({\"metric\": 1.0}))\\n')\n",
        encoding="utf-8")
    dev = CliAgentDeveloper(model="ollama/x", spec=PRESETS["opencode"],
                            cmd_override=[sys.executable, str(child)], timeout=60)
    dev.implement(Idea(operator="draft", params={}))

    run = dev.last_run
    assert run is not None and run.launched is True
    out, err = run.stdout_tail or "", run.stderr_tail or ""
    # The shape screen, and the env-VALUE screen — the two halves that need no configuration.
    assert "sk-abcdefABCDEF0123456789TOKEN" not in out and "sk-***" in out
    assert "hunter2-correct-horse" not in err and "***REDACTED_ENV***" in err
    # ...and the diagnostic an operator actually reads survives: no `Settings` is reachable from a
    # `core` dataclass, so the entropy half is deliberately NOT applied here and this path is intact.
    assert "/home/ci/models/rubert-tiny-lite-v2/final" in out
    assert "resolving workspace deps" in out


def test_the_agent_run_screen_is_the_shared_redactor_and_not_a_second_spelling(tmp_path):
    """A copied regex is the failure this is shaped to avoid, so pin that the two agree exactly."""
    from looplab.core.redact import redact_output_tail
    from looplab.core.validate import AgentRun

    raw = "Authorization: Bearer abcdefABCDEF0123456789 while reading /var/data/emb_v3/index.faiss"
    run = AgentRun(stdout_tail=raw, stderr_tail=raw)
    expected = redact_output_tail(raw, entropy=False)
    assert run.stdout_tail == expected == run.stderr_tail
    assert "abcdefABCDEF0123456789" not in run.stdout_tail
    assert "/var/data/emb_v3/index.faiss" in run.stdout_tail
    assert AgentRun().stdout_tail == "" and AgentRun().stderr_tail == ""


# --------------------------------------------------------------------------------------------
# doc 27 `external-cli-usage-is-unpriced`: the role that WRITES THE CODE reaches the run's ledger.

def test_every_launched_invocation_reaches_the_ledger_as_one_unpriced_call(tmp_path):
    """THE DEFECT: `CliAgentDeveloper` had no accountant at all, so an external coding agent's spend
    reached neither `llm_usage` nor `looplab tokens` — a run whose Developer is a CLI agent reported
    the cost of every role except the developer.

    UNPRICED is the honest record, and it is not the same as recording nothing: `calls` above
    `priced_calls` is the ledger's existing "we know a paid call happened and not what it cost".
    """
    from looplab.core.llm import CostAccountant

    accountant = CostAccountant()
    dev = CliAgentDeveloper(model="ollama/x", spec=PRESETS["opencode"],
                            cmd_override=_stub(tmp_path), accountant=accountant)
    dev.implement(Idea(operator="draft", params={}))
    dev.repair(Idea(operator="debug", params={}), "raise RuntimeError()", "boom")

    assert accountant.calls == 2                     # one per invocation, implement AND repair
    assert accountant.priced_calls == 0              # ...explicitly unpriced, never a parsed guess
    assert accountant.total_tokens == 0 and accountant.spent == 0.0
    assert dev.last_run.duration_s > 0.0             # the invocation's own wall clock is recorded


def test_a_launcher_that_never_started_spends_nothing(tmp_path):
    """A missing binary is not an unpriced call, it is no call: nothing ran, nothing was billed."""
    from looplab.core.llm import CostAccountant

    accountant = CostAccountant()
    dev = CliAgentDeveloper(model="ollama/x", spec=PRESETS["opencode"],
                            cmd_override=[str(tmp_path / "nope.exe")], accountant=accountant)
    dev.implement(Idea(operator="draft", params={}))
    assert dev.last_run.launched is False and accountant.calls == 0


def test_the_durable_ledger_walk_reaches_the_external_developer(tmp_path):
    """The ledger binds by walking role objects for an `accountant` attribute, and an external agent
    is reached through the ValidatingDeveloper's `inner`. Driven end to end through the walk and the
    sink, because the whole item is "the spend arrives in the ledger", not "an attribute exists"."""
    import types

    from looplab.agents.roles import ValidatingDeveloper
    from looplab.core.llm import CostAccountant
    from looplab.engine.costs import find_cost_accountants, sanitize_usage_delta

    accountant = CostAccountant()
    dev = CliAgentDeveloper(model="ollama/x", spec=PRESETS["opencode"],
                            cmd_override=_stub(tmp_path), accountant=accountant)
    engine_like = types.SimpleNamespace(
        developer=ValidatingDeveloper(dev, fallback=None, max_retries=0))
    assert accountant in find_cost_accountants(engine_like)

    deltas = []
    accountant.set_sink(lambda d: deltas.append(sanitize_usage_delta(d)))
    dev.implement(Idea(operator="draft", params={}))
    assert deltas == [{"cost": 0.0, "calls": 1, "priced_calls": 0, "prompt_tokens": 0,
                       "completion_tokens": 0, "total_tokens": 0}]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group liveness probe")
def test_a_cancel_token_stops_the_agent_without_waiting_for_its_timeout(tmp_path):
    """doc 27 `cancel-not-propagated-into-provider-request`, the external-CLI leg: the agent used to
    be killed on its TIMEOUT alone, so a stopped run kept a multi-minute subprocess — and the
    language server / training children it spawned — alive to the deadline.

    DRIVEN: a 120 s agent under a 120 s timeout, cancelled a moment after it starts. If the token did
    not reach the wait, this test would take two minutes and the record would say `timed_out`.
    """
    import time as _time

    started = tmp_path / "started"
    child = tmp_path / "slow_agent.py"
    child.write_text("import pathlib, sys, time\n"
                     "pathlib.Path(sys.argv[1]).write_text('go')\n"
                     "print('working', flush=True)\n"
                     "time.sleep(120)\n", encoding="utf-8")
    dev = CliAgentDeveloper(model="ollama/x", spec=PRESETS["opencode"],
                            cmd_override=[sys.executable, str(child), str(started)],
                            timeout=120.0, cancel_check=started.exists)

    began = _time.monotonic()
    dev.implement(Idea(operator="draft", params={}))
    elapsed = _time.monotonic() - began

    assert elapsed < 30, f"the cancel did not reach the wait ({elapsed:.1f}s)"
    assert dev.last_run.cancelled is True and dev.last_run.timed_out is False
    assert "working" in (dev.last_run.stdout_tail or "")   # what it printed first is kept
    assert "cancelled by the caller" in (dev.last_run.stderr_tail or "")


def test_the_ambient_request_cancel_token_reaches_the_agent_too(tmp_path):
    """No constructor argument needed: a caller that scopes `cancel_check_scope` around the build
    reaches the external developer exactly as it reaches an in-process client."""
    from looplab.core.llm import cancel_check_scope

    child = tmp_path / "slow_agent.py"
    child.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    dev = CliAgentDeveloper(model="ollama/x", spec=PRESETS["opencode"],
                            cmd_override=[sys.executable, str(child)], timeout=120.0)
    with cancel_check_scope(lambda: True):
        dev.implement(Idea(operator="draft", params={}))
    assert dev.last_run.cancelled is True


def test_the_invocation_opens_a_generation_span_so_its_spend_is_attributable(tmp_path):
    """CLAUDE.md: a phase that SPENDS must open a SPAN. Without one the external Developer's calls
    are money attributable to nothing — invisible to `looplab timings`, `looplab tokens` and the
    trace view — which is the same hole the unpriced ledger entry closes from the other side."""
    import json

    from looplab.core.llm import CostAccountant
    from looplab.core.tracing import JsonlSpanExporter, Tracer

    spans = tmp_path / "spans.jsonl"
    tracer = Tracer(JsonlSpanExporter(str(spans)), run_id="r")
    dev = CliAgentDeveloper(model="ollama/x", spec=PRESETS["opencode"],
                            cmd_override=_stub(tmp_path), accountant=CostAccountant())
    with tracer.span("implement", kind="operation"):
        dev.implement(Idea(operator="draft", params={}))
    tracer.force_flush()

    rows = [json.loads(line) for line in spans.read_text().splitlines() if line.strip()]
    generations = [r for r in rows if r.get("kind") == "generation"]
    assert len(generations) == 1, "the external agent's invocation opened no generation span"
    attrs = generations[0]["attributes"]
    assert attrs["op"] == "cli_agent" and attrs["model"] == "ollama/x"
    assert attrs["agent"] == "opencode" and attrs["unpriced"] is True
    assert attrs["duration_s"] >= 0.0
    # The commit stamps the span from below (`tracing.record_paid_call`), so the span agrees with
    # the ledger about the call having happened — at zero, which is what unpriced means.
    assert attrs["cost_billings"] == 1
