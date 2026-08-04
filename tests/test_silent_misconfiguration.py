"""Three misconfigurations that used to launch a run and then produce nothing anyone could act on.

Each was found by a documentation-vs-code audit and reproduced end to end before being fixed here.
They share one shape — the configuration is WRONG, the code has a defensive branch for it, and the
defensive branch is silent — so they share one test file:

  1. a `file_json`/`file_regex` metric spec authored without `path` passed submit-time validation
     (which checked only the reader NAME), the run started, and EVERY node failed `no_metric`;
  2. `--no-require-approval` was declared as a plain `bool`, so only the TRUE case was forwarded into
     the typed-settings dict and a `require_approval: true` config could not be overridden from the
     command line — the run then blocked forever on `looplab approve`;
  3. an unreachable LLM endpoint produced N identical empty "fallback" nodes, a flat metric and
     `finished=True`, with no error anywhere — and `backend` now defaults to "llm".

New FILE on purpose: these cut across `runtime`, `adapters`, `cli` and `agents`, so there is no one
existing module they belong in.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from looplab.core.config import Settings
from looplab.core.errors import LLMError

runner = CliRunner()


# ------------------------------------------------------- 1. a file reader with no file to read from
def _repo_task(metric: dict) -> dict:
    return {"kind": "repo", "id": "t", "goal": "g", "direction": "max",
            "editable_path": "examples/repo_example", "edit_surface": ["*.json"],
            "eval": {"command": ["python", "ttrain.py"], "metric": metric, "timeout": 60}}


def test_a_file_reader_without_a_path_is_refused_at_submit():
    """The reproduction: `{"kind": "file_json", "key": "metric"}` is a KNOWN reader with no file.

    `command_eval._read_file` returns None the instant `spec["path"]` is missing, so pre-fix this
    spec validated, the run started, and every node failed `no_metric` with nothing naming the
    cause. `key` is the key INSIDE the file, never the file — the message has to say so, because
    "I gave it a key" is exactly why the author thought the spec was complete.
    """
    from looplab.adapters.repo_task import RepoTask
    for kind in ("file_json", "file_regex"):
        with pytest.raises(ValueError) as ei:
            RepoTask(**_repo_task({"kind": kind, "key": "metric"}))
        msg = str(ei.value)
        assert "path" in msg and kind in msg, msg
        assert "no_metric" in msg, msg          # names the failure it is preventing


def test_a_file_reader_with_a_path_still_validates():
    """The guard must not reject the shape `examples/repo_drift_task.json` ships."""
    from looplab.adapters.repo_task import RepoTask
    task = RepoTask(**_repo_task({"kind": "file_json", "path": "metrics.json", "key": "metric"}))
    assert task.eval.metric["path"] == "metrics.json"


def test_the_composable_reader_spelling_is_validated_too():
    """`{"reader": "file_json"}` is normalized to `kind` by `adapters/tasks.py` BEFORE construction,
    so the load path — the one Genesis and the UI actually use — must reach the same refusal."""
    from looplab.adapters.tasks import validate_task
    with pytest.raises(ValueError, match="path"):
        validate_task({"id": "t", "goal": "g", "direction": "max",
                       "repo": "examples/repo_example",
                       "cmd": {"command": ["python", "ttrain.py"],
                               "metric": {"reader": "file_json", "key": "metric"}}})


def test_a_non_string_path_is_refused_because_it_crashes_the_run_not_the_node():
    """Worse than missing: `_confined` catches only (OSError, ValueError, RuntimeError), so
    `Path(workdir) / 123` raises an uncaught TypeError out of `read_metric` and takes down the RUN."""
    from looplab.adapters.repo_task import RepoTask
    with pytest.raises(ValueError, match="STRING"):
        RepoTask(**_repo_task({"kind": "file_json", "path": 123, "key": "metric"}))


def test_only_the_readers_that_need_a_path_demand_one():
    """`adapter` DEFAULTS its path and the stdout readers have none, so "needs a path" is a
    per-reader fact. Pinned against the reader table so a new reader can't inherit the rule by
    accident (or dodge it by being file-shaped)."""
    from looplab.runtime.command_eval import (METRIC_READERS, READERS_REQUIRING_PATH,
                                              metric_spec_path_error)
    assert READERS_REQUIRING_PATH <= set(METRIC_READERS)
    for kind in set(METRIC_READERS) - set(READERS_REQUIRING_PATH):
        assert metric_spec_path_error({"kind": kind}) is None, kind
    assert metric_spec_path_error({"key": "metric"}) is None          # default kind = stdout_json
    assert metric_spec_path_error({"kind": "file_json", "path": "  "}) is not None


def test_the_genesis_task_prompt_tells_the_model_about_path():
    """The prompt that AUTHORS these specs described the metric reader without ever naming `path`,
    so the LLM could produce the broken spec above by following our own instructions."""
    from looplab.serve.serve_prompts import genesis_system
    prompt = genesis_system(["repo"], {}, "")
    reader_line = next(ln for ln in prompt.splitlines() if "metric.reader options" in ln)
    assert '"path"' in reader_line, reader_line
    assert "file_json" in reader_line


# --------------------------------------------------------- 2. a bool flag with only one direction
def _typed_settings(monkeypatch, argv: list[str], tmp_path):
    """Run `looplab run` far enough to capture the merged Settings, then abort before any work."""
    import looplab.cli as cli
    seen = {}

    def _capture(_out, _task, settings, _crash_after, **_kw):
        seen["settings"] = settings
        raise RuntimeError("captured-before-run")

    monkeypatch.setattr(cli, "_engine", _capture)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(json.dumps({
        "task": {"id": "toy_quadratic", "kind": "quadratic", "goal": "g", "direction": "min",
                 "bounds": {"x": [-10.0, 10.0], "y": [-10.0, 10.0]}},
        "settings": {"backend": "toy", "max_nodes": 1, "require_approval": True},
    }), encoding="utf-8")
    runner.invoke(cli.app, ["run", str(cfg), "--out", str(tmp_path / "r"), *argv])
    return seen.get("settings")


def test_no_require_approval_overrides_the_config_file(monkeypatch, tmp_path):
    """The reproduction: a `require_approval: true` config plus `--no-require-approval` still ran
    with require_approval=True and ended finished=False, blocked forever on `looplab approve`.

    `docs/guide/cli-reference.md` documents "CLI over file over environment"; the old plain-`bool`
    declaration could only ever forward the TRUE case, so precedence held in one direction only.
    """
    assert _typed_settings(monkeypatch, ["--no-require-approval"], tmp_path).require_approval is False


def test_require_approval_is_tri_state_like_its_neighbours(monkeypatch, tmp_path):
    """Absent = keep the file's value (that is why it can't just default to False); present = win."""
    assert _typed_settings(monkeypatch, [], tmp_path).require_approval is True
    assert _typed_settings(monkeypatch, ["--require-approval"], tmp_path).require_approval is True
    # --set stays the FINAL precedence layer, above the typed flag.
    assert _typed_settings(
        monkeypatch, ["--require-approval", "-s", "require_approval=false"],
        tmp_path).require_approval is False


def test_every_settings_backed_bool_on_run_is_tri_state():
    """The class-level guard: a plain `bool` typer option whose name is a `Settings` field can only
    forward one of its two states, which is the whole defect. `genesis` /
    `speculation_gate_calibration` / `force` are flow-control flags, not settings, so they stay."""
    import inspect
    from looplab.cli.run_cmds import run
    checked = 0
    for name, param in inspect.signature(run).parameters.items():
        if name not in Settings.model_fields:
            continue
        # `run_cmds` has `from __future__ import annotations`, so these are STRINGS ('bool',
        # 'Optional[bool]'). Comparing against the `bool` OBJECT would make this guard vacuously
        # true and it would have passed on the very defect it exists to catch.
        assert isinstance(param.annotation, str)
        checked += 1
        assert param.annotation != "bool", (
            f"--{name.replace('_', '-')} is a Settings field declared as a plain bool; its "
            "--no- form cannot override the file/env value. Use Optional[bool] + a tri-state "
            "forward, like every other typed bool on this command.")
    assert checked, "no Settings-backed flags found — the signature scan stopped working"


# ------------------------------------------- 3. an unreachable provider that reported success
class _DeadClient:
    """Stands in for a client whose endpoint is not there. `probe` is the only method a preflight
    may call — a preflight that reached for anything else would be testing a different code path
    than the run does."""

    def __init__(self, *_a, **_kw):
        pass

    def probe(self, _messages, **_kw):
        raise LLMError("LLM request to http://127.0.0.1:9/v1 failed: Connection error.")


def test_an_unreachable_endpoint_refuses_to_start_the_run(monkeypatch):
    """The reproduction: `looplab run examples/toy_task.json --max-nodes 3` with no reachable
    endpoint produced three identical `x=0,y=0` nodes annotated "fallback (agent parse failed)", a
    metric flat at 10.0 and finished=True. Fail at the preflight instead — before any role, any
    event and any snapshot."""
    from looplab.agents import factory, preflight
    monkeypatch.setattr(factory, "make_llm_client", lambda *a, **k: _DeadClient())
    with pytest.raises(LLMError) as ei:
        preflight.preflight_role_endpoints(
            Settings(backend="llm", llm_model="m", llm_base_url="http://127.0.0.1:9/v1"))
    msg = str(ei.value)
    assert "preflight" in msg
    assert "http://127.0.0.1:9/v1" in msg and "m" in msg    # WHICH endpoint and WHICH model
    assert "backend=toy" in msg                              # and what to do instead


def test_the_offline_backend_is_never_probed(monkeypatch):
    """`--backend toy` must stay a fully offline command — no network, no cost, no new failure mode."""
    from looplab.agents import factory, preflight
    monkeypatch.setattr(factory, "make_llm_client",
                        lambda *a, **k: pytest.fail("toy backend built an LLM client"))
    preflight.preflight_role_endpoints(Settings(backend="toy"))


def test_a_reachable_endpoint_is_probed_once_per_distinct_target(monkeypatch):
    """A live-backend run resolves half a dozen roles; the single-model operator must still pay for
    exactly ONE four-token completion, and a role pointed somewhere else must still be probed."""
    from looplab.agents import factory, preflight
    probes: list[tuple] = []

    class _Ok:
        def __init__(self, **kw):
            self.kw = kw

        def probe(self, messages, **kw):
            probes.append((self.kw.get("base_url"), self.kw.get("model"), kw.get("max_tokens")))

    monkeypatch.setattr(factory, "make_llm_client", lambda _s, **kw: _Ok(**kw))
    preflight.preflight_role_endpoints(
        Settings(backend="llm", llm_model="m", llm_base_url="http://a/v1"))
    assert len(probes) == 1 and probes[0][0] == "http://a/v1"
    assert probes[0][2] == 4                                  # bounded output, not a real generation

    probes.clear()
    preflight.preflight_role_endpoints(
        Settings(backend="llm", llm_model="m", llm_base_url="http://a/v1",
                 developer_model="big", developer_base_url="http://b/v1"))
    assert sorted({p[0] for p in probes}) == ["http://a/v1", "http://b/v1"]


def test_the_probe_client_is_bounded_and_uncached(monkeypatch):
    """A preflight must not stream, must not be answered from cache, and must not be able to hang:
    the same probe-only controls `/api/llm/health` uses, so the health card and a run agree."""
    from looplab.agents import factory, preflight
    seen: dict = {}

    class _Ok:
        def probe(self, _messages, **_kw):
            pass

    def _factory(_settings, **kw):
        seen.update(kw)
        return _Ok()

    monkeypatch.setattr(factory, "make_llm_client", _factory)
    preflight.preflight_role_endpoints(
        Settings(backend="llm", llm_model="m", llm_base_url="http://a/v1"), timeout_s=7.0)
    assert seen["stream"] is False and seen["cache"] is False
    assert seen["disable_reasoning"] is True
    assert seen["wall_timeout"] == 7.0


def test_a_client_without_a_probe_is_not_read_as_an_unreachable_endpoint(monkeypatch):
    """`probe` belongs to the real transport, not to the `LLMClient` protocol
    (`core/parse.py` declares only complete_tool/complete_text). A test double or an integration
    supplying its own transport through the `make_llm_client` seam must not be turned into a run
    that refuses to start — the same optional-hook rule as `tools/_base.py::bind_state`."""
    from looplab.agents import factory, preflight
    monkeypatch.setattr(factory, "make_llm_client", lambda *a, **k: object())
    preflight.preflight_role_endpoints(
        Settings(backend="llm", llm_model="m", llm_base_url="http://a/v1"))


def test_an_external_developer_backend_is_not_probed_for_a_key_it_never_gets(monkeypatch):
    """External coding agents are launched with every secret stripped and authenticate from their
    own store — `validate_bound_profiles` already special-cases them for exactly this reason, so
    probing that role here would test a credential the run never uses."""
    from looplab.agents import factory, preflight
    roles: list = []

    class _Ok:
        def probe(self, _messages, **_kw):
            pass

    def _factory(_settings, **kw):
        roles.append(kw.get("base_url"))
        return _Ok()

    monkeypatch.setattr(factory, "make_llm_client", _factory)
    preflight.preflight_role_endpoints(
        Settings(backend="llm", llm_model="m", llm_base_url="http://a/v1",
                 developer_backend="opencode", developer_base_url="http://dev/v1"))
    assert "http://dev/v1" not in roles


def test_the_run_path_preflights_before_it_builds_a_single_role(monkeypatch, tmp_path):
    """Wiring: the gate is `cli._engine` — the one constructor every CLI run/resume (and the UI,
    which spawns them) funnels through, beside the credential preflight it mirrors. If a role were
    built first, its client would already be issuing the calls this exists to prevent."""
    import looplab.cli as cli
    from looplab.agents import preflight
    built: list = []

    def _refuse(*_a, **_k):
        raise LLMError("LLM endpoint preflight failed: unreachable")

    monkeypatch.setattr(preflight, "preflight_role_endpoints", _refuse)
    monkeypatch.setattr(cli, "make_roles", lambda *a, **k: built.append(1) or (None, None))
    task = tmp_path / "t.json"
    task.write_text(json.dumps({"id": "toy_quadratic", "kind": "quadratic", "goal": "g",
                                "direction": "min",
                                "bounds": {"x": [-1.0, 1.0], "y": [-1.0, 1.0]}}), encoding="utf-8")
    with pytest.raises(LLMError, match="preflight"):
        cli._engine(tmp_path / "run", cli._load_task(task),
                    Settings(backend="llm", max_nodes=1), crash_after=None)
    assert built == []


def test_the_degraded_agentic_proposal_records_why(monkeypatch):
    """Defense in depth for the residual case the preflight cannot cover — an endpoint that dies
    MID-run. The fallback node is then the only record that anything happened, and pre-fix its
    rationale was the bare string "fallback (agent parse failed)": indistinguishable from a weak
    model emitting bad JSON. LLMResearcher's sibling fallback has named its error all along."""
    from looplab.agents.agent import ToolUsingResearcher

    class _Dead:
        def complete_tool(self, *_a, **_kw):
            raise LLMError("Connection error.")

        def complete_text(self, *_a, **_kw):
            raise LLMError("Connection error.")

    r = ToolUsingResearcher(_Dead(), tools=None)
    idea = r._fallback([], LLMError("endpoint 127.0.0.1:9 refused the connection"))
    assert "refused the connection" in idea.rationale
    # The genuinely causeless path (the tool loop ran out of turns) still works with one argument —
    # that is the `drive_tool_loop(fallback=...)` callback contract, which calls `fallback(messages)`.
    assert isinstance(r._fallback([]), type(idea))
