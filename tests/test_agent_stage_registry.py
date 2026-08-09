"""Closed unified-agent stage vocabulary at fresh boundaries, compatible on snapshot resume."""
from __future__ import annotations

import logging
import warnings
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from looplab.core.appconfig import build_settings
from looplab.core.config import CONFIG_SNAPSHOT_SCHEMA_KEY, Settings, settings_from_snapshot
from looplab.core.llm import (AGENT_STAGE_KEYS, LLM_ROLE_KEYS, resolve_llm_target)


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_STAGE_KEYS = frozenset({"propose", "implement", "repair", "strategy", "pilot"})
STAGE_MAP_FIELDS = ("agent_stage_models", "agent_stage_base_urls")


def test_the_exported_five_key_registry_is_shared_by_roles_resolver_and_docs():
    assert AGENT_STAGE_KEYS == EXPECTED_STAGE_KEYS
    assert LLM_ROLE_KEYS == AGENT_STAGE_KEYS | frozenset({
        "researcher", "developer", "strategist", "compressor", "embed",
    })

    # Even a validation-bypassing historical/custom Settings-like object cannot make a split-role
    # key act as a stage override: the resolver consumes the same registry as Settings.
    bypassed = Settings(llm_model="shared", researcher_model="researcher").model_copy(update={
        "agent_stage_models": {"researcher": "wrong-stage-model"},
    })
    assert resolve_llm_target(bypassed, role="researcher").model == "researcher"

    guide = (ROOT / "docs" / "guide" / "configuration.md").read_text(encoding="utf-8")
    row = next(line for line in guide.splitlines()
               if line.startswith("| `agent_stage_models`"))
    for stage in AGENT_STAGE_KEYS:
        assert f"`{stage}`" in row
    assert "unknown or mis-cased keys and non-string/blank values are rejected" in row
    assert "does not rewrite the snapshot evidence" in guide


@pytest.mark.parametrize("field", STAGE_MAP_FIELDS)
def test_fresh_settings_accept_exact_stage_keys_and_reject_typos(field):
    values = {stage: f"override-{stage}" for stage in AGENT_STAGE_KEYS}
    assert getattr(Settings(**{field: values}), field) == values

    with pytest.raises(ValidationError, match=r"unknown agent stage key.*'implment'"):
        Settings(**{field: {"implment": "silent-before-this-fix"}})


@pytest.mark.parametrize("field", STAGE_MAP_FIELDS)
def test_stage_maps_require_mappings_with_exact_string_keys(field):
    with pytest.raises(ValidationError, match="must be a mapping"):
        Settings(**{field: [["implement", "coder"]]})
    with pytest.raises(ValidationError, match="keys must be exact strings"):
        Settings(**{field: {1: "coder"}})


@pytest.mark.parametrize("field", STAGE_MAP_FIELDS)
@pytest.mark.parametrize("value", [None, 7, {}, "", " \t"], ids=[
    "none", "number", "mapping", "empty", "blank",
])
def test_fresh_stage_map_values_are_nonblank_strings(field, value):
    with pytest.raises(ValidationError, match="values must be nonblank strings"):
        Settings(**{field: {"pilot": value}})


@pytest.mark.parametrize(("field", "env_name"), [
    ("agent_stage_models", "LOOPLAB_AGENT_STAGE_MODELS"),
    ("agent_stage_base_urls", "LOOPLAB_AGENT_STAGE_BASE_URLS"),
])
def test_environment_stage_maps_reject_unknown_keys(field, env_name, monkeypatch):
    monkeypatch.setenv(env_name, '{"stratgey":"override"}')
    with pytest.raises(ValidationError, match=r"unknown agent stage key.*'stratgey'"):
        Settings()


@pytest.mark.parametrize("field", STAGE_MAP_FIELDS)
def test_config_loader_rejects_unknown_stage_keys(field):
    with pytest.raises(ValidationError, match=r"unknown agent stage key.*'repait'"):
        build_settings({field: {"repait": "override"}}, {}, {})


def test_launch_preflight_rejects_unknown_stage_keys(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app

    response = TestClient(make_app(tmp_path)).post("/api/start/preflight", json={
        "run_id": "bad-stage-key",
        "task": {
            "benchmark": "quadratic",
            "goal": "minimize the objective",
            "direction": "min",
        },
        "settings": {"agent_stage_models": {"implment": "coder"}},
    })

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_launch_settings"
    assert "unknown agent stage key" in detail["field_errors"]["settings.agent_stage_models"]
    assert not (tmp_path / "bad-stage-key").exists()


@pytest.mark.parametrize("legacy", [False, True], ids=["current", "legacy"])
def test_snapshot_resume_filters_unknown_stage_keys_with_sorted_warning_without_mutation(
        legacy, caplog):
    snapshot = Settings(
        agent_stage_models={"implement": "kept-model"},
        agent_stage_base_urls={"strategy": "https://kept.invalid/v1"},
    ).masked_snapshot()
    if legacy:
        snapshot.pop(CONFIG_SNAPSHOT_SCHEMA_KEY)
    snapshot["agent_stage_models"] = {
        "zeta": "old-z", "implement": "kept-model", "alpha": "old-a",
    }
    snapshot["agent_stage_base_urls"] = {
        "url-z": "old-z", "strategy": "https://kept.invalid/v1", "url-a": "old-a",
    }
    evidence_before = deepcopy(snapshot)

    with caplog.at_level(logging.WARNING, logger="looplab.core.config"):
        restored = settings_from_snapshot(snapshot)

    assert restored.agent_stage_models == {"implement": "kept-model"}
    assert restored.agent_stage_base_urls == {"strategy": "https://kept.invalid/v1"}
    assert snapshot == evidence_before
    assert [record.getMessage() for record in caplog.records
            if record.name == "looplab.core.config"] == [
        "config snapshot agent_stage_models contains unknown agent stage key(s): "
        "'alpha', 'zeta'; ignoring them for resume compatibility; "
        "snapshot evidence was not rewritten",
        "config snapshot agent_stage_base_urls contains unknown agent stage key(s): "
        "'url-a', 'url-z'; ignoring them for resume compatibility; "
        "snapshot evidence was not rewritten",
    ]


def test_snapshot_resume_does_not_repair_a_non_mapping_stage_map():
    snapshot = Settings().masked_snapshot()
    snapshot["agent_stage_models"] = [["implement", "coder"]]
    with pytest.raises(ValidationError, match="must be a mapping"):
        settings_from_snapshot(snapshot)


@pytest.mark.parametrize("legacy", [False, True], ids=["current", "legacy"])
def test_snapshot_resume_filters_invalid_stage_values_without_mutating_evidence(legacy, caplog):
    snapshot = Settings(
        agent_stage_models={"implement": "kept-model"},
        agent_stage_base_urls={"strategy": "https://kept.invalid/v1"},
    ).masked_snapshot()
    if legacy:
        snapshot.pop(CONFIG_SNAPSHOT_SCHEMA_KEY)
    snapshot["agent_stage_models"].update({"pilot": {"bad": "shape"}, "repair": "  "})
    snapshot["agent_stage_base_urls"].update({"pilot": 123, "repair": ""})
    evidence_before = deepcopy(snapshot)

    with caplog.at_level(logging.WARNING, logger="looplab.core.config"):
        restored = settings_from_snapshot(snapshot)

    assert restored.agent_stage_models == {"implement": "kept-model"}
    assert restored.agent_stage_base_urls == {"strategy": "https://kept.invalid/v1"}
    assert snapshot == evidence_before
    assert [record.getMessage() for record in caplog.records
            if record.name == "looplab.core.config"] == [
        "config snapshot agent_stage_models contains non-string/blank override value(s) for "
        "stage key(s): 'pilot', 'repair'; ignoring them for resume compatibility; "
        "snapshot evidence was not rewritten",
        "config snapshot agent_stage_base_urls contains non-string/blank override value(s) for "
        "stage key(s): 'pilot', 'repair'; ignoring them for resume compatibility; "
        "snapshot evidence was not rewritten",
    ]


def test_snapshot_stage_compatibility_survives_runtime_warnings_as_errors(caplog):
    """Process-wide warning policy must not turn a compatible resume diagnostic into refusal."""
    snapshot = Settings(agent_stage_models={"implement": "kept-model"}).masked_snapshot()
    snapshot["agent_stage_models"] = {
        "implement": "kept-model", "retired-stage": "old-model", "pilot": {"bad": "shape"},
    }
    evidence_before = deepcopy(snapshot)

    with (warnings.catch_warnings(),
          caplog.at_level(logging.WARNING, logger="looplab.core.config")):
        warnings.simplefilter("error")
        restored = settings_from_snapshot(snapshot)

    assert restored.agent_stage_models == {"implement": "kept-model"}
    assert snapshot == evidence_before
    messages = [record.getMessage() for record in caplog.records
                if record.name == "looplab.core.config"]
    assert messages == [
        "config snapshot agent_stage_models contains unknown agent stage key(s): "
        "'retired-stage'; ignoring them for resume compatibility; "
        "snapshot evidence was not rewritten",
        "config snapshot agent_stage_models contains non-string/blank override value(s) for "
        "stage key(s): 'pilot'; ignoring them for resume compatibility; "
        "snapshot evidence was not rewritten",
    ]
