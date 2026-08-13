"""EngineOptions — the Engine's pure-config knobs as one frozen bundle (BACKLOG §4).

Before this module, every engine knob cost FOUR edits: a `Settings` field, a ~1-line passthrough in
`cli.py::_engine`, an `Engine.__init__` keyword, and a `self._x` assign. The Settings→Engine
passthrough is now a single `options=EngineOptions.from_settings(settings)`; the Engine resolves each
knob as: explicitly passed kwarg > `options` field > default (see `_UNSET` below). The
~100 existing `Engine(...)` keyword call sites keep working unchanged.

How to add an engine knob now:
  1. add the field to `Settings` (core/config.py) — flat, `LOOPLAB_<FIELD>` env mapping;
  2. add the SAME-NAMED field here with the Engine-side default (`from_settings` picks it up
     automatically; only a Settings-vs-Engine name mismatch needs a `_RENAMES` entry);
  3. add the `name=_UNSET` keyword + one `_opt(...)` resolution line in `Engine.__init__` and use it.
Object seams (task, roles, sandbox, policy, strategist, scorers, embedder, …) are NOT options —
they stay explicit `Engine.__init__` parameters wired by the caller (cli.py / tests).

Field defaults here MUST equal the legacy `Engine.__init__` signature defaults exactly — a bare
`Engine(...)` and an `Engine(..., options=EngineOptions())` must be indistinguishable
(tests/test_engine_options.py locks this in). Note that several `Settings` defaults deliberately
DIFFER from the engine defaults (e.g. merge_mode="auto", report_every=3): Settings is the
opinionated product surface, the engine default is the conservative library behavior.
"""
from __future__ import annotations

# BACKLOG §4: THE engine sentinel for "keyword not passed" — used by every pure-config
# Engine.__init__ knob AND by `_run_eval(start_stage=…)` (the engine passes it through
# `_evaluate`'s `next_start`), so knob resolution and the stage-reuse identity check share ONE
# object across orchestrator.py and the eval_dispatch mixin. It distinguishes "not passed" from
# any REAL value, including None/0/False.
_UNSET = object()

import dataclasses
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from looplab.core.models import FAILURE_REASONS

if TYPE_CHECKING:  # layering: engine may import core, but avoid the import cost at runtime
    from looplab.core.config import Settings

# EngineOptions field name -> Settings field name, for the few knobs whose engine-side name differs
# from the config name. Everything else maps 1:1 by name (see from_settings).
_RENAMES = {"policy_name": "policy"}


@dataclass(frozen=True)
class EngineOptions:
    """Every pure-config `Engine.__init__` knob (scalars/strings/bools/dicts mirroring `Settings`).

    Grouping and inline comments mirror the original `Engine.__init__` signature — the comments
    there remain the authoritative documentation for each knob's semantics.
    """
    max_parallel: int = 1                # single experiment at a time; > 1 = backlog parallel seam
    parallel_build: int = 1              # Variant-1: build (research+code) N nodes concurrently (1 = serial)
    # Layer-2 canonical parallelism names (docs/23). None (both) => fall back to the legacy fields above
    # => byte-identical to today. eval_parallel = concurrent evals (GPU consumer); llm_parallel =
    # shared provider-call total whose settled value also controls LLM build fan-out. Same default
    # (None) on BOTH sides -> no options-divergence and an unbounded broker total at startup.
    eval_parallel: Optional[int] = None
    llm_parallel: Optional[int] = None
    train_monitor: bool = False          # per-eval live training-log observer (off = today)
    train_monitor_interval_s: float = 600.0   # base tick cadence (s); effective cadence adapts to the budget
    train_monitor_kill: bool = False     # Phase 3: let the monitor tree-kill a 'broken' training early
    train_monitor_kill_confidence: float = 0.8   # min verdict confidence to act on a 'broken' kill
    asha_live: bool = False              # ASHA live-curve rank watchdog (advisory); off = today
    asha_live_kill: bool = False         # opt-in: tree-kill a persistently-underperforming node early
    asha_live_quantile: float = 0.5      # rank bar = this quantile of finished siblings' finals (median)
    asha_live_min_siblings: int = 3      # min finished peers before it ranks at all
    asha_live_kill_confidence: float = 0.8   # min LLM stop-verdict confidence to act on a rank flag
    timeout: float = 30.0
    # Hard ceiling for a governed Researcher `Idea.eval_timeout`. Keep equal to the product default:
    # a bare Engine and EngineOptions.from_settings(Settings()) must enforce the same finite ceiling.
    max_eval_timeout: float = 3600.0
    sweep_timeout_mult: float = 8.0      # intra-node sweep nodes get this × the single-eval budget
    eval_stall_timeout_s: float = 1800.0  # #6: silence-before-kill CAP for an eval stage; 0 disables
    eval_deadline_grace_s: float = 0.0   # one-shot judge-granted extension at the deadline; 0 = off
    confirm_top_k: int = 0
    confirm_seeds: int = 0
    confirm_seed_base: int = 1           # D1: first confirm seed; 1 keeps confirm splits disjoint
    max_seconds: Optional[float] = None
    max_eval_seconds: Optional[float] = None
    memory_dir: Optional[str] = None
    require_approval: bool = False
    archive_resolution: float = 1.0
    eval_trust_mode: str = "ratify_freeze"
    trust_mode: str = "trusted_local"
    docker_image: str = "python:3.12-slim"
    sandbox_memory: str = "4g"           # --memory for the untrusted command-eval Docker tier ("" = unbounded)
    sandbox_cpus: str = ""               # --cpus for the untrusted command-eval Docker tier ("" = unbounded)
    seed_mode: str = "auto"              # RepoTask node seeding fallback: auto|tracked|all
    # Source-tree read fence: off|warn|deny (see Settings.read_fence for why deny is the default).
    # Same value on BOTH sides on purpose — unlike merge_mode/report_every, this is not a knob where
    # the library may be quieter than the product: a bare `Engine(...)` driving a repo task can
    # record the same corrupt metric, and it is a no-op for every non-repo task anyway.
    read_fence: str = "deny"
    # --- A7 Strategist + richer-operator knobs (config-first; defaults == today's behavior) ---
    n_seeds: int = 3
    max_nodes: int = 8
    policy_name: str = "greedy"          # Settings.policy (renamed: the Engine keeps the policy OBJECT under .policy)
    ablate_every: int = 0
    strategist_every: int = 3
    concept_retag_every: int = 5    # PART V (F1): matches Settings; decoupled from strategist_every
    # Stop the whole RUN when nothing has ever worked: N distinct nodes ended failed and not one
    # has ever produced a metric. 0 = off, which is the bare-library default — a direct
    # `Engine(...)` gains no new terminal. See `orchestrator.systemic_failure_stop_reason`.
    systemic_failure_stop: int = 0
    # Run the stage every N created nodes. `-1` = OFF (manual/strategist only) — the bare-library
    # value, so a direct `Engine(...)` gains no cadence-driven paid think. `0` is NOT off here: since
    # 2026-08-07 it means "start immediately, then every node" (`engine/cadence.py`), which is the
    # product default. That inversion is why this row is -1 and not 0.
    deep_research_every: int = -1
    concurrent_research: bool = False    # overlap a due research "think" with the GPU-bound eval
    concurrent_research_repeat: bool = False   # one-shot overlap (== today); product re-runs on a cadence
    concurrent_research_interval_s: float = 1800.0   # base repeat cadence (s); floors the budget-derived pace
    concurrent_research_max_calls: int = 40     # per-eval-window repeated-research LLM backstop (0 = no cap)
    concurrent_consolidate: bool = False        # consolidate the hypothesis board on the eval-window bg loop (off = today)
    report_every: int = 0                # regenerate the run report every N created nodes (0 = manual only)
    merge_mode: str = "mean"             # A0b: "mean" | "ensemble" ("auto" resolves in Engine.__init__)
    complexity_cue: bool = False         # A0d: breadth-keyed prompt hint
    budget_aware: bool = False           # A5: surface remaining eval budget into the prompt
    failure_reflection: bool = False     # A4: reflect on recent failed branches in the prompt
    watchdog_reflection: bool = False    # feed recent live-watchdog (train-monitor/ASHA) flags to proposals
    deep_repair: bool = False            # C3: structured failure-taxonomy repair context
    localize_faults: bool = False        # C1: surface fault-localized files for repo tasks
    feature_engineering: bool = False    # I1: CV-gated feature-engineering directive
    ablate_code_blocks: bool = False     # A0a: ablate pipeline code blocks, not just params
    proxy_kill_fraction: float = 0.0
    reward_hack_detect: bool = False     # B5: flag suspicious wins
    trust_gate: str = "audit"            # T2: audit|gate|block — what a hack/leak flag does to selection
    code_leakage_detect: bool = False    # I3: static code-leakage scan per node
    critic_check: bool = False           # C4: execution-free critic per node
    redact_output: bool = False          # B3: redact secrets from persisted output tails
    novelty_mode: str = "llm"            # off | algo | llm — how a proposal is dedup-checked
    novelty_gate: bool = False           # E1: dedup near-duplicate proposals (algo mode)
    novelty_epsilon: float = 0.05
    reflection_priors: bool = False      # E4/M2/M3: cross-run priors + lessons (needs memory_dir)
    comparative_lessons: bool = False    # M6: credit-assigned pair lessons (needs reflection_priors)
    lessons_every: int = 0               # M6: mid-run distill cadence in nodes (0 = run-end only)
    lessons_refresh_every: int = 0       # M6: mid-run shared-store re-read cadence (0 = start only)
    track_hypotheses: bool = True        # P1: register deep-research directions as hypotheses
    surrogate_explore: float = 0.1       # A2/A3: explore weight for a lazily-wired BOHB surrogate
    unified_agent: bool = False          # one agent plays Researcher+Developer(+Strategist)
    agent_drives_actions: bool = False   # agent picks the next macro action (within a legal gate)
    # Layer 3: opt-in Card queue owns macro-action selection; false preserves the legacy policy/pilot path.
    card_driven_selection: bool = False
    # Layer 5: maximum request+committed speculative Card backlog. Zero keeps speculation fully off;
    # -1 is AUTO (Engine resolves it to the settled eval width — see `Engine._resolve_speculation_depth`).
    speculation_depth: int = 0
    # OPTIONAL calibration-benchmark receipt. Not required to run speculation; when present the Engine
    # revalidates it and refuses a stale/forged one (`Engine.__init__`'s admission block).
    speculation_gate_receipt: Optional[str] = None
    inline_repair: bool = True           # hybrid: triage + repair a crashed node IN PLACE (no new node)
    inline_repair_attempts: int = 12     # HARD cap on in-place repair retries per node (0 = UNLIMITED).
                                         # The primary stop is the triage model's `abandon` verdict;
                                         # this is the backstop — see core/config.py for the numbers
                                         # behind 12. Same value as the product Settings on purpose:
                                         # a bound is the CONSERVATIVE choice, so the library default
                                         # must not be the more permissive of the two.
    # ALL of `core/models.py::FAILURE_REASONS` since 2026-08-12 — see the field comment in
    # `core/config.py` for the run this default cost. Bound to the registry, not respelled:
    # this is the copy that would silently disagree with Settings.
    inline_repair_reasons: tuple = FAILURE_REASONS  # reasons eligible for inline repair
    metric_salvage: str = "audit"        # off | audit | select — see core/config.py
    metric_salvage_repair: bool = True   # fix the DECLARATION too; never re-evaluates
    inline_repair_retrain_cap: int = 2   # max FULL pipeline re-runs (re-trains) before abandoning
    auto_install_deps: bool = True       # pip-install a missing KNOWN lib + re-run (trusted_local only)
    dep_install_timeout: float = 900.0   # per-package install wall-clock budget (seconds)
    agent_control: Optional[dict] = None  # per-setting allow-list of roles that may change it
    # D1 holdout-gated promotion (B6) — see the Engine.__init__ comment block.
    holdout_fraction: float = 0.25
    holdout_select: bool = True
    holdout_top_k: int = 3
    # R1-c: calibrated §12-verifier metric-tie-break in best-selection (opt-in, lazy; needs a client).
    select_verifier: bool = False
    # R1-d: widen that tie-break to a statistical (CI) tie grounded in confirm noise (needs select_verifier).
    verifier_ci_tie: bool = False
    select_verifier_samples: int = 3
    # Phase 2 (D3/D4/T10/P4) knobs — kept on the engine so strategist-driven policy swaps
    # rebuild policies with the same run-wide settings.
    debug_depth: int = 1                 # T10: debug-lineage bound for every policy
    operator_bandit: bool = False        # P4: deterministic UCB over operator yields (GreedyTree)
    # T5 embedding-similarity dedup inside the "algo" gate. False matches the Settings default
    # and the documented rationale (novelty is the agentic Researcher's job by default): the old
    # True made a direct `Engine(novelty_gate=True)` behave differently from the identical
    # product config — the one inversion in the deliberate Settings-vs-Engine divergence table
    # (tests/test_options_divergence.py). Deliberate library-default change (docs/15 §P4.4).
    novelty_semantic: bool = False
    novelty_semantic_threshold: float = 0.92
    digest_char_cap: int = 0             # M5: digest prompt budget; 0 = auto-scale with run size
    research_verify: bool = True         # D8: verify memo claims against cited evidence
    workdir_audit: bool = True           # 4.4: flag unexpected writes in the eval workdir
    # ADR-17: capture this run's bounded/redacted LLM I/O into its spans.jsonl. Bound to the run's OWN
    # Tracer (core/tracing.py) rather than the process-global flag, so two Engines in one process keep
    # opposite policies. None = declare nothing and defer to that process-wide default (what a bare
    # `Engine(...)` did before this knob existed).
    trace_llm_io: Optional[bool] = None
    coverage_context: bool = True        # narrowing signal: coverage_snapshot at the strategist cadence
    concept_pivot: bool = False          # PART IV 2a: concept-graph uncovered-region pivot (opt-in)
    graded_novelty: bool = False         # PART IV 2b: D3 graded novelty into the live gate (level-4/5 allow)
    capability_expansion: bool = False   # PART IV 2b: D7 capability-expansion forced-jump directive on lock-in
    fingerprint_universal: bool = False  # PART IV CR Step 0: universal (any-script) task-fingerprint tokens
    cross_run_concepts: bool = False     # PART IV CR Step 2: surface prior-run concept outcomes (audit-only)
    concept_run_base: bool = False        # PART V B: run-base + node-delta concept authoring (opt-in)
    cross_run_advisory: bool = False     # PART IV CR Step 5: fold the cross-run context pack into the prompt
    cross_run_structured_claims: bool = False  # PART IV CR §21.20.13: scope+polarity-safe structured claim key
    cross_run_curation: bool = False     # PART IV §22.4: agentic taxonomy steward proposes merge/split/purge
    task_facets_finalize: bool = False   # opt-in paid facet steward; no live behavior consumer yet
    cross_run_curation_auto: bool = False  # deprecated/inert: old snapshots validate; proposals never auto-apply
    concept_tidy: bool = False           # PART IV §22.4: RATIFY the steward's proposed merges at finalize
    cross_run_read_tools: bool = False   # PART V §22: reasoning roles get the cross_run_* READ tools; the engine
    # mirrors the Settings flag ONLY so the proposal hint can add a lean pointer to those tools (never wires them)
    phase_handoff_summary: bool = True   # per-phase handoff briefs across a node build (propose→…→implement)

    @classmethod
    def from_settings(cls, s: "Settings") -> "EngineOptions":
        """Build the bundle from a `Settings` — the exact Settings→Engine mapping cli.py::_engine
        used to spell out kwarg-by-kwarg. Every field is a straight `settings.<name>` copy (direct
        attribute access on purpose: a missing Settings field should fail loudly, exactly like the
        old literal passthrough did), except the `_RENAMES` entries. Anything cli.py derives with
        real logic (built objects, CLI-flag paths) is NOT here — those stay explicit Engine kwargs."""
        return cls(**{f.name: getattr(s, _RENAMES.get(f.name, f.name))
                      for f in dataclasses.fields(cls)})
