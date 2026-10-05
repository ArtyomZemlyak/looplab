import React from 'react'
import { uiText, uiMessage, useUILanguage } from './uiLanguage.js'
import { fmt } from './util.js'
import { nodeTheme } from './conceptId.js'

function EvidenceRows({ rows, state, onPickNode }) {
  const render = ({ node, parent, gain }) => <li key={node.id}>
    <div className="report-outcome-row">
      {onPickNode ? <button type="button" className="btn sm" onClick={() => onPickNode(node.id)}
        aria-label={uiMessage('Inspect experiment #{0}', [node.id])}>#{node.id}</button> : <b>#{node.id}</b>}
      <span>{nodeTheme(node, state) || node.operator}</span>
      <strong>{gain > 0 ? '+' : ''}{fmt(gain)}</strong>
    </div>
    <div className="muted">{uiMessage('Evaluation score {0} → {1}; parent #{2}.',
      [fmt(parent.metric), fmt(node.metric), parent.id])}</div>
  </li>
  return <>
    <ul className="report-outcome-list">{rows.slice(0, 4).map(render)}</ul>
    {rows.length > 4 && <details className="report-more-evidence">
      <summary>{uiMessage('Show {0} more comparisons', [rows.length - 4])}</summary>
      <ul className="report-outcome-list">{rows.slice(4).map(render)}</ul>
    </details>}
  </>
}

export default function ReportOutcomes({ evidence, state, onPickNode }) {
  useUILanguage()
  return <section className="report-outcomes" aria-labelledby="report-section-outcomes">
    <h2 id="report-section-outcomes" tabIndex={-1} className="section-h">{uiText('What worked and what did not')}</h2>
    <p className="report-section-intro">{uiText('Recorded outcomes first. Only scores compared with the exact parent attempt under matching conditions appear below. They do not prove that an individual technique caused the change.')}</p>
    <div className="report-outcome-columns">
      {[
        ['better', 'Better evaluation scores', 'No comparable score improvement is established.'],
        ['worse', 'Worse evaluation scores', 'No comparable score regression is recorded.'],
      ].map(([key, label, empty]) => <section key={key} aria-label={uiText(label)}>
        <h3>{uiText(label)} <span className="muted">{evidence[key].length}</span></h3>
        {evidence[key].length ? <EvidenceRows rows={evidence[key]} state={state} onPickNode={onPickNode} />
          : <p className="muted">{uiText(empty)}</p>}
      </section>)}
    </div>
    <p className="report-evidence-gap">{uiMessage('Unchanged score: {0}. Comparison not established: {1}. Missing comparison is not evidence that an approach failed.',
      [evidence.unchanged.length, evidence.unknown.length])}</p>
  </section>
}
