// Run-report analysis: derive the human-readable conclusions ("what worked / what didn't"), the
// key-improvement waterfall, and per-operator/per-theme effectiveness purely from the folded node
// set. Mirrors the engine's selection rule — only FEASIBLE evaluated nodes move the frontier — so
// the report never credits a result the engine itself rejected.

import { fmt } from './util.js'

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

// A portable Markdown report (download / paste into a PR) built from the same analysis.
export function toMarkdown(state, best) {
  const a = analyze(state)
  const L = []
  L.push(`# LoopLab run report — ${state.goal || state.task_id}`)
  L.push('')
  L.push(`- **Run:** ${state.run_id}`)
  L.push(`- **Direction:** ${state.direction}`)
  L.push(`- **Status:** ${state.phase || (state.finished ? 'finished' : 'running')}${state.stop_reason ? ` (${state.stop_reason})` : ''}`)
  L.push(`- **Nodes:** ${Object.keys(state.nodes).length} — ${a.nEval} evaluated, ${Object.values(state.failures || {}).reduce((s, x) => s + x.length, 0)} failed`)
  if (best) L.push(`- **Best:** node #${best.id} · metric ${fmt(best.confirmed_mean ?? best.metric)} · params ${JSON.stringify(best.idea?.params)}`)
  if (state.llm_cost) L.push(`- **LLM:** ${state.llm_cost.total_tokens} tokens · $${fmt(state.llm_cost.cost)}`)
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
