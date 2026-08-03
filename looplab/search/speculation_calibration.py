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

import orjson

from looplab.core.config import Settings
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
# The snapshot's own DOCUMENT-format marker (`config.CONFIG_SNAPSHOT_SCHEMA_KEY`). It describes how
# the file is written, never how the run behaves, so it must not vary the runtime scope — otherwise
# stamping it would invalidate every receipt earned before it existed. Spelled literally rather than
# imported: `search` importing `core.config` here would be a new dependency for one string.
SPECULATION_RUNTIME_SCOPE_DOCUMENT_FIELDS = frozenset({"config_snapshot_schema"})
# Fields `Settings.masked_snapshot` DROPS rather than masks. A digest that includes them can only be
# reproduced by a caller holding live Settings, never by one reading a persisted snapshot — so the
# two sides of the same comparison (the CLI/engine scope pin, built from live Settings, and the
# receipt's own `runtime_scope_sha256`, built from the run's config snapshot) would disagree by
# construction, and every positive `speculation_depth` would be refused as "runtime-scope
# mismatched". Same reasoning as the document marker above: what no reader can see must not vary the
# scope. Spelled literally to keep `search` from importing `core.config` for one string.
SPECULATION_RUNTIME_SCOPE_REDACTED_FIELDS = frozenset({"llm_api_key_base_url"})
SPECULATION_RUNTIME_SCOPE_IGNORED_FIELDS = frozenset(
    SPECULATION_RUNTIME_SCOPE_VARIANT_FIELDS
    | SPECULATION_RUNTIME_SCOPE_PLACEMENT_FIELDS
    | SPECULATION_RUNTIME_SCOPE_DOCUMENT_FIELDS
    | SPECULATION_RUNTIME_SCOPE_REDACTED_FIELDS
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
    "SPECULATION_RUNTIME_SCOPE_DOCUMENT_FIELDS",
    "SPECULATION_RUNTIME_SCOPE_IGNORED_FIELDS",
    "SPECULATION_RUNTIME_SCOPE_PLACEMENT_FIELDS",
    "SPECULATION_RUNTIME_SCOPE_REDACTED_FIELDS",
    "SPECULATION_RUNTIME_SCOPE_SCHEMA",
    "SPECULATION_RUNTIME_SCOPE_VARIANT_FIELDS",
    "SPECULATION_TASK_SCOPE_SCHEMA",
    "SPECULATION_WORKLOAD_SCOPE",
    "canonical_speculation_toy_task",
    "speculation_runtime_scope_digest",
]


# --------------------------------------------------------------------------- the immutable profile
# Moved here from `engine/orchestrator.py` (doc 25 SE-07). This module's docstring already claimed
# ownership of "source-owned scope primitives ... to avoid importing the engine from the quality
# layer (and the resulting import cycle)" — but two of the three constants the quality reader needed
# had stayed in the engine, so `search/speculation_quality.py` imported the orchestrator to reach
# them and the cycle existed anyway, merely deferred to call time.
#
# Nothing here touches the engine: the profile is derived from `Settings`' DECLARED defaults (core)
# and this module's own variant-field set. The digest is a receipt gate, so the move is verified
# byte-identical rather than merely "still passes".

def _declared_settings_json_defaults() -> dict[str, object]:
    """Read schema-declared defaults without consulting Settings env/.env sources.

    ``BaseSettings()`` is intentionally forbidden here: its environment precedence would make the
    supposedly source-owned profile depend on the launcher's machine. ``model_construct`` receives
    every field's declared default/default_factory directly, then Pydantic's JSON serializer turns
    tuples and other schema-native containers into the same representation written to snapshots.
    """
    declared: dict[str, object] = {}
    for name, field in Settings.model_fields.items():
        if field.is_required():
            raise RuntimeError(
                f"calibration profile cannot infer required Settings field {name!r}")
        declared[name] = field.get_default(call_default_factory=True)
    snapshot = Settings.model_construct(**declared).model_dump(mode="json")
    try:
        # Round-trip now so a future non-JSON default fails at import/source review, not after an
        # expensive GPU calibration has begun.
        canonical = orjson.loads(orjson.dumps(snapshot, option=orjson.OPT_SORT_KEYS))
    except (TypeError, ValueError, orjson.JSONEncodeError) as exc:
        raise RuntimeError("Settings declared defaults are not calibration-snapshot JSON") from exc
    if not isinstance(canonical, dict) or set(canonical) != set(Settings.model_fields):
        raise RuntimeError("Settings declared-default snapshot is incomplete")
    return canonical


# Start from *all* deterministic schema defaults, then turn every optional model/network/memory/
# adaptive path off. This literal is only the source-owned overrides; the public profile below is the
# complete Settings map minus the exact three variants above.
_SPECULATION_CALIBRATION_PROFILE_OVERRIDES: dict[str, object] = {
    "profile": "default",
    "backend": "toy",
    "developer_backend": "default",
    "n_seeds": 3,
    "max_parallel": 1,
    "parallel_build": 1,
    "eval_parallel": 1,
    "llm_parallel": 1,
    "train_monitor": False,
    "train_monitor_kill": False,
    "asha_live": False,
    "asha_live_kill": False,
    "trust_mode": "trusted_local",
    "policy": "greedy",
    "ablate_every": 0,
    "ablate_code_blocks": False,
    "merge_mode": "mean",
    "complexity_cue": False,
    "feature_engineering": False,
    "budget_aware": False,
    "failure_reflection": False,
    "watchdog_reflection": False,
    "deep_repair": False,
    "inline_repair": False,
    "auto_install_deps": False,
    "agent_control": {},
    "localize_faults": False,
    "surrogate_proposer": False,
    "researcher_panel": 1,
    "proxy_scoring": False,
    "proxy_kill_fraction": 0.0,
    "novelty_mode": "off",
    "novelty_gate": False,
    "novelty_semantic": False,
    "debug_depth": 1,
    "operator_bandit": False,
    "track_hypotheses": False,
    "reflection_priors": False,
    "comparative_lessons": False,
    "lessons_every": 0,
    "lessons_refresh_every": 0,
    "reward_hack_detect": False,
    "code_leakage_detect": False,
    "workdir_audit": False,
    "research_verify": False,
    "critic_check": False,
    "strategist_backend": "off",
    "confirm_top_k": 0,
    "confirm_seeds": 0,
    "holdout_fraction": 0.0,
    "holdout_select": False,
    "holdout_top_k": 1,
    "select_verifier": False,
    "verifier_ci_tie": False,
    "max_seconds": None,
    "max_eval_seconds": None,
    "memory_dir": None,
    "require_approval": False,
    "coverage_context": False,
    "concept_pivot": False,
    "graded_novelty": False,
    "capability_expansion": False,
    "fingerprint_universal": False,
    "cross_run_concepts": False,
    "concept_run_base": False,
    "cross_run_advisory": False,
    "cross_run_structured_claims": False,
    "cross_run_curation": False,
    "cross_run_curation_auto": False,
    "best_of_n": 1,
    "best_of_n_listwise": False,
    "foresight": False,
    "foresight_panel": 1,
    "foresight_agentic": False,
    "foresight_verify": False,
    "unified_agent": False,
    "agent_drives_actions": False,
    "card_driven_selection": True,
    "llm_cache": False,
    "phase_handoff_summary": False,
    "trace_llm_io": False,
    "researcher_tools": False,
    "cross_run_tools": False,
    "all_runs_tools": False,
    "cross_run_read_tools": False,
    "knowledge_dir": None,
    "embed_model": None,
    "embed_base_url": None,
    "memora": False,
    "memora_llm": False,
    "memora_cache": None,
    "literature_search": False,
    "web_search": False,
    "deep_research_every": 0,
    "concurrent_research": False,
    "concurrent_research_repeat": False,
    "concurrent_research_max_calls": 0,
    "concurrent_consolidate": False,
    "report_every": 0,
    "skills_dir": None,
    "prompt_dir": None,
    # Never inherit/persist a credential even though the toy profile constructs no LLM client.
    "llm_api_key": None,
    "llm_api_key_base_url": None,
}
SPECULATION_CALIBRATION_PROFILE_SETTINGS = _declared_settings_json_defaults()
SPECULATION_CALIBRATION_PROFILE_SETTINGS.update(
    _SPECULATION_CALIBRATION_PROFILE_OVERRIDES)
for _variant_field in SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS:
    SPECULATION_CALIBRATION_PROFILE_SETTINGS.pop(_variant_field, None)
_expected_calibration_profile_fields = (
    set(Settings.model_fields) - set(SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS))
if set(SPECULATION_CALIBRATION_PROFILE_SETTINGS) != _expected_calibration_profile_fields:
    missing = sorted(
        _expected_calibration_profile_fields - set(SPECULATION_CALIBRATION_PROFILE_SETTINGS))
    extra = sorted(
        set(SPECULATION_CALIBRATION_PROFILE_SETTINGS) - _expected_calibration_profile_fields)
    raise RuntimeError(
        f"calibration Settings coverage drifted (missing={missing}, extra={extra})")
try:
    # Enforce the same plain-JSON shape the quality reader compares after json.loads().
    _profile_json = orjson.loads(orjson.dumps(
        SPECULATION_CALIBRATION_PROFILE_SETTINGS, option=orjson.OPT_SORT_KEYS))
except (TypeError, ValueError, orjson.JSONEncodeError) as exc:
    raise RuntimeError("calibration Settings profile must remain JSON-safe") from exc
if _profile_json != SPECULATION_CALIBRATION_PROFILE_SETTINGS:
    raise RuntimeError("calibration Settings profile is not canonical snapshot JSON")
_SPECULATION_CALIBRATION_PROFILE_SCHEMA = "looplab.speculation-calibration-profile/v1"
SPECULATION_CALIBRATION_PROFILE_DIGEST = "sha256:" + hashlib.sha256(orjson.dumps(
    {
        "schema": _SPECULATION_CALIBRATION_PROFILE_SCHEMA,
        "settings": SPECULATION_CALIBRATION_PROFILE_SETTINGS,
    },
    option=orjson.OPT_SORT_KEYS,
)).hexdigest()
