import { uiText, uiMessage, useUILanguage } from './uiLanguage.js'
import React, { useEffect, useMemo, useState } from 'react'
import { get } from './api.js'
import { fmt, fmtAgo } from './format.js'
import { campaignFolders, foldersSkipped, openCommand, rootListingCut } from './campaignRunsModel.js'

// The runs inside the root's CAMPAIGN folders, read-only (doc 70 70.1; the rules live in
// `campaignRunsModel.js`). One read when the list mounts: the rows are a finder, and each folder's
// command serves it as a root, where its runs open live.
export default function CampaignRuns() {
  useUILanguage()

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
    return <div className="notice resource-warning" role="status">{uiText("Campaign folders could not be listed.")}</div>
  }
  if (!folders.length) return null
  const skipped = foldersSkipped(payload)
  return <section aria-label={uiText("Campaign folders")}>
    <h2 className="muted" style={{ fontSize: 13, margin: '16px 2px 4px' }}>{uiText("Campaign folders")}</h2>
    <p className="muted" style={{ fontSize: 12, margin: '0 2px 8px' }}>{uiText("Runs inside a folder of the runs root, listed read-only. To open them, serve the folder as the root:")}</p>
    {folders.map(f => <details key={f.folder} className="notice compact">
      <summary>{f.folder} · {f.runs.length}{uiText(" run")}{f.runs.length === 1 ? '' : 's'}
        {(f.runsSkipped > 0 && uiMessage(" ({0} more not listed)", [f.runsSkipped]))}
        {((f.listingCut && uiText(' (its listing was cut at the entry bound)')))}</summary>
      <code style={{ userSelect: 'all' }}>{openCommand(f.runRoot)}</code>
      <ul>{f.runs.map(r => <li key={r.runId}>
        <strong>{r.runId}</strong>{r.taskId && ` · ${r.taskId}`}{r.phase && ` · ${r.phase}`}
        {((r.external && uiText(' · External agent')))}
        {(r.bestMetric !== null && uiMessage(" · best {0}", [fmt(r.bestMetric)]))}{((r.running && uiText(' · engine active')))}
        {r.updated !== null && <span className="muted"> · {fmtAgo(r.updated)}</span>}
      </li>)}</ul>
    </details>)}
    {skipped > 0 && <p className="muted">{skipped}{uiText(" more folder")}{skipped === 1 ? '' : 's'}{uiText(" not examined.")}</p>}
    {rootListingCut(payload) && <p className="muted">{uiText("The runs root's listing was cut at the entry bound.")}</p>}
  </section>
}
