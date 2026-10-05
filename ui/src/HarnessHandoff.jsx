import React, { useEffect, useRef, useState } from 'react'
import { get, runApiPath } from './util.js'
import { useScopedResource } from './useScopedResource.js'
import { PANEL_REQUEST_TIMEOUT_MS, RUN_GENERATION_RE } from './panelPrimitives.js'
import { harnessAgentInstruction, harnessMcpDescriptor, harnessServerUrl, validHarnessHandoff } from './harnessHandoff.js'
import './harness-handoff.css'
import HarnessReceipt from './HarnessReceipt.jsx'
import { useAssistantLanguage } from './useAssistantLanguage.js'
import { harnessText } from './harnessText.js'
import LazyBoundary from './LazyBoundary.jsx'

export default function HarnessHandoff({ runId, generation, seq, defaultOpen = false, focusOrigin = null }) {
  const [language] = useAssistantLanguage()
  const t = text => harnessText(language, text)
  const [open, setOpen] = useState(defaultOpen)
  const [copied, setCopied] = useState(null)
  const [client, setClient] = useState('codex')
  const summaryRef = useRef(null)
  useEffect(() => {
    // An explicit connection click replaces its trigger after the module loads.
    // Preserve another control reached while waiting, and never infer a new read.
    if (focusOrigin && (document.activeElement === focusOrigin || document.activeElement === document.body)) {
      summaryRef.current?.focus({ preventScroll: true })
    }
  }, [focusOrigin])
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
      await navigator.clipboard.writeText(harnessAgentInstruction(value, url, language))
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
  return <details className="harness-handoff" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary ref={summaryRef}>{t('Connect external agent')}</summary>
    {open && <>
      <p>{t('Use a stdio MCP client on a machine with LoopLab installed. The URL must be reachable from that client.')}</p>
      {!fresh && <p role="status">{resource.status === 'error' || resource.status === 'stale'
        ? t('Connection context unavailable. Refresh before copying.') : t('Reading connection context…')}{' '}
        <button type="button" className="btn sm ghost" disabled={!!resource.pending} onClick={() => resource.retry()}>{t('Retry')}</button></p>}
      {value && fresh && <>
        <dl>
          <dt>{t('UI / API server')}</dt><dd>{url}</dd>
          <dt>{t('Run')}</dt><dd>{value.run_id} · external harness</dd>
          <dt>{t('Run root on server')}</dt><dd>{value.server_paths.run_root}</dd>
          <dt>{t('Run directory')}</dt><dd>{value.server_paths.run_dir}</dd>
          <dt>{t('Engine probe')}</dt><dd>{value.engine_running === null ? t('Unknown') : value.engine_running ? t('Alive at last read') : t('Stopped at last read')}{t(' · agent connection is not measured')}</dd>
        </dl>
        <h4>{t('1. Configure the MCP process')}</h4>
        <label>{t('MCP client ')}<select value={client} onChange={event => { setClient(event.target.value); setCopied(null) }}>
          <option value="codex">Codex</option><option value="claude">Claude Code</option>
          <option value="generic">{t('Other MCP client')}</option></select></label>
        <pre>{harnessMcpDescriptor(url, client)}</pre>
        <button type="button" className="btn sm" onClick={copyConfig}>{t('Copy MCP configuration')}</button>
        {copied?.scope === scope && copied.type === 'config' && copied.client === client && <p role="status">{t(copied.message)}</p>}
        <p>{client === 'codex' ? t('Place this in a trusted project .codex/config.toml or your user config. env_vars forwards the scoped token from the Codex process environment.')
          : client === 'claude' ? t('Place this in your project .mcp.json. The token placeholder reads the scoped credential from the Claude process environment.')
            : t("This is a generic stdio descriptor; configure scoped credential forwarding in your client's environment settings.")}{t(' The token value is not included.')}</p>
        <p>{t('For a virtual environment, set command to the full looplab executable path on the client machine. Supply the scoped credential and restart the client. harness-mcp refuses a missing token; it never falls back to owner access.')}</p>
        <h4>{t('2. Supply the scoped credential separately')}</h4>
        <p role="status">{value.credential_configured ? t('A harness credential is configured on this server.')
          : t('Operator setup required: this server has no scoped harness credential configured.')}</p>
        <p>{t(value.credential_policy)}</p><p>{t(value.scope)}</p>
        <h4>{t('3. Connect and give the agent this run')}</h4>
        <p>{client === 'claude' ? t('Open Claude interactively in that project and review its project MCP approval prompt. Pending approval means the MCP process has not connected yet.')
          : client === 'codex' ? t('Open Codex in a trusted project and inspect /mcp to confirm that looplab loaded.')
            : t('Inspect the MCP server status in your client.')}{t(' Connected confirms the stdio process only. Tool calls can still need client approval. Use connection_check for this run and inspect its result.')}{client === 'claude' && t(' In Claude --print, exit 0 can include permission_denials.')}</p>
        <button type="button" className="btn sm" onClick={copy}>{t('Copy agent instruction')}</button>
        {copied?.scope === scope && copied.type === 'instruction' && <p role="status">{t(copied.message)}</p>}
        <details><summary>{t('Preview instruction and workspace permissions')}</summary>
          <pre>{harnessAgentInstruction(value, url, language)}</pre></details>
        <p className="muted">{t('Copying starts no work. Reconnect here with current state, receipts and checkpoints.')}</p>
      </>}
      {RUN_GENERATION_RE.test(generation || '') && <LazyBoundary label={t('Reconnect or recover a lost response')}
        resetKey={scope} language={language} focusOnFailure="if-lost">
        <HarnessReceipt key={scope} runId={runId} generation={generation} />
      </LazyBoundary>}
    </>}
  </details>
}
