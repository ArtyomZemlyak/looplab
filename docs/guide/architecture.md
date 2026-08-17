---
hide:
  - toc
---

# Architecture at a glance

Two **capability maps** of the engine: a **high-level one-pager** for the mental model, and a
**detailed process diagram** covering the main stages, agents, memory tiers and trust controls.
They are navigation aids, not an exhaustive executable specification. Where shown, status labels
distinguish shipped defaults from opt-in and off-by-default capabilities; the configuration and
event contracts remain the authority for a particular run.

## The one-pager

Three planes and their connections. **Magenta = where the LLM / agent is invoked** (Genesis,
Strategist, Researcher, Developer, Critic, Reflector); the **engine** plane owns deterministic
selection/folding around the append-only `events.jsonl` spine; and the **stores** — Search, Memory,
Knowledge — feed the loop through explicit event, snapshot and sidecar contracts. The event log is
authoritative for replayable `RunState`, not for every value shown in the product.

[![LoopLab architecture — one-pager schema](../infographic/architecture-one-pager.svg)](../infographic/architecture-one-pager.svg)

## The full process diagram

A boxes-and-arrows flowchart of one turn of the engine and its main adjacent systems. Read the top row
left→right: **Propose → Novelty stage → Implement → Evaluate → Score · Trust → Refine**, then loop.
Under each stage sit its detail boxes (the memory funnel under Propose, the trust/confirm stack under
Score, …); the **Card lifecycle board** (one Card per work item, with `belief_id` grouping retries or
other work items that test the same hypothesis), **cross-run memory**
(write → hygiene → the five tiers) and the
**event spine** hang below. Colour = which agent acts.

[:material-open-in-new: Open the diagram full-screen](../infographic/agent-architecture.html){ .md-button .md-button--primary .ll-open target="_blank" }

<div class="ll-frame">
  <iframe src="../../infographic/agent-architecture.html"
          title="LoopLab full process diagram"
          loading="lazy"></iframe>
</div>

!!! note "How to read it"

    **Solid teal arrows** are the main loop; **thin dashed arrows** are feedback / memory reads &
    writes. Two edges break the circle: a **repair ↺** loop (a crash/timeout is fed back with its
    stderr, fixed in place) and a **merge** branch (two strong lineages fused into one multi-parent
    child). The **research board** (Card work items plus an explicit belief grouping) is *derived on every
    fold* — beliefs
    are deduped (exact hash + an agentic paraphrase merge), prioritized (foresight), and tracked to a
    verdict. The base
    cross-run memory paths and reflection priors are **on by default** (`~/.looplab/memory` +
    `~/.looplab/knowledge`). Product `Settings` also enable the Part-IV concept, advisory and
    structured-claim reads by default; only callers that construct bare `EngineOptions` directly
    retain the lower-level opt-in defaults.
    A run with an eligible best result can write a case and reflection artifacts, while only a
    supported improving hypothesis can seed an auto-skill. Later matching runs may retrieve the
    applicable records. A model-authored meta-note is an explanatory hypothesis over recorded
    observations, not causal proof.

    Parallel work has two canonical axes: `eval_parallel` admits experiments, while a **positive
    canonical** `llm_parallel` activates a shared provider-call total. Only that positive canonical
    value turns the ceiling on — unset, legacy-only, or a launch-time `0` bounds build fan-out
    *without* a shared provider-call total, so research overlap is not counted against one budget.
    When active, a run-local broker divides the LLM total among `build`, `deep_research`,
    `novelty_dedup`, `enrichment`, and the fail-safe `engine` lane. The Strategist can durably
    reallocate both totals and that lane map; operator pins win. Three of those lanes
    (`deep_research`, `novelty_dedup`, `enrichment`) are capped at one concurrent request with or
    without a total — a cap that describes the PRODUCER, so a lane earns it only when everything in
    it runs beside the main task with nothing blocked on its latency. Foreground engine work that is
    not a build (the per-eval repair loop, the per-eval inter-stage check) belongs in the uncapped
    `engine` lane, and `core/llm_broker.py::BACKGROUND_LANE_PRODUCERS` enforces the split.

    An evaluation outlives the turn that admitted it. The eval task group belongs to the Run, not to
    the Card session, so a session hands back to the outer loop as soon as a decision is owed — an
    eval terminal, or a producer that needs a fresh authority snapshot — and the next session adopts
    whatever is still training. A freed slot is therefore refilled by the same turn that observed the
    terminal, instead of waiting for the slowest sibling. What that widens is a LIFETIME, not a
    writer: node terminals are still appended under the engine's write lock, node creation still
    commits from the main task, and the durable eval-start boundary is still written at the dispatch
    decision. The cost is paid on the way out — finishing the run is refused while an evaluation is
    still running, and the loop drains before it finalizes, so a champion, a budget summary and a
    paid report are never computed over a metric that does not exist yet.

    GPU packing is concurrent inside one Run. Separate local Engine processes that share an OS-user
    filesystem namespace conservatively serialize GPU ownership through one crash-released pool lease;
    this avoids treating ordinal, GPU-UUID, and MIG aliases as different hardware. Different OS users,
    containers, or hosts do not share that lease and require an external scheduler. Because that lease
    is pool-wide, a blocked wait is announced — the lease file and the holding process id — rather than
    looking like a stalled Run.

    An **undeclared** footprint resolves against the task as well as the box. A task adapter may
    declare itself CPU-locked (`gpu_capable() -> False`; the shipped toy, regression, classification,
    timeseries and offline MLE-bench adapters do, because their solution code is a numpy/stdlib
    template or their brief forbids anything else); an undeclared footprint on such a task reserves
    nothing and never touches the pool lease, so an offline quadratic run cannot queue behind a
    neighbour's training job. Absent that declaration the task is assumed GPU-capable and the
    historical rule stands, and an explicit `footprint.gpus` always outranks the adapter's answer.

    Admission scans the queue for the first experiment whose complete footprint fits *now*, so an
    explicit CPU-only node behind a GPU-heavy one still starts. That is work-conserving but not fair
    on its own: a steady stream of one-GPU jobs consumes every partial release, and a wide request can
    wait for all of its GPUs to be free at the same instant forever. After **3** consecutive bypasses
    the queue head therefore takes exclusive claim on releases until it is admitted — or until the
    pool has drained completely and it still does not fit, which proves it wants more than the box
    has and hands the queue back to the jobs behind it rather than wedging the batch.

    An evaluation's resources are released when its process group is empty, not merely when the
    command exited: a metric-producing parent that leaves a descendant running would otherwise keep
    the GPU while the scheduler hands it to the next node. The sweep runs while the child is still an
    un-reaped zombie, so its process group id cannot have been recycled onto an unrelated process.
    A child that ended up in the *engine's own* process group (no new session) is swept per-PID
    instead, so the group syscall that would take the engine down with it is never issued — the tree
    still dies, just by a route that cannot overshoot.

## Where each piece lives in the code

| Concept | Module |
|---|---|
| Control loop + crash-resume | `engine/orchestrator.py` |
| The two pacing clocks: the node-count window (`cadence_due`, behind lessons/deep-research/report/Strategist/concept cadences) and the occupancy pace (`occupancy_due` — produce while an eval is running and the board behind it does not cover the width; records no `at_node`, has no setting of its own) | `engine/cadence.py`, `engine/orchestrator.py::_occupancy_paced_creates` |
| Standing watches: the durable always-on assistant record (`<runs>/assistant/.watches/`) + the lazily-started scheduler over it — the server evaluates the trigger, the wake-up carries its own stored instruction at the mode pinned when armed | `serve/assistant_watch.py`, `serve/routers/assistant.py`, `ui/src/assistantWatchModel.js` |
| Append-only log · pure fold · SQLite read-model | `events/eventstore.py`, `events/replay.py`, `events/readmodel.py` |
| Researcher / Developer / unified agent | `agents/roles.py`, `agents/unified_agent.py` |
| Canonical eval/LLM concurrency + named-lane broker | `engine/orchestrator.py`, `core/llm_broker.py`, `engine/strategy.py` |
| Card model · identity digests/receipts · replay/public projection · selection | `core/cards.py`, `events/card_ledger.py`, `serve/public_cards.py`, `search/card_selection.py` |
| Resource admission · GPU lifecycle reservations | `engine/resources.py`, `core/hardware.py` |
| Speculative Card producer/consumer · freshness/quality gates | `engine/speculation.py`, `search/speculation_quality.py`, `search/speculation_calibration.py` |
| Foresight (belief-card prioritization, predict-before-execute) | `search/foresight.py` |
| Hybrid retrieval + agent-decided merge (lessons & Card belief board) | `search/hybrid_merge.py` |
| Search policies · operators | `search/policy.py`, `search/operators.py` |
| Part IV/V concept materialization · graph · bounded frame | `core/concepts.py`, `search/concept_projection.py`, the five-module concept cluster `search/concept_graph.py` (structure) → `search/concept_tagging.py` / `search/concept_lens.py` → `search/concept_analytics.py` → `search/concept_map.py`, `serve/concept_frame.py` |
| Repo Developer: env-inspector + auto-validate | `tools/env_inspect.py`, `adapters/repo_write_tools.py` (re-exported via `repo_developer.py`) |
| Typed tool contract: immutable capability/effect/approval metadata, structured result/error/provenance/receipt, canonical manifest digest, legacy-string compatibility and cancellation propagation. Undeclared legacy tools remain `unknown` (names never imply safety); MCP preserves `structuredContent`/`isError` and cancels an in-flight future | `tools/_base.py`, `agents/tool_loop.py`, `tools/mcp_tools.py`, `tools/_mcp_transport.py` |
| Developer preflight commands: exact operator-pinned argv selected only by name, disposable candidate workspace, typed SHA-256 receipts, same trust-tier Docker hardening as eval; candidate seeding primitives are shared with eval rather than re-derived | `tools/dev_commands.py`, `engine/workspace_seed.py`, `engine/workspace.py`, `adapters/repo_task.py`, `adapters/repo_developer.py` |
| Sandbox seam (subprocess / Docker) · built-in eval watchdogs (loss/grad divergence · stall) | `runtime/sandbox.py` |
| Training-log monitor (product `Settings`: watcher on **and** early-kill on — `train_monitor_kill=True`; bare `EngineOptions`: both off; the verdict is advisory until the kill switch is on, and then only a `broken` verdict at ≥ `train_monitor_kill_confidence`, about a log the eval plan can PROVE is the run's own training — the single-command `eval.log` or a one-stage pipeline, never a stage of a multi-stage pipeline — CONFIRMED by a second consecutive parseable tick, and NOT contradicted by the engine's own deterministic measurement of the loss trajectory across the whole eval, acts; the model is asked what a tail can answer, the engine owns "is it still descending") | `engine/train_monitor.py` |
| ASHA live-curve watchdog: deterministic same-resource rank as EVIDENCE, LLM judge as the stop DECISION (consulted only inside the rank gate, so it can only narrow the stop set) | `engine/asha_monitor.py` |
| Both watchdog judges may LOOK rather than be handed a slice (`train_monitor_tools`, on): `read_log` reads a named stage log like a file — tail/head read a bounded WINDOW, range and search SEEK from the run's start, the search reporting an exact match TOTAL with the first matches and the last ones and the elided middle counted, and stopping only at its own byte ceiling or a wall-clock deadline (the pattern is a MODEL's and `re` cannot be interrupted mid-match), each naming the `from_byte` that continues it; records split on newline OR carriage return — and `metric_series` answers the log's numeric series over a time window at a granularity the judge picks, aggregating per bucket and never dropping a sample. It exists because the slice was measured at ~10 loss values ≈ 30 s of a five-hour run. A log is chosen by NAME from the eval plan's own map, never by path; what is read is candidate-authored text, so it informs the verdict and never the record | `tools/log_tools.py` |
| The crash/timeout TRIAGE judge may LOOK too (`repair_log_tools`, on): the same two tools over the same source map (`train_monitor.repair_log_tools` delegates to the watchdogs' own `_log_query_tools`, so there is one derivation of what is lookable and one reading of the attempt byte floor), for the role that had the SMALLEST slice in the engine — `res.stderr[-500:]`. Measured: v8 node 3 completed all 15 epochs and was killed 20 minutes into a second progress bar on a different total; the 522-char tail held only that second bar, the verdict read its elapsed field as training progress ("still in epoch 1 at 31:20") and prescribed a fix for a problem the node did not have — whose `n_epochs` 15→8 then never landed (`repair_verify`: `unmet`), so attempt 6 re-ran the same 10,590 steps into the same ceiling. Same trust line: it widens what the judge SEES, never what any record rests on — the verdict vocabulary, the authenticated failure `reason` and every selection rule are untouched | `engine/train_monitor.py`, `engine/crash_repair.py` |
| Variance gate · multi-seed confirmation · CV · leakage · reward-hack | `trust/gate.py`, `trust/confirm.py`, `trust/cv.py`, `trust/leakage.py`, `trust/reward_hack.py` |
| Metric salvage: recover a metric the eval already produced from a node that failed for something else, through DETERMINISTIC rungs only (the operator's declared reader, re-asked — never a model, because the agent writes the script an extractor would read). Selection-affecting: under the `audit` default the node is evaluated and counted but carries a `metric_salvaged` violation that keeps it out of `feasible_nodes` — and, since 2026-08-13, out of every CROSS-RUN claim about its metric (comparative pair lessons, the ranked reflection table, skill-card evidence — `engine/memory.py::unreliable_metric_ids`, which covers the trust gate's flagged nodes too), while what it OBSERVED is still recorded | `engine/metric_salvage.py` |
| Champion caveats: WHAT KIND OF NUMBER a run's `best_metric` is, for the portfolio row that publishes it and nothing else about it. The complementary half of `unreliable_metric_ids` — whose intersection with the champion is empty by construction, since both its members are populations the SELECTOR already refuses — so it states the two caveats that SURVIVE selection: salvaged and ADMITTED (`metric_salvage: select`) and hard-flagged and NOT ENFORCED (`trust_gate: audit`, the default). Spelled as calls to those same two predicates, so it cannot drift from the rung that decides it; read into `/api/runs` as `best_metric_caveats` and rendered by the run list and the same-task leaderboard. Since 2026-08-15 a THIRD member says something the join cannot: `params_overridden`, the champion's own committed `.py` code assigning a different value to a parameter its `Idea` DECLARES — not a caveat about how the number was measured but about what it is a number for, since `idea.params` is the coordinate every `numeric_params` reader places the result at and the one `merge_idea` breeds from. Derived from the declaration and the committed bytes, never from model-authored text; measured over the 46 preserved logs it is the one member that is NON-empty (v8 node 3, the champion, `batch_size` 8192 declared / 4096 in code) | `engine/champion_caveats.py`, `engine/repair_verify.py` |
| Repair verification: did a repair DO what its rationale said? The same deterministic-only rule and the same visible-but-not-trusted tiering — an EMPTY change set (bytes, never the rationale) is the only verdict allowed to stop the loop, and it does so after two in a row; an unmet named claim is evidence handed to the stop judge. Since 2026-08-15 that advisory half is narrower in the two ways the live evidence demanded (it was right ONCE in its first four verdicts): a token used only to cite ANOTHER node is evidence and not a promise, so it demotes to `unstated` rather than convicting, and a claim written as an abbreviation of the identifier the diff actually contains (`grad_accum` / `gradient_accumulation_steps`) counts as met. Both move a verdict away from an accusation only; replayed over all 2,480 preserved repairs, three rows move and no `inert` verdict moves, so no stop changes. A THIRD rung beside them asks a different question of different inputs and never reads a rationale at all: `declared_param_overrides` compares the RECORD's declared `idea.params` against the `.py` bytes the engine committed, so it sits in `inert`'s trust tier. It stamps `node_repaired.param_overrides` (additive, fold-ignored, the attribution half — only what THIS repair introduced) and backs the `params_overridden` champion caveat (the whole-node half, asked of the fold). It stops nothing; parsed with `ast` and bounded to declarations of ≥2 dotted parts against numeric literals, because a bare `lr` would be met by any local of that name | `engine/repair_verify.py` |
| Cross-run memory · retrieval · harmonic index | `engine/memory.py`, `engine/lessons.py`, `tools/memora.py` |
| Cross-run memory CASCADE: what a run's deletion may remove from the five shared stores — one attribution predicate per store, and an irreversible purge keyed on `run_uid` (never the reused directory NAME) | `serve/memory_cascade.py` |
| Cross-run index · claims · taxonomy/claim governance | `engine/cross_run_index.py`, `engine/claims.py`, `engine/concept_registry.py`, `engine/governance_health.py` |
| Paid proposal steward lifecycle — two at-most-once transactions over the same three ledgers: `curation_protocol.py` is the unattended FINALIZE one (semantic content-digest key, side-file claim), `steward_invocation.py` the on-demand HTTP/CLI one (operator `action_id`, in-ledger `begun` claim) | `engine/curation_protocol.py`, `engine/steward_invocation.py`, `engine/concept_steward.py`, `engine/claim_steward.py`, `engine/task_facets.py` |
| Claims & Curation (ex-Research Atlas) / owner governance API · UI | `serve/routers/cross_run.py`, `ui/src/ClaimsCuration.jsx`, `ui/src/claimsCurationModel.js` |
| Trace span exporter | `core/tracing.py` |

For the narrative behind each box, read **[Concepts](concepts.md)**; for the full design rationale and
decision records, see the **[Architecture spec](../02-architecture.md)** and the
**[Design records index](../00-INDEX.md)**.
