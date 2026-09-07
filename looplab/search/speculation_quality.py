"""Paired quality BENCHMARK for speculative Card execution, and its receipt.

It consumes completed run directories as immutable evidence, reconstructs speculative ownership through
replay, and emits a bounded receipt whose source and implementation digests can be revalidated later.
It measures scorer fidelity, prediction hit rate, selection divergence and normalized regret on the
shipped quadratic toy, at three fixed seeds, on real GPUs.

WHAT THE RECEIPT MEANS (changed 2026-08-04): it is the benchmark's result, NOT a licence to speculate.
Positive ``speculation_depth`` runs on any TaskAdapter without one — the node-budget refund removed the
cost the receipt's `normalized_regret` bound was protecting (see the admission block in
`engine/orchestrator.py`). What a receipt still does: attest that this build passes the gate, and — on
the toy itself — bind a replay to the exact measured runtime envelope. It is revalidated whenever it is
supplied, so a stale or forged receipt is refused rather than ignored. Counts supplied by a caller are
never accepted as evidence.

HOW LONG A RECEIPT LIVES, because the answer is "not long" and reading that as a defect has now cost
two separate investigations. A receipt is revoked by FOUR independent identities, and
`speculation_implementation_digest` — the one everybody reaches for — is only one of them, and the
only one any code change can influence at all:

  * the `Settings` FIELD SET, which `_validate_calibration_setup` compares each archived
    `config.snapshot.json` against. Every field added to `Settings` anywhere in the repo revokes every
    receipt ever issued, and this axis is UNREPAIRABLE by construction: the evidence is frozen JSON on
    disk and can never grow the new key. (CLAUDE.md records the same trap from the writer's side — "a
    new unconditional `run_started` key would revoke every issued speculation-calibration receipt".)
  * `speculation_environment_fingerprint` — any `pip install`, any interpreter change.
  * `speculation_implementation_digest` — a semantic edit to any shipped `.py` (v2 already excludes
    comments and formatting; see `_semantic_source`).
  * the effective GPU inventory — a different box, driver or `CUDA_VISIBLE_DEVICES`.

Measured 2026-08-14 against this repo's own 2026-08-04 receipt: ALL FOUR had moved, and the `Settings`
field set had drifted by 17 additions and 1 removal in ten days. Pinning the source digest and the
environment fingerprint by hand still left it refused. So scoping the implementation digest to "the
modules that can change speculation behaviour" was examined and DECLINED: it would not have kept a
single receipt alive, while narrowing what a gate covers is precisely the change that could let a real
behavioural edit past a lane that refuses runs. What was missing was never coverage — it was the
ability to ASK, which is `speculation_gate_receipt_rejection`. The operating procedure (re-earn the
receipt immediately before the replay that needs it, and ignore staleness entirely off the calibration
lane, where a declined receipt costs nothing) is in `docs/guide/cli-reference.md`.
"""
from __future__ import annotations

import ast
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

from looplab.core.jsonutil import canonical_json, valid_digest_ref
from looplab.core.atomicio import strict_atomic_write_text
from looplab.core.config import RUN_START_PINNED_FIELDS
from looplab.core.fitness import VERIFIER_SELECTION_CONTRACT, finite_metric
from looplab.core.hardware import effective_gpu_inventory
# The finalization protocol and the run-start setup identity are the ENGINE WRITER's, read back here.
# Both live below `search` (`events/` and `core/`) rather than in the engine because `search` may not
# import `engine` — the edge doc 25 XP-07 closed and `tests/test_calibration_profile_home.py` pins at
# zero. Importing them is what makes writer and validator one spelling instead of two (doc 25 SE-01).
from looplab.core.setup_identity import setup_config_hash, setup_manifest_digest
from looplab.events.finalize_protocol import (
    FINALIZE_BUDGET_FIELDS,
    FINALIZE_STEP_ABANDONED,
    FINALIZE_STEP_BEGUN,
    FINALIZE_STEP_BUDGET,
    FINALIZE_STEP_COMPLETE,
    FINALIZE_STEP_REFLECTION,
    FINALIZE_STEP_REFLECTION_BEGUN,
    QUIET_FINALIZATION_SUFFIX,
)
from looplab.core.models import (
    CARD_ACTION_DIGEST_V2_FIELDS,
    Event,
    NodeStatus,
    card_ownership_receipt,
    idea_proposal_ref,
    normalize_researcher_footprint,
)
from looplab.events.eventstore import MAX_EVENT_BATCH_BYTES, decode_event_record
from looplab.events.replay import FoldCursor, flagged_node_ids, fold, promotion_eligible_nodes
from looplab.events.types import (
    EV_CARD_AUTO_DROPPED,
    ALL_EVENT_TYPES,
    EV_BUDGET,
    EV_BUDGET_EXTEND,
    EV_CARD_ADDED,
    EV_CARD_BUILD_ATTEMPTED,
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
    EV_NODE_EVAL_STARTED,
    EV_NODE_EVALUATED,
    EV_NODE_FAILED,
    EV_NODE_REPAIRED,
    EV_NODE_RESET,
    EV_NODE_TOMBSTONED,
    EV_PAUSE,
    EV_PHASE_PROGRESS,
    EV_POLICY_DECISION,
    EV_RESTART,
    EV_RESUME,
    EV_RESUME_REQUESTED,
    EV_RESUME_SERVED,
    EV_RUN_ABORT,
    EV_RUN_FINISHED,
    EV_RUN_REOPENED,
    EV_RUN_STARTED,
    EV_RUN_WIDTH_SETTLED,
    EV_SETUP_FINISHED,
    EV_SETUP_STARTED,
    EV_SETUP_STEP,
    EV_STAGE_FINISHED,
)
from looplab.search.card_selection import (
    CARD_FRESHNESS_SUPERSEDED_ERROR,
    META_CARD_ID,
    CardResourceEnvelope,
    SpeculativeSelectionContext,
    card_budget_used,
    is_unevaluated_speculative_discard,
    node_counts_toward_card_budget,
    refunded_node_reservations,
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
# The probe's canonical home since 2026-08-14 is `core/calibration.py` (it moved down out of
# `agents/` so `runtime/sandbox.py` could name it). `agents/roles.py` still re-exports every name,
# so this import is the SAME objects it always was — spelled at the home that owns them.
from looplab.core.calibration import (
    SPECULATION_CUDA_PROBE_CODE_PREFIX,
    SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC,
    SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS,
    SPECULATION_CUDA_PROBE_STATIC_EXTRA_METRICS,
)


SPECULATION_RUN_ANALYSIS_SCHEMA = "looplab.speculation-run-analysis/v1"
SPECULATION_QUALITY_GATE_SCHEMA = "looplab.speculation-quality-gate/v1"
# The run-start identity token for the receiptless (product) speculation lane. Its own schema string
# keeps its preimage disjoint from a gate receipt's, so the two lanes can never validate each other.
# v2 BECAUSE THE PREIMAGE CHANGED: v1 hashed `implementation_digest` alongside the policy scope and
# task kind, and that field was later removed (a whole-source digest must not gate a real run's
# resume) without bumping the id — leaving two different preimages claiming one schema name, which is
# exactly the collision a schema id exists to prevent.
SPECULATION_PRODUCT_AUTHORITY_SCHEMA = "looplab.speculation-product-authority/v2"
# Read-only. A run started between the field's removal and the bump recorded a v1 token over the v2
# preimage; those logs are still resumable because re-entry accepts this alternative derivation. The
# ORIGINAL v1 preimage is deliberately not reproducible here — it needs the source digest as of that
# run, which no later process has — so a log from before the removal is refused with a named cause
# rather than guessed at.
SPECULATION_PRODUCT_AUTHORITY_LEGACY_SCHEMAS: tuple[str, ...] = (
    "looplab.speculation-product-authority/v1",
)

# These values are source-owned.  There is deliberately no thresholds argument on any public API.
SPECULATION_QUALITY_THRESHOLDS: Mapping[str, int | float] = MappingProxyType({
    # DERIVED from the calibration seed set, and the gate reads it back (`exact_pair_count`), so the
    # published number is by construction the one enforced — a receipt can never advertise a pair
    # threshold no code path applies. Under v1 the bound is EXACT, not a floor: the gate requires
    # this many pairs and refuses more. The key keeps its v1 name so stored receipts stay readable.
    "min_pairs": len(SPECULATION_CALIBRATION_SEEDS),
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
    # The settled concurrency widths (`Engine._run_start_settled_widths`). Named here rather than
    # reached through RUN_START_PINNED_FIELDS because they are RESOLVED integers, while that set
    # drives the per-field equality loop below — which compares run_started against the SETTINGS in
    # config.snapshot.json. The two coincide for this profile (it spells both widths `1`, not the `0`
    # AUTO sentinel the product default ships), but that is a coincidence, not the contract.
    "eval_parallel",
    "llm_parallel",
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
    # The live-progress beacon (`events/types.py::EV_PHASE_PROGRESS`). It is admitted rather than
    # excluded for the same reason `EV_CARD_BUILD_ATTEMPTED` below had to be: this allow-list is
    # consulted by `_validate_calibration_event_envelope`, which every clean offline calibration run
    # passes through, and the beacon is emitted unconditionally by `_enter_run` and by the build
    # spine — so leaving it out would reject EVERY future calibration run as "outside the clean
    # calibration protocol" and no receipt could ever be minted again. That is the exact defect the
    # comment on EV_CARD_BUILD_ATTEMPTED records, five days late.
    #
    # This widening cannot revoke an already-issued receipt. The allow-list only RAISES; it is not an
    # input to the receipt body, so `canonical_json(body)` over any log that validated before this
    # change is byte-identical after it. What changes is only which logs are admitted at all, and the
    # only new admissions are logs carrying a fold-ignored beacon that no calibration measurement
    # reads. (`_validate_calibration_setup`'s five-event prefix and its `setup_step` count of exactly
    # 2 are untouched: the first beacon is `startup/read_log`, which is appended before the setup
    # prefix even begins, so the prefix assertion is checked against a log whose head it no longer
    # is. See the guard test — this is the ONE ordering fact the widening actually depends on.)
    EV_PHASE_PROGRESS,
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
    # The engine's OWN retirement of a Card whose reservation it gave up on
    # (`engine/card_reservation.py`, the `_plan`/CAS drop helper — "a failed drop LEAKS: the caller
    # has already given up on this Card"). It is admitted for the reason the two rows below were,
    # and it became necessary on 2026-08-24: with the empty-authority freshness clause gone, a
    # pre-commit build head can no longer close `skipped="stale"` merely because the board filled,
    # so the only remaining way a speculative reservation ends without a node — and RELEASES its
    # budget slot, which is what lets the next staging carry canonical Greedy authority — is the
    # producer abandoning the Card and the engine retiring it. Without this row that sequence reads
    # as "outside the clean calibration protocol" and the pre-commit accounting has no reachable
    # case left to be measured on at all.
    #
    # SOURCE REVIEW, as this allow-list demands: it retires a Card and can therefore only REMOVE a
    # candidate from selection, never add one or move a metric; the fold applies it through the same
    # `_apply_card_drops` path as an operator drop; and it carries no cost, no metric and no node.
    # Like the widening recorded above it only RAISES what is admitted — the allow-list is not an
    # input to the receipt body, so `canonical_json(body)` over any log that validated before this
    # change is byte-identical after it.
    EV_CARD_AUTO_DROPPED,
    # The paid-attempt receipt a speculative Card producer writes BEFORE it can reach a provider
    # (`feat(durability) 7a2a2ff4`). It is emitted on exactly the path this allow-list exists to
    # admit, but that commit landed five days after this set was last touched (`8d9952a1`), so every
    # positive-depth treatment run carried a row the gate rejected as "outside the clean calibration
    # protocol" — the second of the two reasons no receipt could be minted.
    EV_CARD_BUILD_ATTEMPTED,
    EV_CARD_BUILD_DONE,
    # The durable eval-START boundary, appended by the MAIN task at the dispatch decision and ONLY
    # for a speculative attempt-zero lifecycle — so it appears in the treatment lane and never in a
    # baseline. SOURCE REVIEW, as this allow-list demands: it is set-only and generation-keyed
    # (`replay._on_node_eval_started`), so its splice position cannot change selection; it carries no
    # cost and no metric; and the one thing that DOES read it is the node-budget refund, which is the
    # quantity this A/B measures. Admitting it is what keeps that measurement honest — without the
    # boundary, a treatment run killed mid-evaluation refunds a slot the GPU already spent.
    EV_NODE_EVAL_STARTED,
    EV_NODE_FAILED,
})

# A calibration lane is launch-only and deterministic.  These rows all denote operator recovery,
# mutation, or an alternate node lifecycle; accepting them would let a reset/retry/error trajectory
# be presented as the clean attempt-zero A/B protocol measured by this receipt.
_FORBIDDEN_CALIBRATION_LIFECYCLE_EVENTS = frozenset({
    EV_BUDGET_EXTEND,
    EV_LOG_REPAIRED,
    # docs/29 F1. A proposal-derived width re-pin is a run that CHANGED ITS EXECUTION TREATMENT
    # mid-log, which is precisely what this receipt asserts did not happen: the width is inside the
    # runtime-scope digest and inside `_comparable_config`'s replicate equality, so a trajectory that
    # ran part of itself at another width is not the clean attempt-zero A/B protocol measured here.
    # `Engine._settle_proposal_width` already refuses to fire on a calibration lane (the profile
    # SPELLS all four widths as `1`, so no axis is AUTO, and the calibration flag is checked outright)
    # — this is the OTHER end of the same rule, and it is named rather than left to the
    # `_CALIBRATION_COMMON_EVENT_TYPES` allow-list because an unlisted type is refused as an anonymous
    # unexpected row. Six GPU runs is too expensive a thing to debug from "unexpected event type".
    EV_RUN_WIDTH_SETTLED,
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


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _valid_digest(value: object) -> bool:
    return valid_digest_ref(value, prefix="sha256:")


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
        canonical_json(decoded)
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
    canonical_json(value)
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


# Byte-equivalent to the rule it now delegates to: reject bool/non-numeric, coerce, require finite.
# `is_usable_metric` additionally survives an arbitrary-precision JSON integer such as `10**400`,
# which `float()` raises OverflowError on — the same outcome, reached without the local try/except.
_finite_metric = finite_metric


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
        if field not in config or canonical_json(started_data.get(field)) != canonical_json(
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

    # Re-derive Engine._setup_phase/_setup_manifest's two identities through the SHARED derivation
    # (`core/setup_identity`) rather than a hand-copied one. Copying it here is what made this a
    # byte-level mirror of the writer: the config hash dumps the task payload UNSORTED while the
    # manifest's inner hash dumps it SORTED, and a copy made from memory gets that backwards into a
    # self-consistent digest no honest run produces (doc 25 SE-01). `provenance` stays `{}` because
    # the fresh offline Toy has no data assets — that is a fact about the calibration workload, so it
    # is supplied here rather than assumed by the shared helper.
    try:
        from looplab.adapters.toytask import ToyTask

        task_model = ToyTask.model_validate(dict(canonical_task))
        task_payload = task_model.model_dump(mode="json")
        config_hash = setup_config_hash(task_payload)
        setup_manifest = setup_manifest_digest(task_payload, started_data["workspace"], {})
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


# Payload fields a finalize-step marker carries ONLY under the offline calibration profile, keyed by
# step. Reflection is disabled there (`memory_dir` unset, `reflection_priors` off), and the writer's
# disabled branch records that as `outcome: "disabled"` on both of its markers. This is deliberately
# NOT in `events/finalize_protocol.py`: the protocol says which steps appear in which order, while
# what a step's payload says about a run belongs to the profile that produced it.
_CALIBRATION_STEP_PAYLOAD: Mapping[str, Mapping[str, Any]] = MappingProxyType({
    FINALIZE_STEP_REFLECTION_BEGUN: MappingProxyType({"outcome": "disabled"}),
    FINALIZE_STEP_REFLECTION: MappingProxyType({"outcome": "disabled"}),
})


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
    begun = [event for event in scoped_steps if event.data.get("step") == FINALIZE_STEP_BEGUN]
    complete = [event for event in scoped_steps if event.data.get("step") == FINALIZE_STEP_COMPLETE]
    abandoned = [
        event for event in scoped_steps if event.data.get("step") == FINALIZE_STEP_ABANDONED]
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
    # DOWNWARD into `events`, not up into the engine (doc 25 XP-07): this is a pure projection
    # over an event list, and `search` importing `engine` is the direction that closes the cycle.
    from looplab.events.finalize_scope import incomplete_finalize_scope
    if incomplete_finalize_scope(events) is not None:
        raise ValueError("quality evidence retains a pending finalization scope")

    # The suffix SHAPE is the finalization protocol's, not this validator's. `engine/finalize.py`
    # writes it and `events/finalize_protocol.py::QUIET_FINALIZATION_SUFFIX` is the one spelling both
    # sides read; hand-copying it here is what made every engine finalize change break this gate in
    # lockstep, and silently — the refusal below says "evidence", never "the protocol moved"
    # (doc 25 SE-01). What stays LOCAL is the calibration profile's own payload detail: reflection is
    # off in that profile, so its two markers carry `outcome: "disabled"`, which is a fact about the
    # profile rather than about the protocol.
    expected_types = tuple(event_type for event_type, _step in QUIET_FINALIZATION_SUFFIX)
    if finish.seq < 1 or len(events) != finish.seq + len(QUIET_FINALIZATION_SUFFIX) - 1:
        raise ValueError("quality evidence lacks the exact terminal finalization suffix")
    # `begun` is the row immediately before the finish, so the suffix starts one seq earlier. Every
    # positional read below is safe only because this type comparison has already matched the table.
    suffix = events[finish.seq - 1:]
    if tuple(event.type for event in suffix) != expected_types:
        raise ValueError("quality evidence terminal finalization order differs")

    expected_begun = {
        "scope": scope,
        "step": FINALIZE_STEP_BEGUN,
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
        # The writer mints this payload through `finalize_protocol.budget_receipt`, whose key set is
        # `FINALIZE_BUDGET_FIELDS`. Comparing against the shared constant rather than a local copy is
        # what keeps a field added on the writer side from silently refusing every run (doc 25 SE-01).
        # `elapsed_s` is the RUN's wall clock, read from the log's own first/last `ts` so it is
        # correct across a stop-then-`finalize` process boundary; `process_s` is the measurement
        # the finalizing PROCESS made. Both are checked for shape only — a wall clock is not
        # reproducible from a fold, unlike every other field here.
        or set(budget) != set(FINALIZE_BUDGET_FIELDS)
        or _finite_metric(budget.get("elapsed_s")) is None
        or float(budget["elapsed_s"]) < 0.0
        or _finite_metric(budget.get("process_s")) is None
        or float(budget["process_s"]) < 0.0
        or _finite_metric(budget.get("eval_s")) != expected_eval_s
        or type(budget.get("nodes")) is not int
        or budget["nodes"] != len(state.nodes)
        # Recomputed from this run's own fold, exactly like every other number in the receipt: the
        # observation that replaced the pre-run precondition is itself evidence, so a run may not
        # advertise a `charged_discards: 0` its event log does not support.
        or budget.get("speculation") != speculation_budget_observation(state)
        or budget.get("finalize_scope") != scope
        or budget.get("finish_seq") != finish.seq
    ):
        raise ValueError("quality evidence budget finalization receipt differs from folded state")
    if suffix[3].data != {"scope": scope, "step": FINALIZE_STEP_BUDGET}:
        raise ValueError("quality evidence budget finalization marker differs")

    from looplab.search.archive import DiversityArchive
    expected_archive = {
        **DiversityArchive(1.0).summary(state),
        "finalize_scope": scope,
        "finish_seq": finish.seq,
    }
    if suffix[4].data != expected_archive:
        raise ValueError("quality evidence diversity finalization receipt differs from folded state")
    # The remaining rows are pure markers, so their payloads follow from the protocol table above.
    # `EV_FINALIZATION_FINISHED` (the one non-`finalize_step` row, marked `None` in the table) is the
    # exact-finish acknowledgement and carries the finish seq instead of a scope/step pair.
    expected_tail_data = tuple(
        {"scope": scope, "step": step, **_CALIBRATION_STEP_PAYLOAD.get(step, {})}
        if step is not None else {"finish_seq": finish.seq}
        for _event_type, step in QUIET_FINALIZATION_SUFFIX[5:]
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
                or canonical_json(data) != canonical_json(expected_building)
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


def _live_selection_denominator(events: Sequence[Event], state, *, max_nodes: int) -> int:
    """Mirror ``Engine._refresh_speculation_budget``'s translated Card denominator on one prefix.

    Two terms, exactly as the engine computes them: the refund-aware L3 count (``card_budget_used``)
    plus whatever is left of the physical ceiling.  That ceiling is the operator's ``max_nodes``
    EXTENDED by the reservations the refund gave back — see
    ``Engine._hard_node_reservation_limit`` / ``card_selection.refunded_node_reservations``.  A
    speculative build discarded before it ran spends no slot, so the engine may mint a replacement
    reservation and this authority recomputation must expect exactly the same one.

    The engine's third term (unmaterialized request reservations) is structurally zero here: both
    call sites below have already proven ``card_builds_done == len(card_build_requests)``.
    """

    return card_budget_used(state) + max(
        0,
        max_nodes
        + refunded_node_reservations(state, max_nodes)
        - _raw_node_ceiling(events, state),
    )


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
        # snapshot() deep-copies accumulated RunState and reruns all fold finalizers, yet
        # this prefix is consumed only for card_added/card_build_requested. At the admitted 100k-event
        # bound this approaches quadratic work. Extend on every row, but snapshot only immediately
        # before the authority event types that actually consult the prefix — the same shape as
        # `_validate_calibration_card_owners`. The cursor still advances on EVERY row, so the prefix
        # each branch observes is byte-identical to the unconditional version.
        if event.type == EV_CARD_ADDED:
            prefix = cursor.snapshot()
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
                live_max = _live_selection_denominator(
                    prefix_events, prefix, max_nodes=max_nodes,
                )
                try:
                    actions = speculative_raw_actions(
                        prefix,
                        _canonical_calibration_policy(live_max),
                        live_max,
                        context=SpeculativeSelectionContext(
                            scoring=None,
                            excluded_card_ids=excluded,
                            ignored_pending_node_ids={node.id for node in pending},
                            resource_envelope=envelope,
                        ),
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
            prefix = cursor.snapshot()
            data = event.data if isinstance(event.data, Mapping) else {}
            card_id = data.get("card_id")
            pending = list(prefix.pending_nodes())
            excluded = {
                node.idea.card_id for node in pending
                if isinstance(node.idea.card_id, str)
            }
            if prefix.card_builds_done != len(prefix.card_build_requests):
                raise ValueError("treatment requested a Card while another request was open")
            live_max = _live_selection_denominator(
                prefix_events, prefix, max_nodes=max_nodes,
            )
            try:
                actions = speculative_card_actions(
                    prefix,
                    _canonical_calibration_policy(live_max),
                    live_max,
                    context=SpeculativeSelectionContext(
                        scoring=None,
                        excluded_card_ids=excluded,
                        ignored_pending_node_ids={node.id for node in pending},
                        resource_envelope=envelope,
                    ),
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
            for field in CARD_ACTION_DIGEST_V2_FIELDS
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
            or canonical_json(card.steering_context)
            != canonical_json(data["steering_context"])
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
                canonical_json(idea) != canonical_json(expected_idea)
                or data.get("statement") != statement
                or data.get("rationale") != (node.idea.rationale or "")[:400]
                or data.get("at_node") != node.id
                or data.get("parent_id")
                != (expected_parents[0] if expected_parents else None)
                or data.get("parent_ids") != expected_parents
                or data.get("parent_generations")
                != {str(parent): 0 for parent in expected_parents}
                or canonical_json(data.get("footprint"))
                != canonical_json(node_idea.get("footprint"))
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
            or canonical_json(data) != canonical_json(expected)
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

    return _sha256(canonical_json(value))


def speculation_task_profile_digest(task: object) -> str:
    """Digest the exact admitted calibration workload while excluding only replicate seed."""

    canonical = canonical_speculation_toy_task(task)
    profile = {key: value for key, value in canonical.items() if key != "seed"}
    return _sha256(canonical_json({
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
    # events.  The profile identity is a SIBLING module now, not the engine (doc 25 SE-07) — this
    # layer no longer reaches upward for it.
    from looplab.search.speculation_calibration import (
        SPECULATION_CALIBRATION_PROFILE_DIGEST,
        SPECULATION_CALIBRATION_PROFILE_SETTINGS,
    )
    # Compare against the DOCUMENT the snapshot writer actually emits, not the raw Settings field
    # set: `masked_snapshot()` pops credential bindings and stamps `config_snapshot_schema`, so the
    # two sets are structurally different and an exact equality against the profile can never hold.
    # (See `calibration_snapshot_document_fields` for the two commits that each closed this gate.)
    from looplab.search.speculation_calibration import (
        SPECULATION_CALIBRATION_SNAPSHOT_FIELDS, SPECULATION_RUNTIME_SCOPE_DOCUMENT_FIELDS)
    # THE SET IS DIRECTIONAL (2026-09-03), and it was a two-way equality against a CURRENT constant.
    #
    # `SPECULATION_CALIBRATION_SNAPSHOT_FIELDS` is derived from THIS BINARY's `Settings.model_fields`,
    # so every `Settings` field added after a calibration run was recorded appeared as `missing` and
    # revoked it. Six preserved GPU runs sat as a dead asset on this box and the gate reported it as
    # a snapshot mismatch — which reads like a corrupt run rather than like a version skew — and no
    # receipt could be minted here at all. Adding an unrelated field changes no derivation and no
    # measurement; it changes only what a snapshot happens to contain. The equality was doing the job
    # of a version check with the tool of an exactness check.
    #
    # THE EXACTNESS THAT MATTERS IS ELSEWHERE AND IS UNTOUCHED, which is what makes this narrowing
    # safe rather than a loosening. `speculation_runtime_scope_digest(config)` below digests the
    # whole snapshot document and compares it to the `speculation_runtime_scope_sha256` the RUN
    # stamped at start, so a snapshot that is not byte-exactly the one that run started with already
    # fails — and that check consults no current constant, only the two artifacts. Every value the
    # profile pins is re-checked one loop down, and each field the protocol reads is validated by
    # name below.
    #
    # So what is left for this check is the two things a digest cannot say:
    #   * a field the protocol NEEDS is absent -> still fatal, exactly as before. `_REQUIRED` is
    #     that set spelled out, not "everything Settings currently declares", and it is the clause
    #     the BACKLOG entry insists on: "a field the calibration actually READS staying absent is
    #     still fatal". Subtracting unknown names without it turns this into a no-op.
    #   * a field THIS BINARY DOES NOT UNDERSTAND is present -> still fatal, because a snapshot
    #     written by a newer LoopLab may carry semantics this build would silently drop. That is the
    #     same fail-closed rule `core/config.py::settings_from_snapshot` already applies on resume.
    # A field this binary declares that an OLDER snapshot predates is absent-and-fine, which is the
    # whole of the change.
    known_config_fields = (
        set(SPECULATION_CALIBRATION_SNAPSHOT_FIELDS)
        | set(SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS)
    )
    required_config_fields = (
        set(SPECULATION_CALIBRATION_PROFILE_SETTINGS)
        | set(SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS)
        | set(SPECULATION_RUNTIME_SCOPE_DOCUMENT_FIELDS)
        # ...and every key this function goes on to read by name.
        | {"speculation_gate_receipt", "max_nodes", "card_driven_selection",
           "speculation_depth", "trust_gate"}
        # INTERSECTED with what a snapshot DOCUMENT can carry, because `masked_snapshot()` pops the
        # credential bindings: `llm_api_key_base_url` is pinned by the profile and can never appear
        # in a snapshot, so requiring it present refuses EVERY calibration run — which is the
        # original defect with the sign flipped, and is exactly what the first cut of this did.
        # The profile-settings loop below still checks its VALUE through `config.get`, where an
        # absent key reads None and a pinned None matches.
    ) & known_config_fields
    missing = sorted(required_config_fields - set(config))
    unknown = sorted(set(config) - known_config_fields)
    if missing or unknown:
        raise ValueError(
            f"config fields differ from the exact calibration snapshot "
            f"(missing={missing}, extra={unknown})"
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
    # The RUN-START PIN, explicitly: `state.speculation_depth` is the run's EFFECTIVE treatment, which
    # a `speculation_depth_settled` row is allowed to move, and this line is asserting the run_started
    # authority against the snapshot. Calibration always SPELLS depth 1, and a spelled depth never
    # settles, so no honest calibration log carries such a row — which is why the floor is refused on
    # its own line rather than folded into this comparison: evidence must be ONE exact treatment for
    # its whole length, and a hand-forged row saying otherwise has to name itself in the refusal.
    if getattr(state, "speculation_depth_pinned", None) != speculation_depth:
        raise ValueError("config and run-start pinned speculation_depth differ")
    if getattr(state, "speculation_depth_settled", None) is not None:
        raise ValueError("calibration evidence records an adaptive speculation depth settle")
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
    # The property is "the run did not stop before spending its whole node budget", and the exact
    # equality below expressed it only while every physical reservation was charged. A speculative
    # build discarded before it ever ran is refunded (`card_selection.node_counts_toward_card_budget`),
    # so a treatment lane legitimately re-spends that slot and ends with `max_nodes + <refunds>`
    # physical reservations while consuming exactly `max_nodes` of BUDGET. Widen by exactly the
    # refunds and no further: the lower bound still rejects a lane that stopped early, and the upper
    # bound still rejects any reservation the log cannot account for. With no refunds — every
    # baseline lane, and every log written before the refund existed — this is byte-identical to the
    # original equality.
    refunded = refunded_node_reservations(state, max_nodes)
    if not max_nodes <= len(state.nodes) <= max_nodes + refunded:
        raise ValueError("quality evidence did not consume its complete physical node budget")
    if sorted(state.nodes) != list(range(len(state.nodes))):
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
            # A SALVAGED metric is not a measured one, and this lane compares MEASUREMENTS. The
            # metric of a salvaged node was recovered by re-asking the run's declared reader over the
            # output of an eval that FAILED (`engine/metric_salvage.py`), not produced by the
            # protected scoring path — so it is not a value a baseline/treatment pair may be scored
            # on, whatever rung the operator set.
            #
            # ASKED BEFORE the infeasibility check, and separately from it, because the two rungs of
            # `metric_salvage` fail here in two different ways and neither answer was usable. Under
            # the DEFAULT `audit` the node carries a `metric_salvaged` violation and already failed
            # below — as "contains an infeasible calibration node", which sends the operator hunting
            # a constraint the calibration task does not even declare, after six GPU runs. Under
            # `select` it carries NO violation row and is `feasible`, so it passed this contract in
            # silence and an unmeasured number went into the paired scoring. The second is why this
            # is a check and not merely a better message.
            #
            # WHAT IT DOES NOT MEAN: "re-run with metric_salvage off". The immutable calibration
            # profile is derived from the Settings DEFAULTS
            # (`search/speculation_calibration.py::SPECULATION_CALIBRATION_PROFILE_SETTINGS`), so
            # every calibration run pins `audit` and an operator cannot change it without changing
            # the profile digest, which revokes every receipt ever issued. What makes the lane safe
            # today is structural instead: it runs the offline toy task, which has no declared
            # metric reader at all (`_salvage_eval_metric` returns None without one), so the shipped
            # lane cannot produce this row. A salvaged node in evidence therefore says the evidence
            # did not come from the shipped lane, which is exactly what this gate exists to detect.
            #
            # It cannot revoke an issued receipt either: `metric_provenance` did not exist before
            # 2026-08-12 and folds to None for every log written before it, and an issued receipt is
            # a body with no errors — so over the shipped corpus this branch is unreachable and
            # `canonical_json(body)` is unchanged.
            if (node.metric_provenance or {}).get("salvaged"):
                raise ValueError(
                    "quality evidence contains a node whose metric was SALVAGED rather than "
                    "measured: metric_salvage recovered it from an eval that failed, which the "
                    "offline calibration lane cannot do")
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
            "comparable_config_sha256": _sha256(canonical_json(comparable)),
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
    if len(canonical_json(report)) > _MAX_RECEIPT_BYTES:
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


def _total_predicate(predicate, state, node, *, on_error: bool) -> bool:
    """Evaluate one shared budget predicate without letting a foreign state break finalization.

    `speculation_budget_observation` promises never to raise, and its caller in `finalize_run` makes
    that promise load-bearing: a raise there is swallowed and takes the run's `finalize_step` marker
    with it.  The predicates themselves stay the single definition — this only bounds what a state
    they cannot read is allowed to do, and both call sites pass the fail-closed answer (an unreadable
    node is assumed to have SPENT its slot and to NOT have been refunded).
    """

    if node is None:
        return False
    try:
        return bool(predicate(state, node))
    except Exception:  # noqa: BLE001 - see the docstring: the fallback is the caller's choice
        return on_error


# The `node_failed.reason` values that mean "the BUILD LIFECYCLE threw this node away", as opposed to
# "the experiment ran and produced a failure". Every one is written by
# `engine/orchestrator.py::_fail_reserved_build` or by the speculation commit terminal beside it; none
# of them is ever the outcome of an evaluation.
#
# A REGISTRY, guarded two-way by `tests/test_speculation_product_admission.py`, for the reason every
# other duck-typed seam in this codebase is (CLAUDE.md): the producers are string literals at ~13 call
# sites, so a new discard reason added there and not here would silently hand out a FALSE ALL-CLEAR on
# the one signal that is supposed to notice speculation regressing. The observation's TOTAL contract
# says an unreadable state may over-report and must never under-report; this set is where that promise
# is kept for the discard axis, since an unrecognised reason falls through to the "did it ever run"
# test rather than being trusted.
#
# `events/replay.py::_FAILURE_SPIKE_IGNORED_REASONS` draws a neighbouring line for a different
# question ("is the SEARCH failing?"), and deliberately does not coincide: `card_dropped`/`aborted`
# are operator intent, not build-lifecycle discards, and `build_crash` is a genuine build failure that
# the spike counter should see.
SPECULATION_DISCARD_REASONS: frozenset[str] = frozenset({
    "superseded",               # freshness gate, a lost commit CAS, a replay-rejected creation
    "frozen",                   # the Card lane closed under the subject while it was being built
    "build_batch_cancelled",    # a sibling's crash stopped the rest of an atomically-claimed lane
    "build_crash",              # the build itself raised (concurrent fan-out's guarded terminal)
    "build_interrupted",        # a process died mid-build; recovery closed the reservation
    "proposal_rejected",        # the proposal never formed a bounded, ownable action
})
# NOT here: "reproposed" (a `_drop_card_once` CARD reason, never a node terminal), and every reason a
# node that reached EVALUATION can carry — "crash", "timeout", "developer_crash", "aborted",
# "card_dropped", "gpu_unavailable", "proxy_skipped". Those are experiment outcomes and operator
# intent; counting them is precisely the defect.


def speculation_budget_observation(state) -> dict[str, int]:
    """Per-run answer to "is speculation costing this run real experiment budget?".

    Positive ``speculation_depth`` no longer needs a pre-run calibration receipt on a real workload,
    because a discarded prediction is refunded its node-budget slot. That removed most of the harm
    the receipt's regret bound was protecting, but NOT all of it: a clean H200 A/B over seeds 0/1/2
    put `mean_normalized_regret` at 0.002590 after the refund versus 0.025643 before — ~10x smaller,
    not zero. So the precondition became an observation: this projection is stamped into the run's
    own `budget` finalization receipt (fold-ignored, one append per run), which is where "what did
    this run spend" already lives.

    ``charged_discards`` is the regression signal. It counts every speculative build that produced no
    experiment AND still consumed a node-budget slot — the exact quantity the calibration evidence
    measured as ~2.6% worse final metric. It is zero while the refund holds, and becomes positive the
    moment a prefetch spends budget the run got nothing for (most dangerously by consuming an
    evaluation, since `is_unevaluated_speculative_discard` refuses to refund a build the log shows
    actually ran). Nobody has to run six GPU calibration runs to see that.

    TWO CLASSES OF HARM WERE INVISIBLE while this keyed only on linked nodes in ``failed``:

    * ABANDONED, not discarded. A committed prefetch that is still ``pending`` when the run finishes
      is charged by `node_counts_toward_card_budget` and never terminalized by
      `_drop_stale_speculation` (which only terminalizes STALE ones), so the whole depth's worth of
      slots — up to `speculation_depth`, which AUTO can resolve to one per GPU — went unreported
      whenever the consumer stopped admitting fresh prefetches: eval-seconds budget crossed, wall
      deadline reached, operator stop.
    * UNLINKED. A speculative node whose `card_build_done` link never landed (the "creation was
      rejected during replay" path) is charged by `card_budget_used` but is in neither
      ``state.speculative_nodes`` nor the refunded set — it was counted as no kind of outcome at all.

    So the universe is every speculative Node row plus every committed link, and the charged count is
    decided by the SAME predicate the budget itself uses (`node_counts_toward_card_budget`, which
    already excludes the refunded, the tombstoned and the gate-excluded). A second definition of
    "this build spent a slot" is exactly the drift this observation exists to catch.

    TOTAL, and it has to be: this is called from `finalize_run` inside a block whose failure silently
    skips BOTH the `budget` append and its `finalize_step` marker, which leaves `requirements_complete`
    false and a run that never acknowledges its own finalization. Every attribute read is defensive and
    every predicate is evaluated through `_total_predicate`, whose fallbacks all point the same way:
    an unreadable state may over-report `charged_discards`, never hand out a false all-clear.
    """

    raw_nodes = getattr(state, "nodes", None)
    nodes: Mapping = raw_nodes if isinstance(raw_nodes, Mapping) else {}
    raw_links = getattr(state, "speculative_nodes", None)
    linked_ids = set(raw_links) if isinstance(raw_links, (Mapping, set, frozenset)) else set()
    raw_outcomes = getattr(state, "card_build_outcomes", None)
    outcomes = list(raw_outcomes) if isinstance(raw_outcomes, (list, tuple)) else []
    speculative_ids = linked_ids | {
        node_id for node_id, node in nodes.items()
        if getattr(node, "speculative", False) is True
    }

    def _with_status(status) -> set:
        return {
            node_id for node_id in speculative_ids
            if getattr(nodes.get(node_id), "status", None) is status
        }

    def _never_ran(node) -> bool:
        """Is there NO durable receipt that this node's evaluation ever started?

        Three independent facts, ANDed because each alone can be absent for a benign reason: the
        eval-start boundary (`node_eval_started`, absent on logs written before it existed), charged
        eval seconds (zero for a killed eval), and stage rows (written inside the terminal's own
        write-lock, so a killed eval has none). Exactly the receipts
        `is_unevaluated_speculative_discard` calls its execution CORROBORATION, so the two halves of
        this summary cannot disagree about what "never ran" means.
        """
        try:
            return not (
                getattr(node, "eval_started", False) is True
                or (getattr(node, "eval_seconds", 0) or 0) > 0
                or getattr(node, "stages", None)
            )
        except Exception:  # noqa: BLE001 — TOTAL (see the docstring): unreadable -> assume a discard
            return True

    def _discard_terminal(node) -> bool:
        try:
            reason = str(getattr(node, "error_reason", "") or "").strip().lower()
        except Exception:  # noqa: BLE001 — TOTAL: an unreadable reason falls back to the ran-test
            return False
        return reason in SPECULATION_DISCARD_REASONS

    # A DISCARD is a speculative build the BUILD LIFECYCLE threw away — not merely a speculative node
    # that failed. This used to be `_with_status(NodeStatus.failed)`, i.e. ANY failure, and with
    # speculation shipping ON that made `charged_discards` positive for every crashing experiment:
    # `/tmp/ll-s2b/run` reported `discarded: 1, charged_discards: 1` where the "discard" was node 0, a
    # real experiment that ran five evaluations and died on a CUDA device-side assert. The docstring
    # below sells `charged_discards` as the speculation regression signal that is "zero while the
    # refund holds"; a signal every ordinary crash trips measures crashes, not speculation.
    #
    # Two ways in, so the fail-safe direction is preserved everywhere it cost nothing. A terminal
    # written by the build lifecycle (`SPECULATION_DISCARD_REASONS`) is a discard whether or not it
    # had already burned evaluation time — that case, a prefetch superseded mid-eval, is the most
    # expensive one there is and `is_unevaluated_speculative_discard` deliberately refuses to refund
    # it. And a failed speculative node with NO execution receipt at all is a discard whatever its
    # reason says, which keeps every pre-dispatch failure this observation used to catch (e.g.
    # `parent_unavailable`) without having to enumerate them. The only thing that stops counting is
    # the case the defect was about: a speculative node that RAN and whose terminal is the
    # experiment's own outcome.
    discarded = {
        node_id for node_id in _with_status(NodeStatus.failed)
        if _discard_terminal(nodes.get(node_id)) or _never_ran(nodes.get(node_id))
    }
    abandoned = _with_status(NodeStatus.pending)
    evaluated = _with_status(NodeStatus.evaluated)
    refunded = {
        node_id for node_id in speculative_ids
        if _total_predicate(is_unevaluated_speculative_discard, state, nodes.get(node_id),
                            on_error=False)
    }
    charged = {
        node_id for node_id in (discarded | abandoned)
        if _total_predicate(node_counts_toward_card_budget, state, nodes.get(node_id),
                            on_error=True)
    }
    raw_requests = getattr(state, "card_build_requests", None)
    depth = getattr(state, "speculation_depth", 0)
    return {
        "depth": depth if type(depth) is int and 0 <= depth <= 64 else 0,
        "requested": len(raw_requests) if isinstance(raw_requests, (list, tuple)) else 0,
        "committed": outcomes.count("committed"),
        "stale": outcomes.count("stale"),
        "producer_failed": outcomes.count("producer_failed"),
        "evaluated": len(evaluated),
        "discarded": len(discarded),
        # Committed/created but never terminalized: at finalize this is a slot the run paid a
        # Developer call for and got nothing back from. Additive key; `charged_discards` covers it.
        "abandoned": len(abandoned),
        "refunded": len(refunded),
        "charged_discards": len(charged),
    }


def speculation_product_authority_digest(*, policy_scope: str, task_kind: str) -> str:
    """Run-start LANE token for a speculative run that carries no calibration receipt.

    Positive ``speculation_depth`` needs no receipt on a real workload (see the admission block in
    `engine/orchestrator.py`). A run that HAS speculated still records which lane produced its
    speculative prefix, so re-entry cannot reinterpret it as the other lane's: this token is what
    `_require_pinned_speculation_receipt` re-derives and compares. It is derived, never granted — an
    identity, not an authorization.

    Deliberately DISJOINT from a gate receipt's ``self_digest`` (a different preimage schema), so a
    receipt-authorized log and a product-lane log can never be resumed into each other's lane.

    Deliberately NOT bound to `speculation_implementation_digest`. That digest hashes every shipped
    Python file, so ANY source edit — a comment, a `pip install -U` — would revoke it, and a
    long-running Repo/GPU run could never be resumed. It is an EVIDENCE identity and belongs to the
    lanes that claim evidence; the product lane claims none. ``speculation_depth`` is likewise not
    part of it: the depth is pinned and compared as its own field, and AUTO may legitimately
    re-resolve on a differently sized box.
    """

    return speculation_product_authority_digests(
        policy_scope=policy_scope, task_kind=task_kind,
    )[0]


def speculation_product_authority_digests(
    *, policy_scope: str, task_kind: str,
) -> tuple[str, ...]:
    """The mintable product-lane token first, then the tokens re-entry still ACCEPTS.

    Only the head is ever written into a `run_started`.  The tail exists because the schema id had to
    be bumped for a preimage change that had already shipped (see
    `SPECULATION_PRODUCT_AUTHORITY_SCHEMA`): a run already underway recorded the old id over the
    current preimage, and refusing it would strand exactly the long-running real-workload runs the
    product lane exists to keep resumable.  Accepting a superseded IDENTITY is not accepting a
    superseded authorization — this token grants nothing.
    """

    if not policy_scope or len(policy_scope) > 64:
        raise ValueError("product authority requires a bounded policy scope")
    if len(task_kind) > 64:
        raise ValueError("product authority requires a bounded task kind")
    return tuple(
        _sha256(canonical_json({
            "schema": schema,
            "policy_scope": policy_scope,
            "task_kind": task_kind,
        }))
        for schema in (
            SPECULATION_PRODUCT_AUTHORITY_SCHEMA,
            *SPECULATION_PRODUCT_AUTHORITY_LEGACY_SCHEMAS,
        )
    )


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


def _semantic_source(raw: bytes, relative: str) -> bytes:
    """The bytes a Python file's MEANING reduces to: its parsed tree, comments and layout removed.

    Hashing raw source made a comment-only, reformatting or line-ending-conversion commit revoke
    every previously issued calibration receipt, even though runtime semantics were identical — the
    defect this module's own comment recorded, whose cost is an operational stop/resume outage plus
    six fresh GPU calibration runs after a documentation edit (doc 25 XP-07).

    `ast.dump` without attributes carries no line or column numbers, so blank lines, wrapping and
    trailing whitespace vanish; comments never reach the AST at all. Everything that can change what
    the process DOES survives — including docstrings, which are AST nodes and are deliberately kept:
    a tool's docstring is its agent-facing description, so editing one really can change a run.

    A file that does not parse falls back to its raw bytes. That is strictly conservative (it can
    only over-revoke, never under-revoke) and keeps the digest total: a syntactically broken shipped
    module must still be covered rather than silently excluded from the manifest.
    """
    try:
        return ast.dump(ast.parse(raw, filename=relative)).encode("utf-8")
    except (SyntaxError, ValueError, RecursionError):
        return raw


def _manifest_entry(relative: str, raw: bytes) -> dict:
    """One row of the implementation manifest, whose EVERY field derives from the same bytes.

    Kept as its own function because the row — not `_semantic_source` alone — is what the digest
    consumes: sizing `raw` beside a hash of the parsed tree would smuggle byte-for-byte sensitivity
    back in through the other field, and a comment-only edit would still revoke every receipt.
    """
    body = _semantic_source(raw, relative)
    return {"path": relative, "bytes": len(body), "sha256": _sha256(body)}


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
        manifest.append(_manifest_entry(relative, raw))
    if not 1 <= len(manifest) <= 1000:
        raise ValueError("implementation source manifest is empty or oversized")
    return _sha256(canonical_json({
        # v2: the per-file hash covers the PARSED module, not its raw bytes (see `_semantic_source`).
        # Bumped rather than reused because the same tree now yields a different digest — every
        # receipt issued under v1 is revoked ONCE by this change, which is correct and is the last
        # time a comment edit will do it.
        "schema": "looplab.speculation-implementation/v2",
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


def _note_unique(seen: set, key: Any, errors: list[str], message: str) -> None:
    """The no-clone rule: an identity already seen in another evidence lane is an error.

    Two statements, and the SECOND is the one that vanishes when this is written out by hand — as
    it was, six times: the key is recorded on every sighting, including the first.  A copy that
    checks membership but never records makes its own clone check permanently vacuous, and a
    vacuous check here admits six copies of ONE run as three independent replicate pairs.
    """
    if key in seen:
        errors.append(message)
    seen.add(key)


def _replicate_invariant(bound: Any, value: Any, errors: list[str], message: str) -> Any:
    """The replicate-invariant rule: the first pair BINDS the value, a later mismatch is an error.

    Returns the binding to keep.  A mismatch never re-binds — the admitted value stays the first
    pair's, so the receipt reports the envelope the corpus actually agreed on rather than the last
    dissenting pair's.
    """
    if bound is None:
        return value
    if bound != value:
        errors.append(message)
    return bound


def _unavailable_aggregates(pair_count: int) -> dict[str, Any]:
    """The bounded fail-closed aggregate row: the counts only, every derived metric absent.

    Two paths reach it — an aggregate that will not compute, and a report that will not fit the
    receipt byte bound — and they used to spell the same seven keys twice.
    """
    return {
        "pair_count": pair_count,
        "valid_metric_pairs": 0,
        "mean_normalized_regret": None,
        "max_pair_normalized_regret": None,
        "mean_hit_rate": None,
        "max_pair_divergence_rate": None,
        "min_pair_coverage_ratio": None,
    }


def _scorer_fidelity_section() -> tuple[dict[str, Any], list[str]]:
    """Run the scorer-compatibility matrix and decide whether it admits a receipt.

    Returns the report body that goes into the receipt verbatim, plus the errors it raised.
    """
    errors: list[str] = []
    # This call is unconditional: malformed pair input must not bypass the scorer compatibility gate.
    try:
        raw_scorer = scorer_fidelity_gate()
        if not isinstance(raw_scorer, Mapping):
            raise ValueError("scorer fidelity report is not a mapping")
        scorer = dict(raw_scorer)
        scorer_bytes = canonical_json(scorer)
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
    return scorer, errors


def _gpu_evidence_section(
    require_gpu: object, gpu_inventory: object,
) -> tuple[bool, list[dict[str, Any]], list[str]]:
    """Settle the real-GPU requirement and the host inventory every lane is checked against.

    `require_gpu` comes back SETTLED: a non-boolean is refused and forced to the strict value, so a
    malformed argument can never be the thing that relaxes the inventory check below it.
    """
    errors: list[str] = []
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
    return require_gpu, inventory, errors


def _gate_identity_section(
    implementation_digest_fn: Callable[[], str] | None,
    environment_fingerprint: object,
) -> tuple[str, str, list[str]]:
    """The two host identities every lane's evidence must have been produced under.

    An identity that cannot be derived becomes the empty string, which no run report can equal —
    so an unavailable digest fails every pair rather than matching vacuously.
    """
    errors: list[str] = []
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
    return implementation_digest, environment_sha256, errors


class _ReplicateInvariants:
    """The cross-pair state one pair's evaluation reads and extends.

    These thirteen names were loop-carried locals of `speculation_quality_gate`, which is what kept
    the 160-line per-pair contract inside it.  Bundling them is what makes `_evaluate_pair` a
    function: the `seen_*` sets carry the no-clone rule across evidence lanes, the six bound fields
    carry the replicate-invariant rule across pairs, and `valid_pair_metrics` collects exactly the
    pairs the aggregates may be computed from.

    A plain `__init__` rather than a dataclass: `field` is already a loop variable elsewhere in this
    module, and the initializers below are the gate's own lines unchanged.
    """

    def __init__(self) -> None:
        self.seen_dirs: set[str] = set()
        self.seen_run_ids: set[str] = set()
        self.seen_event_digests: set[str] = set()
        self.seen_source_identities: set[tuple[str, str, str]] = set()
        self.seen_semantic_trajectories: set[str] = set()
        self.seen_calibration_seeds: set[int] = set()
        self.admitted_depth: int | None = None
        self.admitted_max_nodes: int | None = None
        self.runtime_scope_sha256: str | None = None
        self.task_profile_sha256: str | None = None
        self.replicate_comparable_config: dict[str, Any] | None = None
        self.replicate_provenance: dict[str, str] | None = None
        self.valid_pair_metrics: list[dict[str, float]] = []


def _evaluate_pair(
    pair_index: int,
    raw_pair: object,
    inv: _ReplicateInvariants,
    *,
    implementation_digest: str,
    environment_sha256: str,
    inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate ONE baseline/treatment pair against the fixed v1 contract; return its report.

    Every check appends to this pair's own error list and none of them raises: the contract is a
    complete verdict per pair, not its first failure.  The single containment `except` is the one
    the inline loop had, so a malformed run directory fails its own pair instead of the gate.
    """
    pair_errors: list[str] = []
    baseline_report: dict[str, Any] | None = None
    treatment_report: dict[str, Any] | None = None
    quality: dict[str, float] | None = None
    try:
        baseline_dir, treatment_dir = _normalize_pair(raw_pair)
        baseline_report, baseline_config, baseline_task = _analyze_speculation_run(baseline_dir)
        treatment_report, treatment_config, treatment_task = _analyze_speculation_run(treatment_dir)
        for report in (baseline_report, treatment_report):
            # Five spellings of ONE rule, in this order.  Each was three hand-written lines whose
            # `.add` half is the easy one to lose (see `_note_unique`): no identity of a completed
            # run may appear in a second evidence lane.
            _note_unique(inv.seen_dirs, _run_dir_identity(report["run_dir"]), pair_errors,
                         "run directory is reused across pairs")
            _note_unique(inv.seen_run_ids, report["run"]["run_id"], pair_errors,
                         "run_id is reused across evidence lanes")
            event_digest = report["sources"]["events"]["sha256"]
            _note_unique(inv.seen_event_digests, event_digest, pair_errors,
                         "events source is cloned across evidence lanes")
            _note_unique(inv.seen_source_identities,
                         (event_digest,
                          report["sources"]["config"]["sha256"],
                          report["sources"]["task"]["sha256"]), pair_errors,
                         "complete source identity is cloned across evidence lanes")
            _note_unique(inv.seen_semantic_trajectories,
                         report["sources"]["semantic_trajectory_sha256"], pair_errors,
                         "semantic execution trajectory is cloned across evidence lanes")
        if _run_dir_identity(baseline_report["run_dir"]) == _run_dir_identity(
            treatment_report["run_dir"]
        ):
            pair_errors.append("baseline and treatment must be distinct directories")
        if baseline_task != treatment_task:
            pair_errors.append("task.snapshot.json bytes differ inside pair")
        pair_task_profile = baseline_report["sources"]["task_profile_sha256"]
        if treatment_report["sources"]["task_profile_sha256"] != pair_task_profile:
            pair_errors.append("task profiles differ inside pair")
        inv.task_profile_sha256 = _replicate_invariant(
            inv.task_profile_sha256, pair_task_profile, pair_errors,
            "task profile differs across replicate pairs")
        if _comparable_config(baseline_config) != _comparable_config(treatment_config):
            pair_errors.append("pair configs differ outside allowed treatment fields")
        pair_comparable_config = _comparable_config(baseline_config)
        inv.replicate_comparable_config = _replicate_invariant(
            inv.replicate_comparable_config, pair_comparable_config, pair_errors,
            "comparable config differs across replicate pairs")

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
        if inv.replicate_provenance is None:
            inv.replicate_provenance = pair_provenance
        else:
            for material_key, material_digest in pair_provenance.items():
                if inv.replicate_provenance[material_key] != material_digest:
                    pair_errors.append(
                        f"{material_key.removesuffix('_sha256')} provenance differs "
                        "across replicate pairs"
                    )

        baseline_run = baseline_report["run"]
        treatment_run = treatment_report["run"]
        pair_seed = baseline_run["calibration_seed"]
        if treatment_run["calibration_seed"] != pair_seed:
            pair_errors.append("baseline and treatment calibration seeds differ")
        _note_unique(inv.seen_calibration_seeds, pair_seed, pair_errors,
                     "calibration seed is reused across replicate pairs")
        if baseline_run["finished"] is not True or treatment_run["finished"] is not True:
            pair_errors.append("both runs must be finished")
        if baseline_run["direction"] != treatment_run["direction"]:
            pair_errors.append("pair directions differ")
        if baseline_run["max_nodes"] != treatment_run["max_nodes"]:
            pair_errors.append("pair max_nodes differ")
        else:
            inv.admitted_max_nodes = _replicate_invariant(
                inv.admitted_max_nodes, baseline_run["max_nodes"], pair_errors,
                "max_nodes differs across replicate pairs")
        pair_runtime_scope = baseline_run["runtime_scope_sha256"]
        if treatment_run["runtime_scope_sha256"] != pair_runtime_scope:
            pair_errors.append("pair runtime scope digests differ")
        else:
            inv.runtime_scope_sha256 = _replicate_invariant(
                inv.runtime_scope_sha256, pair_runtime_scope, pair_errors,
                "runtime scope differs across replicate pairs")
        if baseline_run["card_driven_selection"] is not True:
            pair_errors.append("baseline card_driven_selection is not true")
        if treatment_run["card_driven_selection"] is not True:
            pair_errors.append("treatment card_driven_selection is not true")
        if baseline_run["speculation_depth"] != 0:
            pair_errors.append("baseline speculation_depth is not zero")
        if type(treatment_run["speculation_depth"]) is not int or treatment_run["speculation_depth"] <= 0:
            pair_errors.append("treatment speculation_depth is not positive")
        else:
            inv.admitted_depth = _replicate_invariant(
                inv.admitted_depth, treatment_run["speculation_depth"], pair_errors,
                "treatment speculation_depth differs across replicate pairs")
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
            inv.valid_pair_metrics.append(quality)
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
    return {
        "index": pair_index,
        "baseline": baseline_report,
        "treatment": treatment_report,
        "quality": quality,
        "errors": pair_errors,
        "passed": passed,
    }


def _quality_aggregates(
    pair_reports: list[dict[str, Any]], valid_pair_metrics: list[dict[str, float]],
) -> tuple[dict[str, Any], list[str]]:
    """Roll the per-pair quality metrics up, or report that they are not derivable."""
    errors: list[str] = []
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
        aggregates = _unavailable_aggregates(len(pair_reports))
    return aggregates, errors


def _aggregate_thresholds_pass(
    aggregates: Mapping[str, Any], *,
    pair_count: int, valid_metric_pairs: int, exact_pair_count: int,
) -> bool:
    """The fixed v1 aggregate admission, stated once over the numbers it reads.

    A COMPLETE corpus is part of the test, not a precondition of it: an absent metric or a missing
    pair fails here rather than being compared as ``None``.
    """
    return bool(
        pair_count == exact_pair_count
        and valid_metric_pairs == pair_count
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

    The phases below are ORDERED and the order is part of the contract: the scorer matrix runs
    before pair input is looked at, the two host identities are derived before any pair is compared
    against them, and the aggregates are read only from pairs that passed their own contract.
    `errors` is extended in that same order and deduplicated ONCE, at the receipt body — so a
    reordered phase is a changed receipt.
    """

    errors: list[str] = []
    pair_reports: list[dict[str, Any]] = []

    scorer, scorer_errors = _scorer_fidelity_section()
    errors.extend(scorer_errors)
    require_gpu, inventory, gpu_errors = _gpu_evidence_section(require_gpu, gpu_inventory)
    errors.extend(gpu_errors)
    implementation_digest, environment_sha256, identity_errors = _gate_identity_section(
        implementation_digest_fn, environment_fingerprint)
    errors.extend(identity_errors)

    # Read the PUBLISHED threshold, not the seed set directly, so the receipt's `min_pairs` row and
    # this check can never disagree.
    exact_pair_count = SPECULATION_QUALITY_THRESHOLDS["min_pairs"]
    if not isinstance(pairs, Sequence) or isinstance(pairs, (str, bytes)):
        pair_values: list[object] = []
        errors.append("pairs must be the exact bounded calibration sequence")
    elif len(pairs) != exact_pair_count:
        pair_values = list(pairs[:exact_pair_count])
        errors.append(f"pair count must be exactly {exact_pair_count}")
    else:
        pair_values = list(pairs)

    inv = _ReplicateInvariants()
    all_pair_contracts = len(pair_values) == exact_pair_count
    for pair_index, raw_pair in enumerate(pair_values):
        pair_report = _evaluate_pair(
            pair_index, raw_pair, inv,
            implementation_digest=implementation_digest,
            environment_sha256=environment_sha256,
            inventory=inventory,
        )
        all_pair_contracts = all_pair_contracts and pair_report["passed"]
        pair_reports.append(pair_report)

    if inv.seen_calibration_seeds != set(SPECULATION_CALIBRATION_SEEDS):
        errors.append(
            "calibration seed set must be exactly "
            f"{list(SPECULATION_CALIBRATION_SEEDS)}"
        )
        all_pair_contracts = False

    aggregates, aggregate_errors = _quality_aggregates(pair_reports, inv.valid_pair_metrics)
    errors.extend(aggregate_errors)
    aggregate_passed = _aggregate_thresholds_pass(
        aggregates,
        pair_count=len(pair_reports),
        valid_metric_pairs=len(inv.valid_pair_metrics),
        exact_pair_count=exact_pair_count,
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
        "task_profile_sha256": inv.task_profile_sha256 or "",
        "admitted_depth": inv.admitted_depth,
        "admitted_max_nodes": inv.admitted_max_nodes,
        "runtime_scope_sha256": inv.runtime_scope_sha256 or "",
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
    if len(canonical_json(body)) > _MAX_RECEIPT_BYTES:
        # Preserve a bounded fail-closed report rather than returning an attacker-sized object.
        body["pairs"] = []
        body["errors"] = ["gate report exceeds the receipt byte bound"]
        body["passed"] = False
        body["aggregates"] = _unavailable_aggregates(len(pair_reports))
    return body


def _self_digest(body: Mapping[str, Any]) -> str:
    return _sha256(canonical_json({key: value for key, value in body.items() if key != "self_digest"}))


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

    return publish_speculation_gate_receipt(path, speculation_quality_gate(
        pairs,
        require_gpu=require_gpu,
        gpu_inventory=gpu_inventory,
        implementation_digest_fn=implementation_digest_fn,
        environment_fingerprint=environment_fingerprint,
    ))


def publish_speculation_gate_receipt(
    path: str | Path, body: Mapping[str, Any],
) -> dict[str, Any]:
    """Self-digest and atomically publish an ALREADY-COMPUTED gate body; a failing one is refused.

    Split out of `write_speculation_gate_receipt` so a caller that has just run the gate — the
    `speculation-gate` CLI, which has to render a FAILING report before deciding to publish — does
    not run it a second time. On the real calibration corpus that second run re-parses six run
    directories (up to 64 MiB of events each), re-executes the scorer matrix, and re-derives the
    whole-source implementation digest; the CLI paid for all of it twice (doc 25 SE-01).

    Publishing from a body the caller computed is not a new forgery surface: a receipt's authority
    comes from `validated_speculation_gate_receipt` recomputing the entire gate from the raw run
    directories at READ time, not from who assembled the bytes at write time. `passed is not True`
    is still refused here, so the "failing evidence is never published" contract is unchanged.
    """
    if body.get("passed") is not True:
        raise ValueError("speculation quality gate did not pass; refusing to publish a receipt")
    receipt = {**body, "self_digest": _self_digest(body)}
    encoded = canonical_json(receipt)
    if len(encoded) + 1 > _MAX_RECEIPT_BYTES:
        raise ValueError("speculation gate receipt exceeds its byte bound")
    strict_atomic_write_text(path, encoded.decode("utf-8") + "\n")
    return receipt


def _receipt_mapping(path_or_mapping: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(path_or_mapping, Mapping):
        encoded = canonical_json(path_or_mapping)
        if len(encoded) > _MAX_RECEIPT_BYTES:
            raise ValueError("receipt mapping is oversized")
        decoded = _json_loads(encoded)
    else:
        decoded = _json_loads(
            _read_bounded(Path(path_or_mapping), limit=_MAX_RECEIPT_BYTES, label="gate receipt"))
    if not isinstance(decoded, dict):
        raise ValueError("gate receipt must be an object")
    return decoded


def speculation_gate_receipt_rejection(
    path_or_mapping: str | Path | Mapping[str, Any],
    *,
    gpu_inventory: object = None,
    implementation_digest_fn: Callable[[], str] | None = None,
    environment_fingerprint: object = None,
) -> tuple[dict[str, Any] | None, str]:
    """``(revalidated receipt, "")`` — or ``(None, the one invariant it failed)``.

    THE ORDERED CHECKLIST BELOW IS THE VALIDATOR ITSELF, not a second copy of it:
    `validated_speculation_gate_receipt` is this function's first element and nothing else, so the
    reason and the verdict can never disagree about why a receipt was refused. Short-circuiting at
    the FIRST failure is deliberate and preserves the historical cost exactly — a receipt whose
    schema is wrong must not go on to re-parse six run directories to collect a second complaint.

    WHY A REASON EXISTS AT ALL. A receipt is revoked by at least four independent identities, and a
    bare `None` says which one moved about as well as a closed door explains a lock. Measured on
    this box's own issued receipt (2026-08-04) against master on 2026-08-14, all four had moved:
    the `Settings` field set the archived `config.snapshot.json` files are compared against (17
    fields added, 1 removed — and archived evidence can never gain a key, so this one is
    unrepairable except by re-running the calibration), the installed-distribution fingerprint, the
    whole-source implementation digest, and the box's visible GPU inventory. Two separate agents
    reading the same code that week each reported the SOURCE DIGEST as the cause; it is in fact the
    only one of the four that a code change can even influence, and pinning both volatile identity
    seams by hand still left the receipt refused. Diagnosing that took a scripted bisection of a
    function whose whole answer was `None`, which is the defect this return value closes.

    The reason is a DIAGNOSTIC, never an authorization: no caller may branch on its text, and the
    verdict is the first element in every case.
    """

    try:
        receipt = _receipt_mapping(path_or_mapping)
        if set(receipt) != _RECEIPT_FIELDS:
            return None, "receipt field set differs from the current schema"
        if receipt.get("schema") != SPECULATION_QUALITY_GATE_SCHEMA:
            return None, (
                f"receipt schema is {receipt.get('schema')!r}, "
                f"expected {SPECULATION_QUALITY_GATE_SCHEMA!r}"
            )
        if receipt.get("thresholds") != dict(SPECULATION_QUALITY_THRESHOLDS):
            return None, "receipt thresholds differ from the shipped fixed thresholds"
        if type(receipt.get("require_gpu")) is not bool:
            return None, "receipt require_gpu is not a boolean"
        if not _valid_digest(receipt.get("self_digest")):
            return None, "receipt self_digest is not a SHA-256 digest reference"
        if receipt["self_digest"] != _self_digest(receipt):
            return None, "receipt self_digest does not cover its own body"
        # Both identities are computed ONCE here and handed to the recomputation below, which would
        # otherwise derive each a second time inside `speculation_quality_gate`. Neither is cheap:
        # the implementation digest reads and PARSES every shipped `.py`, and the environment
        # fingerprint walks the installed distributions — so one validation used to do both twice
        # (doc 25 SE-01). Passing them down is also strictly more correct than recomputing: a tree or
        # environment that changed between the two derivations can no longer make the receipt fail a
        # comparison against an identity that no longer exists.
        current_implementation = _implementation_digest(implementation_digest_fn)
        if receipt.get("implementation_digest") != current_implementation:
            return None, (
                "implementation digest moved: the receipt was earned on "
                f"{receipt.get('implementation_digest')} and this tree is {current_implementation} "
                "(re-run the calibration; see `speculation_implementation_digest`)"
            )
        current_fingerprint = (
            speculation_environment_fingerprint()
            if environment_fingerprint is None else environment_fingerprint
        )
        # A caller may supply the seam as a CALLABLE (`_environment_digest` resolves one). Resolve it
        # here so the digest compared above and the fingerprint handed to the recomputation are the
        # same VALUE — a callable invoked twice is exactly the second derivation this hoist removes,
        # and one that answered differently each time would compare against an identity the
        # recomputation never saw.
        if callable(current_fingerprint):
            current_fingerprint = current_fingerprint()
        current_environment = _environment_digest(current_fingerprint)
        if receipt.get("environment_sha256") != current_environment:
            return None, (
                "environment fingerprint moved: the receipt was earned under "
                f"{receipt.get('environment_sha256')} and this box is {current_environment} "
                "(an installed distribution or interpreter changed)"
            )
        if receipt.get("policy_scope") != SPECULATION_POLICY_SCOPE:
            return None, (
                f"receipt policy scope is {receipt.get('policy_scope')!r}, "
                f"expected {SPECULATION_POLICY_SCOPE!r}"
            )
        if receipt.get("workload_scope") != SPECULATION_WORKLOAD_SCOPE:
            return None, (
                f"receipt workload scope is {receipt.get('workload_scope')!r}, "
                f"expected {SPECULATION_WORKLOAD_SCOPE!r}"
            )
        if receipt.get("calibration_seeds") != list(SPECULATION_CALIBRATION_SEEDS):
            return None, "receipt calibration seeds differ from the shipped fixed seeds"
        if not _valid_digest(receipt.get("task_profile_sha256")):
            return None, "receipt task_profile_sha256 is not a SHA-256 digest reference"
        admitted_depth = receipt.get("admitted_depth")
        if type(admitted_depth) is not int or not 1 <= admitted_depth <= 64:
            return None, "receipt admitted_depth is not an integer in 1..64"
        admitted_max_nodes = receipt.get("admitted_max_nodes")
        if type(admitted_max_nodes) is not int or not 1 <= admitted_max_nodes <= 64:
            return None, "receipt admitted_max_nodes is not an integer in 1..64"
        if not _valid_digest(receipt.get("runtime_scope_sha256")):
            return None, "receipt runtime_scope_sha256 is not a SHA-256 digest reference"
        if not _valid_digest(receipt.get("calibration_profile_digest")):
            return None, "receipt calibration_profile_digest is not a SHA-256 digest reference"
        rows = receipt.get("pairs")
        if (
            not isinstance(rows, list)
            or len(rows) != len(SPECULATION_CALIBRATION_SEEDS)
        ):
            return None, (
                "receipt does not carry exactly one evidence pair per calibration seed"
            )
        source_pairs: list[tuple[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                return None, "receipt evidence pair is not an object"
            baseline = row.get("baseline")
            treatment = row.get("treatment")
            if not isinstance(baseline, dict) or not isinstance(treatment, dict):
                return None, "receipt evidence pair is missing a baseline/treatment side"
            baseline_dir = baseline.get("run_dir")
            treatment_dir = treatment.get("run_dir")
            if (
                not isinstance(baseline_dir, str)
                or not isinstance(treatment_dir, str)
                or len(baseline_dir) > _MAX_PATH_CHARS
                or len(treatment_dir) > _MAX_PATH_CHARS
            ):
                return None, "receipt evidence pair names an invalid or oversized run directory"
            source_pairs.append((baseline_dir, treatment_dir))

        # The two identities validated above, forwarded rather than re-derived — see the note there.
        # `implementation_digest_fn` is a seam returning a digest, so the already-validated digest is
        # handed back through the same seam; the gate re-checks its shape exactly as before.
        recomputed = speculation_quality_gate(
            source_pairs,
            require_gpu=receipt["require_gpu"],
            gpu_inventory=gpu_inventory,
            implementation_digest_fn=lambda: current_implementation,
            environment_fingerprint=current_fingerprint,
        )
        stored_body = {key: value for key, value in receipt.items() if key != "self_digest"}
        # Equality covers every source digest and raw metric. `passed` is not consulted until after the
        # recomputation has independently crossed all fixed constants.
        if (
            recomputed.get("passed") is True
            and canonical_json(stored_body) == canonical_json(recomputed)
        ):
            return dict(receipt), ""
        # The recomputation's own errors were previously discarded, and BOTH levels have to be read
        # or the diagnosis is worse than useless. The top-level list is only the phase-order summary
        # ("calibration seed set must be exactly [0, 1, 2]") — a downstream consequence of whatever
        # actually went wrong, phrased as though the seeds were edited. What names the real cause
        # lives PER PAIR, and it is where the axis this module can least repair shows up: an archived
        # `config.snapshot.json` compared against a `Settings` field set that has moved on since the
        # evidence was written (`_validate_calibration_setup`). Evidence already on disk can never
        # gain a key, so that one is unrepairable except by re-running the calibration — which is
        # exactly the fact a bare summary hid. Bounded: this string reaches a `ConfigRefusal`.
        detail = [str(item) for item in (recomputed.get("errors") or [])[:3]]
        for index, row in enumerate(recomputed.get("pairs") or []):
            pair_errors = row.get("errors") if isinstance(row, dict) else None
            if pair_errors:
                detail.append(f"pair {index}: {pair_errors[0]}")
                break
        return None, (
            "recomputation from the receipt's own run directories disagrees: "
            + ("; ".join(detail) if detail
               else "the stored body differs from the recomputation")
        )[:600]
    except Exception as exc:
        # The reason is diagnostic only, so an unexpected failure NAMES itself rather than being
        # flattened into the same silence every other rejection used to share.
        return None, f"receipt could not be revalidated: {type(exc).__name__}: {exc}"[:600]


def validated_speculation_gate_receipt(
    path_or_mapping: str | Path | Mapping[str, Any],
    *,
    gpu_inventory: object = None,
    implementation_digest_fn: Callable[[], str] | None = None,
    environment_fingerprint: object = None,
) -> dict[str, Any] | None:
    """Return an independently revalidated passing receipt, or ``None``.

    Engine wiring can pin the returned ``self_digest`` without re-parsing an untrusted mapping.  The
    public boolean validator below remains the convenient yes/no boundary.  The verdict is the
    ordered checklist in `speculation_gate_receipt_rejection` and nothing else — a caller that also
    wants to TELL the operator which invariant failed must call that one instead of calling this and
    then re-deriving a reason, which would re-parse every shipped `.py` and all six run directories
    a second time (doc 25 SE-01).
    """

    return speculation_gate_receipt_rejection(
        path_or_mapping,
        gpu_inventory=gpu_inventory,
        implementation_digest_fn=implementation_digest_fn,
        environment_fingerprint=environment_fingerprint,
    )[0]


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
    "SPECULATION_PRODUCT_AUTHORITY_LEGACY_SCHEMAS",
    "SPECULATION_PRODUCT_AUTHORITY_SCHEMA",
    "SPECULATION_QUALITY_GATE_SCHEMA",
    "SPECULATION_QUALITY_THRESHOLDS",
    "SPECULATION_RUN_ANALYSIS_SCHEMA",
    "analyze_speculation_run",
    "publish_speculation_gate_receipt",
    "speculation_budget_observation",
    "speculation_environment_fingerprint",
    "speculation_gate_receipt_rejection",
    "speculation_implementation_digest",
    "speculation_product_authority_digest",
    "speculation_product_authority_digests",
    "speculation_quality_gate",
    "speculation_task_profile_digest",
    "validate_speculation_gate_receipt",
    "validated_speculation_gate_receipt",
    "write_speculation_gate_receipt",
]
