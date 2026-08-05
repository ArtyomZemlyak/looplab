"""One wrong credential knob must produce ONE diagnosis, and it must name the knob.

Two properties, and they are independent:

1. **Counting.** A run has one credential configuration, so N roles failing to resolve it is N
   symptoms of ONE mistake. `validate_bound_profiles` used to `"; ".join` a role-prefixed copy of the
   identical refusal once per role — seven sentences for one wrong variable, with the `dict.fromkeys`
   dedup defeated by the very prefix that made them look distinct. The operator learned nothing from
   copies two through seven except that something is repeated.

2. **Content.** "LLM credential source contains an incomplete key+endpoint pair" describes the STATE
   the resolver found. The operator cannot edit a state; they can only edit what they typed. The
   refusal now leads with the action (`LOOPLAB_LLM_BASE_URL` overridden without its credential),
   says why the pair is atomic, and names the variables to set together.

What must NOT move is the atomicity itself: a key bound to endpoint A is never sent to endpoint B.
That is a security property, and `test_the_atomicity_itself_still_holds` is what pins it — the
messages above are only its explanation.

The truth-table tests drive the real renderers (CLAUDE.md's "make the rule statable"), and the
end-to-end tests drive `validate_bound_profiles` itself rather than reading its source, so a refactor
of the aggregation keeps them meaningful.
"""
from __future__ import annotations

import pytest

from looplab.core.config import Settings
from looplab.core.errors import LLMCredentialError, LLMError, credential_cause
from looplab.core.llm import (SHARED_BINDING_ENV, SHARED_ENDPOINT_ENV, SHARED_KEY_ENV,
                              _ambient_credential, _ambient_shared_pair, bound_api_key_for,
                              incomplete_pair_refusal, misbound_credential_refusal,
                              render_credential_failures, validate_bound_profiles)

_A = "https://provider-a.example.com/v1"
_B = "https://provider-b.example.com/v1"


@pytest.fixture()
def clean_ambient(monkeypatch):
    """No shared pair from either tier unless a test states one."""
    for name in (SHARED_KEY_ENV, SHARED_BINDING_ENV, SHARED_ENDPOINT_ENV):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------------------------
# 1. the counting rule: one cause, one diagnosis, every affected role named once
# ---------------------------------------------------------------------------------------------

def test_one_wrong_variable_produces_one_diagnosis_naming_every_role_it_breaks(
        clean_ambient, monkeypatch):
    """The headline defect, driven end to end through the real gate.

    Seven roles consume the shared credential in the default configuration. One half-override must
    yield ONE diagnosis with seven roles listed under it — not seven diagnoses.
    """
    monkeypatch.setenv(SHARED_KEY_ENV, "sk-orphaned")     # …and no SHARED_BINDING_ENV anywhere

    settings = Settings(backend="llm", llm_model="m", llm_base_url=_A)
    with pytest.raises(LLMError) as refused:
        validate_bound_profiles(settings)
    message = str(refused.value)

    # The cause is stated ONCE. Counting the action sentence is the whole property.
    assert message.count(f"{SHARED_KEY_ENV} was set without {SHARED_BINDING_ENV}") == 1
    assert message.count("Fix:") == 1
    # …and every role that shares the broken configuration is named, so nothing is hidden by the
    # collapse. These are the roles a default `backend="llm"` run actually builds.
    for role in ("the default target", "propose", "implement", "repair", "pilot", "researcher",
                 "strategy"):
        assert role in message, role
    assert "are not 7 separate problems" in message


def test_the_roles_are_not_lost_when_the_causes_collapse(clean_ambient, monkeypatch):
    """Collapsing must summarize, never discard: each role appears exactly once."""
    monkeypatch.setenv(SHARED_KEY_ENV, "sk-orphaned")
    with pytest.raises(LLMError) as refused:
        validate_bound_profiles(Settings(backend="llm", llm_model="m", llm_base_url=_A))
    message = str(refused.value)
    for role in ("propose", "implement", "repair", "pilot", "researcher"):
        assert message.count(role) == 1, role


def test_two_genuinely_different_causes_stay_two_diagnoses(clean_ambient, monkeypatch):
    """The dedup must not over-collapse. Two wrong knobs are two problems and read as two.

    A profile credential broken for one role, and the shared pair broken for the rest: distinct root
    causes, so the grouping keeps them apart and numbers them.
    """
    monkeypatch.setenv(SHARED_KEY_ENV, "sk-orphaned")           # shared pair: half-overridden
    monkeypatch.delenv("VENDOR_API_KEY", raising=False)         # profile credential: unset
    settings = Settings(
        backend="llm", llm_model="m", llm_base_url=_A,
        role_profiles={"researcher": "vendor"},
        llm_profiles={"vendor": {"model": "m", "base_url": _A,
                                 "api_key_env": "VENDOR_API_KEY"}})
    with pytest.raises(LLMError) as refused:
        validate_bound_profiles(settings)
    message = str(refused.value)
    assert "2 distinct problems." in message
    assert "[1]" in message and "[2]" in message
    assert "VENDOR_API_KEY" in message                 # the profile variable is named
    assert SHARED_BINDING_ENV in message               # so is the shared one


def test_the_renderer_counts_causes_not_roles():
    """`render_credential_failures`' truth table, without a Settings in sight."""
    one = render_credential_failures({"the cause": ["a", "b", "c"]})
    assert one.count("the cause") == 1
    assert "a, b, c" in one and "all 3" in one

    two = render_credential_failures({"first": ["a"], "second": ["b", "c"]})
    assert two.startswith("2 distinct problems.")
    assert "[1] first" in two and "[2] second" in two
    assert "Affects: b, c." in two

    # A cause with no role attached (a run-level refusal) still renders, without an empty role list.
    assert render_credential_failures({"lonely": []}) == "lonely"

    # One role is nothing collapsed, so it must not claim "all 1 of these fail".
    single = render_credential_failures({"the cause": ["embed"]})
    assert single == "the cause\n  Affects: embed."


def test_a_cause_without_a_role_neutral_form_keeps_its_own_group():
    """A plain LLMError from a neighbouring gate must not be merged into an unrelated cause."""
    assert credential_cause(LLMError("plain")) == "plain"
    assert credential_cause(LLMCredentialError("shown", cause_detail="grouped")) == "grouped"
    assert credential_cause(LLMCredentialError("both")) == "both"


# ---------------------------------------------------------------------------------------------
# 2. the content rule: the message names the mistake and the fix
# ---------------------------------------------------------------------------------------------

def test_overriding_only_the_endpoint_is_diagnosed_as_exactly_that(clean_ambient, monkeypatch):
    """The single most natural operator mistake, and the one the old text described worst.

    Nothing about changing one endpoint variable suggests the key has to move too, so the refusal
    has to say it: which knob moved, where the request would now go, where the key is actually
    bound, and which two variables to set together.
    """
    monkeypatch.setenv(SHARED_KEY_ENV, "sk-live")
    monkeypatch.setenv(SHARED_BINDING_ENV, _A)          # a COMPLETE pair, bound to A…
    monkeypatch.setenv(SHARED_ENDPOINT_ENV, _B)         # …and the endpoint moved to B

    settings = Settings(backend="llm", llm_model="m", llm_base_url=_B)
    with pytest.raises(LLMError) as refused:
        validate_bound_profiles(settings)
    message = str(refused.value)

    assert f"{SHARED_ENDPOINT_ENV} was overridden without its credential" in message
    assert _B in message and _A in message              # both endpoints, so the mismatch is visible
    assert f"{SHARED_BINDING_ENV}={_B}" in message      # the exact value to set
    assert "ONE atomic credential" in message           # why it is refused rather than retargeted
    assert "sk-live" not in message                     # never the secret itself


def test_an_endpoint_moved_by_a_setting_rather_than_the_env_var_enumerates_the_spellings(
        clean_ambient, monkeypatch):
    """`-s llm_base_url=…` is not detectable as the knob, so the refusal must not guess one.

    There is no `--base-url` flag; the spellings are the env var, `-s`, and a config file. Naming a
    variable the operator did not set would send them to the wrong file, so this branch lists them.
    """
    monkeypatch.setenv(SHARED_KEY_ENV, "sk-live")
    monkeypatch.setenv(SHARED_BINDING_ENV, _A)
    # No SHARED_ENDPOINT_ENV: the endpoint came from Settings, as `-s llm_base_url=` leaves it.
    settings = Settings(backend="llm", llm_model="m", llm_base_url=_B)
    with pytest.raises(LLMError) as refused:
        validate_bound_profiles(settings)
    message = str(refused.value)
    assert "The endpoint was overridden without its credential" in message
    assert "-s llm_base_url=" in message and SHARED_ENDPOINT_ENV in message
    assert f"{SHARED_BINDING_ENV}={_B}" in message


def test_the_half_left_behind_in_dotenv_is_named_as_dropped(clean_ambient, monkeypatch, tmp_path):
    """The sentence that answers "but it IS in my .env, why did you not use it?".

    Overriding the key in the shell while its binding stays in `.env` selects the process tier WHOLE
    — the `.env` half is dropped, not merged. The operator is staring at a file that plainly contains
    it, so the refusal has to say it was deliberately not used.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{SHARED_KEY_ENV}=sk-from-dotenv\n"
                                   f"{SHARED_BINDING_ENV}={_A}\n", encoding="utf-8")
    monkeypatch.setenv(SHARED_KEY_ENV, "sk-from-shell")   # …binding left in .env only

    resolved = _ambient_credential()
    assert resolved.key == "sk-from-shell" and resolved.binding == ""     # no cross-tier merge
    assert resolved.shadowed == (SHARED_BINDING_ENV,)

    with pytest.raises(LLMError) as refused:
        bound_api_key_for(Settings(llm_base_url=_A), _A)
    assert "was deliberately NOT merged in" in str(refused.value)
    assert "sk-from-dotenv" not in str(refused.value)


def test_the_incomplete_pair_refusal_truth_table():
    """Which half is present decides the sentence; the source decides where to set them."""
    key_only = incomplete_pair_refusal(key_env="K", binding_env="B", have_key=True,
                                       source="the process environment")
    assert key_only.startswith("K was set without B.")
    assert "Fix: set BOTH K and B in the process environment" in key_only
    assert "or unset K in the process environment" in key_only   # the tiered escape hatch

    binding_only = incomplete_pair_refusal(key_env="K", binding_env="B", have_key=False,
                                           source="the .env file")
    assert binding_only.startswith("B was set without K.")

    # A non-tiered (profile) pair has no lower source, so it must not offer the escape hatch.
    profile = incomplete_pair_refusal(key_env="VENDOR_KEY", binding_env="VENDOR_KEY_BASE_URL",
                                      have_key=True, source="the process environment", tiered=False)
    assert "lower source" not in profile
    assert "or unset" not in profile

    # The shadow sentence appears only when the losing source really holds the missing half.
    assert "NOT merged in" in incomplete_pair_refusal(
        key_env="K", binding_env="B", have_key=True, source="x", shadowed=True)
    assert "NOT merged in" not in incomplete_pair_refusal(
        key_env="K", binding_env="B", have_key=True, source="x", shadowed=False)


def test_the_misbound_refusal_truth_table():
    named = misbound_credential_refusal(key_env="K", binding_env="B", target=_B, binding=_A,
                                        source="the .env file", endpoint_knob="THE_KNOB")
    assert named.startswith("THE_KNOB was overridden without its credential.")
    assert f"B={_B}" in named and _A in named
    assert f"If {_B} takes no key, unset both." in named

    anonymous = misbound_credential_refusal(key_env="K", binding_env="B", target=_B, binding=_A,
                                            source="the .env file")
    assert anonymous.startswith("The endpoint was overridden without its credential")
    assert "-s llm_base_url=" in anonymous


# ---------------------------------------------------------------------------------------------
# 3. the other tiers: a profile half-override is diagnosed as clearly as an environment one
# ---------------------------------------------------------------------------------------------

def test_a_profile_credential_missing_its_binding_gets_the_same_diagnosis(
        clean_ambient, monkeypatch):
    """The profile tier's spelling of the same defect — and it used to be the tersest message here.

    "role 'implement' profile credential X is missing its same-source endpoint binding X_BASE_URL"
    states a fact and no action. The profile pair is atomic for the identical reason, so it earns the
    identical explanation, with its own variable names in it.
    """
    monkeypatch.setenv("VENDOR_API_KEY", "sk-vendor")
    monkeypatch.delenv("VENDOR_API_KEY_BASE_URL", raising=False)
    settings = Settings(backend="llm", llm_model="m", llm_base_url=_A, llm_profile="vendor",
                        llm_profiles={"vendor": {"model": "m", "base_url": _A,
                                                 "api_key_env": "VENDOR_API_KEY"}})
    with pytest.raises(LLMError) as refused:
        validate_bound_profiles(settings)
    message = str(refused.value)
    assert "VENDOR_API_KEY was set without VENDOR_API_KEY_BASE_URL" in message
    assert "ONE atomic credential" in message
    assert "Fix: set BOTH VENDOR_API_KEY and VENDOR_API_KEY_BASE_URL" in message
    assert "sk-vendor" not in message
    # …and it is stated once for all the roles that share the profile.
    assert message.count("was set without VENDOR_API_KEY_BASE_URL") == 1


def test_a_profile_credential_bound_elsewhere_names_both_endpoints(clean_ambient, monkeypatch):
    monkeypatch.setenv("VENDOR_API_KEY", "sk-vendor")
    monkeypatch.setenv("VENDOR_API_KEY_BASE_URL", _B)      # bound to B, profile points at A
    settings = Settings(backend="llm", llm_model="m", llm_base_url=_A, llm_profile="vendor",
                        llm_profiles={"vendor": {"model": "m", "base_url": _A,
                                                 "api_key_env": "VENDOR_API_KEY"}})
    with pytest.raises(LLMError) as refused:
        validate_bound_profiles(settings)
    message = str(refused.value)
    assert "profile's `base_url` was overridden without its credential" in message
    assert _A in message and _B in message
    assert "sk-vendor" not in message


def test_a_direct_single_role_caller_still_reads_the_role_in_the_message(clean_ambient, monkeypatch):
    """The role-neutral form is for the AGGREGATOR. A direct build still names the role it failed for.

    Two audiences, which is why `cause_detail` is an attribute rather than a reworded message.
    """
    from looplab.core.llm import make_llm_client_for

    monkeypatch.setenv("VENDOR_API_KEY", "sk-vendor")
    monkeypatch.delenv("VENDOR_API_KEY_BASE_URL", raising=False)
    settings = Settings(backend="llm", llm_model="m", llm_base_url=_A, llm_profile="vendor",
                        llm_profiles={"vendor": {"model": "m", "base_url": _A,
                                                 "api_key_env": "VENDOR_API_KEY"}})
    with pytest.raises(LLMError) as direct:
        make_llm_client_for(settings, role="implement")
    assert "role 'implement'" in str(direct.value)
    assert "VENDOR_API_KEY was set without VENDOR_API_KEY_BASE_URL" in str(direct.value)
    # …while the grouping key drops the role, so seven roles collapse to one cause.
    assert "implement" not in credential_cause(direct.value)


# ---------------------------------------------------------------------------------------------
# 4. the property none of the above may weaken
# ---------------------------------------------------------------------------------------------

def test_the_atomicity_itself_still_holds(clean_ambient, monkeypatch):
    """A key bound to A is never sent to B — the security rule the messages only EXPLAIN.

    Every refusal above exists to describe this rule better. None of them may soften it: a clearer
    message that also started merging halves across sources, or sending an A-key to B, would be a
    strictly worse outcome than the eight-copy wall of text it replaced.
    """
    monkeypatch.setenv(SHARED_KEY_ENV, "sk-bound-to-a")
    monkeypatch.setenv(SHARED_BINDING_ENV, _A)

    # …to its own endpoint: resolved, and the key really is returned.
    assert bound_api_key_for(Settings(llm_base_url=_A), _A) == "sk-bound-to-a"
    # …to any other endpoint: refused, before any transport is constructed.
    with pytest.raises(LLMError):
        bound_api_key_for(Settings(llm_base_url=_B), _B)

    # A half-override does not merge with the other half from the lower tier — it replaces the pair.
    monkeypatch.delenv(SHARED_BINDING_ENV)
    with pytest.raises(LLMError):
        bound_api_key_for(Settings(llm_base_url=_A), _A)


def test_no_refusal_on_this_path_can_echo_the_secret(clean_ambient, monkeypatch):
    """Every message here is printed to a terminal and into CI logs."""
    secret = "sk-must-never-be-printed"
    monkeypatch.setenv(SHARED_KEY_ENV, secret)
    for binding in (None, _A):
        if binding is None:
            monkeypatch.delenv(SHARED_BINDING_ENV, raising=False)
        else:
            monkeypatch.setenv(SHARED_BINDING_ENV, binding)
        with pytest.raises(LLMError) as refused:
            validate_bound_profiles(Settings(backend="llm", llm_model="m", llm_base_url=_B))
        assert secret not in str(refused.value)


def test_a_complete_matching_override_is_still_accepted(clean_ambient, monkeypatch):
    """The fix the message prescribes must actually work — otherwise it is worse than no advice."""
    monkeypatch.setenv(SHARED_ENDPOINT_ENV, _B)
    monkeypatch.setenv(SHARED_KEY_ENV, "sk-for-b")
    monkeypatch.setenv(SHARED_BINDING_ENV, _B)
    settings = Settings(backend="llm", llm_model="m", llm_base_url=_B)
    validate_bound_profiles(settings)                      # no refusal
    assert bound_api_key_for(settings, _B) == "sk-for-b"


def test_the_two_tuple_view_of_the_ambient_pair_is_the_same_resolution(clean_ambient, monkeypatch):
    """`_ambient_shared_pair` is an adapter over `_ambient_credential`, never a second tier rule."""
    monkeypatch.setenv(SHARED_KEY_ENV, "k")
    monkeypatch.setenv(SHARED_BINDING_ENV, _A)
    resolved = _ambient_credential()
    assert _ambient_shared_pair() == (resolved.key, resolved.binding) == ("k", _A)
