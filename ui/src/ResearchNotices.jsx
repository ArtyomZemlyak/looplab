import React, { useState } from 'react'
import { get, runApiPath } from './api.js'
import { uiText } from './uiLanguage.js'

export default function ResearchNotices({ runId, generation }) {
  const [page, setPage] = useState(null), [error, setError] = useState(false)
  const load = async offset => {
    setError(false)
    try {
      const params = new URLSearchParams({ expected_generation: generation, offset: String(offset), limit: '20' })
      setPage(await get(`${runApiPath(runId, '/research-notices')}?${params}`, { cache: 'no-store' }))
    } catch { setError(true) }
  }
  return <section>
    <button type="button" onClick={() => load(0)}>{uiText('Inspect saved intake notices')}</button>
    {error && <p role="status">{uiText('Notice history is unavailable; missing notices are unknown.')}</p>}
    {page && <>
      <p>{uiText('Dispositions are self-reported, not proof of a correction. Transient or unreadable historical warnings may be missing.')}</p>
      {page.unavailable_receipts > 0 && <p role="status">{uiText('Some command receipts are unavailable.')}</p>}
      {!page.items.length && <p>{uiText('No saved notices on this page.')}</p>}
      <ul>{page.items.map(item => <li key={item.notice_id}>
        {item.notice.kind} · {item.notice.field || '—'} · {item.disposition} · {item.command_id}
        {item.reason && ` · ${item.reason}`}
        <p>{uiText('Read the command receipt and correct the original content if needed.')}</p>
      </li>)}</ul>
      {page.offset > 0 && <button type="button" onClick={() => load(Math.max(0, page.offset - page.limit))}>{uiText('Previous')}</button>}
      {page.has_more && <button type="button" onClick={() => load(page.offset + page.limit)}>{uiText('Next')}</button>}
    </>}
  </section>
}
