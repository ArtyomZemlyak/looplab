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
from looplab.runtime.sandbox import SECRET_ENV, is_secret_env

_ACCEPTED = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LOOPLAB_LLM_API_KEY", "LOOPLAB_LLM_API_KEY_CODER",
    "HF_TOKEN", "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD", "MY_CREDENTIAL", "SOME_PASSWD",
]
_REJECTED = ["MY_HANDLE", "PATH", "HOME", "LOOPLAB_LLM_BASE_URL", "CUDA_VISIBLE_DEVICES"]
# Names the SANDBOX strips but the config validator still refuses. Strictly-narrower-config is the
# safe direction (a stripped name that config rejects costs nothing; the reverse hands the sandbox a
# live credential), so this asymmetry is the contract, not a gap.
_SANDBOX_ONLY = ["MY_AUTH", "AUTHORIZATION", "DB_PASSPHRASE", "SESSION_COOKIE",
                 "SLACK_WEBHOOK_URL", "SENTRY_DSN"]


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


@pytest.mark.parametrize("name", _SANDBOX_ONLY)
def test_the_sandbox_strips_secret_shapes_the_config_validator_still_refuses(name):
    assert SECRET_ENV.search(name), f"{name} rides into the sandbox un-stripped"
    assert not _SECRET_ENV_NAME.match(name), (
        f"{name} became an ACCEPTABLE api_key_env; config must stay the stricter of the two")


def test_a_connection_string_is_screened_by_its_value_not_its_name():
    """The class no name pattern can catch: a credential embedded in a connection URL under a
    perfectly innocent name. Matching DATABASE_URL/MONGO_URI/*_URI by NAME would also strip
    LOOPLAB_LLM_BASE_URL and every other legitimate endpoint, so the authority's userinfo is what
    marks it — `scheme://user:pw@host` is a credential, `https://host/v1` is an address."""
    for name, value in (("DATABASE_URL", "postgres://app:hunter2@db.internal:5432/prod"),
                        ("MONGO_URI", "mongodb+srv://svc:pw@c0.mongodb.net/rec"),
                        ("BROKER", "amqps://guest:guest@rabbit:5671/")):
        assert not SECRET_ENV.search(name), f"{name} is caught by NAME; this case is about the value"
        assert is_secret_env(name, value), f"{name} leaks its inline credentials to child processes"

    # ...and an endpoint carrying no credentials is untouched, on both screens.
    for name, value in (("LOOPLAB_LLM_BASE_URL", "https://api.example.com/v1"),
                        ("PIP_INDEX_URL", "https://pypi.org/simple"),
                        ("NO_PROXY", "localhost,127.0.0.1")):
        assert not is_secret_env(name, value), f"{name} was stripped but holds no credential"
