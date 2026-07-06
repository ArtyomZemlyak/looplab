"""Domain models + event envelope (I0). Pydantic v2; JSON Schemas derive from these."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


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
    # Per-node eval wall-clock budget (seconds) the Researcher may set for THIS experiment — e.g. a
    # neural-net / large-ensemble idea that legitimately needs longer than the run's default `timeout`.
    # Honored by the engine ONLY when the governance matrix grants the researcher the "timeout" setting
    # (Settings.agent_control); otherwise ignored. None => use the run-wide timeout. Flows through the
    # event log on the Idea automatically (no new event), so it's replay-safe.
    eval_timeout: Optional[float] = None
    # Semantic grouping (UI #7): a short, reusable slug the Researcher assigns to cluster related
    # experiments in one search tree (e.g. "loss-fn", "architecture", "regularization"). Optional
    # and audit-only — never affects search/selection; the UI groups nodes by it. Flows through the
    # event log automatically (idea.model_dump → node_created → Idea(**d["idea"]) in replay.fold).
    theme: Optional[str] = None
    # Intra-node sweep: instead of a single point in `params`, the Researcher may attach a discrete
    # search GRID here {name: [values...]}. When non-empty, the Developer renders code that runs
    # every grid point in ONE process (shared data load / warm GPU) and reports all results back as
    # node.trials in a single node_evaluated event. `params` may still carry fixed/shared
    # hyperparameters alongside the swept grid. Grids only (not ranges) to keep the model union-free
    # and the enumeration deterministic for replay — a future `space_kind` field can add ranges.
    space: dict[str, list[float]] = Field(default_factory=dict)

    # Hypothesis ledger (P1): a one-line statement of WHAT THIS EXPERIMENT TESTS ("residual features
    # help", "a deeper tree overfits here"). Optional and audit-only — it turns the search from
    # "propose the next mutation" into "run experiments that resolve open questions". When set, the
    # fold derives/links a Hypothesis (id = slug of the statement) and tracks it to a verdict from the
    # node's outcome. Flows through the event log on the Idea automatically; None => today's behavior.
    hypothesis: Optional[str] = None

    @property
    def is_sweep(self) -> bool:
        return bool(self.space)

    @model_validator(mode="after")
    def _backfill_rationale(self) -> "Idea":
        # An idea's `rationale` is the human-readable "why" the UI panel shows. Researchers sometimes
        # emit a structural idea (a code change, not a param sweep) with a filled `hypothesis` but an
        # empty `rationale` — leaving the node with no visible description. When that happens, derive
        # the rationale from the hypothesis so every node always carries a "why". Runs replay-safe:
        # fold rebuilds ideas through this validator, so it also heals such nodes in existing runs.
        if not (self.rationale or "").strip() and (self.hypothesis or "").strip():
            self.rationale = self.hypothesis.strip()[:500]
        return self


class Trial(BaseModel):
    """One configuration evaluated inside an intra-node sweep. Audit/UI data — the node's scalar
    `metric` is set (by the engine) from the best feasible trial, so fold/best-selection are
    untouched."""
    params: dict[str, float] = Field(default_factory=dict)
    metric: Optional[float] = None
    seconds: Optional[float] = None
    extra_metrics: dict[str, Optional[float]] = Field(default_factory=dict)
    error: str = ""


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
    # Failure taxonomy (set by node_failed): setup | timeout | oom | crash | no_metric | drift.
    # Audit/observability only — lets a UI/operator see WHY runs fail across a search.
    error_reason: str = ""
    stdout_tail: str = ""
    # Multi-seed confirmation (I12): set by a node_confirmed event. When present,
    # best-selection ranks by confirmed_mean (the robust metric) instead of `metric`.
    confirmed_mean: Optional[float] = None
    confirmed_std: Optional[float] = None
    confirmed_seeds: Optional[int] = None   # how many seeds actually succeeded (I12)
    # D1 holdout-gated promotion (B6): metric of this node on the FINAL holdout partition the
    # search never saw (set by a `holdout_evaluated` event at finish, val-top-k only). When
    # `holdout_select` was recorded on the run, best-selection ranks holdout-carrying nodes by
    # THIS metric — the anti-validation-overfitting gate (AIRA val-test gap 15-16.6%).
    holdout_metric: Optional[float] = None
    # Direction-aware val-vs-robust gap, DERIVED by the fold: how much better the search metric
    # looked than the unseen-signal metric (holdout, else confirmed mean). Positive = the node
    # overperformed on the signal the search optimized — the overfitting indicator the Trust
    # panel surfaces. Audit-only.
    generalization_gap: Optional[float] = None
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
    # Intra-node sweep results: when the node's idea carried a `space`, the Developer's code ran
    # many configurations in one process and reported them all here. `metric` above is the best
    # feasible trial's metric (computed by the engine), so this list is audit/UI only and never
    # affects search/selection. Empty for ordinary single-config nodes (backward compat).
    trials: list[Trial] = Field(default_factory=list)
    # Cross-run provenance: set when this node was SEEDED from an experiment in a sibling run (via an
    # `import` inject). {"run_id","node_id","metric"} of the source. None for ordinary nodes. Audit/UI
    # only — eval/confirmation/best-selection treat it exactly like any other injected node.
    origin: Optional[dict] = None
    # Deep-research provenance: set when this node was proposed right after a deep-research memo (its
    # directions were the active steering). {"at_node","trigger"} of the memo. None otherwise. Audit/UI
    # only (a 💡 chip) — shows where research landed in the tree; never affects search/selection.
    research_origin: Optional[dict] = None


def hypothesis_id(statement: str) -> str:
    """Stable id for a hypothesis statement so the same claim (from different ideas / a human /
    a deep-research direction) links to ONE ledger entry that accumulates evidence. A normalized
    slug + short hash: readable in the log, collision-resistant across paraphrases-of-the-exact-same
    wording (paraphrase *variation* is intentionally a new hypothesis — dedup is by exact intent)."""
    import hashlib
    import re
    norm = re.sub(r"\s+", " ", (statement or "").strip().lower())
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")[:48] or "hypothesis"
    return f"{slug}-{hashlib.md5(norm.encode('utf-8')).hexdigest()[:6]}"


class Hypothesis(BaseModel):
    """A first-class research hypothesis (P1). Audit-only — it never affects best-selection; it makes
    the run legible ("what have we learned?") and gives the UI a board. Mostly DERIVED by the fold
    from nodes whose `idea.hypothesis` is set (evidence + verdict computed from their outcomes); an
    `hypothesis_added` event can also register one with no evidence yet (a human ask, or a
    deep-research direction) that later accrues evidence when a matching node runs."""
    id: str
    statement: str
    source: str = "researcher"          # researcher | deep_research | human | strategist
    # open (no evaluated evidence) | testing (evidence running) | supported (an experiment improved) |
    # tested (evaluated, no improvement) | abandoned (explicitly dropped)
    status: str = "open"
    rationale: str = ""
    evidence: list[int] = Field(default_factory=list)   # node ids that tested it
    created_at_node: int = 0
    best_delta: Optional[float] = None  # best improvement-over-parent among evidence (audit)
    # FOREAGENT board prioritization (search/foresight.py): 0-based rank among the OPEN hypotheses in
    # the latest `hypothesis_ranked` event — 0 = predicted highest payoff. DERIVED each fold from that
    # event; None when unranked (no predictor run, or the card isn't open). Audit/UI only — the kanban
    # sorts open cards by it; never read by best-selection.
    priority: Optional[int] = None


class ResearchMemo(BaseModel):
    """Output of the Deep-Research stage (Phase 2): the model reads across ALL results so far +
    the literature/web and writes a strategic memo that steers the next batch. Audit-only — it is
    recorded as a `research_completed` event folded into `RunState.research`, NEVER into the search
    DAG, so best-selection/policies are untouched. The UI renders it as a node and surfaces the
    `summary`/`findings`/`recommended_directions` as the conclusion; `reasoning` is debug-only."""
    summary: str = ""                                   # one-paragraph conclusion (the takeaway)
    reasoning: str = ""                                 # the "think hard" narrative (debug-only)
    findings: list[str] = Field(default_factory=list)   # concrete observations across results/web
    # D8 evidence ledger: findings as CLAIMS with per-claim provenance — {statement,
    # node_ids: [int], urls: [str]}. Kosmos's failure data says cross-evidence SYNTHESIS is the
    # weakest link (57.9% accurate vs ~85% for analysis), so every synthesis claim must be
    # traceable to the experiments/sources it rests on; the Verifier (trust/verify.py) then
    # checks each claim against its cited evidence and flags the unsupported ones.
    claims: list[dict] = Field(default_factory=list)
    sources: list[dict] = Field(default_factory=list)   # {title, url} consulted (web/arXiv)
    recommended_directions: list[str] = Field(default_factory=list)  # what to try next (steer hints)
    # Optional concrete proposals the engine may materialize as injected nodes (empty for v1; the
    # directions above already feed the Researcher as standing context).
    proposed_ideas: list[Idea] = Field(default_factory=list)
    at_node: Optional[int] = None                       # node count when the stage ran (UI anchor)
    trigger: str = ""                                   # "manual" | "cadence" | "strategist"


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
    # T2 trust enforcement (folded from run_started; "audit" for old logs). "gate"/"block" make
    # best-selection exclude nodes flagged for a reward-hack / data-leakage signal (not critic).
    trust_gate: str = "audit"
    # D1 holdout-gated promotion (folded from run_started; False for old logs -> byte-identical
    # legacy selection). When True, best-selection prefers the holdout metric among the nodes
    # that carry one (the val-top-k re-scored on the unseen partition at finish).
    holdout_select: bool = False
    # The reserved-holdout fraction the run committed to at start (None in old logs / when off).
    # The engine re-uses this on resume so the split every metric was scored against never changes.
    holdout_fraction: Optional[float] = None
    nodes: dict[int, Node] = Field(default_factory=dict)
    best_node_id: Optional[int] = None
    finished: bool = False
    data_profile: Optional[dict] = None   # set by the grounding pre-phase (I16)
    leakage: Optional[dict] = None        # set by the grounding leakage scan (I9)
    data_provenance: Optional[dict] = None  # D4: pinned content hashes of task assets/data
    # Out-of-process / host-side grading (B1+): when set, the candidate wrote only predictions and the
    # HOST scored them against held-out labels it never put on the candidate FS. {scorer, predictions,
    # n_labels} — the labels themselves NEVER enter the event log. Audit/UI only.
    host_grading: Optional[dict] = None
    stop_reason: Optional[str] = None     # why the run finished (budget/leakage/done)
    confirmed_done: bool = False          # the multi-seed confirmation phase completed (I12)
    awaiting_approval: bool = False       # HITL: approval requested, not yet granted (I21)
    approved: bool = False                # HITL: a human approved the result (I21)
    archive: Optional[dict] = None        # diversity-archive summary at run end (I22)
    # Breadth read-model recorded at the strategist cadence: the run's narrowing curve (themes,
    # niches, theme entropy, dominant-theme fraction). Audit-only — never affects selection; each
    # entry carries `at_node` so the emission gate is idempotent on resume. See search/coverage.py.
    coverage_snapshots: list[dict] = Field(default_factory=list)
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
    # D1: every node that received a `holdout_evaluated` event (even with a null metric — e.g.
    # its predictions file was gone). The replay-safe gate that stops the holdout phase from
    # re-attempting a node forever on resume.
    holdout_evaluated_ids: list[int] = Field(default_factory=list)

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
    policy_reason: str = ""                               # short why-this-node label (exploit/merge/promote/…)
    # A7 Strategist (audit-only; never read by best-selection). `active_strategy` is the latest
    # applied Strategy dict; `strategy_history` is the timeline of switches for the "why this
    # strategy" panel. `pending_strategy` is an operator override (set_strategy control event) that
    # the engine applies before consulting the Strategist (human-wins parity with pause/hint).
    active_strategy: Optional[dict] = None
    strategy_history: list[dict] = Field(default_factory=list)
    pending_strategy: Optional[dict] = None
    # A1 ASHA: rung-promotion audit trail {rung, survivors} for the UI (successive-halving view).
    rungs: list[dict] = Field(default_factory=list)
    # Unified self-driving agent (audit-only; never read by best-selection): timeline of the agent's
    # macro-action choices {at_node, chosen, legal, recommended, rationale} for the "why this action"
    # view. Additive — old event logs without `agent_decision` events fold to an empty list.
    agent_decisions: list[dict] = Field(default_factory=list)
    # A6 proxy/predictive scoring: per-node early-signal scores + which candidates were skipped.
    proxy_scores: dict[int, float] = Field(default_factory=dict)
    proxy_skipped: list[int] = Field(default_factory=list)
    # B5 reward-hacking detector (audit-only; never changes selection): flagged suspicious wins
    # {node_id, signals:[{signal, detail}]} for the Trust panel.
    reward_hacks: list[dict] = Field(default_factory=list)
    # E1 novelty/dedup gate: near-duplicate proposals that were nudged off {node_id, near_node, ...}.
    novelty_events: list[dict] = Field(default_factory=list)
    # Deep-Research stage (Phase 2, audit-only sidecar — NEVER read by best-selection). `research`
    # is the timeline of completed memos (each a ResearchMemo dump); `research_requests` are pending
    # manual `deep_research` control events and `research_served` how many have been fulfilled (the
    # replay-safe gate, mirroring inject_requests/injects_done).
    research: list[dict] = Field(default_factory=list)
    research_requests: list[dict] = Field(default_factory=list)
    research_served: int = 0
    # Hypothesis ledger (P1, audit-only — NEVER read by best-selection). Derived each fold: from every
    # node whose `idea.hypothesis` is set, plus any explicit `hypothesis_added` events (human /
    # deep-research directions). Keyed by `hypothesis_id`. The UI renders it as a board.
    hypotheses: dict[str, Hypothesis] = Field(default_factory=dict)
    # Explicitly-added hypotheses (human `add_hypothesis` control event or a deep-research direction),
    # kept separately so the derived-from-nodes pass can merge evidence into them. `abandoned` ids are
    # a human/agent override of the derived status.
    hypotheses_added: list[dict] = Field(default_factory=list)
    # P1+ agentic merge: `hypothesis_merged` events fold ALIAS hypotheses (paraphrases the exact-hash
    # ledger kept separate) into a CANONICAL id, deterministically applied in `_derive_hypotheses`.
    hypotheses_merged: list[dict] = Field(default_factory=list)
    hypotheses_abandoned: list[str] = Field(default_factory=list)
    # FOREAGENT board prioritization (audit-only — NEVER read by best-selection). The latest
    # `hypothesis_ranked` event: {at_node, order:[ids], confidence, reason, ranked:[{id,statement}]}.
    # `_derive_hypotheses` stamps each open card's `priority` from `order`; the UI kanban sorts by it
    # and shows `reason` as the "why this order" analysis trace. None until the predictor first runs.
    hypothesis_ranking: Optional[dict] = None
    # Agent-authored run report (conclusion-first; audit-only sidecar — NEVER read by best-selection).
    # The latest `report_generated` event's content, regenerated on a cadence + on manual refresh. The
    # UI renders the deterministic analysis from the node set and layers this narrative on top.
    report: Optional[dict] = None
    # M6 comparative-lesson sidecars (audit-only — NEVER read by best-selection).
    # `lessons_distilled` records each mid-run distillation (at_node + the (child, parent) node-id
    # pairs spent + the statements) — it is BOTH the replay-safe cadence gate and the ledger that
    # stops a later firing (or run-end reflection) from re-distilling the same pair.
    # `lessons_refreshed` records each mid-run re-read of the shared cross-run store (cadence gate).
    lessons_distilled: list[dict] = Field(default_factory=list)
    lessons_refreshed: list[dict] = Field(default_factory=list)

    # --- read helpers (no mutation) ---
    def best(self) -> Optional[Node]:
        return self.nodes.get(self.best_node_id) if self.best_node_id is not None else None

    def evaluated_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.status is NodeStatus.evaluated]

    def feasible_nodes(self) -> list[Node]:
        """Evaluated nodes that satisfied all hard constraints (#5). These are the only nodes
        eligible to be selected as best or bred from — a constraint-violating node keeps its
        metric for the audit trail but never drives the search forward. A node with no metric
        (tolerated from a hand-edited/BYO-script log by replay) is excluded too: it can neither be
        sorted against real metrics nor selected as best, and would raise TypeError in the policies'
        metric-keyed sorts."""
        return [n for n in self.evaluated_nodes() if n.feasible and n.metric is not None]

    def pending_nodes(self) -> list[Node]:
        return sorted(
            (n for n in self.nodes.values() if n.status is NodeStatus.pending),
            key=lambda n: n.id,
        )

    def is_better(self, a: float, b: float) -> bool:
        return a < b if self.direction == "min" else a > b
