import React, { useState } from 'react'
import { get, runApiPath } from './util.js'
import { useScopedResource } from './useScopedResource.js'
import { PANEL_REQUEST_TIMEOUT_MS, RUN_GENERATION_RE } from './panelPrimitives.js'
import { harnessAgentInstruction, harnessMcpDescriptor, harnessServerUrl, validHarnessHandoff } from './harnessHandoff.js'
import './harness-handoff.css'

export default function HarnessHandoff({ runId, generation, seq }) {
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(null)
  const scope = `${runId}:${generation}`
  const resource = useScopedResource(signal => get(runApiPath(runId, '/harness-handoff')
    + `?expected_generation=${generation}`, { cache: 'no-store', signal }).then(value => {
    if (!validHarnessHandoff(value, runId, generation)) throw new Error('Invalid handoff context')
    return value
  }), { scope, gate: open && RUN_GENERATION_RE.test(generation || '') ? null : 'idle',
    timeout: PANEL_REQUEST_TIMEOUT_MS, deps: [seq], pollMs: 15_000 })
  const value = resource.data
  const fresh = resource.status === 'ready' && !(seq > value?.event_seq)
  const url = harnessServerUrl(location.href)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(harnessAgentInstruction(value, url))
      setCopied({ scope, message: 'Agent instruction copied. Supply the scoped secret separately.' })
    } catch {
      setCopied({ scope, message: 'Clipboard unavailable. Select the instruction below and copy it manually.' })
    }
  }
  return <details className="harness-handoff" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Connect external agent</summary>
    {open && <>
      <p>Use a stdio MCP client on a machine with LoopLab installed. The URL must be reachable from that client.</p>
      {!fresh && <p role="status">{resource.status === 'error' || resource.status === 'stale'
        ? 'Connection context unavailable. Refresh before copying.' : 'Reading connection context…'}{' '}
        <button type="button" className="btn sm ghost" disabled={!!resource.pending} onClick={() => resource.retry()}>Retry</button></p>}
      {value && fresh && <>
        <dl>
          <dt>UI / API server</dt><dd>{url}</dd>
          <dt>Run</dt><dd>{value.run_id} · external harness</dd>
          <dt>Run root on server</dt><dd>{value.server_paths.run_root}</dd>
          <dt>Run directory</dt><dd>{value.server_paths.run_dir}</dd>
          <dt>Engine probe</dt><dd>{value.engine_running === null ? 'Unknown' : value.engine_running ? 'Alive at last read' : 'Stopped at last read'} · agent connection is not measured</dd>
        </dl>
        <h4>1. Configure the MCP process</h4>
        <pre>{harnessMcpDescriptor(url)}</pre>
        <p>This is a generic stdio descriptor; place these fields in your client's MCP configuration. It contains no token.</p>
        <h4>2. Supply the scoped credential separately</h4>
        <p role="status">{value.credential_configured ? 'A harness credential is configured on this server.'
          : 'Operator setup required: this server has no scoped harness credential configured.'}</p>
        <p>{value.credential_policy}</p><p>{value.scope}</p>
        <h4>3. Pass the run instruction to your agent</h4>
        <button type="button" className="btn sm" onClick={copy}>Copy agent instruction</button>
        {copied?.scope === scope && <p role="status">{copied.message}</p>}
        <details><summary>Preview instruction and workspace permissions</summary>
          <pre>{harnessAgentInstruction(value, url)}</pre></details>
        <p className="muted">Copying configures nothing and starts no experiment. Reconnect to this same run using current state, receipts and checkpoints.</p>
      </>}
    </>}
  </details>
}
