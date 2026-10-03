import React from 'react'
import { fmt } from './util.js'
import { objectiveKey } from './objectiveModel.js'
import { nodeIsActive } from './nodeProjection.js'
import { nodeComparabilityStatus, nodesComparabilitySplit, COMPARABILITY_REFUSAL_SHORT, sourceIncomplete } from './runIndex.js'
import { eligibleMeasuredResult, parentScoreDifference, scoreDifference } from './scoreComparison.js'
import { resultMeasurement } from './resultMeasurement.js'
import { nodeFeasibilityStatus, objectiveMetricSource, objectiveSourceCaveated,
  OBJECTIVE_SOURCE_LABEL, objectiveSourceHelp } from './trustSemantics.js'
import { extraMetricCaveated, extraMetricSourceLabel,
  extraMetricSourceHelp } from './extraMetrics.js'
import './experiment-result.css'

const finite = value => typeof value === 'number' && Number.isFinite(value)

// Read the recorded scoring result and confirmation separately. A shared base-eval
// receipt does not certify that two confirmation means used the same ruler.
export default function ExperimentResult({ node: n, state = {}, onTab }) {
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
  const measurement = resultMeasurement(confirmed, n.confirmed_seeds)
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
  return <section className="experiment-result" aria-label="Experiment result">
    <div className="experiment-result-head"><strong>Experiment result</strong>
      {currentMatch && n.confirmed_mean === current.confirmed_mean && eligibleMeasuredResult(n, state)
        && eligibleMeasuredResult(current, state) && n.id === state.best_node_id && <span>Selected by the engine</span>}</div>
    <p className="experiment-result-objective">{key || 'Objective'} · {direction}</p>
    <dl className="experiment-result-values">
      <div><dt>Evaluation score</dt><dd>{score ? fmt(n.metric) : '—'}</dd>
        {score && <p className={caveated ? 'experiment-result-warning' : 'muted'} title={sourceHelp}>{sourceLabel}</p>}</div>
      <div><dt>Confirmation mean</dt><dd>{confirmed ? fmt(n.confirmed_mean) : 'Not confirmed'}</dd>
        <p>{measurement.reliability}</p>
        {confirmed && <p>{finite(n.confirmed_std) && n.confirmed_std >= 0
          ? `Standard deviation: ${fmt(n.confirmed_std)}` : 'Spread is not recorded.'}</p>}</div>
    </dl>
    <p className={feasibility.tone === 'ok' && active && !excluded ? '' : 'experiment-result-warning'}>{status}.
      {active && completed ? ` ${feasibility.detail}` : ' Inspect Trace and Trust before using this result.'}</p>
    {score && caveated && <p className="experiment-result-warning">{sourceHelp}</p>}
    {confirmed && <p>Multiple seeds do not establish generalization or statistical significance. Review their spread and evaluation conditions.</p>}
    {parents.length > 0 ? <div className="experiment-result-parents"><strong>Parent comparison · evaluation scores</strong>
      <ul>{parents.map(({ id, node: parent }) => {
        const available = parent?.status === 'evaluated' && nodeIsActive(parent, state)
          && finite(parent.metric)
        return <li key={id}>#{id}: {available ? fmt(parent.metric) : 'not available'} · current evaluation score</li>
      })}</ul>
      <p className={difference == null ? 'experiment-result-warning' : ''}>{comparison}</p>
      <p>These are evaluation scores, not confirmation means. Parentage alone does not establish improvement.</p>
    </div> : <p>{Array.isArray(n.parent_ids)
      ? 'No parent experiment is recorded. This is not automatically a task baseline.'
      : 'Parent references are unavailable; refresh the evidence before interpreting this result.'}</p>}
    {onTab && <div className="experiment-result-actions">
      <button type="button" className="btn sm" onClick={() => onTab('Metrics')}>All metrics & repeat checks</button>
      <button type="button" className="btn sm ghost" onClick={() => onTab('Trust')}>Review trust evidence</button>
    </div>}
  </section>
}
