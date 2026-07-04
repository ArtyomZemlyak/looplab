"""Engine config (I0, ADR-11). pydantic-settings: env override (LOOPLAB_*) over
defaults. A resolved, secret-masked snapshot is written next to each run for
reproducibility. (No real secrets in P0, but the masking discipline is in place.)
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
        "novelty_gate": True,
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


class Settings(BaseSettings):
    # Config sources, highest priority first: explicit init kwargs (e.g. Settings(**snapshot)) >
    # real OS env vars (LOOPLAB_*) > a `.env` file in the CWD > field defaults. The `.env` is read
    # so `looplab run`/`looplab ui` pick up LOOPLAB_LLM_BASE_URL etc. without exporting them by hand
    # (keys MUST carry the LOOPLAB_ prefix, same as the env vars). utf-8-sig tolerates a BOM from
    # Windows editors. The test suite disables this (tests/conftest.py) so a dev's real .env in the
    # repo root can't leak into assertions.
    model_config = SettingsConfigDict(
        env_prefix="LOOPLAB_",
        env_file=".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )

    # Run profile (config-first preset, see PROFILES above). "default"/"fast" = today's lean
    # defaults; "thorough" turns the built quality/trust machinery ON in one word. A profile only
    # fills fields the user did NOT set — every knob it touches stays individually overridable.
    profile: str = "default"

    # Lower bounds (review C/⚪): `max_parallel=0` silently stalls the loop; a non-positive
    # node/seed budget or timeout is never valid. Reject at config time, not mid-run.
    n_seeds: int = Field(default=3, ge=1)
    max_nodes: int = Field(default=8, ge=1)
    # Concurrent evaluation is a backlog seam. The base/primary mode is a single
    # experiment at a time (deterministic, one eval process running) — important for
    # RepoTask where an eval is a real training run with its own resources/trackers.
    # Set > 1 to opt into the parallel fan-out (the task-group path).
    max_parallel: int = Field(default=1, ge=1)
    timeout: float = Field(default=30.0, gt=0)
    # Intra-node sweep: a sweep node runs a whole grid in one process, so it gets this multiple of
    # `timeout` as its wall-clock budget (solution.py path; RepoTasks use their per-profile timeout).
    sweep_timeout_mult: float = Field(default=8.0, ge=1)
    # Sandbox tier (ADR-13): "trusted_local" (subprocess, no Docker) for the CLI;
    # "untrusted" (Docker --network none -> gVisor) for hosted/multi-tenant UI.
    trust_mode: str = "trusted_local"
    # Image for the untrusted command-eval tier (RepoTask, Phase 4): the framework's deps
    # should be baked in (the container runs --network none, so a pip setup can't fetch).
    docker_image: str = "python:3.12-slim"
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
    # then fix" directive on debug, not just the raw stderr tail. Off by default.
    deep_repair: bool = True
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
    # snapshot/UI. Defaults are conservative: resource/search-shape knobs the agents already reason about;
    # infra (llm_*, trust_mode, api_key, docker_image) stays locked. The UI shows S/B/R pills per setting.
    # `timeout` granted to the researcher IS the "auto" per-node mode — the researcher sizes each
    # experiment's wall-clock (Idea.eval_timeout); the config `timeout` is the fallback for nodes it
    # doesn't size. Env override expects a JSON object.
    agent_control: dict[str, list[str]] = Field(default_factory=lambda: {
        "timeout": ["researcher", "strategist"],
        "max_parallel": ["strategist"],
        "max_nodes": ["strategist", "boss"],
        "max_eval_seconds": ["strategist", "boss"],
        "policy": ["strategist"],
        "n_seeds": ["strategist"],
        "ablate_every": ["strategist"],
        "merge_mode": ["strategist"],
        "complexity_cue": ["strategist"],
        "ablate_code_blocks": ["strategist"],
        "fidelity": ["strategist", "researcher"],
    })
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
    # E1 novelty/dedup gate: reject near-duplicate proposals (normalized param-space distance below
    # `novelty_epsilon`) by deterministically nudging them off the duplicate — stops the search
    # wasting evals re-trying the same idea. Audit event `novelty_rejected`. Off by default.
    novelty_gate: bool = False
    novelty_epsilon: float = 0.05
    # T5 semantic novelty (Phase 2, needs novelty_gate): ALSO reject a proposal whose idea TEXT
    # (rationale+hypothesis) embeds within `novelty_semantic_threshold` cosine of an existing
    # node's — with one informed re-propose surfacing the duplicate's outcome ("you tried X, it
    # failed because Y"). ShinkaEvolve's ablation ranks duplicate-rejection-before-eval above
    # model routing. Uses the T4 embedder (hash_embed fallback, zero-dep).
    novelty_semantic: bool = True
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
    #   "gate"  = a flagged node can no longer be selected as BEST (it stays in the tree; the search
    #             may still repair/improve it into a clean version) — closes the "a hacked/leaky node
    #             can win" hole;
    #   "block" = additionally mark it infeasible so the policy won't breed from it either.
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
    # every choice the Strategist makes is also a direct knob. Defaults to "llm" so the agent adapts
    # the search policy/operators/fidelity per situation; set "off" for static config-driven search.
    # "agent" = a tool-using Strategist that READS the run/data/sibling-runs/KB/memory (B1-guarded)
    # before deciding, instead of the single-shot "llm" call over aggregate stats.
    strategist_backend: str = "llm"            # "off" | "rule" | "llm" | "agent"
    strategist_every: int = Field(default=3, ge=1)   # consult cadence (created nodes)
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
    llm_base_url: str = "http://localhost:11434/v1"  # Ollama OpenAI-compatible endpoint
    llm_temperature: float = 0.6
    # H3 per-role model presets (5090 recipe): optionally run the Researcher and Developer on
    # DIFFERENT models/endpoints (e.g. Developer=Qwen3-Coder-30B for code, a fast model for breadth).
    # Blank = use the shared llm_model/llm_base_url. Generalizes the role-backend swap to per-role.
    researcher_model: str | None = None
    developer_model: str | None = None
    researcher_base_url: str | None = None
    developer_base_url: str | None = None
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
    # H4: cap the growing agentic-researcher tool-call history (chars) by middle-truncating stale
    # tool output, so a long trace doesn't blow the context window. 0 = off (unbounded).
    context_budget_chars: int = 0
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
    # Agentic tool-loop limits — apply to EVERY tool-using agent (the LLM Researcher, the unified
    # agent's pilot + crash-triage, Deep-Research, the run-chat Boss, the genesis repo-scout, and the
    # cross-run report synthesizer). The loop lets the model call read-only tools across turns before
    # emitting its structured result. Both default to UNLIMITED so the agent takes as many turns / as
    # long as it needs — it is never cut off mid-reasoning; set a positive value here (or per-run, or
    # in the UI settings) to bound latency/cost. These used to be hardcoded per call site (e.g. the
    # boss at max_turns=3 / 45s), which silently dropped a slow reasoning model to a no-op reply.
    agent_max_turns: int = 0           # max tool turns before the emit is forced (0 = unlimited)
    agent_time_budget_s: float = 0.0   # wall-clock ceiling across the loop's turns (0 = no cap)
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
    deep_research_every: int = 0
    # Overlap a DUE deep-research "think" with the GPU-bound eval instead of running it in its own
    # serial step (the agent is otherwise idle while a node trains). research() is pure compute on a
    # state snapshot — the engine still records the memo as the sole writer AFTER the eval, so the
    # event log stays single-writer. OFF by default: only a win when the LLM is REMOTE (no GPU
    # contention with eval), and it needs a live-run validation before enabling. See ROADMAP/notes.
    concurrent_research: bool = False
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
        return self

    def masked_snapshot(self) -> dict:
        d = self.model_dump()
        if self.llm_api_key is not None:
            d["llm_api_key"] = "***"
        else:
            d["llm_api_key"] = None
        return d
