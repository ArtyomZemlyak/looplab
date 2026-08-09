"""Task-aware credential/endpoint reachability for the Repo onboarding Developer."""
from __future__ import annotations

import pytest

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.core.config import Settings
from looplab.core.errors import LLMError


def _task(tmp_path, *, onboard: bool) -> RepoTask:
    (tmp_path / "score.py").write_text("print('{\"metric\": 1}')\n", encoding="utf-8")
    return RepoTask(
        id="onboard-reachability", goal="maximize score", direction="max",
        editable_path=str(tmp_path), onboard=onboard,
        onboard_command=["python", "score.py"] if onboard else [],
        eval=(None if onboard else EvalSpec(
            command=["python", "score.py"],
            metric={"kind": "stdout_json", "key": "metric"})),
    )


def _settings(*, unified: bool, external: bool, stage_profiles: bool = False,
              dedicated_stage_keys: bool = True) -> Settings:
    profiles = {
        "onboard": {
            "model": "onboard-model", "base_url": "https://onboard.invalid/v1",
            "api_key_env": "ONBOARD_KEY",
        },
    }
    roles = {"developer": "onboard"}
    if stage_profiles:
        profiles.update({role: {
            "model": f"{role}-model", "base_url": f"https://{role}.invalid/v1",
            **({"api_key_env": f"{role.upper()}_KEY"} if dedicated_stage_keys else {}),
        } for role in ("implement", "repair")})
        roles.update({"implement": "implement", "repair": "repair"})
    return Settings(
        backend="llm", unified_agent=unified,
        developer_backend="opencode" if external else "default",
        llm_model="shared", llm_base_url="http://localhost:11434/v1",
        llm_profiles=profiles, role_profiles=roles,
    )


@pytest.mark.parametrize("unified", (False, True), ids=("split", "unified"))
@pytest.mark.parametrize("external", (False, True), ids=("in-house", "external"))
def test_active_repo_onboarder_adds_the_exact_developer_consumer(
        tmp_path, unified, external):
    from looplab.agents.reachability import llm_consumer_plan

    plan = llm_consumer_plan(
        _task(tmp_path, onboard=True), _settings(unified=unified, external=external))
    assert plan.onboarder_roles == frozenset({"developer"})
    assert "developer" in plan.roles
    assert "developer" in plan.trusted_in_process_roles


def test_non_onboarding_unified_repo_gets_no_false_developer_requirement(tmp_path, monkeypatch):
    from looplab.agents.reachability import llm_consumer_plan
    from looplab.core.llm import validate_bound_profiles

    for name in ("ONBOARD_KEY", "IMPLEMENT_KEY", "REPAIR_KEY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_BASE_URL", raising=False)
    settings = _settings(
        unified=True, external=True, stage_profiles=True, dedicated_stage_keys=False)
    plan = llm_consumer_plan(_task(tmp_path, onboard=False), settings)
    assert "developer" not in plan.roles
    assert not plan.onboarder_roles
    assert not ({"implement", "repair"} & set(plan.roles))
    # The unset profiles belong only to the isolated external process in this task shape. The scoped
    # child gate must not resurrect the Settings-level superset the parent boundary just removed.
    validate_bound_profiles(
        settings, consumer_roles=plan.roles,
        trusted_in_process_roles=plan.trusted_in_process_roles)


def test_non_onboarding_external_stages_reject_an_explicit_looplab_key(tmp_path, monkeypatch):
    from looplab.agents.reachability import llm_consumer_plan
    from looplab.core.llm import validate_bound_profiles

    for name in ("IMPLEMENT_KEY", "REPAIR_KEY"):
        monkeypatch.setenv(name, "secret-for-test")
        monkeypatch.setenv(f"{name}_BASE_URL", f"https://{name[:-4].lower()}.invalid/v1")
    settings = _settings(unified=True, external=True, stage_profiles=True)
    plan = llm_consumer_plan(_task(tmp_path, onboard=False), settings)
    with pytest.raises(LLMError, match="external developer backend"):
        validate_bound_profiles(
            settings, consumer_roles=plan.roles,
            trusted_in_process_roles=plan.trusted_in_process_roles)


@pytest.mark.parametrize("unified", (False, True), ids=("split", "unified"))
def test_active_onboarder_missing_dedicated_key_fails_child_preflight(
        tmp_path, monkeypatch, unified):
    from looplab.agents.reachability import llm_consumer_plan
    from looplab.core.llm import validate_bound_profiles

    monkeypatch.delenv("ONBOARD_KEY", raising=False)
    monkeypatch.delenv("ONBOARD_KEY_BASE_URL", raising=False)
    settings = _settings(unified=unified, external=True)
    plan = llm_consumer_plan(_task(tmp_path, onboard=True), settings)
    with pytest.raises(LLMError, match="ONBOARD_KEY"):
        validate_bound_profiles(
            settings, consumer_roles=plan.roles,
            trusted_in_process_roles=plan.trusted_in_process_roles)


@pytest.mark.parametrize("unified", (False, True), ids=("split", "unified"))
def test_active_onboarder_target_is_probed_once(tmp_path, monkeypatch, unified):
    from looplab.agents import factory, preflight
    from looplab.agents.reachability import llm_consumer_plan
    from looplab.core.llm import validate_bound_profiles

    monkeypatch.setenv("ONBOARD_KEY", "secret-for-test")
    monkeypatch.setenv("ONBOARD_KEY_BASE_URL", "https://onboard.invalid/v1")
    probed: list[str] = []

    class _Probe:
        def __init__(self, base_url):
            self.base_url = base_url

        def probe(self, _messages, **_kwargs):
            probed.append(self.base_url)

    monkeypatch.setattr(
        factory, "make_llm_client",
        lambda _settings, **kwargs: _Probe(kwargs["base_url"]))
    settings = _settings(unified=unified, external=True)
    plan = llm_consumer_plan(_task(tmp_path, onboard=True), settings)
    validate_bound_profiles(
        settings, consumer_roles=plan.roles,
        trusted_in_process_roles=plan.trusted_in_process_roles)
    preflight.preflight_role_endpoints(settings, consumer_roles=plan.roles)
    assert probed.count("https://onboard.invalid/v1") == 1


def test_repo_make_onboarder_consumes_the_declared_developer_target(
        tmp_path, monkeypatch):
    from looplab.adapters import tasks

    monkeypatch.setenv("ONBOARD_KEY", "secret-for-test")
    monkeypatch.setenv("ONBOARD_KEY_BASE_URL", "https://onboard.invalid/v1")
    built: list[tuple[str, str]] = []

    class _Client:
        def complete_text(self, _messages):
            return "```python\ndef read_metric(workdir):\n    return 1.0\n```"

    def _client(_settings, **kwargs):
        built.append((kwargs["model"], kwargs["base_url"]))
        return _Client()

    monkeypatch.setattr(tasks, "make_llm_client", _client)
    task = _task(tmp_path, onboard=True)
    onboarder = task.make_onboarder(_settings(unified=True, external=True))
    assert onboarder is not None
    assert built == [("onboard-model", "https://onboard.invalid/v1")]
    assert "LOOPLAB_adapter.py" in onboarder()["adapter_files"]


def test_settings_parent_best_effort_exports_onboarder_but_does_not_require_unused_stages(
        monkeypatch):
    from looplab.serve.settings_store import SettingsStore

    for name in ("IMPLEMENT_KEY", "REPAIR_KEY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_BASE_URL", raising=False)
    monkeypatch.setenv("ONBOARD_KEY", "secret-for-test")
    monkeypatch.setenv("ONBOARD_KEY_BASE_URL", "https://onboard.invalid/v1")
    settings = _settings(unified=True, external=True, stage_profiles=True)

    env = SettingsStore._runtime_credential_env(settings)

    assert env["ONBOARD_KEY"] == "secret-for-test"
    assert env["ONBOARD_KEY_BASE_URL"] == "https://onboard.invalid/v1"
    assert "IMPLEMENT_KEY" not in env and "REPAIR_KEY" not in env


def test_settings_parent_does_not_fail_when_every_potential_dedicated_pair_is_missing(monkeypatch):
    from looplab.serve.settings_store import SettingsStore

    for name in ("ONBOARD_KEY", "IMPLEMENT_KEY", "REPAIR_KEY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_BASE_URL", raising=False)
    env = SettingsStore._runtime_credential_env(
        _settings(unified=True, external=True, stage_profiles=True))
    assert "ONBOARD_KEY" not in env
    assert "IMPLEMENT_KEY" not in env and "REPAIR_KEY" not in env
