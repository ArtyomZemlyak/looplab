import React, { useState } from 'react'
import { get, runApiPath } from './api.js'
import { uiText } from './uiLanguage.js'

export default function ReportMetricBindings({ runId, generation, seq }) {
  const [data, setData] = useState(null), [error, setError] = useState(false)
  if (!runId || !Number.isSafeInteger(seq) || !/^[a-f0-9]{64}$/.test(generation || '')) return null
  const load = async () => {
    setError(false)
    try {
      setData(await get(`${runApiPath(runId, `/report/versions/${seq}/metric-bindings`)}?expected_generation=${generation}`, { cache: 'no-store' }))
    } catch { setError(true) }
  }
  return <section>
    <button type="button" onClick={load}>{uiText('Inspect per-query metric bindings')}</button>
    {error && <p role="status">{uiText('Metric bindings are unavailable. Refresh before acting.')}</p>}
    {data && <>
      <p>{uiText('Matching test and attempt does not establish matching GT, cohort or policy.')}</p>
      {!data.upload_history_complete && <p role="status">{uiText('Upload history is incomplete; missing evidence is unknown.')}</p>}
      {data.truncated && <p>{uiText('Additional bindings are not shown.')}</p>}
      {data.bindings.map((b, i) => <article key={i}>
        <h5>#{b.node_id} · {b.metric} · {b.test_id || uiText('test not declared')}</h5>
        {!b.uploads.length && <p>{uiText('No matching upload found; this is not a zero measurement.')}</p>}
        {b.uploads.map((u, j) => <p key={j}>{u.upload_id} · {u.uploaded_metric} · N {u.n_queries ?? 'N/A'} · {uiText('Attempt')} {u.attempt ?? 'N/A'} · {u.consistency}
          {!u.attempt_verified && ` · ${uiText('attempt not verified')}`}{u.experiment_id && ` · ${u.experiment_id} / ${u.arm}`}</p>)}
        {b.additional_uploads > 0 && <p>{uiText('Additional bindings are not shown.')}</p>}
      </article>)}
    </>}
  </section>
}
