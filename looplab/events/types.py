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

# --- DOMAIN events folded into RunState by `replay.fold`. The engine owns domain reduction and
#     effects; authenticated server/CLI paths may append explicitly allow-listed control intents or
#     receipts through EventStore serialization. Route every new non-engine append through the protocol's
#     allow-list and generation/authorization boundary instead of creating an ad-hoc writer. ---
EV_RUN_STARTED = "run_started"
# Emitted at the START of building a node — BEFORE the Researcher/Developer run — so the UI can show the
# node the instant work begins on it (its live agent-trace streams in) instead of only after the minutes-
# long dev session ends with node_created. A TRANSIENT marker (folds to st.building, NOT st.nodes) so it
# never affects node-id allocation or resume: node_created clears it and adds the real node.
EV_NODE_BUILDING = "node_building"
EV_NODE_CREATED = "node_created"
# The DURABLE eval-START boundary for one exact node lifecycle, appended at the dispatch decision by
# the MAIN task (`engine/speculation.py::_run_card_session`'s admission, with `_evaluate` as the
# funnel backstop for recovery/direct callers) and only for the lifecycles whose budget slot can
# later be REFUNDED (a speculative attempt-zero prefetch — see `search/card_selection.py`) — every
# other run's log bytes are unchanged. It exists because the log
# otherwise records evaluation cost only at the TERMINAL (`events/replay.py::_charge_terminal_cost`)
# and stage rows are appended inside the terminal's own write-lock block: a process killed 40 minutes
# into a training run therefore left a node byte-indistinguishable from one that was never dispatched,
# and the resumed process — whose in-memory `eval_inflight` set is empty — refunded its slot. Presence
# of this row is proof the sandbox was entered; ABSENCE is only evidence for a lifecycle whose
# node_created carries `eval_start_boundary`, which is the writer's own promise that it would have
# appended one. Data: {"node_id", "generation"}. Folded to `Node.eval_started`; a duplicate is a
# no-op and a node_reset clears it with the rest of the abandoned lifecycle.
EV_NODE_EVAL_STARTED = "node_eval_started"
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
# THE RECORD REPAIRED IN PLACE, for nodes whose evaluation predates `metric_provenance.
# applied_params` (merged 2026-08-20). Those rows keep the PROPOSAL — `Idea.params` — and every
# reader downstream presents it as the parameters that ran. Under `params_style: "none"` the engine
# applies nothing and the Developer realises the idea by EDITING THE REPO, so the two legitimately
# differ: measured over every run on disk, 41 of 457 comparisons (9.0%), 18 of them on nodes that
# produced a metric. The e5 champion at 0.793426 is recorded as batch 8192 / 15 epochs and ran
# batch 512 / 3 epochs — and that record is what put 8192 into the v3 task goal, which then died
# with three nodes and no metric.
#
# APPEND-ONLY, like `node_tombstoned` above and `concept_aliases.jsonl` beside it: nothing rewrites
# a historical row. The fold applies this at READ time and ONLY where the node has no applied-params
# record of its own — a live record always wins, so a backfill can never overwrite a measurement
# the engine made while the eval was running. Re-running the backfill is therefore idempotent by
# construction rather than by a check.
#
# Data: {"node_id", "generation", "applied_params" | null, "unrecoverable": str, "read_at": float,
#        "workdir_digest": str}. `applied_params: null` WITH an `unrecoverable` reason is a real
# answer and is folded as one — "the workdir is gone, so what ran cannot be recovered" is the honest
# record, and it is exactly the row that must not be mistaken for "the proposal is what ran".
EV_APPLIED_PARAMS_BACKFILLED = "applied_params_backfilled"
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
# THE OTHER HALF OF THE ROW ABOVE, and the reason it had to become an event rather than a nicer
# comment: `reward_hack_suspected` is written only WHEN SOMETHING IS FOUND, so a run in which every
# node was scanned and came back clean is byte-identical to a run in which the scan call was deleted
# — measured 2026-08-05, when deleting both `sigs +=` lines in `engine/evaluate.py` left 117 trust
# tests green. `trust_scan` is appended for EVERY evaluated node, hit or no hit, and says what was
# scanned (`code_digest`, the same subject digest the flagged row publishes), WHICH detectors ran,
# and how many findings came back. Never what they read: the flagged row already carries the detail,
# and a receipt quoting candidate text would be a second copy of the artifact inside the audit trail.
# DIAGNOSTIC on purpose — the fold ignores it, so no selection can move, and `_proposal_authority_seq`
# excludes DIAGNOSTIC_EVENTS wholesale so a per-node receipt cannot lose a reservation's CAS.
# (That fence stopped spanning a paid proposal on 2026-08-20 — see
# `engine/card_reservation.py::_proposal_receipt_fence` — so what the exclusion buys here is a
# retry, not a Developer call. `trust_scan` was inside 52 of the 56 windows the old one discarded.)
# The reader-side default lives in `trust/scan_receipt.py` and is the load-bearing half: a node with
# no receipt reads `unknown`, NEVER `clean` — which is the inversion this event exists to prevent.
EV_TRUST_SCAN = "trust_scan"
EV_NOVELTY_REJECTED = "novelty_rejected"
# PART IV D3 (§21.4, Phase 2b): the LIVE novelty gate GRADED a proposal over the concept graph and
# ALLOWED it despite a concept overlap the flat dedup gate would have rejected — a level-4 "same
# direction, different implementation" or a level-5 "re-opens a wrongly-abandoned failed direction".
# Behavioral admission receipt: the allow short-circuits the flat novelty gate, then folds onto the Card
# novelty signal and can steer which candidate is built. It never directly re-ranks evaluated metrics.
# Recorded only when `graded_novelty` is on; additive/reader-defaulted. See search/graded_novelty.py.
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
# Paid-attempt receipt for ONE Deep-Research step, appended BEFORE the provider call. The memo only
# becomes durable at `research_completed`, so a kill in between used to leave every trigger gate
# (manual counter / at_node cadence / strategist) still outstanding and resume paid for the same
# think twice. This receipt advances those gates on the ATTEMPT; `research_completed` carries the
# same `attempt_id` back so a completed attempt is not counted as still outstanding.
EV_RESEARCH_ATTEMPTED = "research_attempted"
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
#     serve/protocol.py, which is the authority CLAUDE.md names (serve/server.py only re-imports
#     it to keep the historical import path alive), and the control section of `replay.fold`). ---
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
# stage_finished is ENGINE-WRITTEN (engine/evaluate.py) and FOLDED (replay.py, into Node.stages); it
# is NOT in serve/protocol.py::CONTROL_EVENTS, so the UI may not append it. Filed in this section
# only for proximity to the other eval-progress rows — same caveat as
# foresight_selected/hypothesis_merged/the card ledger above.
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
EV_CARD_AUTO_DROPPED = "card_auto_dropped"      # engine lifecycle effect: reason + dropped_by=engine
EV_CARD_ENRICHED = "card_enriched"              # Layer 1b: novelty/cross-run/footprint delta (last-write-by-seq)
EV_CARD_RANKED = "card_ranked"                  # Layer 1b: board priority order (mirrors hypothesis_ranked)
# Layer 6 operator controls. These are generation-fenced, server-stamped command intents; they fold
# into dedicated override maps and are overlaid after every engine/Strategist projection.  Old logs may
# contain engine-authored ``card_dropped`` rows; replay keeps that format readable, but new domain writers
# use ``card_auto_dropped`` so command and progress consumers can classify new rows by type alone.
EV_CARD_REPRIORITIZED = "card_reprioritized"
EV_CARD_EDITED = "card_edited"
EV_CARD_RESOURCE_PINNED = "card_resource_pinned"
EV_CARD_DROPPED = "card_dropped"                # explicit operator stop intent (server-stamped)
# Layer 5's request/done execution ledger. Both are folded and main-task-only: the request is the
# durable selection+compute gate; done advances it after commit or an explicit producer-failure give-up.
EV_CARD_BUILD_REQUESTED = "card_build_requested"
EV_CARD_BUILD_DONE = "card_build_done"
# Paid-attempt receipt for ONE producer run against a request head. The request identifies LOGICAL
# work (this Card, this generation) and survives a kill, but a kill after the provider accepted the
# call and before the process-local result existed left the head open with no evidence that anything
# was already paid for — recovery just started another producer. This receipt is appended before the
# producer starts, so an unreconciled prior attempt is visible on resume and its head is quarantined
# instead of silently re-issued.
EV_CARD_BUILD_ATTEMPTED = "card_build_attempted"
# AUTO speculation depth RE-RESOLVING mid-run, from evidence the run measured about itself. FOLDED,
# main-task-only, and the reason it is an event rather than a process-local decision is engine
# invariant #3: the resolved depth IS the run's search treatment, so a change to it that only lived
# in memory would make a resume continue under a treatment its own log never recorded — the exact
# defect 5f86626d fixed for the concurrency widths.
#
# THE PAYLOAD MUST MAKE THE NEW DEPTH A FUNCTION OF THE LOG, NOT OF THE BOX. It carries the resolved
# integer AND the evidence it was derived from, so `replay.fold` READS the outcome and never
# re-measures anything: a resume on a different host reproduces the same treatment.
#
# The fold applies it as a MINIMUM (a one-way ratchet DOWN), which is what makes it order-tolerant
# and idempotent under invariant #5 — several rows in any order, or the same row twice, settle to the
# same depth. Absent on every pre-existing log, where the reader-side default is simply the depth
# `run_started` pinned, so old runs behave exactly as before.
EV_SPECULATION_DEPTH_SETTLED = "speculation_depth_settled"
# docs/29 F1 — the run's concurrency WIDTH, RE-PINNED mid-run from what the research proposed.
# `run_started` pins the width the run launched at, and that pin is load-bearing for replay: a resume
# on a different box continues the run's own treatment instead of re-deriving one off the new
# hardware (invariant #6). So a width derived from the PROPOSALS cannot be a per-turn recomputation —
# there has to be a durable row saying the run changed treatment, and this is it.
#
# ENGINE-APPENDED, and deliberately NOT `budget_extend`, which is the shape docs/29 F1 pointed at.
# Four reasons, each of which is a defect rather than a preference:
#   * `budget_extend` is a CONTROL event (`serve/protocol.py::CONTROL_EVENTS`), i.e. a HUMAN intent
#     the UI/CLI author. `_repin_settled_widths` stands aside on any axis a control event has taken
#     over — so the engine's own first repin would permanently disable the re-entry refusal that
#     invariant #6 rests on, for the operator too.
#   * it folds into `state.budget_overrides`, which `_apply_control_overrides` re-applies every turn.
#     Engine and operator writing the same cell leaves no way to say the human's number outranks the
#     research's, which is the precedence this feature needs.
#   * `_apply_control_overrides` RAISES on any non-empty `budget_overrides` during a speculation
#     calibration run — an engine-authored one would hard-crash the calibration lane.
#   * `search/speculation_quality.py::_FORBIDDEN_CALIBRATION_LIFECYCLE_EVENTS` contains
#     `budget_extend`, so writing one would silently disqualify every run as calibration evidence.
#
# The fold is LAST-WRITE-WINS into fields of its OWN (`eval_parallel_settled`/`llm_parallel_settled`),
# never onto `run_started`'s pins, which is what keeps it order-tolerant against `run_started` itself
# (invariant #5) — the exact defect `_on_speculation_depth_settled`'s own comment records. Unlike the
# depth ratchet this is TWO-WAY: proposals narrow the run when they declare wide footprints and widen
# it back when they stop, so a minimum would be wrong. Absent on every pre-existing log, where the
# reader-side default is `None` = "the run never repinned" = the `run_started` pin, unchanged.
EV_RUN_WIDTH_SETTLED = "run_width_settled"
EV_RUN_REOPENED = "run_reopened"
EV_RESUME_REQUESTED = "resume_requested"   # P1-1: durable resume intent, appended by /resume pre-spawn
EV_TRUST_GATE_CHANGED = "trust_gate_changed"   # server config edit; folded last-write-wins
# Predict-before-execute pick among K ideas / N code candidates. FOLDED into RunState.foresight_selected
# without re-ranking evaluated nodes, so the world model can be primed with its own calibration track
# and change later pre-execution picks. Kept above the "NOT folded" divider because the fold reads it.
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
# The SETUP phase's `setup_started`/`setup_step` pair exists because the pre-node work "is otherwise
# silent between run_started and the first node" (orchestrator.py's own comment at the append site).
# Every other multi-minute stretch of a run had the same hole and no such pair, so an operator watching
# a live run saw a frozen panel for the whole of it. `phase_progress` is that same idea generalized
# ONCE, rather than a new event type per silent stretch: a beacon a long operation emits at each of its
# own step boundaries so the activity feed, the status strip and the in-flight node card can all say
# WHICH step is running and for how long.
#
# ONE type, not one per stage, because invariant #7 makes every new type a registry + narration +
# diagram change, and the thing that actually has to be typo-proof is the (stage, phase) PAIR — which
# `PROGRESS_PHASES` below closes and `assert_progress_phase` enforces at every append site. A bare
# string phase would render as an empty chip and no test would notice.
#
# DIAGNOSTIC on purpose. It must never fold: it is emitted at a rate the fold has no reason to carry,
# it says nothing about selection, and a resume must reconstruct the same RunState from a log that has
# these rows and from one that does not. It is safe to append from a concurrent producer for the
# reason invariant #1 states for diagnostics — `speculation.py::_proposal_authority_seq` excludes
# DIAGNOSTIC_EVENTS wholesale, so a beacon landing inside a reservation's CAS window cannot lose it.
# That exclusion is the load-bearing property, NOT "the fold ignores it". The beacon has a second
# obligation since 2026-08-20 and it is the opposite kind: a phase that SPENDS must ALSO open a span
# (`engine/shared.py::_paid_progress`), because a beacon alone leaves its money untraceable.
EV_PHASE_PROGRESS = "phase_progress"
# WHICH long operation is reporting. Closed so that a stage name can be rendered as a label without
# every reader re-deriving the vocabulary.
PROGRESS_STAGE_BUILD = "build"      # idea -> code: one node's whole build, `_create_node_scoped`
# ONE stage, and the absence of a `resume` one is a MEASURED result rather than an omission. A resume
# is just as blank as a build — every line of `Engine._enter_run` runs before the loop's first turn,
# so no node, marker or pending count has moved and the run looks dead — and beacons were added there
# and REVERTED, because the event log is the wrong channel for that particular wait. Thirteen tests
# across four files broke, and each was a real property: `test_speculation_runtime_gate` pins the log
# BYTES as unchanged when the receipt gate rejects a run (an authorization property, and the gate sits
# below the read a beacon would have to bracket); `test_report`/`test_stop_finalize_resume` broke on
# finalize RECOVERY, where the appended rows changed which branch the wrap-up handshake took and
# minted a fresh PAID scope instead of resuming the existing one; `test_end_to_end` and
# `test_settled_width_pins` pin exact event counts across a resume. That is invariant #1's warning
# arriving in practice — the question is never "does the fold read it?" but "does any reader key on
# it?", and the prologue is precisely where the authorization fences, the finalize-scope
# reconciliation and the width pins all read the raw log. Making a resume visible needs a channel
# that is NOT events.jsonl (a run-dir progress sidecar the server tails is the obvious candidate);
# adding a stage here without that channel would reintroduce all thirteen.
PROGRESS_STAGES: frozenset[str] = frozenset({PROGRESS_STAGE_BUILD})
# The steps of each stage, in the order they actually happen. The ORDER is meaningful to a reader (a
# UI may render a stepper) but it is NOT a contract the engine has to satisfy, and a surface that
# assumes every phase appears will be wrong on most real runs. Which ones fire is CONFIGURATION-
# dependent: a mechanical merge/debug idea never crosses the novelty gate; `repair` replaces
# `implement` only on the `debug` operator's error-feedback branch; and `reserve` fires on the serial
# `_create_node_scoped` path but NOT on the batch/staged lane that the shipped default width takes,
# because that lane reserves elsewhere. Measured on the offline quadratic smoke run: 12 propose,
# 8 novelty, 16 implement, 0 reserve, 0 repair. What IS a contract is membership —
# `assert_progress_phase` refuses anything else, so a renamed step goes red at its append site rather
# than rendering as a blank chip.
PROGRESS_PHASES: dict[str, tuple[str, ...]] = {
    PROGRESS_STAGE_BUILD: (
        "propose",     # the Researcher's proposal call — the long, wholly invisible one: it runs
                       # BEFORE `node_building` is appended, so until it returns the UI has no node
                       # to draw at all and the strip falls through to "Planning next experiment…".
        "novelty",     # `_apply_novelty_gate`, which may pay for a whole second proposal
        "reserve",     # `_reserve_node_build` — the Card plan + the `node_building` append
        "implement",   # the Developer's implement call
        # NO `repair` phase. One existed for exactly one day: it bracketed `developer.repair` inside
        # the `debug` operator's build branch, and F5 deleted that branch on 2026-08-13 — the whole
        # justification for building a FRESH node to repair in was that the in-node loop had a fixed
        # count and had to hand off when it ran out, and F8 removed the count. Repair now happens
        # only inside `_evaluate`'s own loop, which is a different stage and would need its own
        # append site. Removed rather than left dangling, per the rule stated below: an entry no run
        # can reach renders a step the operator watches and never sees complete.
    ),
    # There is deliberately NO `commit` phase for the fold-verify-and-append tail after the Developer
    # returns. It is seconds, not minutes, and it already ENDS in `node_created` — a folded event the
    # feed, the strip and the DAG all react to today. A phase whose only signal duplicates an existing
    # one is noise, and a phase listed here that nothing emits would be worse: this table is what a
    # UI stepper would render, so an entry no run ever reaches shows the operator a step that never
    # completes. Add one here only together with its append site.
}
PROGRESS_STATUSES: frozenset[str] = frozenset({"started", "finished"})


def assert_progress_phase(stage: str, phase: str, status: str) -> None:
    """Refuse an unregistered `phase_progress` triple AT THE APPEND SITE.

    A progress beacon has no reader that fails loudly: the fold skips it, and a UI keyed on an
    unknown phase renders nothing. So a typo'd `"implememt"` would ship as a silently missing signal
    — the exact class of defect invariant #7 exists for, and the exact thing this whole change is
    trying to remove. Raising here makes it a red test in any run that reaches the site instead.
    """
    if stage not in PROGRESS_STAGES:
        raise ValueError(f"unknown progress stage: {stage!r}")
    if phase not in PROGRESS_PHASES[stage]:
        raise ValueError(f"unknown progress phase for stage {stage!r}: {phase!r}")
    if status not in PROGRESS_STATUSES:
        raise ValueError(f"unknown progress status: {status!r}")
# FOLDED, despite sitting in this diagnostic block: `replay.py::_on_setup_finished` is registered in
# `_HANDLERS`, and the constant is correctly absent from DIAGNOSTIC_EVENTS below (the partition test
# would fail otherwise). Called out here the same way EV_RUN_SETUP_STARTED/FINISHED are, so a reader
# scanning the section header does not conclude the fold ignores it.
EV_SETUP_FINISHED = "setup_finished"
EV_DRIFT_UNAVAILABLE = "drift_unavailable"
EV_INJECT_FAILED = "inject_failed"
EV_BUDGET = "budget"
EV_READMODEL_SKIPPED = "readmodel_skipped"
EV_DEPS_INSTALLED = "deps_installed"
# What the repo's own source tree DECLARES its dependencies to be, and what LoopLab did about it —
# appended once per run at run-setup, before any install. `deps_installed` records what pip was
# asked for; this records what the REPO asked for, which is the half no run has ever carried. An
# operator reading `runs/rubert-dr-0807` cannot today tell that its Lightning is 2.6.5 against a
# `pytorch_lightning==1.5.1` pin, and `run_started.env` cannot tell them either — its `libs` list is
# a FIXED 17 packages (`search/speculation_quality.py::speculation_environment_fingerprint`) chosen
# to pin a calibration identity, and pytorch-lightning/sentence-transformers/faiss/tensorboard are
# none of them. Carries the decision too (`action`), so a tier that may not install says so instead
# of being silently absent.
EV_DEPS_DECLARED = "deps_declared"
# One charge against `inline_repair_retrain_cap`, recorded WHERE IT HAPPENS. It cannot ride on
# `node_repaired` the way `changed`/`stages_passed` do: that event is appended BEFORE the loop asks
# `_repair_forces_full_retrain`, so the field would always carry the pre-charge count and a resume
# would refund the last re-train — which is the one thing this cap exists to prevent, since it guards
# GPU hours rather than attempts. Diagnostic, so the FOLD ignores it — which is NOT the same as "its
# splice position cannot matter", as this comment claimed when the event was added on 2026-08-06.
# Other readers key on position: `engine/speculation.py::_proposal_authority_seq` fenced a paid
# proposal by comparing a max-seq for equality and excluded only the LLM-accounting rows, so any
# diagnostic row appended during that window discarded a Developer call the run had paid for. That
# fence now excludes DIAGNOSTIC_EVENTS wholesale, which is what makes this event's position
# immaterial — a property of the READERS, not of the event.
# `engine/evaluate.py::_durable_full_retrains` reads it straight off the log.
EV_FULL_RETRAIN_CHARGED = "full_retrain_charged"
# ONE stage ROLLBACK decision — the Developer asserting that a LATER stage's failure was caused by an
# EARLIER stage that had already been counted successful, and the engine's answer. Written on BOTH
# outcomes (`accepted` true/false, with `refusal` naming which rung of the ladder said no) because the
# refusals are the half that has to be auditable: a rollback that is refused every attempt is a
# Developer stuck on a wrong guess, and a run where none are ever accepted is a feature nobody can
# use. Diagnostic for the same reason `full_retrain_charged` is, and safe for the same reason: no
# reader keys on its position, since `_proposal_authority_seq` excludes DIAGNOSTIC_EVENTS wholesale.
# `engine/evaluate.py::_durable_rollbacks` reads the ACCEPTED rows straight off the log, which is what
# makes "at most one rollback per suspect stage per node" survive a resume — a bound a resume refunds
# is not a bound, and this one guards hours of GPU time.
EV_STAGE_ROLLBACK = "stage_rollback"
# WHAT F8's REPAIR CRITIC ANSWERED, and on what evidence. One row per consultation, written whatever
# the verdict is — including `continue`, which is the row that matters most and the one a
# stop-shaped record would never hold.
#
# WHY IT IS AN EVENT AND NOT JUST A SPAN. The critic already had a `repair_critic` span, and it
# carried `{attempt, node_id, generation}` — the fact of a consultation and nothing about its answer.
# Measured on `rubertlite-dr-unified-v8`: 3 spans, `events: []` on every one, zero occurrences of the
# word "critic" anywhere in `events.jsonl`. Three reasons the span cannot be the record:
#   * `spans.jsonl` is DESTROYABLE and OPTIONAL. `serve/trace_clear.py` is an operator-facing button
#     that rewrites it, `serve/reset_transaction.py` lists it among the files a reset removes, and
#     tracing can be off entirely (`looplab timings` prints "tracing off or pre-tracing run"). F8's
#     premise is that a JUDGEMENT replaces a counter as the stop rule; the record of a stop rule
#     cannot be the one file the product offers to delete.
#   * Three of the six `CRITIC_SOURCES` never open a span at all — `not_wired` and `no_trajectory`
#     return before the `with`, and `unreachable` closes it as an error carrying no verdict. Those
#     are exactly the cases where "6 consultations, 0 stops" is misleading.
#   * The verdict has to be joinable to the durable repair chain it judged (`node_repaired` rows in
#     the same log), and a cross-file join through a sidecar with its own health caveats is not a
#     join an operator will make.
# It is NOT FOLDED, and that is the same decision stated from the other side: a `continue` moves no
# metric, champion, selectability or violation (`engine/evaluate.py` says so at the call site, doc 36
# requires it), so folding it would create `RunState` derived from an LLM verdict — one refactor away
# from a selection reader, which is precisely doc 36's line. A STOP does change the run, but through
# the `abandon` triage outcome and the node terminal that already fold; this row is the REASON beside
# them, never a second authority for them.
# Appended from `_evaluate`'s attempt loop under `_write_lock` — a concurrent eval child, which is
# invariant #1's `DIAGNOSTIC_EVENTS` seam (`deps_installed`/`full_retrain_charged` are appended from
# the same loop). Position-immaterial for the same READER-side reason they are:
# `speculation.py::_proposal_authority_seq` excludes DIAGNOSTIC_EVENTS wholesale.
EV_REPAIR_CRITIC_VERDICT = "repair_critic_verdict"
EV_WORKSPACE_SEEDED = "workspace_seeded"
# FOLDED (moved out of DIAGNOSTIC_EVENTS): the start of an arbitrary operator `run_setup` command is
# the only evidence that its side effects may have been applied. Without folding it, a kill between
# start and finish was indistinguishable from "never ran" and resume re-executed the command while
# still calling itself exactly-once. `RunState.run_setup_open` now carries that ambiguity forward.
EV_RUN_SETUP_STARTED = "run_setup_started"
EV_RUN_SETUP_FINISHED = "run_setup_finished"
# ------------------------------------------------------------------ live-watchdog log roles
# WHICH eval phase a watchdog verdict is about. These live here, not in `engine/train_monitor.py`,
# because they are part of the DURABLE vocabulary of `EV_TRAIN_MONITOR_ALERT.log_role` (below) —
# `events/digest.py` and every other reader must be able to name them without importing the engine
# (layering: `events` imports only `core`). `engine/train_monitor.py` imports and re-exports them, so
# `train_monitor.LOG_ROLE_*` remains the spelling engine code and tests already use.
#
# The roles are assigned by `train_monitor.eval_log_plan` from the eval's OWN resolved pipeline, and
# only `LOG_ROLE_TRAINING` carries kill authority — see that function's docstring for why a pipeline
# work stage cannot be PROVEN to be the training step and is therefore advisory.
LOG_ROLE_TRAINING = "training"    # the log that is the WHOLE eval's own work — judged AND kill-eligible
LOG_ROLE_WORK = "work"            # a pipeline work stage: judged (advisory), never kill authority
LOG_ROLE_SETUP = "setup"          # pip/dep install: no loss trajectory exists yet, by construction
LOG_ROLE_SCORE = "score"          # the pipeline's final, metric-producing stage: training already FINISHED
LOG_ROLE_AMBIGUOUS = "ambiguous"  # one filename, more than one possible writer — unattributable
LOG_ROLE_UNKNOWN = "unknown"      # no plan available / a log the plan cannot name — advisory only
LOG_ROLES: frozenset[str] = frozenset({
    LOG_ROLE_TRAINING, LOG_ROLE_WORK, LOG_ROLE_SETUP, LOG_ROLE_SCORE,
    LOG_ROLE_AMBIGUOUS, LOG_ROLE_UNKNOWN,
})

# Training-log monitor (engine/train_monitor.py): a NON-healthy verdict (watch|broken) from the per-eval
# LLM log observer. DIAGNOSTIC — the fold never reads it (splice-neutral even though the concurrent
# monitor appends it), so it cannot directly change lifecycle, ranking, or replay. The raw
# event may still enter a later Researcher prompt through watchdog_reflection; it also feeds owner
# attention + audit. Healthy verdicts stay trace-only except for a recovery transition after an alert.
# Every row carries the STAGE ATTRIBUTION the verdict was formed about: `log_role` (one of LOG_ROLES
# above) and, when the plan could name it, `stage`. They are what makes "which phase of the eval was
# this said about?" answerable from the durable log rather than only from the high-volume trace
# sidecar — a `broken` verdict about a `data_prep` stage narrated to the next Researcher as "training
# flagged broken ... loss is stuck at its initialization value" is a false statement about a node whose
# training had not started. Both are ADDITIVE: rows written before 2026-08-05 carry neither, and
# readers must default (an absent `log_role` means the attribution is unknown, NOT that it was training).
# When the monitor DECIDES to stop the run it also stamps the kill-attribution pair described on
# EV_ASHA_VERDICT below (`stop_decided` + `kill`, plus `kill_superseded_by` when a sibling watchdog won
# the claim); those fields are absent on an ordinary advisory row.
# `trajectory` (2026-08-14, additive) is the ENGINE'S OWN deterministic measurement of this stage's
# loss over the whole eval so far — direction/first/last/minimum/noise/net/points, from
# `engine/train_monitor.py::trajectory_row` — recorded BESIDE the model's verdict because the verdict
# is formed from a tail worth ~30 seconds of a multi-hour run and the two can disagree. It states
# what the loss DID, never what anyone concluded; `trajectory_veto: true` marks a row where the
# measurement is what refused a kill the other four conjuncts would have granted. Readers must
# default an absent `trajectory` to "the engine measured nothing" (an old row, or a log printing no
# parseable loss — `runs/rubert-dr-0807` writes 45 MB containing the string "loss" zero times),
# NEVER to "the loss was flat".
EV_TRAIN_MONITOR_ALERT = "train_monitor_alert"
# ASHA live-curve watchdog (engine/asha_monitor.py): a node whose latest INTERMEDIATE metric ranks below
# completed endpoints and/or comparable same-resource observations. New rows distinguish those verdicts;
# only enough underperforming same-resource evidence may trigger the opt-in kill. DIAGNOSTIC / fold-
# ignored; intervention is recorded by the node's single `node_failed` terminal, so concurrent append
# remains splice-neutral and replay-safe. The raw advisory row can still feed a later proposal through
# watchdog_reflection; that prompt effect is separate from fold/champion semantics.
EV_ASHA_RANK = "asha_rank"
# The ASHA watchdog's LLM STOP DECISION (engine/asha_monitor.py). The deterministic rank test above is
# only the EVIDENCE; when it fires past the grace window a judge sees the live curve, the same-resource
# sibling distribution + bar, the other metrics the run prints, and the training monitor's latest health
# verdict, and answers continue|watch|stop (`status`; "unavailable" when no client answered).
# Kill attribution is TWO bits, neither of which is "the node stopped" — that is knowable only from the
# node's own terminal, and this row is written before it exists: `stop_decided` is what this watchdog
# decided, and `kill` whether it then WON the shared per-eval claim (`train_monitor.claim_watchdog_kill`)
# and therefore owns the terminal's reason; `kill_superseded_by` names the sibling that won instead.
# `kill: true` still does not prove a stop, because `engine/evaluate.py` reaches its kill branch only
# after THREE pre-emptions, any of which returns first with a DIFFERENT terminal: a `reset` that
# SUPERSEDED this lifecycle (`_record_superseded`), an operator `abort`/Card drop (`aborted and not ok`),
# and a usable result (`if kill_signal.get("kill") and not ok` — a kill claimed against an eval that
# had already produced a metric ends in `node_evaluated`). So a `kill: true` row can sit beside a
# superseded, aborted OR evaluated terminal, not only beside `node_failed`.
# Join to the node's single terminal for the outcome; these fields answer "which watchdog
# decided/claimed what" — a renderer that branches on `kill` alone (as `ui/src/Dock.jsx` once did)
# reports a superseded claim as an early kill. DIAGNOSTIC / fold-ignored as
# EV_ASHA_RANK: the judge runs in a concurrent per-eval task, so its splice position is thread-dependent
# and must not reach folded state — the stop itself is recorded by the node's single `node_failed`
# terminal (reason=asha_underperforming), which is what replay reads instead of re-invoking the LLM.
EV_ASHA_VERDICT = "asha_verdict"
# A served `fork` request whose `fork_done` receipt was spent but which produced NO node. The receipt
# is appended BEFORE `_create_node` on purpose (at-most-once beats duplicating a paid experiment), and
# `_create_node` can then decline silently in a live process: a lost proposal-authority CAS, a slot
# race, `paused`, or the novelty/card-contract gate dropping the proposal. The operator's request then
# vanished with the Researcher call already paid and nothing in the log saying so. DIAGNOSTIC /
# fold-ignored — the fork cursor already advanced, so this only makes the drop legible in the event
# log, `looplab replay` and the trace; it never re-serves the request or changes selection.
EV_FORK_UNFULFILLED = "fork_unfulfilled"
# The SHARED cross-run lessons store (`memory_dir/lessons.jsonl`, `meta_notes.jsonl`) could not be
# read or written — permissions, a full/quota'd/read-only network mount, a transient FS fault. That
# store is a DIFFERENT filesystem from the run dir, so a run whose own events.jsonl appends succeed
# can still lose every cross-run prior. Cross-run memory is advisory ("the store misses one batch"),
# so both sides degrade instead of failing the run; this row is how the degrade stays legible in the
# event log, `looplab replay` and the trace. `mode` is "read" or "write". DIAGNOSTIC / fold-ignored:
# it changes no state, and the read side deliberately does NOT advance `lessons_refreshed`, so the
# next cadence retries the same still-unread store rather than treating it as read.
EV_LESSONS_STORE_UNAVAILABLE = "lessons_store_unavailable"

ALL_EVENT_TYPES: frozenset[str] = frozenset(
    v for k, v in globals().items() if k.startswith("EV_") and isinstance(v, str)
)


def standing_hint_dedup_key(data) -> tuple[object, str]:
    """Semantic identity for one standing hint without collapsing authority classes.

    Deep Research still writes the legacy ``hint`` event for replay compatibility, but its model
    output is advisory rather than operator-authored. Equal text across those two classes is not a
    duplicate: a later human instruction must remain durable and reach the operator-authority prompt
    block. All historical/non-deep sources retain the operator class for backward compatibility.
    """
    row = data if isinstance(data, dict) else {}
    authority = "deep_research_advisory" if row.get("source") == "deep_research" else "operator"
    return row.get("text"), authority

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
    EV_RESEARCH_COMPLETED, EV_RESEARCH_ATTEMPTED, EV_HINT, EV_HYPOTHESIS_ADDED, EV_LLM_USAGE,
})

# Invariant #1's OTHER thread-side seam, registried for the same reason. `_ensure_run_setup`
# (engine/eval_dispatch.py) appends these FOLDED events from an eval WORKER THREAD — `_run_eval`
# runs under `anyio.to_thread` from `_evaluate` — so they are outside `_write_lock` and outside
# BACKGROUND_APPENDABLE/DIAGNOSTIC_EVENTS/the per-node parallel-build seam. It is safe because
# `EventStore.append` serializes bytes, `_run_setup_lock` makes the section once-per-run, and the
# fold keys `run_setup_open`/`run_setup_done` purely BY COMMAND — never by position, node or
# ordering against any other event. That argument was prose at the append site and nothing checked
# it; membership here is asserted at the append site and guarded by
# `tests/test_setup_thread_appendable.py`, so widening thread-side folded appends can no longer
# happen silently.
SETUP_THREAD_APPENDABLE: frozenset[str] = frozenset({
    EV_RUN_SETUP_STARTED, EV_RUN_SETUP_FINISHED,
})

# Conditional extension for legacy Hypothesis/Policy selection only. ``hypothesis_merged`` became a
# Card ownership/lifecycle input when native Card selection landed, so it is not universally neutral.
# The overlap call site must prove Card-driven selection is off; Card mode performs consolidation only
# on the joined main-task decision boundary.
#
# ITS BYTE POSITION IS NOW LOAD-BEARING, which is invariant #1's real question ("does any reader key
# on its position?") answered YES since `card_ledger`'s `aliases.canon_at`: a `card_dropped` resolves
# its target through the merge edges durable AT THAT POINT, so a drop landing before vs after a
# merge receipt folds to a different card. In LEGACY mode this event may be appended from the
# concurrent research task while an operator `card_dropped` control append races it, and neither
# writer orders against the other. The Card-mode gate is what keeps that unreachable where it would
# matter — consolidation happens on the joined main-task boundary — and it is the reason this
# registry is conditional rather than a plain extension of BACKGROUND_APPENDABLE. Any further use of
# `canon_at` has to re-ask this question, and `tests/test_background_appendable.py`'s
# splice-neutrality proof does not model a racing drop.
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
    EV_SETUP_STARTED, EV_SETUP_STEP, EV_PHASE_PROGRESS,
    EV_DRIFT_UNAVAILABLE, EV_INJECT_FAILED, EV_BUDGET,
    EV_READMODEL_SKIPPED, EV_DEPS_INSTALLED, EV_DEPS_DECLARED, EV_FULL_RETRAIN_CHARGED,
    EV_STAGE_ROLLBACK, EV_REPAIR_CRITIC_VERDICT, EV_TRUST_SCAN,
    EV_WORKSPACE_SEEDED,
    EV_LOG_REPAIRED, EV_REFLECTION_NOTE, EV_LESSONS_RECONCILED,
    EV_COMMAND_ACK, EV_FINALIZE_STEP, EV_REPORT_REFRESH_STARTED, EV_REPORT_REFRESH_FAILED,
    EV_CONCEPT_LENS_STARTED, EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED,
    EV_TRAIN_MONITOR_ALERT,
    EV_ASHA_RANK,
    EV_ASHA_VERDICT,
    EV_FORK_UNFULFILLED,
    EV_LESSONS_STORE_UNAVAILABLE,
    # EV_ENV_CHANGED moved to the FOLDED set (F18): it now sets a dedup flag (RunState.env_changed) so
    # the drift note is emitted once, not re-appended on every resume of an upgraded run.
})
