# LoopLab — Deep Research SOTA review and roadmap (2026-08-10)

**Reviewed baseline:** published `2c36b7ed` (`master`; reviewed 2026-08-10)

**Status:** research-backed design proposal — **NOT SHIPPED**, except rows explicitly marked
**SHIPPED** with commit and test evidence

**Scope:** Deep Research planning, execution, evidence, verification, durability, evaluation and
operator-facing UI

> **Authority rule.** This document deliberately separates shipped behavior from proposed work. A
> proposal here does not change a default, event schema or product claim. It becomes shipped only
> after code, replay/receipt contracts, tests, user-guide text and the process diagram land in the
> same change. Current source/tests and `docs/guide/` remain runtime authority.

> **Status update (2026-08-14).** DR-01..DR-13 are **still unshipped in master** (`d307542`): no
> `research_episode_*` / `research_plan_*` / `research_question_*` event exists in
> `events/types.py`, and no `EvidenceItem` / `ResearchEpisode` / `ProgressLedger` /
> `ResearchQuestion` symbol exists anywhere under `looplab/`. The §1 shipped-baseline table remains
> accurate. The next slice remains **DR-01 + DR-02 together (+ the DR-03 deterministic gate in the
> same release)**, exactly as §9 decides. External validation of the ordering: see the AREX note
> under DR-04.

## 0. Executive verdict

LoopLab now has a strong production foundation for Deep Research: a bounded tool loop, typed
working plan, lifecycle-aware run coverage, append-only attempt/result receipts, hard budget-stop
propagation, evidence-aware memo verification and one progressive-disclosure UI across the run
panel, report and timeline. The published `2c36b7ed` change also closes two trust failures found during the
final adversarial review: model-generated research advice can no longer be relabelled as operator
authority, and incomplete evidence receipts can no longer become false-green after a second UI
normalization pass.

It is **not yet a complete SOTA research architecture**. The remaining gap is structural: the
current stage is still one tool-using researcher producing one final memo. It has no durable
research episode, dependency-aware parallel specialists, immutable exact-span evidence items,
verifier-directed revision loop or branch-level crash recovery. Adding parallel calls before those
contracts would increase cost and noise without increasing trustworthy coverage.

The recommended sequence is:

1. durable plan/progress and exact evidence identities;
2. deterministic evidence integrity/coverage gates;
3. verifier-directed retrieve/revise/replan;
4. crash-safe branch checkpoints;
5. only then, bounded parallel specialists for independent questions;
6. close with a frozen-corpus + live-web evaluation program.

## 1. What is shipped at the reviewed baseline

| Capability | Status at `2c36b7ed` | Evidence in the tree |
|---|---|---|
| Bounded agentic investigation | **SHIPPED** | `agents/tool_loop.py`: stuck/repeat detection, convergence nudges, forced emit, context compaction |
| Typed 2–4 question working plan | **SHIPPED** | Deep Research follows `agent_self_plan` and exposes `update_plan` |
| Coverage-aware run brief | **SHIPPED** | champion, early seeds, top robust metrics, representative failure classes, recent active work; explicit omitted/retired counts |
| Lifecycle safety | **SHIPPED** | tombstoned and aborted attempts are excluded from current evidence |
| Prompt-injection boundary | **SHIPPED** | immutable suffix after prompt override; tool/external and free-form run text are untrusted data |
| Global budget hard stop | **SHIPPED** | `BudgetExceeded` propagates through researcher, verifier and background/repeat boundaries |
| Attempt/result durability | **SHIPPED, episode-incomplete** | append-only `research_attempted` / `research_completed`; a hard kill between them still spends the logical attempt |
| Claim verification | **SHIPPED, limited evidence unit** | deterministic provenance checks + semantic verifier over cited node/URL material |
| Operator-authority separation | **SHIPPED** | deep-research hints remain advisory and deduplicate separately from human directives |
| Shared memo presentation | **SHIPPED** | one `ResearchMemoCard`/body for Panel, Report and Dock |
| Progressive disclosure | **SHIPPED** | newest memo open; history, sources/tool activity and technical reasoning collapsed; trust warnings auto-open |
| Inline evidence navigation | **SHIPPED, URL/node level** | safe source chips and experiment links; no exact quote/block viewer yet |
| Assistant tool activity | **SHIPPED** | bounded unified live/legacy disclosure; raw args/results are not rendered |

The shipped slice is a meaningful P0 improvement, not a relabelled roadmap item. The proposals
below start where that slice ends.

## 2. Target architecture

The target unit is a durable **ResearchEpisode**, not a long transcript:

```text
Research request
  -> ResearchPlan + ProgressLedger
  -> dependency-ready branch wave(s)
  -> typed branch summaries + immutable EvidenceItems
  -> evolving draft
  -> integrity / coverage / semantic / freshness verification
  -> accept | targeted retrieval | revise | replan
  -> final ResearchMemo + episode receipt
```

The event store remains authoritative. The engine remains the sole domain writer; workers return
typed results and never append arbitrary episode state. Existing `research_attempted` and
`research_completed` rows remain readable, while new episode events must be designed so old logs
fold byte-compatibly and partially completed episodes resume without rebuying settled work.

### 2.1 Proposed typed records

**ResearchEpisode**

```text
episode_id, request_id, run_id, run_generation, trigger,
status, plan_revision, draft_revision, budget, progress,
started_at, updated_at, terminal_reason
```

**ResearchQuestion**

```text
question_id, prompt, dependency_ids, coverage_target, owner,
status, attempt_id, budget, result_summary, evidence_ids,
remaining_gaps, queries_tried, stop_reason
```

**EvidenceItem**

```text
evidence_id, source_type, canonical_url, title,
exact_quote_or_block, locator, retrieved_at, content_hash,
tool_observation_id, node_id, node_generation, source_quality
```

**Claim** then cites `evidence_ids`. URLs and node ids remain projections for compatibility, not
the durable citation identity.

## 3. Prioritized proposal ledger

| ID | Priority | Proposal | Why it comes here | Acceptance gate |
|---|---|---|---|---|
| DR-01 | P0 | Durable `ResearchEpisode`, `ResearchPlan` and `ProgressLedger` | Planning currently survives only inside one tool-loop context | Replay yields identical episode state across every event splice; resume never repeats a settled question |
| DR-02 | P0 | Immutable exact-span `EvidenceItem` ledger | URL + short snippet is too weak for citation integrity and later re-verification | Every externally checkable claim resolves to stable evidence ids with locator/hash/provenance |
| DR-03 | P0 | Four-layer evidence verification | Current checks do not fully separate missing, unsupported, contradictory, stale and low-quality evidence | Integrity, coverage, semantic support/contradiction and freshness/source-quality report separate verdicts |
| DR-04 | P0 | Verifier-directed retrieve/revise/replan loop | A verifier warning currently annotates a memo but does not close the evidence gap | Unsupported/unclear clauses trigger bounded targeted work or remain explicit terminal warnings |
| DR-05 | P0 | Branch checkpoints and paid-work reconciliation | A hard kill after attempt receipt can leave the research result permanently absent | Resume reconciles provider/background ids first, then runs only unfinished branches |
| DR-06 | P1 | Dependency-aware parallel specialists | Independent questions can gain breadth and latency only after P0 identities exist | 2–4 workers run only on dependency-ready questions; dependent work remains layered and deterministic |
| DR-07 | P1 | Evolving draft with explicit open gaps | Rebuilding a final memo each pass loses iterative coverage state | Every revision names closed/open gaps and the evidence delta that caused the change |
| DR-08 | P1 | Episode/branch budget controller | A 500-turn backstop is safety, not research resource allocation | Tokens, tool calls, time and branch width have hard episode-wide limits and durable spend receipts |
| DR-09 | P1 | Typed context compaction | Generic history summary can blur decisions and citation identities | Plan, decisions, gaps, evidence ids, rejected leads and remaining budget survive compaction losslessly |
| DR-10 | P1 | Observable plan/worker UI | The UI cannot show real progress until the backend exposes it | Live status derives from typed events; no decorative worker spinner or raw chain-of-thought |
| DR-11 | P1 | Deep Research evaluation harness | Report quality alone hides citation and recovery failures | Frozen-corpus regression + live-web smoke report quality, citation, retrieval, cost and crash-resume metrics separately |
| DR-12 | P2 | Source policy and freshness profiles | Web/literature grounding is opt-in and source quality is not a first-class policy | Offline/run-only, literature and live-web modes have explicit cost/freshness/source-quality contracts |
| DR-13 | P2 | Cross-episode query/evidence deduplication | Repeated cadences can rebuy the same source and query | Canonical query/source identities suppress redundant work without hiding changed content hashes |

## 4. P0 design details

### DR-01 · Durable plan and progress ledgers

Use the Magentic-One separation of a task ledger from a progress ledger, adapted to LoopLab's event
spine. The **plan** holds questions, dependencies, hypotheses and coverage targets. The
**progress ledger** records completed work, remaining gaps, stalls and the next scheduling
decision. A deterministic policy decides `continue | follow-up | replan | verify | finish` from
that typed state; an LLM may propose a replan but does not mutate folded state directly.

Minimum events:

- `research_episode_started`
- `research_plan_updated`
- `research_question_started`
- `research_question_completed` / `research_question_failed`
- `research_draft_revised`
- `research_verification_completed`
- `research_episode_completed` / `research_episode_failed`

Every event needs `episode_id`, `run_generation`, `plan_revision` and an idempotency identity.
Handlers must be splice-neutral; partially unknown future records must fail closed without making old
run playback unreadable.

### DR-02 · Exact evidence identities

Do not make a URL the citation primary key. Pages change, two blocks on one page can support
opposite claims, and a 200-character snippet cannot prove what the verifier saw. Persist the exact
quote/block within the project's advisory payload limits, a page/block locator, retrieval time,
content hash and the tool observation that produced it. For experiment evidence, retain both node id
and lifecycle generation.

Required invariants:

- `evidence_id` is content/provenance bound and stable across UI projections;
- a claim cannot cite an evidence id absent from its episode ledger;
- changed content produces a new hash/identity rather than silently rewriting old evidence;
- redaction occurs before the evidence item reaches the event writer;
- public/review projections preserve completeness receipts when evidence is withheld.

### DR-03 · Verification as four independent questions

1. **Integrity:** does every cited evidence id exist, and do locator/hash/provenance fields agree?
2. **Coverage:** does every externally checkable claim clause have evidence?
3. **Semantic support:** does the exact span support, contradict or fail to determine the clause?
4. **Freshness and source quality:** is the evidence timely enough and appropriate for the claim?

Do not collapse these into one confidence number. `unsupported`, `contradicted`, `unclear`, `stale`
and `missing` require different remediation and different UI copy. Deterministic integrity and
coverage must run before any paid semantic verifier.

### DR-04 · Verification must control the next step

Adopt the generator/verifier/revise/replan shape demonstrated by Aletheia and related scientific
research agents. After verification:

- missing coverage -> targeted retrieval;
- unclear support -> retrieve a more precise span or narrow the claim;
- contradiction -> surface both sides, revise synthesis or replan;
- stale evidence -> refresh only the affected branch;
- clean and budget-satisfied -> finalize.

The loop is bounded by revision count, remaining episode budget and a no-progress detector over
`(open_gaps, evidence_ids, draft_digest)`. A terminal memo may still contain uncertainty, but it must
name it; the system must never turn exhausted budget into a clean trust badge.

> **External confirmation (added 2026-08-14).** AREX (BAAI, arXiv:2607.21461, July 2026) implements
> exactly this pair — verifier-directed refinement (DR-04) plus typed context compaction (DR-09) —
> with "verification as the signal for the next round, not a final filter" as its central claim, and
> reports the gains coming from the directed-revision loop rather than from wider fan-out. That is
> independent, current evidence for this roadmap's priority order (DR-04 before DR-06 parallelism).
> Full synergy analysis in [doc 41](41-external-works-synergy-2026-08-14.md).

### DR-05 · Crash-safe checkpoints

Persist after plan creation, every branch terminal, every draft revision and verification pass.
Store provider response/background ids and the logical idempotency key where the provider supports
them. On resume:

1. reconcile any in-flight provider response;
2. attach/replay an already settled result;
3. retry only when the prior attempt is provably absent or safely retryable;
4. otherwise terminate as ambiguous rather than silently rebuying.

This can be built on LoopLab's existing event store and paid-operation patterns; adopting Temporal
is not required. Temporal and LangGraph are reference semantics for durable activities/checkpoints,
not proposed dependencies.

## 5. P1 execution and UI

### DR-06 · Bounded parallel specialists

Follow the orchestrator/worker/synthesis pattern only for questions whose dependency set is already
complete. Start with width 2, cap at 4, and let cost/latency measurements justify any increase.
Each worker returns a typed package, not its transcript:

```text
answer, claims, evidence_ids, remaining_gaps,
confidence, queries_tried, budget_spent, stop_reason
```

The parent owns synthesis and the engine owns durable writes. Worker histories remain optional trace
detail. Multi-agent execution must fall back to the serial scheduler with the same episode result
contract; otherwise parallelism becomes a different product mode that cannot be replay-tested.

### DR-07/DR-09 · Evolving draft and typed compaction

Maintain one evolving draft whose revisions are bound to the plan and evidence ledger. Compaction
may summarize tool chatter, but it must copy typed state verbatim: current plan, decisions, open
gaps, evidence ids, contradicted/rejected leads, branch terminals and remaining budget. Raw tool
output stays out of active context and is retrieved by observation/evidence id when needed.

### DR-10 · Operator-facing research block

The shipped card already establishes the information hierarchy. Extend it with real episode state:

- header: `Planning | Researching | Verifying | Ready | Needs review`, takeaway, question progress,
  source/evidence counts and trust counters;
- open by default: latest key findings, next actions and any unsupported/contradicted evidence;
- collapsed: plan/workers, source ledger, tool activity and technical reasoning;
- claim chips open the exact quote/block, locator, freshness and verifier note;
- branch/tool errors auto-open, while successful raw activity stays collapsed;
- older episodes remain collapsed summaries;
- Panel, Report and Dock continue to share one presentation model.

Never expose hidden chain-of-thought. The process view shows typed questions, tool status, evidence
and decisions that are already durable product state.

## 6. Evaluation and rollout gates

### 6.1 Frozen-corpus regression suite

Build a versioned corpus with answerable, conflicting, stale-source, missing-source and adversarial
prompt-injection cases. Measure separately:

- report coverage, depth and readability;
- citation correctness, coverage and effective-citation rate;
- unsupported/contradicted claim rate;
- retrieval recall and hard-negative precision;
- duplicate-query/source rate;
- replan utility and revision convergence;
- crash/resume invariance;
- p50/p95 latency, tokens, tool calls and provider-reported cost.

### 6.2 Live-web smoke suite

Use a small current-events/freshness set only as a smoke lane. Record retrieval timestamps and source
hashes so a changing web does not masquerade as a code regression. Network/provider failures must be
reported separately from research-quality failures.

### 6.3 Rollout order

| Phase | Scope | Ship gate |
|---|---|---|
| A | DR-01 plan/progress + serial scheduler | Replay/splice/resume proofs; no result-quality claim yet |
| B | DR-02 evidence ledger + DR-03 deterministic gates | Exact-span citation integrity and completeness receipts pass frozen corpus |
| C | DR-04 revise/replan + DR-05 checkpoints | Targeted revision improves supported coverage; crash injection does not rebuy settled branches |
| D | DR-06 parallel specialists + DR-08 budgets | Beats serial baseline on quality-adjusted latency/cost; otherwise remain serial by default |
| E | DR-10 live UI + DR-11 public eval reporting | Accessibility, bounded payload/bundle and trust-state tests; metrics reported by dimension |

## 7. Risks and explicit non-goals

- **Parallelism is not automatically quality.** Do not fan out dependent questions or use workers
  to vote on facts without shared evidence identities.
- **More sources are not automatically better.** Coverage, source quality and contradiction handling
  matter more than raw source count.
- **LLM verification is not proof.** Deterministic integrity/coverage gates remain authoritative;
  semantic verdicts are evidence-bearing advisory judgments.
- **No raw transcript as state.** Plans, evidence, progress and decisions are typed records; traces
  remain bounded diagnostics.
- **No hidden clean state on failure.** Budget exhaustion, omitted evidence, ambiguous provider
  completion and stale sources remain visible terminal conditions.
- **No new orchestration dependency by default.** Build on the event spine first; adopt an external
  workflow runtime only if measured recovery/operations needs justify it.

## 8. Primary research basis

- Anthropic, **How we built our multi-agent research system** — orchestrator/parallel researcher/
  citation-agent architecture and cost caveats:
  <https://www.anthropic.com/engineering/multi-agent-research-system>
- Microsoft Research, **Magentic-One** — separate task and progress ledgers with replanning:
  <https://www.microsoft.com/en-us/research/articles/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/>
- Google Research, **Deep Researcher with test-time diffusion** — evolving draft and gap-driven
  retrieval: <https://research.google/blog/deep-researcher-with-test-time-diffusion/>
- Aletheia — generator/verifier/revise/replan scientific research loop:
  <https://arxiv.org/abs/2602.10177>
- OpenAI, **Deep research** and **citation formatting** — agentic research and visible, validated
  citation presentation:
  <https://developers.openai.com/api/docs/guides/deep-research> and
  <https://developers.openai.com/api/docs/guides/citation-formatting>
- LangGraph persistence and Temporal activity semantics — checkpoint/durable-activity references:
  <https://docs.langchain.com/oss/python/langgraph/persistence> and
  <https://docs.temporal.io/activity-definition>
- LangChain Deep Agents frontend and Microsoft Magentic-UI — scoped, observable worker/tool UI:
  <https://docs.langchain.com/oss/javascript/deepagents/frontend/overview> and
  <https://www.microsoft.com/en-us/research/wp-content/uploads/2025/07/magentic-ui-report.pdf>
- DeepResearch Bench, BrowseComp-Plus and RefLens — separate report, retrieval and citation-level
  evaluation: <https://arxiv.org/abs/2506.11763>, <https://arxiv.org/abs/2508.06600>,
  <https://ojs.aaai.org/index.php/AAAI/article/view/42361/46322>

## 9. Decision summary

The next implementation should be **DR-01 + DR-02 together as one design slice**: durable plan/
progress identity plus immutable evidence identity. DR-03 deterministic integrity/coverage belongs
in the same release gate. Parallel specialists are intentionally P1: they become valuable only when
every worker can return durable, verifiable evidence into a shared episode contract.

That ordering preserves LoopLab's strongest property — replayable, receipt-backed truth — while
adding the planning breadth, iterative verification and operator observability that define the best
current Deep Research systems.
