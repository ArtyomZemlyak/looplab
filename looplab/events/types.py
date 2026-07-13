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
* **Unknown event types are ignored** by the fold (forward compat): a newer writer never
  breaks an older reader, and diagnostic/sidecar events are pure observability.
* **Duplicate terminal events are idempotent** (first-terminal pattern): only a node's
  FIRST terminal event (``node_evaluated``/``node_failed``) contributes its eval time, so a
  corrupt/double-folded log can't inflate budgets or make the fold order-dependent.
* **Fulfillment gates** for operator requests use ``<x>_requests`` / ``<x>s_done`` counter
  pairs (``fork``/``fork_done``, ``inject_node``/``inject_done``, ``deep_research``/
  ``research_completed(served_manual)``): the engine serves ``len(requests) - done``
  outstanding items, so a resume never re-serves an already-fulfilled request.
* **Envelope versioning:** ``Event.v`` is written (always 1) but never read today — v=1 is
  the only envelope version; a future reader can key log migrations off it.

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
# Breadth read-model recorded at the strategist cadence (narrowing curve). Audit-only — it never
# affects node selection; folded ONLY so the at_node gate makes resume idempotent and the UI/replay
# can plot coverage over time. See looplab/search/coverage.py.
EV_COVERAGE_SNAPSHOT = "coverage_snapshot"
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
EV_PROMOTE = "promote"
EV_APPROVAL_GRANTED = "approval_granted"
EV_SPEC_APPROVED = "spec_approved"
EV_HYPOTHESIS_ADDED = "hypothesis_added"       # also engine-written after deep research
EV_HYPOTHESIS_UPDATED = "hypothesis_updated"
EV_HYPOTHESIS_MERGED = "hypothesis_merged"     # engine-written: fold alias hypotheses into a canonical
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
# role is calling its client. The splice test below proves every member remains selection-neutral
# regardless of thread-dependent position.
BACKGROUND_APPENDABLE: frozenset[str] = frozenset({
    EV_RESEARCH_COMPLETED, EV_HINT, EV_HYPOTHESIS_ADDED, EV_LLM_USAGE,
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
    EV_COMMAND_ACK, EV_FINALIZE_STEP,
    # EV_ENV_CHANGED moved to the FOLDED set (F18): it now sets a dedup flag (RunState.env_changed) so
    # the drift note is emitted once, not re-appended on every resume of an upgraded run.
})
