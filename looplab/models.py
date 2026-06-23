"""Domain models + event envelope (I0). Pydantic v2; JSON Schemas derive from these."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class NodeStatus(str, Enum):
    pending = "pending"      # node_created seen, not yet evaluated (resume re-entry point)
    evaluated = "evaluated"  # has a metric
    failed = "failed"        # ran but produced no usable metric


class Idea(BaseModel):
    """A proposed experiment: which operator, what parameters, why."""
    operator: str
    params: dict[str, float] = Field(default_factory=dict)
    rationale: str = ""
    # RepoTask Phase 2: the Researcher may pick which eval profile to run (e.g. cheap
    # "smoke" during search vs "full" on confirm) — eval depth is part of the action space.
    eval_profile: Optional[str] = None
    # Semantic grouping (UI #7): a short, reusable slug the Researcher assigns to cluster related
    # experiments in one search tree (e.g. "loss-fn", "architecture", "regularization"). Optional
    # and audit-only — never affects search/selection; the UI groups nodes by it. Flows through the
    # event log automatically (idea.model_dump → node_created → Idea(**d["idea"]) in replay.fold).
    theme: Optional[str] = None


class Node(BaseModel):
    """A node in the search DAG. `parent_ids` is a list to allow merges (P2)."""
    id: int
    parent_ids: list[int] = Field(default_factory=list)
    operator: str
    idea: Idea
    code: str = ""
    # Multi-file solutions (ADR-7 patch-gated agent): extra in-surface files the agent
    # created/edited besides solution.py. Materialized into the eval workdir. `code`
    # remains the solution.py entrypoint the sandbox runs.
    files: dict[str, str] = Field(default_factory=dict)
    # In-surface files the agent DELETED (patch-gate accepted the deletion). Applied to the eval
    # workdir after the pristine repo is seeded, so an accepted deletion actually takes effect.
    deleted: list[str] = Field(default_factory=list)
    metric: Optional[float] = None
    status: NodeStatus = NodeStatus.pending
    error: str = ""
    # Failure taxonomy (set by node_failed): setup | timeout | crash | no_metric | drift.
    # Audit/observability only — lets a UI/operator see WHY runs fail across a search.
    error_reason: str = ""
    stdout_tail: str = ""
    # Multi-seed confirmation (I12): set by a node_confirmed event. When present,
    # best-selection ranks by confirmed_mean (the robust metric) instead of `metric`.
    confirmed_mean: Optional[float] = None
    confirmed_std: Optional[float] = None
    confirmed_seeds: Optional[int] = None   # how many seeds actually succeeded (I12)
    eval_seconds: Optional[float] = None     # wall-clock of this node's eval (cost accounting #2)
    # Multi-objective (#5): extra reported metrics + unmet hard constraints. `feasible` is
    # False when any constraint was violated — such a node keeps its metric (for the audit
    # trail) but is excluded from best-selection.
    extra_metrics: dict[str, Optional[float]] = Field(default_factory=dict)
    violations: list[dict] = Field(default_factory=list)
    feasible: bool = True
    # External-agent audit (ADR-7): set by an `agent_validated` event when the code was
    # produced by a validated CLI-agent Developer. {"ok": bool, "checks": [...]}.
    agent_report: Optional[dict] = None


class Project(BaseModel):
    """A ClearML-style organizational folder for runs. Projects nest via `parent_id` (None = a
    top-level project). Pure UI metadata stored in `<run-root>/projects.json` — runs never move
    on disk and the engine/event log are untouched (see `projects.ProjectStore`)."""
    id: str
    name: str
    parent_id: Optional[str] = None


class Event(BaseModel):
    """Append-only event envelope = the source of truth ([ADR-1])."""
    v: int = 1            # envelope schema version (ADR-1): lets a future reader migrate old logs
    seq: int = -1
    ts: float = 0.0
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    # Trace correlation (observability): the (trace_id, span_id) active when this event was
    # emitted, so the UI can join the research tree (events) to its execution detail (spans).
    # Diagnostics only — never read by `replay.fold`.
    trace_id: Optional[str] = None
    span_id: Optional[str] = None


class RunState(BaseModel):
    """Pure fold of the event log ([ADR-12]). Never mutated except by `replay.fold`."""
    run_id: str = ""
    task_id: str = ""
    goal: str = ""
    direction: str = "min"  # "min" | "max"
    config_hash: str = ""
    nodes: dict[int, Node] = Field(default_factory=dict)
    best_node_id: Optional[int] = None
    finished: bool = False
    data_profile: Optional[dict] = None   # set by the grounding pre-phase (I16)
    leakage: Optional[dict] = None        # set by the grounding leakage scan (I9)
    stop_reason: Optional[str] = None     # why the run finished (budget/leakage/done)
    confirmed_done: bool = False          # the multi-seed confirmation phase completed (I12)
    awaiting_approval: bool = False       # HITL: approval requested, not yet granted (I21)
    approved: bool = False                # HITL: a human approved the result (I21)
    archive: Optional[dict] = None        # diversity-archive summary at run end (I22)
    # RepoTask onboarding (Phase 3, ADR-7): the agent proposes a trusted eval spec + metric
    # adapter; a human ratifies it once; then the loop trusts it.
    proposed_spec: Optional[dict] = None  # {eval_spec, adapter_files, goal} from the agent
    spec_approval_requested: bool = False
    spec_confirmed: bool = False          # human ratified the proposed eval spec
    # Drift cross-check audit (Phase 4, ratify_freeze_drift): each entry is a divergence the
    # independent reader caught {node_id, primary, cross, tolerance, [seed]}. Audit only —
    # the metric was already discarded (node failed), so this never changes selection.
    drifts: list[dict] = Field(default_factory=list)
    # Workspace reproducibility (item #4): the editable-repo/data fingerprint pinned at
    # run_started, and whether a resume detected the source changed underneath.
    workspace: Optional[dict] = None
    workspace_changed: bool = False
    # Eval-compute budget accounting (#2): cumulative wall-clock spent INSIDE evals (training
    # runs), distinct from the run's total wall-clock (which includes LLM/agent time). The
    # search stops cleanly once this crosses `max_eval_seconds` — guards the silent long sweep.
    total_eval_seconds: float = 0.0
    # Per-seed confirmation results {node_id: {seed: metric|None}} from `confirm_eval` events —
    # lets a crash-interrupted confirm pass RESUME mid-node (skip seeds already run) instead of
    # re-executing every expensive full-profile seed from scratch.
    confirm_seed_results: dict[int, dict] = Field(default_factory=dict)

    # --- live operator control (UI intervention via the event log) ---
    # These are folded from appended CONTROL events (intent). The engine remains the sole writer
    # of DOMAIN events: it reads the intent here and writes the effect (e.g. node_abort -> a
    # node_failed reason="aborted"). All deterministic under replay; audit-only fields never
    # change best-selection.
    paused: bool = False                       # `pause`/`resume`: resumable break (not finished)
    stop_requested: Optional[str] = None       # `run_abort`: reason; loop -> run_finished + break
    aborted_nodes: list[int] = Field(default_factory=list)   # `node_abort`: skip/kill these nodes
    budget_overrides: dict = Field(default_factory=dict)     # `budget_extend`: max_seconds/eval
    pending_hints: list[dict] = Field(default_factory=list)  # `hint`: operator directives to steer
    confirm_requests: list[int] = Field(default_factory=list)  # `force_confirm`: operator robustness ask
    confirmed_forced: list[int] = Field(default_factory=list)   # nodes a forced confirm finished (gate)
    ablate_requests: list[int] = Field(default_factory=list)    # `force_ablate` (wired in Phase 5)
    fork_requests: list[dict] = Field(default_factory=list)     # `fork`: operator-seeded improve
    forks_done: int = 0                        # count of processed forks (replay-safe fulfillment)
    # `inject_node`: an operator-authored experiment hand-added to the tree (a manual idea +
    # optional parent + optional code). The engine materializes each one into a real pending node
    # that the policy then evaluates like any other — so a human can steer the search directly.
    inject_requests: list[dict] = Field(default_factory=list)
    injects_done: int = 0                      # count of processed injects (replay-safe fulfillment)
    annotations: dict[int, list[str]] = Field(default_factory=dict)  # `annotation`: node notes
    promotions: list[dict] = Field(default_factory=list)        # `promote`: solution-registry audit
    champion: Optional[int] = None             # node id the `champion` registry alias points at
    llm_cost: Optional[dict] = None            # run-level LLM cost/token roll-up ({cost,tokens,…})
    ablations: list[dict] = Field(default_factory=list)  # ablate events {parent_id, impacts} (sensitivity)
    policy_scores: dict[int, float] = Field(default_factory=dict)  # latest policy_decision candidate scores
    policy_chosen: Optional[int] = None                  # node the policy expanded ("why this node")

    # --- read helpers (no mutation) ---
    def best(self) -> Optional[Node]:
        return self.nodes.get(self.best_node_id) if self.best_node_id is not None else None

    def evaluated_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.status is NodeStatus.evaluated]

    def feasible_nodes(self) -> list[Node]:
        """Evaluated nodes that satisfied all hard constraints (#5). These are the only nodes
        eligible to be selected as best or bred from — a constraint-violating node keeps its
        metric for the audit trail but never drives the search forward."""
        return [n for n in self.evaluated_nodes() if n.feasible]

    def pending_nodes(self) -> list[Node]:
        return sorted(
            (n for n in self.nodes.values() if n.status is NodeStatus.pending),
            key=lambda n: n.id,
        )

    def is_better(self, a: float, b: float) -> bool:
        return a < b if self.direction == "min" else a > b
