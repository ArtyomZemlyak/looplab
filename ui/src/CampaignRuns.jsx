import React, { useEffect, useMemo, useState } from 'react'
import { get } from './api.js'
import { fmt, fmtAgo } from './format.js'
import { campaignFolders, foldersSkipped, openCommand, rootListingCut } from './campaignRunsModel.js'

// The runs inside the root's CAMPAIGN folders, read-only (doc 70 70.1; the rules live in
// `campaignRunsModel.js`). One read when the list mounts: the rows are a finder, and each folder's
// command serves it as a root, where its runs open live.
export default function CampaignRuns() {
  const [payload, setPayload] = useState(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    get('/api/campaign-runs', { signal: controller.signal, cache: 'no-store' })
      .then(setPayload, () => { if (!controller.signal.aborted) setFailed(true) })
    return () => controller.abort()
  }, [])
  const folders = useMemo(() => campaignFolders(payload), [payload])
  if (failed) {
    return <div className="notice resource-warning" role="status">Campaign folders could not be listed.</div>
  }
  if (!folders.length) return null
  const skipped = foldersSkipped(payload)
  return <section aria-label="Campaign folders">
    <h2 className="muted" style={{ fontSize: 13, margin: '16px 2px 4px' }}>Campaign folders</h2>
    <p className="muted" style={{ fontSize: 12, margin: '0 2px 8px' }}>
      Runs inside a folder of the runs root, listed read-only. To open them, serve the folder as the root:
    </p>
    {folders.map(f => <details key={f.folder} className="notice compact">
      <summary>{f.folder} · {f.runs.length} run{f.runs.length === 1 ? '' : 's'}
        {f.runsSkipped > 0 && ` (${f.runsSkipped} more not listed)`}
        {f.listingCut && ' (its listing was cut at the entry bound)'}</summary>
      <code style={{ userSelect: 'all' }}>{openCommand(f.runRoot)}</code>
      <ul>{f.runs.map(r => <li key={r.runId}>
        <strong>{r.runId}</strong>{r.taskId && ` · ${r.taskId}`}{r.phase && ` · ${r.phase}`}
        {r.bestMetric !== null && ` · best ${fmt(r.bestMetric)}`}{r.running && ' · running'}
        {r.updated !== null && <span className="muted"> · {fmtAgo(r.updated)}</span>}
      </li>)}</ul>
    </details>)}
    {skipped > 0 && <p className="muted">{skipped} more folder{skipped === 1 ? '' : 's'} not examined.</p>}
    {rootListingCut(payload) && <p className="muted">The runs root's listing was cut at the entry bound.</p>}
  </section>
}
