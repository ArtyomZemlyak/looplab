# LoopLab — Agent-Prompt & Daily-Diff Mega-Review (2026-07-09)

**Scope.** (a) Every commit of 2026-07-09 (`0fee408…8a53520`, 15 commits, 51 files, +1509/−179);
(b) every LLM-facing prompt string in the codebase — `agents/` (roles, tool-loop, strategist,
unified, cli, deep-research), `search/` (foresight, best-of-N, hybrid-merge), `engine/`
(orchestrator hint prose, lessons, genesis), `serve/` (genesis boss, command boss, chat, assistant,
report, scope-report), `adapters/` (repo developer mega-prompt, task briefs, mlebench),
`tools/` (all agent-facing tool descriptions), `trust/`, `core/hardware.py` attention points,
plus the PromptStore plumbing. Each finding below was verified against the consuming code
(file:line refs are as of `8a53520`).

**Method.** Five parallel reviewers (today's diff; agents/ prompts; search+engine prompts;
serve/ prompts; adapters/tools prompts), findings adversarially re-verified, duplicates merged.
The four highest-severity findings were independently re-confirmed by a second reader.

**Verdict in one paragraph.** Today's diff is high quality — invariants hold, docs/diagram were
updated in the same change, suite green (1466 passed / 23 skipped) — but the new inline-repair
checkpoint-reuse predicate is fail-open in four shapes its docstring claims are impossible.
The prompt corpus is unusually disciplined (format contracts almost universally match parsers;
tool names named in prompts exist; memory/lesson injections are labeled truthfully), but it has
one genuine self-contradiction (skip-training), one systemic delivery bug (facade/wrapper attr
forwarding silently drops newer hints, killing board prioritization in the DEFAULT config), a
truncation layer that breaks the pagination contract every scout tool promises, and a cluster of
"the prompt teaches X but the shipped default role can't do X" drift.

---

## A. Today's diff — findings (13)

The inline-repair reuse feature (`e12c43c` + `2d1b9bf`) is the risk concentration: the
`_safe_reuse_start` predicate is conservative for the tested shapes but fail-open in four
untested ones, each ending in the exact "silent stale metric" its docstring names as the
worst case.

| # | Sev | Where | Defect |
|---|-----|-------|--------|
| D1 | major | `orchestrator.py:2490-2540` + `workspace.py:90-101` | A repair that **deletes** a module imported by an earlier stage escapes the reuse guard: deleted files are unlinked *before* the reachability closure is computed, and `_stage_reachable_files` only follows imports that still exist on disk — `changed ∩ reachable = ∅`, reuse allowed, checkpoint trained by now-deleted code is scored. |
| D2 | major | `orchestrator.py:2542-2572` | **Non-`.py` inputs are invisible** to the predicate (`reachable` only ever holds `.py`): a repair that edits `config.yaml` / a writable data copy read by `train` reuses the stale checkpoint. Only `looplab_stages.json` is special-cased. |
| D3 | major (conditional) | `orchestrator.py:2419-2436` vs `:2636` | **`cmd.cwd` base mismatch**: reachability resolves stage scripts against the workdir root, but stages execute in `es['cwd']` — with a non-default `cwd`, changed keys (`sub/train.py`) and reachable names (`train.py`) can never intersect → fail-open. |
| D4 | minor | `orchestrator.py:2439-2463` | Import parser misses parenthesized multi-line `from pkg import (a, b)` submodules and dynamic imports — `models/vit.py` edits don't invalidate reuse. |
| D5 | minor | `orchestrator.py:3023-3028` vs `replay.py:230-235` | After an in-loop reuse re-eval, the log's only stage record is `train={reused, 0s}` — the real 2h completion is never recorded for this node (the new fold guard protects only records that exist). Accounting/UI only. |
| D6 | nit | `orchestrator.py:3005-3015` | Retrain cap counts by **post-repair** stage names; a repair that renames the failed stage makes `_fi=-1` → uncounted full retrain (still bounded by attempts + anti-stuck). |
| D7 | minor | `serve/routers/runs.py:186-220` | `node_logs`: (a) docstring promises an `eval` fallback the code doesn't do (test asserts `eval == ""`); (b) stage names come only from `looplab_stages.json` + `score`, so **operator `cmd.stages` pipelines still show no live logs** — the very mode this fix targeted. |
| D8 | minor | `core/llm.py:782` | (a) pool-wide socket shutdown kills healthy siblings under `max_parallel>1` (known, BACKLOG §5); (b) `self._sdk._client` is dereferenced **outside** the try — a client shape without `_client` turns a timeout into an AttributeError. |
| D9 | nit | `repo_developer.py:40-43` | `_OUTPUT_HINT_RE` substring-matches `--dropout/--timeout/--layout` → the following token is excused as a pipeline output; missing-input bounce weakened (fails open to a loud eval error). |
| D10 | nit | `ui/src/Inspector.jsx:71-77` | Live poll only for the numerically-latest pending node — under `max_parallel>1` an older mid-eval node's Trace freezes. |
| D11 | nit | `events/replay.py:236-247` | Unkeyed (legacy) `confirm_eval` duplicates still double-count seconds; a legit re-eval of the same (node, seed) has its seconds dropped while its metric still overwrites. |
| D12 | nit | `orchestrator.py:2351` | `not spec.get('edit', False)` in `_data_binds` is dead after the DataSpec coercion — the all-mounts-read-only invariant now rests solely on the validator; a hand-rolled `repo_spec` dict would silently get a writable bind. |
| D13 | minor | tests | The reuse feature's **loop wiring is untested** (only the static helpers are unit-tested): nothing asserts `next_start` actually reaches `run_command_eval` on re-eval, that the retrain-cap abandon fires, or `node_logs` for `cmd.stages` runs. |

Prompt-text changes today were limited and consistent: `runs_tools.py:281-286` (goal-is-the-only-
task-text block), `runs_tools.py:331-338` + `serve_prompts.py:60-70` (mount/edit coercion wording
— matches the shipped coercing validator), and best-of-N now feeding real goal/direction into the
foresight ranker (bug fix). Note the `1af12a2` commit message says "reject mount:true+edit:true"
while the shipped code and all prompts say **coerce** — the code/prompts are self-consistent; only
the commit message is misleading.

---

## B. Prompt corpus — findings

~60 distinct prompts were inventoried (see the reviewer inventories in the session log; the
per-file map is reproduced in §E). Severity: 1 critical, 12 major, ~20 minor, ~12 nit.
Everything below is CONFIRMED against code unless marked otherwise.

### Critical

**P1 · The repo Developer's system prompt orders both "NEVER self-skip training" and "SKIP
training if a checkpoint exists".** One assembled system prompt (`repo_developer.py:984-991`)
contains: the ban — *"Do NOT add 'skip training if a checkpoint already exists' idempotency …
train UNCONDITIONALLY"* (`repo_developer.py:466-472`, reiterated `:496-499`) — and two mandates:
the DEFINITION OF DONE — *"At its start, if a valid checkpoint already exists there, SKIP training
and reuse it"* (`:525-526`) — and the appended hardware attention points — *"SKIP the expensive
step when its output already exists"* (`core/hardware.py:117-123`). The ban is the correct
contract (the engine's `_safe_reuse_start` machinery exists precisely because in-script
skip-if-exists silently scores a stale/parent checkpoint). Which instruction the model obeys is
per-node roulette; obeying the mandate recreates the frozen-metric failure the ban describes.
*Fix: rewrite the DoD bullet to "structure as separate stages; the ENGINE reuses the checkpoint"
and parameterize the hardware cue for the repo-developer context.*

### Major — delivery/flow (the prompt never reaches the model, or teaches something the role can't do)

**P2 · Hypothesis-board prioritization is dead in the DEFAULT config.** `foresight.py:311-322`
sets `_hyp_order` on `self.base`; with `unified_agent=True` (default) base is the `UnifiedAgent`
facade, whose `propose` forwards only `RESEARCHER_HINT_ATTRS + track_hypotheses`
(`unified_agent.py:89-92`) — and `_hyp_order` is not in the tuple (`roles.py:105-106`). The
reader (`roles.py:290`, `agent.py:839`) always gets None: the engine emits `hypothesis_ranked`
audit events claiming predicted-payoff ordering the Researcher never sees, and the `[:5]` board
cap keeps dropping arbitrary cards. Same wrapper-forwarding gap: an explicit
`track_hypotheses=False` is shadowed by the Foresight wrapper (`orchestrator.py:277-281` setattrs
onto the wrapper; `foresight.py:289-295` doesn't forward it). *Fix: register `_hyp_order` (and
`_novelty_stance`) in the hint-attr registry the wrappers forward — the registry docstring
already says "keep in sync"; this is the systemic lesson: every facade forwards ONLY the
registry, so any hint not in it silently dies at the wrapper.*

**P3 · The 4000-char tool-result cap silently destroys the pagination contract.**
`drive_tool_loop` head-truncates every tool result at 4000 chars with **no marker**
(`agent.py:448,454`), while `reposcout.read_file` promises ~16KB pages with a resume pointer
appended at the END (`reposcout.py:122-135, 261-269`) and `env_inspect.read_installed` promises
12KB (`env_inspect.py:23,95-103`). Any page > 4000 chars loses its tail *and* the pointer; the
prompt's "read a file ONCE, don't re-read" makes the model act on code it never saw.
`_base.py:39` warns providers to clip under the cap; `runs_tools` obeys (`_TRACE_CHARS=3600`),
reposcout/env_inspect don't. *Fix: page size ≤ ~3800 with the resume pointer inside the cap.*

**P4 · "Copy the hypothesis statement EXACTLY" is unsatisfiable beyond 200 chars.** The board
renders statements truncated to 200 chars (`roles.py:243`) while instructing verbatim copying
(`:244-246`); evidence links by exact-normalized hash (`models.py:173-182`). Deep-research
directions are registered untruncated and routinely longer (`orchestrator.py:1337-1341`) → the
copied (truncated) statement mints a NEW hypothesis and the board card stays open forever.
*Fix: link by id, or raise the render cap to the hash-normalization cap.*

**P5 · The default Researcher is taught a tool it doesn't have.** `tool_researcher_system`
teaches `read_file(start_line/lines)` pagination (`agent.py:731-733`), but the Researcher's
toolset (`tasks.py:400-451, 633-680`) has no `read_file` (paginating readers are `repo_read` on
repo tasks, and `read_file` only in RepoScoutTools — genesis/assistant/repo-Developer). A
compliant model burns turns on `"(unknown tool: read_file)"`.

**P6 · Sweep + `eval_timeout` guidance never reaches the DEFAULT Researcher, and the sweep
promise is false for two of three Developer backends.** The instructions live only in
`_RESEARCHER_SYSTEM` (`roles.py:35-46`, the plain researcher); `ToolUsingResearcher._SYSTEM`
(`agent.py:726-736`) — the shipped default — never mentions either, though the engine honors
`idea.eval_timeout` (`orchestrator.py:2675-2677`). Meanwhile the prompt's promise "the Developer
evaluates every grid point in ONE process" holds only for `LLMDeveloper` (`roles.py:341-347`):
`CliAgentDeveloper` (`cli_agent.py:255-266`) and `LLMRepoDeveloper` never read `idea.space`,
while the engine still applies `sweep_timeout_mult` and expects a `trials` line. Also
`eval_timeout` is consumed only on the sandbox branch, not command-eval (`orchestrator.py:
2619-2678`) — dead on repo tasks. *Fix: move sweep/eval_timeout prose into the shared suffix and
gate the sweep offer on the active backend/task kind.*

**P7 · `from looplab.sweep import run_sweep` is a guaranteed crash in Docker tiers.**
`_SWEEP_CONTRACT` recommends it unconditionally (`roles.py:90-92`); under
`trust_mode=untrusted/hostile` the solution runs in `python:3.12-slim` with only the workdir
mounted (`sandbox.py:408-449`) — looplab is not importable there.

**P8 · The hardware "attention points" miss the roles the doc says they cover.**
`hardware.py:81-83` claims the block reaches "every planning/coding agent (… Researcher,
Developer, Strategist)". Not appended for: `ToolUsingResearcher` (the default Researcher,
`agent.py:834-836`), both Strategists (`strategist.py:458, 515`), `LLMDeveloper.repair`
(`roles.py:357-358`).

### Major — serve/boss

**P9 · The boss action-router is never taught `finalize`.** `COMMAND_SYSTEM` teaches
"approve, ratify, pause, resume, stop" (`serve_prompts.py:155`); the mapper distinguishes
`stop/pause` → `EV_PAUSE` (freeze) from `finalize/abort` → `EV_RUN_ABORT` (wrap-up: report,
lessons, cost — `boss.py:120-128`). "Finish the run and write the report" therefore produces a
freeze; the wrap-up path is unreachable through the taught vocabulary (and `pause` vs `stop`
are taught as distinct but map to the same event).

**P10 · Web genesis can launch generative tasks on `backend="toy"`.** The genesis prompt and
`key_defaults` never mention `backend` (`serve_prompts.py:102-106`, `routers/genesis.py:142-143`);
`Settings.backend` defaults to `"toy"` (`config.py:384`). The CLI genesis path defaults
`backend=llm` for generative kinds (`cli.py:495`); the serve path has no equivalent — a repo task
launched from the UI (without a deployment-level backend override) gets `NoOpRepoDeveloper` and
every node silently evaluates the unchanged baseline.

**P11 · The repair prompt lists the wrong node's files.** `_REPO_DEV_REPAIR_BLOCK`'s "Files you
already wrote: {already}" is filled from `self.last_files` — "whatever node it BUILT LAST —
almost never the node being repaired" per the module's own comment (`repo_developer.py:553-559,
1006-1008` vs `:969-976`; `repair_from` seeds the true base at `:1126-1134`). *Fix: fill from
`write.files`.*

**P12 · `declare_stages` succeeds-but-is-ignored in repair sessions on operator-stages tasks.**
The tool is unconditionally in `RepoWriteTools.specs()` (`repo_developer.py:192-202`) and its
success message claims the manifest is live, but for operator-declared `cmd.stages` the engine
ignores `looplab_stages.json` (`orchestrator.py:2383-2390`). A repair that "fixes" a stage
timeout via the manifest ships, the engine re-runs the identical pipeline, and the node loops to
abandon — the D1 failure the tool spec claims to fix.

**P13 · The missing-stage-path bounce tells the Developer to `list_dir` paths its tools are
hard-refused from reaching.** `repo_developer.py:103-109` ("list_dir the ACTUAL data … or the
absolute dataset path") vs the same prompt's SCOPE rule (`:433-435`, tools rooted at the editable
repos only; data mounts materialize in per-node eval workdirs, `workspace.py:150-160`). The model
follows the bounce, gets "(path not allowed…)" repeatedly, and burns the phase's retries.

### Minor (confirmed; grouped)

*Dead or unreachable prompt surface*
- `operator` field is decorative: every researcher prompt asks for it and `_validate_emit`
  requires it, but the engine unconditionally overwrites it from the policy
  (`orchestrator.py:2046,2077,2097,2106`).
- `Idea.eval_profile` is engine-consumable (`orchestrator.py:2427,2623`) but no prompt mentions it.
- `merge_system` PromptStore override + configured parser never reach `agent_merge` from either
  production caller (`hybrid_merge.py:220-241` accepts neither `prompts` nor `parser`;
  callers `memory.py:136`, `orchestrator.py:1389`); its `{kind}` substitution is `.replace`-style,
  not the store's `$var`.
- On non-repo tasks the Researcher pays a per-node handoff-summary LLM call nobody consumes,
  under the label "the Developer (stages → plan → implement)" that misdescribes the single-shot
  developer (`agent.py:847-852`; `phase_handoff_summary=True` default).
- Repair sessions' system prompt says "the task message states this node's ACTUAL pipeline —
  trust it", but the stage note is only appended on fresh runs (`repo_developer.py:482-484` vs
  `:1020,1069`).
- foresight `rank`'s inline user message and `_rank_user_msg` are byte-identical twins that can
  desync silently (`foresight.py:94-99` vs `126-132`); the `_MAX_ITEMS` comment misdescribes
  overflow (items are truncated, not appended in input order; `:42-43` vs `:92,147`).

*Wrong info stated to the model*
- `read_logs`/`read_run_logs` claim "the FULL error/stderr" — the value is a chain of tails
  (64KB capture → 2000/500-char event tails → 3600-char clip → 4000-char loop cap)
  (`run_tools.py:85-88`, `runs_tools.py:119-124`).
- "no shell, no nvidia-smi — call gpu_info" and "check the GPU … (nvidia-smi)" coexist in the
  same assembled prompt (`repo_developer.py:436` vs `hardware.py:90`); the attention points also
  say "install only what's genuinely missing" to a Developer with no install capability.
- Data-mount write refusal says "the operator owns the eval" for files that are data mounts,
  misexplaining the actual reason (read-only mount) (`repo_developer.py:266`; mounts protected
  since `1af12a2`, `repo_task.py:427-431`).
- ValidatingDeveloper retry feedback hardcodes "Edit solution.py to fix this" — wrong for the
  repo/CLI backends it also feeds (`roles.py:558-561`).
- `_base.py` documents `bind_state(state)`; the loop calls `bind_state(state, parent)`
  (`_base.py:50` vs `agent.py:62-66`) — a provider written to the contract raises TypeError.
- Assistant "default" mode line is garbled ("reads run immediately; every mutating action … is
  PROPOSED", `assistant.py:187`); genesis/assistant tool lists omit the provided `grep`
  (`routers/genesis.py:179-180`, `assistant.py:198-199`).
- "Author the eval entrypoint … (it does not exist yet)" is stated unconditionally even for
  seeded repos that ship the script (`repo_developer.py:452-457`).
- `trust/verify.py:6-7` module docstring claims a numeric-match check `check_claims`
  deliberately does not perform (`:53-57`) — doc-only.

*Collisions / contradictions between prompts*
- Lesson statements: `lessons.py:311-313, 417-418` demand number-free generality; the downstream
  merge prompt (`hybrid_merge.py:163-167`) demands preserving "thresholds, numbers" and keys its
  SAME/DIFFERENT example on exact values.
- mlebench in-workdir brief mandates `from grader import score` — the exact pattern
  `trust/reward_hack.py:17` flags as suspicious (no allowlist).
- Genesis forced-emit fallback demands "a concrete `kind`" while the genesis system prompt says
  (twice) composable tasks have NO `kind` (`routers/genesis.py:208-211` vs
  `serve_prompts.py:24-30`); works only via the legacy-kind shim.
- Terminology drift between the two task-authoring prompts (genesis vs `propose_run`):
  `competition` vs `kaggle`; "objective name in `key`" vs "give `pattern` … NOT `key`"; legacy
  `params_style:"cli_overrides"` vs composable `%params%` — every pair works only via
  normalization shims (`tasks.py:99-102, 189-196`; `command_eval.py:279-295`).
- `agent_brief`'s "Make one focused change … then stop" (written for the external CLI agent,
  `repo_task.py:458-463`) is spliced verbatim into the in-house Developer prompt that mandates a
  full stages+plan+implement build.
- `_boss_context`'s RUN STATUS block commands action ("you MUST act: resume …") inside the
  advisory-only `/chat` endpoint with no actions channel (`llm_context.py:93-102` vs
  `boss.py:211,379-380`) — invites hallucinated "I'll resume it" replies.
- Loop nudges speak Researcher language ("call `emit` NOW with your best idea — you can refine
  on the next node") to the Strategist/pilot/triage loops that share `drive_tool_loop`
  (`agent.py:471-474`; wired via `loop_opts_from_settings`).

*Format-contract edges*
- `Idea.space` is `dict[str, list[float]]` (`models.py:41`) but the prompt says "grid
  {name: [values, ...]}" — a categorical grid fails validation and `_sanitize` doesn't clean
  `space` (`agent.py:766-782`).
- Untagged reflection lines default to outcome `"tested"`, which is in `_NEGATIVE` — a
  tag-noncompliant model can quarantine a matching "supported" lesson (`memory.py:56, 337-338`).
- foresight's `reason` field is stamped onto audit events as "the model's analysis trace" but
  the prompt never asks for it (forced tool call → often empty) (`foresight.py:49, 53-60,
  325-330`).
- The pilot menu renders `parent=` only from `parent_id`; merge actions carry `parent_ids`, so
  the pilot chooses "[i] merge" blind to what it merges (`unified_agent.py:151-160`;
  `policy.py:14,546`).
- COMMAND_SYSTEM's `reset` teaches only "propose/implement/eval" stages; the backend accepts any
  pipeline stage name ("train", "data_prep") (`serve_prompts.py:144-147` vs `control.py:45-50`);
  `CHAT_SYSTEM` omits the `merge` operator that its sibling prompts include
  (`serve_prompts.py:169` vs `:140`).
- Genesis never teaches `cmd.stages` authoring though both launch gates now accept stages-only
  tasks (`0fee408`) and `propose_run` teaches it (`runs_tools.py:341`).

*Docs/registry hygiene (nit)*
- The 13 PromptStore override keys (`researcher_system`, `developer_system`,
  `developer_repair_prefix`, `tool_researcher_system`, `strategist_system`,
  `tool_strategist_system`, `pilot_system`, `triage_system`, `foresight_system`,
  `bestofn_judge_system`, `merge_system`, `deep_research_system`, + genesis addenda) are
  documented nowhere — docs name only `prompt_dir` (`configuration.md:263`,
  `llm-and-agents.md:220`).
- Stale cross-refs: `roles.py:108` cites "`_digest_cap` ~388" (now `orchestrator.py:372`); the
  hint-attr registry docstring doesn't mention `_novelty_stance`/`_hyp_order` — the omission
  that produced P2.
- `routers/control.py:209` "require an explicit kind" comment and the `_GenesisSpec` docstring
  example predate composable authoring.

### Verified clean (worth knowing)

- **Format contracts hold** everywhere else checked: Idea schema ↔ researcher prompts; sweep
  `trials` JSON ↔ `json_line_trials`; `P<n> [GOOD|BAD]` ↔ `parse_credit_lessons`; strategist
  `_StrategyOut` fields ↔ `validate_strategy`; foresight `_Ranking` ↔ `_sanitize_ranking`;
  merge `_MergePlan` ↔ partition repair; report/scope-report fields ↔ UI consumers; all boss
  verbs map into the `CONTROL_EVENTS` allow-list; genesis's nine task kinds match the registry;
  every tool name cited in agents' prompts (triage, novelty, reflection, boss, scout) exists —
  except P5.
- **Memory/lesson injection is truthfully labeled** (prior-run insights vs same-run lessons;
  own-run exclusion on refresh; fingerprint/run_id dedup as tested).
- **`LOOPLAB_EVAL_SEED` claims match runtime** (unset during search; varied only in confirm).
- **The `1af12a2` mount/edit prompts are NOT stale** — prompt text, `propose_run`, docs and the
  shipped coercing validator all agree (only the commit message says "reject").
- The two deliberately-divergent `_IDEA_SPACE_*` wordings are documented as such and were not
  flagged.

---

## C. Information-flow assessment

**Strong:** the Researcher's working set is genuinely rich and honest (best + parent + digest +
siblings + lineage lessons + open board + operator hints with explicit newest-wins precedence via
`hints.py`); repair context is well-engineered (failure-kind tagging, ancestral repair chains,
timeout→cost-reduction directives, validator feedback folded into the rationale so retries are
never byte-identical); cross-run memory arrives labeled and deduplicated; the strategist's brief
describes real knobs that all validate; audit events for prompts exist (`agent_validated`,
`strategy_decision`, `hypothesis_ranked`).

**The systemic weakness is the delivery layer, not the prompt text:**
1. **Wrapper/facade forwarding** — hints travel by `setattr` through up to three wrappers
   (Foresight panel → UnifiedAgent → inner researcher), each forwarding only the frozen
   `RESEARCHER_HINT_ATTRS` tuple; any newer hint (`_hyp_order`, `track_hypotheses=False`) dies
   silently at a wrapper (P2). One registry, enforced by a test that greps setattr sites, would
   close the class.
2. **Truncation stack** — content passes through up to five independent caps (capture 64KB →
   event tail → provider clip → 4000-char loop cap → context budget), and the outermost one is
   the only one without a marker (P3). Tools describe their own caps but not the loop's.
3. **Prompt/capability skew across role variants** — the plain and tool-using variants of the
   same role drifted (sweep/eval_timeout/hardware present in one, absent in the other; P5-P8):
   shared contract prose should live in shared fragments (the `_hypothesis_system_suffix`
   pattern), with per-variant deltas only for genuinely different capabilities.

---

## D. Recommendations (ordered by leverage)

1. **P1** — remove/rewrite the DoD skip-training bullet + parameterize the hardware idempotency
   cue for the repo-developer context. One prompt, removes the only critical contradiction.
2. **P2** — add `_hyp_order` (and audit `_novelty_stance`, `track_hypotheses`) to the forwarded
   hint registry; add a test that every engine/foresight setattr target is in the registry.
3. **D1-D3** — make `_safe_reuse_start` fail-closed for its blind spots: treat deleted files,
   non-`.py` reachable content, and non-default `cmd.cwd` as "can't bound → full re-run"; compute
   the closure before unlinking deletions. Add a loop-level wiring test (D13).
4. **P3** — clip scout/inspect pages to fit the 4000-char cap with the resume pointer inside it;
   or make `drive_tool_loop` append an explicit `…[truncated]` marker.
5. **P9/P10** — teach the boss `finalize` (and collapse the pause/stop synonym); default
   `backend=llm` for generative kinds on the serve genesis path (mirror `cli.py:495`).
6. **P4-P8, P11-P13** — one focused "prompt-capability sync" pass: fill `{already}` from
   `write.files`; drop/refuse `declare_stages` when operator stages are canonical; reword the
   stage-path bounce; unify the researcher variants' shared prose; gate the sweep offer by
   backend; fix the docker-tier sweep hint.
7. Document the PromptStore keys (one table in `docs/guide/llm-and-agents.md`).

---

## E. Prompt inventory (file → prompts)

- `agents/roles.py` — `_RESEARCHER_SYSTEM` (30), `_HYPOTHESIS_INSTRUCTION` (50),
  `_IDEA_SPACE_PLAIN` (69), `_DEVELOPER_SYSTEM` (75), `_SWEEP_CONTRACT` (85), `_state_brief`
  (207), researcher user (288), developer implement/repair user (340/359), validator retry
  feedback (558).
- `agents/agent.py` — `_IDEA_SPACE_TOOL` (32), plan-tool spec (120), plan reminder (306),
  emit-reject (378), dedup stubs (414-424), cap (448/454), emit-after nudge (471), stuck (479),
  budget salvage (493), summarizer (554), phase-handoff (664), `ToolUsingResearcher._SYSTEM`
  (726) + user (837).
- `agents/strategist.py` — `_STRATEGIST_SYSTEM` (346), `_strategist_brief` (387),
  `_TOOL_STRATEGIST_SYSTEM` (470).
- `agents/unified_agent.py` — `_PILOT_SYSTEM` (131) + menu (158), `_TRIAGE_SYSTEM` (202) + user (236).
- `agents/cli_agent.py` — implement/repair messages (255-277), `_SEED` (24).
- `agents/deep_research.py` — `_SYSTEM` (42), `state_brief` (59), nudges (158).
- `agents/hints.py` — operator-directive block (13-31).
- `search/foresight.py` — `_SYSTEM` (53), rank user ×2 (94/126), agentic suffix (150-168),
  novelty directives (193-206), `verified_report` (63-80).
- `search/best_of_n.py` — listwise judge (54-62). `search/hybrid_merge.py` — `_MERGE_SYSTEM`
  (159), agent_merge user (186).
- `engine/lessons.py` — reflect (308), comparative (399), distill-skill (526), causal note (569).
- `engine/orchestrator.py` — hint prose (1428-1518), novelty gate (1666-1755), ensemble directive
  (1805), repair error contexts (1899-1941), inter-stage verify (2589).
- `engine/genesis.py` — kind/data/autonomy guides (28-87), sys assembly (177-194).
- `serve/serve_prompts.py` — `genesis_system` (14), `COMMAND_SYSTEM` (121), `CHAT_SYSTEM` (160),
  `COMPACT_SYSTEM` (176), `RESEARCH_BRIEF_SYSTEM` (186).
- `serve/routers/genesis.py` — addenda/tool_sys/emit/fallback (147-211). `routers/boss.py` —
  tool_sys (316), advise fallback (379), suggest (245). `serve/llm_context.py` — grounding
  (61-135). `serve/report.py` (33), `serve/scope_report.py` (114-180), `serve/assistant.py`
  (182-218, 255-261, 475-529), `assistant_commands.py` (7-40).
- `adapters/repo_developer.py` — the mega-prompt (415-537) + phase prompts (723-910) + tool
  specs/refusals (103-344) + onboarder (1144). `adapters/repo_task.py` — agent_brief (458),
  data brief (465). `adapters/dataset_task.py` — brief (303). `adapters/mlebench*.py` — briefs
  (249-262 / 177-196).
- `tools/` — reposcout (117-141), env_inspect (79-113), shell_tools (118-136), runs_tools
  (100-131, 272-345, 412-440), run_tools (66-110, 219), memora `_PROMPT` (108), `_base.py`
  contract (28-51).
- `core/hardware.py` — environment brief + attention points (74-126). `trust/verify.py` —
  `_RUBRIC` (81).

*Companion docs: [BACKLOG.md](BACKLOG.md) §5 (the same day's engine-side mega-review),
[CODE_REVIEW.md](CODE_REVIEW.md).*
