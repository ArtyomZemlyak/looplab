import React, { useState } from 'react'
import { get, runApiPath, COMMAND_ID_RE, COMMAND_STATUSES, COMMAND_PENDING } from './util.js'
import { useScopedResource } from './useScopedResource.js'
import { PANEL_REQUEST_TIMEOUT_MS } from './panelPrimitives.js'

export function validReceipt(value, generation, commandId = '') {
  const row = value?.command
  return value?.version === 1 && value.generation === generation
    && typeof row?.id === 'string' && COMMAND_ID_RE.test(row.id) && (!commandId || row.id === commandId)
    && COMMAND_STATUSES.has(row.status) && value.terminal === !COMMAND_PENDING.has(row.status)
    && typeof row.event_type === 'string' && row.event_type.length <= 100
    && (row.event_seq === null || (Number.isSafeInteger(row.event_seq) && row.event_seq >= 0))
    && typeof row.error_code === 'string' && row.error_code.length <= 256 && typeof row.retryable === 'boolean'
}

export default function HarnessReceipt({ runId, generation }) {
  const [kind, setKind] = useState('key')
  const [identity, setIdentity] = useState('')
  const [lookup, setLookup] = useState(null)
  const valid = kind === 'id' ? COMMAND_ID_RE.test(identity)
    : identity.length > 0 && identity.length <= 512 && !/[\x00-\x1f\x7f]/.test(identity)
  const resource = useScopedResource(signal => get(runApiPath(runId, '/command-receipt')
    + `?expected_generation=${generation}` + (lookup.kind === 'id' ? `&command_id=${lookup.value}` : ''), {
    cache: 'no-store', signal, headers: lookup.kind === 'key' ? { 'Idempotency-Key': lookup.value } : {},
  }), { scope: `${runId}:${generation}:${lookup?.kind}:${lookup?.value}`, gate: lookup ? null : 'idle',
    timeout: PANEL_REQUEST_TIMEOUT_MS,
    validate: value => validReceipt(value, generation, lookup?.kind === 'id' ? lookup.value : '') ? '' : 'Invalid receipt' })
  const read = event => {
    event.preventDefault()
    if (!valid || resource.pending) return
    if (lookup?.kind === kind && lookup.value === identity) resource.retry()
    else setLookup({ kind, value: identity })
  }
  const row = resource.status === 'ready' ? resource.data.command : null
  return <details className="harness-recovery">
    <summary>Reconnect or recover a lost response</summary>
    <ol>
      <li>Keep this run. Read current state and generation; connecting MCP does not restart a stopped engine.</li>
      <li>Observe the original command receipt, then inspect events and node results. The command may already have applied.</li>
      <li>Read Agent cycle and checkpoints, including questions opened after the evaluator exits.</li>
      <li>Choose an explicit continuation, pause or finish. A quiet log does not prove that an agent died.</li>
    </ol>
    <form onSubmit={read}>
      <label>Find original command by{' '}<select className="text" value={kind} onChange={event => {
        setKind(event.target.value); setIdentity(''); setLookup(null)
      }}><option value="key">Original Idempotency-Key</option><option value="id">Command ID</option></select></label>
      <label>{kind === 'key' ? 'Original Idempotency-Key' : 'Command ID'}{' '}
        <input className="text" value={identity} maxLength={512} autoComplete="off" spellCheck={false}
          onChange={event => { setIdentity(event.target.value); setLookup(null) }} /></label>
      <button className="btn sm" type="submit" disabled={!valid || !!resource.pending}>Read saved receipt</button>
    </form>
    <p className="muted">One read only. No worker restart, command retry or resume. The key is sent in a header and kept only in this form's memory.</p>
    <p className="muted">No original identity? Inspect Events for command_id. Do not invent a fresh key for an uncertain request.</p>
    {lookup && resource.pending && <p role="status">Reading saved receipt…</p>}
    {lookup && ['error', 'stale'].includes(resource.status) && <p role="status">
      Receipt unavailable or changed. Read current state and evidence; absence does not prove no action occurred.</p>}
    {row && <div aria-label="Saved command receipt">
      <p><strong>{row.event_type} · {row.status}</strong> · {row.id}</p>
      <p>{resource.data.terminal ? 'Command receipt is terminal; this does not prove an experiment evaluated.'
        : 'Nonterminal receipt. Reading it did not continue the command.'}</p>
      {row.event_seq !== null && <p>Recorded intent event #{row.event_seq}; verify it in Events.</p>}
      {row.error_code && <p>Error code: {row.error_code}. {row.retryable ? 'The receipt permits considering an explicit retry after checking current evidence.' : 'No retry permission recorded.'}</p>}
      <p className="muted">Saved snapshot may lag events. GET /commands/&#123;command_id&#125; can restart a nonterminal worker; choose that recovery deliberately. Preserve the original payload and key for an exact lost-response resubmission.</p>
    </div>}
  </details>
}
