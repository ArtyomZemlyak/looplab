"""Engine / control loop (I6, ADR-12/18). anyio structured concurrency:
node *creation* is sequential & deterministic; node *evaluation* fans out under a
CapacityLimiter. State is always a fresh fold of the log (files-as-truth); resume
is just re-entering this loop on an existing run dir — pending nodes get re-evaluated
idempotently, and node ids are a monotonic count so reruns never duplicate.

A crash can be injected (for the resume test) via `crash_after`: hard-exit after N
node_evaluated events have been written, simulating `kill -9` mid-run.
"""
from __future__ import annotations

import functools
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Optional

import anyio
import orjson

from looplab.tools.agents_md import generate_agents_md
from looplab.events.eventstore import EventStore
from looplab.events.types import (
    EV_AGENT_DECISION, EV_AGENT_VALIDATED, EV_APPROVAL_REQUESTED,
    EV_COVERAGE_SNAPSHOT, EV_DATA_LEAKAGE,
    EV_DATA_PROFILED, EV_DATA_PROVENANCE, EV_DEPS_INSTALLED,
    EV_DRIFT_UNAVAILABLE, EV_FORK_DONE, EV_HINT, EV_HOST_GRADING,
    EV_FORESIGHT_SELECTED,
    EV_HYPOTHESIS_ADDED, EV_HYPOTHESIS_MERGED, EV_HYPOTHESIS_RANKED, EV_INJECT_DONE, EV_INJECT_FAILED,
    EV_NODE_ABORT, EV_NODE_BUILDING, EV_NODE_CREATED,
    EV_NODE_EVALUATED, EV_NODE_FAILED, EV_NODE_REPAIRED, EV_NOVELTY_REJECTED, EV_PAUSE,
    EV_POLICY_DECISION, EV_PROXY_SCORED, EV_REPORT_GENERATED,
    EV_RESEARCH_COMPLETED, EV_REWARD_HACK_SUSPECTED, EV_RUN_FINISHED,
    EV_RUN_SETUP_FINISHED, EV_RUN_SETUP_STARTED, EV_RUN_STARTED, EV_RUNG_PROMOTED,
    EV_SETUP_FINISHED, EV_SETUP_STARTED, EV_SETUP_STEP, EV_SPEC_APPROVAL_REQUESTED,
    EV_SPEC_APPROVED, EV_SPEC_DRIFT, EV_SPEC_PROPOSED, EV_STAGE_FINISHED, EV_STRATEGY_DECISION,
    EV_WORKSPACE_CHANGED)
from looplab.trust.leakage import target_leakage, temporal_leakage, train_test_contamination
from looplab.engine.ablation import AblationMixin
from looplab.engine.confirm_phase import ConfirmPhaseMixin
from looplab.engine.finalize import finalize_run
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
from looplab.core.llm import BudgetExceeded
from looplab.core.models import Idea, NodeStatus, RunState
from looplab.search.coverage import coverage_signal
from looplab.search.operators import merge_idea
from looplab.search.policy import SearchPolicy, available_policies, make_policy
from looplab.agents.strategist import (
    NOVELTY_STANCES,
    StrategyContext,
    failure_rate,
    improves_since_best,
    is_numeric_space,
    run_phase,
    validate_strategy,
)
from looplab.core.profile import profile_dataset
from looplab.events.replay import fold
from looplab.agents.roles import Developer, Researcher
from looplab.runtime.sandbox import Sandbox
from looplab.core.tracing import JsonlSpanExporter, Tracer

# BACKLOG §4: sentinel default for every pure-config Engine.__init__ keyword. It distinguishes
# "kwarg not passed" (fall back to the `options` bundle, then to the EngineOptions default) from
# any REAL value, including None/0/False — so an explicitly passed kwarg always wins over
# `options`, and the ~100 existing keyword call sites keep their exact pre-options behavior.
_UNSET = object()

# Sentinel for `_emit_node_created`'s optional payload keys: distinguishes "key not passed"
# (the key is OMITTED from the event, matching each call site's historical payload shape)
# from a REAL value, including None (e.g. `research_origin=None` must still be emitted).
_OMIT = object()


# The confirm phase (engine/confirm_phase.py) and ablation (engine/ablation.py) clusters are
# MIXINS — pure file-level moves inherited unchanged, so every `self._confirm_phase(...)` /
# `self._ablate(...)` call site (and every test poking those names on Engine) is untouched.
class Engine(ConfirmPhaseMixin, AblationMixin):
    def __init__(
        self,
        run_dir: str | os.PathLike,
        *,
        task,
        researcher: Researcher,
        developer: Developer,
        sandbox: Sandbox,
        policy: SearchPolicy,
        # BACKLOG §4: every PURE-CONFIG knob below (the scalars/strings/bools/dicts that mirror
        # Settings) also lives on EngineOptions, and its signature default is the _UNSET sentinel.
        # Resolution per knob: explicitly passed kwarg > `options` field > EngineOptions default
        # (whose value equals the old signature default — see engine/options.py). Object seams
        # (task/roles/sandbox/policy above and the `*=None` hooks below) stay explicit parameters.
        # The annotations keep the knobs' REAL types; _UNSET is resolved away first thing below.
        options: Optional[EngineOptions] = None,
        max_parallel: int = _UNSET,   # single experiment at a time; > 1 = backlog parallel seam
        timeout: float = _UNSET,
        sweep_timeout_mult: float = _UNSET,  # intra-node sweep nodes get this × the single-eval budget
        crash_after: Optional[int] = None,
        confirm_top_k: int = _UNSET,
        confirm_seeds: int = _UNSET,
        confirm_seed_base: int = _UNSET,   # D1: first confirm seed; 1 keeps confirm splits disjoint from search's seed 0
        max_seconds: Optional[float] = _UNSET,
        max_eval_seconds: Optional[float] = _UNSET,
        memory_dir: Optional[str] = _UNSET,
        require_approval: bool = _UNSET,
        archive_resolution: float = _UNSET,
        onboarder=None,
        eval_trust_mode: str = _UNSET,
        trust_mode: str = _UNSET,
        docker_image: str = _UNSET,
        seed_mode: str = _UNSET,    # RepoTask node seeding fallback: auto|tracked|all (per-editable overrides)
        # --- A7 Strategist + richer-operator knobs (config-first; defaults == today's behavior) ---
        n_seeds: int = _UNSET,
        max_nodes: int = _UNSET,
        policy_name: str = _UNSET,
        ablate_every: int = _UNSET,
        strategist=None,            # Optional[Strategist]; None => static config policy (default)
        strategist_every: int = _UNSET,
        deep_researcher=None,       # Optional[DeepResearcher]; None => Deep-Research stage off
        deep_research_every: int = _UNSET,  # run the stage every N created nodes (0 = manual/strategist only)
        concurrent_research: bool = _UNSET,  # overlap a due research "think" with the GPU-bound eval (opt-in)
        report_writer=None,         # Optional[ReportWriter]; None => agent report off (deterministic only)
        report_every: int = _UNSET,      # regenerate the run report every N created nodes (0 = manual only)
        developer_factory=None,     # Optional[Callable[[str], Developer]] for live backend swap
        merge_mode: str = _UNSET,        # A0b: "mean" | "ensemble"
        complexity_cue: bool = _UNSET,    # A0d: breadth-keyed prompt hint
        budget_aware: bool = _UNSET,      # A5: surface remaining eval budget into the prompt
        failure_reflection: bool = _UNSET,  # A4: reflect on recent failed branches in the prompt
        deep_repair: bool = _UNSET,       # C3: structured failure-taxonomy repair context
        localize_faults: bool = _UNSET,   # C1: surface fault-localized files for repo tasks
        feature_engineering: bool = _UNSET,  # I1: CV-gated feature-engineering directive
        ablate_code_blocks: bool = _UNSET,  # A0a: ablate pipeline code blocks, not just params
        proxy_scorer=None,          # A6: Optional[ProxyScorer] early-signal candidate gate
        proxy_kill_fraction: float = _UNSET,
        reward_hack_detect: bool = _UNSET,   # B5: flag suspicious wins
        trust_gate: str = _UNSET,          # T2: audit|gate|block — what a hack/leak flag does to selection
        code_leakage_detect: bool = _UNSET,  # I3: static code-leakage scan per node
        critic_check: bool = _UNSET,         # C4: execution-free critic per node
        redact_output: bool = _UNSET,        # B3: redact secrets from persisted output tails
        novelty_mode: str = _UNSET,          # off | algo | llm — how a proposal is dedup-checked
        novelty_gate: bool = _UNSET,         # E1: dedup near-duplicate proposals (algo mode)
        novelty_epsilon: float = _UNSET,
        reflection_priors: bool = _UNSET,    # E4/M2/M3: cross-run priors + lessons (needs memory_dir)
        comparative_lessons: bool = _UNSET,  # M6: credit-assigned pair lessons (needs reflection_priors)
        lessons_every: int = _UNSET,             # M6: mid-run distill cadence in nodes (0 = run-end only)
        lessons_refresh_every: int = _UNSET,     # M6: mid-run shared-store re-read cadence (0 = start only)
        track_hypotheses: bool = _UNSET,      # P1: register deep-research directions as hypotheses
        surrogate_explore: float = _UNSET,     # A2/A3: explore weight for a lazily-wired BOHB surrogate
        unified_agent: bool = _UNSET,        # one agent plays Researcher+Developer(+Strategist)
        agent_drives_actions: bool = _UNSET,  # agent picks the next macro action (within a legal gate)
        inline_repair: bool = _UNSET,          # hybrid: triage + repair a crashed node IN PLACE (no new node)
        inline_repair_attempts: int = _UNSET,     # max in-place repair retries per node (0 = UNLIMITED)
        inline_repair_stuck_repeat: int = _UNSET, # abandon when the SAME error repeats this many times in a row
        inline_repair_reasons: tuple = _UNSET,  # reasons eligible for inline repair
        auto_install_deps: bool = _UNSET,      # pip-install a missing KNOWN lib + re-run (trusted_local only)
        dep_install_timeout: float = _UNSET,  # per-package install wall-clock budget (seconds)
        dep_installer=None,                  # Optional[Callable] install hook (test seam; default = deps.install)
        agent_control: Optional[dict] = _UNSET,  # per-setting allow-list of roles that may change it (governance)
        # D1 holdout-gated promotion (B6): reserve a fraction of host-held labels as a FINAL
        # holdout partition the search never sees; at finish, re-score the val-top-k on it and
        # (when holdout_select) let the unseen signal pick the champion. Host-graded tasks only
        # (label-partition holdout is free — the predictions already exist); 0.0 = off.
        holdout_fraction: float = _UNSET,
        holdout_select: bool = _UNSET,
        holdout_top_k: int = _UNSET,
        # Phase 2 (D3/D4/T10/P4) knobs — kept on the engine so strategist-driven policy swaps
        # rebuild policies with the same run-wide settings.
        debug_depth: int = _UNSET,            # T10: debug-lineage bound for every policy
        operator_bandit: bool = _UNSET,   # P4: deterministic UCB over operator yields (GreedyTree)
        novelty_semantic: bool = _UNSET,       # T5: embedding-similarity idea dedup (needs novelty_gate)
        novelty_semantic_threshold: float = _UNSET,
        embedder=None,                       # text→vector callable (default: zero-dep hash_embed)
        digest_char_cap: int = _UNSET,            # M5: digest prompt budget; 0 = auto-scale with run size
        research_verify: bool = _UNSET,        # D8: verify memo claims against cited evidence
        workdir_audit: bool = _UNSET,          # 4.4: flag unexpected writes in the eval workdir
        coverage_context: bool = _UNSET,       # narrowing signal: coverage_snapshot at strategist cadence
        lesson_abstractor=None,              # Memora synergy: harmonic recall over cross-run lessons
    ):
        # Resolve each pure-config knob ONCE, up front — explicit kwarg > options field > default —
        # so the assignment/validation body below is exactly the pre-EngineOptions code operating on
        # plain locals (no behavior change, no re-plumbing of the ~100 keyword call sites).
        if options is None:
            options = EngineOptions()

        def _opt(val, field: str):
            return val if val is not _UNSET else getattr(options, field)

        max_parallel = _opt(max_parallel, "max_parallel")
        timeout = _opt(timeout, "timeout")
        sweep_timeout_mult = _opt(sweep_timeout_mult, "sweep_timeout_mult")
        confirm_top_k = _opt(confirm_top_k, "confirm_top_k")
        confirm_seeds = _opt(confirm_seeds, "confirm_seeds")
        confirm_seed_base = _opt(confirm_seed_base, "confirm_seed_base")
        max_seconds = _opt(max_seconds, "max_seconds")
        max_eval_seconds = _opt(max_eval_seconds, "max_eval_seconds")
        memory_dir = _opt(memory_dir, "memory_dir")
        require_approval = _opt(require_approval, "require_approval")
        archive_resolution = _opt(archive_resolution, "archive_resolution")
        coverage_context = _opt(coverage_context, "coverage_context")
        eval_trust_mode = _opt(eval_trust_mode, "eval_trust_mode")
        trust_mode = _opt(trust_mode, "trust_mode")
        docker_image = _opt(docker_image, "docker_image")
        seed_mode = _opt(seed_mode, "seed_mode")
        n_seeds = _opt(n_seeds, "n_seeds")
        max_nodes = _opt(max_nodes, "max_nodes")
        policy_name = _opt(policy_name, "policy_name")
        ablate_every = _opt(ablate_every, "ablate_every")
        strategist_every = _opt(strategist_every, "strategist_every")
        deep_research_every = _opt(deep_research_every, "deep_research_every")
        concurrent_research = _opt(concurrent_research, "concurrent_research")
        report_every = _opt(report_every, "report_every")
        merge_mode = _opt(merge_mode, "merge_mode")
        complexity_cue = _opt(complexity_cue, "complexity_cue")
        budget_aware = _opt(budget_aware, "budget_aware")
        failure_reflection = _opt(failure_reflection, "failure_reflection")
        deep_repair = _opt(deep_repair, "deep_repair")
        localize_faults = _opt(localize_faults, "localize_faults")
        feature_engineering = _opt(feature_engineering, "feature_engineering")
        ablate_code_blocks = _opt(ablate_code_blocks, "ablate_code_blocks")
        proxy_kill_fraction = _opt(proxy_kill_fraction, "proxy_kill_fraction")
        reward_hack_detect = _opt(reward_hack_detect, "reward_hack_detect")
        trust_gate = _opt(trust_gate, "trust_gate")
        code_leakage_detect = _opt(code_leakage_detect, "code_leakage_detect")
        critic_check = _opt(critic_check, "critic_check")
        redact_output = _opt(redact_output, "redact_output")
        novelty_mode = _opt(novelty_mode, "novelty_mode")
        novelty_gate = _opt(novelty_gate, "novelty_gate")
        novelty_epsilon = _opt(novelty_epsilon, "novelty_epsilon")
        reflection_priors = _opt(reflection_priors, "reflection_priors")
        comparative_lessons = _opt(comparative_lessons, "comparative_lessons")
        lessons_every = _opt(lessons_every, "lessons_every")
        lessons_refresh_every = _opt(lessons_refresh_every, "lessons_refresh_every")
        track_hypotheses = _opt(track_hypotheses, "track_hypotheses")
        surrogate_explore = _opt(surrogate_explore, "surrogate_explore")
        unified_agent = _opt(unified_agent, "unified_agent")
        agent_drives_actions = _opt(agent_drives_actions, "agent_drives_actions")
        inline_repair = _opt(inline_repair, "inline_repair")
        inline_repair_attempts = _opt(inline_repair_attempts, "inline_repair_attempts")
        inline_repair_stuck_repeat = _opt(inline_repair_stuck_repeat, "inline_repair_stuck_repeat")
        inline_repair_reasons = _opt(inline_repair_reasons, "inline_repair_reasons")
        auto_install_deps = _opt(auto_install_deps, "auto_install_deps")
        dep_install_timeout = _opt(dep_install_timeout, "dep_install_timeout")
        agent_control = _opt(agent_control, "agent_control")
        holdout_fraction = _opt(holdout_fraction, "holdout_fraction")
        holdout_select = _opt(holdout_select, "holdout_select")
        holdout_top_k = _opt(holdout_top_k, "holdout_top_k")
        debug_depth = _opt(debug_depth, "debug_depth")
        operator_bandit = _opt(operator_bandit, "operator_bandit")
        novelty_semantic = _opt(novelty_semantic, "novelty_semantic")
        novelty_semantic_threshold = _opt(novelty_semantic_threshold, "novelty_semantic_threshold")
        digest_char_cap = _opt(digest_char_cap, "digest_char_cap")
        research_verify = _opt(research_verify, "research_verify")
        workdir_audit = _opt(workdir_audit, "workdir_audit")

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
        # boss / researcher seams via `_agent_may`. None => the conservative defaults are off (locked).
        self._agent_control: dict = dict(agent_control or {})
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
        self._idea_vecs: dict[int, list] = {}   # node_id -> embedding of its idea text (lazy cache)
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
        # The FRACTION defines the split every search metric is scored against, so it must be pinned
        # in the event log (like trust_gate / holdout_select) — on resume the recorded value is
        # re-used (see run()), so a changed live setting can't silently make pre/post-resume metrics
        # incomparable. `_build_holdout_idx` rebuilds the partition from a fraction.
        self._holdout_fraction = float(holdout_fraction)
        self._holdout_idx: frozenset = self._build_holdout_idx(self._holdout_fraction)
        # RepoTask (ADR-7): an existing repo the agent edits + a command-based eval.
        rs = getattr(task, "repo_spec", None)
        self._repo_spec: dict = rs() if callable(rs) else {}
        es = getattr(task, "eval_spec", None)
        self._eval_spec: dict = es() if callable(es) else {}
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
    async def run(self) -> RunState:
        state = fold(self.store.read_all())
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
            state = fold(self.store.read_all())
            # node_reset (operator "re-run this node from a stage"): a reset from implement/propose
            # re-develops the SAME node id IN PLACE before any other loop work, so it never mints a new
            # node. (An eval-reset needs no help here — the fold left it pending-with-code and the normal
            # eval dispatch below re-scores it.)
            _resets = [n for n in state.nodes.values() if n.rerun_from in ("implement", "propose")]
            if _resets:
                for _n in _resets:
                    self._rerun_node(_n, state)
                continue
            _terminal_now = sum(1 for _n in state.nodes.values()
                                if _n.status is not NodeStatus.pending)
            if _terminal_now != _prev_terminal:      # a node reached terminal (progress) -> reset
                _created_no_terminal = 0
                _prev_terminal = _terminal_now
            if state.finished:
                break
            # Live operator control (UI intervention via the event log). The UI appends a
            # control event; the engine — sole writer of domain events — reads the intent here
            # and writes the effect. `run_abort` terminates (resumable=no); `pause` breaks
            # WITHOUT finishing (a later `resume` event + re-entering run() continues), the same
            # files-as-truth shape as the HITL approval gate below.
            if state.stop_requested:
                self.store.append(EV_RUN_FINISHED, {"reason": "aborted"})
                break
            if state.paused:
                break
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
                self.store.append(EV_RUN_FINISHED, {"reason": "time_budget"})
                break
            # Eval-compute budget (#2): cumulative time spent inside evals across the whole run
            # (persisted via the event log, so it survives resume — unlike wall-clock). Stops
            # the silent multi-hour sweep that real training runs can produce.
            if (max_es is not None
                    and state.total_eval_seconds >= max_es):
                self.store.append(EV_RUN_FINISHED, {"reason": "eval_budget"})
                break

            if await self._serve_forced_requests(state):
                continue

            state = self._run_cadences(state)

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
                    if not state.awaiting_approval:
                        best = state.best()
                        self.store.append(EV_APPROVAL_REQUESTED, {
                            "node_id": best.id if best else None,
                            "metric": best.metric if best else None})
                    break  # awaiting approval -> stop without finishing
                # Final report on clean completion: the confirm pass just ran, so the champion +
                # robustness are settled — this is the definitive report (it reflects post-confirmation
                # state a same-at_node cadence report wouldn't). Skip only when the cadence is off
                # (report_every=0 = manual-only), so "manual only" stays truly call-free.
                if self.report_writer is not None and self.report_every > 0:
                    state = self._write_report(state, trigger="finish")
                self.store.append(EV_RUN_FINISHED, {})
                break

            ablates = [a for a in actions if a["kind"] == "ablate"]
            if ablates:
                for a in ablates:
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
                    self.store.append(EV_RUN_FINISHED, {
                        "reason": "stuck: node creation not converging (no node reached terminal)"})
                    break
                for a in creates:
                    if "_scores" in a:   # policy exposed candidate scores -> surface "why this node"
                        self.store.append(EV_POLICY_DECISION,
                                          {"scores": a["_scores"], "chosen": a.get("_chosen"),
                                           "reason": a.get("_reason")})
                    if a.get("_rung") is not None:   # A1 ASHA: surface the successive-halving promotion
                        self.store.append(EV_RUNG_PROMOTED,
                                          {"rung": a["_rung"], "survivors": a.get("_promoted", [])})
                    self._create_node(a)  # sequential -> deterministic ids/proposals
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

    def _setup_phase(self, state: RunState) -> None:
        if not state.run_id:
            # SETUP PHASE (task + data), made an explicit, ONLINE-watchable phase: the pre-node work
            # (fingerprint the workspace, hash data provenance, profile columns, write AGENTS.md) is
            # otherwise silent between run_started and the first node. `setup_started` +/ `setup_step`
            # + `setup_finished` events land in the activity feed live, and a `setup` span (node_id=-1)
            # captures the trace so the UI's Setup pseudo-node shows what happened. fold ignores these
            # (forward-compat), so they're pure observability.
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
                self.store.append(
                    EV_RUN_STARTED,
                    {
                        "run_id": self.run_dir.name,
                        "task_id": self.task.id,
                        "goal": self.task.goal,
                        "direction": self.task.direction,
                        "config_hash": cfg_hash,
                        "workspace": wf,
                        # T2 trust enforcement: recorded here so the pure fold applies the same
                        # gate on replay/resume (config isn't available to `replay.fold`). Absent in
                        # old logs -> "audit" -> byte-identical legacy selection.
                        "trust_gate": self.trust_gate,
                        # D1 holdout-gated promotion: same recorded-at-start discipline. Absent in
                        # old logs -> False -> byte-identical legacy selection. The FRACTION is
                        # pinned too so a resume re-uses the exact split every metric was scored on.
                        "holdout_select": self._holdout_select,
                        "holdout_fraction": self._holdout_fraction,
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
                _ev("agents_md"); _su_step("wrote AGENTS.md")
                # D4 data provenance: pin a content hash of every task asset/dataset into the run so a
                # result is tied to the exact data (repo tasks also pin via `workspace`). Reproducibility.
                prov = {name: hashlib.sha256(
                            c.encode("utf-8") if isinstance(c, str) else bytes(c)).hexdigest()[:16]
                        for name, c in (self._assets or {}).items()}
                if prov:
                    self.store.append(EV_DATA_PROVENANCE, {"assets": prov})
                    _ev("data_provenance", n=len(prov)); _su_step("data provenance", assets=list(prov))
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
                    _ev("data_profiled"); _su_step("data profiled")
                # Leakage-first grounding (I9): if the task exposes split/feature/target/time
                # data and a leak is detected, refuse to run — don't produce results on leaky data.
                if self._leakage_blocks():
                    self.store.append(EV_RUN_FINISHED, {"reason": "leakage"})
            self.store.append(EV_SETUP_FINISHED, {"seconds": round(time.time() - _su_t0, 3)})
        elif self._repo_spec and state.workspace and not state.workspace_changed:
            # Resume (item #4): the editable workspace is copied fresh each node, so if the
            # operator's repo changed since the run started, later nodes silently evaluate a
            # DIFFERENT codebase. Record it instead of pretending the run is reproducible.
            now = self._workspace_fingerprint()
            if now != state.workspace:
                self.store.append(EV_WORKSPACE_CHANGED, {"was": state.workspace, "now": now})

    def _reentry_repin(self) -> bool:
        entry_finished = fold(self.store.read_all()).finished  # resuming a done run?
        # A7 Strategist: re-apply the last-decided strategy on (re)entry so a resumed run continues
        # with it WITHOUT re-consulting the Strategist (the decision lives in the event log).
        _entry = fold(self.store.read_all())
        if _entry.active_strategy:
            self._apply_strategy(_entry.active_strategy)
        # D1 resume-safety: honor the holdout split the run ORIGINALLY committed to (recorded in
        # run_started), not a possibly-changed live `holdout_fraction` — otherwise nodes evaluated
        # before vs. after a config change would be scored on different splits and the champion pick
        # would mix incomparable metrics. Recorded holdout_select likewise wins on resume.
        if _entry.holdout_fraction is not None:
            if _entry.holdout_fraction != self._holdout_fraction:
                self._holdout_fraction = _entry.holdout_fraction
                self._holdout_idx = self._build_holdout_idx(self._holdout_fraction)
            self._holdout_select = _entry.holdout_select
        # E4: cross-run meta-learned priors. Excluding THIS run's id matters on resume: a run that
        # already mid-run-distilled its own comparative lessons (M6) must not read them back as if
        # they were another run's experience — its own results are already in the digest. The stamp
        # is taken BEFORE the read (a write landing in between is re-read next refresh — safe).
        self._lessons_seen_stamp = self._lessons_store_stamp()
        self._prior_note_text = self._load_reflection_priors(exclude_run_id=_entry.run_id or None)
        return entry_finished

    def _apply_control_overrides(self, state: RunState) -> tuple[Optional[float], Optional[float]]:
        # Effective budgets: an operator may raise (or lower) them live via a `budget_extend`
        # control event (folded into state.budget_overrides), e.g. "keep going for 600s more".
        max_s = state.budget_overrides.get("max_seconds", self.max_seconds)
        max_es = state.budget_overrides.get("max_eval_seconds", self.max_eval_seconds)
        # Boss (run-chat) resource retune: a `budget_extend` may carry timeout / max_parallel. Apply
        # to self.* (read fresh per eval / per batch) only when the matrix grants the boss — so the
        # operator can e.g. give the run more per-eval time or more parallelism mid-flight.
        _bo = state.budget_overrides
        if "timeout" in _bo and self._agent_may("boss", "timeout"):
            try: self.timeout = max(0.1, float(_bo["timeout"]))
            except (TypeError, ValueError): pass
        if "max_parallel" in _bo and self._agent_may("boss", "max_parallel"):
            try: self.max_parallel = max(1, int(_bo["max_parallel"]))
            except (TypeError, ValueError): pass
        return max_s, max_es

    async def _serve_forced_requests(self, state: RunState) -> bool:
        # Operator-forced steering (Phase 5), one per iteration then re-fold. Each is gated on
        # the domain event it produces (fork_done / an ablate event / node_confirmed), so a
        # resume never repeats it — deterministic under replay. Returns True when a request was
        # served (the caller re-folds via `continue`); False lets the loop fall through.
        if len(state.fork_requests) > state.forks_done:
            req = state.fork_requests[state.forks_done]
            pid = req.get("from_node_id")
            if pid in state.nodes:
                self._create_node({"kind": "improve", "parent_id": pid})  # operator-seeded branch
            self.store.append(EV_FORK_DONE, {"from_node_id": pid})         # always advance the gate
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
        forced_ablate = next((p for p in state.ablate_requests
                              if p in state.nodes
                              and not any(a.get("parent_id") == p for a in state.ablations)), None)
        if forced_ablate is not None:
            await self._ablate(forced_ablate)
            return True
        forced_confirm = next((n for n in state.confirm_requests
                               if n in state.nodes
                               and state.nodes[n].status is NodeStatus.evaluated
                               and n not in state.confirmed_forced), None)
        if forced_confirm is not None:
            await self._confirm_node(state.nodes[forced_confirm])
            return True
        return False

    def _run_cadences(self, state: RunState) -> RunState:
        # Breadth read-model: record the run's narrowing curve at the strategist cadence BEFORE the
        # Strategist decides, so the same snapshot both (a) feeds the meta-controller's decision
        # context and (b) lands in the log for the UI / historical-replay measurement. Audit-only,
        # replay-safe (at_node gate); no-op when coverage_context is off. See search/coverage.py.
        state = self._maybe_snapshot_coverage(state)

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
                    "node_id": a["node_id"], "error": "aborted by operator",
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
                    await self._evaluate(a["node_id"], limiter)
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
                    tg.start_soon(self._evaluate, a["node_id"], limiter)
                    started += 1

    # -------------------------------------------------- strategist cadence (A7)
    @staticmethod
    def _strategy_core(s: Optional[dict]) -> dict:
        """The decision-relevant subset of a Strategy (ignores rationale/source) — used to detect a
        REAL change so the engine doesn't re-record/re-apply an identical strategy every iteration."""
        if not s:
            return {}
        return {k: s.get(k) for k in ("policy", "policy_params", "developer", "operators", "fidelity", "novelty_stance", "request_research")}

    def _available_developers(self) -> list[str]:
        from looplab.agents.cli_agent import PRESETS
        names = ["default", "llm", *PRESETS]
        return names if self.developer_factory is not None else names[:1]

    def _strategy_ctx(self, state: RunState) -> StrategyContext:
        max_es = state.budget_overrides.get("max_eval_seconds", self.max_eval_seconds)
        rem = (max_es - state.total_eval_seconds) if max_es is not None else None
        defaults = {"policy": self._policy_name, "operators": {"ablate_every": self._ablate_every}}
        if max_es:
            defaults["_budget_frac"] = max(0.0, (rem or 0.0) / max_es)
        # Mean per-node eval cost so far — the cost signal the Strategist uses to bias toward an
        # intra-node sweep (amortizing data load / warm-up pays off when each eval is expensive).
        ev = [n.eval_seconds for n in state.nodes.values() if n.eval_seconds]
        avg_es = (sum(ev) / len(ev)) if ev else None
        return StrategyContext(
            node_count=len(state.nodes),
            phase=run_phase(state, self.n_seeds),
            eval_budget_remaining=rem,
            failure_rate=failure_rate(state),
            improves_since_best=improves_since_best(state),
            is_numeric_space=is_numeric_space(state),
            avg_eval_seconds=avg_es,
            node_budget_frac=(len(state.nodes) / self.policy.max_nodes
                              if getattr(self.policy, "max_nodes", 0) else 0.0),  # P2 endgame reserve
            current_policy=self._policy_name,   # D3: lets the rule switch BACK to greedy post-stall
            available_policies=available_policies(),
            available_developers=self._available_developers(),
            defaults=defaults,
            coverage=self._coverage_for_ctx(state),
        )

    def _coverage_for_ctx(self, state: RunState) -> dict:
        """The breadth read-model for the Strategist's decision context. On the cadence path the
        snapshot `_maybe_snapshot_coverage` just recorded (it runs FIRST in `_run_cadences`) already
        sits in `state` at this node-count — reuse it instead of recomputing the O(nodes) signal
        twice; an off-cadence pin_drift consult (no snapshot at this n) computes fresh. Empty when
        coverage_context is off."""
        if not self._coverage_context:
            return {}
        snaps = state.coverage_snapshots
        if snaps and snaps[-1].get("at_node") == len(state.nodes):
            return {k: v for k, v in snaps[-1].items() if k != "at_node"}
        return coverage_signal(state, resolution=self.archive_resolution)

    def _should_consult(self, state: RunState) -> bool:
        """Bounded, deterministic cadence: only at a creation decision point (no pending evals),
        at the seed boundary or every `strategist_every` created nodes."""
        if state.pending_nodes():
            return False
        n = len(state.nodes)
        if n == 0:
            return False
        return n == self.n_seeds or n % self.strategist_every == 0

    def _record_strategy(self, strat: dict, state: RunState,
                         ctx: Optional[StrategyContext] = None) -> None:
        self.store.append(EV_STRATEGY_DECISION, {
            "strategy": strat,
            "at_node": len(state.nodes),
            "ctx": (ctx.model_dump(include={"phase", "eval_budget_remaining", "failure_rate"})
                    if ctx is not None else None),
        })
        self._apply_strategy(strat)

    def _ensure_surrogate(self) -> None:
        """Wrap the Researcher in a SurrogateResearcher if it isn't already (idempotent). Used when a
        mid-run strategy switch turns BOHB on: BOHB is ASHA's racing schedule PLUS the surrogate
        proposer, and the proposer is only wired at startup for policy=bohb/surrogate_proposer — so a
        Strategist switching to bohb would otherwise run bare ASHA. Needs numeric bounds; if the
        Researcher (or its inner/fallback) exposes none, this is a no-op (bohb degrades to ASHA)."""
        from looplab.search.surrogate import SurrogateResearcher
        # Unified mode: re-wrapping `self.researcher` here would desync it from `self.developer`
        # (the same agent object) — the cli already skips the startup surrogate wrap for the same
        # reason (R1). A mid-run switch to bohb degrades to bare ASHA, which is acceptable.
        if self.unified_agent or isinstance(self.researcher, SurrogateResearcher):
            return
        bounds = (getattr(self.researcher, "bounds", None)
                  or getattr(getattr(self.researcher, "inner", None), "bounds", None)
                  or getattr(getattr(self.researcher, "fallback", None), "bounds", None))
        if bounds:
            self.researcher = SurrogateResearcher(bounds, fallback=self.researcher,
                                                  explore=self._surrogate_explore)

    def _apply_strategy(self, strat: dict) -> None:
        """Rebuild the live search machinery from a Strategy (pure wiring, no events). Policies share
        the action vocabulary and are pure, so swapping between loop iterations is safe; the Developer
        is swapped only between sequential _create_node calls."""
        if strat.get("novelty_stance") in NOVELTY_STANCES:
            self._novelty_stance = strat["novelty_stance"]   # Strategist's novelty dial (slice 2)
        ops = strat.get("operators") or {}
        if "ablate_every" in ops:
            self._ablate_every = int(ops["ablate_every"])
        if "merge_mode" in ops:
            self._merge_mode = ops["merge_mode"]
        if "complexity_cue" in ops:
            self._complexity_cue = bool(ops["complexity_cue"])
        if "ablate_code_blocks" in ops:
            self._ablate_code_blocks = bool(ops["ablate_code_blocks"])
        if "prefer_sweep" in ops:
            self._prefer_sweep = bool(ops["prefer_sweep"])
        # Resource budgets the Strategist may retune live (gated by the governance matrix). self.timeout
        # is read fresh per eval and self.max_parallel rebuilds the CapacityLimiter each batch, so a
        # mid-run change takes effect on the next node without any rewiring.
        if "timeout" in strat and self._agent_may("strategist", "timeout"):
            try:
                self.timeout = max(0.1, float(strat["timeout"]))
            except (TypeError, ValueError):
                pass
        if "max_parallel" in strat and self._agent_may("strategist", "max_parallel"):
            try:
                self.max_parallel = max(1, int(strat["max_parallel"]))
            except (TypeError, ValueError):
                pass
        pol = strat.get("policy")
        if pol:
            try:
                # Strip the names make_policy takes as explicit kwargs: a policy_params entry like
                # {"n_seeds": 4} would otherwise raise "multiple values for keyword argument",
                # silently dropping the whole switch (recorded decision diverging from live policy).
                pp = {k: v for k, v in (strat.get("policy_params") or {}).items()
                      if k not in ("n_seeds", "max_nodes", "ablate_every",
                                   "debug_depth", "operator_bandit")}
                self.policy = make_policy(pol, n_seeds=self.n_seeds, max_nodes=self.max_nodes,
                                          ablate_every=self._ablate_every,
                                          debug_depth=self._debug_depth,
                                          operator_bandit=self._operator_bandit, **pp)
                self._base_max_nodes = getattr(self.policy, "max_nodes", self.max_nodes)  # new base for the live override
                # A3 BOHB = ASHA racing + the surrogate proposer. make_policy only builds the racing
                # half; wire the surrogate now so a mid-run switch to bohb isn't bare ASHA.
                if pol == "bohb":
                    self._ensure_surrogate()
                self._policy_name = pol
            except (ValueError, TypeError):
                pass    # keep the current policy on a bad spec (validate_strategy already whitelisted)
        fid = strat.get("fidelity")
        if fid in ("smoke", "full"):
            self._strategy_fidelity = fid
        elif fid == "adaptive":
            self._strategy_fidelity = None
        dev = strat.get("developer")
        # Unified mode: researcher IS developer (one agent). A live developer-backend swap would
        # replace `self.developer` with a different object, desyncing it from `self.researcher` (and
        # the factory, still seeing unified_agent=True, would build a whole new agent). The unified
        # agent owns its own implement stage — skip the swap rather than fracture the identity (R1).
        if dev and self.developer_factory is not None and dev != self._developer_name \
                and not self.unified_agent:
            try:
                self.developer = self.developer_factory(dev)
                self._developer_name = dev
            except Exception:  # noqa: BLE001 — a bad backend swap must never abort the run
                pass

    @staticmethod
    def _already_covered_at(state: RunState, n: int) -> bool:
        return any((c or {}).get("at_node") == n for c in state.coverage_snapshots)

    def _maybe_snapshot_coverage(self, state: RunState) -> RunState:
        """Record a `coverage_snapshot` (breadth read-model) at the strategist cadence, then re-fold.
        Recorded even when NO Strategist is wired, so the run's narrowing curve is always queryable
        over the log / replayable historically (fold -> coverage_signal). Audit-only — it never
        affects node selection; folded only so the at_node gate makes a resume idempotent (each
        node-count decision point is reached once across the run's lifetime). No-op when
        coverage_context is off, off-cadence, mid-eval, or already snapshotted at this node-count."""
        n = len(state.nodes)
        if (not self._coverage_context or not self._should_consult(state)
                or self._already_covered_at(state, n)):
            return state
        self.store.append(EV_COVERAGE_SNAPSHOT, {
            "at_node": n, **coverage_signal(state, resolution=self.archive_resolution)})
        return fold(self.store.read_all())

    def _op_span(self, name: str, **attrs):
        """A named NEW-trace span for a sub-operation (strategist consult, hypothesis merge …) so the
        event appended inside it is auto-stamped with THIS op's trace_id (eventstore reads current_ids),
        letting the UI scope the event's trace to just that operation. Null-context when no tracer is
        wired (tests build Engine via __new__ and skip __init__) — the op still runs, just untraced."""
        import contextlib
        tr = getattr(self, "tracer", None)
        return tr.span(name, new_trace=True, **attrs) if tr is not None else contextlib.nullcontext()

    def _maybe_consult_strategist(self, state: RunState) -> RunState:
        """Operator/boss pin first (HITL parity), then the bounded-cadence Strategist consult.
        Records a `strategy_decision` and re-folds only when the strategy actually changes.

        An operator/boss `set_strategy` pin owns ONLY the fields it names (policy / policy_params /
        fidelity); those stay in force for the rest of the run (until re-pinned), while the
        autonomous Strategist keeps tuning everything else. The pin is MERGED onto the live strategy
        (not reset to the bare pin) and re-asserted only when a pinned field actually drifts — that,
        plus overlaying the pinned fields onto the Strategist's own decision below, is what stops the
        pin and the Strategist from thrashing (the old "reset to bare pin on any divergence"
        oscillated the policy every consult and dropped the Strategist's fidelity/operators)."""
        pin = state.pending_strategy or {}
        raw_pin = {k: pin[k] for k in ("policy", "policy_params", "fidelity")
                   if pin.get(k) is not None}
        consulting = self.strategist is not None and self._should_consult(state)
        active_core = self._strategy_core(state.active_strategy)
        # Cheap pre-check (no ctx/validate): a pin "drifts" if a raw pinned field differs from what's
        # active. For an INVALID pin this is a false alarm (it can never become active), so we still
        # validate below before acting on it.
        pin_drift = bool(raw_pin) and any(active_core.get(k) != v for k, v in raw_pin.items())
        if not pin_drift and not consulting:
            return state
        ctx = self._strategy_ctx(state)
        # Validate the pin against the SAME whitelist the engine applies, keeping only the pinned
        # fields that survive. The boss `strategy` action carries free-text policy/fidelity (server
        # `_Action.policy/fidelity`, unvalidated), so an out-of-whitelist value would otherwise be
        # overlaid RAW onto the recorded strategy below — diverging from the live policy that
        # make_policy silently rejects — and, never matching active_strategy, re-assert (and starve
        # the autonomous Strategist + spam the log) on every consult. Dropping it here makes an
        # invalid pin a harmless no-op.
        vpin = validate_strategy({**raw_pin, "source": "operator"}, ctx) if raw_pin else None
        pin_fields = {k: vpin[k] for k in raw_pin if vpin and k in vpin}
        # 1. Re-assert the pin only if a VALID pinned field isn't currently in force (merge onto active).
        if pin_fields and any(active_core.get(k) != v for k, v in pin_fields.items()):
            strat = validate_strategy({**(state.active_strategy or {}), **pin_fields,
                                       "source": "operator"}, ctx)
            if strat:
                strat.setdefault("rationale", "operator-pinned strategy")
                self._record_strategy(strat, state, ctx)
                return fold(self.store.read_all())
        # 2. Bounded-cadence Strategist consult — but the pin wins over it for the pinned fields.
        # Its own trace (new_trace) so the strategy_decision event — appended INSIDE via _record_strategy
        # — is stamped with THIS operation's trace_id (eventstore auto-stamps current_ids()), letting the
        # UI show only the strategist's own reasoning trace under that event, not the whole node's trace.
        if consulting:
            # No node_id on the op span: stamping it would file the strategist's LLM generations under
            # the NEXT node (id == len(nodes)) in /trace, polluting that node's Trace tab. The event still
            # gets THIS span's trace_id (current_ids), which is how the UI scopes it via by_trace.
            with self._op_span("strategist_consult"):
                strat = validate_strategy(self.strategist.decide(state, ctx), ctx)
                if strat:
                    strat.update(pin_fields)   # pinned (validated) policy/fidelity are non-negotiable
                    if self._strategy_core(strat) != self._strategy_core(state.active_strategy):
                        self._record_strategy(strat, state, ctx)
                        return fold(self.store.read_all())
        return state

    # ---------------------------------------------------- research cadence (P2)
    def _maybe_deep_research(self, state: RunState) -> RunState:
        """Run the Deep-Research stage when there's demand, then re-fold. Three triggers, each gated
        for replay safety: a MANUAL `deep_research` control event (counter gate), a CADENCE
        (`deep_research_every`, once per node-count), or a Strategist `request_research` decided at
        this node-count. No-op when the stage is off or already served. Records `research_completed`
        (audit-only sidecar) and feeds the memo's directions back as a standing hint."""
        n = len(state.nodes)
        # Manual: serve outstanding requests first, regardless of node-count (operator asked now).
        if len(state.research_requests) > state.research_served:
            return self._run_deep_research(state, trigger="manual", manual=True)
        # Auto triggers only at a creation decision point (no pending evals), never re-firing at a
        # node-count already researched (the at_node gate makes resume a no-op).
        if state.pending_nodes() or n == 0 or self._already_researched_at(state, n):
            return state
        if self.deep_research_every and n % self.deep_research_every == 0:
            return self._run_deep_research(state, trigger="cadence", manual=False)
        hist = state.strategy_history
        if (hist and hist[-1].get("at_node") == n
                and (hist[-1].get("strategy") or {}).get("request_research")):
            return self._run_deep_research(state, trigger="strategist", manual=False)
        return state

    @staticmethod
    def _already_researched_at(state: RunState, n: int) -> bool:
        return any((m or {}).get("at_node") == n for m in state.research)

    def _run_deep_research(self, state: RunState, *, trigger: str, manual: bool) -> RunState:
        """Execute one Deep-Research step (serial path) and record it, then re-fold. Always records a
        `research_completed` event (even with no model wired, so a manual request's gate advances and
        the loop doesn't spin)."""
        # One trace for the whole serial step: compute WITHOUT its own inner span (trace=False) so the
        # research LLM spans + the research_completed append both live in THIS op-trace → the event is
        # stamped with it (UI scopes the event's trace to just the research, not a node).
        with self._op_span("deep_research", trigger=trigger):
            memo = self._compute_deep_research(state, trigger, trace=False)
            self._record_deep_research(memo, trigger=trigger, manual=manual)
        return fold(self.store.read_all())

    def _compute_deep_research(self, state: RunState, trigger: str, *, trace: bool = True):
        """PURE compute: run one Deep-Research step and RETURN the memo WITHOUT writing the event log,
        so it can run in a worker thread concurrently with an eval while the engine stays the sole
        writer. Best-effort — never raises (a crash/None model yields a stub so the gate still advances).
        `trace=False` skips the span: the tracer is not safe to write from the concurrent worker."""
        from looplab.core.models import ResearchMemo
        if self.deep_researcher is None:
            return ResearchMemo(at_node=len(state.nodes), trigger=trigger,
                                summary="(deep research unavailable: no model configured)")
        try:
            if trace:
                with self.tracer.span("deep_research", new_trace=True, trigger=trigger):
                    return self.deep_researcher.research(state, trigger=trigger)
            return self.deep_researcher.research(state, trigger=trigger)
        except Exception as exc:  # noqa: BLE001 — advisory sidecar must never kill the run
            return ResearchMemo(at_node=len(state.nodes), trigger=trigger,
                                summary=f"(deep research failed: {exc})")

    def _record_deep_research(self, memo, *, trigger: str, manual: bool) -> None:
        """Append the memo to the event log (engine = sole writer; called only from the main task)."""
        memo_d = memo.model_dump(mode="json")
        # D8 · decoupled Verifier: check the memo's claims against their CITED evidence before the
        # memo is recorded — synthesis is the documented weak link (Kosmos: 57.9% accurate).
        # Deterministic layer always (refs exist? quoted numbers match?); LLM rubric pass when a
        # client is wired. Verdicts ride INSIDE the memo dict (audit-only; fold untouched).
        if self._research_verify and memo_d.get("claims"):
            try:
                from looplab.trust.verify import verify_memo
                state = fold(self.store.read_all())
                ver = verify_memo(memo_d, state,
                                  client=getattr(self.deep_researcher, "client", None),
                                  parser=getattr(self.deep_researcher, "parser", "tool_call"))
                if ver is not None:
                    memo_d["verification"] = ver
            except Exception:  # noqa: BLE001 — verification must never block the memo
                pass
        self.store.append(EV_RESEARCH_COMPLETED, {
            "memo": memo_d,
            "at_node": memo.at_node, "trigger": trigger, "served_manual": manual})
        # Steer the next proposals: surface the memo's directions as a standing operator hint (the
        # same channel the Researcher already reads), so deep research actually informs planning.
        if memo.recommended_directions:
            self.store.append(EV_HINT, {
                "text": "deep-research directions: " + "; ".join(memo.recommended_directions[:5]),
                "source": "deep_research"})
            # P1: also register each direction as an OPEN hypothesis so a deep-research idea is
            # tracked to a verdict (was fire-and-forget) — it accrues evidence when a matching node
            # runs, and shows on the board as an open question the search should resolve.
            if self._track_hypotheses:
                for direction in memo.recommended_directions[:5]:
                    if str(direction).strip():
                        self.store.append(EV_HYPOTHESIS_ADDED, {
                            "statement": str(direction).strip(), "source": "deep_research",
                            "at_node": memo.at_node})

    def _due_research_trigger(self, state: RunState) -> str | None:
        """Is an AUTO deep-research trigger (cadence/strategist) due at the current node-count? Used by
        the concurrent-research seam to overlap the "think" with an in-flight eval. Mirrors the auto
        triggers in _maybe_deep_research but WITHOUT the no-pending gate (we overlap with pending evals
        on purpose). Manual requests stay on the serial path; the at_node gate (a memo recorded at this
        node-count) keeps the serial path from re-firing after the concurrent memo lands."""
        if self.deep_researcher is None:
            return None
        n = len(state.nodes)
        if n == 0 or self._already_researched_at(state, n):
            return None
        if self.deep_research_every and n % self.deep_research_every == 0:
            return "cadence"
        hist = state.strategy_history
        if (hist and hist[-1].get("at_node") == n
                and (hist[-1].get("strategy") or {}).get("request_research")):
            return "strategist"
        return None

    def _maybe_merge_hypotheses(self, state: RunState) -> RunState:
        """Agentic consolidation of the OPEN-hypothesis board (P1+). The fold merges hypotheses only by
        EXACT statement hash, so paraphrases of one belief pile up as separate open entries. Here —
        LIVE only, gated on `track_hypotheses` + a reflect client — hybrid retrieval clusters near-dups
        and the agent decides the true merges, appended as `hypothesis_merged` events that the fold
        applies deterministically (alias evidence -> canonical). Best-effort: never raises, never
        blocks the loop. Cadence: only when the open board has grown to >=4 and by >=2 since the last
        pass, so it doesn't re-run every node or thrash. Replay-safe — the engine only WRITES the
        decision here; on replay the fold reapplies the recorded merges with no model call, and a
        re-run finds already-merged aliases gone (converges)."""
        if not self._track_hypotheses:
            return state
        client = self._reflect_client()
        if client is None:
            return state
        open_hyps = [h for h in state.hypotheses.values() if getattr(h, "status", "") == "open"]
        n = len(open_hyps)
        if n < 4 or (n - getattr(self, "_last_hyp_merge_n", -1)) < 2:
            return state
        self._last_hyp_merge_n = n
        try:
            from looplab.search.hybrid_merge import consolidate
            texts = [h.statement for h in open_hyps]
            wrote = False
            # Own trace so each hypothesis_merged event (appended INSIDE) is stamped with THIS merge's
            # trace_id — the UI can then show only the merge's own retrieval+decision trace under it.
            with self._op_span("hypothesis_merge"):   # no node_id — see strategist_consult (avoids leaking into a node's trace)
                for g in consolidate(texts, client, kind="research hypotheses",
                                     embed=self._embedder, goal=state.goal):
                    if len(g["members"]) < 2:
                        continue
                    ids = [open_hyps[i].id for i in g["members"]]
                    self.store.append(EV_HYPOTHESIS_MERGED, {
                        "canonical": ids[0], "aliases": ids[1:], "statement": g["merged"],
                        "at_node": len(state.nodes)})
                    wrote = True
        except Exception:  # noqa: BLE001 — advisory hygiene; a merge hiccup must not disturb the loop
            return state
        return fold(self.store.read_all()) if wrote else state

    def _maybe_refresh_report(self, state: RunState) -> RunState:
        """Regenerate the agent-authored run report on a node-count cadence, then re-fold. No-op when
        the writer is off, when there's nothing evaluated yet, or when the report is already current
        for this node-count (the `at_node` gate makes resume a no-op). Best-effort sidecar."""
        if self.report_writer is None or self.report_every <= 0:
            return state
        if state.pending_nodes() or not state.evaluated_nodes():
            return state
        n = len(state.nodes)
        last = int((state.report or {}).get("at_node") or 0)
        if not self._cadence_due(n, last, self.report_every):   # resume-safe since-last gate
            return state
        return self._write_report(state, trigger="cadence")

    def _write_report(self, state: RunState, *, trigger: str) -> RunState:
        """Generate one run report and record it as a `report_generated` event, then re-fold. Never
        raises — the writer itself degrades to a minimal report on any failure."""
        if self.report_writer is None:
            return state
        with self.tracer.span("report", new_trace=True, trigger=trigger):
            content = self.report_writer.generate(state, trigger=trigger)
            # append INSIDE the span so report_generated is stamped with the report op-trace (UI scopes it).
            self.store.append(EV_REPORT_GENERATED, {
                "content": content, "at_node": content.get("at_node"), "trigger": trigger})
        return fold(self.store.read_all())

    def _set_complexity_hint(self, state: RunState, parent) -> None:
        """Inject the engine-computed proposal cues into the next prompt: A0d (breadth-keyed
        complexity) + A5 (remaining eval budget). No-op unless the respective knob is on; harmless on
        Toy roles. Both flow via the single `_complexity_hint` attribute both Researchers read."""
        hint = ""
        if self._complexity_cue:
            nc = (sum(1 for n in state.nodes.values() if parent.id in n.parent_ids)
                  if parent is not None else len([n for n in state.nodes.values() if not n.parent_ids]))
            level = ("a minimal baseline" if nc < 2 else "a moderate approach" if nc < 4
                     else "an advanced approach (ensembling / HPO / feature-engineering)")
            hint += (f"\nComplexity guidance: this branch already has {nc} sibling experiment(s); "
                     f"propose {level}.")
        if self._budget_aware:
            max_es = state.budget_overrides.get("max_eval_seconds", self.max_eval_seconds)
            if max_es:
                rem = max(0.0, max_es - state.total_eval_seconds)
                frac = rem / max_es if max_es else 1.0
                stance = ("explore broadly — plenty of budget" if frac > 0.5 else
                          "be selective — budget is over half spent" if frac > 0.2 else
                          "exploit the leader with cheap experiments — budget nearly spent")
                hint += (f"\nBudget guidance: {rem:.0f}s of {max_es:.0f}s eval budget remain "
                         f"({frac:.0%}); {stance}.")
        if self._failure_reflection:
            fails = sorted((n for n in state.nodes.values()
                            if n.status is NodeStatus.failed and n.error_reason),
                           key=lambda n: n.id, reverse=True)[:3]
            if fails:
                summ = "; ".join(f"node {n.id} ({n.error_reason}): {(n.error or '')[:60]}" for n in fails)
                hint += f"\nReflection — recent failures to avoid repeating: {summ}."
        if self._localize_faults and self._repo_spec.get("editables"):
            fails = sorted((n for n in state.nodes.values()
                            if n.status is NodeStatus.failed and n.error),
                           key=lambda n: n.id, reverse=True)
            if fails:
                from looplab.engine.localize import localize
                roots = [e["path"] for e in self._repo_spec["editables"]]
                loc = localize(fails[0].error, roots,
                               idea_text=(parent.idea.rationale if parent is not None else ""))
                if loc:
                    files = ", ".join(item["file"] for item in loc[:3])
                    hint += f"\nFault localization — likely files to edit: {files}."
        if self._feature_engineering and (self.task_has_columns or self._assets):
            hint += ("\nFeature engineering: propose 1-2 semantically-meaningful engineered features "
                     "(ratios, interactions, aggregations, domain transforms) as code. The eval's "
                     "cross-validation gates them — KEEP a feature only if it improves CV; drop any "
                     "that don't (feature engineering is non-universal).")
        hint += self._prior_note_text   # E4: cross-run meta-learned prior (empty unless enabled)
        try:
            setattr(self.researcher, "_complexity_hint", hint)
        except Exception:  # noqa: BLE001
            pass
        # A7 `prefer_sweep`: nudge — never force — the Researcher toward an intra-node sweep when the
        # Strategist's cost model favors in-process execution. Cleared when the flag is off, so a one-
        # time bias doesn't persist after the Strategist moves on.
        sweep_hint = ("\nStrategy bias: evals here are costly and the space is numeric — STRONGLY "
                      "consider a SWEEP (set `space` to a small grid) so many configs share one "
                      "data load." if self._prefer_sweep else "")
        try:
            setattr(self.researcher, "_sweep_hint", sweep_hint)
        except Exception:  # noqa: BLE001
            pass
        self._stamp_novelty_hint(state, self._novelty_stance)

    def _stamp_novelty_hint(self, state: RunState, stance: str) -> None:
        """Stamp the Strategist's novelty dial onto the ACTIVE researcher (slice 2/4): a prose
        directive `_novelty_hint` (+ the coverage gaps to act on) that the researcher folds into its
        prompt, plus the stance VALUE `_novelty_stance` the foresight ranker reads. "balanced" ->
        empty hint (byte-identical to today's prompt). Extracted so the DEBUG/repair path can force a
        NEUTRAL "balanced" stance — novelty pressure ("open a new direction") is wrong when the job is
        to FIX a failure — and so draft/improve refresh it from the live `self._novelty_stance` every
        node (no stale hint bleeds from a prior operator into a later one)."""
        nov_hint = ""
        if stance == "explore":
            # Reuse the breadth snapshot the strategist cadence already recorded (its most recent view)
            # instead of recomputing the O(nodes) signal on this per-proposal hot path — the hint is
            # prose, so the last snapshot is fresh enough. Falls back to {} before the first snapshot.
            cov = state.coverage_snapshots[-1] if state.coverage_snapshots else {}
            top = cov.get("top_themes") or []
            spread = (f" So far the search concentrates on '{top[0][0]}' "
                      f"({cov.get('dominant_theme_frac', 0.0):.0%} of experiments); "
                      f"themes tried: {[t for t, _ in top]}." if top else "")
            nov_hint = ("\nNovelty stance: EXPLORE — the search is narrowing." + spread +
                        " Propose a MEANINGFULLY DIFFERENT direction (a new theme / approach / "
                        "component), not a variation of the current leader — broaden the space.")
        elif stance == "exploit":
            nov_hint = ("\nNovelty stance: EXPLOIT — refine and deepen the current best line of "
                        "attack; a focused improvement beats opening a new direction now.")
        for _attr, _val in (("_novelty_hint", nov_hint), ("_novelty_stance", stance)):
            try:
                setattr(self.researcher, _attr, _val)
            except Exception:  # noqa: BLE001
                pass

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

    def _load_reflection_priors(self, exclude_run_id: Optional[str] = None) -> str:
        return self.lessons.load_reflection_priors(exclude_run_id=exclude_run_id)

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

    @staticmethod
    def _cadence_due(n: int, last: int, every: int) -> bool:
        """The shared since-last node-count gate (report/distill/refresh cadences). Since-last
        (not `n % every == 0`): a failed/merge/ablate node-count jump must not step over the only
        multiple and silently skip the whole window."""
        return every > 0 and n > 0 and n - last >= every

    # -------------------------------------------------------- novelty gate (E1/T5)
    @staticmethod
    def _idea_text(idea) -> str:
        """The semantic identity of a proposal: what it claims to try + why."""
        return " ".join(filter(None, [getattr(idea, "rationale", "") or "",
                                      getattr(idea, "hypothesis", "") or ""])).strip()

    def _idea_vec(self, node_id: int, text: str):
        v = self._idea_vecs.get(node_id)
        if v is None:
            v = self._embedder(text)
            self._idea_vecs[node_id] = v
        return v

    def _semantic_duplicate(self, state: RunState, idea: Idea):
        """T5: nearest existing node by idea-TEXT embedding similarity, or None. Only meaningful
        for proposals with real text (LLM ideas); short/empty rationales (toy backends) skip."""
        text = self._idea_text(idea)
        if len(text) < 20:
            return None, 0.0
        from looplab.tools.vectorstore import _cosine
        v = self._embedder(text)
        best_n, best_s = None, 0.0
        for n in state.nodes.values():
            nt = self._idea_text(n.idea)
            if len(nt) < 20:
                continue
            try:
                s = _cosine(v, self._idea_vec(n.id, nt))
            except Exception:  # noqa: BLE001 — an embedder hiccup must never block proposing
                continue
            if s > best_s:
                best_n, best_s = n, s
        if best_n is not None and best_s >= self._novelty_semantic_threshold:
            return best_n, best_s
        return None, best_s

    def _llm_novelty_gate(self, state: RunState, idea: Idea, repropose=None) -> Idea:
        """novelty_mode="llm": an LLM (not an embedding/param-distance heuristic) judges whether the
        proposed idea near-duplicates an already-tried experiment — READING the real experiments via
        tools when unsure — and, if it does and a `repropose` callable is given, asks the Researcher once
        more for a meaningfully different idea (surfacing the duplicate's outcome). Loop-safe + best-
        effort: any failure just returns the original idea. Emits the same `novelty_rejected` audit
        event (kind="llm") the algorithmic gate does."""
        if not state.nodes:
            return idea
        try:
            client = self._reflect_client()
        except Exception:  # noqa: BLE001
            client = None
        if client is None:
            return idea
        from pydantic import BaseModel
        from looplab.agents.agent import agentic_struct, CompositeTools
        from looplab.tools.run_tools import RunTools

        class _NoveltyVerdict(BaseModel):
            is_duplicate: bool = False
            near_node_id: Optional[int] = None
            reason: str = ""

        brief = "; ".join(f"#{n.id} {n.operator}: {self._idea_text(n.idea)[:80]}"
                          for n in list(state.nodes.values())[-25:])
        msgs = [{"role": "system",
                 "content": "You judge experiment NOVELTY for an ML research loop. Decide if a PROPOSED "
                            "idea is a near-duplicate of an experiment already tried in THIS run. Read the "
                            "actual experiments (read_experiment / read_code) when unsure. A rewording or a "
                            "trivially-close variant of a tried idea is a DUPLICATE; a genuinely different "
                            "approach, component, loss, data or direction is NOVEL. Prefer NOVEL unless "
                            "clearly a repeat."},
                {"role": "user",
                 "content": f"PROPOSED idea: {self._idea_text(idea)}\n\nAlready tried: {brief}\n\n"
                            "Emit is_duplicate, near_node_id (the tried experiment it duplicates, or null), "
                            "and a one-line reason."}]
        try:
            rt = RunTools()
            rt.bind_state(state, None)
            v = agentic_struct(client, CompositeTools([rt]), msgs, _NoveltyVerdict,
                               loop_opts={"max_turns": 12})
        except Exception:  # noqa: BLE001
            return idea
        if not (v and getattr(v, "is_duplicate", False)
                and isinstance(v.near_node_id, int) and v.near_node_id in state.nodes):
            return idea
        dup = state.nodes[v.near_node_id]
        outcome = (f"it FAILED ({dup.error_reason})" if dup.status is NodeStatus.failed
                   else f"it scored {dup.metric}")
        self.store.append(EV_NOVELTY_REJECTED, {
            "node_id": len(state.nodes), "near_node": dup.id, "kind": "llm",
            "reason": str(v.reason)[:200], "stance": self._novelty_stance,
            "action": "reproposed" if callable(repropose) else "kept"})
        if callable(repropose):
            hint = (f"\nNOVELTY GATE (LLM): your proposal near-duplicates experiment #{dup.id} — "
                    f"{outcome} ({str(v.reason)[:160]}). Propose something MEANINGFULLY DIFFERENT "
                    "(another approach, component or direction), not a rewording.")
            prev = getattr(self.researcher, "_novelty_feedback", "")
            setattr(self.researcher, "_novelty_feedback", hint)
            try:
                idea2 = repropose()
                if idea2 is not None:
                    idea = idea2
            except BudgetExceeded:
                raise
            except Exception:  # noqa: BLE001
                pass
            finally:
                setattr(self.researcher, "_novelty_feedback", prev)
        return idea

    def _apply_novelty_gate(self, state: RunState, idea: Idea, repropose=None) -> Idea:
        """E1+T5: novelty/dedup gate over fresh proposals, BEFORE any compute is spent.
        Two layers:
        (1) SEMANTIC (T5, ShinkaEvolve `novelty rejection before evaluation`): if the idea TEXT is a
            near-duplicate of an existing node's, reject it — and when a `repropose` callable is
            given, ask the Researcher ONCE more with the duplicate (and its outcome, especially a
            FAILURE) surfaced, so the search learns "you already tried X, it scored Y because Z"
            instead of paying another eval for the same idea.
        (2) NUMERIC (E1 legacy): params within `novelty_epsilon` (normalized L2) of an existing
            node are deterministically nudged off the duplicate.
        Loop-safe (always returns a usable idea) and replay-safe (the final idea lands in
        node_created; the gate is not re-run on replay). Runs when `novelty_gate` is on OR the
        Strategist's novelty stance is "explore" (slice 5): the stance can engage a soft dedup +
        one informed re-propose even when the static gate is off, so novelty pressure follows the
        meta-controller. "balanced"/"exploit" (and gate off) leave this a no-op — exactly as before."""
        mode = getattr(self, "_novelty_mode", "llm")
        # "llm" -> an LLM adjudicates duplication by READING the real experiments (not an embedding/
        # distance heuristic), then re-proposes if it's a dup.
        if mode == "llm":
            return self._llm_novelty_gate(state, idea, repropose)
        # The deterministic "algo" gate below runs when mode is "algo" OR the Strategist's novelty stance
        # is "explore" (the stance can engage a cheap soft dedup + one informed re-propose even when the
        # mode is otherwise off). "off" without explore leaves this a no-op — the Researcher's own
        # read-the-history judgment stands.
        if not (mode == "algo" or self._novelty_stance == "explore"):
            return idea
        import random as _random

        from looplab.events.digest import param_distance

        if self._novelty_semantic:
            dup, sim = self._semantic_duplicate(state, idea)
            if dup is not None:
                outcome = (f"it FAILED ({dup.error_reason}: {(dup.error or '')[:80]})"
                           if dup.status is NodeStatus.failed
                           else f"it scored {dup.metric}")
                self.store.append(EV_NOVELTY_REJECTED, {
                    "node_id": len(state.nodes), "near_node": dup.id, "kind": "semantic",
                    "similarity": round(sim, 4), "stance": self._novelty_stance,
                    "action": "reproposed" if callable(repropose) else "kept"})
                if callable(repropose):
                    hint = (f"\nNOVELTY GATE: your proposal is a near-duplicate of experiment "
                            f"#{dup.id} ('{self._idea_text(dup.idea)[:160]}') — {outcome}. "
                            "Propose something MEANINGFULLY DIFFERENT (another approach, "
                            "component or direction), not a rewording.")
                    prev = getattr(self.researcher, "_novelty_feedback", "")
                    setattr(self.researcher, "_novelty_feedback", hint)
                    try:
                        idea2 = repropose()
                        if idea2 is not None:
                            idea = idea2
                    except BudgetExceeded:      # the hard budget stop must end the run, not be swallowed
                        raise
                    except Exception:  # noqa: BLE001 — a repropose failure keeps the original idea
                        pass
                    finally:
                        # ALWAYS restore, even if repropose() raised: otherwise this transient
                        # "you are duplicating #N" directive leaks into EVERY later proposal in the
                        # run — including drafts in unrelated regions — permanently mis-steering the
                        # researcher away from a direction the operator never banned.
                        setattr(self.researcher, "_novelty_feedback", prev)

        params = {k: float(v) for k, v in idea.params.items() if isinstance(v, (int, float))}
        if not params:
            return idea

        nearest, mind = None, float("inf")
        for n in state.nodes.values():
            d = param_distance(params, n.idea.params)
            if d < mind:
                mind, nearest = d, n.id
        if mind >= self._novelty_epsilon:
            return idea
        nid = len(state.nodes)
        rng = _random.Random(nid * 1009 + 7)        # deterministic per node-slot
        nudged = dict(idea.params)
        for k in params:
            scale = max(abs(params[k]), 1.0) * 0.1
            nudged[k] = round(params[k] + rng.uniform(-1.0, 1.0) * scale, 4)
        self.store.append(EV_NOVELTY_REJECTED, {
            "node_id": nid, "near_node": nearest, "distance": round(mind, 4),
            "stance": self._novelty_stance,
            "original": idea.params, "nudged": nudged})
        out = idea.model_copy()
        out.params = nudged
        out.rationale = (idea.rationale + " [novelty-gate: nudged off a near-duplicate]").strip()
        return out

    # ------------------------------------------------------------- node creation
    def _ensemble_idea(self, parents) -> Idea:
        """A0b: an ensembling/recombination merge — instruct the Developer to combine the parents'
        solutions (stack/average predictions) rather than mean-averaging params. Carries the mean
        params as a safe payload so a Toy/baseline Developer degrades to the legacy mean-merge."""
        base = merge_idea(parents)
        descr = "; ".join(
            f"node {p.id} (metric={p.metric}, params={p.idea.params})"
            + (f": {p.idea.rationale[:120]}" if p.idea.rationale else "")
            for p in parents)
        base.rationale = ("Ensemble/recombine the top solutions into one stronger pipeline "
                          "(e.g. average or stack their predictions, or merge their best components). "
                          f"Parents — {descr}.")
        return base

    def _agent_next_actions(self, state: RunState) -> list[dict]:
        """Self-driving action selection (Step 5). The unified agent picks the next macro action
        from the pure legal-action gate; forced phases (evaluate-pending / budget / seed) give it
        no discretion. Records an audit-only `agent_decision` (never read by best-selection); the
        chosen action then flows through the SAME bucket logic as the policy path. Falls back to the
        policy's own recommendation on any malformed/abstaining choice — the agent can never escape
        `legal`, so 'follow the right pipeline' is a structural invariant, not prompt obedience."""
        from looplab.search.policy import legal_actions
        # Honor a live node-budget extension (set on self.policy.max_nodes in the run loop) so the
        # agent path and the pure-policy path agree on when the search is allowed to keep going.
        legal = legal_actions(state, self.policy, max_nodes=self.policy.max_nodes)
        if len(legal) <= 1:
            return legal                       # finish ([]), forced evaluate/seed, or single option
        if {a["kind"] for a in legal} == {"evaluate"}:
            return legal                       # forced: evaluate all pending, no discretion
        recommended = next(iter(self.policy.next_actions(state)), None)
        chooser = getattr(self.researcher, "choose_action", None)
        if not callable(chooser):              # defensive: agent_drives_actions implies unified
            return self.policy.next_actions(state)
        from looplab.agents.roles import _state_brief
        try:
            brief = _state_brief(state, None)
        except Exception:  # noqa: BLE001 - a brief is advisory; never block on it
            brief = ""
        choice = chooser(state, legal, recommended, brief=brief)
        idx = choice.get("index", -1) if isinstance(choice, dict) else -1
        chosen = legal[idx] if isinstance(idx, int) and 0 <= idx < len(legal) else \
            (recommended if recommended is not None else legal[0])

        def _summ(a: Optional[dict]) -> Optional[dict]:
            if not a:
                return None
            return {"kind": a.get("kind"), "parent_id": a.get("parent_id"),
                    "parent_ids": a.get("parent_ids"), "node_id": a.get("node_id")}

        self.store.append(EV_AGENT_DECISION, {
            "at_node": len(state.nodes),
            "chosen": _summ(chosen),
            "legal": [_summ(a) for a in legal],
            "recommended": _summ(recommended),
            "rationale": (choice.get("rationale", "") if isinstance(choice, dict) else "")[:500],
        })
        return [chosen]

    def _triage_crash(self, state: RunState, node, error: str, attempt: int,
                      reason: str = "crash") -> dict:
        """Decide what to do with a just-failed node BEFORE spending another eval:
        {"action": "repair"|"abandon"|"reject_idea", "rationale": str}. Base mode: the unified
        agent decides (it can consult the run via its pilot tools — read_code / find_analogous —
        to judge whether nearby configs also fail, i.e. whether the IDEA is wrong vs the code).
        Falls back to a deterministic rule when no LLM triage agent is wired (unified_agent off),
        which never rejects an idea — so the feature is safe without an agent.

        `reason` (crash|timeout) is surfaced to both paths so a timeout is triaged as "too slow ->
        reduce compute" rather than mis-read as a wrong idea (a missing KNOWN lib never reaches here
        — env-prep installs it and re-runs first)."""
        # Tag the failure kind so the LLM agent (and the rule's marker scan) see crash vs timeout.
        tagged = f"[failure kind: {reason}]\n{error}"
        fn = getattr(self.researcher, "triage_crash", None)
        if callable(fn):
            try:
                from looplab.agents.roles import _state_brief
                try:
                    brief = _state_brief(state, None)
                except Exception:  # noqa: BLE001 - a brief is advisory; never block on it
                    brief = ""
                out = fn(node, tagged, attempt, state=state, brief=brief)
                if isinstance(out, dict) and out.get("action") in ("repair", "abandon", "reject_idea"):
                    return {"action": out["action"], "rationale": str(out.get("rationale", ""))[:300]}
            except BudgetExceeded:      # the hard budget stop must propagate, not degrade to the rule
                raise
            except Exception:  # noqa: BLE001 - agent triage is best-effort; fall through to the rule
                pass
        # 0 = unlimited attempts -> pass a large cap so the rule path keeps repairing mechanical
        # crashes (the anti-stuck guard, not a count, stops a genuinely stuck node).
        cap = self._inline_repair_attempts or 10**9
        return _rule_triage(reason, error, attempt, cap)

    def _repair_error_context(self, reason: str, error: str,
                              state: Optional[RunState] = None, node=None) -> str:
        """Error context handed to Developer.repair(). A timeout gets an explicit cost-reduction
        directive (the code was too slow, not wrong — shrink it to fit the budget). With deep_repair
        (C3) a crash is enriched with the failure taxonomy + a 'reproduce then fix' directive; else
        the raw tail. Shared by the inter-node debug operator and the inline (in-node) repair loop.

        M1/A0c: when `state`+`node` are given, the ANCESTRAL REPAIR CHAIN of the lineage is
        prepended (aira-dojo MEM_OPS `ancestral`) — prior fixes and what they hit — so a repair
        doesn't oscillate undo↔redo with an earlier one."""
        chain = ""
        if state is not None and node is not None:
            from looplab.events.digest import ancestral_repair_chain
            chain = ancestral_repair_chain(state, node)
            if chain:
                chain += "\n\n"
        error = chain + (error or "")
        if reason == "timeout":
            # Don't quote a specific budget here: the wall-clock varies by node kind (a sweep node gets
            # timeout×sweep_timeout_mult; a RepoTask uses its own per-profile timeout), so a hardcoded
            # self.timeout would be misleading. The directive — cut compute — is what matters.
            return ("[failure kind: timeout]\n" + error + "\n"
                    "The script exceeded its evaluation time budget and was killed before it produced a "
                    "metric. The IDEA is fine — it was just too slow. Return a corrected, complete script "
                    "that finishes WELL within the budget by reducing compute: fewer estimators/boosting "
                    "rounds, fewer epochs, fewer CV folds or seeds, early stopping, a smaller/lighter "
                    "model, capped n_jobs, or a subsample — keep the approach, cut the cost.")
        if reason == "oom":
            # The OOM-kill usually leaves NO Python traceback (the kernel SIGKILLs the process — that's
            # how _failure_reason recognised it), so a "diagnose the root cause" directive has nothing
            # to read. Give the actionable memory-reduction directive instead, mirroring the timeout one.
            return ("[failure kind: oom]\n" + error + "\n"
                    "The script was KILLED by the out-of-memory killer — it exceeded the available "
                    "RAM/VRAM (e.g. a JupyterHub pod's cgroup memory limit) before producing a metric, "
                    "typically with no Python traceback. The IDEA is fine — it was just too "
                    "memory-hungry. Return a corrected, complete script that fits in LESS memory: a "
                    "smaller batch size, a lighter/smaller model, fewer features or a subsample of the "
                    "rows, gradient accumulation instead of one large batch, lower precision "
                    "(float16/bfloat16), or freeing large intermediates — keep the approach, cut the "
                    "memory.")
        if self._deep_repair:
            return (f"[failure kind: {reason or 'unknown'}]\n{error}\n"
                    "Diagnose the root cause; if it's unclear, add a tiny reproduction/"
                    "assert near the failure, then return a corrected, complete script.")
        return error

    def _prepare_env(self, stderr: str) -> list[str]:
        """Environment self-prep: pip-install the KNOWN libraries a crash reports as missing, into
        the eval interpreter, so the engine can re-run instead of rejecting the idea. Returns the
        pip packages successfully installed (empty => nothing to do / install failed -> normal
        triage). Trusted_local only (gated by the caller via `self._auto_install_deps`).

        Per-package so a partial failure only stops the bad name; `_dep_attempted` + `_dep_lock`
        make it install-once-per-module and concurrency-safe (pip mutates one shared env)."""
        from looplab.runtime import deps
        # Parse the missing KNOWN libs BEFORE taking the lock — a crash with nothing to install (the
        # common case, and every non-dep crash) must not block on `_dep_lock` while another eval holds
        # it through a multi-minute pip install (max_parallel>1). Only contend for the lock when there
        # is real installable work.
        candidates = [m for m in deps.missing_modules(stderr) if deps.is_installable(m)]
        if not candidates:
            return []
        with self._dep_lock:
            mods = [m for m in candidates if m not in self._dep_attempted]  # re-check inside the lock
            if not mods:
                return []
            python = getattr(self.sandbox, "python", sys.executable)
            installer = self._dep_installer or deps.install
            installed: list[str] = []
            for mod in mods:
                self._dep_attempted.add(mod)    # one pip attempt per module per run (success or fail)
                pkg = deps.pip_package(mod)
                try:
                    with self.tracer.span("install_dep", package=pkg):
                        res = installer(pkg, python=python, timeout=self._dep_install_timeout)
                except Exception:  # noqa: BLE001 - a misbehaving installer must degrade to "not installed",
                    res = None     # not crash the eval; the node then flows to normal triage/repair.
                if getattr(res, "ok", False):
                    installed.append(pkg)
            return installed

    def _implement(self, idea, parent=None) -> str:
        """Route an implement through `implement_from(idea, parent)` when the Developer supports it
        and a parent exists — so an IMPROVE/REFINE starts from the parent's actual solution (its
        code/files) and patches it, instead of regenerating everything from the pristine baseline
        (which loses the parent's accumulated edits and burns tokens re-deriving them). Falls back
        to the plain `implement(idea)` for developers that don't take a parent (draft, offline)."""
        impl_from = getattr(self.developer, "implement_from", None)
        if parent is not None and callable(impl_from):
            return impl_from(idea, parent)
        return self.developer.implement(idea)

    def _repair(self, node, err: str) -> str:
        """Route a repair through `repair_from(idea, node, error)` when the Developer supports it, so
        the fix is seeded from the FAILING NODE's OWN files — not the shared developer's `last_files`,
        which holds whatever node it built last (a batch builds every node before any eval, so
        `last_files` is almost never the node being repaired). Falls back to `repair(idea, code, err)`."""
        rf = getattr(self.developer, "repair_from", None)
        if callable(rf):
            return rf(node.idea, node, err)
        return self.developer.repair(node.idea, node.code, err)

    def _emit_node_created(self, *, node_id: int, parent_ids: list, operator: str, idea: dict,
                           code: str, files: dict, deleted=_OMIT, research_origin=_OMIT,
                           source=_OMIT, origin=_OMIT) -> None:
        """The single `node_created` emitter for all four creation sites (`_create_node`,
        `_create_injected_node`, `_ablate`, `_ablate_code`). Optional keys default to the
        `_OMIT` sentinel and are LEFT OUT of the payload when not passed — never None-filled —
        so every site emits EXACTLY its historical payload shape (key set AND key order),
        byte-identical event data. Known quirk kept for replay compatibility: the two ablate
        sites emit NO `deleted` key at all (`_create_node` always emits it, `_create_injected_node`
        emits `deleted` + `source` + `origin` but no `research_origin`) — the fold reads every
        optional key with a default, so do not "normalize" the shapes here."""
        data = {"node_id": node_id, "parent_ids": parent_ids, "operator": operator,
                "idea": idea, "code": code, "files": files}
        for k, v in (("deleted", deleted), ("research_origin", research_origin),
                     ("source", source), ("origin", origin)):
            if v is not _OMIT:
                data[k] = v
        self.store.append(EV_NODE_CREATED, data)

    def _create_node(self, action: dict) -> None:
        state = fold(self.store.read_all())
        node_id = max(state.nodes, default=-1) + 1  # monotonic across the whole run -> unique
        kind = action["kind"]
        with self.tracer.span("create_node", new_trace=True, node_id=node_id, operator=kind):
            # Announce the node the INSTANT we start building it — before the Researcher/Developer run —
            # so the UI shows it (and streams its live agent-trace) immediately, not only after the
            # minutes-long dev session ends with node_created. Transient marker (folds to st.building,
            # NOT st.nodes), so node-id allocation + resume are untouched; node_created supersedes it.
            _bparents = (list(action["parent_ids"]) if action.get("parent_ids")
                         else ([action["parent_id"]] if action.get("parent_id") is not None else []))
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
                    code = self.developer.implement(idea)
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
                    code = (_impl_from(idea, pnodes[0])
                            if (self._merge_mode == "ensemble" and _impl_from and pnodes)
                            else self.developer.implement(idea))
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
                        code = self._repair(parent, err)   # seed from parent's OWN files (repair_from)
                else:
                    # Debug/repair is stance-NEUTRAL: novelty pressure ("open a new direction") is
                    # wrong when the job is to FIX a failure. Clear any stale explore/exploit hint a
                    # prior draft/improve left on the researcher before this repair proposal (this
                    # branch does not call _set_complexity_hint, so nothing else refreshes it).
                    self._stamp_novelty_hint(state, "balanced")
                    with self.tracer.span("propose"):
                        idea = self.researcher.propose(state, parent)
                    idea.operator = "debug"
                    with self.tracer.span("implement"):
                        code = self._implement(idea, parent)
            else:  # improve
                parent = state.nodes[action["parent_id"]]
                self._set_complexity_hint(state, parent)   # A0d breadth-keyed complexity cue
                with self.tracer.span("propose"):
                    idea = self.researcher.propose(state, parent)
                # E1+T5 dedup near-duplicate proposals (one informed re-propose on a semantic hit)
                idea = self._apply_novelty_gate(
                    state, idea, repropose=lambda p=parent: self.researcher.propose(state, p))
                idea.operator = "improve"
                parents = [parent.id]
                with self.tracer.span("implement"):
                    code = self._implement(idea, parent)
            # 💡 deep-research provenance: tag the first couple of nodes created right after a research
            # memo (its directions are the active steering) so the UI can show WHERE research landed in
            # the tree. Audit/UI only — never affects search. Coarse-but-honest (temporal proximity).
            research_origin = None
            if state.research:
                _m = state.research[-1]
                _ra = _m.get("at_node")
                if _ra is not None and _ra <= node_id < _ra + 2:
                    research_origin = {"at_node": _ra, "trigger": _m.get("trigger")}
            self._emit_node_created(
                node_id=node_id,
                parent_ids=parents,
                operator=idea.operator,
                idea=idea.model_dump(mode="json"),
                code=code,
                files=getattr(self.developer, "last_files", {}) or {},
                deleted=getattr(self.developer, "last_deleted", []) or [],
                research_origin=research_origin,
            )
            # The Developer session CRASHED when its code is the "(developer error: …)" sentinel (an
            # exception in _run — e.g. an LLM 401/timeout). FAIL the node now: without this it stays
            # pending, and the eval runs the PARENT's carried-over entrypoint and inherits the PARENT's
            # metric — a false success that pollutes the search (the 401-window nodes 50-54 each faked
            # the parent's 0.81 this way). node_created → node_failed keeps the one-terminal invariant.
            if isinstance(code, str) and code.startswith("(developer error:"):
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node_id, "error": code, "reason": "developer_crash",
                    "eval_seconds": 0.0})
                # Circuit-breaker — PAUSE on the FIRST developer_crash. A developer_crash means the
                # Developer couldn't finish THIS node even after the LLM client's own within-call retries
                # (429 / 5xx / throttle-403 all back off + retry): a problem that a NEW node can't fix
                # (LLM unreachable, or a hard error), NOT a bad experiment. One node = one experiment; if
                # it can't be resolved within the node, stop the whole run rather than rapid-fire more
                # dead nodes (the 403 blowout spun 67 of them). Freeze (not finish) so a plain `resume`
                # continues once the cause is resolved — no premature report/lessons.
                self.store.append(EV_PAUSE, {
                    "reason": "auto-paused: a Developer session crashed (LLM unreachable or a hard error, "
                              "unresolved within the node) — resume once it's fixed"})
        self._emit_agent_report(node_id)
        self._emit_hypothesis_ranked(node_id)
        self._emit_foresight_selected(node_id)

    def _rerun_node(self, node: Node, state: RunState) -> None:
        """node_reset "propose"/"implement": re-run this EXISTING node id IN PLACE (never mints a new
        id — the whole point is to FIX a node, not proliferate). "implement" keeps the Researcher's idea
        (only the Developer re-runs — the "researcher ok, developer crashed" case); "propose" re-proposes
        a fresh idea too. Emits node_building + node_created for the SAME id — the fold applies it over the
        reset (clearing the rerun marker), the node goes pending-with-code, and the eval loop scores it
        next. Same developer-crash circuit-breaker as a first build. (An "eval" reset never reaches here —
        the fold left it pending-with-code and the eval dispatch re-scores it directly.)"""
        stage = node.rerun_from
        parents = list(node.parent_ids)
        parent = state.nodes.get(parents[0]) if parents else None
        with self.tracer.span("create_node", new_trace=True, node_id=node.id, operator=node.operator):
            self.store.append(EV_NODE_BUILDING,
                              {"node_id": node.id, "operator": node.operator, "parent_ids": parents})
            # "propose" re-proposes (draft/improve/debug); a merge node has no single proposable idea
            # (it's an ensemble of parents), so a propose-reset there degrades to re-implement.
            if stage == "propose" and node.operator != "merge":
                with self.tracer.span("propose"):
                    idea = self.researcher.propose(state, parent)
                idea.operator = node.operator      # operator stays authoritative (from the original node)
            else:                                   # "implement" (or merge): keep the idea, re-develop
                idea = node.idea
            with self.tracer.span("implement"):
                code = self._implement(idea, parent)
            self._emit_node_created(
                node_id=node.id, parent_ids=parents, operator=idea.operator,
                idea=idea.model_dump(mode="json"), code=code,
                files=getattr(self.developer, "last_files", {}) or {},
                deleted=getattr(self.developer, "last_deleted", []) or [])
            if isinstance(code, str) and code.startswith("(developer error:"):
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node.id, "error": code, "reason": "developer_crash", "eval_seconds": 0.0})
                self.store.append(EV_PAUSE, {
                    "reason": "auto-paused: a Developer session crashed (LLM unreachable or a hard error, "
                              "unresolved within the node) — resume once it's fixed"})
        self._emit_agent_report(node.id)

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
                # Cross-run provenance: a DICT when this inject seeded from a sibling run's
                # experiment (an `import` action), else None. Coerce defensively — a non-dict
                # origin (a hand-authored/API inject that passed a label string) would make the
                # folded Node fail validation and silently vanish, so the inject gate would keep
                # re-creating the SAME node id forever.
                origin=req.get("origin") if isinstance(req.get("origin"), dict) else None,
            )
        if not req.get("code"):
            self._emit_agent_report(node_id)

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

    def _seed_workspace(self, workdir) -> None:
        return self.workspace.seed_workspace(workdir)

    def _seed_repo_tree(self, src, dst, ignore, mode: str = "auto") -> int:
        return self.workspace.seed_repo_tree(src, dst, ignore, mode)

    def _link_input(self, src, dst) -> None:
        return self.workspace.link_input(src, dst)

    # ------------------------------------------------------------- eval dispatch
    def _agent_may(self, role: str, setting: str) -> bool:
        """Governance gate (Settings.agent_control): may `role` (strategist|boss|researcher) change
        `setting` at runtime? A setting absent from the map is LOCKED for everyone. Pure + cheap —
        called at each agent seam so the matrix is the single source of truth."""
        return role in (self._agent_control.get(setting) or ())

    def _ensure_run_setup(self) -> None:
        """Run the eval's RUN-LEVEL `run_setup` exactly ONCE, before the first eval — e.g. a one-time
        dependency install into the shared interpreter (the autonomy default when deps are stable
        across experiments). Distinct from per-node `setup`, which reinstalls before EVERY eval. Runs
        in the first editable repo's SOURCE dir so `-r requirements.txt` resolves; output streams to
        `run_setup.log`. A non-zero/timed-out run_setup ABORTS the run (the env would be unusable).
        Only in trusted_local (an untrusted/docker eval is a fresh container — use per-node `setup`).
        No-op when `run_setup` is unset. The guard is set BEFORE running so a crash can't retry-loop."""
        if self._run_setup_done:
            return
        # Serialize the check-then-set: parallel eval worker threads would otherwise all see
        # _run_setup_done == False and launch pip (not concurrency-safe) N times into one interpreter.
        with self._run_setup_lock:
            if self._run_setup_done:
                return
            cmd = list((self._eval_spec or {}).get("run_setup") or [])
            if not cmd or self.trust_mode != "trusted_local":
                self._run_setup_done = True
                return
            self._run_setup_done = True
            self._do_run_setup(cmd)

    def _do_run_setup(self, cmd: list) -> None:
        from looplab.runtime.sandbox import _run_argv
        eds = (self._repo_spec or {}).get("editables", [])
        cwd = eds[0]["path"] if eds else str(self.run_dir)
        to = float((self._eval_spec or {}).get("run_setup_timeout", 1800.0))
        self.store.append(EV_RUN_SETUP_STARTED, {"command": cmd, "cwd": cwd})
        log = str(Path(self.run_dir) / "run_setup.log")
        rc, out, err, timed = _run_argv(cmd, cwd, to, log_path=log)
        self.store.append(EV_RUN_SETUP_FINISHED,
                          {"exit_code": rc, "timed_out": timed, "stderr_tail": (err or "")[-2000:]})
        if rc != 0 or timed:
            raise RuntimeError(f"run_setup failed (exit={rc}, timed_out={timed}); see {log}\n"
                               + (err or out or "")[-500:])

    def _sandbox_cwd(self, workdir, cwd_spec) -> str:
        # extracted to engine/workspace.py — see the delegator block after __init__
        return self.workspace.sandbox_cwd(workdir, cwd_spec)

    def _resolve_stages(self, workdir, es):
        """Multi-stage eval manifest (Phase 1): the Developer's `looplab_stages.json` in the workdir wins,
        else the task's recommended default (`es["stages"]`). Shape: {"stages": [{name, command, timeout?,
        check?}, …]} (or a bare list). The LAST stage is the metric stage — its stdout is read for the
        metric. Returns None (classic single-command eval) when no valid manifest is present."""
        import json
        stages = None
        mf = Path(workdir) / "looplab_stages.json"
        if mf.exists():
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
                stages = data.get("stages") if isinstance(data, dict) else data
            except Exception:  # noqa: BLE001 — a malformed manifest just falls back to the single command
                stages = None
        if not stages:
            stages = es.get("stages")
        if not isinstance(stages, list) or not stages:
            return None
        clean = [s for s in stages if isinstance(s, dict)
                 and isinstance(s.get("command"), list) and s.get("command")]
        return clean or None

    def _run_eval(self, node, workdir, env=None, profile=None, cancel=None):
        """Eval dispatcher: RepoTask runs the operator's command + reads its metric;
        otherwise the classic solution.py sandbox path. Both return a `RunResult`, so all
        downstream metric/exit/timeout checks are identical.

        Phase 2: the command is built with an eval profile (smoke/full — `profile` arg, else
        the Researcher's `idea.eval_profile`) and, when params_style=cli_overrides, the
        node's params as `key=value` overrides."""
        if self._eval_spec:
            from looplab.runtime import command_eval
            es = self._eval_spec
            self._ensure_run_setup()             # one-time run-level dep install (before the first eval)
            prof = profile or (node.idea.eval_profile if node is not None else None)
            # A7 Strategist fidelity override: when the active strategy pins smoke/full and the node
            # didn't request a profile, use the strategy's. An explicit `profile` arg (confirm=full)
            # always wins. "adaptive" leaves _strategy_fidelity None => the Idea's own profile.
            if prof is None and self._strategy_fidelity in ("smoke", "full"):
                prof = self._strategy_fidelity
            params = node.idea.params if node is not None else {}
            cmd, timeout = command_eval.build_command(es, params, prof)
            root = str(Path(workdir).resolve())               # repo/workdir root
            stages = self._resolve_stages(root, es)           # multi-stage pipeline (Developer file / task default)
            cwd = self._sandbox_cwd(workdir, es.get("cwd", "."))
            # untrusted tier (Phase 4): sandbox the eval in docker, mounting the workspace
            # root so the cwd subdir + host metric reading line up. Fails loudly w/o docker.
            wrap = (command_eval.make_docker_wrap(
                        root, self.docker_image,
                        runtime=("runsc" if self.trust_mode == "hostile" else None))
                    if self.trust_mode in ("untrusted", "hostile") else None)
            res = command_eval.run_command_eval(
                cmd, cwd, timeout, es["metric"], env,
                setup=es.get("setup") or None, setup_timeout=es.get("setup_timeout", 600.0),
                setup_cwd=root,                               # deps install at the repo root
                cross_check=es.get("cross_check"),            # Phase 4 drift cross-check …
                drift_tolerance=float(es.get("drift_tolerance", 1e-6)),
                enforce_drift=(self.eval_trust_mode == "ratify_freeze_drift"),
                wrap=wrap,
                metrics=es.get("metrics") or None,            # #5 multi-objective …
                constraints=es.get("constraints") or None,
                tracer=self.tracer,                           # child spans: setup/command/read
                cancel=cancel,                                # operator mid-eval node_abort
                log_dir=root,                                 # live setup.log/eval.log in the node workdir
                stages=stages)                                # multi-stage pipeline (Phase 1); None = single command
        else:
            # Intra-node sweep nodes run a whole grid in one process, so they need ~N× the
            # single-eval budget. `sweep_timeout_mult` scales the wall-clock for sweep nodes only;
            # _kill_tree + the mid-eval cancel watcher still bound a runaway. (The RepoTask path
            # gets its per-profile timeout from build_command above.)
            timeout = self.timeout
            if node is not None and node.idea.is_sweep:
                timeout = self.timeout * self.sweep_timeout_mult
            # Researcher-sized per-node budget (e.g. a neural-net / large-ensemble idea that needs longer
            # than the run default) — honored ONLY when the governance matrix grants the researcher the
            # `timeout` setting; otherwise the run-wide budget stands. This is the "auto" per-node mode.
            etv = getattr(node.idea, "eval_timeout", None) if node is not None else None
            if etv and etv > 0 and self._agent_may("researcher", "timeout"):
                timeout = float(etv)
            res = self.sandbox.run(node.code, str(workdir), timeout, env, cancel=cancel)
        # Intra-node sweep: if the solution reported a grid of trials, collapse them into the node's
        # scalar `metric` (the best feasible trial under the task direction) so fold/best-selection/
        # improve are untouched. Done BEFORE host grading so a host grader still has the final say on
        # the best trial's predictions file. The full trial list rides along on `res.trials`.
        if res.trials:
            self._apply_sweep_best(res)
        # Out-of-process host-side grading (general): override the (ignored) self-reported metric with
        # the HOST's score of the candidate's predictions. Applied for BOTH the command-eval and the
        # sandbox path, so a task that exposes host_grader() is always host-scored — and so EVERY
        # sandbox-path eval (normal AND the multi-seed confirm pass, both call _run_eval) is graded
        # the same way. host_grader takes precedence: its score replaces any self-reported metric.
        if self._host_grader is not None:
            res = self._apply_host_grade(res, workdir)
        return res

    def _apply_sweep_best(self, res):
        """Collapse an intra-node sweep's `res.trials` into the node's scalar `metric`: pick the
        best trial that produced a usable (finite) metric, under the task direction. Keeping
        `metric` a single number means fold, best-selection, confirm and `improve` treat a sweep
        node like any other; the trials are audit/UI only. No usable trial -> no metric (the node
        fails like an empty run, so a sweep where every config crashed can't pass)."""
        from looplab.runtime.sandbox import _to_float
        scored = [(t, _to_float(t.get("metric"))) for t in (res.trials or [])]
        scored = [(t, m) for t, m in scored if m is not None]
        if not scored:
            res.metric = None
            return
        chooser = min if self.task.direction == "min" else max
        best_t, best_m = chooser(scored, key=lambda tm: tm[1])
        res.metric = best_m
        extra = best_t.get("extra_metrics") or {}
        if extra:
            res.extra_metrics = {**(res.extra_metrics or {}), **extra}

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

    def _build_holdout_idx(self, fraction: float) -> frozenset:
        return self.holdout.build_holdout_idx(fraction)

    def _holdout_topk(self, state: RunState) -> list[int]:
        return self.holdout.holdout_topk(state)

    def _holdout_pending(self, state: RunState) -> bool:
        return self.holdout.holdout_pending(state)

    async def _holdout_phase(self, state: RunState) -> None:
        return await self.holdout.holdout_phase(state)

    def _emit_agent_report(self, node_id: int) -> None:
        """External-agent audit (ADR-7): if the Developer validated its output (a
        `ValidatingDeveloper`), record the verdict as an `agent_validated` event so each
        node carries a trail of how the external coding agent performed. No-op for
        plain developers (no `last_report`).

        Safe because node *creation* (`_create_node` / `_ablate`) is awaited sequentially
        in the main loop and never inside the parallel `evals` task group, so the shared
        `developer.last_report` set just above always belongs to `node_id`."""
        report = getattr(self.developer, "last_report", None)
        if report is not None:
            data = {"node_id": node_id, **report.summary()}
            extra = getattr(self.developer, "audit_extra", None)
            if callable(extra):
                data.update(extra())
            self.store.append(EV_AGENT_VALIDATED, data)

    def _emit_role_telemetry(self, role, attr: str, event_type: str, node_id: int) -> None:
        """Append `event_type` from a role's predictive-telemetry attr (a dict set during
        propose/implement), stamped with `node_id`, then CONSUME it (reset to None). Like
        `_emit_agent_report` this relies on sequential node creation for correctness; the consume adds
        a further guard specific to these predictive channels — a following non-propose action (merge /
        debug-repair, which never re-predicts) then finds None and can't re-emit a stale pick for the
        wrong node. No-op when the attr is absent/None (the role didn't predict for this node)."""
        pick = getattr(role, attr, None)
        if isinstance(pick, dict):
            pick = dict(pick)   # copy before consuming; strip the captured op-trace ids out of the data
            tid, sid = pick.pop("_trace_id", None), pick.pop("_span_id", None)
            # The ranking LLM ran DURING propose in its own named span (captured there); stamp the event
            # with THAT trace so the UI scopes it to just the ranking, not the whole node.
            self.store.append(event_type, {"node_id": node_id, **pick}, trace_id=tid, span_id=sid)
            setattr(role, attr, None)

    def _emit_hypothesis_ranked(self, node_id: int) -> None:
        """FOREAGENT board prioritization audit: if the active Researcher (a `ForesightPanelResearcher`)
        predicted an order over the OPEN-hypothesis board while proposing THIS node, record it as a
        `hypothesis_ranked` event — the analysis + selection trace the UI surfaces (kanban order + the
        model's `reason`)."""
        self._emit_role_telemetry(self.researcher, "last_hyp_priority", EV_HYPOTHESIS_RANKED, node_id)

    def _emit_foresight_selected(self, node_id: int) -> None:
        """FOREAGENT predict-before-execute audit: when the world model picked WHICH candidate becomes
        this node — the best of K generated ideas (the researcher panel) or of N code implementations
        (best-of-N) — record the ranking + confidence + the model's reasoning as a `foresight_selected`
        event. Without it the choice and its discarded alternatives vanish (only the winner survives in
        `node_created`)."""
        self._emit_role_telemetry(self.developer, "last_foresight_pick", EV_FORESIGHT_SELECTED, node_id)
        self._emit_role_telemetry(self.researcher, "last_foresight", EV_FORESIGHT_SELECTED, node_id)

    @property
    def _probe_developer(self):
        """Developer used for ablation *probes* (I7): the raw inner developer, bypassing
        any ValidatingDeveloper's retry/fallback. Probes are a measurement harness, not a
        shipped step — routing them through validation would (a) substitute the LLM
        fallback mid-measurement, corrupting impact numbers, and (b) multiply expensive
        external-agent calls by len(params) per ablation (ADR-7 cost rule)."""
        return getattr(self.developer, "inner", self.developer)

    async def _evaluate(self, node_id: int, limiter: anyio.CapacityLimiter) -> None:
        async with limiter:
          with self.tracer.span("evaluate", new_trace=True, node_id=node_id) as sp:
            state = fold(self.store.read_all())
            node = state.nodes[node_id]
            sp.set("operator", node.operator)
            # A6 proxy/predictive scoring: cheaply predict this candidate's metric from the observed
            # history and skip a full eval for the doomed bottom fraction (cost lever). Deterministic
            # + replay-safe: the skip is recorded as node_failed reason="proxy_skipped" and a
            # proxy_scored audit event. OFF by default (kill_fraction=0 -> never skips).
            if self.proxy_scorer is not None and self.proxy_kill_fraction > 0:
                pred = self.proxy_scorer.score(state, node)
                if pred is not None:
                    skip = self.proxy_scorer.should_skip(state, node, pred)
                    sp.set_many(proxy_score=round(pred, 6), proxy_skipped=skip)
                    async with self._write_lock:
                        self.store.append(EV_PROXY_SCORED,
                                          {"node_id": node_id, "score": round(pred, 6), "skipped": skip})
                        if skip:
                            self.store.append(EV_NODE_FAILED, {
                                "node_id": node_id,
                                "error": "skipped by proxy scorer (predicted in the doomed bottom fraction)",
                                "reason": "proxy_skipped", "eval_seconds": 0.0})
                            self._maybe_crash()
                    if skip:
                        return
            workdir = self.run_dir / "nodes" / f"node_{node_id}"
            self._materialize(node, workdir)        # seed tree -> node edits -> task assets
            # Hybrid crash repair: each attempt runs the eval (with the mid-eval abort watcher) and,
            # if it CRASHES, the agent triages it and may repair the code IN PLACE and re-run — all
            # within this one node (no new tree node, no max_nodes spent). At most
            # `inline_repair_attempts` repairs; then the node fails normally and stays eligible for the
            # budgeted inter-node debug operator. Exactly ONE terminal event (node_evaluated/node_failed)
            # is emitted at the end so first_terminal budget accounting and resume re-entry are intact;
            # only NON-terminal `node_repaired` events are written mid-loop.
            import threading
            attempt = 0
            dep_rounds = 0                   # env-prep auto-install + re-run rounds (separate from repair attempts)
            total_eval = 0.0                 # summed subprocess wall-clock across all attempts (cost)
            triage_outcome = None            # ("abandon"|"reject_idea", rationale) for the terminal event
            err = ""
            reason = "crash"
            stuck_sig = None; stuck_n = 0    # anti-stuck: consecutive identical-error signatures
            while True:
                _t0 = time.time()
                # Mid-eval per-node intervention (v2): a watcher polls the log while the eval runs in a
                # worker thread; if the operator appends `node_abort` for THIS node, it sets the cancel
                # Event, which tree-kills the in-flight subprocess (sandbox._run_argv). v1's pre-eval
                # skip only catches not-yet-started nodes — this kills a running one.
                cancel = threading.Event()
                aborted = False
                async with anyio.create_task_group() as _tg:
                    def _abort_seen() -> bool:   # cached incremental read — no full re-parse each tick
                        for e in self.store.read_all():
                            if e.type == EV_NODE_ABORT and e.data.get("node_id") == node_id:
                                return True
                        return False
                    async def _watch():
                        nonlocal aborted
                        while True:
                            await anyio.sleep(0.3)
                            if cancel.is_set():
                                return
                            if await anyio.to_thread.run_sync(_abort_seen):
                                aborted = True
                                cancel.set()
                                return
                    _tg.start_soon(_watch)
                    res = await anyio.to_thread.run_sync(
                        self._run_eval, node, str(workdir), None, None, cancel
                    )
                    cancel.set()                  # eval finished on its own …
                    _tg.cancel_scope.cancel()     # … stop the watcher now (no poll-interval latency)
                total_eval = round(total_eval + (time.time() - _t0), 3)   # cumulative eval cost (#2)
                ok = res.metric is not None and res.exit_code == 0 and not res.timed_out
                if aborted and not ok:                       # killed mid-eval by the operator (and the
                    async with self._write_lock:             # eval didn't already finish cleanly first)
                        self.store.append(EV_NODE_FAILED, {
                            "node_id": node_id, "error": "aborted by operator (killed mid-eval)",
                            "reason": "aborted", "eval_seconds": total_eval})
                        self._maybe_crash()
                    return
                if ok:
                    break
                reason = _failure_reason(res)
                # A clean run (exit 0) with no parseable metric is the most confusing failure for the
                # repair agent — the terse "no_metric" gave it nothing to fix, so the debug node just
                # re-ran and failed again. Tell it EXACTLY what the eval reads (the configured metric
                # key + the one line it must print), so a no-metric node can actually be repaired.
                _ms = (self._eval_spec.get("metric") or {}) if isinstance(self._eval_spec, dict) else {}
                _mk = _ms.get("key", "metric")
                _no_metric_hint = (
                    f" — the command ran cleanly (exit 0) but printed NO parseable metric. The eval reads"
                    f" a stdout JSON line for key {_mk!r}; the entrypoint MUST print exactly one line like"
                    f" print(json.dumps({{{_mk!r}: <float>}})) as its last stdout."
                    if _ms.get("kind", "stdout_json") == "stdout_json"
                    else " — ran cleanly but produced no parseable metric (check the eval's metric reader).")
                err = self._redact(res.stderr[-500:]) or (
                    f"metric drift: {res.drift}" if res.drift is not None else
                    f"exit={res.exit_code} timed_out={res.timed_out} no_metric{_no_metric_hint}"
                )
                # Environment self-prep (deps.py): a crash that is purely a missing KNOWN library is
                # not a bad idea — install it (trusted_local only) and re-run BEFORE the crash-triage
                # agent can reject the idea. This is what lets torch/XGBoost/CatBoost (e.g. a GRU
                # model) run on a fresh box instead of dying as `idea_rejected`. Bounded by
                # _MAX_DEP_ROUNDS + the `_dep_failed` cache; does NOT consume a repair attempt (env
                # prep is not a code fix), and the unchanged node is simply re-evaluated.
                if (self._auto_install_deps and reason == "crash" and dep_rounds < _MAX_DEP_ROUNDS):
                    installed = await anyio.to_thread.run_sync(self._prepare_env, res.stderr)
                    if installed:
                        dep_rounds += 1
                        async with self._write_lock:
                            self.store.append(EV_DEPS_INSTALLED, {
                                "node_id": node_id, "packages": installed, "round": dep_rounds})
                        continue   # re-run now that the library is present (no repair attempt spent)
                # Anti-stuck: when the SAME error recurs with no progress, stop (even under unlimited
                # repair) so the agent doesn't loop forever on an unfixable failure.
                # T10: NORMALIZED signature — the same semantic error with different line numbers /
                # sizes / paths counts as "stuck" too (exact-match compare missed those loops).
                _sig = _normalize_error_sig(err)
                stuck_n = (stuck_n + 1) if _sig and _sig == stuck_sig else 1
                stuck_sig = _sig
                # Inline-repair gate: feature on, repairable reason, a Developer that can repair, and
                # something to repair (whole-file code, multi-file edits, or a repo). The attempt CAP is
                # skipped when unlimited (_inline_repair_attempts == 0); the anti-stuck guard bounds it.
                if (not self._inline_repair
                        or reason not in self._inline_repair_reasons
                        or (self._inline_repair_attempts and attempt >= self._inline_repair_attempts)
                        or stuck_n >= self._inline_repair_stuck_repeat
                        or not callable(getattr(self.developer, "repair", None))
                        or not (node.code or node.files or self._repo_spec)):
                    if stuck_n >= self._inline_repair_stuck_repeat and self._inline_repair:
                        triage_outcome = ("abandon", f"same error repeated {stuck_n}x — stuck, abandoning")
                    break
                triage = self._triage_crash(state, node, err, attempt + 1, reason=reason)
                action = triage.get("action", "repair")
                if action == "abandon":
                    triage_outcome = ("abandon", triage.get("rationale", ""))
                    break
                if action == "reject_idea":   # the idea itself is wrong -> mark the lineage; steer to a new idea
                    reason = "idea_rejected"
                    triage_outcome = ("reject_idea", triage.get("rationale", ""))
                    break
                # action == "repair": fix the code in place and re-eval (no new node, no budget spent).
                with self.tracer.span("inline_repair", node_id=node_id, attempt=attempt + 1):
                    new_code = self._repair(
                        node, self._repair_error_context(reason, err, state=state, node=node))
                # Snapshot the developer's per-call audit state IMMEDIATELY, before any `await`: under
                # max_parallel>1 the developer instance is SHARED across concurrent _evaluate tasks,
                # and `async with self._write_lock` below is a checkpoint — a sibling task's repair()
                # would overwrite `developer.last_files` in the gap, so reading it after the lock would
                # record (and re-materialize) ANOTHER node's edits as this node's. Capture now.
                repaired_files = dict(getattr(self.developer, "last_files", {}) or {})
                repaired_deleted = list(getattr(self.developer, "last_deleted", []) or [])
                attempt += 1
                async with self._write_lock:
                    self.store.append(EV_NODE_REPAIRED, {
                        "node_id": node_id, "attempt": attempt, "code": new_code,
                        "files": repaired_files,
                        "deleted": repaired_deleted,
                        "error_in": err, "triage_action": "repair",
                        "rationale": str(triage.get("rationale", ""))[:300]})
                node = fold(self.store.read_all()).nodes[node_id]   # node.code now == repaired code
                self._write_node_files(node, workdir)               # re-materialize before re-eval
                # loop -> re-run the eval with the corrected code
            sp.set_many(eval_seconds=total_eval, exit_code=res.exit_code, timed_out=res.timed_out,
                        metric=res.metric, ok=ok, repair_attempts=attempt)
            if res.violations:
                sp.set("violations", len(res.violations))
            if res.drift is not None:
                sp.set("drift", True)
            async with self._write_lock:
                # Multi-stage pipeline (Phase 1): record each stage's pass/fail BEFORE the terminal so the
                # fold + trace show data_prep ✓ / train ✓ / eval ✗, and a later stage-scoped re-run knows
                # which stages already passed. Empty on the classic single-command eval.
                for _st in (res.stages or []):
                    self.store.append(EV_STAGE_FINISHED, {"node_id": node_id, **_st})
                if res.drift is not None:               # Phase 4: uncorroborated metric (audit)
                    self.store.append(EV_SPEC_DRIFT, {"node_id": node_id, **res.drift})
                if ok:
                    self.store.append(
                        EV_NODE_EVALUATED,
                        {"node_id": node_id, "metric": res.metric,
                         "stdout_tail": self._redact(res.stdout[-500:]), "eval_seconds": total_eval,
                         "extra_metrics": res.extra_metrics or {},   # #5 multi-objective
                         "violations": res.violations or [],
                         # Intra-node sweep: the whole grid's per-trial results, carried on the ONE
                         # node_evaluated event (the sweep is a single atomic eval — eval_seconds is
                         # the whole-sweep wall-clock; per-trial seconds are audit-only). [] normally.
                         "trials": res.trials or []},
                    )
                    # B5 reward-hacking detector + I3 code-leakage scan (audit-only): flag a
                    # suspicious win / leaky pipeline without ever changing selection. Both surface in
                    # the Trust panel via the same reward_hack_suspected event.
                    sigs = []
                    # Scan the WHOLE solution surface, not just solution.py — a patch-gated multi-file
                    # agent can hide answer-key access / leakage / the real computation in an in-surface
                    # helper module that solution.py imports. Concatenate node.files so the reward-hack /
                    # leakage / critic scans cover the imported code too (not only the clean entrypoint).
                    scan_src = node.code + "".join(
                        f"\n\n# --- {fn} ---\n{src}" for fn, src in (node.files or {}).items()
                        if str(fn).replace("\\", "/").lower() != "solution.py")
                    if self.reward_hack_detect:
                        from looplab.trust.reward_hack import detect_reward_hacks
                        protected = set(self._repo_spec.get("protected_names", [])) | set(self._assets)
                        sigs += detect_reward_hacks(scan_src, res.metric, state.direction,
                                                    protected_names=protected, stdout=res.stdout)
                        # 4.3: also apply the hardened exploit ruleset grown by `looplab harden`
                        # (hacker-fixer-solver) — each previously-discovered exploit stays guarded.
                        if self._exploit_suite is not None:
                            sigs += self._exploit_suite.scan(scan_src)
                        # 4.4 sandbox instrumentation (RewardHackingAgents recipe): flag RUNTIME
                        # writes to protected/frozen files — behavioral evidence a static scan of the
                        # code can miss (a write via a helper, os.system, a template). Compares the
                        # workdir against the assets/protected set the engine placed there.
                        if self._workdir_audit:
                            sigs += self._audit_workdir_writes(workdir, protected)
                    if self._code_leakage_detect and scan_src:
                        from looplab.trust.leakage import code_leakage_scan
                        for f in code_leakage_scan(scan_src)["flags"]:
                            sigs.append({"signal": "data_leakage:" + f["signal"],
                                         "detail": f"line {f['line']}: {f['code']}"})
                    if self._critic_check and scan_src:
                        from looplab.trust.critic import critique
                        # Host-graded tasks (MLE-bench &c.) score a submission file out-of-process,
                        # so the critic's in-code `metric` checks don't apply — hand it the expected
                        # submission filename so it checks the right output contract instead.
                        sub_file = self._graded_output_name()
                        for c in critique(node.idea, scan_src, submission_file=sub_file):
                            sigs.append({"signal": "critic:" + c["issue"], "detail": c["detail"]})
                    if sigs:
                        self.store.append(EV_REWARD_HACK_SUSPECTED,
                                          {"node_id": node_id, "signals": sigs})
                else:
                    # `err`/`reason` were computed in the attempt loop (reason may be "idea_rejected"
                    # if the crash-triage agent judged the idea fundamentally wrong).
                    sp.set("error_reason", reason)
                    data = {"node_id": node_id, "error": err, "reason": reason,
                            "eval_seconds": total_eval}
                    if res.failed_stage:                # Phase 1: pinpoint which pipeline stage broke
                        data["failed_stage"] = res.failed_stage
                    if triage_outcome is not None:
                        data["triage_action"], data["triage_rationale"] = (
                            triage_outcome[0], str(triage_outcome[1])[:300])
                    self.store.append(EV_NODE_FAILED, data)
                self._maybe_crash()

    def _audit_workdir_writes(self, workdir, protected: set) -> list[dict]:
        """4.4: after an eval, flag any PROTECTED/frozen file whose on-disk content differs from
        what the engine wrote there (assets/answer keys) — a runtime tamper the static code scan
        can't see. Pure host-side check; audit-only (feeds reward_hack_suspected). Best-effort."""
        sigs: list[dict] = []
        try:
            wd = Path(workdir)
            for name in protected:
                p = wd / name
                if not p.is_file():
                    continue
                original = self._assets.get(name)
                if original is None:
                    continue
                # Compare as TEXT for str assets: `_write_assets` writes them via
                # `Path.write_text` (text mode translates '\n' -> os.linesep), so a raw-BYTES
                # compare would flag EVERY honest eval on a platform where os.linesep != '\n'
                # (e.g. Windows CRLF) as a tamper. Bytes assets compare byte-exact.
                if isinstance(original, str):
                    try:
                        got = p.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        got = None
                    tampered = got is not None and got != original
                else:
                    tampered = p.read_bytes() != bytes(original)
                if tampered:
                    sigs.append({"signal": "protected_write",
                                 "detail": f"protected file '{name}' was modified at runtime"})
        except Exception:  # noqa: BLE001 — an audit failure must never fail the eval
            pass
        return sigs

    # ------------------------------------------------------------------- confirm
    # `_already_confirmed` / `_run_confirm_seed` / `_confirm_phase` / `_confirm_node` live in
    # looplab/engine/confirm_phase.py (ConfirmPhaseMixin — inherited, zero call-site churn).

    # ------------------------------------------------------------------ ablation
    # `_ablate` / `_segment_blocks` / `_comment_block` / `_ablate_code` live in
    # looplab/engine/ablation.py (AblationMixin — inherited, zero call-site churn).

    # ------------------------------------------------------------- trust & audit
    def _redact(self, text: str) -> str:
        """B3: mask secrets in an output tail before it is persisted, when redaction is enabled."""
        if not self._redact_output or not text:
            return text
        from looplab.trust.redact import redact_secrets
        return redact_secrets(text)

    def _maybe_crash(self) -> None:
        if self.crash_after is None:
            return
        n_eval = sum(1 for e in self.store.read_all() if e.type == EV_NODE_EVALUATED)
        if n_eval >= self.crash_after:
            os._exit(137)  # simulate kill -9 (no cleanup, no run_finished)

    def _leakage_blocks(self) -> bool:
        """Leakage-first gate (I9): run the detectors on whatever split/feature/target/
        timestamp data the task exposes via `leakage_inputs()`. Emit a verdict; return
        True (abort) if a hard leak is found. Tasks without the method are skipped."""
        fn = getattr(self.task, "leakage_inputs", None)
        if not callable(fn):
            return False
        inp = fn() or {}
        verdicts = []
        if "train_rows" in inp and "test_rows" in inp:
            verdicts.append(train_test_contamination(inp["train_rows"], inp["test_rows"]))
        if "features" in inp and "target" in inp:
            verdicts.append(target_leakage(inp["features"], inp["target"]))
        if "train_timestamps" in inp and "test_timestamps" in inp:
            verdicts.append(temporal_leakage(inp["train_timestamps"], inp["test_timestamps"]))
        leak = any(v.get("leak") for v in verdicts)
        self.store.append(EV_DATA_LEAKAGE, {"leak": leak, "verdicts": verdicts})
        return leak
