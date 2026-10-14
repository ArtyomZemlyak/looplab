import React from 'react'
import './tracking-panels.css'
import { uiText, uiPlural, useUILanguage } from './uiLanguage.js'
import { dirtyInputRows, workspaceRows } from './sourceSnapshot.js'

// The run's pinned sources (`run_started.workspace`) and its uncommitted inputs, beside the node's
// own base revision (BaseRevision) and patch (Code tab). doc 77 §1.
export default function SourceSnapshotPanel({ state }) {
  useUILanguage()
  const rows = workspaceRows(state?.workspace)
  const dirty = dirtyInputRows(state?.dirty_inputs)
  if (!rows.length && !dirty.length) return null
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
    <p className="muted">{uiText("This experiment's own changes are in the Code tab; its base is shown above.")}</p>
  </details>
}
