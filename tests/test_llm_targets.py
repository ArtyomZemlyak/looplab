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

from _source_scan import iter_sources

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
    shared = LlmTarget("shared", "http://shared/v1", 0.6, None)
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
    URL could not express this at all. LlmTarget IS the client cache key, so these must not compare
    equal — collapsing them would hand one role the other's credential."""
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
    # A profile credential must declare the endpoint it is bound to, from the SAME source, so the
    # key can never travel to a host it was not issued for. Setting only the key is now a loud
    # LLMError before any client is built.
    monkeypatch.setenv("LOOPLAB_LLM_API_KEY_CODER_BASE_URL", "https://provider.tld/v1")
    s = Settings(llm_profiles=_PROFILES, role_profiles={"implement": "coder"})
    make_llm_client_for(s, role="implement")
    assert seen[0]["api_key"] == "sk-coder"
    assert seen[0]["api_key_base_url"] == "https://provider.tld/v1"


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


# --------------------------------------------------------------------------- the endpoint owns the key
def test_a_credential_never_follows_an_endpoint_the_profile_did_not_name():
    """SECURITY. Resolving the key independently of the endpoint meant a `<role>_base_url` or a stage
    map could redirect the request while keeping the profile's key — putting a live secret in an
    Authorization header to a host it was never issued for."""
    s = Settings(llm_base_url="https://shared.tld/v1", developer_base_url="https://internal.local/v1",
                 llm_profiles={"paid": {"base_url": "https://paid.tld/v1", "model": "big",
                                        "api_key_env": "PAID_API_KEY"}},
                 llm_profile="paid")
    redirected = resolve_llm_target(s, role="developer")
    assert redirected.base_url == "https://internal.local/v1"
    assert redirected.api_key_env is None                  # was PAID_API_KEY
    # A role left on the profile's own endpoint still gets its key.
    assert resolve_llm_target(s, role="researcher").api_key_env == "PAID_API_KEY"


def test_a_profile_without_an_endpoint_keeps_its_key_on_the_shared_one():
    """The two-roles-one-provider-two-keys case: the profile names no endpoint, so the shared one IS
    the endpoint it stands for and the key must still apply."""
    s = Settings(llm_base_url="https://shared.tld/v1",
                 llm_profiles={"team_a": {"api_key_env": "TEAM_A_API_KEY"}},
                 role_profiles={"researcher": "team_a"})
    t = resolve_llm_target(s, role="researcher")
    assert t.base_url == "https://shared.tld/v1" and t.api_key_env == "TEAM_A_API_KEY"


def test_the_preflight_demands_exactly_the_keys_the_clients_will_ask_for(monkeypatch):
    """Reading `role_profiles` raw made it abort a run over a credential no client resolves to."""
    monkeypatch.delenv("PAID_API_KEY", raising=False)
    s = Settings(backend="llm", llm_base_url="https://shared.tld/v1",
                 developer_base_url="https://internal.local/v1",
                 llm_profiles={"paid": {"base_url": "https://paid.tld/v1",
                                        "api_key_env": "PAID_API_KEY"}},
                 role_profiles={"developer": "paid"})
    validate_bound_profiles(s)                             # the key is never used -> not demanded


# --------------------------------------------------------------------------- the registry
def _role_literals() -> set[str]:
    return set(re.findall(r'role=["\']([a-z_]+)["\']',
                          "\n".join(source for _p, source in iter_sources(ROOT / "looplab"))))


def test_every_role_key_has_a_reader():
    """Two-way source scan, the discipline this project applies to its other duck-typed seams. It
    has NO exemptions: the first version excused the five stage names as "read through `stage=`",
    which was not true of any line in the tree — so the registry certified as wired exactly the names
    that did nothing, and the bug shipped green."""
    unread = {k for k in LLM_ROLE_KEYS if k not in _role_literals()}
    assert not unread, f"role keys nothing resolves: {sorted(unread)}"


def test_no_reader_resolves_a_role_the_registry_does_not_know():
    """The reverse half. It must compare against the SOURCE, not against a hand-copied duplicate of
    the registry — the first version filtered candidates through a literal set identical to
    LLM_ROLE_KEYS, so the subtraction was empty by construction and a new unregistered reader could
    never trip it."""
    known_settings_roles = {"researcher", "developer", "strategist", "compressor", "embed",
                            "propose", "implement", "repair", "strategy", "pilot"}
    assert known_settings_roles == set(LLM_ROLE_KEYS)      # the doc'd list and the registry agree
    # Any `role=` literal that names a Settings-ish role must be registered. Unrelated `role=` kwargs
    # elsewhere in the tree (tool providers, ARIA attributes) are excluded by the field-name test.
    for name in _role_literals():
        if any(hasattr(Settings, f"{name}_model") for _ in (0,)) and name not in LLM_ROLE_KEYS:
            raise AssertionError(f"{name!r} is resolved as a role but is not in LLM_ROLE_KEYS")


def test_every_role_field_name_exists_on_settings():
    """`_ROLE_FIELDS` is read with a DEFAULTED getattr, so a renamed Settings field would not raise —
    the role would just quietly fall back to the shared values."""
    from looplab.core.llm import _ROLE_FIELDS
    for role, fields in _ROLE_FIELDS.items():
        for field in fields:
            if field is not None:
                assert field in Settings.model_fields, f"{role} -> {field}"


def test_every_profile_field_is_read_by_the_resolver():
    """A validated field with no reader is the same silent no-op the closed allow-list exists to
    prevent — `provider` was accepted, documented and snapshotted while nothing ever looked at it."""
    from looplab.core.config import _PROFILE_FIELDS
    profile = {"model": "m", "base_url": "http://b/v1", "temperature": 0.25,
               "api_key_env": "X_API_KEY"}
    assert set(profile) == set(_PROFILE_FIELDS)
    s = Settings(llm_profiles={"p": profile}, role_profiles={"researcher": "p"})
    t = resolve_llm_target(s, role="researcher")
    assert (t.model, t.base_url, t.temperature, t.api_key_env) == (
        "m", "http://b/v1", 0.25, "X_API_KEY")
