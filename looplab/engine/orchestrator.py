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
import os
import secrets
import time
from pathlib import Path
from typing import Optional

import anyio
import orjson

from looplab.tools.agents_md import generate_agents_md
from looplab.events.eventstore import EventStore, EventStoreConcurrencyError
from looplab.events.types import (
    EV_APPROVAL_REQUESTED,
    EV_COMMAND_ACK,
    EV_DATA_PROFILED, EV_DATA_PROVENANCE,
    EV_DRIFT_UNAVAILABLE, EV_FORK_DONE, EV_HOST_GRADING,
    EV_INJECT_DONE, EV_INJECT_FAILED,
    EV_FINALIZE_STEP,
    EV_NODE_BUILDING,
    EV_NODE_FAILED, EV_PAUSE,
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
from looplab.engine.proposal_cues import ProposalCuesMixin
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
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.core.config import RUN_START_PINNED_FIELDS
from looplab.core.fitness import VERIFIER_SELECTION_CONTRACT
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


# The confirm phase (engine/confirm_phase.py) and ablation (engine/ablation.py) clusters are
# MIXINS — pure file-level moves inherited unchanged, so every `self._confirm_phase(...)` /
# `self._ablate(...)` call site (and every test poking those names on Engine) is untouched.
class Engine(ConfirmPhaseMixin, AblationMixin, NoveltyGateMixin, StrategyCadenceMixin,
             ResearchCadenceMixin, EvalStagesMixin, CrashRepairMixin, EvalDispatchMixin,
             AuditMixin, EvaluateMixin, NodeBuildMixin, ProposalCuesMixin):
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

        max_parallel = _opt("max_parallel")
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
        report_every = _opt("report_every")
        merge_mode = _opt("merge_mode")
        complexity_cue = _opt("complexity_cue")
        budget_aware = _opt("budget_aware")
        failure_reflection = _opt("failure_reflection")
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
        self.report_writer = report_writer
        self.report_every = max(0, report_every)
        self.developer_factory = developer_factory
        self._developer_name = "default"
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
        self._strategy_fidelity: Optional[str] = None   # None => use the Idea's own profile
        self.max_parallel = max_parallel
        self.timeout = timeout
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
            # A command ACK is a durable observation boundary. If it (or any concurrent writer)
            # extends the log after this fold, refold before doing domain work so neither a stale
            # reset nor a stale natural-finish decision can cross the newly-observed intent.
            self._ack_commands(decision_events)
            observed_tail = self.store.read_all()
            if (observed_tail[-1].seq if observed_tail else -1) != decision_seq:
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
                if self._finish_with_report_if_quiescent(
                        state, {"reason": "leakage"}, after_seq=decision_seq):
                    break
                continue
            if state.stop_requested:
                if self._finish_with_report_if_quiescent(
                        state, {"reason": "aborted"}, after_seq=decision_seq):
                    break
                continue
            if state.paused:
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
                    with self.tracer.span("onboard", new_trace=True):
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
                if self._finish_with_report_if_quiescent(
                        state, {"reason": "time_budget"}, after_seq=decision_seq):
                    break
                continue
            # Eval-compute budget (#2): cumulative time spent inside evals across the whole run
            # (persisted via the event log, so it survives resume — unlike wall-clock). Stops
            # the silent multi-hour sweep that real training runs can produce.
            if (max_es is not None
                    and state.total_eval_seconds >= max_es):
                if self._finish_with_report_if_quiescent(
                        state, {"reason": "eval_budget"}, after_seq=decision_seq):
                    break
                continue

            if await self._serve_forced_requests(state):
                continue

            state = self._run_cadences(state)
            post_cadence_events = self.store.read_all()
            post_cadence_seq = post_cadence_events[-1].seq if post_cadence_events else -1
            if post_cadence_seq != decision_seq:
                # Re-enter every gate after either an internal cadence append or a concurrent control.
                continue

            # Effective node budget: a `budget_extend` with add_nodes (e.g. "give the run 10 more
            # nodes") raises the policy's max_nodes so a reopened/resumed run keeps proposing
            # experiments instead of immediately re-finishing. Applied HERE — AFTER any in-loop policy
            # swap (strategist / set_strategy above, which rebuilds the policy un-extended) and right
            # before action selection — so the override is never dropped on a swap iteration. Floored
            # at the current node count so a stale/negative delta can't shrink the gate below work done.
            self.policy.max_nodes = max(
                len(state.nodes),
                self._base_max_nodes + int(state.budget_overrides.get("add_nodes", 0) or 0))

            # Action selection: the pure policy decides, UNLESS the unified agent self-drives —
            # in which case it picks one action from the policy-derived legal-action gate (so the
            # pipeline stays disciplined no matter what the agent chooses).
            actions = (self._agent_next_actions(state) if self.agent_drives_actions
                       else self.policy.next_actions(state))
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
                # generously so operator injects / wide seed batches never false-trip.
                _created_no_terminal += len(creates)
                if _created_no_terminal > max(self.policy.max_nodes, 4) * 3 + 50:
                    if self._finish_with_report_if_quiescent(state, {
                            "reason": "stuck: node creation not converging (no node reached terminal)"},
                            after_seq=decision_seq):
                        break
                    continue
                self._create_paused = False   # set by _create_node's developer_crash circuit-breaker
                for a in creates:
                    if "_scores" in a:   # policy exposed candidate scores -> surface "why this node"
                        self.store.append(EV_POLICY_DECISION,
                                          {"scores": a["_scores"], "chosen": a.get("_chosen"),
                                           "reason": a.get("_reason")})
                    if a.get("_rung") is not None:   # A1 ASHA: surface the successive-halving promotion
                        self.store.append(EV_RUNG_PROMOTED,
                                          {"rung": a["_rung"], "survivors": a.get("_promoted", [])})
                    self._create_node(a)  # sequential -> deterministic ids/proposals
                    if self._create_paused:
                        # A developer_crash auto-PAUSED the run (LLM unreachable / hard error). STOP the
                        # rest of the batch instead of building every seed and paying the full within-call
                        # retry/backoff on each — honouring the "PAUSE on the FIRST developer_crash"
                        # guarantee the crash branch documents. The loop re-folds paused=True at the top
                        # and finalizes; a plain `resume` continues once the cause is fixed.
                        break
                continue

            await self._dispatch_evals(evals, state, max_es)

        # Finalize (extracted to looplab/engine/finalize.py, a pure move): budget summary,
        # diversity archive, LLM cost roll-up, case store + reflection note, read-model,
        # trace.json + tree.html. Event emission order is preserved exactly.
        return finalize_run(self, entry_finished=entry_finished, start_time=start)

    # -------------------------------------------------- run() phase helpers (§4 decomposition)
    # Pure structural decomposition of run(): each method is a cohesive span lifted verbatim so the
    # loop body reads as a table of guarded steps. No behavior/ordering/gating change — every event
    # emission, _write_lock point, and fold site stays exactly where it was in the original run().

    def _run_start_pinned_values(self) -> dict:
        """The config values whose run-start record, not a later snapshot, owns re-entry semantics."""
        values = {
            "holdout_fraction": self._holdout_fraction,
            "holdout_select": self._holdout_select,
            "select_verifier": self._select_verifier,
            "select_verifier_samples": self._select_verifier_samples,
            "verifier_ci_tie": self._verifier_ci_tie,
        }
        if values.keys() != RUN_START_PINNED_FIELDS:
            raise RuntimeError("run-start pinned settings contract drifted")
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

    def _reentry_repin(self) -> bool:
        _events = self.store.read_all()
        _entry = fold(_events)
        self._pending_finalize_scope = incomplete_finalize_scope(_events)
        # A failed finalize attempt is recorded as finished(reason=error) by the CLI guard, but its
        # durable stop is still pending. Treat that as NOT already finalized so the retry below can
        # write run_finished(aborted) and re-run budget/archive/case/cost wrap-up exactly once.
        entry_finished = bool(_entry.finished and self._pending_finalize_scope is None and not (
            _entry.stop_requested and str(_entry.stop_reason or "").lower() == "error"))
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

    def _apply_control_overrides(self, state: RunState) -> tuple[Optional[float], Optional[float]]:
        # Effective budgets: an operator may raise (or lower) them live via a `budget_extend`
        # control event (folded into state.budget_overrides), e.g. "keep going for 600s more".
        # max_seconds ("keep going 600s more") is a first-class operator budget extension via the
        # budget_extend control event, not an agent_control-governed knob — applied as-is.
        max_s = state.budget_overrides.get("max_seconds", self.max_seconds)
        _bo = state.budget_overrides
        # A `budget_extend` is a HUMAN control intent, NOT an agent decision: CONTROL_EVENTS are
        # UI/CLI-authored (see the engine-writer invariant), and the boss action-builder
        # (serve/routers/boss.py::_Action) can ONLY ever emit `add_nodes` — it carries no field for
        # any resource ceiling. So the budget fields below reach the log ONLY from an operator via the
        # /control endpoint. Apply them AS-IS ("a human can always change it via the UI/snapshot").
        # Gating them on `_agent_may("boss", …)` (as an earlier M4 pass did) protected against nothing
        # — no agent authors them — and only ever DROPPED the operator's OWN override, silently pinning
        # the run to the old cap. Agent-authored resource retunes (the Strategist's timeout/max_parallel)
        # remain governed by the matrix in `_apply_strategy`, which is where the M4 lock genuinely lives.
        max_es = _bo.get("max_eval_seconds", self.max_eval_seconds)
        if "timeout" in _bo:
            try:
                self.timeout = max(0.1, float(_bo["timeout"]))
            except (TypeError, ValueError):
                pass
        if "max_parallel" in _bo:
            try:
                self.max_parallel = max(1, int(_bo["max_parallel"]))
            except (TypeError, ValueError):
                pass
        return max_s, max_es

    async def _serve_forced_requests(self, state: RunState) -> bool:
        # Operator-forced steering (Phase 5), one per iteration then re-fold. Each is gated on
        # the domain event it produces (fork_done / an ablate event / node_confirmed), so a
        # resume never repeats it — deterministic under replay. Returns True when a request was
        # served (the caller re-folds via `continue`); False lets the loop fall through.
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
            try:
                self._create_injected_node(req)
            except Exception as e:  # noqa: BLE001 - a malformed operator/API inject must not
                # crash-loop the engine: without advancing the gate, every resume replays the same
                # bad request and dies again, leaving the run unrecoverable. Record + skip it.
                self.store.append(EV_INJECT_FAILED,
                                  {"idx": state.injects_done, "error": str(e)[:500]})
            self.store.append(EV_INJECT_DONE, {"idx": state.injects_done})
            return True
        forced_ablate = next((r for r in state.ablate_request_generations
                              if r.get("node_id") in state.nodes
                              and r.get("node_id") not in state.aborted_nodes
                              and not state.nodes[r["node_id"]].tombstoned
                              and state.nodes[r["node_id"]].attempt == r.get("generation")
                              and not any(a.get("parent_id") == r["node_id"]
                                          and a.get("generation") == r.get("generation")
                                          for a in state.ablations)), None)
        if forced_ablate is None:
            legacy_ablate = next((p for p in state.ablate_requests
                                  if p in state.nodes
                                  and p not in state.aborted_nodes
                                  and not state.nodes[p].tombstoned
                                  and not any(a.get("parent_id") == p for a in state.ablations)), None)
            if legacy_ablate is not None:
                forced_ablate = {"node_id": legacy_ablate,
                                  "generation": state.nodes[legacy_ablate].attempt}
        if forced_ablate is not None:
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
        # context and (b) lands in the log for the UI / historical-replay measurement. Audit-only,
        # replay-safe (at_node gate); no-op when coverage_context is off. See search/coverage.py.
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

        # M6 comparative lessons, live-shared (doc 13 §7 items 2+5): on a node-count cadence,
        # distill credit-assigned PAIR lessons into the SHARED cross-run store DURING the run
        # (write side), and re-read the store so lessons distilled by CONCURRENT runs reach
        # this run's proposals (read side). Audit-only sidecars; replay-safe (at_node gates);
        # no-op when the cadences are 0.
        state = self._maybe_distill_lessons(state)
        state = self._maybe_refresh_lessons(state)

        # Reconciliation (memory ↔ corrected outcomes): when a node_reset re-eval FLIPS a node's
        # outcome (a false-failure re-scored to evaluated, a demoted champion), this run's DISTILLED
        # lessons grounded in that node go stale — fold-derived memory self-corrects but the LLM-written
        # lesson file does not. Retire + re-derive those lessons from the corrected state. Cheap
        # {node->sig}-hash gate: no-op unless a signature actually moved; LLM only on a genuine drift.
        state = self._maybe_reconcile_lessons(state)
        return state

    def _skip_if_aborted(self, a: dict, cur: RunState) -> bool:
        # Operator stopped this specific node (`node_abort`): skip the eval and record
        # the effect as a node_failed reason="aborted" (cooperative pre-eval skip; a
        # mid-eval kill of an in-flight subprocess is the deferred v2). An aborted node
        # keeps no metric, so replay excludes it from best-selection.
        if a["node_id"] in cur.aborted_nodes:
            n = cur.nodes.get(a["node_id"])
            if n is not None and n.status is NodeStatus.pending:
                self.store.append(EV_NODE_FAILED, {
                    "node_id": a["node_id"], "generation": n.attempt,
                    "error": "aborted by operator",
                    "reason": "aborted", "eval_seconds": 0.0})
            return True
        return False

    def _spawn_research(self, tg, state: RunState) -> None:
        """Overlap a DUE deep-research 'think' with the in-flight eval(s), INDEPENDENT of max_parallel.
        The memo is computed on a `state` snapshot in a worker thread, then RECORDED IMMEDIATELY when
        research finishes — NOT coupled to the eval completing — so its directions steer the very next
        proposal instead of landing ~an eval later. Recording from the research task is safe: it's an
        AUDIT-only event (the fold ignores it for node selection) and `EventStore.append` serializes
        writers under an interprocess lock with collision-safe seq derivation. No-op when
        concurrent_research is off or no trigger is due."""
        if not self.concurrent_research:
            return
        rtrig = self._due_research_trigger(state)
        if rtrig is None:
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

    async def _dispatch_evals(self, evals: list, state: RunState,
                              max_es: Optional[float]) -> None:
        # Single experiment at a time is the base mode: run evals sequentially and
        # deterministically. Concurrent fan-out (the task-group below) is a backlog
        # seam — opt in with max_parallel > 1. Deep research overlaps + records immediately
        # in BOTH modes (see _spawn_research), independent of max_parallel.
        if self.max_parallel <= 1:
            limiter = anyio.CapacityLimiter(1)
            async with anyio.create_task_group() as tg:
                self._spawn_research(tg, state)
                for a in evals:
                    cur = fold(self.store.read_all())
                    if self._skip_if_aborted(a, cur):
                        continue
                    # Re-check the eval-compute budget BEFORE each eval (not just per loop
                    # iteration), so a multi-eval batch can't overshoot by a whole batch (#2/#25).
                    if (max_es is not None and cur.total_eval_seconds >= max_es):
                        break
                    await self._evaluate(a["node_id"], limiter, max_es)
        else:
            # G3 distributed/parallel eval: fan out under a CapacityLimiter (worker pool). The
            # eval-budget guard the review flagged for this path: cap the number STARTED so an
            # over-budget run launches at most ~max_parallel more evals, not the whole batch.
            limiter = anyio.CapacityLimiter(self.max_parallel)
            cur = fold(self.store.read_all())
            started = 0
            async with anyio.create_task_group() as tg:
                self._spawn_research(tg, state)   # overlap deep research here too (max_parallel-independent)
                for a in evals:
                    if self._skip_if_aborted(a, cur):
                        continue
                    # Budget guard (parallel path): cap each fan-out batch to the worker-pool size.
                    # `cur` is folded ONCE before this loop and never changes mid-batch (the evals
                    # join only at the task-group exit), so a budget check on cur here is dead — the
                    # real enforcement is the per-iteration outer guard (it re-folds and finishes the
                    # run once total_eval_seconds >= max_es). Capping the batch to max_parallel bounds
                    # the overshoot to ~one batch instead of launching the whole `evals` list at once.
                    if started >= self.max_parallel:
                        break
                    tg.start_soon(self._evaluate, a["node_id"], limiter, max_es)
                    started += 1

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

    def _write_reflection_note(self, final: RunState) -> None:
        return self.lessons.write_reflection_note(final)

    def _reflect_lessons(self, final: RunState, best, fp: list) -> list:
        return self.lessons.reflect_lessons(final, best, fp)

    def _append_lessons(self, lessons: list, *, hygiene: bool = True) -> None:
        return self.lessons.append_lessons(lessons, hygiene=hygiene)

    def _comparative_lessons(self, state: RunState, fp: list, exclude=()) -> tuple[list, list]:
        return self.lessons.comparative_lessons(state, fp, exclude=exclude)

    _spent_pairs = staticmethod(LessonMemory.spent_pairs)

    def _maybe_distill_lessons(self, state: RunState) -> RunState:
        # Own op-trace: LessonMemory writes lessons_distilled via the SAME store, so an append inside
        # this span is stamped with it (current_ids) → the UI scopes the event's trace to the distill.
        with self._op_span("lessons_distill"):
            return self.lessons.maybe_distill_lessons(state)

    def _lessons_store_stamp(self):
        return self.lessons.lessons_store_stamp()

    def _maybe_refresh_lessons(self, state: RunState) -> RunState:
        with self._op_span("lessons_refresh"):
            return self.lessons.maybe_refresh_lessons(state)

    def _maybe_reconcile_lessons(self, state: RunState) -> RunState:
        # Own op-trace: reconcile appends lessons_reconciled / lessons_distilled via the SAME store,
        # so those events are scoped to this span in the UI.
        with self._op_span("lessons_reconcile"):
            return self.lessons.reconcile_lessons(state)

    def _distill_skill_body(self, final: RunState, h, ev: list) -> str:
        return self.lessons.distill_skill_body(final, h, ev)

    def _reflect_client(self):
        return self.lessons.reflect_client()

    def _causal_meta_note(self, final: RunState, best) -> Optional[str]:
        return self.lessons.causal_meta_note(final, best)

    _consolidate_lessons_file = staticmethod(LessonMemory.consolidate_lessons_file)
    _compact_lessons = staticmethod(LessonMemory.compact_lessons)

    def _store_case(self, final: RunState) -> None:
        return self.lessons.store_case(final)

    def _store_concept_capsule(self, final: RunState) -> None:
        return self.lessons.store_concept_capsule(final)

    def _store_research_claims(self, final: RunState) -> None:
        return self.lessons.store_research_claims(final)

    def _store_concept_curation(self, final: RunState) -> None:
        return self.lessons.store_concept_curation(final)

    def _store_claim_curation(self, final: RunState) -> None:
        return self.lessons.store_claim_curation(final)

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

    def _create_node(self, action: dict) -> None:
        state = fold(self.store.read_all())
        node_id = max(state.nodes, default=-1) + 1  # monotonic across the whole run -> unique
        kind = action["kind"]
        _bparents = (list(action["parent_ids"]) if action.get("parent_ids")
                     else ([action["parent_id"]] if action.get("parent_id") is not None else []))
        # Snapshot the exact parent lifecycles used below before any slow Researcher/Developer work.
        # A forced fork may also carry the generation the operator inspected; reject it before build
        # if reset already won the race. The same snapshot is embedded in node_created so replay makes
        # the final check atomically against event order (closing the check->append TOCTOU gap).
        raw_expected = action.get("parent_generations")
        if raw_expected is not None and not isinstance(raw_expected, dict):
            return
        parent_generations: dict[str, int] = {}
        for pid in _bparents:
            parent = state.nodes.get(pid)
            if parent is None or parent.tombstoned or pid in state.aborted_nodes:
                return
            if raw_expected is not None:
                expected = raw_expected.get(str(pid), raw_expected.get(pid))
                if isinstance(expected, bool):
                    return
                try:
                    expected = int(expected)
                except (TypeError, ValueError, OverflowError):
                    return
                if expected != parent.attempt:
                    return
            parent_generations[str(pid)] = parent.attempt
        if raw_expected is not None and len(raw_expected) != len(parent_generations):
            return
        # Phase-handoff ledger for THIS node build: propose → stages → plan → implement each distill
        # their transcript into a brief the next phase reads (see agents.agent.run_phase), so later
        # phases trust what earlier ones explored instead of re-reading the repo. Node-scoped (fresh
        # per build), and a no-op when the setting is off.
        from looplab.agents.agent import handoff_scope
        with self.tracer.span("create_node", new_trace=True, node_id=node_id, operator=kind), \
                handoff_scope(enabled=self._phase_handoff_summary):
            # Announce the node the INSTANT we start building it — before the Researcher/Developer run —
            # so the UI shows it (and streams its live agent-trace) immediately, not only after the
            # minutes-long dev session ends with node_created. Transient marker (folds to st.building,
            # NOT st.nodes), so node-id allocation + resume are untouched; node_created supersedes it.
            self.store.append(EV_NODE_BUILDING,
                              {"node_id": node_id, "operator": kind, "parent_ids": _bparents})
            if kind == "draft":
                self._set_complexity_hint(state, None)   # A0d breadth-keyed complexity cue
                with self.tracer.span("propose"):
                    idea = self.researcher.propose(state, None)
                # E1+T5 dedup near-duplicate proposals (one informed re-propose on a semantic hit)
                idea = self._apply_novelty_gate(
                    state, idea, repropose=lambda: self.researcher.propose(state, None))
                idea.operator = "draft"        # operator is authoritative from the policy,
                parents: list[int] = []        # not whatever label the LLM returns
                with self.tracer.span("implement"):
                    code = self.developer.implement(self._directed_idea(idea, state))
            elif kind == "merge":
                parents = list(action["parent_ids"])
                # A0b: real ensembling (code recombination) when configured/Strategist-selected;
                # else the legacy mean-param merge. Toy/baseline developers degrade to mean.
                pnodes = [state.nodes[i] for i in parents]
                idea = (self._ensemble_idea(pnodes) if self._merge_mode == "ensemble"
                        else merge_idea(pnodes))
                with self.tracer.span("implement"):
                    # A code-ensemble merge must SEED from the primary parent's solution (like improve),
                    # not implement() from scratch: from-scratch gave the Developer no base, so the
                    # ensemble node shipped without the agent-authored eval entrypoint and crash-failed
                    # ("can't open file test_looplab.py" — live node 63, 3 repairs couldn't recover). Now
                    # parent[0]'s working code + entrypoint carry over and the idea directs blending in
                    # the other parent. Mean-param merges (numeric tasks, no files) stay from-scratch.
                    _impl_from = getattr(self.developer, "implement_from", None)
                    _didea = self._directed_idea(idea, state)   # §1: directives steer the merge code too
                    code = (_impl_from(_didea, pnodes[0])
                            if (self._merge_mode == "ensemble" and _impl_from and pnodes)
                            else self.developer.implement(_didea))
            elif kind == "debug":
                parent = state.nodes[action["parent_id"]]
                parents = [parent.id]
                repair = getattr(self.developer, "repair", None)
                # Error-feedback debug: hand the failure back to the Developer to fix. Fires for
                # whole-file solutions (parent.code), multi-file edits (parent.files), AND any
                # repo task (self._repo_spec) even when a prior attempt fell back to the empty
                # baseline — so an e2e agent can fix runtime errors / missing deps from the
                # error alone (it edits requirements and the eval's setup step re-installs them).
                if callable(repair) and parent.error and (parent.code or parent.files
                                                          or self._repo_spec):
                    idea = parent.idea.model_copy()
                    idea.operator = "debug"
                    # C3 deep test-driven repair (when enabled): failure taxonomy + a structured
                    # "reproduce then fix" directive, not just the raw stderr tail. Depth is already
                    # bounded by debug_depth.
                    err = self._repair_error_context(parent.error_reason, parent.error,
                                                     state=state, node=parent)
                    with self.tracer.span("repair", parent_id=parent.id):
                        code = self._repair(parent, err, state)   # seed from parent's OWN files (repair_from)
                else:
                    # Signal-delivery (§1): the debug re-propose now gets the SAME cross-run priors +
                    # failure-reflection + fault-localization + trust cues as draft/improve — exactly
                    # when the agent is FIXING a failure it most needs "this crash class recurred
                    # before" and "the likely files to edit". Previously this branch called only
                    # _stamp_novelty_hint, so those cues were absent on the repair proposal.
                    self._set_complexity_hint(state, parent)
                    # ...then FORCE a balanced novelty stance: novelty pressure ("open a new
                    # direction") is wrong when the job is to FIX a failure, so override the stance
                    # _set_complexity_hint just stamped (it uses the live self._novelty_stance).
                    self._stamp_novelty_hint(state, "balanced")
                    with self.tracer.span("propose"):
                        idea = self.researcher.propose(state, parent)
                    idea.operator = "debug"
                    with self.tracer.span("implement"):
                        code = self._implement(self._directed_idea(idea, state), parent)
            else:  # improve
                parent = state.nodes[action["parent_id"]]
                self._set_complexity_hint(state, parent)   # A0d breadth-keyed complexity cue
                with self.tracer.span("propose"):
                    idea = self.researcher.propose(state, parent)
                # E1+T5 dedup near-duplicate proposals (one informed re-propose on a semantic hit)
                idea = self._apply_novelty_gate(
                    state, idea, repropose=lambda p=parent: self.researcher.propose(state, p))
                idea.operator = "improve"
                # PART IV D7 (§21.8): under action-space LOCK-IN with the capability_expansion lever on, this
                # improve proposal was already steered by the "build a new capability" directive — stamp it
                # as its OWN `expand` operator so operator_yields MEASURES whether expanding paid off (scored,
                # SearchFitness-competing as its own lineage), not silently as another `improve`. The relabel
                # must fire on the EXACT SAME condition as the directive (proposal_cues `_stamp_novelty_hint`):
                # flag on + `capability_expansion_due` + the EXPLORE stance. Without the stance gate, a
                # balanced/exploit improve (which got NO expand directive) would be mislabeled `expand` and
                # contaminate the expand yield (CODEX P1). Off (flag default) -> stays `improve`, byte-identical.
                if (getattr(self, "_capability_expansion", False)
                        and getattr(self, "_novelty_stance", None) == "explore"):
                    from looplab.engine.proposal_cues import _LOCK_IN_STREAK
                    from looplab.search.lock_in import capability_expansion_due
                    if capability_expansion_due(state, streak_threshold=_LOCK_IN_STREAK)[0]:
                        idea.operator = KIND_EXPAND
                parents = [parent.id]
                with self.tracer.span("implement"):
                    code = self._implement(self._directed_idea(idea, state), parent)
            # 💡 deep-research provenance: tag the first couple of nodes created right after a research
            # memo (its directions are the active steering) so the UI can show WHERE research landed in
            # the tree. Audit/UI only — never affects search. Coarse-but-honest (temporal proximity).
            research_origin = None
            if state.research:
                _m = state.research[-1]
                _ra = _m.get("at_node")
                if _ra is not None and _ra <= node_id < _ra + 2:
                    research_origin = {"at_node": _ra, "trigger": _m.get("trigger")}
            latest = fold(self.store.read_all())
            if any(pid not in latest.nodes
                   or latest.nodes[pid].attempt != generation
                   or latest.nodes[pid].tombstoned
                   or pid in latest.aborted_nodes
                   for pid, generation in ((int(pid), gen)
                                           for pid, gen in parent_generations.items())):
                # Clear the transient building marker without creating a child from abandoned code.
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node_id, "generation": 0,
                    "error": "parent lifecycle changed while building", "reason": "superseded",
                    "eval_seconds": 0.0})
                self._discard_node_build_telemetry()
                return
            self._emit_node_created(
                node_id=node_id,
                parent_ids=parents,
                operator=idea.operator,
                idea=idea.model_dump(mode="json"),
                code=code,
                files=getattr(self.developer, "last_files", {}) or {},
                deleted=getattr(self.developer, "last_deleted", []) or [],
                research_origin=research_origin,
                cross_run_receipt=getattr(self, "_cross_run_advisory_receipt", {}),
                **({"parent_generations": parent_generations} if parent_generations else {}),
            )
            if node_id not in fold(self.store.read_all()).nodes:
                self._discard_node_build_telemetry()
                return
            # The Developer session CRASHED when its code is the "(developer error: …)" sentinel (an
            # exception in _run — e.g. an LLM 401/timeout). FAIL the node now: without this it stays
            # pending, and the eval runs the PARENT's carried-over entrypoint and inherits the PARENT's
            # metric — a false success that pollutes the search (the 401-window nodes 50-54 each faked
            # the parent's 0.81 this way). node_created → node_failed keeps the one-terminal invariant.
            if isinstance(code, str) and code.startswith("(developer error:"):
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
        self._emit_agent_report(node_id)
        self._emit_hypothesis_ranked(node_id, 0)
        self._emit_foresight_selected(node_id, 0)

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
        with self.tracer.span("create_node", new_trace=True, node_id=node.id, operator=node.operator):
            self.store.append(EV_NODE_BUILDING,
                              {"node_id": node.id, "generation": node.attempt,
                               "operator": node.operator, "parent_ids": parents})
            # "propose" re-proposes (draft/improve/debug); a merge node has no single proposable idea
            # (it's an ensemble of parents), so a propose-reset there degrades to re-implement.
            if stage == "propose" and node.operator != "merge":
                with self.tracer.span("propose"):
                    idea = self.researcher.propose(state, parent)
                idea.operator = node.operator      # operator stays authoritative (from the original node)
            else:                                   # "implement" (or merge): keep the idea, re-develop
                idea = node.idea
            with self.tracer.span("implement"):
                # §1: a reset RE-BUILDS the node from scratch, so standing operator directives must
                # steer its code too — same as the four _create_node build sites.
                code = self._implement(self._directed_idea(idea, state), parent)
            latest = fold(self.store.read_all())
            current = latest.nodes.get(node.id)
            parents_current = all(
                pid in latest.nodes and latest.nodes[pid].attempt == parent_generation
                and pid not in latest.aborted_nodes and not latest.nodes[pid].tombstoned
                for pid, parent_generation in ((int(pid), gen)
                                                for pid, gen in parent_generations.items()))
            if (current is None or current.attempt != generation
                    or current.tombstoned or node.id in latest.aborted_nodes or not parents_current):
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node.id, "generation": generation,
                    "error": "node lifecycle changed while rebuilding", "reason": "superseded",
                    "eval_seconds": 0.0})
                self._discard_node_build_telemetry()
                return
            self._emit_node_created(
                node_id=node.id, parent_ids=parents, operator=idea.operator,
                idea=idea.model_dump(mode="json"), code=code,
                files=getattr(self.developer, "last_files", {}) or {},
                deleted=getattr(self.developer, "last_deleted", []) or [],
                generation=generation,
                **({"parent_generations": parent_generations} if parent_generations else {}))
            landed = fold(self.store.read_all()).nodes.get(node.id)
            if (landed is None or landed.attempt != generation or landed.rerun_from is not None
                    or landed.code != code):
                self._discard_node_build_telemetry()
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
        node_id = max(state.nodes, default=-1) + 1
        idea_d = dict(req.get("idea") or {})
        idea_d.setdefault("operator", "manual")
        # Coerce params to floats defensively (a manual form may send strings); drop unparseable.
        raw_params = idea_d.get("params") or {}
        if not isinstance(raw_params, dict):
            raw_params = {}   # a non-dict params (e.g. "lr=0.1") would AttributeError on .items()
        params: dict[str, float] = {}
        for k, v in raw_params.items():
            try:
                params[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        idea_d["params"] = params
        # Parents: accept a multi-parent `parent_ids` list (U3 drag-to-merge) or the legacy single
        # `parent_id`. Keep only ids that exist, preserving order.
        raw_parents = req.get("parent_ids")
        if isinstance(raw_parents, list):
            parents = [p for p in raw_parents if p in state.nodes]
        else:
            pid = req.get("parent_id")
            parents = [pid] if pid is not None and pid in state.nodes else []
        unavailable = [pid for pid in parents
                       if state.nodes[pid].tombstoned or pid in state.aborted_nodes]
        if unavailable:
            raise ValueError(f"parent node(s) unavailable: {unavailable}")
        parent_generations = {str(pid): state.nodes[pid].attempt for pid in parents}
        expected_parent_generations = req.get("parent_generations")
        if expected_parent_generations is not None:
            if not isinstance(expected_parent_generations, dict):
                raise ValueError("parent_generations must be an object")
            if len(expected_parent_generations) != len(parent_generations):
                raise ValueError("parent generation snapshot does not match parents")
            for pid, generation in parent_generations.items():
                if expected_parent_generations.get(pid) != generation:
                    raise ValueError(f"stale parent generation for node #{pid}")
        code = req.get("code")
        # U3 real merge: two parents + a merge operator + no ready-made code => build the idea via the
        # engine's own merge/ensemble path (code recombination), identical to a policy-driven merge —
        # so dragging node A onto node B produces a genuine combined child, not a blank manual node.
        if not code and idea_d.get("operator") == "merge" and len(parents) >= 2:
            pnodes = [state.nodes[i] for i in parents]
            idea = (self._ensemble_idea(pnodes) if self._merge_mode == "ensemble"
                    else merge_idea(pnodes))
        else:
            idea = Idea(**idea_d)
        with self.tracer.span("create_node", new_trace=True, node_id=node_id,
                              operator=idea.operator, source="manual"):
            if not code:
                with self.tracer.span("implement"):
                    # An injected experiment usually BUILDS ON its parent (a human picked it as the
                    # base) — hand the parent's solution to a parent-aware developer.
                    _pnode = state.nodes.get(parents[0]) if parents else None
                    code = self._implement(idea, _pnode)
            latest = fold(self.store.read_all())
            if any(pid not in latest.nodes
                   or latest.nodes[pid].attempt != generation
                   or latest.nodes[pid].tombstoned
                   or pid in latest.aborted_nodes
                   for pid, generation in ((int(pid), gen)
                                           for pid, gen in parent_generations.items())):
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node_id, "generation": 0,
                    "error": "parent lifecycle changed while building", "reason": "superseded",
                    "eval_seconds": 0.0})
                self._discard_node_build_telemetry()
                return
            self._emit_node_created(
                node_id=node_id,
                parent_ids=parents,
                operator=idea.operator,
                idea=idea.model_dump(mode="json"),
                code=code,
                # Honour explicit files/deleted on the request (a cross-run `import` ships the
                # sibling's full multi-file solution); else use the Developer's last build, and
                # only when the Developer actually implemented (no ready-made code was supplied).
                files=(req.get("files")
                       or ({} if req.get("code") else getattr(self.developer, "last_files", {}))) or {},
                deleted=req.get("deleted") or [],
                source="manual",
                **({"parent_generations": parent_generations} if parent_generations else {}),
                # Cross-run provenance: a DICT when this inject seeded from a sibling run's
                # experiment (an `import` action), else None. Coerce defensively — a non-dict
                # origin (a hand-authored/API inject that passed a label string) would make the
                # folded Node fail validation and silently vanish, so the inject gate would keep
                # re-creating the SAME node id forever.
                origin=req.get("origin") if isinstance(req.get("origin"), dict) else None,
            )
            if node_id not in fold(self.store.read_all()).nodes:
                self._discard_node_build_telemetry()
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
        if not req.get("code"):
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
        """P0-5 environment identity: a small, stable record of the interpreter + the key libraries a
        result depends on, pinned at run start so a resume after an upgrade can flag that the run is no
        longer bit-reproducible. Best-effort: a missing/broken package is simply omitted, and
        importlib.metadata never touches the network. Deterministic on a fixed environment."""
        import platform
        import sys
        env: dict = {"python": sys.version.split()[0], "platform": platform.platform()}
        libs: dict = {}
        try:
            from importlib.metadata import PackageNotFoundError, version
            for pkg in ("numpy", "pandas", "scikit-learn", "scipy", "torch", "xgboost",
                        "lightgbm", "tensorflow", "transformers"):
                try:
                    libs[pkg] = version(pkg)
                except PackageNotFoundError:
                    pass
                except Exception:  # noqa: BLE001 — a broken metadata entry must not fail setup
                    pass
        except Exception:  # noqa: BLE001 — importlib.metadata unavailable: skip the lib pins
            pass
        if libs:
            env["libs"] = libs
        return env

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

