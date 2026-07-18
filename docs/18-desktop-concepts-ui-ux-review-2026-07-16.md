# Desktop Concepts UI/UX Review — integration concept, graph views, and product gaps

**Integration review:** 2026-07-18. The landed baseline is the dated `master` checkpoint `4d1218c`; the
post-checkpoint integration status and still-pending items are recorded in
[doc 18 §37](18-ui-ux-review-2026-07-11.md#37-post-checkpoint-ui-integration-reconciliation-2026-07-18).

**Historical origin:** the first focused checkpoint was recorded on 2026-07-16; §1.1 preserves its findings and
marks their current status instead of rewriting them as if they had never existed.

**Scope:** desktop owner UI, with emphasis on Part IV Concepts, the experiment DAG, the folded primary concept
axis, evidence references, typed relation lenses, paid lens creation/recovery, and Settings discoverability.

**Related authority:** the current integration corrections and broader UI priorities are in
[doc 18 §37](18-ui-ux-review-2026-07-11.md#37-post-checkpoint-ui-integration-reconciliation-2026-07-18);
implementation/test chronology is recorded in
[doc 21](21-full-functionality-review-2026-07-13.md#post-round-25-integration-ledger-pending-final-commit-2026-07-18).

This round deliberately does **not** validate or redesign mobile/touch/reflow. Mobile remains deferred and is
not a publication blocker for this desktop checkpoint. Existing mobile roadmap requirements are not deleted,
but no new mobile-conformance claim is made here.

## 1. Executive finding

LoopLab now has a coherent, bounded and recovery-aware **per-run Concepts workspace**. The 2026-07-18
checkpoint also removes a major semantic ambiguity: the lineage UI no longer presents “Direction” as if it
were a separate governed entity. The two durable user questions are:

~~~text
Research
  Lineage              — how current experiments derive from one another
  Concepts              — multi-label semantic structure, relationships, metrics and evidence

Lineage grouping
  Primary concept axis  — one lossy compatibility projection of folded concept memberships
~~~

The current tree/table is the right default for Concepts because it can show canonical IDs, projection
semantics, metric context, omissions and exact evidence compactly. A future Focused Concept Map should be a
coordinated secondary view with a complete relationship table, not the only way to navigate semantic
structure.

The largest remaining UI gain is therefore not another unbounded graph. It is a unified Research container,
a concept detail Inspector, a bounded synchronized map/table for one selected concept, and reproducible view
state. A primary-axis×Concept matrix is deliberately retired from the near-term plan: the axis is already one
member selected from the same multi-membership set, so such a matrix would mostly restate the source data.

### 1.1 Historical 2026-07-16 checkpoint, validated on 2026-07-18

The original review identified five P0 defects. They are retained here as chronology and are **landed** at the
current checkpoint:

| 2026-07-16 finding | Current 2026-07-18 status |
|---|---|
| paid lens creation lacked durable idempotency, metering and reload recovery | landed at the LoopLab logical-work boundary: generation/request fences, durable started/terminal events, one worker operation, same-identity replay and owner-plane recovery; bounded core transport retries remain possible |
| provenance and rollup eligibility were hidden | landed: evidence rows expose attempt, status, feasibility, membership provenance, rollup eligibility and metric availability |
| stale experiment menus could survive same-ID generation replacement | landed: run generation/attempt fences close or disable stale actions |
| rapid live projection updates could starve Concepts refresh | landed: semantic projection changes coalesce while run/generation/sequence/lens changes remain hard fences |
| selected pills and split/collapsed groups could lose identity or lie about totals | landed: canonical selected IDs, stable chip order, active-lifecycle counts, matched/total aggregates and zero-match treatment |

These fixes close the immediate correctness gate. They do **not** claim that the complete Research Space,
Focused Concept Map, governance workbench or canonical portfolio Atlas exists.

## 2. Current product truth

The owner run workspace ships a bounded per-run Concepts tree/table backed by `ConceptFrame` v1. The response
is bound to the exact run generation and captured event prefix; carries requested/captured/max sequence,
completeness and authority receipts; exposes typed lens projections; and returns self-contained,
generation-bound experiment references plus descriptive per-concept outcome rollups. HTTP success alone is not
projection truth: the browser validates identity, topology, bounds, provenance and authority before rendering.

It is **not** the canonical Research Space or Focused Concept Map. The unified Research information
architecture, Landscape/Intersections/Journey, complete paged relationship table, immutable
taxonomy/assignment releases, exact evidence-family proof and governance workbench remain open. The older
primary-axis×Concept crosswalk proposal is not an open deliverable unless a separately identified Direction
entity and a measured comprehension need are established.

| Surface | Shipped user promise | Explicit non-promise |
|---|---|---|
| Search / experiment DAG | one run’s active experiment lineage, exact experiment selection and optional grouping | not concept topology, governance or cross-run evidence |
| Primary concept axis | one compatibility grouping derived from the current folded concept memberships | not a Direction entity, taxonomy, causal claim or cross-run identity |
| Concept chip bar | canonical breadcrumb browsing plus OR selection/highlight of matching current memberships; canonical order stays stable when counts change | not a complete graph, proof system, metric sort or winner selector |
| Collapsed/expanded groups | active-lifecycle counts; a filter shows matched/total, dims zero matches and computes best/status only over matching eligible members | not evidence that excluded, aborted or tombstoned attempts never existed |
| Concepts tree/table | bounded current/historical frame; dynamic projected-relationship vocabulary; generation-bound refs; descriptive rollups and explicit metric context | not a complete taxonomy, scientific verification or Atlas |
| Create lens · paid | ask the configured provider for a validated relation-subset projection spec; the spec changes view state, not run meaning | never creates a concept, edge, assignment or governance decision |
| Atlas preview | independently fetched bounded portfolio response slices | not a Saved Scope, coherent snapshot, complete corpus or canonical concept graph |

### 2.1 Primary concept axis contract

`Primary concept axis` is deliberately labelled as a compatibility projection. For a node that has a folded
`RunState.node_concepts` row, the reader:

1. canonicalizes every membership through consolidation aliases;
2. takes distinct top-level axes;
3. chooses the lexicographically first axis; and
4. treats an explicit empty folded row as untagged/unassigned, with **no** fallback.

Only when the folded row is genuinely missing may compatibility migrate through legacy `idea.theme`, then
the first authored concept’s top-level axis. This keeps the DAG, charts, reports and Inspector in the same
post-retag/post-rename vocabulary. It also makes the lossiness explicit: additional memberships and deeper
paths are intentionally omitted.

The obsolete URL `focus` value for the old Direction surface is not silently reactivated. It is ignored with
the visible migration notice “Legacy Direction focus is no longer supported; use the Concepts filter
instead.” `theme` remains an internal/legacy wire name, not current user-facing product language.

## 3. Desktop composition and visual hierarchy

The Concepts workspace is a **table-first evidence explorer**, not another force graph squeezed above the
lineage canvas. Its desktop reading order is:

1. run identity, generation/history state and Concepts workspace selection;
2. frame identity (concept count, tagged experiments and captured sequence);
3. one **Projection lens** selector for hierarchy or projected relationships;
4. a visually secondary paid custom-lens creator with explicit charge/recovery copy;
5. **Expand concept rows**, **Collapse concept rows** and refresh controls;
6. selectable outcome columns and their run-median reference;
7. explicit metric and relationship context; and
8. the internally scroll-contained concept/evidence table.

At 1280×720 the page itself must have no horizontal overflow. The table owns its vertical overflow and keeps
the column header visible; the command bar must not cover the final evidence row. Long canonical concept IDs
wrap or truncate only with the complete accessible value retained. Default entry is the collapsed top-level
axis list: bulk expansion is explicit, and expanding hierarchy never silently expands evidence references.

Typed edge lenses are graph projections rendered as an outline. The relationship vocabulary is read from the
validated `requested_lens_spec.rels` for the active lens, so the heading and legend describe the actual
projected relation set instead of using a hard-coded “hierarchy” label. `co_occurs` additionally discloses that
its links are derived from the current frame memberships, not recorded edge claims. Every node shows its complete canonical
ID; the selected primary display parent defines indentation; and `+N links` is an expandable disclosure with
exact additional parent IDs, usable by keyboard and pointer. Counts are explicitly **displayed concept nodes**,
not relationship counts. Loading and error states retain the selected projection's vocabulary and context
instead of falling back to a contradictory hierarchy label. The outline never implies that the chosen display
parent is the only relation or that an edge projection is a taxonomy hierarchy.

Search keeps concept chips compact, but selected pills preserve canonical ancestry to distinguish equal leaves
such as `model/x` and `data/x`. Chips are sorted by canonical ID, so an SSE count change does not move
keyboard/click targets. A drilled exact-level membership remains a trailing “· here” choice instead of
vanishing.

Expanded topology bands show matched/band counts and disclose a split when one semantic group spans multiple
bands. Collapsed cards show `matched/total`, compute status/best/trajectory only from matching current
members, and render a zero-match card as dimmed with “no matching experiments” rather than leaking a metric
from another concept.

## 4. Evidence, lifecycle and metric comprehension

The experiment reference is the hand-off from a concept claim to its run evidence. Its visible row—not a
hover-only tooltip—exposes:

- experiment ID **and attempt/generation**;
- lifecycle status;
- feasible / infeasible / constraint-unreported state;
- membership provenance (for example researcher-authored or classifier-derived);
- robust metric availability/value and whether that observation is included in the rollup;
- current-frame champion state; and
- whether the exact attempt can open in Inspector from the displayed snapshot.

Tooltips and accessible names may expand this summary, but cannot be the only place where provenance or
eligibility exists. Disabled stale-attempt links explain the generation mismatch rather than opening a current
experiment with the same numeric ID.

Every current experiment projection uses the same lifecycle boundary: tombstoned nodes and IDs in
`aborted_nodes` remain in replay/audit history but are excluded from DAG geometry, chips, grouping counts,
collapsed aggregates, charts and ConceptFrame current metrics/references. The post-checkpoint integration also
keys live refreshes and report projections to that lifecycle boundary. A historical frame retains its own
generation-bound evidence; it never joins a reference to a later attempt with the same numeric ID.

`best`, `mean`, `Δ best`, `Δ mean`, evaluated count and first-touch are selectable columns. The
visible metric context states that the table uses the primary objective, that its display name/unit are not
recorded, whether the run minimizes or maximizes, and that positive Δ is orientation-normalized to mean
“better”. The run median is the descriptive comparison reference. Rows remain in the active
hierarchy/relationship projection order: choosing a metric column does **not** create an implicit Δ sort.
Any future ranking control must be explicit, labelled and reversible.

Multi-membership experiments count fully in every membership and that rule is disclosed; membership is not
independent evidence and does not directly score or select the champion. `concept_pivot` and
`graded_novelty` may separately steer exploration or proposal admission when enabled, so guides must not call
concepts universally inert.

## 5. Resource, generation and paid-action safety

Concept GETs retain the last validated frame during refresh and distinguish loading, stale, partial,
historical, authoritative empty and unavailable states. Semantic changes to memberships, consolidation,
lifecycle, status, feasibility, metrics, champion or typed edges coalesce into a current projection refresh;
run, generation, historical sequence and lens changes remain hard semantic fences.

Every run-local menu, selected experiment and paid intent is generation-scoped. Replacing a run with the same
ID and reusing node `#7` closes the old node-action menu, clears stale callbacks and requires selection in the
new generation.

The paid Concepts lens path is production-safe at this checkpoint within the following explicit boundary:

- the browser saves one run-, generation- and prompt-bound request identity before dispatch and fails closed
  when tab storage cannot preserve it;
- the server checks the exact generation and the same bounded ConceptFrame input used by the free view;
- cap-only partial frames may be used and remain labelled partial, while corruption-class completeness
  reasons block provider construction;
- a durable `concept_lens_started` claim precedes background work; one worker performs one logical
  `tool_call_once` operation through the metered run client, without parser-repair or outer same-identity
  redispatch;
- durable completed/failed/declined receipts and run usage accounting make terminal replay observable;
- the same identity rejoins or replays the existing LoopLab job instead of minting another logical operation;
- reload recovery is an owner-plane GET that exposes no prompt, digest, paid idempotency key or resolution
  key; it can poll the exact job, restore a terminal, or report orphan/conflict; and
- resolving an orphan uses a separate resolution idempotency key plus exact request ID and started sequence.
  It sends no provider retry and truthfully leaves provider completion/billing unknown.

Review links and historical snapshots cannot create paid lenses. A new paid identity remains disabled while
an unresolved or conflicting claim exists, and paid storage/accounting failure is fail-closed.

Provider-side exactly-once billing is still impossible without provider-supported idempotency. The core client
may make bounded transport retries after timeout/reset/429/5xx/empty/decode failures, and an earlier request may
already have been accepted or billed. LoopLab therefore guarantees one durable worker/logical invocation and
no parser-repair or outer same-identity redispatch—not one HTTP/provider attempt. Provider completion, billing
and returned-usage reconciliation can remain explicitly unknown.

## 6. High-impact next UI changes

### 6.1 One Research container

Rename the overloaded run-level `Search` workspace to **Research** and expose
**Lineage | Concepts** as the primary lenses. Keep **Primary concept axis** as an explicitly lossy grouping
control inside Lineage, not a peer governed entity. Landscape, Intersections, Journey and Focused Map belong
under Concepts. Overview remains a summary, not a third pressed lens.

Selection is one shared subject (experiment attempt or concept), while each lens keeps its own
camera/scroll/filter return context. The Inspector changes schema with that subject; it does not mix an
experiment action menu with concept governance.

### 6.2 Concept detail instead of hover archaeology

Selecting a concept opens a stable detail Inspector with:

- canonical ID, label/alias state, release/revision and provenance;
- incoming/outgoing typed relations with direction, confidence and provenance;
- exact experiment memberships and their attempt identities;
- descriptive rollups with eligibility and omissions;
- history/rename/merge/split state when governance identity exists; and
- **Inspect evidence**, **Focus here** and **Back to prior view** as separate actions.

### 6.3 Coordinated Focused Map and relationship table

The map defaults to a bounded neighbourhood around one selected concept and uses distinct grammars for
hierarchy, usage/composition, co-occurrence/similarity and assignment. Its legend is generated from the
active relationship vocabulary and never relies on colour alone. The synchronized table is the complete path
for the declared query/access receipt. Graph edge selection focuses the row; table selection highlights the
edge without silently re-rooting.

The map always exposes scope, generation/release, truncation, hidden-node/edge counts and a “why this edge?”
path into evidence. Layout and camera are view state, not domain meaning.

### 6.4 Retired crosswalk and migration explanation

Do not build a primary-axis×Concept matrix from the current model. The primary axis is already a deterministic,
lossy member of each node's folded concept set, so crossing it against that set risks presenting a derived
layout slot as an independent entity. Explain migration in-place instead: label the grouping **Primary concept
axis**, disclose omitted memberships, link to the Concepts filter/detail, and keep the legacy-focus notice. A
crosswalk becomes valid only if a future Direction receives its own opaque identity/provenance and usability
research shows that the extra view answers a distinct question.

### 6.5 Reproducible analysis state

A saved/shareable concept view must bind run generation, captured sequence, lens/spec, selected concept,
filters, visible metric columns, expanded hierarchy/evidence and map camera. Restoring a stale generation
opens a clearly historical read-only state; it never silently retargets to current evidence.

## 7. Functional gap and priority ledger

| Status | Priority | Capability | Why it materially changes UX |
|---|---|---|---|
| landed 2026-07-18 | P0 | visible provenance/eligibility; generation-reset fencing; coalesced live refresh; canonical stable chips; lifecycle-safe matched/total groups | removes stale actions, hidden evidence, moving controls, cross-filter metrics and live starvation |
| landed 2026-07-18 | P0 | durable/idempotent/metered logical paid-lens job with same-identity Resume and owner-plane recovery | prevents blind logical redispatch and makes lost-tab work inspectable; provider retries/billing ambiguity remain explicit |
| retired / conditional | — | Primary-axis×Concept crosswalk | do not reify a lossy member of the concept set; reconsider only with a distinct Direction identity and measured user need |
| open | P1 | concept detail Inspector with typed relations, membership history, aliases and proof links | replaces hover archaeology with an evidence workflow |
| open | P1 | synchronized Focused Map + complete relationship table | gives a bounded spatial view without making the graph the only accessible source |
| open | P1 | Landscape, Intersections and Journey | answers portfolio shape, co-occurrence/gaps and change-over-time questions that a tree cannot |
| open | P1 | saved/shareable concept view state | makes analysis reproducible rather than ephemeral browser state |
| open | P2 | release-pinned taxonomy/assignment history, impact preview and reversible governance | separates observed claims from approved portfolio meaning |
| open | P2 | immutable Saved Scope + compatible Atlas comparison/proof/export | enables defensible cross-run decisions rather than overlapping live previews |

## 8. Settings discoverability and concurrency

The Settings UI loads a versioned server-owned editor catalogue with **10 groups and 143 of the 166 direct
`Settings` fields**. Its default **Essential** mode contains 17 high-frequency keys; search spans the full
catalogue. The catalogue is intentionally curated rather than exhaustive. Fields outside it remain
configurable through environment/config inputs and survive sparse UI writes.

The packaged catalogue is v1 and the HTTP/editor contract is v2. Its weak ETag is a semantic cache revision
for schema metadata, **not** a settings mutation token.

Global settings and the write-only secret store carry separate opaque `settings_revision` and
`secret_revision` CAS tokens. The current browser sends the observed token, receives structured 409
conflicts, keeps the operator’s draft, refreshes authoritative state and requires a deliberate retry. Per-run
config uses a separate 64-character `config_revision`; its current editor sends `expected_revision` under
its own equivalent read/compare/merge/write locking contract. Token omission remains a legacy API
compatibility path at the raw HTTP boundary, not the current UI contract.

`concept_pivot`, `graded_novelty` and `capability_expansion` are present with dependency and behavioral
copy. Contextual entry points should open the relevant setting with its effective value, dependency, apply
timing, cost/risk and disabled reason. A generic Settings dump is not a substitute for explaining why a
Concepts capability is unavailable in the current run.

## 9. Desktop acceptance gate and exact evidence

The following bullets are the **remaining release acceptance contract**, not a claim that every journey has
already passed:

- Browser journeys must cover a concept-rich run and a concept-authored run with no legacy theme at 1280×720
  and one wider desktop viewport; page/table overflow, sticky header, command-bar clearance and console errors
  must be inspected from the mounted production build.
- Concept-only runs derive one deterministic **primary concept axis** from folded canonical memberships;
  reports, charts, grouping and Inspector use the same compatibility reader. An explicit empty folded row
  never revives a legacy theme.
- Chip controls stay in canonical order as counts change; retired aliases retarget through consolidation;
  tombstoned/aborted experiments disappear from current counts and highlights.
- Active concept filtering makes expanded and collapsed groups show honest matched/total values; zero-match
  cards expose no best/status from excluded members; split topology bands disclose their wider total.
- Expanding all hierarchy nodes does not expand refs. Opening refs shows every field in §4. Switching typed
  lenses updates the relationship vocabulary, retains complete IDs and discloses secondary links.
- Metric context names objective scope, missing display name/unit, orientation and Δ semantics. The table does
  not silently reorder by Δ.
- Rapid semantic projection changes settle to a current frame without request starvation. Replacing the same
  run ID closes an open node menu even when the numeric node survives. A legacy `focus` URL produces the
  migration notice and no hidden filter.
- A paid lens browser journey must use one durable logical identity, reach one terminal result, record the
  returned run usage, survive same-identity replay/reload without another LoopLab logical dispatch, expose no
  prompt/paid key/credential through recovery, and label provider completion/billing ambiguity honestly.
- Two Settings tabs and two per-run Config editors exercise accepted saves, structured revision conflicts,
  retained drafts and deliberate reconciliation. Schema ETag changes are not treated as mutation conflicts.
- Full React tests, production build, focused backend durability/concurrency tests, strict MkDocs and
  `git diff --check` must pass from the final integrated tree.

Release evidence recorded for the published source/UI commit `f956685`:

| Check | Result and boundary |
|---|---|
| full + focused UI | **PASS — 571/571 full suite; 62/62 focused final selection** |
| paid OpenRouter MiniMax smoke | **PASS** for bounded text + structured `looplab smoke`; it is not a Concepts-lens route/E2E or an exactly-once billing proof |
| full Python run | **PASS** — 3,892 tests collected; complete run exited zero with external `basetemp`; the prior six failures were repository-ancestry artifacts |
| route landmark/focus | **CODE + AUTOMATED + DESKTOP BROWSER PASS** — named ready/state-card run mains, Concepts region, exact URL/Back/Forward focus, and no focus theft on local tabs |
| production/bundle | **PASS** — build passed; total JS gzip 350,183 B ≤ 350,208 B and Atlas increment 7,415 B ≤ 7,680 B |
| desktop visual replay | **PASS with boundary** — production-asset route and relationship/fallback journeys passed. A 1280px bulk-action wrap was found and fixed in `f956685`; the final CSS is automated/build-verified, but no second post-fix screenshot is claimed. Paid-lens recovery is not called browser E2E. |
| strict docs, diff hygiene and publication | **PASS** — strict MkDocs + `git diff --check`; `f956685` pushed to `origin/master` and equality confirmed before this docs-only certificate |

This is a desktop gate only. No mobile/touch/reflow result is inferred from it.
