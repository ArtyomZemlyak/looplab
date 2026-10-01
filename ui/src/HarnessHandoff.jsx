import React, { useState } from 'react'
import { get, runApiPath } from './util.js'
import { useScopedResource } from './useScopedResource.js'
import { PANEL_REQUEST_TIMEOUT_MS, RUN_GENERATION_RE } from './panelPrimitives.js'
import { harnessAgentInstruction, harnessMcpDescriptor, harnessServerUrl, validHarnessHandoff } from './harnessHandoff.js'
import './harness-handoff.css'
import HarnessReceipt from './HarnessReceipt.jsx'

export default function HarnessHandoff({ runId, generation, seq }) {
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(null)
  const [client, setClient] = useState('codex')
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
      setCopied({ scope, type: 'instruction', message: 'Agent instruction copied. Supply the scoped secret separately.' })
    } catch {
      setCopied({ scope, type: 'instruction', message: 'Clipboard unavailable. Select the instruction below and copy it manually.' })
    }
  }
  const copyConfig = async () => {
    try {
      await navigator.clipboard.writeText(harnessMcpDescriptor(url, client))
      setCopied({ scope, type: 'config', client, message: 'MCP configuration copied. Supply the scoped secret separately.' })
    } catch {
      setCopied({ scope, type: 'config', client, message: 'Clipboard unavailable. Select the configuration below and copy it manually.' })
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
        <label>MCP client <select value={client} onChange={event => { setClient(event.target.value); setCopied(null) }}>
          <option value="codex">Codex</option><option value="claude">Claude Code</option>
          <option value="generic">Other MCP client</option></select></label>
        <pre>{harnessMcpDescriptor(url, client)}</pre>
        <button type="button" className="btn sm" onClick={copyConfig}>Copy MCP configuration</button>
        {copied?.scope === scope && copied.type === 'config' && copied.client === client && <p role="status">{copied.message}</p>}
        <p>{client === 'codex' ? 'Place this in a trusted project .codex/config.toml or your user config. env_vars forwards the scoped token from the Codex process environment.'
          : client === 'claude' ? 'Place this in your project .mcp.json. The token placeholder reads the scoped credential from the Claude process environment.'
            : "This is a generic stdio descriptor; configure scoped credential forwarding in your client's environment settings."} The token value is not included.</p>
        <p>If LoopLab is installed in a virtual environment, replace command with the full path to its looplab executable on the client machine. Restart the client after supplying the credential. No scoped token means harness-mcp refuses to connect; it never falls back to owner access.</p>
        <h4>2. Supply the scoped credential separately</h4>
        <p role="status">{value.credential_configured ? 'A harness credential is configured on this server.'
          : 'Operator setup required: this server has no scoped harness credential configured.'}</p>
        <p>{value.credential_policy}</p><p>{value.scope}</p>
        <h4>3. Pass the run instruction to your agent</h4>
        <button type="button" className="btn sm" onClick={copy}>Copy agent instruction</button>
        {copied?.scope === scope && copied.type === 'instruction' && <p role="status">{copied.message}</p>}
        <details><summary>Preview instruction and workspace permissions</summary>
          <pre>{harnessAgentInstruction(value, url)}</pre></details>
        <p className="muted">Copying configures nothing and starts no experiment. Reconnect to this same run using current state, receipts and checkpoints.</p>
      </>}
      {RUN_GENERATION_RE.test(generation || '') && <HarnessReceipt key={scope} runId={runId} generation={generation} />}
    </>}
  </details>
}
