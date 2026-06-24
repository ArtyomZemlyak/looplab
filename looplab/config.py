"""Engine config (I0, ADR-11). pydantic-settings: env override (LOOPLAB_*) over
defaults. A resolved, secret-masked snapshot is written next to each run for
reproducibility. (No real secrets in P0, but the masking discipline is in place.)
"""
from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOOPLAB_", extra="ignore")

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
    asha_rung_nodes: int = Field(default=4, ge=2)   # candidates batched at rung 0
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
    # A7 Strategist (NEW, user-requested): optional LLM/rule meta-controller that picks the search
    # policy/allocator + operator mix + fidelity (+ Developer backend) per situation. Config-first:
    # "off" (default) == today's behavior, every choice the Strategist makes is also a direct knob.
    strategist_backend: str = "off"            # "off" | "rule" | "llm"
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
    llm_parser: str = "tool_call"
    # H1: drive structured calls with the endpoint's constrained decoding (vLLM/SGLang guided_json +
    # response_format json_schema) so weak models can't emit invalid JSON. Off for Ollama (default).
    llm_guided_json: bool = False
    # H4: cap the growing agentic-researcher tool-call history (chars) by middle-truncating stale
    # tool output, so a long trace doesn't blow the context window. 0 = off (unbounded).
    context_budget_chars: int = 0
    # Observability (ADR-17): capture each LLM call's full prompt + completion into the active
    # span (spans.jsonl) so the UI can show exactly what the model read and wrote per node.
    # Diagnostics only — `replay.fold` never reads spans. Default on for local single-user; set
    # LOOPLAB_TRACE_LLM_IO=0 to suppress (e.g. to keep spans.jsonl small or avoid storing prompts).
    trace_llm_io: bool = True
    # Agentic retrieval (ADR-16): if set, the LLM Researcher gets grep/kb_search/read
    # tools over this directory of markdown notes and chooses when to use them.
    knowledge_dir: str | None = None
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
