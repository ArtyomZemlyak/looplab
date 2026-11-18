import { uiMessage } from './uiLanguage.js'
import React from 'react'
import { uiText, useUILanguage } from './uiLanguage.js'

const integer = value => Number.isSafeInteger(value) && value >= 0
const finite = value => typeof value === 'number' && Number.isFinite(value)
const object = value => value && typeof value === 'object' && !Array.isArray(value)
export function metricBehaviorRows(state) {
  if (!object(state?.metric_behavior_scans)) return []
  return Object.values(state.metric_behavior_scans).filter(row => object(row)
    && row.version === 1 && row.detector === 'metric_regime_shift'
    && row.authority === 'advisory_observation_only'
    && integer(row.node_id) && integer(row.generation)
    && integer(row.checked_metrics) && integer(row.shifted_metrics)
    && row.shifted_metrics <= row.checked_metrics && typeof row.is_current === 'boolean'
    && ['insufficient_evidence', 'checked', 'regime_shift'].includes(row.status)
    && object(row.subject) && row.subject.node_id === row.node_id && row.subject.attempt === row.generation
    && integer(row.subject.terminal_event_seq)
    && typeof row.evidence_digest === 'string' && /^[0-9a-f]{64}$/.test(row.evidence_digest)
    && Array.isArray(row.references) && row.references.length <= 32
    && row.references.every(ref => object(ref) && integer(ref.node_id) && integer(ref.attempt) && integer(ref.terminal_event_seq))
    && Array.isArray(row.strongest_shifts) && row.strongest_shifts.length <= 3
    && row.strongest_shifts.every(shift => object(shift) && typeof shift.metric_name === 'string'
      && finite(shift.value) && finite(shift.reference_median) && shift.reference_median > 0
      && finite(shift.ratio) && shift.ratio > 0 && integer(shift.reference_count)))
    .sort((a, b) => a.subject.terminal_event_seq - b.subject.terminal_event_seq).slice(-10)
    .map(row => ({ ...row, current: row.is_current && [row.subject, ...row.references].every(ref => {
      const node = state.nodes?.[ref.node_id]
      return node?.status === 'evaluated' && integer(node.attempt) && node.attempt === ref.attempt
        && node.protocol_version === ref.protocol_version && node.metric === ref.metric
        && !node.tombstoned
        && !(state.aborted_nodes || []).includes(ref.node_id)
    }) }))
}

const statusText = status => status === 'regime_shift' ? uiText('Numerical regime shift observed')
  : status === 'checked' ? uiText('No shift under this numerical heuristic') : uiText('Insufficient qualified evidence')

export default function MetricBehavior({ state }) {
  useUILanguage()
  const rows = metricBehaviorRows(state)
  return <section className="report-section">
    <h3>{uiText('Metric behavior observations')}</h3>
    <p>{uiText('Shows up to 10 latest native observations.')}</p>
    <p>{uiText('Declared co-metrics only. This uncalibrated advisory observation proves neither cheating, faithfulness nor causal improvement; it changes no score, winner or execution gate.')}</p>
    {!rows.length && <p>{uiText('No recorded behavior observation. Absence does not prove stable metrics.')}</p>}
    {rows.map(row => <article key={`${row.node_id}:${row.generation}`}>
      <p>#{row.node_id} · {uiText('attempt')} {row.generation} · {uiText(statusText(row.status))}</p>
      <p>{uiText(row.current ? 'Current recorded subject and references' : 'Historical observation · subject or references changed')}</p>
      <p>{uiText('Checked co-metrics')}: {row.checked_metrics} · {uiText('Shifted co-metrics')}: {row.shifted_metrics}</p>
      {row.strongest_shifts.map(shift => <p key={shift.metric_name}>
        <code>{shift.metric_name}</code>: {shift.value} · {uiText('Reference median')}: {shift.reference_median}
        {' · '}{uiText('Ratio')}: {shift.ratio} · N={shift.reference_count}
      </p>)}
      {row.status === 'regime_shift' && <p>{uiText('Do these changes match the intended objective, or did the population, target or evaluator change? This observation requests no new evaluation or repeat.')}</p>}
      <small>{uiText('Evidence digest')}: <code>{row.evidence_digest}</code></small>
    </article>)}
  </section>
}
