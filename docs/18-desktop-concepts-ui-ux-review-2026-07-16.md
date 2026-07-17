# Desktop Concepts UI/UX Review — shipped authority, graph views, and product gaps

**Date:** 2026-07-16
**Scope:** desktop owner UI, with emphasis on Part IV Concepts, Directions, experiment DAG, evidence references,
typed relation lenses, paid lens creation, Settings discoverability, and recovery behavior.
**Authority:** this document is the current focused review for the shipped per-run Concepts slice. The broader
UI target and priorities remain in [doc 18 §35](18-ui-ux-review-2026-07-11.md); implementation/test chronology
is recorded in [doc 21](21-full-functionality-review-2026-07-13.md).

This round deliberately does **not** validate or redesign mobile/touch/reflow. Mobile remains deferred and is
not a publication blocker for this desktop checkpoint. Existing mobile roadmap requirements are not deleted,
but no new mobile-conformance claim is made here.

## 1. Executive finding

LoopLab now has a useful, honest **bounded per-run Concepts workspace**, but it is still one product slice
inside an information architecture originally organized around experiment lineage. The largest UI gain will
not come from adding another graph. It will come from explicitly separating three questions:

```text
Research
  Lineage    — how experiments derive from one another
  Directions — the run-local coarse narrative projection
  Concepts   — multi-label semantic structure and evidence
```

The current tree/table is the right default for Concepts because it can show exact IDs, metrics, omissions and
evidence compactly. A future Focused Concept Map should be a coordinated secondary view with a complete
relationship table, not the only way to navigate semantic structure.

The highest-risk current defects are state and evidence defects rather than cosmetics:

1. paid lens creation needs durable idempotency, metering and reload recovery before it is production-safe;
2. experiment provenance and rollup eligibility must be visible, not hidden in a tooltip;
3. generation changes must close stale experiment menus even when the replacement reuses the same numeric ID;
4. rapid live projection changes must coalesce rather than repeatedly abort the only Concepts request;
5. selected concept pills and split Direction bands must retain enough identity to avoid ambiguous labels.

## 2. Current product truth

Today the owner run workspace ships a bounded per-run Concepts tree/table backed by `ConceptFrame` v1. The
response is bound to the exact run generation and captured event prefix; carries requested/captured/max
sequence, completeness and authority receipts; exposes typed hierarchy projections; and returns self-contained,
generation-bound experiment references plus descriptive per-concept outcome rollups. HTTP success alone is not
projection truth: the browser validates identity, topology, bounds, provenance and authority before rendering.

It is **not** the canonical Research Space or Focused Concept Map. The unified Research information architecture,
Direction×Concept crosswalk, Landscape/Intersections/Journey, complete paged relationship table, immutable
taxonomy/assignment releases, exact evidence-family proof and governance workbench remain open.

| Surface | Shipped user promise | Explicit non-promise |
|---|---|---|
| Search / experiment DAG | one run's experiment lineage, grouping and exact experiment selection | not concept topology or cross-run evidence |
| Directions | coarse run-local compatibility grouping: legacy `idea.theme`, otherwise the first authored concept's top-level axis | ignores additional concepts/deeper paths; not governed taxonomy, causality or cross-run identity |
| Concept chip bar | browse concept paths and OR-filter/highlight matching experiment memberships | not a complete graph, proof system or winner selector |
| Concepts tree/table | bounded current/historical frame; shipped/validated lenses; experiment refs; descriptive rollups | not a complete taxonomy, scientific verification or Atlas |
| Create lens · paid | ask the configured provider for a relation-subset projection spec; the spec changes view state, not run meaning | never creates a concept, edge, assignment or governance decision |
| Atlas preview | four independently fetched, overlapping bounded portfolio response slices | not a Saved Scope, coherent snapshot, complete corpus or canonical concept graph |

`Direction` is the only user-facing term for the compatibility projection. `theme` remains an internal/legacy
wire name. A displayed Direction summary is descriptive observed data, never an effect or winner claim.

## 3. Desktop composition and visual hierarchy

The Concepts workspace is a **table-first evidence explorer**, not another force graph squeezed above the
lineage canvas. Its desktop reading order should be:

1. run identity, generation/history state and Concepts workspace selection;
2. frame identity (`N concepts`, tagged experiments and captured sequence);
3. one primary hierarchy-lens selector;
4. a visually secondary paid custom-lens creator with explicit charge/recovery copy;
5. hierarchy expand/collapse and refresh controls;
6. selectable outcome columns and their run-median reference;
7. the internally scroll-contained concept/evidence table.

At 1280×720 the page itself must have no horizontal overflow. The table owns its vertical overflow and keeps
the column header visible; the command bar must not cover the final evidence row. Long canonical concept IDs
wrap or truncate only with the complete accessible value retained. Default entry is the collapsed top-level
axis list: bulk expansion is explicit, and expanding hierarchy never silently expands evidence references.

Typed edge lenses are graph projections rendered as an outline. Therefore every node shows its complete
canonical ID, not only an ambiguous leaf; the selected primary parent defines indentation; and omitted
secondary parents are disclosed as `+N links` with their exact IDs. The outline must never imply that one
chosen parent is the only semantic relation.

Search keeps concept chips compact, but a selected filter pill must preserve enough canonical ancestry to
distinguish equal leaves such as `model/x` and `data/x`. Direction bands split by topology/phase must disclose
their global total and continuation (`2/4 · continued` or an equivalent grammar); two visually identical
`architecture 2` cards may not masquerade as two independent Directions.

## 4. Evidence and metric comprehension

The experiment reference is the hand-off from a concept claim to its run evidence. Its visible row—not a
hover-only tooltip—must expose:

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

`best`, `mean`, `Δ best`, `Δ mean`, evaluated count and first-touch are individually selectable columns.
The run median is the descriptive comparison reference; orientation follows the run's minimize/maximize
contract. Positive/negative colour is redundant with the signed value. Multi-membership experiments count
fully in every membership and that rule is disclosed; membership is not independent evidence and does not
directly score or select the champion. `concept_pivot` and `graded_novelty` may separately steer exploration or
proposal admission when enabled, so guides must not call concepts universally inert.

## 5. Resource, generation, and paid-action safety

Concept GETs retain the last validated frame during refresh and distinguish loading, stale, partial,
historical, authoritative empty and unavailable states. A live run may update faster than one projection
round-trip; projection changes must be bounded/coalesced so rapid SSE ticks cannot repeatedly abort the only
request and leave the view refreshing forever. Run, generation, historical sequence or lens changes remain hard
semantic fences.

Every run-local menu, selected experiment and paid intent is also generation-scoped. Replacing a run with the
same ID and reusing node `#7` must close the old node-action menu, clear stale callbacks and require selection in
the new generation.

The paid lens path is production-safe only with all of the following:

- an exact generation fence and authoritative complete base frame **before** provider construction;
- a caller idempotency key whose durable claim precedes provider work, with request-digest collision checks;
- server-side concurrent-tab coalescing and terminal replay after lost HTTP responses;
- a background job receipt rather than an unbounded inline POST;
- a generation-bound run-activity lease around model work;
- the shared metered run client and durable `llm_usage` reconciliation;
- a saved browser intent before fetch, explicit same-key Resume after reload and fail-closed storage handling;
- durable validated derived/declined terminal receipts, while an orphaned/unknown paid outcome is never
  automatically retried.

Provider-side exactly-once billing is still impossible without provider-supported idempotency. LoopLab can
guarantee that it does not knowingly issue a second provider call for one durable claim; an accepted request
whose provider response is lost remains an explicit unknown outcome.

## 6. High-impact UI changes

### 6.1 One Research container

Rename the overloaded run-level `Search` workspace to **Research** and expose primary
**Lineage | Directions | Concepts** lenses. Crosswalk belongs under Directions. Landscape, Intersections,
Journey and Focused Map belong under Concepts. Overview remains a summary, not a fourth pressed lens.

Selection is one shared subject (experiment attempt, Direction or concept), while each lens keeps its own
camera/scroll/filter return context. The Inspector changes schema with that subject; it does not mix an
experiment action menu with concept governance.

### 6.2 Concept detail instead of hover archaeology

Selecting a concept opens a stable detail Inspector with:

- canonical ID, label/alias state, release/revision and provenance;
- incoming/outgoing typed relations with direction and confidence/provenance;
- exact experiment memberships and their attempt identities;
- descriptive rollups with eligibility and omissions;
- history/rename/merge/split state when governance identity exists; and
- **Inspect evidence**, **Focus here** and **Back to prior view** as separate actions.

### 6.3 Coordinated Focused Map and relationship table

The map defaults to a bounded neighbourhood around one selected concept and uses distinct grammars for
hierarchy, usage/composition, co-occurrence/similarity and assignment. The synchronized table is the complete
path for the declared query/access receipt. Graph edge selection focuses the row; table selection highlights
the edge without silently re-rooting.

### 6.4 Direction×Concept crosswalk

The crosswalk is the most important migration screen because it explains how familiar run-local Directions
decompose into multi-label concepts. Sticky Direction rows and canonical Concept columns show distinct
experiment counts, eligible denominators, unknown/partial states and exact drill-down. It never treats the
coarse Direction fallback as taxonomy.

## 7. Functional gap and priority ledger

| Priority | Missing/changed capability | Why it materially changes UX |
|---|---|---|
| P0 | visible provenance/eligibility in refs; generation-reset menu fencing; live projection coalescing; canonical selected pills; honest continued band totals | removes current deception, stale actions and live starvation |
| P0 | durable/idempotent/metered paid lens with Resume/unknown states | prevents blind duplicate charges and makes paid work recoverable |
| P1 | Direction×Concept crosswalk with counts, unknown/partial states and exact experiment drill-down | explains how familiar Directions decompose into multi-label concepts |
| P1 | concept detail Inspector with incoming/outgoing typed relations, membership history, aliases and proof links | replaces hover archaeology with an evidence workflow |
| P1 | synchronized Focused Map + complete relationship table | gives a bounded spatial view without making the graph the only accessible source |
| P1 | Landscape, Intersections and Journey | answers portfolio shape, co-occurrence/gaps and change-over-time questions that a tree cannot |
| P1 | saved/shareable concept view state (generation, sequence, lens, focus, filters, expanded evidence) | makes analysis reproducible rather than ephemeral browser state |
| P2 | release-pinned taxonomy/assignment history, impact preview and reversible governance | separates observed claims from approved portfolio meaning |
| P2 | immutable Saved Scope + compatible Atlas comparison/proof/export | enables defensible cross-run decisions rather than overlapping live previews |

## 8. Settings discoverability

The Settings UI loads a versioned server-owned editor catalogue with **10 groups and 141 of the 164 direct
`Settings` fields**, plus the nested agent-control matrix. It is intentionally curated rather than exhaustive.
Fields outside the catalogue remain configurable through environment/config inputs and must survive sparse UI
writes. `concept_pivot`, `graded_novelty` and `capability_expansion` are present with their dependency and
behavioral copy; the interface must not imply that every engine field is editable in the browser.

Contextual entry points should open the relevant setting with its effective value, dependency, apply timing,
cost/risk and disabled reason. A generic Settings dump is not a substitute for explaining why a Concepts
capability is unavailable in the current run.

## 9. Desktop acceptance for this checkpoint

- Browser journeys cover a concept-rich run and a concept-authored run with no legacy theme at 1280×720 and
  one wider desktop viewport; page/table overflow, sticky header, command-bar clearance and console errors are
  inspected from the mounted production build.
- Concept-only runs show deterministic Directions from authored top-level axes; every affected report/chart/
  group/Inspector surface uses the same compatibility reader.
- Expanding all hierarchy nodes does not expand refs. Opening refs shows all fields in §4. Switching typed
  lenses retains complete IDs and secondary-link disclosure.
- Rapid live projection changes settle to a current frame without request starvation. Replacing the same run ID
  closes an open node menu even when the numeric node survives.
- A paid MiniMax M3 journey uses one durable key, reaches one terminal lens result, records one run usage delta,
  survives same-key replay without another provider call, and exposes no credential value.
- Full React tests, production build, bundle budgets, focused backend durability/concurrency tests, strict
  MkDocs and `git diff --check` pass from the integrated current `master` tree.

This is a desktop gate only. No mobile/touch/reflow result is inferred from it.
