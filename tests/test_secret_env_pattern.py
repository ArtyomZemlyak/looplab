"""The two "does this variable name hold a secret?" patterns must agree.

`runtime/sandbox.py::SECRET_ENV` is the filter that strips the operator's keys out of the environment
handed to generated candidate code. `core/config.py::_SECRET_ENV_NAME` is what a connection profile's
`api_key_env` must match. They are duplicated rather than shared because layering forbids `core` from
importing `runtime` — so a name accepted by the config validator but NOT recognized by the sandbox
would be a credential the sandbox happily hands to untrusted code. This test is the joint.
"""
from __future__ import annotations

import pytest

from looplab.core.config import Settings, _SECRET_ENV_NAME
from looplab.runtime.sandbox import SECRET_ENV

_ACCEPTED = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LOOPLAB_LLM_API_KEY", "LOOPLAB_LLM_API_KEY_CODER",
    "HF_TOKEN", "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD", "MY_CREDENTIAL", "SOME_PASSWD",
]
_REJECTED = ["MY_AUTH", "PATH", "HOME", "LOOPLAB_LLM_BASE_URL", "CUDA_VISIBLE_DEVICES"]


@pytest.mark.parametrize("name", _ACCEPTED)
def test_an_accepted_variable_name_is_one_the_sandbox_also_strips(name):
    assert _SECRET_ENV_NAME.match(name), name
    assert SECRET_ENV.search(name), f"{name} passes config but the sandbox would NOT strip it"


@pytest.mark.parametrize("name", _REJECTED)
def test_a_name_that_does_not_look_like_a_secret_is_refused_by_both(name):
    assert not _SECRET_ENV_NAME.match(name), name
    assert not SECRET_ENV.search(name), name


def test_the_config_pattern_is_the_stricter_of_the_two():
    """Config additionally requires UPPER_SNAKE and a length bound, because it names a variable the
    operator is about to create; the sandbox only has to RECOGNIZE whatever already exists. Strictly
    narrower is the safe direction — never the reverse."""
    for name in ("openai_api_key", "Api-Key", "9_API_KEY", "A" * 70 + "_API_KEY"):
        assert not _SECRET_ENV_NAME.match(name), name
        assert SECRET_ENV.search(name), name          # the sandbox is the looser of the two


def test_the_validator_actually_uses_the_pattern():
    Settings(llm_profiles={"p": {"api_key_env": "LOOPLAB_LLM_API_KEY_CODER"}})
    with pytest.raises(Exception):
        Settings(llm_profiles={"p": {"api_key_env": "MY_AUTH"}})
