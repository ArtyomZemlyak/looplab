import React, { useState } from 'react'
import './tracking-panels.css'
import { uiText, useUILanguage } from './uiLanguage.js'
import { get, runNodeApiPath } from './api.js'
import { useScopedResource } from './hooks.js'
import { runtimeParamsView } from './sourceSnapshot.js'

const MATCH_LABEL = { same: 'applied as declared', different: 'differs from the idea', absent: 'not in the configuration', conflict: 'conflicting values' }

// The configuration documents this attempt carried, flattened (GET …/runtime-params, doc 77 §2).
// Loaded only when opened; a key the documents do not state is absent, never zero.
export default function RuntimeSnapshotPanel({ runId, node, expectedGeneration }) {
  useUILanguage()
  const [open, setOpen] = useState(false)
  const attempt = Number.isSafeInteger(node?.attempt) ? node.attempt : 0
  const scope = open && expectedGeneration && node ? `${runId}@${expectedGeneration}:${node.id}:${attempt}:params` : ''
  const resource = useScopedResource(signal => get(runNodeApiPath(runId, node.id, '/runtime-params')
    + `?expected_generation=${encodeURIComponent(expectedGeneration)}&attempt=${attempt}`, { signal }),
  { scope, gate: scope ? null : 'idle' })
  const view = resource.data ? runtimeParamsView(resource.data) : null
  return <details className="runtime-snapshot" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{uiText('Parameters it ran with')}</summary>
    {open && resource.status === 'loading' && <p className="muted" role="status">{uiText('Loading…')}</p>}
    {open && resource.status === 'error' && <p className="muted" role="status">{uiText('Parameters are unavailable for this attempt.')}</p>}
    {view && <>
      {view.comparisons.length > 0 && <table className="tbl"><caption>{uiText('Idea parameters against the configuration')}</caption>
        <thead><tr><th scope="col">{uiText('Parameter')}</th><th scope="col">{uiText('Idea')}</th>
          <th scope="col">{uiText('Configuration')}</th><th scope="col">{uiText('Result')}</th></tr></thead>
        <tbody>{view.comparisons.map(row => <tr key={row.param}><th scope="row"><code>{row.param}</code></th>
          <td>{String(row.declared)}</td>
          <td>{(row.sources || []).map(s => `${s.value} (${s.source}${s.line ? `:${s.line}` : ''})`).join('; ') || '—'}</td>
          <td>{uiText(MATCH_LABEL[row.match] || row.match)}</td></tr>)}</tbody></table>}
      {view.carriers.length === 0 && <p className="muted">{uiText('No configuration document (.env, JSON, YAML, TOML) was found for this attempt.')}</p>}
      {view.carriers.map(carrier => {
        const rows = view.params.filter(p => p.source === carrier.path)
        return <div key={carrier.path} className="runtime-carrier">
          <div className="section-h"><code>{carrier.path}</code> <span className="muted">· {uiText(carrier.origin === 'authored' ? 'changed by this experiment' : 'task workspace')}
            {carrier.status !== 'read' ? ` · ${uiText(carrier.status === 'withheld' ? 'withheld: secret-like file name' : carrier.status)}` : ''}</span></div>
          {rows.length > 0 && <div className="md-table-wrap" role="region" tabIndex={0}><table className="tbl">
            <tbody>{rows.map(row => <tr key={row.key}><th scope="row"><code>{row.key}</code></th>
              <td style={row.numeric ? { textAlign: 'right' } : undefined}>{row.masked ? <span className="muted">***</span> : row.value}</td>
              <td className="muted">{row.line ? `:${row.line}` : ''}</td></tr>)}</tbody></table></div>}
        </div>
      })}
      {view.truncated && <p className="muted">{uiText('Only the first configuration rows are shown.')}</p>}
      <p className="muted">{uiText('Read from the documents only: a value set on the command line or in code is not visible here.')}</p>
    </>}
  </details>
}
