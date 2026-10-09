import { uiText, uiMessage, uiPlural } from './uiLanguage.js'
// Run-report analysis: derive the human-readable conclusions ("what worked / what didn't"), the
// key-improvement waterfall, and per-operator/per-theme effectiveness purely from the folded node
// set. Mirrors the engine's selection rule — only FEASIBLE evaluated nodes move the frontier — so
// the report never credits a result the engine itself rejected.

import { costPricing, fmt, isSweep, operatorMeta } from './util.js'
import { nodeTheme } from './conceptId.js'
import { activeNodeMap, currentRewardHacks, rewardHackNodeCount } from './nodeProjection.js'
import { normalizeRunReport, reportCoverageText, reportNarrativeCoverage } from './reportModel.js'
import { OBJECTIVE_SOURCE_LABEL, objectiveMetricSource,
  objectiveSourceCaveated } from './trustSemantics.js'
import { sourceIncomplete } from './runIndex.js'
import { scoreDifference, parentScoreDifference, eligibleMeasuredResult as eligibleResult } from './scoreComparison.js'
import { resultMeasurement } from './resultMeasurement.js'

const metricOf = (n) => (n.confirmed_mean ?? n.metric)
const isEvaluated = (n) => n.status === 'evaluated' && metricOf(n) != null
const better = (dir) => (a, b) => (dir === 'min' ? a < b : a > b)

// The parameters that changed between a node and its first parent (the "what changed" of a step).
export function paramDiff(node, parent) {
  if (!parent) return []
  const a = parent.idea?.params || {}, b = node.idea?.params || {}
  const keys = Array.from(new Set([...Object.keys(a), ...Object.keys(b)]))
  const out = []
  keys.forEach(k => {
    const va = a[k], vb = b[k]
    if (va !== vb) out.push({ key: k, from: va, to: vb })
  })
  return out
}

export function paramDiffLabel(diff) {
  if (!diff.length) return '—'
  return diff.map(d => `${d.key}: ${fmt(d.from)} → ${fmt(d.to)}`).join(', ')
}

// The frontier walk: in node-id order, every FEASIBLE node that set a new best is one improvement
// "step". Returns the ordered list with the delta it contributed and what changed vs its parent.
export function improvements(nodes, direction, state = null) {
  const dir = direction
  if (!['min', 'max'].includes(dir)) return []
  const bt = better(dir)
  const ev = Object.values(nodes).filter(isEvaluated).sort((a, b) => a.id - b.id)
  const steps = []
  let best = null
  ev.forEach(n => {
    if (!eligibleResult(n, state)) return
    const v = metricOf(n)
    if (best === null || bt(v, best.v)) {
      const parent = (n.parent_ids || []).map(p => nodes[p]).find(Boolean)
      steps.push({
        id: n.id, operator: n.operator, theme: nodeTheme(n, state),
        from: best ? best.v : null, to: v,
        measurement: resultMeasurement(n.confirmed_mean != null).label,
        delta: best ? v - best.v : null,
        params: n.idea?.params || {}, rationale: n.idea?.rationale || '',
        diff: paramDiff(n, parent), parentId: parent?.id ?? null,
        source: n.source || null,
      })
      best = { v, id: n.id }
    }
  })
  return steps
}

// Count comparison coverage separately from outcomes. Numeric frontiers may mix measurement types.
function rollup(nodes, direction, keyFn, state = null) {
  const context = state || { direction, nodes }
  const dir = context.direction
  const bt = better(dir)
  // Agent-authored direction names are data, not prototype-bearing object properties.
  const out = Object.create(null)
  Object.values(nodes).forEach(n => {
    const key = keyFn(n)
    if (key == null) return
    const e = (out[key] ||= { key, count: 0, evaluated: 0, compared: 0, uncompared: 0,
      improved: 0, failed: 0, best: null, bestId: null, bestConfirmed: null })
    e.count++
    if (n.status === 'failed') e.failed++
    if (isEvaluated(n)) {
      e.evaluated++
      const v = metricOf(n)
      // Only a FEASIBLE result may define `best` — same rule the frontier walk (`improvements`) and the
      // `improved` count below apply, and the module invariant at the top ("never credit a result the
      // engine itself rejected"). Otherwise a constraint-violating node's raw metric would inflate this
      // operator/theme's numeric frontier and the directionProfit compatibility projection.
      if (['min', 'max'].includes(dir) && eligibleResult(n, context) && (e.best === null || bt(v, e.best))) {
        e.best = v; e.bestId = n.id; e.bestConfirmed = n.confirmed_mean != null
      }
      const d = parentScoreDifference(n, nodes, context)
      if (d == null) e.uncompared++
      else {
        e.compared++
        if (dir === 'min' ? d < 0 : d > 0) e.improved++
      }
    }
  })
  return Object.values(out).sort((a, b) => b.improved - a.improved || b.evaluated - a.evaluated)
}

export const operatorEffectiveness = (nodes, dir, state = null) => rollup(nodes, dir, n => n.operator || 'unknown', state)
// Keep this compatibility projection aligned with events/digest.py::node_theme: new Ideas author
// concepts rather than the legacy `theme`, so their first concept axis becomes the coarse direction.
export const themeEffectiveness = (nodes, dir, state = null) =>
  rollup(nodes, dir, node => nodeTheme(node, state), state)

// Compatibility projection with no current rendered consumer. Keep first-discovery order and
// distinguish the robust numeric frontier from a comparable primary-score gain.
export function directionProfit(state) {
  const nodes = activeNodeMap(state.nodes || {}, state)
  const dir = state.direction
  const byId = new Map(Object.values(nodes).map(n => [n.id, n]))
  const first = Object.values(nodes).filter(n => eligibleResult(n, state)).sort((a, b) => a.id - b.id)[0]
  const baseline = Number.isFinite(first?.metric) ? first.metric : null
  const byDirection = new Map(themeEffectiveness(nodes, dir, state).map(row => [row.key, row]))
  const directions = [...new Set(Object.values(nodes).sort((a, b) => a.id - b.id)
    .map(node => nodeTheme(node, state)).filter(Boolean))]
  return directions.map(direction => {
    const t = byDirection.get(direction)
    const selected = byId.get(t.bestId)
    const d = scoreDifference(selected, first, state)
    const gain = d == null ? null : dir === 'min' ? -d : d
    return { ...t, direction, gain, baseline, baselineId: first?.id ?? null,
      comparisonMetric: d == null ? null : selected.metric, gainBasis: 'evaluation_score' }
  })
}

export const optimizationLabel = direction => direction === 'max' ? 'maximize'
  : direction === 'min' ? 'minimize' : 'unknown'

// For a MERGE node (≥2 parents): what each parent contributed — its theme + the "trick" it carried
// (that parent's own param-diff vs its parent). Powers the node card's "⊕ combines" line + the
// Inspector's "uses" list, so a merge says which techniques it actually fused.
export function mergeSummary(node, nodes, state = null) {
  if (!node || (node.parent_ids || []).length < 2) return []
  return node.parent_ids.map(pid => {
    const p = nodes[pid]
    if (!p) return { parentId: pid, theme: null, change: '' }
    const gp = (p.parent_ids || []).map(x => nodes[x]).find(Boolean)
    return { parentId: pid, theme: nodeTheme(p, state), change: paramDiffLabel(paramDiff(p, gp)) }
  })
}

function _pearson(xs, ys) {
  const n = xs.length
  const mx = xs.reduce((a, b) => a + b, 0) / n, my = ys.reduce((a, b) => a + b, 0) / n
  let sxy = 0, sxx = 0, syy = 0
  for (let i = 0; i < n; i++) { const dx = xs[i] - mx, dy = ys[i] - my; sxy += dx * dy; sxx += dx * dx; syy += dy * dy }
  const d = Math.sqrt(sxx * syy)
  // A correlation needs BOTH series to vary. When either is constant the denominator is 0 and r is
  // UNDEFINED — it is not zero. Returning 0 here reported "importance 0 · r +0" for a knob every
  // node set to the same value, which reads as "this knob was measured and does not matter" when
  // the truth is "it was never varied, so nothing could be learned" — the opposite conclusion, on
  // the panel whose whole job is telling an operator what to tune next. Measured across the runs
  // under `runs/`: EVERY `importance 0` row in the corpus is this degenerate case (`l2` was
  // [0.01, 0.01, 0.01], `lr` was [0.03, 0.03, 0.03], `learning_rate` was [0.5, 0.5, 0.5]) and not
  // one of them is a real measured zero correlation. `null` = "cannot be computed from this
  // evidence", which every caller already renders as `—`.
  return d === 0 ? null : sxy / d
}

// Run-wide hyperparameter importance: |Pearson r| of each numeric param vs the metric across all
// evaluated feasible nodes ("which knobs mattered"). Needs ≥3 points per param. One source of truth
// shared by the Report's Learnings section and the Importance panel.
export function hyperImportance(state) {
  // lifecycle-retired rows remain in the append-only fold for audit, but every current
  // report projection must use the same active population as analyze/DAG/Concepts.
  const nodes = Object.values(activeNodeMap(state.nodes || {}, state))
    .filter(n => eligibleResult(n, state) && Number.isFinite(n.metric))
  const keys = new Set()
  nodes.forEach(n => Object.entries(n.idea?.params || {}).forEach(([k, v]) => { if (typeof v === 'number') keys.add(k) }))
  const rows = []
  keys.forEach(k => {
    const pts = nodes.filter(n => typeof n.idea?.params?.[k] === 'number')
    if (pts.length < 3) return
    const r = _pearson(pts.map(n => n.idea.params[k]), pts.map(n => n.metric))
    rows.push({ k, imp: r == null ? null : Math.abs(r), r, n: pts.length })
  })
  // An unmeasurable param sorts LAST. It is not a small importance — it is no measurement at all,
  // and `null - null` is 0, which would otherwise leave these rows interleaved with the real ones
  // in whatever order the key set happened to produce.
  return rows.sort((a, b) => (b.imp ?? -1) - (a.imp ?? -1))
}

// Normalize free text to a compact, single-line caption: collapse whitespace and cap the length.
// The card's .change-chip CSS ellipsizes the VISIBLE chip at 168px; this additionally caps the string
// at `max` chars so an enormous rationale can't bloat the hover-title tooltip.
function brief(text, max = 140) {
  const s = String(text || '').replace(/\s+/g, ' ').trim()
  return s.length > max ? s.slice(0, max - 1).trimEnd() + '…' : s
}

// The card's one-line "what this node did" caption. Deterministic so it shows IMMEDIATELY (no waiting
// on a late LLM summary), and — by request — EVERY non-merge node carries a non-empty, explanatory
// caption (the card is never left blank):
//   • sweep  → what was SEARCHED (the grid), not the single best value (the old `p=2.5` bug);
//   • draft / root (no parent) → `initial experiment` PLUS a brief description (its rationale/theme) so the
//       starting point is explained too, instead of a bare label;
//   • param change → the agent's `change_summary` if it wrote one, else the param-diff vs the parent;
//   • no param change (code-only edit / re-run / repair) → the agent's rationale if any, else the
//       operator's role ("improve — hill-climb around best", …) so the card still says something.
// Returns '' ONLY for a real merge — it renders its own ⊕ combines line in its place.
export function nodeChip(node, nodes, state = null) {
  if (!node) return ''
  // Resolve parents the SAME way the card's isMerge does (filter out ids missing from the fold), so
  // a node with a dangling 2nd parent id isn't mis-classified as a merge and left with no chip.
  const parents = (node.parent_ids || []).map(p => nodes[p]).filter(Boolean)
  if (parents.length > 1) return ''                        // real merge → renders its ⊕ combines line
  if (isSweep(node)) {
    const sp = node.idea?.space || {}
    const keys = Object.keys(sp)
    if (!keys.length) return uiText('swept')
    const tok = (k) => {
      const vs = (sp[k] || []).filter(v => v != null)
      return (vs.length > 1 && vs.every(v => typeof v === 'number'))
        ? `${k}∈[${fmt(Math.min(...vs))}…${fmt(Math.max(...vs))}]` : k
    }
    return uiText('swept ') + (keys.length <= 2 ? keys.map(tok).join(', ') : uiPlural(keys.length, '{0} params', '{0} params'))
  }
  const parent = parents[0]
  if (!parent) {                                           // draft / root — nothing to diff against
    const what = brief(node.idea?.rationale) || brief(nodeTheme(node, state))
    return what ? uiMessage('initial experiment · {0}', [what]) : uiText('initial experiment')
  }
  if (node.idea?.change_summary) return brief(node.idea.change_summary)
  const lbl = paramDiffLabel(paramDiff(node, parent))      // diff vs the resolved parent directly
  if (lbl !== '—') return lbl
  // No param change vs the parent — still carry an explanatory caption rather than a blank card.
  return brief(node.idea?.rationale) || operatorMeta(node.operator).label
}

// Nodes that ran but made things worse than their parent (regressions) — the "tried, didn't help".
export function regressions(nodes, direction, state = null) {
  const context = state || { direction, nodes }
  const out = []
  Object.values(nodes).filter(isEvaluated).forEach(n => {
    const d = parentScoreDifference(n, nodes, context)
    if (d != null && (context.direction === 'min' ? d > 0 : d < 0)) {
      const parent = nodes[n.parent_ids[0]]
      out.push({ id: n.id, operator: n.operator, metric: n.metric, parentId: parent.id,
                 parentMetric: parent.metric, diff: paramDiff(n, parent), theme: nodeTheme(n, state) })
    }
  })
  return out.sort((a, b) => a.id - b.id)
}

export function failureBreakdown(nodes) {
  // provider-authored error reasons are untrusted keys. A null-prototype index keeps
  // values such as "__proto__" and "constructor" as ordinary buckets instead of object internals.
  const by = Object.create(null)
  Object.values(nodes).filter(n => n.status === 'failed').forEach(n => {
    (by[n.error_reason || 'unknown'] ||= []).push(n)
  })
  return by
}

// One call that assembles the whole analysis for the report.
export function analyze(state) {
  const nodes = activeNodeMap(state.nodes || {}, state)
  const dir = state.direction
  const steps = improvements(nodes, dir, state)
  const evald = Object.values(nodes).filter(isEvaluated)
  const infeasible = evald.filter(n => n.feasible === false)
  const operators = operatorEffectiveness(nodes, dir, state)
  const compared = operators.reduce((count, row) => count + row.compared, 0)
  return {
    steps,
    firstBest: steps.length ? steps[0].to : null,
    finalBest: steps.length ? steps[steps.length - 1].to : null,
    // `steps` only advances the direction-aware frontier, so improvement is the positive distance
    // from its first feasible value regardless of whether the objective is minimized or maximized.
    totalGain: steps.length > 1
      ? (dir === 'max'
          ? steps[steps.length - 1].to - steps[0].to
          : steps[0].to - steps[steps.length - 1].to)
      : 0,
    operators,
    themes: themeEffectiveness(nodes, dir, state),
    regressions: regressions(nodes, dir, state),
    failures: failureBreakdown(nodes),
    infeasible,
    nEval: evald.length,
    compared, uncompared: evald.length - compared,
  }
}

// Outcomes need the same attempt-bound primary-score comparison as the verdict. A lower
// frontier value, a merge, a failed command or missing evidence is never a proved technique win.
export function reportOutcomeEvidence(state) {
  const nodes = activeNodeMap(state.nodes || {}, state)
  const out = { better: [], worse: [], unchanged: [], unknown: [] }
  for (const node of Object.values(nodes).sort((a, b) => a.id - b.id)) {
    if (!isEvaluated(node)) continue
    const difference = parentScoreDifference(node, nodes, state)
    if (difference == null) { out.unknown.push(node.id); continue }
    const gain = state.direction === 'min' ? -difference : difference
    const row = { node, parent: nodes[node.parent_ids[0]], gain }
    out[gain > 0 ? 'better' : gain < 0 ? 'worse' : 'unchanged'].push(row)
  }
  return out
}

// Trust caveats that must not be buried in the verdict: reward-hack / leakage / drift / single-seed /
// infeasibility. Each is a chip with a deep-link to the panel that explains it. Pure (from state).
export function trustCaveats(state, best) {
  const out = []
  const hacks = currentRewardHacks(state)
  if (best && hacks.some(h => h.node_id === best.id))
    out.push({ kind: 'reward-hack', severity: 'alarm', text: 'champion flagged as a possible reward-hack', panel: 'trust' })
  else if (hacks.length)
    out.push({ kind: 'reward-hack', severity: 'warn', text: uiPlural(rewardHackNodeCount(hacks), '{0} node(s) flagged as possible reward-hacks', '{0} node(s) flagged as possible reward-hacks'), panel: 'trust' })
  if (state.leakage?.leak)
    out.push({ kind: 'leakage', severity: 'alarm', text: 'data-leakage scan flagged this run', panel: 'data' })
  if ((state.drifts || []).length)
    out.push({ kind: 'drift', severity: 'warn', text: uiPlural(state.drifts.length, '{0} metric-drift divergence(s) caught', '{0} metric-drift divergence(s) caught'), panel: 'trust' })
  const infeasible = Object.values(activeNodeMap(state.nodes || {}, state))
    .filter(n => isEvaluated(n) && n.feasible === false)
  if (infeasible.length)
    out.push({ kind: 'infeasible', severity: 'warn', text: uiPlural(infeasible.length, '{0} evaluated node(s) violated a constraint', '{0} evaluated node(s) violated a constraint'), panel: 'trust' })
  if (best && !(Number.isFinite(best.confirmed_mean)
      && Number.isSafeInteger(best.confirmed_seeds) && best.confirmed_seeds >= 2))
    out.push({ kind: 'single-seed', severity: 'warn', text: 'multiple successful repeat checks are not established', panel: 'trust' })
  // WHAT THE CHAMPION'S NUMBER IS, in the aggregator every run-level claim is built from.
  //
  // The salvage/subject vocabulary was wired into four DISPLAY surfaces (the Metrics tab, the Pareto
  // front, the archive elites, the cross-run champion row) and not into this list — which is the one
  // the Report headline, the model card and both exports read. So under `metric_salvage: "select"`,
  // the rung that mints NO violation row, every branch above found nothing: no reward-hack row, no
  // infeasible node, and, for a multi-seed champion, no single-seed caveat either. `verdict` then
  // computed `trust = 'unverified'` and published "Improved the metric by X — champion #N is robust
  // across N seeds; no trust flags are recorded" about a number the protected scoring path never
  // read, while the Pareto tab one click away marked that same node `salvaged`.
  //
  // Read through the SAME two helpers as those four surfaces (`trustSemantics.js` owns the
  // vocabulary, and its whole reason for existing is that a second home drifts), so the headline and
  // the tabs cannot come to disagree about one node — and read from the CHAMPION, because this is a
  // claim about the run's own result rather than a census of the tree.
  //
  // `alarm`, not `warn`, and deliberately: severity is what `verdict` turns into `trust: 'suspect'`
  // and the headline's "the win is flagged, treat with caution". The champion's metric being
  // unmeasured does not qualify the win, it is the win's own evidence — the same grade as the
  // champion-flagged reward-hack branch at the top of this function, and for the same reason.
  const objective = best ? objectiveMetricSource(best) : null
  if (objectiveSourceCaveated(objective)) {
    out.push({
      kind: 'objective-source',
      severity: 'alarm',
      text: `champion’s objective is ${OBJECTIVE_SOURCE_LABEL[objective.channel]}`,
      panel: 'trust',
    })
  }
  return out
}

// The deterministic verdict — classify the outcome purely from data so the Report always leads with a
// plain-language bottom line, even with no model. `a` is the analyze() result (reused, not recomputed).
export function verdict(state, a) {
  const dir = state.direction
  const candidate = state.best_node_id != null ? state.nodes?.[state.best_node_id] : null
  const finite = value => typeof value === 'number' && Number.isFinite(value)
  const eligible = node => eligibleResult(node, state)
  const best = eligible(candidate) ? candidate : null
  const caveats = trustCaveats(state, best)
  if (sourceIncomplete(state)) caveats.unshift({ kind: 'incomplete-record', severity: 'alarm',
    text: 'the event record is incomplete', panel: 'trust' })
  const trust = caveats.some(c => c.severity === 'alarm') ? 'suspect'
    : caveats.length ? 'caveats' : 'unverified'
  if (!best) {
    return { outcome: 'none', robustness: 'n/a', trust, best, caveats,
      headline: a.nEval ? 'No completed eligible result is selected. Review evaluation and selection evidence.'
        : 'No experiments have been evaluated yet.',
      nextStep: a.nEval ? 'Inspect failed or excluded experiments in Trace and Trust before proposing another candidate.'
        : 'Complete an evaluation before judging the result.' }
  }
  const first = Object.values(state.nodes || {}).filter(eligible).sort((x, y) => x.id - y.id)[0]
  const baseline = finite(first?.metric) ? first.metric : null
  // Base-evaluation receipts describe scores. They do not certify comparability of confirmation means.
  const gain = scoreDifference(best, first, state)
  const comparable = gain != null
  const outcome = first?.id === best.id ? 'baseline' : !comparable ? 'uncompared'
    : gain === 0 ? 'flat' : (dir === 'min' ? gain < 0 : gain > 0) ? 'improved' : 'regressed'
  const repeated = finite(best.confirmed_mean) && Number.isSafeInteger(best.confirmed_seeds)
    && best.confirmed_seeds >= 2
  const robustness = repeated ? 'repeat-checked' : finite(best.confirmed_mean) ? 'mean recorded' : 'unconfirmed'
  const valueLabel = uiText(finite(best.confirmed_mean) ? 'confirmation mean' : 'evaluation score')
  let headline = uiMessage('Selected #{0}: {1} {2}.', [best.id, valueLabel, fmt(metricOf(best))])
  if (outcome === 'baseline') headline += uiText(' This is the first eligible experiment; it does not establish improvement.')
  else if (outcome === 'uncompared') headline += uiText(' Improvement over the first eligible experiment is not established.')
  else headline += outcome === 'flat' ? uiText(' Its evaluation score matches the first eligible experiment.')
    // Names WHAT it is better than (doc 74 EB-21): "better by 77.01" alone left the reader to guess
    // the reference, which is the first eligible experiment, not the parent or the previous best.
    : uiMessage(' Its evaluation score is {0} by {1} than the first eligible experiment #{2}, under matching recorded conditions.', [uiText(outcome === 'improved' ? 'better' : 'worse'), fmt(Math.abs(gain)), first.id])
  headline += trust === 'suspect' ? uiText(' The result is flagged, treat with caution.')
    : uiText(' Detector coverage is not fully verified.')
  const nextStep = trust === 'suspect' ? 'Review the flagged evidence in Trust before using the selected result.'
    : !comparable && outcome !== 'baseline' ? 'Establish matching evaluation conditions before claiming improvement. Compare evaluation scores and confirmation means separately.'
      : !repeated ? 'Repeat the selected experiment with multiple seeds and inspect Trust before relying on the result.'
        : 'Inspect the spread and evaluation conditions of repeat checks. Multiple seeds alone do not establish generalization or statistical significance.'
  return { outcome, robustness, trust, best, baseline, first, gain, gainPct: null,
    direction: dir, caveats, headline, nextStep: nextStep }
}

const reportContext = context => ({
  generation: /^[0-9a-f]{64}$/.test(context?.generation || '') ? context.generation : null,
  snapshotSeq: Number.isSafeInteger(context?.snapshotSeq) && context.snapshotSeq >= 0
    ? context.snapshotSeq : null,
})

const coverageRecord = coverage => ({
  status: coverage.status, at_node: coverage.atNode,
  current_node_count: coverage.currentNodeCount, stale_by: coverage.staleBy,
  basis: 'node_count', full_state_freshness: 'unknown',
})

export function buildModelCard(state, _best = null, context = {}) {
  const a = analyze(state)
  const v = verdict(state, a)
  const rep = normalizeRunReport(state.report)
  const nodeCount = Object.keys(activeNodeMap(state.nodes || {}, state)).length
  const coverage = reportNarrativeCoverage(rep, nodeCount)
  const ctx = reportContext(context)
  // The folded event state is the authority. Keep the legacy argument position for callers, but
  // never let a caller-supplied object contradict the deterministic verdict in the same export.
  const champion = v.best || null
  return {
    schema_id: 'looplab.model-card', schema_version: 2,
    task: state.task_id, goal: state.goal, direction: state.direction, run_id: state.run_id,
    champion: champion ? { node_id: champion.id, operator: champion.operator,
      metric: champion.confirmed_mean ?? champion.metric, confirmed: champion.confirmed_mean != null,
      params: champion.idea?.params || {}, lineage: champion.parent_ids || [] } : null,
    verdict: uiText(v.headline), verdict_source: 'deterministic',
    agent_report_caveats: rep?.caveats || [],
    deterministic_trust: { status: v.trust, caveats: v.caveats.map(caveat => uiText(caveat.text)) },
    counts: { nodes: nodeCount, evaluated: a.nEval },
    deterministic_verdict: { headline: uiText(v.headline), outcome: v.outcome,
      robustness: v.robustness, trust: v.trust, caveats: v.caveats.map(caveat => uiText(caveat.text)),
      next_step: uiText(v.nextStep) },
    agent_narrative: rep ? {
      advisory: true, headline: rep.headline, verdict: rep.verdict, summary: rep.summary,
      champion_summary: rep.champion_summary, what_worked: rep.what_worked,
      learnings: rep.learnings, what_didnt: rep.what_didnt,
      next_directions: rep.next_directions, caveats: rep.caveats,
      coverage: coverageRecord(coverage),
      provenance: { published_event_seq: rep.published_seq, published_at: rep.published_at,
        published_at_unit: 'unix_seconds', trigger: rep.trigger || null,
        node_count_at_publication: rep.at_node },
    } : null,
    provenance: { authority: 'events.jsonl', run_generation: ctx.generation,
      snapshot_seq: ctx.snapshotSeq },
  }
}

// A portable Markdown report (download / paste into a PR) built from the same analysis.
export function toMarkdown(state, _best, context = {}) {
  const a = analyze(state)
  const v = verdict(state, a)
  const rep = normalizeRunReport(state.report)
  const nodeCount = Object.keys(activeNodeMap(state.nodes || {}, state)).length
  const coverage = reportNarrativeCoverage(rep, nodeCount)
  const ctx = reportContext(context)
  const champion = v.best || null
  const L = []
  L.push(uiMessage("# LoopLab run report — {0}", [state.label || state.run_id || state.task_id]))
  L.push('')
  // Conclusion-first and authority-first: provider prose can explain, never replace, this verdict.
  L.push(uiMessage("## Verdict", []))
  L.push('')
  L.push(`**${uiText(v.headline)}**`)
  L.push('', uiMessage("**Next step:** {0}", [uiText(v.nextStep)]))
  if (v.caveats.length) {
    L.push('')
    L.push(uiMessage("Deterministic trust caveats: {0}.", [v.caveats.map(c => uiText(c.text)).join('; ')]))
  }
  const outcomes = reportOutcomeEvidence(state)
  L.push('', uiText('## Recorded outcomes'), '')
  L.push(uiMessage('Better score: {0}; worse score: {1}; unchanged: {2}; comparison not established: {3}.',
    [outcomes.better.length, outcomes.worse.length, outcomes.unchanged.length, outcomes.unknown.length]))
  L.push(uiText('Only attempt-bound parent comparisons with matching evaluation conditions count. Missing comparison is not failure; a command failure does not refute a hypothesis.'))
  L.push('')
  L.push(uiMessage("- **Run:** {0}", [state.run_id]))
  L.push(uiMessage("- **Optimization orientation:** {0}", [uiText(optimizationLabel(state.direction))]))
  L.push(uiMessage("- **Status:** {0}{1}", [uiText(state.phase || (state.finished ? 'finished' : 'running')), state.stop_reason ? ` (${state.stop_reason})` : '']))
  L.push(uiMessage("- **Nodes:** {0} — {1} evaluated, {2} failed", [nodeCount, a.nEval, Object.values(a.failures || {}).reduce((s, x) => s + x.length, 0)]))
  if (champion) L.push(uiMessage("- **Best:** node #{0} · metric {1}{2} · params {3}", [champion.id, fmt(champion.confirmed_mean ?? champion.metric), champion.confirmed_mean != null ? ` ±${fmt(champion.confirmed_std)} (${champion.confirmed_seeds}×)` : '', JSON.stringify(champion.idea?.params)]))
  // The exported Markdown is what gets pasted into a report or a ticket, so it is the LAST place a
  // `$0` may stand in for "nobody priced this run" — spell the pricing evidence out in full here.
  if (state.llm_cost) L.push(uiPlural(state.llm_cost.total_tokens, '- **LLM:** {0} tokens · {1} ({2})', '- **LLM:** {0} tokens · {1} ({2})', [state.llm_cost.total_tokens,
    uiText(costPricing(state.llm_cost).text), uiText(costPricing(state.llm_cost).title)]))
  if (ctx.generation) L.push(uiMessage("- **Run generation:** {0}", [ctx.generation]))
  if (ctx.snapshotSeq != null) L.push(uiMessage("- **Snapshot event:** #{0}", [ctx.snapshotSeq]))
  if (rep) {
    // Provider prose must remain inside the advisory quote with every platform newline form.
    const quote = text => String(text || '').split(/\r\n?|\n/).forEach(line => L.push(`> ${line}`))
    L.push('', uiText('## Agent narrative (advisory)'), '')
    quote(uiText('**Advisory only — not the deterministic verdict or trust decision.**'))
    quote(uiMessage('**Node coverage:** {0}', [uiText(reportCoverageText(coverage))]))
    const receipt = [rep.published_seq != null ? uiMessage('event #{0}', [rep.published_seq]) : uiText('event unknown'),
      rep.published_at != null ? new Date(rep.published_at * 1000).toISOString() : uiText('time unknown'),
      rep.trigger ? uiMessage('trigger {0}', [rep.trigger]) : uiText('trigger unknown')].join(' · ')
    quote(uiMessage('**Published:** {0}', [receipt]))
    if (rep.headline) { L.push('>'); quote(uiMessage('**Agent headline:** {0}', [rep.headline])) }
    if (rep.verdict || rep.summary) { L.push('>'); quote(rep.verdict || rep.summary) }
    const advisoryLists = [
      ['Agent caveats', rep.caveats], ['Champion note', rep.champion_summary ? [rep.champion_summary] : []],
      ['What worked', rep.what_worked], ["What didn't work", rep.what_didnt],
      ['Learnings', rep.learnings], ['Next directions', rep.next_directions],
    ]
    advisoryLists.forEach(([label, items]) => {
      if (!items?.length) return
      L.push('>'); quote(`**${uiText(label)}:**`); items.forEach(item => quote(`- ${item}`))
    })
  }
  L.push('')
  L.push((a.steps.length === 1 ? uiText('## First eligible metric') : uiText('## Recorded metric trajectory')))
  L.push('', uiText('This numeric frontier may combine evaluation scores and confirmation means. Its changes do not establish a comparable improvement. Use the selected-result verdict above.'))
  if (a.steps.length) {
    L.push('')
    L.push(uiText('| step | node | operator | recorded value | measurement | numeric change | what changed |'))
    L.push('|---|---|---|---|---|---|---|')
    a.steps.forEach((s, i) => L.push(`| ${i + 1} | #${s.id} | ${s.operator}${s.theme ? ` (${s.theme})` : ''} | ${fmt(s.to)} | ${uiText(s.measurement)} | ${s.delta == null ? uiText('first eligible') : fmt(s.delta)} | ${paramDiffLabel(s.diff)} |`))
    if (a.steps.length > 1) L.push(uiPlural(a.steps.length, '\nRecorded frontier change: **{0}** across {1} steps (first eligible {2} → numeric frontier {3}).', '\nRecorded frontier change: **{0}** across {1} steps (first eligible {2} → numeric frontier {3}).', [fmt(a.totalGain), a.steps.length, fmt(a.firstBest), fmt(a.finalBest)]))
  } else L.push(uiText('\n_No improving steps recorded yet._'))
  L.push('')
  L.push(uiText('## What didn\'t work'))
  const fr = Object.entries(a.failures)
  if (fr.length) { L.push(uiMessage("\n**Failures by reason:** {0}", [fr.map(([r, ns]) => `${r} (${ns.length})`).join(', ')])) }
  if (a.regressions.length) { L.push(uiMessage("\n**Worse evaluation scores:** {0} under matching recorded parent conditions.", [a.regressions.length])) }
  if (a.infeasible.length) { L.push('\n' + uiPlural(a.infeasible.length, '**Infeasible:** {0} node(s) violated a constraint and were excluded.', '**Infeasible:** {0} node(s) violated a constraint and were excluded.')) }
  const deadThemes = a.themes.filter(t => t.improved === 0)
  if (deadThemes.length) L.push(uiMessage("\n**Primary concept axes without a comparable score improvement:** {0}. Absence of an improvement is not evidence that an axis failed.", [deadThemes.map(t => t.key).join(', ')]))
  if (!fr.length && !a.regressions.length && !a.infeasible.length) L.push(uiText('\n_No recorded failures or comparable regressions._'))
  L.push('')
  L.push(uiText('## Parent comparison coverage'))
  L.push(uiMessage("\n{0} compared; {1} not compared. First experiments and missing evidence are not failed experiments. Only evaluation scores are compared; the numeric frontier may include confirmation means.", [a.compared, a.uncompared]))
  L.push('')
  L.push(uiText('| operator | nodes | evaluated | compared with parent | better score | not compared | numeric frontier |'))
  L.push('|---|---|---|---|---|---|---|')
  a.operators.forEach(o => L.push(`| ${o.key} | ${o.count} | ${o.evaluated} | ${o.compared} | ${o.improved} | ${o.uncompared} | ${fmt(o.best)}${o.best != null ? o.bestConfirmed ? uiText(' (confirmation mean)') : uiText(' (evaluation score)') : ''} |`))
  return L.join('\n')
}
