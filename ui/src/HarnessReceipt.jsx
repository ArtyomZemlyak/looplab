import { uiText, useUILanguage } from './uiLanguage.js'
import React, { useState } from 'react'
import { useAssistantLanguage } from './useAssistantLanguage.js'
import { harnessText } from './harnessText.js'
import { get, runApiPath, COMMAND_ID_RE } from './util.js'
import { useScopedResource } from './useScopedResource.js'
import { PANEL_REQUEST_TIMEOUT_MS } from './panelPrimitives.js'

import { receiptCommandId, validReceipt } from './harnessReceiptModel.js'
export { validReceipt } from './harnessReceiptModel.js'

export default function HarnessReceipt({ runId, generation }) {
  useUILanguage()

  return <ReceiptForm key={`${runId}:${generation}`} runId={runId} generation={generation} />
}

function ReceiptForm({ runId, generation }) {
  useUILanguage()

  const [language] = useAssistantLanguage()
  const t = text => harnessText(language, text)
  const [kind, setKind] = useState('key')
  const [identity, setIdentity] = useState('')
  const [lookup, setLookup] = useState(null)
  const valid = kind === 'id' ? COMMAND_ID_RE.test(identity)
    : identity.length > 0 && identity.length <= 512 && !/[\x00-\x1f\x7f]/.test(identity)
  const resource = useScopedResource(async signal => {
    const commandId = await receiptCommandId(lookup.kind, lookup.value)
    signal.throwIfAborted()
    const value = await get(runApiPath(runId, '/command-receipt')
      + `?expected_generation=${generation}` + (lookup.kind === 'id' ? `&command_id=${lookup.value}` : ''), {
      cache: 'no-store', signal, headers: lookup.kind === 'key' ? { 'Idempotency-Key': lookup.value } : {},
    })
    if (!validReceipt(value, generation, commandId)) throw new Error('Invalid receipt')
    return value
  }, { scope: `${runId}:${generation}:${lookup?.kind}:${lookup?.value}`, gate: lookup ? null : 'idle',
    timeout: PANEL_REQUEST_TIMEOUT_MS,
    classifyFailure: ({ error }) => ({ status: 'error', data: null,
      error: error?.code === 'receipt_key_unavailable' ? 'key_verification' : '' }) })
  const read = event => {
    event.preventDefault()
    if (!valid || resource.pending) return
    // A new observation withdraws the prior verdict even if this read is aborted.
    if (lookup?.kind === kind && lookup.value === identity) resource.retry({ mapLastGood: () => null })
    else setLookup({ kind, value: identity })
  }
  const row = resource.status === 'ready' ? resource.data.command : null
  return <details className="harness-recovery">
    <summary>{t('Reconnect or recover a lost response')}</summary>
    <ol>
      <li>{t('Keep this run. Read current state and generation; connecting MCP does not restart a stopped engine.')}</li>
      <li>{t('Observe the original command receipt, then inspect events and node results. The command may already have applied.')}</li>
      <li>{t('Read Agent cycle and checkpoints, including questions opened after the evaluator exits.')}</li>
      <li>{t('Choose an explicit continuation, pause or finish. A quiet log does not prove that an agent died.')}</li>
    </ol>
    <form onSubmit={read}>
      <label>{t('Find original command by')}{' '}<select className="text" value={kind} onChange={event => {
        setKind(event.target.value); setIdentity(''); setLookup(null)
      }}><option value="key">{t('Original Idempotency-Key')}</option><option value="id">{t('Command ID')}</option></select></label>
      <label>{kind === 'key' ? t('Original Idempotency-Key') : t('Command ID')}{' '}
        <input className="text" value={identity} maxLength={512} autoComplete="off" spellCheck={false}
          onChange={event => { setIdentity(event.target.value); setLookup(null) }} /></label>
      <button className="btn sm" type="submit" disabled={!valid || !!resource.pending}>{t('Read saved receipt')}</button>
    </form>
    <p className="muted">{t("One read only. No worker restart, command retry or resume. The key is sent in a header and kept only in this form's memory.")}</p>
    <p className="muted">{t('No original identity? Inspect Events for command_id. Do not invent a fresh key for an uncertain request.')}</p>
    {lookup && resource.pending && <p role="status">{t('Reading saved receipt…')}</p>}
    {lookup && ['error', 'stale'].includes(resource.status) && <p role="status">
      {t(resource.error === 'key_verification'
        ? 'Key verification is unavailable in this browser. Read by the original Command ID from Events.'
        : 'Receipt unavailable or changed. Read current state and evidence; absence does not prove no action occurred.')}</p>}
    {row && <div aria-label={t('Saved command receipt')}>
      <p><strong>{row.event_type} · {uiText(row.status)}</strong> · {row.id}</p>
      <p>{resource.data.terminal ? t('Command receipt is terminal; this does not prove an experiment evaluated.')
        : t('Nonterminal receipt. Reading it did not continue the command.')}</p>
      {row.event_seq !== null && <p>{t('Recorded intent event #')}{row.event_seq}{t('; verify it in Events.')}</p>}
      {row.error_code && <p>{t('Error code: ')}{row.error_code}. {row.retryable ? t('The receipt permits considering an explicit retry after checking current evidence.') : t('No retry permission recorded.')}</p>}
      <p className="muted">{t('Saved snapshot may lag events. GET /commands/{command_id} can restart a nonterminal worker; choose that recovery deliberately. Preserve the original payload and key for an exact lost-response resubmission.')}</p>
    </div>}
  </details>
}
