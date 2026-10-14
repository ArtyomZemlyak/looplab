import { uiText, useUILanguage } from './uiLanguage.js'
import React, { useEffect, useState } from 'react'
import { get, runNodeApiPath } from './api.js'
import { forensicsView } from './orchestrationModel.js'

// What a failed node actually died of (doc 78 §3): the original exception under any wrapper,
// signals per rank, exit codes and log tails — read from the node's `node_failed` /
// `node_repaired` rows on demand, newest attempt first. Earlier attempts' logs are listed too.
export default function FailureForensics({ runId, nodeId }) {
  useUILanguage()
  const [data, setData] = useState(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let on = true
    setData(null); setFailed(false)
    get(runNodeApiPath(runId, nodeId, '/failure-forensics'))
      .then(body => { if (on) setData(body) })
      .catch(() => { if (on) setFailed(true) })
    return () => { on = false }
  }, [runId, nodeId])
  if (failed) return <div className="muted">{uiText("Failure forensics unavailable.")}</div>
  if (!data) return null
  const attempts = (data.attempts || []).map(a => ({ ...a, view: forensicsView(a.forensics) }))
    .filter(a => a.view)
  if (!attempts.length && !(data.attempt_logs || []).length) return null
  return <div className="failure-forensics" data-testid="failure-forensics">
    <div className="section-h">{uiText("Failure forensics")}</div>
    {attempts.map(a => <details key={a.seq} open={a === attempts[0]}>
      <summary><code>{a.event}</code> · {uiText("generation")} {a.generation ?? '—'} · {a.view.headline}</summary>
      {a.view.root && <div><b>{uiText("Original exception")}</b>: <code>{a.view.root}</code></div>}
      {a.view.signals.length > 0 && <div><b>{uiText("Signals")}</b>: {a.view.signals.join('; ')}</div>}
      {a.view.exitCodes.length > 0 && <div><b>{uiText("Exit codes")}</b>: {a.view.exitCodes.join(', ')}</div>}
      {a.view.wrappers.length > 0 && <div className="muted">{uiText("Other exceptions")}: {a.view.wrappers.join(' · ')}</div>}
      {a.view.hints.length > 0 && <div className="muted">{uiText("Hints")}: {a.view.hints.join(', ')}</div>}
      {a.view.fault && <pre className="code">{a.view.fault}</pre>}
      {a.view.tails.map(t => <details key={t.name}><summary>{t.name}</summary><pre className="code">{t.text}</pre></details>)}
      {a.view.ranks.length > 0 && <details><summary>{uiText("Last lines per rank")}</summary>
        {a.view.ranks.map(r => <div key={r.name}><b>{r.name}</b><pre className="code">{r.text}</pre></div>)}</details>}
    </details>)}
    {(data.attempt_logs || []).length > 0 && <details>
      <summary>{uiText("Logs of earlier attempts")}</summary>
      <ul>{data.attempt_logs.map(archive => <li key={archive.archive}><code>{archive.archive}</code>: {archive.files.map(f => f.name).join(', ')}</li>)}</ul>
    </details>}
  </div>
}
