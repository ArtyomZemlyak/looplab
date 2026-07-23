"""Engine / control loop (I6, ADR-12/18). anyio structured concurrency:
node *creation* is sequential & deterministic; node *evaluation* fans out under a
CapacityLimiter. State is always a fresh fold of the log (files-as-truth); resume
is just re-entering this loop on an existing run dir — pending nodes get re-evaluated
idempotently, and node ids are a monotonic count so reruns never duplicate.

A crash can be injected (for the resume test) via `crash_after`: hard-exit after N
node_evaluated events have been written, simulating `kill -9` mid-run.
"""
from __future__ import annotations

import dataclasses
import functools
import hashlib
import math
import os
import secrets
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple, Optional

import anyio
import orjson

from looplab.tools.agents_md import generate_agents_md
from looplab.events.eventstore import EventStore, EventStoreConcurrencyError
from looplab.events.types import (
    EV_ABLATE,
    EV_APPROVAL_REQUESTED,
    EV_COMMAND_ACK,
    EV_CARD_ADDED, EV_CARD_AUTO_DROPPED, EV_CARD_DROPPED, EV_CARD_MERGED,
    EV_DATA_PROFILED, EV_DATA_PROVENANCE,
    EV_DRIFT_UNAVAILABLE, EV_FORK_DONE, EV_HOST_GRADING,
    EV_INJECT_DONE, EV_INJECT_FAILED,
    EV_FINALIZE_STEP,
    EV_NODE_BUILDING,
    EV_HYPOTHESIS_MERGED, EV_NODE_FAILED, EV_PAUSE,
    EV_NOVELTY_REJECTED,
    EV_POLICY_DECISION,
    EV_REPORT_GENERATED,
    EV_RESUME_SERVED, EV_RUN_ABORT, EV_RUN_FINISHED,
    EV_RUN_STARTED, EV_RUNG_PROMOTED,
    EV_SETUP_FINISHED, EV_SETUP_STARTED, EV_SETUP_STEP, EV_SPEC_APPROVAL_REQUESTED,
    EV_SPEC_APPROVED, EV_SPEC_PROPOSED,
    EV_ENV_CHANGED, EV_WORKSPACE_CHANGED)
from looplab.engine.ablation import AblationMixin
from looplab.engine.audit import AuditMixin
from looplab.engine.confirm_phase import ConfirmPhaseMixin
from looplab.engine.costs import bind_cost_accountants
from looplab.engine.crash_repair import CrashRepairMixin
from looplab.engine.eval_dispatch import EvalDispatchMixin
from looplab.engine.eval_stages import EvalStagesMixin
from looplab.engine.evaluate import EvaluateMixin
from looplab.engine.node_build import NodeBuildMixin
from looplab.engine.proposal_cues import ProposalCuesMixin, normalize_steering_context
from looplab.engine.resources import (ResourceSchedulingMixin, cuda_visible_device_tokens,
                                      default_gpu_host_lease_path, detect_gpu_inventory)
from looplab.engine.speculation import SpeculationMixin
from looplab.engine.train_monitor import TrainingMonitorMixin
from looplab.engine.asha_monitor import AshaMonitorMixin
from looplab.engine.novelty import NoveltyGateMixin
from looplab.engine.strategy import StrategyCadenceMixin
from looplab.engine.research_cadence import ResearchCadenceMixin
from looplab.engine.finalize import (
    ensure_finish_report,
    finalize_run,
    finalize_scope_quiescent,
    incomplete_finalize_scope,
    mark_finish_report_complete,
    scoped_finish_report,
)
from looplab.engine.holdout import HoldoutGrader
from looplab.engine.lessons import LessonMemory
from looplab.engine.options import EngineOptions
from looplab.engine.workspace import WorkspaceSeeder
# Pure triage/fingerprint helpers extracted to looplab/engine/triage.py, imported back under
# their original names so `looplab.engine.orchestrator._normalize_error_sig`, `._holdout_indices`
# (& friends) stay importable — tests import them from this module path.
from looplab.engine.triage import (_MAX_DEP_ROUNDS, _MECHANICAL_MARKERS,  # noqa: F401
                                   _dir_fingerprint, _failure_reason, _holdout_indices,
                                   _normalize_error_sig, _rule_triage, _shallow_fingerprint)
from looplab.core.models import (
    Idea, Node, NodeStatus, RunState, card_action_digest, card_ownership_receipt,
    durable_idea_payload, idea_proposal_ref, normalize_researcher_footprint,
)
from looplab.core.advisory_payloads import bounded_cross_run_advisory_receipt
from looplab.core.config import RUN_START_PINNED_FIELDS, Settings
from looplab.core.fitness import VERIFIER_SELECTION_CONTRACT
from looplab.core.llm_broker import (LLMConcurrencyBroker, default_llm_lane_limits,
                                     in_llm_lane, llm_broker_scope, llm_lane_scope)
from looplab.search.card_selection import (
    META_CARD_ID, card_action as projected_card_action, card_budget_used,
    card_next_actions, card_selection_set, eligible_cards, forced_card_actions,
    speculative_raw_actions,
)
from looplab.search.speculation_calibration import (
    SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS,
    SPECULATION_CALIBRATION_SEEDS,
    SPECULATION_POLICY_SCOPE,
    canonical_speculation_toy_task,
    speculation_runtime_scope_digest,
)
from looplab.search.operators import merge_idea
from looplab.search.policy import KIND_EXPAND, SearchPolicy
# The strategist-cadence cluster (StrategyContext / make_policy / validate_strategy / coverage_signal
# / run_phase / operator_yields / NOVELTY_STANCES …) moved to engine/strategy.py (StrategyCadenceMixin),
# which imports those symbols from their canonical sources — so they are no longer imported here.
from looplab.core.profile import profile_dataset
from looplab.events.replay import fold
from looplab.agents.roles import Developer, Researcher
from looplab.runtime.sandbox import Sandbox
from looplab.core.tracing import JsonlSpanExporter, Tracer

# Re-export (back-compat): the engine sentinel lives in engine/options.py since the F3 knob
# collapse (the signature takes **knobs now, so the orchestrator itself no longer needs it);
# kept importable from this module path for pre-collapse importers.
from looplab.engine.options import _UNSET  # noqa: F401

# P0-5 dirty-input diff digest: the byte ceiling on how much of `git diff HEAD` is hashed before the
# digest is marked truncated (`~`). A real code diff is far under this; beyond it we're diffing a
# tracked data/generated file, where buffering the whole patch would spike run-start memory (a latent
# OOM) and a truncated "did-it-change" signal is enough. Module-level so an operator/test can retune.
_DIFF_DIGEST_CAP = 8 * 1024 * 1024

# Back-compatible export: the source-owned definition lives beside the shared runtime-scope digest.
SPECULATION_CALIBRATION_VARIANT_FIELDS = SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS


class SpeculationAuthorizationError(RuntimeError):
    """A durable speculation prefix cannot be re-entered under the current evidence authority.

    This is deliberately distinct from an ordinary fatal engine error.  CLI fatal-error recovery
    writes terminal events, while an authorization failure must return without changing the log it
    refused to trust.
    """


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
}
SPECULATION_CALIBRATION_PROFILE_SETTINGS = _declared_settings_json_defaults()
SPECULATION_CALIBRATION_PROFILE_SETTINGS.update(
    _SPECULATION_CALIBRATION_PROFILE_OVERRIDES)
for _variant_field in SPECULATION_CALIBRATION_VARIANT_FIELDS:
    SPECULATION_CALIBRATION_PROFILE_SETTINGS.pop(_variant_field, None)
_expected_calibration_profile_fields = (
    set(Settings.model_fields) - set(SPECULATION_CALIBRATION_VARIANT_FIELDS))
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
def _stable_effective_gpu_inventory(raw) -> list[dict]:
    """Canonical, resume-stable projection of ``effective_gpu_inventory``.

    Free memory is deliberately excluded: it changes while unrelated jobs start and is not machine
    identity.  The effective helper already applies CUDA_VISIBLE_DEVICES; this projection preserves its
    logical indices and stable hardware identity only.
    """
    if not isinstance(raw, list):
        return []
    stable: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            return []
        index = item.get("index")
        uuid = item.get("uuid")
        pci_bus_id = item.get("pci_bus_id")
        name = item.get("name")
        total = item.get("mem_total_mib")
        driver_version = item.get("driver_version")
        cuda_driver_version = item.get("cuda_driver_version")
        if (type(index) is not int or index < 0 or not isinstance(name, str)
                or not name.strip() or type(total) is not int or total <= 0
                or not isinstance(uuid, str) or not uuid.strip()
                or not isinstance(pci_bus_id, str) or not pci_bus_id.strip()
                or not isinstance(driver_version, str) or not driver_version.strip()
                or type(cuda_driver_version) is not int or cuda_driver_version <= 0):
            return []
        stable.append({
            "index": index,
            "uuid": uuid.strip(),
            "pci_bus_id": pci_bus_id.strip(),
            "name": name.strip(),
            "mem_total_mib": total,
            "driver_version": driver_version.strip(),
            "cuda_driver_version": cuda_driver_version,
        })
    stable.sort(key=lambda row: row["index"])
    if (
        len({row["index"] for row in stable}) != len(stable)
        or len({row["uuid"] for row in stable}) != len(stable)
        or len({row["pci_bus_id"] for row in stable}) != len(stable)
    ):
        return []
    return stable


def _calibration_role_pair_errors(task, researcher, developer) -> list[str]:
    """Validate the two default-off purpose flags without accepting wrappers/subclasses."""
    from looplab.agents.roles import ToyObjectiveDeveloper, ToyResearcher

    errors: list[str] = []
    if type(researcher) is not ToyResearcher:  # exact: a wrapper could make live/model calls
        errors.append("researcher must be the exact ToyResearcher")
    else:
        if getattr(researcher, "calibration_concepts", False) is not True:
            errors.append("ToyResearcher.calibration_concepts must be true")
        if (researcher.bounds != task.bounds or researcher.step != task.step
                or researcher.seed != task.seed):
            errors.append("ToyResearcher must match the calibrated task bounds/step/seed")
    if type(developer) is not ToyObjectiveDeveloper:
        errors.append("developer must be the exact ToyObjectiveDeveloper")
    else:
        if getattr(developer, "calibration_gpu_probe", False) is not True:
            errors.append("ToyObjectiveDeveloper.calibration_gpu_probe must be true")
        if developer.noise != 0.0:
            errors.append("ToyObjectiveDeveloper noise must be zero")
    return errors


class _BuildReservation(NamedTuple):
    """Durable node/card reservation handed from the main task to one build worker.

    The first five positions preserve the historical internal tuple layout, so focused tests and
    integrations that only read ``reservation[1]`` (the node id) keep working.  The final two fields
    carry the exact native Card identity and already-prepared Idea; no worker is allowed to mint or
    re-propose after the main task has committed the reservation.
    """

    state: RunState
    node_id: int
    kind: str
    parent_ids: list[int]
    parent_generations: dict[str, int]
    card_id: Optional[str]
    idea: Optional[Idea]


class _CardReservationPlan(NamedTuple):
    """Pure result of resolving one exact native Card identity against the journal."""

    disposition: str  # mint | reuse | duplicate | invalid
    card_id: Optional[str]
    idea: Optional[Idea]
    payload: Optional[dict]


class _InjectedNodePlan(NamedTuple):
    """Pure, bounded preparation result for one operator-authored Node request."""

    idea: Idea
    parent_ids: list[int]
    parent_generations: dict[str, int]
    code: Optional[str]
    implementation_ref: Optional[str]


def _detect_gpu_ids() -> list[int]:
    """Best-effort list of usable GPU ordinals for the per-eval GPU pinning + `max_parallel=0` AUTO
    (evaluate.py). Honors an existing `CUDA_VISIBLE_DEVICES` (respect an operator/scheduler that already
    fenced the box), else asks torch, else `nvidia-smi -L`. Returns [] when there is no GPU (CPU box /
    detection unavailable) — the caller then simply never pins and AUTO collapses to 1. Never raises."""
    cvd = os.environ.get("CUDA_VISIBLE_DEVICES")
    if cvd is not None:
        ids = cuda_visible_device_tokens(cvd) or []
        # Ordinals INSIDE this fenced view are 0..n-1 regardless of the physical ids named in the var.
        return list(range(len(ids)))
    try:
        import torch  # optional
        n = int(torch.cuda.device_count())
        if n > 0:
            return list(range(n))
    except Exception:  # noqa: BLE001 — torch missing / driver error -> fall through
        pass
    try:
        import subprocess
        out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            n = sum(1 for line in out.stdout.splitlines() if line.strip().startswith("GPU "))
            return list(range(n))
    except Exception:  # noqa: BLE001
        pass
    return []


# The confirm phase (engine/confirm_phase.py) and ablation (engine/ablation.py) clusters are
# MIXINS — pure file-level moves inherited unchanged, so every `self._confirm_phase(...)` /
# `self._ablate(...)` call site (and every test poking those names on Engine) is untouched.
class Engine(ConfirmPhaseMixin, AblationMixin, NoveltyGateMixin, StrategyCadenceMixin,
             ResearchCadenceMixin, EvalStagesMixin, CrashRepairMixin, EvalDispatchMixin,
             AuditMixin, ResourceSchedulingMixin, SpeculationMixin, EvaluateMixin, NodeBuildMixin,
             ProposalCuesMixin,
             TrainingMonitorMixin, AshaMonitorMixin):
    @property
    def max_parallel(self) -> int:
        """Deprecated read-through alias for the canonical evaluation width.

        Keep the descriptor instead of a second instance attribute: integrations may continue to
        read or assign ``max_parallel``, but there is only one live value and new runtime code cannot
        observe a stale legacy copy.
        """
        return self._eval_parallel

    @max_parallel.setter
    def max_parallel(self, value: int) -> None:
        self._eval_parallel = value

    @property
    def parallel_build(self) -> int:
        """Deprecated read-through alias for the canonical LLM/build width."""
        return self._llm_parallel

    @parallel_build.setter
    def parallel_build(self, value: int) -> None:
        self._llm_parallel = value

    def __init__(
        self,
        run_dir: str | os.PathLike,
        *,
        task,
        researcher: Researcher,
        developer: Developer,
        sandbox: Sandbox,
        policy: SearchPolicy,
        options: Optional[EngineOptions] = None,
        crash_after: Optional[int] = None,
        onboarder=None,
        # --- A7 Strategist + richer-operator knobs (config-first; defaults == today's behavior) ---
        strategist=None,            # Optional[Strategist]; None => static config policy (default)
        deep_researcher=None,       # Optional[DeepResearcher]; None => Deep-Research stage off
        report_writer=None,         # Optional[ReportWriter]; None => agent report off (deterministic only)
        developer_factory=None,     # Optional[Callable[[str], Developer]] for live backend swap
        role_factory=None,          # Variant-1: Optional[Callable[[], (Researcher, Developer)]] building a
        #                             FRESH wired role pair for a parallel build worker (None => no pool =>
        #                             parallel_build clamps to 1). Typically `lambda: make_roles(task, settings)`.
        proxy_scorer=None,          # A6: Optional[ProxyScorer] early-signal candidate gate
        dep_installer=None,                  # Optional[Callable] install hook (test seam; default = deps.install)
        # D1 holdout-gated promotion (B6): reserve a fraction of host-held labels as a FINAL
        # holdout partition the search never sees; at finish, re-score the val-top-k on it and
        # (when holdout_select) let the unseen signal pick the champion. Host-graded tasks only
        # (label-partition holdout is free — the predictions already exist); 0.0 = off.
        # Phase 2 (D3/D4/T10/P4) knobs — kept on the engine so strategist-driven policy swaps
        # rebuild policies with the same run-wide settings.
        embedder=None,                       # text→vector callable (default: zero-dep hash_embed)
        lesson_abstractor=None,              # Memora synergy: harmonic recall over cross-run lessons
        _speculation_gate_calibration: bool = False,  # private mechanics-test/bootstrap seam
        _speculation_runtime_scope_sha256: Optional[str] = None,
        # Private CLI→Engine provenance seam. Narrow calibration/receipt paths independently
        # reconstruct this digest from their source-owned full Settings profile before trusting it.
        # BACKLOG §4 (docs/15 F3): every PURE-CONFIG knob — one per EngineOptions field — is
        # accepted via **knobs and validated against EngineOptions, so adding a knob is TWO edits
        # (Settings field + EngineOptions field) instead of four. Each knob's type/default/why
        # lives on EngineOptions (engine/options.py), which mirrors the old signature comments.
        # Resolution per knob (unchanged): explicitly passed kwarg > `options` field > default.
        **knobs,
    ):
        # Resolve each pure-config knob ONCE, up front — explicit kwarg > options field > default —
        # so the assignment/validation body below is exactly the pre-EngineOptions code operating on
        # plain locals (no behavior change, no re-plumbing of the ~100 keyword call sites).
        if options is None:
            options = EngineOptions()
        # Unknown knob -> TypeError, exactly like a real keyword (a typo'd knob must not silently
        # fall back to the default). The field set IS EngineOptions — verified 1:1 by
        # tests/test_engine_options.py + tests/test_options_divergence.py.
        _fields = {f.name for f in dataclasses.fields(EngineOptions)}
        _bad = set(knobs) - _fields
        if _bad:
            raise TypeError(f"Engine() got unexpected keyword argument(s): {sorted(_bad)}")

        def _opt(field: str):
            return knobs[field] if field in knobs else getattr(options, field)

        # Layer-2 decoupling (docs/23): the CANONICAL `eval_parallel`/`llm_parallel` win over the legacy
        # `max_parallel`/`parallel_build` when set; None => fall back to the legacy field => byte-identical.
        _eval_parallel_opt = _opt("eval_parallel")
        _eval_parallel_value = (_eval_parallel_opt if _eval_parallel_opt is not None
                                else _opt("max_parallel"))
        _llm_parallel_opt = _opt("llm_parallel")
        _llm_parallel_value = (_llm_parallel_opt if _llm_parallel_opt is not None
                               else _opt("parallel_build"))
        train_monitor = _opt("train_monitor")
        train_monitor_interval_s = _opt("train_monitor_interval_s")
        train_monitor_kill = _opt("train_monitor_kill")
        train_monitor_kill_confidence = _opt("train_monitor_kill_confidence")
        asha_live = _opt("asha_live")
        asha_live_kill = _opt("asha_live_kill")
        asha_live_quantile = _opt("asha_live_quantile")
        asha_live_min_siblings = _opt("asha_live_min_siblings")
        timeout = _opt("timeout")
        sweep_timeout_mult = _opt("sweep_timeout_mult")
        confirm_top_k = _opt("confirm_top_k")
        confirm_seeds = _opt("confirm_seeds")
        confirm_seed_base = _opt("confirm_seed_base")
        max_seconds = _opt("max_seconds")
        max_eval_seconds = _opt("max_eval_seconds")
        memory_dir = _opt("memory_dir")
        require_approval = _opt("require_approval")
        archive_resolution = _opt("archive_resolution")
        coverage_context = _opt("coverage_context")
        concept_pivot = _opt("concept_pivot")
        graded_novelty = _opt("graded_novelty")
        capability_expansion = _opt("capability_expansion")
        fingerprint_universal = _opt("fingerprint_universal")
        cross_run_concepts = _opt("cross_run_concepts")
        concept_run_base = _opt("concept_run_base")
        cross_run_advisory = _opt("cross_run_advisory")
        cross_run_structured_claims = _opt("cross_run_structured_claims")
        cross_run_curation = _opt("cross_run_curation")
        cross_run_curation_auto = _opt("cross_run_curation_auto")
        cross_run_read_tools = _opt("cross_run_read_tools")
        phase_handoff_summary = _opt("phase_handoff_summary")
        eval_trust_mode = _opt("eval_trust_mode")
        trust_mode = _opt("trust_mode")
        docker_image = _opt("docker_image")
        sandbox_memory = _opt("sandbox_memory")
        sandbox_cpus = _opt("sandbox_cpus")
        seed_mode = _opt("seed_mode")
        n_seeds = _opt("n_seeds")
        max_nodes = _opt("max_nodes")
        policy_name = _opt("policy_name")
        ablate_every = _opt("ablate_every")
        strategist_every = _opt("strategist_every")
        concept_retag_every = _opt("concept_retag_every")
        deep_research_every = _opt("deep_research_every")
        concurrent_research = _opt("concurrent_research")
        concurrent_research_repeat = _opt("concurrent_research_repeat")
        concurrent_research_interval_s = _opt("concurrent_research_interval_s")
        concurrent_research_max_calls = _opt("concurrent_research_max_calls")
        concurrent_consolidate = _opt("concurrent_consolidate")
        report_every = _opt("report_every")
        merge_mode = _opt("merge_mode")
        complexity_cue = _opt("complexity_cue")
        budget_aware = _opt("budget_aware")
        failure_reflection = _opt("failure_reflection")
        watchdog_reflection = _opt("watchdog_reflection")
        deep_repair = _opt("deep_repair")
        localize_faults = _opt("localize_faults")
        feature_engineering = _opt("feature_engineering")
        ablate_code_blocks = _opt("ablate_code_blocks")
        proxy_kill_fraction = _opt("proxy_kill_fraction")
        reward_hack_detect = _opt("reward_hack_detect")
        trust_gate = _opt("trust_gate")
        code_leakage_detect = _opt("code_leakage_detect")
        critic_check = _opt("critic_check")
        redact_output = _opt("redact_output")
        novelty_mode = _opt("novelty_mode")
        novelty_gate = _opt("novelty_gate")
        novelty_epsilon = _opt("novelty_epsilon")
        reflection_priors = _opt("reflection_priors")
        comparative_lessons = _opt("comparative_lessons")
        lessons_every = _opt("lessons_every")
        lessons_refresh_every = _opt("lessons_refresh_every")
        track_hypotheses = _opt("track_hypotheses")
        surrogate_explore = _opt("surrogate_explore")
        unified_agent = _opt("unified_agent")
        agent_drives_actions = _opt("agent_drives_actions")
        card_driven_selection = _opt("card_driven_selection")
        speculation_depth = _opt("speculation_depth")
        speculation_gate_receipt = _opt("speculation_gate_receipt")
        inline_repair = _opt("inline_repair")
        inline_repair_attempts = _opt("inline_repair_attempts")
        inline_repair_stuck_repeat = _opt("inline_repair_stuck_repeat")
        inline_repair_reasons = _opt("inline_repair_reasons")
        inline_repair_retrain_cap = _opt("inline_repair_retrain_cap")
        auto_install_deps = _opt("auto_install_deps")
        dep_install_timeout = _opt("dep_install_timeout")
        agent_control = _opt("agent_control")
        holdout_fraction = _opt("holdout_fraction")
        holdout_select = _opt("holdout_select")
        holdout_top_k = _opt("holdout_top_k")
        select_verifier = _opt("select_verifier")
        verifier_ci_tie = _opt("verifier_ci_tie")
        select_verifier_samples = _opt("select_verifier_samples")
        debug_depth = _opt("debug_depth")
        operator_bandit = _opt("operator_bandit")
        novelty_semantic = _opt("novelty_semantic")
        novelty_semantic_threshold = _opt("novelty_semantic_threshold")
        digest_char_cap = _opt("digest_char_cap")
        research_verify = _opt("research_verify")
        workdir_audit = _opt("workdir_audit")

        self.run_dir = Path(run_dir)
        self.task = task
        self.researcher = researcher
        # P1: propagate the hypothesis-tracking knob to the researcher (LLMResearcher reads it;
        # UnifiedAgent forwards it to its inner researcher). Default-on already via the constructor;
        # this makes an explicit OFF reach the prompt. Best-effort (toy researchers ignore it).
        try:
            setattr(self.researcher, "track_hypotheses", track_hypotheses)
        except Exception:  # noqa: BLE001
            pass
        self.developer = developer
        self.sandbox = sandbox
        self.policy = policy
        # A7 Strategist: the policy is now hot-swappable, so the engine keeps the knobs needed to
        # rebuild it (n_seeds/max_nodes/ablate_every) + the meta-controller + operator-mix state.
        self.n_seeds = n_seeds
        self.max_nodes = max_nodes
        # The policy's OWN node budget is the base a live add_nodes override extends — NOT self.max_nodes
        # (the engine default can differ from a passed-in policy's, e.g. in tests). Tracked separately so
        # the override is applied idempotently (absolute set per iteration) without compounding, and
        # re-captured on a strategy-driven policy swap below.
        self._base_max_nodes = getattr(policy, "max_nodes", max_nodes)
        self._policy_name = policy_name
        self._ablate_every = ablate_every
        self.strategist = strategist
        self.strategist_every = max(1, strategist_every)
        self.concept_retag_every = max(1, concept_retag_every)
        self.deep_researcher = deep_researcher
        self.deep_research_every = max(0, deep_research_every)
        self.concurrent_research = concurrent_research
        # Repeated concurrent research (don't idle a multi-day eval): the overlapped think re-runs on
        # an adaptive time cadence for the whole window instead of once. Off in the library default
        # (one-shot == today); the product turns it on. Interval floors the budget-derived pace;
        # max_calls is a per-window LLM backstop. See _spawn_research / _research_overlap_loop.
        self._concurrent_research_repeat = bool(concurrent_research_repeat)
        self._concurrent_research_interval_s = max(1.0, float(concurrent_research_interval_s or 1800.0))
        self._concurrent_research_max_calls = max(0, int(concurrent_research_max_calls or 0))
        # Overlap the hypothesis-board consolidation with the eval too (dedup the board the repeated
        # research keeps filling). Off in the library default (== today); product turns it on.
        self._concurrent_consolidate = bool(concurrent_consolidate)
        self.report_writer = report_writer
        self.report_every = max(0, report_every)
        self.developer_factory = developer_factory
        self._developer_name = "default"
        # Variant-1 parallel BUILD: a pool of fresh (researcher, developer) pairs so N drafts research +
        # code CONCURRENTLY without clobbering each other's role state (developer.last_files, researcher
        # hints). The settled canonical LLM width is the fan-out; the pool is built lazily on
        # the first parallel batch and clamped to what `role_factory` can supply (None => stays serial).
        self.role_factory = role_factory
        # NB: draft builds fan out via anyio.to_thread, whose default capacity limiter is 40 threads;
        # a `parallel_build` above that (le=64) just queues the excess (no deadlock — workers never
        # re-enter the loop), so effective build concurrency silently caps near 40. The value is a raw
        # opt here (0 = AUTO); it is resolved against the settled `self._eval_parallel` further down.
        # Layer-2: the canonical `llm_parallel` wins over the legacy `parallel_build` when set.
        self._llm_parallel_startup_opt = _llm_parallel_value
        self._llm_parallel = max(1, self._llm_parallel_startup_opt)  # provisional; re-resolved below
        self._role_pool: Optional[list] = None
        # A successful live Developer swap owns every subsequent build worker too. None means the
        # CLI factory's configured backend is still authoritative; a string means pooled developers
        # must be rebuilt through developer_factory under that exact Strategist-selected backend.
        self._pool_developer_override: Optional[str] = None
        # A0b/T8: "auto" resolves by Developer capability — code recombination is the verified
        # strongest merge (removing it costs ~9 pp), so it is the default wherever the Developer
        # actually GENERATES code (LLM/agent backends declare `is_code_generating`); templated/toy
        # developers keep the legacy mean-param merge (a code ensemble is meaningless there).
        if merge_mode == "auto":
            merge_mode = ("ensemble" if getattr(developer, "is_code_generating", False)
                          else "mean")
        self._merge_mode = merge_mode
        self._complexity_cue = complexity_cue
        self._prefer_sweep = False   # A7: Strategist-set bias toward intra-node sweeps (audit-driven)
        self._budget_aware = budget_aware
        self._failure_reflection = failure_reflection
        self._watchdog_reflection = watchdog_reflection
        self._deep_repair = deep_repair
        # Hybrid in-node crash repair (triage + inline repair). See Settings.inline_repair.
        self._inline_repair = inline_repair
        self._inline_repair_attempts = max(0, int(inline_repair_attempts))   # 0 = unlimited
        self._inline_repair_stuck_repeat = max(2, int(inline_repair_stuck_repeat))
        self._inline_repair_reasons = tuple(inline_repair_reasons or ("crash",))
        self._inline_repair_retrain_cap = max(0, int(inline_repair_retrain_cap))
        # Environment self-prep (deps.py): auto-install a missing KNOWN library and re-run, instead
        # of letting the crash-triage agent reject the idea. Trusted_local tier ONLY — the Docker
        # tiers run --network none and must not mutate a shared image. `_dep_attempted` records every
        # module we've already run pip for THIS run (one attempt per module: success => now present
        # forever; failure => won't change on retry), so an offline/misnamed package can't loop.
        # `_dep_lock` serializes pip + that set across parallel evals (pip is not concurrency-safe).
        self._auto_install_deps = bool(auto_install_deps) and trust_mode == "trusted_local"
        self._dep_install_timeout = float(dep_install_timeout)
        self._dep_installer = dep_installer        # None => deps.install (real pip)
        self._dep_attempted: set[str] = set()
        import threading as _threading
        self._dep_lock = _threading.Lock()
        # Agent governance (Settings.agent_control): per-setting allow-list of which roles may change it
        # at runtime. A setting absent from the map is LOCKED (no agent). Enforced at the strategist /
        # boss / researcher seams via `_agent_may`. `None` (a bare Engine(...) with no options) resolves
        # to the SHIPPED default matrix — so a directly-constructed engine behaves like a real CLI run
        # (the EngineOptions "Engine() == shipped defaults" invariant); pass an explicit `{}` to lock
        # every knob against the agents.
        from looplab.core.config import default_agent_control
        self._agent_control: dict = (dict(agent_control) if agent_control is not None
                                     else default_agent_control())
        self._localize_faults = localize_faults
        self._feature_engineering = feature_engineering
        self._ablate_code_blocks = ablate_code_blocks
        self.proxy_scorer = proxy_scorer
        self.proxy_kill_fraction = proxy_kill_fraction
        self.reward_hack_detect = reward_hack_detect
        if trust_gate not in ("audit", "gate", "block"):
            # A security control must fail LOUDLY: silently coercing a typo ("Gate") to "audit"
            # would run with no enforcement while the caller believes the gate is on.
            raise ValueError(f"trust_gate must be 'audit', 'gate' or 'block', got {trust_gate!r}")
        self.trust_gate = trust_gate
        self._code_leakage_detect = code_leakage_detect
        self._critic_check = critic_check
        self._redact_output = redact_output
        # novelty_mode is the primary selector; a legacy novelty_gate=True forces the "algo" path.
        self._novelty_mode = str(novelty_mode or "llm") if not novelty_gate else "algo"
        self._novelty_gate = novelty_gate
        self._novelty_epsilon = novelty_epsilon
        # T5 semantic novelty (Phase 2): reject a proposal whose idea TEXT is a near-duplicate of
        # an existing node's — with one informed re-propose when the duplicate FAILED (the
        # ShinkaEvolve lever: novelty rejection before evaluation, ablation-ranked above model
        # routing). hash_embed is the zero-dep default; T4 wires a real embedder from config.
        self._novelty_semantic = bool(novelty_semantic)
        self._novelty_semantic_threshold = float(novelty_semantic_threshold)
        if embedder is None:
            from looplab.tools.vectorstore import hash_embed as _he
            embedder = _he
        self._embedder = embedder
        self._idea_vecs: dict[int, list] = {}   # hash(idea text) -> embedding (lazy in-memory cache)
        self._debug_depth = max(1, int(debug_depth))
        self._operator_bandit = bool(operator_bandit)
        # M5: the Researcher's always-on digest budget (0 = auto-scale with run size).
        try:
            setattr(researcher, "_digest_cap", int(digest_char_cap))
        except Exception:  # noqa: BLE001 — toy researchers without attrs are fine
            pass
        self._research_verify = bool(research_verify)
        self._workdir_audit = bool(workdir_audit)
        self._coverage_context = bool(coverage_context)
        self._concept_pivot = bool(concept_pivot)
        self._graded_novelty = bool(graded_novelty)
        self._capability_expansion = bool(capability_expansion)
        self._fingerprint_universal = bool(fingerprint_universal)
        self._cross_run_concepts = bool(cross_run_concepts)
        self._concept_run_base = bool(concept_run_base)
        self._cross_run_advisory = bool(cross_run_advisory)
        self._cross_run_structured_claims = bool(cross_run_structured_claims)
        self._cross_run_curation = bool(cross_run_curation)
        self._cross_run_curation_auto = bool(cross_run_curation_auto)
        self._cross_run_read_tools = bool(cross_run_read_tools)
        self._phase_handoff_summary = bool(phase_handoff_summary)
        # Novelty stance (Strategist-owned dial): how hard the proposer / foresight ranker / novelty
        # gate push for NEW directions. "balanced" == today's behavior; the Strategist raises it to
        # "explore" when coverage shows narrowing, or "exploit" to converge. Set by _apply_strategy.
        self._novelty_stance = "balanced"
        # Memora synergy: the SAME abstractor Memora uses for the case/KB index, applied to the
        # cross-run LESSONS tier so lesson retrieval gains anchor-expansion (harmonic recall)
        # instead of fingerprint-Jaccard alone. None (memora off) => the legacy Jaccard-only path.
        self._lesson_abstractor = lesson_abstractor
        self._exploit_suite = None   # 4.3 hardened ruleset; loaded once memory_dir is set (below)
        self._reflection_priors = reflection_priors
        # M6 comparative lessons: credit-assigned pair distillation (run-end and, when the
        # cadences are set, mid-run into/from the SHARED cross-run store — the live-share seam).
        self._comparative_lessons_on = comparative_lessons
        self.lessons_every = max(0, lessons_every)
        self.lessons_refresh_every = max(0, lessons_refresh_every)
        # Cross-run memory / lessons / reflection cluster (looplab/engine/lessons.py). The Engine
        # keeps thin delegators under the original `_`-names below (tests call/monkeypatch them);
        # the lessons-owned mutable state (seen stamp, prior note) lives on LessonMemory.
        self.lessons = LessonMemory(self)
        self._track_hypotheses = track_hypotheses
        self._surrogate_explore = surrogate_explore
        # Unified self-driving agent: in unified mode `researcher is developer` (one object plays
        # both roles); `agent_drives_actions` additionally lets it pick the next macro action.
        self.unified_agent = unified_agent
        self.agent_drives_actions = unified_agent and agent_drives_actions
        # The receipt-backed Card authority wins when both opt-in selectors are enabled. Letting the
        # free-form agent arm pre-empt it would silently bypass the atomic existing-work claim below.
        self.card_driven_selection = bool(card_driven_selection)
        # Keep a settled, bounded scalar for the Layer-5 producer/consumer seam. Zero is a hard
        # off-switch; no task group/request event is allowed to infer a non-zero depth from hardware.
        self.speculation_depth = max(0, min(64, int(speculation_depth or 0)))
        self.speculation_gate_receipt = (
            str(Path(speculation_gate_receipt).expanduser().resolve())
            if speculation_gate_receipt is not None else None
        )
        self._speculation_gate_calibration = bool(_speculation_gate_calibration)
        self._speculation_gate_admitted = False
        self._speculation_gate_receipt_digest = ""
        self._speculation_implementation_digest = ""
        self._speculation_policy_scope = ""
        self._speculation_calibration_profile_digest = ""
        self._speculation_calibration_gpu_inventory: list[dict] = []
        self._speculation_calibration_seed: Optional[int] = None
        self._speculation_runtime_scope_sha256 = ""
        _gate_receipt = None

        def _narrow_runtime_envelope_errors() -> tuple[list[str], str]:
            """Validate the one runtime that calibration evidence actually measured."""
            import sys
            from looplab.adapters.toytask import ToyTask
            from looplab.runtime.sandbox import SubprocessSandbox
            from looplab.search.policy import GreedyTree
            from looplab.tools.vectorstore import hash_embed

            errors: list[str] = []
            option_renames = {"policy": "policy_name"}
            for setting, expected in SPECULATION_CALIBRATION_PROFILE_SETTINGS.items():
                option_name = option_renames.get(setting, setting)
                if option_name not in _fields:
                    continue
                actual = _opt(option_name)
                try:
                    # EngineOptions retains schema-native tuples while snapshots contain JSON arrays.
                    matches_profile = orjson.dumps(
                        actual, option=orjson.OPT_SORT_KEYS) == orjson.dumps(
                            expected, option=orjson.OPT_SORT_KEYS)
                except (TypeError, ValueError, orjson.JSONEncodeError):
                    matches_profile = False
                if not matches_profile:
                    errors.append(f"{setting} must be {expected!r}, got {actual!r}")

            if card_driven_selection is not True:
                errors.append("card_driven_selection must be exactly true")
            if type(max_nodes) is not int or not 1 <= max_nodes <= 64:
                errors.append("max_nodes must be an integer in 1..64")
            if type(speculation_depth) is not int or not 0 <= speculation_depth <= 64:
                errors.append("speculation_depth must be an integer in 0..64")
            if not self.run_dir.name.strip():
                errors.append("run directory must have a non-empty run id")

            expected_scope = ""
            if type(max_nodes) is int and type(speculation_depth) is int:
                try:
                    expected_scope = speculation_runtime_scope_digest({
                        **SPECULATION_CALIBRATION_PROFILE_SETTINGS,
                        "max_nodes": max_nodes,
                        "speculation_depth": speculation_depth,
                        "speculation_gate_receipt": self.speculation_gate_receipt,
                    })
                except ValueError as exc:
                    errors.append(f"runtime scope could not be constructed: {exc}")
            if (
                not expected_scope
                or _speculation_runtime_scope_sha256 != expected_scope
            ):
                errors.append(
                    "runtime scope digest must match the source-owned full Settings profile "
                    "and live max_nodes")

            if type(task) is not ToyTask:
                errors.append("task must be the exact offline ToyTask")
            else:
                try:
                    canonical_speculation_toy_task(task, require_seed_set=True)
                except ValueError as exc:
                    errors.append(str(exc))
                errors.extend(_calibration_role_pair_errors(task, researcher, developer))
            if (
                type(policy) is not GreedyTree
                or policy.n_seeds != len(SPECULATION_CALIBRATION_SEEDS)
                or policy.max_nodes != max_nodes
                or policy.debug_depth != 1
                or policy.enable_merge is not True
                or policy.merge_every != 3
                or policy.max_merges != 2
                or policy.ablate_every != 0
                or policy.operator_bandit is not False
            ):
                errors.append("policy must be the canonical bounded GreedyTree")
            if (
                type(sandbox) is not SubprocessSandbox
                or sandbox.python != sys.executable
                or sandbox.max_output_bytes != 64_000
                or sandbox.mem_bytes is not None
                or sandbox.fsize_bytes is not None
            ):
                errors.append("sandbox must be the exact default trusted-local SubprocessSandbox")
            for name, value in (
                ("strategist", strategist), ("deep_researcher", deep_researcher),
                ("report_writer", report_writer), ("developer_factory", developer_factory),
                ("onboarder", onboarder), ("proxy_scorer", proxy_scorer),
                ("lesson_abstractor", lesson_abstractor), ("dep_installer", dep_installer),
            ):
                if value is not None:
                    errors.append(f"{name} must be disabled")
            if self._embedder is not hash_embed:
                errors.append("embedder must be the offline hash embedder")
            if not callable(self.role_factory):
                errors.append("role_factory must provide isolated calibrated Toy roles")
            if crash_after is not None:
                errors.append("crash_after is forbidden in the calibrated runtime")
            return errors, expected_scope

        def _guard_calibrated_role_factory() -> None:
            original_role_factory = self.role_factory

            def _calibrated_role_factory():
                pair = original_role_factory()
                if not isinstance(pair, tuple) or len(pair) != 2:
                    raise RuntimeError("calibrated role_factory must return one role pair")
                pair_errors = _calibration_role_pair_errors(task, pair[0], pair[1])
                if pair_errors:
                    raise RuntimeError(
                        "calibrated role_factory escaped the purpose envelope: "
                        + "; ".join(pair_errors))
                return pair

            self.role_factory = _calibrated_role_factory

        if self._speculation_gate_calibration:
            # Validate the bootstrap at the library boundary.  A caller cannot obtain the waiver by
            # constructing Engine directly with arbitrary roles/settings, and no run artifact exists
            # yet when these checks execute.
            calibration_errors, expected_runtime_scope = _narrow_runtime_envelope_errors()
            if self.speculation_gate_receipt is not None:
                calibration_errors.append("speculation_gate_receipt must be unset")

            # The CLI has already created engine.lock.  An empty events file is also harmless; every
            # material snapshot/artifact is forbidden because copied evidence must not bootstrap a run.
            if self.run_dir.exists():
                unexpected = sorted(
                    path.name for path in self.run_dir.iterdir()
                    if path.name not in {"engine.lock", "events.jsonl"}
                )
                event_path = self.run_dir / "events.jsonl"
                if unexpected:
                    calibration_errors.append(
                        "run directory contains stale material: " + ", ".join(unexpected))
                if event_path.exists() and event_path.stat().st_size:
                    calibration_errors.append("events.jsonl must be exactly empty")

            try:
                from looplab.core.hardware import effective_gpu_inventory
                gpu_inventory = _stable_effective_gpu_inventory(effective_gpu_inventory())
            except Exception:
                gpu_inventory = []
            if not gpu_inventory:
                calibration_errors.append(
                    "effective CUDA_VISIBLE_DEVICES GPU inventory must be non-empty")
            if calibration_errors:
                raise ValueError(
                    "speculation gate calibration profile mismatch: "
                    + "; ".join(calibration_errors)
                )

            _guard_calibrated_role_factory()
            self._speculation_gate_admitted = True  # depth=0 baseline and depth>0 treatment
            # SpeculationMixin's live enablement also requires a non-empty internal admission token.
            # This value is never serialized as a receipt digest for calibration evidence; the durable
            # authority is the explicit profile/GPU/seed envelope below.
            self._speculation_gate_receipt_digest = SPECULATION_CALIBRATION_PROFILE_DIGEST
            self._speculation_policy_scope = SPECULATION_POLICY_SCOPE
            self._speculation_calibration_profile_digest = (
                SPECULATION_CALIBRATION_PROFILE_DIGEST
            )
            self._speculation_calibration_gpu_inventory = gpu_inventory
            self._speculation_calibration_seed = task.seed
            self._speculation_runtime_scope_sha256 = expected_runtime_scope
            from looplab.search.speculation_quality import speculation_implementation_digest
            self._speculation_implementation_digest = speculation_implementation_digest()
        elif self.card_driven_selection and self.speculation_depth > 0:
            from looplab.adapters.toytask import ToyTask
            if not self.run_dir.name.strip():
                raise ValueError("positive Card speculation requires a non-empty run id")
            if not self.speculation_gate_receipt:
                raise ValueError(
                    "positive speculation_depth requires speculation_gate_receipt from "
                    "`looplab speculation-gate`"
                )
            from looplab.search.speculation_quality import (
                speculation_task_profile_digest,
                validated_speculation_gate_receipt,
            )
            _gate_receipt = validated_speculation_gate_receipt(
                self.speculation_gate_receipt,
            )
            runtime_errors, expected_runtime_scope = _narrow_runtime_envelope_errors()
            if (
                runtime_errors
                or _gate_receipt is None
                or _gate_receipt.get("require_gpu") is not True
                or not _gate_receipt.get("gpu_inventory")
                or _gate_receipt.get("policy_scope") != SPECULATION_POLICY_SCOPE
                or type(_gate_receipt.get("admitted_depth")) is not int
                or _gate_receipt.get("admitted_depth") != self.speculation_depth
                or type(_gate_receipt.get("admitted_max_nodes")) is not int
                or _gate_receipt.get("admitted_max_nodes") != max_nodes
                or _gate_receipt.get("runtime_scope_sha256") != expected_runtime_scope
                or _gate_receipt.get("calibration_profile_digest")
                != SPECULATION_CALIBRATION_PROFILE_DIGEST
                or _gate_receipt.get("workload_scope") != "quadratic_toy"
                # The receipt is intentionally scoped to the shipped quadratic adapter, not merely
                # to an arbitrary TaskAdapter/subclass that can spoof the same model_dump while
                # executing a different workload.
                # CODEX AGENT: the public positive-depth path currently admits only the calibration
                # toy itself; every real Dataset/Repo/Command TaskAdapter is rejected here. Thus the
                # generic Settings/UI knob is not a product rollout at all, only a replay of its own
                # benchmark. Keep it explicitly maintainer-only or define workload-scoped evidence
                # before users can reasonably interpret speculation_depth as usable functionality.
                or type(task) is not ToyTask
                or _gate_receipt.get("task_profile_sha256")
                != speculation_task_profile_digest(task)
                or not isinstance(_gate_receipt.get("implementation_digest"), str)
                or not _gate_receipt.get("implementation_digest")
                or self._policy_name != SPECULATION_POLICY_SCOPE
            ):
                raise ValueError(
                    "speculation_gate_receipt is stale, invalid, non-GPU, policy/depth-mismatched, "
                    "runtime-scope/max-nodes-mismatched, or does not pass the current "
                    "scorer/search-quality gates"
                )
            _guard_calibrated_role_factory()
            self._speculation_gate_receipt_digest = _gate_receipt["self_digest"]
            self._speculation_implementation_digest = _gate_receipt["implementation_digest"]
            self._speculation_policy_scope = SPECULATION_POLICY_SCOPE
            self._speculation_runtime_scope_sha256 = expected_runtime_scope
            self._speculation_gate_admitted = True
        self._strategy_fidelity: Optional[str] = None   # None => use the Idea's own profile
        # GPU pool + max_parallel=0 AUTO. Multi-GPU boxes were used at 1/N: a single-command eval pins
        # itself to one GPU (or DataParallel-deadlocks on cleanup), leaving the others idle. To actually
        # parallelize, each concurrent eval is pinned to a DISTINCT GPU via CUDA_VISIBLE_DEVICES (see
        # evaluate.py::_evaluate); `max_parallel=0` means AUTO — run one experiment per detected GPU.
        self._gpu_ids: list[int] = _detect_gpu_ids()
        self._gpu_physical_ids, self._gpu_mem = detect_gpu_inventory(self._gpu_ids)
        if _eval_parallel_value == 0:                    # AUTO: the agent/operator lets the box decide
            _eval_parallel_value = max(1, len(self._gpu_ids))
        self._eval_parallel = max(1, int(_eval_parallel_value))
        # Now that eval_parallel is settled, resolve llm_parallel (0 = AUTO = eval_parallel), so a build
        # fan-out never exceeds what we can concurrently evaluate.
        self._llm_parallel = self._resolve_llm_parallel(self._llm_parallel_startup_opt)
        # Layer-2 compatibility lives solely in the two descriptors above. New runtime logic reads the
        # canonical attributes; legacy Engine(...) callers and direct assignments transparently feed them.
        # The canonical field is also the opt-in switch for the SHARED provider-call budget. An
        # unset field (including legacy-only parallel_build) and startup AUTO preserve historical
        # unbounded research overlap; only a positive canonical value activates a finite total.
        try:
            _startup_llm_total = (min(64, int(_llm_parallel_opt))
                                  if _llm_parallel_opt is not None
                                  and int(_llm_parallel_opt) > 0 else None)
        except (TypeError, ValueError, OverflowError):
            _startup_llm_total = None
        self._llm_broker = LLMConcurrencyBroker(
            total=_startup_llm_total,
            lane_limits=default_llm_lane_limits(_startup_llm_total),
        )
        self._llm_lane_limits_explicit = False
        self._free_gpus: list[int] = list(self._gpu_ids)   # free-list handed out per concurrent eval
        # Every local Engine process otherwise sees the same physical devices as independently free.
        # Hold one crash-released OS lease while this Engine has any GPU reservation. It intentionally
        # serializes separate Runs at pool granularity because ordinal/UUID/MIG aliases are not safely
        # comparable across independently configured CUDA_VISIBLE_DEVICES environments.
        self._gpu_host_lease_path = (
            default_gpu_host_lease_path() if self._gpu_ids else None)
        self._gpu_host_lease_handle = None
        self._gpu_lock = threading.Lock()
        self._gpu_condition = threading.Condition(self._gpu_lock)
        self._gpu_epoch = 0
        self._eval_gpu_reservations: dict[tuple[int, int], dict] = {}
        self.timeout = timeout
        self.max_eval_timeout = _opt("max_eval_timeout")
        self._train_monitor = bool(train_monitor)
        self._train_monitor_interval_s = train_monitor_interval_s
        self._train_monitor_kill = bool(train_monitor_kill)
        self._train_monitor_kill_confidence = train_monitor_kill_confidence
        # ASHA live-curve rank watchdog (advisory in the product surface; opt-in kill). off == today.
        self._asha_live = bool(asha_live)
        self._asha_live_kill = bool(asha_live_kill)
        self._asha_live_quantile = float(asha_live_quantile)
        self._asha_live_min_siblings = max(1, int(asha_live_min_siblings))
        self.sweep_timeout_mult = max(1.0, sweep_timeout_mult)
        self.crash_after = crash_after
        self.confirm_top_k = confirm_top_k
        self.confirm_seeds = confirm_seeds
        self.max_seconds = max_seconds
        self.max_eval_seconds = max_eval_seconds
        self.memory_dir = memory_dir
        # 4.3: load the hardened exploit ruleset grown by `looplab harden` (hacker-fixer-solver)
        # from <memory_dir>/exploits.jsonl — merged into the reward-hack scan so every
        # previously-discovered exploit stays guarded on later runs. None => built-in detector only.
        if self.memory_dir and self.reward_hack_detect:
            _ep = Path(self.memory_dir) / "exploits.jsonl"
            if _ep.exists():
                try:
                    from looplab.trust.harden import ExploitSuite
                    self._exploit_suite = ExploitSuite.load(_ep)
                except Exception:  # noqa: BLE001
                    self._exploit_suite = None
        self.require_approval = require_approval
        self.archive_resolution = archive_resolution
        # RepoTask onboarding (Phase 3): `onboarder()` -> a proposed {eval_spec,
        # adapter_files, goal}; ratified per `eval_trust_mode` then frozen+trusted.
        self.onboarder = onboarder
        self.eval_trust_mode = eval_trust_mode
        # Sandbox tier for the command-eval path (ADR-13, Phase 4): "untrusted" wraps each
        # eval in `docker run --network none` (real isolation for an arbitrary framework);
        # "trusted_local" runs it directly. The solution.py path uses self.sandbox instead.
        self.trust_mode = trust_mode
        self.docker_image = docker_image
        # Resource caps for the untrusted/hostile command-eval Docker tier (make_docker_wrap).
        # Mirror the solution.py DockerSandbox tier so both untrusted tiers bound memory/cpu.
        self.sandbox_memory = sandbox_memory
        self.sandbox_cpus = sandbox_cpus
        self._seed_mode = seed_mode or "auto"   # run-wide fallback for per-editable seeding
        self._run_setup_done = False             # run-level (once) dependency setup guard
        self._run_setup_lock = _threading.Lock()   # _run_eval runs on parallel worker threads; the
        #   check-then-set on _run_setup_done races without this, launching run_setup (pip) N times
        self._drift_warned = False   # one-shot guard for the #8 drift-coverage warning
        # Fail loud at START, not mid-sweep: the untrusted tier needs docker, so verify it once
        # here instead of re-discovering (and re-scanning PATH) on every eval's make_docker_wrap.
        if trust_mode in ("untrusted", "hostile"):
            import shutil as _sh
            if not _sh.which("docker"):
                raise RuntimeError(
                    f"trust_mode={trust_mode!r} needs the docker CLI to sandbox evals, but it was "
                    "not found on PATH. Install Docker or use trust_mode='trusted_local'.")
        self._spec_activated = False
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.store = EventStore(self.run_dir / "events.jsonl")
        # Bind after EventStore exists and before any role can make an LLM call. Paid usage now
        # survives process restarts in the same append-only source of truth as the run itself.
        bind_cost_accountants(self)
        self._write_lock = anyio.Lock()
        # Node-id reservation lock (Variant-1 parallel build): serialises the CHEAP build prefix (fold ->
        # id=max(nodes)+1 -> parent-check -> node_building append) so PARALLEL `_create_node` threads get
        # DISTINCT monotonic ids. A threading.Lock (not the anyio _write_lock) because parallel builds run
        # in worker THREADS (anyio.to_thread). Uncontended on the serial path -> byte-identical.
        self._id_lock = threading.Lock()
        # Variant-1 parallel build: serialises the cross-run advisory-text computation + its receipt
        # capture in `_set_complexity_hint` so two concurrent draft builds can't clobber the shared
        # `self._cross_run_advisory_receipt` between one build's write and its per-build capture.
        # Uncontended on the serial path and no-op unless `cross_run_advisory` is on.
        self._advisory_lock = threading.Lock()
        # Tracing (I14): nested, correlated spans -> spans.jsonl (files-as-truth), bridged to
        # OpenTelemetry when the SDK is configured. Diagnostics only; never drives state.
        self.tracer = Tracer(JsonlSpanExporter(self.run_dir / "spans.jsonl"),
                             run_id=self.run_dir.name)
        # Task assets (e.g. the dataset) materialized into each node's sandbox workdir.
        assets = getattr(task, "assets", None)
        self._assets: dict = assets() if callable(assets) else {}
        self.task_has_columns = callable(getattr(task, "columns", None))   # I1: tabular task?
        # Out-of-process / host-side grading (B1+, general): a task may expose `host_grader()` ->
        # {"predictions": <file>, "scorer": <name>, "labels": <held-out answer key>, "key"?: ...}. When
        # present, the candidate (a separate sandbox process) writes ONLY predictions; the host (this
        # engine process) scores them — the labels live in engine memory and never touch the candidate
        # FS or the event log. Works for ANY solution.py-path task, not just MLEBench.
        hg = getattr(task, "host_grader", None)
        self._host_grader: Optional[dict] = hg() if callable(hg) else None
        # Host-grading/holdout cluster (looplab/engine/holdout.py) and workspace-seeding cluster
        # (looplab/engine/workspace.py). Like `self.lessons` above, the Engine keeps thin
        # delegators under the original `_`-names (tests + internal callers use them); both
        # wrappers read engine state live through their engine handle, so construction order
        # only matters relative to the first CALL (`_build_holdout_idx` just below needs
        # `self.holdout`; the first workspace call is in run()).
        self.holdout = HoldoutGrader(self)
        self.workspace = WorkspaceSeeder(self)
        # D1 holdout partition: a deterministic subset of the host-held labels reserved as the
        # final unseen signal. Every search/confirm eval is scored on the COMPLEMENT only; the
        # holdout rows are touched exactly once, at finish, to re-score the val-top-k. The
        # partition is a pure function of (n_labels, fraction) — identical across resume/replay,
        # no state to persist. Real MLE-bench (kind="mlebench") is graded by the official
        # out-of-process grader, which the engine cannot partition — skipped.
        self.confirm_seed_base = max(0, int(confirm_seed_base))
        self._holdout_select = bool(holdout_select)
        self._holdout_top_k = max(1, int(holdout_top_k))
        self._select_verifier = bool(select_verifier)
        self._verifier_ci_tie = bool(verifier_ci_tie)
        self._select_verifier_samples = max(1, int(select_verifier_samples))
        # The FRACTION defines the split every search metric is scored against, so it must be pinned
        # in the event log (like trust_gate / holdout_select) — on resume the recorded value is
        # re-used (see run()), so a changed live setting can't silently make pre/post-resume metrics
        # incomparable. `_build_holdout_idx` rebuilds the partition from a fraction.
        self._holdout_fraction = float(holdout_fraction)
        self._holdout_idx: frozenset = self._build_holdout_idx(self._holdout_fraction)
        self._holdout_epoch = 0
        # RepoTask (ADR-7): an existing repo the agent edits + a command-based eval.
        rs = getattr(task, "repo_spec", None)
        self._repo_spec: dict = rs() if callable(rs) else {}
        es = getattr(task, "eval_spec", None)
        self._eval_spec: dict = es() if callable(es) else {}
        # Ablation probes run via the solution.py sandbox path, which is wrong for a repo/eval-spec
        # run (the repo tree is absent) — so `_ablate` no-ops there. Tell the policy not to PROPOSE
        # ablate on such runs: the skip creates no refine_block node, so the ablate cadence would
        # never clear and the loop would spin forever (re-stamped on every policy rebuild, see
        # strategy.py::_apply_strategy). The flag is read via getattr so any policy object is safe.
        self._ablation_capable: bool = not (bool(self._repo_spec) or bool(self._eval_spec))
        self.policy.ablation_capable = self._ablation_capable
        # Fail loudly: a repo task with no trusted eval AND no onboarder would silently
        # evaluate every node via the empty solution.py path. Require one or the other.
        if self._repo_spec and not self._eval_spec and onboarder is None:
            raise ValueError(
                "RepoTask has no eval and no onboarder: set `onboard: true` with "
                "backend=llm (so an onboarder is built), or provide `eval` in the task.")

    # --------------------- workspace materialization (extracted to engine/workspace.py)
    # The workspace seeding / materialization cluster lives in looplab/engine/workspace.py
    # (`WorkspaceSeeder`, constructed as `self.workspace` in __init__). These thin delegators
    # keep the ORIGINAL method names on the Engine — tests call e.g. `engine._write_node_files`
    # / `engine._seed_workspace` directly — and WorkspaceSeeder routes its internal cross-calls
    # back through them, so an instance-level monkeypatch intercepts every path.
    def _write_assets(self, workdir) -> None:
        return self.workspace.write_assets(workdir)

    def _write_node_files(self, node, workdir) -> None:
        return self.workspace.write_node_files(node, workdir)

    def _materialize(self, node, workdir) -> None:
        return self.workspace.materialize(node, workdir)

    # ------------------------------------------------------------ loop control
    def _ack_commands(self, events) -> None:
        """Causally acknowledge every marked server command this engine has folded.

        The ack is replay-neutral diagnostics.  It names both command id and exact intent sequence,
        so an unrelated engine/background event can never be mistaken for command observation. The
        caller passes the exact snapshot used for ``fold``: a second read here could include a command
        appended after the fold and falsely acknowledge an intent this iteration never observed.

        A long-running engine calls this at every decision boundary.  Keep a local cursor over the
        exact ``EventStore`` snapshot: the first call bootstraps the historical acknowledgement set,
        while later calls inspect only the appended suffix.  ``EventStore.read_all`` retains Event
        object identity across ordinary appends and rebuilds the cache on replacement/rewrite, so a
        changed first object (or a shorter snapshot) safely invalidates the cursor.  The attributes
        are initialized lazily because a few focused tests construct ``Engine`` with
        ``object.__new__``.
        """
        total = len(events)
        initialized = bool(getattr(self, "_command_ack_initialized", False))
        cursor = int(getattr(self, "_command_ack_cursor", 0)) if initialized else 0
        first = events[0] if total else None
        cached_first = getattr(self, "_command_ack_first_event", None)
        invalidated = initialized and (
            cursor > total or (cursor > 0 and (first is None or first is not cached_first)))
        if invalidated:
            cursor = 0
            acked: set[tuple[str, object]] = set()
        else:
            # Copy, not alias: the dedup passes below mutate ``acked`` in place, but the durable
            # seen-set must not advance until every ack row is appended — otherwise a failed append
            # marks an unwritten ack as seen and it is lost for the process lifetime.
            acked = set(getattr(self, "_command_ack_seen", set()))

        # Two passes over the *new suffix* matter: an already-durable ack later in that same suffix
        # must suppress its intent even when the intent row appears first.
        for index in range(cursor, total):
            event = events[index]
            if event.type == EV_COMMAND_ACK:
                acked.add((str((event.data or {}).get("command_id")),
                           (event.data or {}).get("event_seq")))

        pending: list[tuple[str, int]] = []
        for index in range(cursor, total):
            event = events[index]
            command_id = (event.data or {}).get("_command_id")
            identity = (str(command_id), event.seq)
            if command_id and identity not in acked:
                acked.add(identity)
                pending.append(identity)

        # Append the diagnostics FIRST, then commit the process-local cursor/seen against the exact
        # folded snapshot. A crash before the commit is harmless (a restart re-bootstraps from cursor
        # 0); a NON-fatal append failure is now also harmless — because the cursor and seen-set stay
        # unadvanced, the next call re-scans this suffix and re-attempts the un-acked intents (the
        # already-appended acks are re-observed and deduped in the first pass). A subsequent call sees
        # the new ack rows in its suffix.
        for command_id, event_seq in pending:
            self.store.append(EV_COMMAND_ACK, {
                "command_id": command_id, "event_seq": event_seq,
            })
        self._command_ack_initialized = True
        self._command_ack_cursor = total
        self._command_ack_first_event = first
        self._command_ack_seen = acked

    def _begin_finalize(
            self, data: dict, *, scope: str | None = None,
            finish_report_planned: bool = False, after_seq: int | None = None) -> str:
        """Durably stage one exact terminal payload and return its stable wrap-up scope.

        ``after_seq`` is the natural-finish decision CAS. The EventStore check prevents even an
        invalid marker from landing when a control won before the claim; replay also validates the
        physical adjacency for defense in depth.
        """
        scope = scope or f"finalize:{secrets.token_hex(16)}"
        already_begun = any(
            event.type == EV_FINALIZE_STEP and (event.data or {}).get("scope") == scope
            and (event.data or {}).get("step") == "begun" for event in self.store.read_all())
        if not already_begun:
            payload = {
                "scope": scope,
                "step": "begun",
                "finish_data": dict(data),
                "finish_report_planned": bool(finish_report_planned),
            }
            kwargs = {}
            if after_seq is not None:
                payload["after_seq"] = after_seq
                kwargs["expected_last_seq"] = after_seq
            self.store.append(EV_FINALIZE_STEP, payload, **kwargs)
        return scope

    def _finish_run(self, data: dict, *, scope: str | None = None) -> None:
        """Open one durable finalization scope, then publish its terminal run event.

        The begun marker precedes ``run_finished``. A hard kill after the terminal event is therefore
        distinguishable from a fully projected run, and re-entry can finish the same scope without
        reopening search or repeating already-gated paid wrap-up work.
        """
        scope = self._begin_finalize(data, scope=scope)
        self.store.append(EV_RUN_FINISHED, {**data, "finalize_scope": scope})

    def _finish_if_quiescent(self, data: dict, *, after_seq: int) -> bool:
        """CAS-claim a scoped terminal intent and publish it only while the log stays quiescent.

        The begin marker is the first adjacency claim. ``run_finished`` then names that marker as its
        immediate predecessor and opts into the exact-finish crash handshake.
        """
        scope = f"finalize:{secrets.token_hex(16)}"
        try:
            self._begin_finalize(data, scope=scope, after_seq=after_seq)
        except EventStoreConcurrencyError:
            return False
        events = self.store.read_all()
        begun = next(
            event for event in reversed(events)
            if event.type == EV_FINALIZE_STEP
            and (event.data or {}).get("scope") == scope
            and (event.data or {}).get("step") == "begun"
        )
        try:
            finished = self.store.append(
                EV_RUN_FINISHED,
                {
                    **data,
                    "after_seq": begun.seq,
                    "finalization_required": True,
                    "finalize_scope": scope,
                },
                expected_last_seq=begun.seq,
            )
        except EventStoreConcurrencyError:
            return False
        return finished.seq == begun.seq + 1

    def _finish_with_report_if_quiescent(
            self, state: RunState, data: dict, *, after_seq: int) -> bool:
        """Write one scoped paid report and finish as an adjacency-checked CAS chain.

        The provider attempt is guarded by ``report_begun``. A crash retry can reuse the durable
        report or record an ambiguous attempt, but can never buy it again. The successful report event
        remains immediately before ``run_finished`` as required by replay.
        """
        report_planned = self.report_writer is not None and self.report_every > 0
        if not report_planned:
            return self._finish_if_quiescent(data, after_seq=after_seq)

        scope = f"finalize:{secrets.token_hex(16)}"
        try:
            self._begin_finalize(
                data,
                scope=scope,
                finish_report_planned=True,
                after_seq=after_seq,
            )
        except EventStoreConcurrencyError:
            return False
        if not ensure_finish_report(self, self.store.read_all(), scope, state=state):
            return False

        events = self.store.read_all()
        if not finalize_scope_quiescent(events, scope):
            self.store.append(EV_FINALIZE_STEP, {
                "scope": scope,
                "step": "abandoned",
                "outcome": "decision_snapshot_changed_during_report",
            })
            return False

        report = scoped_finish_report(events, scope)
        tail_seq = events[-1].seq if events else -1
        if report is not None and report.seq != tail_seq:
            # Only diagnostics may have followed; clone the durable content without another provider
            # call so report->finish is adjacent again. A background-appendable event (an `llm_usage`
            # from a cost sink) can splice in between this tail read and the CAS, exactly like the
            # finish CAS below — abandon the scope on a lost race instead of crashing the finish path.
            try:
                report = self.store.append(
                    EV_REPORT_GENERATED,   # the registry constant, not a literal (invariant #7: a typo'd literal silently no-ops)
                    dict(report.data or {}),
                    expected_last_seq=tail_seq,
                )
            except EventStoreConcurrencyError:
                self.store.append(EV_FINALIZE_STEP, {
                    "scope": scope,
                    "step": "abandoned",
                    "outcome": "event_won_report_clone_cas",
                })
                return False
            tail_seq = report.seq
        try:
            finished = self.store.append(
                EV_RUN_FINISHED,
                {
                    **data,
                    "after_seq": tail_seq,
                    "finalization_required": True,
                    "finalize_scope": scope,
                },
                expected_last_seq=tail_seq,
            )
        except EventStoreConcurrencyError:
            self.store.append(EV_FINALIZE_STEP, {
                "scope": scope,
                "step": "abandoned",
                "outcome": "event_won_report_to_finish_cas",
            })
            return False
        mark_finish_report_complete(self, scope)
        return finished.seq == tail_seq + 1

    async def run(self) -> RunState:
        """Run under one shared broker context inherited by anyio tasks and worker threads."""
        broker = getattr(self, "_llm_broker", None)
        if broker is None:  # defensive for test/library engines constructed through __new__
            broker = self._llm_broker = LLMConcurrencyBroker()
        with llm_broker_scope(broker), llm_lane_scope("engine"):
            return await self._run_with_llm_broker()

    async def _run_with_llm_broker(self) -> RunState:
        events = self.store.read_all()
        state = fold(events)
        # Re-entry authorization is the first semantic boundary.  Recovery, command ACK and setup all
        # append events, so a stale/missing/different receipt must fail before any of them can mutate a
        # positive-depth run.  `_reentry_repin` repeats this after setup to guard a concurrent tail edit.
        self._require_pinned_speculation_receipt(state)
        if self._speculation_gate_calibration and events:
            # The hidden bootstrap is launch-only.  Even an exact prior calibration envelope cannot be
            # resumed/reused as another sample; every evidence lane starts from an exactly empty log.
            raise SpeculationAuthorizationError(
                "speculation gate calibration requires exactly zero prior events at run start")
        if self._recover_interrupted_builds(state):
            # Recovery appends terminal evidence. Re-fold before setup or any policy work so this
            # invocation cannot resurrect the abandoned marker or reuse its reserved id.
            events = self.store.read_all()
            state = fold(events)
        self._ack_commands(events)
        # A hard kill can land after the durable terminal intent (`finalize_step:begun`) but before
        # `run_finished`. Never run setup/search in that gap; finalization restores the exact terminal
        # payload from the begun marker and resumes only the same wrap-up scope.
        if (incomplete_finalize_scope(events) is None
                and not state.finalization_pending()):
            self._setup_phase(state)

        entry_finished = self._reentry_repin()
        start = time.time()
        # Creation-level runaway guard: if the loop keeps CREATING nodes while NO node reaches a
        # terminal (evaluated/failed), it is spinning — e.g. `fold` returning empty `nodes` makes
        # `_create_node` re-mint id 0 forever (the 184MB node_created(0) spin). The eval loop has its
        # own anti-stuck guard, but node CREATION had none. Local counters (not replayed) → on trip we
        # append run_finished (which IS replayed), so resume sees a cleanly-finished run.
        _created_no_terminal = 0
        _prev_terminal = -1
        while True:
            decision_events = self.store.read_all()
            state = fold(decision_events)
            decision_seq = decision_events[-1].seq if decision_events else -1
            # A control can arrive after initial re-entry. Re-check the calibrated authority on every
            # stable decision prefix before ACKs, recovery or any budget/strategy application.
            self._require_pinned_speculation_receipt(state)
            # A command ACK is a durable observation boundary. If it (or any concurrent writer)
            # extends the log after this fold, refold before doing domain work so neither a stale
            # reset nor a stale natural-finish decision can cross the newly-observed intent.
            self._ack_commands(decision_events)
            observed_tail = self.store.read_all()
            if (observed_tail[-1].seq if observed_tail else -1) != decision_seq:
                continue
            # A background consolidation can land immediately before any terminal/operator/budget gate.
            # Mirror it while this decision prefix is stable so an early exit cannot leave the durable
            # Card board permanently behind the Hypothesis board.
            state = self._mirror_hypothesis_card_merges(state)
            reconciled_tail = self.store.read_all()
            if (reconciled_tail[-1].seq if reconciled_tail else -1) != decision_seq:
                continue
            if state.search_epoch != self._holdout_epoch:
                # A reset/new candidate can win the finish race AFTER holdout disclosure while this
                # same Engine process stays alive. Rebuild immediately; waiting for a CLI re-entry
                # would stamp epoch-N events while still scoring the epoch-(N-1) partition.
                self._holdout_epoch = state.search_epoch
                self._holdout_idx = self._build_holdout_idx(
                    self._holdout_fraction, self._holdout_epoch)
            # A scoped terminal intent is itself a work gate. Finalize/recover that exact scope
            # below; never reopen setup/search while a paid-report or terminal append is in flight.
            pending_scope = incomplete_finalize_scope(decision_events)
            self._pending_finalize_scope = pending_scope
            if pending_scope is not None:
                break
            # `/resume` records a durable request even when this process is already alive. A live
            # loop acknowledges it only when it can actually re-enter work; terminal/HITL/pause gates
            # leave it pending so the post-exit waiter (or on-load reconciler) spawns a fresh CLI,
            # whose normal resume path lifts the appropriate gate.
            if state.resume_pending() and not state.finished and not state.paused:
                self.store.append(EV_RESUME_SERVED, {})
                continue
            # Terminal/operator gates precede ALL work, including reset rebuilds. An explicit pause
            # must freeze a queued rerun; a scoped developer-crash pause must stop a stale reset batch.
            # A prior invocation guard may have appended run_finished(error) after a durable abort.
            # That is a retryable failed wrap-up, not the abort's terminal result; republish the
            # stable abort scope and let scoped finalization deduplicate every completed side effect.
            if (state.finished and state.stop_requested
                    and str(state.stop_reason or "").lower() == "error"):
                abort = next(
                    (event for event in reversed(decision_events)
                     if event.type == EV_RUN_ABORT),
                    None,
                )
                abort_scope = f"abort:{abort.seq}" if abort is not None else None
                self._finish_run({"reason": "aborted"}, scope=abort_scope)
                break
            if state.finished:
                break
            if isinstance(state.leakage, dict) and state.leakage.get("leak"):
                if self._close_card_build_before_terminal_gate(state):
                    continue
                if self._finish_with_report_if_quiescent(
                        state, {"reason": "leakage"}, after_seq=decision_seq):
                    break
                continue
            if state.stop_requested:
                if self._close_card_build_before_terminal_gate(state):
                    continue
                if self._finish_with_report_if_quiescent(
                        state, {"reason": "aborted"}, after_seq=decision_seq):
                    break
                continue
            if state.paused:
                if self._close_card_build_before_terminal_gate(state):
                    continue
                break
            # node_reset (operator "re-run this node from a stage"): a reset from implement/propose
            # re-develops the SAME node id IN PLACE before any other loop work, so it never mints a new
            # node. (An eval-reset needs no help here — the fold left it pending-with-code and the normal
            # eval dispatch below re-scores it.)
            _resets = [n for n in state.nodes.values()
                       if n.rerun_from in ("implement", "propose")
                       and n.status is NodeStatus.pending and not n.tombstoned
                       and n.id not in state.aborted_nodes]
            if _resets:
                # One rebuild per fold. A developer crash can auto-pause the first node, and a reset/
                # abort can change the rest while it is building; never process a stale whole batch.
                self._rerun_node(_resets[0], state)
                continue
            _terminal_now = sum(1 for _n in state.nodes.values()
                                if _n.status is not NodeStatus.pending)
            if _terminal_now != _prev_terminal:      # a node reached terminal (progress) -> reset
                _created_no_terminal = 0
                _prev_terminal = _terminal_now
            # Onboarding pre-phase (Phase 3, ADR-7): the agent proposes a trusted eval
            # spec + metric adapter; a human ratifies it once (or autonomous auto-confirms);
            # then it's frozen + protected and the optimization loop trusts it.
            if self.onboarder is not None and not state.spec_confirmed:
                if state.proposed_spec is None:
                    with self.tracer.span("onboard", new_trace=True), \
                            llm_lane_scope("enrichment"):
                        proposal = self.onboarder()
                    self.store.append(EV_SPEC_PROPOSED, proposal)
                    continue
                if self.eval_trust_mode == "autonomous":
                    self.store.append(EV_SPEC_APPROVED, {})   # no human gate
                    continue
                if not state.spec_approval_requested:
                    self.store.append(EV_SPEC_APPROVAL_REQUESTED,
                                      {"eval": state.proposed_spec.get("eval_spec")})
                break  # pause for `LoopLab approve` (ratify_freeze)
            if self.onboarder is not None and not self._spec_activated:
                self._activate_spec(state.proposed_spec)
            # Drift coverage (#8): ratify_freeze_drift only corroborates the metric if a
            # cross_check reader exists. An adapter metric (agent-authored reader) with no
            # cross_check would make the drift guard a SILENT no-op exactly where it matters
            # most — surface it loudly once instead of pretending the metric is corroborated.
            if (self.eval_trust_mode == "ratify_freeze_drift" and self._eval_spec
                    and not self._drift_warned):
                self._drift_warned = True
                _m = self._eval_spec.get("metric", {})
                if _m.get("kind") == "adapter" and not self._eval_spec.get("cross_check"):
                    self.store.append(EV_DRIFT_UNAVAILABLE, {
                        "reason": "ratify_freeze_drift selected but the adapter metric has no "
                                  "cross_check; the agent-authored reader is trusted WITHOUT "
                                  "independent corroboration. Add eval.cross_check (a built-in "
                                  "reader) to enable the drift guard."})
            max_s, max_es = self._apply_control_overrides(state)
            # Budget (I13): per-invocation wall-clock ceiling (resets on each resume).
            if max_s is not None and (time.time() - start) >= max_s:
                if self._close_card_build_before_terminal_gate(state, max_es):
                    continue
                if self._close_node_creating_forced_request_before_terminal_gate(
                    state, reason="time_budget",
                ):
                    continue
                if self._finish_with_report_if_quiescent(
                        state, {"reason": "time_budget"}, after_seq=decision_seq):
                    break
                continue
            # Eval-compute budget (#2): cumulative time spent inside evals across the whole run
            # (persisted via the event log, so it survives resume — unlike wall-clock). Stops
            # the silent multi-hour sweep that real training runs can produce.
            if (max_es is not None
                    and state.total_eval_seconds >= max_es):
                if self._close_card_build_before_terminal_gate(state, max_es):
                    continue
                if self._close_node_creating_forced_request_before_terminal_gate(
                    state, reason="eval_budget",
                ):
                    continue
                if self._finish_with_report_if_quiescent(
                        state, {"reason": "eval_budget"}, after_seq=decision_seq):
                    break
                continue

            if await self._serve_forced_requests(state):
                continue

            if self._speculation_enabled():
                # Crash-prefix cleanup and the durable Card-build queue both precede cadences and
                # empty-action finalization. Otherwise request->node_building->crash can finish the run
                # with its exact request head still unacknowledged.
                if await self._close_developer_sentinel_once():
                    continue
                speculative_state = fold(self.store.read_all())
                if self._head_request(speculative_state) is not None or speculative_state.buildings:
                    await self._run_card_session(
                        [],
                        speculative_state,
                        max_es,
                        None if max_s is None else start + max_s,
                    )
                    continue

            # The translated Card denominator changes whenever an attempt becomes tombstoned/gated or
            # a speculative freshness drop lands.  Refresh it BEFORE the Strategist reads
            # ``node_budget_frac``; the post-cadence refresh below is still required because a live
            # policy swap rebuilds ``policy.max_nodes`` from its unextended base.
            self._refresh_speculation_budget(state, events=decision_events)
            state = self._run_cadences(state)
            post_cadence_events = self.store.read_all()
            post_cadence_seq = post_cadence_events[-1].seq if post_cadence_events else -1
            if post_cadence_seq != decision_seq:
                # Re-enter every gate after either an internal cadence append or a concurrent control.
                continue

            # Refresh after any in-loop policy swap so a live `add_nodes` extension is never lost. Card
            # mode translates the raw hard ceiling into the effective policy view: gated/tombstoned
            # Nodes stay hidden from ranking, but their already-reserved slots cannot be spent again.
            self._refresh_speculation_budget(state, events=post_cadence_events)

            if self._speculation_enabled():
                # Layer 5 freshness is live engine policy, never fold semantics. Drain one stale Node
                # and restart the turn; only a fully-clean fresh prefix may reach Card scoring.
                if await self._drop_stale_speculation():
                    continue
                fresh_events = self.store.read_all()
                fresh_seq = fresh_events[-1].seq if fresh_events else -1
                if fresh_seq != post_cadence_seq:
                    continue
                state = fold(fresh_events)

            actions = self._select_actions(state)
            if not actions:
                # Optional multi-seed confirmation pass (I12) before finishing:
                # re-evaluate the top-k under several seeds and record robust metrics.
                if (self.confirm_top_k > 0 and self.confirm_seeds > 0
                        and not self._already_confirmed(state)):
                    await self._confirm_phase(state)
                    continue
                # D1 holdout-gated promotion: AFTER the confirm pass (so confirmed means pick the
                # top-k), re-score the val-leaders' predictions on the reserved holdout partition.
                # Free (no re-training) and replay-safe (gated per node). The fold then lets the
                # unseen signal pick the champion (holdout_select) + surfaces the gap.
                if self._holdout_pending(state):
                    await self._holdout_phase(state)
                    continue
                # HITL gate (I21, ADR-11): pause for human approval of the final best.
                # Approval flows through the event log (a UI/human appends
                # `approval_granted`); the engine, sole writer of domain events, reads it.
                if self.require_approval and not state.approved:
                    best = state.best()
                    # No real candidate can ever be approved. Do not create an impossible HITL gate;
                    # fall through to the normal report/finalization path with an explicit reason.
                    if best is not None and not state.awaiting_approval:
                        self.store.append(EV_APPROVAL_REQUESTED, {
                            "node_id": best.id, "generation": best.attempt,
                            "metric": best.metric, "after_seq": decision_seq})
                        # An abort/reset can win between the stale loop snapshot and this append. Fold
                        # again and stop only if the exact lifecycle request actually landed; otherwise
                        # keep the engine alive to select/confirm the remaining candidate set.
                        requested = fold(self.store.read_all())
                        if (not requested.awaiting_approval
                                or requested.approval_subject != best.id
                                or requested.approval_generation != best.attempt):
                            continue
                    if best is not None:
                        break  # awaiting approval -> stop without finishing
                finish_data = ({"reason": "no_eligible_candidate"}
                               if state.best() is None else {})
                if self._finish_with_report_if_quiescent(
                        state, finish_data, after_seq=decision_seq):
                    break
                continue

            ablates = [a for a in actions if a["kind"] == "ablate"]
            if ablates:
                for a in ablates:
                    if "_scores" in a:   # surface "why this node" for ablates too (was dropped: this
                        self.store.append(EV_POLICY_DECISION,   # branch continues before the create loop)
                                          {"scores": a["_scores"], "chosen": a.get("_chosen"),
                                           "reason": a.get("_reason")})
                    await self._ablate(a["parent_id"])
                continue

            evals = [a for a in actions if a["kind"] == "evaluate"]
            creates = [a for a in actions
                       if a["kind"] in ("draft", "improve", "debug", "merge")]

            if creates:
                # Runaway trip: created too many nodes with ZERO reaching terminal since the last
                # progress. A healthy run creates a batch then evaluates it (which resets the counter);
                # only a spin (empty-nodes fold re-minting the same id) grows this unbounded. Cap
                # generously so operator injects / wide seed batches never false-trip. (Phase 2 may build
                # FEWER than len(creates) when the batch can't diversify to full width — counting the
                # planned width here only makes the guard trip marginally sooner, i.e. fails safe.)
                _created_no_terminal += len(creates)
                if _created_no_terminal > max(self.policy.max_nodes, 4) * 3 + 50:
                    if self._finish_with_report_if_quiescent(state, {
                            "reason": "stuck: node creation not converging (no node reached terminal)"},
                            after_seq=decision_seq):
                        break
                    continue
                self._create_paused = False   # set by _create_node's developer_crash circuit-breaker
                if self._speculation_enabled():
                    receipt_owned = [META_CARD_ID in action for action in creates]
                    # One turn has one authority. A mixed lane could stage new work while claiming a
                    # stale selection snapshot, so retain the serial spine's existing fail-closed rule.
                    if any(receipt_owned) and not all(receipt_owned):
                        _created_no_terminal -= len(creates)
                        continue

                    if not any(receipt_owned):
                        # Raw policy actions do not yet name executable work. Author their concrete
                        # Ideas and durable Cards now, but deliberately leave every Node slot unowned;
                        # the next fresh fold must select them before a producer can be requested.
                        stageable = speculative_raw_actions(
                            state,
                            self.policy,
                            self.policy.max_nodes,
                            scoring=getattr(self, "_card_scoring", None),
                            resource_envelope=self._resource_envelope(),
                        )
                        if stageable:
                            # Author one work item at a time. The live depth is filled by the isolated
                            # steady-state proposer while eval runs; staging an unreserved wide seed
                            # batch here only creates stale inventory if the first fast eval moves best.
                            one = stageable[:1]
                            _created_no_terminal -= max(0, len(creates) - len(one))
                            if self._stage_card_creates(one, state):
                                continue
                            # A rejected staging attempt gets one ordinary serial compatibility try;
                            # it must not poll the same paid proposal outside the runaway accounting.
                        # Unsupported/custom scorer semantics retain the exact serial compatibility
                        # path below; a Card it cannot score must never be staged/reused in a loop.

                    # A positive depth is useful only with a genuinely isolated role pair. If the
                    # configured factory cannot provide one, fall through to the safe serial Card
                    # claim below. Otherwise request/session is the sole build path: a lost selection
                    # CAS restarts from a fresh fold and never silently converts to serial execution.
                    serial_fallback = any(
                        self._card_requires_serial_fallback(action.get(META_CARD_ID))
                        for action in creates
                    )
                    if (all(receipt_owned)
                            and self._producer_role_pair() is not None
                            and not serial_fallback):
                        if self._request_card_build():
                            await self._run_card_session(
                                [],
                                fold(self.store.read_all()),
                                max_es,
                                None if max_s is None else start + max_s,
                            )
                        continue
                # Variant-1 parallel BUILD: seed/explore DRAFTS are independent, so build (research + code)
                # up to `parallel_build` at once, each on its OWN pooled (researcher, developer) pair + its
                # own pre-reserved id (reserved serially under _id_lock, then fanned out in a task-group of
                # worker threads). Non-draft creates (improve/merge/debug depend on a parent's result and
                # use role helpers not yet pool-threaded) and the no-pool config fall through to the serial
                # loop below — byte-identical to before.
                _card_reservations: Optional[list[_BuildReservation]] = None
                if any(META_CARD_ID in action for action in creates):
                    # A Card lane is one authority decision. Mixing receipt-owned and proposer-owned
                    # work in it would make the score-to-claim fence ambiguous, so fail closed.
                    if not all(META_CARD_ID in action for action in creates):
                        _created_no_terminal -= len(creates)
                        continue
                    _card_reservations = self._claim_existing_card_builds(creates)
                    if _card_reservations is None:
                        _created_no_terminal -= len(creates)
                        continue
                _card_reservation_by_id = {
                    reservation.card_id: reservation
                    for reservation in (_card_reservations or [])
                }
                _pb_pairs = (self._build_role_pairs(min(self._llm_parallel, len(creates)))
                             if (self._llm_parallel > 1 and len(creates) > 1
                                 and all(a.get("kind") == "draft" for a in creates)
                                 and not any(META_CARD_ID in a for a in creates)) else None)
                if _pb_pairs and len(_pb_pairs) > 1:
                    _fan = len(_pb_pairs)
                    for _i in range(0, len(creates), _fan):
                        _chunk = creates[_i:_i + _fan]
                        # Phase 2: ONE shared-researcher pass produces the DISTINCT seed ideas for this
                        # chunk (avoidance-driven diversity + novelty gate); the fan-out below then only
                        # IMPLEMENTS them per-developer, so we never pay N independent research rolls that
                        # collide. If the researcher can't diversify to the full width, build only as many
                        # nodes as we got distinct ideas — the loop re-plans the rest next iteration.
                        # RE-FOLD before each chunk (review finding #6): a batch WIDER than the fan-out is
                        # built in multiple chunks; earlier chunks' nodes are now in the log, so re-folding
                        # lets THIS chunk's vs-history novelty gate see them and not re-propose their ideas
                        # (the serial path gets this for free — each node lands before the next proposes).
                        if _i:
                            state = fold(self.store.read_all())
                        _ideas = self._propose_batch(state, len(_chunk))
                        _dropped_batch = list(
                            getattr(self, "_pending_batch_dropped", None) or [])
                        if not _ideas:
                            for _drop in _dropped_batch:
                                if isinstance(_drop, dict) and isinstance(_drop.get("idea"), Idea):
                                    self._record_node_less_card(
                                        _drop["idea"],
                                        reason=str(_drop.get("reason") or "proposal_rejected")[:160],
                                        steering_context=_drop.get("steering_context", []),
                                    )
                            self._pending_batch_dropped = []
                            self._pending_batch_novelty_gated = []
                            continue
                        # Per-idea FOREAGENT telemetry snapshots captured by _propose_batch (aligned 1:1
                        # with _ideas), so each build emits ITS OWN hypothesis_ranked/foresight_selected.
                        _telem = getattr(self, "_pending_batch_telemetry", None) or [None] * len(_ideas)
                        _chunk = _chunk[:len(_ideas)]
                        for _a in _chunk:               # surface the audit events only for what we build
                            if "_scores" in _a:
                                self.store.append(EV_POLICY_DECISION,
                                                  {"scores": _a["_scores"], "chosen": _a.get("_chosen"),
                                                   "reason": _a.get("_reason")})
                            self._append_rung_promotion(_a)
                        # Proposal is complete before durable reservation: a native Card receipt must
                        # bind the exact immutable statement/action.  The MAIN TASK serially commits
                        # card_added -> node_building for each idea, then workers only implement.
                        _reserved = [
                            self._reserve_node_build(
                                _a, _idea, scored_against=state.best_node_id,
                                source="researcher",
                                steering_context=(
                                    (_tel or {}).get("_steering_context", [])
                                    if isinstance(_tel, dict) else []),
                            )
                            for _a, _idea, _tel in zip(_chunk, _ideas, _telem)
                        ]
                        # Accepted preplanned ids are durable first. Node-less rejects then receive fresh
                        # closed Card ids without shifting any reservation the workers are about to use.
                        for _drop in _dropped_batch:
                            if isinstance(_drop, dict) and isinstance(_drop.get("idea"), Idea):
                                self._record_node_less_card(
                                    _drop["idea"],
                                    reason=str(_drop.get("reason") or "proposal_rejected")[:160],
                                    steering_context=_drop.get("steering_context", []),
                                )
                        self._pending_batch_dropped = []
                        # The accepted Ideas are now durably reserved, so the unreserved compatibility
                        # capability is no longer reachable or needed.
                        self._pending_batch_novelty_gated = []
                        # Cost guardrail (Phase 4): surface the concurrent build fan-out width in the
                        # trace (spans.jsonl / OTel). `built` is structurally bounded by `fan` (=len of
                        # the role pool) which is bounded by `parallel_build`, so a batch can never exceed
                        # the configured fan-out — this span makes the actual per-batch cost observable.
                        with self.tracer.span("parallel_build_batch", fan=_fan, built=len(_chunk),
                                              parallel_build=self._llm_parallel):
                            async with anyio.create_task_group() as _tg:
                                for _a, _res, _pair, _idea, _tel in zip(
                                        _chunk, _reserved, _pb_pairs, _ideas, _telem):
                                    if _res is None:
                                        continue
                                    # _create_node_guarded: an UNEXPECTED exception in one build becomes a
                                    # node_failed terminal for its already-reserved id (node_building was
                                    # appended up front) instead of tearing down the task group and killing
                                    # the whole run — the rest of the concurrent batch still finishes.
                                    _tg.start_soon(anyio.to_thread.run_sync,
                                                   functools.partial(self._create_node_guarded,
                                                                  _a, _pair, _res, _idea, _tel))
                        # Circuit breaker under concurrency: `start_soon` does not yield, so no worker runs
                        # until the task group JOINS above — the pause flag can only be observed HERE, after
                        # the whole chunk finishes. So a developer/build crash pauses after AT MOST this one
                        # chunk (bounded by the fan-out width), not mid-chunk; stop before the next chunk.
                        if self._create_paused:
                            break
                    continue
                for _create_index, a in enumerate(creates):
                    reservation = (_card_reservation_by_id.get(a.get(META_CARD_ID))
                                   if META_CARD_ID in a else None)
                    if META_CARD_ID in a and reservation is None:
                        continue
                    if "_scores" in a:   # policy exposed candidate scores -> surface "why this node"
                        self.store.append(EV_POLICY_DECISION,
                                          {"scores": a["_scores"], "chosen": a.get("_chosen"),
                                           "reason": a.get("_reason")})
                    self._append_rung_promotion(a)
                    if META_CARD_ID in a:
                        # The complete Card lane was claimed atomically above, before the first slow
                        # build could make its siblings ineligible through the evaluate-all prefix.
                        try:
                            self._create_node(a, reserved=reservation)
                        except BaseException:
                            for later in (_card_reservations or [])[_create_index + 1:]:
                                self._fail_reserved_build(
                                    node_id=later.node_id,
                                    card_id=later.card_id,
                                    generation=0,
                                    error="Card build batch stopped by an unexpected build error",
                                    reason="build_batch_cancelled",
                                )
                            raise
                    else:
                        self._create_node(a)  # sequential -> deterministic ids/proposals
                    if self._create_paused:
                        for later in (_card_reservations or [])[_create_index + 1:]:
                            self._fail_reserved_build(
                                node_id=later.node_id,
                                card_id=later.card_id,
                                generation=0,
                                error="Card build batch stopped after a Developer crash",
                                reason="build_batch_cancelled",
                            )
                        # A developer_crash auto-PAUSED the run (LLM unreachable / hard error). STOP the
                        # rest of the batch instead of building every seed and paying the full within-call
                        # retry/backoff on each — honouring the "PAUSE on the FIRST developer_crash"
                        # guarantee the crash branch documents. The loop re-folds paused=True at the top
                        # and finalizes; a plain `resume` continues once the cause is fixed.
                        break
                continue

            if self._speculation_enabled():
                await self._run_card_session(
                    evals,
                    state,
                    max_es,
                    None if max_s is None else start + max_s,
                )
            else:
                await self._dispatch_evals(evals, state, max_es)

        # Finalize (extracted to looplab/engine/finalize.py, a pure move): budget summary,
        # diversity archive, LLM cost roll-up, case store + reflection note, read-model,
        # trace.json + tree.html. Event emission order is preserved exactly.
        return finalize_run(self, entry_finished=entry_finished, start_time=start)

    # -------------------------------------------------- run() phase helpers (§4 decomposition)
    # Pure structural decomposition of run(): each method is a cohesive span lifted verbatim so the
    # loop body reads as a table of guarded steps. No behavior/ordering/gating change — every event
    # emission, _write_lock point, and fold site stays exactly where it was in the original run().

    def _hard_node_reservation_limit(self, state: RunState) -> int:
        """Return the operator-owned ceiling for distinct durable Node reservations."""

        base_limit = getattr(self, "_base_max_nodes", None)
        if base_limit is None:
            # Compatibility for narrowly-constructed Engine test doubles and older embedders. A fully
            # initialized Engine always owns ``_base_max_nodes``; only the partial-object seam falls
            # back to the policy/configured value, and an unconfigured object fails closed at zero.
            base_limit = getattr(getattr(self, "policy", None), "max_nodes", None)
        if base_limit is None:
            base_limit = getattr(self, "max_nodes", 0)
        try:
            base_limit = int(base_limit)
        except (TypeError, ValueError, OverflowError):
            base_limit = 0
        return max(
            0,
            base_limit + int(state.budget_overrides.get("add_nodes", 0) or 0),
        )

    def _unmaterialized_card_request_indices(self, state: RunState) -> set[int]:
        """Return exact outstanding request indexes that still own a future Node slot.

        Materialized ownership is matched as a multiset: one strict speculative ``node_building``
        marker or one not-yet-linked speculative Node can discharge only one request with the exact
        ``(card_id, card_build_generation)`` identity. Ordinary Card build markers, mismatched
        generations, and Nodes already linked by an accepted ``card_build_done`` cannot discharge a
        later duplicate request. The returned absolute indexes make conversion credit head-specific.
        """

        done = max(0, min(int(state.card_builds_done), len(state.card_build_requests)))
        materialized: dict[tuple[str, int], int] = {}

        def _add_materialized(key: tuple[str, int]) -> None:
            materialized[key] = materialized.get(key, 0) + 1

        linked_node_ids = {
            node_id for node_id in state.speculative_nodes
            if type(node_id) is int
        }
        # A valid node_created clears its build marker. If a corrupt prefix leaves both projections,
        # count the physical node id once, preferring the created Node below.
        for node_id, marker in state.buildings.items():
            if (
                type(node_id) is not int
                or node_id in state.nodes
                or node_id in linked_node_ids
                or not isinstance(marker, Mapping)
                or marker.get("node_id") != node_id
                or marker.get("speculative") is not True
            ):
                continue
            card_id = marker.get("card_id")
            generation = marker.get("card_build_generation")
            if isinstance(card_id, str) and card_id and type(generation) is int and generation >= 0:
                _add_materialized((card_id, generation))

        for node in state.nodes.values():
            if (
                node.id in linked_node_ids
                or node.speculative is not True
                or not isinstance(node.idea.card_id, str)
                or not node.idea.card_id
                or type(node.card_build_generation) is not int
            ):
                continue
            _add_materialized((node.idea.card_id, node.card_build_generation))

        unmaterialized: set[int] = set()
        for request_index in range(done, len(state.card_build_requests)):
            request = state.card_build_requests[request_index]
            key = self._request_key(request)
            if key is None:
                continue
            available = materialized.get(key, 0)
            if available:
                materialized[key] = available - 1
            else:
                unmaterialized.add(request_index)
        return unmaterialized

    def _unmaterialized_card_reservations(self, state: RunState) -> int:
        """Count durable requests not yet represented by a distinct physical Node reservation."""

        return len(self._unmaterialized_card_request_indices(state))

    def _node_reservation_slots_remaining(
        self,
        state: RunState,
        *,
        events=None,
        consume_request: bool = False,
    ) -> int:
        """Return strict remaining physical slots at every new-Node append boundary.

        ``consume_request`` is used only while converting the exact outstanding speculative head into
        ``node_building``; that request already owns one slot and must not be charged twice.
        """

        if events is None:
            events = self.store.read_all()
        raw_used = self._node_id_ceiling(events, state)
        unmaterialized = self._unmaterialized_card_request_indices(state)
        request_used = len(unmaterialized)
        head_index = max(
            0, min(int(state.card_builds_done), len(state.card_build_requests)),
        )
        if consume_request and head_index in unmaterialized:
            request_used -= 1
        return max(0, self._hard_node_reservation_limit(state) - raw_used - request_used)

    def _refresh_speculation_budget(self, state: RunState, *, events=None) -> None:
        """Refresh the live policy denominator without refunding the hard Node admission ceiling.

        Card selection ranks an effective view that excludes tombstoned and currently gated Nodes. The
        configured ``max_nodes + add_nodes`` limit, however, bounds physical Node reservations. Translate
        its remaining raw slots into the effective denominator so policy intent keeps the filtered view
        while every slot already reserved — including a failed reservation gap — remains spent. This
        overrides the SpeculationMixin helper so serial and speculative Card admission share one limit.
        """
        hard_limit = self._hard_node_reservation_limit(state)
        if events is None:
            events = self.store.read_all()
        raw_used = self._node_id_ceiling(events, state)
        request_used = self._unmaterialized_card_reservations(state)
        effective_used = (
            card_budget_used(state) if self.card_driven_selection else len(state.nodes)
        )
        self.policy.max_nodes = effective_used + max(
            0, hard_limit - raw_used - request_used,
        )

    def _append_rung_promotion(self, action: dict) -> bool:
        """Durably append one row per exact ASHA halving receipt, including across resume.

        Widened Card lanes stamp the same rung/survivor decision on every chosen parent.  Speculation
        commits those parents in separate turns, so an in-memory per-lane set cannot deduplicate them.
        The append-only log is the authority: retry tail races, but suppress an exact receipt already
        recorded by an ordinary or speculative path.  A changed rung or survivor set remains distinct.
        """
        if action.get("_rung") is None:
            return False
        payload = {"rung": action["_rung"], "survivors": action.get("_promoted", [])}
        for _attempt in range(64):
            events = self.store.read_all()
            if any(
                event.type == EV_RUNG_PROMOTED
                and event.data.get("rung") == payload["rung"]
                and event.data.get("survivors", []) == payload["survivors"]
                for event in events
            ):
                return False
            tail = events[-1].seq if events else -1
            try:
                with self._id_lock:
                    self.store.append(
                        EV_RUNG_PROMOTED, payload, expected_last_seq=tail,
                    )
                return True
            except EventStoreConcurrencyError:
                continue
        return False

    def _select_actions(self, state: RunState) -> list[dict]:
        """Apply the explicit macro-selection authority order for one fresh fold."""
        # Receipt-backed Card selection is the narrowest authority and therefore wins when both opt-in
        # selectors are enabled. The default false flag takes the exact historical branches below.
        if self.card_driven_selection:
            return card_next_actions(
                state, self.policy, self.policy.max_nodes,
                scoring=getattr(self, "_card_scoring", None),
            )
        if self.agent_drives_actions:
            return self._agent_next_actions(state)
        return self.policy.next_actions(state)

    def _run_start_pinned_values(self) -> dict:
        """The config values whose run-start record, not a later snapshot, owns re-entry semantics."""
        values = {
            "holdout_fraction": self._holdout_fraction,
            "holdout_select": self._holdout_select,
            "select_verifier": self._select_verifier,
            "select_verifier_samples": self._select_verifier_samples,
            "verifier_ci_tie": self._verifier_ci_tie,
        }
        legacy_fields = RUN_START_PINNED_FIELDS - {"card_driven_selection", "speculation_depth"}
        if values.keys() != legacy_fields:
            raise RuntimeError("run-start pinned settings contract drifted")
        # Keep the default run_started payload byte-identical. Replay treats an absent key as false;
        # only the opt-in path needs an additive durable marker.
        if self.card_driven_selection:
            values["card_driven_selection"] = True
        if self._speculation_implementation_digest:
            values["speculation_implementation_digest"] = (
                self._speculation_implementation_digest
            )
        if self._speculation_runtime_scope_sha256:
            values["speculation_runtime_scope_sha256"] = (
                self._speculation_runtime_scope_sha256
            )
        # Preserve the default run_started bytes just like the Card selector flag. Replay supplies
        # zero for an absent key, while an enabled overlap treatment must be durable across resume.
        if ((self.card_driven_selection and self.speculation_depth)
                or self._speculation_gate_calibration):
            values["speculation_depth"] = self.speculation_depth
        if self._speculation_gate_calibration:
            if (
                not self._speculation_gate_admitted
                or not self._speculation_implementation_digest
                or not self._speculation_runtime_scope_sha256
                or self._speculation_calibration_profile_digest
                != SPECULATION_CALIBRATION_PROFILE_DIGEST
                or not self._speculation_calibration_gpu_inventory
                or type(self._speculation_calibration_seed) is not int
                or self._speculation_policy_scope != SPECULATION_POLICY_SCOPE
            ):
                raise RuntimeError("calibration reached run start outside its exact profile envelope")
            values.update({
                "speculation_calibration_profile_digest": (
                    self._speculation_calibration_profile_digest),
                "speculation_calibration_gpu_inventory": list(
                    self._speculation_calibration_gpu_inventory),
                "speculation_calibration_seed": self._speculation_calibration_seed,
                "speculation_policy_scope": self._speculation_policy_scope,
            })
        elif self.card_driven_selection and self.speculation_depth:
            if (
                not self._speculation_gate_admitted
                or not self._speculation_gate_receipt_digest
                or not self._speculation_runtime_scope_sha256
            ):
                raise RuntimeError("positive Card speculation reached run start without gate evidence")
            values["speculation_gate_receipt_digest"] = (
                self._speculation_gate_receipt_digest
            )
            values["speculation_policy_scope"] = self._speculation_policy_scope
        return values

    def _setup_phase(self, state: RunState) -> None:
        # Per-RUN reset of the dep-install circuit breaker: it is a module global, so in the long-lived
        # `looplab ui` server a run that latched (egress blip) would leave auto-install disabled for the
        # next run in the same process until some pip call happens to respond.
        try:
            from looplab.runtime.deps import reset_install_latch
            reset_install_latch()
        except Exception:  # noqa: BLE001 - best-effort; a missing helper must not block setup
            pass
        # SETUP-COMPLETION GATE (arch-review §3 P0-3): gate on `setup_done` (folded from
        # setup_finished), NOT on run_id. run_started is appended in the MIDDLE of this block — before
        # AGENTS.md/provenance/host-grading/profiling and the leakage hard-stop — so a crash right
        # after it used to make every later resume skip the rest of preflight (leakage included)
        # forever. Gating on setup_done re-runs the body until it actually completes. Legacy logs that
        # never emitted setup_finished but already reached a node (or finished) are treated as
        # set-up-complete via `state.nodes`/`state.finished`, so they never re-run setup.
        # P0-3 material re-verification: on a PRE-node resume, re-run preflight if setup completed
        # against a DIFFERENT material manifest than we now hold (edited config / changed data or
        # workspace) — the `setup_done` boolean alone would skip the leakage/grounding checks on the
        # changed inputs. Only pre-node (a node present => the run is underway; mid-run drift is handled
        # by workspace_changed below). Re-running records a fresh setup_finished with the new manifest,
        # so this can never loop. Old logs (no recorded manifest) keep the pure-boolean behavior.
        _setup_stale = bool(state.setup_done and not state.nodes and state.setup_manifest
                            and self._setup_manifest() != state.setup_manifest)
        if not (state.setup_done or state.nodes or state.finished) or _setup_stale:
            # SETUP PHASE (task + data), an explicit, ONLINE-watchable phase: the pre-node work
            # (fingerprint the workspace, hash data provenance, profile columns, write AGENTS.md) is
            # otherwise silent between run_started and the first node. `setup_started` +/ `setup_step`
            # + `setup_finished` events land in the activity feed live, and a `setup` span (node_id=-1)
            # captures the trace so the UI's Setup pseudo-node shows what happened. setup_finished is
            # now folded (setup_done); the others stay pure observability.
            _su_t0 = time.time()
            self.store.append(EV_SETUP_STARTED,
                              {"phase": "task+data", "repo": bool(self._repo_spec),
                               "goal": (self.task.goal or "")[:200]})
            def _su_step(step: str, **detail):
                self.store.append(EV_SETUP_STEP, {"step": step, **detail})
            with self.tracer.span("setup", new_trace=True, node_id=-1) as _su:
                def _ev(name, **kv):
                    if _su is not None:
                        _su.event(name, **kv)
                cfg_hash = hashlib.sha256(
                    orjson.dumps(self.task.model_dump(mode="json"))
                ).hexdigest()[:12]
                # Reproducibility (item #4): pin the editable repo(s)+data fingerprint at start so a
                # resume can tell whether the source workspace changed underneath.
                _ev("workspace_fingerprint")
                wf = self._workspace_fingerprint()
                _su_step("workspace fingerprint", sources=list(wf.keys()))
                # run_started is the one-time identity anchor: append it only if it isn't already
                # recorded, so a resume RE-ENTERING setup after a crash-right-after-run_started (P0-3)
                # re-runs the REST of preflight (leakage) without minting a second run_started.
                if not state.run_id:
                    self.store.append(
                        EV_RUN_STARTED,
                        {
                            "run_id": self.run_dir.name,
                            "task_id": self.task.id,
                            "goal": self.task.goal,
                            "direction": self.task.direction,
                            "config_hash": cfg_hash,
                            "workspace": wf,
                            # P0-5 environment identity: pin the interpreter + key-lib versions so a
                            # resume can flag a library upgrade that breaks bit-reproducibility.
                            "env": self._env_fingerprint(),
                            # P0-5 dirty-input enumeration: which repo files were uncommitted at start
                            # (repo tasks only; a clean/non-repo run records []). Provenance on top of
                            # the workspace content hash in `wf`.
                            "dirty_inputs": (self._dirty_inputs(wf) if self._repo_spec else []),
                            # T2 trust enforcement: recorded here so the pure fold applies the same
                            # gate on replay/resume (config isn't available to `replay.fold`). Absent in
                            # old logs -> "audit" -> byte-identical legacy selection.
                            "trust_gate": self.trust_gate,
                            # Holdout and verifier policy are immutable run-start semantics. Re-entry
                            # restores this shared contract from the fold rather than accepting a later
                            # snapshot edit that would mix incomparable scores or selection rules.
                            **self._run_start_pinned_values(),
                            "select_verifier_contract": VERIFIER_SELECTION_CONTRACT,
                        },
                    )
                # AGENTS.md (I18): task/contract context for coding-agent backends. Runtime line is
                # honest about libs/hardware — capable tasks get the auto-install capability sentence,
                # offline/synthetic tasks stay numpy+stdlib (task_runtime_caps returns None for those).
                from looplab.core.hardware import detect_gpu, task_runtime_caps
                _md_caps = task_runtime_caps(self.task, auto_install=self._auto_install_deps,
                                             gpu=detect_gpu() if self._auto_install_deps else None)
                (self.run_dir / "AGENTS.md").write_text(
                    generate_agents_md(self.task, runtime_caps=_md_caps), encoding="utf-8")
                _ev("agents_md")
                _su_step("wrote AGENTS.md")
                # D4 data provenance: pin a content hash of every task asset/dataset into the run so a
                # result is tied to the exact data (repo tasks also pin via `workspace`). Reproducibility.
                prov = {name: hashlib.sha256(
                            c.encode("utf-8") if isinstance(c, str) else bytes(c)).hexdigest()[:16]
                        for name, c in (self._assets or {}).items()}
                if prov:
                    self.store.append(EV_DATA_PROVENANCE, {"assets": prov})
                    _ev("data_provenance", n=len(prov))
                    _su_step("data provenance", assets=list(prov))
                # Out-of-process host-side grading active: record WHICH scorer + how many held-out labels
                # (NEVER the labels themselves — the log is readable). Surfaced in the Trust panel.
                if self._host_grader is not None:
                    hg = self._host_grader
                    evt = {
                        "scorer": hg.get("scorer", "rmse"),
                        "predictions": self._graded_output_name()}
                    if hg.get("kind") == "mlebench":          # real MLE-bench: answers live in the
                        evt["competition"] = hg.get("competition")   # mle-bench data dir, never here —
                        # so there is no in-memory label list to count; n_labels=0 would mislead the Trust
                        # panel into "nothing held out". Omit it; `competition` signals host-held answers.
                    else:
                        evt["n_labels"] = len(hg.get("labels") or [])
                    self.store.append(EV_HOST_GRADING, evt)
                # Grounding pre-phase (I16): profile the dataset if the task exposes one.
                cols = getattr(self.task, "columns", None)
                if callable(cols):
                    self.store.append(EV_DATA_PROFILED, {"columns": profile_dataset(cols())})
                    _ev("data_profiled")
                    _su_step("data profiled")
                # Leakage-first grounding (I9): if the task exposes split/feature/target/time
                # data and a leak is detected, refuse to run — don't produce results on leaky data.
                leakage_blocked = self._leakage_blocks()
            # P0-3: bind this completion to the material it verified (reuse the wf computed above), so a
            # later resume can tell "done for THIS material" from "done for material that has changed".
            self.store.append(EV_SETUP_FINISHED, {"seconds": round(time.time() - _su_t0, 3),
                                                  "manifest": self._setup_manifest(wf=wf)})
            if leakage_blocked:
                # Preserve `_setup_phase`'s direct-call contract while using the same final-report
                # CAS as every other completion. If a control races this append, run()'s top-level
                # leakage gate refolds and retries instead of losing the intent.
                setup_events = self.store.read_all()
                setup_state = fold(setup_events)
                setup_seq = setup_events[-1].seq if setup_events else -1
                self._finish_with_report_if_quiescent(
                    setup_state, {"reason": "leakage"}, after_seq=setup_seq)
        elif self._repo_spec and state.workspace and not state.workspace_changed:
            # Resume (item #4): the editable workspace is copied fresh each node, so if the
            # operator's repo changed since the run started, later nodes silently evaluate a
            # DIFFERENT codebase. Record it instead of pretending the run is reproducible.
            now = self._workspace_fingerprint()
            if now != state.workspace:
                self.store.append(EV_WORKSPACE_CHANGED, {"was": state.workspace, "now": now})
        # P0-5 environment drift: on ANY resume where an env was pinned at run start, flag a Python/
        # library change — a run continued after an upgrade is no longer bit-reproducible, so record it
        # instead of pretending it is. Diagnostic-only (mirrors workspace_changed). state.env is None on
        # the first run (run_started is appended mid-setup, after this fold) and on old logs -> skipped.
        if state.env is not None and not state.env_changed:
            # `not state.env_changed` (F18): emit the drift note ONCE. Without the folded-flag gate a
            # run resumed repeatedly after an env upgrade re-appended an identical env_changed every time.
            _cur_env = self._env_fingerprint()
            if _cur_env != state.env:
                self.store.append(EV_ENV_CHANGED, {"was": state.env, "now": _cur_env})

    def _require_pinned_speculation_receipt(self, entry: RunState) -> None:
        """Fail closed on positive-depth or calibration re-entry before any log mutation."""
        profile_digest = str(getattr(
            entry, "speculation_calibration_profile_digest", "") or "")
        calibration_gpu = getattr(entry, "speculation_calibration_gpu_inventory", None)
        calibration_seed = getattr(entry, "speculation_calibration_seed", None)
        recorded_depth = getattr(entry, "speculation_depth", 0)
        recorded_impl = str(getattr(
            entry, "speculation_implementation_digest", "") or "")
        recorded_scope = str(getattr(entry, "speculation_policy_scope", "") or "")
        recorded_receipt = str(getattr(
            entry, "speculation_gate_receipt_digest", "") or "")
        recorded_runtime_scope = str(getattr(
            entry, "speculation_runtime_scope_sha256", "") or "")
        recorded_calibration = bool(
            profile_digest or calibration_gpu or calibration_seed is not None)
        # Treat every durable speculation authority/prefix as gated, even when another field was
        # corrupted or omitted.  In particular card=false must not turn a receipt/implementation/
        # policy/depth prefix into an inert-looking log that recovery or command ACK may mutate.
        recorded_marker = bool(
            recorded_calibration
            or recorded_impl
            or recorded_scope
            or recorded_receipt
            or recorded_runtime_scope
            or (type(recorded_depth) is int and recorded_depth > 0)
        )
        if not recorded_marker:
            return

        def reject() -> None:
            raise SpeculationAuthorizationError(
                "cannot resume Card speculation/calibration without the exact validated "
                "run-start receipt/profile, implementation, policy, depth, seed and GPU pins"
            )

        if (
            not isinstance(getattr(entry, "run_id", None), str)
            or not entry.run_id.strip()
            or entry.run_id != self.run_dir.name
            or not recorded_impl
            or not self._speculation_implementation_digest
            or recorded_impl != self._speculation_implementation_digest
            or not self._speculation_gate_admitted
            or not recorded_runtime_scope
            or recorded_runtime_scope != self._speculation_runtime_scope_sha256
            or getattr(entry, "card_driven_selection", False) is not True
            or type(recorded_depth) is not int
            or recorded_depth != self.speculation_depth
            or recorded_scope != SPECULATION_POLICY_SCOPE
            or self._speculation_policy_scope != SPECULATION_POLICY_SCOPE
        ):
            reject()

        # The hidden evidence bootstrap is immutable: any control would invalidate the paired
        # measurement.  A public receipt, by contrast, admits the measured launch envelope and keeps
        # explicit Stage-6 operator controls available.  Those interventions remain in the event log
        # and the quality evidence reader rejects such a run as future calibration evidence.
        if self._speculation_gate_calibration and (
            self._policy_name != SPECULATION_POLICY_SCOPE
            or bool(getattr(entry, "budget_overrides", None))
            or getattr(entry, "pending_strategy", None) is not None
            or bool(getattr(entry, "active_strategy", None))
        ):
            reject()

        if recorded_calibration:
            if (
                self._speculation_gate_calibration is not True
                or profile_digest != SPECULATION_CALIBRATION_PROFILE_DIGEST
                or self._speculation_calibration_profile_digest != profile_digest
                or not isinstance(calibration_gpu, list)
                or calibration_gpu != self._speculation_calibration_gpu_inventory
                or type(calibration_seed) is not int
                or calibration_seed != self._speculation_calibration_seed
                # Calibration never serializes its internal admission token as a public receipt.
                or bool(getattr(entry, "speculation_gate_receipt_digest", ""))
            ):
                reject()
            return

        if (
            self._speculation_gate_calibration
            or not recorded_receipt
            or recorded_receipt != self._speculation_gate_receipt_digest
        ):
            reject()

    def _reentry_repin(self) -> bool:
        _events = self.store.read_all()
        _entry = fold(_events)
        self._require_pinned_speculation_receipt(_entry)
        self._pending_finalize_scope = incomplete_finalize_scope(_events)
        # A failed finalize attempt is recorded as finished(reason=error) by the CLI guard, but its
        # durable stop is still pending. Treat that as NOT already finalized so the retry below can
        # write run_finished(aborted) and re-run budget/archive/case/cost wrap-up exactly once.
        entry_finished = bool(_entry.finished and self._pending_finalize_scope is None and not (
            _entry.stop_requested and str(_entry.stop_reason or "").lower() == "error"))
        # Restore Card authority before replaying the active Strategy: its conditional governance
        # grant for card_scoring depends on this run-start-pinned value, not the ambient snapshot.
        if _entry.run_id:
            self.card_driven_selection = _entry.card_driven_selection
            self.speculation_depth = _entry.speculation_depth
        # A7 Strategist: re-apply the last-decided strategy on (re)entry so a resumed run continues
        # with it WITHOUT re-consulting the Strategist (the decision lives in the event log).
        if _entry.active_strategy:
            self._apply_strategy(_entry.active_strategy)
        # R1-c resume-safety (invariant #6): the fold applies the RECORDED tie-break rule
        # (`st.select_verifier_tiebreak`, folded from run_started); re-pin the engine's live-verify gate
        # to match so `_maybe_verify_ties` produces atomic group scores consistently with what the fold
        # reads — not a possibly-changed live `LOOPLAB_SELECT_VERIFIER`. Its direct peer `holdout_select`
        # is re-pinned the same way below. Guard on `run_id` (set only by run_started): on a path where
        # setup hasn't recorded run_started yet, keep the live value rather than zero it from an empty fold.
        if _entry.run_id:
            self._select_verifier = _entry.select_verifier_tiebreak
            self._verifier_ci_tie = _entry.verifier_ci_tie   # R1-d: re-pin the recorded CI-tie rule
            self._select_verifier_samples = _entry.select_verifier_samples
        # D1 resume-safety: honor the holdout split the run ORIGINALLY committed to (recorded in
        # run_started), not a possibly-changed live `holdout_fraction` — otherwise nodes evaluated
        # before vs. after a config change would be scored on different splits and the champion pick
        # would mix incomparable metrics. Recorded holdout_select likewise wins on resume.
        if _entry.holdout_fraction is not None:
            self._holdout_fraction = _entry.holdout_fraction
            self._holdout_select = _entry.holdout_select
            # P0-2 freshly-hidden per-epoch holdout: rebuild the partition for the CURRENT search
            # epoch. A run reopened after finishing (search_epoch>=1) then scores its new candidates
            # on a never-disclosed split instead of the one revealed at the prior finish ('already-
            # seen exam'). Epoch 0 rebuilds the byte-identical original partition, so a normal
            # single-epoch run (and every replay of an existing log) is unchanged.
            self._holdout_idx = self._build_holdout_idx(self._holdout_fraction, _entry.search_epoch)
            self._holdout_epoch = _entry.search_epoch
        # E4: cross-run meta-learned priors. Excluding THIS run's id matters on resume: a run that
        # already mid-run-distilled its own comparative lessons (M6) must not read them back as if
        # they were another run's experience — its own results are already in the digest. The stamp
        # is taken BEFORE the read (a write landing in between is re-read next refresh — safe).
        self._lessons_seen_stamp = self._lessons_store_stamp()
        # §role-split: the RESEARCHER prior carries only R&D lessons; the DEVELOPER prior only its own
        # code-fix lessons (routed into the idea handed to the Developer via `_directed_idea`). One
        # scan builds both — the two role pools share every untagged lesson, so re-reading/re-embedding
        # the store per role is wasted work.
        _rid = _entry.run_id or None
        self._prior_note_text, self._dev_prior_note_text = \
            self._load_reflection_priors_both(exclude_run_id=_rid)
        return entry_finished

    def _recover_interrupted_builds(self, state: RunState) -> bool:
        """Terminalize build reservations left in-flight by a dead engine invocation.

        ``node_building`` is intentionally transient in the fold, but its id is a durable reservation.
        If the process dies before ``node_created``/``node_failed``, replay alone cannot know that no
        worker still owns it and the UI keeps rendering a live build forever. Entering ``run`` under the
        run lock is that proof: no prior engine worker can still be authoritative. Append one ordinary
        failure per surviving marker before setup/search; bare first-build reservations clear without
        fabricating a Node, while an interrupted in-place rebuild closes its current generation.
        """
        markers = getattr(state, "buildings", None) or {}
        recovered = False
        for node_id, marker in sorted(markers.items()):
            node = state.nodes.get(node_id)
            raw_generation = marker.get("generation") if isinstance(marker, dict) else None
            generation = (raw_generation if isinstance(raw_generation, int)
                          and not isinstance(raw_generation, bool) and raw_generation >= 0
                          else node.attempt if node is not None else 0)
            # CODEX AGENT: every durable reservation gets a terminal outcome before any new work. Merely
            # ignoring the transient projection resurrects its breathing card on every subsequent replay.
            card_id = (marker.get("card_id") if isinstance(marker, dict)
                       and isinstance(marker.get("card_id"), str) else None)
            current_card_id = (node.idea.card_id if node is not None and node.idea is not None
                               else None)
            self._fail_reserved_build(
                node_id=node_id,
                card_id=card_id,
                generation=generation,
                reason="build_interrupted",
                error="node build was interrupted before it committed",
                # An implement-reset reuses the Node's existing Card. A propose-reset owns a newly
                # minted Card whose marker id differs until node_created lands, so it must close just
                # like a bare first build.
                drop_card=(node is None or (card_id is not None and card_id != current_card_id)),
            )
            recovered = True
        return recovered

    def _apply_control_overrides(self, state: RunState) -> tuple[Optional[float], Optional[float]]:
        # Effective budgets: an operator may raise (or lower) them live via a `budget_extend`
        # control event (folded into state.budget_overrides), e.g. "keep going for 600s more".
        # max_seconds ("keep going 600s more") is a first-class operator budget extension via the
        # budget_extend control event, not an agent_control-governed knob — applied as-is.
        _bo = state.budget_overrides
        if self._speculation_gate_calibration and _bo:
            raise RuntimeError(
                "Card speculation calibration forbids runtime budget/resource overrides; "
                "max_nodes and the complete execution envelope are receipt-bound")

        def _finite_ceiling(key: str, fallback: Optional[float]) -> Optional[float]:
            raw = _bo.get(key)
            if raw is None or isinstance(raw, bool):
                return fallback
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError):
                return fallback
            return value if math.isfinite(value) and value > 0 else fallback

        # CODEX AGENT: apply stays total even for a manually constructed/forward-version RunState;
        # replay normally sanitizes these first, but a poison ceiling must never disable a budget.
        max_s = _finite_ceiling("max_seconds", self.max_seconds)
        # A `budget_extend` is a HUMAN control intent, NOT an agent decision: CONTROL_EVENTS are
        # UI/CLI-authored (see the engine-writer invariant), and the boss action-builder
        # (serve/routers/boss.py::_Action) can ONLY ever emit `add_nodes` — it carries no field for
        # any resource ceiling. So the budget fields below reach the log ONLY from an operator via the
        # /control endpoint. Apply them AS-IS ("a human can always change it via the UI/snapshot").
        # Gating them on `_agent_may("boss", …)` (as an earlier M4 pass did) protected against nothing
        # — no agent authors them — and only ever DROPPED the operator's OWN override, silently pinning
        # the run to the old cap. Agent-authored resource retunes (the Strategist's timeout/max_parallel)
        # remain governed by the matrix in `_apply_strategy`, which is where the M4 lock genuinely lives.
        max_es = _finite_ceiling("max_eval_seconds", self.max_eval_seconds)
        if "timeout" in _bo and not isinstance(_bo["timeout"], bool):
            try:
                _timeout = float(_bo["timeout"])
                if math.isfinite(_timeout) and _timeout > 0:
                    self.timeout = max(0.1, _timeout)
            except (TypeError, ValueError, OverflowError):
                pass
        # Legacy first, canonical last: a modern command carrying both spellings is deterministic.
        # Live 0 settles to serial width 1; only launch-time Settings retain hardware/eval AUTO.
        for _key in ("max_parallel", "eval_parallel"):
            if _key in _bo:
                try:
                    _value = _bo[_key]
                    if isinstance(_value, bool):
                        continue
                    if isinstance(_value, float) and (
                            not math.isfinite(_value) or not _value.is_integer()):
                        continue
                    _value = int(_value)
                    if not 0 <= _value <= 1024:
                        continue
                    self._eval_parallel = max(1, _value)
                except (TypeError, ValueError, OverflowError):
                    pass
        for _key in ("parallel_build", "llm_parallel"):
            if _key in _bo:
                try:
                    _value = _bo[_key]
                    if isinstance(_value, bool):
                        continue
                    if isinstance(_value, float) and (
                            not math.isfinite(_value) or not _value.is_integer()):
                        continue
                    _value = int(_value)
                    if not 0 <= _value <= 64:
                        continue
                    self._llm_parallel = max(1, _value)
                except (TypeError, ValueError, OverflowError):
                    pass
        # A canonical live control opts into the shared provider-call ceiling. Replay may also retain
        # the last canonical total beside a newer legacy build-only override so resume cannot silently
        # change broker behavior; legacy-only historical controls remain unbounded for compatibility.
        if "llm_broker_total" in _bo or "llm_parallel" in _bo:
            self._reconfigure_llm_broker(
                _bo.get("llm_broker_total", _bo.get("llm_parallel")))
        return max_s, max_es

    async def _defer_for_node_budget(self, state: RunState) -> bool:
        """Keep a durable node-creating control head live until ``budget_extend`` admits it.

        Returning immediately as "not served" would let the empty-action branch finalize with a
        stranded fork/inject/ablation request. Returning immediately as "served" would tight-spin.
        One bounded poll turn keeps the engine responsive to abort/pause/budget controls and survives
        process restart because the request itself remains the append-only queue authority.
        """

        if self._node_reservation_slots_remaining(state) >= 1:
            return False
        await anyio.sleep(0.5)
        return True

    @staticmethod
    def _pending_forced_ablation(state: RunState) -> Optional[dict]:
        """Return the first exact forced-ablation lifecycle not yet acknowledged."""

        forced = next((r for r in state.ablate_request_generations
                       if r.get("node_id") in state.nodes
                       and r.get("node_id") not in state.aborted_nodes
                       and not state.nodes[r["node_id"]].tombstoned
                       and state.nodes[r["node_id"]].attempt == r.get("generation")
                       and not any(a.get("parent_id") == r["node_id"]
                                   and a.get("generation") == r.get("generation")
                                   for a in state.ablations)), None)
        if forced is not None:
            return dict(forced)
        legacy = next((parent_id for parent_id in state.ablate_requests
                       if parent_id in state.nodes
                       and parent_id not in state.aborted_nodes
                       and not state.nodes[parent_id].tombstoned
                       and not any(a.get("parent_id") == parent_id
                                   for a in state.ablations)), None)
        if legacy is None:
            return None
        return {
            "node_id": legacy,
            "generation": state.nodes[legacy].attempt,
        }

    def _append_inject_failure(
        self,
        state: RunState,
        *,
        error: str,
        reason: str,
    ) -> bool:
        """Atomically append one positional inject failure and its replay gate."""

        request_idx = state.injects_done
        for _attempt in range(64):
            events = self.store.read_all()
            current = fold(events)
            if current.injects_done > request_idx:
                return True
            if (
                current.injects_done != request_idx
                or len(current.inject_requests) <= request_idx
            ):
                return False
            tail = events[-1].seq if events else -1
            try:
                self.store.append_many([
                    (EV_INJECT_FAILED, {
                        "idx": request_idx,
                        "error": str(error)[:500],
                        "reason": reason,
                    }),
                    (EV_INJECT_DONE, {
                        "idx": request_idx,
                        "skipped": reason,
                    }),
                ], expected_last_seq=tail)
                return True
            except EventStoreConcurrencyError:
                continue
        return False

    def _close_node_creating_forced_request_before_terminal_gate(
        self,
        state: RunState,
        *,
        reason: str,
    ) -> bool:
        """Durably skip one forced Node creator before a stronger terminal budget wins.

        Wall/eval ceilings intentionally outrank operator work, but finalizing without a matching
        acknowledgement leaves a replay-visible queue head stranded in a finished run. Close one head
        per turn, then re-fold before the terminal CAS. A node-budget-only wait never calls this helper
        and therefore remains resumable via ``budget_extend{add_nodes}``.
        """

        if len(state.fork_requests) > state.forks_done:
            request = state.fork_requests[state.forks_done]
            self.store.append(EV_FORK_DONE, {
                "from_node_id": request.get("from_node_id"),
                "generation": request.get("generation"),
                "skipped": reason,
            })
            return True
        if len(state.inject_requests) > state.injects_done:
            self._append_inject_failure(
                state,
                error=f"not executed: terminal {reason} gate won",
                reason=reason,
            )
            # A lost tail CAS still means this head blocks finalization. Re-fold and retry next turn.
            return True
        forced_ablate = self._pending_forced_ablation(state)
        if forced_ablate is not None:
            self.store.append(EV_ABLATE, {
                "parent_id": forced_ablate["node_id"],
                "generation": forced_ablate["generation"],
                "impacts": {},
                "eval_seconds": 0.0,
                "skipped": reason,
            })
            return True
        return False

    async def _serve_forced_requests(self, state: RunState) -> bool:
        # Operator-forced steering (Phase 5), one per iteration then re-fold. Each is gated on
        # the domain event it produces (fork_done / an ablate event / node_confirmed), so a
        # resume never repeats it — deterministic under replay. Returns True when a request was
        # served OR deliberately left pending for node budget (the caller re-folds via `continue`);
        # False lets the loop fall through. The pending branch performs its own bounded wait.
        if len(state.fork_requests) > state.forks_done:
            req = state.fork_requests[state.forks_done]
            pid = req.get("from_node_id")
            generation = req.get("generation")
            current = state.nodes.get(pid)
            # Unstamped queued-before-create requests are historical and bind when their node appears.
            # Every modern producer stamps, so explicit generations remain strict CAS.
            served = (current is not None and not current.tombstoned
                      and pid not in state.aborted_nodes
                      and (generation is None or current.attempt == generation))
            if served:
                # A valid fork remains the durable queue head while the physical Node ceiling is full.
                # Do not append fork_done: a later budget_extend must be able to serve this same intent.
                if await self._defer_for_node_budget(state):
                    return True
                generation = current.attempt
                self._create_node({"kind": "improve", "parent_id": pid,
                                   "parent_generations": {str(pid): generation}})
            self.store.append(EV_FORK_DONE, {
                "from_node_id": pid, "generation": generation,
                **({} if served else {"skipped": "stale_generation"})})  # always advance the gate
            return True
        # Operator-authored experiment (manual tree edit): the human hand-adds a node (an idea
        # + optional parent + optional ready-made code). Materialize it into a real pending node;
        # the policy then evaluates it next (pending nodes are scheduled first). Gated on
        # `inject_done` so a resume never re-creates it — deterministic under replay.
        if len(state.inject_requests) > state.injects_done:
            req = state.inject_requests[state.injects_done]
            # Reject a structurally impossible durable row before waiting for Node capacity. The
            # validator is pure/bounded and mirrors materialization; no Developer/LLM work occurs.
            try:
                self._prepare_injected_node(state, req)
            except Exception as exc:  # noqa: BLE001 - legacy/hand-authored event rows are untrusted
                self._append_inject_failure(
                    state,
                    error=str(exc),
                    reason="invalid_request",
                )
                return True
            # Unlike malformed input, temporary budget exhaustion is not a failed inject. Leave the
            # request unacknowledged so an additive budget extension can admit it exactly once.
            if await self._defer_for_node_budget(state):
                return True
            try:
                self._create_injected_node(req)
            except Exception as e:  # noqa: BLE001 - a malformed operator/API inject must not
                # crash-loop the engine: without advancing the gate, every resume replays the same
                # bad request and dies again, leaving the run unrecoverable. Record + skip it.
                # The request is about to be marked done in this SAME invocation, so unlike an
                # escaping serial build exception there may be no resume boundary to clean a partial
                # reservation. Terminalize any surviving marker before advancing the request gate.
                failed_state = fold(self.store.read_all())
                if failed_state.buildings:
                    self._recover_interrupted_builds(failed_state)
                self._append_inject_failure(
                    state,
                    error=str(e),
                    reason="materialization_failed",
                )
                return True
            self.store.append(EV_INJECT_DONE, {"idx": state.injects_done})
            return True
        forced_ablate = self._pending_forced_ablation(state)
        if forced_ablate is not None:
            # Ablation probes culminate in one new refine_block Node. Avoid both the paid probes and a
            # false completion while that physical reservation has no budget slot.
            if await self._defer_for_node_budget(state):
                return True
            await self._ablate(forced_ablate["node_id"],
                               expected_generation=forced_ablate["generation"])
            return True
        forced_confirm = next((r for r in state.confirm_request_generations
                               if r.get("node_id") in state.nodes
                               and r.get("node_id") not in state.aborted_nodes
                               and not state.nodes[r["node_id"]].tombstoned
                               and state.nodes[r["node_id"]].attempt == r.get("generation")
                               and state.nodes[r["node_id"]].status is NodeStatus.evaluated
                               and r not in state.confirmed_forced_generations), None)
        if forced_confirm is None:
            legacy_confirm = next((nid for nid in state.confirm_requests
                                   if nid in state.nodes
                                   and nid not in state.aborted_nodes
                                   and not state.nodes[nid].tombstoned
                                   and state.nodes[nid].status is NodeStatus.evaluated
                                   and nid not in state.confirmed_forced), None)
            if legacy_confirm is not None:
                forced_confirm = {"node_id": legacy_confirm,
                                  "generation": state.nodes[legacy_confirm].attempt}
        if forced_confirm is not None:
            await self._confirm_node(state.nodes[forced_confirm["node_id"]])
            return True
        return False

    def _run_cadences(self, state: RunState) -> RunState:
        # Breadth read-model: record the run's narrowing curve at the strategist cadence BEFORE the
        # Strategist decides, so the same snapshot both (a) feeds the meta-controller's decision
        # context and (b) lands in the log for the UI / historical-replay measurement. It never
        # re-ranks the current champion directly, but it can change later policy/proposal cues;
        # replay-safe (at_node gate), no-op when coverage_context is off. See search/coverage.py.
        state = self._maybe_snapshot_coverage(state)

        # PART IV Phase 2a: concept-graph coverage + uncovered-region snapshot (the "0 coverage in {X}"
        # pivot signal). Deterministic, replay-safe (at_node gate); no-op when concept_pivot is off or
        # the task has no curated concept skeleton. Feeds the explore-stance novelty hint below.
        state = self._maybe_snapshot_concept_coverage(state)

        # PART V (B): seed the RUN BASE concept set from the first evaluated node's authored concepts, once.
        # Idempotent (fires only while run_base_concepts is empty), replay-safe. Turns on per-node DELTA
        # authoring downstream (proposal_cues injects the base + a "author concepts_added/removed" directive).
        state = self._maybe_seed_run_base_concepts(state)

        # R1-c: calibrated §12-verifier metric-tie-break. When select_verifier is on and eligible nodes
        # TIE on the ranked metric, verify the tied nodes (grounded on their realized result) so the
        # fold's final selector breaks the tie by soundness. Lazy (only real ties), replay-safe (persists one
        # verifier_group_scored event), advisory (never overrides a strictly-better metric). No-op when off.
        state = self._maybe_verify_ties(state)

        # A7 Strategist: adapt the search machinery (policy/operators/fidelity/Developer) before
        # the policy proposes the next actions. No-op when strategist is off (== today).
        state = self._maybe_consult_strategist(state)

        # Deep-Research stage (Phase 2): a "go think hard" step over all results + the web that
        # writes a memo to steer the next batch. Fires on a manual request, a cadence, or a
        # Strategist `request_research`. No-op when the stage is off. Replay-safe (gated).
        state = self._maybe_deep_research(state)

        # Run report (conclusion-first, agent-authored): regenerate on a node-count cadence so the
        # Report grows with the search. Audit-only sidecar; no-op when off. Replay-safe (gated on
        # the report's at_node). The deterministic report renders regardless.
        state = self._maybe_refresh_report(state)

        # Agentic hypothesis-board consolidation: the exact-hash ledger keeps paraphrases apart, so the
        # open board accumulates near-duplicate beliefs (deep-research directions + researcher + human
        # all phrasing the same idea). Hybrid-retrieve the near-dups + let the Researcher decide the
        # true merges, recorded as `hypothesis_merged` events the fold applies deterministically.
        state = self._maybe_merge_hypotheses(state)
        state = self._mirror_hypothesis_card_merges(state)

        # M6 comparative lessons, live-shared (doc 13 §7 items 2+5): on a node-count cadence,
        # distill credit-assigned PAIR lessons into the SHARED cross-run store DURING the run
        # (write side), and re-read the store so lessons distilled by CONCURRENT runs reach
        # this run's proposals (read side). The receipts do not re-rank current nodes, but they gate
        # paid cadence work and their shared-store output steers later proposals; replay-safe
        # (at_node gates), no-op when the cadences are 0.
        state = self._maybe_distill_lessons(state)
        state = self._maybe_refresh_lessons(state)

        # Reconciliation (memory ↔ corrected outcomes): when a node_reset re-eval FLIPS a node's
        # outcome (a false-failure re-scored to evaluated, a demoted champion), this run's DISTILLED
        # lessons grounded in that node go stale — fold-derived memory self-corrects but the LLM-written
        # lesson file does not. Retire + re-derive those lessons from the corrected state. Cheap
        # {node->sig}-hash gate: no-op unless a signature actually moved; LLM only on a genuine drift.
        state = self._maybe_reconcile_lessons(state)
        # Layer 1b: the producers above may run in background/read-only channels, while Card events are
        # main-task-only.  Materialize their opaque memo/lesson/claim refs now, with exact Card + node
        # lifecycle + proposal fences; no bodies or paths cross into the Card ledger.
        return self._sync_card_enrichments(state)

    def _skip_if_aborted(self, a: dict, cur: RunState) -> bool:
        # Both explicit stop affordances close not-yet-started work at zero cost. A mid-eval abort/drop
        # is handled by EvaluateMixin's watcher and records the time already spent.
        node_id = a["node_id"]
        n = cur.nodes.get(node_id)
        node_aborted = node_id in cur.aborted_nodes
        card_dropped = bool(
            n is not None and self._operator_card_dropped_for_node(cur, n))
        if node_aborted or card_dropped:
            if n is not None and n.status is NodeStatus.pending:
                reason = "aborted" if node_aborted else "card_dropped"
                error = "aborted by operator" if node_aborted else "Card dropped by operator"
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node_id, "generation": n.attempt,
                    "error": error, "reason": reason, "eval_seconds": 0.0})
            return True
        return False

    def _spawn_research(self, tg, state: RunState) -> None:
        """Overlap a DUE deep-research 'think' with the in-flight eval(s), INDEPENDENT of max_parallel.
        The memo is computed on a `state` snapshot in a worker thread, then RECORDED IMMEDIATELY when
        research finishes — NOT coupled to the eval completing — so its directions steer the very next
        proposal instead of landing ~an eval later. Recording from the research task is safe because
        `_record_deep_research` admits only `BACKGROUND_APPENDABLE` event types and
        `EventStore.append` serializes writers under an interprocess lock with collision-safe seq
        derivation. Those records never rewrite the current champion, but their hints/open hypotheses
        deliberately steer later proposals. No-op when concurrent_research is off.

        Two modes: the library default fires ONCE per window when a trigger is due (== today,
        byte-identical). With `concurrent_research_repeat` on, the overlapped think RE-RUNS on an
        adaptive time cadence for the whole eval window (`_research_overlap_loop`) so a multi-day
        training doesn't leave the reasoning agents idle after one memo — the caller cancels the
        loop when the evals join (see `_dispatch_evals`)."""
        if not self.concurrent_research:
            return
        # CODEX AGENT: repeat is a continuation of a research episode, not an independent timer.
        # Requiring a due cadence/strategist trigger here keeps ``deep_research_every=0`` truly
        # manual-only and prevents a long eval from silently starting paid research on its own.
        rtrig = self._due_research_trigger(state)
        if rtrig is None:
            return
        # Defensive getattr: some tests build a partial Engine (no __init__) — a missing knob means
        # the safe one-shot default (== today), exactly like the train-monitor gates.
        if getattr(self, "_concurrent_research_repeat", False):
            # Repeat mode: keep researching for the whole eval window. Pass the initially-due trigger
            # so the FIRST pass fires promptly (matching one-shot promptness). `_due_research_trigger`
            # already rejects a missing model, so an unavailable stage cannot spin stub memos either.
            tg.start_soon(self._research_overlap_loop, rtrig)
            return

        async def _bg(snap=state, trig=rtrig):
            # Best-effort: an error in the advisory research MUST NOT propagate — it shares the eval's
            # task group, so an uncaught raise here would CANCEL the in-flight eval. Swallow everything.
            try:
                memo = await anyio.to_thread.run_sync(
                    functools.partial(self._compute_deep_research, snap, trig, trace=False))
                if memo is not None:
                    await anyio.to_thread.run_sync(
                        functools.partial(self._record_deep_research, memo, trigger=trig, manual=False))
            except Exception:  # noqa: BLE001 — never let deep research disturb the eval
                pass
        tg.start_soon(_bg)

    def _research_repeat_cadence(self) -> float:
        """Base interval (seconds) between REPEATED concurrent-research passes. Research is expensive
        (multi-turn LLM + web/arXiv), so the config `concurrent_research_interval_s` is a FLOOR, not a
        ceiling: the effective pace is max(config, ~5% of the per-experiment time budget). A two-day
        eval is re-researched roughly hourly; a short eval's first tick outlasts it (so it fires once
        or not at all). Falls back to the config interval when no budget is known."""
        cfg = max(1.0, float(getattr(self, "_concurrent_research_interval_s", 1800.0) or 1800.0))
        budget = None
        fn = getattr(self, "_experiment_time_budget", None)
        if callable(fn):
            try:
                budget = fn()
            except Exception:  # noqa: BLE001 — cadence is advisory; a budget hiccup just uses the config
                budget = None
        if isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget > 0:
            derived = min(3600.0, max(300.0, float(budget) * 0.05))
            return max(cfg, derived)         # research is costly: never MORE often than the config floor
        return cfg

    async def _research_overlap_loop(self, initial_trigger: Optional[str] = None) -> None:
        """Repeated concurrent deep-research: keep the reasoning agents productive for the WHOLE eval
        window (a multi-day training must not idle them after a single memo). Re-runs the overlapped
        think on an adaptive cadence, records ONLY memos whose content is NEW (identical re-runs are
        skipped so the log/hypothesis board don't bloat), backs off geometrically as the analysis
        converges (capped so it always re-checks — new sibling-eval results or cross-run lessons can
        land mid-window), and stops calling the LLM past the per-window cap. Its allowlisted
        BACKGROUND_APPENDABLE records are order-tolerant and never rewrite the current champion, while
        their hints/open hypotheses deliberately steer later proposals and are reconstructed by replay.
        Runs in `_dispatch_evals`'s background task group; cancelled when the evals join."""
        from looplab.engine.research_cadence import research_memo_sig
        from looplab.engine.train_monitor import next_monitor_sleep
        base = self._research_repeat_cadence()
        # Fire promptly if research was already due at spawn (one-shot promptness); else wait a full
        # cadence before the first deepening pass, so a short eval that outlasts no tick never researches.
        next_sleep = 0.0 if initial_trigger else base
        last_sig: Optional[str] = None
        converged = 0
        calls = 0
        cap = self._concurrent_research_max_calls
        trig = initial_trigger or "repeat"
        while True:
            await anyio.sleep(next_sleep)    # only cancellation (evals joined) unwinds the loop from here
            try:
                # Re-fold each tick (invariant #4): pick up sibling evals that finished + fresh hints.
                # This snapshot read is pure and owns no paid/shared role, so cancellation may abandon
                # only this read without permitting a late event/cost or rebinding run-scoped tools.
                snap = await anyio.to_thread.run_sync(
                    lambda: fold(self.store.read_all()), abandon_on_cancel=True)
                # Overlap the hypothesis-board CONSOLIDATION too (Phase 2): repeated research keeps
                # ADDING near-duplicate directions as open hypotheses, so dedup/merge them on the same
                # loop instead of only between nodes. `_maybe_merge_hypotheses` self-gates (open board
                # >= 4 AND grown >= 2 since its last pass) so it no-ops until there is something to
                # merge. This overlap is allowed only for legacy Hypothesis/Policy selection;
                # hypothesis_merged changes native Card ownership/readiness and therefore runs only
                # later on Card mode's joined main-task cadence. NOT abandon_on_cancel — this is
                # REQUIRED for safety, not style:
                # an abandoned merge worker could append EV_HYPOTHESIS_MERGED (and set _last_hyp_merge_n)
                # AFTER _dispatch_evals returns, concurrently with the main task's serial merge, which
                # is exactly the race the "background joined before _run_cadences" argument rules out.
                # So eval-join WAITS for an in-flight consolidate — one hybrid-retrieval + one
                # merge-decision LLM call, bounded by the endpoint timeout (comparable to the record
                # thread, not shorter). The self-gate keeps this rare: a converged tick whose board did
                # not grow no-ops fast. Runs before the research cap so a capped-out window still keeps
                # the board tidy. No-op when off / no reflect client / board small.
                if (getattr(self, "_concurrent_consolidate", False)
                        and not getattr(self, "card_driven_selection", False)):
                    await anyio.to_thread.run_sync(
                        functools.partial(self._maybe_merge_hypotheses, snap))
                if cap > 0 and calls >= cap:
                    return                   # research LLM budget spent; the health monitor still runs
                # CODEX AGENT: DeepResearcher owns a paid client and mutable run-bound tools. Its worker
                # must be joined before the eval window closes; abandoning it permits post-finalization
                # usage events and lets the next research pass rebind the same tools under a live call.
                memo = await anyio.to_thread.run_sync(
                    functools.partial(self._compute_deep_research, snap, trig, trace=False),
                    abandon_on_cancel=False)
                calls += 1
                if memo is None:
                    next_sleep = base
                    continue
                sig = research_memo_sig(memo)
                if sig == last_sig:          # converged — same conclusions; don't re-record, just back off
                    converged += 1
                    # cap = max(base, 3600): the backoff must never drop BELOW the configured interval
                    # FLOOR. next_monitor_sleep returns min(cap, base·2^k); with the default cap=3600 a
                    # base>3600 (user set interval_s>1h) would be clamped to 3600 < base, re-calling the
                    # LLM MORE often than the floor when converged. Raising the cap to base keeps the
                    # floor honoured (for base>3600 the sleep just stays at base — still bounded by the cap).
                    next_sleep = next_monitor_sleep(base, status="healthy", healthy_streak=converged,
                                                    cap=max(base, 3600.0))
                    continue
                last_sig = sig
                converged = 0
                next_sleep = base
                # The RECORD thread is deliberately NOT abandon_on_cancel (unlike the reads above): it
                # WRITES the event log (and may run a verify LLM pass), so abandoning it could append
                # research_completed/hint/hypothesis_added AFTER _dispatch_evals returns — possibly past
                # finalize. Waiting for the append (bounded, far shorter than the compute path) is safer.
                await anyio.to_thread.run_sync(
                    functools.partial(self._record_deep_research, memo, trigger=trig, manual=False))
                trig = "repeat"              # subsequent passes are repeats, not the initial due trigger
            except anyio.get_cancelled_exc_class():
                raise                        # cooperative cancellation (evals joined) — must propagate
            except Exception:  # noqa: BLE001 — an advisory tick hiccup must not disturb the eval
                next_sleep = base
                continue

    async def _dispatch_evals(self, evals: list, state: RunState,
                              max_es: Optional[float]) -> None:
        # Single experiment at a time is the base mode: run evals sequentially and
        # deterministically. Concurrent fan-out (the task-group below) is a backlog
        # seam — opt in with max_parallel > 1. Deep research overlaps + records immediately
        # in BOTH modes (see _spawn_research), independent of max_parallel.
        #
        # Nested groups: the repeating research (`_spawn_research`, when
        # `concurrent_research_repeat` is on) lives in the OUTER `bg_tg` and never finishes on its
        # own; the evals run in / under it and, once they JOIN, the `finally` cancels `bg_tg` to stop
        # the loop. The one-shot path (repeat off, == today) is NOT cancelled — `bg_tg` waits for the
        # single memo to finish exactly as the pre-refactor single group did (byte-identical).
        async with anyio.create_task_group() as bg_tg:
            self._spawn_research(bg_tg, state)
            try:
                if self._eval_parallel <= 1:
                    limiter = anyio.CapacityLimiter(1)
                    for a in evals:
                        cur = fold(self.store.read_all())
                        if self._skip_if_aborted(a, cur):
                            continue
                        # Re-check the eval-compute budget BEFORE each eval (not just per loop
                        # iteration), so a multi-eval batch can't overshoot by a whole batch (#2/#25).
                        if (max_es is not None and cur.total_eval_seconds >= max_es):
                            break
                        node = cur.nodes.get(a["node_id"])
                        reservation = None
                        generation = None
                        skip_eval = False
                        if node is not None and hasattr(self, "_wait_reserve_node_resources"):
                            generation = node.attempt
                            while True:
                                # Resource waits must not pin a stale fold forever.  A GPU->CPU Card
                                # re-pin does not release a GPU (and therefore does not bump the pool
                                # epoch), so re-fold after every bounded condition tick and fence the
                                # exact lifecycle plus run-level operator gates before retrying.
                                # CODEX AGENT: with the cross-run host lease this wait is no longer
                                # bounded by a sibling eval in this process — it lasts as long as
                                # ANOTHER run holds the pool (hours of training), and every 0.5s tick
                                # re-folds the WHOLE log (the parallel branch folds twice per tick):
                                # an O(total-events) busy-poll, the same cost confirm F26 documents.
                                # The stated reason (a re-pin doesn't bump the pool epoch) doesn't
                                # need an unconditional fold — a re-pin always APPENDS, so gate the
                                # re-fold on the tail seq having changed, or lengthen the idle tick.
                                waiting = fold(self.store.read_all())
                                live = waiting.nodes.get(node.id)
                                if self._skip_if_aborted(a, waiting):
                                    skip_eval = True
                                    break
                                terminal = bool(
                                    getattr(waiting, "paused", False)
                                    or getattr(waiting, "finished", False)
                                    or getattr(waiting, "stop_requested", None)
                                )
                                lifecycle_current = bool(
                                    live is not None
                                    and live.attempt == generation
                                    and live.status is NodeStatus.pending
                                    and not live.tombstoned
                                    and live.id not in waiting.aborted_nodes
                                    and not terminal
                                    and not (max_es is not None
                                             and waiting.total_eval_seconds >= max_es)
                                )
                                if not lifecycle_current:
                                    if live is not None and live.id in waiting.aborted_nodes:
                                        self._skip_if_aborted(a, waiting)
                                    skip_eval = True
                                    break
                                cur, node = waiting, live
                                reservation = await self._wait_reserve_node_resources(
                                    node,
                                    resource_pin=self._card_resource_pin_for_node(cur, node),
                                    wait_once=True,
                                )
                                if reservation is None:
                                    continue
                                admitted = fold(self.store.read_all())
                                live = admitted.nodes.get(node.id)
                                if self._skip_if_aborted(a, admitted):
                                    self._release_gpus(reservation.get("gpu_ids"))
                                    reservation = None
                                    skip_eval = True
                                    break
                                terminal = bool(
                                    getattr(admitted, "paused", False)
                                    or getattr(admitted, "finished", False)
                                    or getattr(admitted, "stop_requested", None)
                                )
                                if (
                                    live is None
                                    or live.attempt != generation
                                    or live.status is not NodeStatus.pending
                                    or live.tombstoned
                                    or live.id in admitted.aborted_nodes
                                    or terminal
                                    or (max_es is not None
                                        and admitted.total_eval_seconds >= max_es)
                                ):
                                    self._release_gpus(reservation.get("gpu_ids"))
                                    reservation = None
                                    if live is not None and live.id in admitted.aborted_nodes:
                                        self._skip_if_aborted(a, admitted)
                                    skip_eval = True
                                    break
                                if not self._node_resource_reservation_is_current(
                                    admitted, live, reservation,
                                ):
                                    self._release_gpus(reservation.get("gpu_ids"))
                                    cur, node = admitted, live
                                    continue
                                node = live
                                self._register_eval_resource_reservation(
                                    node.id, generation, reservation)
                                break
                        if skip_eval:
                            continue
                        try:
                            await self._evaluate(a["node_id"], limiter, max_es)
                        finally:
                            if reservation is not None and generation is not None:
                                self._clear_eval_resource_reservation(a["node_id"], generation)
                                self._release_gpus(reservation.get("gpu_ids"))
                else:
                    # G3 distributed/parallel eval: CONTINUOUS dispatch. A pool of `max_parallel` slots
                    # is kept FULL — the instant any eval finishes and the dispatcher worker returns
                    # its lifecycle reservation to `_free_gpus`, the producer admits the NEXT
                    # queued eval into that slot. This closes the head-of-line gap the old
                    # `started >= max_parallel: break` left: that break capped the batch at max_parallel
                    # STARTED and deferred the rest to a FUTURE spine iteration, so a short eval that
                    # freed its GPU left it idle for the whole remaining life of a long sibling (the
                    # 10h-vs-1h case). The semaphore bounds concurrency to max_parallel AND refills a
                    # freed slot; each eval gets its own no-op CapacityLimiter(1) so `_evaluate`'s
                    # internal `async with limiter` is inert and the semaphore is the SOLE bound.
                    # fast_acquire: when a slot is already free the admit takes no checkpoint, so a batch
                    # that fits in the pool behaves like the old tight loop (all started before any child
                    # runs); the checkpoint only happens on the genuine refill wait.
                    #
                    # STILL A BARRIER: the inner task group joins the WHOLE batch before returning, so
                    # `bg_tg`'s lifecycle and every `pending_nodes()`-keyed guarantee are unchanged.
                    slots = anyio.Semaphore(self._eval_parallel, fast_acquire=True)

                    async def _eval_in_slot(nid: int, generation: Optional[int],
                                            reservation: Optional[dict]) -> None:
                        try:
                            # A private single-token limiter -> `_evaluate`'s `async with limiter` is a
                            # no-op; the outer semaphore is what bounds fan-out and drives the refill.
                            await self._evaluate(nid, anyio.CapacityLimiter(1), max_es)
                        finally:
                            if reservation is not None and generation is not None:
                                self._clear_eval_resource_reservation(nid, generation)
                                self._release_gpus(reservation.get("gpu_ids"))
                            slots.release()          # free the slot -> wakes the producer to admit next

                    async with anyio.create_task_group() as tg:
                        pending = list(evals)
                        while pending:
                            # Fresh fold PER ADMISSION (like the serial branch, unlike the old fold-once):
                            # continuous dispatch means earlier evals in THIS batch complete mid-loop, so
                            # the abort-skip and the eval-budget guard both act on LIVE state — strictly
                            # stricter than the dead fold-once check the old comment flagged.
                            cur = fold(self.store.read_all())
                            # Budget guard (parallel path): now that `cur` reflects mid-batch completions,
                            # this actually enforces the eval-second cap — admit no more once spent. The
                            # overshoot is bounded to the ~max_parallel evals already in flight.
                            if (max_es is not None and cur.total_eval_seconds >= max_es):
                                break
                            await slots.acquire()     # blocks only when the pool is full -> the refill point
                            # CODEX AGENT: the pre-check above may be minutes old after a genuine refill
                            # wait. Re-fold while owning the freed slot so a sibling that crossed the hard
                            # eval budget (or an operator abort) cannot be followed by one more admission.
                            cur = fold(self.store.read_all())
                            if max_es is not None and cur.total_eval_seconds >= max_es:
                                slots.release()
                                break
                            # Scan for the first candidate whose complete footprint fits *now*.  A
                            # GPU-heavy head may wait while an explicit CPU node (gpus=0) behind it
                            # starts; reservation and release both use the condition-protected pool.
                            epoch = (self._gpu_pool_epoch()
                                     if hasattr(self, "_gpu_pool_epoch") else 0)
                            chosen_index = None
                            chosen_node = None
                            chosen_reservation = None
                            kept = []
                            for a in pending:
                                if self._skip_if_aborted(a, cur):
                                    continue
                                kept.append(a)
                            pending = kept
                            for pos, a in enumerate(pending):
                                node = cur.nodes.get(a["node_id"])
                                if node is None or not hasattr(self, "_try_reserve_node_resources"):
                                    chosen_index = pos
                                    chosen_node = node
                                    break
                                candidate = self._try_reserve_node_resources(
                                    node,
                                    resource_pin=self._card_resource_pin_for_node(cur, node),
                                )
                                if candidate is not None:
                                    chosen_index = pos
                                    chosen_node = node
                                    chosen_reservation = candidate
                                    break
                            if chosen_index is None:
                                slots.release()
                                if not pending:
                                    break
                                # A release between the scan and this wait changes the epoch, so the
                                # condition returns immediately rather than losing the wake-up.
                                await anyio.to_thread.run_sync(
                                    self._wait_for_gpu_change, epoch, abandon_on_cancel=True)
                                continue
                            if chosen_node is not None and chosen_reservation is not None:
                                admitted = fold(self.store.read_all())
                                live = admitted.nodes.get(chosen_node.id)
                                if self._skip_if_aborted(pending[chosen_index], admitted):
                                    self._release_gpus(chosen_reservation.get("gpu_ids"))
                                    pending.pop(chosen_index)
                                    slots.release()
                                    continue
                                terminal_gate = bool(
                                    getattr(admitted, "paused", False)
                                    or getattr(admitted, "finished", False)
                                    or getattr(admitted, "stop_requested", None)
                                )
                                lifecycle_current = bool(
                                    live is not None
                                    and live.attempt == chosen_node.attempt
                                    and getattr(live, "status", NodeStatus.pending)
                                    is NodeStatus.pending
                                    and not getattr(live, "tombstoned", False)
                                    and live.id not in admitted.aborted_nodes
                                    and not terminal_gate
                                    and not (max_es is not None
                                             and admitted.total_eval_seconds >= max_es)
                                )
                                if (
                                    not lifecycle_current
                                    or not self._node_resource_reservation_is_current(
                                        admitted, live, chosen_reservation,
                                    )
                                ):
                                    self._release_gpus(chosen_reservation.get("gpu_ids"))
                                    if terminal_gate:
                                        # A pause/stop can land during the bounded resource wait.  The
                                        # reservation was formed from the old turn, so release it and end
                                        # admission instead of spinning or scheduling work past the gate.
                                        slots.release()
                                        break
                                    if not lifecycle_current:
                                        pending.pop(chosen_index)
                                    slots.release()
                                    continue
                                chosen_node = live
                            chosen = pending.pop(chosen_index)
                            generation = (chosen_node.attempt if chosen_node is not None else None)
                            if chosen_reservation is not None and generation is not None:
                                self._register_eval_resource_reservation(
                                    chosen["node_id"], generation, chosen_reservation)
                            try:
                                tg.start_soon(_eval_in_slot, chosen["node_id"], generation,
                                              chosen_reservation)
                            except BaseException:
                                if chosen_reservation is not None and generation is not None:
                                    self._clear_eval_resource_reservation(
                                        chosen["node_id"], generation)
                                    self._release_gpus(chosen_reservation.get("gpu_ids"))
                                slots.release()
                                raise
            finally:
                # Evals have joined (or errored out) — stop the repeating research loop. One-shot
                # research (repeat off) leaves `bg_tg` uncancelled so its single memo still records,
                # preserving the pre-refactor behaviour byte-for-byte. Defensive getattr: a partial
                # test Engine defaults to one-shot (no cancel), == today.
                if getattr(self, "_concurrent_research_repeat", False):
                    bg_tg.cancel_scope.cancel()

    # ------------------------------- strategist cadence (extracted to engine/strategy.py)
    # The A7 strategist-consultation + coverage-snapshot cluster (`_strategy_core`,
    # `_available_developers`, `_strategy_ctx`, `_coverage_for_ctx`, `_should_consult`,
    # `_record_strategy`, `_ensure_surrogate`, `_apply_strategy`, `_already_covered_at`,
    # `_maybe_snapshot_coverage`, `_maybe_consult_strategist`) lives in looplab/engine/strategy.py
    # (StrategyCadenceMixin — inherited, zero call-site churn). `_op_span` STAYS here: it is a
    # generic new-trace span helper shared by the research / hypothesis-merge / lessons clusters too.
    def _op_span(self, name: str, **attrs):
        """A named NEW-trace span for a sub-operation (strategist consult, hypothesis merge …) so the
        event appended inside it is auto-stamped with THIS op's trace_id (eventstore reads current_ids),
        letting the UI scope the event's trace to just that operation. Null-context when no tracer is
        wired (tests build Engine via __new__ and skip __init__) — the op still runs, just untraced."""
        import contextlib
        tr = getattr(self, "tracer", None)
        return tr.span(name, new_trace=True, **attrs) if tr is not None else contextlib.nullcontext()

    # ------------------------------ research cadence (extracted to engine/research_cadence.py)
    # The P2 deep-research + open-hypothesis-board merge + run-report cadence cluster
    # (`_maybe_deep_research`, `_already_researched_at`, `_run_deep_research`,
    # `_compute_deep_research`, `_record_deep_research`, `_due_research_trigger`,
    # `_maybe_merge_hypotheses`, `_maybe_refresh_report`, `_write_report`) lives in
    # looplab/engine/research_cadence.py (ResearchCadenceMixin — inherited, zero call-site churn).

    # ----------------------------------------------------------- proposal cues
    # `_set_complexity_hint` / `_stamp_novelty_hint` live in looplab/engine/proposal_cues.py
    # (ProposalCuesMixin — inherited, zero call-site churn; the hint-forwarding registry test
    # source-scans that module too).

    # ---------------------------- cross-run memory / lessons / reflection (extracted)
    # The lessons/reflection cluster lives in looplab/engine/lessons.py (`LessonMemory`,
    # constructed as `self.lessons` in __init__). These thin delegators keep the ORIGINAL
    # method/attribute names on the Engine — tests call and monkeypatch e.g.
    # `engine._write_reflection_note` / `engine._reflect_client` / `engine._prior_note_text` —
    # and LessonMemory routes its internal cross-calls back through them, so an instance-level
    # monkeypatch intercepts every path.
    @property
    def _lessons_seen_stamp(self):
        return self.lessons.seen_stamp

    @_lessons_seen_stamp.setter
    def _lessons_seen_stamp(self, value) -> None:
        self.lessons.seen_stamp = value

    @property
    def _prior_note_text(self) -> str:
        return self.lessons.prior_note_text

    @_prior_note_text.setter
    def _prior_note_text(self, value: str) -> None:
        self.lessons.prior_note_text = value

    @property
    def _dev_prior_note_text(self) -> str:
        return self.lessons.dev_prior_note_text

    @_dev_prior_note_text.setter
    def _dev_prior_note_text(self, value: str) -> None:
        self.lessons.dev_prior_note_text = value

    def _load_reflection_priors(self, exclude_run_id: Optional[str] = None,
                                role: Optional[str] = None) -> str:
        return self.lessons.load_reflection_priors(exclude_run_id=exclude_run_id, role=role)

    def _load_reflection_priors_both(self, exclude_run_id: Optional[str] = None) -> tuple[str, str]:
        return self.lessons.load_reflection_priors_both(exclude_run_id=exclude_run_id)

    def _empty_state_for_fp(self) -> RunState:
        return self.lessons.empty_state_for_fp()

    def _task_fingerprint(self, final: RunState, best=None) -> list[str]:
        return self.lessons.task_fingerprint(final, best)

    @in_llm_lane("enrichment")
    def _write_reflection_note(self, final: RunState) -> None:
        return self.lessons.write_reflection_note(final)

    @in_llm_lane("enrichment")
    def _reflect_lessons(self, final: RunState, best, fp: list) -> list:
        return self.lessons.reflect_lessons(final, best, fp)

    def _append_lessons(self, lessons: list, *, hygiene: bool = True) -> None:
        return self.lessons.append_lessons(lessons, hygiene=hygiene)

    @in_llm_lane("enrichment")
    def _comparative_lessons(self, state: RunState, fp: list, exclude=()) -> tuple[list, list]:
        return self.lessons.comparative_lessons(state, fp, exclude=exclude)

    _spent_pairs = staticmethod(LessonMemory.spent_pairs)

    @in_llm_lane("enrichment")
    def _maybe_distill_lessons(self, state: RunState) -> RunState:
        # Own op-trace: LessonMemory writes lessons_distilled via the SAME store, so an append inside
        # this span is stamped with it (current_ids) → the UI scopes the event's trace to the distill.
        with self._op_span("lessons_distill"):
            return self.lessons.maybe_distill_lessons(state)

    def _lessons_store_stamp(self):
        return self.lessons.lessons_store_stamp()

    @in_llm_lane("enrichment")
    def _maybe_refresh_lessons(self, state: RunState) -> RunState:
        with self._op_span("lessons_refresh"):
            return self.lessons.maybe_refresh_lessons(state)

    @in_llm_lane("enrichment")
    def _maybe_reconcile_lessons(self, state: RunState) -> RunState:
        # Own op-trace: reconcile appends lessons_reconciled / lessons_distilled via the SAME store,
        # so those events are scoped to this span in the UI.
        with self._op_span("lessons_reconcile"):
            return self.lessons.reconcile_lessons(state)

    def _distill_skill_body(self, final: RunState, h, ev: list) -> str:
        return self.lessons.distill_skill_body(final, h, ev)

    def _reflect_client(self):
        return self.lessons.reflect_client()

    @in_llm_lane("enrichment")
    def _causal_meta_note(self, final: RunState, best) -> Optional[str]:
        return self.lessons.causal_meta_note(final, best)

    _consolidate_lessons_file = staticmethod(LessonMemory.consolidate_lessons_file)
    _compact_lessons = staticmethod(LessonMemory.compact_lessons)

    def _store_case(self, final: RunState) -> None:
        return self.lessons.store_case(final)

    def _store_concept_capsule(self, final: RunState) -> None:
        return self.lessons.store_concept_capsule(final)

    @in_llm_lane("enrichment")
    def _store_research_claims(self, final: RunState) -> None:
        return self.lessons.store_research_claims(final)

    @in_llm_lane("enrichment")
    def _store_concept_curation(self, final: RunState) -> None:
        return self.lessons.store_concept_curation(final)

    @in_llm_lane("enrichment")
    def _store_claim_curation(self, final: RunState) -> None:
        return self.lessons.store_claim_curation(final)

    @in_llm_lane("enrichment")
    def _store_task_facets(self, final: RunState) -> None:
        return self.lessons.store_task_facets(final)

    @staticmethod
    def _cadence_due(n: int, last: int, every: int) -> bool:
        """The shared since-last node-count gate (report/distill/refresh cadences). Since-last
        (not `n % every == 0`): a failed/merge/ablate node-count jump must not step over the only
        multiple and silently skip the whole window."""
        return every > 0 and n > 0 and n - last >= every

    # -------------------------------------------------- novelty gate (extracted to engine/novelty.py)
    # The E1/T5 novelty/dedup gate cluster (`_idea_text`, `_idea_vec`, `_semantic_duplicate`,
    # `_llm_novelty_gate`, `_apply_novelty_gate`) lives in looplab/engine/novelty.py
    # (NoveltyGateMixin — inherited, zero call-site churn).

    # ------------------------------------------------------------- node creation
    # ---------------------------------------------------------- node building
    # `_ensemble_idea` / `_agent_next_actions` / `_implement` / `_directed_idea` / `_repair` /
    # `_emit_node_created` live in looplab/engine/node_build.py (NodeBuildMixin — inherited,
    # zero call-site churn). `_create_node` / `_rerun_node` / `_create_injected_node` stay HERE:
    # they call the module-global `fold` that two tests monkeypatch through this module.

    # ----------------------------------------------------------- crash & repair
    # `_triage_crash` / `_repair_error_context` / `_prepare_env` live in
    # looplab/engine/crash_repair.py (CrashRepairMixin — inherited, zero call-site churn).

    @staticmethod
    def _node_id_ceiling(events, state) -> int:
        """The next unique, monotonic node id = 1 + max of every id EVER reserved (a `node_building` event)
        OR created (`state.nodes`). A `node_building` folds to the transient single `st.building` marker,
        NOT `st.nodes`, so a plain `max(state.nodes)+1` would hand concurrent builds the same id. Every
        site that MINTS a node (draft build, ablation refine_block, forced inject) computes the id from
        this helper AND commits (node_building/node_created) under `_id_lock`, so parallel builds never
        collide. Replay-deterministic (ids follow the log's node_building order); a failed reservation
        leaves a harmless id gap."""
        _max_building = max((e.data.get("node_id", -1) for e in events
                             if e.type == EV_NODE_BUILDING and isinstance(e.data.get("node_id"), int)),
                            default=-1)
        return max(max(state.nodes, default=-1), _max_building) + 1

    @staticmethod
    def _canonical_card_id(value) -> Optional[str]:
        """Mirror replay's bounded Card-id canonicalization without copying hostile strings."""
        if not isinstance(value, str) or len(value) > 256:
            return None
        bounded = value.strip()
        return bounded if bounded and bounded.isprintable() else None

    @classmethod
    def _engine_card_number(cls, value) -> Optional[int]:
        """Return ``k`` only for the writer-owned canonical spelling ``card-{k}``."""
        card_id = cls._canonical_card_id(value)
        if card_id is None or value != card_id or not card_id.startswith("card-"):
            return None
        suffix = card_id[5:]
        if (not suffix or not suffix.isascii() or not suffix.isdecimal()
                or (len(suffix) > 1 and suffix.startswith("0"))):
            return None
        number = int(suffix)
        return number if card_id == f"card-{number}" else None

    @classmethod
    def _card_id_ceiling(cls, events) -> int:
        """Next monotonic ``card-{k}`` suffix from every raw card_added receipt in the log.

        Folded Cards are intentionally unsuitable for allocation: conflicts, merges and malformed
        registrations may suppress them. The append-only log remains the durable reservation ledger.
        Canonicalize whitespace exactly like replay before scanning, and reject oversized input before
        ``int`` so a corrupt 5,000-digit suffix cannot trip Python's conversion guard.
        """
        ceiling = 0
        for event in events:
            if event.type != EV_CARD_ADDED:
                continue
            raw = cls._canonical_card_id(event.data.get("id"))
            if raw is None or not raw.startswith("card-"):
                continue
            suffix = raw[5:]
            if not suffix or not suffix.isascii() or not suffix.isdecimal():
                continue
            ceiling = max(ceiling, int(suffix) + 1)
        if len(f"card-{ceiling}") > 256:
            raise RuntimeError("native card id space is exhausted")
        return ceiling

    @staticmethod
    def _card_statement(idea: Idea) -> Optional[str]:
        """Return one lossless bounded seed statement, or ``None`` when it cannot be owned safely.

        The node-side join uses ``Idea.hypothesis.strip()``. Silently collapsing whitespace, deleting
        controls, or truncating here creates two seed identities under one explicit card id and makes
        the fail-closed projection suppress the Card. Choose the first actually non-empty source and
        reject an unrepresentable identity instead.
        """
        hypothesis = idea.hypothesis.strip() if isinstance(idea.hypothesis, str) else ""
        rationale = idea.rationale.strip() if isinstance(idea.rationale, str) else ""
        statement = hypothesis or rationale or f"{idea.operator} experiment"
        if (not statement or len(statement) > 2_048 or not statement.isprintable()
                or statement != statement.strip()):
            return None
        return statement

    @staticmethod
    def _implementation_ref(*, code=None, files=None, deleted=None) -> Optional[str]:
        """Exact bounded digest of operator-supplied implementation material.

        Ordinary Researcher/Developer builds pass no material and return ``None``. Inject requests may
        carry ready code/files; folding two such requests merely because their Idea matches would lose
        executable work, so the crash-prefix matcher also binds this digest.
        """
        if code in (None, "") and not files and not deleted:
            return None
        if code is not None and not isinstance(code, str):
            raise ValueError("injected code must be text")
        if files is None:
            files = {}
        if (not isinstance(files, dict)
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for key, value in files.items())):
            raise ValueError("injected files must be a text mapping")
        if deleted is None:
            deleted = []
        if (not isinstance(deleted, list)
                or any(not isinstance(value, str) for value in deleted)):
            raise ValueError("injected deleted paths must be a text list")
        encoded = orjson.dumps(
            {"code": code or "", "files": files, "deleted": deleted},
            option=orjson.OPT_SORT_KEYS,
        )
        if len(encoded) > 16 * 1024 * 1024:
            raise ValueError("injected implementation identity is oversized")
        return "implementation:v1:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _build_parent_snapshot(state: RunState, action: dict):
        """Validate and snapshot the exact parent generations named by one build action."""
        kind = action.get("kind")
        if not isinstance(kind, str) or not kind:
            return None
        raw_parents = action.get("parent_ids")
        if raw_parents:
            if not isinstance(raw_parents, list):
                return None
            parents = list(raw_parents)
        else:
            parents = [action["parent_id"]] if action.get("parent_id") is not None else []
        if (len(parents) > 64 or len(set(parents)) != len(parents)
                or any(type(pid) is not int or pid < 0 for pid in parents)):
            return None
        raw_expected = action.get("parent_generations")
        if raw_expected is not None and not isinstance(raw_expected, dict):
            return None
        parent_generations: dict[str, int] = {}
        for pid in parents:
            parent = state.nodes.get(pid)
            if parent is None or parent.tombstoned or pid in state.aborted_nodes:
                return None
            if raw_expected is not None:
                expected = raw_expected.get(str(pid), raw_expected.get(pid))
                if isinstance(expected, bool):
                    return None
                try:
                    expected = int(expected)
                except (TypeError, ValueError, OverflowError):
                    return None
                if expected != parent.attempt:
                    return None
            parent_generations[str(pid)] = parent.attempt
        if raw_expected is not None and len(raw_expected) != len(parent_generations):
            return None
        return kind, parents, parent_generations

    @staticmethod
    def _card_action(idea: Idea, parents: list[int], parent_generations: dict[str, int],
                     scored_against: Optional[int], scored_against_generation: Optional[int],
                     *, scored_against_empty: bool) -> dict:
        footprint = normalize_researcher_footprint(idea.footprint)
        return {
            "operator": idea.operator,
            "params": dict(idea.params or {}),
            "space": {key: list(values) for key, values in (idea.space or {}).items()},
            "eval_profile": idea.eval_profile,
            "eval_timeout": idea.eval_timeout,
            "parent_id": parents[0] if parents else None,
            "parent_ids": list(parents),
            "parent_generations": dict(parent_generations),
            "scored_against": scored_against,
            "scored_against_generation": scored_against_generation,
            "scored_against_empty": scored_against_empty,
            "footprint": footprint,
        }

    @staticmethod
    def _card_added_payload(card_id: str, statement: str, action: dict, idea: Idea, *,
                            source: str, at_node: int,
                            implementation_ref: Optional[str] = None,
                            steering_context=(), cross_run_receipt=None) -> dict:
        receipt = card_ownership_receipt(card_id, statement, action)
        proposal_ref = idea_proposal_ref(idea)
        bounded_steering = normalize_steering_context(steering_context)
        if (receipt is None or proposal_ref is None
                or bounded_steering is None
                or not isinstance(source, str) or not source or len(source) > 64
                or source != source.strip() or not source.isprintable()
                or type(at_node) is not int or not 0 <= at_node <= (1 << 31) - 1):
            raise ValueError("prepared idea cannot form a bounded native card receipt")
        if (implementation_ref is not None
                and (not isinstance(implementation_ref, str)
                     or not implementation_ref.startswith("implementation:v1:")
                     or len(implementation_ref) != len("implementation:v1:") + 64)):
            raise ValueError("invalid implementation identity")
        advisory_receipt = bounded_cross_run_advisory_receipt(cross_run_receipt)
        return {
            "id": card_id,
            "statement": statement,
            "source": source,
            "at_node": at_node,
            "rationale": (idea.rationale or "")[:400],
            # Deliberately narrow: replay treats any future executable member in this block as an
            # incomplete v1 action rather than silently blessing lossy semantics.
            # CODEX AGENT: the production writer also drops Idea.concepts, while novelty/card-enriched
            # signals gain a Card subject only after a Node exists. Such a Card is then work-owned and
            # no longer selection-ready, so real selectable Cards reach novelty/coverage scoring empty.
            # Persist bounded proposal-time scoring receipts, or remove these terms from live ranking.
            "idea": {
                "operator": action["operator"],
                "params": action["params"],
                "space": action["space"],
                "eval_profile": action["eval_profile"],
                "eval_timeout": action["eval_timeout"],
            },
            "parent_id": action["parent_id"],
            "parent_ids": action["parent_ids"],
            "parent_generations": action["parent_generations"],
            "scored_against": action["scored_against"],
            "scored_against_generation": action["scored_against_generation"],
            "scored_against_empty": action["scored_against_empty"],
            "footprint": action["footprint"],
            "steering_context": bounded_steering,
            "ownership_receipt": receipt,
            # Full normalized Idea identity is a separate crash-reuse/dedupe proof. The receipt-bound
            # Card action deliberately stays compact, but two repo rationales or implementation budgets must not
            # collapse merely because their params/profile happen to match.
            "proposal_ref": proposal_ref,
            **({"implementation_ref": implementation_ref} if implementation_ref else {}),
            **({"cross_run_receipt": advisory_receipt} if advisory_receipt else {}),
        }

    @classmethod
    def _card_event_matches(cls, data: dict, idea: Idea, action: dict, *, source: str,
                            at_node: int, implementation_ref: Optional[str],
                            steering_context=(), cross_run_receipt=None) -> bool:
        """True only for the exact writer shape used by a crash-prefix card reservation."""
        card_id = data.get("id")
        if cls._engine_card_number(card_id) is None:
            return False
        rebound = idea.model_copy(deep=True, update={"card_id": card_id})
        statement = cls._card_statement(rebound)
        if statement is None:
            return False
        expected = cls._card_added_payload(
            card_id, statement, action, rebound, source=source, at_node=at_node,
            implementation_ref=implementation_ref, steering_context=steering_context,
            cross_run_receipt=cross_run_receipt,
        )
        if data == expected:
            return True
        # ``at_node`` is allocation-time provenance, not executable proposal identity. A second
        # pre-reservation naturally sees a later node-id ceiling; everything else in the immutable
        # writer receipt must still match exactly so active work dedupes without weakening source,
        # action, steering, implementation or advisory identity.
        recorded_at_node = data.get("at_node")
        if (type(recorded_at_node) is not int
                or not 0 <= recorded_at_node <= (1 << 31) - 1):
            return False
        expected["at_node"] = recorded_at_node
        # This is intentionally a writer-prefix matcher, not a loose semantic comparison. A future
        # additive mint field must make an old writer decline reuse until that field is reviewed.
        return data == expected

    @staticmethod
    def _card_score_snapshot(state: RunState, requested: Optional[int]):
        score_id = state.best_node_id if requested is None else requested
        if score_id is None:
            return None, None, True
        if type(score_id) is not int or not 0 <= score_id <= (1 << 31) - 1:
            return None
        node = state.nodes.get(score_id)
        if node is None or node.tombstoned or score_id in state.aborted_nodes:
            return None
        return score_id, node.attempt, False

    @classmethod
    def _next_available_card_id(cls, events, state: RunState, excluded=()) -> str:
        """Allocate from the raw log ceiling, skipping only exact namespace collisions.

        Node-only/marker ids are not allocator authority (a stray ``card-99`` must not jump the
        ceiling to 100), but the exact next spelling cannot be reused without joining unrelated legacy
        evidence to a newly-native Card.
        """
        used = {
            card_id
            for event in events if event.type == EV_CARD_ADDED
            if (card_id := cls._canonical_card_id(event.data.get("id"))) is not None
        }
        used.update(
            card_id for node in state.nodes.values() if node.idea is not None
            if (card_id := cls._canonical_card_id(node.idea.card_id)) is not None
        )
        used.update(
            card_id for marker in state.buildings.values() if isinstance(marker, dict)
            if (card_id := cls._canonical_card_id(marker.get("card_id"))) is not None
        )
        used.update(card_id for card_id in state.cards
                    if cls._canonical_card_id(card_id) is not None)
        used.update(card_id for value in excluded
                    if (card_id := cls._canonical_card_id(value)) is not None)
        number = cls._card_id_ceiling(events)
        while f"card-{number}" in used:
            number += 1
            if len(f"card-{number}") > 256:
                raise RuntimeError("native card id space is exhausted")
        return f"card-{number}"

    @classmethod
    def _plan_native_card(cls, events, state: RunState, idea: Idea, *, parents: list[int],
                          parent_generations: dict[str, int], scored_against: Optional[int],
                          source: str, at_node: int,
                          implementation_ref: Optional[str] = None, excluded=(),
                          steering_context=(), cross_run_receipt=None,
                          superseded_card_id: Optional[str] = None) -> _CardReservationPlan:
        """Resolve exact live dedupe, crash-prefix reuse, or a fresh engine id without appending."""
        score_snapshot = cls._card_score_snapshot(state, scored_against)
        if score_snapshot is None:
            return _CardReservationPlan("invalid", None, None, None)
        score_id, score_generation, score_empty = score_snapshot
        statement = cls._card_statement(idea)
        if statement is None:
            return _CardReservationPlan("invalid", None, None, None)
        action = cls._card_action(
            idea, parents, parent_generations, score_id, score_generation,
            scored_against_empty=score_empty,
        )

        registrations: dict[str, int] = {}
        matches: list[str] = []
        for event in events:
            if event.type != EV_CARD_ADDED:
                continue
            cid = cls._canonical_card_id(event.data.get("id"))
            if cid is not None:
                registrations[cid] = registrations.get(cid, 0) + 1
            try:
                if cls._card_event_matches(
                        event.data, idea, action, source=source, at_node=at_node,
                        implementation_ref=implementation_ref,
                        steering_context=steering_context,
                        cross_run_receipt=cross_run_receipt):
                    matches.append(cid)
            except (TypeError, ValueError, OverflowError):
                continue

        reusable: list[str] = []
        unsafe_match = False
        merged_aliases = {
            alias
            for receipt in (getattr(state, "cards_merged", None) or [])
            if isinstance(receipt, dict)
            for raw_alias in (receipt.get("aliases") or [])
            if (alias := cls._canonical_card_id(raw_alias)) is not None
            and alias != cls._canonical_card_id(receipt.get("canonical"))
        }
        for cid in matches:
            if cid == superseded_card_id:
                continue
            if (cid is None or cls._engine_card_number(cid) is None
                    or registrations.get(cid) != 1 or cid in excluded):
                unsafe_match = True
                continue
            if cid in merged_aliases:
                # The immutable alias registration remains in the raw journal but its work item was
                # explicitly closed by consolidation. It is not reusable and must not permanently ban a
                # deliberate fresh retry of the same proposal under a new monotonic Card id.
                continue
            projected = state.cards.get(cid)
            if projected is None or projected.identity.kind != "native":
                unsafe_match = True
                continue
            owner_state = projected.selection_provenance.owner_state
            if owner_state in {"in_flight", "mixed", "unknown"}:
                return _CardReservationPlan("duplicate", None, None, None)
            if (projected.status == "dropped" or projected.merged_into
                    or "merged_work_items" in projected.selection_blockers
                    or projected.dropped_reason is not None or projected.evidence):
                # Closed/historical work is immutable but does not ban a deliberate future retry.
                continue
            reusable.append(cid)

        if unsafe_match or len(set(reusable)) > 1:
            return _CardReservationPlan("duplicate", None, None, None)
        card_id = reusable[0] if reusable else cls._next_available_card_id(
            events, state, excluded)
        reserved_idea = idea.model_copy(deep=True, update={"card_id": card_id})
        try:
            payload = cls._card_added_payload(
                card_id, statement, action, reserved_idea, source=source, at_node=at_node,
                implementation_ref=implementation_ref, steering_context=steering_context,
                cross_run_receipt=cross_run_receipt,
            )
        except (TypeError, ValueError, OverflowError):
            return _CardReservationPlan("invalid", None, None, None)
        return _CardReservationPlan(
            "reuse" if reusable else "mint", card_id, reserved_idea, payload)

    def _reserve_node_build(self, action: dict, idea: Optional[Idea] = None, *,
                            scored_against: Optional[int] = None,
                            source: str = "researcher",
                            implementation_ref: Optional[str] = None,
                            steering_context=(), cross_run_receipt=None):
        """Reserve one native Card and its node-building owner under one log-tail CAS.

        The final Idea must already exist: the immutable statement and exact action receipt cannot be
        minted honestly before proposal. A new ``card_added`` and its ``node_building{card_id}`` claim
        are one bounded EventStore batch, so another process can land before or after them, never between.
        Legacy orphan registrations remain reusable by an exact retry. ``idea`` remains optional only
        for historical internal callers/tests; production creation paths always supply it.
        """
        if idea is not None and not isinstance(idea, Idea):
            idea = Idea.model_validate(idea)
        with self._id_lock:
            proposal_authority_seq = None
            for _attempt in range(64):
                events = self.store.read_all()
                tail = events[-1].seq if events else -1
                authority_seq = self._proposal_authority_seq(events)
                if proposal_authority_seq is None:
                    proposal_authority_seq = authority_seq
                elif authority_seq != proposal_authority_seq:
                    # A control/research/lifecycle event won the CAS. The caller must return to the
                    # selection boundary; silently minting a replacement for a just-dropped orphan
                    # would defeat the operator's stop intent. LLM accounting alone may be retried.
                    return None
                state = fold(events)
                if state.paused or state.finished or state.stop_requested:
                    return None
                if self._node_reservation_slots_remaining(state, events=events) < 1:
                    return None
                parent_snapshot = self._build_parent_snapshot(state, action)
                if parent_snapshot is None:
                    return None
                kind, parents, parent_generations = parent_snapshot
                node_id = self._node_id_ceiling(events, state)
                if idea is None:
                    # Compatibility seam for callers that reserve only a node id. No production path
                    # uses this branch once writer-side Card minting is enabled.
                    try:
                        self.store.append(EV_NODE_BUILDING, {
                            "node_id": node_id, "operator": kind, "parent_ids": parents,
                        }, expected_last_seq=tail)
                    except EventStoreConcurrencyError:
                        continue
                    return _BuildReservation(
                        state, node_id, kind, parents, parent_generations, None, None)

                plan = self._plan_native_card(
                    events, state, idea, parents=parents,
                    parent_generations=parent_generations,
                    scored_against=scored_against, source=source, at_node=node_id,
                    implementation_ref=implementation_ref, steering_context=steering_context,
                    cross_run_receipt=cross_run_receipt,
                )
                if plan.disposition == "invalid":
                    self._append_proposal_event(EV_NOVELTY_REJECTED, {
                        "node_id": node_id, "generation": 0, "kind": "card_contract",
                        "reason": "proposal cannot form a bounded native Card action",
                        "action": "dropped",
                    })
                    return None
                if plan.disposition == "duplicate":
                    return None
                if plan.disposition not in {"mint", "reuse"} \
                        or plan.card_id is None or plan.idea is None:
                    return None
                # A proposal-bound sidecar may already name this Card. Main-task-only minting means
                # planner and commit must agree; never silently rebind its digest.
                if idea.card_id is not None and idea.card_id != plan.card_id:
                    return None
                card_id = plan.card_id
                reserved_idea = plan.idea
                claim = (EV_NODE_BUILDING, {
                    "node_id": node_id,
                    "operator": kind,
                    "parent_ids": parents,
                    "card_id": card_id,
                })
                try:
                    if plan.disposition == "mint":
                        self.store.append_many(
                            [(EV_CARD_ADDED, plan.payload), claim],
                            expected_last_seq=tail,
                        )
                    else:
                        self.store.append(*claim, expected_last_seq=tail)
                except EventStoreConcurrencyError:
                    continue
                return _BuildReservation(
                    state, node_id, kind, parents, parent_generations,
                    card_id, reserved_idea)
            return None

    @staticmethod
    def _proposal_cue_fence(state: RunState) -> bytes:
        """Bounded proposal authority that may move without changing the search epoch."""

        return orjson.dumps({
            "pending_hints": state.pending_hints,
            "research_count": len(state.research),
            "latest_research": state.research[-1] if state.research else None,
            "pending_strategy": state.pending_strategy,
            "active_strategy": state.active_strategy,
        }, option=orjson.OPT_SORT_KEYS)

    def _stage_prepared_card(self, action: dict, idea: Idea, *, proposal_state: RunState,
                             proposal_node_ceiling: int, at_node: int, source: str,
                             steering_context=(), cross_run_receipt=None,
                             proposal_cue_fence: Optional[bytes] = None,
                             proposal_authority_seq: Optional[int] = None) -> Optional[str]:
        """Commit one concrete proposal as a ready Card, without reserving a Node.

        Layer 5 needs durable inventory *before* it can elect a request-driven producer.  Proposal is
        slow and therefore happens outside ``_id_lock``; this short commit re-folds and accepts the
        result only while its epoch, parents, best anchor and future node-slot ceiling are unchanged.
        Serial callers may retry harmless tail churn; isolated RAW callers additionally fence every
        non-LLM-telemetry event. A lifecycle move returns to the outer loop so a proposal authored
        against an old search state can never be relabelled as current work.
        """
        if not isinstance(idea, Idea):
            try:
                idea = Idea.model_validate(idea)
            except Exception:
                return None
        if (type(proposal_node_ceiling) is not int or proposal_node_ceiling < 0
                or type(at_node) is not int or at_node < proposal_node_ceiling):
            return None
        if (proposal_authority_seq is not None
                and (type(proposal_authority_seq) is not int
                     or proposal_authority_seq < -1)):
            return None
        bounded_steering = normalize_steering_context(steering_context)
        if bounded_steering is None:
            return None
        expected_parent = self._build_parent_snapshot(proposal_state, action)
        expected_score = self._card_score_snapshot(
            proposal_state, proposal_state.best_node_id)
        expected_cues = (
            self._proposal_cue_fence(proposal_state)
            if proposal_cue_fence is None else proposal_cue_fence
        )
        if expected_parent is None or expected_score is None:
            return None

        # A Researcher declaration is persisted as the effective, schedulable request.  In particular,
        # an over-declared GPU count must not become an immutable receipt that Layer 4 later clamps to a
        # different action.  The writer owns card_id, so discard the provisional planner sidecar too.
        clean = idea.model_copy(deep=True, update={
            "card_id": None,
            "footprint": self._clamp_resource_footprint(idea.footprint),
        })
        for _attempt in range(64):
            # The log scan, fold, lifecycle fences and duplicate/id plan are intentionally outside
            # `_id_lock`: they scale with run history and may invoke bounded hashing/validation.  The
            # append's tail CAS is the authority for the snapshot.  If another reservation or control
            # wins after this plan, the CAS loses and the next turn recomputes every derived value.
            events = self.store.read_all()
            tail = events[-1].seq if events else -1
            # The isolated RAW worker is authorized by one exact semantic proposal prefix. LLM usage
            # telemetry is worker-owned and may advance the physical tail, but every other event is
            # authority-bearing. Serial outer batches omit this optional fence and retain CAS retries.
            if (proposal_authority_seq is not None
                    and self._proposal_authority_seq(events) != proposal_authority_seq):
                return None
            state = fold(events)
            if (state.search_epoch != proposal_state.search_epoch
                    or state.paused or state.finished or state.stop_requested
                    or state.best_node_id != proposal_state.best_node_id
                    or self._proposal_cue_fence(state) != expected_cues
                    or self._node_id_ceiling(events, state) != proposal_node_ceiling
                    or self._build_parent_snapshot(state, action) != expected_parent
                    or self._card_score_snapshot(
                        state, proposal_state.best_node_id) != expected_score):
                return None
            kind, parents, parent_generations = expected_parent
            del kind
            plan = self._plan_native_card(
                events,
                state,
                clean,
                parents=parents,
                parent_generations=parent_generations,
                scored_against=proposal_state.best_node_id,
                source=source,
                at_node=at_node,
                steering_context=bounded_steering,
                cross_run_receipt=cross_run_receipt,
            )
            if plan.disposition == "reuse":
                # Reuse mutates nothing.  Its eventual request/claim always re-folds and revalidates
                # the Card, so it needs no writer lock or synthetic event merely to stabilize the tail.
                return plan.card_id
            if plan.disposition != "mint" or plan.card_id is None or plan.payload is None:
                return None
            try:
                with self._id_lock:
                    self.store.append(
                        EV_CARD_ADDED, plan.payload, expected_last_seq=tail)
                return plan.card_id
            except EventStoreConcurrencyError:
                continue
        return None

    @in_llm_lane("build")
    def _stage_card_creates(self, actions: list[dict], state: RunState) -> list[str]:
        """Turn raw policy creates into durable, selection-ready Card receipts only.

        No ``node_building`` is written here.  A later fresh fold must select the Card, after which the
        isolated producer is gated by ``card_build_requested``.  Multi-seed drafts retain the existing
        shared-Researcher diversity pass; non-draft actions reuse the exact ordinary proposal helper.
        """
        raw = [dict(action) for action in actions
               if isinstance(action, dict) and META_CARD_ID not in action]
        if not raw:
            return []
        proposal_events = self.store.read_all()
        proposal_state = fold(proposal_events)
        proposal_node_ceiling = self._node_id_ceiling(proposal_events, proposal_state)
        prepared: list[tuple[dict, Idea, str, int, list, dict]] = []
        dropped_batch: list[dict] = []
        try:
            if len(raw) > 1 and all(action.get("kind") == "draft" for action in raw):
                ideas = self._propose_batch(proposal_state, len(raw))
                telemetry = list(
                    getattr(self, "_pending_batch_telemetry", None) or [])
                if len(telemetry) < len(ideas):
                    telemetry.extend([None] * (len(ideas) - len(telemetry)))
                dropped_batch = list(
                    getattr(self, "_pending_batch_dropped", None) or [])
                for offset, (action, idea, record) in enumerate(
                        zip(raw, ideas, telemetry)):
                    steering = ((record or {}).get("_steering_context", [])
                                if isinstance(record, dict) else [])
                    advisory_receipt = bounded_cross_run_advisory_receipt(
                        (record or {}).get("_cross_run_advisory_receipt", {})
                        if isinstance(record, dict) else {}
                    )
                    prepared.append((
                        action, idea, "researcher",
                        proposal_node_ceiling + offset, steering, advisory_receipt,
                    ))
            else:
                for offset, action in enumerate(raw):
                    source = "engine" if action.get("kind") == "merge" else "researcher"
                    idea = self._prepare_node_idea(
                        action,
                        proposal_state,
                        researcher=self.researcher,
                        prospective_node_id=proposal_node_ceiling + offset,
                        source=source,
                        proposal_events=proposal_events,
                    )
                    if idea is None:
                        continue
                    prepared.append((
                        action,
                        idea,
                        source,
                        proposal_node_ceiling + offset,
                        list(getattr(self.researcher, "_steering_context", []) or []),
                        bounded_cross_run_advisory_receipt(getattr(
                            self.researcher, "_cross_run_advisory_receipt", {}) or {}),
                    ))

            staged: list[str] = []
            for action, idea, source, at_node, steering, advisory_receipt in prepared:
                card_id = self._stage_prepared_card(
                    action,
                    idea,
                    proposal_state=proposal_state,
                    proposal_node_ceiling=proposal_node_ceiling,
                    at_node=at_node,
                    source=source,
                    steering_context=steering,
                    cross_run_receipt=advisory_receipt,
                )
                if card_id is not None:
                    staged.append(card_id)

            # Preserve the existing audit treatment for batch proposals rejected before Node ownership.
            # Accepted staged Cards land first, so rejected receipts allocate fresh ids after them.
            for dropped in dropped_batch:
                if isinstance(dropped, dict) and isinstance(dropped.get("idea"), Idea):
                    self._record_node_less_card(
                        dropped["idea"],
                        reason=str(dropped.get("reason") or "proposal_rejected")[:160],
                        steering_context=dropped.get("steering_context", []),
                    )
            return staged
        finally:
            self._pending_batch_dropped = []
            self._pending_batch_telemetry = []
            self._pending_batch_novelty_gated = []
            # Node-oriented telemetry cannot truthfully be emitted until a Node exists.  Clear the
            # primary pair so it cannot leak onto a later repair/legacy build; the staged Card already
            # owns its immutable proposal and steering receipts.
            self._discard_node_build_telemetry(
                researcher=self.researcher, developer=self.developer)

    @staticmethod
    def _card_claim_receipt_action(card) -> dict:
        """Rebuild the exact immutable action whose digest makes a native Card selectable."""
        return {
            "operator": card.operator,
            "params": dict(card.params or {}),
            "space": {key: list(values) for key, values in (card.space or {}).items()},
            "eval_profile": card.eval_profile,
            "eval_timeout": card.eval_timeout,
            "parent_id": card.parent_id,
            "parent_ids": list(card.parent_ids or []),
            "parent_generations": (
                dict(card.parent_generations) if isinstance(card.parent_generations, dict) else None
            ),
            "scored_against": card.scored_against,
            "scored_against_generation": card.scored_against_generation,
            "scored_against_empty": card.scored_against_empty,
            "footprint": normalize_researcher_footprint(card.footprint),
        }

    def _prepare_existing_card_claim(self, events, state: RunState, action: dict, card,
                                     node_id: int) -> Optional[_BuildReservation]:
        """Validate and reconstruct one Card claim against an already-fenced snapshot."""
        raw_card_id = action.get(META_CARD_ID)
        card_id = self._canonical_card_id(raw_card_id)
        if card_id is None or raw_card_id != card_id:
            return None
        if card.id != card_id or not card.selection_ready:
            return None

        expected_macro = projected_card_action(card)
        if expected_macro is None or expected_macro.get(META_CARD_ID) != card_id:
            return None
        if any(
                not isinstance(key, str)
                or (key not in expected_macro and not key.startswith("_"))
                for key in action):
            return None
        if any(action.get(key) != value for key, value in expected_macro.items()):
            return None

        # A modern selectable action always carries the complete generation fence, including an
        # explicit empty map for drafts. Rechecking it through the ordinary reservation validator
        # closes the score-to-claim race for resets, tombstones, aborts and parent replacement.
        if not isinstance(card.parent_generations, dict):
            return None
        claim_action = {**expected_macro, "parent_generations": card.parent_generations}
        parent_snapshot = self._build_parent_snapshot(state, claim_action)
        if parent_snapshot is None:
            return None
        kind, parents, parent_generations = parent_snapshot
        if parent_generations != card.parent_generations:
            return None

        receipt_action = self._card_claim_receipt_action(card)
        digest = card_action_digest(card.id, card.seed_statement, receipt_action)
        expected_receipt = card_ownership_receipt(card.id, card.seed_statement, receipt_action)
        if (digest is None or expected_receipt is None
                or digest != card.identity.action_digest):
            return None
        registrations = [
            event for event in events
            if event.type == EV_CARD_ADDED
            and self._canonical_card_id(event.data.get("id")) == card_id
        ]
        if (len(registrations) != 1
                or registrations[0].data.get("id") != card_id
                or registrations[0].data.get("statement") != card.seed_statement
                or registrations[0].data.get("ownership_receipt") != expected_receipt):
            return None

        try:
            calibration_concepts = ({
                "concept_mode": "full",
                "concepts": [
                    f"operator/{card.operator}",
                    "objective/quadratic",
                    "space/two-dimensional",
                ],
            } if self._speculation_gate_calibration else {})
            idea = Idea(
                operator=card.operator,
                params=dict(card.params or {}),
                space={key: list(values) for key, values in (card.space or {}).items()},
                rationale=card.rationale,
                eval_profile=card.eval_profile,
                eval_timeout=card.eval_timeout,
                hypothesis=card.seed_statement,
                card_id=card.id,
                footprint=normalize_researcher_footprint(card.footprint),
                **calibration_concepts,
            )
        except Exception:  # hostile/future Card data cannot escape the closed Idea schema
            return None
        rebuilt_action = self._card_action(
            idea, parents, parent_generations,
            card.scored_against, card.scored_against_generation,
            scored_against_empty=card.scored_against_empty,
        )
        if (self._card_statement(idea) != card.seed_statement
                or rebuilt_action != receipt_action):
            return None
        return _BuildReservation(
            state, node_id, kind, parents, parent_generations, card_id, idea)

    def _claim_existing_card_builds(
        self, actions: list[dict],
    ) -> Optional[list[_BuildReservation]]:
        """Atomically claim the complete Card lane selected from one fresh snapshot.

        A population policy may select several Cards at once. Claiming and building them one-by-one
        would make the first pending node engage the evaluate-all forced gate and invalidate its
        siblings. The whole lane is therefore revalidated under ``_id_lock`` and its ``node_building``
        owners are appended as one tail-CAS group before any slow Developer work begins.
        """
        if not actions:
            return []
        with self._id_lock:
            events = self.store.read_all()
            state = fold(events)
            self._refresh_speculation_budget(state, events=events)
            if self._node_reservation_slots_remaining(state, events=events) < len(actions):
                return None
            try:
                max_nodes = max(0, int(self.policy.max_nodes))
            except (TypeError, ValueError, OverflowError):
                return None
            remaining = max_nodes - card_budget_used(state)
            if remaining < len(actions):
                return None

            requested_ids: list[str] = []
            for action in actions:
                raw_card_id = action.get(META_CARD_ID)
                card_id = self._canonical_card_id(raw_card_id)
                if card_id is None or raw_card_id != card_id or card_id in requested_ids:
                    return None
                requested_ids.append(card_id)

            try:
                live = {card.id: card for card in eligible_cards(state, self.policy)}
                forced = forced_card_actions(state, self.policy, max_nodes)
                if forced is not None:
                    current_ids = [
                        candidate.get(META_CARD_ID) for candidate in forced
                        if isinstance(candidate, dict) and META_CARD_ID in candidate
                    ]
                else:
                    treatment = getattr(self, "_card_scoring", None)
                    current_ids = [
                        candidate.id for candidate in card_selection_set(
                            state, self.policy, max_nodes, scoring=treatment)
                    ]
            except Exception:  # policy/Card hooks must never weaken the ownership boundary
                return None
            if requested_ids != current_ids:
                return None

            first_node_id = self._node_id_ceiling(events, state)
            reservations: list[_BuildReservation] = []
            for offset, (action, card_id) in enumerate(zip(actions, requested_ids)):
                card = live.get(card_id)
                if card is None:
                    return None
                reservation = self._prepare_existing_card_claim(
                    events, state, action, card, first_node_id + offset)
                if reservation is None:
                    return None
                reservations.append(reservation)

            records = [
                (EV_NODE_BUILDING, {
                    "node_id": reservation.node_id,
                    "operator": reservation.kind,
                    "parent_ids": reservation.parent_ids,
                    "card_id": reservation.card_id,
                })
                for reservation in reservations
            ]
            try:
                self.store.append_many(
                    records, expected_last_seq=events[-1].seq if events else -1)
            except EventStoreConcurrencyError:
                return None
            return reservations

    def _claim_existing_card_build(self, action: dict):
        """Compatibility wrapper for callers that claim one selected Card."""
        reservations = self._claim_existing_card_builds([action])
        return reservations[0] if reservations else None

    def _drop_card_once(self, card_id: Optional[str], *, reason: str,
                        dropped_by: str = "engine") -> None:
        if not card_id:
            return
        # This helper is called both inside and outside `_id_lock`, so nesting that non-reentrant lock is
        # unsafe. Use the EventStore's atomic tail CAS instead: concurrent callers either observe the
        # first drop or lose the CAS and retry against its prefix.
        for _attempt in range(64):
            events = self.store.read_all()
            if any(
                    event.type in {EV_CARD_AUTO_DROPPED, EV_CARD_DROPPED}
                    and self._canonical_card_id(event.data.get("id")) == card_id
                    for event in events):
                return
            tail_seq = events[-1].seq if events else -1
            try:
                self.store.append(EV_CARD_AUTO_DROPPED, {
                    "id": card_id,
                    "reason": reason,
                    "dropped_by": dropped_by,
                }, expected_last_seq=tail_seq)
                return
            except EventStoreConcurrencyError:
                continue
        raise RuntimeError("could not append an idempotent card drop after concurrent log movement")

    def _record_node_less_card(self, idea: Idea, *, reason: str,
                               steering_context=(), source: str = "researcher") -> Optional[str]:
        """Mint and immediately close one rejected proposal with no Node owner.

        Unlike ordinary reservation this deliberately permits an exact live sibling: the point is to
        retain the discarded proposal and its reason without confusing it with the accepted work item.
        Accepted batch Cards are committed first, so this fresh id cannot invalidate their preplanned ids.
        """
        with self._id_lock:
            events = self.store.read_all()
            state = fold(events)
            clean = idea.model_copy(deep=True, update={"card_id": None})
            statement = self._card_statement(clean)
            score_snapshot = self._card_score_snapshot(state, state.best_node_id)
            bounded_steering = normalize_steering_context(steering_context)
            if statement is None or score_snapshot is None or bounded_steering is None:
                return None
            score_id, score_generation, score_empty = score_snapshot
            card_id = self._next_available_card_id(events, state)
            reserved = clean.model_copy(deep=True, update={"card_id": card_id})
            action = self._card_action(
                reserved, [], {}, score_id, score_generation,
                scored_against_empty=score_empty,
            )
            try:
                payload = self._card_added_payload(
                    card_id, statement, action, reserved, source=source,
                    at_node=self._node_id_ceiling(events, state),
                    steering_context=bounded_steering,
                )
            except (TypeError, ValueError, OverflowError):
                return None
            # This Card is rejected before it can ever own a Node. If the process dies after the first
            # append, an otherwise-valid receipt would resurrect it as a selectable proposal with no
            # recovery marker. Reserve the id with an intrinsically non-executable registration, then
            # append the normal terminal override. The full two-event prefix remains visible/auditable.
            payload.pop("ownership_receipt", None)
            self.store.append(EV_CARD_ADDED, payload)
            self.store.append(EV_CARD_AUTO_DROPPED, {
                "id": card_id, "reason": reason, "dropped_by": "engine",
            })
            return card_id

    def _mirror_hypothesis_card_merges(self, state: RunState) -> RunState:
        """Main-task durable Card receipts for background-safe Hypothesis consolidations.

        The LLM consolidation step may append ``hypothesis_merged`` from the research-overlap worker,
        while every Card lifecycle event is main-task-only. Reconcile by source event seq at the next
        decision boundary. Replay already understands statement-hash aliases, so this is additive audit
        durability and idempotent across resume; no model call or selection decision occurs here.
        """
        with self._id_lock:
            events = self.store.read_all()
            mirrored = {
                event.data.get("source_event_seq")
                for event in events if event.type == EV_CARD_MERGED
                if type(event.data.get("source_event_seq")) is int
            }
            wrote = False
            for event in events:
                if event.type != EV_HYPOTHESIS_MERGED or event.seq in mirrored:
                    continue
                canonical = self._canonical_card_id(event.data.get("canonical"))
                raw_aliases = event.data.get("aliases")
                if canonical is None or not isinstance(raw_aliases, list):
                    continue
                aliases: list[str] = []
                for raw_alias in raw_aliases[:256]:
                    alias = self._canonical_card_id(raw_alias)
                    if alias is not None and alias != canonical and alias not in aliases:
                        aliases.append(alias)
                if not aliases:
                    continue
                payload = {
                    "canonical": canonical,
                    "aliases": aliases,
                    "source_event_seq": event.seq,
                    "merged_by": "engine",
                }
                statement = event.data.get("statement")
                if (isinstance(statement, str) and statement.strip()
                        and len(statement.strip()) <= 2_048 and statement.strip().isprintable()):
                    payload["statement"] = statement.strip()
                self.store.append(EV_CARD_MERGED, payload)
                wrote = True
        return fold(self.store.read_all()) if wrote else state

    def _fail_reserved_build(self, *, node_id: int, card_id: Optional[str], generation: int,
                             reason: str, error: str, drop_card: bool = True) -> None:
        """Close a pre-node reservation and, when bare, its immutable Card work item.

        A terminal on a bare ``node_building`` clears the transient marker but creates no Node evidence.
        Without the paired card_auto_dropped receipt that Card would resurrect as a fresh proposed item after
        replay.  Existing-node reruns pass ``drop_card=False`` because they reuse the original lifecycle.
        """
        # Fail closed first. If the process dies between these two appends, the still-live build marker
        # makes recovery retry the terminal, while the Card is already non-selectable. Skip an existing
        # drop receipt so that prefix recovery remains idempotent.
        if card_id and drop_card:
            self._drop_card_once(card_id, reason=reason)
        payload = {
            "node_id": node_id,
            "generation": generation,
            "error": error,
            "reason": reason,
            "eval_seconds": 0.0,
        }
        if card_id:
            payload["card_id"] = card_id
        self.store.append(EV_NODE_FAILED, payload)

    def _resolve_llm_parallel(self, value: int) -> int:
        """Resolve startup ``llm_parallel`` to a concrete build fan-out. ``0`` = AUTO = the (already
        resolved) ``self._eval_parallel``, so we build exactly as many seeds as we can concurrently evaluate;
        any other value is used as-is (clamped to >=1). The build pool still clamps to 1 downstream
        (`_build_role_pairs`) when no `role_factory` is wired. Live strategy/control updates use 0=1
        because they settle immediately rather than retaining an AUTO mode."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return 1
        # Clamp to the config `le=64` ceiling on BOTH branches: AUTO resolves to max_parallel (config
        # `le=1024`), which must not silently exceed the parallel_build cap the config author set (nor
        # eagerly instantiate >64 wired role pairs); the operator budget-override path is otherwise
        # unvalidated. The explicit Settings/Strategist paths are already bounded 0..64.
        resolved = self._eval_parallel if value == 0 else value
        return min(64, max(1, resolved))

    def _reconfigure_llm_broker(self, value) -> None:
        """Apply one live canonical total without replacing a broker held by active borrowers."""
        if isinstance(value, bool):
            return
        if isinstance(value, float) and (
                not math.isfinite(value) or not value.is_integer()):
            return
        try:
            # Live Strategist/operator zero is a finite safety floor (1), not startup AUTO. This
            # matches the canonical runtime contract and avoids surprising GPU-count re-resolution.
            raw_total = int(value)
        except (TypeError, ValueError, OverflowError):
            return
        # CODEX AGENT: this method is also a defensive resume boundary for manually-constructed or
        # forward-version state. Never turn an invalid/huge value into a different valid paid-call cap.
        if not 0 <= raw_total <= 64:
            return
        total = max(1, raw_total)
        broker = getattr(self, "_llm_broker", None)
        if broker is None:
            self._llm_broker = LLMConcurrencyBroker(
                total=total, lane_limits=default_llm_lane_limits(total))
            return
        snapshot = broker.snapshot()
        current_lanes = snapshot["lane_limits"]
        # A total-only live delta must not erase a prior Strategist/operator lane allocation (and a
        # persistent budget override is re-applied every loop). Recompute the work-conserving defaults
        # only until a validated Strategy has explicitly owned the split; otherwise retain that split.
        next_lanes = (current_lanes if getattr(self, "_llm_lane_limits_explicit", False)
                      else default_llm_lane_limits(total))
        broker.reconfigure(total=total, lane_limits=next_lanes)

    def _build_role_pairs(self, n: int) -> list:
        """Up to `n` (researcher, developer) pairs for a parallel build batch: the primary (self's roles)
        plus fresh WIRED pairs from `role_factory`, cached in `self._role_pool` and reused across batches
        (each pair's per-build state — developer.last_files, researcher hints — is captured at node_created
        before the next batch reuses it, so reuse is safe). `role_factory` None or `n<=1` -> just the
        primary pair, and the caller stays serial. Fresh pairs are what isolate per-build role state so
        concurrent drafts don't clobber each other."""
        if n <= 1 or self.role_factory is None:
            return [(self.researcher, self.developer)]
        if self._role_pool is None:
            self._role_pool = []
        while len(self._role_pool) < n - 1:
            try:
                pair = self.role_factory()
            except Exception:  # noqa: BLE001 — a factory failure just caps fan-out, never crashes the run
                break
            if not (isinstance(pair, tuple) and len(pair) == 2):
                break
            if self._pool_developer_override is not None and self.developer_factory is not None:
                try:
                    pair = (pair[0], self.developer_factory(self._pool_developer_override))
                except Exception:  # noqa: BLE001 - cap fan-out if the selected backend cannot be built
                    break
            self._role_pool.append(pair)
        # CODEX AGENT: workers are constructed lazily, after Engine.__init__ bound the primary role
        # graph. Attach every newly reachable accountant before the first concurrent paid request.
        bind_cost_accountants(self)
        return [(self.researcher, self.developer)] + self._role_pool[: n - 1]

    def _prepare_node_idea(self, action: dict, state: RunState, *, researcher,
                           prospective_node_id: int, source: str,
                           proposal_events=None, preproposed=None) -> Optional[Idea]:
        """Finish the concrete Idea before Card/node reservation, without implementing code.

        A native ownership receipt binds the final operator/params/space/profile/footprint, so the
        old reserve-before-propose ordering cannot produce an honest Card.  This helper is the moved
        proposal half of ``_create_node``; every Developer call remains after durable reservation.
        """
        kind = action["kind"]
        events = list(proposal_events) if proposal_events is not None else self.store.read_all()
        try:
            setattr(researcher, "_steering_context", [])
        except Exception:  # noqa: BLE001 - wrappers may expose a read-only compatibility surface
            pass
        parent_snapshot = self._build_parent_snapshot(state, action)
        if parent_snapshot is None:
            return None
        _kind, parents, parent_generations = parent_snapshot

        def _link(candidate) -> Optional[Idea]:
            if candidate is None:
                return None
            linked = (candidate if isinstance(candidate, Idea)
                      else Idea.model_validate(candidate)).model_copy(deep=True)
            linked.card_id = None  # a Researcher/plugin can never claim writer namespace authority
            if self._speculation_gate_calibration:
                # Mechanical merge/debug Ideas do not pass through ToyResearcher, but they are still
                # members of the calibrated workload. Keep every physical node inside the same
                # one-GPU resource/provenance envelope without changing ordinary Idea bytes.
                linked.footprint = {"gpus": 1}
                linked.concept_mode = "full"
                linked.concepts = [
                    f"operator/{_kind}", "objective/quadratic", "space/two-dimensional"]
                linked.concepts_added = []
                linked.concepts_removed = []
            # Bind the Card and the durable Node to the action that execution will actually honor.
            # Keeping the model-requested value here would make a 3600s request with a 90s ceiling
            # appear as 3600s in both receipts even though eval_dispatch runs only 90s.
            linked.eval_timeout = self._effective_researcher_eval_timeout(linked)
            steering_context = normalize_steering_context(
                getattr(researcher, "_steering_context", []))
            if steering_context is None:
                return None
            plan = self._plan_native_card(
                events, state, linked, parents=parents, parent_generations=parent_generations,
                scored_against=state.best_node_id, source=source, at_node=prospective_node_id,
                steering_context=steering_context,
            )
            if plan.disposition == "invalid":
                self._append_proposal_event(EV_NOVELTY_REJECTED, {
                    "node_id": prospective_node_id, "generation": 0,
                    "kind": "card_contract",
                    "reason": "proposal cannot form a bounded native Card action",
                    "action": "dropped",
                })
            return plan.idea if plan.disposition in {"mint", "reuse"} else None

        if preproposed is not None:
            already_gated = False
            pending_batch = getattr(self, "_pending_batch_novelty_gated", None)
            if isinstance(pending_batch, list):
                for index, batch_idea in enumerate(pending_batch):
                    if preproposed is batch_idea:
                        # Consume the capability exactly once.  Equality is intentionally insufficient:
                        # a direct plugin/caller proposal that happens to match a batch result has not
                        # itself crossed the proposal-bound gate.
                        del pending_batch[index]
                        already_gated = True
                        break
            candidate = (self._canonicalize_draft_idea(preproposed)
                         if kind == "draft" else preproposed)
            linked = _link(candidate)
            if linked is None or kind in {"merge", "debug"}:
                return linked
            if already_gated:
                return linked
            # Direct callers may supply a concrete proposal without a batch reservation. Resolve its
            # final writer-owned Card id first, then run the same proposal-bound novelty sidecar as the
            # ordinary draft/improve path. Reserved parallel batches bypass this helper entirely: their
            # shared proposal pass has already applied the gate.
            final = self._apply_novelty_gate(
                state, linked, researcher=researcher,
                prospective_node_id=prospective_node_id,
            )
            return _link(final)

        if kind == "draft":
            self._set_complexity_hint(state, None, researcher=researcher)
            with self.tracer.span("propose"):
                idea = _link(self._canonicalize_draft_idea(researcher.propose(state, None)))
            if idea is None:
                return None
            final = self._apply_novelty_gate(
                state, idea,
                repropose=lambda: _link(self._canonicalize_draft_idea(
                    researcher.propose(state, None))),
                researcher=researcher, prospective_node_id=prospective_node_id)
            return _link(final)

        if kind == "merge":
            parents = list(action["parent_ids"])
            pnodes = [state.nodes[node_id] for node_id in parents]
            return _link(self._ensemble_idea(pnodes) if self._merge_mode == "ensemble"
                         else merge_idea(pnodes))

        parent = state.nodes[action["parent_id"]]
        if kind == "debug":
            repair = getattr(self.developer, "repair", None)
            if callable(repair) and parent.error and (parent.code or parent.files or self._repo_spec):
                idea = parent.idea.model_copy(deep=True)
                idea.operator = "debug"
                return _link(idea)
            self._set_complexity_hint(state, parent, researcher=researcher)
            # A repair proposal should not be pushed toward an unrelated direction.
            self._stamp_novelty_hint(state, "balanced", researcher=researcher)
            with self.tracer.span("propose"):
                idea = self._canonicalize_idea_operator(
                    researcher.propose(state, parent), "debug")
            return _link(idea)

        # improve / capability-expand
        self._set_complexity_hint(state, parent, researcher=researcher)
        authoritative_operator = "improve"
        if (getattr(self, "_capability_expansion", False)
                and getattr(self, "_novelty_stance", None) == "explore"):
            from looplab.engine.proposal_cues import _LOCK_IN_STREAK
            from looplab.search.lock_in import capability_expansion_due
            if capability_expansion_due(state, streak_threshold=_LOCK_IN_STREAK)[0]:
                authoritative_operator = KIND_EXPAND
        with self.tracer.span("propose"):
            idea = _link(self._canonicalize_idea_operator(
                researcher.propose(state, parent), authoritative_operator))
        if idea is None:
            return None
        final = self._apply_novelty_gate(
            state, idea,
            repropose=lambda p=parent: _link(self._canonicalize_idea_operator(
                researcher.propose(state, p), authoritative_operator)),
            researcher=researcher, prospective_node_id=prospective_node_id)
        return _link(final)

    @in_llm_lane("build")
    def _create_node(self, action: dict, roles=None, reserved=None, preproposed=None,
                     pretelemetry=None, precoded=None,
                     precoded_max_eval_seconds: Optional[float] = None) -> None:
        """Run proposal, reservation and implementation in one node-scoped handoff context."""
        from looplab.agents.agent import handoff_scope

        if reserved is not None:
            trace_node_id = reserved.node_id
        else:
            trace_events = self.store.read_all()
            trace_state = fold(trace_events)
            trace_node_id = self._node_id_ceiling(trace_events, trace_state)
        with self.tracer.span(
                "create_node", new_trace=True, node_id=trace_node_id,
                operator=action.get("kind")), handoff_scope(enabled=self._phase_handoff_summary):
            if precoded is not None:
                # Layer 5: the isolated producer already completed every slow role call.  Keep the
                # ordinary path below literally unchanged; this main-task branch only commits the
                # exact buffered result and its durable speculative marker.
                return self._create_precoded_node(
                    action,
                    reserved,
                    precoded,
                    max_eval_seconds=precoded_max_eval_seconds,
                )
            return self._create_node_scoped(
                action, roles, reserved, preproposed=preproposed,
                pretelemetry=pretelemetry)

    def _create_node_scoped(self, action: dict, roles=None, reserved=None, preproposed=None,
                            pretelemetry=None) -> None:
        # Variant-1 parallel build: `roles` is a per-build (researcher, developer) pair from the pool
        # (isolated per-build state so concurrent drafts don't clobber each other's hints/last_files);
        # `reserved` is a pre-reserved (state, id, kind, parents, parent_generations) tuple (the parallel
        # path reserves ids up front, serially, then fans out). `preproposed` (Phase 2) is a draft Idea
        # the shared researcher already proposed + novelty-gated in the batch pass (`_propose_batch`), so
        # the fan-out only IMPLEMENTS it. All default to the serial behaviour.
        researcher, developer = roles if roles is not None else (self.researcher, self.developer)
        if reserved is None:
            proposal_events = self.store.read_all()
            proposal_state = fold(proposal_events)
            if self._build_parent_snapshot(proposal_state, action) is None:
                return
            prospective_node_id = self._node_id_ceiling(proposal_events, proposal_state)
            source = "engine" if action.get("kind") == "merge" else "researcher"
            idea = self._prepare_node_idea(
                action, proposal_state, researcher=researcher,
                prospective_node_id=prospective_node_id,
                source=source, proposal_events=proposal_events, preproposed=preproposed)
            if idea is None:
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
            steering_context = normalize_steering_context(
                getattr(researcher, "_steering_context", []))
            if steering_context is None:
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
            reserved = self._reserve_node_build(
                action, idea, scored_against=proposal_state.best_node_id,
                source=source, steering_context=steering_context)
        if reserved is None:
            self._discard_node_build_telemetry(researcher=researcher, developer=developer)
            return
        state = reserved.state
        node_id = reserved.node_id
        kind = reserved.kind
        parent_generations = reserved.parent_generations
        idea = reserved.idea.model_copy(deep=True) if reserved.idea is not None else None
        if idea is None:
            # Legacy direct reservation: retain the historical behavior for internal callers, but do
            # not pretend it produced a native Card. Production paths always prepare before reserve.
            idea = self._prepare_node_idea(
                action, state, researcher=researcher,
                prospective_node_id=node_id,
                source="engine" if action.get("kind") == "merge" else "researcher",
                proposal_events=self.store.read_all(), preproposed=preproposed)
            if idea is None:
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
        # Phase-handoff ledger for THIS node build: propose → stages → plan → implement each distill
        # their transcript into a brief the next phase reads (see agents.agent.run_phase), so later
        # phases trust what earlier ones explored instead of re-reading the repo. Node-scoped (fresh
        # per build), and a no-op when the setting is off.
        with self.tracer.span("materialize_node", node_id=node_id, operator=kind):
            # node_building was appended inside _reserve_node_build (under _id_lock) — the id is committed
            # to the log atomically, so a PARALLEL build (parallel_build>1) can never pick the same id.
            # Restore THIS pre-proposed idea's own FOREAGENT telemetry after main-task reservation and
            # before the worker's audit emitters consume it.
            if pretelemetry:
                for _attr, _val in pretelemetry.items():
                    if _val is not None:
                        try:
                            setattr(researcher, _attr, _val)
                        except Exception:  # noqa: BLE001
                            pass
            # Per-call output: never let a reused wrapper/backend leak another node's resource
            # finalization into this build.  The exact pooled Developer is cleared and read below.
            self._reset_developer_footprint(developer)
            if kind == "draft":
                parents: list[int] = []        # not whatever label the LLM returns
                with self.tracer.span("implement"):
                    code = developer.implement(
                        self._directed_idea(idea.model_copy(deep=True), state))
            elif kind == "merge":
                parents = list(action["parent_ids"])
                # A0b: real ensembling (code recombination) when configured/Strategist-selected;
                # else the legacy mean-param merge. Toy/baseline developers degrade to mean.
                pnodes = [state.nodes[i] for i in parents]
                with self.tracer.span("implement"):
                    # A code-ensemble merge must SEED from the primary parent's solution (like improve),
                    # not implement() from scratch: from-scratch gave the Developer no base, so the
                    # ensemble node shipped without the agent-authored eval entrypoint and crash-failed
                    # ("can't open file test_looplab.py" — live node 63, 3 repairs couldn't recover). Now
                    # parent[0]'s working code + entrypoint carry over and the idea directs blending in
                    # the other parent. Mean-param merges (numeric tasks, no files) stay from-scratch.
                    _impl_from = getattr(developer, "implement_from", None)
                    _didea = self._directed_idea(
                        idea.model_copy(deep=True), state)   # §1: directives steer the merge code too
                    code = (_impl_from(_didea, pnodes[0])
                            if (self._merge_mode == "ensemble" and _impl_from and pnodes)
                            else developer.implement(_didea))
            elif kind == "debug":
                parent = state.nodes[action["parent_id"]]
                parents = [parent.id]
                repair = getattr(developer, "repair", None)
                # Error-feedback debug: hand the failure back to the Developer to fix. Fires for
                # whole-file solutions (parent.code), multi-file edits (parent.files), AND any
                # repo task (self._repo_spec) even when a prior attempt fell back to the empty
                # baseline — so an e2e agent can fix runtime errors / missing deps from the
                # error alone (it edits requirements and the eval's setup step re-installs them).
                if callable(repair) and parent.error and (parent.code or parent.files
                                                          or self._repo_spec):
                    # C3 deep test-driven repair (when enabled): failure taxonomy + a structured
                    # "reproduce then fix" directive, not just the raw stderr tail. Depth is already
                    # bounded by debug_depth.
                    err = self._repair_error_context(parent.error_reason, parent.error,
                                                     state=state, node=parent)
                    with self.tracer.span("repair", parent_id=parent.id):
                        code = self._repair(
                            parent, err, state, developer=developer)  # seed from parent's OWN files
                else:
                    # Signal-delivery (§1): the debug re-propose now gets the SAME cross-run priors +
                    # failure-reflection + fault-localization + trust cues as draft/improve — exactly
                    # when the agent is FIXING a failure it most needs "this crash class recurred
                    # before" and "the likely files to edit". Previously this branch called only
                    # _stamp_novelty_hint, so those cues were absent on the repair proposal.
                    with self.tracer.span("implement"):
                        code = self._implement(
                            self._directed_idea(idea.model_copy(deep=True), state), parent,
                            developer=developer)
            else:  # improve
                parent = state.nodes[action["parent_id"]]
                parents = [parent.id]
                with self.tracer.span("implement"):
                    code = self._implement(
                        self._directed_idea(idea.model_copy(deep=True), state), parent,
                        developer=developer)
            idea, footprint_finalized = self._finalize_developer_footprint(
                idea, developer, code)
            # 💡 deep-research provenance: tag the first couple of nodes created right after a research
            # memo (its directions are the active steering) so the UI can show WHERE research landed in
            # the tree. Audit/UI only — never affects search. Coarse-but-honest (temporal proximity).
            research_origin = None
            if state.research:
                _m = state.research[-1]
                _ra = _m.get("at_node")
                if _ra is not None and _ra <= node_id < _ra + 2:
                    from looplab.core.advisory_payloads import valid_advisory_ref
                    _memo_id = _m.get("memo_id")
                    research_origin = {
                        "at_node": _ra,
                        "trigger": _m.get("trigger"),
                        **({"memo_id": _memo_id}
                           if valid_advisory_ref(_memo_id, "memo") else {}),
                    }
            latest = fold(self.store.read_all())
            if any(pid not in latest.nodes
                   or latest.nodes[pid].attempt != generation
                   or latest.nodes[pid].tombstoned
                   or pid in latest.aborted_nodes
                   for pid, generation in ((int(pid), gen)
                                           for pid, gen in parent_generations.items())):
                # Clear both the transient node owner and its immutable, now-unbuildable Card.
                self._fail_reserved_build(
                    node_id=node_id, card_id=reserved.card_id, generation=0,
                    error="parent lifecycle changed while building", reason="superseded")
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
            materialize_abort = node_id in state.aborted_nodes
            self._emit_node_created(
                node_id=node_id,
                parent_ids=parents,
                operator=idea.operator,
                idea=durable_idea_payload(idea),
                code=code,
                files=getattr(developer, "last_files", {}) or {},         # per-build developer (pool-safe)
                deleted=getattr(developer, "last_deleted", []) or [],
                research_origin=research_origin,
                # Variant-1: read the receipt THIS build stamped on its own researcher (set under
                # `_advisory_lock` in `_set_complexity_hint`), so a concurrent sibling draft's advisory
                # write to `self._cross_run_advisory_receipt` can't mis-stamp this node. Falls back to
                # the shared attr only when a path never refreshed it (attr genuinely absent).
                cross_run_receipt=(_rcpt if (_rcpt := getattr(researcher, "_cross_run_advisory_receipt", None))
                                   is not None else getattr(self, "_cross_run_advisory_receipt", {})),
                **({"parent_generations": parent_generations} if parent_generations else {}),
                **({"footprint_finalized": True} if footprint_finalized else {}),
                # A legacy generation-less abort may intentionally reserve a not-yet-created slot.
                # Mark only an intent already present in the reservation snapshot. An abort that lands
                # after node_building is a losing-worker race and deliberately gets no escape hatch.
                **({"materialize_aborted_intent": True}
                   if materialize_abort else {}),
            )
            if node_id not in fold(self.store.read_all()).nodes:
                self._fail_reserved_build(
                    node_id=node_id, card_id=reserved.card_id, generation=0,
                    error="node creation was rejected during replay", reason="superseded")
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
            if materialize_abort:
                # Preserve the already-recorded operator intent as the first terminal for this newly
                # materialized lifecycle. This also keeps a Developer-error sentinel from stealing the
                # terminal with an unrelated crash/pause after the operator had already cancelled it.
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node_id, "generation": 0,
                    "error": "aborted by operator",
                    "reason": "aborted", "eval_seconds": 0.0,
                })
            # The Developer session CRASHED when its code is the "(developer error: …)" sentinel (an
            # exception in _run — e.g. an LLM 401/timeout). FAIL the node now: without this it stays
            # pending, and the eval runs the PARENT's carried-over entrypoint and inherits the PARENT's
            # metric — a false success that pollutes the search (the 401-window nodes 50-54 each faked
            # the parent's 0.81 this way). node_created → node_failed keeps the one-terminal invariant.
            elif isinstance(code, str) and code.startswith("(developer error:"):
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node_id, "generation": 0,
                    "error": code, "reason": "developer_crash",
                    "eval_seconds": 0.0})
                # Circuit-breaker — PAUSE on the FIRST developer_crash. A developer_crash means the
                # Developer couldn't finish THIS node even after the LLM client's own within-call retries
                # (429 / 5xx / throttle-403 all back off + retry): a problem that a NEW node can't fix
                # (LLM unreachable, or a hard error), NOT a bad experiment. One node = one experiment; if
                # it can't be resolved within the node, stop the whole run rather than rapid-fire more
                # dead nodes (the 403 blowout spun 67 of them). Freeze (not finish) so a plain `resume`
                # continues once the cause is resolved — no premature report/lessons.
                self.store.append(EV_PAUSE, {
                    "node_id": node_id, "generation": 0,
                    "reason": "auto-paused: a Developer session crashed (LLM unreachable or a hard error, "
                              "unresolved within the node) — resume once it's fixed"})
                self._create_paused = True   # tell the create-batch loop to STOP after this node
        # Variant-1: pass THIS build's pooled roles so concurrent draft builds don't cross-wire
        # each other's telemetry (last_report / last_hyp_priority / last_foresight). For serial
        # paths `researcher`/`developer` ARE `self.researcher`/`self.developer`, so byte-identical.
        self._emit_agent_report(node_id, developer=developer)
        self._emit_hypothesis_ranked(node_id, 0, researcher=researcher)
        self._emit_foresight_selected(node_id, 0, researcher=researcher, developer=developer)

    def _create_node_guarded(self, action: dict, roles=None, reserved=None, preproposed=None,
                             pretelemetry=None) -> None:
        """Variant-1 parallel build: run one pooled build, converting an UNEXPECTED exception into a
        `node_failed` terminal for its already-reserved id (its `node_building` was appended up front
        under `_id_lock`) instead of letting the exception propagate through the task group and tear
        down — and kill — the whole run. Keeps the one-terminal-per-node invariant (the reserved id
        gets exactly one terminal) and lets the rest of the concurrent batch finish. Used ONLY on the
        parallel path; the serial path keeps its historical crash-on-raise so bugs surface in tests."""
        try:
            self._create_node(action, roles, reserved, preproposed=preproposed,
                              pretelemetry=pretelemetry)
        except Exception as exc:  # noqa: BLE001 — one build's crash must not abort the concurrent batch
            node_id = reserved[1] if reserved else None
            if node_id is None:
                return
            latest = fold(self.store.read_all())
            node = latest.nodes.get(node_id)
            # Synthesise a terminal ONLY when this id has no node_created yet (a bare node_building whose
            # build raised before landing). A node that ALREADY has node_created carries real generated
            # code and is `pending` — the exception then came from the post-creation audit emitters
            # (audit-only); leave it for the evaluator. Marking a built, code-carrying node `failed` here
            # would silently discard a good build (review finding #2). If _create_node already wrote a
            # terminal (developer-crash sentinel, or node_evaluated), likewise nothing to do.
            if node is None:
                try:
                    self._fail_reserved_build(
                        node_id=node_id,
                        card_id=getattr(reserved, "card_id", None),
                        generation=0,
                        error=f"(build error: {exc})",
                        reason="build_crash",
                    )
                    # An EXCEPTION out of a build (not the graceful "(developer error: …)" sentinel) is a
                    # HARD fault — an LLM client that RAISES on a 401/outage, or a real bug in implement().
                    # The serial path crashes the run on such a raise; under concurrency we can't crash
                    # (it would kill sibling builds), so mirror the developer_crash circuit-breaker: PAUSE
                    # so the batch loop stops after this chunk instead of burning the node budget on
                    # repeated build_crash nodes (review finding #3). A plain resume continues once fixed.
                    self.store.append(EV_PAUSE, {
                        "node_id": node_id, "generation": 0,
                        "reason": "auto-paused: a node build raised (LLM unreachable or a hard error, "
                                  "unresolved within the build) — resume once it's fixed"})
                    self._create_paused = True
                except Exception:  # noqa: BLE001 — best-effort terminal; never re-raise into the group
                    pass

    @in_llm_lane("build")
    def _rerun_node(self, node: Node, state: RunState) -> None:
        """node_reset "propose"/"implement": re-run this EXISTING node id IN PLACE (never mints a new
        id — the whole point is to FIX a node, not proliferate). "implement" keeps the Researcher's idea
        (only the Developer re-runs — the "researcher ok, developer crashed" case); "propose" re-proposes
        a fresh idea too. Emits node_building + node_created for the SAME id — the fold applies it over the
        reset (clearing the rerun marker), the node goes pending-with-code, and the eval loop scores it
        next. Same developer-crash circuit-breaker as a first build. (An "eval" reset never reaches here —
        the fold left it pending-with-code and the eval dispatch re-scores it directly.)"""
        if (node.id in state.aborted_nodes or node.tombstoned
                or node.status is not NodeStatus.pending):
            return
        stage = node.rerun_from
        parents = list(node.parent_ids)
        parent = state.nodes.get(parents[0]) if parents else None
        generation = node.attempt
        parent_generations = {str(pid): state.nodes[pid].attempt for pid in parents
                              if pid in state.nodes}
        if len(parent_generations) != len(parents) or any(
                pid in state.aborted_nodes or state.nodes[pid].tombstoned for pid in parents):
            self.store.append(EV_NODE_FAILED, {
                "node_id": node.id, "generation": generation,
                "error": "parent is missing or aborted", "reason": "parent_unavailable",
                "eval_seconds": 0.0})
            return
        replacement_card = stage == "propose" and node.operator != "merge"
        with self.tracer.span("create_node", new_trace=True, node_id=node.id, operator=node.operator):
            if replacement_card:
                # Re-proposal changes immutable work-item meaning. Finish the Idea first, then replace
                # the old Card with one exact native receipt while keeping the operator-requested node id.
                self._set_complexity_hint(state, parent)
                with self.tracer.span("propose"):
                    proposed = self.researcher.propose(state, parent)
                idea = self._canonicalize_idea_operator(proposed, node.operator)
                if idea is None:
                    self._fail_reserved_build(
                        node_id=node.id, card_id=node.idea.card_id, generation=generation,
                        error="researcher returned no replacement proposal",
                        reason="proposal_rejected", drop_card=bool(node.idea.card_id))
                    return
                idea = idea.model_copy(deep=True, update={
                    "card_id": None,
                    # Re-proposal is the same Researcher-owned action boundary as a fresh proposal.
                    # Persist the governed value so rerun receipts cannot diverge from execution.
                    "eval_timeout": self._effective_researcher_eval_timeout(idea),
                })
                with self._id_lock:
                    events = self.store.read_all()
                    latest = fold(events)
                    current = latest.nodes.get(node.id)
                    parents_current = all(
                        pid in latest.nodes
                        and latest.nodes[pid].attempt == parent_generation
                        and pid not in latest.aborted_nodes
                        and not latest.nodes[pid].tombstoned
                        for pid, parent_generation in (
                            (int(pid), value) for pid, value in parent_generations.items()))
                    if (current is None or current.attempt != generation
                            or current.rerun_from != "propose" or current.tombstoned
                            or node.id in latest.aborted_nodes or not parents_current):
                        self._discard_node_build_telemetry()
                        return
                    plan = self._plan_native_card(
                        events, latest, idea, parents=parents,
                        parent_generations=parent_generations,
                        scored_against=latest.best_node_id, source="researcher", at_node=node.id,
                        steering_context=getattr(self.researcher, "_steering_context", []),
                        superseded_card_id=current.idea.card_id,
                    )
                    if plan.disposition not in {"mint", "reuse"}:
                        self._fail_reserved_build(
                            node_id=node.id, card_id=current.idea.card_id,
                            generation=generation,
                            error="replacement proposal was duplicate or outside the Card contract",
                            reason="proposal_rejected", drop_card=bool(current.idea.card_id))
                        self._discard_node_build_telemetry()
                        return
                    self._drop_card_once(current.idea.card_id, reason="reproposed")
                    if plan.disposition == "mint":
                        self.store.append(EV_CARD_ADDED, plan.payload)
                    self.store.append(EV_NODE_BUILDING, {
                        "node_id": node.id, "generation": generation,
                        "operator": node.operator, "parent_ids": parents,
                        "card_id": plan.card_id,
                    })
                    state = latest
                    idea = plan.idea
                    active_card_id = plan.card_id
            else:
                # An implement reset keeps immutable Idea/Card identity and only re-runs Developer.
                idea = node.idea.model_copy(deep=True)
                active_card_id = idea.card_id
                building_payload = {
                    "node_id": node.id, "generation": node.attempt,
                    "operator": node.operator, "parent_ids": parents,
                }
                if active_card_id:
                    building_payload["card_id"] = active_card_id
                self.store.append(EV_NODE_BUILDING, building_payload)
            self._reset_developer_footprint(self.developer)
            with self.tracer.span("implement"):
                # §1: a reset RE-BUILDS the node from scratch, so standing operator directives must
                # steer its code too — same as the four _create_node build sites.
                code = self._implement(
                    self._directed_idea(idea.model_copy(deep=True), state), parent)
            idea, footprint_finalized = self._finalize_developer_footprint(
                idea, self.developer, code)
            latest = fold(self.store.read_all())
            current = latest.nodes.get(node.id)
            parents_current = all(
                pid in latest.nodes and latest.nodes[pid].attempt == parent_generation
                and pid not in latest.aborted_nodes and not latest.nodes[pid].tombstoned
                for pid, parent_generation in ((int(pid), gen)
                                                for pid, gen in parent_generations.items()))
            if (current is None or current.attempt != generation
                    or current.tombstoned or node.id in latest.aborted_nodes or not parents_current):
                self._fail_reserved_build(
                    node_id=node.id, card_id=active_card_id, generation=generation,
                    error="node lifecycle changed while rebuilding", reason="superseded",
                    drop_card=replacement_card)
                self._discard_node_build_telemetry()   # serial single-node path: self.researcher/self.developer
                return
            self._emit_node_created(
                node_id=node.id, parent_ids=parents, operator=idea.operator,
                idea=durable_idea_payload(idea), code=code,
                files=getattr(self.developer, "last_files", {}) or {},
                deleted=getattr(self.developer, "last_deleted", []) or [],
                generation=generation,
                **({"parent_generations": parent_generations} if parent_generations else {}),
                **({"footprint_finalized": True} if footprint_finalized else {}))
            landed = fold(self.store.read_all()).nodes.get(node.id)
            if (landed is None or landed.attempt != generation or landed.rerun_from is not None
                    or landed.code != code):
                self._fail_reserved_build(
                    node_id=node.id, card_id=active_card_id, generation=generation,
                    error="rebuilt node creation was rejected during replay", reason="superseded",
                    drop_card=replacement_card)
                self._discard_node_build_telemetry()   # serial single-node path: self.researcher/self.developer
                return
            if isinstance(code, str) and code.startswith("(developer error:"):
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node.id, "generation": generation,
                    "error": code, "reason": "developer_crash", "eval_seconds": 0.0})
                self.store.append(EV_PAUSE, {
                    "node_id": node.id, "generation": generation,
                    "reason": "auto-paused: a Developer session crashed (LLM unreachable or a hard error, "
                              "unresolved within the node) — resume once it's fixed"})
        self._emit_agent_report(node.id, generation)
        # Consume the predictive telemetry for THIS node too: a "propose" reset re-runs the researcher
        # (setting last_hyp_priority/last_foresight), so without consuming it here the pick set would
        # leak onto the NEXT _create_node's id — the exact mis-attribution _emit_role_telemetry prevents.
        self._emit_hypothesis_ranked(node.id, generation)
        self._emit_foresight_selected(node.id, generation)

    def _prepare_injected_node(
        self,
        state: RunState,
        req: Mapping,
    ) -> _InjectedNodePlan:
        """Purely validate and normalize an inject request before any slot/LLM wait.

        Control/API writers already enforce a stricter schema. This boundary also handles legacy or
        hand-authored event rows and deliberately mirrors the tolerant materializer semantics; it has
        no provider, Developer, filesystem, or event-log side effect.
        """

        if not isinstance(req, Mapping):
            raise ValueError("injected request must be an object")
        idea_d = dict(req.get("idea") or {})
        idea_d.setdefault("operator", "manual")
        # Coerce params to floats defensively (a manual form may send strings); drop unparseable.
        raw_params = idea_d.get("params") or {}
        if not isinstance(raw_params, dict):
            raw_params = {}
        params: dict[str, float] = {}
        for key, value in raw_params.items():
            try:
                params[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        idea_d["params"] = params

        raw_parents = req.get("parent_ids")
        if isinstance(raw_parents, list):
            parents = [parent_id for parent_id in raw_parents if parent_id in state.nodes]
        else:
            parent_id = req.get("parent_id")
            parents = [parent_id] if parent_id is not None and parent_id in state.nodes else []
        unavailable = [
            parent_id for parent_id in parents
            if state.nodes[parent_id].tombstoned or parent_id in state.aborted_nodes
        ]
        if unavailable:
            raise ValueError(f"parent node(s) unavailable: {unavailable}")
        parent_generations = {
            str(parent_id): state.nodes[parent_id].attempt for parent_id in parents
        }
        expected_parent_generations = req.get("parent_generations")
        if expected_parent_generations is not None:
            if not isinstance(expected_parent_generations, dict):
                raise ValueError("parent_generations must be an object")
            if len(expected_parent_generations) != len(parent_generations):
                raise ValueError("parent generation snapshot does not match parents")
            for parent_id, generation in parent_generations.items():
                if expected_parent_generations.get(parent_id) != generation:
                    raise ValueError(f"stale parent generation for node #{parent_id}")

        code = req.get("code")
        # U3 real merge: this combines Idea metadata only. Developer work remains after reservation.
        if not code and idea_d.get("operator") == "merge" and len(parents) >= 2:
            parent_nodes = [state.nodes[parent_id] for parent_id in parents]
            idea = (self._ensemble_idea(parent_nodes) if self._merge_mode == "ensemble"
                    else merge_idea(parent_nodes))
        else:
            idea = Idea(**idea_d)
        idea = idea.model_copy(deep=True, update={"card_id": None})
        implementation_ref = self._implementation_ref(
            code=code,
            files=req.get("files"),
            deleted=req.get("deleted"),
        )
        return _InjectedNodePlan(
            idea,
            parents,
            parent_generations,
            code,
            implementation_ref,
        )

    @in_llm_lane("build")
    def _create_injected_node(self, req: dict) -> None:
        """Materialize an operator-authored experiment (`inject_node` control event) into a real
        pending node. The operator supplies an idea (operator label, params, rationale, optional
        theme) and optionally a parent and ready-made code. If no code is given, the Developer
        implements the idea — so a human can describe an experiment and let the agent build it.
        The new node enters the search as `pending`; the policy evaluates it next.

        Manual injection deliberately bypasses the policy's proposal step — the human IS the
        researcher here — but everything downstream (eval, confirmation, best-selection, lineage)
        is identical to an agent-authored node, so a hand-added winner can be selected as best."""
        state = fold(self.store.read_all())
        prepared = self._prepare_injected_node(state, req)
        idea = prepared.idea
        parents = prepared.parent_ids
        parent_generations = prepared.parent_generations
        code = prepared.code
        implementation_ref = prepared.implementation_ref
        reservation = self._reserve_node_build(
            {
                "kind": idea.operator,
                "parent_ids": parents,
                "parent_generations": parent_generations,
            },
            idea,
            scored_against=state.best_node_id,
            source="operator",
            implementation_ref=implementation_ref,
        )
        if reservation is None:
            raise ValueError("injected idea could not reserve one exact native Card")
        state = reservation.state
        node_id = reservation.node_id
        parent_generations = reservation.parent_generations
        idea = reservation.idea.model_copy(deep=True)
        with self.tracer.span("create_node", new_trace=True, node_id=node_id,
                              operator=idea.operator, source="manual"):
            developer_called = not bool(code)
            footprint_finalized = False
            if developer_called:
                try:
                    self._reset_developer_footprint(self.developer)
                    with self.tracer.span("implement"):
                        # An injected experiment usually BUILDS ON its parent (a human picked it as the
                        # base) — hand the parent's solution to a parent-aware developer. Preserve the
                        # receipt-bound Idea by handing the plugin a deep working copy.
                        _pnode = state.nodes.get(parents[0]) if parents else None
                        code = self._implement(idea.model_copy(deep=True), _pnode)
                except Exception:
                    self._fail_reserved_build(
                        node_id=node_id, card_id=reservation.card_id, generation=0,
                        error="injected Developer raised before node creation", reason="build_crash")
                    self._discard_node_build_telemetry()
                    raise
                idea, footprint_finalized = self._finalize_developer_footprint(
                    idea, self.developer, code)
            latest = fold(self.store.read_all())
            if any(pid not in latest.nodes
                   or latest.nodes[pid].attempt != generation
                   or latest.nodes[pid].tombstoned
                   or pid in latest.aborted_nodes
                   for pid, generation in ((int(pid), gen)
                                           for pid, gen in parent_generations.items())):
                self._fail_reserved_build(
                    node_id=node_id, card_id=reservation.card_id, generation=0,
                    error="parent lifecycle changed while building", reason="superseded")
                self._discard_node_build_telemetry()   # serial single-node path: self.researcher/self.developer
                return
            try:
                self._emit_node_created(
                    node_id=node_id,
                    parent_ids=parents,
                    operator=idea.operator,
                    idea=durable_idea_payload(idea),
                    code=code,
                    # Honour explicit files/deleted on the request (a cross-run `import` ships the
                    # sibling's full multi-file solution); else use the Developer's last build, and
                    # only when the Developer actually implemented (no ready-made code was supplied).
                    files=(req.get("files")
                           or ({} if req.get("code") else getattr(self.developer, "last_files", {}))) or {},
                    deleted=req.get("deleted") or [],
                    source="manual",
                    **({"parent_generations": parent_generations} if parent_generations else {}),
                    **({"footprint_finalized": True} if footprint_finalized else {}),
                    # Cross-run provenance: a DICT when this inject seeded from a sibling run's
                    # experiment (an `import` action), else None. Coerce defensively — a non-dict
                    # origin (a hand-authored/API inject that passed a label string) would make the
                    # folded Node fail validation and silently vanish, so the inject gate would keep
                    # re-creating the SAME node id forever.
                    origin=req.get("origin") if isinstance(req.get("origin"), dict) else None,
                )
            except Exception:
                try:
                    landed = node_id in fold(self.store.read_all()).nodes
                except Exception:
                    landed = False
                if not landed:
                    self._fail_reserved_build(
                        node_id=node_id, card_id=reservation.card_id, generation=0,
                        error="injected node append failed", reason="build_crash")
                raise
            if node_id not in fold(self.store.read_all()).nodes:
                self._fail_reserved_build(
                    node_id=node_id, card_id=reservation.card_id, generation=0,
                    error="injected node creation was rejected during replay", reason="superseded")
                self._discard_node_build_telemetry()   # serial single-node path: self.researcher/self.developer
                return
            # Mirror _create_node / _rerun_node: a Developer session that CRASHED returns the
            # "(developer error: …)" sentinel as its code (an LLM 401/timeout/hard error). Without
            # this guard the injected node stays pending and its eval runs the PARENT's carried-over
            # entrypoint/files and inherits the PARENT's metric — a false success (the exact bug the
            # two sibling create paths already fix). FAIL it now (node_created → node_failed keeps the
            # one-terminal invariant) and trip the SAME developer-crash circuit-breaker, so an operator
            # inject during an LLM outage can't silently slip a garbage-code node past it.
            if isinstance(code, str) and code.startswith("(developer error:"):
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node_id, "generation": 0,
                    "error": code, "reason": "developer_crash", "eval_seconds": 0.0})
                self.store.append(EV_PAUSE, {
                    "node_id": node_id, "generation": 0,
                    "reason": "auto-paused: a Developer session crashed while building an injected node "
                              "(LLM unreachable or a hard error, unresolved within the node) — resume "
                              "once it's fixed"})
        if developer_called:
            self._emit_agent_report(node_id)
            # consume predictive telemetry for this node so it can't leak onto the next created node
            self._emit_hypothesis_ranked(node_id, 0)
            self._emit_foresight_selected(node_id, 0)

    def _activate_spec(self, proposal: dict) -> None:
        """Make the ratified onboarding proposal the trusted eval (Phase 3): the eval_spec
        drives `_run_eval`, and the metric adapter is written into every eval workdir as a
        task asset AND added to the protected set so the optimization agent can't edit it
        (freeze + surface-exclude)."""
        if not proposal:
            return
        self._eval_spec = proposal.get("eval_spec", {})
        adapters = proposal.get("adapter_files", {})
        self._assets = {**self._assets, **adapters}        # frozen: written into every wd
        protected = list(self._repo_spec.get("protected_names", []))
        protected += list(adapters)                        # agent may never overwrite them
        self._repo_spec = {**self._repo_spec, "protected_names": protected}
        self._spec_activated = True

    # --------------------------------------------------------- workspace seeding
    # (extracted to engine/workspace.py — see the delegator block after __init__)
    def _workspace_fingerprint(self) -> dict:
        return self.workspace.workspace_fingerprint()

    def _setup_manifest(self, wf: "dict | None" = None) -> str:
        """P0-3 content-addressed setup: a stable digest of the MATERIAL the task+data preflight
        verified — the config hash, the workspace fingerprint, and the data-asset provenance. Binds
        `setup_done` to the exact inputs so a pre-node resume re-runs preflight (leakage!) when they
        changed rather than trusting a stale boolean. Deterministic (pure content hashes), so an
        unchanged workspace yields the recorded digest and never loops. `wf` may be passed to reuse an
        already-computed fingerprint. Both hashlib + orjson are imported for the setup block above."""
        cfg = hashlib.sha256(orjson.dumps(self.task.model_dump(mode="json"),
                                          option=orjson.OPT_SORT_KEYS)).hexdigest()[:12]
        wf = self._workspace_fingerprint() if wf is None else wf
        prov = {name: hashlib.sha256(
                    c.encode("utf-8") if isinstance(c, str) else bytes(c)).hexdigest()[:16]
                for name, c in (self._assets or {}).items()}
        return hashlib.sha256(orjson.dumps(
            {"config": cfg, "workspace": wf, "provenance": prov},
            option=orjson.OPT_SORT_KEYS)).hexdigest()[:16]

    def _env_fingerprint(self) -> dict:
        """Use the same source-owned environment identity as the quality receipt validator.

        A calibration run is pinned here and re-read later by ``speculation_quality``; two nearly
        identical package lists would make valid local evidence impossible to revalidate (or, worse,
        omit a broken direct dependency from one side).  The shared helper is metadata-only and never
        touches the network.
        """
        from looplab.search.speculation_quality import speculation_environment_fingerprint
        return speculation_environment_fingerprint()

    def _dirty_inputs(self, wf: "dict | None") -> list:
        """P0-5 dirty-input enumeration: for each git-repo workspace source, the uncommitted-file LIST
        (`git status --porcelain`) plus a bounded DIGEST of the actual diff vs HEAD (`git diff HEAD`) —
        the EXPLICIT record of which inputs differ from a clean checkout AND a content fingerprint of
        HOW, on top of the HEAD-SHA the workspace fingerprint pins (which is blind to uncommitted work).
        The digest (not the diff TEXT) is stored on purpose: it detects a changed dirty-content across
        runs WITHOUT leaking a secret a raw patch could carry (a pasted key, an edited .env) into the
        world-readable log.

        Corner-case behavior (all best-effort — a source never fails the run):
          * A heavy UNTRACKED artifact costs nothing: `git diff HEAD` never emits untracked files, so
            only its NAME lands in the porcelain list. A heavy TRACKED+modified text file would make
            git stream a giant patch, so the diff is hashed INCREMENTALLY and capped at
            `_DIFF_DIGEST_CAP` — the engine never buffers the whole patch, and an over-cap digest is
            marked `~` (truncated) so a reader knows the tail was not seen.
          * A gitignored file is INVISIBLE here BY DESIGN — porcelain skips it and the repo fingerprint
            is HEAD-only, so declared-non-source scratch (`runs/`, `__pycache__`, `model.pkl`, `.env`)
            never pollutes the enumeration (and `.env`'s secret never enters the log). A gitignored
            path that is genuinely a run INPUT should be mounted as a `data:` source, where
            `_shallow_fingerprint` covers it outside git's ignore rules.
          * Multiple sources under one repo share a single diff (computed once per resolved root).
        Bounded output: <=500 porcelain lines x 200 chars, and one capped digest per repo root."""
        import os
        import subprocess
        import time

        def _diff_digest(root: str) -> "str | None":
            # Incrementally hash `git diff HEAD` (staged + unstaged) so a multi-GB tracked-file diff
            # never lands in memory: raw fd reads, an 8 MiB byte cap, and a wall-clock deadline.
            proc = None
            try:
                proc = subprocess.Popen(["git", "-C", root, "diff", "HEAD"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                fd = proc.stdout.fileno()
                h, read, truncated, deadline = hashlib.sha256(), 0, False, time.monotonic() + 15
                while read < _DIFF_DIGEST_CAP:
                    if time.monotonic() > deadline:
                        truncated = True
                        break
                    chunk = os.read(fd, min(65536, _DIFF_DIGEST_CAP - read))
                    if not chunk:
                        break                                       # EOF: the whole diff was hashed
                    h.update(chunk)
                    read += len(chunk)
                else:
                    truncated = bool(os.read(fd, 1))                # bytes remained past the cap
                return (h.hexdigest()[:16] + ("~" if truncated else "")) if read else None
            except Exception:  # noqa: BLE001 — no HEAD / git error / decode: keep the file list only
                return None
            finally:
                if proc is not None:
                    try:
                        proc.stdout.close()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        proc.terminate()                            # stop git if we bailed mid-stream
                        proc.wait(timeout=5)
                    except Exception:  # noqa: BLE001
                        try:
                            proc.kill()
                        except Exception:  # noqa: BLE001
                            pass

        out: list = []
        digests: dict = {}                                          # resolved-root -> digest (once)
        for src in sorted((wf or {}).keys()):
            try:
                p = Path(src)
                root = str(p if p.is_dir() else p.parent)
                r = subprocess.run(["git", "-C", root, "status", "--porcelain"],
                                   capture_output=True, text=True, timeout=10)
                dirty = [ln[:200] for ln in r.stdout.splitlines() if ln.strip()][:500]
                if r.returncode == 0 and dirty:
                    entry = {"source": src, "dirty": dirty}
                    if root not in digests:
                        digests[root] = _diff_digest(root)
                    if digests[root] is not None:
                        entry["diff_digest"] = digests[root]
                    out.append(entry)
            except Exception:  # noqa: BLE001 — git missing / not a repo / timeout: no enumeration
                pass
        return out

    def _seed_workspace(self, workdir) -> None:
        return self.workspace.seed_workspace(workdir)

    def _seed_repo_tree(self, src, dst, ignore, mode: str = "auto") -> int:
        return self.workspace.seed_repo_tree(src, dst, ignore, mode)

    def _link_input(self, src, dst) -> None:
        return self.workspace.link_input(src, dst)

    # ------------------------------------------------------------- eval dispatch
    # `_agent_may` / `_ensure_run_setup` / `_do_run_setup` / `_data_binds` / `_run_eval` /
    # `_apply_sweep_best` live in looplab/engine/eval_dispatch.py (EvalDispatchMixin —
    # inherited, zero call-site churn).

    def _sandbox_cwd(self, workdir, cwd_spec) -> str:
        # extracted to engine/workspace.py — see the delegator block after __init__
        return self.workspace.sandbox_cwd(workdir, cwd_spec)

    # -------------------------------------------------------------- staged eval
    # `_resolve_stages` / `_resolved_stages` / `_imported_modules` / `_module_file_candidates` /
    # `_stage_reachable_files` / `_safe_reuse_start` / `_stage_check_fn` live in
    # looplab/engine/eval_stages.py (EvalStagesMixin — inherited, zero call-site churn).

    # ---------------------- host grading / holdout (extracted to engine/holdout.py)
    # The host-grading + D1 holdout cluster lives in looplab/engine/holdout.py
    # (`HoldoutGrader`, constructed as `self.holdout` in __init__). These thin delegators keep
    # the ORIGINAL method names on the Engine — internal callers (_run_eval / run() / the
    # critic seam) use them, and HoldoutGrader routes its internal cross-calls back through
    # them, so an instance-level monkeypatch intercepts every path. The holdout-owned MUTABLE
    # state (`_holdout_idx`, `_holdout_fraction`, `_holdout_select`, `_holdout_top_k`)
    # deliberately stays on the Engine: __init__ and run()'s resume block assign it directly
    # (and tests read `eng._holdout_idx`), so plain attributes are lower churn than
    # lessons-style properties.
    def _graded_output_name(self) -> Optional[str]:
        return self.holdout.graded_output_name()

    def _apply_host_grade(self, res, workdir):
        return self.holdout.apply_host_grade(res, workdir)

    def _host_score_split(self, preds, g: dict, *, holdout: bool) -> Optional[float]:
        return self.holdout.host_score_split(preds, g, holdout=holdout)

    def _build_holdout_idx(self, fraction: float, epoch: int = 0) -> frozenset:
        return self.holdout.build_holdout_idx(fraction, epoch)

    def _holdout_topk(self, state: RunState) -> list[int]:
        return self.holdout.holdout_topk(state)

    def _holdout_pending(self, state: RunState) -> bool:
        return self.holdout.holdout_pending(state)

    async def _holdout_phase(self, state: RunState) -> None:
        return await self.holdout.holdout_phase(state)

    # ---------------------------------------------------------------- eval task
    # `_probe_developer` / `_evaluate` (materialize -> eval -> trust scans -> inline repair ->
    # ONE terminal event) live in looplab/engine/evaluate.py (EvaluateMixin — inherited, zero
    # call-site churn).

    # ------------------------------------------------------------------- confirm
    # `_already_confirmed` / `_run_confirm_seed` / `_confirm_phase` / `_confirm_node` live in
    # looplab/engine/confirm_phase.py (ConfirmPhaseMixin — inherited, zero call-site churn).

    # ------------------------------------------------------------------ ablation
    # `_ablate` / `_segment_blocks` / `_comment_block` / `_ablate_code` live in
    # looplab/engine/ablation.py (AblationMixin — inherited, zero call-site churn).

    # ------------------------------------------------------------- trust & audit
    # `_emit_agent_report` / `_emit_role_telemetry` / `_emit_hypothesis_ranked` /
    # `_emit_foresight_selected` / `_audit_workdir_writes` / `_redact` / `_maybe_crash` /
    # `_leakage_blocks` live in looplab/engine/audit.py (AuditMixin — inherited, zero
    # call-site churn).

