# Search-loop audit — run start → node metric (2026-08-07)

Scope: the Layer-3 Card queue, Card identity/dedup, Research + Deep Research, the repo Developer
(stage manifest / inline repair / triage / env prep), and evaluation (stages, reuse, the two live
watchdogs, trust gates). Everything below is code read against the **60+ real runs in `runs/`** —
48 of which carry an `events.jsonl` (11,731 logical events after batch expansion).

Findings already recorded in `/home/jovyan/data/looplab-open-questions.md` (LiteLLM, the leakage
hook, the MCP bus, the dollar budget, doc 17, the resume width refusal, convergence/futility stops)
are **not** repeated. Three sibling agents are fixing the dependency installer, the missing
`looplab_eval.py` at the `score` stage, and the `mine` stage / per-stage repair; where the audit
touches those they are cited and left alone.

**Corpus method.** All counts come from the shipped reader (`EventStore.read_all`), not a
line-by-line JSON scan — `append_many` writes a batch envelope, so a naive scan under-counts
`card_added` by 2× and hides every `card_ranked`. Scripts used: `/tmp/ae9cd716-*.py`.

---

## Verdict table

| Subsystem | Verdict |
|---|---|
| Card action digest + mint round-trip proof | **Working, and the strongest part of the loop.** |
| Card budget + Layer-5 refund | **Working**, measured on two real runs. |
| Attempt receipts (`research_attempted`, `card_build_attempted`) | **Working**; the "gate spent, memo lost" defect is fixed. |
| Card mint/claim batch atomicity | **Working** — 42 `[card_added, node_building]` envelopes, 0 torn. |
| Inline-repair durable ledger + the 50-repair ceiling | **Working**; the 2345-repair incident is closed. |
| Crash-triage verdict vocabulary | **Working**; all five verdicts reachable, two unforgeable from the wire. |
| Stage manifest + stage reuse | **Working**, but rarely exercised (3 `reused` rows in the corpus). |
| Dependency allow-list size | **Matches the docs exactly** (61). |
| Hypothesis-board merge | **Working** — but never fires under the shipped default (see F7). |
| **Card QUEUE as a selector** | **Inert unless `speculation_depth > 0`** (F2). |
| **Card scorer's exploration/foresight terms** | **Structurally blank on every selectable card** (F5). |
| **Deep Research on a long-eval run** | **Never fires** (F1). One half fixed here. |
| **`train_monitor_kill` / `asha_live_kill`** | **On by default, cannot fire on the shipped workload** (F3). |
| **Durable cost ledger** | **No attribution dimension at all** (F4). |
| Card retirement ladder, `card_ranked`, 4 of 6 card sources | **Never exercised outside tests** (F6). |
| Event-log health receipt vs the reader | **Disagree; 99% of one run is silently unreadable** (F8). |

---

## F1 — Deep Research never runs on the workload it exists for (highest cost)

`concurrent_research` exists to "overlap the think with the GPU-bound eval … the LLM is typically
remote, so overlapping hides latency behind long training" (`core/config.py:1425-1431`). Measured
across the corpus, it does the opposite of that:

| run | task | nodes | `deep_research_every` | `research_completed` |
|---|---|---|---|---|
| `rubert-dr-0804` | repo, GPU, hours/node | 2 | 3 | **0** |
| `rubert-dr-0805` | repo, GPU, hours/node | 2 | 3 | **0** |
| `rubert-dr-0807` (live) | repo, GPU, hours/node | 3 | 3 | **0** |
| `live-0806-features` | toy, ms/node | 6 | 3 | 2 |
| `rubertlite-dr-unified-v4` | repo, pre-Card | 11 | 3 | 57 (3 cadence + 54 repeat) |

Every run where Deep Research fired has *sub-second or pre-Card* evals. Every long-eval Card run has
zero. Two independent causes, both structural:

**(a) The Card session latched on the ASK, not on the SPAWN.** `engine/speculation.py`'s
`_card_phase_admit_evals` read

```python
if not session.research_spawned:
    self._spawn_research(session.bg_task_group, current)
    session.research_spawned = True          # <- unconditional
```

`_spawn_research` returns immediately when `_due_research_trigger(state)` is `None`. So a session
asked "is research due?" **exactly once**, at its first eval admission — which sits at node-count 1
while `deep_research_every` is 3 — and then never asked again for the life of the session, however
many further nodes it admitted. `CardSession(research_spawned=bool(evals))` had the same shape at
the session's entry. On `rubert-dr-0807` the session admitted n=1, n=2 and n=3 without re-asking.

**Fixed in this change** (`engine/orchestrator.py::_spawn_research` now returns whether it started
anything; both latch sites bind to that result). Driven by
`tests/test_research_overlap.py::test_spawn_research_reports_whether_it_actually_started_anything`
(behavioural) and `::test_card_session_binds_the_research_latch_to_the_spawn_result` (AST over both
call sites — the exact mutation is a constant `True`). Both proven red on a `git archive HEAD` copy
of the tree.

**(b) The cadence is counted in NODES, and that is a product decision, not a defect.** Even with (a)
fixed, `_due_research_trigger` requires `cadence_due(len(state.nodes), last, 3)`. On a workload where
one node costs 1.5–4 GPU-hours, the first "think" cannot happen before ~5–12 hours of wall clock,
and the serial `_maybe_deep_research` path is additionally gated on `not state.pending_nodes()`
(`engine/research_cadence.py:73`) — which under speculation is almost never true. The whole feature
is phrased around *time* ("a two-day eval is re-researched about hourly",
`config.py:1441-1445`) but *triggered* by node count. **Decision needed:** should the first
concurrent research pass be time-triggered (e.g. once an eval has been running longer than
`concurrent_research_interval_s`) rather than waiting for `deep_research_every` nodes?

Cost: on the flagship GPU workload the entire Deep-Research chain — memo → hints → open belief cards
→ steering of the next proposal — is dark, while the run has an idle reasoning model and hours of
GPU time.

---

## F2 — `card_driven_selection=True` is not a selector unless `speculation_depth > 0`

**Measured.** For every run, folding every observable prefix boundary (excluding prefixes that split
an atomic `append_many` batch, which the engine can never observe) and counting `selection_ready`
cards:

| run | `card_driven_selection` | pinned `speculation_depth` | nodes | boundaries with ≥1 ready card |
|---|---|---|---|---|
| `live-cards-0804` | true | (absent = 0) | 12 | **0** |
| `live-dr-check-0804` | true | (absent = 0) | 8 | **0** |
| `live-deps4-0804` | true | (absent = 0) | 3 | **0** |
| `live-deps5-0804` | true | (absent = 0) | 2 | **0** |
| `live-repair-probe` | true | (absent = 0) | 2 | **0** |
| `live-deps-0804`, `live-deps3-0804` | true | (absent = 0) | 0 | **0** |
| `spec-live-0804` | true | 1 | 12 | 12 (12 distinct cards) |
| `live-0806-features` | true | 1 | 6 | 6 (6 distinct cards) |
| `rubert-dr-0805` | true | 2 | 2 | 3 |
| `rubert-dr-0807` | true | 2 | 3 | 3 |

**Root cause.** `_stage_card_creates` (`engine/card_reservation.py:810`) is the only writer that
stages a Card as durable *inventory* — a `card_added` with no `node_building` beside it. Its only
production call site sits inside `if self._speculation_enabled():`
(`engine/orchestrator.py:1726-1751`). With speculation off, every `card_added` is minted by
`_reserve_node_build` in the same crash-atomic batch as the `node_building` that claims it, so the
card is work-owned the instant it exists and can never satisfy `_strictly_selection_ready`.
`card_next_actions` then finds nothing eligible and falls through to `policy.next_actions(state)`
(`search/card_selection.py:1894-1912`). Corroborated by the physical batch shapes: the seven runs
above wrote **only** batched `card_added` rows (plus `_record_node_less_card` audit pairs); the four
speculation runs wrote **only** standalone ones.

**Docs vs code.** `docs/guide/configuration.md:351` and `docs/guide/concepts.md:569-574` promise the
Card queue owns macro-action selection whenever `card_driven_selection` is true, with no mention of
`speculation_depth`. That is **wrong as written**. `docs/23-hypothesis-card-kanban-2026-07-20.md:20-27`
records the *symptom* on `live-cards-0804` (18 cards, `selection_ready=0`, every `policy_decision`
plain Greedy) but not the conditional — and the same doc still checks off "Layer 3 — Card selection,
scoring, policy fidelity" at `:280` and still states `speculation_depth` defaults to `0` at `:11-13`
and `:753`.

**Why it matters even though the shipped default is AUTO.** `speculation_depth` defaults to `-1`
(AUTO), which resolves to the settled `eval_parallel` — so a normal LLM+greedy run *does* get a real
Card queue. But AUTO settles to **off** for a build whose roles call no LLM, for any policy other
than `greedy`, and for a run with no id (`config.py:1210-1214`), and the AUTO down-ratchet can move
it to 0 mid-run. In every one of those cases the run silently changes selector while `run_started`
still pins `card_driven_selection: true` and the operator has no event saying so.

---

## F3 — Two watchdogs ship ON with kill authority that cannot be exercised

**`train_monitor_kill = True`** (`config.py:472-483`). A kill requires `log_role ==
LOG_ROLE_TRAINING`, which `train_monitor.eval_log_plan` grants only to the single-command `eval.log`
or a **one-stage** pipeline (`engine/train_monitor.py:437-447`). Every repo-Developer manifest
produces at least two stages (the declared stages plus the engine-appended protected `score`), so on
exactly the workload the STAGES phase exists for, the monitor is permanently advisory.
**Measured:** 41 `train_monitor_alert` rows in the whole corpus, from 2 runs.
`rubert-dr-0807`'s two rows carry `log_role: "work"`; `rubertlite-dr-unified-v4`'s 39 carry no
`log_role` at all (pre-attribution). **Zero kill-eligible rows have ever been written.**

**`asha_live_kill = True`** (`config.py:489-510`). A kill requires the metric spec to declare
`metric.resource_key` (`engine/asha_monitor.py:206-207`, `:244-245`). No `EvalSpec` field, no
default, and Genesis never authors it — it appears only in the monitor, one line of
`evaluate.py`, one comment in `core/models.py`, the docs and the tests. **Measured:** `asha_rank`
and `asha_verdict` have **0 occurrences across all 48 runs**; `rung_promoted` fired twice, in one
run. Even `runs/live-asha-0804` — the run named for the feature — contains neither.

The docs *are* honest about both preconditions (`configuration.md:152`, `:155`). The problem is the
**default**: `True` reads as "this safety net is on", and an operator watching a 4-hour training
diverge has no way to know the kill path is structurally unreachable. Either flip the defaults to
match reachability, or make the engine emit one diagnostic at eval start naming why the gate is
inert.

---

## F4 — The durable cost ledger has no attribution dimension

`llm_usage` carries exactly `{cost, calls, priced_calls, prompt_tokens, completion_tokens,
total_tokens, usage_id}` — no role, phase, lane, or node. So "where did the money go?" is
unanswerable from `events.jsonl`, from `looplab replay`, or from the `llm_cost` ledger. The
attribution *does* exist, but only in the trace sidecar as `generation.attributes.phase` — a 36 MB
`spans.jsonl` on `rubert-dr-0807`.

Measured from that sidecar:

**`rubert-dr-0807`** (live; 3 nodes created, 0 evaluated; 625 generation calls, 20.3 M tokens)

| phase | calls | tokens | share |
|---|---:|---:|---:|
| `inline_repair` | 115 | 4.45 M | **21.9 %** |
| `card_build` (the L5 speculative producer) | 118 | 4.18 M | **20.6 %** |
| `propose` | 130 | 4.01 M | 19.7 % |
| `stages` | 75 | 3.60 M | 17.8 % |
| `plan` | 69 | 3.32 M | 16.3 % |
| `triage` | 70 | 0.55 M | 2.7 % |
| `strategist_consult` / `train_monitor` / `foresight_rank` / `concept_coverage` / `evaluate` | 48 | 0.18 M | 1.0 % |
| **deep research** | **0** | **0** | **0 %** |

**`rubert-dr-0804`** (4,999 generation calls, 12.0 M tokens, **zero nodes evaluated**): `implement`
29.8 %, `plan` 18.8 %, `inline_repair` 16.9 % over **2,390 calls**, `propose` 16.8 %, `stages` 15.9 %,
`triage` 0.4 % over 2,350 calls.

Two readings the operator cannot get today without parsing a 36 MB file: the Developer's
build-and-repair chain (`stages + plan + implement + inline_repair + triage`) is **58–81 %** of the
spend on a repo task, and the Layer-5 speculative producer is a fifth of it on its own.
Adding a `phase` (or `role`) field to `llm_usage` is additive and reader-defaulted, so it costs
nothing under invariant 5.

---

## F5 — The Card scorer's exploration and foresight terms are blank on every selectable card

`card_score` (`search/card_selection.py:832`) ranks by `(band, exploration_key)` where the key is
`(primary, secondary, priority, confidence, novelty, coverage)`. Measured over **all 24 cards that
were ever `selection_ready`** anywhere in the corpus:

| field the scorer reads | cards where it is empty/None |
|---|---|
| `concept_tags` (→ `coverage`) | **24 / 24** |
| `novelty_verdict` (→ `novelty`) | **24 / 24** |
| `priority` (→ `_priority_signal`) | **24 / 24** |
| `confidence` (→ `_foresight_signal`) | **24 / 24** |
| `cross_run_prior` | 24 / 24 |
| `lesson_refs` | 24 / 24 |
| `research_origin` | 24 / 24 |

So on real data `coverage = 0.0`, `novelty = _UNGRADED_NOVELTY = 0.5`, `priority = 0.0`,
`confidence = 0.0`, and every eligible card ties on the whole key. What actually orders the queue is
`band` — `pinned` (never used), then *exact match against the legacy policy's own candidate action*,
then same-operator, then everything else — followed by the ascending card-id tie-break. **The Card
queue's discretionary ranking, in practice, re-selects what `policy.next_actions` already chose.**

This is not hidden: `engine/card_reservation.py:393-398` says so in the mint payload's own comment
("real selectable Cards reach novelty/coverage scoring empty. Persist bounded proposal-time scoring
receipts, or remove these terms from live ranking"). The measurement above is the confirmation that
it holds on every card the corpus ever made selectable. The Strategist's `card_scoring` treatment
(`stance`/`novelty_weight`/`coverage_weight`) is therefore also a no-op today: all three only
re-weight terms that are constant.

Related: `card_ranked` — the foresight board order, the only producer of `Card.priority` — fired
**2 times in 48 runs, in one run** (`live-stagnation`). `engine/audit.py:160` emits it only when
*every* ranked direction resolves to a live card, and `hypothesis_ranked` (75 rows, 6 after batch
expansion) only ever fired in three **pre-Card** runs. The two populations are disjoint in the whole
corpus.

---

## F6 — Card lifecycle paths that have never executed outside tests

Not defects — but the operator should know these are unexercised:

* **`card_added.source`**: only `researcher` (75) and `engine` (7). The `Card.source` vocabulary
  documents six values; `operator`, `foresight`, `novelty` and `freshness` have never been written.
* **Auto-drop reasons**: only `intra_batch_duplicate` (16) and `build_interrupted` (1). The
  three-turn retirement ladder (`_CARD_CLAIM_RETIRE_AFTER = 3`, `card_reservation.py:1133`) and the
  `reproposed` drop have never fired.
* **`card_build_done`**: all 23 rows carry exactly `{card_id, generation, node_id, speculative}` —
  the `skipped: producer_failed | stale` give-up path has never been taken.
* **Card `status` lane `coded`**: reserved and, as documented, never produced.
* **Operator Layer-6 controls** (`card_reprioritized`, `card_edited`, `card_resource_pinned`,
  `card_dropped`): 0 events.
* **`node_eval_started`**: 10 rows in 3 runs — correct (it is stamped only on speculative
  attempt-zero lifecycles), but it means the budget-refund proof has been exercised on 3 runs.

---

## F7 — Belief-board consolidation is off on the shipped default

The merge itself is **good**: `rubertlite-dr-unified-v4` folded 252 `hypothesis_added` into 54 cards
via 103 `hypothesis_merged` receipts, with aliases correctly absorbed into the surviving card.

But `hypothesis_merged` has **never fired in a `card_driven_selection=true` run** in the corpus (its
3 runs are `live-periodic`, `rubertlite-dense-retrieval`, `rubertlite-dr-unified-v4` — all with the
flag off). That is partly by design: `concurrent_consolidate` is documented as inert under Card mode
because `hypothesis_merged` becomes a Card ownership input, so the merge is deferred to the joined
main-task cadence. The consequence is measurable — `live-dr-check-0804`: 17 hypotheses added,
26 cards, **13 still open, 0 merges**, over 8 nodes. On the deep-research runs each memo registers up
to 5 directions as open beliefs, so a board that is never consolidated is the board the Researcher
reads on every proposal.

**Question for the owner:** under Card mode, does the between-nodes merge cadence actually get a
turn on a long-eval run? On `rubert-dr-0807` there were no `research_completed` rows to merge, so the
question has not been answered by any real run yet.

---

## F8 — The log health receipt and the log reader disagree

`runs/rubertlite-dense-retrieval/events.jsonl` has 1,624 physical rows.
`core/jsonlio.read_jsonl_lenient_with_health` reports `{read_complete: True, accepted_rows: 1624}` —
i.e. healthy. `EventStore.read_all()` returns **20 events**: the logical sequence jumps 19 → 25, and
`iter_event_jsonl` ends the recoverable prefix at a non-dense seq (`events/eventstore.py:343-344`).
**1,604 events — 99 % of the run — are invisible to replay, the UI, every export and every
cross-run projection, with no error anywhere.**

`log_divergence` (`eventstore.py:358`) *does* detect it and `EventStore.append` refuses to append
past it, so a live run fails closed. The gap is in the *diagnostic* surface: the health receipt an
operator or the UI would consult reports the file as complete. One line in `looplab replay` /
`looplab inspect` that calls `log_divergence` and names the boundary would close it. (This is an old
pre-Card run; I found no evidence the gap is currently reachable.)

---

## What is fine (and worth not re-litigating)

* **Card identity is the best-engineered part of the loop.** `_card_added_payload`
  (`card_reservation.py:342`) *proves* the mint is a fixed point of the exact rebuild the claim will
  perform — `_rebuilt_claim_idea` + `_card_action` are one shared spelling called from both ends of
  the round trip, so an un-revalidated Idea fails closed **before** a card exists rather than
  producing a card the claim can never match. `_fixed_point_idea` heals the common case beforehand.
  No unclaimable card appears anywhere in the corpus. The versioned digests (v1 / expanded-v1 / v2),
  `valid_card_action_digest` and the three receipt constructors are coherent, and
  `CardIdentityProvenance._coherent_identity` makes "native" un-forgeable from an id's spelling.
  Measured folded identity across 268 cards: 66 native (`card_added_receipt`), 186 `legacy_hash`
  (hypothesis shadows), 16 `card_added_unbound` (the deliberately receipt-stripped
  `_record_node_less_card` audit rows). Exactly the shape the design describes.
* **The budget and its refund do what `configuration.md:145` promises.**
  `node_counts_toward_card_budget` is genuinely the single predicate behind the L3 denominator, the
  policy's node universe and the fold's debug anchor. `rubert-dr-0805`'s budget receipt reads
  `{depth 2, requested 2, committed 2, discarded 2, refunded 1, charged_discards 1}` — the refund cap
  visibly doing its job; `live-0806-features` reads `{depth 1, requested 6, committed 6, evaluated 6,
  discarded 0, refunded 0}`.
* **Mint/claim atomicity holds.** 42 `[card_added, node_building]` batch envelopes across the
  corpus, zero torn, zero orphaned `card_added` from that path.
* **Attempt receipts work, and the defect they were built for is fixed.** `live-cards-0804` shows the
  old failure exactly as `_research_attempt_step`'s docstring records it (4 `research_attempted`,
  0 `research_completed`); `live-0806-features` two days later shows 2/2.
* **The inline-repair ledger is durable and the 2345-repair incident is closed.** `attempt`,
  `dep_rounds` and `full_retrains` are all re-derived from the log, and `_UNLIMITED_REPAIR_CEILING =
  50` bounds the `inline_repair_attempts=0` snapshots that 38 of the preserved runs carry. Verified:
  `rubert-dr-0804` is the only run in the corpus above 6 repairs on a node.
* **Crash triage is closed and correctly fail-closed.** All five verdicts are reachable; `unanswerable`
  and `unreadable` are engine-only, and `coerce_triage_action` explicitly refuses the literal
  `unanswerable` arriving from the wire, so a model cannot forge a run-level pause.
* **Stage manifest and stage reuse behave as documented.** Operator `cmd.stages` wins verbatim; a
  malformed operator list refuses rather than falling back to the Developer's; the engine appends the
  protected `score`. Reuse fired 3 times in the corpus (`train` ×2, `mine` ×1) with
  `status: "reused", seconds: 0.0`, and the eight-clause `_safe_reuse_start` gate is fail-closed.
* **The dependency allow-list is exactly 61 entries**, matching `configuration.md:504`.
* **Cadences and caps match their documentation** where I checked them: `deep_research_every=3`,
  `inline_repair_attempts=12`, `inline_repair_retrain_cap=2`, `_UNLIMITED_REPAIR_CEILING=50`,
  `_CARD_CLAIM_RETIRE_AFTER=3`, `_MAX_DEP_ROUNDS=6`, `asha_live_min_siblings=3`,
  `train_monitor_kill_confidence=0.8`.

---

## One number the owner should look at

The final protected `score` stage — the trust boundary the whole stage design is built around —
**fails 10 times and succeeds 3** across every `stage_finished` row in the corpus. Some of that is
the missing-`looplab_eval.py` bug a sibling agent is fixing right now. But `train` also reads 6 ok /
4 fail / 1 timeout / 1 check_failed. Across all 48 runs, **248 `node_evaluated` vs 47 `node_failed`
plus 2,414 `node_repaired`** — i.e. on repo tasks the loop currently spends most of its money getting
a node to produce *any* metric, not on searching. F4's phase breakdown says the same thing from the
cost side.

---

## Change landed with this audit

`engine/orchestrator.py::_spawn_research` now returns whether it started a research task, and both
`CardSession.research_spawned` writers in `engine/speculation.py` bind to that result instead of an
unconditional `True` / `bool(evals)`. This is F1(a) only — F1(b) (node-count vs time trigger) is a
product decision and was left alone. Tests:
`tests/test_research_overlap.py::test_spawn_research_reports_whether_it_actually_started_anything`
and `::test_card_session_binds_the_research_latch_to_the_spawn_result`, both proven red against a
`git archive HEAD` copy of the tree and green on this one.

## Docs that need editing (not done here — they are the owner's call on wording)

1. `docs/guide/configuration.md:351` and `docs/guide/concepts.md:569-574` — the Card queue owns
   selection only when `speculation_depth > 0`; say so.
2. `docs/23-hypothesis-card-kanban-2026-07-20.md:11-13, :753` — still say `speculation_depth`
   defaults to `0`; it has defaulted to `-1` (AUTO) since 2026-08-05. `:280` still checks off
   Layer 3 while `:20-27` says the scorer never sees a candidate.
3. `docs/guide/configuration.md:152, :155` — the reachability preconditions for both watchdog kills
   are stated correctly but read as footnotes to a `true` default. Consider stating the default as
   "on, but advisory-only unless <precondition>".
