"""Connection profiles: one resolver decides which model, endpoint, temperature and KEY a role uses.

Before profiles existed, `llm_api_key` was the project's only credential field, so "a model per role"
could never mean "a provider per role" — and several roles (the Strategist, the history compressor,
the embedder) had no way to be pointed anywhere at all. These tests pin the resolution order, the
single-model operator's untouched path, and the registry that stops a role name from silently
becoming a no-op.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from looplab.core.config import Settings
from looplab.core.llm import (LLM_ROLE_KEYS, LlmTarget, make_llm_client_for, resolve_llm_target,
                              validate_bound_profiles)

ROOT = Path(__file__).resolve().parents[1]
_PROFILES = {
    "local": {"base_url": "http://localhost:11434/v1", "model": "small"},
    "coder": {"base_url": "https://provider.tld/v1", "model": "big-coder",
              "api_key_env": "LOOPLAB_LLM_API_KEY_CODER", "temperature": 0.1},
}


# --------------------------------------------------------------------------- the untouched default
def test_bare_settings_resolve_every_role_to_the_shared_values():
    """The one-model operator must not be able to tell that profiles exist."""
    s = Settings(llm_model="shared", llm_base_url="http://shared/v1", llm_temperature=0.6)
    shared = LlmTarget("shared", "http://shared/v1", 0.6, None, None)
    assert resolve_llm_target(s) == shared
    for role in LLM_ROLE_KEYS:
        assert resolve_llm_target(s, role=role) == shared, role


def test_no_profile_means_no_api_key_argument_at_all(monkeypatch):
    """`make_llm_client_for` must call the factory with the SAME arguments the historical
    `make_llm_client(settings)` did — otherwise every monkeypatch of that seam stops intercepting."""
    from looplab.core import llm as llm_mod
    seen = []
    monkeypatch.setattr(llm_mod, "make_llm_client",
                        lambda settings, **kw: seen.append(kw) or object())
    s = Settings(llm_model="shared", llm_base_url="http://shared/v1", llm_temperature=0.6)
    make_llm_client_for(s, role="researcher")
    assert "api_key" not in seen[0]
    assert seen[0] == {"model": "shared", "base_url": "http://shared/v1",
                       "temperature": 0.6, "timeout": None}


# --------------------------------------------------------------------------- precedence
def test_each_property_resolves_independently():
    """A profile that changes only the endpoint keeps the role's model; a role field that changes
    only the model keeps the profile's key. Resolving the four together as one unit is what makes a
    partial override silently drag the rest along."""
    s = Settings(llm_model="shared", llm_base_url="http://shared/v1", llm_temperature=0.6,
                 llm_profiles=_PROFILES, role_profiles={"implement": "coder", "propose": "local"},
                 developer_temperature=0.05)
    impl = resolve_llm_target(s, role="implement")
    assert impl.model == "big-coder" and impl.base_url == "https://provider.tld/v1"
    assert impl.temperature == 0.05                    # the ROLE field beats the profile's 0.1
    assert impl.api_key_env == "LOOPLAB_LLM_API_KEY_CODER"
    prop = resolve_llm_target(s, role="propose")
    assert prop.model == "small" and prop.temperature == 0.6   # profile has none -> shared
    assert prop.api_key_env is None                            # a profile without a key uses the shared one


def test_the_stage_map_beats_the_role_field_which_beats_the_profile():
    s = Settings(llm_model="shared", llm_base_url="http://shared/v1",
                 llm_profiles=_PROFILES, role_profiles={"implement": "coder"},
                 developer_model="role-model",
                 agent_stage_models={"implement": "stage-model"})
    assert resolve_llm_target(s, role="implement").model == "stage-model"
    s2 = s.model_copy(update={"agent_stage_models": {}})
    assert resolve_llm_target(s2, role="implement").model == "role-model"
    s3 = s2.model_copy(update={"developer_model": None})
    assert resolve_llm_target(s3, role="implement").model == "big-coder"
    s4 = s3.model_copy(update={"role_profiles": {}})
    assert resolve_llm_target(s4, role="implement").model == "shared"


def test_llm_profile_moves_every_unbound_role_at_once():
    s = Settings(llm_model="shared", llm_base_url="http://shared/v1",
                 llm_profiles=_PROFILES, llm_profile="local",
                 role_profiles={"implement": "coder"})
    assert resolve_llm_target(s, role="propose").model == "small"       # follows the default
    assert resolve_llm_target(s, role="implement").model == "big-coder"  # its own binding wins


def test_two_roles_on_one_endpoint_with_different_keys_are_different_targets():
    """The reason the credential belongs to the PROFILE and not to the endpoint: a key map keyed by
    URL could not express this at all. LlmTarget doubles as the client cache key, so these must not
    compare equal — collapsing them would hand one role the other's credential."""
    profiles = {
        "a": {"base_url": "https://p/v1", "model": "m", "api_key_env": "TEAM_A_API_KEY"},
        "b": {"base_url": "https://p/v1", "model": "m", "api_key_env": "TEAM_B_API_KEY"},
    }
    s = Settings(llm_profiles=profiles, role_profiles={"propose": "a", "implement": "b"})
    a = resolve_llm_target(s, role="propose")
    b = resolve_llm_target(s, role="implement")
    assert a.model == b.model and a.base_url == b.base_url
    assert a != b and len({a, b}) == 2


# --------------------------------------------------------------------------- credentials
def test_a_bound_profile_supplies_its_own_key(monkeypatch):
    from looplab.core import llm as llm_mod
    seen = []
    monkeypatch.setattr(llm_mod, "make_llm_client",
                        lambda settings, **kw: seen.append(kw) or object())
    monkeypatch.setenv("LOOPLAB_LLM_API_KEY_CODER", "sk-coder")
    s = Settings(llm_profiles=_PROFILES, role_profiles={"implement": "coder"})
    make_llm_client_for(s, role="implement")
    assert seen[0]["api_key"] == "sk-coder"


def test_a_missing_credential_fails_loudly_and_never_echoes_a_value(monkeypatch):
    monkeypatch.delenv("LOOPLAB_LLM_API_KEY_CODER", raising=False)
    s = Settings(llm_profiles=_PROFILES, role_profiles={"implement": "coder"})
    with pytest.raises(Exception) as exc:
        make_llm_client_for(s, role="implement")
    assert "LOOPLAB_LLM_API_KEY_CODER" in str(exc.value)

    monkeypatch.setenv("LOOPLAB_LLM_API_KEY_CODER", "sk-should-never-be-printed")
    s2 = Settings(llm_profiles={"x": {"api_key_env": "LOOPLAB_OTHER_API_KEY"}},
                  role_profiles={"implement": "x"})
    monkeypatch.delenv("LOOPLAB_OTHER_API_KEY", raising=False)
    with pytest.raises(Exception) as exc2:
        make_llm_client_for(s2, role="implement")
    assert "sk-should-never-be-printed" not in str(exc2.value)


def test_a_bound_profiles_missing_key_stops_the_run_before_its_first_paid_call(monkeypatch):
    monkeypatch.delenv("LOOPLAB_LLM_API_KEY_CODER", raising=False)
    s = Settings(backend="llm", llm_profiles=_PROFILES, role_profiles={"implement": "coder"})
    with pytest.raises(Exception) as exc:
        validate_bound_profiles(s)
    assert "LOOPLAB_LLM_API_KEY_CODER" in str(exc.value) and "implement" in str(exc.value)

    # An UNBOUND profile is not this run's problem — one machine-wide map may describe providers it
    # never touches — and the offline backend builds no clients at all.
    validate_bound_profiles(Settings(backend="llm", llm_profiles=_PROFILES))
    validate_bound_profiles(s.model_copy(update={"backend": "toy"}))
    validate_bound_profiles(Settings(backend="llm"))            # no profiles -> nothing to check


# --------------------------------------------------------------------------- validation
@pytest.mark.parametrize("kwargs,needle", [
    ({"llm_profiles": {"a": {"api_key": "sk-live"}}}, "api_key"),
    ({"llm_profiles": {"a": {"token": "t"}}}, "token"),
    ({"llm_profiles": {"a": {"api_key_env": "MY_AUTH"}}}, "MY_AUTH"),
    ({"llm_profiles": {"a": {"api_key_env": "lower_api_key"}}}, "lower_api_key"),
    ({"llm_profiles": {"a": {"modle": "typo"}}}, "unknown field"),
    ({"llm_profiles": {"a": {}}, "role_profiles": {"resercher": "a"}}, "unknown role"),
    ({"llm_profiles": {"a": {}}, "role_profiles": {"implement": "b"}}, "not in llm_profiles"),
    ({"llm_profiles": {"a": {}}, "llm_profile": "b"}, "not in llm_profiles"),
])
def test_a_profile_typo_is_an_error_not_a_silent_no_op(kwargs, needle):
    """Assignment is not validated anywhere in this project, so a typo here would otherwise be a
    setting the operator believes is on: an unknown role name is simply never read, and a dangling
    profile name falls back to the shared model."""
    with pytest.raises(Exception) as exc:
        Settings(**kwargs)
    assert needle in str(exc.value)


def test_a_profile_carries_no_secret_into_the_snapshot():
    """The map is written verbatim into config.snapshot.json and served over HTTP. That is only safe
    because a profile holds a variable NAME (ADR-11 as a data type), which is why a literal key is
    refused above — there is nothing here for masking to catch."""
    s = Settings(llm_profiles=_PROFILES, role_profiles={"implement": "coder"})
    snap = s.masked_snapshot()
    assert snap["llm_profiles"] == _PROFILES
    assert "LOOPLAB_LLM_API_KEY_CODER" in str(snap["llm_profiles"])   # the NAME travels
    assert snap["llm_api_key"] is None


def test_an_old_snapshot_without_the_new_fields_still_loads():
    from looplab.core.config import settings_from_snapshot
    snap = Settings(llm_model="old").masked_snapshot()
    for field in ("llm_profiles", "llm_profile", "role_profiles",
                  "strategist_model", "strategist_base_url"):
        snap.pop(field, None)
    restored = settings_from_snapshot(snap)
    assert restored.llm_profiles == {} and restored.role_profiles == {}
    assert restored.llm_profile is None and restored.strategist_model is None


# --------------------------------------------------------------------------- the registry
def test_every_role_key_has_a_reader_and_every_reader_is_registered():
    """Two-way source scan, the discipline this project already applies to its other duck-typed
    seams: a name in the registry with nothing reading it is a setting that silently does nothing,
    and a `role=` string nobody registered is rejected at config time for no reason."""
    sources = [p.read_text(encoding="utf-8") for p in (ROOT / "looplab").rglob("*.py")]
    blob = "\n".join(sources)
    used = set(re.findall(r'role=["\']([a-z_]+)["\']', blob))
    unread = {k for k in LLM_ROLE_KEYS if k not in used}
    # The five unified-agent STAGE names are read through `stage=`/the stage maps, not `role=`.
    assert unread <= {"propose", "implement", "repair", "strategy", "pilot"}, unread
    unregistered = {r for r in used if r in {"researcher", "developer", "strategist", "compressor",
                                             "embed", "propose", "implement", "repair", "strategy",
                                             "pilot"}} - set(LLM_ROLE_KEYS)
    assert not unregistered, unregistered
