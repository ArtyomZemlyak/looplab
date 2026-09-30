import React from 'react'
import { fmt } from './util.js'
import { objectiveKey } from './objectiveModel.js'
import { nodeIsActive } from './nodeProjection.js'
import { nodeComparabilityStatus, nodesComparabilitySplit, COMPARABILITY_REFUSAL_SHORT } from './runIndex.js'
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
  const parents = (n.parent_ids || []).map(id => ({ id, node: state.nodes?.[id] }))
  const status = !active ? 'Removed from active results'
    : !completed ? n.status === 'failed' ? 'Evaluation failed' : 'No completed evaluation yet'
      : feasibility.label
  const seeds = Number.isSafeInteger(n.confirmed_seeds) && n.confirmed_seeds >= 0
    ? n.confirmed_seeds : null
  return <section className="experiment-result" aria-label="Experiment result">
    <div className="experiment-result-head"><strong>Experiment result</strong>
      {active && completed && n.id === state.best_node_id && <span>Selected by the engine</span>}</div>
    <p className="experiment-result-objective">{key || 'Objective'} · {direction}</p>
    <dl className="experiment-result-values">
      <div><dt>Evaluation score</dt><dd>{score ? fmt(n.metric) : '—'}</dd>
        {score && <p className={caveated ? 'experiment-result-warning' : 'muted'} title={sourceHelp}>{sourceLabel}</p>}</div>
      <div><dt>Repeat checks</dt><dd>{confirmed ? fmt(n.confirmed_mean) : 'Not confirmed'}</dd>
        <p>{confirmed ? `Mean · ${seeds == null ? 'seed count not recorded' : `${seeds} successful seed${seeds === 1 ? '' : 's'}`}`
          : 'No recorded confirmation mean.'}</p>
        {confirmed && <p>{finite(n.confirmed_std) && n.confirmed_std >= 0
          ? `Standard deviation: ${fmt(n.confirmed_std)}` : 'Spread is not recorded.'}</p>}</div>
    </dl>
    <p className={feasibility.tone === 'ok' && active ? '' : 'experiment-result-warning'}>{status}.
      {active && completed ? ` ${feasibility.detail}` : ' Inspect Trace and Trust before using this result.'}</p>
    {score && caveated && <p className="experiment-result-warning">{sourceHelp}</p>}
    {confirmed ? <p>{seeds != null && seeds >= 2
      ? 'Multiple seeds do not establish generalization or statistical significance. Review their spread and evaluation conditions.'
      : 'Multiple successful repeat checks are not established.'}</p>
      : score && <p>Treat this score as exploratory until repeat checks and trust evidence support it.</p>}
    {parents.length > 0 ? <div className="experiment-result-parents"><strong>Parent comparison · evaluation scores</strong>
      <ul>{parents.map(({ id, node: parent }) => {
        const available = score && active && parent?.status === 'evaluated' && nodeIsActive(parent, state)
          && finite(parent.metric)
        const comparison = available ? key ? 'retargeted' : nodeComparabilityStatus(n, parent) : 'unavailable'
        const reason = comparison === 'different' ? COMPARABILITY_REFUSAL_SHORT[nodesComparabilitySplit([n, parent])] : ''
        return <li key={id}>#{id}: {available ? fmt(parent.metric) : 'not available'} · {comparison === 'same'
          ? 'matching evaluation conditions recorded' : comparison === 'different'
            ? `do not compare: ${reason}` : comparison === 'unknown'
              ? 'matching evaluation conditions are not established' : comparison === 'retargeted'
                ? 'objective changed; inspect its source in Metrics' : 'no completed active pair to compare'}</li>
      })}</ul>
      <p>These are evaluation scores, not confirmation means. Parentage alone does not establish improvement.</p>
    </div> : <p>No parent experiment is recorded. This is not automatically a task baseline.</p>}
    {onTab && <div className="experiment-result-actions">
      <button type="button" className="btn sm" onClick={() => onTab('Metrics')}>All metrics & repeat checks</button>
      <button type="button" className="btn sm ghost" onClick={() => onTab('Trust')}>Review trust evidence</button>
    </div>}
  </section>
}
