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
