"""Task-aware startup contract for external Developer validation fallbacks."""
from __future__ import annotations

from itertools import product
from pathlib import Path

import pytest

from looplab.core.config import Settings
from looplab.core.errors import LLMError


class _TaskShape:
    def __init__(self, uses_llm: bool):
        self.uses_llm = uses_llm

    def external_fallback_uses_llm(self) -> bool:
        return self.uses_llm


def _stage_roles(unified: bool) -> set[str]:
    return {"implement", "repair"} if unified else {"developer"}


def _settings(*, unified: bool, validate: bool, dedicated_key: bool = True) -> Settings:
    roles = {role: "coder" for role in _stage_roles(unified)}
    profile = {"model": "coder", "base_url": "https://coder.invalid/v1"}
    if dedicated_key:
        profile["api_key_env"] = "CODER_KEY"
    return Settings(
        backend="llm", llm_model="shared", llm_base_url="https://shared.invalid/v1",
        developer_backend="opencode", unified_agent=unified, validate_agent=validate,
        llm_profiles={"coder": profile},
        role_profiles=roles,
    )


_MATRIX = [
    pytest.param(unified, validate, script, id=("unified" if unified else "split") + "-"
                 + ("validate" if validate else "raw") + "-"
                 + ("script" if script else "repo"))
    for unified, validate, script in product((False, True), repeat=3)
]


@pytest.mark.parametrize("unified,validate,script", _MATRIX)
def test_external_fallback_consumer_matrix(unified, validate, script):
    from looplab.agents.reachability import llm_consumer_plan

    plan = llm_consumer_plan(_TaskShape(script), _settings(unified=unified, validate=validate))
    expected_fallback = _stage_roles(unified) if (validate and script) else set()
    assert set(plan.roles) & _stage_roles(unified) == expected_fallback
    assert set(plan.external_fallback_roles) == expected_fallback


@pytest.mark.parametrize("unified,validate,script", _MATRIX)
def test_dedicated_credentials_follow_the_same_matrix(
        monkeypatch, unified, validate, script):
    from looplab.agents.reachability import llm_consumer_plan
    from looplab.core.llm import validate_bound_profiles

    monkeypatch.setenv("CODER_KEY", "secret-for-test")
    monkeypatch.setenv("CODER_KEY_BASE_URL", "https://coder.invalid/v1")
    settings = _settings(unified=unified, validate=validate)
    plan = llm_consumer_plan(_TaskShape(script), settings)
    # A trusted in-process fallback may use the dedicated key. An external-only stage is not a
    # required consumer, but explicitly promising it a LoopLab-managed key is still contradictory:
    # the scrubbed nested CLI can never receive that key.
    if validate and script:
        validate_bound_profiles(
            settings, consumer_roles=plan.roles,
            trusted_in_process_roles=plan.trusted_in_process_roles)
    else:
        with pytest.raises(LLMError, match="external developer backend"):
            validate_bound_profiles(
                settings, consumer_roles=plan.roles,
                trusted_in_process_roles=plan.trusted_in_process_roles)


@pytest.mark.parametrize("unified", (False, True), ids=("split", "unified"))
def test_reachable_fallback_still_refuses_an_unset_dedicated_key(monkeypatch, unified):
    from looplab.agents.reachability import llm_consumer_plan
    from looplab.core.llm import validate_bound_profiles

    monkeypatch.delenv("CODER_KEY", raising=False)
    monkeypatch.delenv("CODER_KEY_BASE_URL", raising=False)
    settings = _settings(unified=unified, validate=True)
    plan = llm_consumer_plan(_TaskShape(True), settings)
    with pytest.raises(LLMError, match="CODER_KEY"):
        validate_bound_profiles(
            settings, consumer_roles=plan.roles,
            trusted_in_process_roles=plan.trusted_in_process_roles)


def test_split_external_only_start_does_not_require_or_probe_the_developer_target(monkeypatch):
    from looplab.agents import factory, preflight
    from looplab.agents.reachability import llm_consumer_plan
    from looplab.core.llm import validate_bound_profiles

    monkeypatch.delenv("CODER_KEY", raising=False)
    monkeypatch.delenv("CODER_KEY_BASE_URL", raising=False)
    settings = _settings(unified=False, validate=False, dedicated_key=False)
    plan = llm_consumer_plan(_TaskShape(False), settings)
    assert "developer" not in plan.roles
    validate_bound_profiles(
        settings, consumer_roles=plan.roles,
        trusted_in_process_roles=plan.trusted_in_process_roles)

    probed: list[str] = []

    class _Probe:
        def __init__(self, base_url):
            self.base_url = base_url

        def probe(self, _messages, **_kwargs):
            probed.append(self.base_url)

    monkeypatch.setattr(
        factory, "make_llm_client",
        lambda _settings, **kwargs: _Probe(kwargs["base_url"]))
    preflight.preflight_role_endpoints(settings, consumer_roles=plan.roles)
    assert "https://coder.invalid/v1" not in probed


@pytest.mark.parametrize("unified,validate,script", _MATRIX)
def test_endpoint_probe_follows_the_same_matrix(monkeypatch, unified, validate, script):
    from looplab.agents import factory, preflight
    from looplab.agents.reachability import llm_consumer_plan

    monkeypatch.setenv("CODER_KEY", "secret-for-test")
    monkeypatch.setenv("CODER_KEY_BASE_URL", "https://coder.invalid/v1")
    probed: list[str] = []

    class _Probe:
        def __init__(self, base_url: str):
            self.base_url = base_url

        def probe(self, _messages, **_kwargs):
            probed.append(self.base_url)

    def _client(_settings, **kwargs):
        return _Probe(kwargs["base_url"])

    monkeypatch.setattr(factory, "make_llm_client", _client)
    settings = _settings(unified=unified, validate=validate)
    plan = llm_consumer_plan(_TaskShape(script), settings)
    preflight.preflight_role_endpoints(settings, consumer_roles=plan.roles)
    expected = 1 if (validate and script) else 0
    # Implement + repair share one full LlmTarget here, so unified mode must still probe it once.
    assert probed.count("https://coder.invalid/v1") == expected


def test_explicit_llm_swap_validates_before_role_construction(monkeypatch):
    from looplab.agents import factory

    monkeypatch.delenv("CODER_KEY", raising=False)
    monkeypatch.delenv("CODER_KEY_BASE_URL", raising=False)
    built: list[str] = []
    monkeypatch.setattr(
        factory, "make_roles",
        lambda _task, _settings: built.append("built"))

    replacement = factory.make_developer_factory(
        _TaskShape(False), _settings(unified=False, validate=False))
    with pytest.raises(LLMError, match="CODER_KEY"):
        replacement("llm")
    assert not built


def test_explicit_llm_swap_probes_new_target_then_builds_when_valid(monkeypatch):
    from looplab.agents import factory, preflight
    from looplab.core.llm import resolve_llm_target

    monkeypatch.setenv("CODER_KEY", "secret-for-test")
    monkeypatch.setenv("CODER_KEY_BASE_URL", "https://coder.invalid/v1")
    events: list[tuple] = []
    expected_developer = object()

    def _probe(settings, *, timeout_s, consumer_roles):
        events.append((
            "probe", frozenset(consumer_roles),
            resolve_llm_target(settings, role="developer").base_url,
            timeout_s,
        ))

    def _build(_task, settings):
        events.append(("build", settings.developer_backend))
        return object(), expected_developer

    monkeypatch.setattr(preflight, "preflight_role_endpoints", _probe)
    monkeypatch.setattr(factory, "make_roles", _build)
    replacement = factory.make_developer_factory(
        _TaskShape(False), _settings(unified=False, validate=False))

    assert replacement("llm") is expected_developer
    assert events == [
        ("probe", frozenset({"developer"}), "https://coder.invalid/v1",
         preflight.PREFLIGHT_TIMEOUT_S),
        ("build", "default"),
    ]


def test_custom_role_with_writable_none_client_still_receives_dedicated_rebind(monkeypatch):
    from types import SimpleNamespace

    from looplab.agents import factory

    monkeypatch.setenv("CODER_KEY", "secret-for-test")
    monkeypatch.setenv("CODER_KEY_BASE_URL", "https://coder.invalid/v1")
    built: list[str] = []

    class _BoundClient:
        def __init__(self, base_url):
            self.base_url = base_url

    class _CustomDeveloper:
        def __init__(self):
            self.client = None

    class _CustomTask:
        def llm_roles(self, client, **_kwargs):
            self.developer = _CustomDeveloper()
            return SimpleNamespace(client=client), self.developer

    def _client(_settings, **kwargs):
        built.append(kwargs["base_url"])
        return _BoundClient(kwargs["base_url"])

    monkeypatch.setattr(factory, "make_llm_client", _client)
    settings = _settings(unified=False, validate=False).model_copy(update={
        "developer_backend": "default", "researcher_tools": False,
        "knowledge_dir": None, "memory_dir": None, "auto_install_deps": False,
    })
    task = _CustomTask()
    _researcher, developer = factory.make_roles(task, settings)

    assert developer is task.developer
    assert developer.client.base_url == "https://coder.invalid/v1"
    assert built.count("https://coder.invalid/v1") == 1


def test_legacy_repo_like_task_does_not_gain_a_fallback_requirement_in_unified_mode():
    from looplab.agents.reachability import llm_consumer_plan

    class _LegacyRepo:
        def repo_spec(self):
            return {"editables": []}

    settings = _settings(unified=True, validate=True)
    plan = llm_consumer_plan(_LegacyRepo(), settings)
    assert "developer" not in plan.roles
    assert not plan.external_fallback_roles


def test_server_authorizes_a_potential_fallback_key_for_the_task_aware_child(monkeypatch):
    from looplab.serve.settings_store import SettingsStore

    monkeypatch.setenv("CODER_KEY", "secret-for-test")
    monkeypatch.setenv("CODER_KEY_BASE_URL", "https://coder.invalid/v1")
    env = SettingsStore._runtime_credential_env(_settings(unified=True, validate=True))
    assert env["CODER_KEY"] == "secret-for-test"
    assert env["CODER_KEY_BASE_URL"] == "https://coder.invalid/v1"


class _Client:
    def __init__(self, **kwargs):
        self.model = kwargs.get("model")
        self.base_url = kwargs.get("base_url")
        self.temperature = kwargs.get("temperature")

    def complete_text(self, _messages):
        return "```python\nprint('{\"metric\": 1.0}')\n```"

    def complete_tool(self, _messages, _schema):
        return {}


@pytest.mark.parametrize("unified", (False, True), ids=("split", "unified"))
@pytest.mark.parametrize("validate,expected", ((False, 0), (True, 1)),
                         ids=("raw", "validated-fallback"))
def test_factory_builds_a_stage_client_only_for_the_script_fallback(
        monkeypatch, unified, validate, expected):
    from looplab.adapters.tasks import load_task, make_roles
    from looplab.agents import factory

    monkeypatch.setenv("CODER_KEY", "secret-for-test")
    monkeypatch.setenv("CODER_KEY_BASE_URL", "https://coder.invalid/v1")
    built: list[str] = []

    def _client(_settings, **kwargs):
        built.append(kwargs["base_url"])
        return _Client(**kwargs)

    monkeypatch.setattr(factory, "make_llm_client", _client)
    task = load_task(Path(__file__).resolve().parents[1] / "examples" / "code_regression_task.json")
    make_roles(task, _settings(unified=unified, validate=validate))
    assert built.count("https://coder.invalid/v1") == expected


def test_repo_noop_fallback_never_builds_the_dedicated_client(monkeypatch, tmp_path):
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    from looplab.adapters.tasks import make_roles
    from looplab.agents import factory

    (tmp_path / "solution.py").write_text("print('{\"metric\": 0}')\n", encoding="utf-8")
    task = RepoTask(
        goal="keep the baseline", editable_path=str(tmp_path), edit_surface=["*.py"],
        eval=EvalSpec(command=["python", "solution.py"],
                      metric={"kind": "stdout_json", "key": "metric"}),
    )
    monkeypatch.setenv("CODER_KEY", "secret-for-test")
    monkeypatch.setenv("CODER_KEY_BASE_URL", "https://coder.invalid/v1")
    built: list[str] = []

    def _client(_settings, **kwargs):
        built.append(kwargs["base_url"])
        return _Client(**kwargs)

    monkeypatch.setattr(factory, "make_llm_client", _client)
    make_roles(task, _settings(unified=True, validate=True))
    assert "https://coder.invalid/v1" not in built


def test_unified_invalid_agent_activates_the_exact_implement_and_repair_fallbacks(monkeypatch):
    from looplab.adapters.tasks import load_task, make_roles
    from looplab.agents import factory
    from looplab.core.models import Idea

    for name, url in (("IMPLEMENT_KEY", "https://implement.invalid/v1"),
                      ("REPAIR_KEY", "https://repair.invalid/v1")):
        monkeypatch.setenv(name, "secret-for-test")
        monkeypatch.setenv(f"{name}_BASE_URL", url)
    called: list[str] = []

    class _TrackingClient(_Client):
        def complete_text(self, messages):
            called.append(self.base_url)
            return super().complete_text(messages)

    monkeypatch.setattr(factory, "make_llm_client",
                        lambda _settings, **kwargs: _TrackingClient(**kwargs))
    settings = Settings(
        backend="llm", llm_model="shared", llm_base_url="https://shared.invalid/v1",
        developer_backend="opencode", unified_agent=True, validate_agent=True,
        agent_max_retries=0,
        llm_profiles={
            "implement": {"model": "coder", "base_url": "https://implement.invalid/v1",
                          "api_key_env": "IMPLEMENT_KEY"},
            "repair": {"model": "fixer", "base_url": "https://repair.invalid/v1",
                       "api_key_env": "REPAIR_KEY"},
        },
        role_profiles={"implement": "implement", "repair": "repair"},
    )
    task = load_task(Path(__file__).resolve().parents[1] / "examples" / "code_regression_task.json")
    agent, _same = make_roles(task, settings)
    agent.developer.inner.implement = lambda _idea: ""       # validator rejects: force fallback
    agent.repair_developer.inner.repair = lambda _idea, _code, _error: ""

    agent.developer.implement(Idea(operator="draft", rationale="implement"))
    agent.repair_developer.repair(
        Idea(operator="debug", rationale="repair"), "broken", "traceback")
    assert called == ["https://implement.invalid/v1", "https://repair.invalid/v1"]
