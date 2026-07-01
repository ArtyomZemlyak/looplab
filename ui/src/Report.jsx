import React, { useEffect, useMemo, useRef, useState } from 'react'
import { get, fmt, fmtInt, CONTROL } from './util.js'
import { Trajectory, ImprovementWaterfall } from './charts.jsx'
import { analyze, verdict, paramDiffLabel, toMarkdown, hyperImportance } from './report.js'
import { MemoCard } from './panels.jsx'
import Markdown from './markdown.jsx'

const TRUST_CLASS = { trustworthy: 'ok', caveats: 'warn', suspect: 'alarm' }
const OUTCOME_LABEL = { improved: '▲ improved', flat: '— flat', regressed: '▼ regressed', none: 'no result' }

// Conclusion-first banner: the agent headline (if any) else the deterministic verdict, trust-colored,
// with inline caveat chips that deep-link to the explaining panel.
function VerdictBanner({ v, rep, onOpenPanel }) {
  const cls = TRUST_CLASS[v.trust] || 'warn'
  return (
    <div className={'verdict-banner ' + cls}>
      <div className="verdict-row">
        <span className={'verdict-pill ' + (v.outcome === 'improved' ? 'ok' : v.outcome === 'regressed' ? 'fail' : '')}>{OUTCOME_LABEL[v.outcome] || v.outcome}</span>
        {v.robustness && v.robustness !== 'n/a' && <span className="pill">{v.robustness}</span>}
        <span className="pill">{v.trust}</span>
      </div>
      <div className="verdict-headline">{rep?.headline || v.headline}</div>
      {rep?.verdict && <div className="verdict-text"><Markdown text={rep.verdict} /></div>}
      {v.caveats.length > 0 && <div className="caveat-chips">
        {v.caveats.map((c, i) => <button key={i} className={'caveat-chip ' + c.severity}
          title={`see ${c.panel} →`} onClick={() => onOpenPanel?.(c.panel)}>⚠ {c.text}</button>)}
      </div>}
    </div>
  )
}

function ChampionCard({ state, best }) {
  if (!best) return null
  const m = best.confirmed_mean ?? best.metric
  return (
    <div className="champion-card">
      <div className="kv">
        <div className="k">champion</div><div className="v">#{best.id} · {best.operator}{best.idea?.theme ? ` · ${best.idea.theme}` : ''}</div>
        <div className="k">metric</div><div className="v"><b>{fmt(m)}</b>{best.confirmed_mean != null
          ? <span className="muted"> ±{fmt(best.confirmed_std)} over {best.confirmed_seeds} seed{best.confirmed_seeds === 1 ? '' : 's'}</span>
          : <span className="muted"> (single-seed)</span>}</div>
        <div className="k">params</div><div className="v">{Object.keys(best.idea?.params || {}).length
          ? Object.entries(best.idea.params).map(([k, val]) => `${k}=${fmt(val)}`).join(', ') : '—'}</div>
        {(best.parent_ids || []).length > 0 && <><div className="k">lineage</div><div className="v">{best.parent_ids.map(p => '#' + p).join(' → ')}</div></>}
        <div className="k">feasible</div><div className="v">{best.feasible === false ? '⚠ constraint violated' : 'yes'}</div>
      </div>
    </div>
  )
}

function List({ items }) {
  if (!items || !items.length) return null
  return <ul className="bul">{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
}

export default function ReportView({ state, runId, onOpenPanel, onToast, onPickNode }) {
  const best = state.best_node_id != null ? state.nodes[state.best_node_id] : null
  const failed = Object.values(state.nodes).filter(n => n.status === 'failed')
  const a = useMemo(() => analyze(state), [state])
  const v = useMemo(() => verdict(state, a), [state, a])
  const rep = state.report || null
  const imp = useMemo(() => hyperImportance(state).slice(0, 6), [state])
  const memos = state.research || []
  const [bestCode, setBestCode] = useState(null)
  const [openMemo, setOpenMemo] = useState(memos.length ? memos.length - 1 : null)
  const [refreshing, setRefreshing] = useState(false)
  const lastAt = useRef(rep?.at_node)
  useEffect(() => {
    setBestCode(null)
    if (!best) return
    let alive = true
    get(`/api/runs/${runId}/nodes/${best.id}`).then(d => { if (alive) setBestCode(d) }).catch(() => {})
    return () => { alive = false }
  }, [runId, best?.id])
  // Refresh completes when a newer report (different at_node / trigger) folds in via SSE.
  useEffect(() => {
    if (refreshing && (rep?.at_node !== lastAt.current || rep?.trigger === 'manual')) setRefreshing(false)
    lastAt.current = rep?.at_node
  }, [rep?.at_node, rep?.trigger])

  const dl = (name, text, type) => {
    const blob = new Blob([text], { type }); const u = URL.createObjectURL(blob)
    const el = document.createElement('a'); el.href = u; el.download = name; el.click(); URL.revokeObjectURL(u)
  }
  const refresh = async () => {
    setRefreshing(true)
    try {
      const r = await CONTROL.refreshReport(runId)
      if (r && r.ok === false) { setRefreshing(false); onToast?.('report: no model reachable') }
    } catch (e) { setRefreshing(false); onToast?.('refresh failed: ' + e.message) }
    setTimeout(() => setRefreshing(false), 30000)   // safety net if no SSE update arrives
  }
  const impr = (s) => (s.delta == null ? true : (state.direction === 'min' ? s.delta < 0 : s.delta > 0))
  const modelCard = () => JSON.stringify({
    task: state.task_id, goal: state.goal, direction: state.direction, run_id: state.run_id,
    champion: best ? { node_id: best.id, operator: best.operator, metric: best.confirmed_mean ?? best.metric,
      confirmed: best.confirmed_mean != null, params: best.idea?.params || {}, lineage: best.parent_ids || [] } : null,
    verdict: rep?.headline || v.headline, counts: { nodes: Object.keys(state.nodes).length, evaluated: a.nEval },
  }, null, 2)

  return (
    <div className="report-view">
      <div className="toolbar report-toolbar">
        <button className="btn sm primary" disabled={refreshing} onClick={refresh}
          title="Agent rewrites the report from all results so far">{refreshing ? '⟳ Refreshing…' : '🔄 Refresh report'}</button>
        <span className="muted report-fresh">{rep
          ? `agent report @ ${rep.at_node} nodes${rep.trigger ? ` · ${rep.trigger}` : ''}`
          : 'deterministic report (no agent narrative yet)'}</span>
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn sm" onClick={() => window.print()}>🖨 Print / PDF</button>
        <button className="btn sm" onClick={() => dl(`${state.run_id}_report.md`, toMarkdown(state, best), 'text/markdown')}>⬇ Markdown</button>
        {best && <button className="btn sm" disabled={!bestCode?.code} onClick={() => dl(`solution_node${best.id}.py`, bestCode.code, 'text/x-python')}>⬇ Solution</button>}
        <button className="btn sm" onClick={() => dl(`${state.run_id}_model_card.json`, modelCard(), 'application/json')}>⬇ Model card</button>
      </div>

      <h1 className="report-title">{state.goal || state.task_id}</h1>
      <div className="report-sub muted">{state.run_id} · {state.direction} · {state.phase || (state.finished ? 'finished' : 'running')}{state.stop_reason ? ` (${state.stop_reason})` : ''}
        {' · '}{Object.keys(state.nodes).length} nodes ({a.nEval} evaluated, {failed.length} failed)
        {state.llm_cost && ` · ${fmtInt(state.llm_cost.total_tokens)} tokens · $${fmt(state.llm_cost.cost)}`}</div>

      <VerdictBanner v={v} rep={rep} onOpenPanel={onOpenPanel} />

      {best && <><div className="section-h">Champion — the answer</div>
        <ChampionCard state={state} best={best} />
        {rep?.champion_summary && <div className="v" style={{ marginTop: 6 }}><Markdown text={rep.champion_summary} /></div>}</>}

      {a.steps.length > 0 && <>
        <div className="section-h">How the metric got better</div>
        <Trajectory nodes={Object.values(state.nodes)} direction={state.direction} steps={a.steps} onPick={onPickNode} />
        <ImprovementWaterfall steps={a.steps} direction={state.direction} />
        <table className="tbl"><thead><tr><th>#</th><th>node</th><th>operator</th><th>metric</th><th>Δ</th><th>what changed</th></tr></thead><tbody>
          {a.steps.map((s, i) => <tr key={s.id}>
            <td>{i + 1}</td><td>#{s.id}</td><td>{s.operator}{s.theme ? <span className="pill" style={{ marginLeft: 4 }}>{s.theme}</span> : null}</td>
            <td>{fmt(s.to)}</td>
            <td style={{ color: s.delta == null ? 'var(--accent)' : (impr(s) ? 'var(--ok)' : 'var(--fail)') }}>{s.delta == null ? 'baseline' : fmt(s.delta)}</td>
            <td className="muted">{paramDiffLabel(s.diff)}</td></tr>)}
        </tbody></table>
        {a.steps.length > 1 && <div className="muted">Total improvement <b>{fmt(a.totalGain)}</b> over {a.steps.length} steps (baseline {fmt(a.firstBest)} → best {fmt(a.finalBest)}).</div>}
      </>}

      {(rep?.what_worked?.length || rep?.learnings?.length || rep?.next_directions?.length || memos.length || imp.length) ? <>
        <div className="section-h">What we learned</div>
        {rep?.what_worked?.length > 0 && <><div className="muted">what worked</div><List items={rep.what_worked} /></>}
        {rep?.learnings?.length > 0 && <><div className="muted">learnings</div><List items={rep.learnings} /></>}
        {rep?.next_directions?.length > 0 && <><div className="muted">next directions</div><List items={rep.next_directions} /></>}
        {imp.length > 0 && <>
          <div className="muted" style={{ marginTop: 6 }}>which knobs mattered (|correlation| with the metric)</div>
          <table className="tbl"><thead><tr><th>param</th><th>importance</th><th>r</th><th>n</th></tr></thead><tbody>
            {imp.map(row => <tr key={row.k}><td>{row.k}</td><td>{fmt(row.imp, 3)}</td>
              <td className="muted">{row.r >= 0 ? '+' : ''}{fmt(row.r, 3)}</td><td className="muted">{row.n}</td></tr>)}
          </tbody></table></>}
        {memos.length > 0 && <div style={{ marginTop: 8 }}>
          {memos.map((m, i) => <MemoCard key={i} memo={m} idx={i} open={openMemo === i} onToggle={(k) => setOpenMemo(o => o === k ? null : k)} />)}
        </div>}
      </> : null}

      <div className="section-h">What didn't work</div>
      <div className="cardgrid" style={{ marginBottom: 10 }}>
        {Object.entries(a.failures).map(([r, ns]) => <div key={r} className="stat"><div className="n">{ns.length}</div><div className="l">failed · {r}</div></div>)}
        {a.regressions.length > 0 && <div className="stat"><div className="n">{a.regressions.length}</div><div className="l">regressions</div></div>}
        {a.infeasible.length > 0 && <div className="stat"><div className="n">{a.infeasible.length}</div><div className="l">infeasible</div></div>}
        {(rep?.what_didnt || []).length === 0 && !Object.keys(a.failures).length && !a.regressions.length && !a.infeasible.length && <div className="stat"><div className="n">0</div><div className="l">nothing notably failed</div></div>}
      </div>
      {rep?.what_didnt?.length > 0 && <List items={rep.what_didnt} />}

      {best && <><div className="section-h">Reproduce — winning solution</div>
        <pre className="code">{bestCode?.code || '(loading…)'}</pre></>}
    </div>
  )
}
