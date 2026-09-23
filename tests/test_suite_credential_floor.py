"""The suite runs without the developer's credentials (review 2026-09-22, TST-03).

`conftest.py::_scrub_developer_credentials_for_the_whole_session` removes, for the whole pytest
process, the credentials the product would otherwise inherit from the shell that launched the suite
— 93 tests went red on a box exporting a dummy `LOOPLAB_LLM_API_KEY` pair, and none in CI, which
exports no key (the measurement is in that fixture's comment). Three halves:

* the RULE's truth table (`tests/_credential_floor.py`), including what it must NOT remove;
* a two-way scan: every credential the product reads from the environment — a `SecretStr` Settings
  field, the shared pair in `core/llm.py`, a secret-shaped literal read anywhere in `looplab/` — is
  removed by the floor or by a NAMED per-test fixture, so a new credential read cannot arrive
  unscrubbed;
* a DRIVEN run: a child pytest launched with dummy credentials exported sees none of them — from a
  MODULE-scoped fixture too, so the floor is under every higher-scoped fixture — and sees the floor's
  names again when the live tests are opted into.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _credential_floor import CREDENTIAL_ENV_EXACT, live_scenarios_opted_in, scrubbed_credential_names
from _source_scan import iter_trees
from looplab.core.config import Settings
from looplab.core.envsafe import is_secret_env
from looplab.core.llm import SHARED_BINDING_ENV, SHARED_KEY_ENV

PROBE_ENV = "LOOPLAB_TEST_CREDENTIAL_PROBE"

# Secret-shaped names the product reads that the FLOOR leaves alone, each removed for every test by
# the named per-test fixture instead (which a test that means "shared hub" overrides itself).
REMOVED_PER_TEST = {
    "LOOPLAB_UI_TOKEN": "conftest.py::_isolate_shared_origin_detection",
    "JUPYTERHUB_API_TOKEN": "conftest.py::_isolate_shared_origin_detection",
}

# What the driven half exports into the child: the floor's names, one profile-convention key with
# its binding, and the per-test ones.
EXPORTED = {
    SHARED_KEY_ENV: "sk-dummy-floor", SHARED_BINDING_ENV: "https://api.example.invalid/v1",
    "OPENAI_API_KEY": "sk-dummy-profile", "OPENAI_API_KEY_BASE_URL": "https://example.invalid/v1",
    "KAGGLE_KEY": "dummy", "LOOPLAB_KAGGLE_TOKEN": "dummy",
    "LOOPLAB_UI_TOKEN": "dummy-ui", "JUPYTERHUB_API_TOKEN": "dummy-hub",
}
FLOOR = {name for name in EXPORTED if name not in REMOVED_PER_TEST}


def test_the_rule_removes_the_credentials_and_nothing_a_box_needs():
    environ = {name: "x" for name in (
        SHARED_KEY_ENV, SHARED_BINDING_ENV, "OPENAI_API_KEY", "OPENROUTER_API_KEY_BASE_URL",
        "LOOPLAB_LLM_API_KEY_CODER", "anthropic_api_key", *CREDENTIAL_ENV_EXACT,
        # kept: git's env-injected config on a proxied box (removing a KEY while its COUNT stays
        # breaks every git call), the endpoint (not a credential), tokens the product never reads
        "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "LOOPLAB_LLM_BASE_URL",
        "GITHUB_TOKEN", "PATH")}
    assert scrubbed_credential_names(environ) == sorted([
        SHARED_KEY_ENV, SHARED_BINDING_ENV, "OPENAI_API_KEY", "OPENROUTER_API_KEY_BASE_URL",
        "LOOPLAB_LLM_API_KEY_CODER", "anthropic_api_key", *CREDENTIAL_ENV_EXACT])
    assert scrubbed_credential_names({**environ, "LOOPLAB_LIVE_SCENARIOS": "1"}) == []
    # an EMPTY value is not an opt-in — the live tests' own `skipif` reads it the same way
    assert not live_scenarios_opted_in({"LOOPLAB_LIVE_SCENARIOS": ""})
    assert scrubbed_credential_names({**environ, "LOOPLAB_LIVE_SCENARIOS": ""}) != []


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _literal_secret_env_reads() -> dict[str, set[str]]:
    """`{name: {file, …}}` for every secret-shaped name `looplab/` reads from the environment BY
    LITERAL: `os.environ.get("X")`, `os.getenv("X")`, `os.environ.pop/setdefault("X")`,
    `os.environ["X"]`."""
    reads: dict[str, set[str]] = {}
    for path, tree in iter_trees():
        for node in ast.walk(tree):
            name = None
            if (isinstance(node, ast.Call) and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                    and _dotted(node.func).endswith(("environ.get", "getenv", "environ.pop",
                                                     "environ.setdefault"))):
                name = node.args[0].value
            elif (isinstance(node, ast.Subscript) and _dotted(node.value).endswith("environ")
                    and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
                name = node.slice.value
            if name and is_secret_env(name):
                reads.setdefault(name, set()).add(path.name)
    return reads


def _product_credential_names() -> set[str]:
    secret_fields = {f"LOOPLAB_{name.upper()}" for name, field in Settings.model_fields.items()
                     if "SecretStr" in str(field.annotation)}
    assert secret_fields, "no SecretStr Settings field found — the scan below has lost its anchor"
    return secret_fields | {SHARED_KEY_ENV, SHARED_BINDING_ENV} | set(_literal_secret_env_reads())


def test_every_credential_the_product_reads_is_removed_by_the_floor_or_a_named_fixture():
    names = _product_credential_names()
    unscrubbed = {name for name in names
                  if not scrubbed_credential_names({name: "x"}) and name not in REMOVED_PER_TEST}
    assert not unscrubbed, (
        f"{sorted(unscrubbed)} are credentials the product reads from the environment that the "
        "suite would inherit from the developer's shell: extend `tests/_credential_floor.py` (or "
        "remove them in a per-test fixture and add a row to REMOVED_PER_TEST)")


def test_the_named_per_test_exemptions_are_still_credentials_the_product_reads():
    """Shrink-only: a row whose name the product no longer reads would silently license the next."""
    stale = set(REMOVED_PER_TEST) - set(_literal_secret_env_reads())
    assert not stale, f"{sorted(stale)} are no longer read by `looplab/` — delete their rows"


@pytest.fixture(scope="module")
def _module_scope_view():
    return sorted(name for name in EXPORTED if name in os.environ)


@pytest.mark.skipif(not os.environ.get(PROBE_ENV), reason="the child half of the driven checks")
def test_probe_reports_what_a_test_sees(_module_scope_view):
    Path(os.environ[PROBE_ENV]).write_text(json.dumps({
        "module_scope": _module_scope_view,
        "test_body": sorted(name for name in EXPORTED if name in os.environ)}), encoding="utf-8")


def _child_view(tmp_path: Path, *, live: bool) -> dict:
    out = tmp_path / "seen.json"
    env = {**os.environ, **EXPORTED, PROBE_ENV: str(out)}
    env.pop("LOOPLAB_LIVE_SCENARIOS", None)
    if live:
        env["LOOPLAB_LIVE_SCENARIOS"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
         f"--basetemp={tmp_path / 'bt'}", f"{Path(__file__).name}::test_probe_reports_what_a_test_sees"],
        cwd=Path(__file__).parent, env=env, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0 and out.exists(), (proc.stdout[-3000:], proc.stderr[-3000:])
    return json.loads(out.read_text(encoding="utf-8"))


def test_a_suite_launched_with_credentials_exported_sees_none_of_them(tmp_path):
    seen = _child_view(tmp_path, live=False)
    assert seen["test_body"] == [], f"a test saw {seen['test_body']} from the launching shell"
    assert not FLOOR & set(seen["module_scope"]), (
        f"a module-scoped fixture saw {sorted(FLOOR & set(seen['module_scope']))} — the floor must "
        "be under every higher-scoped fixture, not only under the tests")


def test_opting_into_the_live_tests_keeps_the_credentials_they_need(tmp_path):
    seen = _child_view(tmp_path, live=True)
    assert FLOOR <= set(seen["test_body"]), (
        f"the live tests were opted into, yet a test saw only {seen['test_body']}")
