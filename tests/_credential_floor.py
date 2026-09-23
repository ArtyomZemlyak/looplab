"""Which of the developer's credentials the suite removes from its own process (review 2026-09-22,
TST-03). The rule lives here, apart from `conftest.py`, so `tests/test_suite_credential_floor.py`
can state its truth table without importing a conftest; the SESSION fixture that applies it is
`conftest.py::_scrub_developer_credentials_for_the_whole_session`, whose comment has the measurement.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

# Credentials the product reads by a name that does not say API_KEY (`adapters/kaggle_dl.py`).
CREDENTIAL_ENV_EXACT = frozenset({"LOOPLAB_KAGGLE_TOKEN", "KAGGLE_KEY"})


def live_scenarios_opted_in(environ: Mapping[str, str] | None = None) -> bool:
    """The live tests' OWN predicate (`tests/test_live_*.py` skip on `os.environ.get(...)`
    truthiness), so "opted in" cannot mean one thing to the floor and another to the tests it
    protects: an empty value is not an opt-in in either place."""
    return bool((os.environ if environ is None else environ).get("LOOPLAB_LIVE_SCENARIOS"))


def scrubbed_credential_names(environ: Mapping[str, str]) -> list[str]:
    """The names the session floor removes from *environ*: none when the live tests are opted into;
    otherwise every `API_KEY`-named variable — the shared pair (`core/llm.py::SHARED_KEY_ENV` /
    `SHARED_BINDING_ENV`) and whatever a connection profile's `api_key_env` names by convention,
    with its `<name>_BASE_URL` binding — plus `CREDENTIAL_ENV_EXACT`.

    Deliberately NOT every secret-shaped name (`core/envsafe.py::is_secret_env`): that screen also
    matches `GIT_CONFIG_KEY_<n>`, which a proxied box uses to configure git, and removing a key while
    its `GIT_CONFIG_COUNT` stays makes every git call in the suite fail."""
    if live_scenarios_opted_in(environ):
        return []
    return sorted(name for name in environ
                  if "API_KEY" in name.upper() or name in CREDENTIAL_ENV_EXACT)
