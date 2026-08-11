// Pure model for the GLOBAL concept view — the run list's `Concepts` representation. No React, no
// I/O; unit-tested with `node --test`.
//
// WHY THIS EXISTS. `ConceptView.jsx` draws the concept tree of ONE run. `MapView.jsx` draws projects
// and runs. The Research Atlas draws a flat, frequency-ranked leaderboard of concept ids. Nothing
// drew the concept tree ACROSS runs, which is the question "what has this lab actually studied?".
//
// WHY IT IS A FOREST AND NOT A TREE. Concept ids are `/`-paths authored per run, so each id asserts
// its OWN ancestry and nothing more (`events/digest.py::concept_rollup`: "the hierarchy is recovered
// by the reader"). Runs genuinely disagree: a real 46-run corpus here carries `feature/polynomial`,
// `feature-engineering/polynomial` and `feature_engineering/polynomial_expansion` side by side. We
// materialize every id's own ancestor chain and STOP. Two roots that look like the same word stay two
// roots, because joining them under a synthetic parent would be LoopLab asserting a taxonomy nobody
// authored — the same stance `ConceptView.jsx` states as "LoopLab does not infer a taxonomy".
// `spellingVariants` is the concession: it REPORTS ids that differ only in `-`/`_` so the operator can
// see the disagreement, and it never merges them.
//
// WHERE THE DATA COMES FROM. Every run row the run list already polls carries `concepts`:
// `{id: {count, best_metric}}`, the whole-id rollup `serve/run_projections.py` documents as "the join
// key the cross-run concept surfaces need". So this fold needs NO new request and NO server work — it
// runs over the exact array the list is showing, which is also what makes the scope shared by
// CONSTRUCTION rather than by two code paths agreeing to filter the same way.
import { conceptMap, normalizeConceptId } from './conceptId.js'
import { metricComparable } from './runIndex.js'
import { UNTAGGED } from './conceptShelf.js'

export { UNTAGGED }

// Bounds. `normalizeConceptId` already caps one id at 12 segments / 256 chars, and the server caps a
// run at `MAX_ROLLUP_CONCEPTS = 64` ids, so a 500-run page tops out around 32k ids. These are the
// render-side backstops so a pathological corpus degrades to a truncated-but-honest tree rather than
// to a frozen tab; both truncations are REPORTED, never silent.
const MAX_FOREST_NODES = 4_000
const MAX_VISIBLE_ROWS = 2_000

const isRecord = value => !!value && typeof value === 'object' && !Array.isArray(value)
const finiteOrNull = value => (typeof value === 'number' && Number.isFinite(value) ? value : null)

// Apply the versioned cross-run governance lookup before ANY lossy forest/co-occurrence fold. Splits
// depend on each run's sibling concepts and are therefore retained as raw ids with an explicit
// unapplied count; aliases/purges are exact lookup operations. The returned runs preserve the caller's
// population and every non-concept field.
export function applyConceptPolicy(runs = [], policy = null) {
  const source = Array.isArray(runs) ? runs : []
  if (!isRecord(policy) || !isRecord(policy.canonical) || !Array.isArray(policy.split_sources)) {
    return {
      runs: source, applied: false, complete: false, unappliedSplits: 0, invalidTargets: 0,
      capsuleCoverageKnown: false, unrepresentedRuns: 0, capsuleIdsOmitted: 0,
    }
  }
  const splitSources = new Set(policy.split_sources.map(normalizeConceptId).filter(Boolean))
  let unappliedSplits = 0
  let invalidTargets = 0
  const governed = source.map(run => {
    if (!isRecord(run) || !isRecord(run.concepts)) return run
    const concepts = conceptMap()
    for (const [rawId, rawValue] of Object.entries(run.concepts)) {
      const id = normalizeConceptId(rawId)
      if (!id) { invalidTargets += 1; continue }
      let target = id
      if (splitSources.has(id)) unappliedSplits += 1
      else if (Object.prototype.hasOwnProperty.call(policy.canonical, id)) {
        if (policy.canonical[id] == null) continue
        target = normalizeConceptId(policy.canonical[id])
        if (!target) { invalidTargets += 1; continue }
      }
      const value = isRecord(rawValue) ? rawValue : {}
      const previous = concepts[target]
      if (!previous) concepts[target] = { ...value }
      else {
        const a = Number.isSafeInteger(previous.count) && previous.count > 0 ? previous.count : 0
        const b = Number.isSafeInteger(value.count) && value.count > 0 ? value.count : 0
        const priorMetric = finiteOrNull(previous.best_metric)
        const nextMetric = finiteOrNull(value.best_metric)
        concepts[target] = {
          ...previous, count: a + b,
          best_metric: nextMetric == null ? priorMetric : priorMetric == null
            ? nextMetric : pickBetter(priorMetric, nextMetric, run.direction),
        }
      }
    }
    return { ...run, concepts }
  })
  const omitted = Number(policy.canonical_omitted || 0) + Number(policy.split_sources_omitted || 0)
  const capsuleCoverageKnown = Array.isArray(policy.capsule_run_ids)
  const capsuleIds = new Set(capsuleCoverageKnown
    ? policy.capsule_run_ids.filter(value => typeof value === 'string') : [])
  const unrepresentedRuns = capsuleCoverageKnown
    ? source.filter(run => isRecord(run) && typeof run.run_id === 'string'
      && !capsuleIds.has(run.run_id)).length : 0
  const capsuleIdsOmitted = Number.isSafeInteger(policy.capsule_run_ids_omitted)
    && policy.capsule_run_ids_omitted > 0 ? policy.capsule_run_ids_omitted : 0
  return {
    runs: governed, applied: true,
    complete: omitted === 0 && unappliedSplits === 0 && invalidTargets === 0,
    unappliedSplits, invalidTargets, capsuleCoverageKnown, unrepresentedRuns, capsuleIdsOmitted,
  }
}

// The `concepts` rollup of ONE run, canonicalized: `[{id, count, bestMetric}]` sorted by id, plus the
// number of entries that could not be canonicalized. `dropped` is not cosmetic — without it a run whose
// tags were all malformed reads as "never tagged", which blames the operator for a producer's bug.
export function runConceptEntries(run) {
  const raw = isRecord(run) && isRecord(run.concepts) ? run.concepts : {}
  const byId = conceptMap()
  let dropped = 0
  for (const key of Object.keys(raw)) {
    const id = normalizeConceptId(key)
    if (!id) { dropped += 1; continue }
    const value = isRecord(raw[key]) ? raw[key] : {}
    const count = Number.isSafeInteger(value.count) && value.count > 0 ? value.count : 0
    const bestMetric = finiteOrNull(value.best_metric)
    // Two spellings can canonicalize to one id (`Loss/Contrastive` and `loss/contrastive`). Merging
    // them by SUMMING counts is the only reading that keeps "experiments tagged here" true.
    const current = byId[id]
    if (!current) byId[id] = { id, count, bestMetric }
    else {
      current.count += count
      if (bestMetric != null) current.bestMetric = current.bestMetric == null
        ? bestMetric : pickBetter(current.bestMetric, bestMetric, run?.direction)
    }
  }
  return { entries: Object.keys(byId).sort().map(id => byId[id]), dropped }
}

// Direction-aware "better". An unknown/absent direction has no better — returning the incumbent keeps
// the value SOME run actually reported instead of inventing an ordering over an unstated objective.
function pickBetter(a, b, direction) {
  if (direction === 'min') return Math.min(a, b)
  if (direction === 'max') return Math.max(a, b)
  return a
}

// Fold a SCOPED run array into the concept forest.
//
// Returns `{ nodes, roots, untagged, totals, variants, truncated }`. `{roots, nodes:{parent, depth,
// children, tagged}}` is deliberately the SAME shape and the same field names as the server's
// `search/concept_lens.py::project_hierarchy`, which is what the per-run concept view consumes — a
// global tree that spelled ancestry differently from the in-run one would be a second vocabulary for
// one fact. This adds the per-node scope EVIDENCE (`runs`/`runIds`/`direct*`/`best`) that a per-run
// projection has no way to carry. `nodes` is a null-prototype map because concept ids are LLM-authored
// and `__proto__` is a reachable key. Every node is:
//
//   id, label, depth, parent          the id's own path, split — never inferred
//   children[]                        sorted by id
//   tagged                            a run named this EXACT id, vs. an ancestor we materialized
//   directRuns / directExperiments    runs, and experiments, carrying this EXACT id. Both exact.
//   runs / runIds[]                   DISTINCT runs carrying this id or a descendant. Exact: a set.
//   best                              subtree best metric, or null — see `nodeBest` for the rule
//
// Deliberately NOT reported: a subtree EXPERIMENT total. Within one run an experiment is tagged with
// several ids, so summing `count` over a subtree counts one experiment once per tag — measured on the
// real corpus, run `b2-validate` has 8 experiments and 21 tag-pairs. A number that large labelled
// "experiments" is simply false, and there is nothing in this payload to de-duplicate it with.
export function buildConceptForest(runs = [], { runsById = null } = {}) {
  const rows = Array.isArray(runs) ? runs.filter(isRecord) : []
  const byId = runsById instanceof Map
    ? runsById : new Map(rows.map(run => [run.run_id, run]))
  const nodes = conceptMap()
  const untaggedRunIds = []
  let truncated = false
  let malformedRuns = 0
  let droppedIds = 0
  let taggedRuns = 0

  const ensure = (id) => {
    const existing = nodes[id]
    if (existing) return existing
    if (Object.keys(nodes).length >= MAX_FOREST_NODES) { truncated = true; return null }
    const parts = id.split('/')
    const parent = parts.length > 1 ? parts.slice(0, -1).join('/') : null
    const node = nodes[id] = {
      id, label: parts[parts.length - 1], depth: parts.length - 1, parent,
      children: [], tagged: false, directRuns: 0, directExperiments: 0,
      // run id -> that run's best metric anywhere in this subtree. A Map keyed by run id is what
      // makes `runs` a DISTINCT count rather than a sum that double-counts a run tagging `a/b` and
      // `a/c` under `a`.
      contributors: new Map(), runIds: [], runs: 0, best: null,
    }
    if (parent) {
      const above = ensure(parent)
      if (above && !above.children.includes(id)) above.children.push(id)
    }
    return node
  }

  for (const run of rows) {
    const { entries, dropped } = runConceptEntries(run)
    if (dropped) { droppedIds += dropped; malformedRuns += 1 }
    if (!entries.length) { untaggedRunIds.push(run.run_id); continue }
    taggedRuns += 1
    for (const entry of entries) {
      const node = ensure(entry.id)
      if (!node) continue
      node.tagged = true
      node.directRuns += 1
      node.directExperiments += entry.count
      // Credit this run to the id AND to every ancestor: picking `loss` must find the run that only
      // ever tagged `loss/contrastive/in-batch`, or the hierarchy is decoration (the rule
      // `conceptShelf.js::rowMatchesConcept` states for memory rows).
      for (let cursor = node; cursor; cursor = cursor.parent ? nodes[cursor.parent] : null) {
        const seen = cursor.contributors.get(run.run_id)
        cursor.contributors.set(run.run_id, seen === undefined || seen == null
          ? entry.bestMetric
          : entry.bestMetric == null ? seen : pickBetter(seen, entry.bestMetric, run.direction))
      }
    }
  }

  for (const node of Object.values(nodes)) {
    node.children.sort()
    node.runIds = [...node.contributors.keys()].sort()
    node.runs = node.runIds.length
    node.best = nodeBest(node, byId)
  }
  const roots = Object.keys(nodes).filter(id => !nodes[id].parent).sort()
  return {
    nodes, roots, truncated,
    untagged: { id: UNTAGGED, label: 'Untagged', runIds: untaggedRunIds.slice().sort(),
      runs: untaggedRunIds.length },
    variants: spellingVariants(Object.keys(nodes).filter(id => nodes[id].tagged)),
    totals: {
      runs: rows.length,
      tagged: taggedRuns,
      untagged: untaggedRunIds.length,
      // Ids a run actually named. The materialized ancestors are OUR grouping, not evidence, so
      // counting them here would inflate "concepts studied" with rows nobody authored.
      concepts: Object.keys(nodes).filter(id => nodes[id].tagged).length,
      nodes: Object.keys(nodes).length,
      roots: roots.length,
      malformedRuns, droppedIds,
    },
  }
}

// The subtree's best metric, or null. A metric is only shown when every contributing run shares ONE
// task id and ONE direction — `runIndex.js::metricComparable`, the same predicate that decides whether
// the run list may sort by metric. Anything else is two objectives printed in one column, which is the
// single most convincing wrong number this surface could produce. `taskId`/`direction` ride along so
// the render can NAME the objective and defuse comparison against a sibling row's number.
export function nodeBest(node, runsById) {
  const contributing = node.runIds.length
    ? node.runIds.map(id => runsById.get(id)).filter(Boolean)
    : [...node.contributors.keys()].map(id => runsById.get(id)).filter(Boolean)
  if (contributing.length !== node.contributors.size) return null   // a run left the scope mid-poll
  if (!metricComparable(contributing)) return null
  const direction = contributing[0].direction
  let value = null
  for (const metric of node.contributors.values()) {
    if (metric == null) continue
    value = value == null ? metric : pickBetter(value, metric, direction)
  }
  if (value == null) return null
  return { value, direction, taskId: contributing[0].task_id, runs: contributing.length }
}

// Ids that differ ONLY in `-` vs `_` — the disagreement this corpus actually contains
// (`optimization/hyperparameter-tuning` and `optimization/hyperparameter_tuning`;
// `model/logistic-regression` and `model/logistic_regression`). Reported, never merged: they are the
// same words with different punctuation, which is evidence the taggers drifted, not licence for us to
// pick a winner. Anything beyond punctuation (`feature/` vs `feature-engineering/`) is a genuine
// difference of opinion about the hierarchy and is not guessed at here at all.
export function spellingVariants(ids = []) {
  const groups = conceptMap()
  for (const id of ids) {
    const key = id.replaceAll('-', '').replaceAll('_', '')
    ;(groups[key] ||= []).push(id)
  }
  return Object.keys(groups).sort()
    .filter(key => new Set(groups[key]).size > 1)
    .map(key => ({ key, ids: [...new Set(groups[key])].sort() }))
}

// CO-OCCURRENCE — the one relation a concept id cannot spell for itself.
//
// The tree above IS the `is_a` relation: `loss/contrastive/dcl` asserts its own ancestry, so nesting is
// recovered, never inferred. What no id can tell you is which concepts a lab studies TOGETHER, and that
// is the whole content of `co_occurs`: an unordered PAIR that appeared in the same run, weighted by the
// number of DISTINCT runs the pair appeared in.
//
// WHY IT FOLDS HERE INSTEAD OF ARRIVING OVER HTTP. Co-occurrence is a pure fold over per-run concept
// SETS, and the run rows already on screen carry exactly those. Fetching it instead would mean a second
// code path that decides which runs count — and the server's cross-run corpus is the run-end capsule
// ledger, which on the real corpus here holds 3 runs against the list's 15 tagged ones. Rendering that
// inside a filtered view would be a lie about the population, whatever the numbers said. Folding it
// from `runs` makes the scope shared BY CONSTRUCTION: change a filter and the pairs change with it.
// `search/concept_lens.py::project_concept_map` is the Python half over its own population (capsules,
// for the agent tools); the rules below are the same rules, `runs` here spelling its `n_runs`.
//
// THE FLOOR IS THE POINT. One run tagging `a` and `b` says a tagger emitted two labels in one run, not
// that the lab pairs them. `minRuns` defaults to 2 so a single-run coincidence is not an edge, and the
// candidates BELOW the floor are counted rather than dropped silently — "nothing reached 2 runs" and
// "co-occurrence was not computed" must not render as the same blank space.
const MAX_PAIR_NODES = 512
const MAX_PAIRS = 2_000

export function buildConceptCooccurrence(runs = [], { minRuns = 2, forest = null } = {}) {
  const floor = Number.isSafeInteger(minRuns) && minRuns > 0 ? minRuns : 2
  const rows = Array.isArray(runs) ? runs.filter(isRecord) : []
  const tree = forest || buildConceptForest(rows)
  // Pair only over ids a run actually NAMED, ranked by how many runs named them. A materialized
  // ancestor is our grouping, so it is in no run's set and could never pair; the rank + cap is what
  // keeps this quadratic in a bound rather than in the corpus, and the cap is reported.
  const taggedIds = Object.keys(tree.nodes).filter(id => tree.nodes[id].tagged)
  const ranked = taggedIds
    .sort((a, b) => tree.nodes[b].directRuns - tree.nodes[a].directRuns || (a < b ? -1 : 1))
  const scope = new Set(ranked.slice(0, MAX_PAIR_NODES))
  const pruned = ranked.length - scope.size

  const counts = new Map()
  for (const run of rows) {
    const ids = runConceptEntries(run).entries.map(entry => entry.id).filter(id => scope.has(id))
    ids.sort()
    for (let i = 0; i < ids.length; i += 1) {
      for (let j = i + 1; j < ids.length; j += 1) {
        const key = `${ids[i]} ${ids[j]}`
        counts.set(key, (counts.get(key) || 0) + 1)
      }
    }
  }
  const above = []
  for (const [key, runCount] of counts) {
    if (runCount < floor) continue
    const [a, b] = key.split(' ')
    above.push({ a, b, runs: runCount })
  }
  // Rank by evidence, then by id. Unlike the TREE (ordered by id so click targets never move under
  // the cursor on the 2.5s poll), this list has no expand/collapse targets and its whole question is
  // "which pairs are best attested" — so strength first, with the id tie-break keeping it stable.
  above.sort((x, y) => y.runs - x.runs || (x.a < y.a ? -1 : x.a > y.a ? 1 : x.b < y.b ? -1 : 1))
  return {
    minRuns: floor,
    pairs: above.slice(0, MAX_PAIRS),
    pairCandidates: counts.size,
    pairsOmitted: Math.max(0, above.length - MAX_PAIRS),
    // Two different ways a pair can be missing, kept apart on purpose. The cap above is EXACT — we
    // counted them and showed fewer. A pruned node's pairs were never materialized at all, so their
    // count is UNKNOWN, and reporting it as zero would turn a bound into a finding.
    pairSourceComplete: pruned === 0 && !tree.truncated,
    pairSourceNodesIncluded: scope.size,
    pairSourceNodesPruned: pruned,
    pairsOutsideProjectionUnknown: pruned > 0 || tree.truncated,
  }
}

// The pairs incident to one concept, strongest first — "what has this lab studied alongside this?".
// Exact ids only: a pair is evidence a run named BOTH, and rolling a subtree up would attribute a
// child's partners to a parent nobody tagged.
export function partnersOf(cooccurrence, id) {
  const canonical = normalizeConceptId(id)
  if (!canonical || !cooccurrence?.pairs) return []
  return cooccurrence.pairs
    .filter(pair => pair.a === canonical || pair.b === canonical)
    .map(pair => ({ id: pair.a === canonical ? pair.b : pair.a, runs: pair.runs }))
}

// A stable DFS flattening honoring an `expanded` set — roots always shown, a node's children only when
// it is open. Mirrors `conceptViewModel.js::visibleConceptRows` in shape so both concept trees behave
// identically under the keyboard, but it is a separate function: that one defends against a malformed
// SERVER tree (cycles, dangling children), and a forest built from `/`-paths cannot contain either.
//
// Order is by ID, not by size. This surface re-renders on the list's 2.5s poll, and ranking by a count
// would move click targets under the operator's cursor every time a run finishes an experiment — the
// same stability rule `conceptShelf.js::shelfConcepts` and `conceptChips.js::chipsAtPath` follow.
export function visibleForestRows(forest, expanded = new Set()) {
  const rows = []
  if (!forest || !isRecord(forest.nodes) || !Array.isArray(forest.roots)) return rows
  const open = expanded instanceof Set ? expanded : new Set()
  const stack = forest.roots.slice().reverse().map(id => ({ id, depth: 0 }))
  while (stack.length) {
    if (rows.length >= MAX_VISIBLE_ROWS) break
    const { id, depth } = stack.pop()
    const node = forest.nodes[id]
    if (!node) continue
    rows.push({ id, depth, hasChildren: node.children.length > 0, node })
    if (!open.has(id)) continue
    for (let i = node.children.length - 1; i >= 0; i -= 1) {
      stack.push({ id: node.children[i], depth: depth + 1 })
    }
  }
  return rows
}

// Every ancestor of `id` plus `id` — what a search/deep-link has to open so the row is on screen.
export function forestPathTo(id) {
  const canonical = normalizeConceptId(id)
  if (!canonical) return []
  const parts = canonical.split('/')
  return parts.map((_, index) => parts.slice(0, index + 1).join('/'))
}

// What the Concepts heading NAMES, and the gap it has to disclose.
//
// A decision, so it lives here and not in the component: the restricted state is behind a click, and
// a rule only a click can reach is a rule no test can drive — which is exactly how the heading came
// to announce a population its tree did not contain. `active` is the array the tree is folded from;
// `selected` is what is ticked in List, and `RunList.jsx::compareRuns` maps those ids over the WHOLE
// payload rather than over the visible rows, so a run stays ticked after a filter hides it. The two
// counts are therefore routinely different, and the heading must report the one the tree describes.
export function conceptScopeClaim({ scopeLabel = 'All runs', restrictToSelection = false,
  selectedCount = 0, activeCount = 0 } = {}) {
  const selected = Number.isSafeInteger(selectedCount) && selectedCount > 0 ? selectedCount : 0
  const active = Number.isSafeInteger(activeCount) && activeCount > 0 ? activeCount : 0
  if (!restrictToSelection || !selected) return { name: scopeLabel, outOfScope: 0 }
  return {
    name: `${active} selected run${active === 1 ? '' : 's'}`,
    // Never negative: `active` is a filter OVER the selection, so it cannot exceed it — but a caller
    // that passed the two from different renders would otherwise print a negative disclosure.
    outOfScope: Math.max(0, selected - active),
  }
}

// The coverage sentence that goes ABOVE the tree, in the spirit of `conceptShelf.js::coverageSummary`:
// a tree drawn from 15 of 46 runs is a true picture of 15 runs and says nothing about the other 31, and
// the surface must not let that read as "the lab studied 71 things". `complete` is the only state in
// which the tree describes the whole scope.
export function forestCoverage(forest) {
  const totals = forest?.totals
  if (!totals || !Number.isFinite(totals.runs) || totals.runs <= 0) return null
  return {
    runs: totals.runs,
    tagged: totals.tagged,
    untagged: totals.untagged,
    concepts: totals.concepts,
    roots: totals.roots,
    complete: totals.untagged === 0 && totals.tagged > 0,
    empty: totals.tagged === 0,
    malformedRuns: totals.malformedRuns,
    droppedIds: totals.droppedIds,
  }
}
