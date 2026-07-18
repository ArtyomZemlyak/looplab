// Run-report analysis: derive the human-readable conclusions ("what worked / what didn't"), the
// key-improvement waterfall, and per-operator/per-theme effectiveness purely from the folded node
// set. Mirrors the engine's selection rule — only FEASIBLE evaluated nodes move the frontier — so
// the report never credits a result the engine itself rejected.

import { fmt, isSweep, operatorMeta } from './util.js'
import { nodeTheme } from './conceptId.js'
import { activeNodeMap, nodeIsActive } from './nodeProjection.js'
import { normalizeRunReport, reportCoverageText, reportNarrativeCoverage } from './reportModel.js'

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
  const dir = direction || 'min'
  const bt = better(dir)
  const ev = Object.values(nodes).filter(isEvaluated).sort((a, b) => a.id - b.id)
  const steps = []
  let best = null
  ev.forEach(n => {
    if (n.feasible === false) return
    const v = metricOf(n)
    if (best === null || bt(v, best.v)) {
      const parent = (n.parent_ids || []).map(p => nodes[p]).find(Boolean)
      steps.push({
        id: n.id, operator: n.operator, theme: nodeTheme(n, state),
        from: best ? best.v : null, to: v,
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

// Per-operator and per-theme productivity: how many nodes each produced, how many evaluated, how
// many actually beat their parent (a real improvement), and the best metric it reached.
function rollup(nodes, direction, keyFn) {
  const dir = direction || 'min'
  const bt = better(dir)
  // Agent-authored direction names are data, not prototype-bearing object properties.
  const out = Object.create(null)
  Object.values(nodes).forEach(n => {
    const key = keyFn(n)
    if (key == null) return
    const e = (out[key] ||= { key, count: 0, evaluated: 0, improved: 0, failed: 0, best: null })
    e.count++
    if (n.status === 'failed') e.failed++
    if (isEvaluated(n)) {
      e.evaluated++
      const v = metricOf(n)
      // Only a FEASIBLE result may define `best` — same rule the frontier walk (`improvements`) and the
      // `improved` count below apply, and the module invariant at the top ("never credit a result the
      // engine itself rejected"). Otherwise a constraint-violating node's raw metric would inflate this
      // operator/theme's reported best and, through `directionProfit`, its treemap gain.
      if (n.feasible !== false && (e.best === null || bt(v, e.best))) e.best = v
      const parent = (n.parent_ids || []).map(p => nodes[p]).find(Boolean)
      const pm = parent ? metricOf(parent) : null
      if (n.feasible !== false && pm != null && bt(v, pm)) e.improved++
    }
  })
  return Object.values(out).sort((a, b) => b.improved - a.improved || b.evaluated - a.evaluated)
}

export const operatorEffectiveness = (nodes, dir) => rollup(nodes, dir, n => n.operator || 'unknown')
// Keep this compatibility projection aligned with events/digest.py::node_theme: new Ideas author
// concepts rather than the legacy `theme`, so their first concept axis becomes the coarse direction.
export const themeEffectiveness = (nodes, dir, state = null) =>
  rollup(nodes, dir, node => nodeTheme(node, state))

// Per-direction profit for the Directions overview. `idea.theme` remains the legacy wire field, but
// this UI projection calls the concept a direction. Controls stay in first-discovery order: live gain
// is evidence shown inside a control, never an implicit ranking that makes controls jump underhand.
export function directionProfit(state) {
  const nodes = activeNodeMap(state.nodes || {}, state)
  const dir = state.direction || 'min'
  const baseline = (improvements(nodes, dir, state)[0] || {}).to ?? null   // first feasible frontier value
  const byDirection = new Map(themeEffectiveness(nodes, dir, state).map(row => [row.key, row]))
  const directions = [...new Set(Object.values(nodes).sort((a, b) => a.id - b.id)
    .map(node => nodeTheme(node, state)).filter(Boolean))]
  return directions.map(direction => {
    const t = byDirection.get(direction)
    let gain = null
    if (baseline != null && t.best != null) gain = dir === 'min' ? baseline - t.best : t.best - baseline
    return { ...t, direction, gain, baseline }
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
  return d === 0 ? 0 : sxy / d
}

// Run-wide hyperparameter importance: |Pearson r| of each numeric param vs the metric across all
// evaluated feasible nodes ("which knobs mattered"). Needs ≥3 points per param. One source of truth
// shared by the Report's Learnings section and the Importance panel.
export function hyperImportance(state) {
  // CODEX AGENT: lifecycle-retired rows remain in the append-only fold for audit, but every current
  // report projection must use the same active population as analyze/DAG/Concepts.
  const nodes = Object.values(activeNodeMap(state.nodes || {}, state))
    .filter(n => n.status === 'evaluated' && n.metric != null && n.feasible !== false)
  const keys = new Set()
  nodes.forEach(n => Object.entries(n.idea?.params || {}).forEach(([k, v]) => { if (typeof v === 'number') keys.add(k) }))
  const rows = []
  keys.forEach(k => {
    const pts = nodes.filter(n => typeof n.idea?.params?.[k] === 'number')
    if (pts.length < 3) return
    const r = _pearson(pts.map(n => n.idea.params[k]), pts.map(n => n.metric))
    rows.push({ k, imp: Math.abs(r), r, n: pts.length })
  })
  return rows.sort((a, b) => b.imp - a.imp)
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
//   • draft / root (no parent) → `baseline` PLUS a brief description (its rationale/theme) so the
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
    if (!keys.length) return 'swept'
    const tok = (k) => {
      const vs = (sp[k] || []).filter(v => v != null)
      return (vs.length > 1 && vs.every(v => typeof v === 'number'))
        ? `${k}∈[${fmt(Math.min(...vs))}…${fmt(Math.max(...vs))}]` : k
    }
    return 'swept ' + (keys.length <= 2 ? keys.map(tok).join(', ') : `${keys.length} params`)
  }
  const parent = parents[0]
  if (!parent) {                                           // draft / root — nothing to diff against
    const what = brief(node.idea?.rationale) || brief(nodeTheme(node, state))
    return what ? `baseline · ${what}` : 'baseline'        // describe the baseline, don't just label it
  }
  if (node.idea?.change_summary) return brief(node.idea.change_summary)
  const lbl = paramDiffLabel(paramDiff(node, parent))      // diff vs the resolved parent directly
  if (lbl !== '—') return lbl
  // No param change vs the parent — still carry an explanatory caption rather than a blank card.
  return brief(node.idea?.rationale) || operatorMeta(node.operator).label
}

// Nodes that ran but made things worse than their parent (regressions) — the "tried, didn't help".
export function regressions(nodes, direction, state = null) {
  const dir = direction || 'min'
  const bt = better(dir)
  const out = []
  Object.values(nodes).filter(isEvaluated).forEach(n => {
    const parent = (n.parent_ids || []).map(p => nodes[p]).find(Boolean)
    const pm = parent ? metricOf(parent) : null
    if (pm != null && !bt(metricOf(n), pm) && metricOf(n) !== pm) {
      out.push({ id: n.id, operator: n.operator, metric: metricOf(n), parentId: parent.id,
                 parentMetric: pm, diff: paramDiff(n, parent), theme: nodeTheme(n, state) })
    }
  })
  return out.sort((a, b) => a.id - b.id)
}

export function failureBreakdown(nodes) {
  // CODEX AGENT: provider-authored error reasons are untrusted keys. A null-prototype index keeps
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
    operators: operatorEffectiveness(nodes, dir),
    themes: themeEffectiveness(nodes, dir, state),
    regressions: regressions(nodes, dir, state),
    failures: failureBreakdown(nodes),
    infeasible,
    nEval: evald.length,
  }
}

// Trust caveats that must not be buried in the verdict: reward-hack / leakage / drift / single-seed /
// infeasibility. Each is a chip with a deep-link to the panel that explains it. Pure (from state).
export function trustCaveats(state, best) {
  const out = []
  const hacks = state.reward_hacks || []
  if (best && hacks.some(h => h.node_id === best.id))
    out.push({ kind: 'reward-hack', severity: 'alarm', text: 'champion flagged as a possible reward-hack', panel: 'trust' })
  else if (hacks.length)
    out.push({ kind: 'reward-hack', severity: 'warn', text: `${hacks.length} node(s) flagged as possible reward-hacks`, panel: 'trust' })
  if (state.leakage?.leak)
    out.push({ kind: 'leakage', severity: 'alarm', text: 'data-leakage scan flagged this run', panel: 'data' })
  if ((state.drifts || []).length)
    out.push({ kind: 'drift', severity: 'warn', text: `${state.drifts.length} metric-drift divergence(s) caught`, panel: 'trust' })
  const infeasible = Object.values(activeNodeMap(state.nodes || {}, state))
    .filter(n => isEvaluated(n) && n.feasible === false)
  if (infeasible.length)
    out.push({ kind: 'infeasible', severity: 'warn', text: `${infeasible.length} evaluated node(s) violated a constraint`, panel: 'trust' })
  if (best && best.confirmed_mean == null)
    out.push({ kind: 'single-seed', severity: 'warn', text: 'champion is single-seed (not multi-seed confirmed)', panel: 'trust' })
  return out
}

// The deterministic verdict — classify the outcome purely from data so the Report always leads with a
// plain-language bottom line, even with no model. `a` is the analyze() result (reused, not recomputed).
export function verdict(state, a) {
  const dir = state.direction || 'min'
  const candidate = state.best_node_id != null ? state.nodes[state.best_node_id] : null
  const best = nodeIsActive(candidate, state) ? candidate : null
  const caveats = trustCaveats(state, best)
  if (!best || a.finalBest == null) {
    return { outcome: 'none', robustness: 'n/a', trust: caveats.length ? 'caveats' : 'unverified', best, caveats,
      headline: a.nEval ? 'No feasible result yet — every evaluated node violated a constraint.' : 'No experiments have been evaluated yet.' }
  }
  const baseline = a.firstBest, finalv = a.finalBest
  const gain = baseline != null ? finalv - baseline : 0
  const moved = baseline != null && finalv !== baseline
  const improved = moved && (dir === 'min' ? finalv < baseline : finalv > baseline)
  const outcome = !moved ? 'flat' : (improved ? 'improved' : 'regressed')
  // robustness from the champion's multi-seed confirmation
  let robustness = 'unconfirmed'
  if (best.confirmed_mean != null && (best.confirmed_seeds || 0) >= 2) {
    const sd = best.confirmed_std ?? 0, mean = Math.abs(best.confirmed_mean) || 1
    robustness = (sd <= Math.abs(gain) || sd / mean < 0.1) ? 'robust' : 'fragile'
  }
  // trust rollup
  const hasAlarm = caveats.some(c => c.severity === 'alarm')
  // No recorded caveat is not proof that every optional detector ran. Without the run config and
  // per-detector coverage in this folded report state, the honest roll-up is unverified, not green.
  const trust = hasAlarm ? 'suspect' : (caveats.length ? 'caveats' : 'unverified')
  const gainPct = baseline ? Math.abs(gain / baseline) * 100 : null
  const gainStr = fmt(Math.abs(gain)) + (gainPct != null ? ` (${fmt(gainPct, 1)}%)` : '')
  let headline
  if (outcome === 'improved') {
    const rob = robustness === 'robust' ? `robust across ${best.confirmed_seeds} seeds`
      : robustness === 'fragile' ? `but the multi-seed spread is wide` : `single-seed so far`
    headline = `Improved the metric by ${gainStr} over baseline — champion #${best.id} is ${rob}`
      + (trust === 'suspect' ? '; the win is flagged, treat with caution.'
        : trust === 'caveats' ? ' (with caveats).'
          : '; no trust flags are recorded, but detector coverage is not fully verified.')
  } else if (outcome === 'flat') {
    headline = `No improvement over the baseline yet — best stays at ${fmt(finalv)} (#${best.id}).`
  } else if (outcome === 'regressed') {
    headline = `Best result ${fmt(finalv)} (#${best.id}) is below the first baseline — search hasn't paid off.`
  }
  return { outcome, robustness, trust, best, baseline, gain, gainPct, direction: dir, caveats, headline }
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
    verdict: v.headline, verdict_source: 'deterministic',
    agent_report_caveats: rep?.caveats || [],
    deterministic_trust: { status: v.trust, caveats: v.caveats.map(caveat => caveat.text) },
    counts: { nodes: nodeCount, evaluated: a.nEval },
    deterministic_verdict: { headline: v.headline, outcome: v.outcome,
      robustness: v.robustness, trust: v.trust, caveats: v.caveats.map(caveat => caveat.text) },
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
  L.push(`# LoopLab run report — ${state.goal || state.task_id}`)
  L.push('')
  // Conclusion-first and authority-first: provider prose can explain, never replace, this verdict.
  L.push(`## Verdict`)
  L.push('')
  L.push(`**${v.headline}**`)
  if (v.caveats.length) {
    L.push('')
    L.push('Deterministic trust caveats: ' + v.caveats.map(c => c.text).join('; ') + '.')
  }
  L.push('')
  L.push(`- **Run:** ${state.run_id}`)
  L.push(`- **Optimization orientation:** ${optimizationLabel(state.direction)}`)
  L.push(`- **Status:** ${state.phase || (state.finished ? 'finished' : 'running')}${state.stop_reason ? ` (${state.stop_reason})` : ''}`)
  L.push(`- **Nodes:** ${nodeCount} — ${a.nEval} evaluated, ${Object.values(a.failures || {}).reduce((s, x) => s + x.length, 0)} failed`)
  if (champion) L.push(`- **Best:** node #${champion.id} · metric ${fmt(champion.confirmed_mean ?? champion.metric)}${champion.confirmed_mean != null ? ` ±${fmt(champion.confirmed_std)} (${champion.confirmed_seeds}×)` : ''} · params ${JSON.stringify(champion.idea?.params)}`)
  if (state.llm_cost) L.push(`- **LLM:** ${state.llm_cost.total_tokens} tokens · $${fmt(state.llm_cost.cost)}`)
  if (ctx.generation) L.push(`- **Run generation:** ${ctx.generation}`)
  if (ctx.snapshotSeq != null) L.push(`- **Snapshot event:** #${ctx.snapshotSeq}`)
  if (rep) {
    // Provider prose must remain inside the advisory quote with every platform newline form.
    const quote = text => String(text || '').split(/\r\n?|\n/).forEach(line => L.push(`> ${line}`))
    L.push('', '## Agent narrative (advisory)', '')
    quote('**Advisory only — not the deterministic verdict or trust decision.**')
    quote(`**Node coverage:** ${reportCoverageText(coverage)}`)
    const receipt = [rep.published_seq != null ? `event #${rep.published_seq}` : 'event unknown',
      rep.published_at != null ? new Date(rep.published_at * 1000).toISOString() : 'time unknown',
      rep.trigger ? `trigger ${rep.trigger}` : 'trigger unknown'].join(' · ')
    quote(`**Published:** ${receipt}`)
    if (rep.headline) { L.push('>'); quote(`**Agent headline:** ${rep.headline}`) }
    if (rep.verdict || rep.summary) { L.push('>'); quote(rep.verdict || rep.summary) }
    const advisoryLists = [
      ['Agent caveats', rep.caveats], ['Champion note', rep.champion_summary ? [rep.champion_summary] : []],
      ['What worked', rep.what_worked], ['Learnings', rep.learnings],
      ["What didn't work", rep.what_didnt], ['Next directions', rep.next_directions],
    ]
    advisoryLists.forEach(([label, items]) => {
      if (!items?.length) return
      L.push('>'); quote(`**${label}:**`); items.forEach(item => quote(`- ${item}`))
    })
  }
  L.push('')
  L.push(a.steps.length === 1 ? '## Metric baseline' : '## What worked — key improvements')
  if (a.steps.length) {
    L.push('')
    L.push('| step | node | operator | metric | Δ | what changed |')
    L.push('|---|---|---|---|---|---|')
    a.steps.forEach((s, i) => L.push(`| ${i + 1} | #${s.id} | ${s.operator}${s.theme ? ` (${s.theme})` : ''} | ${fmt(s.to)} | ${s.delta == null ? 'baseline' : fmt(s.delta)} | ${paramDiffLabel(s.diff)} |`))
    if (a.steps.length > 1) L.push(`\nTotal improvement: **${fmt(a.totalGain)}** across ${a.steps.length} steps (baseline ${fmt(a.firstBest)} → best ${fmt(a.finalBest)}).`)
  } else L.push('\n_No improving steps recorded yet._')
  L.push('')
  L.push('## What didn\'t work')
  const fr = Object.entries(a.failures)
  if (fr.length) { L.push('\n**Failures by reason:** ' + fr.map(([r, ns]) => `${r} (${ns.length})`).join(', ')) }
  if (a.regressions.length) { L.push(`\n**Regressions:** ${a.regressions.length} node(s) ran but did not beat their parent.`) }
  if (a.infeasible.length) { L.push(`\n**Infeasible:** ${a.infeasible.length} node(s) violated a constraint and were excluded.`) }
  const deadThemes = a.themes.filter(t => t.improved === 0)
  if (deadThemes.length) L.push(`\n**Primary concept axes that didn't pay off:** ${deadThemes.map(t => t.key).join(', ')}.`)
  if (!fr.length && !a.regressions.length && !a.infeasible.length && !deadThemes.length) L.push('\n_Nothing notably failed._')
  L.push('')
  L.push('## Operator effectiveness')
  L.push('')
  L.push('| operator | nodes | evaluated | improved | best |')
  L.push('|---|---|---|---|---|')
  a.operators.forEach(o => L.push(`| ${o.key} | ${o.count} | ${o.evaluated} | ${o.improved} | ${fmt(o.best)} |`))
  return L.join('\n')
}
