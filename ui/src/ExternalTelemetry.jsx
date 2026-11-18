import React, { useEffect, useState } from 'react'
import { get, runApiPath } from './api.js'
import { uiText, uiMessage } from './uiLanguage.js'

function Subtotal({ value }) {
  return <>{value?.known_sum == null ? uiText('unknown') : value.known_sum}
    {value?.unknown_count > 0 && <> · {uiMessage('Unknown receipt values: {0}', [value.unknown_count])}</>}</>
}

// All reads are passive. External provider envelopes are not independently attested invoices.
export default function ExternalTelemetry({ runId, generation, summary }) {
  const [request, setRequest] = useState(null), [page, setPage] = useState(null), [error, setError] = useState(false)
  useEffect(() => { setRequest(null); setPage(null); setError(false) }, [runId, generation])
  useEffect(() => {
    if (!request) return undefined
    let live = true
    const controller = new AbortController()
    setPage(null); setError(false)
    const params = new URLSearchParams({ expected_generation: generation, offset: request.offset, limit: 20 })
    if (request.prefix) params.set('expected_prefix', request.prefix)
    get(`${runApiPath(runId, '/external-telemetry')}?${params}`, { signal: controller.signal, cache: 'no-store' })
      .then(value => { if (live) setPage(value) }).catch(() => { if (live) setError(true) })
    return () => { live = false; controller.abort() }
  }, [runId, generation, request])
  const value = page || summary
  if (!value) return null
  return <section>
    <h4>{uiText('External harness identity and spend')}</h4>
    <p>{uiText('Provider usage is observed by the client adapter; billing is not independently attested. Agent summaries remain declared. Missing values are unknown.')}</p>
    {value.state === 'unavailable' && <p role="status">{uiText('Some usage records conflict or are malformed. Subtotals cover only usable receipts.')}</p>}
    {value.state === 'not_recorded' && <p>{uiText('External harness receipts were not recorded.')}</p>}
    {(value.llm || []).map(bucket => <article key={bucket.observation_level}>
      <h5>{uiText(bucket.observation_level === 'provider_observed' ? 'Provider response observations' : 'Agent-declared usage')}</h5>
      <p>{uiText('Input tokens')}: <Subtotal value={bucket.tokens.input_tokens} /> · {uiText('Output tokens')}: <Subtotal value={bucket.tokens.output_tokens} /> · {uiText('Total tokens')}: <Subtotal value={bucket.tokens.total_tokens} /></p>
      <ul>{bucket.cost_by_currency.map(cost => <li key={cost.currency}>{uiText('Known cost subtotal')}: {cost.known_sum} {cost.currency}</li>)}</ul>
      <p>{uiMessage('Unknown receipt costs: {0}', [bucket.cost_unknown_count])}</p>
      {bucket.omitted_currency_count > 0 && <p>{uiMessage('Currencies omitted from this summary: {0}', [bucket.omitted_currency_count])}</p>}
    </article>)}
    <p>{uiText('Agent-reported wall segments (seconds)')}: <Subtotal value={value.agent_reported_wall_seconds} /> · {uiText('Segments may overlap; this is not elapsed run time.')}</p>
    <p>{uiText('Native evaluation wall time (seconds)')}: {value.native?.evaluation_wall_seconds?.value ?? uiText('unknown')} · {uiText('GPU hours')}: {value.native?.gpu_hours?.value ?? uiText('unknown')}</p>
    <p>{uiText('Native LLM cost ledger')}: {value.native?.llm?.totals?.cost ?? uiText('unknown')} · {uiText('External receipts do not change the native LLM ledger. Money, GPU hours and wall time have no combined total.')}</p>
    <button type="button" onClick={() => setRequest({ offset: 0 })}>{uiText('Read harness revisions')}</button>
    {error && <p role="status">{uiText('Harness receipts changed or are unavailable. Read the first page again before continuing.')}</p>}
    {page?.items?.map(row => <article key={row.receipt_id}>
      <h5>#{row.node_id} · {uiText('attempt')} {row.attempt} · {row.revision.provider || uiText('unknown')} / {row.revision.model || uiText('unknown')}</h5>
      <p>{uiText('Harness revision')}: <code>{row.revision_id}</code> · {row.recorded_at}</p>
      <p>{uiText(row.observation_level === 'provider_observed' ? 'Provider response observations' : 'Agent-declared usage')} · {uiText('Provider attestation')}: {uiText('not independently attested')}</p>
      <p>{uiText('Outcome')}: {row.outcome.state === 'historical_attempt' ? uiText('Historical attempt; current outcome withheld') : `${row.outcome.status} · ${row.outcome.metric ?? uiText('not recorded')}`}</p>
      <details><summary>{uiText('Non-secret revision fingerprints')}</summary><dl>{Object.entries(row.revision).map(([key, item]) => <React.Fragment key={key}><dt>{key}</dt><dd>{item ?? uiText('unknown')}</dd></React.Fragment>)}</dl></details>
    </article>)}
    {page?.has_more && <button type="button" onClick={() => setRequest({ offset: page.next_offset, prefix: page.prefix_digest })}>{uiText('Next receipt page')}</button>}
  </section>
}
