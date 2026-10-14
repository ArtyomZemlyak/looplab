import React, { useState } from 'react'
import './tracking-panels.css'
import { uiText, uiPlural, useUILanguage } from './uiLanguage.js'
import { get, runApiPath } from './api.js'
import { useScopedResource } from './hooks.js'
import { attemptCodeRow, dirtyInputRows, effectiveConfigRows, workspaceRows } from './sourceSnapshot.js'

// This attempt's commit and effective configuration from the run-owned repository (doc 79),
// read only when the operator opens it.
function AttemptCode({ runId, node }) {
  useUILanguage()
  const [open, setOpen] = useState(false)
  const attempt = Number.isSafeInteger(node?.attempt) ? node.attempt : 0
  const scope = open && runId && node ? `${runId}:code:${node.id}:${attempt}` : ''
  const resource = useScopedResource(signal => get(runApiPath(runId, '/code'), { signal, cache: 'no-store' }),
    { scope, gate: scope ? null : 'idle' })
  const row = resource.data ? attemptCodeRow(resource.data, node.id, attempt) : null
  const config = row?.effective_config ? effectiveConfigRows(row.effective_config) : []
  return <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{uiText('This attempt: commit and effective configuration')}</summary>
    {open && resource.status === 'loading' && <p className="muted" role="status">{uiText('Loading…')}</p>}
    {open && resource.status === 'error' && <p className="muted" role="status">{uiText("The run's code repository could not be read.")}</p>}
    {resource.data && !row && <p className="muted">{uiText('No commit is recorded for this attempt.')}</p>}
    {row && <ul className="bul">
      <li>{uiText('Commit')} {row.commit ? <code title={row.commit}>{String(row.commit).slice(0, 12)}</code> : <span className="muted">{uiText(row.unavailable ? 'unavailable' : 'not recorded')}</span>}
        {row.base_digest && <> · {uiText('base')} <code>{String(row.base_digest).slice(0, 12)}</code></>}</li>
      {Number.isSafeInteger(row.changed_count) && <li>{uiPlural(row.changed_count, '{0} changed file', '{0} changed files')}
        {Array.isArray(row.changed) && row.changed.length > 0 && <>: {row.changed.slice(0, 12).map(c => <code key={c.path || c}>{c.path || c} </code>)}</>}</li>}
    </ul>}
    {config.length > 0 && <table className="tbl"><caption>{uiText('Effective configuration (what the evaluation used)')}</caption>
      <tbody>{config.map(c => <tr key={c.key}><th scope="row"><code>{c.key}</code></th><td>{c.value}</td><td className="muted">{c.source}</td></tr>)}</tbody></table>}
  </details>
}

// The run's pinned sources (`run_started.workspace`) and its uncommitted inputs, beside the node's
// own base revision (BaseRevision) and patch (Code tab). doc 77 §1.
export default function SourceSnapshotPanel({ state, runId, node }) {
  useUILanguage()
  const rows = workspaceRows(state?.workspace)
  const dirty = dirtyInputRows(state?.dirty_inputs)
  if (!rows.length && !dirty.length && !node) return null
  return <details className="source-snapshot">
    <summary>{uiText('Source code of the run')}{state?.workspace_changed ? ` · ${uiText('changed since launch')}` : ''}</summary>
    <ul className="bul">{rows.map(row => <li key={row.source}>
      <code>{row.source}</code>{' — '}
      {row.kind === 'git'
        ? <>{uiText('Git commit')} <code title={row.commit}>{row.commit.slice(0, 12)}</code> <span className="muted">({uiText('captured at launch')})</span></>
        : row.kind === 'absent' ? <span className="muted">{uiText('absent at launch')}</span>
          : <>{uiText('commit unknown (not a git checkout)')}{row.digest && <> · {uiText('content digest')} <code>{row.digest.slice(0, 16)}</code></>}</>}
    </li>)}</ul>
    {dirty.length > 0 && <>
      <p className="muted">{uiPlural(dirty.length, '{0} uncommitted input at launch', '{0} uncommitted inputs at launch')}</p>
      <ul className="bul">{dirty.slice(0, 50).map(row => <li key={row.path}><code>{row.status ? `${row.status} ` : ''}{row.path}</code></li>)}</ul>
    </>}
    {node && <AttemptCode runId={runId} node={node} />}
    <p className="muted">{uiText("This experiment's own changes are in the Code tab; its base is shown above.")}</p>
  </details>
}
