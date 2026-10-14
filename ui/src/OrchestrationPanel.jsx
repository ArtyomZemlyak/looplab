import { uiText, uiMessage, useUILanguage } from './uiLanguage.js'
import React, { useState } from 'react'
import { get } from './api.js'
import { usePoll } from './hooks.js'
import { gpuQueueView, wakeLedgerView } from './orchestrationModel.js'

// Cross-run orchestration on the runs page (doc 78): who holds the host GPU pool, who waits for it
// in FIFO order, which runs are gated, and the delivery ledger of the agent wake-up hook. Read-only;
// the hook and the gates are configured through the CLI/API (`looplab agent-notify`, `gpu-gate`).
export default function OrchestrationPanel() {
  useUILanguage()
  const [gpu, setGpu] = useState(null)
  const [wake, setWake] = useState(null)
  const [failed, setFailed] = useState(false)
  usePoll(async alive => {
    try {
      const [q, n] = await Promise.all([get('/api/gpu-queue'), get('/api/agent-notify')])
      if (!alive()) return
      setGpu(q); setWake(n); setFailed(false)
    } catch {
      if (alive()) setFailed(true)
    }
  }, 10_000, [], { pauseHidden: true })
  const queue = gpuQueueView(gpu)
  const ledger = wakeLedgerView(wake)
  const busy = queue.holder || queue.queue.length || queue.gates.length
  if (!busy && !ledger.configured && !ledger.rows.length) return null
  return <details className="orchestration-panel" data-testid="orchestration-panel">
    <summary>
      <b>{uiText("GPU queue and agent wake-ups")}</b>
      {' · '}
      {queue.holder
        ? uiMessage("GPUs held by {0}", [queue.holder.runId || `pid ${queue.holder.pid}`])
        : uiText("GPUs free")}
      {queue.queue.length ? ` · ${uiMessage("{0} waiting", [queue.queue.length])}` : ''}
      {failed ? ` · ${uiText("unavailable")}` : ''}
    </summary>
    <div className="orchestration-body">
      <section aria-label={uiText("GPU queue")}>
        <div className="section-h">{uiText("GPU queue")}</div>
        <div className="muted">{queue.holder
          ? uiMessage("Held by {0} (pid {1}) for {2}", [queue.holder.runId || '—', queue.holder.pid ?? '—', queue.holder.since || '—'])
          : uiText("No run holds the host GPU pool.")}</div>
        {queue.queue.length > 0 && <ol className="orchestration-queue">
          {queue.queue.map(row => <li key={row.position}>
            <code>{row.runId || `pid ${row.pid}`}</code> · {uiMessage("waiting {0}", [row.waited || '—'])}
          </li>)}
        </ol>}
        {queue.gates.length > 0 && <table className="tbl"><thead><tr>
          <th>{uiText("run")}</th><th>{uiText("gate")}</th><th>{uiText("waits for")}</th></tr></thead>
          <tbody>{queue.gates.map(g => <tr key={g.runId}>
            <td><code>{g.runId}</code></td>
            <td>{g.open ? uiText("open") : (g.hold ? uiText("held") : uiText("waiting"))}{g.reason ? ` · ${g.reason}` : ''}</td>
            <td>{g.waitingFor.length ? g.waitingFor.join(', ') : '—'}</td></tr>)}</tbody></table>}
      </section>
      <section aria-label={uiText("Agent wake-ups")}>
        <div className="section-h">{uiText("Agent wake-ups")}</div>
        <div className="muted">{ledger.configured
          ? uiMessage("Hook: {0} · debounce {1}s · {2}", [ledger.command || '—', ledger.debounce ?? '—', ledger.enabled ? uiText('enabled') : uiText('disabled')])
          : uiText("No notification hook is configured (looplab agent-notify).")}</div>
        {ledger.rows.length > 0 && <table className="tbl"><thead><tr>
          <th>{uiText("when")}</th><th>{uiText("status")}</th><th>{uiText("events")}</th><th>{uiText("runs")}</th><th>{uiText("detail")}</th></tr></thead>
          <tbody>{ledger.rows.slice(0, 20).map(row => <tr key={row.key} className={`wake-${row.tone}`}>
            <td>{row.when || '—'}</td>
            <td>{uiText(row.status || '—')}{row.attempt > 1 ? ` #${row.attempt}` : ''}</td>
            <td>{row.test ? uiText("test") : `${row.events} · ${row.kinds.join(', ')}`}</td>
            <td>{row.runs.join(', ') || '—'}</td>
            <td className="muted">{row.exitCode != null && row.exitCode !== 0 ? uiMessage("exit {0}", [row.exitCode]) + ' ' : ''}{row.error}</td>
          </tr>)}</tbody></table>}
      </section>
    </div>
  </details>
}
