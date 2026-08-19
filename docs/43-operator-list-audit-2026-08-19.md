# Operator list — captured and audited, 2026-08-19

The operator keeps this list in their own notebook and has sent it several times. Most of it was
recorded nowhere. This document is the capture, in [doc 29](29-operator-backlog-2026-08-11.md)'s
format: **what was asked** (their words, kept in Russian so nothing is lost in translation), **what
the code does today** with the site that proves it, whether it is a **BUG** (it used to work, or a
document claims it works, and it does not) or a **FEATURE** (it never existed), and what closing it
would take.

This is an audit, not an implementation pass. Nothing here was fixed. Every status was read off the
tree at `master` `1e47472f` on 2026-08-19.

**Doc 29 is not superseded.** It is the record of the 2026-08-11 session and keeps its own dated
integrity. Eight of the items below already have an entry there (F1–F8) and those entries are
**re-verified** here rather than restated — where the re-verification changes the status, this
document says so and doc 29's entry is the historical record of what was believed on the day.

---

## 0 · Read this first: what the operator is actually looking at

Before any item below is debugged, this section decides which of them are even about the code on
`master`. **They are largely not.** Three independent pins, all measured on 2026-08-19:

| what | pinned to | measured |
|---|---|---|
| the browser bundle | a build from **2026-08-15 01:41 UTC** (master `83a091b5`) | `ui/dist/index.html` mtime; `ui/dist/assets/*.js` same second |
| the UI **server** process | master **`499ba72d`** (2026-08-17 12:25 UTC) | pid 727475, started 2026-08-17 12:54:32, `python -m looplab.cli ui --port 8792 --no-build` |
| the **live engine** run | master **`fbbab725`** (2026-08-17 16:08 UTC) | pid 1851519, started 2026-08-17 16:38:12, `looplab run /home/jovyan/data/e5small-v2-task.json` -> `runs/e5small-dr-unified-v2` |

Against those pins:

* **19 commits have touched `ui/src` since the bundle was built** (newest `5eeab953`, 2026-08-18
  15:24 UTC). None of them is in the JavaScript the operator's browser executes. `--no-build` is
  documented as exactly this — *"serve the last good bundle"*
  (`looplab/cli/ui_cmds.py:65`, and the flag's own docstring at `:54`).
* **16 commits have touched `looplab/serve` or `ui/src` since the server started**, and 28 have
  touched `looplab/` since the engine run started.
* The worst shape this produces is a **half-deployed fix**, and one is live right now.
  `3492bfad` (2026-08-17 09:34 UTC, *"perf(trace): optimize S3 reads and page early history"*)
  changes both halves of the trace reader — server (`events/span_index.py`,
  `events/traceview.py`, `serve/appstate.py`, `serve/routers/runs.py`) and client
  (`ui/src/Inspector.jsx`, `ui/src/api.js`, `ui/src/traceEpisodeModel.js`). It **is** an ancestor of
  the running server's pin and it is **not** in the running bundle. So the operator is driving the
  *old* client against the *new* server: the pager that would fetch earlier history does not exist
  in the page, while the route that would serve it does.

**This is cheap to close and it is not a code change.** `looplab/serve/server.py:242-261` mounts the
dist through `StaticFiles(directory=...)`, which reads from disk per request, and `:686` serves
`index.html` with `Cache-Control: no-cache` while the hashed assets are immutable. So
**`looplab build-ui` alone republishes the client to the running server with no restart**; only the
16 server-side commits need the process bounced.

> **Rule this establishes, and it is the one to keep:** a symptom reported against this box is not
> evidence about `master` until the bundle's mtime and the server/engine process start times have
> been read. Four days of UI fixes are invisible to the operator by construction, and "опять ушла
> кнопка" — *the button went away again* — is exactly what a stale bundle looks like from the
> outside.

---

## 1 · Navigation and the data model (N1–N8)

### N1 · "надо из концептов уметь переходить в память, заметки, кейсы, знания и тд"

**Today — the join renders and it is not navigation.** The surface is the run list's **Concepts**
view (`ui/src/PortfolioConcepts.jsx`, mounted `ui/src/RunList.jsx:2767`, deep-linkable `#/concepts`
via `ui/src/App.jsx:49`). `_ConceptMemory` (`PortfolioConcepts.jsx:109-134`, mounted `:184`) reads
`GET /api/memory` once, lazily, after a concept is selected (`:228`); the route is
`looplab/serve/routers/misc.py:1957` with the three tiers allow-listed at `:1980-1982`
(`cases.jsonl`, `lessons.jsonl`, `meta_notes.jsonl`) and concept stamping via `build_shelf`
(`misc.py:2002` → `looplab/engine/concept_shelf.py:229`).

Three things are true at once:

1. **Doc 29 F7's claim is accurate for the day it was written.** The join landed in `f4c4f4bb`,
   2026-08-12 — *"feat(ui): from a concept into what the lab actually learned about it"* — and F7's
   measurement is dated 2026-08-12.
2. **It is an excerpt, not a link.** `PortfolioConcepts.jsx:125` emits
   `<span>{String(row._text||'').slice(0,220)}</span>` plus a muted `task_id`. Twenty lines below,
   at `:175`, the run evidence list DOES get a clickable `pc-run-link`. So the operator can see that
   a lesson exists and cannot go to it — which is exactly the verb in the ask ("переходить").
3. **`knowledge` is excluded by design** (`ui/src/conceptMemoryModel.js:16-21`: "knowledge is
   deliberately absent — it is human-authored and carries no run concepts"), and **the in-run
   Concepts view has no memory join at all** — `ui/src/ConceptView.jsx` (1,473 lines) contains zero
   references to `/api/memory`, `conceptMemory`, `lessons` or `cases`.

**Measured** (read-only, `/home/jovyan/data/looplab-memory`): 86 rows across the three tiers, of
which **22 carry any concept** — 20/28 lessons, 1/29 cases, 1/29 notes, and both the case and the
note belong to `rubertlite-dr-unified-v8`. So for most concepts the panel shows lessons only.

**Verdict — BUG (a half-built affordance), plus a FEATURE.** To close: make each row an anchor into
`#/memory` with a preselected tier + concept (`PortfolioConcepts.jsx:121-131`), add a memory-panel
route parameter so the target can open on a row, and decide whether knowledge notes should be
concept-taggable at all. The in-run Concepts view needs the same join or an explicit statement that
it is run-scoped.

### N2 · "во всех этих вьюшках сортировать по дереву концептов (это как вариант отображения)"

**Today — SHIPPED for three of the four memory tiers, and the operator's phrase "как вариант
отображения" is exactly what was built.** `MemoryPanel` (`ui/src/panels.jsx:2074-2286`) carries a
shared concept axis (`:2086-2087`, rendered by `ConceptShelfBar` `:2014-2050`) and a real
**List / Concept tree** display toggle (`:2033-2036`) grouping through `groupByConceptTree`
(`:2133`) into `ConceptGroup` headings (`:2055-2072`), applied to lessons (`:2219`), cases (`:2247`)
and notes (`:2268`). Landed `d75b2fe9`, 2026-08-12.

**What is not there:**

| view | sort / grouping today |
|---|---|
| Memory → Lessons / Cases / Notes | concept filter + flat/tree. **No sort control at all** |
| Memory → **Knowledge** | nothing — excluded at `panels.jsx:2122` (`tab !== 'knowledge' && !!conceptIndex`) |
| Authoring (prompts/skills/knowledge) | no sort anywhere (`panels.jsx:942-1960`) |
| Claims & Curation | none; snapshot order, deliberately (`claimsCurationModel.js:301`) |
| Run list | 6 keys and **no concept key** — `LIST_SORT_KEYS = {time,name,metric,task,nodes,phase}` (`ui/src/RunList.jsx:62`), comparator `ui/src/runIndex.js:491-516` |

Row order inside every memory tier is store order (`"projection": "bounded_recent_tail"`,
`misc.py:2004`).

**Verdict — mostly SHIPPED; two concrete gaps**: the Knowledge tab has no concept axis
(`panels.jsx:2122` plus a knowledge concept index), and the run LIST has no concept sort key
(Concepts is a separate fourth representation at `RunList.jsx:65`, not a sort mode). Note that "no
sort at all" in the memory tiers is a separate and probably larger complaint hiding inside this one.

### N3 · "в общей карте уметь отображать не только раны, но и память, заметки, кейсы и тп"

**Today — FEATURE, never built.** The "общая карта" is the run list's `map` view, labelled
**Lineage** (`ui/src/RunList.jsx:65`, label `:2464`). Its projection is
`buildGraph` at `ui/src/MapView.jsx:96-189`, and its complete population is:

* `type: 'run'` — one per run (`:128-129`)
* `type: 'projSuper'` — a collapsed project cluster (`:138`, `:150`)
* `type: 'projRegion'` — a project hull (`:168`, `:176`)
* edges — **only** run→run lineage from `run.seeded_from` (`:182-187`)

No lesson, case, note, claim, capsule or concept node exists in that graph. The second candidate —
what the backend itself calls "the GLOBAL concept map" (`looplab/serve/routers/cross_run.py:648`) —
is `ui/src/conceptForest.js::buildConceptForest` (`:155`) driving `PortfolioConcepts.jsx`, and it
folds ONLY the run array's `concepts` rollup (`conceptForest.js:187-197`). Memory appears there
only inside the per-concept detail pane (N1), never as map population.

**Verdict — FEATURE, and the operator's own instinct ("это надо прям посмотреть как красиво
сделать") is right, because the blocker is not drawing.** It is anchoring: **64 of 86 memory rows
carry no concept**, and **28 of 29 cases carry no `run_id`** to join on. Plot them today and most
of the new population lands unattached to anything. **The prerequisite is N1's data problem, not
`MapView.jsx`.**

### N4 · "Кейсы вообще не понимаю зачем нужны."

**This has a complete written answer already, and the operator has evidently never been pointed at
it: `docs/guide/memory.md:120-144`.** The audit confirms every line of it against the tree.

**One writer.** `looplab/engine/lessons.py:392-423::store_case(final)`, called from
`looplab/engine/finalize.py:736` via `orchestrator._store_case` (`orchestrator.py:4854`) — one case
per run end, only when `final.best()` is not None.

**Schema** (`lessons.py:401-419`, validated by `valid_case_record`, `looplab/engine/memory.py:868-891`):
`{task_id, goal, direction, fingerprint, params, metric, rationale, run_id, run_uid, concepts}` plus
`active`. Deliberately **unversioned** — any row carrying `v` or `record_kind` is quarantined
(`memory.py:874`), so it cannot be migrated in place, only rewritten under a new filename.

**Store.** `JsonlCaseLibrary` (`memory.py:894-1020`), upsert keyed by `(task_id, direction)`,
retain-on-improvement (`_add_locked`, `:943-997`). The vector-backed `CaseLibrary` (`memory.py:1125`)
is explicitly unwired ("Nothing under `looplab/` constructs this", `:1128`).

**Readers — three, and only one can reach a model.**

1. `agents/factory.py:152-161` hands `cases.jsonl` to `KnowledgeTools`, which embeds each case into
   the **same `kb` index as the knowledge notes** (`tools/knowledge_tools.py:304-345`). A case
   therefore reaches an agent only if a tool-using role calls `kb_search` (`:415`) AND the case wins
   a top-3 semantic ranking against every knowledge note.
2. `/api/memory` → the Memory panel table (`panels.jsx:2242-2244`).
3. `JsonlCaseLibrary._reload`, so `add` can compare metrics.

`docs/guide/memory.md:127` states it flatly and grep confirms it: **"`JsonlCaseLibrary.search()` and
`.all()` have no production call sites at all."**

**Against the neighbouring kinds** — a **lesson** is injected into the next Researcher/Developer
prompt and is fingerprint-matched, so it transfers across SIMILAR tasks; a **meta-note** is injected
verbatim into the next run of the SAME task; a **case** is injected into nothing and is keyed by
exact `task_id`, so it cannot fuzzy-transfer at all. Its one genuine property is that **it is the
only place the winning `params` survive in structured form** — the artifact a human can paste into a
re-run.

**Measured on this box, 2026-08-19 (read-only).** `/home/jovyan/data/looplab-memory/cases.jsonl`:
**29 rows, 28 of them `toy_quadratic`** (the smoke test, all `run_id: "run"`), **exactly one real
case** (`repo_task`, `rubertlite-dr-unified-v8`); 2 active; 1 of 29 carries `concepts`; and that one
real case's `fingerprint` is 76 raw goal tokens (`"anything"`, `"before"`, `"jovyan"`, `"not"`, …) —
lexical noise, not a retrieval key. `~/.looplab/memory/cases.jsonl`: 22 rows, 100 % `toy_quadratic`,
0 with concepts.

**Verdict — not a bug: a kind that costs a write per run and is read by essentially nothing.** The
operator's question is well-founded and the answer is a DECISION, which `memory.md:139-144` already
prices: folding cases into meta-notes costs (1) an un-migratable store, (2) `KnowledgeTools` losing
its only non-note corpus, changing `kb_search` for every existing memory dir, and (3) the structured
`params`, which prose does not replace. **Recommendation for the owner: keep the record, drop the
kind** — write the winning params onto the meta-note that already reaches the prompt, and stop
maintaining a second store whose search has no callers.

> **SUPERSEDED the same day — the recommendation above was drawn from a corpus that is 29/30 toy.**
> It is kept as the record of what this audit believed. On the ONE real row the premise does not
> hold: `rubertlite-dr-unified-v8`'s meta-note is a causal narrative naming ONE hyperparameter
> (`R-Drop … at α=0.5 … lifted recall from 0.7384 to 0.762`) while its case carries fifteen beside
> `metric 0.762048` — so "the note already says the same thing in prose" is true of `toy_quadratic`
> (two params, inline) and false of every real run. A second measurement made the case for fixing
> rather than folding: the one reader a case had never delivered the params either — a `kb_search`
> hit is head-clipped at 600 chars and the record led with the `goal`, so `best params=` began at
> char **691** of that 1,610-char record and what arrived was 600 chars of the reader's own task
> prompt. Both are now closed: `engine/lessons_priors.py::_scan_prior_context` reads the active case
> beside the meta-note it already reads, under the same `(task_id, direction)` key and the same
> fail-closed `LessonScope`, into one Researcher-only prior line; and the `kb_search` record leads
> with its params. See `docs/guide/memory.md` § *What are cases for?* and
> `tests/test_case_store_wiring.py`, which drives both on `tests/data/v8_case_and_note.json` — that
> run's own two rows, verbatim.

### N5 · "Надо подписи в UI чтобы понимать чем отличаются все виды памяти"

**Today — SHIPPED for the seven kinds that have a UI; absent for the other nine.** The authoritative
list is a 16-row table at `docs/guide/memory.md:40-56`; in code the nearest enumerations are
`serve/memory_cascade.py:544-550` (`CASCADED_TIERS`: lessons, meta_notes, cases, research_claims,
concept_capsules) and `:52-55` (`PRESERVED_TIERS`: skills, curation_logs), plus the three authoring
kinds at `serve/routers/misc.py:461`.

Labels that exist: `MEMORY_TIER_BLURB` (`ui/src/conceptShelf.js:147-158`, rendered
`panels.jsx:2149`) — one paragraph each for lessons/cases/notes/knowledge, and its own comment at
`conceptShelf.js:143-146` cites the operator's two complaints; `MEMORY_TAB_PURPOSE`
(`panels.jsx:1946-1962`, rendered `:2176`), a deeper per-tab paragraph keyed on WHO READS IT BACK —
the Cases entry says outright *"Not pasted into any prompt: an agent sees a case only if it calls
`kb_search`"*; the cross-surface orientation at `panels.jsx:2162-2172`; and
`AUTHORING_KIND_PURPOSE` (`panels.jsx:917-940`, rendered `:1835`). Landed `d75b2fe9`, 2026-08-12.

**Missing:** claims (`ClaimsCuration.jsx` has section headings only, `:461`/`:486`/`:508`), concept
capsules, exploits, task facets, steward proposals, cards (a per-chip `title` attribute only,
`CardBoard.jsx:301`, `:344`) — and **concepts themselves**: `PortfolioConcepts.jsx` explains
coverage and spelling variants but never says what a concept IS.

**Verdict — SHIPPED for four memory tabs + three authoring tabs; FEATURE for the rest.** Note the
interaction with §0: these blurbs landed 2026-08-12 and ARE in the running bundle, so if the
operator still cannot tell the kinds apart, the labels are not being found rather than not existing.

### N6 · "Чем авторинг от мемори отличается? почему нет скиллов и промптов? че это?"

**Today — the distinction is SHIPPED and printed on both panels; the empty tabs are a DEFAULTS
problem, not a missing feature.**

* **Authoring** = `panels.jsx:942::AuthoringPanel`, kinds exactly `['prompts','skills','knowledge']`
  (`:1818`), backed by `GET /api/{kind}` (`serve/routers/misc.py:461`, dirs resolved `:1046-1047`).
* **Memory** = `panels.jsx:2074::MemoryPanel`, backed by `/api/memory` + `/api/knowledge`.
* **The boundary is authorship direction and both panels say so** — `panels.jsx:1829-1833` ("**You**
  write these Markdown files; agents read them during runs. Run-written lessons, cases and
  meta-notes live in **Memory**") and its mirror at `:2166-2171`; also `docs/guide/memory.md:145-161`.
* **The overlap is exactly one directory**: `knowledge`, writable in Authoring and read-only in
  Memory — the same files (`panels.jsx:936-938`).

**Why there are "no skills and prompts":** `skills_dir: str | None = None` (`core/config.py:2000`)
and `prompt_dir: str | None = None` (`:2002`), while `memory_dir` (`:1201`) and `knowledge_dir`
(`:1902`) default under `~/.looplab/`. So those two tabs render `no prompts dir configured` /
`no skills dir configured` (`panels.jsx:1820`) — **indistinguishable from "not built"**, which is
precisely how the operator read it. Confirmed on this box: `~/.looplab/knowledge` does not exist,
and `/home/jovyan/data/looplab-memory/skills/` exists but is empty and is not `settings.skills_dir`
anyway.

**A second, real gap, and it is not hypothetical:** auto-distilled skills
(`<memory_dir>/skills/auto-*.md`, written by `engine/memory.py:757::write_auto_skill`) are visible
in **neither** panel — `/api/memory` serves only cases/lessons/notes and `/api/skills` resolves
`settings.skills_dir` (`docs/guide/memory.md:157-161`). This box has written 27 of them: they are
sitting in `/home/jovyan/data/looplab-memory/skills.quarantined-2026-08-12/`
(`auto-mean-merge-of-nodes-0-1.md`, `auto-perturb-best-node-5-metric-4-83980328.md`, …), all from
toy runs, and the live `skills/` directory beside them is empty. Nobody has ever seen them in a
UI.

**Verdict — FEATURE, cheap: default `skills_dir`/`prompt_dir` to `~/.looplab/{skills,prompts}`
(`config.py:2000-2002`), and give auto-skills a review surface.** An empty tab must say "no
directory configured — here is how", not render as an unimplemented screen.

### N7 · "Надо отделить менюшку рана от менюшки всего луплаб"

**Today — SHIPPED, with one deliberate documented overlap.** The split is defined once in
`ui/src/globalNav.js` (rule stated in its header, `:1-10`): `GLOBAL_DESTINATIONS` (`:14-44`) =
Runs, Claims & Curation, Cross-run memory, Knowledge & prompts, Host & GPU, Settings, rendered by
`ui/src/GlobalMenu.jsx:46-108` off the brand mark. The run menu is `ui/src/RunView.jsx:2736-2777`
over four hubs (`HUBS`, `:175-180`: Progress, Trust, Analysis, Lab) plus a `Run settings` button
explicitly renamed to disambiguate it from global Settings (`:2779-2784`). The three run-id-less
panels (`memory`, `authoring`, `gpu`) moved to the LoopLab side (`globalNav.js:49-61`) while staying
in `RUN_ROUTE_PANELS` (`ui/src/runRouteState.js:13-17`) so old bookmarks resolve. Landed `b937f1a2`,
2026-08-13.

**The one overlap is on purpose**: the run menu's `Lab` hub re-lists the installation destinations
under a `Whole installation` heading, as `↗` anchors (`RunView.jsx:2768-2773`, derived from the same
`GLOBAL_DESTINATIONS` at `:186-188`), because operators inside a run looked for Memory in the run
menu and concluded it had been deleted (`RunView.jsx:160-167`).

**Verdict — SHIPPED.** If the operator still perceives mixing, `RunView.jsx:2768-2773` is the only
site where a global destination appears inside a run menu, and it is defended in a comment — so
this is a conversation, not a defect.

### N8 · "Зачем вообще Атлас??" — doc 29 F7 re-verified

**The UI ship LANDED. The label survives where the operator still meets it.**

| F7 claim | today |
|---|---|
| `ui/src/ClaimsCuration.jsx` exists | ✅ 537 lines, `<h1>Claims & Curation</h1>` at `:404` |
| `#/atlas`, `#/research-atlas` aliased | ✅ `ui/src/App.jsx:41-43` → `{view:'claims', canonicalHash:'#/claims'}`, canonicalized `:237-247` |
| concepts section dropped | ✅ three sections remain (mixed-evidence `:459`, claim records `:484`, curation `:506`); pointer to `#/concepts` at `:421-423` |
| menu renamed | ✅ `ui/src/globalNav.js:25` |
| HTTP contract untouched | ✅ `/api/cross-run/atlas` still `serve/routers/cross_run.py:558` |

Landed `b937f1a2`, 2026-08-13.

**Where "Atlas" still reaches a human:**

1. **The CLI, prominently.** `looplab/cli/governance_cmds.py:817` is `@app.command(name="atlas")` and
   its output literally prints `Research Atlas: N run(s), …` (`:854`), with warnings naming "Atlas
   concept observations" (`:866`). Documented as live at `docs/guide/cli-reference.md:40` and
   `:1446`. F7 chose deliberately not to touch the contract (doc 29 `:1044-1046`) — but the CLI is
   not a contract, it is a surface with a human on the other end.
2. **The assistant's system prompt** names the tool `cross_run_atlas`
   (`looplab/serve/assistant.py:1501`, also `:1600`, `:1632`), so the assistant will say "atlas" in
   chat. The activity chip is safe (`routers/assistant.py:87` maps it to "Reading shared research"),
   but `ui/src/assistantToolActivity.js:29` falls back to the RAW tool name for anything unlabelled.

**Verdict — SHIPPED in the UI; the question survives because the word does.** The rename's whole
argument was that the NAME is the deliverable, and it was applied to one of the three surfaces that
say it. To finish: rename/alias the CLI command and its echo (`governance_cmds.py:817`, `:854`),
rename the assistant tool or its description (`serve/assistant.py:1501`), and update
`docs/guide/cli-reference.md:40`, `:1446`.

---

## 2 · Memory content quality (M1–M5)

All measurements are read-only, taken 2026-08-19 against the store the real runs actually write:
`/home/jovyan/data/looplab-memory`, pinned by `LOOPLAB_MEMORY_DIR` in `.env:88` and present as
`memory_dir` in the live run's own `config.snapshot.json`. **There are TWO stores with the same
filenames** — the default `~/.looplab/memory` (`core/config.py:1201`) holds 22 cases, 100 %
`toy_quadratic`. Anyone re-measuring this must say which one they read; doc 29's numbers and these
are not comparable until that is fixed.

### M1 · "надо еще и уроки и заметки прошерстить на адекватность… куча дичи с дублированиями"

**This closes doc 29's open tail entry** ("MECHANISMS LANDED, STORE NOT RE-AUDITED (2026-08-14) …
the 2026-08-11 measurement stands until someone purges the toy runs and re-measures"). Two things
came out of the re-measurement, and the second is worth more than the first.

**(1) Doc 29 measured a store that no longer exists, and it is still on disk to prove it.** The
store was quarantined on **2026-08-12 04:25** — `lessons.jsonl.quarantined-2026-08-12` (77 KB),
`meta_notes.jsonl.quarantined-2026-08-12`, `cases.jsonl.quarantined-2026-08-12`,
`research_claims.jsonl.quarantined-2026-08-12`, and a `skills.quarantined-2026-08-12/` directory of
27 auto-distilled skills. Those files reproduce doc 29's numbers exactly (the quarantined notes:
162 rows, 70 distinct, 56.8 % duplicate rows, top text ×23). **Nothing in `docs/` records why the
quarantine happened** — a store-wide reset with no written reason is its own small defect.

Today:

| tier | 2026-08-11 (doc 29) | **2026-08-19** |
|---|---|---|
| lessons | 161 rows, 7 real | **28 rows, 22 real** — 28 distinct statements, **0 exact duplicates**, 20 carry concepts |
| meta-notes | 163 rows, 71 distinct (56 % dupes) | **29 rows, 3 distinct texts** — 26 duplicate rows (89.7 %), 28 of 29 toy |
| cases | 10 rows, 9 fixtures | **29 rows, 3 distinct, 1 real** — but only **2 are `active:true`**, and `/api/memory` drops the rest (`serve/routers/misc.py:718-720`), so the panel shows 2 |

So the duplication complaint is now **half right, and the half that is right is in the two tiers no
prompt reads.** 28 distinct toy smoke runs in seven days each wrote one note and one case; the note
de-dup key is `(run_uid, finish_seq)` (`engine/lessons_distill.py:203-212`) and by construction
cannot see a different run that landed on the same winner. **`meta_notes.jsonl` and `cases.jsonl`
have no consolidation pass at all** — `consolidate_lessons_file` is only ever called on
`lessons.jsonl` (`engine/lessons.py:117`, `:171`, `:321`), at run end and at reconcile
(`lessons_reconcile.py:369`) and nowhere else; the mid-run path passes `hygiene=False`
(`lessons.py:234`).

**(2) The real quality defect today is CONTRADICTION, not duplication, and no mechanism can see
it.** Among the 22 real lessons there are 0 fuzzy clusters at SequenceMatcher ≥ 0.90 — and this:

* v7, `supported`: *"optimizer and scheduler must match the documented recipe exactly — AdamW
  wd 0.01 + OneCycleLR"*
* v8, `supported`: *"wd 0.1 beat 0.01 … should be kept rather than 'fixed' toward a documented
  recipe's 0.01"*
* v8, `failed`: *"swapping cosine for OneCycleLR moved recall by under 0.001"*

`filter_contradicted` (`engine/lesson_hygiene.py:296-319`) keys on an exact `normalize_statement`
plus `task_id`, so it sees none of them. Both contradictory lessons are `supported` and both are
retrievable into the next Researcher prompt.

**Verdict — the duplication half is largely CLOSED by hygiene that landed after doc 29; the
adequacy half is OPEN and is a different bug.** Also worth recording: `unreliable_metric_ids` is
**empty on this corpus** (0 `metric_salvaged`/trust violations in six runs), `memory_cascade` has
**never been used** (all 28 toy identities are still in the store), and `claim_curation_log.jsonl`
is 205 rows of which **198 are `unavailable`** — the claim steward has essentially never run.

### M2 · "не вижу что в памяти что-то по руберт рану заполнялось"

**Right about the run they are watching, wrong about the rubert family, and there are three
different gates behind that.**

| store | rows by run |
|---|---|
| `lessons.jsonl` | v8 **11**, v7 **7**, v6 **2**, `rubert-dr-0807` **2**, toy 6 |
| `research_claims.jsonl` (204) | v8 **129**, v7 **19**, toy/demo 56 |
| `concept_capsules.jsonl` (4) | v8 1, `rubert-dr-0807` 1, +2 |
| `meta_notes.jsonl` | v8 **1**, rest toy |
| `cases.jsonl` | v8 **1**, rest toy |
| **`e5small-dr-unified-v2` (LIVE)** | **0 rows in every store** |
| `rubertlite-dr-unified-v9` | **0 rows in every store** |

**Gate 1 — the mid-run share is structurally dead (this is E1 again).**
`engine/lessons.py:209` calls `at_creation_boundary(len(state.pending_nodes()), …)`, which since
F1f is never satisfiable on a GPU run. Re-measured over all six logs:

```
                                     dr   v6  v7  v8  v9  e5small
lessons_distilled   (trigger=cadence) 19   1   0   1   0   0
report_generated    (trigger=cadence) 26   1   0   1   0   0
research_completed  (trigger=cadence) 27   5   2  14   6   9   <- control, never carried the guard
```

**Gate 2 — the run-end path needs a finalize that never happened.**
`finalize.py:732-746` writes the case, the research claims and the concept capsule in one step, and
`ensure_finalize_reflection` (`finalize.py:465-469` → `lessons_distill.py:122`) writes the
reflection note. `finalization_finished` across the corpus: dense-retrieval 1, v7 1, v8 1 — and
**v6, v9 and the live run 0**. v9 is the pointed case: it created 8 nodes and **evaluated 4**, so it
had a winner to record and contributed nothing to any store because it never reached finalize. The
live run is ~40 h in and has never finalized either.

**Gate 3 — a run with no winner writes lessons but no note and no case.**
`store_case` returns on `best is None` (`lessons.py:395-397`) and the meta-note branch is
`if best is not None` (`lessons_distill.py:180`). v7 created 8 nodes and evaluated **0** (5 failed),
so although it DID finalize, it produced 7 hypothesis/failure lessons — which need no winner — and
no note and no case.

**The sharpest single piece of evidence**: the last row appended to *every* store file today
(08:28) is `run_id: "run"`, `task_id: "toy_quadratic"` — a smoke run. The live GPU run has written
nothing while a toy run writes continuously into the same files.

**Verdict — not one bug; a gate the operator cannot see.** **To close, cheaply:** put per-run
memory contribution in the run view (rows written, by tier, plus "case/note pending finalize"), so
an empty panel is distinguishable from a run that has contributed 11 lessons already.

### M3 · "Auto классификатор гумно надо улучшать"

**Well-founded, with five separable mechanical causes. The largest is E1 and not classifier
quality.**

1. **It mostly does not run.** `node_concepts` per run: dense-retrieval **159**, v6 3, v7 **0**,
   v8 16, v9 **0**, live **0** — and the 159 are not even a live classifier pass; they are an
   offline CLI retro-tag eight days after the run (`cli/concept_cmds.py:102`, with `--task-type`
   supplied). Across all 42 event logs on this box the classifier reached **three**.
2. **It is handed no vocabulary, so it invents one every time.**
   `search/concept_graph.py:395::skeleton_for` holds exactly one curated pack (`dense-retrieval`,
   46 ids) plus 7 substring aliases. Every log here answers `repo_task`, `toy_quadratic` or
   `e5small-dr-unified-v2` — **none matches**. `engine/concept_cadence.py:227-228` then sets
   `seed=None`, `search/concept_map.py:276` builds a graph with 0 concepts and 0 axes, and the
   prompt literally emits `KNOWN AXES: (none — propose axis/slug ids)` /
   `KNOWN VOCABULARY: (empty — this is a new task type; propose concept ids from scratch…)`
   (`search/concept_tagging.py:255-258`). That is free invention, not a narrow allow-list, and the
   prompt is inline at `concept_tagging.py:239-259` with no `PromptStore` override.
3. **It overwrites the researcher's authored membership.** On v8, **24 authored (node, id) slots →
   2 survive → 91.7 % replaced**; node 3's exactly-curated `regularization/r-drop` was replaced by
   an invented `regularization/rdrop` + `regularization/rdrop/symmetric-kl`. The write is a plain
   assignment — `events/replay.py:2403`, `st.node_concepts[nid] = bounded`, inside
   `_on_node_concepts` (`:2363`) — and **only `OPERATOR` rows are protected** (`:2386`); `AUTHORED`
   has no guard. The cause of the churn is one layer up: `engine/concept_cadence.py:309-315` admits
   only OPERATOR/CLASSIFIER rows into the vocabulary, so **the classifier cannot even see the
   researcher's spelling** — v8's classifier minted 7 spellings for the 4 curated `negatives/*` ids
   in a single run.
4. **Consolidation is read-time only.** v8 recorded 9 renames and persists the pre-rename ids
   (`concept_cadence.py:415-445`), so its cross-run capsule publishes 7 rename pairs with BOTH
   sides present. `concept_curation_log.jsonl` holds 21 proposed merges and **zero applied**;
   `concept_aliases.jsonl` does not exist.
5. **Capsules are classifier-only** (`engine/lessons.py:455`), which is why only 4 exist.

**A CLAUDE.md number does not survive re-measurement and should be corrected there.** The recorded
"58.8 % invented on classifier-live runs vs 40.7 % on dead ones" does not reproduce and its
DIRECTION inverts: against the curated 46-id pack, exact-id invention is **74.5 % (76/102) on
classifier-live runs and 93.3 % (28/30) on classifier-dead ones**. What does reproduce exactly is
`run_constant_split` (v9 40/48, v7 16/25, 0 % on the classifier-reached runs) and the overwrite
measurement.

**Verdict — BUG (1), FEATURE (2), DESIGN DEFECT (3-5), in that order of value.** Improving the
prompt of a classifier that does not fire, has no seed vocabulary and overwrites the human's answer
is the least valuable of the three. The cheapest real move is (2): `repo_task` — the id five of six
real runs declare — is not in the alias list, and that is one line.

### M4 · "На сколько дип ресерч спамер? На сколько он реально помогает? … более эволюционирующим и допиливающим текущие гипотезы?"

**Measured over all 90 memos on this box.**

| run | nodes | memos | `hypothesis_added` | `hypothesis_merged` |
|---|---|---|---|---|
| dense-retrieval | 81 | 27 | 132 | **64** |
| v6 | 7 | **28** | 40 | 7 |
| v7 | 8 | 3 | 6 | **0** |
| v8 | 16 | 15 | 10 | **0** |
| v9 | 8 | 7 | 11 | **0** |
| e5small (live) | 11 | 10 | 10 | **0** |

**Volume.** 90 memos, **2.94 M characters** of memo JSON; median 32,261 chars, max 45,270; ~31 % of
the bytes are the `sources` ledger. Triggers: cadence 63, repeat 22, run_start 5, **manual 0,
strategist 0**. Only **5 of 90 thinks are traced** — `_compute_deep_research` passes `trace=False`
on every non-serial path (`engine/research_cadence.py:413-427`) — so the operator cannot inspect
94 % of the research they are paying for.

**"Спамер": yes semantically, no lexically.** Exact duplication is near zero (351 directions → 0
exact duplicates; 807 claim statements → 28), which is exactly why the deterministic
`normalized_belief_key` guard (`research_cadence.py:54-77`) catches nothing — the model re-words
rather than repeats. Cluster v6's 97 directions by idea name at Jaccard ≥ 0.5 and you get **61
clusters, the largest recurring 17 times** (frozen-teacher distillation, re-proposed seventeen
times across 28 memos). v8: 49 → 34. dense-retrieval: 141 → 90.

**"Помогает?" — the yield chain, corpus-wide: 90 memos → 351 directions → 209 `hypothesis_added` →
71 `hypothesis_merged` → 131 nodes.** v6 is the sharpest single row: 28 memos and 97 directions
produced 7 nodes.

**The verdicts are in the record and they are poor.** 807 verdict rows across 89 verification
blocks: **unsupported 438 (54.3 %)**, supported 328 (40.6 %), unclear 41 (5.1 %); the top notes are
`no evidence cited` (123) and `cited experiments do not exist: [0]` (46). Memos verified by the
DETERMINISTIC method are **42 of 42 `unsupported`**. In the persisted `research_claims.jsonl`: 148
claims, 96 unsupported, and **113 of 148 carry no URL while 52 carry no node id**. (One arithmetic
note for whoever builds on this: the `verification.total_verdicts` FIELD sums to 506 across the
corpus while the `verdicts` ROWS number 807 — the tally and the rows disagree by 301, so quote the
rows.)

**"Перерабатывать их уметь?" — the rework component EXISTS, is correctly wired, and has been dead
for four runs.** `_maybe_merge_hypotheses` (`research_cadence.py:818`, import `:898`, call `:909`
`consolidate(texts, client, kind="research hypotheses", …)` — i.e. `search/hybrid_merge.py` really
is wired for hypotheses) fired 64 times on dense-retrieval, 7 on v6, and **0 on v7, v8, v9 and the
live run**. The source at `:838-846` already recorded the cause on v6 — *"research ran concurrently
four times, the gate below was satisfied many times over, and `hypothesis_merged` fired zero times
while the main task sat in a 90-minute evaluation"* — and it was re-examined and deliberately NOT
lifted, because `hypothesis_merged` is folded and two readers key on its POSITION
(`replay._on_hypothesis_merged` and `speculation._proposal_authority_seq`). It is **not** wired for
`research_claims.jsonl`: `engine/claims.py:560`, `:671-701` is a per-run replace-in-place with no
clustering, so the cross-run claim store is the unmerged one.

**"Более эволюционирующим" — structurally impossible today.** The prompt is built at
`agents/deep_research.py:419-425` from `state_brief` (`:101-369`); the only feedback channel is the
board's ≤ 5 seed statements (`agents/roles.py:771::board_prompt_lines`, cap `BOARD_PROMPT_CARDS = 5`
at `roles.py:664`), stripped of reasoning. **No prior summary, finding, claim or verdict enters the
prompt at all.** The one pull path (`read_research_memo`) was called 6 times inside a
`deep_research` phase and returned `"(no deep-research memo yet…)"` all six. The board is
append-only in practice — 209 `hypothesis_added`, 71 `hypothesis_merged`, **0 `hypothesis_updated`,
0 `card_dropped`** — and the prompt says retiring a belief *"is the operator's call, not the
memo's"* (`deep_research.py:316-317`).

**Verdict — the "does it help" half is answerable and unflattering; the "make it evolve" half is a
FEATURE with an existing, blocked component.** Honest ordering: (a) unblock
`_maybe_merge_hypotheses` under the ordering constraint its own comment states — that IS refinement
of existing hypotheses and it demonstrably worked twice; (b) trace the other 85 memos, which costs
a flag and is the only way anyone can audit this; (c) only then feed a prior memo into `research()`,
which is a prompt-contract change and needs doc 36's line about what a memo may decide.

### M5 · "Оформить дип ресерч нормально а то портянка"

**The operator's UI is already structured — and the wall of text is real, in the channel they
cannot see.**

`ui/src/ResearchMemoCard.jsx` (307 lines) renders a memo as discrete sections: takeaway (`:212`),
Findings (`:216`), Recommended actions with a per-direction **steer** button (`:222-236`),
collapsible Technical reasoning (`:238-244`), collapsible Evidence & Verification with per-claim
verdict chips (`:112-172`), collapsible Research activity & sources (`:178-200`); bounded projection
at `ui/src/researchMemoModel.js:11-30`; mounted from `Dock.jsx:34`, `MemoCard.jsx:9` and
`panels.jsx:502`. It landed in **`95033e4c`, 2026-08-12 07:03 UTC**, whose title is literally
*"fix(ui): a research memo reads as a memo, not a wall of text"* — **before the running bundle was
built, so it IS deployed.**

**The портянка is the AGENT channel, and there the cut destroys the useful half.**

* **Push** is one line: `agents/roles.py:938-963` pushes `summary[:300]`. Findings, claims, verdicts
  and directions never reach a proposal prompt — and `core/advisory_payloads.py:703` records that
  `trust/memo_verify.py::verify_memo` verifies `memo["claims"]` and **has never looked at
  `memo["summary"]` at any commit**, so the one field pushed into every prompt is the one field
  nothing checks.
* **Pull** is the wall: `tools/run_tools.py:764::_research_memo` returns ONE undelimited string
  (verifier lead + `Summary:` + 12 findings + 12 claims + 8 directions), capped at
  `RESULT_CAP = 4000` (`core/context_budget.py:20`) **head-keep** (`agents/tool_loop.py:442` →
  `_cap_tool_result:205-218`).
* **Replayed over all 90 memos**: render median **9,083** chars, max 11,605; **89 of 90 exceed the
  cap**; median 5,094 chars discarded; median 44 % survives. `Summary` and `Findings` always
  survive; **`Claims` starts past the cut in 80 of 89 and `Recommended directions` in 89 of 89.**
  Confirmed against real traces: **375 `read_research_memo` calls, 362 (97 %) over the cap, 717,955
  characters dropped, directions past the cut in 194 of 212 calls.**
* Secondary: the tool span is written BEFORE the cap (`tool_loop.py:396`, `:433` vs `:442`), so
  `spans.jsonl` records the full render the model never received — a trace that is not evidence of
  what the model read.

**Verdict — the ask is right and its target is not where either side assumed.** The operator's own
screen was fixed a week ago; the agent's view of a memo is a head-cut string that loses the single
most actionable section every single time. **To close:** give `_research_memo` the same section
structure the card already has, and order it so the sections a caller acts on survive a head cut —
`recommended_directions` and the verification tally FIRST, `sources` last. No new data, no new
event, and it is the same fix for the tool and for the push line.

> **CLOSED 2026-08-19 for the PULL half.** Re-derived independently over the same 90 memos and the
> numbers reproduce exactly (89 of 90 over the cap, median 9,083 chars, median 5,180 discarded,
> `Claims` past the cut in 80 of 89 and `Recommended directions` in 89 of 89; in the traces 375 calls,
> 362 over the cap, directions past the cut in 194 of 212). Reordering alone was refused, because it
> only moves which section is silently amputated: `read_research_memo` now takes a `section`
> (`overview` (default) | `directions` | `findings` | `claims` | `summary`), the overview carries the
> verifier block + the directions IN FULL + a clipped summary, each section page gets the whole cap to
> itself, and the answer NAMES what it left out beside the call that returns it (`log_tools.py` rule
> 3). The cap was not raised. Measured after: **0 of 90** answers are cut by the agent layer in any
> section and the directions arrive complete in **86 of 89** overviews, with the other 3 naming
> `section="directions"`, which delivers them for 89 of 89. The PUSH half (`summary[:300]`) is
> unchanged and remains open.

## 3 · UI defects reported live (U1–U10)

Three parallel investigations were running on these clusters while this audit was written, so these
entries **record and classify** rather than diagnose. Read §0 first: for several of them the fix is
already on `master` and simply is not in the bundle the operator is running.

### U1 · "Карты сломаны в UI" — the card board is broken

**Today.** The board exists and is lazily mounted (`ui/src/CardBoard.jsx`, mounted at
`ui/src/RunView.jsx:62`, re-exported through `ui/src/panels.jsx:2533`). It has **no route of its
own**: it renders `state.cards` / `state.cards_projection` off `/api/runs/{id}/state`
(`looplab/serve/routers/runs.py:213-214`, `:1011`), projected by `looplab/serve/public_cards.py`
with `PUBLIC_CARD_MAX_COUNT = 256` (`:26`) mirrored client-side as `CARD_RENDER_LIMIT`
(`ui/src/cardBoardModel.js:53`). The one dedicated card route is the card trace,
`runs.py:3196`.

**Verdict — BUG, under investigation, and the report is under-specified.** "Broken" is not a state
the code has a name for; the board shares the run-state payload's fate, so a slow or capped
`/state` degrades the board without any board-specific error. **The capture value here is the
coupling**: a card surface whose only data source is the run-state blob cannot report its own
failure. Ask the operator for the screen state ("empty lanes", "stale", "error"), then look at the
`/state` payload, not at `CardBoard.jsx`.

### U2 · "Concepts UNAVAILABLE / Membership unavailable; not empty. Хотя они есть!"

**Today.** The string is `ui/src/ConceptChipBar.jsx:137-143`, fired by the condition at `:136`
(`materialization === 'unavailable' || (!hasConcepts && withheld > 0)`); the DAG tooltip twin is
`ui/src/Dag.jsx:275`. The producing projection is `conceptMaterializationStatus()` at
`ui/src/nodeProjection.js:122-158` over `receiptStatus()` (`:98`) and `receiptStoreValid()`
(`:110`) — it returns `unavailable` for a bad `run_base_concept_receipt`, a non-object
`node_concept_materialization_receipts`, or a store failing `receiptStoreValid`. Server half:
`looplab/serve/concept_frame.py`.

**Verdict — BUG, and the comment at `nodeProjection.js:144-151` says this is the SECOND round of it**:
a per-node → whole-run escalation caused this exact complaint before and was narrowed to `partial`.
The operator's "хотя они есть!" is the diagnostic: concepts exist, the RECEIPT does not validate.
Note this interacts with E1 below — a run whose classifier never fired has run-level concepts and
no node membership, which is a legitimate `not empty` with nothing to show.

**To close.** The refusal must name WHICH receipt failed and for which node; a single
`UNAVAILABLE` chip over three different causes is what makes it unfalsifiable from the outside.

### U3 · "Trace projection is partial — у нас опять ушла кнопка подгрузки всего трейса"

**Today.** `TRACE_PARTIAL_NOTICE` is `ui/src/traceProjection.js:24` (and
`TRACE_PARTIAL_EMPTY_NOTICE` `:26`), emitted as a FALLBACK when the counts are unusable
(`traceWindowNotice` `:66-70`, `conversationWindowNotice` `:131-135`). The load control exists and
is `TraceReach` at `ui/src/Inspector.jsx:1449-1465` — a real button in a scroll sentinel, wired to
`useTraceScroll` (`ui/src/hooks.js:150+`) and `useNodeSpanWindow` (`hooks.js:114-124`, which
DOUBLES the window and returns `loadMore === undefined` at the ceiling).

**Verdict — a BUG and a FEATURE in one line, and the distinction is what the operator needs.**

* The button is not gone from `master`. The comment at `Inspector.jsx:1429-1447` names this
  complaint verbatim and records that it has now been reported **twice**, the `↧` pager having been
  replaced by this scroll-sentinel affordance. An affordance that must be scrolled into existence
  is one the operator reports as missing; that is a design defect, not a regression.
* **A "load the WHOLE trace" control has never existed.** `useNodeSpanWindow` doubles up to
  `NODE_TRACE_SPAN_WINDOW_MAX` and stops. Doc 29 F6 is explicit that raising the ceiling is not the
  fix and was deliberately not done (the cost is 3.4 ms/span + 0.9 ms/span on the request thread).
  So "подгрузка всего трейса" is a FEATURE that was refused with a measurement, and the operator has
  never been told that. **Say it in the UI**: the notice should state the bound and offer the SEEK
  (U4), not imply a fuller view is one click away.

### U4 · "Трейсы очень долго грузятся!!! и нельзя получить более ранние трейсы — не происходит ничего"

**Today.** The seek exists on both halves. Routes in `looplab/serve/routers/runs.py`:
`/nodes/{nid}/trace` (`:2315`, `before` documented `:2329`), `/nodes/{nid}/conversation`
(`before` `:2764`), `/nodes/{nid}/episodes` (`:2379`); anchor settling is
`looplab/events/traceview.py:181-210` (`settle_trace_anchor`), an unplaceable anchor raises
`TraceEpisodeCursorUnknown` → 409 `trace_anchor_unknown` (`traceview.py:76`, `runs.py:1824`);
episode paging `traceview.py:1749-1855` with `next_before` `:1830` and `has_older` `:1832`. The
client half is `traceReadQuery` (`ui/src/api.js:1257-1271`), `ui/src/traceEpisodeModel.js`, and
`TraceEpisodes` (`ui/src/Inspector.jsx:2132`) whose "load earlier steps" button at `:2270-2275` is
gated on `map.hasOlder` and whose whole picker is gated on `traceIsBounded` (`:2382-2385`).

**Verdict — BUG, and §0 is the mechanism.** The client half of the 2026-08-17 fix
(`3492bfad`, which also rewrote `events/span_index.py` and `serve/appstate.py` for the "very slow"
half) is **not in the running bundle**, while its server half **is** in the running server. "Не
происходит ничего" is the literal behaviour of an old client against a new route.

**To close.** Rebuild the bundle (§0) and re-report. Only if it survives a rebuild is there
anything here to debug.

### U5 · "Куда делись трейсы ресерчера и девелопера?"

**Today.** **There is no run-level agent trace surface, and doc 29 claims there is.** Doc 29 F6
closes with *"run-level agents (the Researcher above all) had no surface at all — see the new
Operations panel"* (`docs/29-operator-backlog-2026-08-11.md:1125-1126`). Grep for
`Operations panel` / `OperationsPanel` across `ui/src` and `docs/guide/` returns **nothing**. What
exists is per-EVENT op-trace expansion only: `OP_TRACE_TYPES` (`ui/src/Dock.jsx:74` —
`strategy_decision`, `hypothesis_merged`, `research_completed`, …), the `OpTrace` component
(`Dock.jsx:421`, rendered `:624-630`), `opTraceSubject` (`ui/src/traceSurfaceModel.js:67`), and the
Inspector render at `Inspector.jsx:2110`. The run-level `/api/runs/{id}/trace` payload's `unscoped`
list (`runs.py:3193`) has **zero consumers in `ui/src`**.

**Verdict — FEATURE, never built, and a DOC OVERCLAIM in doc 29 F6.** This is one of the two
contradictions this audit was sent to find. The run-level spans are served and nothing reads them.

**To close.** A panel over `/api/runs/{id}/trace`'s `unscoped` spans, grouped by role. The data is
already on the wire; this is a client-only change. Doc 29 F6's closing line should be corrected to
say the panel was scoped and not built.

### U6 · "Не видно трейсы с прошлых версий ноды (к примеру баги были и репейр пошел)"

**Today.** Two pickers exist and they answer different questions. The **attempt** picker
(`ui/src/Inspector.jsx:2360-2372`) renders only when `attemptOptions.length > 1`, and
`Node.attempt` is bumped by `node_reset` ONLY — never by inline repair
(`looplab/core/models.py:1018-1031`). The **episode** picker (`node_episodes`,
`looplab/events/traceview.py:1749`, route `runs.py:2379`, UI `Inspector.jsx:2132`) is the one that
reaches inside a lifecycle.

**Verdict — the doc-29 F6 shipped answer is correct and the operator still cannot use it**, because
the picker that is VISIBLE (attempt) does not render on a repaired node and the picker that WORKS
(episodes) is gated behind `traceIsBounded` and, per §0, is at its pre-`3492bfad` version in the
bundle. Doc 29 measured the same thing on `rubert-dr-0804` node 1 (2,345 inline repairs, all
generation 0, `docs/29-operator-backlog-2026-08-11.md:1086-1090`) — so this is a NAMING defect that
survived a correct fix: the operator's word "версия ноды" means repair episode, and the control
called "attempt" means something else.

### U7 · "Кажется в разных попытках логов стейджей один и тот же лог"

**Today — it is not a perception; there is literally one file.** `GET
/api/runs/{run_id}/nodes/{nid}/logs` (`looplab/serve/routers/runs.py:2118`) tails
`<stage>.log` from the node directory (`stages = {name: body for name in stage_names ...}`,
`:2202`). A repair re-runs the stage into the SAME `<stage>.log`. The route DOES take an
`attempt` parameter (`:2119`) — but read what it does: `:2144-2146` raises a 409 when
`attempt != current_attempt`. **It is a CAS guard, not a selector**: you may assert which attempt
you are reading, you may never ask for an earlier one.

The engine already knows where an attempt begins: `attempt_byte_floor`
(`looplab/engine/train_monitor.py:1292`), extracted precisely so two readers cannot disagree about
where the previous attempt ended. **Nothing under `looplab/serve/` imports it** (grep: zero hits),
and no event carries it — `node_repaired`'s payload is
`attempt, changed, code, deleted, error_in, eval_seconds, files, footprint_finalized, generation,
idea_footprint, node_id, rationale, reason, stages_passed, triage_action, unmet,
unparseable_repairs, verified` (read off the live run), with no log offset.

**Verdict — BUG, confirmed, with a precise site.** The judges can separate attempts and the
operator cannot.

**To close.** The floor is in-process monitor state (it needs a `TrainingLogSnapshot`), so it
cannot simply be imported by a route. Make it DURABLE: append the per-log byte offset at each
attempt boundary as an additive, fold-ignored diagnostic — `EV_PHASE_PROGRESS`
(`looplab/events/types.py:361`) is the existing precedent for exactly that shape — then the logs
route can slice `[floor(n), floor(n+1))` per attempt with no new reader of the bytes.

### U8 · "blocked написано на карточке — я думал что это статус. а это что её перемещать нельзя"

**Today — already fixed on `master`, and invisible to the operator.** The chip is no longer the bare
word: `ui/src/CardBoard.jsx:311-321` renders `cardSelectionBlock(card)` with
`tone === 'fault' ? ' warn' : ' quiet'`. The vocabulary is `ui/src/cardBoardModel.js:55-120` —
`card.selection_ready === false` means "the Card queue will not pick this up next", split into
`BLOCKER_LIFECYCLE` (`:71-84`), `BLOCKER_FAULT` (`:85-91`) and `BELIEF_ONLY_BLOCKERS` (`:96-97`).
Blocker names originate at `looplab/events/card_ledger.py` (`c.selection_blockers`).

**Verdict — FIXED on `master`, NOT DEPLOYED.** See §0: `CardBoard.jsx` is in the 19 commits the
bundle predates. This is the cleanest single demonstration that the deployment gap, not the code,
is what the operator is looking at.

### U9 · "/stop is pending in the run timeline — 'Stop requested… · cmd_fc2a50f8…' заместо кнопок и не убирается"

**Today.** The chip is `ui/src/Dock.jsx:1349` with the truncated command id at `:1351-1353`, inside
the `transportPending` block `:1336-1380` (siblings at `Dock.jsx:919`, `ui/src/AssistantBar.jsx:134`).
Lifecycle rules are `ui/src/runCommandMachine.js`: the 2026-08-11 incident is recorded in the
source at `:94-106`, `COMMAND_STALL_NOTICE_MS = 15000` at `:107`, and `pendingCommandRemedy`
(`:113-140`) binds a remedy off the server's `absolute_deadline_at`. Server side:
`CONTROL_EVENTS` (`looplab/serve/protocol.py:65`), the durable record store and worker
(`looplab/serve/run_commands.py`), routes `looplab/serve/routers/control.py`.

**Verdict — BUG, and doc 29's tail entry is the same incident recurring.** Doc 29 records this
mechanism at length and says FIXED 2026-08-13, with `tests/test_control_plane_liveness.py` as the
general guard. The chip clears only on a TERMINAL command record, so any path that leaves a record
non-terminal reproduces the visual symptom even after the liveness fix. The operator's specific
complaint — the chip *replaces the buttons* — is a client decision (`transportPending`) and is
independent of whether the command is recoverable.

**To close.** Two separable things: (a) never let a pending command hide the controls, since the
documented remedy is itself a control; (b) confirm against a rebuilt bundle whether a stuck record
still occurs post-`fix/control-plane-liveness`.

### U10 · "При старте рана очень долго висит 'еще билдится' хотя можно было ноду показать и как она собирается"

**Today — both halves exist.** Producer: `EV_PHASE_PROGRESS = "phase_progress"`
(`looplab/events/types.py:361`, rationale `:345-360`, listed in `DIAGNOSTIC_EVENTS` `:679`),
appended by `looplab/engine/shared.py:185`. Consumer: `openPhases` at
`ui/src/buildingModel.js:90`, folding `event.type === 'phase_progress'` at `:93`, consumed `:130`,
phrase table `:140-155`; the synthetic `status:'building'` splice is `:34-52`.

**Verified live**: the running `e5small-dr-unified-v2` log carries **76 `phase_progress` events**, so
the beacon really is being written by the engine the operator is watching.

**Verdict — SHIPPED and working on the producer side; the remaining ask is a DIFFERENT one.** The
operator asks to see *the node itself while it assembles* — the files appearing — not a phase
label. That is a FEATURE: `node_created.files` is written once, at the end of the build, and
nothing streams partial authorship.

---

## 4 · Assistant (A1–A6)

Also under parallel investigation; recorded and classified here.

### A1 · "Почему ассистент в какой-то момент виснет? (40 тул юзов) и потом в ответ прилетает какой-то тул юз"

**Today.** The loop's five silent exits are a named vocabulary since doc 29 F4:
`LOOP_CUTOFF_KINDS = ("time","turns","stuck","stalled","emit_force")`
(`looplab/agents/tool_loop.py:472`, with the note at `:466` that three of the five used to fall
through to `fallback` with nothing said). Turn budget: `max_turns` (`:496`, semantics `:512-518`,
`turns = itertools.count() if max_turns <= 0 else range(max_turns)` at `:645`, cutoff at `:838`,
`return fallback(messages)` at `:850`). The assistant's own cap is
`looplab/serve/assistant.py:2005` (`agent_max_turns`), applied at `:2013`, and
`_reply_from(messages)` (`:1998-2002`) takes the last non-empty assistant string — i.e. an
interstitial narration — when the loop ends without an emit.

**Verdict — DIAGNOSED AND SHIPPED (doc 29 F4), and the source now names this report.** The comment
at `serve/assistant.py:2021-2026` cites it verbatim ("hangs around 40 tool uses and then something
odd comes back") and routes all five kinds through `cutoff_notice` (`:1890-1906`), appended at
`:2094` and persisted as `budget_exhausted` (`:2100-2103`).

**Residual, and it is the operator's actual experience:** a cutoff is now LABELLED but still ends
the turn. "Виснет" is the wait; nothing streams "I am on tool call 37 of 40". The notice explains
the ending after the fact, and there is no live turn-progress signal.

### A2 · "тыкнул стоп и сразу написал новое сообщение — и старое сообщение ассистента продублировалось"

**Today.** Cancel is `POST /api/assistant/sessions/{sid}/cancel`
(`looplab/serve/routers/assistant.py:862-868`) over `_asst_cancel` (`:510`) / `_asst_epoch`
(`:512`), with the turn slot claimed at `_acquire_turn` (`:688-713`). The append is conditional:
`_persist` uses `expected_len=len(history)+1` (`:1663`) through
`SessionStore.append_if_len` (`looplab/serve/assistant.py:734-744`), and the comment at
`routers/assistant.py:1649-1653` describes **exactly this bug** — "if the user cancelled and sent a
newer message, appending unconditionally would interleave the transcripts (u1,u2,a1,a2)".

**Verdict — the guard EXISTS and the operator still saw the symptom, so this is the sharpest
open assistant item.** Two candidate paths that bypass the conditional append are named in the
source and must be checked against the report: the `recover_turn` branch (`:1532-1560`), which
re-runs a durably staged user turn that has no reply, and watch wake-ups, which deliberately
bypass the conditional append (`:1758`). Under investigation; do not close from the guard's
existence alone.

### A3 · "'Let me try ls with just the dir. The runcommand tool seems to be misbehaving. Let me use listdir instead.'"

**Today.** The assistant's advertised tools are `list_dir, read_file, find_files, grep` plus
`list_runs, read_run, read_run_experiment, read_run_logs, read_run_trace`
(`looplab/serve/assistant.py:1489-1491`). **`run_command` does not exist in the assistant's
toolset** — `grep -c run_command looplab/serve/assistant.py` = 0. So the model narrated the failure
of a tool it never had, and that narration became the REPLY via `tool_loop.py:850`
(`return fallback(messages)`) → `_reply_from` (`serve/assistant.py:1998-2002`).

**Verdict — TWO defects in one quote, and the second is the interesting one.**

1. The model hallucinating a tool name is a prompt/model fact, not a code defect.
2. **The engine promoted an interstitial to a final answer.** That is the `stuck` exit, and
   `serve/assistant.py:2021-2026` says so in the source: it "reproduces the operator's report most
   exactly (a bare interstitial narration returned as the answer)". The notice now labels it —
   which makes it legible, not absent.

**To close.** When the last assistant string is an interstitial (it names a next action rather than
answering), the cutoff notice should REPLACE it, not follow it.

### A4 · "Контекст в ассистенте как считается? то 20к то 50 то 25к то 18к в одном разговоре"

**Today.** `contextUsage()` (`ui/src/assistantChromeModel.js:56-84`) reads `message.tokens.context`
ONLY — the peak SINGLE prompt of that turn — and deliberately never `tokens.prompt`, which SUMS the
same context re-sent by every call in a turn (O(calls²), a COST number). The source states the
consequence outright: a turn with no measured peak folds to `last = 0` and **the chip disappears**.
Server half: `_client_tokens` (`looplab/serve/llm_context.py`, imported at
`routers/assistant.py:43`), attached at `:1645`, persisted `:1663`, streamed on `done` `:1969`.

**Verdict — the "50k" was a real BUG and it is FIXED and DEPLOYED**: the `tokens.prompt` fallback was
removed in `38de9c02` (2026-08-13 11:56 UTC), which is *before* the running bundle was built
(§0) — so unlike most of §3, this fix IS in the operator's browser. What remains is not a
defect but an **unexplained semantic**: the number is a per-turn peak, so it legitimately goes down
after compaction and legitimately disappears when unmeasured. Nothing on screen says that except
`contextChipTitle` (`:86-97`).

**To close — FEATURE (labelling).** Say what the number is next to it. The operator's question is
literally "как считается?" and the answer exists only in a source comment.

### A5 · "Саммари ассистента берет весь диалог. Нужно суммаризовать только то, что он в конкретном ответе делал"

**Today.** `final_answer_messages(convo, *, boundary=..., directive=...)`
(`looplab/serve/assistant.py:1793`), called at `:2075` with `boundary=turn_request`. Its directive
(`:1730-1732`) says to summarize what was done in THIS turn — everything after that marker — and
explicitly not to re-summarize earlier ones; `:1798` records that it used to say "based on
everything above". Unlocatable boundary is handled at `:1811`. Pinned by
`tests/test_assistant_final_answer_scope.py`.

**Verdict — SHIPPED, and it does exactly what was asked**, including the operator's own definition
of the boundary (everything after the last user message) — implemented as the turn's OWN
`turn_request` rather than a scan for the last `user` role, which is stronger. The whole
conversation deliberately stays as CONTEXT (`:1796-1797`); only the summarized SCOPE narrowed.

**Action: none, other than telling the operator it landed.** If they still see whole-dialogue
summaries, it is the pin: the server process predates nothing here, but confirm the observation is
newer than the fix before re-opening.

### A6 · "Бесконечный режим работы ассистента / Ожидание статусов / Мониторинг раз в н времени"

**Doc 29 F4 claims SHIPPED 2026-08-13. Re-verified: the BACKEND shipped, the operator's control did
not.**

* Backend exists and is wired: `looplab/serve/assistant_watch.py`; `Settings.assistant_time_budget_s`
  (`looplab/core/config.py:1793`) resolved by `assistant_time_budget()`
  (`looplab/serve/assistant.py:1856`); `WatchStore` constructed at `routers/assistant.py:494`, with
  three routes — `GET /api/assistant/watches` (`:1797`), `POST /api/assistant/watches` = `arm_watch`
  (`:1802-1828`), `DELETE .../{watch_id}` (`:1830`).
* **The browser can LIST and STOP a watch and cannot ARM one.** `ui/src/api.js:1095-1101` defines
  `assistantWatches` (GET) and `assistantWatchStop` (DELETE) and **no client for the POST**; the
  comment at `:1095` states the list "is the ONLY thing the browser owns here". The read-only strip
  is `watchStrip` (`ui/src/assistantWatchModel.js:106`) rendered at
  `ui/src/AssistantBar.jsx:3728-3745`.
* Arming is reachable only by asking the assistant in prose, through its own tool vocabulary
  (`serve/assistant.py:2205`, `:2237`, `:2313`).

**Verdict — the second doc-29 contradiction.** F4 is not wrong about what was built; it is wrong
about what the operator got. A durable monitoring feature whose only arming path is "ask the model
nicely" reads, from the chair, as not shipped — which is why this line is still on the notebook
list. Note also that `b8fd9f22` ("feat(assistant): add durable continuous work", 2026-08-17 14:43
UTC) is in NEITHER the running bundle nor the running server pin (§0).

**To close.** One POST client and one control. The route, the store, the scheduler, the restart
policy and the refusal vocabulary are all already there.

---

## 5 · Engine (E1–E7)

### E1 · "Концепты перестали ставиться"

**Today — TRUE, it is a regression, and it is not about concepts.** The whole node-count cadence
FAMILY died at the same moment, because they shared one precondition:
`if state.pending_nodes(): return False`, copied by imitation from the 2026-06-24 Strategist commit
into five consumers. F1f (2026-08-13) made evaluation children outlive the turn that admitted
them, so on a GPU-shaped run that predicate is never true again. Measured over every event log on
this box:

| event | dense-retrieval 07-18 | v6 08-13 | v7 08-14 | v8 08-16 | v9 08-17 | e5small **LIVE** 08-19 |
|---|---|---|---|---|---|---|
| `node_concepts` (the classifier) | **159** | 3 | **0** | 16 | **0** | **0** |
| `strategy_decision` | 25 | 1 | 0 | 1 | 0 | 0 |
| `concept_coverage_snapshot` | 0 | 1 | 0 | 1 | 0 | 0 |
| `lessons_distilled` | 22 | 1 | 0 | 2 | 0 | 0 |
| `research_completed` *(control — never carried the guard)* | 27 | 28 | 3 | 15 | 7 | 10 |
| nodes created | 81 | 7 | 8 | 16 | 8 | 11 |

`concept_retag_every` and `strategist_every` are identical across all five snapshots, so this is not
configuration. The live run carries exactly **one** `run_concepts` event (the authored base set,
seq 2050) and **zero** `node_concepts` over 11 nodes.

**The fix exists**: `engine/cadence.py:44::at_creation_boundary`, wired at the classifier's own call
site (`engine/concept_cadence.py:73-75`) and at `strategy.py:357`, `lessons.py:209`,
`research_cadence.py:966`; the fifth consumer is a stated refusal, not an oversight
(`cadence.py:63-80`). It landed in `db470130`, **2026-08-18 05:54 UTC**.

**And it cannot reach the run the operator is looking at — twice over.**

1. The live engine process started **2026-08-17 16:38**, pinned to `fbbab725`; `db470130` is not an
   ancestor of that pin (§0).
2. Even on RESUME it stays off. `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` pins
   `cadence_while_evaluating: False` for any run whose snapshot predates the field
   (`core/config.py:2532`), and **neither `runs/e5small-dr-unified-v2/config.snapshot.json` nor
   `runs/rubertlite-dr-unified-v9/config.snapshot.json` contains the key** (213 settings each,
   verified). The `Settings` default is `True` (`config.py:1234`) and `EngineOptions` is `False`
   (`engine/options.py:261`).

The code says this out loud rather than hiding it — `config.py:2526-2531`: *"A run resumed from a
snapshot written before this field keeps the dead cadence — which on this box is v9's and the live
e5small's shape, i.e. exactly the runs the fix exists for. That is deliberate."* The re-entry rule
(never silently add paid calls to an old run) is right; the consequence is that **there is not one
byte of on-disk evidence that the fix works.**

**Verdict — BUG (regression), fixed in code, unproven and unreachable in practice.**

**To close.** (a) Resume or start the live run with `cadence_while_evaluating` set EXPLICITLY —
which the map's own rule says wins over the missing-field default — and re-count `node_concepts`;
that is the first on-disk evidence. (b) Note a second-order gap for later:
`core/models.py::classifier_verified_node_concepts` returns `[]` for a tag stamped while
`pending > 0`, so on a run that never goes quiescent a visible tag is still not evidence for
graded-novelty admission.

### E2 · "Опять не устанавливаются нормально зависимости"

**Today — there is no open installer defect, because the installer has never run.** Three install
paths exist, all `python -m pip` into the single shared interpreter: run setup
(`engine/eval_dispatch.py:379`), crash-time `_install_missing`
(`engine/crash_repair.py:623` → `runtime/deps.py:792`), and the per-node re-sync
(`eval_dispatch.py:160`), gated by `auto_install_deps` (`core/config.py:898`) AND
`trust_mode == "trusted_local"` (`engine/orchestrator.py:784`).

Measured over all six event logs: `deps_installed` = **0**, `run_setup_started/finished` = **0**,
no resolver errors. Every run's `deps_declared` is byte-identical and says
`"action": "nothing_declared"`, `"observed": ["pyproject.toml", "poetry.lock"]` — because
`DECLARATION_FILES = ("requirements.txt",)` (`runtime/deps.py:390`) and the testbed repo ships
neither. LoopLab observes Poetry metadata and deliberately never executes it
(`deps.py:382-396`).

The operator's symptom — a module missing at run time — occurred **twice**, both routed to repair,
neither an install failure:

* `runs/e5small-dr-unified-v2` **2026-08-18 20:17:09**, node 7,
  `ModuleNotFoundError: No module named 'vectorsearch'` — a `sys.path` bug in agent-written code
  (`train_merge.py` run as a script). Correctly not a pip candidate.
* `runs/rubertlite-dr-unified-v7` 2026-08-14, node 0, `No module named 'mlflow'` — a genuinely
  missing library, but `mlflow` is **not in the `_PIP_NAME` allowlist** (`runtime/deps.py:40-119`,
  ~61 entries), so the engine could not install it and the Developer spent a repair writing a fake
  shim. This is the same shape doc 29 F2 quotes for `loguru`.

**Verdict — FEATURE-shaped, in two named gaps**, not a bug in the installer:

1. **Allowlist coverage.** `mlflow` and `loguru` are used by this testbed and are absent from
   `_PIP_NAME`. The allowlist is documented as intended behaviour
   (`docs/guide/configuration.md:769`), so widening it is a decision, not a fix.
2. **Observability.** A crash-time install failure is swallowed at `crash_repair.py:677-680` and
   there is **no `deps_install_failed` event type at all** in `events/types.py`. That is why "опять
   не устанавливаются" cannot be answered from the log — the engine never says it tried.

### E3 · Debug node, inline repair, systemic vs local failure

**Asked (verbatim, and it is three separate claims):** *"У нас есть лимит inline repair. При этом
если за него выходим, создается нода Debug где опять идет фиксинг. Это выглядит бесполезно… Либо
Debug ноду убираем вообще. НО: если проблема чисто системная (только нулевую ноду стартуем и у нас
баги в окружении, в самой либе или основных данных) — надо весь ран стопать. Если проблема в новом
куске кода — стопается только нода и ее направление, а ран продолжается."* plus *"Инлайн репейр по
умолчанию бесконечный и стопается критиком (разве что аля 100 штук поставить сверху лимит)."*

**Doc 29 F5 + F8 re-verified line by line on today's tree — every claim holds:**

| doc 29 claim | today |
|---|---|
| `search/policy.py::debug_action` and every producer cut | ✅ gone; `policy.py:90-110` states it; `KIND_DEBUG` survives only as an event-log spelling (`policy.py:12`, `:40`) |
| `tests/test_debug_node_removed.py` pins it | ✅ exists, **12 passed** when run alone; `_PRODUCER_NAMES` at `:179` |
| `inline_repair_attempts` default `0` | ✅ `core/config.py:808`, `engine/options.py:207` |
| `_UNLIMITED_REPAIR_CEILING = 50` | ✅ `engine/evaluate.py:131`, applied by `_effective_repair_cap` `:149` |
| `DEVELOPER_STUCK_PREFIX` told to the Developer | ✅ `core/models.py:837`; contract rendered into the prompt at `evaluate.py:2395` |
| repair critic from attempt 3 | ✅ `repair_critic_after = 3` (`config.py:818`), gate `evaluate.py:2314` → `crash_repair.py:358` → `unified_agent.py:627` |

**And it is FIRING**: the live run's log carries a `repair_critic_verdict` event. So the operator's
second sentence is now a description of the shipped system, not a request.

**The THIRD clause is the part doc 29 never answered, and the answer is "half".**

* **Systemic → stop the run: EXISTS, with a stricter predicate than the operator assumes.**
  `orchestrator.py:427::systemic_failure_stop_reason`, threshold `Settings.systemic_failure_stop = 3`
  (`config.py:1136`), call site `orchestrator.py:1752`. Its first line is
  `if state.evaluated_nodes(): return None` (`:456-462`) — **one node that ever produced a metric
  disables the bound permanently, for the rest of the run.** That matches the operator's "только
  нулевую ноду стартуем" framing exactly, and it means a run that succeeded once and then broke its
  environment (a mid-run `pip`, a corrupted checkpoint, a moved dataset) has no systemic stop at all.
* **New-code failure → retire the node AND its direction: PARTIAL.** The node is terminal and its
  Card is dropped (`orchestrator.py:4964::_drop_card_once`); the search cannot breed from it because
  `RunState.breedable_nodes()` (`core/models.py:1893`) is evaluated-and-feasible only — which is
  also what made removing the Debug node safe. ASHA retires a PARENT after
  `_ASHA_MAX_FAILED_PROMOTIONS = 2` failed children (`search/policy.py:64`, `:505-513`). **What is
  not retired is the IDEA**: nothing stops the Researcher proposing the same direction again on a
  fresh Card. There is no topic/idea blocklist.

**Verdict — SHIPPED for clauses 1 and 2; clause 3 is a FEATURE with two named halves**: a systemic
stop that survives a run's first success, and a direction-level retirement that outlives the node.

### E4 · "Разобраться как вообще работает параллелизм… если карты уже заняты, он будет ждать или фейлится?"

This is the operator's longest engine item and doc 29 F1 answers only its first half.

**Half one — who decides the width. BUILT (doc 29 F1), and NEVER OBSERVED.** Verified today:
`Settings.proposal_width = True` (`core/config.py:504`), `EV_RUN_WIDTH_SETTLED`
(`events/types.py:317`, folded `events/replay.py:3704`, appended `engine/orchestrator.py:3080`),
`engine/widths.py:95::proposal_derived_width` = `max(1, min(pool // widest_declared_gpus,
ceiling))`. `proposal_width: true` is in the v8/v9/live snapshots — and
**`run_width_settled` appears 0 times across every event log on this box.** The live run settled
`eval_parallel: 2` at launch and never moved. So the mechanism is built, enabled, and unproven on
disk; a two-GPU box with one-GPU footprints simply never produces a re-pin.

**Half two — the question actually asked. There IS cross-run arbitration, and the answer is: it
WAITS, forever, and it waits for more than it needs.**

A single **pool-wide advisory lease**, one file per OS user:
`/tmp/looplab-gpu-pool-<uid>.lock` (`engine/resources.py:48::default_gpu_host_lease_path`,
`flock(LOCK_EX|LOCK_NB)` at `:93`, wired at `orchestrator.py:1047-1049`).

* If run B holds any GPU and run A wants one, **A blocks indefinitely** — `_acquire_gpus`
  (`resources.py:562`) returns `None` on contention (`:608-612`) and
  `_wait_reserve_node_resources` re-polls every 0.5 s (`:645-650`). It does not fail, does not time
  out, and does not oversubscribe. So for the operator's scenario — repair/restart an older run
  while a newer one is training — **plain repair is fine and a repair that needs the GPU queues
  behind the whole other run.**
* The wait is announced at WARNING every 30 s naming the lease path, the holding PID and the
  elapsed time (`resources.py:523-546`) — added because it was previously silent and misread as a
  deadlock.
* **The lease is released only when the holder's pool is COMPLETELY free**
  (`if len(self._free_gpus) == len(self._gpu_ids)`, `resources.py:632`). One busy device on an
  8-GPU box keeps the neighbour out of all eight. It is one lease for the POOL, not per device;
  disjoint-GPU runs still serialize; reacquisition is non-blocking with no fairness, so a lightly
  loaded holder can starve a waiter. All of this is written down as an accepted tradeoff at
  `resources.py:593-601`.
* `eval_parallel=1` does not exempt you: a serial GPU-capable run reserves the entire pool
  (`whole_pool_unpinned`, `resources.py:678-690`). The escape hatches are a Card footprint of
  `{"gpus": 0}` or launching with `CUDA_VISIBLE_DEVICES=`.
* The only FAIL case is a lease file that cannot be OPENED (EACCES on a squatted lock, read-only
  fs): `GpuPinUnenforceable` → a durable `admission_unpinnable` marker → that node is terminal, the
  run is not (`resources.py:592-604`, `:668-676`).
* **Boundary, and it is the real risk:** the lease is per-OS-user and per-filesystem namespace.
  Different users, containers or hosts get **no coordination and will oversubscribe.**

Operator-facing documentation already exists and matches the code:
`docs/guide/configuration.md:277-292` ("Two runs, one box: the second one WAITS").

**Verdict — ANSWERED, not a defect.** The remaining FEATURE is fairness/granularity: a per-device
lease and a queue with an announced position, so "waiting" is a state with an ETA rather than a
silent 0.5 s poll. Zero lease-wait notices exist in the console logs on this box, so the contention
has never actually been hit here.

### E5 · "Общие задачи и переиспользование в рамках рана или нескольких"

**Today.** An INSTRUMENT shipped; the CACHE was refused on measurement; cross-run compute reuse is
structurally forbidden.

* **Recorded, decides nothing.** `runtime/stage_identity.py::stage_input_key` (`:322-405`, schema
  `stage-input-key/v1` at `:150`) and `stage_output_identity` (`:408-425`), written onto
  `stage_finished` (`runtime/command_eval.py:2525-2528`). `engine/eval_stages.py:696-699` says it
  outright: "It RECORDS and decides nothing."
* **Instrument.** `looplab stage-dups` (`cli/inspect_cmds.py:665-673`), read-only, reports observed
  duplication AND how many hits a key would have got WRONG.
* **Real reuse, within one node.** `_safe_reuse_start` (`engine/eval_stages.py:728-880`, called at
  `engine/evaluate.py:2655`) restarts the same node's pipeline from a later stage across an inline
  repair. Corpus: 7 reuses, all within one node's own retry.
* **The cross-node cache was REFUSED with numbers** (`docs/BACKLOG.md:3443-3600`): the obvious
  declared-params key scores 7 hits of which **4 are wrong**; the only sound key yields 1 reuse and
  0.64 h = **0.26 % of stage time**, against 3.0-4.4 s per stage to compute the key.
* **Cross-run compute reuse: none, by construction.** `stage_input_key` takes a mandatory `scope` of
  run dir + config hash (`eval_stages.py:702-706`) and `stage_identity.py:79-81` states "A key never
  matches across runs"; the cross-run variant measured 8 hits, **5 wrong**.
  `engine/cross_run_context.py` / `cross_run_index.py` carry text and numbers only — knowledge,
  never artifacts. The single recorded "run B used run A's bytes" event is the v6 node-4 incident,
  i.e. the defect that produced `runtime/read_fence.py`.

**Verdict — the reuse half is ANSWERED AND REFUSED with evidence.** The operator's other half —
"общие задачи", shared TASK definitions across runs — is a genuine FEATURE with nothing behind it
today: a task is a per-run snapshot (`task.snapshot.json`), and there is no library of task
definitions, no inheritance and no way to say "run this same task with these two changes". That is
the ask worth converting into a design item, and it is not the same ask as artifact reuse.

### E6 · "Дать девелоперу выполнять простые bash команды" — doc 29 F2 re-verified

**F2's answer holds and the tree has MOVED PAST doc 29.**

* The probe: `looplab/tools/dev_probe.py`, `Settings.developer_probe = True`
  (`core/config.py:1852`), `developer_probe_timeout_s = 60.0` (`:1859`), legacy-snapshot default
  `False` (`config.py:2498`), wired at `adapters/repo_developer.py:798-803` inside `_scout_tools`.
  It is an INTERPRETER, not a shell, and `dev_probe.py:18-31` argues why.
* **New since doc 29 and not recorded there: `looplab/tools/dev_commands.py`**, added
  `cb3433b3` on **2026-08-17 11:58 UTC**. The Developer CAN now run shell commands — but only ones
  the operator pinned in advance: `RepoTask.developer_commands` is
  `Field(default_factory=list)` (`adapters/repo_task.py:1170`), the model selects only an immutable
  `name`, and argv/cwd/env/timeout come from `task.snapshot.json`. Argv is validated as a LIST,
  never a shell string; it runs through `runtime/sandbox.run_argv` in a disposable workspace.
  Wired at `repo_developer.py:792-797`.

**Verdict — the literal ask ("simple bash commands") is answered TWICE and still reads as "no" from
the chair**, because `developer_commands` is EMPTY by default: out of the box the Developer has a
Python probe and zero commands. **Doc 29 F2 should be updated** to name `dev_commands.py` — it
currently reads as though the probe were the whole answer.

### E7 · "Переход на git worktree?" — doc 29 F3 re-verified

**DECLINED WITH MEASUREMENT (doc 37) still stands** — nothing on the tree reopens it. The follow-up
doc 29's own status box names as outstanding is **still not done**: `workspace_seeded` carries only
the materialized name list —

```
looplab/engine/workspace.py:179
    self._e.store.append(EV_WORKSPACE_SEEDED, {"node_id": nid, "materialized": seeded})
```

`seeded` (`workspace.py:158-169`) holds strings like `"{name}[{mode}]:{count} tracked"` — a file
COUNT for editables, **no bytes anywhere** (`grep byte engine/workspace.py` returns only the
unrelated binary-asset branch at `:41-49`). So the measurement that declined the migration is still
not reproducible from the event log, which is exactly the condition that let the 727 GB be
misattributed to the seed in the first place.

**Verdict — DECLINED (feature), with one open one-field diagnostic.**

---

## 6 · Doc 29 F1–F8, re-verified against today's tree

The brief for this audit named the contradiction to hunt: *several entries claim BUILT or SHIPPED
and the operator is still reporting the symptom.* **The simplest explanation was checked first and
is FALSE** — every one of the seven feature branches is merged into `master`
(`feat/proposal-derived-width` `e9930e77`, `feat/developer-shell` `a6a8e04c`,
`research/node-workspace-worktree` `a92cc3db`, `feat/assistant-always-on` `fe89a731`,
`feat/repair-judgment-no-debug-node` `b9533026`, `feat/conversation-trace` `95c58c5d`,
`feat/atlas-rename` `b937f1a2`; all verified with `git merge-base --is-ancestor`). Nothing was left
on a branch.

So the contradictions are of three other kinds, and naming them is the point of this table:
**(D) deployment** — it is on `master` and not in the running process or bundle (§0);
**(H) half a surface** — the backend shipped and the operator's control did not;
**(N) narrower than the ask** — what shipped answers a smaller question than the one asked.

| entry | doc 29 status | today | kind |
|---|---|---|---|
| **F1** run width from proposals | BUILT 2026-08-13 | present and enabled (`config.py:504`, `widths.py:95`, `types.py:317`) — and **`run_width_settled` has fired 0 times in every event log on this box**. The operator's actual question was cross-run GPU contention, which F1 never addressed (see **E4**) | N |
| **F2** Developer shell | BUILT 2026-08-14, "deliberately NOT as a shell" | still true, and **doc 29 is now STALE**: `looplab/tools/dev_commands.py` (`cb3433b3`, 2026-08-17) gives the Developer operator-pinned shell commands, defaulting to an EMPTY list | — |
| **F3** worktree | DECLINED WITH MEASUREMENT | stands; the one follow-up it names is still not done (`engine/workspace.py:179` carries no byte total) | — |
| **F4** assistant always-on | SHIPPED 2026-08-13 | backend and three routes exist; **the browser cannot ARM a watch** — `ui/src/api.js:1095-1101` has GET and DELETE and no POST | **H** |
| **F5** Debug node removed | LANDED 2026-08-13 | fully confirmed, `tests/test_debug_node_removed.py` 12 passed; the operator's third clause (systemic stop vs direction retirement) was never in scope | N |
| **F6** conversation trace | SHIPPED 2026-08-13 | the seek and the episode map are real and correct — but its closing line promises an **"Operations panel"** that **does not exist in `ui/src`** (`docs/29-…:1125-1126`), and the 2026-08-17 client-side paging is not in the running bundle | **H + D** |
| **F7** Atlas → Claims & Curation | SHIPPED | the UI rename landed exactly as described; the word survives in `looplab atlas` (`governance_cmds.py:817`, echoing `Research Atlas:` at `:854`) and in the assistant's `cross_run_atlas` tool (`serve/assistant.py:1501`) | N |
| **F8** unbounded repair | LANDED 2026-08-13 | fully confirmed and **observed firing** — the live run carries a `repair_critic_verdict` event | — |

**Two corrections doc 29 should carry**, and they are the two most valuable findings of this audit
after §0:

1. **F6's "see the new Operations panel" is false.** There is no such panel and never was; the
   run-level spans are served (`/api/runs/{id}/trace`'s `unscoped` list, `runs.py:3193`) and have
   **zero consumers in `ui/src`**. The operator's "куда делись трейсы ресерчера и девелопера?" is
   therefore correct and doc 29 answered it with a promise.
2. **F4 shipped a feature the operator cannot start.** The watch store, scheduler, restart policy
   and refusal vocabulary are all real; the missing piece is one POST client and one button. A
   feature reachable only by asking the model in prose is, from the chair, not shipped.

---

## 7 · What to do first

Ordered by evidence-per-hour, not by size. The first three cost almost nothing and change what every
later measurement means.

1. **Rebuild the UI bundle** (`looplab build-ui`). It republishes to the RUNNING server with no
   restart (`serve/server.py:242-261`, `:686`), and it retires or sharpens U3, U4, U8 and part of U2
   in one step. Then bounce the UI server for the 16 server-side commits. **Until this is done, no
   UI symptom reported on this box is evidence about `master`.**
2. **Restart or explicitly re-configure the live run for E1.** `cadence_while_evaluating` set
   explicitly wins over the legacy snapshot default (`core/config.py:2526-2531`). Today there is
   **zero on-disk evidence** that the cadence fix works, on a box whose every recent run
   demonstrates the bug.
3. **Point the operator at `docs/guide/memory.md:120-144` (N4) and
   `docs/guide/configuration.md:277-292` (E4).** Two of the questions on the notebook list have
   complete, correct written answers that nobody has read.

Then, in cost order:

4. **Re-order `_research_memo`'s render** (M5, `tools/run_tools.py:764`) so the sections a caller
   acts on survive the 4,000-char head cut. Measured: `Recommended directions` is past the cut in
   **89 of 89** memos and in **194 of 212** real tool calls. This is the highest-value line-count in
   this document.
5. **One POST client + one button** for assistant watches (F4/A6).
6. **Default `skills_dir` / `prompt_dir`** (N6, `core/config.py:2000-2002`) — an unconfigured tab
   must not look like an unimplemented one.
7. **Make the concept→memory rows links** (N1, `PortfolioConcepts.jsx:121-131`).
8. **Finish the Atlas rename** on the CLI and the assistant tool (N8).
9. **A run-level agent trace panel** over the `unscoped` spans already on the wire (U5) — client-only.
10. **Durable per-attempt log offsets** (U7), as a fold-ignored diagnostic in the `phase_progress`
   shape, so the operator can see what the judges already can.
11. **Decide the Cases question** (N4). It is a decision, not a refactor, and it is already priced.

**Deliberately not recommended here:** anything in the UI/assistant/engine defect clusters that the
three parallel investigations own. This document records them so they stop living in one person's
notebook; it does not pre-empt their diagnoses.
