# Event reference

**Generated** from `looplab/events/types.py::EVENT_PAYLOAD_KEYS` by
`python -m looplab.events.event_reference` and pinned by `tests/test_event_payload_contract.py`
(doc 52 row 30). Every row of `events.jsonl` carries one of these types; the columns say whether
`replay.fold` reads it, what the payload records, and which keys it carries.

Read the columns like this:

* **folded** — `replay.fold` reduces it into `RunState`, so it is part of the replayable run state
  (engine invariant #4). **diagnostic** — the fold ignores it by design: an audit, activity-feed or
  observability row that never changes what the search decides.
* **whole** — the handler keeps the payload OBJECT rather than named keys, so every key a writer
  puts there reaches `RunState` and every projection over it. A key added to one of those types is
  a new field of the UI's data model.
* **required** is what a writer must write today (each one is checked against every literal writer);
  **optional** is everything else. Neither is a claim about OLD rows: a log written before a key
  existed does not carry it, and the fold defaults both halves — which is engine invariant #5, and
  is proved by folding every type here with an empty payload.

The event type itself is the contract's identity and is never renamed or reused; see the head of
`looplab/events/types.py` for the four steps that add one.

<!-- generated: event types -->

155 event types — 112 folded into `RunState`, 43 diagnostic; 899 declared payload keys; 26 types whose whole payload is stored by the fold.

| type | fold | records | required keys | optional keys |
|---|---|---|---|---|
| `ablate` | folded · whole | One ablation of the champion's code: which blocks were removed and what each removal cost the metric. | `generation`, `impacts`, `parent_id` | `ablation_id`, `attempt`, `blocks`, `eval_seconds`, `mode`, `skipped`, `superseded`, `top_block` |
| `agent_checkpointed` | diagnostic | An agentic role's mid-loop checkpoint: the turn, the plan it works from, the todo updates it just made. | `label`, `plan`, `plan_updates`, `todos`, `turn` | — |
| `agent_decision` | folded · whole | The unified agent's pick of the next action, beside the legal set it was offered. | `at_node`, `chosen`, `legal`, `rationale`, `recommended` | — |
| `agent_phase_completed` | diagnostic | One agentic phase ended: how it exited, after how many turns and seconds. | `exit`, `label`, `plan_updates`, `seconds`, `turns` | — |
| `agent_phase_started` | diagnostic | One agentic phase began: its label, tool surface and the turn/time budget it was given. | `emit`, `label`, `max_turns`, `time_budget_s`, `tools` | — |
| `agent_validated` | folded | The Developer's self-validation over a build: which checks ran, whether it shipped, after how many attempts. | — | `attempt`, `attempts`, `checks`, `fell_back`, `generation`, `node_id`, `ok`, `shipped_ok` |
| `annotation` | folded | An operator note pinned to one node. | `text` | `node_id` |
| `applied_params_backfilled` | folded | What the configuration that actually RAN assigned to the declared params, read back off the workdir. | `applied_params`, `generation`, `node_id`, `read_at`, `unrecoverable`, `workdir_digest` | — |
| `approval_granted` | folded | The operator ratified the node the run paused on (HITL). | `generation`, `node_id` | `attempt` |
| `approval_requested` | folded | The run paused for a human decision about one node, at a named log position. | `after_seq`, `generation`, `metric`, `node_id` | `attempt` |
| `asha_rank` | diagnostic | One ASHA tick's ranking of a running node against its comparable population. | `comparable_population`, `direction`, `endpoint_underperforming`, `generation`, `intermediate`, `kill_comparable`, `node_id`, `population`, `quantile`, `resource_underperforming`, `underperforming` | — |
| `asha_verdict` | diagnostic | The ASHA judge's call on a persistently underperforming node: stop or spare, with confidence. | `comparable_population`, `confidence`, `direction`, `generation`, `intermediate`, `kill`, `node_id`, `quantile`, `reason`, `status`, `stop_decided`, `under_streak` | `confidence_valid`, `kill_superseded_by`, `train_monitor_status` |
| `belief_admission` | diagnostic | How many researcher-proposed beliefs one proposal turn offered and how many the board admitted. | `admitted`, `blank`, `board_read`, `capped`, `proposed`, `repeated`, `restated`, `shape` | — |
| `best_confirmed` | folded | The champion the run confirmed by re-evaluation, and whether that confirmation was significant. | `generations`, `node_id`, `search_epoch`, `significant` | `attempt`, `generation` |
| `budget` | diagnostic | The finalization budget receipt: wall clock, in-process seconds, evaluation seconds and node count. | — | `elapsed_s`, `eval_s`, `finalize_scope`, `finish_seq`, `nodes`, `process_s`, `speculation` |
| `budget_extend` | folded | An operator raising a live run's node, time or parallelism budget. | — | `add_nodes`, `eval_parallel`, `llm_parallel`, `max_eval_seconds`, `max_parallel`, `max_seconds`, `parallel_build`, `timeout` |
| `card_added` | folded · whole | A research Card minted into durable inventory: its id, statement and the action it owns. | — | `action`, `at_node`, `concepts`, `footprint`, `generation`, `id`, `idea`, `node_id`, `ownership_receipt`, `parent_card_id`, `parent_generations`, `parent_id`, `parent_ids`, `rationale`, `scored_against`, `scored_against_empty`, `scored_against_generation`, `source`, `statement`, `steering_context` |
| `card_auto_dropped` | folded | The engine dropped a Card as a lifecycle effect, with the reason (`dropped_by=engine`). | `dropped_by`, `id`, `reason` | `by` |
| `card_build_attempted` | folded | One dispatch attempt for a Card's build, indexed so a repeat is visible instead of silently re-issued. | `card_id`, `generation`, `index` | — |
| `card_build_done` | folded | A Card's build finished: the node it produced, or the reason it was skipped. | `error`, `eval_seconds`, `generation`, `node_id`, `reason` | `card_id`, `skipped`, `speculative` |
| `card_build_requested` | folded | The durable selection-and-compute gate for one Card's build. | `card_id`, `generation` | — |
| `card_dropped` | folded | The operator stopped a Card (server-stamped). | `id` | `by`, `dropped_by`, `reason` |
| `card_edited` | folded | The operator rewrote a Card's statement. | `id` | `source`, `statement` |
| `card_enriched` | folded · whole | A Card's novelty / cross-run / footprint delta (last write by seq wins). | — | `claim_refs`, `concept_tags`, `confidence`, `cross_run_prior`, `footprint`, `foresight_rank`, `generation`, `id`, `lesson_refs`, `node_id`, `novelty_verdict`, `proposal_ref`, `research_origin`, `steering_context` |
| `card_merged` | folded | Alias Cards folded into a canonical one, with the seq that decided the edge. | `aliases`, `canonical`, `merged_by`, `source_event_seq` | `statement` |
| `card_ranked` | folded | The board's priority order over the Cards, with per-Card confidence and reason. | — | `at_node`, `confidence`, `order`, `ranked`, `reason` |
| `card_reopened` | folded | The operator resumed a dropped Card (server-stamped). | `id` | `by`, `dropped_by`, `reason` |
| `card_reprioritized` | folded | The operator moved one Card's priority. | `id` | `pinned`, `priority`, `source` |
| `card_resource_pinned` | folded | The operator pinned one Card's GPU footprint. | `id` | `gpu_mem_mib`, `gpus`, `pinned`, `source` |
| `command_ack` | diagnostic | The engine folded one server command intent — the causal ack that closes it. | `command_id`, `event_seq` | — |
| `comment_created` | folded | An operator comment on one node, at that node's generation. | `node_id` | `node_generation`, `text` |
| `comment_edited` | folded | A new revision of one comment, compare-and-swapped against the version the author saw. | `comment_id` | `expected_version`, `node_generation`, `node_id`, `text` |
| `comment_resolution_changed` | folded | One comment's resolved flag moved, compare-and-swapped against the version the author saw. | `comment_id` | `expected_version`, `node_generation`, `node_id`, `resolved` |
| `concept_consolidation` | folded | A concept-vocabulary consolidation: which ids were renamed into which. | `mode`, `rename` | — |
| `concept_coverage_snapshot` | folded | The concept-coverage gate's snapshot at one node, and which of its rules fired. | — | `at_node`, `current_streak`, `directive`, `experiments`, `fired`, `locked_axis`, `projection_token`, `recent_axis`, `streak`, `tag_mode`, `top_concept`, `top_concept_frac`, `uncovered_axes`, `uncovered_key` |
| `concept_edge` | folded | Edges added to the concept graph, and the mode that derived them. | `edges`, `mode` | — |
| `concept_lens_completed` | diagnostic | A paid concept-lens projection's terminal: the validated spec, or an authoritative decline. | `generation`, `lens_request_id`, `outcome`, `reason`, `request_digest`, `resolution`, `resolution_id` | — |
| `concept_lens_failed` | diagnostic | A concept-lens attempt that failed before any provider call — retry-safe, no projection written. | — | `error_kind`, `generation`, `lens_request_id`, `request_digest` |
| `concept_lens_started` | diagnostic | The durable idempotency claim for one paid concept-lens projection. | — | `generation`, `lens_request_id`, `request_digest` |
| `concept_tag_edited` | folded | The operator re-tagged one node's concepts; the classifier cadence must not clobber it. | `node_id` | `concepts`, `node_generation` |
| `confirm_done` | folded | The fulfillment receipt for one `force_confirm` request. | `generation`, `node_id` | `attempt` |
| `confirm_eval` | folded | One seed of a champion-confirmation re-evaluation, with its metric and eval seconds. | `eval_seconds`, `generation`, `metric`, `node_id`, `seed` | `attempt`, `error`, `reason`, `superseded` |
| `coverage_snapshot` | folded · whole | The search's coverage snapshot at one node. | — | `at_node`, `dominant_theme_frac`, `niches`, `nodes`, `operators`, `projection_token`, `recent_dominant_frac`, `theme_entropy`, `themes`, `top_themes` |
| `cross_run_prior` | folded · whole | Concepts of this proposal a SIMILAR earlier run already tried, and how those runs went. | — | `concept_source`, `literature`, `matched_concepts`, `prior_runs`, `prior_runs_complete`, `prior_runs_omitted`, `prior_runs_total`, `stance`, `v` |
| `data_leakage` | folded · whole | The deterministic leakage scan's verdicts over the task's data. | `leak`, `verdicts` | — |
| `data_profiled` | folded | The task's data profile: the columns the bounded profiler read. | `columns` | — |
| `data_provenance` | folded · whole | Where each of the task's declared data assets came from. | `assets` | — |
| `data_shift` | diagnostic | How far the deployment sample the task declares is from the training one, per column. | `checked`, `columns`, `detector`, `n_columns`, `n_shifted`, `only_current`, `only_reference`, `shift`, `source` | — |
| `deep_research` | folded · whole | An operator request for a deep-research pass — the intent itself, with no payload. | — | — |
| `deps_declared` | diagnostic | The dependency directives a task declared, what the resolver pinned, and what it dropped. | `action`, `command`, `digest`, `directives`, `dropped`, `env_delta`, `file`, `observed`, `pin_count`, `pins`, `pins_truncated`, `root` | — |
| `deps_installed` | diagnostic | The packages one evaluation installed and how they resolved. | `generation`, `node_id`, `packages`, `resolved`, `round` | `source` |
| `diversity_archive` | folded · whole | The finalization snapshot of the diversity archive. | — | `elites`, `finalize_scope`, `finish_seq`, `niches`, `resolution` |
| `drift_unavailable` | diagnostic | Why the run could not compare its environment against the one it started in. | `reason` | — |
| `effective_train_batch` | diagnostic | What the training process itself recorded as the batch it ran at, read off the node's own workdir at the metric read. | `disagree`, `generation`, `node_id`, `read_at`, `readings`, `train_batch_size` | `files_seen`, `truncated` |
| `env_changed` | folded | A resume observed that the Python/library environment differs from the one the run started in. | `now`, `was` | — |
| `eval_invocation_claimed` | diagnostic | One paid evaluation attempt is about to invoke the evaluator, under a reconciliable id. | `attempt`, `generation`, `invocation_id`, `node_id` | `after_interrupted_attempt` |
| `eval_invocation_settled` | diagnostic | That evaluator invocation returned, with the outcome and the seconds it charged. | `attempt`, `eval_seconds`, `generation`, `invocation_id`, `node_id`, `outcome` | — |
| `eval_noise_floor` | folded | The repeated-seed spread of ONE candidate's metric: the run's own evaluation noise floor. | `generation`, `mean`, `metrics`, `n`, `node_id`, `profile`, `search_metric`, `seeds`, `sem`, `spread`, `std` | `reason` |
| `eval_noise_seed` | folded | One repeat of that candidate's evaluation, with its seed, metric and eval seconds. | `eval_seconds`, `generation`, `metric`, `node_id`, `seed` | `superseded` |
| `finalization_finished` | folded | The wrap-up for one finish (keyed by that finish's seq) completed. | `finish_seq` | — |
| `finalize_step` | diagnostic | One replay-safe step gate inside a single logical finalization. | — | `after_seq`, `finish_data`, `finish_report_planned`, `outcome`, `scope`, `step` |
| `force_ablate` | folded | The operator asked for an ablation of one node. | `node_id` | `attempt`, `generation` |
| `force_confirm` | folded | The operator asked for a confirmation re-evaluation of one node. | `node_id` | `attempt`, `generation` |
| `foresight_selected` | folded | The pre-execution foresight pick among candidate actions, with its confidence. | — | `attempt`, `confidence`, `generation`, `node_id` |
| `fork` | folded · whole | The operator asked to branch a new node from an existing one. | `from_node_id` | `attempt`, `generation` |
| `fork_done` | folded | The fulfillment receipt for one `fork` request, indexed into the request queue. | `from_node_id`, `generation`, `idx` | `skipped` |
| `fork_unfulfilled` | diagnostic | A `fork` request the engine could not serve — recorded instead of silently dropped. | `from_node_id`, `generation`, `idx` | — |
| `full_retrain_charged` | diagnostic | A repair that forced a full retrain, and the evaluation budget it spent. | `attempt`, `generation`, `node_id`, `spent` | — |
| `hint` | folded · whole | An operator hint pushed into the next proposals; `replace` swaps the standing one. | `text` | `replace`, `source` |
| `holdout_evaluated` | folded | The node's number on the agent-invisible holdout split, beside the search metric and their gap. | `gap`, `generation`, `metric`, `n_holdout`, `node_id`, `search_epoch` | `attempt`, `program_sha256`, `protocol` |
| `host_grading` | folded · whole | The host-side scorer's grade over the candidate's predictions. | `predictions`, `scorer` | `competition`, `n_hidden`, `n_labels`, `protocol` |
| `hypothesis_added` | folded | A research hypothesis on the board — operator-authored, or engine-written after a deep-research pass. | `source`, `statement` | `at_node`, `concept_tags`, `concepts`, `id`, `parent_belief_id` |
| `hypothesis_concepts` | folded | The concept ids one hypothesis was tagged with, against a named vocabulary. | `at_vocab`, `concepts`, `hyp_id`, `mode` | — |
| `hypothesis_merged` | folded | Alias hypotheses folded into a canonical one. | `aliases`, `at_node`, `canonical`, `statement` | — |
| `hypothesis_ranked` | folded · whole | The board's priority order over the open hypotheses, with confidence. | — | `attempt`, `generation`, `node_id`, `order` |
| `hypothesis_updated` | folded | One hypothesis's status moved. | `id` | `status` |
| `inject_done` | folded | The fulfillment receipt for one `inject_node` request. | `idx` | — |
| `inject_failed` | diagnostic | An `inject_node` request that could not be materialized, with the reason. | `error`, `idx`, `reason` | — |
| `inject_node` | folded · whole | An operator-authored node: its idea and code, or a branch of an existing (possibly foreign) node. | — | `code`, `deleted`, `files`, `forked_from`, `idea`, `origin`, `parent_generations`, `parent_id`, `parent_ids`, `source_node`, `source_run` |
| `lessons_distilled` | folded · whole | The lessons one distillation pass drew from this run's node pairs. | `at_node`, `count`, `lessons`, `pairs`, `trigger` | — |
| `lessons_reconciled` | diagnostic | A re-evaluation changed an outcome, so this run's lessons were re-derived. | `at_node`, `derivation`, `lessons`, `n_added`, `n_retired`, `pairs`, `reflect` | — |
| `lessons_refreshed` | folded · whole | The cross-run lesson store was re-read at a node, and whether it changed. | `at_node` | `changed`, `chars`, `error`, `skipped` |
| `lessons_store_unavailable` | diagnostic | The lesson store could not be read this cadence; the next one retries the same unread store. | `error`, `mode` | `count`, `phase` |
| `literature_retrieved` | folded | The papers one deep-research pass READ, beside the memo it produced. | `at_node`, `items` | `memo_id` |
| `llm_cost` | folded · whole | The finalization roll-up of the run's provider spend. | `calls`, `completion_tokens`, `cost`, `priced_calls`, `prompt_tokens`, `total_tokens` | `finalize_scope`, `finish_seq` |
| `llm_usage` | folded · whole | One sanitized provider-call delta, folded cumulatively into the run's durable ledger. | — | `priced_calls`, `usage_id` |
| `log_repaired` | diagnostic | The `looplab repair-log` receipt for a rewritten torn log: what was dropped, and where the backup is. | `backup`, `corrupt_line`, `dropped_lines`, `good_records`, `ts` | — |
| `memory_read` | diagnostic | One memory / cross-run / skill tool call: the rows it showed and the digest of the exact bytes the role saw. | `args`, `invocation_id`, `result_chars`, `result_sha256`, `rows`, `tool` | `source` |
| `node_abort` | folded | The operator aborted one node. | `node_id` | `attempt`, `generation`, `reason` |
| `node_build_delta` | diagnostic | A build byte-identical to another node's — the duplicate is surfaced, never refused. | `generation`, `identical_to`, `node_id`, `parent_ids`, `source_digest` | — |
| `node_building` | folded | A node id was reserved and its build started; `node_created` clears the marker. | `node_id`, `operator`, `parent_ids` | `attempt`, `card_build_generation`, `card_id`, `generation`, `speculative` |
| `node_concepts` | folded · whole | The concept ids one node was tagged with, by which mode, against a named vocabulary. | `at_vocab`, `concepts`, `generation`, `mode`, `node_id` | `at_pending`, `attempt` |
| `node_confirmed` | folded | A node's confirmation statistics over its seeds (mean, std). | `generation`, `mean`, `node_id`, `seeds`, `std` | `attempt` |
| `node_created` | folded | A node exists: its idea, the code and files the Developer wrote, and its parents. | `code`, `files`, `idea`, `node_id`, `operator`, `parent_ids` | `attempt`, `card_build_generation`, `deleted`, `eval_start_boundary`, `footprint_finalized`, `forked_from`, `generation`, `materialize_aborted_intent`, `model_arm`, `origin`, `parent_generations`, `research_origin`, `seed`, `speculative` |
| `node_eval_started` | folded | A node's evaluation was dispatched — the promise `node_created`'s eval-start boundary made. | `generation`, `node_id` | `attempt` |
| `node_evaluated` | folded | A node's terminal: its metric, the trials behind it, its secondary metrics and any trust violations. | `eval_seconds`, `extra_metrics`, `generation`, `metric`, `node_id`, `stdout_tail`, `trials`, `violations` | `attempt`, `extra_metrics_direction`, `extra_metrics_provenance`, `metric_provenance`, `resource_curve`, `self_metric`, `stderr_tail` |
| `node_failed` | folded | A node's other terminal: why the evaluation produced no number, and who said so. | — | `attempt`, `card_id`, `engine_reason`, `error`, `error_evidence`, `eval_seconds`, `failed_stage`, `finish_data`, `finish_report_planned`, `generation`, `never_evaluated`, `node_id`, `reason`, `reason_evidence`, `reason_evidence_resolved`, `reason_findings`, `reason_hypotheses`, `reason_override_refused`, `reason_source`, `reason_summary`, `scope`, `step`, `triage_action`, `triage_rationale` |
| `node_repaired` | folded | One repair round on a failing node: what it changed, on what evidence, and the verdict on the change. | `attempt`, `changed`, `deleted`, `error_in`, `files`, `generation`, `node_id`, `rationale`, `stages_passed`, `triage_action` | `attribution`, `budget_exhausted`, `code`, `edit_calls`, `engine_reason`, `error_evidence`, `eval_seconds`, `footprint_finalized`, `idea_footprint`, `param_overrides`, `reason`, `reason_evidence`, `reason_evidence_resolved`, `reason_findings`, `reason_hypotheses`, `reason_override_refused`, `reason_source`, `reason_summary`, `salvaged_metric`, `unmet`, `unparseable_repairs`, `verified` |
| `node_reset` | folded | The operator re-ran an existing node in place from a named stage. | `node_id` | `attempt`, `from_stage`, `generation` |
| `node_tombstoned` | folded | Nodes struck from selection without deleting their history. | `node_ids` | — |
| `node_value_estimated` | folded | How much a model thinks expanding one node's branch still has left, in [0, 1]. | `generation`, `node_id`, `value` | `attempt`, `rationale` |
| `node_verified` | folded | The selection verifier's score for one node, over a named evidence digest. | — | `attempt`, `evidence_digest`, `generation`, `node_id`, `score` |
| `novelty_graded` | folded · whole | The graded-novelty verdict on a proposal the flat gate would have rejected. | — | `grade`, `level`, `literature`, `rationale`, `recommendation`, `shared_concepts`, `stance` |
| `novelty_rejected` | folded · whole | A near-duplicate proposal the novelty gate nudged off, with the distance that decided it. | — | `action`, `distance`, `generation`, `kind`, `literature`, `node_id`, `nudged`, `original`, `reason`, `stance` |
| `pause` | folded | The run paused — by an operator, or by the engine with a stated reason. | — | `attempt`, `detail`, `generation`, `node_id`, `reason` |
| `phase_progress` | diagnostic | One build/eval phase started or finished — the live activity feed's row. | `phase`, `stage`, `status` | — |
| `plan` | folded · whole | The run's PLAN artifact: how `max_nodes` was cut into seed, search and endgame reserve. | — | `at_node`, `endgame_start`, `phases`, `reason`, `reserve` |
| `policy_decision` | folded | The search policy's pick among the legal actions, with the scores behind it. | `chosen`, `reason`, `scores` | — |
| `prior_injected` | diagnostic | A cross-run prior was put in front of a role at a node — the receipt the citation instrument reads. | — | `at_node`, `case`, `notes`, `operator`, `operator_scoped`, `phase`, `quarantined_useless`, `role`, `rows`, `source` |
| `promote` | folded · whole | The operator promoted one node to an alias (`champion` by default). | `node_id` | `alias`, `attempt`, `generation` |
| `proxy_scored` | folded | The pre-eval proxy's score for a candidate, or its abstention when the nearest neighbour is too far. | `abstained`, `generation`, `nearest`, `node_id`, `score`, `skipped` | `attempt` |
| `readmodel_skipped` | diagnostic | The SQLite read-model sidecar could not be updated. | `error` | — |
| `reflection_note` | diagnostic | The run-end distillation: the causal note, the lessons and the auto-skills it proposed. | `at_nodes`, `coverage_digest`, `fingerprint`, `finish_seq`, `lessons`, `n_lessons`, `n_skill_candidates`, `n_skills`, `n_skills_demoted`, `n_skills_promoted_earlier`, `note`, `prior_citations`, `skill_candidates`, `skills`, `skills_demoted`, `task_id` | — |
| `repair_critic_verdict` | diagnostic | The critic's judgement on one repair round, over the durable repairs it could see. | `after`, `attempt`, `durable_repairs`, `generation`, `judged`, `node_id`, `rationale`, `source`, `verdict` | — |
| `report_generated` | folded | A run report was written, at a node and for a stated trigger. | `at_node`, `content`, `trigger` | `finalize_scope`, `generation`, `refresh_id` |
| `report_refresh_failed` | diagnostic | A paid report refresh failed before anything was written — sanitized, retry-safe. | — | `error_kind`, `generation`, `refresh_id` |
| `report_refresh_started` | diagnostic | The durable idempotency claim for one paid report refresh. | — | `generation`, `refresh_id` |
| `research_attempted` | folded | A deep-research pass was PAID for — the receipt its memo closes by `attempt_id`. | `at_node`, `attempt_id`, `manual`, `trigger` | — |
| `research_completed` | folded | One deep-research memo: its claims with evidence bindings, plan, literature and verifier verdicts. | `at_node`, `memo`, `served_manual`, `trigger` | `attempt_id`, `converged_skips`, `memo_id` |
| `restart` | folded | The operator handed a paused run to a replacement owner. | — | — |
| `resume` | folded | The operator resumed a paused run. | — | — |
| `resume_requested` | folded | A durable resume intent, appended before the engine is spawned. | `mode` | `launch_claim`, `request_seq` |
| `resume_served` | folded | The replacement owner acquired the singleton lock and served the resume. | — | `activity_recovery`, `engine_owner_boundary` |
| `reward_hack_suspected` | folded | The reward-hack scan's signals about one node's code, over a named code digest. | `code_digest`, `evidence_version`, `generation`, `node_id`, `signals` | `attempt` |
| `run_abort` | folded | The run was aborted, with the reason. | `reason` | — |
| `run_concepts` | folded | The run's BASE concept set, which nodes inherit. | `concepts` | — |
| `run_finished` | folded | The run ended: the reason, the log position it ended at, and its final spend. | — | `after_seq`, `calls`, `completion_tokens`, `cost`, `error`, `finalization_required`, `finalize_scope`, `priced_calls`, `prompt_tokens`, `reason`, `total_tokens` |
| `run_loop_exited` | diagnostic | Why the engine's outer loop exited (one of `RUN_EXIT_REASONS`). | `reason` | — |
| `run_reopened` | folded | A finished run was reopened for more work. | — | — |
| `run_setup_finished` | folded | The task's setup command finished: exit code, environment delta, stderr tail. | `command`, `dropped_requirements`, `env_delta`, `exit_code`, `stderr_tail`, `timed_out` | — |
| `run_setup_started` | folded | The task's setup command started, in a named working directory. | `after_interrupted_attempt`, `command`, `cwd` | — |
| `run_started` | folded | The run's launch record: task, goal, direction, and the settings pinned at launch (invariant #6). | — | `card_driven_selection`, `config_hash`, `direction`, `dirty_inputs`, `env`, `eval_env`, `eval_env_absent_from_task`, `eval_parallel`, `goal`, `holdout_fraction`, `holdout_select`, `llm_parallel`, `require_approval`, `run_id`, `run_uid`, `select_verifier`, `select_verifier_contract`, `select_verifier_samples`, `speculation_calibration_gpu_inventory`, `speculation_calibration_profile_digest`, `speculation_calibration_seed`, `speculation_depth`, `speculation_depth_auto`, `speculation_gate_receipt_digest`, `speculation_implementation_digest`, `speculation_policy_scope`, `speculation_runtime_scope_sha256`, `task_id`, `trust_gate`, `verifier_ci_tie`, `workspace` |
| `run_width_settled` | folded | The run's live width was re-pinned, with the evidence behind the new value. | — | `evidence`, `finish_data`, `finish_report_planned`, `previous`, `reason`, `scope`, `step` |
| `rung_promoted` | folded | The successive-halving rung that promoted a named set of survivors. | — | `finish_data`, `finish_report_planned`, `rung`, `scope`, `step`, `survivors` |
| `score_metrics_backfilled` | folded | Secondary metrics read back off the score stage's own artifact after the fact. | `extra_metrics`, `generation`, `node_id`, `precision_decimals`, `read_at`, `unrecoverable` | — |
| `set_strategy` | folded | The operator set the search strategy. | `strategy` | — |
| `setup_finished` | folded | Workspace setup finished, with the manifest it produced. | `manifest`, `seconds` | — |
| `setup_started` | diagnostic | Workspace setup started, for a goal and a repo. | `goal`, `phase`, `repo` | — |
| `setup_step` | diagnostic | One workspace-setup step. | — | `sources`, `step` |
| `skills_promoted` | diagnostic | The mid-run per-card skill promotion: which settled cards it judged, and what it wrote. | `at_node`, `cards`, `count`, `promoted`, `skill_candidates`, `skills`, `trigger` | — |
| `spec_approval_requested` | folded | The proposed evaluation spec is waiting for a human. | `eval` | — |
| `spec_approved` | folded | The evaluation spec was ratified; the optimization loop trusts it from here. | — | — |
| `spec_drift` | folded · whole | One evaluation's spec drifted from the ratified one. | — | `attempt`, `generation`, `node_id`, `seed` |
| `spec_proposed` | folded · whole | The onboarding agent's proposed evaluation spec and metric adapter. | — | `eval`, `metric` |
| `speculation_depth_settled` | folded | The speculation depth this run settled on, and the evidence behind it. | `reason` | `depth`, `error`, `eval_seconds`, `evidence`, `generation`, `node_id`, `previous` |
| `stage_finished` | folded | One stage of a multi-stage eval pipeline finished: name, status, exit code, seconds. | — | `attempt`, `exit_code`, `generation`, `name`, `node_id`, `seconds`, `status` |
| `stage_rollback` | diagnostic | A failed stage's rollback to a checkpoint — accepted, or refused with a reason. | `accepted`, `attempt`, `failed_stage`, `generation`, `node_id`, `refusal`, `stage` | — |
| `strategy_decision` | folded | The Strategist's consult: the strategy it returned and the context it was given. | `at_node`, `ctx`, `strategy` | `developer_application`, `width_unfilled` |
| `trace_export_health` | diagnostic | The span exporter is unhealthy — one row per distinct state, never on a healthy run. | — | `accepted_spans`, `buffered_bytes`, `buffered_spans`, `dropped_spans`, `export_failures`, `exported_spans`, `loss_receipt_failures`, `queued_spans`, `shutdown`, `worker_alive`, `worker_stop_reason` |
| `train_monitor_alert` | diagnostic | The live training-log judge's verdict about one running stage, and the log role it judged. | `confidence`, `generation`, `log_role`, `node_id`, `reason`, `status` | `citation_resolved`, `confidence_valid`, `evidence_locator`, `evidence_source`, `fault`, `kill`, `kill_role_withheld`, `kill_superseded_by`, `repair_decided`, `stage`, `stop_decided`, `trajectory`, `trajectory_veto` |
| `trust_gate_changed` | folded | The run's trust gate was changed, by a named source (last write wins). | `source`, `trust_gate` | — |
| `trust_scan` | diagnostic | Which trust detectors ran over one node's code, how many findings they made, over what digest. | — | `code_digest`, `detectors`, `evidence_version`, `findings`, `generation`, `node_id` |
| `verifier_group_scored` | folded | One verifier round over a GROUP of nodes, keyed on the contract and evidence digests. | `contract`, `members`, `requested_samples`, `v` | — |
| `workspace_changed` | folded | The workspace directory differs from the one the run started in. | `now`, `was` | — |
| `workspace_seeded` | diagnostic | One node's workspace was seeded with materialized inputs. | `materialized`, `node_id` | — |

<!-- /generated -->
