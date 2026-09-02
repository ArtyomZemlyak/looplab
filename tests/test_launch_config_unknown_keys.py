"""Every launch-time settings layer refuses a key that is not a setting — the file layer included.

`--set` has validated its keys since it shipped, and `parse_sets`' own docstring says why: a typo
"errors loudly instead of being silently dropped by `extra="ignore"`". The FILE layer — the
documented primary launch surface, the one `looplab init` scaffolds, the one every example in the
guide uses — validated nothing, so the identical typo took the opposite path.

Probed before the fix: a `settings:` block carrying `max_node: 30` produced a `Settings` with
`max_nodes == 8` and no diagnostic anywhere. The run then did eight nodes while its config file said
thirty, which is the "looks configured" failure the enum table one layer up exists to stop.

WHY THIS IS SAFE TO MAKE STRICT, and why the same strictness would be wrong twenty lines away:
`build_settings` has exactly ONE caller, `cli/run_cmds.py::run`. Resume reads
`config.snapshot.json` through `_settings_for_run`, which must keep `extra="ignore"` so an older
binary can still load a snapshot a newer one wrote. Refusing at launch and ignoring on resume is not
an inconsistency — the operator is present for one and absent for the other.
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from looplab.core import appconfig
from looplab.core.config import PARALLELISM_ALIASES, Settings
from looplab.core.errors import ConfigRefusal, OperatorRefusal


def _file_settings(body: str) -> dict:
    path = pathlib.Path(tempfile.mkdtemp()) / "looplab.yaml"
    path.write_text(body, encoding="utf-8")
    return appconfig.load_document(path)[1]


def test_a_mistyped_file_key_is_refused_instead_of_taking_the_default():
    """THE INCIDENT SHAPE. MUTATION: drop the call from `build_settings` -> `max_nodes` is 8, the
    run does eight nodes, and the config file says thirty with nothing to read."""
    with pytest.raises(ConfigRefusal) as excinfo:
        appconfig.build_settings(_file_settings("settings:\n  max_node: 30\n"), {}, {})

    assert "max_node" in str(excinfo.value)
    assert "settings:" in str(excinfo.value), "the refusal must name WHICH layer holds the typo"


def test_the_refusal_names_every_unknown_key_at_once():
    """One run, one fix. A refusal that names the first typo makes the operator launch N times."""
    with pytest.raises(ConfigRefusal) as excinfo:
        appconfig.build_settings(
            _file_settings("settings:\n  max_node: 30\n  llm_temperture: 0.9\n"), {}, {})

    assert "max_node" in str(excinfo.value) and "llm_temperture" in str(excinfo.value)


def test_the_refusal_is_an_operator_refusal_so_the_cli_prints_one_line():
    """CLAUDE.md: a deliberate refusal is a TYPE. `ConfigRefusal` is `OperatorRefusal` AND
    `ValueError`, so the CLI boundary prints one message at exit 2 while every existing
    `except ValueError` around this code keeps working.

    MUTATION: raise a bare `ValueError` -> the first assert fails and the operator gets a traceback
    for their own typo.
    """
    with pytest.raises(OperatorRefusal):
        appconfig.build_settings(_file_settings("settings:\n  nope: 1\n"), {}, {})
    with pytest.raises(ValueError):
        appconfig.build_settings(_file_settings("settings:\n  nope: 1\n"), {}, {})


def test_a_valid_file_still_builds_and_the_legacy_aliases_are_not_collateral():
    """The regression this could most easily cause. `max_parallel` and `parallel_build` are legacy
    spellings the canonicalizer promotes, and refusing them would break every config using them —
    they are DECLARED fields, which is what makes the plain `model_fields` test correct here.
    """
    for legacy in PARALLELISM_ALIASES:
        assert legacy in Settings.model_fields, (
            f"{legacy} is promoted by the canonicalizer but is not a declared field, so the "
            "unknown-key rule would refuse it — it needs an exception, or this test is the warning")

    settings = appconfig.build_settings(
        _file_settings("settings:\n  max_nodes: 30\n  parallel_build: 2\n  max_parallel: 3\n"),
        {}, {})

    assert settings.max_nodes == 30
    assert settings.parallel_build == 2
    assert settings.eval_parallel == 3, "the legacy alias must still be promoted"


def test_the_two_layers_share_one_rule_rather_than_two_spellings():
    """`--set` and the file layer answered the same question differently; they now answer through
    the same function. MUTATION: re-inline either check -> they drift again, which is how this
    started."""
    with pytest.raises(ConfigRefusal):
        appconfig.parse_sets(["max_node=30"])
    with pytest.raises(ConfigRefusal):
        appconfig.build_settings({"max_node": 30}, {}, {})

    import inspect
    source = inspect.getsource(appconfig.parse_sets)
    assert "refuse_unknown_settings_keys" in source, (
        "--set must resolve the question through the shared rule, not its own copy")


def test_a_typed_flag_layer_is_checked_too():
    """The layer built in CODE. It should never carry an unknown key, and that is exactly why a
    silent drop there would be a bug nobody ever sees."""
    with pytest.raises(ConfigRefusal) as excinfo:
        appconfig.build_settings({}, {"not_a_field": 1}, {})

    assert "command-line flags" in str(excinfo.value)


def test_the_credential_refusal_still_fires_and_is_not_shadowed():
    """It runs in the same loop and is the stricter rule; a reordering that let an unknown-key
    refusal answer first would change a security refusal into a spelling complaint."""
    from looplab.core.appconfig import _RUNTIME_CREDENTIAL_FIELDS

    field = sorted(_RUNTIME_CREDENTIAL_FIELDS)[0]
    with pytest.raises(ValueError, match="runtime credentials"):
        appconfig.build_settings({field: "sk-xxx"}, {}, {})
