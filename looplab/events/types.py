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
#     except `spec_approved`/`approval_granted` which the CLI/UI may also ratify). ---
EV_RUN_STARTED = "run_started"
EV_NODE_CREATED = "node_created"
EV_NODE_EVALUATED = "node_evaluated"
EV_NODE_FAILED = "node_failed"
EV_NODE_REPAIRED = "node_repaired"
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
EV_DIVERSITY_ARCHIVE = "diversity_archive"
EV_LLM_COST = "llm_cost"
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
EV_FORK_DONE = "fork_done"               # fulfillment gate for `fork` requests
EV_INJECT_DONE = "inject_done"           # fulfillment gate for `inject_node` requests
EV_RESEARCH_COMPLETED = "research_completed"   # memo sidecar + gate for `deep_research`
EV_LESSONS_DISTILLED = "lessons_distilled"
EV_LESSONS_REFRESHED = "lessons_refreshed"
EV_REPORT_GENERATED = "report_generated"
EV_CONFIRM_DONE = "confirm_done"         # fulfillment gate for `force_confirm` requests

# --- CONTROL events (live operator/UI intents appended to the same log; the engine reads
#     the folded intent and writes the matching DOMAIN effect — see CONTROL_EVENTS in
#     serve/server.py and the control section of `replay.fold`). ---
EV_RUN_ABORT = "run_abort"
EV_PAUSE = "pause"
EV_RESUME = "resume"
EV_NODE_ABORT = "node_abort"
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
EV_TRUST_GATE_CHANGED = "trust_gate_changed"   # server config edit; folded last-write-wins

# --- DIAGNOSTIC / SIDECAR events (deliberately NOT folded — `replay.fold` ignores them
#     (forward compat); they exist for the live activity feed / audit trail only). ---
EV_FORESIGHT_SELECTED = "foresight_selected"   # predict-before-execute pick among K ideas / N code candidates
EV_REFLECTION_NOTE = "reflection_note"          # run-end LLM distillation: causal note + lessons + auto-skills
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
