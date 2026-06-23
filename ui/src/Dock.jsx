import React, { useEffect, useState } from 'react'
import { get, fmt } from './util.js'
import { Trajectory, Gantt } from './charts.jsx'

const NARR = {
  run_started: (d) => `run started — ${d.goal || d.task_id} (${d.direction})`,
  node_created: (d) => `node #${d.node_id} via ${d.operator}${d.idea?.rationale ? ' — ' + d.idea.rationale.slice(0, 80) : ''}`,
  node_evaluated: (d) => `node #${d.node_id} → ${fmt(d.metric)}`,
  node_failed: (d) => `node #${d.node_id} failed (${d.reason})`,
  node_confirmed: (d) => `node #${d.node_id} confirmed: ${fmt(d.mean)} ±${fmt(d.std)} (${d.seeds}×)`,
  best_confirmed: (d) => `robust winner: #${d.node_id}${d.significant ? ' (significant >1SE)' : ''}`,
  ablate: (d) => `ablated #${d.parent_id}: ${Object.entries(d.impacts || {}).map(([k, v]) => `${k}=${fmt(v, 2)}`).join(', ')}`,
  data_leakage: (d) => `leakage scan: ${d.leak ? 'LEAK DETECTED' : 'clean'}`,
  approval_requested: (d) => `awaiting approval of #${d.node_id}`,
  approval_granted: (d) => `approved #${d.node_id}`,
  pause: () => 'paused by operator', resume: () => 'resumed', run_abort: () => 'abort requested',
  node_abort: (d) => `stop requested for #${d.node_id}`, budget_extend: (d) => `budget extended ${JSON.stringify(d)}`,
  hint: (d) => `hint: ${d.text}`, promote: (d) => `promoted #${d.node_id} → ${d.alias || 'champion'}`,
  run_finished: (d) => `run finished${d.reason ? ' (' + d.reason + ')' : ''}`,
  llm_cost: (d) => `LLM: ${d.total_tokens} tokens, $${fmt(d.cost)}`,
}

// The node an event refers to, if any — lets a feed/timeline click drill into that node.
function eventNode(e) {
  const d = e.data || {}
  return d.node_id ?? d.parent_id ?? null
}

export default function Dock({ runId, liveSeq, viewSeq, setViewSeq, onFocus, collapsed, onToggleCollapse, height = 230 }) {
  const [tab, setTab] = useState('travel')
  const [log, setLog] = useState([])
  const [trace, setTrace] = useState(null)
  const [filter, setFilter] = useState('')
  useEffect(() => { get(`/api/runs/${runId}/log`).then(setLog).catch(() => {}) }, [runId, liveSeq])
  useEffect(() => { if (tab === 'timeline') get(`/api/runs/${runId}/trace`).then(setTrace).catch(() => {}) }, [tab, runId, liveSeq])
  const atLive = viewSeq == null || viewSeq >= liveSeq

  // Click an event → select its node, jump the scrubber to that moment, open the right inspector tab.
  const focusEvent = (e) => {
    const nid = eventNode(e)
    if (nid == null) { setViewSeq(e.seq); return }
    onFocus?.(Number(nid), e.type === 'node_created' ? 'Reasoning' : 'Overview', e.seq)
  }
  const matches = (e) => {
    if (!filter) return true
    const q = filter.toLowerCase()
    const narr = (NARR[e.type] || (() => ''))(e.data)
    return e.type.toLowerCase().includes(q) || String(narr).toLowerCase().includes(q)
  }
  const Row = ({ e, showSeq }) => (
    <div className="ev clickable" key={e.seq} onClick={() => focusEvent(e)}
         title={eventNode(e) != null ? `open node #${eventNode(e)} @ seq ${e.seq}` : `jump to seq ${e.seq}`}>
      {showSeq && <span className="t">{e.seq}</span>}
      <span className="ty">{e.type}</span>
      <span>{(NARR[e.type] || ((d) => JSON.stringify(d).slice(0, 80)))(e.data)}</span>
      {eventNode(e) != null && <span className="ev-go">↗</span>}
    </div>
  )

  return (
    <div className="dock">
      <div className="dock-tabs">
        {[['travel', 'Time-travel'], ['feed', 'Activity'], ['timeline', 'Timeline']].map(([k, l]) =>
          <div key={k} className={'tab' + (k === tab ? ' active' : '')} onClick={() => !collapsed && setTab(k)}>{l}</div>)}
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn sm ghost dock-collapse" title={collapsed ? 'expand' : 'collapse'}
                onClick={onToggleCollapse}>{collapsed ? '▴' : '▾'}</button>
      </div>
      {!collapsed && <div className="dock-body" style={{ height }}>
        {tab === 'travel' && <div className="scrubber">
          <button className="btn sm" onClick={() => setViewSeq(null)} disabled={atLive}>⏵ Live</button>
          <input type="range" min={0} max={Math.max(0, liveSeq)} value={atLive ? liveSeq : viewSeq}
                 onChange={e => setViewSeq(Number(e.target.value))} />
          <span className={atLive ? 'live-tag' : 'hist-tag'}>{atLive ? `live · seq ${liveSeq}` : `replay · seq ${viewSeq}/${liveSeq}`}</span>
        </div>}
        {tab === 'travel' && <div style={{ marginTop: 8 }} className="feed">
          {log.filter(e => atLive || e.seq <= viewSeq).slice(-8).reverse().map(e => <Row key={e.seq} e={e} />)}
        </div>}
        {tab === 'feed' && <>
          <input className="text feed-filter" placeholder="filter events (type or text)…"
                 value={filter} onChange={e => setFilter(e.target.value)} />
          <div className="feed">
            {log.filter(e => (atLive || e.seq <= viewSeq) && matches(e)).slice().reverse().map(e => <Row key={e.seq} e={e} showSeq />)}
          </div>
        </>}
        {tab === 'timeline' && (trace
          ? <Gantt spans={trace} onPick={(nid) => onFocus?.(Number(nid), 'Reasoning', null)} />
          : <div className="muted">loading spans…</div>)}
      </div>}
    </div>
  )
}
