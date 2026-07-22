"""Receipt-backed paired quality gate for speculative Card execution.

This module is intentionally outside the engine/config/CLI wiring.  It consumes completed run
directories as immutable evidence, reconstructs speculative ownership through replay, and emits a
bounded receipt whose source and implementation digests can be revalidated later.  Counts supplied
by a caller are never accepted as evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from looplab.core.atomicio import strict_atomic_write_text
from looplab.core.config import RUN_START_PINNED_FIELDS
from looplab.core.fitness import VERIFIER_SELECTION_CONTRACT
from looplab.core.hardware import effective_gpu_inventory
from looplab.core.models import (
    CARD_ACTION_DIGEST_V1_FIELDS,
    Event,
    NodeStatus,
    card_ownership_receipt,
    idea_proposal_ref,
    normalize_researcher_footprint,
)
from looplab.events.eventstore import MAX_EVENT_BATCH_BYTES, decode_event_record
from looplab.events.replay import FoldCursor, flagged_node_ids, fold, promotion_eligible_nodes
from looplab.events.types import (
    ALL_EVENT_TYPES,
    EV_BUDGET,
    EV_BUDGET_EXTEND,
    EV_CARD_ADDED,
    EV_CARD_BUILD_DONE,
    EV_CARD_BUILD_REQUESTED,
    EV_CARD_ENRICHED,
    EV_DIVERSITY_ARCHIVE,
    EV_FINALIZATION_FINISHED,
    EV_FINALIZE_STEP,
    EV_LOG_REPAIRED,
    EV_NODE_ABORT,
    EV_NODE_BUILDING,
    EV_NODE_CREATED,
    EV_NODE_EVALUATED,
    EV_NODE_FAILED,
    EV_NODE_REPAIRED,
    EV_NODE_RESET,
    EV_NODE_TOMBSTONED,
    EV_PAUSE,
    EV_POLICY_DECISION,
    EV_RESTART,
    EV_RESUME,
    EV_RESUME_REQUESTED,
    EV_RESUME_SERVED,
    EV_RUN_ABORT,
    EV_RUN_FINISHED,
    EV_RUN_REOPENED,
    EV_RUN_STARTED,
    EV_SETUP_FINISHED,
    EV_SETUP_STARTED,
    EV_SETUP_STEP,
    EV_STAGE_FINISHED,
)
from looplab.search.card_selection import (
    CARD_FRESHNESS_SUPERSEDED_ERROR,
    META_CARD_ID,
    CardResourceEnvelope,
    card_budget_used,
    speculative_card_actions,
    speculative_raw_actions,
)
from looplab.search.concept_projection import current_concept_projection
from looplab.search.policy import GreedyTree
from looplab.search.speculation_calibration import (
    SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS,
    SPECULATION_CALIBRATION_SEEDS,
    SPECULATION_POLICY_SCOPE,
    SPECULATION_WORKLOAD_SCOPE,
    canonical_speculation_toy_task,
    speculation_runtime_scope_digest,
)
from looplab.search.scorer_fidelity import (
    SCORER_FIDELITY_CASE_COUNT,
    SCORER_FIDELITY_CASE_NAMES,
    SCORER_FIDELITY_SCHEMA,
    scorer_fidelity_gate,
)
from looplab.agents.roles import (
    SPECULATION_CUDA_PROBE_CODE_PREFIX,
    SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC,
    SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS,
    SPECULATION_CUDA_PROBE_STATIC_EXTRA_METRICS,
)


SPECULATION_RUN_ANALYSIS_SCHEMA = "looplab.speculation-run-analysis/v1"
SPECULATION_QUALITY_GATE_SCHEMA = "looplab.speculation-quality-gate/v1"

# These values are source-owned.  There is deliberately no thresholds argument on any public API.
SPECULATION_QUALITY_THRESHOLDS: Mapping[str, int | float] = MappingProxyType({
    "min_pairs": 3,
    "scorer_mismatches": 0,
    "max_mean_normalized_regret": 0.05,
    "max_pair_normalized_regret": 0.10,
    "min_mean_hit_rate": 0.70,
    "max_pair_divergence_rate": 0.34,
    "min_pair_coverage_ratio": 0.90,
})

_MAX_LOGICAL_EVENTS = 100_000
_MAX_EVENTS_BYTES = 64 * 1024 * 1024
_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_TASK_BYTES = 1024 * 1024
_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_SCORER_BYTES = 256 * 1024
_MAX_PATH_CHARS = 4096
_MAX_TRAJECTORY_POINTS = 4096
_MAX_ERROR_CHARS = 300
_MAX_GPUS = 256
_GPU_IDENTITY_FIELDS = frozenset({
    "index",
    "uuid",
    "pci_bus_id",
    "name",
    "mem_total_mib",
    "driver_version",
    "cuda_driver_version",
})
_GPU_UUID_RE = re.compile(
    r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_GPU_PCI_RE = re.compile(r"[0-9a-f]{4,8}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", re.IGNORECASE)
_FINALIZE_SCOPE_RE = re.compile(r"finalize:[0-9a-f]{32}")
_IDEA_PROPOSAL_REF_RE = re.compile(r"idea:v1:[0-9a-f]{64}")
_CONFIG_HASH_RE = re.compile(r"[0-9a-f]{12}")
_SETUP_MANIFEST_RE = re.compile(r"[0-9a-f]{16}")

_CALIBRATION_RUN_STARTED_FIELDS = frozenset({
    "run_id",
    "task_id",
    "goal",
    "direction",
    "config_hash",
    "workspace",
    "env",
    "dirty_inputs",
    "trust_gate",
    "select_verifier_contract",
    "speculation_implementation_digest",
    "speculation_runtime_scope_sha256",
    "speculation_calibration_profile_digest",
    "speculation_calibration_gpu_inventory",
    "speculation_calibration_seed",
    "speculation_policy_scope",
}) | RUN_START_PINNED_FIELDS
_CALIBRATION_CARD_ADDED_FIELDS = frozenset({
    "id",
    "statement",
    "source",
    "at_node",
    "rationale",
    "idea",
    "parent_id",
    "parent_ids",
    "parent_generations",
    "scored_against",
    "scored_against_generation",
    "scored_against_empty",
    "footprint",
    "steering_context",
    "ownership_receipt",
    "proposal_ref",
})
_CALIBRATION_CARD_IDEA_FIELDS = frozenset({
    "operator", "params", "space", "eval_profile", "eval_timeout",
})

# This is an allow-list, not merely a list of known-bad controls.  A future event type therefore
# cannot silently become admissible calibration evidence until its selection/finalization semantics
# receive an explicit source review here.
_CALIBRATION_COMMON_EVENT_TYPES = frozenset({
    EV_SETUP_STARTED,
    EV_SETUP_STEP,
    EV_RUN_STARTED,
    EV_SETUP_FINISHED,
    EV_CARD_ADDED,
    EV_CARD_ENRICHED,
    EV_NODE_BUILDING,
    EV_NODE_CREATED,
    EV_NODE_EVALUATED,
    EV_POLICY_DECISION,
    EV_FINALIZE_STEP,
    EV_RUN_FINISHED,
    EV_BUDGET,
    EV_DIVERSITY_ARCHIVE,
    EV_FINALIZATION_FINISHED,
})
_CALIBRATION_TREATMENT_EVENT_TYPES = frozenset({
    EV_CARD_BUILD_REQUESTED,
    EV_CARD_BUILD_DONE,
    EV_NODE_FAILED,
})

# A calibration lane is launch-only and deterministic.  These rows all denote operator recovery,
# mutation, or an alternate node lifecycle; accepting them would let a reset/retry/error trajectory
# be presented as the clean attempt-zero A/B protocol measured by this receipt.
_FORBIDDEN_CALIBRATION_LIFECYCLE_EVENTS = frozenset({
    EV_BUDGET_EXTEND,
    EV_LOG_REPAIRED,
    EV_NODE_ABORT,
    EV_NODE_REPAIRED,
    EV_NODE_RESET,
    EV_NODE_TOMBSTONED,
    EV_PAUSE,
    EV_RESTART,
    EV_RESUME,
    EV_RESUME_REQUESTED,
    EV_RESUME_SERVED,
    EV_RUN_ABORT,
    EV_RUN_REOPENED,
    EV_STAGE_FINISHED,
})

# Fresh calibration snapshots contain the complete Settings schema.  Only the treatment selector and
# the (necessarily null) receipt path differ scientifically; accepting output-placement aliases here
# would create a second snapshot schema that the launcher never writes.
_PAIR_VARIANT_CONFIG_FIELDS = frozenset({"speculation_depth", "speculation_gate_receipt"})

_IMPLEMENTATION_OPTIONAL_FILES = ("pyproject.toml",)
_IMPLEMENTATION_REQUIRED_PACKAGE_FILES = ("serve/settings_ui_schema.json",)

_RECEIPT_FIELDS = frozenset({
    "schema",
    "thresholds",
    "require_gpu",
    "gpu_inventory",
    "implementation_digest",
    "environment_sha256",
    "policy_scope",
    "workload_scope",
    "calibration_seeds",
    "task_profile_sha256",
    "admitted_depth",
    "admitted_max_nodes",
    "runtime_scope_sha256",
    "calibration_profile_digest",
    "scorer_fidelity",
    "pairs",
    "aggregates",
    "errors",
    "passed",
    "self_digest",
})


class _DuplicateKey(ValueError):
    pass


def _object_without_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in items:
        if key in out:
            raise _DuplicateKey(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _json_loads(data: bytes) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("file is not UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, _DuplicateKey, ValueError, RecursionError) as exc:
        raise ValueError(f"invalid strict JSON: {exc}") from exc


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


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _valid_digest(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(ch in "0123456789abcdef" for ch in value[7:])
    )


def _read_bounded(path: Path, *, limit: int, label: str) -> bytes:
    try:
        if not path.is_file():
            raise ValueError(f"missing {label}: {path.name}")
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot inspect {label}: {exc}") from exc
    if size < 1 or size > limit:
        raise ValueError(f"{label} size must be between 1 and {limit} bytes")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read {label}: {exc}") from exc
    if len(data) != size:
        raise ValueError(f"{label} changed while it was read")
    return data


def _strict_events(path: Path) -> tuple[bytes, list[Event]]:
    raw = _read_bounded(path, limit=_MAX_EVENTS_BYTES, label="events.jsonl")
    if not raw.endswith(b"\n"):
        raise ValueError("events.jsonl has a torn final record")
    physical = raw.splitlines()
    if not physical or len(physical) > _MAX_LOGICAL_EVENTS:
        raise ValueError("events.jsonl physical record count is out of bounds")

    events: list[Event] = []
    for line_number, line in enumerate(physical, start=1):
        if not line or len(line) + 1 > MAX_EVENT_BATCH_BYTES:
            raise ValueError(f"events.jsonl record {line_number} is empty or oversized")
        decoded = _json_loads(line)
        if not isinstance(decoded, dict):
            raise ValueError(f"events.jsonl record {line_number} is not an object")
        # ``json.loads('1e999')`` produces infinity without invoking parse_constant.  Re-encoding with
        # allow_nan=False closes that less-obvious non-finite path before Pydantic sees the envelope.
        _canonical_json(decoded)
        try:
            members = decode_event_record(decoded, strict=True)
        except Exception as exc:  # Pydantic/batch decoder failures are all invalid gate evidence.
            raise ValueError(f"events.jsonl record {line_number} is invalid: {exc}") from exc
        if len(events) + len(members) > _MAX_LOGICAL_EVENTS:
            raise ValueError("events.jsonl logical event count is out of bounds")
        unknown = [event.type for event in members if event.type not in ALL_EVENT_TYPES]
        if unknown:
            raise ValueError(
                f"events.jsonl record {line_number} contains an unknown event type: "
                f"{unknown[0]}"
            )
        events.extend(members)

    for expected_seq, event in enumerate(events):
        if event.seq != expected_seq:
            raise ValueError(
                f"events.jsonl sequence is not contiguous at {expected_seq}: got {event.seq}")
    return raw, events


def _read_json_object(path: Path, *, limit: int, label: str) -> tuple[bytes, dict[str, Any]]:
    raw = _read_bounded(path, limit=limit, label=label)
    value = _json_loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one JSON object")
    # Re-encoding also rejects values outside the canonical JSON value domain.
    _canonical_json(value)
    return raw, value


def _resolved_run_dir(run_dir: str | Path) -> Path:
    raw = str(run_dir)
    if not raw or len(raw) > _MAX_PATH_CHARS:
        raise ValueError("run directory path is empty or oversized")
    try:
        resolved = Path(run_dir).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"run directory does not resolve: {exc}") from exc
    if not resolved.is_dir():
        raise ValueError("run directory is not a directory")
    if len(str(resolved)) > _MAX_PATH_CHARS:
        raise ValueError("resolved run directory path is oversized")
    return resolved


def _run_dir_identity(path: str) -> str:
    """Canonical comparison key (not a published path) for duplicate-directory rejection."""

    return os.path.normcase(os.path.realpath(path))


def _comparable_config(config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in config.items()
        if key not in _PAIR_VARIANT_CONFIG_FIELDS
    }


def _finite_metric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _required_finite(value: object, *, label: str) -> float:
    number = _finite_metric(value)
    if number is None:
        raise ValueError(f"{label} must be finite")
    return number


def _validate_cuda_probe_artifact(node: object) -> None:
    """Bind each accepted artifact/outcome to the source-owned CUDA allocation proof."""

    code = getattr(node, "code", None)
    if not isinstance(code, str) or not code.startswith(SPECULATION_CUDA_PROBE_CODE_PREFIX):
        raise ValueError("every calibration node must start with the exact CUDA proof prefix")
    metrics = getattr(node, "extra_metrics", None)
    if not isinstance(metrics, Mapping):
        raise ValueError("calibration node extra metrics must be a mapping")
    if getattr(node, "status", None) is not NodeStatus.evaluated:
        if metrics:
            raise ValueError("a non-evaluated calibration node must not claim CUDA proof metrics")
        return
    if set(metrics) != set(SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS):
        raise ValueError("evaluated calibration node lacks the exact CUDA proof metric schema")
    for key, expected in SPECULATION_CUDA_PROBE_STATIC_EXTRA_METRICS:
        if _finite_metric(metrics.get(key)) != float(expected):
            raise ValueError(f"evaluated calibration node has invalid CUDA proof metric {key}")
    device_count = _finite_metric(metrics.get(SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC))
    if (
        device_count is None
        or not device_count.is_integer()
        or device_count < 1
        # Every calibration Idea requests exactly one GPU.  The scheduler pins that one reservation
        # into the child process's CUDA_VISIBLE_DEVICES, so CUDA correctly reports one logical device
        # even when the parent run-start inventory contains several physical GPUs.
        or int(device_count) != 1
    ):
        raise ValueError("evaluated calibration node CUDA device count differs from its inventory")


def _bounded_card_id(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and value == value.strip()
        and value
        and len(value) <= 256
        and value.isprintable()
    )


def _validate_calibration_setup(
    events: Sequence[Event],
    started: Event,
    config: Mapping[str, Any],
    canonical_task: Mapping[str, Any],
) -> None:
    """Bind evidence to the exact fresh Toy setup and run-start authority writer."""

    expected_prefix = (
        EV_SETUP_STARTED,
        EV_SETUP_STEP,
        EV_RUN_STARTED,
        EV_SETUP_STEP,
        EV_SETUP_FINISHED,
    )
    if len(events) < len(expected_prefix) or tuple(
        event.type for event in events[:len(expected_prefix)]
    ) != expected_prefix:
        raise ValueError("calibration evidence lacks the exact fresh Toy setup prefix")
    if (
        events[2] is not started
        or sum(event.type == EV_SETUP_STARTED for event in events) != 1
        or sum(event.type == EV_SETUP_STEP for event in events) != 2
        or sum(event.type == EV_SETUP_FINISHED for event in events) != 1
    ):
        raise ValueError("calibration evidence setup lifecycle differs from the Toy writer")

    started_data = started.data or {}
    if not isinstance(started.data, dict) or set(started_data) != set(
        _CALIBRATION_RUN_STARTED_FIELDS
    ):
        raise ValueError("calibration run_started has a non-writer payload schema")
    if (
        started_data.get("goal") != canonical_task.get("goal")
        or started_data.get("task_id") != canonical_task.get("id")
        or started_data.get("direction") != canonical_task.get("direction")
    ):
        raise ValueError("calibration run_started task authority differs from the task snapshot")
    for field in RUN_START_PINNED_FIELDS:
        if field not in config or _canonical_json(started_data.get(field)) != _canonical_json(
            config[field]
        ):
            raise ValueError(
                f"calibration run_started {field} authority differs from config.snapshot.json"
            )
    if (
        started_data.get("trust_gate") != config.get("trust_gate")
        or started_data.get("select_verifier_contract") != VERIFIER_SELECTION_CONTRACT
        or not isinstance(started_data.get("env"), dict)
    ):
        raise ValueError("calibration run_started provenance/control authority is invalid")
    if started_data.get("workspace") != {}:
        raise ValueError("calibration fresh Toy workspace must be exactly empty")
    if started_data.get("dirty_inputs") != []:
        raise ValueError("calibration fresh Toy dirty_inputs must be exactly empty")

    # Mirror Engine._setup_phase/_setup_manifest rather than trusting a hand-authored config hash.
    try:
        import orjson
        from looplab.adapters.toytask import ToyTask

        task_model = ToyTask.model_validate(dict(canonical_task))
        task_payload = task_model.model_dump(mode="json")
        config_hash = hashlib.sha256(orjson.dumps(task_payload)).hexdigest()[:12]
        manifest_config_hash = hashlib.sha256(orjson.dumps(
            task_payload, option=orjson.OPT_SORT_KEYS,
        )).hexdigest()[:12]
        setup_manifest = hashlib.sha256(orjson.dumps(
            {
                "config": manifest_config_hash,
                "workspace": started_data["workspace"],
                "provenance": {},
            },
            option=orjson.OPT_SORT_KEYS,
        )).hexdigest()[:16]
    except Exception as exc:
        raise ValueError(f"calibration setup identity could not be reconstructed: {exc}") from exc
    if (
        _CONFIG_HASH_RE.fullmatch(str(started_data.get("config_hash", ""))) is None
        or started_data.get("config_hash") != config_hash
    ):
        raise ValueError("calibration run_started config_hash differs from the Toy writer")

    expected_started = {
        "phase": "task+data",
        "repo": False,
        "goal": canonical_task.get("goal"),
    }
    expected_workspace_step = {
        "step": "workspace fingerprint",
        "sources": list(started_data["workspace"]),
    }
    expected_agents_step = {"step": "wrote AGENTS.md"}
    finished_data = events[4].data or {}
    if events[0].data != expected_started:
        raise ValueError("calibration setup_started payload differs from the Toy writer")
    if events[1].data != expected_workspace_step or events[3].data != expected_agents_step:
        raise ValueError("calibration setup_step payload/order differs from the Toy writer")
    if (
        not isinstance(events[4].data, dict)
        or set(finished_data) != {"seconds", "manifest"}
        or _finite_metric(finished_data.get("seconds")) is None
        or float(finished_data["seconds"]) < 0.0
        or _SETUP_MANIFEST_RE.fullmatch(str(finished_data.get("manifest", ""))) is None
        or finished_data.get("manifest") != setup_manifest
    ):
        raise ValueError("calibration setup_finished payload differs from the Toy writer")


def _validate_calibration_event_envelope(events: Sequence[Event], state) -> None:
    """Admit only events and treatment receipts emitted by the exact offline protocol."""

    allowed = set(_CALIBRATION_COMMON_EVENT_TYPES)
    if getattr(state, "speculation_depth", 0) > 0:
        allowed.update(_CALIBRATION_TREATMENT_EVENT_TYPES)
    unexpected = sorted({event.type for event in events if event.type not in allowed})
    if unexpected:
        raise ValueError(
            "quality evidence contains an event outside the clean calibration protocol: "
            f"{unexpected[0]}"
        )

    open_card_head: tuple[str, int] | None = None
    for event in events:
        data = event.data or {}
        # Validate the raw writer schema before any folded Card/Node join.  A forbidden generation
        # alias can make replay ignore node_created; reporting the later enrichment as broken would
        # obscure the actual authority violation and make this exact raw check unreachable.
        if event.type == EV_NODE_CREATED and "generation" in data:
            raise ValueError(
                "quality evidence node_created must use the attempt-zero writer schema"
            )
        if event.type == EV_CARD_ENRICHED:
            if not isinstance(event.data, dict) or set(data) != {
                "id", "node_id", "generation", "proposal_ref", "footprint",
            }:
                raise ValueError("calibration card_enriched must be the exact footprint receipt")
            proposal_ref = data.get("proposal_ref")
            footprint = data.get("footprint")
            node_id = data.get("node_id")
            node = state.nodes.get(node_id) if type(node_id) is int else None
            if (
                not _bounded_card_id(data.get("id"))
                or node is None
                or node.attempt != 0
                or node.idea.card_id != data.get("id")
                or type(data.get("generation")) is not int
                or data["generation"] != 0
                or not isinstance(proposal_ref, dict)
                or set(proposal_ref) != {"v", "digest"}
                or proposal_ref.get("v") != 1
                or not isinstance(proposal_ref.get("digest"), str)
                or _IDEA_PROPOSAL_REF_RE.fullmatch(proposal_ref["digest"]) is None
                or footprint != {
                    "gpus": 1,
                    "proposed_by": "researcher",
                    "finalized_by": "developer",
                }
            ):
                raise ValueError("calibration card_enriched footprint receipt is invalid")
        elif event.type == EV_CARD_BUILD_REQUESTED:
            if (
                not isinstance(event.data, dict)
                or set(data) != {"card_id", "generation"}
                or not _bounded_card_id(data.get("card_id"))
                or type(data.get("generation")) is not int
                or data["generation"] != 0
            ):
                raise ValueError("calibration card_build_requested payload is invalid")
            if open_card_head is not None:
                raise ValueError(
                    "calibration Card-build queue opened a request before closing its current head"
                )
            open_card_head = (data["card_id"], data["generation"])
        elif event.type == EV_CARD_BUILD_DONE:
            committed = set(data) == {
                "card_id", "generation", "node_id", "speculative",
            }
            skipped = set(data) == {"card_id", "generation", "skipped"}
            if (
                not isinstance(event.data, dict)
                or not _bounded_card_id(data.get("card_id"))
                or type(data.get("generation")) is not int
                or data["generation"] != 0
                or (
                    committed
                    and (
                        type(data.get("node_id")) is not int
                        or data["node_id"] < 0
                        or data.get("speculative") is not True
                    )
                )
                or (skipped and data.get("skipped") not in {"stale", "producer_failed"})
                or not (committed or skipped)
            ):
                raise ValueError("calibration card_build_done payload is invalid")
            if open_card_head != (data["card_id"], data["generation"]):
                raise ValueError(
                    "calibration card_build_done does not exactly close its current request head"
                )
            open_card_head = None
    if open_card_head is not None:
        raise ValueError("quality evidence has an open or inconsistent Card-build queue")


def _validate_calibration_terminal(events: Sequence[Event], state) -> None:
    """Require the exact clean modern finalization suffix of the launch-only calibration path."""

    forbidden = sorted({
        event.type for event in events
        if event.type in _FORBIDDEN_CALIBRATION_LIFECYCLE_EVENTS
    })
    if forbidden:
        raise ValueError(
            "quality evidence contains a forbidden calibration lifecycle event: "
            f"{forbidden[0]}"
        )
    _validate_calibration_event_envelope(events, state)

    finishes = [event for event in events if event.type == EV_RUN_FINISHED]
    if len(finishes) != 1:
        raise ValueError("quality evidence requires exactly one raw accepted run_finished")
    finish = finishes[0]
    finish_data = finish.data or {}
    if "reason" in finish_data:
        raise ValueError("quality evidence has a non-qualifying terminal reason")
    if finish_data.get("finalization_required") is not True:
        raise ValueError("quality evidence requires modern finalization")
    if (
        finish.seq is None
        or finish.seq != getattr(state, "last_finish_seq", -1)
        or type(finish_data.get("after_seq")) is not int
        or finish_data["after_seq"] != finish.seq - 1
    ):
        raise ValueError("quality evidence run_finished is not the accepted adjacent finish")
    scope = finish_data.get("finalize_scope")
    if not isinstance(scope, str) or _FINALIZE_SCOPE_RE.fullmatch(scope) is None:
        raise ValueError("quality evidence run_finished lacks the exact finalization scope")

    finalize_steps = [event for event in events if event.type == EV_FINALIZE_STEP]
    scoped_steps = [
        event for event in finalize_steps
        if isinstance(event.data, dict) and event.data.get("scope") == scope
    ]
    if len(scoped_steps) != len(finalize_steps):
        raise ValueError("quality evidence contains a foreign finalization scope")
    begun = [event for event in scoped_steps if event.data.get("step") == "begun"]
    complete = [event for event in scoped_steps if event.data.get("step") == "complete"]
    abandoned = [event for event in scoped_steps if event.data.get("step") == "abandoned"]
    if len(begun) != 1 or len(complete) != 1 or abandoned:
        raise ValueError("quality evidence lacks one complete un-abandoned finalization scope")
    begun_data = begun[0].data
    if (
        begun[0].seq is None
        or type(begun_data.get("after_seq")) is not int
        or begun_data["after_seq"] != begun[0].seq - 1
        or finish_data["after_seq"] != begun[0].seq
        or begun_data.get("finish_report_planned") is not False
    ):
        raise ValueError("quality evidence has an invalid calibration finalization claim")
    if begun_data.get("finish_data") != {}:
        raise ValueError("quality evidence finalization intent differs from run_finished")

    acknowledgements = [
        event for event in events if event.type == EV_FINALIZATION_FINISHED
    ]
    if (
        len(acknowledgements) != 1
        or (acknowledgements[0].data or {}).get("finish_seq") != finish.seq
        or getattr(state, "finalized_finish_seq", -1) != finish.seq
        or state.finalization_pending()
    ):
        raise ValueError("quality evidence has incomplete modern finalization")
    # A complete marker must close the sole scope.  The helper additionally catches any newer
    # incomplete scope that could otherwise be hidden behind the folded terminal state.
    from looplab.engine.finalize import incomplete_finalize_scope
    if incomplete_finalize_scope(events) is not None:
        raise ValueError("quality evidence retains a pending finalization scope")

    expected_types = (
        EV_FINALIZE_STEP,
        EV_RUN_FINISHED,
        EV_BUDGET,
        EV_FINALIZE_STEP,
        EV_DIVERSITY_ARCHIVE,
        EV_FINALIZE_STEP,
        EV_FINALIZE_STEP,
        EV_FINALIZE_STEP,
        EV_FINALIZE_STEP,
        EV_FINALIZE_STEP,
        EV_FINALIZATION_FINISHED,
        EV_FINALIZE_STEP,
    )
    if finish.seq < 1 or len(events) != finish.seq + 11:
        raise ValueError("quality evidence lacks the exact terminal finalization suffix")
    suffix = events[finish.seq - 1:]
    if tuple(event.type for event in suffix) != expected_types:
        raise ValueError("quality evidence terminal finalization order differs")

    expected_begun = {
        "scope": scope,
        "step": "begun",
        "finish_data": {},
        "finish_report_planned": False,
        "after_seq": finish.seq - 2,
    }
    expected_finish = {
        "after_seq": finish.seq - 1,
        "finalization_required": True,
        "finalize_scope": scope,
    }
    if suffix[0].data != expected_begun or suffix[1].data != expected_finish:
        raise ValueError("quality evidence terminal intent/finish payload differs")

    budget = suffix[2].data or {}
    expected_eval_s = round(float(getattr(state, "total_eval_seconds", 0.0)), 3)
    if (
        not isinstance(suffix[2].data, dict)
        or set(budget) != {
            "elapsed_s", "eval_s", "nodes", "finalize_scope", "finish_seq",
        }
        or _finite_metric(budget.get("elapsed_s")) is None
        or float(budget["elapsed_s"]) < 0.0
        or _finite_metric(budget.get("eval_s")) != expected_eval_s
        or type(budget.get("nodes")) is not int
        or budget["nodes"] != len(state.nodes)
        or budget.get("finalize_scope") != scope
        or budget.get("finish_seq") != finish.seq
    ):
        raise ValueError("quality evidence budget finalization receipt differs from folded state")
    if suffix[3].data != {"scope": scope, "step": "budget"}:
        raise ValueError("quality evidence budget finalization marker differs")

    from looplab.search.archive import DiversityArchive
    expected_archive = {
        **DiversityArchive(1.0).summary(state),
        "finalize_scope": scope,
        "finish_seq": finish.seq,
    }
    if suffix[4].data != expected_archive:
        raise ValueError("quality evidence diversity finalization receipt differs from folded state")
    expected_tail_data = (
        {"scope": scope, "step": "diversity"},
        {"scope": scope, "step": "case"},
        {"scope": scope, "step": "reflection_begun", "outcome": "disabled"},
        {"scope": scope, "step": "reflection", "outcome": "disabled"},
        {"scope": scope, "step": "llm_cost"},
        {"finish_seq": finish.seq},
        {"scope": scope, "step": "complete"},
    )
    if tuple(event.data for event in suffix[5:]) != expected_tail_data:
        raise ValueError("quality evidence terminal finalization checklist differs")


def _raw_node_lifecycle(events: Sequence[Event], state) -> None:
    """Reject ignored, duplicated, reset, or cross-generation candidate lifecycle rows."""

    building: dict[int, int] = {}
    created: dict[int, int] = {}
    terminal: dict[int, int] = {}
    building_seq: dict[int, int] = {}
    created_seq: dict[int, int] = {}
    node_ids = set(state.nodes)
    for event in events:
        if event.type not in {
            EV_NODE_BUILDING, EV_NODE_CREATED, EV_NODE_EVALUATED, EV_NODE_FAILED,
        }:
            continue
        data = event.data or {}
        node_id = data.get("node_id")
        if type(node_id) is not int or node_id not in node_ids:
            raise ValueError(
                "quality evidence contains an ignored or cross-generation node lifecycle row")
        if event.type == EV_NODE_BUILDING:
            node = state.nodes[node_id]
            card_id = getattr(getattr(node, "idea", None), "card_id", None)
            if not _bounded_card_id(card_id):
                raise ValueError(
                    "every calibration node_building requires its exact native Card owner")
            expected_building: dict[str, Any] = {
                "node_id": node_id,
                "operator": node.operator,
                "parent_ids": list(node.parent_ids),
                "card_id": card_id,
            }
            if node.speculative is True:
                expected_building.update({
                    "speculative": True,
                    "card_build_generation": node.card_build_generation,
                })
            if (
                not isinstance(event.data, dict)
                or _canonical_json(data) != _canonical_json(expected_building)
                or type(event.seq) is not int
            ):
                raise ValueError(
                    "calibration node_building payload differs from its accepted node_created")
            building[node_id] = building.get(node_id, 0) + 1
            building_seq[node_id] = event.seq
            continue
        if event.type == EV_NODE_CREATED:
            if "generation" in data:
                raise ValueError(
                    "quality evidence node_created must use the attempt-zero writer schema")
            if type(event.seq) is not int:
                raise ValueError("calibration node_created lacks a physical sequence")
            created_seq[node_id] = event.seq
        elif type(data.get("generation")) is not int or data["generation"] != 0:
            raise ValueError(
                "quality evidence contains an ignored or cross-generation node lifecycle row")
        counts = created if event.type == EV_NODE_CREATED else terminal
        counts[node_id] = counts.get(node_id, 0) + 1
    if any(building.get(node_id, 0) != 1 for node_id in node_ids):
        raise ValueError("every calibration node requires exactly one matching node_building")
    if any(created.get(node_id, 0) != 1 for node_id in node_ids):
        raise ValueError("every calibration node requires exactly one accepted node_created")
    if any(terminal.get(node_id, 0) != 1 for node_id in node_ids):
        raise ValueError("every calibration node requires exactly one terminal outcome")
    if any(building_seq[node_id] >= created_seq[node_id] for node_id in node_ids):
        raise ValueError("every calibration node_building must precede its node_created")


def _canonical_calibration_policy(max_nodes: int) -> GreedyTree:
    """Reconstruct the exact source-owned policy pinned by the calibration profile."""

    return GreedyTree(
        n_seeds=len(SPECULATION_CALIBRATION_SEEDS),
        max_nodes=max_nodes,
        debug_depth=1,
        enable_merge=True,
        merge_every=3,
        max_merges=2,
        ablate_every=0,
        operator_bandit=False,
    )


def _calibration_action_shape(action: Mapping[str, Any]) -> tuple[str, tuple[int, ...]]:
    """Project one exact Greedy creation macro, rejecting ambiguous parent spellings."""

    kind = action.get("kind", action.get("operator"))
    parent_id = action.get("parent_id")
    raw_parents = action.get("parent_ids", [])
    if not isinstance(raw_parents, list) or any(type(parent) is not int for parent in raw_parents):
        raise ValueError("calibration action has a malformed parent list")
    parents = tuple(raw_parents)
    if kind == "draft":
        if parent_id is not None or parents:
            raise ValueError("calibration draft action must be parentless")
    elif kind in {"improve", "debug"}:
        if type(parent_id) is not int:
            raise ValueError("calibration single-parent action lacks an exact parent")
        if parents and parents != (parent_id,):
            raise ValueError("calibration single-parent action has ambiguous parents")
        parents = (parent_id,)
    elif kind == "merge":
        if len(parents) != 2 or (parent_id is not None and parent_id != parents[0]):
            raise ValueError("calibration merge action lacks its ordered top-two parents")
    else:
        raise ValueError("calibration Card is outside the canonical Greedy creation vocabulary")
    return kind, parents


def _raw_node_ceiling(events: Sequence[Event], state) -> int:
    """Mirror Engine's monotonic physical reservation denominator on one event prefix."""

    building_max = max(
        (
            event.data.get("node_id", -1)
            for event in events
            if event.type == EV_NODE_BUILDING
            and isinstance(event.data, Mapping)
            and type(event.data.get("node_id")) is int
        ),
        default=-1,
    )
    return max(max(state.nodes, default=-1), building_max) + 1


def _validate_calibration_greedy_authority(
    events: Sequence[Event],
    state,
    *,
    max_nodes: int,
    gpu_inventory: Sequence[Mapping[str, Any]],
) -> None:
    """Recompute every Card stage/request from its immediate canonical Greedy prefix.

    Card receipts prove that a Node consumed the recorded proposal; they do not prove that Greedy
    selected that proposal.  This pass supplies that missing authority without trusting the optional
    ``policy_decision`` audit row.
    """

    envelope = CardResourceEnvelope(
        gpu_count=len(gpu_inventory),
        gpu_memory_mib=tuple(row["mem_total_mib"] for row in gpu_inventory),
    )
    depth = getattr(state, "speculation_depth", 0)
    cursor = FoldCursor()
    prefix_events: list[Event] = []
    baseline_stages = 0

    for event in events:
        prefix = cursor.snapshot()
        if event.type == EV_CARD_ADDED:
            data = event.data if isinstance(event.data, Mapping) else {}
            idea = data.get("idea")
            if not isinstance(idea, Mapping):
                raise ValueError("calibration Card stage lacks an exact action")
            actual = _calibration_action_shape({
                "operator": idea.get("operator"),
                "parent_id": data.get("parent_id"),
                "parent_ids": data.get("parent_ids"),
            })
            raw_ceiling = _raw_node_ceiling(prefix_events, prefix)
            expected_generations = {
                str(parent): prefix.nodes[parent].attempt
                for parent in actual[1]
                if parent in prefix.nodes
            }
            if (
                data.get("at_node") != raw_ceiling
                or len(expected_generations) != len(actual[1])
                or data.get("parent_generations") != expected_generations
            ):
                raise ValueError(
                    "calibration Card stage differs from its immediate physical/parent prefix"
                )

            policy = _canonical_calibration_policy(max_nodes)
            if depth == 0:
                # Greedy authorizes the initial seed batch in one decision.  Serial Card/Node commits
                # make seeds two and three observe an earlier pending sibling, so their immediate
                # prefixes cannot independently reproduce that already-authorized batch.
                if baseline_stages < min(len(SPECULATION_CALIBRATION_SEEDS), max_nodes):
                    expected = ("draft", ())
                else:
                    actions = policy.next_actions(prefix)
                    if len(actions) != 1:
                        raise ValueError(
                            "baseline Card stage lacks one canonical Greedy action"
                        )
                    expected = _calibration_action_shape(actions[0])
                baseline_stages += 1
            else:
                if prefix.card_builds_done != len(prefix.card_build_requests):
                    raise ValueError("treatment staged a Card while a build request was open")
                pending = list(prefix.pending_nodes())
                excluded = {
                    node.idea.card_id for node in pending
                    if isinstance(node.idea.card_id, str)
                }
                live_max = card_budget_used(prefix) + max(
                    0, max_nodes - raw_ceiling,
                )
                try:
                    actions = speculative_raw_actions(
                        prefix,
                        _canonical_calibration_policy(live_max),
                        live_max,
                        scoring=None,
                        excluded_card_ids=excluded,
                        ignored_pending_node_ids={node.id for node in pending},
                        resource_envelope=envelope,
                    )
                except Exception as exc:
                    raise ValueError(
                        "treatment Card stage could not reproduce canonical Greedy authority"
                    ) from exc
                if not actions:
                    raise ValueError("treatment Card stage lacks canonical Greedy authority")
                expected = _calibration_action_shape(actions[0])
            if actual != expected:
                raise ValueError(
                    "calibration Card action/operator/parents differ from canonical Greedy"
                )

        if event.type == EV_CARD_BUILD_REQUESTED and depth > 0:
            data = event.data if isinstance(event.data, Mapping) else {}
            card_id = data.get("card_id")
            pending = list(prefix.pending_nodes())
            excluded = {
                node.idea.card_id for node in pending
                if isinstance(node.idea.card_id, str)
            }
            if prefix.card_builds_done != len(prefix.card_build_requests):
                raise ValueError("treatment requested a Card while another request was open")
            raw_ceiling = _raw_node_ceiling(prefix_events, prefix)
            live_max = card_budget_used(prefix) + max(0, max_nodes - raw_ceiling)
            try:
                actions = speculative_card_actions(
                    prefix,
                    _canonical_calibration_policy(live_max),
                    live_max,
                    scoring=None,
                    excluded_card_ids=excluded,
                    ignored_pending_node_ids={node.id for node in pending},
                    resource_envelope=envelope,
                )
            except Exception as exc:
                raise ValueError(
                    "treatment request could not reproduce canonical Card election"
                ) from exc
            if not actions or actions[0].get(META_CARD_ID) != card_id:
                raise ValueError("treatment request is not the canonical Greedy Card head")

        cursor.extend((event,))
        prefix_events.append(event)


def _calibration_staged_proposal_ref(data: Mapping[str, Any], node) -> dict | None:
    """Reconstruct the exact thin proposal before Card materialization.

    A staged Card deliberately carries no executable hypothesis body.  The positive-depth claim
    reconstructs that display join as ``hypothesis=card.seed_statement``; ``card_enriched`` binds the
    resulting materialized Idea separately.  Treating those two phase identities as byte-identical
    rejects the real writer, while accepting an arbitrary staged digest would weaken the source
    receipt.  The calibrated Toy path changes only this one field, so invert that transition exactly.
    """

    idea = getattr(node, "idea", None)
    if idea is None or getattr(idea, "card_id", None) != data.get("id"):
        return None
    hypothesis = getattr(idea, "hypothesis", None)
    if hypothesis is not None and hypothesis != data.get("statement"):
        return None
    try:
        staged = idea.model_copy(deep=True, update={"hypothesis": None})
    except Exception:
        return None
    rationale = staged.rationale.strip() if isinstance(staged.rationale, str) else ""
    statement = rationale or f"{staged.operator} experiment"
    if statement != data.get("statement"):
        return None
    return idea_proposal_ref(staged)


def _validate_calibration_card_owners(
    events: Sequence[Event],
    state,
    requests: Sequence[Mapping[str, Any]],
    outcomes: Sequence[str],
) -> None:
    """Require one native, receipt-bound Card registration for every admitted work owner."""

    registrations = [event for event in events if event.type == EV_CARD_ADDED]
    by_card: dict[str, Event] = {}
    nodes_by_card: dict[str, Any] = {}
    for node in state.nodes.values():
        card_id = getattr(getattr(node, "idea", None), "card_id", None)
        if not _bounded_card_id(card_id) or card_id in nodes_by_card:
            raise ValueError("calibration nodes require unique native Card owners")
        nodes_by_card[card_id] = node

    score_authority: dict[int, tuple[int | None, int | None, bool]] = {}
    cursor = FoldCursor()
    for event in events:
        if event.type == EV_CARD_ADDED:
            prefix = cursor.snapshot()
            score_id = prefix.best_node_id
            if score_id is None:
                score_authority[event.seq] = (None, None, True)
            else:
                scored_node = prefix.nodes.get(score_id)
                if (
                    type(score_id) is not int
                    or scored_node is None
                    or scored_node.tombstoned
                    or score_id in prefix.aborted_nodes
                ):
                    raise ValueError(
                        "calibration card_added has invalid immediate-prefix score authority"
                    )
                score_authority[event.seq] = (score_id, scored_node.attempt, False)
        cursor.extend((event,))

    for event in registrations:
        data = event.data or {}
        card_id = data.get("id")
        idea = data.get("idea")
        if (
            not isinstance(event.data, dict)
            or set(data) != set(_CALIBRATION_CARD_ADDED_FIELDS)
            or not _bounded_card_id(card_id)
            or card_id in by_card
            or not isinstance(idea, dict)
            or set(idea) != set(_CALIBRATION_CARD_IDEA_FIELDS)
        ):
            raise ValueError("calibration card_added is not one exact native registration")
        action = {
            field: (idea[field] if field in _CALIBRATION_CARD_IDEA_FIELDS else data[field])
            for field in CARD_ACTION_DIGEST_V1_FIELDS
        }
        expected_receipt = card_ownership_receipt(card_id, data.get("statement"), action)
        proposal_ref = data.get("proposal_ref")
        expected_source = "engine" if idea.get("operator") == "merge" else "researcher"
        expected_score = score_authority.get(event.seq)
        if (
            data.get("source") != expected_source
            or type(data.get("at_node")) is not int
            or not 0 <= data["at_node"] <= (1 << 31) - 1
            or not isinstance(data.get("rationale"), str)
            or len(data["rationale"]) > 400
            or not isinstance(data.get("steering_context"), list)
            or expected_receipt is None
            or data.get("ownership_receipt") != expected_receipt
            or not isinstance(proposal_ref, dict)
            or set(proposal_ref) != {"v", "digest"}
            or proposal_ref.get("v") != 1
            or not isinstance(proposal_ref.get("digest"), str)
            or _IDEA_PROPOSAL_REF_RE.fullmatch(proposal_ref["digest"]) is None
        ):
            raise ValueError("calibration card_added ownership/proposal receipt is invalid")
        if expected_score is None or (
            data.get("scored_against"),
            data.get("scored_against_generation"),
            data.get("scored_against_empty"),
        ) != expected_score:
            raise ValueError(
                "calibration card_added score authority differs from its immediate event prefix"
            )
        card = state.cards.get(card_id)
        identity = getattr(card, "identity", None) if card is not None else None
        if (
            card is None
            or getattr(identity, "kind", None) != "native"
            or getattr(identity, "durable", None) is not True
            or getattr(identity, "receipt_valid", None) is not True
            or getattr(identity, "action_digest", None)
            != expected_receipt["action_digest"]
            or card.seed_statement != data.get("statement")
            or card.source != expected_source
            or card.created_at_node != data.get("at_node")
            or (
                card.scored_against,
                card.scored_against_generation,
                card.scored_against_empty,
            ) != expected_score
            or _canonical_json(card.steering_context)
            != _canonical_json(data["steering_context"])
        ):
            raise ValueError("calibration card_added does not fold to one native Card identity")

        node = nodes_by_card.get(card_id)
        if node is not None:
            node_idea = node.idea.model_dump(mode="json")
            expected_idea = {
                field: node_idea.get(field) for field in _CALIBRATION_CARD_IDEA_FIELDS
            }
            statement = (
                node.idea.hypothesis.strip()
                if isinstance(node.idea.hypothesis, str) and node.idea.hypothesis.strip()
                else node.idea.rationale.strip()
                if isinstance(node.idea.rationale, str) and node.idea.rationale.strip()
                else f"{node.idea.operator} experiment"
            )
            expected_parents = list(node.parent_ids)
            if (
                _canonical_json(idea) != _canonical_json(expected_idea)
                or data.get("statement") != statement
                or data.get("rationale") != (node.idea.rationale or "")[:400]
                or data.get("at_node") != node.id
                or data.get("parent_id")
                != (expected_parents[0] if expected_parents else None)
                or data.get("parent_ids") != expected_parents
                or data.get("parent_generations")
                != {str(parent): 0 for parent in expected_parents}
                or _canonical_json(data.get("footprint"))
                != _canonical_json(node_idea.get("footprint"))
                or proposal_ref != _calibration_staged_proposal_ref(data, node)
            ):
                raise ValueError(
                    "calibration card_added does not join its materialized node action")
        by_card[card_id] = event

    card_ids = list(by_card)
    building_by_node = {
        (event.data or {}).get("node_id"): event
        for event in events if event.type == EV_NODE_BUILDING
    }
    for card_id, node in nodes_by_card.items():
        registration = by_card.get(card_id)
        building = building_by_node.get(node.id)
        if (
            registration is None
            or building is None
            or type(registration.seq) is not int
            or type(building.seq) is not int
            or registration.seq >= building.seq
        ):
            raise ValueError("every calibration node requires one prior native card_added")

    enrichments = [event for event in events if event.type == EV_CARD_ENRICHED]
    created_by_node = {
        (event.data or {}).get("node_id"): event
        for event in events if event.type == EV_NODE_CREATED
    }
    enriched_cards: set[str] = set()
    for event in enrichments:
        data = event.data or {}
        card_id = data.get("id")
        node = nodes_by_card.get(card_id)
        footprint = (
            normalize_researcher_footprint(node.idea.footprint)
            if node is not None else None
        )
        expected = ({
            "id": card_id,
            "node_id": node.id,
            "generation": node.attempt,
            "proposal_ref": idea_proposal_ref(node.idea),
            "footprint": {
                **footprint,
                "proposed_by": "researcher",
                "finalized_by": "developer",
            },
        } if node is not None and footprint is not None else None)
        created_event = created_by_node.get(node.id) if node is not None else None
        if (
            expected is None
            or card_id in enriched_cards
            or not isinstance(event.data, dict)
            or _canonical_json(data) != _canonical_json(expected)
            or created_event is None
            or type(event.seq) is not int
            or type(created_event.seq) is not int
            or event.seq <= created_event.seq
        ):
            raise ValueError(
                "calibration card_enriched does not exactly join its finalized node footprint"
            )
        enriched_cards.add(card_id)
    if enriched_cards != set(nodes_by_card) or len(enrichments) != len(nodes_by_card):
        raise ValueError(
            "every calibration node requires exactly one matching footprint card_enriched"
        )

    depth = getattr(state, "speculation_depth", 0)
    if depth == 0:
        if set(card_ids) != set(nodes_by_card) or len(card_ids) != len(nodes_by_card):
            raise ValueError("baseline card_added registrations must map one-to-one to nodes")
        return

    request_ids = [request.get("card_id") for request in requests]
    if (
        len(request_ids) != len(set(request_ids))
        or request_ids != card_ids
        or len(outcomes) != len(request_ids)
    ):
        raise ValueError(
            "treatment card_added registrations must map one-to-one to its request ledger")
    raw_requests = [event for event in events if event.type == EV_CARD_BUILD_REQUESTED]
    for registration, request in zip(registrations, raw_requests):
        if (
            type(registration.seq) is not int
            or type(request.seq) is not int
            or registration.seq >= request.seq
        ):
            raise ValueError("treatment card_added must precede its exact build request")

    linked_by_card = {
        link.get("card_id"): node_id
        for node_id, link in getattr(state, "speculative_nodes", {}).items()
        if isinstance(link, Mapping)
    }
    if set(linked_by_card) != set(nodes_by_card):
        raise ValueError("every treatment node must be owned by one committed Card request")
    for card_id, outcome in zip(request_ids, outcomes):
        if outcome == "committed":
            node = nodes_by_card.get(card_id)
            if node is None or linked_by_card.get(card_id) != node.id:
                raise ValueError("committed Card request does not join its accepted node")
        elif outcome == "stale":
            if card_id in nodes_by_card or card_id in linked_by_card:
                raise ValueError("stale Card request must not own an accepted node")
        else:
            raise ValueError("treatment Card registration has a non-qualifying outcome")


def _material_digest(value: object) -> str:
    """Bound a folded provenance object into the receipt without duplicating its raw contents."""

    return _sha256(_canonical_json(value))


def speculation_task_profile_digest(task: object) -> str:
    """Digest the exact admitted calibration workload while excluding only replicate seed."""

    canonical = canonical_speculation_toy_task(task)
    profile = {key: value for key, value in canonical.items() if key != "seed"}
    return _sha256(_canonical_json({
        "schema": "looplab.speculation-task-profile/v1",
        "task": profile,
    }))


def speculation_environment_fingerprint() -> dict[str, Any]:
    """Current interpreter/platform/key-library identity, matching Engine's run-start pin."""

    env: dict[str, Any] = {"python": sys.version.split()[0], "platform": platform.platform()}
    libs: dict[str, str] = {}
    try:
        from importlib.metadata import PackageNotFoundError, version
        direct_packages = (
            "pydantic", "pydantic-settings", "orjson", "anyio", "typer", "PyYAML",
            "openai", "httpx",
        )
        optional_packages = (
            "numpy", "pandas", "scikit-learn", "scipy", "torch", "xgboost",
            "lightgbm", "tensorflow", "transformers",
        )
        # Missing direct dependencies are part of the identity too.  This keeps a partially broken
        # environment from sharing the digest of the intended runtime and makes a later repair visible.
        for package in direct_packages:
            try:
                libs[package] = version(package)
            except PackageNotFoundError:
                libs[package] = "<missing>"
            except Exception:
                libs[package] = "<unavailable>"
        for package in optional_packages:
            try:
                libs[package] = version(package)
            except PackageNotFoundError:
                pass
            except Exception:
                pass
    except Exception:
        pass
    if libs:
        env["libs"] = libs
    return env


def _environment_digest(value: object) -> str:
    if callable(value):
        value = value()
    if not isinstance(value, Mapping):
        raise ValueError("environment fingerprint must be a mapping")
    return _material_digest(dict(value))


def _coverage_trajectory(state) -> tuple[list[dict[str, int]], int]:
    projection = current_concept_projection(state)
    trusted = projection.trusted_memberships
    evaluated = [
        node
        for node in state.nodes.values()
        if node.status is NodeStatus.evaluated and not node.tombstoned
    ]
    evaluated.sort(key=lambda node: (
        node.terminal_event_seq if type(node.terminal_event_seq) is int else (1 << 63) - 1,
        node.id,
    ))
    if len(evaluated) + 1 > _MAX_TRAJECTORY_POINTS:
        raise ValueError("trusted concept coverage trajectory is oversized")
    covered: set[str] = set()
    trajectory = [{"evaluated": 0, "coverage": 0}]
    for index, node in enumerate(evaluated, start=1):
        covered.update(trusted.get(node.id, ()))
        trajectory.append({"evaluated": index, "coverage": len(covered)})
    return trajectory, len(covered)


def _semantic_execution_trajectory_digest(state) -> str:
    """Digest scientific execution state without incidental event-envelope identity.

    Raw source digests still bind every byte.  This second identity deliberately ignores run ids,
    replicate seeds, event timestamps/traces, diagnostic rows and wall-clock/stdout noise so none of
    those can make a copied lane look like independent evidence.  It retains candidate artifacts,
    terminal outcomes and the accepted speculative queue.  Card ids are canonicalized by first
    appearance because they are opaque per-run identities, while their joins and ordering remain
    comparison-relevant.
    """

    card_aliases: dict[str, str] = {}

    def card_alias(value: object) -> object:
        if not isinstance(value, str):
            return value
        if value not in card_aliases:
            card_aliases[value] = f"card-{len(card_aliases)}"
        return card_aliases[value]

    # Establish aliases in deterministic candidate order before the queue projection.  This makes a
    # per-run card-id rename inert while keeping every node/request relationship exact.
    ordered_nodes = [state.nodes[node_id] for node_id in sorted(state.nodes)]
    for node in ordered_nodes:
        card_alias(node.idea.card_id)
    for request in getattr(state, "card_build_requests", ()):
        if isinstance(request, Mapping):
            card_alias(request.get("card_id"))

    candidates: list[dict[str, Any]] = []
    for node in ordered_nodes:
        idea = node.idea.model_dump(mode="json", exclude_none=True)
        if "card_id" in idea:
            idea["card_id"] = card_alias(idea["card_id"])
        candidates.append({
            "node_id": node.id,
            "generation": node.attempt,
            "parent_ids": list(node.parent_ids),
            "operator": node.operator,
            "idea_sha256": _material_digest(idea),
            "artifact_sha256": _material_digest({
                "code": node.code,
                "files": node.files,
                "deleted": node.deleted,
            }),
            "speculative": node.speculative is True,
            "card_build_generation": node.card_build_generation,
            "terminal": {
                "status": node.status.value,
                "metric": node.metric,
                "extra_metrics": node.extra_metrics,
                "violations": node.violations,
                "feasible": node.feasible,
                "freshness_dropped": bool(
                    node.status is NodeStatus.failed
                    and node.speculative is True
                    and node.error_reason == "superseded"
                    and node.error == CARD_FRESHNESS_SUPERSEDED_ERROR
                    and _finite_metric(node.eval_seconds) == 0.0
                ),
            },
        })

    requests = list(getattr(state, "card_build_requests", ()))
    outcomes = list(getattr(state, "card_build_outcomes", ()))
    queue = [
        {
            "index": index,
            "card": card_alias(request.get("card_id")),
            "generation": request.get("generation"),
            "outcome": outcomes[index] if index < len(outcomes) else None,
        }
        for index, request in enumerate(requests)
    ]
    links = [
        {
            "node_id": node_id,
            "card": card_alias(link.get("card_id")),
            "generation": link.get("generation"),
        }
        for node_id, link in sorted(getattr(state, "speculative_nodes", {}).items())
    ]
    return _material_digest({
        "schema": "looplab.speculation-semantic-trajectory/v1",
        "protocol": {
            "card_driven_selection": getattr(state, "card_driven_selection", None),
            "speculation_depth": getattr(state, "speculation_depth", None),
        },
        "candidates": candidates,
        "card_queue": queue,
        "speculative_links": links,
    })


def _analyze_speculation_run(run_dir: str | Path) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    resolved = _resolved_run_dir(run_dir)
    events_raw, events = _strict_events(resolved / "events.jsonl")
    config_raw, config = _read_json_object(
        resolved / "config.snapshot.json", limit=_MAX_CONFIG_BYTES, label="config.snapshot.json")
    task_raw, task = _read_json_object(
        resolved / "task.snapshot.json", limit=_MAX_TASK_BYTES, label="task.snapshot.json")
    started = [event for event in events if event.type == "run_started"]
    if len(started) != 1 or not isinstance(started[0].data, dict):
        raise ValueError("evidence requires exactly one valid run_started event")
    try:
        state = fold(events)
    except Exception as exc:
        raise ValueError(f"events do not fold: {exc}") from exc

    direction = getattr(state, "direction", None)
    if direction not in {"min", "max"}:
        raise ValueError("folded run direction must be min or max")
    run_id = getattr(state, "run_id", "")
    task_id = getattr(state, "task_id", "")
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 256:
        raise ValueError("folded run_id must be nonempty and bounded")
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 256:
        raise ValueError("folded task_id must be nonempty and bounded")
    if run_id != resolved.name:
        raise ValueError("folded run_id must exactly equal the resolved run directory name")
    if started[0].data.get("run_id") != run_id:
        raise ValueError("run_started identity does not match the folded run")
    if task.get("id") != task_id:
        raise ValueError("task snapshot id differs from run_started task_id")
    if task.get("direction") != direction:
        raise ValueError("task snapshot direction differs from run_started direction")
    if getattr(state, "workspace_changed", False) is True:
        raise ValueError("run continued after workspace drift")
    if getattr(state, "env_changed", False) is True:
        raise ValueError("run continued after environment drift")
    try:
        canonical_task = canonical_speculation_toy_task(task, require_seed_set=True)
    except ValueError as exc:
        raise ValueError(f"invalid canonical calibration task: {exc}") from exc

    # Calibration is a purpose-built, offline Greedy/Toy measurement protocol.  A copied ordinary
    # run with hand-edited snapshots must not become receipt evidence merely because it has Card
    # events.  Import lazily to avoid making read-only replay import the full Engine at module load.
    from looplab.engine.orchestrator import (
        SPECULATION_CALIBRATION_PROFILE_DIGEST,
        SPECULATION_CALIBRATION_PROFILE_SETTINGS,
    )
    expected_config_fields = (
        set(SPECULATION_CALIBRATION_PROFILE_SETTINGS)
        | set(SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS)
    )
    if set(config) != expected_config_fields:
        missing = sorted(expected_config_fields - set(config))
        extra = sorted(set(config) - expected_config_fields)
        raise ValueError(
            f"config fields differ from the exact calibration snapshot "
            f"(missing={missing}, extra={extra})"
        )
    if config.get("speculation_gate_receipt") is not None:
        raise ValueError("config.speculation_gate_receipt must be null in fresh calibration evidence")
    max_nodes = config.get("max_nodes")
    if type(max_nodes) is not int or not 1 <= max_nodes <= 64:
        raise ValueError("config.max_nodes must be an integer in 1..64")
    card_driven = config.get("card_driven_selection")
    if type(card_driven) is not bool:
        raise ValueError("config.card_driven_selection must be boolean")
    speculation_depth = config.get("speculation_depth")
    if type(speculation_depth) is not int or not 0 <= speculation_depth <= 64:
        raise ValueError("config.speculation_depth must be an integer in 0..64")
    # The snapshot describes intent; run_started is the replay authority for what selection/execution
    # actually ran.  A gate must not accept a treatment whose snapshot was edited after the fact.
    if getattr(state, "card_driven_selection", None) is not card_driven:
        raise ValueError("config and folded card_driven_selection differ")
    if getattr(state, "speculation_depth", None) != speculation_depth:
        raise ValueError("config and folded speculation_depth differ")
    implementation_digest = getattr(state, "speculation_implementation_digest", "")
    if not _valid_digest(implementation_digest):
        raise ValueError("run lacks a valid run-start speculation implementation digest")
    profile_digest = getattr(state, "speculation_calibration_profile_digest", "")
    if profile_digest != SPECULATION_CALIBRATION_PROFILE_DIGEST:
        raise ValueError("run lacks the exact source-owned calibration profile digest")
    for key, expected in SPECULATION_CALIBRATION_PROFILE_SETTINGS.items():
        if config.get(key) != expected:
            raise ValueError(f"config.{key} is outside the immutable calibration profile")
    calibration_seed = getattr(state, "speculation_calibration_seed", None)
    if type(calibration_seed) is not int or calibration_seed != canonical_task["seed"]:
        raise ValueError("run-start calibration seed differs from the task snapshot")
    policy_scope = getattr(state, "speculation_policy_scope", "")
    if policy_scope != SPECULATION_POLICY_SCOPE:
        raise ValueError("run lacks the exact Greedy speculation policy scope")
    if getattr(state, "speculation_gate_receipt_digest", "") != "":
        raise ValueError("calibration evidence must not carry public receipt authority")
    runtime_scope_sha256 = speculation_runtime_scope_digest(config)
    if getattr(state, "speculation_runtime_scope_sha256", "") != runtime_scope_sha256:
        raise ValueError("run lacks the exact source-owned runtime scope pin")
    run_gpu_inventory = _normalize_gpu_inventory(
        getattr(state, "speculation_calibration_gpu_inventory", ()))
    if not run_gpu_inventory:
        raise ValueError("run lacks a nonempty effective GPU inventory pin")
    _validate_calibration_setup(events, started[0], config, canonical_task)
    task_profile_sha256 = speculation_task_profile_digest(task)

    if getattr(state, "finished", None) is not True:
        raise ValueError("quality evidence must be terminal")
    _validate_calibration_terminal(events, state)
    if len(state.nodes) != max_nodes:
        raise ValueError("quality evidence did not consume its complete physical node budget")
    if sorted(state.nodes) != list(range(max_nodes)):
        raise ValueError("quality evidence node ids must be the exact contiguous calibration range")
    if state.pending_nodes() or getattr(state, "building", None) is not None or state.buildings:
        raise ValueError("quality evidence is not quiescent")
    _raw_node_lifecycle(events, state)
    for node in state.nodes.values():
        if node.attempt != 0:
            raise ValueError("every calibration node must remain at attempt zero")
        if node.tombstoned:
            raise ValueError("quality evidence contains a tombstoned calibration node")
        footprint = getattr(node.idea, "footprint", None)
        if not isinstance(footprint, dict) or footprint.get("gpus") != 1:
            raise ValueError("every calibration node must retain its one-GPU resource envelope")
        if node.footprint_finalized is not True:
            raise ValueError("every calibration node requires a Developer-finalized footprint")
        _validate_cuda_probe_artifact(node)

    requests = list(getattr(state, "card_build_requests", ()))
    outcomes = list(getattr(state, "card_build_outcomes", ()))
    raw_requests = sum(event.type == "card_build_requested" for event in events)
    raw_done = sum(event.type == "card_build_done" for event in events)
    if raw_requests != len(requests):
        raise ValueError(
            "raw card_build_requested count differs from folded accepted requests")
    if (
        raw_done != len(outcomes)
        or raw_done != getattr(state, "card_builds_done", -1)
    ):
        raise ValueError(
            "raw card_build_done count differs from folded accepted outcomes")
    if (
        getattr(state, "card_builds_done", -1) != len(requests)
        or len(outcomes) != len(requests)
    ):
        raise ValueError("quality evidence has an open or inconsistent Card-build queue")
    if outcomes.count("producer_failed"):
        raise ValueError("quality evidence contains a Card producer failure")
    _validate_calibration_card_owners(events, state, requests, outcomes)
    _validate_calibration_greedy_authority(
        events,
        state,
        max_nodes=max_nodes,
        gpu_inventory=run_gpu_inventory,
    )
    links = dict(sorted(getattr(state, "speculative_nodes", {}).items()))
    committed = len(links)
    if outcomes.count("committed") != committed:
        raise ValueError("Card-build outcome ledger differs from exact committed links")
    speculative_evaluated = 0
    freshness_dropped = 0
    for node_id, link in links.items():
        node = state.nodes.get(node_id)
        if node is None:
            raise ValueError("fold exposed a speculative link without its node")
        if not (
            node.speculative is True
            and node.idea.card_id == link.get("card_id")
            and node.card_build_generation == link.get("generation")
        ):
            raise ValueError("folded speculative link has mismatched node ownership")
        if node.status is NodeStatus.evaluated:
            speculative_evaluated += 1
        if (
            node.status is NodeStatus.failed
            and node.speculative is True
            and node.idea.card_id == link.get("card_id")
            and node.card_build_generation == link.get("generation")
            and node.error_reason == "superseded"
            and node.error == CARD_FRESHNESS_SUPERSEDED_ERROR
            and _finite_metric(node.eval_seconds) == 0.0
        ):
            freshness_dropped += 1

    unlinked_speculative = [
        node.id for node in state.nodes.values()
        if node.speculative is True and node.id not in links
    ]
    if unlinked_speculative:
        raise ValueError("quality evidence contains an unlinked speculative node")

    # Baseline candidates must all have one successful, finite attempt-zero evaluation.  Treatment
    # admits only the single explicitly-modelled post-commit freshness outcome in addition to that
    # same success contract; every other failure/error/infeasible path invalidates the lane.
    for node in state.nodes.values():
        if node.status is NodeStatus.evaluated:
            _required_finite(node.metric, label=f"node {node.id} metric")
            if node.feasible is not True or node.violations:
                raise ValueError("quality evidence contains an infeasible calibration node")
            if node.error or node.error_reason:
                raise ValueError("evaluated calibration node retains an error outcome")
            continue
        link = links.get(node.id)
        exact_freshness_drop = bool(
            speculation_depth > 0
            and link is not None
            and node.status is NodeStatus.failed
            and node.speculative is True
            and node.idea.card_id == link.get("card_id")
            and node.card_build_generation == link.get("generation")
            and node.error_reason == "superseded"
            and node.error == CARD_FRESHNESS_SUPERSEDED_ERROR
            and _finite_metric(node.eval_seconds) == 0.0
            and node.metric is None
            and node.feasible is True
            and not node.violations
        )
        if not exact_freshness_drop:
            raise ValueError(
                "quality evidence contains a non-freshness calibration terminal outcome")

    accepted_requests = len(requests)
    stale_precommit = outcomes.count("stale")
    producer_failed = outcomes.count("producer_failed")
    hit_rate = (speculative_evaluated / accepted_requests) if accepted_requests else None
    divergence_rate = (
        (stale_precommit + freshness_dropped) / accepted_requests
        if accepted_requests else None
    )
    eligible = promotion_eligible_nodes(state, flagged=flagged_node_ids(state))
    evaluated_metrics = [
        metric for node in eligible if (metric := _finite_metric(node.metric)) is not None
    ]
    best = state.best()
    if best is None or all(node.id != best.id for node in eligible):
        raise ValueError("folded best is outside the promotion-eligible metric population")
    final_best = _finite_metric(best.metric) if best is not None else None
    if final_best is None:
        raise ValueError("finished quality evidence requires a finite final best metric")
    if not evaluated_metrics:
        raise ValueError("run has no finite evaluated metrics")
    metric_min = min(evaluated_metrics)
    metric_max = max(evaluated_metrics)
    metric_range = metric_max - metric_min
    if not math.isfinite(metric_range):
        raise ValueError("evaluated calibration metric range is not finite")
    trajectory, final_coverage = _coverage_trajectory(state)

    comparable = _comparable_config(config)
    report: dict[str, Any] = {
        "schema": SPECULATION_RUN_ANALYSIS_SCHEMA,
        "run_dir": str(resolved),
        "sources": {
            "events": {"sha256": _sha256(events_raw), "bytes": len(events_raw)},
            "config": {"sha256": _sha256(config_raw), "bytes": len(config_raw)},
            "task": {"sha256": _sha256(task_raw), "bytes": len(task_raw)},
            "task_profile_sha256": task_profile_sha256,
            "comparable_config_sha256": _sha256(_canonical_json(comparable)),
            "semantic_trajectory_sha256": _semantic_execution_trajectory_digest(state),
            # The raw events remain the primary evidence. These explicit sub-digests make pair
            # comparability reviewable and bind the implementation, environment, workspace and
            # data/corpus provenance that produced the trajectory.
            "environment_sha256": _material_digest(getattr(state, "env", None)),
            "workspace_sha256": _material_digest(getattr(state, "workspace", None)),
            "dirty_inputs_sha256": _material_digest(getattr(state, "dirty_inputs", [])),
            "data_provenance_sha256": _material_digest(
                getattr(state, "data_provenance", None)),
        },
        "run": {
            "run_id": run_id,
            "task_id": task_id,
            "direction": direction,
            "finished": True,
            "stop_reason": getattr(state, "stop_reason", None),
            "max_nodes": max_nodes,
            "card_driven_selection": card_driven,
            "speculation_depth": speculation_depth,
            "runtime_scope_sha256": runtime_scope_sha256,
            "implementation_digest": implementation_digest,
            "calibration_profile_digest": profile_digest,
            "calibration_gpu_inventory": run_gpu_inventory,
            "calibration_seed": calibration_seed,
            "policy_scope": policy_scope,
        },
        "metrics": {
            "accepted_requests": accepted_requests,
            "closed_requests": len(outcomes),
            "precommit_stale": stale_precommit,
            "producer_failed": producer_failed,
            "committed_exact_links": committed,
            "speculative_evaluated": speculative_evaluated,
            "freshness_dropped": freshness_dropped,
            "hit_rate": hit_rate,
            "divergence_rate": divergence_rate,
            "trusted_concept_coverage_trajectory": trajectory,
            "final_trusted_concept_coverage": final_coverage,
            "evaluated_metric_count": len(evaluated_metrics),
            "evaluated_metric_min": metric_min,
            "evaluated_metric_max": metric_max,
            "evaluated_metric_range": metric_range,
            "final_best_metric": final_best,
        },
    }
    # This public result must itself remain safe to embed in the bounded gate receipt.
    if len(_canonical_json(report)) > _MAX_RECEIPT_BYTES:
        raise ValueError("run analysis exceeds the receipt byte bound")
    return report, config, task_raw


def analyze_speculation_run(run_dir: str | Path) -> dict[str, Any]:
    """Recompute one bounded run analysis from raw snapshots and strictly decoded event envelopes."""

    report, _config, _task_raw = _analyze_speculation_run(run_dir)
    return report


def _normalize_pair(value: object) -> tuple[object, object]:
    if isinstance(value, Mapping):
        if set(value) != {"baseline", "treatment"}:
            raise ValueError("pair mapping must contain exactly baseline and treatment")
        return value["baseline"], value["treatment"]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 2:
        return value[0], value[1]
    raise ValueError("each pair must be a (baseline, treatment) pair")


def _normalize_gpu_inventory(value: object) -> list[dict[str, Any]]:
    if callable(value):
        value = value()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("GPU inventory must be a sequence")
    if len(value) > _MAX_GPUS:
        raise ValueError("GPU inventory is oversized")
    normalized: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    seen_uuids: set[str] = set()
    seen_pci: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("GPU inventory rows must be mappings")
        if set(raw) != _GPU_IDENTITY_FIELDS:
            raise ValueError("GPU inventory rows must contain only the exact stable identity schema")
        index = raw.get("index")
        uuid = raw.get("uuid")
        pci_bus_id = raw.get("pci_bus_id")
        name = raw.get("name")
        total = raw.get("mem_total_mib")
        driver_version = raw.get("driver_version")
        cuda_driver_version = raw.get("cuda_driver_version")
        if (
            type(index) is not int
            or index < 0
            or index in seen_indices
            or not isinstance(uuid, str)
            or _GPU_UUID_RE.fullmatch(uuid) is None
            or uuid.lower() in seen_uuids
            or not isinstance(pci_bus_id, str)
            or _GPU_PCI_RE.fullmatch(pci_bus_id) is None
            or pci_bus_id.lower() in seen_pci
            or not isinstance(name, str)
            or not name.strip()
            or len(name) > 256
            or not name.isprintable()
            or name != name.strip()
            or type(total) is not int
            or total <= 0
            or not isinstance(driver_version, str)
            or not driver_version
            or len(driver_version) > 64
            or not driver_version.isprintable()
            or driver_version != driver_version.strip()
            or type(cuda_driver_version) is not int
            or cuda_driver_version <= 0
        ):
            raise ValueError("GPU inventory row is not an exact bounded CUDA identity receipt")
        seen_indices.add(index)
        seen_uuids.add(uuid.lower())
        seen_pci.add(pci_bus_id.lower())
        normalized.append({
            "index": index,
            "uuid": uuid,
            "pci_bus_id": pci_bus_id,
            "name": name,
            "mem_total_mib": total,
            "driver_version": driver_version,
            "cuda_driver_version": cuda_driver_version,
        })
    return sorted(normalized, key=lambda row: row["index"])


def _implementation_digest(
    implementation_digest_fn: Callable[[], str] | None,
) -> str:
    digest = (
        implementation_digest_fn()
        if implementation_digest_fn is not None
        else speculation_implementation_digest()
    )
    if not _valid_digest(digest):
        raise ValueError("implementation digest seam returned an invalid SHA-256 digest")
    return digest


def speculation_implementation_digest() -> str:
    """Digest the complete Python runtime plus shipped runtime/packaging resources.

    An allow-list proved too easy to under-specify: dispatch, evaluation, policy, adapters and broker
    code can all alter an A/B trajectory.  Hashing every shipped Python module plus the settings schema
    consumed by the runtime/UI remains small and prevents old evidence being re-labelled after an edit.
    """

    root = Path(__file__).resolve().parents[2]
    manifest: list[dict[str, Any]] = []
    package_root = root / "looplab"
    if not package_root.is_dir():
        # Installed wheels still place this module two levels below the import package.  Derive the
        # package root directly instead of assuming a repository checkout surrounds it.
        package_root = Path(__file__).resolve().parents[1]
        root = package_root.parent
    # CODEX AGENT: hashing raw source bytes makes comments, formatting and line-ending conversion
    # revoke every previously issued receipt even when runtime semantics are identical. That turns
    # review-only commits into an operational stop/resume outage and forces six fresh GPU calibration
    # runs after documentation edits. Bind a versioned semantic/runtime manifest (or an explicit
    # rollout protocol version) while retaining exact hashes only for files that affect execution.
    paths = list(package_root.rglob("*.py"))
    for relative in _IMPLEMENTATION_REQUIRED_PACKAGE_FILES:
        resource = package_root / relative
        if not resource.is_file():
            raise ValueError(f"required implementation resource is missing: {relative}")
        paths.append(resource)
    paths.extend(
        path for relative in _IMPLEMENTATION_OPTIONAL_FILES
        if (path := root / relative).is_file()
    )
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        raw = _read_bounded(path, limit=8 * 1024 * 1024, label=f"implementation file {relative}")
        manifest.append({"path": relative, "bytes": len(raw), "sha256": _sha256(raw)})
    if not 1 <= len(manifest) <= 1000:
        raise ValueError("implementation source manifest is empty or oversized")
    return _sha256(_canonical_json({
        "schema": "looplab.speculation-implementation/v1",
        "files": manifest,
    }))


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    if any(_finite_metric(value) is None for value in values):
        raise ValueError("aggregate input contains a non-finite metric")
    try:
        result = math.fsum(values) / len(values)
    except OverflowError as exc:
        raise ValueError("aggregate metric overflowed") from exc
    if not math.isfinite(result):
        raise ValueError("aggregate metric is not finite")
    return result


def _pair_quality(
    baseline: Mapping[str, Any], treatment: Mapping[str, Any], direction: str,
) -> dict[str, float]:
    baseline_metrics = baseline["metrics"]
    treatment_metrics = treatment["metrics"]
    baseline_best = _required_finite(
        baseline_metrics["final_best_metric"], label="baseline final best metric")
    treatment_best = _required_finite(
        treatment_metrics["final_best_metric"], label="treatment final best metric")
    raw_regret = (
        max(0.0, baseline_best - treatment_best)
        if direction == "max"
        else max(0.0, treatment_best - baseline_best)
    )
    if not math.isfinite(raw_regret):
        raise ValueError("pair regret overflowed")
    # Normalize in the same promotion-eligible metric population that produced ``final_best``.
    # A hard ``1`` floor made the threshold depend on the objective's units (and could hide a large
    # relative loss on small-valued objectives).  The best magnitude + observed eligible range gives
    # a dimensionless, scale-aware denominator while epsilon handles the exact-zero degenerate case.
    denominator = max(
        abs(baseline_best),
        _required_finite(
            baseline_metrics["evaluated_metric_range"],
            label="baseline evaluated metric range",
        ),
        1e-12,
    )
    baseline_coverage = int(baseline_metrics["final_trusted_concept_coverage"])
    treatment_coverage = int(treatment_metrics["final_trusted_concept_coverage"])
    if baseline_coverage <= 0:
        raise ValueError("baseline trusted concept coverage must be nonzero")
    result = {
        "normalized_regret": raw_regret / denominator,
        "hit_rate": _required_finite(
            treatment_metrics["hit_rate"], label="treatment hit rate"),
        "divergence_rate": _required_finite(
            treatment_metrics["divergence_rate"], label="treatment divergence rate"),
        "coverage_ratio": treatment_coverage / baseline_coverage,
    }
    if any(not math.isfinite(value) for value in result.values()):
        raise ValueError("derived pair quality metric is not finite")
    return result


def _bounded_error(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")[:_MAX_ERROR_CHARS]


def speculation_quality_gate(
    pairs: Sequence[object],
    require_gpu: bool = True,
    gpu_inventory: object = None,
    *,
    implementation_digest_fn: Callable[[], str] | None = None,
    environment_fingerprint: object = None,
) -> dict[str, Any]:
    """Evaluate fixed v1 paired-run thresholds and return a deterministic receipt body.

    ``gpu_inventory`` and ``implementation_digest_fn`` are explicit test/air-gapped seams.  Neither
    can change thresholds or replace raw run evidence.
    """

    errors: list[str] = []
    pair_reports: list[dict[str, Any]] = []

    # This call is unconditional: malformed pair input must not bypass the scorer compatibility gate.
    try:
        raw_scorer = scorer_fidelity_gate()
        if not isinstance(raw_scorer, Mapping):
            raise ValueError("scorer fidelity report is not a mapping")
        scorer = dict(raw_scorer)
        scorer_bytes = _canonical_json(scorer)
        if len(scorer_bytes) > _MAX_SCORER_BYTES:
            raise ValueError("scorer fidelity report is oversized")
        # Receipt equality is a JSON contract, not a Python-key-type contract. Policy audit metadata
        # may legitimately contain integer node-id keys; serialize+decode once here so the freshly
        # computed body has the same string-key representation as a receipt loaded from disk.
        normalized_scorer = _json_loads(scorer_bytes)
        if not isinstance(normalized_scorer, dict):
            raise ValueError("scorer fidelity report is not a JSON object")
        scorer = normalized_scorer
    except Exception as exc:
        scorer = {
            "schema": SCORER_FIDELITY_SCHEMA,
            "passed": False,
            "cases": 0,
            "mismatches": 1,
            "case_results": [],
            "error": _bounded_error(exc),
        }
    scorer_mismatches = scorer.get("mismatches") if isinstance(scorer, Mapping) else None
    scorer_rows = scorer.get("case_results") if isinstance(scorer, Mapping) else None
    scorer_names = (
        tuple(row.get("name") for row in scorer_rows)
        if isinstance(scorer_rows, list) and all(isinstance(row, dict) for row in scorer_rows)
        else ()
    )
    if (
        scorer.get("schema") != SCORER_FIDELITY_SCHEMA
        or scorer.get("cases") != SCORER_FIDELITY_CASE_COUNT
        or not isinstance(scorer_rows, list)
        or len(scorer_rows) != SCORER_FIDELITY_CASE_COUNT
        or scorer_names != SCORER_FIDELITY_CASE_NAMES
        or not all(row.get("passed") is True for row in scorer_rows)
        or type(scorer_mismatches) is not int
        or scorer_mismatches != SPECULATION_QUALITY_THRESHOLDS["scorer_mismatches"]
        or scorer.get("passed") is not True
    ):
        errors.append("scorer fidelity has mismatches")

    if type(require_gpu) is not bool:
        errors.append("require_gpu must be boolean")
        require_gpu = True
    elif require_gpu is not True:
        errors.append("the speculation quality receipt requires real-GPU evidence")
    try:
        inventory_source = effective_gpu_inventory() if gpu_inventory is None else gpu_inventory
        inventory = _normalize_gpu_inventory(inventory_source)
    except Exception as exc:
        inventory = []
        errors.append(f"invalid GPU inventory: {_bounded_error(exc)}")
    if require_gpu and not inventory:
        errors.append("a nonempty real GPU inventory is required")

    try:
        implementation_digest = _implementation_digest(implementation_digest_fn)
    except Exception as exc:
        implementation_digest = ""
        errors.append(f"implementation digest unavailable: {_bounded_error(exc)}")
    try:
        environment_sha256 = _environment_digest(
            speculation_environment_fingerprint()
            if environment_fingerprint is None else environment_fingerprint
        )
    except Exception as exc:
        environment_sha256 = ""
        errors.append(f"environment fingerprint unavailable: {_bounded_error(exc)}")

    exact_pair_count = len(SPECULATION_CALIBRATION_SEEDS)
    if not isinstance(pairs, Sequence) or isinstance(pairs, (str, bytes)):
        pair_values: list[object] = []
        errors.append("pairs must be the exact bounded calibration sequence")
    elif len(pairs) != exact_pair_count:
        pair_values = list(pairs[:exact_pair_count])
        errors.append(f"pair count must be exactly {exact_pair_count}")
    else:
        pair_values = list(pairs)

    seen_dirs: set[str] = set()
    seen_run_ids: set[str] = set()
    seen_event_digests: set[str] = set()
    seen_source_identities: set[tuple[str, str, str]] = set()
    seen_semantic_trajectories: set[str] = set()
    seen_calibration_seeds: set[int] = set()
    admitted_depth: int | None = None
    admitted_max_nodes: int | None = None
    runtime_scope_sha256: str | None = None
    task_profile_sha256: str | None = None
    replicate_comparable_config: dict[str, Any] | None = None
    replicate_provenance: dict[str, str] | None = None
    valid_pair_metrics: list[dict[str, float]] = []
    all_pair_contracts = len(pair_values) == exact_pair_count
    for pair_index, raw_pair in enumerate(pair_values):
        pair_errors: list[str] = []
        baseline_report: dict[str, Any] | None = None
        treatment_report: dict[str, Any] | None = None
        quality: dict[str, float] | None = None
        try:
            baseline_dir, treatment_dir = _normalize_pair(raw_pair)
            baseline_report, baseline_config, baseline_task = _analyze_speculation_run(baseline_dir)
            treatment_report, treatment_config, treatment_task = _analyze_speculation_run(treatment_dir)
            for report in (baseline_report, treatment_report):
                path = report["run_dir"]
                identity = _run_dir_identity(path)
                if identity in seen_dirs:
                    pair_errors.append("run directory is reused across pairs")
                seen_dirs.add(identity)
                run_id = report["run"]["run_id"]
                if run_id in seen_run_ids:
                    pair_errors.append("run_id is reused across evidence lanes")
                seen_run_ids.add(run_id)
                event_digest = report["sources"]["events"]["sha256"]
                if event_digest in seen_event_digests:
                    pair_errors.append("events source is cloned across evidence lanes")
                seen_event_digests.add(event_digest)
                source_identity = (
                    event_digest,
                    report["sources"]["config"]["sha256"],
                    report["sources"]["task"]["sha256"],
                )
                if source_identity in seen_source_identities:
                    pair_errors.append("complete source identity is cloned across evidence lanes")
                seen_source_identities.add(source_identity)
                semantic_trajectory = report["sources"]["semantic_trajectory_sha256"]
                if semantic_trajectory in seen_semantic_trajectories:
                    pair_errors.append(
                        "semantic execution trajectory is cloned across evidence lanes")
                seen_semantic_trajectories.add(semantic_trajectory)
            if _run_dir_identity(baseline_report["run_dir"]) == _run_dir_identity(
                treatment_report["run_dir"]
            ):
                pair_errors.append("baseline and treatment must be distinct directories")
            if baseline_task != treatment_task:
                pair_errors.append("task.snapshot.json bytes differ inside pair")
            pair_task_profile = baseline_report["sources"]["task_profile_sha256"]
            if treatment_report["sources"]["task_profile_sha256"] != pair_task_profile:
                pair_errors.append("task profiles differ inside pair")
            if task_profile_sha256 is None:
                task_profile_sha256 = pair_task_profile
            elif task_profile_sha256 != pair_task_profile:
                pair_errors.append("task profile differs across replicate pairs")
            if _comparable_config(baseline_config) != _comparable_config(treatment_config):
                pair_errors.append("pair configs differ outside allowed treatment fields")
            pair_comparable_config = _comparable_config(baseline_config)
            if replicate_comparable_config is None:
                replicate_comparable_config = pair_comparable_config
            elif pair_comparable_config != replicate_comparable_config:
                pair_errors.append("comparable config differs across replicate pairs")

            for report, lane in ((baseline_report, "baseline"),
                                 (treatment_report, "treatment")):
                if report["run"]["implementation_digest"] != implementation_digest:
                    pair_errors.append(
                        f"{lane} was not produced by the current implementation digest")
                if report["sources"]["environment_sha256"] != environment_sha256:
                    pair_errors.append(
                        f"{lane} was not produced by the current environment fingerprint")
                if report["run"]["calibration_gpu_inventory"] != inventory:
                    pair_errors.append(
                        f"{lane} effective GPU inventory differs from the gate host")
            for material_key in (
                "environment_sha256",
                "workspace_sha256",
                "dirty_inputs_sha256",
                "data_provenance_sha256",
            ):
                if (baseline_report["sources"][material_key]
                        != treatment_report["sources"][material_key]):
                    pair_errors.append(
                        f"pair {material_key.removesuffix('_sha256')} provenance differs")
            pair_provenance = {
                material_key: baseline_report["sources"][material_key]
                for material_key in (
                    "workspace_sha256",
                    "dirty_inputs_sha256",
                    "data_provenance_sha256",
                )
            }
            if replicate_provenance is None:
                replicate_provenance = pair_provenance
            else:
                for material_key, material_digest in pair_provenance.items():
                    if replicate_provenance[material_key] != material_digest:
                        pair_errors.append(
                            f"{material_key.removesuffix('_sha256')} provenance differs "
                            "across replicate pairs"
                        )

            baseline_run = baseline_report["run"]
            treatment_run = treatment_report["run"]
            pair_seed = baseline_run["calibration_seed"]
            if treatment_run["calibration_seed"] != pair_seed:
                pair_errors.append("baseline and treatment calibration seeds differ")
            if pair_seed in seen_calibration_seeds:
                pair_errors.append("calibration seed is reused across replicate pairs")
            seen_calibration_seeds.add(pair_seed)
            if baseline_run["finished"] is not True or treatment_run["finished"] is not True:
                pair_errors.append("both runs must be finished")
            if baseline_run["direction"] != treatment_run["direction"]:
                pair_errors.append("pair directions differ")
            if baseline_run["max_nodes"] != treatment_run["max_nodes"]:
                pair_errors.append("pair max_nodes differ")
            elif admitted_max_nodes is None:
                admitted_max_nodes = baseline_run["max_nodes"]
            elif baseline_run["max_nodes"] != admitted_max_nodes:
                pair_errors.append("max_nodes differs across replicate pairs")
            pair_runtime_scope = baseline_run["runtime_scope_sha256"]
            if treatment_run["runtime_scope_sha256"] != pair_runtime_scope:
                pair_errors.append("pair runtime scope digests differ")
            elif runtime_scope_sha256 is None:
                runtime_scope_sha256 = pair_runtime_scope
            elif pair_runtime_scope != runtime_scope_sha256:
                pair_errors.append("runtime scope differs across replicate pairs")
            if baseline_run["card_driven_selection"] is not True:
                pair_errors.append("baseline card_driven_selection is not true")
            if treatment_run["card_driven_selection"] is not True:
                pair_errors.append("treatment card_driven_selection is not true")
            if baseline_run["speculation_depth"] != 0:
                pair_errors.append("baseline speculation_depth is not zero")
            if type(treatment_run["speculation_depth"]) is not int or treatment_run["speculation_depth"] <= 0:
                pair_errors.append("treatment speculation_depth is not positive")
            elif admitted_depth is None:
                admitted_depth = treatment_run["speculation_depth"]
            elif treatment_run["speculation_depth"] != admitted_depth:
                pair_errors.append("treatment speculation_depth differs across replicate pairs")
            if baseline_run["policy_scope"] != "greedy" or treatment_run["policy_scope"] != "greedy":
                pair_errors.append("pair is outside the Greedy policy scope")
            if (baseline_run["calibration_profile_digest"]
                    != treatment_run["calibration_profile_digest"]):
                pair_errors.append("pair calibration profile digests differ")
            if baseline_report["metrics"]["accepted_requests"] != 0:
                pair_errors.append("depth-zero baseline contains accepted speculative requests")
            if baseline_report["metrics"]["committed_exact_links"] != 0:
                pair_errors.append("depth-zero baseline contains committed speculative links")
            if treatment_report["metrics"]["committed_exact_links"] <= 0:
                pair_errors.append("treatment committed no exact speculative links")

            if not pair_errors:
                quality = _pair_quality(
                    baseline_report, treatment_report, baseline_run["direction"])
                valid_pair_metrics.append(quality)
                if quality["normalized_regret"] > SPECULATION_QUALITY_THRESHOLDS[
                    "max_pair_normalized_regret"
                ]:
                    pair_errors.append("pair normalized regret exceeds 0.10")
                if quality["divergence_rate"] > SPECULATION_QUALITY_THRESHOLDS[
                    "max_pair_divergence_rate"
                ]:
                    pair_errors.append("pair divergence rate exceeds 0.34")
                if quality["coverage_ratio"] < SPECULATION_QUALITY_THRESHOLDS[
                    "min_pair_coverage_ratio"
                ]:
                    pair_errors.append("pair trusted coverage ratio is below 0.90")
        except Exception as exc:
            pair_errors.append(_bounded_error(exc))

        passed = not pair_errors
        all_pair_contracts = all_pair_contracts and passed
        pair_reports.append({
            "index": pair_index,
            "baseline": baseline_report,
            "treatment": treatment_report,
            "quality": quality,
            "errors": pair_errors,
            "passed": passed,
        })

    if seen_calibration_seeds != set(SPECULATION_CALIBRATION_SEEDS):
        errors.append(
            "calibration seed set must be exactly "
            f"{list(SPECULATION_CALIBRATION_SEEDS)}"
        )
        all_pair_contracts = False

    regrets = [row["normalized_regret"] for row in valid_pair_metrics]
    hits = [row["hit_rate"] for row in valid_pair_metrics]
    divergences = [row["divergence_rate"] for row in valid_pair_metrics]
    coverage = [row["coverage_ratio"] for row in valid_pair_metrics]
    try:
        aggregates: dict[str, Any] = {
            "pair_count": len(pair_reports),
            "valid_metric_pairs": len(valid_pair_metrics),
            "mean_normalized_regret": _mean(regrets),
            "max_pair_normalized_regret": max(regrets) if regrets else None,
            "mean_hit_rate": _mean(hits),
            "max_pair_divergence_rate": max(divergences) if divergences else None,
            "min_pair_coverage_ratio": min(coverage) if coverage else None,
        }
        if any(
            value is not None and _finite_metric(value) is None
            for key, value in aggregates.items()
            if key not in {"pair_count", "valid_metric_pairs"}
        ):
            raise ValueError("derived aggregate quality metric is not finite")
    except (OverflowError, ValueError) as exc:
        errors.append(f"aggregate quality metrics unavailable: {_bounded_error(exc)}")
        aggregates = {
            "pair_count": len(pair_reports),
            "valid_metric_pairs": 0,
            "mean_normalized_regret": None,
            "max_pair_normalized_regret": None,
            "mean_hit_rate": None,
            "max_pair_divergence_rate": None,
            "min_pair_coverage_ratio": None,
        }
    aggregate_passed = bool(
        len(pair_reports) == exact_pair_count
        and len(valid_pair_metrics) == len(pair_reports)
        and aggregates["mean_normalized_regret"] is not None
        and aggregates["max_pair_normalized_regret"] is not None
        and aggregates["mean_hit_rate"] is not None
        and aggregates["max_pair_divergence_rate"] is not None
        and aggregates["min_pair_coverage_ratio"] is not None
        and aggregates["mean_normalized_regret"]
        <= SPECULATION_QUALITY_THRESHOLDS["max_mean_normalized_regret"]
        and aggregates["max_pair_normalized_regret"]
        <= SPECULATION_QUALITY_THRESHOLDS["max_pair_normalized_regret"]
        and aggregates["mean_hit_rate"] >= SPECULATION_QUALITY_THRESHOLDS["min_mean_hit_rate"]
        and aggregates["max_pair_divergence_rate"]
        <= SPECULATION_QUALITY_THRESHOLDS["max_pair_divergence_rate"]
        and aggregates["min_pair_coverage_ratio"]
        >= SPECULATION_QUALITY_THRESHOLDS["min_pair_coverage_ratio"]
    )
    if not aggregate_passed:
        errors.append("fixed v1 aggregate thresholds are not satisfied")

    body: dict[str, Any] = {
        "schema": SPECULATION_QUALITY_GATE_SCHEMA,
        "thresholds": dict(SPECULATION_QUALITY_THRESHOLDS),
        "require_gpu": require_gpu,
        "gpu_inventory": inventory,
        "implementation_digest": implementation_digest,
        "environment_sha256": environment_sha256,
        "policy_scope": SPECULATION_POLICY_SCOPE,
        "workload_scope": SPECULATION_WORKLOAD_SCOPE,
        "calibration_seeds": list(SPECULATION_CALIBRATION_SEEDS),
        "task_profile_sha256": task_profile_sha256 or "",
        "admitted_depth": admitted_depth,
        "admitted_max_nodes": admitted_max_nodes,
        "runtime_scope_sha256": runtime_scope_sha256 or "",
        "calibration_profile_digest": (
            pair_reports[0]["baseline"]["run"]["calibration_profile_digest"]
            if pair_reports and isinstance(pair_reports[0].get("baseline"), dict)
            else ""
        ),
        "scorer_fidelity": dict(scorer) if isinstance(scorer, Mapping) else {},
        "pairs": pair_reports,
        "aggregates": aggregates,
        "errors": list(dict.fromkeys(errors)),
        "passed": bool(not errors and all_pair_contracts and aggregate_passed),
    }
    if len(_canonical_json(body)) > _MAX_RECEIPT_BYTES:
        # Preserve a bounded fail-closed report rather than returning an attacker-sized object.
        body["pairs"] = []
        body["errors"] = ["gate report exceeds the receipt byte bound"]
        body["passed"] = False
        body["aggregates"] = {
            "pair_count": len(pair_reports),
            "valid_metric_pairs": 0,
            "mean_normalized_regret": None,
            "max_pair_normalized_regret": None,
            "mean_hit_rate": None,
            "max_pair_divergence_rate": None,
            "min_pair_coverage_ratio": None,
        }
    return body


def _self_digest(body: Mapping[str, Any]) -> str:
    return _sha256(_canonical_json({key: value for key, value in body.items() if key != "self_digest"}))


def write_speculation_gate_receipt(
    path: str | Path,
    pairs: Sequence[object],
    require_gpu: bool = True,
    gpu_inventory: object = None,
    *,
    implementation_digest_fn: Callable[[], str] | None = None,
    environment_fingerprint: object = None,
) -> dict[str, Any]:
    """Atomically publish a passing canonical v1 receipt; failing evidence is never published."""

    body = speculation_quality_gate(
        pairs,
        require_gpu=require_gpu,
        gpu_inventory=gpu_inventory,
        implementation_digest_fn=implementation_digest_fn,
        environment_fingerprint=environment_fingerprint,
    )
    if body.get("passed") is not True:
        raise ValueError("speculation quality gate did not pass; refusing to publish a receipt")
    receipt = {**body, "self_digest": _self_digest(body)}
    encoded = _canonical_json(receipt)
    if len(encoded) + 1 > _MAX_RECEIPT_BYTES:
        raise ValueError("speculation gate receipt exceeds its byte bound")
    strict_atomic_write_text(path, encoded.decode("utf-8") + "\n")
    return receipt


def _receipt_mapping(path_or_mapping: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(path_or_mapping, Mapping):
        encoded = _canonical_json(path_or_mapping)
        if len(encoded) > _MAX_RECEIPT_BYTES:
            raise ValueError("receipt mapping is oversized")
        decoded = _json_loads(encoded)
    else:
        decoded = _json_loads(
            _read_bounded(Path(path_or_mapping), limit=_MAX_RECEIPT_BYTES, label="gate receipt"))
    if not isinstance(decoded, dict):
        raise ValueError("gate receipt must be an object")
    return decoded


def validated_speculation_gate_receipt(
    path_or_mapping: str | Path | Mapping[str, Any],
    *,
    gpu_inventory: object = None,
    implementation_digest_fn: Callable[[], str] | None = None,
    environment_fingerprint: object = None,
) -> dict[str, Any] | None:
    """Return an independently revalidated passing receipt, or ``None``.

    Engine wiring can pin the returned ``self_digest`` without re-parsing an untrusted mapping.  The
    public boolean validator below remains the convenient yes/no boundary.
    """

    try:
        receipt = _receipt_mapping(path_or_mapping)
        if set(receipt) != _RECEIPT_FIELDS:
            return None
        if receipt.get("schema") != SPECULATION_QUALITY_GATE_SCHEMA:
            return None
        if receipt.get("thresholds") != dict(SPECULATION_QUALITY_THRESHOLDS):
            return None
        if type(receipt.get("require_gpu")) is not bool:
            return None
        if not _valid_digest(receipt.get("self_digest")):
            return None
        if receipt["self_digest"] != _self_digest(receipt):
            return None
        current_implementation = _implementation_digest(implementation_digest_fn)
        if receipt.get("implementation_digest") != current_implementation:
            return None
        current_environment = _environment_digest(
            speculation_environment_fingerprint()
            if environment_fingerprint is None else environment_fingerprint
        )
        if receipt.get("environment_sha256") != current_environment:
            return None
        if receipt.get("policy_scope") != SPECULATION_POLICY_SCOPE:
            return None
        if receipt.get("workload_scope") != SPECULATION_WORKLOAD_SCOPE:
            return None
        if receipt.get("calibration_seeds") != list(SPECULATION_CALIBRATION_SEEDS):
            return None
        if not _valid_digest(receipt.get("task_profile_sha256")):
            return None
        admitted_depth = receipt.get("admitted_depth")
        if type(admitted_depth) is not int or not 1 <= admitted_depth <= 64:
            return None
        admitted_max_nodes = receipt.get("admitted_max_nodes")
        if type(admitted_max_nodes) is not int or not 1 <= admitted_max_nodes <= 64:
            return None
        if not _valid_digest(receipt.get("runtime_scope_sha256")):
            return None
        if not _valid_digest(receipt.get("calibration_profile_digest")):
            return None
        rows = receipt.get("pairs")
        if (
            not isinstance(rows, list)
            or len(rows) != len(SPECULATION_CALIBRATION_SEEDS)
        ):
            return None
        source_pairs: list[tuple[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                return None
            baseline = row.get("baseline")
            treatment = row.get("treatment")
            if not isinstance(baseline, dict) or not isinstance(treatment, dict):
                return None
            baseline_dir = baseline.get("run_dir")
            treatment_dir = treatment.get("run_dir")
            if (
                not isinstance(baseline_dir, str)
                or not isinstance(treatment_dir, str)
                or len(baseline_dir) > _MAX_PATH_CHARS
                or len(treatment_dir) > _MAX_PATH_CHARS
            ):
                return None
            source_pairs.append((baseline_dir, treatment_dir))

        recomputed = speculation_quality_gate(
            source_pairs,
            require_gpu=receipt["require_gpu"],
            gpu_inventory=gpu_inventory,
            implementation_digest_fn=implementation_digest_fn,
            environment_fingerprint=environment_fingerprint,
        )
        stored_body = {key: value for key, value in receipt.items() if key != "self_digest"}
        # Equality covers every source digest and raw metric. `passed` is not consulted until after the
        # recomputation has independently crossed all fixed constants.
        return dict(receipt) if (
            recomputed.get("passed") is True
            and _canonical_json(stored_body) == _canonical_json(recomputed)
        ) else None
    except Exception:
        return None


def validate_speculation_gate_receipt(
    path_or_mapping: str | Path | Mapping[str, Any],
    *,
    gpu_inventory: object = None,
    implementation_digest_fn: Callable[[], str] | None = None,
    environment_fingerprint: object = None,
) -> bool:
    """Whether a receipt revalidates against current code, scorer, GPU identity and raw runs."""

    return validated_speculation_gate_receipt(
        path_or_mapping,
        gpu_inventory=gpu_inventory,
        implementation_digest_fn=implementation_digest_fn,
        environment_fingerprint=environment_fingerprint,
    ) is not None


__all__ = [
    "SPECULATION_QUALITY_GATE_SCHEMA",
    "SPECULATION_QUALITY_THRESHOLDS",
    "SPECULATION_RUN_ANALYSIS_SCHEMA",
    "analyze_speculation_run",
    "speculation_environment_fingerprint",
    "speculation_implementation_digest",
    "speculation_quality_gate",
    "speculation_task_profile_digest",
    "validate_speculation_gate_receipt",
    "validated_speculation_gate_receipt",
    "write_speculation_gate_receipt",
]
