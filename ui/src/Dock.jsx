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

export default function Dock({ runId, liveSeq, viewSeq, setViewSeq }) {
  const [tab, setTab] = useState('travel')
  const [log, setLog] = useState([])
  const [trace, setTrace] = useState(null)
  useEffect(() => { get(`/api/runs/${runId}/log`).then(setLog).catch(() => {}) }, [runId, liveSeq])
  useEffect(() => { if (tab === 'timeline') get(`/api/runs/${runId}/trace`).then(setTrace).catch(() => {}) }, [tab, runId, liveSeq])
  const atLive = viewSeq == null || viewSeq >= liveSeq

  return (
    <div className="dock">
      <div className="dock-tabs">
        {[['travel', 'Time-travel'], ['feed', 'Activity'], ['timeline', 'Timeline']].map(([k, l]) =>
          <div key={k} className={'tab' + (k === tab ? ' active' : '')} onClick={() => setTab(k)}>{l}</div>)}
      </div>
      <div className="dock-body">
        {tab === 'travel' && <div className="scrubber">
          <button className="btn sm" onClick={() => setViewSeq(null)} disabled={atLive}>⏵ Live</button>
          <input type="range" min={0} max={Math.max(0, liveSeq)} value={atLive ? liveSeq : viewSeq}
                 onChange={e => setViewSeq(Number(e.target.value))} />
          <span className={atLive ? 'live-tag' : 'hist-tag'}>{atLive ? `live · seq ${liveSeq}` : `replay · seq ${viewSeq}/${liveSeq}`}</span>
        </div>}
        {tab === 'travel' && <div style={{ marginTop: 8 }} className="feed">
          {log.filter(e => atLive || e.seq <= viewSeq).slice(-6).reverse().map(e =>
            <div className="ev" key={e.seq}><span className="ty">{e.type}</span><span>{(NARR[e.type] || (() => ''))(e.data)}</span></div>)}
        </div>}
        {tab === 'feed' && <div className="feed">
          {log.filter(e => atLive || e.seq <= viewSeq).slice().reverse().map(e =>
            <div className="ev" key={e.seq}><span className="t">{e.seq}</span><span className="ty">{e.type}</span><span>{(NARR[e.type] || (() => JSON.stringify(e.data).slice(0, 80)))(e.data)}</span></div>)}
        </div>}
        {tab === 'timeline' && (trace ? <Gantt spans={trace} /> : <div className="muted">loading spans…</div>)}
      </div>
    </div>
  )
}
