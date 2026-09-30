import React from 'react'
import { fmt } from './util.js'
import { terminalReady, sourceIncomplete, sourceIntegrityNotice,
  bestMetricCaveats, bestMetricCaveatLabel, bestMetricCaveatNotice } from './runIndex.js'
import { hashWithRunRouteState } from './runRouteState.js'
import './assistant-run-result.css'

const measured = value => value && Number.isSafeInteger(value.node_id) && value.node_id >= 0
  && Number.isSafeInteger(value.attempt) && value.attempt >= 0
  && typeof value.value === 'number' && Number.isFinite(value.value)
  && typeof value.confirmed === 'boolean'

export default function AssistantRunResult({ run, onOpen, onAsk, onReady, askDisabled = false, askDisabledReason }) {
  React.useEffect(() => { onReady?.() }, [onReady, run?.run_id, run?.generation])
  if (!run || !terminalReady(run) || run.finalization_incomplete) return null
  const generation = /^[0-9a-f]{64}$/.test(run.generation || '') ? run.generation : null
  const base = `#/run/${encodeURIComponent(run.run_id)}`
  const report = generation ? hashWithRunRouteState(base, { generation, view: 'report' }) : base
  const evidence = run.result_summary
  const complete = !sourceIncomplete(run) && generation
    && measured(evidence?.first) && measured(evidence?.selected)
  const selected = complete ? evidence.selected : null
  const first = complete ? evidence.first : null
  const caveats = bestMetricCaveats(run)
  const direction = run.direction === 'min' ? 'lower is better'
    : run.direction === 'max' ? 'higher is better' : 'direction not recorded'
  const sameNode = first && first.node_id === selected.node_id && first.attempt === selected.attempt
  const nodeHref = node => hashWithRunRouteState(base, {
    generation, nodeId: node.node_id, nodeGeneration: node.attempt, inspectTab: 'Code',
  })
  const link = (href, text) => <a className="btn sm" href={href}
    onClick={onOpen ? event => onOpen(event, href) : undefined}>{text}</a>
  return <section className="asst-run-result" aria-label="Run result summary">
    <div className="asst-run-result-head"><strong>Run result</strong>
      <span>Free to read</span></div>
    <p>{run.stop_reason === 'error' ? 'The run ended with an error. Review partial results and failures.'
      : 'Review the recorded result before planning another experiment.'}</p>
    {selected ? <>
      <div className="asst-run-result-metric">{run.objective_key || 'Objective'} · {direction}</div>
      <dl><div><dt>First eligible experiment · #{first.node_id} · {first.confirmed ? 'mean' : 'score'}</dt><dd>{fmt(first.value)}</dd></div>
        <div><dt>Selected result · #{selected.node_id} · {selected.confirmed ? 'mean' : 'score'}</dt><dd>{fmt(selected.value)}</dd></div></dl>
      <p>{sameNode ? 'The selected result is the first eligible experiment.'
        : 'Read Report to compare these values, their evaluation conditions, and confirmation.'}</p>
      <p className="asst-run-result-caution">{selected.confirmed
        ? Number.isSafeInteger(selected.seeds) && selected.seeds >= 2
          ? `Selected mean from ${selected.seeds} seeds. Check spread and trust evidence in Report.`
          : 'Confirmation mean recorded; multiple successful seeds are not established.'
        : 'Selected result has no multi-seed confirmation. Treat it as exploratory.'}</p>
      {caveats.length > 0 && <p className="asst-run-result-caution" title={bestMetricCaveatNotice(run)}>
        Recorded caveats: {caveats.map(bestMetricCaveatLabel).join(' · ')}. Review Report and Trust.
      </p>}
    </> : <p className="asst-run-result-caution">{sourceIncomplete(run)
      ? sourceIntegrityNotice(run) : 'No complete result summary is available. Open Report to inspect the recorded evidence.'}</p>}
    <div className="asst-run-result-actions">
      {link(report, 'Read Report')}
      {selected && link(nodeHref(selected), 'Open selected code')}
      {generation && link(hashWithRunRouteState(base, { generation, panel: 'artifacts' }), 'Find artifacts')}
      {onAsk && <button className="btn sm ghost" disabled={askDisabled} onClick={onAsk}
        title={askDisabled ? askDisabledReason || 'Wait for the current action before preparing a question'
          : 'Prepare a question in this chat; review it before Send'}>Ask about this result</button>}
    </div>
    {onAsk && <div className="asst-run-result-note">Ask prepares a message. Sending it may use a paid model.</div>}
  </section>
}
