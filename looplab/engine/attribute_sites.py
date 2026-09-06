"""Where every `Engine` attribute is DECLARED — the registry behind `tests/test_engine_attribute_sites.py`.

THE DISEASE (doc 50 XP-08, doc 52 row 21). `Engine` is one class spread over twenty mixins, and
nothing declared its state: re-measured 2026-09-06 over `Engine.__mro__`, 261 data attributes, 218
assigned in `Engine.__init__`, 42 minted ONLY somewhere else (a memo written on first use, a lease
timestamp stamped by whichever resource method ran first, `_create_paused` assigned by five methods in
two files), and 125 names read through `getattr(self, "<name>", <default>)`. A typo in such a read
does not fail — it answers the default — and 143 blind handlers in this package stand ready to absorb
the `AttributeError` a bare read would have raised (the `_AshaStub` incident: an ASHA-only object
raised inside a per-tick containment `except` and the watchdog silently stopped producing verdicts).

THE GUARD FOUND ONE ON ITS FIRST RUN. `Settings.single_command_divergence_watch` shipped 2026-08-30
with a catalogue row, a legacy-snapshot row, a doc row and a test — and NO `EngineOptions` field and no
`__init__` assignment, so `eval_dispatch._run_eval`'s `getattr(self, "single_command_divergence_watch",
False)` answered False on every product run for a week. The source pin that "covered" it
(`health_check=bool(ex.divergence_watch)`) is on the RUNTIME's half of the plumbing and was green
throughout. That is the whole argument for a declaring-site rule: a read of a name nothing assigns is
not a runtime error here, it is a default, and only a census can see it.

THE RULE. Every data attribute the family reads — bare `self.x`, `getattr(self, "x", …)`,
`hasattr(self, "x")` — has exactly ONE declaring site:
  * an assignment in `Engine.__init__` (or a class-level name), the default; or
  * a row in `LAZY_ENGINE_ATTRIBUTES` below, naming the method(s) that mint it.
A read of an undeclared name is a red test. A new lazily-minted attribute is a red test until it is
either declared in `__init__` or registered here, and the row must name EXACTLY the methods that
assign it (both directions) — so the table cannot quietly drift from the tree.

THE TABLE IS A SHRINK-ONLY BACKLOG, like `tests/data/containment_unreviewed.txt`: every row is a
debt, and the fix for a row is to declare the attribute in `Engine.__init__` with its real default and
delete the row (the guard refuses a row whose name `__init__` also assigns, so a declared attribute
cannot keep a stale row). A row with more than one minting site is the strongest case for that fix —
its value today is "whichever method ran first". The `_ensure_*_state` families
(`speculation.py::_ensure_speculation_state`, `resources.py::_ensure_resource_state`) exist so a stub
that never ran `Engine.__init__` can still call the mixin — `tests/test_asha_monitor.py::_AshaStub` —
which is a reason to register them, not a reason for the reads around them to stay silent.

`GETATTR_DEFAULT_DRIFT` is the second, smaller backlog: a DECLARED attribute read through `getattr`
with two different defaults at two sites (`_gpu_ids` as `None` here and `[]` there). Each such pair is
two readers disagreeing about what an unset value means; the fix is to read the declared attribute
bare, or to agree on one default, and delete the row. The guard pins the set exactly — a new drift is
red, a fixed one must delete its row.

This module imports nothing: it is data, read by the test and by nobody else in the package.
"""
from __future__ import annotations

LAZY_ENGINE_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    # name -> every method that MINTS it (sorted), by `<module file>::<method>`.
    '_belief_refusal_said': ('research_cadence.py::_admissible_beliefs',),
    '_budget_wait_s': ('orchestrator.py::_defer_for_node_budget',),
    '_card_enrichment_attempted': ('research_cadence.py::_sync_card_enrichments',),
    '_card_scoring': ('strategy.py::_apply_strategy',),
    '_card_stage_attached_to': ('card_reservation.py::_stage_prepared_card',),
    '_card_stage_refusal': ('card_reservation.py::_stage_prepared_card',),
    '_command_ack_cursor': ('orchestrator.py::_ack_commands',),
    '_command_ack_first_event': ('orchestrator.py::_ack_commands',),
    '_command_ack_initialized': ('orchestrator.py::_ack_commands',),
    '_command_ack_seen': ('orchestrator.py::_ack_commands',),
    '_concept_cadence_attempted_at': ('concept_cadence.py::_maybe_snapshot_concept_coverage',),
    '_confirm_refusal_streak': ('confirm_phase.py::_pace_confirm_refusal', 'confirm_phase.py::_run_confirm_seed'),
    '_create_paused': ('orchestrator.py::_handle_create_actions', 'orchestrator.py::_refuse_degraded_proposal', 'orchestrator.py::_request_create_pause', 'speculation.py::_close_developer_sentinel_once', 'speculation.py::_create_precoded_node'),
    '_cross_run_advisory_receipt': ('proposal_cues.py::_cross_run_advisory_text',),
    '_cross_run_note_receipt': ('strategy.py::_cross_run_note_for_ctx',),
    '_deps_setup_derived': ('eval_dispatch.py::_settle_declared_deps',),
    '_eval_inflight': ('speculation.py::_ensure_speculation_state',),
    '_failed_direction_brief_cache': ('novelty.py::_failed_direction_asset_brief',),
    '_gpu_host_lease_notice_at': ('resources.py::_ensure_resource_state', 'resources.py::_note_gpu_host_lease_acquired', 'resources.py::_note_gpu_host_lease_contention'),
    '_gpu_host_lease_wait_since': ('resources.py::_ensure_resource_state', 'resources.py::_note_gpu_host_lease_acquired', 'resources.py::_note_gpu_host_lease_contention'),
    '_idea_identity_cache': ('novelty.py::_cached_prior_idea_identity',),
    '_idea_identity_warnings': ('novelty.py::_warn_incomplete_prior_identity',),
    '_landlock_cache': ('resources.py::_landlock_allow',),
    '_last_hyp_merge_n': ('research_cadence.py::_maybe_merge_hypotheses',),
    '_main_loop_thread_ident': ('orchestrator.py::run',),
    '_pending_batch_dropped': ('card_reservation.py::_stage_card_creates', 'novelty.py::_propose_batch', 'orchestrator.py::_handle_create_actions'),
    '_pending_batch_novelty_gated': ('card_reservation.py::_stage_card_creates', 'novelty.py::_propose_batch', 'orchestrator.py::_handle_create_actions'),
    '_pending_batch_telemetry': ('card_reservation.py::_stage_card_creates', 'novelty.py::_propose_batch'),
    '_pending_create_pause': ('orchestrator.py::_drain_create_pause', 'orchestrator.py::_handle_create_actions', 'orchestrator.py::_refuse_degraded_proposal', 'orchestrator.py::_request_create_pause'),
    '_pending_finalize_scope': ('orchestrator.py::_reentry_repin', 'orchestrator.py::_run_with_llm_broker'),
    '_read_fence_cache': ('resources.py::_read_fence_dir',),
    '_run_loop_exit_owed': ('orchestrator.py::_record_run_loop_exit', 'orchestrator.py::_run_with_llm_broker'),
    '_spec_build_inflight': ('speculation.py::_ensure_speculation_state',),
    '_spec_builds': ('speculation.py::_ensure_speculation_state',),
    '_spec_force_outer': ('speculation.py::_card_phase_serve_head', 'speculation.py::_ensure_speculation_state', 'speculation.py::_serve_card_builds'),
    '_spec_raw_stage_abandoned': ('speculation.py::_card_phase_serve_raw_stage',),
    '_spec_raw_stage_inflight': ('speculation.py::_card_phase_request_build', 'speculation.py::_ensure_speculation_state', 'speculation.py::_produce_raw_card_stage'),
    '_spec_raw_stage_result': ('speculation.py::_ensure_speculation_state', 'speculation.py::_produce_raw_card_stage', 'speculation.py::_serve_raw_card_stage'),
    '_spec_role_pair': ('speculation.py::_ensure_speculation_state', 'speculation.py::_producer_role_pair', 'strategy.py::_apply_strategy'),
    '_strategist_consulted_at': ('strategy.py::_maybe_consult_strategist',),
    '_strategist_plateau_seen': ('strategy.py::_maybe_consult_strategist',),
    '_verify_attempted': ('verifier_tiebreak.py::_maybe_verify_ties',),
}
GETATTR_DEFAULT_DRIFT: dict[str, tuple[str, ...]] = {
    # name -> the distinct default spellings its `getattr(self, name, <default>)` reads use.
    '_gpu_ids': ('None', '[]'),
    '_gpu_mem': ('None', '{}'),
    '_novelty_mode': ("'llm'", "'off'"),
    'eval_deadline_grace_s': ('0.0', 'None'),
    'run_dir': ("''", 'None'),
}
