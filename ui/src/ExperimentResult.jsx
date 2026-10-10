import { uiText, uiMessage, useUILanguage } from './uiLanguage.js'
import React from 'react'
import { fmt } from './util.js'
import { objectiveKey } from './objectiveModel.js'
import { nodeIsActive } from './nodeProjection.js'
import { nodeComparabilityStatus, nodesComparabilitySplit, COMPARABILITY_REFUSAL_SHORT, sourceIncomplete } from './runIndex.js'
import { eligibleMeasuredResult, parentScoreDifference, scoreDifference } from './scoreComparison.js'
import { nodeRepeatChecksNotApplicable, resultMeasurement } from './resultMeasurement.js'
import { nodeFeasibilityStatus, objectiveMetricSource, objectiveSourceCaveated,
  OBJECTIVE_SOURCE_LABEL, objectiveSourceHelp } from './trustSemantics.js'
import { extraMetricCaveated, extraMetricSourceLabel,
  extraMetricSourceHelp } from './extraMetrics.js'
import './experiment-result.css'

const finite = value => typeof value === 'number' && Number.isFinite(value)

// Read the recorded scoring result and confirmation separately. A shared base-eval
// receipt does not certify that two confirmation means used the same ruler.
export default function ExperimentResult({ node: n, state = {}, onTab }) {
  useUILanguage()

  const completed = n.status === 'evaluated'
  const active = nodeIsActive(n, state)
  const score = completed && finite(n.metric)
  const confirmed = completed && finite(n.confirmed_mean)
  const feasibility = nodeFeasibilityStatus(n)
  const key = objectiveKey(state)
  const source = objectiveMetricSource(n)
  const caveated = key ? extraMetricCaveated(n, key) : objectiveSourceCaveated(source)
  const sourceLabel = key ? extraMetricSourceLabel(n, key) : OBJECTIVE_SOURCE_LABEL[source.channel]
  const sourceHelp = key ? extraMetricSourceHelp(n, key) : objectiveSourceHelp(source)
  const direction = state.direction === 'min' ? 'Lower is better'
    : state.direction === 'max' ? 'Higher is better' : 'Improvement direction is not recorded'
  const parents = (Array.isArray(n.parent_ids) ? n.parent_ids : []).map(id => ({ id, node: state.nodes?.[id] }))
  const current = state.nodes?.[n.id]
  const currentMatch = current && current.id === n.id && Number.isSafeInteger(n.attempt) && n.attempt >= 0
    && n.attempt === current.attempt && n.status === current.status && n.metric === current.metric
    && Array.isArray(n.parent_ids) && Array.isArray(current.parent_ids)
    && n.parent_ids.length === current.parent_ids.length && n.parent_ids.every((id, i) => id === current.parent_ids[i])
  // Detail can lag or lead the state read. Both must support the same measured comparison;
  // never borrow a parent's current attempt to repair an absent detail reference.
  const difference = currentMatch && scoreDifference(n, current, state) === 0
    && parentScoreDifference(current, state.nodes, state) != null
    ? parentScoreDifference(n, state.nodes, state) : null
  const excluded = (state.breed_excluded || []).some(id => Number(id) === Number(n.id))
  const status = !active ? 'Removed from active results'
    : !completed ? n.status === 'failed' ? 'Evaluation failed' : 'No completed evaluation yet'
      : excluded ? 'Excluded by Trust gate' : feasibility.label
  const measurement = resultMeasurement(confirmed, n.confirmed_seeds, 'en', nodeRepeatChecksNotApplicable(n))
  const parent = parents[0]?.node
  const keys = nodeComparabilityStatus(n, parent)
  const comparison = key ? 'Objective changed; inspect its source in Metrics.'
    : parents.length > 1 ? 'Several parents; no single comparison baseline.'
      : sourceIncomplete(state) ? 'Event evidence is incomplete; comparison is not established.'
        : !currentMatch ? 'Details and current result differ or are unavailable; refresh before comparing.'
          : difference != null ? `Matching evaluation conditions recorded for the referenced parent attempt and base. Evaluation score ${difference === 0
            ? 'matches the parent' : `is ${(state.direction === 'min' ? difference < 0 : difference > 0) ? 'better' : 'worse'} by ${fmt(Math.abs(difference))}`}.`
            : parent && n.parent_comparison?.attempt !== parent.attempt
              ? 'Recorded parent attempt is unavailable or has changed; comparison is not established.'
              : keys === 'different' ? `Do not compare: ${COMPARABILITY_REFUSAL_SHORT[nodesComparabilitySplit([n, parent])]}.`
                : 'Comparison is not established. Check the recorded base, evaluation conditions, feasibility and Trust.'
  return <section className="experiment-result" aria-label={uiText("Experiment result")}>
    <div className="experiment-result-head"><strong>{uiText("Experiment result")}</strong>
      {currentMatch && n.confirmed_mean === current.confirmed_mean && eligibleMeasuredResult(n, state)
        && eligibleMeasuredResult(current, state) && n.id === state.best_node_id && <span>{uiText("Selected by the engine")}</span>}</div>
    <p className="experiment-result-objective">{((key || uiText('Objective')))} · {uiText(direction)}</p>
    <dl className="experiment-result-values">
      <div><dt>{uiText("Evaluation score")}</dt><dd>{score ? fmt(n.metric) : '—'}</dd>
        {score && <p className={caveated ? 'experiment-result-warning' : 'muted'} title={uiText(sourceHelp)}>{uiText(sourceLabel)}</p>}</div>
      <div><dt>{uiText("Confirmation mean")}</dt><dd>{((confirmed ? fmt(n.confirmed_mean) : uiText('Not confirmed')))}</dd>
        <p>{uiText(measurement.reliability)}</p>
        {confirmed && <p>{((finite(n.confirmed_std) && n.confirmed_std >= 0 ? uiMessage("Standard deviation: {0}", [fmt(n.confirmed_std)]) : uiText('Spread is not recorded.')))}</p>}</div>
    </dl>
    <p className={feasibility.tone === 'ok' && active && !excluded ? '' : 'experiment-result-warning'}>{uiText(status)}.
      {((active && completed ? ` ${uiText(feasibility.detail)}` : uiText(' Inspect Trace and Trust before using this result.')))}</p>
    {score && caveated && <p className="experiment-result-warning">{uiText(sourceHelp)}</p>}
    {confirmed && <p>{uiText("Multiple seeds do not establish generalization or statistical significance. Review their spread and evaluation conditions.")}</p>}
    {parents.length > 0 ? <div className="experiment-result-parents"><strong>{uiText("Parent comparison · evaluation scores")}</strong>
      <ul>{parents.map(({ id, node: parent }) => {
        const available = parent?.status === 'evaluated' && nodeIsActive(parent, state)
          && finite(parent.metric)
        return <li key={id}>#{id}: {((available ? fmt(parent.metric) : uiText('not available')))}{uiText(" · current evaluation score")}</li>
      })}</ul>
      <p className={difference == null ? 'experiment-result-warning' : ''}>{uiText(comparison)}</p>
      <p>{uiText("These are evaluation scores, not confirmation means. Parentage alone does not establish improvement.")}</p>
    </div> : <p>{((Array.isArray(n.parent_ids) ? uiText('No parent experiment is recorded. This is not automatically a task baseline.') : uiText('Parent references are unavailable; refresh the evidence before interpreting this result.')))}</p>}
    {onTab && <div className="experiment-result-actions">
      <button type="button" className="btn sm" onClick={() => onTab('Metrics')}>{uiText("All metrics & repeat checks")}</button>
      <button type="button" className="btn sm ghost" onClick={() => onTab('Trust')}>{uiText("Review trust evidence")}</button>
    </div>}
  </section>
}
