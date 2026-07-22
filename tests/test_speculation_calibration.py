from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.config import Settings
from looplab.search.speculation_calibration import (
    SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS,
    SPECULATION_CALIBRATION_SEEDS,
    SPECULATION_POLICY_SCOPE,
    SPECULATION_RUNTIME_POLICY_DESCRIPTOR,
    SPECULATION_RUNTIME_ROLES_DESCRIPTOR,
    SPECULATION_RUNTIME_SANDBOX_DESCRIPTOR,
    SPECULATION_RUNTIME_SCOPE_IGNORED_FIELDS,
    SPECULATION_RUNTIME_SCOPE_PLACEMENT_FIELDS,
    SPECULATION_RUNTIME_SCOPE_SCHEMA,
    SPECULATION_RUNTIME_SCOPE_VARIANT_FIELDS,
    SPECULATION_TASK_SCOPE_SCHEMA,
    SPECULATION_WORKLOAD_SCOPE,
    canonical_speculation_toy_task,
    speculation_runtime_scope_digest,
)


def _declared_settings_snapshot() -> dict:
    values = {
        name: field.get_default(call_default_factory=True)
        for name, field in Settings.model_fields.items()
    }
    return Settings.model_construct(**values).model_dump(mode="json")


def _canonical_task(seed: int = 0) -> dict:
    return {
        "kind": "quadratic",
        "id": "toy_quadratic",
        "goal": "minimize (x-3)^2 + (y+1)^2",
        "direction": "min",
        "comparison_contract": None,
        "bounds": {"x": [-10.0, 10.0], "y": [-10.0, 10.0]},
        "seed": seed,
        "step": 1.0,
        "noise": 0.0,
    }


def test_scope_vocabulary_and_exclusion_sets_are_exact_and_source_owned():
    assert SPECULATION_CALIBRATION_SEEDS == (0, 1, 2)
    assert SPECULATION_WORKLOAD_SCOPE == "quadratic_toy"
    assert SPECULATION_POLICY_SCOPE == "greedy"
    assert SPECULATION_TASK_SCOPE_SCHEMA == "looplab.speculation-task-scope/v1"
    assert SPECULATION_RUNTIME_SCOPE_SCHEMA == "looplab.speculation-runtime-scope/v1"
    assert SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS == {
        "max_nodes", "speculation_depth", "speculation_gate_receipt"}
    assert SPECULATION_RUNTIME_SCOPE_VARIANT_FIELDS == {
        "speculation_depth", "speculation_gate_receipt"}
    assert SPECULATION_RUNTIME_SCOPE_PLACEMENT_FIELDS == {
        "out", "output", "output_dir", "run_dir", "run_root", "work_dir", "workdir"}
    assert SPECULATION_RUNTIME_SCOPE_IGNORED_FIELDS == (
        SPECULATION_RUNTIME_SCOPE_VARIANT_FIELDS
        | SPECULATION_RUNTIME_SCOPE_PLACEMENT_FIELDS
    )
    assert SPECULATION_RUNTIME_POLICY_DESCRIPTOR["implementation"].endswith(".GreedyTree")
    assert SPECULATION_RUNTIME_POLICY_DESCRIPTOR["n_seeds"] == 3
    assert SPECULATION_RUNTIME_ROLES_DESCRIPTOR["isolated_role_factory"] is True
    assert SPECULATION_RUNTIME_SANDBOX_DESCRIPTOR == {
        "implementation": "looplab.runtime.sandbox.SubprocessSandbox",
        "trust_mode": "trusted_local",
    }


def test_canonical_task_accepts_mapping_pydantic_object_and_only_integer_seed_varies():
    expected = _canonical_task(seed=17)
    assert canonical_speculation_toy_task(expected) == expected
    assert canonical_speculation_toy_task(ToyTask(seed=17)) == expected
    without_null = {key: value for key, value in expected.items()
                    if key != "comparison_contract"}
    assert canonical_speculation_toy_task(without_null) == expected

    for seed in SPECULATION_CALIBRATION_SEEDS:
        assert canonical_speculation_toy_task(
            _canonical_task(seed), require_seed_set=True)["seed"] == seed
    with pytest.raises(ValueError, match="seed must be one of"):
        canonical_speculation_toy_task(expected, require_seed_set=True)


@pytest.mark.parametrize(("field", "spoof"), [
    ("kind", "regression"),
    ("id", "lookalike"),
    ("goal", "minimize something else"),
    ("direction", "max"),
    ("bounds", {"x": [-10.0, 10.0], "y": [-9.0, 10.0]}),
    ("step", 2.0),
    ("noise", 0.01),
    ("comparison_contract", {"schema": 1}),
    ("seed", True),
])
def test_canonical_task_rejects_every_spoof_variant(field, spoof):
    task = _canonical_task()
    task[field] = spoof
    with pytest.raises(ValueError):
        canonical_speculation_toy_task(task)


def test_canonical_task_rejects_missing_extra_and_non_json_material():
    missing = _canonical_task()
    missing.pop("goal")
    with pytest.raises(ValueError, match="fields differ"):
        canonical_speculation_toy_task(missing)

    extra = {**_canonical_task(), "objective_variant": "spoof"}
    with pytest.raises(ValueError, match="fields differ"):
        canonical_speculation_toy_task(extra)

    non_json = _canonical_task()
    non_json["bounds"] = {"x": (-10.0, 10.0), "y": [-10.0, 10.0]}
    with pytest.raises(ValueError, match="non-JSON"):
        canonical_speculation_toy_task(non_json)


def test_runtime_scope_digest_is_order_stable_and_ignores_only_variants_and_placement():
    base = _declared_settings_snapshot()
    base["max_nodes"] = 11
    expected = speculation_runtime_scope_digest(base)
    assert expected.startswith("sha256:") and len(expected) == 71
    assert speculation_runtime_scope_digest(dict(reversed(list(base.items())))) == expected

    changed = deepcopy(base)
    changed.update({
        "speculation_depth": 7,
        "speculation_gate_receipt": "D:/other-machine/receipt.json",
        "out": "D:/runs/a",
        "output": "D:/runs/b",
        "output_dir": "D:/runs/c",
        "run_dir": "D:/runs/d",
        "run_root": "D:/runs/e",
        "work_dir": "D:/runs/f",
        "workdir": "D:/runs/g",
    })
    assert speculation_runtime_scope_digest(changed) == expected

    changed["speculation_gate_receipt"] = "C:/entirely/different/authority.json"
    changed["speculation_depth"] = 0
    changed["run_dir"] = "C:/entirely/different/run"
    assert speculation_runtime_scope_digest(changed) == expected


def _different_json_value(value):
    if value is None:
        return "changed-from-null"
    if type(value) is bool:
        return not value
    if type(value) is int:
        return value + 1
    if type(value) is float:
        return value + 0.125
    if type(value) is str:
        return value + "-changed"
    if type(value) is list:
        return [*value, "changed"]
    if type(value) is dict:
        return {**value, "__changed__": True}
    raise AssertionError(f"unexpected Settings JSON type: {type(value).__name__}")


def test_runtime_scope_digest_changes_for_max_nodes_and_every_behavior_field():
    base = _declared_settings_snapshot()
    base["max_nodes"] = 11
    original = speculation_runtime_scope_digest(base)

    max_nodes_changed = {**base, "max_nodes": 12}
    assert speculation_runtime_scope_digest(max_nodes_changed) != original

    for field, value in base.items():
        if field in SPECULATION_RUNTIME_SCOPE_IGNORED_FIELDS or field == "max_nodes":
            continue
        changed = deepcopy(base)
        changed[field] = _different_json_value(value)
        assert speculation_runtime_scope_digest(changed) != original, field

    # Future behavior fields are covered automatically rather than falling
    # through an allow-list that would need to be updated in lockstep.
    assert speculation_runtime_scope_digest(
        {**base, "future_behavior": {"enabled": True}}) != original


@pytest.mark.parametrize("config", [
    None,
    [],
    "not-an-object",
    {"max_nodes": True},
    {"max_nodes": 8.0},
    {"max_nodes": 0},
    {"max_nodes": 1_000_001},
    {"behavior": "missing max_nodes"},
    {"max_nodes": 8, "bad": (1, 2)},
    {"max_nodes": 8, "bad": {1: "non-string key"}},
    {"max_nodes": 8, "bad": {1, 2}},
    {"max_nodes": 8, "bad": Path("not-json")},
    {"max_nodes": 8, "bad": float("nan")},
    {"max_nodes": 8, "bad": float("inf")},
    {"max_nodes": 8, "bad": [0.0, float("-inf")]},
])
def test_runtime_scope_digest_rejects_invalid_non_json_or_nonfinite_config(config):
    with pytest.raises(ValueError):
        speculation_runtime_scope_digest(config)
