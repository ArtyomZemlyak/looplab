"""Source-owned scope primitives for the speculative Card calibration gate.

The quality reader, CLI, and engine all need to agree on what one calibration
run means.  Keeping that identity here avoids importing the engine from the
quality layer (and the resulting import cycle).  The helpers are deliberately
small and strict: a calibration receipt is useful only when every accepted run
has the same workload and runtime behavior envelope.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


SPECULATION_CALIBRATION_SEEDS = (0, 1, 2)
SPECULATION_WORKLOAD_SCOPE = "quadratic_toy"
SPECULATION_POLICY_SCOPE = "greedy"

SPECULATION_TASK_SCOPE_SCHEMA = "looplab.speculation-task-scope/v1"
SPECULATION_RUNTIME_SCOPE_SCHEMA = "looplab.speculation-runtime-scope/v1"

# The immutable calibration profile covers every Settings field except these
# experiment inputs.  ``max_nodes`` is nevertheless comparison-relevant and is
# therefore included in the runtime-scope digest below.
SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS = frozenset({
    "max_nodes",
    "speculation_depth",
    "speculation_gate_receipt",
})

# These two values select the treatment and its authority; neither changes the
# runtime implementation being calibrated.  The receipt path in particular is
# host placement, never scientific identity.
SPECULATION_RUNTIME_SCOPE_VARIANT_FIELDS = frozenset({
    "speculation_depth",
    "speculation_gate_receipt",
})

# Config snapshots do not normally contain these CLI/run-placement aliases,
# but readers accept historical/synthetic snapshots that do.  Keep the set
# exact and source-owned so a new ignored field requires an explicit code edit.
SPECULATION_RUNTIME_SCOPE_PLACEMENT_FIELDS = frozenset({
    "out",
    "output",
    "output_dir",
    "run_dir",
    "run_root",
    "work_dir",
    "workdir",
})
SPECULATION_RUNTIME_SCOPE_IGNORED_FIELDS = frozenset(
    SPECULATION_RUNTIME_SCOPE_VARIANT_FIELDS
    | SPECULATION_RUNTIME_SCOPE_PLACEMENT_FIELDS
)

# These descriptors bind behavior that is constructed outside Settings.  They
# intentionally name the concrete shipped implementations and all policy
# choices checked by the calibration-only Engine boundary.
SPECULATION_RUNTIME_POLICY_DESCRIPTOR: Mapping[str, Any] = MappingProxyType({
    "implementation": "looplab.search.policy.GreedyTree",
    "scope": SPECULATION_POLICY_SCOPE,
    "n_seeds": len(SPECULATION_CALIBRATION_SEEDS),
    "debug_depth": 1,
    "enable_merge": True,
    "merge_every": 3,
    "max_merges": 2,
    "ablate_every": 0,
    "operator_bandit": False,
})
SPECULATION_RUNTIME_ROLES_DESCRIPTOR: Mapping[str, Any] = MappingProxyType({
    "researcher": "looplab.agents.roles.ToyResearcher",
    "researcher_calibration_concepts": True,
    "developer": "looplab.agents.roles.ToyObjectiveDeveloper",
    "developer_calibration_gpu_probe": True,
    "isolated_role_factory": True,
})
SPECULATION_RUNTIME_SANDBOX_DESCRIPTOR: Mapping[str, Any] = MappingProxyType({
    "implementation": "looplab.runtime.sandbox.SubprocessSandbox",
    "trust_mode": "trusted_local",
})

_CANONICAL_TOY_TASK_WITHOUT_SEED: dict[str, Any] = {
    "kind": "quadratic",
    "id": "toy_quadratic",
    "goal": "minimize (x-3)^2 + (y+1)^2",
    "direction": "min",
    "comparison_contract": None,
    "bounds": {"x": [-10.0, 10.0], "y": [-10.0, 10.0]},
    "step": 1.0,
    "noise": 0.0,
}
_TOY_TASK_FIELDS = frozenset({*_CANONICAL_TOY_TASK_WITHOUT_SEED, "seed"})


def _strict_json_value(value: object, *, path: str = "$") -> Any:
    """Copy *value* into the plain strict-JSON domain.

    ``json.dumps`` accepts Python conveniences such as tuples and integer map
    keys.  Runtime snapshots are already JSON, so accepting those conveniences
    here would create two possible preimages for one apparent snapshot.
    """

    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite JSON number")
        return value
    if type(value) is list:
        return [
            _strict_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{path} contains a non-string JSON object key")
            normalized[key] = _strict_json_value(item, path=f"{path}.{key}")
        return normalized
    raise ValueError(f"{path} contains a non-JSON value of type {type(value).__name__}")


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
        raise ValueError(f"value is not canonical JSON: {exc}") from exc


def _task_mapping(task: object) -> Mapping[str, Any]:
    if isinstance(task, Mapping):
        return task
    model_dump = getattr(task, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json", exclude_none=False)
        except Exception as exc:
            raise ValueError("calibration task could not be serialized") from exc
        if isinstance(dumped, Mapping):
            return dumped
        raise ValueError("calibration task serialization must be a mapping")

    # Supporting a plain attribute object keeps this primitive adapter-neutral;
    # the Engine separately requires the concrete ToyTask class at admission.
    try:
        return {field: getattr(task, field) for field in _TOY_TASK_FIELDS}
    except (AttributeError, TypeError) as exc:
        raise ValueError("calibration task must be an object or mapping") from exc


def canonical_speculation_toy_task(
    task: object,
    *,
    require_seed_set: bool = False,
) -> dict[str, Any]:
    """Validate and return the one deterministic quadratic calibration task.

    Every field is exact and only the integer seed may vary.  When
    ``require_seed_set`` is true, the seed must be one of the three
    source-owned paired-calibration replicates.
    """

    raw = dict(_task_mapping(task))
    # A missing null comparison contract and an explicitly serialized null are
    # the same canonical ToyTask.  No other missing or additional key is valid.
    raw.setdefault("comparison_contract", None)
    if set(raw) != _TOY_TASK_FIELDS:
        missing = sorted(_TOY_TASK_FIELDS - set(raw))
        extra = sorted(set(raw) - _TOY_TASK_FIELDS)
        raise ValueError(
            f"calibration task fields differ (missing={missing}, extra={extra})")
    try:
        normalized = _strict_json_value(raw, path="task")
    except RecursionError as exc:
        raise ValueError("calibration task is recursively nested") from exc
    if not isinstance(normalized, dict):  # defensive; ``raw`` is a dict above.
        raise ValueError("calibration task must serialize to an object")

    seed = normalized.get("seed")
    if type(seed) is not int:
        raise ValueError("calibration task seed must be an integer")
    if require_seed_set and seed not in SPECULATION_CALIBRATION_SEEDS:
        raise ValueError(
            "calibration task seed must be one of "
            f"{SPECULATION_CALIBRATION_SEEDS}")

    expected = {**_CANONICAL_TOY_TASK_WITHOUT_SEED, "seed": seed}
    if _canonical_json(normalized) != _canonical_json(expected):
        raise ValueError(
            "task must be the canonical deterministic quadratic ToyTask "
            "(only its integer seed may vary)")
    return expected


def speculation_runtime_scope_digest(config: object) -> str:
    """Digest all behavior in a normalized Settings/runtime envelope.

    Only the treatment depth, receipt location, and explicit output-placement
    aliases are removed.  In particular ``max_nodes`` remains in the digest,
    so a cheaper or longer trajectory cannot borrow another run's receipt.
    """

    if not isinstance(config, Mapping):
        raise ValueError("speculation runtime config must be a mapping")
    try:
        normalized = _strict_json_value(config, path="config")
    except RecursionError as exc:
        raise ValueError("speculation runtime config is recursively nested") from exc
    if not isinstance(normalized, dict):  # defensive; Mapping normalizes to dict.
        raise ValueError("speculation runtime config must serialize to an object")

    max_nodes = normalized.get("max_nodes")
    if type(max_nodes) is not int:
        raise ValueError("speculation runtime config requires integer max_nodes")
    if not 1 <= max_nodes <= 64:
        raise ValueError("speculation runtime config max_nodes must be in 1..64")

    settings = {
        key: value for key, value in normalized.items()
        if key not in SPECULATION_RUNTIME_SCOPE_IGNORED_FIELDS
    }
    envelope = {
        "schema": SPECULATION_RUNTIME_SCOPE_SCHEMA,
        "settings": settings,
        "workload_scope": SPECULATION_WORKLOAD_SCOPE,
        "calibration_seeds": list(SPECULATION_CALIBRATION_SEEDS),
        "policy": dict(SPECULATION_RUNTIME_POLICY_DESCRIPTOR),
        "roles": dict(SPECULATION_RUNTIME_ROLES_DESCRIPTOR),
        "sandbox": dict(SPECULATION_RUNTIME_SANDBOX_DESCRIPTOR),
    }
    return "sha256:" + hashlib.sha256(_canonical_json(envelope)).hexdigest()


__all__ = [
    "SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS",
    "SPECULATION_CALIBRATION_SEEDS",
    "SPECULATION_POLICY_SCOPE",
    "SPECULATION_RUNTIME_POLICY_DESCRIPTOR",
    "SPECULATION_RUNTIME_ROLES_DESCRIPTOR",
    "SPECULATION_RUNTIME_SANDBOX_DESCRIPTOR",
    "SPECULATION_RUNTIME_SCOPE_IGNORED_FIELDS",
    "SPECULATION_RUNTIME_SCOPE_PLACEMENT_FIELDS",
    "SPECULATION_RUNTIME_SCOPE_SCHEMA",
    "SPECULATION_RUNTIME_SCOPE_VARIANT_FIELDS",
    "SPECULATION_TASK_SCOPE_SCHEMA",
    "SPECULATION_WORKLOAD_SCOPE",
    "canonical_speculation_toy_task",
    "speculation_runtime_scope_digest",
]
