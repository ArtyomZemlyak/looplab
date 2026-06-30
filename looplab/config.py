"""Engine config (I0, ADR-11). pydantic-settings: env override (LOOPLAB_*) over
defaults. A resolved, secret-masked snapshot is written next to each run for
reproducibility. (No real secrets in P0, but the masking discipline is in place.)
"""
from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Autonomous roles that may be granted per-setting write access (see Settings.agent_control).
# strategist = A7 meta-controller (run-wide); boss = run-chat operator-proxy (run-wide);
# researcher = per-experiment proposer (per-node sizing, e.g. eval timeout for a heavy model).
AGENT_ROLES = ("strategist", "boss", "researcher")


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
    # ensembling 37.9%->43.9%). Falls back to mean when the Developer can't ensemble.
    merge_mode: str = "mean"
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
    # back into the proposal prompt so the proposer reflects on and avoids repeating them. Off default.
    failure_reflection: bool = False
    # C3 deep test-driven repair: hand the Developer the failure taxonomy + a structured "reproduce
    # then fix" directive on debug, not just the raw stderr tail. Off by default.
    deep_repair: bool = True
    # Hybrid in-node crash repair: when an LLM-generated node CRASHES at runtime (mechanical errors
    # — bad import, removed kwarg, typo), the agent triages it and may repair the code IN PLACE within
    # the same eval (cheap; does NOT consume max_nodes and does NOT add a node to the search tree).
    # Semantic failures still flow to a new idea/debug node via the policy. ON by default; set False
    # to restore the prior behavior (every crash waits for a budgeted inter-node debug node).
    inline_repair: bool = True
    # Max in-place repair attempts per node before it fails normally (and stays eligible for the
    # budgeted inter-node debug operator). 1 = a single repair retry.
    inline_repair_attempts: int = Field(default=1, ge=1)
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
    # E4 reflection-memory -> priors (gradient-free cross-run meta-learning): at run end distill a
    # one-line meta-review (best params/operator) into `<memory_dir>/meta_notes.jsonl`; at run start,
    # inject prior notes for this task into the proposal prompt. Needs memory_dir; off without it.
    reflection_priors: bool = False
    # B3 output redaction: mask credentials (known key shapes + high-entropy tokens) in the
    # stdout/stderr tail before it is persisted to the event log / spans / UI — a leaked secret in a
    # print()/traceback must not land in the durable log. Off by default; recommend on for untrusted.
    redact_output: bool = False
    # B5 reward-hacking detector: a host-side monitor that flags suspicious wins (grader/answer-key
    # access, runtime writes to frozen files, suspiciously-perfect metrics) as a `reward_hack_suspected`
    # audit event in the Trust panel. Never changes selection. Off by default.
    reward_hack_detect: bool = False
    # I3 data-centric: static code-leakage scan of each evaluated solution (fit-before-split,
    # fit-on-test) surfaced into the Trust panel alongside reward-hack flags. Off by default.
    code_leakage_detect: bool = False
    # C4 independent critic: an execution-free critic of each solution (stub / hardcoded-metric /
    # params-ignored; on host-graded tasks the metric checks become a submission-output check)
    # surfaced in the Trust panel. Audit-only. Off by default.
    critic_check: bool = False
    # A7 Strategist (NEW, user-requested): optional LLM/rule meta-controller that picks the search
    # policy/allocator + operator mix + fidelity (+ Developer backend) per situation. Config-first:
    # every choice the Strategist makes is also a direct knob. Defaults to "llm" so the agent adapts
    # the search policy/operators/fidelity per situation; set "off" for static config-driven search.
    strategist_backend: str = "llm"            # "off" | "rule" | "llm"
    strategist_every: int = Field(default=3, ge=1)   # consult cadence (created nodes)
    # Multi-seed confirmation (I12, ADR-15): confirm the top-k under N seeds before
    # finishing. 0 disables (default). Only meaningful when eval has variance.
    confirm_top_k: int = 0
    confirm_seeds: int = 0
    # Budget (I13): hard wall-clock ceiling; the run aborts cleanly when exceeded.
    max_seconds: float | None = None
    # Eval-compute budget (#2): hard ceiling on cumulative time spent INSIDE evals (training
    # runs), separate from wall-clock. Survives resume (summed from the event log). The real
    # guard against a silent multi-hour sweep when an eval is a minutes-hours training run.
    max_eval_seconds: float | None = None
    # Cross-run memory (I19, ADR-10): if set, the best result of each run is stored as
    # a case here, and the cases become retrievable knowledge for future runs.
    memory_dir: str | None = None
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
    # H4: cap the growing agentic-researcher tool-call history (chars) by middle-truncating stale
    # tool output, so a long trace doesn't blow the context window. 0 = off (unbounded).
    context_budget_chars: int = 0
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
    knowledge_dir: str | None = None
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

    def masked_snapshot(self) -> dict:
        d = self.model_dump()
        if self.llm_api_key is not None:
            d["llm_api_key"] = "***"
        else:
            d["llm_api_key"] = None
        return d
