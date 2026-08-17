# Operator-side audit — what LoopLab promises a human, and whether a human can get it

**Date** 2026-08-07 · **Scope** the run lifecycle a human drives, the reporting surfaces, governance,
and the UI's own claims. The search loop and cross-run memory are two sibling audits; they appear
here only where a surface exposes them.

**Method.** Every finding below was **measured against the shipped 48-run corpus**, not read out of
source. Two servers: `python -m looplab.cli ui --port 8846 --no-build --run-root
/home/jovyan/data/looplab/runs` (read-only over the real runs) and a second on `:8862` over a
`/tmp` copy for anything that mutates. Where a browser model owned the answer, the model was driven
directly with `node --test` over the real payload rather than inspected. `rubert-dr-0807` was live
throughout and was only read (716 events at the end, engine still up).

The ten items in `looplab-open-questions.md` are **not** re-reported. Where a finding touches one it
says so and stays on the part that is a defect rather than a decision.

---

## Ranked by cost to the operator

### 1. A run whose log has a sequence gap renders as a different, much smaller run — and every surface asserts the small numbers

`rubertlite-dense-retrieval` is the operator's largest retrieval run. On the same server, in the
same second:

| surface | says |
|---|---|
| `GET /api/runs/rubertlite-dense-retrieval/log-page` | `total_events: 1624`, `torn_tail: false`, last seq `1628` |
| `GET /api/runs/rubertlite-dense-retrieval/state` | `event_count: 20`, `seq: 19`, `max_seq: 19` |
| `GET /api/runs/rubertlite-dense-retrieval/lifecycle` | `event_count: 20` |
| `GET /api/runs/rubertlite-dense-retrieval/cost` | `{"cost":0.0,"calls":0,"priced_calls":0,"total_tokens":0,"recorded":false}` |
| run-list row | `phase: search`, `finished: false`, `nodes: 2`, `best_metric: 0.8077` |
| the log's own `run_finished` (seq 1193) | `{"reason":"aborted","finalization_required":true, …}` — it was finalized |
| the log's own `budget` event (seq 1195) | `nodes: 81` |
| the log's own `llm_cost` events (seq 265/276/1229) | 3497 calls, 76,869,684 tokens |
| the log itself | 230 `llm_usage` rows, 27 `research_completed`, 81 `node_created` |

Cause, and it is deliberate: `EventStore.read_all` fails closed at a sequence discontinuity
(`looplab/events/eventstore.py:992-1000`, the 5f011a2 dense-prefix fence). Seqs 20–24 are absent from
the file; line 20 is a plain `research_completed` row, so nothing expands to cover them. The fold
therefore sees a 20-event prefix and **everything derived from it is arithmetically correct on 1.2%
of the run.** The lenient reader disagrees and says so — `read_jsonl_lenient_with_health` returns
1624 rows with `read_complete: True`, `invalid_lines: 0` — which is why the timeline pager shows the
operator 1624 events and positively asserts `torn_tail: false` beside a state payload built from 20.

**This is the only run in 48 that trips it** — checked by comparing `EventStore.read_all()` against
`read_jsonl_lenient_with_health()` on every run in the root. The twelve other runs whose seqs look
gapped (`live-cards-0804`, `rubert-dr-0804`, …) are gapped only because a
`__looplab_event_batch_v1__` row carries several events under one line; those read to the end.
**A single-run defect is exactly the shape that never gets noticed**, and this run looks entirely
normal in the list.

The way out is worse than the state. There is no UI affordance at all, and the CLI's only remedy
destroys the run:

```
$ python -m looplab.cli repair-log /tmp/a4b6-runs/rlt-dr-copy
repaired …/events.jsonl: truncated at line 21 (kept 20 record(s), dropped 1603).
Original backed up to events.jsonl.corrupt-…bak. You can now `looplab resume` …
```

It is honest about the count and it backs up, but it is presented as *the* repair, there is no
renumber-and-keep option, and after it the copy folds to the same 2 nodes and no cost.

**What is missing is not the fence — it is the notice.** `read_all` already knows it stopped early;
nothing between it and the operator carries that fact. `/state`, `/lifecycle`, `/cost`, `/concepts`,
the run-list row, the report and the attention feed all present the truncated projection as the run.
The minimum honest fix is a `prefix_truncated_at_seq` field on the state/lifecycle payloads and a
`RunView` banner, in the same shape as the concepts endpoint's existing `completeness.reasons`.

### 2. The Deep-research drawer shows 7 of 57 memos, and drops all 575 claims

Measured by driving the shipped projection over the real payload
(`ui/src/researchMemoModel.js::normalizeResearchMemos`, run `rubertlite-dr-unified-v4`):

- `state.research` holds **57 memos, 1.60 MB**. The projection returns **7**, `omitted: 50`, in 1.2 ms.
- `MAX_MEMOS = 32` never binds. The binding limit is `collectionChars: 128_000`, spent after seven
  memos of ~20 KB each. So the cap that fires is the one the operator is not told about: the notice
  reads *"Showing 7 of 57 newest valid memos; older, malformed, or over-budget entries are omitted."*
- There is no pager, no "load more", no per-memo fetch, and no other screen renders a memo.
  **50 of this run's 57 deep-research memos are unreachable from the browser.** The operator's only
  route is `events.jsonl`.

Separately, and independently of the budget: `normalizeCollections`
(`ui/src/researchMemoModel.js:88-118`) reads `findings`, `recommended_directions`, `sources` and
`at_node`. **It never reads `claims`.** On this run that is **575 claims across 51 memos**, each with
a statement, `node_ids` and cited URLs — for example
`"The prior run's best result is 0.8835 recall@100 with DCL+threshold thr=0.05 + R-Drop alpha=0.5…"`
with two arXiv links. Those are the same rows that become `research_claims.jsonl` and the Atlas's
234 claims, so **an operator can read a claim portfolio-wide but never in the run that produced it.**
`RESEARCH_MEMO_LIMITS.claimChars` exists and is spent only on verifier-verdict statements.

What the drawer *does* render is good and should not be rebuilt: summary, findings, D8 verdicts with
an `N unsupported` chip, steerable `recommended_directions`, sources as safe external links, and
`reasoning` behind a `<details>`. The owner's "wall of text" complaint is addressed; **the residue is
volume and the missing claims tier**, not formatting.

Note for the record: `deep_research` — the event `narration.js:132` renders as
`'deep research requested'` — is the *operator request*, and it occurs **zero times in all 48 runs**.
The engine writes `research_completed`, which `narration.js:133` renders as
`deep research (auto) — <first 80 chars>`. The one-line narration is correct for a request; the memo
has a home. See finding 3 for why the request has never fired.

### 3. Two screens told the operator to type commands that do not exist — **fixed**

- `ui/src/panels.jsx:446` (Deep research, empty state): *"Trigger one with `/deep-research` in the chat"*.
- `ui/src/panels.jsx:684` (Queue): *"add one from the chat (`/experiment`)"*.

Neither is a command. The browser's entire slash vocabulary is two tables:
`assistantCommand.js::INTENTS` = {stop, pause, finalize, abort, resume, ratify, approve} and
`AssistantBar.jsx::NEW_RUN_DRAFT_RE` = `/^\/(?:new|genesis|run)\b/i`. Free text falls through to the
LLM assistant, whose toolset (`looplab/tools/machine_runs_tools.py::RunControlTools`) exposes
`finalize_run`, `stop_run`, `resume_run`, `reset_node`, `retag_node`, `set_run_concepts`,
`delete_node`, `delete_run`, `extend_budget`, `set_directive`, `set_trust_gate` — **no deep-research
and no inject**. Both sentences are printed at the one moment the reader has nothing else to go on:
an empty panel.

The control itself is real and reachable, just not from a browser. Measured on the scratch server:

```
POST /api/runs/live-nosignal/commands {"type":"deep_research","data":{},"expected_generation":"…"}
→ 200 {"status":"accepted","engine_policy":"ensure_running", …}   # engine spawned, event appended at seq 12
```

`api.js:250 CONTROL.deepResearch` exists and is imported by nothing in `ui/src`.

- `ui/src/PortfolioConcepts.jsx:297` printed `looplab governance concept-merge` in `<code>`. The Typer
  app is flat — 41 commands, no groups — and answers `No such command 'governance'.` at exit 2. Its
  own comment additionally named a `POST /api/cross-run/concept-alias` route that does not exist
  (the writes are `/concept-merge`, `/concept-purge`, `/concept-alias-clear`).

All three are corrected in this change, with two tests that go red on a revert (see *Fixes shipped*).

### 4. The Atlas's Concepts tile answers a different question from Runs → Concepts, by 3×

`GET /api/cross-run/atlas` returns `n_runs: 48`, `n_concepts: 25`. `ResearchAtlas.jsx:411-420` renders
those as adjacent stat tiles — **"Referenced runs 48" / "Concepts 25"** — directly above a section
headed **"Concepts seen across runs"**. A reader takes that to mean 25 concepts observed over 48 runs.

Measured, it is two populations glued together:

- `n_runs` is the union of capsule runs and lesson-cited runs (`claims_retrieval.py:704-710`) = 48.
- `n_concepts` comes from `concept_capsules.jsonl`, which holds **exactly 3 rows**:
  `live-deps4-0804` (10 concepts), `slow-auto` (12), `rubert-dr-0805` (3). `slow-auto` is not in the
  run root at all. Every concept in the CLI's listing is observed `1×`.
- Meanwhile `Runs → Concepts` (`PortfolioConcepts`) folds the run-list rollup and sees **16 tagged
  runs of 48 and 77 distinct concept ids** — measured off `/api/runs`.

Nothing discloses the gap. `EvidenceSourceNotice` (`ResearchAtlas.jsx:63-71`) returns `null` because
`concept_source.source_complete` is `true` — but that flag means *the capsule store read cleanly*,
which is completeness of a 3-row file, not coverage of a 48-run portfolio. The CLI repeats the claim
verbatim: `Research Atlas: 48 run(s), 25 concept(s), 236 claim record(s), 0 mixed-evidence`.

This is the same defect that removing `engine/concept_capsules.py::portfolio_concept_graph` fixed for
the run list (3 rows vs 15 tagged runs, per `CLAUDE.md`'s `ui/` row). **The Atlas kept the capsule
reader.** The Atlas's real, non-duplicated value is the claims side — 234 claims, contested/mixed
evidence, the curation log — and that answers the owner's *"why does the Atlas exist?"* far better
than a concept tile that contradicts the Concepts view. Either scope the tile's label to its
population ("Concepts from 3 run capsules") or drop it and let Runs → Concepts own that question.

### 5. Nothing anywhere reports an inline-repair count — and the corpus's worst run is 2345 of them

`rubert-dr-0804` is 106 MB and 2678 lines. Of those, **2345 are `node_repaired` on a single node**,
the last at `attempt: 2341`, every one carrying
`"(developer error: LLM request to https://openrouter.ai/… failed…"`.

`_on_node_repaired` (`looplab/events/replay.py:1024-1046`) updates code, files, deletions and
footprint. It records no counter, and `Node.attempt` is the *reset* generation, not the repair
attempt. So the folded node reads:

```
node 1  status='pending'  attempt=0  operator='draft'
```

The run-list row says `search · 1 node · no best`. The Inspector says pending, attempt 0. The only
evidence that this run burned 2345 LLM round-trips is 2345 lines in the timeline — which is precisely
the wall of text the operator objected to. `/api/runs/{id}/log-page` copes honestly (`total_events:
2680`, byte-limited 5-event pages in 0.7 s), but a raw log is not a report.

This is **not** open question #10. That one is about converged and futile runs; this is a repair
runaway, and the missing piece is an operator-visible count (`repairs` on the node, and a
`node_repaired` roll-up in the timeline) rather than a stop policy.

### 6. There is no portfolio cost figure, and 87% of calls carry no price

Swept all 48 runs through `/api/runs/{id}/cost`:

```
runs=48  runs with a roll-up=35  calls=7454  priced_calls=983 (13.2%)
total_tokens=148,814,155  total known cost=$18.08
runs with any priced call: 8
```

Two separate things, and only one is a defect.

**Not a defect — this is the model.** `ui/src/format.js::costPricing` distinguishes "$0 because no
calls" from "unpriced" from "`$X+`, a floor", with tooltips that name the priced/unpriced split, and
`Inspector.jsx:1996-1997` refuses to rewrite an absent `priced_calls` into a hard `0 of N`. Verified
against three real payloads (`b2-validate` 148 calls / 0 priced; `rubert-dr-0804` 313 / 209 →
`$8.26+`). Every other numeric surface in the product should be held to this standard.

**Defect.** Nothing sums it. `RunCompare` has a per-run column; the Report has a per-run line; the
scope report has none. An operator with 48 runs and at least $18 of measured spend has **no screen
that answers "what has this lab cost"**, and no per-project total either. This is adjacent to open
question #6 (there is no dollar *budget*) but is not the same call: a roll-up is reporting, not
policy, and the per-run numbers already exist.

### 7. Governance has no screen at all, and the one screen that mentions it says the loop does not close

Concept merge / split / purge / alias-clear, the concept and claim stewards, `concept-ratify`,
`claim-decide`, `task-facets(-set)` — **all CLI + HTTP, zero React callers.** On this corpus the
ledgers are not empty: `/api/cross-run/curation-log` returns 4 concept rows and
`/api/cross-run/claim-curation-log` 186 claim rows, and the Atlas renders them read-only under
"Recent proposals + outcomes". There is nowhere to act on one.

Worse, `PortfolioConcepts.jsx` correctly tells the operator the governed merge **will not change what
they are looking at**: the canonicalization is behind `GET /api/cross-run/concept-policy` and nothing
in `ui/` reads that route, so a merged pair still draws as two roots and the drift notice still calls
it drift. That sentence is honest — and it is a standing admission that the product's only lever over
the shared taxonomy is a terminal command whose effect is invisible in both screens that draw the
taxonomy. Reading `concept-policy` in `conceptForest.js` would close it; the payload is already
shaped for a browser caller (`canonical`, `split_sources`, `alias_revision`).

### 8. CLI, HTTP and UI disagree about which lifecycle verbs exist

Full three-column inventory was taken; the asymmetries that cost an operator something:

- **CLI has 7 run verbs** — `run`, `resume`, `stop`, `finalize`, `approve`, `replay` (read-only fold),
  `repair-log`. There is **no CLI reset/start-over, delete, rename, fork, inject, node-abort,
  node-reset, budget-extend, restart or reopen.** A headless operator cannot recover a run the way a
  browser operator can.
- **UI-only**: `restart` (`ConfigPanel.jsx:571`, "Pause & resume ▸"), start-over
  (`POST /api/runs/{id}/reset`), delete, rename, project/super-task assignment.
- **HTTP-only, no caller in either UI or CLI**: `deep_research`, `set_strategy`, `promote`,
  `annotation`, `force_confirm`, `inject_node`, `run_reopened`, `POST /api/start/{id}/resolve-claim`,
  `POST /api/runs/{id}/resolve-activity-claims`, and the whole boss chat plane (`/chat`, `/suggest`,
  `/chat-log`, `/chat-compact`, `/command`) which only the TUI drives. Seven of these are declared in
  `api.js` and imported by nothing.
- **`looplab replay` is a name collision.** It is a read-only fold. The UI's "Start over" / round-7
  "Replay" is `POST /reset`, which archives a generation. Same word, opposite consequence, and the
  CLI has no command for the destructive one.
- **`budget_extend` means different things per column**: the CLI sets budgets only at launch
  (`--max-nodes`, `--max-seconds`), the legacy `/control` route documents an additive `add_nodes`
  delta, and the UI sends only an absolute `max_eval_seconds` ceiling.
- `DELETE /api/runs/{id}` always 409s — measured — with
  `{"code":"deletion_identity_required","remediation":"POST /api/runs/{id}/deletions …"}`. That is a
  correct, self-explaining refusal, not a defect.

### 9. The champion notebook is CLI-only, and the code claims otherwise

`looplab export-notebook /…/b2-validate --out x.ipynb` works (4089 bytes). **Zero occurrences of
"notebook" or "ipynb" anywhere in `ui/src`**, and no HTTP route builds or serves one. The Report
offers Markdown, `solution_nodeN.py` and a model-card JSON — not the artifact
`looplab/events/notebook.py:1-3` calls "the artifact data scientists actually want to take away",
whose docstring says it "works headless **or from the UI**". It has never worked from the UI.

### 10. A symlinked run directory disappears with no explanation

Measured: a symlink placed in the run root is excluded from `/api/runs` entirely, and every route
answers `404 {"detail":"no such run"}`. This is `serve/engine_proc.py::_engine_liveness` +
`core/pathsafe.is_reparse` failing closed, which is right — but an operator who symlinks a run onto a
bigger disk sees it silently vanish. One line of copy in the run list's empty state would cover it.

---

## What is fine

One line each, all verified against real runs rather than read.

- **Finalization-stalled is recoverable, from both planes.** `live-deps4-0804` sat at
  `phase=finalizing, finished=true, engine_running=false, finalization_incomplete=true`. On a copy,
  `looplab finalize` returned it to `finished / not incomplete`, and its degradation note named
  exactly which artifacts the unreachable model cost and which land anyway. The UI reaches the same
  command through `Dock.jsx:1259` "Reattach finalization" and the `finalization-stalled` empty state.
  **[corrected 2026-08-17] The second sentence was wrong, and it is the one bullet in this section
  that was read rather than verified.** "Reattach finalization" submits a durable `run_abort`, which
  ATTACHES to a finalize request already on the log; `live-deps4-0804` finished naturally and had
  none, so the control was rejected `command_intent_missing` on exactly the run this bullet cites.
  Driven in `tests/test_stalled_finalization_affordance.py`; the fix and its measurement are in
  `docs/BACKLOG.md` §0.11.
- **Every state I could reach offers an exit.** paused/stalled → Resume + Finalize; finished → Resume
  + Start over; grounding with a dead engine → stalled → Resume/Finalize/Events; approval → a
  dedicated topbar (`RunView.jsx:2599`) naming the exact `/approve #N`, shown whether or not the DAG
  is empty.
- **`runIndex.js::dagEmptyPresentation` is the reference honest empty state** — seven distinct causes,
  each with its own copy, actions and `aria-live` politeness, and it refuses to call a recoverable run
  "empty".
- **The attention feed derives what it claims**: `finalization_stalled` for `live-deps4-0804`,
  `stalled` for four dead engines, each with `derived`/`browser` provenance and the exact seq.
- **`format.js::costPricing`** — see finding 6. This is the standard the rest of the UI should meet.
- **`looplab timings`** reconciles against wall clock and *names the residual*: `live-cards-0804`
  attributed 4.7 min (56%), traced 4.7 min, untraced 3.7 min (44%).
- **`/api/runs/{id}/concepts`** carries `status`, `authority`, `provenance.membership_counts` and
  `completeness.reasons`; `b2-validate` correctly self-reports `partial` /
  `delta_dependency_unknown_parent_membership`. "Concepts UNAVAILABLE" is fixed.
- **The three memory tiers have their blurbs** (`panels.jsx:1814 MEMORY_TAB_PURPOSE`), each naming who
  reads that tier back — the distinction an operator can act on. That item is done.
- **`/api/runs/{id}/log-page`** is cursor-paged and byte-limited with `torn_tail`,
  `source_tail_limited` and `total_events` explicit; the 2680-event / 106 MB run pages in 0.7 s.
- **Review links fail closed and say why**: `POST /api/runs/{id}/reviews` → `409 read-only sharing
  requires LOOPLAB_UI_TOKEN…`, and `CollabPanel::createFailureCopy` renders that exact case instead of
  a generic error.
- **Paid-work surfaces never present an unconfirmed action as done** — the report refresh, scope
  report and concept lens each carry a full uncertain / resume-same-request / abandon vocabulary.
- **Run-list membership is right**: 48 of 67 run-root entries; the 19 excluded have no `events.jsonl`.

---

## One correction to the brief

The bundle budget is not un-run by CI. `.github/workflows/tests.yml`'s `ui` job has run
`npm test` → `npm run build` → `npm run check:bundle` since `e1e5c218` (2026-07-14). So the job is
**red, not absent** — `node ui/scripts/check-bundle.mjs` against the shipped dist reports 13
violations, headed by

1. `[manifest_cycle] _4djirT.js -> _C0CBra.js -> _4djirT.js` — which the checker itself says makes the
   remaining measurements untrustworthy, and
2. the owner run-DAG route at **362.0 KiB JS gzip against a 260 KiB budget (102 KiB over)**, plus the
   Concepts route 86.9 KiB over and the total bundle 141.1 KiB over.

Adding a second CI step would be the wrong fix; the step exists and is failing.

---

## Fixes shipped in this change

Small, confident, and each proven red on a revert of the production edit.

| file | change |
|---|---|
| `ui/src/panels.jsx:446` | the Deep-research empty state no longer offers `/deep-research`; it names the `deep_research_every` cadence (Config → save → resume), which is the only browser-reachable trigger, and says there is no one-off trigger yet |
| `ui/src/panels.jsx:684` | the Queue empty state no longer offers `/experiment`; it names the graph node menu's own "Explore from here" / "Merge with…" |
| `ui/src/PortfolioConcepts.jsx:297` | `looplab governance concept-merge` → `looplab concept-merge`; the surrounding comment's `/api/cross-run/concept-alias` corrected to `/concept-merge` |
| `ui/test/portfolioConceptsScopeTruth.test.js:134` | the same two wrong spellings in its comment |

New guards, both of which **drive the real vocabulary** rather than pinning a literal:

- `tests/test_ui_named_commands.py` — every code-formatted `looplab <cmd>` under `ui/src` must resolve
  in `looplab.cli.app.registered_commands`. Red before the fix with
  `PortfolioConcepts.jsx:287/297 names \`looplab governance\` … not a registered command`. A second
  test proves the scan is not silently empty.
- `ui/test/uiNamedSlashCommands.test.js` — every slash command printed inside `<code>` must be
  accepted by `assistantDirectIntent` or by `AssistantBar`'s own `NEW_RUN_DRAFT_RE` (read out of its
  source and executed, so a rename goes red). Red before the fix on `/deep-research` and
  `/experiment`; green on the five real ones (`/stop`, `/finalize`, `/resume`, `/approve`, `/new`).

Both edited JSX modules were SSR-compiled (`vite.ssrLoadModule`) and the 25 tests across
`portfolioConceptsRender` / `portfolioConceptsScopeTruth` / `portfolioUsability` / `panelsBarrel`
pass. (Those files need well over 60 s of node timeout in this container — a pre-existing property,
confirmed by re-running them against the un-edited tree.)

---

## Suggested order of work

1. Surface the `read_all` dense-prefix truncation (finding 1). It is the only defect here that makes
   the product state a wrong number with full confidence, and it is one field plus one banner.
2. Render memo `claims`, and give the Deep-research drawer a pager (finding 2). 50 of 57 memos and
   575 claims are currently write-only.
3. Decide what the Atlas's concept tile is for (finding 4) — scope its label to the capsule
   population, or delete it and let Runs → Concepts own the question.
4. A repair counter on the node and a `node_repaired` roll-up in the timeline (finding 5).
5. A portfolio/project cost roll-up (finding 6).
6. Read `GET /api/cross-run/concept-policy` in `conceptForest.js` so a governed merge finally changes
   the tree it governs (finding 7).
