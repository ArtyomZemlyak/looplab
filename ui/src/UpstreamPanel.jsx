import React from 'react'
import './baseRevision.css'
import { baseChoices } from './baseRevision.js'
import { upstreamCheckSummary } from './upstreamCheckModel.js'

export default function UpstreamPanel({ state, onClose }) {
  const bases = baseChoices(state.nodes)
  const history = Array.isArray(state.upstream_history) ? state.upstream_history : []
  const advances = history.filter(row => row?.type === 'base_advanced')
  const check = upstreamCheckSummary(history)
  if (!bases.some(row => row.digest !== 'unknown') && !history.length) return null
  const discuss = () => {
    onClose?.()
    requestAnimationFrame(() => window.dispatchEvent(new CustomEvent('ll:focus-assistant', { detail: {
      text: 'Review reusable capabilities and recorded base history for this run. Read upstream_status and Maintainer instructions. Explain generalization, old default, real gate results and costs. Before base advance, pause and wait for engine exit, then require explicit current-evidence CAS; resume separately. Missing evidence is not a pass.' } })))
  }
  return <section className="ov-section upstream-panel" aria-label="Code bases and reusable capabilities">
    <div className="ov-section-head"><h3>Code bases & reusable capabilities</h3><span>{advances.length} recorded advancements</span></div>
    {state.upstream_base && <p>Promoted base <code title={state.upstream_base.selector?.digest}>{state.upstream_base.selector?.digest?.slice(0, 12)}</code> · from experiment #{state.upstream_base.source_node_id}</p>}
    <p className="muted">Experiments retain the base they actually evaluated. Promotion changes future work; scores across bases need comparable evidence.</p>
    <div className="upstream-bases">{bases.map(row => <span className="pill" key={row.digest} title={row.digest}>{row.digest === 'unknown' ? 'Base unknown' : row.digest.slice(0, 12)} · {row.count} experiments</span>)}</div>
    {check && <p>{check.status === 'unfinished' ? 'Unfinished check — read progress; recover interrupted checks explicitly.'
      : check.status === 'abandoned' ? 'Abandoned check — late results cannot authorize advancement.'
      : check.status === 'unknown' ? 'Check evidence unavailable — read complete upstream history.'
      : <>Recorded completed check: <strong>{check.status}</strong> · {check.executions === null ? 'execution count unavailable' : `${check.executions} explicit executions`} · {check.seconds === null ? 'cost unavailable' : `${check.seconds.toFixed(1)} s`}</>}</p>}
    {check && <p className="muted">Read current upstream evidence before advancement; recorded results do not approve it.</p>}
    {advances.length > 0 && <details><summary>Capability origins</summary><ol>{advances.map(row => <li key={row.seq}>Experiment #{row.source_node_id} → <code>{row.selector?.digest?.slice(0, 12)}</code> · {row.summary} · flag {row.flag?.name}, old default {row.flag?.default}</li>)}</ol></details>}
    <p className="muted">{state.upstream_enabled
      ? 'Pause → wait for engine exit → Maintainer proposal → measured checks → explicit base advance → resume. Interrupted checks require operator recovery.'
      : 'Capability promotion was not enabled for this run. Start a pinned task with declared upstream tests and regression probes to use it.'}</p>
    {state.upstream_enabled && <button type="button" className="btn" onClick={discuss}>Review capabilities in Assistant</button>}
  </section>
}
