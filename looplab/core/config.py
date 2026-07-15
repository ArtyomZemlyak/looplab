"""Engine config: the schema (what settings exist). (I0, ADR-11)

One pydantic-settings class declaring every engine knob, its default, and its docs. The loader —
how a run file / CLI / env becomes a `Settings` — is `looplab.core.appconfig`, which also owns
the config-precedence order. A resolved, secret-masked snapshot is written next to each run for
reproducibility. (No real secrets in P0, but the masking discipline is in place.)
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Single source of the LLM first-byte (response-headers) default — see core/llm.py.
from looplab.core.llm import DEFAULT_HEADER_TIMEOUT_S

# Default home for cross-run memory + the knowledge base (shared across every run). Both are ON by
# default now (a user asked for it) so a fresh install accumulates + consults knowledge with no setup;
# set the env var to "" to disable, or to a path to relocate. `~/.looplab/` keeps them out of any one
# project + shared across projects.
_LL_HOME = Path.home() / ".looplab"

# Autonomous roles that may be granted per-setting write access (see Settings.agent_control).
# strategist = A7 meta-controller (run-wide); boss = run-chat operator-proxy (run-wide);
# researcher = per-experiment proposer (per-node sizing, e.g. eval timeout for a heavy model).
AGENT_ROLES = ("strategist", "boss", "researcher")

# --- Run profiles (config-first presets) -------------------------------------------------------
# A `profile` is a NAMED BUNDLE of setting defaults — nothing more. It only fills fields the user
# did NOT set explicitly (via file / --set / LOOPLAB_* env / init-kwargs), so any explicit knob
# always wins (see `_apply_profile`). The whole intelligence of the engine already exists behind
# individual flags but ships OFF so a toy `looplab run` stays cheap and deterministic; `thorough`
# turns the built-and-tested machinery ON in one word for real tasks. Every value here is reachable
# by hand — the profile is a convenience, never a hidden mode.
#
# `thorough` deliberately touches only QUALITY/TRUST machinery, not spend: it does NOT raise
# max_nodes / max_parallel (those are cost knobs the user owns). It enables multi-seed confirmation
# (demote seed-lucky leaders), the novelty gate (stop re-evaluating duplicates), the reward-hack /
# leakage / critic monitors AND flips them from advisory to *gating* (`trust_gate="gate"` — a
# flagged win can no longer be selected as best), ablation-driven refinement, and the prompt cues
# (complexity / budget / failure-reflection). Effect is large; marginal cost is a few extra evals.
PROFILES: dict[str, dict] = {
    "default": {},   # explicit no-op alias for "ship-as-is" (== omitting profile)
    "fast":    {},   # today's lean defaults, named for symmetry with `thorough`
    "thorough": {
        "confirm_top_k": 3,
        "confirm_seeds": 3,
        # novelty_gate intentionally NOT enabled here: duplicate-detection is the agentic Researcher's
        # job (it reads past experiments + find_analogous_across_runs and DECIDES), not an algorithmic
        # param-distance / embedding auto-reject that mis-fired (it nudged a good fresh idea onto a
        # twice-dead lineage — live node 61). Search stays as tools; the LLM judges novelty.
        "operator_bandit": True,   # P4: adaptive operator mix over folded yields (GreedyTree)
        "reward_hack_detect": True,
        "code_leakage_detect": True,
        "critic_check": True,
        "trust_gate": "gate",          # detectors stop gating a flagged node from WINNING (not just audit)
        "ablate_every": 3,
        "complexity_cue": True,
        "budget_aware": True,
        "failure_reflection": True,
        "reflection_priors": True,     # no-op unless memory_dir is set (cross-run priors)
    },
}


# The shipped agent-governance matrix (who may change which setting at runtime). Defined as a module
# constant so the Engine's direct-construction default matches this shipped default exactly (the
# EngineOptions "Engine() == shipped defaults" invariant) instead of diverging to an all-locked map.
DEFAULT_AGENT_CONTROL: dict[str, list[str]] = {
    "timeout": ["researcher", "strategist"],
    "max_parallel": ["strategist"],
    "max_nodes": ["strategist", "boss"],
    "max_eval_seconds": ["strategist", "boss"],
    "policy": ["strategist"],
    # The policy NAME and its PARAMS are gated independently in `_apply_strategy`; grant both by default
    # so the Strategist that may switch to (say) MCTS may also apply the `c`/`eta` it decided — else its
    # tuned params are silently dropped and the recorded `active_strategy` diverges from the live engine.
    "policy_params": ["strategist"],
    "n_seeds": ["strategist"],
    "ablate_every": ["strategist"],
    "merge_mode": ["strategist"],
    "complexity_cue": ["strategist"],
    "ablate_code_blocks": ["strategist"],
    "prefer_sweep": ["strategist"],
    "novelty_stance": ["strategist"],
    "developer": ["strategist"],
    "fidelity": ["strategist", "researcher"],
}


def default_agent_control() -> dict[str, list[str]]:
    """A FRESH deep-ish copy of the shipped governance matrix (each role list copied so a caller can
    mutate its own map without touching the module constant). The SINGLE constructor of the default,
    called by both `Settings.agent_control`'s default_factory AND the Engine's direct-construction
    default — so "Engine() == shipped defaults" can't drift if the copy depth ever needs to change
    (e.g. nested values requiring copy.deepcopy), instead of two verbatim copy expressions."""
    return {k: list(v) for k, v in DEFAULT_AGENT_CONTROL.items()}


class Settings(BaseSettings):
    """The engine settings schema (every knob a run accepts).

    Timeout family — five distinct knobs, each owned by a different subsystem:
      - `timeout`:             per-eval wall-clock budget for ONE experiment's evaluation (engine/eval).
      - `llm_timeout`:         LLM request idle timeout — inter-token stall limit in stream mode
                               (core.llm OpenAICompatibleClient).
      - `llm_header_timeout`:  LLM first-byte (response-headers) window for stream attempts (core.llm).
      - `agent_time_budget_s`: wall-clock ceiling across a tool-using agent loop's turns (agents).
      - `dep_install_timeout`: per-package budget for auto-installing a missing dep (env self-prep).

    Config-source precedence is owned by `looplab.core.appconfig` (the loader) — see its module
    docstring for the one canonical order.
    """
    # Config precedence: see appconfig.py — the loader owns the full order. The `.env` is read
    # so `looplab run`/`looplab ui` pick up LOOPLAB_LLM_BASE_URL etc. without exporting them by hand
    # (keys MUST carry the LOOPLAB_ prefix, same as the env vars). utf-8-sig tolerates a BOM from
    # Windows editors. The test suite disables this (tests/conftest.py) so a dev's real .env in the
    # repo root can't leak into assertions.
    model_config = SettingsConfigDict(
        env_prefix="LOOPLAB_",
        env_file=".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
        allow_inf_nan=False,
    )

    # Run profile (config-first preset, see PROFILES above). "default"/"fast" = today's lean
    # defaults; "thorough" turns the built quality/trust machinery ON in one word. A profile only
    # fills fields the user did NOT set — every knob it touches stays individually overridable.
    profile: str = "default"

    # Bounds (review C/⚪): `max_parallel=0` silently stalls the loop; a non-positive node/seed
    # budget or timeout is never valid. Upper bounds are equally load-bearing — the UI /start path
    # writes these straight into the engine's env, and an unbounded `max_parallel` (e.g. 100000 from
    # a crafted preflight) would fan out that many sandboxes → resource exhaustion. Reject at config
    # time, not mid-run. Ceilings are generous (far above any real run); tests top out near 50.
    n_seeds: int = Field(default=3, ge=1, le=1024)
    max_nodes: int = Field(default=8, ge=1, le=1_000_000)
    # Concurrent evaluation is a backlog seam. The base/primary mode is a single
    # experiment at a time (deterministic, one eval process running) — important for
    # RepoTask where an eval is a real training run with its own resources/trackers.
    # Set > 1 to opt into the parallel fan-out (the task-group path).
    max_parallel: int = Field(default=1, ge=1, le=1024)
    timeout: float = Field(default=30.0, gt=0)
    # Intra-node sweep: a sweep node runs a whole grid in one process, so it gets this multiple of
    # `timeout` as its wall-clock budget (solution.py path; RepoTasks use their per-profile timeout).
    sweep_timeout_mult: float = Field(default=8.0, ge=1)
    # Sandbox tier (ADR-13): "trusted_local" (subprocess, no Docker) for the CLI;
    # "untrusted" (Docker --network none, shared-kernel runtime) for hosted/multi-tenant UI;
    # "hostile" (untrusted + a true-isolation OCI runtime, gVisor `runsc` by default / Kata) for
    # running untrusted code. See runtime/sandbox.py::make_sandbox for the tier selection.
    trust_mode: str = "trusted_local"
    # Image for the untrusted command-eval tier (RepoTask, Phase 4): the framework's deps
    # should be baked in (the container runs --network none, so a pip setup can't fetch).
    docker_image: str = "python:3.12-slim"
    # Resource caps for the untrusted/hostile Docker tier (passed to `docker run --memory/--cpus`).
    # The default 4g bound protects other tenants from an OOM, but a model-training MLE-bench eval
    # routinely needs more — raise it (e.g. "16g") for such runs. "" disables the cap (unbounded).
    # Ignored by the trusted_local (subprocess) tier.
    sandbox_memory: str = "4g"
    sandbox_cpus: str = ""
    # Best-effort host-OOM guard for the TRUSTED-LOCAL (subprocess) tier. When set (e.g. "8g"), each
    # eval child gets an RLIMIT_AS address-space cap so a runaway trainer hits MemoryError instead of
    # OOM-killing the whole host (and the engine). POSIX only; a no-op on Windows. Default "" = OFF:
    # RLIMIT_AS bounds VIRTUAL memory, and CUDA/torch reserve tens of GB of virtual without using it,
    # so a default cap would spuriously kill GPU evals — leave it off for those and use the Docker
    # tier's real --memory cgroup bound (sandbox_memory) instead. Ideal for CPU/numpy/sklearn runs.
    sandbox_memory_local: str = ""
    # Best-effort disk-fill guard for the TRUSTED-LOCAL tier (P1-5): an RLIMIT_FSIZE cap on the size of
    # any single file an eval child writes (e.g. "2g"), so a runaway that writes an unbounded file gets
    # SIGXFSZ instead of filling the host disk. POSIX only. Default "" = OFF (large model checkpoints
    # need big files); set it for tasks that write only small artifacts.
    sandbox_fsize_local: str = ""
    # Search policy (ADR-2): "greedy" | "evolutionary" | "mcts" | "asha".
    policy: str = "greedy"
    # Ablation-driven refinement (I7): every N improves, ablate the best to find the
    # highest-impact parameter and refine it. 0 = off. (GreedyTree only.)
    ablate_every: int = 0
    # A0a (MLE-STAR): when ablating, treat each generated pipeline *code block* (not just a
    # numeric param) as an ablation unit — neutralize a block, measure the metric delta, refine
    # the highest-impact block. Off => the classic numeric-param ablation. Config-first knob.
    ablate_code_blocks: bool = False
    # A0b: real merge/ensembling. "mean" = legacy mean-param merge; "ensemble" = the Developer
    # writes a code-recombination ensemble over the two parents' solutions (verified: agent-proposed
    # ensembling 37.9%->43.9%). "auto" (default) = ensemble whenever the Developer actually
    # generates code (LLM/agent backends; declared via `is_code_generating`), mean for the
    # templated/toy developers where a code ensemble is meaningless — the evidence says code
    # recombination is the strongest single merge (removing it costs ~9 pp), so it is the default
    # wherever it can work. Falls back to mean when the Developer can't ensemble.
    merge_mode: str = "auto"
    # A0d (AIRA): inject a dynamic complexity hint into the draft/improve prompt keyed on the
    # node's child count (few children -> keep minimal; many -> escalate to ensembling/HPO).
    complexity_cue: bool = False
    # I1 (CAAFE-style) LLM feature-engineering: instruct the proposer/developer to add
    # semantically-meaningful engineered features (ratios, interactions, aggregations) as code. The
    # existing cross-validation eval + best-selection is the CV GATE — a feature set that doesn't
    # improve CV is never selected (feature engineering is non-universal, so this gate is mandatory).
    # Tabular tasks (those exposing column data). Off by default.
    feature_engineering: bool = False
    # A5: surface the REMAINING eval-compute budget into the proposal prompt so the Researcher can
    # reason over how much compute is left (explore broad while flush, exploit/cheapen when low).
    # No-op unless a max_eval_seconds budget is set.
    budget_aware: bool = False
    # A4 (LATS-style): feed a summary of the most recent FAILED branches (operator + error reason)
    # back into the proposal prompt so the proposer reflects on and avoids repeating them. ON by
    # default: it is SELECTIVE by construction (injects only when recent failures exist — the
    # evidenced form of reflection; reflecting at every step shows no gains, arXiv:2506.12928).
    failure_reflection: bool = True
    # C3 deep test-driven repair: hand the Developer the failure taxonomy + a structured "reproduce
    # then fix" directive on debug, not just the raw stderr tail. ON by default (product surface); the
    # conservative library default in EngineOptions stays off, per that module's contract.
    deep_repair: bool = True
    # === Inline crash repair ==============================================================
    # Hybrid in-node crash repair: when an LLM-generated node CRASHES at runtime (mechanical errors
    # — bad import, removed kwarg, typo), the agent triages it and may repair the code IN PLACE within
    # the same eval (cheap; does NOT consume max_nodes and does NOT add a node to the search tree).
    # Semantic failures still flow to a new idea/debug node via the policy. ON by default; set False
    # to restore the prior behavior (every crash waits for a budgeted inter-node debug node).
    inline_repair: bool = True
    # Max in-place repair attempts per node before it fails normally. Default 0 = UNLIMITED: the same
    # agent keeps fixing + re-evaluating IN THE SAME node until it produces a clean metric — no new
    # node just to retry. Runaway is prevented not by a count but by the anti-stuck guard
    # (`inline_repair_stuck_repeat`): when the SAME error signature repeats with no progress, or the
    # agent's crash-triage says "abandon", the node fails. Set a positive N to cap the retries instead.
    inline_repair_attempts: int = Field(default=0, ge=0)
    # Anti-stuck: abandon in-node repair when the SAME error recurs this many times in a row (no
    # progress). Keeps "unlimited" repair from looping forever on an unfixable error.
    inline_repair_stuck_repeat: int = Field(default=4, ge=2)
    # Which failure reasons (_failure_reason: crash|timeout|oom|setup|no_metric|drift) are eligible for
    # inline repair. Default: mechanical crashes, timeouts AND OOM-kills — a timeout/OOM means the code
    # was too slow / too memory-hungry for the budget (or the pod's cgroup limit), not that the idea is
    # wrong, so the agent repairs it by reducing compute/memory (fewer estimators/epochs/folds/seeds, a
    # lighter model, a smaller batch, subsampling) instead of letting the node die unrecovered. Env
    # override expects a JSON array.
    inline_repair_reasons: tuple[str, ...] = ("crash", "timeout", "oom")
    # Cost ceiling for in-node repair of a MULTI-STAGE eval: the max number of FULL pipeline re-runs
    # (i.e. re-trains from the first stage) the inline-repair loop may do before abandoning to the
    # inter-node debug operator. A late-stage failure (a broken `score`/eval script that didn't touch
    # `train`) REUSES the completed train checkpoint and re-runs only from the failed stage — that is
    # CHEAP and NOT counted here; only a repair that changes an EARLIER stage's code (train.py/loss.py/…)
    # forces a full re-train, which each costs the whole training time. Without this bound, a repair that
    # keeps changing training code could burn many full trains (the anti-stuck guard is error-signature
    # based, not cost based). 0 = unlimited (legacy behavior). Single-command evals ignore it.
    inline_repair_retrain_cap: int = Field(default=2, ge=0)
    # Environment self-prep: when a solution crashes purely because a KNOWN library isn't installed
    # (ModuleNotFoundError), the engine pip-installs it into the eval interpreter and re-runs — so a
    # torch/XGBoost/CatBoost idea (e.g. a GRU model) actually runs on a fresh box instead of being
    # thrown away as `idea_rejected`. Trusted_local tier only (the untrusted/hostile Docker tiers run
    # --network none). A typo'd or local helper module is NOT installed (that's a code bug to repair).
    auto_install_deps: bool = True
    # Per-package wall-clock budget for an auto-install (seconds). Generous: large wheels like torch
    # can take minutes to download/build.
    dep_install_timeout: float = Field(default=900.0, gt=0)
    # --- Agent governance: who may change a setting at runtime (per-setting allow-list of roles) ---
    # Roles: "strategist" (A7 meta-controller; run-wide), "boss" (run-chat operator-proxy; run-wide),
    # "researcher" (per-experiment proposer; PER-NODE — e.g. set a longer eval timeout for a neural-net
    # idea). A setting ABSENT from this map is LOCKED — no agent may change it, only a human via the
    # snapshot/UI. The gate is ENFORCED at every AGENT seam via `_agent_may` — the strategist's whole
    # control surface (policy/operators/novelty_stance/prefer_sweep/developer/fidelity/timeout/
    # max_parallel) is checked in `_apply_strategy`, so removing a role from a knob genuinely locks it,
    # not just greys out the UI pill. The boss/max_* grants document boss-relevant knobs, but a
    # `budget_extend` is a HUMAN control intent (the boss action-builder can only emit `add_nodes`), so
    # it is applied as-is — the human always wins via the UI/snapshot. Defaults are conservative: resource/
    # search-shape knobs the agents already reason about; infra (llm_*, trust_mode, api_key,
    # docker_image) stays locked. The UI shows S/B/R pills per setting. `timeout` granted to the
    # researcher IS the "auto" per-node mode — the researcher sizes each experiment's wall-clock
    # (Idea.eval_timeout); the config `timeout` is the fallback for nodes it doesn't size. Env override
    # expects a JSON object. (`fidelity`/`novelty_stance`/`prefer_sweep`/`developer` are governance keys
    # for the strategist's per-node dials — not 1:1 Settings fields, but gated the same way.) The default
    # lives in the module-level DEFAULT_AGENT_CONTROL so the Engine's direct-construction default matches.
    agent_control: dict[str, list[str]] = Field(default_factory=default_agent_control)
    # C1 (Agentless localization): for repo tasks, rank the source files most relevant to the most
    # recent failure (traceback + identifiers) and surface them in the proposal/repair prompt so the
    # Developer edits the right place. Off by default; repo tasks only.
    localize_faults: bool = False
    # A2 surrogate-guided proposal (BO-lite): fit a k-NN surrogate over (params->metric) and propose
    # by acquisition instead of random/hill-climb. Numeric-bounds tasks only; bootstraps via the
    # base Researcher. Off by default. `surrogate_explore` = UCB-style exploration weight.
    surrogate_proposer: bool = False
    surrogate_explore: float = 0.1
    # E2 researcher panel: generate K candidate ideas per proposal and keep the one ranked best by a
    # cheap empirical surrogate over the (params->metric) history (NOT an LLM-judge). 1 = off.
    researcher_panel: int = 1
    # A1 ASHA / successive-halving: reduction factor (keep top 1/eta per rung) and rung budget.
    asha_eta: int = Field(default=3, ge=2)
    # Rung-0 width for ASHA/BOHB (the wide base of cheap drafts). 0 = use n_seeds (default,
    # preserving prior behavior); set >0 to seed a wider/narrower base independent of n_seeds.
    asha_rung_nodes: int = Field(default=0, ge=0)
    # A6 proxy/predictive scoring: cheaply rank a candidate's potential from early-stage signals
    # and skip a full eval for the doomed bottom fraction. 0.0 = off (no candidate skipped).
    proxy_scoring: bool = False
    proxy_kill_fraction: float = Field(default=0.0, ge=0.0, le=0.9)
    # === Novelty / dedup ==================================================================
    # E1 novelty/dedup gate: reject near-duplicate proposals (normalized param-space distance below
    # `novelty_epsilon`) by deterministically nudging them off the duplicate — stops the search
    # wasting evals re-trying the same idea. Audit event `novelty_rejected`. Off by default.
    # Novelty/dedup MODE selector: how a fresh proposal is checked against already-tried experiments.
    #   "off"  — no explicit gate; the agentic Researcher's own read-the-history judgment is trusted.
    #   "algo" — the deterministic gate: numeric param-distance (`novelty_epsilon`) + optional embedding
    #            similarity (`novelty_semantic`). Cheap, no LLM call, but can't explain itself.
    #   "llm"  — an LLM adjudicates (reads the real experiments via tools) whether the idea is a
    #            near-duplicate and, if so, asks the Researcher once more for something different. One
    #            extra LLM call per proposal — highest quality, follows the "let the LLM decide" line.
    novelty_mode: str = "llm"
    # Legacy sub-toggles, honored by the "algo" mode (and back-compat: novelty_gate=True forces "algo"):
    novelty_gate: bool = False
    novelty_epsilon: float = 0.05
    # T5 semantic novelty (active whenever the deterministic gate runs — novelty_mode=algo,
    # novelty_gate=true which is the legacy alias for algo, OR the Strategist novelty stance is
    # "explore"; a no-op only under novelty_mode=llm/off with a non-explore stance): reject a proposal
    # whose idea TEXT embeds within
    # `novelty_semantic_threshold` cosine of an existing node's. OFF BY DEFAULT: novelty is the agentic
    # Researcher's call, made by READING the actual prior experiments (read_experiment /
    # find_analogous_across_runs / list_experiments), NOT an embedding-similarity auto-reject that can't
    # explain itself and mis-fires on paraphrases. Semantic/param search stays available as TOOLS in the
    # Researcher's context; the LLM decides whether an idea is a true duplicate.
    novelty_semantic: bool = False
    novelty_semantic_threshold: float = Field(default=0.92, ge=0.5, le=1.0)
    # T10: debug-lineage depth bound for every policy — how many error-feedback repairs a failing
    # lineage gets before it is abandoned. The old default (1) abandoned lineages after ONE repair;
    # multi-turn/deeper debugging is a verified lever (AIRA2 ReAct debug +5.5 percentile points).
    debug_depth: int = Field(default=2, ge=1)
    # P4 operator bandit (GreedyTree): replace the fixed merge/ablate cadences with a
    # deterministic UCB over per-operator yield (Δmetric per eval-second, folded from the run).
    # Off by default (the cadences are well-tested and the bandit has no direct published
    # ablation); `thorough` turns it on.
    operator_bandit: bool = False
    # P1 hypothesis ledger: ask the Researcher to state the one-line hypothesis each experiment tests,
    # register deep-research directions as hypotheses, and track them to a verdict on the board. ON by
    # default (audit-only — never changes selection). Set False to drop the prompt nudge + registration.
    track_hypotheses: bool = True
    # E4/M2/M3 reflection-memory -> priors (gradient-free cross-run meta-learning): at run end distill
    # the winner into `<memory_dir>/meta_notes.jsonl` AND structured lessons (incl. NEGATIVE results:
    # tested/abandoned hypotheses + failure themes) into `lessons.jsonl` with a task fingerprint; at run
    # start, inject exact-task notes + fingerprint-matched lessons from SIMILAR tasks into the proposal
    # prompt. ON by default — but a NO-OP until `memory_dir` is set (that's where cross-run memory lives).
    reflection_priors: bool = True
    # M6 comparative lessons (MARS "comparative reflective memory", doc 13 §7 item 2): distill
    # credit-assigned lessons from PAIRS of solutions — which SPECIFIC change made a child beat
    # (or regress from) its parent, and what fixed a failure — on top of the one-shot run-end
    # reflection. At most one extra LLM call per distillation (all pairs batched); offline a
    # deterministic param-diff credit stands in. Gated on reflection_priors + memory_dir like the
    # rest of cross-run memory — and since memory_dir has a default (~/.looplab/memory), this is
    # ACTIVE out of the box; clear memory_dir to disable cross-run memory wholesale.
    comparative_lessons: bool = True
    # M6 mid-run cadences (doc 13 §7 item 5 — the AgentRxiv live-share pattern): every N created
    # nodes, WRITE comparative lessons to the shared cross-run store during the run (lessons_every
    # — one batched LLM call per firing) and RE-READ the store so lessons distilled by CONCURRENT
    # runs reach this run's proposals (lessons_refresh_every; file re-read, no LLM call, skipped
    # when the store is unchanged). ON by default (every 4 nodes) and, like comparative_lessons
    # above, live out of the box because memory_dir defaults on; 0 = off — run-end write /
    # run-start read only, the pre-M6 behavior.
    lessons_every: int = Field(default=4, ge=0)
    lessons_refresh_every: int = Field(default=4, ge=0)
    # B3 output redaction: mask credentials (known key shapes + high-entropy tokens) in the
    # stdout/stderr tail before it is persisted to the event log / spans / UI — a leaked secret in a
    # print()/traceback must not land in the durable log. Off by default; recommend on for untrusted.
    redact_output: bool = False
    # B5 reward-hacking detector: a host-side monitor that flags suspicious wins (grader/answer-key
    # access, runtime writes to frozen files, suspiciously-perfect metrics) as a `reward_hack_suspected`
    # audit event in the Trust panel. Off by default. Whether a flag CHANGES selection is governed by
    # `trust_gate` below (default: audit-only, never changes selection).
    reward_hack_detect: bool = False
    # Trust enforcement (T2): what a reward-hack / data-leakage flag actually DOES to selection.
    #   "audit" (default) = surface it in the Trust panel, never change selection (today's behavior);
    #   "gate"  = a flagged node can no longer be selected as BEST **and isn't bred/confirmed from**
    #             (breed_excluded — see replay._apply_trust_gate / models.breedable_nodes), but stays
    #             FEASIBLE so it still counts for diversity/audit — closes the "a hacked/leaky node can
    #             win OR seed its lineage" hole;
    #   "block" = additionally mark it fully INFEASIBLE (removed from feasible_nodes entirely).
    # Deliberately gates only high-precision CHEATING/LEAKAGE signals; the heuristic `critic` signal
    # stays advisory (audit) in every mode — the field is a per-node quality hint, not proof of a hack.
    trust_gate: str = "audit"
    # I3 data-centric: static code-leakage scan of each evaluated solution (fit-before-split,
    # fit-on-test) surfaced into the Trust panel alongside reward-hack flags. Off by default.
    code_leakage_detect: bool = False
    # 4.4 sandbox instrumentation (needs reward_hack_detect): after each eval, flag a RUNTIME write
    # to a protected/frozen file (an answer key or grader tampered mid-run) — behavioral evidence a
    # static code scan misses. ON by default; only fires when the reward-hack detector is on.
    workdir_audit: bool = True
    # D8 decoupled research Verifier: check every Deep-Research memo's CLAIMS against their cited
    # evidence (node ids / urls) before recording — synthesis is the documented weak link (Kosmos
    # 57.9% accurate). Deterministic layer always; an LLM rubric pass when the DeepResearcher has a
    # client. Audit-only (verdicts ride inside the memo). ON by default (no-op with no memo/claims).
    research_verify: bool = True
    # C4 independent critic: an execution-free critic of each solution (stub / hardcoded-metric /
    # params-ignored; on host-graded tasks the metric checks become a submission-output check)
    # surfaced in the Trust panel. Audit-only. Off by default.
    critic_check: bool = False
    # A7 Strategist (NEW, user-requested): optional LLM/rule meta-controller that picks the search
    # policy/allocator + operator mix + fidelity (+ Developer backend) per situation. Config-first:
    # every choice the Strategist makes is also a direct knob. Defaults to "agent" so the agent adapts
    # the search policy/operators/fidelity per situation; set "off" for static config-driven search.
    # "agent" = a tool-using Strategist that READS the run/data/sibling-runs/KB/memory (B1-guarded)
    # before deciding, instead of the single-shot "llm" call over aggregate stats.
    strategist_backend: str = "agent"          # "off" | "rule" | "llm" | "agent" — default AGENTIC: the
    #   Strategist READS the run/data/sibling-runs/KB/memory with tools before deciding, not a single-shot
    #   call over aggregate stats. "llm" = the old non-agentic single-shot; "rule"/"off" = no LLM.
    strategist_every: int = Field(default=3, ge=1)   # consult cadence (created nodes)
    # === Confirmation & holdout ===========================================================
    # Multi-seed confirmation (I12, ADR-15): confirm the top-k under N seeds before
    # finishing. 0 disables (default). Only meaningful when eval has variance.
    confirm_top_k: int = 0
    confirm_seeds: int = 0
    # D1 seed-holdout discipline: the FIRST seed the confirm phase uses. Search evals run with the
    # implicit seed 0 (LOOPLAB_EVAL_SEED unset -> "0"), so a base of 1 keeps every confirm split
    # DISJOINT from anything the search optimized against — the confirm metric is then a
    # generalization signal, not a re-measurement (AIRA: selecting on a signal the search saw
    # overfits by 9-13 pp). Set 0 to restore the legacy overlapping seeds 0..N-1.
    confirm_seed_base: int = Field(default=1, ge=0)
    # D1 holdout-gated promotion (B6, Arbor-style): for host-graded tasks, reserve this fraction of
    # the held-out labels as a FINAL holdout partition the search never sees — every search/confirm
    # eval is scored on the remaining rows only, and at finish the val-top-k are re-scored on the
    # holdout partition (free: the predictions already exist). 0.0 = off (legacy full-label scoring).
    holdout_fraction: float = Field(default=0.25, ge=0.0, le=0.9)
    # Champion selection prefers the HOLDOUT metric among the nodes that have one (the val-top-k).
    # Recorded in run_started so replay applies the same rule; old logs fold to False (legacy pick).
    # The per-node val-holdout `generalization_gap` folds into the Trust panel either way.
    holdout_select: bool = True
    # How many val-leaders get a holdout evaluation at finish (host-graded tasks).
    holdout_top_k: int = Field(default=3, ge=1)
    # R1-c / Part IV — calibrated §12-verifier tie-break in best-selection. When a fresh node's robust
    # metric EXACTLY TIES an already-eligible node, the engine runs the §12 verifier (grounded on each
    # node's realized result, `selection_criteria`) and records a soundness score per node. The tie-break
    # then applies across the WHOLE selection path: the mean pick breaks a robust_metric tie by the score,
    # and — when `holdout_select` is on — the holdout-slot ranking prefers the sounder node so it isn't
    # denied a holdout eval, and the final holdout override breaks a holdout_metric tie by it too. Strictly
    # a TIE-BREAK — it can NEVER move a node ahead of a strictly-better metric (§21.7 advisory-never-
    # overrides). Best-effort: verification is LAZY (only fires on an actual tie) and bounded per cadence,
    # so an unscored node in a large/late tie group contributes a NEUTRAL midpoint (a fully-scored tie is
    # resolved by soundness; a partially-scored one degrades to neutral+id). Recorded in run_started so
    # replay applies the same rule; old logs fold to False (legacy pick). Opt-in (default off); needs a
    # reachable LLM client. Enable only after validating calibration offline with `verifier.calibrate`.
    # See trust/verifier.py + core/fitness.py.
    select_verifier: bool = False
    # R1-d (§21.19): widen the verifier tie-break from EXACT-metric to a STATISTICAL tie — two confirmed
    # means within the confirm-phase noise (|Δ| <= 1.96·SE, SE=confirmed_std/sqrt(confirmed_seeds)) are
    # tied and broken by soundness. Requires `select_verifier`. NEVER widens past measured noise, so a
    # SIGNIFICANT difference is never a tie (§21.7). Off -> exact-metric tie only.
    verifier_ci_tie: bool = False
    # Verifier sample count for `select_verifier` (the §12 repeated-sampling expectation). 3 tames the
    # single-shot variance; 1 = single-shot (cheaper, noisier).
    select_verifier_samples: int = Field(default=3, ge=1)
    # Budget (I13): hard wall-clock ceiling; the run aborts cleanly when exceeded.
    max_seconds: float | None = None
    # Eval-compute budget (#2): hard ceiling on cumulative time spent INSIDE evals (training
    # runs), separate from wall-clock. Survives resume (summed from the event log). The real
    # guard against a silent multi-hour sweep when an eval is a minutes-hours training run.
    max_eval_seconds: float | None = None
    # Cross-run memory (I19, ADR-10): if set, the best result of each run is stored as
    # a case here, and the cases become retrievable knowledge for future runs.
    memory_dir: str | None = Field(default_factory=lambda: str(_LL_HOME / "memory"))
    # HITL (I21, ADR-11): pause for human approval of the final best before finishing.
    require_approval: bool = False
    # Diversity archive (I22): niche bucket width in parameter space.
    archive_resolution: float = 1.0
    # Coverage read-model (narrowing signal): at the strategist cadence, compute a breadth summary
    # (themes / param-niches / theme entropy / dominant-theme fraction) from the folded run, record
    # it as a `coverage_snapshot` audit event, and surface it into the Strategist's decision context.
    # Deterministic, cheap, and purely additive context — no search-behavior change on its own. On by
    # default (like the rest of the always-on situational context). See search/coverage.py.
    coverage_context: bool = True
    # PART IV Phase 2a live steering (§21.11/§21.13). When on, the strategist cadence records a
    # concept-graph coverage + uncovered-region snapshot (deterministic heuristic tagger over the
    # task-type skeleton) and, on an `explore` stance, the Researcher's novelty hint names the exact
    # uncovered regions ("0 coverage in {negatives/external-mining, distillation} — go there") instead
    # of the vague "broaden". OPT-IN (default off): it only enriches an already-mode-gated explore
    # hint, never forces exploration, and no-ops for a task with no curated concept skeleton. Audit +
    # prompt-cue only; never touches selection. See search/concept_graph.py.
    concept_pivot: bool = False
    # PART IV Phase 2b — D3 graded novelty into the LIVE novelty gate (§21.4/§21.13). When on and the
    # task has a curated concept skeleton, the novelty gate first grades a proposal over the concept
    # graph: a level-4 "same direction, DIFFERENT implementation" or a level-5 "re-opens a
    # wrongly-abandoned FAILED direction" proposal is ALLOWED through unchanged — the flat LLM/semantic
    # dedup gate can't tell "this DCL tweak" from "the whole DCL branch" and would wrongly reject a
    # legitimate variant or never re-open a sound-but-killed direction (the node_63 archetype). Levels
    # 1/2/3 (identical / near-dup / prior-run) still fall through to the existing gate. Deterministic
    # (heuristic tagger, no LLM), audit event `novelty_graded`, replay-safe. OPT-IN (default off);
    # no-ops for a task with no skeleton. See search/graded_novelty.py.
    graded_novelty: bool = False
    # PART IV Phase 2b — D7 capability-expansion forced-jump DIRECTIVE (§21.8/§21.13, issue #7). When on
    # and the concept-graph cadence detects action-space LOCK-IN (the search has stayed inside one D5
    # branch for a long consecutive streak) on an `explore` stance, the Researcher's novelty hint
    # ESCALATES from "broaden" to "expand the ACTION SPACE — build the missing infra (external mining,
    # a new data pipeline, a different eval), do NOT swap another {locked} lever." Prompt-cue only, on
    # top of the already-mode-gated explore hint; never adds a scored policy action and never touches
    # selection (the SCORED capability-expansion operator waits on the R1/SearchFitness gates, §21.13).
    # Reads the 2a concept-coverage snapshot, so it needs `concept_pivot` to record one. OPT-IN
    # (default off). See engine/proposal_cues.py + search/lock_in.py.
    capability_expansion: bool = False
    # PART IV cross-run Step 0 (§21.20.12/§21.20.13). Universal task-fingerprint tokenization: drop the
    # ASCII-only `[a-z0-9]` allowlist on goal keywords so a non-Latin goal (Russian, CJK, …) is NOT
    # silently dropped from its cross-run fingerprint (today such a goal keys only on kind/dir/metric and
    # never reaches SIMILAR-task priors/lessons/cases). Uses `[^\W_]+`/`.casefold()` — same splitting as
    # before, any script. OPT-IN (default off) because it changes which stored fingerprints a LIVE run
    # matches; a running portfolio must not silently re-key mid-flight. See engine/memory.py.
    fingerprint_universal: bool = False
    # PART IV cross-run Step 2 (§21.20). Fill graded-novelty's `prior_concepts` from the cross-run
    # ConceptCapsuleStore: at run end write a per-run concept capsule (the shipped `node_concepts` tags +
    # best outcome, keyed by task_fingerprint) to `memory_dir`; when a later SIMILAR run proposes an idea
    # whose concept was tried before, SURFACE the prior outcome as a `cross_run_prior` audit event (never
    # reject — D3 level 3). Audit-only, off the selection path; OPT-IN (default off). Needs a `memory_dir`
    # to share capsules. See engine/memory.py (ConceptCapsuleStore) + engine/novelty.py.
    # PREREQUISITES (CODEX — this flag is NOT standalone): the WRITE needs per-run concept tags
    # (`node_concepts`, produced by `concept_pivot`/F1 tagging) — no tags => no capsule; the SURFACE path
    # runs inside the graded-novelty precheck, so it also needs `graded_novelty` on. With only this flag +
    # `memory_dir` it persists/surfaces nothing. Enable `concept_pivot` + `graded_novelty` alongside it.
    # A non-Latin (Russian/CJK) portfolio should ALSO set `fingerprint_universal`, else the capsule's goal
    # tokens are dropped and its fingerprint over-matches on kind/dir/metric alone (the novelty direction
    # gate narrows this, but goal-word similarity is still lost).
    cross_run_concepts: bool = False
    # PART IV cross-run Step 5 advisory (§21.20.5). Fold the bounded cross-run CONTEXT PACK — evidence-
    # grounded claims with BOTH support and counter-evidence (Step 4) + a portfolio-coverage line (Step 3) —
    # into the Researcher's proposal prompt, exactly like the E4 cross-run prior note. Advisory ONLY: it is
    # prompt-grounding, never touches node selection (§21.7). Reads `memory_dir` (lessons.jsonl +
    # concept_capsules.jsonl); "" when empty. OPT-IN (default off) — the gated flip of Step 2 from audit-only
    # to a live prompt cue; measure the effect via a frozen A/B before defaulting on. See engine/proposal_cues.py.
    cross_run_advisory: bool = False
    # Role backend (ADR-7/14): "toy" (offline optimizer) | "llm" (live model).
    backend: str = "toy"
    # Developer backend (ADR-7): "default" (templated/LLM from the task) or an external
    # CLI coding agent: "opencode" | "aider" | "goose" | "continue".
    developer_backend: str = "default"
    # C2 best-of-N: generate N candidate implementations per node and keep the best by an
    # execution-free reward (static validity + metric-print). 1 = off. In-house LLM developer only
    # (not external agents — ADR-7 cost rule). The top SWE-bench reliability lever for weak models.
    best_of_n: int = 1
    # D10 list-wise best-of-N selection: when the top static-scorers TIE, break the tie with a
    # comparative LLM selection over the candidates presented together (+~3 pts vs pointwise on
    # GAIA, arXiv:2506.12928). The static score stays the primary filter — the LLM is a weak
    # comparative prior, never the sole oracle. ON by default (no-op when best_of_n==1).
    best_of_n_listwise: bool = True
    # === Foresight / predict-before-execute ===============================================
    # FOREAGENT predict-before-execute (arXiv:2601.05930): use the LLM as an IMPLICIT WORLD MODEL to
    # predict — WITHOUT executing — which candidate / hypothesis will score best, primed with a
    # Verified Data Analysis Report (data profile / brief) + the accumulated experiment memory. Master
    # switch, ON by default; a no-op wherever there is nothing to rank (best_of_n==1 and
    # foresight_panel==1) or no LLM client. See `looplab/search/foresight.py`.
    foresight: bool = True
    # Hypothesis foresight breadth: generate K candidate ideas per proposal and keep the one PREDICTED
    # best pre-execution by the world model — it ranks the STRUCTURAL / text ideas the numeric k-NN
    # surrogate (researcher_panel) is blind to, primed with the data profile + memory (the synergy).
    # >1 enables it (LLM backend only; needs `foresight` on); 2 = on by default at modest cost, 1 = off.
    foresight_panel: int = 2
    # AGENTIC foresight: run the ranking (hypothesis-board prioritization + K-idea pick) as a TOOL-USING
    # loop that can pull actual experiment results / data facts before deciding, instead of a one-shot
    # prediction from a pre-baked report. ON by default (needs foresight + a client; falls back to the
    # one-shot predictor on any hiccup). A few extra LLM calls per proposal; set False for the cheaper
    # single-call ranker.
    foresight_agentic: bool = True
    # §1 foresight confidence gate: the minimum predicted confidence at which a predict-before-execute
    # pick is ACTED ON. Below it the ranker abstains — the K-idea panel falls back to the first
    # proposal, best-of-N falls through to the D10 tie-break — instead of committing a low-confidence
    # choice. 0.0 (default) = OFF (act on every pick, the historical behavior); raise toward ~0.5 to
    # make the world model defer when it isn't sure. Pairs with the foresight track record (§1) the
    # predictor is now primed with. Range 0.0-1.0.
    foresight_min_confidence: float = 0.0
    # PART IV Phase 2c — replace the world model's SELF-REPORTED confidence (measured Pearson≈0 with the
    # realized outcome, §21.12) with a CALIBRATED §12-verifier score. When on and a client is available,
    # after the K-idea ranker picks the predicted-best candidate the §12 verifier scores it (grounded +
    # repeated + criteria-decomposed: `foresight_criteria` — likely to improve the objective, and
    # sound/feasible) and THAT calibrated score becomes the confidence the `foresight_min_confidence` gate
    # and the telemetry use. Degrades to the self-reported confidence without a client or on any verifier
    # error (the telemetry records which source was used). OPT-IN (default off); a few extra LLM calls per
    # acted-on proposal. See looplab/trust/verifier.py + search/foresight.py.
    foresight_verify: bool = False
    # Verifier sample count for `foresight_verify` (the §12 repeated-sampling expectation on a no-logprob
    # backend). 3 tames the single-shot variance §21.12 measured; 1 = single-shot (cheaper, noisier).
    foresight_verify_samples: int = 3
    # T7 LLM response cache: serve an identical DETERMINISTIC (temperature 0) request from an
    # in-process content-addressed cache instead of re-hitting the model — cuts cost on
    # retry/panel/verify flows. NEVER caches sampling calls (temp>0: best-of-N / panel / novelty
    # re-propose must vary). Off by default (most role calls run at temperature>0 anyway).
    llm_cache: bool = False
    agent_cmd: str | None = None  # override the agent's launcher (path / wrapper)
    # External-agent validation (ADR-7): wrap a CLI-agent Developer with a validator that
    # audits each output (no-op/syntax/crash/timeout), retries with feedback, and falls
    # back to the in-house LLM Developer. Off -> use the raw agent output unchecked.
    validate_agent: bool = True
    agent_max_retries: int = 1    # re-prompts of the agent on an invalid result
    # Patch-gated multi-file agent (ADR-7 Rule 3): run the CLI agent in a git worktree and
    # accept only edits whose paths match `agent_surface` (reject-not-strip out-of-surface
    # touches). Lets the agent create helper modules, not just solution.py. Degrades to
    # whole-file readback if git is unavailable.
    agent_patch_gate: bool = True
    agent_surface: list[str] = ["*.py"]   # edit-surface allow-list (globs)
    # Trust policy for an agent-authored eval/metric adapter (RepoTask onboarding, Phase 3):
    # "ratify_freeze" (human confirms once, then frozen+protected) | "autonomous" |
    # "ratify_freeze_drift". Selected per project. Not yet enforced (Phase 1 uses an
    # explicit operator-written eval_spec).
    eval_trust_mode: str = "ratify_freeze"
    # RepoTask node seeding policy (run-wide fallback; a task/editable `seed_mode` overrides):
    # "auto" (default) copies git-tracked files when the editable is a git repo (so a tree bloated
    # with untracked artifacts isn't deep-copied per node), else a full copy; "tracked" forces
    # code-only; "all" forces a full recursive copy (legacy). Genesis authors per-task from the
    # user's words; this is the global default when unspecified.
    seed_mode: str = "auto"
    llm_model: str = "qwen3:8b"
    # === LLM / transport ==================================================================
    llm_base_url: str = "http://localhost:11434/v1"  # Ollama OpenAI-compatible endpoint
    llm_temperature: float = 0.6
    # H3 per-role model presets (5090 recipe): optionally run the Researcher and Developer on
    # DIFFERENT models/endpoints (e.g. Developer=Qwen3-Coder-30B for code, a fast model for breadth).
    # Blank = use the shared llm_model/llm_base_url. Generalizes the role-backend swap to per-role.
    researcher_model: str | None = None
    developer_model: str | None = None
    researcher_base_url: str | None = None
    developer_base_url: str | None = None
    # Per-role sampling temperature (§4.1): the Researcher wants breadth (higher temp = more diverse
    # ideas), the Developer wants determinism (lower temp = fewer syntax slips), the Strategist a
    # steady hand. Each overrides the shared `llm_temperature` for that role only; None = use the
    # shared value (byte-identical to before). Flat + nullable, mirroring the per-role model fields.
    researcher_temperature: float | None = None
    developer_temperature: float | None = None
    strategist_temperature: float | None = None
    # Unified self-driving agent: one LLM identity that plays Researcher + Developer (+ Strategist)
    # across pipeline stages, choosing its own model/toolset per stage. ON by default — the agent
    # drives the loop (incl. crash triage: repair/abandon/reject_idea) and, via
    # `agent_drives_actions`, picks the next macro action within a pure legal-action gate. Set both
    # to False for the legacy byte-identical split-role behavior. (No-op unless backend="llm".)
    unified_agent: bool = True
    agent_drives_actions: bool = True
    # Per-stage model/endpoint overrides for the unified agent. Recognized keys (see
    # tasks.build_unified_agent): `propose` (researcher), `implement`/`repair` (developer),
    # `strategy`, `pilot`. Unlisted keys are ignored. Empty = the per-role researcher_*/developer_*
    # models, then the shared llm_model. Explicit map wins. No-op unless `unified_agent` is true.
    agent_stage_models: dict = {}
    agent_stage_base_urls: dict = {}
    llm_parser: str = "tool_call"
    # Reasoning/thinking toggle sent IN the request (providers differ — see `llm.reasoning_body`):
    #   Qwen3 on vLLM/SGLang -> chat_template_kwargs.enable_thinking (bool)
    #   OpenAI/Ollama-v1/DeepSeek -> reasoning_effort (low|medium|high|none)
    # `llm_reasoning`: "" = send nothing (server default — current behavior); off = actively disable;
    #   on = enable at default depth; low|medium|high = enable at that effort.
    # Defaults to "high" so the agent reasons before proposing/repairing/triaging (better code, fewer
    # crashes); set "" to send nothing (server default) or "off" to disable. The model's chain-of-thought
    # is CAPTURED either way (reasoning_content field or inline <think>). NOTE: to get a SEPARATE
    # reasoning_content field from SGLang/vLLM, the server also needs `--reasoning-parser qwen3`.
    llm_reasoning: str = "high"
    llm_reasoning_style: str = "auto"    # auto | qwen | effort | none — how to shape the request param
    llm_reasoning_extra: dict = {}       # raw fields merged into the body (escape hatch, e.g. Anthropic thinking)
    # H1: drive structured calls with the endpoint's constrained decoding (vLLM/SGLang guided_json +
    # response_format json_schema) so weak models can't emit invalid JSON. Off for Ollama (default).
    llm_guided_json: bool = False
    # Stream every LLM request (SSE) and reassemble it, so the request `timeout` is an INTER-TOKEN
    # idle timeout rather than a whole-request deadline: a long-but-alive generation is never cut off
    # (tokens keep resetting the timer), while a genuinely STALLED endpoint (no token for `timeout` s)
    # is detected and retried on a fresh connection — the fix for silent multi-minute hangs. Set False
    # to use one blocking read (old per-op timeout semantics) if an endpoint streams badly.
    llm_stream: bool = True
    # LLM IDLE timeout (seconds). Stream mode: the INTER-TOKEN stall limit — a steady generation is
    # never cut off, only a silent endpoint. Non-stream: bounds the whole read. Raise for endpoints
    # with a long prefill on huge prompts; lower to fail fast on a flaky shared endpoint.
    llm_timeout: float = Field(default=180.0, gt=0)
    # First-byte (response-headers) window for STREAM attempts, seconds: an SSE response sends its
    # headers on admission, so no-headers ≈ a black-holed request — fail over to a fresh connection
    # fast instead of waiting the full idle timeout. Clamped to llm_timeout. Non-stream attempts are
    # NOT bounded by this (their headers arrive only after the whole generation). The default mirrors
    # `DEFAULT_HEADER_TIMEOUT_S` in looplab/core/llm.py is the single source (imported above).
    llm_header_timeout: float = Field(default=DEFAULT_HEADER_TIMEOUT_S, gt=0)
    # Honour HTTP(S)_PROXY / NO_PROXY / SSL_CERT_FILE env vars for LLM calls. Default False: the
    # transport connects DIRECTLY (many internal/OLAP endpoints are reachable only by bypassing an
    # ambient proxy — a picked-up proxy yields "connection refused"). Set true when the endpoint is
    # reachable ONLY via a corporate proxy or needs a custom CA bundle from the environment.
    llm_trust_env: bool = False
    # H4/C2: compact the growing agentic tool-call history once it exceeds this many chars (auto_summary
    # LLM-summarizes the stale middle; else middle-truncate). Sized to the model's real context window so
    # files read earlier STAY in context instead of being compacted away and re-read: ~1,000,000 chars ≈
    # 250k tokens (≈4 chars/token), matching the configured deepseek-v4-flash (≥200k tokens confirmed).
    # The old 120k-char (~30k-token) default was ~8× too aggressive and drove a read-thrash loop. 0 = off.
    context_budget_chars: int = 1_000_000
    # M5: the Researcher's always-on experiments digest budget (chars). 0 = AUTO — scales with the
    # run size (60 chars/node, bounded [1200, 6000]) instead of one flat cap for an 8-node toy run
    # and a 200-node benchmark run alike. Set a positive value to pin it.
    digest_char_cap: int = Field(default=0, ge=0)
    # D11 compression model slot (open_deep_research's four-slot pattern): a dedicated CHEAP model
    # for agent-history summarization (C2 auto-summary), so compression doesn't pay the main
    # model's price. Blank = use each loop's own model (legacy). `compressor_base_url` blank =
    # the shared llm_base_url.
    compressor_model: str | None = None
    compressor_base_url: str | None = None
    # === Agentic tool-loop ================================================================
    # Agentic tool-loop limits — apply to EVERY tool-using agent (the LLM Researcher, the unified
    # agent's pilot + crash-triage, Deep-Research, the run-chat Boss, the genesis repo-scout, and the
    # cross-run report synthesizer). The loop lets the model call read-only tools across turns before
    # emitting its structured result. Both default to UNLIMITED so the agent takes as many turns / as
    # long as it needs — it is never cut off mid-reasoning; set a positive value here (or per-run, or
    # in the UI settings) to bound latency/cost. These used to be hardcoded per call site (e.g. the
    # boss at max_turns=3 / 45s), which silently dropped a slow reasoning model to a no-op reply.
    agent_max_turns: int = 0           # max tool turns before the emit is forced (0 = unlimited)
    agent_time_budget_s: float = 0.0   # wall-clock ceiling across the loop's turns (0 = no cap)
    # G · Soft convergence SAFETY NET (not a research cap). Two thresholds on the agent's tool-turn count:
    # at `agent_emit_after` the loop NUDGES the agent once ("you've investigated enough — emit now"); at
    # `agent_emit_force` it FORCES the emit from what was gathered. Set GENEROUSLY — a focused researcher
    # may legitimately read many files/experiments before it can propose a grounded idea; these only catch
    # a genuine runaway that keeps issuing DIFFERENT calls forever (which stuck-detection, keyed on
    # repeats, misses). NB: the old 499-read runaway (node 63) was 79% REDUNDANT re-reads caused by a
    # broken read_file — fixing read_file (pagination) is the real cure; this is just insurance. 0 = off.
    agent_emit_after: int = 300
    agent_emit_force: int = 500
    # B1 · No-progress / stuck detection — the safety net that makes "unlimited turns" safe. The
    # turn/time ceilings above are only BACKSTOPS; this is what actually stops a runaway loop, on the
    # cheapest signal: when the model repeats the SAME tool call (or ping-pongs between two, or keeps
    # hitting the SAME error) with no progress, the loop forces the final emit and finishes. ON by
    # default; reading DIFFERENT files or running ONE long command never trips it. (OpenHands-style.)
    agent_stuck_detection: bool = True
    agent_stuck_repeat: int = 4        # identical calls in a row that count as "stuck" (>=2)
    agent_stuck_alternate: int = 4     # ping-pong cycles between two calls that count as "stuck" (>=2)
    # C1 · Self-plan (TodoWrite-style): expose an `update_plan` tool so a long-running agent keeps its
    # OWN working TODO and re-surfaces it every `agent_plan_reinject_every` turns — keeps the goal in
    # view across a long loop so the agent "writes its own context" (Claude Code style). ON by default.
    agent_self_plan: bool = True
    agent_plan_reinject_every: int = 5
    # C2 · Auto-summary (ON by default): when the tool-loop history grows long, LLM-summarize the
    # stale middle instead of only middle-truncating it. The trigger is `context_budget_chars` if set,
    # else a built-in high-water mark (context_budget.DEFAULT_SUMMARY_CHARS ≈ 120k chars) so short
    # loops are untouched. Falls back to truncation on any summarizer error; one extra model call per
    # over-budget turn.
    agent_auto_summary: bool = True
    # C4 · Repo-developer plan decomposition. A big feature attempted in ONE developer session made a
    # non-converging reasoning model loop indefinitely — it kept writing + exploring without ever
    # emitting `done` (10k+ LLM calls / hours; agentic stuck-detection can't catch VARIED exploration,
    # only literal repeats). Fix: the developer first proposes an ordered plan of ATOMIC steps; a
    # multi-step plan is executed step-by-step, each in a FRESH bounded session with only that step's
    # scope + the files accumulated so far, syntax-validated per write. Every session stays
    # small-context and convergent. ON by default for repo tasks (a 1-step plan == the old single pass).
    developer_plan_decompose: bool = True
    developer_plan_min_steps: int = 2      # a proposed plan with >= this many steps runs step-by-step
    developer_plan_max_steps: int = 8      # cap on plan length (a runaway planner can't spawn 100 steps)
    # Hard per-session backstop for the repo developer — the ONE write-heavy agent that can run away
    # even with stuck-detection on (varied writes/reads never trip the repeat/ping-pong signal). Bounds
    # the plan phase, EACH step, and the single-session fallback. Unlike the global agent_* limits
    # (0 = unlimited, meant for read-only agents), the developer always gets a FINITE ceiling so a
    # model that never emits `done` fails cleanly with the code it wrote so far, instead of looping.
    developer_session_max_turns: int = 500
    developer_session_time_budget_s: float = 1200.0  # 20 min wall-clock per developer session
    # Phase-handoff summaries. Each LLM phase in a node build (Researcher·propose → Developer·stages →
    # plan → implement) ends with ONE extra LLM call that distills its transcript — the repo structure
    # it mapped, files/data confirmed, decisions made — into a brief injected into the NEXT phase (even
    # across the role boundary). The next phase TRUSTS that instead of re-reading the same files, which
    # cuts the biggest source of tool-call thrash (stages/plan/implement each re-exploring the repo).
    # One summary call per phase in exchange for many fewer read calls downstream. ON by default.
    phase_handoff_summary: bool = True
    # Observability (ADR-17): capture each LLM call's full prompt + completion into the active
    # span (spans.jsonl) so the UI can show exactly what the model read and wrote per node.
    # Diagnostics only — `replay.fold` never reads spans. Default on for local single-user; set
    # LOOPLAB_TRACE_LLM_IO=0 to suppress (e.g. to keep spans.jsonl small or avoid storing prompts).
    trace_llm_io: bool = True
    # Run-introspection tools (ON by default): make the LLM Researcher (and DeepResearcher) a
    # tool-using agent that can read its OWN experiments (list/read/find-analogous/themes/code) and
    # the task data (schema/profile/asset) mid-loop, instead of seeing only best+parent. Advisory —
    # never changes best-selection. Off = the legacy single-shot Researcher (richer digest still added).
    researcher_tools: bool = True
    # Cross-run introspection: give the agentic Researcher / pilot read-only tools to look at
    # SIBLING runs (same task_id, same run-root) — list them, read an experiment / its code, and
    # find analogous configs across runs — so a run can build on what neighbouring runs already
    # learned instead of rediscovering it. Advisory; never changes best-selection. Needs the run's
    # own dir wired through (no-op for unit-built roles). Off = the legacy single-run view only.
    cross_run_tools: bool = True
    # Read-only access to EVERY run on this machine ACROSS ALL TASKS (not just same-task siblings):
    # the Developer/Researcher get list_all_runs + read_run_code + read_run_experiment so they can read
    # the code + result of ANY past experiment anywhere and reuse an approach. Broader than
    # `cross_run_tools` (same-task only); the agent decides when a foreign run is relevant. Advisory;
    # never changes best-selection. Needs the run's own dir wired through (no-op for unit-built roles).
    all_runs_tools: bool = True
    # PART V §22 — read-only CROSS-RUN KNOWLEDGE tool for the reasoning roles (Researcher, Strategist,
    # deep-research, and a Developer-scoped variant). Adds `cross_run_prior_attempts` / `cross_run_claims`
    # / `cross_run_atlas` over the §21.20 read-models (concept overview + claim assessments + atlas), read
    # from `memory_dir` (lessons.jsonl + concept_capsules.jsonl). ADVISORY ONLY — an agent may cite what it
    # finds but never mutates cross-run truth (facts are engine-written, verdicts operator-ratified, §22.4).
    # OPT-IN (default off) because the cross-run stores only exist once the Part-IV features have run.
    cross_run_read_tools: bool = False
    # Agentic retrieval (ADR-16): if set, the LLM Researcher gets grep/kb_search/read
    # tools over this directory of markdown notes and chooses when to use them.
    knowledge_dir: str | None = Field(default_factory=lambda: str(_LL_HOME / "knowledge"))
    # T4 real embeddings: model id for an OpenAI-compatible `/embeddings` endpoint used for
    # kb_search / case retrieval. Blank (default) = the dependency-free lexical `hash_embed` (today's
    # behavior). Set e.g. "nomic-embed-text" (Ollama) or "text-embedding-3-small" for SEMANTIC
    # retrieval; a misconfigured/offline endpoint degrades back to hash_embed at call time (never
    # crashes a run). `embed_base_url` blank = reuse `llm_base_url`; the shared `llm_api_key` is used.
    embed_model: str | None = None
    embed_base_url: str | None = None
    # Harmonic memory (Memora, ICML'26 — idea import). When ON, cross-run cases and knowledge notes are
    # indexed by a short ABSTRACTION + cue ANCHORS instead of their raw text, near-duplicate memories are
    # CONSOLIDATED into one entry under a matching abstraction, and `kb_search`/case retrieval EXPAND
    # through the top hits' anchors to surface related-but-not-similar memories. ON by default. Set
    # `memora=false` to restore the pre-Memora raw-text index. Never a source of truth — abstractions
    # live only in the derived, rebuildable retrieval index (never the event log or canonical cases.jsonl).
    memora: bool = True
    # Write abstractions with the wired chat model (richer than the deterministic lexical fallback). ON
    # by default; results are CACHED by content hash (see `memora_cache`) so a re-built index doesn't
    # re-call the model on unchanged notes/cases, and any endpoint failure degrades to lexical at call
    # time — so an offline box just gets lexical abstractions, never a crash. Set false to force lexical.
    memora_llm: bool = True
    # === Cross-run memory / Memora ========================================================
    memora_anchors: int = 6           # max cue anchors kept per memory
    # Cosine similarity at/above which a new case is CONSOLIDATED into an existing one (same evolving
    # topic) rather than stored as a separate entry. Only used when `memora` is on.
    memora_consolidate_threshold: float = 0.86
    # Where the LLM-abstraction cache lives (JSON). Blank (default) derives it from `memory_dir`
    # (`<memory_dir>/memora_cache.json`) or else `knowledge_dir` (`<knowledge_dir>/.memora_cache.json`);
    # with neither set the cache is in-memory only (per process). No-op unless `memora_llm` is on.
    memora_cache: str | None = None
    # E3 literature-grounded ideation (network-OPTIONAL): give the agentic Researcher an arXiv search
    # tool to ground ideas in real techniques. Off by default (egress is unreliable on some boxes);
    # fails gracefully if the network is blocked.
    literature_search: bool = False
    # Deep-Research stage (network-OPTIONAL): give the DeepResearcher a general web search/fetch
    # tool (DuckDuckGo) on top of arXiv to read across results + the web before steering the next
    # batch. Off by default (egress is unreliable on some boxes); fails gracefully when blocked.
    web_search: bool = False
    # Cadence for the Deep-Research stage: run it automatically every N created nodes (0 = off; it
    # still fires on a manual `deep_research` control event or a Strategist `request_research`).
    # Default 3: deep research analyzes the accumulating results and steers the next batch (its
    # directions become hints + open hypotheses); paired with `concurrent_research` it overlaps the
    # think with the GPU-bound eval, so the cadence costs LLM tokens but no wall-clock on the search.
    deep_research_every: int = 3
    # Overlap a DUE deep-research "think" with the GPU-bound eval instead of running it in its own
    # serial step (the agent is otherwise idle while a node trains). research() is pure compute on a
    # state snapshot — the engine still records the memo as the sole writer AFTER the eval, so the
    # event log stays single-writer. ON by default: the LLM is typically remote (no GPU contention
    # with eval), so overlapping is a free intelligence win over the long per-node training time.
    concurrent_research: bool = True
    # Cadence for the agent-authored run report: regenerate the conclusion-first narrative every N
    # created nodes (0 = off; it still regenerates on a manual `report_refresh` from the UI). The
    # deterministic report always renders from the node set regardless of this knob.
    report_every: int = 3
    # Agent Skills (I18, ADR-9): dir of SKILL.md the Researcher can list/load as tools.
    skills_dir: str | None = None
    # Prompt store (I18, ADR-8): dir of editable, hot-reloaded role prompt .md files.
    prompt_dir: str | None = None
    # API key as a reference, never serialized as a value (ADR-11). Local servers
    # ignore it; default to a placeholder.
    llm_api_key: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def _apply_profile(cls, data):
        """Expand `profile` into a bundle of setting defaults BEFORE validation.

        pydantic-settings hands this validator the fully-merged input from ALL explicit sources
        (init kwargs + LOOPLAB_* env + .env), with fields the user left unset simply ABSENT (they
        fall to field defaults afterward). So a profile value is applied only when its key is not
        already present — i.e. any explicit file/CLI/env setting wins over the profile, and the
        profile wins over the bare field default. This makes `thorough` a true convenience preset,
        never an override of what the user asked for."""
        if not isinstance(data, dict):
            return data
        prof = data.get("profile", "default")
        # env may deliver the key as "profile" (env_prefix stripped) — normalize to a str
        prof = str(prof).strip().lower() if prof is not None else "default"
        overrides = PROFILES.get(prof)
        if overrides is None:
            raise ValueError(f"unknown profile {prof!r}; known: {sorted(PROFILES)}")
        for key, val in overrides.items():
            if key not in data:          # explicit source (file/CLI/env) always wins
                data[key] = val
        return data

    @model_validator(mode="after")
    def _check_trust_gate(self):
        if self.trust_gate not in ("audit", "gate", "block"):
            raise ValueError(
                f"trust_gate must be audit|gate|block, got {self.trust_gate!r}")
        if self.merge_mode not in ("auto", "mean", "ensemble"):
            raise ValueError(
                f"merge_mode must be auto|mean|ensemble, got {self.merge_mode!r}")
        # novelty_mode drives the dedup/novelty gate; an out-of-set value (e.g. a mis-cased
        # LOOPLAB_NOVELTY_MODE=LLM, or "on") used to fall through _apply_novelty_gate silently as a
        # NO-OP — turning the gate off with no diagnostic. Fail loudly at config time like trust_gate/
        # merge_mode do. (Other enum-ish string fields — seed_mode/eval_trust_mode/strategist_backend —
        # share the pattern; left unvalidated here only because their full value sets are less settled.)
        if self.novelty_mode not in ("off", "algo", "llm"):
            raise ValueError(
                f"novelty_mode must be off|algo|llm, got {self.novelty_mode!r}")
        # The remaining enum-ish string fields (arch-review §5 P3): a typo used to be accepted at
        # construction and only fail-safe/later-loud downstream (e.g. a mis-cased strategist_backend
        # silently ran the default). Fail loudly here like the fields above.
        if self.strategist_backend not in ("off", "rule", "llm", "agent"):
            raise ValueError(
                f"strategist_backend must be off|rule|llm|agent, got {self.strategist_backend!r}")
        if self.eval_trust_mode not in ("ratify_freeze", "autonomous", "ratify_freeze_drift"):
            raise ValueError(
                "eval_trust_mode must be ratify_freeze|autonomous|ratify_freeze_drift, "
                f"got {self.eval_trust_mode!r}")
        if self.seed_mode not in ("auto", "tracked", "all"):
            raise ValueError(f"seed_mode must be auto|tracked|all, got {self.seed_mode!r}")
        return self

    def masked_snapshot(self) -> dict:
        d = self.model_dump()
        if self.llm_api_key is not None:
            d["llm_api_key"] = "***"
        else:
            d["llm_api_key"] = None
        return d
