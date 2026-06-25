// Run-report analysis: derive the human-readable conclusions ("what worked / what didn't"), the
// key-improvement waterfall, and per-operator/per-theme effectiveness purely from the folded node
// set. Mirrors the engine's selection rule — only FEASIBLE evaluated nodes move the frontier — so
// the report never credits a result the engine itself rejected.

import { fmt, isSweep } from './util.js'

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
export function improvements(nodes, direction) {
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
        id: n.id, operator: n.operator, theme: n.idea?.theme || null,
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
  const out = {}
  Object.values(nodes).forEach(n => {
    const key = keyFn(n)
    if (key == null) return
    const e = (out[key] ||= { key, count: 0, evaluated: 0, improved: 0, failed: 0, best: null })
    e.count++
    if (n.status === 'failed') e.failed++
    if (isEvaluated(n)) {
      e.evaluated++
      const v = metricOf(n)
      if (e.best === null || bt(v, e.best)) e.best = v
      const parent = (n.parent_ids || []).map(p => nodes[p]).find(Boolean)
      const pm = parent ? metricOf(parent) : null
      if (n.feasible !== false && pm != null && bt(v, pm)) e.improved++
    }
  })
  return Object.values(out).sort((a, b) => b.improved - a.improved || b.evaluated - a.evaluated)
}

export const operatorEffectiveness = (nodes, dir) => rollup(nodes, dir, n => n.operator || 'unknown')
export const themeEffectiveness = (nodes, dir) => rollup(nodes, dir, n => n.idea?.theme || null)

// Per-direction (theme) profit for the Directions overview: how many experiments each theme ran and
// how much its best beat the run baseline (signed, direction-aware). Drives the treemap (area = count,
// color = gain). Pure — reuses the same frontier baseline the Report leads with.
export function directionProfit(state) {
  const nodes = state.nodes || {}
  const dir = state.direction || 'min'
  const baseline = (improvements(nodes, dir)[0] || {}).to ?? null   // first feasible frontier value
  return themeEffectiveness(nodes, dir).map(t => {
    let gain = null
    if (baseline != null && t.best != null) gain = dir === 'min' ? baseline - t.best : t.best - baseline
    return { theme: t.key, count: t.count, evaluated: t.evaluated, improved: t.improved, best: t.best, gain, baseline }
  }).sort((a, b) => (b.gain ?? -Infinity) - (a.gain ?? -Infinity) || b.count - a.count)
}

// For a MERGE node (≥2 parents): what each parent contributed — its theme + the "trick" it carried
// (that parent's own param-diff vs its parent). Powers the node card's "⊕ combines" line + the
// Inspector's "uses" list, so a merge says which techniques it actually fused.
export function mergeSummary(node, nodes) {
  if (!node || (node.parent_ids || []).length < 2) return []
  return node.parent_ids.map(pid => {
    const p = nodes[pid]
    if (!p) return { parentId: pid, theme: null, change: '' }
    const gp = (p.parent_ids || []).map(x => nodes[x]).find(Boolean)
    return { parentId: pid, theme: p.idea?.theme || null, change: paramDiffLabel(paramDiff(p, gp)) }
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
  const nodes = Object.values(state.nodes || {}).filter(n => n.status === 'evaluated' && n.metric != null && n.feasible !== false)
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

// The short "what changed vs the (first) parent" chip for a node card — the deterministic fallback
// when the Researcher didn't write a `change_summary`. Returns '' for a merge (it describes its
// fusion via mergeSummary instead) and when nothing changed, so callers needn't re-derive that rule.
export function changeLabel(node, nodes) {
  if ((node?.parent_ids || []).length > 1) return ''
  const parent = (node.parent_ids || []).map(p => nodes[p]).find(Boolean)
  if (!parent) return ''
  const lbl = paramDiffLabel(paramDiff(node, parent))
  return lbl === '—' ? '' : lbl
}

// The card's one-line "what this node did" chip. Deterministic so it shows IMMEDIATELY (no waiting on
// the late LLM `change_summary`), and correct for the cases the bare param-diff got wrong:
//   • sweep  → what was SEARCHED (the grid), not the single best value (the old `p=2.5` bug);
//   • draft / root (no parent) → `baseline` (nothing to diff against — it used to render nothing);
//   • else   → the agent's `change_summary` if it wrote one, else the param-diff vs the first parent.
// Returns '' for a merge (it renders its own ⊕ line) and when there's genuinely nothing to say.
export function nodeChip(node, nodes) {
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
  if (!parent) return 'baseline'                           // draft / root — nothing to diff against
  if (node.idea?.change_summary) return node.idea.change_summary
  const lbl = paramDiffLabel(paramDiff(node, parent))      // diff vs the resolved parent directly
  return lbl === '—' ? '' : lbl
}

// Nodes that ran but made things worse than their parent (regressions) — the "tried, didn't help".
export function regressions(nodes, direction) {
  const dir = direction || 'min'
  const bt = better(dir)
  const out = []
  Object.values(nodes).filter(isEvaluated).forEach(n => {
    const parent = (n.parent_ids || []).map(p => nodes[p]).find(Boolean)
    const pm = parent ? metricOf(parent) : null
    if (pm != null && !bt(metricOf(n), pm) && metricOf(n) !== pm) {
      out.push({ id: n.id, operator: n.operator, metric: metricOf(n), parentId: parent.id,
                 parentMetric: pm, diff: paramDiff(n, parent), theme: n.idea?.theme || null })
    }
  })
  return out.sort((a, b) => a.id - b.id)
}

export function failureBreakdown(nodes) {
  const by = {}
  Object.values(nodes).filter(n => n.status === 'failed').forEach(n => {
    (by[n.error_reason || 'unknown'] ||= []).push(n)
  })
  return by
}

// One call that assembles the whole analysis for the report.
export function analyze(state) {
  const nodes = state.nodes || {}
  const dir = state.direction
  const steps = improvements(nodes, dir)
  const evald = Object.values(nodes).filter(isEvaluated)
  const infeasible = evald.filter(n => n.feasible === false)
  return {
    steps,
    firstBest: steps.length ? steps[0].to : null,
    finalBest: steps.length ? steps[steps.length - 1].to : null,
    totalGain: steps.length > 1 ? steps[steps.length - 1].to - steps[0].to : 0,
    operators: operatorEffectiveness(nodes, dir),
    themes: themeEffectiveness(nodes, dir),
    regressions: regressions(nodes, dir),
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
  const infeasible = Object.values(state.nodes || {}).filter(n => isEvaluated(n) && n.feasible === false)
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
  const best = state.best_node_id != null ? state.nodes[state.best_node_id] : null
  const caveats = trustCaveats(state, best)
  if (!best || a.finalBest == null) {
    return { outcome: 'none', robustness: 'n/a', trust: 'caveats', best, caveats,
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
  const trust = hasAlarm ? 'suspect' : (caveats.length ? 'caveats' : 'trustworthy')
  const gainPct = baseline ? Math.abs(gain / baseline) * 100 : null
  const gainStr = fmt(Math.abs(gain)) + (gainPct != null ? ` (${fmt(gainPct, 1)}%)` : '')
  let headline
  if (outcome === 'improved') {
    const rob = robustness === 'robust' ? `robust across ${best.confirmed_seeds} seeds`
      : robustness === 'fragile' ? `but the multi-seed spread is wide` : `single-seed so far`
    headline = `Improved the metric by ${gainStr} over baseline — champion #${best.id} is ${rob}`
      + (trust === 'suspect' ? '; ⚠ the win is flagged, treat with caution.'
        : trust === 'caveats' ? ' (with caveats).' : ' and passes the trust checks.')
  } else if (outcome === 'flat') {
    headline = `No improvement over the baseline yet — best stays at ${fmt(finalv)} (#${best.id}).`
  } else if (outcome === 'regressed') {
    headline = `Best result ${fmt(finalv)} (#${best.id}) is below the first baseline — search hasn't paid off.`
  }
  return { outcome, robustness, trust, best, baseline, gain, gainPct, direction: dir, caveats, headline }
}

// A portable Markdown report (download / paste into a PR) built from the same analysis.
export function toMarkdown(state, best) {
  const a = analyze(state)
  const v = verdict(state, a)
  const rep = state.report || null
  const L = []
  L.push(`# LoopLab run report — ${state.goal || state.task_id}`)
  L.push('')
  // Conclusion-first: the verdict (agent headline when present, else the deterministic one) leads.
  L.push(`## Verdict`)
  L.push('')
  L.push(`**${rep?.headline || v.headline}**`)
  if (rep?.verdict) { L.push(''); L.push(rep.verdict) }
  if (v.caveats.length) { L.push(''); L.push('Caveats: ' + v.caveats.map(c => c.text).join('; ') + '.') }
  L.push('')
  L.push(`- **Run:** ${state.run_id}`)
  L.push(`- **Direction:** ${state.direction}`)
  L.push(`- **Status:** ${state.phase || (state.finished ? 'finished' : 'running')}${state.stop_reason ? ` (${state.stop_reason})` : ''}`)
  L.push(`- **Nodes:** ${Object.keys(state.nodes).length} — ${a.nEval} evaluated, ${Object.values(state.failures || {}).reduce((s, x) => s + x.length, 0)} failed`)
  if (best) L.push(`- **Best:** node #${best.id} · metric ${fmt(best.confirmed_mean ?? best.metric)}${best.confirmed_mean != null ? ` ±${fmt(best.confirmed_std)} (${best.confirmed_seeds}×)` : ''} · params ${JSON.stringify(best.idea?.params)}`)
  if (state.llm_cost) L.push(`- **LLM:** ${state.llm_cost.total_tokens} tokens · $${fmt(state.llm_cost.cost)}`)
  if (rep?.champion_summary) { L.push(''); L.push('### Champion'); L.push(''); L.push(rep.champion_summary) }
  if (rep && (rep.learnings || []).length) { L.push(''); L.push('### What we learned'); L.push(''); rep.learnings.forEach(x => L.push(`- ${x}`)) }
  if (rep && (rep.next_directions || []).length) { L.push(''); L.push('### Next directions'); L.push(''); rep.next_directions.forEach(x => L.push(`- ${x}`)) }
  L.push('')
  L.push('## What worked — key improvements')
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
  if (deadThemes.length) L.push(`\n**Themes that didn't pay off:** ${deadThemes.map(t => t.key).join(', ')}.`)
  if (!fr.length && !a.regressions.length && !a.infeasible.length && !deadThemes.length) L.push('\n_Nothing notably failed._')
  L.push('')
  L.push('## Operator effectiveness')
  L.push('')
  L.push('| operator | nodes | evaluated | improved | best |')
  L.push('|---|---|---|---|---|')
  a.operators.forEach(o => L.push(`| ${o.key} | ${o.count} | ${o.evaluated} | ${o.improved} | ${fmt(o.best)} |`))
  return L.join('\n')
}
