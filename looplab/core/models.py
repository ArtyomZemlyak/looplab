"""Domain models + event envelope (I0). Pydantic v2; JSON Schemas derive from these."""
from __future__ import annotations

import math
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

# The single direction-comparator owner. fitness.py imports nothing from models (duck-typed on nodes),
# so this top-level import is cycle-free and keeps RunState.is_better a one-hop delegation, not a
# per-call import (R1/SearchFitness).
from looplab.core.fitness import is_better as _is_better, is_usable_metric


def normalize_extra_metrics(value, *, max_items: int = 256) -> dict[str, float]:
    """Normalize the public multi-objective metric map to finite scalar JSON numbers.

    Evaluation stdout and old event logs are untrusted JSON.  Bookkeeping objects/lists occasionally landed
    in ``extra_metrics`` even though every consumer (Pareto UI, MLflow, schemas) treats values as scalars;
    Pydantic then warned on every API serialization.  The append-only raw event retains those values for
    audit, while the folded/public model exposes only its documented numeric contract.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for key, raw in value.items():
        if len(out) >= max_items or isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        try:
            number = float(raw)
        except (TypeError, OverflowError, ValueError):
            continue
        if math.isfinite(number):
            out[str(key)[:200]] = number
    return out


MAX_LESSON_NODE_COUNT = (1 << 31) - 1

# Stable replay-derived trust labels for ``RunState.node_concept_provenance``.  Keep these strings
# boring and explicit: the novelty admission path compares them exactly and treats every future /
# malformed / missing value as untrusted until that producer is reviewed.
NODE_CONCEPT_PROVENANCE_AUTHORED = "researcher-authored"
NODE_CONCEPT_PROVENANCE_CLASSIFIER = "classifier"
# ``concept-coverage --offline --persist`` restores a useful display taxonomy on old runs, but its
# alias matcher is not an independent semantic classifier.  Keep the exact producer visible while
# excluding it from evidence consumers through ``classifier_verified_node_concepts``.
NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC = "offline-heuristic"
# A future/malformed ``node_concepts.mode`` must not inherit classifier trust merely because replay
# understands the event type.  Preserve memberships for forward-compatible read models while
# collapsing the unknown producer to one explicit, permanently non-evidence label.
NODE_CONCEPT_PROVENANCE_UNTRUSTED = "untrusted-source"
# PART V Phase 2b: an OPERATOR manually re-tagged this node's concepts. Authoritative for the run's
# READ MODELS (UI/tools) and NOT clobbered by the classifier re-tag cadence — but deliberately NOT treated
# as independent classifier EVIDENCE (classifier_verified_node_concepts stays classifier-only), so a human
# curation edit never silently becomes cross-run/novelty evidence without its own review.
NODE_CONCEPT_PROVENANCE_OPERATOR = "operator-edited"


def classifier_verified_node_concepts(state: Any, node_id: int) -> list[str]:
    """Return concept memberships backed by the independent classifier.

    ``Idea.concepts`` and classifier output intentionally share the public ``node_concepts`` read-model
    for UI compatibility.  Any consumer that turns those labels into admission or cross-run evidence must
    cross the provenance sidecar through this helper so missing, malformed, and future producers fail closed.
    """
    provenance = getattr(state, "node_concept_provenance", None) or {}
    # CODEX AGENT: only the exact reviewed producer is evidence; proposer-authored labels remain display-only.
    if provenance.get(node_id) != NODE_CONCEPT_PROVENANCE_CLASSIFIER:
        return []
    memberships = getattr(state, "node_concepts", None) or {}
    return list(memberships.get(node_id) or [])


def node_concept_event_provenance(data: Any) -> str:
    """Resolve a durable ``node_concepts`` producer without guessing.

    Historical cadence events predate ``mode`` and were emitted only by the reviewed classifier,
    so an *absent* field retains classifier trust.  Current classifier writers use one of the two
    exact modes below.  The exact offline fallback is display-only, and every explicit unknown,
    malformed, or future value fails closed as untrusted until that producer is reviewed.
    """
    if not isinstance(data, dict):
        return NODE_CONCEPT_PROVENANCE_UNTRUSTED
    if "mode" not in data:
        return NODE_CONCEPT_PROVENANCE_CLASSIFIER
    mode = data.get("mode")
    if mode in ("llm", "agentic"):
        return NODE_CONCEPT_PROVENANCE_CLASSIFIER
    if mode == "offline-heuristic":
        return NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC
    # CODEX AGENT: explicit-but-unknown is not legacy. Treating it like an absent legacy field would
    # let a typo or future producer silently enter graded-novelty and cross-run evidence.
    return NODE_CONCEPT_PROVENANCE_UNTRUSTED


def safe_lesson_node_count(value) -> int | None:
    """Total parser for a durable advisory node-count watermark.

    Current writers emit integers. Lossless numeric legacy scalars remain accepted, while malformed or
    enormous values cannot crash resume or suppress lesson/reflection cadence forever.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        result = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 10 or not text.isascii() or not text.isdecimal():
            return None
        result = int(text)
    else:
        return None
    return result if 0 <= result <= MAX_LESSON_NODE_COUNT else None


def latest_lesson_node_count(records, *, key: str = "at_node") -> int:
    """Largest valid watermark in heterogeneous durable rows; invalid rows contribute nothing."""
    latest = 0
    for record in records or ():
        if not isinstance(record, dict):
            continue
        parsed = safe_lesson_node_count(record.get(key))
        if parsed is not None:
            latest = max(latest, parsed)
    return latest


class NodeStatus(str, Enum):
    pending = "pending"      # node_created seen, not yet evaluated (resume re-entry point)
    evaluated = "evaluated"  # has a metric
    failed = "failed"        # ran but produced no usable metric


_MAX_CONCEPT_ID_CHARS = 256      # mirrors serve.concept_frame.MAX_ID_CHARS (the frame projection gate)
_MAX_CONCEPT_ID_DEPTH = 12       # mirrors serve.concept_frame.MAX_ID_DEPTH


def valid_concept_id(raw: Any) -> bool:
    """True iff ``raw`` is a well-formed concept id: ``<seg>[/<seg>...]`` where each ``/``-segment is a
    bounded token of unicode letters/digits plus ``-``, ``.``, ``_`` and contains at least one letter/digit.

    LLM-authored English/Cyrillic/German slugs pass (``loss/decoupled-contrastive``, ``данные/размер``);
    base64/hash garbage, symbols, emoji, control chars and pure-punctuation segments are rejected
    (``a/b#c==``, ``loss/💥``, ``<script>``, ``a/..``). Pure/deterministic (no I/O) so every concept-id
    WRITE path can gate on it — the shared CHARSET owner the three per-layer canonicalizers (serve
    ``concept_id``, search ``_normalize_concept_id``, engine ``normalize_key``) lacked. Normalizes like the
    per-run canonicalizers (lower + space→dash + strip surrounding slashes) before checking."""
    if not isinstance(raw, str):
        return False
    s = raw.strip().lower().replace(" ", "-").strip("/")
    if not s or len(s) > _MAX_CONCEPT_ID_CHARS:
        return False
    parts = s.split("/")
    if len(parts) > _MAX_CONCEPT_ID_DEPTH:
        return False
    return all(any(ch.isalnum() for ch in part) and all(ch.isalnum() or ch in "-._" for ch in part)
               for part in parts)


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
    # DEPRECATED by `concepts` (below) — the multi-label concept graph supersedes the single theme
    # slug; kept only until every theme consumer is migrated off it. No longer authored.
    theme: Optional[str] = None
    # PART IV concepts: the SET of research concepts this experiment touches, as `axis/slug` ids
    # (e.g. "loss/contrastive", "architecture/moe", "regularization/r-drop"). The Researcher AUTHORS
    # these (many-to-many — a node usually touches several; propose a new id when none fits). This is
    # the grouping substrate that replaces the flat `theme` slug. These are PROPOSER claims, not
    # independent classifier evidence. Folded into RunState.node_concepts at node_created so concept
    # read-models see them from the first node; RunState.node_concept_provenance keeps that trust boundary
    # explicit and a later classifier event may consolidate/enrich them.
    # Flows through the event log automatically (idea.model_dump → node_created → Idea(**d["idea"])).
    concepts: list[str] = Field(default_factory=list)
    # PART V (B) run-base + node-DELTA authoring: instead of re-stating the full `concepts` set, a node may
    # author only what CHANGES vs the run base + its parents — `concepts_added` (new this node) and
    # `concepts_removed` (dropped this node, e.g. "swapped transformer -> diffusion"). When either is set,
    # the fold post-pass materializes node_concepts = inherited − removed + added (inherited = run base at a
    # root, else the union of parents' effective sets); `concepts` (full set) is then ignored for that node.
    # Empty (the default) keeps the legacy full-set path.
    concepts_added: list[str] = Field(default_factory=list)
    concepts_removed: list[str] = Field(default_factory=list)
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

    @field_validator("concepts", mode="after")
    @classmethod
    def _drop_malformed_concepts(cls, v):
        # Concept ids are a bounded axis/slug taxonomy. Silently drop malformed AUTHORED ids (base64/hash
        # garbage, symbols, emoji — e.g. an observed real-run tag) so a proposer/LLM hallucination never
        # pollutes node_concepts, the /concepts tree, or (via the classifier) cross-run capsules. Runs at
        # fold too — the Idea is rebuilt via Idea(**d["idea"]) — so it deterministically heals old logs;
        # legitimate ids (incl. non-ASCII letters) pass unchanged.
        return [c for c in v if valid_concept_id(c)] if isinstance(v, list) else v

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

    @model_validator(mode="after")
    def _clamp_params_to_space(self) -> "Idea":
        # Safety net: clamp any `params` value that falls OUTSIDE its declared `space` bound back into
        # range. A mutation/latent-sampling path occasionally leaked raw out-of-range values into the
        # idea — e.g. lr_stage2=-0.0204, temperature=-0.0119, batch_size=17541 with space
        # lr_stage2=[3e-4, 1e-3] — and the Developer either crash-implemented them or wasted reasoning
        # decoding "why is the learning rate negative" (live nodes 59, 61 both crashed off this). Only
        # values strictly outside [lo, hi] are touched, so valid points pass through untouched; replay
        # rebuilds ideas through this validator, healing such params in existing logs too.
        for k, val in list(self.params.items()):
            rng = self.space.get(k)
            if not (isinstance(rng, (list, tuple)) and len(rng) >= 2):
                continue
            try:
                lo, hi = float(min(rng)), float(max(rng))
                v = float(val)
            except (TypeError, ValueError, OverflowError):
                continue
            if v < lo or v > hi:
                self.params[k] = round(min(hi, max(lo, v)), 6)
        return self

    @field_validator("eval_timeout", mode="before")
    @classmethod
    def _coerce_eval_timeout(cls, v):
        # `eval_timeout` is LLM-proposed and its ONLY consumer treats a non-positive/non-finite value as
        # "unset" (engine/eval_dispatch: `if etv and etv > 0`). COERCE such values to None rather than
        # REJECT them: the fold rebuilds every idea through this validator, so a hard `gt=0`/`allow_inf_nan`
        # constraint would raise ValidationError inside `Idea(**d["idea"])` and silently DROP a node when
        # replaying an old log that carried eval_timeout ∈ {0, negative, inf, nan} — an invariant-5
        # back-compat break (old logs must fold as before). Coercing keeps the "0 => use run default"
        # semantics the consumer already honors, on both the live and replay paths, in one place.
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError, OverflowError):
            return None
        return f if math.isfinite(f) and f > 0 else None


class Trial(BaseModel):
    """One configuration evaluated inside an intra-node sweep. Audit/UI data — the node's scalar
    `metric` is set (by the engine) from the best feasible trial, so fold/best-selection are
    untouched."""
    params: dict[str, float] = Field(default_factory=dict)
    metric: Optional[float] = None
    seconds: Optional[float] = None
    extra_metrics: dict[str, float] = Field(default_factory=dict)
    error: str = ""

    @field_validator("extra_metrics", mode="before")
    @classmethod
    def _normalize_extra_metrics(cls, value):
        return normalize_extra_metrics(value)


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
    # Logically deleted via a `node_tombstoned` event (append-only delete, §6.3). The node and its
    # events STAY in the log — so parent links still resolve, the delete is reversible/auditable, and
    # node-id allocation never reuses the id — but a tombstoned node is invisible to selection: the
    # evaluated/feasible/breedable/pending helpers skip it, so it can never be chosen best, bred from,
    # or re-picked for eval. Irreversible physical purge is a separate explicit compaction, never an
    # ordinary domain command. Additive + reader-defaulted: absent on old logs -> False -> unchanged fold.
    tombstoned: bool = False
    metric: Optional[float] = None
    status: NodeStatus = NodeStatus.pending
    # Fold-internal causal anchor for projections that must identify the FIRST accepted terminal of
    # this lifecycle. Excluded from every public model dump: the durable source remains the event log.
    terminal_event_seq: Optional[int] = Field(default=None, exclude=True)
    error: str = ""
    # Failure taxonomy (set by node_failed): setup | timeout | oom | crash | no_metric | drift.
    # Audit/observability only — lets a UI/operator see WHY runs fail across a search.
    error_reason: str = ""
    # Crash-triage verdict (set by node_failed when the LLM triage ran): the agent's one-line
    # judgment of WHY the failure happened / whether the IDEA is at fault — the most expensive
    # reasoning in the failure path. Folded onto the node so the failure-reflection hint and the
    # digest can feed it to the NEXT proposal instead of dropping it (signal-delivery, §1).
    triage_rationale: str = ""
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
    # R1-c: a calibrated §12-verifier soundness score in [0,1] for THIS node's realized result. New writers
    # publish the complete tie atomically in `verifier_group_scored`; legacy `node_verified` remains readable.
    # Used ONLY as a tie-break among metric-EQUAL/CI-tied feasible nodes (SearchFitness)
    # — it can never override a strictly-better robust_metric (§21.7 advisory-never-overrides). None
    # otherwise; additive/reader-defaulted so old logs fold byte-identically.
    verifier_score: Optional[float] = None
    eval_seconds: Optional[float] = None     # wall-clock of this node's eval (cost accounting #2)
    # Multi-objective (#5): extra reported metrics + unmet hard constraints. `feasible` is
    # False when any constraint was violated — such a node keeps its metric (for the audit
    # trail) but is excluded from best-selection.
    extra_metrics: dict[str, float] = Field(default_factory=dict)
    violations: list[dict] = Field(default_factory=list)
    feasible: bool = True

    @field_validator("extra_metrics", mode="before")
    @classmethod
    def _normalize_extra_metrics(cls, value):
        return normalize_extra_metrics(value)
    # Transient re-run marker (node_reset): "propose" | "implement" set it so the engine RE-RUNS this
    # existing node in place from that stage; cleared once the re-run's node_created lands. ("eval" resets
    # just clear the terminal — the node becomes pending-with-code and the normal eval loop re-scores it,
    # no marker needed.) Not persisted meaningfully — always None on a settled node.
    rerun_from: Optional[str] = None
    # Multi-stage eval pipeline (Phase 1): per-stage outcomes [{name, status, exit_code, seconds}] in run
    # order (from stage_finished events); `failed_stage` names the stage that broke a failed node. Both
    # empty/None on the classic single-command eval.
    stages: list = Field(default_factory=list)
    failed_stage: Optional[str] = None
    # Phase 2 stage-scoped re-run: the pipeline stage a reset asked to RESTART from (skip earlier stages,
    # reuse their artifacts). Transient — set by node_reset, cleared on the next terminal.
    rerun_stage: Optional[str] = None
    # Immutable lifecycle generation (arch-review §3 P0-1): bumped by every `node_reset`. Every effect
    # derived from work on the node (repair/stage/terminal/confirm/holdout/trust) is stamped with this
    # value and rejected after a newer reset, so an abandoned worker can never adopt or mutate the next
    # lifecycle. The field keeps its original `attempt` name for projection/backward compatibility;
    # new event payloads call the same value `generation` to avoid colliding with node_repaired's
    # pre-existing inline-repair attempt counter. Absent in old logs -> 0.
    attempt: int = 0
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

    @property
    def robust_metric(self) -> Optional[float]:
        """The metric used for ranking/display: the multi-seed confirmed mean when present, else the
        raw metric. THE single spelling of "robust metric" — previously copy-pasted at a dozen call
        sites (replay/_select_best, digest, holdout, lessons, exporters, UI, cli, bench), where the
        copies could drift. Holdout precedence deliberately stays OUT of this property: holdout-gated
        selection layers `holdout_metric` on top explicitly (see replay._select_best). A plain
        @property (not a pydantic field/computed_field): excluded from model_dump, so event/snapshot
        serialization is byte-identical."""
        return self.confirmed_mean if self.confirmed_mean is not None else self.metric


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


def run_setup_key(command) -> str:
    """Stable identity for a run-level `run_setup` command, so a resume can tell "this exact setup
    already completed" from "not yet run" (arch-review §5 P2). A short hash of the canonical argv —
    single-sourced here (core) so the fold (`run_setup_finished` handler) and the engine's skip-check
    compute it identically without a layering violation (events/engine both import core)."""
    import hashlib
    canon = "\x00".join(str(a) for a in (command or []))
    return hashlib.md5(canon.encode("utf-8")).hexdigest()[:12]


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


class CommentState(BaseModel):
    """Current projection of one event-sourced operator comment.

    The append-only events remain the audit history.  This model intentionally stores only the
    current revision so folding a frequently edited comment does not duplicate every historical text
    inside ``RunState``.  ``RunState.comments`` is excluded from its ordinary JSON dump because the
    live state/SSE surface is intentionally tokenless; authenticated comment routes serialize an
    explicit allow-list instead.
    """

    comment_id: str
    node_id: int
    node_generation: Optional[int] = None
    text: str
    actor_kind: str
    version: int = 1
    resolved: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    created_seq: int = -1
    updated_seq: int = -1
    legacy: bool = False
    editable: bool = True


class RunState(BaseModel):
    """Pure fold of the event log ([ADR-12]). Never mutated except by `replay.fold`.

    Field regions (docs/15 §P5.3 — banners, deliberately NOT nested sub-models: readers spell
    `st.<field>` at dozens of sites and the flat shape is additive-safe):
      1. core run state (below)            — selection-relevant: nodes/best/gates/budget;
      2. live operator control             — the `<x>_requests`/`<x>s_done` counter pairs (see
         engine invariant #3: every side effect gates on a domain event);
      3. audit-only sidecars               — folded for the UI/exports; NEVER touch selection;
      4. read helpers                      — derived views, no mutation."""
    # --- core run state (selection-relevant) ---
    run_id: str = ""
    task_id: str = ""
    goal: str = ""
    direction: str = "min"  # "min" | "max"
    config_hash: str = ""
    # Setup completion, folded from `setup_finished` (arch-review §3 P0-3). run_started is appended in
    # the MIDDLE of setup (before AGENTS.md/provenance/host-grading/profiling and the leakage
    # hard-stop), so gating the setup phase on run_id let a crash right after run_started PERMANENTLY
    # skip the rest of preflight on resume — including leakage enforcement. Gating on setup_done
    # instead makes setup re-run until it actually completes. Absent in old logs -> False; but old logs
    # that already reached the first node also have run_id set, so `_setup_phase` treats a run with any
    # node/finished as already-set-up (see the guard there) — legacy runs never re-run setup.
    setup_done: bool = False
    # P0-3 content-addressed setup: a digest of the MATERIAL setup completed against (config hash +
    # workspace fingerprint + data provenance), folded from `setup_finished`. Binds `setup_done` to the
    # exact inputs so resume can tell "setup done for THIS material" from "done for material that has
    # since changed" — the boolean alone trusted a stale preflight (leakage!). Empty on old logs.
    setup_manifest: str = ""
    # RUN-LEVEL run_setup (dep install) completion, folded from a successful `run_setup_finished`
    # keyed by the command (arch-review §5 P2). Distinct from `setup_done` above: this is the eval's
    # one-time `run_setup` command, not the task/data preflight. The engine's in-memory `_run_setup_done`
    # flag only makes it once-per-PROCESS, so a resume (fresh Engine) re-installs deps every time and a
    # crash mid-setup can't be told from a completed one. Folding the successful command here makes it
    # crash-safe exactly-once across resume. Absent in old logs -> empty set -> setup runs as before.
    run_setup_done: set[str] = Field(default_factory=set)
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
    # R1-c (folded from run_started; False for old logs -> byte-identical legacy selection). When True,
    # best-selection's mean pick breaks a metric-EQUAL tie by the calibrated §12-verifier soundness score
    # (Node.verifier_score) — advisory, never overriding a strictly-better robust_metric (§21.7).
    select_verifier_tiebreak: bool = False
    # R1-d (§21.19): recorded `verifier_ci_tie` — widen the verifier tie-break to a statistical (CI) tie.
    # Folded from run_started; absent on old logs -> False -> byte-identical exact-tie selection.
    verifier_ci_tie: bool = False
    # Complete verifier treatment pinned by run_started so resume cannot mix sampling/criteria policies.
    select_verifier_samples: int = 3
    select_verifier_contract: str = "selection-criteria:v1"
    nodes: dict[int, Node] = Field(default_factory=dict)
    # Fold-internal current-failure threshold state. Keeping the causal crossing seq prevents a
    # reset/abort from regrouping old failures into a brand-new browser notification identity.
    current_failure_count: int = Field(default=0, exclude=True)
    failure_spike_level: int = Field(default=0, exclude=True)
    failure_spike_seq: Optional[int] = Field(default=None, exclude=True)
    # The node currently BEING BUILT (a `node_building` marker), shown in the UI the instant work starts
    # on it — before the dev session finishes with node_created. Transient: {node_id, operator,
    # parent_ids, started}; cleared when that node's node_created/node_failed folds. NOT in `nodes`, so it
    # never affects id allocation (max(nodes)+1) or resume. None when no node is mid-build.
    building: Optional[dict] = None
    best_node_id: Optional[int] = None
    # Node ids the trust gate bars the search from BREEDING (improve/merge/ablate/confirm target) —
    # the hard-flagged (cheating/leaking) set under trust_gate=gate/block, stamped by the fold's
    # `_apply_trust_gate` post-pass. Under `gate` these stay `feasible` (kept in the tree for
    # diversity/audit) but are excluded from `breedable_nodes()`; empty under `audit` / old logs.
    breed_excluded: set[int] = Field(default_factory=set)
    finished: bool = False
    # Durable, opt-in finalization handshake. `last_finish_seq` is the currently accepted
    # run_finished. Modern engine finishes carry `finalization_required=true`; only their matching
    # finalization_finished marker advances `finalized_finish_seq`. Legacy markerless finishes are
    # treated as finalized by replay so old persisted runs never become synthetic recovery work.
    last_finish_seq: int = -1
    finalized_finish_seq: int = -1
    # Seq of the accepted finalization_finished marker (not the finish it names). Fold-internal and
    # excluded from API payloads; attention uses it to ignore duplicate/stale marker envelopes.
    finalization_marker_seq: Optional[int] = Field(default=None, exclude=True)
    data_profile: Optional[dict] = None   # set by the grounding pre-phase (I16)
    leakage: Optional[dict] = None        # set by the grounding leakage scan (I9)
    data_provenance: Optional[dict] = None  # D4: pinned content hashes of task assets/data
    # Out-of-process / host-side grading (B1+): when set, the candidate wrote only predictions and the
    # HOST scored them against held-out labels it never put on the candidate FS. {scorer, predictions,
    # n_labels} — the labels themselves NEVER enter the event log. Audit/UI only.
    host_grading: Optional[dict] = None
    stop_reason: Optional[str] = None     # why the run finished (budget/leakage/done)
    confirmed_done: bool = False          # the multi-seed confirmation phase completed (I12)
    # P0-2 search epoch: bumped when a FINISHED run is reopened (resume/run_reopened). The nodes
    # added after a reopen are a fresh candidate set, so the prior confirmation/approval COMPLETION
    # (confirmed_done/approved below) must not carry over — else a better new candidate can never be
    # confirmed (the confirm phase is skipped) or re-approved. Defaults 0; old logs stay at 0 and
    # fold byte-identically until an actual reopen-after-finish occurs.
    search_epoch: int = 0
    # P1-1 recoverable-intent kernel: seq of the last durable `resume_requested` (appended by /resume
    # before spawning the detached engine) and of the last engine-written `resume_served` (appended
    # once the engine holds the singleton lock). A request whose seq is NEWER than the last serve is an
    # UNFULFILLED resume — the engine crashed before running — which the on-load reconciler re-spawns.
    # `resume_pending()` reads these. `_ts` carries the request's event time so the reconciler can wait
    # a grace period before re-spawning. All 0 on old logs -> never pending -> unchanged behavior.
    last_resume_request_seq: int = 0
    last_resume_served_seq: int = 0
    last_resume_request_ts: float = 0.0
    # Which command a pending durable launch must run. A finalize request that arrives in the narrow
    # post-run_finished lock tail must remain a finalize hand-off, not be replayed as a normal resume
    # (which would reopen the search). Launch-claim records preserve this mode.
    last_resume_request_mode: str = "resume"
    # A `resume_requested` carrying launch_claim=True is the durable cross-process claim made
    # immediately before Popen. It prevents two uvicorn workers (or a post-exit waiter racing a new
    # request) from launching duplicate detached CLIs during the gap before engine.lock is acquired.
    # If the claimant itself dies, the timestamp expires and the reconciler may safely claim again.
    last_resume_launch_seq: int = 0
    last_resume_launch_ts: float = 0.0
    awaiting_approval: bool = False       # HITL: approval requested, not yet granted (I21)
    approved: bool = False                # HITL: a human approved the result (I21)
    # P0-2: the node id the pending approval request was raised for (folded from `approval_requested`),
    # audit-only — surfaced in the projection so the UI can show WHAT is awaiting approval. It does NOT
    # gate the grant: `_on_approval_granted` honors any grant that names a REAL node in the run (so an
    # operator may `approve --node-id N` a non-best node) and rejects a forged/unhashable/non-existent id.
    # None when no request is pending.
    approval_subject: Optional[int] = None
    approval_generation: Optional[int] = None   # lifecycle generation the pending request names
    approval_request_seq: Optional[int] = Field(default=None, exclude=True)
    approved_node_id: Optional[int] = None      # explicit human choice; overrides algorithmic best
    archive: Optional[dict] = None        # diversity-archive summary at run end (I22)
    # Breadth read-model recorded at the strategist cadence: the run's narrowing curve (themes,
    # niches, theme entropy, dominant-theme fraction). Audit-only — never affects selection; each
    # entry carries `at_node` so the emission gate is idempotent on resume. See search/coverage.py.
    coverage_snapshots: list[dict] = Field(default_factory=list)
    # PART IV Phase 2a: concept-graph coverage + uncovered-region snapshots (the "0 coverage in {X}"
    # pivot signal) recorded at the strategist cadence when `concept_pivot` is on. Audit-only — never
    # affects selection; each entry carries `at_node` so the emission gate is idempotent on resume.
    # Additive/reader-defaulted: empty on old logs -> byte-identical fold. See search/concept_graph.py.
    concept_coverage_snapshots: list[dict] = Field(default_factory=list)
    # PART IV D5 (§21.16, Phase 2c): per-node concept memberships (node_id -> [concept_id]). A membership
    # may originate on the Researcher-authored Idea or from the independent `node_concepts` classifier
    # event; the last writer wins for read-model compatibility. Consumers that can affect admission MUST
    # consult node_concept_provenance rather than assuming every membership came from the classifier.
    node_concepts: dict[int, list[str]] = Field(default_factory=dict)
    # PART V (B): the RUN's BASE concept set — the common technologies every node uses unless a node
    # states otherwise (folded from `run_concepts` events). A node may then author only the DELTA vs this
    # base + its parents (see `node_concept_deltas`), keeping per-node annotations minimal. Additive /
    # reader-defaulted; empty on runs that never set a base (every node then authors its own full set).
    run_base_concepts: list[str] = Field(default_factory=list)
    # PART V (B): raw per-node concept DELTAS {node_id -> {"added": [...], "removed": [...]}} authored on
    # the Idea. Stored raw during replay; a deterministic POST-PASS in `fold` materializes each such node's
    # effective `node_concepts` = inherited − removed + added, where inherited = the run BASE at a root, else
    # the UNION of the node's parents' effective sets (the base flows in through the roots and down the DAG,
    # so a removal propagates). Kept as a
    # topological read-time resolution (not folded in event order) so `fold` stays ORDER-TOLERANT
    # (invariant 5): the post-pass sees the complete DAG, so a spliced/reordered log resolves identically.
    node_concept_deltas: dict[int, dict] = Field(default_factory=dict)
    # CODEX AGENT: proposer-authored taxonomy is an untrusted claim, never classifier evidence. This
    # replay-derived sidecar records the producer of the CURRENT last-write-wins membership. Missing and
    # unknown values are deliberately untrusted; legacy generation-zero `node_concepts` events replay as
    # `classifier`, while old `node_created` Idea.concepts replay as `researcher-authored` without migration.
    node_concept_provenance: dict[int, str] = Field(default_factory=dict)
    # PART IV D5 (§21.18 B1): the concept-graph vocabulary SIZE when each node was last tagged. A node
    # tagged against a much smaller vocabulary than the current one is STALE (a concept minted by a later
    # node may now apply to it), so the cadence re-tags the most-stale nodes against the grown vocabulary
    # (bounded per cadence). Additive/reader-defaulted; empty on old logs / pre-B1 events -> no re-tag.
    node_concepts_at_vocab: dict[int, int] = Field(default_factory=dict)
    # PART IV D4 (§21.18 HT): per-hypothesis agentic concept tags (hyp_id -> [concept_id]) recorded once by
    # the LLM tagger, reused by taxonomy dedup instead of the tag_text alias heuristic. Populated only when
    # `concept_pivot` is on; audit-only. Additive/reader-defaulted: empty on old logs -> byte-identical fold.
    hypothesis_concepts: dict[str, list[str]] = Field(default_factory=dict)
    # PART IV D4 (§21.18 B1-ext): concept-graph vocabulary SIZE when each hypothesis was tagged — a
    # hypothesis tagged against a much smaller vocabulary is STALE and gets re-tagged against the grown one
    # (bounded per cadence), mirroring node_concepts_at_vocab. Additive/reader-defaulted; empty on old logs.
    hypothesis_concepts_at_vocab: dict[str, int] = Field(default_factory=dict)
    # PART IV D5 (§21.18 B3): the accumulated concept-consolidation rename map (raw_id -> canonical_id).
    # Reused by later cadences so consolidation decisions stay FIXED (stable vocabulary, no flapping / B1
    # churn). Populated only when `concept_pivot` is on; audit-only. Additive/reader-defaulted: empty on
    # old logs -> byte-identical fold.
    concept_consolidation: dict[str, str] = Field(default_factory=dict)
    # PART IV concept-edge substrate: the typed concept graph (src, rel, dst) -> {provenance, confidence},
    # keyed by "src\trel\tdst". Makes hierarchy a swappable projection (project_hierarchy). Folded
    # COMMUTATIVELY from explicit EV_CONCEPT_EDGE assertions (max-confidence-wins per triple ->
    # order-tolerant). Derived ``co_occurs`` rows are intentionally omitted and recomputed from current
    # memberships by ConceptFrame, so stale counts can decrease/disappear. Audit-only, never touches
    # selection. Additive/reader-defaulted: empty on old logs -> path projection remains available.
    concept_edges: dict[str, dict] = Field(default_factory=dict)
    # RepoTask onboarding (Phase 3, ADR-7): the agent proposes a trusted eval spec + metric
    # adapter; a human ratifies it once; then the loop trusts it.
    proposed_spec: Optional[dict] = None  # {eval_spec, adapter_files, goal} from the agent
    spec_approval_requested: bool = False
    spec_approval_request_seq: Optional[int] = Field(default=None, exclude=True)
    spec_confirmed: bool = False          # human ratified the proposed eval spec
    # Drift cross-check audit (Phase 4, ratify_freeze_drift): each entry is a divergence the
    # independent reader caught {node_id, primary, cross, tolerance, [seed]}. Audit only —
    # the metric was already discarded (node failed), so this never changes selection.
    drifts: list[dict] = Field(default_factory=list)
    # Workspace reproducibility (item #4): the editable-repo/data fingerprint pinned at
    # run_started, and whether a resume detected the source changed underneath.
    workspace: Optional[dict] = None
    workspace_changed: bool = False
    # F18: folded like workspace_changed so the env-drift note is emitted ONCE, not re-appended on
    # every resume of an upgraded run (the emit is gated on `not state.env_changed`).
    env_changed: bool = False
    # P0-5 environment identity: the Python/platform + key-library version fingerprint pinned at
    # run_started. A resume compares the current environment against it and emits `env_changed` (a
    # diagnostic) on drift — a run continued after a library upgrade is no longer bit-reproducible, so
    # record it instead of pretending it is. None on old logs -> no env pin -> the check is skipped.
    env: Optional[dict] = None
    # P0-5 dirty-input enumeration: for a repo task, the list of workspace files that were UNCOMMITTED
    # (git status --porcelain) at run start — the explicit "which inputs differ from a clean checkout"
    # on top of the content hash the workspace fingerprint already pins. Empty for non-repo/clean runs
    # and old logs. Provenance only; never gates.
    dirty_inputs: list[dict] = Field(default_factory=list)
    # Eval-compute budget accounting (#2): cumulative wall-clock spent INSIDE evals (training
    # runs), distinct from the run's total wall-clock (which includes LLM/agent time). The
    # search stops cleanly once this crosses `max_eval_seconds` — guards the silent long sweep.
    total_eval_seconds: float = 0.0
    # P1-2 separate budget buckets: the SAME cumulative eval seconds split by category (node/search
    # eval vs multi-seed confirm) for observability — where the compute went, not just the total. LLM
    # spend is already its own bucket (llm_cost -> total_llm_*); holdout re-scores existing predictions
    # for free (no eval_seconds), so it never contributes. Sums to total_eval_seconds. Empty on old
    # logs -> populated additively on the next fold; never gates selection.
    eval_seconds_by_kind: dict[str, float] = Field(default_factory=dict)
    # Per-seed confirmation results {node_id: {seed: metric|None}} from `confirm_eval` events —
    # lets a crash-interrupted confirm pass RESUME mid-node (skip seeds already run) instead of
    # re-executing every expensive full-profile seed from scratch.
    confirm_seed_results: dict[int, dict] = Field(default_factory=dict)
    # D1: every node that received a `holdout_evaluated` event (even with a null metric — e.g.
    # its predictions file was gone). The replay-safe gate that stops the holdout phase from
    # re-attempting a node forever on resume.
    holdout_evaluated_ids: list[int] = Field(default_factory=list)
    # Whether the CURRENTLY-disclosed holdout was recorded with epoch semantics (a modern
    # holdout_evaluated stamps `search_epoch`; a legacy one does not). Derived during fold, not
    # persisted. Gates the metric-wiping requeue when a later candidate change re-hides the split:
    # legacy holdout logs predate search epochs and must NOT wipe surviving incumbents on replay
    # (invariant 5b — old logs fold as before). Default False = legacy-safe for old logs.
    holdout_epoch_aware: bool = False

    # --- live operator control (UI intervention via the event log) ---
    # These are folded from appended CONTROL events (intent). The engine remains the sole writer
    # of DOMAIN events: it reads the intent here and writes the effect (e.g. node_abort -> a
    # node_failed reason="aborted"). All deterministic under replay; audit-only fields never
    # change best-selection.
    paused: bool = False                       # `pause`/`resume`: resumable break (not finished)
    pause_node_id: Optional[int] = None         # scoped auto-pause owner (None = explicit operator pause)
    pause_generation: Optional[int] = None
    pause_event_seq: Optional[int] = Field(default=None, exclude=True)
    stop_requested: Optional[str] = None       # `run_abort`: reason; loop -> run_finished + break
    # Seq of the latest finalize intent. A request newer than the accepted finish still needs a new
    # finish/finalization boundary; an older one was already consumed by that finish.
    last_stop_request_seq: int = -1
    aborted_nodes: list[int] = Field(default_factory=list)   # `node_abort`: skip/kill these nodes
    budget_overrides: dict = Field(default_factory=dict)     # `budget_extend`: max_seconds/eval
    pending_hints: list[dict] = Field(default_factory=list)  # `hint`: operator directives to steer
    confirm_requests: list[int] = Field(default_factory=list)  # `force_confirm`: operator robustness ask
    confirmed_forced: list[int] = Field(default_factory=list)   # nodes a forced confirm finished (gate)
    # Generation-aware twins keep reset lifecycles distinct while the id-only lists above preserve the
    # existing UI projection/backward-compatible surface.
    confirm_request_generations: list[dict] = Field(default_factory=list)
    confirmed_forced_generations: list[dict] = Field(default_factory=list)
    ablate_requests: list[int] = Field(default_factory=list)    # `force_ablate` (wired in Phase 5)
    ablate_request_generations: list[dict] = Field(default_factory=list)
    fork_requests: list[dict] = Field(default_factory=list)     # `fork`: operator-seeded improve
    forks_done: int = 0                        # count of processed forks (replay-safe fulfillment)
    # `inject_node`: an operator-authored experiment hand-added to the tree (a manual idea +
    # optional parent + optional code). The engine materializes each one into a real pending node
    # that the policy then evaluates like any other — so a human can steer the search directly.
    inject_requests: list[dict] = Field(default_factory=list)
    injects_done: int = 0                      # count of processed injects (replay-safe fulfillment)
    annotations: dict[int, list[str]] = Field(default_factory=dict)  # legacy `annotation`: node notes
    # Modern collaboration is read only through authenticated, bounded projections.  Excluding it
    # here prevents free-form comment text from entering the tokenless /state + SSE payload.
    comments: dict[str, CommentState] = Field(default_factory=dict, exclude=True)
    # Safe scalar used by clients to refresh the bounded comment projection only when it changes.
    comments_revision: int = -1
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
    # --- audit-only sidecars (folded for the UI/exports; NEVER touch selection) ---
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
    # FOREAGENT predict-before-execute picks {node_id, confidence, chosen, ...}, folded from
    # `foresight_selected` events. Audit-only — never touches selection — but the fold keeps them so
    # the world model can be primed with its OWN track record (did past predicted-best picks beat
    # their parent?), closing the open predict→outcome loop (signal-delivery, §1). Additive.
    foresight_selected: list[dict] = Field(default_factory=list)
    # E1 novelty/dedup gate: near-duplicate proposals that were nudged off {node_id, near_node, ...}.
    novelty_events: list[dict] = Field(default_factory=list)
    # PART IV D3 (Phase 2b): the live gate's GRADED-ALLOW decisions — proposals allowed despite a
    # concept overlap the flat gate would reject (level-4 same-direction-new-impl, level-5 re-open of a
    # wrongly-abandoned direction). Audit-only sidecar; never read by best-selection. Additive.
    novelty_grades: list[dict] = Field(default_factory=list)
    # PART IV cross-run Step 2 (§21.20): concepts the proposed idea shares with a SIMILAR earlier run,
    # surfaced (never rejected) so the trace/researcher sees "tried in run X -> metric Y". Populated only
    # under `cross_run_concepts`; audit-only sidecar, never read by best-selection. Additive.
    cross_run_priors: list[dict] = Field(default_factory=list)
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
    # Human-DELETED hypotheses (hypothesis_updated status=deleted): removed from the board entirely,
    # unlike `abandoned` (which stays visible in its own column). Excluded from `hypotheses` on fold.
    hypotheses_deleted: list[str] = Field(default_factory=list)
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

    @field_serializer("run_setup_done")
    def _ser_run_setup_done(self, v: set) -> list:
        # Serialize the str-set as a SORTED list so the projection is deterministic across processes
        # (final ultra-review §A): a plain set[str] dumps in hash-slot order, which PYTHONHASHSEED
        # randomizes per process (unlike set[int], whose hash==value), so `looplab replay` / `/state`
        # could show a spurious ordering diff for a run with ≥2 distinct run_setup commands. The live
        # attribute stays a set (membership is all the fold/engine use); only the dump is ordered.
        return sorted(v)

    # --- read helpers (no mutation) ---
    def resume_pending(self) -> bool:
        """P1-1: a durable resume intent was recorded but no engine has served it yet (its request seq
        is newer than the last serve). Combined by the reconciler with a not-alive / not-finished probe
        to detect a zombie whose resume spawn died before the engine ran."""
        return self.last_resume_request_seq > self.last_resume_served_seq

    def finalization_pending(self) -> bool:
        return (self.finished and self.last_finish_seq >= 0
                and self.finalized_finish_seq != self.last_finish_seq)

    def best(self) -> Optional[Node]:
        return self.nodes.get(self.best_node_id) if self.best_node_id is not None else None

    def evaluated_nodes(self) -> list[Node]:
        # `not n.tombstoned` gates ALL downstream selection at the source: feasible_nodes/
        # breedable_nodes and the best-pick post-pass all read through here, so a logically-deleted
        # node can never be selected best, bred from, or confirmed. (§6.3 append-only delete.)
        return [n for n in self.nodes.values()
                if n.status is NodeStatus.evaluated and not n.tombstoned]

    def feasible_nodes(self) -> list[Node]:
        """Evaluated nodes that satisfied all hard constraints (#5). These are the only nodes
        eligible to be selected as best or bred from — a constraint-violating node keeps its
        metric for the audit trail but never drives the search forward. A node with no metric
        (tolerated from a hand-edited/BYO-script log by replay) is excluded too: it can neither be
        sorted against real metrics nor selected as best, and would raise TypeError in the policies'
        metric-keyed sorts."""
        return [n for n in self.evaluated_nodes()
                if n.feasible and is_usable_metric(n.metric) and n.id not in self.aborted_nodes]

    def breedable_nodes(self) -> list[Node]:
        """Feasible nodes the search may BREED FROM or CONFIRM (improve/merge/ablate/promote/confirm
        target). Under `trust_gate=gate` a hard-flagged (cheating/leaking) node stays feasible — kept
        in the tree for diversity/audit and barred from WINNING elsewhere — but is NOT bred from, so
        the search never sinks budget improving a cheating lineage or displaces an honest node from
        the confirm top-k (T2, §2.2). Under `block` it is already infeasible (out of feasible_nodes).
        `audit` / no flags -> identical to feasible_nodes(); the fast path keeps it a no-op there."""
        if not self.breed_excluded:
            return self.feasible_nodes()
        return [n for n in self.feasible_nodes() if n.id not in self.breed_excluded]

    def pending_nodes(self) -> list[Node]:
        # A tombstoned pending node (its subtree was logically deleted while it was still queued)
        # must NOT be handed back to the eval loop on resume — skip it here too (§6.3).
        return sorted(
            (n for n in self.nodes.values()
             if n.status is NodeStatus.pending and not n.tombstoned),
            key=lambda n: n.id,
        )

    def is_better(self, a: float, b: float) -> bool:
        # Delegates to the single comparator owner (core/fitness.py) so "better" has ONE spelling
        # across the fold, the policies and this convenience primitive (R1/SearchFitness).
        return _is_better(self.direction, a, b)
