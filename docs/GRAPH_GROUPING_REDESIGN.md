# LoopLab graph layout: groups that don't read as ragged stripes

**Status:** proposal / decision doc
**Audience:** maintainer of `layoutWithGroups`
**TL;DR:** Ship a **mode-conditional fixed-grid pack** (deterministic, append-stable, zero-dep, stays in React Flow) for the two modes that actually have the problem (theme, niche), gated by an **auto-collapse policy** that reuses the collapse pipeline we already have. Keep today's layered layout untouched for operator/metric/none. Do **not** adopt cola/ELK/G6/Cytoscape — they are iterative, non-deterministic, and most are heavier than the entire current UI. Use a fixed-column grid pack rather than a squarified treemap, because only the fixed grid is stable under live append. Treat BubbleSets as an optional, narrowly-scoped second channel for the merge story.

> Produced by an 11-agent research workflow (6 technique families web-researched, grounded in two real ~100-node runs, adversarially critiqued). Families surveyed: constraint layout (WebCoLa), Group-in-a-Box, cluster-aware layered (dagre/ELK), force + similarity embedding (UMAP/fcose), set overlays (BubbleSets/Kelp), semantic-zoom/combos (G6/Cytoscape).

---

## 1. Root cause

In `layoutWithGroups` (`ui/src/util.js:198–212`) `y = depth * YS`, where `depth` is longest-path-from-root. A group is a *property* (theme/operator/metric/niche), and properties recur across iterations — so a theme's members land on rows 2, 5, 9, …. The layout can only reorder a group *within* each locked row. `regionGeometry` (`ui/src/grouping.js:83`) then draws an axis-aligned bounding rect over wherever they fell ⇒ a **tall, ragged vertical stripe**. Because two stripes can share rows, the boxes **overlap**.

The empirics (from `runs/mle_minimax_m3_100` and `runs/mle_deepseek_flash_100`) make this structural, not a tuning problem:

- **54–67% of theme-group pairs** have overlapping vertical row-spans. You cannot make those disjoint by reordering within fixed rows — it is geometrically impossible. Non-overlap *requires* moving nodes off their exact depth row.
- ~100 nodes live in only **8–11 rows** (one row holds **32 nodes**). Depth is a *coarse* axis with abundant horizontal room and almost no vertical room. There is slack to exploit.
- The graph is **84–89% single-parent** (a tree) with a **10–16% merge minority**, and *every* merge carries `operator=merge`, `theme=None`. The backbone is cleanly hierarchical; merges are a small, special, themeless set.

A pre-existing wrinkle worth naming: even today, layer centering (`offset = -span/2`, `util.js:210`) re-centers a whole layer when a node is appended, so existing nodes already shift by up to half a slot per tick. Any new packing must not amplify this.

---

## 2. The core trade-off

Three goals pull the y-axis in three directions, and they are mutually exclusive on a single axis:

| Goal | Wants the y-axis to encode… |
|---|---|
| **DAG flow** (root-at-top, depth = iteration) | global longest-path depth |
| **Similarity-as-distance** (ask #3) | feature/embedding distance |
| **Square, non-overlapping group boxes** (asks #1/#2) | group membership (pack each group locally) |

Pick which one owns y globally; demote the other two to "soft / within-band." LoopLab's load-bearing signal is DAG flow, and the data says depth is coarse (≈10 bands). That is exactly what lets us keep depth as a **band** while repacking groups *inside* a band — we relax depth from a hard row to a soft band, and pay for non-overlap with a small loss of within-band depth precision.

A second axis of the trade-off the data forces on us: **group-by mode**. Boxing only makes sense where groups are many and medium-sized. In **operator** mode one group holds 70–80% of nodes (its box ≈ the whole graph); in **metric** mode the three terciles are full-height by construction. Boxing there is meaningless. So the packed layout is **conditional on mode**, not a new global default.

---

## 3. Candidate directions

### A. Banded Grid-Pack — depth owns y *softly*; groups pack into fixed-grid cells within a band *(recommended, theme/niche only)*

Depth collapses from ~10 hard rows into ~5 **bands** (adjacent depths). Within a band, each group's members fill a **fixed-column grid** (`cols = ceil(sqrt(maxExpectedGroupSize))`), filling top-to-bottom, left-to-right. `regionGeometry` draws the cell as today, but now it's a tight near-square block, not a stripe. The champion spine still flows downward across bands. Themeless merge nodes float in a band-local bridge column, not a cell.

- **Ask 1 (square/compact):** YES. A fixed grid of ~`⌈√N⌉` columns packs N members into a near-square block. The 35-node `gbm-log-targets` stripe becomes a ~6×6 block.
- **Ask 2 (merges):** Merges (`theme=None`) are not packed into any cell; they sit in the bridge column and are drawn as **custom leader edges** (see §5, Phase 2 — this is net-new edge-routing code, not free reuse).
- **Ask 3 (similarity-distance):** PARTIAL by default, **upgradable cheaply** — order cells *within a band* along x by group-centroid feature distance (§5, Phase 1b), so similar groups sit adjacent. Cross-band similarity stays depth-bounded; that is the price of keeping DAG flow.
- **Dependency cost:** **zero.** A fixed-grid pack is ~20 lines. No renderer change; the `pos` contract is unchanged.
- **Stability:** EXCELLENT and *real*. A new member appends to the next free grid slot of its cell — **existing members never move**, because the grid's column count is fixed up front, not recomputed from current member count. (This is the decisive reason we choose a fixed grid over a squarified treemap; see the explicit rejection below.)

**Why fixed grid and not a squarified treemap (Bruls / `d3-hierarchy.treemap`).** A squarified treemap optimizes aspect ratio as a function of *current member count*: adding the 26th node to a 25-node group reflows a 5×5 block into 5×6, moving every existing member — and the cell's changed size then shifts neighbor cells and their `regionGeometry` boxes. That is exactly the "whole forest jumps under the user" failure we disqualify d3-force for. A treemap is deterministic *given the full graph* but **not stable under append**, which is the property we actually need on the SSE hot path. The fixed grid trades a little wasted whitespace in half-full cells for true append-stability. (As a bonus, this also sidesteps `d3-hierarchy`, which requires a nested `d3.hierarchy()` object, not a flat array, and would pull `hierarchy` + tiling code — so even the "tree-shakeable import" alternative loses to ~20 lines vendored.)

### B. Group-in-a-Box — group membership owns y; DAG flow goes box-local

A treemap tiles the canvas into one non-overlapping rectangle per group; inside each tile the current depth layout runs box-locally. React Flow sub-flows (`parentId`) are the native substrate.

- **Ask 1:** strongest — non-overlap is structural.
- **Ask 2:** WEAKEST — React Flow nested auto-layout breaks on child-to-outside edges, so all 10–16 merges must be hand-routed at top level anyway.
- **Stability / cost:** depth becomes **box-local** — a row no longer reads as a global iteration, discarding the signal LoopLab values most. This is A's trade-off inverted, and it loses the wrong thing. **Rejected.**

### C. Bubble Overlay — positions unchanged; make multi-membership legible

A concave BubbleSets isocontour drawn behind a focused subset; a merge node sits *inside both* parent bubbles, turning "crosses a boundary" into "belongs to both."

- **Ask 2:** best merge story in the study.
- **Caveat:** after A, each group is already a compact rounded-rect block (that is literally why `regionGeometry` exists), so BubbleSets is **redundant for group membership**. Its only remaining value is the merge picture. **Keep as a narrowly-scoped fallback: bubbles around the ≤16 merge nodes and their parents — not a general focused-group overlay.**

### D. Tidy Button — one-shot WebCoLa "settle," then freeze

A manual action runs WebCoLa once (`groups` + `avoidOverlaps` + `flowLayout('y')`) into genuinely square non-overlapping boxes, freezes positions into the cache, and resumes deterministic incremental layout. webcola is abandoned (2019), O(n²), soft-constraint, and merge children with two-box parents are its adversarial case — disqualified as a *live* layout. Defensible only as an opt-in freeze-frame escape hatch. **Not v1.**

---

## 4. Recommendation

**Primary: A (Banded Grid-Pack), mode-conditional (theme/niche only), gated by auto-collapse. Fallback channel: C (Bubble Overlay), scoped to merge nodes only.**

1. **Mode-conditional shrinks the blast radius.** We are *not* rewriting the default layout. theme/niche → banded grid-pack; operator/metric/none → today's layered layout, unchanged (the complaint is theme-driven; the layered layout is fine where groups are few or full-height). A reader implementing Phase 1 has an explicit branch, not an unanswered question.
2. **Dependency-light is a hard product value.** The whole layout is a few hundred lines of zero-dep JS. ELK is ~1.5 MB (bigger than the UI) and async; G6/Cytoscape *replace* React Flow and re-settle non-deterministically; cola is abandoned + O(n²) + soft. A is **zero new runtime deps** and slots into the exact same `pos` contract.
3. **Stability is the real constraint, not perf.** Layout re-runs on every SSE frame and `fitView` never re-fires after mount. Only a function that is *stable under append* survives this. The fixed-grid pack is; a treemap and every force/constraint engine are not.
4. **The merge math is cheap because merges are rare and uniform** — 10–16 nodes, all `operator=merge`/`theme=None`. They don't belong in any box; route them as custom leader edges. This is a ≤16-edge routing feature, not a layout-engine problem — but it *is* net-new code, owned in Phase 2.
5. **Depth is coarse — that's the gift.** Banding into ~5 bands barely degrades the iteration signal while creating vertical slack to pack groups square, keeping the DAG flow that B and the force family throw away.

---

## 5. Direct answers to the four questions

1. **Square-ish groups + non-overlapping?** **Yes — by relaxing `y = global depth` into `y = band`, and only in theme/niche mode.** Within locked depth rows it is provably impossible (54–67% span overlap). Band depth into ~5 bands and fixed-grid-pack each group inside a band ⇒ near-square, non-overlapping cells. (Guaranteed-perfect squares would need GIB tiling or a "Tidy" cola pass; the banded grid gets ~90% there with zero deps and full append-stability.)

2. **How are merges resolved if groups are boxed?** Don't box them. All merges are `theme=None`/`operator=merge`, already ungrouped. Float them in a band-local bridge column and draw the ≤16 cross-cell edges as **custom leader edges** — a new orthogonal/waypoint edge type that routes *around* cell bounding boxes (default React Flow beziers would otherwise cut straight through packed cells). Note honestly: `rerouteForCollapse`'s dedup does **not** solve this — in the expanded case nothing is collapsed, so it returns the identical edge set; a fan-in to 7 distinct targets stays 7 edges. We reuse its `S+'>'+T` *dedup key* for any genuinely duplicate endpoints, but the geometric routing is new. Optionally enclose merges in both parent bubbles via overlay C. Do **not** delegate cross-box edges to any nested auto-layout — both dagre and RF sub-flows break on exactly that.

3. **Similarity-distance (similar = closer)?** **Mostly, cheaply.** Beyond the obvious intra-group ≪ inter-group within a band, we **order cells within each band along x by group-centroid feature distance** — a 1-D ordering of group centroids that replaces the current cluster-mean-barycenter tiebreak. It is a few lines, stays deterministic, and preserves DAG flow, so "similar groups are adjacent" comes nearly for free. Full multi-feature "near = similar on both axes" requires a UMAP/force embedding that **discards DAG flow**; offer that only as an optional alternate mode, fit off the SSE path (`umap-js` `transform()` for incremental adds), never the default.

4. **Auto-collapse / semantic zoom?** **Yes — highest-value, lowest-cost item; ship it first.** The entire pipeline already exists: `collapsed:Set` + `rerouteForCollapse` + `groupAggregate` + `SuperShell` + `focusSet`/`champSet` + `LodWatcher` hysteresis. Auto-collapse is purely a **policy that fills the Set** — zero new rendering code. But it must **default off during a live run** and on only for finished/reopened runs, and must hard-pin `groupOf(workId)` as never-collapse — otherwise it will hide the very node the user is watching grow the instant it stops being champion.

---

## 6. Phased implementation

### Phase 0 — Auto-collapse policy *(low-risk symptom fix; ship first)*
A pure function `autoCollapseSet(state, {focusSet, champSet, workId, zoom, groupMode}) → Set<groupKey>` feeding the existing `collapsed` Set.

- **Never-collapse (hard):** `groupOf(workId)` (the actively-growing group — `workId` lives in `util.js:35`; add the `groupOf` lookup), the focused group, any group on the champion spine.
- **Collapse candidates (in priority):** off-`focusSet` groups; "settled" themes (best metric flat for N iterations — `groupAggregate` already exposes `best`/`series`); everything below a *deeper* zoom threshold under the existing `LOD_ON=0.42` band.
- **Live-run default OFF.** On a live run `focusSet` is usually null (`Dag.jsx:200`), so the fallback would collapse the most-recently-active theme — exactly what the user wants to watch. Enable auto-collapse only for finished/reopened runs unless the user opts in.
- **Guardrails:** hysteresis on every threshold (reuse the `LOD_ON`/`LOD_OFF` dead-band) so groups don't flap; disable in **operator** mode (one dominant group) and **metric** mode (terciles) — collapse is meaningless there.
- **Files:** new `autoCollapseSet(...)` in `ui/src/grouping.js`; wire into `ui/src/RunView.jsx` where `collapsed`/`toggleGroup` live; drive the zoom signal off `LodWatcher` in `ui/src/Dag.jsx`, debounced, kept out of the layout memo.

### Phase 1 — Banded fixed-grid pack, mode-conditional *(the primary bet)*
Rewrite the **x/y assignment tail** of `layoutWithGroups` (`ui/src/util.js:198–212`); keep the depth + barycenter machinery (`util.js:138–187`) as the *ordering seed*.

- **Mode gate at the top of the tail:** if `groupMode ∈ {operator, metric, none}`, run today's layered assignment unchanged and return. Only `{theme, niche}` enter the pack.
- **Band:** `band = floor(depth / K)` (start `K=2`, ~5 bands for depth 10); `y = bandTop(band) + localY`.
- **Cell:** per `(band, groupKey)`, lay members into a **fixed-column grid**, `cols = ceil(sqrt(maxExpectedGroupSize))`, filling row-major. Member slot is keyed on node id so **appended members take the next free slot and never reflow placed ones**. Cell order within a band keyed on mean barycenter (stable). Themeless entities → band-local bridge column.
- **Position cache (mandatory):** add a `useRef` `pos` cache in the layout memo. On each tick, pinned (already-placed) nodes keep their cached coordinates; only genuinely new entities are assigned. This is load-bearing for Phase 1 (not just the Phase 4 cola escape hatch) — it is the belt to the fixed grid's suspenders and absorbs the pre-existing `offset=-span/2` re-centering wobble.
- **Keep the `pos` output contract identical** (`n:<id>` / `super:<key>`, `{x,y}`) so `regionGeometry`, `GroupRegion`, `SuperShell`, edge rerouting, the research lane (`max(pos.y)`), and `MapView` all keep working untouched.
- Re-tune `regionGeometry` `pad` (`grouping.js:83`) for the tighter cells; the rounded-rect path already draws a clean box around a compact cluster.
- **Global-extent reframe:** banding compresses total height and the grid changes total width, so a viewport that mounted on the old layout will be framed on empty space after the first SSE tick. Either ship the banded layout **from frame 0** (so the single mount `fitView` at `Dag.jsx:304` is correct) or fire a one-shot `fitView()` on layout-mode change from `RunView`.
- **Verify stability empirically:** replay `runs/mle_minimax_m3_100/events.jsonl` and `runs/mle_deepseek_flash_100/events.jsonl` frame-by-frame; assert that every already-placed node's `{x,y}` is byte-identical after each append (the no-jump contract).

### Phase 1b — Similarity ordering of cells *(cheap ask-#3 win, folds into Phase 1)*
Replace the cluster-mean-barycenter tiebreak (`util.js:181`) for cell x-order *within a band* with a 1-D ordering of **group centroids by feature distance** (seed by barycenter to stay deterministic and break ties stably). Deterministic, DAG-preserving, ~a few lines. Delivers "similar groups adjacent" without UMAP.

### Phase 2 — Custom leader edges for merges *(own the scope)*
A new React Flow edge type (orthogonal/waypoint) for inter-cell edges (source/target in different cells), routed *around* cell bounding boxes from `pos`. Reuse `rerouteForCollapse`'s `S+'>'+T` dedup key for any duplicate-endpoint edges, but treat the routing geometry as new code. Wire in `ui/src/Dag.jsx` where edges are built. Scope: the ≤16 merge edges.

### Phase 3 — Bubble overlay for merges *(optional polish; verify the lib first)*
**Verify `@upsetjs/bubblesets-js` maintenance + API before committing** (claimed MIT, zero-dep, emits an SVG `d` string — confirm in-tree, don't assume). If it survives: render its `d` path in a new node type modeled on `GroupRegion`/`groupLane` in `ui/src/groupnodes.jsx`, positioned behind `exp` nodes in flow coords (inherits the viewport transform). Scope **narrowly to merge nodes and their parents** (≤16 nodes, ≤ a handful of bubbles) — *not* a general focused-group overlay, which after Phase 1 would draw a fancy contour around an already-square block. Throttle/memoize; never compute inside the layout memo.

### Phase 4 — "Tidy" button *(bet; only on user demand)*
One-shot WebCoLa settle (`groups`+`avoidOverlaps`+`flowLayout('y')`), freeze positions into the `pos` cache, resume deterministic incremental layout. Off the SSE path. Adds an abandoned dependency for a manual action — justify only by explicit user demand.

---

## 7. Files to touch (summary)

| File / function | Change |
|---|---|
| `ui/src/util.js` `layoutWithGroups` (l.198–212) | Mode gate (theme/niche → pack; else unchanged); replace x/y tail with **banded fixed-grid pack**; add `useRef` position cache; **keep** depth+barycenter ordering (l.138–187) and the `pos` contract. Phase 1b: centroid-similarity cell ordering (l.181). |
| `ui/src/grouping.js` `regionGeometry` (l.83) | Re-tune `pad` for compact cells (path logic unchanged). |
| `ui/src/grouping.js` new `autoCollapseSet(...)` | Phase 0 policy; hard-pin `groupOf(workId)`, default off live. Reuse `rerouteForCollapse` (l.94) dedup *key* for Phase 2. |
| `ui/src/Dag.jsx` | Wire auto-collapse signal (debounced off `LodWatcher`); add custom **leader edge type** for inter-cell merges; ensure mount `fitView` (l.304) frames the banded extent (or fire one-shot on mode change). |
| `ui/src/RunView.jsx` | Feed `autoCollapseSet(...)` into the existing `collapsed` state; optional one-shot `fitView()` on layout-mode change. |
| `ui/src/groupnodes.jsx` | (Phase 3) BubbleSets `GroupRegion`-style node, scoped to merge nodes. |

**Net:** Phase 0 + Phase 1/1b ship the user's asks with **zero new runtime dependencies**, true append-stability under SSE, the DAG flow preserved as bands, and a cheap real win on similarity-distance — while *shrinking* scope by leaving operator/metric/none on today's layout. The heavyweight engines (ELK ~1.5 MB, cola abandoned, G6/Cytoscape renderer-swaps) are the wrong tool for a tuned, dependency-light, recompute-every-tick layout.

---

## 8. References

- **Layout / contract:** `ui/src/util.js` `layoutWithGroups` (l.127–214; depth+barycenter l.138–187; x/y tail + `offset=-span/2` re-centering l.198–212; `workId` l.35).
- **Grouping:** `ui/src/grouping.js` `regionGeometry` (l.83–89), `rerouteForCollapse` dedup (l.94–109), `groupAggregate` (l.112+).
- **Render / SSE:** `ui/src/Dag.jsx` (layout memo + deps; `focusSet` null-when-unselected l.200; single mount `fitView` l.304); `ui/src/RunView.jsx` (`collapsed`/`toggleGroup` state); `ui/src/groupnodes.jsx` (`GroupRegion`/`groupLane`).
- **Replay fixtures:** `runs/mle_minimax_m3_100/events.jsonl`, `runs/mle_deepseek_flash_100/events.jsonl`.
- **Techniques:** fixed-column grid pack (vendored, ~20 lines); squarified treemap (Bruls et al. 2000 / `d3-hierarchy.treemap`) — *evaluated and rejected for append-instability*; BubbleSets (Collins et al.) via `@upsetjs/bubblesets-js` — *verify before adopting*; WebCoLa (`flowLayout`/`avoidOverlaps`) — *escape-hatch only*; UMAP (`umap-js` `transform()`) — *optional alternate mode only*; Group-in-a-Box (Rodrigues et al. 2011, SocialCom); forceInABox (john-guerra); SetCoLa (Hoffswell et al. 2018); AntV G6 combos; Cytoscape.js expand-collapse / fcose.
