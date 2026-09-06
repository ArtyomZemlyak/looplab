# LoopLab — guide for coding agents

LoopLab is an autonomous ML/DS research engine: a Researcher proposes ideas, a Developer writes
code, a sandbox runs it, an evaluator scores it, and the loop refines/merges the best candidates.
The append-only event log (`events.jsonl`) is authoritative for replayable `RunState`; task/config
snapshots, traces, chat and cross-run stores are explicit sidecars and are not rebuilt by replay.
Design docs live in `docs/` (see `docs/02-architecture.md`, ADRs in
`docs/03-decisions.md`).

## Commands

```bash
pip install -e ".[dev,ui]"        # dev deps; [ui] needed for server/assistant/TUI tests (fastapi)
python -m pytest                  # full suite (8,900+ collected tests, a few minutes; addopts already has -q)
python -m pytest tests/test_events_replay.py           # targeted run — always do this first
python -m pytest -o addopts="" -q ...                  # if you need to override the default -q
python -m pytest -m "not docker"  # skip Docker-daemon tests
looplab run --no-genesis --kind quadratic --goal "min (x-3)^2" --direction min --backend toy --out runs/demo  # offline smoke
# (--no-genesis matters: any --goal otherwise invokes Genesis, which needs a reachable LLM.
#  --backend toy is now REQUIRED for an offline run: the `backend` default was changed from
#  "toy" to "llm" on 2026-08-04, so without it this command hits the LLM endpoint preflight.)
looplab replay runs/demo          # rebuild state from the event log (reproducibility check)
looplab timings runs/demo         # wall-clock: per node + run-level (from spans.jsonl), reconciled
                                  # against the run's duration (events.jsonl first->last ts), residual named
# Tool-call repeats are measurable since 2026-08-28: each `tool` span carries
# `repeat_streak` (every call) and `repeat_note_sent` (only when the identical-result
# nudge fired). Before that the nudge went into the model message alone, so its firing
# rate — and whether it changes behaviour — could not be read off any run.
looplab tokens runs/demo          # TOKENS by phase (from spans.jsonl), reconciled against the DURABLE
                                  # llm_usage ledger; residual SIGNED and printed. The build
                                  # (plan+stages+card_build) measured 61-63% of two real runs.
                                  # Also a per-CARD roll-up on card-driven runs, flagging cards
                                  # whose every node was discarded before dispatch: 21.0% of
                                  # e5small-dr-unified-v9 bought builds never evaluated.
looplab ui                        # FastAPI server + React UI (see looplab/serve/)
python -m ruff check looplab      # the blind-except census (BLE001 only; every hit needs a stated reason)
```

The suite runs fully offline in ~1-2 minutes; live-LLM tests auto-skip (opt in with
`LOOPLAB_LIVE_SCENARIOS=1`). There is no formatter and ONE lint rule, and that rule is a CENSUS, not a
style: `[tool.ruff]` selects `BLE` only (doc 52 row 14), so `python -m ruff check looplab` lists every
blind `except Exception`/`BaseException`/bare `except` that carries no `# noqa: BLE001 — <why this is
safe to contain>`. Containment is the house posture (670 such handlers), so the rule is not "do not
write one" but "say why"; `tests/test_containment_census.py` re-derives the same census by AST with no
`ruff` installed, refuses a NEW blind handler that states no reason, and keeps the 103 pre-existing
reason-less sites as a shrink-only backlog in `tests/data/containment_unreviewed.txt` (review one =
write its reason, delete its row). Match the style of surrounding code (~100-col lines, heavy
why-comments) and do not reformat.
Docs are built with `mkdocs build --strict` in CI — broken doc links fail the deploy.
`looplab build-ui` builds the React UI (`npm ci && npm run build` in `ui/`); `looplab ui`
auto-builds when the dist is missing. `looplab/cli/` is a PACKAGE (command groups in
`run_cmds`/`export_cmds`/`inspect_cmds`/`concept_cmds`/`governance_cmds`/`memory_cmds`/`audit_cmds`/`ui_cmds` —
`inspect_cmds` is run diagnostics ONLY, the Part IV concept/novelty diagnostics are `concept_cmds`,
and everything that spends money on a steward or authors cross-run memory CONTENT is
`governance_cmds`; `audit_cmds` (2026-09-06, doc 52 row 22) is the post-run INSTRUMENT group — a paid judge over ONE finished run that writes that run's sidecar and moves nothing (`mlebench-extras`; `bait-materialize` / `bait-audit`, the BAITBENCH-shaped hack-rate instrument over `judgebench/bait.py`, whose box measurement is still owed). `memory_cmds` is the other deliberate exception and it is a DOMAIN split, not a
drift: `memory-orphans` writes the shared stores too, but only ever by REMOVING rows whose run no
longer exists — a maintenance sweep with no model call and no new claim — and it lives apart
because `governance_cmds` was 11 lines under its file ceiling when the command arrived — and `prior-citations` (2026-09-06, doc 52 row 17) joined it as the READ side of the same stores, the citation instrument over `prior_injected` + `memory_read` (`events/prior_citations.py`), writing nothing and calling no model; the Typer app +
patchable builders (including the shared `_make_llm_client`/`_settings_for_run`) live in
its `__init__`; `python -m looplab.cli` works via `__main__.py`).

**Keep the docs and the process diagram in sync with the code — in the SAME change.** When you
change a default, a cadence/threshold, an event type, or add/rename a subsystem, update: (1) the
settings table in `docs/guide/configuration.md` (every `Settings` field must have a row with the
CORRECT default) and the relevant `docs/guide/*.md` page; (2) the **full process diagram**
`docs/infographic/agent-architecture.html` — a self-contained boxes-and-arrows flowchart whose
numbers/cadences/thresholds are verified against `looplab/` (embedded on `docs/guide/architecture.md`).
Stale docs/diagram are treated as a bug. The diagram is data-driven (a `B` block map + `E` edge list
in its inline `<script>`); edit the data, not hand-placed SVG.

## Package map (what lives where)

The one-line rule per module lives here; the MEASUREMENTS behind each rule (corpus counts, the
runs that motivated a design, the alternatives refused) live in
[`docs/53-agent-guide-narratives-2026-09-06.md`](docs/53-agent-guide-narratives-2026-09-06.md),
one section per row below, and in the numbered docs and module docstrings they were written from.
This file is on a byte budget (`tests/test_documentation_contracts.py::CLAUDE_MD_MAX_BYTES`): add a
rule here, put its story in doc 53 or the module docstring, never both.

| Path | Contents |
|---|---|
| `looplab/core/` | Foundation; imports nothing above itself. `models.py` (`Idea`/`Node`/`RunState`; card IDENTITY in `cards.py`, re-exported so both spellings are the same objects), `config.py` (`Settings` schema; `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` pins a pre-field snapshot to the historical behaviour) + `appconfig.py` (loader), `llm.py` + `llm_streaming`/`llm_toolcall`/`llm_transient` (every split name re-exported through `llm.py`; `model_override` is the ContextVar every request site reads, so a routed build changes only the MODEL asked of the same endpoint), `llm_broker.py` (run-local concurrency ADMISSION by lane, borrowed around the real request only; `BACKGROUND_LANE_PRODUCERS` is the registry a capped lane's producers must be in), `llm_budget.py` (`RunBudget`: the run's spend as a RESERVE-COMMIT budget the broker meters at `borrow()`; `llm_cost_limit`/`llm_token_limit` 0 = off), `containment.py` (`contain(reason, exc)`: the countable contain-and-continue — stamps the span, counts, logs, and RE-RAISES `BudgetExceeded`; every blind handler around a paid call in the run path re-raises `BudgetExceeded` first, pinned by `tests/test_containment_census.py`), `evidence.py` (the ONE untrusted-evidence envelope: `EVIDENCE_LABEL`, `untrusted_evidence_guard`, the idempotent `fence_untrusted`/`is_fenced`, `envelope_enabled`; every consumer defaults it OFF at its constructor because a prompt is a contract), `pathsafe.py`/`atomicio.py` (the single spelling of reparse/reserved-name/file-identity rules; `durable_no_replace_rename`), `envsafe.py` (`is_secret_env` + the DECLARED ENVIRONMENT rule `validate_env_map`/`merge_env` shared by the run, task and stage levels — a secret-shaped declaration is REFUSED, never redacted), `redact.py` (`redact_persisted_text`, `redact_env_values`; `Settings.redact_output` gates only the ENTROPY pass — known shapes and this box's env values are always masked; `engine/audit.py::Engine._redact` is the one funnel every persisted tail goes through), `research_record.py` (the durable research record's pure builders: exact-span `evidence_item`, `parse_literature`, the deterministic `bind_claims_to_evidence`), `phase_events.py` (the inner-agent phase moments and the `memory_read` record as DIAGNOSTIC events through an engine-installed ContextVar sink; a no-op outside a run), `jsonutil.py` (`canonical_json(_digest)`, `valid_digest_ref` — the ONE reader of the digest shape; never re-derive it), `setup_identity.py` (the two run-start digests), `numeric.py` (`numeric_params`/`knn_idw`), `param_carriers.py` (what number a committed configuration DOCUMENT assigns a dotted path — YAML/JSON composed trees beside Python's `ast`; a document declaration naming two leaves is REFUSED), `jsonlio.py` (generic JSONL store I/O, re-exported by `events/eventstore.py`), `trace_files.py`/`trace_append.py` (the descriptor-first trace-sidecar boundary and the append receipt), `run_identity.py` (`run_ref` for GROUPING vs `row_belongs_to_run` for cascade ATTRIBUTION — deliberately not one rule), `memory_window.py` (the ONE bounded-tail snapshot rule for the cross-run JSONL stores; its index is window-relative), `claimpin.py` (`CLAIM[...] decided:` evaluation and `citation_defects()`), `profile.py` (the pure data profiler / leakage front-end). |
| `looplab/events/` | The event store and `types.py`, the event-type registry — add every new type there (`tests/test_event_types.py` partitions each into a fold handler or `DIAGNOSTIC_EVENTS`); the writer seams `BACKGROUND_APPENDABLE` / `SETUP_THREAD_APPENDABLE` / `ASSISTANT_APPENDABLE` / `NON_CARD_SELECTION_BACKGROUND_APPENDABLE` (see invariant 1); `replay.py::fold` (event log → `RunState`; the `_HANDLERS` table); `card_ledger.py` (the derived Card ledger, a LEAF that imports only `core`); `span_index.py` (the light span index; its `_anchored` is the ONE `?before=` seek rule and an unplaceable anchor is REFUSED at the boundary); the pure UI projections `traceview.py` (incl. `claimed_build_traces`, the one reading of a node's claim on the run-scoped trace that BUILT it), `htmlview.py`, `comment_projection.py`, `belief_projection.py`, `authoring_projection.py`; `trust_gate.py::apply_trust_gate` (the ONE write policy for `trust_gate_changed`, shared by the config PUT and the assistant tool — it returns an outcome and phrases no refusal); `notebook.py` (an export); `finalize_scope.py` / `finalize_protocol.py` (the finalization step vocabulary and `QUIET_FINALIZATION_SUFFIX`, which `engine/finalize.py` writes and `search/speculation_quality.py` checks — never hand-sync a copy); `prior_citations.py` (the prior citation-rate instrument over `prior_injected` + `memory_read`, `looplab prior-citations`); digest, readmodel, exporters. |
| `looplab/runtime/` | Process execution ONLY; imports nothing above `core`. `sandbox.py` (subprocess/Docker tiers; `eval_deadline_env` is the ONE derivation of `LOOPLAB_EVAL_DEADLINE` + `LOOPLAB_EVAL_TIMEOUT_S`, set by `run_argv` via `setdefault` so a declaration wins), `command_eval.py` (staged command evaluation; `validate_stages` is the single definition of a valid stage manifest — `role: "training"` on at most one stage is what grants a live-watchdog kill authority; `expect.numeric` is the model-free contract (`runtime/numeric_contract.py`: a declared relation held to the last value the stage printed, fails closed, never salvaged); `epoch_floor_acquits` is the deterministic floor under `declared_condition_violated` and may only ever ACQUIT; `READER_PATH_KEYS` registers which spec keys become filesystem paths; `HOST_STAGE_KEY` is engine-stamped, never declarable), `read_fence.py` (the source-tree READ FENCE: a per-run `sitecustomize` audit hook that REFUSES `open` under an editable source root with a non-`OSError` exception on purpose, watches `os.chdir` + the `MUTATION_EVENTS` family, and refuses WRITES under the run dir outside the launch's own workdir; a PATH fence, not an inode one, for measured cost reasons), `landlock.py` (the kernel twin: an opt-in READ allow-list `Settings.landlock`, and `NO_MUTATION_HANDLED`, always on for the Developer probe — both refuse with `EACCES`, so both sit BESIDE the hook, never instead of it), `read_allowlist.py` (the ONE derivation of what an eval may read, from the operator's declarations), `applied_params.py` (the metric's COORDINATES: what the configuration that actually ran said the declared `Idea.params` were worth — `committed` carriers vs the `resolved` config elected by `eval.metric.applied_config_glob`; SURFACES, never refuses; a conflicted coordinate rides in `conflicts` and raises the champion caveat), `metric_subject.py` (the metric's REFERENT, bound at the score stage's start; `subject_glob` binds ONLY on a unique match), `stage_identity.py` (`stage_input_key` + `stage_outputs`, recorded on every `stage_finished` row and read by nothing that decides — the cross-node reuse cache was measured a corruption primitive and REFUSED; `looplab stage-dups` is the instrument), `deps.py` (text may NOMINATE a distribution, `is_present` DECIDES by probing the eval interpreter). |
| `looplab/tools/` | Agent-facing tools. `_base.py` is the ToolProvider contract and the shared bounded-output rules (`clip`, `fit_rows`, `stream_tails`, `RESULT_CAP`): every agent-facing reader states the range it covered and the call that continues past it, and that call must be one the caller has NOT already spent. `env_inspect.py` (read-only env inspector) and `dev_probe.py` (`run_probe(code)`: a Python program against the REAL environment inside a disposable replica — no read of the editable source, no write anywhere (audit hook + Landlock `NO_MUTATION_HANDLED` + `RLIMIT_FSIZE 0`), no new program, no GPU; the interpreter IS the surface because it is what the fence can cover). `log_tools.py` (`read_log` / `metric_series`: the live-eval judges LOOK at a named stage log instead of a slice; a search SWEEPS to the end of the log with an exact total; four roles use it through `train_monitor.monitor_log_tools` / `repair_log_tools` / `stage_check_tools` / `diagnosis_tools`). `skills.py` (bounded, addressable, tiered: `render_skill_body` keeps whole sections under `SKILL_RESULT_CAP` and names what it left out beside the `use_skill(name, section)` call; `skill_tier` = declared `tier:` else hand-written `global` / promoted auto `domain` / other auto `task`; `parse_skill_fingerprints` is the ONE parser of that frontmatter field, shared with `engine/memory.py`'s writer). `memory_tools.py`, `cross_run_tools.py` and `skills.py` each report a `memory_read` record through `core/phase_events.py::emit_memory_read`. `clock.py` (`remaining_time`: the agent's own clock, published by `drive_tool_loop` before every tool execution). `run_tools.py::ForeignRunReader` carries the EVALUATION-CONTRACT receipt beside a foreign run's number (`engine/eval_contract.py`) and annotates, never withholds; `_research_memo` is addressable by `section=`. `vectorstore.py` + `memora.py` (embeddings + harmonic index); `reposcout.py`, `repo_write_tools.py` (the manifest-collision rule: a legitimate INPUT is never a declared OUTPUT). |
| `looplab/agents/` | LLM personas. `roles.py` (the plain roles; `_state_brief` is the PUSH channel of the deep-research signal, prefixed by the memo's own claim tally under `Settings.memo_verdict_cue`; the registries `DEVELOPER_OUTPUT_ATTRS` / `RESEARCHER_ACTION_ATTRS` / `RESEARCHER_HINT_ATTRS` / `LLM_PRESENCE_ATTRS` / `FACADE_STAGE_ATTRS`; `DeveloperResult` is the envelope every build reads its outputs off, captured under the instance's lock, never off the shared instance afterwards; `ValidatingDeveloper` forwards `implement_from(..., co_parents=)` only to an inner that accepts it), `providers.py` (the providers every agentic role shares — keeps every `search`/`tools`/`agents` import function-local), `tool_loop.py::drive_tool_loop` / `agentic_*` (the shared loop: history compaction, stuck detection, the `LoopOptions` bundle vs `EXPLICIT_ONLY_LOOP_ARGS` partition — a keyword reachable both ways silently degrades the Researcher to non-agentic; it publishes the `LoopClock`, reports the three `agent_phase_*` moments through `core/phase_events.py`, and fences every tool result when the envelope is on), `agent.py` (`run_phase` + re-exports; `looplab.agents.agent.drive_tool_loop` is THE documented monkeypatch seam), `deep_research.py` (the memo, its plan/evidence/literature captured on the memo FROM THE FIRST TURN so a junk emit keeps the record), `unified_agent.py` (the facade; `_TRIAGE_EVIDENCE_GUARD` / `_REPAIR_CRITIC_EVIDENCE_GUARD`; the emit caps are decided per consumer, not inherited), `strategist.py` (`RuleStrategist` + the LLM/agent variants; `stall_rung` is the stall's identity; `validate_strategy` is the paranoid whitelist; `Strategy.operators.endgame_sweep` is the plan reserve's sweep switch), `cli_agent.py`, `preflight.py` (an unreachable endpoint REFUSES the run before any role is built). |
| `looplab/search/` | Search policies (`policy.py`: action kinds/meta keys, `GreedyTree`, `legal_actions`, `operator_yields` + `_bandit_pick`, and the operator × MODEL router — `parse_model_arms`, `model_arm_yields`, `_model_arm_pick` with the gain divided by the arm's relative cost, stamped as `META_MODEL`; inert without `operator_bandit` or a declared arm), `operators.py` (`merge_idea` does ARITHMETIC on `idea.params`), best-of-N, `surrogate.py` (k-NN BO-lite; `acq = pred ± explore × nearest`), `panel.py` (K LLM ideas ranked by that same ACQUISITION — the CLI wires `Settings.surrogate_explore`; a bare `PanelResearcher` keeps the point estimate), `proxy.py` (the pre-eval kill; `should_skip` ABSTAINS when the candidate's nearest neighbour is beyond `support_radius`), `archive`, `foresight.py`, `hybrid_merge.py`, `card_selection.py` (`card_next_actions`, `_speculative_selection`; `_asha_mask_is_unsound` is consulted at the ONE lane that hands the policy a masked view), `coverage.py`, `lock_in.py`, `speculation_quality.py` (the paired-run calibration BENCHMARK — an ORDERED pipeline whose order IS the receipt; a changed derivation revokes every issued receipt), and the PART IV/V concept cluster in strict layer order `concept_graph.py` ← `concept_tagging.py` / `concept_lens.py` (`run_constant_split`, `project_concept_map`) ← `concept_analytics.py` ← `concept_map.py` — no re-export façade, and a cluster module reaches a sibling's functions through the module object so monkeypatch seams survive. `search` may import `agents` at module level; `agents` reaches `search` only through a deferred import. |
| `looplab/trust/` | Gates that keep results honest, in three tiers. DETERMINISTIC (pure Python, no model): `leakage.py`, `reward_hack.py`, `cv.py`, `gate.py` (the >1-SE rule — plus `multi_test` since 2026-09-06 (doc 52 row 22): repeated evaluation on the TEST split followed by selection on those scores, AST-based, both halves required), `confirm.py`. MODEL-BACKED and advisory: `critic.py`, `verifier.py`, `memo_verify.py` (D8: a synthesis claim and its check are decoupled; `finalize_verified_evidence` consults `unreliable_metric_ids` and refuses with a distinct reason), `lesson_guard.py`, `judge.py` (the ONE structured-judge invocation both verifiers share; `structured_judge(tools=…)`). Plus `findings.py` (the single finding shape), `harden.py` (hacker-fixer-solver), `cross_run.py` (bounded, always-redacted projections; `cross_run_text`). Whether any flag CHANGES selection is `Settings.trust_gate`, not this package (`audit`, the default, only surfaces). Redaction is NOT here — `core/redact.py` owns it. |
| `looplab/engine/` | **The orchestrator loop** + cross-run memory; see the invariants below. `Engine` spans `orchestrator.py` (the `run` spine, node creation, the module-global `fold` seam every helper that folds must reach through — a direct import in another file is a SILENT narrowing of every existing monkeypatch; the loop-exit signal vocabulary is CLOSED) and twenty-one mixins: `shared.py` (members every cluster uses and none owns: `_agent_may`, `_op_span`, `_cadence_due`, the two eval-timeout rules), `node_build.py` (`_implement_result` with `co_parents`, `_emit_node_created` — the single emitter, optional keys LEFT OUT not None-filled), `card_reservation.py` (the native-Card reservation ledger; every fold through `_fold` → `orchestrator.fold`), `speculation.py` (the producer lanes; `CARD_BUILD_SKIP_REASONS`; `_proposal_authority_seq` fences a paid proposal on seq EQUALITY and excludes `DIAGNOSTIC_EVENTS` wholesale), `speculation_gate.py` (the calibrated speculation ENVELOPE as statable free functions; `engine_authored_artifacts` is the ONLY asserter of the `engine` extra-metric channel), `evaluate.py` (`_evaluate` is a DRIVER over `EvalAttempt` — the slots record of one lifecycle's evaluation — and nine `_eval_*` phases in one order: admit / prepare_workdir / seed_ledgers, then per attempt run_attempt -> settle_outcome -> salvage -> decide_repair -> apply_repair, and write_terminal; a phase reports the loop control it cannot execute as a CLOSED `PHASE_*` signal (`tests/test_eval_attempt_phases.py`), a guard over "the attempt loop" reads `tests/_source_scan.py::eval_attempt_source()` (the phases in driver order) or names its phase; all eight node terminals inside `_write_lock`; the proxy kill reads the k-NN pair), `eval_dispatch.py`, `eval_stages.py` (`_resolve_stages` appends the host-side `score` stage; `manifest_prefix_unchanged` is the per-ENTRY reuse clause), `crash_repair.py` + `triage.py` (`TRIAGE_ACTIONS`; `TRIAGE_RATIONALE_CAP` is the intake bound), `failure_diagnosis.py` (WHO may say what a failed eval failed of: `ENGINE_FINAL_REASONS` vs `DIAGNOSED_FAILURE_REASONS`, `unclassified`; the diagnostician IS the triage call and its verdict RECORDS evidence, never refuses on it), `repair_verify.py` (`REPAIR_VERDICTS`: `inert` decided on BYTES is the only verdict the loop acts on; `verified` means "the repair's vocabulary appears in what it changed", never "it did what it said"; `declared_param_overrides`), `metric_salvage.py` (deterministic rungs only; a salvaged node carries `metric_salvaged` and is out of `feasible_nodes` unless `metric_salvage_repair` re-measured it), `champion_caveats.py` (the caveats that SURVIVE selection, incl. `params_overridden`; `mislead_gap` is the Protocol Validity pair on the run row — champion beside the best un-flagged, un-salvaged, feasible node and their gap, from the same two predicates), `train_monitor.py` (the live-log judge: `LossTrajectoryTracker` is a VETO that may only refuse; `should_monitor_repair` stops a stage with `not_learning`, in `FAILURE_REASONS`, so the inline repair loop picks it up; kill authority needs the declared `training` role and is SPENT once the stage's artifact exists; `stamp_projected_overrun` writes a row on a healthy verdict when the clock says the stage cannot finish), `asha_monitor.py`, `research_cadence.py` (`_record_deep_research` appends the memo and `literature_retrieved`), `strategy.py` (consult due on cadence AND on a plateau via `cadence.py::plateau_due`; `_apply_strategy` gates every knob on the governance matrix), `concept_cadence.py`, `verifier_tiebreak.py` (SELECTION machinery), `confirm_phase.py`, `ablation.py`, `novelty.py` (`_capture_proposal_events`: the offloaded proposal buffers its folded intents for the MAIN task), `audit.py` (`_redact` funnel, `_append_phase_event` sink), `resources.py` (the host GPU-pool lease), `proposal_cues.py`, `plan.py` (the PLAN artifact: `build_plan` cuts `max_nodes` into seed/search/endgame from `endgame_reserve_frac`, `endgame_actions` is the ONE gate `_plan_gate` applies to every selected action set — top-2 ensemble once, then champion sweeps proposed by the k-NN surrogate — `replan` re-cuts on a budget change or a hard stall), `cadence.py` (`cadence_due` = the since-last node-count gate, `occupancy_due` = the eval-occupancy pace, `at_creation_boundary` = the precondition every node-count consumer shares; a third pace must record no `at_node`), `widths.py` (the live width settling rule and `per_experiment_gpu_budget`), `costs.py` (the durable `llm_usage`/`llm_cost` ledger; `seed_run_budget`), `finalize.py`, `holdout.py`, `eval_contract.py` (may this run's number be read on that one's scale — tri-state, `None` never a guess), `memory.py` + `lesson_hygiene.py` (`lesson_id`, `lesson_utility`, `filter_useless` beside `filter_contradicted`) + `lessons_priors.py` (`_pick_role_prior` returns the text AND the receipt `record_prior_injection` writes) + `lessons_distill.py` (auto-skills: `next_auto_skill_status` is the support edge, `reconcile_auto_skill_statuses` the contradiction edge; the `lesson_utility.jsonl` ledger) + `lessons_reconcile.py` + `concept_capsules.py` + `concept_shelf.py`, `curation_protocol.py` vs `steward_invocation.py` (two paid-curation writers, deliberately not one), `governance_health.py` (`_validate_curation_row` stays TOTAL over every row generation). In a mixin `self` IS the Engine — grep the engine package before renaming an engine attribute. `attribute_sites.py` is the declaring-site registry behind `tests/test_engine_attribute_sites.py` (doc 52 row 21): a read of an attribute nothing assigns is a red test, not a default. `bundle.py` is the RO-Crate reviewer bundle (`looplab export-bundle`: the run's own record copied and digested, nothing re-derived but the summary row; here and not in `events/` because it composes the fold with the engine's derivations). |
| `looplab/adapters/` | Task types (toy → dataset → repo → MLE-bench); the TaskAdapter contract is documented in `adapters/tasks.py` (`TASK_OPTIONAL_HOOKS` is the registry). `perception.py` (the ONE home of the bounded on-disk data readers shared by `dataset_task.py` and `repo_task.py::columns`/`data_samples`: the primary table per declared `data:` mount, 200 rows, ≤4 tables, ≤64 columns, never executing anything). `repo_task.py` (`DataSpec` per-source permissions — only `mount`/`edit` are enforced; `HostScorerSpec`, the host-side `score` stage; `eval_source_tree_command_paths` is a submit-time WARNING). `repo_developer.py` (the repo Developer's four phases; `co_parent_block` renders an ensemble's other parents — traces and the files that differ; `_scout_tools` is the one point all phases compose; `_time_budget_note`). `repo_write_tools.py` (the manifest-collision rule). `mlebench_real.py` + `mlebench_split.py` (the search is scored on an agent-invisible split; `holdout_fraction=0` is the explicit legacy protocol). `mlebench_extras.py` (the two official MLE-bench extras as POST-RUN instruments: the rule-violation judge against `MLEBENCH_RULES` + the task's own description, and the Dolos plagiarism pass; a record in `mlebench_extras.json`, never a selection — `looplab mlebench-extras`). `mlebench_campaign.py` (the ≥3-seed mean ± SEM table off each run's own record, the Mislead-adjusted column BESIDE the raw one; `mlebench_grade.py::percentile_rank` is the leaderboard percentile every official report now carries). |
| `looplab/serve/` | FastAPI server; `server.py` is a thin composition root, routes live in `serve/routers/*`. `http.py` (`REFUSALS`: every route answers an unreadable input with a coded 503, never a 500 or the `OSError` text; no GET body takes the exclusive sequencer except `start_status`). `run_commands.py` (the durable command lifecycle) with `control_validation.py` split out (`normalize_control` + `ControlSpec` rows joined from FIVE tables asserted equal to `protocol.py::CONTROL_EVENTS` at import; the direction is one-way). `router_wiring.py` (`LATE_BOUND_ROUTER_CALLABLES`: no router imports a router; `mount_routers` refuses a live consumer without its producer). `durable_op.py` (the RECEIPT tier; `refuse_unless_quiescent` shares the probe set, never the words), `paid_work.py` + `paid_ledger.py` (the paid-work claim→terminal ledger; `conflict_policy` is an explicit spec field; `strict_fsync` may not be re-bound by a router), `memory_cascade.py` (what a run's deletion may remove from the shared stores, keyed on `run_uid`; `orphan_survey` for out-of-band removals), `service_reaper.py`, `assistant_watch.py` (the durable standing watch; the TRIGGER is evaluated by the server, a wake-up runs at the mode pinned at arming), `launch.py::validate_launch` (`POST /api/validate`, the same funnel `/api/start` refuses through), `owner_token.py` (an unset `LOOPLAB_UI_TOKEN` on the shared hub origin FAILS CLOSED with a minted token), `settings_ui_schema.py` (the curated catalogue; every `Settings` field is a row or a written-down omission), `run_projections.py`, `scope_report_store.py` / `scope_actions.py`, `jupyter.py`; never imported by the engine. |
| `ui/` | React control plane (built artifacts served by `serve/server.py`). The house pattern: a **pure model beside its React half** — transitions in a plain ES module `node --test` can drive, the hook/component keeping only the choreography. Pairs: `resourceModel.js`/`useScopedResource.js`, `runCommandMachine.js`/`useCommandStatusPoll`, `narration.js`/`Dock.jsx`, `stageAttribution.js`/`Inspector.jsx::StagePipeline` (a superseded stage row draws `STAGE_SUPERSEDED_ICON`, never its old outcome), `runStateModel.js`/`useRunState`, `traceClearModel.js`/`useTraceClear.js`, `forkFromSeqModel.js`/`ForkFromSeqPanel.jsx` (branch-from-history is `inject_node` with a stamped `forked_from` receipt, a CONTENT compare-and-swap; the only node action a historical snapshot offers) + `forkProvenance.js`, `traceScrollModel.js`/`useTraceScroll`/`TraceReach` (the reach button is VISIBLE; `?before=` moves the window, the server ceiling stays), `traceEpisodeModel.js`/`TraceEpisodes`, `conceptForest.js`/`PortfolioConcepts.jsx`, `crossRunRank.js`/`CrossRunPanel` (ranks only inside one (task, direction) group, competition ranks, ties named), `claimsCurationModel.js`/`ClaimsCuration.jsx`, the `assistant*Model.js` family beside `AssistantBar.jsx` (whose three layouts stay render functions; its coverage is source pins, so `test/assistantBarResourceTruth.test.js` SSR-loads the module). `api.js` is a BARREL over `apiClient.js`/`commandModel.js`/`commandProtocol.js`/`commandStorage.js`; the one narrow exception is `reviewRouteApi.js`. Build into a STAGING dir (`--outDir .dist.stage`) when a live `test_server` is running. |

## Engine invariants (violating these breaks replay/resume)

1. **The engine is the sole writer of domain events.** Background tasks return values; only the
   main task appends FOLDED events. The accepted exceptions are TYPED and each is a registry with a
   splice-position proof, not a convention: the concurrent-research task may append
   `events/types.py::BACKGROUND_APPENDABLE` (asserted at the append sites;
   `tests/test_background_appendable.py` proves splice neutrality); a concurrent task or thread may
   append `DIAGNOSTIC_EVENTS` (fold-ignored AND excluded wholesale from every seq-equality fence —
   `engine/speculation.py::_proposal_authority_seq` discards a paid proposal when a non-diagnostic
   row lands in its window, which is why a `train_monitor_alert`, an `agent_phase_*` row or a
   `memory_read` must be diagnostic and not merely fold-ignored); the eval worker thread appends the
   FOLDED `SETUP_THREAD_APPENDABLE` pair (keyed purely by command; the only folded pair whose
   neutrality is proven — not a precedent for `node_evaluated`); the assistant tool layer appends
   `ASSISTANT_APPENDABLE` (disjoint from `serve/protocol.py::CONTROL_EVENTS` by a guard); the
   UI/CLI append only control intents in `CONTROL_EVENTS`. `EV_NODE_EVAL_STARTED` is a FOLDED
   main-task append outside `_write_lock` whose POSITION is load-bearing (after its own
   `node_created`; splicing it elsewhere flips another Card's `selection_ready`) — keep it on the
   main task at the dispatch decision. Concurrent build fan-out threads append their OWN node's
   folded events (independent nodes, serial id reservation, an order-tolerant fold); every build
   now runs in a worker through `orchestrator.py::_offload_build`, and its outputs are read off the
   `DeveloperResult` envelope, never off the shared instance. A worker that needs a run-GLOBAL
   gate reports it and the main task appends it after the join. The offloaded PROPOSAL writes
   nothing itself: `novelty.py::_capture_proposal_events` buffers its folded intents and the main
   task publishes them after the await (`tests/test_offload_lane_writes_no_folded_events.py`
   follows the call one helper down, which the AST walk that missed this could not). Evaluation
   children outlive the turn that admitted them, so ask of anything new on the main task: "does
   this decision assume no eval is running?" (`_refuse_finish_over_adopted_evals`,
   `_drain_adopted_evals`). When you add a non-folded event the question is not "does the fold
   read it?" but **"does any reader key on its position?"**.
2. **Exactly one terminal event per node** (`node_evaluated` | `node_failed`). The fold is
   idempotent on duplicate terminals (first terminal wins).
3. **Every side effect must be gated on a domain event** so resume-by-replay is idempotent
   (`fork_done`, `inject_done`, `confirm_done`, `<x>_requests`/`<x>s_done` counter pairs).
4. **State is only observed via `fold(store.read_all())`** — never cache derived state across
   loop iterations without re-folding. This is about the ENGINE's loop: the server memoizes the
   fold per REQUEST (`serve/appstate.py::request_fold_scope`, keyed by file identity, a pure ASGI
   middleware) and deliberately not across requests, because a folded `RunState` is mutable.
5. **`fold` must stay deterministic and order-tolerant**: no I/O, no LLM calls, unknown event
   types are ignored (forward compat), new event data fields are additive-only with reader-side
   defaults for old logs.
6. Settings recorded in the `run_started` event win over live config on resume.
7. Event type names are constants in `looplab/events/types.py`; a typo'd literal silently
   no-ops (unknown types are skipped), and `tests/test_event_types.py` guards against that —
   always add new event types to the registry.

The full account of every seam above — the measurements, the incidents and the alternatives
refused — is in `docs/53-agent-guide-narratives-2026-09-06.md` ("Engine invariants").

## Conventions and traps

- **This file is on a byte budget.** `tests/test_documentation_contracts.py::CLAUDE_MD_MAX_BYTES`
  refuses a guide over the budget, because every agent turn pays for every byte here before a
  single file is read (the guide was 259 KB on 2026-09-06, 75 % of it package-map narrative). A
  RULE belongs here; the measurement, the incident and the alternatives refused belong in the
  module docstring, the numbered doc, or `docs/53-agent-guide-narratives-2026-09-06.md`, which is
  where every row and bullet of this file was archived verbatim when the budget landed. Cite by
  `<mod>.py::<symbol>`, never by line number.
- **Back-compat import shim**: `looplab/__init__.py` aliases every pre-split flat module path via
  a meta-path finder; both names resolve to the SAME module object, so monkeypatching either path
  works. Keep the `_LAYOUT` map in sync when MOVING modules (`tests/test_package_layout.py` is
  two-way), and `_RENAMED` (old FULL path → canonical, checked FIRST) when RENAMING one — both go
  through the same `_CompatLoader`, because these modules are patch seams.
- **Layering**: `core` imports nothing above itself; `events` only `core`; `runtime` nothing
  above `core`; `serve` may import anything; the engine must not grow new dependencies on `serve`.
  **`search` may import `agents` at module level; `agents` may reach `search` ONLY through a
  deferred (function-local) import** — five search modules import `agents` at module scope, so a
  module-level `looplab.search` import in `agents/` closes the cycle into an ImportError at
  startup (`tests/test_agents_search_direction.py`). `tools` reaches `engine` only function-locally
  (`tests/test_cross_package_private_seams.py` also refuses cross-package PRIVATE imports).
- **Comments are load-bearing.** The codebase documents *why* (ADR references, review provenance,
  replay-safety notes) inline. Preserve comments verbatim when moving code; write the same style.
- **Prompt strings are contracts.** Changes to prompt text alter agent behavior — never "clean up"
  prompt wording as part of a refactor. A flag that changes a prompt or a paid call defaults OFF at
  every constructor, is read through ONE `Settings` reader, and gets a
  `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` row so a resumed pre-field run keeps its historical bytes; a
  knob that only REMOVES calls or makes none gets neither. Several prompts are routed through the
  PromptStore (`render(prompts, key, default)`); grep for `render(` to find overridable prompts.
- **An HTTP-contract change must move its tests in the SAME change.** Changing a route's verb,
  path, params, response shape or refusal codes silently retires every test that speaks the old
  contract (a 404/422 before the assertion runs) — and those tests guard disproportionately the
  security properties. Grep the suite for the old spelling, re-point it, and RE-VERIFY the
  property. If a refusal code now depends on a race, pin the fail-closed SET, not one member.
- **Duck-typed seams are REGISTRY-GUARDED** — a rename that used to break silently is now a red
  test, and adding or renaming a seam means updating its registry in the SAME change. The
  registries (each with a two-way source scan): `adapters/tasks.py::TASK_OPTIONAL_HOOKS`;
  `agents/roles.py::DEVELOPER_OUTPUT_ATTRS` / `RESEARCHER_ACTION_ATTRS` / `RESEARCHER_HINT_ATTRS`
  / `LLM_PRESENCE_ATTRS` / `FACADE_STAGE_ATTRS`; `core/prompts.py::PROMPT_KEYS`;
  `engine/signal_delivery.py::SIGNALS`; `engine/triage.py::TRIAGE_ACTIONS` / `AGENT_TRIAGE_ACTIONS`
  and their classification siblings `engine/failure_diagnosis.py::ENGINE_FINAL_REASONS` /
  `DIAGNOSABLE_ENGINE_REASONS` / `DIAGNOSED_FAILURE_REASONS` (the `not_learning` overlap is
  registered EXACTLY); `engine/repair_verify.py::REPAIR_VERDICTS` (disjoint from `TRIAGE_ACTIONS`
  by a guard: a byte comparison is not a judgement a model may emit);
  `engine/speculation.py::CARD_BUILD_SKIP_REASONS`; `core/tracing.py::TRACE_WORKER_STOP_REASONS`;
  `runtime/command_eval.py::READER_PATH_KEYS`; `core/config.py::DEVELOPER_BACKENDS` +
  `DEVELOPER_BACKEND_ALIASES`; `events/types.py::BACKGROUND_APPENDABLE` /
  `SETUP_THREAD_APPENDABLE` / `ASSISTANT_APPENDABLE` / `NON_CARD_SELECTION_BACKGROUND_APPENDABLE`;
  `core/llm.py::OpenAICompatibleClient._RETRY_POLICY` (an ordered table);
  `core/llm_broker.py::BACKGROUND_LANE_PRODUCERS`; `agents/loop_options.py::LOOP_OPTION_FIELDS` +
  `EXPLICIT_ONLY_LOOP_ARGS` (must PARTITION `drive_tool_loop`'s keyword-only parameters);
  `engine/orchestrator.py::Engine.FORWARDED_SUBOBJECT_MEMBERS` (a forgotten lane runs outside the
  capped enrichment lane and reads as an unexplained stall); `serve/control_validation.py`'s five
  `ControlSpec` tables; `core/models.py::EXTRA_METRIC_CHANNELS`;
  `engine/attribute_sites.py::LAZY_ENGINE_ATTRIBUTES` + `GETATTR_DEFAULT_DRIFT` (every attribute
  the `Engine` family reads is assigned in `__init__` or owns a row naming EXACTLY the methods that
  mint it — both shrink-only; a `getattr(self, "<typo>", default)` answers the default instead of
  raising, which is how a Settings field went unwired for a week). Note `search/foresight.py`'s
  panel is a `__getattr__` proxy: set hints on the OUTERMOST wrapper and let `forward_hints`
  mirror them, never on `base` directly.
- **A deliberate refusal is a TYPE, not a message.** `core/errors.py::OperatorRefusal` marks every
  exception raised on purpose about the operator's own input (`ConfigRefusal`,
  `EnvironmentRefusal`, `LLMError`, the `RunStartPinError` family); the CLI prints those as one
  line at exit `REFUSAL_EXIT_CODE` (2) and lets everything else keep its traceback at exit 1. A
  bare `ValueError` for a new refusal puts it back in the 42-frame presentation; widening the
  boundary to catch bare `ValueError` is worse (a bug reported as a tidy message looks handled).
- **Containment is countable, not forbidden.** The one lint rule (`[tool.ruff]` selects `BLE`) is
  a CENSUS: every blind `except` needs a `# noqa: BLE001 — <why this is safe to contain>`;
  `tests/test_containment_census.py` re-derives it by AST, refuses a NEW reason-less handler,
  keeps the pre-existing reason-less sites as a shrink-only backlog
  (`tests/data/containment_unreviewed.txt`), and pins that every blind handler around a paid call
  in the run path re-raises `BudgetExceeded` first. Prefer `core/containment.py::contain(reason,
  exc)` inside the handler so the containment is on the span and in `looplab timings`.
- **Tool providers**: the `bind_state` hook is OPTIONAL (`tools/_base.py`), but a provider that
  implements it must accept the second `parent` argument (`bind_state(self, state, parent=None)`).
- **Tests isolate the environment** (`tests/conftest.py`): dotenv loading is disabled,
  `LOOPLAB_MEMORY_DIR`/`LOOPLAB_KNOWLEDGE_DIR` point at tmp dirs, and the host GPU-pool lease is
  redirected to a per-test file. Engine tests construct `Engine(...)` directly (~170 call sites) —
  keep its keyword API stable; new tests use `tests/factories.py::make_engine`. Use pytest's
  `monkeypatch` fixture (a bare `pytest.MonkeyPatch()` leaks across the session). The full suite
  runs in four background shards (`--splits 4 --group N`, `tests/test_open_item_index.py` run
  separately); never edit a module while a shard is running — `inspect.getsource` reads the file
  from disk and a source pin then sees the wrong function.
- **A guard test must not be satisfiable by a COMMENT.** Roughly 200 assertions read production
  source, half of them POSITIVE pins (`assert "<literal>" in source`), and every positive pin is
  one comment away from vacuous — `pass  # self._record_eval_start_boundary(chosen)` satisfies an
  ordered `source.index()` triple while the event is never written (that defect cost invariant 1
  its `eval_started` refund: 17 builds / 5 discards became 12 / 0). Reach for these in order:
  (1) **drive the property** (a real fallback with a counting accountant, a socket that goes silent
  mid-body, a run whose log is then read); (2) **make the rule statable** — hoist a buried rule
  into a named function and test its truth table; (3) **AST, never substrings**, for the residue,
  through `tests/_source_scan.py::called_names` / `names_read` / `function_tree` — and know that
  tier 3 proves a call is in the TEXT, not that it executes (dead branches, nested defs and
  decorators are invisible to it). NEGATIVE pins stay substrings on purpose: what must not come
  back is the TEXT. Re-verify by MUTATING a throwaway copy of the tree, never the real one.
- **The host GPU-pool lease is ONE file per OS user** (`/tmp/looplab-gpu-pool-<uid>.lock`,
  `engine/resources.py`), exclusive ACROSS PROCESSES on purpose, so a GPU-owning run waits for
  every co-hosted one — it logs the lease path and the holding PID at WARNING. An UNSPECIFIED
  footprint resolves against the TASK (`adapters/tasks.py::gpu_capable`) as well as the box, so
  the offline adapters never take the lease. A run that stalls before its first eval: check that
  lock before suspecting the engine.
- **`extra_metrics` has THREE producers and they are not equally trustworthy** — anything that
  writes a secondary metric writes its CHANNEL beside it: `declared` (operator-owned
  `EvalSpec.metrics`), `auto` (every other numeric key off the candidate's own stdout, no gate),
  `engine` (the engine's own spliced artifacts). `core/models.py::EXTRA_METRIC_CHANNELS` is the
  vocabulary, written to `node_evaluated.extra_metrics_provenance`. **Nothing derivable from an
  artifact the candidate writes can authenticate its author** — a byte-exact prefix match, an
  exact key set and an in-source marker were each rejected because the candidate writes the bytes
  — so the fact is CARRIED from where the splice is decided: `apply_engine_extra_metric_channels`
  takes a required `engine_authored` keyword and only `engine/speculation_gate.py::
  engine_authored_artifacts` may assert it. An UNTAGGED value means `unknown`, never `declared`;
  a value merged later must be tagged AT THE MERGE; `engine` means authenticated, not measured.
  `Settings.auto_extra_metrics` refuses the undeclared channel over the tag and is deliberately
  NOT pinned in `run_started`.
- **The open-item index: `OPEN[<slug>]` is the ONE key, and closing an item is a DELETION.**
  Ask "what is still open?" and `grep -rn 'OPEN\[' .` answers, across code and docs. Four
  properties, each chosen against a measured failure: (1) there is no CLOSED marker — closing is
  deleting the line, so an overstated `✅` is unrepresentable; (2) the slug survives a move
  (`tests/test_open_item_index.py` pins one declaration per slug); (3) `DECLINED[<slug>]` is
  PERMANENT and must carry `measured: <number>` and a doc citation; (4) every `OPEN[…]` carries its
  own falsifier, `proof:<predicate>` — `absent:<literal>@<path>`, `present:<literal>@<path>`,
  `missing:<path>`, `line:<a>&&<b>@<path>` — which the guard RE-DERIVES from the tree. **A red
  `test_open_item_index` is not a product defect: the item shipped, delete the marker** (leave an
  italic closure note saying what landed). Prefer a proof over the fix's own symbol, then over the
  defect's text, then over the item's home. The guard strips every marker and `proof:` line before
  evaluating. Rejected alternatives (`TODO(slug)`, `STILL OPEN`, a separate `OPEN.md`, a status
  field, a hard-coded rollup) and the drift they were measured against are in doc 53; the counts
  in this file were the first thing to drift, so **the count comes from the parser, never a
  person**.
- **A recorded fact is pinned to the site that DECIDES it, and a machine constraint is MEASURED
  rather than written down.** Seven claims failed with one shape — a fact recorded in ONE place
  whose truth lives in ANOTHER — and four of them were false on the day they were written, so an
  expiry would have caught none. Rule 1: a constraint of the MACHINE (a memory ceiling, a per-step
  time) is discovered by the thing that runs on it — a calibration step at the head of the node's
  own pipeline — never asserted in prose an agent reads (`tools/dev_probe.py` cannot: it has no
  GPU on purpose). Rule 2: everything else carries `CLAIM[<slug>] … decided:<predicate>`,
  evaluated by `looplab/core/claimpin.py` with the same predicates as the open-item index plus
  `line:<a>&&<b>@<path>`; a red `test_claim_pins` means the SENTENCE IS FALSE. `<mod>.py::<symbol>`
  citations are re-derived by `citation_defects()`; a `<mod>.py:NNN` citation is REFUSED. Run
  `python -m looplab.core.claimpin <task.json>` on a task GOAL before submitting a run, and **do
  not put ANSWERS in a goal** — the objective, the constraints and the MEASURED limits with their
  source, never a configuration copied from a benchmark table.
- Settings are flat on purpose (`LOOPLAB_<FIELD>` env vars map 1:1); never nest or rename fields —
  snapshots and env compat depend on the names. A new field costs: its row in
  `docs/guide/configuration.md`, the settings catalogue (`serve/settings_ui_schema.json` +
  `SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT` + the keyset revision, or a written-down entry in
  `SETTINGS_UI_SCHEMA_UNCURATED_FIELDS`), the `tests/test_settings_ui_schema.py` and
  `ui/test/settingsSchemaResource.test.js` counts, the calibration digest + field count in
  `tests/test_calibration_profile_home.py`, `tests/test_engine_options.py::ATTR_BY_FIELD` for an
  `EngineOptions` field, and a `tests/test_options_divergence.py` row when the two defaults differ.
- `looplab/sweep.py` is NOT a CLI subcommand — it is a runtime helper imported by *generated*
  solution code inside the sandbox (see its docstring).
- A run directory contains `events.jsonl`, `config.snapshot.json`, `task.snapshot.json`,
  `engine.lock`, and per-node workdirs (`docs/guide/concepts.md` is accurate;
  `docs/04-file-layout.md` is the original *design* and differs from what shipped).
