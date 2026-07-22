"""Central registry of event-type names (the run log's vocabulary).

Every event appended to a run's ``events.jsonl`` (via ``EventStore.append``) carries one of
the type strings below. Emission sites and ``replay.fold`` should reference these constants
instead of raw literals — unknown event types are deliberately ignored by the fold
(forward compat), so a typo'd event name silently no-ops; the constants (plus the guard
test ``tests/test_event_types.py``) make that a hard error instead.

Event-schema evolution rules (the contract ``replay.fold`` and every reader rely on):

* **Additive-only data fields.** An event's ``data`` dict may gain new keys over time, but
  existing keys never change meaning or type. Readers supply defaults (``d.get(...)``) so
  old logs — missing the newer fields — fold byte-identically (e.g. ``holdout_select``
  absent -> False -> legacy best-selection).
* **The fold is pure and deterministic.** ``replay.fold`` is the only producer of
  ``RunState``: no I/O, no LLM calls, no wall-clock — resume = re-fold the log. Decisions
  that involved an LLM (e.g. ``strategy_decision``) are *recorded* so replay rebuilds the
  state without re-invoking anything.
* **Unknown logical event types are ignored** by the fold (forward compatibility once a reader has
  decoded the physical record). This does not promise downgrade compatibility for the on-disk grammar:
  ``append_many`` stores a versioned batch envelope that pre-batch readers discard as one unknown row.
* **Duplicate terminal events are idempotent** (first-terminal pattern): only a node's
  FIRST terminal event (``node_evaluated``/``node_failed``) contributes its eval time, so a
  corrupt/double-folded log can't inflate budgets or make the fold order-dependent.
* **Fulfillment gates** for operator requests use ``<x>_requests`` / ``<x>s_done`` counter
  pairs (``fork``/``fork_done``, ``inject_node``/``inject_done``, ``deep_research``/
  ``research_completed(served_manual)``): the engine serves ``len(requests) - done``
  outstanding items, so a resume never re-serves an already-fulfilled request.
* **Envelope versioning:** ordinary ``Event.v`` is written as 1 and is not yet migrated by fold.
  Batch-envelope and nested member versions are decoded strictly as v1 by the event-store reader.

How to add an event type:

1. Add an ``EV_<UPPER_SNAKE> = "<lower_snake>"`` constant in the right section below
   (``ALL_EVENT_TYPES`` picks it up automatically).
2. Emit it via ``store.append(EV_..., {...})`` using the constant.
3. If it must affect ``RunState``, handle it in ``replay.fold`` (deterministically, with
   defaults for old logs); otherwise it is a diagnostic/sidecar event and the fold ignores
   it by design.
4. Never rename or reuse an existing value — old logs must keep folding identically.
"""
from __future__ import annotations

# --- DOMAIN events (folded into RunState by `replay.fold`; the engine is the sole writer,
#     except a few UI/CLI-writable ratification/config events: `spec_approved`/`approval_granted`
#     (ratify), `trust_gate_changed` (PUT /config), and `report_generated` (report_refresh) — all
#     fold-safe last-write-wins/audit-only. Route any new server append through an allow-listed
#     helper rather than adding another exception here). ---
EV_RUN_STARTED = "run_started"
# Emitted at the START of building a node — BEFORE the Researcher/Developer run — so the UI can show the
# node the instant work begins on it (its live agent-trace streams in) instead of only after the minutes-
# long dev session ends with node_created. A TRANSIENT marker (folds to st.building, NOT st.nodes) so it
# never affects node-id allocation or resume: node_created clears it and adds the real node.
EV_NODE_BUILDING = "node_building"
EV_NODE_CREATED = "node_created"
EV_NODE_EVALUATED = "node_evaluated"
EV_NODE_FAILED = "node_failed"
EV_NODE_REPAIRED = "node_repaired"
# Append-only node delete (§6.3): logically removes a node + its descendant subtree instead of
# physically rewriting events.jsonl (which broke append-only semantics and could leave
# parent/chosen/archive references stale). Data: {"node_ids": [subtree ids]} — the writer computes
# the whole subtree so the fold stays a pure set op. Folded: marks each node tombstoned (kept in
# st.nodes, excluded from selection). Written offline by the machine-runs boss tool while the engine
# is stopped (delete refuses on a live run); irreversible physical purge is a separate explicit
# compaction, never an ordinary domain command.
EV_NODE_TOMBSTONED = "node_tombstoned"
EV_CONFIRM_EVAL = "confirm_eval"
EV_NODE_CONFIRMED = "node_confirmed"
EV_HOLDOUT_EVALUATED = "holdout_evaluated"
EV_AGENT_VALIDATED = "agent_validated"
EV_DATA_PROFILED = "data_profiled"
EV_DATA_PROVENANCE = "data_provenance"
EV_HOST_GRADING = "host_grading"
EV_DATA_LEAKAGE = "data_leakage"
EV_APPROVAL_REQUESTED = "approval_requested"
EV_SPEC_PROPOSED = "spec_proposed"
EV_SPEC_APPROVAL_REQUESTED = "spec_approval_requested"
EV_SPEC_DRIFT = "spec_drift"
EV_WORKSPACE_CHANGED = "workspace_changed"
EV_ENV_CHANGED = "env_changed"           # P0-5: the Python/lib environment differs from run start (resume)
EV_DIVERSITY_ARCHIVE = "diversity_archive"
# Breadth read-model recorded at the strategist cadence (narrowing curve). The fold handler never
# selects a node from it, but a live Strategist can consume it and change later search policy. Folded
# so the at_node gate makes resume idempotent and the UI/replay can plot coverage over time.
# See looplab/search/coverage.py.
EV_COVERAGE_SNAPSHOT = "coverage_snapshot"
# PART IV D5/D7 (§21.11/§21.8, Phase 2a): concept-graph coverage + uncovered-region snapshot recorded
# at the `concept_retag_every` cadence (via `_should_consult_concepts`, not `strategist_every`) when
# `concept_pivot` is on. The fold handler is selection-neutral, while
# the live "0 coverage in {X} — go there" directive can steer future candidate generation; folded
# (at_node gate) exactly like coverage_snapshot.
# Produced ONCE per cadence (agentic build when a reflect client is wired -> universal, any task, derived
# importance; heuristic+skeleton fallback otherwise) and RECORDED; fold only READS it, so replay preserves
# the recorded snapshot deterministically. See looplab/search/concept_graph.py.
EV_CONCEPT_COVERAGE_SNAPSHOT = "concept_coverage_snapshot"
# PART IV D5 (§21.16, Phase 2c): the LLM tagger's RAW concept ids for ONE experiment node, recorded the
# first time that node is tagged so later strategist cadences REUSE it instead of re-tagging the whole
# history (turns per-run tagging from ~O(nodes x cadences) LLM calls into ~O(nodes)). Node-scoped and
# STABLE (a node's raw tags don't change once assigned; consolidation/coverage are re-derived cheaply and
# purely each cadence). Recorded only when `concept_pivot` is on; additive, reader-defaulted; folds into
# RunState.node_concepts. See looplab/search/concept_graph.py::tag_nodes_llm (known_tags).
EV_NODE_CONCEPTS = "node_concepts"
# PART V (B): the RUN's BASE concept set — the common technologies every node uses unless it authors a
# delta otherwise. Set once (typically at a run-start preliminary stage) and may be re-set; folds to
# RunState.run_base_concepts (last-write-wins). Additive, reader-defaulted; absent -> nodes author full
# sets as before. `Idea.concept_mode="delta"` is the explicit per-node discriminator (empty lists still
# inherit); the base + per-node deltas are materialized topologically in a fold post-pass.
EV_RUN_CONCEPTS = "run_concepts"
# PART IV D4 (§21.18 HT): the LLM tagger's concept ids for ONE hypothesis on the board, recorded the first
# time it is tagged so later cadences REUSE it (incremental, ~O(hypotheses) not per-cadence) — the agentic
# replacement for the `tag_text` alias heuristic in taxonomy dedup. Hypothesis-scoped (keyed by the
# statement-slug id); recorded only when `concept_pivot` is on; additive, reader-defaulted; folds into
# RunState.hypothesis_concepts. See looplab/search/concept_graph.py::tag_text_llm.
EV_HYPOTHESIS_CONCEPTS = "hypothesis_concepts"
# PART IV D5 (§21.18 B3): the concept-vocabulary consolidation rename map (raw_id -> canonical_id) decided
# by the LLM, recorded so LATER cadences REUSE the decisions instead of re-deciding them (LLM-nondeterministic
# -> flapping coverage + B1 churn). Fixing known renames makes the vocabulary a STABLE coordinate system and
# only new concepts are consolidated. Recorded only when `concept_pivot` is on; additive, reader-defaulted;
# folds (accumulated) into RunState.concept_consolidation. See looplab/search/concept_graph.py.
EV_CONCEPT_CONSOLIDATION = "concept_consolidation"
# PART IV concept-edge substrate: a typed concept-graph edge (src, rel, dst) with provenance + confidence.
# Makes hierarchy a swappable PROJECTION ("any concept can be an axis") instead of the fixed id-prefix
# tree. Emitted from the strategist cadence (asserted is_a/uses from the graph + evidenced co_occurs mined
# from node_concepts). Additive + reader-defaulted (empty edge set -> project_hierarchy falls back to the
# is_a-from-path tree, byte-identical to today). Folds COMMUTATIVELY into RunState.concept_edges
# (max-confidence-wins keyed on the triple -> order-tolerant). See looplab/search/concept_graph.py.
# The themes->concepts series is now documented in the same layer it ships: docs/guide/concepts.md
# covers authored concepts / concept_edge / lenses / the GET /concepts endpoint / the two views,
# docs/infographic/agent-architecture.html carries the concept-substrate block, and docs/guide/ui.md
# marks the legacy theme grouping/filter as the empty-on-concept-runs bridge it now is.
EV_CONCEPT_EDGE = "concept_edge"
EV_LLM_COST = "llm_cost"
EV_LLM_USAGE = "llm_usage"  # durable sanitized provider-call delta; folded cumulatively
EV_ABLATE = "ablate"
EV_POLICY_DECISION = "policy_decision"
EV_STRATEGY_DECISION = "strategy_decision"
EV_HYPOTHESIS_RANKED = "hypothesis_ranked"   # FOREAGENT board prioritization: order + confidence + trace
EV_RUNG_PROMOTED = "rung_promoted"
EV_AGENT_DECISION = "agent_decision"
EV_REWARD_HACK_SUSPECTED = "reward_hack_suspected"
EV_NOVELTY_REJECTED = "novelty_rejected"
# PART IV D3 (§21.4, Phase 2b): the LIVE novelty gate GRADED a proposal over the concept graph and
# ALLOWED it despite a concept overlap the flat dedup gate would have rejected — a level-4 "same
# direction, different implementation" or a level-5 "re-opens a wrongly-abandoned failed direction".
# Audit-only (records the grade + near-node); recorded only when `graded_novelty` is on. Additive,
# reader-defaulted; folds into RunState.novelty_grades. See looplab/search/graded_novelty.py.
EV_NOVELTY_GRADED = "novelty_graded"
# PART IV cross-run Step 2 (§21.20): the proposed idea's concept(s) were tried in a SIMILAR earlier run
# (loaded from the cross-run ConceptCapsuleStore). Audit-only, recorded only when `cross_run_concepts`
# is on; SURFACES the prior outcome, never rejects. Additive, reader-defaulted; folds into
# RunState.cross_run_priors. See looplab/engine/novelty.py + looplab/engine/memory.py.
EV_CROSS_RUN_PRIOR = "cross_run_prior"
# R1-c: a calibrated §12-verifier soundness score in [0,1] for a node's REALIZED result, computed live
# by the engine (an LLM output can't live in the deterministic fold) and frozen here. Generation-scoped
# and read ONLY as a metric-tie-break in best-selection (never overrides ground truth, §21.7). Additive;
# folds into Node.verifier_score. New writers bind the score to a deterministic `evidence_digest`; replay
# drops a mismatched revision and invalidates a score when confirm/holdout evidence changes. Emitted only
# when `select_verifier` is on. See trust/verifier.py.
EV_NODE_VERIFIED = "node_verified"
# Versioned all-or-nothing verifier treatment for one complete selector tie component. Replay validates
# every member/generation/evidence digest before publishing any score; legacy per-node events remain readable.
EV_VERIFIER_GROUP_SCORED = "verifier_group_scored"
EV_PROXY_SCORED = "proxy_scored"
EV_BEST_CONFIRMED = "best_confirmed"
EV_RUN_FINISHED = "run_finished"
# Durable completion of the post-run wrap-up for one accepted run_finished sequence. A process may
# crash after run_finished but before budget/archive/case/reflection; resume retries until this marker
# names the current finish seq. A reopened/new finish gets a new seq and therefore a fresh wrap-up.
EV_FINALIZATION_FINISHED = "finalization_finished"
EV_FORK_DONE = "fork_done"               # fulfillment gate for `fork` requests
EV_INJECT_DONE = "inject_done"           # fulfillment gate for `inject_node` requests
EV_RESEARCH_COMPLETED = "research_completed"   # memo sidecar + gate for `deep_research`
EV_LESSONS_DISTILLED = "lessons_distilled"
EV_LESSONS_REFRESHED = "lessons_refreshed"
EV_REPORT_GENERATED = "report_generated"
EV_CONFIRM_DONE = "confirm_done"         # fulfillment gate for `force_confirm` requests
# P1-1 recoverable-intent kernel for the resume/spawn handoff. `/resume` records a DURABLE
# `resume_requested` intent BEFORE spawning the detached engine (so a spawn that crashes before the
# engine runs isn't lost); the engine appends `resume_served` once it has ACQUIRED the singleton lock
# and is about to drive the loop. The pair is a seq-gated fulfillment (like fork/inject): a request
# whose seq is newer than the last serve is an UNFULFILLED (zombie) resume that the on-load reconciler
# re-spawns — idempotent because a second engine no-ops on the lock. resume_served is engine-written.
EV_RESUME_SERVED = "resume_served"

# --- CONTROL events (live operator/UI intents appended to the same log; the engine reads
#     the folded intent and writes the matching DOMAIN effect — see CONTROL_EVENTS in
#     serve/server.py and the control section of `replay.fold`). ---
EV_RUN_ABORT = "run_abort"
EV_PAUSE = "pause"
# Server-owned pause-and-resume handoff. Unlike a client-side ``pause`` then ``resume`` saga, this
# ONE durable intent both freezes the current owner and leaves an unfulfilled resume watermark. The
# command worker (or startup reconciler after a crash/restart) launches a replacement only after the
# old singleton owner exits; ``resume_served`` is the exact completion boundary.
EV_RESTART = "restart"
EV_RESUME = "resume"
EV_NODE_ABORT = "node_abort"
EV_NODE_RESET = "node_reset"      # re-run an EXISTING node in place from a stage (propose|implement|
#                                   eval, or any eval-pipeline stage name — train, data_prep, …)
EV_STAGE_FINISHED = "stage_finished"   # one stage of a multi-stage eval pipeline finished (name, status)
EV_BUDGET_EXTEND = "budget_extend"
EV_HINT = "hint"
EV_SET_STRATEGY = "set_strategy"
EV_FORCE_CONFIRM = "force_confirm"
EV_FORCE_ABLATE = "force_ablate"
EV_FORK = "fork"
EV_INJECT_NODE = "inject_node"
EV_DEEP_RESEARCH = "deep_research"
EV_ANNOTATION = "annotation"
# Event-sourced collaboration.  ``annotation`` remains the immutable legacy note event; modern
# writers use the versioned comment lifecycle below so edits/resolution retain an auditable history.
EV_COMMENT_CREATED = "comment_created"
EV_COMMENT_EDITED = "comment_edited"
EV_COMMENT_RESOLUTION_CHANGED = "comment_resolution_changed"
# PART V Phase 2b: an operator manually re-tags ONE node's concepts (command-only, generation-fenced like
# comments). Folded to node_concepts + operator provenance; the classifier re-tag cadence must not clobber it.
EV_CONCEPT_TAG_EDITED = "concept_tag_edited"
EV_PROMOTE = "promote"
EV_APPROVAL_GRANTED = "approval_granted"
EV_SPEC_APPROVED = "spec_approved"
EV_HYPOTHESIS_ADDED = "hypothesis_added"       # also engine-written after deep research
EV_HYPOTHESIS_UPDATED = "hypothesis_updated"
EV_HYPOTHESIS_MERGED = "hypothesis_merged"     # engine-written: fold alias hypotheses into a canonical
# Durable Card ledger — a work-item projection beside the thin hypothesis-direction board. It never
# directly selects the metric champion; the opt-in Card queue consumes folded `selection_ready` rows to
# choose candidate actions. Main-task-written; NONE
# are BACKGROUND_APPENDABLE (a monotonic card_id cannot be background-minted — docs/23 decision 29).
EV_CARD_ADDED = "card_added"                    # id + immutable action/ownership receipt (+ later enrich)
EV_CARD_MERGED = "card_merged"                  # fold alias cards into a canonical (mirrors hypothesis_merged)
EV_CARD_DROPPED = "card_dropped"                # engine/operator drop: reason + dropped_by (lifecycle)
EV_CARD_ENRICHED = "card_enriched"              # Layer 1b: novelty/cross-run/footprint delta (last-write-by-seq)
EV_CARD_RANKED = "card_ranked"                  # Layer 1b: board priority order (mirrors hypothesis_ranked)
# Layer 6 operator controls. These are generation-fenced, server-stamped command intents; they fold
# into dedicated override maps and are overlaid after every engine/Strategist projection. `card_dropped`
# above is the fourth control as well as an engine lifecycle event.
EV_CARD_REPRIORITIZED = "card_reprioritized"
EV_CARD_EDITED = "card_edited"
EV_CARD_RESOURCE_PINNED = "card_resource_pinned"
# Layer 5's request/done execution ledger. Both are folded and main-task-only: the request is the
# durable selection+compute gate; done advances it after commit or an explicit producer-failure give-up.
EV_CARD_BUILD_REQUESTED = "card_build_requested"
EV_CARD_BUILD_DONE = "card_build_done"
EV_RUN_REOPENED = "run_reopened"
EV_RESUME_REQUESTED = "resume_requested"   # P1-1: durable resume intent, appended by /resume pre-spawn
EV_TRUST_GATE_CHANGED = "trust_gate_changed"   # server config edit; folded last-write-wins
# Predict-before-execute pick among K ideas / N code candidates. FOLDED into RunState.foresight_selected
# (audit-only, never touches selection) so the world model can be primed with its own calibration track
# record — signal-delivery §1. Kept here (above the "NOT folded" divider) because the fold now reads it.
EV_FORESIGHT_SELECTED = "foresight_selected"

# --- DIAGNOSTIC / SIDECAR events (deliberately NOT folded — `replay.fold` ignores them
#     (forward compat); they exist for the live activity feed / audit trail only). ---
EV_LOG_REPAIRED = "log_repaired"                # operator `repair-log`: provenance of a mid-file
#                                                 divergence recovery (backup + truncate boundary)
EV_REFLECTION_NOTE = "reflection_note"          # run-end LLM distillation: causal note + lessons + auto-skills
EV_LESSONS_RECONCILED = "lessons_reconciled"    # a node re-eval changed an outcome → this run's lessons
#                                                 citing it were retired + re-derived from the corrected state
EV_COMMAND_ACK = "command_ack"                  # engine folded a server command intent (causal ack)
EV_FINALIZE_STEP = "finalize_step"              # one logical finalize's replay-safe step gate
EV_REPORT_REFRESH_STARTED = "report_refresh_started"  # durable paid-work/idempotency claim
EV_REPORT_REFRESH_FAILED = "report_refresh_failed"    # sanitized terminal receipt; no report written
EV_CONCEPT_LENS_STARTED = "concept_lens_started"      # durable paid projection/idempotency claim
EV_CONCEPT_LENS_COMPLETED = "concept_lens_completed"  # validated spec or authoritative decline
EV_CONCEPT_LENS_FAILED = "concept_lens_failed"        # retry-safe pre-provider terminal failure
EV_SETUP_STARTED = "setup_started"
EV_SETUP_STEP = "setup_step"
EV_SETUP_FINISHED = "setup_finished"
EV_DRIFT_UNAVAILABLE = "drift_unavailable"
EV_INJECT_FAILED = "inject_failed"
EV_BUDGET = "budget"
EV_READMODEL_SKIPPED = "readmodel_skipped"
EV_DEPS_INSTALLED = "deps_installed"
EV_WORKSPACE_SEEDED = "workspace_seeded"
EV_RUN_SETUP_STARTED = "run_setup_started"
EV_RUN_SETUP_FINISHED = "run_setup_finished"
# Training-log monitor (engine/train_monitor.py): a NON-healthy verdict (watch|broken) from the per-eval
# LLM log observer. DIAGNOSTIC — the fold never reads it (advisory, splice-neutral even though it is
# appended from the concurrent monitor task), so it never touches node selection or replay; it exists for
# the owner attention feed + audit trail. Healthy verdicts stay trace-only (no event) to keep the log clean.
EV_TRAIN_MONITOR_ALERT = "train_monitor_alert"
# ASHA live-curve watchdog (engine/asha_monitor.py): a node whose latest INTERMEDIATE metric ranks below
# completed endpoints and/or comparable same-resource observations. New rows distinguish those verdicts;
# only enough underperforming same-resource evidence may trigger the opt-in kill. DIAGNOSTIC / fold-
# ignored; intervention is recorded by the node's single `node_failed` terminal, so concurrent append
# remains splice-neutral and replay-safe.
EV_ASHA_RANK = "asha_rank"

ALL_EVENT_TYPES: frozenset[str] = frozenset(
    v for k, v in globals().items() if k.startswith("EV_") and isinstance(v, str)
)

# Engine invariant #1 states "only the main task appends" — this set is the ONE enforced
# exception (previously a prose comment in `_spawn_research`): event types a BACKGROUND task may
# append. Membership requires BOTH properties, and `tests/test_background_appendable.py` proves
# them: (a) SELECTION-NEUTRAL — the fold never reads the event for node selection, so a
# thread-schedule-dependent position in events.jsonl cannot change which node wins; (b) the fold
# handles it ORDER-TOLERANTLY. (EV_HINT does mutate `pending_hints`, so a background hint racing
# an operator `replace` hint can change which STEERING text survives a resume — accepted:
# steering is transient advice; selection is what replay must pin. EV_HYPOTHESIS_ADDED only
# appends to the `hypotheses_added` board list — same class: board order is transient advice,
# selection is untouched.) `research_cadence._record_deep_research` gates its three domain records;
# EV_LLM_USAGE can additionally arrive through the durable accountant sink while that background
# role is calling its client. The splice test proves every member remains selection-neutral regardless
# of thread-dependent position.
BACKGROUND_APPENDABLE: frozenset[str] = frozenset({
    EV_RESEARCH_COMPLETED, EV_HINT, EV_HYPOTHESIS_ADDED, EV_LLM_USAGE,
})

# Conditional extension for legacy Hypothesis/Policy selection only. ``hypothesis_merged`` became a
# Card ownership/lifecycle input when native Card selection landed, so it is not universally neutral.
# The overlap call site must prove Card-driven selection is off; Card mode performs consolidation only
# on the joined main-task decision boundary.
NON_CARD_SELECTION_BACKGROUND_APPENDABLE: frozenset[str] = frozenset({
    EV_HYPOTHESIS_MERGED,
})

# Event types the fold DELIBERATELY does not handle — diagnostic / sidecar records that exist for the
# live activity feed, the audit trail, and observability, but never mutate the RunState projection
# (`replay.fold` skips them for forward-compat). This is the explicit half of the folded/diagnostic
# PARTITION that `tests/test_event_types.py` enforces: every registered type must be EITHER in
# `replay._HANDLERS` (folded) OR in this set (diagnostic), never both and never neither — so adding a
# new event type FORCES a conscious "does the fold read this?" decision (arch-review §5 P2: the old
# source-scan test went dead after the fold became a dispatch table, leaving coverage unprotected).
DIAGNOSTIC_EVENTS: frozenset[str] = frozenset({
    EV_SETUP_STARTED, EV_SETUP_STEP, EV_DRIFT_UNAVAILABLE, EV_INJECT_FAILED, EV_BUDGET,
    EV_READMODEL_SKIPPED, EV_DEPS_INSTALLED, EV_WORKSPACE_SEEDED, EV_RUN_SETUP_STARTED,
    EV_LOG_REPAIRED, EV_REFLECTION_NOTE, EV_LESSONS_RECONCILED,
    EV_COMMAND_ACK, EV_FINALIZE_STEP, EV_REPORT_REFRESH_STARTED, EV_REPORT_REFRESH_FAILED,
    EV_CONCEPT_LENS_STARTED, EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED,
    EV_TRAIN_MONITOR_ALERT,
    EV_ASHA_RANK,
    # EV_ENV_CHANGED moved to the FOLDED set (F18): it now sets a dedup flag (RunState.env_changed) so
    # the drift note is emitted once, not re-appended on every resume of an upgraded run.
})
