import React, { useState } from 'react'
import './tracking-panels.css'
import { uiMessage, uiText, useUILanguage } from './uiLanguage.js'
import { get, runApiPath } from './api.js'
import { useScopedResource } from './hooks.js'
import { fmtSeconds, resourceSnapshot } from './resourceSnapshotModel.js'

const _num = value => (Math.abs(value) >= 100 ? value.toFixed(0) : value.toPrecision(3))

// What one attempt used (doc 77 §4): GPUs, wall time per stage, GPU-hours, throughput, peak memory.
// Measured, derived and not-recorded are three different rows and never one number; the world
// size comes from the attempt's effective config (`GET /code`, read only when this is opened).
export default function ResourceSnapshotPanel({ runId, node }) {
  useUILanguage()
  const [open, setOpen] = useState(false)
  const attempt = Number.isSafeInteger(node?.attempt) ? node.attempt : 0
  const scope = open && node ? `${runId}:${node.id}:${attempt}:resources` : ''
  const code = useScopedResource(signal => get(runApiPath(runId, '/code'), { signal }),
    { scope, gate: scope ? null : 'idle' })
  const effectiveConfig = (code.data?.attempts || [])
    .find(row => row?.node_id === node?.id && row?.generation === attempt)?.effective_config || null
  const snap = resourceSnapshot(node, { effectiveConfig })
  if (!snap) return null
  return <details className="resource-snapshot" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{uiText('Resources of this attempt')}</summary>
    {open && <>
      <dl className="kv">
        <dt>{uiText('attempt')}</dt><dd>{uiMessage('generation {0} · {1} repair(s)', [snap.attempt, snap.repairs])}</dd>
        <dt>{uiText('GPUs')}</dt><dd>{snap.gpus != null ? <>{snap.gpus} <span className="muted">{uiText('declared by the experiment')}</span></> : <span className="muted">{uiText('not declared')}</span>}
          {snap.worldSize != null && <> · {uiMessage('world size {0}', [snap.worldSize])} <span className="muted">{uiText('from the effective config')}</span></>}</dd>
        <dt>{uiText('evaluation wall time')}</dt><dd>{snap.wallSeconds != null ? <>{fmtSeconds(snap.wallSeconds)} <span className="muted">{uiText('measured')}</span></> : <span className="muted">{uiText('not recorded')}</span>}</dd>
        <dt>{uiText('GPU-hours')}</dt><dd>{snap.gpuHours != null ? <>{snap.gpuHours.toFixed(2)} <span className="muted">{uiText('derived: declared GPUs × stage wall time, assuming every GPU was held for every stage')}</span></> : <span className="muted">{uiText('not derivable: GPUs or stage times missing')}</span>}</dd>
        <dt>{uiText('throughput')}</dt><dd>{snap.stepsPerSecond != null
          ? <>{uiMessage('{0} steps/s', [_num(snap.stepsPerSecond)])} <span className="muted">{uiMessage('derived: max_steps {0} ÷ stage {1} wall time (includes start-up)', [snap.maxSteps, snap.train.name])}</span></>
          : <span className="muted">{uiText('not derivable: no max_steps in the effective config or no timed training stage')}</span>}</dd>
        {snap.trainerRates.length > 0 && <><dt>{uiText('trainer reported')}</dt><dd>{snap.trainerRates.map(row =>
          <div key={row.key}><code>{row.key}</code> {_num(row.value)}</div>)}<span className="muted">{uiText('as the trainer reported them, in its own units')}</span></dd></>}
        <dt>{uiText('peak memory')}</dt><dd>{snap.peakMemory.length
          ? snap.peakMemory.map(row => <div key={row.key}><code>{row.key}</code> {_num(row.value)}</div>)
          : <span className="muted">{uiText('not recorded')}</span>}</dd>
      </dl>
      {snap.stages.length > 0 && <table className="tbl"><caption>{uiText('Stage wall time, this attempt')}</caption>
        <thead><tr><th scope="col">{uiText('Stage')}</th><th scope="col">{uiText('Wall time')}</th><th scope="col">{uiText('Runs')}</th><th scope="col">{uiText('Status')}</th></tr></thead>
        <tbody>{snap.stages.map(stage => <tr key={stage.name}><th scope="row"><code>{stage.name}</code></th>
          <td style={{ textAlign: 'right' }}>{stage.timed ? fmtSeconds(stage.seconds) : '—'}</td>
          <td style={{ textAlign: 'right' }}>{stage.runs}</td><td>{stage.status ? uiText(stage.status) : '—'}</td></tr>)}</tbody></table>}
      {code.status === 'error' && <p className="muted">{uiText('The effective config could not be read; world size and step rate are not shown.')}</p>}
    </>}
  </details>
}
