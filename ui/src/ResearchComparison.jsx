import React, { useState } from 'react'
import { get, runApiPath } from './api.js'
import { uiText, useUILanguage } from './uiLanguage.js'

const show = value => typeof value === 'number' && Number.isFinite(value) ? String(value) : 'N/A'

function IntervalPlot({ row }) {
  const { delta, ci95_low: low, ci95_high: high } = row
  if (![delta, low, high].every(v => typeof v === 'number' && Number.isFinite(v)) || low > high) return null
  const lo = Math.min(0, low, delta), hi = Math.max(0, high, delta), span = hi - lo || 1
  const x = value => 12 + 276 * (value - lo) / span
  return <figure>
    <figcaption>{uiText('Paired effect and 95% confidence interval')} · {row.metric} · {row.test}</figcaption>
    <svg width="300" height="46" viewBox="0 0 300 46" role="img"
      aria-label={`${row.metric}, ${row.test}: ${show(delta)} [${show(low)}, ${show(high)}]`}>
      <line x1="12" x2="288" y1="22" y2="22" stroke="currentColor" opacity="0.25" />
      <line x1={x(0)} x2={x(0)} y1="8" y2="36" stroke="currentColor" opacity="0.5" />
      <line x1={x(low)} x2={x(high)} y1="22" y2="22" stroke="currentColor" strokeWidth="4" />
      <circle cx={x(delta)} cy="22" r="5" fill="currentColor" />
    </svg>
    <p>{uiText('Queries')}: {show(row.n)} · {uiText('clusters')}: {show(row.clusters)}</p>
  </figure>
}

export default function ResearchComparison({ runId, experimentId }) {
  useUILanguage()
  const [data, setData] = useState(null), [status, setStatus] = useState('idle')
  const load = () => {
    setStatus('loading')
    get(`${runApiPath(runId, '/experiments/')}${encodeURIComponent(experimentId)}/comparison`, { cache: 'no-store' })
      .then(value => { setData(value); setStatus('ready') }).catch(() => setStatus('error'))
  }
  return <section>
    <button type="button" onClick={load}>{uiText('Show paired comparison')}</button>
    {status === 'loading' && <p>{uiText('Loading…')}</p>}
    {status === 'error' && <p role="status">{uiText('Comparison is unavailable.')}</p>}
    {data && <>
      {!data.contrasts?.length && <p>{uiText('No contrasts were registered.')}</p>}
      {(data.contrasts || []).map(row => <article key={row.id}>
        <h5>{row.control} → {row.treatment} · {row.metric} · {row.test}</h5>
        {row.status !== 'computed' ? <p>N/A · {row.reason}</p> : <>
          <p>{uiText('Control')}: {show(row.control_value)} · {uiText('Treatment')}: {show(row.treatment_value)}</p>
          <IntervalPlot row={row} />
          <p>Δ {show(row.delta)} · {uiText('95% CI')} [{show(row.ci95_low)}, {show(row.ci95_high)}] · p {show(row.p_two_sided)} · {uiText('adjusted p')} {show(row.p_adjusted)}</p>
          <p>N {show(row.n)} · {uiText('clusters')} {show(row.clusters)} · {uiText('same protocol')}: {row.same_protocol == null ? 'N/A' : String(row.same_protocol)}</p>
        </>}
      </article>)}
      {(data.caveats || []).length > 0 && <ul>{data.caveats.map((c, i) => <li key={i}>{c}</li>)}</ul>}
    </>}
  </section>
}
