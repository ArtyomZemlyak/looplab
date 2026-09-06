# LoopLab agent-system mega-review — 2026-08-09

**Review base:** `master` at `66bc0e61` plus the fixes listed in this document.
**Authority:** dated architecture/research record; current source, tests, README and `docs/guide/`
remain the runtime and user-contract authority.
**Scope:** production agent composition, prompts, tools, routing, permissions, memory, evaluation,
operator surfaces, documented business features, and current agent-system research

## Executive verdict

LoopLab should keep its event-sourced orchestrator. Its append-only event log, CAS/atomic append,
deterministic fold, recovery fences, permission registry, bounded loops, sandbox and provenance
controls are already a stronger foundation than replacing the engine with a general-purpose agent
framework would provide.

The principal architectural gap is one level inward: the **domain workflow is durable, but an
agent's internal phase/turn/tool workflow is not**. Prompt revisions, context manifests, model calls,
tool receipts and phase handoffs are mostly transient. A crash can replay the experiment state but
cannot resume the expensive treatment that produced it. The next architecture should extend the
existing event truth inward with additive checkpoints and typed contracts, not introduce LangGraph,
Temporal or another competing source of truth.

This review also found several smaller but live product-contract defects. The compatible ones were
fixed in this change: a registered prompt override was unreachable, external-Developer preflight did
not model its LLM fallback, task eval profiles were consumed but not exposed to the Researcher,
one-run auto-skill candidates were offered before promotion, tool collisions were silent, and several
UI/docs surfaces overstated or misdescribed their runtime scope. Larger changes are specified below
with compatibility boundaries rather than being rushed into the execution spine.

## Method

The review followed production entry points, not class names alone:

1. map every role facade, wrapper, backend, tool provider, prompt key and model-routing key;
2. trace each advertised capability to a production caller and each persisted field to a consumer;
3. compare CLI, TUI, Web/API and current guide claims;
4. exercise confirmed defects with focused tests while editing;
5. compare the resulting architecture with current primary sources on orchestration, durability,
   tool contracts, context, permissions, budgets and evals;
6. run the complete repository suite only after all changes are frozen.

The review distinguishes a **persona** (Researcher), an **execution stage** (`propose`), a
**transport/backend** (in-process LLM or external CLI), an **authority role** (who may change run
policy), and a **capability set** (tools/effects). Existing code often calls all five a “role”; that
overload is the root of several registry and preflight drifts.

## Production topology

```mermaid
flowchart TD
    Intake["CLI / TUI / Web intake"] --> Engine["Event-sourced Engine"]
    Engine --> Think["Researcher / pilot / Strategist"]
    Engine --> Build["Developer stages"]
    Engine --> Eval["Sandbox + adapter eval"]
    Think --> Aux["Deep research / foresight / reflectors"]
    Build --> Eval
    Eval --> Engine
    Engine --> Store["events.jsonl + bounded sidecars"]
```

The owner Assistant is a separate operator/control-plane system. Its run readers are scoped to the
configured run root; other file/shell effects follow their own permission boundary. It can inspect
and control existing runs, propose launch cards and create depth-one read-only subagents. It is not
part of a scientific run's durable state machine and must not be conflated with the Researcher or
Strategist.

## Agent and capability inventory

| Surface | Production implementations/composition | Tools and inputs | Authority/effects | Durable boundary |
|---|---|---|---|---|
| Researcher | offline task researcher; `LLMResearcher`; `ToolUsingResearcher`; panel, surrogate and foresight wrappers | state digest, task-specific search space, run/data/knowledge/skill/memory and optional literature tools | proposes `Idea`/operator/params/sweep/profile; never directly selects the winner | proposal becomes durable only when the Engine records the resulting domain event |
| Unified agent | `UnifiedAgent` control facade over propose, implement, repair, strategy and pilot stage clients | per-stage clients plus the shared role tool graph | one engine-facing object, while stage-local clients, contexts, routing and effects remain distinct | facade state and handoff notes are in-memory; domain results are durable |
| Developer | templated/offline developers; `LLMDeveloper`/repo developer; external `CliAgentDeveloper`; validation and best-of-N wrappers | task brief, parent code/files, stage/plan context, read-only repo/runtime tools | writes candidate code/files inside the declared surface; the external path is patch-gated by default, forced for repo tasks and explicitly disable-able for script tasks | accepted node/files and validation events are durable; an unfinished build is not resumable |
| Strategist | `RuleStrategist`, `LLMStrategist`, `ToolUsingStrategist` | run evidence, operator yields, cards, run/portfolio tools | proposes bounded run-wide strategy/control changes through governance | applied decisions/events are durable; reasoning trajectory is not |
| Pilot/action driver | unified pilot stage and action schemas | run state, pending hints, bounded control surface | chooses one allowed next action; Engine remains executor | chosen/applied domain action is durable |
| Deep Research | `DeepResearcher` over the shared provider assembly plus optional Web | stratified state brief; Run/Data, Sibling/AllRuns, CrossRun, Knowledge/Memory/Skills, Literature and Web tools subject to their existing gates | writes advisory memo/claims; does not select a champion | memo/claim records are durable; the multi-turn research treatment is not |
| Auxiliary judges | foresight ranker/panel, novelty grader, selection verifier, critic, crash triage, report/reflection and curation stewards | narrowly prepared evidence plus optional tools | advisory, gating or proposal-only according to the owning subsystem | verdict/event/sidecar may be durable; prompt/context/model revision generally is not |
| Owner Assistant | server Assistant with run launcher/control, run-root, file/shell and optional MCP tools | operator context and chat session | proposes new-run cards and controls existing runs subject to permission policy; the user starts a validated card through the launch boundary | assistant session/job stores are separate from `events.jsonl` |
| Owner subagent | `SubagentTools`, depth one, fresh read-only `plan` session | delegated prompt + read-only tools | returns text to the owner Assistant; cannot nest or mutate | final text only; no typed task/artifact lifecycle |

### Supporting agent/stage inventory

These are model-backed decision stages or wrappers, not all independent authority-bearing personas:

| Family | Production shape | Effect boundary | Main inconsistency |
|---|---|---|---|
| Proposal enrichment | surrogate, empirical panel and foresight-panel Researcher wrappers | reorders/replaces a proposal before evaluation; Engine still records/executes it | output/hint forwarding is duck-typed and terminology hides extra model cost |
| Build selection | `BestOfNDeveloper` + judge | chooses one of N implementations before execution | hidden model stage is exposed mainly as a numeric knob, not a capability/cost manifest |
| Repo onboarding | `LLMOnboarder` | proposes an eval adapter/spec for ratification | now governed by `PromptStore`; it remains a separate run-start, pre-search stage and `developer` credential route |
| Admission/taxonomy | novelty/dedup judge, concept classifier and consolidator | may admit a proposal or alter advisory concept vocabulary; never metric winner | several prompt families are hard-coded; concept override wiring was previously unreachable |
| Evaluation intervention | training monitor, live ASHA judge, crash triage and confirmation tie-break verifier | may continue/kill/request repair/break a configured statistical tie | deterministic guards and LLM stages are all called “agents,” obscuring authority |
| Reflection/memory | run reflection, comparative lessons, Memora abstraction and auto-skill distillation | writes advisory cross-run sidecars | trust/promotion lifecycle was incomplete; prompt governance remains fragmented |
| Portfolio stewards | concept, claim and task-facet stewards | proposal-only unless explicit governance applies; `concept_tidy` may ratify merges | the product-default umbrella buys concept+claim calls; task faceting is a separate default-off call because it has no behavior consumer |
| Per-run Boss | run chat + durable command service | stages/executes bounded run directives | separate prompt/tool/control vocabulary overlaps global Assistant and run tools |
| Report agents | run report and scope report writers | explanatory artifacts only | another hard-coded prompt family outside the central prompt registry |

### What is intentionally *not* one universal swarm

The current specialization is mostly static and appropriate. Dynamic worker fan-out is valuable for
naturally parallel literature/repository branches, but adding it to every scientific role would
multiply context, cost and failure modes without improving the core optimize-evaluate loop. The
recommended first use is an opt-in, depth-one, shared-budget orchestrator-worker mode for Deep
Research only.

## Confirmed inconsistencies and dispositions

| Priority | Finding | Business/runtime impact | Disposition |
|---|---|---|---|
| P0 | Prompt override `concept_consolidate_system` was registered and leaf-rendered, but production concept-map callers could not pass a `PromptStore` | advertised prompt customization silently did nothing | **Fixed:** prompt store is threaded through library, live cadence and both CLI calls; production-call inventory test added |
| P1 | Repo onboarding had an operator-critical hard-coded system prompt outside the advertised prompt store | the agent that authors a ratifiable metric adapter could not use the same prompt-governance surface as other Developers | **Fixed:** additive `repo_onboarder_system` key, production wiring and override test; byte-identical default retained |
| P1 | External Developer preflight removed all developer-stage endpoints, although validation or run-start onboarding can reach an in-process LLM; the factory also eagerly built clients that no role could consume | valid fallback/onboarding coder profiles were rejected or not probed, then failed after long external retries | **Fixed:** CLI credential/endpoint gates share one task-aware plan; server Start/Replay/resume spawn fences apply the same credential requiredness before `Popen`; factory binding is independently demand-driven, and a latent external→in-process switch is gated only when requested. The external CLI still receives no LoopLab secrets |
| P1 | `Idea.eval_profile` drove dispatch, but an LLM Researcher was not told a repo task's valid profile names or semantics | models guessed names or left a valuable cost/quality control unused | **Fixed:** exact task profiles and effective timeouts are included in the task-specific hint and exercised through real command selection; fresh malformed profiles fail loudly while historical snapshots retain their recorded dispatch semantics |
| P1 | Auto-memory wrote `status: candidate`, but the skill loader ignored lifecycle metadata and exposed every file immediately; its lifecycle key was also a truncated readable slug | one-run model-authored procedure could masquerade as promoted reusable knowledge, including through a same-prefix slug collision | **Fixed:** manual skills remain compatible; auto candidates are hidden by default, promoted skills are visible and labeled untrusted, explicit inspection can include candidates, lifecycle frontmatter cannot be forged through the model body or a multiline/Unicode task id, and promotion evidence is keyed by the full normalized-claim SHA-256 with exact-match legacy reuse |
| P1 | `CompositeTools` silently kept the first provider on duplicate function names | capability loss was deterministic but invisible; routing mistakes looked like model failure | **Fixed:** legacy first-wins behavior remains, collisions are recorded and warned, and opt-in strict composition fails immediately |
| P1 | CLI, TUI and Web had three New-run planners while README implied one shared Genesis | product parity claims exceeded implementation parity; proposal schemas can drift | **Documented truthfully now;** canonical planner/service remains a follow-up rather than a risky launch rewrite |
| P1 | Task-facet stewards spend finalize-time model calls and have a ledger plus operator CLI, but facets do not affect retrieval/ranking and are not fetched by the UI | paid product surface has no behavioral consumer | **Fixed without inventing behavior:** fresh `task_facets_finalize=false` stops scheduling the third paid call while concept+claim curation remains enabled; explicit opt-in, manual/on-demand APIs and ledgers remain. Snapshot schema v2 pins the new paid-treatment bit, while v1/missing-field snapshots preserve the historical all-three treatment behind `cross_run_curation`; facets still never authorize or rank |
| P1 | Prompt files hot-reload without a run/phase-pinned revision; UI prompts have a separate store | identical event inputs can receive different treatment mid-run and cannot be reproduced exactly | **Open architecture item:** pin a prompt bundle/context/tool-schema manifest per run or phase while retaining hot reload for future phases/runs |
| P1 | Outer event sourcing stops at the inner agent loop | crash recovery reconstructs state but loses unfinished expensive work and trajectory evidence | **Open architecture item:** additive phase/checkpoint/tool-receipt events and safe-boundary resume |
| P1 | An external or paid evaluation can finish before its terminal event is appended | a crash in that window can repeat the evaluator side effect on resume | **Open architecture item:** attempt-scoped invocation id plus durable started/completed receipt or outbox/reconciliation contract |
| P1 | `CostAccountant` supports limits, but shipped clients receive no shared finite run budget | observability is not admission control; concurrent roles cannot reserve against one cap | **Open architecture item:** shared reserve/commit call/token/dollar budget, default unlimited for compatibility |
| P1 | `concurrent_research_max_calls` counts research passes, not every provider/forced-emit/consolidation call inside a pass | the named ceiling can materially undercount real inference spend | **Open with shared budget:** debit at the provider broker, not at an outer cadence loop |
| P1 | Concurrent eval lanes admit against already-completed cumulative time rather than atomically reserving their worst-case budget | several lanes can enter under one remaining allowance and overshoot a “hard cumulative” cap | **Open resource item:** reserve bounded eval time at admission and return the unused portion |
| P1 | Developer outputs are `str` plus mutable `last_files`/`last_deleted`/telemetry side channels | wrappers or concurrent invocation can associate artifacts and receipts with the wrong node | **Open contract item:** immutable `DeveloperResult` envelope with a legacy string adapter |
| P1 | Cancellation is checked between blocking calls, while speculative build workers may be awaited to completion | pause/abort can wait for an LLM/external process that no longer has a useful consumer | **Open execution item:** attempt-owned cancellation token, quarantined late result and durable cancel receipt |
| P1 | External CLI agents use a composition-independent 600-second default and have no structured priced/unpriced usage result | timeout, cancellation and cost governance differ from in-process Developers | **Open contract item:** Settings-bound timeout plus immutable external-agent result with duration/cause/usage or explicit `unpriced` |
| P1 | MCP adapter flattens current structured results/security metadata; timeout does not cancel the outstanding operation; cache is process-global | unsafe basis for broad autonomous connector access | **Declaration boundary fixed, expansion still blocked:** malformed/oversized schemas are isolated before routing, ordinary safe names stay compatible, and ambiguous/unsafe/long origin pairs receive deterministic provider-safe full-digest names. Typed results, cancellation, principal-keyed cache and authorization binding remain required before wider role access |
| P1 | Agent trajectory/security eval corpus is absent | state replay and outcome benchmarks cannot catch bad tool routing, handoffs or prompt injection | **Open quality item:** add the eval ladder below |
| P2 | Fresh `agent_stage_models` / `agent_stage_base_urls` maps accepted misspelled keys and silently ignored them | an operator believed a stage override was active while the shared target ran | **Fixed:** one five-key registry validates fresh config; historical snapshots filter unknown old stage names with a warning so resume remains compatible |
| P2 | Deep Research hand-built a smaller tool graph than Researcher/Strategist | capability-layer promises exceeded actual role parity; configured memory/skills/run-root tools could be absent | **Fixed:** Deep Research now uses the shared provider assembly (including configured run-root, memory, skills, knowledge/Memora and literature gates) and appends only its Web-specific provider |
| P2 | Deep Research said it reasoned over all results while its compact brief dropped the middle beyond 40 nodes | an operator/model could mistake a head+tail sample for complete evidence | **Fixed:** the prompt now declares its bounded evidence, uses a deterministic best/failure/recent/seed/middle sample, and reports the exact omitted count |
| P2 | `Strategy` and `_apply_strategy` retain Developer-backend switching, but shipped Strategist output schemas and operator control do not expose it | a latent custom/historical capability complicates credential reachability without a clear product owner | **Operational gap fixed:** a custom/historical external→in-process switch now validates credentials and probes the target lazily, leaving the current Developer active on refusal. **Product choice remains:** expose the switch through governance or retire it through a compatibility migration |
| P2 | Generated run-level `AGENTS.md` described the self-contained script/JSON-metric path even for a seeded repository task | run provenance/API advertised a different execution contract, although the external backend already received the correct task brief directly and retained any repo-owned manifest | **Fixed:** generation is task-aware and records the repo brief/authority boundary; it does not overwrite a seed repository's own `AGENTS.md` |
| P2 | Runtime skill discovery was recursive while the Authoring surface listed only flat Markdown | packaged skills could execute but remain invisible to the first-party editor/review flow | **Partially fixed:** configured `skills_dir` now has a bounded recursive inventory: root Markdown stays writable, nested `**/SKILL.md` packages use safe relative display IDs and are read-only, symlinks/path escapes are skipped, and scan incompleteness is explicit while PUT/recovery stays flat. **Remaining product gap:** auto-distilled `<memory_dir>/skills/` is outside configured Authoring; candidates remain hidden until cross-task promotion and have no first-party review UI |
| P2 | Prompt governance covers a bounded registry while Genesis, assistants, reports, monitors and stewards keep separate prompt families | “canonical prompt store” is only partially true | **Partially fixed:** repo onboarding joined the store; continue with an additive typed `PromptDefinition` registry and explicit override policy, migrating byte-identically one family at a time |
| P2 | Web Settings said All-runs tools cover every run “on this machine”; both All-runs and Assistant readers are actually scoped to one configured run root | operator could infer a broader data boundary than actually exists | **Fixed:** UI schema, runtime output, config/tool copy and tests say run-root; the owner Assistant retains richer liveness/log/trace tools over that same root |
| P2 | UI guide said the launch card was not editable, while the shipped card edits a validated draft and invalidates validation after change | documented workflow contradicted the guarded UI | **Fixed:** guide now describes editable fields and mandatory revalidation |
| P2 | MCP wrapper comment said Auto runs calls inline, while UNKNOWN MCP effects deliberately ask even in Auto | security contract was correct in code/test but false in its owning comment | **Fixed:** comment now matches fail-closed behavior |

> **Re-derived 2026-08-19 against master `8be301f7`, and INDEXED.** This document had two status
> surfaces that could disagree — the table's disposition cells and the 2026-08-14 banner below — and
> they did. This block is the reconciliation, and from here the answer to "what is still open in
> doc 27?" is `grep -rn 'OPEN\['`, not either surface.
>
> **What the two surfaces disagreed about, measured:**
>
> - **The banner covers 8 items; the table has at least 13 non-resolved cells.** Three "Open" cells
>   were never re-derived by any banner — the research-call cap, the eval-lane reservation and the MCP
>   expansion — and the MCP one is the row that moved MOST (below). The banner's own preamble claims
>   it exists "so the table's 'Open …' dispositions stay honest".
> - **One banner has no cell to reconcile with.** The `RunProposal` banner refers to the planners row,
>   whose disposition reads "**Documented truthfully now;** canonical planner/service remains a
>   follow-up" — not in the `Open` vocabulary at all, so a grep for `Open` misses it entirely.
> - **The eval-receipt cell still says flatly "Open architecture item"** while its banner says
>   "PARTIALLY CLOSED, at the serve/governance layer only". The banner is right and the cell was never
>   amended: `serve/paid_ledger.py`, `engine/curation_protocol.py` and `engine/steward_invocation.py`
>   all ship claim→terminal receipts. The ENGINE half is what is still open, and that is what is
>   tagged below.
> - **The cancellation banner is now 2/3 true.** It asserts nothing propagates a cancel into a
>   provider request, an external CLI process "or MCP operation". MCP cancellation shipped in
>   `cb3433b3` on **2026-08-17**, three days after the banner: `tools/mcp_tools.py` takes
>   `cancel_check`, maps `InterruptedError` to `ToolResult(structured={"cancelled": True})` and
>   publishes `cancellable=` on the capability. The provider and external-CLI legs are still open.
> - **The eval-corpus banner was already FALSE ON THE DAY IT WAS WRITTEN.** "No trajectory/handoff/
>   prompt-injection eval ladder exists under `tests/`" — `tests/test_phase_handoff.py` landed
>   2026-08-01 and `tests/test_prompt_injection_rule.py` 2026-07-30, both BEFORE this document's own
>   date. Rungs 2-5 (curated trajectory cases, frozen outcomes, confused-deputy, repeated stochastic
>   trials) are genuinely absent, which is what the marker below claims; the sentence as written is
>   not, and `test_prompt_injection_rule.py`'s own docstring is about exactly this failure mode.
> - **The §2 prescription shipped and neither surface records it.** `ToolResult(content, structured,
>   is_error, provenance, receipt, retryable)` and `ToolCapability(...)` landed field-for-field in
>   `cb3433b3` (2026-08-17), legacy `__str__` view included — yet the SOTA table's Tools row still
>   asserts "internal API is `str` and MCP contract is flattened", which is false today.
> - **Three dead line citations** in the banner: `agents/tool_loop.py:400,474-478` (the real
>   `cancel_check` sites are `:380,502,576-580,765`; `drive_tool_loop` is at `:495`),
>   `agents/roles.py:251::DEVELOPER_OUTPUT_ATTRS` (it is at `:253`) and `agents/factory.py:396`
>   (the construction is at `:403`). One misattribution: `orchestrator._drop_stale_speculation` is
>   defined in `engine/speculation.py`, not `orchestrator.py` — it resolves through the mixin.
> - **Doc 27 has no ID namespace of its own** — the only status-bearing doc in the tree without one —
>   which is why none of the above was greppable. The slugs below are that namespace.
>
> **The index (15 items, each proof re-derived against the tree on 2026-08-19):**
>
> - **OPEN[prompt-bundle-unpinned-across-hot-reload]** the PromptStore is still re-read on every use
>   with no run/phase-pinned revision; the only run-start pins are the `run_started` settings and the
>   two `core/setup_identity.py` digests. proof:absent:revision@looplab/core/prompts.py
> - **OPEN[inner-agent-phases-not-event-sourced]** none of `agent_phase_started` /
>   `agent_checkpointed` / `agent_phase_completed` exists; the inner trajectory lives only in
>   `spans.jsonl`, which replay does not read. proof:absent:agent_phase_started@looplab/events/types.py
> - **OPEN[paid-eval-has-no-attempt-scoped-receipt]** the ENGINE half of the receipt item — the
>   serve/governance half shipped. `EV_NODE_EVAL_STARTED` carries only `node_id`+`generation`: no
>   attempt-scoped invocation id and no completed receipt.
>   proof:absent:eval_invocation_id@looplab/engine/evaluate.py
> - **OPEN[no-shared-reserve-commit-run-budget]** `CostAccountant` still takes a per-client limit,
>   `llm_broker` is concurrency ADMISSION with no reserve, and `engine/costs.py` commits post hoc, so
>   concurrent roles cannot reserve against one cap. proof:`absent:def reserve@looplab/core/llm.py`
> - **OPEN[research-cap-counts-passes-not-provider-calls]** the cap is still incremented once per
>   research PASS in the spine, not debited at the provider broker, so the named ceiling undercounts
>   real spend. proof:absent:concurrent_research_max_calls@looplab/core/llm_broker.py
> - **OPEN[eval-lanes-admit-without-reserving-time]** lanes still admit against
>   `cur.total_eval_seconds`, i.e. already-COMPLETED time, so several can enter under one remaining
>   allowance; the spine carries a live annotation prescribing the reservation.
>   proof:present:cur.total_eval_seconds@looplab/engine/orchestrator.py
> - **[closed 2026-09-06 (doc 52 row 12) — `agents/roles.py::DeveloperResult` is the frozen
>   envelope of one Developer call (its field set IS `DEVELOPER_OUTPUT_ATTRS` plus `code`),
>   captured by `engine/node_build.py::_run_developer` under the instance's own lock in the same
>   step as the call; every build and repair site reads the envelope and none reads the shared
>   instance afterwards, which is what let those calls leave the loop thread.
>   `tests/test_developer_result.py` drives it.]**
> - **OPEN[cancel-not-propagated-into-provider-request]** two of the three legs: nothing reaches an
>   in-flight provider request (`core/llm.py` has no `cancel_check` at all) and the external CLI is
>   killed on TIMEOUT rather than on a cancel token. The MCP leg shipped 2026-08-17.
>   proof:absent:cancel_check@looplab/core/llm.py
> - **[closed 2026-09-03 for the TIMEOUT half — `Settings.agent_timeout` (default 600.0, the
>   constructor's own value, bounded 0 < t <= 24 h) is passed by `agents/factory.py`. It was not a
>   default an operator could override, it was one nobody could REACH: the argument was never passed,
>   so on every composed run the constructor value WAS the value, and no config, env var or form
>   field could move it. `tests/test_agent_timeout_is_settings_bound.py` drives it through the real
>   `make_roles`.]** **OPEN[external-cli-usage-is-unpriced]** the other half of the original row:
>   `CliAgentDeveloper` returns no usage result, so an external coding agent's spend reaches neither
>   the `llm_usage` ledger nor `looplab tokens` — a run whose Developer is a CLI agent reports the
>   cost of everything except the role that writes the code.
>   proof:absent:CostAccountant@looplab/agents/cli_agent.py
> - **OPEN[agent-trajectory-eval-ladder-absent]** rungs 2-5 of §4 — curated trajectory cases, frozen
>   outcome cases, confused-deputy/cross-run-scope, repeated stochastic trials with CIs — have no
>   corpus. (Rung 1 exists and predates this document; see the correction above.)
>   proof:missing:tests/test_agent_trajectory_corpus.py
>   **[2026-08-20 — rung 3 is now built for ONE judge, and the amendment is narrow on purpose.]**
>   `looplab/judgebench/` + `tests/data/judge_bench/train_monitor.v1.jsonl.gz` is a frozen outcome corpus
>   for the training-log monitor: 450 recorded decisions, each carrying the recorded input, the
>   recorded verdict and a label derived from what the node did NEXT. It does exactly what this
>   section's last paragraph asked for — existing traces seeded it after redaction through
>   `core/redact.py::redact_output_tail`, the same screen persisted tails already pass. It is one
>   judge of four, it is one task family and one model, and it says so in its own header rather than
>   only in its docs; the remaining three judges are tracked by the
>   `judge-bench-covers-one-judge-of-four` item in `docs/BACKLOG.md` §0.19 (spelled without its
>   marker token here — a slug is declared exactly once and the declaration lives there).
>   Rungs 2, 4 and 5 are untouched: nothing here scores a TRAJECTORY (which tools were called, in
>   what order), nothing exercises prompt injection or cross-run scope, and nothing repeats a
>   stochastic trial — the corpus holds one sample per decision, so it carries no confidence
>   interval and cannot support one.
> - **OPEN[three-new-run-planners-no-shared-schema]** CLI, TUI and Web still plan a new run three
>   ways; no `RunProposal` service or shared schema exists anywhere in `serve/`, and
>   `engine/genesis.py` says so in production source. proof:absent:RunProposal@looplab/serve
> - **[closed 2026-09-03 — `McpTools.cached()` is keyed on a digest of the config `load_config`
>   resolves, which is what actually determines the server set: an operator who edits `.mcp.json` no
>   longer keeps talking to the old servers, and a per-principal config source would key itself.
>   Deliberately NOT keyed on "the principal", because no per-principal config source exists — see
>   the item below — so that key would spawn N identical subprocess sets and buy nothing. Nothing is
>   evicted (a handle owns a thread, a loop and a subprocess and exposes no close), so the number of
>   distinct configurations one process connects for is bounded instead.
>   `tests/test_mcp_cache_key.py`.]**
> - **OPEN[mcp-config-has-no-per-principal-source]** the residue the cache key cannot supply: MCP
>   servers are resolved from `LOOPLAB_MCP_CONFIG` / `LOOPLAB_MCP_SERVERS` / `.mcp.json`, all
>   process-wide, so every session on a shared server gets the same server set whatever principal is
>   driving it. proof:absent:principal_mcp_config@looplab/tools/mcp_tools.py
> - **OPEN[prompt-governance-has-no-typed-registry]** repo onboarding joined the store, but the
>   additive typed registry the row asks for does not exist, so Genesis, assistants, reports, monitors
>   and stewards keep separate prompt families. proof:absent:PromptDefinition@looplab/core/prompts.py
> - **[closed 2026-09-03 — `serve/control_validation.py::_normalize_set_strategy` accepts
>   `strategy.developer` and validates it against `core/config.py::developer_switch_names()`, the one
>   home the Strategist's own `available_developers` is derived from, so the operator and the model
>   cannot be told different things are switchable. An unknown name is a 400 NAMING the valid set
>   rather than a silent drop: the operator is present here and can fix a typo, which is the
>   asymmetry `core/appconfig.py` already draws. The model half shipped in the same change
>   (`strategist-developer-field`) — `_StrategyOut.developer` plus a durable receipt for the drop
>   `validate_strategy` makes. `tests/test_strategist_developer_switch.py` drives both ends.]**
> - **OPEN[auto-distilled-skills-outside-authoring]** the P2 remaining product gap: the Authoring
>   surface's roots are `prompts`/`skills`/`knowledge` off `Settings`, so auto-distilled
>   `<memory_dir>/skills/` candidates stay hidden until cross-task promotion with no first-party
>   review UI. The named close is a `memory_skills_dir` root on that surface.
>   proof:absent:memory_skills_dir@looplab/serve
>
> **Not re-derived here, and saying so:** the `**Fixed:**` rows above (they were not this pass's
> scope) and the Validation record's suite counts.

> **Status update (2026-08-14) — the open architecture items above, re-verified against master
> `d307542`.** One consolidated banner so the table's "Open …" dispositions stay honest:
>
> - **Prompt/context pinning — STILL OPEN.** No prompt-bundle/manifest pin exists; the PromptStore
>   still hot-reloads mid-run, and the only run-start pins are the settings in `run_started` plus
>   the two `core/setup_identity.py` digests (task payload + sorted config/workspace manifest).
>   *Close at the root:* stamp a content hash per rendered prompt family into `run_started`
>   (additive field) and let `render()` warn or refuse when live text diverges from the pinned hash
>   for an in-flight phase.
> - **Durable inner phases — STILL OPEN.** No `agent_phase_started` / `agent_checkpointed` /
>   `agent_phase_completed` events exist in `events/types.py`; the inner trajectory still lives only
>   in the diagnostic trace sidecar (`spans.jsonl` via `core/trace_append.py`), which is not folded
>   and not resumable. *Close at the root:* land the three additive events of §1 below with a
>   registry entry in the `BACKGROUND_APPENDABLE` style and one declared resume boundary in
>   `agents/tool_loop.py::drive_tool_loop`.
> - **Idempotent external/paid receipts — PARTIALLY CLOSED, at the serve/governance layer only.**
>   `serve/paid_ledger.py` (claim→terminal receipt ledger, doc 25 SR-01),
>   `engine/curation_protocol.py` (at-most-once finalize curation keyed by content digest) and
>   `engine/steward_invocation.py` (operator `action_id` keying) all shipped. The engine eval path
>   gained a durable start boundary (`EV_NODE_EVAL_STARTED`,
>   `engine/evaluate.py::_record_eval_start_boundary`) but still has no attempt-scoped invocation id
>   + completed receipt for an external/paid evaluator — that half of the row stays open.
> - **Shared reserve/commit budget — STILL OPEN.** `core/llm_broker.py` is run-local concurrency
>   ADMISSION (a closed lane vocabulary), not a budget; `engine/costs.py` is a durable POST-HOC
>   metering ledger (`_commit_usage_delta`) with no reservation step; `CostAccountant(limit=...)`
>   exists (`core/llm.py:1638-1641`) but `core/llm.py`'s own header still records that shipped
>   Settings expose no dollar-cap field. *Close at the root:* a run-level reserve/commit pool checked
>   at the broker's single admission choke point (both clients already pass through it), default
>   `None` for compatibility.
> - **Cancellation token — STILL OPEN (cooperative checks only).** `drive_tool_loop` takes a
>   `cancel_check` probe (`agents/tool_loop.py:400,474-478`), eval watchdogs share `_evaluate`'s
>   `kill_signal`, and stale speculation is dropped (`orchestrator._drop_stale_speculation`) — but
>   nothing propagates a cancel into an in-flight provider request, external CLI process or MCP
>   operation, which is what the row asked for.
> - **`DeveloperResult` envelope — STILL OPEN.** Developer output remains `str` plus the mutable
>   side channels `last_files`/`last_deleted`/`last_footprint` (registry
>   `agents/roles.py:251::DEVELOPER_OUTPUT_ATTRS`); no immutable envelope type exists. Likewise the
>   external CLI timeout is still a composition-independent constructor default of 600 s
>   (`agents/cli_agent.py:226`; `agents/factory.py:396` passes none) with no priced/`unpriced`
>   usage result.
> - **Agent eval corpus — STILL OPEN.** No trajectory/handoff/prompt-injection eval ladder exists
>   under `tests/`; the closest artifacts are the opt-in live smokes
>   (`tests/test_live_scenarios.py`, `LOOPLAB_LIVE_SCENARIOS=1`) and the replay/outcome unit suites.
> - **Canonical `RunProposal` service — STILL OPEN.** No such symbol exists; the three planning
>   stacks below remain separate. The launch boundary itself hardened since
>   (`serve/launch.py::_confine_task_file` + `task_file_roots`), which narrows the risk but is not
>   the shared planner/schema.

## Duplication and taxonomy debt

### One word, several contracts

Today these registries overlap without one validated definition:

- protocol personas (`Researcher`, `Developer`, `Strategist`);
- unified execution stages (`propose`, `implement`, `repair`, `strategy`, `pilot`);
- model/profile routing keys (those stages plus split roles and helpers);
- governance roles (`researcher`, `strategist`, `boss`);
- prompt keys;
- duck-typed forwarding/capability attribute lists;
- per-surface tool composition.

The compatible target is an additive `AgentSpec`/`CapabilityManifest`, not a rewrite of the classes:

```text
identity/persona + stage + backend + model target + prompt id/revision
+ input/output schema + tools/effects + authority + budget + fallback + checkpoint policy
```

Existing classes remain adapters. Source-scan tests should prove that every registered stage/prompt/
capability has a consumer and that every consumer is registered. The external-Developer reachability
fix is a first concrete slice of this model.

Use precise names in product and code: **unified control facade** for `UnifiedAgent`, **role** for
Researcher/Developer/Strategist/Pilot, **stage agent** for a model-backed judge/monitor/researcher,
**deterministic guard** for rule-only gates, and **product assistant** for the per-run Boss/global
Assistant surfaces. Describing the facade as a shared persona overpromises shared conversation
state: the unified control facade coordinates distinct stage clients and local contexts. Maintained
user/source copy now uses the facade terminology; no runtime context-sharing behavior was changed.

### Three planning stacks

- Web main-menu New run: owner Assistant + `RunLauncherTools.propose_run`;
- TUI: server `/api/genesis` job;
- CLI `--goal`: `engine/genesis.py` kind-selecting task author.

They share task-adapter validation and backend-default authority, but not planning code or schema.
Web additionally submits a reviewed `/api/start/preflight` token; TUI posts directly to `/api/start`,
whose server validates before spawn but issues no reviewed receipt; CLI validates directly. A future
canonical `RunProposal` service should own task validation, normalized settings, provenance,
editable-field policy and launch fingerprint. Existing callers should be compatibility adapters so
saved cards and CLI behavior do not disappear.

### Paid but inert task facets

Task facets currently have generation, proposal, ledger and operator machinery but no retrieval or
selection consumer. This is worse than a simple roadmap stub because it can add synchronous paid
finalize work. Fresh configurations now keep that specific call off with `task_facets_finalize=false`,
while the manual/on-demand paths, durable ledger and an explicit opt-in preserve the feature. Historical
snapshots that lack the field resume their original schedule instead of silently changing treatment.
Before wiring facets into behavior, create offline fixtures proving that the signal improves applicable
prior ranking without crossing task scope; never treat facets as authorization.

## SOTA comparison

The source statements below come from current primary documentation or original papers; the LoopLab
column is this review's inference from source and tests.

| Area | Current pattern | LoopLab position | Recommended import |
|---|---|---|---|
| Orchestration | OpenAI distinguishes manager-as-tools from ownership-transferring handoffs; Anthropic recommends the simplest composable workflow that fits | static specialization is sound; owner subagent is manager-as-tool-like | make ownership and handoff artifact explicit; do not add universal delegation |
| Durable execution | Temporal records nondeterministic activities; LangGraph checkpoints threads; Google ADK resumes completed/unfinished branches with at-least-once tool assumptions | domain events are excellent, inner treatment is transient | additive phase/checkpoint events, idempotent receipts and resume only at declared safe boundaries |
| Context | Current agent harness guidance uses compact, explicit artifacts and append-only session history outside model context | bounded compaction exists, but handoff is prose and prompt/context revision is unpinned | typed `ContextEnvelope`/`HandoffArtifact` with refs, hashes, provenance, audience and open questions |
| Tools | Current MCP defines output schemas, structured content and `isError`; tool metadata is untrusted | internal API is `str` and MCP contract is flattened | additive internal `ToolResult`/`ToolCapability`, legacy string adapter, typed risk/idempotency/cancel metadata |
| Budgets | Production multi-agent systems bound delegation and spend; long operations expose cancellation/resume | call cost is metered, not globally reserved or cooperatively cancelled | shared call/token/dollar reservations and cancellation propagated into transport/tool execution |
| Evals | OpenAI trace grading and Anthropic agent eval guidance grade trajectories, tools, handoffs and repeated stochastic trials | replay tests state folding; bench grades outcomes | layered trajectory, safety and repeated-trial corpus derived from traces |
| Remote interop | A2A defines cards, skills/auth, task lifecycle, artifacts, streaming and cancel | no current remote-agent business requirement | keep future contracts A2A-compatible; do not ship a remote runtime yet |

### Primary research sources

- [OpenAI Agents orchestration](https://developers.openai.com/api/docs/guides/agents/orchestration),
  [agent evals](https://developers.openai.com/api/docs/guides/agent-evals),
  [trace grading](https://developers.openai.com/api/docs/guides/trace-grading), and
  [prompting](https://developers.openai.com/api/docs/guides/prompting).
- Anthropic on [building effective agents](https://www.anthropic.com/engineering/building-effective-agents),
  [multi-agent research](https://www.anthropic.com/engineering/multi-agent-research-system),
  [context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents),
  [long-running harnesses](https://www.anthropic.com/engineering/harness-design-long-running-apps),
  [managed agents](https://www.anthropic.com/engineering/managed-agents), and
  [agent evals](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents).
- [Temporal workflow definitions](https://docs.temporal.io/workflow-definition),
  [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence), and
  [Google ADK resume](https://adk.dev/runtime/resume/).
- [Microsoft Agent Framework workflows](https://learn.microsoft.com/en-us/agent-framework/workflows/)
  and [durable extension](https://learn.microsoft.com/en-us/agent-framework/integrations/durable-extension).
- MCP 2026-07-28 [tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools),
  [authorization security](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations),
  and [transports](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports).
- [A2A specification](https://a2a-protocol.org/latest/specification/),
  [Magentic-One](https://arxiv.org/abs/2411.04468),
  [τ-bench](https://arxiv.org/abs/2406.12045),
  [ToolSandbox](https://arxiv.org/abs/2408.04682),
  [AgentDojo](https://arxiv.org/abs/2406.13352), and
  [CaMeL](https://arxiv.org/abs/2503.18813).

## Target architecture, without a second orchestrator

### 1. Durable inner phases

Add feature-flagged, fold-compatible events such as:

- `agent_phase_started`: agent/stage id, resolved model, prompt/context/tool manifest hashes, budget;
- `agent_checkpointed`: turn boundary, completed idempotent receipts, compact handoff artifact ref;
- `agent_phase_completed`: typed outcome ref, actual usage/cost and terminal reason.

Old folds ignore additive events. Resume re-enters only a declared boundary and never blindly repeats
an unknown side effect. Large prompt/tool content stays in content-addressed artifacts; events retain
hashes and bounded metadata.

### 2. Typed compatibility layer

Introduce internal types while preserving current public adapters:

- `ToolResult(content, structured, is_error, provenance, receipt, retryable)` with `str(result)` as
  the legacy view;
- `ToolCapability(effect, risk, idempotency_key, concurrency_safe, cancel)`;
- `ContextEnvelope` and `HandoffArtifact` with an existing prose summary compatibility field;
- `AgentSpec`/`CapabilityManifest` generated for current role classes.

Do not infer safety from tool names or provider order. Unknown metadata remains UNKNOWN and asks.

### 3. Shared budget and cancellation

A run-level pool should atomically reserve estimated calls/tokens/dollars before dispatch, commit
actual usage afterward, and release unused reservation. Separate role budgets can partition the same
pool. Default `None` preserves today's unlimited behavior. Cancellation must reach in-flight model
requests, subprocesses and MCP operations; a timeout that merely stops waiting is not cancellation.

### 4. Agent eval ladder

1. deterministic unit cases for routing, schema validation, permissions and checkpoint fold;
2. curated trajectory cases with expected/forbidden tool calls and handoffs;
3. outcome cases on frozen tasks;
4. prompt-injection, confused-deputy and cross-run-scope cases;
5. repeated stochastic trials with confidence intervals and cost/latency regression gates.

Existing traces can seed the corpus after structural redaction and explicit consent; diagnostics-only
raw traces should not silently become permanent training/eval data.

### 5. Bounded Deep-Research workers

Only after the shared budget/checkpoint/tool contracts exist: allow a depth-one orchestrator to split
independent research questions, give each worker a bounded context and budget, and require a typed
evidence artifact. The parent synthesizes and retains ownership. No nesting, no write tools and no
automatic transfer of scientific-run authority.

## Deliberate non-changes

- No framework migration: it would create two replay/durability authorities.
- No universal swarm: specialization stays static unless decomposition is demonstrably parallel.
- No automatic A2A runtime: there is no current remote-agent business use case.
- No “hard budget” that is actually one independent limit per client: concurrent roles require one
  shared reservation ledger.
- No task-facet effect on authorization or champion selection without scoped offline evidence.
- No broad MCP access until typed results, cancellation and auth/cache isolation are fixed.

## Validation record

Focused tests were run during confirmed fixes, followed by release-wide validation on the combined
tree:

- `.venv/bin/python -m pytest -q --disable-warnings`: **9,232 passed, 27 skipped** out of 9,259;
- `cd ui && npm test`: **956 passed**;
- `cd ui && npm run build`: **324 modules transformed**, production build succeeded;
- `cd ui && npm run check:bundle`: every size and reachability budget passed; total JavaScript was
  **1,619,429 bytes raw / 506,743 bytes gzip** against the 506,880-byte gzip budget.

The Python suite includes the documentation/navigation/source-contract guards. A local
`mkdocs build --strict` could not start in this sandbox because the declared `docs` extra was absent
and registry download was denied before dependency resolution; this is recorded as an environment
limitation, not as a successful strict build. Downstream CI must run the declared docs extra to
complete that independent rendering check.
